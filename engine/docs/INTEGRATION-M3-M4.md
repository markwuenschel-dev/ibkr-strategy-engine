# M3↔M4 Integration Contract — Universe Scanner ↔ Logical-Entry Manager

**Status: BINDING for the Lane A / Lane B merge.** Written 2026-08-01 against the
worktree at `feat/options-domain-model` (this tree). Where this document and
landed code disagree, the landed code wins and the divergence must be raised at
merge, not papered over.

**Epistemic key** — every load-bearing claim below is labeled:

- `[verified]` — read in this tree this session, `file:line` cited.
- `[provisional]` — a name proposed by this contract before the lane's file
  landed.

**LANE FILES LANDED MID-DRAFT.** Sections 1-8 were written against a tree
where `universe.py`, `universe_data.py` and `logical.py` did not exist; while
this document was being finished, both lanes landed (`universe.py`,
`universe_data.py`, `logical.py`, `tests/test_options_logical.py`, plus an
`options-universe-scan` command in `cli.py`) `[verified — all read in full]`.
**Section 9 reconciles this contract against the landed code and is
authoritative wherever they differ**; the earlier sections are kept because
their runner-side analysis (§0, §3-§6) is unaffected and their reasoning is
the review trail for §9's verdicts. Where a §1-§8 name is marked
`[provisional]`, §9 gives the landed name.

---

## 0 · The ground the contract stands on

The runner is a single pass: reconcile (`runner.py:1142`), manage every open
position (`runner.py:1150-1166`), then consider **one** entry
(`runner.py:1168` onward) `[verified]`. The entry section's shape, in order:

| Block | Lines (runner.py) | What it does |
|---|---|---|
| Unresolved-order gate | 1173-1181 | any `is_uncertain` position blocks entries |
| Reconciliation gate | 1183-1190 | only `RECONCILED` may open new risk |
| `-- 3.` regime | 1192-1231 | classify; live mode scales `risk_budget_per_position` (1226-1231) |
| Candidate build | 1233-1254 | `_build_candidate` → chain → quotes → delta selection → intent |
| IV-rank wall | 1256-1274 | shadow-mode only |
| First risk/governor pass | 1276-1319 | what-if, portfolio snapshot rebuilt with `store.exposures()` (1287-1292), `assess_candidate`, `PortfolioGovernor.evaluate` |
| `-- 3b.` preflight | 1321-1340 | caller's refuse-only hook |
| `-- 4.` verifier required | 1342-1353 | no gate ⇒ `OPTIONS_VERIFIER_NOT_CONFIGURED`, fail closed |
| `-- 3c.` binding revalidation | 1355-1432 | fresh two-sided quotes on the selected legs (1371-1385), fresh what-if (1386), fresh portfolio snapshot (1387-1405), fresh risk (1406-1417) and governor (1418-1423); any refusal returns (1424-1432) |
| Packet + authorize | 1434-1477 | `packet_for` (1434), `authorize_open` (1453); `AwaitingVerification` recorded and returned, never waited on (1463-1472) |
| Record-then-send | 1479-1503 | `record_open_submitted` **before** `place_combo` (1487, 1490) |
| Reprice ladder | 1505-1568 | each rung re-verified (1522) and re-recorded (1538-1542) |

`[verified]` throughout.

Two already-landed facts do most of the work in this contract:

1. **`build_vertical` accepts a caller-supplied `strategy_id`**
   (`selection.py:678`, applied at `selection.py:728`:
   `strategy_id=uuid4() if strategy_id is None else strategy_id`) `[verified]`.
   The runner currently passes none (`runner.py:918-924` calls it via
   `_build_candidate` with no id) `[verified]`, so every pass mints a fresh
   intent id.
2. **The approval spec digest binds `intent_id`** (`AuthorizedOrderSpec`
   field at `approval.py:468`, hashed at `approval.py:487-504`), and
   `CollabVerifierGate` keys its request marker by that digest
   (`approval.py:1092-1093, 1098-1110`) `[verified]`.

Put together: **the current runner cannot complete an
`AwaitingVerification` round-trip across passes for a rebuilt candidate.**
Pass N files a request for intent-id X; pass N+1 rebuilds the same structure
under a fresh uuid, gets a different spec digest, and files a *new* request —
the pass-N approval can never match. The gate's idempotency
(`approval.py:1114-1125`) works exactly as designed; it is the caller that
changes its own key every pass. This is not a defect in today's single-shot
`--symbol` operation (the operator re-runs and re-reviews), but it is the
precise reason the LogicalEntry exists: **a pending entry must carry a stable
`strategy_id` so its rebuilt packet lands on the same outstanding request.**
`[inferred from the two verified facts above]`

---

## 1 · Nomination handoff: the one record shape

### The shape

One record type, defined **once**, crossing the M3→M4 boundary. Name
`[provisional]`: `Nomination`. Fields (all frozen, all plain data):

