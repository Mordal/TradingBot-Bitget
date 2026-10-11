"""Bitget REST-client (v2) met handtekening, klokoffset, rate limiting en demo-header."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp

from ...config import Credentials, Settings
from ...ratelimit import Backoff, TokenBucket
from ..base import (Balance, Candle, ExchangeError, Instrument,
                    OrderBook, OrderEvent, OrderRequest, Ticker)
from .signing import Clock, rest_signature

log = logging.getLogger(__name__)

OK_CODE = "00000"


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class BitgetRest:
    def __init__(self, settings: Settings, creds: Optional[Credentials] = None, clock: Optional[Clock] = None):
        self.s = settings
        self.creds = creds
        self.clock = clock or Clock()
        self.base_url = settings.exchange.rest_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        r = settings.rate
        self.pub_bucket = TokenBucket(r.rest_public_per_s)
        self.priv_bucket = TokenBucket(r.rest_private_per_s)
        self.order_bucket = TokenBucket(r.orders_per_s)
        self.cancel_bucket = TokenBucket(r.cancels_per_s)
        self.last_limit_header: str | None = None
        self.stats = {"requests": 0, "errors": 0, "rate_limited": 0}
        self.rtt_ms: list[float] = []

    async def start(self) -> None:
        if self._session is None:
            conn = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(
                connector=conn, timeout=aiohttp.ClientTimeout(total=10))
        await self.sync_clock()

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def sync_clock(self) -> None:
        t0 = time.time()
        data = await self._request("GET", "/api/v2/public/time", signed=False, bucket=None)
        rtt = (time.time() - t0) * 1000
        self.clock.calibrate(int(data["serverTime"]), rtt)

    # ------------------------------------------------------------------ kern
    async def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                       signed: bool = False, bucket: Optional[TokenBucket] = "default", retries: int = 2,
                       extra_headers: dict | None = None) -> Any:
        if self._session is None:
            await self.start()
        assert self._session is not None
        if bucket == "default":
            bucket = self.priv_bucket if signed else self.pub_bucket
        query = urlencode(
            {k: v for k, v in (params or {}).items() if v is not None})
        body_s = json.dumps(body, separators=(",", ":")
                            ) if body is not None else ""
        backoff = Backoff(self.s.rate.backoff_base_s,
                          self.s.rate.backoff_max_s)
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            if bucket is not None:
                await bucket.acquire()
            headers = {"Content-Type": "application/json",
                       "locale": "en-US", **(extra_headers or {})}
            if signed:
                if not self.creds:
                    raise ExchangeError(
                        "Geen credentials voor een ondertekende aanvraag")
                ts = self.clock.now_ms()
                headers.update({
                    "ACCESS-KEY": self.creds.key,
                    "ACCESS-SIGN": rest_signature(self.creds.secret, ts, method, path, query, body_s),
                    "ACCESS-TIMESTAMP": str(ts),
                    "ACCESS-PASSPHRASE": self.creds.passphrase,
                })
                if self.creds.demo:
                    headers["paptrading"] = "1"
            url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
            t0 = time.perf_counter()
            try:
                async with self._session.request(method, url, data=body_s or None, headers=headers) as resp:
                    text = await resp.text()
                    self.last_limit_header = resp.headers.get(
                        "x-mbx-used-remain-limit", self.last_limit_header)
                    status = resp.status
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # Netwerkfout: alleen veilig te herhalen bij GET (een POST kan al zijn uitgevoerd).
                self.stats["errors"] += 1
                last_err = ExchangeError(f"netwerkfout: {type(e).__name__}")
                if method != "GET" or attempt == retries:
                    raise last_err from None
                await asyncio.sleep(backoff.next_delay())
                continue
            finally:
                self.stats["requests"] += 1
            self.rtt_ms.append((time.perf_counter() - t0) * 1000)
            if len(self.rtt_ms) > 500:
                del self.rtt_ms[:250]
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = {"code": str(status), "msg": text[:200]}
            code = str(payload.get("code", ""))
            if status == 200 and code == OK_CODE:
                return payload.get("data")
            err = ExchangeError(
                f"{payload.get('msg', '')} (code {code})", code=code, http_status=status)
            self.stats["errors"] += 1
            if err.is_rate_limit:
                self.stats["rate_limited"] += 1
                delay = backoff.next_delay()
                if bucket is not None:
                    bucket.penalize(delay)
                last_err = err
                if attempt == retries or method != "GET":
                    raise err
                await asyncio.sleep(delay)
                continue
            raise err
        raise last_err or ExchangeError("onbekende fout")

    # ------------------------------------------------------------ marktdata
    async def get_instruments(self, demo: bool = False) -> list[Instrument]:
        data = await self._request("GET", "/api/v2/spot/public/symbols",
                                   extra_headers={"paptrading": "1"} if demo else None)
        out = []
        for d in data:
            out.append(Instrument(
                symbol=d["symbol"], base=d["baseCoin"], quote=d["quoteCoin"], status=d.get(
                    "status", ""),
                price_precision=int(d["pricePrecision"]), qty_precision=int(d["quantityPrecision"]),
                min_trade_usdt=_f(d.get("minTradeUSDT"), 1.0), min_trade_base=_f(d.get("minTradeAmount")),
                buy_band=_f(d.get("buyLimitPriceRatio"), 0.02), sell_band=_f(d.get("sellLimitPriceRatio"), 0.02),
                taker_fee_list=_f(d.get("takerFeeRate"), 0.001), open_time_ms=int(_f(d.get("openTime")))))
        return out

    async def get_tickers(self) -> list[Ticker]:
        data = await self._request("GET", "/api/v2/spot/market/tickers")
        return [Ticker(symbol=d["symbol"], last=_f(d.get("lastPr")), bid=_f(d.get("bidPr")), ask=_f(d.get("askPr")),
                       quote_volume=_f(d.get("usdtVolume", d.get("quoteVolume"))), ts_ms=int(_f(d.get("ts"))))
                for d in data]

    async def get_orderbook(self, symbol: str, limit: int = 50, demo: bool = False) -> OrderBook:
        d = await self._request("GET", "/api/v2/spot/market/orderbook",
                                {"symbol": symbol, "type": "step0", "limit": limit},
                                extra_headers={"paptrading": "1"} if demo else None)
        return OrderBook(bids=[(float(p), float(q)) for p, q in d["bids"]],
                         asks=[(float(p), float(q)) for p, q in d["asks"]], ts_ms=int(d.get("ts", 0)))

    async def get_candles(self, symbol: str, granularity: str = "1min", end_ms: int | None = None,
                          limit: int = 1000, history: bool = False) -> list[Candle]:
        """Oplopend gesorteerd; eindigt op end_ms. `market/candles` geeft 1000 stuks maar reikt ~30 dagen terug,
        `history-candles` geeft maximaal 200 stuks en reikt maanden terug."""
        path = "/api/v2/spot/market/history-candles" if history else "/api/v2/spot/market/candles"
        if history:
            limit = min(limit, 200)
        d = await self._request("GET", path,
                                {"symbol": symbol, "granularity": granularity, "endTime": end_ms, "limit": limit})
        rows = [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[7]))
                for r in d]
        rows.sort(key=lambda c: c.ts_ms)
        return rows

    async def get_trade_rate(self, symbol: str) -> tuple[float, float]:
        """(maker, taker) als fractie, voor dit account. Leidend t.o.v. de symbolenlijst."""
        d = await self._request("GET", "/api/v2/common/trade-rate",
                                {"symbol": symbol, "businessType": "spot"}, signed=True)
        return _f(d.get("makerFeeRate")), _f(d.get("takerFeeRate"))

    # ---------------------------------------------------------------- account
    async def get_balances(self) -> list[Balance]:
        d = await self._request("GET", "/api/v2/spot/account/assets", {"assetType": "all"}, signed=True)
        return [Balance(x["coin"], _f(x.get("available")), _f(x.get("frozen")) + _f(x.get("locked")))
                for x in d]

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderEvent]:
        d = await self._request("GET", "/api/v2/spot/trade/unfilled-orders",
                                {"symbol": symbol, "limit": 100}, signed=True)
        return [_order_info_to_event(x) for x in d]

    async def get_order(self, symbol: str, client_oid: str | None = None,
                        order_id: str | None = None) -> Optional[OrderEvent]:
        params = {"orderId": order_id, "clientOid": client_oid}
        try:
            d = await self._request("GET", "/api/v2/spot/trade/orderInfo", params, signed=True)
        except ExchangeError as e:
            if e.code in {"43025", "40109"}:  # order bestaat niet
                return None
            raise
        if not d:
            return None
        return _order_info_to_event(d[0] if isinstance(d, list) else d)

    # ---------------------------------------------------------------- orders
    async def place_order(self, req: OrderRequest) -> dict:
        body = {"symbol": req.symbol, "side": req.side, "orderType": req.order_type, "force": req.force,
                "size": _fmt(req.size), "clientOid": req.client_oid}
        if req.price is not None:
            body["price"] = _fmt(req.price)
        return await self._request("POST", "/api/v2/spot/trade/place-order", body=body, signed=True,
                                   bucket=self.order_bucket, retries=0)

    async def cancel_order(self, symbol: str, client_oid: str | None = None, order_id: str | None = None) -> None:
        body = {"symbol": symbol}
        if order_id:
            body["orderId"] = order_id
        elif client_oid:
            body["clientOid"] = client_oid
        await self._request("POST", "/api/v2/spot/trade/cancel-order", body=body, signed=True,
                            bucket=self.cancel_bucket, retries=0)

    async def cancel_all(self, symbol: str) -> None:
        await self._request("POST", "/api/v2/spot/trade/cancel-symbol-order", body={"symbol": symbol},
                            signed=True, bucket=self.cancel_bucket, retries=0)


def _fmt(x: float) -> str:
    s = f"{x:.10f}".rstrip("0").rstrip(".")
    return s or "0"


def _order_info_to_event(d: dict) -> OrderEvent:
    fee = 0.0
    fee_coin = ""
    detail = d.get("feeDetail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            detail = None
    if isinstance(detail, dict):  # {"BGB": {...}} of {"newFees": {...}}
        for coin, v in detail.items():
            if isinstance(v, dict) and "totalFee" in v:
                fee, fee_coin = abs(_f(v["totalFee"])), coin
    elif isinstance(detail, list):
        for v in detail:
            fee += abs(_f(v.get("fee")))
            fee_coin = v.get("feeCoin", fee_coin)
    return OrderEvent(
        symbol=d.get("symbol", ""), client_oid=d.get("clientOid", ""), order_id=str(d.get("orderId", "")),
        status=d.get("status", ""), side=d.get("side", ""), size=_f(d.get("size")), price=_f(d.get("price")),
        acc_base=_f(d.get("baseVolume")), avg_price=_f(d.get("priceAvg")), fee=fee, fee_coin=fee_coin,
        ts_ms=int(_f(d.get("uTime"))))
