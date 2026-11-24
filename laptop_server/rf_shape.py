"""Experimental reconstruction of a manually labelled, fixed vertical plane from CSI."""

from hashlib import sha256
import json
import math
from uuid import uuid4

import numpy as np

import diagnostics


COLS, ROWS = 8, 10
DELAY, DURATION = 10, 20
WINDOWS = math.ceil(DURATION * .8)
FEATURES_PER_LINK = 106


def mask_key(mask):
    return mask.astype(np.uint8).tobytes()


def iou(estimate, reference):
    return float(np.count_nonzero(estimate & reference) / np.count_nonzero(estimate | reference))


class ShapeModel:
    def __init__(self, captures):
        features = np.stack([np.median(row["features"], axis=0) for row in captures])
        masks = np.stack([row["mask"] for row in captures])
        self.center = np.mean(features, axis=0)
        self.scale = np.maximum(np.std(features, axis=0), .005)
        self.examples = (features - self.center) / self.scale
        distances = self.distances(self.examples)
        kernel = np.exp(-distances**2 / 2)
        self.weights = np.linalg.solve(kernel + .12 * np.eye(len(kernel)), masks)
        np.fill_diagonal(distances, np.inf)
        self.radius = max(.25, 1.5 * float(np.quantile(distances.min(axis=1), .9)))

    def distances(self, features):
        return np.sqrt(np.mean((features[:, None, :] - self.examples[None, :, :])**2, axis=-1))

    def predict(self, features):
        distances = self.distances((features - self.center) / self.scale)
        probabilities = np.exp(-distances**2 / 2) @ self.weights
        return probabilities >= .5, distances.min(axis=1) <= self.radius


def validate(model, training, checks):
    references = [row["mask"] for row in training if row["mask"].any()]
    occupied, empty = [], []
    for row in checks:
        masks, known = model.predict(row["features"])
        if row["mask"].any():
            score = float(np.mean([iou(mask, row["mask"]) if valid else 0
                                   for mask, valid in zip(masks, known)]))
            closest = max(iou(mask, row["mask"]) for mask in references)
            occupied.append((score, closest))
        else:
            empty.append(float(np.mean(known & ~masks.any(axis=1))))
    mean_iou, baseline = np.mean(occupied, axis=0).tolist()
    empty_accuracy = min(empty)
    passed = (min(score for score, _ in occupied) >= .30 and mean_iou >= .45
              and mean_iou >= baseline + .05 and empty_accuracy >= .80)
    return {"passed": passed, "iou": mean_iou, "baseline": baseline, "empty_accuracy": empty_accuracy,
            "occupied_checks": len(occupied), "empty_checks": len(empty)}


