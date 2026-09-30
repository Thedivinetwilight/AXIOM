# Arena 3.0.5 — Complete Troubleshooting & Diagnostic Guide

**Scope:** Arena 3.0.5 trading backend (`app/`), the AXIOM desktop client (`desktop/`), and the
target chain **AXIOM → Arena 3.0.5 → Upstox Sandbox**.

**How this guide was written:** every path, function name, command, endpoint, exit code and error
string in this document was read out of the repository or produced by actually running the
command. Nothing is inferred from naming or from how such systems "usually" work. Where a claim
could **not** be executed in the environment used to write this guide, it is marked
**UNVERIFIED** with the reason.

**Companion documents:**

| Document | Use it for |
| --- | --- |
| `QUICK_DIAGNOSTIC_FLOWCHART.md` | "Something is broken, start here." Narrow to a subsystem fast. |
| `ERROR_DIAGNOSTIC_PLAYBOOK.md` | A known symptom, worked through the fixed 8-part template. |
| `SAFE_DIAGNOSTIC_COMMANDS.md` | Copy-paste commands that cannot place an order or leak a secret. |

---

## Table of contents

1. [Verification status of this document](#1-verification-status-of-this-document)
2. [Architecture map](#2-architecture-map)
3. [Where configuration and environment variables load](#3-where-configuration-and-environment-variables-load)
4. [Error diagnosis: the general procedure](#4-error-diagnosis-the-general-procedure)
5. [Dependency problems](#5-dependency-problems)
6. [Configuration diagnostics](#6-configuration-diagnostics)
7. [Upstox sandbox diagnostics](#7-upstox-sandbox-diagnostics)
8. [CLI diagnostics](#8-cli-diagnostics)
9. [API diagnostics](#9-api-diagnostics)
10. [Trading execution diagnostics](#10-trading-execution-diagnostics)
11. [Market-data diagnostics](#11-market-data-diagnostics)
12. [Database and logging diagnostics](#12-database-and-logging-diagnostics)
13. [AXIOM integration diagnostics](#13-axiom-integration-diagnostics)
14. [Windows / PowerShell / packaging diagnostics](#14-windows--powershell--packaging-diagnostics)
15. [Testing diagnostics](#15-testing-diagnostics)
16. [Never expose secrets](#16-never-expose-secrets)
17. [Emergency recovery](#17-emergency-recovery)
18. [Known limitations and unsupported capabilities](#18-known-limitations-and-unsupported-capabilities)

---

## 1. Verification status of this document

Run against commit `60113ce` on branch `arena/01a0ecfa-axiom`, Linux, Python 3.11.2.

### Verified by execution here

| Claim | Command that proved it | Result |
| --- | --- | --- |
| CLI has 22 subcommands | `python -m app --help` | exit 0, all 22 listed |
| Offline pipeline self-test passes | `python -m app paper-test` | exit 0, 17 checks `true`, `"ok": true` |
| `/health` is unauthenticated | `TestClient.get("/health")` | `200`, `"real_order_routing":"NOT_IMPLEMENTED"` |
| Protected routes 503 without a token | `TestClient.get("/api/v2/overview")`, no `API_TOKEN` | `503 Set API_TOKEN to enable protected API endpoints` |
| Protected routes 401 with a wrong token | same, `Authorization: Bearer nope` | `401` |
| Protected routes 200 with the right token | same, correct bearer | `200` |
| Safety guards reject live config | `MODE=LIVE`, `LIVE_TRADING=true`, `BROKER=LIVE`, `DATABASE_URL=postgres://x`, `NET_EDGE_THRESHOLD=9` | all `REJECTED`, exact messages quoted in §6 |
| `engine` refuses without sandbox config | `python -m app engine` with `BROKER=LOCAL_PAPER` | exit 2, refusal JSON |
| `sandbox-test` refuses without a token | `python -m app sandbox-test` | exit 3, `"performed": false` |
| `upstox-test` refuses without symbols | `python -m app upstox-test` | exit 2 |
| `monitor` reports DB health | `python -m app monitor` | exit 0, `"database_ok": true`, `"alerts": []` |
| `initialize` is offline-safe | `python -m app initialize` | exit 0, `UNIVERSE_SYNC_UNAVAILABLE` recorded, `"ok": true` |
| `run.py` exposes 7 research modes | `python run.py --help` | exit 0 |
| Full test suite | `python -m pytest` | **850 passed, 10 skipped** |

### NOT verified — do not treat as working

| Item | Why it is unverified |
| --- | --- |
| **Any real Upstox call** — auth, order placement, cancellation, reconciliation, the WebSocket feed | No Upstox credentials and no outbound access to `api.upstox.com` / `api-hft.upstox.com` from the authoring sandbox. Every HTTP/WS interaction in §7 and §11 is documented from **source**, not from a live response. |
| **Real 401/403/429 behaviour from Upstox** | Same reason. The classification logic is real code (`classify_history_probe`, `app/brokers/sandbox.py:134`); its *inputs* were never received from the live API here. |
| **`--place-order`** on `sandbox-test` | Never executed. It submits a real sandbox order by design. |
| **Windows 11 / PowerShell specifics** in §14 | Authored on Linux. Commands are given for both shells; only the POSIX forms were executed. |
| **The rendered PySide6 desktop GUI** | No display server / no `libGL.so.1` in the sandbox. `desktop/` was verified by import and by source inspection only. |
| **`AXIOM.exe` / `AXIOM-Kronos.exe`** | `tools/build_windows_executables.py` has never been executed. |
| **`uvicorn` as a real listening server** | §9 used FastAPI's in-process `TestClient`, not a bound socket. Firewall/proxy behaviour is therefore unverified. |

---

## 2. Architecture map

### 2.1 Process and code layout

```
AXIOM desktop client                Arena 3.0.5 backend                    Upstox
─────────────────────               ───────────────────                    ────────
desktop/app.py (PySide6)  ──HTTP──▶ app/api/server.py  create_app()
desktop/api_client.py               app/api/auth.py    authenticate()
  ArenaApiClient._request()         app/api/routes/*.py                     Market Data Feed V3
                                                                          (wss + protobuf)
                                    app/engine/sandbox_engine.py
                                      SandboxEngine.run() / process_candle()
                                      │
                                      ├─ app/risk/limits.py   HardRiskLimits.check()
                                      ├─ app/strategies/*     signal
                                      ├─ app/engine/state.py  SubmissionJournal, DailyRiskAccount
                                      └─ app/brokers/sandbox.py  UpstoxSandboxBroker
                                            POST /v3/order/place      ──────────────▶
                                            DELETE /v3/order/cancel   ──────────────▶
                                            PUT /v3/order/modify      ──────────────▶
                                            GET  /order/history       ──────────────▶
                                    app/database/repository.py ──▶ SQLite
```

### 2.2 Every subsystem, and where it lives

| Subsystem | Path | What it owns |
| --- | --- | --- |
| Package identity | `app/__init__.py` | `__version__ = "3.0.5"`, `__title__ = "arena"` |
| Operational CLI | `app/__main__.py` | 22 subcommands (`parser()` at line 1019, `main()` at line 1155) |
| Research CLI | `run.py` | modes `backtest, tournament, validate, walk-forward, robustness, paper, api` |
| ASGI factory | `app/main.py` | `create_app` re-export; docstring documents `uvicorn app.main:create_app --factory` |
| App construction | `app/api/server.py` | `create_app(settings, data_dir)` line 19; `lifespan` line 23 wires `app.state.repository/broker/simulator` |
| Auth | `app/api/auth.py` | `authenticate`, `loopback_or_bearer`, `LOCAL_CLIENTS` |
| HTTP routes | `app/api/routes/` | `health.py, dashboard.py, market.py, strategies.py, backtests.py, paper.py, v2.py, market_events.py` |
| Settings | `app/config/settings.py` | `Settings` (line 70), `from_env()` (line 236), `coerce_env_value()` (line 23), `ROOT` (line 9) |
| First-run setup | `app/bootstrap.py` | `initialize(settings)` |
| Sandbox broker | `app/brokers/sandbox.py` | `UpstoxSandboxBroker` (line 173), 910 lines |
| Paper broker | `app/paper_trading/broker.py` | `PaperBroker` — local simulation, no network |
| Trading engine | `app/engine/sandbox_engine.py` | `SandboxEngine` (line 51), 1017 lines |
| Engine state | `app/engine/state.py` | `SubmissionJournal` (53), `DailyRiskAccount` (146), `recover_ledger_state` (205) |
| Hard risk | `app/risk/limits.py` | `HardRiskLimits.check()`, `OrderIntent`, `RiskDecision` |
| Net-edge gate | `app/risk/net_edge.py` | 3.0.5 trade-quality gates |
| Strategies | `app/strategies/` | 7 registered strategies + `create_strategy()` / `catalog()` |
| Market data | `app/data/` | `upstox_stream.py`, `upstox_auth.py`, `factory.py`, `historical.py`, `cache.py`, `quality.py`, `validator.py` |
| Feed protobuf | `app/data/upstox_proto/` | generated `MarketDataFeed_pb2` + the `.proto` schema |
| Persistence | `app/database/` | `database.py`, `repository.py` (1175 lines), `models.py` (schema), `recovery.py` |
| Logging | `app/logging/logger.py` | `JsonFormatter`, `configure()` |
| Monitoring | `app/monitoring/` | `system.py` (`SystemMonitor`), `anomalies.py`, `reality_gap.py` |
| Reporting | `app/reporting/` | `weekly.py` (`WeeklyReport`), `activity.py` (`explain_all`) |
| Learning | `app/learning/` | `evaluator.py`, `journal.py` |
| Universe / history | `app/universe/`, `app/history/` | instrument master sync, historical candle sync |
| Desktop client | `desktop/` | `app.py` (489), `api_client.py` (102), `viewmodels.py`, `forecast_*` |
| Packaging | `tools/build_windows_executables.py`, `tools/package_release.py`, `tools/security_scan.py` | Windows executables, release archive, secret scan |

### 2.3 The three data/execution flows

**Flow A — Research (no network, no broker).**
`run.py --mode backtest|tournament|validate|walk-forward|robustness` →
`app/data/historical.py CSVProvider` → `app/backtesting/engine.py BacktestEngine` →
`app/database/repository.py` → `--output` JSON.

**Flow B — Paper (simulated execution).**
`python -m app paper-live` or `run.py --mode paper` → `app/data/factory.py stream_provider()` →
candles → strategy → `app/risk/limits.py` → `app/paper_trading/broker.py PaperBroker` → SQLite.
`PaperBroker` performs **no network I/O**.

**Flow C — Sandbox (real order API, real money never).**
`python -m app engine` → requires `BROKER=UPSTOX_SANDBOX` **and** `DATA_SOURCE=upstox` →
`app/data/upstox_stream.py UpstoxStream` → `SandboxEngine.process_candle()` →
`HardRiskLimits.check()` → `UpstoxSandboxBroker.submit()` → `POST {base}/v3/order/place`.

### 2.4 AXIOM integration points (exactly three)

1. **Transport** — `desktop/api_client.py`, class `ArenaApiClient` (line 15). One private method
   `_request(path, method)` (line 21) built on `urllib.request.urlopen`; every public method is a
   thin wrapper over a `/api/v2/...` path.
2. **Endpoint configuration** — `desktop/app.py` lines 37–38:
   `ARENA_BACKEND_URL` (default `http://127.0.0.1:8000`) and `ARENA_API_TOKEN` (default `""`).
3. **Control surface** — `ArenaApiClient.emergency_stop()` (line 98) → `POST /engine/stop`, and
   `resume()` (line 101) → `POST /engine/resume`. These are the **only** mutating calls AXIOM makes.

`desktop/app.py:123` declares the 14 pages:
`Command Center, Live Market, Forecast, Portfolio, Trades, Performance, Strategies, Research Lab,
Learning, Market Intelligence, Risk, Logs, Reports, Settings`.

---

## 3. Where configuration and environment variables load

**Single entry point:** `Settings.from_env()` in `app/config/settings.py`, line 236.

```python
load_dotenv(ROOT / ".env", override=False)     # line 240
```

`ROOT = Path(__file__).resolve().parents[2]` (line 9) — i.e. the **repository root**, so the file
read is `<repo>/.env`.

### 3.1 Precedence (highest wins)

1. **Process environment variable** — `override=False` means `load_dotenv` will *not* overwrite an
   already-set variable.
2. **`<repo>/.env`** — only for variables not already present in the environment.
3. **The dataclass default** in `Settings` (lines 70–143).

Two documented aliases exist (`aliases = {…}`, line 252):

| Field | Alias | Rule |
| --- | --- | --- |
| `market_data_access_token` | `UPSTOX_ACCESS_TOKEN` | explicit `MARKET_DATA_ACCESS_TOKEN` wins |
| `market_data_symbols` | `UPSTOX_INSTRUMENT_KEYS` | explicit `MARKET_DATA_SYMBOLS` wins |

### 3.2 Type coercion is annotation-driven

`coerce_env_value(name, annotation, raw)` (line 23) converts using the **declared field
annotation**, never the runtime type of the default. Consequences worth knowing when diagnosing:

- An `int` field given `"12.5"` raises rather than silently truncating.
- `STOP_LOSS=none` / `TAKE_PROFIT=none` are documented sentinels for "disabled" (line 60).
- A numeric field given a non-numeric string raises
  `"<NAME> must be a number (got '<value>')"`.

### 3.3 Fail-closed validation in `Settings.__post_init__` (line 144)

Verified rejections (exact messages, produced by running each):

| Setting | Error raised |
| --- | --- |
| `MODE=LIVE` | `Only MODE=PAPER and LIVE_TRADING=false are permitted` |
| `LIVE_TRADING=true` | `Only MODE=PAPER and LIVE_TRADING=false are permitted` |
| `DATABASE_URL=postgres://x` | `Only SQLite is supported` |
| `BROKER=LIVE` | `LIVE trading is not enabled in V1` |
| `BROKER=<anything else>` | `BROKER must be LOCAL_PAPER or UPSTOX_SANDBOX` |
| `BROKER=UPSTOX_SANDBOX` with no token | `BROKER=UPSTOX_SANDBOX requires UPSTOX_SANDBOX_ACCESS_TOKEN in the environment` |
| `SANDBOX_PRODUCT` not in `I/D/MTF` | `SANDBOX_PRODUCT must be I, D or MTF (documented V3 values)` |
| `NET_EDGE_THRESHOLD=9` | `NET_EDGE_THRESHOLD must be 0..0.1` |
| `MARKET_CALENDAR=nyse` | `Unknown market calendar` |
| `DATA_SOURCE=ibkr` | `Unknown data source` |
| `MARKET_DATA_MODE=ticks` | `MARKET_DATA_MODE must be ltpc or full` |

**Diagnostic significance:** a `ValueError` mentioning any of these strings is a *configuration*
failure, not a code failure. It is raised during `Settings.from_env()`, i.e. **before** any
network, database or broker object exists.

---

## 4. Error diagnosis: the general procedure

### 4.1 Step 1 — Get the real traceback, not the summary

Arena's API layer deliberately converts exceptions into short JSON. Two handlers live in
`app/api/server.py`:

```python
@app.exception_handler(ValueError)     # line 73  → 422 {"detail": "<message>"}
@app.exception_handler(Exception)      # line 77  → 500 {"detail": "Internal error; review local logs"}
```

So **a 500 body will never contain the traceback.** The traceback goes to the logger named
`bot.api` under the event `request_failed` (line 79). To see it:

```powershell
# PowerShell — run the server in the foreground and read stderr directly
python run.py --mode api --host 127.0.0.1 --port 8000
```

```bash
# POSIX
LOG_LEVEL=DEBUG python run.py --mode api --host 127.0.0.1 --port 8000 2>&1 | tee logs/api-trace.txt
```

Reproduce the failing request in a second shell, then read the traceback from the first.

### 4.2 Step 2 — Separate root cause from secondary cause

Arena has three layers that each convert failures, and the *outermost* message is usually not the
cause:

| Layer | File | Conversion it performs |
| --- | --- | --- |
| Broker HTTP | `app/brokers/sandbox.py:_request` (line 240) | `httpx.HTTPError` → `SandboxRequestError("Sandbox API unreachable (network error)")`; `401/403` → `SandboxAuthError(...)` **and** `self.available = False` |
| Engine | `app/engine/sandbox_engine.py:_act` (line 484) | catches broker exceptions, records a system event, continues |
| API | `app/api/server.py` (lines 73–82) | `ValueError` → 422, everything else → 500 |

**Rule:** when you see `SandboxRequestError`, the root cause is *below* it — either the network,
the URL, or the token. When you see a 500, the root cause is in the log, never in the response.

### 4.3 Step 3 — Which file to open first, by symptom class

| Symptom class | Open first | Look at |
| --- | --- | --- |
| Configuration | `app/config/settings.py` | `__post_init__` line 144 |
| Dependency / import | the traceback's last `import` line | `requirements.txt`, `requirements-kronos.txt`, `requirements-desktop.txt` |
| Network / broker | `app/brokers/sandbox.py` | `_request` line 240, `_headers` line 230 |
| Auth (API) | `app/api/auth.py` | `authenticate` line 33 |
| Order logic / risk | `app/engine/sandbox_engine.py` | `_act` line 484, `_submit` line 826 |
| Risk rejection | `app/risk/limits.py` | `HardRiskLimits.check` line 53 |
| Market data / feed | `app/data/upstox_stream.py` | `stream()` line 471, `_decode` line 420 |
| Feed decoding | `app/data/upstox_stream.py` | `UpstoxProtobufDecoder.decode` line 110 |
| Database | `app/database/database.py` | `Database.__init__` line 9 |
| Logging | `app/logging/logger.py` | `configure` line 22 |
| Desktop/UI | `desktop/api_client.py` | `_request` line 21 |
| CLI behaviour | `app/__main__.py` | the `cmd_*` function named in the traceback |

### 4.4 Step 4 — Classify the failure

Ask these in order; stop at the first yes.

1. **Does `python -c "from app.config.settings import Settings; Settings.from_env()"` raise?**
   → *Configuration.* Go to §6.
2. **Does `python -c "import app.brokers.sandbox"` raise `ModuleNotFoundError`?**
   → *Dependency.* Go to §5.
3. **Does `python -m app paper-test` fail?** → *Logic or environment.* This test is fully offline
   and deterministic; if it fails, nothing downstream can be trusted. Go to §15.
4. **Does `python -m app monitor` return `"database_ok": false`?** → *Database.* Go to §12.
5. **Does `curl /health` fail but the server print errors?** → *API/process.* Go to §9.
6. **Does the sandbox probe return 401/403?** → *Auth — but read §7.3 before concluding the token
   is invalid.*
7. **None of the above** → *Logic.* Go to §10.

---

## 5. Dependency problems

### 5.1 The three requirement files

| File | Installs | Needed for |
| --- | --- | --- |
| `requirements.txt` | fastapi, pydantic, uvicorn, httpx, websockets, protobuf, python-dotenv, pytest, starlette | the backend, the API, the sandbox broker, tests |
| `requirements-kronos.txt` | torch + the Kronos stack | the AXIOM Kronos forecasting subsystem only |
| `requirements-desktop.txt` | PySide6 | the desktop GUI |

`requirements-lock.txt` also exists as a pinned set.

Note `requirements.txt` pins `starlette>=0.46,<1` with the comment
*"Keep the tested httpx-based TestClient; Starlette 1.x changes its client stack."* — installing
Starlette 1.x will break the test suite's `TestClient` usage. This is a real, documented trap.

### 5.2 Missing module

```powershell
# Which requirement file owns the missing name?
python -m pip show httpx
Select-String -Path requirements*.txt -Pattern "httpx"
```

```bash
grep -rn "httpx" requirements*.txt
```

Repair:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -U pip && python -m pip install -r requirements.txt
```

### 5.3 Wrong interpreter / venv not active

The single most common cause of "it worked yesterday" is a different interpreter.

```powershell
python -c "import sys, httpx; print(sys.executable); print(httpx.__file__)"
```

Both paths must point inside the same environment. If `sys.executable` is a system Python while
`httpx.__file__` is elsewhere, you have a mixed environment.

### 5.4 Python version

`pyproject.toml` declares the supported range. **Verify it rather than assuming:**

```powershell
python --version
Select-String -Path pyproject.toml -Pattern "requires-python"
```

### 5.5 PySide6 imports but the GUI does not open

`desktop/app.py` imports PySide6 at module scope. A missing system OpenGL library surfaces as an
`ImportError` naming `libGL.so.1` on Linux, and as a missing-DLL error on Windows.

**UNVERIFIED here** — the authoring sandbox has no `libGL.so.1` and no package index, so no Qt
window was ever created. Diagnose the desktop client headlessly instead:

```bash
python -c "import desktop.api_client, desktop.viewmodels; print('headless parts OK')"
```

`desktop/api_client.py` and `desktop/viewmodels.py` do **not** import Qt, so they can always be
exercised without a display.

### 5.6 protobuf

The feed decoder imports generated bindings: `app/data/upstox_stream.py:47`
`from app.data.upstox_proto import MarketDataFeed_pb2 as pb2`. `app/data/upstox_stream.py:13`
records the schema file `app/data/upstox_proto/MarketDataFeed.proto` and its sha256. A
`DecodeError` is caught at line 117 and logged as `UPSTOX_DECODE_ERROR` — it does **not** raise.
If every frame decodes to nothing, suspect a protobuf major-version mismatch with the generated
bindings, not the feed.

```powershell
python -c "import google.protobuf as p; print(p.__version__)"
python -c "from app.data.upstox_proto import MarketDataFeed_pb2; print('bindings OK')"
```

---

## 6. Configuration diagnostics

### 6.1 Confirm what is actually loaded — without printing secrets

```powershell
python -c "from app.config.settings import Settings as S; s=S.from_env(); print('mode',s.mode); print('broker',s.broker); print('data_source',s.data_source); print('db',s.database_url); print('calendar',s.market_calendar); print('sandbox_base',s.upstox_sandbox_base_url); print('history_url',s.upstox_sandbox_order_history_url); print('sandbox_token_set', bool(s.upstox_sandbox_access_token.strip())); print('market_data_token_set', bool(s.market_data_access_token.strip())); print('api_token_set', bool(s.api_token.strip()))"
```

Note the last three print **booleans only**. `Settings` also marks every secret field
`repr=False` (`api_token` line 87, `market_data_token` line 88, `data_ws_url` line 89,
`market_data_access_token` line 96, `upstox_sandbox_access_token` line 100), so printing a `Settings` object does not leak them.

### 6.2 Prove precedence is doing what you think

```bash
# .env says one thing, environment says another — environment must win
echo 'BROKER=LOCAL_PAPER' > /tmp/t.env
BROKER=UPSTOX_SANDBOX python -c "from app.config.settings import Settings; print(Settings.from_env().broker)"
```

Because `load_dotenv(..., override=False)`, the answer must be `UPSTOX_SANDBOX`. If it is
`LOCAL_PAPER`, something is loading `.env` with `override=True` — search for it:

```bash
grep -rn "load_dotenv" app/ desktop/ run.py
```

The only call in the codebase is `app/config/settings.py:240`.

### 6.3 The `.env` template

`.env.example` in the repo root is the authoritative, commented template. Copy it:

```powershell
Copy-Item .env.example .env
```

`.gitignore` excludes `.env`. **Confirm that before committing anything:**

```bash
git check-ignore -v .env
```

### 6.4 Common configuration failures and their exact signatures

| Message | Meaning | Fix |
| --- | --- | --- |
| `Only SQLite is supported` | `DATABASE_URL` is not `sqlite:///…` | Set `DATABASE_URL=sqlite:///trading_bot.db` |
| `BROKER=UPSTOX_SANDBOX requires UPSTOX_SANDBOX_ACCESS_TOKEN in the environment` | broker selected, token absent | Export the token, or use `BROKER=LOCAL_PAPER` |
| `Unknown data source` | `DATA_SOURCE` not in `replay/binance/generic/upstox` | See `Settings.__post_init__` |
| `CURRENCY must match Binance quote currency` | raised in `app/data/factory.py:9` | `--source binance` needs a symbol ending in `CURRENCY` |
| `DATA_WS_URL must be configured` | `app/data/factory.py:13` | `--source generic` needs `DATA_WS_URL` |
| `Set MARKET_DATA_SYMBOLS (e.g. NSE_EQ\|INE002A01018) for the Upstox data feed` | `app/data/factory.py:25` | Set `MARKET_DATA_SYMBOLS` |
| `Paper timeframe must match DEFAULT_TIMEFRAME; restart with separate DB to change` | `app/api/routes/paper.py:38` | Align `MARKET_DATA_TIMEFRAME`/`DEFAULT_TIMEFRAME` or use a fresh DB |
| `Unknown strategy: <name>` | `app/strategies/__init__.py:25` | Valid names come from `REGISTRY` — see §8.4 |

---

## 7. Upstox sandbox diagnostics

> **Everything in this section is documented from source. No live Upstox call was made while
> writing this guide.** Treat the HTTP status guidance as the code's *contract*, and verify it
> against your own responses before acting on it.

### 7.1 The endpoints the broker actually uses

From `app/brokers/sandbox.py` lines 176–178 and the call sites:

| Operation | Method | Path | Called from |
| --- | --- | --- | --- |
| Place order | `POST` | `{base_url}/v3/order/place` | `submit()` line 377 |
| Cancel order | `DELETE` | `{base_url}/v3/order/cancel?order_id=…` | `cancel()` line 812 |
| Modify order | `PUT` | `{base_url}/v3/order/modify` | `modify()` line 886 |
| Reconcile / probe | `GET` | `{history_url}?order_id=…` | `reconcile()` line 648, `_child_remote_status()` line 751, `lookup_intent_order()` line 488 |

Defaults (`Settings`, lines 104–106):

- `upstox_sandbox_base_url = "https://api-hft.upstox.com"` (line 101)
- `upstox_sandbox_order_history_url = "https://api.upstox.com/v2/order/history"` (line 102)

**Note the split:** orders go to `api-hft.upstox.com`; history goes to `api.upstox.com/v2`. Two
different hosts. A 404 on history is not evidence about the order endpoint, and vice versa.

### 7.2 The safe credential probe

`python -m app sandbox-test` (`cmd_sandbox_test`, `app/__main__.py:507`) is **read-only** unless
you pass `--place-order`.

```powershell
python -m app sandbox-test
python -m app sandbox-test --instrument "NSE_EQ|INE002A01018"
```

Verified without a token:

```json
{
  "scope": "REAL UPSTOX SANDBOX TEST",
  "performed": false,
  "reason": "UPSTOX_SANDBOX_ACCESS_TOKEN not set; nothing attempted, nothing claimed"
}
```
**exit code 3.** Exit 3 means *nothing was attempted* — it is not a failure of Upstox.

Verified refusal from `engine` when the broker is not sandbox-configured — **exit code 2**:

```json
{
  "ok": false,
  "error": "engine requires explicit BROKER=UPSTOX_SANDBOX configuration; LOCAL_PAPER continues via 'python -m app paper-live'"
}
```

### 7.3 **A 401/403 does not by itself mean the token is invalid**

This is the single most mis-diagnosed Upstox failure. Read `classify_history_probe()`
(`app/brokers/sandbox.py:134`) — it is the project's own answer to the question:

```python
if status in {401, 403}:      → auth_rejected      credential_accepted=False
if status == 429:             → rate_limited       credential_accepted=False
if status >= 500:             → provider_error     credential_accepted=False
if status in {400, 404} and "UDAPI100010" in codes:
                              → documented_not_found  credential_accepted=True   ← !
else:                         → unexpected         credential_accepted=False (fail closed)
```

**The critical line:** an HTTP **400 or 404 carrying the documented `UDAPI100010` ("order not
found") means the credential was ACCEPTED** and the probe order id simply does not exist. If you
conclude "my token is broken" from a 404, you are wrong — the code deliberately treats that as
success.

Conversely, a 401/403 can also be caused by things that are not the token:

- **Wrong host.** A sandbox token presented to a live order host, or a live-looking base URL.
  `UpstoxSandboxBroker.__init__` (lines 197–201) explicitly guards this: a `base_url` containing
  `live` but not `sandbox` logs `SANDBOX_BASE_URL_REVIEW`. Check for that log line first.
- **Static-IP restriction.** The module docstring (line 19) records the documented caveat
  `UDAPI1154`: order APIs can be restricted to a static IP. A 403 from a new machine or a VPN can
  be an IP allowlist rejection with a perfectly valid token.
- **Wrong token type.** The sandbox order token (`UPSTOX_SANDBOX_ACCESS_TOKEN`) is *not* the
  market-data token (`MARKET_DATA_ACCESS_TOKEN`). They are separate fields with separate
  purposes.
- **Expired daily token.** Upstox OAuth tokens are daily; see §11.2.

**Diagnostic sequence before blaming the token:**

```powershell
# 1. Which hosts are configured?
python -c "from app.config.settings import Settings as S; s=S.from_env(); print(s.upstox_sandbox_base_url); print(s.upstox_sandbox_order_history_url)"

# 2. Is the token even present, and is it the sandbox one?
python -c "from app.config.settings import Settings as S; s=S.from_env(); print('sandbox token chars:', len(s.upstox_sandbox_access_token.strip()))"

# 3. Is the broker already latched closed from an earlier 401?
python -c "print('a 401 sets UpstoxSandboxBroker.available=False for the lifetime of the object')"
```

### 7.4 The broker latches closed after any 401/403

`_request()` (line 240) does this on `401`/`403`:

```python
self.available = False
logger.error("SANDBOX_AUTH_FAILURE", extra={"http_status": ...})
raise SandboxAuthError(f"Sandbox credential rejected (HTTP {status}); broker disabled")
```

and every subsequent call begins with:

```python
if not self.available:
    raise SandboxAuthError("Sandbox broker disabled after an auth failure")
```

**Diagnostic consequence:** once one request 401s, *every later error is the same message
regardless of cause.* If you are seeing `"Sandbox broker disabled after an auth failure"`
repeatedly, find the **first** `SANDBOX_AUTH_FAILURE` in the log — that is the real event; the
rest are downstream.

### 7.5 Retry semantics (why a write never retries)

`_request()` line 247: `attempts = 2 if method.upper() == "GET" else 1`.

- **GET** retries once on `429`/`5xx` with a 0.05 s sleep.
- **POST/PUT/DELETE never retry** — the docstring states the reason: *"the remote outcome could
  already exist (duplicate-order safety)."*

So a timed-out `POST /v3/order/place` leaves the order in an **unknown** state. The engine handles
this via `SubmissionJournal` (§10.4) — do not "fix" it by adding retries.

Network failure raises `SandboxRequestError("Sandbox API unreachable (network error)")`.

### 7.6 Reconciliation and order history

`reconcile()` (line 621) calls `GET {history_url}?order_id=…` per order and maps provider status
through `_map_status()` (line 728): `complete`/`completed` → `FILLED`.

Documented caveat, from the module docstring (line 16): **orders are retained for one trading
day.** A reconciliation run the next day legitimately finds nothing. `FINAL_STATUSES =
{"FILLED", "CANCELLED", "REJECTED"}` (`app/engine/sandbox_engine.py:42`).

Cancel on an already-finalized order returns the documented `UDAPI100040`, which the code treats
as *already-closed, never fatal* (docstring line 10).

### 7.7 Inspecting the request without leaking the token

`_headers()` (line 230) builds the `Authorization: Bearer …` header per request. The docstring
(line 31) states the token is never logged, printed, or embedded in exceptions — and
`SandboxRequestError`'s docstring repeats it. To inspect traffic, use a proxy that **redacts**:

```powershell
$env:HTTPS_PROXY = "http://127.0.0.1:8080"
python -m app sandbox-test
# then inspect the proxy log — and redact Authorization headers before sharing (see §16)
```

---

## 8. CLI diagnostics

### 8.1 Two separate CLIs — do not confuse them

| CLI | Entry | Purpose |
| --- | --- | --- |
| `python -m app <command>` | `app/__main__.py`, `main()` line 1155 | operations: engine, sandbox, data platform, monitoring, backup |
| `python run.py --mode <mode>` | `run.py`, `parser()` line 30 | research: backtest, tournament, validation, paper, api |

### 8.2 The 22 operational subcommands

Verified via `python -m app --help` (exit 0):

`upstox-test, market-stream, paper-live, paper-test, upstox-login, engine, sandbox-test,
initialize, market-universe, history, dataset, universe, research, report, maintenance, ask, scan,
discover, lifecycle, replay, monitor, backup`

Each is registered in `parser()` (`app/__main__.py:1019+`) with
`sub.add_parser("<name>")` and wired by `set_defaults(func=cmd_<name>)`.

### 8.3 Tracing a command to its function

The dispatch is one line — `main()` (line 1155):

```python
def main(argv=None) -> None:
    args = parser().parse_args(argv)
    raise SystemExit(args.func(args))
```

So: **the subcommand name maps to `cmd_<name>` with hyphens becoming underscores.**

| Command | Function | Line |
| --- | --- | --- |
| `upstox-test` | `cmd_upstox_test` | `app/__main__.py` |
| `market-stream` | `cmd_market_stream` | `app/__main__.py` |
| `paper-live` | `cmd_paper_live` | `app/__main__.py` |
| `paper-test` | `cmd_paper_test` | `app/__main__.py` |
| `upstox-login` | `cmd_upstox_login` | `app/__main__.py` |
| `engine` | `cmd_engine` | line 408 |
| `sandbox-test` | `cmd_sandbox_test` | line 507 |
| `initialize` | `cmd_initialize` | `app/bootstrap.py:initialize` |
| `monitor` | `cmd_monitor` | line 966 |
| `backup` | `cmd_backup` | line 980 |

To confirm any mapping mechanically:

```bash
grep -n 'add_parser("monitor"' -A 2 app/__main__.py
```

### 8.4 Inspecting registration and the strategy registry

```bash
# Every registered strategy name
python -c "from app.strategies import catalog; [print(c['name'], c['default_parameters']) for c in catalog()]"
```

`REGISTRY` is built in `app/strategies/__init__.py:9` from seven classes:
`MovingAverageStrategy, MomentumStrategy, MeanReversionStrategy, BreakoutStrategy, RSIStrategy,
MACDStrategy, BollingerStrategy`. `create_strategy()` (line 23) raises
`ValueError("Unknown strategy: <name>")` for anything else.

### 8.5 Verified exit codes

| Command | Condition | Exit |
| --- | --- | --- |
| `python -m app --help` | — | 0 |
| `paper-test` | all checks pass | 0 |
| `monitor` | no alerts | 0 |
| `monitor` | `snapshot["alerts"]` non-empty | **1** (`app/__main__.py:975`) |
| `initialize` | offline (universe sync unavailable) | 0 — recorded honestly, not a failure |
| `sandbox-test` | no token | **3** |
| `upstox-test` | no `MARKET_DATA_SYMBOLS` | **2** |
| `engine` | `BROKER != UPSTOX_SANDBOX` | **2** |
| `engine` | `DATA_SOURCE != upstox` | **2** |

### 8.6 The offline self-test is your best first move

`python -m app paper-test` — verified output, **exit 0**, all 17 checks true:

```
pipeline_completed_STOPPED, market_status_gate_held_closed_candle,
duplicate_tick_counted_not_executed, exactly_two_orders_buy_then_sell,
no_duplicate_order_ids, two_fills_one_closed_trade, position_flat_after_round_trip,
realized_pnl_matches_trade_record, equity_is_capital_plus_realized_when_flat,
expected_closes_exact, restart_restored_equity_identical, restart_added_no_orders,
restart_replay_duplicate_suppressed, independent_run_economics_identical,
second_run_equity_exact, expected_equity_exact, expected_realized_pnl_exact
```

It is documented as *"OFFLINE deterministic self-test … pure standard library"*. It needs no
network, no credentials and no Upstox. **If this fails, stop diagnosing the broker and fix the
environment first.**

---

## 9. API diagnostics

### 9.1 Every route

**Open (no auth):**

| Route | File | Notes |
| --- | --- | --- |
| `GET /health` | `app/api/routes/health.py:7` | version, `mode`, `live_trading`, `real_order_routing` |
| `GET /` | `app/api/routes/health.py:18` | landing HTML |
| `GET /dashboard` | `app/api/routes/dashboard.py:177` | `loopback_or_bearer` — local browsers pass without a token |

**Bearer-protected** — `health.router` is included bare; `dashboard.router` applies per-route
dependencies; and `market, strategies, backtests, paper, v2, market_events` are all included with
`dependencies=[Depends(authenticate)]` (`app/api/server.py:91`):

| Route | File |
| --- | --- |
| `GET /dashboard/data` | `dashboard.py:77` |
| `POST /engine/stop` | `dashboard.py:146` |
| `POST /engine/resume` | `dashboard.py:169` |
| `GET /backtests`, `POST /backtests/run`, `GET /backtests/{id}`, `GET /tournaments` | `backtests.py` |
| `GET /market/status`, `GET /market/datasets` | `market.py` |
| `GET /strategies` | `strategies.py` |
| `GET /paper/status`, `/paper/account`, `/paper/positions`, `/paper/orders`, `/paper/trades`; `POST /paper/start`, `/paper/stop`, `/paper/pause`, `/paper/resume` | `paper.py` |
| `GET/POST /api/market-events`, `/api/market-events/{event_id}`, `POST …/ingest`, `POST …/{event_id}/observe`, `GET …/{event_id}/observations` | `market_events.py` (prefix at line 13) |
| `GET /api/v2/` — `overview, trades, performance, strategies, learning, experiments, market_data, risk, logs, report/weekly, command_center, portfolio, attribution, research_lab, learning_detail, market_intelligence, charts/equity.svg, charts/drawdown.svg, charts/attribution.svg, report` | `v2.py` (prefix at line 20) |

### 9.2 Exact checks

```powershell
# PowerShell — health
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json

# PowerShell — protected route
$h = @{ Authorization = "Bearer $env:API_TOKEN" }
Invoke-RestMethod -Headers $h http://127.0.0.1:8000/api/v2/overview | ConvertTo-Json -Depth 5
Invoke-RestMethod -Headers $h http://127.0.0.1:8000/api/v2/risk      | ConvertTo-Json -Depth 5
Invoke-RestMethod -Headers $h http://127.0.0.1:8000/api/v2/logs      | ConvertTo-Json -Depth 5
```

```bash
# POSIX
curl -s http://127.0.0.1:8000/health
curl -s -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/api/v2/overview
curl -s -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/api/v2/risk
```

Verified `/health` body:

```json
{"status":"ok","version":"3.0.5","mode":"PAPER","live_trading":false,"real_order_routing":"NOT_IMPLEMENTED"}
```

### 9.3 The auth state machine — verified

`app/api/auth.py:authenticate` (line 33):

| Condition | Status | Body |
| --- | --- | --- |
| `API_TOKEN` empty | **503** | `Set API_TOKEN to enable protected API endpoints` |
| Header missing or wrong | **401** | `Invalid or missing bearer token` + `WWW-Authenticate: Bearer` |
| Correct `Bearer <token>` | **200** | route payload |

All three rows were produced by execution. The comparison uses `hmac.compare_digest`, so it is
constant-time — a timing attack is not a plausible failure mode here.

**503 vs 401 is the key distinction:** 503 means *the server has no token configured* (fix
`API_TOKEN`); 401 means *the server has a token and yours does not match* (fix the client).

### 9.4 Latency header

`app/api/server.py:54` installs middleware adding `x-api-latency-ms` to every response and
keeping the last 100 samples in `app.state.api_latencies`. `GET /api/v2/overview` exposes
`api_latency_avg_ms` and `api_requests_observed` (verified: `"api_latency_avg_ms": 3.65`).

```powershell
(Invoke-WebRequest http://127.0.0.1:8000/health).Headers["x-api-latency-ms"]
```

### 9.5 A trap when writing your own API test

`app.state.repository`, `app.state.broker` and `app.state.simulator` are assigned **inside the
`lifespan` context manager** (`app/api/server.py:34`). If you build a `TestClient` without
entering it, the lifespan never runs and any protected route dies with:

```
AttributeError: 'State' object has no attribute 'repository'
```

This was reproduced while writing this guide. The correct form:

```python
with TestClient(create_app(Settings.from_env())) as client:   # ← `with` is required
    client.get("/api/v2/overview", headers={"Authorization": "Bearer …"})
```

### 9.6 Starting the server

`run.py:153` — `--mode api` runs `uvicorn.run(create_app(settings), host=args.host,
port=args.port, …)` with defaults `--host 127.0.0.1 --port 8000`. The ASGI factory is also
available as `app.main:create_app` with `--factory`.

```powershell
python run.py --mode api --host 127.0.0.1 --port 8000
# alternative
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

**Binding to `127.0.0.1` is deliberate.** Binding wider exposes protected endpoints to the
network; `loopback_or_bearer` (`app/api/auth.py:47`) stops granting the dashboard page for free
once clients are non-loopback.

---

## 10. Trading execution diagnostics

### 10.1 The full path of one signal

`SandboxEngine.process_candle()` (`app/engine/sandbox_engine.py:348`) →
`_check_protective_exits()` (line 455) → strategy evaluation gated by
`_strategy_evaluation_due()` (line 396) → `_act(signal, candle)` (line 484) →
`_size_entry()` (line 790) → `_allocator_allowance()` (line 811) →
`HardRiskLimits.check()` (`app/risk/limits.py:53`) → `_submit()` (line 826) →
`UpstoxSandboxBroker.submit()` (line 304) → `POST /v3/order/place`.

### 10.2 Risk gate: exact rejection reasons

`HardRiskLimits.check()` returns `RiskDecision(allowed, reason)`. The complete set:

| `reason` | Condition |
| --- | --- |
| `risk_reducing_exit_allowed` | `side == "SELL"` — **always passes**, before any cap |
| `max_position_value_exceeded` | `open_position_value + notional > max_position_value` |
| `max_open_positions_reached` | `open_positions >= max_open_positions` |
| `max_daily_loss_reached` | `realized_today + unrealized <= -max_daily_loss` |
| `max_trades_per_day_reached` | `trades_today >= max_trades_per_day` |
| `within_limits` | allowed |

**Design fact worth knowing when diagnosing:** SELLs bypass every position cap on purpose. The
module docstring states *"exits/risk-reducing actions stay allowed so limits cannot trap the
account in a position."* A `0` limit means **unlimited/disabled**, not "zero allowed".

`HardRiskLimits` is a `frozen` dataclass built only by `from_settings()` — no learning output can
mutate it. `OrderIntent.total_exposure` adds `estimated_fees` to notional, so fee-aware exposure
is what gets capped.

Rejected orders are counted in `SandboxEngine.rejected_by_risk` and surfaced by `status()`
(line 982) and `GET /api/v2/risk`.

### 10.3 Order state machine

`FINAL_STATUSES = {"FILLED", "CANCELLED", "REJECTED"}` (`app/engine/sandbox_engine.py:42`).
Provider statuses map through `UpstoxSandboxBroker._map_status()` (line 728). Sliced orders
aggregate child statuses via `_aggregate_child_status()` (line 767), which consults
`_child_remote_status()` (line 743) per child.

### 10.4 Crash safety: the submission journal

`SubmissionJournal` (`app/engine/state.py:53`) is the answer to "the POST timed out — did the
order exist?"

- `record(intent_id, …)` line 74 — written **before** the order is placed.
- `mark(intent_id, state, detail=…)` line 112 — updated on the outcome.
- `resubmission_forbidden(intent_id)` line 130 — prevents duplicate orders.
- `unresolved()` line 134 — everything without a final outcome.

On startup, `SandboxEngine.restore_from_repository()` (line 200) replays state and
`_surface_unresolved_intents()` (line 266) reports anything ambiguous. `status()` exposes
`unresolved_intents` as a count.

**Diagnostic:** a non-zero `unresolved_intents` after a restart means there are orders whose
remote outcome was never confirmed. Reconcile before doing anything else (§17.4).

Intent ids are deterministic — `intent_id_for(strategy, symbol, candle, side)` (line 45) — which
is what makes duplicate suppression possible across restarts.

### 10.5 The ledger

`SandboxLedger` (`app/brokers/sandbox.py:54`) mirrors reconciled fills locally:
`apply_fill()` line 69, `unrealized()` line 91, `equity()` line 96, `snapshot()` line 99.
`recover_ledger_state(ledger, repository)` (`app/engine/state.py:205`) rebuilds it on startup.
`SandboxEngine.ledger_mark(price)` (line 940) and `_persist_equity()` (line 944) keep it current.

### 10.6 Daily risk rollover

`DailyRiskAccount` (`app/engine/state.py:146`) uses `market_timezone` (default `Asia/Kolkata`,
`Settings` line 126) to compute the trading-day boundary: `trading_date()` line 168,
`rollover()` line 171, `register_trade()` line 185, `add_realized()` line 191. If
`max_daily_loss` appears to reset at the wrong time, check `MARKET_TIMEZONE`, not the limit.

### 10.7 Failure-safe behaviour

- `emergency_stop()` (line 284) persists the flag, records `EMERGENCY_STOP`, cancels every
  non-final order, and records `CANCEL_FAILURE` per failure rather than hiding it.
- `_check_emergency()` (line 312) reads the persisted flag — so a stop **survives a restart**.
- The engine loop `run()` (line 318) stops on the flag, on `max_candles`, or when the provider
  ends.

---

## 11. Market-data diagnostics

### 11.1 Provider selection

`app/data/factory.py:stream_provider(settings, source, …)` (line 4):

| `source` | Class | Requirement |
| --- | --- | --- |
| `replay` | `ReplayStream` | in-memory `bars` |
| `binance` | `BinanceStream` | symbol must end in `CURRENCY` |
| `generic` | `JSONWebSocketStream` | `DATA_WS_URL` set |
| `upstox` | `UpstoxStream` | `MARKET_DATA_SYMBOLS` (or `--symbol`) |

### 11.2 Authentication (market data ≠ sandbox)

`app/data/upstox_auth.py` implements the OAuth authorization-code flow:

- `LOGIN_DIALOG_URL = "https://api.upstox.com/v2/login/authorization/dialog"` (line 29)
- `TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"` (line 30)
- `build_login_url()` line 152, `exchange_code_for_token()` line 162, `store_token_in_env()`
  line 202, `run_login_flow()` line 219

The feed itself authorizes separately:
`AUTHORIZE_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"`
(`app/data/upstox_stream.py:52`), exchanged for a **single-use `wss://` URL** (line 328, and the
docstring notes it is *never logged*).

`.env.example` records the real-world trap: a 1-year read-only Analytics Token lists a *Websocket*
category in the docs, but feed access is verified **behaviourally** by
`python -m app upstox-test` before any connection is attempted; wrong or expired credentials are
rejected with guidance and **no connection is attempted**.

Verified without symbols — **exit 2**:

```json
{"ok": false, "error": "Set MARKET_DATA_SYMBOLS (e.g. NSE_EQ|INE002A01018) for the Upstox data feed"}
```

### 11.3 The stream loop, reconnect and staleness

`UpstoxStream.stream()` (`app/data/upstox_stream.py:470`) — *"Yield completed candles with
reconnect + resubscribe + re-auth."*

Constructor defaults (lines 234–235): `max_backoff = 60.0`, `stale_after = 90.0`.

| Observable | Where |
| --- | --- |
| `self.reconnects` | line 296, surfaced in status at line 456 |
| `connect_latency` (attempts / avg / max / last ms) | line 302 |
| `counters["stale_feed"]` | line 455 |
| status string `CONNECTING` → `RECONNECTING` | lines 482, 582 |
| log events | `UPSTOX_RESUBSCRIBE` (527), `UPSTOX_RECONNECT` (585), `feed_stale` (540), `UPSTOX_DECODE_ERROR` (117) |

Reconnection re-authorizes and resubscribes with bounded exponential backoff capped at
`max_backoff`; on success `attempts` resets to 0 (line 558).

**Stale data:** `LiveCandleBuilder` — imported from `app/data/candles.py` at line 43 and constructed at line 305
with `stale_after=stale_after` — exposes `stale()`, checked at line 538; when true the loop logs `feed_stale` and does **not** emit a
candle. So a missing candle during a quiet period is expected behaviour, not data loss.

### 11.4 Protobuf decoding

`UpstoxProtobufDecoder` (line 97): `decode()` line 110, `_tick_event()` 136, `_full_event()` 148,
`_market_info_event()` 185, `_aggregate_status()` 200. Mode is `ltpc` (build candles from ticks)
or `full` (official aggregate bars), set by `MARKET_DATA_MODE`.

A `google.protobuf.message.DecodeError` is caught (line 117) and logged as
`UPSTOX_DECODE_ERROR` with `reason: protobuf` — **the frame is dropped, the stream continues.**
If you see a flood of these, suspect the protobuf runtime version versus the generated bindings,
not the feed.

Symbol mapping: `to_symbol()` line 80 / `from_symbol()` line 85 convert between Upstox instrument
keys (`NSE_EQ|INE002A01018`) and symbols.

### 11.5 Missing instruments

The instrument master is synced by `python -m app market-universe` into the `instruments` table
(`app/database/models.py:57`), managed by `app/universe/manager.py MarketUniverseManager`.
`python -m app initialize` attempts a sync and records the result honestly — verified offline:

```
UNIVERSE_SYNC_UNAVAILABLE
{ "ok": true, "idempotent": false, ... }
```

**exit 0.** An unavailable sync is *recorded*, never faked, and does not fail the command.

### 11.6 Streaming universe guard

`Settings.streaming_universe` (line 117) is commented *"comma-separated instrument keys; NEVER
the whole universe"*, and `.env.example` states subscribing to the entire instrument universe
*"is refused by design"*. If a stream subscription is rejected, this is why.

---

## 12. Database and logging diagnostics

### 12.1 Location

`DATABASE_URL` default is `sqlite:///trading_bot.db` (`Settings` line 86) — a **relative** path,
so it resolves against the **current working directory**, not the repo root. Running the CLI from
a different directory silently creates a second database. This is a very common source of "my
data disappeared".

```powershell
python -c "from app.config.settings import Settings as S; import os; s=S.from_env(); print(os.path.abspath(s.database_url.replace('sqlite:///','')))"
```

### 12.2 Connection settings

`Database.__init__` (`app/database/database.py:9`) applies:

```python
sqlite3.connect(path, timeout=10, check_same_thread=False)
PRAGMA journal_mode=WAL
PRAGMA synchronous=FULL
PRAGMA foreign_keys=ON
executescript(SCHEMA)
migrate_transitions_child_identity(connection)   # schema_version 5
```

`Database.transaction()` is an `@contextmanager` guarded by an `RLock` — so the process is
single-writer by construction, and `Repository.release_writer()` is called on API shutdown
(`app/api/server.py:40`).

WAL means the directory normally holds `trading_bot.db`, `trading_bot.db-wal` and
`trading_bot.db-shm`. **All three belong to the database.** Copying only the `.db` file can lose
committed transactions.

### 12.3 Schema

`app/database/models.py` holds `SCHEMA` as one `executescript`. Tables include:
`schema_version, backtests, orders, fills, trades, portfolio_snapshots, state, research_runs,
signals, sandbox_orders, sandbox_transitions, model_versions, system_events, engine_state,
instruments, universe_syncs, download_jobs, coverage, quality_events, dataset_versions,
experiments, candidates, shadow_trades, learning_events, daily_snapshots, strategy_lineage,
strategy_lifecycle, regime_history, opportunity_scans, research_questions, system_status,
reality_gap, confidence_calibration, market_events, market_event_sources,
market_event_observations, quality_scores, liquidity_snapshots, volatility_sizing_logs,
cross_market_context`.

Migration marker: `SCHEMA_VERSION_TRANSITIONS_CHILD = 5` (line 272), applied by
`migrate_transitions_child_identity()` (line 297), which is idempotent.

### 12.4 Inspecting safely (read-only)

```powershell
python -c "import sqlite3; c=sqlite3.connect('file:trading_bot.db?mode=ro', uri=True); print(c.execute('PRAGMA integrity_check').fetchone()); print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\")])"
```

```bash
sqlite3 "file:trading_bot.db?mode=ro" "PRAGMA quick_check; SELECT COUNT(*) FROM sandbox_orders;"
```

`mode=ro` guarantees you cannot modify state while diagnosing.

### 12.5 Health and corruption

`python -m app monitor` (`cmd_monitor`, line 966) calls `SystemMonitor.snapshot()`
(`app/monitoring/system.py:41`), which runs `PRAGMA quick_check` and sets `database_ok`.
Alert thresholds are `ALERT_THRESHOLDS = {"disk_used_pct": 0.90, "memory_used_pct": 0.92}`
(line 17). Alerts are `disk`, `memory`, `database`; any alert makes the command **exit 1** and
records a `SYSTEM_ALERT` event.

Verified output:

```json
{"ts":"…","load_1m":0.057,"rss_mb":23.4,"memory_used_pct":0.0707,
 "disk_used_pct":0.054,"database_mb":0.44,"database_ok":true,"alerts":[]}
```

### 12.6 Backup, verify, restore

`RecoveryManager` (`app/database/recovery.py:24`) — backups use SQLite's online-backup API
(consistent while writers run), every backup is verified with `integrity_check` plus
`CORE_TABLES` counts **before it counts**, and **restore refuses any backup that fails
verification**.

```powershell
python -m app backup backup --label pre-change
python -m app backup verify --path backups\<file>.db
python -m app backup restore --path backups\<file>.db
```

`CORE_TABLES` (line 16): `signals, sandbox_orders, sandbox_transitions, instruments, coverage,
model_versions, system_events, engine_state`.

Backup filenames are `arena-<UTC timestamp>-<label>.db` in `--backup-dir` (default `backups`).

### 12.7 Logging

`app/logging/logger.py`:

- Logger name: **`bot`** (child loggers are `bot.sandbox`, `bot.api`, `bot.bootstrap`, …).
- `JsonFormatter` emits `timestamp, level, component, event` plus only the whitelisted
  `FIELDS = {"symbol","strategy","signal","order","price","quantity","error"}` (line 7). The
  module docstring states it *"Never accepts arbitrary credential/config payloads"* — an
  arbitrary `extra=` key is silently dropped, which is a deliberate leak guard.
- `configure(level="INFO", path=None)` (line 22) attaches a `StreamHandler`, plus a
  `FileHandler` when `path` is given. It only adds handlers `if not logger.handlers`, so calling
  it twice does not duplicate output.
- `logger.propagate = False`.

**File location — an important asymmetry:** `run.py:152` is the only call site that passes a path:

```python
configure(settings.log_level, ROOT / "logs/bot.jsonl")
```

`app/__main__.py` contains **no** `configure(...)` call (verified by grep). So:

| You ran | Logs go to |
| --- | --- |
| `python run.py --mode …` | stderr **and** `<repo>/logs/bot.jsonl` |
| `python -m app <command>` | stderr **only** |

If you are looking for a `logs/bot.jsonl` that a `python -m app` run never wrote, that is why —
capture stderr instead:

```powershell
python -m app engine 2>&1 | Tee-Object -FilePath logs\engine.jsonl
```

```bash
python -m app engine 2>&1 | tee logs/engine.jsonl
```

Each line is JSON, so filter with:

```bash
grep '"event": "SANDBOX_AUTH_FAILURE"' logs/engine.jsonl
grep -E 'EMERGENCY_STOP|RISK_REJECT|CANCEL_FAILURE' logs/engine.jsonl
```

---

## 13. AXIOM integration diagnostics

### 13.1 The path

`desktop/app.py` → `ArenaApiClient` (`desktop/api_client.py:15`) →
`ArenaApiClient._request()` (line 21) → `urllib.request.urlopen(request, timeout=self.timeout)`
(line 26) → Arena `/api/v2/...` → parsed into view models (`desktop/viewmodels.py`) → Qt widgets.

**This is a plain HTTP client.** There is no persistent connection, no WebSocket, and no
background thread in `api_client.py`. A failed request raises from `urlopen`.

### 13.2 Endpoint configuration

`desktop/app.py:37–38`:

```python
base_url = os.getenv("ARENA_BACKEND_URL", "http://127.0.0.1:8000")
token    = os.getenv("ARENA_API_TOKEN", "")
```

`ArenaApiClient.__init__` does `base_url.rstrip("/")` (line 17), so a trailing slash is harmless.
Default timeout is `10.0` seconds (line 16).

```powershell
$env:ARENA_BACKEND_URL = "http://127.0.0.1:8000"
$env:ARENA_API_TOKEN   = "<your API_TOKEN>"
python -m desktop
```

### 13.3 Diagnosing a connection failure headlessly

You do **not** need Qt to test the transport. `desktop/api_client.py` does not import PySide6:

```bash
python -c "
from desktop.api_client import ArenaApiClient
c = ArenaApiClient('http://127.0.0.1:8000', '<token>')
try:
    print(c.overview())
except Exception as e:
    print(type(e).__name__, e)
"
```

| Exception | Meaning | Fix |
| --- | --- | --- |
| `URLError` / `ConnectionRefusedError` | nothing listening at `ARENA_BACKEND_URL` | start `python run.py --mode api` |
| `HTTPError 401` | wrong or missing `ARENA_API_TOKEN` | match Arena's `API_TOKEN` |
| `HTTPError 503` | Arena has no `API_TOKEN` set | set it server-side |
| `TimeoutError` / socket timeout | server slow or firewalled | see the 10 s default; check firewall |
| `HTTPError 404` | wrong path or base URL | confirm `/api/v2/overview` works via curl |

### 13.4 Stale or offline data in the UI

The desktop app is a **polling viewer**. If the backend stops, the pages keep showing the last
fetched payload — the client does not clear them. `desktop/app.py:271` carries the operator-facing
text reminding that the app is configured via `ARENA_BACKEND_URL, ARENA_API_TOKEN`.

To distinguish "Arena says X" from "the UI is showing a stale X", query the API directly (§9.2)
and compare. If curl and the UI disagree, the UI is stale.

### 13.5 Capability mapping

Each page maps to client methods, which map 1:1 to `/api/v2` routes:

| Page (`desktop/app.py:123`) | Client method | Route |
| --- | --- | --- |
| Command Center | `command_center(question)` (line 69) | `GET /api/v2/command_center?q=…` |
| Live Market | `live_market()` (line 57), `market_data()` (48) | `GET /api/v2/market_data` |
| Portfolio | `portfolio()` (74) | `GET /api/v2/portfolio` |
| Trades | `trades()` (33) | `GET /api/v2/trades` |
| Performance | `performance()` (36) | `GET /api/v2/performance` |
| Strategies | `strategies()` (39) | `GET /api/v2/strategies` |
| Research Lab | `research_lab()` (80) | `GET /api/v2/research_lab` |
| Learning | `learning()` (42), `learning_detail()` (86) | `GET /api/v2/learning`, `/learning_detail` |
| Market Intelligence | `market_intelligence()` (83) | `GET /api/v2/market_intelligence` |
| Risk | `risk()` (51) | `GET /api/v2/risk` |
| Logs | `logs()` (54) | `GET /api/v2/logs` |
| Reports | `report_markdown()` (60) | `GET /api/v2/report/weekly` |

### 13.6 STOP — the only mutating control

`desktop/app.py:135` creates the `EMERGENCY STOP` button; line 469 `_on_emergency_stop` shows a
confirmation dialog, then line 475 calls `self._safe(self.client.emergency_stop, {})`.

`ArenaApiClient.emergency_stop()` (line 98) → `POST /engine/stop` →
`app/api/routes/dashboard.py:146 engine_stop()`, which:

1. records `EMERGENCY_STOP_REQUESTED` with `source: "dashboard"`;
2. calls `engine.emergency_stop(reason="dashboard")` **if** `app.state.sandbox_engine` is set;
3. pauses the paper simulator if running;
4. **persists** `engine_state["emergency_stop"] = {"stopped": True, …}`.

`resume()` (line 101) → `POST /engine/resume` → `dashboard.py:169` clears the flag and records
`ENGINE_RESUME_REQUESTED`.

`desktop/app.py:414–415` toggles button visibility from `risk.get("emergency_stop")`, which comes
from `GET /api/v2/risk` → `v2.py:138`, reading the same persisted flag. **So the UI's STOP state
is the persisted engine flag, not local UI state** — it survives an app restart and is visible
from any other client.

### 13.7 A note on `app.state.sandbox_engine`

`create_app` sets `app.state.sandbox_engine = None` (`app/api/server.py:92`) with the comment
*"set by the sandbox engine embedding"*. If you run the API server **without** that embedding,
`POST /engine/stop` still persists the flag (so a later `engine` start honours it via
`_check_emergency()`), but there is **no live engine object to cancel orders**. Do not assume a
STOP pressed against a bare API server has cancelled anything at the broker — verify with
`sandbox-test` / reconciliation (§17.4).

### 13.8 GUI thread safety

`desktop/app.py` docstring (line 4) states actions are *"limited to the persisted emergency
STOP/RESUME flags"*. Requests run through `_safe(...)`. **UNVERIFIED:** whether every poll is
off the Qt GUI thread could not be tested here — no Qt window was ever created in the authoring
sandbox. If the UI freezes during a slow poll, that is the first thing to instrument.

---

## 14. Windows / PowerShell / packaging diagnostics

> **Authored on Linux. Nothing in this section was executed on Windows 11.** The file paths and
> script names are real (verified to exist in the repo); the shell behaviour is not verified.

### 14.1 Execution policy

`PowerShell cannot load file … because running scripts is disabled on this system` is an
execution-policy error, not a Python error.

```powershell
Get-ExecutionPolicy -List
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Note the repo ships `.bat` files — `desktop/run_windows.bat` and
`desktop/run_kronos_windows.bat` — which bypass execution policy entirely when launched via
`cmd.exe`.

### 14.2 venv activation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1      # PowerShell
.\.venv\Scripts\activate.bat      # cmd.exe
python -c "import sys; print(sys.executable)"
```

The printed path must be inside `.venv`. If it is not, the venv is not active and you are
diagnosing the wrong interpreter.

### 14.3 Firewall

```powershell
Get-NetFirewallApplicationFilter -Program "*python*" | Get-NetFirewallRule
Test-NetConnection -ComputerName 127.0.0.1 -Port 8000
```

`run.py` defaults to `--host 127.0.0.1`, which needs no inbound firewall rule. A rule is only
needed if you deliberately bind wider.

### 14.4 Certificates / TLS

`httpx` (the sandbox broker) and `urllib` (the desktop client) both rely on the platform trust
store. A TLS failure appears as `httpx.ConnectError` / `ssl.SSLCertVerificationError`.
`requirements.txt` includes `tzdata` only on Windows
(`platform_system == "Windows"`), because Windows ships no IANA timezone database for `zoneinfo`
— and `MARKET_TIMEZONE` (default `Asia/Kolkata`) is used by `DailyRiskAccount`. A missing
`tzdata` on Windows therefore breaks daily-risk date boundaries, not just formatting.

```powershell
python -c "from zoneinfo import ZoneInfo; print(ZoneInfo('Asia/Kolkata'))"
```

### 14.5 Packaging

`tools/build_windows_executables.py` builds `AXIOM.exe` and `AXIOM-Kronos.exe` (the latter's
entry point is `python -m axiom.worker.child`), with explicit `--hidden-import` entries.

**UNVERIFIED — this script has never been executed in any environment available here.** Diagnose
a packaged executable by comparing against the interpreter run:

```powershell
.\dist\AXIOM.exe --help
python -m app --help
```

If the interpreter run works and the `.exe` does not, the difference is the bundle (a missing
hidden import), not the code.

### 14.6 PATH and launcher

```powershell
Get-Command python | Format-List Source
py -0p          # list installed Pythons via the Windows launcher
```

---

## 15. Testing diagnostics

### 15.1 Configuration

`pyproject.toml` sets `[tool.pytest.ini_options] pythonpath = ["."]` and `testpaths = ["tests"]`.
There are **47** test files in `tests/`.

```powershell
python -m pytest                    # full suite
python -m pytest -q                 # quieter
python -m pytest tests/test_v1_sandbox.py            # one file
python -m pytest tests/test_v1_sandbox.py -k cancel  # one area
python -m pytest "tests/test_v1_engine.py::TestX::test_y" -x   # one test, stop on first failure
python -m pytest -x --lf            # rerun only last failures
```

### 15.2 Verified baseline

```
850 passed, 10 skipped in 71.06s
```

Run at commit `60113ce`, Linux, Python 3.11.2, with `requirements.txt` installed. **Record your
own baseline before changing anything** — a count that moves is the signal.

### 15.3 Tests by area

| Concern | File |
| --- | --- |
| Sandbox broker | `tests/test_v1_sandbox.py` |
| Engine | `tests/test_v1_engine.py`, `tests/test_v2_engine_integration.py` |
| Execution safety | `tests/test_v3_0_1_execution_safety.py` |
| CLI safety (no live path) | `tests/test_v1_cli_safety.py` |
| Settings parsing | `tests/test_settings_parser.py` |
| Dashboard auth | `tests/test_dashboard_auth_flow.py`, `tests/test_v1_dashboard.py` |
| Live/paper separation | `tests/test_v1_separation.py` |
| API + CLI | `tests/test_v2_api_cli.py`, `tests/test_runtime_database_api.py` |
| Upstox data feed | `tests/test_upstox_live.py`, `test_upstox_v062.py`, `test_upstox_v063.py` |
| Migrations | `tests/test_v2_migrations.py` |
| Resilience | `tests/test_resilience.py` |
| Security | `tests/test_final_security.py` |
| 3.0.5 gates | `tests/test_performance_hardening_3_0_5.py` |

### 15.4 Isolating a failure

```bash
# 1. Does it fail alone, or only with others? (test pollution)
python -m pytest tests/test_v1_engine.py -q
python -m pytest tests/test_v1_engine.py::TestX::test_y -q

# 2. Full traceback + locals
python -m pytest tests/test_v1_engine.py::TestX::test_y -l --tb=long

# 3. Show captured stdout/stderr
python -m pytest tests/test_v1_engine.py::TestX::test_y -s

# 4. Is it order-dependent?
python -m pytest tests/ -p no:randomly -q
```

### 15.5 Skips are meaningful

Skips in this suite are usually *capability* skips, not laziness — e.g. Kronos tests skip when
`AXIOM_KRONOS_SOURCE` is unset, and `tests/test_v2_universe.py` has a pre-existing TLS-dependent
skip. **Always read the skip reason:**

```bash
python -m pytest -rs -q
```

### 15.6 Sandbox/live integration tests

Tests that would touch the real network are written to refuse when credentials are absent rather
than to fail. That is why `sandbox-test` exits 3 with `"performed": false` instead of erroring.
Do not "fix" a skip by inventing credentials.

---

## 16. Never expose secrets

### 16.1 What counts as a secret here

| Variable | Where used |
| --- | --- |
| `API_TOKEN` | `app/api/auth.py` bearer gate |
| `UPSTOX_SANDBOX_ACCESS_TOKEN` | `app/brokers/sandbox.py:_headers` |
| `MARKET_DATA_ACCESS_TOKEN` / `UPSTOX_ACCESS_TOKEN` | `app/data/upstox_stream.py` authorize |
| `UPSTOX_CLIENT_SECRET` | `app/data/upstox_auth.py:exchange_code_for_token` |
| `MARKET_DATA_TOKEN`, `DATA_WS_URL` (if it embeds a key) | `app/data/factory.py:14` |
| Any `Authorization` header in a captured request | proxy logs, HAR files |
| `.env` itself | repo root |

### 16.2 What the code already does for you

- Every secret field in `Settings` is declared `repr=False` — printing a `Settings` object omits
  them (`app/config/settings.py` lines 87, 88, 89, 96, 100).
- `app/logging/logger.py` only serializes the whitelisted `FIELDS`; arbitrary `extra=` keys are
  dropped. Docstring: *"Never accepts arbitrary credential/config payloads."*
- `app/brokers/sandbox.py` docstring line 31: the token is *"never logged, printed, or embedded in
  exceptions"*; `SandboxRequestError`'s docstring repeats it.
- `app/data/upstox_stream.py:328`: the single-use `wss://` URL is *"never logged"*.
- `app/api/auth.py` docstring: the dashboard page contains **no** secrets; the operator's
  `API_TOKEN` is never rendered into it.
- `tools/security_scan.py` exists to scan for leaked secrets.

### 16.3 Rules when you are diagnosing

1. **Never** paste `.env` into a chat, issue or screenshot.
2. **Never** print a token to check it — print its *length* or a hash:
   ```bash
   python -c "from app.config.settings import Settings as S; t=S.from_env().upstox_sandbox_access_token.strip(); print('len', len(t))"
   ```
3. **Redact before sharing any HTTP capture.** At minimum replace the value of every
   `Authorization` header with `Bearer <REDACTED>`, and drop `access_token` query parameters.
4. **Do not enable request-body logging** on the broker to debug an order problem — the place
   order body carries no token, but a proxy capture will still contain the header.
5. **Check the ignore rule before committing:**
   ```bash
   git check-ignore -v .env
   git log -p --all -S "UPSTOX_SANDBOX_ACCESS_TOKEN" -- .env
   ```
6. **If a token has been committed or pasted, rotate it.** Redacting history is not enough.

### 16.4 A safe "is it set?" check

```powershell
python -c "from app.config.settings import Settings as S; s=S.from_env(); [print(k, 'SET' if v else 'unset') for k,v in {'API_TOKEN':s.api_token,'MARKET_DATA_ACCESS_TOKEN':s.market_data_access_token,'UPSTOX_SANDBOX_ACCESS_TOKEN':s.upstox_sandbox_access_token}.items()]"
```

This prints `SET` / `unset` only.

---

## 17. Emergency recovery

### 17.1 Stop the engine

Three ways, in order of preference:

**a) From the API (works while the server runs):**
```powershell
$h = @{ Authorization = "Bearer $env:API_TOKEN" }
Invoke-RestMethod -Method Post -Headers $h http://127.0.0.1:8000/engine/stop
```

**b) From the desktop app:** the `EMERGENCY STOP` button (`desktop/app.py:135`), which confirms
first, then calls `POST /engine/stop`.

**c) Kill the process (last resort):**
```powershell
Get-Process python | Where-Object { $_.CommandLine -like "*-m app engine*" }
Stop-Process -Id <pid>
```
```bash
pkill -f "app engine"
```

### 17.2 Verify it actually stopped

The stop is **persisted**, so verification does not depend on the process still running:

```powershell
$h = @{ Authorization = "Bearer $env:API_TOKEN" }
(Invoke-RestMethod -Headers $h http://127.0.0.1:8000/api/v2/risk).emergency_stop
```

or straight from the database (read-only):

```bash
sqlite3 "file:trading_bot.db?mode=ro" "SELECT payload FROM engine_state WHERE key='emergency_stop';"
```

Expect `{"stopped": true, "ts": "...", "reason": "..."}`.

`SandboxEngine._check_emergency()` (line 312) reads exactly this key, so **a restarted engine
will refuse to run** while it is set. That is the property you are relying on — confirm it by
reading the flag, not by assuming the kill worked.

Also confirm no process is still bound:
```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen
```

### 17.3 Preserve logs and state **before** changing anything

```powershell
# Logs (only exist if you ran via run.py, or captured stderr — see §12.7)
Copy-Item logs\bot.jsonl logs\incident-$(Get-Date -Format yyyyMMdd-HHmmss).jsonl

# Database — use the project's own verified backup, not a file copy (WAL!)
python -m app backup backup --label incident
python -m app backup verify --path backups\<newest>.db
```

Use `python -m app backup`, not `Copy-Item`, because the database runs in WAL mode and a bare
file copy can omit committed transactions sitting in `-wal`.

### 17.4 Verify broker state before restarting

This is the step people skip. A crash between `POST /v3/order/place` and the response leaves an
order whose remote outcome is unknown.

```powershell
# 1. Credentials still valid, nothing placed
python -m app sandbox-test

# 2. Unresolved intents recorded locally
sqlite3 "file:trading_bot.db?mode=ro" "SELECT intent_id, state FROM sandbox_orders;"

# 3. Remote truth — reconcile against order history (one trading day retention)
python -c "from app.config.settings import Settings as S; s=S.from_env(); print(s.upstox_sandbox_order_history_url)"
```

`SandboxEngine.status()` reports `unresolved_intents` (line 1005); `SubmissionJournal.unresolved()`
(`app/engine/state.py:134`) is the underlying query. `reconcile()`
(`app/engine/sandbox_engine.py:903`) walks open orders against the history endpoint.

**If `unresolved_intents > 0`, reconcile before restarting.** And remember `resubmission_forbidden()`
(line 130) exists precisely so a restart cannot duplicate an order — do not clear the journal to
"fix" a stuck state.

### 17.5 Restart safely

```powershell
# 1. Confirm the stop flag is still set (it should be)
# 2. Clear it deliberately
$h = @{ Authorization = "Bearer $env:API_TOKEN" }
Invoke-RestMethod -Method Post -Headers $h http://127.0.0.1:8000/engine/resume

# 3. Verify the pipeline is sane BEFORE the engine
python -m app paper-test          # must be exit 0
python -m app monitor             # must be exit 0, alerts []
python -m app sandbox-test        # must show credential_accepted

# 4. Only then start, bounded first
python -m app engine --max-candles 20
```

`--max-candles` (`app/__main__.py:1043`) exists for exactly this: *"bound the run for
tests/verification (default: unlimited)"*.

### 17.6 Prevent accidental live trading

This is enforced in code, not by convention. Verified rejections:

| Attempt | Result |
| --- | --- |
| `MODE=LIVE` | `ValueError: Only MODE=PAPER and LIVE_TRADING=false are permitted` |
| `LIVE_TRADING=true` | same |
| `BROKER=LIVE` | `ValueError: LIVE trading is not enabled in V1` |
| `BROKER=<other>` | `ValueError: BROKER must be LOCAL_PAPER or UPSTOX_SANDBOX` |

`GET /health` always reports `"real_order_routing": "NOT_IMPLEMENTED"` and `"live_trading": false`
(verified). There is no live order path in the repository.

Additional guard: `UpstoxSandboxBroker.__init__` (lines 197–201) logs `SANDBOX_BASE_URL_REVIEW`
if `base_url` looks live. **Treat that log line as a stop-everything signal.**

Belt-and-braces checklist before any start:

```powershell
python -c "from app.config.settings import Settings as S; s=S.from_env(); print('mode',s.mode,'live',s.live_trading,'broker',s.broker,'base',s.upstox_sandbox_base_url)"
```
Expected: `mode PAPER live False broker UPSTOX_SANDBOX base https://api-hft.upstox.com`.

---

## 18. Known limitations and unsupported capabilities

Everything below was read out of the source. These are **properties of the implementation**, not
opinions.

### 18.1 Real-money trading does not exist

- `Settings.__post_init__` rejects `MODE != PAPER`, `LIVE_TRADING == true`, and `BROKER == LIVE`
  (verified, exact messages in §6.3).
- `GET /health` hard-codes `"real_order_routing": "NOT_IMPLEMENTED"`
  (`app/api/routes/health.py:12`).
- `app/api/routes/v2.py` docstring: *"no endpoint can place orders or change risk limits."*
- `app/__main__.py` docstring: *"There is no command that can place a real order."*
- The only real order path is `UpstoxSandboxBroker`, which talks exclusively to the configured
  sandbox base URL with `UPSTOX_SANDBOX_ACCESS_TOKEN`.

**You cannot troubleshoot "live trading is broken" — there is nothing to break.**

### 18.2 SQLite only

`Settings.__post_init__` raises `Only SQLite is supported` for any other URL, and
`Database.__init__` repeats the check. There is no Postgres/MySQL path, no connection pool beyond
a single `sqlite3` connection guarded by an `RLock`, and no multi-host deployment story.

### 18.3 Single-process writer

`Database.transaction()` serializes on an `RLock`, and `Repository.release_writer()` is called at
API shutdown. Running the engine and the API server against the same database file from separate
processes relies on SQLite's own locking with `timeout=10` — expect `database is locked` under
contention, not corruption.

### 18.4 Order history retention is one trading day

Documented in `app/brokers/sandbox.py` docstring line 16. Reconciliation performed later finds
nothing. This is a provider constraint the code documents honestly; it is not a bug.

### 18.5 Writes never retry

`_request()` sets `attempts = 1` for POST/PUT/DELETE by design (duplicate-order safety). A
transient network error on `POST /v3/order/place` will surface as
`SandboxRequestError("Sandbox API unreachable (network error)")` and leave the order ambiguous —
resolved by `SubmissionJournal`, not by retrying.

### 18.6 The broker latches closed on auth failure

After one 401/403, `available = False` for the lifetime of the `UpstoxSandboxBroker` object.
There is **no automatic re-authentication**. Recovery requires constructing a new broker — in
practice, restarting the engine process.

### 18.7 `python -m app` does not write a log file

Only `run.py:152` passes a path to `configure()`. `app/__main__.py` has no such call (verified by
grep). Operational runs log to stderr only unless you redirect.

### 18.8 The database path is relative

`sqlite:///trading_bot.db` resolves against the **cwd**. There is no absolute-path default and no
`DATA_DIR`-relative resolution for the database (unlike `settings.data_dir`, which is used for
`data/`, `history/`, `universe/`, `datasets/`).

### 18.9 Protobuf decode failures are silent by design

`DecodeError` is caught and logged as `UPSTOX_DECODE_ERROR`; the frame is dropped and the stream
continues (`app/data/upstox_stream.py:117`). There is no circuit-breaker that stops the stream
after N consecutive decode failures, so a systematic schema mismatch looks like "no candles"
rather than an error.

### 18.10 Stale feeds suppress candles rather than erroring

`LiveCandleBuilder.stale()` gates emission (checked at `app/data/upstox_stream.py:538`); the loop logs
`feed_stale` and emits nothing. There is no alert raised to the API surface for staleness alone.

### 18.11 AXIOM has no independent trading capability

`desktop/api_client.py` exposes exactly two mutating calls — `emergency_stop()` and `resume()` —
both of which only toggle a persisted flag server-side. `desktop/app.py:272` states the app is
*"a viewer + emergency stop only"*. There is no order entry, no risk override and no strategy
editing in the desktop client.

### 18.12 `POST /engine/stop` against a bare API server cancels nothing at the broker

`app.state.sandbox_engine` is `None` unless the engine is embedded
(`app/api/server.py:92`). The route still persists the flag — which correctly prevents a
*subsequent* engine start — but it cannot cancel orders belonging to a separately running engine
process. See §13.7.

### 18.13 No `X-Algo-Name` header is sent by default

The `sandbox.py` docstring (line 21) notes order placement *"may require an exchange-approved
algo name header (X-Algo-Name, optional)"*. `_headers()` (line 230) is the only header builder;
if your sandbox app requires it, this is a configuration gap to confirm rather than assume.

### 18.14 Unverified in the environment that produced this guide

Restating §1.2 because it bears on troubleshooting trust:

- No live Upstox request of any kind was made — no auth, order, cancel, modify, history or
  WebSocket response was ever observed here.
- No Windows 11 / PowerShell command was executed.
- No PySide6 window was created (no display, no `libGL.so.1`).
- `tools/build_windows_executables.py` was never run; no `.exe` exists to inspect.
- The API was exercised in-process via `TestClient`, not through a bound socket, so proxy,
  firewall and TLS behaviour are unverified.

**If your diagnosis depends on any of these, verify it yourself on the target machine and update
this section.**
