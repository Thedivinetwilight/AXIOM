# Arena Trading Bot — USER MANUAL

> **Historical manual snapshot:** this manual identifies Arena 3.0.4 as current. That release identity is stale relative to the AXIOM 3.2.0 source line; use [`AXIOM_PRODUCT_MANUAL.md`](AXIOM_PRODUCT_MANUAL.md) for a qualified product overview and [`AXIOM_CURRENT_AUDIT_REPORT.md`](AXIOM_CURRENT_AUDIT_REPORT.md) for evidence.


**Release: Arena 3.0.4 (current build in this repository)**
Audience: human operator / developer running Arena locally.

> **Safety headline.** Arena is a quantitative **research, paper and sandbox**
> platform by default. `MODE=PAPER` / `LIVE_TRADING=false` / `BROKER=LOCAL_PAPER`
> are the shipped defaults, and no single variable can change that: real-money
> routing requires **four independent switches** (`MODE=LIVE`, `LIVE_TRADING=true`,
> `BROKER=UPSTOX_LIVE`, `LIVE_CONFIRM=I-ACCEPT-REAL-MONEY-RISK`) **plus** all four
> hard risk limits set to non-zero values. Any missing switch aborts startup with
> a message naming what is missing, and the bare alias `BROKER=LIVE` still raises.
> The sandbox broker refuses the live order host (`api-hft.upstox.com`), so a live
> token pasted into `UPSTOX_SANDBOX_ACCESS_TOKEN` cannot place a real order.
> **V1 correction:** earlier builds shipped the live order host as the *sandbox
> default*, which could route real orders while every label said "sandbox"; that
> is fixed. Read [LIVE_TRADING_ENABLEMENT.md](LIVE_TRADING_ENABLEMENT.md) and run
> `python -m app live-preflight` (read-only) before enabling live mode.

---

## 1. WHAT ARENA IS

**Purpose.** Arena ingests real Indian market *data* (read-only), runs
strategies over it, manages a research pipeline (experiments → validation →
walk-forward → locked-test → lifecycle → router), executes the selected
strategy through a hard risk gate into a simulated local paper broker or the
Upstox sandbox broker, reconciles fills, attributes P&L to strategies, and
records every decision durably for replay and reporting.

**High-level architecture** (all under `app/`, SQLite-backed):

- **Data layer** — `app/data/` (Upstox Market Data Feed V3 WebSocket client
  with reconnect/re-auth/resubscribe, historical V3 downloader), `app/history/`
  (candle store: csv.gz, parquet if pyarrow present), `app/universe/`
  (instrument master sync/screening).
- **Research layer** — `app/backtesting/` (cost-aware backtest engine),
  `app/research/` (discovery engine, opportunity scanner, datasets, replay,
  Command Center), `app/learning/` (experiments, evaluator, strategy router,
  lifecycle, calibration, portfolio allocator, journal), `app/validation/`
  (chronological splits, walk-forward, robustness).
- **Execution layer** — `app/engine/` (`SandboxEngine`: event-driven decision
  loop, sizing-before-risk, protective exits, daily risk accounting, intent
  journal, recovery; `events.py`: EventDrivenDecisionEngine + PauseGate;
  `adaptive.py`: RouterStrategySource), `app/brokers/sandbox.py`
  (documented Upstox V3 sandbox order APIs), `app/risk/limits.py`
  (`HardRiskLimits`, `OrderIntent`), `app/paper_trading/` (local paper broker
  + simulator).
- **Interface layer** — CLI (`python -m app`), FastAPI dashboard/API
  (`app/api/`), PySide6 desktop viewer (`desktop/`), reports (`app/reporting/`).
- **Operations** — `app/database/` (SQLite schema v1–4, migrations, repository,
  recovery manager), `app/monitoring/`, `app/maintenance/`, `app/logging/`.

**PAPER vs UPSTOX SANDBOX vs LIVE**

| Mode | Broker used | Real network orders | Enabled? |
|---|---|---|---|
| `BROKER=LOCAL_PAPER` (default) | local simulated `PaperBroker` | never | **enabled**, default |
| `BROKER=UPSTOX_SANDBOX` | `UpstoxSandboxBroker` → documented sandbox REST V3 | sandbox-only, no real money | **enabled** with your sandbox token |
| `BROKER=UPSTOX_LIVE` | `UpstoxLiveBroker` → documented live order host | **REAL money** | **implemented, OFF unless all four switches + non-zero risk limits** |
| `BROKER=LIVE` | none | — | rejected: the bare alias is not a mode |

Market *data* (read-only Upstox feed, `DATA_SOURCE=upstox`) is a separate
concern from order routing; even with real data, `LOCAL_PAPER` never sends
orders anywhere.

---

## 2. INSTALLATION

**Required Python:** CPython **3.11+** (tested on 3.13; the lock file pins
CPython 3.13.14 on Linux). SQLite is the only database.

**Windows / Linux / macOS setup (exact commands)**

```bash
# 1. get the code (or unzip the release archive) and enter it
cd trading_bot

# 2. create a virtual environment
python -m venv .venv
# Windows:            py -m venv .venv

# 3. activate it
source .venv/bin/activate        # Linux/macOS
# Windows:            .venv\Scripts\activate

# 4. install the package + dependencies
pip install -e .                 # OR: pip install -r requirements.txt
# reproducible pins (tested set): pip install -r requirements-lock.txt

# 5. (optional desktop viewer, separate install)
pip install -r requirements-desktop.txt   # PySide6; core never needs this
```

`pip install -e .` works out of the box: the core declares **no mandatory
runtime dependencies**; `requirements.txt` adds the optional subsystems
(fastapi, pydantic, uvicorn, httpx, websockets, protobuf, python-dotenv,
pytest, starlette, tzdata on Windows). Without the extras, the offline core
(`paper-test`, research over stored CSV, CLI) still runs; FastAPI-based
dashboard and Upstox streaming require the extras.

**Verify the installation**

```bash
python -m app --version     # → Arena 3.0.4
python -m app --help        # → command list + banner
python -m app paper-test    # offline self-test; must end with "ok": true
```

---

## 3. PROJECT STRUCTURE

```
trading_bot/
├─ app/                    # all application code (modular, small files)
│  ├─ __main__.py          # CLI entry point (python -m app …)
│  ├─ main.py              # ASGI factory (uvicorn app.main:create_app --factory)
│  ├─ api/                 # FastAPI server + routes (health, dashboard, paper,
│  │                       #  market, strategies, backtests, v2)
│  ├─ brokers/sandbox.py   # Upstox SANDBOX broker (V3 order APIs, tag lookup)
│  ├─ backtesting/         # cost-aware backtest engine
│  ├─ config/settings.py   # Settings dataclass (env + .env), safety validation
│  ├─ data/                # Upstox market-data feed, auth, models, cache
│  ├─ database/            # SQLite schema v1–4, migrations, repository, recovery
│  ├─ engine/              # SandboxEngine, events, adaptive router source, state
│  ├─ learning/            # experiments, evaluator, router, lifecycle, …
│  ├─ maintenance/ monitoring/ paper_trading/ reporting/ research/ risk/
│  ├─ strategies/          # 7 strategies + registry (create_strategy)
│  ├─ universe/ validation/ history/ logging/ indicators/ tournament/
│  └─ bootstrap.py         # idempotent `initialize`
├─ desktop/                # PySide6 desktop viewer (UI only)
├─ tests/                  # 491 pytest tests (incl. 3.0.1/3.0.2/3.0.3/3.0.4 batteries)
├─ data/                   # candles, universe cache, datasets, sample CSVs
│  ├─ history/  universe/  datasets/   # created by `initialize`
│  └─ sample.csv, sample_1h.csv, …    # shipped offline samples
├─ logs/bot.jsonl          # JSONL log (created on first run)
├─ results/ backups/       # research artifacts, DB backups
├─ trading_bot.db          # SQLite database (created by `initialize`)
├─ .env                    # YOUR configuration (gitignored; from .env.example)
├─ .env.example            # documented template of every variable
├─ run.py                  # portable runner: paper/backtest/api modes
├─ requirements*.txt       # dependency sets
└─ ARENA_FINAL_VERIFICATION.md  # current verification report (3.0.4)
```

