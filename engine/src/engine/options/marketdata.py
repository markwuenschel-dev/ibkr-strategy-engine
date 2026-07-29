"""Market-data provenance, greek normalization, and the live-data gate.

This module exists because three separate properties of ``ib_async`` make the
obvious implementation of "is this data live?" quietly wrong.

**1. The entitlement field fails open.** ``Ticker.marketDataType`` is declared
``= 1`` (``ticker.py:56``) and written only when TWS sends its market-data-type
message (``wrapper.py:889-892``). Nothing in ``reqMktData`` seeds it. So a
ticker that has never heard from the server is indistinguishable from one the
server called live, and ``ticker.marketDataType == 1`` is not evidence of
anything. Provenance therefore records ``callback_received`` separately, and an
absent callback classifies as :attr:`Liveness.UNKNOWN` -- never LIVE.

**2. Greeks survive their subscription.** ``Ticker.__post_init__`` resets the
float fields but leaves ``bidGreeks``/``askGreeks``/``lastGreeks``/
``modelGreeks`` alone, and tickers are reused per contract keyed on
``hash(contract)`` (``wrapper.py:406-411``). A ``modelGreeks`` from an earlier
subscription, or from before a market-data-type change, is still sitting there.
So every subscription mints a generation UUID, greeks are stamped with the
generation that was active when the callback arrived, and a greek from an
earlier generation is treated as absent rather than stale-but-usable.

**3. Two sentinels get through.** ``wrapper.py:1390-1391`` reads
``vega if vega != -2 else vega`` -- both branches identical, a copy-paste slip
-- so theta and vega surface as the raw ``-2.0`` sentinel instead of ``None``.
It is finite and plausible-looking, so the DBL_MAX screen at
``broker.py:467-468`` does not catch it. Separately, IBKR sends DBL_MAX for
"field does not apply", which is also finite. Both are screened here.

The upstream bug may be fixed one day. The normalization stays regardless: the
application boundary is the right place to decide what a number means, and a
fix that lives in a pinned dependency is a fix that can be un-pinned.

Nothing here exposes a raw ``Ticker``. Strategy code receives frozen snapshots,
so it cannot observe a value mutating underneath a decision it is in the middle
of making.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum, IntEnum
from typing import Any
from uuid import UUID, uuid4

from ..errors import MarketDataRefusedError

__all__ = [
    "MarketDataType",
    "Liveness",
    "RefusalReason",
    "MarketDataProvenance",
    "OptionGreeks",
    "OptionQuote",
    "UnderlyingQuote",
    "MarketDataSubscription",
    "normalize_greek",
    "require_live_quote",
    "require_uniform_live_provenance",
    "IB_UNSET",
    "GREEK_SENTINEL",
]

# IBKR's "field does not apply" value. Finite, so a NaN/inf screen misses it.
# Mirrors engine.broker.IB_UNSET; duplicated rather than imported so the
# options market-data boundary does not depend on the equity broker module.
IB_UNSET = 1.7976931348623157e308

# The value ib_async fails to null for theta and vega.
GREEK_SENTINEL = -2.0


class MarketDataType(IntEnum):
    """IBKR's four market-data types, as ``reqMarketDataType`` numbers them."""

    LIVE = 1
    FROZEN = 2
    DELAYED = 3
    DELAYED_FROZEN = 4


class Liveness(str, Enum):
    """What the provider actually said, once. ``UNKNOWN`` is not a failure
    state to be retried past -- it is the honest classification of a
    subscription that has told us nothing, and it must never pass as live."""

    LIVE = "LIVE"
    FROZEN = "FROZEN"
    DELAYED = "DELAYED"
    DELAYED_FROZEN = "DELAYED_FROZEN"
    UNKNOWN = "UNKNOWN"


