#!/usr/bin/env python3
"""
Проигрывание записи CSI (*.ruvrec) в UDP — как будто работают живые платы.

  venv/bin/python csi_replay.py recordings/20261005_094634.ruvrec
  venv/bin/python csi_replay.py запись.ruvrec --speed 4 --loop

Каждая плата из записи шлёт со своего адреса 127.0.0.<последний октет её IP>, поэтому линии
не сливаются (Linux позволяет привязываться к любому адресу 127.0.0.0/8).
По ходу печатает метки сценария — видно, что происходило в комнате в этот момент.

Слушатель (nn_pipeline.py / csi_monitor.py) должен быть запущен на том же порту; живые платы
в это время лучше выключить или увести в другую сеть, иначе потоки смешаются.
"""
from __future__ import annotations

import argparse
import socket
import time

from csi_record import REC_LABEL, REC_META, REC_PACKET, read_recording

LABEL_RU = {"exit": "выходит", "empty": "пусто", "walk": "ходьба", "sit_still": "сидит неподвижно",
            "sit_work": "сидит и работает", "end": "конец", "aborted": "прервано"}


def replay_once(path: str, port: int, speed: float, socks: dict[str, socket.socket]) -> None:
    t_rec0 = None
    t_wall0 = time.monotonic()
    sent = 0
    for kind, t, d in read_recording(path):
        if kind == REC_META:
            continue
        if t_rec0 is None:
            t_rec0 = t
        delay = (t - t_rec0) / speed - (time.monotonic() - t_wall0)
        if delay > 0:
            time.sleep(delay)
        if kind == REC_LABEL:
            print(f"[{(t - t_rec0):6.1f} с записи] {LABEL_RU.get(d, d)}   (отправлено пакетов: {sent})", flush=True)
            continue
        ip, data = d
        s = socks.get(ip)
        if s is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind((f"127.0.0.{ip.rsplit('.', 1)[1]}", 0))
            socks[ip] = s
            print(f"  плата {ip} → шлёт с 127.0.0.{ip.rsplit('.', 1)[1]}", flush=True)
        s.sendto(data, ("127.0.0.1", port))
        sent += 1
    print(f"Запись проиграна: {sent} пакетов", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Проигрывание записи CSI в UDP")
    ap.add_argument("path")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--speed", type=float, default=1.0, help="ускорение (2 — вдвое быстрее)")
    ap.add_argument("--loop", action="store_true", help="повторять по кругу")
    args = ap.parse_args()
    socks: dict[str, socket.socket] = {}
    try:
        while True:
            replay_once(args.path, args.port, args.speed, socks)
            if not args.loop:
                break
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
