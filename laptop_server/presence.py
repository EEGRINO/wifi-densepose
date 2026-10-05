"""
Детектор присутствия человека по CSI со всех линий (роутер → узел, узел → узел).

Признаки по каждой линии, раз в секунду:
  motion  — движение: коэффициент вариации амплитуд поднесущих во времени за 2 с
            (каждый кадр делится на свою среднюю амплитуду — компенсация АРУ);
  profile — сдвиг «формы» канала за 5 с от профиля пустой комнаты (RMS z-оценки по поднесущим).
            Ловит и неподвижного человека: тело меняет многолучёвку, даже когда не двигается.

Калибровка: запись пустой комнаты → по каждой линии профиль (μ, σ) и пороги
(перцентиль q значений в пустой комнате × запас).

Решение каждую секунду:
  движение — motion выше порога на любой линии;
  неподвижный человек — profile выше порога на линиях между узлами, либо на линиях от роутера,
  но с порогом в router_profile_factor раз выше: его 11n-ответы «плывут» сами по себе
  (подстройка передачи роутера) — в записи 2026-10-05 пустая комната через 6 мин давала
  на них ~2 порога, а сидящий рядом человек — 3–8;
  присутствие — ≥ vote_k срабатываний за последние vote_n секунд.
Если линий между узлами нет (одна плата), профиль берётся с линий от роутера.

Смена обстановки (стул отодвинули, сумку оставили) для профиля неотличима от неподвижного
человека — в записи 2026-10-05 после ухода «след» сдвинутого стула совпадал с сидящим на 92–95%.
Поэтому:
  - «значимое движение» — выше порога в strong_motion_r раз или выше порога sustained_motion_s
    секунд подряд: одиночные слабые всплески (шаги за дверью) не считаются;
  - «неподвижен» засчитывается только после того, как видели значимое движение (кто-то вошёл);
    после калибровки, автообновления и ≥ disarm_empty_s секунд «пусто» — снова нужно движение;
  - если «неподвижен» держится auto_rebaseline_s без значимого движения, считаем, что изменилась
    обстановка: профиль последних rebaseline_avg_s секунд становится новым эталоном;
  - быстрое обновление эталона по команде (только профиль, пороги остаются).

Значения по умолчанию подобраны по одной записи (кабинет, 2 платы, 2026-10-05):
ходьба 78% секунд, сидит неподвижно 100%, работает за ноутбуком 88%, ложных в пустой комнате 0%.
Запас у линий от роутера тонкий (дрейф ~2.0–2.2 при пороге 2.5) — нужны новые записи для проверки.
"""
from __future__ import annotations

import collections
import json
import os
from dataclasses import dataclass

import numpy as np

from csi_packet import USEFUL_SC, CsiFrame, link_name

STATE_RU = {
    "uncalibrated": "Нужна калибровка",
    "calibrating": "Калибровка",
    "empty": "Пусто",
    "moving": "Движение",
    "still": "Человек неподвижен",
}


@dataclass
class PresenceConfig:
    motion_win_s: float = 2.0
    profile_win_s: float = 5.0
    hop_s: float = 1.0
    quantile: float = 99.5        # перцентиль значений пустой комнаты
    motion_margin: float = 1.15   # порог = перцентиль × запас
    profile_margin: float = 1.8
    router_profile_factor: float = 2.5  # 0 — не использовать профиль линий от роутера
    vote_n: int = 5               # присутствие: ≥ vote_k срабатываний из последних vote_n секунд
    vote_k: int = 3
    moving_n: int = 3             # «движение»: ≥ moving_k из последних moving_n секунд
    moving_k: int = 2
    strong_motion_r: float = 1.3      # значимое движение: motion выше порога в столько раз…
    sustained_motion_s: int = 3       # …или выше порога столько секунд подряд
    auto_rebaseline_s: float = 300.0  # «неподвижен» без значимого движения дольше — обновить эталон (0 — выкл.)
    rebaseline_avg_s: float = 30.0    # по скольким последним секундам строить новый эталон
    disarm_empty_s: float = 10.0      # столько «пусто» подряд — «неподвижен» снова только после движения
    hint_quiet_s: float = 60.0        # «неподвижен» без движения дольше — подсказка в интерфейсе
    quick_min_windows: int = 5        # минимум окон для быстрого обновления эталона
    stale_s: float = 3.0          # линия без кадров дольше — не участвует
    min_frames_per_win: int = 20
    calib_min_windows: int = 15


