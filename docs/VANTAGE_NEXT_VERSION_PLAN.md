# AXIOM next version — implementation baseline and architecture

Status: implementation plan recorded before the Vantage/UI changes. This is not a
profitability claim or evidence of live readiness.

## Source and GitHub state checked first

Checked on 2026-10-23 before editing:

- Repository: `Thedivinetwilight/AXIOM`; default branch `main`.
- Session branch (fixed by Arena): `arena/19f4cba3-axiom`.
- Session base: `0314db2c2d1473b0b63e04ce7c1455695f93a885`.
- Current intended prior source branch: `arena/5ed3f6b6-axiom` at
  `418344798fd3c7f743a7fc7a6ac8fd98149c2940` (not merely the older release tag).
- The session base is an ancestor of that head, so the session branch was
  fast-forwarded to `418344798fd3c7f743a7fc7a6ac8fd98149c2940` without discarding
  or rewriting work.
- Existing draft PR: https://github.com/Thedivinetwilight/AXIOM/pull/7
- Latest prerelease when checked: `v3.2.5-chart-us.1`, source
  `6c24ec3585a0b9d7933e8097f21943aef53c6f84`; its installer and portable assets
  were present. Later source/packaging commits exist on the prior branch.
- Latest prior-head paper-checks Actions runs at `4183447` were successful.
  The seven Windows failures in the old release notes are historical evidence,
  not the new baseline; fresh Windows results are required for this version.

## Baseline actually run

At `418344798fd3c7f743a7fc7a6ac8fd98149c2940` on Linux/Python 3.11 with the
locked dependencies:

- `python -m pytest -q`: **627 passed, 1 skipped**.
- `node tests/js/chart_smoke.mjs`: **passed**.
- The first pytest attempt could not run because pytest was not installed in
  the agent image; this was an environment setup issue, not a test failure. The
  ignored `.venv` was then created from `requirements-lock.txt`.

No real broker account was accessed. No credentials were requested or used.

## Current architecture map

- `app/config/settings.py`: legacy environment configuration; deliberately
  refuses non-PAPER process mode. Paper/US Alpaca guards are validated here.
- `app/api/server.py`: FastAPI composition, lifespan, local SQLite repository,
  legacy `PaperBroker` and `PaperSimulator`.
- `app/api/auth.py`: constant-time local `API_TOKEN` bearer gate; HTML pages may
  be loopback/proxy readable, but account data and controls remain protected.
- `app/brokers/sandbox.py`: documented Upstox sandbox adapter with durable
  intents, ambiguous-outcome handling and reconciliation. It is not a Vantage
  or real-money adapter.
- `app/paper_trading/`: local paper broker, simulated fills, manual ticket and
  simulator. `POST /paper/order` independently requires local paper mode.
- `app/engine/state.py`: durable sandbox submission journal and daily risk
  accounting; useful prior art, but provider-specific and not reused as an MT5
  response mapping.
- `app/risk/limits.py`: legacy hard entry limits.
- `app/database/`: additive SQLite schema, repository, paper state, orders,
  fills, events and exclusive writer lock.
- `app/data/`: replay, read-only Upstox and read-only Alpaca providers. The
  Alpaca path is US market data for local paper only.
- `app/charting/` and `app/static/chart/`: existing chart series, markers and
  interactive canvas terminal. Paper orders route through the same paper guard.
- `desktop/`: optional PySide viewer for legacy research/activity pages; it does
  not place orders.
- `packaging/windows/`: per-user data under `%LOCALAPPDATA%\AXIOM`, generated
  local API token, SQLite preservation, loopback launcher, PyInstaller portable
  executable, Inno Setup per-user installer and install/uninstall test.
- `.github/workflows/`: Linux paper checks and a Windows exact-commit build /
  packaging / prerelease workflow.

## Target Vantage architecture

