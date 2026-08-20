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
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..errors import ConfigError, EngineError
from .chain import select_expiration, narrow_strikes
from .domain import OptionRight, OrderAction
from .freshness import (
    FreshnessClass,
    ObservationEnvelope,
    SessionMetadataStore,
    SymbolSessionMetadata,
)
from .ivrank import IVObservation, IVRankMetric, build_iv_rank
from .ivstore import CachedSeries, IVStore
from .liquidity import check_liquidity
from .pacing import DiscoveryPaced, PacedRequestBudget, Priority, RequestKind
from .pacing_ledger import PacingLedger
from .policy import RiskPolicy
from .ports import (
    ContractDataPort,
    LiveMarketDataPort,
    PriceHistoryPort,
    VolatilityHistoryPort,
)
from .realized_vol import PriceObservation, RealizedVolMetric, build_realized_vol
from .rvstore import CachedPriceSeries, RVStore
from .regime import (
    RegimeDecision,
    VolatilityAssessment,
    VolatilityRegimePolicy,
    classify,
)
from .selection import Bias, candidates_from_snapshot, select_vertical, target_delta_for
from .catalog import CatalogEntry, CatalogSnapshot, UniverseCatalog
from .observation_cache import (
    FairRefreshQueue,
    ObservationCache,
    RawObservation,
)
from .scan_receipts import ScanReceiptStore
from .scanbook_store import (
    ObservationAges,
    PhaseCoverage,
    ScanBookSnapshot,
    ScanBookSnapshotStore,
)
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
    "IntegratedScanResult",
    "UniverseScanRecoveryRequired",
    "run_catalog_universe_pass",
    "run_integrated_universe_pass",
    "ObservationCacheIVStore",
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
    "REASON_CATALOG_ENTRY_INELIGIBLE",
]

