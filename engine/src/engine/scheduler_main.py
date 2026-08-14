"""Command-line entrypoint for the production scheduler process."""

from __future__ import annotations

import argparse
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
    try:
        args = parser.parse_args(argv)
        identity = _identity(args.scheduler_session)
        loop = build_scheduler_loop(
            identity=identity,
            state_dir=args.state_dir,
            schedule_config=args.schedule_config,
            schedule_config_sha256=args.schedule_config_sha256,
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
