"""Historische 1-minuutcandles via REST, gecached als Parquet op een regelmatig minuutraster."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from .exchange.base import Candle

log = logging.getLogger(__name__)
MIN_MS = 60_000
DAY_MS = 86_400_000
# `market/candles` (1000 per aanvraag) reikt ~30 dagen terug; daarvoor `history-candles` (200)
RECENT_DAYS = 25
COLUMNS = ["ts", "open", "high", "low", "close", "quote_vol"]


def candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    return pd.DataFrame({
        "ts": [c.ts_ms for c in candles], "open": [c.open for c in candles], "high": [c.high for c in candles],
        "low": [c.low for c in candles], "close": [c.close for c in candles],
        "quote_vol": [c.quote_vol for c in candles]}, columns=COLUMNS)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Regelmatig minuutraster; ontbrekende minuten krijgen een platte bar zonder volume."""
    if df.empty:
        return df
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    grid = np.arange(df["ts"].iloc[0], df["ts"].iloc[-1] +
                     1, MIN_MS, dtype=np.int64)
    out = df.set_index("ts").reindex(grid)
    out["close"] = out["close"].ffill()
    for c in ("open", "high", "low"):
        out[c] = out[c].fillna(out["close"])
    out["quote_vol"] = out["quote_vol"].fillna(0.0)
    out.index.name = "ts"
    return out.reset_index()[COLUMNS]


async def _fetch_back(rest, symbol: str, start_ms: int, end_ms: int, now_ms: int, max_pages: int) -> pd.DataFrame:
    """Pagineert van end_ms terug naar start_ms; kiest per pagina het goedkoopste endpoint."""
    parts: list[pd.DataFrame] = []
    end = end_ms
    for page_no in range(max_pages):
        recent = end > now_ms - RECENT_DAYS * DAY_MS
        page = await rest.get_candles(symbol, "1min", end_ms=end, limit=1000 if recent else 200, history=not recent)
        if not page:
            break
        parts.append(candles_to_frame(page))
        first = page[0].ts_ms
        if page_no % 100 == 99:
            log.info("%s: %d pagina's, tot %.0f dagen terug",
                     symbol, page_no + 1, (now_ms - first) / DAY_MS)
        if first <= start_ms:
            break
        end = first - 1
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=COLUMNS)


async def download_symbol(rest, symbol: str, days: float, cache_dir: Path, now_ms: Optional[int] = None,
                          max_pages: int = 3000) -> pd.DataFrame:
    """Vult de cache aan: alleen het ontbrekende nieuwe en oudere deel wordt opgehaald."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{symbol}_1m.parquet"
    now_ms = now_ms or int(time.time() * 1000)
    start = now_ms - int(days * DAY_MS)
    cached = pd.read_parquet(path) if path.exists(
    ) else pd.DataFrame(columns=COLUMNS)
    parts = [cached]
    if cached.empty:
        parts.append(await _fetch_back(rest, symbol, start, now_ms, now_ms, max_pages))
    else:
        first_c, last_c = int(cached["ts"].iloc[0]), int(cached["ts"].iloc[-1])
        if last_c < now_ms - 2 * MIN_MS:
            parts.append(await _fetch_back(rest, symbol, last_c, now_ms, now_ms, max_pages))
        if first_c > start + 5 * MIN_MS:
            parts.append(await _fetch_back(rest, symbol, start, first_c - 1, now_ms, max_pages))
    merged = pd.concat([p for p in parts if not p.empty], ignore_index=True)
    merged = merged[merged["ts"] >= start - MIN_MS]
    out = normalize(merged)
    if not out.empty:
        out.to_parquet(path, index=False)
    return out


def load_cached(symbol: str, cache_dir: Path) -> Optional[pd.DataFrame]:
    p = cache_dir / f"{symbol}_1m.parquet"
    return pd.read_parquet(p) if p.exists() else None
