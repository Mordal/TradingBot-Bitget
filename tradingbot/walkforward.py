"""Walk-forward simulatie ("alsof live") op historische 1-minuutcandles.

Per stap (standaard wekelijks) wordt het model hertraind op uitsluitend het verleden (rollend venster, purged splits,
poort op een achtergehouden deel), waarna de eerstvolgende week "live" wordt doorlopen: elke minuut dezelfde features,
dezelfde EV-regel, dezelfde risicolimieten en dezelfde lifecycle (shadow/active) als de echte bot. Uitstappen volgen de
triple-barrière (take-profit, stop, tijd) op de candle-highs en -lows; stop telt eerst als beide in één bar vallen.

Beperkingen (historisch bestaan geen orderboeken of trades): geen order-flow-agent, geen diepte- of spreadinformatie
(spread = momentopname van vandaag), instap op de slotkoers plus kosten. Orderboekfeatures komen uit de recorder.
"""
from __future__ import annotations

import heapq
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import pandas as pd

from .config import Settings
from .features import BASE_FEATURES, FEATURES, MIN_BARS, barrier_bps, build_features
from .model import ModelBundle
from .training import (baseline_trades, evaluate_model, gate, split, split_times, symbol_cost_bps, train_horizon,
                       triple_barrier)

log = logging.getLogger(__name__)
MIN_MS = 60_000
DAY_MS = 86_400_000


# ------------------------------------------------------------------------------------ data
@dataclass
class WFData:
    symbols: list[str]
    sym: np.ndarray                    # int16 index in symbols
    ts: np.ndarray                     # int64 ms
    F: np.ndarray                      # float32 (n, len(BASE_FEATURES))
    cost: np.ndarray                   # float32
    per_h: dict[int, dict[str, np.ndarray]]   # tp, sl, y, out, dur (float32)
    valid: dict[int, np.ndarray]

    @property
    def n(self) -> int:
        return len(self.ts)


def build_wf_data(frames: dict[str, pd.DataFrame], tick_bps: dict[str, float], spreads: dict[str, float],
                  settings: Settings, horizons: list[int]) -> WFData:
    btc, eth = frames.get("BTCUSDT"), frames.get("ETHUSDT")
    ag = settings.agents
    syms, sym_l, ts_l, F_l, cost_l = [], [], [], [], []
    per: dict[int, dict[str, list]] = {
        h: {k: [] for k in ("tp", "sl", "y", "out", "dur", "ok")} for h in horizons}
    for name, df in frames.items():
        if len(df) < MIN_BARS + max(horizons) + 10:
            continue
        df = df.reset_index(drop=True)
        feat = build_features(df, btc, eth, tick_bps.get(name, float("nan")))
        cost = symbol_cost_bps(settings, spreads.get(name))
        high, low, close = df["high"].values, df["low"].values, df["close"].values
        vol30 = feat["vol_30"].values
        base_ok = (vol30 > 0) & feat["btc_ret_5"].notna().values
        base_ok[:MIN_BARS] = False
        k = len(syms)
        syms.append(name)
        sym_l.append(np.full(len(df), k, dtype=np.int16))
        ts_l.append(df["ts"].values.astype(np.int64))
        F_l.append(feat[BASE_FEATURES].to_numpy(dtype=np.float32))
        cost_l.append(np.full(len(df), cost, dtype=np.float32))
        for h in horizons:
            tp, sl, _ = barrier_bps(
                vol30, h, cost, ag.tp_cost_multiple, ag.sl_sigma_multiple)
            y, out, dur = triple_barrier(
                high, low, close, tp, sl, h, return_duration=True)
            d = per[h]
            d["tp"].append(tp.astype(np.float32))
            d["sl"].append(sl.astype(np.float32))
            d["y"].append(y.astype(np.float32))
            d["out"].append(out.astype(np.float32))
            d["dur"].append(dur.astype(np.float32))
            d["ok"].append(base_ok & ~np.isnan(y))
    per_h = {h: {k: np.concatenate(v) for k, v in d.items(
    ) if k != "ok"} for h, d in per.items()}
    valid = {h: np.concatenate(per[h]["ok"]) for h in horizons}
    return WFData(syms, np.concatenate(sym_l), np.concatenate(ts_l), np.concatenate(F_l), np.concatenate(cost_l),
                  per_h, valid)