class _Link:
    def __init__(self, name: str, from_node: bool) -> None:
        self.name = name
        self.from_node = from_node
        self.t: collections.deque = collections.deque()
        self.a: collections.deque = collections.deque()
        self.last_t = 0.0
        self.frames_since_hop = 0
        self.rate = 0.0

    def add(self, t: float, amp52: np.ndarray) -> None:
        self.t.append(t)
        self.a.append(amp52 / (amp52.mean() + 1e-6))
        self.last_t = t
        self.frames_since_hop += 1

    def trim(self, now: float, keep_s: float) -> None:
        while self.t and now - self.t[0] > keep_s:
            self.t.popleft()
            self.a.popleft()

    def window(self, now: float, win_s: float) -> np.ndarray | None:
        n = 0
        for tt in reversed(self.t):
            if now - tt > win_s:
                break
            n += 1
        if n == 0:
            return None
        return np.asarray(list(self.a)[-n:], dtype=np.float32)


class PresenceDetector:
    def __init__(self, calib_path: str | None = None, cfg: PresenceConfig | None = None) -> None:
        self.cfg = cfg or PresenceConfig()
        self.calib_path = calib_path
        self.links: dict[str, _Link] = {}
        self.calib: dict[str, dict] = {}
        self._next_hop = 0.0
        self._present_hist: collections.deque = collections.deque(maxlen=self.cfg.vote_n)
        self._moving_hist: collections.deque = collections.deque(maxlen=self.cfg.moving_n)
        # калибровка: None | {"start", "end", "profile_only", "motion": {link: [...]}, "prof": {link: [...]}}
        self._cal: dict | None = None
        self.last: dict | None = None
        # последние профили по линиям — для автообновления эталона
        self._recent: dict[str, collections.deque] = {}
        self._t0: float | None = None
        self._last_motion_t: float | None = None
        self._empty_since: float | None = None
        self._motion_run = 0
        # «вооружён»: движение уже видели, неподвижного человека можно засчитывать.
        # При старте с готовой калибровкой — да: в комнате уже может сидеть человек.
        self._armed = True
        if calib_path and os.path.exists(calib_path):
            self.load(calib_path)

    # ── вход ────────────────────────────────────────────────────────────────
    def add(self, rx_ip: str, fr: CsiFrame, t: float) -> None:
        name = link_name(rx_ip, fr)
        link = self.links.get(name)
        if link is None:
            link = self.links[name] = _Link(name, fr.from_node)
        link.add(t, fr.amp[USEFUL_SC].astype(np.float32))

    # ── калибровка ──────────────────────────────────────────────────────────
    def start_calibration(self, seconds: float, now: float, delay: float = 0.0, profile_only: bool = False) -> None:
        """
        Записать пустую комнату: через delay с (успеть выйти) в течение seconds с.
        profile_only — быстрое обновление эталона: меняется только профиль μ, пороги и σ остаются.
        """
        self._cal = {"start": now + delay, "end": now + delay + seconds, "profile_only": profile_only,
                     "motion": collections.defaultdict(list), "prof": collections.defaultdict(list)}

    def cancel_calibration(self) -> None:
        self._cal = None

    def _reset_after_baseline(self, now: float | None = None) -> None:
        """После нового эталона комната пуста по определению; таймер автообновления — с нуля."""
        self._present_hist.clear()
        self._moving_hist.clear()
        self._armed = False
        self._empty_since = None
        self._motion_run = 0
        if now is not None:
            self._last_motion_t = now
        if self.calib_path:
            self.save(self.calib_path)

    def _finish_calibration(self) -> tuple[bool, str]:
        cal, self._cal = self._cal, None
        if cal["profile_only"]:
            n = 0
            for name, profs in cal["prof"].items():
                if name in self.calib and len(profs) >= self.cfg.quick_min_windows:
                    self.calib[name]["mu"] = np.stack(profs).mean(0).tolist()
                    n += 1
            if not n:
                return False, "Эталон не обновлён: мало данных"
            self._reset_after_baseline(cal["end"])
            return True, f"Эталон пустой комнаты обновлён: {n} линий"
        q = self.cfg.quantile
        new: dict[str, dict] = {}
        for name, profs in cal["prof"].items():
            mot = cal["motion"].get(name, [])
            if len(profs) < self.cfg.calib_min_windows or len(mot) < self.cfg.calib_min_windows:
                continue
            P = np.stack(profs)
            mu, sd = P.mean(0), P.std(0) + 1e-4
            z = np.sqrt(np.mean(((P - mu) / sd) ** 2, axis=1))
            new[name] = {
                "from_node": self.links[name].from_node if name in self.links else False,
                "mu": mu.tolist(), "sd": sd.tolist(),
                "thr_motion": float(np.percentile(mot, q) * self.cfg.motion_margin),
                "thr_profile": float(np.percentile(z, q) * self.cfg.profile_margin),
                "n_windows": len(profs),
            }
        if not new:
            return False, "Калибровка не удалась: мало данных ни по одной линии"
        self.calib = new
        self._reset_after_baseline(cal["end"])
        return True, f"Калибровка готова: {len(new)} линий"

    def _auto_rebaseline(self, now: float) -> int:
        """Профиль последних rebaseline_avg_s секунд → новый эталон (обстановка изменилась)."""
        n = 0
        for name, dq in self._recent.items():
            vecs = [v for t, v in dq if now - t <= self.cfg.rebaseline_avg_s]
            if name in self.calib and len(vecs) >= self.cfg.quick_min_windows:
                self.calib[name]["mu"] = np.stack(vecs).mean(0).tolist()
                n += 1
        if n:
            self._reset_after_baseline(now)
        return n

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "links": self.calib}, f, ensure_ascii=False)

    def load(self, path: str) -> None:
        with open(path, encoding="utf-8") as f:
            self.calib = json.load(f).get("links", {})

    @property
    def calibrated(self) -> bool:
        return bool(self.calib)

    # ── решение ─────────────────────────────────────────────────────────────
    def tick(self, now: float) -> dict | None:
        """Вызывать часто; раз в hop_s возвращает новое состояние, иначе None."""
        if now < self._next_hop:
            return None
        first = self._next_hop == 0.0
        self._next_hop = now + self.cfg.hop_s
        if first:
            self._t0 = now
            return None
        cfg = self.cfg
        keep = max(cfg.motion_win_s, cfg.profile_win_s) + 1.0

        per_link: dict[str, dict] = {}
        for name, link in self.links.items():
            link.trim(now, keep)
            link.rate = link.frames_since_hop / cfg.hop_s
            link.frames_since_hop = 0
            info = {"node": link.from_node, "rate": round(link.rate, 1), "ok": now - link.last_t <= cfg.stale_s}
            per_link[name] = info
            if not info["ok"]:
                continue
            wm = link.window(now, cfg.motion_win_s)
            wp = link.window(now, cfg.profile_win_s)
            motion = prof_vec = None
            if wm is not None and len(wm) >= cfg.min_frames_per_win:
                motion = float(np.mean(wm.std(0) / (wm.mean(0) + 1e-6)))
            if wp is not None and len(wp) >= cfg.min_frames_per_win:
                prof_vec = wp.mean(0)
            info["motion"] = motion

            if self._cal and self._cal["start"] <= now <= self._cal["end"]:
                if motion is not None:
                    self._cal["motion"][name].append(motion)
                if prof_vec is not None:
                    self._cal["prof"][name].append(prof_vec)

            c = self.calib.get(name)
            if c and prof_vec is not None:
                dq = self._recent.setdefault(name, collections.deque(maxlen=int(cfg.rebaseline_avg_s / cfg.hop_s) + 2))
                dq.append((now, prof_vec))
            if c and motion is not None:
                info["motion_r"] = round(motion / c["thr_motion"], 3)
            if c and prof_vec is not None:
                z = float(np.sqrt(np.mean(((prof_vec - np.asarray(c["mu"])) / np.asarray(c["sd"])) ** 2)))
                info["profile_r"] = round(z / c["thr_profile"], 3)

        cal_msg = None
        if self._cal and now > self._cal["end"]:
            ok, cal_msg = self._finish_calibration()

        st: dict = {"type": "presence", "t": now, "links": per_link}
        if cal_msg:
            st["calibration_result"] = cal_msg
        if self._cal:
            phase = "waiting" if now < self._cal["start"] else "collecting"
            left = (self._cal["start"] if phase == "waiting" else self._cal["end"]) - now
            st.update(state="calibrating", calibration={"phase": phase, "left_s": round(max(0.0, left), 1),
                                                         "profile_only": self._cal["profile_only"]})
        elif not self.calibrated:
            st["state"] = "uncalibrated"
        else:
            active = [i for n, i in per_link.items() if i["ok"] and n in self.calib]
            have_nodes = any(i["node"] for i in active)
            rf = cfg.router_profile_factor

            def profile_fires(i: dict) -> bool:
                r = i.get("profile_r", 0)
                if i["node"] or not have_nodes:   # одна плата — линии от роутера единственные
                    return r > 1
                return rf > 0 and r > rf

            moving_raw = False
            still_raw = False
            motion_max = 0.0
            for i in active:
                m, p = i.get("motion_r", 0) > 1, profile_fires(i)
                i["fired"] = m or p
                moving_raw |= m
                still_raw |= p
                motion_max = max(motion_max, i.get("motion_r", 0))
            self._motion_run = self._motion_run + 1 if moving_raw else 0
            if motion_max > cfg.strong_motion_r or self._motion_run >= cfg.sustained_motion_s:
                self._last_motion_t = now
                self._armed = True
            self._present_hist.append(moving_raw or (still_raw and self._armed))
            self._moving_hist.append(moving_raw)
            present = sum(self._present_hist) >= cfg.vote_k
            moving = sum(self._moving_hist) >= cfg.moving_k
            state = ("moving" if moving else "still") if present else "empty"

            if state == "empty":
                if self._empty_since is None:
                    self._empty_since = now
                if now - self._empty_since >= cfg.disarm_empty_s:
                    self._armed = False
            else:
                self._empty_since = None

            quiet = now - (self._last_motion_t if self._last_motion_t is not None else (self._t0 or now))
            if state == "still":
                if cfg.auto_rebaseline_s > 0 and quiet >= cfg.auto_rebaseline_s:
                    n = self._auto_rebaseline(now)
                    if n:
                        state = "empty"
                        st["rebaseline"] = (f"Долго без движения — похоже, изменилась обстановка. "
                                            f"Эталон пустой комнаты обновлён автоматически ({n} линий)")
                else:
                    st["quiet_s"] = round(quiet)
                    if cfg.auto_rebaseline_s > 0:
                        st["auto_rebaseline_in_s"] = round(cfg.auto_rebaseline_s - quiet)
                    st["suggest_rebaseline"] = quiet >= cfg.hint_quiet_s
            st["state"] = state
            st["armed"] = self._armed
            st["profile_source"] = "узлы + роутер" if have_nodes else "роутер"
            st["active_links"] = len(active)
        st["state_ru"] = STATE_RU[st["state"]]
        self.last = st
        return st

    def activity(self) -> dict | None:
        """Состояние в формате поля activity для map3d (скелет: пусто / идёт / сидит)."""
        if not self.last or self.last["state"] not in ("empty", "moving", "still"):
            return None
        label = {"empty": "empty", "moving": "walking", "still": "sitting"}[self.last["state"]]
        probs = {k: 0.0 for k in ("empty", "sleeping", "sitting", "standing", "walking")}
        probs[label] = 1.0
        return {"label": label, "label_ru": self.last["state_ru"], "conf": 1.0, "probs": probs,
                "n_people": 0 if label == "empty" else 1, "source": "presence"}