SCANBOOK_VERSION = "scanbook/1"
RANK_VERSION = "universe-rank/1"
UNIVERSE_SCAN_VERSION = "options-universe-scan/1"
# Cold phase-two work spends one request on expirations, one on strikes, one
# on qualification, then one underlying quote plus two option-leg quotes.
# The quote fan-out is capped below so this estimate is a hard broker-load
# bound rather than an optimistic accounting label.
MIN_PHASE2_REQUEST_COST = 6

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
REASON_CATALOG_ENTRY_INELIGIBLE = "UNIVERSE_CATALOG_ENTRY_INELIGIBLE"

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
    #: Conservative upper bound for one deep-probe reservation.  The
    #: integrated worker reserves this amount before it touches expirations,
    #: strikes, qualification, or live quotes.  A smaller actual request count
    #: releases the unused estimate at completion.
    phase2_request_cost: int = MIN_PHASE2_REQUEST_COST
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
        if (
            not isinstance(self.phase2_request_cost, int)
            or isinstance(self.phase2_request_cost, bool)
            or self.phase2_request_cost <= 0
            or self.phase2_request_cost > 100
        ):
            _refuse(
                "phase2_request_cost must be a positive int no greater than 100",
                hint="deep probing must reserve a finite broker-load estimate",
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

    @property
    def effective_phase2_request_cost(self) -> int:
        """The reservation floor for the actual phase-two request path.

        A valid vertical probe can issue two chain requests and at least three
        quote requests (the underlying plus the two legs).  Older callers may
        still construct the compatibility value ``4``; the worker never
        reserves below the actual minimum.
        """

        return max(self.phase2_request_cost, MIN_PHASE2_REQUEST_COST)

    def to_record(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "refresh_limit": self.refresh_limit,
            "phase2_limit": self.phase2_limit,
            "target_dte": self.target_dte,
            "minimum_dte": self.minimum_dte,
            "maximum_dte": self.maximum_dte,
            "strike_window": self.strike_window,
            "phase2_request_cost": self.effective_phase2_request_cost,
        }

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
            "phase2_request_cost": _env_int(
                source, "PHASE2_REQUEST_COST", MIN_PHASE2_REQUEST_COST
            ),
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


def _previous_weekday(value: dt.date) -> dt.date:
    candidate = value - dt.timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= dt.timedelta(days=1)
    return candidate


class ObservationCacheIVStore:
    """IVStore-shaped adapter over the durable raw-observation cache.

    The legacy scanner consumes :class:`IVStore`; the unattended worker must
    consume one batch/indexed cache shared by every phase.  This adapter keeps
    that old scanner contract local to this module, stores only raw IV input,
    and never caches a derived IV Rank conclusion.
    """

    def __init__(
        self,
        cache: ObservationCache,
        *,
        session_date: dt.date,
        catalog_version: str,
        configuration_version: str,
        now: dt.datetime,
        ttl: dt.timedelta = dt.timedelta(hours=20),
    ) -> None:
        if now.tzinfo is None:
            raise ValueError("cache adapter now must be timezone-aware")
        if ttl <= dt.timedelta(0):
            raise ValueError("cache adapter ttl must be positive")
        self.cache = cache
        self.session_date = session_date
        self.catalog_version = catalog_version
        self.configuration_version = configuration_version
        self.now = now.astimezone(dt.timezone.utc)
        self.ttl = ttl
        self._records: dict[str, tuple[RawObservation, ...]] = {}

    def prefetch(
        self,
        symbols: Iterable[str],
        *,
        records: Mapping[str, Sequence[RawObservation]] | None = None,
    ) -> None:
        """``records``, when supplied, is an already-fetched batch (typically
        shared with :class:`ObservationCacheRVStore` so the two adapters
        spend one ``read_many`` between them, not one each) -- this reuses it
        instead of hitting the cache again."""
        wanted = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols))
        if not wanted:
            return
        if records is None:
            records = self.cache.read_many(
                wanted,
                now=self.now,
                session_date=self.session_date,
                catalog_version=self.catalog_version,
                configuration_version=self.configuration_version,
                include_expired=True,
            )
        self._records.update(
            {
                symbol: tuple(
                    observation
                    for observation in records.get(symbol, ())
                    if observation.key in {"iv-history", "iv", "realized-volatility"}
                )
                for symbol in wanted
            }
        )

    @staticmethod
    def _decode(observations: Iterable[RawObservation]) -> tuple[IVObservation, ...]:
        decoded: list[IVObservation] = []
        for observation in observations:
            payload = observation.payload
            raw_series: Any = payload.get("observations", payload.get("series"))
            if raw_series is None and "on" in payload and "iv" in payload:
                raw_series = [payload]
            if not isinstance(raw_series, (list, tuple)):
                continue
            for raw in raw_series:
                if not isinstance(raw, Mapping):
                    continue
                try:
                    on = dt.date.fromisoformat(str(raw["on"]))
                    implied = Decimal(str(raw["iv"]))
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    continue
                if implied.is_finite() and implied > ZERO:
                    decoded.append(IVObservation(on=on, implied_volatility=implied))
        return tuple(sorted(decoded, key=lambda item: item.on))

    def _records_for(self, symbol: str) -> tuple[RawObservation, ...]:
        wanted = symbol.strip().upper()
        if wanted not in self._records:
            self.prefetch((wanted,))
        return self._records.get(wanted, ())

    def read(self, symbol: str) -> CachedSeries:
        wanted = symbol.strip().upper()
        records = self._records_for(wanted)
        observations = self._decode(records)
        newest = max(records, key=lambda item: item.observed_at, default=None)
        return CachedSeries(
            symbol=wanted,
            observations=observations,
            fetched_at=newest.observed_at if newest is not None else None,
            source=newest.envelope.source if newest is not None else "cache",
            envelope=newest.envelope if newest is not None else None,
            catalog_version=(
                newest.catalog_version if newest is not None else self.catalog_version
            ),
        )

    def fresh(
        self,
        symbol: str,
        *,
        today: dt.date,
        now: dt.datetime,
        previous_session: dt.date | None = None,
    ) -> bool:
        cached = self.read(symbol)
        if cached.envelope is None or not cached.observations:
            return False
        if not cached.envelope.fresh(now=now, session_date=today):
            return False
        if cached.envelope.session_date != today:
            return False
        previous = previous_session or _previous_weekday(today)
        return cached.last_observation is not None and cached.last_observation >= previous

    def write(
        self,
        symbol: str,
        observations: list[IVObservation] | tuple[IVObservation, ...],
        *,
        fetched_at: dt.datetime,
        source: str = "IBKR:reqHistoricalData:iv",
        ttl: dt.timedelta = dt.timedelta(hours=20),
        configuration_version: str | None = None,
        catalog_version: str | None = None,
    ) -> None:
        fetched_at = fetched_at.astimezone(dt.timezone.utc)
        envelope = ObservationEnvelope(
            symbol=symbol.strip().upper(),
            session_date=fetched_at.date(),
            observed_at=fetched_at,
            expires_at=fetched_at + ttl,
            source=source,
            freshness_class=FreshnessClass.SLOW_OBSERVATION,
            configuration_version=configuration_version or self.configuration_version,
            subscription_generation=uuid4(),
        )
        update = RawObservation(
            symbol=symbol,
            key="iv-history",
            payload={
                "observations": [
                    {"on": item.on.isoformat(), "iv": str(item.implied_volatility)}
                    for item in observations
                ]
            },
            envelope=envelope,
            catalog_version=catalog_version or self.catalog_version,
        )
        self.cache.write_batch((update,))
        self._records[update.symbol] = (update,)

    @property
    def observation_times(self) -> tuple[dt.datetime, ...]:
        return tuple(
            observation.observed_at
            for records in self._records.values()
            for observation in records
        )


