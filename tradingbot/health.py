"""Health-bestand en alerts."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import Settings


class Health:
    def __init__(self, settings: Settings):
        self.path = settings.path(settings.health.path)
        self.alerts_path = settings.path(settings.health.alerts)
        self._alerts_seen: set[str] = set()

    def write(self, status: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **status},
                                  indent=1, default=str), encoding="utf-8")
        tmp.replace(self.path)

    def alert(self, message: str) -> None:
        self.alerts_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.alerts_path, "a", encoding="utf-8") as fh:
            fh.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}\n")
