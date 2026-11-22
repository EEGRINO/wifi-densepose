"""Conservative CSI reception quality gates; does not identify RF interference sources."""
from __future__ import annotations

import collections
import math
import statistics

LOOKBACK = 1.0
MIN_PACKETS = 10
MAX_LOSS = 0.10
MIN_RSSI = -85
MIN_SNR = 6
RESET_SETTLE = 3.0


class SignalQuality:
    def __init__(self):
        self.samples = {}
        self.events = {}
        self.sequence = {}
        self.reset_until = {}
        self.reset_count = collections.Counter()

    def add(self, receiver, frame, now):
        events = self.events.setdefault(receiver, collections.deque(maxlen=4000))
        seq = getattr(frame, "seq", None)
        accepted = True
        gap = duplicate = reordered = reset = 0
        if seq is not None:
            seq = int(seq) & 0xFFFFFFFF
            previous = self.sequence.get(receiver)
            if previous is not None:
                delta = (seq - previous) & 0xFFFFFFFF
                if delta == 0:
                    duplicate, accepted = 1, False
                elif delta < 0x80000000:
                    # Forward sequence including natural uint32 rollover.
                    gap = delta - 1
                elif seq < 64 <= previous:
                    reset = 1
                    self.reset_until[receiver] = now + RESET_SETTLE
                    self.reset_count[receiver] += 1
                else:
                    reordered, accepted = 1, False
            if accepted:
                self.sequence[receiver] = seq
        events.append((now, gap, duplicate, reordered, reset, int(accepted)))
        if not accepted:
            return False
        rssi = getattr(frame, "rssi", None)
        noise = getattr(frame, "noise", None)
        valid_rssi = isinstance(rssi, (int, float)) and math.isfinite(rssi) and -127 <= rssi <= -1
        valid_noise = isinstance(noise, (int, float)) and math.isfinite(noise) and -127 <= noise <= -20
        rssi = float(rssi) if valid_rssi else None
        noise = float(noise) if valid_noise else None
        snr = rssi - noise if rssi is not None and noise is not None else None
        key = f"{receiver} <- {frame.src_id:04X} / ch{frame.channel}"
        good = rssi is not None and rssi >= MIN_RSSI and (snr is None or snr >= MIN_SNR)
        self.samples.setdefault(key, collections.deque(maxlen=1200)).append((now, rssi, noise, snr, good))
        return good

    def assess(self, expected_lines, now):
        active = sorted(k for k, values in self.samples.items()
                        if values and 0 <= now - values[-1][0] <= .5)
        lines = expected_lines or active
        receivers = {}
        for receiver in {k.split(" <- ")[0] for k in lines}:
            events = [v for v in self.events.get(receiver, ()) if 0 <= now - v[0] <= LOOKBACK]
            received = sum(v[5] for v in events)
            lost = sum(v[1] for v in events)
            duplicates = sum(v[2] for v in events)
            reordered = sum(v[3] for v in events)
            ratio = lost / (lost + received) if lost + received else None
            receivers[receiver] = {
                "received": received, "sequence_gaps": lost, "loss_ratio": ratio,
                "duplicates": duplicates, "reordered": reordered,
                "resets_seen": self.reset_count[receiver],
                "settling": now < self.reset_until.get(receiver, 0)}
        reports = {}
        blocked = not bool(lines)
        specific_fault = False
        for key in lines:
            receiver = key.split(" <- ")[0]
            samples = [v for v in self.samples.get(key, ()) if 0 <= now - v[0] <= LOOKBACK]
            rs = [v[1] for v in samples if v[1] is not None]
            ns = [v[2] for v in samples if v[2] is not None]
            sn = [v[3] for v in samples if v[3] is not None]
            med_rssi = statistics.median(rs) if rs else None
            med_noise = statistics.median(ns) if ns else None
            med_snr = statistics.median(sn) if sn else None
            transport = receivers[receiver]
            reasons = []
            if key not in active:
                changed = any(k.split(" / ch")[0] == key.split(" / ch")[0] for k in active)
                reasons.append("channel_changed" if changed else "missing")
            elif len(samples) < MIN_PACKETS or samples[-1][0] - samples[0][0] < .45:
                reasons.append("warming_up")
            if transport["settling"]:
                reasons.append("receiver_restarted")
            if transport["loss_ratio"] is not None and transport["loss_ratio"] > MAX_LOSS:
                reasons.append("packet_loss")
            if transport["received"] and (transport["duplicates"] + transport["reordered"]) / max(1, transport["received"] + transport["duplicates"] + transport["reordered"]) > .2:
                reasons.append("unstable_order")
            if samples and len(rs) < len(samples) * .8:
                reasons.append("invalid_rssi")
            if med_rssi is not None and med_rssi < MIN_RSSI:
                reasons.append("weak_rssi")
            # Invalid/unsupported noise values never masquerade as SNR measurements.
            if med_snr is not None and len(sn) >= len(samples) * .8 and med_snr < MIN_SNR:
                reasons.append("low_snr")
            rejected = sum(not v[4] for v in samples)
            if samples and rejected / len(samples) > MAX_LOSS:
                reasons.append("bad_frames")
            bad = bool(reasons)
            blocked |= bad
            specific_fault |= any(v in reasons for v in (
                "packet_loss", "weak_rssi", "low_snr", "receiver_restarted", "unstable_order",
                "channel_changed", "invalid_rssi", "bad_frames"))
            reports[key] = {"packets_per_window": len(samples), "rejected_frames": rejected, "rssi_dbm": med_rssi,
                            "noise_dbm": med_noise, "snr_est_db": med_snr,
                            "ok": not bad, "reasons": reasons}
        return {"version": 1, "blocked": blocked, "specific_fault": specific_fault,
                "lines": reports, "receivers": receivers,
                "thresholds": {"min_frames": MIN_PACKETS, "max_loss_ratio": MAX_LOSS,
                               "min_rssi_dbm": MIN_RSSI, "min_snr_est_db": MIN_SNR},
                "scope": "Reception quality only; RF interference source is not identified"}
