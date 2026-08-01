"""The independent-verifier gate on opening risk.

**The control this asserts, stated exactly.**

Opening or increasing risk requires an independent, autonomous review. The
builder session proposes; a separate persistent reviewer session claims,
recomputes and answers; and :func:`engine.options.transmit.authorize_open`
refuses to mint a token unless an APPROVED answer exists that binds the exact
handoff it was asked about, the exact trade intent, and the exact
:class:`AuthorizedOrderSpec` digest. Missing, expired, refused, unavailable,
already-consumed and mismatched all fail closed.

That is the whole claim. Three things it deliberately does **not** claim:

* It is **not** cryptographic tamper-proofing. Both seats run as the same
  Windows user against the same filesystem; a malicious process with that user's
  privileges is outside collab-kit's threat model and outside this gate's. No
  signature scheme changes that while both seats can read the same key material.
* It is **not** a human confirmation step. There is no prompt, no environment
  variable, no hash to copy. The reviewer is another agent session and the
  handoff lifecycle carries the decision on its own.
* It does **not** gate anything except opening and risk-increasing orders.

**The asymmetry is inherited deliberately.** ``engine.options.transmit`` explains
why closes and cancels are authorized more loosely than opens: trapping the
engine inside a position is a worse failure than letting it into one. This gate
binds :func:`~engine.options.transmit.authorize_open` and
:func:`~engine.options.transmit.authorize_reprice` -- the two functions that can
put new risk on -- and nothing else. ``authorize_close`` and ``authorize_cancel``
never reach it, reconciliation and inspection never reach it, and a missing or
unreachable reviewer blocks new risk without touching existing risk.

**Waiting never blocks.** :meth:`CollabVerifierGate.require` does not sleep, poll
or wait. If the reviewer has not answered yet it raises
:class:`AwaitingVerification`, the caller records the entry as
:attr:`VerificationState.AWAITING_VERIFICATION` and the pass continues: other
candidates are still evaluated, open positions are still managed, exits still
run. The answer is picked up on a later pass, because the proposal is on disk
and the request id is remembered.

**The artifact format is collab-kit's, not an invented one.** Requests and
responses are ordinary handoffs -- YAML frontmatter plus a markdown body, written
and moved through ``pending -> claimed -> done`` by
:class:`collabkit.store.HandoffStore`.

The machine-readable decision lives in a fenced ``verification`` block **in the
body**, and that placement is forced rather than chosen. collab-kit's
``create``/``reply`` take a title and a body and nothing else: there is no
channel for extra frontmatter keys. A reviewer that had to hand-edit the header
after collab-kit wrote the file would be doing exactly the out-of-band editing
this gate refuses to trust, so the one field a reviewer can actually write
through the real tooling is the one the gate reads.

The block is parsed by handing it to collab-kit's own
:func:`collabkit.frontmatter.parse` between ``---`` delimiters -- the same
strict flat-scalar subset, not a second parser written here to drift away from
it. The human-readable tables around it are for the human.

A handoff with no ``verification`` block is not a verification response and is
ignored. The reviewer's own liveness reply is exactly that: it loads fine, and
authorizes nothing, which is what its section 3.5 says it should do.
"""

from __future__ import annotations

import datetime as dt
import errno
import hashlib
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol
from uuid import UUID

from .. import _collabkit
from ..errors import RefusedError
from .domain import OptionStrategyIntent
from .governor import GovernorVerdict
from .risk import CandidateRiskAssessment

__all__ = [
    "ApprovalContext",
    "ApprovalDecision",
    "ApprovalDefect",
    "AuthorizedOrderSpec",
    "AwaitingVerification",
    "CLOCK_SKEW_TOLERANCE",
    "CollabVerifierGate",
    "ENV_COLLAB_ROOT",
    "default_collab_root",
    "MAXIMUM_APPROVAL_LIFETIME",
    "REVIEWER_SEAT",
    "VERIFICATION_PREFIX",
    "VerificationPacket",
    "VerificationState",
    "VerifierApproval",
    "VerifierGate",
    "commit_sha_at",
    "configuration_fingerprint",
    "governor_digest",
    "packet_for",
    "render_response",
    "verification_block",
    "VERIFICATION_FENCE",
    "risk_digest",
    "spec_for_open",
]

#: Flat frontmatter keys the gate reads and writes. collab-kit's frontmatter
#: subset has no nested mappings by design, so a prefix is how a namespace is
#: expressed -- and it keeps the fields greppable with a plain ``grep '^verification_'``.
VERIFICATION_PREFIX = "verification_"

#: The seat name the reviewer must sign with. collab-kit *canonicalises*
#: ``from: grok`` to the routing seat ``reviewer`` (see
#: ``collabkit.seats._BASE_ALIASES``), so the specific identity would be lost if
#: the gate only read ``from:``. It is carried in the verification header and
#: checked here; the routing seat is checked separately, and both must hold.
REVIEWER_SEAT = "grok"

#: The longest an approval may be valid for, however far out an artifact dates
#: its own expiry. A reviewer that fat-fingers a year into ``expires_at`` would
#: otherwise issue a standing licence, and a standing licence is precisely the
#: thing this gate exists to abolish.
MAXIMUM_APPROVAL_LIFETIME = dt.timedelta(hours=12)

#: Tolerance for an approval dated slightly ahead of this process's clock.
CLOCK_SKEW_TOLERANCE = dt.timedelta(minutes=5)

_BUILDER_SEAT = "builder"
_REVIEWER_ROUTE = "reviewer"

# The object that makes a VerifierApproval constructible. Module private, never
# exported, held only by the parser -- so the only way to hold an approval is to
# have read one off disk through the lifecycle.
_APPROVAL_KEY = object()


