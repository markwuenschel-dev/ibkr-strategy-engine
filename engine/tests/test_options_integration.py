"""M3<->M4 integration, wired end to end: ScanBook -> logical entry -> Grok gate
-> the one authorization corridor -> transmit.

Every test here drives the REAL pieces: ``run_once`` with a real
:class:`~engine.options.logical.LogicalEntryManager`, a real
:class:`~engine.options.approval.CollabVerifierGate` over a temp collab, the
:class:`reviewer.ScriptedReviewer` seat answering through the real handoff
lifecycle, a real persisted :class:`~engine.options.universe.ScanBook` on
disk, and the fakes the runner suite already trusts (``FakeIB``/``FakeBroker``
from ``test_options_runner``, the two-pass ``ScriptedMarketDataPort`` from
``integration_support``). No sockets, no mocked gate.

The audited checkpoint list (docs/INTEGRATION-M3-M4.md):

(a) bounded two-pass flow: nomination -> claim -> one handoff -> static pass
    reuses it -> reviewer answers -> consuming pass authorizes and transmits
    (unarmed variant refuses at arm; armed variant transmits);
(b) changed market: same id, revision 2, old approval unspent, old handoff
    withdrawn, scanbook row SUPERSEDED, no re-claim while the entry is active;
(c) restart mid-await: a fresh manager+store over the same files restores the
    pending review, files no duplicate, and the approval is then consumed;
(d) three underlyings awaiting concurrently, one approval consumed with no
    cross-talk;
(e) reservations visible to the governor (named test for mutation check 2);
(f) sweep at pass start expires an overdue entry and releases its reservation;
(g) the corridor is the ONLY authorize_open caller (named test for mutation
    check 1); plus the claim cap (named test for mutation check 3), the pacing
    priority correction, and the typed refusal classification with its pinned
    prose.
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import reviewer as reviewer_mod
from engine._collabkit import load as load_collabkit
from engine.errors import RefusedError
import engine.options as options_package
from engine.options.adapters import quote_priority
from engine.options.approval import ApprovalDecision, CollabVerifierGate
from engine.options.logical import (
    LogicalEntryManager,
    LogicalEntryState,
    LogicalEntryStore,
    RevisionOutcome,
    _gate_decision,
)
from engine.options.pacing import Priority
from engine.options.policy import RiskPolicy
from engine.options.runner import EntryMode, run_once
from engine.options.selection import Bias
from engine.options.universe import (
    CoverageSummary,
    NominatedLeg,
    ScanBook,
    ScanBookFileWriter,
    ScanBookRow,
    ScanBookTransitionError,
    ScanState,
    StructureNomination,
)
from engine.cycle_adapter import _identity
from engine.errors import ConfigError
from integration_support import ScriptedMarketDataPort
from test_options_runner import FakeBroker, FakeIB, FakePortfolioPort, gate_for, store_for

D = Decimal

#: One fixed logical clock for the whole file, aligned with
#: ``integration_support.NOW`` so the scripted port's quote timestamps and the
#: pass's ``now`` agree (quote age 0 on every pass).
NOW = dt.datetime(2026, 8, 3, 13, 0, tzinfo=dt.timezone.utc)
TODAY = NOW.date()
SECONDS_BETWEEN_PASSES = 60

EXPIRY = TODAY + dt.timedelta(days=45)


def test_cycle_identity_cannot_be_overridden_with_a_foreign_live_lease(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    paperday = state_dir / "paperday"
    paperday.mkdir(parents=True)
    (paperday / "session.lock").write_text(
        json.dumps({"session_id": "session-current", "fencing_token": "fence-current"}),
        encoding="utf-8",
    )
    (paperday / "scheduler.pid").write_text(
        json.dumps({"session_id": "session-current", "nonce": "nonce-current"}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="FAIL-STALE-PAPERDAY-AUTHORITY"):
        _identity(
            state_dir,
            SimpleNamespace(scheduler_session="session-current:nonce-foreign"),
        )

    assert _identity(state_dir, SimpleNamespace(scheduler_session=None)) == (
        "session-current",
        "nonce-current",
    )


def put_vertical_nomination(symbol: str) -> StructureNomination:
    """The 450/445 put credit spread every fake in this tree quotes."""
    return StructureNomination(
        underlying=symbol,
        family="PUT_CREDIT_SPREAD",
        direction="BULLISH",
        expiration=EXPIRY,
        legs=(
            NominatedLeg(con_id=450, strike=D("450"), right="P", action="SELL"),
            NominatedLeg(con_id=445, strike=D("445"), right="P", action="BUY"),
        ),
        short_delta=D("-0.30"),
        width=D("5"),
    )


def write_scanbook(
    state_dir: Path,
    symbols: tuple[str, ...],
    *,
    at: dt.datetime = NOW,
    session: dt.date = TODAY,
) -> ScanBook:
    """A persisted ScanBook whose rank order follows ``symbols`` order."""
    rows = tuple(
        ScanBookRow(
            symbol=symbol,
            state=ScanState.CANDIDATE,
            nomination=put_vertical_nomination(symbol),
            rank_score=D(100 - index),
            evaluated_at=at,
        )
        for index, symbol in enumerate(symbols)
    )
    book = ScanBook(
        session_date=session,
        generated_at=at,
        rows=rows,
        coverage=CoverageSummary.from_rows(rows),
    )
    book.write(state_dir)
    return book


def read_row(state_dir: Path, symbol: str, *, session: dt.date = TODAY) -> ScanBookRow:
    book = ScanBook.read(state_dir, session)
    assert book is not None
    for row in book.rows:
        if row.symbol == symbol:
            return row
    raise AssertionError(f"no scanbook row for {symbol}")


class Rig:
    """Real gate + reviewer + manager + persisted book, over the runner fakes."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        symbols: tuple[str, ...] = ("SPY",),
        pass_factors: tuple[Decimal, ...] = (D("1"),),
        allowlist: tuple[str, ...] = ("SPY", "AAPL", "MSFT", "QQQ"),
        policy: RiskPolicy | None = None,
        market: Any = None,
        write_book: bool = True,
    ) -> None:
        self.tmp_path = tmp_path
        self.gate = gate_for(tmp_path, symbol_allowlist=allowlist)
        self.state_dir = self.gate.config.state_dir
        self.store = store_for(tmp_path)
        self.root = reviewer_mod.collab_at(tmp_path)
        self.verifier = CollabVerifierGate(
            root=self.root, ledger=tmp_path / "state" / "verification"
        )
        self.reviewer = reviewer_mod.ScriptedReviewer(root=self.root)
        self.context = reviewer_mod.approval_context()
        self.lstore = LogicalEntryStore(self.state_dir / "logical_entries.jsonl")
        self.manager = LogicalEntryManager(store=self.lstore, gate=self.verifier)
        self.market = market if market is not None else ScriptedMarketDataPort(
            pass_factors=pass_factors
        )
        self.ib = FakeIB(today=TODAY)
        self.broker = FakeBroker(ib=self.ib)
        self.policy = policy or RiskPolicy()
        self.portfolio = FakePortfolioPort()
        self.pass_index = 0
        if write_book:
            write_scanbook(self.state_dir, symbols)

    @property
    def now(self) -> dt.datetime:
        return NOW + dt.timedelta(seconds=self.pass_index * SECONDS_BETWEEN_PASSES)

    def run(
        self,
        *,
        armed: bool = False,
        now: dt.datetime | None = None,
        manager: Any = None,
        **extra: Any,
    ) -> Any:
        report = run_once(
            self.broker,
            gate=self.gate,
            journal=self.gate.journal,
            store=self.store,
            policy=self.policy,
            armed=armed,
            symbol="SPY",
            bias=Bias.BULLISH,
            market_data=self.market,
            portfolio=self.portfolio,
            now=now if now is not None else self.now,
            today=TODAY,
            account="DU1234567",
            verifier=self.verifier,
            approval_context=self.context,
            entry_mode=EntryMode.FULL,
            session_lease=extra.pop("session_lease", lambda: None),
            manager=manager if manager is not None else self.manager,
            scanbook_root=self.state_dir,
            **extra,
        )
        self.pass_index += 1
        self.market.next_pass()
        return report

    # -- readbacks --------------------------------------------------------

    def handoffs(self) -> list[Any]:
        paths = load_collabkit("paths", "CollabPaths").at(self.root)
        store = load_collabkit("store", "HandoffStore")(paths)
        return list(
            store.list(("pending", "claimed", "done", "archive"), to="reviewer")
        )

    def handoff(self, handoff_id: str) -> Any:
        paths = load_collabkit("paths", "CollabPaths").at(self.root)
        store = load_collabkit("store", "HandoffStore")(paths)
        return store.find(handoff_id)

    def consumed(self) -> list[Path]:
        return list((Path(self.verifier.ledger) / "consumed").glob("*.used"))

    def entry(self, symbol: str = "SPY") -> Any:
        entries = [
            e
            for e in self.lstore.entries().values()
            if e.normalized_underlying == symbol
        ]
        assert entries, f"no logical entry for {symbol}"
        assert len(entries) == 1, f"{len(entries)} entries for {symbol}"
        return entries[0]

    def assert_no_discovery_paced(self, report: Any) -> None:
        """Contract section 6: DiscoveryPaced in a RunReport is a defect
        signature -- some caller mislabeled its acquire as DISCOVERY."""
        assert not any("DiscoveryPaced" in error for error in report.errors), (
            report.errors
        )


