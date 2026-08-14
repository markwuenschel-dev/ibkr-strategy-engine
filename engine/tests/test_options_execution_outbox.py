"""Focused R6 evidence: durable sends, ambiguity, identity, and caps."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from engine.errors import RefusedError
from engine.journal import OrderJournal
from engine.options.broker_reconciliation import (
    BrokerMatchClassification,
    BrokerOrderIdentity,
    BrokerReconciler,
)
from engine.options.order_outbox import (
    FAIL_BROKER_AMBIGUOUS,
    FAIL_REPRICE_BUDGET,
    ExecutionOutbox,
    OutboxState,
    TransmissionBudget,
)
from engine.options.transmit import authorize_open, place_combo

from test_options_order_control import (
    NOW,
    approving_governor,
    approving_risk,
    gate_for,
    spread,
)
from test_options_transmit import RecordingIB, review_for
from test_options_runner import FakeBroker, FakeIB, FakeMarketDataPort, gate_for as runner_gate_for, store_for, run_pass


def _authorized_with_durability(tmp_path: Path):
    intent = spread()
    gate = gate_for(tmp_path)
    risk = approving_risk(intent.strategy_id)
    governor = approving_governor(intent)
    verifier, packet = review_for(tmp_path, intent, risk=risk, governor=governor)
    outbox = ExecutionOutbox(tmp_path / "outbox")
    budget = TransmissionBudget(
        tmp_path / "budget.json",
        limit=5,
        journal=gate.journal,
        now=NOW,
    )
    authorization = authorize_open(
        intent,
        gate=gate,
        risk=risk,
        governor=governor,
        armed=True,
        now=NOW,
        verifier=verifier,
        packet=packet,
        execution_outbox=outbox,
        transmission_budget=budget,
        account="DU1234567",
    )
    return intent, gate, authorization, outbox, budget


class TestExecutionOutbox:
    def test_intent_and_approval_receipts_precede_submission(self, tmp_path: Path) -> None:
        intent, _gate, authorization, outbox, _budget = _authorized_with_durability(tmp_path)

        before = outbox.records()[0]
        assert before["state"] == OutboxState.APPROVAL_CONSUMED.value
        events = [
            line["event"]
            for line in (
                __import__("json").loads(raw)
                for raw in (outbox.receipts_path.read_text(encoding="utf-8").splitlines())
            )
        ]
        assert events[:2] == ["PHYSICAL_SEND_INTENT", "REVIEW_APPROVAL_CONSUMED"]

        result = place_combo(
            RecordingIB(),
            intent,
            authorization=authorization,
            account="DU1234567",
        )
        assert result.transmitted is True
        final = outbox.records()[0]
        assert final["state"] == OutboxState.OUTCOME.value
        assert final["classification"] == "FILLED"
        assert "BROKER_SUBMISSION_OBSERVED" in events + [
            __import__("json").loads(raw)["event"]
            for raw in outbox.receipts_path.read_text(encoding="utf-8").splitlines()
        ]
        assert authorization.execution_attempt_id == final["attempt_id"]

    def test_broker_exception_is_quarantined_not_retried(self, tmp_path: Path) -> None:
        intent, gate, authorization, outbox, _budget = _authorized_with_durability(tmp_path)

        class RaisesAfterDoor(RecordingIB):
            def placeOrder(self, _contract: Any, _order: Any) -> Any:  # noqa: N802
                raise RuntimeError("socket closed after broker accepted")

        result = place_combo(
            RaisesAfterDoor(),
            intent,
            authorization=authorization,
            account="DU1234567",
        )
        assert result.transmitted is False
        assert outbox.records()[0]["state"] == OutboxState.AMBIGUOUS.value
        with pytest.raises(RefusedError, match=FAIL_BROKER_AMBIGUOUS):
            outbox.assert_clear()

    def test_reconciliation_clears_only_an_explicit_terminal_answer(
        self, tmp_path: Path
    ) -> None:
        intent, _gate, _authorization, outbox, _budget = _authorized_with_durability(tmp_path)
        attempt = outbox.records()[0]["attempt_id"]
        outbox.quarantine(attempt, reason="process died after approval")
        with pytest.raises(RefusedError, match=FAIL_BROKER_AMBIGUOUS):
            outbox.assert_clear()
        outbox.reconcile(
            attempt,
            result="ABSENT_CONFIRMED",
            evidence={"account": "DU1234567", "query_complete": True},
        )
        outbox.assert_clear()


class TestBrokerIdentity:
    def test_working_partial_and_mismatch_are_not_collapsed(self) -> None:
        intent = spread()
        expected = BrokerOrderIdentity.from_intent(intent, account="DU1234567")
        legs = [
            {"conId": leg.con_id, "action": leg.action.value, "ratio": leg.ratio, "exchange": leg.exchange}
            for leg in intent.legs
        ]
        working = {
            "orderRef": str(intent.strategy_id),
            "account": "DU1234567",
            "comboLegs": legs,
            "orderStatus": {"status": "Submitted", "filled": 0, "remaining": 1},
        }
        partial = {
            **working,
            "orderStatus": {"status": "Submitted", "filled": 1, "remaining": 1},
        }
        wrong_account = {**working, "account": "DU9999999"}
        reconciler = BrokerReconciler(now=NOW)
        assert reconciler.reconcile(expected, [working]).classification is BrokerMatchClassification.WORKING
        assert reconciler.reconcile(expected, [partial]).classification is BrokerMatchClassification.PARTIAL
        assert reconciler.reconcile(expected, [wrong_account]).classification is BrokerMatchClassification.ABSENT_CONFIRMED
        assert reconciler.reconcile(expected, None).classification is BrokerMatchClassification.UNKNOWN

    def test_missing_remaining_quantity_does_not_promote_a_partial_fill(self) -> None:
        intent = spread(quantity=2)
        expected = BrokerOrderIdentity.from_intent(intent, account="DU1234567")
        candidate = {
            "orderRef": str(intent.strategy_id),
            "account": "DU1234567",
            "comboLegs": [
                {"conId": leg.con_id, "action": leg.action.value, "ratio": leg.ratio, "exchange": leg.exchange}
                for leg in intent.legs
            ],
            "orderStatus": {"status": "Filled", "filled": 1},
        }
        result = BrokerReconciler(now=NOW).reconcile(expected, [candidate], expected_quantity=2)
        assert result.classification is BrokerMatchClassification.PARTIAL


class TestTransmissionBudget:
    def test_reserved_and_committed_rungs_consume_the_same_cap(self, tmp_path: Path) -> None:
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        journal = OrderJournal(tmp_path / "orders.jsonl")
        budget = TransmissionBudget(
            tmp_path / "budget.json", limit=2, journal=journal, now=now
        )
        first = budget.reserve(intent_id := spread().strategy_id, now=now)
        budget.commit(first.reservation_id)
        journal.record("order_placed", strategy_id=str(intent_id))
        second = budget.reserve(spread().strategy_id, now=now)
        budget.commit(second.reservation_id)
        journal.record("order_placed", strategy_id="second")
        with pytest.raises(RefusedError, match=FAIL_REPRICE_BUDGET):
            budget.reserve(spread().strategy_id, now=now)

    def test_armed_full_runner_without_a_lease_is_refused_before_broker_work(
        self, tmp_path: Path
    ) -> None:
        ib = FakeIB()
        report = run_pass(
            FakeBroker(ib=ib),
            runner_gate_for(tmp_path),
            store_for(tmp_path),
            armed=True,
            session_lease=None,
        )
        assert report.refusal_codes == ["FAIL-LEASE-MISSING"]
        assert ib.placed == []