class ApprovalDecision(str, Enum):
    """The three outcomes the protocol allows, and nothing else.

    ``REFUSED`` and ``UNAVAILABLE`` are not "absence of APPROVED" -- they are
    recorded decisions that block, and they block even when an APPROVED artifact
    for the same request sits beside them. A reviewer who refuses after
    approving has changed their mind, and the later word is not automatically
    the one with the friendlier name.
    """

    APPROVED = "APPROVED"
    REFUSED = "REFUSED"
    UNAVAILABLE = "UNAVAILABLE"


class VerificationState(str, Enum):
    """Where one proposed entry stands with the reviewer.

    ``AWAITING_VERIFICATION`` is the state this whole design exists to make
    representable. Without it the only honest answers were "authorized" and
    "refused", and a pass that had asked but not yet heard back had to be
    reported as one or the other -- either of which is a lie that a later pass
    then acts on.
    """

    PROPOSED = "PROPOSED"
    AWAITING_VERIFICATION = "AWAITING_VERIFICATION"
    APPROVED = "APPROVED"
    REFUSED = "REFUSED"
    UNAVAILABLE = "UNAVAILABLE"
    CONSUMED = "CONSUMED"


class AwaitingVerification(RefusedError):
    """The request is filed and the reviewer has not answered yet.

    A subclass of :class:`~engine.errors.RefusedError` so that every existing
    caller keeps failing closed without being taught a new exception -- but a
    *distinct* type, so a caller that wants to say "come back next pass" rather
    than "this candidate is dead" can tell the two apart. The runner does; the
    difference is the whole of requirement 5, that waiting stalls nothing else.
    """

    def __init__(self, message: str, *, hint: str | None = None, request_id: str = "") -> None:
        super().__init__(message, hint=hint)
        self.request_id = request_id
        self.state = VerificationState.AWAITING_VERIFICATION


class ApprovalDefect(RefusedError):
    """A file that claims to be a verification response and does not parse.

    Raised rather than skipped, and blocking rather than ignorable. A malformed
    response has the same shape as a truncated or half-written one, and skipping
    it would mean the way to slip a bad artifact past the gate is to break it
    slightly.
    """


# ---------------------------------------------------------------------------
# digests
# ---------------------------------------------------------------------------


