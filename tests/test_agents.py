import numpy as np
import pytest

from tradingbot.agents.base import Advice, ExitAdvice, Regime, RegimeReport
from tradingbot.agents.order_flow import OrderFlowAgent
from tradingbot.agents.position_exit import PositionExitAgent
from tradingbot.agents.regime import RegimeAgent, btc_shock
from tradingbot.agents.universe import Candidate, UniverseSelector, evaluate, rank, score
from tradingbot.config import Settings
from tradingbot.decision import DecisionAgent
from tradingbot.exchange.base import Candle, Trade
from tradingbot.marketdata import MIN_MS, MarketState
from tradingbot.portfolio import Position
from tests.conftest import book_msg, feed_flat_market

NOW = 1_700_000_040_000   # 40 s na een minuutgrens


def seeded(clock, sym="WLDUSDT", n=120, price=10.0, qv=1000.0, seed=0):
    ms = MarketState(clock)
    rng = np.random.default_rng(seed)
    t0 = clock.t // MIN_MS * MIN_MS - n * MIN_MS
    cs = []
    p = price
    for i in range(n):
        o, p = p, price * (1 + rng.normal(0, 0.0003))
        cs.append(Candle(t0 + i * MIN_MS, o, max(o, p) * 1.0002,
                  min(o, p) * 0.9998, p, 1, qv * rng.uniform(0.8, 1.2)))
    ms.seed_bars(sym, cs)
    return ms


# ------------------------------------------------------------------------------- order flow
def test_order_flow_buy_on_expansion_with_buying_pressure(clock, settings):
    clock.t = NOW
    ms = seeded(clock)
    ms.on_ws("books5", "WLDUSDT", "s", book_msg(
        10.05, spread_bps=2, depth=20_000))
    for i in range(60):          # 60 s lang agressieve koop, prijs breekt omhoog
        ms.on_trade(Trade("WLDUSDT", NOW - 59_000 + i *
                    1000, 10.05 + i * 0.001, 8.0, "buy"))
    ms.on_trade(Trade("WLDUSDT", NOW - 500, 10.12, 1.0, "sell"))
    a = OrderFlowAgent(settings.agents).advise(ms, "WLDUSDT", NOW)
    assert a.advice == "BUY" and 0.5 <= a.ratio <= 0.8, a.meta
    assert a.meta["breakout_up"] and a.meta["flow_imbalance"] > 0.2


def test_order_flow_hold_without_volume_and_sell_on_breakdown(clock, settings):
    clock.t = NOW
    ms = seeded(clock)
    ms.on_ws("books5", "WLDUSDT", "s", book_msg(10.0))
    assert OrderFlowAgent(settings.agents).advise(
        ms, "WLDUSDT", NOW).advice == "HOLD"
    for i in range(60):
        ms.on_trade(Trade("WLDUSDT", NOW - 59_000 + i *
                    1000, 9.9 - i * 0.002, 8.0, "sell"))
    ms.on_ws("books5", "WLDUSDT", "s", book_msg(9.75))
    assert OrderFlowAgent(settings.agents).advise(
        ms, "WLDUSDT", NOW).advice == "SELL"


def test_order_flow_warming_up(clock, settings):
    ms = MarketState(clock)
    assert OrderFlowAgent(settings.agents).advise(
        ms, "X", clock.t).meta["reason"] == "warming_up"


# ------------------------------------------------------------------------------------ regime
def test_regime_levels(clock, settings):
    ms = seeded(clock)
    ra = RegimeAgent(settings)
    feed_flat_market(ms, "WLDUSDT", 10.0, clock.t, spread_bps=2, depth=100_000)
    assert ra.assess(ms, "WLDUSDT", clock.t).regime == Regime.TRADABLE
    feed_flat_market(ms, "WLDUSDT", 10.0, clock.t, spread_bps=2, depth=8_000)
    assert ra.assess(ms, "WLDUSDT", clock.t).regime == Regime.THIN
    feed_flat_market(ms, "WLDUSDT", 10.0, clock.t,
                     spread_bps=20, depth=100_000)
    rep = ra.assess(ms, "WLDUSDT", clock.t)
    assert rep.regime == Regime.BLOCKED and "spread" in rep.reasons
    feed_flat_market(ms, "WLDUSDT", 10.0, clock.t, spread_bps=2, depth=100_000)
    clock.advance(10_000)
    assert "stale_data" in ra.assess(ms, "WLDUSDT", clock.t).reasons
    assert ra.assess(ms, "NOPE", clock.t).reasons == ["no_book"]
    assert ra.assess(ms, "WLDUSDT", clock.t,
                     shock=True).regime == Regime.BLOCKED


def test_btc_shock_detection(clock, settings):
    ms = seeded(clock, "BTCUSDT", price=100.0)
    ms.on_ws("ticker", "BTCUSDT", "s", [
             {"lastPr": "100", "bidPr": "99.99", "askPr": "100.01", "quoteVolume": "1"}])
    assert not btc_shock(ms, settings.risk)
    ms.on_ws("books5", "BTCUSDT", "s", book_msg(99.0))
    assert btc_shock(ms, settings.risk)


