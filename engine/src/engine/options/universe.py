"""The two-phase universe scanner: read-only discovery over the whole seed set.

**READ-ONLY is the contract, enforced by construction.** This module builds
the daily universe, serves what the caches already know, spends a bounded
request budget refreshing what they do not, classifies every symbol into a
named ScanBook state, and *nominates* structures for logical-entry creation.
It does not mint trade intents, does not file verifier handoffs, does not
reserve capital, and cannot transmit: it imports neither
``engine.options.transmit`` nor ``engine.options.approval``, constructs no
``OptionStrategyIntent`` and no strategy id, and
``tests/test_options_universe.py`` pins both facts against the AST. A
nomination is a plain record -- underlying, family, direction, expiration and
legs by conId/strike/right -- that a *later*, separately-gated stage may turn
into an intent behind the chokepoint.

Scheduling, because ninety symbols do not fit a 55-per-600s historical budget:

* Symbols whose IV series is fresh in the :class:`~engine.options.ivstore.IVStore`
  are served **without any broker request** (the SLOW_OBSERVATION contract in
  :mod:`engine.options.freshness`).
* Stale symbols are refreshed in priority order -- highest previous IV Rank /
  percentile first, computed from the stale cache itself -- up to a bounded
  refresh cap, acquiring from the shared :class:`~engine.options.pacing.PacedRequestBudget`
  at ``Priority.DISCOVERY``.
* :class:`~engine.options.pacing.DiscoveryPaced` is **caught, never propagated**:
  a paced symbol is reported ``DEFERRED_PACING`` -- a deferral, not a
  rejection, because pacing is the broker's state and not the symbol's fault
  -- and the pass continues with the cached work that remains.
* Phase 2 (chain -> qualify -> window quotes -> regime -> leg-level liquidity)
  runs only on the strongest bounded subset, and quotes are PERISHABLE: they
  are fetched inside the pass and never cached across passes.

Precedence decisions, stated once so the states mean one thing:

* **Pacing beats staleness.** A stale symbol whose refresh was paced is
  ``DEFERRED_PACING``, not ``OBSERVATION_STALE`` -- the deferral names the
  actual cause, and staleness would blame the symbol for the broker's window.
* **Event risk beats regime.** A symbol flagged for event risk is excluded
  before its volatility tier is consulted; an earnings date does not care how
  rich the premium is.
* **Regime beats liquidity** only in the trivial sense that liquidity is a
  Phase-2 (paid, per-symbol) question and regime is a Phase-1 (free) one; a
  symbol never reaches the liquidity gate unless its regime permits entry.

``CLAIMED_BY_LOGICAL_ENTRY`` and ``SUPERSEDED`` are defined here with their
transition rules but are never produced by the scanner: they belong to the
logical-entry integration, which claims CANDIDATE rows through
:func:`claim_for_logical_entry` and retires them through :func:`supersede`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any

from ..errors import ConfigError, EngineError
from .chain import select_expiration, narrow_strikes
from .domain import OptionRight, OrderAction
from .freshness import (
    FreshnessClass,
    ObservationEnvelope,
    SessionMetadataStore,
    SymbolSessionMetadata,
)
from .ivrank import IVRankMetric, build_iv_rank
from .ivstore import IVStore
from .liquidity import check_liquidity
from .pacing import DiscoveryPaced, PacedRequestBudget, Priority, RequestKind
from .policy import RiskPolicy
from .ports import ContractDataPort, LiveMarketDataPort, VolatilityHistoryPort
from .regime import (
    RegimeDecision,
    VolatilityAssessment,
    VolatilityRegimePolicy,
    classify,
)
from .selection import Bias, candidates_from_snapshot, select_vertical, target_delta_for
from .universe_data import UNIVERSE_VERSION, UniverseEntry

__all__ = [
    "SCANBOOK_VERSION",
    "RANK_VERSION",
    "UNIVERSE_SCAN_VERSION",
    "ScanState",
    "ScanBookAdmission",
    "ObservationProvenance",
    "ScanBookTransitionError",
    "NominatedLeg",
    "StructureNomination",
    "ScanBookRow",
    "CoverageSummary",
    "ScanBook",
    "ScanBookFileWriter",
    "UniverseScanConfig",
    "run_universe_pass",
    "transition",
    "claim_for_logical_entry",
    "supersede",
    "PACING_ERROR_KINDS",
    "penalize_on_broker_error",
    # named reason codes
    "REASON_PACING_DEFERRED",
    "REASON_REFRESH_NOT_REACHED",
    "REASON_REFRESH_FAILED",
    "REASON_NO_HISTORY_PORT",
    "REASON_NO_CONTRACT_PORT",
    "REASON_NO_MARKET_DATA_PORT",
    "REASON_METADATA_UNAVAILABLE",
    "REASON_NO_EXPIRY_IN_WINDOW",
    "REASON_NO_STRUCTURE",
    "REASON_EVENT_RISK",
    "REASON_PHASE2_NOT_REACHED",
]

SCANBOOK_VERSION = "scanbook/1"
RANK_VERSION = "universe-rank/1"
UNIVERSE_SCAN_VERSION = "options-universe-scan/1"

ENV_PREFIX = "IBKR_OPTIONS_UNIVERSE_"

# -- named reason codes ------------------------------------------------------
#
# Regime rejections carry the regime module's own refusal codes and liquidity
# rejections carry the liquidity module's; the codes below name the conditions
# only this scanner can observe.

REASON_PACING_DEFERRED = "UNIVERSE_PACING_DEFERRED"
REASON_REFRESH_NOT_REACHED = "UNIVERSE_REFRESH_NOT_REACHED"
REASON_REFRESH_FAILED = "UNIVERSE_REFRESH_FAILED"
REASON_NO_HISTORY_PORT = "UNIVERSE_NO_HISTORY_PORT"
REASON_NO_CONTRACT_PORT = "UNIVERSE_NO_CONTRACT_PORT"
REASON_NO_MARKET_DATA_PORT = "UNIVERSE_NO_MARKET_DATA_PORT"
REASON_METADATA_UNAVAILABLE = "UNIVERSE_METADATA_UNAVAILABLE"
REASON_NO_EXPIRY_IN_WINDOW = "UNIVERSE_NO_EXPIRY_IN_WINDOW"
REASON_NO_STRUCTURE = "UNIVERSE_NO_STRUCTURE"
REASON_EVENT_RISK = "UNIVERSE_EVENT_RISK"
REASON_PHASE2_NOT_REACHED = "UNIVERSE_PHASE2_NOT_REACHED"

ZERO = Decimal("0")

# -- broker pacing errors ----------------------------------------------------
#
# The budget's local ledger is a prediction; the broker's error stream is the
# verdict. When IBKR says we paced anyway, the budget's ``penalize`` halves the
# refill and pauses discovery -- and this mapping is the one place that says
# which error code penalizes which bucket.

#: IBKR pacing error codes -> the request bucket each one meters. 162 is the
#: historical-data pacing violation (the hard 60-per-600s ceiling); 100 is the
#: general max-messages-per-second cap. Everything else is not a pacing signal.
PACING_ERROR_KINDS: dict[int, RequestKind] = {
    162: RequestKind.HISTORICAL,
    100: RequestKind.GENERAL,
}


def penalize_on_broker_error(
    budget: PacedRequestBudget, code: int
) -> RequestKind | None:
    """Feed one broker error into the pacing budget.

    The injectable hook the CLI's ``options-universe-scan`` handler registers
    on ``ib.errorEvent`` (the same pattern ``scan.run_scan`` uses for its error
    collection): on a pacing code the matching bucket is penalized -- refill
    halved, tokens dropped, discovery paused -- so the *rest of this pass*
    defers instead of digging the hole deeper. Returns the penalized
    :class:`~engine.options.pacing.RequestKind`, or ``None`` when the code is
    not a pacing signal and nothing was penalized.
    """
    try:
        kind = PACING_ERROR_KINDS.get(int(code))
    except (TypeError, ValueError):
        return None
    if kind is None:
        return None
    budget.penalize(kind)
    return kind


class ScanState(str, Enum):
    """Exactly one per ScanBook row. The scanner produces the first eight;
    the last two are set later by the logical-entry integration through the
    transition helpers below."""

    UNSCANNED = "UNSCANNED"
    DEFERRED_PACING = "DEFERRED_PACING"
    METADATA_UNAVAILABLE = "METADATA_UNAVAILABLE"
    OBSERVATION_STALE = "OBSERVATION_STALE"
    INELIGIBLE_REGIME = "INELIGIBLE_REGIME"
    INELIGIBLE_LIQUIDITY = "INELIGIBLE_LIQUIDITY"
    INELIGIBLE_EVENT_RISK = "INELIGIBLE_EVENT_RISK"
    CANDIDATE = "CANDIDATE"
    CLAIMED_BY_LOGICAL_ENTRY = "CLAIMED_BY_LOGICAL_ENTRY"
    SUPERSEDED = "SUPERSEDED"


class ScanBookAdmission(str, Enum):
    """The persisted-book admission result consumed by the runner.

    The scanner may write a book, but only a book for the requested session,
    inside the claim-age window, and not from the future may feed a logical
    entry.  These values are refusal codes as well as a typed boundary: the
    manager path reports them and stops, rather than silently changing input
    sources.
    """

    ACCEPTED = "OK"
    STALE = "SCANBOOK_STALE"
    FUTURE = "SCANBOOK_FUTURE"
    SESSION_MISMATCH = "SCANBOOK_SESSION_MISMATCH"


#: States that count as *rejections* in the coverage summary. DEFERRED_PACING
#: is pointedly absent: a deferral reports the broker's pacing window, not a
#: judgement about the symbol, and folding it into the rejections would make
#: a paced day read as a day the whole universe failed the strategy.
REJECTION_STATES = frozenset(
    {
        ScanState.INELIGIBLE_REGIME,
        ScanState.INELIGIBLE_LIQUIDITY,
        ScanState.INELIGIBLE_EVENT_RISK,
    }
)


class ObservationProvenance(str, Enum):
    """Which cache served a row's slow observations, if any."""

    #: Fresh cache; zero broker requests were spent on this symbol's series.
    CACHE = "CACHE"
    #: Fetched from the broker this pass and written back to the store.
    REFRESHED = "REFRESHED"
    #: Only a stale series existed and no refresh happened this pass.
    STALE_CACHE = "STALE_CACHE"
    #: No observation data at all, cached or otherwise.
    NONE = "NONE"


