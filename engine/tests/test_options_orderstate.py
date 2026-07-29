"""What the broker did with an order: the classifier and its precedence.

The precedence in :func:`classify` *is* the design, so these tests assert each
rule beating the one below it rather than only the happy cases. A rule that
still produces the right answer when it is the only thing in play, but loses to
the rule underneath it, is not a rule.

Two coverage tests carry the weight. ``PRODUCERS`` maps every
:class:`OrderLifecycleState` to a callable that actually calls ``classify`` --
executed, not name-matched, because a coverage test that compares enum members
against test-method names proves only that someone typed the name.
``PROPERTY_TABLE`` pins ``is_terminal``/``is_working``/``is_uncertain`` for
every member, so a new state must be classified deliberately instead of
defaulting to "neither terminal nor working" and quietly reading as inert.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any, Callable

import pytest

from engine.options.orderstate import (
    IBKR_TERMINAL_STATUSES,
    IBKR_WORKING_STATUSES,
    BrokerOrderSnapshot,
    OrderLifecycleState,
    classify,
    snapshot_from_trade,
)

D = Decimal
NOW = dt.datetime(2026, 7, 29, 13, 0, tzinfo=dt.timezone.utc)
NAIVE = dt.datetime(2026, 7, 29, 13, 0)

S = OrderLifecycleState


def snapshot(state: OrderLifecycleState, **overrides: Any) -> BrokerOrderSnapshot:
    """A minimal valid snapshot in ``state``, for property assertions."""
    kwargs: dict[str, Any] = {"state": state, "observed_at": NOW}
    kwargs.update(overrides)
    return BrokerOrderSnapshot(**kwargs)


# -- fake ib_async objects ---------------------------------------------------
# Plain classes on purpose. The point of snapshot_from_trade is that it survives
# objects that are missing attributes entirely, which a Mock (which invents
# every attribute) or a real ib_async dataclass (which defaults every attribute)
# cannot stage.


class _Bag:
    """An object that has exactly the attributes it was handed, and no others."""

    def __init__(self, **fields: Any) -> None:
        for name, value in fields.items():
            setattr(self, name, value)


class _LogEntry:
    def __init__(self, message: str) -> None:
        self.message = message


def trade(
    *,
    status: Any = "Submitted",
    filled: Any = 0,
    remaining: Any = None,
    avg_fill_price: Any = None,
    commission: Any = None,
    order_id: Any = 7,
    order_perm_id: Any = None,
    status_perm_id: Any = None,
    messages: tuple[str, ...] = (),
    with_status: bool = True,
    with_order: bool = True,
) -> _Bag:
    """A fake ``Trade``. ``with_status=False`` omits ``orderStatus`` entirely."""
    fields: dict[str, Any] = {}
    if with_status:
        status_fields: dict[str, Any] = {"status": status, "filled": filled}
        if remaining is not None:
            status_fields["remaining"] = remaining
        if avg_fill_price is not None:
            status_fields["avgFillPrice"] = avg_fill_price
        if commission is not None:
            status_fields["commission"] = commission
        if status_perm_id is not None:
            status_fields["permId"] = status_perm_id
        fields["orderStatus"] = _Bag(**status_fields)
    if with_order:
        order_fields: dict[str, Any] = {"orderId": order_id}
        if order_perm_id is not None:
            order_fields["permId"] = order_perm_id
        fields["order"] = _Bag(**order_fields)
    if messages:
        fields["log"] = [_LogEntry(text) for text in messages]
    return _Bag(**fields)


# ===========================================================================
# Precedence rule 1: a disconnect beats everything
# ===========================================================================


class TestDisconnectedWins:
    def test_disconnected_overrides_a_complete_fill(self) -> None:
        """A status read while the socket is down describes the last thing we
        heard, not the order. Believing 'Filled' from a dead socket is how the
        engine records a position that may never have existed."""
        assert (
            classify("Filled", filled=3, remaining=0, quantity=3, disconnected=True)
            is S.UNKNOWN
        )

    def test_disconnected_overrides_a_rejection_message(self) -> None:
        """Rejection beats the status string, but not a disconnect -- the
        message is equally stale."""
        assert (
            classify("Inactive", rejected_message="rejected", disconnected=True)
            is S.UNKNOWN
        )

    def test_disconnected_overrides_a_timeout(self) -> None:
        assert classify("", timed_out=True, disconnected=True) is S.UNKNOWN

    def test_the_same_observation_connected_resolves(self) -> None:
        """The control: without the disconnect flag these inputs are decidable,
        so the UNKNOWN above is the flag doing the work and not an accident of
        the fixture."""
        assert classify("Filled", filled=3, remaining=0, quantity=3) is S.FILLED
        assert classify("Inactive", rejected_message="rejected") is S.REJECTED
        assert classify("", timed_out=True) is S.TIMED_OUT


# ===========================================================================
# Precedence rule 2: an explicit rejection beats the status string
# ===========================================================================


class TestRejectionBeatsTheStatusString:
    def test_inactive_with_a_message_is_rejected(self) -> None:
        """IBKR reports most rejections as status 'Inactive' plus an error. If
        the string wins, every rejection reads as the ambiguous suspended
        state and nothing ever surfaces as refused."""
        assert classify("Inactive", rejected_message="201 margin") is S.REJECTED

    def test_inactive_alone_is_inactive_not_rejected(self) -> None:
        """The two must not merge: 'Inactive' alone is ambiguous between refused
        and suspended, and a suspended order can still fill."""
        assert classify("Inactive") is S.INACTIVE

    def test_a_rejection_message_beats_a_working_status_too(self) -> None:
        assert classify("Submitted", rejected_message="cancelled by system") is S.REJECTED

    def test_a_rejection_message_beats_the_fill_arithmetic(self) -> None:
        """Documented precedence: the message sits above fill counts. Asserted
        so a reorder of the two early returns fails here."""
        assert (
            classify("Submitted", filled=3, remaining=0, rejected_message="no")
            is S.REJECTED
        )

    def test_an_empty_rejection_message_does_not_trigger(self) -> None:
        """Falsy message means no message. An empty string arriving from a
        cleared error field must not manufacture a rejection."""
        assert classify("Inactive", rejected_message="") is S.INACTIVE
        assert classify("Inactive", rejected_message=None) is S.INACTIVE


# ===========================================================================
# Precedence rule 3: fill arithmetic beats the status string
# ===========================================================================


class TestFillArithmeticBeatsTheStatusString:
    def test_submitted_with_a_complete_fill_is_filled(self) -> None:
        """Status and fill callbacks arrive out of order, so a 'Submitted'
        string carrying a complete fill is a real sequence. Reading the string
        first leaves the runner waiting for a callback already spent."""
        assert classify("Submitted", filled=3, remaining=0) is S.FILLED

    def test_presubmitted_with_a_partial_fill_is_partially_filled(self) -> None:
        assert classify("PreSubmitted", filled=1, remaining=2) is S.PARTIALLY_FILLED

    def test_quantity_resolves_a_complete_fill_when_remaining_is_absent(self) -> None:
        """Many callbacks omit ``remaining``. The requested quantity is then the
        only way to tell a finished order from a partial one."""
        assert classify("Submitted", filled=3, quantity=3) is S.FILLED
        assert classify("Submitted", filled=4, quantity=3) is S.FILLED
        assert classify("Submitted", filled=2, quantity=3) is S.PARTIALLY_FILLED

    def test_a_positive_fill_with_no_other_evidence_is_partial(self) -> None:
        """Fail toward 'still working': treating an unquantified fill as
        complete would let the runner stop watching an order that is not done."""
        assert classify("Submitted", filled=1) is S.PARTIALLY_FILLED

    def test_the_status_string_alone_never_reaches_filled(self) -> None:
        """The mirror of the rule: money moved is the evidence for FILLED, not
        the word."""
        assert classify("Filled") is S.UNKNOWN


# ===========================================================================
# Precedence rules 4-6: the status string, then timeout, then UNKNOWN
# ===========================================================================


class TestStatusStringMapping:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("PendingSubmit", S.SUBMITTED),
            ("PreSubmitted", S.SUBMITTED),
            ("Submitted", S.ACKNOWLEDGED),
            ("PendingCancel", S.ACKNOWLEDGED),
            ("Cancelled", S.CANCELLED),
            ("ApiCancelled", S.CANCELLED),
            ("Inactive", S.INACTIVE),
        ],
    )
    def test_each_known_status(self, raw: str, expected: OrderLifecycleState) -> None:
        assert classify(raw) is expected

    def test_matching_is_case_and_whitespace_insensitive(self) -> None:
        """IBKR's casing is not contractual; a case-sensitive match would fail
        toward UNKNOWN on a perfectly ordinary status."""
        assert classify("  PRESUBMITTED  ") is S.SUBMITTED
        assert classify("apicancelled") is S.CANCELLED

    def test_pendingcancel_is_acknowledged_not_cancelled(self) -> None:
        """A cancel request in flight can still fill. Reading it as CANCELLED
        marks a live order terminal and stops the runner watching it."""
        assert classify("PendingCancel") is S.ACKNOWLEDGED
        assert snapshot(S.ACKNOWLEDGED).is_working is True

    def test_the_declared_status_vocabularies_are_the_ones_classified(self) -> None:
        """The exported frozensets are the module's own statement of what it
        recognises. A status in a set that classify() sends to UNKNOWN would
        make those exports lie to whoever imports them."""
        for raw in IBKR_WORKING_STATUSES | IBKR_TERMINAL_STATUSES:
            if raw == "filled":
                # 'Filled' with a zero fill count is deliberately incoherent.
                continue
            assert classify(raw) is not S.UNKNOWN, raw


class TestTimeoutIsLast:
    def test_a_timeout_resolves_only_when_nothing_else_did(self) -> None:
        assert classify("", timed_out=True) is S.TIMED_OUT
        assert classify(None, timed_out=True) is S.TIMED_OUT
        assert classify("gibberish", timed_out=True) is S.TIMED_OUT

    def test_a_known_status_beats_the_timeout(self) -> None:
        """Our patience running out says nothing about the order. If the broker
        told us something, that is what we record."""
        assert classify("Submitted", timed_out=True) is S.ACKNOWLEDGED
        assert classify("Inactive", timed_out=True) is S.INACTIVE
        assert classify("Cancelled", timed_out=True) is S.CANCELLED

    def test_a_fill_beats_the_timeout(self) -> None:
        assert classify("Submitted", filled=3, remaining=0, timed_out=True) is S.FILLED
        assert classify("", filled=1, timed_out=True) is S.PARTIALLY_FILLED

    def test_a_rejection_beats_the_timeout(self) -> None:
        assert classify("", rejected_message="refused", timed_out=True) is S.REJECTED


class TestUnrecognisedFallsToUnknown:
    @pytest.mark.parametrize("raw", ["", None, "   ", "Frobnicated", "Filled?", 42])
    def test_anything_unrecognised_is_unknown(self, raw: Any) -> None:
        """Fail toward UNKNOWN, never toward a state that lets trading
        continue. A new IBKR status string must not read as ACKNOWLEDGED."""
        assert classify(raw) is S.UNKNOWN

    def test_unknown_is_not_rejected(self) -> None:
        """An unknown order may be working, filled, or resting in the book.
        Treating it as refused is how the engine transmits a duplicate."""
        assert classify("who knows") is not S.REJECTED
        assert snapshot(S.UNKNOWN).is_uncertain is True


class TestIncoherentFilledStatus:
    def test_filled_with_a_zero_count_refuses_to_resolve(self) -> None:
        """'Filled' and a fill count of zero cannot both be true. Refusing to
        resolve is safer than picking whichever half to believe -- believing
        the string records a phantom position, believing the count discards a
        real one."""
        assert classify("Filled", filled=0) is S.UNKNOWN
        assert classify("filled", filled=0, remaining=0) is S.UNKNOWN

    def test_it_is_unknown_rather_than_a_working_state(self) -> None:
        assert snapshot(classify("Filled", filled=0)).is_uncertain is True
        assert snapshot(classify("Filled", filled=0)).is_working is False

    def test_filled_below_the_known_quantity_is_a_partial(self) -> None:
        """The count beats the string. A status of 'Filled' carrying 1 of a
        known quantity 3 is a partial fill, and reading the string instead would
        record one lot as three -- then size the exit off three and sell two
        contracts that were never bought."""
        assert classify("Filled", filled=1, quantity=3) is S.PARTIALLY_FILLED
        assert classify("Submitted", filled=1, quantity=3) is S.PARTIALLY_FILLED
        assert classify("Filled", filled=3, quantity=3) is S.FILLED


# ===========================================================================
# Partial fills -- the case that recorded a live position as failed
# ===========================================================================


class TestPartialFills:
    def test_one_of_three_filled_is_partially_filled(self) -> None:
        """The original bug: ``filled=1, remaining=2`` was read as 'not filled'
        and the runner recorded OPEN_FAILED -- a live one-lot position, stored
        as never opened. It is a distinct state, and it is still working."""
        state = classify("Submitted", filled=1, remaining=2)
        assert state is S.PARTIALLY_FILLED
        partial = snapshot(state, filled=D("1"), remaining=D("2"))
        assert partial.is_working is True
        assert partial.is_terminal is False
        assert partial.is_uncertain is False
        assert partial.has_position is True

    def test_a_partial_fill_is_not_confused_with_no_fill(self) -> None:
        assert classify("Submitted", filled=0, remaining=3) is S.ACKNOWLEDGED
        assert classify("Submitted", filled=1, remaining=2) is S.PARTIALLY_FILLED

    def test_fractional_and_string_fill_counts_still_count(self) -> None:
        """Broker fields arrive as floats or strings depending on the callback.
        A string '1' that failed to parse would fall through to the status and
        lose the fill."""
        assert classify("Submitted", filled="1", remaining="2") is S.PARTIALLY_FILLED
        assert classify("Submitted", filled=1.0, remaining=2.0) is S.PARTIALLY_FILLED

    def test_a_zero_remaining_closes_it_out(self) -> None:
        assert classify("Submitted", filled=3, remaining=0) is S.FILLED
        assert classify("Submitted", filled=3, remaining="0") is S.FILLED


# ===========================================================================
# Cancel after a partial fill -- terminal and yet something happened
# ===========================================================================


class TestCancelAfterPartial:
    def test_cancelled_with_a_fill_is_cancelled(self) -> None:
        assert classify("Cancelled", filled=1) is S.CANCELLED
        assert classify("ApiCancelled", filled=1) is S.CANCELLED

    def test_terminal_and_has_position_are_simultaneously_true(self) -> None:
        """The whole point of separating the questions. The broker will send
        nothing further AND there is a live one-lot position. A store that
        reads 'terminal' as 'nothing to record' loses it."""
        state = classify("Cancelled", filled=1)
        observation = snapshot(state, raw_status="Cancelled", filled=D("1"))
        assert observation.is_terminal is True
        assert observation.has_position is True
        assert observation.is_working is False

    def test_a_clean_cancel_is_terminal_with_no_position(self) -> None:
        observation = snapshot(classify("Cancelled"), raw_status="Cancelled")
        assert observation.is_terminal is True
        assert observation.has_position is False

    def test_a_cancel_reporting_remaining_is_terminal(self) -> None:
        """The broker being finished is not something a fill count contradicts.

        An earlier version tested ``remaining`` above the cancelled string, so
        this classified as PARTIALLY_FILLED -- working, non-terminal -- for an
        order the broker had closed. ``snapshot_from_trade`` always supplies
        ``remaining``, so that was the normal live path, not an edge case, and
        the runner would have waited forever on a dead order.
        """
        assert classify("Cancelled", filled=1, remaining=2) is S.CANCELLED
        observation = snapshot(classify("Cancelled", filled=1, remaining=2))
        assert observation.is_terminal is True
        assert observation.is_working is False

    def test_a_full_fill_reported_alongside_cancelled_is_filled(self) -> None:
        """Cancelling the unfilled remainder of a fully filled order is a real
        race. The quantity is the evidence and it beats the string."""
        assert classify("Cancelled", filled=3, quantity=3) is S.FILLED


# ===========================================================================
# Every lifecycle state must be produced by a real call to classify()
# ===========================================================================


def _produce_submitted() -> OrderLifecycleState:
    return classify("PreSubmitted")


def _produce_acknowledged() -> OrderLifecycleState:
    return classify("Submitted")


def _produce_partially_filled() -> OrderLifecycleState:
    return classify("Submitted", filled=1, remaining=2)


def _produce_filled() -> OrderLifecycleState:
    return classify("Submitted", filled=3, remaining=0)


def _produce_cancelled() -> OrderLifecycleState:
    return classify("Cancelled")


def _produce_rejected() -> OrderLifecycleState:
    return classify("Inactive", rejected_message="201 margin insufficient")


def _produce_inactive() -> OrderLifecycleState:
    return classify("Inactive")


def _produce_timed_out() -> OrderLifecycleState:
    return classify("", timed_out=True)


def _produce_unknown() -> OrderLifecycleState:
    return classify("a status this version has never seen")


#: Every member of OrderLifecycleState, mapped to a call that produces it.
PRODUCERS: dict[OrderLifecycleState, Callable[[], OrderLifecycleState]] = {
    S.SUBMITTED: _produce_submitted,
    S.ACKNOWLEDGED: _produce_acknowledged,
    S.PARTIALLY_FILLED: _produce_partially_filled,
    S.FILLED: _produce_filled,
    S.CANCELLED: _produce_cancelled,
    S.REJECTED: _produce_rejected,
    S.INACTIVE: _produce_inactive,
    S.TIMED_OUT: _produce_timed_out,
    S.UNKNOWN: _produce_unknown,
}


class TestEveryStateIsReachable:
    def test_the_producer_table_covers_the_whole_enum(self) -> None:
        """Adding a lifecycle state without a call that reaches it fails here,
        rather than shipping a branch nobody has ever executed."""
        assert set(PRODUCERS) == set(OrderLifecycleState)

    @pytest.mark.parametrize("state", sorted(OrderLifecycleState, key=lambda s: s.value))
    def test_each_state_is_actually_produced(self, state: OrderLifecycleState) -> None:
        """The producer is EXECUTED. Matching enum members against test-method
        names proves only that someone typed the name."""
        assert PRODUCERS[state]() is state


# ===========================================================================
# The three questions are three separate properties
# ===========================================================================

#: state -> (is_terminal, is_working, is_uncertain). Terminal is not the same as
#: successful, and neither is the same as known.
PROPERTY_TABLE: dict[OrderLifecycleState, tuple[bool, bool, bool]] = {
    S.SUBMITTED: (False, True, False),
    S.ACKNOWLEDGED: (False, True, False),
    S.PARTIALLY_FILLED: (False, True, False),
    S.FILLED: (True, False, False),
    S.CANCELLED: (True, False, False),
    S.REJECTED: (True, False, False),
    S.INACTIVE: (True, False, False),
    S.TIMED_OUT: (False, False, True),
    S.UNKNOWN: (False, False, True),
}


class TestLifecycleProperties:
    def test_the_property_table_covers_the_whole_enum(self) -> None:
        """A new state must be classified deliberately. Left out of the table it
        would read as neither terminal nor working nor uncertain -- inert, and
        silently ignored by every caller that branches on these."""
        assert set(PROPERTY_TABLE) == set(OrderLifecycleState)

    @pytest.mark.parametrize("state", sorted(OrderLifecycleState, key=lambda s: s.value))
    def test_each_state_answers_the_three_questions(
        self, state: OrderLifecycleState
    ) -> None:
        terminal, working, uncertain = PROPERTY_TABLE[state]
        observation = snapshot(state)
        assert observation.is_terminal is terminal
        assert observation.is_working is working
        assert observation.is_uncertain is uncertain

    @pytest.mark.parametrize("state", sorted(OrderLifecycleState, key=lambda s: s.value))
    def test_no_state_is_both_terminal_and_working(
        self, state: OrderLifecycleState
    ) -> None:
        """Contradictory: the broker cannot both be finished with the order and
        still able to fill it. A caller reading either property first would get
        opposite answers about the same order."""
        observation = snapshot(state)
        assert not (observation.is_terminal and observation.is_working)

    def test_uncertainty_is_neither_terminal_nor_working(self) -> None:
        for state in (S.TIMED_OUT, S.UNKNOWN):
            observation = snapshot(state)
            assert observation.is_uncertain is True
            assert observation.is_terminal is False
            assert observation.is_working is False

    def test_has_position_is_independent_of_the_state(self) -> None:
        """'Did any quantity reach the market' is the question the position
        store needs, and it is not answerable from the lifecycle state alone."""
        assert snapshot(S.CANCELLED, filled=D("1")).has_position is True
        assert snapshot(S.CANCELLED, filled=D("0")).has_position is False
        assert snapshot(S.UNKNOWN, filled=D("2")).has_position is True
        assert snapshot(S.FILLED, filled=D("0")).has_position is False


# ===========================================================================
# BrokerOrderSnapshot invariants
# ===========================================================================


class TestSnapshotInvariants:
    def test_a_naive_observed_at_is_refused(self) -> None:
        """A naive timestamp is an unlabelled one. Journalled next to UTC it
        silently reorders the lifecycle by the local UTC offset."""
        with pytest.raises(ValueError, match="timezone-aware"):
            BrokerOrderSnapshot(state=S.FILLED, observed_at=NAIVE)

    def test_a_non_datetime_observed_at_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be a datetime"):
            BrokerOrderSnapshot(state=S.FILLED, observed_at="2026-07-29T13:00:00Z")  # type: ignore[arg-type]

    def test_a_bare_string_state_is_refused(self) -> None:
        """OrderLifecycleState is a str enum, so a raw string compares equal to
        a member and would be indistinguishable from one until something tried
        to read ``.value`` off it."""
        with pytest.raises(ValueError, match="must be an OrderLifecycleState"):
            BrokerOrderSnapshot(state="ORDER_FILLED", observed_at=NOW)  # type: ignore[arg-type]

    def test_a_none_state_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be an OrderLifecycleState"):
            BrokerOrderSnapshot(state=None, observed_at=NOW)  # type: ignore[arg-type]

    def test_a_negative_fill_is_refused(self) -> None:
        """A negative fill count is not a short position -- direction lives in
        the leg actions. It is corrupt data, and ``has_position`` would read it
        as nothing filled."""
        with pytest.raises(ValueError, match="must not be negative"):
            BrokerOrderSnapshot(state=S.FILLED, observed_at=NOW, filled=D("-1"))

    def test_a_zero_fill_is_accepted(self) -> None:
        assert BrokerOrderSnapshot(state=S.SUBMITTED, observed_at=NOW).filled == D("0")

    def test_describe_names_the_state_and_the_numbers(self) -> None:
        text = snapshot(
            S.PARTIALLY_FILLED,
            raw_status="Submitted",
            order_id=7,
            perm_id=99,
            filled=D("1"),
            remaining=D("2"),
            average_price=D("-1.25"),
            message="partial",
        ).describe()
        assert "ORDER_PARTIALLY_FILLED" in text
        assert "order=7" in text and "perm=99" in text
        assert "filled=1" in text and "remaining=2" in text
        assert "avg=-1.25" in text


class TestSnapshotRecord:
    def _full(self) -> BrokerOrderSnapshot:
        return snapshot(
            S.PARTIALLY_FILLED,
            raw_status="Submitted",
            order_id=7,
            perm_id=99,
            filled=D("1"),
            remaining=D("2"),
            average_price=D("-1.25"),
            commission=D("1.30"),
            message="partial fill",
        )

    def test_the_record_is_json_serialisable(self) -> None:
        """The journal writes these straight out. A Decimal or a datetime left
        in the dict raises at write time, i.e. after the order already went to
        the broker -- the one moment the record must not be lost."""
        encoded = json.dumps(self._full().to_record())
        assert json.loads(encoded)["state"] == "ORDER_PARTIALLY_FILLED"

    def test_every_decimal_is_stringified(self) -> None:
        """str(Decimal) round-trips exactly; float(Decimal) does not. A price
        journalled through float is no longer the price that was paid."""
        record = self._full().to_record()
        for key in ("filled", "remaining", "average_price", "commission"):
            assert isinstance(record[key], str), key
        assert record["average_price"] == "-1.25"
        assert record["commission"] == "1.30"

    def test_the_record_carries_no_python_objects(self) -> None:
        record = self._full().to_record()
        for key, value in record.items():
            assert value is None or isinstance(value, (str, int, float, bool)), key
        assert record["observed_at"] == NOW.isoformat()
        assert record["state"] == "ORDER_PARTIALLY_FILLED"

    def test_an_empty_record_is_still_json_safe(self) -> None:
        record = snapshot(S.UNKNOWN).to_record()
        assert json.dumps(record)
        assert record["raw_status"] is None
        assert record["remaining"] is None
        assert record["filled"] == "0"


# ===========================================================================
# snapshot_from_trade: defensive against everything IBKR hands back
# ===========================================================================


class TestSnapshotFromTrade:
    def test_a_trade_with_no_order_status_yields_a_snapshot(self) -> None:
        """IBKR routinely returns a Trade whose orderStatus has not been
        populated moments after submission. An AttributeError out of the
        transmit path there loses the order id of an order already sent."""
        observation = snapshot_from_trade(
            trade(with_status=False), observed_at=NOW
        )
        assert observation.state is S.UNKNOWN
        assert observation.raw_status == ""
        assert observation.filled == D("0")
        assert observation.order_id == 7

    def test_a_trade_with_nothing_at_all_yields_a_snapshot(self) -> None:
        observation = snapshot_from_trade(
            trade(with_status=False, with_order=False), observed_at=NOW
        )
        assert observation.state is S.UNKNOWN
        assert observation.order_id is None
        assert observation.perm_id is None

    def test_a_zero_average_price_becomes_none(self) -> None:
        """avgFillPrice defaults to 0.0 on an unfilled order. Recording zero as
        the fill price books a free trade."""
        observation = snapshot_from_trade(
            trade(status="Submitted", avg_fill_price=0.0), observed_at=NOW
        )
        assert observation.average_price is None

    def test_a_negative_average_price_is_preserved(self) -> None:
        """A net credit fills at a NEGATIVE average. Screening negatives out as
        'invalid' was a real bug in this repo: the credit that was actually
        collected got recorded as no price at all."""
        observation = snapshot_from_trade(
            trade(status="Filled", filled=1, remaining=0, avg_fill_price=-1.25),
            observed_at=NOW,
        )
        assert observation.average_price == D("-1.25")
        assert observation.state is S.FILLED

    def test_perm_id_comes_from_the_order_when_present(self) -> None:
        """permId is the durable identifier that survives a restart; orderId is
        only unique per session. A reconciler with the wrong one cannot match
        an order it can plainly see."""
        observation = snapshot_from_trade(
            trade(order_perm_id=555, status_perm_id=999), observed_at=NOW
        )
        assert observation.perm_id == 555

    def test_perm_id_falls_back_to_the_order_status(self) -> None:
        observation = snapshot_from_trade(
            trade(status_perm_id=999), observed_at=NOW
        )
        assert observation.perm_id == 999

    def test_a_zero_perm_id_is_treated_as_unassigned(self) -> None:
        """IBKR uses 0 for 'not assigned yet'. Storing 0 as an id makes every
        unassigned order collide with every other one."""
        observation = snapshot_from_trade(
            trade(order_perm_id=0, status_perm_id=0), observed_at=NOW
        )
        assert observation.perm_id is None

    def test_inactive_with_a_log_message_classifies_as_rejected(self) -> None:
        """The rejection text lives in trade.log, not in orderStatus. Without
        reading it, every refusal reads as the ambiguous INACTIVE."""
        observation = snapshot_from_trade(
            trade(status="Inactive", messages=("Order rejected - 201 margin",)),
            observed_at=NOW,
        )
        assert observation.state is S.REJECTED
        assert observation.message == "Order rejected - 201 margin"
        assert observation.is_terminal is True

    def test_the_two_paths_agree_on_rejection_precedence(self) -> None:
        """One decision, made in one place.

        ``snapshot_from_trade`` previously omitted ``rejected_message`` from its
        call to ``classify`` and post-corrected only an INACTIVE result, so a
        rejection arriving alongside a partial fill gave REJECTED through one
        path and PARTIALLY_FILLED through the other -- for identical inputs.
        """
        assert classify("Inactive", filled=1, rejected_message="rejected 201") is S.REJECTED
        observation = snapshot_from_trade(
            trade(status="Inactive", filled=1, messages=("rejected 201",)),
            observed_at=NOW,
        )
        assert observation.state is S.REJECTED
        assert observation.message == "rejected 201"
        # Rejected and yet carrying a position: the two are independent, and the
        # store must record the contracts that filled before the refusal.
        assert observation.has_position is True

    def test_inactive_without_a_log_message_stays_inactive(self) -> None:
        observation = snapshot_from_trade(trade(status="Inactive"), observed_at=NOW)
        assert observation.state is S.INACTIVE
        assert observation.message is None

    def test_only_the_last_log_entry_is_kept(self) -> None:
        observation = snapshot_from_trade(
            trade(status="Inactive", messages=("submitted", "final refusal")),
            observed_at=NOW,
        )
        assert observation.message == "final refusal"

    def test_a_log_message_on_a_working_status_does_not_reject(self) -> None:
        """Every trade carries log entries. Promoting any message to a
        rejection would refuse every order that logged anything."""
        observation = snapshot_from_trade(
            trade(status="Submitted", messages=("order submitted",)),
            observed_at=NOW,
        )
        assert observation.state is S.ACKNOWLEDGED

    def test_a_garbage_fill_count_does_not_raise(self) -> None:
        """Broker junk in a numeric field must degrade to zero, not take down
        the transmit path with a ValueError."""
        observation = snapshot_from_trade(
            trade(status="Submitted", filled="not-a-number"), observed_at=NOW
        )
        assert observation.filled == D("0")
        assert observation.state is S.ACKNOWLEDGED

    def test_garbage_in_every_numeric_field_does_not_raise(self) -> None:
        observation = snapshot_from_trade(
            trade(
                status="Submitted",
                filled=object(),
                remaining="???",
                avg_fill_price="n/a",
                commission=[],
            ),
            observed_at=NOW,
        )
        assert observation.filled == D("0")
        assert observation.remaining is None
        assert observation.average_price is None
        assert observation.commission is None

    def test_a_non_integer_order_id_is_dropped_rather_than_coerced(self) -> None:
        observation = snapshot_from_trade(trade(order_id="7"), observed_at=NOW)
        assert observation.order_id is None

    def test_a_partial_fill_survives_the_whole_path(self) -> None:
        """End to end, the case that caused the original loss: a one-of-three
        fill must arrive at the store as PARTIALLY_FILLED with a position."""
        observation = snapshot_from_trade(
            trade(status="Submitted", filled=1, remaining=2, avg_fill_price=-1.25),
            observed_at=NOW,
        )
        assert observation.state is S.PARTIALLY_FILLED
        assert observation.is_working is True
        assert observation.is_terminal is False
        assert observation.has_position is True
        assert json.dumps(observation.to_record())

    def test_a_disconnect_overrides_the_trade(self) -> None:
        observation = snapshot_from_trade(
            trade(status="Filled", filled=3, remaining=0),
            observed_at=NOW,
            disconnected=True,
        )
        assert observation.state is S.UNKNOWN
        assert observation.filled == D("3")

    def test_a_timeout_only_applies_when_the_trade_says_nothing(self) -> None:
        assert (
            snapshot_from_trade(
                trade(status=""), observed_at=NOW, timed_out=True
            ).state
            is S.TIMED_OUT
        )
        assert (
            snapshot_from_trade(
                trade(status="Submitted"), observed_at=NOW, timed_out=True
            ).state
            is S.ACKNOWLEDGED
        )

    def test_quantity_resolves_a_fill_when_remaining_is_absent(self) -> None:
        observation = snapshot_from_trade(
            trade(status="Submitted", filled=3), observed_at=NOW, quantity=3
        )
        assert observation.state is S.FILLED

    def test_the_observed_at_must_still_be_aware(self) -> None:
        """The snapshot invariant is not bypassed by the convenience builder."""
        with pytest.raises(ValueError, match="timezone-aware"):
            snapshot_from_trade(trade(), observed_at=NAIVE)
