"""Calibrate one-room Wi-Fi fingerprint ranging against marked X/Z positions."""

import argparse
from itertools import combinations
import json
import math
from pathlib import Path
import sys
import time

import numpy as np
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from localization import Localization, inside_room, save_json
from room_geometry import sector_bounds


SERVER = "ws://127.0.0.1:8765/ranging"
DATA = Path(__file__).resolve().parent / "localization_data"
DEVICES = ("router", "esp1", "esp2")
TOLERANCE_MM = 130
FEATURES_PER_LINE = 106


def snapshot(ws):
    ws.send('{"cmd":"ranging_status"}')
    deadline = time.monotonic() + 4
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            message = json.loads(ws.recv(timeout=remaining))
        except TimeoutError:
            break
        if message.get("type") == "ranging":
            return message
    raise TimeoutError("Сервер не ответил на ranging_status. Запустите обновлённый nn_pipeline.py.")


def layout(status):
    if not status["room"] or any(key not in status["devices"] for key in DEVICES):
        raise ValueError("Сохраните план и позиции роутера и двух ESP в localization.html.")
    return {key: status[key] for key in ("dataset_id", "room", "devices")}


def mm(metres):
    return round(1000 * metres)


def distances(point, devices):
    return {key: mm(math.dist(point, devices[key]["position"])) for key in DEVICES}


def reference_points(room, fractions):
    left, top, right, bottom = sector_bounds(room)
    points = ((round(left + x * (right - left), 2), round(top + z * (bottom - top), 2))
              for x, z in fractions)
    return [list(p) for p in points if inside_room(room, p)]


def survey(ws, state, seconds):
    config = layout(state)
    for a, b in combinations(DEVICES, 2):
        separation = math.dist(config["devices"][a]["position"], config["devices"][b]["position"])
        print(f"{a} ↔ {b}: {mm(separation)} мм по плану")
    readings = {}
    for state in stream(ws, state, seconds):
        for key, info in state["quality"]["lines"].items():
            readings.setdefault(key, []).append(info)
    for key, entries in sorted(readings.items()):
        medians = {}
        for field in ("packets_per_window", "rssi_dbm", "noise_dbm", "snr_est_db"):
            values = [entry[field] for entry in entries if entry[field] is not None]
            medians[field] = round(float(np.median(values)), 1) if values else None
        print(f"{key}: {len(entries)} окон, {sum(e['ok'] for e in entries)} пригодны, {medians}")
    if not readings:
        print("Нет активных радиолиний.")


def stream(ws, state, count=None):
    config = layout(state)
    index = 0
    while count is None or index < count:
        start = time.monotonic()
        current = snapshot(ws)
        if layout(current) != config:
            raise ValueError("План или положение устройств изменились. Повторите калибровку.")
        yield current
        index += 1
        if count is None or index < count:
            time.sleep(max(0, 1.05 - (time.monotonic() - start)))


def capture(ws, state, prompt, seconds, settle):
    input(f"\n{prompt}. Нажмите Enter для отсчёта.")
    for remaining in range(settle, 0, -1):
        print(f"\rЗапись через {remaining:2} с", end="", flush=True)
        time.sleep(1)
    print(f"\rЗаписываю {seconds} с... ", end="", flush=True)
    samples = [s["features"] for s in stream(ws, state, seconds)
               if s["lines"] == state["lines"] and s["features"] is not None]
    print(f"{len(samples)}/{seconds} пригодных окон.")
    if len(samples) < math.ceil(seconds * 0.8):
        raise ValueError("Мало пригодных окон. Проверьте радиолинии командой survey и повторите.")
    return np.asarray(samples, dtype=float)


def magnitude(values):
    return np.sqrt(np.mean(np.square(values), axis=-1))


