"""Room-specific CSI fingerprint localization. No pretrained pose weights are used."""
from __future__ import annotations

import collections
import json
import math
import os
from pathlib import Path
import time
import uuid

import numpy as np

import diagnostics

from csi_packet import USEFUL_SC
from signal_quality import SignalQuality
from csi_record import write_recording
from room_geometry import contains as custom_contains, custom_geometry, sector_bounds
from live_ab import AB_PLAN_FILE, ABPreview

WINDOW = 1.0
MIN_FRAMES = 10
RAW_PACKET_LIMIT = 100_000
GUIDED_DELAY = 10
GUIDED_SECONDS = 20
VISIT_POINTS = {"A": (1.0, 0.92), "B": (5.35, 0.72)}
VISIT_DELAY = 30
# Start and end in seconds of the capture, after the countdown.
VISIT_STAGES = (
    ("empty_before", 0, 20, "Пустая комната"),
    ("enter", 20, 35, "Войдите и займите точку {name}"),
    ("stay", 35, 55, "Стойте в точке {name}"),
    ("exit", 55, 70, "Выйдите и закройте дверь"),
    ("empty_after", 70, 90, "Пустая комната после выхода"),
)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


def group_training_sessions(sessions, room, tolerance=0.10):
    """Group repeated clicks within 10 cm and one sector; keep raw sessions intact.

    Complete-link distance prevents long chains of nearby clicks from joining
    genuinely different calibration locations.
    """
    groups = []
    result = []
    for session in sessions:
        point = session["position"]
        label = session["label"]
        if point is not None:
            group = next((g for g in groups
                          if sector_for(room, point) == g["sector"]
                          and all(np.linalg.norm(np.asarray(point) - p) <= tolerance + 1e-9
                                  for p in g["points"])), None)
            if group is None:
                group = {"label": label, "sector": sector_for(room, point), "points": []}
                groups.append(group)
            group["points"].append(np.asarray(point, dtype=np.float64))
            label = group["label"]
        result.append({**session, "label": label})
    return result


def fit_fingerprints(sessions):
    """Balance recordings: at most five temporal prototypes per recording."""
    if not sessions:
        raise ValueError("Нет обучающих записей")
    prototypes, labels, positions = [], [], []
    for s in sessions:
        for block in np.array_split(s["X"], min(5, len(s["X"]))):
            prototypes.append(np.median(block, axis=0))
            labels.append(s["label"])
            positions.append(s["position"])
    raw = np.asarray(prototypes, dtype=np.float64)
    center = np.median(raw, axis=0)
    scale = np.maximum(np.std(raw, axis=0), 0.03)
    return {"X": (raw - center) / scale, "center": center, "scale": scale,
            "labels": labels, "positions": positions}


def fit_ridge(sessions, alpha=10.0):
    """Class-balanced regularized linear classifier, fitted on training only."""
    labels = sorted({s["label"] for s in sessions})
    rows, targets = [], []
    for session in sessions:
        X = session["X"]
        # Cap each recording's influence and training cost; never mix holdout data.
        X = X[np.linspace(0, len(X) - 1, min(30, len(X))).astype(int)]
        rows.append(X)
        targets.extend([labels.index(session["label"])] * len(X))
    X = np.concatenate(rows)
    y = np.asarray(targets)
    counts = np.bincount(y, minlength=len(labels))
    weight = 1.0 / counts[y]
    weight *= len(y) / weight.sum()
    center = np.average(X, axis=0, weights=weight)
    scale = np.maximum(np.std(X, axis=0), 0.03)
    Z = (X - center) / scale
    target = np.eye(len(labels))[y]
    bias = np.average(target, axis=0, weights=weight)
    A = Z * np.sqrt(weight[:, None])
    B = (target - bias) * np.sqrt(weight[:, None])
    U, D, V = np.linalg.svd(A, full_matrices=False)
    beta = V.T @ ((D / (D * D + alpha))[:, None] * (U.T @ B))
    positions = []
    for label in labels:
        points = [s["position"] for s in sessions if s["label"] == label
                  and s["position"] is not None]
        positions.append(np.mean(points, axis=0).tolist() if points else None)
    references = []
    for session in sessions:
        for block in np.array_split(session["X"], min(5, len(session["X"]))):
            references.append(np.median(block, axis=0))
    return {"method": "ridge", "alpha": alpha, "center": center, "scale": scale,
            "beta": beta, "bias": bias, "labels": labels, "positions": positions,
            "reference": (np.asarray(references) - center) / scale}


def rssi_features(features):
    """Extract RSSI from saved 106-value link blocks without changing raw data."""
    values = np.asarray(features, dtype=np.float64)
    if values.ndim not in (1, 2) or not values.shape[-1] or values.shape[-1] % 106:
        raise ValueError("Некорректный размер признаков для контроля RSSI")
    return values[..., 52::106]


def fit_rssi_ridge(sessions):
    projected = [{**s, "X": rssi_features(s["X"])} for s in sessions]
    model = fit_ridge(projected)
    model.update(feature_mode="rssi", input_features=sessions[0]["X"].shape[1])
    return model


def csi_change_features(features, empty_reference):
    """Per-link RMS change of normalized CSI shape from a training-only empty reference."""
    a = np.asarray(features, dtype=np.float64)
    if a.ndim not in (1, 2) or not a.shape[-1] or a.shape[-1] % 106:
        raise ValueError("Некорректный размер признаков изменения CSI")
    ref = np.asarray(empty_reference, dtype=np.float64)
    n_links = a.shape[-1] // 106
    if ref.shape != (n_links, 106):
        raise ValueError("Эталон пустой комнаты не совпадает с радиолиниями")
    blocks = a.reshape((-1, n_links, 106))
    changes = np.sqrt(np.mean((blocks[:, :, :52] - ref[None, :, :52]) ** 2, axis=2))
    return changes[0] if a.ndim == 1 else changes


