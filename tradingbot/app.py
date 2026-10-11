"""Opbouw van de live bot (feeds, gateway, portefeuille) en hulpfuncties voor de CLI."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .bot import TradingBot
from .config import ConfigError, Settings, load_credentials
from .exchange.base import Instrument
from .exchange.bitget.gateway import BitgetGateway
from .exchange.bitget.rest import BitgetRest
from .exchange.bitget.signing import Clock
from .exchange.bitget.ws_public import PublicFeed
from .fillsim import SimGateway
from .marketdata import MarketState
from .model import load_current
from .portfolio import Portfolio
from .recorder import Recorder
from .storage import Storage

log = logging.getLogger(__name__)


def save_instruments(path: Path, instruments: dict[str, Instrument]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        [asdict(i) for i in instruments.values() if i.quote == "USDT"]), encoding="utf-8")


def load_instruments(path: Path) -> dict[str, Instrument]:
    return {d["symbol"]: Instrument(**d) for d in json.loads(path.read_text(encoding="utf-8"))}


async def build_bot(settings: Settings) -> TradingBot:
    storage = Storage(settings.path(settings.storage.db))
    clock = Clock()
    creds = load_credentials(settings) if settings.trading_enabled else None
    rest = BitgetRest(settings, creds, clock)
    await rest.start()
    instruments = {i.symbol: i for i in await rest.get_instruments()}
    save_instruments(settings.path("data/instruments.json"), instruments)
    ms = MarketState(clock.now_ms)
    fee_rest: Optional[BitgetRest] = None
    symbol_filter: Optional[set[str]] = None

    if settings.trading_enabled:
        assert creds is not None
        gateway = BitgetGateway(settings, creds, rest)
        balances = await rest.get_balances()
        cash = next((b.total for b in balances if b.coin == "USDT"), 0.0)
        baseline = {b.coin: b.total for b in balances if b.coin !=
                    "USDT" and b.total > 0}
        if not settings.is_live:
            demo = await rest.get_instruments(demo=True)
            symbol_filter = {i.symbol for i in demo if i.status == "online"}
            log.warning("demo-omgeving: alleen %d demo-handelbare paren; demo-prijzen wijken af van live",
                        len(symbol_filter))
    else:
        gateway = SimGateway(settings, ms, instruments, clock.now_ms, settings.bot.shadow_start_usdt,
                             settings.bot.shadow_latency_ms)
        cash, baseline = settings.bot.shadow_start_usdt, {}
        try:  # alleen-lezen: accounttarief via GET trade-rate (geen orders)
            fee_rest = BitgetRest(settings, load_credentials(
                settings, "live", read_only=True), clock)
            await fee_rest.start()
        except Exception as e:  # noqa: BLE001
            log.info(
                "geen accounttarief beschikbaar (%s); config-fee wordt gebruikt", type(e).__name__)
            fee_rest = None

    pf = Portfolio(cash, baseline, capital_cap=settings.risk.capital_usdt)
    bundle = load_current(settings.path(settings.model.dir))
    recorder = Recorder(settings, clock.now_ms)
    bot = TradingBot(settings, clock.now_ms, storage, pf, gateway, instruments, bundle, rest=rest, recorder=recorder,
                     ms=ms, fee_rest=fee_rest)
    bot.symbol_filter = symbol_filter
    bot.feed = PublicFeed(settings, bot.on_ws)
    return bot
