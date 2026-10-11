import numpy as np
import pandas as pd
import pytest

from tradingbot.config import Settings
from tradingbot.training import build_dataset, triple_barrier
from tradingbot.walkforward import (DAY_MS, MIN_MS, build_wf_data, lifecycle_flags, make_frame, rows, run_walkforward,
                                    simulate_portfolio, summarize)
from tests.test_features_training import synth

T0 = 1_700_000_000_000


def frames(n=2600, n_sym=3):
    f = {"BTCUSDT": synth(n, 10), "ETHUSDT": synth(n, 11)}
    for i in range(n_sym):
        f[f"S{i}USDT"] = synth(n, 20 + i, vol=12.0)
    return f


def test_triple_barrier_duration():
    n, H = 12, 4
    close = np.full(n, 100.0)
    high, low = close.copy(), close.copy()
    tp, sl = np.full(n, 100.0), np.full(n, 100.0)
    high[2] = 101.5
    label, out, dur = triple_barrier(
        high, low, close, tp, sl, H, return_duration=True)
    # TP in de 2e bar na instap
    assert label[0] == 1 and dur[0] == 2
    # time-out: volledige horizon
    assert dur[5] == H
    assert np.isnan(dur[-H:]).all()


def test_wf_arrays_match_training_dataset():
    s = Settings()
    fr = frames()
    wf = build_wf_data(fr, {}, {}, s, [10, 30])
    ref = build_dataset(fr, {}, {}, s, [30])[30]
    idx = rows(wf, 30, 0, 1 << 62)
    got = make_frame(wf, 30, idx).set_index(["symbol", "ts"])
    ref = ref.set_index(["symbol", "ts"])
    common = got.index.intersection(ref.index)
    assert len(common) > 5000 and len(common) >= 0.99 * len(ref)
    for col in ("y", "out_bps", "tp", "sl", "cost", "vol_30", "ret_5", "tp_sigma_ratio"):
        np.testing.assert_allclose(
            got.loc[common, col].values, ref.loc[common, col].values, rtol=1e-4, atol=1e-4)


