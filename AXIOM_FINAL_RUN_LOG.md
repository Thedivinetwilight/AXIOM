# AXIOM Final Run Log

**Date:** 2026-10-19 (Asia/Calcutta) · **Branch:** `arena/107862ec-axiom`
**Base commit:** `4317e3a9f130db3f7fd03db53e00bdf66ac79e9f`
**Environment:** Debian 12 container, Linux 6.1.158 x86_64 (glibc 2.36), Python 3.11.2
(`/home/user/.venv-axiom`), Node 22.22.3, PyInstaller 6.22.3 (installed during the
session), no pandas/numpy/torch, no Upstox credentials, restricted outbound network.

This log is chronological and records commands, observed output and durations.
It deliberately includes failed and blocked steps. Nothing was run against a real
broker account, and no real-money order was placed.

---

## 1. Baseline (unmodified tree)

```text
$ /home/user/.venv-axiom/bin/python -m pytest -q
1886 passed, 39 skipped, 1 warning in 149.22s
```

Collection at baseline: 1925 tests. Focused Upstox subset
(`pytest -q tests/ -k "upstox or market_data"`): **71 passed, 9.01 s**.

Existing skipped tests were inspected with `-rs`: optional `pandas`/`numpy`/`torch`,
absent Kronos checkout/weights, one public Upstox instrument-file probe, and the two
frozen-artifact tests (no local executable). None was converted into a pass.

## 2. Startup defect — failing reproduction (pre-fix)

Disposable home (`/tmp/repro_startup*.py`), `MODE=PAPER`, `BROKER=LOCAL_PAPER`,
`DATA_SOURCE=replay`; run 1 started the paper engine, processed replay bars and
stopped; then `DEFAULT_TIMEFRAME` was changed in `.env` and the launcher restarted.

Pre-fix worktree (`git worktree add /tmp/axiom-prefix 4317e3a`), second launch:

```text
second run exit: 3
...
File "/tmp/axiom-prefix/app/api/server.py", line 47, in lifespan
  broker = PaperBroker(
File "/tmp/axiom-prefix/app/paper_trading/broker.py", line 88, in __init__
  raise ValueError("Saved paper account config differs; use a new database")
ValueError: Saved paper account config differs; use a new database
ERROR:    Application startup failed. Exiting.
```

Observed problems: 12 nested `fastapi/routing.py merged_lifespan` frames instead of a
diagnosis, no field name, no recovery path, exit status indistinguishable from any
other uvicorn failure. Earlier probes also showed the failed start opening the file
read-write and creating `-wal/-shm` side files.

## 3. Startup repair — post-fix reproduction

Same scenario, repaired tree:

```text
second run exit: 3
  Saved paper account configuration does not match the current configuration:
    DEFAULT_TIMEFRAME: saved '1D', configured '5m'
  The database at /tmp/.../trading_bot.db already contains a paper account. ...
  To keep this account: set DEFAULT_TIMEFRAME back to the saved value ...
      python arena.py --new-paper-account
  Nothing was changed, no data was deleted, no order was placed.
db unchanged: True
db sha256 before = after = 19bcd560…f259347 (626,688 bytes)
env unchanged: True
stderr has traceback: False
```

Recovery on the same home:

```json
{
  "ok": true,
  "original_database": "…/trading_bot.db",
  "original_sha256": "19bcd560…f259347",
  "new_database": "…/trading_bot.recovered-20261019T123829Z.db",
  "env_updated": ["DATABASE_URL"],
  "verified_original_unchanged": true
}
```

Then `arena.py --diagnostics` → `paper_account.status = NO_ACCOUNT`,
`health.ok = true`; the launcher served `/health` 200 and shut down cleanly.
Regression suite `tests/test_startup_paper_account.py`: **12 passed in 6.27 s**
(first execution: 10 passed / 2 failed; the two expectations were corrected against
actual launcher output, not by weakening the assertions).

## 4. Upstox defect arc

### 4.1 OAuth login ignored a valid `.env`

Root cause: `run_login_flow` imported `python-dotenv` inside `try/except ImportError:
pass`. In a bundle without that optional package the file was skipped and the operator
was told to "Set UPSTOX_CLIENT_ID ... first".

Regression test `tests/test_upstox_login_env.py` written first; with the pre-fix
implementation and `sys.modules["dotenv"]=None`:

```text
4 failed, 1 passed in 0.21s
```

After the repair (Arena's own `.env` parser, values passed explicitly, no process
environment mutation): **5 passed in 0.12 s**.

### 4.2 Intraday history raised `AttributeError`

`HistoricalDataManager._get_candles(..., intraday=True)` read
`settings.intraday_base`, which does not exist (the field is `intraday_base_url`), so
every intraday sync died before any HTTP request.

`tests/test_v2_history_transport.py` fails first on the old line:

```text
AttributeError: 'types.SimpleNamespace' object has no attribute 'intraday_base'
2 failed, 4 passed
```

After the fix: **6 passed in 0.64 s**, including the exact documented URL
`…/v3/historical-candle/intraday/NSE_EQ|INE002A01018/minutes/5`.

### 4.3 One 429/5xx at authorize stopped the feed for the session

