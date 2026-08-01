"""Proof that one intended position keeps one identity across passes and restarts.

The defect under test: ``run_once`` mints a fresh ``uuid4`` per pass
(``selection.build_vertical``), so an approval awaited on pass N could never be
claimed on pass N+1 -- the new id hashed to a new digest and the answer
orphaned. :mod:`engine.options.logical` owns the identity instead, and every
test here drives the **real** :class:`engine.options.approval.CollabVerifierGate`
over a real temp collab with the :class:`reviewer.ScriptedReviewer` seat -- no
mocked gate anywhere, and one fixed logical clock.

Organisation:

* :class:`TestIdentityAcrossPasses` -- the stable-id contract: static markets,
  late approvals, restarts, duplicate nominations.
* :class:`TestRevisionsAndSupersession` -- changed markets: same id, next
  revision, exactly one replacement handoff, and the digest binding that makes
  an old approval worthless against a new revision.
* :class:`TestRefusalAndExpiry` -- the named cooldown policy and the
  reservation-never-leaks invariant.
* :class:`TestConcurrencyInvariants` -- one review revision per entry, one
  working physical order per entry, three underlyings with no cross-talk.
* :class:`TestPersistenceDiscipline` -- persist-before-file ordering and
  corrupt-line degradation.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from engine._collabkit import load
from engine.errors import RefusedError
from engine.options.approval import (
    ApprovalDecision,
    AwaitingVerification,
    CollabVerifierGate,
)
from engine.options.logical import (
    EntryNomination,
    LineageRecord,
    LogicalEntryManager,
    LogicalEntryState,
    LogicalEntryStore,
    RefusalCooldownPolicy,
    RevisionOutcome,
    ServiceOutcome,
)
from reviewer import ScriptedReviewer, collab_at, packet as build_packet, approval_context
from test_options_transmit import NOW, approving_governor, approving_risk, refusing_risk, spread

D = Decimal

#: One pass later, well inside every TTL.
LATER = NOW + dt.timedelta(minutes=10)
#: Past the 12-hour approval TTL that caps a filed review's life.
AFTER_EXPIRY = NOW + dt.timedelta(hours=13)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def nomination(underlying: str = "SPY") -> EntryNomination:
    """A scan nomination for the standard 500/495 put credit spread."""
    intent = spread(underlying=underlying)
    return EntryNomination(
        underlying=underlying,
        strategy_family=intent.strategy_type,
        direction="SHORT_PREMIUM",
        expiration=intent.expiration,
        legs=intent.legs,
        reservation_amount=intent.total_maximum_loss,
    )


class Harness:
    """A real gate, a real store, a real reviewer seat, one fixed clock."""

    def __init__(self, tmp_path: Path, **manager_kwargs: Any) -> None:
        self.tmp_path = tmp_path
        self.root = collab_at(tmp_path)
        self.gate = CollabVerifierGate(
            root=self.root, ledger=tmp_path / "state" / "verification"
        )
        self.store = LogicalEntryStore(tmp_path / "state" / "logical_entries.jsonl")
        self.reviewer = ScriptedReviewer(root=self.root)
        self.context = approval_context()
        self.manager = LogicalEntryManager(
            store=self.store, gate=self.gate, clock=lambda: NOW, **manager_kwargs
        )

    def packet_for(
        self,
        entry: Any,
        *,
        credit: str = "1.50",
        risk: Any = None,
        now: dt.datetime = NOW,
    ) -> Any:
        """The freshly revalidated packet for the CURRENT market, with the
        intent's strategy_id pinned to the logical identity -- the discipline
        the runner will follow at integration time."""
        intent = spread(underlying=entry.underlying, credit=credit)
        object.__setattr__(intent, "strategy_id", entry.logical_entry_id)
        risk = risk if risk is not None else approving_risk(intent.strategy_id)
        return build_packet(
            intent,
            risk=risk,
            governor=approving_governor(intent),
            context=self.context,
            now=now,
        )

    def handoffs(self) -> list[Any]:
        """Every request ever filed to the reviewer, whatever its state."""
        paths = load("paths", "CollabPaths").at(self.root)
        store = load("store", "HandoffStore")(paths)
        return list(store.list(("pending", "claimed", "done", "archive"), to="reviewer"))

    def consumed(self) -> list[Path]:
        return list((Path(self.gate.ledger) / "consumed").glob("*.used"))

    def awaiting(self, underlying: str = "SPY", *, credit: str = "1.50") -> Any:
        """Claim + propose one entry, leaving it AWAITING_REVIEW."""
        entry = self.manager.claim(nomination(underlying), now=NOW)
        return self.manager.propose(entry, self.packet_for(entry, credit=credit), now=NOW)


# ===========================================================================
# Identity across passes
# ===========================================================================


class TestIdentityAcrossPasses:
    def test_a_static_market_across_passes_preserves_id_and_digest(
        self, tmp_path: Path
    ) -> None:
        """Required test 1. Three passes, nothing moves: same identity, same
        digest, one outstanding handoff, still waiting."""
        h = Harness(tmp_path)
        entry = h.awaiting()
        original_id = entry.logical_entry_id
        original_digest = entry.current_spec_digest
        assert original_digest == h.packet_for(entry).spec.digest

        for minutes in (5, 10, 15):
            result = h.manager.service(
                entry, h.packet_for(entry), now=NOW + dt.timedelta(minutes=minutes)
            )
            assert result.outcome is ServiceOutcome.WAITING
            entry = result.entry

        entry = h.manager.entry(original_id)
        assert entry.logical_entry_id == original_id
        assert entry.current_spec_digest == original_digest
        assert entry.proposal_revision == 1
        assert entry.state is LogicalEntryState.AWAITING_REVIEW
        assert len(h.handoffs()) == 1

    def test_an_approval_arriving_on_a_later_pass_is_accepted(
        self, tmp_path: Path
    ) -> None:
        """Required tests 2 and (first half of) 10. The reviewer answers between
        passes; the later pass claims it, and the caller consumes exactly once."""
        h = Harness(tmp_path)
        entry = h.awaiting()
        assert (
            h.manager.service(entry, h.packet_for(entry), now=NOW).outcome
            is ServiceOutcome.WAITING
        )

        h.reviewer.work(NOW)

        result = h.manager.service(entry, h.packet_for(entry), now=LATER)
        assert result.outcome is ServiceOutcome.APPROVED
        assert result.approval is not None
        assert result.approval.intent_id == entry.logical_entry_id
        assert result.entry.state is LogicalEntryState.APPROVED_PENDING_EXECUTION
        assert result.entry.current_approval_id == result.approval.response_id

        # Consumption is the caller's, mirroring the arm-gate placement -- and
        # it happens exactly once.
        assert h.consumed() == []
        h.gate.consume(result.approval, now=LATER)
        assert len(h.consumed()) == 1

    def test_a_consumed_approval_cannot_be_reused(self, tmp_path: Path) -> None:
        """Required test 10. The second spend refuses, and the gate refuses to
        re-issue the same answer."""
        h = Harness(tmp_path)
        entry = h.awaiting()
        h.reviewer.work(NOW)
        result = h.manager.service(entry, h.packet_for(entry), now=LATER)
        h.gate.consume(result.approval, now=LATER)

        with pytest.raises(RefusedError) as second:
            h.gate.consume(result.approval, now=LATER)
        assert "already been consumed" in second.value.message

        with pytest.raises(RefusedError) as require_again:
            h.gate.require(h.packet_for(entry), now=LATER)
        assert "already consumed" in (require_again.value.hint or "")

    def test_a_restart_while_awaiting_restores_the_pending_review(
        self, tmp_path: Path
    ) -> None:
        """Required test 3. A new store instance over the same file restores the
        entry, the outstanding handoff id and the reservation id -- and the
        restarted manager then accepts the approval."""
        h = Harness(tmp_path)
        entry = h.awaiting()
        assert entry.reservation_id is not None

        restarted_store = LogicalEntryStore(h.store.path)
        restarted = LogicalEntryManager(
            store=restarted_store, gate=h.gate, clock=lambda: NOW
        )
        restored = restarted.active_for("SPY")
        assert restored is not None
        assert restored.logical_entry_id == entry.logical_entry_id
        assert restored.state is LogicalEntryState.AWAITING_REVIEW
        assert restored.current_handoff_id == entry.current_handoff_id
        assert restored.current_spec_digest == entry.current_spec_digest
        assert restored.reservation_id == entry.reservation_id
        assert restored.reservation_amount == entry.reservation_amount

        h.reviewer.work(NOW)
        result = restarted.service(restored, h.packet_for(restored), now=LATER)
        assert result.outcome is ServiceOutcome.APPROVED
        assert len(h.handoffs()) == 1, "the restart filed a duplicate request"

    def test_duplicate_scan_nominations_create_one_logical_entry(
        self, tmp_path: Path
    ) -> None:
        """Required test 4, and the named test for mutation check 2: remove the
        duplicate-underlying claim guard and this fails."""
        h = Harness(tmp_path)
        first = h.manager.claim(nomination(), now=NOW)
        second = h.manager.claim(nomination(), now=NOW)
        third = h.manager.claim(nomination(), now=LATER)

        assert first.logical_entry_id == second.logical_entry_id == third.logical_entry_id
        assert len(h.store.entries()) == 1
        # And the reservation was minted once, not once per nomination.
        assert first.reservation_id == third.reservation_id


# ===========================================================================
# Revisions and supersession
# ===========================================================================


class TestRevisionsAndSupersession:
    def test_a_price_change_preserves_the_id_and_increments_the_revision(
        self, tmp_path: Path
    ) -> None:
        """Required test 5. Same entry, revision 2, exactly one replacement
        handoff, prior revision SUPERSEDED in the lineage."""
        h = Harness(tmp_path)
        entry = h.awaiting(credit="1.50")
        first_handoff = entry.current_handoff_id
        first_digest = entry.current_spec_digest

        repriced = h.packet_for(entry, credit="1.25")
        assert repriced.spec.digest != first_digest

        result = h.manager.service(entry, repriced, now=LATER)
        assert result.outcome is ServiceOutcome.SUPERSEDED
        updated = result.entry
        assert updated.logical_entry_id == entry.logical_entry_id
        assert updated.proposal_revision == 2
        assert updated.current_spec_digest == repriced.spec.digest
        assert updated.current_handoff_id != first_handoff
        assert updated.state is LogicalEntryState.AWAITING_REVIEW
        assert len(h.handoffs()) == 2

        superseded = [
            r for r in updated.lineage if r.outcome is RevisionOutcome.SUPERSEDED
        ]
        assert [(r.revision, r.handoff_id) for r in superseded] == [(1, first_handoff)]

    def test_a_risk_change_supersedes_the_prior_revision(self, tmp_path: Path) -> None:
        """Required test 6. The digest binds the risk verdict, so a re-run that
        refused is a different reviewed question."""
        h = Harness(tmp_path)
        entry = h.awaiting()

        reassessed = h.packet_for(
            entry, risk=refusing_risk(entry.logical_entry_id)
        )
        assert reassessed.spec.digest != entry.current_spec_digest

        result = h.manager.service(entry, reassessed, now=LATER)
        assert result.outcome is ServiceOutcome.SUPERSEDED
        assert result.entry.logical_entry_id == entry.logical_entry_id
        assert result.entry.proposal_revision == 2
        assert len(h.handoffs()) == 2

    def test_an_unchanged_digest_never_creates_a_second_handoff(
        self, tmp_path: Path
    ) -> None:
        """Required test 7, asserted against the collab store's own count."""
        h = Harness(tmp_path)
        entry = h.awaiting()
        for minutes in (1, 2, 30, 60):
            h.manager.service(
                entry, h.packet_for(entry), now=NOW + dt.timedelta(minutes=minutes)
            )
        assert len(h.handoffs()) == 1

    def test_the_old_approval_remains_unconsumed_after_supersession(
        self, tmp_path: Path
    ) -> None:
        """Required test 8, and the named test for mutation check 1: make
        supersession consume the old approval anyway and this fails."""
        h = Harness(tmp_path)
        entry = h.awaiting()
        h.reviewer.work(NOW)  # revision 1's approval is now on disk

        result = h.manager.service(entry, h.packet_for(entry, credit="1.25"), now=LATER)
        assert result.outcome is ServiceOutcome.SUPERSEDED
        assert h.consumed() == [], (
            "supersession spent an approval it must leave on disk untouched"
        )

    def test_an_approval_for_revision_n_cannot_authorize_revision_n_plus_1(
        self, tmp_path: Path
    ) -> None:
        """Required test 9. Proven twice: the new request has no answer (the
        old approval does not carry), and an answer that *echoes* revision 1's
        digest against revision 2 is refused by the gate on the mismatch."""
        h = Harness(tmp_path)
        entry = h.awaiting(credit="1.50")
        revision_one_digest = entry.current_spec_digest
        h.reviewer.work(NOW)  # approve revision 1

        repriced = h.packet_for(entry, credit="1.25")
        result = h.manager.service(entry, repriced, now=LATER)
        assert result.outcome is ServiceOutcome.SUPERSEDED

        # The old approval answers the old request; the new request is unanswered.
        with pytest.raises(AwaitingVerification):
            h.gate.require(repriced, now=LATER)

        # A reviewer that answers revision 2 while binding revision 1's digest
        # is refused on the digest, by the gate itself.
        h.reviewer.mangle = lambda fields: fields.__setitem__(
            "spec_digest", revision_one_digest
        )
        h.reviewer.work(NOW)
        with pytest.raises(RefusedError) as exc:
            h.manager.service(result.entry, repriced, now=LATER)
        assert "approved spec" in (exc.value.hint or "")


