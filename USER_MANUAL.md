# AXIOM Operator — User Manual

**Current source version: 3.7.0a10**

**Audience:** the account owner operating AXIOM on Windows x64 (directly from a cloned Git checkout — no installer required).

> **Version 3.7.0a10 Multi-Market NSE (Upstox) & NASDAQ (Alpaca) Source-First Operator Console:** The dark `/terminal` console (`01`–`12`) runs directly from a cloned source repository (`.\Start-Axiom.ps1` or `python .\axiom_launcher.py`) and presents **Indian NSE (`INR ₹` via Upstox)** and **US NASDAQ (`USD $` via Alpaca)** alongside independent **LOCAL PAPER** simulation, `10 AI & Model Lab` (frozen CPU `NeoQuasar/Kronos-small`), `11 Live Trade Monitor` (strictly read-only candlestick & signal/order/fill/SL/TP/exit inspector), and `12 Ensemble & Allocation` (`1,024+` bounded parallel strategy variants + OOS calibrator + single `PortfolioRiskAllocator`). All research, calibration, and monitor surfaces are strictly PAPER/RESEARCH/READ-ONLY and never authorize or alter live execution.

AXIOM separates execution scopes, data sources, fill types, and session P&L explicitly across its multi-market console:

| Scope | Market & Currency | Data Source, Execution Destination & Fill Type |
|---|---|---|
| **1 · LOCAL REPLAY PAPER** (`AXIOM_PAPER`) | Local simulator | Bundled labeled sample CSV data (`data/sample*.csv`) → local SQLite paper broker (`0` network/broker calls). Fill type: **`SIMULATED`**. Current-run P&L isolated from persisted history. |
| **2 · NSE LIVE-MARKET PAPER** | NSE (`INR ₹` via Upstox) | Real read-only Upstox `NSE_EQ` quotes, candles, and BOD master reconciliation (`0` sample fallback) → 100% locally simulated SQLite fills with Indian statutory fees (STT, NSE txn, SEBI, stamp duty, 18% GST). Fill type: **`SIMULATED`**. `current_session_total_pnl` separated from `persisted_historical_realized_pnl`. |
| **2 · NASDAQ LIVE-MARKET PAPER** | NASDAQ (`USD $` via Alpaca) | Real read-only Alpaca `iex` (single-venue IEX feed — **not** consolidated US tape) or paid `sip` (consolidated US tape) market data (`0` sample fallback; `delayed_sip` blocked for entries) → 100% locally simulated SQLite fills with US SEC Section 31 / FINRA TAF fees. Fill type: **`SIMULATED`**. |
| **3 · ALPACA BROKER (`PAPER` / `LIVE`)** | NASDAQ (`USD $` via Alpaca) | Separate Trading API v2 credentials (`ALPACA_BROKER_PAPER` → `paper-api.alpaca.markets`, fill type **`BROKER_PAPER_SANDBOX_CONFIRMED`**; `ALPACA_BROKER_LIVE` → `api.alpaca.markets`, fill type **`BROKER_CONFIRMED`**), disabled by default (`ALPACA_BROKER_ENABLED=true` required), guarded by USD risk limits, readiness checks, **Arm**, and phrase-confirmed **Activate**. |
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

## 2. Requirements, Source-First Startup, and Optional Packaging

### 2.1 System requirements

- Windows 10/11 **x64** with CPython `3.11+` x64 (`py -3.11-64`) and Git for the primary source-first workflow.
- A current Edge, Chrome, or Firefox browser. Keep the PowerShell window running AXIOM open while using `/terminal`.
- Optional for **`10 AI & Model Lab`** Kronos CPU setup: CPython `3.11` or `3.12` **x64** with `pip` (`py -3.11-64` / `py -3.12-64` or a validated full `python.exe` path). Weights and locked CPU wheels install into `%LOCALAPPDATA%\AXIOM\autonomy\kronos`, never into the repository folder.

