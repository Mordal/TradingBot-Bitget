"""In-memory marktstate per instrument: bars uit trades, orderboek, ticker, trade-flow."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

from .exchange.base import Candle, OrderBook, Trade

MIN_MS = 60_000
MAX_BARS = 600          # 10 uur aan 1-minuutbars
TRADE_WINDOW_MS = 30 * MIN_MS


@dataclass
class Bar:
    ts: int                 # begin van de minuut (ms)
    open: float
    high: float
    low: float
    close: float
    quote_vol: float = 0.0
    buy_quote: float = 0.0
    sell_quote: float = 0.0
    n: int = 0


@dataclass
class InstrumentState:
    symbol: str
    bars: deque = field(default_factory=lambda: deque(maxlen=MAX_BARS))
    cur: Optional[Bar] = None
    trades: deque = field(default_factory=deque)   # (ts, price, quote, +1/-1)
    seen_ids: deque = field(default_factory=lambda: deque(maxlen=500))
    book: Optional[OrderBook] = None
    book_recv_ms: int = 0
    last: float = float("nan")
    bid: float = float("nan")
    ask: float = float("nan")
    vol24h: float = 0.0
    ticker_recv_ms: int = 0
    last_trade_recv_ms: int = 0
    msg_count: int = 0

    @property
    def recv_ms(self) -> int:
        return max(self.book_recv_ms, self.ticker_recv_ms, self.last_trade_recv_ms)

    @property
    def mid(self) -> float:
        if self.book and self.book.bids and self.book.asks:
            return self.book.mid
        if self.bid == self.bid and self.ask == self.ask and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last

    @property
    def spread_bps(self) -> float:
        if self.book and self.book.bids and self.book.asks:
            return self.book.spread_bps
        if self.bid == self.bid and self.ask > 0:
            return (self.ask - self.bid) / ((self.ask + self.bid) / 2) * 1e4
        return float("inf")


BarCb = Callable[[str, Bar], None]


class MarketState:
    def __init__(self, now_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.now_ms = now_ms
        self.inst: dict[str, InstrumentState] = {}
        self.on_bar: list[BarCb] = []

    def get(self, symbol: str) -> InstrumentState:
        st = self.inst.get(symbol)
        if st is None:
            st = self.inst[symbol] = InstrumentState(symbol)
        return st

    def drop(self, symbol: str) -> None:
        self.inst.pop(symbol, None)

    # ----------------------------------------------------------- binnenkomende data
    def on_ws(self, channel: str, symbol: str, action: str, data: list) -> None:
        st = self.get(symbol)
        recv = self.now_ms()
        st.msg_count += 1
        if channel == "trade":
            for d in data:
                tid = d.get("tradeId")
                if tid is not None:
                    if tid in st.seen_ids:
                        continue
                    st.seen_ids.append(tid)
                self.on_trade(Trade(symbol, int(d["ts"]), float(d["price"]), float(d["size"]),
                                    str(d["side"]).lower()), recv)
        elif channel in ("books5", "books1", "books15"):
            d = data[0]
            st.book = OrderBook(bids=[(float(p), float(q)) for p, q in d["bids"]],
                                asks=[(float(p), float(q)) for p, q in d["asks"]], ts_ms=int(d.get("ts", recv)))
            st.book_recv_ms = recv
        elif channel == "ticker":
            d = data[0]
            st.last = float(d["lastPr"])
            st.bid, st.ask = float(d["bidPr"]), float(d["askPr"])
            st.vol24h = float(d.get("quoteVolume", 0) or 0)
            st.ticker_recv_ms = recv

    def on_trade(self, t: Trade, recv_ms: Optional[int] = None) -> None:
        st = self.get(t.symbol)
        st.last_trade_recv_ms = recv_ms if recv_ms is not None else self.now_ms()
        if t.price > 0 and t.price == t.price:
            st.last = t.price
        st.trades.append((t.ts_ms, t.price, t.quote,
                         1 if t.side == "buy" else -1))
        cutoff = t.ts_ms - TRADE_WINDOW_MS
        while st.trades and st.trades[0][0] < cutoff:
            st.trades.popleft()
        minute = t.ts_ms // MIN_MS * MIN_MS
        self._roll(st, minute)
        if st.cur is None:
            st.cur = Bar(minute, t.price, t.price, t.price, t.price)
        if minute < st.cur.ts:
            return  # te late trade voor een al gesloten minuut
        b = st.cur
        b.high, b.low, b.close = max(
            b.high, t.price), min(b.low, t.price), t.price
        b.quote_vol += t.quote
        if t.side == "buy":
            b.buy_quote += t.quote
        else:
            b.sell_quote += t.quote
        b.n += 1

    def _roll(self, st: InstrumentState, minute: int) -> None:
        """Sluit bars tot en met de minuut vóór `minute`; vul stille minuten met platte bars."""
        cur = st.cur
        if cur is None or minute <= cur.ts:
            return
        self._close(st, cur)
        prev = cur.close
        ts = cur.ts + MIN_MS
        while ts < minute:
            self._close(st, Bar(ts, prev, prev, prev, prev))
            ts += MIN_MS
        st.cur = Bar(minute, prev, prev, prev, prev)

    def _close(self, st: InstrumentState, bar: Bar) -> None:
        st.bars.append(bar)
        for cb in self.on_bar:
            cb(st.symbol, bar)

    def flush(self, now_ms: Optional[int] = None) -> None:
        """Periodiek aanroepen: sluit de lopende bar als de minuut voorbij is, ook zonder trades."""
        now = now_ms if now_ms is not None else self.now_ms()
        minute = now // MIN_MS * MIN_MS
        for st in list(self.inst.values()):
            if st.cur is not None:
                self._roll(st, minute)

    def seed_bars(self, symbol: str, candles: list[Candle]) -> None:
        st = self.get(symbol)
        st.bars.clear()
        for c in candles[-MAX_BARS:]:
            st.bars.append(Bar(c.ts_ms // MIN_MS * MIN_MS, c.open,
                           c.high, c.low, c.close, c.quote_vol))
        if st.bars:
            last = st.bars[-1]
            st.cur = Bar(last.ts + MIN_MS, last.close,
                         last.close, last.close, last.close)
            if st.last != st.last:
                st.last = last.close

    # -------------------------------------------------------------- afgeleide data
    def age_ms(self, symbol: str) -> float:
        st = self.inst.get(symbol)
        if st is None or st.recv_ms == 0:
            return float("inf")
        return self.now_ms() - st.recv_ms

    def book_age_ms(self, symbol: str) -> float:
        st = self.inst.get(symbol)
        if st is None or st.book_recv_ms == 0:
            return float("inf")
        return self.now_ms() - st.book_recv_ms

    def frame(self, symbol: str, n: int = 300) -> pd.DataFrame:
        st = self.inst.get(symbol)
        rows = list(st.bars)[-n:] if st else []
        return pd.DataFrame({
            "ts": [b.ts for b in rows], "open": [b.open for b in rows], "high": [b.high for b in rows],
            "low": [b.low for b in rows], "close": [b.close for b in rows],
            "quote_vol": [b.quote_vol for b in rows]})

    def flow(self, symbol: str, window_s: int, now_ms: Optional[int] = None) -> tuple[float, float, int]:
        """(koop-quote, verkoop-quote, aantal trades) in het laatste venster."""
        st = self.inst.get(symbol)
        if st is None:
            return 0.0, 0.0, 0
        now = now_ms if now_ms is not None else self.now_ms()
        lo = now - window_s * 1000
        buy = sell = 0.0
        n = 0
        for ts, _p, q, s in reversed(st.trades):
            if ts < lo:
                break
            n += 1
            if s > 0:
                buy += q
            else:
                sell += q
        return buy, sell, n

    def returns_bps(self, symbol: str, minutes: int) -> float:
        st = self.inst.get(symbol)
        if not st or len(st.bars) <= minutes:
            return float("nan")
        ref = st.bars[-minutes - 1].close
        now = st.mid
        return (now / ref - 1) * 1e4 if ref > 0 and now == now else float("nan")

    def sigma1m_bps(self, symbol: str, minutes: int = 60) -> float:
        st = self.inst.get(symbol)
        if not st or len(st.bars) < 10:
            return float("nan")
        c = np.array([b.close for b in list(st.bars)[-minutes - 1:]])
        r = np.diff(np.log(c)) * 1e4
        return float(np.std(r)) if len(r) > 3 else float("nan")
