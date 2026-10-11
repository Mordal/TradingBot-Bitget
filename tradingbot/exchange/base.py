"""Beurs-onafhankelijke datatypes en de gateway-interface."""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


class ExchangeError(Exception):
    def __init__(self, message: str, code: str | None = None, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status

    @property
    def is_rate_limit(self) -> bool:
        return self.http_status == 429 or self.code in {"429", "43011", "40014", "40015", "00429"}


@dataclass(frozen=True)
class Instrument:
    symbol: str
    base: str
    quote: str
    status: str
    price_precision: int
    qty_precision: int
    min_trade_usdt: float
    min_trade_base: float = 0.0
    buy_band: float = 0.02
    sell_band: float = 0.02
    # lijstwaarde, NIET leidend (zie Crypto Bot.md 2.1)
    taker_fee_list: float = 0.001
    open_time_ms: int = 0

    @property
    def tick(self) -> float:
        return 10.0 ** -self.price_precision

    @property
    def lot(self) -> float:
        return 10.0 ** -self.qty_precision

    @property
    def is_tokenized_stock(self) -> bool:
        return self.base[:1].islower()

    def round_price(self, price: float, side: str) -> float:
        """BUY rondt naar beneden (nooit duurder dan limiet), SELL naar boven."""
        return self.floor_price(price) if side == "buy" else self.ceil_price(price)

    def floor_price(self, price: float) -> float:
        f = 10 ** self.price_precision
        return math.floor(price * f + 1e-9) / f

    def ceil_price(self, price: float) -> float:
        f = 10 ** self.price_precision
        return math.ceil(price * f - 1e-9) / f

    def round_qty(self, qty: float) -> float:
        f = 10 ** self.qty_precision
        return math.floor(qty * f + 1e-9) / f


@dataclass
class Ticker:
    symbol: str
    last: float
    bid: float
    ask: float
    quote_volume: float
    ts_ms: int

    @property
    def spread_bps(self) -> float:
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 1e4 if mid > 0 else float("inf")


@dataclass
class OrderBook:
    bids: list[tuple[float, float]]  # (prijs, hoeveelheid), beste eerst
    asks: list[tuple[float, float]]
    ts_ms: int

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else float("nan")

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else float("nan")

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_bps(self) -> float:
        m = self.mid
        return (self.best_ask - self.best_bid) / m * 1e4 if m > 0 else float("inf")

    def depth_usdt(self, side: str, band_bps: float) -> float:
        """Liquiditeit in USDT binnen band_bps van de mid. side='bid' of 'ask'."""
        m = self.mid
        if not (m > 0):
            return 0.0
        if side == "bid":
            lim = m * (1 - band_bps / 1e4)
            return sum(p * q for p, q in self.bids if p >= lim)
        lim = m * (1 + band_bps / 1e4)
        return sum(p * q for p, q in self.asks if p <= lim)

    def vwap_buy(self, quote_usdt: float) -> Optional[float]:
        """Gemiddelde prijs om voor quote_usdt te kopen; None als de zichtbare diepte te klein is."""
        return _vwap(self.asks, quote_usdt, by_quote=True)

    def vwap_sell(self, base_qty: float) -> Optional[float]:
        return _vwap(self.bids, base_qty, by_quote=False)


def _vwap(levels: list[tuple[float, float]], amount: float, by_quote: bool) -> Optional[float]:
    remaining, cost, got = amount, 0.0, 0.0
    for p, q in levels:
        if by_quote:
            take_q = min(q, remaining / p)
            remaining -= take_q * p
        else:
            take_q = min(q, remaining)
            remaining -= take_q
        cost += take_q * p
        got += take_q
        if remaining <= 1e-12:
            return cost / got if got > 0 else None
    return None


@dataclass
class Trade:
    symbol: str
    ts_ms: int
    price: float
    size: float
    side: str  # "buy" = agressieve koper

    @property
    def quote(self) -> float:
        return self.price * self.size


@dataclass
class Candle:
    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    base_vol: float
    quote_vol: float


@dataclass
class OrderRequest:
    symbol: str
    side: str            # buy | sell
    order_type: str      # limit | market
    size: float          # hoeveelheid basismunt (bij market buy: USDT)
    price: Optional[float] = None
    force: str = "gtc"   # gtc | ioc | fok | post_only
    client_oid: str = ""
    # entry | take_profit | stop | exit (voor prioriteit/logging)
    purpose: str = "entry"


@dataclass
class OrderAck:
    client_oid: str
    order_id: Optional[str]
    ok: bool
    code: str = ""
    msg: str = ""
    latency_ms: float = 0.0


@dataclass
class OrderEvent:
    """Eén update over een order (status en/of fill)."""
    symbol: str
    client_oid: str
    order_id: str
    status: str                 # live | partially_filled | filled | cancelled
    side: str
    size: float
    price: float
    acc_base: float             # cumulatief gevuld (basismunt)
    avg_price: float
    fill_id: Optional[str] = None
    fill_price: float = 0.0
    fill_base: float = 0.0
    fee: float = 0.0            # in fee_coin (positief)
    fee_coin: str = ""
    ts_ms: int = 0


@dataclass
class Balance:
    coin: str
    available: float
    frozen: float = 0.0

    @property
    def total(self) -> float:
        return self.available + self.frozen


OrderEventHandler = Callable[[OrderEvent], Awaitable[None]]
BalanceHandler = Callable[[list[Balance]], Awaitable[None]]


class TradingGateway(ABC):
    """Wat de uitvoeringslaag van een beurs nodig heeft; geïmplementeerd door Bitget en de simulator."""

    on_order: Optional[OrderEventHandler] = None
    on_balance: Optional[BalanceHandler] = None

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def place_order(self, req: OrderRequest) -> OrderAck: ...

    @abstractmethod
    async def cancel_order(self, symbol: str, client_oid: str |
                           None = None, order_id: str | None = None) -> bool: ...

    @abstractmethod
    async def cancel_all(self, symbol: str) -> None: ...

    @abstractmethod
    async def get_order(self, symbol: str, client_oid: str | None = None,
                        order_id: str | None = None) -> Optional[OrderEvent]: ...

    @abstractmethod
    async def get_open_orders(
        self, symbol: str | None = None) -> list[OrderEvent]: ...

    @abstractmethod
    async def get_balances(self) -> list[Balance]: ...

    @property
    @abstractmethod
    def connected(self) -> bool: ...
