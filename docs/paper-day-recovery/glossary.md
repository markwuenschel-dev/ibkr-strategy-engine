# Paper-day recovery — canonical terms

One term, one concept. Where the codebase currently uses a term ambiguously,
the ambiguity is named so it can be repaired rather than inherited.

| Term | Meaning | Anchor |
|------|---------|--------|
| **recovery latch** | The persisted `recovery_required` boolean in `gate.json`. Sticky: re-read and re-published on every start. | `paperday.py:115`, `:1072-1081` |
| **recovery reason** | Free-text cause recorded when the latch is set. Currently destroyed: `write_gate` never emits the key, so it is lost on the next gate write. | `paperday.py:424-452` |
| **unmatched tick** | A `TICK_STARTED` with no terminal record (`TICK_FINISHED` / `ABORTED` / `UNRESOLVED` / `RECONCILED`). Means a worker held transmit authority and never recorded the outcome. | `scheduler.py:543-563` |
| **entry gate** | `OPEN` / `CLOSED` / `PROOF_ONLY` field of `gate.json`, read by `entry_gate_preflight`. Distinct from the recovery latch: closing the gate refuses new entries; the latch refuses even when the gate would open. | `paperday.py:657`, `:700-704` |
| **fencing token** | Per-session opaque token in `session.lock` and `gate.json`. Required for a safe compare-and-swap gate write. | `paperday.py:2007-2013` |
| **broker proof** | Timestamped evidence that engine state agrees with broker truth. Under N1: max 300 s old, bound to session, tick, order, execution, account and broker connection. **Not** a policy hash. | this contract, N1 |
| **CleanStopReceipt** | New durable artifact emitted by `stop()` asserting the five clean-stop conditions. Does not yet exist. | this contract, N2 |
| **mandate** | `MANAGE_ONLY` or `FULL`. Sets `authority_required`. Not a transmission control. | `paperday.py:806-810` |
| **mode** | Policy field: `DRY_RUN` / `SHADOW` / `REVIEW_ONLY` / `ARMED`. Only `ARMED` transmits. Independent of mandate. | `autotrader_policy.py:66`, `autocycle.py:163` |
| **`EntryMode`** | **Ambiguity — do not conflate with `mode`.** A distinct runner enum with only `MANAGE_ONLY` and `FULL`. There is no `EntryMode.REVIEW_ONLY`. | `runner.py:182`, `:194-195` |
| **`FAIL-INCOMPLETE-COVERAGE`** | **Misnomer — do not use as evidence of coverage enforcement.** Fires only when the coverage *SLA is misconfigured* (`<= 0`). | `runner.py:2800-2803` |
| **`SCANBOOK_INCOMPLETE`** | The real incomplete-coverage refusal. Nulls the book on the entry path. Not present in `FAILURE_CODES` and does not set `entry_refusal_code`. | `scanbook_store.py:569-574`, `runner.py:1966-1974` |
| **`automated_entry_allowed`** | Computed property, never a JSON field. Requires active, scan- and entry-eligible, optionable, classified, venue-verified, entitlement-allowed. | `catalog.py:319-330` |
| **seed catalog** | `autotrader-catalog-seed-80-v1.json`, SHA `f2035e99…`. Scan-only: 0 entry-allowed. Git-tracked. | `AUTOTRADER-CYCLE.md:24-36` |
| **operator catalog** | `autotrader-catalog-operator-v1.json`, SHA `31e5a22f…`. **Not** scan-only: 5 entry-allowed (SPY, IWM, GLD, XLE, XLF). Untracked; pinned by policy. | `autotrader-policy-review-only.json:35-39` |
| **liveness evidence** | Reviewer answered a handshake within its TTL. **Not** authorization evidence. | `paperday.py:616-636`, `:120` |
