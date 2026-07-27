"""Per-order gates. Every test here asserts something is refused."""

from __future__ import annotations

import pytest

from engine.config import EngineConfig
from engine.errors import HaltedError, RefusedError
from engine.journal import OrderJournal
from engine.safety import BUY, SELL, OrderIntent, SafetyGate


def intent(symbol: str = "SPY", qty: int = 1, side: str = BUY, limit: float | None = None):
    return OrderIntent(symbol=symbol, quantity=qty, side=side, limit_price=limit)


class TestKillSwitch:
    def test_a_present_halt_file_stops_everything(
        self, gate: SafetyGate, config: EngineConfig
    ) -> None:
        config.halt_file.parent.mkdir(parents=True, exist_ok=True)
        config.halt_file.write_text("market looks weird", encoding="utf-8")
        with pytest.raises(HaltedError) as caught:
            gate.check(intent(), armed=True, reference_price=100.0)
        # The reason is echoed back so whoever finds it knows why it was set.
        assert "market looks weird" in str(caught.value)

    def test_the_kill_switch_also_blocks_a_preview(
        self, gate: SafetyGate, config: EngineConfig
    ) -> None:
        # A halted engine should not be talking to the broker at all.
        config.halt_file.write_text("stop", encoding="utf-8")
        with pytest.raises(HaltedError):
            gate.check_preview(intent())

    def test_an_empty_halt_file_still_halts(
        self, gate: SafetyGate, config: EngineConfig
    ) -> None:
        # `touch HALT` from a phone must work; requiring a reason would be a
        # footgun in exactly the moment you need it.
        config.halt_file.write_text("", encoding="utf-8")
        with pytest.raises(HaltedError):
            gate.check(intent(), armed=True, reference_price=100.0)

    def test_a_halted_engine_resumes_when_the_file_goes(
        self, gate: SafetyGate, config: EngineConfig
    ) -> None:
        config.halt_file.write_text("x", encoding="utf-8")
        with pytest.raises(HaltedError):
            gate.assert_not_halted()
        config.halt_file.unlink()
        gate.assert_not_halted()


class TestArming:
    def test_an_unarmed_order_is_refused(self, gate: SafetyGate) -> None:
        with pytest.raises(RefusedError) as caught:
            gate.check(intent(), armed=False, reference_price=100.0)
        assert "not armed" in str(caught.value)

    def test_arming_is_required_even_when_everything_else_passes(
        self, gate: SafetyGate
    ) -> None:
        gate.check(intent(), armed=True, reference_price=100.0)  # passes
        with pytest.raises(RefusedError):
            gate.check(intent(), armed=False, reference_price=100.0)


class TestSymbolAndQuantity:
    def test_a_symbol_outside_the_allowlist_is_refused(self, gate: SafetyGate) -> None:
        with pytest.raises(RefusedError) as caught:
            gate.check(intent(symbol="GME"), armed=True, reference_price=10.0)
        assert "allowlist" in str(caught.value)

    def test_the_symbol_is_matched_case_insensitively(self, gate: SafetyGate) -> None:
        checked = gate.check(intent(symbol="spy"), armed=True, reference_price=100.0)
        assert checked.symbol == "SPY"

    @pytest.mark.parametrize("qty", [0, -1, -100])
    def test_a_non_positive_quantity_is_refused(self, gate: SafetyGate, qty: int) -> None:
        with pytest.raises(RefusedError):
            gate.check(intent(qty=qty), armed=True, reference_price=100.0)

    def test_a_boolean_quantity_is_refused(self, gate: SafetyGate) -> None:
        # bool is an int subclass; True would otherwise mean "buy 1 share".
        with pytest.raises(RefusedError):
            gate.check(intent(qty=True), armed=True, reference_price=100.0)  # type: ignore[arg-type]

    def test_an_unknown_side_is_refused(self, gate: SafetyGate) -> None:
        with pytest.raises(RefusedError):
            gate.check(intent(side="HOLD"), armed=True, reference_price=100.0)


