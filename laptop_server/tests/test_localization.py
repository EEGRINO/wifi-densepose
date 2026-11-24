"""Meaningful synthetic checks; they do not establish real-world CSI accuracy."""
import json
import struct
from unittest.mock import patch
from csi_record import read_recording, REC_PACKET, REC_LABEL, REC_META
from csi_packet import parse as parse_wire
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import numpy as np

from localization import Localization, inside_room, sector_for, fit_fingerprints, fit_ridge, fit_svm, fit_rssi_ridge, rssi_features, fit_csi_change, csi_change_features, predict, evaluate, group_training_sessions


class LocalizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.loc = Localization(self.tmp.name)
        self.loc.configure(6.46, 2.87, 3, 2, "curved", 4.04, 16.8)

    def frame(self, source=1, channel=6):
        return SimpleNamespace(version=2, src_id=source, channel=channel,
                               amp=np.linspace(2, 20, 64), rssi=-45)

    def feed(self, now):
        for receiver in ("192.168.31.186", "192.168.31.61"):
            for i in range(20):
                self.loc.add(receiver, self.frame(), now - .95 + i * .05)

    def test_guided_calibration_records_background_and_two_passes_in_separate_dataset(self):
        self.training_sessions()
        self.feed(1)
        owner = object()
        self.loc.start_guided([[1, 1], [4, 1]], "original", owner, 1)
        self.assertNotEqual(self.loc.dataset["id"], "original")
        expected = [None, None, [1, 1], [4, 1], [1, 1], [4, 1]]
        for index, point in enumerate(expected):
            start = 1 + index * 30
            step = self.loc.status(start)
            self.assertEqual(step["guided_calibration"]["completed"], index)
            self.assertEqual(step["recording"]["position"], point)
            self.assertEqual(step["recording"]["left_s"], 10)
            for now in range(start + 1, start + 31):
                self.feed(now)
                self.loc.tick(now)
        done = self.loc.status(181)
        self.assertIsNone(done["recording"])
        self.assertEqual(done["guided_calibration"]["state"], "done")
        self.assertTrue(done["can_train"])
        self.assertEqual([session["position"] for session in done["sessions"]], expected)
        self.loc.open_dataset("original")
        self.assertEqual(len(self.loc.sessions), 6)

    def test_guided_calibration_stops_after_failed_window_or_owner_disconnect(self):
        owner = object()
        self.feed(1)
        self.loc.start_guided([[1, 1], [4, 1]], "original", owner, 1)
        self.loc.tick(31)
        self.assertEqual(self.loc.status(31)["guided_calibration"]["state"], "failed")
        self.assertIsNone(self.loc.recording)
        self.feed(32)
        self.loc.start_guided([[1, 1], [4, 1]], "original", owner, 32)
        self.loc.stop_guided_on_disconnect(object())
        self.assertIsNotNone(self.loc.recording)
        self.loc.stop_guided_on_disconnect(owner)
        self.assertEqual(self.loc.status(32)["guided_calibration"]["state"], "cancelled")
        self.assertIsNone(self.loc.recording)
        self.assertEqual(self.loc.sessions, [])

    def training_sessions(self):
        self.loc.lines = ["one", "two"]
        rng = np.random.default_rng(4)
        result = []
        for number, (label, point) in enumerate([
                ("empty", None), ("point:1.000,1.000", [1, 1]),
                ("point:4.000,1.000", [4, 1])]):
            for repeat in range(2):
                # Independent class directions: a multiclass linear model cannot
                # separate three collinear symmetric centers with this least-squares target.
                center = np.zeros(212)
                center[number * 70:(number + 1) * 70] = 3.0
                X = center + rng.normal(0, .02, (20, 212))
                item = {"id": f"s{number}{repeat}", "role": "train", "label": label,
                        "position": point, "windows": len(X), "skipped": 0, "created": 0}
                np.savez_compressed(Path(self.tmp.name) / (item["id"] + ".npz"), X=X)
                result.append({**item, "X": X})
        self.loc.sessions = result
        self.loc._save()
        return result

    def rssi_training_sessions(self):
        sessions = self.training_sessions()
        levels = {"empty": [-40, -40], "point:1.000,1.000": [-60, -40],
                  "point:4.000,1.000": [-40, -60]}
        for session in sessions:
            session["X"][:, 52::106] = np.asarray(levels[session["label"]]) / 60
        return sessions

    def test_rssi_control_ignores_csi_changes_and_keeps_raw_arrays(self):
        sessions = self.rssi_training_sessions()
        snapshots = [s["X"].copy() for s in sessions]
        model = fit_rssi_ridge(sessions)
        self.assertEqual(model["center"].shape, (2,))
        for session, original in zip(sessions, snapshots):
            sample = session["X"][0].copy()
            rssis = sample[52::106].copy()
            sample[:] = 1000
            sample[52::106] = rssis
            self.assertEqual(predict(model, sample)["label"], session["label"])
            np.testing.assert_array_equal(session["X"], original)
        with self.assertRaises(ValueError):
            rssi_features(np.ones(213))
        with self.assertRaises(ValueError):
            predict(model, np.ones(106))

    def test_rssi_control_does_not_train_on_test_or_diagnostic(self):
        sessions = self.rssi_training_sessions()
        expected = fit_rssi_ridge(sessions)
        for role in ("test", "diagnostic"):
            self.loc.sessions.append({"id": role, "role": role, "label": "empty",
                                      "position": None, "X": np.full((10, 212), 999.)})
        report = self.loc.train("ridge_rssi")
        np.testing.assert_array_equal(self.loc.model["center"], expected["center"])
        self.assertEqual(report["feature_mode"], "rssi")
        self.assertEqual(report["feature_count"], 2)
        self.assertEqual(report["training_recordings"], 6)
        self.assertEqual(report["test_recordings"], 1)
        self.assertEqual(report["diagnostic_recordings"], 1)

    def test_failed_independent_empty_recording_hides_live_coordinates(self):
        train = self.training_sessions()
        self.loc.sessions.append({"id": "failed-empty", "role": "test", "label": "empty",
                                  "position": None, "X": train[2]["X"].copy()})
        report = self.loc.train()
        self.assertTrue(report["passes_basic_check"])
        self.assertEqual(report["failed_test_recordings"], ["failed-empty"])
        self.assertEqual(report["test"]["false_present"], 20)
        self.feed(10)
        self.loc.lines = self.loc.active_lines(10)
        status = self.loc.tick(10)
        self.assertEqual(status["estimate"]["state"], "model_unreliable")
        self.assertIsNone(status["estimate"]["position"])

    def test_development_test_can_be_preserved_as_diagnostic(self):
        train = self.training_sessions()
        X = train[2]["X"].copy()
        item = {"id": "used-test", "role": "test", "label": "empty",
                "position": None, "windows": len(X), "skipped": 0, "created": 0}
        path = Path(self.tmp.name) / "used-test.npz"
        np.savez_compressed(path, X=X)
        raw_before = path.read_bytes()
        self.loc.sessions.append({**item, "X": X})
        self.loc.train()
        self.assertEqual(self.loc.report["failed_test_recordings"], ["used-test"])
        self.loc.mark_diagnostic("used-test")
        self.assertEqual(path.read_bytes(), raw_before)
        self.assertEqual(self.loc.report["test_recordings"], 0)
        self.assertIsNone(self.loc.report["test"])
        self.assertEqual(self.loc.report["diagnostic_recordings"], 1)
        self.assertEqual(self.loc.report["failed_test_recordings"], [])
        restored = Localization(self.tmp.name)
        self.assertEqual(len(restored.sessions), 7)
        self.assertEqual(restored.sessions[-1]["role"], "diagnostic")
        with self.assertRaises(ValueError):
            self.loc.mark_diagnostic(train[0]["id"])
        self.loc.recording = {"id": "active"}
        with self.assertRaisesRegex(ValueError, "окончания"):
            self.loc.mark_diagnostic("used-test")

    def csi_change_training_sessions(self):
        sessions = self.training_sessions()
        rng = np.random.default_rng(18)
        pattern = np.tile([1., -1.], 26)
        for i, session in enumerate(sessions):
            a = np.zeros((20, 2, 106))
            a[:, :, :52] = 1
            a[:, :, 52] = -.75
            if session["position"] is not None:
                link = 0 if session["position"][0] == 1 else 1
                a[:, link, :52] += (.3 if i % 2 == 0 else -.3) * pattern
            a[:, :, :52] += rng.normal(0, .002, (20, 2, 52))
            session["X"] = a.reshape(20, 212)
        return sessions

    def test_csi_change_recognizes_opposite_shape_changes_without_mutating_samples(self):
        sessions = self.csi_change_training_sessions()
        saved = [s["X"].copy() for s in sessions]
        # Each occupied class has opposite signed shape perturbations in its two recordings.
        for i, held in enumerate(sessions):
            model = fit_csi_change(sessions[:i] + sessions[i + 1:])
            for row in held["X"]:
                self.assertEqual(predict(model, row)["label"], held["label"])
        for session, before in zip(sessions, saved):
            np.testing.assert_array_equal(session["X"], before)

    def test_csi_change_reference_and_scaler_exclude_nontraining_records(self):
        train = self.csi_change_training_sessions()
        expected = fit_csi_change(train)
        for role in ("test", "diagnostic"):
            self.loc.sessions.append({"id": role, "role": role, "label": "empty",
                                      "position": None, "X": np.full((10, 212), 999.)})
        report = self.loc.train("ridge_csi_change")
        np.testing.assert_array_equal(self.loc.model["empty_reference"], expected["empty_reference"])
        np.testing.assert_array_equal(self.loc.model["center"], expected["center"])
        self.assertEqual(report["feature_mode"], "csi_change")
        self.assertEqual(report["feature_count"], 2)
        self.assertEqual(report["training_recordings"], 6)
        self.assertTrue(report["passes_basic_check"])

    def test_csi_change_rejects_missing_empty_or_incompatible_links(self):
        train = self.csi_change_training_sessions()
        with self.assertRaisesRegex(ValueError, "эталон"):
            fit_csi_change([s for s in train if s["position"] is not None])
        model = fit_csi_change(train)
        with self.assertRaises(ValueError):
            csi_change_features(np.ones(213), model["empty_reference"])
        with self.assertRaises(ValueError):
            csi_change_features(np.ones(106), model["empty_reference"])
        with self.assertRaises(ValueError):
            predict(model, np.ones(106))

    def test_curved_boundary_and_sector(self):
        room = self.loc.room
        self.assertTrue(inside_room(room, [4.04, 2.87]))
        self.assertFalse(inside_room(room, [6.4, 2.8]))
        self.assertFalse(inside_room(room, [float("nan"), 1]))
        self.assertEqual(sector_for(room, [1, 1]), "1:1")
        self.assertIsNone(sector_for(room, [6.4, 2.8]))
        self.assertAlmostEqual(room["declared_area"], 16.8)
        self.assertNotEqual(room["declared_area"], room["sketch_area"])

    def test_cannot_record_outside_boundary_or_without_two_receivers(self):
        with self.assertRaisesRegex(ValueError, "внутри"):
            self.loc.start(6.4, 2.8, "train", 5, 10, 10)
        with self.assertRaisesRegex(ValueError, "двух"):
            self.loc.start(None, None, "train", 5, 10, 10)

    def test_features_keep_lines_separate_and_reject_stale(self):
        self.feed(10)
        self.loc.lines = self.loc.active_lines(10)
        x = self.loc.feature(10)
        self.assertEqual(x.shape, (212,))
        self.assertTrue(np.isfinite(x).all())
        self.assertIsNone(self.loc.feature(12))
        self.loc.buffers.pop(self.loc.lines[0])
        self.assertIsNone(self.loc.feature(10))

    def record_wire_session(self, role="diagnostic"):
        self.feed(10)
        self.loc.start(None, None, role, 5, 10, 10)
        expected = []
        for now in range(11, 26):
            self.feed(now)
            if now in (14, 15, 16, 25):
                ip = "192.168.31.61" if now == 16 else "192.168.31.186"
                packet = struct.pack("<4sIIbbBBHH", b"RUV2", now, now * 1000000,
                                     -45, -96, 6, 0, 64, 1) + bytes([now % 5 + 1] * 128)
                self.loc.add(ip, parse_wire(packet), now, raw_packet=packet)
                if now in (15, 16):
                    expected.append((ip, packet))
            self.loc.tick(now)
        return expected

    def test_diagnostic_keeps_exact_packets_times_and_labels_in_existing_format(self):
        expected = self.record_wire_session()
        session = self.loc.sessions[-1]
        self.assertEqual(session["raw_packet_count"], 2)
        self.assertEqual(session["raw_dropped_count"], 0)
        entries = list(read_recording(Path(self.tmp.name) / session["raw_file"]))
        self.assertEqual(entries[0][0], REC_META)
        self.assertEqual(entries[0][2]["role"], "diagnostic")
        self.assertIsNone(entries[0][2]["position"])
        self.assertEqual(entries[1][0], REC_LABEL)
        self.assertEqual(entries[1][2], "empty")
        packets = [(t, payload) for kind, t, payload in entries if kind == REC_PACKET]
        self.assertEqual([payload for _, payload in packets], expected)
        self.assertAlmostEqual(packets[1][0] - packets[0][0], 1)
        self.assertEqual(len(self.loc.sessions[-1]["X"]), 10)
        restored = Localization(self.tmp.name)
        self.assertEqual(restored.sessions[-1]["raw_file"], session["raw_file"])

    def test_visit_saves_continuous_capture_and_planned_stages(self):
        self.feed(10)
        self.loc.start_visit("B", 10)
        self.assertEqual(self.loc.status(15)["recording"]["phase"], "waiting")
        for now in range(11, 131):
            self.feed(now)
            if now in (39, 40, 60, 75, 95, 110, 129, 130):
                packet = struct.pack("<4sIIbbBBHH", b"RUV2", now, now * 1000000,
                                     -45, -96, 6, 0, 64, 1) + bytes([1] * 128)
                self.loc.add("192.168.31.186", parse_wire(packet), now, raw_packet=packet)
            self.loc.tick(now)
            if now == 60:
                self.assertEqual(self.loc.status(now)["recording"]["visit_instruction"],
                                 "Войдите и займите точку B")
            if now == 75:
                self.assertEqual(self.loc.status(now)["recording"]["visit_step_left_s"], 20)
        restored = Localization(self.tmp.name)
        session = restored.sessions[-1]
        self.assertEqual(session["role"], "visit")
        self.assertIsNone(session["position"])
        entries = list(read_recording(Path(self.tmp.name) / session["raw_file"]))
        meta = entries[0][2]
        self.assertEqual(meta["visit"]["target"], [5.35, 0.72])
        self.assertEqual([s["start_s"] for s in meta["visit"]["planned_stages"]], [0, 20, 35, 55, 70])
        self.assertEqual(entries[1][2], "visit:B")
        packet_times = [round(t-meta["start_unix"]) for kind,t,_ in entries if kind == REC_PACKET]
        self.assertEqual(packet_times, [0, 20, 35, 55, 70, 89])

    def test_visit_A_uses_same_schedule_with_its_own_point_and_instructions(self):
        self.feed(10)
        self.loc.start_visit("A", 10)
        self.assertEqual(self.loc.recording["label"], "visit:A")
        self.assertEqual(self.loc.recording["visit"]["target"], [1.0, 0.92])
        self.assertEqual(self.loc.recording["start"], 40)
        self.assertEqual(self.loc.recording["end"], 130)
        self.assertEqual(self.loc.status(75)["recording"]["visit_instruction"], "Стойте в точке A")

    def test_training_keeps_original_behavior_without_raw_capture(self):
        self.record_wire_session("train")
        self.assertEqual(self.loc.sessions[-1]["raw_packet_count"], 0)
        self.assertIsNone(self.loc.sessions[-1]["raw_file"])
        self.assertEqual(list(Path(self.tmp.name).glob("*.ruvrec")), [])

    def test_raw_limit_is_reported_without_losing_feature_windows(self):
        with patch("localization.RAW_PACKET_LIMIT", 1):
            expected = self.record_wire_session()
        session = self.loc.sessions[-1]
        self.assertEqual(session["raw_packet_count"], 1)
        self.assertEqual(session["raw_dropped_count"], 1)
        packets = [payload for kind, _, payload in
                   read_recording(Path(self.tmp.name) / session["raw_file"]) if kind == REC_PACKET]
        self.assertEqual(packets, expected[:1])
        self.assertEqual(session["windows"], 10)

    def test_cancelling_diagnostic_leaves_no_raw_file(self):
        self.feed(10)
        self.loc.start(None, None, "diagnostic", 5, 10, 10)
        packet = struct.pack("<4sIIbbBBHH", b"RUV2", 1, 1, -45, -96, 6, 0, 64, 1) + bytes([1] * 128)
        self.loc.add("192.168.31.186", parse_wire(packet), 15, raw_packet=packet)
        self.loc.stop()
        self.assertEqual(list(Path(self.tmp.name).glob("*.ruvrec")), [])
        self.assertEqual(self.loc.sessions, [])

    def test_delayed_recording_survives_reload(self):
        self.feed(10)
        self.loc.start(1, 1, "train", 5, 10, 10)
        self.assertEqual(self.loc.status(10)["recording"]["phase"], "waiting")
        for now in range(11, 26):
            self.feed(now)
            self.loc.tick(now)
        self.assertIsNone(self.loc.recording)
        self.assertEqual(len(self.loc.sessions), 1)
        self.assertEqual(self.loc.sessions[0]["windows"], 10)
        restored = Localization(self.tmp.name)
        self.assertEqual(restored.sessions[0]["X"].shape, (10, 212))
        self.assertEqual(restored.sessions[0]["quality_version"], 1)
        with np.load(Path(self.tmp.name) / (restored.sessions[0]["id"] + ".npz")) as data:
            self.assertEqual(len(json.loads(str(data["quality_json"]))), 10)
        self.assertEqual(restored.room, self.loc.room)

    def test_cancel_does_not_create_a_session(self):
        self.feed(10)
        self.loc.start(None, None, "train", 5, 10, 10)
        self.loc.stop()
        self.assertEqual(self.loc.sessions, [])
        self.assertEqual(list(Path(self.tmp.name).glob("*.npz")), [])

    def test_rejects_mostly_missing_recording(self):
        self.feed(10)
        self.loc.start(None, None, "train", 5, 10, 10)
        self.loc.tick(25)
        self.assertEqual(self.loc.sessions, [])
        self.assertIn("не сохранена", self.loc.message)

    def test_train_requires_repeated_recordings_and_empty(self):
        with self.assertRaisesRegex(ValueError, "2 отдельные"):
            self.loc.train()

    def test_training_excludes_test_records_and_exposes_independent_report(self):
        train = self.training_sessions()
        original = fit_ridge(train)
        self.loc.sessions.append({"id": "test", "role": "test", "label": train[2]["label"],
                                  "position": [1, 1], "X": np.full((3, 212), 999.0)})
        report = self.loc.train()
        np.testing.assert_allclose(self.loc.model["center"], original["center"])
        self.assertGreater(report["recording_cv_accuracy"], .9)
        self.assertEqual(report["test"]["coverage"], 0)
        self.assertIsNone(report["test"]["median_error_m"])
        self.assertEqual(report["test_recordings"], 1)

    def test_missing_lines_never_leaves_an_old_point_on_screen(self):
        self.training_sessions()
        self.loc.train()
        self.loc.last = {"state": "estimated", "position": [1, 1]}
        status = self.loc.tick(100)
        self.assertEqual(status["estimate"]["state"], "missing_lines")
        self.assertIsNone(status["estimate"]["position"])

    def test_room_is_locked_after_data_collection(self):
        self.training_sessions()
        with self.assertRaisesRegex(ValueError, "связан"):
            self.loc.configure(7, 3, 3, 2)

    def test_custom_plan_uses_curves_and_excludes_cutouts(self):
        self.loc.configure(None, None, 3, 2, "custom",
                           outline=[[0, 0], [5, 0], [5, 3], [0, 3]],
                           curves=[None, [6, 1.5], None, None],
                           cutouts=[[1, 1, 2, 2]])
        room = self.loc.room
        self.assertAlmostEqual(room["width"], 5.5)
        self.assertAlmostEqual(room["sketch_area"], 15, places=2)
        self.assertTrue(inside_room(room, [5.4, 1.5]))
        self.assertIsNone(sector_for(room, [1.5, 1.5]))
        self.assertFalse(inside_room(room, [1, 1.5]))
        with self.assertRaisesRegex(ValueError, "внутри"):
            self.loc.set_device("router", 1.5, 1.5)
        self.assertEqual(Localization(self.tmp.name).room, room)

    def test_sectors_span_the_actual_contour_instead_of_the_global_origin(self):
        self.loc.configure(None, None, None, None, "custom", sector_mode="auto",
                           outline=[[1.7, 2], [7.1, 2], [7.1, 5], [1.7, 5]],
                           curves=[None] * 4, cutouts=[[4, 3, 4.2, 3.2]])
        room = self.loc.room
        self.assertEqual((room["cols"], room["rows"]), (3, 2))
        self.assertEqual(sector_for(room, [2.5, 3]), "1:1")
        self.assertEqual(sector_for(room, [3.9, 3]), "1:2")
        self.assertEqual(sector_for(room, [6, 3.1]), "1:3")
        self.assertEqual(sector_for(room, [6, 3.6]), "2:3")
        self.assertEqual(sector_for(room, [7.1, 5]), "2:3")
        self.assertIsNone(sector_for(room, [4.1, 3.1]))
        self.assertEqual(Localization(self.tmp.name).room["sector_mode"], "auto")

    def test_new_plan_starts_without_the_previous_geometry_or_equipment(self):
        self.loc.set_device("router", 1, 1)
        previous = self.loc.room
        self.loc.configure(None, None, 3, 2, "custom", new_plan=True,
                           outline=[[0, 0], [4, 0], [4, 3], [0, 3]],
                           curves=[None] * 4, cutouts=[], base_dataset_id="original")
        self.assertNotEqual(self.loc.dataset["id"], "original")
        self.assertEqual(self.loc.room["width"], 4)
        self.assertEqual(self.loc.devices, {})
        self.assertEqual(self.loc.sessions, [])
        self.assertEqual(Localization(self.tmp.name).room, self.loc.room)
        self.loc.open_dataset("original")
        self.assertEqual(self.loc.room, previous)
        self.assertIn("router", self.loc.devices)

    def test_invalid_custom_geometry_does_not_change_plan(self):
        old = self.loc.room
        for outline, cutouts, error in [
            ([[0, 0], [5, 3], [5, 0], [0, 3]], [], "пересекаются"),
            ([[0, 0], [5, 0], [5, 3], [0, 3]], [[1, 1, 2, 3]], "внутри"),
            ([[0, 0], [5, 0], [5, 3], [0, 3]], [[1, 1, 2, 2], [1.5, 1, 3, 2]], "вырезы"),
        ]:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.loc.configure(None, None, 3, 2, "custom", outline=outline,
                                   curves=[None] * 4, cutouts=cutouts, fork_if_recorded=True)
            self.assertEqual(self.loc.room, old)

    def test_editing_recorded_plan_opens_copy_and_retains_old_layout(self):
        self.loc.set_device("router", 1.5, 1.5)
        self.loc.set_device("esp1", 3, 1)
        sessions = self.training_sessions()
        self.loc.sessions = sessions
        for session in sessions:
            np.savez(self.loc.directory / (session["id"] + ".npz"), X=session["X"])
        self.loc._save()
        old = self.loc.room
        self.loc.configure(None, None, 3, 2, "custom",
                           outline=[[0, 0], [6, 0], [6, 3], [0, 3]],
                           curves=[None] * 4, cutouts=[[1, 1, 2, 2]],
                           fork_if_recorded=True, base_dataset_id="original")
        self.assertEqual(self.loc.room["shape"], "custom")
        self.assertEqual(self.loc.sessions, [])
        self.assertEqual(set(self.loc.devices), {"esp1"})
        self.assertNotEqual(self.loc.dataset["id"], "original")
        self.assertEqual(Localization(self.tmp.name).room, self.loc.room)
        self.loc.open_dataset("original")
        self.assertEqual(self.loc.room, old)
        self.assertEqual(len(self.loc.sessions), len(sessions))
        self.assertEqual(set(self.loc.devices), {"esp1", "router"})

    def test_sector_accuracy_is_not_anchor_identity_accuracy(self):
        train = self.training_sessions()
        model = fit_fingerprints(train)
        test = [{"label": "unseen-point", "position": [1.2, 1],
                 "X": train[2]["X"]}]
        report = evaluate(model, test, 10, self.loc.room)
        self.assertEqual(report["label_accuracy"], 0)
        self.assertEqual(report["sector_accuracy"], 1)
        self.assertEqual(report["recordings"][0]["presence_or_sector_accuracy"], 1)
        self.assertAlmostEqual(report["median_error_m"], .2)

    def test_nearby_repeated_clicks_train_without_changing_raw_sessions(self):
        self.training_sessions()
        self.loc.sessions[3]["position"] = [1.01, 1]
        self.loc.sessions[3]["label"] = "point:1.010,1.000"
        originals = [s["label"] for s in self.loc.sessions]
        report = self.loc.train()
        self.assertEqual([s["label"] for s in self.loc.sessions], originals)
        self.assertEqual(len(report["training_places"]), 3)
        self.assertTrue(all(p["recordings"] == 2 for p in report["training_places"]))

    def test_grouping_does_not_merge_adjacent_sectors_or_chained_points(self):
        def point(x):
            return {"label": str(x), "position": [x, 1]}
        grouped = group_training_sessions([point(2.15), point(2.16)], self.loc.room)
        self.assertNotEqual(grouped[0]["label"], grouped[1]["label"])
        grouped = group_training_sessions([point(1), point(1.08), point(1.16)], self.loc.room)
        self.assertEqual(grouped[0]["label"], grouped[1]["label"])
        self.assertNotEqual(grouped[0]["label"], grouped[2]["label"])

    def test_ridge_classifier_is_finite_and_classifies_separated_signals(self):
        train = self.training_sessions()
        model = fit_ridge(train)
        self.assertTrue(np.isfinite(model["beta"]).all())
        self.assertEqual(model["method"], "ridge")
        for session in train:
            p = predict(model, np.mean(session["X"], axis=0))
            self.assertEqual(p["label"], session["label"])
            self.assertGreater(p["score_margin"], .1)
        self.assertIsNone(predict(model, train[0]["X"][0])["position"])

    def test_svm_is_explicit_and_does_not_learn_test_data(self):
        train = self.training_sessions()
        model = fit_svm(train)
        for session in train:
            self.assertEqual(predict(model, session["X"][0])["label"], session["label"])
        self.loc.sessions.append({"id": "svm-test", "role": "test",
            "label": train[2]["label"], "position": [1, 1], "X": np.full((3, 212), 999.)})
        report = self.loc.train("svm")
        np.testing.assert_allclose(self.loc.model["center"], model["center"])
        self.assertEqual(report["algorithm"], "svm")
        self.assertEqual(report["test"]["coverage"], 0)
        with self.assertRaises(ValueError):
            self.loc.train("unknown")

    def test_low_snr_data_cannot_create_a_live_position(self):
        self.training_sessions()
        self.loc.train()
        self.loc.lines = []
        for receiver in ("192.168.31.186", "192.168.31.61"):
            for i in range(20):
                frame = self.frame()
                frame.noise = -47
                self.loc.add(receiver, frame, 9.05 + i * .05)
        self.loc.lines = self.loc.signal_quality.assess([], 10)["lines"].keys()
        self.loc.lines = list(self.loc.lines)
        self.assertIsNone(self.loc.feature(10))
        state = self.loc.tick(10)
        self.assertEqual(state["estimate"]["state"], "poor_signal")
        self.assertIsNone(state["estimate"]["position"])

    def test_new_experiment_keeps_original_files_and_can_reopen_them(self):
        self.loc.set_device("router", 1, .2)
        self.training_sessions()
        original_manifest=(Path(self.tmp.name)/"manifest.json").read_bytes()
        original_npz={p.name:p.read_bytes() for p in Path(self.tmp.name).glob("*.npz")}
        self.loc.new_dataset()
        new_id=self.loc.dataset["id"]
        self.assertNotEqual(new_id,"original")
        self.assertEqual(self.loc.sessions,[])
        self.assertIsNone(self.loc.model)
        self.assertEqual(self.loc.devices["router"]["position"],[1,.2])
        self.assertEqual((Path(self.tmp.name)/"manifest.json").read_bytes(),original_manifest)
        self.assertEqual({p.name:p.read_bytes() for p in Path(self.tmp.name).glob("*.npz")},original_npz)
        reopened=Localization(self.tmp.name)
        self.assertEqual(reopened.dataset["id"],new_id)
        self.assertEqual(reopened.room,self.loc.room)
        reopened.open_dataset("original")
        self.assertEqual(len(reopened.sessions),6)
        self.assertEqual(len(reopened.datasets()),2)
        self.assertEqual(Localization(self.tmp.name).dataset["id"],"original")

    def test_dataset_changes_are_forbidden_during_recording(self):
        self.feed(10)
        self.loc.start(1,1,"train",5,10,10)
        with self.assertRaisesRegex(ValueError,"окончания"):
            self.loc.new_dataset()
        with self.assertRaisesRegex(ValueError,"окончания"):
            self.loc.open_dataset("original")
        self.assertIsNotNone(self.loc.recording)

    def test_dataset_path_cannot_escape_workspace(self):
        for bad in ("../other","a"*31,"x"*32,"/tmp/other",None):
            with self.assertRaises(ValueError):
                self.loc.open_dataset(bad)
        self.assertEqual(self.loc.dataset["id"],"original")

    def test_model_cannot_display_a_point_when_control_is_no_better_than_baseline(self):
        self.training_sessions()
        for session in self.loc.sessions:
            session["X"] = np.ones_like(session["X"])
        report = self.loc.train("ridge")
        self.assertFalse(report["passes_basic_check"])
        self.feed(10)
        self.loc.lines = self.loc.active_lines(10)
        result = self.loc.tick(10)
        self.assertEqual(result["estimate"]["state"], "model_unreliable")
        self.assertIsNone(result["estimate"]["position"])

    def test_device_positions_survive_reload(self):
        self.loc.set_device("router", 1.0, 0.3, 1.4)
        self.loc.set_device("esp1", 0.2, 2.0)
        self.loc.set_device("esp2", 5.8, 0.5, 1.1)
        restored = Localization(self.tmp.name)
        self.assertEqual(restored.devices, self.loc.devices)
        self.assertEqual(restored.status(0)["devices"]["router"]["height"], 1.4)
        self.loc.set_device("esp2", None, None)
        self.assertNotIn("esp2", Localization(self.tmp.name).devices)

    def test_device_validation_rejects_bad_boundaries_and_height(self):
        for key, x, z, height in [
                ("unknown", 1, 1, None), ("router", 6.4, 2.8, None),
                ("esp1", 1, 1, float("nan")), ("esp2", 1, 1, -1),
                ("router", float("inf"), 1, None)]:
            with self.assertRaises(ValueError):
                self.loc.set_device(key, x, z, height)
        self.assertEqual(self.loc.devices, {})

    def test_equipment_is_locked_to_recorded_layout(self):
        self.loc.set_device("router", 1, .2, 1)
        self.training_sessions()
        self.loc.set_device("router", 1, .2, 1)
        with self.assertRaisesRegex(ValueError, "закреплено"):
            self.loc.set_device("router", 2, .2, 1)
        with self.assertRaisesRegex(ValueError, "закреплено"):
            self.loc.set_device("router", None, None)
        self.assertEqual(self.loc.devices["router"]["position"], [1, .2])

    def test_plan_cannot_shrink_past_a_device(self):
        self.loc.set_device("esp1", 5, 1)
        with self.assertRaisesRegex(ValueError, "вне нового"):
            self.loc.configure(2, 2, 2, 2)
        self.assertEqual(self.loc.room["width"], 6.46)

    def test_corrupt_data_load_fails_closed(self):
        path = Path(self.tmp.name) / "manifest.json"
        path.write_text("{not json", encoding="utf-8")
        restored = Localization(self.tmp.name)
        self.assertEqual(restored.sessions, [])
        self.assertIsNone(restored.model)


if __name__ == "__main__":
    unittest.main()