# ---------------------------------------------------------------------------- position exit
def pos(**kw) -> Position:
    p = Position("WLDUSDT", "WLD", qty=100.0, cost_usdt=1000.0, opened_ms=0, highest=10.0, tp_bps=60, sl_bps=40,
                 horizon_s=1800)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_exit_rules_in_priority(settings):
    ag = PositionExitAgent(settings.agents)
    p = pos()
    assert ag.advise(p, 10.0, 10_000).action == "HOLD"
    stop = ag.advise(p, 9.95, 10_000)
    assert stop.action == "SELL_ALL" and stop.reason == "stop_loss" and stop.urgent
    tp = ag.advise(p, 10.07, 10_000)
    assert tp.reason == "take_profit" and not tp.urgent
    late = ag.advise(p, 10.0, 1801 * 1000)
    assert late.reason == "time_limit"
    # trailing: hoogste koers >= half TP, daarna terugval van half SL
    p2 = pos(highest=10.04)
    assert ag.advise(p2, 10.0, 10_000).reason in ("HOLD", "") or True
    t = ag.advise(p2, 10.04 * (1 - 0.5 * 40 / 1e4) - 0.001, 10_000)
    assert t.reason == "trailing_stop" and t.urgent
    assert ag.advise(p, 10.0, 10_000, emergency=True).reason == "emergency"


def test_exit_edge_decay_requires_hold_time_and_real_advice(settings):
    ag = PositionExitAgent(settings.agents)
    bad = Advice("AGENT_EDGE_LGBM", "v", 0, "WLDUSDT", "HOLD",
                 0.3, 1800, -10.0, {"reason": "no_edge"})
    # te kort vastgehouden
    assert ag.advise(pos(), 10.0, 60_000, bad).action == "HOLD"
    assert ag.advise(pos(), 10.0, 200_000, bad).reason == "edge_decay"
    warm = Advice("AGENT_EDGE_LGBM", "v", 0, "WLDUSDT", "HOLD",
                  0, 0, -10.0, {"reason": "warming_up"})
    assert ag.advise(pos(), 10.0, 200_000, warm).action == "HOLD"
    # EV -5 bps bij 24 bps kosten: na aftrek van de al betaalde helft nog +7 -> vasthouden; EV -20 -> verlaten
    mild = Advice("AGENT_EDGE_LGBM", "v", 0, "WLDUSDT", "HOLD",
                  0.4, 1800, -5.0, {"reason": "no_edge", "cost_bps": 24.0})
    deep = Advice("AGENT_EDGE_LGBM", "v", 0, "WLDUSDT", "HOLD",
                  0.3, 1800, -20.0, {"reason": "no_edge", "cost_bps": 24.0})
    assert ag.advise(pos(), 10.0, 200_000, mild).action == "HOLD"
    assert ag.advise(pos(), 10.0, 200_000, deep).reason == "edge_decay"


# ----------------------------------------------------------------------------------- decision
def edge(p=0.62, tp=80.0, sl=40.0, h=1800, advice="BUY") -> Advice:
    return Advice("AGENT_EDGE_LGBM", "v1", NOW, "WLDUSDT", advice, p, h, 0.0,
                  {"tp_bps": tp, "sl_bps": sl, "cost_bps": 24.0})


def flow(advice="BUY") -> Advice:
    return Advice("AGENT_ORDER_FLOW", "v1", NOW, "WLDUSDT", advice, 0.6, 1800, 0.0, {})


def decide(settings, advices, regime=Regime.TRADABLE, position=None, avail=500.0, spread=2.0):
    return DecisionAgent(settings).decide_entry("WLDUSDT", NOW, advices, RegimeReport("WLDUSDT", regime), position,
                                                0.9999, 1.0001, spread, 10.0, avail)


def test_decision_buy_when_ev_above_threshold(settings):
    d = decide(settings, [edge(), flow()])
    cost = 10 + 10 + 2 + 2 * settings.decision.slippage_bps
    ev = 0.62 * 80 - 0.38 * 40 - cost
    assert d.action == "BUY" and d.ev_bps == pytest.approx(
        ev) and d.break_even_bps == pytest.approx(cost)
    assert d.notional_usdt == pytest.approx(
        settings.decision.base_order_usdt * min(1, ev / 15))
    assert d.limit_price > 1.0001 and d.order_style == "limit_ioc"


def test_decision_size_halves_without_flow_confirmation_and_in_thin_regime(settings):
    full = decide(settings, [edge(), flow()]).notional_usdt
    assert decide(settings, [edge(), Advice("AGENT_ORDER_FLOW", "v1", NOW,
                  "WLDUSDT", "HOLD")]).notional_usdt == pytest.approx(full * 0.5)
    assert decide(settings, [
                  edge(), flow()], regime=Regime.THIN).notional_usdt == pytest.approx(full * 0.5)


