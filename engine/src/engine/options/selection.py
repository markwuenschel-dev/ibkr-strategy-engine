"""Delta-based strike selection and max-loss position sizing.

The recorded strategy is "16-delta neutral / 30-delta directional strikes",
sized against a fixed risk budget. Both halves live here because they are the
two decisions that turn a qualified chain into a structure with a quantity, and
both are pure functions of their inputs -- no clock, no broker, no I/O.

Four properties this module has to have:

**A missing delta is never a delta of zero.** ``OptionGreeks.delta`` is ``None``
when IBKR sent nothing usable, and ``0`` is a real, meaningful value for a far
out-of-the-money contract. Conflating them would let a contract with no market
data at all win the "nearest to 0.16" comparison from the far end of the chain.
So a candidate with no delta is dropped from the universe before any comparison
happens, and :func:`select_short_strike` returns ``None`` rather than reaching
for the least-bad substitute.

**Deltas are compared as magnitudes.** IBKR reports put deltas negative and call
deltas positive. A 16-delta short put has ``delta == -0.16``, and comparing that
against a target of ``0.16`` without taking the absolute value would rank the
whole put chain backwards -- picking the strike furthest from the intent rather
than nearest to it, silently, with no error anywhere.

**Ties break toward the further out-of-the-money strike.** Two strikes equally
distant from the target -- a 14-delta and an 18-delta against a 16-delta target
-- are not equally risky. The lower-delta one is further out of the money: it
has a lower probability of finishing in the money and a smaller loss at any
given adverse move. When the rule cannot distinguish them, the tie is broken
toward less risk, and it is broken deterministically so two runs against the
same chain select the same contract.

**Refusal is a return value, not an exception.** ``None`` from
:func:`select_short_strike`, :func:`select_vertical` or :func:`build_vertical`
means "no valid structure exists in this chain", which is an ordinary outcome of
a scan and not an error condition. A quantity of ``0`` from
:func:`size_position` is the same statement about the budget: the caller must
treat it as a refusal to trade and must never round it up to one contract.

The one thing that *does* raise is :func:`size_position` on a non-positive
maximum loss per contract, because there is no sensible quantity to return --
the division is either by zero or produces a negative contract count, and both
would be a sizing bug rather than a market condition.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4

from ..errors import InvalidStrategyError
from .chain import QualifiedOption
from .domain import (
    OptionLegIntent,
    OptionRight,
    OptionStrategyIntent,
    OrderAction,
    PriceEffect,
    StrategyAction,
    StrategyType,
    compute_maximum_loss_per_contract,
)
from .marketdata import OptionQuote
from .policy import RiskPolicy
from .ports import StrategyQuoteSnapshot

__all__ = [
    "Bias",
    "DeltaCandidate",
    "StrikeSelection",
    "candidates_from_snapshot",
    "rights_for",
    "strategy_type_for",
    "target_delta_for",
    "select_short_strike",
    "select_vertical",
    "size_position",
    "build_vertical",
]

ZERO = Decimal("0")
ONE = Decimal("1")


class Bias(str, Enum):
    """Which side of the underlying a structure is willing to be wrong about.

    Bias selects both the rights involved and which of the two configured delta
    targets applies, because those two choices are not independent: the neutral
    target is further out of the money precisely because a condor is short both
    wings and can only be breached on one of them.
    """

    NEUTRAL = "NEUTRAL"  # iron condor, both sides
    BULLISH = "BULLISH"  # put credit spread
    BEARISH = "BEARISH"  # call credit spread


def _refuse(message: str, *, hint: str | None = None) -> None:
    raise InvalidStrategyError(message, hint=hint)


def _normalized_right(raw: object) -> OptionRight | None:
    """IBKR's right for a qualified contract, or ``None`` if it is not one.

    ``QualifiedOption.right`` is a plain string read off the broker's contract,
    and IBKR uses both ``"P"`` and ``"PUT"`` depending on the field. Anything
    else is not a right this engine understands, and an unrecognised value makes
    the contract unselectable rather than defaulting to either side.
    """
    if isinstance(raw, OptionRight):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip().upper()
    if text in {"P", "PUT"}:
        return OptionRight.PUT
    if text in {"C", "CALL"}:
        return OptionRight.CALL
    return None


# ---------------------------------------------------------------------------
# The universe a selection is made from
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeltaCandidate:
    """One qualified contract paired with the delta observed for it.

    A separate type from :class:`~engine.options.chain.QualifiedOption` because
    qualification and market data are two different facts with two different
    failure modes: a contract can be perfectly qualified and still have no
    greeks, and the selector must be able to see the difference. ``delta`` is
    ``None`` for "no usable delta was received", never ``0``.
    """

    contract: QualifiedOption
    delta: Decimal | None = None

    @classmethod
    def from_quote(
        cls, contract: QualifiedOption, quote: OptionQuote | None
    ) -> DeltaCandidate:
        """Pair a contract with its quote. A missing quote yields no delta.

        ``OptionQuote.delta`` is already normalized: the sentinels and DBL_MAX
        values IBKR sends have been screened out in
        :mod:`engine.options.marketdata`, so anything that arrives here is a
        real number or ``None``.
        """
        return cls(contract=contract, delta=None if quote is None else quote.delta)

    @property
    def right(self) -> OptionRight | None:
        return _normalized_right(self.contract.right)

    @property
    def is_selectable(self) -> bool:
        """Whether this contract may be compared against a delta target at all."""
        return self.delta is not None and self.right is not None

    @property
    def absolute_delta(self) -> Decimal | None:
        """The magnitude, which is what a target is stated in."""
        return None if self.delta is None else abs(self.delta)

    def describe(self) -> str:
        delta = "none" if self.delta is None else str(self.delta)
        return (
            f"{self.contract.symbol} {self.contract.expiration:%Y-%m-%d} "
            f"{self.contract.strike} {self.contract.right} delta={delta}"
        )


def candidates_from_snapshot(
    contracts: Sequence[QualifiedOption], snapshot: StrategyQuoteSnapshot
) -> tuple[DeltaCandidate, ...]:
    """Join qualified contracts to the quotes in one snapshot, on ``con_id``.

    Takes a whole :class:`~engine.options.ports.StrategyQuoteSnapshot` rather
    than a loose sequence of quotes for the reason the snapshot exists at all:
    every quote in it was taken at one moment, with the subscription generations
    that were active recorded alongside. Assembling the universe from quotes
    gathered separately would reintroduce exactly the cross-moment mixing the
    snapshot type prevents.

    A contract with no matching quote becomes a candidate with no delta -- it
    stays in the sequence so a caller can count what it saw, and is skipped by
    every selector.
    """
    by_con_id = {quote.con_id: quote for quote in snapshot.legs}
    return tuple(
        DeltaCandidate.from_quote(contract, by_con_id.get(contract.con_id))
        for contract in contracts
    )


# ---------------------------------------------------------------------------
# Bias -> rights, strategy type, delta target
# ---------------------------------------------------------------------------


def rights_for(bias: Bias) -> tuple[OptionRight, ...]:
    """The rights a structure with this bias is built from."""
    if bias is Bias.BULLISH:
        return (OptionRight.PUT,)
    if bias is Bias.BEARISH:
        return (OptionRight.CALL,)
    return (OptionRight.PUT, OptionRight.CALL)


def strategy_type_for(right: OptionRight) -> StrategyType:
    """The single-sided credit structure built from this right."""
    return (
        StrategyType.PUT_CREDIT_SPREAD
        if right is OptionRight.PUT
        else StrategyType.CALL_CREDIT_SPREAD
    )


def target_delta_for(bias: Bias, policy: RiskPolicy) -> Decimal:
    """The configured short-strike delta magnitude for this bias.

    Neutral structures use the further-out target because a condor is short both
    wings; a directional spread is short one, and is compensated for the extra
    risk by the larger credit a nearer strike collects.
    """
    return (
        policy.neutral_target_delta
        if bias is Bias.NEUTRAL
        else policy.directional_target_delta
    )


# ---------------------------------------------------------------------------
# Short strike
# ---------------------------------------------------------------------------


def _further_otm_first(right: OptionRight) -> Decimal:
    """``-1`` when a *lower* strike is further out of the money, else ``1``.

    A put is further out of the money as the strike falls, a call as it rises.
    Used only to make the final tie-break deterministic when two contracts
    somehow report the same delta.
    """
    return -ONE if right is OptionRight.PUT else ONE


def select_short_strike(
    candidates: Iterable[DeltaCandidate],
    *,
    target_delta: Decimal,
    right: OptionRight,
) -> DeltaCandidate | None:
    """The candidate of this right whose ``|delta|`` is nearest ``target_delta``.

    ``None`` when the chain contains no usable candidate of that right -- an
    ordinary outcome when greeks have not arrived, and one the caller must be
    able to distinguish from "here is the best of a bad set".

    Three rules do the work, and each of them is a way this can go wrong:

    * **No delta, no selection.** A candidate whose delta is ``None`` is dropped
      before any comparison. It is not treated as delta ``0`` and does not win by
      being the only one left.
    * **Magnitudes, not signed values.** Put deltas are negative; the comparison
      is against ``abs(delta)`` so a single positive target serves both rights.
    * **Contradictory signs are refused.** A put reporting a positive delta, or a
      call a negative one, is not a strike that happens to be unusual -- it is a
      value that disagrees with the convention the rest of this function relies
      on, and selecting on it would mean selecting on a number nobody can
      interpret. Exactly ``0`` is accepted for either right: a far out-of-the-
      money contract really can round to zero delta.

    Ties in distance break toward the **lower** ``|delta|``, which is the further
    out-of-the-money and therefore more conservative of the two: a lower
    probability of finishing in the money, and a smaller loss at any given
    adverse move. A tie in both distance and magnitude -- which a real chain
    should never produce -- breaks toward the further out-of-the-money strike, so
    that the result is a function of the chain and not of its iteration order.
    """
    if not isinstance(target_delta, Decimal) or not target_delta.is_finite():
        _refuse(
            f"target_delta must be a finite Decimal, got {target_delta!r}",
            hint="a float target would drag binary rounding into strike selection",
        )
    if not ZERO < target_delta < ONE:
        _refuse(
            f"target_delta must be between 0 and 1 exclusive, got {target_delta}",
            hint="the target is a delta magnitude -- 0.16 is a 16-delta short",
        )
    if not isinstance(right, OptionRight):
        _refuse(f"right must be an OptionRight, got {right!r}")

    direction = _further_otm_first(right)
    usable: list[tuple[Decimal, Decimal, Decimal, DeltaCandidate]] = []
    for candidate in candidates:
        if candidate.right is not right:
            continue
        delta = candidate.delta
        if delta is None:
            continue
        if right is OptionRight.PUT and delta > ZERO:
            continue
        if right is OptionRight.CALL and delta < ZERO:
            continue
        magnitude = abs(delta)
        usable.append(
            (
                abs(magnitude - target_delta),
                magnitude,
                direction * candidate.contract.strike,
                candidate,
            )
        )

    if not usable:
        return None
    # min() over the key tuple rather than sorted()[0]: the comparison never
    # reaches the candidate itself, which is not orderable.
    return min(usable, key=lambda entry: (entry[0], entry[1], entry[2]))[3]


# ---------------------------------------------------------------------------
# The protective leg, and the pair
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrikeSelection:
    """A short strike and the protective leg that defines its risk.

    Every invariant an :class:`~engine.options.domain.OptionStrategyIntent` will
    later check about strike ordering, matching expirations and matching
    multipliers is checked here too, at construction. That is deliberate
    duplication: a selection that cannot become a valid intent should fail where
    the selection was made, not several layers later where the only available
    diagnosis is "the domain refused it".
    """

    short: QualifiedOption
    long: QualifiedOption
    short_delta: Decimal
    width: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.short, QualifiedOption) or not isinstance(
            self.long, QualifiedOption
        ):
            _refuse("both legs of a selection must be QualifiedOption contracts")
        if self.short.con_id == self.long.con_id:
            _refuse(
                f"the short and protective legs are the same contract "
                f"({self.short.con_id})",
                hint="a duplicated con_id breaks the domain's distinct-contract "
                "invariant much later and much less clearly",
            )
        right = self.right
        if right is None or _normalized_right(self.long.right) is not right:
            _refuse(
                f"both legs must share one right, got {self.short.right!r} and "
                f"{self.long.right!r}"
            )
        if self.short.symbol.strip().upper() != self.long.symbol.strip().upper():
            _refuse(
                f"both legs must be on one underlying, got {self.short.symbol!r} "
                f"and {self.long.symbol!r}"
            )
        if self.short.expiration != self.long.expiration:
            _refuse(
                f"both legs must share one expiration, got {self.short.expiration} "
                f"and {self.long.expiration}",
                hint="calendars and diagonals are not supported structures",
            )
        if self.short.multiplier != self.long.multiplier:
            _refuse(
                f"both legs must share one multiplier, got {self.short.multiplier} "
                f"and {self.long.multiplier}",
                hint="a mixed-multiplier structure makes the max-loss arithmetic wrong",
            )
        if not isinstance(self.short_delta, Decimal) or not self.short_delta.is_finite():
            _refuse(f"short_delta must be a finite Decimal, got {self.short_delta!r}")
        if not isinstance(self.width, Decimal) or not self.width.is_finite():
            _refuse(f"width must be a finite Decimal, got {self.width!r}")

        # The protective leg must sit further out of the money than the short.
        # A reversed wing is an undefined-risk structure wearing a defined-risk
        # name, which is the whole failure this engine exists to prevent.
        if right is OptionRight.PUT and self.long.strike >= self.short.strike:
            _refuse(
                f"the protective put strike {self.long.strike} must be below the "
                f"short put strike {self.short.strike}"
            )
        if right is OptionRight.CALL and self.long.strike <= self.short.strike:
            _refuse(
                f"the protective call strike {self.long.strike} must be above the "
                f"short call strike {self.short.strike}"
            )
        if self.width != abs(self.short.strike - self.long.strike):
            _refuse(
                f"width {self.width} does not match the strikes "
                f"{self.short.strike}/{self.long.strike}"
            )

    @property
    def right(self) -> OptionRight | None:
        return _normalized_right(self.short.right)

    @property
    def strategy_type(self) -> StrategyType:
        right = self.right
        if right is None:  # pragma: no cover - __post_init__ proves it is not
            _refuse("a selection with no recognisable right has no strategy type")
        return strategy_type_for(right)  # type: ignore[arg-type]

    @property
    def multiplier(self) -> int:
        """Uniform across the legs -- ``__post_init__`` guarantees it."""
        return self.short.multiplier

    def describe(self) -> str:
        return (
            f"SELL {self.short.strike} / BUY {self.long.strike} "
            f"{self.short.right} {self.short.expiration:%Y-%m-%d} "
            f"({self.width} wide, short delta {self.short_delta})"
        )


def _select_protective_strike(
    candidates: Iterable[DeltaCandidate],
    *,
    short: QualifiedOption,
    right: OptionRight,
    target_width: Decimal,
    maximum_width: Decimal | None = None,
) -> QualifiedOption | None:
    """The listed strike nearest ``target_width`` further out than the short.

    Chosen by **width, not delta**. The protective leg's job is to bound the
    loss, and the bound is the distance between the strikes -- a delta-selected
    wing would make the maximum loss a function of the volatility surface, which
    is not something a risk budget can be stated against.

    No delta is required of this leg, and that is not an oversight: a chain can
    legitimately quote greeks for the strikes near the money and nothing for the
    wing, and refusing to protect a short because its wing has no delta would
    leave the alternative of not trading -- or worse, trading it naked. The live
    entitlement gate still demands greeks for every leg of a structure before it
    can be traded; this function's contract is narrower than that one's.

    Ties in distance break toward the **narrower** spread, which caps the loss
    lower. On a chain with 1-wide strikes and a 5-wide target there is no tie; on
    a chain listing only 2.5 and 7.5 away from a 5-wide target there is, and the
    2.5-wide is the one that risks less.

    ``maximum_width`` bounds "nearest". Nearest is a comparison among whatever
    was listed, and on a sparse chain the nearest strike below the short can be
    an outlier far beyond anything intended -- observed live on 2026-07-30, where
    a ladder running 672 then 722..750 offered a 722 short a single protective
    strike **50 wide** against a target of 5. Ten times the intended risk is not
    a near miss, and "the only one available" is a reason to build nothing rather
    than a reason to accept it. The defined-loss gate would refuse it downstream;
    this refuses it where the width is chosen, so the refusal names the cause.
    """
    best: tuple[Decimal, Decimal, QualifiedOption] | None = None
    for candidate in candidates:
        contract = candidate.contract
        if candidate.right is not right:
            continue
        if contract.con_id == short.con_id:
            continue
        if contract.expiration != short.expiration:
            continue
        if contract.multiplier != short.multiplier:
            continue
        if contract.symbol.strip().upper() != short.symbol.strip().upper():
            continue
        if right is OptionRight.PUT:
            if contract.strike >= short.strike:
                continue
            width = short.strike - contract.strike
        else:
            if contract.strike <= short.strike:
                continue
            width = contract.strike - short.strike
        if maximum_width is not None and width > maximum_width:
            continue
        key = (abs(width - target_width), width, contract)
        if best is None or (key[0], key[1]) < (best[0], best[1]):
            best = key
    return None if best is None else best[2]


def _strike_increment(
    candidates: Iterable[DeltaCandidate], *, right: OptionRight
) -> Decimal:
    """The chain's typical gap between adjacent listed strikes.

    The **median** gap, not the smallest or the largest. A real ladder is dense
    near the money and sparse in the wings, so the smallest gap understates what
    a wing can legitimately need and the largest is exactly the outlier this
    exists to catch. The median describes the ladder the structure is actually
    being built in.

    Falls back to ``ZERO`` for a chain too small to have a gap, which makes the
    caller's bound simply ``target_width`` -- correct, because a one-strike chain
    offers no choice to bound.
    """
    strikes = sorted(
        {c.contract.strike for c in candidates if c.right is right}
    )
    gaps = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    if not gaps:
        return ZERO
    gaps.sort()
    middle = len(gaps) // 2
    if len(gaps) % 2:
        return gaps[middle]
    return (gaps[middle - 1] + gaps[middle]) / Decimal("2")


def select_vertical(
    candidates: Sequence[DeltaCandidate],
    *,
    target_delta: Decimal,
    right: OptionRight,
    target_width: Decimal,
    maximum_width: Decimal | None = None,
) -> StrikeSelection | None:
    """A short strike by delta, plus the protective leg that bounds it.

    ``None`` when either half is unavailable: no candidate of this right carries
    a usable delta, or the chain lists nothing further out of the money to buy as
    protection. Both are ordinary conditions in a thin chain, and neither is
    something a caller should be able to proceed past by forgetting to check.

    ``maximum_width`` defaults to ``target_width`` plus one strike increment,
    measured from the chain itself. A ratio would be wrong in both directions:
    a coarse chain listing strikes every 5 legitimately cannot do better than 5
    against a 2-wide target, while a chain listing strikes every 1 has no excuse
    for handing back 50 against a 5-wide target. One increment past the target
    is precisely the worst a *complete* ladder can do; anything beyond it means
    the ladder has a hole, and a hole is not a rounding error.
    """
    if not isinstance(target_width, Decimal) or not target_width.is_finite():
        _refuse(
            f"target_width must be a finite Decimal, got {target_width!r}",
            hint="the width is a dollar distance between strikes",
        )
    if target_width <= ZERO:
        _refuse(
            f"target_width must be positive, got {target_width}",
            hint="a width of zero puts the protection on the short strike itself",
        )

    short = select_short_strike(
        candidates, target_delta=target_delta, right=right
    )
    if short is None:
        return None

    if maximum_width is None:
        maximum_width = target_width + _strike_increment(candidates, right=right)
    protective = _select_protective_strike(
        candidates,
        short=short.contract,
        right=right,
        target_width=target_width,
        maximum_width=maximum_width,
    )
    if protective is None:
        return None

    assert short.delta is not None  # select_short_strike drops candidates without one
    return StrikeSelection(
        short=short.contract,
        long=protective,
        short_delta=short.delta,
        width=abs(short.contract.strike - protective.strike),
    )


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------


def size_position(
    *, maximum_loss_per_contract: Decimal, risk_budget: Decimal
) -> int:
    """How many contracts fit inside ``risk_budget``. Floor, never rounded up.

    Replaces the hardcoded ``quantity=1``, which is only correct when the budget
    happens to be at least one contract's worth of loss and is silently wrong in
    both directions otherwise.

    Returns ``0`` when a single contract exceeds the budget. **Zero means do not
    trade**, and the caller must treat it as a refusal -- rounding it up to one
    "so the scan has something to show" is the entire failure this function is
    written to prevent, because the budget is the only thing standing between a
    thin account and a position it cannot carry.

    Raises :class:`ValueError` on a non-positive maximum loss rather than
    returning a sentinel: zero would be a division by zero and a negative would
    produce a negative contract count, and neither is a market condition the
    caller could respond to. A defined-risk structure always has a positive
    worst case, so a non-positive one means the legs or the credit are wrong and
    the sizing question does not yet apply.

    Decimal throughout. ``//`` on Decimals is exact integer division, whereas
    ``/`` is evaluated at the context's 28 significant digits -- enough that a
    true quotient of ``1.999...`` could round to ``2`` and hand back a position
    one contract larger than the budget allows.
    """
    if not isinstance(maximum_loss_per_contract, Decimal):
        raise ValueError(
            f"maximum_loss_per_contract must be a Decimal, got "
            f"{type(maximum_loss_per_contract).__name__}"
        )
    if not isinstance(risk_budget, Decimal):
        raise ValueError(
            f"risk_budget must be a Decimal, got {type(risk_budget).__name__}"
        )
    # NaN fails every comparison, so an unguarded `<= 0` would let it through and
    # the division would then raise InvalidOperation somewhere further out.
    if not maximum_loss_per_contract.is_finite():
        raise ValueError(
            f"maximum_loss_per_contract must be finite, got {maximum_loss_per_contract}"
        )
    if not risk_budget.is_finite():
        raise ValueError(f"risk_budget must be finite, got {risk_budget}")
    if maximum_loss_per_contract <= ZERO:
        raise ValueError(
            f"maximum_loss_per_contract must be positive, got "
            f"{maximum_loss_per_contract}; a defined-risk structure with no "
            "worst case cannot be sized"
        )
    if risk_budget <= ZERO:
        return 0
    # Both operands are positive here, so `//` truncating toward zero is a floor.
    return int(risk_budget // maximum_loss_per_contract)


# ---------------------------------------------------------------------------
# Selection -> intent
# ---------------------------------------------------------------------------


def build_vertical(
    selection: StrikeSelection,
    *,
    credit: Decimal,
    policy: RiskPolicy,
    configuration_version: str,
    created_at: datetime,
    strategy_id: UUID | None = None,
) -> OptionStrategyIntent | None:
    """Turn a selection plus a credit into a validated opening intent.

    Returns ``None`` -- and builds nothing -- when the budget does not cover one
    contract. That is the sizing refusal surfacing as an absent candidate rather
    than as a zero-quantity object, because
    :class:`~engine.options.domain.OptionStrategyIntent` refuses a quantity of
    zero outright and there is no useful thing to hand a caller in that case.

    Everything else that can be wrong -- a credit at or above the width, a
    reversed wing, a mismatched maximum loss -- raises
    :class:`~engine.errors.InvalidStrategyError` from the domain, unchanged. Those
    are structural errors, not market conditions, and a ``None`` return would let
    a caller's ``if candidate is None: continue`` swallow them.

    ``created_at`` and ``configuration_version`` are supplied by the caller
    rather than read here, so the same selection and credit always produce the
    same record.
    """
    if not isinstance(selection, StrikeSelection):
        _refuse(f"selection must be a StrikeSelection, got {type(selection).__name__}")

    right = selection.right
    if right is None:  # pragma: no cover - StrikeSelection refuses this at build
        _refuse("a selection with no recognisable right cannot become an intent")

    legs = (
        _leg(selection.short, right, OrderAction.SELL),  # type: ignore[arg-type]
        _leg(selection.long, right, OrderAction.BUY),  # type: ignore[arg-type]
    )
    strategy_type = strategy_type_for(right)  # type: ignore[arg-type]

    # Computed, never passed in. The domain recomputes and compares it at
    # construction, so a figure derived any other way would only be caught there.
    maximum_loss = compute_maximum_loss_per_contract(
        strategy_type=strategy_type,
        legs=legs,
        credit=credit,
        multiplier=selection.multiplier,
    )

    quantity = size_position(
        maximum_loss_per_contract=maximum_loss,
        risk_budget=policy.risk_budget_per_position,
    )
    if quantity == 0:
        return None

    return OptionStrategyIntent(
        strategy_id=uuid4() if strategy_id is None else strategy_id,
        strategy_type=strategy_type,
        strategy_action=StrategyAction.OPEN,
        underlying=selection.short.symbol,
        quantity=quantity,
        legs=legs,
        expiration=selection.short.expiration,
        limit_price=credit,
        price_effect=PriceEffect.CREDIT,
        maximum_loss_per_contract=maximum_loss,
        configuration_version=configuration_version,
        created_at=created_at,
    )


def _leg(
    contract: QualifiedOption, right: OptionRight, action: OrderAction
) -> OptionLegIntent:
    """One leg, with multiplier and trading class taken from the qualified
    contract rather than assumed. See :class:`OptionLegIntent` on why there is no
    default multiplier."""
    return OptionLegIntent(
        con_id=contract.con_id,
        symbol=contract.symbol,
        expiration=contract.expiration,
        strike=contract.strike,
        right=right,
        action=action,
        ratio=1,
        multiplier=contract.multiplier,
        exchange=contract.exchange,
        trading_class=contract.trading_class,
    )
