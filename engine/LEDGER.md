# Options engine — working ledger

Baseline: **578 passed, exit 0**, recorded 2026-07-29 after M6.
Branch: `feat/options-domain-model`.

> Superseded: this line read "**116 passed** at `b585ddd`" until M6. That figure
> predated four commits of options work (`f3dab2a`, `d80896c`, `2393178`,
> `e2f0322`) and was stale by 180 tests before M6 added any. Re-measured, not
> adjusted.

Status labels follow the house rule: `[verified]` = read or ran it this session
after the last edit; `[inferred]` = derived, chain shown; `[assumed]` = unchecked.

---

## Confirmed bugs

| # | What | Evidence | Status |
|---|---|---|---|
| C1 | `Quote.market_data_type` was set from the *requested* constant, never the server's reported value, so the field could not witness entitlement — a quote read "live" because live was asked for. | `broker.py:290,323` [verified] | **Fixed.** Split into `requested_market_data_type` / `reported_market_data_type`; `source` reports the provider's answer and says "unconfirmed" when there was none. `broker.py:83-108, 331-346`. Two regression tests. |
| C2 | `ib_async` never nulls the `-2.0` sentinel for theta/vega — `vega if vega != -2 else vega`, both branches identical. Finite, so the DBL_MAX screen at `broker.py:467-468` does not catch it. | `wrapper.py:1390-1391` [verified] | **Contained** at our boundary by `normalize_greek`, which also screens DBL_MAX on every field and extends the `-2.0` screen to gamma. Upstream package deliberately not patched. |
| C3 | `Ticker.marketDataType` defaults to `1` and is written only by the server callback, so "no callback" is indistinguishable from "live". Fail-open. | `ticker.py:56`, `wrapper.py:889-892` [verified] | **Contained.** `MarketDataProvenance` records `callback_received` separately; absent callback classifies `UNKNOWN` and `require_live_quote` refuses it. Mutation-checked. |
| C4 | `Ticker` greeks fields are not reset in `__post_init__` and tickers are reused per contract, so stale greeks survive a subscription or data-type change. | `ticker.py:146-150`, `wrapper.py:406-411` [verified] | **Contained.** Per-subscription generation UUIDs; `restart()` discards all observations, `current_greeks()` drops any stamped with a superseded generation. |
| C5 | `modelGreeks is not None` does not imply `delta is not None` — the computation is assigned even when every field sanitizes away. | `wrapper.py:1383-1393` [verified] | **Contained.** `OptionGreeks.has_valid_delta` is the eligibility signal; `require_uniform_live_provenance` refuses `DELTA_INVALID` separately from `GREEKS_MISSING`. |

"Contained" means the defect still exists upstream and our boundary neutralizes it.
None of these are proven against a live broker — see *Runtime capabilities still unverified*.

## Suspected bugs

| # | What | Why suspected | Next check |
|---|---|---|---|
| S2 | `broker.quote()` swallows `reqMarketDataType` failures in a bare `except Exception: pass`, so a refused data-type request looks identical to an accepted one. | `broker.py:293-294` [verified] | M3 — the provenance layer must not inherit this. |
| S3 | `conftest.py:40-53` blocks `socket.socket` only. Not `socket.create_connection`, not `socket.socketpair`, not a reference captured before the fixture ran. | Enumerated at `conftest.py:45-51` [verified] | Whether any options adapter path can reach a socket by another name. |

## Ruled out

| # | Lead | Why ruled out |
|---|---|---|
| R1 | "Options code exists somewhere in the repo." | Two independent greps over `engine/src` for option/combo/greek/strike/expiry terms returned zero real hits. The tree is 9 equity-only modules. [verified] |
| R2 | "The probe scripts are lost." | All six survive under a prior session's `%TEMP%` scratchpad. They are evidence of broker capability, not production code, and none will be moved into the tree. [verified] |
| R3 | S1 — "`gate_margin` is never called by a composite, so a caller could forget it." | By design, and documented as such at `safety.py:19-21`: it needs a broker round trip, so everything above it must pass first. Both production call sites exist (`cli.py:221`, `cli.py:322`). Not a bug. [verified] |
| R4 | "Changing safety types will break other packages." | Zero importers of `engine.safety` or `engine.broker` outside `engine/`. The one repo-root hit is a docstring at `tests/test_dotenv.py:115`. `engine/__init__.py` exports only `__version__`, so there is no facade to keep in sync. [verified] |

## Self-caught defects (found in this session's own work, before commit)

| # | What | How found |
|---|---|---|
| X1 | `_check_strike_ordering` would have rejected every valid closing order. Ordering is stated in the opening frame; after inversion the "long" put is the one that was short, so the check fired backwards. Now gated to `StrategyAction.OPEN`, with a regression test for closing a condor. | Code review before first run. |
| X2 | The module-level `maximum_loss_per_contract` function shared a name with the dataclass field. Resolution worked by scope, but read as a trap. Renamed `compute_maximum_loss_per_contract`. | Code review before first run. |

## Requirements discoveries