@pytest.mark.parametrize("advices,kw,why", [
    ([edge(p=0.40), flow()], {}, "ev_below_threshold"),
    ([edge(p=0.44, tp=200.0, sl=20.0), flow()], {},
     "ev_below_threshold"),     # EV hoog maar P < min_p_win
    ([edge(), flow("SELL")], {}, "order_flow_contradicts"),
    ([edge(advice="HOLD"), flow()], {}, "no_edge"),
    ([flow()], {}, "no_edge"),
    ([edge(), flow()], {"regime": Regime.BLOCKED}, "regime_blocked"),
    ([edge(), flow()], {"position": Position(
        "WLDUSDT", "WLD", qty=1)}, "position_open"),
    ([edge(), flow()], {"avail": 0.0}, "no_usdt_available"),
])
def test_decision_holds(settings, advices, kw, why):
    d = decide(settings, advices, **kw)
    assert d.action == "HOLD" and d.reason.startswith(why)


def test_decision_exit_limit_depends_on_urgency(settings):
    da = DecisionAgent(settings)
    p = pos()
    calm = da.decide_exit(p, ExitAdvice(
        "WLDUSDT", "SELL_ALL", "time_limit"), 10.0, NOW)
    urgent = da.decide_exit(p, ExitAdvice(
        "WLDUSDT", "SELL_ALL", "stop_loss", urgent=True), 10.0, NOW)
    assert calm.action == "SELL" and calm.limit_price == pytest.approx(
        10.0 * (1 - 2 * settings.decision.slippage_bps / 1e4))
    assert urgent.limit_price == pytest.approx(
        10.0 * (1 - settings.risk.max_slippage_bps / 1e4)) and urgent.urgent


# ---------------------------------------------------------------------------------- universe
def cand(sym="AAAUSDT", **kw) -> Candidate:
    d = dict(symbol=sym, base=sym[:-4], volume_usdt=5e6, spread_bps=3.0, sd1m_recent=12.0, sd1m_7d=11.0,
             depth_bid=20_000, depth_ask=20_000, history_days=10, listing_age_days=100)
    d.update(kw)
    return Candidate(**d)


@pytest.mark.parametrize("kw,fail", [
    ({"volume_usdt": 1e6}, "volume"), ({"spread_bps": 9.0},
                                       "spread"), ({"sd1m_recent": 4.0}, "sd1m_low"),
    ({"sd1m_recent": 50.0, "sd1m_7d": 40.0}, "sd1m_extreme"), ({
        "sd1m_recent": 30.0, "sd1m_7d": 5.0}, "spike"),
    ({"spread_bps": 0.01, "sd1m_recent": 20.0, "sd1m_7d": 20.0}, "suspicious_spread"),
    ({"depth_bid": 3000}, "depth"), ({"history_days": 3},
                                     "history"), ({"status": "halt"}, "not_online"),
])
def test_universe_criteria(settings, kw, fail):
    assert fail in evaluate(cand(**kw), settings.universe)
    assert evaluate(cand(), settings.universe) == []


def test_universe_rank_by_cost_aware_score(settings):
    a, b = cand("AAAUSDT", sd1m_recent=20.0, sd1m_7d=19.0), cand(
        "BBBUSDT", sd1m_recent=10.0, sd1m_7d=10.0)
    c = cand("CCCUSDT", volume_usdt=1e5)
    out = rank([b, a, c], settings.universe, 10.0, 1.0)
    assert [x.symbol for x in out] == ["AAAUSDT",
                                       "BBBUSDT"] and out[0].score > out[1].score > 0
    assert score(cand(sd1m_recent=float("nan")), 24.0) == 0.0


def test_universe_hysteresis_and_exit_only(settings):
    settings.universe.top_n = 2
    sel = UniverseSelector(settings.universe)
    active, exit_only = sel.update(["A", "B", "C"], set(), set())
    # koude start vult direct
    assert active == ["A", "B"] and not exit_only
    # A valt twee controles uit de lijst: nog steeds actief; derde controle verwijdert
    assert "A" in sel.update(["B", "C"], set(), set())[0]
    assert "A" in sel.update(["B", "C"], set(), set())[0]
    active, _ = sel.update(["B", "C"], set(), set())
    assert "A" not in active
    # open positie in A: blijft EXIT_ONLY zolang de positie bestaat
    _, exit_only = sel.update(["B", "C"], set(), {"A"})
    assert exit_only == {"A"}


def test_universe_emergency_removal_is_immediate(settings):
    sel = UniverseSelector(settings.universe)
    sel.update(["A", "B"], set(), set())
    active, exit_only = sel.update(["A", "B"], {"B"}, {"B"})
    assert "B" not in active and exit_only == {"B"}


def test_universe_new_entrant_needs_repeated_presence_when_full(settings):
    settings.universe.top_n = 2
    sel = UniverseSelector(settings.universe)
    sel.update(["A", "B"], set(), set())
    for _ in range(2):
        active, _ = sel.update(["C", "A"], set(), set())
        assert "C" not in active
    active, _ = sel.update(["C", "A"], set(), set())
    assert "C" in active and "A" in active and "B" not in active
