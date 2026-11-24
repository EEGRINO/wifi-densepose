import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

import diagnostics
from csi_ranging import fit, layout
from localization import Localization
from ranging_web import EMPTY_CHECK, REFERENCE, RangingWeb


class RangingWebTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.localization = Localization(folder)
        self.localization.configure(7.5, 7, 4, 4)
        for device, x, z in (("router", 3.74, 6.35), ("esp1", 6.93, 6.41), ("esp2", .24, 2.43)):
            self.localization.set_device(device, x, z)
        self.web = RangingWeb(self.localization)
        self.now = 100.0
        self.lines = ["192.168.31.186 <- 7584 / ch6", "192.168.31.61 <- 7584 / ch6"]
        self.feature = None
        self.enterContext(patch.object(self.localization, "ranging_snapshot", side_effect=self.snapshot))

    def snapshot(self, now):
        return {"dataset_id": self.localization.dataset["id"], "devices": self.localization.devices,
                "room": self.localization.room, "lines": self.lines if self.feature is not None else [],
                "features": self.feature}

    def command(self, action, **values):
        return self.web.handle({"cmd": "ranging_web_" + action, "dataset_id": "original", **values}, self.now)

    def add(self, x, z, role="train"):
        return self.command("add", role=role, x=x, z=z)["points"][-1]["id"]

    def capture(self, target, feature):
        self.feature = feature.tolist()
        result = self.command("record", id=target, delay=2, seconds=5)
        self.assertEqual(result["recording"]["phase"], "waiting")
        for offset in range(8):
            self.web.tick(self.now + offset)
        self.now += 8
        self.assertEqual(self.command("status")["windows"][target], 5)

    def test_edit_and_reload_without_packets(self):
        target = self.add(2, 3, "test")
        with self.assertRaisesRegex(ValueError, "двух ESP"):
            self.command("record", id=target, delay=0, seconds=5)
        with self.assertRaisesRegex(ValueError, "внутри"):
            self.add(10, 10)
        self.web = RangingWeb(self.localization)
        self.assertEqual(self.command("status")["points"][0]["id"], target)
        self.assertEqual(self.command("delete", id=target)["points"], [])
        self.web = RangingWeb(self.localization)
        self.assertEqual(self.command("status")["points"], [])

    def test_live_ranging_uses_trained_lines_even_when_an_extra_line_appears(self):
        base = np.tile(np.r_[np.ones(52), -.8, 2.5, np.full(52, .02)], 2)
        point = base.copy()
        point[52] += .25
        self.feature = point.tolist()
        self.command("status")
        trained_lines = self.lines.copy()
        model = fit(np.tile(base, (5, 1)), [np.tile(point, (5, 1))], [[1, 1]],
                    layout(self.snapshot(self.now)), trained_lines, anchor_only=True)
        self.web.save(model=model)

        self.lines.append("192.168.31.186 <- 46D8 / ch6")
        with patch.object(self.localization, "feature", return_value=point) as feature:
            status = self.command("status")
            self.assertEqual(status["live_state"], "estimated")
            self.assertEqual(status["estimate"]["position"], [1, 1])
            feature.assert_called_once_with(self.now, trained_lines)

        self.lines.pop(0)
        with patch.object(self.localization, "feature") as feature:
            status = self.command("status")
            self.assertEqual(status["live_state"], "lines_changed")
            self.assertIsNone(status["estimate"])
            feature.assert_not_called()

        self.lines = trained_lines
        self.feature = base.tolist()
        status = self.command("status")
        self.assertEqual(status["live_state"], "unknown")
        self.assertEqual(status["live_reason"], "near_background")
        point[52] += .6
        self.feature = point.tolist()
        status = self.command("status")
        self.assertEqual(status["live_state"], "unknown")
        self.assertEqual(status["live_reason"], "outside_calibration")

    def test_journal_distinguishes_missing_model_background_and_outside_calibration(self):
        with tempfile.TemporaryDirectory() as folder:
            log = diagnostics.RunJournal("test", {}, Path(folder))
            with patch.object(diagnostics, "current", log):
                self.web.load()
                self.web.observe_position(self.now)
                base = np.tile(np.r_[np.ones(52), -.8, 2.5, np.full(52, .02)], 2)
                point = base.copy()
                point[52] += .25
                self.feature = base.tolist()
                model = fit(np.tile(base, (5, 1)), [np.tile(point, (5, 1))], [[1, 1]],
                            layout(self.snapshot(self.now)), self.lines, anchor_only=True)
                self.web.save(model=model)
                self.web.observe_position(self.now)
                point[52] += .6
                self.feature = point.tolist()
                self.web.observe_position(self.now)
                self.web.observe_position(self.now)
                point[52] -= .6
                self.feature = point.tolist()
                self.web.observe_position(self.now)
            changes = [json.loads(line) for line in log.snapshot().splitlines()[1:]]
            self.assertEqual([(row["live_state"], row["live_reason"]) for row in changes],
                             [("model_needed", None), ("unknown", "near_background"),
                              ("unknown", "outside_calibration"), ("estimated", "estimated")])
            self.assertEqual(changes[-1]["position"], [1, 1])

    def test_capture_then_train_and_verify_with_radios_disconnected(self):
        promoted = self.add(4, 4, "test")
        training = [self.add(1, 1), self.add(4, 1), promoted]
        checking = self.add(1.05, 1, "test")
        base = np.tile(np.r_[np.ones(52), -.8, 2.5, np.full(52, .02)], 2)
        centers = [base.copy() for _ in training]
        for index, center in enumerate(centers):
            center[52 + index] += .25
        self.capture(REFERENCE, base)
        for target, feature in zip(training, centers):
            self.capture(target, feature)
        self.capture(checking, centers[0])
        self.capture(EMPTY_CHECK, base)

        self.feature = None
        self.web = RangingWeb(self.localization)
        self.assertFalse(self.command("status")["can_train"])
        with self.assertRaises(ValueError):
            self.command("train")
        promotion = self.command("promote", id=promoted)
        self.assertEqual(promotion["windows"][promoted], 5)
        self.assertTrue(promotion["can_train"])
        trained = self.command("train")
        self.assertTrue(trained["model_ready"])
        self.assertEqual([row["matched"] for row in trained["recording_check"]["training"]], [5, 5, 5])
        self.assertEqual(trained["recording_check"]["held_out"], [{"matched": 5, "windows": 5}] * 3)
        self.assertEqual(trained["recording_check"]["empty"], {"false_positions": 0, "windows": 5})
        verified = self.command("validate", tolerance_mm=130)
        self.assertTrue(verified["report"]["passes_at_control_points"])
        self.assertIsNone(verified["estimate"])
        self.feature = centers[0].tolist()
        self.assertIsNotNone(self.command("status")["estimate"])
        self.capture(EMPTY_CHECK, centers[0])
        unsafe = self.command("status")
        self.assertTrue(unsafe["background_failed"])
        self.assertIsNone(unsafe["estimate"])

        self.command("delete", id=checking)
        status = self.command("status")
        self.assertTrue(status["model_ready"])
        self.assertIsNone(status["report"])
        self.assertNotIn(checking, status["windows"])
        self.command("delete", id=training[0])
        self.assertFalse(self.command("status")["model_ready"])
