# Arena — Complete Operator Guide

**Version 3.0.5** · Install → first run → paper → sandbox → live, with every
feature explained and every command documented.

This is the single end-to-end guide. It assumes you have never run Arena
before. Every command in it was executed and verified before being written
down — nothing here is aspirational.

Deeper references, once you are past this document:

| Document | Covers |
|---|---|
| [README.md](README.md) | Full release history, architecture, data contracts |
| [LIVE_TRADING_ENABLEMENT.md](LIVE_TRADING_ENABLEMENT.md) | Real-money runbook + the repository audit behind it |
| [LIVE_OPERATIONS.md](LIVE_OPERATIONS.md) | The live safety rails in depth |
| [USER_MANUAL.md](USER_MANUAL.md) | Feature-by-feature usage manual |
| [FULL_TROUBLESHOOTING_GUIDE.md](FULL_TROUBLESHOOTING_GUIDE.md) | When something breaks |
| [ERROR_DIAGNOSTIC_PLAYBOOK.md](ERROR_DIAGNOSTIC_PLAYBOOK.md) | Error-by-error diagnosis |
| [UPSTOX_SANDBOX_AND_MARKETDATA_GUIDE.md](UPSTOX_SANDBOX_AND_MARKETDATA_GUIDE.md) | Upstox credentials specifically |

---

## 0. The straight answer: can I trade right now?

**Paper trading: yes, today, in about five minutes.** No accounts, no
credentials, no network. Section 4.

**Sandbox trading: yes, once you have free Upstox sandbox credentials.**
Sandbox places real HTTP orders against Upstox's sandbox host, with real
market data, and no real money. Section 11.3.

**Live trading with real money: not yet — and the blockers are on your side,
not in the code.** The trading code is complete and tested. Three things must
happen first, and you control all three:

| # | Blocker | Why | Start it first? |
|---|---|---|---|
| 1 | **A static IP registered with Upstox** | SEBI/NSE mandate since 2025-10-01: order APIs require a registered static IP (one primary + one backup, changeable once a week, registered on your Upstox Apps page) | **Yes — this has a waiting period** |
| 2 | **A live Upstox app + access token** | A live app gives you the API key/secret; the access token expires **daily** and must be refreshed with `python -m app upstox-login` | Yes, but it's same-day |
| 3 | **The live `.env` block** | Four switches + four non-zero risk limits + a dedicated database. Takes ten minutes once 1 and 2 exist | No — do it last |

The static IP is the long pole. Everything else can be done the same day.

> **No real order has ever been placed from this environment.** The live rail
> has been exercised end-to-end against *fake* credentials in an isolated
> rehearsal directory — every gate, preflight check, drill, watchdog and lock
> fired correctly — but nothing ever reached a real broker.

---

## 1. What Arena is

Arena is a modular Python research and trading platform for Indian equities
via Upstox. It is three things in one program:

1. **A research platform** — import or download historical candles, run
   backtests, tournaments, walk-forward validation and robustness testing,
   and score strategies against each other.
2. **A paper-trading system** — the same strategies, the same risk layer,
   the same execution path, but orders are simulated. Run it against
   recorded data or against the live market feed.
3. **A real-money trading system** (gated, off by default) — the identical
   pipeline with a live broker, a canary ramp, hard risk limits, an
   emergency stop, and an audit trail.

The point of that architecture: **the thing you test is the thing you trade.**
Paper and live differ only in which broker object is plugged in at the end.

### The pipeline

```
market data ──► candles ──► strategies ──► strategy router ──► risk layer ──► broker
   (feed)        (build)      (signals)      (which one?)      (hard limits)   (paper/
                                                                                sandbox/live)
                                                    │
                                                    └──► database (audit trail)
                                                              │
                                                              └──► dashboard / API / reports
```

Every arrow is a real module you can inspect. Nothing is hidden in a monolith.

---

## 2. What Arena will not do

These are deliberate design decisions, not missing features. Knowing them
saves you from expecting behaviour that does not exist:

- **It will not enable live trading with one variable.** Four independent
  switches must agree *and* four risk limits must be explicitly non-zero. If
  any one is missing, startup aborts and names what is missing.
- **`0` never means "unlimited" in live mode.** A limit of 0 is refused
  outright, so a runaway loop cannot be unbounded.
