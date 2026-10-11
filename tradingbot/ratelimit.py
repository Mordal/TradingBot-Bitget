"""Token buckets en backoff met jitter."""
from __future__ import annotations

import asyncio
import random
import time
from typing import Callable


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: float | None = None, now: Callable[[], float] = time.monotonic):
        if rate_per_s <= 0:
            raise ValueError("rate_per_s moet > 0 zijn")
        self.rate = rate_per_s
        self.capacity = burst if burst is not None else max(1.0, rate_per_s)
        self._tokens = self.capacity
        self._now = now
        self._last = now()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        t = self._now()
        self._tokens = min(self.capacity, self._tokens +
                           (t - self._last) * self.rate)
        self._last = t

    def try_acquire(self, n: float = 1.0) -> bool:
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    def wait_time(self, n: float = 1.0) -> float:
        self._refill()
        return max(0.0, (n - self._tokens) / self.rate)

    async def acquire(self, n: float = 1.0) -> float:
        """Wacht tot er n tokens zijn; geeft de wachttijd terug."""
        waited = 0.0
        async with self._lock:
            while True:
                if self.try_acquire(n):
                    return waited
                w = self.wait_time(n)
                await asyncio.sleep(w)
                waited += w

    def penalize(self, seconds: float) -> None:
        """Na een limietfout: tokens leegmaken en de terugvaltijd inplannen."""
        self._refill()
        self._tokens = -seconds * self.rate


class Backoff:
    def __init__(self, base_s: float = 0.5, max_s: float = 30.0, rng: random.Random | None = None):
        self.base, self.max, self.attempt = base_s, max_s, 0
        self._rng = rng or random.Random()

    def next_delay(self) -> float:
        d = min(self.max, self.base * (2 ** self.attempt))
        self.attempt += 1
        return d * (0.5 + self._rng.random() / 2)  # jitter 50-100%

    def reset(self) -> None:
        self.attempt = 0
