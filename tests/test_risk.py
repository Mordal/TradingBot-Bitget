import pytest

from tradingbot.agents.base import Regime
from tradingbot.config import Settings
from tradingbot.decision import Decision
from tradingbot.exchange.base import Instrument
from tradingbot.portfolio import Portfolio
from tradingbot.risk import RiskContext, RiskManager
from tests.conftest import make_book

NOW = 1_700_000_000_000


def entry(sym="WLDUSDT", notional=50.0, limit=None, **kw) -> Decision:
    return Decision(NOW, sym, "BUY", notional_usdt=notional, limit_price=limit or 1.0003, tp_bps=60, sl_bps=40,
                    ev_bps=10, p_win=0.55, **kw)


def ctx(pf, inst, book=None, active=True, age=100, in_flight=None, marks=None, depth_ref=50_000.0, now=NOW):
    book = book or make_book(1.0, 2.0, 10_000)
    return RiskContext(now_ms=now, portfolio=pf, marks=marks or {inst.symbol: 1.0}, inst=inst, book=book,
                       data_age_ms=age, active=active, in_flight=in_flight or set(), depth_ref_usdt=depth_ref)


@pytest.fixture
def rm(settings, tmp_path):
    settings.root = tmp_path
    return RiskManager(settings)


def test_happy_path_rounds_and_respects_band(rm, inst):
    pf = Portfolio(1000.0)
    r = rm.check_entry(entry(), ctx(pf, inst))
    assert r.approved and r.order.side == "buy" and r.order.force == "ioc"
    assert r.order.qty == inst.round_qty(
        r.order.qty) and r.order.notional >= 1.0
    assert r.order.price >= make_book(
        1.0, 2.0).best_ask and r.order.price == inst.ceil_price(r.order.price)


def test_kill_switch_file_blocks_entries_but_not_exits(rm, inst, settings):
    pf = Portfolio(1000.0)
    (settings.root / settings.bot.kill_file).write_text("x")
    assert rm.check_entry(entry(), ctx(pf, inst)).reason == "kill_switch"
    pf.apply_fill("WLDUSDT", "WLD", "buy", 1.0, 50.0, 0.0, "USDT", 1, "o", "1")
    d = Decision(NOW, "WLDUSDT", "SELL", fraction=1.0, limit_price=0.999)
    assert rm.check_exit(d, ctx(pf, inst)).approved


@pytest.mark.parametrize("kwargs,reason", [
    ({"active": False}, "not_in_active_universe"),
    ({"age": 5000}, "stale_data"),
    ({"in_flight": {"WLDUSDT"}}, "position_or_order_active"),
])
def test_entry_rejections(rm, inst, kwargs, reason):
    assert rm.check_entry(entry(), ctx(
        Portfolio(1000.0), inst, **kwargs)).reason == reason


def test_tokenized_stock_and_offline_instruments_rejected(rm):
    pf = Portfolio(1000.0)
    stock = Instrument("rAAPLUSDT", "rAAPL", "USDT", "online", 2, 4, 1.0)
    assert rm.check_entry(entry("rAAPLUSDT"), ctx(
        pf, stock, marks={"rAAPLUSDT": 1.0})).reason == "instrument_not_allowed"
    off = Instrument("WLDUSDT", "WLD", "USDT", "halt", 4, 2, 1.0)
    assert rm.check_entry(entry(), ctx(
        pf, off)).reason == "instrument_not_allowed"


def test_circuit_breaker_blocks_entry(rm, inst):
    rm.trip("reconcile: x")
    assert rm.check_entry(entry(), ctx(Portfolio(1000.0), inst)
                          ).reason.startswith("circuit_breaker")
    rm.clear()
    assert rm.check_entry(entry(), ctx(Portfolio(1000.0), inst)).approved


def test_reject_streak_trips_breaker(rm):
    for _ in range(rm.r.max_reject_streak):
        rm.note_reject()
    assert rm.breaker_reason
    rm.clear()
    rm.note_reject()
    rm.note_ok()
    rm.note_reject()
    assert rm.breaker_reason is None


def test_max_positions_and_cooldown(rm, inst):
    pf = Portfolio(10_000.0)
    for i in range(rm.r.max_open_positions):
        pf.apply_fill(f"C{i}USDT", f"C{i}", "buy", 1.0,
                      5.0, 0.0, "USDT", 1, f"o{i}", "1")
    assert rm.check_entry(entry(), ctx(
        pf, inst)).reason == "max_open_positions"
    rm2 = RiskManager(rm.s)
    rm2.note_stop("WLDUSDT", NOW)
    assert rm2.check_entry(entry(), ctx(
        Portfolio(1000.0), inst, now=NOW + 1000)).reason == "stop_cooldown"
    assert rm2.check_entry(entry(), ctx(
        Portfolio(1000.0), inst, now=NOW + 16 * 60_000)).approved


