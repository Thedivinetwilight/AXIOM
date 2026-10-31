# AXIOM Desktop Terminal — Owner Setup Guide & Architecture Reference

**Version:** `3.7.0a9`  
**Target Platform:** Windows 10 / 11 x64 (`AXIOM-Operator-3.7.0a9-windows-x64-no-temp-install.zip` or `*-portable.zip`, or Python 3.11+ x64 source checkout)

> **Safety & Non-Claim Notice**  
> - Real-money execution (`LIVE-MARKET LIVE`) is **OFF by default** and is never enabled by startup, restart, crash recovery, config import, Kronos inference, or paper trading.  
> - `LIVE-MARKET PAPER` uses real read-only market data (when owner credentials are connected) and simulates 100% of orders, fills, and regulatory fees locally in SQLite with **zero broker order API calls**.  
> - Automated tests, mock/fake provider tests, and CI runs **do not** verify real owner broker accounts, real live fills, or strategy profitability. External provider/account checks without owner credentials in CI are explicitly marked **`NOT VERIFIED / BLOCKED`**.

---

## 1. Owner Quick-Start Setup (Windows x64)

### Option A — Packaged Windows Installer or Portable Archive (Recommended)
1. Verify your downloaded installer or portable archive against `SHA256SUMS.txt`:
   ```powershell
   Get-FileHash .\AXIOM-Operator-3.7.0a9-windows-x64-no-temp-install.zip -Algorithm SHA256
   Get-FileHash .\AXIOM-Operator-3.7.0a9-windows-x64-portable.zip -Algorithm SHA256
   ```
2. Extract and run either `AXIOM-Operator-3.7.0a9-windows-x64-no-temp-setup.exe` (keep `-0.bin`/`-1.bin` beside it) or `AXIOM-Operator\AXIOM-Chart.exe` from the portable ZIP. AXIOM binds only to `127.0.0.1` with a per-session bearer token and opens `/terminal`. All user databases, Kronos CPU weights/runtime, and Windows DPAPI-encrypted credentials live under `%LOCALAPPDATA%\AXIOM` (preserved across upgrades).

### Option B — Windows 10/11 x64 Source Checkout Setup
```powershell
git clone https://github.com/Thedivinetwilight/AXIOM.git
cd AXIOM
git checkout arena/ffd46191-axiom
py -3.11-64 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
npm ci --ignore-scripts --no-audit --no-fund
# Run verification suites locally:
.\.venv\Scripts\python.exe -m pytest -q
npm test
# Launch AXIOM Desktop Terminal from source (loopback-only):
.\.venv\Scripts\python.exe axiom_launcher.py
```
> **Optional Pinned Kronos CPU Setup (Installer, Portable, or Source):** Ensure CPython `3.11` or `3.12` x64 with `pip` is installed on Windows, open **`10 AI & Model Lab`** in `/terminal`, click **Install Pinned Kronos (CPU)** (~110 MiB weights + locked CPU `torch==2.7.1+cpu` wheels into `%LOCALAPPDATA%\AXIOM\autonomy\kronos`), then click **Offline CPU Self-Test** to transition Kronos from `NOT_INSTALLED` / `NOT_VERIFIED` to `READY`.

### Step 2 — Connect Read-Only Market Data (`07 Connections`)
1. **Indian NSE (`INR ₹`) — Upstox Market Data**:
   - Generate a daily read-only `access_token` from your Upstox developer app (note: Upstox daily tokens expire around `03:30 IST` daily; AXIOM inspects the JWT `exp` claim automatically).
   - Open **`07 Connections` → Upstox Read-Only Token (NSE Market Data)**, paste the token, and click **Save Upstox token** (encrypted via Windows DPAPI; input cleared immediately).
   - Click **Verify NSE quote (`RELIANCE`)** to run a harmless `GET` probe (`0` order endpoints touched) and confirm `CONNECTED_READ_ONLY`.
