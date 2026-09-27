# Delivery verification — V0.6

Verified on **2026-09-20**, Linux, **CPython 3.13.14**. Commands were run in `trading_bot/`, not merely proposed.

## Results

| Check | Observed result |
|---|---|
| Required directory/file structure | All 55 distinct requested file paths present; package/data/results/logs/tests folders present |
| Import check | All 60 discovered `app` submodules imported successfully |
| Compilation | `python -m compileall -q app scripts run.py` succeeded |
| Static undefined/unused-name checks | `ruff check app scripts tests run.py --select F` passed |
| Test suite | **75 passed**, no warnings in final run |
| CSV import/cache | 1,600 synthetic daily candles imported successfully |
| Example backtest | 1,600 candles processed; 16 fills, 8 realized closing trades |
| Tournament | **92 runs**: 23 configurations × 2 symbols × 2 timeframes |
| Chronological validation | 960 training / 320 validation / 320 unseen-test candles |
| Walk-forward | **9 nonoverlapping test folds** using 360/120/120/120 windows |
| Robustness | **9 scenarios**: parameter, commission, slippage/spread, and chronological period variations |
| Paper replay | STOPPED normally; 16 orders, 16 fills, 8 closing trades, 1,600 persisted snapshots |
| Accounting parity | Backtest final equity matched paper replay final equity within 1e-8 under the same settings |
| Persistence | SQLite stored 93 backtests and 4 research reports; paper orders/fills/trades/snapshots were queried and counted |
| Minimal-dependency core | `python -S run.py --mode backtest ...` succeeded with third-party site packages unavailable |
| Actual API process | Started on `0.0.0.0:8000`; `/health` fetched over HTTP and checked |
| Graceful API shutdown/restart | Prior process shut down cleanly, then latest code restarted and health rechecked |

The equity values and scores in sample reports are **synthetic simulation outputs, not evidence of market profitability**.

## Actual API health response

```json
{
  "status": "ok",
  "version": "0.6.0",
  "mode": "PAPER",
  "live_trading": false,
  "real_order_routing": "NOT_IMPLEMENTED"
}
```

The sandbox preview deliberately has no private `API_TOKEN` configured, so data/control endpoints fail closed with 503. Set your own token locally to enable authenticated access. `/health` is public; `/docs` exposes the interface contract.

## Commands exercised

```bash
python scripts/make_sample_data.py
python -m compileall -q app scripts run.py
python -m pytest -q

python run.py --mode backtest \
  --database sqlite:///results/verification.db --output results/backtest

python run.py --mode tournament --datasets data/tournament_datasets.json \
  --database sqlite:///results/verification.db --output results/tournament

python run.py --mode validate \
  --database sqlite:///results/verification.db --output results/validate

python run.py --mode walk-forward \
  --database sqlite:///results/verification.db --output results/walk-forward

python run.py --mode robustness \
  --database sqlite:///results/verification.db --output results/robustness

python run.py --mode paper \
  --database sqlite:///results/paper_demo.db --output results/paper

python scripts/download_data.py --output results/imported.csv

python -S run.py --mode backtest \
  --database sqlite:///results/stdlib_check.db --output results/stdlib_check

python run.py --mode api --host 0.0.0.0 --port 8000 \
  --database sqlite:///results/api_preview.db
```

The actual HTTP health check used Python's `urllib.request.urlopen` against `http://127.0.0.1:8000/health`. This is a sandbox-local test; browser-facing users access the proxied preview or their own locally deployed server, not the sandbox's localhost.

## Tests include

- Indicator known values, warmups, invalid periods, finite values, and all seven strategies.
- BUY/SELL/HOLD/EXIT generation, parameter validation, trailing-window behavior and future-prefix isolation.
- Invalid/missing OHLCV, naive timestamps, duplicates, ordering, exact-only deduplication, cache invalidation and calendar-aware gaps.
- Next-open execution, no final-candle fills, no pyramiding, long-only quantity restrictions, insufficient cash and partial fills.
- Limit touch behavior, commissions, slippage/spread decomposition, conservative same-candle stops/take-profit, stop gaps and volume-limited exits.
- Cash/equity/realized/unrealized reconciliation, daily P&L, drawdown, win/loss/expectancy/profit-factor calculations.
- Chronological splits, untouched-test selection invariance, one-use holdout, disjoint walk-forward windows, robustness scenarios and composite risk/cost penalties.
- Paper/backtest parity, pause/resume/stop, malformed data errors, data-gap pause, future-candle rejection and replay restart idempotency.
- SQLite rollback, restoration, and prevention of competing paper writers.
- Required API routes, authentication, missing token fail-closed behavior, rejected traversal/unknown fields, backtest persistence and lifecycle control.
- **Mocked** HTTP historical download/cache and **mocked** WSS reconnect/Bearer auth/ping heartbeat; token absence from logs.

## Fixes and refinements made during verification

- Corrected an order-table SQL placeholder count found by the first persistence test.
- Made replay restart skip previously committed older candles while keeping live out-of-order data fail-closed.
- Added conflicting-duplicate and unfinished/future live-candle rejection.
- Made spread/slippage cost attribution explicit without double-debiting cash.
- Preserved protective-exit reasons on partial-fill remainders and conservative intrabar exposure accounting.
- Connected configured calendars to the API and persisted calendar signatures for account consistency.
- Constrained the tested Starlette major version to retain the httpx TestClient without introducing another HTTP client dependency.
- Reformatted source and removed unused imports/variables; reran all tests and all sample workflows afterward.

## Artifacts

- `results/backtest/`: full backtest JSON, CSVs, SVG equity chart.
- `results/tournament/result.json`: 92-run composite ranking with per-component scores.
- `results/validate/result.json`: selection history and unseen-test result.
- `results/walk-forward/result.json`: folds, diagnostics and linked test-equity statistics.
- `results/robustness/result.json`: baseline, scenario results and warnings.
- `results/paper/result.json`: paper status, account, orders and trades.
- `results/pytest.txt`, `results/health.json`, `results/verification_summary.json`, `results/module_inventory.txt`: check records.

Large populated verification databases were inspected, then removed from the delivery; the archive contains reproducible source/data and result artifacts, **not a previously funded/persisted paper account**. The preview server's small live runtime database is also excluded from the archive. New local runs initialize fresh databases automatically.

## What was not verified

- No real-money orders were attempted or possible through an implemented adapter.
- No external Binance/generic live-feed availability claim is made. Real network subscriptions/downloads depend on provider access; adapter behavior was exercised using mocks.
- No physical Android/Termux device, Windows PC, or remote production VPS was tested.
- Python 3.11/3.12 compatibility is targeted in source/API choices, but the executed suite used Python 3.13.14.
- No tick-level/exchange fill fidelity, future profitability, statistical generalization, or production security certification is implied.

See `README.md` for all execution assumptions, architecture roles, exact setup instructions, authentication, limitations and safe extension points.
