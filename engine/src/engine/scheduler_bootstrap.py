"""Production scheduler bootstrap: explicit policy in, scheduler loop out.

This module is intentionally only the policy/configuration layer above
``engine.scheduler``.  The loop knows how to tick but refuses to choose market
hours, cadence, or command.  The bootstrap loads those choices from a required
JSON file whose bytes are pinned by a required SHA-256 digest, validates that no
field was omitted or guessed, and constructs the runtime objects from those
values.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from engine.errors import ConfigError
from engine.market_calendar import CalendarError, MarketCalendar, SessionHours
from engine.runtime import EngineCommandRunner
from engine.scheduler import (
    SchedulerIdentity,
    SchedulerLoop,
    SchedulerPaths,
    SchedulerSpec,
)

POLICY_SCHEMA = "ibkr.scheduler_bootstrap/1"
MISSED_TICK_POLICY = "SKIP_MISSED_TICKS"
_SUPPORTED_MISSED_TICK_POLICIES = frozenset({MISSED_TICK_POLICY})

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "mandate",
        "calendar",
        "cadence_seconds",
        "missed_tick_policy",
        "command",
        "command_timeout_seconds",
    }
)
_CALENDAR_KEYS = frozenset(
    {
        "timezone",
        "regular_open",
        "regular_close",
        "weekend_days",
        "holidays",
        "early_closes",
    }
)
_EARLY_CLOSE_KEYS = frozenset({"date", "close"})
_HEX = frozenset("0123456789abcdefABCDEF")


@dataclass(frozen=True)
class SchedulerPolicy:
    """A fully validated scheduler policy.

    The value carries the assembled calendar so no caller has to repeat parsing
    or supply fallbacks.  Empty holiday/early-close lists are fine, but they must
    have been present in the policy JSON.
    """

    calendar: MarketCalendar
    cadence_seconds: float
    missed_tick_policy: str
    command: tuple[str, ...]
    command_timeout_seconds: float


def load_scheduler_policy(path: Path, expected_sha256: str) -> SchedulerPolicy:
    """Load and validate a production scheduler policy.

    The digest is checked before parsing.  A malformed file with the wrong bytes
    is a digest failure; a malformed file with the exact declared bytes is a
    malformed-policy failure.  Both are closed states.
    """

    raw = _read_required_bytes(Path(path))
    _verify_sha256(raw, expected_sha256, path=Path(path))
    try:
        loaded = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"scheduler policy {path} is not UTF-8 JSON",
            hint="write the policy as UTF-8 JSON and recompute its SHA-256",
        ) from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"scheduler policy {path} is malformed JSON: {exc.msg}",
            hint="fix the JSON bytes and recompute the required SHA-256",
        ) from exc

    if not isinstance(loaded, dict):
        raise ConfigError(
            "scheduler policy root must be a JSON object",
            hint="a list or scalar cannot declare every required scheduler field",
        )

    return parse_scheduler_policy(loaded)


def parse_scheduler_policy(record: dict[str, Any]) -> SchedulerPolicy:
    """Validate an already-loaded policy object."""

    _require_exact_keys(record, _TOP_LEVEL_KEYS, "scheduler policy")
    schema = record["schema"]
    if schema != POLICY_SCHEMA:
        raise ConfigError(
            f"unknown scheduler policy schema {schema!r}",
            hint=f"expected {POLICY_SCHEMA!r}; refusing to guess another schema",
        )

    mandate = record["mandate"]
    if mandate != "MANAGE_ONLY":
        raise ConfigError(
            "scheduler policy mandate must be MANAGE_ONLY",
            hint="the production scheduler may manage existing option positions; "
            "it must not enable new entries",
        )

    calendar = _calendar_from(record["calendar"])
    cadence = _positive_number(record["cadence_seconds"], "cadence_seconds")
    missed_tick_policy = record["missed_tick_policy"]
    if not isinstance(missed_tick_policy, str) or (
        missed_tick_policy not in _SUPPORTED_MISSED_TICK_POLICIES
    ):
        raise ConfigError(
            f"unsupported scheduler missed_tick_policy {missed_tick_policy!r}",
            hint=f"expected {MISSED_TICK_POLICY!r}; the scheduler does not catch up missed ticks",
        )
    timeout = _positive_number(
        record["command_timeout_seconds"], "command_timeout_seconds"
    )
    command = _management_command(record["command"])
    return SchedulerPolicy(
        calendar=calendar,
        cadence_seconds=cadence,
        missed_tick_policy=missed_tick_policy,
        command=command,
        command_timeout_seconds=timeout,
    )


def build_scheduler_loop(
    *,
    identity: SchedulerIdentity,
    state_dir: Path,
    schedule_config: Path,
    schedule_config_sha256: str,
    engine: Any | None = None,
    clock: Callable[[], Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> SchedulerLoop:
    """Construct the real scheduler loop from a pinned policy file."""

    policy = load_scheduler_policy(schedule_config, schedule_config_sha256)
    paths = SchedulerPaths(root=Path(state_dir) / "paperday")
    loop_args: dict[str, Any] = {
        "identity": identity,
        "paths": paths,
        "lock": paths.root / "session.lock",
        "cadence_seconds": policy.cadence_seconds,
        "is_open": policy.calendar.is_open,
        "command": policy.command,
        "engine": engine if engine is not None else EngineCommandRunner(Path(state_dir)),
        "command_timeout": policy.command_timeout_seconds,
    }
    if clock is not None:
        loop_args["clock"] = clock
    if sleep is not None:
        loop_args["sleep"] = sleep
    if monotonic is not None:
        loop_args["monotonic"] = monotonic
    return SchedulerLoop(**loop_args)


def build_scheduler_spec(
    *,
    schedule_config: Path,
    schedule_config_sha256: str,
    state_dir: Path,
    entry_script: Path,
) -> SchedulerSpec:
    """Build the controller-facing spec from the same pinned policy.

    The paper-day controller needs a ``SchedulerSpec`` to supervise the child;
    the child then loads the same policy again through :func:`build_scheduler_loop`.
    Requiring the entrypoint explicitly keeps deployment path selection visible
    and prevents a missing script from becoming a readiness timeout.
    """

    policy = load_scheduler_policy(schedule_config, schedule_config_sha256)
    return SchedulerSpec(
        cadence_seconds=policy.cadence_seconds,
        command=policy.command,
        entry_script=Path(entry_script),
        entry_args=(
            f"--schedule-config={Path(schedule_config)}",
            f"--schedule-config-sha256={schedule_config_sha256}",
            f"--state-dir={Path(state_dir)}",
        ),
    )


def _read_required_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(
            f"scheduler policy file {path} does not exist",
            hint="pass --schedule-config pointing at the operator-reviewed policy",
        ) from exc
    except OSError as exc:
        raise ConfigError(f"could not read scheduler policy file {path}: {exc}") from exc


def _verify_sha256(raw: bytes, expected: str, *, path: Path) -> None:
    if not isinstance(expected, str) or len(expected) != 64 or any(
        char not in _HEX for char in expected
    ):
        raise ConfigError(
            "schedule-config-sha256 must be a 64-character hex SHA-256 digest",
            hint="pin the exact reviewed policy bytes; no digest means no schedule",
        )
    actual = hashlib.sha256(raw).hexdigest()
    if actual.lower() != expected.lower():
        raise ConfigError(
            f"scheduler policy digest mismatch for {path}",
            hint=f"expected {expected.lower()}, got {actual}; refusing unreviewed bytes",
        )


def _require_exact_keys(
    record: dict[str, Any], expected: frozenset[str], label: str
) -> None:
    missing = sorted(expected - record.keys())
    if missing:
        raise ConfigError(
            f"{label} is missing required field(s): {', '.join(missing)}",
            hint="there are no scheduler defaults; every policy field must be explicit",
        )
    unknown = sorted(record.keys() - expected)
    if unknown:
        raise ConfigError(
            f"{label} has unknown field(s): {', '.join(unknown)}",
            hint="unknown policy fields may mean a different schema; refusing to ignore them",
        )


def _calendar_from(value: Any) -> MarketCalendar:
    if not isinstance(value, dict):
        raise ConfigError("calendar must be a JSON object")
    _require_exact_keys(value, _CALENDAR_KEYS, "calendar")

    timezone = _zone(value["timezone"])
    try:
        regular_hours = SessionHours(
            opens_at=_wall_time(value["regular_open"], "calendar.regular_open"),
            closes_at=_wall_time(value["regular_close"], "calendar.regular_close"),
        )
    except CalendarError as exc:
        raise ConfigError(
            "scheduler calendar data is invalid",
            hint=str(exc),
        ) from exc
    weekend_days = _weekend_days(value["weekend_days"])
    holidays = _dates(value["holidays"], "calendar.holidays")
    early_closes = _early_closes(value["early_closes"])
    try:
        return MarketCalendar(
            timezone=timezone,
            regular_hours=regular_hours,
            weekend_days=weekend_days,
            holidays=holidays,
            early_closes=early_closes,
        )
    except CalendarError as exc:
        raise ConfigError(
            "scheduler calendar data is invalid",
            hint=str(exc),
        ) from exc


def _zone(value: Any) -> ZoneInfo:
    if not isinstance(value, str) or not value:
        raise ConfigError("calendar.timezone must be a non-empty IANA timezone name")
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(
            f"calendar.timezone {value!r} is not available",
            hint="use an explicit IANA timezone such as 'America/New_York'",
        ) from exc


def _wall_time(value: Any, label: str) -> time:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label} must be a non-empty ISO wall-clock time")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{label} is not an ISO wall-clock time: {value!r}") from exc
    if parsed.tzinfo is not None:
        raise ConfigError(
            f"{label} must not carry a timezone",
            hint="the timezone belongs to calendar.timezone, not to one clock field",
        )
    return parsed


def _weekend_days(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ConfigError(
            "calendar.weekend_days must be a JSON array",
            hint="an empty array is allowed only when the market really trades every day",
        )
    return tuple(value)


def _dates(value: Any, label: str) -> tuple[date, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{label} must be a JSON array")
    parsed: list[date] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ConfigError(f"{label}[{index}] must be an ISO date string")
        try:
            parsed.append(date.fromisoformat(item))
        except ValueError as exc:
            raise ConfigError(f"{label}[{index}] is not an ISO date: {item!r}") from exc
    return tuple(parsed)


def _early_closes(value: Any) -> tuple[tuple[date, time], ...]:
    if not isinstance(value, list):
        raise ConfigError("calendar.early_closes must be a JSON array")
    parsed: list[tuple[date, time]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ConfigError(f"calendar.early_closes[{index}] must be an object")
        _require_exact_keys(
            item, _EARLY_CLOSE_KEYS, f"calendar.early_closes[{index}]"
        )
        day = _dates([item["date"]], f"calendar.early_closes[{index}].date")[0]
        close = _wall_time(
            item["close"], f"calendar.early_closes[{index}].close"
        )
        parsed.append((day, close))
    return tuple(parsed)


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} must be a positive number")
    if value <= 0:
        raise ConfigError(
            f"{label} must be positive",
            hint="a non-positive scheduler bound is a busy loop or an instant timeout",
        )
    return float(value)


def _management_command(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError("command must be a JSON array of CLI tokens")
    if not value:
        raise ConfigError(
            "command must not be empty",
            hint="the scheduler does not choose which engine command to run",
        )
    if any(not isinstance(token, str) or not token for token in value):
        raise ConfigError("command tokens must be non-empty strings")
    command = tuple(value)
    if command[0] != "options-run":
        raise ConfigError(
            "scheduler command must be options-run",
            hint="production scheduling is restricted to the options management pass",
        )
    if "--arm" not in command:
        raise ConfigError(
            "scheduler options-run command must include --arm",
            hint="management actions that close or cancel live orders must be explicitly armed",
        )
    if "--enable-entry" in command:
        raise ConfigError(
            "scheduler options-run command must not include --enable-entry",
            hint="the scheduler mandate is MANAGE_ONLY; new entries remain disabled",
        )
    return command
