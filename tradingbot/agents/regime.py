"""AGENT_REGIME_FILTER: is dit instrument en deze markt nu verhandelbaar? Beperkt of blokkeert alleen."""
from __future__ import annotations

from typing import Optional

from ..config import RiskCfg, Settings
from ..marketdata import MarketState
from .base import Regime, RegimeReport


def btc_shock(ms: MarketState, risk: RiskCfg, symbol: str = "BTCUSDT") -> bool:
    r = ms.returns_bps(symbol, max(1, risk.btc_shock_window_s // 60))
    return r == r and r <= -risk.btc_shock_bps


class RegimeAgent:
    def __init__(self, settings: Settings):
        self.s = settings

    def assess(self, ms: MarketState, symbol: str, now_ms: int, depth_ref: Optional[tuple[float, float]] = None,
               shock: bool = False) -> RegimeReport:
        u, r = self.s.universe, self.s.risk
        st = ms.inst.get(symbol)
        reasons: list[str] = []
        if st is None or st.book is None or not st.book.bids or not st.book.asks:
            return RegimeReport(symbol, Regime.BLOCKED, ["no_book"])
        if ms.age_ms(symbol) > r.max_data_age_ms or ms.book_age_ms(symbol) > r.max_data_age_ms * 3:
            reasons.append("stale_data")
        if st.book.spread_bps > u.max_spread_bps:
            reasons.append("spread")
        if st.book.best_bid >= st.book.best_ask:
            reasons.append("crossed_book")
        if shock:
            reasons.append("btc_shock")
        depth = min(depth_ref) if depth_ref else min(st.book.depth_usdt("bid", u.depth_band_bps),
                                                     st.book.depth_usdt("ask", u.depth_band_bps))
        if depth < 0.5 * u.min_depth_usdt:
            reasons.append("depth")
        if reasons:
            return RegimeReport(symbol, Regime.BLOCKED, reasons)
        if depth < 2 * u.min_depth_usdt:
            return RegimeReport(symbol, Regime.THIN, ["thin_depth"])
        s10, s120 = ms.sigma1m_bps(symbol, 10), ms.sigma1m_bps(symbol, 120)
        if s10 == s10 and s120 == s120 and s120 > 0 and s10 > 2.0 * s120:
            return RegimeReport(symbol, Regime.FAST, ["high_volatility"])
        return RegimeReport(symbol, Regime.TRADABLE)
