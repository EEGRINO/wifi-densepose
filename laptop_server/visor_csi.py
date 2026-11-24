"""A bounded live view of received RUV2 samples, separate from feature processing."""

import base64
from collections import OrderedDict

from csi_packet import wire_iq


class VisorCsi:
    def __init__(self):
        self.frames = OrderedDict()

    def add(self, receiver, frame, wire, now):
        if frame.version != 2:
            return None
        key = receiver, frame.src_id, frame.channel, frame.from_node
        self.frames[key] = {
            "receiver": receiver, "source": f"{frame.src_id:04X}", "channel": frame.channel,
            "peer": frame.from_node, "sequence": frame.seq, "timestamp_us": frame.ts_us,
            "rssi": frame.rssi, "noise": frame.noise, "received_at": now, "iq": wire_iq(wire),
        }
        self.frames.move_to_end(key)
        if len(self.frames) > 32:
            self.frames.popitem(last=False)
        return key

    def set_pose(self, key, points, wrists=()):
        if key is not None and points and key in self.frames:
            self.frames[key]["pose"] = points
            self.frames[key]["pose_wrists"] = list(wrists)

    def snapshot(self, now):
        packets = []
        for frame in self.frames.values():
            age = now - frame["received_at"]
            if age > 1:
                continue
            payload = {key: value for key, value in frame.items() if key not in ("received_at", "iq")}
            payload["age_ms"] = round(age * 1000)
            payload["iq"] = base64.b64encode(frame["iq"]).decode("ascii")
            packets.append(payload)
        return {"type": "visor_csi", "frames": packets}