# ===========================================================================
# (a) the bounded multi-pass flow
# ===========================================================================


class TestBoundedFlow:
    def test_claim_files_exactly_one_handoff_and_marks_the_row(
        self, tmp_path: Path
    ) -> None:
        rig = Rig(tmp_path, pass_factors=(D("1"), D("1"), D("1"), D("1")))
        report = rig.run()

        entry = rig.entry()
        assert entry.state is LogicalEntryState.AWAITING_REVIEW
        assert entry.proposal_revision == 1
        assert len(rig.handoffs()) == 1, "revision 1 must file exactly once"
        assert "OPTIONS_AWAITING_VERIFICATION" in report.refusal_codes

        row = read_row(rig.state_dir, "SPY")
        assert row.state is ScanState.CLAIMED_BY_LOGICAL_ENTRY
        assert row.claim_reference == str(entry.logical_entry_id)
        rig.assert_no_discovery_paced(report)

    def test_a_static_second_pass_reuses_the_outstanding_request(
        self, tmp_path: Path
    ) -> None:
        rig = Rig(tmp_path, pass_factors=(D("1"), D("1")))
        rig.run()
        first = rig.entry()

        report = rig.run()
        second = rig.entry()
        assert second.logical_entry_id == first.logical_entry_id
        assert second.proposal_revision == 1
        assert second.current_spec_digest == first.current_spec_digest
        assert len(rig.handoffs()) == 1, "a static market must not re-file"
        assert "OPTIONS_AWAITING_VERIFICATION" in report.refusal_codes

    def test_unarmed_consumption_pass_refuses_at_arm_and_spends_nothing(
        self, tmp_path: Path
    ) -> None:
        rig = Rig(tmp_path, pass_factors=(D("1"), D("1"), D("1")))
        rig.run()
        rig.run()
        rig.reviewer.work(rig.now)

        report = rig.run(armed=False)
        assert rig.ib.placed == [], "an unarmed pass must transmit nothing"
        assert any("armed" in blocker for blocker in report.blockers), report.blockers
        assert rig.consumed() == [], "the arm refusal must not spend the approval"
        entry = rig.entry()
        assert entry.state is LogicalEntryState.APPROVED_PENDING_EXECUTION

    def test_armed_consumption_pass_transmits_through_the_corridor(
        self, tmp_path: Path
    ) -> None:
        rig = Rig(tmp_path, pass_factors=(D("1"), D("1"), D("1")))
        rig.run()
        rig.reviewer.work(rig.now)

        report = rig.run(armed=True)
        assert len(rig.ib.placed) == 1, report.describe()
        assert report.entered is True

        entry = rig.entry()
        assert entry.state is LogicalEntryState.FILLED
        assert entry.reservation_id is None, "a fill hands over to the store"
        assert [
            r.outcome
            for r in entry.lineage
            if r.outcome
            in (RevisionOutcome.PHYSICAL_SUBMITTED, RevisionOutcome.PHYSICAL_FILLED)
        ] == [RevisionOutcome.PHYSICAL_SUBMITTED, RevisionOutcome.PHYSICAL_FILLED]

        # The transmitted order IS the logical identity: one auditable id from
        # scanbook row through entry to position store.
        positions = rig.store.open_positions()
        assert len(positions) == 1
        assert positions[0].strategy_id == entry.logical_entry_id
        _bag, order = rig.ib.placed[0]
        assert order.orderRef == str(entry.logical_entry_id)
        assert len(rig.consumed()) == 1
        rig.assert_no_discovery_paced(report)