Every non-200 authorize was classified as a permanent `UpstoxAuthError`. The repair
adds `UpstoxTransientError` (429 → `RATE_LIMITED`, 5xx → `UPSTOX_UNAVAILABLE`), bounded
backoff and a 25-failure cap → `UNAVAILABLE`. Malformed 200 / untrusted scheme remain
permanent (`MALFORMED_RESPONSE`, `UNTRUSTED_ENDPOINT`) so a proxy cannot be retried
into acceptance.

New tests in `tests/test_upstox_live.py::test_authorize_http_*`:
**22 passed in 3.42 s** for the whole module. A full-suite rerun exposed that three
pre-existing verdict names/tests (`http_429`, `http_500`, malformed-200 → AUTH_FAILED)
had been changed by the first implementation; those contracts were restored, and
`packaging/frozen_e2e.py` (which expects `(http_429)`/`(http_5xx)`) was used as the
cross-check.

### 4.4 Real Upstox network — blocked

```text
$ curl -sS https://api.upstox.com/v3/feed/market-data-feed/authorize
curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL in connection to api.upstox.com:443
$ curl -sS https://assets.upstox.com/.../complete.json.gz
curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL in connection to assets.upstox.com:443
```

DNS resolves; TLS is refused by sandbox egress. `tests/test_v2_universe.py:81` skips for
the same reason. No credential exists in this environment. Real connectivity is
reported **BLOCKED**, not passed.

## 5. Test-suite hygiene: the operator database

The repository in this session contained an untracked `trading_bot.db` (one `paper`
state row, 0 orders/trades). It was moved out before any experiment
(`/home/user/axiom_operator_backup/trading_bot.db`, SHA-256
`86ce6fdc…28cc89`, 618,496 bytes, `PRAGMA integrity_check=ok`).

A full run with a temporary `pytest` plugin that fingerprints the file before/after
each test found two leaks:

```text
tests/test_feature_console.py::TestRunningCommands::test_timeout_kills_the_command_and_says_so
tests/test_live_hardening_v2.py::TestCliGates::test_preflight_classifies_a_good_credential
```

The console test spawned the real CLI with the repository as `ARENA_HOME`; the
live-preflight test recorded `live_token_verified` in the ambient database. Both were
pinned to disposable homes/databases. The final full run reports:

```text
operator DB touches: none
operator DB sha256: 86ce6fdc…28cc89 (unchanged)
```

## 6. Wizard write-before-validate

New regression `tests/test_wizard_paper_safety.py` simulates a broker rebuild failure
after the `.env` write. Pre-fix rollback (replaying only touched keys) left a stray
`.env` (a single newline) → both tests failed. The repair snapshots the exact
pre-request bytes and restores them (or deletes a file that did not exist). Post-fix:
**2 passed**; combined wizard/startup selection **57 passed in 15.30 s**.

## 7. Full-suite runs after the repairs

| Run | Scope | Result | Duration |
|---|---|---|---|
| A | full suite, first post-repair run | **6 failed, 1901 passed, 39 skipped** | 1367.12 s |
| B | full suite after restoring the three changed contracts and pinning operator-DB tests | **1918 passed, 39 skipped** | 203.73 s |
| C | full suite after DB-watch retest | **1918 passed, 39 skipped**, 0 operator-DB touches | 193.39 s |
| D | full suite after the wizard/Upstox regression additions | **1924 passed, 39 skipped** | 191.60 s |
| E | full suite after the final two Upstox token-refresh tests (final) | **1926 passed, 39 skipped** | **160.40 s** |

Run A's six failures were contract regressions introduced by this session's edits
(verdict renames in `verify_feed_credential`, malformed-200 classification, settings
constructor rejecting a calendar/timeframe pair that the engine preflight is specified
to report, and one silent-swallow scan). Each was repaired in the code; no assertion
was relaxed, skipped or deleted. Final collection: **1965 tests** (79 test modules). Runs D and E were also monitored with a
`pytest` plugin that fingerprints the operator database before and after every test:
**0 touches**, SHA-256 unchanged (`86ce6fdc…28cc89`).

## 8. Packaging

```text
$ /home/user/.venv-axiom/bin/python build_exe.py --test
$ python -m PyInstaller packaging/arena.spec --noconfirm ...
ERROR: Python shared library ('libpython3.11.so.1.0') was not found!
```

The container's Python is built without the shared library, `libpython3.11` is absent
from the (offline) apt lists, Python headers/dev package are absent, and GitHub release
assets are outside the allowed egress. A local one-file build is therefore **BLOCKED**
by the environment, not by the project. `dist/` was not produced; the frozen-E2E tests
remain skipped locally and are reported as such.

The authoritative artifact gate is the GitHub workflow
`.github/workflows/build-executable.yml`, which builds and runs `frozen_e2e` on
Ubuntu, Windows and macOS. It is dispatched on this branch after the commit; the run
link and per-OS artifact records are quoted in the completion message because they
necessarily post-date the commit that contains this log.

## 9. Repository state checks

- `git diff --check` and no binary/large artifacts staged.
- No `.env`, token, OAuth code, secret, account number or local database is tracked;
  `.env` and `*.db` are ignored by the existing `.gitignore`.
- The operator database is outside the repository during the work and restored only
  after the final full run, byte-identical (`86ce6fdc…`).
- Existing uncommitted work was preserved; no `reset --hard`, no `git clean`, no
  force-push.
