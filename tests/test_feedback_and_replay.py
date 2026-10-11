import gzip
import json
import math

import numpy as np
import pytest

from tradingbot.agents.base import Advice
from tradingbot.agents.edge_lgbm import EdgeAgent
from tradingbot.backtest import replay
from tradingbot.config import Settings
from tradingbot.exchange.base import Candle, Instrument
from tradingbot.feedback import BucketCalibrator, Feedback, evaluate_path
from tradingbot.marketdata import MIN_MS, MarketState
from tradingbot.storage import Storage
from tests.conftest import make_book


# ------------------------------------------------------------------------------- feedback
def arr(*x):
    return np.array(x, dtype=float)


def test_evaluate_path_barriers_and_timeout():
    # TP eerst: +60 - kosten
    net, adv = evaluate_path(100.0, arr(100.2, 100.7), arr(
        99.9, 100.0), 100.5, 24.0, 60.0, 40.0)
    assert net == pytest.approx(60.0 - 24.0) and adv == pytest.approx(-10.0)
    # stop eerst, ook als TP in dezelfde bar geraakt wordt
    net, _ = evaluate_path(100.0, arr(100.7), arr(99.5),
                           100.0, 24.0, 60.0, 40.0)
    assert net == pytest.approx(-40.0 - 24.0)
    # geen barrières: tijduitstap
    net, _ = evaluate_path(100.0, arr(100.1, 100.2), arr(
        99.95, 100.0), 100.1, 24.0, None, None)
    assert net == pytest.approx(10.0 - 24.0)
    # SELL-advies is juist als de koers daalt
    net, _ = evaluate_path(100.0, arr(100.0, 99.8), arr(
        99.7, 99.5), 99.6, 24.0, None, None, "SELL")
    assert net == pytest.approx(40.0)
    assert math.isnan(evaluate_path(
        100.0, arr(), arr(), 100.0, 24.0, None, None)[0])


def test_bucket_calibrator_needs_samples_then_maps_to_hit_rate():
    c = BucketCalibrator(min_samples=10)
    assert c(0.65) == 0.65
    for i in range(10):
        c.add(0.65, correct=i < 4)
    assert c(0.62) == pytest.approx(0.4) and c(0.35) == 0.35


def _fb(settings, clock):
    ms = MarketState(clock)
    db = Storage(":memory:")
    return Feedback(settings, db, ms, clock), ms, db


def _bars(ms, sym, start_min_ts, closes):
    cs, prev = [], closes[0]
    for i, c in enumerate(closes):
        cs.append(Candle(start_min_ts + i * MIN_MS, prev, max(prev, c)
                  * 1.0001, min(prev, c) * 0.9999, c, 1, 100.0))
        prev = c
    ms.seed_bars(sym, cs)


def test_feedback_records_buy_and_evaluates_all_horizons(settings, clock):
    fb, ms, db = _fb(settings, clock)
    t0 = clock.t // MIN_MS * MIN_MS
    adv = Advice("AGENT_EDGE_LGBM", "v1", t0, "WLDUSDT", "BUY", 0.6,
                 30 * 60, 12.0, {"tp_bps": 50, "sl_bps": 40, "cost_bps": 24})
    aid = fb.record(adv, 100.0, 24.0)
    assert aid and len(fb.pending) == 1
    # langzaam stijgend: +50 bps na 50 min
    closes = [100.0 + 0.01 * i for i in range(1, 200)]
    _bars(ms, "WLDUSDT", t0 + MIN_MS, closes)
    n = fb.evaluate_due(t0 + 125 * MIN_MS)
    rows = db.query(
        "SELECT horizon_s, net_bps, correct FROM outcomes ORDER BY horizon_s")
    assert n == 4 and [r["horizon_s"] for r in rows] == [
        600, 1800, 3600, 7200] and not fb.pending
    h30 = next(r for r in rows if r["horizon_s"] == 1800)
    # 30 min * 1 bps/min, geen barrière geraakt
    assert h30["net_bps"] == pytest.approx(30 * 1.0 - 24.0, abs=1.5)


def test_feedback_logs_hold_sparingly(settings, clock):
    fb, _, db = _fb(settings, clock)
    hold = Advice("AGENT_EDGE_LGBM", "v1", clock.t, "WLDUSDT",
                  "HOLD", 0.3, 0, -3.0, {"reason": "no_edge"})
    assert fb.record(hold, 1.0, 24.0)
    hold2 = Advice("AGENT_EDGE_LGBM", "v1", clock.t + 60_000,
                   "WLDUSDT", "HOLD", 0.3, 0, -3.0, {"reason": "no_edge"})
    assert fb.record(hold2, 1.0, 24.0) is None
    assert fb.record(Advice("AGENT_EDGE_LGBM", "v1", clock.t, "X", "HOLD", 0, 0, 0, {
                     "reason": "warming_up"}), 1.0, 24.0) is None
    assert not fb.pending


