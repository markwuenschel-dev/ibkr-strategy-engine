"""Application adapter for the persistent ``options-cycle`` worker.

This is the one place where the operational worker is composed with the
strategy modules.  The scheduler never imports this module.  A cycle owns one
connected :class:`~engine.broker.Broker`, one in-memory IBKR pacing budget, and
one durable pacing ledger; the phase closures below all receive those same
objects.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .autocycle import (
    AutoCycleConfig,
    CycleError,
    CycleMode,
    CyclePhases,
    FixedRateSchedule,
    JobKind,
    OptionsCycleWorker,
    PhaseContext,
    ReceiptStore,
)
from .autotrader_policy import (
    ARMED,
    DRY_RUN,
    FULL,
    REVIEW_ONLY,
    SHADOW,
    AutotraderPolicy,
    WindowSpec,
    load_autotrader_policy,
)
from .broker import Broker
from .config import EngineConfig
from .errors import ConfigError, EXIT_ERROR, EXIT_OK
from .journal import OrderJournal
from .options.adapters import (
    IBKRContractDataAdapter,
    IBKRLiveMarketDataAdapter,
    IBKRPortfolioStateAdapter,
    IBKRVolatilityHistoryAdapter,
)
from .options.catalog import UniverseCatalog
from .options.execution_outbox import ExecutionOutbox
from .options.ivstore import IVStore
from .options.freshness import SessionMetadataStore
from .options.logical import LogicalEntryManager, LogicalEntryStore
from .options.order_outbox import TransmissionBudget
from .options.pacing import PacedRequestBudget, Priority
from .options.pacing_ledger import PacingLedger
from .options.policy import RiskPolicy
from .options.regime import VolatilityRegimePolicy
from .options.runner import EntryMode, EntryPricing, run_once
from .options.universe import UniverseScanConfig, penalize_on_broker_error, run_universe_pass


def _json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _entry_bounds(policy: AutotraderPolicy) -> tuple[dt.time, dt.time]:
    """Require the initial explicit wall-clock entry window.

    A relative session window is valid schema for future policies, but this
    worker does not silently reinterpret it as an all-day opening window.
    """

    window = policy.windows.get("entry")
    if not isinstance(window, WindowSpec) or window.kind != "WALL_CLOCK":
        raise ConfigError(
            "autotrader entry window must be an explicit WALL_CLOCK interval",
            hint="the initial reviewed policy is 10:00-15:00 ET; do not widen it implicitly",
        )
    if not window.start or not window.end:
        raise ConfigError("autotrader entry WALL_CLOCK window needs start and end")
    try:
        start = dt.time.fromisoformat(window.start)
        end = dt.time.fromisoformat(window.end)
    except ValueError as exc:
        raise ConfigError("autotrader entry window contains an invalid wall-clock time") from exc
    return start, end


def _identity(state_dir: Path, args: Any) -> tuple[str, str]:
    lock = _json(state_dir / "paperday" / "session.lock") or {}
    record = _json(state_dir / "paperday" / "scheduler.pid") or {}
    raw = getattr(args, "scheduler_session", None)
    if raw:
        session_id, separator, nonce = str(raw).partition(":")
        if separator and session_id and nonce:
            return session_id, nonce
    session_id = str(lock.get("session_id") or record.get("session_id") or "").strip()
    nonce = str(record.get("nonce") or lock.get("fencing_token") or "").strip()
    if not session_id or not nonce:
        raise ConfigError(
            "options-cycle cannot establish a paper-day session identity",
            hint="start it under PaperDayController so session lock and scheduler nonce exist",
        )
    return session_id, nonce


def _window_allowed(policy: AutotraderPolicy, job: JobKind, instant: dt.datetime) -> bool:
    session = policy.calendar.session_on(instant.astimezone(policy.calendar.timezone).date())
    if session is None:
        return False
    moment = instant.astimezone(dt.timezone.utc)
    if job is JobKind.MANAGEMENT:
        return session.contains(moment)
    if job is JobKind.DISCOVERY:
        # PRE_OPEN is intentionally bounded by the declared session edge; the
        # catalog/policy, not a hidden one-hour code default, defines when the
        # breadth work may start.
        window = policy.windows["breadth_discovery"]
        if window.kind == "PRE_OPEN":
            return moment < session.opens_at
        return session.contains(moment)
    if job is JobKind.PROBE:
        window = policy.windows["candidate_probe"]
        if window.kind == "SESSION_RELATIVE":
            return session.opens_at <= moment < session.closes_at - dt.timedelta(
                minutes=window.minutes_before_close or 0
            )
        return session.contains(moment)
    window = policy.windows["entry"]
    if window.kind == "WALL_CLOCK" and window.start and window.end:
        local = instant.astimezone(policy.calendar.timezone).time().replace(tzinfo=None)
        start = dt.time.fromisoformat(window.start)
        end = dt.time.fromisoformat(window.end)
        return session.contains(moment) and start <= local < end
    return False


@dataclass
class _CycleRuntime:
    config: EngineConfig
    policy: AutotraderPolicy
    journal: OrderJournal
    gate: Any
    strategy_policy: RiskPolicy
    regime_policy: VolatilityRegimePolicy
    catalog: UniverseCatalog
    budget: PacedRequestBudget
    pacing_ledger: PacingLedger
    manager: LogicalEntryManager | None
    verifier: Any
    approval_context: Any
    entry_preflight: Callable[..., str | None]
    session_lease: Callable[[], str | None]
    execution_outbox: ExecutionOutbox
    transmission_budget: TransmissionBudget

    def _adapters(self, broker: Any, *, priority: Priority) -> tuple[Any, Any, Any]:
        contract_data = IBKRContractDataAdapter(broker.ib)
        history = IBKRVolatilityHistoryAdapter(
            broker.ib,
            contract_data,
            budget=self.budget,
            budget_priority=Priority.DISCOVERY,
        )
        market = IBKRLiveMarketDataAdapter(
            broker.ib,
            requested_type=1,
            budget=self.budget,
            budget_priority=priority,
        )
        portfolio = IBKRPortfolioStateAdapter(broker)
        return history, market, portfolio

    @staticmethod
    def _report(report: Any) -> dict[str, Any]:
        try:
            value = report.to_record()
        except AttributeError:
            return {"outcome": type(report).__name__, "detail": str(report)}
        return value if isinstance(value, dict) else {"outcome": str(value)}

    def management(self, context: PhaseContext) -> Mapping[str, Any]:
        _history, market, portfolio = self._adapters(
            context.broker, priority=Priority.EXITS_MANAGEMENT
        )
        report = run_once(
            context.broker,
            gate=self.gate,
            journal=self.journal,
            store=__import__("engine.options.positions", fromlist=["PositionStore"]).PositionStore(
                self.config.state_dir / "positions.jsonl"
            ),
            policy=self.strategy_policy,
            armed=False,
            entry_mode=EntryMode.MANAGE_ONLY,
            market_data=market,
            portfolio=portfolio,
            account=self.config.account_id,
            verifier=self.verifier,
            approval_context=self.approval_context,
            manager=self.manager,
            scanbook_root=self.config.state_dir,
            max_pending_entries=self.policy.entry.max_pending_entries,
            session_id=context.cycle.session_id,
            lease_nonce=context.cycle.lease_nonce,
            tick_id=context.cycle.tick_id,
        )
        return self._report(report)

    def discovery(self, context: PhaseContext) -> Mapping[str, Any]:
        # Reserve a coarse discovery floor in the durable ledger before the
        # scanner can spend the in-memory socket budget.  The scanner itself
        # records exact per-request pacing; this reservation is the crash-safe
        # cross-process guard used by future shards.
        reservation = self.pacing_ledger.reserve(
            __import__("engine.options.pacing", fromlist=["RequestKind"]).RequestKind.HISTORICAL,
            cost=min(self.policy.discovery.refresh_limit, 1),
            priority=Priority.DISCOVERY,
            owner_id=context.cycle.session_id,
            request_key=f"{context.cycle.tick_id}:discovery",
        )
        if reservation is None:
            return {"outcome": "DEFERRED_PACING", "failure_code": "FAIL-UNSHARED-PACING"}
        try:
            scan_config = UniverseScanConfig.from_env(
                refresh_limit=self.policy.discovery.refresh_limit,
                phase2_limit=self.policy.discovery.phase2_limit,
            )
            extras = self.catalog.entries
            def on_error(req_id: int, code: int, message: str, *_: Any) -> None:
                penalize_on_broker_error(self.budget, code)

            ib = context.broker.ib
            ib.errorEvent += on_error
            try:
                history, market, _portfolio = self._adapters(
                    context.broker, priority=Priority.DISCOVERY
                )
                book = run_universe_pass(
                    universe=extras,
                    session_date=context.cycle.session_date,
                    iv_store=IVStore(self.config.state_dir / "universe" / "iv"),
                    metadata_store=SessionMetadataStore(
                        self.config.state_dir / "universe" / "metadata"
                    ),
                    budget=self.budget,
                    policy=self.strategy_policy,
                    regime_policy=self.regime_policy,
                    config=scan_config,
                    volatility_history=history,
                    contract_data=IBKRContractDataAdapter(ib),
                    market_data=market,
                )
            finally:
                with contextlib.suppress(Exception):
                    ib.errorEvent -= on_error
            path = book.write(self.config.state_dir)
            self.pacing_ledger.commit(reservation.reservation_id)
            return {
                "outcome": "SCAN_COMPLETED",
                "scanbook": str(path),
                "symbols": len(book.rows),
                "candidates": [row.symbol for row in book.candidates()],
                "catalog_hash": self.catalog.catalog_hash,
            }
        except Exception:
            with contextlib.suppress(Exception):
                self.pacing_ledger.mark_unknown(reservation.reservation_id)
            raise

    def probe(self, context: PhaseContext) -> Mapping[str, Any]:
        # The legacy scanner combines chain probing with discovery.  I2 may
        # expose a split probe function; until then this explicit receipt keeps
        # the 10-minute job visible without pretending a second full-universe
        # scan is bounded.
        return {
            "outcome": "PROBE_DEFERRED_TO_DISCOVERY",
            "phase2_limit": self.policy.discovery.phase2_limit,
            "detail": "legacy scanner has no independent candidate-probe seam yet",
        }

    def entry(self, context: PhaseContext) -> Mapping[str, Any]:
        if self.policy.mode in {DRY_RUN, SHADOW}:
            return {
                "outcome": "ENTRY_SHADOW_ONLY",
                "claims": 0,
                "review_requests": 0,
                "transmissions": 0,
            }
        _history, market, portfolio = self._adapters(
            context.broker, priority=Priority.AUTHORIZATION
        )
        from .options.positions import PositionStore

        report = run_once(
            context.broker,
            gate=self.gate,
            journal=self.journal,
            store=PositionStore(self.config.state_dir / "positions.jsonl"),
            policy=self.strategy_policy,
            armed=context.transmission_enabled,
            entry_mode=EntryMode.FULL,
            market_data=market,
            portfolio=portfolio,
            account=self.config.account_id,
            verifier=self.verifier,
            approval_context=self.approval_context,
            entry_preflight=self.entry_preflight,
            session_lease=self.session_lease,
            manager=self.manager,
            scanbook_root=self.config.state_dir,
            max_pending_entries=self.policy.entry.max_pending_entries,
            execution_outbox=self.execution_outbox,
            transmission_budget=self.transmission_budget,
            session_id=context.cycle.session_id,
            lease_nonce=context.cycle.lease_nonce,
            tick_id=context.cycle.tick_id,
        )
        return self._report(report)


def run_options_cycle(
    args: Any,
    *,
    config: EngineConfig,
    broker_factory: Callable[..., Any] = Broker,
    clock: Callable[[], dt.datetime] | None = None,
) -> int:
    """Run one persistent application worker under a hash-pinned policy."""

    policy_path = Path(args.schedule_config).expanduser()
    policy = load_autotrader_policy(policy_path, args.schedule_config_sha256)
    if config.state_dir.resolve() != policy.state_dir.resolve():
        raise ConfigError(
            "options-cycle state dir does not match the hash-pinned policy",
            hint="one explicit absolute state directory prevents split-brain state",
        )
    if policy.mode == ARMED and not getattr(args, "arm", False):
        raise ConfigError(
            "ARMED options-cycle invocation is missing the pinned --arm token",
            hint="the worker command in the policy must include --arm",
        )
    catalog = UniverseCatalog.from_artifact(
        policy.catalog.path,
        expected_sha256=policy.catalog.sha256,
        expected_version=policy.catalog.version,
    )
    session_id, lease_nonce = _identity(config.state_dir, args)
    now = (clock or (lambda: dt.datetime.now(dt.timezone.utc)))()
    session = policy.calendar.session_on(now.astimezone(policy.calendar.timezone).date())
    anchor = session.opens_at - dt.timedelta(minutes=30) if session else now
    entry_start, entry_end = _entry_bounds(policy)
    cycle_config = AutoCycleConfig(
        mandate=policy.mandate,
        mode=CycleMode(policy.mode),
        management_seconds=int(policy.cadences.management_seconds),
        discovery_seconds=int(policy.cadences.discovery_seconds),
        probe_seconds=int(policy.cadences.probe_seconds),
        entry_seconds=int(policy.cadences.entry_seconds),
        missed_tick_policy=policy.missed_tick_policy,
        entry_start=entry_start,
        entry_end=entry_end,
        coverage_sla_seconds=int(policy.discovery.coverage_sla_seconds),
        max_pending_entries=policy.entry.max_pending_entries,
        max_new_entries_per_pass=policy.entry.max_new_openings_per_pass,
        phase2_limit=policy.discovery.phase2_limit,
        policy_hash=policy.policy_hash or args.schedule_config_sha256,
        catalog_hash=policy.catalog.sha256,
        state_dir=config.state_dir,
    )
    journal = OrderJournal(config.journal_path)
    journal.preflight()
    from .safety import SafetyGate

    gate = SafetyGate(config, journal)
    gate.assert_not_halted()
    strategy_policy = RiskPolicy.from_env()
    regime_policy = VolatilityRegimePolicy.from_env()

    with broker_factory(config, journal) as broker:
        budget = PacedRequestBudget(
            sleeper=broker.ib.sleep,
            management_reserve_fraction=policy.pacing_reserve.management_fraction,
        )
        setattr(broker, "pacing", budget)
        pacing_ledger = PacingLedger(
            config.state_dir / "pacing.sqlite3",
            management_reserve_fraction=policy.pacing_reserve.management_fraction,
            clock=clock,
        )
        from .cli import _paper_day_preflight, _verifier_for

        verifier, approval_context = _verifier_for(config, strategy_policy)
        manager = None
        if verifier is not None:
            manager = LogicalEntryManager(
                store=LogicalEntryStore(config.state_dir / "logical_entries.jsonl"),
                gate=verifier,
            )
        expected_fingerprint = (
            approval_context.configuration_fingerprint if approval_context is not None else None
        )
        entry_preflight = _paper_day_preflight(
            config, expected_configuration_fingerprint=expected_fingerprint
        )

        def session_lease() -> str | None:
            lock = _json(config.state_dir / "paperday" / "session.lock") or {}
            if lock.get("session_id") != session_id:
                return "FAIL-STALE-PAPERDAY-AUTHORITY: session lock identity changed"
            gate_record = _json(config.state_dir / "paperday" / "gate.json") or {}
            if gate_record.get("policy_sha256") not in {
                policy.policy_hash,
                args.schedule_config_sha256.lower(),
            }:
                return "FAIL-STALE-PAPERDAY-AUTHORITY: policy hash changed"
            if gate_record.get("catalog_sha256") != policy.catalog.sha256:
                return "FAIL-CATALOG-HASH: paper-day catalog authority changed"
            return entry_preflight(armed=True)

        runtime = _CycleRuntime(
            config=config,
            policy=policy,
            journal=journal,
            gate=gate,
            strategy_policy=strategy_policy,
            regime_policy=regime_policy,
            catalog=catalog,
            budget=budget,
            pacing_ledger=pacing_ledger,
            manager=manager,
            verifier=verifier,
            approval_context=approval_context,
            entry_preflight=entry_preflight,
            session_lease=session_lease,
            execution_outbox=ExecutionOutbox(config.state_dir / "execution-outbox"),
            transmission_budget=TransmissionBudget(
                config.state_dir / "transmission-budget.json",
                limit=policy.entry.transmission_limit_per_session,
                journal=journal,
                now=now,
            ),
        )
        receipts = ReceiptStore(config.state_dir / "autocycle")
        schedule = FixedRateSchedule(
            config.state_dir / "autocycle",
            anchor=anchor,
            cadences={
                JobKind.MANAGEMENT: int(policy.cadences.management_seconds),
                JobKind.DISCOVERY: int(policy.cadences.discovery_seconds),
                JobKind.PROBE: int(policy.cadences.probe_seconds),
                JobKind.ENTRY: int(policy.cadences.entry_seconds),
            },
        )
        worker = OptionsCycleWorker(
            config=cycle_config,
            session_id=session_id,
            lease_nonce=lease_nonce,
            broker_factory=lambda: broker,
            phases=CyclePhases(
                management=runtime.management,
                discovery=runtime.discovery,
                probe=runtime.probe,
                entry=runtime.entry,
            ),
            receipts=receipts,
            schedule=schedule,
            clock=clock,
            sleeper=getattr(broker.ib, "sleep", None) or __import__("time").sleep,
            stop_requested=lambda: (
                (config.state_dir / "paperday" / "quiesce").exists()
                or (_json(config.state_dir / "paperday" / "session.lock") or {}).get("session_id")
                != session_id
            ),
            job_allowed=lambda job, moment: _window_allowed(policy, job, moment),
        )
        worker.run_forever(
            arm=bool(getattr(args, "arm", False) and policy.mode == ARMED),
            broker=broker,
            max_cycles=getattr(args, "max_cycles", None),
        )
    return EXIT_OK


__all__ = ["run_options_cycle"]
