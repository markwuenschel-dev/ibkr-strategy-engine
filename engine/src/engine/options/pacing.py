"""A request budget for IBKR's pacing limits.

IBKR enforces roughly sixty historical-data requests per ten minutes (and a
general message-rate ceiling); exceeding either produces error 162/100 and a
cooling-off period that costs far more than the waiting would have. Nothing
in the adapters throttled anything before the universe scanner existed,
because nothing made ninety requests in a burst before the universe scanner
existed.

:class:`PacedRequestBudget` is a token bucket per request kind. ``acquire``
blocks (via the injected sleeper, which in the scanner is ``ib.sleep`` so the
event loop keeps breathing) until a token is available. ``penalize`` doubles
the refill interval for the current window -- called when the broker says we
paced anyway, because the broker's opinion of the limit is the operative one.

Deliberately conservative: 55 historical tokens per 600 seconds, not 60 --
the margin is the price of never meeting the cooling-off period. Injected
clock and sleeper make the whole thing testable in microseconds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

__all__ = ["RequestKind", "PacedRequestBudget"]


class RequestKind(str, Enum):
    #: reqHistoricalData -- the expensive, hard-limited one.
    HISTORICAL = "HISTORICAL"
    #: Everything else that talks to the broker (qualification, chain
    #: parameters, quotes). Bounded only by the general message rate, so the
    #: floor between bursts is small.
    GENERAL = "GENERAL"


@dataclass
class _Bucket:
    capacity: int
    window_seconds: float
    tokens: float = field(default=0.0)
    updated: float = field(default=0.0)
    penalty: float = field(default=1.0)

    def refill_rate(self) -> float:
        return self.capacity / (self.window_seconds * self.penalty)


class PacedRequestBudget:
    def __init__(
        self,
        *,
        historical_per_window: int = 55,
        historical_window_seconds: float = 600.0,
        general_per_window: int = 40,
        general_window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        now = clock()
        self.clock = clock
        self.sleeper = sleeper
        self._buckets = {
            RequestKind.HISTORICAL: _Bucket(
                capacity=historical_per_window,
                window_seconds=historical_window_seconds,
                tokens=float(historical_per_window),
                updated=now,
            ),
            RequestKind.GENERAL: _Bucket(
                capacity=general_per_window,
                window_seconds=general_window_seconds,
                tokens=float(general_per_window),
                updated=now,
            ),
        }
        self.waited_seconds = 0.0

    def _advance(self, bucket: _Bucket) -> None:
        now = self.clock()
        elapsed = max(0.0, now - bucket.updated)
        bucket.tokens = min(
            float(bucket.capacity), bucket.tokens + elapsed * bucket.refill_rate()
        )
        bucket.updated = now

    def acquire(self, kind: RequestKind) -> None:
        """Take one token, sleeping until one exists. Never raises for pace."""
        bucket = self._buckets[kind]
        while True:
            self._advance(bucket)
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return
            shortfall = (1.0 - bucket.tokens) / bucket.refill_rate()
            wait = max(0.05, shortfall)
            self.waited_seconds += wait
            self.sleeper(wait)

    def penalize(self, kind: RequestKind = RequestKind.HISTORICAL) -> None:
        """The broker paced us anyway: halve the refill rate for this window
        and drop the tokens on hand -- the broker's ledger, not ours, is the
        one that counts."""
        bucket = self._buckets[kind]
        self._advance(bucket)
        bucket.penalty *= 2.0
        bucket.tokens = 0.0
