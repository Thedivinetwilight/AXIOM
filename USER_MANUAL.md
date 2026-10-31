# AXIOM Operator — User Manual

**Current source version: 3.7.0a9**

**Audience:** the account owner operating AXIOM on Windows x64.

> **Version 3.7.0a9 Multi-Market NSE (Upstox) & NASDAQ (Alpaca) Operator Console:** The dark `/terminal` console (`01`–`12`) presents **Indian NSE (`INR ₹` via Upstox)** and **US NASDAQ (`USD $` via Alpaca)** alongside independent **LOCAL PAPER** simulation, `10 AI & Model Lab` (frozen CPU `NeoQuasar/Kronos-small`), `11 Live Trade Monitor` (strictly read-only candlestick & signal/order/fill/SL/TP/exit inspector), and `12 Ensemble & Allocation` (`1,024+` bounded parallel strategy variants + OOS calibrator + `PortfolioRiskAllocator`). All research, calibration, and monitor surfaces are strictly PAPER/RESEARCH/READ-ONLY and never authorize or alter live execution.

AXIOM separates execution scopes explicitly across its multi-market console:

| Scope | Market & Currency | Execution Boundary |
|---|---|---|
| **LOCAL PAPER** (`AXIOM_PAPER`) | Local simulator | Independent simulated cash, orders, positions, and replay/manual paper fills. Zero broker order routing. |
| **NSE LIVE-MARKET PAPER** | NSE (`INR ₹` via Upstox) | Read-only Upstox quotes, candles, and BOD master reconciliation with 100% locally simulated SQLite fills and Indian statutory fee modeling (STT, NSE txn, SEBI, stamp duty, 18% GST). |
| **NASDAQ LIVE-MARKET PAPER** | NASDAQ (`USD $` via Alpaca) | Read-only Alpaca `IEX` / `SIP` market data with 100% locally simulated SQLite fills and US SEC Section 31 / FINRA TAF fee modeling. |
| **ALPACA BROKER (`PAPER` / `LIVE`)** | NASDAQ (`USD $` via Alpaca) | Separate Trading API v2 credentials, disabled by default (`ALPACA_BROKER_ENABLED=true` required for live), guarded by USD risk limits, readiness checks, **Arm**, and phrase-confirmed **Activate**. |
| **UPSTOX LIVE (`/autonomy`)** | NSE (`INR ₹` via Upstox) | Separate daily `UPSTOX_LIVE_ACCESS_TOKEN`, disabled by default (`AUTONOMY_LIVE_ENABLED=true`, `UPSTOX_ALGO_NAME`, and SEBI static IP required), guarded by INR risk limits, 3-leg GTT protection, **Arm**, and **Activate**. |

> **No profitability or live-readiness claim.** Automated tests, read-only connection checks, and simulated or broker-paper fills do not establish profitability or readiness for real money. Live credentials and live activation are solely the account owner's responsibility.

---

## 1. Safety model — read before connecting an account

### 1.1 Credentials and startup never authorize trading

Saving Upstox or Alpaca credentials, running a read-only quote or `/v2/account` verification probe, starting AXIOM, restarting after a crash, or running Kronos/Model Lab/Ensemble evaluations **never** Arms or Activates live execution. Every live gate starts `DISABLED` / `DISARMED`.

### 1.2 Separate market-data and execution credentials

AXIOM stores market-data credentials and broker-execution credentials in separate Windows DPAPI-encrypted slots:

1. `UPSTOX_ACCESS_TOKEN` — Read-only NSE quotes, historical candles, and daily BOD `complete.json.gz` reconciliation. Cannot place live orders.
2. `ALPACA_MARKET_DATA` — Read-only NASDAQ snapshots, historical bars, and `/v2/assets` universe sync (`iex`, `sip`, or chart-only `delayed_sip`). Never shared with Alpaca Broker execution.
3. `ALPACA_BROKER_PAPER` / `ALPACA_BROKER_LIVE` — Separate Alpaca Trading API v2 slots for broker-paper (`paper-api.alpaca.markets`) or gated real-money (`api.alpaca.markets`) execution.
4. `UPSTOX_LIVE_ACCESS_TOKEN` — Separate Upstox live order-authorized token for the `/autonomy` boundary.

