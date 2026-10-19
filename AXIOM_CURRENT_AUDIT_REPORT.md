# AXIOM Current Audit Report

**Audit date:** 2026-10-19 (Asia/Calcutta)
**Repository:** `Thedivinetwilight/AXIOM`
**Session branch:** `arena/107862ec-axiom`
**Base commit:** `4317e3a9f130db3f7fd03db53e00bdf66ac79e9f`
**Verified code commit:** `002da1198312a2def2d31a6368729ff91da59d78`
**Scope:** source, tests, operator/product documentation, startup and recovery,
Upstox market data/history/OAuth, the live execution boundary, and the
executable-build workflow. This report is the current evidence index for the
commit that contains it; the commit SHA and commit-matched CI records are
authoritative in Git/GitHub.

> **Verdict.** The source-level audit and repair pass is complete and the full
> suite passes (**1926 passed, 39 skipped, 0 failed, 160.40 s**). The startup
> refusal path and Upstox authentication/history defects found in this campaign
> are fixed with captured reproductions and regression tests. This is **not** a
> claim of production readiness, legal approval, profitability, a real Upstox
> session, genuine Kronos weights, a signed release, or a locally built
> executable. Cross-platform frozen-build evidence is valid only when the
> GitHub `Build executable` run and each build record name the exact commit.

## 1. Executive findings

| Area | Current finding | Evidence / limits |
|---|---|---|
| Startup refusal | A saved paper account that no longer matches the configuration is refused **before** the database is opened read-write, with a field-level message and exit 3 (config) / 4 (storage). Database and `.env` are byte-identical afterwards. | `tests/test_startup_paper_account.py` (12), live disposable-home reproduction. Previously: 12-frame traceback with no field name. |
| Recovery | `arena.py --new-paper-account` creates a timestamped sibling database, re-verifies the original SHA-256 and rewrites only `DATABASE_URL`. | Same file; live run reports `verified_original_unchanged: true`. |
| Diagnostics | `arena.py --diagnostics` always prints JSON (`paper_account.status`, `health`, paths, mode, broker), exits 0 for informational states, 1 only for an unloadable configuration, and never prints a token. | Same file; live probes with corrupt DB and invalid timeframe. |
| Wizard | Validate-before-write; a failed engine rebuild restores the exact previous `.env` bytes and reopens the previous broker when possible. No `os.environ` mutation. | `tests/test_wizard_paper_safety.py` (2, both failed pre-fix), `tests/test_v310_wizard_flow.py`. |
| Upstox market data | 429/5xx are transient with bounded backoff and a 25-failure cap; 401/403/400 permanent; malformed 200 / untrusted scheme fail closed without retry. Protobuf decoding, reconnect, heartbeat, stale detection tested against a real loopback WebSocket server. | `tests/test_upstox_live.py` (22), `tests/test_upstox_credential_probe.py` (15), `tests/test_upstox_v063.py`. No real Upstox session. |
| Upstox history | Chunking/resume/dedup and documented URL shapes tested; the intraday `AttributeError` that killed every intraday sync is fixed. | `tests/test_v2_history.py` (9), `tests/test_v2_history_transport.py` (6). Real downloads blocked by egress. |
| Upstox OAuth | Login reads the same `.env` it writes the token into, verifies OAuth `state`, and refreshes every configured token key atomically; no process-environment mutation. | `tests/test_upstox_login_env.py` (5), `tests/test_upstox_v062.py` (16). No real OAuth completed. |
| Live order authorization | First-party live mutations route through `LiveExecutionGateway`; final authorization requires an active dispatch bound to the exact gateway, broker, immutable intent and capability. | `tests/test_axiom_execution_gateway.py` (85), `test_live_hardening_v2.py` (47), `test_live_ops_v2_1.py` (202). In-process boundary only. |
| Failure behavior | Unknown, stale, unreadable or malformed state blocks new risk; exits/risk reduction have separate authorization. | `test_live_supervisor_lifecycle.py`, `test_submission_journal_durability.py`, `test_v3_0_1_execution_safety.py`. |
| Kronos / forecast | Input validation and pipeline contracts pass with controlled inputs; status distinguishes configured/available/ready. | `test_kronos_runtime.py` (105), `worker` (80), `artifacts` (75), `pipeline_store` (35). No real weights, no GPU. |
| Storage / updater | Backup manifests, restore staging, updater path confinement and caller-supplied digest checks pass. | `test_v3_0_2_hardening.py` (42), `test_axiom_updater.py` (20). Unsigned manifest/checksum limits documented. |
| Packaged application | Not produced in this environment; the frozen gate is CI-only. | `build_exe.py --test` exits 1: `libpython3.11.so.1.0` not found; no egress for a standalone interpreter. |
| Operator data | The operator's `trading_bot.db` was never opened by the final suite. | Per-test fingerprint plugin: 0 touches; SHA-256 `86ce6fdc…28cc89`. |

