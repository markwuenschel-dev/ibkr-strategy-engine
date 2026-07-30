"""What actually filled, and what it cost in commission.

The engine could open a position and never learn what the fill cost. On the
first real paper fill IBKR delivered a ``commissionReport`` this engine did not
read, the store recorded ``commission=None``, and nothing ever went back for it.
The consequence is precise: **net profit and loss is unstateable**. Gross is
arithmetic on a credit and a mark; net needs a number the engine never captured.

Two properties of ``ib_async`` make the obvious implementation quietly wrong, and
both are the same shape as the market-data defects :mod:`engine.options.marketdata`
exists to screen.

**1. An absent commission report is not an absent commission -- it looks like a
free trade.** ``Fill.commissionReport`` is a ``CommissionReport`` dataclass whose
``commission`` field defaults to ``0.0``, not to ``None``. A fill whose report has
not arrived therefore presents a perfectly finite, perfectly plausible commission
of zero. Summing those produces a total that is *smaller* than the truth, in the
flattering direction, with no signal that anything is missing. So a report counts
as evidence only when its ``execId`` is populated **and matches the execution it
claims to describe** -- exactly the reasoning that makes an absent market-data-type
callback ``UNKNOWN`` rather than live.

**2. DBL_MAX gets through.** IBKR sends ``1.797...e308`` for "does not apply".
It is finite, so a NaN screen passes it, and a commission of 1.8e308 would swamp
every other number in the report. Screened here, as everywhere else.

**Completeness is a claim about coverage, not about presence.** One execution
carrying a commission proves nothing about the other leg. Evidence is complete
only when every leg of the structure is covered by executions whose share counts
reach the quantity the position believes filled, and every one of those
executions carries a commission. Anything short of that yields
``is_complete=False`` and a ``total_commission`` of ``None`` -- never a partial
sum wearing a total's name.

Reads only. Nothing here sends anything; ``ib.fills()`` and ``reqExecutions`` are
queries.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from .domain import OptionLegIntent
from .marketdata import IB_UNSET

__all__ = [
    "ExecutionRecord",
    "CommissionEvidence",
    "commission_evidence_for",
    "executions_from_fills",
]

ZERO = Decimal("0")


def _decimal_or_none(value: Any) -> Decimal | None:
    """A finite Decimal, or ``None``. DBL_MAX is not a number here."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or abs(number) >= IB_UNSET:
            return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not parsed.is_finite():
        return None
    if abs(parsed) >= Decimal(str(IB_UNSET)):
        return None
    return parsed


