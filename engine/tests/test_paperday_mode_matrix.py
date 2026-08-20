"""D14: prove non-ARMED autotrader modes cannot transmit an order, ever.

``docs/paper-day-recovery/decisions.md`` D14 flags ``armed=True`` at
``cycle_adapter.py`` (inside ``session_lease()``) as authority-sensitive and
defers a ruling until a mode matrix and negative tests prove REVIEW_ONLY
cannot transmit. ``docs/paper-day-recovery/design.md``'s "Ordering (N4)"
section makes this file's existence -- not a particular verdict -- the merge
gate for anything authority-sensitive that follows (P2, the recovery verb).

Two independent layers, because either one alone has a hole:

**Layer 1 (routing).** ``engine.cycle_adapter._CycleRuntime.entry`` is the
*only* place a cycle decides whether to call the strategy runner at all, and
with what ``armed`` value. ``TestModeToArmedRouting`` drives the real
``entry()`` method for every ``(mode, --arm flag)`` combination this
codebase's schema allows and records the exact ``armed=`` keyword the real
``engine.options.runner.run_once`` receives (or proves it is never called at
all, for DRY_RUN/SHADOW). This is where D14's line lives, so this is the
layer that answers D14 directly.

**Layer 2 (behaviour).** Layer 1 proves *what gets passed*; it does not by
itself prove that an ``armed=False`` pass which reaches a fully-built,
reviewer-approved candidate actually refuses to send it. ``TestNonArmed*``
runs the REAL corridor (``run_once`` -> ``_authorize_and_transmit_entry`` ->
``authorize_open`` -> ``place_combo``) end to end, through the same
``Rig``/``FakeIB`` harness ``test_options_integration.py`` already trusts, and
watches ``FakeIB.placed`` -- the one list that would hold a transmitted order.

A suite that only ever proves "nothing transmits" is not a proof of the mode
gate; it could just as easily be proving every path is broken.
``TestArmedFullArmCanTransmit`` is the control: the one (mode, mandate, --arm)
combination the schema says SHOULD be able to open risk, run through the same
harness, asserting ``FakeIB.placed`` is non-empty.

Explicitly NOT covered here (see the task report for why):
* paperday.py's `_read_json`/gate internals -- a parallel lane may be editing
  them; every observation in this file is either a public mode/policy value,
  cycle_adapter's own transmit-vs-refuse decision, or the AST/corridor-level
  behaviour ``test_options_no_transmit.py`` already certifies.
* P2 (the recovery verb) and its authority state machine.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import engine.cycle_adapter as cycle_adapter_module
from engine.autocycle import AutoCycleConfig, CycleContext, CycleError, CycleMode, PhaseContext
from engine.autotrader_policy import ARMED, DRY_RUN, FULL, MANAGE_ONLY, REVIEW_ONLY, SHADOW
from engine.cycle_adapter import _CycleRuntime
from engine.options.logical import LogicalEntryState

from test_options_integration import Rig

D = Decimal

CYCLE_STARTED_AT = dt.datetime(2026, 8, 20, 13, 0, tzinfo=dt.timezone.utc)


# ===========================================================================
# shared builders
# ===========================================================================


def _auto_cycle_config(
    tmp_path: Path, *, mode: str, mandate: str = FULL
) -> AutoCycleConfig:
    return AutoCycleConfig(
        mandate=mandate,
        mode=CycleMode(mode),
        management_seconds=300,
        discovery_seconds=1800,
        probe_seconds=600,
        entry_seconds=300,
        missed_tick_policy="SKIP_MISSED_TICKS",
        entry_start=dt.time(10, 0),
        entry_end=dt.time(15, 0),
        coverage_sla_seconds=600,
        max_pending_entries=3,
        max_new_entries_per_pass=1,
        phase2_limit=5,
        policy_hash="a" * 64,
        catalog_hash="b" * 64,
        state_dir=tmp_path,
    )


def _phase_context(
    tmp_path: Path, *, mode: str, arm: bool, mandate: str = FULL
) -> PhaseContext:
    config = _auto_cycle_config(tmp_path, mode=mode, mandate=mandate)
    cycle = CycleContext(
        session_id="session-1",
        lease_nonce="nonce-1",
        tick_id="tick-1",
        attempt_id="attempt-1",
        policy_hash="a" * 64,
        catalog_hash="b" * 64,
        started_at=CYCLE_STARTED_AT,
        session_date=CYCLE_STARTED_AT.date(),
    )
    return PhaseContext(
        cycle=cycle,
        broker=SimpleNamespace(ib=object()),
        pacing=None,
        config=config,
        arm=arm,
        due=(),
    )


def _bare_runtime(tmp_path: Path, *, mode: str) -> _CycleRuntime:
    """A ``_CycleRuntime`` built with ``__new__``, the same pattern
    ``test_options_cycle_cli.py``'s ``test_cycle_runtime_reconcile_blocks_on_a_real_blocking_outbox_record``
    already uses to exercise one real method without constructing a live
    broker connection.

    Only ``entry()``'s own attribute reads are stubbed. ``_adapters`` and
    ``_scan_manifest`` are overridden at the instance level because what they
    build (broker adapters, a reproducibility hash) is orthogonal to the
    question this file asks -- which value of ``armed`` reaches
    ``run_once`` -- and stubbing them keeps that question uncontaminated by
    unrelated construction failures.
    """
    runtime = _CycleRuntime.__new__(_CycleRuntime)
    runtime.policy = SimpleNamespace(
        mode=mode,
        policy_hash="a" * 64,
        entry=SimpleNamespace(max_pending_entries=3, packet_ttl_seconds=600.0),
        discovery=SimpleNamespace(
            coverage_sla_seconds=600.0, refresh_limit=5, phase2_limit=2
        ),
    )
    runtime.config = SimpleNamespace(state_dir=tmp_path, account_id="DU1234567")
    runtime.gate = object()
    runtime.journal = object()
    runtime.strategy_policy = object()
    runtime.budget = None
    runtime.manager = object()
    runtime.verifier = object()
    runtime.approval_context = object()
    runtime.entry_preflight = lambda **_: None
    runtime.session_lease = lambda: None
    runtime.execution_outbox = object()
    runtime.transmission_budget = object()
    runtime.scanbook_store = object()
    runtime._adapters = lambda broker, *, priority: (None, None, None, None)
    runtime._scan_manifest = lambda scan_config: {
        "catalog_hash": "c" * 64,
        "catalog_version": "test",
        "policy_hash": "a" * 64,
        "calendar_hash": "d" * 64,
        "config_hash": "e" * 64,
        "behavior_hash": "f" * 64,
    }
    return runtime


# ===========================================================================
# Layer 1: cycle_adapter's mode -> armed routing (the D14 target directly)
# ===========================================================================

#: (mode, --arm CLI flag, expect run_once called at all, expect armed=)
#:
#: DRY_RUN and SHADOW never reach run_once regardless of the --arm flag --
#: entry() returns ENTRY_SHADOW_ONLY before touching the runner, the verifier,
#: or the broker. REVIEW_ONLY reaches run_once (it services review work per
#: docs/AUTOTRADER-CYCLE.md), but composes to armed=False unconditionally,
#: because AutoCycleConfig.transmission_enabled requires mode is ARMED. ARMED
#: itself only composes to armed=True when the operator-supplied --arm token
#: was ALSO present -- an ARMED policy started without --arm must still
#: refuse, which is exactly the "policy says ARMED, operator forgot --arm"
#: near-miss a routing bug would produce.
MODE_ARM_MATRIX: tuple[tuple[str, bool, bool, bool | None], ...] = (
    (DRY_RUN, False, False, None),
    (DRY_RUN, True, False, None),
    (SHADOW, False, False, None),
    (SHADOW, True, False, None),
    (REVIEW_ONLY, False, True, False),
    (REVIEW_ONLY, True, True, False),
    (ARMED, False, True, False),
    (ARMED, True, True, True),
)


class TestModeToArmedRouting:
    @pytest.mark.parametrize(
        "mode,arm_flag,expect_called,expect_armed", MODE_ARM_MATRIX
    )
    def test_entry_routes_the_correct_armed_value_to_run_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
        arm_flag: bool,
        expect_called: bool,
        expect_armed: bool | None,
    ) -> None:
        calls: list[dict[str, Any]] = []

        def fake_run_once(*args: Any, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(to_record=lambda: {"outcome": "FAKE_RUN_ONCE"})

        monkeypatch.setattr(cycle_adapter_module, "run_once", fake_run_once)

        runtime = _bare_runtime(tmp_path, mode=mode)
        context = _phase_context(tmp_path, mode=mode, arm=arm_flag)

        result = runtime.entry(context)

        if not expect_called:
            assert calls == [], (
                f"mode={mode} must never reach run_once, but it did: {calls}"
            )
            assert result["outcome"] == "ENTRY_SHADOW_ONLY"
            assert result["transmissions"] == 0
            assert result["new_openings"] == 0
            return

        assert len(calls) == 1, f"mode={mode} arm_flag={arm_flag}: {calls}"
        assert calls[0]["armed"] is expect_armed, (
            f"mode={mode} arm_flag={arm_flag}: expected armed={expect_armed}, "
            f"got {calls[0]['armed']!r}"
        )
        # entry_mode is always FULL on this path -- MANAGE_ONLY has its own
        # phase (management()) and never reaches entry() at all.
        assert calls[0]["entry_mode"].value == "FULL"

    def test_missing_reviewer_composition_blocks_before_run_once_even_when_armed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live verifier/approval_context/manager triple is mandatory for
        FULL entry regardless of mode or --arm -- the guard immediately above
        the run_once call in cycle_adapter.py. This is the other way entry()
        can refuse to call run_once, and it must not be confused with the
        DRY_RUN/SHADOW short circuit above (different outcome code)."""
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            cycle_adapter_module,
            "run_once",
            lambda *a, **k: calls.append(k),
        )

        runtime = _bare_runtime(tmp_path, mode=ARMED)
        runtime.verifier = None
        context = _phase_context(tmp_path, mode=ARMED, arm=True)

        result = runtime.entry(context)

        assert calls == []
        assert result["outcome"] == "ENTRY_BLOCKED"
        assert result["failure_code"] == "FAIL-UNAUTHORIZED-ENTRY"


