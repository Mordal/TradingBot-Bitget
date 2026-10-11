import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from tradingbot.config import Credentials, Settings, load_settings
from tradingbot.exchange.base import Balance, OrderEvent, OrderRequest
from tradingbot.exchange.bitget.rest import _fmt, _order_info_to_event
from tradingbot.exchange.bitget.signing import Clock, ws_login_signature
from tradingbot.exchange.bitget.ws_private import PrivateFeed, parse_order_event
from tradingbot.exchange.bitget.ws_public import PublicFeed


async def until(cond, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


class FakeServer:
    """Minimale Bitget-nabootsing; bewaart ontvangen berichten en kan verbindingen verbreken."""

    def __init__(self, handler):
        self.handler, self.received, self.conns, self.srv, self.port = handler, [], [], None, 0

    async def __aenter__(self):
        async def h(ws):
            self.conns.append(ws)
            await self.handler(self, ws)
        self.srv = await serve(h, "127.0.0.1", 0)
        self.port = self.srv.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *a):
        self.srv.close()
        await self.srv.wait_closed()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}"


async def public_handler(server, ws):
    async for raw in ws:
        if raw == "ping":
            await ws.send("pong")
            continue
        m = json.loads(raw)
        server.received.append(m)
        if m["op"] == "subscribe":
            for a in m["args"]:
                await ws.send(json.dumps({"event": "subscribe", "arg": a}))
                if a["channel"] == "ticker":
                    await ws.send(json.dumps({"action": "snapshot", "arg": a, "data": [{"lastPr": "1.5"}]}))


def fast_settings(url_attr, url, per_conn=100):
    s = Settings()
    setattr(s.exchange, url_attr, url)
    s.rate.backoff_base_s, s.rate.backoff_max_s = 0.01, 0.05
    s.rate.ws_channels_per_connection = per_conn
    return s


async def test_public_feed_subscribes_dispatches_and_unsubscribes():
    async with FakeServer(public_handler) as srv:
        got = []
        feed = PublicFeed(fast_settings("ws_public_url", srv.url),
                          lambda ch, sym, act, data: got.append((ch, sym, act, data)))
        await feed.set_subscriptions({("ticker", "AAAUSDT"), ("trade", "AAAUSDT")})
        assert await until(lambda: any(g[0] == "ticker" for g in got))
        assert got[0][1] == "AAAUSDT" and got[0][3] == [{"lastPr": "1.5"}]
        assert feed.stats()["subs"] == 2 and feed.connected
        await feed.set_subscriptions({("ticker", "AAAUSDT")})
        assert await until(lambda: any(m["op"] == "unsubscribe" for m in srv.received))
        unsub = next(m for m in srv.received if m["op"] == "unsubscribe")
        assert unsub["args"] == [{"instType": "SPOT",
                                  "channel": "trade", "instId": "AAAUSDT"}]
        await feed.stop()


async def test_public_feed_shards_over_multiple_connections():
    async with FakeServer(public_handler) as srv:
        feed = PublicFeed(fast_settings(
            "ws_public_url", srv.url, per_conn=3), lambda *a: None)
        await feed.set_subscriptions({("ticker", f"S{i}USDT") for i in range(7)})
        assert await until(lambda: len(srv.conns) == 3)
        assert feed.stats()["shards"] == 3 and feed.stats()["subs"] == 7
        assert await until(lambda: sum(len(m["args"]) for m in srv.received if m["op"] == "subscribe") == 7)
        await feed.stop()


async def test_public_feed_reconnects_and_resubscribes():
    async with FakeServer(public_handler) as srv:
        got = []
        feed = PublicFeed(fast_settings("ws_public_url", srv.url),
                          lambda ch, sym, act, data: got.append(ch))
        await feed.set_subscriptions({("ticker", "AAAUSDT")})
        assert await until(lambda: len(got) == 1)
        # server verbreekt de verbinding
        await srv.conns[0].close()
        # na herverbinden opnieuw geabonneerd
        assert await until(lambda: len(got) == 2, 5.0)
        assert feed.stats()["reconnects"] >= 1
        await feed.stop()


