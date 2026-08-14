"""R8 adversarial fixtures for reviewer and opening-entry safety."""

from __future__ import annotations

import dataclasses
import datetime as dt
import importlib
from pathlib import Path

import pytest

from engine.errors import RefusedError
from engine.options.logical import LogicalEntryState, ServiceOutcome
from engine.options.transmit import place_combo
from test_options_logical import Harness, nomination
from test_options_transmit import RecordingIB, authorized, spread


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 14, 14, 0, tzinfo=UTC)
LATER = NOW + dt.timedelta(minutes=10)


def _missing_contract(module_names: tuple[str, ...], symbols: tuple[str, ...]) -> None:
    found: list[str] = []
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name or str(exc.name).startswith(module_name + "."):
                continue
            raise
        found.append(module_name)
        if all(hasattr(module, symbol) for symbol in symbols):
            return
    pytest.skip(
        "missing contract seam: "
        + ", ".join(f"{module}.{symbol}" for module in module_names for symbol in symbols)
        + (f" (searched {', '.join(found)})" if found else "")
    )


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
    def test_crash_after_approval_consumption_has_durable_outbox_recovery(self) -> None:
        _missing_contract(
            ("engine.options.outbox", "engine.execution_outbox", "engine.autocycle"),
            ("ExecutionOutbox",),
        )
        pytest.fail("the execution outbox fixture adapter is not wired")

    def test_reprice_rungs_consume_the_shared_session_order_budget(self) -> None:
        _missing_contract(
            ("engine.options.budget", "engine.options.reprice", "engine.options.runner"),
            ("TransmissionBudget",),
        )
        pytest.fail("the reprice-budget fixture adapter is not wired")

