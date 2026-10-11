"""Risk & Rules Layer: deterministische regels die geen agent kan overschrijven (Crypto Bot.md 7)."""
from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Settings
from .decision import Decision
from .exchange.base import Instrument, OrderBook
from .portfolio import Portfolio


@dataclass
class OrderIntent:
    symbol: str
    side: str
    qty: float
    price: float
    notional: float
    force: str = "ioc"
    purpose: str = "entry"


@dataclass
class RiskContext:
    now_ms: int
    portfolio: Portfolio
    marks: dict[str, float]
    inst: Instrument
    book: Optional[OrderBook]
    data_age_ms: float
    # in het actieve universum (nieuwe BUY's toegestaan)
    active: bool
    # symbolen met een lopende entry/exit-intentie
    in_flight: set[str] = field(default_factory=set)
    # diepte binnen 10 bps (beperkende kant), uit het universum
    depth_ref_usdt: Optional[float] = None


@dataclass
class RiskResult:
    approved: bool
    reason: str
    order: Optional[OrderIntent] = None
    notes: list[str] = field(default_factory=list)


class RiskManager:
    def __init__(self, settings: Settings):
        self.s = settings
        self.r = settings.risk
        self.kill_path = settings.path(settings.bot.kill_file)
        self.buy_times: deque[int] = deque()
        self.cooldown_until: dict[str, int] = {}
        self.reject_streak = 0
        # gezet door de uitvoeringslaag/reconciliatie
        self.breaker_reason: Optional[str] = None
        self.kill_switch = False

    # ----------------------------------------------------------------- toestand
    def killed(self) -> bool:
        return self.kill_switch or self.kill_path.exists()

    def trip(self, reason: str) -> None:
        self.breaker_reason = reason

    def clear(self) -> None:
        self.breaker_reason = None
        self.reject_streak = 0

    def note_reject(self) -> None:
        self.reject_streak += 1
        if self.reject_streak >= self.r.max_reject_streak:
            self.trip(f"{self.reject_streak} opeenvolgende order-rejects")

    def note_ok(self) -> None:
        self.reject_streak = 0

    def note_buy(self, now_ms: int) -> None:
        self.buy_times.append(now_ms)

    def note_stop(self, symbol: str, now_ms: int) -> None:
        self.cooldown_until[symbol] = now_ms + self.r.stop_cooldown_s * 1000

    # ------------------------------------------------------------------- entry
    def check_entry(self, d: Decision, c: RiskContext) -> RiskResult:
        r, dec = self.r, self.s.decision
        sym, inst, pf = d.instrument, c.inst, c.portfolio
        if self.killed():
            return RiskResult(False, "kill_switch")
        if inst.status != "online" or inst.quote != self.s.universe.quote or inst.is_tokenized_stock:
            return RiskResult(False, "instrument_not_allowed")
        if not c.active:
            return RiskResult(False, "not_in_active_universe")
        if self.breaker_reason:
            return RiskResult(False, f"circuit_breaker:{self.breaker_reason}")
        if c.book is None or not c.book.asks or not c.book.bids:
            return RiskResult(False, "no_book")
        if c.data_age_ms > r.max_data_age_ms:
            return RiskResult(False, "stale_data")
        if c.now_ms < self.cooldown_until.get(sym, 0):
            return RiskResult(False, "stop_cooldown")
        if sym in pf.positions or sym in c.in_flight:
            return RiskResult(False, "position_or_order_active")
        equity = pf.equity(c.marks)
        pf.mark_equity(c.now_ms, equity)
        dd, day_loss = pf.drawdown(equity), pf.day_loss(equity)
        if dd >= r.dd_hard:
            return RiskResult(False, f"drawdown_hard({dd:.3f})")
        if day_loss >= r.daily_loss_pct:
            return RiskResult(False, f"daily_loss({day_loss:.3f})")
        while self.buy_times and c.now_ms - self.buy_times[0] > 3600_000:
            self.buy_times.popleft()
        if len(self.buy_times) >= r.max_buys_per_hour:
            return RiskResult(False, "tempo_hour")
        if sum(1 for t in self.buy_times if c.now_ms - t <= 60_000) >= r.max_buys_per_min:
            return RiskResult(False, "tempo_minute")
        if len(pf.positions) + len(c.in_flight) >= r.max_open_positions:
            return RiskResult(False, "max_open_positions")

        notes: list[str] = []
        notional = d.notional_usdt
        if dd >= r.dd_soft:
            notional *= 0.5
            notes.append("dd_soft_half_size")
        held = pf.position_value(sym, c.marks.get(sym, 0.0))
        caps = {
            "max_order_usdt": dec.max_order_usdt,
            "max_position_usdt": r.max_position_usdt - held,
            "max_asset_share": r.max_asset_share * equity - held,
            "total_exposure": r.max_total_exposure_pct * equity - pf.exposure(c.marks),
            "usdt_available": pf.available(r.min_usdt_reserve),
        }
        depth = c.depth_ref_usdt
        if depth is None:
            depth = min(c.book.depth_usdt("ask", 10),
                        c.book.depth_usdt("bid", 10))
        caps["depth_fraction"] = r.max_order_depth_fraction * depth
        for name, cap in caps.items():
            if cap < notional:
                notes.append(f"capped_by_{name}")
                notional = cap
        if notional <= 0:
            return RiskResult(False, "no_capacity", notes=notes)

        best_ask = c.book.best_ask
        cap_px = best_ask * (1 + r.max_slippage_bps / 1e4)
        price = inst.ceil_price(min(d.limit_price or cap_px, cap_px))
        if price < best_ask:
            price = inst.ceil_price(best_ask)
        mark = c.marks.get(sym, c.book.mid)
        if price > mark * (1 + inst.buy_band):
            return RiskResult(False, "outside_price_band", notes=notes)
        # Beschikbare diepte tot aan de limietprijs.
        visible = sum(p * q for p, q in c.book.asks if p <= price)
        if visible < notional:
            notes.append("capped_by_visible_depth")
            notional = visible * 0.9
        qty = inst.round_qty(notional / price)
        eff = qty * price
        if qty <= 0 or eff < max(inst.min_trade_usdt, 1.0) or qty < inst.min_trade_base:
            return RiskResult(False, "below_minimum", notes=notes)
        return RiskResult(True, "ok", OrderIntent(sym, "buy", qty, price, eff, "ioc", "entry"), notes)

    # -------------------------------------------------------------------- exit
    def check_exit(self, d: Decision, c: RiskContext, available_qty: Optional[float] = None) -> RiskResult:
        """Verkopen blijft altijd mogelijk (ook bij kill-switch, circuit breaker en EXIT_ONLY)."""
        inst, pf = c.inst, c.portfolio
        pos = pf.positions.get(d.instrument)
        if pos is None:
            return RiskResult(False, "no_position")
        if c.book is None or not c.book.bids:
            return RiskResult(False, "no_book")
        have = pos.qty if available_qty is None else min(
            pos.qty, available_qty)
        qty = inst.round_qty(have * d.fraction)
        best_bid = c.book.best_bid
        price = inst.floor_price(
            min(d.limit_price or best_bid, best_bid * (1 + 1e-9)))
        price = max(price, inst.tick)
        mark = c.marks.get(d.instrument, c.book.mid)
        if price < mark * (1 - inst.sell_band):
            price = inst.ceil_price(mark * (1 - inst.sell_band))
        eff = qty * price
        if qty <= 0 or eff < max(inst.min_trade_usdt, 1.0) or qty < inst.min_trade_base:
            return RiskResult(False, "dust_position")
        return RiskResult(True, "ok", OrderIntent(d.instrument, "sell", qty, price, eff, "ioc",
                                                  "stop" if d.urgent else "exit"))
