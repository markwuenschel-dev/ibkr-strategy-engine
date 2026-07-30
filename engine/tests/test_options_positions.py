"""The position store: construction invariants, replay, and reconciliation.

The section that matters most is the replay one. Every state the engine can be
in after a restart is reconstructed from the log alone, so a test that only
exercised the in-memory path would prove nothing about the property the module
exists for -- a spread nobody is watching still expires.

Two liveness tests are written exhaustively over :class:`PositionState` rather
than against a hand-picked list, so a new state has to be classified
deliberately instead of defaulting to "not live" and quietly dropping out of
management.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from engine.errors import InvalidPortfolioStateError, JournalError
from engine.options.domain import (
    OptionLegIntent,
    OptionRight,
    OptionStrategyIntent,
    OrderAction,
    PriceEffect,
    StrategyAction,
    StrategyType,
)
from engine.options.portfolio import PositionExposure
from engine.options.positions import (
    OpenPosition,
    PositionEvent,
    PositionState,
    PositionStore,
    ReconciliationOutcome,
    ReconciliationReport,
)

D = Decimal
NOW = dt.datetime(2026, 7, 29, 13, 0, tzinfo=dt.timezone.utc)
LATER = NOW + dt.timedelta(hours=1)
EXPIRY = dt.date(2026, 9, 18)
SHORT_CON_ID = 1001
LONG_CON_ID = 1002

#: What the broker holds against the standard 5-wide spread below. Nothing in
#: these tests depends on the figure being the broker's real one; it only has to
#: be a non-negative Decimal so the exposure it produces is well formed.
BPR = D("350.00")


def spread(
    credit: str = "1.50",
    quantity: int = 1,
    underlying: str = "SPY",
    con_id_base: int = SHORT_CON_ID,
) -> OptionStrategyIntent:
    """A 5-wide put credit spread: short 500, long 495.

    ``con_id_base`` keeps two simultaneously-open structures on distinct
    contracts, which is what a real book would look like.
    """
    legs = (
        OptionLegIntent(
            con_id=con_id_base,
            symbol=underlying.strip().upper(),
            expiration=EXPIRY,
            strike=D("500"),
            right=OptionRight.PUT,
            action=OrderAction.SELL,
            ratio=1,
            multiplier=100,
            exchange="SMART",
        ),
        OptionLegIntent(
            con_id=con_id_base + 1,
            symbol=underlying.strip().upper(),
            expiration=EXPIRY,
            strike=D("495"),
            right=OptionRight.PUT,
            action=OrderAction.BUY,
            ratio=1,
            multiplier=100,
            exchange="SMART",
        ),
    )
    return OptionStrategyIntent(
        strategy_id=uuid4(),
        strategy_type=StrategyType.PUT_CREDIT_SPREAD,
        strategy_action=StrategyAction.OPEN,
        underlying=underlying,
        quantity=quantity,
        legs=legs,
        expiration=EXPIRY,
        limit_price=D(credit),
        price_effect=PriceEffect.CREDIT,
        maximum_loss_per_contract=(D("5") - D(credit)) * 100,
        configuration_version="test",
        created_at=NOW,
    )


def closing(intent: OptionStrategyIntent) -> OptionStrategyIntent:
    """The CLOSE intent that retires ``intent``, built from its own legs."""
    return intent.closing_intent(
        strategy_id=intent.strategy_id,
        limit_price=D("0.40"),
        created_at=NOW,
        configuration_version="test",
        quantity=intent.quantity,
    )


def position(**overrides: Any) -> OpenPosition:
    """A plain OPEN position, with any field replaced by keyword."""
    intent = overrides.pop("intent", None)
    if intent is None:
        intent = spread()
    kwargs: dict[str, Any] = {
        "strategy_id": intent.strategy_id,
        "intent": intent,
        "opened_at": NOW,
        "state": PositionState.OPEN,
        "buying_power_reserved": BPR,
        "filled_credit": D("1.50"),
    }
    kwargs.update(overrides)
    return OpenPosition(**kwargs)


def position_in(state: PositionState) -> OpenPosition:
    """One position in ``state``, carrying whatever that state requires."""
    extras: dict[str, Any] = {}
    if state is PositionState.CLOSED:
        extras["closed_at"] = LATER
        extras["closing_debit"] = D("0.40")
    if state is PositionState.ROLLED:
        extras["rolled_to"] = uuid4()
    if state is PositionState.UNCERTAIN:
        extras["uncertainty"] = "socket dropped while polling"
    return position(state=state, **extras)


def submitted(
    path: Path,
    intent: OptionStrategyIntent | None = None,
    *,
    at: dt.datetime = NOW,
    buying_power_reserved: Decimal = BPR,
) -> tuple[PositionStore, OptionStrategyIntent]:
    """A store with one OPEN_SUBMITTED event already written."""
    if intent is None:
        intent = spread()
    store = PositionStore(path)
    store.record_open_submitted(
        intent, at=at, buying_power_reserved=buying_power_reserved
    )
    return store, intent


def only(store: PositionStore, strategy_id: UUID) -> OpenPosition:
    """The replayed position for ``strategy_id``, asserting it exists."""
    found = store.get(strategy_id)
    assert found is not None, f"{strategy_id} vanished from the replayed book"
    return found


#: Every member of PositionState, mapped to whether the market can still move
#: against a position in it. Asserted to be exactly the enum below.
EXPECTED_LIVENESS: dict[PositionState, bool] = {
    PositionState.OPENING: True,
    PositionState.OPEN: True,
    PositionState.CLOSING: True,
    PositionState.CLOSED: False,
    PositionState.ROLLED: False,
    # Live, deliberately. UNCERTAIN is exactly the state in which we do not know
    # whether something is in the market, and reading "might be" as "is not" is
    # how a real position stops being managed.
    PositionState.UNCERTAIN: True,
}


# ===========================================================================
# Construction invariants
# ===========================================================================


class TestOpenPositionInvariants:
    def test_a_valid_position_is_built(self) -> None:
        """The baseline every refusal below is a single mutation away from."""
        held = position()
        assert held.state is PositionState.OPEN
        assert held.filled_credit == D("1.50")

    def test_the_intent_must_be_an_option_strategy_intent(self) -> None:
        """A flattened dict or a look-alike would give a position whose close
        could not be built from the legs that were opened."""
        with pytest.raises(InvalidPortfolioStateError, match="must be an OptionStrategyIntent"):
            OpenPosition(
                strategy_id=uuid4(),
                intent={"underlying": "SPY"},  # type: ignore[arg-type]
                opened_at=NOW,
                state=PositionState.OPEN,
                buying_power_reserved=BPR,
                filled_credit=D("1.50"),
            )

    def test_a_close_intent_may_not_be_stored_as_the_position(self) -> None:
        """Storing the CLOSE would invert every leg, so the next close built
        from it would re-open the structure."""
        intent = spread()
        with pytest.raises(InvalidPortfolioStateError, match="must hold its OPEN intent"):
            position(intent=closing(intent), strategy_id=intent.strategy_id)

    def test_the_strategy_id_must_match_its_intent(self) -> None:
        """Two ids means the book is keyed by one and the order references the
        other, and a close would be sent for a structure nobody holds."""
        intent = spread()
        with pytest.raises(InvalidPortfolioStateError, match="does not match its intent"):
            position(intent=intent, strategy_id=uuid4())

    def test_the_state_must_be_a_position_state(self) -> None:
        with pytest.raises(InvalidPortfolioStateError, match="must be a PositionState"):
            position(state="OPEN")

    def test_opened_at_must_be_timezone_aware(self) -> None:
        """A naive timestamp cannot be compared against the journal's UTC, so
        neither DTE nor the age of an OPENING strand would be computable."""
        with pytest.raises(InvalidPortfolioStateError, match="timezone-aware"):
            position(opened_at=dt.datetime(2026, 7, 29, 13, 0))

    def test_a_zero_filled_credit_is_refused(self) -> None:
        with pytest.raises(InvalidPortfolioStateError, match="positive Decimal"):
            position(filled_credit=D("0"))

    def test_a_negative_filled_credit_is_refused(self) -> None:
        with pytest.raises(InvalidPortfolioStateError, match="positive Decimal"):
            position(filled_credit=D("-1.50"))

    def test_a_float_filled_credit_is_refused(self) -> None:
        """Binary floats do not represent a credit exactly, and this figure is
        what the profit-target arithmetic runs against."""
        with pytest.raises(InvalidPortfolioStateError, match="positive Decimal"):
            position(filled_credit=1.50)

    def test_buying_power_reserved_must_be_a_decimal(self) -> None:
        with pytest.raises(InvalidPortfolioStateError, match="must be a Decimal"):
            position(buying_power_reserved=350.0)

    def test_negative_buying_power_reserved_is_refused(self) -> None:
        """A negative reservation would subtract from the book's total and let
        the governor approve a candidate the account cannot carry."""
        with pytest.raises(InvalidPortfolioStateError, match="must not be negative"):
            position(buying_power_reserved=D("-1"))

    def test_zero_buying_power_reserved_is_allowed(self) -> None:
        """Zero is a real answer -- the broker can hold nothing against a
        structure -- so it must not be refused alongside negatives."""
        assert position(buying_power_reserved=D("0")).buying_power_reserved == D("0")

    def test_a_closed_position_must_record_when_it_closed(self) -> None:
        with pytest.raises(InvalidPortfolioStateError, match="when it closed"):
            position(state=PositionState.CLOSED, closed_at=None)

    def test_a_rolled_position_must_name_what_it_rolled_into(self) -> None:
        """Without the link the old position is closed and the new one looks
        unrelated, so the roll cannot be audited afterwards."""
        with pytest.raises(InvalidPortfolioStateError, match="rolled into"):
            position(state=PositionState.ROLLED, rolled_to=None)


# ===========================================================================
# Derived properties
# ===========================================================================


class TestDerivedProperties:
    def test_underlying_is_normalized(self) -> None:
        """The governor buckets concentration by symbol; ' spy ' and 'SPY' must
        not be able to become two separate buckets."""
        held = position(intent=spread(underlying=" spy "))
        assert held.underlying == "SPY"

    def test_dte_counts_calendar_days_across_a_month_boundary(self) -> None:
        """The 21-DTE rule is the one defensive trigger that needs no market
        data; an off-by-a-month here would fire it late or never."""
        held = position()
        assert held.expiration == dt.date(2026, 9, 18)
        assert held.dte(dt.date(2026, 8, 30)) == 19
        assert held.dte(dt.date(2026, 9, 18)) == 0
        assert held.dte(dt.date(2026, 9, 19)) == -1

    def test_total_maximum_loss_scales_with_quantity(self) -> None:
        """Sizing off the per-contract figure while capping the total is a
        quantity-squared error waiting to happen."""
        held = position(intent=spread(quantity=3))
        assert held.intent.maximum_loss_per_contract == D("350.00")
        assert held.total_maximum_loss == D("1050.00")
        assert held.total_maximum_loss == held.intent.maximum_loss_per_contract * 3

    def test_pass_through_properties_follow_the_intent(self) -> None:
        intent = spread(quantity=2)
        held = position(intent=intent)
        assert held.strategy_type is StrategyType.PUT_CREDIT_SPREAD
        assert held.quantity == 2
        assert held.multiplier == 100
        assert held.legs == intent.legs

    def test_the_liveness_table_covers_the_whole_enum(self) -> None:
        """A new PositionState must be classified deliberately. Defaulting to
        'not live' would drop it out of management silently."""
        assert set(EXPECTED_LIVENESS) == set(PositionState)

    @pytest.mark.parametrize("state", sorted(PositionState, key=lambda s: s.value))
    def test_is_live_for_every_state(self, state: PositionState) -> None:
        assert position_in(state).is_live is EXPECTED_LIVENESS[state]

    def test_describe_names_the_state_and_the_risk(self) -> None:
        text = position().describe()
        assert "OPEN" in text
        assert "SPY" in text
        assert "350.00" in text


# ===========================================================================
# Event replay
# ===========================================================================


class TestReplay:
    def test_a_submitted_open_is_opening_and_already_in_the_book(
        self, tmp_path: Path
    ) -> None:
        """Recorded before transmission on purpose: a crash here leaves an
        OPENING record the reconciler can resolve, which is recoverable. A fill
        with no record is not."""
        store, intent = submitted(tmp_path / "positions.jsonl")
        held = only(store, intent.strategy_id)
        assert held.state is PositionState.OPENING
        assert [p.strategy_id for p in store.open_positions()] == [intent.strategy_id]
        assert held.buying_power_reserved == BPR

    def test_a_fill_overwrites_the_intended_credit_with_the_filled_one(
        self, tmp_path: Path
    ) -> None:
        """Every profit target and P&L figure is computed off this number. Left
        at the intended price it would be wrong on every position that improved
        or slipped."""
        store, intent = submitted(tmp_path / "positions.jsonl", spread("1.50"))
        store.record_open_filled(intent.strategy_id, at=LATER, filled_credit=D("1.55"))
        held = only(store, intent.strategy_id)
        assert held.state is PositionState.OPEN
        assert held.filled_credit == D("1.55")
        assert held.intent.limit_price == D("1.50")

    def test_a_failed_open_leaves_no_position_at_all(self, tmp_path: Path) -> None:
        """An open that never filled is not a position. Keeping it would reserve
        buying power against nothing and block real candidates."""
        store, intent = submitted(tmp_path / "positions.jsonl")
        store.record_open_failed(intent.strategy_id, at=LATER, reason="no fill")
        assert store.get(intent.strategy_id) is None
        assert store.positions() == {}
        assert store.open_positions() == []

    def test_a_submitted_close_is_closing_and_still_live(self, tmp_path: Path) -> None:
        """The close is sent, not filled. The market can still move against the
        structure, so it must stay in the exposure total."""
        store, intent = submitted(tmp_path / "positions.jsonl")
        store.record_open_filled(intent.strategy_id, at=LATER, filled_credit=D("1.50"))
        store.record_close_submitted(
            intent.strategy_id, at=LATER, target_debit=D("0.40")
        )
        held = only(store, intent.strategy_id)
        assert held.state is PositionState.CLOSING
        assert held.is_live
        assert [p.strategy_id for p in store.open_positions()] == [intent.strategy_id]

    def test_a_filled_close_retires_the_position_and_records_the_debit(
        self, tmp_path: Path
    ) -> None:
        store, intent = submitted(tmp_path / "positions.jsonl")
        store.record_open_filled(intent.strategy_id, at=LATER, filled_credit=D("1.50"))
        store.record_close_submitted(intent.strategy_id, at=LATER)
        store.record_close_filled(
            intent.strategy_id, at=LATER, closing_debit=D("0.42")
        )
        held = only(store, intent.strategy_id)
        assert held.state is PositionState.CLOSED
        assert held.closing_debit == D("0.42")
        assert held.closed_at == LATER
        assert not held.is_live
        assert store.open_positions() == []

    def test_a_failed_close_returns_the_position_to_open(self, tmp_path: Path) -> None:
        """The one that would hurt most quietly: stranded in CLOSING, the
        position is still in the market but management skips it, so the next
        21-DTE or stop-loss trigger never fires."""
        store, intent = submitted(tmp_path / "positions.jsonl")
        store.record_open_filled(intent.strategy_id, at=LATER, filled_credit=D("1.50"))
        store.record_close_submitted(intent.strategy_id, at=LATER)
        store.record_close_failed(
            intent.strategy_id, at=LATER, reason="limit not reached"
        )
        held = only(store, intent.strategy_id)
        assert held.state is PositionState.OPEN
        assert held.is_live
        assert [p.strategy_id for p in store.open_positions()] == [intent.strategy_id]

    def test_a_roll_records_the_structure_it_became(self, tmp_path: Path) -> None:
        store, intent = submitted(tmp_path / "positions.jsonl")
        store.record_open_filled(intent.strategy_id, at=LATER, filled_credit=D("1.50"))
        successor = uuid4()
        store.record_rolled(intent.strategy_id, into=successor, at=LATER)
        held = only(store, intent.strategy_id)
        assert held.state is PositionState.ROLLED
        assert held.rolled_to == successor
        assert not held.is_live
        assert store.open_positions() == []

    def test_a_second_store_on_the_same_path_replays_identical_state(
        self, tmp_path: Path
    ) -> None:
        """The restart property. Nothing is cached in the first store that the
        second cannot rebuild from the file alone."""
        path = tmp_path / "positions.jsonl"
        store, first = submitted(path, spread(underlying="SPY"), at=NOW)
        store.record_open_filled(first.strategy_id, at=LATER, filled_credit=D("1.55"))
        second = spread(underlying="QQQ", con_id_base=2001)
        store.record_open_submitted(second, at=LATER, buying_power_reserved=D("300.00"))
        store.record_close_submitted(second.strategy_id, at=LATER)

        restarted = PositionStore(path)
        assert set(restarted.positions()) == set(store.positions())
        for strategy_id, before in store.positions().items():
            after = only(restarted, strategy_id)
            assert after.state is before.state
            assert after.filled_credit == before.filled_credit
            assert after.underlying == before.underlying
            assert after.buying_power_reserved == before.buying_power_reserved
            assert after.total_maximum_loss == before.total_maximum_loss
            assert after.opened_at == before.opened_at
        assert [p.strategy_id for p in restarted.open_positions()] == [
            p.strategy_id for p in store.open_positions()
        ]

    def test_a_torn_final_line_is_skipped_rather_than_crashing_the_replay(
        self, tmp_path: Path
    ) -> None:
        """A crash mid-append truncates the last line. Refusing to replay would
        turn one lost event into a lost book."""
        path = tmp_path / "positions.jsonl"
        store, intent = submitted(path)
        store.record_open_filled(intent.strategy_id, at=LATER, filled_credit=D("1.55"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"event": "CLOSE_FILLED", "strategy_id": "')

        held = only(store, intent.strategy_id)
        assert held.state is PositionState.OPEN
        assert held.filled_credit == D("1.55")

    def test_an_event_for_an_unknown_strategy_is_ignored(self, tmp_path: Path) -> None:
        """A fill for something never submitted cannot be turned into a position
        -- there is no validated entry credit or maximum loss to invent."""
        store = PositionStore(tmp_path / "positions.jsonl")
        store.record_open_filled(uuid4(), at=NOW, filled_credit=D("1.55"))
        store.record_close_filled(uuid4(), at=NOW, closing_debit=D("0.40"))
        store.record_rolled(uuid4(), into=uuid4(), at=NOW)
        assert store.positions() == {}
        assert store.open_positions() == []

    def test_an_empty_or_absent_file_replays_to_an_empty_book(
        self, tmp_path: Path
    ) -> None:
        assert PositionStore(tmp_path / "never-written.jsonl").positions() == {}
        assert PositionStore(tmp_path / "never-written.jsonl").exposures() == ()

    def test_every_event_kind_is_written_with_its_declared_name(
        self, tmp_path: Path
    ) -> None:
        """The vocabulary on disk is the replay's only input; a renamed event
        would be silently ignored by ``positions()`` rather than failing."""
        path = tmp_path / "positions.jsonl"
        store, intent = submitted(path)
        store.record_open_filled(intent.strategy_id, at=LATER, filled_credit=D("1.55"))
        store.record_close_submitted(intent.strategy_id, at=LATER)
        store.record_close_failed(intent.strategy_id, at=LATER, reason="no fill")
        store.record_close_submitted(intent.strategy_id, at=LATER)
        store.record_close_filled(intent.strategy_id, at=LATER, closing_debit=D("0.40"))
        kinds = [event["event"] for event in store.events()]
        assert kinds == [
            PositionEvent.OPEN_SUBMITTED.value,
            PositionEvent.OPEN_FILLED.value,
            PositionEvent.CLOSE_SUBMITTED.value,
            PositionEvent.CLOSE_FAILED.value,
            PositionEvent.CLOSE_SUBMITTED.value,
            PositionEvent.CLOSE_FILLED.value,
        ]
        assert all(event["v"] == 1 for event in store.events())

    def test_recording_an_open_refuses_a_close_intent(self, tmp_path: Path) -> None:
        """Recording the CLOSE as the position would persist inverted legs."""
        intent = spread()
        store = PositionStore(tmp_path / "positions.jsonl")
        with pytest.raises(InvalidPortfolioStateError, match="takes an OPEN intent"):
            store.record_open_submitted(
                closing(intent), at=NOW, buying_power_reserved=BPR
            )


class TestReplayRobustness:
    """Documented current behaviour of hand-written or corrupted records.

    These assert what the module does today, not what it ought to do. Where the
    behaviour is a hazard it is called out in the docstring so the test does not
    read as an endorsement.
    """

    def test_to_record_and_from_record_are_inverses(self) -> None:
        """They must round-trip on their own. When they did not, a record written
        by anything other than ``record_open_submitted`` raised KeyError inside
        the replay, where it was swallowed -- and the position simply vanished
        from the book rather than failing loudly."""
        original = position()
        record = original.to_record()
        assert "entry_credit" in record

        restored = OpenPosition.from_record(record)
        assert restored.strategy_id == original.strategy_id
        assert restored.filled_credit == original.filled_credit
        assert restored.intent.limit_price == original.intent.limit_price
        assert restored.total_maximum_loss == original.total_maximum_loss
        assert [leg.con_id for leg in restored.legs] == [
            leg.con_id for leg in original.legs
        ]

    def _append_naive_close(self, path: Path, strategy_id: object) -> None:
        naive = '{"v": 1, "event": "CLOSE_FILLED", "at": "2026-07-29T14:00:00", '
        naive += f'"strategy_id": "{strategy_id}", "closing_debit": "0.40"}}'
        with path.open("a", encoding="utf-8") as handle:
            handle.write(naive + "\n")

    def test_a_malformed_transition_degrades_the_replay_it_does_not_brick_it(
        self, tmp_path: Path
    ) -> None:
        """One bad line must cost one transition, not the whole book.

        An engine that cannot read its book cannot manage the positions it
        already holds -- a worse failure than any single lost event. So the
        replay skips and keeps going, and the position stays in its last good
        state rather than disappearing.
        """
        path = tmp_path / "positions.jsonl"
        store, intent = submitted(path)
        store.record_open_filled(intent.strategy_id, at=LATER, filled_credit=D("1.50"))
        self._append_naive_close(path, intent.strategy_id)

        book = store.positions()
        assert intent.strategy_id in book
        assert book[intent.strategy_id].state is PositionState.OPEN

    def test_the_skipped_transition_is_recorded_not_silent(
        self, tmp_path: Path
    ) -> None:
        """Skipping is not free -- a dropped CLOSE_FILLED leaves a position
        looking open when it is closed. So it must be reported."""
        path = tmp_path / "positions.jsonl"
        store, intent = submitted(path)
        store.record_open_filled(intent.strategy_id, at=LATER, filled_credit=D("1.50"))
        self._append_naive_close(path, intent.strategy_id)

        problems = store.integrity_errors()
        assert len(problems) == 1
        assert "CLOSE_FILLED" in problems[0]
        assert str(intent.strategy_id) in problems[0]

    def test_an_unreadable_book_never_agrees_with_the_broker(
        self, tmp_path: Path
    ) -> None:
        """This is what stops the runner opening new risk against a book it only
        partly understands. 'The parts I could read match' is not agreement."""
        path = tmp_path / "positions.jsonl"
        store, intent = submitted(path)
        store.record_open_filled(intent.strategy_id, at=LATER, filled_credit=D("1.50"))
        self._append_naive_close(path, intent.strategy_id)

        report = store.reconcile_against_broker([("SPY", -1.0, 0.0)], checked_at=LATER)
        assert report.replay_errors
        assert report.agrees is False
        assert "UNREADABLE EVENTS" in report.describe()

    def test_a_clean_book_reports_no_integrity_errors(self, tmp_path: Path) -> None:
        """The inverse: a healthy log must not produce phantom problems, or the
        entry path would be blocked forever for no reason."""
        path = tmp_path / "positions.jsonl"
        store, intent = submitted(path)
        store.record_open_filled(intent.strategy_id, at=LATER, filled_credit=D("1.50"))
        assert store.integrity_errors() == ()


# ===========================================================================
# Exposures: what the governor aggregates
# ===========================================================================


class TestExposures:
    def test_one_exposure_per_live_position(self, tmp_path: Path) -> None:
        """This is the per-position attribution the portfolio snapshot could not
        derive before the store existed -- gap G1."""
        path = tmp_path / "positions.jsonl"
        store, first = submitted(path, spread(underlying="SPY"), at=NOW)
        store.record_open_filled(first.strategy_id, at=NOW, filled_credit=D("1.50"))
        second = spread(underlying="qqq", con_id_base=2001, quantity=2)
        store.record_open_submitted(second, at=LATER, buying_power_reserved=D("300.00"))

        exposures = store.exposures()
        assert isinstance(exposures, tuple)
        assert all(isinstance(e, PositionExposure) for e in exposures)
        assert [e.underlying for e in exposures] == ["SPY", "QQQ"]
        assert [e.strategy_id for e in exposures] == [
            first.strategy_id,
            second.strategy_id,
        ]
        assert [e.buying_power_reserved for e in exposures] == [BPR, D("300.00")]
        assert [e.maximum_loss for e in exposures] == [D("350.00"), D("700.00")]

    def test_a_closed_position_contributes_no_exposure(self, tmp_path: Path) -> None:
        """Reserving buying power against a retired structure would block real
        candidates for as long as the file lives."""
        path = tmp_path / "positions.jsonl"
        store, intent = submitted(path)
        store.record_open_filled(intent.strategy_id, at=LATER, filled_credit=D("1.50"))
        store.record_close_submitted(intent.strategy_id, at=LATER)
        store.record_close_filled(intent.strategy_id, at=LATER, closing_debit=D("0.40"))
        assert store.exposures() == ()

    def test_an_opening_position_already_counts(self, tmp_path: Path) -> None:
        """It may already be filled at the broker. Counting it only once the
        fill is confirmed is exactly the window a double-size lives in."""
        store, intent = submitted(tmp_path / "positions.jsonl")
        exposures = store.exposures()
        assert len(exposures) == 1
        assert exposures[0].strategy_id == intent.strategy_id


# ===========================================================================
# Reconciliation against the broker
# ===========================================================================


class TestReconciliation:
    def _open_store(self, path: Path, underlying: str = "SPY") -> tuple[
        PositionStore, OptionStrategyIntent
    ]:
        store, intent = submitted(path, spread(underlying=underlying))
        store.record_open_filled(intent.strategy_id, at=LATER, filled_credit=D("1.50"))
        return store, intent

    def test_agreement_reports_the_open_book_and_nothing_else(
        self, tmp_path: Path
    ) -> None:
        store, intent = self._open_store(tmp_path / "positions.jsonl")
        report = store.reconcile_against_broker(
            [("SPY", 1, 4.20)], checked_at=NOW
        )
        assert isinstance(report, ReconciliationReport)
        assert report.agrees
        assert report.known_open == (intent.strategy_id,)
        assert report.missing_at_broker == ()
        assert report.unknown_at_broker == ()
        assert report.describe().strip()
        assert "broker agrees" in report.describe()

    def test_a_position_the_broker_does_not_report_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """Either the position was closed by hand in TWS or the store is wrong.
        Both demand a human; neither may be guessed at."""
        store, intent = self._open_store(tmp_path / "positions.jsonl")
        report = store.reconcile_against_broker([], checked_at=NOW)
        assert not report.agrees
        assert report.missing_at_broker == (intent.strategy_id,)
        assert "missing at broker" in report.describe()

    def test_a_broker_symbol_the_store_does_not_know_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """Adopting it would mean inventing an entry credit and a maximum loss
        that nothing ever validated."""
        store, _ = self._open_store(tmp_path / "positions.jsonl")
        report = store.reconcile_against_broker(
            [("SPY", 1, 4.20), ("qqq", -1, 2.10)], checked_at=NOW
        )
        assert not report.agrees
        assert report.unknown_at_broker == ("QQQ",)
        assert "unknown structures at broker" in report.describe()

    def test_an_opening_position_is_reported_as_stranded(self, tmp_path: Path) -> None:
        """Exactly the state a crash between record and transmit leaves behind."""
        store, intent = submitted(tmp_path / "positions.jsonl")
        report = store.reconcile_against_broker([("SPY", 1, 4.20)], checked_at=NOW)
        assert not report.agrees
        assert report.stranded_opening == (intent.strategy_id,)
        assert report.stranded_closing == ()
        assert "stranded OPENING" in report.describe()

    def test_a_closing_position_is_reported_as_stranded(self, tmp_path: Path) -> None:
        store, intent = self._open_store(tmp_path / "positions.jsonl")
        store.record_close_submitted(intent.strategy_id, at=LATER)
        report = store.reconcile_against_broker([("SPY", 1, 4.20)], checked_at=NOW)
        assert not report.agrees
        assert report.stranded_closing == (intent.strategy_id,)
        assert report.stranded_opening == ()
        assert "stranded CLOSING" in report.describe()

    def test_agrees_is_true_only_when_all_four_differences_are_empty(self) -> None:
        """One populated field must be enough to withhold agreement, whichever
        it is."""
        assert ReconciliationReport(checked_at=NOW).agrees
        for field in (
            "stranded_opening",
            "stranded_closing",
            "missing_at_broker",
        ):
            report = ReconciliationReport(checked_at=NOW, **{field: (uuid4(),)})
            assert not report.agrees, field
        assert not ReconciliationReport(
            checked_at=NOW, unknown_at_broker=("SPY",)
        ).agrees

    def test_an_empty_book_and_an_empty_broker_agree(self, tmp_path: Path) -> None:
        store = PositionStore(tmp_path / "positions.jsonl")
        report = store.reconcile_against_broker(None, checked_at=NOW)
        assert report.agrees
        assert report.known_open == ()

    def test_the_report_records_itself_as_json_shaped_data(
        self, tmp_path: Path
    ) -> None:
        store, intent = self._open_store(tmp_path / "positions.jsonl")
        record = store.reconcile_against_broker([], checked_at=NOW).to_record()
        assert record["event"] == "position_reconciliation"
        assert record["agrees"] is False
        assert record["missing_at_broker"] == [str(intent.strategy_id)]


# ===========================================================================
# A write that cannot be made durable is fatal
# ===========================================================================


class TestWriteFailureIsFatal:
    def test_an_unwritable_path_raises_journal_error(self, tmp_path: Path) -> None:
        """The engine must not open a position it cannot record. An unrecorded
        spread still expires."""
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("this is a file, not a directory\n", encoding="utf-8")
        store = PositionStore(blocker / "positions.jsonl")
        with pytest.raises(JournalError, match="cannot write the position store"):
            store.record_open_submitted(spread(), at=NOW, buying_power_reserved=BPR)

    def test_every_writer_fails_the_same_way(self, tmp_path: Path) -> None:
        """Only the open path is written before transmission, but a close that
        cannot be recorded is equally unsafe to send."""
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("this is a file, not a directory\n", encoding="utf-8")
        store = PositionStore(blocker / "positions.jsonl")
        strategy_id = uuid4()
        with pytest.raises(JournalError):
            store.record_open_filled(strategy_id, at=NOW, filled_credit=D("1.55"))
        with pytest.raises(JournalError):
            store.record_open_failed(strategy_id, at=NOW, reason="no fill")
        with pytest.raises(JournalError):
            store.record_close_submitted(strategy_id, at=NOW)
        with pytest.raises(JournalError):
            store.record_close_filled(strategy_id, at=NOW, closing_debit=D("0.40"))
        with pytest.raises(JournalError):
            store.record_close_failed(strategy_id, at=NOW, reason="no fill")
        with pytest.raises(JournalError):
            store.record_rolled(strategy_id, into=uuid4(), at=NOW)


class TestReconciliationOutcomeClassification:
    """Turning a report into the one thing the entry gate reads.

    The runner used to hold this as ``ReconciliationReport | None`` and treat the
    ``None`` -- the broker could not be asked at all -- as though it were an
    answer of "you hold nothing". These tests pin the replacement: four named
    outcomes, exactly one of which opens risk.
    """

    def test_a_matched_report_reconciles(self) -> None:
        report = ReconciliationReport(checked_at=NOW)
        assert report.agrees is True
        assert ReconciliationOutcome.for_report(report) is ReconciliationOutcome.RECONCILED

    def test_a_mismatch_is_a_disagreement(self) -> None:
        report = ReconciliationReport(checked_at=NOW, missing_at_broker=(uuid4(),))
        assert ReconciliationOutcome.for_report(report) is ReconciliationOutcome.DISAGREEMENT

    def test_replay_errors_outrank_the_comparison(self) -> None:
        """CORRUPT, not DISAGREEMENT. Both refuse an entry, but only one of them
        sends the operator to the log on disk rather than to TWS -- and a book
        that could not be replayed is not a book whose agreement means anything.
        """
        report = ReconciliationReport(
            checked_at=NOW, replay_errors=("unreadable CLOSE_FILLED",)
        )
        assert ReconciliationOutcome.for_report(report) is ReconciliationOutcome.CORRUPT

    def test_replay_errors_win_even_beside_a_real_mismatch(self) -> None:
        report = ReconciliationReport(
            checked_at=NOW,
            replay_errors=("unreadable CLOSE_FILLED",),
            missing_at_broker=(uuid4(),),
        )
        assert ReconciliationOutcome.for_report(report) is ReconciliationOutcome.CORRUPT

    def test_exactly_one_outcome_may_open_new_risk(self) -> None:
        """Enumerated over the enum, so a fifth state has to be classified
        deliberately instead of defaulting into whichever branch it falls in."""
        opening = [o for o in ReconciliationOutcome if o.may_open_new_risk]
        assert opening == [ReconciliationOutcome.RECONCILED]

    def test_an_unavailable_reconciliation_is_never_produced_from_a_report(
        self,
    ) -> None:
        """UNAVAILABLE means the broker was never successfully asked, so no
        report exists to classify. If this classifier could ever return it, the
        two meanings would have merged again."""
        produced = {
            ReconciliationOutcome.for_report(r)
            for r in (
                ReconciliationReport(checked_at=NOW),
                ReconciliationReport(checked_at=NOW, missing_at_broker=(uuid4(),)),
                ReconciliationReport(checked_at=NOW, replay_errors=("bad line",)),
            )
        }
        assert ReconciliationOutcome.UNAVAILABLE not in produced
