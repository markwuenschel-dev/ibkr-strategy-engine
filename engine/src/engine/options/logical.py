"""The persisted identity of one intended position, across passes and restarts.

**The defect this removes.** ``run_once`` builds its candidate through
``selection.build_vertical``, which mints a fresh ``uuid4`` strategy id on every
pass. The verifier gate keys everything on the intent id inside the spec digest,
so a candidate proposed on pass N and approved by the reviewer between passes
was unclaimable on pass N+1: the new pass carried a new id, hashed to a new
digest, filed a new request, and the awaited approval orphaned. Every awaited
Grok approval died this way.

**Three concepts, kept separate on purpose.**

* ``logical_entry_id`` identifies the *intended position*. It is minted once,
  when a scan nomination is claimed, persisted before anything else happens,
  and never changes for the life of that intention -- across passes, restarts,
  re-quotes and re-reviews.
* ``proposal_revision`` identifies one *reviewed version* of that intention.
  A price or risk change supersedes the revision, never the entry.
* ``spec_digest`` binds the exact order-plus-risk facts of one revision. It is
  computed by :class:`engine.options.approval.AuthorizedOrderSpec` and this
  module never re-derives or approximates it.

The identity is deliberately **not** a hash of market values. A digest of the
credit or the strikes would change with every tick, which is exactly the
instability being removed; identity is a minted UUID and the market facts live
in the revision.

**Persistence is the position-store idiom.** :class:`LogicalEntryStore` is an
append-only fsync'd JSONL log replayed to rebuild state, the same shape as
:class:`engine.options.positions.PositionStore` and for the same reasons: an
append cannot lose an earlier fact, a torn final line costs one line, and a
corrupt line degrades the replay loudly instead of bricking it. Events are
*not* added to the position store itself -- a logical entry exists before any
position does, and most die without ever becoming one.

**The state machine.**

::

    CLAIMED --propose--> AWAITING_REVIEW --approve--> APPROVED_PENDING_EXECUTION
                              |    ^                        |
                   supersede  |    | refile after cooldown  | record_physical_attempt
                   (revision  |    |                        v
                    N -> N+1, |  REFUSED_COOLDOWN        EXECUTING
                    same id)  |    |                        |
                              |    | terminal refusals      | filled / failed
                              v    v                        v
                          EXPIRED  ABANDONED             FILLED | ABANDONED

Supersession applies to **revisions**, never to the entry: the superseded
revision is recorded in the lineage and the entry keeps its id and files the
replacement under revision N+1.

**Named policies**, so a reviewer of this module can find every discretionary
choice in one place:

* *Refusal cooldown* (:class:`RefusalCooldownPolicy`): a REFUSED review puts
  the entry in ``REFUSED_COOLDOWN`` for :attr:`~RefusalCooldownPolicy.cooldown`
  (default 30 minutes). After the cooldown a **new revision** may file -- but
  only with a *changed* spec digest, because the gate keys requests by digest
  and re-filing the identical spec would re-find the identical refusal.
  :attr:`~RefusalCooldownPolicy.terminal_after` refusals (default 3) abandon
  the entry outright: a reviewer that keeps saying no is not being asked the
  wrong way.
* *Review expiry*: a filed review is good until the packet's ``expires_at`` --
  the approval TTL, capped by
  :data:`engine.options.approval.MAXIMUM_APPROVAL_LIFETIME`. An unanswered or
  UNAVAILABLE review past that instant expires the entry and releases its
  reservation; an approval that late would be refused by the gate as expired
  anyway, so waiting longer buys nothing.
* *Transmit failure abandons*: a failed physical attempt marks the entry
  ABANDONED rather than retrying under the same identity. The approval it
  transmitted under is spec-bound and single-use, so a retry needs a fresh
  review of fresh facts -- which is a fresh nomination's job.

**Reservations.** One reservation per entry, minted at claim: an id and an
amount recorded on the entry so a later runner integration can fold pending
entries into the portfolio snapshot. Released -- with a lineage record, never
silently -- on expiry, terminal refusal, abandonment, and fill (where the real
position's ``buying_power_reserved`` takes over).

**Consumption is the caller's.** :meth:`LogicalEntryManager.service` returns
the gate's approval; it does not call ``consume``. The gate's own contract
places consumption after the arm gate so a dry run spends nothing
(:meth:`engine.options.approval.CollabVerifierGate.consume`), and the manager
does not know whether the pass is armed.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, fields as dataclass_fields
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import UUID, uuid4

from ..errors import JournalError, RefusedError
from .approval import (
    ApprovalDecision,
    AwaitingVerification,
    MAXIMUM_APPROVAL_LIFETIME,
    VerificationPacket,
    VerifierApproval,
    VerifierGate,
)
from .domain import (
    OptionLegIntent,
    OptionRight,
    OrderAction,
    StrategyType,
)
from .portfolio import PositionExposure

__all__ = [
    "DEFAULT_REFUSAL_POLICY",
    "EntryNomination",
    "LineageRecord",
    "LogicalEntry",
    "LogicalEntryManager",
    "LogicalEntryState",
    "LogicalEntryStore",
    "LogicalEvent",
    "RefusalCooldownPolicy",
    "RevisionOutcome",
    "SCHEMA_VERSION",
    "ServiceOutcome",
    "ServiceResult",
    "StaleNominationError",
]

SCHEMA_VERSION = 1


class LogicalEntryState(str, Enum):
    """Where one intended position stands. See the module docstring's diagram."""

    #: Nominated by a scan and claimed into a persistent identity. Nothing has
    #: been filed with the reviewer yet.
    CLAIMED = "CLAIMED"
    #: A review request for the current revision is on file and unanswered
    #: (or answered UNAVAILABLE, which blocks without deciding).
    AWAITING_REVIEW = "AWAITING_REVIEW"
    #: The reviewer approved the current revision; the order has not been sent.
    APPROVED_PENDING_EXECUTION = "APPROVED_PENDING_EXECUTION"
    #: A physical order is working. Exactly one may ever be working per entry.
    EXECUTING = "EXECUTING"
    #: The physical order filled. Terminal; the position store owns it now.
    FILLED = "FILLED"
    #: The reviewer refused; the entry is cooling down under the named policy.
    REFUSED_COOLDOWN = "REFUSED_COOLDOWN"
    #: The review outlived the approval TTL unanswered. Terminal.
    EXPIRED = "EXPIRED"
    #: Given up -- explicitly, after terminal refusals, or after a failed
    #: transmission. Terminal.
    ABANDONED = "ABANDONED"


#: States in which an entry still owns its underlying: a second nomination for
#: the same underlying must not create a second entry while one of these holds.
ACTIVE_STATES = (
    LogicalEntryState.CLAIMED,
    LogicalEntryState.AWAITING_REVIEW,
    LogicalEntryState.APPROVED_PENDING_EXECUTION,
    LogicalEntryState.EXECUTING,
    LogicalEntryState.REFUSED_COOLDOWN,
)


class RevisionOutcome(str, Enum):
    """The vocabulary of the lineage: what became of a revision or attempt."""

    SUPERSEDED = "SUPERSEDED"
    APPROVED = "APPROVED"
    REFUSED = "REFUSED"
    EXPIRED = "EXPIRED"
    RESERVATION_RELEASED = "RESERVATION_RELEASED"
    PHYSICAL_SUBMITTED = "PHYSICAL_SUBMITTED"
    PHYSICAL_FILLED = "PHYSICAL_FILLED"
    PHYSICAL_FAILED = "PHYSICAL_FAILED"
    ABANDONED = "ABANDONED"


