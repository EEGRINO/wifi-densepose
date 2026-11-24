"""Local point management, timed captures and offline training for the ranging page."""

import json
import math
import uuid
from collections import Counter
from copy import deepcopy

import numpy as np

import diagnostics
from csi_ranging import TOLERANCE_MM, distances, estimate, evaluate_position, fit, layout, validation
from localization import WINDOW, inside_room, save_json

REFERENCE = "background"
EMPTY_CHECK = "empty_check"
MIN_TRAINING_POINTS = 3


class RangingWeb:
    def __init__(self, localization):
        self.localization = localization
        self.path = None
        self.data = {}
        self.recording = None
        self.message = ""

    def load(self):
        path = self.localization.directory / "ranging_web.json"
        if path == self.path:
            return
        data = (json.loads(path.read_text(encoding="utf-8")) if path.exists()
                else {"points": [], "captures": {}, "model": None, "report": None})
        if self.recording:
            self.log_recording_end(self.recording, "cancelled", "dataset_changed")
        self.path, self.data = path, data
        self.recording = None
        self.message = ""

    def save(self, **changes):
        data = {**self.data, **changes}
        save_json(self.path, data)
        self.data = data

    def capture_changes(self, key, capture, role):
        captures = dict(self.data["captures"])
        if capture is None:
            captures.pop(key, None)
        else:
            captures[key] = capture
        changes = {"captures": captures, "report": None}
        if role == "train":
            changes["model"] = None
        elif key == EMPTY_CHECK and capture is not None:
            model = self.data["model"]
            if (model and model.get("recording_check") and model["layout"] == capture["layout"]
                    and model["lines"] == capture["lines"]):
                changes["model"] = {**model, "recording_check": {
                    **model["recording_check"], "empty": self.check_empty(model, capture["windows"]),
                }}
        return changes

    def status(self, snapshot, now):
        config = {key: snapshot[key] for key in ("dataset_id", "room", "devices")}
        model = self.data["model"]
        if model is not None and model["layout"] != config:
            model = None
        diagnostic = model.get("recording_check") if model else None
        report = self.data["report"] if model else None
        background_failed = bool(diagnostic and diagnostic["empty"] and diagnostic["empty"]["false_positions"])
        if report is not None:
            background_failed = bool(report["empty_false_positions"])
        signal_ready = self.signal_ready(snapshot)
        point, live_reason = None, None
        if not model:
            live_state = "model_needed"
        elif background_failed:
            live_state = "background_failed"
        elif not set(model["lines"]).issubset(snapshot["lines"]):
            live_state = "lines_changed" if signal_ready else "waiting_signal"
        else:
            features = (snapshot["features"] if model["lines"] == snapshot["lines"]
                        else self.localization.feature(now, model["lines"]))
            live_reason, point = evaluate_position(model, features)
            live_state = live_reason if live_reason in ("waiting_signal", "estimated") else "unknown"
        points = self.data["points"]
        captures = self.data["captures"]
        background = captures.get(REFERENCE)
        compatible = {key: value for key, value in captures.items() if value["layout"] == config}
        if background is not None and background["layout"] == config:
            compatible = {key: value for key, value in compatible.items() if value["lines"] == background["lines"]}
        windows = {key: len(value["windows"]) for key, value in compatible.items()}
        trainers = [point["id"] for point in points if point["role"] == "train"]
        checks = [point["id"] for point in points if point["role"] == "test"]
        valid_points = snapshot["room"] and all(inside_room(snapshot["room"], p["position"]) for p in points)
        rec = self.recording
        return {
            "type": "ranging_web", "dataset": self.localization.dataset, "room": snapshot["room"],
            "devices": snapshot["devices"], "points": points, "windows": windows,
            "stale": sorted(set(captures) - set(compatible)), "lines": snapshot["lines"],
            "signal_ready": signal_ready, "model_ready": model is not None, "live_state": live_state,
            "live_reason": live_reason,
            "busy_elsewhere": self.localization.recording is not None,
            "training": {"points": len(trainers), "recorded": sum(key in windows for key in trainers),
                         "required": MIN_TRAINING_POINTS, "background": REFERENCE in windows},
            "can_train": bool(valid_points and len(trainers) >= MIN_TRAINING_POINTS
                              and all(key in windows for key in [REFERENCE, *trainers])),
            "can_validate": bool(valid_points and model and checks
                                 and all(key in windows for key in [EMPTY_CHECK, *checks])),
            "report": report, "recording_check": diagnostic, "background_failed": background_failed,
            "estimate": None if point is None else {"position": point, "distance_mm": distances(point, config["devices"])},
            "recording": None if rec is None else {
                "target": rec["target"], "phase": "waiting" if now < rec["start"] else "recording",
                "left_s": max(0, math.ceil((rec["start"] if now < rec["start"] else rec["end"]) - now)),
                "windows": len(rec["windows"]),
            },
            "message": self.message,
        }

    def load_for_diagnostics(self):
        try:
            self.load()
            if not isinstance(self.data, dict):
                raise ValueError("Invalid ranging data")
            return True
        except (OSError, ValueError) as error:
            diagnostics.current.changed("ranging.load_error", "ranging.model_load_failed",
                            (diagnostics.dataset_id(self.localization.dataset), type(error).__name__),
                            level="warning", exception=type(error).__name__)
            return False

    def observe_position(self, now):
        snapshot = self.localization.ranging_snapshot(now)
        status = self.status(snapshot, now)
        model = self.data.get("model") if status["model_ready"] else None
        point = status["estimate"]["position"] if status["estimate"] else None
        diagnostics.position("ranging", state=status["live_state"], reason=status["live_reason"],
                             active_lines=snapshot["lines"], expected_lines=model["lines"] if model else [],
                             ready=status["model_ready"], point=point)

    @staticmethod
    def signal_ready(snapshot):
        return (snapshot["features"] is not None
                and len({line.split(" <- ")[0] for line in snapshot["lines"]}) >= 2)

    def tick(self, now):
        rec = self.recording
        if rec is None:
            return
        if (self.localization.directory / "ranging_web.json" != self.path
                or self.localization.room != rec["layout"]["room"]
                or self.localization.devices != rec["layout"]["devices"]):
            self.recording = None
            self.message = "План или расстановка изменились. Текущая запись остановлена."
            self.log_recording_end(rec, "cancelled", "layout_changed")
            return
        if now >= rec["end"]:
            self.recording = None
            if len(rec["windows"]) < math.ceil(rec["seconds"] * .8):
                self.log_recording_end(rec, "failed", "too_few_windows")
                self.message = f"Запись прервана: собрано {len(rec['windows'])}/{rec['seconds']} пригодных окон. Повторите измерение."
                return
            capture = {key: rec[key] for key in ("layout", "lines", "windows")}
            try:
                self.save(**self.capture_changes(rec["target"], capture, rec["role"]))
            except OSError as error:
                self.message = f"Запись не сохранена: {error}"
                self.log_recording_end(rec, "failed", "save_failed")
                diagnostics.failure("recording.save_failed", error, model="ranging")
                return
            self.message = f"Запись сохранена: {len(rec['windows'])} окон."
            self.log_recording_end(rec, "saved")
        elif now >= rec["next"]:
            snapshot = self.localization.ranging_snapshot(now)
            rec["next"] += WINDOW
            if rec["next"] <= now:
                rec["next"] = now + WINDOW
            if snapshot["lines"] == rec["lines"] and snapshot["features"] is not None:
                rec["windows"].append(snapshot["features"])
            else:
                rec["skipped"] = rec.get("skipped", 0) + 1

    @staticmethod
    def log_recording_end(rec, result, reason=None):
        diagnostics.event("recording.finished", model="ranging", capture=rec["capture_id"],
                          role=rec["role"], target=rec["kind"], result=result, reason=reason,
                          windows=len(rec["windows"]), skipped=rec.get("skipped", 0))

    def handle(self, cmd, now):
        self.load()
        snapshot = self.localization.ranging_snapshot(now)
        action = cmd["cmd"]
        if action == "ranging_web_status":
            return self.status(snapshot, now)
        if cmd.get("dataset_id") != snapshot["dataset_id"]:
            raise ValueError("Открыт другой набор. Обновите страницу.")
        if action == "ranging_web_cancel":
            if self.recording:
                self.log_recording_end(self.recording, "cancelled", "user_cancelled")
            self.recording = None
            self.message = "Запись отменена."
            return self.status(snapshot, now)
        if self.recording or self.localization.recording:
            raise ValueError("Дождитесь окончания текущей записи.")

        room = snapshot["room"]
        points = self.data["points"]
        key = cmd.get("id")
        selected = next((point for point in points if point["id"] == key), None)
        if action == "ranging_web_add":
            role = cmd.get("role")
            if not room or role not in ("train", "test"):
                raise ValueError("Сохраните план и выберите назначение точки.")
            position = [round(float(cmd[axis]), 3) for axis in ("x", "z")]
            if not all(map(math.isfinite, position)) or not inside_room(room, position):
                raise ValueError("Точка должна находиться внутри комнаты.")
            if len(points) >= 64 or any(point["position"] == position for point in points):
                raise ValueError("Точка с такими координатами уже есть или достигнут предел в 64 точки.")
            changes = {"model": None} if role == "train" else {}
            self.save(points=[*points, {"id": uuid.uuid4().hex, "position": position, "role": role}],
                      report=None, **changes)
            self.message = "Точка добавлена."
        elif action == "ranging_web_delete":
            if selected is None:
                raise ValueError("Точка не найдена.")
            self.save(points=[p for p in points if p["id"] != key],
                      **self.capture_changes(key, None, selected["role"]))
            self.message = "Точка удалена."
        elif action == "ranging_web_promote":
            if selected is None or selected["role"] != "test":
                raise ValueError("Выберите точку независимой проверки.")
            self.save(points=[{**p, "role": "train"} if p["id"] == key else p for p in points],
                      model=None, report=None)
            self.message = "Точка перенесена в обучение. Запись сохранена."
        elif action == "ranging_web_record":
            config = layout(snapshot)
            if key not in (REFERENCE, EMPTY_CHECK) and (not selected or not inside_room(room, selected["position"])):
                raise ValueError("Выберите сохранённую точку внутри комнаты.")
            if not self.signal_ready(snapshot):
                raise ValueError("Для записи нужны пригодные кадры от двух ESP.")
            delay, seconds = cmd.get("delay"), cmd.get("seconds")
            if type(delay) is not int or type(seconds) is not int or not 0 <= delay <= 120 or not 5 <= seconds <= 60:
                raise ValueError("Отсчёт 0–120 с, запись 5–60 с.")
            start = now + delay
            self.recording = {"target": key, "role": selected["role"] if selected else
                              ("train" if key == REFERENCE else "test"),
                              "layout": deepcopy(config), "lines": snapshot["lines"], "windows": [],
                              "start": start, "end": start + seconds, "next": start, "seconds": seconds}
            rec = self.recording
            rec.update(capture_id=uuid.uuid4().hex, kind="point" if selected else key, skipped=0)
            diagnostics.event("recording.started", model="ranging", capture=rec["capture_id"],
                              role=rec["role"], target=rec["kind"], delay_s=delay, duration_s=seconds,
                              position=selected["position"] if selected else None)
            self.message = "Займите отмеченную точку или освободите комнату для записи фона."
        elif action in ("ranging_web_train", "ranging_web_validate"):
            self.process(action, snapshot, now, cmd)
        else:
            raise ValueError("Неизвестная команда калибровки.")
        return self.status(snapshot, now)

    def process(self, action, snapshot, now, cmd):
        status = self.status(snapshot, now)
        config = layout(snapshot)
        captures = self.data["captures"]
        if action == "ranging_web_train":
            if not status["can_train"]:
                raise ValueError("Запишите пустую комнату и все точки обучения. Нужно минимум три точки.")
            points = [point for point in self.data["points"] if point["role"] == "train"]
            background = captures[REFERENCE]
            blocks = [np.asarray(captures[point["id"]]["windows"]) for point in points]
            positions = [point["position"] for point in points]
            baseline = np.asarray(background["windows"])
            model = fit(baseline, blocks, positions, deepcopy(config), background["lines"], anchor_only=True)
            empty = captures[EMPTY_CHECK]["windows"] if EMPTY_CHECK in status["windows"] else []
            model["recording_check"] = self.check_recordings(model, blocks, positions, empty)
            model["recording_check"]["held_out"] = self.check_time_blocks(
                baseline, blocks, positions, config, background["lines"])
            self.save(model=model, report=None)
            self.message = "Модель обучена по сохранённым записям."
            return
        if not status["can_validate"]:
            raise ValueError("Обучите модель, запишите все точки проверки и контрольную пустую комнату.")
        tolerance = cmd.get("tolerance_mm", TOLERANCE_MM)
        if type(tolerance) is not int or tolerance <= 0:
            raise ValueError("Укажите положительный допуск в миллиметрах.")
        checks = [(point["position"], np.asarray(captures[point["id"]]["windows"]))
                  for point in self.data["points"] if point["role"] == "test"]
        report = validation(self.data["model"], checks, np.asarray(captures[EMPTY_CHECK]["windows"]), tolerance)
        self.save(report=report)
        self.message = "Независимая проверка завершена."

    @staticmethod
    def check_recordings(model, blocks, positions, empty):
        rows = []
        for block, expected in zip(blocks, positions):
            results = [estimate(model, sample) for sample in block]
            counts = Counter(tuple(point) for point in results if point is not None)
            most_common = counts.most_common(1)
            rows.append({"position": expected, "matched": counts[tuple(expected)], "windows": len(block),
                         "estimated": sum(counts.values()),
                         "distance_mm": distances(most_common[0][0], model["layout"]["devices"]) if most_common else None})
        return {"training": rows, "empty": RangingWeb.check_empty(model, empty)}

    @staticmethod
    def check_time_blocks(background, blocks, positions, config, lines):
        """Use disjoint time blocks; leave a one-window guard around each test block."""
        if any(len(block) < 4 for block in blocks):
            return None
        partitions = [np.array_split(np.arange(len(block)), 3) for block in blocks]
        rows = [{"matched": 0, "windows": 0} for _ in blocks]
        for fold in range(3):
            holdouts = [indices[fold] for indices in partitions]
            train = [np.concatenate((block[:max(0, indices[0] - 1)], block[indices[-1] + 2:]))
                     for block, indices in zip(blocks, holdouts)]
            model = fit(background, train, positions, config, lines, anchor_only=True)
            for row, expected, block, indices in zip(rows, positions, blocks, holdouts):
                row["matched"] += sum(estimate(model, sample) == expected for sample in block[indices])
                row["windows"] += len(indices)
        return rows

    @staticmethod
    def check_empty(model, windows):
        return None if not windows else {
            "false_positions": sum(estimate(model, sample) is not None for sample in windows),
            "windows": len(windows),
        }
