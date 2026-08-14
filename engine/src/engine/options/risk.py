"""Candidate-level risk checks: what has to be true before a structure is a trade.

This replaces :meth:`engine.safety.SafetyGate.gate_notional` for the options
path. That gate multiplies a share price by a share count, which for a credit
spread is meaningless in both directions: the notional of a 5-wide SPY put spread
is neither ``5 x 100`` nor ``spot x 100``, and the number it would produce has no
relationship to what the position can lose. The equity gate is left exactly as it
is -- it is correct for equity -- and the options path gets four checks that each
answer a question the equity one cannot express.

**These return verdicts; they do not raise.** Every other gate in this engine
raises, deliberately, so a caller cannot forget to check a boolean. Here the
contract is different on purpose: a scan grades many candidates and has to record
*why* each one was refused, including all of the reasons at once rather than
whichever fired first. The safety property is moved rather than dropped --
:class:`CandidateRiskAssessment` cannot be constructed unless every check in
:data:`REQUIRED_CHECKS` is present, so approval-by-omission is not expressible.
A missing check is a construction error, not a silent pass.

The candidate checks:

``market_data_entitlement``
    Delegates to :func:`engine.options.marketdata.require_uniform_live_provenance`
    -- the gate that, until this module existed, had no production callers at all.
    It is listed first and is required, so an options candidate cannot be approved
    without the entitlement question having been asked. Delayed data refuses here.

``defined_loss``
    The local worst case, from the legs themselves. Needs no broker and no market
    data, so it is the one check that still works when everything else is
    unavailable.

``broker_margin``
    What IBKR says it will actually reserve. Local arithmetic and the broker can
    disagree -- the broker is the one whose opinion moves money.

``stress_loss``
    Terminal payoff under an adverse move in the underlying. See
    :func:`stress_loss` for what this model does and does not capture.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from ..errors import MarketDataRefusedError
from .domain import OptionRight, OptionStrategyIntent
from .execution import MarginAssessment
from .marketdata import RefusalReason, require_uniform_live_provenance
from .policy import RiskPolicy
from .ports import StrategyQuoteSnapshot

__all__ = [
    "RiskRefusalReason",
    "RefusalCode",
    "CheckResult",
    "CandidateRiskAssessment",
    "REQUIRED_CHECKS",
    "CHECK_MARKET_DATA_ENTITLEMENT",
    "CHECK_DEFINED_LOSS",
    "CHECK_BROKER_MARGIN",
    "CHECK_STRESS_LOSS",
    "required_buying_power",
    "terminal_profit_per_share",
    "terminal_loss",
    "stress_loss",
    "check_market_data_entitlement",
    "check_defined_loss",
    "check_broker_margin",
    "check_stress_loss",
    "assess_candidate",
]

CHECK_MARKET_DATA_ENTITLEMENT = "market_data_entitlement"
CHECK_DEFINED_LOSS = "defined_loss"
CHECK_BROKER_MARGIN = "broker_margin"
CHECK_STRESS_LOSS = "stress_loss"

#: Every check an approved candidate must have passed. Enforced at construction
#: of :class:`CandidateRiskAssessment` -- adding a name here without producing it
#: makes every existing assessment fail to build, which is the intended pressure.
REQUIRED_CHECKS: tuple[str, ...] = (
    CHECK_MARKET_DATA_ENTITLEMENT,
    CHECK_DEFINED_LOSS,
    CHECK_BROKER_MARGIN,
    CHECK_STRESS_LOSS,
    # The liquidity gate (engine.options.liquidity). Named here so an
    # assessment that skipped it cannot be built -- the intended pressure.
    "liquidity",
)

ZERO = Decimal("0")


class RiskRefusalReason(str, Enum):
    """Machine-readable causes for a candidate-level refusal.

    Prefixed ``OPTIONS_`` so a code appearing in a journal line or an alert is
    unambiguous about which subsystem produced it, and distinct from
    :class:`engine.options.marketdata.RefusalReason`, whose codes this taxonomy
    passes through unchanged rather than re-labelling.
    """

    NO_MARKET_DATA_SNAPSHOT = "OPTIONS_NO_MARKET_DATA_SNAPSHOT"
    NET_LIQUIDATION_UNAVAILABLE = "OPTIONS_NET_LIQUIDATION_UNAVAILABLE"
    MAX_DEFINED_LOSS_EXCEEDED = "OPTIONS_MAX_DEFINED_LOSS_EXCEEDED"
    DEFINED_LOSS_FRACTION_EXCEEDED = "OPTIONS_DEFINED_LOSS_FRACTION_EXCEEDED"
    BROKER_WHATIF_MISSING = "OPTIONS_BROKER_WHATIF_MISSING"
    BROKER_WHATIF_REJECTED = "OPTIONS_BROKER_WHATIF_REJECTED"
    BROKER_MARGIN_FIELD_MISSING = "OPTIONS_BROKER_MARGIN_FIELD_MISSING"
    BROKER_MARGIN_EXCEEDED = "OPTIONS_BROKER_MARGIN_EXCEEDED"
    BROKER_MARGIN_FRACTION_EXCEEDED = "OPTIONS_BROKER_MARGIN_FRACTION_EXCEEDED"
    STRESS_REFERENCE_PRICE_MISSING = "OPTIONS_STRESS_REFERENCE_PRICE_MISSING"
    STRESS_EXCEEDS_DEFINED_LOSS = "OPTIONS_STRESS_EXCEEDS_DEFINED_LOSS"
    STRESS_LOSS_EXCEEDED = "OPTIONS_STRESS_LOSS_EXCEEDED"
    STRESS_LOSS_FRACTION_EXCEEDED = "OPTIONS_STRESS_LOSS_FRACTION_EXCEEDED"


#: Any declared refusal taxonomy: a ``str``-valued :class:`~enum.Enum` whose
#: members are stable machine codes. Deliberately not a closed union of the
#: taxonomies that exist today -- :class:`engine.options.governor.
#: GovernorRefusalReason` also flows through :class:`CheckResult`, and a closed
#: union here would force ``risk`` to import ``governor``, which imports ``risk``.
#: The invariant that matters is enforced at construction: a refusal reason must
#: be an enum member with a string value, so ``reason_code`` is always a stable
#: identifier and never free prose.
RefusalCode = Enum


@dataclass(frozen=True)
class CheckResult:
    """One check's verdict, with the numbers that produced it.

    ``approved`` and ``reason`` are constrained against each other at
    construction: an approved result carrying a refusal code, or a refusal
    carrying none, cannot exist. That is what makes ``all(r.approved ...)`` a
    trustworthy summary rather than a hopeful one.
    """

    check: str
    approved: bool
    reason: RefusalCode | None = None
    detail: str = ""
    observed: Decimal | None = None
    limit: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.check, str) or not self.check.strip():
            raise ValueError("check must be a non-empty name")
        if not isinstance(self.approved, bool):
            raise ValueError(f"approved must be a bool, got {type(self.approved).__name__}")
        if self.approved:
            if self.reason is not None:
                raise ValueError(
                    f"{self.check}: an approved result must not carry a refusal reason "
                    f"({self.reason!r})"
                )
            return
        if self.reason is None:
            raise ValueError(
                f"{self.check}: a refusal must name a machine-readable reason"
            )
        # A declared taxonomy member, never a bare string. This is what makes
        # `reason_code` something a caller can branch on: a free-text reason
        # would be indistinguishable from a code until someone tried to match it.
        if not isinstance(self.reason, Enum) or not isinstance(self.reason.value, str):
            raise ValueError(
                f"{self.check}: reason must be a member of a declared refusal "
                f"taxonomy with a string value, got {type(self.reason).__name__}"
            )
        if not self.detail.strip():
            raise ValueError(f"{self.check}: a refusal must explain itself")

    @property
    def reason_code(self) -> str | None:
        """The stable string a caller branches on, without parsing prose."""
        return self.reason.value if self.reason is not None else None

    def describe(self) -> str:
        if self.approved:
            measured = f"  ({self.observed} of {self.limit})" if self.limit is not None else ""
            return f"  PASS    {self.check}{measured}"
        return f"  REFUSE  {self.check}  [{self.reason_code}] {self.detail}"

    def to_record(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "approved": self.approved,
            "reason": self.reason_code,
            "detail": self.detail or None,
            "observed": str(self.observed) if self.observed is not None else None,
            "limit": str(self.limit) if self.limit is not None else None,
        }


@dataclass(frozen=True)
class CandidateRiskAssessment:
    """Every candidate-level verdict for one structure.

    Construction refuses an incomplete set. This is the safety property that
    replaces "the gate raises": you cannot hold an assessment that skipped the
    entitlement check and reports ``approved is True``, because such an object
    cannot be built.
    """

    strategy_id: UUID
    evaluated_at: dt.datetime
    policy_version: str
    results: tuple[CheckResult, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.results, tuple):
            raise ValueError(f"results must be a tuple, got {type(self.results).__name__}")
        for result in self.results:
            if not isinstance(result, CheckResult):
                raise ValueError(
                    f"every result must be a CheckResult, got {type(result).__name__}"
                )
        names = [result.check for result in self.results]
        if len(set(names)) != len(names):
            raise ValueError(f"a check reported twice: {sorted(names)}")
        missing = sorted(set(REQUIRED_CHECKS) - set(names))
        if missing:
            raise ValueError(
                f"incomplete risk assessment, missing {missing}; "
                f"a candidate must not be approvable by omitting a check"
            )
        unknown = sorted(set(names) - set(REQUIRED_CHECKS))
        if unknown:
            raise ValueError(
                f"unrecognised checks {unknown}; add them to REQUIRED_CHECKS so "
                "they are mandatory rather than optional"
            )
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise ValueError("policy_version must be a non-empty string")
        # isinstance before .tzinfo: without it a string here raises AttributeError,
        # which is not the ValueError every other invariant in this class raises.
        if not isinstance(self.evaluated_at, dt.datetime):
            raise ValueError(
                f"evaluated_at must be a datetime, got {type(self.evaluated_at).__name__}"
            )
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware")

    @property
    def approved(self) -> bool:
        return all(result.approved for result in self.results)

    @property
    def refusals(self) -> tuple[CheckResult, ...]:
        return tuple(result for result in self.results if not result.approved)

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(
            result.reason_code
            for result in self.refusals
            if result.reason_code is not None
        )

    def result_for(self, check: str) -> CheckResult:
        for result in self.results:
            if result.check == check:
                return result
        raise KeyError(check)  # pragma: no cover - __post_init__ proves presence

    def describe(self) -> str:
        header = "APPROVED" if self.approved else "REFUSED"
        lines = [f"CANDIDATE RISK   {header}"]
        # Reported in the declared order rather than the order they were run, so
        # two reports of the same structure are always diffable.
        by_name = {result.check: result for result in self.results}
        lines.extend(by_name[name].describe() for name in REQUIRED_CHECKS)
        return "\n".join(lines)

    def to_record(self) -> dict[str, Any]:
        return {
            "strategy_id": str(self.strategy_id),
            "evaluated_at": self.evaluated_at.isoformat(),
            "policy_version": self.policy_version,
            "approved": self.approved,
            "reason_codes": list(self.reason_codes),
            "checks": [result.to_record() for result in self.results],
        }


# ---------------------------------------------------------------------------
# Shared arithmetic
# ---------------------------------------------------------------------------


def required_buying_power(assessment: MarginAssessment | None) -> Decimal | None:
    """The buying power a candidate reserves, or ``None`` if the broker did not say.

    Takes the **larger** of the initial and maintenance margin changes. They are
    usually equal for a defined-risk spread -- the live what-if returned 500.00
    for both on a 5-wide -- but where they differ, sizing against the smaller one
    would let a position through that the account cannot carry the moment initial
    margin is what is actually held.

    Shared with :mod:`engine.options.governor` on purpose: the number the
    candidate check caps and the number the portfolio governor accumulates must
    be the same number, or the two can disagree about the same position.
    """
    if assessment is None or not assessment.accepted:
        return None
    initial = assessment.initial_margin_change
    maintenance = assessment.maintenance_margin_change
    if initial is None or maintenance is None:
        return None
    return max(initial, maintenance)


def terminal_profit_per_share(
    intent: OptionStrategyIntent, terminal_price: Decimal
) -> Decimal:
    """Profit per share at expiry if the underlying settles at ``terminal_price``.

    Positive is profit. Intrinsic value only -- at expiry there is no extrinsic
    value left, which is exactly why this is evaluated at expiry rather than as a
    mark-to-market.
    """
    net = intent.limit_price  # the credit collected, per share
    for leg in intent.legs:
        if leg.right is OptionRight.PUT:
            intrinsic = max(ZERO, leg.strike - terminal_price)
        else:
            intrinsic = max(ZERO, terminal_price - leg.strike)
        contribution = intrinsic * leg.ratio
        net += contribution if leg.is_long else -contribution
    return net


def terminal_loss(intent: OptionStrategyIntent, terminal_price: Decimal) -> Decimal:
    """Loss across the whole position at expiry, as a non-negative amount.

    Zero when the structure is profitable at that price -- a profit is not a
    negative loss for the purposes of a risk cap, and returning one would let a
    profitable scenario offset a losing one in an aggregate.
    """
    total = terminal_profit_per_share(intent, terminal_price) * intent.multiplier
    total *= intent.quantity
    return -total if total < ZERO else ZERO


def stress_loss(
    intent: OptionStrategyIntent,
    *,
    underlying_price: Decimal,
    move_fraction: Decimal,
) -> Decimal:
    """Worst terminal loss under an adverse move of ``move_fraction`` either way.

    Both directions are evaluated and the worse is taken. A put credit spread only
    loses on the way down and a call spread only on the way up, but an iron condor
    loses on both sides and testing one direction would understate whichever wing
    happens not to have been chosen.

    **What this model does not capture.** It is a terminal payoff, so it says
    nothing about mark-to-market before expiry. A 15% gap with 40 days left can
    show a larger unrealised loss than this number, because the short option still
    carries extrinsic value. It is still a true bound on what the position can
    *settle* for, it needs no volatility surface, and it is exactly reproducible
    from the legs -- which is what makes it usable as a gate. The pre-expiry
    exposure belongs to a later milestone that has a live greeks feed to build it
    from.
    """
    down = underlying_price * (Decimal("1") - move_fraction)
    up = underlying_price * (Decimal("1") + move_fraction)
    return max(terminal_loss(intent, down), terminal_loss(intent, up))


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def execution_entitlement_set(
    snapshot: StrategyQuoteSnapshot, intent: OptionStrategyIntent | None
) -> tuple[Any, ...]:
    """The quotes that must be live for **this structure** to be authorized.

    A snapshot is two different things wearing one name. As a *selection
    universe* it holds every contract the scan inspected -- dozens of strikes,
    most of which were considered and discarded, and any of which may be
    missing a market-data callback for reasons that say nothing about the two
    legs actually being traded. As an *execution proof* it should hold exactly
    the underlying and the legs of the structure about to be sent.

    Conflating them let an unrelated strike veto a finished candidate: contract
    891847214, never selected and never to be traded, refused a 722/721 spread
    three times on 2026-07-30 because it sat in the same chain window.

    Returning the full leg set when there is no intent is deliberate and is the
    fail-closed direction: with nothing naming the structure, there is no basis
    for narrowing, and checking more than necessary refuses trades that would
    have been fine. Checking *fewer* would authorize trades that are not.
    """
    if intent is None:
        return tuple(snapshot.legs)
    wanted = {leg.con_id for leg in intent.legs}
    return tuple(quote for quote in snapshot.legs if quote.con_id in wanted)


def check_market_data_entitlement(
    snapshot: StrategyQuoteSnapshot | None,
    *,
    decision_time: dt.datetime,
    policy: RiskPolicy,
    intent: OptionStrategyIntent | None = None,
) -> CheckResult:
    """Refuse unless the underlying and **the selected legs** are live and current.

    Fails closed on a missing snapshot. "No market data was supplied" is not a
    reason to skip the check -- it is the strongest possible reason to refuse,
    and treating an absent feed as "nothing to verify" is how a delayed-data
    account ends up selecting strikes.

    ``intent`` narrows the check to the structure being authorized. See
    :func:`execution_entitlement_set`: without it the entire scanned chain has
    to be live, so one unrelated strike with no callback vetoes a candidate it
    has nothing to do with.
    """
    if snapshot is None:
        return CheckResult(
            check=CHECK_MARKET_DATA_ENTITLEMENT,
            approved=False,
            reason=RiskRefusalReason.NO_MARKET_DATA_SNAPSHOT,
            detail=(
                "no live market-data snapshot was supplied, so provenance could "
                "not be established"
            ),
        )

    try:
        require_uniform_live_provenance(
            underlying=snapshot.underlying,
            legs=execution_entitlement_set(snapshot, intent),
            decision_time=decision_time,
            maximum_age=policy.quote_maximum_age,
            active_generations=snapshot.generation_map(),
        )
    except MarketDataRefusedError as exc:
        return CheckResult(
            check=CHECK_MARKET_DATA_ENTITLEMENT,
            approved=False,
            # Passed through unchanged: the caller branching on
            # OPTIONS_REALTIME_DATA_REQUIRED needs to see that exact code, not a
            # re-labelled one that hides which layer refused.
            reason=RefusalReason(exc.reason),
            detail=exc.message,
        )

    return CheckResult(
        check=CHECK_MARKET_DATA_ENTITLEMENT,
        approved=True,
        detail="underlying and every leg are LIVE, current and same-generation",
    )


def check_defined_loss(
    intent: OptionStrategyIntent,
    *,
    policy: RiskPolicy,
    net_liquidation: Decimal | None,
) -> CheckResult:
    """Cap the structure's own worst case, absolutely and as a share of equity."""
    total = intent.total_maximum_loss

    if total > policy.max_defined_loss_per_position:
        return CheckResult(
            check=CHECK_DEFINED_LOSS,
            approved=False,
            reason=RiskRefusalReason.MAX_DEFINED_LOSS_EXCEEDED,
            detail=(
                f"maximum defined loss {total} exceeds the per-position cap of "
                f"{policy.max_defined_loss_per_position}"
            ),
            observed=total,
            limit=policy.max_defined_loss_per_position,
        )

    if net_liquidation is None:
        return CheckResult(
            check=CHECK_DEFINED_LOSS,
            approved=False,
            reason=RiskRefusalReason.NET_LIQUIDATION_UNAVAILABLE,
            detail=(
                "net liquidation is unavailable, so the loss cannot be sized "
                "against account equity"
            ),
            observed=total,
        )

    cap = net_liquidation * policy.max_defined_loss_fraction
    if total > cap:
        return CheckResult(
            check=CHECK_DEFINED_LOSS,
            approved=False,
            reason=RiskRefusalReason.DEFINED_LOSS_FRACTION_EXCEEDED,
            detail=(
                f"maximum defined loss {total} exceeds "
                f"{policy.max_defined_loss_fraction} of net liquidation "
                f"{net_liquidation} ({cap})"
            ),
            observed=total,
            limit=cap,
        )

    return CheckResult(
        check=CHECK_DEFINED_LOSS,
        approved=True,
        detail=f"maximum defined loss {total} within {cap}",
        observed=total,
        limit=cap,
    )