class TestInvalidModeMandateCombinationsAreUnconstructible:
    """REVIEW_ONLY and ARMED are schema-invalid outside mandate FULL. This is
    enforced at parse/construction time in three independent places
    (``autotrader_policy.parse_autotrader_policy``,
    ``AutoCycleConfig.__post_init__``); this test pins the one the mode-matrix
    fixtures in this file actually build through, so a regression that let a
    MANAGE_ONLY+ARMED policy reach ``entry()`` would fail here first."""

    @pytest.mark.parametrize("mode", [REVIEW_ONLY, ARMED])
    def test_manage_only_mandate_rejects_review_only_and_armed(
        self, tmp_path: Path, mode: str
    ) -> None:
        with pytest.raises(CycleError, match="REVIEW_ONLY and ARMED require mandate FULL"):
            _auto_cycle_config(tmp_path, mode=mode, mandate=MANAGE_ONLY)


class TestModeCannotChangeMidSession:
    """"the mode is switched mid-session (if that's even possible -- check;
    if not, note it isn't and skip)" -- checked, and it is not possible.

    ``run_options_cycle`` (cycle_adapter.py) loads the hash-pinned policy
    exactly once, before ``OptionsCycleWorker`` is constructed, and
    ``OptionsCycleWorker.__init__`` assigns ``self.config`` exactly once
    (``autocycle.py:775`` -- the only ``self.config =`` in the module). The
    ``arm`` flag flows into ``run_forever(arm=...)`` as a plain function
    argument that ``_run_loop`` closes over for the entire
    ``while not self._stop.is_set()`` loop (``autocycle.py:860-894``); every
    ``PhaseContext`` built inside ``run_tick`` (``autocycle.py:945-952``)
    reuses that same ``self.config`` object and that same ``arm`` value.
    A mode or --arm change is therefore only reachable by killing the worker
    process and starting a new one under a newly hash-pinned policy artifact
    -- not a runtime transition, so there is nothing to drive inside one
    ``entry()``/``run_forever`` call. The two checks below pin the structural
    reason why: both carriers of the value are frozen dataclasses.
    """

    def test_auto_cycle_config_mode_is_frozen(self, tmp_path: Path) -> None:
        config = _auto_cycle_config(tmp_path, mode=REVIEW_ONLY)
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.mode = CycleMode.ARMED  # type: ignore[misc]

    def test_phase_context_arm_is_frozen(self, tmp_path: Path) -> None:
        context = _phase_context(tmp_path, mode=REVIEW_ONLY, arm=False)
        with pytest.raises(dataclasses.FrozenInstanceError):
            context.arm = True  # type: ignore[misc]


