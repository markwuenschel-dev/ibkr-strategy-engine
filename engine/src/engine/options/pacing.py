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

import datetime as dt
import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Callable
from uuid import uuid4

from ..errors import EngineError

__all__ = [
    "RequestKind",
    "Priority",
    "DiscoveryPaced",
    "PacedRequestBudget",
    "SharedPacingBudget",
]


class RequestKind(str, Enum):
    #: reqHistoricalData -- the expensive, hard-limited one.
    HISTORICAL = "HISTORICAL"
    #: Everything else that talks to the broker (qualification, chain
    #: parameters, quotes). Bounded only by the general message rate, so the
    #: floor between bursts is small.
    GENERAL = "GENERAL"


class Priority(IntEnum):
    """Who gets tokens first when they are scarce. Lower number wins.

    The ordering is the 2026-08-01 audit's, verbatim: managing what the book
    already holds outranks watching working orders, which outranks the final
    authorization of a new entry, which outranks building candidates, which
    outranks broad discovery. The budget enforces it three ways: priorities at
    or above WORKING_ORDERS may spend the management reserve, discovery may be
    capped at its policy share, and a pacing penalty stops DISCOVERY outright
    while merely slowing the rest.
    """

    EXITS_MANAGEMENT = 1
    WORKING_ORDERS = 2
    AUTHORIZATION = 3
    CANDIDATE_CONSTRUCTION = 4
    DISCOVERY = 5