| # | Discovery | Consequence |
|---|---|---|
| D1 | IBKR: *"Options Greeks data is based on the underlying symbols. As such, a market data subscription for both the underlying and derivative are necessary for options greeks data."* | Provenance must be tracked **per contract**, not per session. M3 rejects mixed provenance across legs. |
| D2 | IBKR documents error `10090` "Part of requested market data is not subscribed" specifically for *"options subscriptions but not the underlying stock so the system cannot calculate the real time Greek values"*. | The diagnostic to assert on in M20's probe. |
| D3 | IBKR does not document error `10168` anywhere; `10186` carries the observed string. | Do not match on `10168`. Gate on the reported data type, not the code. |
| D4 | Delayed greeks are documented (tick 83, *"based on delayed stock and option prices"*) but IBKR also states it no longer offers delayed US equity quotes to IBLLC clients. | M5 probe resolves this empirically. Outcome changes development convenience only, never trading policy. |
| D5 | Paper market-data sharing is an opt-in toggle, not automatic inheritance. **User confirmed set to Yes on 2026-07-29.** | Removes one failure mode from M20 re-verification. |
| D6 | `maximum_loss` is ambiguous between per-contract and total; the governor's sizing formula divides by the per-contract figure. | Modelled as `maximum_loss_per_contract` with a derived `total_maximum_loss`. |
| D7 | The spec's `OptionStrategyIntent` has no field linking a CLOSE to the open strategy it retires, but the invariant requires one. | Added `closes_strategy_id`, required for CLOSE/ROLL and forbidden for OPEN. |

## Unrelated technical debt

| # | Item | Note |
|---|---|---|
| T1 | `engine/pyproject.toml` sets `addopts = "-q --strict-markers"`, so `pytest -q` resolves to `-qq` and silently suppresses the summary line. | Not touching it; it is load-bearing for CI output. Documented so nobody reads a blank result as a pass. |
| T4 | Restoring a mutated source file and immediately re-running pytest reused stale `__pycache__` bytecode and reported the mutant's failures against restored source. Cost one confused minute. Use `python -B` or clear `__pycache__` when mutation-testing. | Observed 2026-07-29. |
| T2 | `engine/README.md` documents an equities-only engine with no status or roadmap section. | Will need updating once options ship. Out of scope until the domain lands. |
| T3 | `Broker.preview`/`place` docstring at `broker.py:15-16` claims the engine requests type 4 outside hours; no code path ever does. | Stale comment. Fix opportunistically in M3. |

## Live broker results — 2026-07-29, the paper account, pre-market (07:25 ET)

> Account identifiers are deliberately not recorded here. This repository is
> public, and an account id in a committed file is permanent. It lives in
> `IBKR_ACCOUNT_ID` in the shell environment and nowhere else.

First options data ever taken from IBKR by repository-owned code.

**Verified working, no subscription needed:**

| What | Result |
|---|---|
| IV Rank | **26.02** from 251 real bars, 2025-07-29..2026-07-28. Independently reproduces the 28 Jul probe figure to 2 d.p. |
| Expiry selection | 2026-09-18 chosen, 51 DTE, from 35 expirations (2 in the 35–55 window) |
| Strike enumeration | 320 listed for that expiry; 25 qualified with real multipliers |
| `whatIfOrder` on a combo | **initial margin 500.00, maintenance 500.00** on a 5-wide spread — exactly width × multiplier. Defined risk recognised. |
| DBL_MAX screen | Fired in production: commission came back as the sentinel and normalized to `None` |

**Delayed greeks: available, but not while the market is shut.** Run 1 (07:0x ET)
returned four greek callbacks per contract and real, monotonic deltas —
`-0.1899 / -0.1939 / -0.1979 / -0.2020` across the 698/699/700/701 puts. Runs 2
and 3, minutes later, returned `greek_cbs=0` and no deltas. US options open at
09:30 ET. **Re-run after the open for the definitive answer** — one positive
result is enough to prove the capability exists, not enough to depend on.

**The underlying is genuinely blocked.** Error 10089 on SPY stock, and no
market-data-type callback at all (`reported=NONE`). Options report type 3.
So the two feeds are entitled differently, exactly as IBKR documents.

**Error codes, corrected from live observation:**

| Code | Text | Note |
|---|---|---|
| `10167` | "Requested market data is not subscribed. Displaying delayed market data." | The real code. **Not 10168**, which this ledger and three sessions of notes had wrong. |
| `10089` | "Requested market data requires additional subscription for API" | On the underlying. |
| `10091` | "Part of requested market data requires additional subscription for API" | Per option contract — the predicted have-options-not-underlying diagnostic, at 10091 rather than the documented 10090. |

## Confirmed bugs found by running it

| # | What | Fix |
|---|---|---|
| C6 | IBKR returns `bid=-1.0 ask=-1.0` for options with no quote, alongside valid greeks. Unscreened, a spread reads as tradeable at a negative mid. Not caught by the NaN or DBL_MAX screens. | `_price()` in `probe.py` screens negatives for prices only; greeks keep their own normalization, since a delta of -1.0 is a legitimate deep-ITM put. |
| C7 | `Quote.source` printed **"live"** during the scan while holding no price and having received no callback — the fail-open default (`Ticker.marketDataType = 1`) demonstrating itself in production. The M3 fix makes the label honest only where the server answers. | Equity path unchanged; this is why the options path must use `MarketDataProvenance.callback_received`, not the ticker field. |

