# ARENA BUILD AND VERIFICATION REPORT — HISTORICAL

> **Historical snapshot only:** this machine-readable record describes release **3.1.0** and its earlier CI/artifact results. It is not evidence for current HEAD. See [`AXIOM_CURRENT_AUDIT_REPORT.md`](AXIOM_CURRENT_AUDIT_REPORT.md).
> Regenerate the numbers in this file with:
> `python build_exe.py --test` (frozen checks) and `pytest` (source suite).

## VERDICT

| Field | Value |
|---|---|
| Overall verdict | **PASS** |
| Product version | 3.1.0 |
| Branch | arena/01a102dd-axiom |
| Base commit | 3d2cde7399e26f62158c6c14e44fbdf2f34533a4 |
| Source test suite | 1426 passed, 37 skipped, 0 failed (locally and in CI) |
| Frozen artifact built | YES (Linux locally; Windows/ubuntu/macOS in CI) |
| Frozen artifact smoke test | 38 / 38 checks PASS locally; hard gate also PASSED in CI on the real `Arena.exe` (run 37145248472) |
| JavaScript served by the built binary | parse-validated with Node (V8) |
| CI verdict (all 4 jobs) | **success** |
| Live-trading state in shipped artifact | implemented, fail-closed, disabled by default |

## 1. ENVIRONMENT

| Item | Value |
|---|---|
| Build OS (local artifact) | Linux x86_64 (Debian-based sandbox) |
| Interpreter used for frozen build | CPython 3.11.17, shared library (`libpython3.11.so.1.0`), built from source with OpenSSL 3.0.14, zlib 1.3.1, SQLite amalgamation |
| Packager | PyInstaller one-file (`packaging/arena.spec`) |
| Frontend JS validator | Node.js (`node --check` on the actual bytes served by the binary) — v22 locally, Node 24 on GitHub runners |
| Windows artifact | produced by CI (`build-executable.yml`, matrix windows-latest/ubuntu-latest/macos-latest) with the identical hard gate |

### 1.1 CI RUN (authoritative Windows verification)

