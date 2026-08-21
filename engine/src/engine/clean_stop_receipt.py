"""N2's ``CleanStopReceipt`` (decisions.md) -- UNWIRED.

**This module is dead code from the runtime's perspective**, the same as
:mod:`engine.paperday_recovery`: importable and unit-testable here, called
from nowhere an operator can reach. ``paperday.py``'s real ``stop()`` does
not call :func:`persist_clean_stop_receipt` today, and must not until P0/P1
land per ``docs/paper-day-recovery/design.md``'s N4 ordering -- wiring it in
is explicitly deferred, out of scope for this lane.

decisions.md N2: *"``stop()`` emits a durable ``CleanStopReceipt`` asserting
clean exit, no unmatched ticks, no outbox blockers, no stale lease, no
residual opening authority. ``paper-day-status`` displays and validates; it
never manufactures the assertion."* This module provides the receipt shape
and the validator only. It builds nothing from live state itself -- a real
caller (deferred) would populate :class:`CleanStopReceipt` from
``scheduler.find_unmatched_ticks``, an outbox's ``blocking_records()``,
lease-staleness checks, and the gate's ``entry_gate`` -- and this module's
job stops at validating whatever it is handed, never inventing a clean
assertion on its own.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

__all__ = ["CleanStopReceipt", "CleanStopValidation", "persist_clean_stop_receipt", "validate_clean_stop"]


@dataclass(frozen=True)
class CleanStopReceipt:
    """The five assertions decisions.md N2 names, plus enough identity to
    bind the receipt to one session."""

    session_id: str
    clean_exit: bool
    unmatched_tick_count: int
    outbox_blocker_count: int
    stale_lease: bool
    residual_opening_authority: bool
    asserted_at: dt.datetime

    def to_record(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "clean_exit": self.clean_exit,
            "unmatched_tick_count": self.unmatched_tick_count,
            "outbox_blocker_count": self.outbox_blocker_count,
            "stale_lease": self.stale_lease,
            "residual_opening_authority": self.residual_opening_authority,
            "asserted_at": self.asserted_at.isoformat(),
        }


@dataclass(frozen=True)
class CleanStopValidation:
    receipt: CleanStopReceipt
    failures: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        return not self.failures


def validate_clean_stop(receipt: CleanStopReceipt) -> CleanStopValidation:
    """Check all five assertions and report every failure, not just the
    first -- a caller deciding whether recovery is even in scope wants to
    know everything that is wrong, not just what tripped first."""

    failures: list[str] = []
    if not receipt.clean_exit:
        failures.append("clean_exit is False")
    if receipt.unmatched_tick_count != 0:
        failures.append(f"{receipt.unmatched_tick_count} unmatched tick(s)")
    if receipt.outbox_blocker_count != 0:
        failures.append(f"{receipt.outbox_blocker_count} outbox blocker(s)")
    if receipt.stale_lease:
        failures.append("lease is stale")
    if receipt.residual_opening_authority:
        failures.append("residual opening authority present")
    return CleanStopValidation(receipt=receipt, failures=tuple(failures))


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        handle, name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(name)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()


def persist_clean_stop_receipt(path: Path, receipt: CleanStopReceipt) -> Path:
    """Persist whatever receipt it is given, clean or not -- this function
    never manufactures or improves the assertion, only records it durably
    (fsynced temp file + ``os.replace``, the same publish pattern
    ``paperday._atomic_write_json`` uses for ``gate.json``)."""

    _atomic_write_json(path, receipt.to_record())
    return path
