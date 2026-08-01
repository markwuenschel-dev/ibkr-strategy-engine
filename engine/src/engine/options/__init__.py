"""Options strategy engine.

A subpackage rather than more flat modules: the equity engine is nine files and
done, while this side grows a chain service, an IV-rank pipeline, a governor, a
state machine and a reconciler. Keeping them under one namespace keeps the
equity execution layer readable as the small, finished thing it is.

Nothing here is imported by :mod:`engine.safety`, :mod:`engine.broker` or
:mod:`engine.cli`. The dependency points one way -- options code may use the
equity engine's journal, config and errors; the equity path never learns that
options exist.
"""

from __future__ import annotations

from .approval import (
    ApprovalContext,
    ApprovalDecision,
    AuthorizedOrderSpec,
    AwaitingVerification,
    CollabVerifierGate,
    VerificationPacket,
    VerificationState,
    VerifierApproval,
    VerifierGate,
    packet_for,
    render_response,
)
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
from .governor import GovernorRefusalReason, GovernorVerdict, PortfolioGovernor
from .policy import RiskPolicy
from .portfolio import PortfolioSnapshot, PositionExposure
from .positions import OpenPosition, PositionState, PositionStore
from .pricing import (
    PriceLadder,
    TickRegime,
    build_ladder,
    midpoint_credit,
    natural_credit,
    quantize_credit,
    tick_regime_for,
)
from .proof import ExecutionProofProfile, PriceEnvelope
from .reprice import RepriceLadder, RepriceOutcome, RepriceStop, tick_size, work_order
from .risk import (
    CandidateRiskAssessment,
    CheckResult,
    RiskRefusalReason,
    assess_candidate,
)
from .transmit import (
    CancelAuthorization,
    TransmitAuthorization,
    TransmitResult,
    authorize_cancel,
    authorize_close,
    authorize_open,
    authorize_reprice,
    cancel_combo,
    place_combo,
)
from .walk import (
    OrderCancellationPort,
    PolicyReverifier,
    PriceWalk,
    Reverification,
    RiskReverifier,
    WalkOutcome,
    WalkPolicy,
    WalkRefusal,
    WalkState,
    reprice,
)

__all__ = [
    "ApprovalContext",
    "ApprovalDecision",
    "AuthorizedOrderSpec",
    "AwaitingVerification",
    "CollabVerifierGate",
    "VerificationPacket",
    "VerificationState",
    "VerifierApproval",
    "VerifierGate",
    "packet_for",
    "render_response",
    "OptionLegIntent",
    "OptionRight",
    "OptionStrategyIntent",
    "OrderAction",
    "PriceEffect",
    "StrategyAction",
    "StrategyType",
    "compute_maximum_loss_per_contract",
    "CandidateRiskAssessment",
    "CheckResult",
    "RiskRefusalReason",
    "assess_candidate",
    "GovernorRefusalReason",
    "GovernorVerdict",
    "PortfolioGovernor",
    "PortfolioSnapshot",
    "PositionExposure",
    "RiskPolicy",
    "OpenPosition",
    "PositionState",
    "PositionStore",
    "ExecutionProofProfile",
    "PriceEnvelope",
    "RepriceLadder",
    "RepriceOutcome",
    "RepriceStop",
    "tick_size",
    "work_order",
    "CancelAuthorization",
    "PriceLadder",
    "TickRegime",
    "build_ladder",
    "midpoint_credit",
    "natural_credit",
    "quantize_credit",
    "tick_regime_for",
    "TransmitAuthorization",
    "TransmitResult",
    "authorize_open",
    "authorize_close",
    "authorize_cancel",
    "authorize_reprice",
    "cancel_combo",
    "place_combo",
    "OrderCancellationPort",
    "PolicyReverifier",
    "PriceWalk",
    "Reverification",
    "RiskReverifier",
    "WalkOutcome",
    "WalkPolicy",
    "WalkRefusal",
    "WalkState",
    "reprice",
]
