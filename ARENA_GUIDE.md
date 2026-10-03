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

**Or skip all of that: double-click `START_HERE.bat`.** It checks for Python,
creates the virtual environment, installs the dependencies and starts Arena with
the browser open — the same steps as above, automated. It sets up only once.

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

> Want one file you can double-click instead of running commands?
> See **section 8.5 — Running as a single executable**.

Two interfaces are served from that one process:

| Open | What it is |
|---|---|
| **http://127.0.0.1:8000/console** | **The feature console — all 36 features, click to run.** See section 7.3 |
| http://127.0.0.1:8000/dashboard | The live trading HUD. Section 7 |
| http://127.0.0.1:8000/docs | Interactive API reference |

The console is the fastest way to reach everything; the dashboard is the
at-a-glance view of a running paper session. You will be prompted for the token
in both.

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
- The **features →** button in the header opens the feature console (7.3).

### 7.3 The feature console — every command, in the browser

Open **`/console`** on a running API server. It lists every feature the
platform offers and runs the one you pick, showing its exact output.

**What it covers.** All 29 `python -m app` commands and all 7 `run.py` modes —
36 features. The list is **read from the argument parsers themselves**, not
hand-written, so it cannot drift: a command added to the CLI appears in the
console automatically, and a removed command cannot linger as a dead button.

**How to use it**

0. **First run:** Arena opens `/setup` instead. Enter your credentials there
   (it can generate a secure `API_TOKEN` for you), save, and restart.
1. Open `/console`. Enter your `API_TOKEN` when prompted — it is kept in the
   browser's session storage for that tab only.
2. Filter the list, or pick a command. On the right, a form is built from that
   command's real arguments: fixed sets become drop-downs, optional flags become
   checkboxes, and each field shows the same help text as `--help`.
3. Adjust the timeout if the command is a long one, then **Run command**
   (or press **Ctrl/Cmd+Enter**).
4. The output pane shows the exit code, the duration, and the full stdout and
   stderr. **Recent runs** lets you click a previous run to reload it.

Every command shows its raw equivalent, e.g.
`python -m app research run --name demo --periods 2,3`, so anything you do here
you can copy straight into a terminal or a cron job.

**Badges.** Commands are marked `network` (leaves the machine), `changes state`
(can move a lifecycle state or halt/resume), and `long-running` (does not return
on its own; the console will stop it at the timeout and say so).

**What it will not do**

- It cannot enable a mode your `.env` has not already enabled. It runs the real
  CLI as a child process and sets **no** environment variables, so the
  four-switch live opt-in and the risk limits are still evaluated by the
  application itself. There is no "go live" button because there is no code
  path here that could create one.
- It is not a second implementation of anything. Every gate, lock and refusal
  behaves exactly as in the terminal.
- `/console/run` and `/console/catalog` require the bearer token — the same
  gate as every other protected route. Only the HTML shell is served to a local
  browser without a token, exactly like the dashboard page, and it contains no
  secrets and no data.

**Endpoints**

| Endpoint | Purpose |
|---|---|
| `GET /console` | The page (local browsers; no secrets inside) |
| `GET /console/catalog` | Every command and flag, derived from the parsers |
| `POST /console/run` | Run one command, return exit code + output |
| `GET /console/state` | Read-only mode/engine snapshot |

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
| `GET /dashboard` / `GET /dashboard/data` | The live trading HUD and its payload |
| `GET /console` | The feature console page (every command) |
| `GET /console/catalog` | Every feature, derived from the CLI parsers |
| `POST /console/run` | Run one CLI command, return its output |
| `GET /console/state` | Mode/engine snapshot for the console header |
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

## 8.5 Running as a single executable

Everything — server, interface and every command — can be packaged into one
file you double-click.

### The easy way: download a built executable

The repository contains a GitHub Actions workflow that builds the executable
for Windows, macOS and Linux and runs a smoke test on it:

1. Open the repo's **Actions** tab → **Build executable** → **Run workflow**.
2. When it finishes, download the **`Arena-windows-x64`** artifact.
3. Unzip and run **`Arena.exe`**.

No Python installation is needed on the target machine.

### Building it yourself

On the machine you want the executable for:

```bash
pip install -r requirements.txt
python build_exe.py --test
```

`--test` builds, then starts the result in a scratch data directory and checks
that it serves the interface and creates its own database. **Never ship a build
that fails this** — a bundle can compile cleanly and still be missing a module
that only breaks on the first request.

> PyInstaller cannot cross-compile. A Windows `.exe` must be built on Windows;
> a Linux binary must be built on Linux. Use the Actions workflow if you do not
> have the target platform to hand.

### What happens when you run it

```
  ------------------------------------------------------------------
   ARENA — trading research platform
  ------------------------------------------------------------------
   Open this address:   http://127.0.0.1:8042/setup
   (first run: enter your credentials, then press Save)

   Keep this window open while you use Arena.
   Press Ctrl+C here to stop.
  ------------------------------------------------------------------
```