def fit_csi_change(sessions):
    empty = [s for s in sessions if s["label"] == "empty"]
    if not empty:
        raise ValueError("Для изменения CSI нужен обучающий эталон пустой комнаты")
    # One median per recording: longer empty recordings must not dominate the reference.
    ref = np.median(np.stack([np.median(s["X"], axis=0) for s in empty]), axis=0).reshape(-1, 106)
    projected = [{**s, "X": csi_change_features(s["X"], ref)} for s in sessions]
    model = fit_ridge(projected)
    model.update(feature_mode="csi_change", input_features=sessions[0]["X"].shape[1],
                 empty_reference=ref)
    return model


def fit_svm(sessions):
    """Experimental RBF SVM. Hyperparameters selected on training recordings."""
    from sklearn.svm import SVC
    X = np.concatenate([s["X"][np.linspace(0, len(s["X"]) - 1,
                         min(30, len(s["X"]))).astype(int)] for s in sessions])
    y = np.concatenate([np.full(min(30, len(s["X"])), s["label"], dtype=object)
                        for s in sessions])
    center = X.mean(axis=0)
    scale = np.maximum(X.std(axis=0), 0.03)
    classifier = SVC(C=1.0, gamma=0.0005, class_weight="balanced",
                     break_ties=True).fit((X - center) / scale, y)
    labels = classifier.classes_.tolist()
    positions = []
    for label in labels:
        points = [s["position"] for s in sessions if s["label"] == label
                  and s["position"] is not None]
        positions.append(np.mean(points, axis=0).tolist() if points else None)
    references = [np.median(block, axis=0) for s in sessions
                  for block in np.array_split(s["X"], min(5, len(s["X"])))]
    return {"method": "svm", "classifier": classifier, "center": center, "scale": scale,
            "labels": labels, "positions": positions,
            "reference": (np.asarray(references) - center) / scale}


def predict(model, feature):
    mode = model.get("feature_mode")
    if mode in ("rssi", "csi_change"):
        if np.asarray(feature).shape != (model["input_features"],):
            raise ValueError("Радиолинии не совпадают с моделью")
        feature = (rssi_features(feature) if mode == "rssi"
                   else csi_change_features(feature, model["empty_reference"]))
    x = (np.asarray(feature, dtype=np.float64) - model["center"]) / model["scale"]
    if model.get("method") in ("ridge", "svm"):
        if model["method"] == "svm":
            scores = model["classifier"].decision_function(x[None, :])[0]
        else:
            scores = x @ model["beta"] + model["bias"]
        order = np.argsort(scores)
        best = int(order[-1])
        margin = float(scores[best] - scores[order[-2]]) if len(order) > 1 else 1.0
        distance = float(np.min(np.sqrt(np.mean((model["reference"] - x) ** 2, axis=1))))
        # Interface compatibility: a margin quality indicator, NOT a probability.
        agreement = min(1.0, max(0.0, 0.5 + margin))
        return {"label": model["labels"][best], "position": model["positions"][best],
                "distance": distance, "agreement": agreement, "score_margin": margin}
    distances = np.sqrt(np.mean((model["X"] - x) ** 2, axis=1))
    order = np.argsort(distances)[:3]
    weights = 1.0 / np.maximum(distances[order], 0.05)
    votes = collections.defaultdict(float)
    for i, w in zip(order, weights):
        votes[model["labels"][i]] += float(w)
    label = max(votes, key=votes.get)
    agree = votes[label] / float(weights.sum())
    selected = [(int(i), float(w)) for i, w in zip(order, weights)
                if model["labels"][i] == label]
    position = None
    if label != "empty":
        positions = np.asarray([model["positions"][i] for i, _ in selected])
        position = np.average(positions, axis=0, weights=[w for _, w in selected]).tolist()
    return {"label": label, "position": position, "distance": float(distances[order[0]]),
            "agreement": float(agree)}


def inside_room(room, position):
    if not room or position is None:
        return False
    x, z = position
    if not (math.isfinite(x) and math.isfinite(z) and
            0 <= x <= room["width"] and 0 <= z <= room["depth"]):
        return False
    if room.get("shape") == "custom":
        return custom_contains(room, position)
    if room.get("shape") == "curved":
        short = room["short_width"]
        edge = short + (room["width"] - short) * math.sqrt(max(0.0, 1 - (z / room["depth"]) ** 2))
        return x <= edge + 1e-9
    return True


def sector_for(room, position):
    if not inside_room(room, position):
        return None
    left, top, right, bottom = sector_bounds(room)
    col = min(room["cols"] - 1, int((position[0] - left) / (right - left) * room["cols"]))
    row = min(room["rows"] - 1, int((position[1] - top) / (bottom - top) * room["rows"]))
    return f"{row + 1}:{col + 1}"


