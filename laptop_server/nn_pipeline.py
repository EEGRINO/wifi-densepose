import asyncio
import errno
import math
import socket
import json
import traceback
import websockets
import numpy as np
import os
import time
import collections
from typing import Any

import csi_packet
from presence import PresenceDetector

# ── КОНФИГУРАЦИЯ ──────────────────────────────────────────────────────────────
UDP_IP   = "0.0.0.0"
UDP_PORT = 8080
WS_PORT  = 8765

# Несколько ESP: калибровка активности/конечностей, vitals, карта комнаты — только с «основного».
# Пустая строка: первый увиденный по UDP IP закрепляется до перезапуска nn_pipeline.
# Пример: CSI_PRIMARY_IP=192.168.31.131
CSI_PRIMARY_IP = os.environ.get("CSI_PRIMARY_IP", "").strip()
_sticky_primary_udp_ip: str | None = None

# Два ESP: последний нормированный CSI (64,) и время приёма на ПК
_csi_last: dict[str, tuple[np.ndarray, float]] = {}
CSI_PAIR_MAX_DT_S = float(os.environ.get("CSI_PAIR_MAX_DT", "0.12"))
CSI_DUAL_ACTIVITY_FUSION = os.environ.get("CSI_DUAL_ACTIVITY_FUSION", "1").strip().lower() not in (
    "0", "false", "no", "",
)
CSI_DUAL_FUSION_MODE = os.environ.get("CSI_DUAL_FUSION_MODE", "mean").strip().lower()
_dual_activity_announced = False

_HERE      = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(_HERE, "models", "model-q4.bin")

connected_clients: set = set()

# Диагностика: UDP есть, но в браузер не уходит (раз в ~15 с)
_last_log_no_ws: float = 0.0
_last_log_json_err: float = 0.0


def _json_default_udp(o: Any) -> Any:
    """numpy и прочие типы в payload CSI → JSON для WebSocket."""
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"не сериализуется в JSON: {type(o)!r}")


def _sanitize_for_json(obj: Any) -> Any:
    """
    Рекурсивно убирает NaN/Inf — иначе json.dumps выдаёт невалидный JSON (NaN без кавычек),
    а JSON.parse в браузере молча падает (в map3d catch пустой) → «нет CSI» при живом UDP.
    """
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(x) for x in obj]
    if isinstance(obj, np.ndarray):
        return _sanitize_for_json(obj.tolist())
    if isinstance(obj, np.generic):
        x = float(obj)
        return 0.0 if math.isnan(x) or math.isinf(x) else x
    if isinstance(obj, float):
        return 0.0 if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, bool):
        return obj
    if obj is None:
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, str):
        return obj
    return obj


def _udp_payload_to_json(parsed: dict) -> str:
    clean = _sanitize_for_json(parsed)
    return json.dumps(clean, default=_json_default_udp, ensure_ascii=False, allow_nan=False)


def _udp_drain_all(sock: socket.socket, data: bytes, addr) -> list[tuple[bytes, object]]:
    """Сбрасывает очередь датаграмм в сокете: все накопившиеся пакеты по порядку."""
    batch: list[tuple[bytes, object]] = [(data, addr)]
    while True:
        try:
            d, a = sock.recvfrom(65535)
            batch.append((d, a))
        except BlockingIOError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                break
            raise
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                break
            raise
    return batch


def _should_apply_udp_side_effects(source_ip: str | None) -> bool:
    """Запись калибровок и тяжёлая статистика комнаты — с одного выбранного ESP."""
    global _sticky_primary_udp_ip
    if not source_ip:
        return True
    if CSI_PRIMARY_IP:
        return source_ip == CSI_PRIMARY_IP
    if _sticky_primary_udp_ip is None:
        _sticky_primary_udp_ip = source_ip
        print(f"📌 CSI «основной» источник (липкий): {source_ip}  (env CSI_PRIMARY_IP=… для фиксации)")
    return source_ip == _sticky_primary_udp_ip


def _store_csi_frame(source_ip: str | None, heights: list) -> None:
    if not source_ip:
        return
    h = np.asarray(heights[:64], dtype=np.float32)
    _csi_last[source_ip] = (h, time.monotonic())


def _pair_other_heights(source_ip: str) -> tuple[list | None, str | None]:
    """CSI другого датчика, синхронный по времени приёма (окно CSI_PAIR_MAX_DT_S)."""
    if source_ip not in _csi_last:
        return None, None
    _h_self, t_self = _csi_last[source_ip]
    best_h: np.ndarray | None = None
    best_ip: str | None = None
    best_dt = CSI_PAIR_MAX_DT_S + 1.0
    for ip, (h, t) in _csi_last.items():
        if ip == source_ip:
            continue
        dt = abs(t_self - t)
        if dt <= CSI_PAIR_MAX_DT_S and dt < best_dt:
            best_dt = dt
            best_h = h
            best_ip = ip
    if best_h is None:
        return None, None
    return best_h.tolist(), best_ip


# ══════════════════════════════════════════════════════════════════════════════
# POSE MODEL  (CSI → 32 координаты скелета)
# ══════════════════════════════════════════════════════════════════════════════

def load_ml_model():
    if os.path.exists(MODEL_PATH):
        try:
            w = np.fromfile(MODEL_PATH, dtype=np.float32)
            print(f"✅ Позовая модель: {os.path.basename(MODEL_PATH)} ({len(w)} весов)")
            return w
        except Exception as e:
            print(f"⚠️  Ошибка загрузки позовой модели: {e}")
    else:
        print(f"❌  Файл модели не найден: {MODEL_PATH}")
    return None


ml_weights = load_ml_model()
_W_MATRIX = None
# model-q*.bin — 4-битные веса другой модели (энкодер 8→128), прочитанные как float32: «поза» из них —
# мусор порядка 1e35. Скелет из CSI считаем только по матрице, обученной с камеры (camera_calib.html).
_W_FROM_CAMERA = False
_W_MATRIX_BIN: np.ndarray | None = None  # копия из model-q*.bin — откат после камеры
CAMERA_POSE_W_PATH = os.path.join(_HERE, "models", "camera_pose_W.npz")

if ml_weights is not None and len(ml_weights) >= 64 * 32:
    _W_MATRIX = ml_weights[: 64 * 32].reshape(64, 32).astype(np.float32)
    _W_MATRIX_BIN = _W_MATRIX.copy()
    print("✅ Матрица позы (64, 32) готова")


def _json_vec_to_np(v: object, n: int) -> np.ndarray | None:
    """
    JSON-массив или объект { "0": x, "1": y, ... } (так бывает после stringify TypedArray).
    null/NaN из JSON → 0.
    """
    if v is None:
        return None
    if isinstance(v, dict):
        a = np.zeros(n, dtype=np.float64)
        for i in range(n):
            x = v.get(str(i), v.get(i))
            if x is None:
                continue
            try:
                a[i] = float(x)
            except (TypeError, ValueError):
                pass
        return np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0)
    try:
        a = np.asarray(v, dtype=np.float64).ravel()
    except (TypeError, ValueError):
        return None
    if a.size < n:
        z = np.zeros(n, dtype=np.float64)
        z[: a.size] = np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0)
        a = z
    else:
        a = np.nan_to_num(a[:n], nan=0.0, posinf=0.0, neginf=0.0)
    return a


