# AXIOM Parallel Strategy Ensemble, Portfolio Risk Allocator & Live Trade Monitor — Methodology Audit, Synthetic Engineering Evidence & Benchmarks (`v3.7.0a10`)

> **SYNTHETIC ENGINEERING EVIDENCE — NOT EXPECTED RETURNS OR LIVE READINESS**
> - All strategy evaluation, empirical calibration, walk-forward weighting, Kronos advisory blending, and ablation studies are **PAPER / RESEARCH ONLY** (`live_execution_authorized = false`). Nothing in `app/ensemble/` or `app/markets/trade_monitor.py` can arm, activate, or submit a real broker order.
> - Every ablation result computed on AXIOM's bundled OHLCV fixtures (`bundled_nse_synthetic_1h` and `bundled_nasdaq_synthetic_1h`, `real_market_data = false`) is explicitly classified in code and UI as **`SYNTHETIC_ENGINEERING_EVIDENCE` (`expected_returns_claim = false`, `live_readiness_claim = false`)**. These results verify chronological split/embargo machinery, fee/slippage/partial-fill math, and allocator risk controls — **they are NOT expected returns, NOT strategy profitability proof, and NOT live-readiness evidence.**
> - Risk thresholds, calibration cutoffs, and exposure caps were **NOT tuned or relaxed** to improve headline metrics.
> - Real Upstox (NSE) and Alpaca (NASDAQ) owner-account market-data verification, real-market Kronos incremental out-of-sample (OOS) value (`ensemble_with_kronos` minus `ensemble_no_kronos`), and live broker order execution remain **`NOT VERIFIED`** in this environment.

---

## 1. Architecture Summary

### 1.1 Parallel Strategy Registry & Bounded Evaluator (`app/ensemble/registry.py`)
- **8 Strategy Families (`v1.0.0`)**: `sma_crossover`, `ema_crossover`, `rsi_reversion`, `bollinger_reversion`, `donchian_breakout`, `macd_momentum`, `vwap_zscore_reversion`, and `volatility_regime_trend`.
- **Configurable Presets & Custom Variants**: `compact` (`24` variants), `standard` (`96` variants), `large_256` (`256` variants), `massive_1024` (`1,024` unique canonical variants), plus custom JSON registration.
- **Canonical Deduplication**: Every variant computes a deterministic SHA-256 `canonical_signature` over `(family, sorted_normalized_params)`. Duplicate variants are flagged (`duplicate_of`) and skipped before voting so identical variants never double-count.
- **Strict Completed-Bar Gate (`validate_completed_bars`)**: Rejects missing/empty series, timezone-naive timestamps, duplicate timestamps, out-of-order bars, non-finite or malformed OHLCV geometry (`high < max(open, close)`, `low > min(open, close)`), cross-market symbol mismatches, unexplained intraday bar gaps, and stale bars. Any actively forming unclosed tail bar (`bar_open + timeframe > now_utc`) is stripped before indicator calculation so strategies evaluate **completed bars only**.
- **Single-Fetch Memoized Parallelism (`SharedIndicatorContext` + `BoundedParallelEvaluator`)**: Historical bars and read-only quotes are fetched **once per symbol** (never per strategy variant). Rolling indicators (SMA, EMA, Wilder RSI, Bollinger std, ATR, VWAP, Donchian bounds, MACD histogram, 20-bar realized vol) are memoized once per instrument and evaluated across a bounded `ThreadPoolExecutor` (`1..32` workers, configurable timeout).

### 1.2 Empirical OOS Confidence Calibration & Walk-Forward Weighting (`app/ensemble/calibration.py`)
- **Raw Scores Are Never Treated as Probabilities**: Raw strategy outputs in `[-1, +1]` are explicitly marked `raw_score_is_probability = false`.
- **Monotone Empirical Calibration (`EmpiricalCalibrator`)**: Fits Pool-Adjacent-Violators (isotonic) monotone bins with `Beta(2, 2)` Bayesian shrinkage strictly on chronological out-of-sample walk-forward observations, reporting OOS sample size `N`, 90% Wilson confidence intervals, Brier score, Expected Calibration Error (ECE), and per-regime (`TREND`, `RANGE`, `VOLATILE`) sample sufficiency. When uncalibrated or `N < min_oos_samples`, `calibrated_confidence` returns `None` and the allocator emits `NO_TRADE`.
- **Multiple-Testing Guarded Walk-Forward Weighting (`WalkForwardWeightLearner`)**: Evaluates all `M` variants across expanding chronological OOS folds inside `[0 : dev_end)`, applies a multiple-testing Sharpe haircut proportional to `sqrt(2 * ln(M) / N_oos_folds)`, requires positive net return under **both `1.0x` and `2.0x` stressed transaction costs** plus minimum OOS trade count, sets rejected variants to `0.0` weight, and keeps `untouched_test` (`[dev_end + embargo : end)`) 100% unseen.

