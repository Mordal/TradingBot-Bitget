"""Private Bitget-WebSocket: login, orders/account-events en orderplaatsing via op=trade."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Awaitable, Callable, Optional

from websockets.asyncio.client import connect

from ...config import Credentials, Settings
from ...ratelimit import Backoff
from ..base import Balance, OrderAck, OrderEvent, OrderRequest
from .rest import _f, _fmt
from .signing import Clock, ws_login_signature

log = logging.getLogger(__name__)

OrderCb = Callable[[OrderEvent], Awaitable[None]]
BalanceCb = Callable[[list[Balance]], Awaitable[None]]


def parse_order_event(d: dict) -> OrderEvent:
    """Eén item uit het `orders`-kanaal."""
    fee, fee_coin = 0.0, ""
    for fd in d.get("feeDetail") or []:
        fee += abs(_f(fd.get("fee")))
        fee_coin = fd.get("feeCoin", fee_coin)
    if not fee and d.get("fillFee"):
        fee, fee_coin = abs(_f(d["fillFee"])), d.get("fillFeeCoin", "")
    tid = d.get("tradeId")
    return OrderEvent(
        symbol=d.get("instId", ""), client_oid=d.get("clientOid", ""), order_id=str(d.get("orderId", "")),
        status=d.get("status", ""), side=d.get("side", ""), size=_f(d.get("size")), price=_f(d.get("price")),
        acc_base=_f(d.get("accBaseVolume")), avg_price=_f(d.get("priceAvg")),
        fill_id=str(tid) if tid else None, fill_price=_f(d.get("fillPrice")), fill_base=_f(d.get("baseVolume")),
        fee=fee, fee_coin=fee_coin, ts_ms=int(_f(d.get("uTime", d.get("fillTime")))))


class PrivateFeed:
    def __init__(self, settings: Settings, creds: Credentials, clock: Clock):
        self.s, self.creds, self.clock = settings, creds, clock
        self.url = settings.exchange.ws_private_demo_url if creds.demo else settings.exchange.ws_private_live_url
        self.on_order: Optional[OrderCb] = None
        self.on_balance: Optional[BalanceCb] = None
        self.ws = None
        self.connected = False
        self.logged_in = asyncio.Event()
        self.reconnects = 0
        self.last_msg = 0.0
        self._pending: dict[str, asyncio.Future] = {}
        self._task: Optional[asyncio.Task] = None
        self._stop = False
        self._backoff = Backoff(
            settings.rate.backoff_base_s, settings.rate.backoff_max_s)
        self.on_reconnect: Optional[Callable[[], Awaitable[None]]] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="ws-private")

    async def stop(self) -> None:
        self._stop = True
        if self.ws is not None:
            await self.ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def wait_ready(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self.logged_in.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run(self) -> None:
        first = True
        while not self._stop:
            try:
                async with connect(self.url, ping_interval=None, max_size=2 ** 22, open_timeout=10) as ws:
                    self.ws, self.connected = ws, True
                    self.last_msg = time.monotonic()
                    ts = int(self.clock.now_ms() / 1000)
                    await ws.send(json.dumps({"op": "login", "args": [{
                        "apiKey": self.creds.key, "passphrase": self.creds.passphrase, "timestamp": str(ts),
                        "sign": ws_login_signature(self.creds.secret, ts)}]}))
                    ping = asyncio.create_task(self._pinger(ws))
                    try:
                        async for raw in ws:
                            self.last_msg = time.monotonic()
                            await self._handle(raw, first)
                    finally:
                        ping.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("private ws: %s", type(e).__name__)
            finally:
                self.connected = False
                self.logged_in.clear()
                self.ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(
                            ConnectionError("private ws verbroken"))
                self._pending.clear()
            if self._stop:
                return
            first = False
            self.reconnects += 1
            await asyncio.sleep(self._backoff.next_delay())

    async def _pinger(self, ws) -> None:
        while True:
            await asyncio.sleep(20)
            if time.monotonic() - self.last_msg > 45:
                await ws.close()
                return
            await ws.send("ping")

    async def _handle(self, raw: str | bytes, first: bool) -> None:
        if raw == "pong":
            return
        try:
            m = json.loads(raw)
        except json.JSONDecodeError:
            return
        ev = m.get("event")
        if ev == "login":
            if str(m.get("code")) == "0":
                self._backoff.reset()
                await self.ws.send(json.dumps({"op": "subscribe", "args": [
                    {"instType": "SPOT", "channel": "orders", "instId": "default"},
                    {"instType": "SPOT", "channel": "account", "coin": "default"}]}))
                self.logged_in.set()
                if not first and self.on_reconnect:
                    asyncio.create_task(self.on_reconnect())
            else:
                log.error("private ws login mislukt: code %s %s",
                          m.get("code"), m.get("msg"))
            return
        if ev == "error":
            log.error("private ws fout: code %s %s",
                      m.get("code"), m.get("msg"))
            self._resolve_error(m)
            return
        if ev == "trade":
            self._resolve_trade(m)
            return
        arg, data = m.get("arg") or {}, m.get("data")
        if not data:
            return
        ch = arg.get("channel")
        if ch == "orders" and self.on_order:
            for d in data:
                await self.on_order(parse_order_event(d))
        elif ch == "account" and self.on_balance:
            await self.on_balance([Balance(x["coin"], _f(x.get("available")), _f(x.get("frozen")) + _f(x.get("locked")))
                                   for x in data])

    def _resolve_trade(self, m: dict) -> None:
        args = m.get("arg") or [{}]
        rid = args[0].get("id")
        fut = self._pending.pop(rid, None) if rid else None
        if fut and not fut.done():
            fut.set_result(m)

    def _resolve_error(self, m: dict) -> None:
        args = m.get("arg")
        rid = args[0].get("id") if isinstance(args, list) and args else None
        fut = self._pending.pop(rid, None) if rid else None
        if fut and not fut.done():
            fut.set_result(m)

    async def place_order(self, req: OrderRequest, timeout_s: float) -> OrderAck:
        if not (self.connected and self.logged_in.is_set() and self.ws):
            raise ConnectionError("private ws niet klaar")
        rid = uuid.uuid4().hex[:16]
        params = {"orderType": req.order_type, "side": req.side, "size": _fmt(req.size),
                  "force": req.force, "clientOid": req.client_oid}
        if req.price is not None:
            params["price"] = _fmt(req.price)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        t0 = time.perf_counter()
        await self.ws.send(json.dumps({"op": "trade", "args": [{
            "id": rid, "instType": "SPOT", "instId": req.symbol, "channel": "place-order", "params": params}]}))
        try:
            m = await asyncio.wait_for(fut, timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise
        lat = (time.perf_counter() - t0) * 1000
        code = str(m.get("code", ""))
        ok = m.get("event") == "trade" and code in ("0", "00000")
        args = m.get("arg") or [{}]
        oid = (args[0].get("params") or {}).get("orderId")
        return OrderAck(req.client_oid, str(oid) if oid else None, ok, code, str(m.get("msg", "")), lat)
