"""Training: triple-barrier labels, chronologische splits met purge, LightGBM, kalibratie en validatiepoort."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Optional

import lightgbm as lgb
import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from .config import Settings
from .costs import roundtrip_cost_bps
from .features import FEATURES, add_barrier_features, barrier_bps, build_features, MIN_BARS
from .model import IsoCal, ModelBundle

MIN_MS = 60_000


# ----------------------------------------------------------------------------- labels
def triple_barrier(high: np.ndarray, low: np.ndarray, close: np.ndarray, tp_bps: np.ndarray,
                   sl_bps: np.ndarray, horizon: int, return_duration: bool = False):
    """Per rij t (instap op close[t]): label 1 als TP eerst geraakt, en het bruto resultaat in bps.

    Zelfde bar raakt TP en SL: SL telt eerst (conservatief). Geen raak binnen `horizon`: uitstap op close[t+H].
    De laatste `horizon` rijen krijgen NaN. Met `return_duration` volgt een derde array: aantal bars tot de uitstap.
    """
    n = len(close)
    label = np.full(n, np.nan)
    out = np.full(n, np.nan)
    dur = np.full(n, np.nan)
    if n <= horizon + 1:
        return (label, out, dur) if return_duration else (label, out)
    m = n - horizon
    entry = close[:m]
    tp_px = entry * (1 + tp_bps[:m] / 1e4)
    sl_px = entry * (1 - sl_bps[:m] / 1e4)
    wh = sliding_window_view(high[1:], horizon)[:m]
    wl = sliding_window_view(low[1:], horizon)[:m]
    tp_hit, sl_hit = wh >= tp_px[:, None], wl <= sl_px[:, None]
    any_tp, any_sl = tp_hit.any(1), sl_hit.any(1)
    first_tp = np.where(any_tp, tp_hit.argmax(1), horizon + 1)
    first_sl = np.where(any_sl, sl_hit.argmax(1), horizon + 1)
    tp_first = any_tp & (first_tp < first_sl)
    sl_first = any_sl & (first_sl <= first_tp)
    timeout_ret = (close[horizon:horizon + m] / entry - 1) * 1e4
    res = np.where(tp_first, tp_bps[:m], np.where(
        sl_first, -sl_bps[:m], timeout_ret))
    label[:m] = tp_first.astype(float)
    out[:m] = res
    if return_duration:
        dur[:m] = np.where(tp_first | sl_first, np.minimum(
            first_tp, first_sl) + 1, horizon)
        return label, out, dur
    return label, out


def symbol_cost_bps(settings: Settings, spread_bps: Optional[float]) -> float:
    sp = spread_bps if spread_bps is not None and spread_bps == spread_bps else settings.model.default_spread_bps
    return roundtrip_cost_bps(sp, settings.fees.taker_bps, settings.fees.taker_bps, settings.decision.slippage_bps)


def build_dataset(frames: dict[str, pd.DataFrame], tick_bps: dict[str, float], spreads: dict[str, float],
                  settings: Settings, horizons: Optional[list[int]] = None) -> dict[int, pd.DataFrame]:
    horizons = horizons or settings.agents.horizons_min
    btc, eth = frames.get("BTCUSDT"), frames.get("ETHUSDT")
    ag = settings.agents
    per_h: dict[int, list[pd.DataFrame]] = {h: [] for h in horizons}
    for sym, df in frames.items():
        if len(df) < MIN_BARS + max(horizons) + 10:
            continue
        df = df.reset_index(drop=True)
        feat = build_features(df, btc, eth, tick_bps.get(sym, float("nan")))
        cost = symbol_cost_bps(settings, spreads.get(sym))
        high, low, close = df["high"].values, df["low"].values, df["close"].values
        for h in horizons:
            tp, sl, sig = barrier_bps(
                feat["vol_30"].values, h, cost, ag.tp_cost_multiple, ag.sl_sigma_multiple)
            y, out = triple_barrier(high, low, close, tp, sl, h)
            X = add_barrier_features(feat, tp, sl, cost)
            d = X.copy()
            d["symbol"], d["ts"] = sym, df["ts"].values
            d["tp"], d["sl"], d["cost"], d["y"], d["out_bps"] = tp, sl, cost, y, out
            d = d.iloc[MIN_BARS:]
            d = d[d["y"].notna() & d["vol_30"].gt(0) & d["btc_ret_5"].notna()]
            per_h[h].append(d)
    return {h: pd.concat(v, ignore_index=True) if v else pd.DataFrame() for h, v in per_h.items()}


# ------------------------------------------------------------------------------ splits
def split_times(ts_all: np.ndarray, train_frac: float, val_frac: float) -> tuple[int, int]:
    return int(np.quantile(ts_all, train_frac)), int(np.quantile(ts_all, train_frac + val_frac))


def split(d: pd.DataFrame, t1: int, t2: int, purge_ms: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Chronologisch; de labelhorizon valt nooit over een blokgrens (purge)."""
    train = d[d["ts"] <= t1 - purge_ms]
    val = d[(d["ts"] > t1) & (d["ts"] <= t2 - purge_ms)]
    test = d[d["ts"] > t2]
    return train, val, test