2. **US NASDAQ (`USD $`) — Alpaca Market Data**:
   - Create an API Key & Secret in your Alpaca dashboard (free **IEX** real-time US feed works out of the box; select **SIP** only if your account holds a paid CTA/UTP consolidated tape subscription).
   - Open **`07 Connections` → Alpaca Market Data Keys (`ALPACA_MARKET_DATA`)**, select `IEX` or `SIP`, paste Key & Secret, and click **Save Alpaca data keys**.
   - Click **Verify NASDAQ quote (`AAPL`)** to confirm `CONNECTED_READ_ONLY`. If `SIP` is selected without entitlement, AXIOM reports HTTP `403` with actionable guidance to switch to `IEX`.

### Step 3 — Reconcile Instrument Universes (`02 Watchlists & search`)
- Click **Refresh NSE BOD master** to download and reconcile the official daily Upstox NSE `complete.json.gz` master (reporting exact counts by segment/type, alias mappings, suspended exclusions, and SHA-256 digest).
- Click **Sync NASDAQ universe** to download active US equities & ETFs from `GET https://api.alpaca.markets/v2/assets` filtered to `exchange == "NASDAQ"`.
- Search works 24/7 including weekends and off-hours. Outside market hours (`09:15–15:30 IST` for NSE; `09:30–16:00 ET` / `13:00 ET` early-close for NASDAQ), quotes are labeled `CLOSED` or `STALE` and **never** replaced with sample/demo prices.

### Step 4 — Trade in `LIVE-MARKET PAPER` (`03 Supercharts & ticket` / `05 Strategies & modes`)
- Switch the Superchart **Feed** selector to `NSE — Upstox Live Read-Only` or `NASDAQ — Alpaca Live Read-Only` and load any symbol (e.g., `RELIANCE`, `TCS`, `AAPL`, `NVDA`).
- In the right-hand **NSE / NASDAQ Paper** ticket tab, click **Preview fees & checks** to inspect simulated slippage, lot/tick/MIS/short rules, and itemized regulatory fees (Indian STT, NSE txn, SEBI, Stamp Duty, 18% GST; US SEC Section 31 & FINRA TAF), then click **Submit simulated order (0 broker calls)**.
- Optionally start an automated completed-bar paper strategy (`ema_crossover`, `rsi_mean_reversion`, or `manual_only`) in **`05 Strategies & modes`**. All state persists in SQLite WAL; if AXIOM restarts mid-session, running sessions recover explicitly into `RECOVERED_PAUSED` so no orders fire silently.

### Step 5 — Optional Gated Real-Money Execution (`LIVE-MARKET LIVE`)
Keep disabled unless you deliberately intend to risk capital and satisfy every external prerequisite:
- **NASDAQ (`ALPACA_LIVE`)**: Requires an approved, funded Alpaca brokerage account (note: verify non-US/Indian resident account eligibility directly with Alpaca Markets), separate `ALPACA_BROKER_LIVE` keys saved on `07 Connections`, bounded USD risk limits saved on `06 Risk & live gates`, passing **Run Alpaca Readiness** (`GET /v2/account` active + `GET /v2/clock` open + fresh quote + 0 unresolved journal intents), **Arm Gate**, and typing the exact confirmation phrase `I UNDERSTAND THIS WILL PLACE REAL-MONEY LIVE ORDERS`.
- **NSE (`UPSTOX_LIVE`)**: Requires separate `UPSTOX_LIVE_ACCESS_TOKEN`, Upstox Plus plan / SEBI static IPv4 whitelist for order placement, bounded INR risk limits, and explicit Arm → Activate on `/autonomy`. Without those prerequisites, AXIOM labels `UPSTOX_LIVE` **`NOT_AVAILABLE`** with exact missing dependencies.

---

## 2. Architecture & Provider Matrix

