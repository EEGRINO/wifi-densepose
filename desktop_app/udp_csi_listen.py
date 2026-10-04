#!/usr/bin/env python3
"""
Простой приёмник UDP CSI от ESP32 (магия RUV2, порт 5555; старая прошивка — RUV1).
Запуск: python udp_csi_listen.py 0.0.0.0 5555
Подробная диагностика (частота, потери, движение): laptop_server/csi_monitor.py
"""
import math
import socket
import struct
import sys


def main() -> None:
    host = sys.argv[1] if len(sys.argv) > 1 else "0.0.0.0"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 5555
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((host, port))
    print(f"Listening UDP {host}:{port} …")
    while True:
        data, addr = s.recvfrom(65535)
        if len(data) >= 20 and data[:4] == b"RUV2":
            _, seq, ts, rssi, noise, ch, flags, n, _ = struct.unpack_from("<4sIIbbBBHH", data)
            if len(data) < 20 + 2 * n:
                print(addr, "short packet", len(data))
                continue
            iq = struct.unpack_from(f"<{2 * n}b", data, 20)
            amps = [math.hypot(iq[2 * i], iq[2 * i + 1]) for i in range(n)]
            print(f"{addr[0]} seq={seq} ts={ts} rssi={rssi} noise={noise} ch={ch} "
                  f"sig_mode={flags & 3} n={n} amp[1:6]={[round(a, 1) for a in amps[1:6]]}")
        elif len(data) >= 12 and data[:4] == b"RUV1":
            ts, = struct.unpack_from("<I", data, 4)
            rssi = struct.unpack_from("b", data, 8)[0]
            (n,) = struct.unpack_from("<H", data, 10)
            if len(data) < 12 + n * 4:
                print(addr, "short packet", len(data))
                continue
            amps = struct.unpack_from("<" + "f" * n, data, 12)
            print(f"{addr[0]} RUV1 ts={ts} rssi={rssi} n={n} first5={amps[:5]}")
        else:
            print(addr, "unknown", len(data))


if __name__ == "__main__":
    main()