class RefusalReason(str, Enum):
    """Machine-readable causes, so a caller can branch without parsing prose."""

    REALTIME_DATA_REQUIRED = "OPTIONS_REALTIME_DATA_REQUIRED"
    NO_DATA_TYPE_CALLBACK = "MARKET_DATA_TYPE_CALLBACK_MISSING"
    STALE_QUOTE = "MARKET_DATA_STALE"
    NO_PROVIDER_TIMESTAMP = "MARKET_DATA_PROVIDER_TIMESTAMP_MISSING"
    NO_LOCAL_TIMESTAMP = "MARKET_DATA_LOCAL_TIMESTAMP_MISSING"
    GENERATION_MISMATCH = "MARKET_DATA_GENERATION_MISMATCH"
    MIXED_PROVENANCE = "MARKET_DATA_MIXED_PROVENANCE"
    GREEKS_MISSING = "OPTION_GREEKS_MISSING"
    DELTA_INVALID = "OPTION_DELTA_INVALID"


_LIVENESS_BY_TYPE = {
    MarketDataType.LIVE: Liveness.LIVE,
    MarketDataType.FROZEN: Liveness.FROZEN,
    MarketDataType.DELAYED: Liveness.DELAYED,
    MarketDataType.DELAYED_FROZEN: Liveness.DELAYED_FROZEN,
}


def _refuse(reason: RefusalReason, message: str, *, hint: str | None = None) -> None:
    raise MarketDataRefusedError(reason.value, message, hint=hint)


# ---------------------------------------------------------------------------
# Greek normalization
# ---------------------------------------------------------------------------


def normalize_greek(value: float | None, field_name: str) -> Decimal | None:
    """Turn one raw greek from IBKR into a Decimal or an honest ``None``.

    Absent is represented as ``None`` and never as 0.0: a zero delta is a real,
    meaningful value for a far out-of-the-money option, and conflating it with
    "we did not receive one" would let a missing greek select a strike.

    Deviates from the reference implementation in two ways, both widening the
    screen rather than narrowing it:

    * DBL_MAX is rejected for every field. It is finite, so ``math.isfinite``
      passes it, and it is IBKR's "does not apply" marker.
    * The ``-2.0`` sentinel is rejected for gamma as well as theta and vega.
      ``ib_async`` nulls gamma correctly today (``wrapper.py:1389``), but a
      gamma of exactly -2.0 is not a value a real option produces, and relying
      on the upstream branch means relying on the same code that got theta and
      vega wrong.
    """
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    if abs(value) >= IB_UNSET:
        return None
    if field_name in {"theta", "vega", "gamma"} and value == GREEK_SENTINEL:
        return None
    if field_name == "delta" and not -1.0 <= value <= 1.0:
        return None
    if field_name in {"implied_volatility", "underlying_price"} and value <= 0:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):  # pragma: no cover - str(float) is safe
        return None


