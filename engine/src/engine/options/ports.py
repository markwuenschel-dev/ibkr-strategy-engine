"""The boundaries the options engine talks to the outside world across.

Protocols only. Nothing in this module imports ``ib_async``, and nothing in it
performs I/O -- that is the entire point. :mod:`engine.options.adapters` holds
the IBKR implementations, and every risk check and the governor are written
against the shapes here, so they can be exercised in full without a broker.

**No port can transmit an order.** There is deliberately no ``place`` method on
any protocol in this file. A future execution milestone will add one, and adding
it will be a visible diff to this module rather than a new call appearing inside
a strategy function. Until then, the type system says the options path cannot
send anything: there is no method to call.

``BrokerWhatIfPort`` is the closest thing to execution here, and it is exactly
the non-transmitting half -- ``whatIfOrder`` asks what an order *would* cost.

**Quotes come back as one snapshot, not as parts.** ``LiveMarketDataPort``
returns a :class:`StrategyQuoteSnapshot` carrying the underlying, every leg, and
the subscription generations that were active, all together. A port shaped as
``underlying_quote()`` plus ``option_quotes()`` plus ``generations()`` would let
a caller assemble those three from different moments and hand the entitlement
gate a set of generations that never coexisted -- which is the failure the
generation stamping in :mod:`engine.options.marketdata` exists to catch, quietly
reintroduced at the layer above it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable
from uuid import UUID

from ..errors import MarketDataRefusedError
from .chain import QualifiedOption
from .domain import OptionStrategyIntent
from .execution import MarginAssessment
from .executions import ExecutionRecord
from .ivrank import IVObservation
from .realized_vol import PriceObservation
from .marketdata import OptionQuote, RefusalReason, UnderlyingQuote
from .portfolio import PortfolioSnapshot

__all__ = [
    "UNDERLYING_GENERATION_KEY",
    "StrategyQuoteSnapshot",
    "ContractDataPort",
    "LiveMarketDataPort",
    "VolatilityHistoryPort",
    "BrokerWhatIfPort",
    "ExecutionReportPort",
    "PortfolioStatePort",
]

# The key ``require_uniform_live_provenance`` looks the underlying up under.
UNDERLYING_GENERATION_KEY = "underlying"


@dataclass(frozen=True)
class StrategyQuoteSnapshot:
    """Every quote one strategy decision is made against, taken together.

    Construction validates that a generation is recorded for the underlying and
    for every leg. Without that check,
    :func:`engine.options.marketdata.require_uniform_live_provenance` would raise
    ``KeyError`` on a missing entry -- and a ``KeyError`` escaping a gate is an
    outage, not a refusal. Here it is a
    :class:`~engine.errors.MarketDataRefusedError` carrying a machine-readable
    reason, which is what every other market-data failure in this package is.
    """

    underlying: UnderlyingQuote
    legs: tuple[OptionQuote, ...]
    generations: tuple[tuple[str, UUID], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.legs, tuple) or not self.legs:
            raise MarketDataRefusedError(
                RefusalReason.MIXED_PROVENANCE.value,
                "a strategy quote snapshot must contain at least one option leg",
            )

        known = {key for key, _ in self.generations}
        if len(known) != len(self.generations):
            raise MarketDataRefusedError(
                RefusalReason.GENERATION_MISMATCH.value,
                "a subscription key carries more than one active generation",
                hint="two generations for one contract means the snapshot was "
                "assembled from more than one moment",
            )

        required = {UNDERLYING_GENERATION_KEY} | {str(leg.con_id) for leg in self.legs}
        missing = sorted(required - known)
        if missing:
            raise MarketDataRefusedError(
                RefusalReason.GENERATION_MISMATCH.value,
                f"no active subscription generation recorded for {missing}",
                hint="the gate cannot prove a quote is current without knowing "
                "which generation is in force; refusing rather than assuming",
            )

    def generation_map(self) -> dict[str, UUID]:
        """The form :func:`require_uniform_live_provenance` expects."""
        return {key: generation for key, generation in self.generations}

    @property
    def con_ids(self) -> tuple[int, ...]:
        return tuple(leg.con_id for leg in self.legs)


@runtime_checkable
class ContractDataPort(Protocol):
    """Chain discovery and contract qualification.

    Qualification is not eligibility -- see
    :class:`engine.options.chain.ContractStatus`. This port's job ends at "IBKR
    confirms this contract exists and here is its real multiplier".
    """

    def expirations(self, symbol: str) -> Sequence[str]:
        """Every listed expiration, in IBKR's ``YYYYMMDD`` form."""
        ...

    def strikes(self, symbol: str, expiry: str, right: str) -> Sequence[Decimal]:
        """Strikes listed for **this** expiration, never the chain-wide union."""
        ...

    def qualify(
        self,
        symbol: str,
        expiry: str,
        strikes: Sequence[Decimal],
        right: str,
    ) -> Sequence[QualifiedOption]:
        """Confirm each contract and read its real multiplier and trading class."""
        ...


