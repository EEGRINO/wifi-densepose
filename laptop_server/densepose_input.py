"""Input preparation for DensePose From WiFi (arXiv:2301.00250, sections 3 and 3.1)."""

import argparse
import json
from pathlib import Path
import sys

import numpy as np
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from csi_ranging import SERVER, snapshot


WINDOW_SHAPE = (5, 30, 3, 3)


def filter_axis(values, axis, reducer):
    padding = [(0, 0)] * values.ndim
    padding[axis] = (1, 1)
    padded = np.pad(values, padding, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, 3, axis=axis)
    return reducer(windows, axis=-1)


def prepare_windows(csi):
    """Convert aligned 100 Hz windows into amplitude and phase tensors.

    Input axes are time, frequency in ascending order, transmitter, receiver.
    An optional leading dimension batches independent five-frame windows.
    Section 3.1 leaves the filter widths unspecified. Use width 3 along time
    and frequency, with replicated edges and independent antenna pairs.
    """
    if (csi.ndim not in (4, 5) or csi.shape[-4:] != WINDOW_SHAPE or not csi.size
            or not np.iscomplexobj(csi) or not np.isfinite(csi).all()):
        raise ValueError("Нужен конечный комплексный массив 5×30×3×3 или N×5×30×3×3.")

    phase = np.unwrap(np.angle(csi), axis=-3)
    for reducer in (np.median, np.mean):
        for axis in (-4, -3):
            phase = filter_axis(phase, axis, reducer)

    frequencies = WINDOW_SHAPE[1]
    slope = (phase[..., -1, :, :] - phase[..., 0, :, :]) / (2 * np.pi * frequencies)
    offset = phase.mean(axis=-3, keepdims=True)
    subcarriers = np.arange(1, frequencies + 1).reshape(frequencies, 1, 1)
    phase = phase - offset - slope[..., None, :, :] * subcarriers
    shape = (*csi.shape[:-4], WINDOW_SHAPE[0] * frequencies, *WINDOW_SHAPE[-2:])
    return np.abs(csi).reshape(shape).astype(np.float32), phase.reshape(shape).astype(np.float32)


def report_ruview():
    with connect(SERVER, open_timeout=4) as ws:
        state = snapshot(ws)
    return {
        "required": {"transmit_antennas": 3, "receive_antennas": 3, "subcarriers": 30,
                     "sample_rate_hz": 100, "samples_per_window": 5},
        "ruview": {"receiver_ips": sorted({line.split(" <- ")[0] for line in state["lines"]}),
                   "lines": state["quality"]["lines"], "usable": not state["quality"]["blocked"]},
        "supports_paper_input": False,
        "reason": "Кадр RUV2 содержит одну радиолинию и время её приёмника. "
                  "Для модели из статьи нужен синхронный MIMO-отсчёт 3×3.",
    }


def main():
    parser = argparse.ArgumentParser(description="Подготовка входа DensePose From WiFi.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Сравнить живой формат RuView с форматом статьи")
    prepare = commands.add_parser("prepare", help="Подготовить тензоры из синхронизированных комплексных окон")
    prepare.add_argument("source", type=Path, help="Файл .npy с окнами 5×30×3×3")
    prepare.add_argument("output", type=Path, help="Новый файл .npz с amplitude и phase")
    args = parser.parse_args()
    try:
        if args.command == "status":
            print(json.dumps(report_ruview(), ensure_ascii=False, indent=2))
        else:
            if args.source.suffix.lower() != ".npy" or args.output.suffix.lower() != ".npz":
                raise ValueError("Вход должен быть .npy, выход — .npz.")
            csi = np.load(args.source, allow_pickle=False)
            amplitude, phase = prepare_windows(csi)
            with args.output.open("xb") as target:
                np.savez_compressed(target, amplitude=amplitude, phase=phase)
            print(f"{args.output} · amplitude {amplitude.shape}, phase {phase.shape}")
    except (ValueError, OSError, WebSocketException) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