## M6 — risk gates and the portfolio governor, 2026-07-29

**New modules** (all under `engine/src/engine/options/`): `policy.py` (validated,
env-driven thresholds), `portfolio.py` (`PortfolioSnapshot`, `PositionExposure`),
`ports.py` (five Protocols, no `ib_async`), `risk.py` (four candidate checks),
`governor.py` (six portfolio checks), `adapters.py` (the only new module that
imports `ib_async`).

**The entitlement gate now has production callers.** Before M6,
`require_uniform_live_provenance` and `require_live_quote` were reachable only
from `test_options_marketdata.py:291` and `:403` — the C3/C5 containment above
was real at the type level and absent at runtime. `risk.py` calls the gate as one
of four `REQUIRED_CHECKS`, and `CandidateRiskAssessment.__post_init__` refuses to
construct without all four, so approval-by-omission is not expressible.
`MarketDataSubscription` also gained its first production caller
(`adapters.py::IBKRLiveMarketDataAdapter`).

**`gate_notional` is not used for options.** It multiplies a share price by a
share count, which for a credit spread has no relationship to what the position
can lose. The equity path is untouched.

### Live probe re-run — resolves the open M5 question

`engine probe-options-data --symbol SPY`, exit 0, nothing transmitted:

| What | Result |
|---|---|
| Outcome | `DELAYED_GREEKS_AVAILABLE` |
| Option legs | `reported=3` (DELAYED), 17–21 greek callbacks each, real monotonic deltas −0.2085 / −0.2128 / −0.2172 / −0.2216 on the 698/699/700/701 puts, two-sided quotes |
| Underlying SPY | `reported=NONE`, `greek_cbs=0`, **no prices at all** — error 10089 |

So the M5 question "do delayed greeks populate at all" is **answered yes** — this
run returned 17–21 callbacks per contract, against 4 in the pre-market run and 0
in runs 2 and 3. Delayed greeks are usable for development.

**This changes nothing about tradeability, and the gate proves it twice over:**
the options report type 3, which is `Liveness.DELAYED` → `REALTIME_DATA_REQUIRED`;
and the underlying never sent a data-type callback at all, which is
`Liveness.UNKNOWN` → `NO_DATA_TYPE_CALLBACK`. Either alone refuses.

### Live `engine options-scan` with the adapters wired — the end-to-end proof

`cli.py::cmd_options_scan` now constructs `IBKRLiveMarketDataAdapter` and
`IBKRPortfolioStateAdapter` and passes them in. Against live TWS, exit 0, nothing
transmitted:

```
CANDIDATE RISK   REFUSED
  REFUSE  market_data_entitlement  [MARKET_DATA_TYPE_CALLBACK_MISSING] underlying SPY: the provider never reported a market-data type
  PASS    defined_loss  (350.00 of 20001.7216)
  PASS    broker_margin  (500.0 of 20001.7216)
  REFUSE  stress_loss  [OPTIONS_STRESS_REFERENCE_PRICE_MISSING] no usable underlying reference price (None)
PORTFOLIO GOVERNOR   APPROVED  (SPY)   net liq 1000086.08, BPR 0, 0 positions
TRADEABLE        NO
```

Three things this establishes that no unit test could. `IBKRPortfolioStateAdapter`
really reads `NetLiquidation` from `accountSummary` — the governor's caps are
computed against a **real** account figure, not a fixture. The broker-margin check
passes on a real `whatIfOrder` result. And the entitlement refusal is the true
one: **the underlying is what fails first**, with `MARKET_DATA_TYPE_CALLBACK_MISSING`
rather than `REALTIME_DATA_REQUIRED`, because SPY stock is entitled to nothing at
all and never answers — the C3 fail-open defect refusing correctly in production
for the first time.

Before this wiring the same command refused with `OPTIONS_NO_MARKET_DATA_SNAPSHOT`
for every check. Safe, but it proved only that the ports were absent.

### Self-caught in M6

| # | What | Fix |
|---|---|---|
| C8 | `policy.py::_seconds` handed a parsed `Decimal` straight to `timedelta`. `NaN` and `Infinity` are valid Decimals, so `IBKR_OPTIONS_QUOTE_MAXIMUM_AGE_SECONDS=NaN` raised `ValueError`/`OverflowError` — escaping the `ConfigError` contract a caller catches to exit 3. | Finiteness guard, plus a second guard on the float conversion: `Decimal("1e400")` **is** finite as a Decimal and only becomes `inf` as a float. The first guard alone did not catch it. |
| C9 | `portfolio.py`'s module docstring claimed the max(derived, reported) rule stopped unattributed buying power being "invisible to the concentration caps". It does not — only `total_buying_power_reserved` consumes the max; the three concentration checks iterate `positions` alone. | Docstring corrected to state the real boundary. The gap itself is genuine and open — see below. |
| C10 | The governor reported "the broker did not report what this structure would reserve" even when the broker had explicitly *rejected* the what-if. | `_bpr_unknown_detail()` distinguishes the three causes in prose; the code stays `CANDIDATE_BPR_UNKNOWN`, since the governor's decision is the same either way and `check_broker_margin` already separates them at code level. |
| C11 | `GovernorVerdict`/`CandidateRiskAssessment` read `.tzinfo` without an isinstance guard, so a string `evaluated_at` raised `AttributeError` rather than the `ValueError` every other invariant raises. | Guarded in both; `underlying` and `policy_version` now validated too. |
| C12 | **A coverage ratchet that did not ratchet.** `test_options_governor.py`'s "every refusal reason has a test" check matched enum members against *test method names*. An independent verifier refuted it by executing the case: inject a phantom member plus an empty `def test_phantom_member(self): pass`, and the assertion passes. A name proves someone thought about a code; only running a producer proves it is reachable. | Replaced with a `GOVERNOR_PRODUCERS` table mirroring `test_options_risk.py`: set-equality against the enum, plus a parametrized test that executes each producer against the real `PortfolioGovernor` and asserts the emitted code. Re-verified by injection — phantom member **plus** the empty test now yields 2 failures. |

