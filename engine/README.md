# ibkr-engine

Execution layer for an Interactive Brokers **paper** account. Connection, safety
interlocks, order placement, and a durable order journal.

This package builds infrastructure. It contains no trading strategy and offers
no view on what to trade.

## Paper only, by construction

There is no `--live` flag, no `allow_live=True`, and no environment variable that
switches venues. `engine/src/engine/config.py` accepts **only** paper ports:

| port | venue | |
|---|---|---|
| 7497 | TWS paper | accepted |
| 4002 | IB Gateway paper | accepted |
| 7496 | TWS **live** | refused at config load |
| 4001 | IB Gateway **live** | refused at config load |

Reaching a live account requires editing `PAPER_PORTS` in a reviewed diff. That
is deliberate: a config toggle is exactly the sort of thing that gets flipped
"just to test something" at 1am.

A second, independent gate covers the case the port cannot: you must set
`IBKR_ACCOUNT_ID`, and `Broker.connect()` refuses unless the broker confirms it
is serving exactly that account.

## Per-order gates

Applied cheapest-first, in `engine/src/engine/safety.py`. Each raises rather than
returning a boolean a caller could forget to check.

1. **Kill switch** — `engine halt "reason"` writes a `HALT` file; its existence
   stops every order. A file, not a signal, so it works when the process is
   wedged and can be engaged from a phone over SSH with `touch`.
2. **Arming** — dry-run is the default. `engine trade` without `--arm` runs every
   gate, prints what it *would* send, and exits non-zero.
3. **Symbol allowlist**, **quantity sanity**.
4. **Position cap** — applied to the *resulting* position, because ten 1-share
   orders reach the same place as one 10-share order.
5. **Notional cap** — a missing price is a refusal, not a pass.
6. **Daily order cap** — counted from the journal on disk, so a crash-looping
   engine cannot reset it by restarting.
7. **Margin impact** — from `whatIfOrder`, checked before anything transmits. An
   unknown margin impact is refused, not assumed negligible.

## Setup

```bash
cd engine
uv sync --extra dev
```

In TWS: **Configure → API → Settings** → tick *Enable ActiveX and Socket
Clients*, socket port **7497**, trusted IP `127.0.0.1`. Leave *Read-Only API*
ticked until you actually intend to place an order. Log in with your **paper**
credentials.

Then give it an account id. Either put it in the git-ignored `.env` at the
**repository root** (copy `.env.example`), which is the normal way:

```
IBKR_ACCOUNT_ID=DU1234567             # from TWS: Account > Account Window
```

or export it in the shell, which still wins over the file:

```bash
export IBKR_ACCOUNT_ID=DU1234567
```

`engine doctor` prints which `.env` it used, how many keys it took, and any line
it could not parse — never a value.

## Usage

```bash
uv run engine doctor                            # config + alerting, no connection
uv run engine status                            # account summary and positions
uv run engine quote SPY                         # a labelled price
uv run engine preview --symbol SPY --qty 1      # margin preview, places nothing
uv run engine trade --symbol SPY --qty 1        # dry run: refuses, not armed
uv run engine trade --symbol SPY --qty 1 --arm  # places one order

uv run engine halt "stepping away"              # kill switch on
uv run engine resume                            # kill switch off
uv run engine journal -n 20                     # the durable record
```

### Exit codes

`0` ok · `2` usage · `3` config · `4` refused by a safety gate · `5` connection ·
`6` halted · `7` journal unwritable. A supervising script can tell a refusal from
an outage without parsing text.

## Environment

Read from the process environment, which the root `.env` fills in first. A
variable already exported in the shell always beats the file — an explicit
export or a CI secret must not be shadowed by a stale file on disk.

| variable | default | meaning |
|---|---|---|
| `IBKR_ACCOUNT_ID` | *(required)* | account the broker must confirm it is serving |
| `IBKR_PORT` | `7497` | paper ports only |
| `IBKR_HOST` | `127.0.0.1` | |
| `IBKR_CLIENT_ID` | `17` | must be unique per connection |
| `IBKR_STATE_DIR` | `./.engine` | journal, kill switch, lock |
| `IBKR_MAX_ORDER_NOTIONAL` | `1000` | |
| `IBKR_MAX_POSITION_QTY` | `10` | |
| `IBKR_MAX_ORDERS_PER_SESSION` | `5` | per UTC day, counted from the journal |
| `IBKR_MAX_MARGIN_IMPACT` | `5000` | |
| `IBKR_SYMBOL_ALLOWLIST` | `SPY,AAPL,MSFT` | comma separated |
| `IBKR_PROJECT` | `ibkr` | tag on outbox alerts |
| `COLLAB_HOME` | *(kit dir)* | outbox root — the bridge must agree with this |
| `COLLAB_ENV_FILE` | *(search)* | pin one `.env` instead of searching upward |
| `KIT_DIR` | *(auto)* | where collab-kit lives, for phone alerts |

Caps are deliberately tiny. Raise them on purpose.

`KIT_DIR` is the one variable that cannot live in `.env`: it is what locates the
loader, so it has to be real. A plain checkout never needs it.

## Alerts

Fills and failures are written to `$COLLAB_HOME/outbox/` and forwarded by
collab-kit's `tools/telegram-bridge.py`. The engine does not know Telegram
exists — it writes a file. Alerting degrades to a warning if collab-kit is not
importable; it never blocks trading, because by the time an alert is sent the
fill has already happened.

## The order journal

`engine/src/engine/journal.py` deliberately inverts the trade-off made by
collab-kit's `EventLog`, which swallows `OSError` and continues. That is right
for handoffs and wrong for orders: an order the engine cannot record is a
position nobody knows about. So a failed write **raises and stops trading**,
every record is `fsync`-ed before the caller is told it was written, and the file
is **never rotated** — it grows forever.

## Tests

```bash
uv run pytest
```

No test opens a socket — `tests/conftest.py` replaces `socket.socket` for the
whole session so an accidental connection fails loudly rather than succeeding
against a running TWS. The broker is driven by `tests/fakes.py`.