def _int_or_none(value: Any) -> int | None:
    """A usable broker identifier. Zero is IBKR's "unassigned", so it is not one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value or None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _aware(value: Any) -> dt.datetime | None:
    """A timezone-aware execution timestamp, or ``None``.

    A naive timestamp is discarded rather than assumed to be UTC, for the same
    reason :func:`engine.options.adapters._aware` discards one: the alternative
    is shifting a fill's apparent time by hours in whichever direction happened
    to be convenient.
    """
    if not isinstance(value, dt.datetime):
        return None
    if value.tzinfo is None:
        return None
    return value


@dataclass(frozen=True)
class ExecutionRecord:
    """One leg execution, with its commission if the broker actually reported one.

    ``commission`` is ``None`` -- never ``0`` -- when no report has arrived. The
    distinction is the entire point of this type: zero is a number an execution
    can legitimately cost, and conflating it with "we were not told" is what let
    an uncosted fill be recorded as a free one.
    """

    exec_id: str
    con_id: int
    side: str
    shares: Decimal
    price: Decimal
    executed_at: dt.datetime | None = None
    order_id: int | None = None
    perm_id: int | None = None
    order_ref: str | None = None
    commission: Decimal | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.exec_id, str) or not self.exec_id.strip():
            raise ValueError("an execution must carry the broker's execId")
        if not isinstance(self.con_id, int) or isinstance(self.con_id, bool):
            raise ValueError(f"con_id must be an int, got {type(self.con_id).__name__}")
        if self.con_id <= 0:
            raise ValueError(
                f"con_id must be positive, got {self.con_id}; zero is what an "
                "unqualified contract carries"
            )
        for label in ("shares", "price"):
            value = getattr(self, label)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{label} must be a finite Decimal, got {value!r}")
        if self.shares <= ZERO:
            raise ValueError(f"shares must be positive, got {self.shares}")
        if self.commission is not None:
            if not isinstance(self.commission, Decimal) or not self.commission.is_finite():
                raise ValueError(
                    f"commission must be a finite Decimal or None, got {self.commission!r}"
                )

    @property
    def has_commission(self) -> bool:
        return self.commission is not None

    def to_record(self) -> dict[str, Any]:
        return {
            "exec_id": self.exec_id,
            "con_id": self.con_id,
            "side": self.side,
            "shares": str(self.shares),
            "price": str(self.price),
            "executed_at": (
                self.executed_at.isoformat() if self.executed_at is not None else None
            ),
            "order_id": self.order_id,
            "perm_id": self.perm_id,
            "order_ref": self.order_ref,
            "commission": (
                str(self.commission) if self.commission is not None else None
            ),
            "currency": self.currency,
        }

    @classmethod
    def from_fill(cls, fill: Any) -> "ExecutionRecord | None":
        """Read one ``ib_async`` ``Fill``, or anything shaped like one.

        Returns ``None`` rather than raising for a fill this engine cannot make
        sense of -- an unqualified contract, a missing execId, a non-positive
        share count. A malformed fill is a fill we have no evidence about, and
        the completeness check downstream will notice the coverage gap. Raising
        here would make one unreadable row hide every readable one.

        **The commission is taken only from a report that names this execution.**
        ``fill.commissionReport`` is always present and always has a numeric
        ``commission``; only a matching, non-empty ``execId`` proves the broker
        filled it in.
        """
        execution = getattr(fill, "execution", None)
        contract = getattr(fill, "contract", None)
        report = getattr(fill, "commissionReport", None)
        if execution is None or contract is None:
            return None

        exec_id = _text(getattr(execution, "execId", None))
        con_id = _int_or_none(getattr(contract, "conId", None))
        shares = _decimal_or_none(getattr(execution, "shares", None))
        price = _decimal_or_none(getattr(execution, "price", None))
        if exec_id is None or con_id is None or shares is None or price is None:
            return None
        if con_id <= 0 or shares <= ZERO:
            return None

        commission: Decimal | None = None
        currency: str | None = None
        if report is not None:
            report_exec_id = _text(getattr(report, "execId", None))
            # The whole screen, in one condition: a report that does not name
            # this execution is an unpopulated default, and its 0.0 commission
            # is not evidence of anything.
            if report_exec_id is not None and report_exec_id == exec_id:
                commission = _decimal_or_none(getattr(report, "commission", None))
                currency = _text(getattr(report, "currency", None))

        return cls(
            exec_id=exec_id,
            con_id=con_id,
            side=_text(getattr(execution, "side", None)) or "",
            shares=shares,
            price=price,
            executed_at=_aware(
                getattr(execution, "time", None) or getattr(fill, "time", None)
            ),
            order_id=_int_or_none(getattr(execution, "orderId", None)),
            perm_id=_int_or_none(getattr(execution, "permId", None)),
            order_ref=_text(getattr(execution, "orderRef", None)),
            commission=commission,
            currency=currency,
        )

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "ExecutionRecord":
        return cls(
            exec_id=str(record["exec_id"]),
            con_id=int(record["con_id"]),
            side=str(record.get("side") or ""),
            shares=Decimal(str(record["shares"])),
            price=Decimal(str(record["price"])),
            executed_at=(
                dt.datetime.fromisoformat(record["executed_at"])
                if record.get("executed_at")
                else None
            ),
            order_id=_int_or_none(record.get("order_id")),
            perm_id=_int_or_none(record.get("perm_id")),
            order_ref=_text(record.get("order_ref")),
            commission=(
                Decimal(str(record["commission"]))
                if record.get("commission") is not None
                else None
            ),
            currency=_text(record.get("currency")),
        )


def executions_from_fills(fills: Any) -> tuple[ExecutionRecord, ...]:
    """Every readable execution out of a broker fill enumeration.

    Deduplicated on ``execId``: ``ib.fills()`` accumulates over the session and
    a reconnect can deliver the same execution twice. Counting one fill's
    commission twice would overstate the cost, which is the one direction a
    commission error is not conservative in -- it would understate net profit
    and could hold a position past a target it had genuinely reached.
    """
    seen: dict[str, ExecutionRecord] = {}
    for fill in fills or ():
        record = ExecutionRecord.from_fill(fill)
        if record is None:
            continue
        existing = seen.get(record.exec_id)
        # A later delivery of the same execId that finally carries a commission
        # supersedes an earlier one that did not. The reverse never happens:
        # evidence is not un-learned.
        if existing is None or (record.has_commission and not existing.has_commission):
            seen[record.exec_id] = record
    return tuple(sorted(seen.values(), key=lambda r: (r.con_id, r.exec_id)))


@dataclass(frozen=True)
class CommissionEvidence:
    """Whether the cost of a fill is known well enough to state a net number.

    ``total_commission`` is ``None`` whenever ``is_complete`` is false, and that
    coupling is enforced in ``__post_init__`` rather than left to callers. A
    partial sum is the most dangerous possible value here: it is a real number,
    it is in the right units, it is too small, and nothing about it looks wrong.
    """

    strategy_id: UUID
    executions: tuple[ExecutionRecord, ...]
    expected_con_ids: tuple[int, ...]
    is_complete: bool
    total_commission: Decimal | None = None
    gaps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.is_complete:
            if self.total_commission is None:
                raise ValueError(
                    "complete commission evidence must carry the total it proved"
                )
            if self.gaps:
                raise ValueError(
                    f"evidence cannot be complete and still have gaps: {list(self.gaps)}"
                )
        elif self.total_commission is not None:
            raise ValueError(
                "incomplete commission evidence must not carry a total; a partial "
                "sum understates the cost and nothing about it looks wrong"
            )

    @property
    def observed_commission(self) -> Decimal:
        """What has been reported so far, complete or not. **Not a total.**

        Reported to the operator so an incomplete answer still says how much of
        the cost is known. Deliberately a separate name from
        ``total_commission`` so it cannot be mistaken for one in a caller that
        forgot to check ``is_complete``.
        """
        return sum(
            (e.commission for e in self.executions if e.commission is not None), ZERO
        )

    def describe(self) -> str:
        if self.is_complete:
            return (
                f"commission {self.total_commission} across "
                f"{len(self.executions)} execution(s)"
            )
        return (
            f"commission INCOMPLETE ({self.observed_commission} observed across "
            f"{len(self.executions)} execution(s)): " + "; ".join(self.gaps)
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "strategy_id": str(self.strategy_id),
            "is_complete": self.is_complete,
            "total_commission": (
                str(self.total_commission) if self.total_commission is not None else None
            ),
            "observed_commission": str(self.observed_commission),
            "expected_con_ids": list(self.expected_con_ids),
            "gaps": list(self.gaps),
            "executions": [e.to_record() for e in self.executions],
        }


def _belongs_to(
    record: ExecutionRecord,
    *,
    strategy_id: UUID,
    order_id: int | None,
    perm_id: int | None,
) -> bool:
    """Is this execution one of ours, on this position's opening order?

    The same identity ladder :meth:`engine.options.positions.PositionStore._reconcile_orders`
    uses, and for the same reasons:

    ``orderRef``   our own strategy id, stamped by the combo builder. Durable
                   because we chose it, and a v4 UUID is not a string another
                   tool puts on its orders by accident.
    ``permId``     IBKR's durable identifier, valid across reconnects.
    ``orderId``    client-assigned and unique only within a session, so it is
                   evidence of last resort -- after a reconnect it can name an
                   unrelated order.
    """
    if record.order_ref and record.order_ref == str(strategy_id):
        return True
    if perm_id is not None and record.perm_id == perm_id:
        return True
    return order_id is not None and record.order_id == order_id


def commission_evidence_for(
    *,
    strategy_id: UUID,
    legs: Sequence[OptionLegIntent],
    filled_quantity: Decimal,
    executions: Sequence[ExecutionRecord],
    order_id: int | None = None,
    perm_id: int | None = None,
) -> CommissionEvidence:
    """Decide whether this position's fill cost is fully known.

    Three conditions, all required, and each one is a way the naive version is
    wrong:

    1. **Every leg is represented.** A two-legged spread with executions on one
       leg has half its cost unaccounted for, and the half that is present looks
       like a complete answer.
    2. **Coverage reaches the filled quantity.** ``filled_quantity * ratio``
       contracts must be accounted for on each leg. A partial delivery of a
       three-lot's executions carries a smaller commission that is otherwise
       indistinguishable from a fully-reported one-lot.
    3. **Every matched execution carries a commission report.** See the module
       docstring: an unpopulated report presents as ``0.0``.

    ``filled_quantity`` of zero means nothing is confirmed filled, which is not
    completeness -- it is the absence of anything to be complete about, and it is
    reported as a gap rather than as a costless position.
    """
    mine = tuple(
        e
        for e in executions
        if _belongs_to(e, strategy_id=strategy_id, order_id=order_id, perm_id=perm_id)
    )
    expected_con_ids = tuple(leg.con_id for leg in legs)
    gaps: list[str] = []

    if filled_quantity <= ZERO:
        gaps.append(
            "no confirmed opening fill quantity, so there is nothing to attribute "
            "a commission to"
        )

    shares_by_con_id: dict[int, Decimal] = {}
    for execution in mine:
        shares_by_con_id[execution.con_id] = (
            shares_by_con_id.get(execution.con_id, ZERO) + execution.shares
        )

    for leg in legs:
        covered = shares_by_con_id.get(leg.con_id, ZERO)
        if covered <= ZERO:
            gaps.append(f"no execution reported for leg {leg.con_id}")
            continue
        expected = filled_quantity * Decimal(leg.ratio)
        if expected > ZERO and covered < expected:
            gaps.append(
                f"leg {leg.con_id} is covered for {covered} of {expected} contracts"
            )

    uncosted = tuple(e.exec_id for e in mine if not e.has_commission)
    if uncosted:
        gaps.append(
            f"no commission report for execution(s) {list(uncosted)}; an absent "
            "report presents as 0.0 and must not be summed"
        )

    if gaps:
        return CommissionEvidence(
            strategy_id=strategy_id,
            executions=mine,
            expected_con_ids=expected_con_ids,
            is_complete=False,
            total_commission=None,
            gaps=tuple(gaps),
        )

    total = sum((e.commission for e in mine if e.commission is not None), ZERO)
    return CommissionEvidence(
        strategy_id=strategy_id,
        executions=mine,
        expected_con_ids=expected_con_ids,
        is_complete=True,
        total_commission=total,
        gaps=(),
    )