### 1.3 Single Per-Market Portfolio Risk Allocator (`app/ensemble/allocator.py`)
Individual strategies have zero access to order APIs. Exactly **one `PortfolioRiskAllocator` per market/account** (`NSE` in `INR ₹`, `NASDAQ` in `USD $`) combines weighted signals, optional pinned Kronos advisory output (`weight <= 0.25`, with configurable `REQUIRE_KRONOS` fail-closed `NO_TRADE` vs `DEGRADE_TO_NON_KRONOS` policy), and **12 account-level pre-trade risk checks**:
1. `idempotency_bar_lock` — SQLite WAL unique constraint guaranteeing at most **one** decision per `(market, symbol, timeframe, bar_timestamp)` across repeats and restarts.
2. `market_session_open` — NSE (`09:15–15:30 IST`) and NASDAQ (`09:30–16:00 ET`, including early-close calendar) regular session check.
3. `kronos_policy_check` — Verifies `mock_outputs_used == False`, `advisory_only == True`, `manifest_sha256`, and `freshness_state == "FRESH"`; never fabricates forecasts.
4. `quote_freshness` — Enforces `quote_age_seconds <= max_quote_age_seconds` on real read-only quotes.
5. `oos_strategy_weights` — Requires positive walk-forward OOS weight sum.
6. `signal_consensus` — Rejects neutral or opposing/conflicting strategy ensembles (`conflict_ratio > max_conflict_ratio`).
7. `calibrated_confidence_and_oos_evidence` — Enforces `calibrated_confidence >= min_calibrated_confidence` and `oos_sample_size >= min_oos_samples`.
8. `daily_loss_and_drawdown` — Enforces account daily loss budget and peak-to-trough max drawdown ceiling.
9. `existing_orders_and_positions` — Blocks conflicting working orders on the same symbol and blocks naked cash/delivery short sales when flat.
10. `liquidity_and_spread` — Enforces minimum completed-bar volume and maximum bid-ask spread (`bps`).
11. `open_position_limit` & `correlation_and_concentration` — Enforces max concurrent open positions, trailing Pearson return correlation (`rho <= max_position_correlation`), and correlated-cluster notional cap across held positions.
12. `symbol_and_total_exposure` & `risk_budget_position_sizing` — Accounts for held positions **plus** working BUY orders, sizes quantity from `(equity * per_trade_risk_pct) / (stop_distance + round_trip_cost_per_unit)`, caps by bar-volume participation (`max_bar_participation`) and available cash, and rounds strictly to market `lot_size` and `tick_size`.

### 1.4 Read-Only "Live Trade Monitor" (`app/markets/trade_monitor.py` & `11 · Live Trade Monitor`)
- **100% Read-Only**: Exposes `GET /api/markets/trade-monitor` and `GET /api/markets/trade-monitor/chart` only (`POST`/`PUT`/`DELETE`/`PATCH` return `405`). Contains zero order-mutation forms or buttons; links directly to `04 · Orders & Positions` and `03 · Charts & Ticket`.
- **8 Visually Distinct Canvas Event Glyphs**:
  - `SIGNAL` -> `diamond_hollow` (sky-blue hollow diamond; never resembles an order or fill)
  - `ORDER_SUBMITTED` -> `square_hollow` (amber hollow square)
  - `PARTIAL_FILL` -> `circle_half` (orange half-filled square/circle)
  - `COMPLETED_FILL` -> `triangle_solid` (solid directional fill triangle)
  - `CANCELED` -> `cross_x` (slate diagonal `✕`)
  - `STOP_LOSS` -> `shield_sl` (red protective shield + dashed SL line)
  - `TAKE_PROFIT` -> `target_tp` (green concentric target + dashed TP line)
  - `EXIT` -> `chevron_exit` (purple double exit chevron `»`)
- **Unmistakable `PAPER` vs `LIVE` & Honest `"NOT TRADING"` Banner**: Simulated paper fills are labeled `SIMULATED (LOCAL PAPER LEDGER)` (`PAPER · SIMULATED FILLS`); broker journal fills are labeled `BROKER_CONFIRMED`. Sessions that are stopped, paused, disarmed, or `RUNNING` while merely scanning with `0` positions, `0` working orders, and `0` fills explicitly display **`NOT TRADING`**. Missing or disconnected feeds render `0` bars (`sample_data_used = false`).

---

## 2. Reproducible Chronological Walk-Forward & Untouched OOS Ablation

