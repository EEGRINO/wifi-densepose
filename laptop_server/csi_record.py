#!/usr/bin/env python3
"""
Запись сырого CSI со всех линий с автоматической разметкой по сценарию.

  venv/bin/python csi_record.py              # сценарий по умолчанию (~8 мин)
  venv/bin/python csi_record.py --quick      # тот же сценарий по 10 с — проверить, что всё работает

Программа сама ведёт по шагам: на экране — что делать, звуковой сигнал — переходить к
следующему шагу. Метки шагов пишутся в файл с точным временем, вручную ничего отмечать не нужно.

Файл recordings/<дата_время>.ruvrec:
  заголовок b"RUVREC1\\n", затем записи:
    тип 0 (пакет): u8 0, f64 время приёма (unix), 4 байта IPv4 отправителя, u16 длина, пакет RUV2/RUV1
    тип 1 (метка): u8 1, f64 время, u16 длина, текст UTF-8 — начало шага сценария
    тип 2 (мета):  u8 2, f64 время, u16 длина, JSON UTF-8
Чтение — read_recording().

Не запускайте одновременно с nn_pipeline.py / csi_monitor.py на том же порту.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator

from csi_packet import link_name, parse

MAGIC = b"RUVREC1\n"
REC_PACKET, REC_LABEL, REC_META = 0, 1, 2
_HDR = struct.Struct("<Bd")

SOUND_NEXT = "/usr/share/sounds/freedesktop/stereo/complete.oga"
SOUND_END = "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"

# (метка, что делать, секунд)
SCENARIO = [
    ("exit", "Выйдите из кабинета и закройте дверь", 10),
    ("empty", "Кабинет пуст. Ждите сигнала за дверью", 120),
    ("walk", "Ходите по кабинету, пересекайте его в разных направлениях", 60),
    ("sit_still", "Сядьте и замрите: не двигайтесь, дышите спокойно", 120),
    ("sit_work", "Сидите и работайте: печатайте, двигайте руками", 120),
    ("exit", "Выйдите из кабинета и закройте дверь", 10),
    ("empty", "Кабинет пуст. Ждите сигнала «конец» за дверью", 60),
]


def read_recording(path: str | os.PathLike) -> Iterator[tuple[int, float, object]]:
    """Итератор (тип, время, данные): для пакета данные = (ip, bytes), для метки — str, для мета — dict."""
    with open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise ValueError("не файл записи RuView")
        while True:
            h = f.read(_HDR.size)
            if len(h) < _HDR.size:
                return
            kind, t = _HDR.unpack(h)
            if kind == REC_PACKET:
                ip = socket.inet_ntoa(f.read(4))
                (n,) = struct.unpack("<H", f.read(2))
                yield kind, t, (ip, f.read(n))
            else:
                (n,) = struct.unpack("<H", f.read(2))
                text = f.read(n).decode("utf-8")
                yield kind, t, (json.loads(text) if kind == REC_META else text)


def write_recording(path, packets, metadata, label):
    """Write already received packets in the same format, without binding another UDP socket."""
    start = float(metadata["start_unix"])
    with open(path, "xb") as f:
        f.write(MAGIC)
        for kind, value in ((REC_META, json.dumps(metadata, ensure_ascii=False, allow_nan=False)),
                            (REC_LABEL, label)):
            data = value.encode("utf-8")
            f.write(_HDR.pack(kind, start) + struct.pack("<H", len(data)) + data)
        for when, ip, packet in packets:
            f.write(_HDR.pack(REC_PACKET, when) + socket.inet_aton(ip)
                    + struct.pack("<H", len(packet)) + packet)


class Recorder:
    def __init__(self, path: Path, port: int) -> None:
        self.f = open(path, "wb")
        self.f.write(MAGIC)
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.sock.bind(("0.0.0.0", port))
        self.sock.settimeout(0.2)
        self.stop = threading.Event()
        self.count = 0
        self.links: dict[str, int] = {}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _write(self, kind: int, t: float, body: bytes) -> None:
        with self.lock:
            self.f.write(_HDR.pack(kind, t) + body)

    def text(self, kind: int, text: str) -> None:
        b = text.encode("utf-8")
        self._write(kind, time.time(), struct.pack("<H", len(b)) + b)

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            t = time.time()
            fr = parse(data)
            if fr is None:
                continue
            self._write(REC_PACKET, t, socket.inet_aton(addr[0]) + struct.pack("<H", len(data)) + data)
            self.count += 1
            name = link_name(addr[0], fr)
            self.links[name] = self.links.get(name, 0) + 1

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2)
        with self.lock:
            self.f.close()
        self.sock.close()


def play(sound: str, times: int = 1) -> None:
    """Сигнал не блокирует сценарий; если звука нет — просто молчим."""
    if not os.path.exists(sound):
        return
    cmd = "; ".join([f"paplay '{sound}'"] * times)
    subprocess.Popen(["sh", "-c", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    ap = argparse.ArgumentParser(description="Запись CSI по сценарию с автоматической разметкой")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--quick", action="store_true", help="все шаги по 10 с — проверка")
    ap.add_argument("--note", default="", help="комментарий в файл: место, расстановка и т.п.")
    args = ap.parse_args()

    scenario = [(lbl, txt, 10 if args.quick else sec) for lbl, txt, sec in SCENARIO]
    out_dir = Path(__file__).resolve().parent / "recordings"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / (datetime.now().strftime("%Y%m%d_%H%M%S") + ("_quick" if args.quick else "") + ".ruvrec")

    rec = Recorder(path, args.port)
    rec.thread.start()
    print("Проверяю поток CSI (3 с)…")
    time.sleep(3)
    if not rec.links:
        rec.close()
        path.unlink(missing_ok=True)
        sys.exit("Нет пакетов CSI — проверьте, что платы в сети и ноутбук подключён к роутеру.")
    for name, n in sorted(rec.links.items()):
        print(f"  {name:32s} {n / 3:5.0f} кадр/с")
    rec.text(REC_META, json.dumps({
        "scenario": [[l, t, s] for l, t, s in scenario],
        "note": args.note,
        "links_at_start": sorted(rec.links),
        "created": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False))

    total = sum(s for _, _, s in scenario)
    input(f"\nСценарий ~{total // 60} мин {total % 60} с. Звуковой сигнал = переходите к следующему шагу.\n"
          "Нажмите Enter, чтобы начать… ")
    try:
        for i, (label, instruction, seconds) in enumerate(scenario):
            rec.text(REC_LABEL, label)
            if i > 0:
                play(SOUND_NEXT, 2)
            print(f"\n[{i + 1}/{len(scenario)}] {instruction.upper()}")
            nxt = scenario[i + 1][1] if i + 1 < len(scenario) else "конец записи"
            end = time.monotonic() + seconds
            while (left := end - time.monotonic()) > 0:
                print(f"\r  осталось {int(left) // 60}:{int(left) % 60:02d}   кадров записано: {rec.count:7d}"
                      f"   дальше: {nxt}   ", end="", flush=True)
                time.sleep(0.25)
        rec.text(REC_LABEL, "end")
        play(SOUND_END, 2)
        print("\n\nГотово.")
    except KeyboardInterrupt:
        rec.text(REC_LABEL, "aborted")
        print("\n\nПрервано — записанное сохранено.")
    finally:
        rec.close()
    size = path.stat().st_size / 1e6
    print(f"Файл: {path}  ({size:.1f} МБ, {rec.count} кадров)")


if __name__ == "__main__":
    main()