@dataclass(frozen=True)
class OptionGreeks:
    """A normalized option computation, stamped with the subscription it came
    from. Every field is already screened; there are no sentinels in here."""

    received_at: datetime
    subscription_generation: UUID
    implied_volatility: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    vega: Decimal | None = None
    theta: Decimal | None = None
    underlying_price: Decimal | None = None

    @classmethod
    def from_ib(
        cls,
        computation: Any,
        *,
        received_at: datetime,
        subscription_generation: UUID,
    ) -> OptionGreeks:
        """Build from an ``ib_async`` ``OptionComputation``, or anything shaped
        like one. Attributes are read defensively: a computation missing a field
        yields ``None`` for it rather than raising, because a partial greek set
        is a normal thing for IBKR to send and an eligibility check downstream
        is the right place to reject it."""
        return cls(
            received_at=received_at,
            subscription_generation=subscription_generation,
            implied_volatility=normalize_greek(
                getattr(computation, "impliedVol", None), "implied_volatility"
            ),
            delta=normalize_greek(getattr(computation, "delta", None), "delta"),
            gamma=normalize_greek(getattr(computation, "gamma", None), "gamma"),
            vega=normalize_greek(getattr(computation, "vega", None), "vega"),
            theta=normalize_greek(getattr(computation, "theta", None), "theta"),
            underlying_price=normalize_greek(
                getattr(computation, "undPrice", None), "underlying_price"
            ),
        )

    @property
    def has_valid_delta(self) -> bool:
        """The one greek strike selection cannot proceed without.

        ``modelGreeks is not None`` proves nothing: ``wrapper.py:1383-1393``
        assigns the computation even when every field sanitizes away.
        """
        return self.delta is not None


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketDataProvenance:
    """Where a quote came from, and whether the provider ever said so.

    ``requested_type`` and ``reported_type`` are deliberately separate fields.
    Collapsing them -- which is what ``engine.broker.Quote`` does today, storing
    the requested constant and calling it the market data type -- destroys the
    only evidence that distinguishes "the server confirmed live" from "we asked
    for live and heard nothing back".
    """

    requested_type: int
    subscription_generation: UUID
    subscribed_at: datetime
    reported_type: int | None = None
    callback_received: bool = False
    last_provider_event_at: datetime | None = None
    last_local_receive_at: datetime | None = None

    @property
    def liveness(self) -> Liveness:
        """UNKNOWN unless the provider told us, in this generation, what it is."""
        if not self.callback_received or self.reported_type is None:
            return Liveness.UNKNOWN
        try:
            return _LIVENESS_BY_TYPE[MarketDataType(self.reported_type)]
        except ValueError:
            return Liveness.UNKNOWN

    @property
    def is_live(self) -> bool:
        return self.liveness is Liveness.LIVE

    def age_at(self, when: datetime) -> timedelta | None:
        """Measured from the **provider's** event time, not local receipt.

        A delayed quote is delivered to us promptly -- its local receive time is
        recent and a staleness check built on it would pass. The provider's own
        timestamp is the only one that reflects how old the price is.
        """
        if self.last_provider_event_at is None:
            return None
        return when - self.last_provider_event_at

    def describe(self) -> str:
        reported = "none" if self.reported_type is None else str(self.reported_type)
        return (
            f"requested={self.requested_type} reported={reported} "
            f"callback={'yes' if self.callback_received else 'no'} "
            f"-> {self.liveness.value}"
        )


