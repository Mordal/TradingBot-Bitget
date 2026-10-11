"""AGENT_POSITION_EXIT: beheert elke open positie afzonderlijk (take-profit, stop, trailing, tijd, edge-verval)."""
from __future__ import annotations

from typing import Optional

from ..config import AgentsCfg
from ..portfolio import Position
from .base import Advice, ExitAdvice

AGENT_ID = "AGENT_POSITION_EXIT"


class PositionExitAgent:
    def __init__(self, cfg: AgentsCfg):
        self.cfg = cfg

    def advise(self, pos: Position, bid: float, now_ms: int, edge: Optional[Advice] = None,
               emergency: bool = False) -> ExitAdvice:
        sym = pos.symbol
        entry = pos.avg_price
        if entry <= 0 or not (bid > 0):
            return ExitAdvice(sym, "HOLD", "no_price")
        pnl_bps = (bid / entry - 1) * 1e4
        meta = {"entry": entry, "bid": bid,
                "pnl_bps": pnl_bps, "highest": pos.highest}
        c = self.cfg
        if pos.sl_bps > 0 and pnl_bps <= -pos.sl_bps:
            return ExitAdvice(sym, "SELL_ALL", "stop_loss", urgent=True, meta=meta)
        if pos.tp_bps > 0 and pos.highest >= entry * (1 + c.trail_trigger * pos.tp_bps / 1e4):
            trail = pos.highest * (1 - 0.5 * pos.sl_bps / 1e4)
            if bid <= trail:
                return ExitAdvice(sym, "SELL_ALL", "trailing_stop", urgent=True, meta=meta)
        if pos.tp_bps > 0 and pnl_bps >= pos.tp_bps:
            return ExitAdvice(sym, "SELL_ALL", "take_profit", meta=meta)
        held_s = (now_ms - pos.opened_ms) / 1000
        if emergency:
            return ExitAdvice(sym, "SELL_ALL", "emergency", urgent=True, meta=meta)
        if pos.horizon_s > 0 and held_s >= c.max_hold_factor * pos.horizon_s:
            return ExitAdvice(sym, "SELL_ALL", "time_limit", meta=meta)
        # De instapkosten (de helft van de rondrit) zijn al betaald: alleen de resterende verwachting telt.
        remaining_edge = edge.expected_net_edge_bps + 0.5 * \
            float(edge.meta.get("cost_bps", 0.0)) if edge else 0.0
        if (edge is not None and held_s >= 120 and remaining_edge <= c.edge_decay_ev_bps
                and edge.meta.get("reason") != "warming_up"):
            return ExitAdvice(sym, "SELL_ALL", "edge_decay", meta=meta)
        return ExitAdvice(sym, "HOLD", "", meta=meta)
