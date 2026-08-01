"""The two-phase universe scanner: every state reachable, nothing transmittable.

Fake ports only -- no sockets, no ib_async. The fakes record every call so the
tests can assert the *absence* of broker traffic, which is the scanner's core
promise: a fresh cache costs zero requests, a paced budget defers instead of
crashing, and the phase-2 bound is a bound.

Two of these tests are named mutation guards:

* ``test_a_paced_budget_defers_instead_of_crashing`` fails if the
  ``DiscoveryPaced`` catch in the refresh loop is removed (the exception
  propagates and the test errors).
* ``test_phase2_respects_its_bound`` fails if the ``[: config.phase2_limit]``
  slice is removed (the recording contract port sees too many symbols).
"""

from __future__ import annotations

import ast
import datetime as dt
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from engine.cli import COMMANDS, build_parser
from engine.errors import ConfigError
from engine.options.chain import QualifiedOption
from engine.options.freshness import SessionMetadataStore
from engine.options.ivrank import IVObservation
from engine.options.ivstore import IVStore
from engine.options.marketdata import (
    MarketDataProvenance,
    OptionGreeks,
    OptionQuote,
    UnderlyingQuote,
)
from engine.options.pacing import DiscoveryPaced, Priority, RequestKind
from engine.options.policy import RiskPolicy
from engine.options.ports import StrategyQuoteSnapshot
from engine.options.regime import VolatilityRegimePolicy
from engine.options.universe import (
    REASON_EVENT_RISK,
    REASON_NO_CONTRACT_PORT,
    REASON_NO_HISTORY_PORT,
    REASON_NO_MARKET_DATA_PORT,
    REASON_METADATA_UNAVAILABLE,
    REASON_PACING_DEFERRED,
    REASON_PHASE2_NOT_REACHED,
    REASON_REFRESH_FAILED,
    REASON_REFRESH_NOT_REACHED,
    CoverageSummary,
    NominatedLeg,
    ObservationProvenance,
    ScanBook,
    ScanBookRow,
    ScanBookTransitionError,
    ScanState,
    StructureNomination,
    UniverseScanConfig,
    claim_for_logical_entry,
    run_universe_pass,
    supersede,
    transition,
)
from engine.options.universe_data import UniverseEntry, augment

D = Decimal

NOW = dt.datetime(2026, 8, 3, 14, 30, tzinfo=dt.timezone.utc)  # a Monday
TODAY = NOW.date()
FRIDAY = dt.date(2026, 7, 31)  # the previous session
EXPIRY = (TODAY + dt.timedelta(days=45)).strftime("%Y%m%d")

STRIKES = [D(str(s)) for s in range(80, 140, 5)]  # 12 strikes, 80..135


def entry(symbol: str) -> UniverseEntry:
    return UniverseEntry(
        symbol=symbol, sector="TECHNOLOGY", correlation_group="SECTOR_TECH"
    )


# -- IV series shapes --------------------------------------------------------


def rising_series(count: int = 70, *, last: dt.date = FRIDAY) -> list[IVObservation]:
    """Current at the top of its range: IVR 100, percentile 100 -> HIGH."""
    return [
        IVObservation(
            on=last - dt.timedelta(days=count - 1 - i),
            implied_volatility=D("0.10") + D(i) / 200,
        )
        for i in range(count)
    ]


def falling_series(count: int = 70, *, last: dt.date = FRIDAY) -> list[IVObservation]:
    """Current at the bottom of its range: IVR 0 -> DEPRESSED, refused."""
    return [
        IVObservation(
            on=last - dt.timedelta(days=count - 1 - i),
            implied_volatility=D("0.45") - D(i) / 200,
        )
        for i in range(count)
    ]


def mid_series(count: int = 70, *, last: dt.date = FRIDAY) -> list[IVObservation]:
    """Current at 40% of its range: MEDIUM tier, refused for the unknown edge."""
    observations = [
        IVObservation(
            on=last - dt.timedelta(days=count - 1 - i),
            implied_volatility=D("0.10") + (D(i % 60) / 600),
        )
        for i in range(count - 1)
    ]
    observations.append(IVObservation(on=last, implied_volatility=D("0.14")))
    return observations


# -- fakes -------------------------------------------------------------------