```python
@dataclass(frozen=True)
class NominationLeg:                    # PROVISIONAL
    con_id: int                        # qualified — the scanner ran qualify_strikes
    symbol: str
    expiration: datetime.date
    strike: Decimal
    right: str                         # "P" | "C" — OptionRight.value
    action: str                        # "SELL" | "BUY" — OrderAction.value
    ratio: int
    multiplier: int
    exchange: str
    trading_class: str

@dataclass(frozen=True)
class Nomination:                       # PROVISIONAL
    underlying: str
    family: str                        # StrategyType.value, e.g. "PUT_CREDIT_SPREAD"
    direction: str                     # Bias.value — selection.Bias exists [verified selection.py import in runner.py:85-93]
    expiration: datetime.date
    legs: tuple[NominationLeg, ...]    # short first, then wing(s)
    # -- ranking evidence: why the scanner ranked this row where it did --
    evidence: Mapping[str, str]        # iv_rank, iv_percentile, credit_estimate,
                                       # spread_width, open_interest / volume floors met,
                                       # quoted_at (ISO), freshness_class per figure
    # -- provenance --
    scanbook_row: str                  # the ScanBook row id this came from
    scanned_at: datetime.datetime      # tz-aware
    configuration_version: str
```

Leg fields are exactly the constructor surface of `OptionLegIntent`
(`domain.py:138-149` — `con_id, symbol, expiration, strike, right, action,
ratio, multiplier, exchange, trading_class`; the scan builds legs from
precisely these at `scan.py:379-404`) `[verified]`, so the entry path can
rebuild real legs without re-deriving anything but **prices**.

### What a Nomination deliberately is NOT

**The scanner must not construct `OptionStrategyIntent`.** Three reasons, each
anchored:

1. An intent carries `limit_price`, `quantity`, `maximum_loss_per_contract`
   and `created_at` (`domain.py:291-302`) `[verified]` — all binding facts.
   Price and size must come from the market *at authorization time*, not scan
   time: `freshness.py:21-26` classifies quotes/mids as `PERISHABLE`, "never
   cached across passes... nothing perishable may feed a binding
   authorization" `[verified]`.
2. The intent's `strategy_id` is the id the position store keys on
   (`positions.py:784-791`) and the id the approval spec binds
   (`approval.py:468`) `[verified]`. A scanner-minted id would leak scan-time
   identity into the review and the book.
3. The no-bypass rule (§5): a module that can build intents is one import away
   from the packet/authorize surface. The scanner stays on the data side of
   that line entirely.

The evidence mapping is strings-only on purpose: it feeds the packet's
evidence section, which renders values verbatim and marks absences `MISSING`
(`approval.py:548-554, 565-587`; the runner's evidence builder drops `None`s
at `runner.py:1051`) `[verified]`. Scan-time figures ride along as *reviewer
context* labeled with their `scanned_at`; they are never substituted for the
binding-time figures the 3c block re-establishes.

### Who owns the type — recommendation

