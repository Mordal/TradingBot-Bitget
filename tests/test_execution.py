import asyncio

import pytest

from tradingbot.exchange.base import Balance, OrderAck, OrderEvent, OrderRequest
from tradingbot.execution import ExecutionEngine
from tradingbot.fillsim import SimGateway
from tradingbot.marketdata import MarketState
from tradingbot.portfolio import Portfolio
from tradingbot.ratelimit import TokenBucket
from tradingbot.risk import OrderIntent, RiskManager
from tradingbot.storage import Storage
from tests.conftest import feed_flat_market, make_book

SYM = "WLDUSDT"


class World:
    def __init__(self, settings, clock, inst, depth=100_000.0, cash=1000.0, gateway_cls=SimGateway):
        settings.root = settings.root  # onveranderd
        self.clock, self.inst = clock, inst
        self.ms = MarketState(clock)
        feed_flat_market(self.ms, SYM, 1.0, clock.t, spread_bps=2, depth=depth)
        self.sim = gateway_cls(settings, self.ms, {
                               SYM: inst}, clock, cash, latency_ms=300, auto=False)
        self.pf = Portfolio(cash)
        self.pf.lot[SYM] = inst.lot
        self.risk = RiskManager(settings)
        self.db = Storage(":memory:")
        self.eng = ExecutionEngine(
            settings, self.sim, self.pf, self.risk, self.db, {SYM: inst}, clock)
        self.eng.bucket = TokenBucket(1e9)
        self.eng.unknown_delays = (0.01, 0.01)

    def book(self, mid, depth=100_000.0, spread_bps=2.0):
        self.ms.inst[SYM].book = make_book(mid, spread_bps, depth)
        self.ms.inst[SYM].book_recv_ms = self.clock()

    async def settle(self, advance_ms=400, rounds=3):
        # eerst de uitvoeringslaag de order laten versturen
        for _ in range(8):
            await asyncio.sleep(0)
        self.clock.advance(advance_ms)
        for _ in range(rounds):
            await self.sim.process(self.clock())
            for _ in range(4):
                await asyncio.sleep(0)

    def buy_intent(self, qty=50.0, price=1.0005, force="ioc", purpose="entry"):
        return OrderIntent(SYM, "buy", qty, price, qty * price, force, purpose)

    def sell_intent(self, qty, price=0.999, force="ioc", purpose="exit"):
        return OrderIntent(SYM, "sell", qty, price, qty * price, force, purpose)


@pytest.fixture
async def w(settings, clock, inst, tmp_path):
    settings.root = tmp_path
    world = World(settings, clock, inst)
    world.eng.start()
    yield world
    await world.eng.stop()


async def test_ioc_buy_fills_with_fee_in_base_coin_and_releases_reservation(w, inst):
    rec = w.eng.submit(w.buy_intent())
    assert w.pf.reserved[rec.client_oid] > 50
    await w.settle()
    assert rec.state == "FILLED" and rec.filled_base == pytest.approx(50.0)
    pos = w.pf.positions[SYM]
    # fee in gekochte munt
    assert pos.qty == pytest.approx(50.0 * (1 - 0.001))
    assert w.pf.cash == pytest.approx(1000.0 - rec.filled_quote)
    assert rec.client_oid not in w.pf.reserved and rec.done.is_set()
    row = w.db.query("SELECT * FROM orders WHERE client_oid=?",
                     (rec.client_oid,))[0]
    assert row["status"] == "FILLED" and row["ack_latency_ms"] == 300.0
    assert w.db.query("SELECT COUNT(*) c FROM fills")[0]["c"] == 1
    # simulator en ledger zijn consistent
    assert w.pf.reconcile(await w.sim.get_balances()) == []


async def test_partial_fill_on_thin_book_then_ioc_cancel(settings, clock, inst, tmp_path):
    settings.root = tmp_path
    # ~100 USDT zichtbaar
    w = World(settings, clock, inst, depth=100.0)
    w.eng.start()
    rec = w.eng.submit(w.buy_intent(qty=200.0, price=1.01))
    await w.settle()
    assert rec.state == "CANCELLED" and 0 < rec.filled_base < 200.0
    assert w.pf.positions[SYM].qty == pytest.approx(rec.filled_base * 0.999)
    assert rec.client_oid not in w.pf.reserved
    assert w.pf.reconcile(await w.sim.get_balances()) == []
    await w.eng.stop()