class ObservationCacheRVStore:
    """RVStore-shaped adapter over the durable raw-observation cache.

    :class:`ObservationCacheIVStore`'s counterpart for realized-vol input:
    same durable, batch/indexed cache, same raw-input-not-derived-conclusion
    contract. The ``"realized-volatility"`` key was already reserved in
    :meth:`ObservationCacheIVStore.prefetch`'s filter (line ~1146) before any
    writer existed for it -- this class is that writer.
    """

    def __init__(
        self,
        cache: ObservationCache,
        *,
        session_date: dt.date,
        catalog_version: str,
        configuration_version: str,
        now: dt.datetime,
        ttl: dt.timedelta = dt.timedelta(hours=20),
    ) -> None:
        if now.tzinfo is None:
            raise ValueError("cache adapter now must be timezone-aware")
        if ttl <= dt.timedelta(0):
            raise ValueError("cache adapter ttl must be positive")
        self.cache = cache
        self.session_date = session_date
        self.catalog_version = catalog_version
        self.configuration_version = configuration_version
        self.now = now.astimezone(dt.timezone.utc)
        self.ttl = ttl
        self._records: dict[str, tuple[RawObservation, ...]] = {}

    def prefetch(
        self,
        symbols: Iterable[str],
        *,
        records: Mapping[str, Sequence[RawObservation]] | None = None,
    ) -> None:
        """``records``, when supplied, is a batch already fetched elsewhere
        (typically :class:`ObservationCacheIVStore`'s prefetch, since that
        adapter's own filter already includes the ``"realized-volatility"``
        key) -- reused here so the pair spends one ``read_many`` between
        them, not one each."""
        wanted = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols))
        if not wanted:
            return
        if records is None:
            records = self.cache.read_many(
                wanted,
                now=self.now,
                session_date=self.session_date,
                catalog_version=self.catalog_version,
                configuration_version=self.configuration_version,
                include_expired=True,
            )
        self._records.update(
            {
                symbol: tuple(
                    observation
                    for observation in records.get(symbol, ())
                    if observation.key == "realized-volatility"
                )
                for symbol in wanted
            }
        )

    @staticmethod
    def _decode(observations: Iterable[RawObservation]) -> tuple[PriceObservation, ...]:
        decoded: list[PriceObservation] = []
        for observation in observations:
            payload = observation.payload
            raw_series: Any = payload.get("observations", payload.get("series"))
            if raw_series is None and "on" in payload and "close" in payload:
                raw_series = [payload]
            if not isinstance(raw_series, (list, tuple)):
                continue
            for raw in raw_series:
                if not isinstance(raw, Mapping):
                    continue
                try:
                    on = dt.date.fromisoformat(str(raw["on"]))
                    close = Decimal(str(raw["close"]))
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    continue
                if close.is_finite() and close > ZERO:
                    decoded.append(PriceObservation(on=on, close=close))
        return tuple(sorted(decoded, key=lambda item: item.on))

    def _records_for(self, symbol: str) -> tuple[RawObservation, ...]:
        wanted = symbol.strip().upper()
        if wanted not in self._records:
            self.prefetch((wanted,))
        return self._records.get(wanted, ())

    def read(self, symbol: str) -> CachedPriceSeries:
        wanted = symbol.strip().upper()
        records = self._records_for(wanted)
        observations = self._decode(records)
        newest = max(records, key=lambda item: item.observed_at, default=None)
        return CachedPriceSeries(
            symbol=wanted,
            observations=observations,
            fetched_at=newest.observed_at if newest is not None else None,
            source=newest.envelope.source if newest is not None else "cache",
            envelope=newest.envelope if newest is not None else None,
        )

    def fresh(
        self,
        symbol: str,
        *,
        today: dt.date,
        now: dt.datetime,
        previous_session: dt.date | None = None,
    ) -> bool:
        cached = self.read(symbol)
        if cached.envelope is None or not cached.observations:
            return False
        if not cached.envelope.fresh(now=now, session_date=today):
            return False
        if cached.envelope.session_date != today:
            return False
        previous = previous_session or _previous_weekday(today)
        return cached.last_observation is not None and cached.last_observation >= previous

    def write(
        self,
        symbol: str,
        observations: list[PriceObservation] | tuple[PriceObservation, ...],
        *,
        fetched_at: dt.datetime,
        source: str = "IBKR:reqHistoricalData:trades",
        ttl: dt.timedelta = dt.timedelta(hours=20),
        configuration_version: str | None = None,
        catalog_version: str | None = None,
    ) -> None:
        fetched_at = fetched_at.astimezone(dt.timezone.utc)
        envelope = ObservationEnvelope(
            symbol=symbol.strip().upper(),
            session_date=fetched_at.date(),
            observed_at=fetched_at,
            expires_at=fetched_at + ttl,
            source=source,
            freshness_class=FreshnessClass.SLOW_OBSERVATION,
            configuration_version=configuration_version or self.configuration_version,
            subscription_generation=uuid4(),
        )
        update = RawObservation(
            symbol=symbol,
            key="realized-volatility",
            payload={
                "observations": [
                    {"on": item.on.isoformat(), "close": str(item.close)}
                    for item in observations
                ]
            },
            envelope=envelope,
            catalog_version=catalog_version or self.catalog_version,
        )
        self.cache.write_batch((update,))
        self._records[update.symbol] = (update,)

    @property
    def observation_times(self) -> tuple[dt.datetime, ...]:
        return tuple(
            observation.observed_at
            for records in self._records.values()
            for observation in records
        )