# ----------------------------------------------------------------------------- training
def train_horizon(train: pd.DataFrame, val: pd.DataFrame, settings: Settings) -> tuple[lgb.Booster, IsoCal, dict]:
    m = settings.model
    params = {"objective": "binary", "metric": "binary_logloss", "learning_rate": m.learning_rate,
              "num_leaves": m.num_leaves, "min_data_in_leaf": m.min_data_in_leaf, "feature_fraction": 0.8,
              "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 5.0, "verbose": -1, "seed": 7}
    dtr = lgb.Dataset(train[FEATURES].astype(float), train["y"].astype(float))
    dva = lgb.Dataset(val[FEATURES].astype(float),
                      val["y"].astype(float), reference=dtr)
    booster = lgb.train(params, dtr, num_boost_round=m.n_estimators, valid_sets=[dva],
                        callbacks=[lgb.early_stopping(50, verbose=False)])
    raw_val = booster.predict(val[FEATURES].astype(
        float), num_iteration=booster.best_iteration)
    cal = IsoCal.fit(raw_val, val["y"].values)
    from sklearn.metrics import brier_score_loss, roc_auc_score
    cal_val = cal.predict(raw_val)
    info = {"best_iteration": int(booster.best_iteration or 0), "val_brier": float(brier_score_loss(val["y"], cal_val)),
            "val_base_rate": float(val["y"].mean()),
            "val_auc": float(roc_auc_score(val["y"], raw_val)) if val["y"].nunique() > 1 else float("nan")}
    return booster, cal, info


# ---------------------------------------------------------------------------- evaluatie
def _greedy_nonoverlap(sig: pd.DataFrame, horizon_col: str = "h") -> pd.DataFrame:
    """Eén positie per symbool tegelijk: sla signalen over tot de vorige trade voorbij is."""
    keep = []
    for _sym, g in sig.sort_values("ts").groupby("symbol"):
        busy_until = -1
        for idx, ts, h in zip(g.index, g["ts"].values, g[horizon_col].values):
            if ts >= busy_until:
                keep.append(idx)
                busy_until = ts + int(h) * MIN_MS
    return sig.loc[keep]


def _trade_stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"n": 0, "mean_net_bps": float("nan"), "hit_rate": float("nan"), "total_net_bps": 0.0}
    net = t["net_bps"].values
    return {"n": int(len(t)), "mean_net_bps": float(net.mean()), "hit_rate": float((net > 0).mean()),
            "total_net_bps": float(net.sum())}


def _stack(datasets: dict[int, pd.DataFrame], bundle: ModelBundle, min_ev: float, min_p: float) -> pd.DataFrame:
    rows = []
    for h, d in datasets.items():
        if d.empty:
            continue
        p = bundle.predict(d, h)
        ev = p * d["tp"].values - (1 - p) * d["sl"].values - d["cost"].values
        rows.append(pd.DataFrame({"symbol": d["symbol"].values, "ts": d["ts"].values, "h": h, "p": p, "ev": ev,
                                  "net_bps": d["out_bps"].values - d["cost"].values, "y": d["y"].values}))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def evaluate_model(bundle: ModelBundle, test: dict[int, pd.DataFrame], settings: Settings) -> dict:
    dec, m = settings.decision, settings.model
    allr = _stack(test, bundle, dec.min_ev_bps, dec.min_p_win)
    if allr.empty:
        return {"n_rows": 0}
    best = allr.sort_values(
        "ev", ascending=False).drop_duplicates(["symbol", "ts"])
    sig = best[(best["ev"] >= dec.min_ev_bps) & (best["p"] >= dec.min_p_win)]
    trades = _greedy_nonoverlap(sig)
    stats = _trade_stats(trades)
    base_rate = float(allr["y"].mean())
    sig_hit = float(trades["y"].mean()) if len(trades) else float("nan")
    z = float("nan")
    if len(trades) > 5:
        # Is het aandeel TP-eerst op signalen hoger dan het onvoorwaardelijke aandeel (toeval)?
        z = (sig_hit - base_rate) / \
            math.sqrt(max(base_rate * (1 - base_rate), 1e-9) / len(trades))
    ics = {}
    for h, d in test.items():
        if len(d) > 100:
            p = bundle.predict(d, h)
            ics[h] = float(pd.Series(p).corr(
                pd.Series(d["out_bps"].values - d["cost"].values), method="spearman"))
    halves = {}
    if len(trades) >= 10:
        mid_ts = trades["ts"].median()
        halves = {"first_half_mean_net_bps": float(trades[trades["ts"] <= mid_ts]["net_bps"].mean()),
                  "second_half_mean_net_bps": float(trades[trades["ts"] > mid_ts]["net_bps"].mean())}
    return {"n_rows": int(len(allr)), "base_rate_tp": base_rate, "signal_tp_rate": sig_hit, "signal_z": z,
            "trades": stats, "ic_by_horizon": ics, "stability": halves,
            "per_symbol": {s: _trade_stats(g) for s, g in trades.groupby("symbol")}}