def test_rows_respect_window_stride_and_validity():
    wf = build_wf_data(frames(), {}, {}, Settings(), [10, 60])
    lo, hi = int(wf.ts.min()) + 1000 * MIN_MS, int(wf.ts.min()) + 1500 * MIN_MS
    idx = rows(wf, 60, lo, hi, stride=5)
    assert ((wf.ts[idx] >= lo) & (wf.ts[idx] < hi)).all() and (
        (wf.ts[idx] // MIN_MS) % 5 == 0).all()
    assert wf.valid[60][idx].all()
    both = rows(wf, None, int(wf.ts.max()) - 30 * MIN_MS,
                int(wf.ts.max()) + 1, all_horizons=True)
    # laatste bars hebben geen label voor horizon 60
    assert len(both) == 0


def sig_df(rows_):
    cols = ["ts", "sym", "h", "p", "ev", "tp", "sl",
            "cost", "net_bps", "y", "end_ts", "version"]
    return pd.DataFrame(rows_, columns=cols)


def one(ts, sym="AAAUSDT", net=30.0, ev=15.0, dur_min=30, y=1.0, v=0, sl=40.0, cost=24.0):
    return [ts, sym, 30, 0.6, ev, 80.0, sl, cost, net, y, ts + dur_min * MIN_MS, v]


def test_portfolio_single_trade_pnl_and_sizing():
    s = Settings()
    res = simulate_portfolio(sig_df([one(T0, net=50.0, ev=7.5)]), s, 1000.0)
    notional = s.decision.base_order_usdt * min(1, 7.5 / s.decision.ev_ref_bps)
    assert res["trades"] == 1 and res["net_pnl_usdt"] == pytest.approx(
        notional * 50 / 1e4)
    assert res["trade_list"][0]["notional"] == pytest.approx(notional)


def test_portfolio_blocks_overlap_same_symbol_and_caps_open_positions():
    s = Settings()
    s.risk.max_buys_per_min = 100
    s.risk.max_buys_per_hour = 100
    sigs = [one(T0, "AAAUSDT"), one(T0 + MIN_MS, "AAAUSDT"),
            one(T0 + 2 * MIN_MS, "AAAUSDT", dur_min=1)]
    res = simulate_portfolio(sig_df(sigs), s, 1000.0)
    # dezelfde positie loopt nog
    assert res["trades"] == 1
    many = [one(T0 + i * 1000, f"S{i}USDT") for i in range(10)]
    res = simulate_portfolio(sig_df(many), s, 1000.0)
    assert res["trades"] == s.risk.max_open_positions
    res = simulate_portfolio(sig_df(sigs), s, 1000.0,
                             allowed=np.array([False, False, False]))
    assert res["trades"] == 0


def test_portfolio_cooldown_after_stop_and_tempo_limits():
    s = Settings()
    # -SL(40) - kosten(24)
    stop = one(T0, "AAAUSDT", net=-64.0, y=0.0, dur_min=5)
    again = one(T0 + 10 * MIN_MS, "AAAUSDT")
    later = one(T0 + 30 * MIN_MS, "AAAUSDT")
    res = simulate_portfolio(sig_df([stop, again, later]), s, 1000.0)
    assert [t["ts"] for t in res["trade_list"]] == [T0, T0 + 30 * MIN_MS]
    s.risk.max_buys_per_min = 1
    burst = [one(T0 + i * 5000, f"S{i}USDT") for i in range(4)]
    assert simulate_portfolio(sig_df(burst), s, 1000.0)["trades"] == 1


def test_portfolio_drawdown_and_daily_loss_stop_new_entries():
    s = Settings()
    s.risk.dd_soft, s.risk.dd_hard = 0.001, 0.002
    s.decision.base_order_usdt = s.decision.max_order_usdt = 100.0
    s.risk.max_position_usdt = 100.0
    s.risk.max_asset_share = 1.0
    sigs = [one(T0, "AAAUSDT", net=-500.0, y=0.0, ev=20.0, dur_min=5),
            one(T0 + 10 * MIN_MS, "BBBUSDT", net=50.0, ev=20.0)]
    res = simulate_portfolio(sig_df(sigs), s, 1000.0)
    # tweede trade geblokkeerd door drawdown
    assert res["trades"] == 1 and res["net_pnl_usdt"] == pytest.approx(-5.0)


def test_lifecycle_requires_completed_outcomes_and_resets_per_version():
    s = Settings()
    s.model.shadow_activate_n, s.model.shadow_demote_n = 10, 20
    good = [one(T0 + i * 10 * MIN_MS,
                f"S{i % 3}USDT", net=20.0, dur_min=5) for i in range(15)]
    sig = sig_df(good)
    flags = lifecycle_flags(sig, {0: False}, s, carry=False)
    # actief pas na 10 afgeronde, goede outcomes
    assert not flags[:10].any() and flags[11:].all()
    # outcomes die nog lopen tellen niet mee
    slow = sig_df(
        [one(T0 + i * MIN_MS, f"S{i}USDT", net=20.0, dur_min=500) for i in range(15)])
    assert not lifecycle_flags(slow, {0: False}, s, carry=False).any()
    # poort geslaagd: direct actief
    assert lifecycle_flags(sig, {0: True}, s, carry=False)[0]
    # nieuwe versie: zonder carry opnieuw shadow, met carry blijft actief
    nxt = sig_df(good + [one(T0 + 400 * MIN_MS, "S9USDT", net=20.0, v=1)])
    assert not lifecycle_flags(nxt, {0: False, 1: False}, s, carry=False)[-1]
    assert lifecycle_flags(nxt, {0: False, 1: False}, s, carry=True)[-1]


def test_lifecycle_demotes_after_bad_series():
    s = Settings()
    s.model.shadow_activate_n, s.model.shadow_demote_n = 5, 10
    seq = [one(T0 + i * 10 * MIN_MS,
               f"S{i % 3}USDT", net=20.0 if i < 8 else -30.0, dur_min=5) for i in range(40)]
    flags = lifecycle_flags(sig_df(seq), {0: True}, s, carry=False)
    assert flags[0] and flags[7] and not flags[-1]


def test_walkforward_end_to_end_on_random_walk():
    s = Settings()
    s.model.n_estimators, s.model.min_data_in_leaf = 30, 30
    s.agents.horizons_min = [10, 30]
    logs = []
    res, bundle = run_walkforward(frames(9000, 4), {}, {}, s, warmup_days=2, train_days=4, retrain_days=2, stride=3,
                                  progress=logs.append)
    assert res["period"]["days_simulated"] > 3 and len(res["weeks"]) >= 2
    assert bundle is not None and set(bundle.horizons) == {10, 30}
    # op ruis mag de poort niet slagen
    assert not bundle.passed_gate
    # zonder geslaagde poort handelt 'strikt' niet
    for v in res.get("variants", {}).values():
        assert v["final_equity"] > 0
    if res.get("variants"):
        assert res["variants"]["lifecycle_strikt"]["trades"] <= res["variants"]["potentieel_alle_signalen"]["trades"]
    assert "periode" in summarize(res)