class CameraPoseCalibrationManager:
    """
    Пары (нормированный CSI 64, скелет 32 от камеры) → МНК W (64, 32), как базовая голова позы.
    Сохраняется в camera_pose_W.npz; при старте подменяет _W_MATRIX, если файл есть.
    """

    def __init__(self) -> None:
        self.rows_x: list[np.ndarray] = []
        self.rows_y: list[np.ndarray] = []
        self._load_w()

    def _load_w(self) -> None:
        global _W_MATRIX, _W_FROM_CAMERA
        if not os.path.exists(CAMERA_POSE_W_PATH):
            return
        try:
            d = np.load(CAMERA_POSE_W_PATH, allow_pickle=True)
            W = d.get("W")
            if W is not None and isinstance(W, np.ndarray) and W.shape == (64, 32):
                _W_MATRIX = W.astype(np.float32)
                _W_FROM_CAMERA = True
                n = int(d["n_samples"]) if "n_samples" in d.files else 0
                print(f"✅ Загружена позовая матрица с камеры ({CAMERA_POSE_W_PATH}, {n} сэмплов при обучении)")
        except Exception as e:
            print(f"⚠️  camera_pose_W: {e}")

    @property
    def n_samples(self) -> int:
        return len(self.rows_x)

    def clear(self) -> None:
        self.rows_x.clear()
        self.rows_y.clear()

    def add_batch(self, samples: list) -> tuple[int, str]:
        """samples: [{heights: [64], skeleton: [32]}, ...] — heights как в потоке (уже норм. или сырые)."""
        global _W_MATRIX
        if not isinstance(samples, list):
            return 0, "samples не массив"
        n_in = len(samples)
        ok = 0
        for s in samples:
            if not isinstance(s, dict):
                continue
            h = s.get("heights") or s.get("csi")
            sk = s.get("skeleton")
            if h is None or sk is None:
                continue
            ha = _json_vec_to_np(h, 64)
            if ha is None:
                continue
            mx = float(np.max(np.abs(ha))) + 1e-9
            hn = (ha / mx).astype(np.float32)
            y = _json_vec_to_np(sk, 32)
            if y is None:
                continue
            if not np.isfinite(hn).all() or not np.isfinite(y).all():
                continue
            self.rows_x.append(hn)
            self.rows_y.append(y.astype(np.float32))
            ok += 1
        return ok, f"В запросе {n_in} сэмплов, принято +{ok} (всего {self.n_samples})"

    def train(self) -> tuple[bool, str]:
        global _W_MATRIX, _W_FROM_CAMERA
        n = len(self.rows_x)
        if n < 96:
            return False, f"Мало пар CSI–поза: {n} (нужно ≥ 96 для устойчивого МНК)"
        n_cap = 25000
        if n > n_cap:
            rng = np.random.default_rng(42)
            idx = rng.choice(n, size=n_cap, replace=False)
            Xs = [self.rows_x[i] for i in idx]
            Ys = [self.rows_y[i] for i in idx]
        else:
            Xs = self.rows_x
            Ys = self.rows_y
        X = np.stack(Xs, axis=0)
        Y = np.stack(Ys, axis=0)
        if not np.isfinite(X).all() or not np.isfinite(Y).all():
            return False, "NaN/Inf в матрицах X/Y"
        try:
            W, *_ = np.linalg.lstsq(X, Y, rcond=1e-5)
        except Exception as e:
            return False, f"lstsq: {e}"
        if W.shape != (64, 32):
            return False, "неверная форма W"
        _W_MATRIX = W.astype(np.float32)
        _W_FROM_CAMERA = True
        os.makedirs(os.path.dirname(CAMERA_POSE_W_PATH), exist_ok=True)
        np.savez(CAMERA_POSE_W_PATH, W=_W_MATRIX, n_samples=n)
        err = float(np.mean((X @ _W_MATRIX - Y) ** 2))
        msg = f"Обучено по {n} парам, MSE≈{err:.5f} — матрица сохранена"
        print(f"✅ {msg}")
        try:
            limb_cal.recompute_pose_prototypes()
            limb_cal.save()
        except Exception:
            pass
        return True, msg

    def revert_to_bundled_model(self) -> bool:
        global _W_MATRIX, _W_FROM_CAMERA
        if _W_MATRIX_BIN is None:
            return False
        _W_MATRIX = _W_MATRIX_BIN.copy()
        _W_FROM_CAMERA = False
        if os.path.exists(CAMERA_POSE_W_PATH):
            try:
                os.remove(CAMERA_POSE_W_PATH)
            except Exception:
                pass
        print("↩️  Позовая матрица: снова из model-q*.bin")
        # Прототипы позы в limb_cal считались как norm @ W — после смены W их нужно
        # пересчитать, иначе apply_limb_pose_prototype_blend смешивает «чужие» смещения.
        try:
            limb_cal.save()
        except Exception as e:
            print(f"⚠️  limb_cal после отката позы: {e}")
        return True


camera_pose_cal = CameraPoseCalibrationManager()


# ══════════════════════════════════════════════════════════════════════════════
# CALIBRATION MANAGER  (сбор данных + обучение линейного классификатора)
# ══════════════════════════════════════════════════════════════════════════════

ACTIVITY_LABELS = ["empty", "sleeping", "sitting", "standing", "walking"]
# Сдвиг логита «стоя» до softmax (после обучения); при взвешенном МНК обычно достаточно 0…0.1. 0 = выкл.
ACTIVITY_STANDING_LOGIT_BIAS = float(os.environ.get("RUVIEW_ACTIVITY_STANDING_LOGIT_BIAS", "0.06"))
# Если |P(сидя)−P(стою)| меньше порога, отдать всю массу одному классу (убирает «полусидя» в скелете).
ACTIVITY_SIT_STAND_GAP = float(os.environ.get("RUVIEW_ACTIVITY_SIT_STAND_GAP", "0.11"))
# Обучение: 64 CSI + 4 вспомогательных признака (σ, верх/низ, энтропия амплитуд). Старый activity_model.npz (65 строк) без них.
ACTIVITY_USE_EXTENDED_FEATS = os.environ.get("RUVIEW_ACTIVITY_EXT_FEATS", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "",
)
ACTIVITY_RU     = {
    "empty":    "Пусто",
    "sleeping": "Все спят",
    "sitting":  "Все сидят",
    "standing": "Все стоят",
    "walking":  "Все ходят",
}
N_ACT = len(ACTIVITY_LABELS)


def _sharpen_sitting_standing_probs(probs: np.ndarray) -> np.ndarray:
    """Схлопывание почти равных вероятностей sitting/standing в одну метку."""
    if probs.size != N_ACT:
        return probs
    gap_thr = ACTIVITY_SIT_STAND_GAP
    if not math.isfinite(gap_thr) or gap_thr <= 0:
        return probs
    i_sit = ACTIVITY_LABELS.index("sitting")
    i_std = ACTIVITY_LABELS.index("standing")
    ps, pd = float(probs[i_sit]), float(probs[i_std])
    if ps + pd < 0.18:
        return probs
    if abs(ps - pd) >= gap_thr:
        return probs
    out = np.array(probs, dtype=np.float64, copy=True)
    if ps >= pd:
        out[i_sit] = ps + pd
        out[i_std] = 0.0
    else:
        out[i_std] = ps + pd
        out[i_sit] = 0.0
    out = np.maximum(out, 0.0)
    s = float(out.sum())
    if s > 1e-12:
        out /= s
    return out.astype(np.float32)


def _activity_aux_features(h64: np.ndarray) -> np.ndarray:
    """
    Доп. признаки из max-нормированного CSI (64,): мотив — «сидя»/«стою» часто дают разный профиль
    по поднесущим (верх/низ сетки 8×8), разбросу и форме распределения амплитуд.
    """
    h = np.asarray(h64, dtype=np.float64).ravel()
    if h.size < 64:
        z = np.zeros(64, dtype=np.float64)
        z[: h.size] = h
        h = z
    top = float(np.mean(np.abs(h[:32])))
    bot = float(np.mean(np.abs(h[32:])))
    tb = top / (bot + 1e-9)
    st = float(np.std(h))
    s = float(np.sum(np.abs(h))) + 1e-9
    p = np.abs(h) / s
    ent = -float(np.sum(p * np.log(np.clip(p, 1e-12, None))))
    ex = np.array(
        [
            math.log1p(tb),
            st * 3.0,
            (top - bot) * 2.0,
            ent * 0.35,
        ],
        dtype=np.float64,
    )
    return ex.astype(np.float32)


def activity_feature_vector(heights: list | np.ndarray) -> np.ndarray:
    """64 + 4 признака для нового классификатора (итого hb длины 69 с bias)."""
    h = np.asarray(heights[:64], dtype=np.float32).ravel()
    if h.size < 64:
        z = np.zeros(64, dtype=np.float32)
        z[: h.size] = h
        h = z
    ex = _activity_aux_features(h)
    return np.concatenate([h, ex], axis=0)


def activity_hb_for_weights(W_act: np.ndarray, heights: list) -> np.ndarray:
    """Собрать hb той же размерности, что строки W_act (65 или 69)."""
    if W_act.ndim != 2:
        raise ValueError("W_act должен быть 2D")
    n_in = int(W_act.shape[0])
    if n_in == 65:
        h = np.array(heights[:64], dtype=np.float32).ravel()
        if h.size < 64:
            z = np.zeros(64, dtype=np.float32)
            z[: h.size] = h
            h = z
        return np.append(h, np.float32(1.0))
    if n_in == 69:
        return np.append(activity_feature_vector(heights), np.float32(1.0))
    raise ValueError(f"Неизвестная форма W_act: {W_act.shape}")


ACT_MODEL_PATH = os.path.join(_HERE, "models", "activity_model.npz")
PRESENCE_CALIB_PATH = os.path.join(_HERE, "models", "presence_calib.json")

# Детектор присутствия по всем линиям (presence.py). После калибровки пустой комнаты его решение
# (пусто / движение / неподвижен) заменяет старый классификатор активности в поле activity.
presence = PresenceDetector(calib_path=PRESENCE_CALIB_PATH)
LIMB_CALIB_PATH = os.path.join(_HERE, "models", "limb_calib.npz")

# Классификатор сцен (пусто / спят / сидят / стоят / ходят) → WebSocket для HUD и демо.
# Старая модель обучена на прошивке до RUV2 — по умолчанию выключена, состояние даёт детектор присутствия.
# Включить: RUVIEW_OLD_ACTIVITY=1
USE_ACTIVITY_CLASSIFIER = os.environ.get("RUVIEW_OLD_ACTIVITY", "0").strip().lower() in ("1", "true", "yes")