class FakeBudget:
    """Records every acquire; optionally refuses DISCOVERY like a paced one."""

    def __init__(self, *, paced: bool = False) -> None:
        self.paced = paced
        self.acquired: list[tuple[RequestKind, Priority]] = []

    def acquire(self, kind: RequestKind, *, priority: Priority) -> None:
        if self.paced and priority is Priority.DISCOVERY:
            raise DiscoveryPaced("discovery is paused after a broker pacing penalty")
        self.acquired.append((kind, priority))


class FakeHistory:
    def __init__(
        self,
        series: dict[str, list[IVObservation]],
        *,
        fail_for: tuple[str, ...] = (),
    ) -> None:
        self.series = series
        self.fail_for = fail_for
        self.calls: list[str] = []

    def implied_volatility_history(
        self, symbol: str, *, duration: str = "1 Y"
    ) -> list[IVObservation]:
        self.calls.append(symbol)
        if symbol in self.fail_for:
            raise RuntimeError("historical data farm is down")
        return self.series.get(symbol, [])


class FakeContractData:
    def __init__(
        self,
        *,
        expirations: list[str] | None = None,
        strikes: list[Decimal] | None = None,
        fail: bool = False,
    ) -> None:
        self._expirations = [EXPIRY] if expirations is None else expirations
        self._strikes = list(STRIKES) if strikes is None else strikes
        self.fail = fail
        self.expiration_calls: list[str] = []
        self.strike_calls: list[tuple[str, str, str]] = []
        self.qualify_calls: list[tuple[str, str, tuple[Decimal, ...], str]] = []

    def expirations(self, symbol: str) -> list[str]:
        self.expiration_calls.append(symbol)
        if self.fail:
            raise RuntimeError("reqSecDefOptParams timed out")
        return list(self._expirations)

    def strikes(self, symbol: str, expiry: str, right: str) -> list[Decimal]:
        self.strike_calls.append((symbol, expiry, right))
        return list(self._strikes)

    def qualify(
        self, symbol: str, expiry: str, strikes: list[Decimal], right: str
    ) -> list[QualifiedOption]:
        self.qualify_calls.append((symbol, expiry, tuple(strikes), right))
        expiration = dt.datetime.strptime(expiry, "%Y%m%d").date()
        return [
            QualifiedOption(
                con_id=int(strike) * 10,
                symbol=symbol,
                expiration=expiration,
                strike=strike,
                right="P",
                multiplier=100,
                exchange="SMART",
                trading_class=symbol,
            )
            for strike in strikes
        ]


def _provenance(generation) -> MarketDataProvenance:
    return MarketDataProvenance(
        requested_type=1,
        subscription_generation=generation,
        subscribed_at=NOW,
        reported_type=1,
        callback_received=True,
        last_provider_event_at=NOW,
        last_local_receive_at=NOW,
    )


class FakeMarketData:
    """Two-sided, deep, tight quotes with put deltas scaling by strike.

    delta = -(strike - 75)/100, so a 0.30 directional target lands the short
    at 105 and the 5-wide protective leg at 100. bid = (strike - 70)/10 keeps
    the mid credit of that pair at 0.50 with a 0.05 crossing cost.
    """

    def __init__(self, *, spread: Decimal = D("0.05"), open_interest: int = 1000,
                 volume: int = 500) -> None:
        self.spread = spread
        self.open_interest = open_interest
        self.volume = volume
        self.calls: list[tuple[str, tuple[int, ...]]] = []

    def strategy_quotes(
        self,
        *,
        underlying_symbol: str,
        con_ids: list[int],
        require_two_sided: bool = False,
    ) -> StrategyQuoteSnapshot:
        self.calls.append((underlying_symbol, tuple(con_ids)))
        underlying_generation = uuid4()
        generations = [("underlying", underlying_generation)]
        legs = []
        for con_id in con_ids:
            strike = D(con_id) / 10
            generation = uuid4()
            bid = (strike - D("70")) / 10
            legs.append(
                OptionQuote(
                    con_id=con_id,
                    provenance=_provenance(generation),
                    bid=bid,
                    ask=bid + self.spread,
                    open_interest=self.open_interest,
                    volume=self.volume,
                    greeks=OptionGreeks(
                        received_at=NOW,
                        subscription_generation=generation,
                        delta=-(strike - D("75")) / 100,
                    ),
                )
            )
            generations.append((str(con_id), generation))
        return StrategyQuoteSnapshot(
            underlying=UnderlyingQuote(
                symbol=underlying_symbol,
                provenance=_provenance(underlying_generation),
                bid=D("110"),
                ask=D("110.10"),
            ),
            legs=tuple(legs),
            generations=tuple(generations),
        )


