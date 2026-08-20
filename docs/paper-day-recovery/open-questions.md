# Paper-day recovery — open questions and blockers

Status: **DRAFT — awaiting approval**. Date: 2026-08-18.

## BLOCKER-1 — Torn session.lock bricks the system permanently

**Severity: blocks ratification of D5 requirement 2.**

session.lock is written non-atomically (`paperday.py:1279-1288`: `os.open` with
`O_CREAT|O_EXCL|O_WRONLY` then `stream.write`; no fsync, no `os.replace`) while
the module's own `_atomic_write_json` (`:267-287`) fsyncs and replaces and is
used for gate.json (`:453`) and last-shutdown.json (`:1511`). A crash between
create and flush leaves a zero-byte lock.

`_read_json` (`:218-224`) collapses *missing* and *corrupt* to the same `None`:

- **start** — `existing = None` (`:1198`) skips the only unlink (`:1266`);
  `O_EXCL` raises EEXIST; reports "another start acquired the lock
  concurrently" (`:1282-1286`). False diagnostic, no path forward.
- **stop** — gate closes (`:1824`), then `_stop_owns` fails via
  `_current_stop_lock_identity` returning invalid-lock (`:1441`); returns at
  `:1864` and never reaches the `lock is None` branch (`:1866`) or the unlink
  (`:1973`).
- **status** — reports "session lock: held" from `exists()` alone (`:2415`).

D5 (refuse on corrupt identity) + N1 (quarantine must not clear recovery) +
D4 (no deletion) leave **no in-band path out**. The contract's terminal state
(entry authority CLOSED until a new independently validated session starts) is
unreachable, because no new session can start.

**Required before requirement 2 is ratified:**
1. Lock writer uses `_atomic_write_json`.
2. Recovery distinguishes *corrupt* from *missing* identity.

## Prerequisite work with no substrate on main

| Requirement | Gap | Anchor |
|---|---|---|
| N1 proof bound to session/tick/connection | BrokerReconciler / BrokerOrderObservation have **zero production callers**; client_id is a static config default, not a per-connection identity | `broker_reconciliation.py:107-172`; `config.py:70` |
| N1 timestamp after last effectful event | Live path stamps `checked_at=context.cycle.started_at` *before* `broker.positions()` and `read_open_orders` | `cycle_adapter.py:311-314`, `:299-300` |
| Req 1 archive evidence before mutation | No archive primitive exists anywhere in the engine | grep for archive/shutil.copy/shutil.move returns 0 |
| Req 6 fencing-token CAS | Three independent recovery latches; only gate.json has a fencing token. execution_outbox.py has zero session_id occurrences in 568 lines | `paperday.py:115`; `scan_receipts.py:480`; `execution_outbox.py:472` |
| N2 CleanStopReceipt | stop() never calls find_unmatched_ticks; no outbox handle in paperday.py. Two of five assertions not derivable | `scheduler.py:530` |
| D8-C status from receipts | Zero durable per-gate receipts exist; entry_gate_preflight is read-only, no accumulator, returns on first refusal | `paperday.py:456-731`, `:2280-2284` |

## Defects found that are independent of this contract

1. **Recovery-marker fail-open.** `_mark_recovery_required` does
   `gate = read_gate(...)` then `if gate is None: return` (`:1998-2000`) —
   silently declines to record recovery when the gate is unreadable.
2. **D11 already violated.** `write_gate` (`:424-452`) omits recovery_reason, so
   the reason set at `:1126`/`:2002` is dropped by the next write; status()
   falls back to "unresolved" (`:2410`).
3. **checked_at precedes the observations it certifies** (see table above).
4. **Torn-write surface is wider than the lock** — nine separate write
   implementations, no shared atomic helper; `freshness.py:328-330` uses a fixed
   .tmp name with no fsync; `universe.py:809-816` has no fsync.
5. **Two classes both named ExecutionOutbox** (`order_outbox.py:356`,
   `execution_outbox.py:279`) rooted at the same directory
   (`cycle_adapter.py:713`, `runner.py:2713`) with different layouts.
6. **`cycle_adapter.py:777`** calls unmatched() with no session filter, so a
   prior session's scans block the next one.
7. **`report.add("final mark", True, ...)`** is hardcoded True (`:1927`), so
   clean survives a failed mark.
8. **SCANBOOK_INCOMPLETE** is absent from FAILURE_CODES and does not set
   entry_refusal_code — operators filtering on either miss the refusal.
9. **`AUTOTRADER-CYCLE.md:69`** tells the operator to hash the *seed* catalog for
   -CatalogSha256 while policy pins the *operator* catalog. FAIL-CATALOG-HASH
   (`cycle_adapter.py:684-685`) would refuse an ARMED run.

## Deferred, with owner and revisit trigger

| Item | Owner | Revisit trigger |
|---|---|---|
| Reviewer gate demotion (f1e819f) | Nalakram | Only as an independent authority decision, never bundled |
| armed=True at cycle_adapter.py:686 (D14) | Nalakram | After the mode matrix + review-only non-transmission tests exist |
| Next paper session date | Nalakram | After audited recovery lands and is independently verified |
| Gate 12 closure | Nalakram | After CLI corridor fix + incomplete-coverage legacy admission closed |
