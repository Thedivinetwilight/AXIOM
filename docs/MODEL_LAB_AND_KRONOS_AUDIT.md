# Phase 1 Audit: Kronos, Multi-Market Strategies, Research Pipeline & Live-Market Priority

**Baseline Build Identity Inspected Before Edits:**
- **Branch:** `arena/ffd46191-axiom` (PR #10 open against `main`)
- **Commit SHA:** `4494e02e3d7deecc8884bcb0057bb7d08d9a4224`
- **Source Version:** `3.7.0a6`
- **Baseline Test Suite:** `994 passed, 4 skipped` on Linux (`997 passed, 1 skipped` on Windows x64 CI Run `40879942413`); `4/4` JS harnesses green (`chart_smoke`, `terminal_smoke`, `multimarket_terminal_smoke`, `autonomy_smoke`).

---

## 1. Exact Audit of Existing Kronos Code, Artifacts & Packaging

1. **Upstream Source & License**:
   - Vendored inference code: `app/autonomy/vendor/kronos/model/{__init__.py,kronos.py,module.py}` pinned to `https://github.com/shiyu-coder/Kronos` commit `67b630e67f6a18c9e9be918d9b4337c960db1e9a`.
   - License: **MIT License** (`Copyright (c) 2025 ShiYu`, `app/autonomy/vendor/kronos/LICENSE`).
2. **Pinned Model & Tokenizer Manifest (`config/kronos-small.json`, schema `axiom.kronos.artifacts.v1`)**:
   - **Model**: `NeoQuasar/Kronos-small` @ revision `901c26c1332695a2a8f243eb2f37243a37bea320` (`context: 512`), `config.json` (`228` B, SHA-256 `5e0f6a60...`) + `model.safetensors` (`98,980,656` B / `~94.4 MiB`, SHA-256 `b082dfcb...`).
   - **Tokenizer**: `NeoQuasar/Kronos-Tokenizer-base` @ revision `0e0117387f39004a9016484a186a908917e22426`, `config.json` (`301` B, SHA-256 `2366e7cc...`) + `model.safetensors` (`15,842,368` B / `~15.1 MiB`, SHA-256 `59d85f6a...`).
   - **Total Pinned Weight Footprint**: `114,823,553` bytes (`~109.5 MiB`), installed atomically under `%LOCALAPPDATA%\AXIOM\autonomy\kronos` (never bundled in Git or PyInstaller binaries).
3. **Locked CPU Runtime (`config/kronos-runtime-requirements.txt`)**:
   - Pinned transitive closure for CPython 3.11/3.12 x64 (`torch==2.7.1+cpu`, `numpy==2.2.6`, `pandas==2.2.2`, `einops==0.8.1`, `safetensors==0.6.2`, `huggingface_hub==0.33.1`, etc.) installed with `--no-deps --only-binary=:all:` from `pypi.org` and `download.pytorch.org/whl/cpu` (~350–450 MiB disk, ~600–900 MiB peak RSS during CPU inference).
4. **Inference Entry Points & Windows Packaging**:
   - `KronosService` (`app/autonomy/kronos.py`) → `KronosRuntimeManager` (`app/autonomy/kronos_runtime.py`) → isolated subprocess `app/autonomy/kronos_worker.py` (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `device="cpu"`, `mock_outputs_used=False`).
   - `packaging/windows/axiom.spec` bundles `vendor/kronos`, `kronos_*.py`, `config/kronos-small.json`, and `config/kronos-runtime-requirements.txt` while `payload_guard.py` blocks `.safetensors`/`.pt` weights from ever entering the installer.

---

## 2. Current Paper/Live Readiness & Strategy Audit

| Component | What Already Works & Is Verified | Mocked vs. Owner-Verified Status | Identified Gap |
|---|---|---|---|
| **NSE `LIVE-MARKET PAPER`** (`app/markets/live_paper.py`) | INR SQLite ledger, STT/NSE/SEBI/Stamp/GST fees, tick/lot/MIS checks, SL/TP, SHA-256 audit, 0 broker calls | Verified with deterministic fakes; live Upstox quotes require owner `UPSTOX_ACCESS_TOKEN` (`NOT VERIFIED` on owner account) | `POST /api/markets/paper/NSE/sync` did not automatically pull live completed bars for `RUNNING` automated strategies (`ISS-K01`), and `kronos_ensemble` was not wired into `LiveMarketPaperEngine`. |
| **NASDAQ `LIVE-MARKET PAPER`** (`app/markets/live_paper.py`) | USD SQLite ledger, `America/New_York` DST/holidays/early closes, SEC/FINRA fees, fractional/short checks, 0 broker calls | Verified with deterministic fakes; live Alpaca quotes require owner `ALPACA_MARKET_DATA` keys (`NOT VERIFIED` on owner account) | Same HTTP sync completed-bar wiring (`ISS-K01`) + unified 4-way readiness checklist (`ISS-K02`). |
| **NSE & NASDAQ `LIVE-MARKET LIVE`** | Default-OFF, distinct DPAPI credential slots, `AlpacaReadOnlyClient` preflight, SQLite `AlpacaBrokerJournal`, Arm → Phrase Activate, Emergency Stop | Gate/mock verified only; **0 real orders placed**; **`NOT VERIFIED`** on owner brokerage accounts | Needs unified side-by-side readiness checklist in `/terminal` (`01 Overview` & `06 Risk & live gates`) clearly explaining every external blocker (`ISS-K02`). |
| **Current Paper Strategies** | `LiveMarketPaperEngine`: `manual_only`, `moving_average` (SMA 5/20), `rsi` (RSI14 30/70), `breakout` (20-bar), `mean_reversion` (Bollinger 20,2). `/autonomy`: `TREND_MOMENTUM`, `MA_BREAKOUT`, `MEAN_REVERSION`, `KRONOS_INFORMED`. | **None** of the `/terminal` multi-market `LiveMarketPaperEngine` strategies called Kronos. | Add optional fail-closed `kronos_ensemble` to `LiveMarketPaperEngine` (`ISS-K03`). |

---

## 3. Three Distinct Meanings of "Training AXIOM" (Not Conflated)

| Approach | Definition in AXIOM | Data, License, Compute & Time Requirements | Implemented Status |
|---|---|---|---|
| **1. Strategy Parameter Tuning** | Chronological grid search over rule parameters (e.g., fast/slow MA windows, RSI thresholds, breakout lookback) on `train` + `validation` splits only, keeping `test` untouched. | Timestamped OHLCV CSV/JSON or cached provider candles; CPU < 2s per run; < 50 MiB RAM; no external license needed. | **IMPLEMENTED** in `app/model_lab/research_pipeline.py` (PAPER/RESEARCH ONLY). |
| **2. Fitting a Separate AXIOM Decision Model** | Fitting a lightweight, regularized linear/ridge directional probability model (`AxiomLaggedRidgeModel`, pure Python stdlib) on strictly causal lagged return/volatility/volume/regime features (+ optional frozen Kronos forecast features when `READY`) on `train`, calibrating probability threshold on `validation`, and evaluating once on `untouched_test`. | Timestamped OHLCV bars (`>= 180` bars); CPU < 2s per run; deterministic closed-form Gaussian elimination solve; SHA-256 fingerprint saved in Model Registry. | **IMPLEMENTED** in `app/model_lab/research_pipeline.py` & `app/model_lab/registry.py` (PAPER/RESEARCH ONLY). |
| **3. Fine-Tuning Kronos Foundation Weights** | Gradient updates to `NeoQuasar/Kronos-small` (`99 MB`) + `Kronos-Tokenizer-base` (`15.8 MB`). | MIT license permits modification, **but** valid financial foundation fine-tuning requires multi-year survivorship-free multi-asset intraday corpora, PyTorch CUDA GPU (`>= 16 GB VRAM`), AdamW/lr-schedule tuning, and huge overfitting risk on small single-user datasets. AXIOM's locked runtime intentionally ships CPU-only `torch==2.7.1+cpu`. | **INTENTIONALLY NOT PERFORMED** — Kronos is integrated strictly as a **frozen, SHA-256-verified inference-only foundation model** paired with separately tuned/trained AXIOM decision models. |

---

## 4. Issue Register (`ISS-K01` through `ISS-K06`)

| ID | Severity | Finding & Evidence | Affected Files | Resolution & Regression Test |
|---|---|---|---|---|
| `ISS-K01` | `P1` | **HTTP `POST /api/markets/paper/{market}/sync` marks quotes/SL/TP but omits fetching completed candles for `RUNNING` automated paper strategies, and `LiveMarketPaperEngine` needs per-bar `(market, symbol, strategy, timeframe, bar_ts)` deduplication.** (`app/api/routes/markets.py:699`, `app/markets/live_paper.py:1650`) | `app/markets/live_paper.py`, `app/api/routes/markets.py`, `app/api/server.py` | Wire read-only `nse_bars_provider` / `nasdaq_bars_provider`, deduplicate strategy decisions per completed bar timestamp, and support optional `kronos_ensemble` with fail-closed `HOLD` when Kronos is unavailable. **FIXED** (`tests/test_kronos_model_lab.py`). |
| `ISS-K02` | `P1` | **Missing unified 4-mode Readiness Checklist (`NSE Paper`, `NASDAQ Paper`, `NSE Live`, `NASDAQ Live`) with explicit owner-verification tracking.** CI mocks must never appear as owner-verified connections (`app/api/routes/markets.py`, `app/api/routes/connections.py`). | `app/api/routes/connections.py`, `app/api/routes/markets.py`, `app/static/terminal/{index.html,terminal.js}` | Track in-session owner read-only verification timestamps (`NOT_VERIFIED` until owner runs read-only quote/account test) and expose `GET /api/markets/readiness` + UI checklists on `01 Overview` and `06 Risk & live gates`. **FIXED** (`tests/test_kronos_model_lab.py`, `tests/js/multimarket_terminal_smoke.mjs`). |
| `ISS-K03` | `P1` | **`KronosService` lacks explicit 6-state Model Lab provider wrapper (`NOT_INSTALLED`, `NOT_VERIFIED`, `READY`, `RUNNING`, `ERROR`, `STALE`), Load/Unload & Enable/Disable toggles, configurable cadence/timeout, and rolling p50/p95 latency telemetry across NSE & NASDAQ bars.** (`app/autonomy/kronos.py`) | `app/model_lab/kronos_provider.py`, `app/api/routes/model_lab.py` | Implement `KronosForecastProvider` wrapping `KronosService` with strict bar/context/staleness/NaN validation, bounded timeout, Load/Unload & Enable/Disable controls (forecasting-only, never execution), p50/p95 load & inference latency, and fail-closed `UNAVAILABLE` behavior. **FIXED** (`tests/test_kronos_model_lab.py`). |
| `ISS-K04` | `P1` | **No unified multi-market (`NSE` `INR` + `NASDAQ` `USD`) historical research, walk-forward, baseline-comparison, cost-stress (`2x`), and Model Registry pipeline accessible from the desktop terminal.** (`app/autonomy/research.py` is NSE-CLI/autonomy specific). | `app/model_lab/{datasets.py,research_pipeline.py,registry.py}`, `app/api/routes/model_lab.py` | Build chronological train/embargo/validation/embargo/untouched-test + walk-forward pipeline comparing `buy_and_hold`, `no_trade`, rule strategies, `axiom_linear_lagged`, and `kronos_ensemble` with market-specific fees, `2x` spread/slippage stress gate, progress/cancellation, and SHA-256-verified Model Registry export/import. **FIXED** (`tests/test_research_pipeline_leakage.py`). |
| `ISS-K05` | `P1` | **No `AI & Model Lab` page or persistent 1-click navigation bar across all terminal workspaces.** (`app/static/terminal/index.html`) | `app/static/terminal/{index.html,terminal.css,terminal.js}` | Add persistent quick-jump bar + `10 AI & Model Lab` workspace with all 9 required panels wired to `/api/model-lab/*` and `/api/autonomy/kronos/*`, with non-claim warnings and full keyboard/button states. **FIXED** (`tests/js/multimarket_terminal_smoke.mjs`). |
| `ISS-K06` | `P2` | **Linux sandbox blocks outbound `huggingface.co` / `download.pytorch.org`, while Windows CI (`windows-release.yml`) runs real Kronos CPU install & inference.** Must report sandbox vs Windows CI vs owner PC honestly and include new Model Lab docs in `axiom.spec` / `axiom.iss`. | `packaging/windows/{axiom.spec,axiom.iss}`, `.github/workflows/windows-release.yml`, `docs/OWNER_SETUP_GUIDE.md` | Bundle `docs/MODEL_LAB_AND_KRONOS_AUDIT.md`, print non-secret `kronos-status.json` latency/hash evidence during Windows CI build, and clearly distinguish sandbox (`BLOCKED`), Windows CI (`VERIFIED` synthetic smoke), and owner PC (`NOT VERIFIED` until run). **FIXED**. |