class LogicalEvent(str, Enum):
    """The append-only vocabulary. Replaying these rebuilds every entry."""

    ENTRY_CLAIMED = "ENTRY_CLAIMED"
    REVIEW_FILED = "REVIEW_FILED"
    REVISION_SUPERSEDED = "REVISION_SUPERSEDED"
    REVIEW_APPROVED = "REVIEW_APPROVED"
    REVIEW_REFUSED = "REVIEW_REFUSED"
    REVIEW_EXPIRED = "REVIEW_EXPIRED"
    RESERVATION_RELEASED = "RESERVATION_RELEASED"
    PHYSICAL_SUBMITTED = "PHYSICAL_SUBMITTED"
    PHYSICAL_RESOLVED = "PHYSICAL_RESOLVED"
    ENTRY_ABANDONED = "ENTRY_ABANDONED"


class StaleNominationError(RefusedError):
    """A nomination older than the policy's ``max_nomination_age`` at claim time.

    A scan row nominated this morning describes this morning's market; claiming
    it into a reservation hours later would reserve buying power against facts
    nobody re-checked. A typed error, so the caller can distinguish "re-scan and
    nominate again" from every other refusal.
    """


def _refuse(message: str, *, hint: str | None = None) -> None:
    raise RefusedError(message, hint=hint)


def _gate_decision(refusal: RefusedError) -> ApprovalDecision | None:
    """Which recorded decision a gate refusal carries, or ``None`` for
    everything else (which the caller re-raises).

    Typed surface first: a gate that stamps its refusal with a ``decision``
    attribute (an :class:`~engine.options.approval.ApprovalDecision`) is
    believed outright. The shipped :class:`CollabVerifierGate` does not yet --
    ``approval.py`` is frozen for this integration -- so the fallback matches
    the exact prose its ``require`` composes ("the verifier answered
    REFUSED/UNAVAILABLE for trade intent ..."). That prose is pinned by
    ``tests/test_options_integration.py``, so a rewording breaks a test
    loudly instead of silently rerouting refusals into the generic re-raise.
    """
    decision = getattr(refusal, "decision", None)
    if isinstance(decision, ApprovalDecision):
        return decision
    message = str(getattr(refusal, "message", refusal))
    if "answered REFUSED" in message:
        return ApprovalDecision.REFUSED
    if "answered UNAVAILABLE" in message:
        return ApprovalDecision.UNAVAILABLE
    return None


