"""Command-line entrypoint for the production scheduler process."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Sequence

from engine.errors import EXIT_CONFIG, EXIT_OK, EXIT_USAGE, ConfigError
from engine.scheduler import SchedulerIdentity
from engine.scheduler_bootstrap import build_scheduler_loop


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engine-scheduler")
    parser.add_argument("--scheduler-session", required=True)
    parser.add_argument("--schedule-config", required=True, type=Path)
    parser.add_argument("--schedule-config-sha256", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument(
        "--cadence-seconds",
        type=float,
        help="supervisor envelope value; verified against the hash-pinned policy",
    )
    try:
        # ``main()`` is the real process entrypoint as well as an injectable
        # test seam.  Falling back to an empty list makes the production
        # supervisor ignore the exact argv that bootstrap assembled and fail
        # with a misleading "required argument" usage error.
        raw = list(sys.argv[1:] if argv is None else argv)
        if "--" in raw:
            separator = raw.index("--")
            envelope = raw[:separator]
            worker_command = tuple(raw[separator + 1 :])
            if not worker_command:
                raise ConfigError(
                    "scheduler supervisor envelope is missing its worker command",
                    hint="bootstrap must append -- followed by the policy-derived command",
                )
        else:
            envelope = raw
            worker_command = ()
        args = parser.parse_args(envelope)
        identity = _identity(args.scheduler_session)
        loop = build_scheduler_loop(
            identity=identity,
            state_dir=args.state_dir,
            schedule_config=args.schedule_config,
            schedule_config_sha256=args.schedule_config_sha256,
        )
        if args.cadence_seconds is not None and not math.isclose(
            args.cadence_seconds, loop.cadence_seconds, rel_tol=0.0, abs_tol=1e-9
        ):
            raise ConfigError(
                "scheduler envelope cadence does not match the hash-pinned policy",
                hint="rebuild the supervisor command from the same policy artifact",
            )
        if worker_command and worker_command != loop.command:
            raise ConfigError(
                "scheduler envelope worker command does not match the hash-pinned policy",
                hint="the supervisor must pass the policy-derived command unchanged",
            )
        loop.run()
        return EXIT_OK
    except ConfigError as exc:
        print(f"CONFIG: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except ValueError as exc:
        print(f"USAGE: {exc}", file=sys.stderr)
        return EXIT_USAGE


def _identity(value: str) -> SchedulerIdentity:
    session_id, separator, nonce = value.partition(":")
    if not separator or not session_id or not nonce:
        raise ValueError(
            "--scheduler-session must be '<session>:<nonce>' with both halves non-empty"
        )
    if ":" in nonce:
        raise ValueError("--scheduler-session nonce must not contain ':'")
    return SchedulerIdentity(session_id=session_id, nonce=nonce)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
