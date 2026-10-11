"""AGENT_ORDER_FLOW: volume-/volatiliteitsuitbarstingen met richting (vaste, uitlegbare regels)."""
from __future__ import annotations

import math
from typing import Callable, Optional

import numpy as np

from ..config import AgentsCfg
from ..marketdata import MarketState
from .base import Advice

AGENT_ID = "AGENT_ORDER_FLOW"
VERSION = "v1"


class OrderFlowAgent:
    def __init__(self, cfg: AgentsCfg, calibrate: Optional[Callable[[float], float]] = None):
        self.cfg = cfg
        self.calibrate = calibrate or (lambda x: x)

    def metrics(self, ms: MarketState, symbol: str, now_ms: int) -> Optional[dict]:
        st = ms.inst.get(symbol)
        if st is None or len(st.bars) < 35:
            return None
        bars = list(st.bars)
        qv = np.array([b.quote_vol for b in bars[-31:-1]], dtype=float)
        base_mean, base_sd = float(qv.mean()), float(qv.std())
        buy60, sell60, n60 = ms.flow(symbol, 60, now_ms)
        vol60 = buy60 + sell60
        vz = (vol60 - base_mean) / max(base_sd, 0.25 * base_mean, 1e-9)
        # Laatste punt = live prijs, zodat een uitbarsting binnen de lopende minuut direct meetelt.
        closes = np.array([b.close for b in bars[-30:]] +
                          [st.mid if st.mid == st.mid else bars[-1].close], dtype=float)
        r = np.diff(np.log(closes)) * 1e4
        s5, s30 = float(np.std(r[-5:])), float(np.std(r))
        vol_exp = s5 / s30 if s30 > 0 else 0.0
        buy, sell, n = ms.flow(symbol, self.cfg.flow_window_s, now_ms)
        imb = (buy - sell) / (buy + sell) if (buy +
                                              sell) > 0 and n >= 5 else 0.0
        book_imb = 0.0
        if st.book and st.book.bids and st.book.asks:
            b = sum(p * q for p, q in st.book.bids)
            a = sum(p * q for p, q in st.book.asks)
            book_imb = (b - a) / (a + b) if (a + b) > 0 else 0.0
        w = self.cfg.breakout_window_min
        hi = max(b.high for b in bars[-w:])
        lo = min(b.low for b in bars[-w:])
        mid = st.mid
        return {"volume_z": vz, "vol_expansion": vol_exp, "flow_imbalance": imb, "book_imbalance": book_imb,
                "breakout_up": bool(mid > hi), "breakdown": bool(mid < lo), "trades_in_window": n}

    def advise(self, ms: MarketState, symbol: str, now_ms: int) -> Advice:
        m = self.metrics(ms, symbol, now_ms)
        if m is None:
            return Advice.hold(AGENT_ID, VERSION, now_ms, symbol, "warming_up")
        c = self.cfg
        expansion = m["volume_z"] >= c.volume_z_min and m["vol_expansion"] >= c.vol_expansion_min
        advice, raw = "HOLD", 0.0
        if expansion and m["flow_imbalance"] >= c.flow_imbalance_min and m["breakout_up"] and m["book_imbalance"] > -0.3:
            advice = "BUY"
        elif expansion and m["flow_imbalance"] <= -c.flow_imbalance_min and m["breakdown"]:
            advice = "SELL"
        if advice != "HOLD":
            strength = min(2.0, m["volume_z"] - c.volume_z_min) * \
                0.05 + min(0.5, abs(m["flow_imbalance"])) * 0.2
            raw = min(0.8, 0.5 + strength)
        ratio = self.calibrate(raw) if advice != "HOLD" else 0.0
        return Advice(AGENT_ID, VERSION, now_ms, symbol, advice, ratio, 30 * 60, 0.0,
                      {**{k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in m.items()},
                       "raw_ratio": raw})
