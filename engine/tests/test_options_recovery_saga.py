"""Focused R2 proof for approval/logical recovery and the execution outbox."""

from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from engine.errors import RefusedError
from engine.options.approval import ApprovalDecision, render_response
from engine.options.execution_outbox import (
    ExecutionOutbox,
    OutboxState,
    ReceiptConflict,
    ReceiptJournal,
    ReceiptKind,
    authorize_after_intent,
)
from engine.options.logical import (
    LogicalEntryConflict,
    LogicalEntryState,
    ServiceOutcome,
)
from test_options_logical import Harness, LATER, NOW, nomination


class TestApprovalRecovery:
    def test_approved_entry_rebuilds_and_supersedes_when_facts_change(
        self, tmp_path: Path
    ) -> None:
        harness = Harness(tmp_path)
        entry = harness.awaiting()
        harness.reviewer.work(NOW)

        approved = harness.manager.service(entry, harness.packet_for(entry), now=LATER)
        assert approved.outcome is ServiceOutcome.APPROVED

        changed = harness.packet_for(approved.entry, credit="1.25", now=LATER)
        result = harness.manager.service(approved.entry, changed, now=LATER)

        assert result.outcome is ServiceOutcome.SUPERSEDED
        assert result.entry.state is LogicalEntryState.AWAITING_REVIEW
        assert result.entry.proposal_revision == 2
        assert harness.consumed() == []

    def test_approved_entry_expires_independently_of_review_sweep(
        self, tmp_path: Path
    ) -> None:
        harness = Harness(tmp_path)
        entry = harness.awaiting()
        harness.reviewer.work(NOW)
        approved = harness.manager.service(entry, harness.packet_for(entry), now=LATER)
        assert approved.entry.current_approval_expires_at is not None

        swept = harness.manager.sweep(
            now=approved.entry.current_approval_expires_at + dt.timedelta(seconds=1)
        )
        assert len(swept) == 1
        assert swept[0].outcome is ServiceOutcome.EXPIRED
        assert swept[0].entry.state is LogicalEntryState.EXPIRED
        assert swept[0].entry.reservation_id is None

    def test_packet_rejects_non_positive_and_overlong_ttl(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        entry = harness.manager.claim(nomination(), now=NOW)
        packet = harness.packet_for(entry)

        with pytest.raises(RefusedError, match="non-positive packet TTL"):
            dataclasses.replace(packet, expires_at=packet.proposed_at)
        with pytest.raises(RefusedError, match="exceeds"):
            dataclasses.replace(
                packet,
                expires_at=packet.proposed_at + dt.timedelta(hours=13),
            )

    def test_handoff_marker_recovery_is_by_full_digest(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        entry = harness.manager.claim(nomination(), now=NOW)
        packet = harness.packet_for(entry)
        request_id = harness.gate.propose(packet, now=NOW)
        marker = harness.gate._request_marker(packet.spec)
        marker.unlink()

        recovered = harness.gate.propose(packet, now=LATER)

        assert recovered == request_id
        assert marker.read_text(encoding="utf-8").strip() == request_id

    def test_approval_receipts_are_durable_and_idempotent(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        entry = harness.awaiting()
        harness.reviewer.work(NOW)
        result = harness.manager.service(entry, harness.packet_for(entry), now=LATER)
        assert result.approval is not None
        harness.gate.consume(result.approval, now=LATER)

        records = ReceiptJournal(tmp_path / "state" / "receipts.jsonl").records()
        kinds = [record["kind"] for record in records]
        assert ReceiptKind.LOGICAL_ENTRY_CLAIMED.value in kinds
        assert ReceiptKind.REVIEW_REQUEST_INTENT.value in kinds
        assert ReceiptKind.REVIEW_REQUEST_FILED.value in kinds
        assert ReceiptKind.REVIEW_APPROVAL_CONSUMED.value in kinds

    def test_final_door_recheck_refuses_after_approval_expiry(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        entry = harness.awaiting()
        harness.reviewer.work(NOW)
        result = harness.manager.service(entry, harness.packet_for(entry), now=LATER)
        assert result.approval is not None

        with pytest.raises(RefusedError, match="packet expired|failed final-door"):
            harness.gate.recheck(
                harness.packet_for(entry),
                result.approval,
                now=result.approval.expires_at + dt.timedelta(seconds=1),
            )

    def test_render_response_refuses_naive_approval_clock(self) -> None:
        with pytest.raises(RefusedError, match="timezone-aware"):
            render_response(
                decision=ApprovalDecision.APPROVED,
                request_id="request",
                intent_id=uuid4(),
                spec_digest="0" * 64,
                approved_at=dt.datetime(2026, 8, 1, 10, 0),
                expires_at=dt.datetime(2026, 8, 1, 11, 0),
            )


class TestExecutionOutbox:
    def test_intent_then_consumption_is_durable_and_restart_visible(
        self, tmp_path: Path
    ) -> None:
        outbox = ExecutionOutbox(tmp_path / "state" / "outbox.jsonl")
        intent = outbox.prepare(
            logical_entry_id="entry-1",
            proposal_revision=1,
            approval_id="approval-1",
            request_id="request-1",
            spec_digest="a" * 64,
            packet_digest="b" * 64,
            at=NOW,
        )
        assert intent.state is OutboxState.SEND_INTENT
        consumed = outbox.record_approval_consumed(intent, at=NOW)
        assert consumed.state is OutboxState.APPROVAL_CONSUMED
        assert outbox.record_approval_consumed(intent, at=LATER) == consumed
        assert ExecutionOutbox(tmp_path / "state" / "outbox.jsonl").unresolved()

    def test_ambiguous_intent_is_quarantined_and_never_replayed(
        self, tmp_path: Path
    ) -> None:
        outbox = ExecutionOutbox(tmp_path / "state" / "outbox.jsonl")
        intent = outbox.prepare(
            logical_entry_id="entry-2",
            proposal_revision=1,
            approval_id="approval-2",
            request_id="request-2",
            spec_digest="c" * 64,
            packet_digest="d" * 64,
            at=NOW,
        )
        quarantined = outbox.mark_recovery_required(
            intent, at=LATER, reason="process died after the physical-send intent"
        )
        assert quarantined.state is OutboxState.RECOVERY_REQUIRED
        assert outbox.unresolved() == (quarantined,)

        cleared = outbox.clear_recovery(
            quarantined, at=LATER, reconciliation={"broker": "no matching order"}
        )
        assert cleared.state is OutboxState.RECOVERY_CLEARED
        assert outbox.unresolved() == ()

    def test_outbox_compare_and_swap_rejects_stale_transition(self, tmp_path: Path) -> None:
        outbox = ExecutionOutbox(tmp_path / "state" / "outbox.jsonl")
        intent = outbox.prepare(
            logical_entry_id="entry-3",
            proposal_revision=1,
            approval_id="approval-3",
            request_id="request-3",
            spec_digest="e" * 64,
            packet_digest="f" * 64,
            at=NOW,
        )
        current = outbox.record_approval_consumed(intent, at=NOW)
        with pytest.raises(ReceiptConflict):
            outbox.record_broker_submission_observed(intent, broker_order_id="order", at=LATER)
        submitted = outbox.record_broker_submission_observed(
            current, broker_order_id="order", at=LATER
        )
        assert submitted.state is OutboxState.BROKER_SUBMISSION_OBSERVED

    def test_reference_saga_publishes_intent_before_consuming_approval(
        self, tmp_path: Path
    ) -> None:
        outbox = ExecutionOutbox(tmp_path / "state" / "outbox.jsonl")
        events: list[str] = []

        class Verifier:
            def recheck(self, packet, approval, *, now):
                events.append("recheck")
                return approval

            def consume(self, approval, *, now):
                events.append("consume")

        approval = SimpleNamespace(response_id="approval-4", request_id="request-4")
        packet = SimpleNamespace(spec=SimpleNamespace(digest="1" * 64))
        result = authorize_after_intent(
            outbox=outbox,
            verifier=Verifier(),
            packet=packet,
            approval=approval,
            logical_entry_id="entry-4",
            proposal_revision=1,
            now=NOW,
        )

        assert events == ["recheck", "consume"]
        assert result.state is OutboxState.APPROVAL_CONSUMED
        assert outbox.journal.records()[0]["kind"] == ReceiptKind.PHYSICAL_SEND_INTENT.value


class TestLogicalCAS:
    def test_stale_logical_writer_cannot_append_from_old_version(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        entry = harness.manager.claim(nomination(), now=NOW)
        harness.manager.propose(entry, harness.packet_for(entry), now=NOW)

        with pytest.raises(LogicalEntryConflict):
            harness.store.record_abandoned(
                entry.logical_entry_id,
                reason="stale writer",
                at=LATER,
                expected_version=entry.version,
            )
