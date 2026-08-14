"""Volatility regimes: IV Rank as a sizing tier, not a no-trade wall.

The engine's first strategy gate was ``IV Rank >= 50 or no entry``. That reads
a high-volatility *opportunity tier* as a universal threshold, and on an
ordinary low-vol day it turns the whole engine off (2026-07-31: IVR 13-25 all
session, zero eligible candidates, by design). This module replaces the wall
with a versioned classification:

    HIGH       IVR >= 50            full allocation, premium selling permitted
    MEDIUM     30 <= IVR < 50       half allocation, excellent liquidity and a
                                    positive IV/RV edge required
    LOW        20 <= IVR < 30       quarter allocation, directional credit
                                    only, stronger IV/RV edge, prefer 50-65 DTE
    DEPRESSED  IVR < 20             ordinary short premium refused; only
                                    separately *validated* low-IV families may
                                    route, and none are validated yet
    UNKNOWN    IVR unusable         refuse outright

Two properties carried over from :mod:`engine.options.policy`, because a
classifier is only as trustworthy as its inputs' provenance:

**Missing inputs degrade toward refusal, never toward permission.** IV Rank
alone is not allowed to open a tier that also demands an IV/RV edge: if
realized volatility could not be computed, the edge is unknown, and an unknown
edge fails the requirement. The decision records *which* inputs were missing,
so a reviewer reading the packet sees "MEDIUM refused: iv_rv_ratio missing"
rather than a bare no.

**Every decision explains itself.** :class:`RegimeDecision.reasons` names each
input consulted, its value, and the boundary it crossed. The protocol requires
the packet to state the regime, the multiplier and the exact reasons; this is
where those sentences come from.

The DEPRESSED tier routes to :class:`LowIVFamilyRegistry`, whose validated
tuple ships **empty**. That is deliberate scaffolding-with-a-dead-end: the
routing exists so the day the first debit-spread/calendar family passes the
section-7 validation program it has somewhere to plug in, and until then the
tier refuses with a named code rather than inventing a strategy (spec §1:
"do not invent or activate those strategies before they are validated").

Shadow mode: :func:`regime_mode` reads ``IBKR_OPTIONS_REGIME_MODE`` and
defaults to ``"shadow"``. In shadow, the runner computes and records the
decision beside the standing IVR>=50 behavior without changing what trades;
``"live"`` replaces the old gate. The flip to live is a reviewed config
change gated on walk-forward evidence -- policy, not code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from ..errors import ConfigError

__all__ = [
    "REGIME_POLICY_VERSION",
    "REGIME_MODE_ENV",
    "REGIME_MODE_SHADOW",
    "REGIME_MODE_LIVE",
    "VolatilityRegime",
    "StrategyFamily",
    "VolatilityRegimePolicy",
    "VolatilityAssessment",
    "RegimeDecision",
    "LowIVFamilyRegistry",
    "classify",
    "regime_mode",
]

REGIME_POLICY_VERSION = "volatility-regime/1"

REGIME_MODE_ENV = "IBKR_OPTIONS_REGIME_MODE"
REGIME_MODE_SHADOW = "shadow"
REGIME_MODE_LIVE = "live"

ENV_PREFIX = "IBKR_OPTIONS_REGIME_"


def regime_mode(env: Mapping[str, str] | None = None) -> str:
    """``shadow`` unless the operator has explicitly flipped to ``live``.

    Anything that is not exactly ``live`` is shadow, including typos: a
    misspelled activation must not activate.
    """
    source = os.environ if env is None else env
    # Whitespace is a shell artifact and forgiven; case is not -- the exact
    # lowercase token is the documented activation, and anything else (Live,
    # LIVE, on, true) stays shadow. A safety flip is not a place to be
    # accommodating about what the operator probably meant.
    raw = (source.get(REGIME_MODE_ENV) or "").strip()
    return REGIME_MODE_LIVE if raw == REGIME_MODE_LIVE else REGIME_MODE_SHADOW


class VolatilityRegime(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    DEPRESSED = "DEPRESSED"
    #: Not a tier: the state of not knowing which tier. Always refuses.
    UNKNOWN = "UNKNOWN"


class StrategyFamily(str, Enum):
    """What a tier permits. Only defined-risk families exist here."""

    #: Neutral or directional defined-risk credit structures (the verticals
    #: the engine trades today).
    SHORT_PREMIUM = "SHORT_PREMIUM"
    #: Directional defined-risk credit only (LOW tier).
    DIRECTIONAL_CREDIT = "DIRECTIONAL_CREDIT"
    #: Low-IV families -- debit spreads, calendars, diagonals. Present as a
    #: routing target; nothing is validated, so nothing routes yet.
    LOW_IV_VALIDATED = "LOW_IV_VALIDATED"


def _refuse(message: str, *, hint: str | None = None) -> None:
    raise ConfigError(message, hint=hint)


def _check_decimal(value: Any, label: str) -> None:
    if not isinstance(value, Decimal):
        _refuse(f"{label} must be a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        _refuse(f"{label} must be finite, got {value}")


def _check_multiplier(value: Any, label: str) -> None:
    _check_decimal(value, label)
    if not Decimal("0") < value <= Decimal("1"):
        _refuse(
            f"{label} must be in (0, 1], got {value}",
            hint="an allocation multiplier scales the per-position risk budget; "
            "0 would size every trade to nothing and read as 'no opportunity'",
        )


@dataclass(frozen=True)
class LowIVFamilyRegistry:
    """The DEPRESSED tier's routing table. Ships empty, on purpose.

    ``validated`` names strategy families that have passed the validation
    program (spec §7) for low-IV conditions. Adding an entry is a reviewed
    diff that must cite the walk-forward evidence in its commit message --
    the same way the port allowlist moves.
    """

    validated: tuple[StrategyFamily, ...] = ()

    def permits(self, family: StrategyFamily) -> bool:
        return family in self.validated


@dataclass(frozen=True)
class VolatilityRegimePolicy:
    """Tier boundaries and per-tier requirements. Frozen, validated, versioned."""

    high_minimum_iv_rank: Decimal = Decimal("50")
    medium_minimum_iv_rank: Decimal = Decimal("30")
    low_minimum_iv_rank: Decimal = Decimal("20")

    high_allocation: Decimal = Decimal("1.00")
    medium_allocation: Decimal = Decimal("0.50")
    low_allocation: Decimal = Decimal("0.25")

    #: IV/RV ratio floors. MEDIUM demands a positive edge (implied above
    #: 20-day realized); LOW demands a stronger one. HIGH has no edge
    #: requirement -- IVR >= 50 is itself the evidence of rich premium.
    medium_minimum_iv_rv: Decimal = Decimal("1.00")
    low_minimum_iv_rv: Decimal = Decimal("1.15")

    #: LOW-tier DTE preference (spec: "prefer approximately 50-65 DTE").
    #: A *preference* fed to expiry selection, not a hard gate -- recorded in
    #: the decision so the packet shows which window was targeted.
    low_preferred_minimum_dte: int = 50
    low_preferred_maximum_dte: int = 65

    registry: LowIVFamilyRegistry = field(default_factory=LowIVFamilyRegistry)

    version: str = REGIME_POLICY_VERSION

    def __post_init__(self) -> None:
        for label in (
            "high_minimum_iv_rank",
            "medium_minimum_iv_rank",
            "low_minimum_iv_rank",
        ):
            _check_decimal(getattr(self, label), label)
        if not (
            self.low_minimum_iv_rank
            < self.medium_minimum_iv_rank
            < self.high_minimum_iv_rank
        ):
            _refuse(
                "regime boundaries must be strictly increasing: "
                f"low {self.low_minimum_iv_rank} < medium "
                f"{self.medium_minimum_iv_rank} < high {self.high_minimum_iv_rank}",
                hint="inverted boundaries make a tier unreachable, which reads "
                "as 'the market never offered it' rather than a config bug",
            )
        for label in ("high_allocation", "medium_allocation", "low_allocation"):
            _check_multiplier(getattr(self, label), label)
        for label in ("medium_minimum_iv_rv", "low_minimum_iv_rv"):
            value = getattr(self, label)
            _check_decimal(value, label)
            if value <= 0:
                _refuse(f"{label} must be positive, got {value}")
        if self.low_minimum_iv_rv < self.medium_minimum_iv_rv:
            _refuse(
                f"low_minimum_iv_rv {self.low_minimum_iv_rv} is below "
                f"medium_minimum_iv_rv {self.medium_minimum_iv_rv}",
                hint="the thinner the premium, the stronger the demanded edge; "
                "a LOW tier easier to enter than MEDIUM is backwards",
            )
        for label in ("low_preferred_minimum_dte", "low_preferred_maximum_dte"):
            value = getattr(self, label)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                _refuse(f"{label} must be a positive int, got {value!r}")
        if self.low_preferred_minimum_dte >= self.low_preferred_maximum_dte:
            _refuse(
                f"low DTE preference window is empty: "
                f"[{self.low_preferred_minimum_dte}, {self.low_preferred_maximum_dte}]"
            )
        if not isinstance(self.registry, LowIVFamilyRegistry):
            _refuse(
                f"registry must be a LowIVFamilyRegistry, "
                f"got {type(self.registry).__name__}"
            )
        if not isinstance(self.version, str) or not self.version.strip():
            _refuse("version must be a non-empty string")

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, **overrides: object
    ) -> "VolatilityRegimePolicy":
        source = os.environ if env is None else env
        values: dict[str, object] = {}
        for name, key in (
            ("high_minimum_iv_rank", "HIGH_MINIMUM_IV_RANK"),
            ("medium_minimum_iv_rank", "MEDIUM_MINIMUM_IV_RANK"),
            ("low_minimum_iv_rank", "LOW_MINIMUM_IV_RANK"),
            ("high_allocation", "HIGH_ALLOCATION"),
            ("medium_allocation", "MEDIUM_ALLOCATION"),
            ("low_allocation", "LOW_ALLOCATION"),
            ("medium_minimum_iv_rv", "MEDIUM_MINIMUM_IV_RV"),
            ("low_minimum_iv_rv", "LOW_MINIMUM_IV_RV"),
        ):
            raw = (source.get(f"{ENV_PREFIX}{key}") or "").strip()
            if raw:
                try:
                    values[name] = Decimal(raw)
                except ArithmeticError:
                    _refuse(f"{ENV_PREFIX}{key}={raw!r} is not a decimal number")
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]

    def to_record(self) -> dict[str, str]:
        return {
            "version": self.version,
            "high_minimum_iv_rank": str(self.high_minimum_iv_rank),
            "medium_minimum_iv_rank": str(self.medium_minimum_iv_rank),
            "low_minimum_iv_rank": str(self.low_minimum_iv_rank),
            "high_allocation": str(self.high_allocation),
            "medium_allocation": str(self.medium_allocation),
            "low_allocation": str(self.low_allocation),
            "medium_minimum_iv_rv": str(self.medium_minimum_iv_rv),
            "low_minimum_iv_rv": str(self.low_minimum_iv_rv),
            "low_preferred_dte": (
                f"{self.low_preferred_minimum_dte}-{self.low_preferred_maximum_dte}"
            ),
            "validated_low_iv_families": ",".join(
                family.value for family in self.registry.validated
            ),
        }


@dataclass(frozen=True)
class VolatilityAssessment:
    """Everything the classifier consults, with absence stated, never guessed.

    Only ``iv_rank`` is strictly required to place a tier; every other input
    either tightens a tier's requirements (``iv_rv_ratio``) or rides along as
    evidence the reviewer sees (``term_structure``, ``skew``, ``event_risk``,
    ``expected_crossing_cost``). ``None`` always means "could not be
    established" -- and for inputs a tier *requires*, unknown fails the
    requirement.
    """

    symbol: str
    iv_rank: Decimal | None = None
    iv_percentile: Decimal | None = None
    current_iv: Decimal | None = None
    realized_vol_20: Decimal | None = None
    realized_vol_60: Decimal | None = None
    iv_rv_ratio: Decimal | None = None
    term_structure: str | None = None
    skew: str | None = None
    event_risk: str | None = None
    expected_crossing_cost: Decimal | None = None

    def to_record(self) -> dict[str, str | None]:
        return {
            "symbol": self.symbol,
            "iv_rank": None if self.iv_rank is None else str(self.iv_rank),
            "iv_percentile": (
                None if self.iv_percentile is None else str(self.iv_percentile)
            ),
            "current_iv": None if self.current_iv is None else str(self.current_iv),
            "realized_vol_20": (
                None if self.realized_vol_20 is None else str(self.realized_vol_20)
            ),
            "realized_vol_60": (
                None if self.realized_vol_60 is None else str(self.realized_vol_60)
            ),
            "iv_rv_ratio": (
                None if self.iv_rv_ratio is None else str(self.iv_rv_ratio)
            ),
            "term_structure": self.term_structure,
            "skew": self.skew,
            "event_risk": self.event_risk,
            "expected_crossing_cost": (
                None
                if self.expected_crossing_cost is None
                else str(self.expected_crossing_cost)
            ),
        }


@dataclass(frozen=True)
class RegimeDecision:
    """One classification: the tier, what it licenses, and exactly why."""

    regime: VolatilityRegime
    allocation: Decimal
    permitted_families: tuple[StrategyFamily, ...]
    #: True when this decision permits ordinary entry consideration at all.
    permits_entry: bool
    #: The named refusal code when ``permits_entry`` is False, else "".
    refusal_code: str
    reasons: tuple[str, ...]
    assessment: VolatilityAssessment
    policy_version: str
    #: The DTE window entry selection should target, when the tier states one.
    preferred_dte: tuple[int, int] | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "regime": self.regime.value,
            "allocation": str(self.allocation),
            "permitted_families": [f.value for f in self.permitted_families],
            "permits_entry": self.permits_entry,
            "refusal_code": self.refusal_code,
            "reasons": list(self.reasons),
            "assessment": self.assessment.to_record(),
            "policy_version": self.policy_version,
            "preferred_dte": (
                None
                if self.preferred_dte is None
                else f"{self.preferred_dte[0]}-{self.preferred_dte[1]}"
            ),
        }

    def describe(self) -> str:
        head = (
            f"{self.regime.value} x{self.allocation} "
            f"{'ENTRY PERMITTED' if self.permits_entry else self.refusal_code}"
        )
        return head + "".join(f"\n    {reason}" for reason in self.reasons)


REFUSAL_UNKNOWN = "OPTIONS_REGIME_UNKNOWN"
REFUSAL_DEPRESSED = "OPTIONS_REGIME_DEPRESSED_REFUSED"
REFUSAL_EDGE = "OPTIONS_REGIME_EDGE_REFUSED"


def _refusal(
    regime: VolatilityRegime,
    code: str,
    reasons: list[str],
    assessment: VolatilityAssessment,
    policy: VolatilityRegimePolicy,
) -> RegimeDecision:
    return RegimeDecision(
        regime=regime,
        allocation=Decimal("0"),
        permitted_families=(),
        permits_entry=False,
        refusal_code=code,
        reasons=tuple(reasons),
        assessment=assessment,
        policy_version=policy.version,
    )


def classify(
    assessment: VolatilityAssessment, policy: VolatilityRegimePolicy
) -> RegimeDecision:
    """Place one symbol's volatility state into a tier, with its reasons.

    Pure and deterministic: same assessment + same policy = same decision,
    byte for byte, which is what lets the decision live inside a verification
    packet without making the digest unstable.
    """
    ivr = assessment.iv_rank
    if ivr is None or not ivr.is_finite():
        return _refusal(
            VolatilityRegime.UNKNOWN,
            REFUSAL_UNKNOWN,
            [
                f"iv_rank is {'missing' if ivr is None else str(ivr)} -- a tier "
                "cannot be placed, and an unplaceable tier refuses rather than "
                "assumes"
            ],
            assessment,
            policy,
        )

    reasons: list[str] = []

    if ivr >= policy.high_minimum_iv_rank:
        reasons.append(
            f"iv_rank {ivr} >= {policy.high_minimum_iv_rank} places HIGH: "
            "premium selling at full allocation"
        )
        return RegimeDecision(
            regime=VolatilityRegime.HIGH,
            allocation=policy.high_allocation,
            permitted_families=(StrategyFamily.SHORT_PREMIUM,),
            permits_entry=True,
            refusal_code="",
            reasons=tuple(reasons),
            assessment=assessment,
            policy_version=policy.version,
        )

    if ivr >= policy.medium_minimum_iv_rank:
        reasons.append(
            f"iv_rank {ivr} in [{policy.medium_minimum_iv_rank}, "
            f"{policy.high_minimum_iv_rank}) places MEDIUM"
        )
        edge = assessment.iv_rv_ratio
        if edge is None:
            reasons.append(
                "MEDIUM requires a positive implied-versus-realized edge and "
                "iv_rv_ratio could not be established -- unknown fails the "
                "requirement"
            )
            return _refusal(
                VolatilityRegime.MEDIUM, REFUSAL_EDGE, reasons, assessment, policy
            )
        if edge < policy.medium_minimum_iv_rv:
            reasons.append(
                f"iv_rv_ratio {edge} < required {policy.medium_minimum_iv_rv}: "
                "implied is not rich against 20-day realized"
            )
            return _refusal(
                VolatilityRegime.MEDIUM, REFUSAL_EDGE, reasons, assessment, policy
            )
        reasons.append(
            f"iv_rv_ratio {edge} >= {policy.medium_minimum_iv_rv}: edge present; "
            "half allocation, excellent liquidity required (enforced by the "
            "liquidity gate)"
        )
        return RegimeDecision(
            regime=VolatilityRegime.MEDIUM,
            allocation=policy.medium_allocation,
            permitted_families=(StrategyFamily.SHORT_PREMIUM,),
            permits_entry=True,
            refusal_code="",
            reasons=tuple(reasons),
            assessment=assessment,
            policy_version=policy.version,
        )

    if ivr >= policy.low_minimum_iv_rank:
        reasons.append(
            f"iv_rank {ivr} in [{policy.low_minimum_iv_rank}, "
            f"{policy.medium_minimum_iv_rank}) places LOW: directional "
            "defined-risk credit only, quarter allocation"
        )
        edge = assessment.iv_rv_ratio
        if edge is None:
            reasons.append(
                "LOW requires a stronger implied-versus-realized edge and "
                "iv_rv_ratio could not be established -- unknown fails the "
                "requirement"
            )
            return _refusal(
                VolatilityRegime.LOW, REFUSAL_EDGE, reasons, assessment, policy
            )
        if edge < policy.low_minimum_iv_rv:
            reasons.append(
                f"iv_rv_ratio {edge} < required {policy.low_minimum_iv_rv}: "
                "the thin premium of a LOW tier demands a stronger edge"
            )
            return _refusal(
                VolatilityRegime.LOW, REFUSAL_EDGE, reasons, assessment, policy
            )
        reasons.append(
            f"iv_rv_ratio {edge} >= {policy.low_minimum_iv_rv}: edge present; "
            f"preferred expiry window "
            f"{policy.low_preferred_minimum_dte}-{policy.low_preferred_maximum_dte} DTE"
        )
        return RegimeDecision(
            regime=VolatilityRegime.LOW,
            allocation=policy.low_allocation,
            permitted_families=(StrategyFamily.DIRECTIONAL_CREDIT,),
            permits_entry=True,
            refusal_code="",
            reasons=tuple(reasons),
            assessment=assessment,
            policy_version=policy.version,
            preferred_dte=(
                policy.low_preferred_minimum_dte,
                policy.low_preferred_maximum_dte,
            ),
        )

    reasons.append(
        f"iv_rank {ivr} < {policy.low_minimum_iv_rank} places DEPRESSED: "
        "ordinary short-premium entries are refused"
    )
    if policy.registry.validated:
        reasons.append(
            "validated low-IV families exist "
            f"({', '.join(f.value for f in policy.registry.validated)}) but "
            "routing to them is not implemented by this decision -- it only "
            "refuses short premium"
        )
    else:
        reasons.append(
            "no low-IV strategy family has passed validation; the registry is "
            "empty and nothing routes (spec: do not invent or activate "
            "strategies before they are validated)"
        )
    return _refusal(
        VolatilityRegime.DEPRESSED, REFUSAL_DEPRESSED, reasons, assessment, policy
    )