async def test_public_feed_respects_subscribe_budget():
    async with FakeServer(public_handler) as srv:
        s = fast_settings("ws_public_url", srv.url)
        s.rate.ws_subscribes_per_hour = 2
        feed = PublicFeed(s, lambda *a: None)
        await feed.set_subscriptions({("ticker", f"S{i}USDT") for i in range(5)})
        assert feed.stats()["subs"] == 2 and feed.stats()["deferred"] == 3
        await feed.stop()


# --------------------------------------------------------------------------------- private
def private_handler(login_code=0, trade_code=0):
    async def h(server, ws):
        async for raw in ws:
            if raw == "ping":
                await ws.send("pong")
                continue
            m = json.loads(raw)
            server.received.append(m)
            if m["op"] == "login":
                await ws.send(json.dumps({"event": "login", "code": login_code}))
            elif m["op"] == "trade":
                a = m["args"][0]
                if a["instType"] != "SPOT":
                    await ws.send(json.dumps({"event": "error", "arg": [a], "code": 41101, "msg": "Param instType error"}))
                elif trade_code:
                    await ws.send(json.dumps({"event": "error", "arg": [a], "code": trade_code, "msg": "rejected"}))
                else:
                    oid = "9001"
                    await ws.send(json.dumps({"event": "trade", "arg": [{**a, "params": {"orderId": oid, "clientOid": a["params"]["clientOid"]}}],
                                              "code": 0, "msg": "Success"}))
                    await ws.send(json.dumps({"action": "snapshot", "arg": {"channel": "orders"}, "data": [{
                        "instId": a["instId"], "orderId": oid, "clientOid": a["params"]["clientOid"], "status": "filled",
                        "side": "buy", "size": "10", "price": "1", "accBaseVolume": "10", "priceAvg": "1",
                        "tradeId": "t1", "fillPrice": "1", "baseVolume": "10", "uTime": "5",
                        "feeDetail": [{"feeCoin": "WLD", "fee": "-0.01"}]}]}))
    return h


CREDS = Credentials("KEY", "SECRET", "PASS", demo=True)


async def test_private_feed_login_place_order_and_events():
    async with FakeServer(private_handler()) as srv:
        feed = PrivateFeed(fast_settings(
            "ws_private_demo_url", srv.url), CREDS, Clock())
        events, bals = [], []

        async def on_o(e): events.append(e)
        async def on_b(b): bals.append(b)
        feed.on_order, feed.on_balance = on_o, on_b
        feed.start()
        assert await feed.wait_ready(3)
        login = next(m for m in srv.received if m["op"] == "login")["args"][0]
        assert login["apiKey"] == "KEY" and login["sign"] == ws_login_signature(
            "SECRET", int(login["timestamp"]))
        assert await until(lambda: any(m["op"] == "subscribe" for m in srv.received))
        subs = next(m for m in srv.received if m["op"] == "subscribe")["args"]
        assert {a["channel"] for a in subs} == {"orders", "account"}
        ack = await feed.place_order(OrderRequest("WLDUSDT", "buy", "limit", 10, 1.0, "ioc", "cid1"), 2.0)
        assert ack.ok and ack.order_id == "9001" and ack.latency_ms >= 0
        sent = next(m for m in srv.received if m["op"] == "trade")["args"][0]
        assert sent["instType"] == "SPOT" and sent["channel"] == "place-order"
        assert sent["params"] == {"orderType": "limit", "side": "buy",
                                  "size": "10", "force": "ioc", "clientOid": "cid1", "price": "1"}
        assert await until(lambda: events)
        ev = events[0]
        assert ev.status == "filled" and ev.fill_id == "t1" and ev.fee == pytest.approx(
            0.01) and ev.fee_coin == "WLD"
        await feed.stop()


