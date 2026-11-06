# Changelog

All notable verified changes on the AXIOM multi-market NSE (Upstox) & NASDAQ (Alpaca) Operator line are recorded here.

## `3.7.0a10` — Source-First Startup, End-to-End NSE/NASDAQ Live-Market PAPER Verification & Quantitative Audit

### 1. Source-First Local Startup (No Installer Required)
- Added `app/source_launcher.py`, root `axiom_launcher.py`, and Windows PowerShell `Start-Axiom.ps1`, plus `operator` mode in `app/__main__.py` (`python -m app operator`) and `run.py` (`python run.py --mode operator`), and deterministic repo-root bootstrap in `packaging/windows/axiom_launcher.py`.
- Added `--register-venv` (`axiom_source.pth` in `.venv` `site-packages`), `--check-deps` (actionable missing-package & `zoneinfo` preflight with exact install command), and `--smoke` (loopback `/health` + `/terminal` startup check with `0` broker calls and `0` user-data mutations).
- Added `tzdata==2026.6 ; platform_system == "Windows"` to `requirements-lock.txt` so `Asia/Kolkata` and `America/New_York` calendars work reliably on clean Windows Python setups.
- Preserved `%LOCALAPPDATA%\AXIOM` state and kept all live switches OFF/disarmed by default.

### 2. End-to-End NSE Instrument Reconciliation & Exact `instrument_key` Preservation
- Extended `app/autonomy/{models.py,upstox.py}` and `app/markets/nse_reconciliation.py` with explicit `asset_category` (`CASH_EQUITY_OR_ETF`, `DERIVATIVE`, `INDEX`), exact `NSE_EQ` Beginning-of-Day row-conservation reconciliation (`unexplained_nse_eq_row_gap == 0`), watch-only `NSE_INDEX` scoping, and `resolve_nse_instrument_strict`.
- Preserved exact Upstox `instrument_key` (e.g., `NSE_EQ|INE002A01018`) and exchange (`NSE`) from catalog search through watchlists, quotes, candles, charts, ensemble paper routing, and `LIVE-MARKET PAPER` orders/positions/fills without symbol-normalization loss.

### 3. Read-Only Upstox & Alpaca Live-Market PAPER Hardening (`0` Broker Orders)
- Hardened `AlpacaQuoteReader.latest_quote`, `verify_upstox_market_data`, `verify_alpaca_market_data`, `_nasdaq_live_bars_lookup` (`AlpacaHistoricalProvider.historical` with the `1d` → `1D` mapping), and `LiveMarketPaperEngine._validate_quote_for_paper` to fail closed on missing, stale, future-skewed, out-of-order (monotonic watermark), non-finite/negative, inverted (`0 < ask < bid`), closed-session, `delayed_sip`, rate-limited (`HTTP 429`), and disconnected feeds with zero silent sample/replay fallback, plus automatic reconnect recovery (`PAPER_SESSION_RECONNECTED`).
- Surfaced market, exact `instrument_key`, provider, feed entitlement (`IEX` single-venue vs `SIP` consolidated US tape vs `delayed_sip`), source timestamp, local receive timestamp, quote/bar age, and market-session status across API and UI.

### 4. Unambiguous PAPER Modes, Session Lifecycle & P&L Attribution
- Added `/api/markets/paper-modes` (`paper_modes` in `/overview`) and `/api/markets/paper/audit/export` distinguishing `LOCAL_REPLAY_PAPER`, `LIVE_MARKET_LOCAL_PAPER`, and `ALPACA_BROKER_PAPER`.
- Separated `current_session_realized_pnl` / `current_session_total_pnl` (`session_run_id`) from `persisted_historical_realized_pnl` in `LiveMarketPaperEngine` and UI ledgers, and labeled all local fills `SIMULATED` (`0` broker calls) vs `BROKER_PAPER_SANDBOX_CONFIRMED` / `BROKER_CONFIRMED`.

### 5. Strategy Ensemble & Kronos Audit, Neutral DPAPI Store & Legacy Isolation
- Added `evidence_classification` (`SYNTHETIC_ENGINEERING_EVIDENCE` when `real_market_data=false`), causal `RANGE`/`TREND` regime-sliced OOS breakdowns across `no_trade`, `buy_and_hold`, `baseline_moving_average`, `baseline_rsi`, `ensemble_no_kronos`, and `ensemble_with_kronos` at `1.0x` and `2.0x` worse-cost stress, and `methodology_audit_disclosures` in `app/ensemble/service.py`.
- Extracted neutral `app/security/dpapi_secrets.py` so active Upstox/Alpaca credential stores have zero imports from `app.vantage`, and documented every remaining legacy reference in `docs/VANTAGE_MT5_ISOLATION_AUDIT.md`.