def test_lifecycle_activation_and_demotion(settings, clock):
    fb, _, _ = _fb(settings, clock)
    m = settings.model
    fb.set_initial("AGENT_EDGE_LGBM", "shadow")
    a = Advice("AGENT_EDGE_LGBM", "v9", 0, "X", "BUY", 0.6, 1800, 5.0, {})
    # 60% raak, positief gemiddelde
    for i in range(m.shadow_activate_n):
        fb._learn(a, 20.0 if i % 5 < 3 else -10.0, i % 5 < 3)
    assert fb.get_status("AGENT_EDGE_LGBM") == "active"
    # daarna slechte reeks
    for i in range(m.shadow_demote_n):
        fb._learn(a, -15.0, False)
    assert fb.get_status("AGENT_EDGE_LGBM") == "shadow"
    fb.set_initial("AGENT_EDGE_LGBM", "shadow")
    fb.window.clear()
    for i in range(m.shadow_activate_n - 1):
        fb._learn(a, 20.0, True)
    # nog niet genoeg beoordelingen
    assert fb.get_status("AGENT_EDGE_LGBM") == "shadow"


# --------------------------------------------------------------------------------- replay
SYM = "WLDUSDT"


def write_recording(directory, t0, minutes, price_fn):
    directory.mkdir(parents=True, exist_ok=True)
    events, n = [], 0
    for s in range(minutes * 60):
        t = t0 + s * 1000
        p = round(price_fn(s / 60.0), 4)
        n += 1
        events.append({"t": t, "c": "trade", "s": SYM, "a": "update",
                       "d": [{"ts": str(t), "price": str(p), "size": "5", "side": "buy" if s % 3 else "sell", "tradeId": str(n)}]})
        b = make_book(p, 2.0, 400_000)
        events.append({"t": t, "c": "books5", "s": SYM, "a": "snapshot",
                       "d": [{"bids": [[str(a), str(q)] for a, q in b.bids], "asks": [[str(a), str(q)] for a, q in b.asks], "ts": str(t)}]})
        events.append({"t": t, "c": "ticker", "s": SYM, "a": "snapshot",
                       "d": [{"lastPr": str(p), "bidPr": str(b.best_bid), "askPr": str(b.best_ask), "quoteVolume": "5000000"}]})
    with gzip.open(directory / "20260101_00.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(e) for e in events) + "\n")


def patch_edge(monkeypatch, trigger_ms, p=0.6, tp=100.0, sl=40.0):
    fired = {"n": 0}

    def fake(self, symbol, df, btc, eth, tick_bps, spread_bps, fee_bps, now_ms):
        if now_ms >= trigger_ms and fired["n"] == 0:
            fired["n"] += 1
            return Advice("AGENT_EDGE_LGBM", "test", now_ms, symbol, "BUY", p, 1800, 20.0,
                          {"tp_bps": tp, "sl_bps": sl, "cost_bps": 24.0})
        return Advice("AGENT_EDGE_LGBM", "test", now_ms, symbol, "HOLD", 0.3, 0, -5.0, {"reason": "no_edge", "cost_bps": 24.0})

    monkeypatch.setattr(EdgeAgent, "evaluate", fake)


@pytest.fixture
def rsettings(tmp_path):
    s = Settings()
    s.root = tmp_path
    s.bot.force_model_active = True
    return s


INST = {SYM: Instrument(SYM, "WLD", "USDT", "online", 4, 2, 1.0)}
T0 = 1_700_000_000_000 // MIN_MS * MIN_MS


async def test_replay_take_profit_round_trip_end_to_end(tmp_path, rsettings, monkeypatch):
    # vlak, daarna +150 bps: take-profit (+100 bps) wordt geraakt
    write_recording(tmp_path / "rec", T0, 30, lambda m: 1.0 if m <
                    8 else min(1.015, 1.0 + 0.0015 * (m - 8)))
    patch_edge(monkeypatch, T0 + 5 * MIN_MS)
    rep = await replay(rsettings, tmp_path / "rec", INST, [SYM], None, None, 1000.0)
    assert rep["trades"] == 1 and rep["net_pnl_usdt"] > 0 and not rep["open_positions"], rep
    assert rep["counters"]["entries"] == 1 and rep["mean_pnl_bps"] > 50
    assert rep["exit_reasons"] == {"take_profit": 1}


async def test_replay_stop_loss_and_cooldown_prevent_reentry(tmp_path, rsettings, monkeypatch):
    write_recording(tmp_path / "rec", T0, 30, lambda m: 1.0 if m <
                    8 else max(0.992, 1.0 - 0.001 * (m - 8)))
    patch_edge(monkeypatch, T0 + 5 * MIN_MS)
    rep = await replay(rsettings, tmp_path / "rec", INST, [SYM], None, None, 1000.0)
    assert rep["trades"] == 1 and rep["net_pnl_usdt"] < 0 and not rep["open_positions"], rep
    assert rep["exit_reasons"] == {"stop_loss": 1}
    assert rep["mean_pnl_bps"] < -30


async def test_replay_without_model_never_trades(tmp_path, rsettings):
    write_recording(tmp_path / "rec", T0, 10, lambda m: 1.0 + 0.001 * m)
    rep = await replay(rsettings, tmp_path / "rec", INST, [SYM], None, None, 1000.0)
    assert rep["trades"] == 0 and rep["final_equity"] == pytest.approx(1000.0)


async def test_replay_shadow_model_does_not_trade_until_active(tmp_path, monkeypatch):
    s = Settings()
    s.root = tmp_path
    write_recording(tmp_path / "rec", T0, 15, lambda m: 1.0)
    patch_edge(monkeypatch, T0 + 5 * MIN_MS)
    # force_model_active = False
    rep = await replay(s, tmp_path / "rec", INST, [SYM], None, None, 1000.0)
    assert rep["trades"] == 0 and rep["counters"].get(
        "skipped_shadow_model", 0) >= 1