def evaluate(model, sessions, threshold, room=None):
    total = accepted = correct = empty_total = false_present = 0
    present_total = false_empty = sector_correct = 0
    errors = []
    recording_checks = []
    for s in sessions:
        recording_correct = 0
        for row in s["X"]:
            total += 1
            present_total += s["position"] is not None
            empty_total += s["label"] == "empty"
            p = predict(model, row)
            known = p["distance"] <= threshold and p["agreement"] >= 0.6
            if not known:
                continue
            accepted += 1
            correct += p["label"] == s["label"]
            if s["position"] is None:
                recording_correct += p["label"] == "empty"
            elif p["position"] is not None:
                recording_correct += (sector_for(room, s["position"]) == sector_for(room, p["position"])
                                      if room else p["label"] == s["label"])
            if s["position"] is not None and p["label"] == "empty":
                false_empty += 1
            if room and s["position"] is not None and p["position"] is not None:
                sector_correct += sector_for(room, s["position"]) == sector_for(room, p["position"])
            if s["label"] == "empty" and p["label"] != "empty":
                false_present += 1
            if s["position"] is not None and p["position"] is not None:
                errors.append(float(np.linalg.norm(np.asarray(p["position"]) - s["position"])))
        recording_checks.append({"id": s.get("id"), "label": s["label"], "windows": len(s["X"]),
                                 "presence_or_sector_accuracy": float(recording_correct / len(s["X"]))
                                 if len(s["X"]) else 0.0})
    return {"windows": total, "coverage": accepted / total if total else 0,
            "label_accuracy": correct / total if total else 0,
            "accepted_accuracy": correct / accepted if accepted else None,
            "position_windows": len(errors),
            "median_error_m": float(np.median(errors)) if errors else None,
            "p90_error_m": float(np.percentile(errors, 90)) if errors else None,
            "empty_windows": empty_total, "false_present": false_present,
            "present_windows": present_total, "false_empty": false_empty,
            "sector_accuracy": sector_correct / present_total if present_total else None,
            "position_coverage": len(errors) / present_total if present_total else None,
            "recordings": recording_checks}


