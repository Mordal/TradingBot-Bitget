"""Verplichte metingen (Crypto Bot.md 8.4): rate limits, latency en WebSocket-capaciteit. Alleen op demo."""
from __future__ import annotations

import asyncio
import json
import math
import time
from collections import Counter, defaultdict
from typing import Any

import numpy as np
from websockets.asyncio.client import connect

from .config import ConfigError, Settings, load_credentials
from .exchange.base import ExchangeError
from .exchange.bitget.rest import BitgetRest
from .exchange.bitget.ws_public import PublicFeed
from .ratelimit import TokenBucket


def _pct(v: list[float], q: float) -> float | None:
    return float(np.percentile(v, q)) if v else None


async def _paced(rate: float, seconds: float, make_call) -> list[tuple[bool, str, float]]:
    """Start `rate` aanroepen per seconde gedurende `seconds`; geeft (ok, code, latency_ms) per aanroep."""
    results: list[tuple[bool, str, float]] = []

    async def one() -> None:
        t0 = time.perf_counter()
        try:
            await make_call()
            results.append((True, "ok", (time.perf_counter() - t0) * 1000))
        except ExchangeError as e:
            results.append((False, e.code or str(
                e.http_status or "err"), (time.perf_counter() - t0) * 1000))
        except Exception as e:  # noqa: BLE001
            results.append(
                (False, type(e).__name__, (time.perf_counter() - t0) * 1000))

    n = int(rate * seconds)
    t_start = time.perf_counter()
    tasks = []
    for i in range(n):
        delay = t_start + i / rate - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        tasks.append(asyncio.create_task(one()))
    await asyncio.gather(*tasks)
    return results


def _summarize(rate: float, res: list[tuple[bool, str, float]], elapsed: float) -> dict[str, Any]:
    lat = [r[2] for r in res if r[0]]
    codes = Counter(r[1] for r in res if not r[0])
    return {"target_per_s": rate, "sent": len(res), "ok": sum(1 for r in res if r[0]),
            "errors": dict(codes), "achieved_per_s": round(len(res) / max(elapsed, 1e-9), 1),
            "p50_ms": _pct(lat, 50), "p95_ms": _pct(lat, 95), "p99_ms": _pct(lat, 99)}


async def measure_rest_public(settings: Settings, steps=(5, 10, 20, 40, 80), seconds: float = 3.0) -> dict:
    rest = BitgetRest(settings)
    await rest.start()
    out = []
    try:
        async def call() -> None:
            await rest._request("GET", "/api/v2/spot/market/tickers", {"symbol": "BTCUSDT"}, bucket=None, retries=0)
        for rate in steps:
            t0 = time.perf_counter()
            res = await _paced(rate, seconds, call)
            row = _summarize(rate, res, time.perf_counter() - t0)
            row["limit_header"] = rest.last_limit_header
            out.append(row)
            if row["errors"]:
                break
            await asyncio.sleep(1.0)
    finally:
        await rest.close()
    ok_rates = [r["target_per_s"] for r in out if not r["errors"]]
    return {"steps": out, "max_error_free_per_s": max(ok_rates, default=None)}


