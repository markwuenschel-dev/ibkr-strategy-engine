"""Exact identity matching for an execution-outbox attempt.

IBKR exposes several identifiers, each with a different lifetime.  ``orderRef``
is the strategy's correlation id, ``permId`` survives reconnects, execution ids
identify fills, and ``orderId`` is only a client-session handle.  A recovery
routine must keep those facts separate and must include the account and the
qualified combo legs before it calls an order ours.

This module is adapter-light on purpose.  It accepts ``ib_async`` objects,
mapping-shaped test records, or simple objects with the usual IBKR attributes.
It never treats an unavailable broker query as an empty account.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable, Mapping

from .domain import OptionStrategyIntent

__all__ = [
    "BrokerMatchClassification",
    "BrokerOrderIdentity",
    "BrokerOrderObservation",
    "BrokerReconciler",
]


class BrokerMatchClassification(str, Enum):
    WORKING = "WORKING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    ABSENT_CONFIRMED = "ABSENT_CONFIRMED"
    UNKNOWN = "UNKNOWN"


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _nested(item: Any, *names: str) -> Any:
    current = item
    for name in names:
        current = _value(current, name)
        if current is None:
            return None
    return current


def _int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return Decimal("0")
    return parsed if parsed.is_finite() and parsed >= 0 else Decimal("0")


def _execution_ids(item: Any) -> tuple[str, ...]:
    values: list[str] = []
    fills = _value(item, "fills", ()) or ()
    for fill in fills:
        execution = _value(fill, "execution", fill)
        execution_id = _value(execution, "execId")
        if execution_id:
            values.append(str(execution_id))
    direct = _value(item, "execution_ids", ()) or ()
    if isinstance(direct, str):
        direct = (direct,)
    values.extend(str(value) for value in direct if value)
    return tuple(sorted(set(values)))


def _leg_key(leg: Any) -> tuple[Any, ...]:
    """Normalize a broker combo leg or a persisted domain leg."""

    action = _value(leg, "action", "")
    action = getattr(action, "value", action)
    return (
        _int(_value(leg, "conId", _value(leg, "con_id"))),
        str(action).strip().upper(),
        int(_value(leg, "ratio", 0) or 0),
        str(_value(leg, "exchange", "")).strip().upper(),
    )


def _legs(item: Any) -> tuple[tuple[Any, ...], ...]:
    contract = _value(item, "contract", item)
    raw = _value(contract, "comboLegs", _value(contract, "combo_legs", ())) or ()
    return tuple(sorted(_leg_key(leg) for leg in raw))


@dataclass(frozen=True)
class BrokerOrderIdentity:
    """The immutable identity expected for one combo submission."""

    strategy_order_ref: str
    account: str
    legs: tuple[tuple[Any, ...], ...]
    perm_id: int | None = None
    execution_ids: tuple[str, ...] = ()

    @classmethod
    def from_intent(
        cls,
        intent: OptionStrategyIntent,
        *,
        account: str,
        perm_id: int | None = None,
        execution_ids: Iterable[str] = (),
    ) -> "BrokerOrderIdentity":
        return cls(
            strategy_order_ref=str(intent.strategy_id),
            account=str(account).strip(),
            legs=tuple(sorted(_leg_key(leg) for leg in intent.legs)),
            perm_id=perm_id,
            execution_ids=tuple(sorted(set(str(item) for item in execution_ids))),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "strategy_order_ref": self.strategy_order_ref,
            "account": self.account,
            "legs": [list(leg) for leg in self.legs],
            "perm_id": self.perm_id,
            "execution_ids": list(self.execution_ids),
        }


@dataclass(frozen=True)
class BrokerOrderObservation:
    classification: BrokerMatchClassification
    matched: bool
    reason: str
    order_ref: str | None = None
    order_id: int | None = None
    perm_id: int | None = None
    execution_ids: tuple[str, ...] = ()
    filled: Decimal = Decimal("0")
    remaining: Decimal | None = None
    observed_at: dt.datetime | None = None

    @property
    def ambiguous(self) -> bool:
        return self.classification is BrokerMatchClassification.UNKNOWN

    def to_record(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "matched": self.matched,
            "reason": self.reason,
            "order_ref": self.order_ref,
            "order_id": self.order_id,
            "perm_id": self.perm_id,
            "execution_ids": list(self.execution_ids),
            "filled": str(self.filled),
            "remaining": str(self.remaining) if self.remaining is not None else None,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }


class BrokerReconciler:
    """Match one expected combo against a complete broker observation."""

    def __init__(self, *, now: dt.datetime | None = None) -> None:
        self.now = now

    @staticmethod
    def _exact_match(expected: BrokerOrderIdentity, candidate: Any) -> bool:
        order = _value(candidate, "order", candidate)
        order_ref = _value(order, "orderRef", _value(order, "order_ref"))
        account = _value(order, "account", _value(candidate, "account", ""))
        candidate_legs = _legs(candidate)
        if order_ref and str(order_ref) != expected.strategy_order_ref:
            return False
        if account and str(account).strip() != expected.account:
            return False
        if expected.legs and candidate_legs and candidate_legs != expected.legs:
            return False

        candidate_perm = _int(
            _value(order, "permId", _value(candidate, "permId", _value(candidate, "perm_id")))
        )
        if expected.perm_id is not None and candidate_perm != expected.perm_id:
            return False
        candidate_execs = set(_execution_ids(candidate))
        if expected.execution_ids and not set(expected.execution_ids).intersection(candidate_execs):
            return False
        # At least one strong identity must be present.  An object with no
        # orderRef, permId, executions, or legs is not evidence of a match.
        return bool(
            (order_ref and str(order_ref) == expected.strategy_order_ref)
            or (expected.perm_id is not None and candidate_perm == expected.perm_id)
            or (expected.execution_ids and set(expected.execution_ids) & candidate_execs)
        )

    @staticmethod
    def _state(candidate: Any, *, expected_quantity: int) -> BrokerMatchClassification:
        status = str(
            _value(
                _value(candidate, "orderStatus", candidate),
                "status",
                _value(candidate, "status", ""),
            )
            or ""
        ).strip().lower()
        status_obj = _value(candidate, "orderStatus", candidate)
        filled = _decimal(_value(status_obj, "filled", _value(candidate, "filled", 0)))
        remaining_raw = _value(status_obj, "remaining", _value(candidate, "remaining"))
        remaining = _decimal(remaining_raw) if remaining_raw is not None else None
        if filled > 0 and remaining is not None and remaining > 0:
            return BrokerMatchClassification.PARTIAL
        if filled > 0 and expected_quantity > 0 and filled < Decimal(expected_quantity):
            # A broker callback that says ``Filled`` but omits remaining
            # quantity is not proof of a complete fill when the requested
            # quantity was larger.  Preserve the partial position and let a
            # later reconciliation establish whether the remainder exists.
            return BrokerMatchClassification.PARTIAL
        if filled >= Decimal(expected_quantity) and expected_quantity > 0:
            return BrokerMatchClassification.FILLED
        if status in {"submitted", "presubmitted", "pendingsubmit", "pendingcancel"}:
            return BrokerMatchClassification.WORKING
        if status in {"filled"} and filled > 0 and (remaining is None or remaining <= 0):
            return BrokerMatchClassification.FILLED
        if status in {"cancelled", "apicancelled", "inactive", "rejected"}:
            return BrokerMatchClassification.CANCELLED
        return BrokerMatchClassification.UNKNOWN

    def reconcile(
        self,
        expected: BrokerOrderIdentity,
        broker_orders: Iterable[Any] | None,
        *,
        executions: Iterable[Any] | None = None,
        complete: bool = True,
        expected_quantity: int = 1,
    ) -> BrokerOrderObservation:
        """Classify the exact order, never equating an unanswered query to absent."""

        if broker_orders is None or not complete:
            return BrokerOrderObservation(
                BrokerMatchClassification.UNKNOWN,
                False,
                "broker order observation was unavailable or incomplete",
                observed_at=self.now,
            )

        entries = list(broker_orders)
        matched = next((entry for entry in entries if self._exact_match(expected, entry)), None)
        if matched is None:
            # A complete executions response can prove that a fill exists even
            # after the order leaves the working-order endpoint.
            execution_ids = set(expected.execution_ids)
            for execution in executions or ():
                execution_id = _value(execution, "execId", _value(execution, "exec_id"))
                if execution_id and str(execution_id) in execution_ids:
                    return BrokerOrderObservation(
                        BrokerMatchClassification.FILLED,
                        True,
                        "execution id matched after the order left the working set",
                        execution_ids=(str(execution_id),),
                        observed_at=self.now,
                    )
            return BrokerOrderObservation(
                BrokerMatchClassification.ABSENT_CONFIRMED,
                False,
                "complete broker order and execution observations contain no exact combo identity",
                observed_at=self.now,
            )

        order = _value(matched, "order", matched)
        status_obj = _value(matched, "orderStatus", matched)
        filled = _decimal(_value(status_obj, "filled", _value(matched, "filled", 0)))
        remaining_raw = _value(status_obj, "remaining", _value(matched, "remaining"))
        remaining = _decimal(remaining_raw) if remaining_raw is not None else None
        return BrokerOrderObservation(
            self._state(matched, expected_quantity=expected_quantity),
            True,
            "exact orderRef/permId/execution/account/leg identity matched",
            order_ref=(
                str(_value(order, "orderRef", _value(order, "order_ref")))
                if _value(order, "orderRef", _value(order, "order_ref"))
                else None
            ),
            order_id=_int(_value(order, "orderId", _value(order, "order_id"))),
            perm_id=_int(_value(order, "permId", _value(order, "perm_id"))),
            execution_ids=_execution_ids(matched),
            filled=filled,
            remaining=remaining,
            observed_at=self.now,
        )