# -- the pass, with defaults wired for one-line tests ------------------------


def run_pass(
    tmp_path: Path,
    *,
    universe: list[UniverseEntry],
    budget: FakeBudget | None = None,
    volatility_history: FakeHistory | None = None,
    contract_data: FakeContractData | None = None,
    market_data: FakeMarketData | None = None,
    config: UniverseScanConfig | None = None,
    event_risk=None,
) -> ScanBook:
    return run_universe_pass(
        universe=universe,
        session_date=TODAY,
        iv_store=IVStore(tmp_path / "iv"),
        metadata_store=SessionMetadataStore(tmp_path / "metadata"),
        budget=budget if budget is not None else FakeBudget(),
        policy=RiskPolicy(),
        regime_policy=VolatilityRegimePolicy(),
        config=config if config is not None else UniverseScanConfig(phase2_limit=1),
        volatility_history=volatility_history,
        contract_data=contract_data,
        market_data=market_data,
        event_risk=event_risk,
        now=NOW,
    )


def row_for(book: ScanBook, symbol: str) -> ScanBookRow:
    return next(row for row in book.rows if row.symbol == symbol)


def seed_fresh(tmp_path: Path, symbol: str, series: list[IVObservation]) -> None:
    IVStore(tmp_path / "iv").write(symbol, series, fetched_at=NOW)


def seed_stale(tmp_path: Path, symbol: str, series: list[IVObservation]) -> None:
    """Fetched yesterday: the envelope's session is not today's, so stale."""
    IVStore(tmp_path / "iv").write(
        symbol, series, fetched_at=NOW - dt.timedelta(days=1)
    )


# ===========================================================================
# phase 1: cache service and budget-paced refresh
# ===========================================================================


class TestCacheService:
    def test_fresh_cache_serves_with_zero_broker_requests(self, tmp_path) -> None:
        """The cache's core promise: a symbol whose series is fresh costs
        nothing. The recording budget and history port both stay empty."""
        seed_fresh(tmp_path, "AAA", falling_series())
        seed_fresh(tmp_path, "BBB", mid_series())
        history = FakeHistory({})
        budget = FakeBudget()

        book = run_pass(
            tmp_path,
            universe=[entry("AAA"), entry("BBB")],
            budget=budget,
            volatility_history=history,
        )

        assert history.calls == []
        assert budget.acquired == []
        assert row_for(book, "AAA").observation is ObservationProvenance.CACHE
        assert row_for(book, "BBB").observation is ObservationProvenance.CACHE

    def test_stale_symbol_is_refreshed_and_written_back(self, tmp_path) -> None:
        seed_stale(tmp_path, "AAA", rising_series())
        history = FakeHistory({"AAA": rising_series()})

        book = run_pass(
            tmp_path, universe=[entry("AAA")], volatility_history=history
        )

        assert history.calls == ["AAA"]
        assert row_for(book, "AAA").observation is ObservationProvenance.REFRESHED
        assert IVStore(tmp_path / "iv").fresh("AAA", today=TODAY, now=NOW)

    def test_refresh_order_prioritizes_high_previous_rank(self, tmp_path) -> None:
        """Stale symbols are refreshed richest-first, judged from their own
        stale caches; whoever the cap cuts off is OBSERVATION_STALE with the
        not-reached reason, never silently dropped."""
        seed_stale(tmp_path, "LOW", falling_series())  # previous IVR 0
        seed_stale(tmp_path, "HIGH", rising_series())  # previous IVR 100
        seed_stale(tmp_path, "MID", mid_series())  # previous IVR 40
        history = FakeHistory(
            {name: rising_series() for name in ("LOW", "MID", "HIGH")}
        )

        book = run_pass(
            tmp_path,
            universe=[entry("LOW"), entry("HIGH"), entry("MID")],
            volatility_history=history,
            config=UniverseScanConfig(refresh_limit=2, phase2_limit=1),
        )

        assert history.calls == ["HIGH", "MID"]
        cut = row_for(book, "LOW")
        assert cut.state is ScanState.OBSERVATION_STALE
        assert cut.reason == REASON_REFRESH_NOT_REACHED
        assert cut.observation is ObservationProvenance.STALE_CACHE

    def test_a_failed_refresh_is_a_named_stale_row(self, tmp_path) -> None:
        seed_stale(tmp_path, "AAA", rising_series())
        history = FakeHistory({}, fail_for=("AAA",))

        book = run_pass(
            tmp_path, universe=[entry("AAA")], volatility_history=history
        )

        row = row_for(book, "AAA")
        assert row.state is ScanState.OBSERVATION_STALE
        assert row.reason == REASON_REFRESH_FAILED
        assert "historical data farm" in row.detail

    def test_no_history_port_fails_closed_with_a_named_reason(self, tmp_path) -> None:
        book = run_pass(tmp_path, universe=[entry("AAA")])
        row = row_for(book, "AAA")
        assert row.state is ScanState.OBSERVATION_STALE
        assert row.reason == REASON_NO_HISTORY_PORT