### 1.3 Separate gates for live execution

1. **Read-only market data & universe verification:** verify fresh quotes (`CONNECTED_READ_ONLY`) and exact exchange/ISIN/symbol metadata.
2. **Bounded positive risk caps:** configure positive per-market INR (`06 Risk & live gates`) and USD (`06 Risk & live gates`) risk limits.
3. **Readiness:** every server-side check must pass; unknown, stale, or unavailable checks fail closed.
4. **Arm:** explicit operator step that binds a short-lived challenge and submits zero orders.
5. **Activate:** separate explicit confirmation phrase (`I UNDERSTAND THIS WILL PLACE REAL-MONEY LIVE ORDERS` for Alpaca LIVE; separate daily Activate on `/autonomy` for Upstox LIVE).
6. **Emergency stop:** sets a durable halt across Local Paper, NSE/NASDAQ Live-Market Paper, Upstox Autonomy, and Alpaca Broker that persists across restarts until explicitly cleared with `CLEAR EMERGENCY STOP` (which still leaves all gates disarmed).

---

## 2. Requirements and Windows installation

### 2.1 System requirements

- Windows 10/11 **x64** for the packaged application.
- A current Edge, Chrome, or Firefox browser. Keep the AXIOM console window open while using the app.
- Optional for **`10 AI & Model Lab`** Kronos CPU setup: CPython `3.11` or `3.12` **x64** with `pip` (`py -3.11-64` / `py -3.12-64` or a validated full `python.exe` path). Weights and locked CPU wheels install into `%LOCALAPPDATA%\AXIOM\autonomy\kronos`, never into the application folder.

### 2.2 Install or run portable

1. Download the Windows x64 `*-no-temp-install.zip` or `*-portable.zip` and the published `SHA256SUMS.txt` from the same release.
2. Verify the exact ZIP hash before extracting it. Release binaries are **unsigned unless the release metadata explicitly says otherwise**; SmartScreen/Smart App Control can warn or block them.
3. For installation, extract the entire installer ZIP, keep the setup EXE and matching `-0.bin`/`-1.bin` together, then run the EXE. The per-user installer does not require administrator rights and installs under `%LOCALAPPDATA%\Programs\AXIOM-Chart` for upgrade compatibility, while the product name is **AXIOM Operator**.
4. Alternatively, extract the entire portable ZIP and run `AXIOM-Chart.exe` inside `AXIOM-Operator` with `_internal` beside it. The onedir folder avoids onefile TEMP extraction and stores operator data in the per-user data directory, not beside the executable.
5. AXIOM opens `http://127.0.0.1:<port>/terminal`. The displayed local address is not a public hosted service.

For the reported Error 4551, these loaderless/onedir packages avoid the identified Inno setup-loader and onefile runtime TEMP paths. They do not make unsigned code trusted or bypass Windows policy. Keep Application Control enabled; if the device still blocks the package, trusted signing or administrator approval is required. Read `WINDOWS_APP_CONTROL.md`.

### 2.3 Local API token

The local `API_TOKEN` protects AXIOM's loopback API. It is **not** an Upstox token or Alpaca key, and the launcher never prints it to the console.

1. Open `%LOCALAPPDATA%\AXIOM\axiom.env` in Notepad.
2. Copy only the value after `API_TOKEN=` into the browser's token prompt.
3. Keep the file private. The browser remembers the token only in `sessionStorage` for that tab.

---

## 3. Operator console workspaces (`01`–`12`)

The persistent header displays active market switchers (`NSE · INR ₹` and `NASDAQ · USD $`), quick symbol jump, IST/ET clocks, session mode status, and **Emergency stop**. Below it, the three-column execution boundary banner continuously distinguishes **Execution**, **Market Data**, and **Order Authorization**.