| Provider Slot | Market & Currency | Role | Official Endpoints Used | Account / Plan Requirement | Order Types Supported |
|---|---|---|---|---|---|
| `UPSTOX_MARKET_DATA` (`upstox_token`) | NSE (`INR ₹`) | Read-only quotes, candles, BOD master, `LIVE-MARKET PAPER` | `assets.upstox.com/.../complete.json.gz`, `api.upstox.com/v2/market-quote/quotes`, `/v3/historical-candle/...` | Upstox developer token (daily JWT expiry ~03:30 IST) | **None** (0 order methods; local paper simulates `MARKET`, `LIMIT`, `SL`, `SL-M`) |
| `ALPACA_MARKET_DATA` (`alpaca_data_store`) | NASDAQ (`USD $`) | Read-only snapshots, bars, asset sync, `LIVE-MARKET PAPER` | `data.alpaca.markets/v2/stocks/.../snapshot` & `/bars`, `api.alpaca.markets/v2/assets` | Alpaca API key (`IEX` free tier or paid `SIP` entitlement) | **None** (0 order methods; local paper simulates `MARKET`, `LIMIT`, `STOP`, `STOP_LIMIT`) |
| `ALPACA_BROKER_PAPER` | NASDAQ (`USD $`) | Optional broker-hosted paper sandbox | `paper-api.alpaca.markets/v2/account`, `/v2/clock`, `/v2/orders`, `/v2/positions` | Alpaca paper trading key pair | `market`, `limit`, `stop`, `stop_limit` (`day`, `gtc`, `ioc`, `fok`) |
| `ALPACA_BROKER_LIVE` | NASDAQ (`USD $`) | Default-OFF gated real-money US execution | `api.alpaca.markets/v2/account`, `/v2/clock`, `/v2/orders`, `/v2/positions` | Approved, funded Alpaca Live brokerage account + separate LIVE keys + Arm/Activate | `market`, `limit`, `stop`, `stop_limit` (`day`, `gtc`, `ioc`, `fok`) |
| `UPSTOX_LIVE` (`upstox_live_token`) | NSE (`INR ₹`) | Default-OFF gated real-money NSE execution | `api.upstox.com/v3/order/gtt/place` (via isolated `/autonomy` boundary) | Upstox Plus / SEBI static IPv4 whitelist + separate LIVE token + Arm/Activate | Protected 3-leg GTT (`ENTRY` + `STOP_LOSS` + `TARGET`) |

---

## 3. NSE Coverage Reconciliation & Honest Boundaries

- **Official Source**: Upstox Beginning-of-Day (BOD) Instrument Master (`https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz`), refreshed daily by Upstox from NSE exchange master files under Upstox API terms for authorized client use.
- **No Unverified "100% Complete" Claim**: When offline or before the first BOD download, AXIOM loads `app/markets/nse_seed_catalog.json` (`17` reference rows) and explicitly marks `authoritative_complete: false` (`source_kind: "bundled_seed_reference"`). Only an actual BOD ingestion sets `authoritative_complete: true` alongside the exact `source_sha256` digest and `refreshed_at` timestamp.
- **Segment & Subclass Accounting**: Every BOD row is accounted for across `EQUITY_COMMON` (`EQ`), `EQUITY_TRADE_FOR_TRADE` (`BE`/`BZ`, delivery-only, `mis_intraday_allowed=False`), `ETF`, `SME_EQUITY` (`SM`/`ST`, lot-size multiples enforced), `INDEX` (`NSE_INDEX`, watch/chart-only, `tradable=False`), `DERIVATIVE_FO` (`NSE_FO`), `CURRENCY_DERIVATIVE` (`NSE_CD`), `SUSPENDED_OR_INACTIVE`, and `EXCLUDED_OTHER_SEGMENT` (`BSE_*`, `MCX_*`).
- **Identity & Alias Resolution**: Canonical key is `segment|isin_or_token` (e.g. `NSE_EQ|INE002A01018` for `RELIANCE`). Duplicate symbol collisions, historical ticker aliases (`M&M`, `BAJAJ-AUTO`, `L&T` → `LT`, `TATAMOTORS`), NFKC Unicode/zero-width whitespace stripping, and exact/prefix/ISIN ranking with `ambiguous: true` flags are deterministically tested in `tests/test_nse_reconciliation.py`.

---

## 4. NASDAQ End-to-End Implementation & Account Limitations

