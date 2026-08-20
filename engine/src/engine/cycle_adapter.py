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
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from .autocycle import (
    AutoCycleConfig,
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
    IBKRPriceHistoryAdapter,
    IBKRVolatilityHistoryAdapter,
    read_open_orders,
)
from .options.catalog import UniverseCatalog
from .options.freshness import SessionMetadataStore
from .options.logical import (
    DEFAULT_REFUSAL_POLICY,
    LogicalEntryManager,
    LogicalEntryStore,
)
from .options.order_outbox import ExecutionOutbox, TransmissionBudget
from .options.pacing import (
    PacedRequestBudget,
    Priority,
    RequestKind,
    SharedPacingBudget,
)
from .options.pacing_ledger import PacingLedger
from .options.observation_cache import SQLiteObservationCache
from .options.policy import RiskPolicy
from .options.regime import VolatilityRegimePolicy
from .options.runner import EntryMode, run_once
from .options.scan_receipts import ScanReceiptStore
from .options.scanbook_store import ScanBookSnapshotStore
from .options.universe import (
    UniverseScanConfig,
    penalize_on_broker_error,
    run_catalog_universe_pass,
)
from .paperday import effective_configuration_fingerprint
from .scheduler import SchedulerIdentity, SchedulerPaths, announce_ready


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


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
    session_id = str(lock.get("session_id") or record.get("session_id") or "").strip()
    nonce = str(record.get("nonce") or lock.get("fencing_token") or "").strip()
    if not session_id or not nonce:
        raise ConfigError(
            "options-cycle cannot establish a paper-day session identity",
            hint="start it under PaperDayController so session lock and scheduler nonce exist",
        )
    raw = getattr(args, "scheduler_session", None)
    if raw:
        raw_session, separator, raw_nonce = str(raw).partition(":")
        if (
            not separator
            or not raw_session.strip()
            or not raw_nonce.strip()
            or ":" in raw_nonce
        ):
            raise ConfigError(
                "FAIL-STALE-PAPERDAY-AUTHORITY: malformed --scheduler-session",
                hint="the scheduler identity must be '<session>:<lease nonce>'",
            )
        supplied = (raw_session.strip(), raw_nonce.strip())
        live = (
            str(lock.get("session_id") or record.get("session_id") or "").strip(),
            str(record.get("nonce") or lock.get("fencing_token") or "").strip(),
        )
        if supplied != live:
            raise ConfigError(
                "FAIL-STALE-PAPERDAY-AUTHORITY: scheduler identity does not match live paper-day lease",
                hint="do not override the session identity; restart under the current controller lease",
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
    budget: Any
    pacing_ledger: PacingLedger
    manager: LogicalEntryManager | None
    verifier: Any
    approval_context: Any
    entry_preflight: Callable[..., str | None]
    session_lease: Callable[[], str | None]
    execution_outbox: ExecutionOutbox
    transmission_budget: TransmissionBudget
    observation_cache: SQLiteObservationCache
    scanbook_store: ScanBookSnapshotStore
    scan_receipts: ScanReceiptStore

    def _adapters(self, broker: Any, *, priority: Priority) -> tuple[Any, Any, Any, Any]:
        active_budget = self.budget
        contract_data = IBKRContractDataAdapter(
            broker.ib,
            budget=active_budget,
            budget_priority=priority,
        )
        history = IBKRVolatilityHistoryAdapter(
            broker.ib,
            contract_data,
            budget=active_budget,
            budget_priority=Priority.DISCOVERY,
        )
        price_history = IBKRPriceHistoryAdapter(
            broker.ib,
            contract_data,
            budget=active_budget,
            budget_priority=Priority.DISCOVERY,
        )
        market = IBKRLiveMarketDataAdapter(
            broker.ib,
            requested_type=1,
            budget=active_budget,
            budget_priority=priority,
        )
        portfolio = IBKRPortfolioStateAdapter(
            broker,
            budget=active_budget,
            budget_priority=priority,
        )
        return history, price_history, market, portfolio

    @staticmethod
    def _report(report: Any) -> dict[str, Any]:
        try:
            value = report.to_record()
        except AttributeError:
            return {"outcome": type(report).__name__, "detail": str(report)}
        return value if isinstance(value, dict) else {"outcome": str(value)}

    def management(self, context: PhaseContext) -> Mapping[str, Any]:
        _history, _price_history, market, portfolio = self._adapters(
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
            request_budget=self.budget,
        )
        return self._report(report)

    def reconcile(self, context: PhaseContext) -> Mapping[str, Any]:
        """Resolve startup ambiguity before the worker can open risk.

        This path is read-only with respect to the broker.  It proves the
        position journal agrees with a complete broker positions/open-orders
        observation, quarantines any unfinished read-only scan, and refuses to
        clear the worker's tick fence while an execution outbox intent remains
        unresolved.
        """

        blocking = self.execution_outbox.blocking_records()
        if blocking:
            return {
                "outcome": "RECOVERY_REQUIRED",
                "failure_code": "FAIL-BROKER-AMBIGUOUS",
                "unresolved_execution_sagas": [
                    item.get("attempt_id") for item in blocking
                ],
            }
        try:
            from .options.positions import PositionStore, ReconciliationOutcome

            positions = PositionStore(self.config.state_dir / "positions.jsonl")
            self.budget.acquire(
                RequestKind.GENERAL,
                priority=Priority.EXITS_MANAGEMENT,
            )
            broker_positions = context.broker.positions()
            broker_orders = read_open_orders(
                context.broker.ib,
                budget=self.budget,
                budget_priority=Priority.WORKING_ORDERS,
            )
            if broker_orders is None:
                return {
                    "outcome": "RECOVERY_REQUIRED",
                    "failure_code": "FAIL-BROKER-AMBIGUOUS",
                    "detail": "broker did not provide a complete open-order observation",
                }
            report = positions.reconcile_against_broker(
                broker_positions,
                checked_at=context.cycle.started_at,
                broker_orders=broker_orders,
            )
        except Exception as exc:  # noqa: BLE001 - recovery must fail closed
            return {
                "outcome": "RECOVERY_REQUIRED",
                "failure_code": "FAIL-BROKER-AMBIGUOUS",
                "detail": f"reconciliation raised {type(exc).__name__}: {exc}",
            }
        outcome = ReconciliationOutcome.for_report(report)
        if outcome is not ReconciliationOutcome.RECONCILED:
            return {
                "outcome": "RECOVERY_REQUIRED",
                "failure_code": "FAIL-RECOVERY-BLOCKED",
                "reconciliation": report.to_record(),
            }

        unmatched_scans = self.scan_receipts.unmatched()
        foreign_scans = [
            state
            for state in unmatched_scans
            if state.session_id != context.cycle.session_id
        ]
        if foreign_scans:
            return {
                "outcome": "RECOVERY_REQUIRED",
                "failure_code": "FAIL-RECOVERY-BLOCKED",
                "detail": (
                    "unmatched scan receipts belong to a prior session authority; "
                    "operator reconciliation is required before this session can clear recovery"
                ),
                "foreign_scans": [state.scan_id for state in foreign_scans],
            }

        scans_reconciled: list[str] = []
        for state in unmatched_scans:
            receipts = self.scan_receipts.read(state.scan_id)
            first = receipts[0] if receipts else None
            self.scan_receipts.abort(
                session_id=state.session_id,
                scan_id=state.scan_id,
                recorded_at=context.cycle.started_at,
                reason="startup recovery: read-only scan had no terminal receipt",
                reconciled=True,
                tick_id=first.tick_id if first else None,
                attempt_id=first.attempt_id if first else None,
            )
            scans_reconciled.append(state.scan_id)
        return {
            "outcome": "RECOVERY_CLEARED",
            "recovery_cleared": True,
            "reason": "broker journal/order/position reconciliation agreed",
            "reconciliation": report.to_record(),
            "scans_reconciled": scans_reconciled,
        }

    def discovery(self, context: PhaseContext) -> Mapping[str, Any]:
        return self._scan(context, refresh_enabled=True)

    def probe(self, context: PhaseContext) -> Mapping[str, Any]:
        # A probe reuses the indexed breadth cache and performs only the
        # bounded phase-two shortlist.  No stale cache is promoted: if the
        # discovery ring has not produced fresh slow observations, this
        # publication remains diagnostic-only and entry admission refuses it.
        return self._scan(context, refresh_enabled=False)

    def _scan_manifest(self, scan_config: UniverseScanConfig) -> dict[str, str]:
        behavior_hash = _sha256_json(
            {
                "manifest_version": "scan-behavior/1",
                "catalog_version": self.catalog.version,
                "scan": scan_config.to_record(),
                "risk": self.strategy_policy.to_record(),
                "regime": self.regime_policy.to_record(),
            }
        )
        return {
            "catalog_hash": self.catalog.catalog_hash,
            "catalog_version": self.catalog.version,
            "policy_hash": self.policy.policy_hash or "",
            "calendar_hash": _sha256_json(
                self.policy.fingerprint_record()["calendar"]
            ),
            "config_hash": _sha256_json(
                {
                    "scan": scan_config.to_record(),
                    "coverage_sla_seconds": self.policy.discovery.coverage_sla_seconds,
                }
            ),
            "behavior_hash": behavior_hash,
        }

    def _scan(
        self, context: PhaseContext, *, refresh_enabled: bool
    ) -> Mapping[str, Any]:
        scan_config = UniverseScanConfig.from_env(
            refresh_limit=self.policy.discovery.refresh_limit,
            phase2_limit=self.policy.discovery.phase2_limit,
            phase2_request_cost=6,
        )

        def on_error(req_id: int, code: int, message: str, *_: Any) -> None:
            kind = penalize_on_broker_error(self.budget, code)
            if kind is not None and not hasattr(self.budget, "ledger"):
                # The in-memory bucket controls this connection immediately;
                # the durable ledger carries the broker's penalty across a
                # restart and is the authority used by the discovery ring.
                self.pacing_ledger.penalize(kind, now=context.cycle.started_at)

        ib = context.broker.ib
        ib.errorEvent += on_error
        try:
            history, price_history, market, _portfolio = self._adapters(
                context.broker, priority=Priority.DISCOVERY
            )
            manifest = self._scan_manifest(scan_config)
            result = run_catalog_universe_pass(
                catalog=self.catalog,
                observation_cache=self.observation_cache,
                refresh_queue=self.observation_cache.refresh_queue,
                pacing_ledger=self.pacing_ledger,
                snapshot_store=self.scanbook_store,
                receipt_store=self.scan_receipts,
                session_id=context.cycle.session_id,
                session_date=context.cycle.session_date,
                policy_hash=manifest["policy_hash"],
                calendar_hash=manifest["calendar_hash"],
                config_hash=manifest["config_hash"],
                policy=self.strategy_policy,
                regime_policy=self.regime_policy,
                config=scan_config,
                metadata_store=SessionMetadataStore(
                    self.config.state_dir / "universe" / "metadata"
                ),
                volatility_history=history,
                price_history=price_history,
                contract_data=IBKRContractDataAdapter(
                    ib,
                    budget=self.budget,
                    budget_priority=Priority.DISCOVERY,
                ),
                market_data=market,
                now=context.cycle.started_at,
                scan_id=f"{context.cycle.session_id}:{context.cycle.tick_id}:{context.cycle.attempt_id}",
                tick_id=context.cycle.tick_id,
                attempt_id=context.cycle.attempt_id,
                refresh_interval=dt.timedelta(
                    seconds=self.policy.cadences.discovery_seconds
                ),
                refresh_enabled=refresh_enabled,
            )
        finally:
            with contextlib.suppress(Exception):
                ib.errorEvent -= on_error
        return {
            "outcome": "SCAN_COMPLETED" if result.complete else "SCAN_DIAGNOSTIC_ONLY",
            "scan_id": result.scan_id,
            "symbols": result.snapshot.expected_symbols,
            "evaluated": result.snapshot.evaluated_symbols,
            "deferred": result.snapshot.deferred_symbols,
            "unavailable": result.snapshot.unavailable_symbols,
            "candidates": [row.symbol for row in result.book.candidates()],
            "catalog_hash": result.catalog_hash,
            "coverage": result.snapshot.coverage.value,
            "refresh_enabled": refresh_enabled,
        }

    def entry(self, context: PhaseContext) -> Mapping[str, Any]:
        if self.policy.mode in {DRY_RUN, SHADOW}:
            return {
                "outcome": "ENTRY_SHADOW_ONLY",
                "claims": 0,
                "review_requests": 0,
                "transmissions": 0,
                "new_openings": 0,
            }
        # FULL is never allowed to fall through to the legacy candidate
        # corridor when the reviewer/verifier composition is incomplete.  A
        # persistent worker must fail before construction, broker quotes, or
        # claims; otherwise a missing reviewer looks like a harmless setup
        # omission while still doing entry work.
        if self.manager is None or self.verifier is None or self.approval_context is None:
            return {
                "outcome": "ENTRY_BLOCKED",
                "failure_code": "FAIL-UNAUTHORIZED-ENTRY",
                "detail": "FULL entry requires a live verifier, approval context, and logical-entry manager",
                "claims": 0,
                "review_requests": 0,
                "transmissions": 0,
                "new_openings": 0,
            }
        _history, _price_history, market, portfolio = self._adapters(
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
            packet_ttl_seconds=self.policy.entry.packet_ttl_seconds,
            scanbook_max_age_seconds=self.policy.discovery.coverage_sla_seconds,
            scanbook_snapshot_store=self.scanbook_store,
            scanbook_manifest=self._scan_manifest(
                UniverseScanConfig.from_env(
                    refresh_limit=self.policy.discovery.refresh_limit,
                    phase2_limit=self.policy.discovery.phase2_limit,
                    phase2_request_cost=6,
                )
            ),
            session_id=context.cycle.session_id,
            lease_nonce=context.cycle.lease_nonce,
            tick_id=context.cycle.tick_id,
            request_budget=self.budget,
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
        pacing_ledger = PacingLedger(
            config.state_dir / "pacing.sqlite3",
            management_reserve_fraction=policy.pacing_reserve.management_fraction,
            discovery_fraction=policy.pacing_reserve.discovery_fraction,
            minimum_management_requests=policy.pacing_reserve.minimum_management_requests,
            clock=clock,
        )
        budget = SharedPacingBudget(
            PacedRequestBudget(
                sleeper=broker.ib.sleep,
                management_reserve_fraction=policy.pacing_reserve.management_fraction,
                discovery_fraction=policy.pacing_reserve.discovery_fraction,
                minimum_management_requests=policy.pacing_reserve.minimum_management_requests,
            ),
            pacing_ledger,
            owner_id=f"{session_id}:{lease_nonce}",
            clock=pacing_ledger.clock,
        )
        setattr(broker, "pacing", budget)
        from .cli import _paper_day_preflight, _verifier_for

        verifier, approval_context = _verifier_for(config, strategy_policy)
        manager = None
        if verifier is not None:
            manager = LogicalEntryManager(
                store=LogicalEntryStore(config.state_dir / "logical_entries.jsonl"),
                gate=verifier,
                clock=clock,
                refusal_policy=replace(
                    DEFAULT_REFUSAL_POLICY,
                    claimed_max_age=dt.timedelta(
                        seconds=policy.entry.review_ttl_seconds
                    ),
                ),
            )
        if approval_context is not None:
            # The legacy ApprovalContext already binds engine/risk config. Add
            # the two operator artifacts that only this worker can know so a
            # handoff cannot survive a policy or catalog replacement.
            approval_context = replace(
                approval_context,
                configuration_fingerprint=effective_configuration_fingerprint(
                    approval_context.configuration_fingerprint,
                    policy_sha256=(
                        policy.policy_hash or args.schedule_config_sha256.lower()
                    ),
                    catalog_sha256=policy.catalog.sha256,
                    config_sha256=str(
                        (
                            _json(config.state_dir / "paperday" / "gate.json")
                            or {}
                        ).get("config_sha256")
                        or ""
                    ),
                ),
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
            scheduler_record = _json(config.state_dir / "paperday" / "scheduler.pid") or {}
            if (
                scheduler_record.get("session_id") != session_id
                or scheduler_record.get("nonce") != lease_nonce
            ):
                return (
                    "FAIL-STALE-PAPERDAY-AUTHORITY: scheduler lease nonce or "
                    "session identity changed"
                )
            gate_record = _json(config.state_dir / "paperday" / "gate.json") or {}
            if gate_record.get("policy_sha256") != args.schedule_config_sha256.lower():
                return "FAIL-STALE-PAPERDAY-AUTHORITY: policy hash changed"
            if gate_record.get("catalog_sha256") != policy.catalog.sha256:
                return "FAIL-CATALOG-HASH: paper-day catalog authority changed"
            authority = entry_preflight(armed=True)
            if authority:
                return authority
            return None

        def heartbeat() -> None:
            announce_ready(
                SchedulerPaths(root=config.state_dir / "paperday"),
                SchedulerIdentity(session_id=session_id, nonce=lease_nonce),
                now=(clock or (lambda: dt.datetime.now(dt.timezone.utc)))(),
            )

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
            observation_cache=SQLiteObservationCache(
                config.state_dir / "universe" / "observations.sqlite3"
            ),
            scanbook_store=ScanBookSnapshotStore(config.state_dir / "universe"),
            scan_receipts=ScanReceiptStore(config.state_dir / "universe"),
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
            session_id=session_id,
            lease_nonce=lease_nonce,
            policy_hash=policy.policy_hash or args.schedule_config_sha256.lower(),
            catalog_hash=policy.catalog.sha256,
        )
        def stop_requested() -> bool:
            if (config.state_dir / "paperday" / "quiesce").exists():
                return True
            lock = _json(config.state_dir / "paperday" / "session.lock") or {}
            if lock.get("session_id") != session_id:
                return True
            scheduler_record = _json(config.state_dir / "paperday" / "scheduler.pid")
            if scheduler_record is None:
                return True
            return bool(
                scheduler_record is not None
                and (
                    scheduler_record.get("session_id") != session_id
                    or scheduler_record.get("nonce") != lease_nonce
                )
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
                reconcile=runtime.reconcile,
            ),
            receipts=receipts,
            schedule=schedule,
            clock=clock,
            sleeper=getattr(broker.ib, "sleep", None) or __import__("time").sleep,
            stop_requested=stop_requested,
            job_allowed=lambda job, moment: _window_allowed(policy, job, moment),
            heartbeat=heartbeat,
            recovery_required=lambda: bool(runtime.scan_receipts.unmatched())
            or bool(runtime.execution_outbox.blocking_records())
            or schedule.unresolved(),
        )
        worker.run_forever(
            arm=bool(getattr(args, "arm", False) and policy.mode == ARMED),
            broker=broker,
            max_cycles=getattr(args, "max_cycles", None),
        )
    return EXIT_OK


__all__ = ["run_options_cycle"]