def check_broker_margin(
    assessment: MarginAssessment | None,
    *,
    policy: RiskPolicy,
    net_liquidation: Decimal | None,
) -> CheckResult:
    """Cap what the broker will actually reserve.

    Every failure mode of the what-if is a distinct refusal code, because the
    operator's response differs: a rejected structure is a strategy problem, a
    missing field is an adapter problem, and an exceeded cap is working as
    intended.
    """
    if assessment is None:
        return CheckResult(
            check=CHECK_BROKER_MARGIN,
            approved=False,
            reason=RiskRefusalReason.BROKER_WHATIF_MISSING,
            detail="no broker what-if was run for this structure",
        )

    if not assessment.accepted:
        return CheckResult(
            check=CHECK_BROKER_MARGIN,
            approved=False,
            reason=RiskRefusalReason.BROKER_WHATIF_REJECTED,
            detail=(
                f"broker refused the what-if: "
                f"{assessment.rejection_reason or 'no reason given'}"
            ),
        )

    reserved = required_buying_power(assessment)
    if reserved is None:
        return CheckResult(
            check=CHECK_BROKER_MARGIN,
            approved=False,
            reason=RiskRefusalReason.BROKER_MARGIN_FIELD_MISSING,
            detail=(
                "the what-if omitted a margin field; an unknown margin impact is "
                "not a small one"
            ),
        )

    if reserved > policy.max_broker_margin_per_position:
        return CheckResult(
            check=CHECK_BROKER_MARGIN,
            approved=False,
            reason=RiskRefusalReason.BROKER_MARGIN_EXCEEDED,
            detail=(
                f"broker margin {reserved} exceeds the per-position cap of "
                f"{policy.max_broker_margin_per_position}"
            ),
            observed=reserved,
            limit=policy.max_broker_margin_per_position,
        )

    if net_liquidation is None:
        return CheckResult(
            check=CHECK_BROKER_MARGIN,
            approved=False,
            reason=RiskRefusalReason.NET_LIQUIDATION_UNAVAILABLE,
            detail=(
                "net liquidation is unavailable, so broker margin cannot be sized "
                "against account equity"
            ),
            observed=reserved,
        )

    cap = net_liquidation * policy.max_broker_margin_fraction
    if reserved > cap:
        return CheckResult(
            check=CHECK_BROKER_MARGIN,
            approved=False,
            reason=RiskRefusalReason.BROKER_MARGIN_FRACTION_EXCEEDED,
            detail=(
                f"broker margin {reserved} exceeds "
                f"{policy.max_broker_margin_fraction} of net liquidation "
                f"{net_liquidation} ({cap})"
            ),
            observed=reserved,
            limit=cap,
        )

    return CheckResult(
        check=CHECK_BROKER_MARGIN,
        approved=True,
        detail=f"broker reserves {reserved}, within {cap}",
        observed=reserved,
        limit=cap,
    )