### 2.1 Dataset Provenance & Chronological Splits
| Market | Dataset ID | Source | SHA-256 | Bars | Dev Walk-Forward (`[0:1248)`) | Embargo | Untouched OOS Test (`[1253:1600)`) | Real Market Data? |
|---|---|---|---|---:|---|---:|---|---|
| **NSE (`INR ₹`)** | `bundled_nse_synthetic_1h` | `data/sample_1h.csv` | `1fc4cc696eaa9e86b6169d15e6a7832dd158e50bdbf5afa8d9c6d567137762fa` | `1,600` (`1h`) | `2021-01-01T00:00Z` – `2021-02-21T23:00Z` (3 OOS folds = `672` OOS bars) | `5` bars | `2021-02-22T05:00Z` – `2021-03-08T15:00Z` (`347` bars; `selection_touched_test=false`) | **`false`** (Synthetic fixture) |
| **NASDAQ (`USD $`)** | `bundled_nasdaq_synthetic_1h` | `data/sample_us_1h.csv` | `75e820d6528995c616315f3ef523d269da1f42521aa44edc778c16eaf30d93c6` | `1,600` (`1h`) | `2021-01-01T00:00Z` – `2021-02-21T23:00Z` (3 OOS folds = `672` OOS bars) | `5` bars | `2021-02-22T05:00Z` – `2021-03-08T15:00Z` (`347` bars; `selection_touched_test=false`) | **`false`** (Synthetic fixture) |

### 2.2 Multiple-Testing Rejection & Empirical Calibration Summary (`standard` 96-Variant Library)
- **NSE (`INR`)**: `96` variants evaluated on development OOS folds -> Sharpe haircut `0.2564` + `2.0x` cost stress **rejected `39` variants** and retained `57` OOS-validated variants. Empirical calibrator fitted on `N = 662` OOS observations: **Brier = `0.23187`**, **ECE = `0.01171`** (`TREND` empirical hit rate `64.49%`, 90% CI `[60.54%, 68.26%]`; `RANGE` hit rate `58.87%`, 90% CI `[53.66%, 63.89%]`).
- **NASDAQ (`USD`)**: `96` variants evaluated on development OOS folds -> Sharpe haircut `0.2564` + `2.0x` cost stress **rejected `36` variants** and retained `60` OOS-validated variants. Empirical calibrator fitted on `N = 663` OOS observations: **Brier = `0.22135`**, **ECE = `0.00365`** (`TREND` hit rate `69.67%`, 90% CI `[66.10%, 73.01%]`; `RANGE` hit rate `54.59%`, 90% CI `[48.55%, 60.51%]`).

### 2.3 Untouched Out-of-Sample & `2.0x` Stressed Cost Results (`NSE · INR`)
| Candidate | Status | `1.0x` Net Return | `1.0x` Sharpe / Sortino | `1.0x` Max DD | Profit Factor | Trades / `NO_TRADE` Bars | Win Rate `[90% CI]` | Exposure / Turnover | `2.0x` Cost-Stress Return (Sharpe) |
|---|---|---:|---:|---:|---:|---:|---|---:|---:|
| `no_trade` | EVALUATED | `0.00%` | `0.00` / `0.00` | `0.00%` | `0.00` | `0` / `346` | `0.0%` `[0.0–100.0%]` | `0.0%` / `0.00x` | `0.00%` (`0.00`) |
| `buy_and_hold` | EVALUATED | `-2.88%` | `-4.62` / `-5.90` | `4.11%` | `0.00` | `1` / `345` | `0.0%` `[0.0–73.0%]` | `100.0%` / `0.15x` | `-2.89%` (`-4.65`) |
| `baseline_moving_average` (`SMA 10/30`) | EVALUATED | `+9.76%` | `7.43` / `13.37` | `2.25%` | `9.84` | `4` / `339` | `50.0%` `[18.2–81.8%]` | `39.9%` / `1.14x` | `+9.66%` (`7.34`) |
| `baseline_rsi` (`RSI 14`) | EVALUATED | `-15.28%` | `-13.12` / `-13.74` | `16.17%` | `0.00` | `2` / `342` | `0.0%` `[0.0–57.5%]` | `59.5%` / `0.75x` | `-15.34%` (`-13.17`) |
| **`ensemble_no_kronos`** (Calibrated OOS + Risk Allocator) | **EVALUATED** | **`+2.83%`** | **`5.56` / `9.05`** | **`1.56%`** | **`2.61`** | **`13` / `321`** | **`61.5%` `[39.3–79.8%]`** | **`36.4%` / `2.24x`** | **`+2.61%` (`5.26`)** |
| `ensemble_with_kronos` (Advisory Kronos ON) | `SKIPPED_KRONOS_UNAVAILABLE` | — | — | — | — | — | — | — | **NOT VERIFIED** locally (weights not cached in CI sandbox; 0 mock forecasts used) |