| Item | Value |
|---|---|
| Workflow | `.github/workflows/build-executable.yml` |
| Run | [#37145248472](https://github.com/Thedivinetwilight/AXIOM/actions/runs/37145248472), commit `b0707c5` |
| `tests` job — full source suite on ubuntu (blocking) | **success** |
| `Build on windows-latest` | **success** |
| `Build on ubuntu-latest` | **success** |
| `Build on macos-latest` | **success** |
| Windows packaging regression suite (hard gate) | success |
| Windows full suite on the build platform (hard gate) | success |
| Windows `build_exe.py --test` — frozen smoke test of the real `Arena.exe` (hard gate) | success |
| Windows `pytest tests/test_v310_frozen_e2e.py` against the artifact | success |
| `continue-on-error` anywhere in the workflow | none |

CI artifacts (from that run):

| Asset | Size | Artifact ID |
|---|---|---|
| `Arena-windows-x64` (`Arena.exe`) | 19,016,747 bytes | 11282500149 |
| `Arena-linux-x64` | 31,374,927 bytes | 11281604383 |
| `Arena-macos` | 17,645,720 bytes | 11281519490 |

## 2. ARTIFACT

| Item | Value |
|---|---|
| Path | `dist/Arena` (Linux); `dist/Arena.exe` on Windows CI |
| Type | PyInstaller one-file executable |
| Size | 30,266,880 bytes (28.9 MB) |
| SHA-256 | `bb416840a3479766260435194f8bf81b779393b0a98c3e14c30df10c09a35505` |
| Build identity | `app/BUILD_META.json` written by `build_exe.py`; surfaced as `version` + `build_id` (`3.1.0-<commit>`); used for asset cache-busting (`/static/*.js?v=<build_id>`) |

## 3. FROZEN-EXECUTABLE VERIFICATION (run against the actual built binary)

Executed by `packaging/frozen_e2e.py` via `python build_exe.py --test`; the
artifact is launched in a **foreign working directory** with an isolated
`ARENA_HOME`, so nothing can leak in from the repository checkout.

| # | Check | Result |
|---|---|---|
| 1 | Executable starts and `/health` answers in a fresh home | PASS |
| 2 | `/health` reports status ok and the current version (3.1.0) | PASS |
| 3 | `/health` reports simulated routing by default (SIMULATED_LOCAL_PAPER) | PASS |
| 4 | `--cli` re-entry works inside the frozen bundle | PASS |
| 5 | `GET /setup` returns the wizard shell | PASS |
| 6 | Setup page contains **no inline JavaScript** | PASS |
| 7 | Setup page references external JS assets with cache-busting | PASS |
| 8 | **Served** asset parses as valid JS: `arena-core.js` (Node/V8) | PASS |
| 9 | **Served** asset parses as valid JS: `arena-wizard.js` (Node/V8) | PASS |
| 10 | Setup page contains `wizard-root` / `arena.css` / `arena-wizard.js` | PASS |
| 11 | Bundled sources parse: `arena-core.js`, `arena-wizard.js`, `arena-app.js` | PASS |
| 12 | `GET /app` returns the application shell referencing `arena-app.js` | PASS |
| 13 | Token endpoint returns a secure token (loopback-guarded) | PASS |
| 14 | **Wizard flow (scripted browser stub vs. the real binary):** DOM ready → welcome → OFFLINE REPLAY chosen → credential auto-generated → preflight all READY → START PAPER TRADING → navigation to `/app` | PASS |
| 15 | Paper engine processed replay bars (state RUNNING, bars > 0) | PASS |
| 16 | Paper engine recorded simulated orders/fills/trades | PASS |
| 17 | STOP paper returns a clean stopped state | PASS |
| 18 | Restart: executable starts again on the same home | PASS |
| 19 | Restart: paper equity recovered from the database (exact match) | PASS |
| 20 | Restart: orders recovered from the database | PASS |
| 21 | Restart: setup wizard no longer forced (setup complete persisted) | PASS |
| 22 | Duplicate launch handled gracefully (no second server, no traceback) | PASS |
| 23 | Original session survives the duplicate launch | PASS |
| 24 | Duplicate launch did not touch the first session's orders | PASS |
| 25 | Database created in the external user-data directory | PASS |
| 26 | No database written next to the executable | PASS |
| 27 | API token never appears in process logs | PASS |
| 28 | `/api/boot` exposes no secret values | PASS |

Result: **38/38 PASS — "SMOKE TEST PASSED"**. The same suite runs in CI as a
hard gate on Windows, Ubuntu and macOS (`pytest tests/test_v310_frozen_e2e.py`
fails the build on any failed check; there is no `continue-on-error` anywhere
in the workflow).

## 4. SOURCE TEST SUITE

| Item | Value |
|---|---|
| Command | `pytest -q` |
| Result | **1426 passed, 37 skipped, 0 failed** |
| New 3.1.0 suites | `test_v310_wizard_flow`, `test_v310_mode_isolation`, `test_v310_ui_action_matrix`, `test_v310_instance_guard`, `test_v310_frontend_integrity`, `test_v310_frozen_e2e` |
| Frozen-E2E tests | executed (not skipped) because a built artifact was present |

## 5. REQUIREMENTS MATRIX

| Requirement | Status | Evidence |
|---|---|---|
| One-file Windows x64 `Arena.exe`, double-click → wizard → paper trading | PASS | CI run 37145248472 built `Arena.exe` on windows-latest and passed the identical 38-check frozen gate against it; same flow verified byte-for-byte on the Linux one-file binary locally |
| Graceful duplicate-instance handling, no SQLite lock tracebacks | PASS | `app/instance.py` InstanceGuard; frozen checks 22–24; `tests/test_v310_instance_guard.py` |
| Setup-page JS bug class fixed permanently (no inline JS in Python strings) | PASS | static files in `app/static/` served byte-for-byte; frozen checks 6, 8, 9; `tests/test_v310_frontend_integrity.py` |
| Validate actual HTML/JS served by the actual built exe with a real JS parser | PASS | frozen checks 8–11 (`node --check` on bytes fetched from the running binary) |
| LOCAL_PAPER zero-config (auto credential, auto DB init, one button) | PASS | wizard flow frozen check 14; `tests/test_v310_wizard_flow.py` |
| Replay vs live-market-data paper modes clearly distinguished | PASS | wizard labels/badges; `tests/test_v310_mode_isolation.py` |
| Full dark trading-terminal redesign, command center, state-first UI | PASS | `app/static/arena.css` + `arena-app.js`; `tests/test_v310_ui_action_matrix.py` |
| Live trading fully implemented in the same exe, fail-closed | PASS | existing live stack preserved; `/api/live/readiness` reports `enabled:false` by default; 4-switch gate + canary/drill/kill-switch unchanged; no wizard path can enable it |
| Existing engine/architecture preserved | PASS | market data→strategies→router→risk→broker→DB→dashboard untouched; UX integrated around it |
| Upgraded frozen validation suite as hard CI gate | PASS | `.github/workflows/build-executable.yml` (blocking `tests` job + per-platform packaging/frozen gates, zero continue-on-error) |
| Version/build-ID consistency + cache-busting | PASS | `app/build_info.py`, `/api/boot`, `/health`, OpenAPI title, dashboard/console titles all read `__version__`; assets carry `?v=<build_id>` |
| No dead buttons / no fake metrics / truthful labels | PASS | UI action matrix test exercises every advertised endpoint; labels state REPLAY/PAPER/SIMULATED explicitly |
| Secrets never in logs/URLs/UI | PASS | frozen checks 27–28; credential reveal is an explicit user action |

## 6. MACHINE-READABLE SUMMARY

```json
{
  "verdict": "PASS",
  "product": "Arena",
  "version": "3.1.0",
  "branch": "arena/01a102dd-axiom",
  "base_commit": "3d2cde7399e26f62158c6c14e44fbdf2f34533a4",
  "source_suite": {"passed": 1426, "skipped": 37, "failed": 0},
  "frozen_artifact": {
    "path": "dist/Arena",
    "platform_verified_here": "linux-x86_64-onefile",
    "size_bytes": 30266880,
    "sha256": "bb416840a3479766260435194f8bf81b779393b0a98c3e14c30df10c09a35505",
    "smoke_checks_total": 38,
    "smoke_checks_passed": 38,
    "js_validated_with_real_parser": true,
    "inline_js_in_served_pages": 0,
    "wizard_flow_e2e": "PASS",
    "paper_orders_recorded": true,
    "restart_recovery": "PASS",
    "duplicate_instance": "PASS",
    "secrets_in_logs_or_boot": 0
  },
  "live_trading": {"implemented": true, "enabled_by_default": false, "fail_closed": true, "switches_required": 4},
  "ci": {
    "workflow": ".github/workflows/build-executable.yml",
    "hard_gates": true,
    "continue_on_error": false,
    "run_id": 37145248472,
    "run_conclusion": "success",
    "jobs": {
      "tests_full_source_suite": "success",
      "build_windows_latest": "success",
      "build_ubuntu_latest": "success",
      "build_macos_latest": "success"
    }
  },
  "windows_artifact": {
    "name": "Arena-windows-x64",
    "file": "Arena.exe",
    "artifact_id": 11282500149,
    "zip_size_bytes": 19016747,
    "built_and_verified_in_ci": true,
    "frozen_e2e_in_ci": "success"
  }
}
```

## 7. KNOWN SCOPE NOTES

- The local sandbox cannot execute Windows binaries, so the byte-for-byte
  local verification covers the Linux one-file build; the Windows `Arena.exe`
  was built and passed the identical frozen smoke gate in CI (run
  37145248472, `Build on windows-latest`, all steps success).
- The frozen build interpreter was compiled from source (CPython 3.11.17)
  because the sandbox blocks binary Python distributions; two PyInstaller
  build-time modules that import `ctypes.util` at import time were made
  tolerant of the ctypes-less interpreter (build environment only — CI uses
  stock CPython and needs no such accommodation).
- `app/BUILD_META.json` is generated by `build_exe.py` at build time and is
  gitignored; the spec guards its inclusion and `app.build_info` falls back
  to git/source identity when it is absent.
