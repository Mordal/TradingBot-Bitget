"""Uitvoering: orderstatusmachine, idempotente fill-verwerking, take-profit, prioriteit voor exits, reconciliatie."""
from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from .config import Settings
from .decision import Decision
from .exchange.base import (Balance, ExchangeError, Instrument,
                            OrderAck, OrderEvent, OrderRequest, TradingGateway)
from .portfolio import Portfolio
from .ratelimit import TokenBucket
from .risk import OrderIntent, RiskManager
from .storage import Storage

log = logging.getLogger(__name__)

TERMINAL = {"FILLED", "CANCELLED", "REJECTED"}
PRIORITY = {"stop": 0, "exit": 0, "cancel": 0, "take_profit": 1, "entry": 2}
FEE_BUFFER = 0.003   # extra gereserveerde USDT voor fees


@dataclass
class OrderRecord:
    client_oid: str
    symbol: str
    side: str
    purpose: str
    size: float
    price: float
    force: str
    state: str = "NEW"           # NEW SENT ACKED PARTIAL FILLED CANCELLED REJECTED UNKNOWN
    order_id: Optional[str] = None
    filled_base: float = 0.0
    filled_quote: float = 0.0
    fee: float = 0.0
    created_ms: int = 0
    acked_ms: int = 0
    first_fill_ms: int = 0
    done_ms: int = 0
    ack_latency_ms: float = 0.0
    reject_code: str = ""
    meta: dict = field(default_factory=dict)
    done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def avg_price(self) -> float:
        return self.filled_quote / self.filled_base if self.filled_base > 0 else 0.0

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL


class ExecutionEngine:
    def __init__(self, settings: Settings, gateway: TradingGateway, portfolio: Portfolio, risk: RiskManager,
                 storage: Storage, instruments: dict[str, Instrument], clock_ms: Callable[[], int]):
        self.s, self.gw, self.pf, self.risk, self.db = settings, gateway, portfolio, risk, storage
        self.inst, self.clock = instruments, clock_ms
        self.orders: dict[str, OrderRecord] = {}
        self.exit_attempts: dict[str, int] = {}
        self.bucket = TokenBucket(settings.rate.orders_per_s)
        self._q: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._counter = itertools.count(1)
        self._worker: Optional[asyncio.Task] = None
        self._tasks: set[asyncio.Task] = set()
        self.alerts: list[str] = []
        self.unknown_delays = (0.4, 1.0, 2.0)
        self.ack_latencies: list[float] = []
        self.fill_latencies: list[float] = []
        self.last_balances: list[Balance] = []
        gateway.on_order = self.on_order_event
        gateway.on_balance = self.on_balance

    # ------------------------------------------------------------------ levenscyclus
    def start(self) -> None:
        self._worker = asyncio.create_task(self._drain(), name="order-worker")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        for t in list(self._tasks):
            t.cancel()

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def new_oid(self) -> str:
        return f"tb{self.clock():x}{next(self._counter):x}"

    # --------------------------------------------------------------------- indienen
    def in_flight_symbols(self) -> set[str]:
        """Symbolen met een lopende entry of exit (een resting take-profit telt niet)."""
        return {o.symbol for o in self.orders.values() if not o.terminal and o.purpose != "take_profit"}

    def submit(self, intent: OrderIntent, decision: Optional[Decision] = None) -> OrderRecord:
        oid = self.new_oid()
        now = self.clock()
        rec = OrderRecord(oid, intent.symbol, intent.side, intent.purpose, intent.qty, intent.price, intent.force,
                          created_ms=now)
        if decision is not None:
            rec.meta = {"tp_bps": decision.tp_bps, "sl_bps": decision.sl_bps, "ev_bps": decision.ev_bps,
                        "p_win": decision.p_win, "horizon_s": decision.horizon_s, "agents": decision.agents,
                        "entry_mid": decision.limit_price}
        if intent.side == "buy":
            self.pf.reserve(oid, intent.notional * (1 + FEE_BUFFER))
            self.risk.note_buy(now)
        self.orders[oid] = rec
        self.db.insert("orders", {"client_oid": oid, "instrument": rec.symbol, "side": rec.side, "purpose": rec.purpose,
                                  "order_type": "limit", "force": rec.force, "size": rec.size, "price": rec.price,
                                  "status": "NEW", "created_ms": now}, replace=True)
        prio = PRIORITY.get(intent.purpose, 2)
        self._q.put_nowait((prio, next(self._seq), rec))
        return rec

    async def _drain(self) -> None:
        while True:
            _prio, _n, rec = await self._q.get()
            await self.bucket.acquire()
            self._spawn(self._send(rec))

    async def _send(self, rec: OrderRecord) -> None:
        req = OrderRequest(rec.symbol, rec.side, "limit", rec.size,
                           rec.price, rec.force, rec.client_oid, rec.purpose)
        rec.state = "SENT"
        try:
            ack = await self.gw.place_order(req)
        except (asyncio.TimeoutError, ConnectionError, ExchangeError) as e:
            # Na verzenden onbekend of de order de beurs bereikte: nooit blind opnieuw sturen, eerst opvragen.
            rec.state = "UNKNOWN"
            log.warning("order %s %s: uitkomst onbekend (%s)",
                        rec.symbol, rec.client_oid, type(e).__name__)
            await self._resolve_unknown(rec)
            return
        self._on_ack(rec, ack)

    def _on_ack(self, rec: OrderRecord, ack: OrderAck) -> None:
        rec.ack_latency_ms = ack.latency_ms
        if ack.ok:
            if rec.state in ("SENT", "UNKNOWN", "NEW"):
                rec.state = "ACKED"
            rec.order_id = ack.order_id or rec.order_id
            rec.acked_ms = self.clock()
            self.ack_latencies.append(ack.latency_ms)
            self.risk.note_ok()
            self.db.update("orders", "client_oid", rec.client_oid,
                           {"status": rec.state, "order_id": rec.order_id, "acked_ms": rec.acked_ms,
                            "ack_latency_ms": ack.latency_ms})
        else:
            self._finish(rec, "REJECTED")
            rec.reject_code = ack.code
            self.db.update("orders", "client_oid", rec.client_oid, {
                           "detail": f"{ack.code}: {ack.msg}"})
            self.db.insert("rate_events", {"ts_ms": self.clock(), "kind": "order_reject",
                                           "detail": f"{rec.symbol} {ack.code} {ack.msg}"})
            if ack.code in ("429", "43011", "40014", "40015"):
                self.risk.trip("rate_limit")
            else:
                self.risk.note_reject()
            self.alerts.append(
                f"order rejected {rec.symbol} {ack.code} {ack.msg}")

    async def _resolve_unknown(self, rec: OrderRecord) -> None:
        for delay in self.unknown_delays:
            await asyncio.sleep(delay)
            try:
                ev = await self.gw.get_order(rec.symbol, client_oid=rec.client_oid)
            except ExchangeError:
                continue
            if ev is not None:
                rec.order_id = ev.order_id or rec.order_id
                rec.state = "ACKED"
                await self.on_order_event(ev, from_rest=True)
                return
        # De order heeft de beurs niet bereikt (of is vervallen zonder spoor).
        self._finish(rec, "REJECTED")
        self.alerts.append(
            f"order {rec.client_oid} niet gevonden na time-out; als niet uitgevoerd afgesloten")

    # ---------------------------------------------------------------------- events
    async def on_balance(self, bals: list[Balance]) -> None:
        by = {b.coin: b for b in self.last_balances}
        by.update({b.coin: b for b in bals})
        self.last_balances = list(by.values())

    async def on_order_event(self, ev: OrderEvent, from_rest: bool = False) -> None:
        rec = self.orders.get(ev.client_oid)
        if rec is None:
            return                                     # niet van deze bot
        if ev.order_id:
            rec.order_id = ev.order_id
        now = self.clock()
        if ev.fill_id and ev.fill_base > 0:
            self._apply_fill(rec, ev.fill_id, ev.fill_price or ev.avg_price, ev.fill_base, ev.fee, ev.fee_coin,
                             ev.ts_ms or now)
        elif ev.acc_base > rec.filled_base + 1e-12:
            # Gevulde hoeveelheid groter dan wat wij boekten (gemiste fill-event): synthetische fill uit de order.
            miss = ev.acc_base - rec.filled_base
            fee_part = ev.fee * \
                (miss / ev.acc_base) if ev.acc_base > 0 else 0.0
            self._apply_fill(rec, f"sync-{ev.acc_base:.10f}", ev.avg_price or rec.price, miss, fee_part,
                             ev.fee_coin or (self.inst[rec.symbol].base if rec.side == "buy" else "USDT"), now)
        status = ev.status
        if rec.state in ("NEW", "SENT", "UNKNOWN") and status in ("live", "partially_filled", "filled", "new", "partial-fill"):
            rec.state = "ACKED"
            rec.acked_ms = rec.acked_ms or now
        if status in ("partially_filled", "partial-fill") and not rec.terminal:
            rec.state = "PARTIAL"
        elif status == "filled":
            self._finish(rec, "FILLED")
        elif status in ("cancelled", "canceled"):
            self._finish(rec, "CANCELLED")

    def _apply_fill(self, rec: OrderRecord, fill_id: str, price: float, base: float, fee: float, fee_coin: str,
                    ts_ms: int) -> None:
        inst = self.inst[rec.symbol]
        applied = self.pf.apply_fill(rec.symbol, inst.base, rec.side, price, base, fee, fee_coin, ts_ms,
                                     rec.client_oid, fill_id)
        if not applied:
            return
        rec.filled_base += base
        rec.filled_quote += price * base
        rec.fee += fee
        if not rec.first_fill_ms:
            rec.first_fill_ms = ts_ms
            lat = max(0, self.clock() - rec.created_ms)
            self.fill_latencies.append(lat)
            self.db.update("orders", "client_oid", rec.client_oid, {
                           "fill_latency_ms": lat})
        if rec.side == "buy" and rec.client_oid in self.pf.reserved:
            self.pf.reserved[rec.client_oid] = max(
                0.0, self.pf.reserved[rec.client_oid] - price * base)
        pos = self.pf.positions.get(rec.symbol)
        if rec.side == "buy" and pos is not None and rec.purpose == "entry":
            m = rec.meta
            pos.tp_bps, pos.sl_bps = m.get(
                "tp_bps", pos.tp_bps), m.get("sl_bps", pos.sl_bps)
            pos.horizon_s, pos.agents = m.get(
                "horizon_s", pos.horizon_s), m.get("agents", pos.agents)
            pos.entry_ev_bps, pos.entry_p_win = m.get(
                "ev_bps", 0.0), m.get("p_win", 0.0)
            pos.entry_mid = pos.entry_mid or price
        self.db.insert_ignore("fills", {"fill_id": fill_id, "client_oid": rec.client_oid, "instrument": rec.symbol,
                                        "side": rec.side, "price": price, "base": base, "fee": fee,
                                        "fee_coin": fee_coin, "ts_ms": ts_ms})

    def _finish(self, rec: OrderRecord, state: str) -> None:
        if rec.terminal and rec.state == state:
            return
        rec.state = state
        rec.done_ms = self.clock()
        self.pf.release(rec.client_oid)
        rec.done.set()
        self.db.update("orders", "client_oid", rec.client_oid,
                       {"status": state, "filled_base": rec.filled_base, "avg_price": rec.avg_price, "fee": rec.fee,
                        "done_ms": rec.done_ms})
        if rec.purpose in ("exit", "stop") and rec.side == "sell":
            if rec.filled_base <= 0:
                self.exit_attempts[rec.symbol] = self.exit_attempts.get(
                    rec.symbol, 0) + 1
            else:
                self.exit_attempts.pop(rec.symbol, None)
        if rec.purpose == "entry" and rec.side == "buy" and rec.filled_base > 0:
            self._spawn(self.place_take_profit(rec.symbol))

    # ----------------------------------------------------------------- take-profit
    def active_tp(self, symbol: str) -> Optional[OrderRecord]:
        pos = self.pf.positions.get(symbol)
        rec = self.orders.get(
            pos.tp_client_oid) if pos and pos.tp_client_oid else None
        return rec if rec and not rec.terminal else None

    async def place_take_profit(self, symbol: str) -> Optional[OrderRecord]:
        pos = self.pf.positions.get(symbol)
        if pos is None or pos.tp_bps <= 0 or self.active_tp(symbol) is not None:
            return None
        inst = self.inst[symbol]
        if pos.tp_bps > inst.sell_band * 1e4 * 0.9:
            # buiten de prijsband: de exit-agent bewaakt de take-profit
            return None
        qty = inst.round_qty(pos.qty)
        price = inst.ceil_price(pos.avg_price * (1 + pos.tp_bps / 1e4))
        if qty * price < max(inst.min_trade_usdt, 1.0) or qty < inst.min_trade_base:
            return None
        rec = self.submit(OrderIntent(symbol, "sell", qty,
                          price, qty * price, "gtc", "take_profit"))
        pos.tp_client_oid = rec.client_oid
        pos.exit_reason = pos.exit_reason or "take_profit"
        return rec

    async def cancel_take_profit(self, symbol: str, timeout_s: float = 3.0) -> bool:
        """Annuleert de wachtende take-profit zodat de hoeveelheid vrij komt. True = niets meer actief."""
        rec = self.active_tp(symbol)
        if rec is None:
            return True
        # nog niet bevestigd: even laten landen
        if rec.state in ("NEW", "SENT"):
            try:
                await asyncio.wait_for(self._wait_acked(rec), timeout_s)
            except asyncio.TimeoutError:
                return False
        await self.gw.cancel_order(symbol, client_oid=rec.client_oid, order_id=rec.order_id)
        try:
            await asyncio.wait_for(rec.done.wait(), timeout_s)
        except asyncio.TimeoutError:
            ev = await self.gw.get_order(symbol, client_oid=rec.client_oid)
            if ev is not None:
                await self.on_order_event(ev, from_rest=True)
        pos = self.pf.positions.get(symbol)
        if pos is not None and rec.terminal:
            pos.tp_client_oid = None
        return rec.terminal

    async def _wait_acked(self, rec: OrderRecord) -> None:
        while rec.state in ("NEW", "SENT"):
            await asyncio.sleep(0.05)

    # --------------------------------------------------------------- reconciliatie
    async def reconcile(self) -> list[str]:
        """Vergelijkt orders en saldi met de beurs; zet de circuit breaker bij onverklaarde afwijkingen."""
        issues: list[str] = []
        for rec in [o for o in self.orders.values() if o.state == "UNKNOWN"]:
            await self._resolve_unknown(rec)
        open_ex = {o.client_oid: o for o in await self.gw.get_open_orders()}
        for rec in [o for o in self.orders.values() if not o.terminal and o.state not in ("NEW", "SENT")]:
            if rec.client_oid not in open_ex:
                ev = await self.gw.get_order(rec.symbol, client_oid=rec.client_oid)
                if ev is not None:
                    await self.on_order_event(ev, from_rest=True)
                if not rec.terminal:
                    self._finish(rec, "CANCELLED")
        for oid, ev in open_ex.items():
            if oid.startswith("tb") and oid not in self.orders:
                issues.append(f"onbekende open order {ev.symbol} {oid}")
                await self.gw.cancel_order(ev.symbol, client_oid=oid)
        balances = await self.gw.get_balances()
        lots = {i.base: i.lot for i in self.inst.values()}
        issues += self.pf.reconcile(balances, lots)
        if issues:
            self.risk.trip("reconcile: " + "; ".join(issues[:3]))
            self.alerts.extend(issues)
        elif self.risk.breaker_reason and self.risk.breaker_reason.startswith("reconcile"):
            self.risk.clear()
        return issues
