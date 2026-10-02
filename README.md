# Arena — Quantitative Research & Trading Platform (FINAL 3.0.5)

**Arena is a working, modular Python research and paper-trading platform,
identified as `arena` version `3.0.5`. Real-money order routing does not exist
in this project.** Sections below keep the full release history (V0.1 → V0.6 →
V1 → V2.1 → FINAL 3.0.0); each historical section describes its own release.

**Current release (FINAL 3.0.5) highlights:** Market Event Listener — public-internet intelligence for war/escalation/ceasefire, sanctions, tariffs (incl. US-China), policy/central-bank, oil/gas, shipping, regulatory, geopolitical events; impact assessed via transmission paths (not simplistic sell), decides IGNORE/MONITOR/EVALUATE/NO_TRADE and provides context to the existing Strategy Router; hard-risk/Router/STOP/broker/auth/LIVE gates never bypassed, never places orders directly; multi-source dedup, reputable-source validation, timeout/retry/backoff/rate-limit/cache/isolation, no eval/exec; dashboard Market Events panel (EVENT/WHY/AFFECTED/CONFIDENCE/RESPONSE/ACTION/RESULT, sanitized, no secrets); observational learning memory records price/vol/volume per 5m/30m/1h/1d/3d/5d — additive, no external live dependency; plus continuous strategy discovery

**FINAL 3.0.5 (additive — Market Event Listener):** internet news ingestion (public sources, wars/escalations/ceasefires, sanctions/relief, tariffs/trade incl. US-China, policy/central-bank, oil/gas, shipping, regulatory, geopolitical) with relevance detection, source validation, normalization/dedup (fingerprint, multi-source merge, conflict reduces confidence, preserves conflict), impact assessment via transmission paths, decision IGNORE/MONITOR/EVALUATE/NO_TRADE with context to Strategy Router + hard-risk gate; never directly trades, never bypasses Router/lifecycle/portfolio-risk/hard-risk/STOP/broker/auth/LIVE; persisted event_id/timestamp/source/headline/summary/type/countries/markets/assets/sectors/confidence/relevance/effect/response/action/strategy/instruments/result; reputable→primary sources, dedup, unavailable→Arena continues; timeout/retry/backoff/rate-limit/cache/dedup/isolation; no eval/exec/shell; learning memory records price/vol/volume/sector response per 5m/30m/1h/1d/3d/5d via existing market-data infra (observational); dashboard Market Events panel (EVENT/WHY/AFFECTED/CONFIDENCE/RESPONSE/ACTION/RESULT, escaped, no secrets) — additive, no external live dependency; 20 deterministic tests.

**FINAL 3.0.3 (patch — sliced execution & recovery correctness):**
per-child (slice) order accounting — every Upstox child order id persisted,
filled quantity tracked per child and aggregated from child deltas with
duplicate-transition protection (partial-then-cancel credits exactly the
executed quantity); per-child transition identity in the database (primary
key now includes the child order id, with a backward-compatible migration);
UNKNOWN remote-order recovery restores the COMPLETE child list and per-child
fill/status baselines so restart + reconciliation never loses or duplicates a
child; incremental-fill price accounting derives the true incremental basis
(cumulative averages are never re-applied to deltas) so average cost, cash,
fees and realized P&L stay correct for partial fills; and adaptive cadence
— the active routed strategy receives its correct cadence and strategy
changes update execution/evaluation cadence consistently (no hard-coded
Momentum).

**FINAL 3.0.2 (final hardening pass) — execution-path safety (preserved):**
fail-closed multi-instrument routing (no broker-default identity, mismatches
rejected), full lifecycle ladder (RESEARCH → VALIDATION → WALK_FORWARD →
APPROVED + ACTIVE) enforced at the final execution boundary, no qualified
candidate ⇒ no entry (the fallback strategy is diagnostics-only), UNKNOWN
remote orders recovered via the documented intent-derived `tag` lookup
(VERIFIED_MATCH / VERIFIED_ABSENCE / UNRESOLVED — never a blind re-POST),
cancellations never reported CANCELLED without per-child broker confirmation,
and adaptive promotion gated by the complete research evidence policy
(robustness + walk-forward + drawdown + lifecycle approval).
and evolution behind hard validation gates, a versioned lifecycle
(RESEARCH→…→APPROVED, degradation PROBATION→SHADOW→RETIRED), an
event-driven engine with a persisted execution pause, reality-gap/anomaly/
system monitoring with scheduled database backups, an NL command center over
recorded data, daily/weekly/monthly reports with attribution, and a
**PySide6 desktop application** (13 pages — see "Desktop application" below).
LIVE (real-money) trading is **OFF by default** and gated behind a
four-switch opt-in (`MODE=LIVE` + `LIVE_TRADING=true` + `BROKER=UPSTOX_LIVE`
+ `LIVE_CONFIRM`) **and** explicit non-zero hard risk limits; see
[LIVE_TRADING_ENABLEMENT.md](LIVE_TRADING_ENABLEMENT.md). The bare alias
`BROKER=LIVE` still raises, and the sandbox broker refuses live order hosts.
**V2.1 (L-ops)** adds the rails around a live session — token-freshness and
canary entry guards, a stuck-order/reconcile watchdog with a WARNING→CRITICAL
escalation ladder (optional auto-cancel, off by default), daily-loss and
drawdown auto-stop, deduped alerts (durable sink + optional webhook), the
`live-halt` / `live-resume` / `live-status [--audit]` operator commands
(halt always works — even when the configuration is rejected; resume refuses
unless the live opt-in is armed), a whole-system readiness verdict in
`live-preflight` (including audit-trail integrity and the state a fresh engine
would inherit), an offline `live-drill` fire drill for the rails themselves, an
independent audit-trail PnL replay, alert acknowledgement (`live-ack`, so an
unattended CRITICAL becomes an incident of its own), and `GET /api/v2/alerts` ·
`POST /api/v2/alerts/ack` · `GET /api/v2/live/status` — see
[LIVE_OPERATIONS.md](LIVE_OPERATIONS.md).
Verification status: **1117 tests passed / 37 skipped**;
desktop **GUI boot NOT VERIFIED** in the build sandbox (no display server —
source and API contracts verified); authenticated Upstox sandbox/feed flows
**NOT ATTEMPTED** in this environment (no credentials). See
[ARENA_FINAL_VERIFICATION.md](ARENA_FINAL_VERIFICATION.md).

```text
MODE=PAPER
LIVE_TRADING=false
BROKER=LOCAL_PAPER
REAL MONEY = DISABLED (default)
```

**V2 host-classification fix (2026-10-02).** V1 labelled `api-hft.upstox.com`
— Upstox's *live* order host — as a sandbox host and shipped it as the
sandbox default, so a live token placed real orders while the broker, the
audit tables, the dashboard and the reports all said "sandbox". The sandbox
default is now `api-sandbox.upstox.com`, the live host is refused by the
sandbox broker, and real-money routing goes through `UpstoxLiveBroker`
(live audit tables, truthful `mode`/`live` labels, preflight command).

Backtests, validation, and paper trading are simulations. **Historical results are not guarantees of future performance.** A high research score identifies a **selected candidate**, not a strategy guaranteed to make money. Sample prices are explicitly **synthetic**, not actual prices for RELIANCE, any exchange-listed stock, or a cryptocurrency.

## 0. V1 (Arena) — sandbox engine, learning layer, dashboard

V1 adds an **Upstox SANDBOX execution path**, a **24/7 engine**, a **validated
learning layer** and a **dashboard** on top of the intact V0.6.4 platform.
Nothing was removed: LOCAL_PAPER remains the default and the paper pipeline is
pinned by `python -m app paper-test` (17/17 checks, exit 0).

- **Modes.** `BROKER=LOCAL_PAPER` (default) | `UPSTOX_SANDBOX` | `LIVE` is
  **rejected outright** — no live routing exists in V1. Sandbox requires
  `UPSTOX_SANDBOX_ACCESS_TOKEN`; sandbox credentials only ever reach the
  documented sandbox hosts (`api-hft.upstox.com` order APIs +
  `api.upstox.com/v2/order/history` reconciliation), enforced by test.
- **Engine** (`python -m app engine`, requires `BROKER=UPSTOX_SANDBOX` and
  `DATA_SOURCE=upstox`): real V3 feed → strategies → **hard risk gate**
  (outside learning: max position value / open positions / daily loss /
  trades per day) → sandbox orders → documented reconciliation. Deterministic
  intent ids (uuid5 of strategy+symbol+candle+side) persist around each
  submission: **no duplicate orders after reconnect or restart**. An
  emergency STOP flag persists in `engine_state` and halts everything until
  explicitly resumed (dashboard button or engine API). Off-hours the engine
  reconciles and runs the evaluation batch.
- **Sandbox broker** (`app/brokers/sandbox.py`): current documented V3 APIs —
  place (POST api-hft /v3/order/place, slice-aware multi-order response),
  cancel (DELETE /v3/order/cancel; UDAPI100040 already-final treated as
  closed), modify (PUT /v3/order/modify, documented body only), reconcile via
  GET /v2/order/history (delta-based fill application — partial fills credit
  exactly once, partial-then-cancel keeps the executed quantity). 401/403
  **fail closed**: the broker disables itself; no further requests. Tokens
  never appear in logs or exceptions. Every order + transition persists in
  SQLite (`sandbox_orders`, `sandbox_transitions`).
- **Learning layer** (`app/learning/`): journal (signals, regimes, trade
  outcomes), transparent observable-data regime labels, rolling-batch
  metrics, and **versioned candidates that promote only on strictly better
  OUT-OF-SAMPLE expectancy** (chronological train/OOS split through the
  existing backtest engine). Learning cannot modify risk limits, auth,
  broker safety or mode flags — enforced by test.