# Сопоставление текущего CSI с эталонами зон из limb_calib.npz (центроиды записей).
USE_LIMB_CALIB_MATCH = True

# Прототипы позы (32-D выход W@h) по зонам калибровки + смешивание с базовым скелетом.
# Базовый model-q*.bin — линейная проекция без учёта вашей комнаты; прототипы переносят
# «типичный паттерн выхода» при движении зоны head/r_arm/… из ваших сэмплов.
# При больших различиях зон softmax может раздувать силуэт («монстр») — см. clamp ниже.
# Отключить смешивание: RUVIEW_LIMB_POSE_ALPHA=0
USE_LIMB_POSE_PROTO = True
LIMB_POSE_PROTO_WEIGHT = float(os.environ.get("RUVIEW_LIMB_POSE_ALPHA", "0.18"))
# Макс. L2-норма вектора alpha*r в 32-D (признаки не метры); ограничивает «удлинение» конечностей.
LIMB_POSE_PROTO_MAX_BLEND = float(os.environ.get("RUVIEW_LIMB_POSE_MAX_BLEND", "0.42"))

# Калибровка по конечностям: отдельные наборы CSI (64) на зону тела — «зарядка» в комнате
LIMB_KEYS = ["head", "r_arm", "l_arm", "r_leg", "l_leg", "torso"]
LIMB_RU = {
    "head":  "Голова и шея",
    "r_arm": "Правая рука",
    "l_arm": "Левая рука",
    "r_leg": "Правая нога",
    "l_leg": "Левая нога",
    "torso": "Торс и таз",
}


