# Paper-day recovery — decision record

Status: **DRAFT — awaiting approval**
Decision owner: Nalakram
Date: 2026-08-18
Repository state at ruling: `main` @ `bc407a1`, clean tree (4 untracked paths)
`implementation_authorized: false` — nothing in this document has been built.

## Context

The 2026-08-14 paper session ended `STOPPED_TICK_ABORTED` / `clean_exit: false`,
leaving `engine/.engine/paperday/gate.json` at `state: PAPER_DAY_BLOCKED`,
`entry_gate: CLOSED`, `recovery_required: true`. The latch is inherited by every
subsequent session (`paperday.py:1061` -> `_recovery_required_on_disk`
`:1072-1081`) and no code on `main` writes it `False`.

Two forks were opened and are resolved here: how the latch is cleared (Fork 1),
and whether to arm paper entries (Fork 2).

## Settled decisions

| ID | Decision | Ruling |
|----|----------|--------|
| D1 | Hold the Gate-14 rule (no FULL entry authorization until Gates 12 and 13 close) | **HOLD.** Gate 12 stays closed. Complete scan coverage does not prove candidate eligibility. |
| D2 | Scope of the next paper session | **Management/reconciliation only, objective = clean stop.** No entry packets, no arming. Applies to the session *after* audited recovery lands, not to 2026-08-18. |
| D3 | Auto-clear vs explicit operator verb | **Both, fail-closed.** Auto-clear only behind a precise fail-closed clean predicate; ambiguous broker state requires an explicit operator recovery verb. Documentation promising recovery does not justify automatic clearing. |
| D4 | Unblock the current dirty state | **Do not run on 2026-08-18. No deletion of state files.** Missing receipts are not proof that nothing happened (`PAPER-DAY-AUTHORITY.md:45`, `AUTOTRADER-CYCLE.md:107`). |
| D5 | Authority tier for the recovery verb | **No FULL-start ceremony.** Persisted exact session identity + fencing-token CAS + exclusive recovery lock + reason. Operator-supplied hashes MUST NOT reconstruct missing authority state. Missing or corrupt identity => refuse. |
| D8 | Gate ledger location | **Split authority.** Gate definitions and acceptance criteria tracked in-repo; current status derived from durable receipts, never hand-maintained. |
| D9 | Hardcoded-SPY scope | **Narrow fix now.** The cycle's `symbol: SPY` is telemetry and is out of scope. Remediation targets the operator CLI / shared entry corridor. |
| D10 | Reviewer seat | **Confirm first.** Validate the 900 s TTL and epoch binding before depending on them. Liveness evidence is not authorization evidence. |
| D11 | `recovery_reason` persistence | **Through a full cycle, in an immutable receipt.** Adding a field to `gate.json` is insufficient if later writes drop it. |
| D12 | Recovery predicate completeness | **All three additions required:** outbox blocking checks, fresh broker reconciliation, fencing-token CAS. |
| D13 | Reviewer gate demotion (`f1e819f`) | **Rejected as a side effect.** Independent authority decision; must not arrive bundled with recovery. |
| D14 | `armed=True` at `cycle_adapter.py:686` | **Decide after a mode matrix + negative tests** prove review-only cannot transmit. Authority-sensitive, not an implementation detail. |
| D3' | How to land the recovery capability | **Neither cherry-pick nor branch merge.** Land a deliberately scoped recovery PR with compatibility tests. |
| N1 | Broker-proof freshness | **Separate TTL, max 300 s**, distinct from the reviewer's 900 s. Timestamp after the last potentially-effectful event; bound to session, tick, order, execution, account, broker connection. No fresh observation => recovery unreachable. Quarantine/archive may preserve evidence but MUST NOT clear recovery. |
| N2 | Clean-stop assertion | **`stop()` emits a durable `CleanStopReceipt`** asserting clean exit, no unmatched ticks, no outbox blockers, no stale lease, no residual opening authority. `paper-day-status` displays and validates; it never manufactures the assertion. |
| N3 | Schema compatibility | **Both v1 and v2 via explicit adapters** plus negative compatibility tests. Never silently reinterpret v1 state as SESSION_ARM state. A v1-only recovery contract is not acceptable long-term. |
| N4 | Sequencing | **Parallel implementation, serialized integration.** Mode matrix and review-only non-transmission tests are required before any recovery code that writes authority state may merge or be used operationally. |

## Recovery acceptance bar (binding)

A recovery operation MUST:

1. lock exclusively;
2. verify exact session, lease, and process identity;
3. reject unreadable or unknown state;
4. prove no unmatched ticks and no opening outbox records remain;
5. reconcile broker positions, orders, and executions;
6. use fencing-token CAS before clearing;
7. archive evidence before mutation;
8. persist the reason and the reconciliation receipt;
9. leave entry authority CLOSED until a new, independently validated session starts.

## Rejected options and why

- **Hand-deleting `gate.json` + `autocycle/schedule.json`** — destroys crash
  witnesses; contradicts `AUTOTRADER-CYCLE.md:123`. Also insufficient: entry
  additionally requires a live reviewer watcher during start.
- **Cherry-picking `c2695b2`** — `paperday.py:53` imports `SESSION_ARM` at module
  level from `6e4b02f`; cherry-picking alone makes every `import engine.paperday`
  raise ImportError. Its own diff also removes the reviewer-liveness authority
  block.
- **Merging `mission/integration`** — bundles `f1e819f` (reviewer demoted to
  advisory-only), rejected under D13.
- **Arming on 2026-08-18** — Gate 12 red, Gate 13 partial, Gate 14 unreachable.
