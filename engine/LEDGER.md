# Options engine — working ledger

Baseline: **116 passed, exit 0** at `b585ddd`, clean tree, recorded 2026-07-29.
Branch: `feat/options-domain-model`.

Status labels follow the house rule: `[verified]` = read or ran it this session
after the last edit; `[inferred]` = derived, chain shown; `[assumed]` = unchecked.

---

## Confirmed bugs

| # | What | Evidence | Milestone |
|---|---|---|---|
| C1 | `Quote.market_data_type` is set from the *requested* constant, never the server's reported value, so the field cannot witness entitlement. | `broker.py:290,323` [verified] | M3 |
| C2 | `ib_async` never nulls the `-2.0` sentinel for theta/vega — `vega if vega != -2 else vega`, both branches identical. Finite, so the DBL_MAX screen at `broker.py:467-468` does not catch it. | `wrapper.py:1390-1391` [verified] | M4 |
| C3 | `Ticker.marketDataType` defaults to `1` and is written only by the server callback, so "no callback" is indistinguishable from "live". Fail-open. | `ticker.py:56`, `wrapper.py:889-892` [verified] | M3 |
| C4 | `Ticker` greeks fields are not reset in `__post_init__` and tickers are reused per contract, so stale greeks survive a subscription or data-type change. | `ticker.py:146-150`, `wrapper.py:406-411` [verified] | M4 |
| C5 | `modelGreeks is not None` does not imply `delta is not None` — the computation is assigned even when every field sanitizes away. | `wrapper.py:1383-1393` [verified] | M4 |

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

## Runtime capabilities still unverified

Everything below is unproven against a live broker and must not be described as working:

- Real-time option quotes and greeks (blocked on subscription).
- Whether delayed greeks populate at all (M5 probe).
- `engine trade --arm` has never transmitted an order, for equity or options.
- Combo submission, cancel, and replace.
- Reconciliation against real broker state after a restart.