1. **Official MT5 gateway (Windows):** lazy import of the documented
   `MetaTrader5` Python module. Initialize with terminal path, login, password
   and exact server; shut down cleanly. No web scraping and no coordinate/UI
   automation. A small gateway interface makes every broker interaction
   replaceable by a fake in tests.
2. **Connector normalization:** verify connected login/server and MT5 account
   trade mode; expose only masked identity. Discover symbols from
   `symbols_get()` in that same account. Normalize contract constraints,
   product/CFD labels, ticks, account state, rates, working orders, positions,
   history orders and deals. Unknown product/contract data is untradable.
3. **Protected credentials:** full login/password are kept only in a Windows
   DPAPI-encrypted per-user blob. SQLite stores non-secret metadata and masked
   login. APIs, audit events and errors never return the blob, password or full
   login. There is no plaintext fallback on other operating systems. AXIOM's
   separate local `API_TOKEN` remains the API bearer credential.
4. **Durable execution store:** additive SQLite tables for order intents,
   broker mappings/results, account/risk runtime state and redacted audit
   events. Intent and idempotency identity are committed before `order_send`.
   Exceptions/timeouts/unknown retcodes become unresolved; no blind retry.
5. **Reconciliation:** explicit startup/connect/reconnect/timeout/restart path
   reads broker truth and adopts only strict, unique matches. Working orders,
   positions, history orders and deals drive displayed state. UI responses
   never independently mark a fill, cancellation, close or flat account.
6. **Server-side guard chain:** selected and understood symbol, exact mode and
   identity, fresh valid ordered quote, session/trade permission, normalized
   volume/price/stops, bounded order/margin/exposure/positions/daily loss/
   drawdown/trade count/margin use, spread/slippage/quote age, sufficient
   margin, duplicate suppression, emergency stop and lifecycle state.
7. **Fail-closed lifecycle:** selected mode persists but connection does not
   auto-start. Armed/active state is memory-only and clears on restart,
   reconnect, ambiguous outcome, or identity/instrument/risk/mode changes.
   Readiness nonces plus fresh Arm and Activate phrases are separate. Pause
   blocks entries; Stop and Emergency Stop report broker-confirmed remainder.
   Automation is absent for Vantage and defaults off globally.
8. **Authenticated operator API and new shell:** paper continues through the
   legacy broker; demo/live can only use the Vantage connector. Quotes/charts
   for a Vantage session come from that same MT5 connection, never Alpaca.
9. **TradingView deferred:** no restricted library and no broker credentials in
   alerts. Any later webhook requires a separately deployed authenticated HTTPS
   relay, dedupe/timestamp/audit, and proposal-first routing. No local port is
   exposed by this version.

Official references checked:

- https://www.mql5.com/en/docs/python_metatrader5
- https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py
- https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py
- https://www.metatrader5.com/en/terminal/help
- https://www.vantagemarkets.com/trading/all-instruments/
- https://www.vantagemarkets.com/en/quick-question/can-i-trade-us-stocks-such-as-apple-tesla-and-nvidia-on-vantage/
- https://www.vantagemarkets.com/en/academy/how-to-use-metatrader-5/
- https://www.tradingview.com/lightweight-charts/
- https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/
- https://www.tradingview.com/free-charting-libraries/

Vantage's current US-stock answer says its available US-listed stock products
are CFDs and do not convey ownership of the underlying shares; availability is
jurisdiction dependent. AXIOM therefore discovers the actual account universe
rather than copying a website universe.

## Existing page/control inventory (before overhaul)

### Public landing `/`

- Links to `/chart`, `/dashboard`, `/health`, `/docs`: routes exist.
- Misleading for next version: says real-money routing is not implemented and
  reports a stale test count. Replace with accurate mode-neutral entry points;
  never claim live readiness.

### Classic dashboard `/dashboard`

- Account/feed/strategy/positions/events/quality/context/execution/health
  panels: populated by authenticated `/dashboard/data`; empty states are thin.
