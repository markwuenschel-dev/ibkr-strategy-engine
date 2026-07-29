# Options engine — working ledger

Baseline: **116 passed, exit 0** at `b585ddd`, clean tree, recorded 2026-07-29.
Branch: `feat/options-domain-model`.

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

## Live broker results — 2026-07-29, paper DUR318607, pre-market (07:25 ET)

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

## Runtime capabilities still unverified

Everything below is unproven against a live broker and must not be described as working:

- Real-time option quotes and greeks (blocked on subscription).
- Whether delayed greeks populate at all (M5 probe).
- `engine trade --arm` has never transmitted an order, for equity or options.
- Combo submission, cancel, and replace.
- Reconciliation against real broker state after a restart.
