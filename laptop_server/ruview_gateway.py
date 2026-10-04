#!/usr/bin/env python3
"""
Шлюз на ноутбуке: UDP CSI (RUV2/RUV1) → опционально nn_pipeline.process_csi → WebSocket для 3D-карты.

  pip install -r requirements.txt
  python ruview_gateway.py

Откройте в браузере: http://127.0.0.1:8080/map3d.html
На ESP в веб-интерфейсе укажите IP этого ноутбука и порт UDP 5555.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import threading
import http.server
import socketserver
from pathlib import Path

import csi_packet

try:
    from nn_pipeline import process_csi
except ImportError:
    import numpy as np

    def process_csi(amps, rssi, ts):
        a = np.asarray(amps, dtype=np.float32).ravel()
        mx = float(np.max(np.abs(a))) + 1e-6 if a.size else 1.0
        heights = np.zeros(64, dtype=np.float32)
        heights[: min(64, len(a))] = np.clip(a[:64] / mx, 0, 1)
        return {"heights": heights.tolist(), "rssi": rssi, "ts": ts, "meta": {}}


STATIC = Path(__file__).resolve().parent / "static"
WS_CLIENTS = set()


def parse_packet(data: bytes) -> dict | None:
    fr = csi_packet.parse(data)
    if fr is None or fr.from_node:  # шлюз показывает одну линию — от роутера
        return None
    return {"ts": fr.ts_us, "rssi": fr.rssi, "seq": fr.seq, "amps": fr.amp.tolist()}


def run_http_server(host: str, port: int) -> None:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(STATIC), **kw)

        def log_message(self, fmt, *args):
            pass

    with socketserver.TCPServer((host, port), Handler) as httpd:
        httpd.serve_forever()


async def ws_register(websocket):
    WS_CLIENTS.add(websocket)
    try:
        await websocket.wait_closed()
    finally:
        WS_CLIENTS.discard(websocket)


async def ws_broadcast(payload: str) -> None:
    dead = []
    for ws in list(WS_CLIENTS):
        try:
            await ws.send(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        WS_CLIENTS.discard(ws)


async def udp_receiver(udp_port: int) -> None:
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", udp_port))
    sock.setblocking(False)
    while True:
        try:
            data, _addr = await loop.sock_recvfrom(sock, 65535)
        except asyncio.CancelledError:
            break
        pkt = parse_packet(data)
        if not pkt:
            continue
        try:
            viz = process_csi(pkt["amps"], pkt["rssi"], pkt["ts"])
        except Exception as e:
            viz = {
                "heights": [0.0] * 64,
                "error": str(e),
                "ts": pkt["ts"],
                "rssi": pkt["rssi"],
            }
        msg = json.dumps({"raw": pkt, "viz": viz}, separators=(",", ":"))
        await ws_broadcast(msg)


async def main_async(udp_port: int, ws_port: int) -> None:
    import websockets

    async with websockets.serve(ws_register, "0.0.0.0", ws_port, ping_interval=20):
        await udp_receiver(udp_port)


def main() -> None:
    ap = argparse.ArgumentParser(description="RuView laptop gateway: UDP → WebSocket 3D")
    ap.add_argument("--udp", type=int, default=5555, help="UDP порт приёма CSI")
    ap.add_argument("--ws", type=int, default=8765, help="WebSocket порт для браузера")
    ap.add_argument("--http", type=int, default=8080, help="HTTP статика (map3d.html)")
    ap.add_argument("--http-host", default="0.0.0.0", help="HTTP bind")
    args = ap.parse_args()

    if not STATIC.is_dir():
        STATIC.mkdir(parents=True, exist_ok=True)

    t = threading.Thread(
        target=run_http_server,
        args=(args.http_host, args.http),
        daemon=True,
    )
    t.start()
    print(f"HTTP  http://127.0.0.1:{args.http}/map3d.html")
    print(f"UDP   listening 0.0.0.0:{args.udp}  (укажите этот IP на ESP)")
    print(f"WS    ws://127.0.0.1:{args.ws}")
    asyncio.run(main_async(args.udp, args.ws))


if __name__ == "__main__":
    main()