### 2.4 Untouched Out-of-Sample & `2.0x` Stressed Cost Results (`NASDAQ · USD`)
| Candidate | Status | `1.0x` Net Return | `1.0x` Sharpe / Sortino | `1.0x` Max DD | Profit Factor | Trades / `NO_TRADE` Bars | Win Rate `[90% CI]` | Exposure / Turnover | `2.0x` Cost-Stress Return (Sharpe) |
|---|---|---:|---:|---:|---:|---:|---|---:|---:|
| `no_trade` | EVALUATED | `0.00%` | `0.00` / `0.00` | `0.00%` | `0.00` | `0` / `346` | `0.0%` `[0.0–100.0%]` | `0.0%` / `0.00x` | `0.00%` (`0.00`) |
| `buy_and_hold` | EVALUATED | `-16.52%` | `-5.10` / `-6.48` | `23.35%` | `0.00` | `1` / `345` | `0.0%` `[0.0–73.0%]` | `100.0%` / `0.78x` | `-16.62%` (`-5.13`) |
| `baseline_moving_average` (`SMA 10/30`) | EVALUATED | `+7.52%` | `5.18` / `8.79` | `1.99%` | `99.00` | `3` / `341` | `100.0%` `[52.6–100.0%]` | `39.9%` / `1.60x` | `+7.30%` (`5.05`) |
| `baseline_rsi` (`RSI 14`) | EVALUATED | `-12.92%` | `-9.88` / `-11.07` | `13.70%` | `0.00` | `2` / `342` | `0.0%` `[0.0–57.5%]` | `60.4%` / `0.83x` | `-12.97%` (`-9.95`) |
| **`ensemble_no_kronos`** (Calibrated OOS + Risk Allocator) | **EVALUATED** | **`+3.60%`** | **`6.10` / `11.02`** | **`0.65%`** | **`8.92`** | **`7` / `333`** | **`71.4%` `[40.9–90.0%]`** | **`37.9%` / `1.64x`** | **`+3.31%` (`5.87`)** |
| `ensemble_with_kronos` (Advisory Kronos ON) | `SKIPPED_KRONOS_UNAVAILABLE` | — | — | — | — | — | — | — | **NOT VERIFIED** locally (weights not cached in CI sandbox; 0 mock forecasts used) |

### 2.5 Regime-Sliced Out-of-Sample Comparison (`RANGE` vs `TREND`, Synthetic Engineering Evidence)

Causal bar regimes (`classify_bar_regime`) evaluated strictly on trailing completed bars at each untouched OOS step (`selection_touched_test = false`):

- **NSE Synthetic Fixture (`bundled_nse_synthetic_1h`, `346` OOS steps = `124 RANGE` + `222 TREND`)**:
  - `buy_and_hold`: `RANGE` **`-0.35%`**, `TREND` **`-2.54%`** (`2.0x` cost-stress total **`-2.89%`**)
  - `baseline_moving_average` (`SMA 10/30`): `RANGE` **`+0.32%`**, `TREND` **`+9.41%`** (`4` trades total; `2.0x` cost-stress **`+9.66%`**)
  - `baseline_rsi` (`RSI 14`): `RANGE` **`-0.86%`**, `TREND` **`-14.54%`** (`2.0x` cost-stress **`-15.34%`**)
  - `ensemble_no_kronos` (Single `PortfolioRiskAllocator`, Kronos OFF): `RANGE` **`+0.08%`** (`62` active bars), `TREND` **`+3.97%`** (`76` active bars), `2.0x` cost-stress total **`+3.80%`**
  - `ensemble_with_kronos` (Paired Advisory Kronos ON): **`NOT VERIFIED`** on real OOS market data (`SKIPPED_KRONOS_UNAVAILABLE` offline; `0` mock forecasts substituted)

- **NASDAQ Synthetic Fixture (`bundled_nasdaq_synthetic_1h`, `346` OOS steps = `134 RANGE` + `212 TREND`)**:
  - `buy_and_hold`: `RANGE` **`+1.93%`**, `TREND` **`-18.10%`** (`2.0x` cost-stress total **`-16.62%`**)
  - `baseline_moving_average` (`SMA 10/30`): `RANGE` **`+3.84%`**, `TREND` **`+3.55%`** (`3` trades total; `2.0x` cost-stress **`+7.30%`**)
  - `baseline_rsi` (`RSI 14`): `RANGE` **`-0.41%`**, `TREND` **`-12.56%`** (`2.0x` cost-stress **`-12.97%`**)
  - `ensemble_no_kronos` (Single `PortfolioRiskAllocator`, Kronos OFF): `RANGE` **`+2.05%`** (`70` active bars), `TREND` **`+1.52%`** (`64` active bars), `2.0x` cost-stress total **`+3.28%`**
  - `ensemble_with_kronos` (Paired Advisory Kronos ON): **`NOT VERIFIED`** on real OOS market data (`SKIPPED_KRONOS_UNAVAILABLE` offline; `0` mock forecasts substituted)