## 2. Verification run

```text
$ PYTHONPATH=/tmp .venv/bin/python -m pytest -q -p dbwatch --junitxml=/tmp/full4.xml
1926 passed, 39 skipped, 1 warning in 160.40s (0:02:40)
```

- Collection: **1965 tests**, 79 modules.
- One warning: Starlette's deprecation notice for `anyio.abc.BlockingPortal`.
- The 39 skips are: optional `pandas` (5), `numpy` (1), `torch`/CUDA (1);
  absent Kronos checkout or genuine weights (26); the public Upstox instrument
  file (1, TLS); the two frozen-E2E cases (2, no artifact). They are **not**
  counted as passes.
- `README.md` and the run log carry the same numbers; the earlier `1180 tests`
  statement in the retained 3.1.0 history is explicitly labelled historical.

## 3. Repair summary (details in the final verification report)

| ID | Defect | Status |
|---|---|---|
| D-1 | Refused startup printed a traceback with no field-level diagnosis | FIXED |
| D-2 | A refused start could open/mutate the database read-write | FIXED |
| D-3 | No non-destructive recovery from a saved-account mismatch | FIXED |
| D-4 | Diagnostics exited non-zero on first run and had no account status | FIXED |
| D-5 | Wizard wrote `.env` before validation; rollback was lossy | FIXED |
| D-6 | OAuth login ignored a valid `.env` without python-dotenv; stale token key | FIXED |
| D-7 | Intraday history raised `AttributeError` before any request | FIXED |
| D-8 | One 429/5xx at authorize stopped the feed for the session | FIXED |

Every repair has: captured failing reproduction → root cause → regression test
that failed first → narrow change → focused rerun → full-suite rerun. The first
post-repair full run exposed six regressions introduced by this session's own
edits; all were fixed in code, none by relaxing an assertion.

## 4. Commit-matched CI and artifact verification

The [GitHub Actions **Build executable** workflow](https://github.com/Thedivinetwilight/AXIOM/actions/workflows/build-executable.yml)
is configured to gate:

1. the full source suite on Ubuntu (runs on every push touching app/tests);
2. packaging/frontend/wizard/instance/mode-isolation/UI tests on each build OS;
3. the full suite on each build OS;
4. a PyInstaller build and frozen end-to-end run against the actual executable;
5. a build-identity assertion that `app/BUILD_META.json` contains `github.sha`;
6. upload of the binary, SHA-256 sums and a JSON build record.

For the verified commit, the push-triggered run is
[#39262011293](https://github.com/Thedivinetwilight/AXIOM/actions/runs/39262011293).
Only the Ubuntu source-suite job runs on a push; the three-OS packaging matrix
requires `workflow_dispatch` or a tag. Its result and any artifact records are
quoted in the completion message, because they post-date this commit. A green
push run is **not** packaged-artifact evidence.

## 5. Explicitly unverified / not claimed

- Production Upstox credentials, OAuth completion, real account data, a
  real-money order, broker approval, static-IP registration or live readiness.
- A local one-file executable and any Windows/macOS build.
- Real Kronos weights, publisher origin, forecast quality or GPU runtime.
- Statistical edge, profitability, independent out-of-sample validation, or
  legal/regulatory compliance.
- Control of arbitrary out-of-band HTTP calls by another process holding
  credentials.

**Bottom line:** the software contracts and the startup/recovery path pass on
this commit; defaults remain paper-only; no real order, credential or external
Upstox operation was exercised. Treat this as a software audit, not
authorisation to trade.
