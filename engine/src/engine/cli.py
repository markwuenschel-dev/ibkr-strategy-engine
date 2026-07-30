"""``engine`` -- the operator surface.

    engine doctor                                  config + alerting, no connection
    engine status                                  M1: account summary and positions
    engine quote SPY                               M2: a labelled price
    engine preview --symbol SPY --qty 1            M3: margin preview, places nothing
    engine trade   --symbol SPY --qty 1 --arm      M4: one order

    engine probe-options-data                      capability probe; transmits nothing
    engine options-scan --symbol SPY               IV Rank + chain + real what-if; transmits nothing

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
from .errors import EXIT_ERROR, EXIT_OK, EXIT_USAGE, EngineError, RefusedError
from .journal import OrderJournal, utc_now
from .safety import BUY, SIDES, OrderIntent, SafetyGate


def out(*parts: Any) -> None:
    print(" ".join(str(part) for part in parts))


def note(message: str) -> None:
    print(message, file=sys.stderr)


def _open_orders_or_none(broker: Any) -> Any:
    """What the broker is working, or ``None`` when it could not be asked.

    ``None`` and ``()`` are different answers to the reconciler: the first is
    "nobody asked", the second is "asked, nothing working". Collapsing them is
    how a report came to state, of a live working order, that the broker was
    not working it. A broker that raises or has no such method is the first
    case, never the second.
    """
    from .options.adapters import read_open_orders  # noqa: PLC0415 - optional path

    try:
        return read_open_orders(getattr(broker, "ib", broker))
    except Exception:  # noqa: BLE001 - an unanswered question is not an answer of no
        return None


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

    probe = subs.add_parser(
        "probe-options-data",
        help="non-transmitting capability probe: do option greeks arrive?",
    )
    probe.add_argument("--symbol", default="SPY")
    probe.add_argument(
        "--market-data-type",
        type=int,
        default=3,
        choices=[1, 2, 3, 4],
        help="1 live, 2 frozen, 3 delayed (default), 4 delayed-frozen. "
        "Requested once and never changed, so run each type in its own process.",
    )
    probe.add_argument("--dte", type=int, default=45, help="target days to expiry")
    probe.add_argument("--strikes", type=int, default=4, help="contracts to subscribe")
    probe.add_argument(
        "--settle", type=float, default=12.0, help="seconds to wait for ticks"
    )

    scan = subs.add_parser(
        "options-scan",
        help="shadow scan: IV Rank, expiry, chain and a real broker what-if. Transmits nothing.",
    )
    scan.add_argument("--symbol", default="SPY")
    scan.add_argument("--dte", type=int, default=45, help="target days to expiry")
    scan.add_argument("--min-iv-rank", type=float, default=50.0)
    scan.add_argument("--width-steps", type=int, default=5, help="strikes between the wings")

    run = subs.add_parser(
        "options-run",
        help=(
            "one strategy pass: reconcile, manage open positions, consider one "
            "entry. Requires --arm to transmit anything."
        ),
    )
    run.add_argument("--symbol", default="SPY")
    run.add_argument(
        "--bias",
        default="BULLISH",
        choices=["BULLISH", "BEARISH", "NEUTRAL"],
        help="BULLISH sells puts, BEARISH sells calls",
    )
    run.add_argument("--dte", type=int, default=45, help="target days to expiry")
    run.add_argument("--min-iv-rank", type=float, default=50.0)
    run.add_argument(
        "--market-data-type",
        type=int,
        default=1,
        choices=[1, 2, 3, 4],
        help=(
            "1 live (default), 2 frozen, 3 delayed, 4 delayed-frozen. Anything "
            "other than live is for exercising the pipeline during development: "
            "the entitlement gate refuses non-live data, so a run on delayed data "
            "can build and grade a candidate but can never transmit one."
        ),
    )
    run.add_argument(
        "--arm",
        action="store_true",
        help=(
            "actually transmit. Without this the pass runs every gate and shows "
            "exactly what it would have sent, and sends nothing."
        ),
    )

    cancel = subs.add_parser(
        "options-cancel",
        help="pull one working order by its strategy id (requires --arm)",
        description=(
            "Retract a working order through the engine's single cancel "
            "chokepoint. Matched on orderRef, which carries the strategy id -- "
            "never on orderId, which is reused across sessions. Authorized by "
            "the kill switch and --arm only: refusing to cancel because the "
            "book is concentrated would be backwards, since cancelling is what "
            "reduces exposure."
        ),
    )
    cancel.add_argument("--strategy-id", required=True, help="the order's orderRef")
    cancel.add_argument("--reason", default="", help="recorded with the cancellation")
    cancel.add_argument(
        "--arm",
        action="store_true",
        help="actually cancel. Without this the target is printed and nothing is sent.",
    )

    positions = subs.add_parser(
        "options-positions", help="list open option structures and reconcile them"
    )
    positions.add_argument("--no-connect", action="store_true", help="read the store only")

    # A command of its own, not a flag on the strategy pass. The bounded proof
    # and the production strategy answer different questions -- "does the broker
    # lifecycle work" versus "should this trade be made" -- and sharing a command
    # meant one forgotten flag silently ran the other one armed. A separate verb
    # cannot be reached by omission.
    proof = subs.add_parser(
        "options-execution-proof",
        help=(
            "bounded execution proof: one 1-wide SPY vertical, quantity 1, "
            "defined loss <= $100. Tests the broker lifecycle, not the strategy."
        ),
        description=(
            "A tightly bounded operational test of the IBKR execution lifecycle. "
            "It is NOT the trading strategy and does not claim a strategy signal, "
            "which is why it does not consult the IV Rank filter. Every safety "
            "gate still runs: live uniform provenance, quote and greek freshness, "
            "defined-risk construction, maximum loss, broker what-if margin, "
            "stress loss, portfolio reconciliation, the authorization token, the "
            "kill switch, the paper-port restriction and durable persistence. "
            "The bounds are module constants -- no environment variable and no "
            "flag can widen them."
        ),
    )
    proof.add_argument("--symbol", default="SPY")
    proof.add_argument("--dte", type=int, default=45, help="target days to expiry")
    proof.add_argument(
        "--arm",
        action="store_true",
        help=(
            "transmit exactly one bounded order. Without it the whole timeline "
            "runs and stops at the authorization step, printing the exact order "
            "it would have sent."
        ),
    )
    # Pinned, and deliberately not exposed as flags. Market data type in
    # particular: offering --market-data-type here would let an operator run the
    # proof on delayed data, which is exactly the kind of widening this command
    # exists to make impossible. The entitlement gate would refuse it anyway --
    # but a bound you can ask for and be refused is weaker than one you cannot
    # express. IV Rank is carried only because the shared runner signature takes
    # it; the proof does not enforce it.
    proof.add_argument(
        "--price-at",
        default="midpoint",
        choices=["midpoint", "natural"],
        help=(
            "where on the book to ask. midpoint is fair value and the strategy "
            "default; natural is what the book pays now (short bid minus long "
            "ask) and is for an execution experiment that wants a fill rather "
            "than a good price. Every risk bound is re-run at whichever is used."
        ),
    )
    proof.set_defaults(
        execution_proof=True,
        bias="BULLISH",
        market_data_type=1,
        min_iv_rank=0.0,
    )

    verify = subs.add_parser(
        "options-verify-execution",
        help=(
            "print the full authorization, submission, broker-status, fill, "
            "persistence and reconciliation timeline. Paper ports only."
        ),
    )
    verify.add_argument("--symbol", default="SPY")
    verify.add_argument("--bias", default="BULLISH", choices=["BULLISH", "BEARISH", "NEUTRAL"])
    verify.add_argument("--dte", type=int, default=45)
    verify.add_argument("--min-iv-rank", type=float, default=50.0)
    verify.add_argument(
        "--market-data-type", type=int, default=1, choices=[1, 2, 3, 4]
    )
    verify.add_argument(
        "--execution-proof",
        action="store_true",
        help=(
            "run the bounded execution proof instead of an ordinary armed pass: "
            "SPY only, one 1-wide vertical, quantity 1, defined loss <= $100, "
            "broker margin <= $150, stress loss <= $100, one opening order. The "
            "IV Rank filter is off -- this is a lifecycle test, not a strategy "
            "signal -- and every safety gate still runs. Requires --arm to send."
        ),
    )
    verify.add_argument(
        "--arm",
        action="store_true",
        help=(
            "transmit one order. Without it the timeline stops at the "
            "authorization step and reports exactly which gate would have refused."
        ),
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


def cmd_probe_options_data(args: argparse.Namespace, broker_factory: Any = Broker) -> int:
    """Subscribe, observe, report, cancel. Places nothing, ever.

    Exits 0 whichever capability state is found -- an answer of "delayed greeks
    are unavailable" is a successful probe, not a failure. Only a probe that
    could not reach a conclusion exits non-zero.
    """
    from .options.probe import ProbeOutcome, run_market_data_probe

    config = config_from(args)
    journal = OrderJournal(config.journal_path)
    journal.preflight()

    gate = SafetyGate(config, journal)
    # The kill switch covers reads too. If someone has halted the engine, the
    # answer is that nothing talks to the broker -- not "nothing places orders".
    gate.assert_not_halted()

    with broker_factory(config, journal) as broker:
        report = run_market_data_probe(
            broker,
            symbol=args.symbol.strip().upper(),
            market_data_type=args.market_data_type,
            target_dte=args.dte,
            strike_count=args.strikes,
            settle_seconds=args.settle,
            account=config.account_id,
        )

    out(report.describe())
    journal.record(**report.to_record())
    out("")
    note("probe complete; no order was transmitted and none can be from this path")

    if report.outcome is ProbeOutcome.PROBE_ERROR:
        return EXIT_ERROR
    return EXIT_OK


def cmd_options_scan(args: argparse.Namespace, broker_factory: Any = Broker) -> int:
    """Run every options step that works without a market-data subscription.

    Places nothing. Exits 0 whether or not a tradeable candidate was found --
    "IV Rank is 26, no trade" is a successful scan.
    """
    from decimal import Decimal

    from .options.adapters import (
        IBKRLiveMarketDataAdapter,
        IBKRPortfolioStateAdapter,
    )
    from .options.policy import RiskPolicy
    from .options.scan import run_scan

    config = config_from(args)
    journal = OrderJournal(config.journal_path)
    journal.preflight()

    gate = SafetyGate(config, journal)
    gate.assert_not_halted()

    # Built from IBKR_OPTIONS_* with validated defaults. Constructing it here
    # rather than inside run_scan means a bad threshold fails before the socket
    # opens, like every other config error in this engine.
    policy = RiskPolicy.from_env()

    with broker_factory(config, journal) as broker:
        # The real adapters are passed in. Under the current entitlement they
        # will make the gate refuse -- with OPTIONS_REALTIME_DATA_REQUIRED rather
        # than "no data was supplied", which is the difference between the gate
        # being wired in and merely existing.
        report = run_scan(
            broker,
            symbol=args.symbol.strip().upper(),
            target_dte=args.dte,
            minimum_iv_rank=Decimal(str(args.min_iv_rank)),
            width_steps=args.width_steps,
            account=config.account_id,
            policy=policy,
            market_data=IBKRLiveMarketDataAdapter(broker.ib),
            portfolio=IBKRPortfolioStateAdapter(broker),
        )

    out(report.describe())
    journal.record(**report.to_record())
    out("")
    note("scan complete; no order was transmitted and none can be from this path")
    return EXIT_OK


def cmd_options_run(args: argparse.Namespace, broker_factory: Any = Broker) -> int:
    """One strategy pass. The only options command that can transmit.

    Gate ordering mirrors :func:`cmd_trade`, and for the same reason: the local
    gates run before a socket is opened, and ``--arm`` is checked last inside
    :func:`engine.options.transmit.authorize_open`, so an unarmed pass still
    shows every other refusal instead of stopping at "not armed".
    """
    from decimal import Decimal

    from .options.adapters import IBKRLiveMarketDataAdapter, IBKRPortfolioStateAdapter
    from .options.policy import RiskPolicy
    from .options.positions import PositionStore
    from .options.runner import EntryPricing, run_once
    from .options.selection import Bias

    config = config_from(args)
    journal = OrderJournal(config.journal_path)
    journal.preflight()

    gate = SafetyGate(config, journal)
    # Before any socket. A halted engine must not talk to the broker at all.
    gate.assert_not_halted()

    policy = RiskPolicy.from_env()
    store = PositionStore(config.state_dir / "positions.jsonl")

    if not args.arm:
        note("DRY RUN -- every gate will run and nothing will be transmitted.")

    with broker_factory(config, journal) as broker:
        report = run_once(
            broker,
            gate=gate,
            journal=journal,
            store=store,
            policy=policy,
            armed=bool(args.arm),
            symbol=args.symbol.strip().upper(),
            bias=Bias(args.bias),
            market_data=IBKRLiveMarketDataAdapter(
                broker.ib, requested_type=args.market_data_type
            ),
            portfolio=IBKRPortfolioStateAdapter(broker),
            target_dte=args.dte,
            minimum_iv_rank=Decimal(str(args.min_iv_rank)),
            account=config.account_id,
        )

    out(report.describe())
    journal.record(**report.to_record())
    out("")
    if not args.arm:
        note("dry run complete; nothing was transmitted. Pass --arm to trade.")
    return EXIT_OK


def cmd_options_cancel(args: argparse.Namespace, broker_factory: Any = Broker) -> int:
    """Pull one working order, by the strategy id it carries as its orderRef.

    The operator surface for the cancel chokepoint. It exists because a working
    order that neither fills nor rejects had, until now, no way out of this
    engine at all -- it had to be cancelled by hand in TWS, which means the one
    action that makes every other bound enforceable after the fact was the one
    action the engine could not take.

    Matching is on ``orderRef``, which ``build_combo`` sets to the strategy id.
    Deliberately not ``orderId``: it is reused across sessions, so a stranger's
    order can carry one of ours.
    """
    from uuid import UUID

    from .options.adapters import read_open_orders
    from .options.positions import PositionStore
    from .options.sink import LifecycleRecorder
    from .options.transmit import authorize_cancel, cancel_combo

    config = config_from(args)
    try:
        strategy_id = UUID(str(args.strategy_id).strip())
    except (ValueError, AttributeError, TypeError):
        raise RefusedError(
            f"not a strategy id: {args.strategy_id!r}",
            hint="pass the uuid the order carries as its orderRef; "
            "`engine options-positions` prints it",
        ) from None

    journal = OrderJournal(config.journal_path)
    journal.preflight()
    gate = SafetyGate(config, journal)
    # Before the socket, as everywhere else. A halted engine does not connect.
    gate.assert_not_halted()

    store = PositionStore(config.state_dir / "positions.jsonl")
    recorder = LifecycleRecorder(store)

    with broker_factory(config, journal) as broker:
        working = read_open_orders(broker.ib)
        if working is None:
            raise RefusedError(
                "the broker could not be asked what it is working",
                hint="without that answer a cancel would be aimed at a guess",
            )
        matches = [
            trade
            for trade in working
            if str(getattr(getattr(trade, "order", None), "orderRef", "")).strip()
            == str(strategy_id)
        ]
        out(f"working orders   {len(working)} at the broker, {len(matches)} ours")
        if not matches:
            out("")
            note(f"no working order carries orderRef {strategy_id}")
            note("it may have filled, been cancelled, or expired -- reconcile first")
            return EXIT_ERROR

        for trade in matches:
            order = getattr(trade, "order", None)
            status = getattr(trade, "orderStatus", None)
            out(
                f"  orderId={getattr(order, 'orderId', None)} "
                f"permId={getattr(order, 'permId', None)} "
                f"lmt={getattr(order, 'lmtPrice', None)} "
                f"status={getattr(status, 'status', None)} "
                f"filled={getattr(status, 'filled', None)} "
                f"remaining={getattr(status, 'remaining', None)}"
            )

        if not args.arm:
            out("")
            note("not armed: nothing was cancelled. Pass --arm to pull it.")
            return EXIT_OK

        authorization = authorize_cancel(
            strategy_id,
            gate=gate,
            armed=True,
            now=utc_now(),
            reason=args.reason,
        )
        out("")
        out(f"AUTHORIZED CANCEL  {authorization.describe()}")

        for trade in matches:
            result = cancel_combo(
                broker.ib, trade, authorization=authorization, sink=recorder
            )
            out("")
            out(result.describe())
            # A cancel is not a flat book. It can lose a race with a fill, and
            # cancelling the remainder of a partial leaves contracts behind.
            if result.has_position:
                out("")
                note(
                    "THIS DID NOT LEAVE YOU FLAT -- the order carries a position. "
                    "Reconcile before doing anything else."
                )
            journal.record(
                event="order_cancelled",
                strategy_id=str(strategy_id),
                state=result.state.value,
                has_position=bool(result.has_position),
                reason=args.reason,
            )
    return EXIT_OK


def cmd_options_positions(args: argparse.Namespace, broker_factory: Any = Broker) -> int:
    """List what the engine believes it holds, and check it against the broker."""
    from .options.positions import PositionStore

    config = config_from(args)
    store = PositionStore(config.state_dir / "positions.jsonl")

    open_positions = store.open_positions()
    out(f"open positions   {len(open_positions)}")
    for position in open_positions:
        out(f"  {position.describe()}")

    if args.no_connect:
        out("")
        note("--no-connect: the broker was not consulted, so nothing was reconciled")
        return EXIT_OK

    journal = OrderJournal(config.journal_path)
    with broker_factory(config, journal) as broker:
        # Positions AND working orders. A working order is not a position, so
        # asking only the first reported an order the broker was demonstrably
        # working as one it was not.
        report = store.reconcile_against_broker(
            broker.positions(),
            checked_at=utc_now(),
            broker_orders=_open_orders_or_none(broker),
        )
    out("")
    out(report.describe())
    journal.record(**report.to_record())
    return EXIT_OK if report.agrees else EXIT_ERROR


def cmd_options_verify_execution(
    args: argparse.Namespace, broker_factory: Any = Broker
) -> int:
    """Print the whole execution timeline, gate by gate. Paper ports only.

    The point of this command is that it is *legible*: every gate reports pass
    or refuse with its machine-readable code, in the order the engine actually
    evaluates them, so an operator can see precisely where a run stops rather
    than inferring it from an absence.

    It refuses **before** any socket transmission whenever entitlement,
    provenance, risk, governor, kill-switch, allowlist, arm or strategy-id
    checks fail -- and because it reuses :func:`engine.options.runner.run_once`
    rather than reimplementing the pipeline, it cannot drift from what the armed
    command does. A verification path with its own copy of the gates would prove
    something about the copy.

    With ``--execution-proof`` it becomes a different and much narrower thing:
    a bounded operational test of the broker's execution lifecycle, run under
    :class:`engine.options.proof.ExecutionProofProfile` rather than under the
    strategy's own policy. See :func:`cmd_options_execution_proof`.
    """
    if getattr(args, "execution_proof", False):
        return cmd_options_execution_proof(args, broker_factory)

    from decimal import Decimal

    from .options.adapters import IBKRLiveMarketDataAdapter, IBKRPortfolioStateAdapter
    from .options.policy import RiskPolicy
    from .options.positions import PositionStore
    from .options.runner import EntryPricing, run_once
    from .options.selection import Bias

    config = config_from(args)
    # The paper-port interlock is enforced in EngineConfig, so reaching this line
    # already proves the endpoint is a paper one. Stated here because a command
    # named "verify execution" is exactly where someone would look for it.
    journal = OrderJournal(config.journal_path)
    journal.preflight()

    gate = SafetyGate(config, journal)
    gate.assert_not_halted()

    policy = RiskPolicy.from_env()
    store = PositionStore(config.state_dir / "positions.jsonl")

    out("EXECUTION VERIFICATION")
    out(f"  venue          {config.venue} ({config.host}:{config.port})")
    out(f"  account        {config.account_id}")
    out(f"  policy         {policy.version}")
    out(f"  armed          {'YES -- one order may be transmitted' if args.arm else 'NO'}")
    out("")

    with broker_factory(config, journal) as broker:
        report = run_once(
            broker,
            gate=gate,
            journal=journal,
            store=store,
            policy=policy,
            armed=bool(args.arm),
            symbol=args.symbol.strip().upper(),
            bias=Bias(args.bias),
            market_data=IBKRLiveMarketDataAdapter(
                broker.ib, requested_type=args.market_data_type
            ),
            portfolio=IBKRPortfolioStateAdapter(broker),
            target_dte=args.dte,
            minimum_iv_rank=Decimal(str(args.min_iv_rank)),
            account=config.account_id,
        )

    out(report.describe())
    out("")

    out("TIMELINE")
    events = list(store.events())
    if not events:
        out("  no position events were written -- nothing reached the transmit step")
    for event in events[-24:]:
        out(
            f"  {str(event.get('at', ''))[:19]}  {str(event.get('event', '')):<22}"
            f"  order={event.get('order_id')} perm={event.get('perm_id')}"
        )
    out("")

    integrity = store.integrity_errors()
    out("STORE INTEGRITY")
    out("  clean" if not integrity else f"  {len(integrity)} unreadable event(s)")
    for problem in integrity:
        out(f"    {problem}")
    out("")

    journal.record(**report.to_record())

    if not args.arm:
        note(
            "not armed: the timeline above stops at the authorization step and "
            "names the gate that would have refused. Pass --arm to transmit one order."
        )
    elif not report.transmissions:
        note(
            "armed, and nothing was transmitted -- a gate refused first. The "
            "refusal codes above say which."
        )
    return EXIT_OK


def cmd_options_execution_proof(
    args: argparse.Namespace, broker_factory: Any = Broker
) -> int:
    """The bounded execution proof. Not a strategy pass, and it says so.

    Two things make this different from ``options-verify-execution --arm``, and
    both are the point of the command existing:

    **Its bounds come from a schema, not the environment.** The policy it runs
    under is :meth:`ExecutionProofProfile.derive_policy`, which folds the proof's
    ceilings into whatever ``IBKR_OPTIONS_*`` says using ``min()`` -- so an
    environment variable can make this run stricter and can never make it looser.
    The profile's fingerprint is printed before anything happens, so what the run
    was bounded by is a fact on the operator's screen rather than an inference
    from a config file.

    **It does not claim to be the strategy signal.** The IV Rank filter is off,
    deliberately, and the refusal code that would have fired is recorded anyway.
    Every gate that answers "is this survivable if it fills" -- provenance,
    freshness, defined-risk construction, maximum loss, broker what-if margin,
    stress loss, portfolio reconciliation, the authorization token, the kill
    switch, the paper port and durable persistence -- runs untouched, because
    this reaches them through the same :func:`engine.options.runner.run_once`
    the armed strategy command uses.
    """
    from decimal import Decimal

    from .options.adapters import IBKRLiveMarketDataAdapter, IBKRPortfolioStateAdapter
    from .options.positions import PositionStore
    from .options.proof import (
        PROOF_CONFIGURATION_VERSION,
        ExecutionProofProfile,
        OpeningOrderBudget,
        ProofEntryPreflight,
        RecordingLifecycleSink,
        new_proof_session_id,
    )
    from .options.runner import EntryPricing, run_once
    from .options.selection import Bias
    from .options.sink import LifecycleRecorder

    config = config_from(args)

    # Constructed before the journal, before the gate and long before a socket.
    # A non-paper port or a symbol other than SPY is refused here, as a
    # ConfigError, with nothing yet opened -- which is the earliest any of the
    # proof's bounds can possibly be checked.
    profile = ExecutionProofProfile(
        port=config.port,
        account=config.account_id,
        symbol=args.symbol.strip().upper(),
    )

    journal = OrderJournal(config.journal_path)
    journal.preflight()

    gate = SafetyGate(config, journal)
    gate.assert_not_halted()

    policy = profile.derive_policy()
    store = PositionStore(config.state_dir / "positions.jsonl")
    session_id = new_proof_session_id()

    out("EXECUTION PROOF")
    out(f"  session        {session_id}")
    out(f"  venue          {config.venue} ({config.host}:{config.port})")
    out(f"  armed          {'YES -- one order may be sent' if args.arm else 'NO'}")
    out(profile.describe())
    out("")
    out("EFFECTIVE POLICY (proof ceilings folded into the environment)")
    out(policy.describe())
    out("")

    budget = OpeningOrderBudget(limit=profile.maximum_opening_orders)
    preflight = ProofEntryPreflight(profile=profile, budget=budget, emit=out)
    capture = RecordingLifecycleSink(inner=LifecycleRecorder(store))

    with broker_factory(config, journal) as broker:
        report = run_once(
            broker,
            gate=gate,
            journal=journal,
            store=store,
            policy=policy,
            armed=bool(args.arm),
            symbol=profile.symbol,
            bias=Bias(args.bias),
            market_data=IBKRLiveMarketDataAdapter(
                broker.ib, requested_type=args.market_data_type
            ),
            portfolio=IBKRPortfolioStateAdapter(broker),
            target_dte=args.dte,
            minimum_iv_rank=Decimal(str(args.min_iv_rank)),
            account=config.account_id,
            configuration_version=PROOF_CONFIGURATION_VERSION,
            enforce_iv_rank=False,
            entry_pricing=EntryPricing(
                str(getattr(args, "price_at", "midpoint")).upper()
            ),
            entry_preflight=preflight,
            sink=capture,
        )

        # Restart reconciliation, in the same connection but through a *fresh*
        # store and a fresh recorder. This is the question a proof exists to
        # answer and a normal run cannot: would a process that started just now,
        # knowing only what is on disk, agree with the broker about what is
        # held? Reusing the store above would prove only that an object agrees
        # with itself.
        replayed = PositionStore(config.state_dir / "positions.jsonl")
        try:
            restart = replayed.reconcile_against_broker(
                broker.positions(),
                checked_at=utc_now(),
                broker_orders=_open_orders_or_none(broker),
            )
        except Exception as exc:  # noqa: BLE001 - reporting must not crash the proof
            restart = None
            report.errors.append(f"restart reconciliation failed: {exc}")

    out("")
    out(report.describe())
    out("")

    out("ORDER IDENTITY")
    if report.candidate is not None:
        out(f"  intent id      {report.candidate.strategy_id}")
        out(f"  orderRef       {report.candidate.strategy_id}")
        out(f"  configuration  {report.candidate.configuration_version}")
    else:
        out("  no candidate was built, so no order identity exists")
    for result in report.transmissions:
        out(f"  orderId        {result.order_id}")
        out(f"  permId         {result.perm_id}")
        out(f"  state          {result.state.value}")
        out(f"  filled         {result.filled}")
        out(f"  average price  {result.average_price}")
        if result.snapshot is not None:
            out(f"  remaining      {result.snapshot.remaining}")
            out(f"  commission     {result.snapshot.commission}")
            if result.snapshot.message:
                out(f"  broker text    {result.snapshot.message}")
    out(f"  profile hash   {profile.fingerprint()}")
    out(f"  audit tag      {profile.audit_tag}")
    out("")

    out(f"BROKER LIFECYCLE  {len(capture.observations)} observation(s), as heard")
    if not capture.observations:
        out("  none -- nothing reached the order-placement step")
    for line in capture.timeline():
        out(line)
    out("")

    out("DURABLE STORE EVENTS")
    events = list(store.events())
    if not events:
        out("  no position events were written")
    for event in events[-24:]:
        out(
            f"  {str(event.get('at', ''))[:19]}  {str(event.get('event', '')):<22}"
            f"  order={event.get('order_id')} perm={event.get('perm_id')}"
        )
    integrity = store.integrity_errors()
    out(f"  integrity      {'clean' if not integrity else f'{len(integrity)} bad'}")
    for problem in integrity:
        out(f"    {problem}")
    out("")

    out("RESTART RECONCILIATION (a fresh store, replayed from disk)")
    out(restart.describe() if restart is not None else "  could not be run")
    out("")

    out("SESSION BUDGET")
    out(f"  opening orders {budget.spent} of {budget.limit} used")
    if preflight.envelope is not None:
        out(f"  price envelope {preflight.envelope.describe()}")
        out(f"  credit at send {preflight.credit_at_send}")
    if preflight.refusal:
        out(f"  refused by     {preflight.refusal}")

    journal.record(
        "options_execution_proof",
        audit_tag=profile.audit_tag,
        session_id=str(session_id),
        profile_fingerprint=profile.fingerprint(),
        profile=profile.to_record(),
        armed=bool(args.arm),
        opening_orders_used=budget.spent,
        price_envelope=(
            preflight.envelope.to_record() if preflight.envelope is not None else None
        ),
        credit_at_send=(
            str(preflight.credit_at_send)
            if preflight.credit_at_send is not None
            else None
        ),
        preflight_refusal=preflight.refusal,
        observations=capture.to_record(),
        restart_reconciliation=(restart.to_record() if restart is not None else None),
        run=report.to_record(),
    )

    out("")
    if not args.arm:
        note(
            "not armed: every bound and every gate above was evaluated and "
            "nothing was sent. Pass --execution-proof --arm to send one order."
        )
    elif not report.transmissions:
        note(
            "armed, and nothing was sent -- a bound or a gate refused first. The "
            "blockers and refusal codes above say which."
        )
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
    "probe-options-data": cmd_probe_options_data,
    "options-scan": cmd_options_scan,
    "options-run": cmd_options_run,
    "options-positions": cmd_options_positions,
    "options-cancel": cmd_options_cancel,
    "options-verify-execution": cmd_options_verify_execution,
    "options-execution-proof": cmd_options_execution_proof,
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
