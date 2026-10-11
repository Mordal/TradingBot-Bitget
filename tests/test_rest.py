import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tradingbot.config import Credentials, Settings
from tradingbot.exchange.base import ExchangeError, OrderRequest
from tradingbot.exchange.bitget.rest import BitgetRest
from tradingbot.exchange.bitget.signing import rest_signature


async def make(handler_routes, creds=None):
    app = web.Application()
    if not any(p == "/api/v2/public/time" for _m, p, _h in handler_routes):
        async def time_h(request):
            return ok({"serverTime": str(int(__import__("time").time() * 1000))})
        app.router.add_get("/api/v2/public/time", time_h)
    for method, path, h in handler_routes:
        app.router.add_route(method, path, h)
    server = TestServer(app)
    await server.start_server()
    s = Settings()
    s.exchange.rest_url = str(server.make_url("")).rstrip("/")
    s.rate.backoff_base_s, s.rate.backoff_max_s = 0.01, 0.02
    s.rate.rest_public_per_s = s.rate.rest_private_per_s = 1000.0
    rest = BitgetRest(s, creds)
    return server, rest


def ok(data):
    return web.json_response({"code": "00000", "msg": "success", "data": data})


async def test_signed_request_has_correct_signature_and_demo_header():
    seen = {}

    async def h(request):
        seen["headers"] = dict(request.headers)
        seen["query"] = request.query_string
        return ok([{"coin": "USDT", "available": "10", "frozen": "1", "locked": "0.5"}])

    server, rest = await make([("GET", "/api/v2/spot/account/assets", h)],
                              Credentials("K", "S", "P", demo=True))
    try:
        bals = await rest.get_balances()
        hd = seen["headers"]
        assert hd["ACCESS-KEY"] == "K" and hd["ACCESS-PASSPHRASE"] == "P" and hd["paptrading"] == "1"
        expected = rest_signature("S", int(
            hd["ACCESS-TIMESTAMP"]), "GET", "/api/v2/spot/account/assets", seen["query"])
        assert hd["ACCESS-SIGN"] == expected
        assert bals[0].coin == "USDT" and bals[0].available == 10.0 and bals[0].frozen == 1.5
    finally:
        await rest.close()
        await server.close()


async def test_public_calls_are_unsigned_and_live_data():
    seen = {}

    async def h(request):
        seen["headers"] = dict(request.headers)
        return ok([{"symbol": "WLDUSDT", "lastPr": "1", "bidPr": "0.9999", "askPr": "1.0001", "usdtVolume": "5", "ts": "1"}])

    server, rest = await make([("GET", "/api/v2/spot/market/tickers", h)], Credentials("K", "S", "P", demo=True))
    try:
        t = (await rest.get_tickers())[0]
        assert t.symbol == "WLDUSDT" and t.quote_volume == 5.0
        assert "ACCESS-SIGN" not in seen["headers"] and "paptrading" not in seen["headers"]
    finally:
        await rest.close()
        await server.close()


async def test_post_body_is_signed_and_not_retried_on_rate_limit():
    calls = {"n": 0}

    async def h(request):
        calls["n"] += 1
        body = await request.text()
        ts = int(request.headers["ACCESS-TIMESTAMP"])
        assert request.headers["ACCESS-SIGN"] == rest_signature(
            "S", ts, "POST", "/api/v2/spot/trade/place-order", "", body)
        assert json.loads(body)["clientOid"] == "c1"
        return web.json_response({"code": "429", "msg": "Too Many Requests"}, status=429)

    server, rest = await make([("POST", "/api/v2/spot/trade/place-order", h)], Credentials("K", "S", "P", demo=False))
    try:
        with pytest.raises(ExchangeError) as e:
            await rest.place_order(OrderRequest("WLDUSDT", "buy", "limit", 10, 1.0, "ioc", "c1"))
        # een order wordt nooit blind herhaald
        assert e.value.is_rate_limit and calls["n"] == 1
        assert rest.stats["rate_limited"] == 1
    finally:
        await rest.close()
        await server.close()


async def test_get_is_retried_after_rate_limit_then_succeeds():
    calls = {"n": 0}

    async def h(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return web.json_response({"code": "429", "msg": "slow down"}, status=429)
        return ok({"serverTime": "1700000000000"})

    server, rest = await make([("GET", "/api/v2/public/time", h)])
    try:
        # start() synchroniseert de klok via GET /public/time
        await rest.start()
        assert calls["n"] == 3 and rest.stats["rate_limited"] == 2
        assert abs(rest.clock.now_ms() - 1_700_000_000_000) < 5000
    finally:
        await rest.close()
        await server.close()


async def test_exchange_error_code_is_exposed_and_missing_order_returns_none():
    async def h(request):
        return web.json_response({"code": "43025", "msg": "Order does not exist", "data": None})

    server, rest = await make([("GET", "/api/v2/spot/trade/orderInfo", h)], Credentials("K", "S", "P", demo=True))
    try:
        assert await rest.get_order("WLDUSDT", client_oid="x") is None
    finally:
        await rest.close()
        await server.close()

    async def bad(request):
        return web.json_response({"code": "40014", "msg": "Incorrect permissions", "data": None})

    server, rest = await make([("GET", "/api/v2/common/trade-rate", bad)], Credentials("K", "S", "P", demo=False))
    try:
        with pytest.raises(ExchangeError) as e:
            await rest.get_trade_rate("WLDUSDT")
        assert e.value.code == "40014" and e.value.is_rate_limit is True or e.value.code == "40014"
    finally:
        await rest.close()
        await server.close()


async def test_candles_are_sorted_and_instruments_parsed():
    async def candles(request):
        assert request.query["granularity"] == "1min" and request.query["limit"] == "1000"
        return ok([["3000", "3", "3.5", "2.5", "3.2", "10", "32", "32"], ["1000", "1", "1.5", "0.5", "1.2", "10", "12", "12"]])

    async def symbols(request):
        return ok([{"symbol": "WLDUSDT", "baseCoin": "WLD", "quoteCoin": "USDT", "status": "online", "pricePrecision": "4",
                    "quantityPrecision": "2", "minTradeUSDT": "1", "minTradeAmount": "0", "buyLimitPriceRatio": "0.02",
                    "sellLimitPriceRatio": "0.02", "takerFeeRate": "0.002", "openTime": "1690185600000"}])

    server, rest = await make([("GET", "/api/v2/spot/market/candles", candles),
                               ("GET", "/api/v2/spot/public/symbols", symbols)])
    try:
        cs = await rest.get_candles("WLDUSDT", end_ms=5000)
        assert [c.ts_ms for c in cs] == [
            1000, 3000] and cs[0].quote_vol == 12.0
        inst = (await rest.get_instruments())[0]
        assert inst.price_precision == 4 and inst.qty_precision == 2 and inst.open_time_ms == 1690185600000
        # lijstwaarde wordt bewaard maar niet gebruikt voor kosten
        assert inst.taker_fee_list == 0.002
    finally:
        await rest.close()
        await server.close()
