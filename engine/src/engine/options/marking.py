"""What an open position is worth right now, and what it would cost to exit.

The engine held a filled paper spread and could not mark it. Every management
pass printed the same line::

    HOLD [LIFECYCLE_NO_MARK_AVAILABLE] no mark for this position, so the profit
         target cannot be evaluated; 50 days to expiry, so no calendar rule applies

The 50%-profit rule was not merely failing to fire -- it was *structurally unable*
to fire, because everything in position management is downstream of a price the
engine never obtained for a position it already held. The exit logic was never
the blocker. This module is the missing input.

**Closing a credit spread BUYS it back, and the pairing is the whole file.**

A credit structure was opened by selling the near leg and buying the far one. To
retire it you do the opposite: you *buy back* the leg you are short and *sell*
the leg you are long. So each side is valued at the price someone else is
standing ready to trade at -- the side of the book you would hit, not the side
you would join::

    natural closing debit  =  short leg ASK  -  long leg BID

Getting that backwards -- short bid minus long ask -- produces a smaller,
flattering number that **cannot be traded**. It is not a price; it is what you
would pay if two independent counterparties both happened to come to you. That
is the identical error that left a real order resting unfilled for 160 minutes,
recorded in :mod:`engine.options.pricing`, pointed the other way. A test in
:mod:`tests.test_options_marking` swaps the two sides and asserts the result
differs and flatters, so the pairing cannot be quietly inverted again.

The mirror relationship with the opening side is exact, and worth stating because
it is what stops someone "reusing" the wrong function:

    opening natural CREDIT  =  short BID - long ASK   (pricing.natural_credit)
    closing natural DEBIT   =  short ASK - long BID   (this module)

The closing debit is **not** the negation of the opening credit. Both cross the
market; they cross it in opposite directions, so both give up the spread.

**Four states, and they are states rather than prose.**

``MARKED``                  a live, fresh, two-sided book on every leg, and the
                            fill cost is fully known. Gross *and* net are stated.
``STALE``                   the quotes exist but their provenance disqualifies
                            them -- delayed, frozen, unconfirmed, or older than
                            the policy allows. **No number is produced.** A price
                            you may not act on must not be reported as one you
                            may, and the operator's response differs by cause, so
                            the precise market-data reason travels alongside.
``UNAVAILABLE``             there is no usable book: a leg went unquoted, a side
                            is missing, or the adapter refused outright.
``COMMISSION_INCOMPLETE``   the mark is good and gross is reported, but the
                            broker never costed the fill, so net is withheld
                            rather than computed against an assumed zero.

The precedence is severity order: UNAVAILABLE, then STALE, then
COMMISSION_INCOMPLETE, then MARKED. A position with no quotes *and* no commission
evidence reports UNAVAILABLE, because the missing commission is not the thing
stopping it from being marked.

**Fail closed, and specifically fail closed on the flattering side.** Every
refusal path returns no number at all rather than a substituted one. Zero is
never used for a missing commission; the last trade and the close are never used
for a missing quote. Each of those substitutions moves the answer toward "the
target has been reached", which is the direction that sends an order.

**This module marks and proposes. It transmits nothing.** There is no broker
write anywhere in it: it consumes a
:class:`~engine.options.ports.StrategyQuoteSnapshot` and produces a report and,
at most, a :class:`CloseProposal` -- a fully-built but wholly unauthorized
intent. Authorization lives behind the single door in
:mod:`engine.options.transmit` and is not reachable from here.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from ..errors import InvalidStrategyError, MarketDataRefusedError
from .domain import OptionLegIntent, OptionStrategyIntent
from .executions import CommissionEvidence
from .lifecycle import profit_target_debit
from .marketdata import OptionQuote, require_live_quote
from .policy import RiskPolicy
from .ports import LiveMarketDataPort, StrategyQuoteSnapshot
from .positions import OpenPosition, PositionState

__all__ = [
    "QuoteSide",
    "CLOSING_SHORT_SIDE",
    "CLOSING_LONG_SIDE",
    "MarkState",
    "MarkReason",
    "ClosingPrices",
    "CloseProposal",
    "PositionMarkReport",
    "closing_debit",
    "closing_natural_debit",
    "closing_midpoint_debit",
    "confirmed_remaining_quantity",
    "propose_close",
    "mark_position",
    "mark_open_positions",
]

ZERO = Decimal("0")


def _price(value: Decimal | None) -> str:
    """A per-share option price. Three places: a midpoint lands on half-cents."""
    return "--" if value is None else f"{value:.3f}"


def _money(value: Decimal | None) -> str:
    """A dollar amount, signed, two places."""
    return "--" if value is None else f"{value:,.2f}"


# ---------------------------------------------------------------------------
# The direction
# ---------------------------------------------------------------------------


class QuoteSide(str, Enum):
    """Which side of one leg's book a price is taken from.

    Named rather than expressed as a bare ``quote.bid``/``quote.ask`` choice at
    the point of use, because the choice is the thing most worth being able to
    read, assert on, and get wrong exactly once.
    """

    BID = "bid"
    ASK = "ask"
    MID = "mid"


#: Closing a credit structure buys back the leg that was sold, so it **lifts the
#: ask**. This constant and the next are the two facts the whole module rests on.
CLOSING_SHORT_SIDE = QuoteSide.ASK

#: ...and sells the leg that was bought, so it **hits the bid**.
CLOSING_LONG_SIDE = QuoteSide.BID


def _side_price(quote: OptionQuote, side: QuoteSide) -> Decimal | None:
    if side is QuoteSide.BID:
        return quote.bid
    if side is QuoteSide.ASK:
        return quote.ask
    return quote.mid


def closing_debit(
    legs: Sequence[OptionLegIntent],
    snapshot: StrategyQuoteSnapshot | None,
    *,
    short_side: QuoteSide,
    long_side: QuoteSide,
) -> Decimal | None:
    """Per-share cost of buying this structure back, valuing each side as asked.

    ``short_side`` and ``long_side`` are **required and have no defaults.** A
    default is exactly how the inverted pairing would come back: the caller that
    got it wrong would not have written anything wrong, it would have written
    nothing. :func:`closing_natural_debit` and :func:`closing_midpoint_debit` are
    the two pairings this engine actually uses, and they are the ones to call.

    Per share, before the multiplier, to match
    :attr:`~engine.options.positions.OpenPosition.filled_credit`'s unit.
    Comparing a per-contract debit against a per-share credit reports every
    position as a hundred times away from its target, in the safe direction,
    forever.

    ``None`` -- never a substituted number -- when any leg is unquoted on the
    side it needs. A one-sided market has no closing price, and the last trade or
    the previous close would produce a mark backed by nothing.
    """
    if snapshot is None:
        return None
    quotes = {quote.con_id: quote for quote in snapshot.legs}
    total = ZERO
    for leg in legs:
        quote = quotes.get(leg.con_id)
        if quote is None:
            return None
        side = short_side if leg.is_short else long_side
        price = _side_price(quote, side)
        if price is None or price < ZERO:
            return None
        # Buying back what was sold costs; selling what was bought returns.
        total += price * leg.ratio if leg.is_short else -price * leg.ratio
    return total


def closing_natural_debit(
    legs: Sequence[OptionLegIntent], snapshot: StrategyQuoteSnapshot | None
) -> Decimal | None:
    """Short leg's **ask** minus long leg's **bid** -- the tradeable exit price.

    The worst executable price, and being the worst is the point: it is what the
    book will actually pay to let this position go right now, with no waiting.
    """
    return closing_debit(
        legs, snapshot, short_side=CLOSING_SHORT_SIDE, long_side=CLOSING_LONG_SIDE
    )


def closing_midpoint_debit(
    legs: Sequence[OptionLegIntent], snapshot: StrategyQuoteSnapshot | None
) -> Decimal | None:
    """Short mids minus long mids -- the accounting mark, not an exit price.

    Correct for valuing a position and wrong for exiting one: a mid is the
    average of two quotes, and an average is not a quote. It is reported
    alongside the natural so the operator can see the whole band a real exit
    lands in.
    """
    return closing_debit(
        legs, snapshot, short_side=QuoteSide.MID, long_side=QuoteSide.MID
    )


# ---------------------------------------------------------------------------
# The states
# ---------------------------------------------------------------------------


class MarkState(str, Enum):
    """Exactly four, in ascending order of how much is known.

    Distinct states rather than a message, because the operator's response
    differs by each: ``UNAVAILABLE`` is a subscription to look at, ``STALE`` is an
    entitlement or a clock, ``COMMISSION_INCOMPLETE`` is a fill to go back for,
    and ``MARKED`` is a number to act on. Free text carrying all four would have
    to be parsed to be branched on.
    """

    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"
    COMMISSION_INCOMPLETE = "COMMISSION_INCOMPLETE"
    MARKED = "MARKED"

    @property
    def has_mark(self) -> bool:
        """Whether a closing price was produced. False for the two refusals."""
        return self in (MarkState.MARKED, MarkState.COMMISSION_INCOMPLETE)


class MarkReason(str, Enum):
    """Machine-readable causes, prefixed for the layer that produced them.

    Market-data refusals do **not** appear here: when
    :func:`~engine.options.marketdata.require_live_quote` refuses, its own
    ``OPTIONS_REALTIME_DATA_REQUIRED`` / ``MARKET_DATA_STALE`` code is carried
    through verbatim. Re-coding it would throw away the distinction between "buy
    a subscription" and "the quote aged out", which are different problems with
    the same state.
    """

    OK = "MARK_OK"
    NO_SNAPSHOT = "MARK_NO_SNAPSHOT"
    QUOTES_REFUSED = "MARK_QUOTES_REFUSED"
    LEG_QUOTE_MISSING = "MARK_LEG_QUOTE_MISSING"
    ONE_SIDED_MARKET = "MARK_ONE_SIDED_MARKET"
    NEGATIVE_DEBIT = "MARK_NEGATIVE_DEBIT"
    CROSSED_BOOK = "MARK_CROSSED_BOOK"
    NO_QUOTE_TIMESTAMP = "MARK_NO_QUOTE_TIMESTAMP"
    COMMISSION_INCOMPLETE = "MARK_COMMISSION_INCOMPLETE"


@dataclass(frozen=True)
class ClosingPrices:
    """Both closing prices for one structure, taken from one snapshot.

    Carried together because reporting either alone invites the wrong one being
    used: the midpoint is the honest *valuation* and the natural is the honest
    *exit*, and the gap between them is what an exit actually costs to cross.
    """

    midpoint_debit: Decimal
    natural_debit: Decimal
    as_of: dt.datetime

    def __post_init__(self) -> None:
        for label in ("midpoint_debit", "natural_debit"):
            value = getattr(self, label)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{label} must be a finite Decimal, got {value!r}")
            if value < ZERO:
                raise ValueError(
                    f"{label} must not be negative, got {value}; a negative "
                    "buy-back price is a sign error, not a free position"
                )
        if self.natural_debit < self.midpoint_debit:
            raise ValueError(
                f"the natural closing debit {self.natural_debit} is below the "
                f"midpoint {self.midpoint_debit}, which cannot happen on an "
                "uncrossed book -- crossing the market to exit always costs at "
                "least the mid, so this is the inverted bid/ask pairing"
            )
        if not isinstance(self.as_of, dt.datetime) or self.as_of.tzinfo is None:
            raise ValueError("as_of must be a timezone-aware datetime")

    @property
    def crossing_cost(self) -> Decimal:
        """What giving up the whole spread costs, per share."""
        return self.natural_debit - self.midpoint_debit


# ---------------------------------------------------------------------------
# The proposal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CloseProposal:
    """A fully-built closing order that **nothing here is able to send.**

    It exists so the quantity and the limit price are decided by the code that
    holds the evidence, rather than by whichever caller eventually acts. The
    intent is real and valid; it carries no authorization, and the authorization
    type it would need lives in :mod:`engine.options.transmit` behind a token
    this module cannot mint.

    ``quantity`` is bounded by ``confirmed_remaining`` and the bound is enforced
    in ``__post_init__``. Ledger C21 records a defensive exit that sold contracts
    never bought, through exactly one omitted quantity argument that silently
    resolved to a plausible value. A bound that is only *satisfied* at the call
    site is a bound that stops holding when a second call site appears.
    """

    position_id: UUID
    quantity: int
    confirmed_remaining: int
    limit_price: Decimal
    basis: str
    intent: OptionStrategyIntent

    def __post_init__(self) -> None:
        if not isinstance(self.quantity, int) or isinstance(self.quantity, bool):
            raise InvalidStrategyError(
                f"proposal quantity must be an int, got {self.quantity!r}"
            )
        if self.quantity <= 0:
            raise InvalidStrategyError(
                f"proposal quantity must be positive, got {self.quantity}"
            )
        if self.quantity > self.confirmed_remaining:
            raise InvalidStrategyError(
                f"cannot propose closing {self.quantity} contracts against a "
                f"confirmed remaining quantity of {self.confirmed_remaining}",
                hint="contracts already retired by an earlier partial close are "
                "not held any more; closing them again is an opening short",
            )
        if self.intent.quantity != self.quantity:
            raise InvalidStrategyError(
                f"the proposal says {self.quantity} contracts and its intent says "
                f"{self.intent.quantity}"
            )
        if not isinstance(self.limit_price, Decimal) or not self.limit_price.is_finite():
            raise InvalidStrategyError(
                f"limit_price must be a finite Decimal, got {self.limit_price!r}"
            )
        if self.limit_price <= ZERO:
            raise InvalidStrategyError(
                f"a closing debit limit must be positive, got {self.limit_price}"
            )

    def describe(self) -> str:
        return (
            f"close {self.quantity} of {self.confirmed_remaining} @ "
            f"{self.limit_price:.3f} debit ({self.basis}) -- proposal only, "
            "nothing is authorized"
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "position_id": str(self.position_id),
            "quantity": self.quantity,
            "confirmed_remaining": self.confirmed_remaining,
            "limit_price": str(self.limit_price),
            "basis": self.basis,
            "closing_strategy_id": str(self.intent.strategy_id),
        }


def confirmed_remaining_quantity(position: OpenPosition) -> int:
    """Contracts this position is **proven** to still hold.

    What the opening order filled, less what the closing order has already
    retired -- :attr:`~engine.options.positions.OpenPosition.remaining_quantity`,
    floored at zero and narrowed to an int.

    Deliberately **not**
    :attr:`~engine.options.positions.OpenPosition.manageable_quantity`, which
    falls back to the *ordered* quantity when nothing is recorded as filled and,
    more importantly, does not subtract a partial close at all. On a three-lot
    that filled three and has already closed two, ``manageable_quantity`` says
    three and the truth is one. Sizing an exit off the first number sells two
    contracts nobody holds.
    """
    remaining = position.remaining_quantity
    if remaining <= ZERO:
        return 0
    return int(remaining)


def propose_close(
    position: OpenPosition,
    *,
    limit_price: Decimal,
    basis: str,
    created_at: dt.datetime,
    configuration_version: str,
    strategy_id: UUID | None = None,
    quantity: int | None = None,
) -> CloseProposal | None:
    """Build the closing order this position would need, sized to what is held.

    ``None`` when nothing is confirmed held -- a position with no recorded fill,
    or one an earlier close has already fully retired. Returning a
    zero-quantity proposal instead would push the check onto every caller, and
    the caller that forgets is the one that sends it.

    The quantity is the **smaller** of what is asked for and what is confirmed
    remaining, and it is additionally capped by ``manageable_quantity`` so the
    guard in :func:`engine.options.lifecycle.closing_intent_for` and this one
    cannot disagree about the same position.
    """
    confirmed = confirmed_remaining_quantity(position)
    if confirmed <= 0:
        return None
    wanted = confirmed if quantity is None else int(quantity)
    size = min(wanted, confirmed, position.manageable_quantity)
    if size <= 0:
        return None
    intent = position.intent.closing_intent(
        strategy_id=strategy_id or uuid4(),
        limit_price=limit_price,
        created_at=created_at,
        configuration_version=configuration_version,
        quantity=size,
    )
    return CloseProposal(
        position_id=position.strategy_id,
        quantity=size,
        confirmed_remaining=confirmed,
        limit_price=limit_price,
        basis=basis,
        intent=intent,
    )


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionMarkReport:
    """One position's mark, its profit and loss, and why it is or is not usable.

    Every monetary field is ``None`` rather than zero when it could not be
    established, and ``__post_init__`` enforces that a refusing state carries no
    prices. The invariant is the point of the type: a report cannot be
    constructed that says ``STALE`` and also hands the reader a number.
    """

    position_id: UUID
    underlying: str
    state: MarkState
    reason_code: str
    detail: str
    evaluated_at: dt.datetime
    entry_credit: Decimal
    multiplier: int
    quantity_marked: int
    confirmed_remaining: int
    profit_target_debit: Decimal
    closing_midpoint_debit: Decimal | None = None
    closing_natural_debit: Decimal | None = None
    gross_at_midpoint: Decimal | None = None
    gross_at_natural: Decimal | None = None
    commission_paid: Decimal | None = None
    net_at_midpoint: Decimal | None = None
    net_at_natural: Decimal | None = None
    profit_target_reached: bool | None = None
    quote_as_of: dt.datetime | None = None
    liveness: str | None = None
    commission_gaps: tuple[str, ...] = ()
    close_proposal: CloseProposal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, MarkState):
            raise ValueError(f"state must be a MarkState, got {self.state!r}")
        if not isinstance(self.reason_code, str) or not self.reason_code.strip():
            raise ValueError("every mark report must carry a machine-readable reason")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ValueError(f"{self.reason_code}: a mark report must explain itself")
        if not isinstance(self.evaluated_at, dt.datetime):
            raise ValueError("evaluated_at must be a datetime")
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware")

        priced = (
            self.closing_midpoint_debit,
            self.closing_natural_debit,
            self.gross_at_midpoint,
            self.gross_at_natural,
        )
        if self.state.has_mark:
            if any(value is None for value in priced):
                raise ValueError(
                    f"{self.state.value} must carry both closing prices and both "
                    "gross figures; a marked position with a missing number is "
                    "the refusal states' job"
                )
            if self.profit_target_reached is None:
                raise ValueError(
                    f"{self.state.value} must state whether the profit target is "
                    "reached; that evaluation is what the mark exists for"
                )
        elif any(value is not None for value in priced):
            raise ValueError(
                f"{self.state.value} must not carry a price; a quote that may not "
                "be acted on must not be reported as one that may"
            )

        # Net is the strict subset of MARKED. This is the check that stops a
        # missing commission being silently replaced by zero.
        nets = (self.net_at_midpoint, self.net_at_natural)
        if self.state is MarkState.MARKED:
            if any(value is None for value in nets) or self.commission_paid is None:
                raise ValueError(
                    "MARKED means the fill cost is known; a report without a "
                    "commission and a net belongs in COMMISSION_INCOMPLETE"
                )
        elif any(value is not None for value in nets):
            raise ValueError(
                f"{self.state.value} must not carry a net figure; net without "
                "complete commission evidence is gross wearing a net's name"
            )

        if self.state is MarkState.COMMISSION_INCOMPLETE and not self.commission_gaps:
            raise ValueError(
                "COMMISSION_INCOMPLETE must say what is missing, or the operator "
                "has no way to know what to go and fetch"
            )
        if self.close_proposal is not None:
            if self.close_proposal.position_id != self.position_id:
                raise ValueError("the close proposal names a different position")
            if self.close_proposal.confirmed_remaining != self.confirmed_remaining:
                raise ValueError(
                    "the close proposal and the report disagree about how many "
                    "contracts are still held"
                )

    @property
    def is_marked(self) -> bool:
        return self.state.has_mark

    def describe(self) -> str:
        # Fixed width on both scales, because the operator reads these in a
        # column. A raw ``str(Decimal)`` prints the natural as "0.24" and the
        # midpoint as "0.210" -- the exponents differ because one is a
        # subtraction and the other is a division -- and two prices that are
        # meant to be compared should not be printed to different precisions.
        # ``to_record`` keeps the unrounded values; this is presentation only.
        lines = [
            f"  {self.state.value:<22} [{self.reason_code}] {self.underlying} "
            f"x{self.quantity_marked}  {self.detail}"
        ]
        if self.state.has_mark:
            lines.append(
                f"      closing debit    mid {_price(self.closing_midpoint_debit)}"
                f"  /  natural {_price(self.closing_natural_debit)}"
            )
            lines.append(
                f"      gross unrealized {_money(self.gross_at_midpoint)} at mid"
                f"  /  {_money(self.gross_at_natural)} at natural"
            )
            if self.state is MarkState.MARKED:
                lines.append(
                    f"      commission paid  {_money(self.commission_paid)}   "
                    f"net {_money(self.net_at_midpoint)} at mid  /  "
                    f"{_money(self.net_at_natural)} at natural"
                )
            else:
                lines.append(
                    "      net unrealized   NOT STATEABLE -- "
                    + "; ".join(self.commission_gaps)
                )
            reached = "REACHED" if self.profit_target_reached else "not reached"
            lines.append(
                f"      profit target    {reached} (needs a debit <= "
                f"{_price(self.profit_target_debit)})"
            )
        if self.quote_as_of is not None:
            lines.append(
                f"      quotes           {self.liveness} as of "
                f"{self.quote_as_of.isoformat()}"
            )
        if self.close_proposal is not None:
            lines.append(f"      {self.close_proposal.describe()}")
        return "\n".join(lines)

    def to_record(self) -> dict[str, Any]:
        def text(value: Decimal | None) -> str | None:
            return str(value) if value is not None else None

        return {
            "event": "position_mark",
            "position_id": str(self.position_id),
            "underlying": self.underlying,
            "state": self.state.value,
            "reason": self.reason_code,
            "detail": self.detail,
            "evaluated_at": self.evaluated_at.isoformat(),
            "entry_credit": str(self.entry_credit),
            "multiplier": self.multiplier,
            "quantity_marked": self.quantity_marked,
            "confirmed_remaining": self.confirmed_remaining,
            "closing_midpoint_debit": text(self.closing_midpoint_debit),
            "closing_natural_debit": text(self.closing_natural_debit),
            "gross_at_midpoint": text(self.gross_at_midpoint),
            "gross_at_natural": text(self.gross_at_natural),
            "commission_paid": text(self.commission_paid),
            "net_at_midpoint": text(self.net_at_midpoint),
            "net_at_natural": text(self.net_at_natural),
            "profit_target_debit": str(self.profit_target_debit),
            "profit_target_reached": self.profit_target_reached,
            "quote_as_of": (
                self.quote_as_of.isoformat() if self.quote_as_of is not None else None
            ),
            "liveness": self.liveness,
            "commission_gaps": list(self.commission_gaps),
            "close_proposal": (
                self.close_proposal.to_record()
                if self.close_proposal is not None
                else None
            ),
        }


# ---------------------------------------------------------------------------
# The computation
# ---------------------------------------------------------------------------


def _latest_provider_event(snapshot: StrategyQuoteSnapshot) -> dt.datetime | None:
    return max(
        (
            quote.provenance.last_provider_event_at
            for quote in snapshot.legs
            if quote.provenance.last_provider_event_at is not None
        ),
        default=None,
    )


def _refusing_report(
    position: OpenPosition,
    *,
    state: MarkState,
    reason_code: str,
    detail: str,
    now: dt.datetime,
    target: Decimal,
    snapshot: StrategyQuoteSnapshot | None = None,
) -> PositionMarkReport:
    liveness = None
    quote_as_of = None
    if snapshot is not None:
        livenesses = sorted({q.provenance.liveness.value for q in snapshot.legs})
        liveness = "/".join(livenesses) if livenesses else None
        quote_as_of = _latest_provider_event(snapshot)
    return PositionMarkReport(
        position_id=position.strategy_id,
        underlying=position.underlying,
        state=state,
        reason_code=reason_code,
        detail=detail,
        evaluated_at=now,
        entry_credit=position.filled_credit,
        multiplier=position.multiplier,
        quantity_marked=0,
        confirmed_remaining=confirmed_remaining_quantity(position),
        profit_target_debit=target,
        quote_as_of=quote_as_of,
        liveness=liveness,
    )


def _gate_legs(
    snapshot: StrategyQuoteSnapshot,
    *,
    now: dt.datetime,
    policy: RiskPolicy,
) -> MarketDataRefusedError | None:
    """Refuse unless every leg quote is live, current, and from this subscription.

    Gates the **legs only**, deliberately, and not the underlying and not the
    greeks. :func:`~engine.options.marketdata.require_uniform_live_provenance` --
    the gate used for strike selection -- additionally demands greeks with a valid
    delta on every leg, because a strike is *chosen* by delta. A mark is not: it
    is computed from leg bid and ask and nothing else. Requiring greeks here would
    refuse to mark a perfectly quoted position for a reason that has no bearing on
    its price, and an engine that cannot mark is the defect this module was
    written to remove. The underlying is subscribed (IBKR needs it to compute
    greeks at all) and its liveness is reported, but it is not an input to the
    arithmetic and so does not gate it.
    """
    generations = snapshot.generation_map()
    for quote in snapshot.legs:
        key = str(quote.con_id)
        if key not in generations:
            return MarketDataRefusedError(
                "MARKET_DATA_GENERATION_MISMATCH",
                f"option {quote.con_id}: no active subscription generation",
            )
        try:
            require_live_quote(
                quote.provenance,
                decision_time=now,
                maximum_age=policy.quote_maximum_age,
                active_generation=generations[key],
                label=f"option {quote.con_id}",
            )
        except MarketDataRefusedError as exc:
            return exc
    return None


def mark_position(
    position: OpenPosition,
    snapshot: StrategyQuoteSnapshot | None,
    *,
    policy: RiskPolicy,
    now: dt.datetime,
    commission: CommissionEvidence | None = None,
    configuration_version: str = "mark",
    propose: bool = True,
    quotes_error: Exception | None = None,
) -> PositionMarkReport:
    """Mark one open position and say, in one of four states, how far that got.

    Pure: no I/O, no clock read. ``now`` is a parameter, so a report is
    reproducible from the record it wrote -- the same contract
    :func:`engine.options.lifecycle.decide_management_action` keeps.

    ``quotes_error`` lets the caller hand in the exception the market-data
    adapter raised, so an adapter refusal is reported as ``UNAVAILABLE`` carrying
    the broker's own reason rather than as a bare missing snapshot.

    The order of the checks is the precedence stated in the module docstring, and
    it is severity order rather than convenience order: a position with neither
    quotes nor commission evidence reports ``UNAVAILABLE``, because the absent
    commission is not what is stopping it from being marked.
    """
    target = profit_target_debit(
        filled_credit=position.filled_credit,
        profit_target_fraction=policy.profit_target_fraction,
    )

    def refuse(state: MarkState, reason: str, detail: str) -> PositionMarkReport:
        return _refusing_report(
            position,
            state=state,
            reason_code=reason,
            detail=detail,
            now=now,
            target=target,
            snapshot=snapshot,
        )

    # -- 1. is there a book at all? ---------------------------------------
    if quotes_error is not None:
        reason = getattr(quotes_error, "reason", None)
        return refuse(
            MarkState.UNAVAILABLE,
            str(reason) if reason else MarkReason.QUOTES_REFUSED.value,
            f"the market-data adapter refused: {type(quotes_error).__name__}: "
            f"{quotes_error}",
        )
    if snapshot is None:
        return refuse(
            MarkState.UNAVAILABLE,
            MarkReason.NO_SNAPSHOT.value,
            "no quote snapshot was obtained for this position's legs",
        )

    quoted = {quote.con_id for quote in snapshot.legs}
    missing = [leg.con_id for leg in position.legs if leg.con_id not in quoted]
    if missing:
        return refuse(
            MarkState.UNAVAILABLE,
            MarkReason.LEG_QUOTE_MISSING.value,
            f"no quote for leg(s) {missing}; a structure cannot be marked from "
            "a subset of its legs",
        )

    # -- 2. may the book be acted on? -------------------------------------
    refusal = _gate_legs(snapshot, now=now, policy=policy)
    if refusal is not None:
        return refuse(MarkState.STALE, refusal.reason, str(refusal.message))

    # -- 3. the arithmetic -------------------------------------------------
    midpoint = closing_midpoint_debit(position.legs, snapshot)
    natural = closing_natural_debit(position.legs, snapshot)
    if midpoint is None or natural is None:
        return refuse(
            MarkState.UNAVAILABLE,
            MarkReason.ONE_SIDED_MARKET.value,
            "a leg is unquoted on the side a close would need: buying back the "
            "short leg needs its ask, selling the long leg needs its bid",
        )
    if midpoint < ZERO or natural < ZERO:
        return refuse(
            MarkState.UNAVAILABLE,
            MarkReason.NEGATIVE_DEBIT.value,
            f"the closing debit is negative (mid {midpoint}, natural {natural}); "
            "a negative buy-back price is a crossed book or a sign error, not a "
            "free position",
        )
    if natural < midpoint:
        # Algebraically impossible on an uncrossed book: natural minus midpoint
        # is half the sum of the two leg spreads, which cannot be negative unless
        # some leg's ask is below its bid. So this is bad data, not a cheap exit.
        return refuse(
            MarkState.UNAVAILABLE,
            MarkReason.CROSSED_BOOK.value,
            f"the natural closing debit {natural} is below the midpoint "
            f"{midpoint}; crossing the market to exit always costs at least the "
            "mid, so a leg is quoted with its ask below its bid",
        )
    quote_as_of = _latest_provider_event(snapshot)
    if quote_as_of is None:
        return refuse(
            MarkState.UNAVAILABLE,
            MarkReason.NO_QUOTE_TIMESTAMP.value,
            "no provider event timestamp on any leg, so the mark's age cannot be "
            "established",
        )
    prices = ClosingPrices(
        midpoint_debit=midpoint, natural_debit=natural, as_of=quote_as_of
    )

    confirmed = confirmed_remaining_quantity(position)
    # Marked at what is actually held. A position that partially closed is worth
    # what remains of it, not what it was opened as.
    marked_quantity = confirmed if confirmed > 0 else position.manageable_quantity
    contracts = Decimal(marked_quantity) * Decimal(position.multiplier)
    gross_at_midpoint = (position.filled_credit - prices.midpoint_debit) * contracts
    gross_at_natural = (position.filled_credit - prices.natural_debit) * contracts

    # The 50% rule, evaluated against the price a close would actually pay.
    # Using the midpoint here would report a target as reached at a price the
    # book will not fill, which is how a "profit target" order rests all day.
    reached = prices.natural_debit <= target

    liveness = "/".join(sorted({q.provenance.liveness.value for q in snapshot.legs}))

    proposal = None
    if propose and position.state is PositionState.OPEN:
        # Priced at the natural: it is the price a close can actually be done
        # at. A proposal at the midpoint is the 101-minute order again.
        proposal = propose_close(
            position,
            limit_price=prices.natural_debit,
            basis="PROFIT_TARGET" if reached else "NATURAL",
            created_at=now,
            configuration_version=configuration_version,
        )

    # -- 4. is the fill cost known? ---------------------------------------
    # ``total_commission is None`` is tested alongside ``is_complete`` rather
    # than asserted afterwards, both because ``CommissionEvidence`` couples the
    # two and because spelling the narrowing out here is what makes it survive
    # ``python -O`` -- the same reasoning lifecycle.py gives for its own
    # redundant ``mark is not None``.
    paid = commission.total_commission if commission is not None else None
    if commission is None or not commission.is_complete or paid is None:
        gaps = commission.gaps if commission is not None else (
            "no execution or commission evidence was captured for this fill",
        )
        if not gaps:  # pragma: no cover - only a hand-built inconsistent evidence
            gaps = ("commission evidence is complete but carries no total",)
        return PositionMarkReport(
            position_id=position.strategy_id,
            underlying=position.underlying,
            state=MarkState.COMMISSION_INCOMPLETE,
            reason_code=MarkReason.COMMISSION_INCOMPLETE.value,
            detail=(
                "the position is marked and gross profit and loss is stated, but "
                "the broker never costed this fill, so net is withheld rather "
                "than computed against an assumed zero commission"
            ),
            evaluated_at=now,
            entry_credit=position.filled_credit,
            multiplier=position.multiplier,
            quantity_marked=marked_quantity,
            confirmed_remaining=confirmed,
            profit_target_debit=target,
            closing_midpoint_debit=prices.midpoint_debit,
            closing_natural_debit=prices.natural_debit,
            gross_at_midpoint=gross_at_midpoint,
            gross_at_natural=gross_at_natural,
            profit_target_reached=reached,
            quote_as_of=quote_as_of,
            liveness=liveness,
            commission_gaps=tuple(gaps),
            close_proposal=proposal,
        )

    # Net of the commission **already paid to open**. The closing commission has
    # not been incurred and is deliberately not estimated here: an estimate would
    # be this module inventing a number, which is the thing every refusal above
    # exists to avoid.
    return PositionMarkReport(
        position_id=position.strategy_id,
        underlying=position.underlying,
        state=MarkState.MARKED,
        reason_code=MarkReason.OK.value,
        detail=(
            f"marked from a live two-sided book on every leg; "
            f"{marked_quantity} contract(s) held"
        ),
        evaluated_at=now,
        entry_credit=position.filled_credit,
        multiplier=position.multiplier,
        quantity_marked=marked_quantity,
        confirmed_remaining=confirmed,
        profit_target_debit=target,
        closing_midpoint_debit=prices.midpoint_debit,
        closing_natural_debit=prices.natural_debit,
        gross_at_midpoint=gross_at_midpoint,
        gross_at_natural=gross_at_natural,
        commission_paid=paid,
        net_at_midpoint=gross_at_midpoint - paid,
        net_at_natural=gross_at_natural - paid,
        profit_target_reached=reached,
        quote_as_of=quote_as_of,
        liveness=liveness,
        commission_gaps=(),
        close_proposal=proposal,
    )


def mark_open_positions(
    positions: Sequence[OpenPosition],
    *,
    market_data: LiveMarketDataPort | None,
    policy: RiskPolicy,
    now: dt.datetime,
    commission_by_position: dict[UUID, CommissionEvidence] | None = None,
    configuration_version: str = "mark",
    propose: bool = True,
) -> tuple[PositionMarkReport, ...]:
    """Mark every position, one bounded subscription per position.

    **Only the position's own underlying and its own legs are subscribed.** Not a
    chain window, not the strikes around it -- the exact contracts held. A
    marking pass has no strike to select, so every extra subscription is quota
    spent for nothing and one more contract whose slow callback delays the answer.

    An adapter exception is caught per position and turned into an
    ``UNAVAILABLE`` report rather than being allowed to end the pass. One
    unquotable position must not stop the other nine from being marked, and a
    marking pass that aborts halfway is indistinguishable from one that found
    nothing.
    """
    evidence = commission_by_position or {}
    reports: list[PositionMarkReport] = []
    for position in positions:
        snapshot: StrategyQuoteSnapshot | None = None
        error: Exception | None = None
        con_ids = [leg.con_id for leg in position.legs]
        if market_data is None or not con_ids:
            error = None
        else:
            try:
                # Two-sided demanded: these are the held structure's own legs,
                # and a one-sided snapshot is the coin flip that made in-pass
                # marking fail 7 of 10 passes on 2026-07-31. Passed by keyword
                # only when needed, so ports predating the parameter still work
                # on their default path.
                snapshot = market_data.strategy_quotes(
                    underlying_symbol=position.underlying,
                    con_ids=con_ids,
                    require_two_sided=True,
                )
            except Exception as exc:  # noqa: BLE001 - adapter boundary, see scan.py
                error = exc
        reports.append(
            mark_position(
                position,
                snapshot,
                policy=policy,
                now=now,
                commission=evidence.get(position.strategy_id),
                configuration_version=configuration_version,
                propose=propose,
                quotes_error=error,
            )
        )
    return tuple(reports)