def _sha(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _check_rows(results: Any) -> list[list[str]]:
    """Every check as ``[name, verdict, reason, observed, limit]``, sorted.

    Sorted by check name rather than left in ``results`` order: the order the
    checks happened to run in is not part of what was approved, and a reordering
    would otherwise silently invalidate a live approval.

    ``detail`` is excluded on purpose. It is human prose carrying no bound, and
    binding it would kill an approval over a reworded message. The protocol asks
    for "every check name, observed, limit, pass/refuse" -- exactly this, and
    exactly not that.
    """
    return sorted(
        [
            str(result.check),
            "PASS" if result.approved else "REFUSE",
            result.reason_code or "",
            str(result.observed) if result.observed is not None else "",
            str(result.limit) if result.limit is not None else "",
        ]
        for result in results
    )


def risk_digest(risk: CandidateRiskAssessment) -> str:
    """A fingerprint of the candidate risk *result*, not of when it ran.

    ``evaluated_at`` is excluded deliberately. Including it would change the
    digest on every pass, so an approval would be stale the instant the reviewer
    finished reading it -- a gate that can never pass is an outage, not a safety
    feature. Staleness is bounded by the approval's TTL, which is the mechanism
    that is actually about time.
    """
    return _sha(
        {
            "strategy_id": str(risk.strategy_id),
            "policy_version": risk.policy_version,
            "approved": risk.approved,
            "checks": _check_rows(risk.results),
        }
    )


def governor_digest(governor: GovernorVerdict) -> str:
    """The same, for the portfolio governor's verdict.

    The portfolio snapshot behind the verdict is not hashed directly: its
    ``to_record`` reports aggregates rather than individual positions, so hashing
    it would imply a precision it does not have. What *is* bound is every
    governor check with its observed value and its limit, which is where the
    portfolio state shows up as a number the reviewer can recompute.
    """
    return _sha(
        {
            "underlying": governor.underlying.strip().upper(),
            "policy_version": governor.policy_version,
            "approved": governor.approved,
            "checks": _check_rows(governor.results),
        }
    )


def _is_sha(text: str) -> bool:
    return len(text) == 40 and all(c in "0123456789abcdef" for c in text.lower())


def commit_sha_at(start: Path | None = None) -> str:
    """The checked-out commit, read from ``.git`` without running ``git``.

    No subprocess: a trading path that shells out buys a hung ``git``, a missing
    binary and a locked index in exchange for nothing, since the files involved
    are plain text.

    Handles the worktree case, where ``.git`` is a *file* containing
    ``gitdir: <path>``. Every lane of this project runs in a worktree, so the
    plain-directory case is the one that would otherwise go untested.

    Returns ``""`` when the commit cannot be determined; :class:`ApprovalContext`
    refuses that, because an approval bound to an unknown build binds nothing.
    """
    here = (start or Path(__file__)).resolve()
    for candidate in [here, *here.parents]:
        marker = candidate / ".git"
        if not marker.exists():
            continue
        git_dir = marker
        if marker.is_file():
            try:
                pointer = marker.read_text(encoding="utf-8").strip()
            except OSError:  # pragma: no cover - unreadable .git file
                return ""
            if not pointer.startswith("gitdir:"):  # pragma: no cover - malformed
                return ""
            git_dir = Path(pointer.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = (candidate / git_dir).resolve()
        return _resolve_head(git_dir)
    return ""


def _resolve_head(git_dir: Path) -> str:
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:  # pragma: no cover - unreadable HEAD
        return ""
    if not head.startswith("ref:"):
        return head.lower() if _is_sha(head) else ""
    ref = head.split(":", 1)[1].strip()
    # Both roots, loose then packed. In a worktree the HEAD lives beside the
    # worktree gitdir but the branch ref it points at lives in the *common*
    # directory -- so reading only ``git_dir / ref`` returns nothing for every
    # lane of this project, which is every run that matters here.
    roots = [root for root in (git_dir, _common_dir(git_dir)) if root is not None]
    for root in roots:
        try:
            loose = (root / ref).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if _is_sha(loose):
            return loose.lower()
    for root in roots:
        try:
            packed = (root / "packed-refs").read_text(encoding="utf-8")
        except OSError:
            continue
        for line in packed.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref and _is_sha(parts[0]):
                return parts[0].lower()
    return ""  # pragma: no cover - a ref that is neither loose nor packed


def _common_dir(git_dir: Path) -> Path | None:
    try:
        text = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    resolved = Path(text)
    return resolved if resolved.is_absolute() else (git_dir / resolved).resolve()


def configuration_fingerprint(config: Any, policy: Any) -> str:
    """One hash over everything that decides whether a trade is permitted.

    The strategy policy supplies the risk and governor ceilings; the engine
    config supplies the caps, the allowlist and the venue. Both move the answer
    to "may this order go", so an approval issued under one set of numbers must
    not survive a change to the other -- which is exactly what happens when a
    fingerprint covers only the half that felt like "configuration".
    """
    try:
        policy_record = dict(policy.to_record())
    except AttributeError:  # pragma: no cover - a policy with no record
        policy_record = {"repr": repr(policy)}
    return _sha(
        {
            "policy": {str(k): str(v) for k, v in policy_record.items()},
            "engine": {
                "max_order_notional": str(getattr(config, "max_order_notional", "")),
                "max_position_qty": str(getattr(config, "max_position_qty", "")),
                "max_orders_per_session": str(getattr(config, "max_orders_per_session", "")),
                "max_margin_impact": str(getattr(config, "max_margin_impact", "")),
                "symbol_allowlist": sorted(
                    str(s) for s in getattr(config, "symbol_allowlist", ())
                ),
                "port": str(getattr(config, "port", "")),
                "account_id": str(getattr(config, "account_id", "")),
            },
        }
    )


# ---------------------------------------------------------------------------
# what an approval is an approval OF
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalContext:
    """The facts an approval binds that are not part of the order itself.

    An approval naming only the order would survive replay against a different
    account, a different venue, a different build of the engine, or a different
    set of risk ceilings. Each of those changes what the order *means* without
    changing the order, so each is in here and each is in the digest.
    """

    account: str
    port: int
    commit_sha: str
    configuration_fingerprint: str

    def __post_init__(self) -> None:
        if not self.account or not self.account.strip():
            raise RefusedError(
                "an approval context must name the account the order will be sent to",
                hint="an approval for one account must not authorize another",
            )
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise RefusedError(f"port must be an integer, got {self.port!r}")
        if not _is_sha(self.commit_sha):
            raise RefusedError(
                f"an approval context needs the 40-hex commit sha, got {self.commit_sha!r}",
                hint="an approval bound to an unknown build is bound to nothing; if the "
                "commit cannot be read here, no opening trade is authorized",
            )
        if not self.configuration_fingerprint:
            raise RefusedError("an approval context needs a configuration fingerprint")

    @classmethod
    def for_run(
        cls,
        *,
        config: Any,
        policy: Any,
        account: str = "",
        start: Path | None = None,
    ) -> "ApprovalContext":
        """Derive the context from the live config and policy.

        ``account`` overrides the config's only because the runner is handed an
        explicit account string that it also gives the broker -- binding the one
        the order will actually carry is the entire point of the field.
        """
        return cls(
            account=(account or getattr(config, "account_id", "")).strip(),
            port=int(getattr(config, "port", 0)),
            commit_sha=commit_sha_at(start),
            configuration_fingerprint=configuration_fingerprint(config, policy),
        )

    def describe(self) -> str:
        return (
            f"account {self.account} port {self.port} commit {self.commit_sha[:12]} "
            f"config {self.configuration_fingerprint[:12]}"
        )


@dataclass(frozen=True)
class AuthorizedOrderSpec:
    """Everything an approval is an approval *of*, in one hashable object.

    ``structure_digest`` -- the existing one, from
    :func:`engine.options.transmit.structure_digest` -- covers the trade:
    quantity, legs, strikes, limit price, direction. That was already enough to
    stop an approval for a 1-lot transmitting a 50-lot. It was **not** enough to
    stop the same structure going to a different account, on a different port, as
    a different order type, with a different time in force, or after the risk
    gates were re-run and came back with different numbers. Those five are the
    extension, and they are why this type exists rather than the gate matching on
    ``structure_digest`` alone.

    :attr:`digest` is what the reviewer binds and what
    :func:`engine.options.transmit.place_combo` re-derives at the door. Nothing
    advisory is in it, so re-deriving it from the same inputs is stable.
    """

    intent_id: UUID
    structure_digest: str
    account: str
    port: int
    order_type: str
    time_in_force: str
    risk_digest: str
    governor_digest: str
    commit_sha: str
    configuration_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.intent_id, UUID):
            raise RefusedError("an order spec must name a trade intent id")
        if len(self.structure_digest) != 64:
            raise RefusedError("an order spec must carry a structure digest")
        if not self.order_type or not self.time_in_force:
            raise RefusedError("an order spec must state its order type and TIF")

    def to_record(self) -> dict[str, Any]:
        return {
            "intent_id": str(self.intent_id),
            "structure_digest": self.structure_digest,
            "account": self.account,
            "port": self.port,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "risk_digest": self.risk_digest,
            "governor_digest": self.governor_digest,
            "commit_sha": self.commit_sha,
            "configuration_fingerprint": self.configuration_fingerprint,
        }

    @property
    def digest(self) -> str:
        """The authorization-spec digest the protocol binds an approval to."""
        return _sha(self.to_record())

    def describe(self) -> str:
        return (
            f"intent {self.intent_id} spec {self.digest[:12]} "
            f"{self.order_type}/{self.time_in_force} -> {self.account}:{self.port}"
        )


def spec_for_open(
    *,
    intent_id: UUID,
    structure_digest: str,
    risk: CandidateRiskAssessment,
    governor: GovernorVerdict,
    context: ApprovalContext,
    order_type: str,
    time_in_force: str,
) -> AuthorizedOrderSpec:
    """Assemble the spec for one opening order. Pure."""
    return AuthorizedOrderSpec(
        intent_id=intent_id,
        structure_digest=structure_digest,
        account=context.account,
        port=context.port,
        order_type=order_type,
        time_in_force=time_in_force,
        risk_digest=risk_digest(risk),
        governor_digest=governor_digest(governor),
        commit_sha=context.commit_sha,
        configuration_fingerprint=context.configuration_fingerprint,
    )


@dataclass(frozen=True)
class VerificationPacket:
    """The immutable proposal the reviewer is asked about.

    Everything section 3.1 of the protocol enumerates, in one object that hashes
    to :attr:`AuthorizedOrderSpec.digest`. The evidence a reviewer needs but the
    gate cannot itself check -- market-data provenance, IV Rank, stress loss,
    broker what-if margin, portfolio exposure, pending reservations -- rides in
    :attr:`evidence` and is rendered into the body verbatim.

    The gate does **not** refuse an incomplete evidence section. It cannot: the
    protocol makes packet completeness the reviewer's call, and a gate that
    silently substituted its own judgement for the reviewer's would be the
    reviewer. What it does instead is render every expected field, marking the
    absent ones ``MISSING`` in the body, so an incomplete packet is legible as
    incomplete and comes back UNAVAILABLE -- which blocks.
    """

    spec: AuthorizedOrderSpec
    context: ApprovalContext
    intent_record: Mapping[str, Any]
    risk_record: Mapping[str, Any]
    governor_record: Mapping[str, Any]
    expires_at: dt.datetime
    proposed_at: dt.datetime
    evidence: Mapping[str, Any] = field(default_factory=dict)

    #: The evidence fields section 3.1 asks for beyond what the spec already
    #: binds. Listed so the body can say "MISSING" rather than say nothing.
    EXPECTED_EVIDENCE = (
        "market_data_provenance",
        "quote_timestamps",
        "greek_timestamps",
        "iv_rank",
        "iv_rank_filter",
        # The regime protocol (spec §1): every packet states the tier, the
        # sizing multiplier and the exact reasons. Absent renders MISSING,
        # which the reviewer treats as grounds for UNAVAILABLE.
        "volatility_regime",
        "allocation_multiplier",
        "regime_reasons",
        "defined_max_loss",
        "stress_loss",
        "broker_what_if_margin",
        "portfolio_exposure_before",
        "portfolio_exposure_after",
        "pending_reservations",
        "sector_impact",
        "correlation_impact",
    )

    def title(self) -> str:
        return (
            f"VERIFY OPEN: {self.intent_record.get('underlying', '?')} "
            f"{self.intent_record.get('type', '?')} x"
            f"{self.intent_record.get('quantity', '?')} @ "
            f"{self.intent_record.get('limit_price', '?')} "
            f"[{self.spec.digest[:12]}]"
        )

    def render(self) -> str:
        """The packet as markdown. This is what the reviewer reads."""
        lines = [
            "# OPENING TRADE VERIFICATION REQUEST",
            "",
            "Immutable packet. Any edit after this point requires a new packet and a",
            "new review -- the spec digest below is what the answer must bind.",
            "",
            "## 1. Identity",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| Trade intent id | `{self.spec.intent_id}` |",
            f"| Authorization-spec digest | `{self.spec.digest}` |",
            f"| Structure digest | `{self.spec.structure_digest}` |",
            f"| Git commit SHA | `{self.spec.commit_sha}` |",
            f"| Configuration fingerprint | `{self.spec.configuration_fingerprint}` |",
            f"| Paper account | `{self.spec.account}` |",
            f"| Paper port | `{self.spec.port}` |",
            f"| Order type / TIF | `{self.spec.order_type}` / `{self.spec.time_in_force}` |",
            f"| Proposed at (UTC) | `{self.proposed_at.isoformat()}` |",
            f"| Approval must expire by (UTC) | `{self.expires_at.isoformat()}` |",
            "",
            "## 2. The order",
            "",
            "```json",
            json.dumps(dict(self.intent_record), indent=2, sort_keys=True, default=str),
            "```",
            "",
            "## 3. Candidate risk",
            "",
            "```json",
            json.dumps(dict(self.risk_record), indent=2, sort_keys=True, default=str),
            "```",
            "",
            "## 4. Portfolio governor",
            "",
            "```json",
            json.dumps(dict(self.governor_record), indent=2, sort_keys=True, default=str),
            "```",
            "",
            "## 5. Evidence",
            "",
            "| Field | Value |",
            "|---|---|",
        ]
        for name in self.EXPECTED_EVIDENCE:
            value = self.evidence.get(name)
            shown = "**MISSING**" if value is None else f"`{value}`"
            lines.append(f"| {name} | {shown} |")
        extra = sorted(set(self.evidence) - set(self.EXPECTED_EVIDENCE))
        for name in extra:
            lines.append(f"| {name} | `{self.evidence[name]}` |")
        lines += [
            "",
            "## 6. Required answer",
            "",
            "Reply through collab-kit (`handoff ibkr reply <this id> --as grok`) with",
            "exactly one of APPROVED / REFUSED / UNAVAILABLE, and a frontmatter header",
            "carrying `verification_decision`, `verification_request_id` (this handoff",
            "id), `verification_intent_id`, `verification_spec_digest`,",
            "`verification_verifier: grok`, `verification_approved_at` and",
            "`verification_expires_at`. An answer that binds a different digest does not",
            "authorize this order.",
            "",
        ]
        return "\n".join(lines)


def _intent_record(intent: OptionStrategyIntent) -> dict[str, Any]:
    """The order, in full, for the reviewer to recompute against.

    Written here rather than on the domain type because it exists for one
    audience -- the reviewer -- and section 3.1 dictates its contents: the exact
    conIds, strikes, expiration, rights, actions and ratios, not a summary. The
    domain's ``describe`` is for a human reading a terminal and elides all of it.
    """
    return {
        "strategy_id": str(intent.strategy_id),
        "underlying": intent.underlying.strip().upper(),
        "type": intent.strategy_type.value,
        "action": intent.strategy_action.value,
        "quantity": intent.quantity,
        "expiration": intent.expiration.isoformat(),
        "limit_price": str(intent.limit_price),
        "price_effect": intent.price_effect.value,
        "maximum_loss_per_contract": str(intent.maximum_loss_per_contract),
        "total_maximum_loss": str(
            intent.maximum_loss_per_contract * intent.quantity
        ),
        "configuration_version": intent.configuration_version,
        "closes_strategy_id": (
            str(intent.closes_strategy_id) if intent.closes_strategy_id else None
        ),
        "legs": [
            {
                "con_id": leg.con_id,
                "symbol": leg.symbol,
                "expiration": leg.expiration.isoformat(),
                "strike": str(leg.strike),
                "right": leg.right.value,
                "action": leg.action.value,
                "ratio": leg.ratio,
                "multiplier": leg.multiplier,
                "exchange": leg.exchange,
            }
            for leg in intent.legs
        ],
    }


def packet_for(
    intent: OptionStrategyIntent,
    *,
    structure_digest: str,
    risk: CandidateRiskAssessment,
    governor: GovernorVerdict,
    context: ApprovalContext,
    order_type: str,
    time_in_force: str,
    now: dt.datetime,
    lifetime: dt.timedelta = MAXIMUM_APPROVAL_LIFETIME,
    evidence: Mapping[str, Any] | None = None,
) -> VerificationPacket:
    """Build the packet for one opening order, spec included."""
    if lifetime > MAXIMUM_APPROVAL_LIFETIME:
        raise RefusedError(
            f"an approval lifetime of {lifetime} exceeds the {MAXIMUM_APPROVAL_LIFETIME} maximum"
        )
    spec = spec_for_open(
        intent_id=intent.strategy_id,
        structure_digest=structure_digest,
        risk=risk,
        governor=governor,
        context=context,
        order_type=order_type,
        time_in_force=time_in_force,
    )
    return VerificationPacket(
        spec=spec,
        context=context,
        intent_record=_intent_record(intent),
        risk_record=risk.to_record(),
        governor_record=governor.to_record(),
        expires_at=now + lifetime,
        proposed_at=now,
        evidence=dict(evidence or {}),
    )


# ---------------------------------------------------------------------------
# the response
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifierApproval:
    """One parsed reviewer response. Not constructible outside this module.

    ``key`` is compared by identity against a module-private sentinel, so the
    only way to hold one of these is to have parsed a handoff that collab-kit
    put on disk. That is a statement about *this process's code paths*, not a
    security boundary -- see the module docstring, which says so.
    """

    decision: ApprovalDecision
    request_id: str
    response_id: str
    intent_id: UUID
    spec_digest: str
    verifier: str
    approved_at: dt.datetime
    expires_at: dt.datetime
    sender_seat: str
    thread: str
    source: Path
    key: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.key is not _APPROVAL_KEY:
            raise RefusedError(
                "a VerifierApproval cannot be constructed directly",
                hint="it is minted only by reading a reviewer handoff off disk",
            )

    def describe(self) -> str:
        return (
            f"{self.decision.value} {self.intent_id} spec {self.spec_digest[:12]} "
            f"by {self.verifier} answering {self.request_id} "
            f"(expires {self.expires_at.isoformat()})"
        )


def _collab(module: str, attribute: str) -> Any:
    loaded = _collabkit.load(module, attribute)
    if loaded is None:
        raise RefusedError(
            f"collab-kit's {module}.{attribute} is not importable, so no verifier "
            "review can be requested or read",
            hint=(
                "openings are blocked rather than assumed approved. "
                f"{_collabkit.last_error() or 'set KIT_DIR to the collab-kit checkout'}"
            ),
        )
    return loaded


def _required(meta: Mapping[str, Any], name: str, where: str) -> str:
    value = meta.get(VERIFICATION_PREFIX + name)
    if value is None or str(value).strip() == "":
        raise ApprovalDefect(
            f"{where}: verification response is missing {VERIFICATION_PREFIX}{name}",
            hint="an incomplete answer is UNAVAILABLE, which blocks new opening risk",
        )
    return str(value).strip()


def _instant(raw: str, label: str, where: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ApprovalDefect(f"{where}: {label} {raw!r} is not an ISO instant") from None
    if parsed.tzinfo is None:
        raise ApprovalDefect(
            f"{where}: {label} {raw!r} has no timezone",
            hint="a naive timestamp inside a TTL is a bug waiting for a DST boundary",
        )
    return parsed.astimezone(dt.timezone.utc)


VERIFICATION_FENCE = "verification"


def verification_block(body: str) -> dict[str, Any] | None:
    """The fenced ``verification`` block in a handoff body, parsed. ``None`` if absent.

    Parsed by wrapping the block in ``---`` and handing it to collab-kit's own
    frontmatter reader, so the accepted syntax is exactly collab-kit's flat
    scalar subset rather than a second parser written here that would drift.

    More than one block is a defect rather than a choice: a reply carrying two
    decisions is a reply whose meaning depends on which one the reader picks
    first, and picking is not something a gate should do.
    """
    fences: list[str] = []
    collecting: list[str] | None = None
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if collecting is None:
            if stripped.startswith("```") and stripped[3:].strip() == VERIFICATION_FENCE:
                collecting = []
            continue
        if stripped.startswith("```"):
            fences.append("\n".join(collecting))
            collecting = None
            continue
        collecting.append(line)
    if collecting is not None:
        raise ApprovalDefect(
            "a verification block was opened and never closed",
            hint="an unterminated block is a truncated answer, which blocks",
        )
    if not fences:
        return None
    if len(fences) > 1:
        raise ApprovalDefect(
            f"a verification response carries {len(fences)} verification blocks",
            hint="exactly one decision per answer; two is an answer with no meaning",
        )
    parse = _collab("frontmatter", "parse")
    meta, _rest = parse("---\n" + fences[0].strip("\n") + "\n---\n")
    return {f"{VERIFICATION_PREFIX}{key}": value for key, value in dict(meta).items()}


def _approval_from_handoff(handoff: Any) -> VerifierApproval | None:
    """Read one collab-kit handoff. ``None`` when it is not a verification answer."""
    meta = verification_block(str(getattr(handoff, "body", "") or ""))
    if meta is None:
        return None

    where = str(getattr(handoff, "id", "") or getattr(handoff, "path", "?"))
    raw_decision = _required(meta, "decision", where).upper()
    try:
        decision = ApprovalDecision(raw_decision)
    except ValueError:
        raise ApprovalDefect(
            f"{where}: decision {raw_decision!r} is not one of "
            f"{', '.join(d.value for d in ApprovalDecision)}",
            hint="the protocol allows exactly three outcomes; anything else is an "
            "artifact nobody can act on, so it blocks",
        ) from None

    raw_intent = _required(meta, "intent_id", where)
    try:
        intent_id = UUID(raw_intent)
    except ValueError:
        raise ApprovalDefect(f"{where}: intent id {raw_intent!r} is not a UUID") from None

    request_id = _required(meta, "request_id", where)
    verifier = str(meta.get(VERIFICATION_PREFIX + "verifier", "") or "").strip()

    if decision is not ApprovalDecision.APPROVED:
        # A refusal blocks on its decision, its request id and its intent id
        # alone. Demanding a matching digest from a REFUSED would let the "no"
        # be evaded by changing the order it was about -- which is backwards,
        # since a changed order is exactly what the refusal warns against.
        epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        return VerifierApproval(
            decision=decision,
            request_id=request_id,
            response_id=str(getattr(handoff, "id", "")),
            intent_id=intent_id,
            spec_digest="",
            verifier=verifier or "unnamed",
            approved_at=epoch,
            expires_at=epoch,
            sender_seat=str(getattr(handoff, "sender", "")),
            thread=str(getattr(handoff, "thread", "") or ""),
            source=Path(getattr(handoff, "path", "") or where),
            key=_APPROVAL_KEY,
        )

    spec_digest = _required(meta, "spec_digest", where).lower()
    if len(spec_digest) != 64:
        raise ApprovalDefect(
            f"{where}: spec digest {spec_digest!r} is not a sha256 hex digest"
        )
    approved_at = _instant(_required(meta, "approved_at", where), "approved_at", where)
    expires_at = _instant(_required(meta, "expires_at", where), "expires_at", where)
    if expires_at <= approved_at:
        raise ApprovalDefect(
            f"{where}: expires_at {expires_at.isoformat()} is not after approved_at "
            f"{approved_at.isoformat()}"
        )
    if expires_at - approved_at > MAXIMUM_APPROVAL_LIFETIME:
        raise ApprovalDefect(
            f"{where}: approval is valid for {expires_at - approved_at}, over the "
            f"{MAXIMUM_APPROVAL_LIFETIME} maximum",
            hint="a long-lived approval is a standing licence, which is the thing "
            "this gate exists to abolish",
        )

    return VerifierApproval(
        decision=decision,
        request_id=request_id,
        response_id=str(getattr(handoff, "id", "")),
        intent_id=intent_id,
        spec_digest=spec_digest,
        verifier=verifier,
        approved_at=approved_at,
        expires_at=expires_at,
        sender_seat=str(getattr(handoff, "sender", "")),
        thread=str(getattr(handoff, "thread", "") or ""),
        source=Path(getattr(handoff, "path", "") or where),
        key=_APPROVAL_KEY,
    )


def render_response(
    *,
    decision: ApprovalDecision,
    request_id: str,
    intent_id: UUID,
    spec_digest: str = "",
    verifier: str = REVIEWER_SEAT,
    approved_at: dt.datetime | None = None,
    expires_at: dt.datetime | None = None,
    reasons: str = "",
) -> str:
    """The reply body a reviewer must send, block included.

    Exported because the reviewer seat needs one authoritative statement of the
    answer format rather than ten hand-assembled lines, and because the test
    suite has to produce real answers so it exercises the real reader. It writes
    no file and moves no handoff: the caller passes this to collab-kit's
    ``reply``, which is what puts the answer through the lifecycle.
    """
    fields = [
        f"decision: {decision.value}",
        f"request_id: {request_id}",
        f"intent_id: {intent_id}",
        f"verifier: {verifier}",
    ]
    if decision is ApprovalDecision.APPROVED:
        if approved_at is None or expires_at is None:
            raise RefusedError("an APPROVED answer must carry approved_at and expires_at")
        fields += [
            f"spec_digest: {spec_digest}",
            f"approved_at: {approved_at.astimezone(dt.timezone.utc).isoformat()}",
            f"expires_at: {expires_at.astimezone(dt.timezone.utc).isoformat()}",
        ]
    block = "\n".join(fields)
    return "\n".join(
        [
            f"# VERIFICATION {decision.value}",
            "",
            f"```{VERIFICATION_FENCE}",
            block,
            "```",
            "",
            "## Reasons",
            "",
            reasons or "(none given)",
            "",
        ]
    )


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


class VerifierGate(Protocol):
    """The seam :func:`engine.options.transmit.authorize_open` requires.

    One method, and it raises to refuse -- there is no falsy return a caller can
    forget to check. A protocol rather than a concrete class because the runner,
    the walk and the reprice ladder all need to be handed one, and because the
    test suite must be able to drive the reviewer's half of the exchange.
    """

    def require(
        self, packet: VerificationPacket, *, now: dt.datetime
    ) -> VerifierApproval:  # pragma: no cover - protocol
        """Return the APPROVED answer covering ``packet``, or raise."""
        ...

    def consume(
        self, approval: VerifierApproval, *, now: dt.datetime
    ) -> None:  # pragma: no cover - protocol
        """Burn the approval. A second call for the same answer must raise."""
        ...


#: Environment override for where the collab lives, checked before anything is
#: guessed. Tests set it so a run never writes into the operator's real
#: correspondence, and an operator whose collab is not beside the checkout sets
#: it for the same reason the rest of this engine reads ``IBKR_*``.
ENV_COLLAB_ROOT = "IBKR_COLLAB_ROOT"


def default_collab_root(project: str = "ibkr") -> Path | None:
    """Where the collab for ``project`` lives, or ``None`` if it cannot be found.

    ``None`` rather than a guessed path: a gate pointed at a directory nobody
    reviews from would file requests into the void and then block forever, which
    looks exactly like a reviewer being slow. Refusing to guess makes the
    misconfiguration loud at the first entry instead of silent until someone
    wonders why nothing ever trades.
    """
    override = os.environ.get(ENV_COLLAB_ROOT, "").strip()
    if override:
        return Path(override).expanduser()
    home = os.environ.get("COLLAB_HOME", "").strip()
    if home:
        candidate = Path(home).expanduser() / project
        if candidate.is_dir():
            return candidate
    tools = _collabkit.tools_dir()
    if tools is not None:
        candidate = tools.parent / ".collab" / project
        if candidate.is_dir():
            return candidate
    return None


@dataclass
class CollabVerifierGate:
    """The production gate: proposals and answers as collab-kit handoffs.

    ``root`` is a collab root -- ``.collab/ibkr`` -- and ``ledger`` is a
    directory in the engine's own state where the request id for each spec and
    the consumption marker for each answer live. The ledger is deliberately
    *outside* the collab tree: it is the builder's memory of what it asked and
    what it has already spent, and mixing it into the correspondence would make
    it look like part of the conversation.
    """

    root: Path
    ledger: Path
    reviewer_seat: str = REVIEWER_SEAT

    # -- collab-kit plumbing ---------------------------------------------

    def _store(self) -> Any:
        paths = _collab("paths", "CollabPaths").at(self.root)
        return _collab("store", "HandoffStore")(paths)

    def _canonical(self, raw: str | None) -> str:
        return str(_collab("seats", "canonical")(raw) or "")

    # -- the builder's memory --------------------------------------------

    def _request_marker(self, spec: AuthorizedOrderSpec) -> Path:
        return self.ledger / "requests" / f"{spec.digest}.id"

    def _consumed_marker(self, approval: VerifierApproval) -> Path:
        return self.ledger / "consumed" / f"{approval.response_id}.used"

    def request_id_for(self, spec: AuthorizedOrderSpec) -> str:
        """The handoff already filed for this exact spec, or ``""``.

        Keyed by the spec digest, so a re-run of the same candidate at the same
        price under the same config reuses the outstanding request instead of
        filing a duplicate every pass -- and a candidate that changed *anything*
        gets a new key, which is the invalidation rule expressed as a filename.
        """
        marker = self._request_marker(spec)
        try:
            return marker.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    # -- step 2 and 3: persist, then ask ---------------------------------

    def propose(self, packet: VerificationPacket, *, now: dt.datetime) -> str:
        """Persist the proposal and file the review request. Idempotent.

        The proposal is written to the ledger **before** the handoff exists, so
        a crash between the two leaves a record that something was proposed
        rather than a request nobody remembers making. The handoff id is written
        last, and its presence is what "already asked" means.
        """
        marker = self._request_marker(packet.spec)
        existing = self.request_id_for(packet.spec)
        if existing:
            return existing

        marker.parent.mkdir(parents=True, exist_ok=True)
        (marker.parent / f"{packet.spec.digest}.json").write_text(
            json.dumps(
                {
                    "state": VerificationState.PROPOSED.value,
                    "proposed_at": now.isoformat(),
                    "spec": packet.spec.to_record(),
                    "spec_digest": packet.spec.digest,
                    "expires_at": packet.expires_at.isoformat(),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        handoff = self._store().create(
            to=_REVIEWER_ROUTE,
            sender=_BUILDER_SEAT,
            title=packet.title(),
            body=packet.render(),
            priority="high",
            tags=["verification", "opening"],
        )
        marker.write_text(handoff.id, encoding="utf-8")
        return str(handoff.id)

    # -- step 7 and the validation --------------------------------------

    def require(self, packet: VerificationPacket, *, now: dt.datetime) -> VerifierApproval:
        """Propose if needed, then demand a matching APPROVED answer.

        Never waits. Every failure raises; the one that means "not yet" is
        :class:`AwaitingVerification`, so a caller can keep going.
        """
        spec = packet.spec
        store = self._store()
        request_id = self.propose(packet, now=now)

        find = store.find
        try:
            request = find(request_id)
        except Exception as exc:  # noqa: BLE001 - a lost request is a refusal
            raise RefusedError(
                f"the review request {request_id} for this order is no longer in the "
                f"collab at {self.root}: {type(exc).__name__}: {exc}",
                hint="an answer with nothing to answer is not a review",
            ) from exc

        answers = self._answers_to(store, request_id)
        if not answers:
            raise AwaitingVerification(
                f"awaiting independent verification of trade intent {spec.intent_id}",
                hint=f"request {request_id} is filed and unanswered; this candidate "
                "waits, everything else in the pass continues",
                request_id=request_id,
            )

        blocking = [a for a in answers if a.decision is not ApprovalDecision.APPROVED]
        if blocking:
            worst = blocking[0]
            raise RefusedError(
                f"the verifier answered {worst.decision.value} for trade intent "
                f"{spec.intent_id}",
                hint=f"see {worst.response_id}; a refusal is not overridden by an "
                "approval filed beside it",
            )

        reasons: list[str] = []
        for approval in answers:
            problems = self._mismatches(approval, packet, request=request, now=now)
            if not problems:
                return approval
            reasons.append(f"{approval.response_id}: {'; '.join(problems)}")

        raise RefusedError(
            f"no APPROVED answer covers this order (intent {spec.intent_id})",
            hint=" | ".join(reasons),
        )

    def _answers_to(self, store: Any, request_id: str) -> list[VerifierApproval]:
        found: list[VerifierApproval] = []
        for handoff in store.list(("pending", "claimed", "done", "archive"), to=_BUILDER_SEAT):
            approval = _approval_from_handoff(handoff)
            if approval is not None and approval.request_id == request_id:
                found.append(approval)
        return found

    def _mismatches(
        self,
        approval: VerifierApproval,
        packet: VerificationPacket,
        *,
        request: Any,
        now: dt.datetime,
    ) -> list[str]:
        """Every way this answer fails to cover this order, all at once.

        All reasons rather than the first, because "the digest moved" and "and
        the commit moved too" are different problems and reporting one hides the
        other -- which is how a rebuild gets blamed on a re-quote.
        """
        spec = packet.spec
        problems: list[str] = []

        # -- identity of the answer -------------------------------------
        if approval.verifier.strip().lower() != self.reviewer_seat.lower():
            problems.append(
                f"signed by {approval.verifier!r}, not the {self.reviewer_seat!r} "
                "reviewer seat"
            )
        if self._canonical(approval.sender_seat) != _REVIEWER_ROUTE:
            problems.append(
                f"sent from seat {approval.sender_seat!r}, which is not the reviewer"
            )

        # -- it came through the lifecycle, not over the wall ------------
        # A file dropped straight into pending/ has no request behind it that
        # was claimed and completed naming it. These four facts are written by
        # collab-kit's own transitions, in the reviewer's session, one after the
        # other -- so all four holding is what "arrived through the lifecycle"
        # is enforced as.
        expected_thread = getattr(request, "thread", None) or getattr(request, "id", "")
        if approval.thread != str(expected_thread):
            problems.append(
                f"threaded to {approval.thread!r}, not to the request {expected_thread!r}"
            )
        if str(getattr(request, "status", "")) not in ("done", "archive"):
            problems.append(
                f"the request is still {getattr(request, 'status', '?')}; the reviewer "
                "did not close it, so this answer did not come through the lifecycle"
            )
        if self._canonical(getattr(request, "claimed_by", "")) != _REVIEWER_ROUTE:
            problems.append(
                f"the request was never claimed by the reviewer "
                f"(claimed_by={getattr(request, 'claimed_by', None)!r})"
            )
        if approval.response_id not in str(getattr(request, "note", "") or ""):
            problems.append(
                f"the request does not record {approval.response_id} as its answer"
            )

        # -- it is about this order -------------------------------------
        if approval.intent_id != spec.intent_id:
            problems.append(
                f"answers intent {approval.intent_id}, order is {spec.intent_id}"
            )
        if approval.spec_digest != spec.digest:
            problems.append(
                f"approved spec {approval.spec_digest[:12]}, order is {spec.digest[:12]}"
            )

        # -- it is still an approval ------------------------------------
        if now >= approval.expires_at:
            problems.append(f"expired at {approval.expires_at.isoformat()}")
        if approval.approved_at > now + CLOCK_SKEW_TOLERANCE:
            problems.append(f"dated in the future, at {approval.approved_at.isoformat()}")
        if self._consumed_marker(approval).exists():
            problems.append("already consumed; an approval authorizes exactly one order")
        return problems

    # -- single use -------------------------------------------------------

    def consume(self, approval: VerifierApproval, *, now: dt.datetime) -> None:
        """Burn the approval, exclusively. A second call raises.

        ``O_CREAT | O_EXCL`` rather than an exists-check-then-write: the check
        and the write have to be the same operation, or two passes racing the
        same approval both see it unspent. This is also why the marker is a file
        rather than a line in a log -- an append cannot fail on a duplicate.

        Consumption happens **after** the arm gate, so a dry run validates
        everything and spends nothing. Burning an approval on an unarmed pass
        would mean a dry run disarms the real one that follows it, which is a
        worse failure than the one single-use exists to prevent.
        """
        marker = self._consumed_marker(approval)
        marker.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "state": VerificationState.CONSUMED.value,
                "response_id": approval.response_id,
                "request_id": approval.request_id,
                "intent_id": str(approval.intent_id),
                "spec_digest": approval.spec_digest,
                "verifier": approval.verifier,
                "consumed_at": now.isoformat(),
            },
            sort_keys=True,
        )
        try:
            handle = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise RefusedError(
                    f"approval {approval.response_id} has already been consumed",
                    hint="an approval authorizes exactly one opening order; a second "
                    "order needs a second review",
                ) from None
            raise  # pragma: no cover - a genuinely broken ledger directory
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
