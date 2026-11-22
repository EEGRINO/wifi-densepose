import unittest
from types import SimpleNamespace

from signal_quality import SignalQuality


class SignalQualityTests(unittest.TestCase):
    def setUp(self):
        self.q = SignalQuality()
        self.receiver = "192.168.31.61"
        self.key = self.receiver + " <- 75BC / ch6"

    def frame(self, seq, rssi=-45, noise=-95, channel=6):
        return SimpleNamespace(seq=seq, rssi=rssi, noise=noise, src_id=0x75BC, channel=channel)

    def feed(self, now=10, start=100, **kwargs):
        for i in range(20):
            self.q.add(self.receiver, self.frame(start+i, **kwargs), now-.95+i*.05)

    def test_clean_reception_has_snr_and_is_accepted(self):
        self.feed()
        state = self.q.assess([self.key], 10)
        self.assertFalse(state["blocked"])
        self.assertEqual(state["lines"][self.key]["snr_est_db"], 50)
        self.assertEqual(state["receivers"][self.receiver]["loss_ratio"], 0)

    def test_duplicate_and_out_of_order_packets_are_not_accepted(self):
        self.feed()
        self.assertFalse(self.q.add(self.receiver, self.frame(119), 10))
        self.assertFalse(self.q.add(self.receiver, self.frame(118), 10))
        row = self.q.assess([self.key], 10)["receivers"][self.receiver]
        self.assertEqual(row["duplicates"], 1)
        self.assertEqual(row["reordered"], 1)

    def test_sequence_loss_blocks_window(self):
        self.q.add(self.receiver, self.frame(1), 9)
        self.feed(start=100)
        state = self.q.assess([self.key], 10)
        self.assertTrue(state["blocked"])
        self.assertIn("packet_loss", state["lines"][self.key]["reasons"])

    def test_uint32_rollover_is_not_a_reboot(self):
        self.q.add(self.receiver, self.frame(0xFFFFFFFF), 9)
        self.feed(start=0)
        state = self.q.assess([self.key], 10)
        self.assertEqual(state["receivers"][self.receiver]["resets_seen"], 0)
        self.assertFalse(state["blocked"])

    def test_counter_reset_waits_for_stabilization(self):
        self.q.add(self.receiver, self.frame(100), 9)
        self.feed(start=0)
        state = self.q.assess([self.key], 10)
        self.assertTrue(state["blocked"])
        self.assertIn("receiver_restarted", state["lines"][self.key]["reasons"])
        self.feed(now=14, start=20)
        self.assertFalse(self.q.assess([self.key], 14)["blocked"])

    def test_unsupported_noise_does_not_create_a_fake_snr(self):
        self.feed(noise=0)
        state = self.q.assess([self.key], 10)
        self.assertFalse(state["blocked"])
        self.assertIsNone(state["lines"][self.key]["snr_est_db"])

    def test_low_snr_and_weak_rssi_are_rejected(self):
        self.feed(rssi=-55, noise=-58)
        state = self.q.assess([self.key], 10)
        self.assertIn("low_snr", state["lines"][self.key]["reasons"])
        self.q = SignalQuality()
        self.feed(rssi=-90, noise=-100)
        state = self.q.assess([self.key], 10)
        self.assertIn("weak_rssi", state["lines"][self.key]["reasons"])

    def test_changed_channel_does_not_match_training_channel(self):
        self.feed(channel=11)
        state = self.q.assess([self.key], 10)
        self.assertTrue(state["blocked"])
        self.assertIn("channel_changed", state["lines"][self.key]["reasons"])

    def test_isolated_low_snr_frame_is_not_used_in_features(self):
        self.feed()
        accepted = self.q.add(self.receiver, self.frame(120, rssi=-55, noise=-57), 10)
        self.assertFalse(accepted)
        state = self.q.assess([self.key], 10)
        self.assertEqual(state["lines"][self.key]["rejected_frames"], 1)
        self.assertFalse(state["blocked"])

    def test_silence_does_not_report_good_reception(self):
        self.feed()
        self.assertTrue(self.q.assess([self.key], 12)["blocked"])

    def test_loss_flag_clears_after_a_clean_window(self):
        self.q.add(self.receiver, self.frame(1), 9)
        self.feed(start=100)
        self.assertTrue(self.q.assess([self.key], 10)["blocked"])
        self.feed(now=12, start=120)
        self.assertFalse(self.q.assess([self.key], 12)["blocked"])