async def measure_orders_demo(settings: Settings, steps=(2, 5, 10, 20, 40), seconds: float = 3.0) -> dict:
    if settings.is_live:
        raise ConfigError("measure-orders draait nooit op live")
    creds = load_credentials(settings, "demo")
    rest = BitgetRest(settings, creds)
    await rest.start()
    rest.order_bucket = TokenBucket(1e6, 1e6)
    rest.cancel_bucket = TokenBucket(1e6, 1e6)
    try:
        demo = {i.symbol: i for i in await rest.get_instruments(demo=True)}
        inst = demo.get("BTCUSDT")
        if inst is None:
            return {"error": "BTCUSDT niet beschikbaar op demo"}
        book = await rest.get_orderbook("BTCUSDT", 5)
        # ruim binnen de band, vult nooit
        price = inst.floor_price(book.mid * 0.985)
        qty = inst.round_qty(max(inst.lot, 2.0 / price) + inst.lot)
        out: list[dict] = []
        cnt = 0

        async def place() -> None:
            nonlocal cnt
            cnt += 1
            from .exchange.base import OrderRequest
            await rest.place_order(OrderRequest("BTCUSDT", "buy", "limit", qty, price, "gtc", f"m{int(time.time())}{cnt}"))

        for rate in steps:
            t0 = time.perf_counter()
            res = await _paced(rate, seconds, place)
            row = _summarize(rate, res, time.perf_counter() - t0)
            row["limit_header"] = rest.last_limit_header
            out.append(row)
            await rest.cancel_all("BTCUSDT")
            if row["errors"]:
                break
            await asyncio.sleep(1.0)
        ok_rates = [r["target_per_s"] for r in out if not r["errors"]]
        return {"symbol": "BTCUSDT", "price": price, "qty": qty, "steps": out,
                "max_error_free_per_s": max(ok_rates, default=None)}
    finally:
        try:
            await rest.cancel_all("BTCUSDT")
        except ExchangeError:
            pass
        await rest.close()


async def measure_ws(settings: Settings, seconds: float = 12.0, n_symbols: int = 10, channel_test: int = 120) -> dict:
    # 1. berichtsnelheid per kanaal
    rest = BitgetRest(settings)
    await rest.start()
    real = {i.symbol for i in await rest.get_instruments()
            if i.quote == "USDT" and i.status == "online" and not i.is_tokenized_stock}
    tickers = sorted((t for t in await rest.get_tickers() if t.symbol in real), key=lambda t: -t.quote_volume)
    await rest.close()
    symbols = [t.symbol for t in tickers][:n_symbols]
    counts: dict[str, int] = defaultdict(int)

    def on_data(ch: str, sym: str, action: str, data: list) -> None:
        counts[ch] += 1

    feed = PublicFeed(settings, on_data)
    await feed.set_subscriptions({(ch, s) for s in symbols for ch in ("ticker", "trade", "books5")})
    await asyncio.sleep(seconds)
    stats = feed.stats()
    await feed.stop()
    per_channel = {ch: round(n / seconds / len(symbols), 2)
                   for ch, n in counts.items()}

    # 2. aantal kanalen op één verbinding
    syms = [t.symbol for t in tickers][:channel_test]
    errors: list[str] = []
    acks = 0
    async with connect(settings.exchange.ws_public_url, ping_interval=None, open_timeout=10) as ws:
        for i in range(0, len(syms), 20):
            await ws.send(json.dumps({"op": "subscribe", "args": [
                {"instType": "SPOT", "channel": "ticker", "instId": s} for s in syms[i:i + 20]]}))
            await asyncio.sleep(0.3)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), 1.0)
            except asyncio.TimeoutError:
                continue
            if raw == "pong":
                continue
            m = json.loads(raw)
            if m.get("event") == "subscribe":
                acks += 1
            elif m.get("event") == "error":
                errors.append(f"{m.get('code')}:{m.get('msg')}")
    return {"symbols": symbols, "msgs_per_s_per_symbol": per_channel, "feed_stats": stats,
            "channel_test": {"requested": len(syms), "acks": acks, "errors": sorted(set(errors))[:5]}}


def recommend(rest_pub: dict, orders: dict) -> dict:
    """Voorgestelde `[rate]`-waarden: 50% van de hoogste foutvrije snelheid."""
    out = {}
    if rest_pub.get("max_error_free_per_s"):
        out["rest_public_per_s"] = math.floor(
            rest_pub["max_error_free_per_s"] * 0.5)
    if orders.get("max_error_free_per_s"):
        out["orders_per_s"] = max(1, math.floor(
            orders["max_error_free_per_s"] * 0.5))
    return out
