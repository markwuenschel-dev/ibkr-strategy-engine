# Paper-day recovery — design contract

Status: **DRAFT — APPROVAL BLOCKED** (see open-questions.md BLOCKER-1).
Date: 2026-08-18. implementation_authorized: false.

## Problem

A dirty stop latches recovery_required=true in gate.json. The latch is inherited
by every later session and nothing on main clears it. Documentation promises the
opposite: `bin/start-paper-day.ps1:13` says "a stale one is recovered" and
`AUTOTRADER-CYCLE.md:111` says startup reconciles five categories of state.
Neither happens.

## Non-goals

- Arming paper or live entries. Gate 14 stays off (D1).
- Demoting the reviewer gate to advisory-only (D13).
- Changing report-only telemetry because it displays SPY (D9).
- Merging mission/integration or cherry-picking c2695b2 (D3').

## Mechanism

An explicit operator recovery verb, fail-closed, satisfying the nine-point
acceptance bar in decisions.md. Auto-clear on a proven-clean predicate is
permitted (D3); ambiguous broker state requires the explicit verb.

Authority (D5): persisted exact session identity, fencing-token CAS, exclusive
recovery lock, non-empty reason. No FULL-start hash ceremony. Operator-supplied
hashes MUST NOT reconstruct missing authority state.

Broker proof (N1): max 300s old, distinct from the reviewer's 900s TTL,
timestamped after the last potentially-effectful event, bound to session, tick,
order, execution, account and broker connection. No fresh observation means
recovery is unreachable. Quarantine/archive may preserve evidence but never
clears.

## Ordering (N4 — parallel build, serialized integration)

    P0  atomic session.lock writer + corrupt-vs-missing signal   <-- BLOCKER-1
         |                                                          gates all
         v
    P1  mode matrix + review-only non-transmission tests         <-- N4 merge gate
         |
         v
    P2  recovery verb (acceptance bar) + v1/v2 adapters (N3)
        CleanStopReceipt (N2), reconciliation receipt + reason (D11)
         |
         v
    P3  independent verification, then and only then a
        management-only session whose objective is a clean stop (D2)

Parallel and not gating: CLI corridor fix (D9), gate-definition tracking (D8).

## Verification surface

- `test_options_no_transmit.py` already enforces the single-chokepoint invariant
  at AST level, but walks engine/options/ only (`:215-230`) — a recovery verb in
  paperday.py falls outside its coverage and needs the walk extended.
- Negative compatibility tests for v1/v2 (N3): never reinterpret v1 state as
  SESSION_ARM state.
- Torn-lock recovery test: the BLOCKER-1 scenario is currently untested.

## Rollout

No rollout waves. Single-operator local system; the only deploy is a merge to
main behind the N4 gate, followed by P3.