# ---------------------------------------------------------------------------
# Normalized quotes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnderlyingQuote:
    """The underlying's price, with its own provenance.

    Separate from the option's because IBKR states the greeks requirement in
    exactly these terms: "a market data subscription for both the underlying and
    derivative are necessary for options greeks data". One subscription being
    live tells you nothing about the other, and error 10090 is what IBKR sends
    when only one of them is present.
    """

    symbol: str
    provenance: MarketDataProvenance
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    close: Decimal | None = None

    @property
    def mid(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / Decimal("2")


@dataclass(frozen=True)
class OptionQuote:
    """One option leg's market state, frozen at snapshot time."""

    con_id: int
    provenance: MarketDataProvenance
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    close: Decimal | None = None
    open_interest: int | None = None
    volume: int | None = None
    greeks: OptionGreeks | None = None

    @property
    def mid(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def spread_fraction(self) -> Decimal | None:
        """Spread as a fraction of mid. ``None`` when mid is not positive --
        a zero or negative mid makes the ratio meaningless, and returning a
        large number instead would look like a merely-wide market."""
        mid = self.mid
        spread = self.spread
        if mid is None or spread is None or mid <= 0:
            return None
        return spread / mid

    @property
    def delta(self) -> Decimal | None:
        return self.greeks.delta if self.greeks is not None else None


# ---------------------------------------------------------------------------
# Subscription lifecycle
# ---------------------------------------------------------------------------


@dataclass
class MarketDataSubscription:
    """Mutable holder for one contract's in-flight subscription state.

    The adapter feeds raw callbacks in; strategy code only ever receives the
    frozen snapshots this produces. That asymmetry is the point -- it is what
    makes it impossible for a decision to read a value that changes while the
    decision is being made.

    A new generation is minted per subscription. Anything recorded under a
    previous generation is dropped rather than carried forward, which is what
    stops a stale ``modelGreeks`` from a prior market-data-type request from
    being read as current.
    """

    requested_type: int
    subscribed_at: datetime
    generation: UUID = field(default_factory=uuid4)
    reported_type: int | None = None
    callback_received: bool = False
    last_provider_event_at: datetime | None = None
    last_local_receive_at: datetime | None = None
    greeks: OptionGreeks | None = None

    def restart(self, *, requested_type: int, at: datetime) -> UUID:
        """Begin a new generation. Everything previously observed is discarded.

        Called on resubscribe and on any market-data-type change. Returns the
        new generation so the caller can stamp its own bookkeeping.
        """
        self.generation = uuid4()
        self.requested_type = requested_type
        self.subscribed_at = at
        self.reported_type = None
        self.callback_received = False
        self.last_provider_event_at = None
        self.last_local_receive_at = None
        self.greeks = None
        return self.generation

    def record_data_type(self, reported_type: int, *, at: datetime) -> None:
        """The server's market-data-type message. This is the only thing that
        may set ``callback_received``."""
        self.reported_type = reported_type
        self.callback_received = True
        self.last_local_receive_at = at

    def record_greeks(
        self,
        computation: Any,
        *,
        at: datetime,
        generation: UUID | None = None,
    ) -> None:
        """Record an option computation, refusing one from an old generation.

        ``generation`` is what the adapter observed at the moment the callback
        fired. When it does not match the current one the computation is
        dropped: it describes a subscription that no longer exists.
        """
        if generation is not None and generation != self.generation:
            return
        self.greeks = OptionGreeks.from_ib(
            computation, received_at=at, subscription_generation=self.generation
        )
        self.last_local_receive_at = at

    def record_provider_event(self, at: datetime) -> None:
        """The provider's own timestamp for the most recent tick."""
        self.last_provider_event_at = at

    def provenance(self) -> MarketDataProvenance:
        return MarketDataProvenance(
            requested_type=self.requested_type,
            subscription_generation=self.generation,
            subscribed_at=self.subscribed_at,
            reported_type=self.reported_type,
            callback_received=self.callback_received,
            last_provider_event_at=self.last_provider_event_at,
            last_local_receive_at=self.last_local_receive_at,
        )

    def current_greeks(self) -> OptionGreeks | None:
        """Greeks only if they belong to the generation now in force."""
        if self.greeks is None:
            return None
        if self.greeks.subscription_generation != self.generation:
            return None
        return self.greeks


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def require_live_quote(
    provenance: MarketDataProvenance,
    *,
    decision_time: datetime,
    maximum_age: timedelta,
    active_generation: UUID,
    label: str,
) -> None:
    """Refuse unless this data is live, current, and from the live subscription.

    Raises rather than returning a verdict, in keeping with the rest of the
    engine's gates: there is no boolean here for a caller to forget to check.

    Frozen is refused alongside delayed. It may be permitted for position
    accounting outside trading hours, but that is a different call site with a
    different policy -- it is not something this function should be talked into
    with a flag, because the flag is what would eventually be set by default.
    """
    if not provenance.callback_received:
        _refuse(
            RefusalReason.NO_DATA_TYPE_CALLBACK,
            f"{label}: the provider never reported a market-data type",
            hint="Ticker.marketDataType defaults to 1, so silence is not live; "
            "an absent callback is UNKNOWN",
        )

    if not provenance.is_live:
        _refuse(
            RefusalReason.REALTIME_DATA_REQUIRED,
            f"{label}: market data is {provenance.liveness.value}, not LIVE "
            f"({provenance.describe()})",
            hint="delayed data may be used for adapter development and shadow "
            "runs, never for order selection",
        )

    if provenance.subscription_generation != active_generation:
        _refuse(
            RefusalReason.GENERATION_MISMATCH,
            f"{label}: quote belongs to subscription generation "
            f"{provenance.subscription_generation}, active is {active_generation}",
            hint="ib_async reuses ticker objects across subscriptions; a value "
            "from an earlier generation is not current data",
        )

    if provenance.last_provider_event_at is None:
        _refuse(
            RefusalReason.NO_PROVIDER_TIMESTAMP,
            f"{label}: no provider event timestamp, so age cannot be established",
        )

    if provenance.last_local_receive_at is None:
        _refuse(
            RefusalReason.NO_LOCAL_TIMESTAMP,
            f"{label}: no local receive timestamp",
        )

    age = provenance.age_at(decision_time)
    if age is None or age > maximum_age:
        _refuse(
            RefusalReason.STALE_QUOTE,
            f"{label}: quote is {age} old, limit is {maximum_age}",
            hint="age is measured from the provider's event time; a delayed "
            "quote arrives promptly and would pass a local-receipt check",
        )


def require_uniform_live_provenance(
    *,
    underlying: UnderlyingQuote,
    legs: Sequence[OptionQuote],
    decision_time: datetime,
    maximum_age: timedelta,
    active_generations: dict[str, UUID],
) -> None:
    """Every leg and the underlying must be live, current, and consistent.

    ``active_generations`` maps ``"underlying"`` and each leg's ``str(con_id)``
    to the generation the subscription manager currently holds.

    The cross-checks matter as much as the per-quote ones. IBKR computes greeks
    from the underlying price, so an underlying that is live while the options
    are delayed produces deltas that look fine and describe a different market.
    The reverse fails the same way.
    """
    if not legs:
        _refuse(
            RefusalReason.MIXED_PROVENANCE,
            "a strategy quote snapshot must contain at least one leg",
        )

    require_live_quote(
        underlying.provenance,
        decision_time=decision_time,
        maximum_age=maximum_age,
        active_generation=active_generations["underlying"],
        label=f"underlying {underlying.symbol}",
    )

    for leg in legs:
        require_live_quote(
            leg.provenance,
            decision_time=decision_time,
            maximum_age=maximum_age,
            active_generation=active_generations[str(leg.con_id)],
            label=f"option {leg.con_id}",
        )

    # Per-quote checks above already proved each one LIVE, so a disagreement
    # here would mean a classification bug rather than an entitlement problem.
    # Checked anyway: this is the assertion that catches a future refactor that
    # loosens require_live_quote without noticing the cross-leg consequence.
    livenesses = {leg.provenance.liveness for leg in legs}
    livenesses.add(underlying.provenance.liveness)
    if livenesses != {Liveness.LIVE}:
        _refuse(
            RefusalReason.MIXED_PROVENANCE,
            f"mixed provenance across the structure: {sorted(l.value for l in livenesses)}",
        )

    missing = [leg.con_id for leg in legs if leg.greeks is None]
    if missing:
        _refuse(
            RefusalReason.GREEKS_MISSING,
            f"no greeks for option legs {missing}",
        )

    invalid = [
        leg.con_id
        for leg in legs
        if leg.greeks is not None and not leg.greeks.has_valid_delta
    ]
    if invalid:
        _refuse(
            RefusalReason.DELTA_INVALID,
            f"greeks present but delta missing for option legs {invalid}",
            hint="modelGreeks is assigned even when every field sanitizes away; "
            "presence is not validity",
        )

    stale_greeks = [
        leg.con_id
        for leg in legs
        if leg.greeks is not None
        and leg.greeks.subscription_generation != active_generations[str(leg.con_id)]
    ]
    if stale_greeks:
        _refuse(
            RefusalReason.GENERATION_MISMATCH,
            f"greeks from a superseded subscription for legs {stale_greeks}",
        )
