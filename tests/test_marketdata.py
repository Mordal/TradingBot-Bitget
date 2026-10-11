import numpy as np
import pandas as pd
import pytest

from tradingbot.exchange.base import Candle, Trade
from tradingbot.marketdata import MIN_MS, MarketState
from tests.conftest import Clock, book_msg


def test_trades_build_bars_and_close_on_next_minute(clock):
    ms = MarketState(clock)
    closed = []
    ms.on_bar.append(lambda s, b: closed.append((s, b)))
    t0 = clock.t // MIN_MS * MIN_MS
    ms.on_trade(Trade("X", t0 + 1000, 10.0, 2.0, "buy"))
    ms.on_trade(Trade("X", t0 + 20_000, 10.5, 1.0, "sell"))
    ms.on_trade(Trade("X", t0 + MIN_MS + 5, 10.2, 1.0, "buy"))
    assert len(closed) == 1
    b = closed[0][1]
    assert (b.open, b.high, b.low, b.close) == (10.0, 10.5, 10.0, 10.5)
    assert b.quote_vol == pytest.approx(
        20.0 + 10.5) and b.buy_quote == 20.0 and b.sell_quote == 10.5 and b.n == 2


def test_silent_minutes_are_filled_with_flat_bars(clock):
    ms = MarketState(clock)
    t0 = clock.t // MIN_MS * MIN_MS
    ms.on_trade(Trade("X", t0 + 1, 10.0, 1.0, "buy"))
    ms.on_trade(Trade("X", t0 + 4 * MIN_MS + 1, 11.0, 1.0, "buy"))
    st = ms.inst["X"]
    assert [b.ts for b in st.bars] == [t0 + i * MIN_MS for i in range(4)]
    assert st.bars[2].open == st.bars[2].close == 10.0 and st.bars[2].quote_vol == 0.0


def test_flush_closes_bar_without_new_trade(clock):
    ms = MarketState(clock)
    t0 = clock.t // MIN_MS * MIN_MS
    ms.on_trade(Trade("X", t0 + 1, 10.0, 1.0, "buy"))
    ms.flush(t0 + MIN_MS + 100)
    assert len(ms.inst["X"].bars) == 1


def test_ws_trade_dedup_by_trade_id(clock):
    ms = MarketState(clock)
    d = [{"ts": str(clock.t), "price": "5", "size": "2",
          "side": "buy", "tradeId": "1"}]
    ms.on_ws("trade", "X", "update", d)
    ms.on_ws("trade", "X", "snapshot", d)
    assert len(ms.inst["X"].trades) == 1


def test_book_and_age(clock):
    ms = MarketState(clock)
    ms.on_ws("books5", "X", "snapshot", book_msg(2.0, spread_bps=4))
    st = ms.inst["X"]
    assert st.book.best_bid < 2.0 < st.book.best_ask and st.spread_bps == pytest.approx(
        4.0, rel=0.01)
    clock.advance(1500)
    assert ms.age_ms("X") == 1500 and ms.age_ms("missing") == float("inf")


def test_flow_window(clock):
    ms = MarketState(clock)
    now = clock.t
    ms.on_trade(Trade("X", now - 400_000, 1.0, 100.0, "buy"))
    ms.on_trade(Trade("X", now - 10_000, 1.0, 30.0, "buy"))
    ms.on_trade(Trade("X", now - 5_000, 1.0, 10.0, "sell"))
    buy, sell, n = ms.flow("X", 60, now)
    assert (buy, sell, n) == (30.0, 10.0, 2)


def test_seed_and_returns(clock):
    ms = MarketState(clock)
    t0 = clock.t // MIN_MS * MIN_MS - 100 * MIN_MS
    candles = [Candle(t0 + i * MIN_MS, 100 + i, 101 + i, 99 +
                      i, 100 + i, 1, 1000.0) for i in range(100)]
    ms.seed_bars("X", candles)
    ms.on_ws("ticker", "X", "u", [
             {"lastPr": "199", "bidPr": "198.9", "askPr": "199.1", "quoteVolume": "1"}])
    assert ms.returns_bps("X", 5) == pytest.approx(
        (199 / 194 - 1) * 1e4, rel=0.02)
    df = ms.frame("X", 50)
    assert len(df) == 50 and list(df.columns) == [
        "ts", "open", "high", "low", "close", "quote_vol"]
    assert ms.sigma1m_bps("X", 60) > 0