class TestPacingDeferral:
    def test_a_paced_budget_defers_instead_of_crashing(self, tmp_path) -> None:
        """MUTATION GUARD: remove the ``except DiscoveryPaced`` in the refresh
        loop and this test errors with an uncaught DiscoveryPaced.

        The paced symbol is deferred, and the cached symbol is still fully
        evaluated -- a paced scan keeps doing the free work."""
        seed_stale(tmp_path, "STALE", rising_series())
        seed_fresh(tmp_path, "FRESH", rising_series())
        history = FakeHistory({"STALE": rising_series()})

        book = run_pass(
            tmp_path,
            universe=[entry("STALE"), entry("FRESH")],
            budget=FakeBudget(paced=True),
            volatility_history=history,
        )

        deferred = row_for(book, "STALE")
        assert deferred.state is ScanState.DEFERRED_PACING
        assert deferred.reason == REASON_PACING_DEFERRED
        assert history.calls == []  # the paced request was never issued
        # The fresh symbol was still evaluated from cache.
        assert row_for(book, "FRESH").evaluated_at is not None

    def test_pacing_wins_over_staleness(self, tmp_path) -> None:
        """PRECEDENCE, pinned: a stale symbol whose refresh was paced is
        DEFERRED_PACING, not OBSERVATION_STALE. The deferral names the actual
        cause (the broker's window); staleness would blame the symbol."""
        seed_stale(tmp_path, "AAA", rising_series())

        book = run_pass(
            tmp_path,
            universe=[entry("AAA")],
            budget=FakeBudget(paced=True),
            volatility_history=FakeHistory({"AAA": rising_series()}),
        )

        row = row_for(book, "AAA")
        assert row.state is ScanState.DEFERRED_PACING
        assert row.state is not ScanState.OBSERVATION_STALE

    def test_deferred_is_reported_deferred_and_never_rejected(self, tmp_path) -> None:
        seed_stale(tmp_path, "AAA", rising_series())

        book = run_pass(
            tmp_path,
            universe=[entry("AAA")],
            budget=FakeBudget(paced=True),
            volatility_history=FakeHistory({"AAA": rising_series()}),
        )

        assert book.coverage.deferred == 1
        assert book.coverage.rejected == {}


# ===========================================================================
# phase 1: event risk and regime
# ===========================================================================


