"""The capability probe: outcome classification, and the no-transmit guarantee.

The probe's live behaviour is verified by running it against TWS. What is
verified here is everything that can be established without a broker: that it
classifies observations correctly, that it reports greeks presence separately
from delta validity, and that no code path in it can place an order.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from engine.cli import build_parser
from engine.options.marketdata import GREEK_SENTINEL, OptionGreeks
from engine.options.probe import (
    CallbackRecorder,
    ContractObservation,
    ProbeOutcome,
    ProbeReport,
    classify,
)

NOW = datetime(2026, 7, 29, 9, 15, tzinfo=timezone.utc)


def observation(
    *,
    kind: str = "option",
    con_id: int = 1001,
    callbacks: tuple[int, ...] = (3,),
    bid: float | None = None,
    ask: float | None = None,
    delta: str | None = None,
    greeks_present: bool = False,
) -> ContractObservation:
    obs = ContractObservation(label=f"c{con_id}", kind=kind, con_id=con_id)
    obs.data_type_callbacks = list(callbacks)
    obs.bid = bid
    obs.ask = ask
    if greeks_present or delta is not None:
        obs.greeks = OptionGreeks(
            received_at=NOW,
            subscription_generation=uuid4(),
            delta=Decimal(delta) if delta is not None else None,
        )
    return obs


# ===========================================================================
# The five outcome states
# ===========================================================================


class TestClassification:
    def test_valid_delta_means_greeks_are_available(self) -> None:
        outcome = classify(
            observation(kind="underlying", con_id=1),
            [observation(delta="-0.16", bid=1.4, ask=1.6)],
        )
        assert outcome is ProbeOutcome.DELAYED_GREEKS_AVAILABLE

    def test_prices_without_delta_is_quotes_only(self) -> None:
        outcome = classify(
            observation(kind="underlying", con_id=1),
            [observation(bid=1.4, ask=1.6)],
        )
        assert outcome is ProbeOutcome.DELAYED_QUOTES_ONLY

    def test_greeks_object_without_delta_is_still_quotes_only(self) -> None:
        """The exact ib_async trap: the computation is assigned even when every
        field sanitizes away, so its presence must not count as a greek."""
        outcome = classify(
            observation(kind="underlying", con_id=1),
            [observation(bid=1.4, ask=1.6, greeks_present=True)],
        )
        assert outcome is ProbeOutcome.DELAYED_QUOTES_ONLY

    def test_callbacks_but_no_data_is_no_delayed_option_data(self) -> None:
        outcome = classify(
            observation(kind="underlying", con_id=1),
            [observation()],
        )
        assert outcome is ProbeOutcome.NO_DELAYED_OPTION_DATA

    def test_silence_is_unknown_not_absence(self) -> None:
        """'We heard nothing' and 'there is nothing' are different findings.
        Reporting the first as the second would blame the broker for what may
        be an unsigned API acknowledgement."""
        outcome = classify(
            observation(kind="underlying", con_id=1, callbacks=()),
            [observation(callbacks=())],
        )
        assert outcome is ProbeOutcome.UNKNOWN_CALLBACK_STATE

    def test_underlying_callback_alone_still_counts_as_heard_from(self) -> None:
        outcome = classify(
            observation(kind="underlying", con_id=1, callbacks=(3,)),
            [observation(callbacks=())],
        )
        assert outcome is ProbeOutcome.NO_DELAYED_OPTION_DATA

    def test_fatal_error_wins_over_everything(self) -> None:
        outcome = classify(
            observation(kind="underlying", con_id=1),
            [observation(delta="-0.16")],
            fatal_error=True,
        )
        assert outcome is ProbeOutcome.PROBE_ERROR

    def test_no_option_contracts_is_an_error_not_a_finding(self) -> None:
        assert classify(observation(kind="underlying", con_id=1), []) is ProbeOutcome.PROBE_ERROR

    def test_one_valid_delta_among_several_is_enough(self) -> None:
        outcome = classify(
            observation(kind="underlying", con_id=1),
            [observation(con_id=2), observation(con_id=3, delta="0.16")],
        )
        assert outcome is ProbeOutcome.DELAYED_GREEKS_AVAILABLE


class TestObservation:
    def test_callback_received_is_driven_by_the_server_not_the_field(self) -> None:
        assert not observation(callbacks=()).callback_received
        assert observation(callbacks=(3,)).callback_received

    def test_reported_type_is_the_latest_callback(self) -> None:
        assert observation(callbacks=(1, 3)).reported_type == 3

    def test_reported_type_is_none_when_silent(self) -> None:
        assert observation(callbacks=()).reported_type is None

    def test_delta_validity_is_separate_from_greeks_presence(self) -> None:
        obs = observation(greeks_present=True)
        assert obs.greeks is not None
        assert not obs.has_valid_delta

    def test_sentinels_do_not_survive_into_the_record(self) -> None:
        obs = ContractObservation(label="x", kind="option", con_id=1)
        obs.greeks = OptionGreeks.from_ib(
            type("C", (), {"delta": -0.16, "theta": GREEK_SENTINEL, "vega": GREEK_SENTINEL})(),
            received_at=NOW,
            subscription_generation=uuid4(),
        )
        record = obs.to_record()
        assert record["delta"] == "-0.16"
        assert record["theta"] is None
        assert record["vega"] is None
        assert record["delta_valid"] is True


class TestReport:
    def test_record_is_json_safe(self) -> None:
        import json

        report = ProbeReport(
            outcome=ProbeOutcome.DELAYED_QUOTES_ONLY,
            requested_type=3,
            account="DU1234567",
            started_at=NOW,
            finished_at=NOW,
            underlying=observation(kind="underlying", con_id=1, bid=500.0, ask=500.2),
            options=[observation(bid=1.4, ask=1.6)],
        )
        encoded = json.dumps(report.to_record())
        assert "DELAYED_QUOTES_ONLY" in encoded

    def test_describe_names_the_outcome(self) -> None:
        report = ProbeReport(
            outcome=ProbeOutcome.UNKNOWN_CALLBACK_STATE,
            requested_type=3,
            account="DU1234567",
            started_at=NOW,
            options=[observation(callbacks=())],
        )
        assert "UNKNOWN_CALLBACK_STATE" in report.describe()

    def test_a_silent_contract_is_shown_as_NONE_not_as_a_number(self) -> None:
        report = ProbeReport(
            outcome=ProbeOutcome.UNKNOWN_CALLBACK_STATE,
            requested_type=3,
            account="DU1234567",
            started_at=NOW,
            options=[observation(callbacks=())],
        )
        assert "reported=NONE" in report.describe()


# ===========================================================================
# The no-transmit guarantee, asserted mechanically
# ===========================================================================


TRANSMITTING_NAMES = {
    "placeOrder",
    "place",
    "submit",
    "transmit",
    "cancelOrder",
    "reqGlobalCancel",
}


class TestNoTransmit:
    def test_the_probe_module_contains_no_order_transmission_call(self) -> None:
        """A comment promising this would not survive an edit. Parsing the
        module's own AST does."""
        import engine.options.probe as probe_module

        source = Path(inspect.getsourcefile(probe_module)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    called.add(func.attr)
                elif isinstance(func, ast.Name):
                    called.add(func.id)
        offending = called & TRANSMITTING_NAMES
        assert not offending, f"probe must not transmit; found {sorted(offending)}"

    def test_the_probe_module_never_imports_the_safety_order_intent(self) -> None:
        """It has no business constructing an order at all."""
        import engine.options.probe as probe_module

        source = Path(inspect.getsourcefile(probe_module)).read_text(encoding="utf-8")
        assert "OrderIntent" not in source


class TestCallbackRecorderLifecycle:
    def test_install_and_remove_restore_the_original_methods(self) -> None:
        """The wrappers must not outlive the probe -- they are installed on a
        shared ib_async wrapper object."""

        class FakeWrapper:
            reqId2Ticker: dict = {}

            def marketDataType(self, req_id, market_data_id):  # noqa: N802
                return ("mdt", req_id, market_data_id)

            def tickOptionComputation(self, req_id, tick_type, *args):  # noqa: N802
                return ("toc", req_id, tick_type)

        class FakeIB:
            def __init__(self) -> None:
                self.wrapper = FakeWrapper()

        ib = FakeIB()
        original_mdt = ib.wrapper.marketDataType
        original_toc = ib.wrapper.tickOptionComputation

        recorder = CallbackRecorder(ib)
        recorder.install()
        assert ib.wrapper.marketDataType is not original_mdt
        assert ib.wrapper.tickOptionComputation is not original_toc

        recorder.remove()
        assert ib.wrapper.marketDataType == original_mdt
        assert ib.wrapper.tickOptionComputation == original_toc

    def test_recorder_delegates_and_records(self) -> None:
        seen: list[tuple] = []

        class FakeTicker:
            contract = type("C", (), {"conId": 4242})()
            modelGreeks = None

        class FakeWrapper:
            def __init__(self) -> None:
                self.reqId2Ticker = {7: FakeTicker()}

            def marketDataType(self, req_id, market_data_id):  # noqa: N802
                seen.append(("mdt", req_id, market_data_id))

            def tickOptionComputation(self, req_id, tick_type, *args):  # noqa: N802
                seen.append(("toc", req_id, tick_type))

        class FakeIB:
            def __init__(self) -> None:
                self.wrapper = FakeWrapper()

        ib = FakeIB()
        recorder = CallbackRecorder(ib)
        recorder.install()
        try:
            ib.wrapper.marketDataType(7, 3)
            ib.wrapper.tickOptionComputation(7, 83, 0.2, -0.16)
        finally:
            recorder.remove()

        assert recorder.data_types[4242] == [3]
        assert recorder.greek_counts[4242] == 1
        # The original implementations must still have run.
        assert seen == [("mdt", 7, 3), ("toc", 7, 83)]

    def test_unmappable_callbacks_are_counted_not_silently_dropped(self) -> None:
        class FakeWrapper:
            reqId2Ticker: dict = {}

            def marketDataType(self, req_id, market_data_id):  # noqa: N802
                return None

            def tickOptionComputation(self, req_id, tick_type, *args):  # noqa: N802
                return None

        class FakeIB:
            def __init__(self) -> None:
                self.wrapper = FakeWrapper()

        ib = FakeIB()
        recorder = CallbackRecorder(ib)
        recorder.install()
        try:
            ib.wrapper.marketDataType(99, 3)
        finally:
            recorder.remove()
        assert recorder.unmapped_events == 1
        assert recorder.data_types == {}


class TestCommandWiring:
    def test_the_probe_subcommand_is_registered(self) -> None:
        args = build_parser().parse_args(["probe-options-data"])
        assert args.command == "probe-options-data"
        assert args.market_data_type == 3, "delayed is the default"

    def test_the_data_type_is_restricted_to_the_four_ibkr_values(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["probe-options-data", "--market-data-type", "9"])

    def test_the_probe_has_no_arm_flag(self) -> None:
        """There must be no way to ask this command to transmit."""
        with pytest.raises(SystemExit):
            build_parser().parse_args(["probe-options-data", "--arm"])
