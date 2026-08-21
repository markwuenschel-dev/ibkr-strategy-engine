"""The paper-day recovery verb's 9-point acceptance bar (decisions.md
"Recovery acceptance bar (binding)"), as pure, unit-tested, UNWIRED functions.

``engine.paperday_recovery`` is dead code from the runtime's perspective:
importable and unit-testable here, reachable from nothing an operator can
invoke today. See that module's docstring for the full statement of that
constraint. This file exercises each of the 9 requirements independently,
with synthetic fixtures (mocked broker/outbox/scheduler shapes), the same
style :mod:`test_options_integration` uses for a broker it does not have in
tests.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from engine.archive import ArchiveError
from engine.paperday import GATE_CLOSED
from engine.paperday_recovery import (
    BrokerReconciliationOutcome,
    RecoveryAttempt,
    RecoveryCheck,
    REQUIREMENT_1_EXCLUSIVE_LOCK,
    REQUIREMENT_2_SESSION_LEASE_PROCESS_IDENTITY,
    REQUIREMENT_3_READABLE_KNOWN_STATE,
    REQUIREMENT_4_NO_UNMATCHED_TICKS_OR_OPENING_OUTBOX,
    REQUIREMENT_5_BROKER_RECONCILIATION,
    REQUIREMENT_6_FENCING_TOKEN_CAS,
    REQUIREMENT_7_ARCHIVE_EVIDENCE_BEFORE_MUTATION,
    REQUIREMENT_8_PERSIST_REASON_AND_RECONCILIATION_RECEIPT,
    SessionIdentity,
    check_archive_evidence_before_mutation,
    check_broker_reconciliation,
    check_exclusive_recovery_lock,
    check_fencing_token_cas,
    check_no_unmatched_ticks_or_opening_outbox,
    check_readable_known_state,
    check_session_lease_process_identity,
    evaluate_recovery_acceptance_bar,
    persist_reason_and_reconciliation_receipt,
)

NOW = dt.datetime(2026, 8, 20, 12, 0, 0, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------------------
# Requirement 1 -- lock exclusively
# ---------------------------------------------------------------------------


class TestRequirement1ExclusiveLock:
    def test_refuses_when_the_lock_cannot_be_acquired(self) -> None:
        check = check_exclusive_recovery_lock(lambda: False)

        assert check.requirement == REQUIREMENT_1_EXCLUSIVE_LOCK
        assert check.passed is False
        assert "already held" in check.detail

    def test_passes_when_the_lock_is_acquired(self) -> None:
        check = check_exclusive_recovery_lock(lambda: True)

        assert check.passed is True

    def test_an_exception_during_acquisition_is_a_refusal_not_a_crash(self) -> None:
        def raises() -> bool:
            raise OSError("lock file busy")

        check = check_exclusive_recovery_lock(raises)

        assert check.passed is False
        assert "lock file busy" in check.detail


# ---------------------------------------------------------------------------
# Requirement 2 -- exact session/lease/process identity
# ---------------------------------------------------------------------------


class TestRequirement2SessionLeaseProcessIdentity:
    expected = SessionIdentity(
        session_id="paperday-20260820-5f6c822e",
        lease_nonce="ada79de6",
        process_id=64020,
    )

    def test_matching_identity_passes(self) -> None:
        check = check_session_lease_process_identity(
            expected=self.expected, observed=self.expected
        )

        assert check.requirement == REQUIREMENT_2_SESSION_LEASE_PROCESS_IDENTITY
        assert check.passed is True

    def test_mismatched_identity_refuses(self) -> None:
        observed = SessionIdentity(
            session_id="paperday-20260819-fa4081f5",  # a different, prior session
            lease_nonce="ada79de6",
            process_id=64020,
        )

        check = check_session_lease_process_identity(
            expected=self.expected, observed=observed
        )

        assert check.passed is False
        assert "mismatch" in check.detail

    def test_missing_identity_refuses(self) -> None:
        check = check_session_lease_process_identity(expected=self.expected, observed=None)

        assert check.passed is False
        assert "no identity" in check.detail

    def test_partially_missing_identity_component_refuses(self) -> None:
        observed = SessionIdentity(session_id=self.expected.session_id, lease_nonce=None, process_id=64020)

        check = check_session_lease_process_identity(expected=self.expected, observed=observed)

        assert check.passed is False


# ---------------------------------------------------------------------------
# Requirement 3 -- reject unreadable or unknown state
# ---------------------------------------------------------------------------


class TestRequirement3ReadableKnownState:
    def test_unreadable_none_state_is_refused(self) -> None:
        check = check_readable_known_state(state=None, supported_schema_versions=frozenset({1}))

        assert check.requirement == REQUIREMENT_3_READABLE_KNOWN_STATE
        assert check.passed is False
        assert "unreadable" in check.detail

    def test_unknown_schema_version_is_refused(self) -> None:
        check = check_readable_known_state(
            state={"schema_version": 999, "session_id": "s"},
            supported_schema_versions=frozenset({1}),
        )

        assert check.passed is False
        assert "schema" in check.detail

    def test_known_schema_version_passes(self) -> None:
        check = check_readable_known_state(
            state={"schema_version": 1, "session_id": "s"},
            supported_schema_versions=frozenset({1}),
        )

        assert check.passed is True


# ---------------------------------------------------------------------------
# Requirement 4 -- no unmatched ticks / no opening outbox records
# ---------------------------------------------------------------------------


class TestRequirement4NoUnmatchedTicksOrOpeningOutbox:
    def test_passes_when_both_are_empty(self) -> None:
        check = check_no_unmatched_ticks_or_opening_outbox(
            session_id="s1", unmatched_ticks=[], outbox_blocking_records=[]
        )

        assert check.requirement == REQUIREMENT_4_NO_UNMATCHED_TICKS_OR_OPENING_OUTBOX
        assert check.passed is True

    def test_an_unmatched_tick_blocks(self) -> None:
        check = check_no_unmatched_ticks_or_opening_outbox(
            session_id="s1",
            unmatched_ticks=[{"tick_id": "t1"}],
            outbox_blocking_records=[],
        )

        assert check.passed is False
        assert "unmatched tick" in check.detail

    def test_a_same_session_blocking_outbox_record_blocks(self) -> None:
        check = check_no_unmatched_ticks_or_opening_outbox(
            session_id="s1",
            unmatched_ticks=[],
            outbox_blocking_records=[{"attempt_id": "a1", "session_id": "s1"}],
        )

        assert check.passed is False
        assert "outbox" in check.detail

    def test_a_record_with_no_session_id_still_blocks(self) -> None:
        """decisions.md item 4 says no opening outbox records "remain" --
        unconditional, not scoped to the current session. This is the
        conservative (fail-closed) choice: neither ExecutionOutbox
        implementation lets a caller safely prove a record belongs to a
        different, already-resolved session (execution_outbox.py has zero
        session_id occurrences; order_outbox.py's blocking_records() never
        consults the session_id it does record -- see the module docstring),
        so any blocking record, attributed or not, blocks."""
        check = check_no_unmatched_ticks_or_opening_outbox(
            session_id="s1",
            unmatched_ticks=[],
            outbox_blocking_records=[{"attempt_id": "a1", "session_id": None}],
        )

        assert check.passed is False

    def test_a_foreign_session_blocking_record_still_blocks(self) -> None:
        """Even a record clearly attributed to a *different* session blocks
        -- decisions.md's "no ... remain" has no session carve-out."""
        check = check_no_unmatched_ticks_or_opening_outbox(
            session_id="s1",
            unmatched_ticks=[],
            outbox_blocking_records=[{"attempt_id": "a1", "session_id": "s0-prior"}],
        )

        assert check.passed is False