def _aware(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be a timezone-aware datetime, got {value!r}")
    return value


@dataclass(frozen=True)
class RefusalCooldownPolicy:
    """The named policy for what a REFUSED review does to its entry -- and, by
    growth, the manager's whole hygiene policy: every discretionary age bound
    the workflow applies lives here, in one reviewable place.

    ``cooldown`` is how long the entry sits in ``REFUSED_COOLDOWN`` before a
    new revision may be filed. ``terminal_after`` is how many refusals, in
    total, abandon the entry outright -- reaching it releases the reservation
    and ends the entry, because a reviewer refusing the same intention for the
    third time is answering the intention, not the revision.

    The sweep bounds (:meth:`LogicalEntryManager.sweep`):

    * ``claimed_max_age`` -- how long a CLAIMED entry may sit without filing a
      review before the sweep abandons it and releases its reservation.
      Defaults to the approval TTL: a claim older than the longest any review
      could have lived was never going to file a fresh one.
    * ``cooldown_max_age`` -- how long a REFUSED_COOLDOWN entry may wait for a
      changed market before the sweep abandons it (default 24 hours). Without
      it a refusal whose spec never changes holds its reservation forever.

    ``max_nomination_age`` bounds how stale a nomination may be when
    :meth:`LogicalEntryManager.claim` turns it into a reservation (default 30
    minutes); older raises :class:`StaleNominationError`.
    """

    cooldown: dt.timedelta = dt.timedelta(minutes=30)
    terminal_after: int = 3
    claimed_max_age: dt.timedelta = MAXIMUM_APPROVAL_LIFETIME
    cooldown_max_age: dt.timedelta = dt.timedelta(hours=24)
    max_nomination_age: dt.timedelta = dt.timedelta(minutes=30)

    def __post_init__(self) -> None:
        if not isinstance(self.cooldown, dt.timedelta) or self.cooldown < dt.timedelta(0):
            raise ValueError(f"cooldown must be a non-negative timedelta, got {self.cooldown!r}")
        if not isinstance(self.terminal_after, int) or isinstance(self.terminal_after, bool):
            raise ValueError("terminal_after must be an int")
        if self.terminal_after < 1:
            raise ValueError(f"terminal_after must be at least 1, got {self.terminal_after}")
        for label in ("claimed_max_age", "cooldown_max_age", "max_nomination_age"):
            value = getattr(self, label)
            if not isinstance(value, dt.timedelta) or value <= dt.timedelta(0):
                raise ValueError(f"{label} must be a positive timedelta, got {value!r}")
        if self.cooldown_max_age < self.cooldown:
            raise ValueError(
                f"cooldown_max_age {self.cooldown_max_age} is shorter than the "
                f"cooldown {self.cooldown} itself, which would sweep every "
                "refused entry away before it could ever refile"
            )


DEFAULT_REFUSAL_POLICY = RefusalCooldownPolicy()


@dataclass(frozen=True)
class LineageRecord:
    """One fact in an entry's history: a revision's fate or a physical attempt.

    ``handoff_id`` is empty for records that are not about a review exchange
    (reservation releases, abandonment).
    """

    revision: int
    handoff_id: str
    outcome: RevisionOutcome
    at: dt.datetime
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.revision, int) or isinstance(self.revision, bool):
            raise ValueError(f"revision must be an int, got {self.revision!r}")
        if self.revision < 0:
            raise ValueError(f"revision must not be negative, got {self.revision}")
        if not isinstance(self.outcome, RevisionOutcome):
            raise ValueError(f"outcome must be a RevisionOutcome, got {self.outcome!r}")
        _aware(self.at, "at")

    def to_record(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "handoff_id": self.handoff_id,
            "outcome": self.outcome.value,
            "at": self.at.isoformat(),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class EntryNomination:
    """What a scan proposes: an intended position, before it has an identity.

    Deliberately market-value-free at the identity level: the credit and the
    risk numbers belong to a *revision* (they ride in the verification packet),
    not to the identity. What is here is what makes two nominations the same
    intention -- the underlying, the structure family, the direction, the
    selected expiration and legs -- plus the buying power the entry reserves.

    ``nominated_at`` is when the scan produced this nomination (a ScanBook
    row's ``evaluated_at``). ``None`` means "minted at claim time" -- the
    caller that nominates and claims in the same breath has nothing staler
    than now to declare. When it is set, :meth:`LogicalEntryManager.claim`
    enforces the policy's ``max_nomination_age`` against it.
    """

    underlying: str
    strategy_family: StrategyType
    direction: str
    expiration: dt.date
    legs: tuple[OptionLegIntent, ...]
    reservation_amount: Decimal
    nominated_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        if self.nominated_at is not None:
            _aware(self.nominated_at, "nominated_at")
        if not isinstance(self.underlying, str) or not self.underlying.strip():
            raise ValueError("a nomination must name its underlying")
        if not isinstance(self.strategy_family, StrategyType):
            raise ValueError(f"strategy_family must be a StrategyType, got {self.strategy_family!r}")
        if not isinstance(self.direction, str) or not self.direction.strip():
            raise ValueError("a nomination must state its direction")
        if not isinstance(self.expiration, dt.date) or isinstance(self.expiration, dt.datetime):
            raise ValueError("expiration must be a date")
        if not isinstance(self.legs, tuple) or not self.legs:
            raise ValueError("a nomination must carry at least one leg")
        for leg in self.legs:
            if not isinstance(leg, OptionLegIntent):
                raise ValueError(f"every leg must be an OptionLegIntent, got {type(leg).__name__}")
        if not isinstance(self.reservation_amount, Decimal) or self.reservation_amount < 0:
            raise ValueError(
                f"reservation_amount must be a non-negative Decimal, got {self.reservation_amount!r}"
            )


@dataclass(frozen=True)
class LogicalEntry:
    """One intended position, with a stable identity and a revision history.

    Frozen: every transition reconstructs through the constructor so the
    invariants below re-run, exactly as :func:`engine.options.positions._replace`
    does for positions.
    """

    logical_entry_id: UUID
    underlying: str
    strategy_family: StrategyType
    direction: str
    expiration: dt.date
    legs: tuple[OptionLegIntent, ...]
    state: LogicalEntryState
    created_at: dt.datetime
    updated_at: dt.datetime
    proposal_revision: int = 1
    current_spec_digest: str = ""
    current_handoff_id: str = ""
    current_approval_id: str = ""
    review_expires_at: dt.datetime | None = None
    reservation_id: UUID | None = None
    reservation_amount: Decimal | None = None
    refused_at: dt.datetime | None = None
    refusal_count: int = 0
    lineage: tuple[LineageRecord, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.logical_entry_id, UUID):
            _refuse(f"logical_entry_id must be a UUID, got {type(self.logical_entry_id).__name__}")
        if not isinstance(self.underlying, str) or not self.underlying.strip():
            _refuse("a logical entry must name its underlying")
        if not isinstance(self.strategy_family, StrategyType):
            _refuse(f"strategy_family must be a StrategyType, got {self.strategy_family!r}")
        if not isinstance(self.state, LogicalEntryState):
            _refuse(f"state must be a LogicalEntryState, got {self.state!r}")
        if not isinstance(self.legs, tuple) or not self.legs:
            _refuse("a logical entry must carry its legs")
        if not isinstance(self.proposal_revision, int) or self.proposal_revision < 1:
            _refuse(f"proposal_revision must be a positive int, got {self.proposal_revision!r}")
        if not isinstance(self.created_at, dt.datetime) or self.created_at.tzinfo is None:
            _refuse("created_at must be a timezone-aware datetime")
        if not isinstance(self.updated_at, dt.datetime) or self.updated_at.tzinfo is None:
            _refuse("updated_at must be a timezone-aware datetime")
        if self.reservation_amount is not None and (
            not isinstance(self.reservation_amount, Decimal) or self.reservation_amount < 0
        ):
            _refuse(f"reservation_amount must be a non-negative Decimal, got {self.reservation_amount!r}")
        if not isinstance(self.lineage, tuple):
            _refuse(f"lineage must be a tuple, got {type(self.lineage).__name__}")
        if not isinstance(self.refusal_count, int) or self.refusal_count < 0:
            _refuse(f"refusal_count must be a non-negative int, got {self.refusal_count!r}")
        # An entry in a state that owns a reservation must actually hold one.
        # The reverse -- a terminal entry still holding a reservation -- is the
        # leak this module exists to prevent, so it is refused outright.
        if self.state in (LogicalEntryState.EXPIRED, LogicalEntryState.ABANDONED):
            if self.reservation_id is not None:
                _refuse(
                    f"a {self.state.value} entry must not hold a reservation",
                    hint="reservations are released with a lineage record when a "
                    "review expires or an entry is abandoned; a terminal entry "
                    "holding one is a buying-power leak",
                )

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def normalized_underlying(self) -> str:
        return self.underlying.strip().upper()

    def describe(self) -> str:
        return (
            f"{self.state.value:<26} {self.normalized_underlying:<6} "
            f"{self.strategy_family.value} rev {self.proposal_revision} "
            f"[{self.logical_entry_id}]"
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "logical_entry_id": str(self.logical_entry_id),
            "underlying": self.normalized_underlying,
            "strategy_family": self.strategy_family.value,
            "direction": self.direction,
            "expiration": self.expiration.isoformat(),
            "created_at": self.created_at.isoformat(),
            "reservation_id": str(self.reservation_id) if self.reservation_id else None,
            "reservation_amount": (
                str(self.reservation_amount) if self.reservation_amount is not None else None
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
                    "trading_class": leg.trading_class,
                }
                for leg in self.legs
            ],
        }

    @classmethod
    def from_claim_record(cls, record: dict[str, Any], *, at: dt.datetime) -> "LogicalEntry":
        """Rebuild the claimed entry through the domain constructors, so a
        corrupted line fails to load rather than producing a half-entry."""
        legs = tuple(
            OptionLegIntent(
                con_id=int(leg["con_id"]),
                symbol=str(leg["symbol"]),
                expiration=dt.date.fromisoformat(leg["expiration"]),
                strike=Decimal(str(leg["strike"])),
                right=OptionRight(leg["right"]),
                action=OrderAction(leg["action"]),
                ratio=int(leg["ratio"]),
                multiplier=int(leg["multiplier"]),
                exchange=str(leg["exchange"]),
                trading_class=leg.get("trading_class"),
            )
            for leg in record["legs"]
        )
        return cls(
            logical_entry_id=UUID(record["logical_entry_id"]),
            underlying=str(record["underlying"]),
            strategy_family=StrategyType(record["strategy_family"]),
            direction=str(record["direction"]),
            expiration=dt.date.fromisoformat(record["expiration"]),
            legs=legs,
            state=LogicalEntryState.CLAIMED,
            created_at=dt.datetime.fromisoformat(record["created_at"]),
            updated_at=at,
            reservation_id=(
                UUID(record["reservation_id"]) if record.get("reservation_id") else None
            ),
            reservation_amount=(
                Decimal(str(record["reservation_amount"]))
                if record.get("reservation_amount") is not None
                else None
            ),
        )


def _replace(entry: LogicalEntry, **changes: Any) -> LogicalEntry:
    """A frozen update that re-runs every invariant. Enumerated from the
    dataclass, never by hand -- see ``positions._replace`` for the failure a
    hand-written literal invites."""
    values: dict[str, Any] = {
        field.name: getattr(entry, field.name) for field in dataclass_fields(entry)
    }
    values.update(changes)
    return LogicalEntry(**values)


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


