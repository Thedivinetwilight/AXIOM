# USER_MANUAL_VERIFICATION.md

Companion to `USER_MANUAL.md` (Arena **3.0.2**). Documents what was inspected,
what was executed to verify the manual, and every discrepancy found and fixed.
**Documentation-only task:** no trading, risk, broker, strategy, or execution
code was modified while producing the manual (the only repo changes are the
two `.md` files; test suite re-run proves nothing broke).

## 1. Files inspected (source of every manual claim)

- `app/__main__.py` — full argparse tree (22 subcommands) + every `cmd_*`
  implementation (upstox-test, market-stream, paper-live, paper-test,
  upstox-login, engine, sandbox-test, initialize, market-universe, history,
  dataset, universe, research, report, maintenance, ask, scan, discover,
  lifecycle, replay, monitor, backup).
- `app/config/settings.py` — `Settings` dataclass, `__post_init__` safety
  validation (PAPER-only, LIVE rejected, broker/token/product/risk checks),
  `from_env()` + `UPSTOX_ACCESS_TOKEN`/`UPSTOX_INSTRUMENT_KEYS` aliases.
- `.env.example` — documented template (96 lines).
- `app/api/server.py` — `create_app`, bearer-token auth (503/401), latency
  middleware, router registration, `/docs`, version 3.0.2.
- `app/api/routes/{health,dashboard,paper,market,strategies,backtests,v2}.py`
  — every route path/method.
- `app/engine/sandbox_engine.py` — `_act/_submit/reconcile/status`,
  emergency_stop/resume, protective exits, router authorization, recovery.
- `app/engine/state.py`, `app/brokers/sandbox.py` — journal state machine,
  tag lookup, honest cancellation, instrument resolution.
- `app/engine/adaptive.py`, `app/learning/{lifecycle,router,evaluator,
  experiments,portfolio}.py` — lifecycle ladder, router scoring, promotion
  gate, TRAIN→VALIDATION→LOCKED protocol, CapitalAllocator.
- `app/strategies/__init__.py` + strategy modules — registry (7 strategies).
- `app/data/upstox_stream.py`, `app/data/upstox_auth.py` — feed behavior,
  reconnect/backoff/resubscribe, login/verify flows.
- `app/bootstrap.py`, `app/database/recovery.py` usage, `app/monitoring/
  system.py`, `app/reporting/weekly.py`, `app/research/{scanner,command_center,
  replay}.py`.
- `desktop/app.py`, `desktop/run_windows.bat`, `requirements-desktop.txt`.
- `run.py` (modes incl. `api`, `--host/--port` defaults 127.0.0.1:8000),
  `app/main.py`, `pyproject.toml`, `requirements.txt`, `requirements-lock.txt`.
- `README.md`, `ARENA_FINAL_VERIFICATION.md` (3.0.2).

## 2. Commands verified (executed, not just read)

- `python -m app --version` → `Arena 3.0.2` — **VERIFIED** (executed).
- `python -m app --help` → banner + all 22 commands — **VERIFIED** (executed).
- `--help` executed for **every** subcommand (22/22); option lists in the
  manual match the argparse output exactly — **VERIFIED**.
- `python -m app monitor` (fresh DB) → snapshot JSON, exit 0 — **VERIFIED**.
- `python -m app lifecycle status` (fresh DB) → `{"strategies": []}` — **VERIFIED**.
- `python -m app report --format markdown` (fresh DB) → weekly markdown with
  OBSERVED/CALCULATED labels — **VERIFIED**.
- `python -m app paper-test` → `"ok": true`, final equity
  `99637.58463385397` — **VERIFIED** (earlier this session and in the release run).
- `python -m app initialize` ×2 (fresh venv, earlier release verification) —
  **VERIFIED** idempotent.
- Commands requiring external credentials (`upstox-test`, `market-stream`,
  `sandbox-test`, `upstox-login`, `engine`, `paper-live`, `history sync`,
  `market-universe sync`) — syntax **VERIFIED** via `--help`; runtime behavior
  **NOT ATTEMPTED** (no credentials; stated in the manual).

## 3. Routes verified (against the routers in code)

- Public: `GET /health` (exact JSON shape verified), `GET /` landing page.
- Dashboard: `GET /dashboard` (HTML), `GET /dashboard/data`,
  `POST /engine/stop`, `POST /engine/resume`.