# ===========================================================================
# Refusal cooldown and expiry: reservations never leak
# ===========================================================================


class TestRefusalAndExpiry:
    def test_a_timed_out_review_clears_the_reservation(self, tmp_path: Path) -> None:
        """Required test 11 (timeout half). Nobody ever answers; past the
        approval TTL the entry expires and the reservation is released with a
        lineage record."""
        h = Harness(tmp_path)
        entry = h.awaiting()
        assert entry.reservation_id is not None

        result = h.manager.service(entry, h.packet_for(entry), now=AFTER_EXPIRY)
        assert result.outcome is ServiceOutcome.EXPIRED
        expired = result.entry
        assert expired.state is LogicalEntryState.EXPIRED
        assert expired.reservation_id is None
        assert expired.reservation_amount is None
        outcomes = [r.outcome for r in expired.lineage]
        assert RevisionOutcome.RESERVATION_RELEASED in outcomes
        assert RevisionOutcome.EXPIRED in outcomes

    def test_an_unavailable_review_waits_then_expires_without_leaking(
        self, tmp_path: Path
    ) -> None:
        """Required test 11 (UNAVAILABLE half). UNAVAILABLE blocks without
        deciding: the entry keeps waiting with its reservation intact, and the
        TTL is what ends it -- reservation released, not leaked."""
        h = Harness(tmp_path)
        h.reviewer.decision = ApprovalDecision.UNAVAILABLE
        entry = h.awaiting()
        h.reviewer.work(NOW)

        waiting = h.manager.service(entry, h.packet_for(entry), now=LATER)
        assert waiting.outcome is ServiceOutcome.UNAVAILABLE
        assert waiting.entry.state is LogicalEntryState.AWAITING_REVIEW
        assert waiting.entry.reservation_id is not None

        expired = h.manager.service(entry, h.packet_for(entry), now=AFTER_EXPIRY)
        assert expired.outcome is ServiceOutcome.EXPIRED
        assert expired.entry.reservation_id is None

    def test_a_refusal_enters_cooldown_and_keeps_the_entry_alive(
        self, tmp_path: Path
    ) -> None:
        """REFUSED is a recorded decision, not a death: the entry cools down
        under the named policy, id and reservation intact."""
        h = Harness(tmp_path)
        h.reviewer.decision = ApprovalDecision.REFUSED
        entry = h.awaiting()
        h.reviewer.work(NOW)

        result = h.manager.service(entry, h.packet_for(entry), now=LATER)
        assert result.outcome is ServiceOutcome.REFUSED
        refused = result.entry
        assert refused.state is LogicalEntryState.REFUSED_COOLDOWN
        assert refused.refusal_count == 1
        assert refused.reservation_id is not None
        assert refused.logical_entry_id == entry.logical_entry_id

    def test_the_cooldown_gates_refiling_and_a_new_revision_files_after_it(
        self, tmp_path: Path
    ) -> None:
        """The named policy end to end: cooling blocks any filing; after the
        cooldown an identical spec still stands refused (same digest, same
        answer), and a *changed* spec files revision 2 -- one new handoff,
        same identity."""
        h = Harness(tmp_path)
        h.reviewer.decision = ApprovalDecision.REFUSED
        entry = h.awaiting()
        h.reviewer.work(NOW)
        refused = h.manager.service(entry, h.packet_for(entry), now=LATER).entry

        inside_cooldown = LATER + dt.timedelta(minutes=5)
        cooling = h.manager.service(
            refused, h.packet_for(refused, credit="1.25"), now=inside_cooldown
        )
        assert cooling.outcome is ServiceOutcome.COOLING
        assert len(h.handoffs()) == 1

        after_cooldown = LATER + dt.timedelta(minutes=31)
        standing = h.manager.service(refused, h.packet_for(refused), now=after_cooldown)
        assert standing.outcome is ServiceOutcome.REFUSAL_STANDS
        assert len(h.handoffs()) == 1

        refiled = h.manager.service(
            refused, h.packet_for(refused, credit="1.25"), now=after_cooldown
        )
        assert refiled.outcome is ServiceOutcome.REFILED
        assert refiled.entry.logical_entry_id == entry.logical_entry_id
        assert refiled.entry.proposal_revision == 2
        assert refiled.entry.state is LogicalEntryState.AWAITING_REVIEW
        assert len(h.handoffs()) == 2

    def test_terminal_refusals_abandon_the_entry_and_release_the_reservation(
        self, tmp_path: Path
    ) -> None:
        """The terminal option of the named policy: at ``terminal_after``
        refusals the entry is abandoned outright and cannot leak its hold."""
        h = Harness(
            tmp_path,
            refusal_policy=RefusalCooldownPolicy(
                cooldown=dt.timedelta(minutes=1), terminal_after=1
            ),
        )
        h.reviewer.decision = ApprovalDecision.REFUSED
        entry = h.awaiting()
        h.reviewer.work(NOW)

        result = h.manager.service(entry, h.packet_for(entry), now=LATER)
        assert result.outcome is ServiceOutcome.REFUSED_TERMINAL
        assert result.entry.state is LogicalEntryState.ABANDONED
        assert result.entry.reservation_id is None
        # A fresh nomination for the underlying is now a fresh intention.
        fresh = h.manager.claim(nomination(), now=LATER)
        assert fresh.logical_entry_id != entry.logical_entry_id


