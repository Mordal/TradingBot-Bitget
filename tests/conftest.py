from __future__ import annotations

import pytest

from tradingbot.config import Settings
from tradingbot.exchange.base import Instrument, OrderBook
from tradingbot.marketdata import MarketState


class Clock:
    def __init__(self, t: int = 1_700_000_000_000):
        self.t = t

    def __call__(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def inst() -> Instrument:
    return Instrument("WLDUSDT", "WLD", "USDT", "online", 4, 2, 1.0)


def make_book(mid: float = 1.0, spread_bps: float = 2.0, depth: float = 10_000.0, ts: int = 0) -> OrderBook:
    half = mid * spread_bps / 2e4
    bid, ask = mid - half, mid + half
    bids = [(round(bid - i * mid * 1e-4, 6), depth / mid / 5)
            for i in range(5)]
    asks = [(round(ask + i * mid * 1e-4, 6), depth / mid / 5)
            for i in range(5)]
    return OrderBook(bids, asks, ts)


def book_msg(mid: float, **kw) -> list:
    b = make_book(mid, **kw)
    return [{"bids": [[str(p), str(q)] for p, q in b.bids], "asks": [[str(p), str(q)] for p, q in b.asks],
             "ts": "0"}]


def feed_flat_market(ms: MarketState, symbol: str, mid: float, now_ms: int, **kw) -> None:
    ms.on_ws("books5", symbol, "snapshot", book_msg(mid, **kw))
    ms.on_ws("ticker", symbol, "snapshot", [{"lastPr": str(mid), "bidPr": str(mid * 0.9999),
                                             "askPr": str(mid * 1.0001), "quoteVolume": "1000000"}])
