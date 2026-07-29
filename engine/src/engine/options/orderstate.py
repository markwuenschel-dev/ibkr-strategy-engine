"""What the broker did with an order, as a state rather than a string.

Before this module the transmit path read one raw IBKR status string and asked a
single question: does it look filled? That collapsed nine genuinely different
outcomes into two, and the collapse was not safe in the direction it failed. A
**partial fill** reported ``filled`` of 1 on a 3-lot with ``remaining`` 2 and was
classified "not filled", which the runner recorded as ``OPEN_FAILED`` -- a live
position, recorded as never opened. Same failure class as reading a negative
credit fill as a failure, reached down a different path.

**Terminal is not the same as successful, and neither is the same as known.**
Three separate questions, so three separate properties:

``is_terminal``   the broker will send nothing further about this order
``is_working``    the order is alive and can still fill
``is_uncertain``  we do not know, and must not guess

``UNKNOWN`` is the honest classification of silence -- a disconnect during
submission, a timeout before any callback, a status string this version has never
seen. It is deliberately not merged into ``REJECTED``: a rejected order is one the
broker refused, while an unknown one may be working, filled, or resting in the
book. Treating the second as the first is how an engine ends up transmitting a
duplicate.

The IBKR status vocabulary is small, undocumented in places, and version-drifting,
so :func:`classify` is written to fail toward ``UNKNOWN`` on anything it does not
recognise rather than toward a state that would let trading continue.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

__all__ = [
    "OrderLifecycleState",
    "BrokerOrderSnapshot",
    "classify",
    "IBKR_WORKING_STATUSES",
    "IBKR_TERMINAL_STATUSES",
]

ZERO = Decimal("0")


class OrderLifecycleState(str, Enum):
    """Where a transmitted order stands, from this engine's point of view.

    Prefixed values so a journal line names the layer without a lookup.
    """

    #: Handed to the API; the broker has not acknowledged it yet.
    SUBMITTED = "ORDER_SUBMITTED"
    #: The broker has it and is working it. Nothing filled.
    ACKNOWLEDGED = "ORDER_ACKNOWLEDGED"
    #: Some quantity filled, some still working.
    PARTIALLY_FILLED = "ORDER_PARTIALLY_FILLED"
    #: Fully filled. The only state that means the position exists as intended.
    FILLED = "ORDER_FILLED"
    #: Cancelled, by us or by the broker, with whatever filled before that.
    CANCELLED = "ORDER_CANCELLED"
    #: The broker refused it outright.
    REJECTED = "ORDER_REJECTED"
    #: IBKR's "Inactive" -- refused or suspended, and it does not say which.
    INACTIVE = "ORDER_INACTIVE"
    #: We stopped waiting. Says nothing about what the order is doing.
    TIMED_OUT = "ORDER_TIMED_OUT"
    #: No usable information. Silence, a disconnect, or an unrecognised status.
    UNKNOWN = "ORDER_UNKNOWN"


#: Statuses in which IBKR may still fill the order.
IBKR_WORKING_STATUSES = frozenset(
    {"pendingsubmit", "presubmitted", "submitted", "pendingcancel"}
)

#: Statuses after which IBKR sends nothing further.
IBKR_TERMINAL_STATUSES = frozenset({"filled", "cancelled", "apicancelled", "inactive"})


def _decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    """A finite Decimal, or ``default``. Never raises on broker junk."""
    if value is None:
        return default
    try:
        parsed = Decimal(str(float(value)))
    except (TypeError, ValueError, ArithmeticError):
        return default
    return parsed if parsed.is_finite() else default


@dataclass(frozen=True)
class BrokerOrderSnapshot:
    """One observation of a transmitted order, normalized.

    ``order_id`` and ``perm_id`` are both carried and neither is optional by
    accident. ``orderId`` is assigned by the client and is only unique per
    session; ``permId`` is IBKR's durable identifier and is the one that survives
    a restart. Recovery needs the second; the first is what most callbacks carry.
    Storing only one is how a reconciler ends up unable to match an order it can
    plainly see.
    """

    state: OrderLifecycleState
    observed_at: dt.datetime
    raw_status: str = ""
    order_id: int | None = None
    perm_id: int | None = None
    filled: Decimal = ZERO
    remaining: Decimal | None = None
    average_price: Decimal | None = None
    commission: Decimal | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, OrderLifecycleState):
            raise ValueError(f"state must be an OrderLifecycleState, got {self.state!r}")
        if not isinstance(self.observed_at, dt.datetime):
            raise ValueError("observed_at must be a datetime")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.filled < ZERO:
            raise ValueError(f"filled must not be negative, got {self.filled}")

    # -- the three separate questions -------------------------------------

    @property
    def is_terminal(self) -> bool:
        """The broker will send nothing further. Says nothing about success."""
        return self.state in {
            OrderLifecycleState.FILLED,
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.REJECTED,
            OrderLifecycleState.INACTIVE,
        }

    @property
    def is_working(self) -> bool:
        """Alive and able to fill. A partial fill is still working."""
        return self.state in {
            OrderLifecycleState.SUBMITTED,
            OrderLifecycleState.ACKNOWLEDGED,
            OrderLifecycleState.PARTIALLY_FILLED,
        }

    @property
    def is_uncertain(self) -> bool:
        """We do not know what this order is doing.

        The runner treats this as a hard block on new entries: an order whose
        outcome is unknown may be resting in the book, and opening another
        position on top of it is how one intended position becomes two.
        """
        return self.state in {
            OrderLifecycleState.TIMED_OUT,
            OrderLifecycleState.UNKNOWN,
        }

    @property
    def has_position(self) -> bool:
        """Some quantity is in the market as a result of this order.

        True for a partial fill and for a cancel-after-partial. This is the
        question the position store actually needs answered -- "did it fill
        completely" is a different and much less important one.
        """
        return self.filled > ZERO

    def describe(self) -> str:
        bits = [self.state.value]
        if self.order_id is not None:
            bits.append(f"order={self.order_id}")
        if self.perm_id is not None:
            bits.append(f"perm={self.perm_id}")
        bits.append(f"filled={self.filled}")
        if self.remaining is not None:
            bits.append(f"remaining={self.remaining}")
        if self.average_price is not None:
            bits.append(f"avg={self.average_price}")
        if self.raw_status:
            bits.append(f"raw={self.raw_status!r}")
        if self.message:
            bits.append(f"msg={self.message}")
        return "  ".join(bits)

    def to_record(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "observed_at": self.observed_at.isoformat(),
            "raw_status": self.raw_status or None,
            "order_id": self.order_id,
            "perm_id": self.perm_id,
            "filled": str(self.filled),
            "remaining": str(self.remaining) if self.remaining is not None else None,
            "average_price": (
                str(self.average_price) if self.average_price is not None else None
            ),
            "commission": str(self.commission) if self.commission is not None else None,
            "message": self.message,
        }


def classify(
    raw_status: Any,
    *,
    filled: Any = 0,
    remaining: Any = None,
    quantity: int | None = None,
    timed_out: bool = False,
    disconnected: bool = False,
    rejected_message: str | None = None,
) -> OrderLifecycleState:
    """Map a broker observation onto a lifecycle state.

    Precedence is deliberate and is the whole design:

    1. **Disconnected wins over everything.** A status string read while the
       socket is down describes the last thing we heard, not the order.
    2. **An explicit rejection message wins over the status.** IBKR frequently
       reports a rejection as status ``Inactive`` plus an error, and ``Inactive``
       alone is ambiguous between refused and suspended.
    3. **Fill counts win over the status string.** ``remaining == 0`` with a
       positive fill is filled, whatever the string says; a positive fill with
       positive remaining is partial. The numbers are what moved money.
    4. **Then the status string**, mapped conservatively.
    5. **Then timeout**, which is about our patience rather than the order.
    6. **Otherwise UNKNOWN**, never a guess.

    Note that step 3 sits above the string on purpose. Statuses and fill
    callbacks can arrive out of order, and a ``Submitted`` string carrying a
    complete fill is a real sequence -- reading the string first would classify a
    finished order as working and leave the runner waiting for a callback that
    has already been and gone.
    """
    if disconnected:
        return OrderLifecycleState.UNKNOWN

    if rejected_message:
        return OrderLifecycleState.REJECTED

    filled_qty = _decimal(filled, ZERO) or ZERO
    remaining_qty = _decimal(remaining, None)
    text = str(raw_status or "").strip().lower()

    # -- terminal broker verdicts, ABOVE the fill arithmetic ---------------
    #
    # The broker being finished is not something a fill count can contradict.
    # An earlier version tested ``remaining`` first, so a cancel-after-partial
    # ("Cancelled", filled=1, remaining=2) classified as PARTIALLY_FILLED --
    # is_working True, is_terminal False -- and the runner would have waited
    # forever on a dead order. ``snapshot_from_trade`` always supplies
    # ``remaining``, so that path was the normal one, not an edge case.
    #
    # "Filled" is deliberately NOT in this group: the count is more trustworthy
    # than that particular string, and the arithmetic below handles it.
    if text in {"cancelled", "apicancelled"}:
        # One exception, and only one: cancelling the *zero* remainder of an
        # order that already filled completely is a real race, and it is a fill,
        # not a cancellation. The known order size is the evidence -- without it
        # there is no way to tell this from a cancel-after-partial, so it stays
        # CANCELLED and the fill count still reports the position via
        # ``has_position``.
        if quantity is not None and filled_qty >= Decimal(quantity):
            return OrderLifecycleState.FILLED
        return OrderLifecycleState.CANCELLED
    if text == "inactive":
        return OrderLifecycleState.INACTIVE

    # -- fill arithmetic, above the working strings ------------------------
    if filled_qty > ZERO:
        if remaining_qty is not None:
            if remaining_qty > ZERO:
                return OrderLifecycleState.PARTIALLY_FILLED
            if quantity is not None and filled_qty < Decimal(quantity):
                # The broker says nothing is left, and less than the order size
                # filled, and it did not say cancelled. The two numbers disagree
                # about a quantity, and guessing which to believe is how one lot
                # gets recorded as three. Refuse and let reconciliation resolve.
                return OrderLifecycleState.UNKNOWN
            return OrderLifecycleState.FILLED
        if quantity is not None:
            # Checked before the "Filled" string, which is the whole point: a
            # status of "Filled" carrying one lot of a three-lot order is a
            # partial, and reading the string would triple the recorded size.
            if filled_qty >= Decimal(quantity):
                return OrderLifecycleState.FILLED
            return OrderLifecycleState.PARTIALLY_FILLED
        if text == "filled":
            return OrderLifecycleState.FILLED
        return OrderLifecycleState.PARTIALLY_FILLED

    # -- nothing filled; the string is all we have ------------------------
    if text == "filled":
        # Filled with a zero fill count is incoherent. Refusing to resolve it is
        # safer than picking whichever half to believe.
        return OrderLifecycleState.UNKNOWN
    if text in {"pendingsubmit", "presubmitted"}:
        return OrderLifecycleState.SUBMITTED
    if text in {"submitted", "pendingcancel"}:
        return OrderLifecycleState.ACKNOWLEDGED

    if timed_out:
        return OrderLifecycleState.TIMED_OUT

    return OrderLifecycleState.UNKNOWN


def snapshot_from_trade(
    trade: Any,
    *,
    observed_at: dt.datetime,
    quantity: int | None = None,
    timed_out: bool = False,
    disconnected: bool = False,
) -> BrokerOrderSnapshot:
    """Normalize an ``ib_async`` ``Trade`` into a snapshot.

    Every field is read defensively. A ``Trade`` that is missing an attribute, or
    whose ``orderStatus`` has not been populated yet, is an ordinary thing for
    IBKR to hand back moments after submission -- it must produce a snapshot
    saying "we do not know", not an ``AttributeError`` out of the transmit path.
    """
    status_obj = getattr(trade, "orderStatus", None)
    order_obj = getattr(trade, "order", None)

    raw_status = str(getattr(status_obj, "status", "") or "")
    filled = _decimal(getattr(status_obj, "filled", 0), ZERO) or ZERO
    remaining = _decimal(getattr(status_obj, "remaining", None), None)
    average = _decimal(getattr(status_obj, "avgFillPrice", None), None)
    if average is not None and average == ZERO:
        # Zero is an unpopulated field, not a price. Negative is legitimate --
        # a net credit fills at a negative average.
        average = None

    order_id = getattr(order_obj, "orderId", None)
    perm_id = getattr(order_obj, "permId", None)
    if perm_id is None:
        perm_id = getattr(status_obj, "permId", None)

    message = None
    log = getattr(trade, "log", None)
    if log:
        try:
            message = str(getattr(log[-1], "message", "") or "") or None
        except (IndexError, TypeError):  # pragma: no cover - defensive
            message = None

    rejected_message = None
    if raw_status.strip().lower() == "inactive" and message:
        rejected_message = message

    # Why it is unknown, not merely that it is. These two land the operator in
    # completely different places -- one is a connection to investigate, the
    # other is an order to chase at the broker -- and the durable record is the
    # only thing that will still be able to tell them apart tomorrow.
    if disconnected:
        message = message or "connection lost while awaiting the order outcome"
    elif timed_out:
        message = message or "no terminal status before the poll timeout expired"

    # Passed through rather than post-corrected. An earlier version omitted this
    # argument and then promoted INACTIVE to REJECTED afterwards, which meant a
    # rejection message arriving alongside a partial fill never reached the
    # rejection branch at all -- classify() said REJECTED and this path said
    # PARTIALLY_FILLED for identical inputs. One decision, made in one place.
    state = classify(
        raw_status,
        filled=filled,
        remaining=remaining,
        quantity=quantity,
        timed_out=timed_out,
        disconnected=disconnected,
        rejected_message=rejected_message,
    )

    return BrokerOrderSnapshot(
        state=state,
        observed_at=observed_at,
        raw_status=raw_status,
        order_id=int(order_id) if isinstance(order_id, int) else None,
        perm_id=int(perm_id) if isinstance(perm_id, int) and perm_id else None,
        filled=filled,
        remaining=remaining,
        average_price=average,
        commission=_decimal(getattr(status_obj, "commission", None), None),
        message=message,
    )
