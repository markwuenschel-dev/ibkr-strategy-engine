"""Exception hierarchy and the process exit codes they map to.

Exit codes are part of the CLI contract -- scripts and CI depend on them, so
they are defined once here rather than scattered as literals across commands.
"""

from __future__ import annotations

# Exit codes. 0/1/2 follow the usual UNIX convention; 3+ are collab-kit
# specific so a caller can distinguish "not found" from "someone beat me to it"
# without parsing stderr.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 4
EXIT_LOCKED = 5
EXIT_INVALID = 6


class CollabKitError(Exception):
    """Base class for every error this kit raises deliberately.

    ``exit_code`` lets ``main()`` translate any expected failure into the right
    process status without a chain of isinstance checks.
    """

    exit_code = EXIT_ERROR

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.hint:
            return f"{self.message}\n  hint: {self.hint}"
        return self.message


class UsageError(CollabKitError):
    """The caller passed arguments that cannot be interpreted."""

    exit_code = EXIT_USAGE


class NotFoundError(CollabKitError):
    """A named collab, handoff, or path does not exist."""

    exit_code = EXIT_NOT_FOUND


class ConflictError(CollabKitError):
    """The operation lost a race, or the target is already in the goal state.

    Raised by claim() when another seat won, and by register() when a name is
    already taken.
    """

    exit_code = EXIT_CONFLICT


class LockTimeout(CollabKitError):
    """Could not acquire an advisory lock within the deadline."""

    exit_code = EXIT_LOCKED


class ValidationError(CollabKitError):
    """Input is well-formed enough to parse but violates a rule.

    Covers frontmatter schema violations and rejected (traversal-prone) names.
    """

    exit_code = EXIT_INVALID
