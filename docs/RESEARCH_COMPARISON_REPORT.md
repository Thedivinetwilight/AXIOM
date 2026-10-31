# Reproducible Research Datasets & Out-of-Sample Strategy Comparison Report

> **Mandatory Disclosure:** The bundled datasets (`data/sample_1h.csv`, `data/sample_alt_1h.csv`, `data/sample.csv`, `data/sample_alt.csv`) are **synthetic deterministic OHLCV fixtures** (`real_market_data=false`, `survivorship_aware=false`, `live_eligible=false`) shipped solely to verify leak-free split invariants, fee math, cost-stress gates, and SHA-256 reproducibility without requiring external credentials in CI. **They are NOT real NSE or NASDAQ market history and do NOT imply real-world edge or profitability.** Section 3 specifies how to capture or import real multi-regime NSE & NASDAQ historical datasets on the owner's machine.

---

## 1. Reproducible Bundled Verification Datasets

| Dataset ID | File | Market Fee Model | Bars | UTC Range | Canonical Bar SHA-256 | Data Class |
|---|---|---|---|---|---|---|
| `bundled_nse_synthetic_1h` | `data/sample_1h.csv` | NSE (`INR`, Delivery STT + NSE + SEBI + Stamp + 18% GST) | `1,600` | `2021-01-01T00:00Z` → `2021-03-08T15:00Z` | `1fc4cc696eaa9e86b6169d15e6a7832dd158e50bdbf5afa8d9c6d567137762fa` | `BUNDLED_SYNTHETIC_FIXTURE` |
| `bundled_nasdaq_synthetic_1h` | `data/sample_alt_1h.csv` | NASDAQ (`USD`, SEC Section 31 + FINRA TAF) | `1,600` | `2021-01-01T00:00Z` → `2021-03-08T15:00Z` | `75e820d6528995c616315f3ef523d269da1f42521aa44edc778c16eaf30d93c6` | `BUNDLED_SYNTHETIC_FIXTURE` |

### Exact Chronological Split & Cost Protocol
- **Train:** Bars `[0 : 960)` (`960` bars, `2021-01-01T00:00Z` → `2021-02-09T23:00Z`)
- **Embargo 1:** `5` bars (`[960 : 965)`)
- **Validation:** Bars `[965 : 1285)` (`320` bars, `2021-02-10T05:00Z` → `2021-02-23T12:00Z`) — **all hyperparameter & Ridge $(\alpha, \tau)$ selection stops here**
- **Embargo 2:** `5` bars (`[1285 : 1290)`)
- **Untouched Test:** Bars `[1290 : 1600)` (`310` bars, `2021-02-23T18:00Z` → `2021-03-08T15:00Z`, `selection_touched_test = false`)
- **Execution Friction:** Next-bar open fill (`bars[t+1].open`), `4.0 bps` spread, `5.0 bps` slippage (`1.0x` base vs **`2.0x` stressed**: `8.0 bps` spread + `10.0 bps` slippage), `5%` max bar-volume participation cap, `min_trades = 5` gate.

---

## 2. Out-of-Sample & 2.0× Cost-Stress Results (Untouched Test Window)

### Run A — NSE Fee Schedule (`bundled_nse_synthetic_1h`, Manifest SHA-256 `b40e0e8104f56058b77059233876e88b3d02213a4af9c4e3203bdbd8392fd87a`)