## M7 — execution, exits, and the armed runner, 2026-07-29

**New modules**: `selection.py` (delta strike selection + max-loss sizing),
`lifecycle.py` (50%-profit close, 21-DTE exit/roll), `positions.py` (event-sourced
position store), `transmit.py` (the chokepoint), `runner.py` (one strategy pass).
**New commands**: `engine options-run [--arm]`, `engine options-positions`.

### The safety property inverted, deliberately

Until M7, `engine.options` provably could not transmit — zero `placeOrder` in the
package, enforced by an AST test. That is gone. What replaces it:

> **Exactly one function transmits, and it cannot be called without a token that
> only exists if every gate passed.**

`place_combo` takes a `TransmitAuthorization` as a **required, defaultless**
keyword argument. The token's `__post_init__` refuses any instance not built with
a module-private sentinel, and only `authorize_open` / `authorize_close` hold it.
"Forgot to check the gates" is therefore a `TypeError` at the call site, not a
latent bug. `test_options_transmit.py` pins all of it, including that the single
`placeOrder` is inside `place_combo` and that a forged key is rejected.

**Closes are authorized differently, on purpose.** `authorize_close` does not
consult the governor and is exempt from the daily order cap. Refusing to close
because the book is concentrated is backwards — closing is what reduces
concentration — and a cap that can stop you exiting is not a safety feature. The
kill switch still blocks both.

### Live proof, armed

`engine options-run --symbol SPY --market-data-type 3 --min-iv-rank 0 --arm`
against live TWS, exit 0:

```
ENTRY   OPEN 1x PUT_CREDIT_SPREAD SPY @ 0.98 CREDIT [max loss 402.00]
        SELL 1x SPY 2026-09-18 712.0 P | BUY 1x SPY 2026-09-18 707.0 P
CANDIDATE RISK   REFUSED
  REFUSE  market_data_entitlement [MARKET_DATA_TYPE_CALLBACK_MISSING]
  PASS    defined_loss   (402.00 of 20001.7216)
  PASS    broker_margin  (500.0 of 20001.7216)
  REFUSE  stress_loss    [OPTIONS_STRESS_REFERENCE_PRICE_MISSING]
PORTFOLIO GOVERNOR   APPROVED  (all six, net liq 1000086.08)
TRANSMITTED  0 order(s)      ENTERED  NO
```

Everything downstream of market data now works on real IBKR data: **strikes were
delta-selected** (712/707 from delayed greeks), sized against the risk budget,
priced from the book at 0.98 rather than a fraction of the width, margined by a
real `whatIfOrder`, and graded by a governor using a real net-liquidation figure.
The two refusals are both the missing underlying subscription.

`--arm` was passed and **nothing transmitted**, and `positions.jsonl` has zero
lines — the refusal happened before the record-before-transmit step. That is the
interlock demonstrated in production rather than only in tests.

### Self-caught in M7