- **Dashboard** (`python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000`, then `/dashboard`): mode badge
  (PAPER vs SANDBOX, "LIVE: NOT ENABLED IN V1"), feed health, equity/P&L/
  drawdown, signals, positions, sandbox orders, model version, recent
  events, and an **EMERGENCY STOP** button; all behind the existing bearer
  token (`API_TOKEN`).
- **Sandbox credential check:** `python -m app sandbox-test` (read-only
  documented-API probe; `--place-order` submits one small real sandbox order
  on explicit request). Exit codes: 0 verified, 1 rejected, 3 no credentials.
  External driver: `verification_v061_live/real_sandbox_order_check.py`.

## 0.1 V2.1 (Arena) — market intelligence + adaptive research platform

V2.1 upgrades V1.0.0 in place (additive migrations only; every V1 file, test
and behavior preserved — 232 tests total):

- **Market universe** — `python -m app market-universe sync|status|audit`
  downloads the OFFICIAL Upstox instrument files (complete.json.gz or
  per-exchange NSE.json.gz), stores instruments (key, symbol, name, exchange,
  segment, type, ISIN, lot/tick, expiry/strike/options fields, state,
  source, timestamps) keyed by `instrument_key`, and diffs new/changed/
  removed instruments per sync. Verified against the live public file
  (79,346 NSE instruments in-suite).
- **First run** — `python -m app initialize` creates directories, database
  (schema v1→v3 migrations), config defaults and instrument metadata.
  Idempotent and offline-safe (a failed sync is recorded honestly).
- **Historical data** — `python -m app history sync|status|audit` uses the
  documented Historical Candle **V3** API
  (`/v3/historical-candle/{key}/{unit}/{interval}/{to}/{from}`): chunked per
  documented limits (minutes≤15min→1 month, >15min & hours→1 quarter,
  days→1 decade, weeks/months unlimited), availability clamped (minutes/
  hours from 2022, d/w/m from 2000), bounded retries with rate-aware
  backoff, and **resumable downloads** — chunk progress is persisted, so an
  interrupted download continues where it stopped and covered windows are
  never re-fetched. Candles are deduplicated by timestamp. Intraday V3
  endpoint supported (`history sync --interval minutes:1` + intraday merge).
- **Storage** — SQLite for state/metadata; OHLCV in partitioned gzip-CSV
  (or Parquet when pyarrow is installed; `PARQUET_ENABLED`). Partitions are
  atomic (tmp+rename); source data is never edited in place.
- **Data quality** — OHLC relationships, ordering, duplicates, cadence gaps
  (intraday only; weekend/holiday gaps are NOT fabricated), suspicious
  jumps → VALID/WARNING/INVALID/MISSING/UNVERIFIED, recorded as quality
  events. Coverage tracks first/last, records, gaps, completeness
  (honestly n/a for calendar-based units) per instrument/unit/interval.
- **Datasets** — `python -m app dataset build|audit`: versioned manifests of
  store partitions + sha256 hashes + source refs; audit re-verifies hashes
  and detects tampering.
- **Universe selection** — `python -m app universe screen`: explicit filters
  (exchange, segment, type, history depth, price, volume, custom lists).
  "All known instruments" stays separate from the eligible research
  universe; nothing is auto-traded.
- **Adaptive intelligence** — RegimeEngine (observable features: trend,
  volatility, momentum, volume trend, time-of-day; regime-change events),
  StrategyRouter (score = OOS expectancy + robustness + confidence; negative
  OOS expectancy is never tradable; in-sample return is never scored),
  ExperimentEngine (walk-forward + locked-test evaluation + robustness;
  versioned candidates; promotion only on locked-test improvement over the
  baseline), TradeIntelligence (strategy×regime aggregates), and
  ShadowStrategyEngine (candidates trade hypothetically; orders impossible
  by construction). Learning runs in **scheduled batches** (not per trade)
  and can change only strategy selection, parameters and model versions —
  tests prove risk limits/auth/modes are untouchable by learning code.
- **24/7 engine** — unchanged V1 architecture + regime/shadow hooks, daily
  snapshots, streaming-universe guard (explicit key list, hard cap 50,
  subscribing to everything is refused) and continuity merge
  (historical < intraday < live precedence, source-labeled).
- **Desktop app** — PySide6 (`pip install -r requirements-desktop.txt`,
  `python -m desktop`) local PC UI over the same authenticated API:
  Overview, Live Market, Trades, Performance, Strategies, Learning,
  Experiments, Market Data, Risk, Logs (human-readable activity + raw
  Advanced Logs), Settings; mode badge (LOCAL PAPER / UPSTOX SANDBOX ·
  LIVE DISABLED) and a persisted EMERGENCY STOP button. The UI is
  presentation-only — it cannot bypass the hard risk layer.
- **Weekly report** — `python -m app report --format markdown|pdf`
  (stdlib PDF writer, no dependencies): capital, P&L, return, drawdown,
  trades, regimes, learning events, risk events, data status — every line
  labeled OBSERVED / CALCULATED / MODEL INTERPRETATION.
- **Maintenance** — deterministic scheduler (universe sync daily, coverage
  refresh, dataset refresh weekly) with backoff; `python -m app maintenance`.

## 0.2 FINAL (Arena 3.0.0) — autonomous research platform, production hardening

Arena FINAL upgrades V2.1.0 in place (additive; all V1/V2.1 features, data,
migrations, tests and safety boundaries preserved — 318 tests total). The
platform now continuously DISCOVERS, VALIDATES and MONITORS strategies while
hard risk stays outside learning:

- **Schema v4** (versions [1,2,3,4], migration-only): strategy_lineage,
  strategy_lifecycle, regime_history, opportunity_scans, research_questions,
  system_status, reality_gap, confidence_calibration.
- **StrategyDiscoveryEngine + evolution** (`python -m app discover`): bounded
  candidate search over 7 strategy families; every candidate must pass
  chronological-split evaluation → positive OUT-OF-SAMPLE expectancy
  (≥3 OOS trades) → robustness ≥0.25 → walk-forward; in-sample return never
  promotes anything. Versioned lineage with mutation/recombination; children
  validate independently; history is append-only (RETIRED kept forever).
- **Lifecycle** (`python -m app lifecycle status|promote|degrade`):
  RESEARCH→VALIDATION→WALK_FORWARD→SHADOW→CANARY→APPROVED (no skipped
  stages); degradation ACTIVE→PROBATION→SHADOW→RETIRED with automatic
  recovery back to ACTIVE. Only APPROVED strategies are tradable.
- **OpportunityScanner** (`python -m app scan`): quality = validated edge
  fraction − cost drag − liquidity/heat penalties; no evidence → no
  opportunity; top-k output is research-only.
- **Event-driven engine**: cadence (the only unconditional trigger),
  significant move, volume spike, regime change, order events. Safe component
  pause (PauseGate) persists across restarts; paused execution records
  EXECUTION_PAUSED_SKIP instead of trading.
- **Monitoring**: RealityGapMonitor (backtest→walk-forward→paper→sandbox;
  live is a recordable NAME only — no code path produces it), AnomalyDetector
  (order-frequency spike, losing streaks, feed failures, broker mismatch,
  missing data — auto-pauses execution on dangerous anomalies), SystemMonitor
  (CPU/memory/disk/database health + alerts), API latency tracked per request.
- **Recovery** (`python -m app backup backup|verify|restore`): online SQLite
  backups, integrity verification, restore refuses unverified/corrupt files.
- **Research tooling**: DecisionReplay (`replay` — only at-decision-time
  rows), P&L attribution, ConfidenceCalibration (bounded discount ∈
  [0.5,1.0]), MarketMemory (k-NN regime similarity with an explicit
  no-future-guarantee disclaimer), ResearchDirector (auto-generated research
  questions + schedulable experiments), CommandCenter (`ask` — natural-
  language answers computed from stored rows only, honestly labeled
  OBSERVED/CALCULATED/MODEL INTERPRETATION/HONEST).
- **Maintenance** (`python -m app maintenance`): V2.1 jobs + anomaly scan,
  system snapshot, research survey, reality-gap review, database backup; the
  24/7 engine runs the same schedule inline.
- **Reports** (`python -m app report --period daily|weekly|monthly`): daily/
  weekly/monthly with attribution + anomalies; dependency-free PDF.
  API serves SVG charts (equity/drawdown/attribution).
