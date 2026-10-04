"""
Разбор UDP-пакетов CSI от прошивки RuView.

RUV2 (текущая прошивка), 20 байт заголовка + 64 × (imag, real) int8:
   0  4  "RUV2"
   4  4  seq      uint32 — номер кадра CSI (общий для всех линий узла), пропуски = потери
   8  4  ts_us    uint32 — время приёма кадра радиомодулем, мкс
  12  1  rssi     int8
  13  1  noise    int8
  14  1  channel  uint8
  15  1  flags    биты 0-1: sig_mode; бит 2: первые 4 байта I/Q обнулены;
                  бит 3: передатчик — узел RuView (иначе роутер)
  16  2  n_sc     uint16
  18  2  src_id   uint16 — последние 2 байта MAC передатчика

RUV1 (старая прошивка): ts uint32, rssi int8, hint uint8, n uint16, затем n × float32 амплитуд.

Линия (link) = передатчик → приёмник: (IP приёмника, src_id). У RUV1 одна линия — от роутера.

Порядок поднесущих в обоих форматах — как в буфере L-LTF ESP32: 0..31, затем -32..-1.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

N_SC = 64
# Индексы буфера с полезными поднесущими (без DC и защитных полос): 52 шт.
USEFUL_SC = np.r_[1:27, 38:64]
# Перестановка индексов буфера в частотный порядок -32..31 (для графиков).
FREQ_ORDER = np.r_[32:64, 0:32]

_RUV2_HDR = struct.Struct("<4sIIbbBBHH")
_RUV1_HDR = struct.Struct("<4sIbBH")


@dataclass
class CsiFrame:
    version: int
    seq: int | None
    ts_us: int
    rssi: int
    noise: int | None
    channel: int | None
    sig_mode: int | None
    src_id: int | None         # последние 2 байта MAC передатчика (RUV2)
    from_node: bool            # передатчик — узел RuView (маяк ESP-NOW), а не роутер
    amp: np.ndarray            # (64,) float32, порядок буфера; нулевые поднесущие = 0
    iq: np.ndarray | None      # (64,) complex64 (только RUV2)


def parse(data: bytes) -> CsiFrame | None:
    """Пакет RUV2/RUV1 → CsiFrame; чужие и битые пакеты → None."""
    if len(data) < 12:
        return None
    magic = data[:4]
    if magic == b"RUV2":
        if len(data) < _RUV2_HDR.size:
            return None
        _, seq, ts_us, rssi, noise, channel, flags, n, src_id = _RUV2_HDR.unpack_from(data)
        if n == 0 or n > N_SC or len(data) < _RUV2_HDR.size + 2 * n:
            return None
        raw = np.frombuffer(data, dtype=np.int8, count=2 * n, offset=_RUV2_HDR.size).reshape(n, 2)
        iq = np.zeros(N_SC, dtype=np.complex64)
        iq[:n] = raw[:, 1].astype(np.float32) + 1j * raw[:, 0].astype(np.float32)
        mask = np.zeros(N_SC, dtype=bool)
        mask[USEFUL_SC] = True
        iq[~mask] = 0
        return CsiFrame(
            version=2, seq=seq, ts_us=ts_us, rssi=rssi, noise=noise, channel=channel,
            sig_mode=flags & 0x3, src_id=src_id, from_node=bool(flags & 0x08),
            amp=np.abs(iq).astype(np.float32), iq=iq,
        )
    if magic == b"RUV1":
        _, ts, rssi, _hint, n = _RUV1_HDR.unpack_from(data)
        n = min(int(n), N_SC)
        if n == 0 or len(data) < 12 + 4 * n:
            return None
        amp = np.zeros(N_SC, dtype=np.float32)
        amp[:n] = np.frombuffer(data, dtype="<f4", count=n, offset=12)
        amp = np.nan_to_num(amp, nan=0.0, posinf=0.0, neginf=0.0)
        return CsiFrame(
            version=1, seq=None, ts_us=ts, rssi=rssi, noise=None, channel=None,
            sig_mode=None, src_id=None, from_node=False, amp=amp, iq=None,
        )
    return None


def link_name(rx_ip: str, fr: CsiFrame) -> str:
    """Подпись линии для логов: «192.168.31.106 ← узел 75BC» / «… ← роутер 14B1»."""
    if fr.src_id is None:
        return f"{rx_ip} ← роутер"
    kind = "узел" if fr.from_node else "роутер"
    return f"{rx_ip} ← {kind} {fr.src_id:04X}"
