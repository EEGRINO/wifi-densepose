"""Local diagnostic commands on the existing WebSocket listener."""

import ipaddress
import json
import re
import time
from urllib.parse import urlsplit

import diagnostics

VIEWS = {"map", "esp1", "esp2"}
SIGNALS = {"ready", "no_iq", "disconnected", "no_receiver", "zero_iq"}
SILHOUETTES = {"map_view", "no_position", "no_iq", "other_receiver", "no_pose", "outside_view", "drawn"}
POSITION_STATES = {"estimated", "empty", "untrained", "model_needed", "waiting_signal",
                   "missing_lines", "poor_signal", "model_unreliable", "background_failed",
                   "lines_changed", "unknown", "moving", "still", "disconnected", "no_layout",
                   "ab_calibrating", "ab_present_unknown", "ab_recalibrate"}


def local_connection(websocket):
    try:
        peer = ipaddress.ip_address(websocket.remote_address[0])
        mapped = getattr(peer, "ipv4_mapped", None)
        if not (peer.is_loopback or (mapped and mapped.is_loopback)):
            return False
        request = getattr(websocket, "request", None)
        headers = request.headers if request is not None else websocket.request_headers
        origins = headers.get_all("Origin")
        if not origins:
            return True
        if len(origins) != 1:
            return False
        origin = urlsplit(origins[0])
        return origin.scheme in ("http", "https") and origin.hostname in ("localhost", "127.0.0.1", "::1")
    except (ValueError, TypeError, IndexError, AttributeError):
        return False


def browser_event(journal, tab, data):
    code, view = data.get("event"), data.get("view")
    if not isinstance(code, str) or not isinstance(view, str) or view not in VIEWS:
        return
    fields = {"view": view, "tab": tab}
    address = data.get("receiver")
    if isinstance(address, str) and 0 < len(address) <= 39:
        try:
            fields["receiver"] = journal.receiver(str(ipaddress.ip_address(address)))
        except ValueError:
            pass
    if code == "receiver" and view == "map":
        return
    if code == "signal":
        state = data.get("state")
        if not isinstance(state, str) or state not in SIGNALS:
            return
        fields["state"] = state
        count = data.get("iq_count")
        if type(count) is int and 0 <= count <= 1024:
            fields["iq_count"] = count
    elif code == "cell":
        state, cell = data.get("state"), data.get("cell")
        if not isinstance(state, str) or state not in POSITION_STATES:
            return
        fields["state"] = state
        if isinstance(cell, str) and re.fullmatch(r"[A-Z][0-9]{1,3}", cell):
            fields["cell"] = cell
        model = data.get("model")
        if isinstance(model, str) and model in ("sectors", "ranging", "ab"):
            fields["model"] = model
    elif code == "silhouette":
        reason = data.get("reason")
        if not isinstance(reason, str) or reason not in SILHOUETTES:
            return
        fields["reason"] = reason
    elif code not in ("view", "receiver"):
        return
    journal.event(f"browser.{code}", source="browser", **fields)


async def handle_socket(websocket):
    if not local_connection(websocket):
        await websocket.close(code=1008, reason="Local diagnostics only")
        return
    journal = diagnostics.current
    tab = None
    if journal:
        journal.browser_count += 1
        tab = journal.browser_count
    minute, count = int(time.monotonic() // 60), 0
    async for wire in websocket:
        if not isinstance(wire, str) or len(wire) > 2048:
            continue
        try:
            message = json.loads(wire)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if message == {"cmd": "diagnostics_snapshot"}:
            content = journal.snapshot() if journal else None
            response = {"type": "diagnostics_snapshot", "available": content is not None}
            if content is not None:
                response.update(name=journal.path.name, content=content)
            await websocket.send(json.dumps(response, ensure_ascii=False))
        elif journal and message.get("cmd") == "diagnostics_event":
            now = int(time.monotonic() // 60)
            if now != minute:
                minute, count = now, 0
            if count < 300:
                count += 1
                browser_event(journal, tab, message)
