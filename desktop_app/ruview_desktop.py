#!/usr/bin/env python3
"""
RuView Ultimate — десктоп: обнаружение ESP32 по mDNS и визуализация CSI по WebSocket.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from typing import Callable, List, Optional

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from websocket import WebSocketConnectionClosedException, WebSocketTimeoutException, create_connection
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf


def _decode_props(info) -> dict:
    out = {}
    if not info or not info.properties:
        return out
    for k, v in info.properties.items():
        key = k.decode() if isinstance(k, bytes) else str(k)
        if isinstance(v, bytes):
            try:
                out[key] = v.decode()
            except Exception:
                out[key] = repr(v)
        else:
            out[key] = str(v)
    return out


class RuDiscoverListener(ServiceListener):
    def __init__(self, on_service: Callable[[str, int, dict], None]) -> None:
        self._on = on_service

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if not info or not info.addresses:
            return
        address = next((address for address in info.addresses if len(address) == 4), None)
        if address is None:
            return
        ip = socket.inet_ntoa(address)
        props = _decode_props(info)
        if props.get("app") == "ruview" or "ruview" in name.lower():
            self._on(ip, int(info.port), props)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.add_service(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


class WsThread(QObject):
    frame = pyqtSignal(dict)
    status = pyqtSignal(str)

    def __init__(self, url: str) -> None:
        super().__init__()
        self._url = url
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start_ws(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop_ws(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        self.status.emit("connecting…")
        while not self._stop.is_set():
            try:
                ws = create_connection(self._url, timeout=5)
                if self._stop.is_set():
                    ws.close()
                    return
                self.status.emit("live")
                ws.settimeout(0.4)
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except WebSocketTimeoutException:
                        continue
                    except WebSocketConnectionClosedException:
                        break
                    if not raw:
                        break
                    try:
                        msg = json.loads(raw)
                        self.frame.emit(msg)
                    except json.JSONDecodeError:
                        continue
                ws.close()
            except Exception as e:
                if not self._stop.is_set():
                    self.status.emit(f"error: {e}")
                    self._stop.wait(1.2)
            if not self._stop.is_set():
                self.status.emit("reconnect…")


class HeatmapWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows = 96
        self._cols = 64
        self._buf: List[float] = [0.0] * (self._rows * self._cols)
        self._head = 0
        self.setMinimumHeight(220)

    def push_row(self, amps: List[float]) -> None:
        n = min(len(amps), self._cols)
        base = self._head * self._cols
        for c in range(self._cols):
            self._buf[base + c] = float(amps[c]) if c < n else 0.0
        self._head = (self._head + 1) % self._rows
        self.update()

    def paintEvent(self, event) -> None:
        del event
        w, h = self.width(), self.height()
        if w < 4 or h < 4:
            return
        img = QImage(self._cols, self._rows, QImage.Format.Format_RGB32)
        mx = max(max(self._buf), 1e-6)
        for r in range(self._rows):
            src_r = (self._head - 1 - r + self._rows * 2) % self._rows
            for c in range(self._cols):
                t = (self._buf[src_r * self._cols + c] / mx) ** 0.45
                t = max(0.0, min(1.0, t))
                rr = int(30 + (255 - 30) * t)
                gg = int(80 + (180 - 80) * (1.0 - t * 0.5))
                bb = int(160 + (80 - 160) * t)
                img.setPixelColor(c, r, QColor(rr, gg, bb))
        pix = QImage(img).scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        p = QPainter(self)
        p.drawImage(0, 0, pix)


class SilhouetteWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._vals: List[float] = [0.0] * 12
        self.setMinimumHeight(200)

    def set_amps(self, amps: List[float]) -> None:
        bands = len(self._vals)
        per = max(1, len(amps) // bands)
        for b in range(bands):
            s = 0.0
            for k in range(per):
                idx = b * per + k
                if idx < len(amps):
                    s += float(amps[idx])
            self._vals[b] = s / per
        self.update()

    def paintEvent(self, event) -> None:
        del event
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(5, 7, 12))
        mx = max(max(self._vals), 1e-6)
        w = self.width()
        h = self.height()
        n = len(self._vals)
        gap = 6
        bw = max(6, (w - gap * (n + 1)) // n)
        x0 = (w - (bw * n + gap * (n - 1))) // 2
        for i, v in enumerate(self._vals):
            bh = int(12 + (h - 24) * (v / mx))
            x = x0 + i * (bw + gap)
            y = h - 12 - bh
            grad_top = QColor(61, 158, 255)
            grad_bot = QColor(26, 74, 122)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad_top)
            p.drawRoundedRect(x, y, bw, bh, 4, 4)
            p.setBrush(grad_bot)
            p.drawRoundedRect(x, y + bh // 2, bw, bh // 2, 4, 4)


class GraphWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._data: List[float] = []
        self.setMinimumHeight(160)

    def set_series(self, data: List[float]) -> None:
        self._data = list(data)
        self.update()

    def paintEvent(self, event) -> None:
        del event
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(5, 7, 12))
        if len(self._data) < 2:
            return
        mx = max(max(self._data), 1e-6)
        w, h = self.width(), self.height()
        p.setPen(QPen(QColor(61, 158, 255), 2))
        n = len(self._data)
        for i in range(n - 1):
            x1 = 4 + (w - 8) * i / (n - 1)
            x2 = 4 + (w - 8) * (i + 1) / (n - 1)
            y1 = h - 6 - (h - 12) * (self._data[i] / mx)
            y2 = h - 6 - (h - 12) * (self._data[i + 1] / mx)
            p.drawLine(int(x1), int(y1), int(x2), int(y2))


class MainWindow(QMainWindow):
    service_discovered = pyqtSignal(str, int, dict)
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("RuView Ultimate — Desktop")
        self.resize(1100, 720)
        self._zeroconf: Optional[Zeroconf] = None
        self._browser: Optional[ServiceBrowser] = None
        self._ws_worker: Optional[WsThread] = None

        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)

        top = QHBoxLayout()
        self._combo = QComboBox()
        self._combo.setMinimumWidth(220)
        self._host = QLineEdit()
        self._host.setPlaceholderText("IP вручную, напр. 192.168.1.42")
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(80)
        btn_scan = QPushButton("Сканировать mDNS")
        btn_go = QPushButton("Подключить WebSocket")
        top.addWidget(QLabel("Устройства:"))
        top.addWidget(self._combo, 1)
        top.addWidget(QLabel("Или IP:"))
        top.addWidget(self._host, 1)
        top.addWidget(QLabel("Порт HTTP:"))
        top.addWidget(self._port)
        top.addWidget(btn_scan)
        top.addWidget(btn_go)
        lay.addLayout(top)

        self._motion = QLabel("Motion Score: —")
        self._motion.setFont(QFont("JetBrains Mono", 18, QFont.Weight.Bold))
        self._motion.setStyleSheet("color:#3d9eff;")
        self._rssi = QLabel("RSSI: —")
        self._ws_status = QLabel("WS: offline")
        row = QHBoxLayout()
        row.addWidget(self._motion)
        row.addStretch()
        row.addWidget(self._rssi)
        row.addWidget(self._ws_status)
        lay.addLayout(row)

        grid = QGridLayout()
        self._heat = HeatmapWidget()
        self._graph = GraphWidget()
        self._sil = SilhouetteWidget()
        f1 = QFrame()
        f1.setLayout(QVBoxLayout())
        f1.layout().addWidget(QLabel("Тепловая карта"))
        f1.layout().addWidget(self._heat)
        f2 = QFrame()
        f2.setLayout(QVBoxLayout())
        f2.layout().addWidget(QLabel("Амплитуды"))
        f2.layout().addWidget(self._graph)
        f3 = QFrame()
        f3.setLayout(QVBoxLayout())
        f3.layout().addWidget(QLabel("Радио-силуэт"))
        f3.layout().addWidget(self._sil)
        grid.addWidget(f1, 0, 0, 1, 2)
        grid.addWidget(f2, 1, 0)
        grid.addWidget(f3, 1, 1)
        lay.addLayout(grid, 1)

        btn_cal = QPushButton("Calibrate (POST /api/calibrate)")
        lay.addWidget(btn_cal)

        btn_scan.clicked.connect(self._start_scan)
        btn_go.clicked.connect(self._connect_ws)
        btn_cal.clicked.connect(self._calibrate)
        self.service_discovered.connect(self._on_discovered, Qt.ConnectionType.QueuedConnection)

        self._discovered: dict[str, int] = {}

        tip = QLabel(
            "Подсказка: после прошивки ESP объявляет mDNS имя «ruview-csi» и сервис http с TXT app=ruview. "
            "Подключитесь к той же Wi‑Fi сети, что и модуль."
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#7a8aa3;font-size:11px;")
        lay.addWidget(tip)

        self._start_scan()

    def closeEvent(self, event) -> None:
        self._stop_ws()
        self._stop_scan()
        super().closeEvent(event)

    def _stop_scan(self) -> None:
        if self._browser:
            self._browser.cancel()
            self._browser = None
        if self._zeroconf:
            self._zeroconf.close()
            self._zeroconf = None

    def _start_scan(self) -> None:
        self._stop_scan()
        self._combo.clear()
        self._discovered.clear()
        self._zeroconf = Zeroconf()
        self._browser = ServiceBrowser(
            self._zeroconf, "_http._tcp.local.", RuDiscoverListener(self.service_discovered.emit)
        )

    def _on_discovered(self, ip: str, port: int, props: dict) -> None:
        key = f"{ip}:{port}"
        self._discovered[key] = port
        label = f"{ip}:{port} ({props.get('board', '?')})"
        exists = False
        for i in range(self._combo.count()):
            if self._combo.itemData(i) == key:
                exists = True
                break
        if not exists:
            self._combo.addItem(label, key)

    def _stop_ws(self) -> None:
        if self._ws_worker:
            self._ws_worker.stop_ws()
            self._ws_worker.deleteLater()
            self._ws_worker = None

    def _target(self) -> tuple[str, int]:
        ip = self._host.text().strip()
        port = self._port.value()
        key = self._combo.currentData()
        if not ip and key in self._discovered:
            return key.partition(":")[0], self._discovered[key]
        return ip, port

    def _connect_ws(self) -> None:
        ip, port = self._target()
        if not ip:
            QMessageBox.warning(self, "RuView", "Укажите IP или выберите устройство из списка.")
            return
        self._stop_ws()
        url = f"ws://{ip}:{port}/ws"
        self._ws_worker = WsThread(url)
        self._ws_worker.frame.connect(self._on_frame)
        self._ws_worker.status.connect(self._ws_status.setText)
        self._ws_worker.start_ws()

    def _on_frame(self, msg: dict) -> None:
        amps = msg.get("a")
        if isinstance(amps, list) and amps:
            self._heat.push_row([float(x) for x in amps])
            self._graph.set_series([float(x) for x in amps])
            self._sil.set_amps([float(x) for x in amps])
        m = msg.get("motion")
        if m is not None:
            self._motion.setText(f"Motion Score: {float(m):.3f}")
        r = msg.get("rssi")
        if r is not None:
            self._rssi.setText(f"RSSI: {r} dBm")

    def _calibrate(self) -> None:
        ip, port = self._target()
        if not ip:
            QMessageBox.warning(self, "RuView", "Нужен IP устройства.")
            return
        try:
            import urllib.request

            class _Post(urllib.request.Request):
                def get_method(self):  # noqa: D401
                    return "POST"

            url = f"http://{ip}:{port}/api/calibrate"
            urllib.request.urlopen(_Post(url), timeout=3)
        except Exception as e:
            QMessageBox.critical(self, "RuView", str(e))


def main() -> None:
    app = QApplication([])
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
