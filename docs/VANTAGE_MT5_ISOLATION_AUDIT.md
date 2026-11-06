# Vantage / MetaTrader 5 Removal & Legacy Isolation Audit (`v3.7.0a10`)

## 1. Executive Summary
- **Operator UI (`app/static/terminal/*`)**: **100% removed** (`0` Vantage/MT5 forms, tabs, buttons, API calls, or strings — enforced by `tests/test_vantage_ui_removal.py`).
- **Active Multi-Market Runtime (`app/markets/*`, `app/autonomy/*`, `app/alpaca_broker/*`, `app/ensemble/*`, `app/model_lab/*`, `app/source_launcher.py`, `axiom_launcher.py`, `Start-Axiom.ps1`)**: **100% decoupled** (`0` `vantage`/`mt5` imports or references). Shared Windows DPAPI storage now lives in neutral **`app/security/dpapi_secrets.py`**, preserving 100% compatibility with existing `%LOCALAPPDATA%\AXIOM\credentials\*.dpapi` files.
- **Remaining Repository References**: Every remaining `vantage`/`mt5` occurrence in the repository falls into one of four explicitly bounded, non-interfering legacy categories documented below so nothing can affect Upstox (NSE `INR`) or Alpaca (NASDAQ `USD`) data, paper simulation, or gated execution.

---

## 2. Complete Classification by Subsystem

| Category | Paths | Status | Why Retained / How Isolated |
|---|---|---|---|
| **A. Neutralized Active Modules (Cleaned in `v3.7.0a9`–`v3.7.0a10`)** | `app/static/terminal/{index.html,terminal.js,terminal.css}`, `app/security/dpapi_secrets.py`, `app/alpaca_broker/credentials.py`, `app/autonomy/upstox_token.py`, `app/markets/alpaca_credentials.py`, `app/api/routes/markets.py`, `app/ensemble/__init__.py`, `app/model_lab/__init__.py` | **CLEAN (`0` MT5/Vantage refs)** | Operator UI and all Upstox/Alpaca/Kronos/Ensemble runtime packages have zero imports or references to `app.vantage` or `mt5`. |
| **B. Negative Boundary Guards (Explicitly Reject CFD Substitution)** | `app/listed_assets.py`, `docs/LISTED_ASSETS.md` | **ACTIVE GUARDRAIL** | Enforces `"mt5_routing_allowed": False` and `"vantage_mt5_allowed": False` so listed NSE/US equities can never be substituted with CFDs. |
| **C. Dormant Legacy Backend Compatibility Shims** | `app/vantage/{__init__,connector,gateway,models,risk,secrets,service,store}.py`, `app/api/routes/{vantage,paper,market_data,dashboard,health}.py`, `app/market_data/{__init__,mt5,paper_provider,read_only_gateway}.py`, `app/api/{schemas,server}.py`, `app/paper_trading/simulator.py` | **DORMANT / ISOLATED** | Unreferenced by `/terminal`. Retained solely so legacy schema/API isolation tests (`test_vantage_*.py`, `test_mt5_market_data.py`, `test_upstox_live.py` `/health` fields `real_order_routing` & `vantage_startup`) continue passing without breaking backward-compatible health/audit schemas. Never contacts any broker on startup (`0` network calls). |
| **D. Historical Packaging, CI Artifact Slug & Historical Docs/Tests** | `.github/workflows/{paper-checks,windows-release}.yml`, `packaging/windows/{axiom.iss,axiom.spec,build_windows.ps1,install_test.ps1,portable_test.ps1,requirements-build.txt,diagnostics/*}`, `docs/{AI_MODEL_LAB_MANUAL,ARCHITECTURE_AUDIT,AUTONOMY,BASELINE_AUDIT_AND_ISSUES,RESEARCH_CANDIDATES,TERMINAL_BENCHMARK.json,TERMINAL_PERFORMANCE,TERMINAL_REDESIGN_PLAN,WINDOWS_APP_CONTROL}.md`, `scripts/benchmark_terminal.py`, `tests/{mt5_*,test_mt5_*,test_vantage_*,...}` | **CI / HISTORICAL EVIDENCE ONLY** | Historical audit logs, regression tests asserting UI removal (`tests/test_vantage_ui_removal.py`), and optional CI Windows build scripts. None are required by the source-first workflow (`Start-Axiom.ps1` / `axiom_launcher.py`). |

---

## 3. Route Removal — `/api/trading` is no longer mounted (`3.7.0a10` reviewed source)

Earlier revisions of this audit classified `app/api/routes/vantage.py` as *dormant but
mounted*, which was inaccurate and unsafe: the router was registered in
`create_app`, so **30 authenticated endpoints under `/api/trading` were reachable**
in the supported application — including `POST /api/trading/session`
(`VANTAGE_DEMO` / `VANTAGE_LIVE`), `PUT /api/trading/credentials`,
`POST /api/trading/arm`, `POST /api/trading/activate`,
`POST /api/trading/orders/review`, `POST /api/trading/orders/submit`,
`POST /api/trading/positions/modify/submit` and `POST /api/trading/emergency`.
None of those paths appeared in `README.md` or `USER_MANUAL.md`.

Decision: **Vantage is no longer part of the supported application.** The operator
UI was removed in `v3.7.0a9`; the remaining HTTP surface is now removed as well.

| Item | Before | After |
|---|---|---|
| Router import/mount (`app/api/server.py`) | `vantage` inside the `for module in (...)` mount loop | not imported, not mounted — no `/api/trading*` path exists |
| Legacy order/session/credential routes | reachable with a bearer token | `404` for every path, authenticated or not |
| `/health` fields | `real_order_routing: "VANTAGE_MT5_GUARDED"`, `vantage_startup: "DISCONNECTED_DISARMED"` | `real_order_routing: "DISABLED_PAPER_ONLY"`, `vantage_api: "REMOVED"`, `vantage_startup: "REMOVED_UNMOUNTED"` |
| Latency metrics | `/api/trading/performance` | `GET /api/performance` (authenticated, read-only, `paper_only`, `0` broker calls) |
| Regression guard | `tests/test_vantage_api.py` exercised the live routes | `tests/test_vantage_live_api_removal.py` asserts the routes are gone and that nothing re-mounts them |

What deliberately remains in the tree (and why):

- `app/vantage/**` and `app/api/routes/vantage.py` stay as **unreachable** code so the
  legacy isolation tests (`tests/test_vantage_*.py`, `tests/test_mt5_market_data.py`,
  `tests/test_vantage_ui_removal.py`) keep proving that no Upstox/Alpaca runtime
  imports them. They expose no reachable route.
- `MT5MarketDataService` / `app.market_data.mt5` remain wired for the read-only
  `/api/market-data/*` endpoints (status, connect, disconnect, instruments, chart)
  and the `source: "vantage_mt5"` local-paper feed. Those paths are **market data
  only** — no order function exists in that module — and they are documented here.
  They are unsupported legacy: they require a locally installed MT5 terminal and
  were not exercised against a real terminal in this work.

Verification: `tests/test_vantage_live_api_removal.py` (21 tests) — no path under
`/api/trading`, all legacy GET/POST paths `404`, package importable but unmounted,
and the supported surfaces (`/health`, `/api/markets/live/status`, `/paper/status`,
`/api/performance`) still answer with live execution OFF.