### 2.2 Run directly from cloned source (Recommended — no installer required)

1. Clone the repository, create `.venv`, and install locked dependencies (including Windows `tzdata` for `Asia/Kolkata` and `America/New_York` market-session calendars):
   ```powershell
   git clone https://github.com/Thedivinetwilight/AXIOM.git
   Set-Location .\AXIOM
   py -3.11-64 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install --upgrade pip
   .\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
   .\.venv\Scripts\python.exe .\axiom_launcher.py --register-venv
   .\.venv\Scripts\python.exe .\axiom_launcher.py --check-deps
   ```
2. Run a non-destructive loopback smoke check (`0` broker calls, `0` user-data mutations):
   ```powershell
   .\Start-Axiom.ps1 -Smoke
   ```
3. Launch AXIOM from inside the repository (`.\Start-Axiom.ps1`, `.\.venv\Scripts\python.exe .\axiom_launcher.py`, `.\.venv\Scripts\python.exe -m app operator`, or `.\.venv\Scripts\python.exe .\run.py --mode operator`) **or** from any other working directory (`& C:\path\to\AXIOM\Start-Axiom.ps1` or `C:\path\to\AXIOM\.venv\Scripts\python.exe C:\path\to\AXIOM\axiom_launcher.py`) without setting `PYTHONPATH` manually.
4. Stop AXIOM cleanly with `Ctrl+C` in PowerShell. Update at any time with `git pull --ff-only` followed by `.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt`; existing `%LOCALAPPDATA%\AXIOM` data, credentials, and SQLite databases remain untouched and all live gates stay OFF.

### 2.3 Optional frozen Windows package / portable ZIP (CI artifact workflow)

1. If using optional CI-built Windows x64 archives instead of source, download `*-no-temp-install.zip` or `*-portable.zip` and the published `SHA256SUMS.txt` from the same release.
2. Verify the exact ZIP hash before extracting it. Release binaries are **unsigned unless the release metadata explicitly says otherwise**; SmartScreen/Smart App Control can warn or block them.
3. For installation, extract the entire installer ZIP, keep the setup EXE and matching `-0.bin`/`-1.bin` together, then run the EXE. The per-user installer does not require administrator rights and installs under `%LOCALAPPDATA%\Programs\AXIOM-Chart` for upgrade compatibility, while the product name is **AXIOM Operator**.
4. Alternatively, extract the entire portable ZIP and run `AXIOM-Chart.exe` inside `AXIOM-Operator` with `_internal` beside it. The onedir folder avoids onefile TEMP extraction and stores operator data in the per-user data directory, not beside the executable.
5. AXIOM opens `http://127.0.0.1:<port>/terminal`. The displayed local address is not a public hosted service.

For the reported Error 4551, these loaderless/onedir packages avoid the identified Inno setup-loader and onefile runtime TEMP paths. They do not make unsigned code trusted or bypass Windows policy. Keep Application Control enabled; if the device still blocks the package, trusted signing or administrator approval is required. Read `WINDOWS_APP_CONTROL.md`.

### 2.4 Local API token

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
| **`05 Strategies & automation`** | Completed-bar paper strategy sessions (`ema_crossover`, `rsi_mean_reversion`, `manual_only`) per market, Per-market **PAPER / LIVE switch** (§4.3.1) above the raw gate controls, Alpaca NASDAQ `PAPER` / `LIVE` gate controls (`Run Readiness`, `Arm Gate`, phrase `Activate LIVE`, `Disarm`, `Recover`), and Upstox NSE Live boundary link (`/autonomy`). |
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

#### 4.3.1 The console PAPER / LIVE switch (one market at a time)

`05 Strategies & automation` opens with **PAPER / LIVE Execution Switch**, above the detailed coordinator panel. It exists to make the *safe* direction (LIVE → PAPER) one click, while the *risky* direction (PAPER → LIVE) stays a guided walk through every gate the server already enforces. It is a client for `app.markets.live_execution`: no new endpoint, no second execution boundary, no order route, and no "enable both markets" control.