| # | What | Fix |
|---|---|---|
| C13 | `runner._manage_one` called `closing_intent_for` with no `limit_price`. Only the profit-target rule computes one, so **every 21-DTE exit would have raised** instead of closing. Caught by the lifecycle lane's interface report before it ever ran. | The DTE exit is priced from the current mark; with no mark it refuses loudly rather than sending an unpriced combo. A market order on a spread nobody can price is how a defensive exit becomes the worst fill of the day. |
| C14 | `OpenPosition.to_record()` and `from_record()` were not inverses — the reload read an `entry_credit` key only `record_open_submitted` injected. Any other writer's record raised `KeyError` inside the replay, where it was swallowed: **the position silently vanished from the book.** | `to_record` now emits it; a round-trip test pins the inverse property. |
| C21 | **A defensive exit after a partial fill sold contracts that were never bought.** `_manage_one` called `closing_intent_for` without `quantity=`, so it inherited `OptionStrategyIntent.closing_intent`'s default of the full order size. A position with `quantity=3, filled_quantity=1` produced a closing order for **3** at 21 DTE — a defensive exit opening a naked short. `OpenPosition.manageable_quantity` was written for exactly this and its own docstring warns of exactly this; it simply was not called. Found by a recovery lane, reproduced directly. | `quantity=position.manageable_quantity`. The test that documented the defect now asserts 1 and cross-checks it against both `manageable_quantity` and `quantity`. |
| C22 | **A reconnect blocked all new entries, permanently.** `_reconcile_orders` differenced the `permId` and `orderId` sets independently, so a broker order matched on its durable `permId` was *also* reported as unknown by its reassigned `orderId`. Since IBKR reassigns `orderId` on reconnect — the precise case `permId` exists to survive — any reconnect produced a phantom disagreement that made `agrees` False forever. | Broker orders are kept as `(order_id, perm_id)` pairs; an entry matched by **either** identifier is known. Unknown ones are reported once, by the durable id, so the count equals the number of orders rather than the number of identifiers. |
| C16 | **A successful credit fill was recorded as a failure.** `build_combo` submits a net credit as a `BUY` at a *negative* limit, so IBKR reports the fill at a negative average price — and `transmit._decimal_or_none` screened negatives, returning `None`. The runner reads that as "did not fill" and writes `OPEN_FAILED`. Net effect: **a spread live in the market, recorded as never opened** — exactly the unrecorded position the whole store exists to prevent, arriving through the one path nobody thought to check the sign on. Found by *running* the armed path end to end against a fake broker, not by any test. | Renamed to `_fill_price`, negatives allowed, zero still rejected (a zero fill is an unpopulated field, not a price). The runner already took `abs()` when storing the credit; the close path now does too, so the sign convention stays inside the broker boundary. |
| C15 | **One malformed line made the entire book unreadable.** The replay's `try/except` wrapped only the `OPEN_SUBMITTED` branch; a `CLOSE_FILLED` with a naive timestamp raised straight out of `positions()`. An engine that cannot read its book cannot manage what it already holds — worse than any single lost event. | Every branch guarded. A bad line now costs one transition, is **recorded** in `integrity_errors()`, and makes `ReconciliationReport.agrees` False — which stops the runner opening new risk against a partly-understood book while still allowing exits. Degraded, loud, and still able to get out. |

## M8 — the entitlement lifted, and three determinism defects it exposed, 2026-07-30

### The blocker is gone

`engine probe-options-data --symbol SPY`, 09:47 ET, market open:

| What | 2026-07-29 | 2026-07-30 |
|---|---|---|
| SPY stock | `reported=NONE`, no prices, error `10089` | **`reported=1` (LIVE)**, bid 737.08 / ask 737.10 |
| Option legs | `reported=3` (DELAYED) | **`reported=1` (LIVE)**, 10–13 greek callbacks each |
| Errors | `10089`, `10091`, `10167` | none — only `2104` market data farm OK |

The probe **requested** type 3 and the server **returned** type 1, which is the
signature of an entitlement upgrade rather than a code change. Every candidate
this engine ever built refused on this one input, so the transmit path is
reachable for the first time. This supersedes the *"The gate that is still
standing"* reading throughout this file and in the state-of-play artifact.

### Three defects that only running it could find

The unit suite was green throughout. Two identical `options-run` invocations
minutes apart produced two *different* failures — the tell that these were
races, not thresholds.

| # | What | Root cause | Fix |
|---|---|---|---|
| C17 | **The market-data wait was a bet, not a wait.** `adapters.py` fired 26 concurrent subscriptions then slept a flat 6.0s and harvested once — 0.24s of dwell per contract against the probe's 3.0s. Whichever subset of model computations had landed by T+6 became the chain, so strike selection was a race and the same command produced a different answer each run. | `adapters.py:278` (`ib.sleep` is an unconditional `asyncio.sleep`; no deadline loop, no greek check, no retry) | Bounded wait on the actual condition — poll the recorder every 0.25s until every leg has greeks, ceiling raised 6→20s. A healthy run now finishes **faster** (early exit) and a slow one keeps waiting. Plus: underlying subscribed first with a 2s head start (IBKR computes greeks *from* the underlying price), `genericTickList` "106"→"" (106 is implied volatility; `modelGreeks` arrives on a bare request — the probe's proven shape), and a `ticker.modelGreeks` fallback when the reqId mapping misses. |
| C18 | **The strike window was centred on the middle of the ladder, not on spot.** `narrow_strikes(reference_price=None)` takes the *positional* median of the listed strikes. Those coincide with spot only by accident. `scan.py:347-349` passes a real price; the runner was the only caller in the repo passing `None`. | `runner.py:473` | The runner now reads spot via `broker.quote()` and passes it, recording an explicit error when no price is obtainable rather than silently degrading. |
| C19 | **A symmetric window cannot reach a one-sided structure's wing.** Even centred on spot, a width-24 window spans ±12 — so on a 1-point ladder the 0.30-delta short strike landed on the window *floor* with no protective strike beneath it. Half the budget was spent on call-side strikes a put spread never selects. | `chain.py:170-171` | `narrow_strikes` takes an optional `right` and shapes the window one-sided: `width` below spot, a `width//8` cushion above. The symmetric default is unchanged, so the shadow scan is untouched. |

### Self-caught while fixing the above