### 2.6 Full Quantitative Methodology & Leakage / Realism Audit Disclosures

| Audit Dimension | Verified Behavior in Code | Remaining Real-World Limitation |
|---|---|---|
| **Single Risk Allocator & Zero Bypass** | Individual strategy functions (`evaluate_variant_on_context`) return pure `StrategyCandidateSignal` dataclasses and hold zero references to `paper_engine` or broker clients. Every automated ensemble trade must pass through `PortfolioRiskAllocator.allocate_symbol()`. | Applies to ensemble/paper routing; live gates remain separately disabled by default. |
| **Kronos Advisory Role & Paired Ablation** | `kronos_weight` is hard-capped at `<= 0.25`; Kronos cannot override risk checks or submit orders alone. Paired `use_kronos=False` vs `use_kronos=True` runs on identical bars, splits, and `1.0x`/`2.0x` costs. | Real-market incremental OOS value of `NeoQuasar/Kronos-small` is **`NOT VERIFIED`** in this environment (weights/real feeds unavailable in sandbox). |
| **Chronological Splits, Embargo & Leakage** | Strict forward-only `[0 : dev_end)` expanding walk-forward folds, `embargo_bars >= 5` gap, and `[dev_end + embargo : end)` untouched OOS test (`selection_touched_test = false`). Unclosed tail bars are stripped (`validate_completed_bars`). | Single-series fixtures test temporal leakage only, not cross-sectional point-in-time fundamental joins. |
| **Survivorship Bias** | `methodology_audit_disclosures.survivorship_status` explicitly reports `"UNVERIFIED_STATIC_SYMBOL_SELECTION"` unless a point-in-time survivorship-aware universe (`survivorship_aware = true`) is supplied. | Today's NSE BOD or Alpaca `/v2/assets` list omits delisted/bankrupt historical tickers; backtests on static current constituents carry survivorship bias. |
| **Corporate Actions (Splits/Cash Dividends)** | `methodology_audit_disclosures.corporate_actions_status` now derives from verified action data: `CORPORATE_ACTIONS_VERIFIED` only when a hash-pinned split/dividend feed backs the claimed basis, otherwise `PARTIALLY_VERIFIED_CORPORATE_ACTION_ADJUSTMENT` / `UNVERIFIED_CORPORATE_ACTION_ADJUSTMENT` with explicit limitations. Stored dataset bars stay raw; adjusted series are derived per run (`docs/CORPORATE_ACTIONS.md`). | Bonus issues, rights issues, mergers, demergers, spin-offs, delistings and symbol changes are **unsupported** — recorded and labelled, never adjusted. Dividend cash is a research-ledger responsibility (no tax/withholding or FX conversion). |
| **Fees, Spreads, Slippage & Partial Fills** | Models full statutory NSE (`STT`, NSE txn, SEBI, stamp duty, `18%` GST) and US (`SEC Section 31`, `FINRA TAF`) fees, half-spread + adverse slippage (`1.0x` and `2.0x` stress), tick rounding against the trader, and `max_bar_participation = 10%` bar-volume partial-fill caps. | Completed-bar next-open fill model does not simulate Level-2 limit-order queue priority or auction Halt/LULD microstructure. |
| **Short-Sale & Product Rules** | NSE `CNC` (`D`) and US `CASH` enforce long-only cash delivery (`SELL` requires existing settled/open long quantity; naked short entries are rejected). NSE `MIS` intraday requires explicit product selection during open IST session hours. | Intraday stock-borrow recalls and NSE F&O ban lists are not modeled in cash-equity paper. |
| **Session & Holiday Calendar Handling** | NSE checks `Asia/Kolkata` (`09:15–15:30 IST`, holiday list); NASDAQ checks `America/New_York` (`09:30–16:00 ET`, NYSE/NASDAQ holidays + `13:00 ET` early closes). Off-hours market entries fail closed. | Extraordinary unscheduled exchange halts require live feed status updates. |

---

## 3. Parallel Strategy Evaluation Scaling Benchmarks (`BoundedParallelEvaluator`, Max Workers = `4`)

Measured on `180`-bar completed `5m` OHLCV series per instrument (`0` broker calls, `1` `SharedIndicatorContext` computation per instrument):

