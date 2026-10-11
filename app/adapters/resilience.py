"""Shared protection for every real external call: a circuit breaker and retries with backoff + jitter.

Analogy (breaker): the fuse box. If an appliance keeps tripping, cut its power for a while instead of
trying it 50 times a second. After the cool-down we let ONE request through to see if it recovered.
Analogy (jitter): when many people retry at the same instant the server is hit in waves, so each waits
a slightly random time.
"""

import logging
import random
import time
from collections.abc import Callable

from app.adapters.ports import AdapterUnavailable

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """closed (normal) -> open (fail fast) after `threshold` failures within `window_s`
    -> half-open after `cooldown_s` (one trial call) -> closed again on success."""

    def __init__(self, name: str, threshold: int = 3, window_s: float = 60, cooldown_s: float = 120,
                 clock: Callable[[], float] = time.monotonic) -> None:  # fmt: skip
        self.name, self.threshold, self.window_s, self.cooldown_s, self.clock = name, threshold, window_s, cooldown_s, clock
        self.failures: list[float] = []
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if self.clock() - self.opened_at >= self.cooldown_s else "open"

    def call[T](self, fn: Callable[[], T]) -> T:
        if self.state == "open":
            raise AdapterUnavailable(f"{self.name} is temporarily disabled after repeated failures")
        try:
            result = fn()
        except AdapterUnavailable:
            self._record_failure()
            raise
        self.failures.clear()
        self.opened_at = None  # a success (also the half-open trial) closes the breaker
        return result

    def _record_failure(self) -> None:
        now = self.clock()
        self.failures = [t for t in self.failures if now - t < self.window_s] + [now]
        if self.state == "half_open" or len(self.failures) >= self.threshold:
            self.opened_at = now  # (re)open: a failed trial restarts the cool-down
            logger.warning("circuit opened", extra={"adapter": self.name})


def retry[T](fn: Callable[[], T], *, attempts: int = 3, base_s: float = 0.5,
          sleep: Callable[[float], None] = time.sleep) -> T:  # fmt: skip
    """Retry only AdapterUnavailable (timeouts, 5xx, 429). Use ONLY for calls that are safe to repeat."""
    for i in range(attempts):
        try:
            return fn()
        except AdapterUnavailable:
            if i == attempts - 1:
                raise
            sleep(base_s * 2**i * random.uniform(0.5, 1.5))
    raise AssertionError("unreachable")