# ===========================================================================
# (b) changed market between passes
# ===========================================================================


class TestChangedMarket:
    def test_a_moved_market_supersedes_the_revision_not_the_entry(
        self, tmp_path: Path
    ) -> None:
        rig = Rig(tmp_path, pass_factors=(D("1"), D("1.2"), D("1.2")))
        rig.run()
        first = rig.entry()
        first_handoff = first.current_handoff_id
        rig.reviewer.work(rig.now)  # revision 1's approval lands on disk

        report = rig.run()  # market moved 20%
        entry = rig.entry()
        assert entry.logical_entry_id == first.logical_entry_id
        assert entry.proposal_revision == 2
        assert entry.current_spec_digest != first.current_spec_digest
        assert entry.current_handoff_id != first_handoff
        assert len(rig.handoffs()) == 2

        # The old approval was never touched, let alone consumed.
        assert rig.consumed() == []
        # The retired revision's request is closed -- withdrawn by the
        # builder, or already completed by the reviewer's own reply (whose
        # completion note withdraw never overwrites). Either way, the
        # reviewer's queue holds no orphaned open request.
        withdrawn = rig.handoff(first_handoff)
        assert str(withdrawn.status) in ("done", "archive")
        note = str(getattr(withdrawn, "note", "") or "")
        assert "SUPERSEDED" in note or "answered by" in note, note
        # And the claimed scanbook row was retired with it.
        assert read_row(rig.state_dir, "SPY").state is ScanState.SUPERSEDED
        assert "OPTIONS_AWAITING_VERIFICATION" in report.refusal_codes

    def test_a_fresh_book_does_not_reclaim_while_the_entry_is_active(
        self, tmp_path: Path
    ) -> None:
        rig = Rig(tmp_path, pass_factors=(D("1"), D("1.2"), D("1.2")))
        rig.run()
        original = rig.entry()
        rig.run()  # supersession pass

        # A newer scan re-nominates SPY. The active entry owns the underlying:
        # the claim path must return to it, never mint a second identity.
        write_scanbook(rig.state_dir, ("SPY",), at=rig.now)
        rig.run()
        entries = list(rig.lstore.entries().values())
        assert len(entries) == 1
        assert entries[0].logical_entry_id == original.logical_entry_id
        assert read_row(rig.state_dir, "SPY").state is ScanState.CANDIDATE


