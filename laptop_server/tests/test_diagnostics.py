import asyncio
import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from websockets.datastructures import Headers

import diagnostics
from diagnostics_socket import handle_socket, local_connection
from signal_quality import SignalQuality


def records(journal):
    return [json.loads(line) for line in journal.snapshot().splitlines()]


class DiagnosticSocket:
    def __init__(self, messages, peer="127.0.0.1", origin="http://127.0.0.1:8000"):
        self.messages = iter(map(json.dumps, messages))
        self.remote_address = (peer, 43210)
        self.request = SimpleNamespace(headers=Headers([("Origin", origin)]))
        self.sent = []
        self.closed = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.messages)
        except StopIteration:
            raise StopAsyncIteration from None

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def close(self, *, code, reason):
        self.closed = code


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.journal = diagnostics.RunJournal("test", {"udp": [5555]}, self.directory, max_bytes=4096)
        self.enterContext(patch.object(diagnostics, "current", self.journal))

    def test_rotation_and_snapshot_keep_startup_and_complete_utf8_events(self):
        self.journal.event("server.initial_context", pin=True, dataset="original")
        for index in range(85):
            self.journal.event("test.event", index=index, message="Проверка")
        self.assertLessEqual(self.journal.path.stat().st_size, 4096)
        before = records(self.journal)
        self.assertEqual([row["event"] for row in before[:2]], ["server.starting", "server.initial_context"])
        self.assertTrue(any(row["event"] == "server.log_compacted" for row in before))
        self.assertEqual(before[-1]["index"], 84)
        diagnostics.finish("normal")
        self.assertEqual(records(self.journal)[-1]["event"], "server.stopped")
        self.assertEqual({row["run_id"] for row in before}, {self.journal.id})

    def test_storage_error_degrades_and_pruning_targets_journals_only(self):
        unrelated = self.directory / "unrelated.jsonl"
        unrelated.touch()
        old = self.directory / "ruview-20000101-000000-00000000.jsonl"
        old.touch()
        os.utime(old, (1, 1))
        other = diagnostics.RunJournal("test", {}, self.directory)
        self.assertFalse(old.exists())
        self.assertTrue(unrelated.exists())
        self.assertNotEqual(self.journal.id, other.id)
        with patch.object(Path, "open", side_effect=PermissionError(errno.EACCES, "denied")):
            other.event("csi.summary")
        self.assertIsNone(other.snapshot())
        blocked = diagnostics.RunJournal("test", {}, unrelated)
        self.assertIsNone(blocked.snapshot())

    def test_two_receivers_line_loss_and_return_use_stable_aliases(self):
        quality = SignalQuality()
        frame = SimpleNamespace(seq=None, rssi=-50, noise=-90, src_id=1234, channel=6)
        addresses = ["192.168.1.2", "192.168.1.3"]
        keys = [f"{address} <- 04D2 / ch6" for address in addresses]
        diagnostics.observe_signal(quality.assess([], 0), [], 0)
        for at, receivers in ((1, addresses), (2, addresses[:1]), (3, addresses)):
            for address in receivers:
                quality.add(address, frame, at)
                diagnostics.first_frame(address, frame, 5555, b"\x01\x02")
            active = [key for key in keys if key.split(" <- ")[0] in receivers]
            diagnostics.observe_signal(quality.assess(keys, at), active, at)
        diagnostics.observe_signal(quality.assess(keys, 5), [], 5)
        diagnostics.failure("server.bind_failed", OSError(errno.EADDRINUSE, "private path"), port=8765)
        result = records(self.journal)
        first = [row for row in result if row["event"] == "csi.first_frame"]
        self.assertEqual(len(first), 2)
        alias = first[1]["receiver"]
        events = [row["event"] for row in result if row.get("receiver") == alias]
        self.assertEqual(events, ["csi.first_frame", "csi.line_available", "csi.line_lost",
                                  "csi.line_available", "csi.line_lost"])
        self.assertEqual(sum(row["event"] == "csi.summary" for row in result), 2)
        self.assertEqual(result[-1]["reason"], "port_in_use")
        self.assertNotIn("192.168.", self.journal.snapshot())
        self.assertNotIn("private path", self.journal.snapshot())

    def test_snapshot_is_current_and_network_origin_and_payload_are_filtered(self):
        self.assertTrue(local_connection(DiagnosticSocket([], peer="::1", origin="http://[::1]:8000")))
        for peer, origin in (("192.168.1.2", "http://127.0.0.1"), ("127.0.0.1", "https://example.org")):
            blocked = DiagnosticSocket([{"cmd": "diagnostics_snapshot"}], peer, origin)
            asyncio.run(handle_socket(blocked))
            self.assertEqual(blocked.closed, 1008)
            self.assertFalse(blocked.sent)
        socket = DiagnosticSocket([
            {"cmd": "diagnostics_event", "event": "cell", "view": "map", "state": "estimated", "cell": "A1"},
            {"cmd": "diagnostics_event", "event": "silhouette", "view": "esp2",
             "reason": "other_receiver", "receiver": "192.168.1.2", "iq": ["private"]},
            {"cmd": "diagnostics_snapshot", "path": "arbitrary"},
            {"cmd": "diagnostics_snapshot"},
        ])
        asyncio.run(handle_socket(socket))
        self.assertEqual(len(socket.sent), 1)
        response = socket.sent[0]
        self.assertEqual(response["name"], self.journal.path.name)
        self.assertEqual(response["content"], self.journal.snapshot())
        cell, silhouette = records(self.journal)[-2:]
        self.assertEqual((cell["source"], cell["cell"]), ("browser", "A1"))
        self.assertEqual((silhouette["reason"], silhouette["receiver"]), ("other_receiver", "RX1"))
        self.assertNotIn("private", response["content"])