**Own it in `universe_data.py` (Lane A's data module); `logical.py` imports
it.** `[provisional names]`

Rationale: dependency direction. The scanner produces the shape and the
manager consumes it; a data-only module with **no** imports from `transmit`,
`approval` or `runner` can be imported by both sides without giving the
scanner an import path toward authorization machinery (§5) or giving the
manager a dependency on scan mechanics. This mirrors the tree's existing
pattern: `portfolio.py` exists precisely so the governor and its supplying
port "can both name these types without either importing the other"
(`portfolio.py:3-5`) `[verified]`.

Counter-argument, stated fairly: putting the type in `logical.py` would let
`claim()` type-check its input without any cross-lane import, and the
consumer-owns-the-interface rule is respectable. It loses because it inverts
the import arrow — `universe.py` would then import `logical.py`, and the
scanner would see `LogicalEntryManager` and everything `logical.py` imports.
If the lanes land with the type elsewhere, the merge rule is: **whichever
module holds it must import none of `transmit`/`approval`/`runner`**, and the
coordinator moves the type rather than adding the import.

---

## 2 · State ownership: who writes which ScanBook status

ScanBook row states `[provisional]`: `CANDIDATE → CLAIMED_BY_LOGICAL_ENTRY`
(one-way), and `CANDIDATE → SUPERSEDED` (one-way). No other transitions. A
claimed row is **terminal for the ScanBook** — its subsequent story is the
LogicalEntry's, recorded in the manager's own ledger and, once transmitted, in
the position store (`positions.py:768-797`) `[verified]`.

### The writer split

| Transition | Writer | Mechanism |
|---|---|---|
| `CANDIDATE → CLAIMED_BY_LOGICAL_ENTRY` | **LogicalEntryManager**, at the instant it creates the LogicalEntry | via a narrow writer interface (below), compare-and-set: succeeds only if the row is still `CANDIDATE` |
| `CANDIDATE → SUPERSEDED` | **the scanner**, on its next pass, for rows its fresh ranking no longer nominates | same CAS: a row that is no longer `CANDIDATE` is left alone |

**Recommendation: the manager writes the claim, not the coordinator in the
runner.** The claim must be atomic with LogicalEntry creation — one
transition, one writer, one instant. Routing the write through the runner
would open a window (entry created, row still `CANDIDATE`) in which a second
pass, or a concurrent `--symbol` invocation, sees a claimable row that is
already owned. The runner's own record-before-transmit discipline is the
precedent: the state change and the action that depends on it are never
allowed to be separated by a crash window with the permissive reading
(`runner.py:1484-1487`, `positions.py:775-781`) `[verified]`.

Counter-argument: coordinator-written state keeps the ScanBook
single-writer (scanner only) and the manager pure. It loses because it buys
purity with a race, and because "single writer" is preserved anyway by the
narrowness of the interface — the manager can claim and nothing else.

### The narrow writer interface

The manager is handed a **writer**, never the ScanBook itself:

```python
class ScanBookClaimWriter(Protocol):    # PROVISIONAL name; shape is BINDING
    def mark_claimed(self, row_id: str, *, entry_id: UUID, at: datetime) -> bool:
        """CAS CANDIDATE→CLAIMED_BY_LOGICAL_ENTRY. False if the row was not CANDIDATE."""
    def mark_superseded(self, row_id: str, *, reason: str, at: datetime) -> bool:
        """CAS CANDIDATE→SUPERSEDED. False if the row was not CANDIDATE."""
```

`bool`, not exception, for the lost race: a `False` from `mark_claimed` means
"someone else owns it, move to the next nomination" — an ordinary outcome, not
an error. Invalid *transitions* (claiming a `SUPERSEDED` row id that never
existed, re-claiming with a different entry id) raise. The executable
reference implementation is `RecordingScanBookWriter` in
`tests/integration_support.py` (§7).

### The ownership invariant

> **At most one object owns a pending review, and it is always the
> LogicalEntry.** A ScanBook row can never own one, because the scanner has no
> import path to `packet_for`/`propose` (§5) — a row cannot file a request at
> all. Two LogicalEntries can never own the same nomination, because claim is
> CAS on `CANDIDATE`. And one LogicalEntry cannot hold two live requests,
> because the request marker is keyed by spec digest
> (`approval.py:1092-1093`) and the entry's stable `strategy_id` plus
> unchanged structure yields the same digest — while any structural/price
> change yields a new digest, which is the invalidation rule working, not a
> second ownership (`approval.py:1098-1105`) `[verified]`.

---

## 3 · Runner wiring plan

### Where the manager goes

**Insertion point: `runner.py:1192`** — after the unresolved-order gate
(1173-1181) and the reconciliation gate (1183-1190), replacing the current
straight-line entry flow from the `-- 3.` regime block down. Everything above
that line is untouched: reconcile-first and manage-before-enter are
load-bearing orderings (`runner.py:11-27`) `[verified]` and the manager sits
strictly on the entry side of them, so both entry gates apply to *all*
logical-entry work exactly as they apply to today's entry.

Per pass, `manager.service(...)` `[provisional]` does, in order:

**(a) Service pending logical entries FIRST.** For each LogicalEntry in
`AWAITING_VERIFICATION` (oldest first):

1. Re-qualify/rebuild the candidate from the entry's stored legs with the
   entry's **stable id**: `build_vertical(..., strategy_id=entry.strategy_id)`
   (`selection.py:678,728`) `[verified]`, priced from fresh quotes exactly as
   `_build_candidate` prices today (`runner.py:894-916`).
2. Run the **existing** binding-revalidation machinery — the 3c block,
   verbatim: two-sided re-quote of the selected legs (`runner.py:1371-1385`),
   fresh what-if (1386), fresh snapshot rebuilt with `store.exposures()` plus
   §4's reservations (1387-1405), fresh risk and governor (1406-1423).
3. `packet_for` + `authorize_open` (1434-1477). If the market held, the spec
   digest matches the outstanding request and the reviewer's answer (if
   arrived) authorizes; if it moved, the new digest files a new request and
   the entry stays `AWAITING_VERIFICATION` under its new packet. Both are the
   design working.
4. On authorization: `record_open_submitted` → `place_combo` → ladder →
   outcome, unchanged (1479-1569), and the entry goes terminal.

**(b) Then claim at most ONE new nomination**, and only if capacity allows:
no entry currently pending (one outstanding review at a time — the same
"consider one new entry" cardinality the pass has today, `runner.py:1` and
the single-candidate flow `[verified]`), plus the governor's own caps, which
are enforced downstream at authorization regardless
(`governor.py:72-101` check set) `[verified]`. A claim runs the regime block
(1192-1231) and the same build/gate/packet path as (a), producing either a
transmitted entry, a pending entry, or a terminal refusal that releases the
claim (§4).

Pending-first ordering is binding: a pass that claims before servicing could
strand an approved-and-waiting entry behind a fresh review forever, and the
whole point of `AwaitingVerification` is that "the next pass picks the answer
up" (`runner.py:1463-1472`) `[verified]`.

### Which existing blocks move, which stay

| Block | Disposition |
|---|---|
| Gates at 1173-1190 | **stay in the runner**, ahead of the manager — they gate all entry work |
| Regime 1192-1231 | moves into the claim path (b); a serviced pending entry re-runs classification too, since the packet evidence states the tier (`approval.py:575-578`) `[verified]` |
| `_build_candidate` 1233-1254 | becomes the shared rebuild used by both (a) and (b) — with `strategy_id` threaded through to `build_vertical` |
| IV-rank wall 1256-1274 | claim path only, shadow mode only (unchanged semantics) |
| First risk/governor pass 1276-1319 | claim path only — a cheap early refusal before a review is ever filed; the binding pass re-runs both anyway |
| 3b preflight 1321-1340 | stays, both paths |
| 3c → 4 → transmit → ladder, 1355-1569 | **extracted into one shared function** (working name `_authorize_and_transmit_entry` `[provisional]`) called by (a), (b), and the `--symbol` path |

That extraction is the "no second state machine" mechanism: there is exactly
one corridor from candidate to transmitted order, and the manager, the
`--symbol` path, and any future caller all walk it. The corridor's own gates
(verifier-required at 1347-1353, binding refusals at 1424-1432, token-gated
`place_combo` — `transmit.py:246-256` `[verified]`) are what make a new
caller safe, not review of the caller.