def rows(wf: WFData, h: Optional[int], lo: int, hi: int, stride: int = 1, all_horizons: bool = False) -> np.ndarray:
    m = (wf.ts >= lo) & (wf.ts < hi)
    if stride > 1:
        m &= (wf.ts // MIN_MS) % stride == 0
    if all_horizons:
        for hh in wf.valid:
            m &= wf.valid[hh]
    elif h is not None:
        m &= wf.valid[h]
    return np.flatnonzero(m)


def make_X(wf: WFData, h: int, idx: np.ndarray) -> pd.DataFrame:
    d = pd.DataFrame(wf.F[idx], columns=BASE_FEATURES)
    sl = np.maximum(wf.per_h[h]["sl"][idx], 1e-9)
    d["tp_sigma_ratio"] = wf.per_h[h]["tp"][idx] / sl
    d["cost_sigma_ratio"] = wf.cost[idx] / sl
    return d[FEATURES]


def make_frame(wf: WFData, h: int, idx: np.ndarray) -> pd.DataFrame:
    d = make_X(wf, h, idx)
    d["symbol"] = np.array(wf.symbols)[wf.sym[idx]]
    d["ts"] = wf.ts[idx]
    d["tp"], d["sl"], d["cost"] = wf.per_h[h]["tp"][idx], wf.per_h[h]["sl"][idx], wf.cost[idx]
    d["y"], d["out_bps"] = wf.per_h[h]["y"][idx], wf.per_h[h]["out"][idx]
    return d


# --------------------------------------------------------------------------------- hertrainen
def retrain(wf: WFData, t_end: int, settings: Settings, train_days: int, stride: int, version: str):
    """Train op [t_end - train_days, t_end) met purge en poort op de laatste 20%; geeft (bundle, report)."""
    horizons = settings.agents.horizons_min
    t_start = t_end - train_days * DAY_MS
    purge = max(settings.model.purge_min, max(horizons)) * MIN_MS
    # tijdsgrenzen uit de beschikbare tijdstippen van het venster
    in_win = wf.ts[(wf.ts >= t_start) & (wf.ts < t_end)]
    t1, t2 = split_times(in_win[:: max(
        1, len(in_win) // 200_000)], settings.model.train_frac, settings.model.val_frac)
    bundle = ModelBundle(version, horizons)
    info, test = {}, {}
    for h in horizons:
        end_h = t_end - h * MIN_MS              # labels mogen niet voorbij t_end kijken
        idx_tr = rows(wf, h, t_start, min(t1 - purge, end_h), stride)
        idx_va = rows(wf, h, t1, min(t2 - purge, end_h), stride)
        idx_te = rows(wf, h, t2, end_h, max(2, stride))
        if len(idx_tr) < 2000 or len(idx_va) < 500:
            raise ValueError(
                f"te weinig data voor horizon {h}: train={len(idx_tr)}, val={len(idx_va)}")
        tr, va = make_frame(wf, h, idx_tr), make_frame(wf, h, idx_va)
        booster, cal, hinfo = train_horizon(tr, va, settings)
        bundle.boosters[h], bundle.calibrators[h] = booster, cal
        info[h] = {
            **hinfo, "n_train": len(idx_tr), "n_val": len(idx_va), "n_test": len(idx_te)}
        test[h] = make_frame(wf, h, idx_te)
    ev = evaluate_model(bundle, test, settings)
    base = {"order_flow": baseline_trades(
        test, "order_flow"), "naive": baseline_trades(test, "naive")}
    g = gate(ev, base, settings)
    bundle.meta = {"gate": g, "evaluation": ev, "baselines": base, "horizon_info": info,
                   "config": {"tp_cost_multiple": settings.agents.tp_cost_multiple,
                              "sl_sigma_multiple": settings.agents.sl_sigma_multiple},
                   "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    report = {"version": version, "horizons": info,
              "evaluation": ev, "baselines": base, "gate": g}
    return bundle, report


# ------------------------------------------------------------------------------------ signalen
def week_signals(wf: WFData, bundle: ModelBundle, lo: int, hi: int, settings: Settings, vidx: int) -> pd.DataFrame:
    """Alle EV-signalen in [lo, hi): per minuut en instrument de horizon met de hoogste EV."""
    dec = settings.decision
    horizons = bundle.horizons
    idx = rows(wf, None, lo, hi, 1, all_horizons=True)
    if len(idx) == 0:
        return pd.DataFrame()
    P = np.empty((len(horizons), len(idx)), np.float32)
    EV = np.empty_like(P)
    for k, h in enumerate(horizons):
        p = bundle.predict(make_X(wf, h, idx), h).astype(np.float32)
        P[k] = p
        EV[k] = p * wf.per_h[h]["tp"][idx] - \
            (1 - p) * wf.per_h[h]["sl"][idx] - wf.cost[idx]
    best = EV.argmax(0)
    cols = np.arange(len(idx))
    ev, p = EV[best, cols], P[best, cols]
    keep = (ev >= dec.min_ev_bps) & (p >= dec.min_p_win)
    idx, best, ev, p = idx[keep], best[keep], ev[keep], p[keep]
    if len(idx) == 0:
        return pd.DataFrame()
    hs = np.array(horizons)[best]
    out, dur, y = np.empty(len(idx)), np.empty(len(idx)), np.empty(len(idx))
    tp, sl = np.empty(len(idx)), np.empty(len(idx))
    for h in horizons:
        m = hs == h
        out[m], dur[m], y[m] = wf.per_h[h]["out"][idx[m]
                                                  ], wf.per_h[h]["dur"][idx[m]], wf.per_h[h]["y"][idx[m]]
        tp[m], sl[m] = wf.per_h[h]["tp"][idx[m]], wf.per_h[h]["sl"][idx[m]]
    cost = wf.cost[idx]
    return pd.DataFrame({
        "ts": wf.ts[idx], "sym": np.array(wf.symbols)[wf.sym[idx]], "h": hs, "p": p, "ev": ev, "tp": tp, "sl": sl,
        "cost": cost, "net_bps": out - cost, "y": y, "end_ts": wf.ts[idx] + (dur * MIN_MS).astype(np.int64),
        "version": vidx})


# ---------------------------------------------------------------------------------- lifecycle
def lifecycle_flags(sig: pd.DataFrame, gate_passed: dict[int, bool], settings: Settings, carry: bool) -> np.ndarray:
    """Zou de EDGE-agent op dit signaalmoment actief zijn geweest? Zelfde regels als `Feedback._update_lifecycle`.

    Een outcome telt pas mee zodra de trade is afgelopen (`end_ts`). Zonder `carry` begint elke nieuwe modelversie
    opnieuw (actief alleen als de poort slaagde); met `carry` blijven status en venster over de versies heen bestaan.
    """
    m = settings.model
    flags = np.zeros(len(sig), dtype=bool)
    status: dict[int, str] = {}
    window: dict[int, deque] = {}
    # (end_ts, seq, scope, correct, net)
    pending: list[tuple[int, int, int, bool, float]] = []
    seen_versions: set[int] = set()
    seq = 0

    def scope_of(v: int) -> int:
        return 0 if carry else v

    for i, (ts, end_ts, v, net) in enumerate(zip(sig["ts"].values, sig["end_ts"].values, sig["version"].values,
                                                 sig["net_bps"].values)):
        sc = scope_of(int(v))
        if v not in seen_versions:
            seen_versions.add(int(v))
            if not carry or sc not in status:
                status[sc] = "active" if gate_passed.get(
                    int(v), False) else "shadow"
                window[sc] = deque(maxlen=m.shadow_demote_n)
            elif gate_passed.get(int(v), False):
                status[sc] = "active"
        while pending and pending[0][0] <= ts:
            _e, _s, psc, correct, pnet = heapq.heappop(pending)
            w = window[psc]
            w.append((correct, pnet))
            n = len(w)
            hit = sum(1 for c, _ in w if c) / n
            mean = sum(x for _, x in w) / n
            if status[psc] == "shadow" and n >= m.shadow_activate_n and hit >= m.shadow_activate_hit and mean > 0:
                status[psc] = "active"
            elif status[psc] == "active" and n >= m.shadow_demote_n and (hit < m.shadow_demote_hit or mean < 0):
                status[psc] = "shadow"
        flags[i] = status[sc] == "active"
        seq += 1
        heapq.heappush(pending, (int(end_ts), seq,
                       sc, bool(net > 0), float(net)))
    return flags


# --------------------------------------------------------------------------------- portefeuille
def simulate_portfolio(sig: pd.DataFrame, settings: Settings, start_equity: float,
                       allowed: Optional[np.ndarray] = None) -> dict:
    """Past de risicolimieten en positiegroottes toe op de signalen; P&L uit de barrière-uitkomsten."""
    r, d = settings.risk, settings.decision
    if sig.empty:
        return {"trades": 0, "final_equity": start_equity, "net_pnl_usdt": 0.0, "trade_list": []}
    s = sig.assign(allowed_=True if allowed is None else allowed).sort_values(
        "ts", kind="stable")
    cash = start_equity
    # (end_ts, sym, notional, pnl)
    open_pos: list[tuple[int, str, float, float]] = []
    open_syms: set[str] = set()
    cooldown: dict[str, int] = {}
    buys: deque[int] = deque()
    eq_hist: deque[tuple[int, float]] = deque()
    day_key, day_start = None, start_equity
    trades: list[dict] = []
    equity_points: list[tuple[int, float]] = []
    # piek = begin-equity, zoals de live equity-markering
    eq_hist.append((int(s["ts"].iloc[0]), start_equity))

    def close_until(t: int) -> None:
        nonlocal cash
        while open_pos and open_pos[0][0] <= t:
            end, sym, notional, pnl = heapq.heappop(open_pos)
            cash += notional + pnl
            open_syms.discard(sym)
            eq = cash + sum(o[2] for o in open_pos)
            equity_points.append((end, eq))
            eq_hist.append((end, eq))

    for row in s.itertuples(index=False):
        t = int(row.ts)
        close_until(t)
        while eq_hist and eq_hist[0][0] < t - DAY_MS:
            eq_hist.popleft()
        invested = sum(o[2] for o in open_pos)
        equity = cash + invested
        dk = t // DAY_MS
        if dk != day_key:
            day_key, day_start = dk, equity
        if not row.allowed_ or row.sym in open_syms or t < cooldown.get(row.sym, 0):
            continue
        if len(open_pos) >= r.max_open_positions:
            continue
        while buys and t - buys[0] > 3600_000:
            buys.popleft()
        if len(buys) >= r.max_buys_per_hour or sum(1 for b in buys if t - b <= 60_000) >= r.max_buys_per_min:
            continue
        peak = max([e for _t, e in eq_hist] + [equity])
        dd = 1 - equity / peak
        if dd >= r.dd_hard or (day_start > 0 and 1 - equity / day_start >= r.daily_loss_pct):
            continue
        notional = d.base_order_usdt * min(1.0, row.ev / d.ev_ref_bps)
        if dd >= r.dd_soft:
            notional *= 0.5
        notional = min(notional, d.max_order_usdt, r.max_position_usdt, r.max_asset_share * equity,
                       r.max_total_exposure_pct * equity - invested, cash - r.min_usdt_reserve)
        if notional < 1.0:
            continue
        pnl = notional * row.net_bps / 1e4
        cash -= notional
        heapq.heappush(open_pos, (int(row.end_ts), row.sym, notional, pnl))
        open_syms.add(row.sym)
        buys.append(t)
        if row.y == 0 and row.net_bps <= -row.sl - row.cost + 1e-6:       # stop geraakt: afkoelperiode
            cooldown[row.sym] = int(row.end_ts) + r.stop_cooldown_s * 1000
        trades.append({"ts": t, "sym": row.sym, "h": int(row.h), "notional": notional, "net_bps": float(row.net_bps),
                       "pnl_usdt": pnl, "end_ts": int(row.end_ts), "ev": float(row.ev), "p": float(row.p),
                       "version": int(row.version)})
    close_until(1 << 62)
    final = cash
    pnl = np.array([x["pnl_usdt"] for x in trades])
    eq = np.array([e for _t, e in equity_points]
                  ) if equity_points else np.array([start_equity])
    peak = np.maximum.accumulate(np.r_[start_equity, eq])
    dd = float(((peak - np.r_[start_equity, eq]) / peak).max())
    daily: dict[int, float] = {}
    for x in trades:
        daily[x["end_ts"] //
              DAY_MS] = daily.get(x["end_ts"] // DAY_MS, 0.0) + x["pnl_usdt"]
    dv = np.array(list(daily.values())) if daily else np.array([0.0])
    gains, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    return {
        "trades": len(trades), "final_equity": float(final), "net_pnl_usdt": float(final - start_equity),
        "return_pct": float((final / start_equity - 1) * 100),
        "hit_rate": float((pnl > 0).mean()) if len(pnl) else None,
        "mean_net_bps": float(np.mean([x["net_bps"] for x in trades])) if trades else None,
        "profit_factor": float(gains / losses) if losses > 0 else None,
        "max_drawdown": dd,
        "sharpe_daily_ann": float(dv.mean() / dv.std() * np.sqrt(365)) if len(dv) > 2 and dv.std() > 0 else None,
        "trade_list": trades}


# -------------------------------------------------------------------------------------- run
def run_walkforward(frames: dict[str, pd.DataFrame], tick_bps: dict[str, float], spreads: dict[str, float],
                    settings: Settings, warmup_days: int = 30, train_days: int = 60, retrain_days: int = 7,
                    stride: int = 3, start_equity: Optional[float] = None,
                    progress: Optional[Callable[[str], None]] = None) -> tuple[dict, Optional[ModelBundle]]:
    """Geeft (resultaat, eindmodel). Het eindmodel is getraind op het laatste venster tot nu."""
    say = progress or (lambda m: None)
    horizons = settings.agents.horizons_min
    start_equity = start_equity or settings.bot.shadow_start_usdt
    t0 = time.time()
    wf = build_wf_data(frames, tick_bps, spreads, settings, horizons)
    data_start, data_end = int(wf.ts.min()), int(wf.ts.max())
    say(f"data: {len(wf.symbols)} symbolen, {wf.n:,} rijen, {(data_end - data_start) / DAY_MS:.0f} dagen "
        f"({time.time() - t0:.0f}s)")
    first_t = data_start + warmup_days * DAY_MS + MIN_BARS * MIN_MS
    steps = []
    t = first_t
    while t + 1 * DAY_MS <= data_end:
        steps.append((t, min(t + retrain_days * DAY_MS, data_end)))
        t += retrain_days * DAY_MS
    sigs, weeks, gate_passed, bundles = [], [], {}, {}
    for k, (lo, hi) in enumerate(steps):
        ts0 = time.time()
        version = f"wf{k:02d}"
        try:
            bundle, rep = retrain(
                wf, lo, settings, train_days, stride, version)
        except ValueError as e:
            say(f"[{k + 1}/{len(steps)}] overgeslagen: {e}")
            continue
        g = rep["gate"]
        gate_passed[k] = bool(g["passed"])
        s = week_signals(wf, bundle, lo, hi, settings, k)
        n_sig = len(s)
        sigs.append(s)
        day = time.strftime("%Y-%m-%d", time.gmtime(lo / 1000))
        mean = float(s["net_bps"].mean()) if n_sig else float("nan")
        weeks.append({"version": version, "from": day, "signals": n_sig, "gate_passed": gate_passed[k],
                      "gate_checks": g["checks"], "gate_test_mean_net_bps": g["mean_net_bps"],
                      "mean_net_bps_all_signals": mean,
                      "ic_best": g["ic_best"]})
        say(f"[{k + 1}/{len(steps)}] {day} poort={'JA ' if gate_passed[k] else 'nee'} "
            f"signalen={n_sig:>5} gem.netto(oos)={mean:7.1f} bps ({time.time() - ts0:.0f}s)")
    sig = pd.concat([s for s in sigs if len(s)], ignore_index=True) if any(
        len(s) for s in sigs) else pd.DataFrame()
    result = {"period": {"data_start": data_start, "data_end": data_end, "first_trade_ms": first_t,
                         "days_simulated": (data_end - first_t) / DAY_MS},
              "settings": {"warmup_days": warmup_days, "train_days": train_days, "retrain_days": retrain_days,
                           "stride": stride, "start_equity": start_equity},
              "symbols": wf.symbols, "weeks": weeks}
    if sig.empty:
        result["variants"] = {}
        return result, _final_model(wf, data_end, settings, train_days, stride, result, say)
    sig = sig.sort_values("ts", kind="stable").reset_index(drop=True)
    variants = {}
    flags_strict = lifecycle_flags(sig, gate_passed, settings, carry=False)
    flags_carry = lifecycle_flags(sig, gate_passed, settings, carry=True)
    for name, allowed in (("potentieel_alle_signalen", None), ("lifecycle_strikt", flags_strict),
                          ("lifecycle_doorlopend", flags_carry)):
        res = simulate_portfolio(sig, settings, start_equity, allowed)
        res["signals_allowed"] = int(
            len(sig) if allowed is None else allowed.sum())
        variants[name] = res
    result["variants"] = variants
    result["signals_total"] = int(len(sig))
    result["signal_stats"] = {
        "mean_net_bps": float(sig["net_bps"].mean()), "hit_rate": float((sig["net_bps"] > 0).mean()),
        "tp_rate": float(sig["y"].mean()), "mean_ev_pred": float(sig["ev"].mean()),
        "by_horizon": {int(h): {"n": int(len(g)), "mean_net_bps": float(g["net_bps"].mean())}
                       for h, g in sig.groupby("h")}}
    return result, _final_model(wf, data_end, settings, train_days, stride, result, say)


def _final_model(wf: WFData, data_end: int, settings: Settings, train_days: int, stride: int, result: dict,
                 say: Callable[[str], None]) -> Optional[ModelBundle]:
    try:
        bundle, rep = retrain(wf, data_end, settings, train_days,
                              stride, "v" + time.strftime("%Y%m%dT%H%M%S", time.gmtime()))
    except ValueError as e:
        say(f"eindmodel niet getraind: {e}")
        return None
    result["final_model"] = {"version": bundle.version, "gate": rep["gate"], "horizons": rep["horizons"],
                             "evaluation": rep["evaluation"], "baselines": rep["baselines"]}
    say(
        f"eindmodel {bundle.version}: poort {'GESLAAGD' if rep['gate']['passed'] else 'niet gehaald'} {rep['gate']['checks']}")
    return bundle


def summarize(result: dict) -> str:
    lines = [f"periode: {result['period']['days_simulated']:.0f} dagen gesimuleerd, {len(result['symbols'])} paren, "
             f"{len(result['weeks'])} hertrainingen, poort geslaagd in {sum(w['gate_passed'] for w in result['weeks'])}"]
    if not result.get("variants"):
        lines.append("geen enkel signaal haalde de EV-drempel")
        return "\n".join(lines)
    st = result["signal_stats"]
    lines.append(f"signalen: {result['signals_total']} (gem. netto {st['mean_net_bps']:.1f} bps, hit {st['hit_rate']:.2%}, "
                 f"TP-rate {st['tp_rate']:.2%}, gem. voorspelde EV {st['mean_ev_pred']:.1f} bps)")
    for name, v in result["variants"].items():
        lines.append(f"{name}: trades={v['trades']} eind-equity={v['final_equity']:.2f} "
                     f"rendement={v.get('return_pct', 0):.2f}% hit={v.get('hit_rate')} gem.netto={v.get('mean_net_bps')} "
                     f"maxDD={v.get('max_drawdown', 0):.2%} PF={v.get('profit_factor')} sharpe={v.get('sharpe_daily_ann')}")
    return "\n".join(lines)