class _DurablePacingBudget:
    """Legacy budget facade backed by one persistent pacing ledger.

    Refresh requests are committed one-for-one.  Deep probing first reserves
    its full estimate at candidate priority, which preserves the ledger's
    management floor, then consumes only that reservation while the legacy
    phase-two routine makes its individual calls.  A request beyond the
    estimate is refused instead of silently borrowing from management.
    """

    def __init__(self, ledger: PacingLedger, *, owner_id: str, now: dt.datetime) -> None:
        self.ledger = ledger
        self.owner_id = owner_id
        self.now = now.astimezone(dt.timezone.utc)
        self._sequence = 0
        self._phase2: dict[str, Any] = {}
        self._phase2_current: str | None = None
        self.phase2_considered: list[str] = []
        self.phase2_deferred: set[str] = set()

    def _key(self, label: str) -> str:
        self._sequence += 1
        return f"{self.owner_id}:{label}:{self._sequence}"

    def acquire(self, kind: RequestKind, *, priority: Priority) -> None:
        if self._phase2_current is not None:
            reservation = self._phase2[self._phase2_current]
            used = int(reservation["used"])
            if used >= int(reservation["cost"]):
                raise DiscoveryPaced(
                    f"deep probe estimate exhausted for {self._phase2_current}"
                )
            reservation["used"] = used + 1
            return
        reservation = self.ledger.reserve(
            kind,
            cost=1,
            priority=priority,
            owner_id=self.owner_id,
            request_key=self._key(kind.value.lower()),
            now=self.now,
        )
        if reservation is None:
            raise DiscoveryPaced(
                f"durable pacing ledger refused {kind.value} at {priority.name}"
            )
        self.ledger.commit(reservation.reservation_id, actual_cost=1, now=self.now)

    def prepare_phase2(self, symbol: str, *, estimated_cost: int) -> bool:
        symbol = symbol.strip().upper()
        self.phase2_considered.append(symbol)
        pacing = self.ledger.snapshot(RequestKind.GENERAL, now=self.now)
        if pacing.paused_until is not None and self.now < pacing.paused_until:
            self.phase2_deferred.add(symbol)
            return False
        reservation = self.ledger.reserve(
            RequestKind.GENERAL,
            cost=estimated_cost,
            priority=Priority.CANDIDATE_CONSTRUCTION,
            owner_id=self.owner_id,
            request_key=self._key(f"phase2:{symbol}"),
            now=self.now,
        )
        if reservation is None:
            self.phase2_deferred.add(symbol)
            return False
        self._phase2[symbol] = {
            "reservation": reservation,
            "cost": estimated_cost,
            "used": 0,
        }
        return True

    def begin_phase2(self, symbol: str) -> None:
        self._phase2_current = symbol.strip().upper()

    def mark_phase2_deferred(self, symbol: str) -> None:
        self.phase2_deferred.add(symbol.strip().upper())

    def finish_phase2(self, symbol: str) -> None:
        key = symbol.strip().upper()
        reservation = self._phase2.pop(key, None)
        self._phase2_current = None
        if reservation is None:
            return
        item = reservation["reservation"]
        used = int(reservation["used"])
        if used == 0:
            self.ledger.release(item.reservation_id)
            return
        self.ledger.commit(
            item.reservation_id,
            actual_cost=min(used, int(reservation["cost"])),
            now=self.now,
        )

    def penalize(self, kind: RequestKind) -> None:
        self.ledger.penalize(kind, now=self.now)

    def snapshot(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for kind in (RequestKind.HISTORICAL, RequestKind.GENERAL):
            item = self.ledger.snapshot(kind, now=self.now)
            result[kind.value] = {
                "limit": item.limit,
                "window_seconds": item.window_seconds,
                "consumed": item.consumed,
                "outstanding": item.outstanding,
                "available": item.available,
                "discovery_available": item.discovery_available,
                "management_reserve": item.management_reserve,
                "penalty_factor": item.penalty_factor,
                "paused_until": (
                    item.paused_until.isoformat() if item.paused_until else None
                ),
            }
        return result


class UniverseScanRecoveryRequired(EngineError):
    """A prior scan in this session has no terminal/reconciled receipt."""


@dataclass(frozen=True)
class IntegratedScanResult:
    """The legacy book and its immutable, manifest-bound publication."""

    book: ScanBook
    snapshot: ScanBookSnapshot
    scan_id: str
    session_id: str
    tick_id: str
    attempt_id: str
    catalog_hash: str
    policy_hash: str
    calendar_hash: str
    config_hash: str
    refresh_symbols: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.snapshot.coverage_complete

    @property
    def diagnostic_only(self) -> bool:
        return not self.complete


def _manifest_hash(value: str, *, name: str) -> str:
    normalized = str(value).strip().lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ConfigError(f"{name} must be a SHA-256 hex digest")
    return normalized


def _behavior_manifest_hash(
    *,
    catalog_version: str,
    policy: RiskPolicy,
    regime_policy: VolatilityRegimePolicy,
    config: UniverseScanConfig,
) -> str:
    """Digest every scan/risk/regime input that can change a result."""

    record = {
        "manifest_version": "scan-behavior/1",
        "catalog_version": catalog_version,
        "scan": config.to_record(),
        "risk": policy.to_record(),
        "regime": regime_policy.to_record(),
    }
    payload = json.dumps(
        record, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _safe_scan_id(value: str) -> str:
    """Make the identity safe for both receipt ids and Windows filenames."""
    normalized = "".join(
        character if character in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.@+-" else "_"
        for character in str(value).strip()
    )
    if not normalized:
        raise ValueError("scan_id must contain at least one safe identifier character")
    if len(normalized) > 200:
        raise ValueError("scan_id must be no longer than 200 characters")
    return normalized


def _catalog_snapshot(catalog: UniverseCatalog | CatalogSnapshot) -> CatalogSnapshot:
    if isinstance(catalog, UniverseCatalog):
        return catalog.snapshot()
    if isinstance(catalog, CatalogSnapshot):
        return catalog
    raise TypeError("catalog must be a UniverseCatalog or CatalogSnapshot")


def _catalog_universe(snapshot: CatalogSnapshot) -> tuple[UniverseEntry, ...]:
    return tuple(
        UniverseEntry(
            symbol=entry.symbol,
            sector=entry.sector,
            correlation_group=entry.correlation_group,
        )
        for entry in snapshot.entries
        if entry.active and entry.scan_eligible
    )


def _apply_catalog_entry_gate(
    book: ScanBook, entries: Mapping[str, CatalogEntry]
) -> ScanBook:
    rows: list[ScanBookRow] = []
    for row in book.rows:
        entry = entries.get(row.symbol)
        if entry is not None and row.state is ScanState.CANDIDATE and not entry.automated_entry_allowed:
            row = replace(
                row,
                state=ScanState.INELIGIBLE_REGIME,
                reason=REASON_CATALOG_ENTRY_INELIGIBLE,
                detail="catalog classification or optionability does not permit automated entry",
                nomination=None,
            )
        rows.append(row)
    frozen = tuple(rows)
    return replace(book, rows=frozen, coverage=CoverageSummary.from_rows(frozen))


def _observation_ages(
    adapter: ObservationCacheIVStore, *, now: dt.datetime
) -> ObservationAges:
    ages = [
        max(0.0, (now.astimezone(dt.timezone.utc) - observed).total_seconds())
        for observed in adapter.observation_times
    ]
    if not ages:
        return ObservationAges(oldest_seconds=0.0, newest_seconds=0.0)
    return ObservationAges(oldest_seconds=max(ages), newest_seconds=min(ages))


def _snapshot_from_book(
    book: ScanBook,
    *,
    scan_id: str,
    session_id: str,
    tick_id: str,
    attempt_id: str,
    catalog: CatalogSnapshot,
    entries: Mapping[str, CatalogEntry],
    policy_hash: str,
    calendar_hash: str,
    config_hash: str,
    behavior_hash: str,
    budget: _DurablePacingBudget,
    adapter: ObservationCacheIVStore,
) -> ScanBookSnapshot:
    rows = []
    for row in book.rows:
        record = row.to_record()
        entry = entries.get(row.symbol)
        if entry is not None:
            record["catalog"] = entry.to_record()
            record["automated_entry_allowed"] = entry.automated_entry_allowed
        record["scan_id"] = scan_id
        rows.append(record)
    active_catalog = tuple(
        entry for entry in catalog.entries if entry.active and entry.scan_eligible
    )
    catalog_symbols = {entry.symbol for entry in active_catalog}
    row_symbols = {row.symbol for row in book.rows}
    unexpected = sorted(row_symbols - catalog_symbols)
    if unexpected:
        raise ValueError(
            "scan book contains symbols outside the active catalog: "
            + ", ".join(unexpected)
        )
    missing = catalog_symbols - row_symbols
    expected = len(active_catalog)
    reported_deferred = book.coverage.deferred
    deferred = reported_deferred
    unavailable = book.coverage.unavailable
    # The legacy scanner reports coverage over the rows it happened to build.
    # The durable manifest must report coverage over the pinned catalog, or a
    # missing row could make ``len(rows)`` look complete by definition.
    deferred += len(missing)
    # ``ScanBookRow.evaluated_at`` records that phase one had facts, but a
    # later deep-probe deferral/unavailability is still an incomplete breadth
    # outcome.  Snapshot counts must partition the expected universe rather
    # than double-count a row in both evaluated and deferred/unavailable.
    evaluated = max(0, book.coverage.evaluated - reported_deferred - unavailable)
    phase2_expected = len(budget.phase2_considered)
    phase2_deferred = len(budget.phase2_deferred)
    by_symbol = {row.symbol: row for row in book.rows}
    phase2_unavailable = sum(
        1
        for symbol in budget.phase2_considered
        if by_symbol.get(symbol) is not None
        and by_symbol[symbol].state is ScanState.METADATA_UNAVAILABLE
    )
    phase2_completed = max(0, phase2_expected - phase2_deferred - phase2_unavailable)
    return ScanBookSnapshot(
        scan_id=scan_id,
        session_id=session_id,
        session_date=book.session_date,
        generated_at=book.generated_at,
        catalog_hash=catalog.catalog_hash,
        catalog_version=catalog.version,
        policy_hash=policy_hash,
        calendar_hash=calendar_hash,
        config_hash=config_hash,
        behavior_hash=behavior_hash,
        expected_symbols=expected,
        evaluated_symbols=evaluated,
        deferred_symbols=deferred,
        unavailable_symbols=unavailable,
        rows=tuple(rows),
        phase_coverage={
            "breadth": PhaseCoverage(
                expected=expected,
                completed=evaluated,
                deferred=deferred,
                unavailable=unavailable,
                required=True,
            ),
            "deep": PhaseCoverage(
                expected=phase2_expected,
                completed=phase2_completed,
                deferred=phase2_deferred,
                unavailable=phase2_unavailable,
                required=False,
            ),
        },
        observation_ages=_observation_ages(adapter, now=book.generated_at),
        pacing_snapshot=budget.snapshot(),
        cycle_state={
            "status": "COMPLETE" if evaluated == expected and not deferred and not unavailable else "INCOMPLETE",
            "diagnostic_only": not (evaluated == expected and not deferred and not unavailable),
            "coverage": book.coverage.to_record(),
        },
        shard_state={
            "shard_id": "all",
            "expected": expected,
            "completed": evaluated,
            "deferred": deferred,
            "unavailable": unavailable,
        },
        tick_id=tick_id,
        attempt_id=attempt_id,
    )


def _update_refresh_queue(
    queue: FairRefreshQueue,
    *,
    selected: frozenset[str],
    book: ScanBook,
    now: dt.datetime,
    interval: dt.timedelta,
    ledger: PacingLedger,
    phase2_symbols: Sequence[str] = (),
) -> None:
    general = ledger.snapshot(RequestKind.GENERAL, now=now)
    deferred_until = general.paused_until or now + interval
    for row in book.rows:
        if row.symbol not in selected:
            continue
        if row.observation in {
            ObservationProvenance.CACHE,
            ObservationProvenance.REFRESHED,
        }:
            queue.mark_phase_one(
                row.symbol,
                observed_at=now,
                next_due_at=now + interval,
                previous_rank=float(row.rank_score) if row.rank_score is not None else None,
            )
            continue
        reason = row.reason or REASON_REFRESH_FAILED
        until = deferred_until if row.state is ScanState.DEFERRED_PACING else now + interval
        queue.defer(row.symbol, until=until, reason=reason, now=now)
    by_symbol = {row.symbol: row for row in book.rows}
    for symbol in phase2_symbols:
        row = by_symbol.get(symbol)
        queue.mark_phase_two(
            symbol,
            observed_at=now,
            previous_rank=(
                float(row.rank_score)
                if row is not None and row.rank_score is not None
                else None
            ),
        )


def run_catalog_universe_pass(
    *,
    catalog: UniverseCatalog | CatalogSnapshot,
    observation_cache: ObservationCache,
    pacing_ledger: PacingLedger,
    snapshot_store: ScanBookSnapshotStore,
    receipt_store: ScanReceiptStore,
    session_id: str,
    session_date: dt.date,
    policy_hash: str,
    calendar_hash: str,
    config_hash: str,
    policy: RiskPolicy,
    regime_policy: VolatilityRegimePolicy,
    config: UniverseScanConfig,
    metadata_store: SessionMetadataStore | None = None,
    volatility_history: VolatilityHistoryPort | None = None,
    price_history: PriceHistoryPort | None = None,
    contract_data: ContractDataPort | None = None,
    market_data: LiveMarketDataPort | None = None,
    event_risk: Callable[[str], str | None] | None = None,
    refresh_queue: FairRefreshQueue | None = None,
    now: dt.datetime | None = None,
    scan_id: str | None = None,
    tick_id: str | None = None,
    attempt_id: str | None = None,
    refresh_interval: dt.timedelta = dt.timedelta(minutes=30),
    refresh_enabled: bool = True,
    allow_stale_cache: bool = False,
) -> IntegratedScanResult:
    """Run one durable catalog-backed breadth/deep scan.

    The old :func:`run_universe_pass` remains the compatibility/manual API.
    This path adds the unattended contracts around it: one batch cache read,
    a durable fair refresh ring, a shared pacing ledger, immutable manifest
    publication, and a receipt journal that makes an interrupted scan
    observable instead of treating absence as success.
    """
    when = (now or _utcnow()).astimezone(dt.timezone.utc)
    if refresh_interval <= dt.timedelta(0):
        raise ValueError("refresh_interval must be positive")
    if not session_id.strip():
        raise ValueError("session_id is required")
    policy_hash = _manifest_hash(policy_hash, name="policy_hash")
    calendar_hash = _manifest_hash(calendar_hash, name="calendar_hash")
    config_hash = _manifest_hash(config_hash, name="config_hash")
    snapshot = _catalog_snapshot(catalog)
    catalog_hash = _manifest_hash(snapshot.catalog_hash, name="catalog_hash")
    behavior_hash = _behavior_manifest_hash(
        catalog_version=snapshot.version,
        policy=policy,
        regime_policy=regime_policy,
        config=config,
    )
    tick_id = tick_id or f"manual:{when.strftime('%Y%m%dT%H%M%S')}"
    attempt_id = attempt_id or uuid4().hex
    scan_id = _safe_scan_id(scan_id or f"{session_id}:{tick_id}:{attempt_id}")
    if receipt_store.unmatched(session_id=session_id):
        raise UniverseScanRecoveryRequired(
            f"session {session_id} has an unmatched scan receipt; reconcile before starting {scan_id}"
        )
    if receipt_store.read(scan_id):
        raise UniverseScanRecoveryRequired(
            f"scan {scan_id} already has receipts and cannot be replayed"
        )
    if not isinstance(refresh_enabled, bool):
        raise TypeError("refresh_enabled must be a bool")
    if not isinstance(allow_stale_cache, bool):
        raise TypeError("allow_stale_cache must be a bool")
    queue = refresh_queue or getattr(observation_cache, "refresh_queue", None)
    if queue is None:
        raise TypeError("refresh_queue must be supplied when the cache has no queue")
    entries = {entry.symbol: entry for entry in snapshot.entries}
    universe = _catalog_universe(snapshot)
    # Publish the start before queue/cache work. If the process dies during
    # setup, the unmatched receipt is the recovery witness; absence is never
    # interpreted as a scan that did not happen.
    receipt_store.start(
        session_id=session_id,
        scan_id=scan_id,
        recorded_at=when,
        tick_id=tick_id,
        attempt_id=attempt_id,
        expected_shards=1,
        payload={
            "catalog_hash": catalog_hash,
            "policy_hash": policy_hash,
            "calendar_hash": calendar_hash,
            "config_hash": config_hash,
            "catalog_version": snapshot.version,
            "behavior_hash": behavior_hash,
            "expected_symbols": len(universe),
        },
    )
    queue.seed(
        universe,
        catalog_version=snapshot.version,
        configuration_version=config.version,
        now=when,
        interval=refresh_interval,
        estimated_request_cost=2,
    )
    selected_states = (
        queue.select_due(
            now=when,
            limit=min(config.refresh_limit, len(universe)),
            catalog_version=snapshot.version,
            configuration_version=config.version,
            claim_owner=scan_id,
        )
        if refresh_enabled
        else ()
    )
    selected = frozenset(state.symbol for state in selected_states)
    adapter = ObservationCacheIVStore(
        observation_cache,
        session_date=session_date,
        catalog_version=snapshot.version,
        configuration_version=config.version,
        now=when,
    )
    rv_adapter = ObservationCacheRVStore(
        observation_cache,
        session_date=session_date,
        catalog_version=snapshot.version,
        configuration_version=config.version,
        now=when,
    )
    # One batch read shared by both adapters -- IVStore's own key filter
    # already includes "realized-volatility" (line ~1146), so a second
    # read_many for the RV adapter would be pure duplication.
    prefetch_symbols = tuple(
        entry.symbol for entry in snapshot.entries if entry.active and entry.scan_eligible
    )
    if prefetch_symbols:
        prefetched = observation_cache.read_many(
            tuple(dict.fromkeys(s.strip().upper() for s in prefetch_symbols)),
            now=adapter.now,
            session_date=session_date,
            catalog_version=snapshot.version,
            configuration_version=config.version,
            include_expired=True,
        )
        adapter.prefetch(prefetch_symbols, records=prefetched)
        rv_adapter.prefetch(prefetch_symbols, records=prefetched)
    budget = _DurablePacingBudget(pacing_ledger, owner_id=scan_id, now=when)
    phase2_order = queue.phase_two_order(
        symbols=(entry.symbol for entry in universe),
        catalog_version=snapshot.version,
        configuration_version=config.version,
    )
    try:
        book = run_universe_pass(
            universe=universe,
            session_date=session_date,
            iv_store=adapter,  # type: ignore[arg-type]
            metadata_store=metadata_store or SessionMetadataStore(snapshot_store.root / "metadata"),
            budget=budget,  # type: ignore[arg-type]
            policy=policy,
            regime_policy=regime_policy,
            config=config,
            volatility_history=volatility_history,
            rv_store=rv_adapter,  # type: ignore[arg-type]
            price_history=price_history,
            contract_data=contract_data,
            market_data=market_data,
            event_risk=event_risk,
            now=when,
            refresh_symbols=selected,
            phase2_order=phase2_order,
            allow_stale_cache=allow_stale_cache,
        )
        book = _apply_catalog_entry_gate(book, entries)
        # Probe passes intentionally do not refresh the breadth ring, but
        # their deep attempts still advance the fair deep-ring cursor. A
        # conditional update here would let the ten-minute probe cadence
        # repeatedly select the same ranked symbols forever.
        _update_refresh_queue(
            queue,
            selected=selected if refresh_enabled else frozenset(),
            book=book,
            now=when,
            interval=refresh_interval,
            ledger=pacing_ledger,
            phase2_symbols=budget.phase2_considered,
        )
        immutable = _snapshot_from_book(
            book,
            scan_id=scan_id,
            session_id=session_id,
            tick_id=tick_id,
            attempt_id=attempt_id,
            catalog=snapshot,
            entries=entries,
            policy_hash=policy_hash,
            calendar_hash=calendar_hash,
            config_hash=config_hash,
            behavior_hash=behavior_hash,
            budget=budget,
            adapter=adapter,
        )
        receipt_store.shard_completed(
            session_id=session_id,
            scan_id=scan_id,
            shard_id="all",
            recorded_at=when,
            tick_id=tick_id,
            attempt_id=attempt_id,
            evaluated=immutable.evaluated_symbols,
            deferred=immutable.deferred_symbols,
            unavailable=immutable.unavailable_symbols,
            payload={"coverage": immutable.coverage.value},
        )
        snapshot_store.publish(immutable)
        receipt_store.complete(
            session_id=session_id,
            scan_id=scan_id,
            recorded_at=when,
            tick_id=tick_id,
            attempt_id=attempt_id,
            payload={
                "coverage": immutable.coverage.value,
                "catalog_version": snapshot.version,
                "behavior_hash": behavior_hash,
                "published": True,
            },
        )
        return IntegratedScanResult(
            book=book,
            snapshot=immutable,
            scan_id=scan_id,
            session_id=session_id,
            tick_id=tick_id,
            attempt_id=attempt_id,
            catalog_hash=catalog_hash,
            policy_hash=policy_hash,
            calendar_hash=calendar_hash,
            config_hash=config_hash,
            refresh_symbols=tuple(sorted(selected)),
        )
    except Exception as exc:
        try:
            receipt_store.abort(
                session_id=session_id,
                scan_id=scan_id,
                recorded_at=when,
                reason=f"{type(exc).__name__}: {exc}",
                tick_id=tick_id,
                attempt_id=attempt_id,
            )
        except Exception:
            pass
        raise


run_integrated_universe_pass = run_catalog_universe_pass


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
    rv_store: RVStore | None = None,
    price_history: PriceHistoryPort | None = None,
    contract_data: ContractDataPort | None = None,
    market_data: LiveMarketDataPort | None = None,
    event_risk: Callable[[str], str | None] | None = None,
    now: dt.datetime | None = None,
    refresh_symbols: frozenset[str] | None = None,
    phase2_order: Sequence[str] | None = None,
    allow_stale_cache: bool = False,
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
    # Staleness is decided by the IV series alone -- the "IV cache warm means
    # zero broker requests" contract several callers and tests depend on.
    # Realized vol does NOT gate this partition: it rides along for free
    # whenever a symbol is already being refreshed for IV (phase 1c) or reads
    # from its own cache when one exists (phase 1b), but a symbol with a warm
    # IV cache and a cold RV cache is still served fresh -- RV catches up the
    # next time that symbol's IV naturally goes stale, rather than forcing an
    # extra historical pull the IV cache says is unnecessary.
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
    rv_metrics: dict[str, RealizedVolMetric] = {}
    for symbol in fresh:
        cached = iv_store.read(symbol)
        iv_metric = build_iv_rank(symbol, cached.observations, calculated_at=when)
        metrics[symbol] = (iv_metric, ObservationProvenance.CACHE)
        if rv_store is not None:
            cached_rv = rv_store.read(symbol)
            if cached_rv.observations:
                rv_metrics[symbol] = build_realized_vol(
                    symbol,
                    cached_rv.observations,
                    current_iv=iv_metric.current_iv,
                    calculated_at=when,
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
        if allow_stale_cache and previous is not None:
            # Candidate probing is allowed to rank the last known slow facts,
            # but the resulting book remains diagnostic-only because the row is
            # still marked OBSERVATION_STALE.  No refresh request is spent and
            # no stale fact can reach entry admission as complete coverage.
            rows[symbol] = replace(
                rows[symbol],
                state=ScanState.OBSERVATION_STALE,
                reason=REASON_REFRESH_NOT_REACHED,
                detail=(
                    "candidate probe used stale cache facts; discovery must "
                    "refresh this symbol before entry admission"
                ),
                observation=stale_provenance,
                rank_inputs=previous_inputs,
            )
            continue
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
        if refresh_symbols is not None and symbol not in refresh_symbols:
            rows[symbol] = replace(
                rows[symbol],
                state=ScanState.OBSERVATION_STALE,
                reason=REASON_REFRESH_NOT_REACHED,
                detail=(
                    "the durable fair-refresh queue did not select this symbol "
                    "for the current refresh ring"
                ),
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
        iv_metric = build_iv_rank(symbol, observations, calculated_at=when)
        metrics[symbol] = (iv_metric, ObservationProvenance.REFRESHED)

        # Realized vol rides the same slot: the symbol already cleared every
        # gate above (not paced, budget available, port configured), so this
        # is the one place a second historical pull is spent per symbol per
        # pass rather than a whole parallel bookkeeping system. A failure or
        # absence here degrades iv_rv_ratio to None -- it does not touch the
        # IV refresh this loop already committed.
        if price_history is not None:
            try:
                budget.acquire(RequestKind.HISTORICAL, priority=Priority.DISCOVERY)
            except DiscoveryPaced:
                pass
            else:
                try:
                    price_observations = tuple(price_history.price_history(symbol))
                except Exception:  # noqa: BLE001 - adapter boundary
                    price_observations = ()
                if price_observations:
                    if rv_store is not None:
                        try:
                            rv_store.write(symbol, list(price_observations), fetched_at=when)
                        except Exception:  # noqa: BLE001 - cache write degrades, never aborts
                            pass
                    rv_metrics[symbol] = build_realized_vol(
                        symbol,
                        price_observations,
                        current_iv=iv_metric.current_iv,
                        calculated_at=when,
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
        rv_metric = rv_metrics.get(symbol)
        assessment = VolatilityAssessment(
            symbol=symbol,
            iv_rank=metric.iv_rank,
            iv_percentile=metric.iv_percentile,
            current_iv=metric.current_iv,
            realized_vol_20=rv_metric.realized_vol_20 if rv_metric is not None else None,
            realized_vol_60=rv_metric.realized_vol_60 if rv_metric is not None else None,
            iv_rv_ratio=rv_metric.iv_rv_ratio if rv_metric is not None else None,
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

    # -- phase 2: fair deep-ring order, then current rank --------------------
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if phase2_order is not None:
        rank_by_symbol = {symbol: item for item in ranked for symbol in (item[1],)}
        ordered_symbols = [
            symbol for symbol in phase2_order if symbol in rank_by_symbol
        ]
        ordered_set = set(ordered_symbols)
        ordered_symbols.extend(
            symbol for _, symbol, _ in ranked if symbol not in ordered_set
        )
        selected_ranked = [rank_by_symbol[symbol] for symbol in ordered_symbols]
    else:
        selected_ranked = ranked
    for score, symbol, decision in selected_ranked[: config.phase2_limit]:
        prepared = True
        prepare = getattr(budget, "prepare_phase2", None)
        if callable(prepare):
            try:
                prepared = bool(
                    prepare(
                        symbol,
                        estimated_cost=config.effective_phase2_request_cost,
                    )
                )
            except DiscoveryPaced as exc:
                prepared = False
                rows[symbol] = replace(
                    rows[symbol],
                    state=ScanState.DEFERRED_PACING,
                    reason=REASON_PACING_DEFERRED,
                    detail=str(exc),
                )
        if not prepared:
            if rows[symbol].state is not ScanState.DEFERRED_PACING:
                rows[symbol] = replace(
                    rows[symbol],
                    state=ScanState.DEFERRED_PACING,
                    reason=REASON_PACING_DEFERRED,
                    detail="durable pacing ledger refused the deep-probe estimate",
                )
            continue
        begin = getattr(budget, "begin_phase2", None)
        finish = getattr(budget, "finish_phase2", None)
        if callable(begin):
            begin(symbol)
        try:
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
            mark_deferred = getattr(budget, "mark_phase2_deferred", None)
            if rows[symbol].state is ScanState.DEFERRED_PACING and callable(mark_deferred):
                mark_deferred(symbol)
        finally:
            if callable(finish):
                finish(symbol)

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
    # Qualification may return a wide strike window. Never turn that into an
    # unbounded quote fan-out: the phase-two reservation is a hard cap. Two
    # option legs are sufficient for the v1 vertical selector; a future
    # structure family must declare a larger cost explicitly.
    if callable(getattr(budget, "prepare_phase2", None)):
        max_quoted_legs = max(2, config.effective_phase2_request_cost - 4)
        qualified = qualified[:max_quoted_legs]
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
        family=selection.strategy_type.value,
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