- **Session Calendar (`app/markets/us_calendar.py`)**: Full `America/New_York` (`ZoneInfo`) EDT (`UTC-4`) / EST (`UTC-5`) DST handling, all NYSE/NASDAQ holidays (New Year's Day observed incl. Friday Dec 31 per NYSE Rule 7.2, MLK Day, Presidents' Day, Good Friday computus, Memorial Day, Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas), `13:00 ET` early closes (Black Friday, Christmas Eve, pre-Independence Day weekday), and runtime `closed_dates` / `early_close_dates` emergency overrides.
- **US Validation & Fee Realism**: Enforces USD currency isolation, tick sizes (`$0.01` >= `$1.00`, `$0.0001` sub-dollar), whole-share vs `fractionable` rules (`MARKET` `DAY` only for fractional shares), `shortable` / `easy_to_borrow` checks, quote freshness age gates, and SEC Section 31 + FINRA TAF regulatory fee simulation on sells.
- **Known External Limitations**:
  1. Free Alpaca `IEX` data represents IEX exchange book (~2–3% US volume), not full SIP consolidated NBBO.
  2. Real-money `ALPACA_LIVE` availability for Indian residents depends on Alpaca's international onboarding/compliance approval and USD wire funding. Where absent, `LIVE-MARKET PAPER` on real IEX data still works independently.

---

## 5. Desktop Terminal UI Inventory (`01`–`09`) & Test Evidence

| Workspace / Region | Key Controls | Backing API Routes | Automated Test Evidence |
|---|---|---|---|
| **Top Header** | `NSE · INR ₹` / `NASDAQ · USD $` switcher, Quick Symbol Jump, IST/ET clocks, Refresh, Global **Emergency stop** | `/api/markets/overview`, `/api/markets/emergency-stop` | `tests/js/multimarket_terminal_smoke.mjs` (Steps 1, 2, 9) |
| **`01 Overview`** | Mode separation strip, NSE ₹ & NASDAQ $ paper ledgers, `Sync quotes`, `Chart RELIANCE/AAPL`, 4-Role Provider Matrix, Local Paper summary | `/api/markets/overview`, `/api/markets/paper/*/sync`, `/api/trading/workspace` | `multimarket_terminal_smoke.mjs` (Step 1), `terminal_smoke.mjs` |
| **`02 Watchlists & search`** | Unified search, Market/Subclass filters, `Refresh NSE BOD master`, `Sync NASDAQ universe`, `+ Watch` / `Remove`, `Chart`, `Trade Paper` | `/api/markets/search`, `/nse/refresh`, `/nasdaq/sync`, `/watchlists` | `multimarket_terminal_smoke.mjs` (Step 4), `test_nse_reconciliation.py`, `test_us_calendar_and_nasdaq.py` |
| **`03 Supercharts & ticket`** | Feed selector (`SESSION`, `NSE`, `NASDAQ`), TF/Indicators (`EMA20`, `SMA50`), Drawing tools, Dual-tab Ticket (`Preview fees & checks`, `Submit simulated order`) | `/api/markets/chart`, `/api/markets/paper/preview`, `/api/markets/paper/orders` | `multimarket_terminal_smoke.mjs` (Steps 2, 5), `terminal_smoke.mjs` |
| **`04 Orders & positions`** | Open NSE/NASDAQ paper positions (`SL/TP` modal, `Close`), Working Orders (`Cancel`), Fills + Fees, `Reconcile NSE & NASDAQ SQLite` | `/api/markets/paper/positions/*`, `/orders/*`, `/reconcile` | `multimarket_terminal_smoke.mjs` (Step 6), `test_live_market_paper_and_unified_api.py` |
| **`05 Strategies & modes`** | NSE & NASDAQ Paper `Start`, `Pause`, `Resume`, `Stop`, `Reset`; `LIVE-MARKET LIVE` availability cards; Strategy Decisions log | `/api/markets/paper/session/*`, `/paper/reset`, `/live/status` | `multimarket_terminal_smoke.mjs` (Step 7) |
| **`06 Risk & live gates`** | NSE ₹ & NASDAQ $ paper bounds, Alpaca Broker hard limits, `Run Readiness`, `Arm Gate`, phrase-confirmed `Activate LIVE`, `Disarm`, `Recover`, `Clear Emergency Stop` | `/api/markets/paper/config`, `/api/alpaca-broker/limits`, `/readiness`, `/gate/*`, `/recover` | `multimarket_terminal_smoke.mjs` (Steps 8, 9), `test_alpaca_broker_hardening.py` |
| **`07 Connections`** | DPAPI save/verify/delete for `Upstox Read-Only`, `Alpaca Market Data`, `Alpaca Broker Paper/Live`, and `Upstox Live` | `/api/connections/*`, `/api/autonomy/upstox*`, `/api/alpaca-broker/credentials/*` | `multimarket_terminal_smoke.mjs` (Step 3), `test_provider_verification.py` |
| **`08 Activity & audit`** | Tamper-evident SHA-256 hash-chained Multi-Market Paper audit table + Redacted JSON export | `/api/markets/overview`, `/api/trading/activity` | `multimarket_terminal_smoke.mjs` (Step 10) |
| **`09 Settings & manuals`** | Session API token, Lock tab, Auto-refresh interval, Latency p50/p95/p99, Redacted diagnostics, Links to `/manual` & `/autonomy/manual` | `/api/trading/performance`, `/manual`, `/autonomy/manual` | `terminal_smoke.mjs` |
| **`10 AI & Model Lab`** | Pinned Kronos setup/self-test, Load/Unload & Enable/Disable, Forecast Inspector, Latency/RSS diagnostics, Dataset Manager, Walk-Forward & `2.0x` Cost-Stress Research Runner, SHA-256 Model Registry | `/api/model-lab/*`, `/api/autonomy/kronos/*` | `model_lab_smoke.mjs`, `test_kronos_model_lab.py`, `test_research_pipeline_leakage.py` |
| **Persistent Quick-Nav & Readiness** | Always-visible direct jump buttons (`Connections`, `Charts`, `Orders`, `Strategies`, `Risk`, `Activity`, `Model Lab`) + 4-mode Readiness Checklists with read-only verify buttons | `/api/markets/readiness`, `/api/connections/*/verify-quote` | `model_lab_smoke.mjs`, `test_kronos_model_lab.py` |

