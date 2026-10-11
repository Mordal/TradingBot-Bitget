"""AGENT_UNIVERSE: welke instrumenten volgen we, met hysterese en EXIT_ONLY voor open posities."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from ..config import UniverseCfg
from ..costs import roundtrip_cost_bps
from ..exchange.base import ExchangeError, Instrument

log = logging.getLogger(__name__)

AGENT_ID = "AGENT_UNIVERSE"


@dataclass
class Candidate:
    symbol: str
    base: str
    volume_usdt: float
    spread_bps: float
    # σ van 1-minuutrendementen over de laatste ~16 uur
    sd1m_recent: float = float("nan")
    sd1m_7d: float = float("nan")       # afgeleid uit uurcandles
    depth_bid: float = 0.0
    depth_ask: float = 0.0
    depth10_bid: float = 0.0
    depth10_ask: float = 0.0
    history_days: float = 0.0
    listing_age_days: float = 0.0
    status: str = "online"
    score: float = 0.0
    fails: list[str] = field(default_factory=list)


def evaluate(c: Candidate, cfg: UniverseCfg) -> list[str]:
    """Geeft de lijst van niet-gehaalde criteria (leeg = geschikt). Zie Crypto Bot.md 3.2."""
    f: list[str] = []
    if c.status != "online":
        f.append("not_online")
    if c.volume_usdt < cfg.min_volume_24h_usdt:
        f.append("volume")
    if not (c.spread_bps <= cfg.max_spread_bps):
        f.append("spread")
    sd = c.sd1m_recent
    if not (sd == sd) or sd < cfg.min_sd1m_bps:
        f.append("sd1m_low")
    if sd == sd and sd > cfg.max_sd1m_bps:
        f.append("sd1m_extreme")
    if sd == sd and c.sd1m_7d == c.sd1m_7d and c.sd1m_7d > 0 and sd / c.sd1m_7d > cfg.spike_ratio:
        f.append("spike")
    if c.spread_bps < 0.05 and sd == sd and sd >= 15:
        f.append("suspicious_spread")
    if min(c.depth_bid, c.depth_ask) < cfg.min_depth_usdt:
        f.append("depth")
    if c.history_days < cfg.min_history_days or c.listing_age_days < cfg.min_history_days:
        f.append("history")
    return f


def score(c: Candidate, cost_bps: float) -> float:
    """Kostenbewuste kansscore: σ over 30 min gedeeld door de rondrit-kosten."""
    if not (c.sd1m_recent == c.sd1m_recent) or cost_bps <= 0:
        return 0.0
    return c.sd1m_recent * math.sqrt(30) / cost_bps


@dataclass
class UniverseResult:
    active: list[str]
    exit_only: set[str]
    candidates: list[Candidate] = field(default_factory=list)
    ts_ms: int = 0


class UniverseSelector:
    """Hysterese: wisselen pas na `hysteresis_checks` opeenvolgende controles."""

    def __init__(self, cfg: UniverseCfg):
        self.cfg = cfg
        self.members: dict[str, int] = {}     # symbool -> opeenvolgende misses
        # kandidaat -> opeenvolgende keren in top
        self.pending: dict[str, int] = {}
        self.exit_only: set[str] = set()

    def update(self, ranked: list[str], emergency: set[str], positions: set[str]) -> tuple[list[str], set[str]]:
        n, top = self.cfg.hysteresis_checks, self.cfg.top_n
        wanted = ranked[:top]
        wanted_set = set(wanted)
        for m in list(self.members):
            if m in emergency:
                del self.members[m]
                continue
            self.members[m] = 0 if m in wanted_set else self.members[m] + 1
            if self.members[m] >= n:
                del self.members[m]
        cold = not self.members
        for w in wanted:
            if w in self.members:
                continue
            self.pending[w] = self.pending.get(w, 0) + 1
            if cold or self.pending[w] >= n:
                if len(self.members) < top:
                    self.members[w] = 0
                    self.pending.pop(w, None)
                else:
                    weak = [m for m, miss in self.members.items(
                    ) if miss > 0 and m not in positions]
                    if weak:
                        del self.members[weak[0]]
                        self.members[w] = 0
                        self.pending.pop(w, None)
        for p in list(self.pending):
            if p not in wanted_set:
                del self.pending[p]
        self.exit_only = {p for p in positions if p not in self.members}
        return list(self.members), set(self.exit_only)


async def _sigma_from_candles(rest, symbol: str) -> tuple[float, float, float]:
    """(sd1m_recent, sd1m_7d, history_days)."""
    one_min = await rest.get_candles(symbol, "1min", limit=1000)
    hourly = await rest.get_candles(symbol, "1h", limit=200)
    sd_recent = float("nan")
    if len(one_min) > 60:
        c = np.array([x.close for x in one_min])
        sd_recent = float(np.std(np.diff(np.log(c)) * 1e4))
    sd_7d, days = float("nan"), len(hourly) / 24
    if len(hourly) > 24:
        h = np.array([x.close for x in hourly[-168:]])
        sd_7d = float(np.std(np.diff(np.log(h)) * 1e4) / math.sqrt(60))
    return sd_recent, sd_7d, days


async def build_candidates(rest, cfg: UniverseCfg, instruments: Optional[list[Instrument]] = None,
                           concurrency: int = 4) -> list[Candidate]:
    instruments = instruments or await rest.get_instruments()
    now_ms = int(time.time() * 1000)
    ok_inst: dict[str, Instrument] = {}
    for i in instruments:
        if (i.quote != cfg.quote or i.status != "online" or i.is_tokenized_stock
                or i.symbol in cfg.exclude_symbols or i.base.upper() in {b.upper() for b in cfg.exclude_bases}):
            continue
        ok_inst[i.symbol] = i
    tickers = {t.symbol: t for t in await rest.get_tickers() if t.symbol in ok_inst}
    pre = [t for t in tickers.values() if t.quote_volume >=
           cfg.min_volume_24h_usdt and t.spread_bps <= cfg.max_spread_bps]
    pre.sort(key=lambda t: t.quote_volume, reverse=True)
    pre = pre[: cfg.max_candidates]
    sem = asyncio.Semaphore(concurrency)

    async def one(t) -> Optional[Candidate]:
        inst = ok_inst[t.symbol]
        async with sem:
            try:
                sd_recent, sd_7d, days = await _sigma_from_candles(rest, t.symbol)
                book = await rest.get_orderbook(t.symbol, 50)
            except ExchangeError as e:
                log.warning("kandidaat %s overgeslagen: %s", t.symbol, e)
                return None
        age = (now_ms - inst.open_time_ms) / \
            86_400_000 if inst.open_time_ms else 9999.0
        return Candidate(symbol=t.symbol, base=inst.base, volume_usdt=t.quote_volume, spread_bps=t.spread_bps,
                         sd1m_recent=sd_recent, sd1m_7d=sd_7d,
                         depth_bid=book.depth_usdt("bid", cfg.depth_band_bps),
                         depth_ask=book.depth_usdt("ask", cfg.depth_band_bps),
                         depth10_bid=book.depth_usdt("bid", 10), depth10_ask=book.depth_usdt("ask", 10),
                         history_days=days, listing_age_days=age)

    results = await asyncio.gather(*(one(t) for t in pre))
    return [c for c in results if c is not None]


def rank(cands: list[Candidate], cfg: UniverseCfg, fee_bps: float, slippage_bps: float) -> list[Candidate]:
    """Zet `fails` en `score`; geeft de geschikte kandidaten, beste eerst."""
    good = []
    for c in cands:
        c.fails = evaluate(c, cfg)
        c.score = score(c, roundtrip_cost_bps(
            c.spread_bps, fee_bps, fee_bps, slippage_bps))
        if not c.fails:
            good.append(c)
    good.sort(key=lambda c: c.score, reverse=True)
    return good


def save_snapshot(path: Path, cands: list[Candidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        [asdict(c) for c in cands], indent=1, default=str), encoding="utf-8")
