# AI & Model Lab Manual (`10 · AI & Model Lab`)

**Scope & Hard Safety Boundary:**
`AI & Model Lab` (`app/model_lab/*`, `/api/model-lab/*`, and Workspace `10` in `/terminal`) is strictly **PAPER / RESEARCH / ADVISORY ONLY**.
- It has **zero imports** of broker order clients (`AlpacaTradingClient`, `UpstoxLiveGttExecutor`, MT5 order sender).
- Installing Kronos, enabling forecasting, running walk-forward training, promoting a model, or importing a SHA-256 model bundle **never** creates, arms, activates, or modifies any `LIVE-MARKET PAPER` or `LIVE-MARKET LIVE` trading session.
- Historical backtests, walk-forward Sharpe ratios, and Kronos probability bands **never guarantee future profitability**.

---

## 1. Kronos Foundation Model Integration (`KronosForecastProvider`)

### Pinned Artifacts & License
- **Upstream Source:** `https://github.com/shiyu-coder/Kronos` @ `67b630e67f6a18c9e9be918d9b4337c960db1e9a` (**MIT License**, `app/autonomy/vendor/kronos/LICENSE`).
- **Model:** `NeoQuasar/Kronos-small` @ `901c26c1332695a2a8f243eb2f37243a37bea320` (`context: 512`, `98,980,656` B `model.safetensors`, SHA-256 `b082dfcb...`).
- **Tokenizer:** `NeoQuasar/Kronos-Tokenizer-base` @ `0e0117387f39004a9016484a186a908917e22426` (`15,842,368` B `model.safetensors`, SHA-256 `59d85f6a...`).
- **Locked CPU Runtime:** `config/kronos-runtime-requirements.txt` (`torch==2.7.1+cpu`, `numpy==2.2.6`, `pandas==2.2.2`, `einops==0.8.1`, `safetensors==0.6.2`, `huggingface_hub==0.33.1`) installed into `%LOCALAPPDATA%\AXIOM\autonomy\kronos\runtime` with `--no-deps --only-binary=:all:`.

### Six Explicit Provider States
| State | Meaning | Operator Next Action |
|---|---|---|
| `NOT_INSTALLED` | Model/tokenizer safetensors or locked CPU runtime not yet installed in per-user directory. | Click **Install Pinned Kronos (CPU)** (~110 MiB weights + CPU wheels). |
| `NOT_VERIFIED` | Files exist on disk, or offline genuine CPU self-test has not yet validated the current Python + runtime + manifest digest. | Click **Offline CPU Self-Test** (or **Verify SHA-256**). |
| `READY` | SHA-256 pins and offline CPU self-test verified (`mock_outputs_used=false`). | Click **Enable Forecasting** and **Run Validated Forecast**. |
| `RUNNING` | Active install, self-test, or CPU bar inference currently executing. | Wait for completion or click **Cancel Op**. |
| `ERROR` | Corrupted weight digest, subprocess timeout, or non-finite output — **fails closed** (zero mock fallback). | Inspect message; click **Repair** or **Clear Error**. |
| `STALE` | Latest cached forecast exceeded `max_forecast_age_seconds` or newer completed bar arrived. | Request a fresh bar forecast. |

### Forecasting Controls & Resource Diagnostics
- **Enable / Disable Forecasting** (`POST /api/model-lab/kronos/toggle`): Enables on-demand Model Lab & paper-strategy (`kronos_ensemble`) forecasting only.
- **Load Model / Unload Worker** (`POST /api/model-lab/kronos/load`, `/unload`): Verifies readiness or terminates the isolated `kronos_worker.py` subprocess immediately to free CPU RAM (~600–900 MiB RSS). Restarting AXIOM always starts unloaded (`model_loaded_preference=false`).
- **Cadence & Bounds** (`POST /api/model-lab/kronos/config`): Configures `cadence_seconds` (`15..86400`), `timeout_seconds` (`1..300`), `horizon` (`1..60` bars), and Monte Carlo `samples` (`4..32`).
- **Diagnostics**: Tracks rolling `p50` / `p95` `load_ms` and `inference_ms`, inference count, process RSS MiB, and `mock_outputs_used=false`.

---

## 2. Dataset Manager, Leak-Free Research Pipeline & Model Registry

1. **Historical Dataset Manager (`Panel 5`)**:
   - Lists bundled deterministic fixtures (`BUNDLED_SYNTHETIC_FIXTURE`, `real_market_data=false`) and imported/captured NSE (`Asia/Kolkata`) & NASDAQ (`America/New_York`) datasets.
   - **Capture Read-Only Provider History** (`POST /api/model-lab/datasets/capture-live`): Pulls `>= 60` real completed bars from Upstox v3 or Alpaca Market Data v2 and computes canonical bar SHA-256, missing-bar estimate, duplicate check, and survivorship warning.
   - **Import Local OHLCV CSV** (`POST /api/model-lab/datasets/import`): Validates chronological ordering, finite positive OHLC, and non-negative volume; rejects NaN/corrupted rows fail-closed.
2. **Chronological Split & Zero-Leakage Guarantees (`Panel 6 & 7`)**:
   - Enforces `Train [0 : train_end)` → `Embargo 1` → `Validation [val_start : val_end)` → `Embargo 2` → `Untouched Test [test_start : N)` plus 3 expanding walk-forward folds.
   - Features at bar `t` read `bars[:t+1]` only; supervised Ridge training targets on `train` satisfy `t + 2 < train_end` (never crossing into embargo/validation); simulated fills execute at `bars[t+1].open` with spread, slippage, partial-fill participation cap, and market-specific fees (`NSE` STT/NSE/SEBI/Stamp/GST vs `NASDAQ` SEC/FINRA).
   - Evaluates `buy_and_hold`, `no_trade`, `moving_average_tuned`, `rsi_tuned`, `axiom_linear_lagged` (causal 9-feature Ridge model), and optional `kronos_ensemble`.
   - **Promotion Rejection Gates**: Automatically marks any candidate `GATE_REJECTED` if validation Sharpe `<= 0`, trade count `< min_trades` (default `5`), or `2.0x` cost-stressed net return `<= 0%`.
3. **Model Registry & SHA-256 Bundles (`Panel 8`)**:
   - **Promote (Paper)** marks an eligible candidate `PROMOTED_PAPER_CANDIDATE` for operator paper reference only.
   - **Export JSON** / **Verify SHA-256 & Import Model Bundle** serializes canonical `axiom.model_lab.bundle.v1` artifacts and rejects any tampered payload whose recomputed SHA-256 does not match `bundle_sha256`.