# ===========================================================================
# (c) restart mid-await
# ===========================================================================


class TestRestartMidAwait:
    def test_a_new_manager_over_the_same_files_completes_the_review(
        self, tmp_path: Path
    ) -> None:
        rig = Rig(tmp_path, pass_factors=(D("1"), D("1"), D("1")))
        rig.run()
        before = rig.entry()

        # Process death: a NEW store and manager over the same files.
        restarted = LogicalEntryManager(
            store=LogicalEntryStore(rig.lstore.path), gate=rig.verifier
        )
        restored = restarted.active_for("SPY")
        assert restored is not None
        assert restored.logical_entry_id == before.logical_entry_id
        assert restored.current_handoff_id == before.current_handoff_id

        report = rig.run(manager=restarted)
        assert len(rig.handoffs()) == 1, "the restart filed a duplicate handoff"
        assert "OPTIONS_AWAITING_VERIFICATION" in report.refusal_codes

        rig.reviewer.work(rig.now)
        report = rig.run(manager=restarted, armed=True)
        assert report.entered is True
        assert len(rig.consumed()) == 1
        assert restarted.store.get(before.logical_entry_id).state is (
            LogicalEntryState.FILLED
        )


# ===========================================================================
# (d) three underlyings, no cross-talk
# ===========================================================================


class TestConcurrentUnderlyings:
    def test_one_approval_is_consumed_without_cross_talk(
        self, tmp_path: Path
    ) -> None:
        rig = Rig(
            tmp_path,
            symbols=("SPY", "AAPL", "MSFT"),
            pass_factors=tuple(D("1") for _ in range(10)),
        )
        rig.run()  # claims SPY (rank 1)
        rig.run()  # services SPY, claims AAPL
        rig.run()  # services both, claims MSFT
        # Each claim folds a new reservation into every later binding pass,
        # which moves the pending packets' governor numbers and supersedes
        # their revisions -- the invalidation rule doing its job. Two settle
        # passes later every packet binds the full three-reservation book and
        # the digests are stable.
        rig.run()
        rig.run()

        entries = {e.normalized_underlying: e for e in rig.lstore.entries().values()}
        assert set(entries) == {"SPY", "AAPL", "MSFT"}
        assert all(
            e.state is LogicalEntryState.AWAITING_REVIEW for e in entries.values()
        )
        assert len({e.current_handoff_id for e in entries.values()}) == 3

        # The reviewer answers ONLY SPY's current request. Superseded
        # revisions' requests were withdrawn, so they are no longer pending
        # and the reviewer's queue holds exactly the three live questions.
        rig.reviewer.answered = [
            entries["AAPL"].current_handoff_id,
            entries["MSFT"].current_handoff_id,
        ]
        rig.reviewer.work(rig.now)

        report = rig.run(armed=True)
        assert report.entered is True
        assert len(rig.ib.placed) == 1
        assert len(rig.consumed()) == 1

        after = {e.normalized_underlying: e for e in rig.lstore.entries().values()}
        assert after["SPY"].state is LogicalEntryState.FILLED
        for symbol in ("AAPL", "MSFT"):
            assert after[symbol].state is LogicalEntryState.AWAITING_REVIEW
            assert after[symbol].current_handoff_id == entries[symbol].current_handoff_id
            assert after[symbol].reservation_id is not None


