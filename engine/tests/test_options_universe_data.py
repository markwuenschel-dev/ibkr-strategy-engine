"""The seed universe table: every symbol classified, nothing listed twice."""

from __future__ import annotations

import pytest

from engine.options.universe_data import (
    SEED_UNIVERSE,
    UNIVERSE_VERSION,
    UniverseEntry,
    augment,
    seed_universe,
)


class TestSeedUniverse:
    def test_the_seed_set_is_the_audited_eighty(self) -> None:
        assert len(SEED_UNIVERSE) == 80
        assert seed_universe() is SEED_UNIVERSE

    def test_every_seed_symbol_is_classified_on_both_axes(self) -> None:
        unclassified = [
            entry.symbol for entry in SEED_UNIVERSE if not entry.classified
        ]
        assert unclassified == []

    def test_no_symbol_appears_twice(self) -> None:
        symbols = [entry.symbol for entry in SEED_UNIVERSE]
        assert len(symbols) == len(set(symbols))

    def test_the_version_is_pinned(self) -> None:
        assert UNIVERSE_VERSION == "universe-seed/1"

    @pytest.mark.parametrize(
        ("symbol", "sector", "group"),
        [
            ("SPY", "BROAD_MARKET", "US_LARGE_CAP"),
            ("IWM", "BROAD_MARKET", "US_SMALL_CAP"),
            ("TLT", "RATES", "RATES"),
            ("HYG", "CREDIT", "CREDIT"),
            ("GLD", "GOLD", "GOLD"),
            ("SLV", "SILVER", "SILVER"),
            ("USO", "OIL", "OIL"),
            ("EEM", "EM_EQUITY", "EM_EQUITY"),
            ("FXI", "CHINA_EQUITY", "CHINA_EQUITY"),
            ("NVDA", "SEMICONDUCTORS", "SECTOR_TECH"),
            ("JPM", "FINANCIALS", "SECTOR_FIN"),
            ("XOM", "ENERGY", "SECTOR_ENERGY"),
            ("LLY", "HEALTHCARE", "SECTOR_HEALTH"),
            ("COIN", "CRYPTO", "CRYPTO"),
        ],
    )
    def test_spot_classifications(self, symbol: str, sector: str, group: str) -> None:
        entry = next(e for e in SEED_UNIVERSE if e.symbol == symbol)
        assert entry.sector == sector
        assert entry.correlation_group == group

    def test_the_audited_symbols_are_all_present(self) -> None:
        expected = set(
            "SPY QQQ IWM DIA TLT HYG GLD SLV USO EEM FXI XLE XLF XLK XLV XLI "
            "XLP XLY XLU XLB SMH XBI KRE AAPL MSFT NVDA AMZN META GOOGL TSLA "
            "AMD AVGO NFLX CRM ORCL INTC MU QCOM PLTR IBM JPM BAC C GS MS WFC "
            "SCHW XOM CVX OXY COP SLB WMT COST TGT HD LOW NKE SBUX MCD DIS "
            "UBER ABNB LLY UNH JNJ PFE MRK ABBV AMGN CAT BA GE RTX LMT DE F "
            "GM COIN ARKK".split()
        )
        assert {entry.symbol for entry in SEED_UNIVERSE} == expected


class TestUniverseEntry:
    def test_lowercase_symbols_are_refused(self) -> None:
        with pytest.raises(ValueError):
            UniverseEntry(symbol="spy", sector="BROAD_MARKET", correlation_group="X")

    def test_an_empty_classification_is_refused(self) -> None:
        with pytest.raises(ValueError):
            UniverseEntry(symbol="SPY", sector="  ", correlation_group="X")

    def test_none_classifications_are_allowed_and_reported(self) -> None:
        entry = UniverseEntry(symbol="ZZZ", sector=None, correlation_group=None)
        assert entry.classified is False


class TestAugment:
    def test_unknown_extras_join_unclassified(self) -> None:
        merged = augment(SEED_UNIVERSE, ["zzzt", "QQXX"])
        added = {e.symbol: e for e in merged}
        assert added["ZZZT"].classified is False
        assert added["QQXX"].classified is False
        assert len(merged) == len(SEED_UNIVERSE) + 2

    def test_existing_symbols_keep_their_seed_classification(self) -> None:
        merged = augment(SEED_UNIVERSE, ["spy", "SPY"])
        assert len(merged) == len(SEED_UNIVERSE)
        spy = next(e for e in merged if e.symbol == "SPY")
        assert spy.sector == "BROAD_MARKET"

    def test_duplicate_and_blank_extras_are_collapsed(self) -> None:
        merged = augment(SEED_UNIVERSE, ["NEWA", "newa", " ", ""])
        assert len(merged) == len(SEED_UNIVERSE) + 1
