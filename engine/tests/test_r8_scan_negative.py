"""R8 adversarial fixtures for ScanBook, coverage, fairness, and pacing."""

from __future__ import annotations

import datetime as dt
import inspect
import importlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from engine.options.pacing import PacedRequestBudget, Priority, RequestKind
from engine.options.universe import (
    CoverageSummary,
    NominatedLeg,
    ScanBook,
    ScanBookAdmission,
    ScanBookFileWriter,
    ScanBookRow,
    ScanState,
    StructureNomination,
)


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 14, 14, 0, tzinfo=UTC)
TODAY = NOW.date()


def _candidate_book() -> ScanBook:
    nomination = StructureNomination(
        underlying="SPY",
        family="PUT_CREDIT_SPREAD",
        direction="BULLISH",
        expiration=TODAY + dt.timedelta(days=45),
        legs=(
            NominatedLeg(con_id=5001, strike=Decimal("500"), right="P", action="SELL"),
            NominatedLeg(con_id=5002, strike=Decimal("495"), right="P", action="BUY"),
        ),
        short_delta=Decimal("-0.30"),
        width=Decimal("5"),
    )
    row = ScanBookRow(
        symbol="SPY",
        state=ScanState.CANDIDATE,
        nomination=nomination,
        rank_score=Decimal("95"),
        evaluated_at=NOW,
    )
    return ScanBook(
        session_date=TODAY,
        generated_at=NOW,
        rows=(row,),
        coverage=CoverageSummary.from_rows((row,)),
    )


def _missing_contract(module_names: tuple[str, ...], symbols: tuple[str, ...]) -> None:
    found: list[str] = []
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name or str(exc.name).startswith(module_name + "."):
                continue
            raise
        found.append(module_name)
        if all(hasattr(module, symbol) for symbol in symbols):
            return
    pytest.skip(
        "missing contract seam: "
        + ", ".join(f"{module}.{symbol}" for module in module_names for symbol in symbols)
        + (f" (searched {', '.join(found)})" if found else "")
    )


class TestScanBookCorruptionControls:
    def test_corrupt_scanbook_is_not_read(self, tmp_path: Path) -> None:
        path = ScanBook.path_for(tmp_path, TODAY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"version": "scanbook/1",', encoding="utf-8")

        assert ScanBook.read(tmp_path, TODAY) is None

    def test_wrong_scanbook_version_is_not_read(self, tmp_path: Path) -> None:
        book = _candidate_book()
        record = book.to_record()
        record["version"] = "scanbook/999"
        path = ScanBook.path_for(tmp_path, TODAY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")

        assert ScanBook.read(tmp_path, TODAY) is None

    def test_incomplete_scanbook_cannot_be_admitted_for_entry(self, tmp_path: Path) -> None:
        book = _candidate_book()
        parameters = set(inspect.signature(book.admit).parameters)
        required = {"catalog_hash", "policy_hash", "require_complete"}
        if not required <= parameters:
            pytest.skip(
                "missing contract seam: ScanBook.admit catalog_hash/policy_hash/"
                "require_complete (baseline admits age-valid partial books)"
            )

        admission = book.admit(
            session_date=TODAY,
            now=NOW,
            max_age=dt.timedelta(minutes=30),
            catalog_hash="catalog-r8",
            policy_hash="policy-r8",
            require_complete=True,
        )
        assert admission is not ScanBookAdmission.ACCEPTED

    def test_wrong_policy_scanbook_cannot_be_admitted_for_entry(self, tmp_path: Path) -> None:
        book = _candidate_book()
        parameters = set(inspect.signature(book.admit).parameters)
        required = {"catalog_hash", "policy_hash", "require_complete"}
        if not required <= parameters:
            pytest.skip(
                "missing contract seam: manifest-bound ScanBook admission; "
                "baseline has no policy digest input"
            )

        admission = book.admit(
            session_date=TODAY,
            now=NOW,
            max_age=dt.timedelta(minutes=30),
            catalog_hash="catalog-r8",
            policy_hash="wrong-policy",
            require_complete=True,
        )
        assert admission is not ScanBookAdmission.ACCEPTED


class TestScanBookClaimRace:
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "R4 must publish claims with a version/CAS check; baseline "
            "ScanBookFileWriter performs stale read-modify-write"
        ),
    )
    def test_a_stale_scanbook_claim_cannot_overwrite_a_competing_claim(
        self, tmp_path: Path
    ) -> None:
        _candidate_book().write(tmp_path)
        competitor = ScanBookFileWriter(tmp_path, TODAY)

        class RacingWriter(ScanBookFileWriter):
            raced = False

            def _book(self) -> ScanBook:
                book = super()._book()
                if not self.raced:
                    self.raced = True
                    assert competitor.mark_claimed(
                        "SPY", entry_id="entry-2", at=NOW
                    )
                return book

        first = RacingWriter(tmp_path, TODAY)
        assert first.mark_claimed("SPY", entry_id="entry-1", at=NOW) is False

        final = ScanBook.read(tmp_path, TODAY)
        assert final is not None
        assert final.rows[0].claim_reference == "entry-2"


class TestPacingReserveControl:
    def test_discovery_cannot_spend_the_management_reserve(self) -> None:
        class ReserveExhausted(RuntimeError):
            pass

        def no_wait(seconds: float) -> None:
            raise ReserveExhausted(f"would wait {seconds}")

        budget = PacedRequestBudget(
            historical_per_window=4,
            general_per_window=4,
            management_reserve_fraction=0.5,
            clock=lambda: 0.0,
            sleeper=no_wait,
        )
        budget.acquire(RequestKind.HISTORICAL, priority=Priority.DISCOVERY)
        budget.acquire(RequestKind.HISTORICAL, priority=Priority.DISCOVERY)

        with pytest.raises(ReserveExhausted):
            budget.acquire(RequestKind.HISTORICAL, priority=Priority.DISCOVERY)

        # Exits may consume the reserved token even when discovery cannot.
        budget.acquire(RequestKind.HISTORICAL, priority=Priority.EXITS_MANAGEMENT)


class TestScanScaleContractSkips:
    def test_catalog_mutation_mid_cycle_is_rejected(self) -> None:
        _missing_contract(
            ("engine.options.catalog", "engine.options.universe_data"),
            ("CatalogSnapshot",),
        )
        pytest.fail("the catalog snapshot fixture adapter is not wired")

    def test_refresh_queue_prevents_symbol_starvation(self) -> None:
        _missing_contract(
            ("engine.options.refresh", "engine.options.universe"),
            ("RefreshLedger",),
        )
        pytest.fail("the refresh fairness fixture adapter is not wired")

    def test_deep_probe_reserves_estimated_request_cost(self) -> None:
        _missing_contract(
            ("engine.options.planner", "engine.options.universe"),
            ("reserve_probe_cost",),
        )
        pytest.fail("the cost-bounded probe fixture adapter is not wired")