# ===========================================================================
# (e) reservations visible to the governor -- NAMED TEST, mutation check 2:
# drop the reservation folding and these fail.
# ===========================================================================


class TestReservationsVisibleToTheGovernor:
    def _rig(self, tmp_path: Path) -> Rig:
        policy = RiskPolicy()
        policy = dataclasses.replace(
            policy,
            # 0.18% of the 1,000,000 fake net liquidation = 1,800: three
            # pending 500 reservations plus a 500 candidate breach it; the
            # candidate alone (500) does not. Incremental stays above one
            # candidate's 500 (policy refuses incremental > total).
            max_total_bpr_fraction=D("0.0018"),
            max_incremental_bpr_fraction=D("0.0006"),
            # This scenario is deliberately sized in flat dollars (the 500
            # reservations the comment above describes) against a governor
            # cap narrowed far below the equity-fraction sizing default --
            # unrelated to what this test checks, so sizing stays flat.
            risk_budget_fraction_of_equity=None,
            sectors=policy.sectors + (("QQQ", "BROAD_MARKET"),),
            correlation_groups=policy.correlation_groups + (("QQQ", "US_LARGE_CAP"),),
        )
        return Rig(
            tmp_path,
            symbols=("SPY", "AAPL", "MSFT", "QQQ"),
            pass_factors=tuple(D("1") for _ in range(8)),
            policy=policy,
        )

    def test_reservations_are_visible_to_the_governor(self, tmp_path: Path) -> None:
        """A fourth claim is refused by the total-BPR cap only because the
        three pending entries' reservations are folded into the snapshot."""
        rig = self._rig(tmp_path)
        rig.run(max_pending_entries=10)
        rig.run(max_pending_entries=10)
        rig.run(max_pending_entries=10)
        assert len(rig.lstore.entries()) == 3

        report = rig.run(max_pending_entries=10)
        assert "GOVERNOR_TOTAL_BPR_EXCEEDED" in report.refusal_codes, (
            report.refusal_codes
        )
        assert len(rig.lstore.entries()) == 3, "the fourth claim must not happen"
        assert rig.lstore.active_for("QQQ") is None

        # And the refusing snapshot really contained the reservations, keyed
        # by logical_entry_id (the fold's dedupe key).
        entry_ids = {e.logical_entry_id for e in rig.lstore.entries().values()}
        folded_ids = {
            p.strategy_id for p in report.portfolio.positions if p.strategy_id
        }
        assert entry_ids <= folded_ids

    def test_the_snapshot_folds_pending_reservations_beside_store_exposures(
        self, tmp_path: Path
    ) -> None:
        rig = self._rig(tmp_path)
        rig.run(max_pending_entries=10)  # SPY pending
        spy = rig.entry("SPY")
        report = rig.run(max_pending_entries=10)  # claims AAPL with SPY folded
        folded = {
            p.strategy_id: p for p in report.portfolio.positions if p.strategy_id
        }
        assert spy.logical_entry_id in folded
        assert folded[spy.logical_entry_id].buying_power_reserved == D("500")


# ===========================================================================
# claim cap -- NAMED TEST, mutation check 3: remove the cap and this fails.
# ===========================================================================


class TestPendingVerificationCap:
    def test_the_pending_verification_cap_refuses_a_fourth_claim(
        self, tmp_path: Path
    ) -> None:
        policy = RiskPolicy()
        policy = dataclasses.replace(
            policy,
            sectors=policy.sectors + (("QQQ", "BROAD_MARKET"),),
            correlation_groups=policy.correlation_groups + (("QQQ", "US_LARGE_CAP"),),
        )
        rig = Rig(
            tmp_path,
            symbols=("SPY", "AAPL", "MSFT", "QQQ"),
            pass_factors=tuple(D("1") for _ in range(8)),
            policy=policy,
        )
        rig.run()
        rig.run()
        rig.run()
        assert len(rig.lstore.entries()) == 3

        report = rig.run()  # default cap: 3 pending
        assert "OPTIONS_LOGICAL_PENDING_CAP" in report.refusal_codes
        assert len(rig.lstore.entries()) == 3
        assert rig.lstore.active_for("QQQ") is None


# ===========================================================================
# (f) the sweep at pass start
# ===========================================================================