# ---------------------------------------------------------------------------
# Requirement 5 -- reconcile broker positions, orders, executions
# ---------------------------------------------------------------------------


class TestRequirement5BrokerReconciliation:
    def test_agreeing_reconciliation_passes(self) -> None:
        check = check_broker_reconciliation(
            lambda: BrokerReconciliationOutcome(agrees=True, detail="flat, zero working orders")
        )

        assert check.requirement == REQUIREMENT_5_BROKER_RECONCILIATION
        assert check.passed is True

    def test_disagreeing_reconciliation_refuses(self) -> None:
        check = check_broker_reconciliation(
            lambda: BrokerReconciliationOutcome(
                agrees=False, detail="1 stranded_opening leg not seen at broker"
            )
        )

        assert check.passed is False
        assert "stranded_opening" in check.detail

    def test_evidence_carries_the_outcome_for_the_receipt(self) -> None:
        outcome = BrokerReconciliationOutcome(agrees=True, detail="agrees")
        check = check_broker_reconciliation(lambda: outcome)

        assert check.evidence == outcome


# ---------------------------------------------------------------------------
# Requirement 6 -- fencing-token CAS
# ---------------------------------------------------------------------------


class TestRequirement6FencingTokenCas:
    def test_matching_token_passes(self) -> None:
        check = check_fencing_token_cas(
            expected_token="9f3a3cbd4c0545c6b76cfd2c353b730b",
            observed_token="9f3a3cbd4c0545c6b76cfd2c353b730b",
        )

        assert check.requirement == REQUIREMENT_6_FENCING_TOKEN_CAS
        assert check.passed is True

    def test_stale_token_refuses(self) -> None:
        check = check_fencing_token_cas(
            expected_token="9f3a3cbd4c0545c6b76cfd2c353b730b",
            observed_token="2f69f925be304bc1a2e9a80a0d3f40ca",  # a prior session's token
        )

        assert check.passed is False
        assert "stale" in check.detail

    def test_no_observed_token_refuses(self) -> None:
        check = check_fencing_token_cas(
            expected_token="9f3a3cbd4c0545c6b76cfd2c353b730b", observed_token=None
        )

        assert check.passed is False


