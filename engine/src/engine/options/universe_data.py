"""The seed universe: which symbols the daily scan considers, and how each is
classified for concentration purposes.

Data, deliberately separated from the machinery in
:mod:`engine.options.universe` so a change to *what is scanned* is a diff to a
table rather than to a scheduler. The seed set is versioned
(:data:`UNIVERSE_VERSION`) for the same reason every other input to a decision
is versioned in this engine: a ScanBook row produced under ``universe-seed/1``
can be told apart from one produced after the table changed.

Two classification axes ride on every entry, using the same label conventions
as :mod:`engine.options.policy` (``UPPER_SNAKE`` sector names, coarse
correlation groups). They are **descriptive inputs to ranking and reporting**,
not enforcement: the portfolio governor enforces concentration from
``RiskPolicy.sectors`` / ``RiskPolicy.correlation_groups`` and fails closed on
anything unclassified there. A symbol classified here but absent from the
policy maps can therefore be scanned, ranked and nominated -- and still cannot
become a position, because the governor refuses what it cannot classify and
the transmit allowlist refuses what the operator never named.

:func:`augment` merges operator-supplied extra symbols for a single day.
Unclassified extras are scannable on purpose: discovery is read-only, and the
fail-closed layers above (governor classification, port allowlist, verifier
gate) are what stand between a scanned symbol and a traded one. That guarantee
is documented here and enforced there -- this module enforces nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "UNIVERSE_VERSION",
    "UniverseEntry",
    "SEED_UNIVERSE",
    "seed_universe",
    "augment",
]

UNIVERSE_VERSION = "universe-seed/1"


@dataclass(frozen=True)
class UniverseEntry:
    """One scannable underlying with its coarse risk classification.

    ``sector`` and ``correlation_group`` are ``None`` only for daily-augmented
    symbols nobody has classified; every seed entry carries both. ``None``
    means "not classified", never "unconstrained" -- the governor reads absence
    as a refusal, and this type records the absence honestly rather than
    inventing a bucket.
    """

    symbol: str
    sector: str | None
    correlation_group: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("a universe entry must name its symbol")
        if self.symbol != self.symbol.strip().upper():
            raise ValueError(
                f"universe symbols are stored uppercase; got {self.symbol!r}"
            )
        for label, value in (
            ("sector", self.sector),
            ("correlation_group", self.correlation_group),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{self.symbol}: {label} must be None or non-empty")

    @property
    def classified(self) -> bool:
        return self.sector is not None and self.correlation_group is not None


def _entries(
    rows: Sequence[tuple[str, str, str]],
) -> tuple[UniverseEntry, ...]:
    seen: set[str] = set()
    entries: list[UniverseEntry] = []
    for symbol, sector, group in rows:
        key = symbol.strip().upper()
        if key in seen:
            raise ValueError(f"the seed universe lists {key} twice")
        seen.add(key)
        entries.append(
            UniverseEntry(symbol=key, sector=sector, correlation_group=group)
        )
    return tuple(entries)


#: The 2026-08-01 audit's seed set: broad/asset-class ETFs, the sector SPDRs,
#: and the liquid large-cap option chains. Every symbol is classified on both
#: axes; the classifications are coarse on purpose -- they exist to stop six
#: correlated trades wearing six tickers, not to be a factor model.
SEED_UNIVERSE: tuple[UniverseEntry, ...] = _entries(
    (
        # -- broad market and asset classes ---------------------------------
        ("SPY", "BROAD_MARKET", "US_LARGE_CAP"),
        ("QQQ", "TECHNOLOGY", "SECTOR_TECH"),
        ("IWM", "BROAD_MARKET", "US_SMALL_CAP"),
        ("DIA", "BROAD_MARKET", "US_LARGE_CAP"),
        ("TLT", "RATES", "RATES"),
        ("HYG", "CREDIT", "CREDIT"),
        ("GLD", "GOLD", "GOLD"),
        ("SLV", "SILVER", "SILVER"),
        ("USO", "OIL", "OIL"),
        ("EEM", "EM_EQUITY", "EM_EQUITY"),
        ("FXI", "CHINA_EQUITY", "CHINA_EQUITY"),
        # -- sector and industry ETFs ----------------------------------------
        ("XLE", "ENERGY", "SECTOR_ENERGY"),
        ("XLF", "FINANCIALS", "SECTOR_FIN"),
        ("XLK", "TECHNOLOGY", "SECTOR_TECH"),
        ("XLV", "HEALTHCARE", "SECTOR_HEALTH"),
        ("XLI", "INDUSTRIALS", "SECTOR_INDUSTRIAL"),
        ("XLP", "CONSUMER_STAPLES", "SECTOR_CONSUMER"),
        ("XLY", "CONSUMER_DISCRETIONARY", "SECTOR_CONSUMER"),
        ("XLU", "UTILITIES", "SECTOR_UTILITIES"),
        ("XLB", "MATERIALS", "SECTOR_MATERIALS"),
        ("SMH", "SEMICONDUCTORS", "SECTOR_TECH"),
        ("XBI", "BIOTECH", "SECTOR_HEALTH"),
        ("KRE", "REGIONAL_BANKS", "SECTOR_FIN"),
        # -- technology and communications -----------------------------------
        ("AAPL", "TECHNOLOGY", "SECTOR_TECH"),
        ("MSFT", "TECHNOLOGY", "SECTOR_TECH"),
        ("NVDA", "SEMICONDUCTORS", "SECTOR_TECH"),
        ("AMZN", "TECHNOLOGY", "SECTOR_TECH"),
        ("META", "TECHNOLOGY", "SECTOR_TECH"),
        ("GOOGL", "TECHNOLOGY", "SECTOR_TECH"),
        ("TSLA", "CONSUMER_DISCRETIONARY", "SECTOR_TECH"),
        ("AMD", "SEMICONDUCTORS", "SECTOR_TECH"),
        ("AVGO", "SEMICONDUCTORS", "SECTOR_TECH"),
        ("NFLX", "TECHNOLOGY", "SECTOR_TECH"),
        ("CRM", "TECHNOLOGY", "SECTOR_TECH"),
        ("ORCL", "TECHNOLOGY", "SECTOR_TECH"),
        ("INTC", "SEMICONDUCTORS", "SECTOR_TECH"),
        ("MU", "SEMICONDUCTORS", "SECTOR_TECH"),
        ("QCOM", "SEMICONDUCTORS", "SECTOR_TECH"),
        ("PLTR", "TECHNOLOGY", "SECTOR_TECH"),
        ("IBM", "TECHNOLOGY", "SECTOR_TECH"),
        # -- financials -------------------------------------------------------
        ("JPM", "FINANCIALS", "SECTOR_FIN"),
        ("BAC", "FINANCIALS", "SECTOR_FIN"),
        ("C", "FINANCIALS", "SECTOR_FIN"),
        ("GS", "FINANCIALS", "SECTOR_FIN"),
        ("MS", "FINANCIALS", "SECTOR_FIN"),
        ("WFC", "FINANCIALS", "SECTOR_FIN"),
        ("SCHW", "FINANCIALS", "SECTOR_FIN"),
        # -- energy -----------------------------------------------------------
        ("XOM", "ENERGY", "SECTOR_ENERGY"),
        ("CVX", "ENERGY", "SECTOR_ENERGY"),
        ("OXY", "ENERGY", "SECTOR_ENERGY"),
        ("COP", "ENERGY", "SECTOR_ENERGY"),
        ("SLB", "ENERGY", "SECTOR_ENERGY"),
        # -- consumer ---------------------------------------------------------
        ("WMT", "CONSUMER_STAPLES", "SECTOR_CONSUMER"),
        ("COST", "CONSUMER_STAPLES", "SECTOR_CONSUMER"),
        ("TGT", "CONSUMER_DISCRETIONARY", "SECTOR_CONSUMER"),
        ("HD", "CONSUMER_DISCRETIONARY", "SECTOR_CONSUMER"),
        ("LOW", "CONSUMER_DISCRETIONARY", "SECTOR_CONSUMER"),
        ("NKE", "CONSUMER_DISCRETIONARY", "SECTOR_CONSUMER"),
        ("SBUX", "CONSUMER_DISCRETIONARY", "SECTOR_CONSUMER"),
        ("MCD", "CONSUMER_DISCRETIONARY", "SECTOR_CONSUMER"),
        ("DIS", "CONSUMER_DISCRETIONARY", "SECTOR_CONSUMER"),
        ("UBER", "CONSUMER_DISCRETIONARY", "SECTOR_CONSUMER"),
        ("ABNB", "CONSUMER_DISCRETIONARY", "SECTOR_CONSUMER"),
        # -- healthcare -------------------------------------------------------
        ("LLY", "HEALTHCARE", "SECTOR_HEALTH"),
        ("UNH", "HEALTHCARE", "SECTOR_HEALTH"),
        ("JNJ", "HEALTHCARE", "SECTOR_HEALTH"),
        ("PFE", "HEALTHCARE", "SECTOR_HEALTH"),
        ("MRK", "HEALTHCARE", "SECTOR_HEALTH"),
        ("ABBV", "HEALTHCARE", "SECTOR_HEALTH"),
        ("AMGN", "HEALTHCARE", "SECTOR_HEALTH"),
        # -- industrials and autos --------------------------------------------
        ("CAT", "INDUSTRIALS", "SECTOR_INDUSTRIAL"),
        ("BA", "INDUSTRIALS", "SECTOR_INDUSTRIAL"),
        ("GE", "INDUSTRIALS", "SECTOR_INDUSTRIAL"),
        ("RTX", "INDUSTRIALS", "SECTOR_INDUSTRIAL"),
        ("LMT", "INDUSTRIALS", "SECTOR_INDUSTRIAL"),
        ("DE", "INDUSTRIALS", "SECTOR_INDUSTRIAL"),
        ("F", "CONSUMER_DISCRETIONARY", "SECTOR_CONSUMER"),
        ("GM", "CONSUMER_DISCRETIONARY", "SECTOR_CONSUMER"),
        # -- crypto-adjacent and thematics ------------------------------------
        ("COIN", "CRYPTO", "CRYPTO"),
        ("ARKK", "INNOVATION", "SECTOR_TECH"),
    )
)


def seed_universe() -> tuple[UniverseEntry, ...]:
    """The versioned seed set. A copy of nothing: the tuple is immutable."""
    return SEED_UNIVERSE


def augment(
    universe: Sequence[UniverseEntry], extra_symbols: Iterable[str]
) -> tuple[UniverseEntry, ...]:
    """Merge daily-augmented symbols into a universe, without classifying them.

    An extra symbol already present in the universe is skipped rather than
    duplicated (its seed classification wins). An unknown extra becomes an
    unclassified entry -- scannable, and *only* scannable: the governor's
    classification maps and the operator allowlist both fail closed, so an
    unclassified symbol's rows can reach CANDIDATE and can never become a
    claimed, traded position through this table. That property lives in those
    gates; this helper merely refuses to invent a sector nobody assigned.
    """
    known = {entry.symbol for entry in universe}
    merged = list(universe)
    seen_extras: set[str] = set()
    for raw in extra_symbols:
        symbol = str(raw).strip().upper()
        if not symbol or symbol in known or symbol in seen_extras:
            continue
        seen_extras.add(symbol)
        merged.append(
            UniverseEntry(symbol=symbol, sector=None, correlation_group=None)
        )
    return tuple(merged)
