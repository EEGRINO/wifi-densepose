"""Calibrated live preview for the two measured CSI locations."""

import collections
import json
import math

import numpy as np


AB_PLAN_FILE = "post_firmware_visit_B_followup_plan.json"


def _rms(value, axis):
    return np.sqrt(np.mean(value ** 2, axis=axis))


class ABPreview:
    def __init__(self, indices, labels, positions, prototypes, radii, now):
        self.indices = indices
        self.labels = labels
        self.positions = positions
        self.prototypes = prototypes
        self.radii = radii
        self.start = now + 30
        self.end = self.start + 20
        self.empty_samples = []
        self.baseline = None
        self.threshold = None
        self.failed = False
        self.last_valid = None
        self.votes = collections.deque(maxlen=5)
        self.recent = collections.deque(maxlen=4)
        self.started_at = now
        self.changes = collections.deque(maxlen=180)

    def record_state(self, now, estimate):
        state = estimate["state"]
        point = estimate.get("point")
        previous = self.changes[-1] if self.changes else None
        if previous is None or (previous["state"], previous["point"]) != (state, point):
            self.changes.append({"elapsed_s": round(now - self.started_at, 1),
                                 "state": state, "point": point})

    @classmethod
    def from_dataset(cls, directory, dataset_id, lines, sessions, positions, now):
        plan = json.loads((directory / AB_PLAN_FILE).read_text(encoding="utf-8"))
        indices = plan["presence_rule"]["peer_indices"]
        if (plan["dataset_id"] != dataset_id or len(indices) != 2 or len(set(indices)) != 2
                or any(not isinstance(i, int) or not 0 <= i < len(lines) for i in indices)):
            raise ValueError("Эталоны A/B не подходят к этому набору радиолиний")

        samples_by_id = {session["id"]: session for session in sessions}
        templates = {name: [] for name in positions}
        reference_blocks = {name: [] for name in positions}
        for ref in plan["position_rule"]["reference_sessions"]:
            name = ref["label"]
            session = samples_by_id.get(ref["id"])
            if (name not in positions or session is None or session["role"] != "visit"
                    or session["label"] != f"visit:{name}"
                    or session["visit"]["target"] != list(positions[name])):
                raise ValueError("В наборе отсутствует подходящий эталон захода A/B")
            path = directory / (session["id"] + ".npz")
            with np.load(path, allow_pickle=False) as data:
                quality = json.loads(str(data["quality_json"]))
            times = np.array([row["t"] for row in quality if not row["quality"]["blocked"]])
            shape = session["X"].reshape(-1, len(lines), 106)[:, indices, :52]
            if len(times) != len(shape):
                raise ValueError("Не удалось сопоставить окна CSI со временем эталона")
            empty = (times >= 2) & (times < 20)
            steady = (times >= 38) & (times < 54)
            if empty.sum() < 15 or steady.sum() < 12:
                raise ValueError("В эталоне A/B недостаточно данных")
            delta = shape - np.median(shape[empty], axis=0)
            templates[name].append(np.median(delta[steady], axis=0))
            for start in range(38, 54, 4):
                block = delta[(times >= start) & (times < start + 4)]
                if len(block) < 3:
                    raise ValueError("В эталоне A/B недостаточно данных")
                reference_blocks[name].append(np.median(block, axis=0))

        if not all(templates.values()):
            raise ValueError("Нужны эталоны и для A, и для B")
        labels = sorted(templates)
        prototypes = np.stack([np.median(templates[name], axis=0) for name in labels])
        radii = np.array([1.25 * np.max(_rms(np.stack(reference_blocks[name]) - proto, axis=(1, 2)))
                          for name, proto in zip(labels, prototypes)])
        return cls(indices, labels, positions, prototypes, radii, now)

    def status(self, now):
        if self.failed:
            return {"phase": "recalibrate", "left_s": 0}
        if now < self.start:
            return {"phase": "waiting", "left_s": math.ceil(self.start - now)}
        if self.baseline is None:
            return {"phase": "calibrating", "left_s": max(0, math.ceil(self.end - now)),
                    "windows": len(self.empty_samples)}
        return {"phase": "active", "left_s": 0}

    def update(self, now, feature):
        result = {"state": "ab_calibrating", "position": None, "sector": None, "mode": "ab"}
        if self.failed:
            result["state"] = "ab_recalibrate"
            return result
        sample = None if feature is None else feature.reshape(-1, 106)[self.indices, :52]
        if self.baseline is None:
            if now < self.end:
                if sample is not None and now >= self.start + 2:
                    self.empty_samples.append(sample.copy())
                return result
            if len(self.empty_samples) < 15:
                self.failed = True
                result["state"] = "ab_recalibrate"
                return result
            empty = np.stack(self.empty_samples)
            self.baseline = np.median(empty, axis=0)
            self.threshold = 1.25 * np.percentile(_rms(empty - self.baseline, axis=2), 95, axis=0)
            self.empty_samples.clear()
            self.last_valid = now

        if now - self.last_valid >= 3.0:
            self.failed = True
            result["state"] = "ab_recalibrate"
            return result
        if sample is None:
            self.votes.clear()
            self.recent.clear()
            result["state"] = "missing_lines"
            return result

        self.last_valid = now
        delta = sample - self.baseline
        self.votes.append(bool(np.all(_rms(delta, axis=1) > self.threshold)))
        self.recent.append(delta)
        if len(self.votes) < 3:
            result["state"] = "unknown"
            return result
        if sum(self.votes) < 3:
            result["state"] = "empty"
            return result
        result["state"] = "ab_present_unknown"
        if len(self.recent) < 4:
            return result
        distances = _rms(np.median(self.recent, axis=0) - self.prototypes, axis=(1, 2))
        best, other = np.argsort(distances)
        name = self.labels[best]
        if distances[best] <= self.radii[best] and distances[best] < 0.8 * distances[other]:
            result.update(state="estimated", position=self.positions[name], point=name)
        return result