class TestEventRiskAndRegime:
    def test_event_risk_excludes_with_a_named_reason(self, tmp_path) -> None:
        seed_fresh(tmp_path, "AAA", rising_series())

        book = run_pass(
            tmp_path,
            universe=[entry("AAA")],
            event_risk=lambda symbol: "EARNINGS_2026-08-06",
        )

        row = row_for(book, "AAA")
        assert row.state is ScanState.INELIGIBLE_EVENT_RISK
        assert row.reason == REASON_EVENT_RISK
        assert "EARNINGS_2026-08-06" in row.detail

    def test_depressed_regime_is_ineligible_with_the_regime_code(self, tmp_path) -> None:
        seed_fresh(tmp_path, "AAA", falling_series())

        book = run_pass(tmp_path, universe=[entry("AAA")])

        row = row_for(book, "AAA")
        assert row.state is ScanState.INELIGIBLE_REGIME
        assert row.reason == "OPTIONS_REGIME_DEPRESSED_REFUSED"
        assert row.regime is not None and row.regime["regime"] == "DEPRESSED"

    def test_medium_without_an_edge_is_ineligible(self, tmp_path) -> None:
        """The scanner supplies no IV/RV ratio, and an unknown edge fails the
        MEDIUM tier's requirement -- degradation toward refusal, recorded."""
        seed_fresh(tmp_path, "AAA", mid_series())

        book = run_pass(tmp_path, universe=[entry("AAA")])

        row = row_for(book, "AAA")
        assert row.state is ScanState.INELIGIBLE_REGIME
        assert row.reason == "OPTIONS_REGIME_EDGE_REFUSED"

    def test_ranking_inputs_are_recorded_on_the_row(self, tmp_path) -> None:
        seed_fresh(tmp_path, "AAA", rising_series())

        book = run_pass(tmp_path, universe=[entry("AAA")])

        row = row_for(book, "AAA")
        assert row.iv_rank == D("100")
        assert row.iv_percentile == D("100")
        assert row.rank_inputs["version"] == "universe-rank/1"
        # 0.7*100 + 0.3*100 - 5 (iv_rv_ratio missing penalty)
        assert row.rank_score == D("95.00")
        assert "iv_rv_ratio missing" in row.rank_inputs["penalties"]


# ===========================================================================
# phase 2
# ===========================================================================