| Workspace | Purpose |
|---|---|
| **`01 Overview`** | Mode separation strip (`BACKTEST / REPLAY`, `LIVE-MARKET PAPER`, `ALPACA BROKER PAPER`, `LIVE-MARKET LIVE`), NSE (`INR ₹`) & NASDAQ (`USD $`) paper ledgers, 4-role provider status matrix, local paper run summary, and readiness checklists. |
| **`02 Watchlists & search`** | Unified NSE & NASDAQ catalog search, **Refresh NSE BOD master**, **Sync NASDAQ universe**, subclass filters (`EQUITY`, `COMMON_STOCK`, `ETF`, `TRADE_FOR_TRADE`, `SME_EQUITY`, `INDEX`), and pinned multi-market watchlists. |
| **`03 Supercharts & ticket`** | Dark HTML5 candlestick canvas (`Local Paper / Replay`, `NSE Live Read-Only (Upstox)`, `NASDAQ Live Read-Only (Alpaca)`), timeframe shortcuts, zoom/pan, crosshair OHLC readout, `EMA 20` / `SMA 50`, horizontal/trend drawings, and dual-tab order ticket (`Local Paper Ticket` + `NSE / NASDAQ Paper` with itemized fee preview). |
| **`04 Orders & positions`** | NSE & NASDAQ SQLite paper positions (SL/TP edit & close), working orders (cancel), fills & regulatory fee ledger, `Reconcile NSE & NASDAQ SQLite`, plus local paper positions/orders/fills. |
| **`05 Strategies & automation`** | Completed-bar paper strategy sessions (`ema_crossover`, `rsi_mean_reversion`, `manual_only`) per market, Alpaca NASDAQ `PAPER` / `LIVE` gate controls (`Run Readiness`, `Arm Gate`, phrase `Activate LIVE`, `Disarm`, `Recover`), and Upstox NSE Live boundary link (`/autonomy`). |
| **`06 Risk & live gates`** | Positive server-side NSE (`INR ₹`) and NASDAQ (`USD $`) paper bounds, Alpaca Broker hard limits, local paper pause/stop, and durable emergency stop clear. |
| **`07 Connections`** | Windows DPAPI credential forms and harmless read-only GET verification probes for Upstox NSE market data, Alpaca NASDAQ market data (`iex` / `sip` / `delayed_sip`), Alpaca Broker (`paper` / `live`), and Upstox Live token. |
| **`08 Activity & audit`** | Tamper-evident SHA-256 hash-chained NSE & NASDAQ paper audit log plus redacted local session activity and JSON export. |
| **`09 Settings & manuals`** | Tab-scoped `API_TOKEN` replacement, auto-refresh cadence, p50/p95/p99 latency & canvas render diagnostics, listed-share/ETF identity boundary, and links to `/manual`, `/autonomy`, and `/docs`. |
| **`10 AI & Model Lab`** | Pinned CPU `NeoQuasar/Kronos-small` setup & offline self-test, validated OHLCV forecast inspector, dataset manager, leak-free chronological walk-forward + `2.0x` cost-stress runner, and SHA-256 model registry. |
| **`11 Live Trade Monitor`** | Strictly **read-only** real-time candlestick & 8-shape trade event monitor (`PAPER` vs `LIVE` badged, `"NOT TRADING"` when stopped/paused/disarmed/scanning, zero order-mutation buttons). |
| **`12 Ensemble & Allocation`** | Up to `1,024+` parallel technical/mean-reversion/volatility/volume strategy variants, OOS empirical probability calibrator, advisory Kronos status, `PortfolioRiskAllocator` sizing inspector, ablation runner, and scaling benchmark. |

---

## 4. Upstox (NSE `INR ₹`) & Alpaca (NASDAQ `USD $`) setup

### 4.1 Connect Upstox for NSE (`INR ₹`)

1. Generate a daily read-only `access_token` from your Upstox developer app (tokens reset daily around `03:30 IST`; AXIOM inspects JWT `exp` automatically).
2. Open **`07 Connections` → 1. India NSE Market Data (Read-Only)**, paste the token, and click **Save Upstox data token**. The field clears immediately and stores the token encrypted via Windows DPAPI.
3. Click **Verify NSE quote (RELIANCE)** to run a read-only GET quote check with zero order calls and confirm `CONNECTED_READ_ONLY`.
4. On **`02 Watchlists & search`**, click **Refresh NSE BOD master** to download and reconcile the official daily Upstox NSE `complete.json.gz` file. Before the first BOD sync, AXIOM uses `nse_seed_catalog.json` and explicitly marks `authoritative_complete: false`.

