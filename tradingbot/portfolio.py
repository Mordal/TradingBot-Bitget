"""Boekhouding: onafhankelijke posities, USDT-allocator, P&L uitsluitend uit fills."""
from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .exchange.base import Balance

QUOTE = "USDT"


@dataclass
class Position:
    symbol: str
    base: str
    qty: float = 0.0               # netto, na fee in basismunt
    # totale instapkosten van de resterende qty (incl. fee)
    cost_usdt: float = 0.0
    opened_ms: int = 0
    highest: float = 0.0
    tp_bps: float = 0.0
    sl_bps: float = 0.0
    horizon_s: int = 0
    entry_ev_bps: float = 0.0
    entry_p_win: float = 0.0
    entry_mid: float = 0.0
    fees_usdt: float = 0.0
    realized_usdt: float = 0.0
    # totale instapkosten van de hele trade (voor P&L in bps)
    invested_usdt: float = 0.0
    bought_qty: float = 0.0
    tp_client_oid: Optional[str] = None
    agents: dict = field(default_factory=dict)
    exit_reason: str = ""

    @property
    def avg_price(self) -> float:
        return self.cost_usdt / self.qty if self.qty > 0 else 0.0


@dataclass
class ClosedTrade:
    symbol: str
    opened_ms: int
    closed_ms: int
    qty: float
    entry_price: float
    exit_price: float
    pnl_usdt: float
    pnl_bps: float
    reason: str
    agents: dict