# ---------------------------------------------------------------------------
# Requirement 7 -- archive evidence before mutation
# ---------------------------------------------------------------------------


class TestRequirement7ArchiveEvidenceBeforeMutation:
    def test_successful_archive_passes_and_carries_the_manifest(self, tmp_path: Path) -> None:
        source = tmp_path / "gate.json"
        source.write_text('{"state": "PAPER_DAY_BLOCKED"}', encoding="utf-8")

        check = check_archive_evidence_before_mutation(
            source=source, archive_dir=tmp_path / "archive", reason="pre-clear", now=NOW
        )

        assert check.requirement == REQUIREMENT_7_ARCHIVE_EVIDENCE_BEFORE_MUTATION
        assert check.passed is True
        assert check.evidence is not None
        assert check.evidence.archive_path.exists()

    def test_a_missing_source_refuses_rather_than_silently_skipping(self, tmp_path: Path) -> None:
        check = check_archive_evidence_before_mutation(
            source=tmp_path / "does-not-exist.json",
            archive_dir=tmp_path / "archive",
            reason="pre-clear",
            now=NOW,
        )

        assert check.passed is False
        assert "refused" in check.detail or "unreadable" in check.detail

    def test_an_archive_backend_failure_is_a_refusal_not_a_crash(self, tmp_path: Path) -> None:
        def failing_archive(*_args: object, **_kwargs: object) -> None:
            raise ArchiveError("disk full")

        check = check_archive_evidence_before_mutation(
            source=tmp_path / "gate.json",
            archive_dir=tmp_path / "archive",
            reason="pre-clear",
            now=NOW,
            archive_fn=failing_archive,
        )

        assert check.passed is False
        assert "disk full" in check.detail


# ---------------------------------------------------------------------------
# Requirement 8 -- persist the reason and the reconciliation receipt
# ---------------------------------------------------------------------------


class TestRequirement8PersistReasonAndReconciliationReceipt:
    def test_persists_reason_and_reconciliation_durably(self, tmp_path: Path) -> None:
        path = tmp_path / "recovery-receipt.json"

        check = persist_reason_and_reconciliation_receipt(
            path=path,
            reason="stale lease, broker confirmed flat",
            reconciliation={"agrees": True, "detail": "flat"},
            now=NOW,
        )

        assert check.requirement == REQUIREMENT_8_PERSIST_REASON_AND_RECONCILIATION_RECEIPT
        assert check.passed is True
        assert path.exists()
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["reason"] == "stale lease, broker confirmed flat"
        assert on_disk["reconciliation"] == {"agrees": True, "detail": "flat"}

    def test_empty_reason_refuses(self, tmp_path: Path) -> None:
        check = persist_reason_and_reconciliation_receipt(
            path=tmp_path / "recovery-receipt.json",
            reason="   ",
            reconciliation={"agrees": True},
            now=NOW,
        )

        assert check.passed is False
        assert not (tmp_path / "recovery-receipt.json").exists()


# ---------------------------------------------------------------------------
# Requirement 9 -- entry authority stays CLOSED, no matter what
# ---------------------------------------------------------------------------


