"""SimGateway: lokale fill-simulator tegen live (of opgenomen) orderboeken, achter dezelfde interface als Bitget."""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Callable, Optional

from .config import Settings
from .exchange.base import (
    Balance, Instrument, OrderAck, OrderEvent, OrderRequest, TradingGateway)
from .marketdata import MarketState


@dataclass
class _SimOrder:
    req: OrderRequest
    order_id: str
    arrive_ms: int
    created_ms: int
    filled: float = 0.0
    quote: float = 0.0
    fee: float = 0.0
    state: str = "pending"      # pending | resting | done
    status: str = "live"
    frozen: float = 0.0
    nfills: int = 0


class SimGateway(TradingGateway):
    """Taker-fills tegen bied/laat met diepte (books5), vertraging, fees in ontvangen valuta (zoals Bitget)."""

    def __init__(self, settings: Settings, ms: MarketState, instruments: dict[str, Instrument],
                 clock_ms: Callable[[], int], start_usdt: float, latency_ms: int = 300, auto: bool = True):
        self.s, self.ms, self.inst, self.clock = settings, ms, instruments, clock_ms
        self.latency_ms = latency_ms
        self.auto = auto
        self.taker = settings.fees.taker_bps / 1e4
        self.maker = settings.fees.maker_bps / 1e4
        self.free: dict[str, float] = {"USDT": start_usdt}
        self.orders: dict[str, _SimOrder] = {}          # op client_oid
        self._ids = itertools.count(1)
        self._task: Optional[asyncio.Task] = None
        self._connected = True
        self.reject_next: Optional[str] = None           # voor tests

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        if self.auto:
            self._task = asyncio.create_task(self._loop(), name="sim-gateway")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            await self.process(self.clock())

    # ---------------------------------------------------------------- opdrachten
    async def place_order(self, req: OrderRequest) -> OrderAck:
        now = self.clock()
        if self.reject_next:
            code, self.reject_next = self.reject_next, None
            return OrderAck(req.client_oid, None, False, code, "simulated reject", self.latency_ms)
        inst = self.inst.get(req.symbol)
        st = self.ms.inst.get(req.symbol)
        if inst is None or st is None or st.book is None:
            return OrderAck(req.client_oid, None, False, "40309", "no book", self.latency_ms)
        if req.client_oid in self.orders:
            return OrderAck(req.client_oid, None, False, "40786", "duplicate clientOid", self.latency_ms)
        if req.price is None or req.size <= 0 or req.price * req.size < inst.min_trade_usdt:
            return OrderAck(req.client_oid, None, False, "45110", "below minimum", self.latency_ms)
        mid = st.book.mid
        band = inst.buy_band if req.side == "buy" else inst.sell_band
        if req.side == "buy" and req.price > mid * (1 + band) or req.side == "sell" and req.price < mid * (1 - band):
            return OrderAck(req.client_oid, None, False, "40808", "price outside band", self.latency_ms)
        frozen = 0.0
        if req.side == "buy":
            need = req.price * req.size * (1 + self.taker)
            if self.free.get("USDT", 0.0) < need:
                return OrderAck(req.client_oid, None, False, "43012", "insufficient balance", self.latency_ms)
            self.free["USDT"] -= need
            frozen = need
        else:
            if self.free.get(inst.base, 0.0) + 1e-12 < req.size:
                return OrderAck(req.client_oid, None, False, "43012", "insufficient balance", self.latency_ms)
            self.free[inst.base] -= req.size
            frozen = req.size
        oid = f"sim{next(self._ids)}"
        self.orders[req.client_oid] = _SimOrder(
            req, oid, now + self.latency_ms, now, frozen=frozen)
        return OrderAck(req.client_oid, oid, True, "00000", "", float(self.latency_ms))

    async def cancel_order(self, symbol: str, client_oid: str | None = None, order_id: str | None = None) -> bool:
        o = self._find(client_oid, order_id)
        if o is None or o.state == "done":
            return False
        await self._complete(o, "cancelled")
        return True

    async def cancel_all(self, symbol: str) -> None:
        for o in [o for o in self.orders.values() if o.req.symbol == symbol and o.state != "done"]:
            await self._complete(o, "cancelled")

    def _find(self, client_oid: Optional[str], order_id: Optional[str]) -> Optional[_SimOrder]:
        if client_oid:
            return self.orders.get(client_oid)
        return next((o for o in self.orders.values() if o.order_id == order_id), None)

    async def get_order(self, symbol: str, client_oid: str | None = None, order_id: str | None = None):
        o = self._find(client_oid, order_id)
        return self._event(o, None) if o else None

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderEvent]:
        return [self._event(o, None) for o in self.orders.values()
                if o.state != "done" and (symbol is None or o.req.symbol == symbol)]

    async def get_balances(self) -> list[Balance]:
        out: dict[str, Balance] = {c: Balance(
            c, v, 0.0) for c, v in self.free.items()}
        for o in self.orders.values():
            if o.state == "done":
                continue
            coin = "USDT" if o.req.side == "buy" else self.inst[o.req.symbol].base
            b = out.setdefault(coin, Balance(coin, 0.0, 0.0))
            b.frozen += o.frozen
        return list(out.values())

    # --------------------------------------------------------------- verwerking
    async def process(self, now_ms: int) -> None:
        """Verwerkt aangekomen orders tegen het orderboek van dat moment en rustende limietorders."""
        for o in [o for o in self.orders.values() if o.state != "done"]:
            st = self.ms.inst.get(o.req.symbol)
            if st is None or st.book is None:
                continue
            if o.state == "pending":
                if now_ms < o.arrive_ms:
                    continue
                await self._arrive(o, st.book, now_ms)
            elif o.state == "resting":
                await self._match_resting(o, st.book, now_ms)

    async def _arrive(self, o: _SimOrder, book, now_ms: int) -> None:
        r = o.req
        taker_side = book.asks if r.side == "buy" else book.bids
        crossing = [(p, q) for p, q in taker_side if (
            p <= r.price if r.side == "buy" else p >= r.price)]
        if r.force == "post_only" and crossing:
            await self._complete(o, "cancelled")
            return
        if crossing and r.force != "post_only":
            remaining, fills = r.size, []
            for p, q in crossing:
                take = min(q, remaining)
                if take > 0:
                    fills.append((p, take))
                    remaining -= take
                if remaining <= 1e-12:
                    break
            got = sum(q for _p, q in fills)
            if r.force == "fok" and remaining > 1e-12:
                await self._complete(o, "cancelled")
                return
            if got > 0:
                price = sum(p * q for p, q in fills) / got
                await self._fill(o, price, got, self.taker, now_ms)
        if o.filled >= r.size - 1e-12:
            await self._complete(o, "filled")
        elif r.force in ("ioc", "fok"):
            await self._complete(o, "cancelled")
        else:
            o.state = "resting"
            await self._emit(o, "live", None)

    async def _match_resting(self, o: _SimOrder, book, now_ms: int) -> None:
        r = o.req
        levels = book.bids if r.side == "sell" else book.asks
        avail = sum(q for p, q in levels if (
            p >= r.price if r.side == "sell" else p <= r.price))
        take = min(avail, r.size - o.filled)
        if take > 1e-12:
            await self._fill(o, r.price, take, self.maker, now_ms)
            if o.filled >= r.size - 1e-12:
                await self._complete(o, "filled")

    async def _fill(self, o: _SimOrder, price: float, base: float, rate: float, now_ms: int) -> None:
        r, inst = o.req, self.inst[o.req.symbol]
        if r.side == "buy":
            fee = base * rate                                  # fee in gekochte munt
            cost = price * base
            o.frozen -= cost
            self.free[inst.base] = self.free.get(inst.base, 0.0) + base - fee
            fee_coin = inst.base
        else:
            fee = price * base * rate                          # fee in USDT
            self.free["USDT"] = self.free.get("USDT", 0.0) + price * base - fee
            o.frozen -= base
            fee_coin = "USDT"
        o.quote += price * base
        o.filled += base
        o.fee += fee
        o.nfills += 1
        await self._emit(o, "partially_filled" if o.filled < r.size - 1e-12 else "filled",
                         (f"{o.order_id}-{o.nfills}", price, base, fee, fee_coin))

    async def _complete(self, o: _SimOrder, status: str) -> None:
        if o.state == "done":
            return
        o.state, o.status = "done", status
        if o.req.side == "buy":
            self.free["USDT"] = self.free.get(
                "USDT", 0.0) + max(0.0, o.frozen)   # ongebruikt deel vrijgeven
        else:
            self.free[self.inst[o.req.symbol].base] = self.free.get(
                self.inst[o.req.symbol].base, 0.0) + max(0.0, o.frozen)
        o.frozen = 0.0
        await self._emit(o, status, None)

    def _event(self, o: _SimOrder, fill: Optional[tuple]) -> OrderEvent:
        status = o.status if o.state == "done" else (
            "live" if o.filled == 0 else "partially_filled")
        ev = OrderEvent(symbol=o.req.symbol, client_oid=o.req.client_oid, order_id=o.order_id, status=status,
                        side=o.req.side, size=o.req.size, price=o.req.price or 0.0, acc_base=o.filled,
                        avg_price=o.quote / o.filled if o.filled else 0.0, ts_ms=self.clock())
        if fill:
            ev.fill_id, ev.fill_price, ev.fill_base, ev.fee, ev.fee_coin = fill
        else:
            ev.fee = o.fee
            ev.fee_coin = self.inst[o.req.symbol].base if o.req.side == "buy" else "USDT"
        return ev

    async def _emit(self, o: _SimOrder, status: str, fill: Optional[tuple]) -> None:
        if self.on_order:
            ev = self._event(o, fill)
            if status in ("filled", "cancelled") and fill is not None:
                ev.status = status
            elif fill is not None:
                ev.status = "partially_filled"
            await self.on_order(ev)