- Paper: `GET /paper/{status,account,positions,orders,trades}`,
  `POST /paper/{start,stop,pause,resume}`.
- Market: `GET /market/status`, `GET /market/datasets`. Strategies: `GET /strategies`.
- Backtests: `GET /backtests`, `GET /backtests/{id}`, `POST /backtests/run`,
  `GET /tournaments`.
- v2 (prefix `/api/v2`): overview, trades, performance, strategies, learning,
  experiments, market_data, risk, logs, report/weekly, report, command_center,
  portfolio, attribution, research_lab, learning_detail, market_intelligence,
  charts/{equity,drawdown,attribution}.svg — **VERIFIED** in `v2.py`.
- Auth behavior: protected routers depend on `authenticate` (503 without
  `API_TOKEN`; 401 on wrong bearer) — **VERIFIED** in `server.py`.
- Interactive schema: `/docs` (docs_url in `create_app`) — **VERIFIED**.

**Discrepancy found & fixed during verification:** the manual initially wrote
the emergency endpoints as `/dashboard/engine/stop|resume`; the actual paths
are **`POST /engine/stop`** and **`POST /engine/resume`** (dashboard router
has no prefix). Corrected in §5, §14, §21.

## 4. Environment variables verified

Every variable in manual §6 was cross-checked against
`app/config/settings.py` fields and `.env.example`:
`MODE, LIVE_TRADING, BROKER, DATABASE_URL, INITIAL_CAPITAL, CURRENCY,
COMMISSION, SLIPPAGE, SPREAD, POSITION_SIZE, STOP_LOSS, TAKE_PROFIT,
MAX_PARTICIPATION, DEFAULT_SYMBOL, DEFAULT_TIMEFRAME, MARKET_CALENDAR,
API_TOKEN, MARKET_DATA_TOKEN, DATA_WS_URL, DATA_SOURCE,
MARKET_DATA_PROVIDER, MARKET_DATA_SYMBOLS, MARKET_DATA_TIMEFRAME,
MARKET_DATA_MODE, MARKET_DATA_ACCESS_TOKEN (alias UPSTOX_ACCESS_TOKEN),
MARKET_DATA_AUTHORIZE_URL, UPSTOX_SANDBOX_ACCESS_TOKEN,
UPSTOX_SANDBOX_BASE_URL, UPSTOX_SANDBOX_ORDER_HISTORY_URL, SANDBOX_PRODUCT,
SANDBOX_ORDER_TAG, UPSTOX_CLIENT_ID, UPSTOX_CLIENT_SECRET,
UPSTOX_REDIRECT_URI, DATA_DIR, UNIVERSE_SYNC_URL, HISTORY_BASE_URL,
INTRADAY_BASE_URL, PARQUET_ENABLED, UPSTOX_STREAMING_UNIVERSE,
MAINTENANCE_HOUR, MAX_DOWNLOAD_RETRIES, RESEARCH_BATCH_SIZE,
MAX_STRATEGY_ALLOCATION, EVENT_MOVE_THRESHOLD, EVENT_VOLUME_SPIKE,
MARKET_TIMEZONE, SCANNER_TOP_K, DISCOVERY_MAX_CANDIDATES, LOG_LEVEL,
MAX_POSITION_VALUE, MAX_OPEN_POSITIONS, MAX_DAILY_LOSS, MAX_TRADES_PER_DAY`.

**Discrepancies found (documented, not hidden):**
1. `MARKET_TIMEZONE` exists in `Settings` (default `Asia/Kolkata`, used by
   daily risk accounting) but is **absent from `.env.example`** — flagged in
   the manual (§6) as a known documentation gap. Not fixed in this task
   (documentation-only scope; `.env.example` is repo content beyond the
   two manual files).
2. `SANDBOX_ORDER_TAG` is still accepted and passed to the broker, but since
   3.0.2 order tags are intent-derived; the manual states this explicitly.
3. The desktop env vars `ARENA_BACKEND_URL` (default `http://127.0.0.1:8000`)
   and `ARENA_API_TOKEN` are desktop-only (read in `desktop/app.py`), not part
   of core `Settings` — the manual documents them in §16.

## 5. Features verified against source