Your browser opens on the **setup screen**. Enter your credentials, press Save,
restart, and the console opens from then on. The window that stays open is the
server — closing it stops Arena.

| Option | Effect |
|---|---|
| *(none)* | Start, open the browser on setup (first run) or the console |
| `--no-browser` | Start without opening a browser (servers, containers) |
| `--port 9000` | Use a specific port (default 8042; finds the next free one if busy) |
| `--host 0.0.0.0` | Accept connections from other machines — **read the warning below** |
| `--setup` | Re-open the credential screen |
| `--diagnostics` | Print where Arena keeps its files, then exit |
| `--cli <command>` | Run any CLI command, e.g. `--cli live-status` |

### Where your data lives

A packaged Arena does **not** write next to the executable. That folder may be
read-only, and a one-file build unpacks into a temporary directory that is
deleted on exit. Your state lives in a per-user folder instead:

| Platform | Location |
|---|---|
| Windows | `%LOCALAPPDATA%\Arena` |
| macOS | `~/Library/Application Support/Arena` |
| Linux | `~/.local/share/Arena` |

`--diagnostics` prints the exact path, and the setup screen shows it too.
Inside that folder: `trading_bot.db` (trades, positions, audit trail), `.env`
(your configuration), `data/` (candles), `logs/`, `results/`, `backups/`.

Set **`ARENA_HOME`** to override it — useful for a portable install on a USB
stick, or to keep several configurations side by side. A `.env` placed beside
the executable is also picked up, so a portable copy stays self-contained.

> **Binding to `0.0.0.0`** makes the interface reachable from your network. The
> console can *run commands*, so only do this with `API_TOKEN` set to a long
> random value, and never on an untrusted network. Arena refuses setup saves and
> console runs from non-local clients without that token.

### If SmartScreen or antivirus complains

Unsigned executables produced by PyInstaller are commonly flagged on Windows.
That is a reputation problem, not a detection of anything malicious. To proceed,
choose **More info → Run anyway**. To avoid it entirely, sign the binary with a
code-signing certificate (`signtool sign /f cert.pfx dist\Arena.exe`) — the spec
deliberately disables UPX compression because compressed binaries are flagged
far more often.

### Platform notes (Windows)

The executable is built and smoke-tested on Windows by CI, and the core paths —
install, setup, console, paper trading, monitoring, live gates — pass there. A
few older test cases in the research subsystems still assume POSIX and are
listed here rather than hidden, because they mark real differences:

| Area | Behaviour on Windows |
|---|---|
| System monitoring | Working. Memory, RSS and disk are reported through Windows APIs. |
| `python -m app monitor` | Working, including the memory threshold. |
| Kronos worker process | `test_clean_shutdown_leaves_no_process_behind` fails with WinError 87 — process teardown semantics differ. The research worker path is untested there. |
| Kronos model cache | The `~` expansion tests assume POSIX tilde/XDG handling. Weights still load; the cache location may differ from what those tests expect. |
| `upstox-login` token write | One test asserts POSIX file permissions, which Windows does not apply. The token is still written to a gitignored `.env`. |

None of these affect the trading path. Treat Windows as verified for
paper/sandbox operation and for the packaging, and unverified for the Kronos
research extras.

### Live trading from the executable

Unchanged, and deliberately unchanged: the setup screen **refuses** to set
`LIVE_TRADING`, a live `BROKER`/`MODE`, or any risk limit. A packaged app is
still not a shortcut past the opt-in. See section 0 and the "Going live"
checklist.

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

- `python -m app <command>` — operational commands (29 of them). **All 29
  are also available as buttons in the feature console — see 7.3.**
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
| `tools/live_config_check.py` | Check a live/sandbox config offline, before `live-preflight` |
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
   `python -m app upstox-login`. Paste it into `.env`, then let
   `tools/live_config_check.py` confirm it does not look like a placeholder.

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
python tools/live_config_check.py
```

This is the step people skip and then regret. It reads your configuration the
same way the app does and reports **every** problem at once, instead of the
first one:

- the four switches and four non-zero limits,
- whether the order host is really a live order host,
- whether your timeframe and market-data mode are a pair the engine accepts,
- **keys you mistyped** — `MAX_POSITION_SIZE` instead of `MAX_POSITION_VALUE`
  is silently ignored by the app, so the limit looks set and is not,
- unfilled placeholders (`<your-token>`) and tokens carrying whitespace,
- rails that are inert: canary off, drawdown auto-stop off, no webhook.

It is offline, it places no order, and it never prints a credential. Exit code
0 means nothing blocks, 1 means at least one blocker, 2 means the config is not
aiming at live at all. `--json` gives machine-readable output and `--env-file`
checks a candidate file before you replace the real one.

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

**Before any live session (safety habit)**

```bash
python tools/live_config_check.py     # every config problem, at once
python -m app live-preflight          # the authority: readiness.ok must be true
python -m app live-drill              # exercise the rails offline
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