- **The emergency stop is not disabled by configuration.** `live-halt`
  always works, even when the rest of the configuration is invalid.
- **Live resume refuses to run when live is not armed.** You cannot clear a
  stop into a live session that was never enabled.
- **The engine will not start in paper mode.** `python -m app engine`
  requires `BROKER=UPSTOX_SANDBOX` or `UPSTOX_LIVE`. Local paper trading uses
  a different command (`paper-live`) so the two can never be confused.
- **The sandbox broker refuses the live order host.** `api-hft.upstox.com` is
  Upstox's *live* order endpoint; the sandbox broker rejects it by name.
- **The desktop GUI cannot trade.** It is read-only plus the emergency stop.
- **The dashboard cannot place orders.** Only the STOP flag is writable.

---

## 3. Install

### Prerequisites

- **Python 3.11 or newer** (check: `python3 --version`)
- ~200 MB of disk for the virtualenv
- Linux, macOS, or Windows. Android/Termux also works (see 3.5)

### 3.1 Linux / macOS

```bash
cd /path/to/AXIOM

python3 --version              # must print 3.11 or higher
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

cp .env.example .env           # your private config; gitignored
```

Always run Arena commands with the virtualenv active, or call
`.venv/bin/python` explicitly. Mixing with system Python is the most common
source of "module not found" confusion.

### 3.2 Windows (PowerShell)

```powershell
cd C:\path\to\AXIOM

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

Copy-Item .env.example .env
```

If script execution is blocked, call the interpreter directly instead of
activating: `.\.venv\Scripts\python.exe -m app paper-test`.

### 3.3 Optional: desktop application

```bash
python -m pip install -r requirements-desktop.txt   # PySide6
```

### 3.4 Optional: research extras

```bash
python -m pip install -r requirements-kronos.txt
```

### 3.5 Android / Termux

```bash
pkg install python
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

The deterministic core (backtest, tournament, validation, paper replay)
touches only the standard library plus a few pure-Python packages, so it runs
on a phone. See README §10 for the full Termux notes.

### 3.6 Verify the install

This is the fastest possible proof that everything works. It needs no
credentials, no network, and no configuration:

```bash
python -m app paper-test
```

You are looking for `"ok": true` at the end of the JSON. It builds a
deterministic paper pipeline, runs it, restarts it, replays it, and asserts
the economics come out identical every time.

To run the full test suite:

```bash
python -m pytest -q
```

Expected in this environment: **1180 passed, 37 skipped**. The skips are
tests that require credentials or a display server. One FastAPI deprecation
warning is harmless.

---

## 4. First run in five minutes (no credentials)

Do this before you configure anything. It proves the install and shows you
what the system does.

```bash
# 1. Create directories, the database and the instrument tables (idempotent —
#    safe to run as many times as you like)
python -m app initialize

