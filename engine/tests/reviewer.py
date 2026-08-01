"""A reviewer seat for the tests, driving the real collab-kit lifecycle.

This is **not** a mock of the gate. :class:`ScriptedReviewer` claims and answers
handoffs through :class:`collabkit.store.HandoffStore` exactly as the Grok seat
does -- ``claim`` moves ``pending -> claimed``, ``reply`` writes the answer and
completes the parent -- and the answer body is produced by the shipped
:func:`engine.options.approval.render_response`. Every check in
:meth:`~engine.options.approval.CollabVerifierGate.require` therefore runs
against a real exchange: the real request, the real thread, the real claim, the
real completion note, the real fenced block, the real parser.

What is collapsed is only *latency*. :class:`ReviewedGate` runs the reviewer's
turn immediately before the builder looks for an answer, so a test that would
otherwise need two passes and a wait needs neither. The two-pass behaviour --
propose, get ``AWAITING_VERIFICATION``, come back later -- is exercised
separately by driving :class:`ScriptedReviewer` by hand, which is what the
end-to-end lifecycle proof does.

Reviewers can be told to answer wrongly on purpose. That is the point: every
negative proof in ``test_options_verifier_gate.py`` is a real reviewer producing
a real artifact that the gate must reject, rather than a hand-built object
asserted about directly.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from engine._collabkit import load
from engine.options.approval import (
    ApprovalDecision,
    CollabVerifierGate,
    REVIEWER_SEAT,
    VerificationPacket,
    render_response,
    verification_block,
)

__all__ = [
    "ReviewedGate",
    "ScriptedReviewer",
    "approval_context",
    "approving_gate",
    "block_of",
    "collab_at",
    "gate_at",
    "packet",
    "reviewed",
]


def collab_at(root: Path, name: str = "ibkr") -> Path:
    """Create an empty collab under ``root`` and return its directory."""
    paths = load("paths", "CollabPaths").at(root / ".collab" / name, name)
    paths.ensure()
    return Path(paths.root)


def gate_at(tmp_path: Path, *, ledger: Path | None = None) -> CollabVerifierGate:
    """A real :class:`CollabVerifierGate` over a fresh temp collab."""
    return CollabVerifierGate(
        root=collab_at(tmp_path),
        ledger=(ledger or (tmp_path / "state" / "verification")),
    )


@dataclass
class ScriptedReviewer:
    """The reviewer seat. Claims what it is sent and answers as instructed.

    ``decision`` is what it returns; the ``mangle`` hook receives the parsed
    request and the answer fields about to be written, and may corrupt any of
    them. Corrupting through this hook rather than by editing files afterwards
    keeps every negative case on the same path as the positive one -- the answer
    is still written by ``reply``, still threaded, still closes its parent.
    """

    root: Path
    decision: ApprovalDecision = ApprovalDecision.APPROVED
    seat: str = REVIEWER_SEAT
    lifetime: dt.timedelta = dt.timedelta(hours=1)
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)
    mangle: Callable[[dict[str, Any]], None] | None = None
    #: When False the reviewer answers without claiming first, which is the
    #: "arrived outside the lifecycle" case the gate must reject.
    claim_first: bool = True
    answered: list[str] = field(default_factory=list)

    def _store(self) -> Any:
        paths = load("paths", "CollabPaths").at(self.root)
        return load("store", "HandoffStore")(paths)

    def work(self, now: dt.datetime | None = None) -> list[str]:
        """Answer every unanswered verification request. Returns the reply ids.

        ``now`` is the instant the reviewer stamps its answer with. Threaded in
        rather than read from the wall clock because the suite runs at a fixed
        logical time, and an answer dated "actually now" against a request dated
        2026-07-29 is refused as future-dated -- correctly, which is why the
        clock has to be one clock.
        """
        store = self._store()
        replies: list[str] = []
        for handoff in store.list(("pending", "claimed"), to="reviewer"):
            if "verification" not in (handoff.tags or []):
                continue
            if handoff.id in self.answered:
                continue
            replies.append(self._answer(store, handoff, now=now))
        return replies

    def _answer(self, store: Any, request: Any, *, now: dt.datetime | None = None) -> str:
        fields = self._fields_for(request, now=now)
        if self.mangle is not None:
            self.mangle(fields)
        body = render_response(**fields)
        if self.claim_first and request.status == "pending":
            store.claim(request.id, by=self.seat)
        reply, _closed = store.reply(
            request.id,
            title=f"{fields['decision'].value}: {request.title}",
            body=body,
            sender=self.seat,
        )
        self.answered.append(request.id)
        return str(reply.id)

    def _fields_for(self, request: Any, *, now: dt.datetime | None = None) -> dict[str, Any]:
        """Recompute the answer from the request body, as the real seat does.

        The digest and the intent id are read back out of the *packet*, not
        carried in from the caller. A reviewer that echoed values handed to it by
        the builder would be validating nothing, and a test built on one would
        pass whatever the builder said.
        """
        proposal = _packet_fields(request.body)
        issued = now or self.now()
        return {
            "decision": self.decision,
            "request_id": request.id,
            "intent_id": proposal["intent_id"],
            "spec_digest": proposal["spec_digest"],
            "verifier": self.seat,
            "approved_at": issued,
            "expires_at": issued + self.lifetime,
            "reasons": "recomputed independently by the test reviewer seat",
        }


def _packet_fields(body: str) -> dict[str, Any]:
    """Pull the intent id and spec digest back out of a rendered packet."""
    import re
    from uuid import UUID

    intent = re.search(r"Trade intent id \| `([0-9a-fA-F-]{36})`", body)
    digest = re.search(r"Authorization-spec digest \| `([0-9a-f]{64})`", body)
    if intent is None or digest is None:  # pragma: no cover - malformed packet
        raise AssertionError("the request body is not a verification packet")
    return {"intent_id": UUID(intent.group(1)), "spec_digest": digest.group(1)}


@dataclass
class ReviewedGate(CollabVerifierGate):
    """A real gate with the reviewer's turn taken for it, immediately.

    Subclasses rather than wraps, so ``require`` and ``consume`` are the shipped
    implementations and only the *timing* of the reviewer's reply is different.
    """

    reviewer: ScriptedReviewer | None = None

    def require(self, packet: VerificationPacket, *, now: dt.datetime):
        # Propose first, so there is something for the reviewer to claim, then
        # let it answer, then run the real matching over the real exchange.
        self.propose(packet, now=now)
        if self.reviewer is not None:
            self.reviewer.work(now)
        return super().require(packet, now=now)


def approving_gate(tmp_path: Path, **kwargs: Any) -> ReviewedGate:
    """The common case: a gate whose reviewer approves whatever it is sent."""
    root = collab_at(tmp_path)
    return ReviewedGate(
        root=root,
        ledger=tmp_path / "state" / "verification",
        reviewer=ScriptedReviewer(root=root, **kwargs),
    )


def block_of(body: str) -> dict[str, Any] | None:
    """Re-export for tests that want to read an answer back off disk."""
    return verification_block(body)


def approval_context(config: Any = None, policy: Any = None):
    """An :class:`ApprovalContext` for a test config. Reads the real commit sha."""
    from engine.config import EngineConfig
    from engine.options.approval import ApprovalContext
    from engine.options.policy import RiskPolicy

    if config is None:
        config = EngineConfig(account_id="DU1234567", port=7497)
    return ApprovalContext.for_run(config=config, policy=policy or RiskPolicy())


def packet(intent: Any, *, risk: Any, governor: Any, context: Any, now: dt.datetime):
    """The packet a caller of ``authorize_open`` has to build. Shared, so every
    test builds it the way the runner does rather than inventing its own."""
    from engine.options.approval import packet_for
    from engine.options.execution import COMBO_ORDER_TYPE, COMBO_TIME_IN_FORCE
    from engine.options.transmit import structure_digest

    return packet_for(
        intent,
        structure_digest=structure_digest(intent),
        risk=risk,
        governor=governor,
        context=context,
        order_type=COMBO_ORDER_TYPE,
        time_in_force=COMBO_TIME_IN_FORCE,
        now=now,
    )


def reviewed(tmp_path: Path, **kwargs: Any) -> tuple[ReviewedGate, Any]:
    """``(gate, context)`` -- everything ``authorize_open`` now needs."""
    return approving_gate(tmp_path, **kwargs), approval_context()