def check_stress_loss(
    intent: OptionStrategyIntent,
    *,
    policy: RiskPolicy,
    underlying_price: Decimal | None,
    net_liquidation: Decimal | None,
) -> CheckResult:
    """Cap the loss under an adverse move, per position and against equity."""
    if underlying_price is None or underlying_price <= ZERO:
        return CheckResult(
            check=CHECK_STRESS_LOSS,
            approved=False,
            reason=RiskRefusalReason.STRESS_REFERENCE_PRICE_MISSING,
            detail=(
                f"no usable underlying reference price ({underlying_price!r}), so "
                "the stress scenario cannot be priced"
            ),
        )

    loss = stress_loss(
        intent,
        underlying_price=underlying_price,
        move_fraction=policy.stress_move_fraction,
    )

    # A defined-risk structure cannot lose more than its stated maximum. If this
    # fires, the stored maximum_loss_per_contract and the legs disagree in a way
    # the domain's own recomputation did not catch -- refuse rather than trust
    # either number.
    if loss > intent.total_maximum_loss:
        return CheckResult(
            check=CHECK_STRESS_LOSS,
            approved=False,
            reason=RiskRefusalReason.STRESS_EXCEEDS_DEFINED_LOSS,
            detail=(
                f"stress loss {loss} exceeds the structure's stated maximum "
                f"{intent.total_maximum_loss}; the legs and the stored maximum "
                "disagree"
            ),
            observed=loss,
            limit=intent.total_maximum_loss,
        )

    if loss > policy.max_stress_loss_per_position:
        return CheckResult(
            check=CHECK_STRESS_LOSS,
            approved=False,
            reason=RiskRefusalReason.STRESS_LOSS_EXCEEDED,
            detail=(
                f"stress loss {loss} at a {policy.stress_move_fraction} move "
                f"exceeds the per-position cap of "
                f"{policy.max_stress_loss_per_position}"
            ),
            observed=loss,
            limit=policy.max_stress_loss_per_position,
        )

    if net_liquidation is None:
        return CheckResult(
            check=CHECK_STRESS_LOSS,
            approved=False,
            reason=RiskRefusalReason.NET_LIQUIDATION_UNAVAILABLE,
            detail=(
                "net liquidation is unavailable, so the stress loss cannot be "
                "sized against account equity"
            ),
            observed=loss,
        )

    cap = net_liquidation * policy.max_stress_loss_fraction
    if loss > cap:
        return CheckResult(
            check=CHECK_STRESS_LOSS,
            approved=False,
            reason=RiskRefusalReason.STRESS_LOSS_FRACTION_EXCEEDED,
            detail=(
                f"stress loss {loss} exceeds {policy.max_stress_loss_fraction} of "
                f"net liquidation {net_liquidation} ({cap})"
            ),
            observed=loss,
            limit=cap,
        )

    return CheckResult(
        check=CHECK_STRESS_LOSS,
        approved=True,
        detail=(
            f"stress loss {loss} at a {policy.stress_move_fraction} move, "
            f"within {cap}"
        ),
        observed=loss,
        limit=cap,
    )


