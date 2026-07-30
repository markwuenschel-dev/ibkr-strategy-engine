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
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ..errors import RefusedError
from ..safety import SafetyGate
from .domain import OptionStrategyIntent, PriceEffect, StrategyAction
from .execution import build_combo
from .governor import GovernorVerdict
from .orderstate import BrokerOrderSnapshot, OrderLifecycleState, snapshot_from_trade
from .risk import CandidateRiskAssessment

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance only
    from .sink import OrderLifecycleSink

__all__ = [
    "TransmitAuthorization",
    "TransmitResult",
    "authorize_open",
    "authorize_close",
    "place_combo",
]

# The only object that makes a TransmitAuthorization constructible. Module
# private, never exported, and held solely by the two authorize_* functions.
_AUTHORIZATION_KEY = object()


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
    payload = json.dumps(
        {
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
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
) -> TransmitAuthorization:
    """Run every gate and mint the token, or raise.

    Order matters and mirrors :func:`engine.cli.cmd_trade`: cheapest and most
    absolute first, ``--arm`` checked **last** so an unarmed run still surfaces
    every other refusal rather than stopping at "not armed" and hiding a problem
    that would have bitten on the next run.
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

    gate.gate_armed(armed=armed)

    return TransmitAuthorization(
        strategy_id=intent.strategy_id,
        action=StrategyAction.OPEN,
        authorized_at=now,
        armed=armed,
        digest=structure_digest(intent),
        risk=risk,
        governor=governor,
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
) -> TransmitResult:
    """Transmit a combo order. **The only transmitting call in this package.**

    ``authorization`` is not optional and has no default. A caller who has not
    been through :func:`authorize_open` or :func:`authorize_close` has nothing to
    pass here, and the call does not compile into a working program.

    The identity check below is not redundant with the token's own validation:
    the token proves *some* strategy was approved, and this proves it was **this**
    one. Without it, an approval for a 1-lot could transmit a 10-lot.
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
    # Set explicitly rather than relying on ib_async's default. This is the one
    # file permitted to arm an order, and an armed order should say so in the
    # code that arms it rather than inheriting it from a library default that
    # could change.
    order.transmit = True

    trade = ib.placeOrder(bag, order)

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

    return TransmitResult(
        strategy_id=intent.strategy_id,
        action=intent.strategy_action,
        transmitted=True,
        snapshot=snapshot,
        message=(
            "connection lost while awaiting the order outcome"
            if disconnected
            else None
        ),
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
