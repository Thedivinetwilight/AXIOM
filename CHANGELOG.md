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
- Hardened `AlpacaQuoteReader.latest_quote`, `verify_upstox_market_data`, `verify_alpaca_market_data`, `_nasdaq_live_bars_lookup` (`AlpacaHistoricalProvider.get_candles` `"1D"` fix), and `LiveMarketPaperEngine._validate_quote_for_paper` to fail closed on missing, stale, future-skewed, out-of-order (monotonic watermark), non-finite/negative, inverted (`0 < ask < bid`), closed-session, `delayed_sip`, rate-limited (`HTTP 429`), and disconnected feeds with zero silent sample/replay fallback, plus automatic reconnect recovery (`PAPER_SESSION_RECONNECTED`).
- Surfaced market, exact `instrument_key`, provider, feed entitlement (`IEX` single-venue vs `SIP` consolidated US tape vs `delayed_sip`), source timestamp, local receive timestamp, quote/bar age, and market-session status across API and UI.

### 4. Unambiguous PAPER Modes, Session Lifecycle & P&L Attribution
- Added `/api/markets/paper-modes` (`paper_modes` in `/overview`) and `/api/markets/paper/audit/export` distinguishing `LOCAL_REPLAY_PAPER`, `LIVE_MARKET_LOCAL_PAPER`, and `ALPACA_BROKER_PAPER`.
- Separated `current_session_realized_pnl` / `current_session_total_pnl` (`session_run_id`) from `persisted_historical_realized_pnl` in `LiveMarketPaperEngine` and UI ledgers, and labeled all local fills `SIMULATED` (`0` broker calls) vs `BROKER_PAPER_SANDBOX_CONFIRMED` / `BROKER_CONFIRMED`.

### 5. Strategy Ensemble & Kronos Audit, Neutral DPAPI Store & Legacy Isolation
- Added `evidence_classification` (`SYNTHETIC_ENGINEERING_EVIDENCE` when `real_market_data=false`), causal `RANGE`/`TREND` regime-sliced OOS breakdowns across `no_trade`, `buy_and_hold`, `baseline_moving_average`, `baseline_rsi`, `ensemble_no_kronos`, and `ensemble_with_kronos` at `1.0x` and `2.0x` worse-cost stress, and `methodology_audit_disclosures` in `app/ensemble/service.py`.
- Extracted neutral `app/security/dpapi_secrets.py` so active Upstox/Alpaca credential stores have zero imports from `app.vantage`, and documented every remaining legacy reference in `docs/VANTAGE_MT5_ISOLATION_AUDIT.md`.

---

## Prior Milestones on Branch
- **`3.7.0a9`**: Removed Vantage MT5 operator UI surfaces and MT5 configuration docs while keeping Upstox (NSE) and Alpaca (NASDAQ) intact.
- **`3.7.0a8`**: Added parallel strategy ensemble (`1,024` bounded variants), OOS empirical calibrator, single per-market `PortfolioRiskAllocator`, and strictly read-only `11 Live Trade Monitor`.
- **`3.7.0a7`**: Added `10 AI & Model Lab` with manifest-pinned CPU `NeoQuasar/Kronos-small` verification and fail-closed advisory integration.
- **`3.7.0a6`**: Added unified dark multi-market NSE (`INR ₹`) + NASDAQ (`USD $`) terminal (`01`–`09`), SQLite `LiveMarketPaperEngine`, and isolated Alpaca market-data vs broker gates.
