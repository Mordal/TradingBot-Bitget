"""Feedback: outcomes per horizon (netto na kosten), kalibratie en lifecycle (shadow/active)."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from .agents.base import Advice
from .config import Settings
from .marketdata import MIN_MS, MarketState
from .storage import Storage


def evaluate_path(entry: float, highs: np.ndarray, lows: np.ndarray, last_close: float, cost_bps: float,
                  tp_bps: Optional[float], sl_bps: Optional[float], direction: str = "BUY") -> tuple[float, float]:
    """(netto bps, max adverse bps) van een advies over de bars na het adviesmoment.

    BUY met barrières: take-profit eerst = +TP, stop eerst = -SL (zelfde bar: stop eerst), anders tijduitstap.
    SELL telt als juist als de koers daalt (hypothetisch short; geen kosten voor 'niet kopen').
    """
    if len(highs) == 0 or entry <= 0:
        return float("nan"), 0.0
    adverse = float((lows.min() / entry - 1) *
                    1e4) if direction == "BUY" else float((highs.max() / entry - 1) * 1e4)
    if direction == "SELL":
        return -float((last_close / entry - 1) * 1e4), adverse
    if tp_bps and sl_bps:
        tp_px, sl_px = entry * (1 + tp_bps / 1e4), entry * (1 - sl_bps / 1e4)
        for h, l in zip(highs, lows):
            if l <= sl_px:
                return -sl_bps - cost_bps, adverse
            if h >= tp_px:
                return tp_bps - cost_bps, adverse
    return float((last_close / entry - 1) * 1e4) - cost_bps, adverse


class BucketCalibrator:
    """Kalibreert een ruwe score (0..1) naar de gemeten hit-rate per emmer van 0,1; minstens 30 voorbeelden."""

    def __init__(self, min_samples: int = 30):
        self.min_samples = min_samples
        self.n: dict[int, int] = defaultdict(int)
        self.hits: dict[int, int] = defaultdict(int)

    def add(self, raw: float, correct: bool) -> None:
        b = min(9, max(0, int(raw * 10)))
        self.n[b] += 1
        self.hits[b] += int(correct)

    def __call__(self, raw: float) -> float:
        b = min(9, max(0, int(raw * 10)))
        if self.n[b] >= self.min_samples:
            return self.hits[b] / self.n[b]
        return raw


@dataclass
class _Pending:
    advice_id: int
    advice: Advice
    entry: float
    cost_bps: float
    done_h: set


class Feedback:
    def __init__(self, settings: Settings, db: Storage, ms: MarketState, clock_ms: Callable[[], int]):
        self.s, self.db, self.ms, self.clock = settings, db, ms, clock_ms
        self.pending: list[_Pending] = []
        self.flow_cal = BucketCalibrator()
        # agent_id -> active | shadow
        self.status: dict[str, str] = {}
        self.window: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=settings.model.shadow_demote_n))
        self.last_hold_logged: dict[str, int] = {}

    # ----------------------------------------------------------------- vastleggen
    def record(self, a: Advice, entry_mid: float, cost_bps: float) -> Optional[int]:
        if a.advice == "HOLD":
            # HOLD alleen sporadisch loggen om de database klein te houden.
            if a.meta.get("reason") != "no_edge":
                return None
            if a.timestamp_ms - self.last_hold_logged.get(a.instrument, 0) < 300_000:
                return None
            self.last_hold_logged[a.instrument] = a.timestamp_ms
        aid = self.db.insert("advices", {
            "ts_ms": a.timestamp_ms, "agent_id": a.agent_id, "model_version": a.model_version,
            "instrument": a.instrument, "advice": a.advice, "ratio": a.ratio, "horizon_s": a.horizon_seconds,
            "edge_bps": a.expected_net_edge_bps, "entry_mid": entry_mid, "meta": a.meta})
        if a.advice != "HOLD":
            self.pending.append(_Pending(aid, a, entry_mid, cost_bps, set()))
        return aid

    # ----------------------------------------------------------------- beoordelen
    def evaluate_due(self, now_ms: int) -> int:
        horizons = self.s.agents.horizons_min
        n_done = 0
        keep: list[_Pending] = []
        for p in self.pending:
            st = self.ms.inst.get(p.advice.instrument)
            adv_min = p.advice.timestamp_ms // MIN_MS * MIN_MS
            for h in horizons:
                if h in p.done_h or st is None:
                    continue
                end = adv_min + h * MIN_MS
                if now_ms < end + MIN_MS:
                    continue
                bars = [b for b in st.bars if adv_min < b.ts <= end]
                if len(bars) < max(1, h // 2):
                    # te weinig data (bv. herstart): overslaan
                    p.done_h.add(h)
                    continue
                highs, lows = np.array([b.high for b in bars]), np.array(
                    [b.low for b in bars])
                tp = p.advice.meta.get(
                    "tp_bps") if p.advice.agent_id == "AGENT_EDGE_LGBM" else None
                sl = p.advice.meta.get("sl_bps") if tp else None
                if tp and h * 60 != p.advice.horizon_seconds:
                    tp = sl = None            # barrières gelden alleen op de horizon van het advies
                net, adverse = evaluate_path(
                    p.entry, highs, lows, bars[-1].close, p.cost_bps, tp, sl, p.advice.advice)
                p.done_h.add(h)
                if net != net:
                    continue
                correct = net > 0
                self.db.insert_ignore("outcomes", {
                    "advice_id": p.advice_id, "agent_id": p.advice.agent_id, "instrument": p.advice.instrument,
                    "horizon_s": h * 60, "advice": p.advice.advice, "ratio": p.advice.ratio, "net_bps": net,
                    "correct": int(correct), "max_adverse_bps": adverse, "evaluated_ms": now_ms})
                n_done += 1
                if h * 60 == p.advice.horizon_seconds:
                    self._learn(p.advice, net, correct)
            if len(p.done_h) < len(horizons):
                keep.append(p)
        self.pending = keep
        return n_done

    def _learn(self, a: Advice, net: float, correct: bool) -> None:
        if a.agent_id == "AGENT_ORDER_FLOW":
            raw = a.meta.get("raw_ratio", a.ratio)
            self.flow_cal.add(raw, correct)
        if a.advice == "BUY":
            self.window[a.agent_id + "|" +
                        a.model_version].append((correct, net))
            self._update_lifecycle(a.agent_id, a.model_version)

    # ------------------------------------------------------------------ lifecycle
    def set_initial(self, agent_id: str, status: str) -> None:
        self.status[agent_id] = status

    def get_status(self, agent_id: str) -> str:
        return self.status.get(agent_id, "active")

    def _update_lifecycle(self, agent_id: str, version: str) -> None:
        m = self.s.model
        w = list(self.window[agent_id + "|" + version])
        n = len(w)
        if n == 0:
            return
        hit = sum(1 for c, _ in w if c) / n
        mean = sum(x for _, x in w) / n
        cur = self.get_status(agent_id)
        new = cur
        if agent_id == "AGENT_EDGE_LGBM":
            if cur == "shadow" and n >= m.shadow_activate_n and hit >= m.shadow_activate_hit and mean > 0:
                new = "active"
            elif cur == "active" and n >= m.shadow_demote_n and (hit < m.shadow_demote_hit or mean < 0):
                new = "shadow"
        self.status[agent_id] = new
        self.db.insert("agent_stats", {"agent_id": agent_id, "model_version": version, "status": new, "n": n,
                                       "hits": int(hit * n), "mean_net_bps": mean,
                                       "updated_ms": self.clock()}, replace=True)