# 2. Run a backtest on the bundled sample data
python run.py --mode backtest
```

`initialize` creates `data/` (with `sample.csv`, `sample_1h.csv`, example
grid/scoring files), `data/history/`, `data/datasets/`, and the SQLite
database `trading_bot.db` with schema versions 1–8.

`backtest` prints a full metrics JSON: total return, Sharpe, Sortino,
max drawdown, win rate, profit factor, expectancy, exposure, stability,
commission and slippage costs. If you see those numbers, **your install is
correct.**

Other research modes to try immediately:

```bash
python run.py --mode tournament       # every strategy × parameter grid
python run.py --mode validate         # chronological splits
python run.py --mode walk-forward     # rolling train/test
python run.py --mode robustness       # parameter perturbation
python run.py --mode paper --source replay    # paper trading on recorded data
```

### Start the dashboard

```bash
export API_TOKEN=pick-something-private     # required for API endpoints
python run.py --mode api --host 0.0.0.0 --port 8000
```

Then open **http://127.0.0.1:8000/dashboard** — you will be prompted for the
token. Section 7 explains the interface.

> `API_TOKEN` is what protects every API endpoint except `/health`. Use a
> long random value. Never use the placeholder from `.env.example`.

---

## 5. How it works

### 5.1 Data flow

1. **Market data** arrives from one of four sources, selected by
   `DATA_SOURCE`: `replay` (bundled/generated data, the default), `binance`
   (public crypto, read-only), `generic` (your own websocket), or `upstox`
   (real Indian equities via Upstox).
2. **Candles are built** at `DEFAULT_TIMEFRAME`. Two Upstox modes matter here
   and they are not interchangeable:
   - `MARKET_DATA_MODE=ltpc` — builds candles from live ticks. Supports any
     intraday timeframe.
   - `MARKET_DATA_MODE=full` — official Upstox aggregate bars, published for
     **1m and 30m only**.
   If you ask for 5m with `full`, the system refuses with an explanatory
   message rather than silently degrading. This check runs in three places:
   the data adapter, the preflight, and the engine startup.
3. **Strategies emit signals.** Seven ship: `momentum`, `moving_average`,
   `rsi`, `macd`, `bollinger`, `breakout`, `mean_reversion`.
4. **The strategy router** decides which strategy is actually allowed to
   trade, based on lifecycle state and current market conditions.
5. **The risk layer** applies hard limits — position value, open positions,
   daily loss, trades per day, exposure, drawdown.
6. **The broker** executes: simulated (paper), sandbox HTTP, or live HTTP.
7. **Everything is written to the database** — orders, fills, trades,
   transitions, decisions, system events. The dashboard, API and reports all
   read from those rows; nothing is reconstructed from memory.

### 5.2 Event ordering and look-ahead protection

Candles are processed strictly in time order and a decision at time *t* can
only use data available at or before *t*. This is enforced in the data layer,
not left to each strategy's discipline.

### 5.3 Durability

State lives in SQLite with versioned migrations. If the process dies
mid-order, on restart the system reconciles against the broker: an order
whose remote state is unknown is looked up by its intent-derived tag and
resolved as a verified match, a verified absence, or left unresolved —
**never** blindly re-posted. This is one of the more subtle parts of the
codebase and it is well tested.

### 5.4 The learning layer

The system records its own decisions and outcomes, evaluates strategies on
that evidence, and promotes or degrades them through a lifecycle:
`RESEARCH → VALIDATION → WALK_FORWARD → APPROVED → ACTIVE`, with a separate
degradation path `PROBATION → SHADOW → RETIRED`. Promotion requires the full
evidence policy — robustness, walk-forward, drawdown and lifecycle approval
must all pass. A strategy cannot promote itself on a good week.

---

## 6. Configuration — the `.env` file

Copy `.env.example` to `.env` and edit. `.env` is gitignored and never
committed. The file is organised in blocks; here is what each controls.

### 6.1 Core

```ini
INITIAL_CAPITAL=100000       # starting equity for paper/backtest
CURRENCY=INR
DEFAULT_SYMBOL=DEMO
DEFAULT_TIMEFRAME=1D         # 1m 5m 15m 30m 1h 4h 1D
COMMISSION=0.0005            # 5 bps per side
SLIPPAGE=0.0005
SPREAD=0.0002
DATABASE_URL=sqlite:///trading_bot.db
API_TOKEN=                   # REQUIRED for API; make it long and random
```

### 6.2 Market data

```ini
DATA_SOURCE=replay           # replay | binance | generic | upstox
MARKET_DATA_PROVIDER=upstox
MARKET_DATA_SYMBOLS=NSE_EQ|INE002A01018     # start with ONE liquid instrument
MARKET_DATA_TIMEFRAME=1m     # must match DEFAULT_TIMEFRAME
MARKET_DATA_MODE=ltpc        # ltpc (any intraday tf) | full (1m/30m only)
MARKET_DATA_ACCESS_TOKEN=
MARKET_CALENDAR=continuous   # continuous | nse-session
```

Quote `MARKET_DATA_SYMBOLS` when sourcing the file from bash — the `|` is a
shell pipe otherwise.

### 6.3 Execution

```ini
BROKER=LOCAL_PAPER           # LOCAL_PAPER | UPSTOX_SANDBOX | UPSTOX_LIVE
```

### 6.4 Risk limits

```ini
MAX_POSITION_VALUE=0         # 0 = unlimited in paper; REFUSED in live
MAX_OPEN_POSITIONS=0
MAX_DAILY_LOSS=0
MAX_TRADES_PER_DAY=0
```

### 6.5 Alerts (recommended before any unattended run)

```ini
ALERT_WEBHOOK_URL=           # optional HTTP endpoint for pages
LIVE_UNACKED_CRITICAL_MINUTES=15
```

### 6.6 The live block

Do not fill this in until you have completed Section 12. Full detail in
[LIVE_TRADING_ENABLEMENT.md](LIVE_TRADING_ENABLEMENT.md).

```ini
MODE=LIVE
LIVE_TRADING=true
BROKER=UPSTOX_LIVE
LIVE_CONFIRM=I-ACCEPT-REAL-MONEY-RISK
UPSTOX_LIVE_ACCESS_TOKEN=
MAX_POSITION_VALUE=10000     # all four MUST be non-zero
MAX_OPEN_POSITIONS=1
MAX_DAILY_LOSS=2000
MAX_TRADES_PER_DAY=5
DATA_SOURCE=upstox
MARKET_DATA_ACCESS_TOKEN=
MARKET_DATA_SYMBOLS=NSE_EQ|INE002A01018
MARKET_DATA_TIMEFRAME=1m
DEFAULT_TIMEFRAME=1m
MARKET_CALENDAR=nse-session
DATABASE_URL=sqlite:///trading_live.db       # a SEPARATE database for live
```

The four switches are independent by design. `MODE=LIVE` alone does nothing;
`LIVE_TRADING=true` alone does nothing; the bare alias `BROKER=LIVE` is
rejected outright.

---

## 7. The dashboard — how to work it

Start the API (Section 4) and open
**http://127.0.0.1:8000/dashboard**.

The token prompt asks for `API_TOKEN`. The token is kept in browser
`sessionStorage` and sent as a bearer header on every protected call — it
never appears in a URL. Click the error text in the header to re-enter it.

**Local vs remote.** Requests arriving from loopback can load the page without
a token; requests from anywhere else must present the bearer token. This is
deliberate: a dashboard exposed on a network is an entry point, so the gate
does not relax.

### 7.1 Reading the console

- **Command bar** — mode and live badges (rendered server-side, so they are
  truthful even with JavaScript disabled), connection state, a UTC clock, and
  the two appearance dials.
- **The core rings** — the animated element beside the wordmark. Ring colour
  tracks state: green nominal, **amber** when an unacknowledged CRITICAL
  alert exists, **red** on emergency stop. The rings accelerate and fire a
  shockwave **only when the poll actually observed order or fill activity**,
  with a ticker naming the change. If nothing is moving, the console is
  still — motion means the market moved.
- **Vitals rail** — equity, realized P&L, drawdown, open positions,
  unacknowledged CRITICAL alerts, and engine lock state. A `—` means the
  value is genuinely unavailable (for example, no broker account is
  reachable), not zero.
- **Twelve panels** — Account, Feed/Market, Strategy & Signals, Positions &
  Sandbox Orders, Learning/Model, Execution, Alerts & Live Ops, Strategy
  Health, Market Events, Market Context, Quality, Recent Events.
- **Alert rows** are colour-coded by severity: red tR border for CRITICAL,
  amber for WARNING, green for INFO. An unacknowledged CRITICAL blinks.
- **Appearance dials** — `glow` cycles the green intensity (calm/std/vivid)
  and `density` cycles panel spacing (comfortable/compact/dense). Both are
  saved in your browser. Neither touches trading state.

### 7.2 Working the dashboard

- **Polling** — the page refreshes every 5 seconds.
- **EMERGENCY STOP** — the red button. It confirms first, then sets the
  persisted stop flag and cancels open sandbox orders. **The stop persists
  across restarts**; clearing it requires an explicit resume rather than
  restarting the process.
- The footer repeats the mode and, when the configured order host is not a
  documented live host, says so plainly: `ORDER HOST NOT A LIVE HOST`.

---

## 8. The HTTP API

Authenticated endpoints need the header:

```
Authorization: Bearer <API_TOKEN>
```

`GET /health` is public (no token) and returns mode, live flag, broker and a
`real_order_routing` field — useful for monitoring.

Selected endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness + mode, no auth |
| `GET /dashboard` / `GET /dashboard/data` | The console and its payload |
| `GET /api/v2/live/status` | Live-ops snapshot (rails, lock, token verdict) |
| `GET /api/v2/alerts` | Recorded alerts with acknowledgement state |
| `POST /api/v2/alerts/ack` | Acknowledge alerts |
| `GET /api/v2/paper/*` | Paper account, orders, positions, trades, status |
| `GET /api/v2/market/status`, `/market_data` | Feed state and recent data |
| `GET /api/v2/portfolio`, `/risk`, `/performance` | Portfolio and risk views |
| `GET /api/v2/strategies`, `/learning`, `/experiments` | Strategy lifecycle and research |
| `GET /api/v2/market_intelligence` | Market event listener output |
| `GET /api/v2/report/weekly` | Weekly report |
| `GET /api/v2/charts/*.svg` | Equity, drawdown and attribution charts |

---

## 9. Desktop application (optional)

A PySide6 GUI with 13 pages. **Presentation only** — every number comes from
the backend API, and the only write action is the persisted emergency
STOP/RESUME flag. It cannot place orders or change risk limits.

```bash
python -m pip install -r requirements-desktop.txt
export ARENA_BACKEND_URL=http://127.0.0.1:8000
export ARENA_API_TOKEN=<your token>
python -m desktop
```

It needs the backend running. GUI boot was **not verified** in the build
sandbox (no display server), so treat the first launch as unproven.

---

## 10. Command reference

Two entry points:

- `python -m app <command>` — operational commands (29 of them)
- `python run.py --mode <mode>` — research and serving modes

### 10.1 Research and serving — `run.py`

| Mode | What it does |
|---|---|
| `backtest` | Single strategy backtest with full metrics |
| `tournament` | Strategies × parameter grids, ranked |
| `validate` | Chronological train/test splits |
| `walk-forward` | Rolling re-optimisation |
| `robustness` | Parameter perturbation / stability |
| `paper` | Paper trading (`--source replay` for recorded data) |
| `api` | Serve the HTTP API and dashboard |

Useful flags: `--strategy`, `--parameters '{...}'`, `--timeframe`,
`--source {replay,binance,generic,upstox}`, `--csv`, `--symbol`,
`--calendar {continuous,nse-session}`, `--clean`, `--strict-gaps`,
`--host`, `--port`, `--database`.

### 10.2 Verification commands

| Command | Purpose |
|---|---|
| `paper-test` | Offline deterministic pipeline self-test. **Start here.** |
| `sandbox-test [--place-order] [--quantity N]` | Verify sandbox credentials (read-only unless you pass `--place-order`) |
| `live-preflight [--instrument X]` | Full live readiness verdict. Read-only; never places an order |
| `live-drill [--database-url URL]` | Offline fire drill for the safety rails. No network, no orders |
| `monitor` | Self-monitoring snapshot; exit code 1 on alerts (cron-friendly) |
| `upstox-test [--timeout N]` | Test Upstox auth and connectivity (read-only) |

### 10.3 Live operations

| Command | Purpose |
|---|---|
| `live-status [--audit]` | Live-ops snapshot from recorded state; `--audit` recomputes PnL from the audit trail |
| `live-halt [--reason "..."]` | Set the persisted EMERGENCY STOP. **Always permitted** |
| `live-resume [--allow-non-live]` | Clear the stop; refuses when live is not armed |
| `live-ack [--id X] [--kind K] [--all] [--by NAME] [--note "..."]` | Acknowledge alerts (bookkeeping only) |
| `live-digest [--hours N] [--markdown] [--out PATH] [--post]` | Read-only roll-up of alerts, rails, orders, audit PnL |
| `engine [--force-live-lock] [--max-candles N]` | The 24/7 engine. Requires sandbox or live broker |

### 10.4 Data and research

| Command | Purpose |
|---|---|
| `initialize` | Idempotent first-run setup |
| `market-universe {sync,status,audit}` | Instrument master |
| `history {sync,status,audit}` | Historical candles |
| `dataset {build,audit}` | Versioned research datasets |
| `universe screen` | Screen the eligible research universe |
| `research {run,status}` | Parameter experiments |
| `discover` | Controlled strategy discovery run |
| `scan` | Scan the universe for opportunities |
| `lifecycle {status,promote,degrade}` | Strategy pipeline management |
| `replay [--signal X]` | Reconstruct a decision from recorded rows |
| `ask "<question>"` | Natural-language command center over recorded data |
| `report [--format] [--output]` | Human-readable report |
| `maintenance` | Run due maintenance jobs once |
| `backup {backup,verify,restore}` | Database backup and restore |

### 10.5 Credentials

| Command | Purpose |
|---|---|
| `upstox-login` | OAuth helper; writes the token into gitignored `.env` |
| `market-stream [--duration N]` | Print normalized real market events |
| `paper-live` | Full Upstox → candles → strategies → risk → paper pipeline |

---

## 11. The trading modes, in order

Climb these in order. Each one is a superset of the last, so a failure at
stage *n* means do not proceed to stage *n+1*.

### 11.1 Backtest (no network, no credentials)

```bash
python run.py --mode backtest
python run.py --mode tournament
python run.py --mode walk-forward
```

Proves the strategy logic on historical data.

### 11.2 Paper trading (no credentials)

Two flavours:

```bash
# recorded/replay data — fully offline, deterministic
python run.py --mode paper --source replay

# real market data, simulated orders (needs market-data credentials)
python -m app paper-live
```

This is where you spend most of your time. The strategies, risk layer and
database writes are all real; only the broker is simulated.

### 11.3 Sandbox (free Upstox credentials)

Sandbox places **real HTTP orders on Upstox's sandbox host** with real market
data and no real money. It is the last stop before live.

```bash
# 1. Put sandbox credentials in .env
BROKER=UPSTOX_SANDBOX
UPSTOX_SANDBOX_ACCESS_TOKEN=<sandbox token>

# 2. Verify credentials (read-only)
python -m app sandbox-test

# 3. Place ONE small sandbox order to prove the full path
python -m app sandbox-test --place-order --quantity 1

# 4. Run the continuous engine
python -m app engine
```

Notes:
- One sandbox app per Upstox user, and sandbox tokens work only on the
  sandbox order APIs.
- The sandbox default host is `api-sandbox.upstox.com`. The live order host
  `api-hft.upstox.com` is **refused** by the sandbox broker by name.

### 11.4 Live (real money)

Do not start here. Complete Section 12, then:

```bash
python -m app live-preflight      # must report readiness.ok: true
python -m app live-drill          # exercise the rails offline
python -m app engine              # with canary caps in place
```

---

## 12. Going live — the complete checklist

### Phase 1: Upstox account setup (start today, has a waiting period)

1. **Register a static IP with Upstox.** SEBI/NSE require it for order APIs
   as of 2025-10-01. You need a primary and a backup, and it can only be
   changed once a week. Register it on the Upstox Apps page. **This is the
   step with a queue — do it first.**
2. **Create a live app** on Upstox to get your API key and secret.
3. **Generate an access token.** Live tokens expire **daily**; refresh with
   `python -m app upstox-login`.

Regulatory context: algorithmic trading through a broker API requires that
your orders pass generic risk checks and carry an algo identifier. Under 10
orders per second is treated as low-frequency, which does not need NSE algo
registration — but the risk checks and identifier still apply. The
`LIVE_ALGO_NAME` variable exists for the case where you have an
exchange-approved algo name.

### Phase 2: Configuration

Fill in the live block from Section 6.6. Every line is required; any omission
aborts startup and names the missing item.

Before you continue, satisfy yourself that:

- [ ] All four switches are set (`MODE`, `LIVE_TRADING`, `BROKER`, `LIVE_CONFIRM` with the exact phrase)
- [ ] All four limits are non-zero and chosen deliberately
- [ ] `DATABASE_URL` points at a **separate** database from your paper one
- [ ] `MARKET_DATA_SYMBOLS` has exactly one liquid instrument
- [ ] `DEFAULT_TIMEFRAME` matches `MARKET_DATA_TIMEFRAME`, and the pairing is legal for your `MARKET_DATA_MODE`
- [ ] `ALERT_WEBHOOK_URL` is set if you will not be watching the screen

### Phase 3: Prove it before risking money

```bash
python -m app live-preflight
```

This is read-only and never places an order. It checks the order host is a
documented live host, the credential verdict, the locks, the risk limits, the
audit-trail integrity, and the state a fresh engine would inherit. **You must
see `readiness.ok: true` with zero errors.** If it reports errors, fix them —
do not proceed on a warning you have talked yourself out of.

```bash
python -m app live-drill
```

Runs the safety rails offline: halt, resume, duplicate-engine refusal, the
watchdog, the single-engine lock.

### Phase 4: First live session

```bash
python -m app engine
```

Start with the canary ramp active (`LIVE_CANARY_MAX_NOTIONAL` and
`LIVE_CANARY_ORDERS`) so the first entries are capped. Watch the dashboard.
Confirm the alerts rail works. Keep the first session small and short.

### Stopping and resuming

```bash
python -m app live-halt --reason "stopping for the day"    # always works
python -m app live-resume                                   # requires armed live
```

Ctrl-C also stops the engine, which reconciles before exiting. The emergency
stop flag **persists across restarts** and requires an explicit resume — that
is the point.

### Going back to paper

Remove or rename the four live switches. Live is never the default and there
is no state to unwind.

---

## 13. The safety systems, explained

These exist around the live session. All are inert unless live is enabled.

**Single-engine lock.** A second live engine refuses to start while the first
is heartbeating. A crashed engine's heartbeat goes stale and never blocks a
restart. Override with `--force-live-lock` only when certain the other process
is dead.

**Canary ramp.** Caps the notional of the first N live entries so a
misconfiguration cannot immediately trade full size.

**Token freshness.** New entries are refused when the last verified
credential is older than `LIVE_TOKEN_MAX_AGE_HOURS`. Running `live-preflight`
is what records freshness.

**Watchdog.** An order working longer than `LIVE_STUCK_ORDER_MINUTES` raises a
WARNING, escalating to CRITICAL after
`LIVE_STUCK_ORDER_ESCALATE_MINUTES`. Silence from reconciliation raises
`RECONCILE_STALLED`.

**Kill switches.** Daily-loss and equity-drawdown breaches trigger a
persisted auto-stop (`LIVE_AUTO_STOP_ON_BREACH`).

**Alert acknowledgement.** A CRITICAL nobody acknowledges within
`LIVE_UNACKED_CRITICAL_MINUTES` becomes an incident of its own. Acknowledge
with `live-ack`. This closes the gap where an alarm fires into an empty room.

**Alert delivery.** Durable sink always; optional webhook with bounded
retries. A failed alert never becomes a failed order path — `emit()` never
raises.

**Audit trail.** Every order, fill, transition and decision is a row. The
audit-trail PnL can be recomputed independently
(`live-status --audit`) and compared against the reported PnL.

**Exits are never blocked.** The rails gate new *entries*. Protective stops
and exits always get through, because a rail that traps you in a position is
worse than the risk it was guarding against.

---

## 14. Feature catalog

**Research** — backtesting with full metrics; tournaments across strategies
and parameter grids; chronological validation; walk-forward analysis;
robustness testing; composite scoring.

**Strategy lifecycle** — `RESEARCH → VALIDATION → WALK_FORWARD → APPROVED →
ACTIVE` with degradation `PROBATION → SHADOW → RETIRED`. Promotion requires
the complete evidence policy.

**Strategy discovery** — controlled automated search over the eligible
universe, behind hard validation gates.

**Market event listener** — ingests public-internet intelligence (conflict,
sanctions, tariffs, policy, energy, shipping, regulatory) and assesses impact
through transmission paths rather than simplistic reactions. It decides
IGNORE / MONITOR / EVALUATE / NO_TRADE and provides context to the strategy
router. It never places orders and never bypasses any gate.

**Observational learning memory** — records price, volatility and volume
response per event at 5m/30m/1h/1d/3d/5d horizons. Observational only.

**Risk layer** — hard position, exposure, daily-loss, trade-count and
drawdown limits, enforced at the execution boundary.

**Execution** — slice-level order accounting, per-child fill tracking,
incremental-fill price accounting, unknown-order recovery with verified
match/absence resolution, and confirmed cancellations.

**Reconciliation** — restart recovery that never loses or duplicates a child
order, and never blindly re-posts.

**Dashboard** — the twelve-panel console in Section 7.

**HTTP API** — full read surface plus alert acknowledgement and the stop flag.

**Desktop GUI** — 13 read-only pages plus emergency stop.

**Reporting** — daily/weekly/monthly reports with attribution; SVG charts for
equity, drawdown and attribution.

**Command center** — ask natural-language questions about recorded data.

**Monitoring** — `monitor` exits non-zero on alerts, ready for cron;
reality-gap and anomaly detection; scheduled backups.

**Backup** — `backup backup|verify|restore` with labels.

**Determinism checking** — `tools/determinism_check.py` runs the real
backtest engine twice in-process and again in fresh interpreters with
different `PYTHONHASHSEED` values, comparing a canonical fingerprint of
orders, fills, trades and metrics. A failure names the first differing
location.

**Security scanning** — `tools/security_scan.py` checks the tree for leaked
credentials; the test suite asserts no `.env` is committed.

---

## 15. Everyday recipes

**Run a strategy comparison**

```bash
python run.py --mode tournament --timeframe 1h --calendar nse-session
```

**Validate something before believing it**

```bash
python run.py --mode walk-forward --strategy momentum
```

**Watch the paper system on replay data**

```bash
python run.py --mode paper --source replay
```

**Serve the console**

```bash
export API_TOKEN=...
python run.py --mode api --host 0.0.0.0 --port 8000
```

**Daily operations check**

```bash
python -m app live-status --audit
python -m app live-digest --hours 24 --markdown
python -m app monitor            # exit 1 if anything is alerting
```

**Acknowledge the alarm**

```bash
python -m app live-ack --kind RECONCILE_STALLED --by "your name" --note "checked, broker was down"
```

**Back up before changes**

```bash
python -m app backup backup --label pre-change
# prints the file it wrote, e.g. data/backups/arena-20261003T050733-pre-change.db
python -m app backup verify --path data/backups/arena-20261003T050733-pre-change.db
```

`verify` and `restore` act on a named file, so `--path` is required — omitting
it is refused with an explanatory message rather than a traceback.

**Check determinism**

```bash
python tools/determinism_check.py
```

---

## 16. Troubleshooting

**`ModuleNotFoundError`** — the virtualenv is not active. Run
`source .venv/bin/activate`, or call `.venv/bin/python` directly.

**`SystemExit` from pip, "externally-managed-environment"** — you are using
system Python. Use the virtualenv (PEP 668 blocks system installs by design).

**API returns 401** — set `API_TOKEN` in your shell or `.env`, and send
`Authorization: Bearer <token>`. Note that non-loopback clients always need
the token even for the dashboard page.

**Dashboard panels are empty / all `—`** — the value is genuinely
unavailable. This is common with no broker account reachable. Check the
footer for mode and order-host state.

**Engine refuses to start** — in paper mode that is correct behaviour; use
`paper-live`. Otherwise the message names exactly which switch or limit is
missing.

**"MARKET_DATA_MODE=full carries official 1m/30m bars only"** — you asked for
a timeframe `full` does not publish. Either set `MARKET_DATA_MODE=ltpc`, or
move to 1m/30m. Do not force it.

**Data looks empty after exporting variables and sourcing a file** — `VAR=x
bash script.sh` gets overwritten if the script sources an env file. Export
*after* sourcing: `bash -c 'set -a; . envfile; set +a; export X=…'`. Also
remember that sourcing without `set -a` does not export, so the app silently
falls back to defaults.

**Tests failing after a fresh clone** — make sure you installed
`requirements.txt` *and* `pytest`, and that you are on Python 3.11+.

More: [FULL_TROUBLESHOOTING_GUIDE.md](FULL_TROUBLESHOOTING_GUIDE.md) and
[QUICK_DIAGNOSTIC_FLOWCHART.md](QUICK_DIAGNOSTIC_FLOWCHART.md).

---

## 17. Where you are, and what to do next

| You want to… | Do this |
|---|---|
| Prove the install works | `python -m app paper-test` |
| See it trade (no credentials) | `python run.py --mode paper --source replay` |
| See the console | `python run.py --mode api` then open `/dashboard` |
| Trade the sandbox | Section 11.3 |
| Trade real money | Section 12 — **start the static IP registration today** |

The system is built so that the last step is the only one that can cost you
money, and the only one that needs your deliberate, repeated consent.
Everything before it is free to practise on.
