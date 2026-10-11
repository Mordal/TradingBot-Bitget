"""Beslissingsagent: vaste, uitlegbare EV-regel (Crypto Bot.md 6)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from .agents.base import Advice, ExitAdvice, Regime, RegimeReport
from .agents.edge_lgbm import AGENT_ID as EDGE_ID
from .agents.order_flow import AGENT_ID as FLOW_ID
from .config import Settings
from .costs import roundtrip_cost_bps
from .portfolio import Position


@dataclass
class Decision:
    ts_ms: int
    instrument: str
    action: str                      # BUY | SELL | HOLD
    notional_usdt: float = 0.0
    order_style: str = "limit_ioc"
    limit_price: float = 0.0
    tp_bps: float = 0.0
    sl_bps: float = 0.0
    ev_bps: float = 0.0
    p_win: float = 0.0
    horizon_s: int = 0
    break_even_bps: float = 0.0
    fraction: float = 1.0            # bij SELL: deel van de positie
    urgent: bool = False
    reason: str = ""
    agent_contributions: dict = field(default_factory=dict)
    agents: dict = field(default_factory=dict)   # versies, voor de audit
    reasoning_log: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class DecisionAgent:
    def __init__(self, settings: Settings):
        self.s = settings

    def decide_entry(self, symbol: str, now_ms: int, advices: list[Advice], regime: RegimeReport,
                     position: Optional[Position], bid: float, ask: float, spread_bps: float,
                     fee_bps: float, usdt_available: float) -> Decision:
        d = self.s.decision

        def hold(why: str, **kw) -> Decision:
            return Decision(now_ms, symbol, "HOLD", reason=why, **kw)

        if regime.regime == Regime.BLOCKED:
            return hold("regime_blocked:" + ",".join(regime.reasons))
        if position is not None:
            return hold("position_open")
        edge = next((a for a in advices if a.agent_id == EDGE_ID), None)
        flow = next((a for a in advices if a.agent_id == FLOW_ID), None)
        if edge is None or edge.advice != "BUY":
            return hold("no_edge")
        contrib = {a.agent_id: f"{a.advice}/{a.ratio:.2f}" for a in advices}
        if flow is not None and flow.advice == "SELL":
            return hold("order_flow_contradicts", agent_contributions=contrib)
        tp, sl = float(edge.meta["tp_bps"]), float(edge.meta["sl_bps"])
        p = float(edge.ratio)
        cost = roundtrip_cost_bps(spread_bps, fee_bps, fee_bps, d.slippage_bps)
        ev = p * tp - (1 - p) * sl - cost
        if ev < d.min_ev_bps or p < d.min_p_win:
            return hold(f"ev_below_threshold(ev={ev:.1f},p={p:.2f})", ev_bps=ev, p_win=p, break_even_bps=cost,
                        agent_contributions=contrib)
        size = d.base_order_usdt * min(1.0, ev / d.ev_ref_bps)
        if regime.regime in (Regime.THIN, Regime.FAST):
            size *= d.thin_size_factor
        if flow is None or flow.advice != "BUY":
            size *= d.thin_size_factor        # geen bevestiging door de order-flow-agent
        size = min(size, d.max_order_usdt, usdt_available)
        if size <= 0:
            return hold("no_usdt_available", ev_bps=ev, p_win=p, break_even_bps=cost)
        limit = ask * (1 + 2 * d.slippage_bps / 1e4)
        return Decision(
            now_ms, symbol, "BUY", notional_usdt=size, order_style="limit_ioc", limit_price=limit, tp_bps=tp, sl_bps=sl,
            ev_bps=ev, p_win=p, horizon_s=edge.horizon_seconds, break_even_bps=cost, agent_contributions=contrib,
            agents={a.agent_id: a.model_version for a in advices},
            reasoning_log=f"EV {ev:.1f} bps >= {d.min_ev_bps}, P_win {p:.2f}, regime {regime.regime.value}")

    def decide_exit(self, pos: Position, adv: ExitAdvice, bid: float, now_ms: int) -> Decision:
        d, r = self.s.decision, self.s.risk
        slip = r.max_slippage_bps if adv.urgent else 2 * d.slippage_bps
        limit = bid * (1 - slip / 1e4)
        frac = 1.0 if adv.action == "SELL_ALL" else max(
            0.0, min(1.0, adv.fraction))
        return Decision(now_ms, pos.symbol, "SELL", notional_usdt=pos.qty * frac * bid, limit_price=limit,
                        fraction=frac, urgent=adv.urgent, reason=adv.reason, agents=dict(
                            pos.agents),
                        reasoning_log=f"exit {adv.reason}, pnl {adv.meta.get('pnl_bps', 0):.1f} bps")