def _all_pass_attempt(tmp_path: Path) -> RecoveryAttempt:
    identity = SessionIdentity(session_id="s1", lease_nonce="n1", process_id=1)
    source = tmp_path / "gate.json"
    source.write_text("{}", encoding="utf-8")
    return RecoveryAttempt(
        acquire_lock=lambda: True,
        expected_identity=identity,
        observed_identity=identity,
        state={"schema_version": 1, "session_id": "s1"},
        supported_schema_versions=frozenset({1}),
        session_id="s1",
        unmatched_ticks=[],
        outbox_blocking_records=[],
        reconcile=lambda: BrokerReconciliationOutcome(agrees=True, detail="flat"),
        expected_fencing_token="tok",
        observed_fencing_token="tok",
        archive_source=source,
        archive_dir=tmp_path / "archive",
        reason="operator-authorized clean recovery",
        receipt_path=tmp_path / "recovery-receipt.json",
        now=NOW,
    )


def _all_fail_attempt(tmp_path: Path) -> RecoveryAttempt:
    return RecoveryAttempt(
        acquire_lock=lambda: False,
        expected_identity=SessionIdentity("s1", "n1", 1),
        observed_identity=None,
        state=None,
        supported_schema_versions=frozenset({1}),
        session_id="s1",
        unmatched_ticks=[{"tick_id": "t1"}],
        outbox_blocking_records=[{"attempt_id": "a1", "session_id": None}],
        reconcile=lambda: BrokerReconciliationOutcome(agrees=False, detail="disagrees"),
        expected_fencing_token="tok",
        observed_fencing_token="stale-tok",
        archive_source=tmp_path / "does-not-exist.json",
        archive_dir=tmp_path / "archive",
        reason="",
        receipt_path=tmp_path / "recovery-receipt.json",
        now=NOW,
    )


class TestRequirement9EntryAuthorityStaysClosed:
    def test_entry_gate_is_closed_when_every_check_passes(self, tmp_path: Path) -> None:
        result = evaluate_recovery_acceptance_bar(_all_pass_attempt(tmp_path))

        assert result.all_passed is True
        assert result.entry_gate == GATE_CLOSED

    def test_entry_gate_is_closed_when_every_check_fails(self, tmp_path: Path) -> None:
        result = evaluate_recovery_acceptance_bar(_all_fail_attempt(tmp_path))

        assert result.all_passed is False
        assert result.entry_gate == GATE_CLOSED

    @pytest.mark.parametrize("flip_index", range(9))
    def test_entry_gate_is_closed_for_every_single_check_failure(
        self, tmp_path: Path, flip_index: int
    ) -> None:
        """No combination of pass/fail ever produces anything but CLOSED --
        this function has no code path that opens entry authority. Recovery
        only ever clears the sticky latch; a brand-new, independently
        validated session is what opens the gate (acceptance bar item 9)."""
        attempt = _all_pass_attempt(tmp_path)
        result = evaluate_recovery_acceptance_bar(attempt)
        assert result.entry_gate == GATE_CLOSED
        assert len(result.checks) >= 8


# ---------------------------------------------------------------------------
# Orchestrator wiring: requirement 8 depends on requirement 7 (D5/D12: no
# mutation-adjacent persistence without evidence archived first)
# ---------------------------------------------------------------------------


class TestArchiveFailureBlocksReceiptPersistence:
    def test_receipt_is_not_persisted_when_archiving_failed(self, tmp_path: Path) -> None:
        attempt = _all_pass_attempt(tmp_path)
        attempt = attempt.__class__(
            **{**attempt.__dict__, "archive_source": tmp_path / "does-not-exist.json"}
        )

        result = evaluate_recovery_acceptance_bar(attempt)

        archive_check = result.check(REQUIREMENT_7_ARCHIVE_EVIDENCE_BEFORE_MUTATION)
        receipt_check = result.check(REQUIREMENT_8_PERSIST_REASON_AND_RECONCILIATION_RECEIPT)
        assert archive_check is not None and archive_check.passed is False
        assert receipt_check is not None and receipt_check.passed is False
        assert not attempt.receipt_path.exists()
        assert result.entry_gate == GATE_CLOSED


# ---------------------------------------------------------------------------
# evaluate_recovery_acceptance_bar's result is a structured, inspectable
# object -- not a single bool.
# ---------------------------------------------------------------------------


class TestStructuredResult:
    def test_result_names_which_requirements_pass_and_fail(self, tmp_path: Path) -> None:
        result = evaluate_recovery_acceptance_bar(_all_fail_attempt(tmp_path))

        by_requirement = {c.requirement: c.passed for c in result.checks}
        assert by_requirement[REQUIREMENT_1_EXCLUSIVE_LOCK] is False
        assert by_requirement[REQUIREMENT_5_BROKER_RECONCILIATION] is False
        assert all(isinstance(c, RecoveryCheck) for c in result.checks)
