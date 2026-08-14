"""Strict, hash-pinned policy for the unattended options-cycle worker.

The legacy ``ibkr.scheduler_bootstrap/1`` document is intentionally left
untouched.  It describes one management-only command and remains a useful
compatibility contract.  This module describes the larger control plane: one
worker that owns all four cadences, its session windows, its catalog identity,
and the limits that make an ``ARMED`` policy auditable.

There are no defaults in this parser.  A missing cadence, window, limit, hash,
or reserve is a configuration error rather than an invitation to inherit a
value from code.  The policy bytes are verified before they are parsed, and the
result carries that digest so callers can put it in a paper-day fingerprint and
every downstream handoff.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from engine.errors import ConfigError
from engine.market_calendar import CalendarError, MarketCalendar, SessionHours

AUTOTRADER_POLICY_SCHEMA = "ibkr.autotrader/1"
MISSED_TICK_POLICY = "SKIP_MISSED_TICKS"

MANAGE_ONLY = "MANAGE_ONLY"
FULL = "FULL"
DRY_RUN = "DRY_RUN"
SHADOW = "SHADOW"
REVIEW_ONLY = "REVIEW_ONLY"
ARMED = "ARMED"

FAILURE_CODES = (
    "FAIL-STALE-PAPERDAY-AUTHORITY",
    "FAIL-UNMATCHED-TICK",
    "FAIL-RECOVERY-BLOCKED",
    "FAIL-EXECUTION-OUTBOX",
    "FAIL-BROKER-AMBIGUOUS",
    "FAIL-APPROVAL-REVISION-STALE",
    "FAIL-APPROVAL-TTL",
    "FAIL-LEASE-MISSING",
    "FAIL-DUPLICATE-SAGA",
    "FAIL-REPRICE-BUDGET",
    "FAIL-CATALOG-HASH",
    "FAIL-INCOMPLETE-COVERAGE",
    "FAIL-SCAN-CLAIM-RACE",
    "FAIL-REFRESH-STARVATION",
    "FAIL-UNSHARED-PACING",
    "FAIL-UNBOUNDED-BROKER-LOAD",
    "FAIL-CADENCE-DRIFT",
    "FAIL-MISSED-TICK-CATCHUP",
    "FAIL-UNAUTHORIZED-ENTRY",
)

_HEX = frozenset("0123456789abcdefABCDEF")
_SCHEMAS = frozenset({AUTOTRADER_POLICY_SCHEMA})
_MANDATES = frozenset({MANAGE_ONLY, FULL})
_MODES = frozenset({DRY_RUN, SHADOW, REVIEW_ONLY, ARMED})
_WINDOW_KINDS = frozenset(
    {"SESSION", "PRE_OPEN", "SESSION_RELATIVE", "WALL_CLOCK"}
)
_WINDOW_KEYS = frozenset({"kind", "start", "end", "minutes_before_close"})


@dataclass(frozen=True)
class JobCadences:
    """Fixed-rate intervals, in seconds, for the four worker jobs."""

    management_seconds: float
    discovery_seconds: float
    probe_seconds: float
    entry_seconds: float


@dataclass(frozen=True)
class WindowSpec:
    """A validated, explicit worker window.

    ``SESSION`` means the complete regular session. ``PRE_OPEN`` is the
    read-only breadth window before the open. ``SESSION_RELATIVE`` currently
    supports ``OPEN`` through ``CLOSE_MINUS`` with an explicit number of
    minutes. ``WALL_CLOCK`` is a local wall-clock interval in the calendar's
    timezone. Future venues can add another kind only by changing this schema.
    """

    kind: str
    start: str | None = None
    end: str | None = None
    minutes_before_close: int | None = None


@dataclass(frozen=True)
class CatalogPin:
    path: Path
    version: str
    sha256: str


@dataclass(frozen=True)
class DiscoveryLimits:
    refresh_limit: int
    phase2_limit: int
    coverage_sla_seconds: float


@dataclass(frozen=True)
class EntryLimits:
    max_pending_entries: int
    max_new_openings_per_pass: int
    transmission_limit_per_session: int
    review_ttl_seconds: float
    packet_ttl_seconds: float


@dataclass(frozen=True)
class PacingReserve:
    management_fraction: float
    discovery_fraction: float
    minimum_management_requests: int


@dataclass(frozen=True)
class AutotraderPolicy:
    """The complete, immutable policy consumed by the application tier."""

    schema: str
    mandate: str
    mode: str
    calendar: MarketCalendar
    cadences: JobCadences
    missed_tick_policy: str
    windows: Mapping[str, WindowSpec]
    worker_command: tuple[str, ...]
    command_timeout_seconds: float
    state_dir: Path
    catalog: CatalogPin
    discovery: DiscoveryLimits
    entry: EntryLimits
    pacing_reserve: PacingReserve
    policy_hash: str | None = None

    @property
    def entry_enabled(self) -> bool:
        return self.mandate == FULL and self.mode in {REVIEW_ONLY, ARMED}

    @property
    def armed(self) -> bool:
        return self.mandate == FULL and self.mode == ARMED

    def fingerprint_record(self) -> dict[str, Any]:
        """Return a canonical, JSON-safe policy identity for downstream CAS."""

        return {
            "schema": self.schema,
            "mandate": self.mandate,
            "mode": self.mode,
            "calendar": _calendar_record(self.calendar),
            "cadences": {
                "management_seconds": self.cadences.management_seconds,
                "discovery_seconds": self.cadences.discovery_seconds,
                "probe_seconds": self.cadences.probe_seconds,
                "entry_seconds": self.cadences.entry_seconds,
            },
            "missed_tick_policy": self.missed_tick_policy,
            "windows": {
                name: {
                    "kind": spec.kind,
                    "start": spec.start,
                    "end": spec.end,
                    "minutes_before_close": spec.minutes_before_close,
                }
                for name, spec in self.windows.items()
            },
            "worker_command": list(self.worker_command),
            "command_timeout_seconds": self.command_timeout_seconds,
            "state_dir": str(self.state_dir),
            "catalog": {
                "path": str(self.catalog.path),
                "version": self.catalog.version,
                "sha256": self.catalog.sha256,
            },
            "discovery": {
                "refresh_limit": self.discovery.refresh_limit,
                "phase2_limit": self.discovery.phase2_limit,
                "coverage_sla_seconds": self.discovery.coverage_sla_seconds,
            },
            "entry": {
                "max_pending_entries": self.entry.max_pending_entries,
                "max_new_openings_per_pass": self.entry.max_new_openings_per_pass,
                "transmission_limit_per_session": self.entry.transmission_limit_per_session,
                "review_ttl_seconds": self.entry.review_ttl_seconds,
                "packet_ttl_seconds": self.entry.packet_ttl_seconds,
            },
            "pacing_reserve": {
                "management_fraction": self.pacing_reserve.management_fraction,
                "discovery_fraction": self.pacing_reserve.discovery_fraction,
                "minimum_management_requests": self.pacing_reserve.minimum_management_requests,
            },
        }


def load_autotrader_policy(path: Path, expected_sha256: str) -> AutotraderPolicy:
    """Read a UTF-8 policy after verifying its exact SHA-256 bytes."""

    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(
            f"autotrader policy file {path} does not exist",
            hint="pass the operator-reviewed policy artifact and its SHA-256",
        ) from exc
    except OSError as exc:
        raise ConfigError(f"could not read autotrader policy file {path}: {exc}") from exc

    _verify_sha256(raw, expected_sha256, path)
    try:
        loaded = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"autotrader policy {path} is not UTF-8 JSON") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"autotrader policy {path} is malformed JSON: {exc.msg}"
        ) from exc
    if not isinstance(loaded, dict):
        raise ConfigError("autotrader policy root must be a JSON object")
    return parse_autotrader_policy(loaded, policy_hash=expected_sha256.lower())


def parse_autotrader_policy(
    record: dict[str, Any], *, policy_hash: str | None = None
) -> AutotraderPolicy:
    """Validate an already-decoded ``ibkr.autotrader/1`` record."""

    _require_exact_keys(
        record,
        {
            "schema",
            "mandate",
            "mode",
            "calendar",
            "cadences",
            "missed_tick_policy",
            "windows",
            "worker_command",
            "command_timeout_seconds",
            "state_dir",
            "catalog",
            "discovery",
            "entry",
            "pacing_reserve",
        },
        "autotrader policy",
    )
    if record["schema"] not in _SCHEMAS:
        raise ConfigError(
            f"unknown autotrader policy schema {record['schema']!r}",
            hint=f"expected {AUTOTRADER_POLICY_SCHEMA!r}",
        )
    mandate = _one_of(record["mandate"], _MANDATES, "mandate")
    mode = _one_of(record["mode"], _MODES, "mode")
    if mode in {REVIEW_ONLY, ARMED} and mandate != FULL:
        raise ConfigError(
            f"mode {mode} requires mandate FULL",
            hint="MANAGE_ONLY cannot create, review, or transmit an opening",
        )
    if mode == ARMED and "--arm" not in _string_tuple(record["worker_command"], "worker_command"):
        raise ConfigError(
            "ARMED autotrader policy requires --arm inside the pinned worker_command",
            hint="do not add --arm through an un-hashed CLI override",
        )

    command = _string_tuple(record["worker_command"], "worker_command")
    if command[0] != "options-cycle":
        raise ConfigError(
            "worker_command must start with options-cycle",
            hint="the unattended path is one persistent broker-owning worker",
        )
    for token in command:
        if token in {
            "--mode",
            "--mandate",
            "--enable-entry",
            "--policy-hash",
            # The scheduler supplies these from the bytes it verified.  A
            # policy-owned copy would create two authorities and, for a
            # self-referential policy hash, an impossible artifact.
            "--schedule-config",
            "--schedule-config-sha256",
            "--state-dir",
            # Scheduler identity is runtime authority, not policy input.  If
            # a policy could pin it, a copied command could impersonate a
            # different paper-day lease while keeping the same policy hash.
            "--scheduler-session",
        } or any(
            token.startswith(prefix)
            for prefix in (
                "--schedule-config=",
                "--schedule-config-sha256=",
                "--state-dir=",
                "--scheduler-session=",
            )
        ):
            raise ConfigError(
                f"worker_command cannot override policy through {token}",
                hint="mode and mandate belong to the hash-pinned policy artifact",
            )

    calendar = _calendar_from(record["calendar"])
    cadences = _cadences_from(record["cadences"])
    missed = record["missed_tick_policy"]
    if missed != MISSED_TICK_POLICY:
        raise ConfigError(
            f"unsupported autotrader missed_tick_policy {missed!r}",
            hint=f"expected {MISSED_TICK_POLICY!r}; missed slots are never burst-replayed",
        )
    windows = _windows_from(record["windows"])
    timeout = _positive_number(record["command_timeout_seconds"], "command_timeout_seconds")
    state_dir = _absolute_path(record["state_dir"], "state_dir")
    catalog = _catalog_from(record["catalog"])
    discovery = _discovery_from(record["discovery"])
    entry = _entry_from(record["entry"])
    if entry.max_new_openings_per_pass != 1:
        raise ConfigError(
            "entry.max_new_openings_per_pass must be exactly 1",
            hint="the unattended worker has one-opening-per-eligible-pass policy",
        )
    pacing = _pacing_from(record["pacing_reserve"])
    if policy_hash is not None:
        _digest(policy_hash, "policy_hash")

    return AutotraderPolicy(
        schema=AUTOTRADER_POLICY_SCHEMA,
        mandate=mandate,
        mode=mode,
        calendar=calendar,
        cadences=cadences,
        missed_tick_policy=missed,
        windows=MappingProxyType(windows),
        worker_command=command,
        command_timeout_seconds=timeout,
        state_dir=state_dir,
        catalog=catalog,
        discovery=discovery,
        entry=entry,
        pacing_reserve=pacing,
        policy_hash=policy_hash.lower() if policy_hash else None,
    )


def _verify_sha256(raw: bytes, expected: str, path: Path) -> None:
    _digest(expected, "schedule-config-sha256")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected.lower():
        raise ConfigError(
            f"autotrader policy digest mismatch for {path}: expected {expected}, got {actual}",
            hint="the policy bytes changed; review them and update the pinned digest",
        )


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in _HEX for c in value):
        raise ConfigError(f"{label} must be a 64-character hex SHA-256 digest")
    return value.lower()


def _require_exact_keys(record: Any, expected: set[str], label: str) -> None:
    if not isinstance(record, dict):
        raise ConfigError(f"{label} must be a JSON object")
    actual = set(record)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise ConfigError(f"{label} missing required field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise ConfigError(f"{label} has unknown field(s): {', '.join(sorted(unknown))}")


def _one_of(value: Any, allowed: frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ConfigError(f"{label} must be one of {sorted(allowed)}, got {value!r}")
    return value


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ConfigError(f"{label} must be a non-empty JSON array of non-empty strings")
    return tuple(value)


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{label} must be a positive number")
    return float(value)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{label} must be a positive integer")
    return value


def _fraction(value: Any, label: str) -> float:
    number = _positive_number(value, label)
    if number > 1:
        raise ConfigError(f"{label} must be in the interval (0, 1]")
    return number


def _absolute_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError(
            f"{label} must be absolute",
            hint="one explicit state directory prevents split-brain paper-day state",
        )
    return path


def _cadences_from(value: Any) -> JobCadences:
    expected = {"management_seconds", "discovery_seconds", "probe_seconds", "entry_seconds"}
    _require_exact_keys(value, expected, "cadences")
    return JobCadences(
        management_seconds=_positive_number(value["management_seconds"], "cadences.management_seconds"),
        discovery_seconds=_positive_number(value["discovery_seconds"], "cadences.discovery_seconds"),
        probe_seconds=_positive_number(value["probe_seconds"], "cadences.probe_seconds"),
        entry_seconds=_positive_number(value["entry_seconds"], "cadences.entry_seconds"),
    )


def _windows_from(value: Any) -> dict[str, WindowSpec]:
    names = {"management", "breadth_discovery", "candidate_probe", "entry"}
    _require_exact_keys(value, names, "windows")
    parsed: dict[str, WindowSpec] = {}
    for name in sorted(names):
        record = value[name]
        if not isinstance(record, dict):
            raise ConfigError(f"windows.{name} must be a JSON object")
        kind = record.get("kind")
        if kind not in _WINDOW_KINDS:
            raise ConfigError(f"windows.{name}.kind must be one of {sorted(_WINDOW_KINDS)}")
        allowed = {"kind"}
        if kind == "SESSION_RELATIVE":
            allowed = {"kind", "start", "end", "minutes_before_close"}
        elif kind == "WALL_CLOCK":
            allowed = {"kind", "start", "end"}
        _require_exact_keys(record, allowed, f"windows.{name}")
        if kind == "SESSION" or kind == "PRE_OPEN":
            parsed[name] = WindowSpec(kind=kind)
            continue
        if kind == "SESSION_RELATIVE":
            if record["start"] != "OPEN" or record["end"] != "CLOSE_MINUS":
                raise ConfigError(
                    f"windows.{name} SESSION_RELATIVE must be OPEN through CLOSE_MINUS"
                )
            minutes = _positive_int(
                record["minutes_before_close"],
                f"windows.{name}.minutes_before_close",
            )
            parsed[name] = WindowSpec(
                kind=kind,
                start="OPEN",
                end="CLOSE_MINUS",
                minutes_before_close=minutes,
            )
            continue
        _wall_time(record["start"], f"windows.{name}.start")
        _wall_time(record["end"], f"windows.{name}.end")
        if _parse_time(record["start"]) >= _parse_time(record["end"]):
            raise ConfigError(f"windows.{name} wall-clock start must be before end")
        parsed[name] = WindowSpec(kind=kind, start=record["start"], end=record["end"])
    return parsed


def _catalog_from(value: Any) -> CatalogPin:
    _require_exact_keys(value, {"path", "version", "sha256"}, "catalog")
    return CatalogPin(
        path=_absolute_path(value["path"], "catalog.path"),
        version=_nonempty(value["version"], "catalog.version"),
        sha256=_digest(value["sha256"], "catalog.sha256"),
    )


def _discovery_from(value: Any) -> DiscoveryLimits:
    _require_exact_keys(value, {"refresh_limit", "phase2_limit", "coverage_sla_seconds"}, "discovery")
    return DiscoveryLimits(
        refresh_limit=_positive_int(value["refresh_limit"], "discovery.refresh_limit"),
        phase2_limit=_positive_int(value["phase2_limit"], "discovery.phase2_limit"),
        coverage_sla_seconds=_positive_number(
            value["coverage_sla_seconds"], "discovery.coverage_sla_seconds"
        ),
    )


def _entry_from(value: Any) -> EntryLimits:
    _require_exact_keys(
        value,
        {
            "max_pending_entries",
            "max_new_openings_per_pass",
            "transmission_limit_per_session",
            "review_ttl_seconds",
            "packet_ttl_seconds",
        },
        "entry",
    )
    return EntryLimits(
        max_pending_entries=_positive_int(value["max_pending_entries"], "entry.max_pending_entries"),
        max_new_openings_per_pass=_positive_int(
            value["max_new_openings_per_pass"], "entry.max_new_openings_per_pass"
        ),
        transmission_limit_per_session=_positive_int(
            value["transmission_limit_per_session"], "entry.transmission_limit_per_session"
        ),
        review_ttl_seconds=_positive_number(value["review_ttl_seconds"], "entry.review_ttl_seconds"),
        packet_ttl_seconds=_positive_number(value["packet_ttl_seconds"], "entry.packet_ttl_seconds"),
    )


def _pacing_from(value: Any) -> PacingReserve:
    _require_exact_keys(
        value,
        {"management_fraction", "discovery_fraction", "minimum_management_requests"},
        "pacing_reserve",
    )
    management = _fraction(value["management_fraction"], "pacing_reserve.management_fraction")
    discovery = _fraction(value["discovery_fraction"], "pacing_reserve.discovery_fraction")
    if management + discovery > 1:
        raise ConfigError("pacing_reserve fractions cannot sum above 1")
    return PacingReserve(
        management_fraction=management,
        discovery_fraction=discovery,
        minimum_management_requests=_positive_int(
            value["minimum_management_requests"],
            "pacing_reserve.minimum_management_requests",
        ),
    )


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    return value.strip()


def _parse_time(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"invalid wall-clock time {value!r}") from exc
    if parsed.tzinfo is not None or parsed.second or parsed.microsecond:
        raise ConfigError(f"wall-clock time {value!r} must be HH:MM without a timezone")
    return parsed


def _wall_time(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be an ISO wall-clock time")
    try:
        _parse_time(value)
    except ConfigError as exc:
        raise ConfigError(f"{label} is invalid: {value!r}") from exc


def _calendar_from(value: Any) -> MarketCalendar:
    expected = {
        "timezone",
        "regular_open",
        "regular_close",
        "weekend_days",
        "holidays",
        "early_closes",
    }
    _require_exact_keys(value, expected, "calendar")
    timezone_name = _nonempty(value["timezone"], "calendar.timezone")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"calendar.timezone {timezone_name!r} is not available") from exc
    _wall_time(value["regular_open"], "calendar.regular_open")
    _wall_time(value["regular_close"], "calendar.regular_close")
    try:
        hours = SessionHours(_parse_time(value["regular_open"]), _parse_time(value["regular_close"]))
        weekend_days = tuple(value["weekend_days"])
        holidays = tuple(_parse_date(item, f"calendar.holidays[{index}]") for index, item in enumerate(value["holidays"]))
        early_closes = tuple(
            (_parse_date(item["date"], f"calendar.early_closes[{index}].date"), _parse_time(item["close"]))
            for index, item in enumerate(value["early_closes"])
        )
        return MarketCalendar(
            timezone=zone,
            regular_hours=hours,
            weekend_days=weekend_days,
            holidays=holidays,
            early_closes=early_closes,
        )
    except (TypeError, KeyError, ValueError, CalendarError) as exc:
        raise ConfigError(f"invalid calendar data: {exc}") from exc


def _parse_date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{label} is not an ISO date: {value!r}") from exc


def _calendar_record(calendar: MarketCalendar) -> dict[str, Any]:
    return {
        "timezone": str(calendar.timezone),
        "regular_open": calendar.regular_hours.opens_at.isoformat(timespec="minutes"),
        "regular_close": calendar.regular_hours.closes_at.isoformat(timespec="minutes"),
        "weekend_days": list(calendar.weekend_days),
        "holidays": [day.isoformat() for day in calendar.holidays],
        "early_closes": [
            {"date": day.isoformat(), "close": close.isoformat(timespec="minutes")}
            for day, close in calendar.early_closes
        ],
    }


__all__ = [
    "ARMED",
    "AUTOTRADER_POLICY_SCHEMA",
    "AutotraderPolicy",
    "CatalogPin",
    "DRY_RUN",
    "DiscoveryLimits",
    "EntryLimits",
    "FAILURE_CODES",
    "FULL",
    "JobCadences",
    "MANAGE_ONLY",
    "MISSED_TICK_POLICY",
    "PacingReserve",
    "REVIEW_ONLY",
    "SHADOW",
    "WindowSpec",
    "load_autotrader_policy",
    "parse_autotrader_policy",
]