class Portfolio:
    def __init__(self, cash_usdt: float, baseline: dict[str, float] | None = None, capital_cap: float = 0.0):
        # ledger-USDT (inclusief gereserveerd)
        self.cash = cash_usdt
        self.capital_cap = capital_cap
        # holdings die niet van de bot zijn
        self.baseline = dict(baseline or {})
        self.baseline_usdt = cash_usdt
        self.positions: dict[str, Position] = {}
        self.reserved: dict[str, float] = {}     # client_oid -> USDT
        self.seen_fills: set[tuple[str, str]] = set()
        self.closed: list[ClosedTrade] = []
        # symbool -> lotgrootte (stof-drempel)
        self.lot: dict[str, float] = {}
        self.on_closed: Optional[Callable[[ClosedTrade], None]] = None
        self._eq_hist: deque[tuple[int, float]] = deque()
        self._day_key: Optional[int] = None
        self.day_start_equity: float = 0.0
        self.realized_total = 0.0

    # --------------------------------------------------------------- allocator
    @property
    def reserved_total(self) -> float:
        return sum(self.reserved.values())

    def budget(self) -> float:
        """USDT dat de bot mag inzetten (begrensd door capital_cap)."""
        return min(self.cash, self.capital_cap) if self.capital_cap > 0 else self.cash

    def available(self, min_reserve: float) -> float:
        return max(0.0, self.budget() - self.reserved_total - min_reserve)

    def reserve(self, client_oid: str, usdt: float) -> None:
        self.reserved[client_oid] = usdt

    def release(self, client_oid: str) -> None:
        self.reserved.pop(client_oid, None)

    # -------------------------------------------------------------------- fills
    def apply_fill(self, symbol: str, base_coin: str, side: str, price: float, base: float, fee: float,
                   fee_coin: str, ts_ms: int, client_oid: str, fill_id: str) -> bool:
        """Verwerkt één fill idempotent. Geeft False als de fill al verwerkt was."""
        key = (client_oid, fill_id)
        if key in self.seen_fills:
            return False
        self.seen_fills.add(key)
        pos = self.positions.get(symbol)
        gross = price * base
        if side == "buy":
            if pos is None:
                pos = self.positions[symbol] = Position(
                    symbol, base_coin, opened_ms=ts_ms, highest=price)
            # fee in gekochte munt (Bitget): netto minder in bezit
            if fee_coin == base_coin:
                net_qty, fee_usdt, cost_add, cash_out = base - fee, fee * price, gross, gross
            elif fee_coin == QUOTE:         # fee in USDT
                net_qty, fee_usdt, cost_add, cash_out = base, fee, gross + fee, gross + fee
            # andere feemunt (bv. BGB): niet in het ledger opgenomen
            else:
                net_qty, fee_usdt, cost_add, cash_out = base, 0.0, gross, gross
            pos.qty += net_qty
            pos.cost_usdt += cost_add
            pos.invested_usdt += cost_add
            pos.bought_qty += net_qty
            pos.fees_usdt += fee_usdt
            pos.highest = max(pos.highest, price)
            self.cash -= cash_out
        else:
            if pos is None:
                # geen botpositie (bv. vooraf bestaande holding): negeren
                return True
            fee_usdt = fee if fee_coin in (QUOTE, "") else 0.0
            proceeds = gross - fee_usdt
            sold = min(base, pos.qty)
            cost_part = pos.avg_price * sold
            pnl = proceeds - cost_part
            pos.realized_usdt += pnl
            pos.fees_usdt += fee_usdt
            pos.cost_usdt -= cost_part
            pos.qty -= sold
            self.cash += proceeds
            self.realized_total += pnl
            if pos.qty < max(self.lot.get(symbol, 0.0), 1e-12):
                if pos.qty > 0:  # stofrest onder één lot is niet te verkopen: bij de vooraf bestaande holdings
                    self.baseline[pos.base] = self.baseline.get(
                        pos.base, 0.0) + pos.qty
                self._close(pos, price, ts_ms, proceeds, cost_part)
        return True

    def _close(self, pos: Position, exit_price: float, ts_ms: int, last_proceeds: float, last_cost: float) -> None:
        entry_price = pos.invested_usdt / pos.bought_qty if pos.bought_qty > 0 else 0.0
        total_pnl = pos.realized_usdt
        ct = ClosedTrade(pos.symbol, pos.opened_ms, ts_ms, pos.bought_qty, entry_price, exit_price, total_pnl,
                         total_pnl / max(pos.invested_usdt, 1e-9) * 1e4, pos.exit_reason, pos.agents)
        self.closed.append(ct)
        del self.positions[pos.symbol]
        if self.on_closed:
            self.on_closed(ct)

    # ------------------------------------------------------------------ waarde
    def exposure(self, marks: dict[str, float]) -> float:
        return sum(p.qty * marks.get(s, p.avg_price) for s, p in self.positions.items())

    def position_value(self, symbol: str, mark: float) -> float:
        p = self.positions.get(symbol)
        return p.qty * mark if p else 0.0

    def equity(self, marks: dict[str, float]) -> float:
        return self.cash + self.exposure(marks)

    def unrealized(self, marks: dict[str, float]) -> float:
        return sum(p.qty * marks.get(s, p.avg_price) - p.cost_usdt for s, p in self.positions.items())

    # --------------------------------------------------------------- drawdown
    def mark_equity(self, ts_ms: int, equity: float) -> None:
        self._eq_hist.append((ts_ms, equity))
        cutoff = ts_ms - 24 * 3600_000
        while self._eq_hist and self._eq_hist[0][0] < cutoff:
            self._eq_hist.popleft()
        day = ts_ms // 86_400_000
        if self._day_key != day:
            self._day_key, self.day_start_equity = day, equity

    def drawdown(self, equity: float) -> float:
        peak = max((e for _t, e in self._eq_hist), default=equity)
        peak = max(peak, equity)
        return 1 - equity / peak if peak > 0 else 0.0

    def day_loss(self, equity: float) -> float:
        return 1 - equity / self.day_start_equity if self.day_start_equity > 0 else 0.0

    # ------------------------------------------------------------ reconciliatie
    def reconcile(self, balances: list[Balance], lot_by_coin: dict[str, float] | None = None) -> list[str]:
        """Vergelijkt het ledger met de beurssaldi; geeft beschrijvingen van afwijkingen."""
        issues = []
        by_coin = {b.coin: b.total for b in balances}
        usdt_ex = by_coin.get(QUOTE, 0.0)
        tol = max(0.5, 0.001 * max(usdt_ex, self.cash))
        if abs(usdt_ex - self.cash) > tol:
            issues.append(
                f"USDT: ledger {self.cash:.4f} vs beurs {usdt_ex:.4f}")
        coins = {p.base for p in self.positions.values()}
        for coin in coins:
            expected = self.baseline.get(
                coin, 0.0) + sum(p.qty for p in self.positions.values() if p.base == coin)
            have = by_coin.get(coin, 0.0)
            lot = (lot_by_coin or {}).get(coin, 0.0)
            if abs(have - expected) > max(lot, 0.005 * expected, 1e-9):
                issues.append(
                    f"{coin}: ledger {expected:.8f} vs beurs {have:.8f}")
        return issues

    # ------------------------------------------------------------- persistentie
    def dump(self) -> str:
        return json.dumps({
            "cash": self.cash, "baseline": self.baseline, "baseline_usdt": self.baseline_usdt,
            "positions": {s: asdict(p) for s, p in self.positions.items()},
            "realized_total": self.realized_total})

    def load(self, text: str) -> None:
        d = json.loads(text)
        self.cash = d["cash"]
        self.baseline = d.get("baseline", {})
        self.baseline_usdt = d.get("baseline_usdt", self.cash)
        self.realized_total = d.get("realized_total", 0.0)
        self.positions = {s: Position(**p) for s, p in d["positions"].items()}