| # | What | Fix |
|---|---|---|
| C23 | **One refusal message covered two unrelated market conditions.** `select_vertical` returns `None` both when no strike carries a usable delta *and* when a short was found but no wing is within reach — and the runner printed the same sentence for both. That is the difference between "the feed is broken" and "this expiry cannot build the structure", and the soak's acceptance condition asks for a *specific* refusal. | `runner.py:544-548`. The runner now re-runs `select_short_strike` — pure and cheap — and names which branch fired, including the short strike it settled on. |
| C20 | **The protective leg had no upper width bound.** `_select_protective_strike` scores by `abs(width - target_width)` and takes the nearest — "nearest" among whatever is listed. On the sparse weekly ladder observed live (`672`, then `722..750`) a 722 short had exactly one strike beneath it: **50 wide against a target of 5**. Ten times the intended risk. Two downstream gates would have refused it (defined-loss cap; no two-sided market on a strike nobody quotes) — but neither names the cause. | `selection.py:482-484`. Bounded to `target_width` + one **median** strike increment measured from the chain itself. A ratio would be wrong both ways: a coarse ladder listing strikes every 5 legitimately cannot beat 5 against a 2-wide target, while a 1-point ladder has no excuse for 50 against 5. One increment past target is precisely the worst a *complete* ladder can do. |

**All four properties are mutation-verified**, not merely tested: removing the
bounded wait fails 2 tests, flattening the window to symmetric fails 2, deleting
the width bound fails 2, and subscribing the underlying last fails 1. New file
`tests/test_options_adapters.py`, 16 tests. Full suite green, exit 0.

### The determinism soak — the acceptance gate for the above

**20 consecutive live unarmed passes, frozen tree, market open: PASS.** [verified]

```
distinct outcomes across 20 passes: 1
   20x  no protective strike within reach of the # short: the chain lists
        nothing between it and # away
distinct widths seen: none (no candidate built)
passes NOT confirming 0 transmitted: 0
```

The acceptance condition is **stability, not a verdict**: a refusal repeated
20/20 is the engine being deterministic, whereas the same command splitting
across two outcomes is the race C17 existed to kill. Before the fixes, two
consecutive passes produced two *different* failures.

A methodology note worth keeping: the first soak attempt was contaminated — a
source edit landed at pass 19 and the outcome changed under it. A soak measures
a *frozen* tree or it measures nothing. It was re-run clean.

### The next thing in the way — and it is not a defect

The single refusal is market-driven, and the exact text names it: [verified]

```
blocked by  no protective strike within reach of the 722.0 short: the chain
            lists nothing between it and 5 away
```

`select_expiration` chose **2026-09-11 at 43 DTE** over 2026-09-18 at 50 DTE,
because it optimises `abs(dte - 45)` alone. 09-11 is a *weekly*: its ladder is
`672` then `722..750`, 30 strikes. At spot ~738 the 0.30-delta put sits below
722, which that chain does not list — so the short pins to the band floor at
**722** and the only strike beneath it is the 50-wide `672` outlier, which C20
now refuses. The short is found; the wing is not. That is a chain that cannot
build this structure, stated precisely.

| # | Gap | Status |
|---|---|---|
| G9 | **Expiry selection cannot see whether the chain it picked can build the structure.** It ranks by DTE proximity only, so a thin weekly beats a rich monthly by two days and then cannot supply a wing. The fix is a fallback — try the next expiry in the window when the chosen one yields no valid structure — which preserves the DTE-proximity rule rather than replacing it with a monthly preference. | Open. Not a strategy change; the strategy still wants ~45 DTE. |

> Numbered **G9**, not G8: G8 is already in use for callback-driven persistence,
> which is not in the committed gap list (it stops at G7) and was easy to
> collide with. See below — G8 turned out to be already closed.

### G8 was already closed, and looking for it found a real defect on the other side

A lane dispatched to "continue G8" reported back that it was built at `a373063`
and the brief describing it as open was wrong. Verified directly rather than
taken on the lane's word: `transmit.py:380` emits to the sink **before** the poll
loop and `:393` on **every** iteration, and `runner.py:633` constructs the
`LifecycleRecorder` and passes it into both the exit path (`:330`) and the entry
path (`:790`). Opening-side identity and partial fills already reach disk as
observed, not from a final snapshot.

| # | What | Fix |
|---|---|---|
| C24 | **The closing side was the broken one.** `record_partial_fill(closing=True)` wrote the fill quantity to the journal (`positions.py:659`) and the replay **discarded** it — `CLOSE_PARTIAL` set only the state, because `OpenPosition` had no field to hold it. A cancelled-after-partial exit reloaded as "closing, amount unknown": the contracts that got out and the ones still held were indistinguishable on disk. The mirror of C21, on the retiring side. | `close_filled_quantity` field with invariants bounded by the *ordered* quantity rather than `filled_quantity`, so a `CLOSE_PARTIAL` replayed before an `OPEN_FILLED` cannot raise inside `_replace` and get silently dropped. Also `sink.py:160-173`: `_seed` seeded only the opening order, so a restart mid-close came back believing nothing had filled on the exit. |

**This could not have re-sent an oversized exit** — `lifecycle.py:337-348` holds
any position that is not `OPEN`, so a `CLOSING` position is never re-decided. The
consequence was lost information, not a duplicate order. Worth stating precisely,
because "the store forgot how much got out" and "the engine sold it twice" are
very different findings and the first should not be reported as the second.

Landed on branch `lane4-callback-persistence`, **not yet merged**. Suite there:
1166 passed, exit 0 (against 1153 at `a373063`). `transmit.py` untouched, single
`placeOrder` call site unchanged, AST guard green.