class TestPositionCap:
    def test_the_cap_applies_to_the_resulting_position_not_the_order(
        self, gate: SafetyGate
    ) -> None:
        # Ten 1-share orders reach the same place as one 10-share order.
        gate.check(intent(qty=1), armed=True, reference_price=10.0, current_qty=9)
        with pytest.raises(RefusedError) as caught:
            gate.check(intent(qty=1), armed=True, reference_price=10.0, current_qty=10)
        assert "position would become 11" in str(caught.value)

    def test_selling_reduces_the_resulting_position(self, gate: SafetyGate) -> None:
        gate.check(
            intent(qty=5, side=SELL), armed=True, reference_price=10.0, current_qty=10
        )

    def test_a_short_position_is_capped_by_magnitude(self, gate: SafetyGate) -> None:
        with pytest.raises(RefusedError):
            gate.check(
                intent(qty=5, side=SELL), armed=True, reference_price=10.0, current_qty=-8
            )


class TestNotionalCap:
    def test_an_order_over_the_notional_cap_is_refused(self, gate: SafetyGate) -> None:
        with pytest.raises(RefusedError) as caught:
            gate.check(intent(qty=2), armed=True, reference_price=600.0)
        assert "notional" in str(caught.value)

    def test_a_missing_price_is_a_refusal_not_a_pass(self, gate: SafetyGate) -> None:
        # The engine must not place an order whose value it cannot bound.
        with pytest.raises(RefusedError) as caught:
            gate.check(intent(), armed=True, reference_price=None)
        assert "no reference price" in str(caught.value)

    @pytest.mark.parametrize("price", [0.0, -5.0])
    def test_a_nonsensical_price_is_refused(self, gate: SafetyGate, price: float) -> None:
        with pytest.raises(RefusedError):
            gate.check(intent(), armed=True, reference_price=price)


class TestDailyOrderCap:
    def test_the_cap_counts_orders_from_the_journal(
        self, gate: SafetyGate, journal: OrderJournal, config: EngineConfig
    ) -> None:
        for _ in range(config.max_orders_per_session):
            journal.record("order_placed", symbol="SPY", quantity=1)
        with pytest.raises(RefusedError) as caught:
            gate.check(intent(), armed=True, reference_price=100.0)
        assert "already placed today" in str(caught.value)

    def test_the_cap_survives_a_restart(
        self, gate: SafetyGate, journal: OrderJournal, config: EngineConfig
    ) -> None:
        # This is the whole reason the count comes from disk: a crash-looping
        # engine restarts its memory but not its day.
        for _ in range(config.max_orders_per_session):
            journal.record("order_placed", symbol="SPY", quantity=1)
        fresh = SafetyGate(config, OrderJournal(config.journal_path))
        with pytest.raises(RefusedError):
            fresh.check(intent(), armed=True, reference_price=100.0)

    def test_previews_do_not_count_towards_the_cap(
        self, gate: SafetyGate, journal: OrderJournal, config: EngineConfig
    ) -> None:
        for _ in range(config.max_orders_per_session * 3):
            journal.record("preview", symbol="SPY", quantity=1)
        gate.check(intent(), armed=True, reference_price=100.0)


class TestMarginGate:
    def test_an_over_cap_margin_impact_is_refused(self, gate: SafetyGate) -> None:
        with pytest.raises(RefusedError):
            gate.gate_margin(margin_impact=5_000.01)

    def test_an_unknown_margin_impact_is_refused_not_assumed_small(
        self, gate: SafetyGate
    ) -> None:
        with pytest.raises(RefusedError) as caught:
            gate.gate_margin(margin_impact=None)
        assert "no margin impact" in str(caught.value)

    def test_a_within_cap_impact_passes(self, gate: SafetyGate) -> None:
        gate.gate_margin(margin_impact=100.0)


class TestOrdering:
    def test_the_kill_switch_is_checked_before_arming(
        self, gate: SafetyGate, config: EngineConfig
    ) -> None:
        # A halted engine reports "halted", not "not armed" -- the more
        # fundamental refusal should be the one you see.
        config.halt_file.write_text("halted", encoding="utf-8")
        with pytest.raises(HaltedError):
            gate.check(intent(), armed=False, reference_price=100.0)
