"""Executable fixtures for the M3<->M4 integration suites.

Companion to ``docs/INTEGRATION-M3-M4.md``; the section numbers below refer to
that contract. Both future suites -- the universe scanner's (Lane A) and the
logical-entry manager's (Lane B) -- import from here, so this module depends on
**neither**: it imports only landed modules (``engine.options.marketdata``,
``engine.options.ports``) plus stdlib, and every cross-lane shape is expressed
as a local duck-typed stand-in or a Protocol. It must keep importing cleanly
whichever lane's concrete names win; the coordinator reconciles names, not
shapes.

Three fixtures:

* :func:`nomination` -- a deterministic fake nomination whose leg fields are
  a superset of BOTH landed shapes: Lane A's ``NominatedLeg``
  (con_id/strike/right/action, ``universe.py:198-229``) and the fully
  qualified fields Lane B's ``EntryNomination`` needs to build
  ``OptionLegIntent`` legs (``logical.py:294-298``). A test can project either
  side out of it, which is exactly the coordinator's bridging job per the
  contract's Section 9.1.
* :class:`ScriptedMarketDataPort` with :func:`market_static_port` /
  :func:`market_moved_port` -- a two-pass quote port pair built on the same
  idioms as ``test_options_runner.py``'s ``FakeMarketDataPort`` (mid rises
  with strike, delta is exactly -0.30 at 450, one shared ``price_factor``
  scales every leg together), with explicit pass boundaries so a test can say
  "the market held between passes" or "the market moved".
* :class:`RecordingScanBookWriter` -- an in-memory, transition-recording
  implementation of the contract's Section 2 claim-writer interface, with the
  compare-and-set semantics the invariant depends on.

Determinism is load-bearing everywhere: contract ids are derived from strikes,
clocks are fixed constants, and nothing here reads a wall clock unless a caller
passes one in.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Protocol
from uuid import UUID, uuid4

from engine.options.marketdata import (
    MarketDataProvenance,
    MarketDataType,
    OptionGreeks,
    OptionQuote,
    UnderlyingQuote,
)
from engine.options.ports import StrategyQuoteSnapshot

__all__ = [
    "NOW",
    "TODAY",
    "SPOT",
    "FakeNominationLeg",
    "FakeNomination",
    "nomination",
    "QuoteCall",
    "ScriptedMarketDataPort",
    "market_static_port",
    "market_moved_port",
    "ScanBookClaimWriter",
    "RowState",
    "ScanBookTransition",
    "RecordingScanBookWriter",
]

D = Decimal

#: One clock for the whole module: the Monday the universe-support tests use
#: (``test_options_universe_support.py``), so fixtures from both files can meet
#: in one test without a timestamp argument.
NOW = dt.datetime(2026, 8, 3, 13, 0, tzinfo=dt.timezone.utc)
TODAY = NOW.date()

#: Same market as ``test_options_runner.py``: spot 500, strikes in fives,
#: |delta| exactly 0.30 at 450 -- so real delta selection lands on 450/445.
SPOT = D("500")
UNDERLYING_CON_ID = 9000
HALF_SPREAD = D("0.05")


# ---------------------------------------------------------------------------
# Section 1: the nomination record (PROVISIONAL shape, duck-typed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeNominationLeg:
    """Field-for-field the contract's Section 1 leg shape.

    Deliberately NOT an ``OptionLegIntent``: a nomination carries contract
    identity, never prices or quantities. The field set is the constructor
    surface of ``OptionLegIntent`` (``domain.py:138-149``) so the entry path
    can rebuild real legs from it.
    """

    con_id: int
    symbol: str
    expiration: dt.date
    strike: Decimal
    right: str
    action: str
    ratio: int = 1
    multiplier: int = 100
    exchange: str = "SMART"
    trading_class: str = ""


@dataclass(frozen=True)
class FakeNomination:
    """The Section 1 handoff record. No intent, no prices, no strategy_id."""

    underlying: str
    family: str
    direction: str
    expiration: dt.date
    legs: tuple[FakeNominationLeg, ...]
    evidence: Mapping[str, str]
    scanbook_row: str
    scanned_at: dt.datetime
    configuration_version: str


def nomination(
    *,
    underlying: str = "SPY",
    short_strike: Decimal = D("450"),
    long_strike: Decimal = D("445"),
    right: str = "P",
    family: str = "PUT_CREDIT_SPREAD",
    direction: str = "BULLISH",
    expiration: dt.date | None = None,
    scanned_at: dt.datetime = NOW,
    evidence: Mapping[str, str] | None = None,
    configuration_version: str = "options-universe/0-test",
) -> FakeNomination:
    """A deterministic nomination: same inputs, same record, same row id.

    Contract ids ARE the strikes, matching the ``test_options_runner.py``
    convention, so the same fake market-data port can quote a nomination's
    legs without a lookup table. The default 450/445 put pair is the exact
    structure the runner suite's delta selection lands on.
    """
    expiry = expiration or (scanned_at.date() + dt.timedelta(days=45))
    legs = (
        FakeNominationLeg(
            con_id=int(short_strike),
            symbol=underlying,
            expiration=expiry,
            strike=short_strike,
            right=right,
            action="SELL",
        ),
        FakeNominationLeg(
            con_id=int(long_strike),
            symbol=underlying,
            expiration=expiry,
            strike=long_strike,
            right=right,
            action="BUY",
        ),
    )
    row_id = (
        f"scan-{underlying}-{expiry.isoformat()}-"
        f"{short_strike}x{long_strike}{right}"
    )
    resolved_evidence: Mapping[str, str] = dict(
        {
            "iv_rank": "62.50",
            "iv_percentile": "71.00",
            "credit_estimate": "1.50",
            "spread_width": str(short_strike - long_strike),
            "quoted_at": scanned_at.isoformat(),
            "freshness_class": "PERISHABLE",
        },
        **(dict(evidence) if evidence else {}),
    )
    return FakeNomination(
        underlying=underlying,
        family=family,
        direction=direction,
        expiration=expiry,
        legs=legs,
        evidence=resolved_evidence,
        scanbook_row=row_id,
        scanned_at=scanned_at,
        configuration_version=configuration_version,
    )


# ---------------------------------------------------------------------------
# Section 3(a): the scripted two-pass market
# ---------------------------------------------------------------------------


def leg_mid(strike: Decimal) -> Decimal:
    """A put mid that rises with the strike (same curve as the runner suite),
    so different legs price differently and a structure has a real credit."""
    return max(D("0.10"), (strike - D("400")) * D("0.30"))


def leg_delta(strike: Decimal) -> Decimal:
    """Put delta, negative, exactly -0.30 at 450 (the selection target)."""
    return -(D("0.50") + (strike - SPOT) * D("0.004"))


def provenance(
    generation: UUID, *, reported: MarketDataType, at: dt.datetime
) -> MarketDataProvenance:
    return MarketDataProvenance(
        requested_type=int(MarketDataType.LIVE),
        subscription_generation=generation,
        subscribed_at=at,
        reported_type=int(reported),
        callback_received=True,
        last_provider_event_at=at,
        last_local_receive_at=at,
    )


def option_quote(
    *,
    con_id: int,
    mid: Decimal,
    delta: Decimal | None = None,
    reported: MarketDataType = MarketDataType.LIVE,
    at: dt.datetime = NOW,
    open_interest: int | None = 5000,
    volume: int | None = 1000,
) -> OptionQuote:
    """One leg quote whose greeks share the quote's generation, two-sided,
    liquid -- so no test refuses for a reason it is not about."""
    generation = uuid4()
    return OptionQuote(
        con_id=con_id,
        provenance=provenance(generation, reported=reported, at=at),
        bid=mid - HALF_SPREAD,
        ask=mid + HALF_SPREAD,
        open_interest=open_interest,
        volume=volume,
        greeks=OptionGreeks(
            received_at=at, subscription_generation=generation, delta=delta
        ),
    )


def quote_snapshot(
    legs: tuple[OptionQuote, ...],
    *,
    symbol: str = "SPY",
    underlying_reported: MarketDataType = MarketDataType.LIVE,
    at: dt.datetime = NOW,
) -> StrategyQuoteSnapshot:
    underlying_generation = uuid4()
    return StrategyQuoteSnapshot(
        underlying=UnderlyingQuote(
            symbol=symbol,
            provenance=provenance(
                underlying_generation, reported=underlying_reported, at=at
            ),
            bid=SPOT - D("0.10"),
            ask=SPOT + D("0.10"),
        ),
        legs=legs,
        generations=(
            ("underlying", underlying_generation),
            *((str(q.con_id), q.provenance.subscription_generation) for q in legs),
        ),
    )


@dataclass(frozen=True)
class QuoteCall:
    """One recorded ``strategy_quotes`` call, pass index included, so a test
    can assert which pass asked for what -- and whether the binding
    revalidation demanded a two-sided book."""

    pass_index: int
    underlying_symbol: str
    con_ids: tuple[int, ...]
    require_two_sided: bool


class ScriptedMarketDataPort:
    """A quote port whose prices are scripted per PASS, not per call.

    A pass (one ``run_once`` / one ``manager.service``) may quote the same
    legs several times -- the build and the binding revalidation both ask --
    and all calls within a pass must agree, or the test measures its own
    fixture's jitter instead of the market. So the script advances only when
    the test says so, via :meth:`next_pass`.

    ``pass_factors[i]`` scales every leg mid together during pass ``i`` (the
    ``price_factor`` idiom from ``test_options_runner.py``): a factor change
    between passes changes the credit, therefore the structure digest,
    therefore the approval spec digest -- which is exactly the "market moved,
    new review required" case of contract Section 3(a). Identical factors are
    the "market static, pending review completes" case.

    The last factor is sticky: a test that runs more passes than it scripted
    keeps the final market rather than crashing in fixture code.
    """

    def __init__(
        self,
        pass_factors: tuple[Decimal, ...] = (D("1"),),
        *,
        reported: MarketDataType = MarketDataType.LIVE,
        start_at: dt.datetime = NOW,
        seconds_between_passes: int = 60,
    ) -> None:
        if not pass_factors:
            raise ValueError("a scripted port needs at least one pass factor")
        self.pass_factors = tuple(D(f) for f in pass_factors)
        self.reported = reported
        self.start_at = start_at
        self.seconds_between_passes = seconds_between_passes
        self.pass_index = 0
        self.calls: list[QuoteCall] = []

    # -- pass control ------------------------------------------------------

    def next_pass(self) -> None:
        """Advance to the next scripted pass. The TEST calls this between
        passes; the port never advances itself."""
        self.pass_index += 1

    @property
    def price_factor(self) -> Decimal:
        index = min(self.pass_index, len(self.pass_factors) - 1)
        return self.pass_factors[index]

    @property
    def now(self) -> dt.datetime:
        """The quote timestamp for the current pass: strictly later each pass,
        so staleness ordering is realistic without reading a wall clock."""
        return self.start_at + dt.timedelta(
            seconds=self.pass_index * self.seconds_between_passes
        )

    # -- the port ----------------------------------------------------------

    def strategy_quotes(
        self,
        *,
        underlying_symbol: str,
        con_ids: Any,
        require_two_sided: bool = False,
    ) -> StrategyQuoteSnapshot:
        """Always two-sided (so ``require_two_sided`` is honoured trivially),
        recorded per call with the pass index and the flag."""
        con_ids = tuple(int(c) for c in con_ids)
        self.calls.append(
            QuoteCall(
                pass_index=self.pass_index,
                underlying_symbol=underlying_symbol,
                con_ids=con_ids,
                require_two_sided=require_two_sided,
            )
        )
        at = self.now
        legs = tuple(
            option_quote(
                con_id=con_id,
                mid=leg_mid(D(con_id)) * self.price_factor,
                delta=leg_delta(D(con_id)),
                reported=self.reported,
                at=at,
            )
            for con_id in con_ids
        )
        return quote_snapshot(
            legs,
            symbol=underlying_symbol,
            underlying_reported=self.reported,
            at=at,
        )


def market_static_port() -> ScriptedMarketDataPort:
    """Two passes, identical prices: a pending review's rebuilt packet must
    reproduce the same spec digest and complete against the pass-1 request."""
    return ScriptedMarketDataPort(pass_factors=(D("1"), D("1")))


def market_moved_port() -> ScriptedMarketDataPort:
    """Two passes, prices up 20% on pass 2: the rebuilt packet's digest must
    differ, the pass-1 approval must NOT match, and a new review is filed --
    the invalidation rule working, not failing."""
    return ScriptedMarketDataPort(pass_factors=(D("1"), D("1.2")))


# ---------------------------------------------------------------------------
# Section 2: the ScanBook claim-writer, in memory
# ---------------------------------------------------------------------------


class ScanBookClaimWriter(Protocol):
    """The narrow writer interface of contract Section 2. PROVISIONAL name;
    the shape is binding. ``False`` means "the row was not CANDIDATE" -- a
    lost race, an ordinary outcome. Invalid transitions raise."""

    def mark_claimed(
        self, row_id: str, *, entry_id: UUID, at: dt.datetime
    ) -> bool: ...

    def mark_superseded(
        self, row_id: str, *, reason: str, at: dt.datetime
    ) -> bool: ...


#: Row states, as strings so no lane's enum is imported. The values are the
#: contract's; ``universe_data.py`` owns the real enum when it lands.
class RowState:
    CANDIDATE = "CANDIDATE"
    CLAIMED_BY_LOGICAL_ENTRY = "CLAIMED_BY_LOGICAL_ENTRY"
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True)
class ScanBookTransition:
    """One recorded state change, for assertions."""

    row_id: str
    to_state: str
    at: dt.datetime
    entry_id: UUID | None = None
    reason: str = ""


class RecordingScanBookWriter:
    """In-memory :class:`ScanBookClaimWriter` with the CAS semantics the
    Section 2 invariant depends on, recording every transition.

    The edge set mirrors the LANDED state machine
    (``universe.py:411-416``, ``_ALLOWED_TRANSITIONS``): CANDIDATE may go to
    CLAIMED_BY_LOGICAL_ENTRY or SUPERSEDED, and CLAIMED_BY_LOGICAL_ENTRY may
    go to SUPERSEDED (a newer book retiring a claimed row). Duck-typed on
    purpose -- this module still imports neither lane.

    * ``mark_claimed`` succeeds only on a CANDIDATE row; claiming again with
      the SAME entry id is idempotent (True, no second transition); claiming
      with a DIFFERENT entry id raises -- two entries owning one row is the
      exact double-ownership the invariant forbids, and a fixture that
      returned False there would let a buggy manager read it as a benign race.
    * ``mark_superseded`` succeeds on a CANDIDATE **or CLAIMED** row, per the
      landed edge set; re-superseding a SUPERSEDED row returns False;
      an unknown row raises.
    """

    def __init__(self, candidate_rows: tuple[str, ...] = ()) -> None:
        self._states: dict[str, str] = {
            row: RowState.CANDIDATE for row in candidate_rows
        }
        self._owners: dict[str, UUID] = {}
        self.transitions: list[ScanBookTransition] = []

    # -- seeding (the scanner's half, faked) -------------------------------

    def add_candidate(self, row_id: str) -> None:
        """What the scanner does on a scan pass: a new CANDIDATE row.
        Re-adding an existing row is refused -- row ids are unique."""
        if row_id in self._states:
            raise ValueError(f"scanbook row {row_id!r} already exists")
        self._states[row_id] = RowState.CANDIDATE

    # -- the writer interface ----------------------------------------------

    def mark_claimed(self, row_id: str, *, entry_id: UUID, at: dt.datetime) -> bool:
        state = self._require(row_id)
        if state == RowState.CLAIMED_BY_LOGICAL_ENTRY:
            if self._owners.get(row_id) == entry_id:
                return True  # idempotent re-claim by the same entry
            raise ValueError(
                f"scanbook row {row_id!r} is already claimed by "
                f"{self._owners.get(row_id)}; a second entry may not claim it"
            )
        if state != RowState.CANDIDATE:
            return False
        self._states[row_id] = RowState.CLAIMED_BY_LOGICAL_ENTRY
        self._owners[row_id] = entry_id
        self.transitions.append(
            ScanBookTransition(
                row_id=row_id,
                to_state=RowState.CLAIMED_BY_LOGICAL_ENTRY,
                at=at,
                entry_id=entry_id,
            )
        )
        return True

    def mark_superseded(self, row_id: str, *, reason: str, at: dt.datetime) -> bool:
        state = self._require(row_id)
        if state not in (RowState.CANDIDATE, RowState.CLAIMED_BY_LOGICAL_ENTRY):
            return False
        self._states[row_id] = RowState.SUPERSEDED
        self.transitions.append(
            ScanBookTransition(
                row_id=row_id,
                to_state=RowState.SUPERSEDED,
                at=at,
                reason=reason,
            )
        )
        return True

    # -- read-back for assertions ------------------------------------------

    def state_of(self, row_id: str) -> str:
        return self._require(row_id)

    def owner_of(self, row_id: str) -> UUID | None:
        return self._owners.get(row_id)

    def _require(self, row_id: str) -> str:
        try:
            return self._states[row_id]
        except KeyError:
            raise ValueError(f"unknown scanbook row {row_id!r}") from None