def radio_changes(empty, captures, points, lines):
    background = np.median(empty, axis=0).reshape(-1, FEATURES_PER_LINE)
    reports = []
    for point, capture in zip(points, captures):
        measured = np.median(capture, axis=0).reshape(background.shape)
        amplitude_ratio = np.expm1(measured[:, 53]) / np.expm1(background[:, 53])
        reports.append({"position_m": point, "links": {
            key: {"rssi_change_db": round(float(60 * (reading[52] - baseline[52])), 2),
                  "csi_amplitude_change_pct": round(float(100 * (ratio - 1)), 1),
                  "csi_shape_rms_change": round(float(magnitude(reading[:52] - baseline[:52])), 4)}
            for key, reading, baseline, ratio in zip(lines, measured, background, amplitude_ratio)
        }})
    return reports


def fit(empty, captures, points, config, lines, *, anchor_only=False):
    baseline = np.median(empty, axis=0)
    # Floors keep flat or quantized measurements from overwhelming the other features.
    floors = np.tile(np.r_[np.full(52, 0.025), 1 / 60, 0.04, np.full(52, 0.015)], len(lines))
    noise = np.maximum(np.percentile(np.abs(empty - baseline), 75, axis=0), floors)
    # Each 52-carrier group has the same aggregate weight as one scalar metric.
    weights = np.tile(np.r_[np.full(52, 1 / math.sqrt(52)), 1, 1,
                            np.full(52, 1 / math.sqrt(52))], len(lines))
    scale = weights / noise
    empty_threshold = max(0.1, float(np.percentile(magnitude((empty - baseline) * scale), 95)) * 1.3)
    normalized = [(block - baseline) * scale for block in captures]
    centers = np.stack([np.median(block, axis=0) for block in normalized])
    radius_floor, radius_factor = (0.08, 1.5) if anchor_only else (0.2, 3)
    radii = [max(radius_floor, radius_factor * float(np.percentile(magnitude(block - center), 95)))
             for block, center in zip(normalized, centers)]
    return {
        "version": 1, "layout": config, "lines": lines, "positions": points, "anchor_only": anchor_only,
        "baseline": baseline.tolist(), "scale": scale.tolist(), "centers": centers.tolist(),
        "empty_threshold": empty_threshold, "radii": radii,
        "radio_changes": radio_changes(empty, captures, points, lines),
    }


def estimate(model, feature):
    if feature is None:
        return None
    vector = (np.asarray(feature) - model["baseline"]) * model["scale"]
    if magnitude(vector) <= model["empty_threshold"]:
        return None
    errors = magnitude(model["centers"] - vector)
    order = np.argsort(errors)[:3]
    best = order[0]
    if errors[best] > model["radii"][best]:
        return None
    if model.get("anchor_only"):
        return model["positions"][best]
    points = np.asarray(model["positions"])
    weights = 1 / np.maximum(errors[order], 0.01) ** 3
    position = np.average(points[order], axis=0, weights=weights).tolist()
    return position if inside_room(model["layout"]["room"], position) else points[best].tolist()


def validation(model, controls, empty, tolerance):
    devices = model["layout"]["devices"]
    errors = {key: [] for key in DEVICES}
    total = sum(len(block) for _, block in controls)
    coverage = []
    for point, block in controls:
        known = distances(point, devices)
        hits = 0
        for feature in block:
            position = estimate(model, feature)
            if position is not None:
                hits += 1
                for key, value in distances(position, devices).items():
                    errors[key].append(abs(value - known[key]))
        coverage.append({"position_mm": [mm(v) for v in point], "estimated": hits, "windows": len(block)})
    accepted = len(errors["router"])
    p90 = {key: math.ceil(np.percentile(values, 90)) if values else None
           for key, values in errors.items()}
    false_empty = sum(estimate(model, feature) is not None for feature in empty)
    return {
        "control_points": coverage, "estimated_windows": accepted, "windows": total,
        "p90_error_mm": p90, "empty_false_positions": false_empty, "empty_windows": len(empty),
        "tolerance_mm": tolerance,
        "passes_at_control_points": (accepted > 0 and all(row["estimated"] >= math.ceil(0.8 * row["windows"])
                                                         for row in coverage)
                                     and not false_empty and all(v <= tolerance for v in p90.values())),
    }