async def test_take_profit_rests_and_fills_when_bid_reaches_it(w):
    entry = w.eng.submit(w.buy_intent(), None)
    entry.meta = {"tp_bps": 60.0, "sl_bps": 40.0,
                  "horizon_s": 1800, "agents": {"A": "v"}}
    await w.settle()
    pos = w.pf.positions[SYM]
    assert pos.tp_bps == 60.0 and pos.horizon_s == 1800 and pos.agents == {
        "A": "v"}
    # TP-order plaatsen en laten aankomen
    await w.settle(400)
    tp = w.eng.active_tp(SYM)
    assert tp is not None and tp.purpose == "take_profit" and tp.side == "sell"
    assert w.sim.orders[tp.client_oid].state == "resting"
    assert tp.price == pytest.approx(w.inst.ceil_price(pos.avg_price * 1.006))
    bals = {b.coin: b for b in await w.sim.get_balances()}
    assert bals["WLD"].frozen == pytest.approx(
        w.inst.round_qty(pos.qty))    # hoeveelheid is vastgezet
    # een resting TP telt niet als exit
    assert w.eng.in_flight_symbols() == set()
    # +100 bps: TP geraakt
    w.book(1.01)
    await w.settle(50)
    assert SYM not in w.pf.positions
    ct = w.pf.closed[-1]
    assert ct.pnl_usdt > 0 and ct.pnl_bps > 0
    assert w.pf.reconcile(await w.sim.get_balances()) == []


async def test_exit_requires_cancelling_take_profit_first(w):
    entry = w.eng.submit(w.buy_intent())
    entry.meta = {"tp_bps": 60.0, "sl_bps": 40.0}
    await w.settle()
    await w.settle(50)
    qty = w.pf.positions[SYM].qty
    # TP houdt de hoeveelheid vast
    blocked = w.eng.submit(w.sell_intent(w.inst.round_qty(qty)))
    await w.settle()
    assert blocked.state == "REJECTED" and blocked.reject_code == "43012"
    assert await w.eng.cancel_take_profit(SYM)
    assert w.eng.active_tp(SYM) is None
    ok = w.eng.submit(w.sell_intent(w.inst.round_qty(w.pf.positions[SYM].qty)))
    ok.purpose = "exit"
    await w.settle()
    assert ok.state == "FILLED" and SYM not in w.pf.positions
    assert w.pf.reconcile(await w.sim.get_balances()) == []


async def test_reject_releases_reservation_and_counts_streak(w):
    w.sim.reject_next = "40001"
    rec = w.eng.submit(w.buy_intent())
    await w.settle()
    assert rec.state == "REJECTED" and rec.client_oid not in w.pf.reserved and w.pf.cash == 1000.0
    assert w.risk.reject_streak == 1 and w.risk.breaker_reason is None
    for _ in range(2):
        w.sim.reject_next = "40001"
        w.eng.submit(w.buy_intent())
        await w.settle()
    # drie rejects achter elkaar
    assert w.risk.breaker_reason


async def test_rate_limit_reject_trips_breaker_immediately(w):
    w.sim.reject_next = "429"
    w.eng.submit(w.buy_intent())
    await w.settle()
    assert w.risk.breaker_reason == "rate_limit"


async def test_duplicate_events_are_idempotent(w):
    rec = w.eng.submit(w.buy_intent())
    await w.settle()
    qty, cash = w.pf.positions[SYM].qty, w.pf.cash
    ev = OrderEvent(SYM, rec.client_oid, "x", "filled", "buy", 50, 1.0, 50.0, 1.0, fill_id="sim1-1", fill_price=1.0,
                    fill_base=50.0, fee=0.05, fee_coin="WLD")
    first_fill = w.db.query("SELECT fill_id FROM fills")[0]["fill_id"]
    ev.fill_id = first_fill
    await w.eng.on_order_event(ev)
    await w.eng.on_order_event(ev)
    assert w.pf.positions[SYM].qty == qty and w.pf.cash == cash


async def test_missed_fill_event_is_synthesised_from_order_state(w):
    rec = w.eng.submit(w.buy_intent())
    await asyncio.sleep(0.01)
    # simuleer verloren WS-events
    w.eng.gw.on_order = None
    await w.settle()
    assert w.pf.positions.get(SYM) is None and rec.filled_base == 0
    w.eng.gw.on_order = w.eng.on_order_event
    ev = await w.sim.get_order(SYM, client_oid=rec.client_oid)
    await w.eng.on_order_event(ev, from_rest=True)
    assert rec.filled_base == pytest.approx(50.0) and rec.state == "FILLED"
    assert w.pf.positions[SYM].qty == pytest.approx(50.0 * 0.999)


class TimeoutAfterSend(SimGateway):
    """Order bereikt de beurs, maar de aanroeper krijgt een time-out (onbekende uitkomst)."""

    async def place_order(self, req):
        await super().place_order(req)
        raise asyncio.TimeoutError()