**Two cards, never one switch.** `NSE · INR ₹` and `NASDAQ · USD $` each have their own switch, their own readiness, their own limits, and their own gate. A card shows: the selected market and currency, the broker and destination label, account/credential **presence** (never a secret, never a full account number), the eight configured limits or `NOT configured`, the current gate state and kill-switch state, the open-order policy, and the exit route.

**States you can see:**

| Card state | Meaning | What the switch allows |
|---|---|---|
| `PAPER` (default after every start) | Local simulated fills on live read-only data | *LIVE* disabled until readiness is read and passes |
| `LIVE` (mode=LIVE, gate still `DISABLED`) | Mode intent only — no order can route | *PAPER*, or continue to Arm → Activate |
| `LIVE · ACTIVE` | Gate `ACTIVE` with a 300-second permit | *PAPER* (disarm + revoke), Emergency Stop below |
| `LIVE` disabled + reason | A blocking readiness check, a stale read, or no read yet | Nothing: the control is disabled and a click contacts no endpoint |

**PAPER → LIVE is four deliberate clicks.** Each one is a separate state on screen and none of them is skipped or implied:

1. **Read readiness (GET only)** — `GET /api/markets/live/{market}/readiness` plus the report `GET /api/markets/live/readiness`. Prints every blocker by name with its meaning, the documented exit route, and the open-order policy. This step never changes a mode and never arms anything.
2. **Set mode LIVE** — `POST /api/markets/live/{market}/mode {mode: LIVE}`. The gate stays `DISARMED`; a mode alone can never place an order. The cached readiness read is discarded at this point, because `mode_is_live` is itself a readiness check.
3. **Arm** — `POST /api/markets/live/{market}/arm`, offered only after a *fresh* passing read. The server re-runs readiness and refuses with `409` if anything fails.
4. **Acknowledge + phrase + Activate** — the risk acknowledgement checkbox, the exact phrase `ACTIVATE LIVE REAL-MONEY TRADING`, and (where the market routes no exit) the no-exit acknowledgement. Only then does **4. Activate LIVE** enable, and it sends `POST /api/markets/live/{market}/activate`.

Activating LIVE **does not start or resume a strategy** and does not start a paper session: the switch makes no such call, and the coordinator only executes what a strategy independently signals.

**LIVE → PAPER is one click and changes nothing else.** It sends `POST /api/markets/live/{market}/mode {mode: PAPER}`; the server disarms the gate and revokes the permit. The switch never cancels, modifies, or test-submits a working order and never touches a protective exit — the only control that can attempt a cancel is *Emergency Stop* with `cancel_working=true`, which this switch does not use.

**Why a switch can be disabled, in the operator's words.** Every name below is the server's own check name; the console prints the name and this meaning.