| Candidate | Family | Val Sharpe | Untouched Test Net % | Test Sharpe | Max DD % | Test Trades | 2.0× Cost-Stress Net % | Gate Verdict & Reason |
|---|---|---:|---:|---:|---:|---:|---:|---|
| `buy_and_hold` | `BASELINE` | `2.96` | `-2.14%` | `-3.11` | `3.96%` | `1` | `-2.16%` | Benchmark baseline |
| `no_trade` | `BASELINE` | `0.00` | `0.00%` | `0.00` | `0.00%` | `0` | `0.00%` | Zero-exposure cash baseline |
| `moving_average_tuned` | `RULE_STRATEGY` | `9.29` | `+4.18%` | `8.32` | `0.91%` | `4` | `+4.16%` | **REJECTED** (`trade_count 4 < min_trades 5`) |
| `rsi_tuned` | `RULE_STRATEGY` | `-5.45` | `-9.78%` | `-10.15` | `11.02%` | `4` | `-9.81%` | **REJECTED** (Val Sharpe <= 0, trades < 5, stress <= 0%) |
| `axiom_linear_lagged` | `AXIOM_DECISION_MODEL` | `8.49` | `+4.65%` | `8.81` | `1.00%` | `12` | `+4.53%` | **ELIGIBLE** (Paper-reference candidate only) |
| `kronos_ensemble` | `KRONOS_ASSISTED` | `—` | `—` | `—` | `—` | `—` | `—` | `SKIPPED` unless genuine Kronos CPU runtime is `READY` |

### Run B — NASDAQ Fee Schedule (`bundled_nasdaq_synthetic_1h`, Manifest SHA-256 `64f365aa95b58e72747f376b10543e5d76ef1f7a9a9e933c8f2a0c0ceca99b8a`)

| Candidate | Family | Val Sharpe | Untouched Test Net % | Test Sharpe | Max DD % | Test Trades | 2.0× Cost-Stress Net % | Gate Verdict & Reason |
|---|---|---:|---:|---:|---:|---:|---:|---|
| `buy_and_hold` | `BASELINE` | `5.01` | `-8.60%` | `-5.75` | `10.78%` | `1` | `-8.63%` | Benchmark baseline |
| `no_trade` | `BASELINE` | `0.00` | `0.00%` | `0.00` | `0.00%` | `0` | `0.00%` | Zero-exposure cash baseline |
| `moving_average_tuned` | `RULE_STRATEGY` | `11.37` | `+6.71%` | `5.14` | `1.72%` | `4` | `+6.56%` | **REJECTED** (`trade_count 4 < min_trades 5`) |
| `rsi_tuned` | `RULE_STRATEGY` | `-5.19` | `-10.99%` | `-8.54` | `12.70%` | `2` | `-11.04%` | **REJECTED** (Val Sharpe <= 0, trades < 5, stress <= 0%) |
| `axiom_linear_lagged` | `AXIOM_DECISION_MODEL` | `11.43` | `+2.61%` | `2.40` | `2.29%` | `16` | `+2.04%` | **ELIGIBLE** (Paper-reference candidate only) |
| `kronos_ensemble` | `KRONOS_ASSISTED` | `—` | `—` | `—` | `—` | `—` | `—` | `SKIPPED` unless genuine Kronos CPU runtime is `READY` |

---

## 3. Importing Real Survivorship-Aware NSE & NASDAQ Datasets on Your PC

1. **Read-Only Provider Capture (Single-Symbol Point-in-Time Inspection)**:
   - Save your Upstox read-only token or Alpaca Market Data (`IEX`/`SIP`) keys in **`07 Connections`**.
   - Open **`10 AI & Model Lab` → `5. Historical Dataset Manager`**, choose Market (`NSE` or `NASDAQ`), Symbol (e.g., `RELIANCE`, `NIFTYBEES`, `AAPL`, `QQQ`), and Timeframe (`5m`, `15m`, `1h`, `1D`), and click **Capture & Fingerprint Live History**.
2. **External Survivorship-Aware CSV Import (Recommended for Multi-Year Evaluation)**:
   - Prepare a UTC or ISO-8601 timezone-qualified CSV (`timestamp,open,high,low,close,volume`) adjusted for splits/dividends and including delisted constituents where evaluating universe strategies.
   - Paste or POST via `/api/model-lab/datasets/import` with `corporate_action_adjustment="SPLIT_AND_DIVIDEND_ADJUSTED"` and `survivorship_aware=true`. AXIOM verifies monotonic timestamps, rejects NaN/corrupted rows, and pins the dataset SHA-256 in every run manifest.