- `CHARTS + TRADE`: working navigation to `/chart`.
- `EMERGENCY STOP`: authenticated and persisted, pauses paper and invokes the
  legacy sandbox stop where present. Confirmation exists. Its text only
  mentions sandbox orders, so it is misleading for a multi-broker shell.
- No visible resume control despite `/engine/resume` existing; clearing an
  emergency stop from this page is therefore incomplete.
- `LIVE: NOT ENABLED IN V1`: accurate for legacy source but must not remain in
  a live-capable shell. API-token error text can be clicked to re-enter a token.
- Auto-refresh works, but tables have weak headers/empty states and several
  values are generic JSON rather than explanatory operator outcomes.

### Chart `/chart`

- Symbol selector, timeframe buttons, indicator buttons, zoom/pan/crosshair,
  reset, follow and keyboard shortcuts: implemented and covered by the JS
  harness and Python chart tests.
- Session start/stop: working paper calls with confirmation on stop.
- Dashboard links: working navigation.
- Buy/Sell, quantity, MAX, MARKET/LIMIT, limit price/click-to-set and place:
  working local paper order flow, with simulated result toasts. Sell is an
  exit, not a modelled short; this must remain clear.
- Paper risk-cap panel: read-only, real values.
- Misleading for Vantage if reused unchanged: fixed PAPER badge, US/Alpaca feed
  labels, quantity semantics rather than MT5 lots, no SL/TP, no account/server
  identity, no broker review/reconciliation, and no demo/live lifecycle.
- Empty state and feed labels are deliberately honest and must be preserved.

### Optional PySide desktop pages

- Command Center (question + Ask), Live Market, Portfolio, Trades, Performance,
  Strategies, Research Lab, Learning, Market Intelligence, Risk, Logs, Reports
  (period + Load report), Settings: mapped to existing authenticated reads.
- Emergency Stop and Resume: real legacy API calls; Stop has confirmation,
  Resume does not. Backend failures are reduced to `backend unreachable` and
  stale table rows can remain when a later read returns empty.
- Settings is read-only and correctly says the viewer cannot alter execution;
  it is not a Vantage credentials UI.
- These legacy research pages remain for compatibility, but the Windows web
  operator shell is the target for the complete page-by-page redesign.

### Windows launcher and package controls

- First-run config generation, loopback port selection, browser launch,
  `--no-browser`, Ctrl+C: implemented.
- Current launcher prints the local API token to the console and refuses every
  non-paper legacy `BROKER`; token display should be removed, while Vantage
  must remain an explicit in-app selection rather than an environment bypass.
- Existing install test covers install/launch/chart/static/token/uninstall but
  not upgrade preservation, credentials status, MT5 status or the new shell;
  extend it without contacting an account.

## Implementation and verification sequence

1. Add normalized models, DPAPI store, MT5 gateway/connector and durable store.
2. Add risk, readiness/lifecycle, review/submit/reconcile/cancel/close services.
3. Add authenticated API schemas/routes and aggregate paper/Vantage workspace.
4. Unit/integration-test fake MT5 success, rejection, timeout, disconnect,
   partial fill, duplicates, restarts, stale/out-of-order data and redaction.
5. Build the eight-page responsive operator shell and headless interaction
   tests; retain legacy chart/Forex/US paper regressions.
6. Update Windows launcher/spec/install/upgrade tests and manuals. Keep real
   MetaTrader5 optional for test builds, with status explicitly unavailable
   until installed on the operator's Windows MT5 host.
7. Run complete Linux and Windows suites. Publish a draft PR and exact-commit
   unsigned prerelease only after real assets and checksums are uploaded.
8. Report fake verification separately from Vantage demo verification. No demo
   account is available to the agent by default, so demo discovery/order
   acceptance remains unverified unless the owner later supplies a demo account
   locally. No live login or real-money order is permitted in agent testing.