| Check | Meaning when it fails |
|---|---|
| `configuration_complete` | A required broker/account dependency is missing (listed as *Missing dependency*). |
| `risk_limits_configured` | Fewer than all eight per-market limits are positive — partial or zero limits fail closed. |
| `no_unresolved_intents` | An earlier live order outcome is unresolved; **Reconcile** first. |
| `not_blocked` | The market is latched after an unknown outcome; **Reconcile** first. |
| `kill_switch_not_engaged` | Emergency Stop is engaged; **Reset kill switch** (explicit acknowledgement) returns the gate to `DISABLED`. |
| `live_master_flag` | `AUTONOMY_LIVE_ENABLED` is not set on the host. |
| `separate_live_credential` | The separate `UPSTOX_LIVE_ACCESS_TOKEN` is missing (paper/read-only keys cannot unlock LIVE). |
| `algo_name_configured` / `static_ip_configured` | `UPSTOX_ALGO_NAME` / SEBI static IP registration is missing. |
| `catalog_authoritative_today` | Today's Upstox BOD instrument catalog is not authoritative. |
| `market_session_open` / `calendar_session_open` | The exchange session is closed. |
| `account_verified` / `account_trading_allowed` | The account cannot be read, or the broker reports it blocked from trading. |
| `account_currency_inr` / `account_currency_usd` | The account currency does not match the market. |
| `market_quote_available` | No live quote for the requested symbol. |
| `broker_enabled` / `alpaca_broker_configured` | The Alpaca broker service is disabled or not bound. |
| `environment_is_live` | Alpaca is bound to PAPER keys, not the separate live keys. |
| `service_unblocked` / `reconciliation_ok` | The broker service is latched, or the last reconciliation failed. |
| `clock_synced` / `daily_loss_within_limit` | Server clock skew, or the daily live-loss limit is reached. |
| `mode_is_live` | **Not a blocker.** It is the step the switch performs at step 2; it is listed separately until the mode is LIVE. |

Any answer the server cannot give — timeout, `409` refusal, unexpected error — invalidates the displayed readiness evidence and re-locks LIVE. A refused Arm never leaves the card claiming a passing check. Nothing about the mode is stored in the browser: a reload or restart shows the server's own state, and every start begins at `PAPER` / `DISABLED`.

**The NSE exit limitation stays on screen.** For NSE the card prints **NO ROUTED EXIT — AXIOM cannot close an NSE position**, the documented closing path (the GTT `STOPLOSS`/`TARGET` legs placed with the entry), and the manual fallback (close in the Upstox app, then Reconcile). LIVE cannot be activated for NSE until that is acknowledged with a checkbox of its own; the switch offers no exit control, because the adapter refuses any NSE exit with `exit_route_unsupported` before a broker call.

**What this switch is not.** It is not a readiness claim for either broker. In this build both markets report blocking checks and stay `PAPER` / `DISABLED`; the switch only shows what the server already says. The console's local `API_TOKEN` remains separate from every broker credential, and no broker secret is displayed, typed into, or stored by the switch.

#### 4.3.2 Per-market LIVE execution coordinator (manual, per market, disarmed after every restart)

`LIVE-MARKET LIVE` is a **separate execution path** from `LIVE-MARKET PAPER`: it has its own per-market SQLite journal, order intents, audit hash chain, risk limits, and broker clients. It never converts a paper session, and a market set to `LIVE` is refused by every paper endpoint (`409`, mode separation). Both markets start as `PAPER` with the gate `DISABLED` on every process start, crash recovery, and restart — nothing about live trading is persisted except the mode *intent*, the journal, and the limits.

Do this **per market**, only when you intend to place real orders (NSE = real INR, NASDAQ = real USD):

1. **Configure the broker for live** (nothing below works with paper credentials):
   - NSE: `AUTONOMY_LIVE_ENABLED=true`, `UPSTOX_ALGO_NAME`, `AXIOM_UPSTOX_EXPECTED_PUBLIC_IP` (the SEBI-registered static IP), a separate `UPSTOX_LIVE_ACCESS_TOKEN`, and today's authoritative Upstox BOD instrument catalog.
   - NASDAQ: `ALPACA_BROKER_ENABLED=true`, broker environment **`live`**, separate `ALPACA_BROKER_LIVE` key/secret, and positive `BrokerRiskLimits`.