class TestSweep:
    def test_an_overdue_review_is_expired_and_its_reservation_released(
        self, tmp_path: Path
    ) -> None:
        rig = Rig(tmp_path, pass_factors=(D("1"), D("1")))
        rig.run()
        entry = rig.entry()
        handoff_id = entry.current_handoff_id
        assert entry.reservation_id is not None

        # 13 hours later: past the 12-hour approval TTL that bounds a review.
        report = rig.run(now=NOW + dt.timedelta(hours=13))

        swept = rig.lstore.get(entry.logical_entry_id)
        assert swept.state is LogicalEntryState.EXPIRED
        assert swept.reservation_id is None
        assert any(
            r.outcome is RevisionOutcome.RESERVATION_RELEASED for r in swept.lineage
        )
        assert any("EXPIRED" in blocker for blocker in report.blockers), (
            report.blockers
        )
        # The orphaned request was withdrawn through the gate.
        withdrawn = rig.handoff(handoff_id)
        assert str(withdrawn.status) in ("done", "archive")


# ===========================================================================
# (g) no-bypass -- NAMED TEST, mutation check 1: route the manager path (or
# any path) around the corridor and this fails.
# ===========================================================================


class TestNoBypass:
    def test_the_corridor_is_the_only_authorize_open_caller(self) -> None:
        """AST enumeration over the whole package: every ``authorize_open``
        CALL in src/engine lives in the corridor or the frozen walk
        experiment. ``logical.py`` and ``universe.py`` have zero."""
        package_root = Path(options_package.__file__).parent.parent
        callers: set[tuple[str, str | None]] = set()
        for path in sorted(package_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            stack: list[tuple[ast.AST, str | None]] = [(tree, None)]
            while stack:
                node, function = stack.pop()
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    function = node.name
                if isinstance(node, ast.Call):
                    func = node.func
                    name = (
                        func.id
                        if isinstance(func, ast.Name)
                        else func.attr
                        if isinstance(func, ast.Attribute)
                        else None
                    )
                    if name == "authorize_open":
                        callers.add((path.name, function))
                for child in ast.iter_child_nodes(node):
                    stack.append((child, function))
        assert callers == {
            ("runner.py", "_authorize_and_transmit_entry"),
            ("walk.py", "run"),  # the frozen execution experiment, not M3/M4
        }, callers

    def test_grep_level_guard_over_the_lane_modules(self) -> None:
        """The plain-text half (the M9 audit's D5 lesson: one AST guard has
        holes). The lane modules never even name the authorization surface."""
        package_root = Path(options_package.__file__).parent
        for module in ("logical.py", "universe.py", "universe_data.py"):
            source = (package_root / module).read_text(encoding="utf-8")
            for banned in ("authorize_open", "place_combo", "authorize_close"):
                assert banned not in source, f"{module} names {banned}"


# ===========================================================================
# pacing priority correction (contract section 6)
# ===========================================================================


class PriorityRecordingPort(ScriptedMarketDataPort):
    """A scripted port that also records the per-call pacing priority."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.priorities: list[tuple[Any, bool]] = []

    def strategy_quotes(
        self,
        *,
        underlying_symbol: str,
        con_ids: Any,
        require_two_sided: bool = False,
        budget_priority: Any = None,
    ) -> Any:
        self.priorities.append((budget_priority, require_two_sided))
        return super().strategy_quotes(
            underlying_symbol=underlying_symbol,
            con_ids=con_ids,
            require_two_sided=require_two_sided,
        )


class TestPacingPriority:
    def test_explicit_priority_outranks_the_two_sided_heuristic(self) -> None:
        assert (
            quote_priority(require_two_sided=True, per_call=Priority.AUTHORIZATION)
            is Priority.AUTHORIZATION
        )
        assert (
            quote_priority(require_two_sided=True, per_call=None)
            is Priority.EXITS_MANAGEMENT
        ), "the marking path's heuristic must survive for callers stating nothing"
        assert (
            quote_priority(require_two_sided=False, per_call=None)
            is Priority.CANDIDATE_CONSTRUCTION
        )
        assert (
            quote_priority(
                require_two_sided=False,
                per_call=None,
                instance_default=Priority.DISCOVERY,
            )
            is Priority.DISCOVERY
        )

    def test_the_binding_revalidation_draws_at_authorization(
        self, tmp_path: Path
    ) -> None:
        """The corridor's two-sided re-quote states AUTHORIZATION explicitly;
        the pricing rebuild states nothing (and so would fall to the
        heuristic/default, never the management reserve)."""
        port = PriorityRecordingPort(pass_factors=(D("1"), D("1")))
        rig = Rig(tmp_path, market=port)
        rig.run()

        binding = [p for p, two_sided in port.priorities if two_sided and p is not None]
        assert binding, f"no explicit-priority two-sided call: {port.priorities}"
        assert all(p is Priority.AUTHORIZATION for p in binding)
        # No corridor call ever states a management-reserve priority.
        assert all(
            p is not Priority.EXITS_MANAGEMENT for p, _ in port.priorities
        )


# ===========================================================================
# typed refusal classification (review minor)
# ===========================================================================


class TestGateRefusalClassification:
    def test_a_typed_decision_attribute_outranks_the_prose(self) -> None:
        refusal = RefusedError("anything at all")
        refusal.decision = ApprovalDecision.UNAVAILABLE
        assert _gate_decision(refusal) is ApprovalDecision.UNAVAILABLE

    def test_the_prose_fallback_is_pinned_to_approval_py(self) -> None:
        """approval.py is frozen for this integration, so the classification
        falls back to the exact prose its ``require`` composes. This test
        pins that prose: reword the sentence and this fails loudly, which is
        the alarm the substring match needs."""
        import engine.options.approval as approval_module

        source = Path(approval_module.__file__).read_text(encoding="utf-8")
        assert (
            'f"the verifier answered {worst.decision.value} for trade intent "'
            in source
        )
        assert _gate_decision(
            RefusedError("the verifier answered REFUSED for trade intent x")
        ) is ApprovalDecision.REFUSED
        assert _gate_decision(
            RefusedError("the verifier answered UNAVAILABLE for trade intent x")
        ) is ApprovalDecision.UNAVAILABLE
        assert _gate_decision(RefusedError("some other refusal")) is None

    def test_a_real_refusal_routes_into_the_cooldown(self, tmp_path: Path) -> None:
        rig = Rig(tmp_path, pass_factors=(D("1"), D("1")))
        rig.reviewer.decision = ApprovalDecision.REFUSED
        rig.run()
        rig.reviewer.work(rig.now)

        report = rig.run()
        entry = rig.entry()
        assert entry.state is LogicalEntryState.REFUSED_COOLDOWN
        assert entry.refusal_count == 1
        assert any("REFUSED" in blocker for blocker in report.blockers)
        assert rig.ib.placed == []

    def test_a_real_unavailable_keeps_the_entry_waiting(self, tmp_path: Path) -> None:
        rig = Rig(tmp_path, pass_factors=(D("1"), D("1")))
        rig.reviewer.decision = ApprovalDecision.UNAVAILABLE
        rig.run()
        rig.reviewer.work(rig.now)

        report = rig.run()
        entry = rig.entry()
        assert entry.state is LogicalEntryState.AWAITING_REVIEW
        assert any("UNAVAILABLE" in blocker for blocker in report.blockers)


# ===========================================================================
# scanbook loader refusals: no --symbol fallback
# ===========================================================================


class TestScanbookAdmission:
    @pytest.mark.parametrize(
        ("scenario", "expected_code"),
        (
            ("missing", "SCANBOOK_MISSING"),
            ("stale", "SCANBOOK_STALE"),
            ("future", "SCANBOOK_FUTURE"),
            ("session-mismatched", "SCANBOOK_SESSION_MISMATCH"),
        ),
    )
    def test_refusal_cases_do_not_resurrect_the_symbol_fallback(
        self, tmp_path: Path, scenario: str, expected_code: str
    ) -> None:
        """Mutation guard: if the refusal branch falls through to legacy
        ``--symbol`` handling, any case here would mint a candidate or handoff."""
        rig = Rig(tmp_path, write_book=False)
        if scenario == "stale":
            write_scanbook(rig.state_dir, ("SPY",), at=NOW - dt.timedelta(hours=2))
        elif scenario == "future":
            write_scanbook(rig.state_dir, ("SPY",), at=NOW + dt.timedelta(minutes=1))
        elif scenario == "session-mismatched":
            stale_session = TODAY - dt.timedelta(days=1)
            rows = (
                ScanBookRow(
                    symbol="SPY",
                    state=ScanState.CANDIDATE,
                    nomination=put_vertical_nomination("SPY"),
                    rank_score=D("100"),
                    evaluated_at=NOW,
                ),
            )
            book = ScanBook(
                session_date=stale_session,
                generated_at=NOW,
                rows=rows,
                coverage=CoverageSummary.from_rows(rows),
            )
            path = ScanBook.path_for(rig.state_dir, TODAY)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(book.to_record()), encoding="utf-8")

        report = rig.run()
        assert expected_code in report.refusal_codes
        assert report.candidate is None
        assert rig.lstore.entries() == {}, "a refused book must not mint entries"
        assert rig.handoffs() == []

    def test_no_fallback_fires_beside_pending_entries(self, tmp_path: Path) -> None:
        """A fallback filed beside pending entries would mint per-pass
        fresh-id reviews -- the orphaning defect M4 removes."""
        rig = Rig(tmp_path, pass_factors=(D("1"), D("1")))
        rig.run()
        assert len(rig.handoffs()) == 1

        # Next session-shaped pass: the book is now stale, but SPY is pending.
        (ScanBook.path_for(rig.state_dir, TODAY)).unlink()
        report = rig.run()
        assert "SCANBOOK_MISSING" in report.refusal_codes
        assert len(rig.handoffs()) == 1, "a missing book filed a fresh-id review"
        assert report.candidate is not None  # the serviced entry's rebuild


class TestProductionVerifierBoundary:
    def test_missing_verifier_blocks_before_legacy_candidate_or_claim_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = Rig(tmp_path, write_book=False)
        candidate_calls: list[str] = []
        claim_calls: list[str] = []

        def fail_if_legacy_candidate_is_built(**_: Any) -> Any:
            candidate_calls.append("legacy")
            raise AssertionError("production entry reached legacy candidate construction")

        class ClaimProbe:
            def mark_claimed(self, symbol: str, **_: Any) -> bool:
                claim_calls.append(symbol)
                raise AssertionError("production entry attempted a ScanBook claim")

        monkeypatch.setattr(
            "engine.options.runner._build_candidate", fail_if_legacy_candidate_is_built
        )

        report = run_once(
            rig.broker,
            gate=rig.gate,
            journal=rig.gate.journal,
            store=rig.store,
            policy=rig.policy,
            armed=True,
            symbol="SPY",
            bias=Bias.BULLISH,
            market_data=rig.market,
            portfolio=rig.portfolio,
            now=NOW,
            today=TODAY,
            account="DU1234567",
            entry_mode=EntryMode.FULL,
            verifier=None,
            approval_context=None,
            manager=None,
            scanbook_writer=ClaimProbe(),
            session_id="paperday-production",
            session_lease=lambda: None,
        )

        assert "OPTIONS_VERIFIER_NOT_CONFIGURED" in report.refusal_codes
        assert report.entry_refusal_code == "OPTIONS_VERIFIER_NOT_CONFIGURED"
        assert report.candidate is None
        assert report.transmissions == []
        assert rig.broker.ib.placed == []
        assert candidate_calls == []
        assert claim_calls == []
        assert rig.lstore.entries() == {}


# ===========================================================================
# the persisted claim-writer seam
# ===========================================================================


class TestLogicalEntriesCommand:
    def test_the_operator_window_is_read_only_and_registered(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        from engine.cli import COMMANDS, build_parser, cmd_logical_entries

        assert "logical-entries" in COMMANDS
        parser = build_parser()
        # No --arm exists on this command, by construction.
        with pytest.raises(SystemExit):
            parser.parse_args(["logical-entries", "--arm"])

        # Seed one pending entry through the real flow, then read it back.
        rig = Rig(tmp_path, pass_factors=(D("1"),))
        rig.run()
        entry = rig.entry()
        capsys.readouterr()

        args = parser.parse_args(
            [
                "--account",
                "DU1234567",
                "--port",
                "7497",
                "--state-dir",
                str(rig.state_dir),
                "logical-entries",
                "--all",
            ]
        )
        assert cmd_logical_entries(args) == 0
        printed = capsys.readouterr().out
        assert str(entry.logical_entry_id) in printed
        assert "AWAITING_REVIEW" in printed
        assert entry.current_spec_digest[:12] in printed
        assert str(entry.reservation_amount) in printed


class TestScanBookFileWriter:
    def _writer(self, tmp_path: Path) -> ScanBookFileWriter:
        write_scanbook(tmp_path, ("SPY",))
        return ScanBookFileWriter(tmp_path, TODAY)

    def test_cas_claim_then_supersede(self, tmp_path: Path) -> None:
        from uuid import uuid4

        writer = self._writer(tmp_path)
        entry_id = uuid4()
        assert writer.mark_claimed("SPY", entry_id=entry_id, at=NOW) is True
        # Idempotent re-claim by the same entry; a different entry raises.
        assert writer.mark_claimed("SPY", entry_id=entry_id, at=NOW) is True
        with pytest.raises(ScanBookTransitionError):
            writer.mark_claimed("SPY", entry_id=uuid4(), at=NOW)
        assert writer.mark_superseded("SPY", reason="newer book", at=NOW) is True
        # A superseded row is terminal for both writers.
        assert writer.mark_superseded("SPY", reason="again", at=NOW) is False
        assert writer.mark_claimed("SPY", entry_id=entry_id, at=NOW) is False

    def test_unknown_rows_raise(self, tmp_path: Path) -> None:
        from uuid import uuid4

        writer = self._writer(tmp_path)
        with pytest.raises(ScanBookTransitionError):
            writer.mark_claimed("QQQ", entry_id=uuid4(), at=NOW)