def calibrate(ws, state, args, path):
    config = layout(state)
    room = config["room"]
    points = args.point or reference_points(room, ((.25, .25), (.75, .25), (.25, .75), (.75, .75)))
    checks = args.check or reference_points(room, ((.5, .5), (.5, .25)))
    all_points = points + checks
    if (len(points) < 3 or not checks or len({tuple(p) for p in all_points}) != len(all_points)
            or any(not inside_room(room, p) for p in all_points)):
        raise ValueError("Нужны три разные точки --point и отдельная --check внутри плана.")
    print("Координаты X/Z в метрах. Отметьте точки на полу рулеткой от начала плана.")
    print(f"Эталоны: {points}. Независимые точки проверки: {checks}.")
    empty = capture(ws, state, "Освободите помещение", args.seconds, args.settle)
    captures = [capture(ws, state, f"Займите отметку X={x:.2f}, Z={z:.2f}",
                        args.seconds, args.settle) for x, z in points]
    model = fit(empty, captures, points, config, state["lines"])
    controls = [(p, capture(ws, state, f"Займите контрольную отметку X={p[0]:.2f}, Z={p[1]:.2f}",
                           args.seconds, args.settle)) for p in checks]
    empty_after = capture(ws, state, "Снова освободите помещение", args.seconds, args.settle)
    model["validation"] = validation(model, controls, empty_after, args.tolerance_mm)
    save_json(path, model)
    print(json.dumps(model["validation"], ensure_ascii=False, indent=2))
    print(f"Калибровка сохранена: {path}")


def live(ws, state, path):
    if not path.is_file():
        raise ValueError("Калибровка отсутствует. Сначала выполните calibrate.")
    model = json.loads(path.read_text(encoding="utf-8"))
    if model["version"] != 1 or model["layout"] != layout(state):
        raise ValueError("Файл не соответствует текущему плану или расстановке. Повторите calibrate.")
    print("Результат контрольных измерений:", json.dumps(model["validation"], ensure_ascii=False))
    passed = model["validation"]["passes_at_control_points"]
    for current in stream(ws, state):
        values = current["features"] if current["lines"] == model["lines"] else None
        point = estimate(model, values)
        if point is None:
            print("Нет оценки: пусто, незнакомый сигнал или не хватает радиолиний.", flush=True)
            continue
        print(json.dumps({"x_mm": mm(point[0]), "z_mm": mm(point[1]), "passes_at_control_points": passed,
                          "distance_mm": distances(point, model["layout"]["devices"])},
                         ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description="Калибровка оценки расстояния до устройств по CSI в одной комнате.")
    parser.add_argument("command", choices=("survey", "calibrate", "live"))
    parser.add_argument("--seconds", type=int, default=15, help="Окон на сеанс измерения")
    parser.add_argument("--settle", type=int, default=15, help="Секунд на переход к следующей точке")
    parser.add_argument("--point", type=float, nargs=2, action="append", metavar=("X", "Z"),
                        help="Отметка для обучения в метрах. Повторите для каждой точки.")
    parser.add_argument("--check", type=float, nargs=2, action="append", metavar=("X", "Z"),
                        help="Отдельная отметка для проверки в метрах. Повторите для каждой точки.")
    parser.add_argument("--tolerance-mm", type=int, default=TOLERANCE_MM,
                        help="Допуск для 90-го процентиля ошибки на контрольных точках.")
    args = parser.parse_args()
    if not 5 <= args.seconds <= 180 or not 0 <= args.settle <= 120 or args.tolerance_mm <= 0:
        parser.error("Допустимы --seconds 5–180, --settle 0–120 и положительный допуск.")
    try:
        with connect(SERVER, open_timeout=4) as ws:
            state = snapshot(ws)
            if args.command == "survey":
                survey(ws, state, args.seconds)
                return 0
            config = layout(state)
            path = Localization(DATA, config["dataset_id"]).directory / "ranging_calibration.json"
            if args.command == "calibrate":
                if len({line.split()[0] for line in state["lines"]}) < 2 or state["features"] is None:
                    raise ValueError("Для калибровки нужен стабильный CSI от двух ESP. Начните с survey.")
                calibrate(ws, state, args, path)
            else:
                live(ws, state, path)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, WebSocketException, EOFError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