@runtime_checkable
class LiveMarketDataPort(Protocol):
    """Quotes and greeks for a whole structure, with their provenance.

    The return type carries provenance rather than bare prices because the
    decision this feeds is not "what is the price" but "may this price be used to
    select a strike", and only the provenance answers that.
    """

    def strategy_quotes(
        self,
        *,
        underlying_symbol: str,
        con_ids: Sequence[int],
        require_two_sided: bool = False,
    ) -> StrategyQuoteSnapshot:
        """One coherent snapshot of the underlying and every leg.

        ``require_two_sided`` asks the port to hold its settle window open
        until every leg carries both a bid and an ask. Callers quoting a
        *selected structure* -- marking a held position, pricing an exit --
        pass ``True``; callers sweeping a chain window must not, because deep
        wings without bids would pin the wait at its ceiling. The flag changes
        patience, never truthfulness: at the deadline a one-sided snapshot is
        still returned for downstream freshness, provenance, and pricing gates
        to classify independently.
        """
        ...


@runtime_checkable
class VolatilityHistoryPort(Protocol):
    """Trailing implied-volatility history, for IV Rank.

    Separate from :class:`LiveMarketDataPort` because it has a different
    entitlement story: IBKR's ``OPTION_IMPLIED_VOLATILITY`` historical bars
    return real data on an account with no market-data subscription at all, while
    live greeks do not. Collapsing them into one port would tie the working half
    to the blocked half.
    """

    def implied_volatility_history(
        self, symbol: str, *, duration: str = "1 Y"
    ) -> Sequence[IVObservation]:
        """Daily implied-volatility observations, oldest first."""
        ...


@runtime_checkable
class PriceHistoryPort(Protocol):
    """Trailing daily closes for the underlying, for realized volatility.

    Separate from :class:`VolatilityHistoryPort` because it is a different
    ``reqHistoricalData`` series (``whatToShow="TRADES"`` against the
    underlying, not ``OPTION_IMPLIED_VOLATILITY``) with its own entitlement
    story and its own pacing cost -- collapsing the two would make one
    series's outage look like the other's.
    """

    def price_history(
        self, symbol: str, *, duration: str = "4 M"
    ) -> Sequence[PriceObservation]:
        """Daily closes, oldest first. Four calendar months comfortably
        clears the 60-trading-day context window even across a holiday
        cluster; three sits too close to the edge."""
        ...


@runtime_checkable
class BrokerWhatIfPort(Protocol):
    """The broker's own margin opinion on a structure it will not be sent.

    This is the only port that names an :class:`OptionStrategyIntent`, and it
    still cannot transmit: ``whatIfOrder`` is a pricing question. There is no
    ``place`` counterpart in this module.
    """

    def what_if(
        self, intent: OptionStrategyIntent, *, observed_at: dt.datetime
    ) -> MarginAssessment:
        """What the broker says this would cost. Places nothing."""
        ...


@runtime_checkable
class ExecutionReportPort(Protocol):
    """What actually filled, and what the broker charged for it.

    A read-only query, and separate from every other port here because it answers
    a question about the *past*: a fill that already happened and a commission
    that has already been charged. Every other port describes the present.

    It is a port rather than a direct ``ib.fills()`` call inside the marking code
    for the usual reason -- so the completeness rules in
    :mod:`engine.options.executions` can be exercised against hand-written fills,
    including the ones with an unpopulated commission report, which is the case
    that mattered and the case a live broker will not produce on demand.
    """

    def executions(self) -> Sequence[ExecutionRecord]:
        """Every execution the broker will report for this session."""
        ...


@runtime_checkable
class PortfolioStatePort(Protocol):
    """The book the governor sizes an incremental position against.

    Not one of the three ports the milestone named, and added because the
    governor's total-BPR, sector and correlation caps are unimplementable without
    it: each one is a statement about the portfolio, and a governor handed no
    portfolio can only ever fail closed. Keeping it a port rather than a plain
    argument means the "where does this come from" question is answered by an
    adapter that can be tested, instead of being relocated into every caller.
    """

    def snapshot(self, *, as_of: dt.datetime) -> PortfolioSnapshot:
        """Current net liquidation, reserved buying power and open exposures."""
        ...
