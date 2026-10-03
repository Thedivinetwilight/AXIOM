# Arena — Install the `.exe` and Start Trading

A step-by-step guide from downloading the file to a running session. No Python,
no command line, no prior knowledge assumed.

**Read §1 before you start** — it tells you honestly how far you can get today.

---

## 1. What you can actually do, and when

There are three ordered stages. Each one is safe and each one takes you closer.

| Stage | Money at risk | What you need | Time to start |
|---|---|---|---|
| **1. Paper trading** | **None** — fully simulated | Nothing at all | 2 minutes |
| **2. Sandbox** | None — real API, fake fills | An Upstox **sandbox** app | ~15 minutes |
| **3. Live** | **Real money** | A static IP **registered with Upstox**, a live app, daily tokens | **Start this today — it has a queue** |

**Do this now, before anything else:** if you intend to go live eventually,
register your **static IP with Upstox**. SEBI/NSE have required it for order
APIs since 1 October 2025, you need a primary *and* a backup, and it can only
be changed once a week. It is the only step with a waiting period. Everything
else can be done while you wait.

Meanwhile, stages 1 and 2 give you the full experience with no money involved.

---

## 2. Download

1. Open the repository on GitHub.
2. Click the **Actions** tab.
3. In the left sidebar click **Build executable**.
4. Click the newest run at the top of the list (a green ✓).
5. Scroll to the bottom, to **Artifacts**.
6. Click **`Arena-windows-x64`** to download a ZIP (~18 MB).
7. Open the ZIP and extract **`Arena.exe`** anywhere convenient — your Desktop
   or a folder like `C:\Arena`.

Artifacts are kept for **30 days** after each build, so download the most recent
one. If you prefer to build it yourself, see §9.

> The macOS (`Arena-macos`) and Linux (`Arena-linux-x64`) artifacts are built by
> the same workflow if you need them.

---

## 3. Install

There is no installer. `Arena.exe` is a single self-contained file — no Python,
no dependencies, nothing to configure.

### 3.1 The Windows SmartScreen warning

The first time you run it, Windows will likely show **"Windows protected your
PC"** with a *Don't run* button. This is expected and is not a virus detection:

- `Arena.exe` is **unsigned**. Windows flags every unsigned executable that it
  has not seen before; that is a reputation warning, not a scan result.
- Click **More info** → **Run anyway**.

You only do this once. If the warning bothers you, §9 explains signing the
binary so it disappears.

> If your antivirus quarantines it, that is the same reputation issue. Add an
> exclusion, or build from source yourself (§9) — a locally built executable is
> not flagged.

### 3.2 Where Arena keeps your files

**Not** next to the `.exe`. That folder may be read-only, and Windows
occasionally relocates it. Arena stores everything in your user profile:

```
C:\Users\<you>\AppData\Local\Arena\
├── .env               your settings (credentials, mode, limits)
├── trading_bot.db     trades, positions, audit trail
├── data\              price data and candle cache
├── logs\              logs
├── results\           backtest and paper-session output
└── backups\           database backups
```

To see the exact path, run:

```
Arena.exe --diagnostics
```

To keep it somewhere else — an external drive, or several configurations side
by side — set `ARENA_HOME` before starting:

```cmd
set ARENA_HOME=D:\ArenaData
Arena.exe
```

### 3.3 Firewall

Arena binds to `127.0.0.1` only, so your firewall should not prompt at all.
If it does, **allow it on private networks only** — nothing outside your machine
needs to reach it unless you deliberately pass `--host 0.0.0.0` (§7.4).

---

## 4. First run — do this in the next 2 minutes

Double-click **`Arena.exe`**.

A black console window opens (that is the server — keep it open) and your
browser opens on the setup screen:

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

> **If Arena cannot start**, the console window stays open and says so
> (`Arena did not start: …`) instead of flashing and disappearing. Copy that
> text — it is the first thing anyone helping you will ask for. A failed start
> changes nothing and places no order. If you ever close the window before
> reading it, run Arena from a terminal instead: open Command Prompt in the
> folder holding `Arena.exe` and type `Arena.exe` — the message stays on screen.

**Before entering any credentials, prove it works.** Arm yourself with the
quickstart at the top of the setup page. In the Arena console window type `Ctrl+C`
to stop, then run:

```cmd
Arena.exe --run paper --source replay --symbol DEMO
```

You will see one JSON line per event — signals, risk decisions, simulated
orders — scrolling past. That is the whole trading pipeline running on bundled
sample data with **no credentials and no money**. Press `Ctrl+C` to stop.

That is your baseline: if it works, Arena works.

---

## 5. Set up the interface (still in the next 5 minutes)

Back on the setup screen (`http://127.0.0.1:8042/setup`):

### 5.1 Generate a token

Click **Generate a secure token**, then **Copy** and paste it somewhere safe.
It protects the interface; you will paste it into the console once and your
browser remembers it for that session.

### 5.2 Leave everything else alone (for paper trading)

`BROKER` stays **`LOCAL_PAPER`** and `MODE` stays **`PAPER`**. Skip the
credentials section entirely.

### 5.3 Save, then restart

Press **Save configuration**. Arena tells you a restart is needed — the running
server still holds the old settings. Close the console window and start
`Arena.exe` again.

This time it opens **`/console`** directly: the feature console, with all 36
features listed.

---

## 6. Start paper trading

Paper trading is a full simulation: real strategy logic, real risk checks, real
order bookkeeping, no broker and no money.

**From the console** (recommended): find **`paper`** under *run.py modes*, set
`--symbol` to `DEMO`, and press **Run command**. Leave the timeout at 15
minutes. Output streams into the panel; the command runs until the timeout and
is then stopped cleanly.

**From the console window** (what I recommend for a long session, because the
server stays responsive):

```cmd
Arena.exe --run paper --source replay --symbol DEMO
```

`Ctrl+C` stops it cleanly and cancels any working orders.

### Watching it

In the same window where the server runs, open a browser at
**`http://127.0.0.1:8042/dashboard`** — the live HUD showing account, positions,
orders, strategy state and recent events, refreshing every 5 seconds. Enter the
same API token when prompted.

### 6.1 When your data is your own

`DEFAULT_SYMBOL` and the data must agree. `DEMO` matches the bundled sample
file. To use your own CSV:

```cmd
Arena.exe --run paper --source replay --csv C:\path\to\your.csv --symbol YOURSYMBOL
```

The CSV needs columns `timestamp,open,high,low,close,volume,symbol`. A mismatch
between the symbol and the file shows up as `Invalid symbol`.

---

## 7. Where your results go

Everything from §4–§6 is written under your Arena home, never next to the `.exe`:

| What | Where |
|---|---|
| Backtest / paper output | `%LOCALAPPDATA%\Arena\results\<mode>\` |
| Trades, positions, audit trail | `%LOCALAPPDATA%\Arena\trading_bot.db` |
| Logs | `%LOCALAPPDATA%\Arena\logs\` |

`results\<mode>\result.json` holds the summary; `trades.csv` and `equity.csv`
sit beside it for a spreadsheet.

---

## 8. Going further: sandbox, then live

### 8.1 Sandbox — real API, no real money

1. Create a **sandbox app** in the Upstox developer console (only one per
   user). Copy its access token.
2. Setup screen → `BROKER` = **`UPSTOX_SANDBOX`**, paste
   `UPSTOX_SANDBOX_ACCESS_TOKEN`, Save, restart.
3. Verify with `Arena.exe --cli sandbox-test` — read-only, places no order.

Sandbox orders go to Upstox's real API with simulated fills. This is where you
find out whether your credentials and instrument keys are right.

### 8.2 Live — real money

**The setup screen will not do this for you, on purpose.** It refuses to set
`LIVE_TRADING`, a live broker, or any risk limit, and tells you so. Arena is
built so that no single control — no button, no checkbox, no environment
variable — can start real-money trading. You enable live by hand, in the
`.env` file, deliberately.

#### Phase 1 — Upstox prerequisites (start these today)

1. **Register a static IP**, primary and backup, on the Upstox Apps page.
   Mandatory for order APIs since 2025-10-01. **This is the one with a queue.**
2. Create a **live app** to get your API key and secret.
3. Understand: live tokens **expire daily**. Refresh with
   `Arena.exe --cli upstox-login`, which writes the new token into
   `%LOCALAPPDATA%\Arena\.env` — the same file the application reads.

#### Phase 2 — edit `.env` by hand

Open `%LOCALAPPDATA%\Arena\.env` in Notepad. Set all four switches **and** all
four limits to non-zero values you have chosen deliberately:

```ini
MODE=LIVE
LIVE_TRADING=true
BROKER=UPSTOX_LIVE
LIVE_CONFIRM=I-ACCEPT-REAL-MONEY-RISK

