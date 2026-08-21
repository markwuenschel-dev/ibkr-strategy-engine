"""N2's CleanStopReceipt (decisions.md): a durable assertion that a stop was
clean -- no unmatched ticks, no outbox blockers, no stale lease, no residual
opening authority. Small, separate, and UNWIRED -- see
``engine.clean_stop_receipt``'s module docstring for the same dead-code
constraint ``engine.paperday_recovery`` states. Referenced by acceptance-bar
requirement 8 and by decisions.md D11.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from engine.clean_stop_receipt import (
    CleanStopReceipt,
    persist_clean_stop_receipt,
    validate_clean_stop,
)

NOW = dt.datetime(2026, 8, 20, 12, 0, 0, tzinfo=dt.timezone.utc)


def _clean_receipt() -> CleanStopReceipt:
    return CleanStopReceipt(
        session_id="paperday-20260820-5f6c822e",
        clean_exit=True,
        unmatched_tick_count=0,
        outbox_blocker_count=0,
        stale_lease=False,
        residual_opening_authority=False,
        asserted_at=NOW,
    )


class TestValidateCleanStop:
    def test_a_fully_clean_receipt_validates(self) -> None:
        validation = validate_clean_stop(_clean_receipt())

        assert validation.is_clean is True
        assert validation.failures == ()

    def test_dirty_exit_fails(self) -> None:
        receipt = CleanStopReceipt(
            session_id="s1",
            clean_exit=False,
            unmatched_tick_count=0,
            outbox_blocker_count=0,
            stale_lease=False,
            residual_opening_authority=False,
            asserted_at=NOW,
        )

        validation = validate_clean_stop(receipt)

        assert validation.is_clean is False
        assert any("clean_exit" in failure for failure in validation.failures)

    def test_unmatched_ticks_fail(self) -> None:
        receipt = CleanStopReceipt(
            session_id="s1",
            clean_exit=True,
            unmatched_tick_count=2,
            outbox_blocker_count=0,
            stale_lease=False,
            residual_opening_authority=False,
            asserted_at=NOW,
        )

        validation = validate_clean_stop(receipt)

        assert validation.is_clean is False
        assert any("unmatched tick" in failure for failure in validation.failures)

    def test_outbox_blockers_fail(self) -> None:
        receipt = CleanStopReceipt(
            session_id="s1",
            clean_exit=True,
            unmatched_tick_count=0,
            outbox_blocker_count=1,
            stale_lease=False,
            residual_opening_authority=False,
            asserted_at=NOW,
        )

        validation = validate_clean_stop(receipt)

        assert validation.is_clean is False
        assert any("outbox" in failure for failure in validation.failures)

    def test_stale_lease_fails(self) -> None:
        receipt = CleanStopReceipt(
            session_id="s1",
            clean_exit=True,
            unmatched_tick_count=0,
            outbox_blocker_count=0,
            stale_lease=True,
            residual_opening_authority=False,
            asserted_at=NOW,
        )

        validation = validate_clean_stop(receipt)

        assert validation.is_clean is False
        assert any("lease" in failure for failure in validation.failures)

    def test_residual_opening_authority_fails(self) -> None:
        receipt = CleanStopReceipt(
            session_id="s1",
            clean_exit=True,
            unmatched_tick_count=0,
            outbox_blocker_count=0,
            stale_lease=False,
            residual_opening_authority=True,
            asserted_at=NOW,
        )

        validation = validate_clean_stop(receipt)

        assert validation.is_clean is False
        assert any("opening authority" in failure for failure in validation.failures)

    def test_multiple_failures_are_all_reported_not_just_the_first(self) -> None:
        receipt = CleanStopReceipt(
            session_id="s1",
            clean_exit=False,
            unmatched_tick_count=3,
            outbox_blocker_count=2,
            stale_lease=True,
            residual_opening_authority=True,
            asserted_at=NOW,
        )

        validation = validate_clean_stop(receipt)

        assert len(validation.failures) == 5


class TestPersistCleanStopReceipt:
    def test_persists_the_receipt_durably(self, tmp_path: Path) -> None:
        path = tmp_path / "clean-stop-receipt.json"

        persist_clean_stop_receipt(path, _clean_receipt())

        assert path.exists()
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["session_id"] == "paperday-20260820-5f6c822e"
        assert on_disk["clean_exit"] is True
        assert on_disk["asserted_at"] == NOW.isoformat()

    def test_persists_a_dirty_receipt_too_the_assertion_is_not_manufactured(
        self, tmp_path: Path
    ) -> None:
        """decisions.md N2: status() 'never manufactures the assertion' --
        this function persists whatever receipt it is given, clean or not."""
        path = tmp_path / "clean-stop-receipt.json"
        receipt = CleanStopReceipt(
            session_id="s1",
            clean_exit=False,
            unmatched_tick_count=1,
            outbox_blocker_count=0,
            stale_lease=False,
            residual_opening_authority=False,
            asserted_at=NOW,
        )

        persist_clean_stop_receipt(path, receipt)

        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["clean_exit"] is False
        assert on_disk["unmatched_tick_count"] == 1
