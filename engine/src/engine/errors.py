"""Engine exceptions and the exit codes they map to.

Exit codes are part of the CLI contract. A caller -- a shell script, a cron
entry, a supervising agent -- must be able to tell "the safety rails stopped
this" apart from "the broker was unreachable" without parsing stderr, because
those two demand opposite responses: one is a bug to fix, the other is a retry.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_REFUSED = 4
EXIT_CONNECTION = 5
EXIT_HALTED = 6
EXIT_JOURNAL = 7


class EngineError(Exception):
    """Base for every error this package raises deliberately."""

    exit_code = EXIT_ERROR

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.message}\n  hint: {self.hint}" if self.hint else self.message


class ConfigError(EngineError):
    """Configuration is missing, malformed, or points somewhere it must not."""

    exit_code = EXIT_CONFIG


class UnsafeConfigError(ConfigError):
    """Configuration would let the engine reach a live account.

    Deliberately a distinct type from ConfigError. A typo in a cap is a config
    problem; pointing at a live trading port is a different category of event
    and should never be caught by a broad ``except ConfigError`` that shrugs and
    carries on with a default.
    """


class RefusedError(EngineError):
    """A safety gate refused the order. This is the system working."""

    exit_code = EXIT_REFUSED


class HaltedError(RefusedError):
    """The kill switch is engaged."""

    exit_code = EXIT_HALTED


class InvalidStrategyError(RefusedError):
    """An option strategy violates a structural invariant and was not built.

    A distinct type from RefusedError for the same reason UnsafeConfigError is
    distinct from ConfigError: "this credit is too small" and "this structure
    contains an uncovered short call" are not the same category of event, and a
    broad ``except RefusedError`` that logs and moves to the next candidate must
    not be able to swallow the second one quietly.

    Raised at construction time, so an invalid structure cannot exist as an
    object waiting to be passed to a gate that happens not to check it.
    """


class MarketDataRefusedError(RefusedError):
    """Market data was not good enough to make a trading decision on.

    Carries a machine-readable ``reason`` because the caller's response differs
    by cause: an entitlement problem is a purchase, a stale quote is a retry,
    and a generation mismatch is a bug in the subscription lifecycle. A human
    reading the message must not be the only way to tell them apart.
    """

    def __init__(self, reason: str, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.reason = reason

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.reason}] {super().__str__()}"


class ConnectionError_(EngineError):
    """Could not reach TWS / IB Gateway, or the connection was rejected.

    Named with a trailing underscore so it never shadows the builtin
    ``ConnectionError`` for a reader skimming an ``except`` clause.
    """

    exit_code = EXIT_CONNECTION


class JournalError(EngineError):
    """The durable order journal could not be written.

    Fatal by design. The engine must not place orders it cannot record: an
    unrecorded fill is a position nobody knows about.
    """

    exit_code = EXIT_JOURNAL
