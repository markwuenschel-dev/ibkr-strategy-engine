"""The session lease fence: a pass that lost its session refuses, rather than being timed out.

The failure this prevents is narrow and was real. A scheduler-driven pass holds
a session mandate for the window it was started in. Nothing stopped a pass that
*lost* that mandate mid-flight -- the scheduler stopped, the day rolled, the
supervisor took the session back -- from carrying on: proposing a trade to the
reviewer, minting an authorization, and putting a live order in the market on
behalf of a session that no longer existed. The only thing bounding that was the
supervisor's drain timeout, and a timeout bounds *how long* a pass may keep
going without ever refusing *what it does*. Those are different properties, and
only the second one keeps an order out of the book.

So there are two fences, and each closes a window the other cannot:

* **Before authorization**, in ``run_once``, after every risk and governor check
  and before ``packet_for``. A lost lease here means nothing is filed with the
  reviewer and no single-use approval is spent.
* **At the door**, inside ``place_combo``, immediately before the order is armed
  and handed to ``placeOrder``. This is the only thing that covers the interval
  the first fence cannot see: proposing, waiting for an answer, minting the
  token and writing the submission record all take time, and the session can go
  away during any of it.

**Exits are deliberately not fenced, and that is asserted here rather than
assumed.** ``place_combo`` guards its lease check on ``StrategyAction.OPEN``, so
a close transmits under a lost lease even when a caller passes one, and
``run_once`` does not hand the lease to ``_manage_one`` at all. This follows the
asymmetry the whole codebase already runs on -- ``authorize_close`` skips the
governor and the daily cap, management runs under every reconciliation outcome
-- for one reason: a stale session is a reason to stop *taking on* risk and
never a reason to trap a position. A fence that could refuse an exit would turn
"the scheduler stopped" into "this spread cannot be closed", which is strictly
worse than the thing being prevented.

The broker and market-data fakes are imported from ``test_options_runner`` and
the transmit builders from ``test_options_transmit`` rather than copied, for the
reason ``test_options_execution_proof`` gives: a second copy drifts, and the
moment it stops reaching the transmission decision every "nothing was sent"
assertion here starts passing for the wrong reason.
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib
import inspect
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

import reviewer
from engine.errors import RefusedError
from engine.options import runner as runner_module
from engine.options import transmit as transmit_module
from engine.options.domain import StrategyAction
from engine.options.policy import RiskPolicy
from engine.options.positions import PositionStore
from engine.options.runner import EntryMode, RunReport, run_once
from engine.options.selection import Bias
from engine.options.transmit import SESSION_LEASE_LOST, authorize_close, place_combo
from engine.safety import SafetyGate

from test_options_runner import (  # noqa: E402 - sibling test module, see docstring
    NOW,
    TODAY,
    FakeBroker,
    FakeIB,
    FakeMarketDataPort,
    FakePortfolioPort,
    event_names,
    gate_for,
    seed_open_position,
    store_for,
)
from test_options_transmit import (  # noqa: E402 - sibling test module, see docstring
    RecordingIB,
    authorized,
    closing_intent_for,
    spread,
)
from test_options_recovery import (  # noqa: E402 - shared stateful broker fake
    SCRIPT_WORKING_FOREVER,
    FakeBroker as RecoveryFakeBroker,
    FakeMarketDataPort as RecoveryMarketDataPort,
    FakePortfolioPort as RecoveryPortfolioPort,
    ScriptedIB,
)

RUNNER_SOURCE = Path(runner_module.__file__).resolve()
REPRICE_SOURCE = Path(importlib.import_module("engine.options.reprice").__file__).resolve()
TRANSMIT_SOURCE = Path(transmit_module.__file__).resolve()
PACKAGE_DIR = TRANSMIT_SOURCE.parent

LOST = "the 2026-08-13 session ended at 15:55 ET and this pass outlived it"


# ---------------------------------------------------------------------------
# Leases
# ---------------------------------------------------------------------------


class CountingLease:
    """A lease that answers from a script and records how often it was asked.

    The count is the load-bearing part. "Both fences exist" is not shown by two
    refusals -- one fence firing twice would look identical -- it is shown by a
    lease that says *held* the first time and *lost* the second, and by the pass
    then stopping at the door with the order never sent.
    """

    def __init__(self, *answers: str | None) -> None:
        self.answers = list(answers)
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        if self.calls <= len(self.answers):
            return self.answers[self.calls - 1]
        return self.answers[-1] if self.answers else None


def raising_lease() -> str | None:
    """A lease that cannot answer at all. Must be read as lost, not as held."""
    raise RuntimeError("the session registry is unreachable")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


class LeasePass:
    """One armed ``run_once`` with a real reviewed verifier, plus its ledger.

    Deliberately not ``test_options_runner.run_pass``: that helper has no
    ``session_lease`` argument, and adding one would mean editing a file this
    lane does not own. Everything else is the same wiring, including the real
    :class:`~reviewer.ReviewedGate` over a temp collab -- so "nothing was filed"
    below is a statement about a real reviewer queue and a real ledger on disk,
    not about a stub that was never called.
    """

    def __init__(
        self, tmp_path: Path, *, positions: tuple[tuple[str, int, float], ...] = ()
    ) -> None:
        self.ib = FakeIB()
        self.broker = FakeBroker(ib=self.ib, positions=positions)
        self.gate: SafetyGate = gate_for(tmp_path)
        self.store: PositionStore = store_for(tmp_path)
        self.verifier = reviewer.approving_gate(tmp_path / "verifier")
        self.context = reviewer.approval_context()

    @property
    def ledger(self) -> Path:
        return Path(self.verifier.ledger)

    @property
    def filed_requests(self) -> list[str]:
        """Every verification request the gate has written to its ledger.

        ``.id`` files only: ``CollabVerifierGate`` writes one ``<digest>.id``
        holding the handoff id and one ``<digest>.json`` beside it, and counting
        both would say two where one request was filed.
        """
        directory = self.ledger / "requests"
        return sorted(p.name for p in directory.glob("*.id")) if directory.exists() else []

    @property
    def consumed_approvals(self) -> list[str]:
        """Every approval marked spent. A refusal must not burn one."""
        directory = self.ledger / "consumed"
        return (
            sorted(p.name for p in directory.glob("*.used")) if directory.exists() else []
        )

    def run(self, *, session_lease: Any = None, armed: bool = True) -> RunReport:
        return run_once(
            self.broker,
            gate=self.gate,
            journal=self.gate.journal,
            store=self.store,
            policy=RiskPolicy(),
            armed=armed,
            entry_mode=EntryMode.FULL,
            symbol="SPY",
            bias=Bias.BULLISH,
            market_data=FakeMarketDataPort(),
            portfolio=FakePortfolioPort(),
            now=NOW,
            today=TODAY,
            account="DU1234567",
            verifier=self.verifier,
            approval_context=self.context,
            session_lease=session_lease,
        )


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def function_in(path: Path, name: str) -> ast.FunctionDef:
    for node in ast.walk(parse(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined in {path.name}")


def place_combo_calls(function: ast.FunctionDef) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "place_combo"
    ]


# ===========================================================================
# No lease means nothing changed
# ===========================================================================


class TestNoLeaseIsNoFence:
    """Every existing caller passes no lease, and must behave exactly as before.

    The failure prevented is the one a fail-closed default would cause: a fence
    whose absence refuses would silently stop every caller in the tree -- the
    CLI, the execution proof, the reprice ladder -- from ever transmitting
    again, which is a far larger outage than the one being fixed.
    """

    def test_both_lease_parameters_default_to_no_fence(self) -> None:
        runner_default = inspect.signature(run_once).parameters["session_lease"].default
        door_default = inspect.signature(place_combo).parameters["session_lease"].default
        assert runner_default is None, (
            f"run_once's session_lease defaults to {runner_default!r}, so an "
            "existing caller would get a fence it never asked for"
        )
        assert door_default is None, (
            f"place_combo's session_lease defaults to {door_default!r}, so an "
            "existing caller would get a fence it never asked for"
        )

    def test_an_armed_pass_with_no_lease_still_transmits_its_entry(
        self, tmp_path: Path
    ) -> None:
        session = LeasePass(tmp_path)
        report = session.run()
        assert report.entered, f"the unfenced pass did not enter: {report.blockers}"
        assert len(session.ib.placed) == 1, session.ib.placed
        assert SESSION_LEASE_LOST not in report.refusal_codes, report.refusal_codes

    def test_place_combo_with_no_lease_still_transmits(self, tmp_path: Path) -> None:
        intent = spread()
        ib = RecordingIB()
        result = place_combo(
            ib, intent, authorization=authorized(tmp_path, intent), account="DU1234567"
        )
        assert result.transmitted, result.describe()
        assert len(ib.placed) == 1, ib.placed


# ===========================================================================
# Fence one: before the proposal, before the token
# ===========================================================================


class TestALostLeaseRefusesBeforeAuthorization:
    """A pass whose session went away files nothing and mints nothing.

    Refusing *after* the packet was filed would be nearly as bad as not refusing
    at all: an unanswerable request sits in the reviewer's queue attributed to a
    session that no longer exists, and if the answer arrives the single-use
    approval it carries is spent on an order that can never be sent.
    """

    def test_a_lost_lease_refuses_the_entry_and_transmits_nothing(
        self, tmp_path: Path
    ) -> None:
        session = LeasePass(tmp_path)
        report = session.run(session_lease=lambda: LOST)
        assert not report.entered, "a pass without its session opened new risk"
        assert session.ib.placed == [], session.ib.placed

    def test_the_reason_code_and_the_lease_detail_reach_the_report(
        self, tmp_path: Path
    ) -> None:
        """A distinct code, so this is not confused with a risk or reviewer refusal."""
        session = LeasePass(tmp_path)
        report = session.run(session_lease=lambda: LOST)
        assert SESSION_LEASE_LOST in report.refusal_codes, report.refusal_codes
        assert SESSION_LEASE_LOST == "OPTIONS_SESSION_LEASE_LOST", SESSION_LEASE_LOST
        assert any(LOST in blocker for blocker in report.blockers), report.blockers
        assert SESSION_LEASE_LOST in report.describe(), report.describe()

    def test_a_lost_lease_files_no_request_and_consumes_no_approval(
        self, tmp_path: Path
    ) -> None:
        session = LeasePass(tmp_path)
        session.run(session_lease=lambda: LOST)
        assert session.filed_requests == [], session.filed_requests
        assert session.consumed_approvals == [], session.consumed_approvals

    def test_the_control_files_a_request_when_the_lease_is_held(
        self, tmp_path: Path
    ) -> None:
        """Without this, the assertion above would pass on a pass that died earlier."""
        session = LeasePass(tmp_path)
        report = session.run(session_lease=lambda: None)
        assert report.entered, f"the held-lease pass did not enter: {report.blockers}"
        assert len(session.filed_requests) == 1, session.filed_requests
        assert len(session.consumed_approvals) == 1, session.consumed_approvals

    def test_a_lost_lease_writes_no_position_record(self, tmp_path: Path) -> None:
        """No submission record either -- there is nothing for a reconciler to resolve."""
        session = LeasePass(tmp_path)
        session.run(session_lease=lambda: LOST)
        assert event_names(session.store) == [], event_names(session.store)

    def test_a_lease_that_raises_is_read_as_lost_rather_than_as_held(
        self, tmp_path: Path
    ) -> None:
        """Fail-closed. A lease that cannot answer has not said yes."""
        session = LeasePass(tmp_path)
        report = session.run(session_lease=raising_lease)
        assert SESSION_LEASE_LOST in report.refusal_codes, report.refusal_codes
        assert session.ib.placed == [], session.ib.placed
        assert any("RuntimeError" in blocker for blocker in report.blockers), (
            f"the blocker does not name what went wrong: {report.blockers}"
        )

    def test_an_empty_string_is_held_not_lost(self, tmp_path: Path) -> None:
        """The contract is "a refusal *string* or None", and "" is not a refusal."""
        session = LeasePass(tmp_path)
        report = session.run(session_lease=lambda: "")
        assert report.entered, f"an empty answer refused the entry: {report.blockers}"
        assert len(session.ib.placed) == 1, session.ib.placed

    def test_a_lost_lease_refuses_the_entry_and_not_the_pass(
        self, tmp_path: Path
    ) -> None:
        """The fence records a blocker; it never raises. Exits ran above it.

        A fence that raised would take reconciliation and management down with
        it -- which is the trapped-position failure the whole design ranks
        worst, arriving through the door meant to prevent a lesser one.
        """
        session = LeasePass(tmp_path, positions=(("SPY", 1, 100.0),))
        seed_open_position(session.store, dte=40)
        report = session.run(session_lease=lambda: LOST)
        assert isinstance(report, RunReport), report
        assert SESSION_LEASE_LOST in report.refusal_codes, (
            f"the pass was refused by something else first: {report.refusal_codes}"
        )
        assert report.reconciliation is not None, "reconciliation did not run"
        assert report.reconciliation.agrees, report.reconciliation.describe()
        assert report.decisions, "management did not evaluate the open position"


# ===========================================================================
# Fence two: at the door, as late as anything can be
# ===========================================================================


class TestALostLeaseRefusesAtTheDoor:
    """The interval between "authorized" and "transmitted" is its own window.

    Proposing, waiting for the reviewer, minting the token and writing the
    submission record all take real time. The first fence proves the session was
    held before all of that; only this one proves it is still held now.
    """

    def test_a_lease_lost_after_authorization_sends_nothing(
        self, tmp_path: Path
    ) -> None:
        intent = spread()
        ib = RecordingIB()
        with pytest.raises(RefusedError) as caught:
            place_combo(
                ib,
                intent,
                authorization=authorized(tmp_path, intent),
                account="DU1234567",
                session_lease=lambda: LOST,
            )
        assert ib.placed == [], ib.placed
        assert SESSION_LEASE_LOST in caught.value.message, caught.value.message
        assert LOST in caught.value.message, caught.value.message

    def test_a_raising_lease_refuses_at_the_door_too(self, tmp_path: Path) -> None:
        intent = spread()
        ib = RecordingIB()
        with pytest.raises(RefusedError, match=SESSION_LEASE_LOST):
            place_combo(
                ib,
                intent,
                authorization=authorized(tmp_path, intent),
                account="DU1234567",
                session_lease=raising_lease,
            )
        assert ib.placed == [], ib.placed

    def test_a_held_lease_at_the_door_transmits(self, tmp_path: Path) -> None:
        intent = spread()
        ib = RecordingIB()
        result = place_combo(
            ib,
            intent,
            authorization=authorized(tmp_path, intent),
            account="DU1234567",
            session_lease=lambda: None,
        )
        assert result.transmitted, result.describe()
        assert len(ib.placed) == 1, ib.placed

    def test_the_runner_asks_twice_and_stops_at_the_door_on_the_second_answer(
        self, tmp_path: Path
    ) -> None:
        """The proof that there are really two fences and not one asked twice.

        Held before authorization, lost by the time the order reaches the door:
        the pass gets all the way through the reviewer, mints a token, writes
        its submission record -- and still sends nothing.
        """
        session = LeasePass(tmp_path)
        lease = CountingLease(None, LOST)
        report = session.run(session_lease=lease)

        assert lease.calls == 2, (
            f"the lease was consulted {lease.calls} time(s); the design is one "
            "check before authorization and one at the door"
        )
        assert session.ib.placed == [], session.ib.placed
        assert not report.entered, "an order was recorded as entered but never sent"
        assert any(SESSION_LEASE_LOST in error for error in report.errors), report.errors
        # Recorded before the send and failed after it, exactly as any other
        # refused transmission is -- there is no half-written position left.
        assert event_names(session.store) == ["OPEN_SUBMITTED", "OPEN_FAILED"], (
            event_names(session.store)
        )


# ===========================================================================
# Exits are never fenced
# ===========================================================================


class TestExitsAreNeverFenced:
    """A stale session must never be the reason a position cannot be closed.

    This is the deliberate decision, pinned so it cannot be quietly reversed:
    the fence applies to opens and to nothing else. Reducing risk is exactly
    what a pass whose session has ended should still be able to do.
    """

    def test_a_closing_order_transmits_even_when_the_lease_is_lost(
        self, tmp_path: Path
    ) -> None:
        """Structural, not conventional: the caller *did* pass a refusing lease."""
        closing = closing_intent_for(spread())
        ib = RecordingIB()
        result = place_combo(
            ib,
            closing,
            authorization=authorize_close(
                closing, gate=gate_for(tmp_path), armed=True, now=NOW
            ),
            account="DU1234567",
            session_lease=lambda: LOST,
        )
        assert result.transmitted, result.describe()
        assert len(ib.placed) == 1, (
            "a lost session lease trapped an exit -- the one outcome this design "
            "ranks worse than the failure it prevents"
        )
        assert closing.strategy_action is StrategyAction.CLOSE, closing.strategy_action

    def test_a_closing_order_transmits_even_when_the_lease_raises(
        self, tmp_path: Path
    ) -> None:
        closing = closing_intent_for(spread())
        ib = RecordingIB()
        result = place_combo(
            ib,
            closing,
            authorization=authorize_close(
                closing, gate=gate_for(tmp_path), armed=True, now=NOW
            ),
            account="DU1234567",
            session_lease=raising_lease,
        )
        assert result.transmitted, result.describe()
        assert len(ib.placed) == 1, ib.placed

    def test_the_runner_manages_and_exits_a_position_under_a_lost_lease(
        self, tmp_path: Path
    ) -> None:
        """End to end: the entry is refused, the exit still reaches the broker.

        The broker is given the matching holding on purpose, so the pass
        reconciles and the *only* thing refusing the entry is the lease. With a
        disagreeing book the reconciler would refuse first and this would pass
        without the fence existing at all.
        """
        session = LeasePass(tmp_path, positions=(("SPY", 1, 100.0),))
        seed_open_position(session.store, dte=18)
        report = session.run(session_lease=lambda: LOST)

        assert SESSION_LEASE_LOST in report.refusal_codes, report.refusal_codes
        assert len(report.transmissions) == 1, [
            t.describe() for t in report.transmissions
        ]
        sent = report.transmissions[0]
        assert sent.action is StrategyAction.CLOSE, sent.action
        assert len(session.ib.placed) == 1, session.ib.placed

    def test_the_runner_does_not_hand_the_lease_to_the_exit_path(self) -> None:
        """Belt to the door's braces, asserted over the AST.

        ``place_combo`` already refuses to fence a close. This says the runner
        does not even offer it one, so the exemption does not rest on a single
        ``is OPEN`` comparison continuing to be written correctly.
        """
        exit_calls = place_combo_calls(function_in(RUNNER_SOURCE, "_manage_one"))
        entry_calls = place_combo_calls(
            function_in(RUNNER_SOURCE, "_authorize_and_transmit_entry")
        )
        assert len(exit_calls) == 1, f"expected one exit send, found {len(exit_calls)}"
        assert len(entry_calls) == 1, f"expected one entry send, found {len(entry_calls)}"

        exit_keywords = {kw.arg for kw in exit_calls[0].keywords}
        entry_keywords = {kw.arg for kw in entry_calls[0].keywords}
        assert "session_lease" not in exit_keywords, (
            "_manage_one passes a session lease to place_combo; an exit must not "
            "be fenced by a session that has gone away"
        )
        assert "session_lease" in entry_keywords, (
            "the shared entry corridor does not pass its session lease to the "
            "entry send, so the door fence is unreachable from the strategy path"
        )

    def test_the_door_fence_is_guarded_on_the_opening_action(self) -> None:
        """The exemption is in the source, not only in a test that happens to pass."""
        door = function_in(TRANSMIT_SOURCE, "place_combo")
        guards = [
            node
            for node in ast.walk(door)
            if isinstance(node, ast.If)
            for inner in ast.walk(node)
            if isinstance(inner, ast.Name) and inner.id == "_lease_refusal"
        ]
        assert len(guards) == 1, f"expected one lease guard, found {len(guards)}"
        tested = ast.dump(guards[0].test)
        assert "OPEN" in tested, (
            "the lease guard in place_combo is not conditioned on the strategy "
            f"action, so it can trap a close: {tested}"
        )


# ===========================================================================
# Every opening ladder rung reaches the door
# ===========================================================================


class TestTheRepriceLadderFence:
    def test_the_reprice_chokepoint_receives_the_existing_lease(self) -> None:
        """Mutation guard: every replacement must pass the lease explicitly."""
        work = function_in(REPRICE_SOURCE, "work_order")
        parameters = [arg.arg for arg in work.args.kwonlyargs]
        assert "session_lease" in parameters, parameters

        sends = [
            node
            for node in ast.walk(work)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "place_combo"
        ]
        assert len(sends) == 1, f"expected one replacement chokepoint, found {sends}"
        lease_keywords = {
            keyword.arg: keyword.value for keyword in sends[0].keywords if keyword.arg
        }
        assert isinstance(lease_keywords.get("session_lease"), ast.Name)
        assert lease_keywords["session_lease"].id == "session_lease"

    def test_lease_loss_after_rung_one_blocks_every_later_open_send(
        self, tmp_path: Path
    ) -> None:
        """A replacement is a fresh OPEN transmission, not a lease exemption.

        The initial order and the first replacement are allowed. The lease is
        then revoked before the second replacement reaches ``place_combo``.
        The second order is cancelled as part of the ordinary replace protocol,
        but no third broker ``placeOrder`` call may occur.
        """
        ib = ScriptedIB(scripts=(SCRIPT_WORKING_FOREVER,))
        broker = RecoveryFakeBroker(ib=ib)
        gate = gate_for(tmp_path)
        store = store_for(tmp_path)
        verifier = reviewer.approving_gate(tmp_path / "verifier")
        lease = CountingLease(None, None, None, LOST)

        report = run_once(
            broker,
            gate=gate,
            journal=gate.journal,
            store=store,
            policy=RiskPolicy(),
            armed=True,
            entry_mode=EntryMode.FULL,
            symbol="SPY",
            bias=Bias.BULLISH,
            market_data=RecoveryMarketDataPort(),
            portfolio=RecoveryPortfolioPort(),
            now=NOW,
            today=TODAY,
            account="DU1234567",
            verifier=verifier,
            approval_context=reviewer.approval_context(),
            session_lease=lease,
        )

        assert lease.calls == 4, lease.calls
        assert len(ib.placed) == 2, "initial send plus rung 1 only"
        assert len(ib.cancelled) == 2, "rung 1 and the refused rung were cancelled"
        assert report.reprice is not None, report.describe()
        assert SESSION_LEASE_LOST in report.reprice.detail, report.reprice.describe()
        assert report.reprice.attempts == 1, report.reprice.describe()


# ===========================================================================
# The door is still the door
# ===========================================================================


class TestTheChokepointIsUnchanged:
    """Adding a fence must not add a second way out of the process.

    ``test_options_transmit`` asserts the same uniqueness property; it is
    re-asserted here because this lane is the one that edited the file, and a
    property whose test lives only in a file the change did not touch is a
    property nobody re-ran on purpose.
    """

    def test_the_package_still_has_exactly_one_placeorder(self) -> None:
        sites: list[str] = []
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            for node in ast.walk(parse(path)):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "placeOrder"
                ):
                    sites.append(f"{path.name}:{node.lineno}")
        assert len(sites) == 1, f"expected exactly one transmitting call, found {sites}"
        assert sites[0].startswith("transmit.py:"), sites

    def test_the_fence_sits_between_the_last_gate_and_the_send(self) -> None:
        """Placement is the property: as late as anything can be, before arming.

        A lease checked earlier in ``place_combo`` -- before the digest and spec
        comparisons, say -- would reopen exactly the window it exists to close.
        """
        source = TRANSMIT_SOURCE.read_text(encoding="utf-8").splitlines()
        door = function_in(TRANSMIT_SOURCE, "place_combo")

        def line_of(needle: str) -> int:
            for offset in range(door.lineno - 1, len(source)):
                if needle in source[offset]:
                    return offset + 1
            raise AssertionError(f"{needle!r} is not in place_combo")

        digest_check = line_of("authorization.digest != sending")
        fence = line_of("lost = _lease_refusal(session_lease)")
        arming = line_of("order.transmit = True")
        send = line_of("ib.placeOrder(")

        assert digest_check < fence, (
            f"the lease fence at {fence} runs before the digest check at "
            f"{digest_check}; it is meant to be the last word, not the first"
        )
        assert fence < arming < send, (
            f"the lease fence at {fence} does not sit immediately before arming "
            f"({arming}) and transmission ({send})"
        )

    def test_authorization_still_has_no_default(self) -> None:
        """The new keyword must not have shifted the one that carries the proof."""
        door = function_in(TRANSMIT_SOURCE, "place_combo")
        names = [arg.arg for arg in door.args.kwonlyargs]
        assert "authorization" in names, names
        assert door.args.kw_defaults[names.index("authorization")] is None, (
            "authorization acquired a default value"
        )
        assert "session_lease" in names, names

    def test_the_fence_imports_nothing_from_the_operational_tier(self) -> None:
        """The port is a callable precisely so this stays true. See
        ``test_architecture_boundaries``; asserted here on the two edited files
        so the lane that added the fence owns the boundary it could have broken."""
        forbidden = ("engine.paperday", "engine.scheduler", "engine.runtime", "subprocess")
        offences: list[str] = []
        for path in (RUNNER_SOURCE, TRANSMIT_SOURCE):
            for node in ast.walk(parse(path)):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    if any(name == bad or name.startswith(f"{bad}.") for bad in forbidden):
                        offences.append(f"{path.name}:{node.lineno} {name}")
        assert offences == [], offences


# ===========================================================================
# The port itself
# ===========================================================================


class TestTheLeasePortShape:
    """The contract a caller in the operational tier has to satisfy.

    Stated as a test because the port is the whole design: an injected
    zero-argument callable returning a refusal string or ``None``. Anything that
    needs an argument, or that answers with a bool, is a different port and
    would have to change both call sites.
    """

    def test_the_lease_is_called_with_no_arguments(self, tmp_path: Path) -> None:
        seen: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        def lease(*args: Any, **kwargs: Any) -> str | None:
            seen.append((args, kwargs))
            return None

        session = LeasePass(tmp_path)
        session.run(session_lease=lease)
        assert seen, "the lease was never consulted"
        assert all(call == ((), {}) for call in seen), seen

    def test_a_refusal_string_is_what_refuses(self, tmp_path: Path) -> None:
        """Not a bool: the string is the operator-facing reason, and is carried."""
        detail = f"lease {uuid4()} expired at {dt.datetime(2026, 8, 13, 20, 0)}"
        session = LeasePass(tmp_path)
        report = session.run(session_lease=lambda: detail)
        assert any(detail in blocker for blocker in report.blockers), report.blockers
