"""Combo construction and the broker what-if. Nothing here transmits.

``what_if`` runs today with no market-data subscription: the credit-spread
what-if returned ``initMarginChange`` 500.00 on a 5-wide spread against the
delayed-only paper account — exactly width x multiplier, so IBKR recognises the
defined risk. That makes the real margin number available before any strike can
be selected properly.

Three IBKR mechanics are encoded here, each of which cost a failed probe:

**A credit is ``BUY`` at a negative limit.** The ``ComboLeg`` actions describe
the position you want — SELL the short, BUY the long — and the parent order is a
``BUY`` whose limit price is the negative of the credit. Submitting ``SELL`` at a
positive price inverts the leg actions and is rejected with error 201, "riskless
combination". This is the only place that sign convention exists; the domain
carries a positive magnitude plus a ``PriceEffect``.

**TIF must be set explicitly.** Left blank, TWS fills it from an order preset
and announces it with error 10349, which ``ib_async`` does not classify as a
warning — so the request ends and ``whatIfOrder`` returns ``[]`` instead of an
``OrderState``.

**DBL_MAX means "does not apply".** IBKR sends ``1.797...e308`` for fields that
do not apply, and it appeared as the commission on the very first options
what-if. It is finite, so a NaN/infinity screen passes it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .domain import OptionStrategyIntent, OrderAction, PriceEffect

__all__ = [
    "COMBO_ORDER_TYPE",
    "COMBO_TIME_IN_FORCE",
    "MarginAssessment",
    "IB_UNSET",
    "build_combo",
    "what_if",
]

IB_UNSET = 1.7976931348623157e308

#: What :func:`build_combo` produces, named so the two facts can be *bound*
#: rather than assumed. An approval that did not cover the order type and the
#: time in force would survive the same legs at the same price being sent as a
#: market order, or good-till-cancelled -- two orders with the same structure
#: digest and very different risk. ``place_combo`` compares the order it is
#: about to send against these, so the names are load-bearing and not decoration.
COMBO_ORDER_TYPE = "LMT"
COMBO_TIME_IN_FORCE = "DAY"

_ORDER_STATE_FIELDS = (
    "initMarginChange",
    "maintMarginChange",
    "commission",
    "equityWithLoanChange",
)


@dataclass(frozen=True)
class MarginAssessment:
    """What the broker said about a structure it has not been asked to place."""

    accepted: bool
    observed_at: dt.datetime
    initial_margin_change: Decimal | None = None
    maintenance_margin_change: Decimal | None = None
    equity_with_loan_change: Decimal | None = None
    commission: Decimal | None = None
    warning_text: str | None = None
    rejection_reason: str | None = None

    @property
    def has_required_fields(self) -> bool:
        """Both margin figures must be present. A missing one is a refusal, not
        a zero — an unknown margin impact assumed negligible is how an account
        gets a position it cannot carry."""
        return (
            self.initial_margin_change is not None
            and self.maintenance_margin_change is not None
        )

    def describe(self) -> str:
        if not self.accepted:
            return f"what-if REJECTED: {self.rejection_reason or 'no reason given'}"
        parts = [
            f"initial margin   {self.initial_margin_change}",
            f"maintenance      {self.maintenance_margin_change}",
            f"equity w/ loan   {self.equity_with_loan_change}",
            f"commission       {self.commission}",
        ]
        if self.warning_text:
            parts.append(f"warning          {self.warning_text}")
        return "\n".join(f"  {p}" for p in parts)

    def to_record(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return str(value) if value is not None else None

        return {
            "accepted": self.accepted,
            "initial_margin_change": s(self.initial_margin_change),
            "maintenance_margin_change": s(self.maintenance_margin_change),
            "equity_with_loan_change": s(self.equity_with_loan_change),
            "commission": s(self.commission),
            "warning_text": self.warning_text,
            "rejection_reason": self.rejection_reason,
            "observed_at": self.observed_at.isoformat(),
        }


def _as_decimal(value: Any) -> Decimal | None:
    """A Decimal, or None for absent, NaN, infinity and IBKR's DBL_MAX."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    if number in (float("inf"), float("-inf")):
        return None
    if abs(number) >= IB_UNSET:
        return None
    try:
        return Decimal(str(number))
    except (InvalidOperation, ValueError):  # pragma: no cover
        return None


def build_combo(intent: OptionStrategyIntent) -> tuple[Any, Any]:
    """Build the BAG contract and its parent order. Transmits nothing.

    Returns ``(contract, order)``. The order's ``transmit`` flag is left at
    ib_async's default and the order is never handed to ``placeOrder`` from this
    module — the what-if path is the only consumer.
    """
    from ib_async import Bag, ComboLeg, LimitOrder  # noqa: PLC0415

    legs = [
        ComboLeg(
            conId=leg.con_id,
            ratio=leg.ratio,
            action=leg.action.value,
            exchange=leg.exchange or "SMART",
        )
        for leg in intent.legs
    ]

    bag = Bag(
        symbol=intent.underlying,
        exchange="SMART",
        currency="USD",
        comboLegs=legs,
    )

    # A net credit is BUY at a negative limit. SELL at a positive price would
    # invert every leg action and be rejected as a riskless combination.
    signed = (
        -intent.limit_price
        if intent.price_effect is PriceEffect.CREDIT
        else intent.limit_price
    )
    order = LimitOrder(OrderAction.BUY.value, intent.quantity, float(signed))
    # Without this, TWS fills the TIF from a preset and error 10349 ends the
    # request, so whatIfOrder returns [] rather than an OrderState.
    order.tif = COMBO_TIME_IN_FORCE
    order.orderRef = str(intent.strategy_id)

    return bag, order


def what_if(ib: Any, intent: OptionStrategyIntent, *, observed_at: dt.datetime) -> MarginAssessment:
    """Ask the broker what this structure would cost. Places nothing.

    A response missing the margin fields is reported as ``accepted=False``. IBKR
    returns an empty or partial state for a structure it will not accept, and
    treating that as a zero-margin success is the failure this guards.
    """
    bag, order = build_combo(intent)
    state = ib.whatIfOrder(bag, order)

    if not state:
        return MarginAssessment(
            accepted=False,
            observed_at=observed_at,
            rejection_reason="whatIfOrder returned no order state",
        )

    present = [name for name in _ORDER_STATE_FIELDS if hasattr(state, name)]
    if not present:
        return MarginAssessment(
            accepted=False,
            observed_at=observed_at,
            rejection_reason=(
                f"whatIfOrder returned {type(state).__name__} without any margin fields"
            ),
        )

    assessment = MarginAssessment(
        accepted=True,
        observed_at=observed_at,
        initial_margin_change=_as_decimal(getattr(state, "initMarginChange", None)),
        maintenance_margin_change=_as_decimal(getattr(state, "maintMarginChange", None)),
        equity_with_loan_change=_as_decimal(getattr(state, "equityWithLoanChange", None)),
        commission=_as_decimal(getattr(state, "commission", None)),
        warning_text=(getattr(state, "warningText", "") or None),
    )

    if not assessment.has_required_fields:
        return MarginAssessment(
            accepted=False,
            observed_at=observed_at,
            initial_margin_change=assessment.initial_margin_change,
            maintenance_margin_change=assessment.maintenance_margin_change,
            equity_with_loan_change=assessment.equity_with_loan_change,
            commission=assessment.commission,
            warning_text=assessment.warning_text,
            rejection_reason="whatIfOrder omitted a required margin field",
        )

    return assessment
