"""Per-run diagnostic JSONL. Storage failure must not stop the pipeline."""

import atexit
import errno
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent / "logs"
MAX_BYTES = 2 * 1024 * 1024
MAX_FILES = 20
RETENTION_SECONDS = 14 * 86400
LOG_NAME = re.compile(r"ruview-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}\.jsonl")
UDP_PORTS = (5555, 8080)
WS_PORT = 8765
current = None


def _revision():
    try:
        git = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=DIRECTORY.parent.parent,
            capture_output=True, text=True, timeout=1, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        revision = git.stdout.strip()
        return revision if re.fullmatch(r"[a-f0-9]{7,40}", revision) else None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


class RunJournal:
    def __init__(self, entrypoint, ports, directory=DIRECTORY, *, max_bytes=MAX_BYTES):
        self.id = uuid.uuid4().hex[:8]
        self.path = directory / f"ruview-{datetime.now():%Y%m%d-%H%M%S}-{self.id}.jsonl"
        self.lock = threading.RLock()
        self.max_bytes = max_bytes
        self.available = False
        self.size = 0
        self.pinned = {}
        self.receivers = {}
        self.lines = {}
        self.transmitters = {}
        self.seen = set()
        self.active_lines = set()
        self.next_summary = 0
        self.states = {}
        self.finished = False
        self.browser_count = 0
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=False)
            self.available = True
        except OSError as error:
            self.storage_failed(error)
        if self.available:
            self.event("server.starting", pin=True, schema=1, entrypoint=entrypoint, ports=ports, git_commit=_revision())
            if self.available:
                print(f"Журнал диагностики: {self.path}", flush=True)
                self._prune(directory)

    def storage_failed(self, error):
        self.available = False
        print(f"Журнал диагностики недоступен: {self.path} ({type(error).__name__}). "
              "Приём CSI и калибровка продолжатся.", file=sys.stderr, flush=True)

    def _prune(self, directory):
        try:
            logs = [(path.stat().st_mtime, path) for path in directory.iterdir()
                    if path != self.path and LOG_NAME.fullmatch(path.name)
                    and not path.is_symlink() and path.is_file()]
            cutoff = time.time() - RETENTION_SECONDS
            for index, (modified, path) in enumerate(sorted(logs, reverse=True)):
                if index >= MAX_FILES - 1 or modified < cutoff:
                    path.unlink(missing_ok=True)
        except OSError as error:
            print(f"Не удалось очистить старые журналы ({type(error).__name__}).",
                  file=sys.stderr, flush=True)

    def event(self, code, *, level="info", source="server", pin=False, **fields):
        with self.lock:
            if not self.available:
                return
            try:
                line = self._encode(code, level, source, fields)
                if len(line) > self.max_bytes // 4:
                    return
                if pin:
                    self.pinned[code] = line
                if self.size + len(line) > self.max_bytes:
                    self._compact(line)
                else:
                    with self.path.open("ab") as stream:
                        self.size += stream.write(line)
            except OSError as error:
                self.storage_failed(error)
            except (ValueError, TypeError, OverflowError):
                # A malformed diagnostic field must not interrupt the application.
                return

    def _encode(self, code, level, source, fields):
        return (json.dumps({"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                            "level": level, "event": code, "source": source, "run_id": self.id, **fields},
                           ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")

    def _compact(self, incoming):
        pinned = b"".join(self.pinned.values())
        marker = self._encode("server.log_compacted", "info", "server", {})
        budget = max(0, self.max_bytes // 2 - len(pinned) - len(marker) - len(incoming))
        old = self.path.read_bytes()
        tail = old[-budget:] if budget else b""
        if budget and len(old) > budget:
            tail = tail[tail.index(b"\n") + 1:] if b"\n" in tail else b""
        tail = b"".join(line for line in tail.splitlines(keepends=True)
                        if line not in self.pinned.values())
        data = pinned + tail + marker + (b"" if incoming in self.pinned.values() else incoming)
        temp = self.path.with_suffix(".tmp")
        try:
            temp.write_bytes(data)
            temp.replace(self.path)
        finally:
            temp.unlink(missing_ok=True)
        self.size = len(data)

    def snapshot(self):
        with self.lock:
            if self.available:
                try:
                    return self.path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as error:
                    self.storage_failed(error)
        return None

    def receiver(self, address):
        return self.receivers.setdefault(address, f"RX{len(self.receivers) + 1}")

    def line(self, key):
        address, _, rest = key.partition(" <- ")
        transmitter, _, channel = rest.partition(" / ch")
        return {"line": self.lines.setdefault(key, f"L{len(self.lines) + 1}"),
                "receiver": self.receiver(address),
                "transmitter": self.transmitters.setdefault(transmitter, f"TX{len(self.transmitters) + 1}"),
                "channel": int(channel) if re.fullmatch(r"[0-9]{1,3}", channel) else None}

    def changed(self, slot, code, signature, **fields):
        if self.states.get(slot) != signature:
            self.states[slot] = signature
            self.event(code, **fields)


def start(entrypoint, *, http_port=None):
    global current
    current = RunJournal(entrypoint, {"udp": UDP_PORTS, "websocket": WS_PORT, "http": http_port})
    atexit.register(finish)
    return current


def finish(reason="process_exit"):
    if current and not current.finished:
        current.event("server.stopped", reason=reason)
        current.finished = True


def event(code, **fields):
    if current:
        current.event(code, **fields)


def failure(code, error, **fields):
    number = getattr(error, "errno", None)
    conflict = number in (errno.EADDRINUSE, 10048)
    event(code, level="error", exception=type(error).__name__, errno=number,
          reason=fields.pop("reason", "port_in_use" if conflict else "exception"), **fields)


def dataset_id(dataset):
    key = (dataset or {}).get("id")
    return key if isinstance(key, str) and re.fullmatch(r"original|[a-f0-9]{32}", key) else None


def first_frame(receiver, frame, port, iq):
    if current and current.available and receiver not in current.seen and any(iq):
        current.seen.add(receiver)
        line = f"{receiver} <- {frame.src_id:04X} / ch{frame.channel}"
        event("csi.first_frame", **current.line(line), port=port, iq_count=len(iq) // 2)


def observe_signal(quality, active, now):
    if not current or not current.available:
        return
    active = set(active)
    previous = current.active_lines
    for key in sorted(active - previous):
        event("csi.line_available", **current.line(key))
    for key in sorted(previous - active):
        event("csi.line_lost", **current.line(key))
    current.active_lines = active
    if now >= current.next_summary:
        current.next_summary = now + 5
        event("csi.summary", state="receiving" if active else "waiting_signal",
              lines=[{**current.line(key), **report} for key, report in sorted(quality["lines"].items())],
              receivers=[{"receiver": current.receiver(address), **report}
                         for address, report in sorted(quality["receivers"].items())])


def position(model, *, state, reason, active_lines, expected_lines, ready, point=None, sector=None):
    if not current or not current.available:
        return
    coords = [round(float(value), 3) for value in point] if point is not None else None
    missing = tuple(sorted(set(expected_lines) - set(active_lines)))
    signature = (state, reason, len(active_lines), tuple(expected_lines), missing, ready, sector,
                 tuple(round(value, 1) for value in coords) if coords and sector is None else None)
    current.changed(("position", model), "position.changed", signature,
                    model=model, model_ready=ready, available_lines=len(active_lines),
                    expected_lines=len(expected_lines), missing_lines=[current.line(key) for key in missing],
                    live_state=state, live_reason=reason, position=coords, sector=sector)
