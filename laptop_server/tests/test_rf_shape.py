from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np

from rf_shape import COLS, ROWS, RFShape, ShapeModel, validate


def example(cells, role="train"):
    mask = np.zeros(ROWS * COLS, dtype=bool)
    mask[cells] = True
    values = np.zeros(212)
    values[:len(mask)] = mask
    return {"mask": mask, "features": np.tile(values, (18, 1)), "role": role}


class RFShapeTests(unittest.TestCase):
    def test_validation_requires_more_than_a_training_template(self):
        training = [example(cells) for cells in ([], [], [0, 1], [3, 4], [6, 7])]
        checks = [example(cells, role="test") for cells in ([], [0, 1, 2], [3, 4, 5])]
        template = ShapeModel(training)
        self.assertFalse(validate(template, training, checks)["passed"])
        reconstructed = SimpleNamespace(predict=lambda features: (
            features[:, :ROWS * COLS] > .5, np.ones(len(features), dtype=bool)))
        result = validate(reconstructed, training, checks)
        self.assertTrue(result["passed"])
        self.assertEqual(result["iou"], 1.0)

    def test_captures_only_the_recording_interval_and_remains_hidden_until_validated(self):
        with TemporaryDirectory() as name:
            loc = SimpleNamespace(room={"width": 3, "depth": 6}, devices={}, directory=Path(name),
                                  dataset={"name": "Test", "id": "test"})
            loc.active_lines = Mock(return_value=["a <- AP", "b <- AP"])
            loc.feature = Mock(return_value=np.zeros(212))
            shape = RFShape(loc)
            shape.sync()
            owner = object()
            shape.start([1, 9], "train", owner, 0)
            self.assertEqual(shape.status(0)["recording"]["left_s"], 10)
            for now in range(1, 31):
                shape.tick(now)
            self.assertEqual(shape.status(30)["state"], "untrained")
            self.assertIsNone(shape.status(30)["estimate"])
            self.assertEqual(shape.status(30)["captures"][0]["windows"], 19)
            reload = RFShape(loc)
            reload.sync()
            self.assertEqual(reload.status(30)["captures"], shape.status(30)["captures"])
            with self.assertRaises(ValueError):
                reload.train()
            reload.delete_capture(reload.captures[0]["id"])
            self.assertEqual(reload.captures, [])

    def test_import_uses_only_training_background_from_the_current_plan(self):
        with TemporaryDirectory() as name:
            features = np.zeros((18, 212))
            loc = SimpleNamespace(room={"width": 3, "depth": 6}, devices={}, directory=Path(name),
                                  dataset={"id": "test", "name": "Test"}, recording=None,
                                  lines=["a <- AP", "b <- AP"], sessions=[
                                      {"id": "background", "role": "train", "position": None, "X": features},
                                      {"id": "private_check", "role": "test", "position": None, "X": features},
                                      {"id": "occupied", "role": "train", "position": [1, 1], "X": features}])
            shape = RFShape(loc)
            shape.sync()
            shape.import_background()
            self.assertEqual([row["id"] for row in shape.captures], ["background"])
            self.assertFalse(shape.captures[0]["mask"].any())
            with self.assertRaises(ValueError):
                shape.import_background()
