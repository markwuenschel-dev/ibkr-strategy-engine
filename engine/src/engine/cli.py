"""``engine`` -- the operator surface.

    engine doctor                                  config + alerting, no connection
    engine status                                  M1: account summary and positions
    engine quote SPY                               M2: a labelled price
    engine preview --symbol SPY --qty 1            M3: margin preview, places nothing
    engine trade   --symbol SPY --qty 1 --arm      M4: one order

    engine halt "reason"                           engage the kill switch
    engine resume                                  release it
    engine journal -n 20                           read the durable record

Two conventions run through every command:

* **Nothing is transmitted without ``--arm``.** ``trade`` without it runs every
  gate, prints exactly what it *would* send, and exits non-zero. That makes the
  dry run the default and the live path an explicit act.
* **Exit codes carry the reason** (see :mod:`engine.errors`), so a supervising
  script can tell a refusal (4) from a broker outage (5) without parsing text.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from . import __version__
from ._collabkit import last_error as last_collabkit_error
from ._collabkit import load as load_collabkit
from ._collabkit import load_dotenv
from .alerts import Alerter
from .broker import Broker
from .config import PAPER_PORTS, EngineConfig
from .errors import EXIT_OK, EXIT_USAGE, EngineError, RefusedError
from .journal import OrderJournal
from .safety import BUY, SIDES, OrderIntent, SafetyGate


def out(*parts: Any) -> None:
    print(" ".join(str(part) for part in parts))


def note(message: str) -> None:
    print(message, file=sys.stderr)


# ==========================================================================
# argument parsing
# ==========================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="engine",
        description="IBKR paper-trading engine. Paper endpoints only, by construction.",
        epilog=(
            "Connects only to "
            + ", ".join(f"{port} ({name})" for port, name in sorted(PAPER_PORTS.items()))
            + ". Live ports are refused at config load."
        ),
    )
    parser.add_argument("--version", action="version", version=f"ibkr-engine {__version__}")
    parser.add_argument("--account", help="override IBKR_ACCOUNT_ID")
    parser.add_argument("--port", type=int, help="override IBKR_PORT")
    parser.add_argument("--host", help="override IBKR_HOST")
    parser.add_argument("--client-id", type=int, help="override IBKR_CLIENT_ID")
    parser.add_argument("--state-dir", help="override IBKR_STATE_DIR")
    parser.add_argument("--no-alerts", action="store_true", help="do not write phone alerts")

    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("doctor", help="check config and alerting without connecting")
    subs.add_parser("status", help="connect read-only; show account and positions")

    quote = subs.add_parser("quote", help="print a labelled quote")
    quote.add_argument("symbol")

    for name, help_text in (
        ("preview", "run whatIfOrder; place nothing"),
        ("trade", "place an order (requires --arm)"),
    ):
        sub = subs.add_parser(name, help=help_text)
        sub.add_argument("--symbol", required=True)
        sub.add_argument("--qty", type=int, required=True)
        sub.add_argument("--side", default=BUY, choices=list(SIDES))
        sub.add_argument("--limit", type=float, help="limit price (default: market order)")
        if name == "trade":
            sub.add_argument(
                "--arm",
                action="store_true",
                help="actually transmit. Without this, the order is only described.",
            )

    halt = subs.add_parser("halt", help="engage the kill switch")
    halt.add_argument("reason", nargs="?", default="halted from the CLI")
    subs.add_parser("resume", help="release the kill switch")

    journal = subs.add_parser("journal", help="tail the durable order journal")
    journal.add_argument("-n", "--count", type=int, default=20)

    return parser


def config_from(args: argparse.Namespace) -> EngineConfig:
    overrides: dict[str, Any] = {}
    if args.account:
        overrides["account_id"] = args.account.strip()
    if args.port is not None:
        overrides["port"] = args.port
    if args.host:
        overrides["host"] = args.host.strip()
    if args.client_id is not None:
        overrides["client_id"] = args.client_id
    if args.state_dir:
        from pathlib import Path

        overrides["state_dir"] = Path(args.state_dir).expanduser()
    return EngineConfig.from_env(**overrides)


# ==========================================================================
# commands
# ==========================================================================


def cmd_doctor(args: argparse.Namespace) -> int:
    """Everything that can be checked without touching the broker."""
    # Reported first because it is upstream of everything below: a missing
    # account id is usually a .env that was not found, not one that was wrong.
    env_file = load_dotenv()
    if env_file is None:
        out(f"env file   not loaded -- {last_collabkit_error() or 'collab-kit unavailable'}")
    else:
        out(f"env file   {env_file.describe()}")
        for problem in env_file.problems:
            note(f"  .env: {problem}")

    config = config_from(args)
    out("\nconfig")
    out(config.describe())

    journal = OrderJournal(config.journal_path)
    journal.preflight()
    out(f"\njournal    writable at {journal.path}")

    alerter = Alerter(config.project, enabled=not args.no_alerts)
    ok, reason = alerter.preflight()
    out(f"alerts     {'ok' if ok else 'UNAVAILABLE'}{f' -- {reason}' if reason else ''}")

    halted = config.halt_file.exists()
    out(f"kill switch{' ENGAGED at ' + str(config.halt_file) if halted else ' clear'}")
    out(f"orders today {journal.orders_today()} / {config.max_orders_per_session}")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    config = config_from(args)
    journal = OrderJournal(config.journal_path)
    journal.preflight()

    with Broker(config, journal) as broker:
        out(f"connected to {config.venue} as {config.account_id}\n")
        out("account")
        for tag, value, currency in broker.account_summary():
            if tag in _SUMMARY_TAGS:
                out(f"  {tag:<24} {value:>18} {currency}")
        positions = broker.positions()
        out("\npositions")
        if not positions:
            out("  (none)")
        for symbol, quantity, cost in positions:
            out(f"  {symbol:<8} {quantity:>10,.0f} @ {cost:,.4f}")
    return EXIT_OK


_SUMMARY_TAGS = {
    "AccountType",
    "NetLiquidation",
    "TotalCashValue",
    "BuyingPower",
    "AvailableFunds",
    "ExcessLiquidity",
}


def cmd_quote(args: argparse.Namespace) -> int:
    config = config_from(args)
    journal = OrderJournal(config.journal_path)
    with Broker(config, journal) as broker:
        quote = broker.quote(args.symbol)
        out(quote.describe())
        if quote.price is None:
            note(
                "no price came back. Paper accounts often lack market-data "
                "subscriptions; outside trading hours even delayed data can be empty."
            )
            return 1
    return EXIT_OK


def cmd_preview(args: argparse.Namespace) -> int:
    config = config_from(args)
    journal = OrderJournal(config.journal_path)
    gate = SafetyGate(config, journal)
    intent = gate.check_preview(_intent_from(args))

    with Broker(config, journal) as broker:
        preview = broker.preview(intent)
        out("preview (nothing was transmitted)")
        out(preview.describe())
        journal.record(
            "preview",
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            margin_impact=preview.margin_impact,
            commission=preview.commission,
        )
        try:
            gate.gate_margin(margin_impact=preview.margin_impact)
            out("\n  margin gate  PASS")
        except RefusedError as exc:
            out(f"\n  margin gate  REFUSE -- {exc.message}")
            return exc.exit_code
    return EXIT_OK


def cmd_trade(args: argparse.Namespace, broker_factory: Any = Broker) -> int:
    """M4. The only command that can transmit an order.

    Gate ordering is load-bearing and is why this does not just call
    ``SafetyGate.check``:

    1. local gates (kill switch, symbol, quantity) -- **before** any socket;
    2. connect, price, position;
    3. the gates that need a price;
    4. ``whatIfOrder`` and the margin gate;
    5. ``--arm``, checked **last**, so an unarmed run still shows you the full
       preview of what it would have sent.

    A halted engine must never reach step 2. ``broker_factory`` is injectable so
    a test can prove it does not.
    """
    config = config_from(args)
    journal = OrderJournal(config.journal_path)
    journal.preflight()
    gate = SafetyGate(config, journal)
    alerter = Alerter(config.project, enabled=not args.no_alerts)

    raw_intent = _intent_from(args)

    # Step 1. Nothing here needs a broker, so nothing here may cost a connection.
    try:
        raw_intent = gate.check_preflight(raw_intent)
    except RefusedError as exc:
        journal.record(
            "refused",
            symbol=raw_intent.symbol,
            side=raw_intent.side,
            quantity=raw_intent.quantity,
            reason=exc.message,
            stage="preflight",
        )
        raise

    # Only one engine may hold the order path at a time. Reuses collab-kit's
    # SingletonLock, the same primitive the Telegram bridge uses to stop two
    # copies double-delivering.
    singleton = load_collabkit("locking", "SingletonLock")
    lock_context: Any = _NullContext()
    if singleton is not None:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        lock_context = singleton(config.lock_file, name="ibkr engine")
    else:
        note("warning: collab-kit not importable; running without the single-instance lock")

    with lock_context:
        # Step 2. Only now is a connection opened.
        with broker_factory(config, journal) as broker:
            quote = broker.quote(raw_intent.symbol)
            held = broker.position_qty(raw_intent.symbol)
            reference = raw_intent.limit_price if raw_intent.limit_price else quote.price
            out(f"reference price {reference} [{quote.source}], currently holding {held}")

            def refuse(exc: RefusedError, stage: str) -> None:
                journal.record(
                    "refused",
                    symbol=raw_intent.symbol,
                    side=raw_intent.side,
                    quantity=raw_intent.quantity,
                    reason=exc.message,
                    stage=stage,
                )
                if args.arm:
                    # Only alert on a refusal that was actually trying to trade;
                    # a dry run reporting "not armed" is not news.
                    alerter.refused(raw_intent.describe(), exc.message)

            # Step 3. The gates that needed a price and a position.
            try:
                intent = gate.check_tradeable(
                    raw_intent, reference_price=reference, current_qty=held
                )
            except RefusedError as exc:
                refuse(exc, "tradeable")
                raise

            # Step 4. Ask the broker what the order would cost, then gate on it.
            preview = broker.preview(intent)
            out("\npre-trade preview")
            out(preview.describe())
            journal.record(
                "preview",
                symbol=intent.symbol,
                side=intent.side,
                quantity=intent.quantity,
                margin_impact=preview.margin_impact,
                commission=preview.commission,
            )
            try:
                gate.gate_margin(margin_impact=preview.margin_impact)
            except RefusedError as exc:
                refuse(exc, "margin")
                raise

            # Step 5. Arming is checked last, so a dry run still shows the
            # preview above -- which is the whole value of a dry run.
            try:
                gate.gate_armed(armed=args.arm)
            except RefusedError as exc:
                out(f"\nwould send: {intent.describe()}")
                refuse(exc, "armed")
                raise

            out(f"\ntransmitting {intent.describe()} ...")
            journal.record(
                "order_placed",
                symbol=intent.symbol,
                side=intent.side,
                quantity=intent.quantity,
                limit_price=intent.limit_price,
                account=config.account_id,
            )
            result = broker.place(intent)
            record = journal.record("order_result", **result)
            out(
                f"  status {result['status']}  filled {result['filled']:g} "
                f"@ {result['avg_fill_price']}"
            )
            if result["timed_out"]:
                note("order did not reach a terminal state before the timeout; check TWS")
                alerter.problem(
                    f"order {result['order_id']} did not settle",
                    f"{intent.describe()} status={result['status']}",
                )
            else:
                alerter.fill(record)
            if alerter.last_error:
                note(f"  alert NOT queued: {alerter.last_error}")
            else:
                out("  alert queued to the collab-kit outbox")
    return EXIT_OK


def cmd_halt(args: argparse.Namespace) -> int:
    config = config_from(args)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.halt_file.write_text(args.reason + "\n", encoding="utf-8")
    OrderJournal(config.journal_path).record("halted", reason=args.reason)
    out(f"kill switch ENGAGED: {config.halt_file}")
    out("no order will be placed until this file is removed (engine resume).")
    return EXIT_OK


def cmd_resume(args: argparse.Namespace) -> int:
    config = config_from(args)
    if config.halt_file.exists():
        config.halt_file.unlink()
        OrderJournal(config.journal_path).record("resumed")
        out(f"kill switch released: {config.halt_file}")
    else:
        out("kill switch was not engaged")
    return EXIT_OK


def cmd_journal(args: argparse.Namespace) -> int:
    config = config_from(args)
    journal = OrderJournal(config.journal_path)
    records = journal.tail(max(1, args.count))
    if not records:
        out(f"journal is empty ({journal.path})")
        return EXIT_OK
    for record in records:
        extra = " ".join(
            f"{key}={value}"
            for key, value in sorted(record.items())
            if key not in ("v", "ts", "event")
        )
        out(f"{record.get('ts', '?'):<21} {record.get('event', '?'):<14} {extra}")
    return EXIT_OK


class _NullContext:
    def __enter__(self) -> "_NullContext":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _intent_from(args: argparse.Namespace) -> OrderIntent:
    return OrderIntent(
        symbol=args.symbol,
        quantity=args.qty,
        side=args.side,
        limit_price=args.limit,
    )


COMMANDS = {
    "doctor": cmd_doctor,
    "status": cmd_status,
    "quote": cmd_quote,
    "preview": cmd_preview,
    "trade": cmd_trade,
    "halt": cmd_halt,
    "resume": cmd_resume,
    "journal": cmd_journal,
}


def main(argv: list[str] | None = None) -> int:
    # Before argument parsing, and therefore before any EngineConfig.from_env:
    # this repo keeps IBKR_* and the bot token in a git-ignored .env. Failure to
    # find one is not an error -- exporting the variables directly still works,
    # and `engine doctor` is where the difference is reported.
    load_dotenv()

    parser = build_parser()
    args = parser.parse_args(argv)
    handler = COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse enforces the choices
        parser.error(f"unknown command {args.command!r}")
        return EXIT_USAGE

    try:
        return handler(args)
    except EngineError as exc:
        note(f"error: {exc.message}")
        if exc.hint:
            note(f"  hint: {exc.hint}")
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover
        note("interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