# ===========================================================================
# Concurrency invariants
# ===========================================================================


class TestConcurrencyInvariants:
    def test_one_active_review_revision_per_entry(self, tmp_path: Path) -> None:
        """Supersession retires before it refiles: at every instant the entry
        names exactly one outstanding handoff, and the retired one is in the
        lineage, not current."""
        h = Harness(tmp_path)
        entry = h.awaiting(credit="1.50")
        first = entry.current_handoff_id
        second_result = h.manager.service(
            entry, h.packet_for(entry, credit="1.25"), now=LATER
        )
        updated = second_result.entry
        assert updated.current_handoff_id != first
        superseded_ids = {
            r.handoff_id
            for r in updated.lineage
            if r.outcome is RevisionOutcome.SUPERSEDED
        }
        assert superseded_ids == {first}

    def test_one_working_physical_order_per_entry(self, tmp_path: Path) -> None:
        """A second physical attempt while one is open refuses; resolution
        completes the entry and releases the reservation."""
        h = Harness(tmp_path)
        entry = h.awaiting()
        h.reviewer.work(NOW)
        approved = h.manager.service(entry, h.packet_for(entry), now=LATER).entry

        executing = h.manager.record_physical_attempt(approved, now=LATER)
        assert executing.state is LogicalEntryState.EXECUTING

        with pytest.raises(RefusedError) as exc:
            h.manager.record_physical_attempt(executing, now=LATER)
        assert "already has a working physical order" in exc.value.message

        filled = h.manager.record_physical_outcome(executing, filled=True, now=LATER)
        assert filled.state is LogicalEntryState.FILLED
        assert filled.reservation_id is None

    def test_a_failed_transmission_abandons_the_entry(self, tmp_path: Path) -> None:
        """The named transmit-failure policy: the consumed, spec-bound approval
        cannot cover a retry, so the entry ends rather than half-lives."""
        h = Harness(tmp_path)
        entry = h.awaiting()
        h.reviewer.work(NOW)
        approved = h.manager.service(entry, h.packet_for(entry), now=LATER).entry
        executing = h.manager.record_physical_attempt(approved, now=LATER)

        failed = h.manager.record_physical_outcome(
            executing, filled=False, detail="rejected by broker", now=LATER
        )
        assert failed.state is LogicalEntryState.ABANDONED
        assert failed.reservation_id is None
        assert RevisionOutcome.PHYSICAL_FAILED in [r.outcome for r in failed.lineage]

    def test_three_distinct_underlyings_await_review_concurrently(
        self, tmp_path: Path
    ) -> None:
        """Required test 12. Three entries, three outstanding handoffs, and an
        answer cross-bound to another entry's identity is refused by the gate
        -- no cross-talk."""
        h = Harness(tmp_path)
        spy = h.awaiting("SPY")
        aapl = h.awaiting("AAPL")
        qqq = h.awaiting("QQQ")

        entries = (spy, aapl, qqq)
        assert len({e.logical_entry_id for e in entries}) == 3
        assert len({e.current_handoff_id for e in entries}) == 3
        assert len(h.handoffs()) == 3
        for entry in entries:
            assert (
                h.manager.service(entry, h.packet_for(entry), now=NOW).outcome
                is ServiceOutcome.WAITING
            )

        # The reviewer answers all three -- but AAPL's answer is cross-bound to
        # SPY's identity. The gate must refuse it on the intent id.
        def cross_bind(fields: dict[str, Any]) -> None:
            if fields["intent_id"] == aapl.logical_entry_id:
                fields["intent_id"] = spy.logical_entry_id

        h.reviewer.mangle = cross_bind
        h.reviewer.work(NOW)

        for entry in (spy, qqq):
            result = h.manager.service(entry, h.packet_for(entry), now=LATER)
            assert result.outcome is ServiceOutcome.APPROVED
            assert result.approval.intent_id == entry.logical_entry_id

        with pytest.raises(RefusedError) as exc:
            h.manager.service(aapl, h.packet_for(aapl), now=LATER)
        assert "answers intent" in (exc.value.hint or "")

    def test_a_packet_carrying_a_foreign_intent_id_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The manager's own identity guard: a packet built with a fresh uuid4
        -- the exact defect this module removes -- is refused loudly, before
        anything is filed."""
        h = Harness(tmp_path)
        entry = h.manager.claim(nomination(), now=NOW)
        foreign_intent = spread()  # mints its own uuid4
        risk = approving_risk(foreign_intent.strategy_id)
        foreign_packet = build_packet(
            foreign_intent,
            risk=risk,
            governor=approving_governor(foreign_intent),
            context=h.context,
            now=NOW,
        )
        with pytest.raises(RefusedError) as exc:
            h.manager.propose(entry, foreign_packet, now=NOW)
        assert "logical identity" in exc.value.message
        assert len(h.handoffs()) == 0


# ===========================================================================
# Persistence discipline
# ===========================================================================


class TestPersistenceDiscipline:
    def test_the_entry_is_persisted_before_any_review_request_is_filed(
        self, tmp_path: Path
    ) -> None:
        """Contract step 2. A crash after claim leaves an entry that remembers
        itself and no handoff -- never a handoff nobody remembers filing."""
        h = Harness(tmp_path)
        entry = h.manager.claim(nomination(), now=NOW)

        on_disk = LogicalEntryStore(h.store.path).get(entry.logical_entry_id)
        assert on_disk is not None
        assert on_disk.state is LogicalEntryState.CLAIMED
        assert len(h.handoffs()) == 0

        h.manager.propose(entry, h.packet_for(entry), now=NOW)
        assert len(h.handoffs()) == 1

    def test_a_corrupt_line_degrades_the_replay_never_bricks_it(
        self, tmp_path: Path
    ) -> None:
        """The positions-store contract, inherited: garbage lines are skipped,
        a malformed event costs its own transition and is *recorded*, and the
        entry survives with its pending review intact."""
        h = Harness(tmp_path)
        entry = h.awaiting()

        with h.store.path.open("a", encoding="utf-8") as stream:
            stream.write("this is not json at all\n")
            stream.write(
                '{"v": 1, "event": "REVIEW_FILED", "logical_entry_id": "%s", '
                '"at": "2026-07-29T13:30:00+00:00", "revision": "not-an-int", '
                '"spec_digest": "x", "handoff_id": "y", '
                '"expires_at": "2026-07-30T01:00:00+00:00"}\n' % entry.logical_entry_id
            )

        problems: list[str] = []
        book = h.store.entries(errors=problems)
        survivor = book[entry.logical_entry_id]
        assert survivor.state is LogicalEntryState.AWAITING_REVIEW
        assert survivor.current_handoff_id == entry.current_handoff_id
        assert problems, "the malformed event was silently swallowed"
        assert h.store.integrity_errors()

    def test_the_full_lifecycle_survives_a_restart_at_every_stage(
        self, tmp_path: Path
    ) -> None:
        """Replay equals live state after claim, file, supersede, approve and
        execute -- the whole log is its own backup."""
        h = Harness(tmp_path)
        entry = h.awaiting(credit="1.50")
        entry = h.manager.service(entry, h.packet_for(entry, credit="1.25"), now=LATER).entry
        h.reviewer.work(NOW)
        entry = h.manager.service(entry, h.packet_for(entry, credit="1.25"), now=LATER).entry
        entry = h.manager.record_physical_attempt(entry, now=LATER)
        entry = h.manager.record_physical_outcome(entry, filled=True, now=LATER)

        replayed = LogicalEntryStore(h.store.path).get(entry.logical_entry_id)
        assert replayed is not None
        assert replayed.state is LogicalEntryState.FILLED
        assert replayed.proposal_revision == 2
        assert replayed.reservation_id is None
        assert [r.outcome for r in replayed.lineage] == [
            r.outcome for r in entry.lineage
        ]

    def test_a_lineage_record_refuses_nonsense(self) -> None:
        with pytest.raises(ValueError):
            LineageRecord(
                revision=-1,
                handoff_id="",
                outcome=RevisionOutcome.SUPERSEDED,
                at=NOW,
            )
        with pytest.raises(ValueError):
            LineageRecord(revision=1, handoff_id="", outcome="SUPERSEDED", at=NOW)  # type: ignore[arg-type]

    def test_a_nomination_refuses_a_market_value_free_for_all(self) -> None:
        """The identity is never derived from mutable market values -- and the
        nomination type itself refuses the obvious malformations."""
        good = nomination()
        with pytest.raises(ValueError):
            EntryNomination(
                underlying="",
                strategy_family=good.strategy_family,
                direction=good.direction,
                expiration=good.expiration,
                legs=good.legs,
                reservation_amount=good.reservation_amount,
            )
        with pytest.raises(ValueError):
            EntryNomination(
                underlying="SPY",
                strategy_family=good.strategy_family,
                direction=good.direction,
                expiration=good.expiration,
                legs=good.legs,
                reservation_amount=D("-1"),
            )
