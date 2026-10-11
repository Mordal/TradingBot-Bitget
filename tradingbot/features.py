"""Features (identiek voor training en live) en barrière-berekening."""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

BASE_FEATURES = [
    "ret_1", "ret_5", "ret_15", "ret_30", "ret_60",
    "vol_5", "vol_30", "vol_120", "vol_240", "vol_ratio_5_30", "vol_ratio_30_120",
    "volq_ratio_5_60", "volq_z_30",
    "dist_high_60", "dist_low_60", "dist_high_240", "dist_low_240", "range_5",
    "tick_bps",
    "btc_ret_5", "btc_ret_30", "btc_vol_30", "eth_ret_5", "eth_ret_30",
    "hour_sin", "hour_cos",
]
# Barrière-afhankelijke inputs; het model voorspelt P(TP eerst | features, TP/σ).
BARRIER_FEATURES = ["tp_sigma_ratio", "cost_sigma_ratio"]
FEATURES = BASE_FEATURES + BARRIER_FEATURES
MIN_BARS = 260  # vol_240 + marge


def _context(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if df is None or len(df) < 40:
        return None
    c = np.log(df["close"].astype(float))
    lr = c.diff() * 1e4
    out = pd.DataFrame({
        "ret_5": (c - c.shift(5)) * 1e4, "ret_30": (c - c.shift(30)) * 1e4, "vol_30": lr.rolling(30).std()})
    out.index = df["ts"].values
    return out


def build_features(df: pd.DataFrame, btc: Optional[pd.DataFrame] = None, eth: Optional[pd.DataFrame] = None,
                   tick_bps: float = float("nan")) -> pd.DataFrame:
    """df: kolommen ts, open, high, low, close, quote_vol (oplopend, 1 minuut). Rij t gebruikt alleen data <= t."""
    c = df["close"].astype(float)
    lc = np.log(c)
    lr = lc.diff() * 1e4
    qv = df["quote_vol"].astype(float)
    out = pd.DataFrame(index=df.index)
    for k in (1, 5, 15, 30, 60):
        out[f"ret_{k}"] = (lc - lc.shift(k)) * 1e4
    for w in (5, 30, 120, 240):
        out[f"vol_{w}"] = lr.rolling(w).std()
    out["vol_ratio_5_30"] = out["vol_5"] / out["vol_30"].replace(0, np.nan)
    out["vol_ratio_30_120"] = out["vol_30"] / out["vol_120"].replace(0, np.nan)
    out["volq_ratio_5_60"] = np.log(
        (qv.rolling(5).mean() + 1) / (qv.rolling(60).mean() + 1))
    lv = np.log1p(qv)
    out["volq_z_30"] = (lv - lv.rolling(30).mean()) / \
        lv.rolling(30).std().replace(0, np.nan)
    for w in (60, 240):
        out[f"dist_high_{w}"] = (c / df["high"].rolling(w).max() - 1) * 1e4
        out[f"dist_low_{w}"] = (c / df["low"].rolling(w).min() - 1) * 1e4
    out["range_5"] = ((df["high"] - df["low"]) / c).rolling(5).mean() * 1e4
    out["tick_bps"] = tick_bps
    ts = df["ts"].values
    for name, ctx in (("btc", _context(btc)), ("eth", _context(eth))):
        cols = ("ret_5", "ret_30", "vol_30") if name == "btc" else (
            "ret_5", "ret_30")
        for col in cols:
            key = f"{name}_{col}"
            if ctx is None:
                out[key] = np.nan
            else:
                out[key] = ctx[col].reindex(
                    ts, method="ffill", tolerance=5 * 60_000).values
    minute_of_day = (ts // 60_000) % 1440
    ang = 2 * np.pi * minute_of_day / 1440
    out["hour_sin"] = np.sin(ang)
    out["hour_cos"] = np.cos(ang)
    return out


def barrier_bps(vol_1m_bps, horizon_min: int, cost_bps: float, tp_cost_multiple: float, sl_sigma_multiple: float):
    """(TP, SL, sigma_H) in bps; werkt voor scalars en arrays. TP dekt minstens `tp_cost_multiple` x kosten."""
    sigma_h = np.asarray(vol_1m_bps, dtype=float) * math.sqrt(horizon_min)
    tp = np.maximum(tp_cost_multiple * cost_bps, sigma_h)
    sl = sl_sigma_multiple * sigma_h
    if np.ndim(vol_1m_bps) == 0:
        return float(tp), float(sl), float(sigma_h)
    return tp, sl, sigma_h


def add_barrier_features(feat: pd.DataFrame, tp: np.ndarray | float, sl: np.ndarray | float,
                         cost_bps: float) -> pd.DataFrame:
    out = feat.copy()
    sigma = np.maximum(np.asarray(sl, dtype=float), 1e-9)
    out["tp_sigma_ratio"] = np.asarray(tp, dtype=float) / sigma
    out["cost_sigma_ratio"] = cost_bps / sigma
    return out[FEATURES]
