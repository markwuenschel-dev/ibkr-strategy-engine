"""When an open position must be managed, and what the closing order should be.

:mod:`engine.options.risk` and :mod:`engine.options.governor` decide whether a
structure may be *opened*. Nothing in either of them ever looks at a position
again. This module is the other half: given a position that already exists, a
mark, and a calendar, it answers one question -- hold, take the profit, or get
out before gamma does the deciding.

**Two rules, and the precedence between them is the whole design.**

``profit target``
    A credit structure's maximum profit is the credit it collected. Taking
    "50% of max profit" therefore means *buying it back for half the credit*:
    a 1.50 credit is closed at a 0.75 debit. The target is
    ``filled_credit * (1 - profit_target_fraction)``, not
    ``filled_credit * profit_target_fraction`` -- the two agree only at exactly
    0.50, which is why the default value is the one place this error hides
    best, and why :mod:`tests.test_options_lifecycle` asserts the arithmetic at
    a fraction where the inverted formula gives a different number.

``management DTE``
    At and inside ``management_dte`` days to expiry the position must not be
    held. Gamma is what changes here, not delta: the same adverse move costs
    several times more at 14 days than at 45, and the defined-risk maximum
    stops being a comfortable bound when it can be reached in a single session.
    Whether that becomes an exit or a roll is policy, not judgement.

**Precedence: the profit target wins.** When both rules fire on the same
position, the decision is ``CLOSE_PROFIT_TARGET``. Both produce a closing order
on the same legs, so the difference is not what gets sent but what gets
recorded and how the exit is priced -- and those must not disagree with why the
exit happened. Taking a target that has been reached is a planned exit at a
computed limit; a DTE exit is a defensive one at whatever the market offers.
Labelling a filled profit target as a defensive exit would corrupt the only
statistic that says whether the profit target is set correctly at all.

**Fail closed, asymmetrically, and the asymmetry is the point.**

* The profit-target rule *cannot fire without a usable mark*. There is no way
  to know a position is worth half its credit without a price for it, and a
  missing feed must never be read as "not yet at target" in the direction of
  taking a profit that was not there.
* The DTE rule *still fires without any market data at all*. It needs a
  calendar and nothing else. A data outage is not a reason to sit in a position
  through expiration week -- it is a reason the exit matters more.

So a position at 15 DTE with no quotes produces ``CLOSE_DTE``, never ``HOLD``.
A position at 40 DTE with no quotes produces ``HOLD`` carrying
``LIFECYCLE_NO_MARK_AVAILABLE``: nothing is actionable, and the record says why
rather than implying the profit target was evaluated and missed.

**A position that is not OPEN never produces an action.** A ``CLOSING``
position already has a working order; issuing a second close against it is how
one four-lot becomes two. Everything else here is downstream of that check.

Pure functions throughout. No I/O, no clock reads -- ``now`` and ``today`` are
parameters, so a decision is reproducible from the record it wrote.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from ..errors import InvalidStrategyError
from .domain import OptionStrategyIntent
from .policy import RiskPolicy
from .positions import OpenPosition, PositionState

__all__ = [
    "ManagementAction",
    "ManagementReason",
    "LifecycleRefusalReason",
    "PositionMark",
    "ManagementDecision",
    "profit_target_debit",
    "decide_management_action",
    "closing_intent_for",
]

ZERO = Decimal("0")
ONE = Decimal("1")


class ManagementAction(str, Enum):
    """What to do with a position that already exists.

    ``ROLL`` is a distinct action rather than a flavour of close because the
    follow-on open is a separate, freshly validated structure -- the domain
    models a roll as close plus open plus a link record, and a caller that
    cannot tell the two apart would send the close and forget the open.
    """

    HOLD = "HOLD"
    CLOSE_PROFIT_TARGET = "CLOSE_PROFIT_TARGET"
    CLOSE_DTE = "CLOSE_DTE"
    ROLL = "ROLL"


class ManagementReason(str, Enum):
    """Why a decision *acted*. Every member implies an action other than HOLD.

    Prefixed ``LIFECYCLE_`` for the same reason the risk and governor
    taxonomies are prefixed: a code in a journal line names the layer that
    produced it without anyone having to look up which module owns it.
    """

    PROFIT_TARGET_REACHED = "LIFECYCLE_PROFIT_TARGET_REACHED"
    MANAGEMENT_DTE_REACHED = "LIFECYCLE_MANAGEMENT_DTE_REACHED"
    MANAGEMENT_DTE_ROLL = "LIFECYCLE_MANAGEMENT_DTE_ROLL"


class LifecycleRefusalReason(str, Enum):
    """Why a decision did **not** act. Every member yields ``HOLD``.

    "Refusal" here means refusal to manage, which is not the same as approval:
    ``PROFIT_TARGET_NOT_REACHED`` is a healthy position, while
    ``NO_MARK_AVAILABLE`` is a blind one. They are separate codes precisely so
    an operator can tell a quiet book from a broken feed, which reading either
    of them as a bare ``HOLD`` would make impossible.
    """

    POSITION_NOT_OPEN = "LIFECYCLE_POSITION_NOT_OPEN"
    NO_MARK_AVAILABLE = "LIFECYCLE_NO_MARK_AVAILABLE"
    MARK_NOT_LIVE = "LIFECYCLE_MARK_NOT_LIVE"
    MARK_STALE = "LIFECYCLE_MARK_STALE"
    PROFIT_TARGET_NOT_REACHED = "LIFECYCLE_PROFIT_TARGET_NOT_REACHED"


@dataclass(frozen=True)
class PositionMark:
    """What it would cost, per share, to buy this structure back right now.

    The same unit as :attr:`OpenPosition.filled_credit` -- a per-share net
    price for the whole structure, positive, before the multiplier. Comparing a
    per-contract debit against a per-share credit would report every position
    as a hundred times away from its target, in the safe direction, forever.

    ``is_live`` is carried rather than inferred. The entitlement rules live in
    :mod:`engine.options.marketdata`; this module's contract is only that a
    mark whose provenance was not established cannot be used to take a profit.
    """

    debit_to_close: Decimal
    as_of: dt.datetime
    is_live: bool

    def __post_init__(self) -> None:
        if not isinstance(self.debit_to_close, Decimal):
            raise ValueError(
                f"debit_to_close must be a Decimal, got "
                f"{type(self.debit_to_close).__name__}"
            )
        if not self.debit_to_close.is_finite():
            raise ValueError(f"debit_to_close must be finite, got {self.debit_to_close}")
        if self.debit_to_close < ZERO:
            raise ValueError(
                f"debit_to_close must not be negative, got {self.debit_to_close}; "
                "a negative buy-back price is a sign error, not a free position"
            )
        if not isinstance(self.as_of, dt.datetime):
            raise ValueError(f"as_of must be a datetime, got {type(self.as_of).__name__}")
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if not isinstance(self.is_live, bool):
            raise ValueError(f"is_live must be a bool, got {type(self.is_live).__name__}")

    def age(self, now: dt.datetime) -> dt.timedelta:
        return now - self.as_of


@dataclass(frozen=True)
class ManagementDecision:
    """One position's management verdict, with the numbers that produced it.

    ``reason_code`` is always set, including on ``HOLD``. A decision with no
    code would be indistinguishable from a decision that was never made, and
    "held because nothing was wrong" and "held because the feed was down" are
    the two states an operator most needs to tell apart.

    ``target_debit`` is the limit **this rule computed**, and only the profit
    -target rule computes one. A DTE exit is priced against the book at the
    time it is sent, not against a number this function invented, so carrying
    one there would look like a limit the rule had endorsed.
    """

    action: ManagementAction
    position_id: UUID
    reason_code: str
    detail: str
    evaluated_at: dt.datetime
    target_debit: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, ManagementAction):
            raise ValueError(f"action must be a ManagementAction, got {self.action!r}")
        if not isinstance(self.position_id, UUID):
            raise ValueError(
                f"position_id must be a UUID, got {type(self.position_id).__name__}"
            )
        if not isinstance(self.reason_code, str) or not self.reason_code.strip():
            raise ValueError("every decision must carry a machine-readable reason_code")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ValueError(f"{self.reason_code}: a decision must explain itself")
        if not isinstance(self.evaluated_at, dt.datetime):
            raise ValueError(
                f"evaluated_at must be a datetime, got "
                f"{type(self.evaluated_at).__name__}"
            )
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware")

        if self.action is ManagementAction.CLOSE_PROFIT_TARGET:
            if self.target_debit is None:
                raise ValueError(
                    "a profit-target close must carry the target debit it fired on"
                )
        elif self.target_debit is not None:
            raise ValueError(
                f"{self.action.value} must not carry a target_debit; only the "
                "profit-target rule computes a limit price"
            )

        if self.target_debit is not None:
            if not isinstance(self.target_debit, Decimal):
                raise ValueError(
                    f"target_debit must be a Decimal, got "
                    f"{type(self.target_debit).__name__}"
                )
            if not self.target_debit.is_finite():
                raise ValueError(f"target_debit must be finite, got {self.target_debit}")
            if self.target_debit <= ZERO:
                raise ValueError(
                    f"target_debit must be positive, got {self.target_debit}"
                )

    @property
    def acts(self) -> bool:
        """True when this decision produces an order."""
        return self.action is not ManagementAction.HOLD

    def describe(self) -> str:
        target = f"  @ {self.target_debit}" if self.target_debit is not None else ""
        return f"  {self.action.value:<20} [{self.reason_code}] {self.detail}{target}"

    def to_record(self) -> dict[str, Any]:
        return {
            "position_id": str(self.position_id),
            "action": self.action.value,
            "reason": self.reason_code,
            "detail": self.detail,
            "target_debit": (
                str(self.target_debit) if self.target_debit is not None else None
            ),
            "evaluated_at": self.evaluated_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def profit_target_debit(
    *, filled_credit: Decimal, profit_target_fraction: Decimal
) -> Decimal:
    """The debit at which ``profit_target_fraction`` of maximum profit is banked.

    Maximum profit on a credit structure is the credit received, so capturing a
    fraction ``f`` of it means the remaining ``1 - f`` is what it still costs to
    buy back::

        credit 1.50, f 0.50 -> 0.75
        credit 1.50, f 0.75 -> 0.375

    The second line is the one that catches an inverted formula; the first does
    not, because at exactly one half the two expressions agree.
    """
    return filled_credit * (ONE - profit_target_fraction)


def _mark_refusal(
    mark: PositionMark | None, *, now: dt.datetime, policy: RiskPolicy
) -> tuple[LifecycleRefusalReason, str] | None:
    """Why this mark may not be used to take a profit, or ``None`` if it may."""
    if mark is None:
        return (
            LifecycleRefusalReason.NO_MARK_AVAILABLE,
            "no mark for this position, so the profit target cannot be evaluated",
        )
    if not mark.is_live:
        return (
            LifecycleRefusalReason.MARK_NOT_LIVE,
            "the mark is not from a live feed; delayed or frozen data must not "
            "decide when a profit is taken",
        )
    age = mark.age(now)
    if age > policy.quote_maximum_age:
        return (
            LifecycleRefusalReason.MARK_STALE,
            f"the mark is {age} old, past the maximum of "
            f"{policy.quote_maximum_age}",
        )
    return None


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def decide_management_action(
    position: OpenPosition,
    *,
    policy: RiskPolicy,
    mark: PositionMark | None,
    now: dt.datetime,
    today: dt.date,
) -> ManagementDecision:
    """Hold, take the profit, exit on time, or roll -- exactly one of them.

    ``now`` and ``today`` are both parameters and both used: ``now`` ages the
    mark, ``today`` counts the days to expiry. Passing a ``today`` that
    disagrees with ``now`` is the caller's business -- a backtest does exactly
    that -- and this function reads no clock of its own either way.

    Order of evaluation, which is also the precedence:

    1. a position that is not ``OPEN`` is never acted on;
    2. the profit target, which needs a usable mark;
    3. the management DTE, which needs only the calendar.
    """
    if position.state is not PositionState.OPEN:
        return ManagementDecision(
            action=ManagementAction.HOLD,
            position_id=position.strategy_id,
            reason_code=LifecycleRefusalReason.POSITION_NOT_OPEN.value,
            detail=(
                f"position is {position.state.value}, not OPEN; only an open "
                "position may be managed, and a second close against a working "
                "one doubles the order"
            ),
            evaluated_at=now,
        )

    target = profit_target_debit(
        filled_credit=position.filled_credit,
        profit_target_fraction=policy.profit_target_fraction,
    )
    refusal = _mark_refusal(mark, now=now, policy=policy)

    # The profit target first, and unconditionally when it fires: it beats the
    # DTE rule even on the last day, because a target that has been reached is
    # a planned exit and a DTE exit is a defensive one.
    # ``mark is not None`` is implied by ``refusal is None`` -- _mark_refusal
    # refuses a missing mark -- and is spelled out rather than asserted so the
    # narrowing survives ``python -O``.
    if refusal is None and mark is not None:
        if mark.debit_to_close <= target:
            return ManagementDecision(
                action=ManagementAction.CLOSE_PROFIT_TARGET,
                position_id=position.strategy_id,
                reason_code=ManagementReason.PROFIT_TARGET_REACHED.value,
                detail=(
                    f"mark {mark.debit_to_close} is at or below the target debit "
                    f"{target} ({policy.profit_target_fraction} of the "
                    f"{position.filled_credit} credit collected)"
                ),
                evaluated_at=now,
                target_debit=target,
            )

    dte = position.dte(today)
    if dte <= policy.management_dte:
        # Reached with or without market data. A feed outage is not a reason to
        # hold a position into expiration week; it is a reason the exit matters.
        blind = "" if refusal is None else f" (no usable mark: {refusal[0].value})"
        if policy.roll_at_management_dte:
            return ManagementDecision(
                action=ManagementAction.ROLL,
                position_id=position.strategy_id,
                reason_code=ManagementReason.MANAGEMENT_DTE_ROLL.value,
                detail=(
                    f"{dte} days to expiry is at or inside the management "
                    f"threshold of {policy.management_dte}; policy rolls rather "
                    f"than exits{blind}"
                ),
                evaluated_at=now,
            )
        return ManagementDecision(
            action=ManagementAction.CLOSE_DTE,
            position_id=position.strategy_id,
            reason_code=ManagementReason.MANAGEMENT_DTE_REACHED.value,
            detail=(
                f"{dte} days to expiry is at or inside the management threshold "
                f"of {policy.management_dte}{blind}"
            ),
            evaluated_at=now,
        )

    if refusal is not None or mark is None:
        reason, detail = (
            refusal
            if refusal is not None
            else (
                LifecycleRefusalReason.NO_MARK_AVAILABLE,
                "no mark for this position, so the profit target cannot be "
                "evaluated",
            )
        )
        return ManagementDecision(
            action=ManagementAction.HOLD,
            position_id=position.strategy_id,
            reason_code=reason.value,
            detail=f"{detail}; {dte} days to expiry, so no calendar rule applies",
            evaluated_at=now,
        )

    return ManagementDecision(
        action=ManagementAction.HOLD,
        position_id=position.strategy_id,
        reason_code=LifecycleRefusalReason.PROFIT_TARGET_NOT_REACHED.value,
        detail=(
            f"mark {mark.debit_to_close} is above the target debit {target}, and "
            f"{dte} days to expiry is outside the management threshold of "
            f"{policy.management_dte}"
        ),
        evaluated_at=now,
    )


# ---------------------------------------------------------------------------
# The order that carries the decision out
# ---------------------------------------------------------------------------


def closing_intent_for(
    decision: ManagementDecision,
    position: OpenPosition,
    *,
    strategy_id: UUID,
    created_at: dt.datetime,
    configuration_version: str,
    limit_price: Decimal | None = None,
    quantity: int,
) -> OptionStrategyIntent:
    """The closing order for an acting decision, built from the position's legs.

    Delegates to :meth:`OptionStrategyIntent.closing_intent`, which inverts the
    persisted legs and keeps their ``con_id`` values, so a close cannot land on
    a contract the position never held. Nothing here hand-builds an inverted
    leg.

    Raises rather than returning a verdict, unlike the rest of this module.
    Every failure below is a programming error -- closing a HOLD, or closing
    position A with position B's decision -- and there is no scan report for
    those to appear in. A refusal object would have to be checked by the same
    caller that already got the decision wrong.

    ``limit_price`` defaults to ``decision.target_debit``, which exists only for
    a profit-target close. A DTE exit or a roll must be priced by the caller
    against the live book; there is deliberately no fallback that would send one
    at a number this module invented.
    """
    if not isinstance(decision, ManagementDecision):
        raise InvalidStrategyError(
            f"decision must be a ManagementDecision, got {type(decision).__name__}"
        )
    if not decision.acts:
        raise InvalidStrategyError(
            f"{decision.action.value} produces no order; "
            f"[{decision.reason_code}] {decision.detail}",
            hint="check ManagementDecision.acts before building a closing order",
        )
    if decision.position_id != position.strategy_id:
        raise InvalidStrategyError(
            f"decision names position {decision.position_id}, not "
            f"{position.strategy_id}",
            hint="a closing order must be built from the position the decision "
            "was made about",
        )
    if position.state is not PositionState.OPEN:
        raise InvalidStrategyError(
            f"cannot close a position in state {position.state.value}"
        )

    # The hard invariant:
    #
    #     close_quantity <= confirmed_filled_quantity <= requested_quantity
    #
    # The right-hand half is the domain's (``closing_intent`` refuses a quantity
    # above the order size). The left-hand half can only be checked here, because
    # only the position knows what actually filled -- and it is the half that
    # matters, since a partial fill is exactly when the two differ. Enforced
    # rather than merely satisfied at the one current call site: a second caller
    # is how the first fix stops holding.
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        raise InvalidStrategyError(
            f"closing quantity must be a positive int, got {quantity!r}"
        )
    if quantity > position.manageable_quantity:
        raise InvalidStrategyError(
            f"cannot close {quantity} contracts of a position holding "
            f"{position.manageable_quantity}",
            hint="a partial fill holds fewer contracts than its intent says; "
            "closing the ordered size sells contracts that were never bought, "
            "which makes a defensive exit an opening naked short",
        )

    price = decision.target_debit if limit_price is None else limit_price
    if price is None:
        raise InvalidStrategyError(
            f"{decision.action.value} carries no target debit, so a limit price "
            "must be supplied",
            hint="a defensive exit is priced against the live book, not against "
            "a number the management rule invented",
        )

    return position.intent.closing_intent(
        strategy_id=strategy_id,
        limit_price=price,
        created_at=created_at,
        configuration_version=configuration_version,
        quantity=quantity,
    )
