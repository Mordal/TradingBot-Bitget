"""Publieke Bitget-WebSocket: sharded verbindingen, herverbinden, stale-detectie, subscribe-budget."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Awaitable, Callable, Optional

from websockets.asyncio.client import connect

from ...config import Settings
from ...ratelimit import Backoff

log = logging.getLogger(__name__)

Sub = tuple[str, str]  # (channel, instId)
OnData = Callable[[str, str, str, list], None]  # channel, symbol, action, data


def _arg(channel: str, inst: str) -> dict:
    return {"instType": "SPOT", "channel": channel, "instId": inst}


class _Shard:
    def __init__(self, idx: int, url: str, on_data: OnData, stale_after_s: float, backoff: Backoff):
        self.idx, self.url, self.on_data = idx, url, on_data
        self.stale_after_s = stale_after_s
        self.desired: set[Sub] = set()
        self.ws = None
        self.connected = False
        self.last_msg = 0.0
        self.msg_count = 0
        self.reconnects = 0
        self._backoff = backoff
        self._task: Optional[asyncio.Task] = None
        self._stop = False

    def start(self) -> None:
        self._task = asyncio.create_task(
            self._run(), name=f"ws-public-{self.idx}")

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

    async def send_ops(self, op: str, subs: set[Sub]) -> None:
        if not (self.connected and self.ws and subs):
            return
        items = sorted(subs)
        for i in range(0, len(items), 20):  # kleine batches
            await self.ws.send(json.dumps({"op": op, "args": [_arg(c, s) for c, s in items[i:i + 20]]}))

    async def _run(self) -> None:
        while not self._stop:
            try:
                async with connect(self.url, ping_interval=None, max_size=2 ** 22, open_timeout=10) as ws:
                    self.ws, self.connected = ws, True
                    self.last_msg = time.monotonic()
                    self._backoff.reset()
                    await self.send_ops("subscribe", set(self.desired))
                    ping = asyncio.create_task(self._pinger(ws))
                    try:
                        async for raw in ws:
                            self.last_msg = time.monotonic()
                            self._handle(raw)
                    finally:
                        ping.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # verbinding weggevallen: opnieuw proberen
                log.warning("public ws shard %s: %s",
                            self.idx, type(e).__name__)
            finally:
                self.connected, self.ws = False, None
            if self._stop:
                return
            self.reconnects += 1
            await asyncio.sleep(self._backoff.next_delay())

    async def _pinger(self, ws) -> None:
        while True:
            await asyncio.sleep(20)
            if self.desired and time.monotonic() - self.last_msg > self.stale_after_s:
                log.warning(
                    "public ws shard %s: geen data, opnieuw verbinden", self.idx)
                await ws.close()
                return
            await ws.send("ping")

    def _handle(self, raw: str | bytes) -> None:
        if raw == "pong":
            return
        try:
            m = json.loads(raw)
        except json.JSONDecodeError:
            return
        if "event" in m:
            if m["event"] == "error":
                log.error("public ws fout: %s", m.get("msg"))
            return
        arg, data = m.get("arg"), m.get("data")
        if not arg or data is None:
            return
        self.msg_count += 1
        self.on_data(arg["channel"], arg["instId"],
                     m.get("action", "update"), data)


class PublicFeed:
    def __init__(self, settings: Settings, on_data: OnData):
        self.s = settings
        self.on_data = on_data
        self.shards: list[_Shard] = []
        self._sub_times: deque[float] = deque()
        self.deferred = 0

    @property
    def subscriptions(self) -> set[Sub]:
        return {s for sh in self.shards for s in sh.desired}

    @property
    def connected(self) -> bool:
        return bool(self.shards) and all(sh.connected for sh in self.shards if sh.desired)

    def _budget_left(self) -> int:
        now = time.monotonic()
        while self._sub_times and now - self._sub_times[0] > 3600:
            self._sub_times.popleft()
        return self.s.rate.ws_subscribes_per_hour - len(self._sub_times)

    async def set_subscriptions(self, wanted: set[Sub]) -> None:
        current = self.subscriptions
        to_remove, to_add = current - wanted, wanted - current
        cap = self.s.rate.ws_channels_per_connection
        # Eerst verwijderen: dat kost niets van het budget aan nieuwe abonnementen.
        for sh in self.shards:
            gone = sh.desired & to_remove
            if gone:
                sh.desired -= gone
                await sh.send_ops("unsubscribe", gone)
        added: dict[_Shard, set[Sub]] = {}
        for sub in sorted(to_add):
            if self._budget_left() <= 0:
                self.deferred += 1
                log.warning("subscribe-budget op, %s uitgesteld", sub)
                continue
            target = next(
                (sh for sh in self.shards if len(sh.desired) < cap), None)
            if target is None:
                target = _Shard(len(self.shards), self.s.exchange.ws_public_url, self.on_data, 45.0,
                                Backoff(self.s.rate.backoff_base_s, self.s.rate.backoff_max_s))
                self.shards.append(target)
                target.start()
            target.desired.add(sub)
            added.setdefault(target, set()).add(sub)
            self._sub_times.append(time.monotonic())
        for sh, subs in added.items():
            await sh.send_ops("subscribe", subs)

    async def stop(self) -> None:
        await asyncio.gather(*(sh.stop() for sh in self.shards), return_exceptions=True)

    def stats(self) -> dict:
        return {"shards": len(self.shards), "subs": len(self.subscriptions),
                "messages": sum(sh.msg_count for sh in self.shards),
                "reconnects": sum(sh.reconnects for sh in self.shards), "deferred": self.deferred}
