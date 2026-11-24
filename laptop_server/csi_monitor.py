#!/usr/bin/env python3
"""
Диагностика потока CSI от ESP32 в терминале.

  venv/bin/python csi_monitor.py            # порт 5555
  venv/bin/python csi_monitor.py --port 8080

Раз в секунду для каждой линии (передатчик → ESP-приёмник: роутер или другой узел RuView)
печатает: частоту кадров, RSSI/шум, долю 11n-кадров и метрику движения — разброс амплитуд
поднесущих во времени за окно. Потери считаются по seq для каждого приёмника целиком.
Проверка: в пустой комнате метрика держится у минимума, при ходьбе через линию растёт в разы.

Не запускайте одновременно с nn_pipeline.py на том же порту — пакеты поделятся между ними.
"""
from __future__ import annotations

import argparse
import collections
import socket
import time

import numpy as np

from csi_packet import USEFUL_SC, link_name, parse

BAR = " ▁▂▃▄▅▆▇█"


def motion_metric(amps: np.ndarray) -> float:
    """
    amps: (T, 64). Каждый кадр делим на его среднюю амплитуду (компенсация АРУ),
    затем для каждой поднесущей — коэффициент вариации во времени; усредняем по поднесущим.
    """
    a = amps[:, USEFUL_SC].astype(np.float64)
    a /= a.mean(axis=1, keepdims=True) + 1e-9
    return float(np.mean(a.std(axis=0) / (a.mean(axis=0) + 1e-9)))


class ReceiverStats:
    """Потери по seq: номер общий для всех линий одного приёмника."""

    def __init__(self) -> None:
        self.recv = 0
        self.first_seq: int | None = None
        self.last_seq: int | None = None
        self.resets = 0

    def add(self, seq: int | None) -> None:
        if seq is not None:
            if self.last_seq is not None and seq < self.last_seq:
                self.resets += 1                       # ESP перезагрузилась — seq начался заново
                self.first_seq = seq
                self.recv = 0
            if self.first_seq is None:
                self.first_seq = seq
            self.last_seq = seq
        self.recv += 1

    def loss_text(self) -> str:
        if self.first_seq is None or self.last_seq is None:
            return " n/a "
        expected = self.last_seq - self.first_seq + 1
        return f"{max(0.0, 1.0 - self.recv / expected) * 100:5.1f}%" if expected > 0 else "  —  "

    def reset_window(self) -> None:
        self.recv = 0
        self.first_seq = None


class LinkStats:
    def __init__(self, window_s: float) -> None:
        self.frames: collections.deque = collections.deque()
        self.window_s = window_s
        self.recv = 0
        self.sig11n = 0
        self.rssi: list[int] = []
        self.noise: list[int] = []
        self.version = 0
        self.min_motion: float | None = None

    def add(self, fr, now: float) -> None:
        self.version = fr.version
        self.recv += 1
        if fr.sig_mode == 1:
            self.sig11n += 1
        self.rssi.append(fr.rssi)
        if fr.noise is not None:
            self.noise.append(fr.noise)
        self.frames.append((now, fr.amp))
        while self.frames and now - self.frames[0][0] > self.window_s:
            self.frames.popleft()

    def report(self, name: str, dt: float) -> str:
        n = self.recv
        rate = n / dt if dt > 0 else 0.0
        rssi = f"{np.mean(self.rssi):5.1f}" if self.rssi else "  —  "
        noise = f"{np.mean(self.noise):5.1f}" if self.noise else "  —  "
        ht = f"{self.sig11n / n * 100:3.0f}%" if n and self.version == 2 else " n/a"

        if len(self.frames) >= 10:
            m = motion_metric(np.stack([a for _, a in self.frames]))
            self.min_motion = m if self.min_motion is None else min(self.min_motion, m)
            ratio = m / (self.min_motion + 1e-9)
            bar = BAR[min(len(BAR) - 1, int((ratio - 1.0) * 2))]
            motion = f"{m:.4f}  ×{ratio:4.1f} к минимуму {bar}"
        else:
            motion = "мало кадров"

        self.recv = 0
        self.sig11n = 0
        self.rssi.clear()
        self.noise.clear()
        return (f"{name:32s} {rate:6.1f} кадр/с  RSSI {rssi}  шум {noise}  11n {ht}  "
                f"движение {motion}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Диагностика потока CSI от ESP32")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--window", type=float, default=1.0, help="окно метрики движения, с")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(0.2)
    print(f"Слушаю UDP 0.0.0.0:{args.port} … (Ctrl+C — выход)")

    receivers: dict[str, ReceiverStats] = {}
    links: dict[str, LinkStats] = {}
    foreign = 0
    last_report = time.monotonic()
    try:
        while True:
            try:
                data, addr = sock.recvfrom(2048)
                now = time.monotonic()
                fr = parse(data)
                if fr is None:
                    foreign += 1
                else:
                    receivers.setdefault(addr[0], ReceiverStats()).add(fr.seq)
                    links.setdefault(link_name(addr[0], fr), LinkStats(args.window)).add(fr, now)
            except socket.timeout:
                now = time.monotonic()
            if now - last_report >= 1.0:
                dt = now - last_report
                last_report = now
                if not links:
                    print(f"нет пакетов CSI (чужих пакетов: {foreign}) — проверьте IP/порт в настройках ESP")
                for ip, rs in sorted(receivers.items()):
                    extra = f", перезагрузок: {rs.resets}" if rs.resets else ""
                    print(f"── приёмник {ip}: потери {rs.loss_text().strip()}{extra}")
                    rs.reset_window()
                    for name in sorted(n for n in links if n.startswith(ip + " ")):
                        print("   " + links[name].report(name, dt))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