# ===========================================================================
# Layer 2: the real corridor, armed=False, several adversarial scenarios
# ===========================================================================


class TestNonArmedScenariosNeverTransmit:
    """``armed=False`` is exactly what REVIEW_ONLY composes to under every
    (arm-flag) combination proved above -- DRY_RUN/SHADOW never even reach
    this corridor. These scenarios run the REAL ``run_once`` ->
    ``_authorize_and_transmit_entry`` -> ``authorize_open`` -> ``place_combo``
    chain through the same ``Rig``/``FakeIB`` harness
    ``test_options_integration.py`` trusts, and watch ``FakeIB.placed`` -- the
    one list a transmitted order would land in.
    """

    def test_a_fully_eligible_candidate_transmits_nothing_unarmed(
        self, tmp_path: Path
    ) -> None:
        """Scenario: a candidate that would pass every risk/governor check is
        found on the very first pass. Nothing has even reached the reviewer
        yet -- proves the armed gate holds before review enters the picture."""
        rig = Rig(tmp_path, pass_factors=(D("1"),))
        report = rig.run(armed=False)

        assert rig.ib.placed == []
        assert report.candidate is not None, "the scenario built no candidate at all"
        assert report.entered is False

    def test_a_reviewer_approved_packet_transmits_nothing_unarmed(
        self, tmp_path: Path
    ) -> None:
        """Scenario: the reviewer approves. The approval is real (consumed on
        an armed pass elsewhere in the suite); here the consuming pass is
        unarmed and must still refuse to spend it."""
        rig = Rig(tmp_path, pass_factors=(D("1"), D("1"), D("1")))
        rig.run()
        rig.reviewer.work(rig.now)

        report = rig.run(armed=False)

        assert rig.ib.placed == []
        assert any("armed" in blocker for blocker in report.blockers), report.blockers
        assert rig.consumed() == [], "an unarmed refusal must not spend the approval"
        entry = rig.entry()
        assert entry.state is LogicalEntryState.APPROVED_PENDING_EXECUTION

    def test_a_reviewer_that_never_responds_transmits_nothing_unarmed(
        self, tmp_path: Path
    ) -> None:
        """Scenario: the reviewer never answers at all. Past the review TTL
        the entry is swept to EXPIRED; at no point along the way -- waiting,
        or after expiry -- does an unarmed pass transmit anything."""
        rig = Rig(tmp_path, pass_factors=(D("1"), D("1")))
        rig.run(armed=False)
        entry = rig.entry()
        assert entry.state is LogicalEntryState.AWAITING_REVIEW
        assert rig.ib.placed == []

        report = rig.run(now=rig.now + dt.timedelta(hours=13), armed=False)

        assert rig.ib.placed == []
        swept = rig.lstore.get(entry.logical_entry_id)
        assert swept.state is LogicalEntryState.EXPIRED
        assert any("EXPIRED" in blocker for blocker in report.blockers), report.blockers

    def test_a_stale_reviewer_approval_transmits_nothing_unarmed(
        self, tmp_path: Path
    ) -> None:
        """Scenario: the reviewer DOES answer, but its own stated
        ``expires_at`` has already passed by the time an (unarmed) pass tries
        to consume it -- a genuinely expired approval is presented, not just
        an absent one. Proves the staleness gate and the armed gate are two
        independent refusals, either of which alone would stop this."""
        rig = Rig(tmp_path, pass_factors=(D("1"), D("1"), D("1")))
        rig.reviewer.lifetime = dt.timedelta(seconds=1)
        rig.run()
        rig.reviewer.work(rig.now)

        # SECONDS_BETWEEN_PASSES is 60s of logical time; the reviewer's own
        # approval said it was only good for 1s.
        report = rig.run(armed=False)

        assert rig.ib.placed == []
        assert report.blockers, "the pass must have refused for a stated reason"

    def test_max_pending_entries_reached_while_unarmed_still_transmits_nothing(
        self, tmp_path: Path
    ) -> None:
        """A second adversarial angle on the same claim: even when the pass
        is doing real, visible work (refusing a claim at the pending cap)
        while unarmed, that work never touches the broker."""
        import dataclasses as dc

        from engine.options.policy import RiskPolicy

        policy = RiskPolicy()
        policy = dc.replace(
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
        rig.run(armed=False)
        rig.run(armed=False)
        rig.run(armed=False)
        report = rig.run(armed=False)  # default cap: 3 pending

        assert "OPTIONS_LOGICAL_PENDING_CAP" in report.refusal_codes
        assert rig.ib.placed == []


class TestArmedFullArmCanTransmit:
    """The control. If this fails, the "nothing transmits" suite above is not
    proving a mode gate -- it is proving the whole corridor is broken, which
    is a very different (and much worse) finding. ``armed=True`` here is
    exactly what Layer 1 proved ``cycle_adapter._CycleRuntime.entry()``
    passes to ``run_once`` for, and only for, ``mode=ARMED, mandate=FULL,
    --arm``."""

    def test_the_one_authorized_combination_reaches_place_order(
        self, tmp_path: Path
    ) -> None:
        rig = Rig(tmp_path, pass_factors=(D("1"), D("1"), D("1")))
        rig.run()
        rig.reviewer.work(rig.now)

        report = rig.run(armed=True)

        assert len(rig.ib.placed) == 1, report.describe()
        assert report.entered is True