## M9 — the adversarial shield, 2026-07-30. **Armed execution is blocked.**

An independent lane was told to *disprove* the safety claims rather than confirm
them, and to execute every attack. It broke six of them. The headline finding
invalidates the property this whole design rests on.

### D1 — a *genuine* authorization transmitted an arbitrarily larger order. **FIXED**

No forgery involved. `place_combo` bound only `strategy_id` and `action`
(`transmit.py:330,336`) — and an id and an action are shared by every variant of
a structure. Executed: mint a real token for a 1-lot 5-wide spread, hand
`place_combo` a 50-lot 100-wide spread carrying the same id. Both checks pass.

```
AUTHORIZED   : qty=1   strikes 500/495   max loss $350
TRANSMITTED  : qty=50  strikes 500/400   max loss $492,500      (1407x)
```

`place_combo`'s own docstring claimed that check prevented exactly this — *"an
approval for a 1-lot could transmit a 10-lot."* It did not. The docstring was
the load-bearing lie: it described a guarantee nobody had implemented, and three
sessions of notes repeated it.

**Fixed** in `b3504fb`: the token carries a sha256 `structure_digest` over
everything that moves the maximum loss — quantity, each leg's contract/side/
ratio/multiplier, limit price and direction. `authorize_*` compute it from the
intent they approved; `place_combo` recomputes from the intent it is about to
send. Mutation-verified: disabling the comparison fails all three attack tests,
and a control test proves the order it *was* minted for still transmits.

### D11 — `maximum_width` was unvalidated. **FIXED**

Mine, introduced in `8d90529`. `target_width` is validated; the bound I added
beside it was not. `Decimal("Infinity")` is an ordinary Decimal that reads as a
limit and enforces nothing — strictly worse than no bound — and `NaN` turned the
comparison into an uncaught `InvalidOperation` inside the selector. Fixed in
`b3504fb` on the same terms as `target_width`, plus a contradiction check.

### Still open — each one blocks the first armed order

| # | What | Why it blocks |
|---|---|---|
| D2 | **Absence of a reconciler reads as permission.** `runner.py:641` swallows a reconciliation exception into `report.errors` and leaves `report.reconciliation is None`; the gate at `:679` only blocks when it is *not* None and disagrees. Executed: restart with `broker.positions()` raising → one `placeOrder`, book holds two strategies for the identical spread. | Duplicate send after restart. |
| D5 | **The single-`placeOrder` AST proof has two holes.** It inspects only `Call.func` and globs non-recursively. A subpackage `options/execution_ext/sender.py` containing a literal `ib.placeOrder(...)` left the suite green; so did `sender = ib.placeOrder; sender(c,o)` and `getattr(ib, "plac"+"eOrder")` inside `positions.py`. | The chokepoint is the whole safety story and it is not actually enforced. |
| D6 | **The token is forgeable** by `object.__new__`, subclass overriding `__post_init__`, `dataclasses.replace`, and pickle round-trip (which never re-runs `__post_init__`). | Weaker threat model — all require in-process code — but the stated property is "cannot be constructed", and that is false. |
| D4 | **The C21 guard is load-bearing and untested.** Disabling `lifecycle.py:505` leaves the full suite green. The test that looks like coverage uses ordered=1/close=5, so `domain.py:608` catches it first — it passes for the wrong reason and never exercises close-more-than-*filled*. | A naked short is the failure mode. |
| D7 | Three more guards whose removal breaks nothing: `transmit.py:97` (armed-only), `transmit.py:111-118` (risk/governor re-check), `marketdata.py:564` (cross-leg uniform-LIVE). | The C12 failure mode, three more times. |
| D8 | Foreign or corrupt events silently size exits to the **ordered** quantity, and `integrity_errors()` stays empty so the reconciler never engages. | |
| D9 | `has_valid_delta` is `delta is not None` and nothing else. Deltas of `-5.0`, `12345`, `0` and `NaN` were all **approved**; `NaN` then crashes `selection.py:309` with an uncaught `InvalidOperation`. | Fail-closed by crash, not by refusal. |
| D10 | A future-dated quote (`age = -3600s`) bypasses the staleness bound. | |
| D3 | Second exit after a partial close is a naked short. **Already fixed** on `lane4-callback-persistence` (C24), unmerged. | |

**Survived**, and worth recording as genuinely holding: the width bound (nine
chain shapes, including the real 2026-07-30 incident chain); the provenance gate
on all six named conditions, with a control proving the checks are not vacuous;
C21 partial-fill exit sizing; and three forgery attempts stopped by domain
invariants before reaching the target.

## M10 — the first order this engine has ever sent, 2026-07-30 15:16 UTC

`engine options-execution-proof --symbol SPY --dte 50 --arm`, exit 0.

```
orderId 896   permId 1151642162   orderRef 13e95292-dbbd-4846-94ab-dcdddaa5f77e
BAG SPY  BUY 1  LMT -0.20 (net credit 0.20)  TIF=DAY
SELL 1x SPY 2026-09-18 713.0 P | BUY 1x SPY 2026-09-18 712.0 P   max loss $80
PreSubmitted -> Submitted,  filled 0.0,  remaining 1.0
```