class TestPhaseTwo:
    def test_a_full_pass_produces_a_candidate_nomination(self, tmp_path) -> None:
        seed_fresh(tmp_path, "AAA", rising_series())

        book = run_pass(
            tmp_path,
            universe=[entry("AAA")],
            contract_data=FakeContractData(),
            market_data=FakeMarketData(),
        )

        row = row_for(book, "AAA")
        assert row.state is ScanState.CANDIDATE
        assert row.reason == ""
        nomination = row.nomination
        assert nomination is not None
        assert nomination.underlying == "AAA"
        assert nomination.family == "SHORT_PREMIUM"
        assert nomination.direction == "BULLISH"
        # delta -(strike-75)/100 against a 0.30 target: short 105, long 100.
        assert [(leg.con_id, str(leg.strike), leg.action) for leg in nomination.legs] == [
            (1050, "105", "SELL"),
            (1000, "100", "BUY"),
        ]
        assert nomination.width == D("5")

    def test_nominations_are_plain_records_not_intents(self, tmp_path) -> None:
        """Runtime half of the read-only proof: what a CANDIDATE row carries
        has no strategy id, no quantity, no price -- nothing an order needs."""
        from engine.options.domain import OptionStrategyIntent

        seed_fresh(tmp_path, "AAA", rising_series())
        book = run_pass(
            tmp_path,
            universe=[entry("AAA")],
            contract_data=FakeContractData(),
            market_data=FakeMarketData(),
        )

        nomination = row_for(book, "AAA").nomination
        assert isinstance(nomination, StructureNomination)
        assert not isinstance(nomination, OptionStrategyIntent)
        for forbidden in ("strategy_id", "quantity", "limit_price", "price_effect"):
            assert not hasattr(nomination, forbidden), forbidden

    def test_phase2_respects_its_bound(self, tmp_path) -> None:
        """MUTATION GUARD: remove the ``[: config.phase2_limit]`` slice and
        the recording contract port sees three symbols instead of one."""
        for symbol in ("AAA", "BBB", "CCC"):
            seed_fresh(tmp_path, symbol, rising_series())
        contract_data = FakeContractData()

        book = run_pass(
            tmp_path,
            universe=[entry("AAA"), entry("BBB"), entry("CCC")],
            contract_data=contract_data,
            market_data=FakeMarketData(),
            config=UniverseScanConfig(phase2_limit=1),
        )

        assert len(contract_data.expiration_calls) == 1
        held_back = [
            row
            for row in book.rows
            if row.state is ScanState.UNSCANNED
            and row.reason == REASON_PHASE2_NOT_REACHED
        ]
        assert len(held_back) == 2

    def test_phase2_takes_the_strongest_ranked_symbol(self, tmp_path) -> None:
        """Equal-IVR ties break alphabetically; here the ranks differ and the
        higher rank gets the only phase-2 slot regardless of universe order."""
        seed_fresh(tmp_path, "AAA", mid_series())  # IVR 40 -> regime-refused
        seed_fresh(tmp_path, "ZZZ", rising_series())  # IVR 100 -> eligible
        contract_data = FakeContractData()

        book = run_pass(
            tmp_path,
            universe=[entry("AAA"), entry("ZZZ")],
            contract_data=contract_data,
            market_data=FakeMarketData(),
            config=UniverseScanConfig(phase2_limit=1),
        )

        assert contract_data.expiration_calls == ["ZZZ"]
        assert row_for(book, "ZZZ").state is ScanState.CANDIDATE

    def test_a_failing_contract_port_is_metadata_unavailable(self, tmp_path) -> None:
        seed_fresh(tmp_path, "AAA", rising_series())

        book = run_pass(
            tmp_path,
            universe=[entry("AAA")],
            contract_data=FakeContractData(fail=True),
            market_data=FakeMarketData(),
        )

        row = row_for(book, "AAA")
        assert row.state is ScanState.METADATA_UNAVAILABLE
        assert row.reason == REASON_METADATA_UNAVAILABLE

    def test_no_contract_port_is_metadata_unavailable(self, tmp_path) -> None:
        seed_fresh(tmp_path, "AAA", rising_series())

        book = run_pass(tmp_path, universe=[entry("AAA")])

        row = row_for(book, "AAA")
        assert row.state is ScanState.METADATA_UNAVAILABLE
        assert row.reason == REASON_NO_CONTRACT_PORT

    def test_no_market_data_port_is_ineligible_liquidity(self, tmp_path) -> None:
        """Unmeasured counts as insufficient: with no quote port the symbol is
        refused on liquidity, not waved through."""
        seed_fresh(tmp_path, "AAA", rising_series())

        book = run_pass(
            tmp_path,
            universe=[entry("AAA")],
            contract_data=FakeContractData(),
        )

        row = row_for(book, "AAA")
        assert row.state is ScanState.INELIGIBLE_LIQUIDITY
        assert row.reason == REASON_NO_MARKET_DATA_PORT

    def test_wide_spreads_fail_the_liquidity_gate(self, tmp_path) -> None:
        seed_fresh(tmp_path, "AAA", rising_series())

        book = run_pass(
            tmp_path,
            universe=[entry("AAA")],
            contract_data=FakeContractData(),
            market_data=FakeMarketData(spread=D("2.00")),
        )

        row = row_for(book, "AAA")
        assert row.state is ScanState.INELIGIBLE_LIQUIDITY
        assert row.reason.startswith("OPTIONS_LIQUIDITY")

    def test_session_metadata_is_cached_for_the_second_pass(self, tmp_path) -> None:
        """SESSION_METADATA freshness class: the expiration catalog is fetched
        once per session. A second pass reads it from the store and spends no
        expiration request -- while quotes, being PERISHABLE, are re-fetched."""
        seed_fresh(tmp_path, "AAA", rising_series())
        first = FakeContractData()
        run_pass(
            tmp_path,
            universe=[entry("AAA")],
            contract_data=first,
            market_data=FakeMarketData(),
        )
        assert first.expiration_calls == ["AAA"]

        second = FakeContractData()
        market_data = FakeMarketData()
        book = run_pass(
            tmp_path,
            universe=[entry("AAA")],
            contract_data=second,
            market_data=market_data,
        )

        assert second.expiration_calls == []  # served from the session cache
        assert second.strike_calls  # the chain walk still ran
        assert market_data.calls  # quotes are perishable: fetched again
        assert row_for(book, "AAA").metadata_source == "CACHE"

    def test_quotes_are_acquired_at_candidate_priority(self, tmp_path) -> None:
        """The audit's ordering: a phase-2 symbol has earned candidate
        priority for its quotes, while chain discovery stays at DISCOVERY."""
        seed_fresh(tmp_path, "AAA", rising_series())
        budget = FakeBudget()

        run_pass(
            tmp_path,
            universe=[entry("AAA")],
            budget=budget,
            contract_data=FakeContractData(),
            market_data=FakeMarketData(),
        )

        priorities = {priority for _, priority in budget.acquired}
        assert Priority.CANDIDATE_CONSTRUCTION in priorities
        assert Priority.DISCOVERY in priorities


# ===========================================================================
# coverage and persistence
# ===========================================================================