---

## 6. Verification Taxonomy Matrix

| Capability | Automated Suite / Mock / Contract Status | Real Owner Credential / Live Provider Status |
|---|---|---|
| NSE BOD master parsing, alias/ISIN/Unicode search, exclusions | **VERIFIED** (`tests/test_nse_reconciliation.py`) | **NOT VERIFIED** against live `assets.upstox.com` in sandbox (outbound host restricted) |
| NASDAQ `America/New_York` DST, holidays, early closes, catalog | **VERIFIED** (`tests/test_us_calendar_and_nasdaq.py`) | **NOT VERIFIED** against live `api.alpaca.markets` in sandbox (no owner keys) |
| Upstox & Alpaca 6-state connection/JWT/entitlement probes | **VERIFIED** (`tests/test_provider_verification.py`) | **BLOCKED / OWNER-SIDE** (requires owner Upstox token & Alpaca API keys) |
| `LIVE-MARKET PAPER` (INR & USD SQLite ledgers, fees, SL/TP, recovery, 0 broker calls) | **VERIFIED** (`tests/test_live_market_paper_and_unified_api.py`) | **BLOCKED / OWNER-SIDE** for live quote ingestion until owner saves keys |
| `LIVE-MARKET LIVE` gates, journal recovery, fail-closed blockers, kill switch | **VERIFIED** (`tests/test_alpaca_broker_hardening.py`) | **NOT VERIFIED / INTENTIONALLY NOT EXECUTED** (zero real orders placed per safety rule) |
| Dark TradingView-inspired Desktop UI (`01`–`09` + legacy smoke) | **VERIFIED** (`npm test`: 4/4 headless JSDOM suites green) | Browser GPU rasterization & Windows DPAPI verified on Windows CI / owner PC |
