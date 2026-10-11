"""Opname van ruwe trade/books5/ticker-berichten (gzip-jsonl) voor replay en latere training."""
from __future__ import annotations

import gzip
import json
import time
from pathlib import Path
from typing import Callable, Iterator

from .config import Settings


class Recorder:
    def __init__(self, settings: Settings, now_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.cfg = settings.recorder
        self.dir = settings.path(self.cfg.dir)
        self.now_ms = now_ms
        self._buf: list[str] = []
        self._last_book: dict[str, int] = {}
        self._last_flush = 0
        self.count = 0

    def on_ws(self, channel: str, symbol: str, action: str, data: list) -> None:
        if not self.cfg.enabled:
            return
        t = self.now_ms()
        if channel in ("books5", "books1"):
            if t - self._last_book.get(symbol, 0) < self.cfg.books_every_ms:
                return
            self._last_book[symbol] = t
        self._buf.append(json.dumps(
            {"t": t, "c": channel, "s": symbol, "a": action, "d": data}, separators=(",", ":")))
        self.count += 1
        if t - self._last_flush >= self.cfg.flush_s * 1000:
            self.flush()

    def flush(self) -> None:
        self._last_flush = self.now_ms()
        if not self._buf:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        name = time.strftime("%Y%m%d_%H", time.gmtime(
            self._last_flush / 1000)) + ".jsonl.gz"
        with gzip.open(self.dir / name, "at", encoding="utf-8") as fh:
            fh.write("\n".join(self._buf) + "\n")
        self._buf.clear()


def read_recordings(directory: Path, start_ms: int = 0, end_ms: int = 1 << 62) -> Iterator[dict]:
    """Geeft de opgenomen berichten in tijdsvolgorde terug."""
    for f in sorted(directory.glob("*.jsonl.gz")):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                ev = json.loads(line)
                if start_ms <= ev["t"] <= end_ms:
                    yield ev