class ScanBookTransitionError(EngineError):
    """A state change the ScanBook state machine does not define."""


# ---------------------------------------------------------------------------
# nominations: plain records, never intents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NominatedLeg:
    """One leg of a nominated structure, by contract identity only.

    Deliberately *not* an ``OptionLegIntent``: no exchange routing, no trading
    class, no multiplier -- nothing an order needs. Identity and role, so the
    logical-entry stage can re-qualify and re-quote before anything binding is
    built behind its own gates.
    """

    con_id: int
    strike: Decimal
    right: str
    action: str

    def __post_init__(self) -> None:
        if not isinstance(self.con_id, int) or isinstance(self.con_id, bool) or self.con_id <= 0:
            raise ValueError(f"a nominated leg needs a positive con_id, got {self.con_id!r}")
        if not isinstance(self.strike, Decimal) or not self.strike.is_finite() or self.strike <= 0:
            raise ValueError(f"leg {self.con_id}: strike must be a positive Decimal")
        if self.right not in ("P", "C", "PUT", "CALL"):
            raise ValueError(f"leg {self.con_id}: unrecognised right {self.right!r}")
        if self.action not in ("SELL", "BUY"):
            raise ValueError(f"leg {self.con_id}: action must be SELL or BUY")

    def to_record(self) -> dict[str, Any]:
        return {
            "con_id": self.con_id,
            "strike": str(self.strike),
            "right": self.right,
            "action": self.action,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "NominatedLeg":
        return cls(
            con_id=int(record["con_id"]),
            strike=Decimal(str(record["strike"])),
            right=str(record["right"]),
            action=str(record["action"]),
        )


@dataclass(frozen=True)
class StructureNomination:
    """What a CANDIDATE row proposes: a structure, described, not built.

    Carries no strategy id, no quantity, no limit price and no maximum loss --
    all of those are decisions the logical-entry stage makes when (and if) it
    claims the row, behind the authorization gates this module cannot reach.
    """

    underlying: str
    family: str
    direction: str
    expiration: dt.date
    legs: tuple[NominatedLeg, ...]
    short_delta: Decimal
    width: Decimal

    def __post_init__(self) -> None:
        if not self.underlying.strip():
            raise ValueError("a nomination must name its underlying")
        if not self.family.strip() or not self.direction.strip():
            raise ValueError(f"{self.underlying}: family and direction are required")
        if not isinstance(self.legs, tuple) or len(self.legs) < 2:
            raise ValueError(
                f"{self.underlying}: a defined-risk nomination needs at least two legs"
            )
        if len({leg.con_id for leg in self.legs}) != len(self.legs):
            raise ValueError(f"{self.underlying}: nominated legs repeat a con_id")

    def to_record(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "family": self.family,
            "direction": self.direction,
            "expiration": self.expiration.isoformat(),
            "legs": [leg.to_record() for leg in self.legs],
            "short_delta": str(self.short_delta),
            "width": str(self.width),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "StructureNomination":
        return cls(
            underlying=str(record["underlying"]),
            family=str(record["family"]),
            direction=str(record["direction"]),
            expiration=dt.date.fromisoformat(str(record["expiration"])),
            legs=tuple(NominatedLeg.from_record(leg) for leg in record["legs"]),
            short_delta=Decimal(str(record["short_delta"])),
            width=Decimal(str(record["width"])),
        )


# ---------------------------------------------------------------------------
# rows, coverage, book
# ---------------------------------------------------------------------------


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class ScanBookRow:
    """One symbol's outcome for one session, with everything that produced it."""

    symbol: str
    state: ScanState
    sector: str | None = None
    correlation_group: str | None = None
    #: The named code for every non-CANDIDATE state; "" for CANDIDATE and for
    #: an untouched UNSCANNED row.
    reason: str = ""
    detail: str = ""
    observation: ObservationProvenance = ObservationProvenance.NONE
    #: "CACHE" or "FETCHED" once session metadata was consulted; None before.
    metadata_source: str | None = None
    iv_rank: Decimal | None = None
    iv_percentile: Decimal | None = None
    #: The full regime decision record, when a classification was computed.
    regime: dict[str, Any] | None = None
    rank_score: Decimal | None = None
    rank_inputs: dict[str, str] = field(default_factory=dict)
    nomination: StructureNomination | None = None
    evaluated_at: dt.datetime | None = None
    #: Set by claim_for_logical_entry; names the claiming logical entry.
    claim_reference: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("a scan book row must name its symbol")
        if self.evaluated_at is not None and self.evaluated_at.tzinfo is None:
            raise ValueError(f"{self.symbol}: evaluated_at must be timezone-aware")
        if self.state is ScanState.CANDIDATE and self.nomination is None:
            raise ValueError(
                f"{self.symbol}: a CANDIDATE row must carry its nomination"
            )
        if self.state in REJECTION_STATES and not self.reason.strip():
            raise ValueError(
                f"{self.symbol}: state {self.state.value} requires a named reason"
            )

    def to_record(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "state": self.state.value,
            "sector": self.sector,
            "correlation_group": self.correlation_group,
            "reason": self.reason,
            "detail": self.detail,
            "observation": self.observation.value,
            "metadata_source": self.metadata_source,
            "iv_rank": None if self.iv_rank is None else str(self.iv_rank),
            "iv_percentile": (
                None if self.iv_percentile is None else str(self.iv_percentile)
            ),
            "regime": self.regime,
            "rank_score": None if self.rank_score is None else str(self.rank_score),
            "rank_inputs": dict(self.rank_inputs),
            "nomination": None if self.nomination is None else self.nomination.to_record(),
            "evaluated_at": (
                None if self.evaluated_at is None else self.evaluated_at.isoformat()
            ),
            "claim_reference": self.claim_reference,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ScanBookRow":
        raw_nomination = record.get("nomination")
        raw_evaluated = record.get("evaluated_at")
        return cls(
            symbol=str(record["symbol"]),
            state=ScanState(str(record["state"])),
            sector=record.get("sector"),
            correlation_group=record.get("correlation_group"),
            reason=str(record.get("reason", "")),
            detail=str(record.get("detail", "")),
            observation=ObservationProvenance(
                str(record.get("observation", ObservationProvenance.NONE.value))
            ),
            metadata_source=record.get("metadata_source"),
            iv_rank=_optional_decimal(record.get("iv_rank")),
            iv_percentile=_optional_decimal(record.get("iv_percentile")),
            regime=dict(record["regime"]) if isinstance(record.get("regime"), Mapping) else None,
            rank_score=_optional_decimal(record.get("rank_score")),
            rank_inputs={
                str(k): str(v)
                for k, v in (record.get("rank_inputs") or {}).items()
            },
            nomination=(
                StructureNomination.from_record(raw_nomination)
                if isinstance(raw_nomination, Mapping)
                else None
            ),
            evaluated_at=(
                dt.datetime.fromisoformat(str(raw_evaluated))
                if raw_evaluated
                else None
            ),
            claim_reference=record.get("claim_reference"),
        )


# -- transitions -------------------------------------------------------------

#: The only state changes the book defines *after* a pass has written a row.
#: Everything else is a new pass producing a new row, never a mutation.
_ALLOWED_TRANSITIONS: dict[ScanState, frozenset[ScanState]] = {
    ScanState.CANDIDATE: frozenset(
        {ScanState.CLAIMED_BY_LOGICAL_ENTRY, ScanState.SUPERSEDED}
    ),
    ScanState.CLAIMED_BY_LOGICAL_ENTRY: frozenset({ScanState.SUPERSEDED}),
}


def transition(
    row: ScanBookRow, new_state: ScanState, *, reason: str, at: dt.datetime
) -> ScanBookRow:
    """A new row in ``new_state``, if the state machine defines the edge.

    Raises :class:`ScanBookTransitionError` otherwise -- an undefined edge is
    a caller bug, and silently allowing it would let a rejected symbol be
    "claimed" by code that never checked what it was claiming.
    """
    allowed = _ALLOWED_TRANSITIONS.get(row.state, frozenset())
    if new_state not in allowed:
        raise ScanBookTransitionError(
            f"{row.symbol}: no transition {row.state.value} -> {new_state.value}",
            hint="only CANDIDATE rows can be claimed, and only CANDIDATE or "
            "claimed rows can be superseded",
        )
    if not reason.strip():
        raise ScanBookTransitionError(
            f"{row.symbol}: a transition to {new_state.value} must state a reason"
        )
    return replace(row, state=new_state, reason=reason, evaluated_at=at)


def claim_for_logical_entry(
    row: ScanBookRow, *, claimed_by: str, at: dt.datetime
) -> ScanBookRow:
    """CANDIDATE -> CLAIMED_BY_LOGICAL_ENTRY, recording who claimed it."""
    if not claimed_by.strip():
        raise ScanBookTransitionError(
            f"{row.symbol}: a claim must name the claiming logical entry"
        )
    claimed = transition(
        row,
        ScanState.CLAIMED_BY_LOGICAL_ENTRY,
        reason=f"claimed by {claimed_by}",
        at=at,
    )
    return replace(claimed, claim_reference=claimed_by)


def supersede(row: ScanBookRow, *, reason: str, at: dt.datetime) -> ScanBookRow:
    """CANDIDATE or CLAIMED -> SUPERSEDED (a newer book replaced this row)."""
    return transition(row, ScanState.SUPERSEDED, reason=reason, at=at)


class ScanBookFileWriter:
    """The claim-writer seam over one session's persisted whole-file book.

    The logical-entry integration's writer (contract sections 2 and 9.2): the
    scanner itself never calls this. Each mark is read-modify-write over the
    whole frozen book -- ``ScanBook.read``, one row replaced through the
    transition functions above, coverage recomputed, atomic ``write`` -- so
    the compare-and-set is against the state on disk at the instant of the
    call, under the same single-writer deployment assumption the logical-entry
    store states.

    Semantics mirror ``tests/integration_support.py``'s executable reference
    (``RecordingScanBookWriter``): a lost race returns ``False`` (an ordinary
    outcome -- someone else owns the row, or a newer book retired it), an
    idempotent re-claim by the same entry returns ``True`` without a second
    transition, a re-claim by a *different* entry raises (double ownership is
    the invariant, never a race), and an unknown row raises.
    """

    def __init__(self, root: Path, session_date: dt.date) -> None:
        self.root = Path(root)
        self.session_date = session_date

    def _book(self) -> ScanBook:
        book = ScanBook.read(self.root, self.session_date)
        if book is None:
            raise ScanBookTransitionError(
                f"no readable scanbook for {self.session_date.isoformat()} "
                f"under {self.root}",
                hint="a claim against a book that is not on disk records nothing",
            )
        return book

    def _row(self, book: ScanBook, symbol: str) -> ScanBookRow:
        wanted = symbol.strip().upper()
        for row in book.rows:
            if row.symbol.strip().upper() == wanted:
                return row
        raise ScanBookTransitionError(
            f"the scanbook for {self.session_date.isoformat()} has no row "
            f"for {wanted}"
        )

    def _write(self, book: ScanBook, updated: ScanBookRow) -> None:
        rows = tuple(
            updated if row.symbol == updated.symbol else row for row in book.rows
        )
        replace(
            book, rows=rows, coverage=CoverageSummary.from_rows(rows)
        ).write(self.root)

    def mark_claimed(self, symbol: str, *, entry_id: Any, at: dt.datetime) -> bool:
        book = self._book()
        row = self._row(book, symbol)
        if row.state is ScanState.CLAIMED_BY_LOGICAL_ENTRY:
            if (row.claim_reference or "") == str(entry_id):
                return True  # idempotent re-claim by the same entry
            raise ScanBookTransitionError(
                f"{row.symbol} is already claimed by {row.claim_reference!r}; "
                "a second logical entry may not claim it"
            )
        if row.state is not ScanState.CANDIDATE:
            return False
        self._write(
            book, claim_for_logical_entry(row, claimed_by=str(entry_id), at=at)
        )
        return True

    def mark_superseded(self, symbol: str, *, reason: str, at: dt.datetime) -> bool:
        book = self._book()
        row = self._row(book, symbol)
        if row.state not in (
            ScanState.CANDIDATE,
            ScanState.CLAIMED_BY_LOGICAL_ENTRY,
        ):
            return False
        self._write(book, supersede(row, reason=reason, at=at))
        return True


# -- coverage ----------------------------------------------------------------


@dataclass(frozen=True)
class CoverageSummary:
    """The end-of-day accounting: what was looked at, and what happened to it."""

    total: int
    evaluated: int
    cached: int
    refreshed: int
    deferred: int
    stale: int
    unavailable: int
    eligible: int
    #: Named-reason counts over the three INELIGIBLE_* states only. Deferrals
    #: are not in here, by design: deferred is not rejected.
    rejected: dict[str, int]

    @classmethod
    def from_rows(cls, rows: Sequence[ScanBookRow]) -> "CoverageSummary":
        rejected: dict[str, int] = {}
        for row in rows:
            if row.state in REJECTION_STATES:
                rejected[row.reason] = rejected.get(row.reason, 0) + 1
        return cls(
            total=len(rows),
            evaluated=sum(1 for row in rows if row.evaluated_at is not None),
            cached=sum(
                1 for row in rows if row.observation is ObservationProvenance.CACHE
            ),
            refreshed=sum(
                1 for row in rows if row.observation is ObservationProvenance.REFRESHED
            ),
            deferred=sum(1 for row in rows if row.state is ScanState.DEFERRED_PACING),
            stale=sum(1 for row in rows if row.state is ScanState.OBSERVATION_STALE),
            unavailable=sum(
                1 for row in rows if row.state is ScanState.METADATA_UNAVAILABLE
            ),
            eligible=sum(1 for row in rows if row.state is ScanState.CANDIDATE),
            rejected=dict(sorted(rejected.items())),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "evaluated": self.evaluated,
            "cached": self.cached,
            "refreshed": self.refreshed,
            "deferred": self.deferred,
            "stale": self.stale,
            "unavailable": self.unavailable,
            "eligible": self.eligible,
            "rejected": dict(self.rejected),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "CoverageSummary":
        return cls(
            total=int(record["total"]),
            evaluated=int(record["evaluated"]),
            cached=int(record["cached"]),
            refreshed=int(record["refreshed"]),
            deferred=int(record["deferred"]),
            stale=int(record["stale"]),
            unavailable=int(record["unavailable"]),
            eligible=int(record["eligible"]),
            rejected={str(k): int(v) for k, v in (record.get("rejected") or {}).items()},
        )

    def describe(self) -> str:
        head = (
            f"coverage: {self.evaluated}/{self.total} evaluated  "
            f"{self.cached} cached  {self.refreshed} refreshed  "
            f"{self.deferred} deferred  {self.stale} stale  "
            f"{self.unavailable} unavailable  {self.eligible} eligible"
        )
        if not self.rejected:
            return head
        reasons = "  ".join(f"{code} x{count}" for code, count in self.rejected.items())
        return f"{head}\nrejected: {reasons}"


# -- the book ----------------------------------------------------------------


@dataclass(frozen=True)
class ScanBook:
    """One session's rows plus their coverage, persisted as a single JSON file."""

    session_date: dt.date
    generated_at: dt.datetime
    rows: tuple[ScanBookRow, ...]
    coverage: CoverageSummary
    version: str = SCANBOOK_VERSION
    universe_version: str = UNIVERSE_VERSION
    rank_version: str = RANK_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.session_date, dt.date) or isinstance(
            self.session_date, dt.datetime
        ):
            raise ValueError("session_date must be a date")
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if self.version != SCANBOOK_VERSION:
            raise ValueError(
                f"unsupported scanbook version {self.version!r}; expected "
                f"{SCANBOOK_VERSION!r}"
            )
        if self.universe_version != UNIVERSE_VERSION:
            raise ValueError(
                f"unsupported universe version {self.universe_version!r}; expected "
                f"{UNIVERSE_VERSION!r}"
            )
        if self.rank_version != RANK_VERSION:
            raise ValueError(
                f"unsupported rank version {self.rank_version!r}; expected "
                f"{RANK_VERSION!r}"
            )
        if not isinstance(self.rows, tuple):
            raise ValueError("rows must be a tuple")
        symbols = [row.symbol.strip().upper() for row in self.rows]
        if len(symbols) != len(set(symbols)):
            raise ValueError("a scanbook may contain only one row per symbol")

    def admit(
        self,
        *,
        session_date: dt.date,
        now: dt.datetime,
        max_age: dt.timedelta,
    ) -> ScanBookAdmission:
        """Admit this persisted book for a claim pass.

        ``generated_at`` is the freshness witness for the whole book.  Row
        ``evaluated_at`` remains the nomination-age witness used by the
        manager at claim time.  A future-dated book is refused explicitly so
        a clock or persistence error cannot manufacture freshness.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if not isinstance(max_age, dt.timedelta) or max_age <= dt.timedelta(0):
            raise ValueError("max_age must be a positive timedelta")
        if self.session_date != session_date:
            return ScanBookAdmission.SESSION_MISMATCH
        age = now - self.generated_at
        if age < dt.timedelta(0):
            return ScanBookAdmission.FUTURE
        if age > max_age:
            return ScanBookAdmission.STALE
        return ScanBookAdmission.ACCEPTED

    @staticmethod
    def path_for(root: Path, session_date: dt.date) -> Path:
        return root / "universe" / f"scanbook-{session_date.isoformat()}.json"

    def candidates(self) -> tuple[ScanBookRow, ...]:
        ranked = [row for row in self.rows if row.state is ScanState.CANDIDATE]
        ranked.sort(key=lambda row: (-(row.rank_score or ZERO), row.symbol))
        return tuple(ranked)

    def to_record(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "universe_version": self.universe_version,
            "rank_version": self.rank_version,
            "session_date": self.session_date.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "coverage": self.coverage.to_record(),
            "rows": [row.to_record() for row in self.rows],
        }

    def write(self, root: Path) -> Path:
        """Atomic whole-file write: temp + ``os.replace``, like every store."""
        path = self.path_for(root, self.session_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.stem}-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self.to_record(), stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        return path

    @classmethod
    def read(cls, root: Path, session_date: dt.date) -> "ScanBook | None":
        """The persisted book for a session, or ``None`` when absent/unreadable."""
        path = cls.path_for(root, session_date)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict):
            return None
        try:
            return cls(
                session_date=dt.date.fromisoformat(str(record["session_date"])),
                generated_at=dt.datetime.fromisoformat(str(record["generated_at"])),
                rows=tuple(
                    ScanBookRow.from_record(row) for row in record.get("rows", [])
                ),
                coverage=CoverageSummary.from_record(record["coverage"]),
                version=str(record["version"]),
                universe_version=str(record["universe_version"]),
                rank_version=str(record["rank_version"]),
            )
        except (AttributeError, KeyError, ValueError, TypeError):
            return None

    def describe(self, *, top: int = 10) -> str:
        lines = [
            f"UNIVERSE SCAN    {self.session_date.isoformat()}  "
            f"[{self.version} | {self.universe_version} | {self.rank_version}]",
            self.coverage.describe(),
        ]
        nominated = self.candidates()
        if nominated:
            lines.append("")
            lines.append(f"TOP CANDIDATES   ({min(top, len(nominated))} of {len(nominated)})")
            for row in nominated[:top]:
                nomination = row.nomination
                structure = (
                    f"{nomination.family} {nomination.direction} "
                    f"{nomination.expiration.isoformat()} "
                    + "/".join(str(leg.strike) for leg in nomination.legs)
                    if nomination
                    else "?"
                )
                lines.append(
                    f"  {row.symbol:<6} score {row.rank_score}  "
                    f"IVR {row.iv_rank}  pct {row.iv_percentile}  {structure}"
                )
        else:
            lines.append("")
            lines.append("TOP CANDIDATES   none this pass")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def _refuse(message: str, *, hint: str | None = None) -> None:
    raise ConfigError(message, hint=hint)


def _env_int(source: Mapping[str, str], key: str, default: int) -> int:
    raw = (source.get(f"{ENV_PREFIX}{key}") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{ENV_PREFIX}{key}={raw!r} is not a whole number") from None


@dataclass(frozen=True)
class UniverseScanConfig:
    """Bounds for one scheduling pass. Frozen, validated, versioned."""

    #: How many stale symbols one pass may refresh. Default 100 (the whole
    #: seed set fits, budget permitting); hard ceiling 200 -- past that a
    #: single pass is guaranteed to spend more than three full pacing windows.
    refresh_limit: int = 100
    #: How many top-ranked symbols get the paid Phase-2 treatment per pass.
    phase2_limit: int = 5
    target_dte: int = 45
    minimum_dte: int = 35
    maximum_dte: int = 55
    #: Strikes to enumerate around the ladder anchor before qualification.
    strike_window: int = 24
    version: str = UNIVERSE_SCAN_VERSION

    def __post_init__(self) -> None:
        for label, ceiling in (("refresh_limit", 200), ("phase2_limit", 100)):
            value = getattr(self, label)
            if not isinstance(value, int) or isinstance(value, bool):
                _refuse(f"{label} must be an int, got {type(value).__name__}")
            if value <= 0:
                _refuse(
                    f"{label} must be positive, got {value}",
                    hint="a bound of zero would scan nothing while reading as "
                    "'the market offered nothing'",
                )
            if value > ceiling:
                _refuse(
                    f"{label} must not exceed {ceiling}, got {value}",
                    hint="the bound exists to keep one pass inside the broker's "
                    "pacing windows",
                )
        for label in ("target_dte", "minimum_dte", "maximum_dte", "strike_window"):
            value = getattr(self, label)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                _refuse(f"{label} must be a positive int, got {value!r}")
        if not self.minimum_dte <= self.target_dte <= self.maximum_dte:
            _refuse(
                f"the DTE window [{self.minimum_dte}, {self.maximum_dte}] does not "
                f"contain the target {self.target_dte}"
            )
        if not isinstance(self.version, str) or not self.version.strip():
            _refuse("version must be a non-empty string")

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, **overrides: object
    ) -> "UniverseScanConfig":
        source: Mapping[str, str] = os.environ if env is None else env
        values: dict[str, object] = {
            "refresh_limit": _env_int(source, "REFRESH_LIMIT", 100),
            "phase2_limit": _env_int(source, "PHASE2_LIMIT", 5),
            "target_dte": _env_int(source, "TARGET_DTE", 45),
            "minimum_dte": _env_int(source, "MINIMUM_DTE", 35),
            "maximum_dte": _env_int(source, "MAXIMUM_DTE", 55),
            "strike_window": _env_int(source, "STRIKE_WINDOW", 24),
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ranking (v1: deterministic Decimal; the M4b factor model replaces this
# under a new version string, never silently)
# ---------------------------------------------------------------------------

_RANK_WEIGHT_IVR = Decimal("0.7")
_RANK_WEIGHT_PERCENTILE = Decimal("0.3")
_RANK_DEGRADATION_PENALTY = Decimal("5")
_RANK_QUANTUM = Decimal("0.01")


def _rank(metric: IVRankMetric, decision: RegimeDecision) -> tuple[Decimal | None, dict[str, str]]:
    """The v1 score and every input that produced it, for the row.

    Higher IV Rank and percentile rank first; each missing auxiliary input
    (percentile, IV/RV edge) subtracts a flat degradation penalty so a symbol
    with complete data outranks an equal one with holes. ``None`` when the
    rank itself is unusable -- an unrankable symbol is not a low-ranked one.
    """
    inputs: dict[str, str] = {
        "version": RANK_VERSION,
        "iv_rank": "" if metric.iv_rank is None else str(metric.iv_rank),
        "iv_percentile": (
            "" if metric.iv_percentile is None else str(metric.iv_percentile)
        ),
        "regime": decision.regime.value,
        "allocation": str(decision.allocation),
    }
    if metric.iv_rank is None:
        inputs["degraded"] = metric.degraded_reason or "iv_rank unavailable"
        return None, inputs
    penalties: list[str] = []
    percentile = metric.iv_percentile
    if percentile is None:
        percentile = metric.iv_rank
        penalties.append("iv_percentile missing")
    if decision.assessment.iv_rv_ratio is None:
        penalties.append("iv_rv_ratio missing")
    score = (
        _RANK_WEIGHT_IVR * metric.iv_rank
        + _RANK_WEIGHT_PERCENTILE * percentile
        - _RANK_DEGRADATION_PENALTY * len(penalties)
    ).quantize(_RANK_QUANTUM)
    if penalties:
        inputs["penalties"] = "; ".join(penalties)
    inputs["score"] = str(score)
    return score, inputs


# ---------------------------------------------------------------------------
# leg-level liquidity probe: check_liquidity without an intent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ProbeLeg:
    """The four fields :func:`~engine.options.liquidity.check_liquidity`
    reads off a leg. Not an ``OptionLegIntent`` and never becomes one."""

    con_id: int
    multiplier: int
    action: OrderAction
    ratio: int = 1


@dataclass(frozen=True)
class _StructureProbe:
    """The one field ``check_liquidity`` reads off a structure: its legs."""

    legs: tuple[_ProbeLeg, ...]


# ---------------------------------------------------------------------------
# the pass
# ---------------------------------------------------------------------------


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def run_universe_pass(
    *,
    universe: Sequence[UniverseEntry],
    session_date: dt.date,
    iv_store: IVStore,
    metadata_store: SessionMetadataStore,
    budget: PacedRequestBudget,
    policy: RiskPolicy,
    regime_policy: VolatilityRegimePolicy,
    config: UniverseScanConfig,
    volatility_history: VolatilityHistoryPort | None = None,
    contract_data: ContractDataPort | None = None,
    market_data: LiveMarketDataPort | None = None,
    event_risk: Callable[[str], str | None] | None = None,
    now: dt.datetime | None = None,
) -> ScanBook:
    """One read-only scheduling pass over the universe.

    Ports default to ``None`` and ``None`` fails closed with a named reason,
    exactly like :func:`engine.options.scan.run_scan`: a pass with no ports is
    a complete pass whose rows say precisely which capability was absent.

    ``event_risk`` is an injected source because nothing in the engine
    computes event risk yet: given a symbol it returns a named flag (e.g. an
    earnings marker) or ``None``. The default of no source excludes nothing
    -- absence of an event calendar is recorded per-row as an unflagged pass,
    never invented into a veto.
    """
    when = now if now is not None else _utcnow()
    rows: dict[str, ScanBookRow] = {}
    by_symbol: dict[str, UniverseEntry] = {}
    for entry in universe:
        by_symbol[entry.symbol] = entry
        rows[entry.symbol] = ScanBookRow(
            symbol=entry.symbol,
            state=ScanState.UNSCANNED,
            sector=entry.sector,
            correlation_group=entry.correlation_group,
        )

    # -- phase 1a: partition by cache freshness -----------------------------
    fresh: list[str] = []
    stale: list[tuple[str, IVRankMetric | None]] = []
    for symbol in by_symbol:
        if iv_store.fresh(symbol, today=session_date, now=when):
            fresh.append(symbol)
        else:
            cached = iv_store.read(symbol)
            previous = (
                build_iv_rank(symbol, cached.observations, calculated_at=when)
                if cached.observations
                else None
            )
            stale.append((symbol, previous))

    # Stale symbols are refreshed highest-previous-potential first: a symbol
    # whose stale cache already showed a rich rank is the one most likely to
    # be tradeable today, so it gets the budget before the quiet ones.
    def _stale_key(item: tuple[str, IVRankMetric | None]) -> tuple[int, Decimal, str]:
        symbol, previous = item
        ivr = previous.iv_rank if previous is not None else None
        if ivr is None:
            return (1, ZERO, symbol)
        percentile = previous.iv_percentile if previous.iv_percentile is not None else ZERO
        return (0, -(ivr + percentile / Decimal("1000")), symbol)

    stale.sort(key=_stale_key)

    # -- phase 1b: serve fresh from cache (zero broker requests) ------------
    metrics: dict[str, tuple[IVRankMetric, ObservationProvenance]] = {}
    for symbol in fresh:
        cached = iv_store.read(symbol)
        metrics[symbol] = (
            build_iv_rank(symbol, cached.observations, calculated_at=when),
            ObservationProvenance.CACHE,
        )

    # -- phase 1c: bounded, budget-paced refresh of the stale ---------------
    refresh_attempts = 0
    for symbol, previous in stale:
        previous_inputs = {
            "previous_iv_rank": (
                "" if previous is None or previous.iv_rank is None else str(previous.iv_rank)
            )
        }
        stale_provenance = (
            ObservationProvenance.STALE_CACHE
            if previous is not None
            else ObservationProvenance.NONE
        )
        if volatility_history is None:
            rows[symbol] = replace(
                rows[symbol],
                state=ScanState.OBSERVATION_STALE,
                reason=REASON_NO_HISTORY_PORT,
                detail="no volatility-history port was supplied, so a stale "
                "series cannot be refreshed",
                observation=stale_provenance,
                rank_inputs=previous_inputs,
            )
            continue
        if refresh_attempts >= config.refresh_limit:
            rows[symbol] = replace(
                rows[symbol],
                state=ScanState.OBSERVATION_STALE,
                reason=REASON_REFRESH_NOT_REACHED,
                detail=f"the refresh bound of {config.refresh_limit} was reached "
                "before this symbol's turn; it is first in line next pass",
                observation=stale_provenance,
                rank_inputs=previous_inputs,
            )
            continue
        try:
            # One general token (underlying qualification) plus one historical
            # token (the bars themselves), both at DISCOVERY: the priority that
            # is *refused* -- not queued -- while a pacing penalty is in force.
            budget.acquire(RequestKind.GENERAL, priority=Priority.DISCOVERY)
            budget.acquire(RequestKind.HISTORICAL, priority=Priority.DISCOVERY)
        except DiscoveryPaced as exc:
            # A deferral, never a rejection, and never a crash: the broker's
            # pacing window is not the symbol's fault, and the pass continues
            # with whatever cached work remains.
            rows[symbol] = replace(
                rows[symbol],
                state=ScanState.DEFERRED_PACING,
                reason=REASON_PACING_DEFERRED,
                detail=str(exc),
                observation=stale_provenance,
                rank_inputs=previous_inputs,
            )
            continue
        refresh_attempts += 1
        try:
            observations = tuple(
                volatility_history.implied_volatility_history(symbol)
            )
        # Adapter boundary: whatever ib_async raises, the outcome is a row
        # that says the refresh failed, never a crashed pass.
        except Exception as exc:  # noqa: BLE001
            rows[symbol] = replace(
                rows[symbol],
                state=ScanState.OBSERVATION_STALE,
                reason=REASON_REFRESH_FAILED,
                detail=f"refresh failed: {type(exc).__name__}: {exc}",
                observation=stale_provenance,
                rank_inputs=previous_inputs,
            )
            continue
        if observations:
            try:
                iv_store.write(symbol, list(observations), fetched_at=when)
            except Exception:  # noqa: BLE001 - a cache write failure degrades, never aborts
                pass
        metrics[symbol] = (
            build_iv_rank(symbol, observations, calculated_at=when),
            ObservationProvenance.REFRESHED,
        )

    # -- phase 1d: event risk, regime, rank ---------------------------------
    ranked: list[tuple[Decimal, str, RegimeDecision]] = []
    for symbol in sorted(metrics):
        metric, provenance = metrics[symbol]
        flag = event_risk(symbol) if event_risk is not None else None
        if flag is not None:
            rows[symbol] = replace(
                rows[symbol],
                state=ScanState.INELIGIBLE_EVENT_RISK,
                reason=REASON_EVENT_RISK,
                detail=f"event risk flagged: {flag}",
                observation=provenance,
                iv_rank=metric.iv_rank,
                iv_percentile=metric.iv_percentile,
                evaluated_at=when,
            )
            continue
        assessment = VolatilityAssessment(
            symbol=symbol,
            iv_rank=metric.iv_rank,
            iv_percentile=metric.iv_percentile,
            current_iv=metric.current_iv,
            event_risk=flag,
        )
        decision = classify(assessment, regime_policy)
        score, rank_inputs = _rank(metric, decision)
        base = replace(
            rows[symbol],
            observation=provenance,
            iv_rank=metric.iv_rank,
            iv_percentile=metric.iv_percentile,
            regime=decision.to_record(),
            rank_score=score,
            rank_inputs=rank_inputs,
            evaluated_at=when,
        )
        if not decision.permits_entry:
            rows[symbol] = replace(
                base,
                state=ScanState.INELIGIBLE_REGIME,
                reason=decision.refusal_code,
                detail="; ".join(decision.reasons),
            )
            continue
        rows[symbol] = replace(
            base,
            state=ScanState.UNSCANNED,
            reason=REASON_PHASE2_NOT_REACHED,
            detail="regime permits entry; awaiting a phase-2 slot",
        )
        if score is not None:
            ranked.append((score, symbol, decision))

    # -- phase 2: the strongest bounded subset ------------------------------
    ranked.sort(key=lambda item: (-item[0], item[1]))
    for score, symbol, decision in ranked[: config.phase2_limit]:
        rows[symbol] = _phase_two(
            rows[symbol],
            decision=decision,
            session_date=session_date,
            now=when,
            metadata_store=metadata_store,
            budget=budget,
            policy=policy,
            config=config,
            contract_data=contract_data,
            market_data=market_data,
            entry=by_symbol[symbol],
        )

    ordered = tuple(rows[entry.symbol] for entry in universe)
    return ScanBook(
        session_date=session_date,
        generated_at=when,
        rows=ordered,
        coverage=CoverageSummary.from_rows(ordered),
    )


# ---------------------------------------------------------------------------
# phase 2
# ---------------------------------------------------------------------------


def _phase_two(
    row: ScanBookRow,
    *,
    decision: RegimeDecision,
    session_date: dt.date,
    now: dt.datetime,
    metadata_store: SessionMetadataStore,
    budget: PacedRequestBudget,
    policy: RiskPolicy,
    config: UniverseScanConfig,
    contract_data: ContractDataPort | None,
    market_data: LiveMarketDataPort | None,
    entry: UniverseEntry,
) -> ScanBookRow:
    """Chain, qualification, window quotes, and leg-level liquidity for one
    top-ranked symbol. Returns the finished row; never raises for a broker or
    pacing condition."""
    symbol = row.symbol

    # -- session metadata: cached for the session, fetched at most once -----
    metadata = metadata_store.read(symbol, session_date=session_date, now=now)
    if metadata is not None:
        expirations: Sequence[str] = metadata.expirations
        metadata_source = "CACHE"
    else:
        if contract_data is None:
            return replace(
                row,
                state=ScanState.METADATA_UNAVAILABLE,
                reason=REASON_NO_CONTRACT_PORT,
                detail="no contract-data port was supplied",
            )
        try:
            budget.acquire(RequestKind.GENERAL, priority=Priority.DISCOVERY)
            expirations = contract_data.expirations(symbol)
        except DiscoveryPaced as exc:
            return replace(
                row,
                state=ScanState.DEFERRED_PACING,
                reason=REASON_PACING_DEFERRED,
                detail=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            return replace(
                row,
                state=ScanState.METADATA_UNAVAILABLE,
                reason=REASON_METADATA_UNAVAILABLE,
                detail=f"expiration discovery failed: {type(exc).__name__}: {exc}",
            )
        if not expirations:
            return replace(
                row,
                state=ScanState.METADATA_UNAVAILABLE,
                reason=REASON_METADATA_UNAVAILABLE,
                detail="the broker listed no expirations for this underlying",
            )
        metadata_source = "FETCHED"
        _cache_metadata(
            metadata_store,
            symbol=symbol,
            expirations=tuple(str(e) for e in expirations),
            entry=entry,
            contract_data=contract_data,
            session_date=session_date,
            now=now,
        )
    row = replace(row, metadata_source=metadata_source)

    # -- expiry: the regime's preferred window when it states one -----------
    if decision.preferred_dte is not None:
        minimum_dte, maximum_dte = decision.preferred_dte
        target_dte = (minimum_dte + maximum_dte) // 2
    else:
        minimum_dte, maximum_dte = config.minimum_dte, config.maximum_dte
        target_dte = config.target_dte
    expiry = select_expiration(
        list(expirations),
        today=session_date,
        target_dte=target_dte,
        minimum_dte=minimum_dte,
        maximum_dte=maximum_dte,
    )
    if expiry is None:
        return replace(
            row,
            state=ScanState.INELIGIBLE_LIQUIDITY,
            reason=REASON_NO_EXPIRY_IN_WINDOW,
            detail=f"no expiration between {minimum_dte} and {maximum_dte} DTE",
        )

    if contract_data is None:
        return replace(
            row,
            state=ScanState.METADATA_UNAVAILABLE,
            reason=REASON_NO_CONTRACT_PORT,
            detail="no contract-data port was supplied for strike enumeration",
        )

    # v1 nominates defined-risk put verticals: the directional-credit shape
    # every permitted family shares. The condor/call variants arrive with the
    # logical-entry stage, which owns direction selection.
    bias = Bias.BULLISH
    right = OptionRight.PUT

    try:
        budget.acquire(RequestKind.GENERAL, priority=Priority.DISCOVERY)
        listed = contract_data.strikes(symbol, expiry.expiry, right.value)
        window = narrow_strikes(
            list(listed),
            reference_price=None,
            width=config.strike_window,
            right=right.value,
        )
        budget.acquire(RequestKind.GENERAL, priority=Priority.DISCOVERY)
        qualified = list(
            contract_data.qualify(symbol, expiry.expiry, window, right.value)
        )
    except DiscoveryPaced as exc:
        return replace(
            row,
            state=ScanState.DEFERRED_PACING,
            reason=REASON_PACING_DEFERRED,
            detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - adapter boundary
        return replace(
            row,
            state=ScanState.METADATA_UNAVAILABLE,
            reason=REASON_METADATA_UNAVAILABLE,
            detail=f"strike enumeration failed: {type(exc).__name__}: {exc}",
        )
    if not qualified:
        return replace(
            row,
            state=ScanState.INELIGIBLE_LIQUIDITY,
            reason=REASON_NO_STRUCTURE,
            detail="no contract in the strike window qualified",
        )

    # -- window quotes: PERISHABLE, fetched in-pass, never cached -----------
    if market_data is None:
        return replace(
            row,
            state=ScanState.INELIGIBLE_LIQUIDITY,
            reason=REASON_NO_MARKET_DATA_PORT,
            detail="no live market-data port was supplied, so depth and spread "
            "cannot be established (unmeasured counts as insufficient)",
        )
    con_ids = [contract.con_id for contract in qualified]
    try:
        # Candidate-construction priority: this symbol has already earned its
        # phase-2 slot, so its quotes queue behind management but ahead of
        # broad discovery, and a pacing penalty slows rather than refuses.
        for _ in range(1 + len(con_ids)):
            budget.acquire(
                RequestKind.GENERAL, priority=Priority.CANDIDATE_CONSTRUCTION
            )
        snapshot = market_data.strategy_quotes(
            underlying_symbol=symbol, con_ids=con_ids
        )
    except DiscoveryPaced as exc:
        return replace(
            row,
            state=ScanState.DEFERRED_PACING,
            reason=REASON_PACING_DEFERRED,
            detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - adapter boundary
        return replace(
            row,
            state=ScanState.INELIGIBLE_LIQUIDITY,
            reason="OPTIONS_LIQUIDITY_UNMEASURABLE",
            detail=f"window quotes unavailable: {type(exc).__name__}: {exc}",
        )

    candidates = candidates_from_snapshot(tuple(qualified), snapshot)
    selection = select_vertical(
        candidates,
        target_delta=target_delta_for(bias, policy),
        right=right,
        target_width=policy.target_width,
    )
    if selection is None:
        return replace(
            row,
            state=ScanState.INELIGIBLE_LIQUIDITY,
            reason=REASON_NO_STRUCTURE,
            detail="no delta-selectable defined-risk vertical exists in the "
            "quoted window",
        )

    # Leg-level liquidity on the *chosen* legs, without building an intent:
    # check_liquidity reads only legs (con_id, multiplier, action, ratio), so
    # a probe carrying exactly those fields is the honest way to ask the
    # question this early.
    probe = _StructureProbe(
        legs=(
            _ProbeLeg(
                con_id=selection.short.con_id,
                multiplier=selection.short.multiplier,
                action=OrderAction.SELL,
            ),
            _ProbeLeg(
                con_id=selection.long.con_id,
                multiplier=selection.long.multiplier,
                action=OrderAction.BUY,
            ),
        )
    )
    verdict = check_liquidity(
        probe,  # type: ignore[arg-type] - duck-typed: only .legs is read
        quotes=snapshot,
        policy=policy,
        quoted_window=len(snapshot.legs),
    )
    if not verdict.approved:
        code = getattr(verdict.reason, "value", None) or str(verdict.reason)
        return replace(
            row,
            state=ScanState.INELIGIBLE_LIQUIDITY,
            reason=str(code),
            detail=verdict.detail,
        )

    nomination = StructureNomination(
        underlying=symbol,
        family=decision.permitted_families[0].value,
        direction=bias.value,
        expiration=selection.short.expiration,
        legs=(
            NominatedLeg(
                con_id=selection.short.con_id,
                strike=selection.short.strike,
                right=str(selection.short.right),
                action=OrderAction.SELL.value,
            ),
            NominatedLeg(
                con_id=selection.long.con_id,
                strike=selection.long.strike,
                right=str(selection.long.right),
                action=OrderAction.BUY.value,
            ),
        ),
        short_delta=selection.short_delta,
        width=selection.width,
    )
    return replace(
        row,
        state=ScanState.CANDIDATE,
        reason="",
        detail=f"nominated {nomination.family} {nomination.direction} "
        f"{selection.describe()}",
        nomination=nomination,
        evaluated_at=now,
    )


def _cache_metadata(
    store: SessionMetadataStore,
    *,
    symbol: str,
    expirations: tuple[str, ...],
    entry: UniverseEntry,
    contract_data: ContractDataPort,
    session_date: dt.date,
    now: dt.datetime,
) -> None:
    """Best-effort SESSION_METADATA write. A failed cache write degrades the
    next read to a re-fetch; it never fails the pass."""
    con_id = 0
    reader = getattr(contract_data, "underlying_con_id", None)
    if callable(reader):
        try:
            con_id = int(reader(symbol))
        except Exception:  # noqa: BLE001 - identity is optional in the cache
            con_id = 0
    envelope = ObservationEnvelope(
        symbol=symbol,
        session_date=session_date,
        observed_at=now,
        expires_at=now + dt.timedelta(days=1),
        source="IBKR:reqSecDefOptParams",
        freshness_class=FreshnessClass.SESSION_METADATA,
        configuration_version=UNIVERSE_SCAN_VERSION,
    )
    metadata = SymbolSessionMetadata(
        envelope=envelope,
        con_id=con_id,
        expirations=expirations,
        # The record describes the *underlying*, whose share multiplier is 1;
        # option-contract multipliers come from qualification, never from here.
        multiplier=1,
        standard=True,
        sector=entry.sector,
        correlation_group=entry.correlation_group,
    )
    try:
        store.write(metadata)
    except Exception:  # noqa: BLE001 - a cache miss next pass, not an outage
        pass


def merged_universe(
    universe: Sequence[UniverseEntry], extra_symbols: Iterable[str]
) -> tuple[UniverseEntry, ...]:
    """Re-export of :func:`engine.options.universe_data.augment` under the
    scanner's own name, so callers wiring a pass need one import."""
    from .universe_data import augment  # noqa: PLC0415 - trivial delegation

    return augment(universe, extra_symbols)