class TestCoverageAndPersistence:
    def _mixed_book(self, tmp_path) -> ScanBook:
        seed_fresh(tmp_path, "CAND", rising_series())  # -> CANDIDATE
        seed_fresh(tmp_path, "DEPR", falling_series())  # -> INELIGIBLE_REGIME
        seed_stale(tmp_path, "STALE", rising_series())  # -> refresh fails
        return run_pass(
            tmp_path,
            universe=[entry("CAND"), entry("DEPR"), entry("STALE")],
            volatility_history=FakeHistory({}, fail_for=("STALE",)),
            contract_data=FakeContractData(),
            market_data=FakeMarketData(),
        )

    def test_coverage_counts_are_correct(self, tmp_path) -> None:
        book = self._mixed_book(tmp_path)
        coverage = book.coverage
        assert coverage.total == 3
        assert coverage.cached == 2
        assert coverage.refreshed == 0
        assert coverage.stale == 1
        assert coverage.deferred == 0
        assert coverage.eligible == 1
        assert coverage.evaluated == 2  # CAND and DEPR; STALE never got data
        assert coverage.rejected == {"OPTIONS_REGIME_DEPRESSED_REFUSED": 1}

    def test_the_book_round_trips_from_disk(self, tmp_path) -> None:
        book = self._mixed_book(tmp_path)
        path = book.write(tmp_path)
        assert path.name == f"scanbook-{TODAY.isoformat()}.json"
        assert path.parent.name == "universe"

        loaded = ScanBook.read(tmp_path, TODAY)
        assert loaded is not None
        assert loaded.rows == book.rows
        assert loaded.coverage == book.coverage
        assert loaded.version == book.version
        assert loaded.universe_version == book.universe_version

    def test_reading_an_absent_book_returns_none(self, tmp_path) -> None:
        assert ScanBook.read(tmp_path, TODAY) is None

    def test_candidates_are_ordered_by_score(self, tmp_path) -> None:
        book = self._mixed_book(tmp_path)
        ranked = book.candidates()
        assert [row.symbol for row in ranked] == ["CAND"]
        assert ranked[0].rank_score == D("95.00")


# ===========================================================================
# read-only by construction
# ===========================================================================