**G7 is closed on acceptance, open on fill.** `place_combo` reached IBKR, IBKR
accepted the combo, and the identifiers came back. What remains unproven is
everything downstream of a fill: partial fills, commissions, the close path.

**Persistence worked as designed.** `OPEN_SUBMITTED` was written *before* the
send, then `OPEN_ACKNOWLEDGED order=896`, then again with `perm=1151642162` as
the permId arrived. Identity reached disk from live observation, not from a
final snapshot.

**The restart gate held, and D2 is why.** A fresh process replayed from disk and
refused a second entry with `RUNNER_RECONCILIATION_DISAGREEMENT`, transmitting
nothing. Worth recording precisely: the session budget printed `opening orders
0 of 1 used` — it is per-process and **reset on restart**. The only thing that
stopped a duplicate was the reconciliation outcome gate merged forty minutes
earlier. Without D2 this restart is the duplicate send D2 was written to
describe.

### C25 — the reconciler asserts something it cannot know

The run reported:

> `ORDERS ABSENT  ... transmitted, and the broker is not working them; either
> they filled unobserved or were never accepted`

**False.** A direct read-only query returned `status=Submitted, remaining=1.0` —
the broker *was* working it. `run_once` asks only `broker.positions()`, and a
working unfilled order is not a position, so it is invisible to reconciliation
and then described with a claim the code has no evidence for. The refusal was
conservative and correct; the stated reason was wrong. Being blocked for a false
reason is its own defect: it trains the operator to distrust the message.

### C26 — a mid-price limit on a 1-wide spread does not fill

The order priced at the mid (short mid − long mid = 0.20) and sat working,
unfilled, for over three hours of liquid regular-session trading. This is not a
plumbing failure — it is the pricing rule meeting the real book. `_build_candidate`
prices from the mid, which on a 1-wide SPY vertical is frequently outside where
the spread actually trades.

**Consequence for the multi-position target:** the bottleneck on gathering fill,
partial-fill and commission evidence is *pricing*, not concurrency. Three
concurrent mid-priced orders produce three working orders and no fills. Cancel /
replace with a bounded walk toward the natural price is what converts a working
order into evidence.

| # | Gap | Status |
|---|---|---|
| G10 | No `cancelOrder` anywhere in `engine/src` — a working order cannot be pulled or repriced programmatically, only by hand in TWS. | Open; lane in flight. |
| G11 | Reconciliation never queries open orders, so a working order is invisible (C25). | Open; lane in flight. |
| G12 | Entry pricing is the mid with no walk toward the natural (C26). | Open. |

## Runtime capabilities still unverified

Everything below is unproven against a live broker and must not be described as working:

- Real-time option quotes and greeks (**blocked on subscription — reconfirmed
  2026-07-29**: options are type 3, the underlying is entitled to nothing).
- `engine trade --arm` has never transmitted an order, for equity or options.
- Combo submission, cancel, and replace.
- Reconciliation against real broker state after a restart.
- Every adapter in `adapters.py` except the what-if path. `IBKRLiveMarketDataAdapter`
  and `IBKRPortfolioStateAdapter` are unit-tested against fakes only and have
  never run against TWS.

## Open gaps this milestone did not close

| # | Gap | Why it is open |
|---|---|---|
| ~~G1~~ | ~~Sector and correlation caps count only the candidate.~~ **CLOSED in M7.** `PositionStore.exposures()` supplies per-position attribution, and `runner.run_once` injects it into the snapshot before the governor evaluates, so the three concentration buckets now aggregate the engine's own open structures. Unattributed broker BPR still reaches only the total check — an inherent limit, since a reported total carries no attribution to invent one from. | Closed. |
| G2 | The stress model is a **terminal** payoff. It bounds what a position can settle for and needs no volatility surface, but says nothing about pre-expiry mark-to-market, so it understates a gamma event with 40 days left. | A pre-expiry model needs a live greeks feed, which is the thing that is blocked. |
| G3 | `options-scan`'s `report.tradeable` still cannot be `True`, because the **shadow scan** remains offset-selected. That is now a legacy path: `options-run` is the real entry point and does delta-select. | Superseded rather than fixed. `options-scan` is kept as the non-transmitting diagnostic it was built to be. |
| ~~G4~~ | ~~delta strike selection, max-loss sizing, 50%-profit close, 21-DTE exit/roll~~ **all CLOSED in M7** (`selection.py`, `lifecycle.py`). | Closed. |
| G5 | **No scheduler.** `run_once` is a single pass with an explicit `now`; nothing calls it repeatedly. Deliberate — a pass that returns a report can be driven by cron, by hand, or by a future loop, and none of those need each other. | Open. The unit of work exists; the driver does not. |
| G6 | **A roll only closes.** `ManagementAction.ROLL` sends the close half; the follow-on open re-enters through the ordinary entry path on a later pass, so it is gated like any other entry. Nothing yet writes the `ROLLED` link record joining the two. | Open. The link record has a store event (`PositionEvent.ROLLED`) and no writer. |
| G7 | **Nothing has ever filled.** Every transmit-path test uses a fake. `place_combo` has never sent an order to IBKR, so partial fills, rejections, and the `Trade.isDone()` polling loop are unproven against the real API. | Open, and blocked on the same market-data entitlement — the entitlement gate refuses before the transmit path is reached. |