- PAPER/LIVE enforcement (`Settings.__post_init__` raises on non-PAPER,
  `LIVE_TRADING=true`, `BROKER=LIVE`) — **VERIFIED**.
- 7-strategy registry names — **VERIFIED** (executed `catalog()`).
- Sizing-before-risk, exact-gated-quantity submission, `RISK_REJECT` reasons
  (`NO_QUALIFIED_CANDIDATE`, `INSTRUMENT_IDENTITY_MISSING`,
  `ENTRY_ORDER_STILL_WORKING`) — **VERIFIED** in `_act/_submit` + tests.
- Full lifecycle ladder at the execution boundary; promoted-only blocked —
  **VERIFIED** in `adaptive.py` + `test_v3_0_2_hardening.py`.
- UNKNOWN→REMOTE_LOOKUP→VERIFIED_MATCH/ABSENCE/UNRESOLVED with durable
  intent-derived tag persisted before POST — **VERIFIED** in
  `state.py`/`sandbox.py` + tests.
- Honest per-child cancellation (logical CANCELLED only on full
  confirmation) — **VERIFIED** + tests.
- Full promotion gate (robustness/walk-forward/drawdown/lifecycle, fail
  closed) — **VERIFIED** in `evaluator.py` + tests.
- Emergency STOP semantics (persisted flag checked before every submission;
  blocks entries AND exits; cancels working orders when the engine is
  embedded in the API process; dashboard-only STOP sets the durable flag the
  standalone engine reads) — **VERIFIED** in `dashboard.py` +
  `sandbox_engine.py`; nuance stated honestly in §14.
- Recovery/restart/reconciliation behavior — **VERIFIED** against
  `state.py::recover_ledger_state`, broker `reconcile`, and the restart test
  battery.
- Weekly report sections/labels — **VERIFIED** by executing it.
- Desktop app: viewer + STOP/RESUME only, env vars, launcher — **VERIFIED**
  in source; GUI boot **NOT VERIFIED** (no display in this environment).

## 6. Discrepancies found (complete list)

1. `/dashboard/engine/stop|resume` → actual `POST /engine/stop|resume`
   (**fixed in the manual**).
2. `/health` JSON shape (manual initially showed `paper_only`; actual keys:
   `status, version, mode, live_trading, real_order_routing`) —
   (**fixed in the manual**; `paper_only` belongs to `paper-test` output).
3. `MARKET_TIMEZONE` missing from `.env.example` — **reported**, not fixed
   (outside the two documentation files this task may touch).
4. `SANDBOX_ORDER_TAG` no longer overrides order tags (3.0.2 intent-derived
   tags) — **manual wording corrected**.
5. No `sell`-only CLI probe exists (sandbox-test places a BUY with
   `--place-order`); §18 step 12 states this honestly instead of inventing a
   command.

## 7. Tests run / results

- Full suite after writing the manual: **404 collected, 404 passed,
  0 failed, 0 skipped** (Python 3.13 venv, `pytest -q`) — confirms the
  documentation task changed no behavior.

## 8. External verification status

- Upstox REST order APIs (sandbox): **NOT ATTEMPTED** (no credentials) —
  manual marks sandbox flows "implemented, not externally verified".
- Upstox WebSocket market data: **NOT ATTEMPTED** — same marking.
- Live trading: **NOT IMPLEMENTED** (cannot be enabled).
- Desktop GUI boot: **NOT VERIFIED** (no display; PySide6 not installed here).
- Termux: **NOT ATTEMPTED**. Python 3.11/3.12 on this machine: **NOT
  ATTEMPTED** (3.13 verified).

## 9. Known limitations (also in manual §20)

- Optional subsystem extras are not installed by the core package.
- SQLite is single-writer (two live processes → `database is locked`).
- Dashboard STOP cancels working orders only when the engine is embedded in
  the API process; for a standalone engine it sets the durable flag (blocks
  the next submission) — cancelling already-working remote orders then
  requires the engine's own stop path.
- No guaranteed-profit language anywhere; regime similarity disclaimed.

## 10. Scope statement

Files created/modified by this task: `USER_MANUAL.md`,
`USER_MANUAL_VERIFICATION.md` (this file). No source, test, configuration, or
packaging file was touched; the post-manual test run (§7) is the proof.