def assess_candidate(
    intent: OptionStrategyIntent,
    *,
    policy: RiskPolicy,
    quotes: StrategyQuoteSnapshot | None,
    margin: MarginAssessment | None,
    underlying_price: Decimal | None,
    net_liquidation: Decimal | None,
    evaluated_at: dt.datetime,
    quoted_window: int | None = None,
) -> CandidateRiskAssessment:
    """Run every candidate-level check and collect the verdicts.

    Nothing short-circuits. All four run even once one has refused, so a single
    report names every problem rather than sending the operator round the loop
    once per cause.

    ``underlying_price`` is passed explicitly rather than read out of ``quotes``.
    The two questions are genuinely separate -- "what price do we stress against"
    and "was that price allowed to inform a decision" -- and approval requires
    both, so a delayed price can be used to produce an informative stress number
    while still being unable to make the candidate tradeable.
    """
    from .liquidity import check_liquidity  # noqa: PLC0415 - avoids a module cycle

    results = (
        check_market_data_entitlement(
            quotes, decision_time=evaluated_at, policy=policy, intent=intent
        ),
        check_defined_loss(intent, policy=policy, net_liquidation=net_liquidation),
        check_broker_margin(margin, policy=policy, net_liquidation=net_liquidation),
        check_stress_loss(
            intent,
            policy=policy,
            underlying_price=underlying_price,
            net_liquidation=net_liquidation,
        ),
        check_liquidity(
            intent, quotes=quotes, policy=policy, quoted_window=quoted_window
        ),
    )
    return CandidateRiskAssessment(
        strategy_id=intent.strategy_id,
        evaluated_at=evaluated_at,
        policy_version=policy.version,
        results=results,
    )
