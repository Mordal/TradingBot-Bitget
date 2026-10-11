"""BitgetGateway: orders via private WS (REST als terugval), status via events + REST."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from ...config import Credentials, Settings
from ..base import (Balance, ExchangeError, OrderAck,
                    OrderEvent, OrderRequest, TradingGateway)
from .rest import BitgetRest
from .ws_private import PrivateFeed

log = logging.getLogger(__name__)


class BitgetGateway(TradingGateway):
    def __init__(self, settings: Settings, creds: Credentials, rest: BitgetRest):
        self.s, self.rest = settings, rest
        self.feed = PrivateFeed(settings, creds, rest.clock)
        self.feed.on_order = self._order
        self.feed.on_balance = self._balance
        self.use_ws_orders = True

    async def _order(self, ev: OrderEvent) -> None:
        if self.on_order:
            await self.on_order(ev)

    async def _balance(self, bals: list[Balance]) -> None:
        if self.on_balance:
            await self.on_balance(bals)

    async def start(self) -> None:
        await self.rest.start()
        self.feed.start()
        if not await self.feed.wait_ready(10):
            log.warning(
                "private ws niet klaar bij opstart; REST-terugval actief")

    async def stop(self) -> None:
        await self.feed.stop()

    @property
    def connected(self) -> bool:
        return self.feed.connected and self.feed.logged_in.is_set()

    async def place_order(self, req: OrderRequest) -> OrderAck:
        timeout = self.s.rate.order_timeout_s
        if self.use_ws_orders and self.connected:
            # Na verzenden is de uitkomst bij een time-out onbekend: de aanroeper reconcilieert op clientOid.
            return await self.feed.place_order(req, timeout)
        t0 = time.perf_counter()
        try:
            d = await asyncio.wait_for(self.rest.place_order(req), timeout)
        except ExchangeError as e:
            return OrderAck(req.client_oid, None, False, e.code or "", str(e), (time.perf_counter() - t0) * 1000)
        return OrderAck(req.client_oid, str(d.get("orderId", "")), True, "00000", "", (time.perf_counter() - t0) * 1000)

    async def cancel_order(self, symbol: str, client_oid: str | None = None, order_id: str | None = None) -> bool:
        try:
            await self.rest.cancel_order(symbol, client_oid, order_id)
            return True
        except ExchangeError as e:
            log.info("cancel %s mislukt: %s", symbol, e)
            return False

    async def cancel_all(self, symbol: str) -> None:
        await self.rest.cancel_all(symbol)

    async def get_order(self, symbol: str, client_oid: str | None = None,
                        order_id: str | None = None) -> Optional[OrderEvent]:
        return await self.rest.get_order(symbol, client_oid, order_id)

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderEvent]:
        return await self.rest.get_open_orders(symbol)

    async def get_balances(self) -> list[Balance]:
        return await self.rest.get_balances()
