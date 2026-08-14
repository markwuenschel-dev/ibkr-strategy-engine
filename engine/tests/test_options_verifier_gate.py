"""Proof that opening risk cannot be authorized without an independent review.

Every test here drives the **real** exchange. There is no mock of the gate and
no hand-built approval object: the builder proposes through
:class:`engine.options.approval.CollabVerifierGate`, a reviewer seat claims and
answers through :class:`collabkit.store.HandoffStore`, and the answer is read
back off disk by the shipped parser. A negative case is a real reviewer writing
a real artifact that the gate must reject -- not an assertion about a stub.

The file is organised as the brief asked for it:

* :class:`TestTheWholeLifecycle` -- one synthetic candidate all the way through,
  unarmed, with the intermediate ``AWAITING_VERIFICATION`` state observed rather
  than skipped over.
* :class:`TestEveryBlockingCase` -- the negatives, each of which must refuse.
* :class:`TestTheControl` -- close, cancel, reconcile and inspect with **no**
  approval anywhere. If these ever fail, the gate has started trapping positions,
  which is the failure this design ranks as worse than the one it prevents.
* :class:`TestTheGuardsAreLoadBearing` -- mutation checks. Each one deletes a
  guard's *input* and asserts the refusal that follows, so a test cannot pass
  because the code happens to refuse for some unrelated reason.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from engine._collabkit import load
from engine.errors import RefusedError
from engine.options.approval import (
    MAXIMUM_APPROVAL_LIFETIME,
    ApprovalContext,
    ApprovalDecision,
    ApprovalDefect,
    AuthorizedOrderSpec,
    AwaitingVerification,
    CollabVerifierGate,
    VerificationState,
    commit_sha_at,
    packet_for,
    render_response,
    verification_block,
)
from engine.options.domain import StrategyAction
from engine.options.execution import COMBO_ORDER_TYPE, COMBO_TIME_IN_FORCE
from engine.options.transmit import (
    authorize_cancel,
    authorize_close,
    authorize_open,
    structure_digest,
)

from reviewer import ScriptedReviewer, approving_gate, collab_at, packet
from test_options_transmit import (
    NOW,
    approving_governor,
    approving_risk,
    gate_for,
    spread,
)

D = Decimal


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def context_at(account: str = "DU1234567", port: int = 7497) -> ApprovalContext:
    """A context bound to the real commit and a fixed config fingerprint.

    The commit sha is read from the checkout rather than faked, because
    ``ApprovalContext`` refuses anything that is not 40 hex -- and a test that
    stubbed it would stop proving that the reader works in a git worktree, which
    is the only environment this project ever runs in.
    """
    from engine.config import EngineConfig
    from engine.options.policy import RiskPolicy

    return ApprovalContext.for_run(
        config=EngineConfig(account_id=account, port=port),
        policy=RiskPolicy(),
    )


def open_it(
    intent: Any,
    *,
    verifier: Any,
    context: ApprovalContext,
    tmp_path: Path,
    armed: bool = True,
    now: dt.datetime = NOW,
    risk: Any = None,
    governor: Any = None,
) -> Any:
    """One call to ``authorize_open``, packet and all, exactly as the runner makes it."""
    risk = risk if risk is not None else approving_risk(intent.strategy_id)
    governor = governor if governor is not None else approving_governor(intent)
    return authorize_open(
        intent,
        gate=gate_for(tmp_path),
        risk=risk,
        governor=governor,
        armed=armed,
        now=now,
        verifier=verifier,
        packet=packet(intent, risk=risk, governor=governor, context=context, now=now),
    )


def paused(tmp_path: Path, **kwargs: Any) -> tuple[CollabVerifierGate, ScriptedReviewer]:
    """A real gate whose reviewer answers only when the test says so.

    The approving fixture answers every request the moment it is filed, which is
    right for the happy path and wrong for every invalidation test: a changed
    order files a *new* request, and an auto-answering reviewer would approve it
    immediately, hiding the fact that the old approval no longer covers it. With
    this, the reviewer is stepped by hand, so "the previous approval does not
    carry over" is observable as the new order being left unreviewed.
    """
    root = collab_at(tmp_path)
    return (
        CollabVerifierGate(root=root, ledger=tmp_path / "ledger"),
        ScriptedReviewer(root=root, **kwargs),
    )


def store_at(root: Path) -> Any:
    paths = load("paths", "CollabPaths").at(root)
    return load("store", "HandoffStore")(paths)


# ===========================================================================
# The whole lifecycle, unarmed
# ===========================================================================


class TestTheWholeLifecycle:
    """Proposal -> pending handoff -> claim -> answer -> receipt -> consumption.

    Driven with a bare :class:`CollabVerifierGate` and a reviewer stepped by
    hand, so each stage is observed as a separate fact rather than collapsed
    into one call that either works or does not.
    """

    def test_the_unarmed_path_runs_the_whole_exchange_and_stops_at_arming(
        self, tmp_path: Path
    ) -> None:
        root = collab_at(tmp_path)
        gate = CollabVerifierGate(root=root, ledger=tmp_path / "ledger")
        reviewer = ScriptedReviewer(root=root)
        context = context_at()
        intent = spread()

        # -- 1 & 2 & 3. propose: the request is on disk, in pending/ ------
        with pytest.raises(AwaitingVerification) as awaiting:
            open_it(intent, verifier=gate, context=context, tmp_path=tmp_path, armed=False)
        request_id = awaiting.value.request_id
        assert request_id, "the gate did not file a request"
        assert awaiting.value.state is VerificationState.AWAITING_VERIFICATION

        request = store_at(root).find(request_id)
        assert request.status == "pending"
        assert request.to == "reviewer"
        assert "verification" in request.tags

        # -- the packet really carries what the reviewer needs ------------
        assert str(intent.strategy_id) in request.body
        assert commit_sha_at() in request.body
        assert str(intent.legs[0].con_id) in request.body
        assert f"`{COMBO_ORDER_TYPE}` / `{COMBO_TIME_IN_FORCE}`" in request.body

        # -- 4 & 5. waiting is a state, and it stalls nothing -------------
        # A *different* candidate authorizes while the first one waits: the
        # gate is per-order, so an unanswered request cannot dam the queue.
        other = spread(credit="1.40")
        other_gate = approving_gate(tmp_path / "other")
        assert open_it(
            other,
            verifier=other_gate,
            context=context,
            tmp_path=tmp_path / "other",
        ).action is StrategyAction.OPEN

        # -- 6. the reviewer claims and answers ---------------------------
        replies = reviewer.work(NOW)
        assert len(replies) == 1
        answered = store_at(root).find(request_id)
        assert answered.status == "done"
        assert answered.claimed_by == "reviewer"
        assert replies[0] in (answered.note or "")

        reply = store_at(root).find(replies[0])
        block = verification_block(reply.body)
        assert block is not None
        assert block["verification_decision"] == "APPROVED"
        assert block["verification_verifier"] == "grok"

        # -- 7. the builder receives it; the digest validates --------------
        # Still unarmed: the ONLY refusal left is the arm gate, which is what
        # proves every verification check passed.
        with pytest.raises(RefusedError) as unarmed:
            open_it(intent, verifier=gate, context=context, tmp_path=tmp_path, armed=False)
        assert "not armed" in unarmed.value.message

        # An unarmed pass must not spend the approval. Burning one on a dry run
        # would disarm the real run that follows it.
        consumed = list((tmp_path / "ledger" / "consumed").glob("*.used"))
        assert consumed == []

        # -- and armed, it authorizes and consumes ------------------------
        authorization = open_it(
            intent, verifier=gate, context=context, tmp_path=tmp_path, armed=True
        )
        assert authorization.approval is not None
        assert authorization.approval.response_id == replies[0]
        assert authorization.spec is not None
        assert authorization.approval.spec_digest == authorization.spec.digest
        assert len(list((tmp_path / "ledger" / "consumed").glob("*.used"))) == 1

    def test_a_second_pass_reuses_the_outstanding_request(self, tmp_path: Path) -> None:
        """Asking again must not file a second handoff for the same order.

        A gate that re-proposed every pass would bury the reviewer under
        duplicates of one question and make the queue depth meaningless.
        """
        root = collab_at(tmp_path)
        gate = CollabVerifierGate(root=root, ledger=tmp_path / "ledger")
        context = context_at()
        intent = spread()

        ids = set()
        for _ in range(3):
            with pytest.raises(AwaitingVerification) as awaiting:
                open_it(intent, verifier=gate, context=context, tmp_path=tmp_path)
            ids.add(awaiting.value.request_id)
        assert len(ids) == 1
        assert len(store_at(root).list(("pending",), to="reviewer")) == 1


# ===========================================================================
# Everything that must block
# ===========================================================================


class TestEveryBlockingCase:
    def test_a_matching_approved_authorizes(self, tmp_path: Path) -> None:
        """The positive control. Without it every refusal below proves nothing:
        a gate that refused unconditionally would pass all of them."""
        intent = spread()
        authorization = open_it(
            intent,
            verifier=approving_gate(tmp_path),
            context=context_at(),
            tmp_path=tmp_path,
        )
        assert authorization.action is StrategyAction.OPEN
        assert authorization.approval.decision is ApprovalDecision.APPROVED

    def test_refused_blocks(self, tmp_path: Path) -> None:
        gate = approving_gate(tmp_path)
        gate.reviewer.decision = ApprovalDecision.REFUSED
        with pytest.raises(RefusedError) as exc:
            open_it(spread(), verifier=gate, context=context_at(), tmp_path=tmp_path)
        assert "REFUSED" in exc.value.message
        assert not isinstance(exc.value, AwaitingVerification)

    def test_unavailable_blocks(self, tmp_path: Path) -> None:
        gate = approving_gate(tmp_path)
        gate.reviewer.decision = ApprovalDecision.UNAVAILABLE
        with pytest.raises(RefusedError) as exc:
            open_it(spread(), verifier=gate, context=context_at(), tmp_path=tmp_path)
        assert "UNAVAILABLE" in exc.value.message

    def test_a_refusal_is_not_overridden_by_an_approval_beside_it(
        self, tmp_path: Path
    ) -> None:
        """Two answers to one request, one of each. The refusal wins.

        A gate that scanned for "is there an APPROVED" would pass this, and a
        reviewer who changed their mind would be silently ignored.
        """
        root = collab_at(tmp_path)
        gate = CollabVerifierGate(root=root, ledger=tmp_path / "ledger")
        context = context_at()
        intent = spread()
        with pytest.raises(AwaitingVerification):
            open_it(intent, verifier=gate, context=context, tmp_path=tmp_path)

        approver = ScriptedReviewer(root=root)
        approver.work(NOW)
        refuser = ScriptedReviewer(root=root, decision=ApprovalDecision.REFUSED)
        # The request is done, so answer it directly: a second reviewer replying
        # to the same thread is exactly the change-of-mind case.
        request_id = gate.request_id_for(
            packet(
                intent,
                risk=approving_risk(intent.strategy_id),
                governor=approving_governor(intent),
                context=context,
                now=NOW,
            ).spec
        )
        store = store_at(root)
        store.create(
            to="builder",
            sender="grok",
            title="REFUSED: on reflection",
            body=render_response(
                decision=ApprovalDecision.REFUSED,
                request_id=request_id,
                intent_id=intent.strategy_id,
            ),
            tags=["verification"],
        )
        with pytest.raises(RefusedError) as exc:
            open_it(intent, verifier=gate, context=context, tmp_path=tmp_path)
        assert "REFUSED" in exc.value.message

    def test_a_missing_artifact_blocks(self, tmp_path: Path) -> None:
        """No reviewer at all. The request is filed and nothing answers it."""
        root = collab_at(tmp_path)
        gate = CollabVerifierGate(root=root, ledger=tmp_path / "ledger")
        with pytest.raises(AwaitingVerification):
            open_it(spread(), verifier=gate, context=context_at(), tmp_path=tmp_path)

    def test_no_collab_at_all_blocks(self, tmp_path: Path) -> None:
        """A gate pointed at nothing must refuse, not proceed."""
        gate = CollabVerifierGate(root=tmp_path / "nowhere", ledger=tmp_path / "ledger")
        with pytest.raises(Exception) as exc:
            open_it(spread(), verifier=gate, context=context_at(), tmp_path=tmp_path)
        assert isinstance(exc.value, (RefusedError, OSError))

    def test_an_expired_ttl_blocks(self, tmp_path: Path) -> None:
        """Approved at NOW for thirty minutes, authorized an hour later.

        Stepped by hand so the same approval is the one being re-read; an
        auto-answering reviewer would simply issue a fresh one and the test would
        prove nothing about staleness.
        """
        gate, reviewer = paused(tmp_path, lifetime=dt.timedelta(minutes=30))
        context = context_at()
        intent = spread()
        with pytest.raises(AwaitingVerification):
            open_it(intent, verifier=gate, context=context, tmp_path=tmp_path)
        reviewer.work(NOW)

        with pytest.raises(RefusedError) as exc:
            open_it(
                intent,
                verifier=gate,
                context=context,
                tmp_path=tmp_path,
                now=NOW + dt.timedelta(hours=1),
            )
        assert "expired" in (exc.value.hint or "")

        # And the control: the same approval at the same instant it was issued
        # authorizes, so the refusal above is the TTL and not something else.
        assert open_it(
            intent, verifier=gate, context=context, tmp_path=tmp_path, now=NOW
        ).approval is not None

    def test_an_answer_dated_in_the_future_blocks(self, tmp_path: Path) -> None:
        gate = approving_gate(tmp_path)
        drift = dt.timedelta(hours=6)

        def ahead(fields: dict[str, Any]) -> None:
            fields["approved_at"] = fields["approved_at"] + drift
            fields["expires_at"] = fields["expires_at"] + drift

        gate.reviewer.mangle = ahead
        with pytest.raises(RefusedError) as exc:
            open_it(spread(), verifier=gate, context=context_at(), tmp_path=tmp_path)
        assert "future" in (exc.value.hint or "")

    def test_a_mismatched_intent_id_blocks(self, tmp_path: Path) -> None:
        gate = approving_gate(tmp_path)
        gate.reviewer.mangle = lambda fields: fields.__setitem__("intent_id", uuid4())
        with pytest.raises(RefusedError) as exc:
            open_it(spread(), verifier=gate, context=context_at(), tmp_path=tmp_path)
        assert "answers intent" in (exc.value.hint or "")

    def test_a_mismatched_digest_blocks(self, tmp_path: Path) -> None:
        gate = approving_gate(tmp_path)
        gate.reviewer.mangle = lambda fields: fields.__setitem__("spec_digest", "b" * 64)
        with pytest.raises(RefusedError) as exc:
            open_it(spread(), verifier=gate, context=context_at(), tmp_path=tmp_path)
        assert "approved spec" in (exc.value.hint or "")

    def test_a_mismatched_request_id_blocks(self, tmp_path: Path) -> None:
        """An answer that binds a different handoff is not an answer to this one."""
        gate = approving_gate(tmp_path)
        gate.reviewer.mangle = lambda fields: fields.__setitem__("request_id", "not-the-request")
        with pytest.raises(AwaitingVerification):
            open_it(spread(), verifier=gate, context=context_at(), tmp_path=tmp_path)

    def test_a_response_not_from_the_reviewer_seat_blocks(self, tmp_path: Path) -> None:
        """Signed by someone other than grok. Blocks even though everything else fits."""
        gate = approving_gate(tmp_path)
        gate.reviewer.mangle = lambda fields: fields.__setitem__("verifier", "claude")
        with pytest.raises(RefusedError) as exc:
            open_it(spread(), verifier=gate, context=context_at(), tmp_path=tmp_path)
        assert "reviewer seat" in (exc.value.hint or "")

    def test_an_answer_that_skipped_the_claim_blocks(self, tmp_path: Path) -> None:
        """The lifecycle check. A reply that never claimed its request did not
        come through the two-seat exchange, whatever its contents say."""
        root = collab_at(tmp_path)
        gate = CollabVerifierGate(root=root, ledger=tmp_path / "ledger")
        context = context_at()
        intent = spread()
        with pytest.raises(AwaitingVerification):
            open_it(intent, verifier=gate, context=context, tmp_path=tmp_path)

        request_id = gate.request_id_for(
            packet(
                intent,
                risk=approving_risk(intent.strategy_id),
                governor=approving_governor(intent),
                context=context,
                now=NOW,
            ).spec
        )
        # A loose reply dropped straight into pending/: right shape, no lifecycle.
        store_at(root).create(
            to="builder",
            sender="grok",
            title="APPROVED: dropped over the wall",
            body=render_response(
                decision=ApprovalDecision.APPROVED,
                request_id=request_id,
                intent_id=intent.strategy_id,
                spec_digest=gate_spec(intent, context).digest,
                approved_at=NOW,
                expires_at=NOW + dt.timedelta(hours=1),
            ),
            tags=["verification"],
        )
        with pytest.raises(RefusedError) as exc:
            open_it(intent, verifier=gate, context=context, tmp_path=tmp_path)
        hint = exc.value.hint or ""
        assert "did not close it" in hint or "never claimed" in hint

    def test_a_reused_approval_blocks(self, tmp_path: Path) -> None:
        gate = approving_gate(tmp_path)
        context = context_at()
        intent = spread()
        open_it(intent, verifier=gate, context=context, tmp_path=tmp_path)
        with pytest.raises(RefusedError) as exc:
            open_it(intent, verifier=gate, context=context, tmp_path=tmp_path)
        assert "already consumed" in (exc.value.hint or "")

    @pytest.mark.parametrize(
        "field,value",
        [
            ("quantity", 5),
            ("credit", "1.25"),
        ],
    )
    def test_changing_the_order_after_approval_blocks(
        self, tmp_path: Path, field: str, value: Any
    ) -> None:
        """Approve one order, then authorize a different one under the same id.

        The approval was for a spec digest; the altered order hashes differently,
        so the answer on disk covers something that is not being sent.
        """
        gate, reviewer = paused(tmp_path)
        context = context_at()
        approved = spread()
        with pytest.raises(AwaitingVerification):
            open_it(approved, verifier=gate, context=context, tmp_path=tmp_path)
        reviewer.work(NOW)
        assert open_it(
            approved, verifier=gate, context=context, tmp_path=tmp_path
        ).approval is not None

        altered = spread(**{field: value})
        object.__setattr__(altered, "strategy_id", approved.strategy_id)
        # The approval on disk is for the old structure. The altered order is
        # unreviewed, and the gate says so rather than reaching for the nearest
        # approval that shares an intent id.
        with pytest.raises(AwaitingVerification):
            open_it(altered, verifier=gate, context=context, tmp_path=tmp_path)

    def test_changing_the_legs_changes_the_spec_digest(self, tmp_path: Path) -> None:
        """The invalidation rule, at the level of the digest itself."""
        context = context_at()
        one = spread()
        two = spread(underlying="AAPL")
        object.__setattr__(two, "strategy_id", one.strategy_id)
        assert gate_spec(one, context).digest != gate_spec(two, context).digest

    def test_a_wrong_commit_sha_blocks(self, tmp_path: Path) -> None:
        """An approval issued against a different build does not carry forward.

        Expressed by moving the *context*: the answer was computed for the spec
        under commit A, and the order is now being authorized under commit B, so
        the spec digest -- which binds the commit -- no longer matches.
        """
        context = context_at()
        rebuilt = ApprovalContext(
            account=context.account,
            port=context.port,
            commit_sha="0" * 40,
            configuration_fingerprint=context.configuration_fingerprint,
        )
        intent = spread()
        assert gate_spec(intent, context).digest != gate_spec(intent, rebuilt).digest

        gate, reviewer = paused(tmp_path)
        with pytest.raises(AwaitingVerification):
            open_it(intent, verifier=gate, context=context, tmp_path=tmp_path)
        reviewer.work(NOW)
        assert open_it(
            intent, verifier=gate, context=context, tmp_path=tmp_path
        ).approval is not None
        with pytest.raises(AwaitingVerification):
            open_it(intent, verifier=gate, context=rebuilt, tmp_path=tmp_path)

    def test_a_wrong_configuration_fingerprint_blocks(self, tmp_path: Path) -> None:
        context = context_at()
        reconfigured = ApprovalContext(
            account=context.account,
            port=context.port,
            commit_sha=context.commit_sha,
            configuration_fingerprint="deadbeef" * 8,
        )
        intent = spread()
        assert gate_spec(intent, context).digest != gate_spec(intent, reconfigured).digest

        gate, reviewer = paused(tmp_path)
        with pytest.raises(AwaitingVerification):
            open_it(intent, verifier=gate, context=context, tmp_path=tmp_path)
        reviewer.work(NOW)
        assert open_it(
            intent, verifier=gate, context=context, tmp_path=tmp_path
        ).approval is not None
        with pytest.raises(AwaitingVerification):
            open_it(intent, verifier=gate, context=reconfigured, tmp_path=tmp_path)

    def test_a_different_account_or_port_changes_the_spec(self, tmp_path: Path) -> None:
        """Account and port are in the digest, so an approval cannot be replayed
        against another paper account or another venue."""
        intent = spread()
        base = context_at()
        assert gate_spec(intent, base).digest != gate_spec(
            intent, context_at(account="DU7654321")
        ).digest
        assert gate_spec(intent, base).digest != gate_spec(
            intent, context_at(port=4002)
        ).digest

    def test_a_changed_risk_result_changes_the_spec(self, tmp_path: Path) -> None:
        """The risk and governor verdicts are inside the digest, so an approval
        issued when every check passed does not survive a re-run that refused."""
        from test_options_transmit import refusing_risk

        intent = spread()
        context = context_at()
        approved = packet(
            intent,
            risk=approving_risk(intent.strategy_id),
            governor=approving_governor(intent),
            context=context,
            now=NOW,
        )
        refused = packet(
            intent,
            risk=refusing_risk(intent.strategy_id),
            governor=approving_governor(intent),
            context=context,
            now=NOW,
        )
        assert approved.spec.digest != refused.spec.digest

    def test_a_packet_describing_another_order_blocks(self, tmp_path: Path) -> None:
        """The packet is a binding, not a claim.

        ``authorize_open`` re-derives the spec from the intent, risk and governor
        actually in hand and refuses if it differs from the packet's. Without
        this, a caller could submit one order for review and authorize another.
        """
        gate = approving_gate(tmp_path)
        context = context_at()
        reviewed_intent = spread()
        sent_intent = spread(quantity=9)
        with pytest.raises(RefusedError) as exc:
            authorize_open(
                sent_intent,
                gate=gate_for(tmp_path),
                risk=approving_risk(sent_intent.strategy_id),
                governor=approving_governor(sent_intent),
                armed=True,
                now=NOW,
                verifier=gate,
                packet=packet(
                    reviewed_intent,
                    risk=approving_risk(reviewed_intent.strategy_id),
                    governor=approving_governor(reviewed_intent),
                    context=context,
                    now=NOW,
                ),
            )
        assert "does not describe the order being authorized" in exc.value.message

    def test_an_over_long_approval_lifetime_is_refused(self, tmp_path: Path) -> None:
        """A reviewer cannot hand out a standing licence by widening the window."""
        gate = approving_gate(tmp_path)
        gate.reviewer.lifetime = MAXIMUM_APPROVAL_LIFETIME + dt.timedelta(hours=1)
        with pytest.raises(ApprovalDefect) as exc:
            open_it(spread(), verifier=gate, context=context_at(), tmp_path=tmp_path)
        assert "maximum" in exc.value.message

    def test_a_truncated_verification_block_is_a_defect_not_a_skip(self) -> None:
        """A half-written answer blocks. Skipping it would mean the way past the
        gate is to break the artifact slightly."""
        with pytest.raises(ApprovalDefect):
            verification_block("```verification\ndecision: APPROVED\n")

    def test_two_verification_blocks_are_a_defect(self) -> None:
        body = (
            "```verification\ndecision: APPROVED\n```\n"
            "```verification\ndecision: REFUSED\n```\n"
        )
        with pytest.raises(ApprovalDefect):
            verification_block(body)

    def test_an_unknown_decision_is_a_defect(self, tmp_path: Path) -> None:
        from engine.options.approval import _approval_from_handoff

        class Fake:
            id = "x"
            body = "```verification\ndecision: MAYBE\nrequest_id: r\nintent_id: {}\n```".format(
                uuid4()
            )

        with pytest.raises(ApprovalDefect):
            _approval_from_handoff(Fake())

    def test_the_real_reviewer_liveness_reply_authorizes_nothing(self) -> None:
        """The artifact that actually exists on disk today.

        It is an APPROVED handoff -- of the *handshake*, not of a trade -- and it
        carries no verification block. The gate must read it without error and
        treat it as no answer at all, which is precisely what its own section 3.5
        says the standing posture is.
        """
        # Searched upward rather than hard-coded: this suite runs from a git
        # worktree, and the collab tree lives at the main checkout's root.
        here = Path(__file__).resolve()
        real = None
        for parent in here.parents:
            candidate = (
                parent
                / ".collab"
                / "ibkr"
                / "handoffs"
                / "pending"
                / "20260730T200057Z-340829-approved-grok-verifier-liveness-post-trade-revie.md"
            )
            if candidate.is_file():
                real = candidate
                break
        if real is None:  # pragma: no cover - collab state is git-ignored
            pytest.skip("the reviewer's liveness reply is not present on this machine")
        parse_file = load("frontmatter", "parse_file")
        _meta, body = parse_file(real)
        assert "APPROVED" in body
        assert verification_block(body) is None


def gate_spec(intent: Any, context: ApprovalContext) -> AuthorizedOrderSpec:
    return packet(
        intent,
        risk=approving_risk(intent.strategy_id),
        governor=approving_governor(intent),
        context=context,
        now=NOW,
    ).spec


# ===========================================================================
# The control: nothing that reduces or inspects risk may be gated
# ===========================================================================


class TestTheControl:
    """No approval exists anywhere in these tests. Everything still works.

    This is the assertion the module docstring of ``engine.options.transmit``
    argues for: an engine that cannot get *out* of a position is in a worse
    state than one that got in without review. If the gate ever leaks into
    these paths, these tests fail before anyone finds out in a live account.
    """

    def test_closing_needs_no_approval(self, tmp_path: Path) -> None:
        opened = spread()
        closing = spread()
        object.__setattr__(closing, "strategy_action", StrategyAction.CLOSE)
        object.__setattr__(closing, "closes_strategy_id", opened.strategy_id)
        object.__setattr__(closing, "price_effect", closing.price_effect)

        authorization = authorize_close(
            closing, gate=gate_for(tmp_path), armed=True, now=NOW
        )
        assert authorization.action is StrategyAction.CLOSE
        assert authorization.approval is None
        assert authorization.spec is None

    def test_cancelling_needs_no_approval(self, tmp_path: Path) -> None:
        authorization = authorize_cancel(
            uuid4(), gate=gate_for(tmp_path), armed=True, now=NOW, reason="control"
        )
        assert authorization.strategy_id is not None

    def test_neither_close_nor_cancel_names_the_verifier(self) -> None:
        """Structural, not merely behavioural. A verifier argument appearing on
        either signature is the affordance this control exists to forbid."""
        import inspect

        for function in (authorize_close, authorize_cancel):
            parameters = set(inspect.signature(function).parameters)
            assert "verifier" not in parameters, function.__name__
            assert "packet" not in parameters, function.__name__

    def test_reconciliation_and_inspection_never_reach_the_gate(self) -> None:
        """The reconciler and the position store must not import the gate at all.

        Checked by import graph rather than by running them: a module that
        cannot name the gate cannot be blocked by it, however the code inside
        changes later.
        """
        import ast

        from engine.options import positions as positions_module

        tree = ast.parse(Path(positions_module.__file__).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not any("approval" in name for name in imported), imported

    def test_the_entry_gate_sits_after_reconciliation_and_management(self) -> None:
        """Ordering, not merely presence -- the shape of the trapping failure.

        A fail-closed verifier check placed at the top of ``run_once`` would
        refuse the whole pass when no reviewer is configured, and an engine that
        will not reconcile or manage because nobody approved a *new* trade is
        exactly the trapped-position failure this design ranks worst. So the
        refusal must come after both.
        """
        import ast

        from engine.options import runner as runner_module

        source = Path(runner_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        run_once = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "run_once"
        )
        lines = source.splitlines()

        def line_of(needle: str) -> int:
            for offset in range(run_once.lineno - 1, len(lines)):
                if needle in lines[offset]:
                    return offset + 1
            raise AssertionError(f"{needle!r} is not in run_once")

        reconcile = line_of("_reconcile(broker, store")
        manage = line_of("_manage_one(")
        refusal = line_of("OPTIONS_VERIFIER_NOT_CONFIGURED")
        assert reconcile < refusal, (
            f"the verifier refusal at {refusal} precedes reconciliation at "
            f"{reconcile} -- a pass with no reviewer would stop reconciling"
        )
        assert manage < refusal, (
            f"the verifier refusal at {refusal} precedes management at {manage} "
            "-- a pass with no reviewer would stop managing open positions"
        )

    def test_the_runner_refuses_the_entry_rather_than_the_pass(self) -> None:
        """The refusal is a blocker on the report, not a raise.

        ``run_once`` returns a report for everything except the kill switch. A
        verifier gate that raised would take the exits down with it.
        """
        import ast

        from engine.options import runner as runner_module

        tree = ast.parse(Path(runner_module.__file__).read_text(encoding="utf-8"))
        run_once = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "run_once"
        )
        guards = [
            node
            for node in ast.walk(run_once)
            if isinstance(node, ast.If)
            for name in ast.walk(node.test)
            if isinstance(name, ast.Name) and name.id == "verifier"
        ]
        assert guards, "run_once does not check for a verifier at all"
        for guard in guards:
            raises = [
                inner for inner in ast.walk(guard) if isinstance(inner, ast.Raise)
            ]
            assert raises == [], (
                f"the verifier guard at line {guard.lineno} raises instead of "
                "recording a blocker, which would abort the whole pass"
            )



# ===========================================================================
# Mutation checks: each guard is load-bearing
# ===========================================================================


class TestTheGuardsAreLoadBearing:
    """Each test removes one input and asserts the *specific* refusal.

    A refusal test that only asserts "it raised" passes when the code raises for
    an unrelated reason, which is how a guard rots into decoration without any
    test noticing.
    """

    def test_an_opening_token_cannot_exist_without_an_approval(self) -> None:
        """Constructed directly, past ``authorize_open``, with the private key
        unavailable -- so the assertion is that the *type* refuses, not the
        function."""
        from engine.options import transmit as transmit_module

        intent = spread()
        with pytest.raises(RefusedError) as exc:
            transmit_module.TransmitAuthorization(
                strategy_id=intent.strategy_id,
                action=StrategyAction.OPEN,
                authorized_at=NOW,
                armed=True,
                digest=structure_digest(intent),
                risk=approving_risk(intent.strategy_id),
                governor=approving_governor(intent),
                key=transmit_module._AUTHORIZATION_KEY,
            )
        assert "independent verifier approval" in exc.value.message

    def test_an_approval_bound_to_another_spec_is_refused_by_the_token(
        self, tmp_path: Path
    ) -> None:
        """The invariant travels with the object, not just with the function."""
        from engine.options import transmit as transmit_module

        gate = approving_gate(tmp_path)
        context = context_at()
        intent = spread()
        good = open_it(intent, verifier=gate, context=context, tmp_path=tmp_path)

        other = spread(quantity=4)
        with pytest.raises(RefusedError) as exc:
            transmit_module.TransmitAuthorization(
                strategy_id=intent.strategy_id,
                action=StrategyAction.OPEN,
                authorized_at=NOW,
                armed=True,
                digest=structure_digest(intent),
                risk=approving_risk(intent.strategy_id),
                governor=approving_governor(intent),
                spec=gate_spec(other, context),
                approval=good.approval,
                key=transmit_module._AUTHORIZATION_KEY,
            )
        assert "binds spec" in exc.value.message

    def test_a_verifier_approval_cannot_be_constructed(self) -> None:
        from engine.options.approval import VerifierApproval

        with pytest.raises(RefusedError):
            VerifierApproval(
                decision=ApprovalDecision.APPROVED,
                request_id="r",
                response_id="x",
                intent_id=uuid4(),
                spec_digest="a" * 64,
                verifier="grok",
                approved_at=NOW,
                expires_at=NOW,
                sender_seat="reviewer",
                thread="t",
                source=Path("."),
            )

    def test_an_unknown_commit_refuses_the_context(self) -> None:
        """``commit_sha_at`` returning "" must not become an approval bound to
        nothing. This is the branch that fires on a tarball with no ``.git``."""
        with pytest.raises(RefusedError) as exc:
            ApprovalContext(
                account="DU1234567",
                port=7497,
                commit_sha="",
                configuration_fingerprint="x",
            )
        assert "commit sha" in exc.value.message

    def test_the_commit_reader_agrees_with_git(self) -> None:
        """Mutation check on the reader itself. A ``commit_sha_at`` that always
        returned the same wrong constant would satisfy every other test here."""
        import subprocess

        expected = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert commit_sha_at() == expected

    def test_the_order_type_and_tif_are_really_in_the_digest(self) -> None:
        """Named constants that were not hashed would be documentation."""
        intent = spread()
        context = context_at()
        base = packet_for(
            intent,
            structure_digest=structure_digest(intent),
            risk=approving_risk(intent.strategy_id),
            governor=approving_governor(intent),
            context=context,
            order_type=COMBO_ORDER_TYPE,
            time_in_force=COMBO_TIME_IN_FORCE,
            now=NOW,
        ).spec
        market = packet_for(
            intent,
            structure_digest=structure_digest(intent),
            risk=approving_risk(intent.strategy_id),
            governor=approving_governor(intent),
            context=context,
            order_type="MKT",
            time_in_force=COMBO_TIME_IN_FORCE,
            now=NOW,
        ).spec
        gtc = packet_for(
            intent,
            structure_digest=structure_digest(intent),
            risk=approving_risk(intent.strategy_id),
            governor=approving_governor(intent),
            context=context,
            order_type=COMBO_ORDER_TYPE,
            time_in_force="GTC",
            now=NOW,
        ).spec
        assert len({base.digest, market.digest, gtc.digest}) == 3

    def test_the_consumption_marker_is_exclusive(self, tmp_path: Path) -> None:
        """``consume`` twice on the same approval must raise the second time.

        Asserted against the gate directly rather than through the runner, so a
        change that moved consumption earlier or later cannot hide the property.
        """
        gate = approving_gate(tmp_path)
        context = context_at()
        intent = spread()
        authorization = open_it(
            intent, verifier=gate, context=context, tmp_path=tmp_path
        )
        with pytest.raises(RefusedError) as exc:
            gate.consume(authorization.approval, now=NOW)
        assert "already been consumed" in exc.value.message

    def test_the_packet_marks_missing_evidence_rather_than_omitting_it(self) -> None:
        """A silently short packet would come back APPROVED on incomplete
        evidence. Every expected field must appear, absent ones as MISSING."""
        intent = spread()
        rendered = packet(
            intent,
            risk=approving_risk(intent.strategy_id),
            governor=approving_governor(intent),
            context=context_at(),
            now=NOW,
        ).render()
        for name in ("iv_rank", "stress_loss", "broker_what_if_margin"):
            assert name in rendered
        assert "**MISSING**" in rendered


# ===========================================================================
# Mis-addressed blocking answers (M4): a "no" about another order is not a
# "no" about this one
# ===========================================================================


class TestMisaddressedBlockingAnswers:
    """A non-APPROVED answer blocks only when it is about THIS order (intent
    id) and from the reviewer route. A reply that names a foreign intent never
    judged this order; it is recorded as a reason, never a refusal -- while a
    correctly-addressed REFUSED still blocks with no digest demanded of it
    (the anti-evasion property in the module docstring)."""

    def test_a_refused_naming_a_foreign_intent_does_not_refuse_this_order(
        self, tmp_path: Path
    ) -> None:
        """MUTATION GUARD (M4): revert the intent-id check on blocking answers
        and this fails -- the mis-addressed REFUSED would refuse an order it
        never judged, instead of leaving it awaiting its real answer."""
        gate = approving_gate(tmp_path)
        gate.reviewer.decision = ApprovalDecision.REFUSED
        gate.reviewer.mangle = lambda fields: fields.__setitem__("intent_id", uuid4())
        with pytest.raises(AwaitingVerification):
            open_it(spread(), verifier=gate, context=context_at(), tmp_path=tmp_path)

    def test_a_refused_naming_this_intent_still_blocks_with_no_digest(
        self, tmp_path: Path
    ) -> None:
        """The control, and the preserved anti-evasion property: a reviewer's
        REFUSED that names this intent blocks outright -- no spec digest is
        demanded of a refusal, so a re-quote cannot evade the 'no'."""
        gate = approving_gate(tmp_path)
        gate.reviewer.decision = ApprovalDecision.REFUSED
        with pytest.raises(RefusedError) as exc:
            open_it(spread(), verifier=gate, context=context_at(), tmp_path=tmp_path)
        assert "REFUSED" in exc.value.message
        assert not isinstance(exc.value, AwaitingVerification)

    def test_a_refused_from_a_non_reviewer_seat_does_not_block(
        self, tmp_path: Path
    ) -> None:
        """A REFUSED carrying the right intent but sent from the builder's own
        seat is not the reviewer saying no; it is recorded, not obeyed."""
        root = collab_at(tmp_path)
        gate = CollabVerifierGate(root=root, ledger=tmp_path / "ledger")
        context = context_at()
        intent = spread()
        with pytest.raises(AwaitingVerification) as awaiting:
            open_it(intent, verifier=gate, context=context, tmp_path=tmp_path)
        request_id = awaiting.value.request_id

        store_at(root).create(
            to="builder",
            sender="builder",  # the wrong seat: not the reviewer route
            title="REFUSED: mis-sent",
            body=render_response(
                decision=ApprovalDecision.REFUSED,
                request_id=request_id,
                intent_id=intent.strategy_id,
            ),
            tags=["verification"],
        )
        with pytest.raises(AwaitingVerification):
            open_it(intent, verifier=gate, context=context, tmp_path=tmp_path)

    def test_a_correctly_addressed_unavailable_still_blocks(
        self, tmp_path: Path
    ) -> None:
        """UNAVAILABLE is the other blocking decision, and the narrowed check
        must not have loosened it."""
        gate = approving_gate(tmp_path)
        gate.reviewer.decision = ApprovalDecision.UNAVAILABLE
        with pytest.raises(RefusedError) as exc:
            open_it(spread(), verifier=gate, context=context_at(), tmp_path=tmp_path)
        assert "UNAVAILABLE" in exc.value.message


# ===========================================================================
# The request marker is installed atomically (minor a)
# ===========================================================================


class TestRequestMarkerDurability:
    def test_the_request_marker_cannot_half_exist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTATION GUARD (minor a): revert ``propose`` to a direct
        ``write_text`` of the marker and this fails -- with the atomic install
        broken, propose must fail loudly and leave NO marker behind, never a
        torn one that 'remembers' an id nothing can find."""
        from engine.options import approval as approval_module

        root = collab_at(tmp_path)
        gate = CollabVerifierGate(root=root, ledger=tmp_path / "ledger")
        intent = spread()
        proposal = packet(
            intent,
            risk=approving_risk(intent.strategy_id),
            governor=approving_governor(intent),
            context=context_at(),
            now=NOW,
        )

        real_replace = approval_module.os.replace
        marker = gate._request_marker(proposal.spec)

        def broken_replace(source: Any, destination: Any) -> None:
            if str(destination) == str(marker):
                raise OSError("simulated crash installing the request marker")
            real_replace(source, destination)

        monkeypatch.setattr(approval_module.os, "replace", broken_replace)
        with pytest.raises(OSError):
            gate.propose(proposal, now=NOW)

        assert not marker.exists()
        assert gate.request_id_for(proposal.spec) == ""
        assert list(marker.parent.glob("*.tmp")) == [], "a torn temp file leaked"
