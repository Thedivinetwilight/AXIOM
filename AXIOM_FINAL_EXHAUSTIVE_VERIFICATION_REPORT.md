# AXIOM — Final Exhaustive Verification Report

**Date:** 2026-10-19 (Asia/Calcutta)
**Repository:** `Thedivinetwilight/AXIOM`
**Branch:** `arena/107862ec-axiom`
**Base commit:** `4317e3a9f130db3f7fd03db53e00bdf66ac79e9f` (single-commit squashed history)
**Verified code commit:** `002da1198312a2def2d31a6368729ff91da59d78`
**Documentation commit:** the commit containing this file (docs/reports only)
**Environment:** Debian 12 container, Linux 6.1.158 x86_64 (glibc 2.36), CPython
3.11.2, Node 22.22.3, PyInstaller 6.22.3, no pandas/numpy/torch, no Upstox
credentials, restricted egress (GitHub/npm/PyPI only).

Status vocabulary used below: **PASS / FAIL / FIXED / PARTIAL / BLOCKED /
UNVERIFIED / NOT IMPLEMENTED / NOT APPLICABLE**.

---

## 1. Bottom line

| Tier | Verdict | Basis |
|---|---|---|
| **Software contract** (source tree) | **PASS** | 1926 passed, 39 skipped, 0 failed, 160.40 s, 1965 collected tests in 79 modules. Final run at the verified code commit `002da11` with the operator database untouched. |
| **Packaged application** | **PARTIAL / UNVERIFIED** | The one-file build cannot be produced in this container (no shared `libpython3.11`, no egress for a private interpreter). CI is configured to build and run the frozen end-to-end gate on Ubuntu, Windows and macOS; the commit-matched run is CI run [#39262011293](https://github.com/Thedivinetwilight/AXIOM/actions/runs/39262011293) (status recorded in the completion message). No local artifact was tested, so no frozen-binary claim is made. |
| **Real external integration** | **BLOCKED** | Upstox endpoints (`api.upstox.com`, `assets.upstox.com`) refuse TLS from this sandbox (`SSL_ERROR_SYSCALL`, 2026-10-19T13:33:38Z); no credentials exist. No real OAuth, live/sandbox order, real market feed, real historical download or Kronos weights were exercised. |

The campaign repaired eight defects, each with a captured failing reproduction,
a stated root cause, a regression test that failed before the fix, a narrow
change, and a focused plus full-suite rerun. No test was skipped, deleted or
weakened to obtain a green run; six regressions introduced by the session's own
first-cut edits were caught by the full suite and fixed in code.

## 2. Test totals and durations

```text
Full suite (final):        1926 passed, 39 skipped, 0 failed
                           160.40 s (2:40), 79 test modules, 1965 collected
Focused selections:        startup/diagnostics 12 passed (6.27 s)
                           database paths 9 passed (0.04 s)
                           wizard safety + wizard flow 10 passed (3.92 s)
                           wizard/startup group 57 passed (15.30 s)
                           Upstox modules 177 passed, 3 skipped (12.82 s)
                           Upstox live module 22 passed (3.42 s)
                           history transport 6 passed (0.64 s)
                           operator-DB-adjacent modules 95 passed (6.56 s)
Baseline before work:      1886 passed, 39 skipped, 149.22 s
```

The 39 skips, verbatim from the JUnit report:

- 5 × `pandas` absent, 1 × `numpy` absent, 1 × `torch`/CUDA absent;
- 26 × no Kronos checkout (`AXIOM_KRONOS_SOURCE`) or no genuine pretrained
  weights — the tests refuse to substitute random weights;
- 1 × `test_live_public_file_schema`: public Upstox instrument file unreachable
  (TLS closed);
- 2 × frozen-E2E tests: no built executable in this environment.

A skip is **not** counted as a pass anywhere in this report.

## 3. Defects repaired

Each row is a defect with a failing reproduction captured **before** the fix.

### D-1 — Refused startup printed a traceback and had no field-level diagnosis
- **Repro:** disposable home; run 1 starts paper replay engine and exits 0;
  change `DEFAULT_TIMEFRAME` in `.env`; run 2 on base commit `4317e3a`:
  `ValueError: Saved paper account config differs; use a new database` behind
  12 nested `fastapi/routing.py merged_lifespan` frames, exit 3.
- **Root cause:** the comparison lived inside `PaperBroker.__init__`, i.e. after
  the server had claimed the database and inside uvicorn's lifespan; the error
  carried no field, no recovery path, and was indistinguishable from a crash.
- **Fix:** a read-only preflight (`app/paper_trading/compatibility.py`) runs
  before `create_app`/`server.run()`; `arena.StartupRefusal` prints a named
  block and returns exit 3 (config) or 4 (storage). Recovery is printed and
  never executed automatically.
- **Regression:** `tests/test_startup_paper_account.py` (12 tests).
- **Rerun:** 12 passed; live repro shows `DEFAULT_TIMEFRAME: saved '1D',
  configured '5m'`, no traceback, database SHA-256 and size identical.

### D-2 — A refused start could modify the database
- **Repro:** on base commit, the failed startup opened the SQLite file
  read-write, ran `executescript(SCHEMA)`/migrations and left `-wal`/`-shm`.
- **Root cause:** preflight checks ran after normal database initialization.
- **Fix:** preflight opens `mode=ro` (+ `immutable=1` when no pending WAL) and
  `quick_check`; all refusals are raised before the server claims the file.
- **Regression:** same file, including a corrupt-database case that asserts the
  corrupt file is unmodified.
- **Rerun:** sha and byte size unchanged across refused starts.

### D-3 — No non-destructive way to recover from a saved-account mismatch
- **Repro:** the only documented options were "delete the database" or "edit
  `.env`", both lossy or opaque.
- **Root cause:** no recovery command existed.
- **Fix:** `python arena.py --new-paper-account` creates a timestamped sibling
  database, re-verifies the original SHA-256, updates only `DATABASE_URL`
  (atomic `.env` write) and reports the proof.
- **Regression:** `test_new_paper_account_preserves_original_and_starts`,
  `test_recovery_refuses_when_database_is_only_in_the_process_environment`.
- **Rerun:** original sha `19bcd560…` unchanged; next launch `/health` 200.

### D-4 — Diagnostics were not truthful or usable on a first run
- **Repro:** `arena.py --diagnostics` exited non-zero for "no `.env` yet", and
  a corrupt database produced no readable status.
- **Root cause:** informational findings were treated as failures; the report
  had no account-compatibility section.
- **Fix:** always emit JSON and exit 0 for informational states (1 only when
  the configuration cannot be loaded); add `paper_account.status ∈
  {NO_ACCOUNT, MATCH, MISMATCH, UNREADABLE}` and `health`; never print a token.
- **Regression:** `test_reports_paths_mode_and_never_a_token`,
  `test_reports_a_mismatch_without_refusing_to_diagnose`,
  `test_invalid_configuration_still_prints_structured_diagnostics`.
- **Rerun:** corrupt DB → exit 0, `UNREADABLE`, "SQLite integrity check failed:
  file is not a database", file unchanged; `DEFAULT_TIMEFRAME=7m` → exit 1.

### D-5 — Wizard wrote `.env` before validating, and rollback was lossy
- **Repro:** with the broker rebuild forced to fail, the old code wrote `.env`,
  closed the running broker, then returned 409; replaying touched keys left a
  stray `.env` (a single newline) in a fresh home — "nothing was saved" was
  false. Both new tests failed on the old rollback.
- **Root cause:** validate-after-write ordering plus key-level rollback instead
  of byte-level restore.
- **Fix:** `_proposed_settings` → `_refuse_incompatible_paper` before any write;
  snapshot the exact `.env` bytes and `restore_snapshot()` them (delete if the
  file did not exist); `_rebuild_engine_for` reopens the previous broker.
- **Regression:** `tests/test_wizard_paper_safety.py` (2 tests, both failed
  pre-fix).
- **Rerun:** 409 message contains "Nothing was saved", `.env` absent/identical,
  a reopened broker is installed, settings unchanged; unrestorable case says
  "could NOT be restored".

### D-6 — Upstox OAuth login ignored a valid `.env` in builds without python-dotenv
- **Repro:** `sys.modules["dotenv"]=None` on base code → 4 of 5 new tests failed
  with "Set UPSTOX_CLIENT_ID ... first" although `.env` contained all three
  values.
- **Root cause:** optional dependency imported inside `try/except ImportError:
  pass`; a packaged build silently skipped the file. The flow also mutated
  `os.environ` and could leave a stale token key.
- **Fix:** parse with Arena's own `.env` reader, pass values explicitly
  (exported variables still win), no process-environment mutation; refresh all
  configured token keys via one atomic write.
- **Regression:** `tests/test_upstox_login_env.py` (5),
  `test_store_token_updates_every_configured_token_key` (failed on old code).
- **Rerun:** all pass; old token value absent from the file.

### D-7 — Every intraday historical sync raised `AttributeError`
- **Repro:** `_get_candles(..., intraday=True)` on base code →
  `'Settings' object has no attribute 'intraday_base'`, before any HTTP request.
- **Root cause:** the field is `intraday_base_url`; the wrong attribute name made
  the whole intraday-history path dead.
- **Fix:** use `settings.intraday_base_url`; no other change.
- **Regression:** `tests/test_v2_history_transport.py` (6; 2 failed pre-fix).
- **Rerun:** asserted exact URL
  `https://api.upstox.com/v3/historical-candle/intraday/<key>/minutes/5`, bearer
  header present, missing token blocks without a request.

### D-8 — One 429/5xx at authorize stopped the feed for the whole session
- **Repro:** 503 from the mock authorize endpoint on base code → permanent
  `UpstoxAuthError`, `AUTH_FAILED`, no retry.
- **Root cause:** every non-200 was treated as a credential verdict.
- **Fix:** `UpstoxTransientError` for 429 (`RATE_LIMITED`) and 5xx
  (`UPSTOX_UNAVAILABLE`) with bounded exponential backoff and a 25-failure cap
  → `UNAVAILABLE`; 401/403/400 stay permanent; malformed 200 and untrusted
  schemes fail closed without retry (a possible interception must not be
  retried into acceptance).
- **Regression:** `test_authorize_http_429_is_transient_and_retried`,
  `test_authorize_http_500_is_transient_not_auth_failure`,
  `test_authorize_http_400_is_permanent_and_not_retried`,
  `test_a_successful_authorize_clears_the_previous_failure_kind`.
- **Rerun:** 22/22 in the module.

### Regressions introduced by this session and caught before release
The first post-repair full run failed six tests. All six were caused by this
session's edits, and all were fixed in code (no assertion relaxed):

1. three pre-existing verdict names (`http_429`, `http_5xx`, 400) in
   `verify_feed_credential` had been renamed — restored to the documented
   contract (also relied on by `packaging/frozen_e2e.py`);
2. malformed authorize bodies had been made retryable — restored to permanent
   `AUTH_FAILED`, matching the pre-existing contract;
3. a settings-constructor rejection of `nse-session + 1D` duplicated the engine
   preflight and broke two live-ops tests — removed; the preflight keeps the
   actionable message;
4. a silent-swallow scanner flagged the old `except ImportError: pass` — fixed
   by removing the optional dependency entirely;
5. the console runner and a live-preflight test were opening the operator's
   real database — both pinned to disposable homes (found with a per-test
   database fingerprint plugin);
6. ambient `DATABASE_URL` in wizard/frontend/history fixtures — pinned.

## 4. Startup verification (end-to-end, disposable home)

```text
first run    /health 200; start-default 200; 122 bars processed; stop 200; exit 0
config edit  DEFAULT_TIMEFRAME 1m -> 5m
second run   exit 3
             "Saved paper account configuration does not match ...:
              DEFAULT_TIMEFRAME: saved '1m', configured '5m'"
             no traceback; db sha256 and size identical; .env unchanged
recovery     --new-paper-account -> ok, original_sha256 recorded,
             verified_original_unchanged true, only DATABASE_URL rewritten
after        diagnostics NO_ACCOUNT + health.ok true; /health 200; clean shutdown
```

Exit-code taxonomy: `0` ok, `1` unexpected/unloadable configuration,
`3` configuration refusal, `4` storage refusal (corrupt/read-only/locked/not a
file). Every refusal ends with "Nothing was changed, no data was deleted, no
order was placed."

## 5. Upstox and connectivity

Verified in source and tests: authorize probe classification, transient vs
permanent handling, bounded retries, `connection_state()`, protobuf decoding
against a real loopback WebSocket server, reconnect/heartbeat/duplicate
handling, stale detection, historical chunking/resume/dedup and the documented
URL shapes (including the repaired intraday endpoint), host pinning for
sandbox/live/read endpoints, OAuth state verification, token-key refresh.

Not verified (BLOCKED): a real Upstox session, a real historical download, a
real OAuth token exchange, a real sandbox order. Requests to
`api.upstox.com`/`assets.upstox.com` fail at TLS from this sandbox, and no
credentials exist. A sandbox token is never treated as evidence of live
connectivity.

## 6. Live execution boundary

Verified as software contracts: the four-switch opt-in, session state machine,
separate ARM/ACTIVATE, confirmation phrase, readiness/supervisor/lease checks,
reconciliation, submission-journal durability, canary reservation, rate
limiting, emergency stop and clear, guard chain, final-boundary revalidation
and refusal of direct broker mutations without a gateway capability. Exits and
risk reduction remain independently authorized.

Not verified (BLOCKED): any order against a real account. Not attempted by
design; the mission forbids it.

## 7. Packaging and CI

`python build_exe.py --test` fails before packaging:

```text
ERROR: Python shared library ('libpython3.11.so.1.0') was not found!
```

The container's interpreter is not built with a shared library, `libpython3.11`
is not available offline, the `python3.11-dev` headers are absent, and the
GitHub release asset (a standalone CPython) is outside the allowed egress.
Consequence: `dist/Arena` does not exist, and the two frozen-E2E tests are
skipped locally — reported as UNVERIFIED, never as PASS.

The authoritative gate is `.github/workflows/build-executable.yml`: full source
suite, packaging tests, full suite per OS, `build_exe.py --test` (frozen E2E on
the actual artifact), build-identity assertion against `github.sha`, and
artifact upload. A push-triggered run for the verified commit is
[#39262011293](https://github.com/Thedivinetwilight/AXIOM/actions/runs/39262011293);
only the Ubuntu source-suite job runs on a push, and it **completed
successfully** ([run summary](https://github.com/Thedivinetwilight/AXIOM/actions/runs/39262011293)).
The three-OS packaging matrix requires `workflow_dispatch` or a tag; the
dispatch was attempted and rejected (`HTTP 403: Resource not accessible by
integration` — no `actions:write` on the session token), so it was **not**
executed. No artifact record exists for this commit and none is claimed.

## 8. Repository and data safety

- The operator's untracked `trading_bot.db` (one `paper` row, 0 orders, 0
  trades) was moved out before any experiment and kept at
  `/home/user/axiom_operator_backup/` during the work; SHA-256
  `86ce6fdc087053249cba57d21367b7d90a8c1e05f531fe3291ee5897d328cc89`.
- Final full run monitored with a per-test fingerprint plugin: **0 touches**.
- Two pre-existing tests that did open the operator database were pinned to
  disposable homes; that is a test-suite fix, not a production behaviour change.
- No `.env`, token, OAuth code, secret, account number or database is tracked;
  `git diff --check` clean; nothing was force-pushed, reset or cleaned.
- No real-money order, modification or cancellation was ever submitted.

## 9. Deliverables

| File | Content |
|---|---|
| `AXIOM_FINAL_EXHAUSTIVE_VERIFICATION_REPORT.md` | this report |
| `AXIOM_FINAL_FEATURE_TEST_MATRIX.csv` | 95 features with status and evidence, classified by verification tier |
| `AXIOM_FINAL_RUN_LOG.md` | chronological commands, outputs, durations, failures, blocked steps |
| `AXIOM_FINAL_KNOWN_ISSUES.md` | every known limitation with its status |
| `AXIOM_CURRENT_AUDIT_REPORT.md` | rewritten current-commit evidence index |
| `README.md`, `AXIOM_PRODUCT_MANUAL.md`, `USER_MANUAL.md`, `LIVE_OPERATIONS.md`, `LIVE_TRADING_ENABLEMENT.md`, `UPSTOX_SANDBOX_AND_MARKETDATA_GUIDE.md` | updated to 3.2.4 with superseded claims marked |