class LogicalEntryStore:
    """Append-only, fsync'd record of every logical entry's lifecycle.

    Same durability contract as :class:`engine.options.positions.PositionStore`:
    a write that cannot be made durable raises :class:`~engine.errors.JournalError`
    and stops the workflow, because an identity that cannot be recorded must not
    file a review the next pass will not remember asking for.

    **Single writer, stated as an assumption rather than discovered as a bug.**
    Exactly one engine process appends to this log at a time -- the same
    assumption :class:`~engine.options.positions.PositionStore` and the order
    journal make, enforced operationally by the one-runner deployment rather
    than by a file lock. It matters here specifically because
    :meth:`LogicalEntryManager.claim` is check-then-append: two concurrent
    writers could both pass the ``active_for`` check and append two ACTIVE
    entries for one underlying, shadowing a reservation nothing can release.
    ``entries`` therefore treats a second concurrent ACTIVE claim for an
    underlying as an integrity error: the later entry is excluded from the
    replayed book and the exclusion is recorded loudly, never absorbed.
    """

    FILENAME = "logical_entries.jsonl"

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.suffix != ".jsonl":
            self.path = self.path / self.FILENAME

    # -- writing ----------------------------------------------------------

    def _append(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = {"v": SCHEMA_VERSION, **record}
        try:
            line = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        except Exception as exc:  # noqa: BLE001 - default=str can raise anything
            raise JournalError(
                f"could not serialize a {record.get('event')!r} logical-entry event: "
                f"{type(exc).__name__}: {exc}",
                hint="an identity that cannot be recorded must not file a review",
            ) from exc

        data = (line + "\n").encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                written = os.write(fd, data)
                if written != len(data):  # pragma: no cover - short append
                    raise JournalError(
                        f"short write to the logical-entry store ({written}/{len(data)} bytes)"
                    )
                os.fsync(fd)
            finally:
                os.close(fd)
        except JournalError:
            raise
        except OSError as exc:
            raise JournalError(
                f"cannot write the logical-entry store at {self.path}: {exc}",
                hint="a review filed against an unrecorded identity orphans on the "
                "next pass, which is the exact defect this store removes",
            ) from exc
        return payload

    def record_claimed(self, entry: LogicalEntry, *, at: dt.datetime) -> dict[str, Any]:
        """Persist the identity. Written **before** any review request exists,
        so a crash between the two leaves an entry that remembers itself rather
        than a handoff nobody claims."""
        return self._append(
            {
                "event": LogicalEvent.ENTRY_CLAIMED.value,
                "at": at.isoformat(),
                **entry.to_record(),
            }
        )

    def record_review_filed(
        self,
        entry_id: UUID,
        *,
        revision: int,
        spec_digest: str,
        handoff_id: str,
        expires_at: dt.datetime,
        at: dt.datetime,
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": LogicalEvent.REVIEW_FILED.value,
                "at": at.isoformat(),
                "logical_entry_id": str(entry_id),
                "revision": revision,
                "spec_digest": spec_digest,
                "handoff_id": handoff_id,
                "expires_at": expires_at.isoformat(),
            }
        )

    def record_revision_superseded(
        self,
        entry_id: UUID,
        *,
        revision: int,
        handoff_id: str,
        reason: str,
        at: dt.datetime,
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": LogicalEvent.REVISION_SUPERSEDED.value,
                "at": at.isoformat(),
                "logical_entry_id": str(entry_id),
                "revision": revision,
                "handoff_id": handoff_id,
                "reason": reason,
            }
        )

    def record_review_approved(
        self, entry_id: UUID, *, revision: int, response_id: str, at: dt.datetime
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": LogicalEvent.REVIEW_APPROVED.value,
                "at": at.isoformat(),
                "logical_entry_id": str(entry_id),
                "revision": revision,
                "response_id": response_id,
            }
        )

    def record_review_refused(
        self,
        entry_id: UUID,
        *,
        revision: int,
        handoff_id: str,
        at: dt.datetime,
        detail: str = "",
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": LogicalEvent.REVIEW_REFUSED.value,
                "at": at.isoformat(),
                "logical_entry_id": str(entry_id),
                "revision": revision,
                "handoff_id": handoff_id,
                "detail": detail,
            }
        )

    def record_review_expired(
        self, entry_id: UUID, *, revision: int, handoff_id: str, at: dt.datetime
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": LogicalEvent.REVIEW_EXPIRED.value,
                "at": at.isoformat(),
                "logical_entry_id": str(entry_id),
                "revision": revision,
                "handoff_id": handoff_id,
            }
        )

    def record_reservation_released(
        self, entry_id: UUID, *, reservation_id: UUID, reason: str, at: dt.datetime
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": LogicalEvent.RESERVATION_RELEASED.value,
                "at": at.isoformat(),
                "logical_entry_id": str(entry_id),
                "reservation_id": str(reservation_id),
                "reason": reason,
            }
        )

    def record_physical_submitted(
        self, entry_id: UUID, *, revision: int, handoff_id: str, at: dt.datetime
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": LogicalEvent.PHYSICAL_SUBMITTED.value,
                "at": at.isoformat(),
                "logical_entry_id": str(entry_id),
                "revision": revision,
                "handoff_id": handoff_id,
            }
        )

    def record_physical_resolved(
        self, entry_id: UUID, *, filled: bool, detail: str, at: dt.datetime
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": LogicalEvent.PHYSICAL_RESOLVED.value,
                "at": at.isoformat(),
                "logical_entry_id": str(entry_id),
                "filled": bool(filled),
                "detail": detail,
            }
        )

    def record_abandoned(
        self, entry_id: UUID, *, reason: str, at: dt.datetime
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": LogicalEvent.ENTRY_ABANDONED.value,
                "at": at.isoformat(),
                "logical_entry_id": str(entry_id),
                "reason": reason,
            }
        )

    # -- reading ----------------------------------------------------------

    def events(self) -> Iterator[dict[str, Any]]:
        """Every well-formed event; an unparseable line is skipped here and its
        consequences surface through ``entries(errors=...)``."""
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return
        except OSError as exc:
            raise JournalError(f"cannot read the logical-entry store: {exc}") from exc

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                yield parsed

    def entries(self, *, errors: list[str] | None = None) -> dict[UUID, LogicalEntry]:
        """Rebuild every entry by replaying the log, oldest event first.

        A corrupt line degrades the replay and is recorded in ``errors``; it
        never bricks the book. Mirrors ``PositionStore.positions``.
        """
        problems: list[str] = [] if errors is None else errors
        book: dict[UUID, LogicalEntry] = {}
        for event in self.events():
            kind = event.get("event")
            raw_id = event.get("logical_entry_id")
            if not isinstance(raw_id, str):
                continue
            try:
                entry_id = UUID(raw_id)
            except (ValueError, AttributeError):
                continue

            if kind == LogicalEvent.ENTRY_CLAIMED.value:
                try:
                    at = dt.datetime.fromisoformat(str(event.get("at")))
                    claimed = LogicalEntry.from_claim_record(event, at=at)
                except (KeyError, ValueError, TypeError, InvalidOperation, RefusedError) as exc:
                    problems.append(
                        f"{entry_id}: unreadable ENTRY_CLAIMED ({type(exc).__name__}: {exc})"
                    )
                    continue
                # One ACTIVE entry per underlying is claim()'s invariant, held
                # only under the single-writer assumption (class docstring). A
                # log carrying a second concurrent ACTIVE claim is corrupt, and
                # replaying it silently would put two entries -- and a
                # reservation nothing can release -- in the book. The later
                # claim is excluded and the exclusion recorded, loudly.
                holder = next(
                    (
                        existing
                        for existing in book.values()
                        if existing.is_active
                        and existing.normalized_underlying
                        == claimed.normalized_underlying
                    ),
                    None,
                )
                if holder is not None:
                    problems.append(
                        f"{entry_id}: duplicate ACTIVE claim for "
                        f"{claimed.normalized_underlying} while "
                        f"{holder.logical_entry_id} holds it; the shadowed entry "
                        f"{entry_id} is excluded from the replayed book"
                    )
                    continue
                book[entry_id] = claimed
                continue

            current = book.get(entry_id)
            if current is None:
                continue
            try:
                book[entry_id] = self._apply(current, kind, event)
            except (KeyError, ValueError, TypeError, InvalidOperation, RefusedError) as exc:
                problems.append(
                    f"{entry_id}: unreadable {kind} ({type(exc).__name__}: {exc})"
                )
        return book

    @staticmethod
    def _apply(current: LogicalEntry, kind: Any, event: dict[str, Any]) -> LogicalEntry:
        """One transition. Raises on a malformed event; the caller records the
        failure and drops the transition, not the entry."""
        at = dt.datetime.fromisoformat(str(event.get("at")))
        if at.tzinfo is None:
            raise ValueError(f"{kind} carries a naive timestamp")

        if kind == LogicalEvent.REVIEW_FILED.value:
            revision = int(event["revision"])
            expires = dt.datetime.fromisoformat(str(event["expires_at"]))
            return _replace(
                current,
                state=LogicalEntryState.AWAITING_REVIEW,
                proposal_revision=revision,
                current_spec_digest=str(event["spec_digest"]),
                current_handoff_id=str(event["handoff_id"]),
                current_approval_id="",
                review_expires_at=expires,
                updated_at=at,
            )

        if kind == LogicalEvent.REVISION_SUPERSEDED.value:
            record = LineageRecord(
                revision=int(event["revision"]),
                handoff_id=str(event.get("handoff_id") or ""),
                outcome=RevisionOutcome.SUPERSEDED,
                at=at,
                detail=str(event.get("reason") or ""),
            )
            return _replace(current, lineage=current.lineage + (record,), updated_at=at)

        if kind == LogicalEvent.REVIEW_APPROVED.value:
            record = LineageRecord(
                revision=int(event["revision"]),
                handoff_id=current.current_handoff_id,
                outcome=RevisionOutcome.APPROVED,
                at=at,
                detail=str(event.get("response_id") or ""),
            )
            return _replace(
                current,
                state=LogicalEntryState.APPROVED_PENDING_EXECUTION,
                current_approval_id=str(event.get("response_id") or ""),
                lineage=current.lineage + (record,),
                updated_at=at,
            )

        if kind == LogicalEvent.REVIEW_REFUSED.value:
            record = LineageRecord(
                revision=int(event["revision"]),
                handoff_id=str(event.get("handoff_id") or ""),
                outcome=RevisionOutcome.REFUSED,
                at=at,
                detail=str(event.get("detail") or ""),
            )
            return _replace(
                current,
                state=LogicalEntryState.REFUSED_COOLDOWN,
                refused_at=at,
                refusal_count=current.refusal_count + 1,
                lineage=current.lineage + (record,),
                updated_at=at,
            )

        if kind == LogicalEvent.REVIEW_EXPIRED.value:
            record = LineageRecord(
                revision=int(event["revision"]),
                handoff_id=str(event.get("handoff_id") or ""),
                outcome=RevisionOutcome.EXPIRED,
                at=at,
            )
            return _replace(
                current,
                state=LogicalEntryState.EXPIRED,
                lineage=current.lineage + (record,),
                updated_at=at,
            )

        if kind == LogicalEvent.RESERVATION_RELEASED.value:
            record = LineageRecord(
                revision=current.proposal_revision,
                handoff_id="",
                outcome=RevisionOutcome.RESERVATION_RELEASED,
                at=at,
                detail=str(event.get("reason") or ""),
            )
            return _replace(
                current,
                reservation_id=None,
                reservation_amount=None,
                lineage=current.lineage + (record,),
                updated_at=at,
            )

        if kind == LogicalEvent.PHYSICAL_SUBMITTED.value:
            record = LineageRecord(
                revision=int(event["revision"]),
                handoff_id=str(event.get("handoff_id") or ""),
                outcome=RevisionOutcome.PHYSICAL_SUBMITTED,
                at=at,
            )
            return _replace(
                current,
                state=LogicalEntryState.EXECUTING,
                lineage=current.lineage + (record,),
                updated_at=at,
            )

        if kind == LogicalEvent.PHYSICAL_RESOLVED.value:
            filled = bool(event.get("filled"))
            record = LineageRecord(
                revision=current.proposal_revision,
                handoff_id=current.current_handoff_id,
                outcome=(
                    RevisionOutcome.PHYSICAL_FILLED if filled else RevisionOutcome.PHYSICAL_FAILED
                ),
                at=at,
                detail=str(event.get("detail") or ""),
            )
            return _replace(
                current,
                state=LogicalEntryState.FILLED if filled else LogicalEntryState.ABANDONED,
                lineage=current.lineage + (record,),
                updated_at=at,
            )

        if kind == LogicalEvent.ENTRY_ABANDONED.value:
            record = LineageRecord(
                revision=current.proposal_revision,
                handoff_id="",
                outcome=RevisionOutcome.ABANDONED,
                at=at,
                detail=str(event.get("reason") or ""),
            )
            return _replace(
                current,
                state=LogicalEntryState.ABANDONED,
                lineage=current.lineage + (record,),
                updated_at=at,
            )

        # A newer writer's vocabulary is not corruption. Left alone.
        return current

    def integrity_errors(self) -> tuple[str, ...]:
        problems: list[str] = []
        self.entries(errors=problems)
        return tuple(problems)

    def get(self, entry_id: UUID) -> LogicalEntry | None:
        return self.entries().get(entry_id)

    def active(self) -> list[LogicalEntry]:
        return sorted(
            (e for e in self.entries().values() if e.is_active),
            key=lambda e: e.created_at,
        )

    def active_for(self, underlying: str) -> LogicalEntry | None:
        """The one ACTIVE entry for this underlying, or ``None``."""
        wanted = underlying.strip().upper()
        for entry in self.active():
            if entry.normalized_underlying == wanted:
                return entry
        return None


