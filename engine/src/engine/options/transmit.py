"""The only place an option order leaves this process.

Until this module existed, ``engine.options`` provably could not transmit: there
was no ``placeOrder`` anywhere in the package, and a test walked the AST to keep
it that way. That property is now gone, deliberately, and it has been replaced by
a narrower one that is worth more:

    **Exactly one function transmits, and it cannot be called without a token
    that only exists if every gate passed.**

``place_combo`` takes a :class:`TransmitAuthorization` as a required argument. The
token has no public constructor -- ``__post_init__`` refuses any instance not
built with a module-private key -- and :func:`authorize_open` is the only code
that holds that key. So "forgot to check the gates" is not a mistake a caller can
make; it is a ``TypeError`` at the call site, because there is no token to pass.

This is the same move the domain makes with ``OptionStrategyIntent``: if you are
holding one, the invariants have already been checked. Here, if you are holding a
``TransmitAuthorization``, the kill switch was clear, ``--arm`` was given, the
symbol was on the allowlist, the daily cap had room, and both the candidate risk
assessment and the portfolio governor approved *this* strategy id.

**And, for an opening order, an independent reviewer said yes.**
:func:`authorize_open` takes a :class:`~engine.options.approval.VerifierGate`
and a :class:`~engine.options.approval.VerificationPacket` as required
arguments: it proposes the packet to a separate persistent reviewer session
through collab-kit's handoff lifecycle and refuses unless an APPROVED answer
comes back bound to this trade intent id and this
:class:`~engine.options.approval.AuthorizedOrderSpec` digest. Approvals are
single-use. REFUSED, UNAVAILABLE, missing, expired, mismatched and
already-spent all block. That gate is *not* cryptographic tamper-proofing --
see :mod:`engine.options.approval`, which states exactly what it does and does
not assert -- it is autonomous independent review with exact digest binding and
fail-closed authorization.

**Closes are authorized differently, and that asymmetry is the point.**
:func:`authorize_close` deliberately does **not** consult the governor, and is
exempt from the daily order cap:

* Refusing to close because the book is too concentrated is backwards. Closing is
  what *reduces* concentration; a governor veto on an exit would trap the account
  in exactly the position it objected to.
* A daily order cap that can stop you exiting is not a safety feature. The cap
  exists to bound how much new risk a runaway loop can take on, so it counts
  opens. Closes are always permitted.

The kill switch still blocks both. That is the one case where the operator has
said "stop", and the engine obeying literally is the whole value of the file.

**Cancelling is the same asymmetry, taken further.** :func:`authorize_cancel`
consults neither the governor, nor the daily cap, nor any risk assessment, and
does not even need an intent: a cancel can only ever *reduce* exposure, and the
worst thing a spurious one can do is leave the engine flat. Requiring a full
opening authorization to pull a working order would mean the engine could get
into a position it was not allowed to get out of -- which is how the live run
that motivated this module ended, with a combo working unfilled and no
programmatic way to retract it.

What a cancel still needs, and why:

* the kill switch clear -- ``HALT`` means stop, and an engine that keeps
  touching the broker after a halt is not halted;
* ``--arm`` -- a dry run must not reach out and cancel a real order;
* a strategy id -- so the cancellation lands in the same position's history as
  the send it retracts, rather than as an orphan event.

**A pass can lose the mandate it started under, and the door checks.**
:func:`place_combo` takes an optional ``session_lease`` -- a
:data:`SessionLease`, a callable that answers "do you still hold the session you
began this pass under?" with a refusal string or ``None``. It is consulted as
late as anything can be, immediately before the order is armed and handed to
``placeOrder``, so the window between "authorized" and "transmitted" is the one
window it closes. A supervisor's drain timeout only *bounds* how long a pass
that has lost its session may keep running; it does not refuse the send, and
those are different properties.

It is a callable rather than an import for the reason
``tests/test_architecture_boundaries.py`` enforces: the thing that knows about
sessions is the operational tier, and the options domain must not be able to
name it. The same shape as ``EntryPreflight`` in
:mod:`engine.options.runner`, injected the same way, and defaulting to ``None``
so a caller that has no session concept is unaffected.

**The lease fences opens and nothing else, structurally.** The check in
``place_combo`` is guarded on ``StrategyAction.OPEN``, so a closing or rolling
order transmits even when the lease is lost -- and even when a caller passes a
lease that refuses. This is the same asymmetry ``authorize_close`` and
``authorize_cancel`` already argue for one paragraph up: a stale session is a
reason to stop *taking on* risk and never a reason to stop *shedding* it. A
fence that could trap an exit would turn "the scheduler stopped" into "the
position cannot be closed", which is strictly worse than the thing being
prevented. Enforcing it here rather than by asking every caller to remember not
to pass a lease on the exit path is what makes it a property of the code.

**A replace is a new send, and is authorized as one.** :func:`authorize_reprice`
mints an ordinary :class:`TransmitAuthorization`, so the repriced order goes out
through :func:`place_combo` past the same digest check as any other -- and, when
the order being repriced is an OPEN, past a fresh independent review, because
the protocol's invalidation rule names price. It builds
the repriced intent *itself* from the structure that was originally approved,
so the only thing a caller can vary is a single price -- and that price must
land inside the :class:`~engine.options.proof.PriceEnvelope` the risk gates were
run against. Everything else about the order is carried over rather than
supplied, which is what makes "a replace cannot become a different order" a
property of the code rather than a promise in a comment.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable
from uuid import UUID

from ..errors import RefusedError
from ..safety import SafetyGate
from .approval import (
    ApprovalContext,
    AuthorizedOrderSpec,
    VerificationPacket,
    VerifierApproval,
    VerifierGate,
    packet_for,
    spec_for_open,
)
from .domain import (
    OptionStrategyIntent,
    PriceEffect,
    StrategyAction,
    compute_maximum_loss_per_contract,
)
from .execution import COMBO_ORDER_TYPE, COMBO_TIME_IN_FORCE, build_combo
from .governor import GovernorVerdict
from .orderstate import BrokerOrderSnapshot, OrderLifecycleState, snapshot_from_trade
from .order_outbox import (
    ExecutionOutbox,
    TransmissionBudget,
    TransmissionReservation,
)
from .proof import PriceEnvelope
from .risk import CandidateRiskAssessment

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance only
    from .sink import OrderLifecycleSink

__all__ = [
    "CancelAuthorization",
    "ExecutionOutbox",
    "RepricedOrder",
    "SESSION_LEASE_LOST",
    "SessionLease",
    "TransmissionBudget",
    "TransmitAuthorization",
    "TransmitResult",
    "authorize_cancel",
    "authorize_close",
    "authorize_open",
    "authorize_reprice",
    "cancel_combo",
    "place_combo",
    "repricing_digest",
    "structure_digest",
]

# The only object that makes a TransmitAuthorization constructible. Module
# private, never exported, and held solely by the two authorize_* functions.
_AUTHORIZATION_KEY = object()

#: Does the caller still hold the session mandate it began this pass under?
#:
#: Returns a refusal string naming what was lost, or ``None`` when the mandate
#: is still held. A **port**, deliberately -- the answer lives in the
#: operational tier (a scheduler, a session controller, a supervisor) and the
#: options domain is forbidden by ``tests/test_architecture_boundaries.py`` from
#: importing any of it. So the answer is injected as a callable, exactly as
#: :data:`engine.options.runner.EntryPreflight` is.
#:
#: Like the preflight, it can only ever *refuse*. There is no return value that
#: widens anything, and ``None`` -- no lease supplied -- means no fence.
SessionLease = Callable[[], "str | None"]

#: The refusal code a lost lease produces, at both fences.
#:
#: One string, greppable, distinct from every other refusal in the engine: an
#: operator seeing it knows the pass was stopped because its session went away
#: mid-flight rather than because a risk gate or a reviewer said no.
SESSION_LEASE_LOST = "OPTIONS_SESSION_LEASE_LOST"


def _lease_refusal(session_lease: SessionLease | None) -> str | None:
    """Ask the lease, treating any failure to answer as a lost lease.

    Fail-closed for the same reason ``run_once`` treats a raising preflight as a
    refusal: a lease that cannot say whether the session is still held has, for
    every purpose that matters here, not said yes.
    """
    if session_lease is None:
        return None
    try:
        return session_lease() or None
    except Exception as exc:  # noqa: BLE001 - a lease that cannot answer is lost
        return f"the session lease check raised {type(exc).__name__}: {exc}"


def _digest_payload(intent: OptionStrategyIntent) -> dict[str, Any]:
    legs = sorted(
        (
            leg.con_id,
            leg.action.value,
            leg.ratio,
            leg.multiplier,
            str(leg.strike),
            leg.right.value,
        )
        for leg in intent.legs
    )
    return {
        "strategy_id": str(intent.strategy_id),
        "action": intent.strategy_action.value,
        "type": intent.strategy_type.value,
        "underlying": intent.underlying.strip().upper(),
        "quantity": intent.quantity,
        "expiration": intent.expiration.isoformat(),
        "limit_price": str(intent.limit_price),
        "price_effect": intent.price_effect.value,
        "maximum_loss_per_contract": str(intent.maximum_loss_per_contract),
        "legs": legs,
    }


def _sha(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def repricing_digest(intent: OptionStrategyIntent) -> str:
    """Everything about an order that a **reprice** may not change.

    :func:`structure_digest` minus the two fields a reprice legitimately moves:
    the limit price, and the maximum loss that is arithmetically derived from
    it. Those two are omitted rather than compared because on a credit spread
    they are the same fact -- ``max loss = (width - credit) x multiplier``, see
    :func:`engine.options.domain.compute_maximum_loss_per_contract` -- so
    holding one fixed while moving the other would only ever produce an intent
    the domain refuses to construct.

    Everything a reprice must **not** touch is still here: the strategy id, the
    action, the underlying, the quantity, the expiration, the direction of the
    price, and every leg's contract, side, ratio and strike. So two intents with
    the same repricing digest are the same trade at two prices, and nothing else
    can hide behind the word "reprice".

    This is not a substitute for the digest check in :func:`place_combo`. The
    authorization :func:`authorize_reprice` mints still carries a full
    :func:`structure_digest` of the exact repriced order, and ``place_combo``
    still compares it. This one bounds what may be *asked for*; that one binds
    what is actually *sent*.
    """
    payload = _digest_payload(intent)
    payload.pop("limit_price")
    payload.pop("maximum_loss_per_contract")
    return _sha(payload)


def structure_digest(intent: OptionStrategyIntent) -> str:
    """A fingerprint of everything about an order that determines its risk.

    The strategy id names *which* candidate was approved; this names *what was
    approved about it*. Those are different questions, and binding only the
    first is what let an approval for a 1-lot authorize a 50-lot: two intents
    sharing an id are indistinguishable to an id check, however carefully it is
    written.

    Every field that moves the maximum loss is in here -- quantity, each leg's
    contract, side and ratio, the limit price and its direction. Deliberately
    *not* included: ``created_at`` and anything advisory, so that re-deriving
    the digest from the same structure is stable.
    """
    return _sha(_digest_payload(intent))


@dataclass(frozen=True)
class TransmitAuthorization:
    """Proof that every gate passed for one specific strategy.

    Not constructible outside this module. ``key`` is compared by identity
    against a private sentinel, so neither a caller nor a test can mint one by
    passing a plausible-looking value -- and a test that *needs* one has to go
    through the real gates, which is the point.

    ``digest`` pins the structure itself. Without it the token proves only that
    *something* under this strategy id was approved, and ``place_combo``'s id
    check cannot tell a 1-lot from a 50-lot carrying the same id.
    """

    strategy_id: UUID
    action: StrategyAction
    authorized_at: dt.datetime
    armed: bool
    digest: str = ""
    risk: CandidateRiskAssessment | None = None
    governor: GovernorVerdict | None = None
    #: The full spec the independent reviewer approved, and the approval itself.
    #: Required on an OPEN and absent on a close, which is the asymmetry the
    #: module docstring argues for expressed as a field rather than a comment.
    spec: AuthorizedOrderSpec | None = None
    approval: VerifierApproval | None = None
    #: Recovery state for the physical send.  Kept on the token so the only
    #: transmitting door can commit the reservation and receipt immediately
    #: around ``placeOrder`` without a second, caller-controlled chokepoint.
    execution_outbox: ExecutionOutbox | None = field(default=None, repr=False, compare=False)
    execution_attempt_id: str | None = field(default=None, repr=False, compare=False)
    transmission_budget: TransmissionBudget | None = field(default=None, repr=False, compare=False)
    budget_reservation_id: str | None = field(default=None, repr=False, compare=False)
    key: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.key is not _AUTHORIZATION_KEY:
            raise RefusedError(
                "a TransmitAuthorization cannot be constructed directly",
                hint="use authorize_open() or authorize_close(); they are the only "
                "code that can mint one, and they run every gate first",
            )
        if not isinstance(self.strategy_id, UUID):
            raise RefusedError("authorization must name a strategy id")
        if self.armed is not True:
            raise RefusedError("an authorization may only exist for an armed run")
        if not isinstance(self.digest, str) or len(self.digest) != 64:
            raise RefusedError(
                "an authorization must carry a structure digest",
                hint="mint it with authorize_open()/authorize_close(), which "
                "compute the digest from the intent they approved",
            )
        if self.authorized_at.tzinfo is None:
            raise RefusedError("authorized_at must be timezone-aware")

        if self.action is StrategyAction.OPEN:
            # Re-checked here rather than trusted from authorize_open, so the
            # invariant travels with the object. An approved-looking assessment
            # for a *different* strategy is the failure this catches.
            if self.risk is None or self.governor is None:
                raise RefusedError(
                    "an opening authorization requires both a risk assessment and "
                    "a governor verdict"
                )
            if not self.risk.approved:
                raise RefusedError(
                    f"risk assessment did not approve: {list(self.risk.reason_codes)}"
                )
            if not self.governor.approved:
                raise RefusedError(
                    f"governor did not approve: {list(self.governor.reason_codes)}"
                )
            if self.risk.strategy_id != self.strategy_id:
                raise RefusedError(
                    f"risk assessment is for {self.risk.strategy_id}, not "
                    f"{self.strategy_id}",
                    hint="an approval for one candidate must not authorize another",
                )
            # The verifier gate, re-checked here for the same reason as the two
            # above: so the invariant travels with the object rather than being
            # trusted from the function that built it. An opening token without
            # an independent approval bound to its own spec cannot exist.
            if self.spec is None or self.approval is None:
                raise RefusedError(
                    "an opening authorization requires an independent verifier "
                    "approval and the spec it approved",
                    hint="see engine.options.approval; opening risk is gated on a "
                    "reviewer answer, closes and cancels are not",
                )
            if self.approval.spec_digest != self.spec.digest:
                raise RefusedError(
                    f"the approval binds spec {self.approval.spec_digest[:12]}, but "
                    f"this authorization carries {self.spec.digest[:12]}"
                )
            if self.spec.intent_id != self.strategy_id:
                raise RefusedError(
                    f"the approved spec is for intent {self.spec.intent_id}, not "
                    f"{self.strategy_id}"
                )
            if self.spec.structure_digest != self.digest:
                raise RefusedError(
                    "the approved spec describes a different structure than this "
                    "authorization",
                    hint="quantity, legs, strikes or price moved between the review "
                    "and the token",
                )

    def describe(self) -> str:
        return (
            f"authorized {self.action.value} {self.strategy_id} at "
            f"{self.authorized_at.isoformat()}"
        )


@dataclass(frozen=True)
class TransmitResult:
    """What the broker did with an order that really was sent.

    Carries a :class:`~engine.options.orderstate.BrokerOrderSnapshot` rather than
    a status string and a boolean. An earlier version exposed ``is_filled`` as
    ``filled > 0 and status in {...}``, which read a **partial fill** as not
    filled -- and the runner then recorded a live position as ``OPEN_FAILED``.
    Nine outcomes do not compress into two, so they are no longer asked to.
    """

    strategy_id: UUID
    action: StrategyAction
    transmitted: bool
    snapshot: BrokerOrderSnapshot | None = None
    message: str | None = None
    #: The broker's own handle on the order, when there is one.
    #:
    #: Carried because a working order cannot be cancelled without it -- IBKR's
    #: ``cancelOrder`` takes the ``Order`` object, and after ``place_combo``
    #: returns, this is the only reference to it that exists. Excluded from
    #: equality, ``repr`` and :meth:`to_record`: it is a live broker object, not
    #: part of what happened, and it must never reach the journal.
    trade: Any = field(default=None, repr=False, compare=False)

    @property
    def state(self) -> OrderLifecycleState:
        if self.snapshot is None:
            return OrderLifecycleState.UNKNOWN
        return self.snapshot.state

    @property
    def is_filled(self) -> bool:
        """Completely filled. **Not** the question the store usually wants."""
        return self.state is OrderLifecycleState.FILLED

    @property
    def has_position(self) -> bool:
        """Some quantity reached the market -- including a partial fill.

        This is what the position store keys off. A partial fill and a
        cancel-after-partial both put contracts in the book, and both must be
        recorded as positions rather than as failures.
        """
        return self.snapshot is not None and self.snapshot.has_position

    @property
    def is_uncertain(self) -> bool:
        return self.snapshot is None or self.snapshot.is_uncertain

    @property
    def order_id(self) -> int | None:
        return self.snapshot.order_id if self.snapshot else None

    @property
    def perm_id(self) -> int | None:
        return self.snapshot.perm_id if self.snapshot else None

    @property
    def filled(self) -> Decimal:
        return self.snapshot.filled if self.snapshot else Decimal("0")

    @property
    def average_price(self) -> Decimal | None:
        return self.snapshot.average_price if self.snapshot else None

    def describe(self) -> str:
        if not self.transmitted:
            return f"NOT TRANSMITTED  {self.message or 'no reason given'}"
        detail = self.snapshot.describe() if self.snapshot else "no broker response"
        return f"{self.action.value} {self.strategy_id}  {detail}"

    def to_record(self) -> dict[str, Any]:
        return {
            "strategy_id": str(self.strategy_id),
            "action": self.action.value,
            "transmitted": self.transmitted,
            "state": self.state.value,
            "broker": self.snapshot.to_record() if self.snapshot else None,
            "message": self.message,
        }


def _check_allowlist(gate: SafetyGate, intent: OptionStrategyIntent) -> None:
    symbol = intent.underlying.strip().upper()
    if symbol not in gate.config.symbol_allowlist:
        raise RefusedError(
            f"{symbol} is not in the symbol allowlist",
            hint=f"allowed: {', '.join(gate.config.symbol_allowlist)}",
        )


def authorize_open(
    intent: OptionStrategyIntent,
    *,
    gate: SafetyGate,
    risk: CandidateRiskAssessment,
    governor: GovernorVerdict,
    armed: bool,
    now: dt.datetime,
    verifier: VerifierGate,
    packet: VerificationPacket,
    execution_outbox: ExecutionOutbox | None = None,
    transmission_budget: TransmissionBudget | None = None,
    execution_attempt_id: str | None = None,
    session_id: str | None = None,
    lease_nonce: str | None = None,
    tick_id: str | None = None,
    account: str = "",
) -> TransmitAuthorization:
    """Run every gate, demand an independent APPROVED answer, and mint the token.

    ``verifier`` and ``packet`` have **no defaults**. A caller who has not
    arranged for an independent review has nothing to pass, and the call does
    not compile into a working program -- the same move the token itself makes
    on ``place_combo``. This is what turns "no opening trade without an
    independent APPROVED artifact" from a sentence in a handoff into a property
    of the code.

    Order matters and mirrors :func:`engine.cli.cmd_trade`: cheapest and most
    absolute first, ``--arm`` checked **last** so an unarmed run still surfaces
    every other refusal rather than stopping at "not armed" and hiding a problem
    that would have bitten on the next run. The verifier sits immediately before
    the arm gate, so an unarmed pass exercises the whole review path -- it
    proposes, it reads the answer, it validates the digest -- and then refuses
    on arming without spending the approval. Consumption is the very last step,
    after arming, because burning an approval on a dry run would disarm the real
    run that follows it.

    Raises :class:`engine.options.approval.AwaitingVerification` -- a
    ``RefusedError`` subclass -- when the request is filed and unanswered. The
    caller is expected to record ``AWAITING_VERIFICATION`` and carry on with the
    rest of the pass; nothing here waits.
    """
    if intent.strategy_action is not StrategyAction.OPEN:
        raise RefusedError(
            f"authorize_open received a {intent.strategy_action.value} intent"
        )
    if intent.price_effect is not PriceEffect.CREDIT:
        raise RefusedError("this strategy only opens for a credit")

    gate.assert_not_halted()
    _check_allowlist(gate, intent)
    gate.gate_daily_count()
    if execution_outbox is not None:
        # Do not file or consume a new approval while an earlier physical send
        # is unresolved.  The runner checks this too, but keeping the invariant
        # at the authorization boundary closes direct production callers.
        execution_outbox.assert_clear()

    if not risk.approved:
        raise RefusedError(
            f"candidate risk refused: {list(risk.reason_codes)}",
            hint="; ".join(r.detail for r in risk.refusals),
        )
    if not governor.approved:
        raise RefusedError(
            f"portfolio governor refused: {list(governor.reason_codes)}",
            hint="; ".join(r.detail for r in governor.refusals),
        )

    # The packet is what the reviewer was shown. Deriving the spec here from the
    # intent, the risk and the governor that are actually in hand -- and
    # refusing if it differs from the packet's -- is what stops a caller
    # submitting one order for review and authorizing another. Without it the
    # packet would be a claim rather than a binding.
    digest = structure_digest(intent)
    expected = spec_for_open(
        intent_id=intent.strategy_id,
        structure_digest=digest,
        risk=risk,
        governor=governor,
        context=packet.context,
        order_type=COMBO_ORDER_TYPE,
        time_in_force=COMBO_TIME_IN_FORCE,
    )
    if expected.digest != packet.spec.digest:
        raise RefusedError(
            "the packet sent for review does not describe the order being authorized",
            hint=f"packet {packet.spec.digest[:12]}, order {expected.digest[:12]}; "
            "quantity, price, legs, account, port, order type, TIF, risk or "
            "governor result changed after the packet was built",
        )

    approval = verifier.require(packet, now=now)

    gate.gate_armed(armed=armed)

    # The physical-send intent must be durable before the single-use approval
    # is consumed.  A crash after consume but before ``placeOrder`` therefore
    # leaves an explicit attempt for recovery rather than a consumed approval
    # with no answer to "did the broker see it?".
    attempt_id: str | None = None
    reservation: TransmissionReservation | None = None
    if execution_outbox is not None:
        attempt_id = execution_outbox.prepare(
            intent,
            structure_digest=digest,
            spec_digest=expected.digest,
            account=account or packet.context.account,
            approval_id=approval.response_id,
            attempt_id=execution_attempt_id,
            session_id=session_id,
            lease_nonce=lease_nonce,
            tick_id=tick_id,
            now=now,
        )
    try:
        if transmission_budget is not None:
            reservation = transmission_budget.reserve(
                intent.strategy_id, attempt_id=attempt_id, now=now
            )
    except Exception:
        if execution_outbox is not None and attempt_id is not None:
            try:
                execution_outbox.abort(
                    attempt_id,
                    reason="transmission budget refused before approval consumption",
                )
            except Exception:
                pass
        raise
    try:
        # This is intentionally after both durable intent and budget reserve.
        verifier.consume(approval, now=now)
        if execution_outbox is not None and attempt_id is not None:
            execution_outbox.approval_consumed(attempt_id)
    except Exception as exc:
        if execution_outbox is not None and attempt_id is not None:
            try:
                execution_outbox.quarantine(
                    attempt_id,
                    reason=f"approval consumption or reservation failed: {type(exc).__name__}: {exc}",
                )
            except Exception:
                # The original failure is still the most useful signal; the
                # outbox's own durability error is fail-closed on the next
                # startup when the PREPARED record is found.
                pass
        raise

    return TransmitAuthorization(
        strategy_id=intent.strategy_id,
        action=StrategyAction.OPEN,
        authorized_at=now,
        armed=armed,
        digest=digest,
        risk=risk,
        governor=governor,
        spec=expected,
        approval=approval,
        execution_outbox=execution_outbox,
        execution_attempt_id=attempt_id,
        transmission_budget=transmission_budget,
        budget_reservation_id=(reservation.reservation_id if reservation else None),
        key=_AUTHORIZATION_KEY,
    )


def authorize_close(
    intent: OptionStrategyIntent,
    *,
    gate: SafetyGate,
    armed: bool,
    now: dt.datetime,
) -> TransmitAuthorization:
    """Authorize an exit. No governor, no daily cap -- see the module docstring.

    The kill switch and ``--arm`` still apply. Everything else that gates an
    *open* is a reason to avoid taking on risk, and none of those are reasons to
    refuse to shed it.
    """
    if intent.strategy_action not in (StrategyAction.CLOSE, StrategyAction.ROLL):
        raise RefusedError(
            f"authorize_close received a {intent.strategy_action.value} intent"
        )
    if intent.closes_strategy_id is None:
        raise RefusedError(
            "a closing order must name the open strategy it retires",
            hint="closing legs come from the persisted position, never from a "
            "fresh chain lookup",
        )

    gate.assert_not_halted()
    gate.gate_armed(armed=armed)

    return TransmitAuthorization(
        strategy_id=intent.strategy_id,
        action=intent.strategy_action,
        authorized_at=now,
        armed=armed,
        digest=structure_digest(intent),
        key=_AUTHORIZATION_KEY,
    )


@dataclass(frozen=True)
class CancelAuthorization:
    """Proof that a cancellation is permitted. Deliberately cheap to obtain.

    Same unforgeable construction as :class:`TransmitAuthorization` -- a private
    key checked by identity -- and a deliberately smaller set of things it
    proves: the kill switch was clear, the run was armed, and the cancellation
    names a strategy. It carries no risk assessment and no governor verdict
    because neither is a reason to refuse to *reduce* exposure.

    A separate type rather than a flag on ``TransmitAuthorization``: the two
    grant different powers, and a token that could be passed to either
    :func:`place_combo` or :func:`cancel_combo` would make "authorized to
    cancel" silently sufficient to send. The type system says otherwise --
    ``place_combo`` rejects a ``CancelAuthorization`` on the ``isinstance``
    check it already performs.
    """

    strategy_id: UUID
    authorized_at: dt.datetime
    armed: bool
    reason: str = ""
    key: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.key is not _AUTHORIZATION_KEY:
            raise RefusedError(
                "a CancelAuthorization cannot be constructed directly",
                hint="use authorize_cancel(); it is the only code that can mint "
                "one, and it checks the kill switch first",
            )
        if not isinstance(self.strategy_id, UUID):
            raise RefusedError("a cancellation must name a strategy id")
        if self.armed is not True:
            raise RefusedError("an authorization may only exist for an armed run")
        if self.authorized_at.tzinfo is None:
            raise RefusedError("authorized_at must be timezone-aware")

    def describe(self) -> str:
        detail = f" ({self.reason})" if self.reason else ""
        return (
            f"authorized cancel {self.strategy_id} at "
            f"{self.authorized_at.isoformat()}{detail}"
        )


def authorize_cancel(
    strategy_id: UUID,
    *,
    gate: SafetyGate,
    armed: bool,
    now: dt.datetime,
    reason: str = "",
) -> CancelAuthorization:
    """Authorize pulling a working order. Two gates, and they are the right two.

    No governor, no daily cap, no risk assessment, no intent -- see the module
    docstring. Cancelling is the operation that makes every other bound
    enforceable after the fact, and a cancel path that could itself be vetoed
    would be a bound with no way out.
    """
    gate.assert_not_halted()
    gate.gate_armed(armed=armed)
    return CancelAuthorization(
        strategy_id=strategy_id,
        authorized_at=now,
        armed=armed,
        reason=reason,
        key=_AUTHORIZATION_KEY,
    )


@dataclass(frozen=True)
class RepricedOrder:
    """A repriced intent and the authorization that covers exactly it.

    Only :func:`authorize_reprice` can produce one, and it builds **both**
    halves. A caller cannot assemble an intent of its own and pair it with an
    authorization, which is the failure mode a plain
    ``(intent, authorization)`` return would have left open.
    """

    intent: OptionStrategyIntent
    authorization: TransmitAuthorization
    previous_price: Decimal
    envelope: PriceEnvelope

    def describe(self) -> str:
        return (
            f"reprice {self.intent.strategy_id} "
            f"{self.previous_price} -> {self.intent.limit_price} "
            f"[{self.envelope.minimum}, {self.envelope.maximum}]"
        )


def authorize_reprice(
    previous: TransmitAuthorization,
    reference: OptionStrategyIntent,
    *,
    limit_price: Decimal,
    envelope: PriceEnvelope,
    tick: Decimal,
    gate: SafetyGate,
    armed: bool,
    now: dt.datetime,
    verifier: VerifierGate | None = None,
    context: ApprovalContext | None = None,
    execution_outbox: ExecutionOutbox | None = None,
    transmission_budget: TransmissionBudget | None = None,
    execution_attempt_id: str | None = None,
    session_id: str | None = None,
    lease_nonce: str | None = None,
    tick_id: str | None = None,
    account: str = "",
) -> RepricedOrder:
    """Re-authorize the same structure at a new price, or refuse.

    **A reprice of an opening order is a new opening order, and is verified as
    one.** The protocol's invalidation rule names price explicitly, so the
    approval that covered the resting order does not cover its replacement. When
    ``reference`` is an OPEN, ``verifier`` and ``context`` are required and a
    fresh review is demanded for the new price -- this function builds the
    packet itself, from the structure it is already refusing to let vary, so the
    thing reviewed and the thing sent cannot come apart.

    They stay optional in the signature only because a **closing** reprice needs
    neither, and forcing an exit path to carry a verifier would be the exact
    asymmetry violation the module docstring forbids. An opening reprice with
    ``verifier=None`` raises; it does not quietly proceed.

    ``previous`` is the authorization that covered the order now working. It is
    required, and not merely as bookkeeping: it is the proof that the gates ran
    at all, and its risk assessment and governor verdict are carried into the
    new token, so a repriced *opening* order is still an order the governor
    approved. Nobody who did not hold the original authorization can reprice.

    Five refusals, each guarding a different way a "reprice" could become
    something else:

    1. ``previous`` does not match ``reference`` -- the structure being repriced
       is not the structure that was approved, so nothing here means anything.
    2. the new price is outside ``envelope`` -- the risk figures were computed
       against a credit inside that band, and a replace that leaves it is an
       order whose arithmetic nobody checked. Bounded on **both** sides, for
       the reason :func:`engine.options.proof.envelope_for` gives.
    3. the new price is not a multiple of ``tick`` -- an off-tick limit is
       rejected or silently rounded by the exchange, and a silently rounded
       limit is an order at a price the engine did not choose.
    4. the price did not actually change -- a "replace" that replaces nothing
       burns one of the four attempts and tells the operator a lie.
    5. the rebuilt intent's repricing digest differs from the reference's --
       belt and braces, since this function builds the intent itself, but it is
       the assertion that would fire if that construction ever grew a way to
       vary something other than the price.

    The kill switch and ``--arm`` are checked too. A repriced order is still an
    order going to the market.
    """
    if not isinstance(previous, TransmitAuthorization):
        raise RefusedError(
            "repricing requires the authorization the working order was sent under",
            hint="mint it with authorize_open() or authorize_close()",
        )
    if previous.strategy_id != reference.strategy_id:
        raise RefusedError(
            f"authorization is for strategy {previous.strategy_id}, but the working "
            f"order is {reference.strategy_id}"
        )
    if previous.digest != structure_digest(reference):
        raise RefusedError(
            "the order being repriced is not the structure that was authorized",
            hint="reprice the intent that was actually sent, not a rebuilt copy",
        )

    price = Decimal(limit_price)
    if not envelope.contains(price):
        raise RefusedError(
            f"a replace at {price} is outside the approved envelope "
            f"[{envelope.minimum}, {envelope.maximum}]",
            hint="the risk gates approved a structure at a credit inside that band; "
            "a price outside it is a different trade and needs the gates re-run",
        )
    if tick <= 0:
        raise RefusedError(f"tick increment must be positive, got {tick}")
    if price % tick != 0:
        raise RefusedError(
            f"a replace at {price} is not a multiple of the {tick} tick increment",
            hint="an off-tick limit is rejected or silently rounded by the "
            "exchange, and a silently rounded limit is not the price we chose",
        )
    if price == reference.limit_price:
        raise RefusedError(
            f"a replace at {price} does not change the working order's price"
        )

    # Built here, from the reference, so the ONLY thing that varies is the
    # price. The maximum loss is re-derived rather than carried over: on a
    # credit spread it is a function of the credit, and a repriced order
    # carrying the old figure would misreport its own risk to the governor,
    # the store and every downstream reader.
    maximum_loss = (
        compute_maximum_loss_per_contract(
            strategy_type=reference.strategy_type,
            legs=reference.legs,
            credit=price,
            multiplier=reference.multiplier,
        )
        if reference.strategy_action is StrategyAction.OPEN
        else reference.maximum_loss_per_contract
    )
    repriced = OptionStrategyIntent(
        strategy_id=reference.strategy_id,
        strategy_type=reference.strategy_type,
        strategy_action=reference.strategy_action,
        underlying=reference.underlying,
        quantity=reference.quantity,
        legs=reference.legs,
        expiration=reference.expiration,
        limit_price=price,
        price_effect=reference.price_effect,
        maximum_loss_per_contract=maximum_loss,
        configuration_version=reference.configuration_version,
        created_at=reference.created_at,
        estimated_buying_power_change=reference.estimated_buying_power_change,
        closes_strategy_id=reference.closes_strategy_id,
    )
    if repricing_digest(repriced) != repricing_digest(reference):
        raise RefusedError(  # pragma: no cover - unreachable while the build above is a copy
            "a replace changed something other than the price",
            hint="only the limit price and the maximum loss derived from it may move",
        )

    gate.assert_not_halted()
    if execution_outbox is not None:
        execution_outbox.assert_clear()

    spec: AuthorizedOrderSpec | None = None
    approval: VerifierApproval | None = None
    repriced_digest = structure_digest(repriced)
    attempt_id: str | None = None
    reservation: TransmissionReservation | None = None
    if repriced.strategy_action is StrategyAction.OPEN:
        if verifier is None or context is None:
            raise RefusedError(
                "repricing an opening order requires an independent verifier gate "
                "and an approval context",
                hint="the protocol's invalidation rule names price: the approval "
                "that covered the resting order does not cover its replacement",
            )
        if previous.risk is None or previous.governor is None:  # pragma: no cover
            raise RefusedError(
                "an opening authorization without risk and governor verdicts cannot "
                "be repriced"
            )
        repriced_packet = packet_for(
            repriced,
            structure_digest=repriced_digest,
            risk=previous.risk,
            governor=previous.governor,
            context=context,
            order_type=COMBO_ORDER_TYPE,
            time_in_force=COMBO_TIME_IN_FORCE,
            now=now,
            evidence={
                "reprice_of": str(reference.strategy_id),
                "previous_price": str(reference.limit_price),
                "envelope_minimum": str(envelope.minimum),
                "envelope_maximum": str(envelope.maximum),
            },
        )
        spec = repriced_packet.spec
        approval = verifier.require(repriced_packet, now=now)

    gate.gate_armed(armed=armed)
    if approval is not None:
        execution_outbox = execution_outbox or previous.execution_outbox
        transmission_budget = transmission_budget or previous.transmission_budget
        if execution_outbox is not None:
            attempt_id = execution_outbox.prepare(
                repriced,
                structure_digest=repriced_digest,
                spec_digest=spec.digest if spec is not None else "",
                account=(account or (context.account if context is not None else "")),
                approval_id=approval.response_id,
                attempt_id=execution_attempt_id,
                session_id=session_id,
                lease_nonce=lease_nonce,
                tick_id=tick_id,
                now=now,
            )
        try:
            if transmission_budget is not None:
                reservation = transmission_budget.reserve(
                    repriced.strategy_id, attempt_id=attempt_id, now=now
                )
        except Exception:
            if execution_outbox is not None and attempt_id is not None:
                try:
                    execution_outbox.abort(
                        attempt_id,
                        reason="transmission budget refused before reprice approval consumption",
                    )
                except Exception:
                    pass
            raise
        try:
            verifier.consume(approval, now=now)
            if execution_outbox is not None and attempt_id is not None:
                execution_outbox.approval_consumed(attempt_id)
        except Exception as exc:
            if execution_outbox is not None and attempt_id is not None:
                try:
                    execution_outbox.quarantine(
                        attempt_id,
                        reason=f"reprice approval consumption or reservation failed: {type(exc).__name__}: {exc}",
                    )
                except Exception:
                    pass
            raise
    else:
        # Closing reprices do not consume the opening budget, but inherit the
        # outbox/budget references only as a convenience for the transmission
        # door; no reservation is minted for an exit.
        execution_outbox = execution_outbox or previous.execution_outbox
        transmission_budget = transmission_budget or previous.transmission_budget

    authorization = TransmitAuthorization(
        strategy_id=repriced.strategy_id,
        action=repriced.strategy_action,
        authorized_at=now,
        armed=armed,
        digest=repriced_digest,
        risk=previous.risk,
        governor=previous.governor,
        spec=spec,
        approval=approval,
        execution_outbox=execution_outbox,
        execution_attempt_id=attempt_id,
        transmission_budget=transmission_budget,
        budget_reservation_id=(reservation.reservation_id if reservation else None),
        key=_AUTHORIZATION_KEY,
    )
    return RepricedOrder(
        intent=repriced,
        authorization=authorization,
        previous_price=reference.limit_price,
        envelope=envelope,
    )


def place_combo(
    ib: Any,
    intent: OptionStrategyIntent,
    *,
    authorization: TransmitAuthorization,
    account: str = "",
    timeout: float = 30.0,
    poll_seconds: float = 0.5,
    observed_at: dt.datetime | None = None,
    sink: OrderLifecycleSink | None = None,
    session_lease: SessionLease | None = None,
) -> TransmitResult:
    """Transmit a combo order. **The only transmitting call in this package.**

    ``authorization`` is not optional and has no default. A caller who has not
    been through :func:`authorize_open` or :func:`authorize_close` has nothing to
    pass here, and the call does not compile into a working program.

    The identity check below is not redundant with the token's own validation:
    the token proves *some* strategy was approved, and this proves it was **this**
    one. Without it, an approval for a 1-lot could transmit a 10-lot.

    ``session_lease`` is the last fence, and it is about *time* rather than
    about the order. Every other check here asks "is this the structure that was
    approved"; this one asks "does the caller still hold the session it was
    approved under". A scheduler-driven pass that loses its session between
    authorization and this line is authorized to send an order on behalf of a
    session that no longer exists, and refusing that is not the same thing as
    the supervisor eventually timing the pass out. Defaults to ``None``, which
    is no fence and byte-for-byte the previous behaviour.

    **It fences opens only.** A CLOSE or a ROLL transmits regardless of what the
    lease says, and the guard below says so structurally rather than relying on
    callers not to pass one -- see the module docstring. A lost session must
    never be the reason a position cannot be exited.
    """
    if not isinstance(authorization, TransmitAuthorization):
        raise RefusedError(
            "place_combo requires a TransmitAuthorization",
            hint="mint one with authorize_open() or authorize_close()",
        )
    if authorization.strategy_id != intent.strategy_id:
        raise RefusedError(
            f"authorization is for strategy {authorization.strategy_id}, but the "
            f"order is {intent.strategy_id}",
            hint="an approval is for one specific structure and does not transfer",
        )
    if authorization.action is not intent.strategy_action:
        raise RefusedError(
            f"authorization is for {authorization.action.value}, but the order is "
            f"{intent.strategy_action.value}"
        )
    # The check the two above only looked like. An id and an action are shared
    # by every variant of a structure, so on their own they authorize a 50-lot
    # against an approval for a 1-lot -- demonstrated, not theorised. This
    # compares what was actually approved against what is about to be sent.
    sending = structure_digest(intent)
    if authorization.digest != sending:
        raise RefusedError(
            "the order does not match the structure that was authorized",
            hint="quantity, legs, strikes, limit price or maximum loss changed "
            "after approval; re-run the gates on the order you intend to send",
        )

    bag, order = build_combo(intent)
    if account:
        order.account = account

    # The spec check, against the order object itself rather than against the
    # intent it came from. The digest above proves the *structure* is the one
    # that was approved; this proves the four things the structure digest cannot
    # see -- which account it is going to, on which port, as what order type,
    # with what time in force. All four move the risk and none of them appear in
    # the legs, so an approval that did not bind them would survive the same
    # spread being sent to a different account as a GTC market order.
    if authorization.spec is not None:
        approved = authorization.spec
        sending_account = getattr(order, "account", "") or account or approved.account
        sending_type = str(getattr(order, "orderType", "") or COMBO_ORDER_TYPE)
        sending_tif = str(getattr(order, "tif", "") or "")
        drift = []
        if sending_account != approved.account:
            drift.append(f"account {sending_account!r} != approved {approved.account!r}")
        if sending_type != approved.order_type:
            drift.append(f"order type {sending_type!r} != approved {approved.order_type!r}")
        if sending_tif != approved.time_in_force:
            drift.append(f"TIF {sending_tif!r} != approved {approved.time_in_force!r}")
        if drift:
            raise RefusedError(
                "the order about to be sent is not the order that was approved",
                hint="; ".join(drift),
            )

    # The session fence, and it is deliberately the last thing checked. Every
    # gate above is about the order; this one is about whether the caller still
    # has standing to send it at all. Placed here -- after the order object
    # exists, before it is armed -- so the window it closes is as small as the
    # code can make it. Nothing has reached the broker yet, so refusing costs
    # exactly the local objects built above.
    #
    # Opens only. A close or a roll is exempt by construction: see the module
    # docstring, and ``authorize_close``'s exemption from the governor and the
    # daily cap, which this is the transmission-time twin of.
    if intent.strategy_action is StrategyAction.OPEN:
        lost = _lease_refusal(session_lease)
        if lost is not None:
            raise RefusedError(
                f"{SESSION_LEASE_LOST}: {lost}",
                hint="the session this order was authorized under is no longer "
                "held; nothing was sent, and the authorization is spent",
            )

    outbox = authorization.execution_outbox
    attempt_id = authorization.execution_attempt_id
    budget = authorization.transmission_budget
    reservation_id = authorization.budget_reservation_id
    if intent.strategy_action is StrategyAction.OPEN and outbox is not None:
        if not attempt_id:
            raise RefusedError(
                "an opening authorization has an execution outbox but no attempt id",
                hint="prepare the physical-send intent before consuming approval",
            )
        # This is the last local write before the broker call.  If it cannot be
        # published, nothing is handed to IBKR.
        outbox.send_intent(
            attempt_id, order_ref=str(getattr(order, "orderRef", ""))
        )

    # Set explicitly rather than relying on ib_async's default. This is the one
    # file permitted to arm an order, and an armed order should say so in the
    # code that arms it rather than inheriting it from a library default that
    # could change.
    order.transmit = True

    try:
        trade = ib.placeOrder(bag, order)
    except Exception as exc:  # noqa: BLE001 - broker may have accepted before raising
        if outbox is not None and attempt_id is not None:
            try:
                outbox.quarantine(
                    attempt_id,
                    reason=(
                        "placeOrder raised after the send door opened: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
            except Exception:
                pass
        return TransmitResult(
            strategy_id=intent.strategy_id,
            action=intent.strategy_action,
            transmitted=False,
            snapshot=None,
            message=(
                "broker submission outcome is ambiguous after placeOrder raised: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

    # A reservation is committed only after the broker call returns.  If this
    # durable transition fails, the broker may still have the order; retain the
    # reservation and quarantine rather than releasing a potentially used slot.
    budget_failure: str | None = None
    if budget is not None and reservation_id is not None:
        try:
            budget.commit(reservation_id)
        except Exception as exc:  # noqa: BLE001 - fail closed after broker touch
            budget_failure = (
                "transmission budget commit failed: "
                f"{type(exc).__name__}: {exc}"
            )
            if outbox is not None and attempt_id is not None:
                try:
                    outbox.quarantine(attempt_id, reason=budget_failure)
                except Exception:
                    pass

    # From here the order may be live at the broker. Every path below must
    # produce a TransmitResult rather than raise: an exception escaping after
    # placeOrder has returned is the one case where the caller cannot tell
    # whether anything was sent, which is worse than any state we can name.
    closing = intent.strategy_action is not StrategyAction.OPEN
    # A closing order is its own strategy with its own id, but the *position* it
    # retires is the thing the store tracks -- and the domain already records
    # which one that is. Observing against the closing intent's fresh id would
    # write a stream of events for a strategy the book has never heard of, which
    # replay would silently discard as orphans.
    record_as = intent.closes_strategy_id or intent.strategy_id

    def emit(timed: bool = False, lost: bool = False) -> BrokerOrderSnapshot:
        """Snapshot the trade and hand it to the sink. Never raises.

        A persistence failure is fatal and must propagate -- an unrecorded
        position is the thing the store exists to prevent -- but anything the
        *broker object* does wrong here is not a reason to lose the send.
        """
        observation = snapshot_from_trade(
            trade,
            observed_at=dt.datetime.now(dt.timezone.utc),
            quantity=intent.quantity,
            timed_out=timed,
            disconnected=lost,
        )
        if outbox is not None and attempt_id is not None:
            payload = observation.to_record()
            payload.update(
                {
                    "strategy_order_ref": str(getattr(order, "orderRef", "")),
                    "account": getattr(order, "account", None) or account,
                    "legs": [
                        {
                            "con_id": getattr(leg, "conId", None),
                            "action": getattr(leg, "action", None),
                            "ratio": getattr(leg, "ratio", None),
                            "exchange": getattr(leg, "exchange", None),
                        }
                        for leg in getattr(bag, "comboLegs", ())
                    ],
                }
            )
            try:
                outbox.submission_observed(attempt_id, observation=payload)
            except Exception as exc:  # noqa: BLE001 - broker state now needs quarantine
                try:
                    outbox.quarantine(
                        attempt_id,
                        reason=(
                            "submission receipt failed: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                    )
                except Exception:
                    pass
        if sink is not None:
            sink.observe(record_as, observation, closing=closing)
        return observation

    # The submission itself, before any polling. If the process dies on the very
    # next line, the store already knows an order was sent for this strategy.
    emit()

    waited = 0.0
    disconnected = False
    while waited < timeout:
        if not _is_connected(ib):
            disconnected = True
            break
        try:
            done = trade.isDone()
        except Exception:  # noqa: BLE001 - a partial Trade must not mask the send
            break
        # Emitted every iteration, so each state the broker walks through
        # reaches disk as it happens rather than being collapsed into whatever
        # happened to be true when the loop exited. The sink de-duplicates, so
        # polling faster than the broker moves costs nothing.
        emit()
        if done:
            break
        try:
            ib.sleep(poll_seconds)
        except Exception:  # noqa: BLE001 - a dropped socket surfaces here
            disconnected = True
            break
        waited += poll_seconds

    timed_out = False
    if not disconnected:
        try:
            timed_out = not trade.isDone()
        except Exception:  # noqa: BLE001
            timed_out = True

    # The final observation, which is also the one that records a timeout or a
    # disconnect -- neither of which the per-poll emissions above can see, since
    # both are facts about *us* rather than about the order.
    snapshot = emit(timed=timed_out, lost=disconnected)

    result = TransmitResult(
        strategy_id=intent.strategy_id,
        action=intent.strategy_action,
        transmitted=True,
        snapshot=snapshot,
        message=(
            "connection lost while awaiting the order outcome"
            if disconnected
            else None
        ),
        trade=trade,
    )
    if outbox is not None and attempt_id is not None:
        if snapshot.state is OrderLifecycleState.FILLED:
            classification = "FILLED"
        elif snapshot.state is OrderLifecycleState.PARTIALLY_FILLED:
            classification = "PARTIAL"
        elif snapshot.state in {
            OrderLifecycleState.SUBMITTED,
            OrderLifecycleState.ACKNOWLEDGED,
        }:
            classification = "WORKING"
        elif snapshot.state in {
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.REJECTED,
            OrderLifecycleState.INACTIVE,
        }:
            classification = "CANCELLED"
        else:
            classification = "UNKNOWN"
        try:
            outbox.outcome(
                attempt_id,
                classification=classification,
                observation=snapshot.to_record(),
            )
        except Exception as exc:  # noqa: BLE001 - never make an ambiguous send look clean
            try:
                outbox.quarantine(
                    attempt_id,
                    reason=(
                        "outcome receipt failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
            except Exception:
                pass
    if budget_failure:
        result = TransmitResult(
            strategy_id=result.strategy_id,
            action=result.action,
            transmitted=result.transmitted,
            snapshot=result.snapshot,
            message=budget_failure,
            trade=result.trade,
        )
    return result


def cancel_combo(
    ib: Any,
    trade: Any,
    *,
    authorization: CancelAuthorization,
    closing: bool = False,
    quantity: int | None = None,
    timeout: float = 15.0,
    poll_seconds: float = 0.5,
    sink: OrderLifecycleSink | None = None,
) -> TransmitResult:
    """Retract a working order. **The only cancelling call in this package.**

    Shaped exactly like :func:`place_combo` and for the same reasons: one call
    site, a required token with no public constructor, every observation pushed
    through the sink as it happens rather than summarised at the end, and no
    exception permitted to escape once the broker has been touched.

    ``trade`` is what ``place_combo`` returned on
    :attr:`TransmitResult.trade`, or an entry from ``ib.openTrades()`` after a
    restart. Both carry the ``Order`` that ``cancelOrder`` needs.

    **A cancel does not mean the order died.** ``PendingCancel`` is a working
    state, a cancel can lose a race with a fill, and cancelling the remainder of
    a partial leaves contracts in the book. So this returns the same nine-state
    :class:`TransmitResult` every other broker interaction does, and the caller
    must read :attr:`TransmitResult.has_position` rather than assume a
    cancellation left it flat.
    """
    if not isinstance(authorization, CancelAuthorization):
        raise RefusedError(
            "cancel_combo requires a CancelAuthorization",
            hint="mint one with authorize_cancel(); an opening or closing "
            "authorization does not grant this and is not interchangeable",
        )
    if trade is None:
        raise RefusedError(
            "there is no broker handle for the order being cancelled",
            hint="pass the trade place_combo returned, or one from openTrades()",
        )

    order = getattr(trade, "order", None)
    if order is None:
        raise RefusedError("the broker handle carries no order to cancel")

    strategy_id = authorization.strategy_id

    def emit(timed: bool = False, lost: bool = False) -> BrokerOrderSnapshot:
        observation = snapshot_from_trade(
            trade,
            observed_at=dt.datetime.now(dt.timezone.utc),
            quantity=quantity,
            timed_out=timed,
            disconnected=lost,
        )
        if sink is not None:
            sink.observe(strategy_id, observation, closing=closing)
        return observation

    ib.cancelOrder(order)

    # From here the request is at the broker. Every path below produces a
    # result rather than raising, for the reason place_combo gives: an
    # exception escaping after the broker has been touched is the one state
    # the caller cannot reason about.
    waited = 0.0
    disconnected = False
    while waited < timeout:
        if not _is_connected(ib):
            disconnected = True
            break
        try:
            done = trade.isDone()
        except Exception:  # noqa: BLE001 - a partial Trade must not mask the cancel
            break
        emit()
        if done:
            break
        try:
            ib.sleep(poll_seconds)
        except Exception:  # noqa: BLE001 - a dropped socket surfaces here
            disconnected = True
            break
        waited += poll_seconds

    timed_out = False
    if not disconnected:
        try:
            timed_out = not trade.isDone()
        except Exception:  # noqa: BLE001
            timed_out = True

    snapshot = emit(timed=timed_out, lost=disconnected)
    return TransmitResult(
        strategy_id=strategy_id,
        action=StrategyAction.CLOSE if closing else StrategyAction.OPEN,
        transmitted=True,
        snapshot=snapshot,
        message=(
            "connection lost while awaiting the cancellation outcome"
            if disconnected
            else None
        ),
        trade=trade,
    )


def _is_connected(ib: Any) -> bool:
    """Whether the broker connection is still up.

    Defaults to **True** when the object cannot answer -- a fake in a test has no
    ``isConnected``, and treating its silence as a disconnect would make every
    test order come back ``UNKNOWN``. The real ``IB`` always implements it, so
    the default only ever applies where there is no socket to lose.
    """
    probe = getattr(ib, "isConnected", None)
    if probe is None:
        return True
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001
        return False


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(float(value)))
    except (TypeError, ValueError, ArithmeticError):
        return None
    return parsed if parsed.is_finite() else None


def _fill_price(value: Any) -> Decimal | None:
    """An average fill price, which for a credit is **negative**.

    This must not screen negatives, and an earlier version did. ``build_combo``
    submits a net credit as a ``BUY`` at a negative limit, so IBKR reports the
    fill at a negative average price. Rejecting it returned ``None``, which the
    runner reads as "did not fill" -- and then records ``OPEN_FAILED`` for a
    spread that is live in the market. That is precisely the unrecorded position
    the position store exists to make impossible, arriving through the one path
    nobody thought to check the sign on.

    Zero is still rejected: a fill at exactly zero is not a price, it is an
    unpopulated field. The caller takes ``abs()`` when storing the credit, so the
    sign convention stays inside the broker boundary where the rest of it lives.
    """
    parsed = _decimal(value)
    if parsed is None or parsed == 0:
        return None
    return parsed