class TestReadOnlyByConstruction:
    def test_the_scanner_imports_no_intent_construction(self) -> None:
        """AST half of the read-only proof: the module cannot mint an intent
        or reach the chokepoints, because it never imports the names."""
        import engine.options.universe as universe_module

        source = Path(universe_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        banned_names = {"OptionStrategyIntent", "OptionLegIntent", "build_vertical"}
        banned_modules = {"transmit", "approval"}
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module_tail = (node.module or "").rsplit(".", 1)[-1]
                if module_tail in banned_modules:
                    offenders.append(f"line {node.lineno}: from {node.module}")
                for alias in node.names:
                    if alias.name in banned_names or alias.name in banned_modules:
                        offenders.append(f"line {node.lineno}: {alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.rsplit(".", 1)[-1] in banned_modules:
                        offenders.append(f"line {node.lineno}: import {alias.name}")
            elif isinstance(node, ast.Name) and node.id in banned_names:
                offenders.append(f"line {node.lineno}: {node.id}")
        assert offenders == [], offenders

    def test_the_universe_cli_command_cannot_be_armed(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["options-universe-scan", "--arm"])

    def test_the_universe_cli_command_is_registered(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["options-universe-scan", "--refresh-limit", "10", "--phase2-limit", "2"]
        )
        assert args.command == "options-universe-scan"
        assert args.refresh_limit == 10
        assert "options-universe-scan" in COMMANDS


# ===========================================================================
# the state machine's later edges
# ===========================================================================


def _candidate_row() -> ScanBookRow:
    return ScanBookRow(
        symbol="AAA",
        state=ScanState.CANDIDATE,
        nomination=StructureNomination(
            underlying="AAA",
            family="SHORT_PREMIUM",
            direction="BULLISH",
            expiration=TODAY + dt.timedelta(days=45),
            legs=(
                NominatedLeg(con_id=1050, strike=D("105"), right="P", action="SELL"),
                NominatedLeg(con_id=1000, strike=D("100"), right="P", action="BUY"),
            ),
            short_delta=D("-0.30"),
            width=D("5"),
        ),
    )


class TestTransitions:
    def test_a_candidate_can_be_claimed(self) -> None:
        claimed = claim_for_logical_entry(
            _candidate_row(), claimed_by="logical-entry/42", at=NOW
        )
        assert claimed.state is ScanState.CLAIMED_BY_LOGICAL_ENTRY
        assert claimed.claim_reference == "logical-entry/42"

    def test_a_claimed_row_can_be_superseded(self) -> None:
        claimed = claim_for_logical_entry(
            _candidate_row(), claimed_by="logical-entry/42", at=NOW
        )
        retired = supersede(claimed, reason="a newer scan book exists", at=NOW)
        assert retired.state is ScanState.SUPERSEDED

    def test_a_candidate_can_be_superseded_directly(self) -> None:
        retired = supersede(
            _candidate_row(), reason="a newer scan book exists", at=NOW
        )
        assert retired.state is ScanState.SUPERSEDED

    def test_claiming_a_non_candidate_refuses(self) -> None:
        row = ScanBookRow(symbol="AAA", state=ScanState.UNSCANNED)
        with pytest.raises(ScanBookTransitionError):
            claim_for_logical_entry(row, claimed_by="logical-entry/42", at=NOW)

    def test_superseding_a_rejected_row_refuses(self) -> None:
        row = ScanBookRow(
            symbol="AAA",
            state=ScanState.INELIGIBLE_REGIME,
            reason="OPTIONS_REGIME_DEPRESSED_REFUSED",
        )
        with pytest.raises(ScanBookTransitionError):
            supersede(row, reason="newer book", at=NOW)

    def test_a_claim_must_name_its_claimer(self) -> None:
        with pytest.raises(ScanBookTransitionError):
            claim_for_logical_entry(_candidate_row(), claimed_by="  ", at=NOW)

    def test_a_scanner_pass_never_emits_the_later_states(self, tmp_path) -> None:
        seed_fresh(tmp_path, "AAA", rising_series())
        book = run_pass(
            tmp_path,
            universe=[entry("AAA")],
            contract_data=FakeContractData(),
            market_data=FakeMarketData(),
        )
        emitted = {row.state for row in book.rows}
        assert ScanState.CLAIMED_BY_LOGICAL_ENTRY not in emitted
        assert ScanState.SUPERSEDED not in emitted


# ===========================================================================
# configuration bounds
# ===========================================================================


class TestUniverseScanConfig:
    def test_defaults_are_within_the_contract_bounds(self) -> None:
        config = UniverseScanConfig()
        assert config.refresh_limit <= 100 or config.refresh_limit <= 200
        assert config.refresh_limit == 100
        assert config.phase2_limit == 5

    def test_refresh_limit_ceiling_is_enforced(self) -> None:
        with pytest.raises(ConfigError):
            UniverseScanConfig(refresh_limit=201)

    def test_zero_bounds_are_refused(self) -> None:
        with pytest.raises(ConfigError):
            UniverseScanConfig(phase2_limit=0)

    def test_from_env_reads_overridable_bounds(self) -> None:
        config = UniverseScanConfig.from_env(
            {"IBKR_OPTIONS_UNIVERSE_REFRESH_LIMIT": "150"}, phase2_limit=3
        )
        assert config.refresh_limit == 150
        assert config.phase2_limit == 3

    def test_a_non_numeric_bound_is_a_config_error(self) -> None:
        with pytest.raises(ConfigError):
            UniverseScanConfig.from_env({"IBKR_OPTIONS_UNIVERSE_PHASE2_LIMIT": "many"})


class TestAugmentedUniverse:
    def test_an_unclassified_extra_symbol_is_scanned(self, tmp_path) -> None:
        """Daily-augmented symbols are scannable without classification; the
        row records the absence, and the layers above (governor, allowlist)
        are what keep an unclassified CANDIDATE from ever trading."""
        universe = list(augment([entry("AAA")], ["zzz"]))
        seed_fresh(tmp_path, "ZZZ", rising_series())
        seed_fresh(tmp_path, "AAA", falling_series())

        book = run_pass(
            tmp_path,
            universe=universe,
            contract_data=FakeContractData(),
            market_data=FakeMarketData(),
        )

        row = row_for(book, "ZZZ")
        assert row.sector is None and row.correlation_group is None
        assert row.state is ScanState.CANDIDATE