### The `--symbol` fresh-id path: retire or keep?

**Recommendation: keep it, unchanged, for explicit operator invocation; it is
not reachable for scanner-nominated entries.** When a manager is wired,
scanner nominations flow only through `manager.service`; `run_once` with an
explicit `--symbol` remains the operator's single-shot tool and continues to
mint a fresh id per invocation.

The bypass-path risk, stated plainly: this is a second door to
`packet_for`/`authorize_open`, and two doors can drift. Why it is acceptable:
(i) the door leads into the **same extracted corridor**, so drift would have
to be introduced inside a shared function; (ii) every safety property lives
in the corridor and below it — verifier fail-closed (`runner.py:1347-1353`),
digest binding (`approval.py:487-504`), the unforgeable transmit token
(`transmit.py:246-263`) `[verified]` — none of it is caller-supplied; (iii)
the fresh-id inefficiency (a new review per invocation) is *correct* for a
human-driven one-shot: each invocation is a new proposal and deserves a new
review. The counter-argument — that retiring it entirely removes a door — is
real, but it would also remove the only way to operate the engine without a
scanner, including the paperday and proof harnesses that exist today
(`proof.py:682` uses the preflight to stop before `authorize_open`)
`[verified]`. Retirement can be revisited once the manager has soaked.

---

## 4 · Reservation folding

### The gap being closed

Between "a LogicalEntry exists / a review is pending" and
`record_open_submitted`, the position store has **no record** — exposures come
only from live positions (`positions.py:1290-1292`, filtered by `is_live`,
which includes `OPENING` — `positions.py:307-319`) `[verified]`. The
governor's snapshot is rebuilt from `store.exposures()` at both rebuild sites
(`runner.py:1287-1292` first pass; `runner.py:1387-1398` binding pass)
`[verified]`. So a pending entry's earmarked buying power is invisible: a
second claim in the same or next pass would be sized against a book that
believes that capital is free — the same defect the reprice ladder's
`record_submission` comment warns about (`runner.py:1533-1537`) `[verified]`.

### The fold

A LogicalEntry carries `reservation_id` (= its stable `strategy_id`) and
`reserved_amount` (the what-if `initial_margin_change` observed when the
entry was created, the same figure the runner banks as `bpr` at
`runner.py:1479-1487`) `[verified for the figure's provenance]`. The manager
exposes:

```python
def reservations(self) -> tuple[PositionExposure, ...]:   # PROVISIONAL method name
    # one per non-terminal LogicalEntry not yet in the position store:
    # PositionExposure(underlying=..., buying_power_reserved=reserved_amount,
    #                  maximum_loss=reserved_amount, strategy_id=entry.strategy_id)
```

`PositionExposure` already carries an optional `strategy_id`
(`portfolio.py:81, 88-89`) `[verified]` — the fold needs no new type. Both
snapshot-rebuild sites change from

```python
positions=store.exposures(),
```

to

```python
positions=fold(store.exposures(), manager.reservations()),
```

where `fold` **drops any reservation whose `strategy_id` already appears in
the store's exposures**. `maximum_loss` is set to the reserved amount as a
stand-in (the true defined max loss is not known until an intent is sized);
this only ever *overstates* per-position loss for the concentration checks —
the conservative direction, same posture as the snapshot's max(derived,
reported) rule, which "can only refuse a candidate that a more precise figure
would have allowed" (`portfolio.py:147-159`) `[verified]`.

### Release triggers

| Event | Reservation |
|---|---|
| Store gains any record for the entry's id (`record_open_submitted` at `runner.py:1487` writes an `OPENING` exposure carrying the same reserved figure — `positions.py:784-791`) | released by the id-dedupe automatically — no ledger write needed to be safe |
| Entry terminal: verifier `REFUSED`/`UNAVAILABLE` (`approval.py:1185-1193`), binding refusal (`runner.py:1424-1432`), packet expiry, operator abandon | manager marks the entry terminal; reservation stops being emitted |
| Claim never completed (crash between `mark_claimed` and entry persist) | on restart the manager treats a claimed row with no entry as an orphan: emit the reservation **conservatively** until the operator resolves or a TTL supersedes — never silently free |
| `OPEN_UNCERTAIN` | store exposure exists (`UNCERTAIN` is live, `positions.py:310-318`) — dedupe applies; nothing is freed until reconciliation resolves it `[verified]` |

### Crash-window posture

The id-dedupe makes the steady-state double-count structurally impossible
(same UUID on both sides). The residual windows — crash after
`record_open_submitted` before the manager's ledger flushes, or a manager
ledger that lags the store — resolve to *counting once* (dedupe) or *counting
twice briefly* (distinct ids only if an implementation violates the stable-id
rule, which §3 forbids). Where doubt exists the fold counts **more**, never
less: over-reservation degrades to a refused entry with a named governor code
(`GOVERNOR_TOTAL_BPR_EXCEEDED`, `governor.py:96`) `[verified]`, which is
loud, attributable, and reconcilable from two append-only ledgers. An
under-count degrades to real risk with no witness. Conservative, reconcilable
— per the audit.

---

## 5 · No-bypass enforcement

Every path that can reach `packet_for` / `authorize_open` after integration
(caller enumeration by repo-wide grep, `[verified]` — hits listed are the
complete production set):