### 6. NASDAQ Kronos Forecast Read-Only Bar Retrieval Fix
- Corrected the read-only NASDAQ bar call sites (`_nasdaq_live_bars_lookup` in `app/api/server.py` and the `/api/markets/chart` NASDAQ branch in `app/api/routes/markets.py`) from the non-existent `AlpacaHistoricalProvider.get_candles(...)` to the adapter's public `MarketDataProvider.historical(symbol, timeframe, start, end)`. The bug surfaced as `AttributeError: 'AlpacaHistoricalProvider' object has no attribute 'get_candles'`, which the forecast route reported as "read-only bars unavailable".
- Preserved the existing UTC `[start, end)` window, the `1d→1D` / `4h→1h` timeframe mapping and lookbacks (240/30/7 days), and the fail-closed `409` when historical data is unavailable (no mock/sample fallback, no forecast fabricated, no order capability touched).
- Added regression coverage in `tests/test_kronos_forecast_runtime_regression.py` that keeps the real `AlpacaHistoricalProvider` and mocks only `httpx.Client`, exercising `POST /api/model-lab/kronos/forecast` and `/api/markets/chart` end to end, so a wrong-method regression can no longer hide behind a hand-written fake.


### 7. Paper-Release Reliability & Documentation Closure (`3.7.0a10` reviewed source)
- **Cross-market PAPER timeout is now genuinely per-market.** `LiveMarketPaperEngine.advance_selected_markets_concurrently` previously shared one deadline across all markets and waited on the futures in list order, so time spent waiting on a slow NSE worker was deducted from NASDAQ's remaining budget and a NASDAQ cycle that finished inside its own window could be reported `TIMEOUT`. Each market now owns `[its submission, +market_timeout_seconds]` and the batch is awaited once; verdicts no longer depend on another market's latency or on market ordering (`tests/test_concurrent_multimarket_paper.py`).
- **Timing-only tests replaced with deterministic synchronisation.** The `time.sleep(1.5)` / `elapsed < 1.0` regression was rewritten around `threading.Event` gating: the NSE provider parks inside its worker, NASDAQ signals its own completion, and only then is the slow provider released. Added order-independence (`["NSE","NASDAQ"]` vs `["NASDAQ","NSE"]`) and background-runner isolation cases. No wall-clock thresholds remain.
- **`python -m app paper-live ...` forwards its arguments again.** `cmd_paper_live` referenced a module-level `argv` that does not exist (`NameError`) and the sub-parser rejected unknown flags. The tail of the command line is now split off before argparse (`split_forwarded_argv`) and handed verbatim to `run.py`; every other command keeps strict validation. Covered by `tests/test_cli_paper_live_forwarding.py`.
- **Hidden Vantage/MT5 live API removed from the supported application.** `app/api/routes/vantage.py` (`/api/trading/*`: session mode incl. `VANTAGE_LIVE`, DPAPI credentials, `arm`/`activate`, order review/submit/cancel, position modify, emergency stop) is no longer imported or mounted — every path returns `404`. It was never described in the product documentation and its operator UI was removed in `a9`; a documented live route the team cannot verify must not stay reachable. The dormant package remains for legacy isolation tests. Guarded by `tests/test_vantage_live_api_removal.py`; `/health` now reports `real_order_routing: DISABLED_PAPER_ONLY`, `vantage_api: REMOVED`.
- **Read-only latency metrics kept on a supported route.** The removed surface also served `/api/trading/performance`; `GET /api/performance` (authenticated, `paper_only`, `0` broker calls) exposes the same `LatencyTracker` summary for the Settings → Latency panel and tests.
- **Patched, audited dependency set.** `fastapi 0.141.1 → 0.162.0`, `starlette 0.52.1 → 1.10.0` (0.5x carries unpatched `PYSEC-2026-161/-248/-249/-2280/-2281`; every fix lands in 1.x, and the httpx-based `TestClient` was re-verified against 1.10.0 by the full suite), `certifi 2026.7.22 → 2026.10.23` (clears `GHSA-6q48-6c7w-9gw2` / `PYSEC-2026-4320`, the revoked emSign roots). `pip-audit -r requirements-lock.txt`: **no known vulnerabilities**.
- **Documentation now matches the source.** README states the reviewed source branch (`arena/8d3d005e-axiom`), that the default branch does not contain this application, and that **no release has been published**; checkout/update commands use the reviewed branch. New `docs/PAPER_RELEASE_AUDIT.md` records changes, exact test results, safety evidence and remaining (credential/broker-gated) validation.

---

## Prior Milestones on Branch
- **`3.7.0a9`**: Removed Vantage MT5 operator UI surfaces and MT5 configuration docs while keeping Upstox (NSE) and Alpaca (NASDAQ) intact.
- **`3.7.0a8`**: Added parallel strategy ensemble (`1,024` bounded variants), OOS empirical calibrator, single per-market `PortfolioRiskAllocator`, and strictly read-only `11 Live Trade Monitor`.
- **`3.7.0a7`**: Added `10 AI & Model Lab` with manifest-pinned CPU `NeoQuasar/Kronos-small` verification and fail-closed advisory integration.
- **`3.7.0a6`**: Added unified dark multi-market NSE (`INR ₹`) + NASDAQ (`USD $`) terminal (`01`–`09`), SQLite `LiveMarketPaperEngine`, and isolated Alpaca market-data vs broker gates.