| Variants | Instruments | Total Strategy Evaluations | Workers Used | Wall Latency (`ms`) | Per-Instrument `p50` / `p95` (`ms`) | Throughput (`evals/sec`) | Peak Heap Delta (`MB`) | Process RSS (`MB`) | Missed / Duplicate Bars | UI Non-Blocking (`< 2.5s`) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `24` | `1` | `24` | `1` | **`0.92 ms`** | `0.90` / `0.90 ms` | `26,203 /s` | `0.05 MB` | `30.01 MB` | `0 / 0` | PASS |
| `24` | `16` | `384` | `4` | **`16.18 ms`** | `0.81` / `1.23 ms` | `23,731 /s` | `0.86 MB` | `32.61 MB` | `0 / 0` | PASS |
| `96` | `8` | `768` | `4` | **`15.40 ms`** | `1.74` / `1.88 ms` | `49,858 /s` | `0.98 MB` | `34.20 MB` | `0 / 0` | PASS |
| `96` | `16` | `1,536` | `4` | **`32.30 ms`** | `1.85` / `10.31 ms` | `47,554 /s` | `1.96 MB` | `36.17 MB` | `0 / 0` | PASS |
| `256` | `8` | `2,048` | `4` | **`33.16 ms`** | `3.90` / `6.59 ms` | `61,762 /s` | `2.30 MB` | `39.41 MB` | `0 / 0` | PASS |
| `256` | `16` | `4,096` | `4` | **`68.43 ms`** | `4.52` / `17.88 ms` | `59,858 /s` | `4.66 MB` | `44.13 MB` | `0 / 0` | PASS |
| **`1,024`** | **`1`** | **`1,024`** | `1` | **`14.83 ms`** | `13.06` / `13.06 ms` | **`69,044 /s`** | `0.88 MB` | `50.12 MB` | `0 / 0` | PASS |
| **`1,024`** | **`4`** | **`4,096`** | `4` | **`79.33 ms`** | `39.07` / `53.78 ms` | **`51,633 /s`** | `3.51 MB` | `50.12 MB` | `0 / 0` | PASS |
| **`1,024`** | **`8`** | **`8,192`** | `4` | **`114.40 ms`** | `28.00` / `35.30 ms` | **`71,606 /s`** | `7.02 MB` | `54.27 MB` | `0 / 0` | PASS |
| **`1,024`** | **`16`** | **`16,384`** | `4` | **`247.32 ms`** | `33.19` / `92.16 ms` | **`66,246 /s`** | `14.07 MB` | `69.36 MB` | `0 / 0` | PASS |

---

## 3.1 100-Symbol Single-Cycle Scan Capacity (`app/markets/fetch_gate.py`, 2026-11-07)

Every market-data provider in AXIOM is a **per-symbol** callable, so one watchlist
cycle costs two requests per selected symbol (one completed-bar series + one
quote). The published §3 numbers stop at `16` instruments; nothing there is
extrapolated here. This section measures `100` symbols directly, through the real
`analyze_market` (scan) and `sync_live_tick` (cycle: fetch → ensemble →
`PortfolioRiskAllocator` → paper order plan) paths, with **deterministic synthetic
bars/quotes and fake provider transports**:

```bash
python scripts/benchmark_market_scan.py --symbols 100 \
    --json docs/market_scan_capacity_100.json      # ~5 min, 2 vCPU
```

Machine-readable record: **`docs/market_scan_capacity_100.json`**
(schema `axiom.market_scan_capacity.v1`). Environment: 2 vCPU, CPython 3.11,
`0` network calls, `0` broker order calls, live execution never constructed,
session clock pinned to `2026-01-05T10:00:00+05:30` (NSE `REGULAR_OPEN`),
`220` synthetic `5m` bars per symbol, `24` ensemble variants per symbol.

### 3.1.1 What was measured

| Arm (provider latency, budget) | Scan `analyze_market` | ms / symbol | Full cycle, cold / warm | Provider requests / cycle | 429s | Orders | Open positions |
|---|---:|---:|---:|---:|---:|---:|---:|
| `sequential-reference-100ms` (1 worker, 100 ms) | `20,244 ms` | `202.4` | `26,840 / 20,277 ms` | `600` (3 cycles) | `0` | `6` | `6` |
| **`bounded-8w-50rps-100ms`** (8 workers, 50 rps) | **`2,746 ms`** | **`27.5`** | **`8,734 / 4,015 ms`** | `600` | `0` | `6` | `6` |
| `bounded-8w-50rps-250ms` (slow provider) | `6,630 ms` | `66.3` | `12,429 / 6,724 ms` | `600` | `0` | `6` | `6` |
| `provider-429-at-20rps` (provider serves 20 rps) | `20,469 ms` | `204.7` | `23,911 / 16,660 ms` | `644` | `44` | `6` | `6` |
| `correlated-cluster-100syms` (identical series) | `1,150 ms` | `11.5` | `8,516 / 4,001 ms` | `600` | `0` | `1` | `1` |
| `partial-failures` (15 injected bad symbols) | `1,406 ms` | `14.1` | `8,373 / 4,206 ms` | `633` | `0` | `5` | `5` |