**Entry points:** `python -m app <command>` (CLI), `python run.py --mode api`
or `python -m uvicorn app.main:create_app --factory` (API/dashboard),
`python -m desktop` (desktop viewer).

**Configuration:** `.env` (loaded by `Settings.from_env()`; env vars always
win over `.env`). **Data/database:** `DATABASE_URL` (default
`sqlite:///trading_bot.db` in the project root), `DATA_DIR` (default `data`).
**Logs:** `logs/bot.jsonl`.

---

## 4. CLI COMMAND REFERENCE

Everything below is generated from the actual argparse definition in
`app/__main__.py` (verified against `--help` for every command).

### `python -m app --version` / `--help`
Prints `Arena 3.0.4` / the command list. Safe. Offline. No credentials.

### `initialize`
```bash
python -m app initialize
```
- Idempotent first-run setup: creates `data/{history,universe,datasets}`,
  `logs/`, `results/`; creates/migrates the SQLite DB (schema v1–4); records
  the `initialized` engine_state; runs one universe sync (network unless the
  universe cache already covers it).
- Orders: **no** · Credentials: **no** · Network: universe master download
  (official Upstox asset URL; it degrades gracefully offline on re-runs).
- Example: `python -m app initialize` → JSON `{"ok": true, …}`; safe to re-run.

### `paper-test`
```bash
python -m app paper-test [--database sqlite:///path.db]
```
- Offline deterministic paper-pipeline self-test (simulated feed → strategy →
  risk → local paper fills → restart equality). Zero credentials, zero
  network. Prints a JSON summary ending in `"ok": true`; asserts the exact
  expected equity invariant (99637.58463385397).
- Orders: **no** (local simulated) · Credentials: no · Network: **no**.

### `upstox-login`
```bash
python -m app upstox-login
```
- Official Upstox OAuth authorization-code helper for **market data**; opens
  the login URL, you paste the redirected URL/code, the access token is stored
  in your gitignored `.env`. Requires `UPSTOX_CLIENT_ID`, `UPSTOX_CLIENT_SECRET`,
  `UPSTOX_REDIRECT_URI` in the environment.
- Orders: no · Credentials: **creates** one (your account) · Network: **yes**.

### `upstox-test`
```bash
python -m app upstox-test [--timeout SECONDS]
```
- Behavioral credential verification for the **market data feed** (documented
  authorize probe) and, if verified, one WebSocket connection attempt waiting
  up to `--timeout` for a first completed candle. Read-only.
- Orders: **no** · Credentials: **yes** (`MARKET_DATA_ACCESS_TOKEN` /
  `UPSTOX_ACCESS_TOKEN`) · Network: **yes**.
- Output JSON: `credential_check`, `authenticated`, `websocket`, `frames`,
  `market_status`. Exit 0 only when `websocket == "connected"`.

### `market-stream`
```bash
python -m app market-stream --duration 30
```
- Streams normalized completed candles + market-status transitions to stdout.
- Orders: no · Credentials: yes (data token) · Network: yes.

### `paper-live`
```bash
python -m app paper-live [run.py options…]
```
- Runs the V0.6 portable pipeline (`run.py --mode paper --source upstox …`)
  over the real Upstox data feed with everything executed in the local paper
  broker. Extra args are passed through to `run.py` (e.g. `--symbol`,
  `--timeframe`, `--strategy`, `--parameters '{"period":5}'`).
- Orders: **no** (simulated) · Credentials: yes (data token) · Network: yes.

### `engine`
```bash
python -m app engine [--max-candles N]
```
- The 24/7 **sandbox execution engine**: refuses to start unless
  `BROKER=UPSTOX_SANDBOX` **and** `DATA_SOURCE=upstox`. Wires the real feed →
  event engine → router-selected strategy → sizing → hard risk → durable
  intents → sandbox orders → reconciliation → attribution, plus the
  maintenance scheduler. `--max-candles` bounds the run (tests/verification).
- Orders: **YES (sandbox only)** · Credentials: yes (data + sandbox tokens)
  · Network: yes.

### `sandbox-test`
```bash
python -m app sandbox-test --instrument NSE_EQ|INE002A01018
python -m app sandbox-test --instrument NSE_EQ|INE002A01018 --place-order --quantity 1
```
- Verifies the **sandbox** credential with the documented order-history probe
  (401/403 → rejected; 400/404 + `UDAPI100010` → accepted; anything else →
  reported as unexpected, fail-closed). Without `--place-order` it never
  submits anything. With `--place-order` it explicitly submits ONE sandbox
  market order at `--quantity` (default 1) and reconciles it.
- Orders: **no by default; 1 sandbox order with the explicit flag** ·
  Credentials: yes (`UPSTOX_SANDBOX_ACCESS_TOKEN`) · Network: yes.

### `market-universe`
```bash
python -m app market-universe {sync|status|audit}
```
- `sync`: download the official instrument master into the local cache/
  database. `status`: summarize what is stored. `audit`: consistency checks.
- Orders: no · Credentials: no · Network: only `sync`.

### `history`
```bash
python -m app history sync --instrument NSE_EQ|INE002A01018 \
    --interval minutes:1 --from-date 2026-09-01 --to-date 2026-09-20
python -m app history status [--instrument KEY]
python -m app history audit --instrument NSE_EQ|INE002A01018 --interval minutes:1
```
- Downloads/summarizes/audits historical candles (V3 endpoints; resumable,
  chunked, idempotent). `sync` requires `--instrument`.
- Orders: no · Credentials: no (public historical endpoints) · Network: sync/audit.

### `dataset`
```bash
python -m app dataset build --name rel-1m --instruments NSE_EQ|INE002A01018 \
    --unit minutes --interval 1 --from-date 2026-09-01 --to-date 2026-09-20
python -m app dataset audit --dataset-id <id>
```
- Builds versioned research datasets (with timestamps + hashes) from the
  local candle store; `audit` reports completeness. Offline (uses stored data).

### `universe`
```bash
python -m app universe screen [--exchange NSE_EQ] [--segment ...] [--types EQ]
    [--min-history 100] [--min-price 10] [--max-price 5000] [--min-volume 100000]
    [--unit minutes] [--interval 1] [--limit 20]
```
- Screens the stored universe for eligible research instruments. Offline.