class Localization:
    def __init__(self, directory, dataset_id=None):
        self.root_directory = Path(directory)
        if dataset_id is None:
            pointer = self.root_directory / "active_dataset.json"
            dataset_id = json.loads(pointer.read_text(encoding="utf-8"))["id"] if pointer.exists() else "original"
        if not self.valid_dataset_id(dataset_id):
            raise ValueError("Некорректный идентификатор набора")
        self.dataset = {"id": dataset_id, "name": "Первоначальные записи", "created": None}
        self.directory = self.dataset_path(dataset_id)
        self.room = None
        self.lines = []
        self.devices = {}
        self.sessions = []
        self.buffers = {}
        self.signal_quality = SignalQuality()
        self.recording = None
        self.guided = None
        self.ab_preview = None
        self.model = None
        self.threshold = 1.0
        self.report = None
        self.message = "Задайте размеры комнаты"
        self.last = {"state": "untrained", "position": None}
        self.next_tick = 0.0
        self.last_feature = None
        self._load()

    @staticmethod
    def valid_dataset_id(dataset_id):
        return dataset_id == "original" or (
            isinstance(dataset_id, str) and len(dataset_id) == 32
            and all(c in "0123456789abcdef" for c in dataset_id))

    def dataset_path(self, dataset_id):
        if not self.valid_dataset_id(dataset_id):
            raise ValueError("Некорректный идентификатор набора")
        return (self.root_directory if dataset_id == "original"
                else self.root_directory / "experiments" / dataset_id)

    def datasets(self):
        paths = [self.root_directory / "manifest.json"]
        paths.extend(sorted((self.root_directory / "experiments").glob("*/manifest.json")))
        rows = []
        for path in paths:
            if not path.exists():
                continue
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                dataset_id = "original" if path.parent == self.root_directory else path.parent.name
                if not self.valid_dataset_id(dataset_id):
                    continue
                metadata = manifest.get("dataset", {})
                rows.append({"id": dataset_id,
                             "name": metadata.get("name", "Первоначальные записи"),
                             "recordings": len(manifest.get("sessions", []))})
            except (OSError, ValueError, TypeError):
                continue
        return rows

    def open_dataset(self, dataset_id):
        if self.recording:
            raise ValueError("Сначала дождитесь окончания записи")
        if not (self.dataset_path(dataset_id) / "manifest.json").exists():
            raise ValueError("Набор записей не найден")
        replacement = Localization(self.root_directory, dataset_id=dataset_id)
        if replacement.room is None:
            raise ValueError("Набор повреждён: " + replacement.message)
        replacement.buffers = self.buffers
        replacement.signal_quality = self.signal_quality
        save_json(self.root_directory / "active_dataset.json", {"id": dataset_id})
        self.__dict__.update(replacement.__dict__)
        self.message = "Набор открыт. Для восстановления модели нажмите «Обучить»"

    def new_dataset(self):
        if self.recording:
            raise ValueError("Сначала дождитесь окончания записи")
        if self.room is None:
            raise ValueError("Сначала сохраните план комнаты")
        self._create_dataset("Контроль A/B", self.room, self.devices)
        self.message = "Создан отдельный контроль A/B. Старые записи сохранены"

    def _create_dataset(self, name, room, devices):
        dataset_id = uuid.uuid4().hex
        replacement = Localization(self.root_directory, dataset_id=dataset_id)
        replacement.dataset = {"id": dataset_id,
                               "name": name + " №" + str(len(self.datasets())),
                               "created": time.time()}
        replacement.room = room
        replacement.devices = devices
        replacement._save()
        self.open_dataset(dataset_id)

    def mark_diagnostic(self, session_id):
        """Keep a test used in model development, but remove it from fresh evaluation."""
        if self.recording:
            raise ValueError("Дождитесь окончания записи")
        session = next((s for s in self.sessions if s["id"] == session_id), None)
        if session is None or session["role"] not in ("test", "diagnostic"):
            raise ValueError("Для диагностики нужна сохранённая проверочная запись")
        session["role"] = "diagnostic"
        self._save()
        self._update_test_report()
        self.message = "Запись сохранена как диагностика. Для независимой оценки нужны новые записи"

    def _load(self):
        path = self.directory / "manifest.json"
        if not path.exists():
            return
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            metadata = manifest.get("dataset", {})
            self.dataset["name"] = metadata.get("name", self.dataset["name"])
            self.dataset["created"] = metadata.get("created")
            self.room = manifest.get("room")
            self.lines = manifest.get("lines", [])
            devices = manifest.get("devices", {})
            if not isinstance(devices, dict):
                raise ValueError("Некорректные позиции оборудования")
            self.devices = {key: self._device_value(key, value["position"][0],
                            value["position"][1], value.get("height"))
                            for key, value in devices.items()}
            for item in manifest.get("sessions", []):
                filename = item["id"] + ".npz"
                if Path(filename).name != filename:
                    raise ValueError("Некорректное имя записи")
                with np.load(self.directory / filename, allow_pickle=False) as data:
                    X = data["X"].astype(np.float64)
                if (X.ndim != 2 or len(X) < 3 or X.shape[1] != 106 * len(self.lines)
                        or not np.isfinite(X).all()):
                    raise ValueError("Некорректные признаки")
                self.sessions.append({**item, "X": X})
            self.message = "Записи загружены. Нажмите «Обучить»"
        except Exception as error:
            self.room, self.lines, self.sessions, self.devices = None, [], [], {}
            self.message = "Не удалось загрузить записи: " + str(error)

    def _save(self):
        save_json(self.directory / "manifest.json", {
            "version": 1, "dataset": self.dataset, "room": self.room, "lines": self.lines, "devices": self.devices,
            "sessions": [{k: v for k, v in s.items() if k != "X"} for s in self.sessions]})

    def configure(self, width, depth, cols, rows, shape="rectangle",
                  short_width=None, declared_area=None, *, outline=None,
                  curves=None, cutouts=None, fork_if_recorded=False, base_dataset_id=None,
                  new_plan=False, sector_mode="manual"):
        if self.recording:
            raise ValueError("Сначала остановите запись")
        if base_dataset_id is not None and base_dataset_id != self.dataset["id"]:
            raise ValueError("Набор записей сменился. Откройте редактор заново")
        custom = custom_geometry(outline, curves, cutouts) if shape == "custom" else None
        if custom:
            width, depth = custom["width"], custom["depth"]
        if sector_mode == "auto":
            if custom:
                left, top, right, bottom = sector_bounds({"shape": "custom", **custom})
                span_x, span_z = right - left, bottom - top
            else:
                span_x, span_z = float(width), float(depth)
            if not (math.isfinite(span_x) and math.isfinite(span_z)):
                raise ValueError("Нужны конечные размеры")
            cols, rows = (max(1, min(10, math.ceil(round(span, 8) / 2)))
                          for span in (span_x, span_z))
        elif sector_mode != "manual":
            raise ValueError("Неизвестный режим сетки")
        values = (float(width), float(depth), float(cols), float(rows))
        if not all(math.isfinite(v) for v in values):
            raise ValueError("Нужны конечные размеры")
        if not (1 <= values[0] <= 50 and 1 <= values[1] <= 50):
            raise ValueError("Размеры комнаты: от 1 до 50 м")
        if values[2] != int(values[2]) or values[3] != int(values[3]):
            raise ValueError("Число секторов должно быть целым")
        if not (1 <= values[2] <= 10 and 1 <= values[3] <= 10):
            raise ValueError("Сетка: от 1 до 10 по каждой оси")
        room = {"width": values[0], "depth": values[1],
                "cols": int(values[2]), "rows": int(values[3]), "shape": shape}
        if shape not in ("rectangle", "curved", "custom"):
            raise ValueError("Неизвестная форма комнаты")
        if custom:
            room.update(custom)
        elif shape == "curved":
            short = float(short_width)
            if not math.isfinite(short) or not 0 < short < room["width"]:
                raise ValueError("Короткая стена должна быть больше 0 и меньше длинной")
            room["short_width"] = short
            room["geometry_note"] = "Правая стена приближена четвертью эллипса; точная дуга не измерена"
            room["sketch_area"] = round(room["depth"] * (short + math.pi / 4 * (room["width"] - short)), 3)
        else:
            room["sketch_area"] = round(room["width"] * room["depth"], 3)
        if sector_mode == "auto":
            room["sector_mode"] = "auto"
        if declared_area is not None:
            area = float(declared_area)
            left, top, right, bottom = sector_bounds(room)
            if not math.isfinite(area) or not 0 < area <= (right - left) * (bottom - top) + 1e-8:
                raise ValueError("Проверьте площадь")
            room["declared_area"] = area
        if new_plan:
            self._create_dataset("План", room, {})
            self.message = "Создан новый план. Расставьте оборудование и выберите точки записи"
            return
        devices = {key: device for key, device in self.devices.items()
                   if inside_room(room, device["position"])}
        removed = len(devices) != len(self.devices)
        if removed and not fork_if_recorded:
            raise ValueError("Оборудование окажется вне нового плана. Сначала исправьте его позиции")
        if self.sessions and self.room != room:
            if not fork_if_recorded:
                raise ValueError("План уже связан с записями. Используйте отдельный набор данных")
            self._create_dataset("Изменённый план", room, devices)
            self.message = "Новый план открыт отдельным набором. Прежние записи доступны в списке"
        else:
            previous = self.room, self.devices, self.lines
            self.room, self.devices = room, devices
            if not self.sessions:
                self.lines = []
            try:
                self._save()
            except OSError:
                self.room, self.devices, self.lines = previous
                raise
            self.message = "План сохранён. Выберите точку и запишите примеры"
        if removed:
            self.message += ". Заново разместите устройства, оказавшиеся за контуром или в вырезе"

    def _device_value(self, device, x, z, height):
        if device not in ("router", "esp1", "esp2"):
            raise ValueError("Выберите роутер, ESP №1 или ESP №2")
        x, z = float(x), float(z)
        if not inside_room(self.room, (x, z)):
            raise ValueError("Устройство должно находиться внутри границ комнаты")
        if height is not None:
            height = float(height)
            if not math.isfinite(height) or not 0 <= height <= 10:
                raise ValueError("Высота устройства: от 0 до 10 м")
        return {"position": [x, z], "height": height}

    def set_device(self, device, x, z, height=None):
        if self.recording:
            raise ValueError("Дождитесь окончания записи")
        if self.room is None:
            raise ValueError("Сначала сохраните план комнаты")
        if device not in ("router", "esp1", "esp2"):
            raise ValueError("Неизвестное устройство")
        value = None if x is None and z is None else self._device_value(device, x, z, height)
        if self.sessions and value != self.devices.get(device):
            raise ValueError("Размещение закреплено за собранными записями. Для другой расстановки нужен новый набор")
        previous = dict(self.devices)
        if value is None:
            self.devices.pop(device, None)
        else:
            self.devices[device] = value
        try:
            self._save()
        except OSError:
            self.devices = previous
            raise
        self.message = "Положение устройства сохранено" if value else "Метка устройства убрана"

    def add(self, receiver, frame, now, raw_packet=None):
        if frame.version != 2 or frame.src_id is None:
            return
        rec = self.recording
        if (raw_packet is not None and rec is not None and rec["role"] in ("diagnostic", "visit")
                and rec["start"] <= now < rec["end"]
                and any(k.startswith(f"{receiver} <- {frame.src_id:04X} / ch") for k in self.lines)):
            # Retain exact wire data before quality filtering, including duplicates and bad RF frames.
            if len(rec["raw_packets"]) < RAW_PACKET_LIMIT:
                when = rec["created"] + rec["delay"] + (now - rec["start"])
                rec["raw_packets"].append((when, receiver, bytes(raw_packet)))
            else:
                rec["raw_dropped"] += 1
        if not self.signal_quality.add(receiver, frame, now):
            return
        key = f"{receiver} <- {frame.src_id:04X} / ch{frame.channel}"
        amp = np.asarray(frame.amp[USEFUL_SC], dtype=np.float64)
        if amp.shape != (52,) or not np.isfinite(amp).all() or amp.mean() < 0.01:
            return
        mean = float(amp.mean())
        values = np.r_[amp / mean, frame.rssi / 60.0, math.log1p(mean)]
        buf = self.buffers.setdefault(key, collections.deque(maxlen=400))
        buf.append((now, values))

    def active_lines(self, now):
        return sorted(k for k, b in self.buffers.items() if b and now - b[-1][0] < 0.6)

    def feature(self, now, lines=None):
        lines = self.lines if lines is None else lines
        if not lines:
            return None
        if self.signal_quality.assess(lines, now)["blocked"]:
            return None
        parts = []
        for key in lines:
            samples = [v for t, v in self.buffers.get(key, []) if now - WINDOW <= t <= now]
            buf = self.buffers.get(key)
            if (len(samples) < MIN_FRAMES or not buf or now - buf[-1][0] > 0.5):
                return None
            a = np.asarray(samples)
            # 52 normalized amplitudes + RSSI + log power + 52 temporal deviations.
            parts.extend((np.mean(a, axis=0), np.std(a[:, :52], axis=0)))
        return np.concatenate(parts)

    def ranging_snapshot(self, now):
        lines = self.active_lines(now)
        values = self.feature(now, lines)
        return {
            "type": "ranging", "dataset_id": self.dataset["id"], "room": self.room,
            "devices": self.devices, "lines": lines,
            "quality": self.signal_quality.assess(lines, now),
            "features": None if values is None else values.tolist(),
        }

    def start(self, x, z, role, delay, seconds, now):
        if self.room is None:
            raise ValueError("Сначала сохраните размеры комнаты")
        if self.recording:
            raise ValueError("Запись уже идёт")
        if role not in ("train", "test", "diagnostic"):
            raise ValueError("Назначение записи: train, test или diagnostic")
        if x is None and z is None:
            position, label = None, "empty"
        else:
            x, z = float(x), float(z)
            if not inside_room(self.room, (x, z)):
                raise ValueError("Точка должна быть внутри комнаты")
            position = [round(x, 3), round(z, 3)]
            label = f"point:{position[0]:.3f},{position[1]:.3f}"
        delay, seconds = float(delay), float(seconds)
        if not (math.isfinite(delay) and math.isfinite(seconds) and
                5 <= delay <= 120 and 10 <= seconds <= 180):
            raise ValueError("Отсчёт 5–120 с, запись 10–180 с")
        if not self.lines:
            active = self.active_lines(now)
            if len({line.split(" <- ")[0] for line in active}) < 2:
                raise ValueError("Для начала записи нужны данные от двух ESP")
            self.lines = active
            self._save()
        if self.feature(now) is None:
            raise ValueError("Не все выбранные радиолинии передают достаточно данных")
        self.recording = {"id": uuid.uuid4().hex, "position": position, "label": label,
                          "role": role, "start": now + delay, "end": now + delay + seconds,
                          "X": [], "skipped": 0, "created": time.time(), "duration": seconds,
                          "quality_log": [], "skipped_quality": 0, "delay": delay,
                          "raw_packets": [], "raw_dropped": 0}
        self.message = "Отсчёт запущен. Займите точку или покиньте комнату для записи «Пусто»"

    def start_guided(self, points, dataset_id, owner, now):
        if self.recording:
            raise ValueError("Дождитесь окончания текущей записи")
        if self.room is None or dataset_id != self.dataset["id"]:
            raise ValueError("План изменился. Выберите клетки заново")
        if not isinstance(points, list) or not 2 <= len(points) <= 256:
            raise ValueError("Выберите минимум две разные клетки")
        targets = []
        for point in points:
            if (not isinstance(point, list) or len(point) != 2
                    or not all(type(value) in (int, float) and 0 <= value <= limit
                               for value, limit in zip(point, (self.room["width"], self.room["depth"])))):
                raise ValueError("Некорректные координаты клетки")
            target = tuple(round(value, 3) for value in point)
            if not inside_room(self.room, target) or target in targets:
                raise ValueError("Выберите разные клетки внутри комнаты")
            targets.append(target)

        lines = self.active_lines(now)
        if len({line.split(" <- ")[0] for line in lines}) < 2 or self.feature(now, lines) is None:
            raise ValueError("Для калибровки нужны пригодные кадры от обеих ESP")
        if self.sessions:
            self._create_dataset("Калибровка радиовизора", self.room, self.devices)
        else:
            self.lines = []
        self.stop_ab_preview()
        self.guided = {"id": uuid.uuid4().hex, "owner": owner, "steps": [None, None, *targets, *targets],
                       "completed": 0, "state": "running", "message": ""}
        self._start_guided_step(now)

    def _start_guided_step(self, now):
        point = self.guided["steps"][self.guided["completed"]]
        try:
            self.start(*(point or (None, None)), "train", GUIDED_DELAY, GUIDED_SECONDS, now)
        except (ValueError, OSError) as error:
            self._end_guided("failed", str(error))
            raise
        self.log_recording_start(self.recording)

    def _end_guided(self, state, message):
        self.guided.update(state=state, message=message, owner=None)

    def stop_guided_on_disconnect(self, owner):
        if self.guided and self.guided["owner"] is owner:
            self.stop()
            self.guided["message"] = "Связь с вкладкой потеряна. Завершённые записи сохранены"

    def start_visit(self, name, now):
        point = VISIT_POINTS.get(name)
        if not inside_room(self.room, point):
            raise ValueError(f"Контрольная точка {name} находится вне плана комнаты")
        stages = [{"name": phase, "start_s": start, "end_s": end, "instruction": text.format(name=name)}
                  for phase, start, end, text in VISIT_STAGES]
        self.start(None, None, "diagnostic", VISIT_DELAY, stages[-1]["end_s"], now)
        self.recording.update(role="visit", label=f"visit:{name}",
                              visit={"target": list(point), "planned_stages": stages})
        self.message = f"Заход в {name} запланирован. Выйдите и закройте дверь до окончания отсчёта"

    def stop(self):
        if self.recording:
            self.log_recording_end(self.recording, "cancelled", "user_cancelled")
        self.recording = None
        self.message = "Запись отменена, незавершённые данные не сохранены"
        if self.guided and self.guided["state"] == "running":
            self._end_guided("cancelled", "Калибровка остановлена. Завершённые записи сохранены")

    @staticmethod
    def log_recording_start(rec):
        target = "visit" if rec["role"] == "visit" else "empty" if rec["position"] is None else "point"
        diagnostics.event("recording.started", model="sectors", capture=rec["id"], role=rec["role"],
                          target=target, position=rec["visit"]["target"] if target == "visit" else rec["position"],
                          delay_s=rec["delay"], duration_s=rec["duration"])

    @staticmethod
    def log_recording_end(rec, result, reason=None):
        diagnostics.event("recording.finished", model="sectors", capture=rec["id"],
                          role=rec["role"], target="visit" if rec["role"] == "visit" else "empty" if rec["position"] is None else "point",
                          result=result, reason=reason, windows=len(rec["X"]), skipped=rec["skipped"],
                          skipped_quality=rec["skipped_quality"])

    def _finish(self):
        rec = self.recording
        self.recording = None
        minimum = max(8, int(rec["duration"] * 0.8))
        if len(rec["X"]) < minimum:
            self.log_recording_end(rec, "failed", "too_few_windows")
            self.message = f"Запись не сохранена: {len(rec['X'])} окон, нужно хотя бы {minimum}. Проверьте обе ESP"
            return
        X = np.stack(rec["X"])
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / (rec["id"] + ".npz")
        np.savez_compressed(path, X=X,
                            quality_json=np.array(json.dumps(rec["quality_log"], allow_nan=False)))
        item = {k: rec[k] for k in ("id", "position", "label", "role", "created", "skipped")}
        item["windows"] = len(X)
        item["quality_version"] = 1
        item["skipped_quality"] = rec["skipped_quality"]
        item["raw_packet_count"] = len(rec["raw_packets"])
        item["raw_dropped_count"] = rec["raw_dropped"]
        item["raw_file"] = None
        if rec["role"] == "visit":
            item["visit"] = rec["visit"]
        if rec["raw_packets"]:
            raw_name = rec["id"] + ".ruvrec"
            metadata = {"session_id": rec["id"], "dataset": self.dataset,
                        "role": rec["role"], "position": rec["position"],
                        "start_unix": rec["created"] + rec["delay"],
                        "duration_seconds": rec["duration"], "delay_seconds": rec["delay"],
                        "lines": self.lines, "devices": self.devices,
                        "raw_dropped_count": rec["raw_dropped"],
                        "timestamp_note": "PC receive time; original ESP timestamps remain in packets"}
            if rec["role"] == "visit":
                metadata["visit"] = rec["visit"]
            try:
                write_recording(self.directory / raw_name, rec["raw_packets"], metadata, rec["label"])
                item["raw_file"] = raw_name
            except OSError as error:
                item["raw_capture_error"] = str(error)
        self.sessions.append({**item, "X": X})
        try:
            self._save()
        except OSError:
            self.sessions.pop()
            raise
        self.log_recording_end(rec, "saved")
        self.message = f"Запись сохранена: {len(X)} окон; пропущено {rec['skipped']}"
        if item["raw_file"]:
            self.message += f"; сырых пакетов: {item['raw_packet_count']}"
            if item["raw_dropped_count"]:
                self.message += f"; не сохранено сырых пакетов: {item['raw_dropped_count']}"
        elif item.get("raw_capture_error"):
            self.message += "; ошибка сохранения сырых пакетов: " + item["raw_capture_error"]
        if self.model is not None and rec["role"] in ("test", "diagnostic"):
            self._update_test_report()

    def _update_test_report(self):
        if self.model is None or self.report is None:
            return
        tests = [s for s in self.sessions if s["role"] == "test"]
        diagnostics = [s for s in self.sessions if s["role"] == "diagnostic"]
        self.report["test_recordings"] = len(tests)
        self.report["test"] = evaluate(self.model, tests, self.threshold, self.room) if tests else None
        self.report["diagnostic_recordings"] = len(diagnostics)
        self.report["diagnostic"] = (evaluate(self.model, diagnostics, self.threshold, self.room)
                                     if diagnostics else None)
        # Operational guard, not a statistical accuracy guarantee. Adjacent windows
        # remain correlated; one failed held-out recording is enough to stop display.
        checks = (self.report["test"] or {}).get("recordings", [])
        self.report["failed_test_recordings"] = [r["id"] for r in checks
                                                if r["windows"] >= 8
                                                and r["presence_or_sector_accuracy"] < .5]
        save_json(self.directory / "report.json", self.report)

    @staticmethod
    def training_ready(counts):
        return counts.get("empty", 0) >= 2 and len(counts) >= 3 and min(counts.values()) >= 2

    def train(self, algorithm="ridge"):
        fitters = {"ridge": fit_ridge, "svm": fit_svm, "ridge_rssi": fit_rssi_ridge,
                   "ridge_csi_change": fit_csi_change}
        if algorithm not in fitters:
            raise ValueError("Неизвестная модель")
        fitter = fitters[algorithm]
        if self.recording:
            raise ValueError("Дождитесь окончания записи")
        train = group_training_sessions([s for s in self.sessions if s["role"] == "train"],
                                        self.room)
        counts = collections.Counter(s["label"] for s in train)
        if not self.training_ready(counts):
            missing = ", ".join(f"{label}: {count}/2" for label, count in counts.items()
                                if count < 2)
            raise ValueError("Нужны минимум 2 отдельные записи «Пусто» и по 2 записи хотя бы в 2 точках"
                             + (". Не хватает: " + missing if missing else ""))
        # Entire recording held out. Its windows never take part in scaling or fitting.
        heldout, distances = [], []
        for i, s in enumerate(train):
            model = fitter(train[:i] + train[i + 1:])
            for row in s["X"]:
                distances.append(predict(model, row)["distance"])
            heldout.append((model, s))
        self.threshold = max(0.5, float(np.percentile(distances, 95)) * 1.5)
        folds = [evaluate(m, [s], self.threshold, self.room) for m, s in heldout]
        self.model = fitter(train)
        test = [s for s in self.sessions if s["role"] == "test"]
        self.report = {
            "method": {"ridge": "Class-balanced ridge CSI classifier",
                       "svm": "Experimental RBF SVM",
                       "ridge_rssi": "Ridge RSSI-only control",
                       "ridge_csi_change": "Ridge — изменение CSI от пустой комнаты"}[algorithm],
            "algorithm": algorithm,
            "feature_mode": {"ridge_rssi": "rssi", "ridge_csi_change": "csi_change"}.get(algorithm, "csi_full"),
            "feature_count": len(self.lines) if algorithm in ("ridge_rssi", "ridge_csi_change") else 106 * len(self.lines),
            "parameters": {"C": 1.0, "gamma": 0.0005} if algorithm == "svm" else {"alpha": 10.0},
            "threshold": self.threshold,
            "validation_note": "Порог выбран по обучающим сеансам. Независимую точность смотрите в test.",
            "recording_cv_accuracy": float(np.average([f["label_accuracy"] for f in folds],
                                                       weights=[f["windows"] for f in folds])),
            "training_recordings": len(train), "test_recordings": len(test),
            "grouping_tolerance_m": 0.10,
            "training_places": [{"label": label, "recordings": count}
                                for label, count in sorted(counts.items())],
            "test": evaluate(self.model, test, self.threshold, self.room) if test else None}
        total_windows = sum(len(s["X"]) for s in train)
        class_windows = collections.Counter()
        for session in train:
            class_windows[session["label"]] += len(session["X"])
        baseline = max(class_windows.values()) / total_windows
        self.report["majority_baseline"] = baseline
        self.report["passes_basic_check"] = self.report["recording_cv_accuracy"] > baseline
        self._update_test_report()
        self.message = ("Модель обучена. Для оценки точности запишите отдельные проверочные сеансы"
                        if self.report["passes_basic_check"] else
                        "Модель не прошла контроль: результат не лучше постоянного выбора самого частого класса. Координаты скрыты")
        if self.report["failed_test_recordings"]:
            self.message = "Модель не прошла независимую проверку. Координаты скрыты"
        return self.report

    def start_ab_preview(self, now):
        if self.recording:
            raise ValueError("Дождитесь окончания текущей записи")
        if not (self.directory / AB_PLAN_FILE).is_file():
            raise ValueError("В этом наборе нет эталонов для пробного режима A/B")
        if self.feature(now) is None:
            raise ValueError("Для калибровки нужны данные от всех радиолиний")
        self.ab_preview = ABPreview.from_dataset(self.directory, self.dataset["id"], self.lines,
                                                  self.sessions, VISIT_POINTS, now)
        self.last = {"state": "ab_calibrating", "position": None, "sector": None, "mode": "ab"}

    def stop_ab_preview(self):
        if self.ab_preview is not None:
            self.ab_preview = None
            self.last = {"state": "unknown", "position": None, "sector": None}

    def sector(self, position):
        return sector_for(self.room, position)

    def tick(self, now, presence_state=None):
        if now < self.next_tick:
            return None
        self.next_tick = now + WINDOW
        quality = self.signal_quality.assess(self.lines, now)
        feature = self.feature(now)
        self.last_feature = feature
        rec = self.recording
        if rec:
            if now >= rec["end"]:
                try:
                    self._finish()
                except OSError as error:
                    self.log_recording_end(rec, "failed", "save_failed")
                    diagnostics.failure("recording.save_failed", error, model="sectors")
                    if self.guided and self.guided["state"] == "running":
                        self._end_guided("failed", "Не удалось сохранить запись")
                    raise
                if self.guided and self.guided["state"] == "running":
                    if not self.sessions or self.sessions[-1]["id"] != rec["id"]:
                        self._end_guided("failed", self.message)
                    else:
                        self.guided["completed"] += 1
                        if self.guided["completed"] == len(self.guided["steps"]):
                            self._end_guided("done", "Калибровка завершена. Можно обучить модель")
                        else:
                            try:
                                self._start_guided_step(now)
                            except (ValueError, OSError):
                                # The result is already available in the next status snapshot.
                                pass
            elif now >= rec["start"]:
                rec["quality_log"].append({"t": round(now - rec["start"], 3), "quality": quality})
                if feature is None:
                    rec["skipped_quality"] += int(quality["specific_fault"])
                    rec["skipped"] += 1
                else:
                    rec["X"].append(feature.copy())
        self.last = {"state": "untrained", "position": None, "sector": None}
        if self.ab_preview is not None:
            self.last = self.ab_preview.update(now, feature)
            self.ab_preview.record_state(now, self.last)
            if self.last["position"] is not None:
                self.last["sector"] = self.sector(self.last["position"])
        elif self.model is not None:
            if feature is None:
                self.last["state"] = "poor_signal" if quality["specific_fault"] else "missing_lines"
            elif self.report and (self.report.get("passes_basic_check") is False
                                  or self.report.get("failed_test_recordings")):
                self.last["state"] = "model_unreliable"
            else:
                p = predict(self.model, feature)
                if p["distance"] > self.threshold or p["agreement"] < 0.6:
                    self.last.update(state="unknown", distance=p["distance"])
                elif p["label"] == "empty":
                    self.last["state"] = "empty"
                else:
                    self.last.update(state="estimated", position=p["position"],
                                     sector=self.sector(p["position"]), distance=p["distance"],
                                     agreement=p["agreement"])
        return self.status(now)

    def status(self, now):
        training = group_training_sessions([s for s in self.sessions if s["role"] == "train"], self.room)
        counts = collections.Counter(s["label"] for s in training)
        rec = self.recording
        progress = None
        if rec:
            waiting = now < rec["start"]
            progress = {"phase": "waiting" if waiting else "recording",
                        "left_s": max(0, math.ceil((rec["start"] if waiting else rec["end"]) - now)),
                        "position": rec["position"], "role": rec["role"],
                        "windows": len(rec["X"]), "skipped": rec["skipped"],
                        "raw_packet_count": len(rec["raw_packets"]),
                        "raw_dropped_count": rec["raw_dropped"]}
            if rec["role"] == "visit" and not waiting:
                elapsed = now - rec["start"]
                step = next((stage for stage in rec["visit"]["planned_stages"]
                             if stage["start_s"] <= elapsed < stage["end_s"]), None)
                if step is not None:
                    progress["visit_instruction"] = step["instruction"]
                    progress["visit_step_left_s"] = math.ceil(step["end_s"] - elapsed)
        guided = None
        if self.guided:
            guided = {key: self.guided[key] for key in ("id", "state", "completed", "message")}
            guided["total"] = len(self.guided["steps"])
        return {"type": "localization", "room": self.room, "devices": self.devices, "lines": self.lines,
                "dataset": self.dataset, "datasets": self.datasets(),
                "quality": self.signal_quality.assess(self.lines, now),
                "active_lines": self.active_lines(now), "recording": progress, "guided_calibration": guided,
                "sessions": [{k: v for k, v in s.items() if k != "X"} for s in self.sessions],
                "model_ready": self.model is not None, "report": self.report, "can_train": self.training_ready(counts),
                "ab_available": (self.directory / AB_PLAN_FILE).is_file(),
                "ab_preview": self.ab_preview.status(now) if self.ab_preview is not None else None,
                "ab_changes": list(self.ab_preview.changes) if self.ab_preview is not None else [],
                "message": self.message, "estimate": self.last}
