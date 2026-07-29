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
from .risk import (
    CandidateRiskAssessment,
    CheckResult,
    RiskRefusalReason,
    assess_candidate,
)
from .transmit import (
    TransmitAuthorization,
    TransmitResult,
    authorize_close,
    authorize_open,
    place_combo,
)

__all__ = [
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
    "TransmitAuthorization",
    "TransmitResult",
    "authorize_open",
    "authorize_close",
    "place_combo",
]