class LimbCalibrationManager:
    """
    Сбор CSI по зонам тела.
    После записи можно обучить линейный классификатор (64+bias → softmax по 6 зонам) —
    он лучше разделяет «какая конечность в движении», чем сравнение с центроидами.
    """

    def __init__(self) -> None:
        self.samples: dict[str, list] = {k: [] for k in LIMB_KEYS}
        self.recording = False
        self.rec_limb = ""
        self.rec_end_time = 0.0
        self._centroid_cache: dict[str, np.ndarray] | None = None
        self.W_limb: np.ndarray | None = None  # (65, 6) веса линейного слоя + softmax
        # Средний выход позовой головы по зонам (32,) и глобальное среднее по всем сэмплам калибровки
        self.pose_proto: dict[str, np.ndarray] = {}
        self.pose_proto_global: np.ndarray | None = None
        self._load()

    def _invalidate_centroids(self) -> None:
        self._centroid_cache = None

    def _centroids(self) -> dict[str, np.ndarray]:
        if self._centroid_cache is not None:
            return self._centroid_cache
        out: dict[str, np.ndarray] = {}
        for k in LIMB_KEYS:
            s = self.samples.get(k, [])
            if len(s) >= 1:
                out[k] = np.mean(np.array(s, dtype=np.float32), axis=0)
        self._centroid_cache = out
        return out

    def frame_scores(self, heights: list) -> dict[str, float] | None:
        """
        Насколько текущий CSI похож на записанные движения по каждой зоне (0…1 после нормировки по max).
        Используется в UI для усиления движения суставов соответствующей группы.
        """
        cents = self._centroids()
        if not cents:
            return None
        h = np.array(heights[:64], dtype=np.float32)
        hn = float(np.linalg.norm(h)) + 1e-9
        h = h / hn
        raw: dict[str, float] = {}
        for k, mu in cents.items():
            mn = float(np.linalg.norm(mu)) + 1e-9
            mu_n = mu / mn
            c = float(np.dot(h, mu_n))
            raw[k] = max(0.0, min(1.0, (c + 1.0) * 0.5))
        mx = max(raw.values()) if raw else 0.0
        if mx < 1e-9:
            return {k: 0.0 for k in raw}
        return {k: round(float(v / mx), 3) for k, v in raw.items()}

    @property
    def has_limb_model(self) -> bool:
        return self.W_limb is not None and self.W_limb.size > 0

    def train_limb_net(self) -> tuple[bool, str]:
        """Обучение линейной модели зон (МНК по one-hot меткам, на инференсе — softmax)."""
        rows: list[np.ndarray] = []
        y_idx: list[int] = []
        for ci, k in enumerate(LIMB_KEYS):
            for s in self.samples.get(k, []):
                if len(s) < 64:
                    continue
                rows.append(np.array(s[:64], dtype=np.float32))
                y_idx.append(ci)
        n_tot = len(rows)
        if n_tot < 24:
            return False, f"Мало данных: {n_tot} сэмплов (нужно ≥24 суммарно по всем зонам)"
        n_cls = len({i for i in y_idx})
        if n_cls < 2:
            return False, "Нужно записать минимум 2 разные зоны тела"
        if n_tot > 12000:
            rng = np.random.default_rng(42)
            pick = rng.choice(n_tot, size=12000, replace=False)
            rows = [rows[i] for i in pick]
            y_idx = [y_idx[i] for i in pick]
            n_tot = 12000
        X = np.stack(rows, axis=0)
        Xb = np.column_stack([X, np.ones(n_tot, dtype=np.float32)])
        Y = np.zeros((n_tot, len(LIMB_KEYS)), dtype=np.float32)
        Y[np.arange(n_tot), y_idx] = 1.0
        if not np.isfinite(Xb).all() or not np.isfinite(Y).all():
            return False, "В данных CSI есть NaN/Inf — перезапишите зоны"
        try:
            W = np.linalg.lstsq(Xb, Y, rcond=None)[0]
        except Exception as e:
            return False, f"lstsq: {e}"
        if W.shape != (65, len(LIMB_KEYS)):
            return False, "внутренняя ошибка формы весов"
        self.W_limb = W.astype(np.float32)
        self.save()
        preds = np.argmax(Xb @ self.W_limb, axis=1)
        y_arr = np.array(y_idx, dtype=np.int32)
        acc = float(np.mean(preds == y_arr))
        msg = f"Модель зон: точность на обучении {acc:.0%} ({n_tot} сэмплов, {n_cls} зон)"
        print(f"✅ {msg}")
        return True, msg

    def predict_limbs(self, heights: list) -> dict[str, float] | None:
        """Softmax-вероятности зон по обученной линейной модели."""
        if not self.has_limb_model:
            return None
        h = np.array(heights[:64], dtype=np.float32)
        hb = np.append(h, 1.0)
        logits = hb @ self.W_limb
        logits = logits - float(np.max(logits))
        exp = np.exp(np.clip(logits, -40.0, 40.0))
        s = float(np.sum(exp)) + 1e-9
        probs = exp / s
        return {LIMB_KEYS[i]: round(float(probs[i]), 3) for i in range(len(LIMB_KEYS))}

    def limb_scores_for_frame(self, heights: list) -> tuple[dict[str, float] | None, str | None]:
        """Возвращает (словарь вероятностей/похожести, источник: 'net' | 'centroid')."""
        p = self.predict_limbs(heights)
        if p is not None:
            return p, "net"
        c = self.frame_scores(heights)
        return c, "centroid" if c else None

    def _load(self) -> None:
        if not os.path.exists(LIMB_CALIB_PATH):
            return
        try:
            d = np.load(LIMB_CALIB_PATH, allow_pickle=True)
            for k in LIMB_KEYS:
                arr = d.get(f"samp_{k}")
                if arr is not None and arr.ndim == 2 and arr.shape[0] > 0:
                    self.samples[k] = arr.astype(np.float32).tolist()
            total = sum(len(v) for v in self.samples.values())
            if total:
                print(f"✅ Калибровка конечностей загружена ({total} сэмплов по зонам)")
            wl = d["W_limb"] if "W_limb" in d.files else None
            if wl is not None and isinstance(wl, np.ndarray) and wl.ndim == 2 and wl.shape == (65, len(LIMB_KEYS)):
                self.W_limb = wl.astype(np.float32)
                print("✅ Загружена линейная модель зон конечностей (W_limb)")
            else:
                self.W_limb = None
            self._invalidate_centroids()
            self._load_pose_protos_from_npz(d)
            if self.pose_proto_global is None:
                self.recompute_pose_prototypes()
        except Exception as e:
            print(f"⚠️  Ошибка загрузки limb_calib: {e}")

    def _load_pose_protos_from_npz(self, d: Any) -> None:
        self.pose_proto = {}
        self.pose_proto_global = None
        g = d.get("pose_proto_global")
        if g is not None and isinstance(g, np.ndarray) and g.size == 32:
            self.pose_proto_global = np.nan_to_num(
                g.astype(np.float32).reshape(32), nan=0.0, posinf=0.0, neginf=0.0
            )
        for k in LIMB_KEYS:
            pk = d.get(f"pose_proto_{k}")
            if pk is not None and isinstance(pk, np.ndarray) and pk.size == 32:
                self.pose_proto[k] = np.nan_to_num(
                    pk.astype(np.float32).reshape(32), nan=0.0, posinf=0.0, neginf=0.0
                )

    def recompute_pose_prototypes(self) -> None:
        """Среднее W@norm(h) по каждой зоне и глобально — для смешивания позы с калибровкой."""
        global _W_MATRIX
        self.pose_proto = {}
        self.pose_proto_global = None
        if _W_MATRIX is None:
            return
        W = _W_MATRIX
        all_rows: list[np.ndarray] = []
        for k in LIMB_KEYS:
            rows = self.samples.get(k, [])
            if len(rows) < 1:
                continue
            sks: list[np.ndarray] = []
            for h in rows:
                inp = np.asarray(h[:64], dtype=np.float32).ravel()
                if inp.size < 64:
                    z = np.zeros(64, dtype=np.float32)
                    z[: inp.size] = inp
                    inp = z
                mx = float(np.max(np.abs(inp))) + 1e-6
                norm = inp / mx
                sks.append((norm @ W).astype(np.float32))
            stacked = np.stack(sks, axis=0)
            mu = np.mean(stacked, axis=0)
            self.pose_proto[k] = np.nan_to_num(mu, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
            all_rows.extend(sks)
        if not all_rows:
            return
        gmu = np.mean(np.stack(all_rows, axis=0), axis=0)
        self.pose_proto_global = np.nan_to_num(gmu, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
        n_z = len(self.pose_proto)
        if n_z >= 1:
            print(f"✅ Прототипы позы по зонам: {n_z} зон, глобальное среднее по {len(all_rows)} кадрам")

    def save(self) -> None:
        self.recompute_pose_prototypes()
        data: dict = {}
        for k in LIMB_KEYS:
            s = self.samples[k]
            data[f"samp_{k}"] = np.array(s, dtype=np.float32) if s else np.zeros((0, 64), dtype=np.float32)
        if self.W_limb is not None:
            data["W_limb"] = self.W_limb
        if self.pose_proto_global is not None:
            data["pose_proto_global"] = self.pose_proto_global
        for k, arr in self.pose_proto.items():
            data[f"pose_proto_{k}"] = arr
        os.makedirs(os.path.dirname(LIMB_CALIB_PATH), exist_ok=True)
        np.savez(LIMB_CALIB_PATH, **data)
        self._invalidate_centroids()

    def start_rec(self, limb: str, duration: int = 15) -> bool:
        if limb not in LIMB_KEYS:
            return False
        self.rec_limb = limb
        self.rec_end_time = time.monotonic() + max(5, min(180, int(duration)))
        self.recording = True
        print(f"🦵 Запись калибровки конечности [{LIMB_RU[limb]}] {duration}с")
        return True

    def stop_rec(self) -> None:
        if self.recording:
            self.recording = False
            self.W_limb = None
            self.save()
            print(f"⏹️  Запись конечности остановлена. {LIMB_RU.get(self.rec_limb, '')}: {len(self.samples.get(self.rec_limb, []))} сэмплов")

    def clear_limb(self, limb: str) -> None:
        if limb in self.samples:
            self.samples[limb] = []
            self.W_limb = None
            self.save()
            print(f"🗑️  Зона '{limb}' очищена (модель зон сброшена — переобучите)")

    def clear_all(self) -> None:
        for k in LIMB_KEYS:
            self.samples[k] = []
        if os.path.exists(LIMB_CALIB_PATH):
            try:
                os.remove(LIMB_CALIB_PATH)
            except Exception:
                pass
        self.W_limb = None
        self.pose_proto = {}
        self.pose_proto_global = None
        self._invalidate_centroids()
        print("🗑️  Калибровка конечностей сброшена")

    def add_sample(self, heights: list) -> tuple[bool, float, str]:
        """(ещё_идёт, осталось_сек, имя_завершённой_зоны_или_пусто)."""
        if not self.recording:
            return False, 0.0, ""
        remaining = self.rec_end_time - time.monotonic()
        if remaining <= 0:
            limb_done = self.rec_limb
            self.recording = False
            n = len(self.samples.get(limb_done, []))
            print(f"✅ Калибровка конечности '{limb_done}' завершена: {n} сэмплов")
            self.W_limb = None
            self.save()
            return False, 0.0, limb_done
        h = list(heights[:64])
        while len(h) < 64:
            h.append(0.0)
        self.samples[self.rec_limb].append(h)
        return True, remaining, ""

    def append_batch_for_limb(self, limb: str, samples: list) -> int:
        """
        Батч с камеры: нормированный CSI (как в МНК позы) кладётся только в выбранную зону.
        samples: [{ heights|csi: [64], skeleton?: ... }, ...]
        """
        if limb not in LIMB_KEYS:
            return 0
        n = 0
        for s in samples:
            if not isinstance(s, dict):
                continue
            h = s.get("heights") or s.get("csi")
            if h is None:
                continue
            ha = _json_vec_to_np(h, 64)
            if ha is None:
                continue
            mx = float(np.max(np.abs(ha))) + 1e-9
            hn = (ha / mx).astype(np.float32)
            if not np.isfinite(hn).all():
                continue
            self.samples[limb].append(hn.tolist())
            n += 1
        if n > 0:
            self._invalidate_centroids()
            self.W_limb = None
            self.save()
            print(f"📷 Камера → зона [{LIMB_RU.get(limb, limb)}]: +{n} CSI (всего в зоне {len(self.samples[limb])})")
        return n

    @property
    def counts(self) -> dict:
        return {k: len(self.samples[k]) for k in LIMB_KEYS}


limb_cal = LimbCalibrationManager()


def _limb_proto_vec32(v: Any) -> np.ndarray:
    """32-D вектор прототипа; NaN/Inf → 0 (иначе subtract в бленде даёт RuntimeWarning)."""
    a = np.asarray(v, dtype=np.float64).ravel()
    if a.size < 32:
        z = np.zeros(32, dtype=np.float64)
        z[: a.size] = a
        a = z
    else:
        a = a[:32].copy()
    return np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0)


def apply_limb_pose_prototype_blend(
    skeleton: list,
    heights: list,
    probs: dict[str, float] | None,
) -> list:
    """
    Сдвигает 32-D выход базовой матрицы позы в сторону прототипов зон (разность «зона − глобальное среднее»),
    взвешенную softmax/похожестью. Использует сэмплы калибровки конечностей, не камеру.
    """
    if not USE_LIMB_POSE_PROTO or LIMB_POSE_PROTO_WEIGHT <= 0:
        return skeleton
    if not skeleton or len(skeleton) < 32:
        return skeleton
    if not limb_cal.pose_proto or limb_cal.pose_proto_global is None or probs is None:
        return skeleton
    if _W_MATRIX is None:
        return skeleton
    alpha = LIMB_POSE_PROTO_WEIGHT
    g = _limb_proto_vec32(limb_cal.pose_proto_global)
    r = np.zeros(32, dtype=np.float64)
    for k in LIMB_KEYS:
        if k not in limb_cal.pose_proto:
            continue
        pk = float(probs.get(k, 0))
        if pk <= 0:
            continue
        r += pk * (_limb_proto_vec32(limb_cal.pose_proto[k]) - g)
    sk = np.asarray(skeleton[:32], dtype=np.float64).ravel()
    if sk.size < 32:
        z = np.zeros(32, dtype=np.float64)
        z[: sk.size] = sk
        sk = z
    sk = np.nan_to_num(sk, nan=0.0, posinf=0.0, neginf=0.0)
    blend = alpha * r
    bn = float(np.linalg.norm(blend))
    max_b = max(0.05, float(LIMB_POSE_PROTO_MAX_BLEND))
    if bn > max_b and bn > 1e-12:
        blend = blend * (max_b / bn)
    out = sk + blend
    return out.astype(np.float32).tolist()


class CalibrationManager:
    """
    Линейная модель: 64 CSI (+ опционально 4 вспомогательных признака) + bias → N_ACT классов
    и регрессия числа людей.
    Обучение: взвешенный МНК по классам (слабые классы не давятся частыми — меньше «всё сидя»).
    """

    def __init__(self):
        self.samples:  dict[str, list] = {l: [] for l in ACTIVITY_LABELS}
        self.n_people: dict[str, int]  = {l: 0  for l in ACTIVITY_LABELS}

        # Запись
        self.recording    = False
        self.rec_label    = ""
        self.rec_n_people = 0
        self.rec_end_time = 0.0

        # Веса модели
        self.W_act: np.ndarray | None = None  # shape (65, N_ACT)
        self.W_ppl: np.ndarray | None = None  # shape (65, 1)

        self._load()

    # ── persistence ──

    def _load(self):
        if not os.path.exists(ACT_MODEL_PATH):
            return
        try:
            d = np.load(ACT_MODEL_PATH, allow_pickle=True)
            self.W_act = d["W_act"] if "W_act" in d else None
            self.W_ppl = d["W_ppl"] if "W_ppl" in d else None
            for lbl in ACTIVITY_LABELS:
                arr = d.get(f"samp_{lbl}")
                if arr is not None and arr.ndim == 2 and arr.shape[0] > 0:
                    self.samples[lbl] = arr.tolist()
                n = d.get(f"ppl_{lbl}")
                if n is not None:
                    self.n_people[lbl] = int(n)
            total = sum(len(v) for v in self.samples.values())
            fd = ""
            if self.W_act is not None and self.W_act.ndim == 2:
                fd = f", вход {int(self.W_act.shape[0]) - 1}+bias"
            print(f"✅ Модель активности загружена ({total} сэмплов, "
                  f"{'обучена' if self.W_act is not None else 'не обучена'}{fd})")
        except Exception as e:
            print(f"⚠️  Ошибка загрузки модели активности: {e}")

    def save(self):
        data: dict = {}
        if self.W_act is not None:
            data["W_act"] = self.W_act
        if self.W_ppl is not None:
            data["W_ppl"] = self.W_ppl
        for lbl in ACTIVITY_LABELS:
            s = self.samples[lbl]
            data[f"samp_{lbl}"] = np.array(s, dtype=np.float32) if s else np.zeros((0, 64))
            data[f"ppl_{lbl}"]  = self.n_people[lbl]
        np.savez(ACT_MODEL_PATH, **data)

    # ── recording ──

    def start_rec(self, label: str, n_people: int, duration: int = 10):
        if label not in ACTIVITY_LABELS:
            return
        self.rec_label    = label
        self.rec_n_people = n_people
        self.rec_end_time = time.monotonic() + duration
        self.recording    = True
        print(f"🎙️  Запись [{ACTIVITY_RU[label]}] {n_people} чел. {duration}с")

    def stop_rec(self):
        self.recording = False
        n = len(self.samples.get(self.rec_label, []))
        print(f"⏹️  Запись остановлена. Класс '{self.rec_label}': {n} сэмплов")

    def clear_class(self, label: str):
        if label in self.samples:
            self.samples[label] = []
            print(f"🗑️  Класс '{label}' очищен")

    def add_sample(self, heights: list) -> tuple[bool, float]:
        """Возвращает (ещё_идёт_запись, оставшихся_секунд)."""
        if not self.recording:
            return False, 0.0
        remaining = self.rec_end_time - time.monotonic()
        if remaining <= 0:
            self.recording = False
            n = len(self.samples[self.rec_label])
            print(f"✅ Запись завершена. Класс '{self.rec_label}': {n} сэмплов")
            return False, 0.0
        h = list(heights[:64])
        while len(h) < 64:
            h.append(0.0)
        self.samples[self.rec_label].append(h)
        self.n_people[self.rec_label] = self.rec_n_people
        return True, remaining

    # ── training ──

    def train(self) -> tuple[bool, str]:
        X_rows, y_act, y_ppl = [], [], []
        for ci, lbl in enumerate(ACTIVITY_LABELS):
            for s in self.samples[lbl]:
                if ACTIVITY_USE_EXTENDED_FEATS:
                    X_rows.append(activity_feature_vector(s))
                else:
                    h = np.array(s[:64], dtype=np.float32)
                    if h.size < 64:
                        z = np.zeros(64, dtype=np.float32)
                        z[: h.size] = h
                        h = z
                    X_rows.append(h)
                y_act.append(ci)
                y_ppl.append(float(self.n_people[lbl]))

        if len(set(y_act)) < 2:
            return False, "Нужно записать минимум 2 разных класса"

        X = np.array(X_rows, dtype=np.float32)
        ya = np.array(y_act, dtype=np.int32)
        yp = np.array(y_ppl, dtype=np.float32)

        # One-hot для классификатора
        Y = np.zeros((len(X), N_ACT), dtype=np.float32)
        Y[np.arange(len(X)), ya] = 1.0

        # Веса строк: баланс классов (иначе МНК тянет к самому частому классу — типично «сидя»)
        n_samples = len(ya)
        cnt = np.bincount(ya, minlength=N_ACT)
        row_w = n_samples / (N_ACT * np.maximum(cnt[ya].astype(np.float64), 1.0))
        sw = np.sqrt(row_w)

        # bias-столбец
        Xb = np.column_stack([X, np.ones(len(X), dtype=np.float32)])
        Xb_w = Xb * sw[:, np.newaxis]
        Y_w = Y * sw[:, np.newaxis]

        self.W_act = np.linalg.lstsq(Xb_w, Y_w, rcond=None)[0]

        yp_w = yp * sw
        self.W_ppl = np.linalg.lstsq(Xb_w, yp_w, rcond=None)[0]

        preds = np.argmax(Xb @ self.W_act, axis=1)
        acc = float(np.mean(preds == ya))
        n_tot = len(X)
        self.save()
        ext = "расширенный вход (64+4)+bias, веса классов" if ACTIVITY_USE_EXTENDED_FEATS else "64+bias, веса классов"
        msg = f"Точность: {acc:.1%}  ({n_tot} сэмплов, {ext})"
        print(f"✅ Обучение завершено: {msg}")
        return True, msg

    # ── inference ──

    def predict(self, heights: list) -> dict | None:
        if self.W_act is None or self.W_ppl is None:
            return None
        hb = activity_hb_for_weights(self.W_act, heights)

        scores  = hb @ self.W_act        # (N_ACT,)
        if math.isfinite(ACTIVITY_STANDING_LOGIT_BIAS) and ACTIVITY_STANDING_LOGIT_BIAS != 0.0:
            scores[ACTIVITY_LABELS.index("standing")] += ACTIVITY_STANDING_LOGIT_BIAS
        scores -= scores.max()
        exp_s   = np.exp(scores)
        probs   = exp_s / exp_s.sum()
        probs   = _sharpen_sitting_standing_probs(probs)

        # .ravel() делает (65,1) → (65,) чтобы dot возвращал 0-dim скаляр
        n_raw   = float(np.dot(hb, self.W_ppl.ravel()))
        n_ppl   = max(0, round(n_raw))
        label   = ACTIVITY_LABELS[int(np.argmax(probs))]
        conf    = float(probs.max())

        return {
            "label":    label,
            "label_ru": ACTIVITY_RU[label],
            "conf":     round(conf, 3),
            "probs":    {ACTIVITY_LABELS[i]: round(float(probs[i]), 3) for i in range(N_ACT)},
            "n_people": n_ppl,
        }

    def predict_fused(
        self,
        heights_a: list,
        heights_b: list,
        *,
        mode: str = "mean",
        partner_ip: str | None = None,
    ) -> dict | None:
        """
        Два независимых CSI (два ESP) → одна разметка активности.
        Объединение softmax-вероятностей: mean или max (кто увереннее в классе).
        Форма входа такая же, как у сохранённой модели (64+bias или 68+4+bias).
        """
        if self.W_act is None or self.W_ppl is None:
            return None
        pa = self.predict(heights_a)
        pb = self.predict(heights_b)
        if pa is None:
            return pb
        if pb is None:
            return pa
        merged: dict[str, float] = {}
        for k in ACTIVITY_LABELS:
            va = float(pa["probs"].get(k, 0.0))
            vb = float(pb["probs"].get(k, 0.0))
            merged[k] = max(va, vb) if mode == "max" else 0.5 * (va + vb)
        s = sum(merged.values())
        if s > 1e-12:
            for k in merged:
                merged[k] /= s
        p_arr = np.array([merged[k] for k in ACTIVITY_LABELS], dtype=np.float64)
        p_arr = _sharpen_sitting_standing_probs(p_arr.astype(np.float32))
        for i, k in enumerate(ACTIVITY_LABELS):
            merged[k] = float(p_arr[i])
        label = max(merged, key=merged.get)
        conf = float(merged[label])
        n_ppl = max(0, round(0.5 * (float(pa["n_people"]) + float(pb["n_people"]))))
        return {
            "label":             label,
            "label_ru":          ACTIVITY_RU[label],
            "conf":              round(conf, 3),
            "probs":             {k: round(merged[k], 3) for k in ACTIVITY_LABELS},
            "n_people":          n_ppl,
            "dual_esp":          True,
            "dual_fusion_mode":  mode,
            "dual_partner_ip":   partner_ip or "",
        }

    @property
    def sample_counts(self) -> dict:
        return {lbl: len(self.samples[lbl]) for lbl in ACTIVITY_LABELS}

    @property
    def has_model(self) -> bool:
        return self.W_act is not None and self.W_ppl is not None


calib = CalibrationManager()


# ══════════════════════════════════════════════════════════════════════════════
# VITALS TRACKER  (дыхание и пульс через анализ CSI во времени)
# ══════════════════════════════════════════════════════════════════════════════

_vitals_buf: collections.deque = collections.deque(maxlen=512)
_vitals_last_ts  = 0.0
_vitals_cache    = {"heart_rate": 0, "respiration": 0}
_VITALS_INTERVAL = 4.0   # пересчёт каждые 4 секунды


def _update_vitals(motion_val: float):
    """Добавляет значение энергии движения в буфер и пересчитывает жизненные показатели."""
    global _vitals_last_ts, _vitals_cache
    now = time.monotonic()
    _vitals_buf.append((motion_val, now))

    if now - _vitals_last_ts < _VITALS_INTERVAL or len(_vitals_buf) < 60:
        return _vitals_cache
    _vitals_last_ts = now

    vals  = np.array([x[0] for x in _vitals_buf], dtype=np.float32)
    times = np.array([x[1] for x in _vitals_buf], dtype=np.float64)
    duration = float(times[-1] - times[0])
    if duration < 5.0:
        return _vitals_cache

    # Убираем тренд и нормализуем
    vals -= vals.mean()
    std = vals.std()
    if std < 1e-8:
        return _vitals_cache
    vals /= std

    # Дыхание: подсчёт нулевых пересечений в диапазоне 0.1–0.5 Гц
    # Сглаживаем, чтобы убрать высокочастотный шум (пульс и т.д.)
    breath_win = max(3, len(vals) // 20)
    breath_sig = np.convolve(vals, np.ones(breath_win) / breath_win, mode='same')
    zc_breath  = np.sum(np.diff(np.sign(breath_sig)) != 0)
    resp_hz    = zc_breath / (2.0 * duration)
    respiration = np.clip(resp_hz * 60, 6.0, 40.0)

    # Пульс: вычитаем медленное дыхание, смотрим оставшееся
    hr_sig = vals - breath_sig
    zc_hr  = np.sum(np.diff(np.sign(hr_sig)) != 0)
    hr_hz  = zc_hr / (2.0 * duration)
    heart_rate = np.clip(hr_hz * 60, 40.0, 140.0)

    _vitals_cache = {
        "heart_rate":  round(float(heart_rate)),
        "respiration": round(float(respiration), 1),
    }
    return _vitals_cache


# Эвристическая оценка роста (см) по вертикальному размаху координат Y в выходе позы (16 точек × 2)
_height_ema: float | None = None


def _estimate_height_cm(skel: list) -> int:
    """Сглаженная оценка роста по выходу TinyML-модели; не медицинский прибор."""
    global _height_ema
    if not skel or len(skel) < 32:
        return 175 if _height_ema is None else int(round(_height_ema))
    arr = np.asarray(skel[:32], dtype=np.float64)
    ys = arr[1::2]
    span = float(ys.max() - ys.min())
    t = max(0.0, min(1.0, (span - 0.08) / 2.5))
    raw = 145.0 + t * 65.0
    if _height_ema is None:
        _height_ema = raw
    else:
        _height_ema = _height_ema * 0.88 + raw * 0.12
    return int(round(_height_ema))


# ══════════════════════════════════════════════════════════════════════════════
# ROOM MAP TRACKER  (статистика отражений — где стены/мебель)
# ══════════════════════════════════════════════════════════════════════════════

_rm_mean = np.zeros(64, dtype=np.float64)   # Welford mean
_rm_M2   = np.zeros(64, dtype=np.float64)   # Welford M2 (для дисперсии)
_rm_n    = 0
_rm_last_sent = 0.0
_ROOM_SEND_INTERVAL = 3.0   # отправляем карту раз в 3 сек


def _update_room_map(heights: list) -> dict | None:
    """Обновляет онлайн-статистику Уэлфорда и возвращает карту комнаты."""
    global _rm_mean, _rm_M2, _rm_n, _rm_last_sent
    h = np.array(heights[:64], dtype=np.float64)
    _rm_n += 1
    delta    = h - _rm_mean
    _rm_mean += delta / _rm_n
    delta2   = h - _rm_mean
    _rm_M2  += delta * delta2

    now = time.monotonic()
    if now - _rm_last_sent < _ROOM_SEND_INTERVAL or _rm_n < 20:
        return None
    _rm_last_sent = now

    # Дисперсия: высокая → движение/люди; низкая при высоком среднем → стена/мебель
    variance  = _rm_M2 / max(_rm_n - 1, 1)
    m_norm    = _rm_mean / (np.max(_rm_mean) + 1e-8)
    v_norm    = variance  / (np.max(variance)  + 1e-8)
    wall_score = (m_norm * (1.0 - v_norm * 0.8)).tolist()

    return {"scores": wall_score, "n": _rm_n}


# ══════════════════════════════════════════════════════════════════════════════
# PACKET PARSING
# ══════════════════════════════════════════════════════════════════════════════

def process_csi(amps, rssi, ts):
    """Совместимость с ruview_gateway.py."""
    a   = np.asarray(amps, dtype=np.float32).ravel()
    inp = np.zeros(64, dtype=np.float32)
    inp[: min(64, len(a))] = a[:64]
    mx   = float(np.max(np.abs(inp))) + 1e-6
    norm = inp / mx
    skel = np.dot(norm, _W_MATRIX).tolist() if _W_MATRIX is not None and _W_FROM_CAMERA else []
    if skel and len(skel) >= 32:
        probs, _src = limb_cal.limb_scores_for_frame(norm.tolist())
        if probs is not None:
            skel = apply_limb_pose_prototype_blend(skel, norm.tolist(), probs)
    return {"heights": norm.tolist(), "skeleton": skel, "rssi": rssi, "ts": ts}


def parse_ruview_packet(data: bytes, *, source_ip: str | None = None) -> dict | None:
    """
    Пакет RUV2/RUV1 от ESP32 — формат см. csi_packet.py.

    source_ip: IP отправителя UDP; калибровка/vitals/карта комнаты — только с «основного» ESP
    (_should_apply_udp_side_effects). Без IP — как один источник (всё включено).
    """
    try:
        frame = csi_packet.parse(data)
        # Пока обработка рассчитана на одну линию на источник — берём только линию от роутера.
        if frame is None or frame.from_node:
            return None
        ts_val   = frame.ts_us
        rssi_raw = frame.rssi
        inp      = frame.amp

        mx   = float(np.max(np.abs(inp))) + 1e-6
        norm = inp / mx

        heights  = norm.tolist()
        if source_ip:
            _store_csi_frame(source_ip, heights)

        skeleton = []
        if _W_MATRIX is not None and _W_FROM_CAMERA:
            try:
                skeleton = np.dot(norm, _W_MATRIX).tolist()
            except Exception as e:
                if not hasattr(parse_ruview_packet, "_err"):
                    print(f"❌ Ошибка инференса позы: {e}")
                    parse_ruview_packet._err = True

        apply_effects = _should_apply_udp_side_effects(source_ip)

        # Активность: с основного ESP; при двух датчиках — слияние вероятностей с парой по времени
        global _dual_activity_announced
        activity = None
        dual_activity_partner_ip = ""
        if apply_effects and USE_ACTIVITY_CLASSIFIER:
            if (
                CSI_DUAL_ACTIVITY_FUSION
                and source_ip
                and len(_csi_last) >= 2
                and calib.has_model
            ):
                ph, pip = _pair_other_heights(source_ip)
                mode = CSI_DUAL_FUSION_MODE if CSI_DUAL_FUSION_MODE in ("mean", "max") else "mean"
                if ph is not None:
                    activity = calib.predict_fused(
                        heights, ph, mode=mode, partner_ip=pip
                    )
                    if pip:
                        dual_activity_partner_ip = pip
                    if activity and activity.get("dual_esp") and not _dual_activity_announced:
                        _dual_activity_announced = True
                        print(
                            f"✅ Активность: два ESP → одна оценка "
                            f"(Δt≤{CSI_PAIR_MAX_DT_S * 1000:.0f} ms, {mode})"
                        )
                else:
                    activity = calib.predict(heights)
            else:
                activity = calib.predict(heights)

        presence_act = presence.activity()
        if presence_act is not None:
            activity = presence_act

        # Калибровочная запись
        calib_status = None
        if apply_effects:
            still, remaining = calib.add_sample(heights)
            if still:
                calib_status = {
                    "recording": True,
                    "label":     calib.rec_label,
                    "label_ru":  ACTIVITY_RU[calib.rec_label],
                    "remaining": round(remaining, 1),
                    "n_samples": calib.sample_counts,
                }
            elif not still and calib.rec_label:
                key = (calib.rec_label, calib.rec_end_time)
                if getattr(parse_ruview_packet, "_last_done", None) != key:
                    parse_ruview_packet._last_done = key
                    calib_status = {
                        "recording": False,
                        "done":      True,
                        "label":     calib.rec_label,
                        "n_samples": calib.sample_counts,
                    }

        # Калибровка конечностей (зарядка по зонам)
        limb_calib_status = None
        if apply_effects:
            lc_still, lc_rem, lc_done = limb_cal.add_sample(heights)
            if lc_still:
                limb_calib_status = {
                    "recording": True,
                    "limb":      limb_cal.rec_limb,
                    "limb_ru":   LIMB_RU[limb_cal.rec_limb],
                    "remaining": round(lc_rem, 1),
                    "counts":    limb_cal.counts,
                }
            elif lc_done:
                lk = (lc_done, limb_cal.rec_end_time)
                if getattr(parse_ruview_packet, "_last_limb_done", None) != lk:
                    parse_ruview_packet._last_limb_done = lk
                    limb_calib_status = {
                        "recording": False,
                        "done":      True,
                        "limb":      lc_done,
                        "limb_ru":   LIMB_RU.get(lc_done, lc_done),
                        "counts":    limb_cal.counts,
                    }

        # Энергия движения
        motion_energy = round(float(np.std(heights)) * 6.0, 3)

        # Жизненные показатели и карта комнаты — один буфер на сцену, только основной ESP
        vitals = _update_vitals(float(np.std(heights))) if apply_effects else None
        room_map = _update_room_map(heights) if apply_effects else None

        height_estimate_cm = _estimate_height_cm(skeleton)

        limb_match_scores = None
        limb_match_source = None
        if not limb_cal.recording and (USE_LIMB_CALIB_MATCH or USE_LIMB_POSE_PROTO):
            limb_match_scores, limb_match_source = limb_cal.limb_scores_for_frame(heights)

        if (
            apply_effects
            and skeleton
            and len(skeleton) >= 32
            and limb_match_scores is not None
        ):
            skeleton = apply_limb_pose_prototype_blend(skeleton, heights, limb_match_scores)

        return {
            "rssi":               rssi_raw,
            "heights":            heights,
            "skeleton":           skeleton,
            "ts":                 ts_val,
            "motion":             min(motion_energy, 1.0),
            "activity":           activity,
            "calib_status":       calib_status,
            "vitals":             vitals,
            "room_map":           room_map,
            "height_estimate_cm": height_estimate_cm,
            "limb_calib_status":  limb_calib_status,
            "limb_calib_counts":  limb_cal.counts,
            "limb_match_scores":  limb_match_scores,
            "limb_match_source":  limb_match_source,
            "has_limb_net":       limb_cal.has_limb_model,
            "has_limb_pose_proto": limb_cal.pose_proto_global is not None and len(limb_cal.pose_proto) >= 1,
            "udp_src":                 source_ip or "",
            "csi_primary":             apply_effects,
            "dual_activity_partner_ip": dual_activity_partner_ip,
        }
    except Exception as e:
        if not getattr(parse_ruview_packet, "_err_shown", False):
            print(f"⚠️  Ошибка разбора пакета: {e}  (первые байты: {data[:16].hex() if data else 'пусто'})")
            parse_ruview_packet._err_shown = True
        return None


# ══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET COMMAND HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def handle_cmd(cmd: dict, ws):
    action = cmd.get("cmd", "")

    if action == "calibrate_start":
        label    = cmd.get("label", "walking")
        n_people = max(0, min(20, int(cmd.get("n_people", 1))))
        duration = max(3, min(60, int(cmd.get("duration", 10))))
        calib.start_rec(label, n_people, duration)
        await ws.send(json.dumps({
            "type":     "calib_ack",
            "status":   "started",
            "label":    label,
            "n_people": n_people,
            "duration": duration,
        }))

    elif action == "calibrate_stop":
        calib.stop_rec()
        await ws.send(json.dumps({
            "type":      "calib_ack",
            "status":    "stopped",
            "n_samples": calib.sample_counts,
        }))

    elif action == "clear_class":
        label = cmd.get("label", "")
        calib.clear_class(label)
        await ws.send(json.dumps({
            "type":      "calib_ack",
            "status":    "cleared",
            "label":     label,
            "n_samples": calib.sample_counts,
        }))

    elif action == "train":
        ok, msg = calib.train()
        payload = json.dumps({
            "type":      "train_result",
            "success":   ok,
            "message":   msg,
            "n_samples": calib.sample_counts,
            "has_model": calib.has_model,
        })
        await asyncio.gather(
            *[c.send(payload) for c in connected_clients],
            return_exceptions=True,
        )

    elif action == "clear_all":
        for lbl in ACTIVITY_LABELS:
            calib.samples[lbl] = []
        calib.W_act = None
        calib.W_ppl = None
        if os.path.exists(ACT_MODEL_PATH):
            try:
                os.remove(ACT_MODEL_PATH)
            except Exception:
                pass
        print("🗑️  Все данные калибровки сброшены")
        payload = json.dumps({
            "type":      "calib_ack",
            "status":    "cleared_all",
            "n_samples": calib.sample_counts,
            "has_model": calib.has_model,
        })
        await asyncio.gather(
            *[c.send(payload) for c in connected_clients],
            return_exceptions=True,
        )

    elif action == "limb_calib_start":
        limb = cmd.get("limb", "")
        duration = max(5, min(180, int(cmd.get("duration", 15))))
        ok = limb_cal.start_rec(limb, duration)
        await ws.send(json.dumps({
            "type":       "limb_calib_ack",
            "status":     "started" if ok else "bad_limb",
            "limb":       limb,
            "duration":   duration,
            "counts":     limb_cal.counts,
            "has_limb_net": limb_cal.has_limb_model,
        }))

    elif action == "limb_calib_stop":
        limb_cal.stop_rec()
        await ws.send(json.dumps({
            "type":   "limb_calib_ack",
            "status": "stopped",
            "counts": limb_cal.counts,
            "has_limb_net": limb_cal.has_limb_model,
        }))

    elif action == "limb_calib_clear":
        scope = cmd.get("scope", "")
        if scope == "all":
            limb_cal.clear_all()
        elif scope in LIMB_KEYS:
            limb_cal.clear_limb(scope)
        await ws.send(json.dumps({
            "type":   "limb_calib_ack",
            "status": "cleared",
            "counts": limb_cal.counts,
            "has_limb_net": limb_cal.has_limb_model,
        }))

    elif action == "limb_calib_train":
        try:
            ok, msg = await asyncio.to_thread(limb_cal.train_limb_net)
        except Exception as e:
            traceback.print_exc()
            ok, msg = False, f"Исключение при обучении: {e}"
        await ws.send(json.dumps({
            "type":   "limb_calib_ack",
            "status": "trained" if ok else "train_fail",
            "message": msg,
            "counts": limb_cal.counts,
            "has_limb_net": limb_cal.has_limb_model,
        }))

    elif action == "camera_pose_batch":
        samples = cmd.get("samples", [])
        if not isinstance(samples, list):
            samples = []
        limb_zone = cmd.get("limb_zone")
        if not isinstance(limb_zone, str) or limb_zone not in LIMB_KEYS:
            limb_zone = None
        skip_camera_pose = bool(cmd.get("skip_camera_pose", False))

        n_cam = 0
        msg_cam = ""
        if not skip_camera_pose:
            n_cam, msg_cam = camera_pose_cal.add_batch(samples)
        else:
            msg_cam = "пары CSI+поза для МНК не добавлялись (режим «только зона»)"

        n_limb = 0
        if limb_zone is not None:
            n_limb = limb_cal.append_batch_for_limb(limb_zone, samples)

        parts = [msg_cam]
        if limb_zone is not None:
            parts.append(f"зона «{LIMB_RU.get(limb_zone, limb_zone)}»: +{n_limb} CSI")
        message = " · ".join(parts)

        await ws.send(json.dumps({
            "type":              "camera_pose_ack",
            "status":            "batch",
            "added":             n_cam,
            "received":          len(samples) if isinstance(samples, list) else 0,
            "limb_zone":         limb_zone,
            "limb_zone_added":   n_limb,
            "skip_camera_pose":  skip_camera_pose,
            "message":           message,
            "n_pairs":           camera_pose_cal.n_samples,
            "limb_calib_counts": limb_cal.counts,
        }))

    elif action == "camera_pose_train":
        try:
            ok, msg = await asyncio.to_thread(camera_pose_cal.train)
        except Exception as e:
            traceback.print_exc()
            ok, msg = False, str(e)
        await ws.send(json.dumps({
            "type":    "camera_pose_ack",
            "status":  "trained" if ok else "train_fail",
            "message": msg,
            "n_pairs": camera_pose_cal.n_samples,
        }))

    elif action == "camera_pose_clear":
        camera_pose_cal.clear()
        await ws.send(json.dumps({
            "type":    "camera_pose_ack",
            "status":  "cleared",
            "n_pairs": 0,
        }))

    elif action == "camera_pose_revert":
        ok = camera_pose_cal.revert_to_bundled_model()
        await ws.send(json.dumps({
            "type":    "camera_pose_ack",
            "status":  "reverted" if ok else "revert_fail",
            "message": "Снова веса из model-q*.bin" if ok else "Нет базовой матрицы",
        }))

    elif action == "presence_calibrate":
        seconds = max(30, min(300, int(cmd.get("seconds", 60))))
        delay = max(0, min(60, int(cmd.get("delay", 15))))
        presence.start_calibration(seconds, time.time(), delay=delay)
        print(f"🧭 Калибровка присутствия: через {delay} с, длительность {seconds} с — комната должна быть пуста")
        await _broadcast_json({"type": "presence_ack", "status": "started", "seconds": seconds, "delay": delay})

    elif action == "presence_rebaseline":
        if not presence.calibrated:
            await ws.send(json.dumps({"type": "presence_ack", "status": "need_calibration"}))
            return
        seconds = max(10, min(60, int(cmd.get("seconds", 15))))
        delay = max(0, min(60, int(cmd.get("delay", 15))))
        presence.start_calibration(seconds, time.time(), delay=delay, profile_only=True)
        print(f"🧭 Обновление эталона: через {delay} с, длительность {seconds} с — комната должна быть пуста")
        await _broadcast_json({"type": "presence_ack", "status": "rebaseline_started", "seconds": seconds, "delay": delay})

    elif action == "presence_cancel":
        presence.cancel_calibration()
        await _broadcast_json({"type": "presence_ack", "status": "cancelled"})

    elif action == "get_status":
        await ws.send(json.dumps({
            "type":               "status",
            "n_samples":          calib.sample_counts,
            "has_model":          calib.has_model,
            "limb_calib_counts":  limb_cal.counts,
            "has_limb_net":       limb_cal.has_limb_model,
            "camera_pose_pairs":  camera_pose_cal.n_samples,
            "has_camera_pose_w":  os.path.exists(CAMERA_POSE_W_PATH),
            "csi_primary_ip":     _sticky_primary_udp_ip,
            "csi_primary_env":    CSI_PRIMARY_IP or None,
            "presence_calibrated": presence.calibrated,
        }))
        if presence.last:
            await ws.send(_udp_payload_to_json(presence.last))


# ══════════════════════════════════════════════════════════════════════════════
# UDP LISTENER & WEBSOCKET SERVER
# ══════════════════════════════════════════════════════════════════════════════

async def _udp_loop(sock: socket.socket, port: int, loop: asyncio.AbstractEventLoop):
    """Цикл приёма для одного UDP-сокета."""
    global _last_log_no_ws, _last_log_json_err
    seen_src: set[str] = set()
    pkt_count = 0
    last_stat  = time.monotonic()

    while True:
        try:
            data, addr = await loop.sock_recvfrom(sock, 65535)
            everything = _udp_drain_all(sock, data, addr)
            now = time.monotonic()
            t_wall = time.time()
            pkt_count += len(everything)

            latest: dict[str, tuple[bytes, object]] = {}
            for d, a in everything:
                fr = csi_packet.parse(d)
                if fr is None:
                    continue
                presence.add(a[0], fr, t_wall)
                if not fr.from_node:
                    latest[a[0]] = (d, a)
            batch = list(latest.values())

            for data, addr in sorted(
                batch,
                key=lambda da: (1 if _should_apply_udp_side_effects(da[1][0]) else 0, da[1][0]),
            ):
                src = addr[0]
                if src not in seen_src:
                    seen_src.add(src)
                    print(f"📦 Первый пакет с порта {port} от {src}, "
                          f"{len(data)} байт, hex: {data[:12].hex()}")
                    if hasattr(parse_ruview_packet, "_err_shown"):
                        del parse_ruview_packet._err_shown

                parsed = parse_ruview_packet(data, source_ip=src)
                if not parsed:
                    continue
                if not connected_clients:
                    if now - _last_log_no_ws >= 15.0:
                        _last_log_no_ws = now
                        print(
                            "ℹ️  CSI по UDP разобран, но нет клиентов WebSocket :8765 — "
                            "откройте map3d.html / camera_calib.html (тот же хост, что и nn_pipeline)."
                        )
                    continue
                try:
                    msg = _udp_payload_to_json(parsed)
                except Exception as je:
                    if now - _last_log_json_err >= 5.0:
                        _last_log_json_err = now
                        print(f"⚠️  JSON для WebSocket (CSI): {je}")
                    continue
                clients = list(connected_clients)
                await asyncio.gather(
                    *[c.send(msg) for c in clients],
                    return_exceptions=True,
                )

            if now - last_stat >= 30:
                print(f"📊 UDP:{port} → {pkt_count} пакетов за 30с")
                pkt_count = 0
                last_stat = now
        except Exception as e:
            print(f"⚠️  UDP:{port} ошибка: {e}")
            await asyncio.sleep(0.05)


async def udp_listener():
    loop  = asyncio.get_running_loop()

    # Слушаем оба порта: 8080 (текущий) и 5555 (дефолт ESP / ruview_gateway)
    # чтобы поймать данные независимо от настроек ESP32
    ports = sorted({UDP_PORT, 5555})
    tasks = []

    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass  # Windows не поддерживает SO_REUSEPORT
        try:
            sock.bind((UDP_IP, port))
            sock.setblocking(False)
            tasks.append(_udp_loop(sock, port, loop))
            print(f"📡 UDP слушаем порт {port}")
        except Exception as e:
            print(f"⚠️  Не удалось занять UDP порт {port}: {e}")
            sock.close()

    if not tasks:
        print("❌ Не удалось открыть ни одного UDP порта!")
        return

    print(f"🚀 Сервер запущен!  WebSocket :{WS_PORT}")
    print(f"💡 Ожидаем пакеты от ESP32 (порт 8080 или 5555)")
    if CSI_PRIMARY_IP:
        print(f"💡 CSI_PRIMARY_IP={CSI_PRIMARY_IP} — калибровка только с этого адреса")
    else:
        print("💡 Несколько ESP: первый источник UDP станет «основным» для калибровки (env CSI_PRIMARY_IP=…)")
    await asyncio.gather(*tasks)


async def ws_handler(websocket):
    connected_clients.add(websocket)
    try:
        peer = getattr(websocket, "remote_address", None)
        print(
            f"✅ WebSocket подключён {peer!s} — клиентов: {len(connected_clients)} "
            "(CSI из UDP теперь уходит в браузер)",
            flush=True,
        )
    except Exception:
        print(f"✅ WebSocket подключён, клиентов: {len(connected_clients)}", flush=True)
    # Отправляем текущий статус при подключении
    try:
        await websocket.send(json.dumps({
            "type":               "status",
            "n_samples":          calib.sample_counts,
            "has_model":          calib.has_model,
            "limb_calib_counts":  limb_cal.counts,
            "has_limb_net":       limb_cal.has_limb_model,
            "camera_pose_pairs":  camera_pose_cal.n_samples,
            "has_camera_pose_w":  os.path.exists(CAMERA_POSE_W_PATH),
            "csi_primary_ip":     _sticky_primary_udp_ip,
            "csi_primary_env":    CSI_PRIMARY_IP or None,
        }))
    except Exception:
        pass

    try:
        async for raw in websocket:
            try:
                await handle_cmd(json.loads(raw), websocket)
            except Exception as e:
                print(f"⚠️  WS handle_cmd: {e}")
                traceback.print_exc()
    except (websockets.exceptions.ConnectionClosedOK,
            websockets.exceptions.ConnectionClosedError):
        pass
    finally:
        connected_clients.discard(websocket)


async def _broadcast_json(obj: dict) -> None:
    if not connected_clients:
        return
    msg = _udp_payload_to_json(obj)
    await asyncio.gather(*[c.send(msg) for c in list(connected_clients)], return_exceptions=True)


async def presence_loop():
    """Раз в секунду: решение детектора присутствия → все клиенты WebSocket и лог при смене."""
    prev_state = None
    while True:
        st = presence.tick(time.time())
        if st:
            if st["state"] != prev_state:
                print(f"🧍 Присутствие: {st['state_ru']}")
                prev_state = st["state"]
            if "calibration_result" in st:
                print(f"🧭 {st['calibration_result']}")
            if "rebaseline" in st:
                print(f"🧭 {st['rebaseline']}")
            await _broadcast_json(st)
        await asyncio.sleep(0.2)


async def main():
    # большие JSON-пакеты с сотнями пар CSI+поза
    async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT, max_size=16 * 1024 * 1024):
        await asyncio.gather(udp_listener(), presence_loop())


if __name__ == "__main__":
    _static = os.path.join(_HERE, "static")
    print(
        "\n💡 venv в Fish:  source venv/bin/activate.fish   (файл activate — для bash/zsh)\n"
        "\n💡 Интерфейс в браузере (отдельный терминал, порт любой свободный, напр. 8000):\n"
        f"     cd {_static} && python3 -m http.server 8000\n"
        "     → http://127.0.0.1:8000/map3d.html\n"
        "     → http://127.0.0.1:8000/camera_calib.html  (калибровка позы с камерой)\n"
        "   Не путайте с UDP 8080 выше: это порт для пакетов от ESP, не адрес страницы.\n",
        flush=True,
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Сервер остановлен.")
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(
                f"\n❌ Порт {WS_PORT} уже занят — скорее всего уже запущен nn_pipeline или ruview_gateway.\n"
                f"   Завершите процесс:  fuser -k {WS_PORT}/tcp\n"
                f"   Или:  pkill -f 'nn_pipeline.py'   затем снова запустите этот скрипт.\n",
                flush=True,
            )
            raise SystemExit(1) from e
        raise