def test_tempo_limits(rm, inst):
    for i in range(rm.r.max_buys_per_min):
        rm.note_buy(NOW - 1000 * i)
    assert rm.check_entry(entry(), ctx(
        Portfolio(1000.0), inst)).reason == "tempo_minute"
    assert rm.check_entry(entry(), ctx(
        Portfolio(1000.0), inst, now=NOW + 120_000)).approved


def test_drawdown_hard_blocks_and_soft_halves(rm, inst, settings):
    # alleen de drawdown-regels testen
    settings.risk.daily_loss_pct = 0.5
    pf = Portfolio(1000.0)
    pf.mark_equity(NOW - 1000, 1000.0)
    pf.cash = 930.0                                        # -7%  > dd_hard
    assert rm.check_entry(entry(), ctx(
        pf, inst)).reason.startswith("drawdown_hard")
    pf.cash = 960.0                                        # -4%: soft
    r = rm.check_entry(entry(notional=60.0), ctx(pf, inst))
    assert r.approved and "dd_soft_half_size" in r.notes and r.order.notional <= 30.5


def test_daily_loss_blocks_entries(rm, inst, settings):
    pf = Portfolio(1000.0)
    pf.mark_equity(NOW - 5000, 1000.0)
    # alleen dagverlies, geen drawdown t.o.v. piek
    pf._eq_hist.clear()
    pf.cash = 960.0
    settings.risk.dd_hard = 0.5
    settings.risk.dd_soft = 0.4
    assert rm.check_entry(entry(), ctx(
        pf, inst)).reason.startswith("daily_loss")


def test_caps_depth_fraction_usdt_and_position_limits(rm, inst):
    pf = Portfolio(1000.0)
    r = rm.check_entry(entry(notional=100.0), ctx(
        pf, inst, depth_ref=300.0))   # 10% van 300 = 30
    assert r.approved and r.order.notional <= 30.0 and "capped_by_depth_fraction" in r.notes
    # reserve 50 -> maximaal 70 beschikbaar
    pf2 = Portfolio(120.0)
    r2 = rm.check_entry(entry(notional=100.0), ctx(pf2, inst))
    assert r2.approved and r2.order.notional <= 70.0
    pf3 = Portfolio(120.0)
    assert rm.check_entry(entry(notional=100.0), ctx(
        pf3, inst, depth_ref=1.0)).reason in ("below_minimum", "no_capacity")


def test_below_minimum_and_price_band(rm, inst):
    r = rm.check_entry(entry(notional=0.5), ctx(Portfolio(1000.0), inst))
    assert not r.approved and r.reason == "below_minimum"
    far = make_book(1.0, 2.0)
    r = rm.check_entry(entry(limit=1.5), ctx(
        Portfolio(1000.0), inst, book=far, marks={"WLDUSDT": 0.9}))
    assert not r.approved and r.reason == "outside_price_band"


def test_limit_price_capped_by_max_slippage(rm, inst):
    book = make_book(1.0, 2.0, 100_000)
    r = rm.check_entry(entry(limit=1.05), ctx(
        Portfolio(1000.0), inst, book=book))
    assert r.approved
    assert r.order.price <= book.best_ask * \
        (1 + rm.r.max_slippage_bps / 1e4) + inst.tick


def test_visible_depth_caps_notional(rm, inst):
    # slechts ~100 USDT zichtbaar
    book = make_book(1.0, 2.0, depth=100.0)
    r = rm.check_entry(entry(notional=90.0, limit=1.0005), ctx(
        Portfolio(1000.0), inst, book=book, depth_ref=1e9))
    assert r.approved and r.order.notional <= 100.0


def test_exit_uses_net_quantity_and_flags_dust(rm, inst):
    pf = Portfolio(1000.0)
    pf.apply_fill("WLDUSDT", "WLD", "buy", 1.0, 50.0, 0.05,
                  "WLD", 1, "o", "1")     # netto 49,95
    d = Decision(NOW, "WLDUSDT", "SELL", fraction=1.0,
                 limit_price=0.9992, urgent=True)
    r = rm.check_exit(d, ctx(pf, inst))
    assert r.approved and r.order.qty == pytest.approx(
        49.95) and r.order.side == "sell" and r.order.purpose == "stop"
    assert r.order.price == inst.floor_price(
        r.order.price) and r.order.price <= make_book(1.0, 2.0).best_bid
    pf2 = Portfolio(1000.0)
    pf2.apply_fill("WLDUSDT", "WLD", "buy", 1.0, 0.5, 0.0,
                   "USDT", 1, "o", "1")      # 0,5 USDT: onder minimum
    assert rm.check_exit(d, ctx(pf2, inst)).reason == "dust_position"
    assert rm.check_exit(d, ctx(Portfolio(10.0), inst)).reason == "no_position"
