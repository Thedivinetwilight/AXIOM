# KRONOS INTEGRATION — AXIOM forecasting subsystem

Status: implemented, tested, **off by default** (`AXIOM_KRONOS_ENABLED=false`).

Kronos (<https://github.com/shiyu-coder/Kronos>) is a financial time-series
foundation model. In AXIOM it is a **forecasting subsystem only**: it predicts
future K-line/OHLCV trajectories. It is not the trading engine, it never places
orders, never overrides risk and never enables LIVE trading.

---

## 1. What this repository actually contains

Before implementing, the repository was inspected end to end. The verified state
(`git log -1` → `0314db2 Add Arena 3.0.5 source`) is:

| Path | What it is |
| --- | --- |
| `app/` | **Arena 3.0.5** — FastAPI backend: engine, risk, strategies, learning, execution, paper trading, research, data. This is the authoritative trading system. |
| `desktop/` | The desktop terminal (PySide6): a presentation-only client of the Arena REST API plus the persisted emergency STOP/RESUME flags. |
| `tests/` | 34 Arena/desktop test modules (baseline: **545 passed, 1 skipped**). |
| `tools/` | Release packaging + security scan. |

Notes that shaped the design:

* There was **no** `SnapshotStore`, no `Observed[T]`, no `MARKETS` panel and no
  Kronos code anywhere in the tree. The desktop page closest to "MARKETS" is
  `Live Market`. The Kronos layer therefore introduces its own store
  (`ForecastStore`) and its own domain models rather than extending something
  that does not exist.
* Arena talks to its UI over **HTTP** (`desktop/api_client.py`), not an in-process
  worker. Kronos follows the same isolation principle with its own IPC process.
* Arena has read-only data plumbing in `app/data/` (`MarketDataProvider`,
  `Candle`, `validator`), but no authoritative candle endpoint for an external
  forecaster: `app/api/routes/market.py` exposes only `/market/status` and
  `/market/datasets`. AXIOM therefore owns its own market data contract instead
  of reaching into Arena internals.

**Arena was not modified.** `git diff --stat 0314db2 -- app/` is empty; every
change lives in `axiom/`, `desktop/`, `tests/`, `tools/`, docs and config files.

---

## 2. Architecture

```
REAL MARKET DATA ──► MarketDataProvider ──► validation ──► closed-candle gate
                                                                    │
                                                    cache check (unchanged window ⇒ reuse)
                                                                    │
                                              AXIOM ──IPC──► AXIOM-Kronos process
                                                              (PyTorch, Kronos,
                                                               tokenizer + model)
                                                                    │
                                                        forecast validation
                                                                    │
                                                              ForecastStore
                                          ┌─────────────────────────┼─────────────────────────┐
                                    MARKETS overlay            FORECAST panel          RESEARCH / evaluation

Arena 3.0.5 ──► Arena REST API ──► desktop/api_client ──► existing AXIOM UI   (unchanged)
```

The two paths never meet: Kronos output is not written into Arena decisions,
risk, accounts or strategy state, and Arena state is not read by Kronos.

### Runtime processes

```
AXIOM.exe
 ├── Arena API client (existing, unchanged)
 ├── GUI (PySide6)
 ├── ForecastStore
 └── Kronos manager ──IPC (JSON lines)──► AXIOM-Kronos.exe
                                            ├── PyTorch
                                            ├── Kronos (AXIOM_KRONOS_SOURCE)
                                            └── model + tokenizer weights
```

The Kronos process is the only one that imports PyTorch. If it crashes, hangs or
runs out of memory, the terminal keeps running and reports `OFFLINE`.

---

## 3. Module map

| Module | Responsibility |
| --- | --- |
| `axiom/models.py` | `KronosConfig`, `KronosStatus`, `KronosState`, `ForecastSnapshot`, `ForecastCandle`, `ForecastUncertainty`, `DataProvenance`, `MarketDataQuality` |
| `axiom/market/provider.py` | `MarketDataProvider` ABC, `MarketDataBar`, `MarketDataResult` |
| `axiom/market/validation.py` | OHLCV validation — reports, never repairs |
| `axiom/market/calendar.py` | `ContinuousCalendar`, `DailyCalendar`, `SessionCalendar`, future-timestamp projection |
| `axiom/market/gate.py` | `ClosedCandleGate` — CLOSED candles only, window-change detection |
| `axiom/market/replay.py` | `ReplayMarketDataProvider` — deterministic replay of recorded CSV |
| `axiom/market/http.py` | `HttpJsonMarketDataProvider` — real JSON REST provider |
| `axiom/kronos/backends.py` | `UpstreamKronosBackend` (real Kronos), `StubKronosBackend` (labelled dev stub) |
| `axiom/kronos/runtime.py` | `KronosRuntime` — lifecycle states, integrity gate, inference |
| `axiom/kronos/model_cache.py` | Runtime weight cache + SHA-256 manifest |
| `axiom/kronos/samples.py` | P10/P50/P90 + dispersion from sampled trajectories |
| `axiom/forecast/cache.py` | Cache identity (input hash, model revision, config hash) |
| `axiom/forecast/validation.py` | Forecast shape/finite/alignment validation, median-path candles |
| `axiom/forecast/store.py` | `ForecastStore` (thread-safe, separate from any Arena store) |
| `axiom/forecast/pipeline.py` | The whole data path, returning a state instead of raising |
| `axiom/worker/{protocol,child,manager}.py` | IPC contract, Kronos process, supervisor |
| `axiom/service.py` | `ForecastService` — the only entry point the GUI uses |
| `axiom/research/` | Baselines, metrics, walk-forward evaluation |
| `axiom/bootstrap.py` | Environment → configured stack (never raises into the UI) |
| `desktop/forecast_viewmodels.py`, `desktop/forecast_controller.py` | FORECAST panel + MARKETS overlay (Qt-free logic) |

---

## 4. Market data contract

Minimum required per bar: `timestamp, open, high, low, close, volume`.

Validated (and **never silently repaired**):

* finite values, no NaN/Inf
* no duplicate timestamps
* strictly increasing timestamps
* correct spacing for the **declared** timeframe (a whole-multiple spacing is
  reported as a likely mis-declared timeframe)
* timezone-aware timestamps (naive stamps are rejected, never assumed UTC)
* `high >= max(open, close)`, `low <= min(open, close)`, `high >= low`
* `volume >= 0`, prices `> 0`
* sufficient history (`AXIOM_KRONOS_MIN_HISTORY`)
* calendar consistency (out-of-session stamps produce a warning)

Gaps are reported as errors by default. A gap explained by the calendar
(weekend/holiday on a `DailyCalendar`) is not an error.

### Timestamp rules

* Only CLOSED candles reach the model: a bar is closed when
  `open_time + timeframe <= now`.
* Future timestamps come from the **calendar**, not the model. Closed slots are
  skipped; nothing is padded or fabricated.
* Re-run happens only when the closed-candle window changes. The cache key is
  `instrument · timeframe · last closed timestamp · input window SHA-256 ·
  model revision · config hash`, so a late revision of an existing candle
  invalidates the cache even when the last closed timestamp is unchanged.

### Providers

1. `ReplayMarketDataProvider` — deterministic replay of a recorded CSV
   (`data/sample_1h.csv`, `data/sample.csv`). The timeframe is declared, never
   inferred. Used for offline development, tests and walk-forward research.
2. `HttpJsonMarketDataProvider` — a real JSON candle endpoint with a configurable
   URL template, field mapping, auth header and injectable transport. Malformed
   responses raise; they are never coerced.

---

## 5. Kronos runtime

### Model and settings

| Setting | Default | Note |
| --- | --- | --- |
| model | `NeoQuasar/Kronos-small` | 24.7M params, **512 context** (Kronos README model zoo) |
| tokenizer | `NeoQuasar/Kronos-Tokenizer-base` | `d_in = 6` (OHLCV + amount) |
| lookback | 400 | must be `<= max_context` |
| pred_len | 120 | |
| max_context | 512 | validated against the published context length of the model |
| temperature / top_k / top_p | 1.0 / 0 / 0.9 | upstream defaults |
| sample_paths | 1 | `>= 3` before dispersion is displayed |
| device | `auto` | resolved to `cuda:0` / `mps` / `cpu` from what torch reports |
| update_on_closed_bar | true | |

These values are **not** assumed for other models: `max_context` is checked
against `KNOWN_MODEL_CONTEXT` and an unknown model id is flagged rather than
treated as 512. Tokenizer/model pairing is verified before inference
(`s1_bits`, `s2_bits`, `d_in`); a mismatch raises `KronosModelMismatch`.

### Sampling and uncertainty

Upstream `KronosPredictor.predict(sample_count=N)` **averages** the N sampled
paths internally (`np.mean(preds, axis=1)` in `auto_regressive_inference`), so a
single call can never expose dispersion. AXIOM therefore repeats single-path
inference `sample_paths` times and summarises the trajectories itself.

Reported only when `sample_paths >= 3`: `P10 / P50 / P90` of the horizon close,
per-step P10–P90 band, dispersion (mean per-step std), interval width.
`ForecastUncertainty.calibrated` is permanently `False` and the UI labels it
*"sample dispersion (not calibrated probability)"*.

### States

`DISABLED · INITIALIZING · DOWNLOADING · LOADING · READY · INFERENCING · STALE ·
OFFLINE · ERROR · INSUFFICIENT_HISTORY · DATA_ERROR`

`READY` is only set after tokenizer **and** model loaded and passed the pairing
check. Any failure — missing Kronos source, missing torch, digest mismatch,
unexpected exception — lands in `ERROR` with the reason.

### Weights

Never in Git. Runtime cache:

```
Windows : %LOCALAPPDATA%\AXIOM\models\kronos
macOS   : ~/Library/Caches/AXIOM/models/kronos
other   : $XDG_CACHE_HOME/AXIOM/models/kronos
override: AXIOM_MODEL_CACHE
```

`manifest.json` records model id, revision, file name, size, SHA-256 and the
download timestamp. The digest is verified **before** any backend loads the
weights; a tampered or truncated file yields `ERROR`, not a loaded model.

---

## 6. UI

### FORECAST page

Banner `MODEL FORECAST — not observed market data`, a series selector, an
overlay chart (solid = actual, dashed magenta = forecast), the panel table and a
labelled forecast-candle table. Panel fields: status, detail, model, model
revision, tokenizer, tokenizer revision, backend, device, Kronos source, model
cache, worker pid, instrument, timeframe, lookback, prediction horizon, sample
paths, sampling knobs, last closed candle (ACTUAL), last actual close (ACTUAL),
forecast endpoint (MODEL), forecast close at horizon (MODEL), forecast horizon
return (MODEL), P10/P50/P90 + dispersion when supported, inference latency,
updated timestamp, input data source, input provider, input window, input hash,
cache key, data quality, note, last error.

Predicted values are always named `Forecast …`; there is no field labelled
`Close` or `Volume`.

### MARKETS overlay

The `Live Market` page gained a chart whose legend states the distinction:
`solid = ACTUAL market data · dashed = MODEL FORECAST (Kronos) — not
observations`. The actual series comes from the observed closes stored with the
snapshot, so drawing never triggers I/O on the GUI thread.

### GUI responsiveness

The GUI timer calls only `ForecastService.request_refresh()` (queue a job) and
`ForecastService.poll()` (drain results). Both are non-blocking and measured in
tests; Arena polling continues uninterrupted while Kronos infers.

---

## 7. Research

`axiom/research/walkforward.py` runs origin-anchored walk-forward evaluation.
Each fold asserts `window[-1].timestamp < future_timestamps[0]` and records the
result, so leakage shows up as a failed check rather than a flattering score.

Baselines: `last_close`, `drift`, `moving_average` (plus `seasonal_naive`).

Metrics: MAE, RMSE, MAPE, directional accuracy, horizon return error, range
error; for sampled forecasts also empirical P10–P90 coverage, interval width and
dispersion. Forecasting quality is kept separate from trading profitability: no
fees, fills or positions anywhere in the research code.

---

## 8. Configuration

See `AXIOM_KRONOS.env.example`. Kronos is off unless `AXIOM_KRONOS_ENABLED=true`.

```
AXIOM_KRONOS_ENABLED=true
AXIOM_KRONOS_BACKEND=upstream      # or "stub" for offline development
AXIOM_KRONOS_MODE=process          # AXIOM-Kronos process (preferred)
AXIOM_MARKET_DATA_SOURCE=replay    # or http
AXIOM_MARKET_DATA_CSV=data/sample_1h.csv
AXIOM_MARKET_DATA_TIMEFRAME=1h     # declared, never inferred
AXIOM_MARKET_CALENDAR=continuous   # continuous | daily | session
AXIOM_FORECAST_SERIES=DEMO:1h
AXIOM_KRONOS_SOURCE=%LOCALAPPDATA%\AXIOM\Kronos
```

A misconfigured stack never raises into the GUI: the panel shows the reason and
the terminal keeps working.

---

## 9. Installation and packaging

Base runtime (unchanged): `pip install -r requirements.txt`.

Kronos runtime (separate environment, Windows example):

```bat
desktop\run_kronos_windows.bat
```

which creates `.venv-kronos`, installs `requirements-kronos.txt`, clones Kronos
to `%LOCALAPPDATA%\AXIOM\Kronos` and sets `AXIOM_KRONOS_SOURCE`.

Executables (Windows, inside `.venv-kronos`):

```bat
pip install pyinstaller
python tools\build_windows_executables.py
```

→ `dist\AXIOM.exe` (GUI, no PyTorch) and `dist\AXIOM-Kronos.exe`
(`python -m axiom.worker.child`). Weights are not bundled.

---

## 10. Hard boundaries

* `axiom/` never imports `app/` — enforced by `tests/test_kronos_safety.py`.
* The IPC surface is exactly `{ping, load, status, forecast, shutdown}`; there is
  no order, risk, account or LIVE concept to send.
* `ForecastCandle.origin` is always `KRONOS_FORECAST`; every price field is
  prefixed `forecast_`. A `MarketDataBar` and a `ForecastCandle` are distinct
  types and cannot be substituted for one another.
* Kronos never writes `DecisionSnapshot`, `RiskSnapshot`, `AccountSnapshot`,
  strategy state or risk state — it has no reference to any of them.
* No market data is fabricated: the market layer imports no RNG, the replay
  provider only serves recorded bars, and future timestamps come from the
  calendar.
* Importing `axiom.*` pulls in no torch, pandas, numpy, Qt, einops, safetensors
  or huggingface_hub (asserted in a subprocess test).

---

## 11. Tests

```
python -m pytest -q
```

| File | Covers |
| --- | --- |
| `tests/test_kronos_market_data.py` | valid/invalid OHLCV, NaN/Inf, duplicates, ordering, gaps, wrong timeframe, insufficient history, timezones, calendars, closed-candle gate, replay + HTTP providers |
| `tests/test_kronos_runtime.py` | config rules, lifecycle states, tokenizer/model mismatch, context limits, model cache + SHA-256 + revision tracking, forecast shape/validation, sampling, real Kronos CPU inference, GPU where supported |
| `tests/test_kronos_pipeline_store.py` | end-to-end pipeline, cache correctness, failure paths, ForecastStore, staleness |
| `tests/test_kronos_worker.py` | IPC protocol, child process, manager, clean shutdown, crash isolation, GUI responsiveness, Arena polling during inference, packaged runtime, bootstrap |
| `tests/test_kronos_research.py` | baselines, metrics, walk-forward, no-leakage canaries |
| `tests/test_kronos_ui.py` | FORECAST panel labels, overlay separation, controller degradation, desktop wiring |
| `tests/test_kronos_safety.py` | Arena isolation, no trading API, provenance, no fabricated data, lightweight runtime |
| `tests/test_kronos_packaging.py` | dependency separation, no weights in Git, executable targets, runtime startup, cache policy |

Real-model CPU inference runs when `AXIOM_KRONOS_SOURCE` points at a Kronos
checkout and PyTorch is installed; otherwise those tests skip.

---

## 12. Deliberately not done

* **No scanner batching yet.** `predict_batch` exists upstream, but batch size
  must be chosen from measured latency/memory/throughput, not guessed.
* **No live-trading path of any kind**, and no AI-to-order path.
* **No changes to Arena 3.0.5.**
