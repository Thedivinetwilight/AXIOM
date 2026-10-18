# KRONOS INTEGRATION — HISTORICAL TECHNICAL NOTES

> **Historical snapshot only:** this guide and its §0 test counts describe an earlier integration snapshot (`0314db2`), before later AXIOM app integration and forecast-contract/cache hardening. The implementation and claims below are not a current HEAD runbook or verification report. The old source maps, test counts, and statements such as “Arena 3.0.5 is untouched” are preserved as historical context, not promoted as current facts. For current code paths and evidence, see [`AXIOM_CURRENT_AUDIT_REPORT.md`](AXIOM_CURRENT_AUDIT_REPORT.md).

Historical status at that snapshot: implemented and tested at the integration level, **off by default**
(`AXIOM_KRONOS_ENABLED=false`). **Not production-ready**, and **never verified
against real pretrained Kronos weights** — see [§0 Verification status](#0-verification-status).

Kronos (<https://github.com/shiyu-coder/Kronos>) is a financial time-series
foundation model. In AXIOM it is a **forecasting subsystem only**: it predicts
future K-line/OHLCV trajectories. It is not the trading engine, it never places
orders, never overrides risk and never enables LIVE trading.

---

## 0. Verification status

This section is the authority on what has actually been demonstrated. Anything
not listed under *Verified* is **unverified** and must not be assumed to work.

### Verified in this environment

| Claim | Evidence |
| --- | --- |
| Full test suite passes from clean caches | `python -m pytest -q` → **884 passed, 13 skipped** |
| Same, with the real Kronos code path enabled | `AXIOM_KRONOS_SOURCE=<checkout>` → **895 passed, 2 skipped** |
| Arena 3.0.5 is untouched | `git diff --stat 0314db2 HEAD -- app/` is empty; Arena-only suite unchanged at **545 passed, 1 skipped** |
| Real upstream Kronos *code* runs end to end on CPU | `KronosPredictor` driven through `UpstreamKronosBackend` with the real architecture (small dims, random weights) |
| Upstream averages its samples | `inspect.getsource(auto_regressive_inference)` contains `np.mean(preds, axis=1)`; `predict()` returns exactly `pred_len` rows for `sample_count` 1 and 5 |
| `percentile()` matches numpy's default method | 500 randomised cases, max drift 3.2e-14 |
| A real child process is spawned over JSON-lines IPC | `KronosManager(mode="process")` runs `python -m axiom.worker.child` |
| Process isolation and crash recovery | killing the child yields `OFFLINE`, the supervisor survives, `restart()` recovers |
| No lookahead across the whole chain | canary provider that raises if any bar at/after "now" is served; its own liveness is tested |
| Base runtime stays lightweight | subprocess test: importing `axiom.*` pulls in no torch/pandas/numpy/Qt/einops/safetensors/huggingface_hub |

### Explicitly NOT verified

These were **never demonstrated** in this environment. Do not claim otherwise.

| Not verified | Why |
| --- | --- |
| **Real pretrained NeoQuasar weights** | The local model cache contains none of the three supported model/tokenizer bundles (0/3 verified). This environment cannot fetch Hugging Face artifacts, and no credentials or unverified downloads were used. |
| **Weight-backed inference** | Every inference test uses either the labelled stub backend or the real architecture with **random** weights. Forecast *quality* is entirely unassessed. |
| **GPU / CUDA inference** | No CUDA device here; the GPU test skips itself. |
| **A rendered PySide6 GUI** | No Qt widget has yet been instantiated in this sandbox (PySide6 is not installed). The FORECAST panel is verified through its Qt-free controller/view models plus source-contract assertions — that is **not** a rendered-GUI verification. |
| **`AXIOM.exe` / `AXIOM-Kronos.exe`** | The pair build and frozen-process tests are pending the dedicated genuine Windows workflow; packaging source tests alone are not executable verification. |

Consequences to keep in mind:

* Model integrity checking (SHA-256 before load) is tested against synthetic
  files, never against a real weight download.
* Latency figures in this document come from the stub backend or tiny random
  models and say nothing about real inference cost.
* The Windows child environment fix (inheriting the full environment) is
  reasoned and unit-tested on Linux, but has not been exercised on Windows.

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

`manifest.json` is treated as **untrusted input**. A recorded path is resolved
through `ModelCache.resolve_within_root()`, which requires the result to be
inside the cache root. Without that, a manifest naming an absolute path or a
`../` chain pointed the integrity check at an arbitrary file on disk — and
because the digest is compared against the very file it was pointed at, the
check passed and the planted file loaded as if legitimate.

Weights themselves are unverified in this sandbox: no download has ever
succeeded here, so integrity checking is exercised only against synthetic files.

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

Windows executable pair (build on Windows in two isolated environments; the
GUI environment must not contain PyTorch):

```bat
py -3.11 -m venv .venv-desktop
.venv-desktop\Scripts\python.exe -m pip install -r requirements.txt -r requirements-desktop.txt pyinstaller
py -3.11 -m venv .venv-kronos
.venv-kronos\Scripts\python.exe -m pip install -r requirements-kronos.txt pyinstaller
.venv-kronos\Scripts\python.exe tools\build_windows_executables.py --desktop-python .venv-desktop\Scripts\python.exe --kronos-python .venv-kronos\Scripts\python.exe
```

The builder refuses to use a GUI interpreter where `torch` is importable. The
pair is `dist\AXIOM.exe` (PySide6 GUI) plus `dist\AXIOM-Kronos.exe` (console
worker and JSON-lines IPC). The Windows pair workflow smoke-tests the GUI
offscreen and runs the worker protocol with the labelled **stub** backend; that
is a packaging/process check, not pretrained inference.

Weights are not bundled and the verified backend does not download them
automatically. Stage the approved complete model and tokenizer bundles under
`%LOCALAPPDATA%\AXIOM\models\kronos`, record their local SHA-256 manifests, and
verify them before inference. A passing stub smoke never establishes genuine
Kronos model quality.

At the time of this edit, the dedicated Windows workflow has not yet run on the
current changes. Until it passes, neither executable pair startup nor packaged
IPC is verified. Even after that workflow passes, genuine weight-backed
inference remains unverified unless the artifact verifier confirms real local
weights and a successful upstream inference.

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
python -m pytest -q                                  # 884 passed, 13 skipped
AXIOM_KRONOS_SOURCE=<checkout> python -m pytest -q   # 895 passed, 2 skipped
```

The Kronos subsystem adds 9 modules (~286 tests). The remaining 545 are the
pre-existing Arena/desktop suite, unchanged.

| File | Covers |
| --- | --- |
| `tests/test_kronos_market_data.py` | valid/invalid OHLCV, NaN/Inf, duplicates, ordering, gaps, wrong timeframe, insufficient history, JSON boundary strictness, **DST and timezone handling**, calendars, closed-candle gate, replay + HTTP providers |
| `tests/test_kronos_runtime.py` | config rules, lifecycle states, tokenizer/model mismatch, context limits, **module-discovery trust boundary**, model cache + SHA-256 + revision tracking + **path confinement**, forecast shape/validation, sampling, quantile semantics, real Kronos CPU inference, GPU where supported |
| `tests/test_kronos_pipeline_store.py` | pipeline, cache correctness incl. **model-revision separation** and **stale-hit labelling**, failure paths incl. snapshot-assembly failure, ForecastStore |
| `tests/test_kronos_worker.py` | IPC protocol + **boundary strictness**, child process, manager, clean shutdown, crash isolation, GUI responsiveness, Arena polling during inference, **service error propagation**, packaged runtime, bootstrap |
| `tests/test_kronos_end_to_end.py` | the whole chain in one flow with a **future-data canary**, entered both directly and through the UI controller |
| `tests/test_kronos_research.py` | baselines, metrics, walk-forward, no-leakage canaries |
| `tests/test_kronos_ui.py` | FORECAST panel labels, overlay separation, controller degradation, desktop wiring |
| `tests/test_kronos_safety.py` | Arena isolation, no trading API, provenance, no fabricated data, lightweight runtime |
| `tests/test_kronos_packaging.py` | dependency separation, no weights in Git, executable targets, runtime startup, cache policy |

Skips are honest, never silent: without `AXIOM_KRONOS_SOURCE` the 7 real-Kronos
tests skip; with it only the GPU test and one pre-existing network test skip.

Real-model CPU inference runs when `AXIOM_KRONOS_SOURCE` points at a Kronos
checkout and PyTorch is installed. **It uses the real architecture with small
random weights** — the download is stubbed, so no test has ever exercised
pretrained weights.

---

## 12. Hardening audit — defects found and fixed

An adversarial audit of the integrated subsystem found the defects below. Each
was reproduced before being fixed, and each has a regression test that was
confirmed to **fail against the pre-fix code** and pass afterwards.

### Correctness

| # | Defect | Consequence | Fix |
| --- | --- | --- | --- |
| 1 | **DST drift in calendar projection.** Slots were stepped by an absolute `timedelta`, so `09:30 EST + 86400s = 10:30 EDT`; the local-time slot check then matched nothing until the offset reverted. | Projecting 3 daily NYSE opens from Thu 2024-03-07 returned `2024-03-08`, then **`2024-11-04`** — a 241-day jump that looked plausible and passed validation, since `validate_forecast` only checks strict increase. | `TradingCalendar.advance()` is the single stepping primitive; `SessionCalendar` overrides it to step in local wall-clock time. `_check_gaps()` refuses to emit any projection with an unexplainable gap. |
| 2 | **Naive timestamps accepted by calendars.** `.timestamp()` and `.date()` silently reinterpret a naive value in the machine's local zone. | The same instant could get a different verdict on two machines. | `require_aware()` at every calendar entry point; `DailyCalendar` normalises to UTC before taking `.date()`. |
| 3 | **Cache ignored the resolved model revision** in process mode (no runtime handle, so it fell back to the usually-empty config value). | Upgrading the weights would keep serving forecasts produced by the previous model. | `KronosInference` carries `model_id`/`model_revision` across IPC; `ForecastPipeline` takes a non-blocking `model_identity` accessor wired to `cached_status()`. An unknown revision stays empty — never invented. |
| 4 | **A cache hit always reported `READY`.** | An aged forecast masqueraded as a fresh one. | A hit older than `stale_after_seconds` reports `STALE` while still serving the previous forecast. |
| 5 | **`percentile()` and the upstream-averaging claim were unverified.** | P10/P50/P90 semantics rested on prose. | Pinned against `np.percentile` (max drift 3.2e-14) and against `inspect.getsource` of `auto_regressive_inference`. |
| 6 | **Snapshot assembly could raise** out of `run()`, despite its never-raises contract. | A `ValueError` would reach the Qt thread. | The assembly + store step is wrapped and returns `ERROR` with the cause. |

### Robustness and responsiveness

| # | Defect | Consequence | Fix |
| --- | --- | --- | --- |
| 7 | **`ForecastService.status()` did a blocking IPC round-trip**, and the GUI calls it from its timer. | The GUI thread blocked on the model process. | `status()` reads a cache the worker thread maintains (`cached_status()`, `poll()` only); `refresh_status()` is the blocking variant. |
| 8 | **`_pump_stderr` shared `_io_lock`**, held for a whole inference. | A long inference blocked the stderr drainer, so the child's pipe could fill and wedge the child. | Three separate locks (io / stderr / status). |
| 9 | **`shutdown()` waited on `_io_lock`.** | Shutdown hung whenever a request was in flight — exactly when it is needed. | Shutdown proceeds straight to terminate/kill; added `restart()`. |
| 10 | **Timeouts clobbered `OFFLINE` with `ERROR`.** | A dead child was reported as wedged, hiding that a restart is the fix. | Classified by `process.poll()`: dead → `OFFLINE`, live-but-silent → `ERROR`. |
| 11 | **Four unguarded manager calls in `ForecastService`.** | `manager.load()` failing killed the worker before it drained a job (eternal spinner, no reason); `refresh_status()` failing also **discarded a completed forecast**; `status()` and `stop()` propagated into the Qt thread. | All four degrade and log instead of raising. |
| 12 | **No logging anywhere.** | Failures were invisible unless they happened to reach a status string. | `axiom/log.py` (no handlers on import) wired into lifecycle, rejections, worker start/exit and timeouts. |

### Security

| # | Defect | Consequence | Fix |
| --- | --- | --- | --- |
| 13 | **Module discovery scanned all of `sys.path`** for any `model`/`kronos` exposing the three names, inserting each candidate at the front of the path. | Any unrelated `model` package could stand in for the model implementation — arbitrary code execution in the process that loads the weights, reported to the UI as Kronos output. | Only `AXIOM_KRONOS_SOURCE` (validated, and the imported module must resolve *inside* it) or an installed `kronos` **distribution** (confirmed via `importlib.metadata`). |
| 14 | **`ModelCache.verify()` used `self.root / entry.relative_path`**, and `Path` discards the left operand when the right is absolute. | A tampered `manifest.json` pointed integrity verification at any readable file; the check passed, because the digest is compared against the file it was pointed at. Demonstrated returning `ok=True` for a file outside the cache. | `resolve_within_root()` confines verification to the cache root. |
| 15 | **Bare `float()` at the JSON boundary.** | `float(True) == 1.0` turned a boolean price into a real one; `json.loads` accepts the non-JSON literals `NaN`/`Infinity`. | `parse_number_field()` refuses booleans, non-finites and non-scalars, naming the field. |
| 16 | **`decode_bars` accepted naive timestamps** over IPC. | A naive/aware comparison fails deep in the pipeline, far from the cause. | `decode_timestamp()` requires an offset and names the field; `decode_number()` applies the same numeric rules to the wire. |
| 17 | **The child environment was stripped to an allowlist.** | No `SYSTEMROOT`/`PATH`/`TEMP` → a Python child cannot start on Windows, so `AXIOM-Kronos.exe` would not launch. | `child_environment()` inherits the full environment and applies explicit overrides. *Reasoned and unit-tested on Linux; not exercised on Windows.* |

---

## 13. Deliberately not done

* **No scanner batching yet.** `predict_batch` exists upstream, but batch size
  must be chosen from measured latency/memory/throughput, not guessed.
* **No live-trading path of any kind**, and no AI-to-order path.
* **No changes to Arena 3.0.5.**
* **No claim of production readiness**, and no claim that real pretrained
  Kronos weights were tested. They were not.