* **Scan** = all `100` symbols fetched + ensemble-previewed, no orders.
  **Cycle** = three consecutive `sync_live_tick` runs on one engine (cold = first
  tick with an empty book, warm = steady state).
* Per-request provider latency in the bounded arm: `p50 = 100.7 ms`,
  `p95 = 115.9 ms`, `max = 173.4 ms` (the fake adds `100 ms` plus local
  scheduling). Across the three ticks the gate slept `15.0 s` in total (≈`5 s` per
  tick), i.e. the request budget — not CPU — is the binding constraint at
  `50 rps`.
* Fan-out is bounded at `8` workers (`peak_inflight = 8`, never higher).
* Memory: `tracemalloc` peak `14.0 MB`, process RSS delta `26.3 MB` for a
  100-symbol cycle (`12.4 MB` / `5–30 MB` on the failure arms).
* Determinism: re-running the same arm produces the identical decision
  fingerprint (`284a5a3f3ebcde49`), and the bounded and sequential scans produce
  the **same per-symbol results** — fan-out changes latency only.
* Failure isolation: `15` injected failures (`5` provider error, `3` timeout,
  `4` empty bar series, `3` stale quote) flag exactly those `15` symbols;
  `0` healthy symbols were flagged, the other `85` results are byte-identical to
  the clean run.

### 3.1.2 The five capacity measures (limits unchanged)