| # | Path | Post-integration status | Guard |
|---|---|---|---|
| 1 | `runner.run_once` entry corridor (`runner.py:1434, 1453`) | stays; becomes the shared extracted corridor | verifier fail-closed (`runner.py:1347-1353`); binding revalidation (1355-1432); token-gated transmit (`transmit.py:246-263`) |
| 2 | Reprice ladder rungs (invoked `runner.py:1510-1552`; packet rebuilt inside `transmit.py:792`) | stays, untouched | each rung passes the same `verifier` + `approval_context` (`runner.py:1522-1523`); envelope-bounded (`runner.py:1549`) |
| 3 | `walk.py` execution experiment (`authorize_open` at `walk.py:1018`, `packet_for` at `walk.py:1026`) | stays, frozen — not part of M3/M4 | same gate protocol; bounded caller |
| 4 | `proof.py` execution proof | stays, frozen; uses `entry_preflight` to stop **before** `authorize_open` (`proof.py:682`) | preflight can only refuse (`runner.py:143-153`) |
| 5 | Paperday controller (`tests/paperday_support.py`) | **untouched/frozen** — drives `run_once`, owns no authorization path of its own | inherits corridor guards |
| 6 | **NEW** `logical.py` / `LogicalEntryManager.service` | the only new caller | must call the extracted corridor function — it re-implements nothing; merge review rejects any `packet_for`/`authorize_open` call in `logical.py` that is not the corridor |
| 7 | **Universe scanner** (`universe.py`, `universe_data.py`) | — | **zero imports** of `engine.options.transmit`, `engine.options.approval`, `engine.options.runner`, `engine.options.reprice`, `engine.options.walk`. Enforced by a test, not by convention (below) |

The `--symbol` decision (keep, justified) is #1's second entrance and is
argued in §3.

**Enforcement test for #7** (goes in Lane A's test file or the integration
suite). One approach that does NOT work, stated so nobody builds it: walking
`sys.modules` after importing the scanner. `engine/options/__init__.py`
re-exports the authorization surface (`packet_for` at `__init__.py:26`,
`authorize_open` at `__init__.py:66`) `[verified]`, so importing *anything*
under `engine.options` loads `transmit` and `approval` into `sys.modules` —
confirmed by running it: importing only `marketdata`/`ports` leaves both in
`sys.modules` `[verified by execution]`. The workable guard is
**source-level**: parse `universe.py` / `universe_data.py` with `ast`, walk
`Import`/`ImportFrom` nodes *and attribute access on any `engine.options`
name*, and assert none of the five forbidden modules (or their re-exported
symbols `packet_for`, `authorize_open`, `authorize_close`, `place_combo`) is
named; **plus** a plain-text grep assertion for the same names. Two
mechanisms on purpose: the M9 audit showed a single AST guard has holes
(D5 — non-recursive glob, `Call.func`-only inspection let three synthetic
bypasses through, LEDGER.md "D5" entry) `[verified in LEDGER.md:416]`.
Neither alone is claimed sufficient.

One further structural guard already in force and inherited free:
`place_combo` cannot be called without a `TransmitAuthorization` minted by
`authorize_open`/`authorize_close` (`transmit.py:246-263`; daily count at
`transmit.py:457`) `[verified]` — so even a hypothetical scanner that somehow
built a packet still could not transmit.

---

## 6 · Pacing priorities at integration

The five-level order is landed and documented as the 2026-08-01 audit's,
verbatim (`pacing.py:42-58`) `[verified]`. The caller→priority assignment:

| Priority (`pacing.py:53-57`) | Callers after integration |
|---|---|
| 1 `EXITS_MANAGEMENT` | management-path quotes/marks (`runner.py:1150-1166` / `_manage_one` quote fetch `runner.py:382-389`), reconciliation reads |
| 2 `WORKING_ORDERS` | reprice-ladder polling and cancel/replace (`runner.py:1510-1552`), open-order enumeration (`runner.py:697-701`) |
| 3 `AUTHORIZATION` | the binding-revalidation reads — re-quote (`runner.py:1371-1378`), what-if (`runner.py:1386`), portfolio snapshot (`runner.py:1389`) — for **both** serviced pending entries and fresh claims |
| 4 `CANDIDATE_CONSTRUCTION` | the claim path's chain/qualify/quote/what-if work (`_build_candidate`, `runner.py:1233-1254`) and the `--symbol` build |
| 5 `DISCOVERY` | the universe scanner's sweep, and **only** the scanner |

Budget mechanics that make the assignment meaningful `[verified]`: one budget
per broker connection shared by every consumer (`pacing.py:76-83`);
priorities 1-2 may spend the 25% management reserve, 3-5 may not
(`pacing.py:133-148`); a broker pacing penalty halves refill, zeroes tokens
and pauses discovery for a full penalized window (`pacing.py:187-199`).