- **API v2 FINAL**: /api/v2/command_center, portfolio, attribution,
  research_lab, learning_detail, market_intelligence, charts/*.svg, report.
- **Desktop (13 pages)**: Command Center (NL query box), Live Market,
  Portfolio (+attribution bars), Trades, Performance (equity + drawdown
  charts), Strategies, Research Lab, Learning, Market Intelligence, Risk,
  Logs, Reports, Settings — QPainter charts, no extra dependencies;
  presentation only (STOP/RESUME are the sole writes). Windows launcher:
  `desktop/run_windows.bat`; requires PySide6 (`requirements-desktop.txt`).
  DESKTOP STATUS: source-verified (Qt cannot boot in this sandbox — no
  display/libxkbcommon); see ARENA_FINAL_VERIFICATION.md.
- **Security**: paper by default; real money requires the four-switch opt-in
  plus non-zero risk limits, and the sandbox broker refuses live hosts
  (`BROKER=LIVE` alone raises); `tools/security_scan.py` scans first-party code for
  hardcoded secrets and packaging hygiene (clean); .env gitignored; API auth
  unchanged (Bearer token, all v2 pages 401 without it).
- **Performance**: bounded reads everywhere hot (scanner lookback window,
  anomaly/event windows capped at 1000 rows, instrument keys capped, lineage
  capped); rolling 100-request latency buffer; nothing loads the whole
  market DB into RAM.

## 0.3 Desktop application (FINAL)

Arena FINAL ships a **PySide6 desktop application** (`desktop/`, launched with
`python -m desktop` or `desktop/run_windows.bat` on Windows). It is a local
control/monitoring interface that connects to the existing Arena backend
(`ARENA_BACKEND_URL` + `ARENA_API_TOKEN`); the engine, risk layer and
database stay in the core. **The UI is presentation-only**: the only writes
are the persisted emergency STOP/RESUME actions. Pages (13):

- **Command Center** — natural-language Q&A over stored data ("Why did Arena
  lose money today?"), answers labeled OBSERVED/CALCULATED
- **Live Market** — pipeline/connection/market status/regime
- **Portfolio** — exposure/concentration/allocation + NET P&L-by-strategy bars
- **Trades** — recorded fills (paper/sandbox labeled)
- **Performance** — equity + drawdown charts (QPainter), realized/unrealized
- **Strategies** — promoted versions and discovery candidates with parameters
- **Research Lab** — experiments, research questions, lineage
- **Learning** — calibration, regime history, shadow performance
- **Market Intelligence** — universe, coverage, regimes (+no-guarantee disclaimer)
- **Risk** — hard limits (read-only) and the emergency STOP/RESUME control
- **Logs** — human-readable activity translation plus raw event stream
- **Reports** — daily/weekly/monthly reports loaded from the API
- **Settings** — backend/mode identity and configuration guidance

Requirements: `pip install -r requirements-desktop.txt` (PySide6 only; the
core never needs it). **Verification honesty:** the desktop logic (view
models, API client contract, 13-page definition) is unit-tested — 318 tests
pass — but the Qt GUI itself has **NOT been booted/verified** in the build
sandbox (no display server available). Try it locally with a running backend.

## 1. Scope and implementation

| Version | Implemented |
|---|---|
| V0.1 | Strict CSV import, explicit cleaning, UTC normalization, OHLCV validation, calendar-aware gap detection, content-addressed CSV cache, read-only public historical downloader |
| V0.2 | Seven configurable strategies, explicit signals, reusable trailing indicators, strategy registry |
| V0.3 | Shared candle execution/accounting, next-bar orders, fees/spread/slippage, long-only sizing, limit orders, liquidity-limited fills, stops/take-profit, histories and metrics |
| V0.4 | Cartesian strategy/configuration/symbol/timeframe tournament, bounded composite scoring, reproducible grids |
| V0.5 | Chronological train/validation/test selection, one-use holdout guard, rolling walk-forward selection, parameter/cost/period stress diagnostics |
| V0.6 | Persistent paper account, replay and read-only WSS feeds, reconnect/heartbeat, lifecycle control, SQLite checkpoints, authenticated FastAPI endpoints |
| V0.6.1 | Real external market DATA (Upstox V3 WebSocket, read-only) into the unchanged paper pipeline: authorized connect, binary protobuf decode via the official schema, completed-candle building, data-quality guards, re-auth/resubscribe backoff, feed health in `/market/status` |
| V0.6.2 | Production-hardened Upstox V3 data adapter: normalized `MarketEvent` layer, `UpstoxProtobufDecoder`, market-status gating (connection ≠ tradable market), snapshot/live/market-info handling, structured lifecycle logs, risk-decision counters, `python -m app` commands (`upstox-test`, `market-stream`, `paper-live`, `upstox-login`), official OAuth 2.0 login helper, `UPSTOX_*` env aliases |
| V0.6.3 | Behavioral credential-type verification BEFORE connecting: `verify_feed_credential` GET probe with explicit verdicts (feed_ok/rejected/forbidden/…), guidance distinguishing standard OAuth login tokens from the read-only 1-year Analytics Token, fail-closed CLI/harness (no connection on unverified credentials) |
| V0.6.4 | Offline verification feed: deterministic synthetic `MarketEvent` provider through the SAME normalization/candle/gate pipeline, `python -m app paper-test` (pure standard library, zero third-party dependencies — Termux-friendly) with pinned expected results, restart/replay determinism, and safe refusal without credentials |
| V1.0 (Arena) | Upstox SANDBOX execution (`UpstoxSandboxBroker`, current documented V3 place/cancel/modify + order-history reconciliation, fail-closed auth, persisted orders/transitions), 24/7 engine with hard-risk gating + restart-safe duplicate suppression + persisted emergency STOP, validated learning layer (regime journal, rolling batches, OOS-gated versioned promotion), dashboard with PAPER/SANDBOX badges. LIVE remains rejected at Settings level; 170 tests |

The standard-library research core needs **no pandas, NumPy, GPU, Docker, or database server**. FastAPI/Pydantic, httpx, websockets, dotenv, and pytest are the principal optional runtime/development dependencies. Charts ship three ways: dependency-free SVG charts served by the API, charts embedded in MD/PDF reports, and the desktop application (QPainter) — a real local frontend, not a replacement for the engine; the desktop app is UI-only and cannot bypass risk.

See [VERIFICATION.md](VERIFICATION.md) for the commands actually exercised, test count, and limits of verification. The code targets Python **3.11+**; this delivery was tested on Linux with Python 3.13.14.

## 2. Quick start

Run all commands from the `trading_bot/` directory after extracting the archive.

### Linux/macOS

```bash
python3 --version                    # Must be 3.11 or newer
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python -m pytest -q
python run.py --mode backtest
```

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m pytest -q
python run.py --mode backtest
```

Use an installed newer Python if `py -3.11` is unavailable. If PowerShell activation is restricted, invoke `.\.venv\Scripts\python.exe` directly. Windows support is designed in (including file locking) but was not device-tested for this delivery.

`requirements.txt` uses compatible version ranges. `requirements-lock.txt` records the tested Linux environment's relevant packages for repeatability; it is not a promise of wheel availability on every platform. SQLite tables initialize automatically at first use.

### Offline/lightweight core

Once the project is present, these do not require third-party packages:

```bash
python run.py --mode backtest
python run.py --mode tournament
python run.py --mode validate
python run.py --mode walk-forward
python run.py --mode robustness
python run.py --mode paper --source replay
```

Without `python-dotenv`, `.env` loading is skipped: **export environment variables in your shell instead**. API serving, HTTP downloads, live WSS connections, and the full test suite require their corresponding dependencies. Do not use this fallback to assume an unread `.env` has taken effect.

## 3. Exact commands for each stage

The four bundled datasets each contain 1,600 synthetic candles. Recreate them deterministically with:

```bash
python scripts/make_sample_data.py
```

### V0.1 — import and download

Import/cache/normalize the example CSV:

```bash
python scripts/download_data.py
# Saves data/imported.csv by default
```

Import your own timezone-aware CSV, explicitly allowing sorting/exact deduplication:

```bash
python scripts/download_data.py --provider csv \
  --input data/my_5m.csv --symbol RELIANCE --timeframe 5m \
  --clean --output data/reliance_5m.csv
```

Actual public crypto historical data, when reachable and permitted in your jurisdiction:

```bash
python scripts/download_data.py --provider binance \
  --symbol BTCUSDT --timeframe 1h \
  --start 2025-01-01T00:00:00Z --end 2025-02-01T00:00:00Z \
  --output data/btcusdt_1h.csv
```

This uses **only HTTPS GET `/api/v3/klines`**, excludes unfinished candles, paginates, throttles requests, retries HTTP rate-limit/server responses with bounded backoff, and caches completed historical ranges. It uses no account or API key. Network failures propagate rather than creating fake candles. Provider outages, geographic restrictions, and terms of use remain your responsibility.

### V0.2/V0.3 — backtest

```bash
python run.py --mode backtest
python scripts/run_backtest.py --strategy momentum --parameters '{"period":20,"threshold":0.02}'

python run.py --mode backtest --csv data/sample.csv --symbol DEMO --timeframe 1D \
  --strategy moving_average --parameters '{"fast_period":10,"slow_period":30}' \
  --annualization 365 --output results/ma_10_30
```

Outputs: `result.json`, `equity.csv`, `trades.csv` (when trades exist), `daily_returns.csv`, and a self-contained `equity.svg`. JSON also contains orders, fills, position history, execution parameters, warnings, and identifiers. Backtest results are recorded in SQLite.

For equity intraday data, explicitly select the session example and the annualization convention:

```bash
python run.py --mode backtest --csv data/reliance_5m.csv --symbol RELIANCE --timeframe 5m \
  --calendar nse-session --annualization 252 --strict-gaps
```

The NSE session example is **weekday 09:15–15:30 Asia/Kolkata**, not a complete exchange calendar. Supply holidays via `SessionCalendar(holidays={...})` in your application. Do not use it for daily UTC-midnight bars. No execution happens on bars outside the configured session.

### V0.4 — tournament

```bash
python run.py --mode tournament
python scripts/run_tournament.py --grids data/example_grids.json

# All 7 strategies, 23 parameter configurations, 2 symbols × 2 timeframes = 92 runs
python run.py --mode tournament --datasets data/tournament_datasets.json \
  --scoring data/example_scoring.json --output results/tournament
```

Dataset manifest paths are relative to the manifest. Every `(symbol, timeframe)` needs actual data at that timeframe. No implicit resampling occurs. Runs are sequential for predictable mobile memory usage; results retain summary metrics, not every equity curve in RAM. Each full backtest is saved in SQLite. Large research databases can become sizeable; use separate experiment databases and archive intentionally.

Parameter grid example:

```json
{
  "moving_average": {"fast_period": [10, 20], "slow_period": [30, 50]},
  "rsi": {"period": [14], "lower": [20, 30, 35], "upper": [70, 80]},
  "bollinger": {"period": [20, 50], "std_dev": [2, 2.5]}
}
```

Invalid grids fail, rather than silently dropping configurations. There is a 10,000-configuration expansion cap. The default tournament's top row is **in-sample research only**, not automatically a validated candidate. There is no tournament-to-live/paper auto-deploy action.

### V0.5 — chronological validation

```bash
python run.py --mode validate --splits 0.6 0.2 0.2 --shortlist 5
python run.py --mode walk-forward --windows 360 120 120 120 --shortlist 5
python run.py --mode robustness --strategy moving_average \
  --parameters '{"fast_period":20,"slow_period":50}'
```

`--windows` means **training bars, validation bars, test bars, step bars**. Use `--grids`, `--scoring`, `--csv`, `--symbol`, `--timeframe`, and `--database` to make experiments explicit.

Selection procedure:

1. Split in timestamp order. No shuffling.
2. Rank all configurations using training data only.
3. Shortlist the top N training candidates.
4. Evaluate that shortlist on validation data only.
5. Select using the composite **validation score**.
6. Freeze the strategy/parameters.
7. Evaluate the unseen test once through `LockedTest`.
8. Publish the unseen results and warnings, including disappointing ones.

Earlier candles can warm up indicators, but they create **no inherited positions or pre-evaluation orders**. Each evaluation begins with cash; the first evaluation candle may generate the first order for the following candle. `LockedTest` prevents a second evaluation within that object; it cannot prevent a researcher reopening the same files in another run. After viewing test results, treat that period as seen. Keep experiment records and reserve new holdouts if you revise the search.

Walk-forward repeats training discovery and validation selection at every fold. Test windows cannot overlap (`step >= test`). Earlier test candles may legitimately enter *later* training once they are historical. Freeze grids/windows before looking at fold outcomes. The aggregate curve geometrically links independent marked test returns and computes daily risk metrics on that series. **It is not an executable continuous portfolio:** folds restart flat, terminal open inventory is marked, and liquidation/re-entry costs at fold boundaries are not modeled. Aggregate CAGR is omitted; gaps between test windows are not interpolated.

Robustness diagnostics independently test parameter perturbations, increased commission, increased slippage/spread, and three chronological subperiods (labeled up or down/flat from observed returns). These are diagnostic condition proxies, **not** an implemented market-regime detector. Run this on discovery/validation periods while developing; do not tune repeatedly to an already-inspected test holdout. CLI robustness operates on the entire CSV supplied, so choose the input deliberately.

### V0.6 — paper replay

```bash
python run.py --mode paper
python scripts/run_paper.py --source replay --delay 0.01

# A distinct fresh paper account
python run.py --mode paper --source replay \
  --database sqlite:///results/my_paper_account.db --output results/my_paper_account
```

Replay finishes automatically. Market orders become eligible on the next candle. Paper and backtest execution share the same accounting code; their deterministic results match under the same configuration and data.

A paper account persists across restarts. Replaying the same source with the same selection skips candles earlier than the committed cursor. A completed replay is **not reset automatically**. Use a new database filename for an independent experiment. Graceful stop cancels pending orders but **does not liquidate open positions**. Restart restores cash, positions, marks, orders, fills, trades, last-candle cursors, and indicator history.

### V0.6 — live data, simulated orders

Public Binance crypto example in a POSIX shell:

```bash
CURRENCY=USDT DEFAULT_SYMBOL=BTCUSDT DEFAULT_TIMEFRAME=1m \
python run.py --mode paper --source binance \
  --database sqlite:///results/btcusdt_paper.db --output results/btcusdt_paper
```

On Windows, set equivalent `$env:CURRENCY`, `$env:DEFAULT_SYMBOL`, and `$env:DEFAULT_TIMEFRAME` values before running. Quote currency must match the symbol suffix; an INR account is not silently relabeled as USDT. This adapter is **not** an NSE/Indian-equities feed. Using live prices does not enable real-money trading.

Only **closed candles** are emitted. The initial indicator warmup is accumulated from incoming candles; no hidden historical backfill is performed. Long lookbacks on daily feeds take a long time to warm up. Completed-candle paper fills are confirmed at candle receipt: this is candle-based simulation, not tick-accurate real-time exchange execution.

Generic authenticated data feed:

```dotenv
DATA_SOURCE=generic
DATA_WS_URL=wss://your-data-provider.example/candles
MARKET_DATA_TOKEN=
DEFAULT_SYMBOL=RELIANCE
DEFAULT_TIMEFRAME=5m
MARKET_CALENDAR=nse-session
```

Set the token privately. The `.example` hostname is a placeholder; a real compatible provider is needed. Run `python run.py --mode paper --source generic`.

Protocol sent by the generic adapter:

```json
{"type":"subscribe","symbols":["RELIANCE"],"timeframe":"5m"}
```

Expected closed-candle event (OHLCV numeric values may also be parseable numeric strings):

```json
{
  "closed": true,
  "timeframe": "5m",
  "candle": {
    "timestamp": "2025-01-06T09:15:00+05:30",
    "open": 1200,
    "high": 1205,
    "low": 1199,
    "close": 1203,
    "volume": 10000,
    "symbol": "RELIANCE"
  }
}
```

`{"type":"heartbeat"}` is accepted. Authentication is an optional `Authorization: Bearer ...` header, never a query-string credential. Only WSS URLs without embedded user/password/query secrets are accepted. Arbitrary broker WebSocket protocols are **not** plug-and-play: subclass the adapter's `subscription()` and `decode()` methods for the licensed data provider you use.

WebSocket transport ping/pong heartbeats run every 20 seconds with a 20-second timeout. Idle receive periods trigger an explicit liveness ping. Network disconnects retry with bounded exponential backoff and jitter. Malformed data fails safely; errors are reported by exception class, not by credential-bearing payloads.

On a detected missing-candle gap, pending orders are cancelled, history is reset, and new strategy entries are paused. Existing positions are marked and risk exits can still be simulated on received bars. **There is no claim about unobserved prices during the gap.** Backfill/review externally and explicitly resume, or restart with a repaired historical workflow. Stale/out-of-order live candles are rejected; exact duplicate closed candles are ignored. Future/unfinished live candles fail validation.

### V0.6.1 — real Indian market data (Upstox, read-only), simulated orders

V0.6.1 connects the **existing** paper pipeline to a real external market-DATA API
(Upstox Market Stream Feed V3, per the official documentation and the official
`MarketDataFeed.proto` shipped in `app/data/upstox_proto/`). Only DATA is real.
Execution remains 100% simulated:

```
REAL MARKET DATA → UpstoxStream adapter → normalization/validation → candle builder
→ completed candle ONLY → existing strategy engine → existing risk engine → PaperBroker
```

Upstox order endpoints are never imported or called. `MODE=PAPER` and
`LIVE_TRADING=false` stay enforced; supplying broker credentials changes nothing.

Setup (your own Upstox developer account creates the access token; it is stored
only in `.env` and is never logged or returned by the API):

```bash
cp .env.example .env   # then set, at minimum:
#  DATA_SOURCE=upstox
#  MARKET_DATA_SYMBOLS=NSE_EQ|INE002A01018   # ONE liquid instrument first
#  MARKET_DATA_TIMEFRAME=1m                  # must equal DEFAULT_TIMEFRAME
#  MARKET_DATA_MODE=ltpc                     # or full (official 1m/30m bars)
#  MARKET_DATA_ACCESS_TOKEN=<your token>     # never commit this file
python run.py --mode paper --source upstox
```

Behavior and safety properties (implemented and tested):

- Authentication uses the documented `GET /v3/feed/market-data-feed/authorize`
  call; failures fail closed (no retry storm). Network drops reconnect with
  bounded exponential backoff, fresh authorization and resubscription.
- The subscription frame is the documented binary JSON; feed frames are decoded
  with the official protobuf schema. Only **completed** candles reach the
  strategy: identical completion semantics to backtest/replay.
- Data-quality guards: duplicate and out-of-order ticks are dropped with
  counters; invalid prices/volumes/timestamps are rejected; missing candles are
  never synthesized (gap → new orders paused, pending orders cancelled);
  stale feeds are flagged.
- Upstox instrument keys map to internal symbols (`NSE_EQ|INE002A01018` →
  `NSE_EQ-INE002A01018`). Keys with spaces (e.g. index keys) are rejected
  clearly rather than silently mangled.
- `/market/status` reports `connection_status`, `authenticated`, `subscribed`,
  `last_message_time`, `last_candle_time`, `data_gap_detected`, `stale_feed`,
  `paper_mode`, reconnect and latency counters — never any credential.
- Epoch unit (seconds vs milliseconds) is inferred by magnitude until validated
  against a real authenticated feed; see `V061_VERIFICATION.md` for what was
  and was not verifiable without credentials.

Upstox access tokens are short-lived (typically daily); refresh them through
the provider's official login/token flow. See `V061_VERIFICATION.md` for the
executed verification, including the honest list of what remains unverified.

### V0.6.2 — market-status gating, normalized events, CLI, OAuth helper

V0.6.2 hardens the V0.6.1 adapter without changing the architecture:

- **Market status first.** The official `marketInfo` frame is decoded into the
  normalized `MarketEvent` layer (`app/data/market_events.py`). With the default
  `require_market_status=True`, completed candles are held back until the
  relevant segment reports `NORMAL_OPEN`: a connected WebSocket is **not** a
  tradable market. While closed/pre-open, candles are counted as suppressed and
  NO strategy execution happens. `/market/status` exposes `market_status`,
  `market_open` and the raw segment map.
- **Clean decoder abstraction.** `UpstoxProtobufDecoder` converts official V3
  protobuf frames into provider-neutral `MarketEvent`s (snapshot / live /
  market_info kinds; `ltp`, `close_price`, `last_traded_quantity`, `bid`, `ask`
  populated only when the feed mode actually carries them).
- **Structured logs** (`bot.upstox` component, JSONL): `UPSTOX_CONNECT`,
  `UPSTOX_AUTH_SUCCESS`, `UPSTOX_AUTH_FAILURE`, `UPSTOX_SUBSCRIBE`,
  `UPSTOX_MARKET_STATUS`, `UPSTOX_TICK`, `UPSTOX_DECODE_ERROR`,
  `UPSTOX_RECONNECT`, `UPSTOX_RESUBSCRIBE`, `CANDLE_COMPLETED`,
  `STRATEGY_SIGNAL`, `RISK_DECISION`, `PAPER_ORDER`, `PAPER_FILL`,
  `PAPER_REJECT`, `SYSTEM_RECOVERY`. Credentials are never logged.
- **Health/latency**: `/market/status` adds `last_tick`, `connect_latency_ms`,
  `suppressed_by_market_status`; `/paper/status` adds `risk_counters`
  (actionable signals vs orders created vs risk-skipped) and per-stage
  processing latency.
- **CLI** (paper-only): `python -m app upstox-test` (real auth + connectivity
  proof), `python -m app market-stream` (print normalized real events),
  `python -m app paper-live` (full pipeline), `python -m app upstox-login`
  (official OAuth 2.0 authorization-code helper; exchanges the code via the
  documented token endpoint and stores the token in the gitignored `.env` with
  owner-only permissions — the token is never printed or logged).
- **Environment aliases**: `UPSTOX_ACCESS_TOKEN` and `UPSTOX_INSTRUMENT_KEYS`
  are accepted with the same meaning as `MARKET_DATA_ACCESS_TOKEN` /
  `MARKET_DATA_SYMBOLS` (explicit `MARKET_DATA_*` values win).
- **Sandbox (investigated, per current docs)**: the Upstox sandbox covers
  ORDER APIs only (place/modify/cancel, V3). **Market Data Feed V3 is NOT
  sandbox-enabled** in the current documentation, so no sandbox market-data
  path exists and none was simulated. Real-session verification therefore
  requires a live-market login token; see `V062_VERIFICATION.md`.

Upstox access tokens are short-lived (typically daily); refresh them through
the provider's official login/token flow (`python -m app upstox-login`).

### V0.6.3 — verify the credential type before connecting

Tokens are opaque strings, so V0.6.3 never *assumes* a credential type. Before
any WebSocket connection, `verify_feed_credential` (in `app/data/upstox_auth.py`)
performs the documented read-only `GET /v3/feed/market-data-feed/authorize`
probe and returns an explicit verdict:

- `feed_ok` — credential authorized for Market Data Feed V3 (proven, not assumed).
- `rejected` (401) / `forbidden` (403) — with guidance: a **standard OAuth login
  access token** (`python -m app upstox-login`) is the documented path for the
  feed; the **Analytics Token** (1-year, strictly read-only GET APIs, which the
  current docs list as including a *Websocket* category) may work for the feed
  but must be proven with your own token — never assumed.
- `unreachable` / `invalid_input` / `unexpected_response` — no connection.

`python -m app upstox-test` and `verification_v061_live/real_upstox_check.py`
both run this gate first: an unverified credential results in **no connection
attempt**, a structured verdict, and a non-zero exit. Wrong credentials never
block the paper pipeline silently — they stop it loudly with actionable
guidance (and the adapter carries the same guidance in its auth-failure logs).

### V0.6.4 — offline pipeline verification (Termux-friendly, no credentials)

`python -m app paper-test` runs the complete pipeline **entirely offline** with
**zero third-party dependencies** (pure standard library — no fastapi/pydantic/
httpx/websockets/protobuf; ideal for Android Termux or any minimal Python
3.11+):

```
synthetic feed → normalized MarketEvent → shared MarketEventApplier
→ LiveCandleBuilder (completed candles ONLY) → market-status gate
→ momentum strategy → risk engine → PaperBroker → SQLite → report
```

- The deterministic scenario (fixed base epoch, virtual clock, embedded
  duplicate tick, one candle completed while the market is still CLOSED)
  produces pinned expected results: `[100.5, 101.0, 101.5, 102.0, 101.0,
  100.75]` closes, exactly BUY→SELL, 2 fills, 1 closed trade, realized P&L
  `-362.41536614602916`, final equity `99637.58463385397`.
- Verified in-command: no duplicate orders, correct position accounting,
  realized P&L, equity identity, market-status gating (suppressed counter),
  restart/replay determinism (cursor duplicate ignored; continuation equity
  identical; zero new orders), and a second independent run with identical
  economics.
- The synthetic provider emits the same `MarketEvent` objects as the real
  Upstox V3 adapter and folds them through the same `MarketEventApplier` —
  it is clearly labeled `synthetic-offline` and NEVER substitutes for real
  Upstox verification. The Upstox adapter remains the real provider adapter.
- `--database` defaults to an isolated temp file, so your trading database is
  never touched. MODE=PAPER / LIVE_TRADING=false are enforced as always.

## 4. API and bot control

Generate a private token locally:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put that value in your local `.env` as `API_TOKEN=...`. Do not commit or share it. The empty example token deliberately disables all application-data/control endpoints with HTTP 503. `/health` and the schema/docs remain available without authentication.

```bash
python run.py --mode api --host 127.0.0.1 --port 8000
# Equivalent ASGI factory; set settings via the environment/.env
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Use **one worker** and no development reload for a paper account. The API process itself owns that account's writer lock, even when its bot is stopped. Do not start an independent paper CLI process against the same database. Use the API control endpoints, or use separate databases for distinct paper accounts.

A small public landing page is at `/`; it contains no account data. Documentation: `http://127.0.0.1:8000/docs`. Health:

```bash
curl http://127.0.0.1:8000/health
```

For examples below, export `API_TOKEN` in your terminal to match `.env` (dotenv loading in the Python process does not export it into your shell).

```bash
curl -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/strategies

curl -X POST http://127.0.0.1:8000/backtests/run \
  -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"dataset":"sample.csv","symbol":"DEMO","timeframe":"1D","strategy":"moving_average","parameters":{"fast_period":10,"slow_period":30},"annualization":365}'

curl -X POST http://127.0.0.1:8000/paper/start \
  -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"source":"replay","dataset":"sample.csv","symbol":"DEMO","timeframe":"1D","strategy":"moving_average","replay_delay":0.05}'

curl -X POST -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/paper/pause
curl -X POST -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/paper/resume
curl -X POST -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/paper/stop
```

| Endpoint | Purpose |
|---|---|
| `GET /health` | Version, health, immutable paper-only safety status |
| `GET /market/status` | Bot/connection state, selected strategy, symbols, current signals, sanitized error |
| `GET /market/datasets` | Available local CSV filenames |
| `GET /strategies` | Registry, defaults, supported signals |
| `GET /backtests?limit=100&offset=0` | Saved research summaries |
| `POST /backtests/run` | Validated synchronous local-data backtest |
| `GET /backtests/{id}` | Complete saved result, metrics, curves, fills, trades |
| `GET /tournaments?limit=20` | Stored tournament/validation/walk-forward/robustness results |
| `GET /paper/status` | Controller state and latest signals |
| `GET /paper/account` | Cash, equity, realized/unrealized/daily P&L, drawdown, fees, simulated impact costs |
| `GET /paper/positions` | Open weighted-average positions with current marks |
| `GET /paper/orders?limit=100` | Recent simulated orders |
| `GET /paper/trades?limit=100` | Recent realized closing fills |
| `POST /paper/start` | Start replay or configured live-data paper process |
| `POST /paper/stop` | Cancel pending orders and stop gracefully; preserve inventory |
| `POST /paper/pause` | Stop new signal orders; cancel pending entries; continue received-bar risk handling |
| `POST /paper/resume` | Explicitly allow strategy signals again |

Paper-start timeframe must match `DEFAULT_TIMEFRAME`. Paper capital/cost/currency/calendar settings come from server configuration, not arbitrary per-request account resets. Changing a saved account's execution settings or currency/timeframe/calendar requires a new database. Changing strategy/source with open inventory or pending orders is rejected.

States: `STOPPED → STARTING → RUNNING ↔ PAUSED`; failures enter `ERROR`. Connection state (`CONNECTING`, `CONNECTED`, `RECONNECTING`, `DISCONNECTED`) is separate; `RUNNING` does not imply a fresh feed. EOF in replay stops the controller. SIGINT/SIGTERM in the CLI or server shutdown closes the stream and checkpoints state. Client stop controls the **bot task**, not the API server process.

Security/size behavior:

- Bearer authentication uses constant-time comparison; no config or secret endpoint.
- Unknown fields such as `live_trading` are rejected, rather than ignored.
- Dataset requests are filename-only, rooted under `data/`, and cannot escape via path traversal/symlinks.
- API backtests are bounded to 5 MB / 10,000 candles and one active research job at a time. Use the CLI for large work.
- Input failures are 422; missing results/files are 404; busy research is 409; unexpected failures return a generic 500.
- Queries are parameterized. API account reads are serialized with the async paper task, and SQLite access has a thread lock.
- No permissive CORS configuration. Put TLS, request-size/rate limits, and access control at a reverse proxy before remote use.
- `/docs` exposes the API contract, not account secrets. The API is not an Internet-hardened multi-user service.

## 5. Data contract and cleaning

Required CSV header:

```csv
timestamp,open,high,low,close,volume,symbol
2025-01-06T09:15:00+05:30,1200,1205,1199,1203,10000,RELIANCE
2025-01-06T09:20:00+05:30,1203,1206,1201,1204,12000,RELIANCE
```

Rules:

- Timestamp is the **candle opening timestamp**, with an explicit timezone. Internally normalized to UTC.
- The candle is available to a strategy only at `timestamp + timeframe`.
- `1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `1D` are recognized. Each provider declares capabilities; a CSV instance supports only its declared timeframe.
- Positive finite OHLC, nonnegative finite volume, and `low <= open/close <= high`.
- No duplicates or unsorted timestamps by default; overlapping candles fail.
- Missing fields/values, NaN/Infinity, impossible prices, invalid symbols, corrupt OHLC, and negative volume fail closed.
- Missing timestamps produce warnings by default because sessions vary. `--strict-gaps` turns detected in-session gaps into errors. A configured calendar avoids treating known closures as missing bars.
- `--clean` sorts timestamps, strips symbol whitespace, normalizes dates/numbers, and removes **exact** duplicates. Conflicting duplicates fail. Cleaning notes are reported on a fresh import; normalized cache hits do not reconstruct the original cleaning audit.
- No price interpolation, no silent forward-filling, no fabricated candles, no resampling, and no automatic split/dividend adjustment.
- Source content and normalization options determine the SHA-256 cache key. Changes invalidate the cache. Cache reads are still validated. Normalized CSV writes use atomic replacement.

Historical CSVs are trusted to contain **complete** candles. Do not include a still-forming final candle. Keep exchange/calendar/timeframe metadata with your datasets. For daily equity candles, use an appropriate daily calendar adapter and your provider's timestamp convention rather than assuming a UTC-midnight bar is an NSE intraday open.

## 6. Financial engineering conventions

### Event ordering and look-ahead protection

For each completed candle:

1. Process orders whose submission time is no later than this candle's opening time.
2. Execute eligible market orders at this candle's open plus modeled adverse costs.
3. Process eligible touched limit orders and protective risk levels, within the bar's shared volume budget.
4. Mark positions at this candle's close.
5. Compute indicators/signals using only the completed prefix.
6. Queue a signal order with the candle's **availability timestamp** for the next eligible candle.

No strategy receives future candles. Breakout bands exclude the current candle. Final-bar signal orders are cancelled rather than filled at the just-observed close. Open final inventory is marked, **not** forcibly liquidated. This distinction matters: total equity return can include unrealized profit while trade statistics only use realized closing fills.

### Sizing and costs

- Long-only, no leverage, no margin, no short sales. `SELL` and `EXIT` reduce/close longs.
- Signal entries do not pyramid. Default position budget is 25% of marked equity, capped by cash.
- Quantity is sized using the last known close and entry cost estimate; a gap at the next open can reduce the affordable fill.
- Fractional quantities are supported. Venue lot/tick-size rounding is **not implemented**.
- Commission = `filled_quantity × fill_price × commission_rate` on each side.
- Market buy price = `reference × (1 + slippage + spread/2)`; sell uses minus.
- Spread is a **full proportional spread**. Slippage is a proportional adverse adjustment per side.
- Slippage/spread are embedded in execution prices, not deducted twice. Their separate diagnostic cost is quantity × adverse reference-price difference. Total transaction-cost metrics include commission plus that modeled impact.
- Cash cannot fund an unlimited order: fills are capped to affordability. An unaffordable remainder is cancelled or an unfundable order rejected.
- Sell orders cannot exceed unreserved inventory. Orders are copied to prevent mutation after submission. All accepted/rejected orders and actual fills are recorded.

### Liquidity, limits, stops, calendars

- Shared per-symbol/bar execution capacity = `volume × MAX_PARTICIPATION`; zero-volume bars cannot fill.
- Orders may partially fill; the unfilled liquidity-limited remainder remains eligible on later bars.
- Limit orders require a touch, may improve to an eligible open, and never fill beyond their limit. Modeled adverse impact is capped at the limit. There is no price-time queue simulation.
- Market fills at open receive the open timestamp. Intrabar limit fills receive the bar-availability timestamp as an **estimate**, not an asserted tick time.
- Stops are percentage levels from weighted-average entry price. Gap-down stops execute at the worse open, not at an unavailable stop price.
- If both stop and take-profit levels lie inside one candle, the stop wins. No take-profit is credited on an entry candle. An entry-candle stop can fire conservatively; the true high/low path is unknown.
- Protective exits share volume capacity. Residual triggered exits become next-bar market orders, and stale pending orders are cancelled.
- Bars outside the selected session cannot execute. `continuous` is appropriate for synthetic/24×7 data, not a universal equity calendar.

### Account and metrics

Inventory uses weighted-average entry price and proportional allocation of entry fees on partial exits. Realized P&L is net of allocated entry fees and exit fees. Unrealized P&L includes remaining entry fees. The invariant is:

```text
equity - initial_capital = realized_pnl + unrealized_pnl
```

No deposits, withdrawals, or FX conversions are modeled. Daily P&L compares current marked equity with the previous observed UTC day's end; there is no funding-flow adjustment. Drawdown tracks the high-water mark.

Metrics include total return, CAGR (only with at least one year of elapsed data), maximum drawdown, Sharpe, Sortino, volatility, win rate, average win/loss, profit factor, expectancy, number of trades, average trade, largest win/loss, exposure, stability/instability, and transaction costs.

Definitions:

- Return/volatility/risk ratios use **UTC observed daily last marks**, not each intraday candle as a separate day.
- Risk-free rate and target downside return are zero. Sharpe uses sample standard deviation. Sortino uses root mean squared negative daily returns over all observed days.
- `--annualization 365` for the bundled 24×7 examples; typically 252 for equity sessions. This is a convention, not inferred from symbol names.
- Days without data are not interpolated. Align datasets and conventions before comparing them.
- Undefined ratios (zero variance/downside, or profit factor with no losses) are JSON `null`, not Infinity or a claim of safety.
- Trade counts/statistics count **realized closing fills**, not reconstructed round trips. Partial exits can increase that count. Interpret low-trade penalties with this convention in mind.
- Exposure is the fraction of processed tradable bars with inventory at open/close or an entry during the bar, not a tick-precise time fraction.
- Instability is the population standard deviation of four chronological block returns; stability is `1/(1+instability)`. This is a simple diagnostic, not proof of robustness.
- CAGR is omitted for short samples and discontinuous walk-forward aggregates.

## 7. Composite research scoring

The default score is a weighted sum of bounded benefit components and risk penalties:

```text
return          = tanh(total_return)
sharpe          = tanh(sharpe / 3)
sortino         = tanh(sortino / 3)
profit_factor   = tanh((profit_factor - 1) / 2)
drawdown        = -max_drawdown
low_trades      = -max(0, 1 - trade_count / minimum_trades)
instability     = -std_dev(four_block_returns)
volatility      = -annualized_daily_volatility
costs           = -total_modeled_transaction_costs / initial_capital
```

Undefined risk ratios receive neutral contributions rather than rewards. Weights and minimum trade count are configurable through `ScoringConfig` or `--scoring data/example_scoring.json`. Negative/nonfinite weights and a return-only configuration are rejected. Return already reflects modeled costs; the cost term is a deliberate extra implementation-friction penalty, not a second debit to cash.

Do not compare annualization conventions or different-length markets blindly. Scoring is relative research prioritization, not a universal expected-return measure. Warning diagnostics call out high returns, low trade counts, undefined metrics, extreme parameter sensitivity, and large training/test gaps. Gap comparisons are descriptive and can also reflect different sample lengths.

## 8. Architecture and component map

```text
CSV / read-only REST / read-only WSS
                 │
       normalized UTC Candle
                 │
         validator + cache
                 │
    completed-candle strategy prefix
                 │
         BUY / SELL / HOLD / EXIT
                 │
      sizing / cash / inventory checks
                 │
             PaperBroker
           (no real adapter)
                 │
      shared execution + Portfolio
                 │
       fills / trades / snapshots
                 │
 SQLite repository / metrics / API / JSON+CSV+SVG
```

Backtests and the async simulator both call `PaperBroker.on_bar()` and `handle_signal()`. Research evaluation does not call a real exchange. A stream adapter has read-only data responsibilities, never order routing.

| Module/group | Role |
|---|---|
| `run.py`, `scripts/` | User-facing modes, convenience wrappers, sample-data generator and downloader |
| `app/main.py`, `app/api/server.py` | ASGI factory, lifecycle, authentication, safe error handlers |
| `config/settings.py` | Environment loading, secret fields excluded from repr, immutable paper-only checks |
| `data/models.py` | Candle schema, supported intervals, timezone parsing, symbol validation |
| `data/provider.py` | `MarketDataProvider`, abstract `APIProvider`, `WebSocketProvider` extension contracts |
| `data/historical.py` | CSV import and `BinanceHistorical` read-only HTTP implementation |
| `data/realtime.py` | Replay, generic authenticated JSON WSS, public Binance WSS, heartbeat/reconnect |
| `data/factory.py` | Whitelisted data source construction; no broker factory or arbitrary code loading |
| `data/validator.py`, `data/cache.py` | Fail-closed validation, deliberate cleaning, atomic normalized cache |
| `indicators/` | Pure-Python SMA/EMA, ROC, Wilder RSI, MACD, rolling volatility |
| `strategies/base.py`, `strategies/__init__.py` | Signal enum, interface, bounded parameters, explicit strategy registry |
| `strategies/moving_average.py` | Fast/slow SMA crossover |
| `strategies/momentum.py` | Rate-of-change threshold entry/exit |
| `strategies/mean_reversion.py` | Deviation below rolling mean, exit at mean |
| `strategies/breakout.py` | Close above/below prior rolling high/low |
| `strategies/rsi.py` | Configurable RSI lower/upper thresholds |
| `strategies/macd.py` | MACD/signal-line crossings |
| `strategies/bollinger.py` | Lower-band entry, mean reversion exit |
| `backtesting/engine.py` | Sequential candle loop, indicator-only warmup, labeled result generation/export |
| `backtesting/orders.py` | Order statuses, fills, validation, immutable-at-submission copies |
| `backtesting/costs.py` | Execution configuration, commissions, spread/slippage model |
| `backtesting/portfolio.py` | Cash/inventory/fees, weighted entry, net P&L, marks, drawdown, snapshots |
| `backtesting/metrics.py` | JSON-safe daily returns and risk/trade statistics |
| `tournament/runner.py` | Cartesian bounded grid evaluation and ranking |
| `tournament/scoring.py` | Configurable risk/return/stability/cost composite score |
| `tournament/selection.py` | Training shortlist, validation selection, locked test evaluation |
| `validation/train_test.py` | Chronological disjoint fraction-based splits |
| `validation/walk_forward.py` | Rolling selection and nonoverlapping test folds |
| `validation/robustness.py` | Parameter/cost/subperiod perturbations and suspicious-result warnings |
| `paper_trading/broker.py` | Broker interface, only active paper broker, non-instantiable future real marker |
| `paper_trading/execution.py` | Fill-price logic and continuous/example session calendars |
| `paper_trading/account.py` | Named `PaperAccount` specialization; shared `Portfolio` remains the accounting source of truth |
| `paper_trading/simulator.py` | Async state machine, signal execution, dedupe, gap pause, graceful stop |
| `database/models.py` | Version-1 relational schema for results, orders, fills, trades, snapshots, state |
| `database/database.py` | SQLite initialization, WAL, FULL synchronous writes, thread-safe transactions |
| `database/repository.py` | Parameterized persistence, exclusive paper writer, atomic checkpoint/records |
| `api/schemas.py`, `api/services.py`, `api/routes/` | Pydantic request validation, data-path restrictions, small dashboard-ready route groups |
| `logging/logger.py` | JSON-lines timestamp/level/component/event plus symbol/strategy/signal/order/price/quantity/error |
| `tests/` | Indicators, strategy signals, accounting, execution, validation, database recovery, feeds, lifecycle, security and API tests |

`data/` holds sample/user CSVs and cache; `results/` holds experiment artifacts; `logs/` holds local JSON-lines logs. Required package folders include `__init__.py`. No operational real broker exists, and `FutureRealBroker` cannot be instantiated.

### Durability model

SQLite saves a paper candle's fills, trades, account, processed cursor, warmup history, and newly queued signal orders in one transaction. A process-level advisory lock enforces a single paper writer per database. WAL and FULL synchronous writes protect database integrity; transactions roll back on errors. Lock files may remain on disk, but OS locks release when the process exits. Backtests are stored separately from the paper account.

This is **not distributed exactly-once exchange execution**. The current broker checkpoint contains cumulative order/fill/trade history and is rewritten per paper event; database rows are inserted idempotently. For high-frequency or months-long operation, replace this small-account checkpoint format with incremental indexed event storage/retention before scaling. Back up a stopped database or use SQLite's online backup API; don't copy only the main DB while an active WAL contains uncheckpointed writes.

## 9. Configuration

`.env.example` contains safe defaults/empty secret placeholders. Existing environment variables take precedence over `.env`. Safety flags are rejected if changed away from paper-only. CLI `--mode api` or `--mode backtest` selects an application activity; it does not change the safety `MODE=PAPER` setting.

| Variable | Default | Meaning |
|---|---|---|
| `MODE` | `PAPER` | Only permitted safety mode |
| `LIVE_TRADING` | `false` | `true` fails at startup |
| `INITIAL_CAPITAL` | `100000` | Initial simulation cash |
| `CURRENCY` | `INR` | Account denomination; no FX conversion |
| `DEFAULT_SYMBOL` | `DEMO` | Synthetic default instrument |
| `DEFAULT_TIMEFRAME` | `1D` | Must match real dataset/feed interval |
| `MARKET_CALENDAR` | `continuous` | `continuous` or intraday `nse-session` example |
| `COMMISSION` | `0.0005` | Per-side proportional fee |
| `SLIPPAGE` | `0.0005` | Per-side adverse execution adjustment |
| `SPREAD` | `0.0002` | Full spread; half per side |
| `POSITION_SIZE` | `0.25` | Target fraction of equity per signal entry |
| `STOP_LOSS` | `0.05` | Fraction below entry; `none` disables |
| `TAKE_PROFIT` | `0.10` | Fraction above entry; `none` disables |
| `MAX_PARTICIPATION` | `0.10` | Maximum fraction of a candle's volume filled |
| `DATABASE_URL` | `sqlite:///trading_bot.db` | Relative to current directory; absolute paths also supported |
| `API_TOKEN` | empty | Required private token for application API endpoints |
| `DATA_SOURCE` | `replay` | `replay`, `binance`, or `generic` |
| `DATA_WS_URL` | empty | Optional generic WSS market-data URL |
| `MARKET_DATA_TOKEN` | empty | Optional generic market-data Bearer token |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Use `python run.py --help` for CLI flags. A CLI `--database` overrides the environment URL; `--symbol` and `--timeframe` override their defaults. Files bundled with the project use synthetic symbols and continuous sessions so the initial run needs no network or credentials.

## 10. Android / Termux

Install a maintained Termux release from a supported distribution source. Keep the project under Termux home, not shared `/sdcard`, for executable permissions, reliable SQLite/file-lock behavior, and privacy.

```bash
pkg update
pkg upgrade
pkg install python
cd ~/trading_bot
python --version                      # 3.11+
cp .env.example .env
python run.py --mode backtest          # Core works before optional packages
python -m pip install -r requirements.txt
python -m pytest -q
python run.py --mode paper --database sqlite:///results/phone_paper.db
```

Do not blindly replace Termux's package-managed pip with a system-incompatible version. Android commonly lacks PyPI wheels for Rust/native packages. **Pydantic v2's `pydantic-core` may need a local Rust build**:

```bash
pkg install rust clang make pkg-config openssl libffi
python -m pip install 'pydantic>=2.9,<3'
python -m pip install -r requirements.txt
```

If a session calendar reports `ZoneInfoNotFoundError`, install the pure-data package with `python -m pip install tzdata`. Windows receives it through a conditional requirement.

Build success depends on the current Android ABI, Python, and package versions. The explicit lightweight fallback is the working **stdlib research + replay CLI**, not an unsupported imitation FastAPI server. Export env variables manually if dotenv is unavailable. No NumPy/pandas installation is required. Without FastAPI/Pydantic, the API/full API test suite is unavailable; without httpx/websockets, their live-data adapters are unavailable.

Optional long-session convenience:

```bash
pkg install tmux
tmux new -s paperbot
termux-wake-lock
python run.py --mode paper --source replay --delay 1
```

Android can kill background apps despite a wake lock; exempt Termux from battery optimization if appropriate. SQLite restart recovery helps but does not guarantee uninterrupted feeds. A VPS is more reliable for unattended operation. Termux/Android deployment instructions are provided but not tested on a physical phone in this delivery.

## 11. Linux VPS deployment

A VPS runs independently of your personal PC. Use a non-root service user. On a distribution with Python 3.11+ available:

```bash
sudo apt update
sudo apt install python3 python3-venv
# Transfer/extract the project to /opt/trading_bot and give the service user ownership.
cd /opt/trading_bot
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
.venv/bin/python -m pytest -q
.venv/bin/python run.py --mode backtest
.venv/bin/python run.py --mode api --host 127.0.0.1 --port 8000
```

Confirm the distro's Python version before installing. SQLite creates tables on first startup. Configure absolute database paths for services if the working directory may change. Put TLS at a reverse proxy or use an SSH tunnel; do not expose an unauthenticated control surface. Bind `0.0.0.0` only intentionally with firewall/TLS/access controls. The supplied API preview uses that bind for the sandbox proxy, with protected endpoints disabled unless a private token is configured.

Example `/etc/systemd/system/paperbot.service` (replace the service user):

```ini
[Unit]
Description=Paper-only trading research API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=paperbot
WorkingDirectory=/opt/trading_bot
EnvironmentFile=/opt/trading_bot/.env
ExecStart=/opt/trading_bot/.venv/bin/python run.py --mode api --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
KillSignal=SIGTERM
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now paperbot
sudo journalctl -u paperbot -f
```

The API starts with its bot **STOPPED**, even if there is saved inventory. Start it deliberately through `/paper/start`. For a standalone continuous paper stream, replace `ExecStart` with `run.py --mode paper --source ...` and configure symbol/timeframe/currency/source in `.env`. Do not run that service and the API as competing paper writers against the same database. Replay normally exits at EOF; `Restart=on-failure` avoids an endless successful replay restart cycle.

## 12. Logging and security

JSON-line logs include timestamp, component, event, severity, and the requested trading fields. They are readable as one event per terminal line and suitable for future collection. Signal/fill details use DEBUG to avoid flooding a phone terminal; lifecycle, order placement, reconnect, gap, and error events use appropriate INFO/WARNING/ERROR levels. Logs are also written to `logs/bot.jsonl` by the CLI. Rotate/archive logs for long sessions; automatic log retention is not implemented.

Security boundaries:

- No real-money code path, broker SDK, order REST URL, or live broker factory.
- No keys, secrets, passwords, account numbers, or usable credentials in source/example settings.
- Secrets only from environment/ignored `.env`; repr and API responses exclude them.
- Feed exception logging records error **classes**, not raw URLs/headers/payloads.
- No strategy `eval`, dynamic user code, arbitrary file uploads, pickle caches, or shell execution from API inputs.
- WSS required for authenticated feeds; host/URL comes from trusted local configuration, not an API request.
- SQLite is plaintext: protect filesystem permissions and encrypt host storage/backups if needed.
- Do not use exchange order-authorized credentials for these read-only data adapters. Public Binance data does not require credentials.

## 13. Extending safely

### Add a strategy

Create `app/strategies/my_strategy.py`:

```python
from dataclasses import dataclass
from app.strategies.base import Strategy, Signal
from app.indicators.moving_averages import check_period

@dataclass
class MyStrategy(Strategy):
    period: int = 20
    name = "my_strategy"

    def __post_init__(self):
        check_period(self.period)
        self.history_limit = self.period

    def generate_signal(self, data):
        if len(data) < self.period:
            return Signal.HOLD
        mean = sum(bar.close for bar in data[-self.period:]) / self.period
        return Signal.BUY if data[-1].close > mean else Signal.EXIT
```

Register it explicitly in `app/strategies/__init__.py`. Add known-output, invalid-parameter, warmup, prefix-causality and paper/backtest parity tests. Keep strategy code pure; do not place orders, make network calls, access future data or modify candles in `generate_signal`. Add grids only after specifying your holdout experiment.

SMA uses an exact trailing window. To bound mobile work, RSI/MACD strategies reseed their recursive indicators from a finite trailing history (5× the relevant periods). This is deterministic but can differ slightly from platforms that carry EMA/Wilder state from the beginning of an entire file. The standalone indicator functions compute the full supplied history. If exact platform parity is required, add an incremental indicator state interface with matching warmup conventions and serialization tests.

### Add a market-data provider

Subclass `MarketDataProvider`/`APIProvider`, declare supported timeframes, and implement `historical(symbol, timeframe, start, end)` with a half-open `[start,end)` range. Return normalized `Candle` objects and call the validator. Preserve real timestamp, adjustment and calendar semantics. Give the provider its own cache namespace and proper throttling/retry behavior.

For streaming, subclass `WebSocketProvider` or `JSONWebSocketStream`, implement documented subscription/decode mapping, emit only completed candles, authenticate from secret settings, and implement cancellation/close. Supply exchange holidays where relevant. Tests should cover dropped connections, duplicate/corrected data, missing bars, malformed payloads, timestamps, and credential-safe failures. Do not assume every provider supports every interval.

### Future real broker — **not operational here**

`BrokerInterface` documents `place_order`, `cancel_order`, `get_order`, `get_positions`, `get_balance`, and `get_orders`. The existing `FutureRealBroker` is an abstract, non-operational marker and deliberately fails construction. V0.1–V0.6 must not register a real broker, enable `LIVE_TRADING`, or put order-authorized credentials into this app.

A future separately reviewed release would need broker reconciliation, precision/lot rules, durable idempotency, exchange calendars, authorization, independent kill switches, advanced risk limits and extensive paper/operational testing. Exposing that interface is not a claim that replacing one class is sufficient for safe real trading. Keep the current default and implementation paper-only.

### Future research layers

Regime detection can consume candle/indicator histories without altering accounting. Adaptive selection can consume saved validated candidates without modifying execution. A future advanced risk module can sit between `Signal` and order construction. ML, sentiment/news, optimization and multi-strategy portfolios need their own timestamp/feature-leakage and data-licensing tests. None of those future systems are implemented or implied by this version.

## 14. Known limitations and operational cautions

1. This is a functioning research/paper baseline, **not exchange-certified trading infrastructure** or financial advice.
2. No real orders, shorts, leverage, derivatives, corporate actions, dividends, taxes, interest, borrowing, funding, FX conversion, or venue-specific Indian charges (STT/GST/stamp duty). Add market-specific costs before drawing financial conclusions.
3. No order-book/queue model, tick-accurate stop sequence, latency distribution, true depth, exact fill availability, lot/tick rounding, auctions, or exchange outage calendar. Fixed cost/volume assumptions can misestimate fills materially.
4. Long-only per-symbol research runs. The broker can hold multiple symbols in one currency, but CLI/API paper control selects one symbol at a time; no multi-strategy allocation or portfolio-level optimization is provided.
5. Gap detection does not reconstruct prices or risk events that were never received. Streams reconnect but do not automatically backfill. Candle paper fills are retrospective confirmations from closed bars, not actual venue fills.
6. Cross-market tournaments need comparable history lengths, currencies, adjustment conventions, sessions and annualization settings. There is no survivorship-bias correction, benchmark attribution, statistical multiple-testing correction, or automatic universe quality control.
7. Holdout discipline is procedural. The software cannot stop a human repeatedly inspecting/reusing datasets; a one-use object does not make repeated experiments statistically unseen.
8. Sample data has deliberately visible trends/cycles and is not a profitability demonstration. High synthetic scores should not guide real investments.
9. One local paper writer per SQLite database, no horizontal scaling, users/roles, audit-grade encryption, web dashboard, background research job queue, or deployment-wide rate limiter.
10. Unbounded cumulative paper history/checkpoints and full saved research payloads will grow. Long-running/high-frequency deployments need event-store retention and incremental checkpoint design before scaling.
11. Device-specific Termux, Windows and production VPS service operation were not tested here. External Binance/generic live connectivity requires network access and provider-compatible configuration; transport behavior is tested with mocks, not a claim of verified live market availability.
12. `.env` is ignored, runtime DBs/cache/logs are not shipped as credentials or working accounts. A new archive extraction starts without a paper database. Saved **example result artifacts** are provided separately in `results/`.

## 15. Test and audit workflow

```bash
python -m compileall -q app scripts run.py
python -m pytest -q
python run.py --mode backtest --output results/check_backtest
python run.py --mode tournament --datasets data/tournament_datasets.json
python run.py --mode validate
python run.py --mode walk-forward
python run.py --mode robustness
python run.py --mode paper --database sqlite:///results/check_paper.db
python run.py --mode api --host 127.0.0.1 --port 8000
# In a second shell:
curl http://127.0.0.1:8000/health
```

Tests cover both successful paths and safe failures, including future-data isolation, final-candle nonexecution, liquidity limits, fee/P&L reconciliation, stop gaps and ambiguity, calendar closures, SQLite rollback/restart, exclusive ownership, protected API routes, replay parity, mocked download caching, WSS reconnect/authentication/heartbeats, and malformed/future data rejection.

**Treat real money as a separate deployment: paper/sandbox first, then the
gated live path — with non-zero risk limits and the preflight command.
Evaluate assumptions, not only scores.**

---

## 16. AXIOM Kronos forecasting subsystem (desktop)

Full details: **`KRONOS_INTEGRATION.md`**. Off by default — Kronos does nothing
unless `AXIOM_KRONOS_ENABLED=true`.

[Kronos](https://github.com/shiyu-coder/Kronos) is a financial time-series
foundation model. In this repository it is a **forecasting subsystem only**, in a
new top-level package `axiom/` plus a `Forecast` page in the desktop terminal.
Arena 3.0.5 (`app/`) is untouched: `git diff 0314db2 -- app/` is empty.

```
REAL MARKET DATA → MarketDataProvider → validation → closed-candle gate
   → [IPC] AXIOM-Kronos process (PyTorch, Kronos, tokenizer/model)
   → forecast validation → ForecastStore → MARKETS overlay / FORECAST panel / research
```

What it is not: it never places orders, never changes risk limits, never enables
LIVE trading, and never writes into Arena decisions, risk, account or strategy
state. Its IPC surface is exactly `ping · load · status · forecast · shutdown`.
Every predicted value is labelled `Forecast …` with `origin=KRONOS_FORECAST`;
observed data keeps `origin=MARKET_OBSERVED`. Sample dispersion is reported as
dispersion, never as calibrated probability.

```bash
# Base runtime — unchanged, and it never imports torch/pandas/Qt.
pip install -r requirements.txt
python -m pytest -q

# Kronos runtime — a SEPARATE dependency set (only the Kronos process needs it).
desktop\run_kronos_windows.bat          # Windows: .venv-kronos + AXIOM_KRONOS_SOURCE
pip install -r requirements-kronos.txt  # elsewhere

# Weights are downloaded into %LOCALAPPDATA%\AXIOM\models\kronos and verified by
# SHA-256 before loading. They are never committed (see .gitignore).
python tools/build_windows_executables.py   # AXIOM.exe + AXIOM-Kronos.exe
```

Configuration lives in `AXIOM_KRONOS.env.example` (separate from Arena's `.env`
on purpose). Initial settings: `NeoQuasar/Kronos-small` +
`NeoQuasar/Kronos-Tokenizer-base`, lookback 400, pred_len 120, max_context 512,
T 1.0, top_k 0, top_p 0.9, sample_paths 1, device auto, update_on_closed_bar true
— with `max_context` validated against the published context length of the model
rather than assumed, and tokenizer/model pairing checked before inference.

Tests: `tests/test_kronos_*.py` (8 modules) cover valid/invalid OHLCV, NaN/Inf,
duplicate and unordered timestamps, gaps, wrong timeframe, insufficient history,
model loading, tokenizer/model mismatch, forecast shape and validation, CPU
inference against the real Kronos implementation (when `AXIOM_KRONOS_SOURCE` and
PyTorch are present), GPU where supported, GUI responsiveness, Arena polling
continuing during inference, Kronos failure isolation, clean worker shutdown,
cache correctness, model revision tracking, no future leakage, no fabricated
data, no trading capability, and graceful degradation when Kronos is disabled or
unavailable.