### `research`
```bash
python -m app research run --name exp1 --instruments NSE_EQ|INE002A01018 \
    --unit minutes --interval 1 --from-date 2026-09-01 --to-date 2026-09-20 \
    --timeframe 1m --periods 2,3,5
python -m app research status
```
- Runs a momentum parameter grid through the experiment engine
  (TRAIN → VALIDATION → one-shot locked test) and stores scored candidates;
  `status` lists recent experiments + promoted candidates. Offline.

### `discover`
```bash
python -m app discover --name d1 --instruments NSE_EQ|INE002A01018 \
    --unit minutes --interval 1 --from-date 2026-09-01 --to-date 2026-09-20 \
    --timeframe 1m
```
- Controlled strategy-discovery run (spec generation → OOS-gated scoring).
  Requires ≥60 local candles. Offline.

### `scan`
```bash
python -m app scan --instruments NSE_EQ|INE002A01018 --unit minutes --interval 1
```
- OpportunityScanner over stored candles (research output only; trading still
  requires the hard risk gate). Offline.

### `lifecycle`
```bash
python -m app lifecycle status
python -m app lifecycle promote --lineage <lineage_id>
python -m app lifecycle degrade --lineage <lineage_id>
```
- Shows every lineage's stage/status/tradability; promotes (next ladder
  stage) or degrades (PROBATION→SHADOW→RETIRED) a lineage explicitly.
  **Trading additionally requires the full ladder + ACTIVE status — see §10.**

### `replay`
```bash
python -m app replay --signal <signal_id>
```
- Reconstructs one recorded decision from durable rows using ONLY
  at-decision-time information (no lookahead). Offline.

### `report`
```bash
python -m app report --format markdown           # prints to stdout
python -m app report --format pdf --output arena-weekly.pdf
```
- Weekly report assembled from recorded data, every number labeled
  OBSERVED / CALCULATED / MODEL INTERPRETATION. Offline.

### `ask`
```bash
python -m app ask "how many orders last week?"
```
- Command Center: answers natural-language questions **only from stored
  rows**; unknown questions are answered honestly, never invented. Offline.

### `monitor`
```bash
python -m app monitor
```
- Self-monitoring snapshot (load, memory, disk, database quick_check, data
  staleness). Exit 1 if any alert fires. Offline.

### `maintenance`
```bash
python -m app maintenance
```
- Runs due maintenance jobs once (universe sync, coverage refresh, dataset
  refresh, backups). Network: possibly (universe/dataset jobs).

### `backup`
```bash
python -m app backup backup [--label pre-sandbox-test] [--backup-dir DIR]
python -m app backup verify --path backups/<file>.db
python -m app backup restore --path backups/<file>.db
```
- SQLite backup/verify/restore. Offline.

**Command that can place orders:** `engine` (sandbox), `sandbox-test` (only
with explicit `--place-order`). Nothing else touches order APIs.

---

## 5. DASHBOARD / MONITORING

Arena's dashboard is **web-based** (FastAPI + a built-in HTML page). The
**desktop app is a separate optional viewer** (§16), not a replacement.

**Start the API + dashboard** (requires the FastAPI extras):