2. On **`05 Strategies & automation` → Per-Market Live Execution Coordinator**, select the market's mode **`LIVE`** and click **Save NSE/NASDAQ mode**. The gate stays `DISABLED`; the mode alone cannot place an order.
3. Enter and save **all eight** of that market's limits: max order value, daily orders, daily loss, open positions, max symbol exposure, max total exposure, max quote age (seconds), and max price deviation (bps). The panel refuses to send a partial set and the API rejects one too: zero or partial limits **fail closed** — readiness reports `risk_limits_configured=false` and Arm is refused.
4. Click **Readiness**. Every check must pass (credentials, algo name, static IP, authoritative same-day catalog, market session, account readable and not blocked, broker live environment for NASDAQ).
5. Click **1. Arm**. The gate becomes `ARMED`; it still cannot send orders.
6. Type the exact phrase `ACTIVATE LIVE REAL-MONEY TRADING`, tick the acknowledgement checkbox, and click **2. Activate LIVE**. The gate becomes `ACTIVE` and a 300-second in-memory permit is minted. **Activate is refused if the phrase or the acknowledgement is missing.**
7. Orders are now routed only for that market while the gate is `ACTIVE`, inside the central risk gate (order value, daily orders, daily loss, open positions, symbol/total exposure, quote freshness, price deviation, duplicate suppression), and only while the market is open. Closed markets still analyze but never submit.

Stopping and recovery:

- **Disarm** returns the gate to `DISABLED` and drops the permit.
- **Emergency Stop** is per market: it halts that market only (`KILL_SWITCH`) and never disables the other market's safeguards. **Reset kill switch** requires an explicit operator acknowledgement.
- A submission with an **unknown outcome** (timeout, ambiguous acknowledgement, transport error) latches the market as `UNRESOLVED_ORDER_INTENT`, disarms it, and blocks further orders until **Reconcile** confirms broker state. Never retry an uncertain order by hand.
- A failed protective stop on NASDAQ latches `PROTECTION_FAILED` and disarms the market for operator action.
- Restarting AXIOM, recovering from a crash, or a background-cycle timeout leaves the market **disarmed** (`DISABLED` / `CYCLE_TIMEOUT_RECONCILE_REQUIRED`) and requires readiness, Arm, and Activate again.

#### 4.3.3 Terminal console ↔ server routes (nothing calls a removed route)

The `/terminal` console is an authenticated HTML page and calls only routes this build actually exposes. It previously called the removed Vantage order surface, which is why unlocking the terminal failed with **"Not Found"**.

- **Token check:** the unlock form validates `API_TOKEN` with `GET /paper/status` — an existing, authenticated, read-only route. A successful check proves only that the token is accepted; it arms nothing.
- **Distinct failures:** the gate reports them separately instead of one generic error — `401` → *token rejected*; `503` → *no `API_TOKEN` configured on the server*; `404` → *server build does not expose the probe route* (console/server version mismatch); no response → *server unreachable*. The same three-way classification is used by **Settings → update local token**.
- **Token storage:** the token is held in this browser tab's `sessionStorage` only. It is never written to `localStorage`, the DOM by an API response, a log, or an export.
- **State the console reads:** `/paper/status`, `/paper/account`, `/paper/positions`, `/paper/orders`, `/paper/trades` (local paper ledger, composed client-side — no aggregate endpoint was added), `/dashboard/data` (durable halt flag), `/api/markets/paper/audit/events` (hash-chained audit), `/api/performance`, `/api/connections/alpaca/credentials` (presence/back-end only), `/api/markets/overview`, `/api/listed-assets/status`, `/api/alpaca-broker/status`, `/chart/*`.
- **Emergency controls:** *Set* posts `/engine/stop` (persisted local halt + simulator pause) and `/api/markets/emergency-stop` (atomic multi-subsystem stop); *Clear* requires the typed phrase `CLEAR EMERGENCY STOP` for `/api/markets/emergency-stop/reset` plus `/engine/resume`. Clearing never arms, activates or resumes a session.
- **Unavailable in this build, and labelled as such:** unresolved order-intent feed (no endpoint), bulk quote map (use Supercharts/Watchlists), and a reconciliation timestamp. These render as *unavailable* with the reason — never as `0`, blank, or a fresh-looking value. Because intents cannot be read, an uncertain order outcome stays flagged until the tab is locked or reloaded rather than being auto-cleared.

