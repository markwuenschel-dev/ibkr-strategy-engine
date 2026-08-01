"""The liquidity gate: can this structure be entered AND exited at a fair price?

Liquidity is a **hard gate**, run beside defined-loss and margin in
:func:`engine.options.risk.assess_candidate` -- IV Rank ranks and sizes, but
no volatility tier buys a pass here. The question every sub-check answers is
the same one: the engine's exit rules (50% profit target, 21-DTE) assume the
position can be closed near fair value on demand, and a market that cannot
demonstrate that today will not provide it under stress.

Unmeasured counts as insufficient. An absent open-interest figure is not
evidence of depth, and treating it as neutral would make the gate strictest on
exactly the contracts IBKR bothers to report -- backwards. This bites until
the tick-101 subscription fix is live-verified: expect
``OPTIONS_LIQUIDITY_THIN`` with "unreported" details on the first live run,
and read that as the gate working, not failing.

One :class:`~engine.options.risk.CheckResult` named ``liquidity`` carries the
verdict; when several things are wrong the detail lists all of them and the
refusal code names the first, most structural one (one-sidedness before
width before depth) -- the same convention the entitlement check uses.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

from .domain import OptionStrategyIntent, OrderAction

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    from .policy import RiskPolicy
    from .ports import StrategyQuoteSnapshot

__all__ = [
    "CHECK_LIQUIDITY",
    "LiquidityRefusalReason",
    "check_liquidity",
]

CHECK_LIQUIDITY = "liquidity"

#: Standard US equity option multiplier. Anything else is an adjusted or
#: nonstandard contract (splits, special dividends, unit mergers) whose
#: deliverable is not 100 plain shares -- and whose quotes routinely look
#: cheap for exactly that reason.
STANDARD_MULTIPLIER = 100


class LiquidityRefusalReason(str, Enum):
    ONE_SIDED = "OPTIONS_LIQUIDITY_ONE_SIDED"
    SPREAD_WIDE = "OPTIONS_LIQUIDITY_SPREAD_WIDE"
    THIN = "OPTIONS_LIQUIDITY_THIN"
    SPARSE_CHAIN = "OPTIONS_LIQUIDITY_SPARSE_CHAIN"
    NONSTANDARD = "OPTIONS_CONTRACT_NONSTANDARD"
    UNMEASURABLE = "OPTIONS_LIQUIDITY_UNMEASURABLE"


def check_liquidity(
    intent: OptionStrategyIntent,
    *,
    quotes: "StrategyQuoteSnapshot | None",
    policy: "RiskPolicy",
) -> "Any":
    """Every liquidity failure of this structure, in one named check result.

    Ordering of the refusal code when several fail: NONSTANDARD (the contract
    itself is wrong) > UNMEASURABLE (no snapshot) > ONE_SIDED (no market) >
    SPREAD_WIDE (a market you cannot afford to cross) > THIN (a market that
    may not be there tomorrow) > SPARSE_CHAIN. The detail names all of them.
    """
    problems: list[tuple[LiquidityRefusalReason, str]] = []

    for leg in intent.legs:
        if leg.multiplier != STANDARD_MULTIPLIER:
            problems.append(
                (
                    LiquidityRefusalReason.NONSTANDARD,
                    f"leg {leg.con_id} has multiplier {leg.multiplier}, not "
                    f"{STANDARD_MULTIPLIER}: an adjusted or nonstandard contract",
                )
            )

    if quotes is None:
        problems.append(
            (
                LiquidityRefusalReason.UNMEASURABLE,
                "no quote snapshot, so depth and spread cannot be established",
            )
        )
        return _verdict(problems)

    by_con_id = {leg.con_id: leg for leg in quotes.legs}

    short_mid = Decimal("0")
    long_mid = Decimal("0")
    short_natural = Decimal("0")
    long_natural = Decimal("0")
    structure_priceable = True

    for leg in intent.legs:
        quote = by_con_id.get(leg.con_id)
        if quote is None or quote.bid is None or quote.ask is None:
            state = (
                "unquoted"
                if quote is None
                else f"bid={quote.bid} ask={quote.ask}"
            )
            problems.append(
                (
                    LiquidityRefusalReason.ONE_SIDED,
                    f"leg {leg.con_id} has no two-sided market ({state}); "
                    "a position that cannot be priced cannot be exited",
                )
            )
            structure_priceable = False
            continue

        spread = quote.ask - quote.bid
        fraction = quote.spread_fraction
        if spread > policy.max_leg_spread_dollars:
            problems.append(
                (
                    LiquidityRefusalReason.SPREAD_WIDE,
                    f"leg {leg.con_id} spread {spread} exceeds the "
                    f"{policy.max_leg_spread_dollars} dollar cap",
                )
            )
        if fraction is not None and fraction > policy.max_leg_spread_fraction:
            problems.append(
                (
                    LiquidityRefusalReason.SPREAD_WIDE,
                    f"leg {leg.con_id} spread is {fraction:.3f} of mid, over the "
                    f"{policy.max_leg_spread_fraction} cap",
                )
            )

        open_interest = quote.open_interest
        volume = quote.volume
        if open_interest is None or open_interest < policy.minimum_open_interest:
            problems.append(
                (
                    LiquidityRefusalReason.THIN,
                    f"leg {leg.con_id} open interest "
                    f"{'unreported' if open_interest is None else open_interest} "
                    f"is below the {policy.minimum_open_interest} floor "
                    "(unmeasured counts as insufficient)",
                )
            )
        if volume is None or volume < policy.minimum_volume:
            problems.append(
                (
                    LiquidityRefusalReason.THIN,
                    f"leg {leg.con_id} volume "
                    f"{'unreported' if volume is None else volume} is below the "
                    f"{policy.minimum_volume} floor (unmeasured counts as "
                    "insufficient)",
                )
            )

        mid = (quote.bid + quote.ask) / Decimal("2")
        if leg.action is OrderAction.SELL:
            short_mid += mid * leg.ratio
            short_natural += quote.bid * leg.ratio
        else:
            long_mid += mid * leg.ratio
            long_natural += quote.ask * leg.ratio

    if structure_priceable:
        mid_credit = short_mid - long_mid
        natural_credit = short_natural - long_natural
        crossing = mid_credit - natural_credit
        if mid_credit <= 0:
            problems.append(
                (
                    LiquidityRefusalReason.UNMEASURABLE,
                    f"structure mid credit {mid_credit} is not positive, so the "
                    "crossing-cost fraction is meaningless",
                )
            )
        elif crossing / mid_credit > policy.max_crossing_cost_fraction:
            problems.append(
                (
                    LiquidityRefusalReason.SPREAD_WIDE,
                    f"crossing the book costs {crossing} of a {mid_credit} mid "
                    f"credit ({crossing / mid_credit:.2f}), over the "
                    f"{policy.max_crossing_cost_fraction} cap",
                )
            )

    if len(quotes.legs) < policy.minimum_quoted_strikes:
        problems.append(
            (
                LiquidityRefusalReason.SPARSE_CHAIN,
                f"only {len(quotes.legs)} strikes quoted in the window, below "
                f"the {policy.minimum_quoted_strikes} floor: too sparse a chain "
                "to trust the strikes it did offer",
            )
        )

    return _verdict(problems)


_SEVERITY = (
    LiquidityRefusalReason.NONSTANDARD,
    LiquidityRefusalReason.UNMEASURABLE,
    LiquidityRefusalReason.ONE_SIDED,
    LiquidityRefusalReason.SPREAD_WIDE,
    LiquidityRefusalReason.THIN,
    LiquidityRefusalReason.SPARSE_CHAIN,
)


def _verdict(problems: list[tuple[LiquidityRefusalReason, str]]) -> "Any":
    from .risk import CheckResult  # noqa: PLC0415 - avoids a module cycle

    if not problems:
        return CheckResult(
            check=CHECK_LIQUIDITY,
            approved=True,
            detail="two-sided, tight and deep on every selected leg",
        )
    reasons_present = {reason for reason, _ in problems}
    lead = next(reason for reason in _SEVERITY if reason in reasons_present)
    return CheckResult(
        check=CHECK_LIQUIDITY,
        approved=False,
        reason=lead,
        detail="; ".join(text for _, text in problems),
    )