### 4.2 Connect Alpaca for NASDAQ (`USD $`)

1. Generate an API Key ID and Secret Key in your Alpaca dashboard. Select **`iex`** for free real-time IEX exchange quotes (~2–3% US volume) or **`sip`** only if your account holds a paid consolidated US tape entitlement (`delayed_sip` is chart-only and blocks paper market orders).
2. Open **`07 Connections` → 2. US NASDAQ Market Data (Read-Only)**, enter the Key ID and Secret, choose the feed, and click **Save Alpaca data keys**.
3. Click **Verify NASDAQ quote (AAPL)** to run a read-only snapshot probe and confirm `CONNECTED_READ_ONLY`.
4. On **`02 Watchlists & search`**, click **Sync NASDAQ universe** to load active tradable NASDAQ equities and ETFs from `GET /v2/assets`.

### 4.3 Separate Live Execution Gates (OFF by default)

- **NASDAQ via Alpaca (`05 Strategies & automation` + `06 Risk & live gates` + `07 Connections`)**: Save `ALPACA_BROKER_PAPER` or `ALPACA_BROKER_LIVE` keys in slot 3 on `07 Connections`, save positive USD hard limits on `06 Risk & live gates`, run **Test /v2/account + /v2/clock (Read-Only)**, then on `05 Strategies & automation` run **Readiness**, **Arm Gate**, and (only when `ALPACA_BROKER_ENABLED=true` and deliberately intended) enter the exact confirmation phrase to **Activate**.
- **NSE via Upstox (`/autonomy`)**: Save `UPSTOX_LIVE_ACCESS_TOKEN` in slot 3 on `07 Connections`, configure `AUTONOMY_LIVE_ENABLED=true`, `UPSTOX_ALGO_NAME`, and SEBI static IP registration, and complete daily readiness, **Arm**, and **Activate** on `/autonomy` (documented in `docs/AUTONOMY.md` / `/autonomy/manual`).

---

## 5. LOCAL PAPER and replay accounting

LOCAL PAPER (`AXIOM_PAPER`) runs independently of live brokers:

- **Default replay is automated:** clicking **Start automated replay (moving_average)** on `03 Supercharts & ticket` (or `/chart`) runs `moving_average` on labeled sample CSV data with local strategy automation ON. Actionable strategy signals can create simulated orders and simulated fills without per-ticket review, and zero broker orders are ever routed.
- **Durable replay audit:** `08 Activity & audit` records allowlisted `PAPER_SESSION_STARTED`, `PAPER_SESSION_RUNNING`, `PAPER_STRATEGY_SIGNAL`, `PAPER_ORDER_SIMULATED`, `PAPER_FILL_SIMULATED`, pause/resume, and `PAPER_SESSION_STOPPED` (`END_OF_DATA`, `OPERATOR_STOP`, `EMERGENCY_STOP`, `APPLICATION_SHUTDOWN`) events.
- **P&L scope separation:** `01 Overview` distinguishes persisted account-wide local history from the current/last run's delta summary. Simulated accounting values are never broker fills or profitability evidence.

---

## 6. Verification, recovery, and limitations

1. **Ordinary restart / crash recovery:** all live gates (`UPSTOX_LIVE`, `ALPACA_BROKER_LIVE`) start disarmed; running NSE/NASDAQ paper strategy sessions recover into `RECOVERED_PAUSED` so no orders fire silently on startup.
2. **Unresolved broker intents:** if a network timeout leaves an Alpaca or Upstox live intent unresolved, readiness blocks new entries until journal reconciliation resolves the state against broker records. Never delete SQLite journals to bypass reconciliation.
3. **CI vs owner verification:** `BUILD_METADATA.json` records exact source commit, Windows runner checks, DPAPI tests, frozen-app smoke tests, and offline Kronos CPU self-test (`SYNTHETIC_INFERENCE_SMOKE_ONLY`). External Upstox and Alpaca live/account checks without owner credentials remain `NOT VERIFIED` until run by the account owner on their own machine.