| # | Measure | Value in this tree | Where |
|---|---|---|---|
| 1 | Symbols accepted in a watchlist | **unbounded** (SQLite rows; no cap in `add_watchlist_symbol`) | `live_paper.py` |
| 2 | Symbols fetched + analyzed per cycle | **all selected symbols + held positions**; bounded fan-out `8` workers, `50 rps`, `45 s` batch deadline | `fetch_gate.py`, `analyze_market` |
| 3 | Candidates that can pass portfolio risk checks | one decision per symbol, then risk: confidence `≥ 0.53`, pairwise correlation `< 0.80`, cluster `≤ 0.35`, symbol `≤ 25 %` equity, total `≤ 80 %` equity | `allocator.py` |
| 4 | Orders that could be submitted | `max_daily_orders = 40` per market per day | `PaperMarketConfig` |
| 5 | Maximum concurrent open positions | **`8`** (allocator `max_pos_cap` binds before the engine's `10`) | `allocator.py` |

Selecting `100` symbols is **not** permission to trade `100`. The measured order
path proves it: `100` evaluated → `6` BUY candidates → `6` paper orders → `6`
open positions; the other `94` were blocked by
`risk_budget_position_sizing (86)`, `symbol_and_total_exposure (83)`,
`concentration_and_correlation (81)`, `calibrated_confidence_and_oos_evidence (3)`
and `signal_consensus (1)` (a symbol can be blocked by more than one check). The
`correlated-cluster` arm is the deliberate over-concentration proof: `100`
perfectly correlated names yield **`1`** position. No open-position, exposure or
order limit was raised for this work.

### 3.1.3 Bottleneck and what was actually changed

Sequential fetching dominated the cycle (`20.2 s` of scan time for `100` symbols at
a `100 ms` round trip — exactly `2 × 100 × RTT`). The fix is
`app/markets/fetch_gate.py`, a per-provider gate that the scan, the tick cycle and
the ensemble service all share:

* **worker bound** (`max_workers = 8`, clamped `1–32`) — no unbounded fan-out;
* **request budget** (`max_requests_per_second = 50.0`, burst `50`, clamped
  `0.5–200`) — pacing instead of a burst of `200` calls;
* **shared cooldown on a throttle** — one `429` backs the whole provider off
  (exponential, capped at `4×` the base cooldown and at `max_cooldown_seconds`),
  with one bounded retry, instead of every worker retrying into the same limit;
* **AIMD budget** — a throttle *window* halves the effective rate (floor
  `1 rps`), `25` clean successes add `10 %` of the configured ceiling back;
* **whole-batch deadline** (`45 s`) — a hung provider call becomes a per-symbol
  `FETCH_TIMEOUT` and cannot stall the cycle;
* **per-symbol isolation** — `{symbol: FetchOutcome}` in input order, every
  failure keeps its own code, and a throttle is re-raised by the callers
  (`_probe_symbol`) so the gate — not the symbol — owns the backoff.

Against a provider that only serves `20 rps` while the configured budget is `50`,
the same `100`-symbol cycle now completes with **`100/100` symbols analysed, `0`
lost, `0` stray orders** at `16.7 s` warm (`44` throttles, effective budget
converged to `21.5 rps`). Before the gate, that arm lost `89/100` symbols (`209`
throttles); a mid-tuning variant that cut the budget by `25 %` per window
oscillated above the entitlement and produced twice the throttles, so the halving
rule was kept.

### 3.1.4 Provider entitlement arithmetic (the real ceiling)

A single cycle needs `2N` provider requests. With `N = 100` that is `200`
requests, so the cycle cannot be faster than `200 / effective_rps` plus
per-request latency effects:

| Provider budget the plan actually allows | Floor for a 100-symbol cycle | Fits a 15 s interval? |
|---|---:|---|
| `200 rps` | `~1.0 s` | yes |
| `50 rps` (this default) | `~4 s` | yes, with headroom |
| `20 rps` | `~10 s` | yes, but no failure headroom |
| `5 rps` | `~40 s` | **no** |
| `3.3 rps` (Alpaca basic data, 200 req/min) | `~60 s` | **no** |

`MARKET_DATA_FETCH_MAX_RPS` is the operator knob: set it to the entitlement the
broker plan actually grants. The gate adapts downward on its own, but adaptation
costs a slow first cycle — the default `50 rps` is a *local* budget, not a claim
about any broker's published limit. For plans below `~15 rps`, either lower the
budget (and accept a longer interval) or scan fewer symbols per cycle; no amount
of concurrency creates provider capacity.

### 3.1.5 What was mocked, what is not claimed

* **Mocked:** quote/bar transports (deterministic synthetic OHLCV + quotes with
  injectable latency, `429` throttling and per-symbol failure modes), the session
  clock, and the broker (never constructed — `broker_order_api_calls = 0`,
  `LiveMarketPaperEngine.broker_order_endpoints_reachable is False`).
* **Real:** the scan/cycle code paths, the ensemble evaluator, the
  `PortfolioRiskAllocator`, the paper order planner and the SQLite paper journal.
* **Not claimed:** no perfect timing, no perfect fills, no profitability, no
  broker behaviour. Real per-request latency and the entitlement of a paid plan
  depend on the owner's credentials, region and broker; the numbers above are
  local engineering measurements on `2 vCPU` with a simulated `100 ms` round trip.
  Live execution stayed disabled and no order endpoint was contacted.

---

## 4. Owner Step-by-Step Guide (`LIVE-MARKET PAPER` Validation First)

1. **Verify Read-Only Market Connections (`02 · Connections` / `01 · Overview`)**:
   - Configure Upstox Read-Only Access Token (NSE) and Alpaca Market Data Key/Secret (NASDAQ), then click **Verify NSE Read-Only** and **Verify NASDAQ Read-Only** until both `NSE_PAPER` and `NASDAQ_PAPER` readiness checklists turn `READY`.
2. **Capture Real Historical Datasets & Run Real-Market OOS Ablation (`10 · AI & Model Lab` -> `12 · Ensemble & Allocation`)**:
   - In `10 · AI & Model Lab` Panel 4, capture real NSE (`RELIANCE`, `TCS`, `INFY`) and NASDAQ (`AAPL`, `MSFT`, `NVDA`) historical bars (`real_market_data = true`).
   - Open `12 · Ensemble & Allocation`, select your captured dataset in Panel 4, and click **Run Walk-Forward & Untouched OOS Ablation** with `Persist OOS Weights & Calibrator = YES`. Verify that `Accepted OOS > 0`, `ECE < 0.10`, and `2.0x` cost-stressed OOS net return is positive. Only enable `Kronos Advisory Input = ON` if `ensemble_with_kronos` improves untouched OOS net return and Sharpe over `ensemble_no_kronos`.
3. **Run in `LIVE-MARKET PAPER` First (`01 · Overview` / `05 · Strategies`)**:
   - Select strategy **`Parallel Strategy Ensemble + Portfolio Risk Allocator`** (`parallel_ensemble`) on NSE or NASDAQ Paper, start the paper session during regular market hours, and confirm simulated fills (`0` real broker order calls).
4. **Inspect Read-Only Execution & Signals (`11 · Live Trade Monitor`)**:
   - Click **`11 · Live Trade Monitor`** in the left sidebar or top quick-nav strip. Filter by Market (`NSE`/`NASDAQ`), Mode (`LIVE-MARKET PAPER` vs `LIVE`), or State (`Open Position`, `Working Order`, `Recent Fill`, `Active Signal`), select any symbol to inspect its candlesticks + 8 distinct marker shapes, and click any marker or event row to inspect full attribution (`SIMULATED` vs `BROKER_CONFIRMED`, fees, strategy, rationale, and SL/TP levels).
