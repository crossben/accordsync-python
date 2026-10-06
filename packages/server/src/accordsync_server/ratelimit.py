"""Token buckets, kept in memory per server process (`ratelimit.ts`).

Each key (a device, a user) gets `burst` requests at once, refilled at `per_minute`. A multi-process
deployment limits per process; put a shared limiter at the proxy if that matters, or implement
`Limiter` over a shared store.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Protocol

from .define import RateLimit


class Limiter(Protocol):
    def take(self, key: str) -> int:
        """Takes one token for `key`: 0 if allowed, otherwise the milliseconds to wait."""
        ...


def wall_ms() -> float:
    return time.time() * 1000


class RateLimiter:
    def __init__(
        self,
        limit: RateLimit,
        now: Callable[[], float] = wall_ms,
        max_keys: int = 100_000,
    ) -> None:
        if not limit.per_minute > 0:
            raise ValueError("rate limit per_minute must be > 0")
        self._rate = limit.per_minute / 60_000
        self._burst = limit.burst if limit.burst is not None else limit.per_minute
        self._now = now
        self._max_keys = max_keys
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def take(self, key: str) -> int:
        with self._lock:
            now = self._now()
            b = self._buckets.get(key)
            if b is None:
                if len(self._buckets) >= self._max_keys:
                    self._evict(now)
                b = self._buckets[key] = [self._burst, now]
            b[0] = min(self._burst, b[0] + (now - b[1]) * self._rate)
            b[1] = now
            if b[0] >= 1:
                b[0] -= 1
                return 0
            return math.ceil((1 - b[0]) / self._rate)

    def _evict(self, now: float) -> None:
        """Drops buckets that have refilled completely: they behave exactly like new ones."""
        for key, (tokens, at) in list(self._buckets.items()):
            if tokens + (now - at) * self._rate >= self._burst:
                del self._buckets[key]