# All four MUST be non-zero, or startup aborts and names the missing one.
MAX_POSITION_VALUE=10000
MAX_OPEN_POSITIONS=1
MAX_DAILY_LOSS=2000
MAX_TRADES_PER_DAY=5

UPSTOX_LIVE_ACCESS_TOKEN=<your daily token, never shared>
UPSTOX_CLIENT_ID=<your live app key>
UPSTOX_CLIENT_SECRET=<your live app secret>
UPSTOX_REDIRECT_URI=<must match your Upstox app>

DATA_SOURCE=upstox
MARKET_DATA_ACCESS_TOKEN=<your data token>
MARKET_DATA_SYMBOLS=NSE_EQ|INE002A01018
MARKET_DATA_TIMEFRAME=1m
DEFAULT_TIMEFRAME=1m
MARKET_CALENDAR=nse-session

# A SEPARATE database from your paper one.
DATABASE_URL=sqlite:///trading_live.db

# Strongly recommended by the config checker:
API_TOKEN=<the token you generated on the setup screen>   # else the dashboard 503s
LIVE_CANARY_MAX_NOTIONAL=5000   # cap the first entries; 0 disables the canary
LIVE_CANARY_ORDERS=10           # how many entries stay capped
LIVE_MAX_EQUITY_DRAWDOWN=0.05   # auto-stop at 5% drawdown; 0 disables it
ALERT_WEBHOOK_URL=<your Slack or Discord webhook>   # so you hear about problems
```

`MODE=LIVE` on its own does nothing. `LIVE_TRADING=true` on its own does
nothing. The bare alias `BROKER=LIVE` is rejected outright. That is the design.

> `UPSTOX_CLIENT_ID`, `UPSTOX_CLIENT_SECRET` and `UPSTOX_REDIRECT_URI` are read
> by the OAuth login flow rather than by the settings layer, so the config
> checker reports them as "not a setting the application reads". They **are**
> used — by `Arena.exe --cli upstox-login` — so keep them.

#### Phase 3 — prove it before risking a rupee

```cmd
Arena.exe --cli live-preflight
```

Read-only; never places an order. **You must see `readiness.ok: true` with zero
errors.** If it lists problems, fix them — do not proceed on a warning you have
talked yourself out of.

```cmd
Arena.exe --cli live-drill
```

Runs the safety rails offline: halt, resume, duplicate-engine refusal, the
watchdog, the single-engine lock.

#### Phase 4 — first live session

```cmd
Arena.exe --cli engine
```

Watch the dashboard. Confirm your alerts arrive. Keep the first session small
and short, with the canary limits in place so opening entries are capped.

#### Stopping and resuming

```cmd
Arena.exe --cli live-halt --reason "stopping for the day"
Arena.exe --cli live-resume
```

`live-halt` **always** works. `live-resume` requires an armed live session. The
emergency stop **persists across restarts** and needs an explicit resume — that
is the point of it.

#### Going back to paper

Delete the four live switches from `.env`. Live is never the default and there
is nothing to unwind.

> **Regulatory note.** Algorithmic orders through a broker API must pass
> generic risk checks and carry an algo identifier. Under 10 orders/second is
> treated as low-frequency and does not need NSE algo registration, but the risk
> checks and identifier still apply. `LIVE_ALGO_NAME` exists if you have an
> exchange-approved algo name.

---

## 9. Reference

### Command-line options

| Command | What it does |
|---|---|
| `Arena.exe` | Start the server; opens setup first run, console after |
| `Arena.exe --port 9000` | Use a specific port (default 8042) |
| `Arena.exe --setup` | Re-open the credential screen |
| `Arena.exe --no-browser` | Start without opening a browser |
| `Arena.exe --diagnostics` | Print where your files live, then exit |
| `Arena.exe --cli <command>` | Run any of the 29 CLI commands |
| `Arena.exe --run <mode>` | Run any of the 7 `run.py` modes |

### Useful commands

| Command | What it does |
|---|---|
| `Arena.exe --cli paper-test` | Offline self-test, no network, no credentials |
| `Arena.exe --cli live-status` | Current mode, halt state, rails (read-only) |
| `Arena.exe --cli live-digest` | Roll-up of alerts, orders and PnL |
| `Arena.exe --cli monitor` | Host and database health snapshot |
| `Arena.exe --cli backup backup` | Back up the database |
| `Arena.exe --cli initialize` | Re-run first-time setup (idempotent) |

### Building it yourself

```bash
pip install -r requirements.txt
python build_exe.py --test
```

`--test` builds and then starts the result in a scratch directory and checks it
serves the interface and creates its own database. **Never ship a build that
fails this.**

> PyInstaller cannot cross-compile: a Windows `.exe` must be built on Windows.
> Use the Actions workflow (§2) if you do not have the target platform.

To remove the SmartScreen warning, sign the binary:

```cmd
signtool sign /f your-certificate.pfx dist\Arena.exe
```

---

## 10. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "Windows protected your PC" | Unsigned executable. **More info → Run anyway**. Expected. |
| Browser doesn't open | Go to `http://127.0.0.1:8042` manually. Check the console window for the actual port — Arena picks the next free one if 8042 is taken. |
| Console asks for a token and rejects it | Use the value of `API_TOKEN` in `%LOCALAPPDATA%\Arena\.env`. To reset it: delete that line, restart, and the setup screen offers you a new one. |
| "Port is in use" | Arena already moves to the next free port and says so. Usually a previous Arena still running — check Task Manager. |
| `Invalid symbol` | `DEFAULT_SYMBOL` doesn't match your data. Bundled sample data uses `DEMO`; a live feed needs an instrument key like `NSE_EQ|INE002A01018`. |
| `Set MARKET_DATA_SYMBOLS` | You picked a live data source without symbols. Either set them, or use `--source replay` for paper trading. |
| `live-preflight` says "BROKER is not UPSTOX_LIVE" | Correct behaviour in paper mode. It only runs against live configuration. |
| `sandbox-test` says the token is not set | Expected until you add `UPSTOX_SANDBOX_ACCESS_TOKEN`. It attempts nothing and claims nothing. |
| Nothing appears after `Arena.exe --run paper ...` | Check you passed `--symbol DEMO` (matching the bundled data). |
| SmartScreen/antivirus deletes the file | Reputation, not detection. Add an exclusion or build locally (§9). |
| Where did my results go? | `%LOCALAPPDATA%\Arena\results\`. Never next to the `.exe` (§3.2). |

### Getting help

Run `Arena.exe --diagnostics` and include its output — it shows the version,
where Arena keeps its files, and whether it is running as a packaged build.
Never include your `.env` file: it contains your credentials.

---

## 11. The one-page version

```
1.  GitHub → Actions → "Build executable" → newest green run
2.  Download artifact "Arena-windows-x64", unzip, run Arena.exe
3.  SmartScreen: More info → Run anyway
4.  Browser opens /setup → Generate a secure token → copy it
5.  Leave BROKER=LOCAL_PAPER and MODE=PAPER → Save
6.  Close the window, run Arena.exe again (now opens /console)
7.  Terminal:  Arena.exe --run paper --source replay --symbol DEMO
8.  Dashboard: http://127.0.0.1:8042/dashboard   ← watch it work

Real money, later, by hand — never from the setup screen:
9.  Register your static IP with Upstox TODAY (it has a queue)
10. Edit %LOCALAPPDATA%\Arena\.env: the four switches + four non-zero limits
11. Arena.exe --cli live-preflight    → must say readiness.ok: true
12. Arena.exe --cli live-drill        → safety rails, offline
13. Arena.exe --cli engine            → small size, watch the dashboard
14. Arena.exe --cli live-halt --reason "stopping"
```

---

**Two things worth repeating.** Paper trading needs no credentials, so there is
never a reason to start with real money. And the setup screen refusing to enable
live trading is not an oversight — it is the safety model, and it is why a
mis-click cannot cost you money.
