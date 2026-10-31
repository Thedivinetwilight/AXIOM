# AXIOM Parallel Strategy Ensemble, Portfolio Risk Allocator & Live Trade Monitor — Evidence & Benchmarks (`v3.7.0a8`)

> **Strict Honesty & Verification Boundary**
> - All strategy evaluation, empirical calibration, walk-forward weighting, Kronos advisory blending, and ablation studies are **PAPER / RESEARCH ONLY** (`live_execution_authorized = false`). Nothing in `app/ensemble/` or `app/markets/trade_monitor.py` can arm, activate, or submit a real broker order.
> - The walk-forward and untouched out-of-sample (OOS) metrics below are reproducible verification runs on AXIOM's bundled deterministic OHLCV fixtures (`real_market_data = false`). **Synthetic or single-symbol fixture results do NOT establish real-market profitability or Kronos real-market alpha.**
> - Real Upstox (NSE) and Alpaca (NASDAQ) owner-account market-data verification, real-market Kronos incremental OOS value, and live broker order execution remain **NOT VERIFIED / INTENTIONALLY NOT EXECUTED** until run by the owner on their Windows machine with real read-only provider credentials.

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

### 2.5 Honest Interpretation & Kronos OOS Value Assessment
1. **Risk-Controlled Capital Preservation vs Unconstrained Baselines**: On both fixtures, unconstrained single-indicator `baseline_rsi` suffered `-15.28%` / `-12.92%` losses and `buy_and_hold` suffered `-2.88%` / `-16.52%` drawdowns (`23.35%` Max DD on NASDAQ). While unconstrained `baseline_moving_average` posted higher raw return on `3–4` full-notional trades, its tiny trade count (`3–4` trades) fails statistical promotion gates. By contrast, `ensemble_no_kronos` sized positions strictly from stop-distance risk budgets (`0.50%` equity risk per trade), stood aside (`NO_TRADE`) on `92%–96%` of bars when calibrated OOS confidence or consensus fell below threshold, achieved the **lowest maximum drawdown on both markets (`1.56%` NSE, `0.65%` NASDAQ)**, and remained positive under `2.0x` stressed transaction costs (`+2.61%` NSE, `+3.31%` NASDAQ).
2. **Does Kronos Add Measurable OOS Value?**: **NOT VERIFIED on real market data.** In offline/CI environments without HuggingFace weight downloads, `ensemble_with_kronos` refuses to fabricate forecasts and reports `SKIPPED_KRONOS_UNAVAILABLE`. On the owner's Windows machine—after capturing real NSE/Upstox and NASDAQ/Alpaca datasets in `10 · AI & Model Lab` and installing pinned CPU `NeoQuasar/Kronos-small`—run Panel 4 (`Run Walk-Forward & Untouched OOS Ablation`) in `12 · Ensemble & Allocation` to measure exact real-market incremental OOS return and Sharpe delta (`ensemble_with_kronos` minus `ensemble_no_kronos`) before enabling `Kronos Advisory Input = ON`.

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