The budget is already threaded into two adapters, opt-in
(`budget: Any = None`): `IBKRVolatilityHistoryAdapter` acquires GENERAL then
HISTORICAL, defaulting to DISCOVERY (`adapters.py:250-253, 260-263`), and
`IBKRLiveMarketDataAdapter.strategy_quotes` acquires one GENERAL token per
subscription line up front (`adapters.py:355-367`) `[verified]`. **One landed
heuristic conflicts with the table above and must be resolved at wiring
time:** the quote adapter maps `require_two_sided=True` to
`EXITS_MANAGEMENT` unconditionally, on the stated assumption that a two-sided
demand "is by definition about a held structure's own legs"
(`adapters.py:322-327, 358-362`) `[verified]` — but the binding-revalidation
re-quote also demands two-sided for a candidate that is **not** held
(`runner.py:1371-1378`) `[verified]`. Left as is, every serviced entry's
binding re-quote would spend the management reserve at priority 1 instead of
waiting at AUTHORIZATION. Resolution (coordinator's wiring step): pass an
explicit `budget_priority` per call site and make the explicit priority
outrank the two-sided heuristic — the heuristic stays only as the default for
callers that state nothing.

**Where `DiscoveryPaced` may surface:** inside the scanner's sweep loop only
— it is raised exclusively for `Priority.DISCOVERY` acquires during a penalty
(`pacing.py:164-174`), and the scanner catches it, records it, and stands
down for the pass. **Where it must never surface:** `manager.service`, the
runner's entry corridor, management, reconciliation — every other priority
"waits; none is ever refused" (`pacing.py:162-163`) `[verified]`. A
`DiscoveryPaced` appearing in a `RunReport.errors` is therefore itself a
defect signature: some caller mislabeled its acquire as DISCOVERY. Worth a
one-line assertion in the integration suite.

---

## 7 · Executable fixtures — `tests/integration_support.py`

Landed alongside this document. Import-clean by construction: it imports only
landed modules (`engine.options.marketdata`, `engine.options.ports`,
`engine.options.portfolio`) plus stdlib — **no** `universe`, **no**
`logical`, so it imports before, during and after either lane lands, and its
shapes are duck-typed so whichever lane's concrete names win, the fixtures
still fit. What it provides:

| Fixture | Contract section it executes |
|---|---|
| `nomination(...)` — deterministic fake-nomination factory (con_ids derived from strikes, fixed clock, stable digest-friendly field ordering) | §1 shape |
| `market_static_port()` / `market_moved_port()` — a scripted two-pass quote-port pair built on the same `leg_mid`/provenance idioms as `test_options_runner.py`'s `FakeMarketDataPort` (`test_options_runner.py:348-393`) `[verified]`; pass boundaries are explicit via `next_pass()` | §3(a): same digest across passes vs. digest moved ⇒ new review |
| `RecordingScanBookWriter` — in-memory CAS implementation of §2's writer protocol, transition-recording, invalid-transition-raising | §2 invariant |

The two-pass ports deliberately mirror the runner-test idiom of scaling every
leg mid together (`price_factor`, `test_options_runner.py:350-353`)
`[verified]` so a "market moved" pass changes the credit — and therefore the
structure digest and spec digest — without changing which strikes get
selected.

---

## 8 · Divergences found between the audit contract and the landed code

1. **The cross-pass pending-review flow the audit assumes does not exist in
   the runner yet.** §0's fresh-`strategy_id`-per-pass analysis: the
   machinery (stable-id parameter on `build_vertical`, digest-keyed request
   reuse in the gate) is fully landed, but no caller threads a stable id, so
   `AWAITING_VERIFICATION` → approved-next-pass is currently unreachable for
   rebuilt candidates. Not a defect in today's operation; it is the work item
   Lane B exists to do, and this contract pins how.
2. **Block ordering label vs. execution order.** The binding-revalidation
   block is labeled `-- 3c.` but executes *after* the `-- 4.`
   verifier-required check (`runner.py:1342-1353` before `runner.py:1355`)
   `[verified]`. Deliberate (fail closed before spending broker requests on
   revalidation) but the labels will mislead whoever does the §3 extraction —
   keep the execution order, fix the comments.
3. **The pacing budget is threaded but not wired.** The acquire sites exist
   in two adapters, opt-in behind `budget: Any = None`
   (`adapters.py:250-263, 355-367`) `[verified]`, but no production
   constructor passes a budget: the runner builds `IBKRWhatIfAdapter(ib)`
   bare (`runner.py:1276, 1386`) and `cli.py` never constructs a
   `PacedRequestBudget` (repo-wide grep: the only `budget` in `cli.py` is the
   proof's `OpeningOrderBudget`, `cli.py:1065`) `[verified]`. So today's
   runner path runs unbudgeted, and §6's table describes intent, not current
   behavior. Wiring one connection-scoped budget through runner, manager and
   scanner is the coordinator's step, since it touches `runner.py`/`cli.py`,
   which neither lane owns. Additionally, the quote adapter's
   `require_two_sided → EXITS_MANAGEMENT` heuristic conflicts with the
   audit's priority for binding revalidation — see §6 for the resolution.
4. **No `ScanBook`/`LogicalEntry` code exists yet** (§0) — every name marked
   `[provisional]` above must be reconciled against what the lanes actually
   ship, code outranking this document.

---

## 9 · Reconciliation with the landed lanes — AUTHORITATIVE

Both lanes landed while this contract was being drafted. Their code outranks
the provisional shapes above. Everything below is `[verified]` against the
files as read this session.

### 9.1 · The nomination handoff, as it actually shipped — and the seam gap

Lane A's record is `StructureNomination` + `NominatedLeg`, owned by
**`universe.py`** (`universe.py:198-291`), not by `universe_data.py` —
`universe_data.py` turned out to be the seed *symbol table*
(`universe_data.py:98-190`), a different thing. `NominatedLeg` carries only
`con_id, strike, right, action` — deliberately no symbol, expiration,
multiplier, exchange or trading class ("nothing an order needs... the
logical-entry stage can re-qualify", `universe.py:199-206`). Ranking evidence
rides on `ScanBookRow` (`iv_rank`, `iv_percentile`, `regime`, `rank_score`,
`rank_inputs`, `universe.py:323-328`), not on the nomination; the nomination
adds `short_delta` and `width` (`universe.py:256-257`).

Lane B's input is `EntryNomination` (`logical.py:268-302`) and it demands
**more**: `strategy_family` as a real `StrategyType`, legs as fully-qualified
`OptionLegIntent` tuples (`logical.py:296-298` — refuses anything else), and
a `reservation_amount`.

> **THE ONE REAL SEAM GAP AT MERGE: no landed code converts
> `StructureNomination` → `EntryNomination`.** Neither module imports the
> other (`universe.py:64-86` and `logical.py:101-113`, import lists read in
> full) — which is correct per §5 — but the bridge is unowned. The coordinator
> must build it: re-qualify the nominated con_ids through
> `ContractDataPort.qualify` (recovering multiplier/exchange/trading_class —
> the same fields `LogicalEntry.from_claim_record` round-trips,
> `logical.py:419-433`), and obtain `reservation_amount` from a what-if on a
> provisionally-built intent. Both are `CANDIDATE_CONSTRUCTION`-priority
> broker work per §6. This bridge is the claim path's first half and belongs
> in the runner-wiring step, not in either lane's module.

The §1 recommendation that the scanner must not construct
`OptionStrategyIntent` is **implemented and pinned**: the scanner constructs
none (`universe.py:8-14` states it; the import list confirms no
`OptionStrategyIntent` import — only `OptionRight`/`OrderAction` from domain,
`universe.py:66`), and Lane B's `_check_identity` enforces the stable-id rule
from §0 explicitly: the packet's intent id must **be** `logical_entry_id`, or
the manager refuses with the hint "build the OptionStrategyIntent with
strategy_id=logical_entry_id; a per-pass uuid4 orphans every awaited
approval" (`logical.py:981-992`).

### 9.2 · State ownership, as it actually shipped

Lane A shipped **pure transition functions**, not a writer object:
`transition` (`universe.py:419-439`), `claim_for_logical_entry`
(`universe.py:442-456`, takes `claimed_by: str`, stamps `claim_reference`),
`supersede` (`universe.py:459-461`) — each returning a *new frozen row*, with
the edge set enforced by `_ALLOWED_TRANSITIONS` (`universe.py:411-416`) and
undefined edges raising `ScanBookTransitionError` (`universe.py:429-434`).
The scanner itself never calls them (`universe.py:45-48`).

Two verdicts against §2:

- **§2's invariant holds, differently:** claimed-row re-candidacy is
  unrepresentable (no edge out of `CLAIMED_BY_LOGICAL_ENTRY` except
  `SUPERSEDED`), and double-claim of one *underlying* is prevented on Lane
  B's side — `claim` is idempotent per underlying via `active_for`
  (`logical.py:1038-1041`, pinned by `test_options_logical.py`'s duplicate-
  nomination tests).
- **§2's "a claimed row is terminal for the ScanBook" is OVERRULED:**
  `CLAIMED_BY_LOGICAL_ENTRY → SUPERSEDED` is a defined edge
  (`universe.py:415`) — a newer book may retire a claimed row. The
  `RecordingScanBookWriter` fixture has been aligned to this
  (`tests/integration_support.py`, see its docstring).

**Unowned at merge:** persistence of a claim. `ScanBook` is a frozen
whole-file JSON (`universe.py:550-600`, atomic `write` at 582-600) with no
"replace one row and save" helper. The coordinator's claim step is therefore:
read the session's book (`ScanBook.read`), apply `claim_for_logical_entry`
with `claimed_by=str(logical_entry_id)`, rebuild the book with the new row,
`write`. Ordering with Lane B's ledger: `manager.claim` persists
`ENTRY_CLAIMED` first (`logical.py:1056` — "persisted before any review
request", `logical.py:523-526`), then the book is rewritten; a crash between
the two leaves an ACTIVE entry and a still-CANDIDATE row, which the next
pass's `claim` resolves by returning the existing entry for that underlying
rather than minting a second (`logical.py:1039-1041`) — the conservative
side of the race.

### 9.3 · Runner wiring: the plan in §3 stands, with landed names

`runner.py` is untouched by either lane `[verified — git status]`, so §3's
insertion plan is live work, now concretely: at `runner.py:1192`, for each
`store.active()` entry (oldest first, `logical.py:889-893`) rebuild via
`build_vertical(..., strategy_id=entry.logical_entry_id)`, run the 3c block,
`packet_for`, then `manager.service(entry, packet, now=...)`
(`logical.py:1082-1172`). `service` owns the review lifecycle — filing,
waiting, digest-changed supersession (`logical.py:1138-1139`), refusal
cooldown (`logical.py:1231-1245`), expiry-with-reservation-release
(`logical.py:1121-1134`) — and **returns** the approval without consuming it
(`logical.py:82-86, 926-935`); on `APPROVED` the runner proceeds through the
unchanged `authorize_open` → `record_open_submitted` → `place_combo`
corridor, where the gate's digest-keyed idempotency makes the second
`require` find the same answer. `record_physical_attempt` /
`record_physical_outcome` (`logical.py:1249-1320`) bracket the transmit,
enforcing one working physical order per entry (`logical.py:1280-1291`).
The `--symbol` decision in §3 is unchanged.

### 9.4 · Reservations: fields landed, the fold did not

`LogicalEntry` carries `reservation_id` + `reservation_amount`
(`logical.py:328-329`), minted at claim (`logical.py:1053-1054`), and every
release trigger from §4 is implemented with a lineage record: expiry
(`logical.py:1125`), terminal refusal (`logical.py:1214-1219`), abandonment
(`logical.py:1332`), physical resolution — including fill, where "the real
position's `buying_power_reserved` takes over" (`logical.py:79-80,
1310-1316`). A terminal entry still holding a reservation is refused at
construction (`logical.py:362-369`) — the leak is unrepresentable.

**Still unbuilt:** the manager exposes no `reservations()` aggregation and
the runner's two snapshot-rebuild sites still fold only `store.exposures()`
(`runner.py:1287-1292, 1387-1398`). §4's fold is live work for the wiring
step, with one correction: the dedupe key is `logical_entry_id` (which **is**
the position store's `strategy_id` once submitted, by §9.1's identity rule)
— `reservation_id` is a distinct uuid (`logical.py:1053`) and must not be
used for dedupe.

### 9.5 · No-bypass: verified, with one missing guard and one new path

`universe.py` imports none of `transmit`/`approval`/`runner`/`reprice`/
`walk` (`universe.py:64-86`) and `logical.py` imports `approval` only for the
gate protocol and packet types — no `transmit`, no `runner`
(`logical.py:101-113`); the manager files and reads reviews but cannot mint
a `TransmitAuthorization`. New path #8 for the §5 table:
`cli.cmd_options_universe_scan` — read-only, builds the scanner's ports and
budget, writes the ScanBook, and journals; its own note says "no handoff was
filed and none can be from this path" `[verified in the cli diff]`.

> **Missing guard:** `universe.py:11` claims "``tests/test_options_universe.py``
> pins both facts against the AST" — **that file does not exist**; the tests
> directory holds only `test_options_universe_support.py` and
> `test_options_logical.py` `[verified by listing]`. The §5 source-level
> guard (AST + grep, with the D5 caveat) must be written at merge, or the
> docstring's claim corrected. This is the docstring-as-hypothesis failure
> mode (LEDGER.md M9/D1) in miniature: a stated guarantee with no enforcer.

One fragility worth fixing at merge rather than living with:
`service` classifies gate refusals by **substring match** on the error
message — `"answered REFUSED" in message` (`logical.py:1150-1153`) — against
prose composed in `approval.py:1188-1190`. It works today; a rewording of
that sentence silently reroutes refusals into the generic re-raise.
Recommendation: give the gate's refusal a typed attribute (the
`ApprovalDecision` it saw) and match on that.

### 9.6 · Pacing, as wired

The universe pass acquires exactly as §6 prescribed for discovery — GENERAL +
HISTORICAL at `DISCOVERY` per refresh (`universe.py:937-938`), GENERAL at
`DISCOVERY` for metadata and strikes (`universe.py:1096, 1168, 1176`) — with
one deliberate refinement §6's table missed: **phase-2 window quotes draw at
`CANDIDATE_CONSTRUCTION`**, on the stated ground that a symbol which earned
its phase-2 slot "queues behind management but ahead of broad discovery"
(`universe.py:1213-1219`). Accepted; it is strictly closer to the audit's
intent than flat-DISCOVERY was. `DiscoveryPaced` is caught at every acquire
site and becomes `DEFERRED_PACING` — a deferral, never a rejection, never a
crash (`universe.py:939-951, 1098-1104, 1180-1186, 1223-1229`), and
`DEFERRED_PACING` is excluded from the rejection counts on principle
(`universe.py:163-173`).

The CLI wires **one budget per connection** (`PacedRequestBudget(sleeper=
ib.sleep)` in `cmd_options_universe_scan`) and constructs the adapters
*without* their budget seams so scanner-owned acquires are not double-spent
`[verified in the cli diff]`. Divergence 3 in §8 narrows accordingly: the
budget now has one production wiring (the universe command); the **runner**
path remains unbudgeted, and the `require_two_sided → EXITS_MANAGEMENT`
heuristic conflict (§6) remains open for the wiring step.



1. **One authorization corridor** (§3, confirmed by §9.3): extract
   `runner.py:1355-1569` into a single shared function; the manager services
   the review lifecycle and hands back an unconsumed approval
   (`logical.py:82-86`), and the `--symbol` path, the manager path, and every
   future caller walk the same corridor. No second state machine can exist
   because the state machine's only door is shared.
2. **Stable `strategy_id` = `logical_entry_id`** (§0, enforced at
   `logical.py:981-992`): it is what makes a pending review completable
   across passes, what keys the reservation fold's dedupe (§9.4 — not
   `reservation_id`), and what ties ScanBook row → entry → position store
   record into one auditable identity.
3. **Scanner produces data, never intents** (§1/§5, shipped as
   `StructureNomination` in `universe.py:198-291` with no authorization-
   surface imports): the universe scanner is *structurally* incapable of
   opening risk — provided the missing AST/grep guard test named in §9.5 is
   actually written, since `universe.py:11` currently cites a test file that
   does not exist.
