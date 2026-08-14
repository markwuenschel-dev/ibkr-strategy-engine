"""R8 adversarial fixtures for reviewer and opening-entry safety."""

from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path

import pytest

from engine.errors import RefusedError
from engine.options.logical import LogicalEntryState, ServiceOutcome
from engine.options.order_outbox import (
    FAIL_REPRICE_BUDGET,
    ExecutionOutbox,
    OutboxState,
    TransmissionBudget,
)
from engine.options.transmit import place_combo
from test_options_logical import Harness, nomination
from test_options_transmit import RecordingIB, authorized, spread


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 14, 14, 0, tzinfo=UTC)
LATER = NOW + dt.timedelta(minutes=10)


class TestReviewerRevisionAndDoorGuards:
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "R3 must rebuild and compare the current packet while an entry is "
            "APPROVED_PENDING_EXECUTION; baseline returns ALREADY_APPROVED "
            "without checking the changed digest"
        ),
    )
    def test_approved_pending_entry_revalidates_changed_current_facts(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        entry = h.awaiting()
        h.reviewer.work(NOW)
        approved = h.manager.service(entry, h.packet_for(entry), now=LATER)
        assert approved.outcome is ServiceOutcome.APPROVED
        assert approved.entry.state is LogicalEntryState.APPROVED_PENDING_EXECUTION

        changed_packet = h.packet_for(approved.entry, credit="1.25")
        revalidated = h.manager.service(
            approved.entry,
            changed_packet,
            now=LATER + dt.timedelta(minutes=1),
        )

        assert revalidated.outcome is ServiceOutcome.SUPERSEDED
        assert revalidated.entry.proposal_revision == 2
        assert revalidated.entry.state is LogicalEntryState.AWAITING_REVIEW

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "R3/R7 must reject an approval whose TTL expires between "
            "authorization and the final transmission door"
        ),
    )
    def test_expired_approval_is_rechecked_at_the_physical_send_door(
        self, tmp_path: Path
    ) -> None:
        intent = spread()
        authorization = authorized(tmp_path, intent)
        assert authorization.approval is not None
        expired = dataclasses.replace(
            authorization.approval,
            expires_at=NOW - dt.timedelta(seconds=1),
        )
        expired_authorization = dataclasses.replace(
            authorization,
            approval=expired,
        )
        ib = RecordingIB()

        with pytest.raises(RefusedError, match="expired|TTL|approval"):
            place_combo(
                ib,
                intent,
                authorization=expired_authorization,
                account="DU1234567",
                session_lease=lambda: None,
            )
        assert ib.placed == []

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "R3 must make same-digest proposal recovery atomic; baseline has "
            "a read-then-create handoff race"
        ),
    )
    def test_two_concurrent_same_digest_proposals_file_one_handoff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        h = Harness(tmp_path)
        entry = h.manager.claim(nomination(), now=NOW)
        packet = h.packet_for(entry)

        from engine.options.approval import CollabVerifierGate

        competitor = CollabVerifierGate(root=h.root, ledger=h.gate.ledger)
        first = CollabVerifierGate(root=h.root, ledger=h.gate.ledger)
        real_lookup = first.request_id_for
        raced = False

        def stale_lookup(spec):
            nonlocal raced
            if not raced:
                raced = True
                competitor.propose(packet, now=NOW)
                return ""
            return real_lookup(spec)

        monkeypatch.setattr(first, "request_id_for", stale_lookup)
        first_id = first.propose(packet, now=NOW)

        handoffs = h.handoffs()
        assert len(handoffs) == 1
        assert first_id == handoffs[0].id


class TestEntrySagaContractSkips:
    def test_crash_after_approval_consumption_has_durable_outbox_recovery(
        self, tmp_path: Path
    ) -> None:
        intent = spread()
        outbox = ExecutionOutbox(tmp_path / "outbox")
        attempt = outbox.prepare(
            intent,
            structure_digest="a" * 64,
            spec_digest="b" * 64,
            account="DU1234567",
            approval_id="approval-1",
            now=NOW,
        )
        outbox.approval_consumed(attempt)

        restarted = ExecutionOutbox(tmp_path / "outbox")

        assert restarted.records()[0]["state"] == OutboxState.APPROVAL_CONSUMED.value
        with pytest.raises(RefusedError, match="FAIL-BROKER-AMBIGUOUS"):
            restarted.assert_clear()

    def test_reprice_rungs_consume_the_shared_session_order_budget(
        self, tmp_path: Path
    ) -> None:
        budget = TransmissionBudget(tmp_path / "budget.json", limit=1, now=NOW)
        reservation = budget.reserve(spread().strategy_id, now=NOW)
        budget.commit(reservation.reservation_id)

        with pytest.raises(RefusedError, match=FAIL_REPRICE_BUDGET):
            budget.reserve(spread().strategy_id, now=NOW)
