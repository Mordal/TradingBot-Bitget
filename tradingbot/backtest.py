"""Event-replay: opgenomen marktdata door dezelfde bot-code met de fill-simulator (zelfde agents/risk/execution als live)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from .bot import TradingBot
from .config import Settings
from .exchange.base import Candle, Instrument
from .fillsim import SimGateway
from .marketdata import MIN_MS, MarketState
from .model import ModelBundle
from .portfolio import Portfolio
from .ratelimit import TokenBucket
from .recorder import read_recordings
from .storage import Storage


class VirtualClock:
    def __init__(self, t: int = 0):
        self.t = t

    def __call__(self) -> int:
        return self.t


def seed_from_frames(ms: MarketState, frames: dict[str, pd.DataFrame], until_ms: int) -> None:
    """Vult de bars met historie tot vlak vóór het begin van de opname (nodig voor 260+ bars aan features)."""
    cut = until_ms // MIN_MS * MIN_MS
    for sym, df in frames.items():
        d = df[df["ts"] < cut].tail(600)
        ms.seed_bars(sym, [Candle(int(r.ts), r.open, r.high, r.low, r.close, 0.0, r.quote_vol)
                           for r in d.itertuples()])
        st = ms.inst.get(sym)
        if st is not None and st.bars:
            st.cur = None   # de eerste trade bepaalt de lopende minuut


async def replay(settings: Settings, recordings: Path, instruments: dict[str, Instrument], active: list[str],
                 bundle: Optional[ModelBundle] = None, seed_frames: Optional[dict[str, pd.DataFrame]] = None,
                 start_usdt: float = 1000.0, tick_ms: Optional[int] = None, start_ms: int = 0,
                 end_ms: int = 1 << 62) -> dict:
    events = read_recordings(recordings, start_ms, end_ms)
    first = next(events, None)
    if first is None:
        return {"error": "geen opnames gevonden"}
    clock = VirtualClock(first["t"])
    ms = MarketState(clock)
    db = Storage(":memory:")
    sim = SimGateway(settings, ms, instruments, clock, start_usdt,
                     settings.bot.shadow_latency_ms, auto=False)
    pf = Portfolio(start_usdt)
    bot = TradingBot(settings, clock, db, pf, sim, instruments,
                     bundle, ms=ms, use_threads=False)
    bot.engine.bucket = TokenBucket(1e9)
    bot.engine.start()
    if seed_frames:
        seed_from_frames(ms, seed_frames, first["t"])
    bot.set_universe(active)
    tick = tick_ms or settings.decision.decision_interval_ms
    last_tick = first["t"]
    n_events = 0

    async def pump() -> None:
        for _ in range(4):
            await asyncio.sleep(0)

    def feed(ev: dict) -> None:
        bot.on_ws(ev["c"], ev["s"], ev["a"], ev["d"])

    feed(first)
    equity_curve: list[tuple[int, float]] = []
    for ev in events:
        clock.t = ev["t"]
        feed(ev)
        n_events += 1
        if clock.t - last_tick >= tick:
            last_tick = clock.t
            await sim.process(clock.t)
            await bot.tick()
            await pump()
            await sim.process(clock.t)
            await pump()
            equity_curve.append((clock.t, pf.equity(bot._marks())))
    await bot.engine.stop()
    return _report(bot, db, pf, equity_curve, start_usdt, n_events, first["t"], clock.t)


def _report(bot: TradingBot, db: Storage, pf: Portfolio, curve: list, start: float, n_events: int, t0: int,
            t1: int) -> dict:
    trades = db.query("SELECT * FROM trades_closed")
    pnl = [r["pnl_usdt"] for r in trades]
    bps = [r["pnl_bps"] for r in trades]
    eq = np.array([e for _t, e in curve]) if curve else np.array([start])
    peak = np.maximum.accumulate(eq)
    reasons: dict[str, int] = {}
    for r in trades:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    return {
        "events": n_events, "duration_h": (t1 - t0) / 3.6e6, "trades": len(trades),
        "net_pnl_usdt": float(sum(pnl)), "mean_pnl_bps": float(np.mean(bps)) if bps else None,
        "hit_rate": float(np.mean([p > 0 for p in pnl])) if pnl else None,
        "max_drawdown": float(((peak - eq) / peak).max()), "final_equity": float(eq[-1]),
        "open_positions": list(pf.positions), "counters": dict(bot.counters), "exit_reasons": reasons,
        "alerts": bot.engine.alerts[:10],
    }