class DiscoveryPaced(EngineError):
    """Discovery is paused because the broker paced us. Management is not."""


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
    """One budget per broker connection, shared by every consumer of it.

    Scanning, marking, management, reconciliation and authorization all draw
    from the same buckets, because IBKR meters the *connection*, not the
    module -- a scanner with its own private budget would spend the
    management path's allowance without knowing it.
    """

    def __init__(
        self,
        *,
        historical_per_window: int = 55,
        historical_window_seconds: float = 600.0,
        general_per_window: int = 40,
        general_window_seconds: float = 60.0,
        #: The fraction of each bucket only EXITS_MANAGEMENT and
        #: WORKING_ORDERS may spend. Sized so a full discovery burst leaves
        #: enough behind to mark and exit every held position.
        management_reserve_fraction: float = 0.25,
        #: Maximum fraction of a bucket that DISCOVERY may consume.  The
        #: remainder is left for candidate construction and authorization in
        #: addition to the management reserve.  ``1.0`` preserves the legacy
        #: budget behaviour when no autotrader policy is present.
        discovery_fraction: float = 1.0,
        #: Absolute floor for management/working-order capacity.  This is
        #: useful when a small bucket makes a percentage reserve round down.
        minimum_management_requests: int = 0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 0.0 < management_reserve_fraction < 1.0:
            raise ValueError(
                f"management_reserve_fraction must be in (0, 1), got "
                f"{management_reserve_fraction}"
            )
        if not 0.0 < discovery_fraction <= 1.0:
            raise ValueError(
                f"discovery_fraction must be in (0, 1], got {discovery_fraction}"
            )
        if (
            isinstance(minimum_management_requests, bool)
            or not isinstance(minimum_management_requests, int)
            or minimum_management_requests < 0
        ):
            raise ValueError(
                "minimum_management_requests must be a non-negative integer"
            )
        now = clock()
        self.clock = clock
        self.sleeper = sleeper
        self.reserve_fraction = management_reserve_fraction
        self.discovery_fraction = discovery_fraction
        self.minimum_management_requests = minimum_management_requests
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
        self._discovery_paused_until = 0.0

    def _advance(self, bucket: _Bucket) -> None:
        now = self.clock()
        elapsed = max(0.0, now - bucket.updated)
        bucket.tokens = min(
            float(bucket.capacity), bucket.tokens + elapsed * bucket.refill_rate()
        )
        bucket.updated = now

    def _floor(self, bucket: _Bucket, priority: Priority) -> float:
        """Tokens this priority must leave untouched.

        The reserve exists for the book that already exists: exits and
        working-order supervision spend down to zero; everything else stops
        at the reserve line, so no amount of scanning can starve an exit.
        """
        if priority <= Priority.WORKING_ORDERS:
            return 0.0
        # Never a floor the bucket cannot clear: a reserve of capacity-or-more
        # would make ``tokens >= 1 + floor`` unsatisfiable and spin the
        # acquire loop forever (found by exactly that hang, 2026-08-01). A
        # one-token bucket therefore has no effective reserve -- the honest
        # reading of an impossible configuration, stated here rather than
        # discovered as a frozen scanner.
        management_floor = max(
            bucket.capacity * self.reserve_fraction,
            float(self.minimum_management_requests),
        )
        if priority is Priority.DISCOVERY:
            # ``discovery_fraction`` is a cap, not an extra pool: discovery
            # may consume at most that share of the rolling window.  Candidate
            # construction and authorization can use the remaining share,
            # while the management floor always remains protected.
            management_floor = max(
                management_floor,
                bucket.capacity * (1.0 - self.discovery_fraction),
            )
        return min(management_floor, bucket.capacity - 1.0)

    def acquire(
        self,
        kind: RequestKind,
        *,
        priority: Priority = Priority.CANDIDATE_CONSTRUCTION,
    ) -> None:
        """Take one token, sleeping until one is available above this
        priority's floor.

        Raises :class:`DiscoveryPaced` -- instead of waiting -- for DISCOVERY
        requests while a pacing penalty is in force: a paced scan should
        stop scanning, not queue up more of what caused the pacing. Every
        other priority waits; none is ever refused.
        """
        if (
            priority is Priority.DISCOVERY
            and self.clock() < self._discovery_paused_until
        ):
            remaining = self._discovery_paused_until - self.clock()
            raise DiscoveryPaced(
                f"discovery is paused for another {remaining:.0f}s after a broker "
                "pacing penalty",
                hint="management, exits and authorization continue; re-run "
                "discovery after the pause",
            )
        bucket = self._buckets[kind]
        floor = self._floor(bucket, priority)
        while True:
            self._advance(bucket)
            if bucket.tokens >= 1.0 + floor:
                bucket.tokens -= 1.0
                return
            shortfall = (1.0 + floor - bucket.tokens) / bucket.refill_rate()
            wait = max(0.05, shortfall)
            self.waited_seconds += wait
            self.sleeper(wait)

    def penalize(self, kind: RequestKind = RequestKind.HISTORICAL) -> None:
        """The broker paced us anyway: halve the refill rate for this window,
        drop the tokens on hand -- the broker's ledger, not ours, is the one
        that counts -- and pause discovery for a full penalized window.
        Management keeps drawing on the (refilling) reserve throughout.
        """
        bucket = self._buckets[kind]
        self._advance(bucket)
        bucket.penalty *= 2.0
        bucket.tokens = 0.0
        self._discovery_paused_until = self.clock() + (
            bucket.window_seconds * bucket.penalty
        )


class SharedPacingBudget:
    """One connection budget with a durable reservation authority.

    The token bucket remains the low-latency wait mechanism, but every broker
    request also obtains and commits a row in the session's SQLite pacing
    ledger.  That makes history, contract qualification, market data, and
    restart recovery observe one budget instead of silently maintaining
    module-local allowances.  A durable refusal is fail-closed: no caller is
    permitted to send an unreserved request merely because its process-local
    bucket still has a token.
    """

    def __init__(
        self,
        local: PacedRequestBudget,
        ledger: object,
        *,
        owner_id: str,
        clock: Callable[[], dt.datetime],
    ) -> None:
        if not owner_id.strip():
            raise ValueError("shared pacing owner_id must be non-empty")
        self.local = local
        self.ledger = ledger
        self.owner_id = owner_id
        self.clock = clock
        self._sequence = 0

    @property
    def waited_seconds(self) -> float:
        return self.local.waited_seconds

    def _request_key(self, kind: RequestKind) -> str:
        self._sequence += 1
        return f"{self.owner_id}:{kind.value.lower()}:{self._sequence}:{uuid4().hex}"

    def acquire(
        self,
        kind: RequestKind,
        *,
        priority: Priority = Priority.CANDIDATE_CONSTRUCTION,
    ) -> None:
        now = self.clock()
        reservation = self.ledger.reserve(
            kind,
            cost=1,
            priority=priority,
            owner_id=self.owner_id,
            request_key=self._request_key(kind),
            now=now,
        )
        if reservation is None:
            raise DiscoveryPaced(
                f"shared durable pacing ledger refused {kind.value} at {priority.name}"
            )
        try:
            self.local.acquire(kind, priority=priority)
        except Exception:
            self.ledger.release(reservation.reservation_id)
            raise
        self.ledger.commit(reservation.reservation_id, actual_cost=1, now=self.clock())

    def penalize(self, kind: RequestKind = RequestKind.HISTORICAL) -> None:
        self.local.penalize(kind)
        self.ledger.penalize(kind, now=self.clock())
