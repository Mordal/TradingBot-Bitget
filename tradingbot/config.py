"""config.toml + .env, met validatie van de demo/live-combinatie."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


@dataclass
class BotCfg:
    mode: str = "shadow"
    kill_file: str = "KILL"
    shadow_start_usdt: float = 1000.0
    shadow_latency_ms: int = 300
    force_model_active: bool = False   # alleen voor tests van de keten; nooit op live


@dataclass
class ExchangeCfg:
    environment: str = "demo"
    live_confirmed: bool = False
    rest_url: str = "https://api.bitget.com"
    ws_public_url: str = "wss://ws.bitget.com/v2/ws/public"
    ws_private_live_url: str = "wss://ws.bitget.com/v2/ws/private"
    ws_private_demo_url: str = "wss://wspap.bitget.com/v2/ws/private"


@dataclass
class UniverseCfg:
    quote: str = "USDT"
    top_n: int = 20
    refresh_s: int = 900
    hysteresis_checks: int = 3
    max_candidates: int = 60
    min_volume_24h_usdt: float = 2e6
    max_spread_bps: float = 8.0
    min_sd1m_bps: float = 5.0
    max_sd1m_bps: float = 40.0
    spike_ratio: float = 3.0
    min_depth_usdt: float = 5000.0
    depth_band_bps: float = 25.0
    min_history_days: int = 7
    context_symbols: list[str] = field(
        default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    exclude_symbols: list[str] = field(default_factory=list)
    exclude_bases: list[str] = field(default_factory=list)


@dataclass
class AgentsCfg:
    horizons_min: list[int] = field(default_factory=lambda: [10, 30, 60, 120])
    edge_max_age_s: int = 90
    tp_cost_multiple: float = 2.0
    sl_sigma_multiple: float = 1.0
    volume_z_min: float = 2.0
    vol_expansion_min: float = 1.5
    flow_imbalance_min: float = 0.2
    flow_window_s: int = 180
    breakout_window_min: int = 15
    trail_trigger: float = 0.5
    edge_decay_ev_bps: float = -2.0
    max_hold_factor: float = 1.0


@dataclass
class DecisionCfg:
    min_ev_bps: float = 5.0
    min_p_win: float = 0.45
    ev_ref_bps: float = 15.0
    base_order_usdt: float = 20.0
    max_order_usdt: float = 100.0
    slippage_bps: float = 1.0
    thin_size_factor: float = 0.5
    decision_interval_ms: int = 500


@dataclass
class FeesCfg:
    maker_bps: float = 10.0
    taker_bps: float = 10.0


@dataclass
class RiskCfg:
    max_open_positions: int = 6
    max_position_usdt: float = 100.0
    max_asset_share: float = 0.25
    max_total_exposure_pct: float = 0.6
    min_usdt_reserve: float = 50.0
    dd_soft: float = 0.03
    dd_hard: float = 0.06
    daily_loss_pct: float = 0.03
    stop_cooldown_s: int = 900
    max_buys_per_min: int = 3
    max_buys_per_hour: int = 30
    max_data_age_ms: int = 2000
    max_order_depth_fraction: float = 0.10
    max_slippage_bps: float = 8.0
    btc_shock_bps: float = 50.0
    btc_shock_window_s: int = 300
    max_reject_streak: int = 3
    capital_usdt: float = 0.0


@dataclass
class RateCfg:
    rest_public_per_s: float = 10.0
    rest_private_per_s: float = 8.0
    orders_per_s: float = 5.0
    cancels_per_s: float = 5.0
    ws_channels_per_connection: int = 100
    ws_subscribes_per_hour: int = 200
    backoff_base_s: float = 0.5
    backoff_max_s: float = 30.0
    order_timeout_s: float = 3.0


@dataclass
class ModelCfg:
    dir: str = "models"
    history_days: int = 16
    train_frac: float = 0.6
    val_frac: float = 0.2
    purge_min: int = 120
    min_test_signals: int = 30
    min_ic: float = 0.08
    n_estimators: int = 400
    learning_rate: float = 0.03
    num_leaves: int = 15
    min_data_in_leaf: int = 200
    default_spread_bps: float = 4.0
    shadow_activate_n: int = 100
    shadow_activate_hit: float = 0.52
    shadow_demote_n: int = 200
    shadow_demote_hit: float = 0.45


@dataclass
class RecorderCfg:
    enabled: bool = True
    dir: str = "data/recordings"
    flush_s: int = 60
    books_every_ms: int = 1000


@dataclass
class HealthCfg:
    path: str = "data/health.json"
    interval_s: int = 10
    alerts: str = "data/alerts.log"


@dataclass
class StorageCfg:
    db: str = "data/bot.sqlite"


@dataclass
class Settings:
    bot: BotCfg = field(default_factory=BotCfg)
    exchange: ExchangeCfg = field(default_factory=ExchangeCfg)
    universe: UniverseCfg = field(default_factory=UniverseCfg)
    agents: AgentsCfg = field(default_factory=AgentsCfg)
    decision: DecisionCfg = field(default_factory=DecisionCfg)
    fees: FeesCfg = field(default_factory=FeesCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    rate: RateCfg = field(default_factory=RateCfg)
    model: ModelCfg = field(default_factory=ModelCfg)
    recorder: RecorderCfg = field(default_factory=RecorderCfg)
    health: HealthCfg = field(default_factory=HealthCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    root: Path = ROOT

    @property
    def is_live(self) -> bool:
        return self.exchange.environment == "live"

    @property
    def trading_enabled(self) -> bool:
        return self.bot.mode == "trade"

    def path(self, p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else self.root / q


@dataclass(frozen=True)
class Credentials:
    key: str
    secret: str
    passphrase: str
    demo: bool

    def __repr__(self) -> str:  # nooit geheimen tonen
        return f"Credentials(demo={self.demo}, key=***)"


def _fill(cls: type, data: dict[str, Any], section: str):
    names = {f.name for f in fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ConfigError(
            f"Onbekende instelling(en) in [{section}]: {sorted(unknown)}")
    return cls(**data)


def load_settings(path: str | Path | None = None) -> Settings:
    p = Path(path) if path else ROOT / "config.toml"
    raw: dict[str, Any] = {}
    if p.exists():
        # tolereert een BOM van Windows-editors
        raw = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    sections = {f.name: f.type for f in fields(Settings) if f.name != "root"}
    classes = {
        "bot": BotCfg, "exchange": ExchangeCfg, "universe": UniverseCfg, "agents": AgentsCfg,
        "decision": DecisionCfg, "fees": FeesCfg, "risk": RiskCfg, "rate": RateCfg,
        "model": ModelCfg, "recorder": RecorderCfg, "health": HealthCfg, "storage": StorageCfg,
    }
    unknown = set(raw) - set(sections)
    if unknown:
        raise ConfigError(f"Onbekende sectie(s): {sorted(unknown)}")
    kwargs = {name: _fill(cls, raw.get(name, {}), name)
              for name, cls in classes.items()}
    s = Settings(**kwargs, root=p.parent)
    validate(s)
    return s


def validate(s: Settings) -> None:
    if s.bot.mode not in ("shadow", "trade"):
        raise ConfigError("bot.mode moet 'shadow' of 'trade' zijn")
    if s.exchange.environment not in ("demo", "live"):
        raise ConfigError("exchange.environment moet 'demo' of 'live' zijn")
    if s.trading_enabled and s.is_live and not s.exchange.live_confirmed:
        raise ConfigError(
            "mode=trade op live vereist exchange.live_confirmed = true")
    if s.bot.force_model_active and s.is_live and s.trading_enabled:
        raise ConfigError(
            "bot.force_model_active is niet toegestaan bij echte handel op live")
    if s.decision.base_order_usdt > s.decision.max_order_usdt:
        raise ConfigError(
            "decision.base_order_usdt mag niet groter zijn dan max_order_usdt")
    if s.risk.dd_soft >= s.risk.dd_hard:
        raise ConfigError("risk.dd_soft moet kleiner zijn dan dd_hard")
    if not s.agents.horizons_min or sorted(s.agents.horizons_min) != s.agents.horizons_min:
        raise ConfigError(
            "agents.horizons_min moet oplopend en niet leeg zijn")


def load_credentials(s: Settings, environment: str | None = None, read_only: bool = False) -> Credentials:
    """Leest de sleutels van de gekozen omgeving; faalt als de combinatie niet klopt.

    Live kent twee sleutels: `_LIVE_READ` (alleen lezen, o.a. accounttarief) en `_LIVE` (spot trade, pas voor echte handel).
    """
    env_name = environment or s.exchange.environment
    suffix = "DEMO" if env_name != "live" else (
        "LIVE_READ" if read_only else "LIVE")
    env = {**dotenv_values(ROOT / ".env"), **{k: v for k,
                                              v in os.environ.items() if k.startswith("BITGET_")}}
    try:
        key = env[f"BITGET_API_KEY_ID_{suffix}"]
        secret = env[f"BITGET_API_SECRET_KEY_{suffix}"]
        passphrase = env[f"BITGET_API_PASSPHRASE_2_{suffix}"]
    except KeyError as e:
        raise ConfigError(
            f"Ontbrekende variabele in .env: {e.args[0]}") from None
    if not (key and secret and passphrase):
        raise ConfigError(f"Lege BITGET_*_{suffix}-waarde in .env")
    return Credentials(key=key, secret=secret, passphrase=passphrase, demo=env_name != "live")
