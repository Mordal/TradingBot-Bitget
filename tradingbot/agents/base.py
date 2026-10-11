"""Gedeelde types voor de advies-agents (outputstructuur uit Crypto Bot.md 5.3)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Regime(str, Enum):
    TRADABLE = "TRADABLE"
    THIN = "THIN"
    FAST = "FAST"
    BLOCKED = "BLOCKED"


@dataclass
class Advice:
    agent_id: str
    model_version: str
    timestamp_ms: int
    instrument: str
    advice: str                       # BUY | SELL | HOLD
    ratio: float = 0.0                # gekalibreerde kans dat het advies netto klopt
    horizon_seconds: int = 0
    expected_net_edge_bps: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def hold(agent_id: str, version: str, ts: int, instrument: str, reason: str, **meta: Any) -> "Advice":
        return Advice(agent_id, version, ts, instrument, "HOLD", 0.0, 0, 0.0, {"reason": reason, **meta})


@dataclass
class RegimeReport:
    instrument: str
    regime: Regime
    reasons: list[str] = field(default_factory=list)


@dataclass
class ExitAdvice:
    instrument: str
    action: str                       # HOLD | SELL_PARTIAL | SELL_ALL
    reason: str = ""
    urgent: bool = False              # stop of noodgeval: IOC met ruime limiet
    fraction: float = 1.0
    meta: dict[str, Any] = field(default_factory=dict)
