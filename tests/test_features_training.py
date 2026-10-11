import numpy as np
import pandas as pd
import pytest

from tradingbot.config import Settings
from tradingbot.features import FEATURES, MIN_BARS, add_barrier_features, barrier_bps, build_features
from tradingbot.model import IsoCal, ModelBundle, load_current, set_current
from tradingbot.training import (baseline_trades, build_dataset, evaluate_model, gate, split, split_times,
                                 train_and_validate, triple_barrier)

MIN = 60_000


def synth(n: int, seed: int = 1, start_ts: int = 1_700_000_000_000, drift: float = 0.0, vol: float = 8.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, n) / 1e4
    close = 10 * np.exp(np.cumsum(r))
    open_ = np.r_[close[0], close[:-1]]
    spread = np.abs(rng.normal(0, 3, n)) / 1e4
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    return pd.DataFrame({"ts": start_ts + np.arange(n) * MIN, "open": open_, "high": high, "low": low,
                         "close": close, "quote_vol": rng.lognormal(8, 1, n)})


def test_features_have_no_lookahead():
    df, btc, eth = synth(600, 1), synth(600, 2), synth(600, 3)
    full = build_features(df, btc, eth, 1.0)
    cut = 450
    df2 = df.copy()
    df2.loc[cut + 1:, ["open", "high", "low", "close"]
            ] *= 1.5          # toekomst wijzigen
    btc2 = btc.copy()
    btc2.loc[cut + 1:, "close"] *= 0.5
    changed = build_features(df2, btc2, eth, 1.0)
    pd.testing.assert_series_equal(
        full.iloc[cut], changed.iloc[cut], check_names=False)


def test_live_and_training_features_are_identical_for_last_row():
    df, btc, eth = synth(500, 1), synth(500, 2), synth(500, 3)
    full = build_features(df, btc, eth, 1.0).iloc[-1]
    tail = build_features(df.tail(400).reset_index(drop=True), btc.tail(400).reset_index(drop=True),
                          eth.tail(400).reset_index(drop=True), 1.0).iloc[-1]
    pd.testing.assert_series_equal(full, tail, check_names=False, rtol=1e-9)


def test_barrier_bps_scalar_and_array():
    tp, sl, sig = barrier_bps(10.0, 30, 24.0, 2.0, 1.0)
    assert sig == pytest.approx(
        10 * 30 ** 0.5) and tp == pytest.approx(max(48.0, sig)) and sl == pytest.approx(sig)
    tp_a, sl_a, _ = barrier_bps(np.array([1.0, 20.0]), 30, 24.0, 2.0, 1.0)
    assert tp_a[0] == 48.0 and tp_a[1] > 48.0 and sl_a[1] == pytest.approx(
        20 * 30 ** 0.5)


def test_triple_barrier_tp_sl_timeout_and_same_bar():
    n, H = 12, 4
    close = np.full(n, 100.0)
    high, low = close.copy(), close.copy()
    tp, sl = np.full(n, 100.0), np.full(n, 100.0)      # 1% barrières
    high[2] = 101.5                                     # t=0: TP in bar 2
    low[6], high[6] = 98.0, 100.2                       # t=3..: SL in bar 6
    label, out = triple_barrier(high, low, close, tp, sl, H)
    assert label[0] == 1 and out[0] == 100.0
    assert label[4] == 0 and out[4] == -100.0
    assert np.isnan(label[-H:]).all()
    # TP en SL in dezelfde bar: stop telt eerst
    high2, low2 = close.copy(), close.copy()
    high2[1], low2[1] = 102.0, 98.0
    label2, out2 = triple_barrier(high2, low2, close, tp, sl, H)
    assert label2[0] == 0 and out2[0] == -100.0
    # time-out: uitstap op close[t+H]
    close3 = np.r_[np.full(6, 100.0), np.full(6, 100.3)]
    label3, out3 = triple_barrier(close3, close3, close3, tp, sl, H)
    assert label3[3] == 0 and out3[3] == pytest.approx(30.0, rel=0.01)


def test_split_is_chronological_and_purged():
    ts = np.arange(0, 1000) * MIN
    d = pd.DataFrame({"ts": ts, "y": 0.0})
    t1, t2 = split_times(ts, 0.6, 0.2)
    tr, va, te = split(d, t1, t2, 50 * MIN)
    assert tr["ts"].max() <= t1 - 50 * MIN < va["ts"].min()
    assert va["ts"].max() <= t2 - 50 * MIN and te["ts"].min() > t2
    assert len(set(tr["ts"]) & set(va["ts"])) == 0


def test_isocal_monotonic_and_roundtrip(tmp_path):
    rng = np.random.default_rng(0)
    p = rng.random(2000)
    y = (rng.random(2000) < p).astype(float)
    cal = IsoCal.fit(p, y)
    xs = np.linspace(0, 1, 50)
    out = cal.predict(xs)
    assert (np.diff(out) >= -1e-12).all() and 0 <= out.min() and out.max() <= 1
    assert abs(cal.predict(0.8) - 0.8) < 0.12


def _frames(n_sym=3, n=2200):
    frames = {"BTCUSDT": synth(n, 10), "ETHUSDT": synth(n, 11)}
    for i in range(n_sym):
        frames[f"S{i}USDT"] = synth(n, 20 + i, vol=12.0)
    return frames


def test_dataset_shape_and_label_alignment():
    s = Settings()
    data = build_dataset(_frames(), {}, {}, s)
    for h, d in data.items():
        assert set(FEATURES) <= set(d.columns)
        assert d["y"].isin([0.0, 1.0]).all() and not d[FEATURES].drop(
            columns=["tick_bps"]).isna().any().any()
        # labels bestaan alleen als de horizon in de data valt
        assert (d.groupby("symbol")["ts"].max() <=
                1_700_000_000_000 + 2200 * MIN - h * MIN).all()


def test_train_and_validate_random_walk_is_not_promoted(tmp_path):
    s = Settings()
    s.model.n_estimators = 40
    s.model.min_data_in_leaf = 50
    s.agents.horizons_min = [10, 30]
    bundle, report = train_and_validate(_frames(4, 3000), {}, {}, s)
    # op ruis mag de poort niet slagen
    assert report["gate"]["passed"] is False
    assert set(bundle.boosters) == {10, 30}
    # opslaan en terugladen geeft dezelfde voorspellingen
    bundle.save(tmp_path)
    set_current(tmp_path, bundle.version)
    loaded = load_current(tmp_path)
    X = build_dataset(_frames(1, 800), {}, {}, s, [10])[10].head(20)
    np.testing.assert_allclose(bundle.predict(X, 10), loaded.predict(X, 10))


def test_gate_requires_all_checks():
    s = Settings()
    ev = {"trades": {"n": 100, "mean_net_bps": 5.0},
          "signal_z": 2.5, "ic_by_horizon": {30: 0.1}}
    base = {"order_flow": {"mean_net_bps": 1.0},
            "naive": {"mean_net_bps": 0.5}}
    assert gate(ev, base, s)["passed"]
    assert not gate(
        {**ev, "trades": {"n": 5, "mean_net_bps": 5.0}}, base, s)["passed"]
    assert not gate({**ev, "signal_z": 1.0}, base, s)["passed"]
    assert not gate(ev, {"order_flow": {"mean_net_bps": 9.0},
                    "naive": {"mean_net_bps": 0.5}}, s)["passed"]
    assert not gate(
        {**ev, "trades": {"n": 100, "mean_net_bps": -1.0}}, base, s)["passed"]
