"""AGENT_EDGE_LGBM: kans op take-profit vóór stop-loss, per horizon, na kosten."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import Settings
from ..costs import roundtrip_cost_bps
from ..features import MIN_BARS, add_barrier_features, barrier_bps, build_features
from ..model import ModelBundle
from .base import Advice

AGENT_ID = "AGENT_EDGE_LGBM"


class EdgeAgent:
    def __init__(self, settings: Settings, bundle: ModelBundle | None):
        self.s = settings
        self.bundle = bundle

    @property
    def version(self) -> str:
        return self.bundle.version if self.bundle else "none"

    def evaluate(self, symbol: str, df: pd.DataFrame, btc: pd.DataFrame | None, eth: pd.DataFrame | None,
                 tick_bps: float, spread_bps: float, fee_bps: float, now_ms: int) -> Advice:
        if self.bundle is None:
            return Advice.hold(AGENT_ID, self.version, now_ms, symbol, "no_model")
        if len(df) < MIN_BARS:
            return Advice.hold(AGENT_ID, self.version, now_ms, symbol, "warming_up", bars=len(df))
        feat = build_features(df, btc, eth, tick_bps).iloc[[-1]]
        vol30 = float(feat["vol_30"].iloc[0])
        if not (vol30 > 0) or feat.drop(columns=["tick_bps"]).isna().any(axis=None):
            return Advice.hold(AGENT_ID, self.version, now_ms, symbol, "incomplete_features")
        ag, dec = self.s.agents, self.s.decision
        cost = roundtrip_cost_bps(
            spread_bps, fee_bps, fee_bps, dec.slippage_bps)
        per_h = {}
        for h in self.bundle.horizons:
            tp, sl, _ = barrier_bps(
                vol30, h, cost, ag.tp_cost_multiple, ag.sl_sigma_multiple)
            X = add_barrier_features(feat, tp, sl, cost)
            raw = float(self.bundle.predict_raw(X, h)[0])
            p = float(self.bundle.calibrators[h].predict(raw))
            per_h[h] = {"tp_bps": tp, "sl_bps": sl, "p_win_raw": raw, "p_win": p,
                        "ev_bps": p * tp - (1 - p) * sl - cost}
        best_h = max(per_h, key=lambda k: per_h[k]["ev_bps"])
        b = per_h[best_h]
        meta = {"tp_bps": b["tp_bps"], "sl_bps": b["sl_bps"], "p_win_raw": b["p_win_raw"], "cost_bps": cost,
                "horizon_min": best_h, "per_horizon": per_h, "vol30_bps": vol30, "gate_passed": self.bundle.passed_gate}
        if b["ev_bps"] <= 0:
            meta["reason"] = "no_edge"
            return Advice(AGENT_ID, self.version, now_ms, symbol, "HOLD", b["p_win"], best_h * 60, b["ev_bps"], meta)
        return Advice(AGENT_ID, self.version, now_ms, symbol, "BUY", b["p_win"], best_h * 60, b["ev_bps"], meta)