# ---------------------------------------------------------------------------
# the manager
# ---------------------------------------------------------------------------


class ServiceOutcome(str, Enum):
    """What one service pass did with one entry."""

    FILED = "FILED"
    WAITING = "WAITING"
    UNAVAILABLE = "UNAVAILABLE"
    APPROVED = "APPROVED"
    ALREADY_APPROVED = "ALREADY_APPROVED"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    REFUSED = "REFUSED"
    REFUSED_TERMINAL = "REFUSED_TERMINAL"
    COOLING = "COOLING"
    REFUSAL_STANDS = "REFUSAL_STANDS"
    REFILED = "REFILED"
    #: Produced only by :meth:`LogicalEntryManager.sweep`: the entry outlived a
    #: named hygiene bound and was abandoned with its reservation released.
    ABANDONED = "ABANDONED"


@dataclass(frozen=True)
class ServiceResult:
    """One pass's verdict on one entry. ``approval`` is present only for
    :attr:`ServiceOutcome.APPROVED` and is the gate's own object -- the caller
    authorizes and consumes it; the manager never spends it."""

    outcome: ServiceOutcome
    entry: LogicalEntry
    approval: VerifierApproval | None = None
    request_id: str = ""


class LogicalEntryManager:
    """The workflow owner: claims identities, files reviews, services answers.

    Injectable clock, gate and store. Every method takes an explicit ``now`` so
    tests run at one fixed logical instant; the constructor clock is the
    default for callers that do not care.
    """

    def __init__(
        self,
        *,
        store: LogicalEntryStore,
        gate: VerifierGate,
        clock: Callable[[], dt.datetime] | None = None,
        refusal_policy: RefusalCooldownPolicy = DEFAULT_REFUSAL_POLICY,
    ) -> None:
        self.store = store
        self.gate = gate
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.refusal_policy = refusal_policy

    # -- helpers ----------------------------------------------------------

    def _now(self, now: dt.datetime | None) -> dt.datetime:
        instant = now if now is not None else self.clock()
        return _aware(instant, "now")

    def _reload(self, entry_id: UUID) -> LogicalEntry:
        entry = self.store.get(entry_id)
        if entry is None:
            _refuse(
                f"logical entry {entry_id} is not in the store",
                hint="an entry must be claimed (and persisted) before it is serviced",
            )
        return entry  # type: ignore[return-value]

    def entry(self, entry_id: UUID) -> LogicalEntry:
        return self._reload(entry_id)

    def active_for(self, underlying: str) -> LogicalEntry | None:
        """Step 4: later passes load the same entry, never mint another id."""
        return self.store.active_for(underlying)

    def reservations(self) -> tuple[PositionExposure, ...]:
        """Every ACTIVE entry's reservation, as governor-shaped exposures.

        The runner folds these into each portfolio snapshot beside the
        position store's own exposures (contract section 4). ``strategy_id``
        is the entry's ``logical_entry_id`` -- the SAME uuid the store keys on
        once the order is submitted -- which is what makes the fold's dedupe
        structural rather than heuristic. ``maximum_loss`` is set to the
        reserved amount as a stand-in: the true defined loss is not known
        until an intent is sized, and overstating is the conservative
        direction (it can only refuse a candidate a more precise figure
        would have allowed).
        """
        return tuple(
            PositionExposure(
                underlying=entry.normalized_underlying,
                buying_power_reserved=entry.reservation_amount,
                maximum_loss=entry.reservation_amount,
                strategy_id=entry.logical_entry_id,
            )
            for entry in self.store.active()
            if entry.reservation_amount is not None
        )

    @staticmethod
    def _check_identity(entry: LogicalEntry, packet: VerificationPacket) -> None:
        """The packet's intent id must BE the logical identity. A packet built
        with a freshly minted uuid4 is the exact defect this module removes,
        so it is refused loudly rather than filed and orphaned quietly."""
        if packet.spec.intent_id != entry.logical_entry_id:
            _refuse(
                f"the packet's intent id {packet.spec.intent_id} is not this entry's "
                f"logical identity {entry.logical_entry_id}",
                hint="build the OptionStrategyIntent with strategy_id="
                "logical_entry_id; a per-pass uuid4 orphans every awaited approval",
            )

    def _release_reservation(
        self, entry: LogicalEntry, *, reason: str, at: dt.datetime
    ) -> None:
        if entry.reservation_id is not None:
            self.store.record_reservation_released(
                entry.logical_entry_id,
                reservation_id=entry.reservation_id,
                reason=reason,
                at=at,
            )

    def _withdraw_request(self, request_id: str, *, note: str) -> None:
        """Close the builder's own review request when the question is retired.

        Routed through the gate's ``withdraw`` when the gate offers one
        (:meth:`engine.options.approval.CollabVerifierGate.withdraw`); the
        :class:`~engine.options.approval.VerifierGate` protocol itself does not
        require a closer, so a gate without one simply leaves the request to
        its own lifecycle -- the capability probe is explicit here rather than
        hidden in a try/except.
        """
        if not request_id:
            return
        withdraw = getattr(self.gate, "withdraw", None)
        if withdraw is None:
            return
        withdraw(request_id, note=note)

    def _file_revision(
        self, entry: LogicalEntry, packet: VerificationPacket, *, revision: int, now: dt.datetime
    ) -> str:
        """File exactly one review request for one revision.

        The gate's ``propose`` is idempotent by spec digest, so a crash between
        the handoff being created and ``REVIEW_FILED`` being appended is
        recovered on the next pass: re-proposing the same digest returns the
        same handoff id, and the append happens then.
        """
        handoff_id = self.gate.propose(packet, now=now)
        self.store.record_review_filed(
            entry.logical_entry_id,
            revision=revision,
            spec_digest=packet.spec.digest,
            handoff_id=handoff_id,
            expires_at=packet.expires_at,
            at=now,
        )
        return handoff_id

    # -- step 1: claim ----------------------------------------------------

    def claim(
        self, nomination: EntryNomination, *, now: dt.datetime | None = None
    ) -> LogicalEntry:
        """Claim a nomination into a persistent identity. Idempotent per
        underlying: while an ACTIVE entry holds the underlying, a second
        nomination returns that entry and creates nothing.

        A nomination that declares its ``nominated_at`` is refused with
        :class:`StaleNominationError` once it is older than the policy's
        ``max_nomination_age``: it describes a market nobody has re-checked,
        and the fix is a fresh scan, not a fresh reservation.

        The entry -- identity, reservation and all -- is persisted **before**
        any review request can be filed for it (step 2 of the contract).
        """
        at = self._now(now)
        existing = self.store.active_for(nomination.underlying)
        if existing is not None:
            return existing

        if nomination.nominated_at is not None:
            age = at - nomination.nominated_at
            if age > self.refusal_policy.max_nomination_age:
                raise StaleNominationError(
                    f"the nomination for {nomination.underlying.strip().upper()} is "
                    f"{age} old, past the {self.refusal_policy.max_nomination_age} "
                    "maximum nomination age",
                    hint="a stale nomination describes a market nobody re-checked; "
                    "re-scan and nominate again rather than claiming old facts",
                )

        entry = LogicalEntry(
            logical_entry_id=uuid4(),
            underlying=nomination.underlying.strip().upper(),
            strategy_family=nomination.strategy_family,
            direction=nomination.direction,
            expiration=nomination.expiration,
            legs=nomination.legs,
            state=LogicalEntryState.CLAIMED,
            created_at=at,
            updated_at=at,
            reservation_id=uuid4(),
            reservation_amount=nomination.reservation_amount,
        )
        # Re-check immediately before the append. claim() is check-then-append
        # and safe only under the store's single-writer assumption; the
        # re-check narrows the window in which a concurrent claimer -- or a
        # caller re-entering through a stale first read -- could append a
        # second ACTIVE entry for the underlying, which replay would then have
        # to quarantine as an integrity error.
        raced = self.store.active_for(nomination.underlying)
        if raced is not None:
            return raced
        self.store.record_claimed(entry, at=at)
        return entry

    # -- step 3: propose --------------------------------------------------

    def propose(
        self,
        entry: LogicalEntry,
        packet: VerificationPacket,
        *,
        now: dt.datetime | None = None,
    ) -> LogicalEntry:
        """File the current revision's exact reviewed packet through the gate."""
        at = self._now(now)
        entry = self._reload(entry.logical_entry_id)
        self._check_identity(entry, packet)
        if entry.state is not LogicalEntryState.CLAIMED:
            _refuse(
                f"propose() takes a CLAIMED entry, this one is {entry.state.value}",
                hint="an entry that already filed is serviced, not re-proposed",
            )
        self._file_revision(entry, packet, revision=entry.proposal_revision, now=at)
        return self._reload(entry.logical_entry_id)

    # -- step 5: service --------------------------------------------------

    def service(
        self,
        entry: LogicalEntry,
        packet_now: VerificationPacket,
        *,
        now: dt.datetime | None = None,
    ) -> ServiceResult:
        """One pass over one entry, with the freshly revalidated packet.

        Never sleeps, never waits; every branch either returns a
        :class:`ServiceResult` or raises the gate's own fail-closed error.
        """
        at = self._now(now)
        entry = self._reload(entry.logical_entry_id)
        self._check_identity(entry, packet_now)

        if entry.state is LogicalEntryState.CLAIMED:
            self._file_revision(entry, packet_now, revision=entry.proposal_revision, now=at)
            entry = self._reload(entry.logical_entry_id)
            return ServiceResult(ServiceOutcome.FILED, entry, request_id=entry.current_handoff_id)

        if entry.state is LogicalEntryState.REFUSED_COOLDOWN:
            return self._service_cooldown(entry, packet_now, at)

        if entry.state is LogicalEntryState.APPROVED_PENDING_EXECUTION:
            return ServiceResult(
                ServiceOutcome.ALREADY_APPROVED, entry, request_id=entry.current_handoff_id
            )

        if entry.state is not LogicalEntryState.AWAITING_REVIEW:
            _refuse(
                f"logical entry {entry.logical_entry_id} is {entry.state.value} and "
                "cannot be serviced",
                hint="terminal and executing entries are not review-serviceable",
            )

        # -- expiry outranks everything: an approval this late would be
        # refused by the gate as expired anyway, and the reservation must not
        # outlive the review it was reserved for.
        if entry.review_expires_at is not None and at >= entry.review_expires_at:
            return self._expire(entry, at)

        # -- a changed digest is a different order. The old approval, if one
        # exists, is deliberately never touched: not required, not consumed.
        if packet_now.spec.digest != entry.current_spec_digest:
            return self._supersede(entry, packet_now, at, reason="spec digest changed")

        # -- unchanged digest: ask the gate. Its propose-inside-require is
        # idempotent by digest, so this never files a second handoff.
        try:
            approval = self.gate.require(packet_now, now=at)
        except AwaitingVerification as waiting:
            return ServiceResult(
                ServiceOutcome.WAITING, entry, request_id=waiting.request_id
            )
        except RefusedError as refusal:
            decision = _gate_decision(refusal)
            if decision is ApprovalDecision.REFUSED:
                return self._record_refusal(
                    entry, at, detail=str(getattr(refusal, "message", refusal))
                )
            if decision is ApprovalDecision.UNAVAILABLE:
                # Blocks without deciding: the entry keeps waiting and the
                # expiry above is what eventually ends it.
                return ServiceResult(
                    ServiceOutcome.UNAVAILABLE, entry, request_id=entry.current_handoff_id
                )
            raise

        self.store.record_review_approved(
            entry.logical_entry_id,
            revision=entry.proposal_revision,
            response_id=approval.response_id,
            at=at,
        )
        return ServiceResult(
            ServiceOutcome.APPROVED,
            self._reload(entry.logical_entry_id),
            approval=approval,
            request_id=entry.current_handoff_id,
        )

    def _expire(self, entry: LogicalEntry, at: dt.datetime) -> ServiceResult:
        """Expire an AWAITING_REVIEW entry whose review outlived its TTL."""
        # Release BEFORE the expiry event: LogicalEntry refuses a terminal
        # state that still holds a reservation, so replay depends on this
        # order -- which is exactly the leak-proofing working as designed.
        self._release_reservation(entry, reason="review expired unanswered", at=at)
        self.store.record_review_expired(
            entry.logical_entry_id,
            revision=entry.proposal_revision,
            handoff_id=entry.current_handoff_id,
            at=at,
        )
        self._withdraw_request(
            entry.current_handoff_id,
            note=f"EXPIRED: logical entry {entry.logical_entry_id} revision "
            f"{entry.proposal_revision}: review expired unanswered",
        )
        return ServiceResult(
            ServiceOutcome.EXPIRED, self._reload(entry.logical_entry_id)
        )

    def _supersede(
        self,
        entry: LogicalEntry,
        packet_now: VerificationPacket,
        at: dt.datetime,
        *,
        reason: str,
    ) -> ServiceResult:
        """Retire the current revision and file exactly one replacement.

        The entry keeps its id; the revision increments; the prior revision's
        approval (if any) is left unconsumed on disk, where the gate's digest
        binding already makes it worthless against the new revision. The
        retired revision's *request* is closed through the gate so the
        reviewer's queue never accumulates questions nothing will act on.
        """
        self.store.record_revision_superseded(
            entry.logical_entry_id,
            revision=entry.proposal_revision,
            handoff_id=entry.current_handoff_id,
            reason=reason,
            at=at,
        )
        self._withdraw_request(
            entry.current_handoff_id,
            note=f"SUPERSEDED: logical entry {entry.logical_entry_id} revision "
            f"{entry.proposal_revision} retired ({reason}); revision "
            f"{entry.proposal_revision + 1} replaces it",
        )
        self._file_revision(
            entry, packet_now, revision=entry.proposal_revision + 1, now=at
        )
        updated = self._reload(entry.logical_entry_id)
        return ServiceResult(
            ServiceOutcome.SUPERSEDED, updated, request_id=updated.current_handoff_id
        )

    def _record_refusal(
        self, entry: LogicalEntry, at: dt.datetime, *, detail: str
    ) -> ServiceResult:
        self.store.record_review_refused(
            entry.logical_entry_id,
            revision=entry.proposal_revision,
            handoff_id=entry.current_handoff_id,
            at=at,
            detail=detail,
        )
        updated = self._reload(entry.logical_entry_id)
        if updated.refusal_count >= self.refusal_policy.terminal_after:
            return self._abandon_terminal_refusals(updated, at)
        return ServiceResult(ServiceOutcome.REFUSED, updated)

    def _abandon_terminal_refusals(
        self, entry: LogicalEntry, at: dt.datetime
    ) -> ServiceResult:
        """Enforce ``terminal_after``: release, abandon, close the request."""
        self._release_reservation(
            entry,
            reason=f"terminal after {entry.refusal_count} refusals",
            at=at,
        )
        self.store.record_abandoned(
            entry.logical_entry_id,
            reason=f"refused {entry.refusal_count} times "
            f"(terminal_after={self.refusal_policy.terminal_after})",
            at=at,
        )
        self._withdraw_request(
            entry.current_handoff_id,
            note=f"ABANDONED: logical entry {entry.logical_entry_id} revision "
            f"{entry.proposal_revision}: refused {entry.refusal_count} times "
            f"(terminal_after={self.refusal_policy.terminal_after})",
        )
        return ServiceResult(
            ServiceOutcome.REFUSED_TERMINAL, self._reload(entry.logical_entry_id)
        )

    def _service_cooldown(
        self, entry: LogicalEntry, packet_now: VerificationPacket, at: dt.datetime
    ) -> ServiceResult:
        # The terminal policy outranks the cooldown clock. A crash between the
        # REVIEW_REFUSED append and the ENTRY_ABANDONED append (they are two
        # separate durable writes in _record_refusal) leaves a zombie: a
        # REFUSED_COOLDOWN entry whose refusal_count already reached
        # terminal_after, still holding its reservation. Without this check
        # the zombie would cool down and refile -- a fourth review of an
        # intention the policy already declared answered.
        if entry.refusal_count >= self.refusal_policy.terminal_after:
            return self._abandon_terminal_refusals(entry, at)
        refused_at = entry.refused_at
        if refused_at is not None and at < refused_at + self.refusal_policy.cooldown:
            return ServiceResult(ServiceOutcome.COOLING, entry)
        if packet_now.spec.digest == entry.current_spec_digest:
            # The gate keys requests by digest: re-filing the identical spec
            # would re-find the identical refusal. The refusal stands until
            # the market gives the reviewer a different question.
            return ServiceResult(ServiceOutcome.REFUSAL_STANDS, entry)
        result = self._supersede(entry, packet_now, at, reason="refiled after cooldown")
        return ServiceResult(
            ServiceOutcome.REFILED, result.entry, request_id=result.request_id
        )

    # -- step 6 and 7: the physical side and abandonment ------------------

    def record_physical_attempt(
        self, entry: LogicalEntry, *, now: dt.datetime | None = None
    ) -> LogicalEntry:
        """One working physical order per entry, enforced by state and lineage.

        Only an APPROVED_PENDING_EXECUTION entry may submit, and an entry with
        an open attempt in its lineage (a PHYSICAL_SUBMITTED with no resolution
        after it) refuses a second one outright.
        """
        at = self._now(now)
        entry = self._reload(entry.logical_entry_id)
        if self._open_physical_attempt(entry):
            _refuse(
                f"logical entry {entry.logical_entry_id} already has a working "
                "physical order",
                hint="one intended position, one working order; resolve the open "
                "attempt before recording another",
            )
        if entry.state is not LogicalEntryState.APPROVED_PENDING_EXECUTION:
            _refuse(
                f"a physical attempt needs an approved entry, this one is "
                f"{entry.state.value}"
            )
        self.store.record_physical_submitted(
            entry.logical_entry_id,
            revision=entry.proposal_revision,
            handoff_id=entry.current_handoff_id,
            at=at,
        )
        return self._reload(entry.logical_entry_id)

    @staticmethod
    def _open_physical_attempt(entry: LogicalEntry) -> bool:
        open_attempt = False
        for record in entry.lineage:
            if record.outcome is RevisionOutcome.PHYSICAL_SUBMITTED:
                open_attempt = True
            elif record.outcome in (
                RevisionOutcome.PHYSICAL_FILLED,
                RevisionOutcome.PHYSICAL_FAILED,
            ):
                open_attempt = False
        return open_attempt

    def record_physical_outcome(
        self,
        entry: LogicalEntry,
        *,
        filled: bool,
        detail: str = "",
        now: dt.datetime | None = None,
    ) -> LogicalEntry:
        """Resolve the working order. A fill completes the entry; a failure
        abandons it (see the named policy in the module docstring)."""
        at = self._now(now)
        entry = self._reload(entry.logical_entry_id)
        if entry.state is not LogicalEntryState.EXECUTING:
            _refuse(
                f"no physical order is working for {entry.logical_entry_id} "
                f"(state {entry.state.value})"
            )
        self._release_reservation(
            entry,
            reason=(
                "converted to a held position" if filled else "transmission failed"
            ),
            at=at,
        )
        self.store.record_physical_resolved(
            entry.logical_entry_id, filled=filled, detail=detail, at=at
        )
        return self._reload(entry.logical_entry_id)

    def abandon(
        self, entry: LogicalEntry, *, reason: str, now: dt.datetime | None = None
    ) -> LogicalEntry:
        at = self._now(now)
        entry = self._reload(entry.logical_entry_id)
        if not entry.is_active:
            _refuse(
                f"logical entry {entry.logical_entry_id} is already terminal "
                f"({entry.state.value})"
            )
        self._release_reservation(entry, reason=f"abandoned: {reason}", at=at)
        self.store.record_abandoned(entry.logical_entry_id, reason=reason, at=at)
        self._withdraw_request(
            entry.current_handoff_id,
            note=f"ABANDONED: logical entry {entry.logical_entry_id} revision "
            f"{entry.proposal_revision}: {reason}",
        )
        return self._reload(entry.logical_entry_id)

    # -- hygiene: the packet-free sweep -----------------------------------

    def sweep(self, *, now: dt.datetime | None = None) -> tuple[ServiceResult, ...]:
        """Restore hygiene over every ACTIVE entry, without needing a packet.

        ``service`` can only judge an entry it is handed a freshly revalidated
        packet for -- so an entry no scan nominates again would otherwise sit
        in its state forever, reservation and all. The sweep applies exactly
        the named policy bounds, and nothing discretionary:

        * AWAITING_REVIEW past its ``review_expires_at`` expires (the same
          transition ``service`` would have made, packet or no packet).
        * CLAIMED older than ``claimed_max_age`` is abandoned.
        * REFUSED_COOLDOWN at ``terminal_after`` refusals is abandoned -- the
          crashed-terminal zombie, ended here as well as in ``service``.
        * REFUSED_COOLDOWN older than ``cooldown_max_age`` is abandoned.

        Every ending releases the reservation and closes the outstanding
        review request (where one exists). Intended for startup and periodic
        hygiene in the runner's wiring; the read-only universe scanner never
        calls it.
        """
        at = self._now(now)
        results: list[ServiceResult] = []
        for entry in self.store.active():
            if entry.state is LogicalEntryState.AWAITING_REVIEW:
                if entry.review_expires_at is not None and at >= entry.review_expires_at:
                    results.append(self._expire(entry, at))
            elif entry.state is LogicalEntryState.CLAIMED:
                if at - entry.created_at >= self.refusal_policy.claimed_max_age:
                    results.append(
                        self._sweep_abandon(
                            entry,
                            at,
                            reason=f"swept: CLAIMED since "
                            f"{entry.created_at.isoformat()} without filing a "
                            f"review (claimed_max_age="
                            f"{self.refusal_policy.claimed_max_age})",
                        )
                    )
            elif entry.state is LogicalEntryState.REFUSED_COOLDOWN:
                if entry.refusal_count >= self.refusal_policy.terminal_after:
                    results.append(self._abandon_terminal_refusals(entry, at))
                    continue
                refused_at = entry.refused_at or entry.updated_at
                if at - refused_at >= self.refusal_policy.cooldown_max_age:
                    results.append(
                        self._sweep_abandon(
                            entry,
                            at,
                            reason=f"swept: REFUSED_COOLDOWN since "
                            f"{refused_at.isoformat()} with no changed spec "
                            f"(cooldown_max_age="
                            f"{self.refusal_policy.cooldown_max_age})",
                        )
                    )
        return tuple(results)

    def _sweep_abandon(
        self, entry: LogicalEntry, at: dt.datetime, *, reason: str
    ) -> ServiceResult:
        self._release_reservation(entry, reason=f"abandoned: {reason}", at=at)
        self.store.record_abandoned(entry.logical_entry_id, reason=reason, at=at)
        self._withdraw_request(
            entry.current_handoff_id,
            note=f"ABANDONED: logical entry {entry.logical_entry_id} revision "
            f"{entry.proposal_revision}: {reason}",
        )
        return ServiceResult(
            ServiceOutcome.ABANDONED, self._reload(entry.logical_entry_id)
        )