**Readiness report and documented capabilities.** `GET /api/markets/live/readiness` (or `python -m app live-readiness [--json]`) returns one report for both markets: the broker, mode, gate state, every failed check by name, the required credentials **by name only**, account permissions, offline platform checks, the numbered acceptance steps and the documented capability record for that broker. It also states the open-order policy of the kill switch and labels its own coverage (`mock_or_fake_transport_only`, `live_verified: false`, `windows_acceptance: NOT_RUN`). The report never contacts a broker and never prints a secret. Full detail, including what each broker documents and what AXIOM refuses to send, is in `docs/LIVE_EXECUTION_READINESS.md`.

**Exits: what AXIOM routes and what it refuses.** NSE has **no exit route**. The protected GTT that opened an NSE position keeps live `STOPLOSS` and `TARGET` SELL legs for the same quantity — those are the supported close. A standalone Upstox exit cannot be shown to be close-only: a `SELL` `ENTRY` rule executes while flat, `cancel_gtt` refuses to remove protection from an open position, EDIS authorisation for SELL legs has no readable API, and the position read does not distinguish settled holdings. A `SELL` submitted to the NSE adapter is refused with `exit_route_unsupported` before any broker call; close the position in the Upstox app and run **Reconcile**. NASDAQ exits *are* routed, but only through the coordinator's held-long guard: a SELL without protective prices is refused (`naked_short_refused`) unless the broker position covers the quantity. Both refusal reasons are printed by the readiness report (`exit_route`). Details: `docs/LIVE_EXECUTION_READINESS.md` §3.

**Owner-run broker preflight (read-only, never an order).** Before activating either market, the report's `broker_preflight` lists the exact steps, per broker, to confirm: credential **configuration status** (presence only, values never read back), account identity (Upstox `/v2/user/profile`, Alpaca `/v2/account`), permissions and blocks/funding, market and session eligibility, and platform acceptance (SEBI static IP + algo name for Upstox; a clean paper-host cycle for Alpaca). Every step is `read_only: true` and `writes_orders: false` except Alpaca's paper-sandbox cycle, which is labelled `PAPER_SANDBOX`. AXIOM never runs these steps for you and never contacts a broker to produce the report. **Do not paste a token, key or account number into a ticket, chat or log** — record a masked identifier only.

**Verification levels — do not skip a rung.** The report states where the evidence stops: `MOCK` (achieved: deterministic fake-transport tests), `PAPER_SANDBOX`, `READ_ONLY_LIVE`, `ORDER_PATH_LIVE` (all `not_achieved` here). Nothing in this repository has done more than `MOCK`; a passing readiness report or a read-only probe is **not** live verification.

**Rate limits are their own outcome.** A broker 429 answer means nothing was placed: the intent is removed, `LIVE_ORDER_RATE_LIMITED` is audited, and that market refuses new submissions for 60 seconds without another broker call (`rate_limit_state()` shows the remaining cooldown). A rate limit never latches an unresolved intent and never changes another market's gate.

**Emergency stop and open orders.** The kill switch blocks new orders for that market only. Working orders are left alone unless `cancel_working=true` is passed, in which case a documented cancel is attempted for each and every failure is recorded. A cancel after a partial fill leaves the filled quantity filled; positions come from broker-reported positions on the next cycle. Clearing the kill switch requires an explicit acknowledgement and returns the gate to `DISABLED`, not `ACTIVE`, and it does not clear unresolved intents — those still need Reconcile.

Safety limits of this implementation: NSE live routing uses the existing **protected multi-leg GTT v3** integration, so it only sends `BUY` entries that carry both a stop and a target; `SELL` exits must be placed manually in Upstox (the adapter refuses and audits `exit_route_unsupported`). NASDAQ routing uses the existing gated Alpaca Trading v2 client (limit entry + a separate protective stop order); the status of both orders is reported per submission.

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
