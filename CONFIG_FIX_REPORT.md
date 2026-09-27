# CONFIGURATION FIX REPORT — Arena 3.0.2

**Scope:** complete audit and fix of the configuration system (task: "STOP
PATCHING ONE FIELD AT A TIME"). Documentation/report file only — trading,
risk, broker, strategy and execution behavior untouched.

---

## 1. FILES CHANGED (exactly 2 + this report)

| File | Change |
|---|---|
| `app/config/settings.py` | ① New module-level `coerce_env_value(name, annotation, raw)` — the single conversion function, driven by the **declared field annotation**; ② new `_unwrap_optional()` helper (`X \| None` handling); ③ `Settings.from_env()` rewritten to iterate `dataclasses.fields(Settings)` + `typing.get_type_hints` and coerce **every** field — env-provided values AND defaults — by annotation; ④ type audit completion: `initial_capital` default literal `100_000` → `100_000.0` (float annotation must not hold an int; the parser no longer depends on default literals either way) |
| `tests/test_settings_parser.py` | NEW — 41 regression tests (see §3) |

No other file modified. Repo-wide search confirmed exactly ONE settings
class (`app/config/settings.py::Settings`) and ONE env parser
(`Settings.from_env`); no duplicated config parsers exist.

## 2. EXACT ROOT CAUSE

`Settings.from_env()` converted env values by branching on the **runtime
type of the DEFAULT value**:

```python
default = getattr(defaults, name)
if isinstance(default, bool):    # bool parse
elif isinstance(default, float): # float parse
else: values[name] = raw         # ← raw STRING passthrough
```

Consequences (the two observed crashes + latent siblings):

- `initial_capital: float = 100_000` — float **annotation**, int **default**
  → env string `"100000"` fell into the `else` branch → raw str reached
  `ExecutionConfig(**execution_kwargs())` →
  `TypeError: must be real number, not str` (dashboard boot failure #1).
- `MAX_OPEN_POSITIONS` / `MAX_DAILY_LOSS` / `MAX_TRADES_PER_DAY` /
  `MAINTENANCE_HOUR` / `MAX_DOWNLOAD_RETRIES` / `RESEARCH_BATCH_SIZE` /
  `SCANNER_TOP_K` / `DISCOVERY_MAX_CANDIDATES` — **int** annotations with
  int defaults → env strings passed through → `__post_init__`'s
  `getattr(name) < 0` → `TypeError: '<' not supported between instances of
  'str' and 'int'` (failure #2 and would recur for the others).
- Bool/Optional parsing worked only because their defaults happened to be
  `False`/float; the parser was never annotation-driven.

**Parser audit (repo-wide):** all other `os.getenv`/`os.environ` reads are
string-typed by design and correct — `UPSTOX_SANDBOX_ACCESS_TOKEN`
(`app/__main__.py`), `UPSTOX_CLIENT_ID/SECRET/REDIRECT_URI`
(`app/data/upstox_auth.py`), `ARENA_BACKEND_URL`/`ARENA_API_TOKEN`
(`desktop/app.py`). **No numeric setting is read outside the parser.**
`run.py`, `app/main.py`, `app/api/server.py`, `app/__main__.py` all consume
the fixed `Settings.from_env()`.

## 3. TESTS ADDED (41 — tests/test_settings_parser.py)

- **TestRequiredMatrix (15)** — the exact required matrix:
  `INITIAL_CAPITAL=100000` → float `100000.0`;
  `MAX_OPEN_POSITIONS=5` → int `5`; `MAX_TRADES_PER_DAY=10` → int `10`;
  `LIVE_TRADING=false` → `False`; `LIVE_TRADING=true` → parser converts to
  `True` (unit) and `Settings` validation rejects it (integration — layers
  separated deliberately); `POSITION_SIZE=0.25` → float; `STOP_LOSS=0.05` →
  float; `STOP_LOSS=none` → None; `TAKE_PROFIT=none` → None;
  `MODE=PAPER` → str; `BROKER=LOCAL_PAPER` valid;
  `BROKER=UPSTOX_SANDBOX` without token → the exact validation error;
  `BROKER=LIVE` (and lowercase `live`) → rejected.
- **TestMalformedValuesAreCleanValueErrors (7)** — `INITIAL_CAPITAL=abc`,
  `MAX_OPEN_POSITIONS=abc`, `MAX_TRADES_PER_DAY=3.5`, `POSITION_SIZE=abc`,
  `STOP_LOSS=abc`, `LIVE_TRADING=maybe`, `MAINTENANCE_HOUR=abc` → clean,
  field-named `ValueError`s at parse time (never TypeErrors in unrelated
  modules).
- **TestEveryFieldMatchesDeclaration (3)** — every field of both
  `Settings.from_env()` and the plain `Settings()` constructor asserted
  against its declared annotation (bool/int/float/str), plus an
  all-scalar-env-set integration sweep.
- **TestAliases (4)** — `UPSTOX_ACCESS_TOKEN`, `UPSTOX_INSTRUMENT_KEYS`
  aliases resolve; explicit `MARKET_DATA_*` wins; `field(default=...)`
  secrets stay strings.
- **TestCoerceEnvValueContract (7)** — case-insensitive bools, invalid bool,
  negative/zero ints, `none` accepted ONLY for Optional[float], non-optional
  float rejects `"none"`, string passthrough, programmatic int default
  normalized under float annotation.
- **TestSafetyUnchanged (5)** — `MODE≠PAPER` rejected, non-SQLite rejected,
  negative limit rejected, `SANDBOX_PRODUCT` validation, invalid instrument
  key for `DATA_SOURCE=upstox` — all intact.

## 4. FULL TEST RESULT (executed, not assumed)

- `pytest -q` full suite: **445 collected, 445 passed, 0 failed, 0 skipped**
  (404 carried + 41 new; Python 3.13 venv; run twice — clean both times).
- Configuration-specific tests: 41/41 passed.
- `compileall app desktop tests`: exit 0.
- Fresh virtualenv: `pip install -e .` → parser verified converting
  `100000`→`100000.0` (float) and `5`→`5` (int); `import app` → 3.0.2.
- `paper-test`: `"ok": true`, final equity `99637.58463385397` (exact
  invariant, unchanged).
- CLI: `--version` → `Arena 3.0.2`; `--help` → all 22 commands intact.

## 5. DASHBOARD STARTUP RESULT

Booted with the exact failing configuration from the bug report
(`INITIAL_CAPITAL=100000, MAX_OPEN_POSITIONS=5, MAX_TRADES_PER_DAY=10,
POSITION_SIZE=0.25, STOP_LOSS=0.05, MAX_DAILY_LOSS=500, API_TOKEN=…`):

- `uvicorn app.main:create_app --factory` → started, no TypeError.
- `GET /health` → 200
  `{"status":"ok","version":"3.0.2","mode":"PAPER","live_trading":false,"real_order_routing":"NOT_IMPLEMENTED"}`.
- `GET /dashboard` → 200 (authed); `GET /api/v2/overview` → 200 with
  **`"capital_initial": 100000.0`** (float from the string env value).
- Unauthenticated `/api/v2/overview` → 401 (auth intact).
- Server stopped cleanly afterwards; scratch `.env` removed (verified absent).
- `BROKER=UPSTOX_SANDBOX` without a token still fails boot with the
  intentional, clean `ValueError` — validation preserved.

## 6. REMAINING KNOWN LIMITATIONS

1. `MARKET_TIMEZONE` (real setting, default `Asia/Kolkata`) is still absent
   from `.env.example` — pre-existing documentation gap, previously reported;
   untouched here to keep this fix minimal.
2. Integer parsing is strict by design: `MAX_TRADES_PER_DAY=3.5` → clean
   `ValueError` (tested). Accepting `3.5` would silently truncate — refused.
3. `.env` parsing still requires python-dotenv **or** exported env vars
   (unchanged behavior; documented in `.env.example`).
4. Booleans accept only `true`/`false` (not `1/0/yes/no`) — unchanged,
   documented, and now tested.

## 7. SAFETY VERIFICATION (nothing weakened)

`MODE=PAPER`-only, `LIVE_TRADING=false`-only, `BROKER=LIVE` rejected
(both cases), sandbox-token requirement, SQLite-only, non-negative risk
limits, `SANDBOX_PRODUCT` enum, instrument-key regex for `DATA_SOURCE=upstox`
— all verified intact by the dedicated `TestSafetyUnchanged` tests and by
the live boot checks above.