class RFShape:
    def __init__(self, localization):
        self.localization = localization
        self.directory = None
        self.captures = []
        self.recording = None
        self.report = None
        self.model = None
        self.lines = []
        self.next_tick = 0
        self.live_features = []
        self.estimate = None
        self.message = ""

    def scope(self):
        layout = json.dumps([self.localization.room, self.localization.devices], sort_keys=True)
        return self.localization.directory / "rf_shape" / sha256(layout.encode()).hexdigest()[:16]

    def sync(self):
        directory = self.scope()
        if directory == self.directory:
            return
        self.directory = directory
        self.recording = self.model = self.report = self.estimate = None
        self.live_features = []
        self.lines = []
        self.captures = []
        self.message = ""
        for path in sorted(directory.glob("*.npz")):
            try:
                with np.load(path, allow_pickle=False) as archive:
                    lines = archive["lines"].tolist()
                    role = archive["role"].item()
                    mask = archive["mask"]
                    features = archive["features"]
                if (role not in ("train", "test") or not lines or (self.lines and lines != self.lines)
                        or mask.shape != (ROWS * COLS,) or mask.dtype != np.bool_
                        or features.ndim != 2 or features.shape[0] < WINDOWS
                        or features.shape[1] != len(lines) * FEATURES_PER_LINK or not np.isfinite(features).all()):
                    raise ValueError("Invalid capture")
                self.lines = lines
                self.captures.append({"id": path.stem, "role": role, "mask": mask, "features": features})
            except (OSError, ValueError, KeyError, TypeError) as error:
                diagnostics.failure("rf_shape.load_failed", error)
                self.message = "Одна из сохранённых записей формы повреждена"

    def start(self, cells, role, owner, now):
        if self.recording:
            raise ValueError("Дождитесь окончания записи")
        if not self.localization.room:
            raise ValueError("Сначала сохраните план комнаты")
        if (role not in ("train", "test") or not isinstance(cells, list) or len(cells) > ROWS * COLS
                or not all(type(cell) is int and 0 <= cell < ROWS * COLS for cell in cells)):
            raise ValueError("Проверьте разметку и назначение записи")
        lines = self.lines or self.localization.active_lines(now)
        if len({line.split(" <- ")[0] for line in lines}) < 2 or self.localization.feature(now, lines) is None:
            raise ValueError("Для записи нужны пригодные кадры от обеих ESP")
        mask = np.zeros(ROWS * COLS, dtype=bool)
        mask[cells] = True
        self.recording = {"id": uuid4().hex, "role": role, "mask": mask, "features": [], "lines": lines,
                          "owner": owner, "begin": now + DELAY, "end": now + DELAY + DURATION}
        self.message = "Займите отмеченное положение или освободите комнату"
        diagnostics.event("rf_shape.recording_started", role=role, occupied_cells=int(mask.sum()))

    def cancel(self, owner):
        if self.recording and self.recording["owner"] is owner:
            self.recording = None
            self.message = "Текущая запись формы отменена"

    def delete_capture(self, capture_id):
        if self.recording:
            raise ValueError("Дождитесь окончания записи")
        row = next((row for row in self.captures if row["id"] == capture_id), None)
        if row is None:
            raise ValueError("Запись формы не найдена")
        (self.directory / (row["id"] + ".npz")).unlink()
        self.captures.remove(row)
        if not self.captures:
            self.lines = []
        self.report = self.model = self.estimate = None
        self.live_features.clear()
        self.message = "Запись формы удалена"

    def finish(self, record):
        self.recording = None
        features = np.array(record["features"])
        if len(features) < WINDOWS:
            self.message = f"Запись пропущена. Пригодных окон {len(features)} из необходимых {WINDOWS}"
            diagnostics.event("rf_shape.recording_rejected", windows=len(features))
            return
        row = {"id": record["id"], "role": record["role"], "mask": record["mask"], "features": features}
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / (row["id"] + ".npz")
            temporary = path.with_suffix(".tmp")
            try:
                with temporary.open("wb") as file:
                    np.savez_compressed(file, lines=record["lines"], role=row["role"], mask=row["mask"], features=features)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as error:
            self.message = "Не удалось сохранить запись формы"
            diagnostics.failure("rf_shape.save_failed", error)
            return
        self.lines = record["lines"]
        self.captures.append(row)
        self.report = self.model = self.estimate = None
        self.live_features.clear()
        self.message = f"Сохранено пригодных окон: {len(features)}"
        diagnostics.event("rf_shape.recording_saved", role=row["role"], windows=len(features))

    def import_background(self):
        if self.recording or self.localization.recording:
            raise ValueError("Дождитесь окончания записи")
        lines = self.localization.lines
        if len({line.split(" <- ")[0] for line in lines}) < 2 or (self.lines and self.lines != lines):
            raise ValueError("Радиолинии записей фона и формы должны совпадать")
        existing = {row["id"] for row in self.captures}
        added = 0
        for session in self.localization.sessions:
            if (session["role"] != "train" or session["position"] is not None or session["id"] in existing
                    or len(session["X"]) < WINDOWS):
                continue
            self.finish({"id": session["id"], "role": "train", "mask": np.zeros(ROWS * COLS, dtype=bool),
                         "features": session["X"], "lines": lines})
            if self.captures and self.captures[-1]["id"] == session["id"]:
                added += 1
        if not added:
            raise ValueError("Нет новых подходящих учебных записей пустой комнаты")
        self.message = f"Добавлено записей фона из калибровки клеток: {added}"

    def train(self):
        if self.recording:
            raise ValueError("Дождитесь окончания записи")
        training = [row for row in self.captures if row["role"] == "train"]
        checks = [row for row in self.captures if row["role"] == "test"]
        train_masks = {mask_key(row["mask"]) for row in training if row["mask"].any()}
        check_masks = {mask_key(row["mask"]) for row in checks if row["mask"].any()}
        if len(train_masks) < 3 or sum(not row["mask"].any() for row in training) < 2:
            raise ValueError("Добавьте для обучения три разные формы и две записи пустой комнаты")
        if len(check_masks - train_masks) < 2 or not any(not row["mask"].any() for row in checks):
            raise ValueError("Для проверки нужны две новые формы и отдельная запись пустой комнаты")
        independent = [row for row in checks if mask_key(row["mask"]) not in train_masks]
        model = ShapeModel(training)
        self.report = validate(model, training, independent)
        self.model = model if self.report["passed"] else None
        self.estimate = None
        self.live_features.clear()
        self.message = "Форма прошла проверку" if self.model else "Форма скрыта. Модель не прошла проверку на новых записях"
        diagnostics.event("rf_shape.validation", **self.report)

    def tick(self, now):
        if not (self.recording or self.model) or now < self.next_tick or self.directory != self.scope():
            return
        self.next_tick = now + 1
        record = self.recording
        if record:
            if now >= record["end"]:
                self.finish(record)
            elif now >= record["begin"] + 1:
                window = self.localization.feature(now, record["lines"])
                if window is not None:
                    record["features"].append(window)
            return
        window = self.localization.feature(now, self.lines)
        self.estimate = None
        if window is None:
            self.live_features.clear()
            return
        self.live_features = (self.live_features + [window])[-3:]
        mask, known = self.model.predict(np.median(self.live_features, axis=0)[None, :])
        if len(self.live_features) == 3 and known[0]:
            self.estimate = np.flatnonzero(mask[0]).tolist()

    def status(self, now):
        record = self.recording
        progress = None
        if record:
            waiting = now < record["begin"]
            progress = {"phase": "waiting" if waiting else "recording", "left_s":
                        math.ceil((record["begin"] if waiting else record["end"]) - now)}
        state = "recording" if progress else "untrained"
        if self.report and not self.model:
            state = "rejected"
        if self.model and not progress:
            state = "unknown" if self.estimate is None else "estimated" if self.estimate else "empty"
        return {"type": "rf_shape", "cols": COLS, "rows": ROWS, "state": state, "recording": progress,
                "room_ready": bool(self.localization.room), "dataset": self.localization.dataset["name"],
                "scope": f"{self.localization.dataset['id']}/{self.directory.name}",
                "report": self.report, "estimate": self.estimate if state == "estimated" else None,
                "message": self.message, "captures": [
                    {"id": row["id"], "role": row["role"], "cells": np.flatnonzero(row["mask"]).tolist(),
                     "windows": len(row["features"])} for row in self.captures]}