async def test_private_feed_returns_exchange_error_as_failed_ack():
    async with FakeServer(private_handler(trade_code=43012)) as srv:
        feed = PrivateFeed(fast_settings(
            "ws_private_demo_url", srv.url), CREDS, Clock())
        feed.start()
        assert await feed.wait_ready(3)
        ack = await feed.place_order(OrderRequest("WLDUSDT", "buy", "limit", 10, 1.0, "ioc", "cid2"), 2.0)
        assert not ack.ok and ack.code == "43012"
        await feed.stop()


async def test_private_feed_login_failure_never_becomes_ready():
    async with FakeServer(private_handler(login_code=30012)) as srv:
        feed = PrivateFeed(fast_settings(
            "ws_private_demo_url", srv.url), CREDS, Clock())
        feed.start()
        assert not await feed.wait_ready(0.5)
        with pytest.raises(ConnectionError):
            await feed.place_order(OrderRequest("WLDUSDT", "buy", "limit", 10, 1.0, "ioc", "x"), 1.0)
        await feed.stop()


async def test_private_feed_reconnect_triggers_callback_and_fails_pending():
    # reageert op login, maar nooit op orders
    async def silent(server, ws):
        async for raw in ws:
            m = json.loads(raw) if raw != "ping" else {}
            if m.get("op") == "login":
                await ws.send(json.dumps({"event": "login", "code": 0}))
    async with FakeServer(silent) as srv:
        feed = PrivateFeed(fast_settings(
            "ws_private_demo_url", srv.url), CREDS, Clock())
        called = []

        async def cb(): called.append(1)
        feed.on_reconnect = cb
        feed.start()
        assert await feed.wait_ready(3)
        pending = asyncio.create_task(feed.place_order(OrderRequest(
            "WLDUSDT", "buy", "limit", 10, 1.0, "ioc", "c"), 5.0))
        await asyncio.sleep(0.1)
        await srv.conns[0].close()
        # lopende order krijgt direct een fout i.p.v. te wachten
        with pytest.raises(ConnectionError):
            await pending
        # na herlogin: reconciliatie-callback
        assert await until(lambda: called, 5.0)
        await feed.stop()


def test_parse_order_event_and_order_info_variants():
    ev = parse_order_event({"instId": "WLDUSDT", "orderId": 5, "clientOid": "c", "status": "partially_filled", "side": "sell",
                            "size": "10", "price": "2", "accBaseVolume": "4", "priceAvg": "2.0", "fillFee": "-0.008",
                            "fillFeeCoin": "USDT", "tradeId": "77", "fillPrice": "2.0", "baseVolume": "4", "uTime": "9"})
    assert ev.order_id == "5" and ev.fee == pytest.approx(
        0.008) and ev.fee_coin == "USDT" and ev.fill_id == "77"
    info = _order_info_to_event({"symbol": "WLDUSDT", "orderId": "1", "clientOid": "c", "status": "filled", "side": "buy",
                                 "size": "10", "price": "1", "baseVolume": "10", "priceAvg": "1",
                                 "feeDetail": '{"newFees": {"c": 0, "d": 0, "deduction": false, "r": -0.01, "t": -0.01, "totalDeductionFee": 0}, "WLD": {"deduction": false, "feeCoinCode": "WLD", "totalDeductionFee": 0, "totalFee": -0.01}}',
                                 "uTime": "5"})
    assert info.fee == pytest.approx(
        0.01) and info.fee_coin == "WLD" and info.acc_base == 10


def test_number_formatting_for_orders():
    assert _fmt(0.00012) == "0.00012" and _fmt(10.0) == "10" and _fmt(
        1e-7) == "0.0000001" and _fmt(83000.5) == "83000.5"


def test_config_with_bom_is_accepted(tmp_path):
    p = tmp_path / "c.toml"
    p.write_bytes(b"\xef\xbb\xbf[bot]\nmode = \"trade\"\n")
    assert load_settings(p).bot.mode == "trade"