```bash
python run.py --mode api --host 127.0.0.1 --port 8000
# equivalent: python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Set `API_TOKEN` in `.env` first. `/health`, `/` and — for **local
(loopback) browsers only** — the `/dashboard` HTML page load without a
token (the page contains no secrets; it asks you for the token and sends it
as an `Authorization: Bearer` header on its API calls). Every other route —
including `/dashboard/data` and the engine STOP/RESUME controls — returns
**503** without a configured token and **401** on a wrong
`Authorization: Bearer <token>` header.

**URLs** (all verified against the routers):

| URL | What it is |
|---|---|
| `/health` | public health probe (no auth): `{"status":"ok","version":"3.0.4","mode":"PAPER","live_trading":false,"real_order_routing":"NOT_IMPLEMENTED"}` |
| `/` | public landing page (HTML; account data stays behind auth) |
| `/dashboard` | **the HTML dashboard page** |
| `/dashboard/data` | JSON powering the dashboard panels |
| `/engine/stop` (POST) | emergency STOP (see §14) |
| `/engine/resume` (POST) | clear the persisted STOP flag |
| `/paper/status` `/paper/account` `/paper/positions` `/paper/orders?limit=` `/paper/trades?limit=` | local paper broker views |
| `/paper/start` `/paper/stop` `/paper/pause` `/paper/resume` (POST) | control the embedded paper simulator |
| `/market/status` `/market/datasets` | data-platform status |
| `/strategies` | strategy catalog |
| `/backtests` `/backtests/{id}` `/backtests/run` (POST) `/tournaments` | research records |
| `/api/v2/overview` | aggregate overview (mode, live_trading, emergency_stop, equity, P&L, counts) |
| `/api/v2/trades` `/api/v2/performance` `/api/v2/strategies` `/api/v2/learning` `/api/v2/experiments` `/api/v2/market_data` `/api/v2/risk` `/api/v2/logs` | v2 JSON views |
| `/api/v2/portfolio` `/api/v2/attribution` `/api/v2/research_lab` `/api/v2/learning_detail` `/api/v2/market_intelligence` | FINAL-era views |
| `/api/v2/command_center?q=…` | ask questions over stored data |
| `/api/v2/report/weekly?format=markdown\|pdf` `/api/v2/report?period=weekly` | reports |
| `/api/v2/charts/equity.svg` `/api/v2/charts/drawdown.svg` `/api/v2/charts/attribution.svg` | generated SVG charts |
| `/docs` | interactive OpenAPI UI (lists every route with schemas) |

**Dashboard panels** (`/dashboard` page sections): **Account** (capital,
equity, cash), **Feed / Market** (connection status, authenticated,
market_status, candles), **Strategy & Signals** (active strategy, last
signals), **Positions & Sandbox Orders** (open position, recent orders —
local paper and sandbox), **Learning / Model** (active strategy state,
promotions), **Recent Events** (durable system events). The v2
`/api/v2/risk` view exposes hard-limit configuration; drawdown is visible in
`/api/v2/performance` and the equity/drawdown SVG charts. Emergency
STOP/RESUME buttons are on the dashboard page and in the desktop app.

**Monitor** — feed: dashboard Feed panel or `/api/v2/market_data`;
strategies/router: `/api/v2/strategies`, `/api/v2/learning`, Learning panel;
positions/orders/P&L: Account + Positions panels or `/paper/*`;
drawdown/risk: `/api/v2/performance`, `/api/v2/risk`, charts; events:
Recent Events panel; model state: `/api/v2/learning_detail`.

**Shut down safely:** stop the engine with the dashboard STOP (or
`POST /engine/stop`), then Ctrl-C the server. On KeyboardInterrupt
the engine runs one final `reconcile()`; uvicorn closes the database in the
app lifespan. If the engine was run standalone via `python -m app engine`,
Ctrl-C does the same final reconcile.

---

## 6. CONFIGURATION

All variables are read from the environment and/or a gitignored `.env`
(loaded via python-dotenv; exported env vars override `.env`). Full
documented template: `.env.example`. Validation lives in
`app/config/settings.py::Settings.__post_init__`.

**Identity & safety (do not change for sandbox testing):**

| Variable | Default | Notes |
|---|---|---|
| `MODE` | `PAPER` | **only** `PAPER` accepted; anything else refuses to boot |
| `LIVE_TRADING` | `false` | **only** `false` accepted |
| `BROKER` | `LOCAL_PAPER` | `LOCAL_PAPER` or `UPSTOX_SANDBOX`; `LIVE` raises |
| `DATABASE_URL` | `sqlite:///trading_bot.db` | SQLite only |

**Execution / cost model:**
`INITIAL_CAPITAL=100000`, `CURRENCY=INR`, `COMMISSION=0.0005`,
`SLIPPAGE=0.0005`, `SPREAD=0.0002`, `POSITION_SIZE=0.25`,
`STOP_LOSS=0.05`, `TAKE_PROFIT=0.10` (set `NONE` to disable a protective
exit), `MAX_PARTICIPATION=0.10`, `DEFAULT_SYMBOL=DEMO`,
`DEFAULT_TIMEFRAME=1D`, `MARKET_CALENDAR=continuous|nse-session`.

**Hard risk limits** (learning can never change these; `0` = unlimited —
consider setting real values before any sandbox run):
`MAX_POSITION_VALUE=0`, `MAX_OPEN_POSITIONS=0`, `MAX_DAILY_LOSS=0`,
`MAX_TRAGES_PER_DAY=0` *(typo guard: it is `MAX_TRADES_PER_DAY`)*.

**Market data (read-only feed):**
`DATA_SOURCE=replay|binance|generic|upstox` (engine requires `upstox`),
`MARKET_DATA_PROVIDER=upstox`, `MARKET_DATA_SYMBOLS` (comma-separated
instrument keys, e.g. `NSE_EQ|INE002A01018`; validated by regex),
`MARKET_DATA_TIMEFRAME` (must equal `DEFAULT_TIMEFRAME`; engine uses
`1m`), `MARKET_DATA_MODE=ltpc|full`, `MARKET_DATA_ACCESS_TOKEN`
(alias `UPSTOX_ACCESS_TOKEN`), `MARKET_DATA_AUTHORIZE_URL`,
`MARKET_DATA_TOKEN`, `DATA_WS_URL` (generic WSS, optional).

**Upstox sandbox (order APIs):**
`UPSTOX_SANDBOX_ACCESS_TOKEN` (required iff `BROKER=UPSTOX_SANDBOX`),
`UPSTOX_SANDBOX_BASE_URL=https://api-sandbox.upstox.com`,
`UPSTOX_SANDBOX_ORDER_HISTORY_URL=https://api-sandbox.upstox.com/v2/order/history`
(a production `api.upstox.com` history URL is **refused** for the sandbox
broker — a sandbox token is never sent to a production host; order history is
also not a documented sandbox capability, so the broker reports an explicit
unavailable-capability state instead of guessing),
`SANDBOX_PRODUCT=I|D|MTF`, `SANDBOX_ORDER_TAG=arena-v1` (retained setting,
passed to the broker; since 3.0.2 every ORDER carries the intent-derived
12-char tag — this setting no longer overrides order tags).

**OAuth login helper:** `UPSTOX_CLIENT_ID`, `UPSTOX_CLIENT_SECRET`,
`UPSTOX_REDIRECT_URI`.

**Data platform:** `DATA_DIR=data`,
`UNIVERSE_SYNC_URL` (official complete.json.gz),
`HISTORY_BASE_URL`, `INTRADAY_BASE_URL` (V3 historical endpoints),
`PARQUET_ENABLED=true` (auto-fallback to csv.gz without pyarrow),
`UPSTOX_STREAMING_UNIVERSE` (explicit keys only — subscribing to the whole
universe is refused by design), `MAINTENANCE_HOUR=17` (UTC),
`MAX_DOWNLOAD_RETRIES=3`, `RESEARCH_BATCH_SIZE=30`.

**Research/portfolio/event knobs:** `MAX_STRATEGY_ALLOCATION=0.35` (hard cap,
not learning-editable), `EVENT_MOVE_THRESHOLD=0.003`,
`EVENT_VOLUME_SPIKE=2.0`, `MARKET_TIMEZONE=Asia/Kolkata` (trading-day
boundary for daily risk accounting; present in code, **not yet documented in
.env.example** — known documentation gap), `SCANNER_TOP_K=5`,
`DISCOVERY_MAX_CANDIDATES=12`, `LOG_LEVEL=INFO`, `API_TOKEN=` (required for
all protected API routes).

**Example minimal `.env` for offline use:** none needed — defaults work.
**Example for sandbox execution:**

```env
BROKER=UPSTOX_SANDBOX
UPSTOX_SANDBOX_ACCESS_TOKEN=<your sandbox token>
DATA_SOURCE=upstox
MARKET_DATA_ACCESS_TOKEN=<your data token>
MARKET_DATA_SYMBOLS=NSE_EQ|INE002A01018
MARKET_DATA_TIMEFRAME=1m
DEFAULT_TIMEFRAME=1m
MAX_POSITION_VALUE=50000
MAX_OPEN_POSITIONS=1
MAX_DAILY_LOSS=2000
MAX_TRADES_PER_DAY=10
API_TOKEN=<random secret>
```

**MUST NOT change for sandbox testing:** `MODE`, `LIVE_TRADING`, `BROKER`
(never `LIVE`; it would be rejected anyway), `DATABASE_URL` scheme.

---

## 7. UPSTOX SANDBOX

**Workflow:** get a sandbox token from your own Upstox developer account
(Developer Apps → Sandbox) → put it in `.env` as
`UPSTOX_SANDBOX_ACCESS_TOKEN` → set `BROKER=UPSTOX_SANDBOX` → probe with
`sandbox-test` → run the engine with `engine` (or one explicit order via
`sandbox-test --place-order --quantity 1`).

**Credential validation (implemented as documented; not externally verified —
needs a real sandbox token):** `sandbox-test` sends the documented
`GET /order/history?order_id=0` probe. 401/403 → rejected, fail closed.
HTTP 400/404 with error code **UDAPI100010** (order not found) → credential
accepted, nothing placed. Any other status/body (429, 5xx, malformed,
unrelated error codes) → reported `unexpected`, **not** classified as
verified.

**How orders are placed:** every order is an `OrderIntent` with an explicit
`instrument_key`; the broker POSTs to the documented V3 endpoint
(`https://api-hft.upstox.com/v3/order/place`) with body fields including
`quantity` (integer), `product` (`I`/`D`/`MTF`), `validity=DAY`,
`order_type`, `transaction_type`, `instrument_token`, and a ≤20-char `tag`
(the intent-derived durable reference; `slice=true` so large orders are
auto-split by exchange freeze limits). The response's `data.order_ids`
become the tracked remote ids. 429/5xx/network failure → intent marked
**UNKNOWN**, never re-POSTed blindly (§13).

**Reconciliation:** `reconcile()` pulls the documented order history per
remote id, maps statuses (complete→FILLED, reject→REJECTED, cancel→
CANCELLED, open→OPEN/PARTIALLY_FILLED) and applies fill deltas exactly once
(idempotent transitions, restart-safe).

**Partial fills:** deltas are credited incrementally; a partial-then-
cancelled order still credits exactly the executed quantity once.

**Cancellation:** DELETE per child order; only broker-confirmed cancellations
make the logical order CANCELLED (§13/§11).

**Restart recovery:** on startup Arena loads durable ledger state from
recorded transitions (cash, position, avg entry, realized, fees), replays
order records, resolves unresolved intents via the tag lookup, and only then
trades.

**Verify an order was actually accepted:** the durable journal
(`INTENT_PERSISTED` → `ACKNOWLEDGED` events; `/dashboard/data` events
panel), the sandbox transition rows (`list_sandbox_orders`), and
`reconcile()` fills. There is **no** "assume accepted" path.

**Requires real sandbox access:** everything in this section beyond the code
(contract verified by the 404-test battery) — the sandbox flows are
**implemented, not externally verified** with a real Upstox sandbox token.

---

## 8. MARKET DATA

**Instruments:** `market-universe sync` downloads the official instrument
master (complete.json.gz) into `data/universe/` + DB; `universe screen`
filters eligible instruments by exchange/segment/type/price/volume/history.

**Historical:** `history sync --instrument KEY --interval minutes:1 …`
(chunked, resumable, idempotent; stored under `data/history/` as csv.gz or
parquet). `history status` / `history audit` report coverage and integrity.

**Live feed:** `DATA_SOURCE=upstox` + `MARKET_DATA_*` config. The provider
is the documented Upstox **Market Data Feed V3** WebSocket
(`upstox-test` verifies the credential behaviorally first; the feed protocol
is decoded with the official protobuf bindings bundled in
`app/data/upstox_proto`). `market-stream --duration 30` prints normalized
completed candles; `paper-live` runs the paper pipeline on the live feed;
`engine` runs sandbox execution on it.

**WebSocket behavior:** connects only after the authorize probe passes;
emits **completed candles only** (in-progress bars are never traded);
deboundounced decisions (event cadence) — no per-tick trading.

**Reconnect:** bounded exponential backoff (cap 60 s) with fresh
authorization and **resubscription**; reconnect counters exposed in
`/market/status` metrics (`connection_status`, `reconnects`, `stale_feed`,
`data_gap_detected`). Tested against fakes; **real-socket behavior
implemented, not externally verified**.

**Market-status gating:** the engine checks `provider.metrics()` before
every candle: market closed → `MARKET_CLOSED_SKIP` (plus one reconcile pass),
stale feed → `STALE_FEED_SIGNALS_PAUSED` (reconcile continues, no signals).

**If data is unavailable:** research commands over stored candles keep
working; the live engine skips/pauses as above and exits the stream loop
gracefully when the feed ends.

---

## 9. TRADING MODES

**LOCAL PAPER (default).** Enable: nothing to do (default `BROKER`).
Verify: `/health` → `"mode":"PAPER","live_trading":false`;
`python -m app paper-test`. Broker: in-process `PaperBroker` — nothing leaves
the machine; real money **cannot** move. This is also the mode behind
`paper-live` (real data, simulated execution).

**UPSTOX SANDBOX.** Enable: `.env` with `BROKER=UPSTOX_SANDBOX` +
`UPSTOX_SANDBOX_ACCESS_TOKEN` (validation refuses the broker without a
token). Verify: `python -m app sandbox-test --instrument …` →
`"credential_accepted": true`; `/health` reports `SANDBOX_ORDERS_ONLY` while
`MODE` stays `PAPER` (mode and broker are independent). Broker:
`UpstoxSandboxBroker` → documented sandbox order APIs
(`api-sandbox.upstox.com` by default; the live order host is refused).
Real money: **no**. Safety: every order passes the same hard risk gate, intent
journal, and identity checks; orders are real HTTP calls to the sandbox only.

**UPSTOX LIVE (real money).** Enable — all five conditions at once:

```ini
MODE=LIVE
LIVE_TRADING=true
BROKER=UPSTOX_LIVE
LIVE_CONFIRM=I-ACCEPT-REAL-MONEY-RISK
UPSTOX_LIVE_ACCESS_TOKEN=<live token>
MAX_POSITION_VALUE=10000   # and MAX_OPEN_POSITIONS / MAX_DAILY_LOSS /
MAX_TRADES_PER_DAY=5       # MAX_TRADES_PER_DAY — 0 is refused in live mode
```

Verify (read-only, places no order):

```bash
python -m app live-preflight          # prints hosts, locks, limits, credential verdict
python -m app engine                  # starts only after Settings accepted the opt-in
```

Broker: `UpstoxLiveBroker` → documented live order host
(`api-hft.upstox.com`), writing to the separate `live_orders` /
`live_transitions` audit tables. `/health` then reports `mode: "LIVE"`,
`real_order_routing: "LIVE_REAL_MONEY"` and a real-money warning; the
dashboard and weekly report say so too. On a genuine LIVE host the engine
still enforces the hard risk gate, intent dedup, emergency stop and
reconciliation. Regulatory requirements (static IP, algo registration) apply —
see LIVE_TRADING_ENABLEMENT.md §7.

---

## 10. STRATEGIES AND LEARNING

**Available strategies** (`app/strategies/`, via `create_strategy(name,
parameters)`): `moving_average`, `momentum`, `mean_reversion`, `breakout`,
`rsi`, `macd`, `bollinger`. `GET /strategies` lists them with default
parameters.

**Discovery & experiments:** `discover` (spec generation → OOS-gated
qualification) and `research run` (momentum grid) run the documented
protocol: **TRAIN → VALIDATION → SELECTION/PROMOTION → LOCKED TEST (once,
report-only) → REPORT**. Selection metrics use the **validation** leg only;
robustness (parameter/cost stability) and walk-forward are separate gates;
every experiment/candidate/dataset row is versioned with timestamps/hashes.

**Regime detection:** `RegimeEngine` + `classify_regime` derive the market
regime (e.g. `trend_up_low_vol`) from past-only features; regime labels carry
no guarantee (documented disclaimer).

**Router:** `StrategyRouter` scores candidates by out-of-sample expectancy,
robustness and calibrated confidence; hard filters reject negative/insufficient
OOS and excessive drawdown; in-sample return is never scored. Selection is
persisted as `ROUTER_DECISION` learning events; accepted selections swap the
engine's active strategy and are recorded as versioned `STRATEGY_SWAPPED`
events. Degraded strategies are penalized (PROBATION) or hard-excluded
(SHADOW/RETIRED).

**Candidate lifecycle:** lineages move through
`RESEARCH → VALIDATION → WALK_FORWARD → SHADOW → CANARY → APPROVED` stages
with health `ACTIVE/PROBATION/SHADOW/RETIRED`. `lifecycle status` shows
stage/status/tradable; `lifecycle promote/degrade --lineage …` move a
lineage explicitly (children can never trade on a parent's success;
RETIRED history is kept, never resurrected silently).

**Final execution boundary (3.0.2):** a candidate is tradable only with the
**complete ladder recorded AND current status ACTIVE**. `promoted=True`
alone is never enough.

**Shadow strategies:** `ShadowStrategyEngine` tracks non-traded strategies
for evidence; `CAPITAL ALLOCATION`: `CapitalAllocator` weights by OOS
evidence × robustness × calibration under the `MAX_STRATEGY_ALLOCATION`
hard cap; the resulting plan bounds entry sizing (verified end-to-end).

**Learning may change:** strategy selection, candidate parameters, model
versions (always through the evidence gates). **Learning may NEVER change:**
risk limits, position sizing rules, drawdown protection, STOP, auth,
execution mode, LIVE flags, exposure caps, kill switches (enforced by
separation + tests).

---

## 11. RISK CONTROLS

All of these sit **outside** any learning system and fail closed:

1. **Hard limits** (`HardRiskLimits.from_settings`): `MAX_POSITION_VALUE`
   (per-order notional cap), `MAX_OPEN_POSITIONS`, `MAX_DAILY_LOSS`,
   `MAX_TRADES_PER_DAY` (tz-aware daily accounting, `MARKET_TIMEZONE`
   boundary; resets each trading day, preserved across same-day restarts).
2. **Sizing before the gate:** intended quantity from
   `min(cash, equity×POSITION_SIZE, allocator allowance)` divided by the
   all-in price (costs included) — the **exact** gated quantity is what is
   submitted; oversize/exposure violations are rejected with `RISK_REJECT`.
3. **Portfolio exposure:** the allocator plan caps per-strategy capital
   before the risk gate; no bypass path exists.
4. **Duplicate-order prevention:** deterministic intent ids
   (`uuid5(strategy|symbol|timestamp|side)`), durable `processed_signals`,
   the submission journal (no resubmission from
   SUBMISSION_ATTEMPTED/UNKNOWN/… states — ever), entry-stacking guard, and
   no pyramiding while a position is open.
5. **Emergency STOP:** persisted flag checked before every submission;
   blocks entries AND exits; cancels working sandbox orders (§14).
6. **Pause gate:** operator/engine pause blocks BUY entries only
   (risk-reducing exits still pass); STOP blocks everything.
7. **Lifecycle restrictions:** entries require an eligible router selection
   (full ladder + ACTIVE); no qualified candidate ⇒ no entry.
8. **Instrument identity:** explicit `instrument_key` per order; missing
   mapping, mismatch, ambiguity → `RISK_REJECT:INSTRUMENT_IDENTITY_MISSING`
   or broker `ValueError`; no defaults, ever.
9. **Broker/authentication failures:** 401/403 → `SandboxAuthError`,
   fail closed; 429/5xx on submit → UNKNOWN + no blind retry; status GETs
   retried GET-only.
10. **Protective exits:** stop-loss (`STOP_LOSS`) / take-profit
    (`TAKE_PROFIT`) per candle against the recovered average entry,
    duplicate-exit guard, independent of strategy signals.
11. **Market gating:** closed market / stale feed → no new signals.
12. **Fail-closed philosophy:** every uncertain remote outcome resolves to a
    blocked or reconciled state, never to an optimistic default.

---

## 12. ORDER LIFECYCLE (actual implementation)

```
Market data (completed candle; market-open + fresh checks)
  → event engine (move/volume/volatility/regime/cadence triggers; debounce)
  → protective exits first (SL/TP vs recovered avg entry; dup guard)
  → regime classification → router selection (_maybe_swap_strategy)
  → lifecycle eligibility (full ladder + ACTIVE at the boundary)
  → sizing (_size_entry: min(cash, equity×fraction, allocator) ÷ all-in price)
  → HARD risk gate (HardRiskLimits.check on the exact OrderIntent → RISK_DECISION)
  → intent id (uuid5 V1 format) + durable journal INTENT_PERSISTED (+ tag)
      ↳ duplicates blocked here (journal + processed_signals + working-order guard)
  → DECISION event (full observability record)
  → broker.submit: identity resolution (fail-closed) → POST V3 (tag, slice)
      ↳ 200 → ACKNOWLEDGED (+ remote order ids)
      ↳ 4xx → REJECTED (final)
      ↳ 429/5xx/network → UNKNOWN (no blind re-POST)
  → reconciliation (GET history per remote id; or tag lookup for UNKNOWN)
  → fills applied as deltas exactly once (ledger: cash/position/realized)
  → daily risk accounting (trades/realized for the trading day)
  → TRADE_OUTCOME attribution (strategy/symbol/qty/pnl) on round trips
  → learning/reporting (rolling batches, weekly report, dashboard views)
```

Every step persists durable events (`INTENT_STATE`, `INTENT_PERSISTED`,
`DECISION`, `RISK_DECISION`, `RISK_REJECT`, `SANDBOX_FILL_RECONCILED`,
`TRADE_OUTCOME`, …) — a full decision is reconstructible with
`replay --signal <id>`.

---

## 13. RECOVERY

- **Restart recovery:** engine init = load durable ledger from recorded
  transitions → reregister open orders from DB records **without
  resubmitting** → resolve unresolved intents (tag lookup) → rebuild
  in-memory state → trade. Verified: identical cash/position/realized after
  restart; round-trip P&L matches the original.
- **Unknown remote orders:** submission accepted remotely but the response
  was lost → journal UNKNOWN → reconcile performs the documented tag lookup →
  exactly one strict attribute match is ADOPTED (fills applied once); zero
  matches → VERIFIED_ABSENCE (this intent never auto-resubmits); multiple →
  UNRESOLVED (fail closed). Unknown/Unresolved persist across restarts.
- **Duplicate prevention:** see §11.4 — the same intent can never POST twice.
- **Crash recovery:** any crash point (before POST, during POST, after remote
  accept, after response before persist, during reconcile, during restart) is
  covered by the durable journal state machine (regression-tested).
- **Reconnect recovery:** WebSocket reconnects with fresh auth + resubscribe
  (bounded backoff); reconnects counter exposed.
- **Disagreement broker vs local:** reconciliation and the remote are always
  authoritative for fills/status; unverifiable differences surface as
  UNRESOLVED/UNKNOWN and block further resubmission instead of guessing.
- **Database recovery:** `backup backup|verify|restore` (RecoveryManager),
  `monitor` runs an SQLite `quick_check`; schema migrations are versioned
  (v1–4) and idempotent.

---

## 14. EMERGENCY STOP

- **Dashboard/API:** `POST /engine/stop` (or the STOP button on
  `/dashboard` / in the desktop app) — records `EMERGENCY_STOP_REQUESTED`,
  sets the persisted `emergency_stop` engine_state, and if a sandbox engine
  is embedded in the same process also calls `engine.emergency_stop()` which
  cancels every working sandbox order (cancel failures are recorded, never
  hidden).
- **Standalone engine:** the flag lives in the database; the engine checks it
  **before every submission**, so a STOP from the dashboard on the same
  database halts order flow in a separately-running engine on its next
  decision. (Cancelling already-working remote orders from the dashboard
  requires the engine to be reachable; otherwise cancel them via the broker
  API or let the DAY validity lapse — documented honestly.)
- **What it stops:** ALL submissions — entries AND protective exits
  (`EXECUTION_STOPPED_SKIP`).
- **Existing positions:** NOT auto-closed (orders are cancelled; positions
  remain; close them deliberately after resuming).
- **Resume:** `POST /engine/resume`, the dashboard Resume button,
  or `engine.resume()` → clears the persisted flag; the engine returns to
  STOPPED→RUNNING on next run.
- **Persistence:** the flag survives restarts (verified by tests).

---

## 15. REPORTS

- `python -m app report --format markdown` — weekly report to stdout:
  mode banner, Account (OBSERVED/CALCULATED labels), Activity (sandbox
  orders, fills, trades, risk events), Strategy & regimes (regime changes,
  promotions), Data status (coverage, instruments).
- `python -m app report --format pdf --output arena-weekly.pdf` — same
  content as a PDF file (default name `arena-weekly-<date>.pdf`).
- `/api/v2/report/weekly?format=markdown|pdf` — the same report over HTTP.
- `/api/v2/performance`, `/api/v2/attribution`, equity/drawdown/attribution
  SVG charts — computed from recorded trades/snapshots.
- `research status`, `lifecycle status`, `dataset audit`, `monitor` —
  research/model/ops snapshots.
- Interpretation rule: every number is labeled **OBSERVED** (recorded),
  **CALCULATED** (derived), or **MODEL INTERPRETATION** — never a
  fabricated/guaranteed figure.

---

## 16. DESKTOP APPLICATION

- **Launch:** `python -m desktop` (from the repo root; needs
  `pip install -r requirements-desktop.txt` i.e. PySide6, plus a running
  backend: set `ARENA_BACKEND_URL` (default `http://127.0.0.1:8000`) and
  `ARENA_API_TOKEN`). Windows helper: `desktop/run_windows.bat`.
- **Screens:** overview/monitoring page (equity/drawdown charts, tables),
  Ask (Command Center query box), report loader; header **EMERGENCY STOP**
  and **Resume** buttons; an explicit notice that the app is a
  viewer + emergency stop only.
- **Status indicators:** mode, live-trading-disabled badge, emergency-stop
  state, equity/P&L, feed status — all fetched from the backend API; the
  desktop app cannot place orders or change risk.
- **Known limitations:** Qt could not be booted in the build sandbox — the
  desktop UI is **implemented, not externally verified**; Windows launcher
  provided as source. The backend it talks to is fully verified.
- There is no tray/daemon mode; closing the window detaches the viewer only
  (the backend keeps running).

---

## 17. TROUBLESHOOTING

| Symptom | Likely cause | Diagnostic | Safe fix |
|---|---|---|---|
| `No module named app` / wrong results | wrong directory / venv not active | `pwd` (repo root?); `which python` | `cd trading_bot && source .venv/bin/activate` |
| `ModuleNotFoundError: fastapi` (etc.) | optional extras missing | `pip show fastapi` | `pip install -r requirements.txt` |
| `Only MODE=PAPER and LIVE_TRADING=false are permitted` | env overrides | `env | grep -E "MODE|LIVE"` | remove the override |
| `BROKER must be LOCAL_PAPER or UPSTOX_SANDBOX` | invalid broker value | `grep BROKER .env` | set `LOCAL_PAPER` or `UPSTOX_SANDBOX` |
| `BROKER=UPSTOX_SANDBOX requires UPSTOX_SANDBOX_ACCESS_TOKEN` | token missing | `python -m app sandbox-test …` (exit 3) | add token to `.env` |
| sandbox probe `credential rejected (401/403)` | invalid/expired sandbox token | `python -m app sandbox-test --instrument …` | regenerate the sandbox token |
| `upstox-test` → `credential NOT verified for feed V3` | wrong/expired data token | same command; read `credential_check` | `python -m app upstox-login` |
| WebSocket `connected_no_completed_candle_in_window` | market closed / long candle timeframe | `/market/status` metrics | retry during market hours or increase `--timeout` |
| `STALE_FEED_SIGNALS_PAUSED` in logs | feed gone quiet | `python -m app monitor`; feed metrics | waits for fresh data; check connectivity |
| `RISK_REJECT:NO_QUALIFIED_CANDIDATE` | no lifecycle-eligible candidate | `python -m app lifecycle status` | approve a candidate through research/lifecycle (do NOT bypass) |
| `RISK_REJECT` with a limit reason | hard limit hit | the event payload in `/dashboard/data` | raise the limit deliberately or reduce size |
| `DUPLICATE_INTENT_BLOCKED` / `ENTRY_ORDER_STILL_WORKING` | same signal retried / entry already working | events panel | expected protection; wait for reconciliation |
| `REMOTE_LOOKUP_*` / `UNRESOLVED` warnings | submission outcome uncertain | `INTENT_STATE` events; journal state | let reconcile retry; never resubmit manually |
| `database is locked` | two processes on one SQLite file | `ps \| grep "python -m app"` | stop one process; SQLite is single-writer |
| `PRAGMA quick_check` not ok / corruption | disk/db damage | `python -m app backup verify --path …`; `monitor` | restore latest backup (`backup restore`) |
| `503 Set API_TOKEN…` from API | `API_TOKEN` empty | `curl -i http://127.0.0.1:8000/health` | set `API_TOKEN` in `.env`, restart server |
| `401 Invalid or missing bearer token` | wrong header | same | send `Authorization: Bearer $API_TOKEN` |
| `Address already in use` (port 8000) | server already running | `curl -s http://127.0.0.1:8000/health` | use `--port 8001` or stop the old process |
| `engine` exits with code 2 + "requires BROKER=UPSTOX_SANDBOX" | engine misconfig | `grep -E "BROKER|DATA_SOURCE" .env` | configure both `BROKER=UPSTOX_SANDBOX` and `DATA_SOURCE=upstox` |

---

## 18. SAFE FIRST-RUN PROCEDURE

Offline steps 1–5 need **nothing** but Python. Steps 6–13 need your **own**
Upstox *sandbox* token and are marked accordingly. **There is no
live-trading procedure: LIVE is not implemented and cannot be enabled.**

1. **Verify installation** — `python -m app --version` → `Arena 3.0.4`;
   `python -m app --help` lists 22 commands.
2. **Initialize** — `python -m app initialize` (idempotent; re-run shows
   what already existed). Expect `{"ok": true, …}`.
3. **Paper test** — `python -m app paper-test` → JSON ending `"ok": true`,
   equity exactly `99637.58463385397`.
4. **Launch dashboard** — set `API_TOKEN=<random>` in `.env`;
   `python run.py --mode api --host 127.0.0.1 --port 8000`.
5. **Inspect status** — browser: `http://127.0.0.1:8000/health`, then
   `/dashboard` (set a REST-client header `Authorization: Bearer $API_TOKEN`
   for the JSON routes). Confirm `"mode":"PAPER"`, `"live_trading":false`,
   `"real_order_routing":"NOT_IMPLEMENTED"`.
6. **Configure Upstox Sandbox** *(requires your sandbox account)* — in
   `.env`: `BROKER=UPSTOX_SANDBOX`, `UPSTOX_SANDBOX_ACCESS_TOKEN=<token>`,
   plus data config (`DATA_SOURCE=upstox`, `MARKET_DATA_ACCESS_TOKEN`,
   `MARKET_DATA_SYMBOLS=NSE_EQ|INE002A01018`, `MARKET_DATA_TIMEFRAME=1m`,
   `DEFAULT_TIMEFRAME=1m`) and sensible hard limits (e.g.
   `MAX_POSITION_VALUE=20000`, `MAX_OPEN_POSITIONS=1`,
   `MAX_TRADES_PER_DAY=10`, `MAX_DAILY_LOSS=1000`). Restart the server.
7. **Credential test** — `python -m app sandbox-test --instrument
   NSE_EQ|INE002A01018` → `"credential_accepted": true`, `order_placed` false.
8. **Market/data test** — `python -m app upstox-test --timeout 30` (feed
   probe) and/or `python -m app market-stream --duration 30`.
9. **ONE sandbox BUY** — either `python -m app sandbox-test --instrument
   NSE_EQ|INE002A01018 --place-order --quantity 1` (single explicit order) or
   start the engine (`python -m app engine --max-candles 200`) with an
   eligible candidate so it enters by itself. Quantity: 1 share.
10. **Verify acknowledgement** — events show
    `INTENT_PERSISTED → DECISION → RISK_DECISION → ACKNOWLEDGED`; sandbox-test
    prints `submitted_intent` + `fills_after_reconcile`.
11. **Reconcile** — `reconcile()` runs inside the engine loop / after
    sandbox-test automatically; confirm `SANDBOX_FILL_RECONCILED` and the
    ledger row (position = 1).
12. **Sandbox SELL** — flatten with a strategy exit/protective exit in the
    engine, or stop the engine and submit one explicit sandbox SELL through
    your own script against the same documented API (the CLI exposes the BUY
    probe only; there is no separate `sell` subcommand — this is the honest
    limitation).
13. **Verify flat** — ledger `position_quantity == 0`, realized P&L recorded
    in the report/dashboard.
14. **Inspect reports** — `python -m app report --format markdown`;
    dashboard panels; `python -m app monitor`.
15. **Restart Arena** — Ctrl-C, then start again (`engine` or `run.py`).
16. **Verify recovery** — startup logs show `LEDGER_RECOVERED` when a
    position/fill was replayed; equity/cash identical to pre-restart;
    `/dashboard/data` consistent. Run `python -m app backup backup --label
    first-run` when satisfied.

---

## 19. QUICK REFERENCE

| Task | Command | Safe/Unsafe | Network? | Credentials? | Orders? |
|---|---|---|---|---|---|
| Version check | `python -m app --version` | Safe | No | No | No |
| First-run setup | `python -m app initialize` | Safe | Universe sync | No | No |
| Offline self-test | `python -m app paper-test` | Safe | No | No | No (sim) |
| Weekly report | `python -m app report --format markdown` | Safe | No | No | No |
| Ask stored data | `python -m app ask "…"` | Safe | No | No | No |
| Health snapshot | `python -m app monitor` | Safe | No | No | No |
| DB backup | `python -m app backup backup` | Safe | No | No | No |
| Research run | `python -m app research run …` | Safe | No | No | No |
| Discovery | `python -m app discover …` | Safe | No | No | No |
| Opportunity scan | `python -m app scan …` | Safe | No | No | No |
| Lifecycle view | `python -m app lifecycle status` | Safe | No | No | No |
| Lifecycle mutate | `lifecycle promote/degrade --lineage …` | Mutating (research) | No | No | No |
| Replay decision | `python -m app replay --signal ID` | Safe | No | No | No |
| Universe sync | `python -m app market-universe sync` | Safe | Yes | No | No |
| History sync | `python -m app history sync …` | Safe | Yes | No | No |
| Data login | `python -m app upstox-login` | Safe | Yes | Creates token | No |
| Feed test | `python -m app upstox-test` | Safe (read-only) | Yes | Yes | No |
| Stream print | `python -m app market-stream --duration 30` | Safe (read-only) | Yes | Yes | No |
| Paper pipeline | `python -m app paper-live …` | Safe (sim orders) | Yes | Yes | No (sim) |
| Sandbox probe | `python -m app sandbox-test --instrument KEY` | Safe (read-only) | Yes | Yes | No |
| Sandbox order probe | `… sandbox-test … --place-order --quantity 1` | **ORDER (sandbox)** | Yes | Yes | **Yes** |
| 24/7 execution | `python -m app engine` | **ORDER (sandbox)** | Yes | Yes | **Yes** |
| Dashboard | `python run.py --mode api` | Safe (read-only UI) | Local | API_TOKEN | Via STOP/RESUME only |
| Desktop viewer | `python -m desktop` | Safe (viewer) | Local | API_TOKEN | No |

---

## 20. CURRENT VERIFICATION STATUS

Full detail: `ARENA_FINAL_VERIFICATION.md` (3.0.4) in the repo root.

- **Test suite:** 404 collected, **404 passed, 0 failed, 0 skipped**
  (Python 3.13 venv; ×2 consecutive runs + once from the extracted release
  zip). — VERIFIED.
- **External Upstox REST (order APIs):** NOT ATTEMPTED (no credentials).
  Sandbox flows implemented per current published docs and tested against
  fakes only — **implemented, not externally verified**.
- **External Upstox WebSocket (market data feed):** NOT ATTEMPTED —
  implemented, not externally verified (credential probe, reconnect,
  resubscribe tested against fakes).
- **Sandbox order placement end-to-end with a real token:** NOT ATTEMPTED.
- **Live trading:** IMPLEMENTED (`UpstoxLiveBroker`) but **not externally
  verified** — no live token was used in this environment and no real order was
  ever placed by the test suite. The gate (four switches + non-zero limits),
  host classification, audit-table separation and truthful reporting are
  covered by unit tests; a real-money end-to-end run has NOT been attempted.
- **GUI verification:** desktop (PySide6) — NOT VERIFIED (sandbox has no
  display); web dashboard routes/panels — VERIFIED via the API test suite.
- **Termux:** NOT ATTEMPTED. **Python 3.11/3.12 specifically:** NOT ATTEMPTED
  (3.13 verified; `requires-python >= 3.11`).
- **Windows:** packaging includes a `.bat` launcher and tzdata pin; a real
  Windows run — NOT ATTEMPTED.
- **Known limitations:** optional runtime extras; single-writer SQLite; no
  guaranteed profits (regime similarity disclaimed); rolling evaluator cannot
  auto-promote without research evidence; desktop boot unverified.

---

## 21. FINAL SAFETY CHECKLIST (before ANY order-capable run)

Tick every line before `engine` or `sandbox-test --place-order`:

- [ ] `MODE=PAPER`, `LIVE_TRADING=false` in `.env` (and no shell overrides)
- [ ] `BROKER=UPSTOX_SANDBOX` exactly (never `LIVE` — it would be rejected)
- [ ] Sandbox token is from **your** developer sandbox account; data token
      verified by `upstox-test`; sandbox token verified by `sandbox-test`
- [ ] `MARKET_DATA_SYMBOLS` contains exactly the instrument(s) you intend;
      you know each `instrument_key` by heart
- [ ] Quantity intended is deliberate (`--quantity 1` for probes); sizing
      knob (`POSITION_SIZE`) and allocator plan understood
- [ ] Hard limits set to real values, not `0`/unlimited:
      `MAX_POSITION_VALUE`, `MAX_OPEN_POSITIONS`, `MAX_DAILY_LOSS`,
      `MAX_TRADES_PER_DAY`
- [ ] Emergency STOP path tested: you know `POST /engine/stop`
      and that the flag persists across restart
- [ ] Dashboard healthy: `/health` ok, feed panel connected, no active
      alerts (`python -m app monitor` exits 0)
- [ ] Reconciliation healthy: recent `SANDBOX_FILL_RECONCILED` events, no
      standing `UNRESOLVED`/`UNKNOWN` intents, positions match the broker

*If any box is unticked: do not start an order-capable run.*