def baseline_trades(test: dict[int, pd.DataFrame], kind: str) -> dict:
    """Referentiestrategieën op dezelfde testdata; geeft de beste horizon terug."""
    best = {"n": 0, "mean_net_bps": float("-inf")}
    for h, d in test.items():
        if d.empty:
            continue
        if kind == "order_flow":
            mask = (d["volq_z_30"] >= 2.0) & (d["vol_ratio_5_30"] >= 1.5) & (
                d["ret_5"] > 0) & (d["dist_high_60"] > -10)
        else:  # naief: koop bij volumepiek, verkoop na H
            mask = d["volq_z_30"] >= 2.0
        t = d[mask]
        t = pd.DataFrame({"symbol": t["symbol"].values, "ts": t["ts"].values, "h": h,
                          "net_bps": t["out_bps"].values - t["cost"].values})
        if t.empty:
            continue
        s = _trade_stats(_greedy_nonoverlap(t))
        if s["n"] and s["mean_net_bps"] > best["mean_net_bps"]:
            best = {**s, "horizon": h}
    return best


def gate(evaluation: dict, baselines: dict, settings: Settings, min_ic: float = 0.08) -> dict:
    """Validatiepoort (Crypto Bot.md 10.4). `passed` alleen als alle criteria slagen."""
    t = evaluation.get("trades", {})
    n, mean = t.get("n", 0), t.get("mean_net_bps", float("nan"))
    ic_best = max(evaluation.get("ic_by_horizon", {}
                                 ).values(), default=float("nan"))
    checks = {
        "min_signals": n >= settings.model.min_test_signals,
        "positive_net": bool(mean == mean and mean > 0),
        "significant_hit": bool(evaluation.get("signal_z", float("nan")) >= 1.64),
        "ic": bool(ic_best == ic_best and ic_best >= min_ic),
        "beats_order_flow": bool(mean == mean and mean > baselines["order_flow"]["mean_net_bps"]),
        "beats_naive": bool(mean == mean and mean > baselines["naive"]["mean_net_bps"]),
    }
    return {"passed": all(checks.values()), "checks": checks, "ic_best": ic_best, "mean_net_bps": mean, "n": n}


def train_and_validate(frames: dict[str, pd.DataFrame], tick_bps: dict[str, float], spreads: dict[str, float],
                       settings: Settings, version: Optional[str] = None, min_ic: Optional[float] = None) -> tuple[ModelBundle, dict]:
    min_ic = settings.model.min_ic if min_ic is None else min_ic
    horizons = settings.agents.horizons_min
    data = build_dataset(frames, tick_bps, spreads, settings, horizons)
    all_ts = np.concatenate(
        [d["ts"].values for d in data.values() if not d.empty])
    t1, t2 = split_times(all_ts, settings.model.train_frac,
                         settings.model.val_frac)
    purge = max(settings.model.purge_min, max(horizons)) * MIN_MS
    version = version or "v" + time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    bundle = ModelBundle(version, horizons)
    info, test = {}, {}
    for h in horizons:
        tr, va, te = split(data[h], t1, t2, purge)
        if len(tr) < 500 or len(va) < 200:
            raise ValueError(
                f"Te weinig data voor horizon {h}: train={len(tr)}, val={len(va)}")
        booster, cal, hinfo = train_horizon(tr, va, settings)
        bundle.boosters[h], bundle.calibrators[h] = booster, cal
        info[h] = {
            **hinfo, "n_train": int(len(tr)), "n_val": int(len(va)), "n_test": int(len(te))}
        test[h] = te
    ev = evaluate_model(bundle, test, settings)
    base = {"order_flow": baseline_trades(
        test, "order_flow"), "naive": baseline_trades(test, "naive")}
    g = gate(ev, base, settings, min_ic)
    report = {"version": version, "horizons": info, "evaluation": ev, "baselines": base, "gate": g,
              "split": {"train_end_ms": t1, "val_end_ms": t2, "purge_min": purge // MIN_MS}}
    bundle.meta = {"gate": g, "evaluation": ev, "baselines": base, "horizon_info": info,
                   "config": {"tp_cost_multiple": settings.agents.tp_cost_multiple,
                              "sl_sigma_multiple": settings.agents.sl_sigma_multiple},
                   "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    return bundle, report


def write_report(settings: Settings, report: dict) -> Path:
    p = settings.path("reports")
    p.mkdir(parents=True, exist_ok=True)
    f = p / f"{report['version']}.json"
    f.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return f
