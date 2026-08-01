"""The portfolio governor: what the book can carry, not what one trade risks.

:mod:`engine.options.risk` asks whether a candidate is survivable on its own.
This module asks the question that only makes sense across positions -- whether
adding it leaves the account concentrated in one name, one sector, or one thing
that moves together. Six correlated trades on six different tickers pass every
per-position check ever written, which is exactly why those checks are not
enough.

**Runs before final structure selection, not before transmission.** A governor
consulted at the last moment can only veto. Consulted before the structure is
chosen, its refusals are information: "the technology sector is full" is a reason
to look at a different underlying, and that is only useful while there is still a
choice to make.

**Everything fails closed.** Each of the following refuses rather than proceeds:

* no portfolio snapshot at all;
* a snapshot older than the configured maximum age;
* a broker that would not say what the candidate reserves;
* a candidate underlying the operator has not classified into a sector or a
  correlation group;
* **a snapshot containing any position whose underlying is unclassified.**

The last one is the least obvious and the most important. Sector concentration is
computed by summing the positions in a sector; a position whose sector is unknown
is in no bucket, so it is invisible to every concentration cap while still
consuming real buying power. Refusing the whole evaluation is the only honest
response -- the alternative is a governor that reports headroom which does not
exist, and reports it most confidently exactly when the book has drifted furthest
from what the operator classified.

Verdicts, not exceptions -- see the note in :mod:`engine.options.risk`. The same
completeness invariant applies: a :class:`GovernorVerdict` cannot be built
without every check in :data:`REQUIRED_GOVERNOR_CHECKS`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from .domain import OptionStrategyIntent
from .execution import MarginAssessment
from .policy import RiskPolicy
from .portfolio import PortfolioSnapshot
from .risk import CheckResult, required_buying_power

__all__ = [
    "GovernorRefusalReason",
    "GovernorVerdict",
    "PortfolioGovernor",
    "REQUIRED_GOVERNOR_CHECKS",
    "CHECK_PORTFOLIO_STATE",
    "CHECK_INCREMENTAL_BPR",
    "CHECK_TOTAL_BPR",
    "CHECK_UNDERLYING_CONCENTRATION",
    "CHECK_SECTOR_CONCENTRATION",
    "CHECK_CORRELATION_CONCENTRATION",
]

CHECK_PORTFOLIO_STATE = "portfolio_state"
CHECK_INCREMENTAL_BPR = "incremental_bpr"
CHECK_TOTAL_BPR = "total_bpr"
CHECK_UNDERLYING_CONCENTRATION = "underlying_concentration"
CHECK_SECTOR_CONCENTRATION = "sector_concentration"
CHECK_CORRELATION_CONCENTRATION = "correlation_concentration"

REQUIRED_GOVERNOR_CHECKS: tuple[str, ...] = (
    CHECK_PORTFOLIO_STATE,
    CHECK_INCREMENTAL_BPR,
    CHECK_TOTAL_BPR,
    CHECK_UNDERLYING_CONCENTRATION,
    CHECK_SECTOR_CONCENTRATION,
    CHECK_CORRELATION_CONCENTRATION,
)

ZERO = Decimal("0")


class GovernorRefusalReason(str, Enum):
    """Machine-readable causes for a portfolio-level refusal.

    Prefixed ``GOVERNOR_`` so a journal line names the layer that refused without
    anyone having to look up which module owns which code.
    """

    PORTFOLIO_STATE_UNAVAILABLE = "GOVERNOR_PORTFOLIO_STATE_UNAVAILABLE"
    PORTFOLIO_STATE_STALE = "GOVERNOR_PORTFOLIO_STATE_STALE"
    PORTFOLIO_POSITION_UNCLASSIFIED = "GOVERNOR_PORTFOLIO_POSITION_UNCLASSIFIED"
    CANDIDATE_BPR_UNKNOWN = "GOVERNOR_CANDIDATE_BPR_UNKNOWN"
    INCREMENTAL_BPR_EXCEEDED = "GOVERNOR_INCREMENTAL_BPR_EXCEEDED"
    TOTAL_BPR_EXCEEDED = "GOVERNOR_TOTAL_BPR_EXCEEDED"
    UNDERLYING_CONCENTRATION_EXCEEDED = "GOVERNOR_UNDERLYING_CONCENTRATION_EXCEEDED"
    SECTOR_UNCLASSIFIED = "GOVERNOR_SECTOR_UNCLASSIFIED"
    SECTOR_CONCENTRATION_EXCEEDED = "GOVERNOR_SECTOR_CONCENTRATION_EXCEEDED"
    CORRELATION_GROUP_UNCLASSIFIED = "GOVERNOR_CORRELATION_GROUP_UNCLASSIFIED"
    CORRELATION_CONCENTRATION_EXCEEDED = "GOVERNOR_CORRELATION_CONCENTRATION_EXCEEDED"


@dataclass(frozen=True)
class GovernorVerdict:
    """Every portfolio-level verdict for one candidate.

    Carries the snapshot record it was decided against, so a journal line is
    self-contained: the numbers that produced the decision travel with it rather
    than having to be reconstructed from a portfolio that has since moved.
    """

    underlying: str
    evaluated_at: dt.datetime
    policy_version: str
    results: tuple[CheckResult, ...]
    snapshot: PortfolioSnapshot | None = None

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
            raise ValueError(f"a governor check reported twice: {sorted(names)}")
        missing = sorted(set(REQUIRED_GOVERNOR_CHECKS) - set(names))
        if missing:
            raise ValueError(
                f"incomplete governor verdict, missing {missing}; a candidate must "
                "not be approvable by omitting a portfolio check"
            )
        unknown = sorted(set(names) - set(REQUIRED_GOVERNOR_CHECKS))
        if unknown:
            raise ValueError(
                f"unrecognised governor checks {unknown}; add them to "
                "REQUIRED_GOVERNOR_CHECKS so they are mandatory rather than optional"
            )
        if not isinstance(self.underlying, str) or not self.underlying.strip():
            raise ValueError("underlying must be a non-empty string")
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
        lines = [f"PORTFOLIO GOVERNOR   {header}  ({self.underlying})"]
        by_name = {result.check: result for result in self.results}
        lines.extend(by_name[name].describe() for name in REQUIRED_GOVERNOR_CHECKS)
        if self.snapshot is not None:
            lines.append(self.snapshot.describe())
        return "\n".join(lines)

    def to_record(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "evaluated_at": self.evaluated_at.isoformat(),
            "policy_version": self.policy_version,
            "approved": self.approved,
            "reason_codes": list(self.reason_codes),
            "checks": [result.to_record() for result in self.results],
            "portfolio": self.snapshot.to_record() if self.snapshot else None,
        }


def _refusal(check: str, reason: GovernorRefusalReason, detail: str) -> CheckResult:
    return CheckResult(check=check, approved=False, reason=reason, detail=detail)


def _bpr_unknown_detail(margin: MarginAssessment | None) -> str:
    """Why the candidate's reserve is unknown, in the words of what happened.

    The governor's *code* is the same for all three cases -- it cannot size the
    position either way, and that is one decision. The *detail* is not: "no
    what-if was run" and "the broker answered and said no" are different events,
    and a message saying the broker did not report something when it explicitly
    refused would send the operator looking in the wrong place.
    :meth:`engine.options.risk.check_broker_margin` still separates all three at
    the code level, so nothing machine-readable is lost here.
    """
    if margin is None:
        return "no broker what-if was run for this structure"
    if not margin.accepted:
        return (
            "the broker refused the what-if: "
            f"{margin.rejection_reason or 'no reason given'}"
        )
    return "the broker's what-if omitted a margin field, so the reserve is unknown"


class PortfolioGovernor:
    """Evaluates a candidate against the book. Holds no mutable state.

    Constructed with a policy and nothing else; the snapshot is passed per
    evaluation rather than cached, so two evaluations cannot silently share a
    portfolio view that has aged between them.
    """

    def __init__(self, policy: RiskPolicy) -> None:
        self.policy = policy

    # -- the fail-closed preconditions ------------------------------------

    def _check_portfolio_state(
        self, snapshot: PortfolioSnapshot | None, *, decision_time: dt.datetime
    ) -> CheckResult:
        if snapshot is None:
            return _refusal(
                CHECK_PORTFOLIO_STATE,
                GovernorRefusalReason.PORTFOLIO_STATE_UNAVAILABLE,
                "no portfolio snapshot was supplied; portfolio limits cannot be "
                "evaluated against an unknown book",
            )

        age = snapshot.age_at(decision_time)
        if age > self.policy.portfolio_snapshot_maximum_age:
            return _refusal(
                CHECK_PORTFOLIO_STATE,
                GovernorRefusalReason.PORTFOLIO_STATE_STALE,
                f"portfolio snapshot is {age} old, limit is "
                f"{self.policy.portfolio_snapshot_maximum_age}",
            )

        # See the module docstring: an unclassified open position is invisible to
        # every concentration bucket while still consuming buying power.
        unclassified = sorted(
            symbol
            for symbol in snapshot.underlyings
            if self.policy.sector_for(symbol) is None
            or self.policy.correlation_group_for(symbol) is None
        )
        if unclassified:
            return _refusal(
                CHECK_PORTFOLIO_STATE,
                GovernorRefusalReason.PORTFOLIO_POSITION_UNCLASSIFIED,
                f"open positions on {unclassified} have no sector or correlation "
                "group, so concentration cannot be bounded for the whole book",
            )

        return CheckResult(
            check=CHECK_PORTFOLIO_STATE,
            approved=True,
            detail=(
                f"snapshot {age} old, {len(snapshot.positions)} positions, all "
                "classified"
            ),
        )

    # -- buying power ------------------------------------------------------

    def _check_incremental_bpr(
        self,
        candidate_bpr: Decimal | None,
        snapshot: PortfolioSnapshot | None,
        bpr_detail: str,
    ) -> CheckResult:
        if snapshot is None:
            return _refusal(
                CHECK_INCREMENTAL_BPR,
                GovernorRefusalReason.PORTFOLIO_STATE_UNAVAILABLE,
                "no portfolio snapshot, so net liquidation is unknown",
            )
        if candidate_bpr is None:
            return _refusal(
                CHECK_INCREMENTAL_BPR,
                GovernorRefusalReason.CANDIDATE_BPR_UNKNOWN,
                bpr_detail,
            )

        cap = snapshot.net_liquidation * self.policy.max_incremental_bpr_fraction
        if candidate_bpr > cap:
            return CheckResult(
                check=CHECK_INCREMENTAL_BPR,
                approved=False,
                reason=GovernorRefusalReason.INCREMENTAL_BPR_EXCEEDED,
                detail=(
                    f"this position would reserve {candidate_bpr}, over "
                    f"{self.policy.max_incremental_bpr_fraction} of net liquidation "
                    f"{snapshot.net_liquidation} ({cap})"
                ),
                observed=candidate_bpr,
                limit=cap,
            )
        return CheckResult(
            check=CHECK_INCREMENTAL_BPR,
            approved=True,
            detail=f"reserves {candidate_bpr}, within {cap}",
            observed=candidate_bpr,
            limit=cap,
        )

    def _check_total_bpr(
        self,
        candidate_bpr: Decimal | None,
        snapshot: PortfolioSnapshot | None,
        bpr_detail: str,
    ) -> CheckResult:
        if snapshot is None:
            return _refusal(
                CHECK_TOTAL_BPR,
                GovernorRefusalReason.PORTFOLIO_STATE_UNAVAILABLE,
                "no portfolio snapshot, so total reserved buying power is unknown",
            )
        if candidate_bpr is None:
            return _refusal(
                CHECK_TOTAL_BPR,
                GovernorRefusalReason.CANDIDATE_BPR_UNKNOWN,
                bpr_detail,
            )

        resulting = snapshot.total_buying_power_reserved + candidate_bpr
        cap = snapshot.net_liquidation * self.policy.max_total_bpr_fraction
        if resulting > cap:
            return CheckResult(
                check=CHECK_TOTAL_BPR,
                approved=False,
                reason=GovernorRefusalReason.TOTAL_BPR_EXCEEDED,
                detail=(
                    f"total reserved buying power would become {resulting} "
                    f"({snapshot.total_buying_power_reserved} + {candidate_bpr}), "
                    f"over {self.policy.max_total_bpr_fraction} of net liquidation "
                    f"{snapshot.net_liquidation} ({cap})"
                ),
                observed=resulting,
                limit=cap,
            )
        return CheckResult(
            check=CHECK_TOTAL_BPR,
            approved=True,
            detail=f"total reserved would become {resulting}, within {cap}",
            observed=resulting,
            limit=cap,
        )

    # -- concentration -----------------------------------------------------

    def _check_underlying_concentration(
        self,
        underlying: str,
        candidate_bpr: Decimal | None,
        snapshot: PortfolioSnapshot | None,
        bpr_detail: str,
    ) -> CheckResult:
        if snapshot is None:
            return _refusal(
                CHECK_UNDERLYING_CONCENTRATION,
                GovernorRefusalReason.PORTFOLIO_STATE_UNAVAILABLE,
                "no portfolio snapshot, so existing exposure to this underlying is "
                "unknown",
            )
        if candidate_bpr is None:
            return _refusal(
                CHECK_UNDERLYING_CONCENTRATION,
                GovernorRefusalReason.CANDIDATE_BPR_UNKNOWN,
                bpr_detail,
            )

        existing = snapshot.buying_power_for_underlying(underlying)
        resulting = existing + candidate_bpr
        cap = snapshot.net_liquidation * self.policy.max_underlying_bpr_fraction
        if resulting > cap:
            return CheckResult(
                check=CHECK_UNDERLYING_CONCENTRATION,
                approved=False,
                reason=GovernorRefusalReason.UNDERLYING_CONCENTRATION_EXCEEDED,
                detail=(
                    f"{underlying} exposure would become {resulting} "
                    f"({existing} + {candidate_bpr}), over "
                    f"{self.policy.max_underlying_bpr_fraction} of net liquidation "
                    f"{snapshot.net_liquidation} ({cap})"
                ),
                observed=resulting,
                limit=cap,
            )
        return CheckResult(
            check=CHECK_UNDERLYING_CONCENTRATION,
            approved=True,
            detail=f"{underlying} exposure would become {resulting}, within {cap}",
            observed=resulting,
            limit=cap,
        )

    def _check_group_concentration(
        self,
        *,
        check: str,
        underlying: str,
        candidate_bpr: Decimal | None,
        snapshot: PortfolioSnapshot | None,
        classify: Any,
        pairs: tuple[tuple[str, str], ...],
        fraction: Decimal,
        unclassified_reason: GovernorRefusalReason,
        exceeded_reason: GovernorRefusalReason,
        label: str,
        bpr_detail: str,
    ) -> CheckResult:
        """Sector and correlation share one shape: classify, bucket, cap.

        Written once rather than twice because the two differ only in which
        mapping they consult and which code they refuse with. Two copies would
        drift, and the one that drifted would be the one nobody re-read.
        """
        if snapshot is None:
            return _refusal(
                check,
                GovernorRefusalReason.PORTFOLIO_STATE_UNAVAILABLE,
                f"no portfolio snapshot, so {label} exposure is unknown",
            )

        group = classify(underlying)
        if group is None:
            return _refusal(
                check,
                unclassified_reason,
                f"{underlying} has no {label}; an unclassified symbol is one whose "
                f"concentration nobody has bounded",
            )

        if candidate_bpr is None:
            return _refusal(
                check,
                GovernorRefusalReason.CANDIDATE_BPR_UNKNOWN,
                bpr_detail,
            )

        members = frozenset(
            symbol.strip().upper()
            for symbol, classification in pairs
            if classification == group
        )
        existing = snapshot.buying_power_where(members)
        resulting = existing + candidate_bpr
        cap = snapshot.net_liquidation * fraction
        if resulting > cap:
            return CheckResult(
                check=check,
                approved=False,
                reason=exceeded_reason,
                detail=(
                    f"{label} {group} exposure would become {resulting} "
                    f"({existing} + {candidate_bpr}), over {fraction} of net "
                    f"liquidation {snapshot.net_liquidation} ({cap})"
                ),
                observed=resulting,
                limit=cap,
            )
        return CheckResult(
            check=check,
            approved=True,
            detail=f"{label} {group} exposure would become {resulting}, within {cap}",
            observed=resulting,
            limit=cap,
        )

    # -- the whole evaluation ---------------------------------------------

    def evaluate(
        self,
        candidate: OptionStrategyIntent,
        *,
        snapshot: PortfolioSnapshot | None,
        margin: MarginAssessment | None,
        decision_time: dt.datetime,
    ) -> GovernorVerdict:
        """Every portfolio-level check for one candidate. Nothing short-circuits.

        The candidate's buying power comes from
        :func:`engine.options.risk.required_buying_power` -- the same function the
        candidate-level margin check uses, so the two layers cannot disagree about
        what a single position reserves.
        """
        underlying = candidate.underlying.strip().upper()
        candidate_bpr = required_buying_power(margin)
        bpr_detail = _bpr_unknown_detail(margin)

        results = (
            self._check_portfolio_state(snapshot, decision_time=decision_time),
            self._check_incremental_bpr(candidate_bpr, snapshot, bpr_detail),
            self._check_total_bpr(candidate_bpr, snapshot, bpr_detail),
            self._check_underlying_concentration(
                underlying, candidate_bpr, snapshot, bpr_detail
            ),
            self._check_group_concentration(
                check=CHECK_SECTOR_CONCENTRATION,
                underlying=underlying,
                candidate_bpr=candidate_bpr,
                snapshot=snapshot,
                classify=self.policy.sector_for,
                pairs=self.policy.sectors,
                fraction=self.policy.max_sector_bpr_fraction,
                unclassified_reason=GovernorRefusalReason.SECTOR_UNCLASSIFIED,
                exceeded_reason=GovernorRefusalReason.SECTOR_CONCENTRATION_EXCEEDED,
                label="sector",
                bpr_detail=bpr_detail,
            ),
            self._check_group_concentration(
                check=CHECK_CORRELATION_CONCENTRATION,
                underlying=underlying,
                candidate_bpr=candidate_bpr,
                snapshot=snapshot,
                classify=self.policy.correlation_group_for,
                pairs=self.policy.correlation_groups,
                fraction=self.policy.max_correlation_group_bpr_fraction,
                unclassified_reason=(
                    GovernorRefusalReason.CORRELATION_GROUP_UNCLASSIFIED
                ),
                exceeded_reason=(
                    GovernorRefusalReason.CORRELATION_CONCENTRATION_EXCEEDED
                ),
                label="correlation group",
                bpr_detail=bpr_detail,
            ),
        )

        return GovernorVerdict(
            underlying=underlying,
            evaluated_at=decision_time,
            policy_version=self.policy.version,
            results=results,
            snapshot=snapshot,
        )
