import base64
import struct
import unittest

import csi_packet
from visor_csi import VisorCsi


def datagram(sequence, samples, *, source=0x1234, peer=False):
    return struct.pack("<4sIIbbBBHH", b"RUV2", sequence, sequence * 1000, -50, -93, 6,
                       8 if peer else 0, len(samples) // 2, source) + samples


class VisorCsiTests(unittest.TestCase):
    def test_exact_signed_bytes_without_subcarrier_mask_and_stale_expiry(self):
        # DC and guard bands contain deliberately nonzero raw values.
        samples = bytes(range(128, 256))
        wire = datagram(10, samples)
        frame = csi_packet.parse(wire)
        self.assertEqual(frame.iq[0], 0)
        feed = VisorCsi()
        first = feed.add("192.0.2.10", frame, wire, 7.0)
        feed.set_pose(first, [.3, .6] * 16)

        snapshot = feed.snapshot(7.25)["frames"][0]
        self.assertEqual(base64.b64decode(snapshot["iq"]), samples)
        self.assertEqual((snapshot["sequence"], snapshot["age_ms"], snapshot["source"]), (10, 250, "1234"))
        self.assertEqual(snapshot["pose"], [.3, .6] * 16)
        self.assertEqual(feed.snapshot(8.01)["frames"], [])

    def test_receiver_and_transmitter_samples_are_isolated_and_pose_does_not_carry_over(self):
        feed = VisorCsi()
        router = datagram(10, bytes([10, 20]) * 64)
        peer = datagram(20, bytes([21, 125]) * 64, source=0x5678, peer=True)
        key = feed.add("192.0.2.10", csi_packet.parse(router), router, 2.0)
        feed.set_pose(key, [1] * 32)
        feed.add("192.0.2.10", csi_packet.parse(peer), peer, 2.1)
        feed.add("192.0.2.11", csi_packet.parse(router), router, 2.15)
        latest = datagram(11, bytes([20, 30]) * 64)
        feed.add("192.0.2.10", csi_packet.parse(latest), latest, 2.2)

        frames = feed.snapshot(2.4)["frames"]
        self.assertEqual({(p["receiver"], p["sequence"], p["peer"]) for p in frames}, {
            ("192.0.2.10", 11, False), ("192.0.2.10", 20, True), ("192.0.2.11", 10, False),
        })
        self.assertTrue(all("pose" not in packet for packet in frames))