class TimeoutBeforeSend(SimGateway):
    async def place_order(self, req):
        raise asyncio.TimeoutError()


async def test_unknown_outcome_is_resolved_by_client_oid(settings, clock, inst, tmp_path):
    settings.root = tmp_path
    w = World(settings, clock, inst, gateway_cls=TimeoutAfterSend)
    w.eng.start()
    rec = w.eng.submit(w.buy_intent())
    for _ in range(6):
        await asyncio.sleep(0.02)
    # via get_order teruggevonden
    assert rec.state == "ACKED" and rec.order_id
    await w.settle()
    assert rec.state == "FILLED" and w.pf.positions[SYM].qty > 0
    await w.eng.stop()


async def test_unknown_outcome_not_found_closes_order_as_not_executed(settings, clock, inst, tmp_path):
    settings.root = tmp_path
    w = World(settings, clock, inst, gateway_cls=TimeoutBeforeSend)
    w.eng.start()
    rec = w.eng.submit(w.buy_intent())
    for _ in range(10):
        await asyncio.sleep(0.02)
    assert rec.state == "REJECTED" and rec.client_oid not in w.pf.reserved and w.pf.cash == 1000.0
    assert any("niet gevonden" in a for a in w.eng.alerts)
    await w.eng.stop()


async def test_exits_have_priority_over_entries_in_queue(settings, clock, inst, tmp_path):
    settings.root = tmp_path
    w = World(settings, clock, inst)
    order: list[str] = []
    orig = w.sim.place_order

    async def spy(req):
        order.append(req.purpose)
        return await orig(req)

    w.sim.place_order = spy
    w.pf.apply_fill(SYM, "WLD", "buy", 1.0, 50.0, 0.0, "USDT",
                    1, "pre", "1")   # bestaande positie
    w.sim.free["WLD"] = 50.0
    # eerst een entry in de wachtrij
    w.eng.submit(w.buy_intent())
    w.eng.submit(w.sell_intent(50.0), None)
    w.eng.submit(OrderIntent(SYM, "sell", 1.0, 0.99, 0.99, "ioc", "stop"))
    w.eng.start()
    for _ in range(10):
        await asyncio.sleep(0)
    assert set(order[:2]) == {"stop", "exit"} and order[-1] == "entry"
    await w.eng.stop()


async def test_reconcile_cancels_orphans_and_trips_breaker_on_balance_mismatch(w):
    await w.sim.place_order(OrderRequest(SYM, "buy", "limit", 10.0, 0.98, "gtc", "tb-orphan-1"))
    w.sim.orders["tb-orphan-1"].state = "resting"
    issues = await w.eng.reconcile()
    assert any("onbekende open order" in i for i in issues)
    assert w.sim.orders["tb-orphan-1"].state == "done"
    # saldo wijkt af van het ledger
    w.sim.free["USDT"] -= 25.0
    issues = await w.eng.reconcile()
    assert any("USDT" in i for i in issues) and w.risk.breaker_reason.startswith(
        "reconcile")
    w.sim.free["USDT"] += 25.0
    await w.eng.reconcile()
    assert w.risk.breaker_reason is None


async def test_sim_rejects_outside_band_and_insufficient_balance(w):
    ack = await w.sim.place_order(OrderRequest(SYM, "buy", "limit", 10.0, 1.2, "ioc", "a1"))
    assert not ack.ok and ack.code == "40808"
    ack = await w.sim.place_order(OrderRequest(SYM, "buy", "limit", 1e6, 1.0, "ioc", "a2"))
    assert not ack.ok and ack.code == "43012"
    ack = await w.sim.place_order(OrderRequest(SYM, "buy", "limit", 0.5, 1.0, "ioc", "a3"))
    # onder het minimum
    assert not ack.ok
    ack = await w.sim.place_order(OrderRequest(SYM, "buy", "limit", 10.0, 1.0, "ioc", "a4"))
    dup = await w.sim.place_order(OrderRequest(SYM, "buy", "limit", 10.0, 1.0, "ioc", "a4"))
    assert ack.ok and not dup.ok


async def test_sim_order_arrives_only_after_latency_and_uses_book_at_arrival(w):
    rec = w.eng.submit(w.buy_intent(qty=50.0, price=1.0005))
    # nog onderweg
    await w.settle(100)
    assert rec.filled_base == 0
    # markt loopt weg tijdens de vertraging
    w.book(1.02)
    await w.settle(400)
    assert rec.state == "CANCELLED" and rec.filled_base == 0 and SYM not in w.pf.positions
    assert w.pf.cash == 1000.0
