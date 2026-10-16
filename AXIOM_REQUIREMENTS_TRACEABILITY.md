# AXIOM Requirements Traceability

Session: 16 Oct 2026, branch `arena/6f2d964a-axiom`.
Source of requirements: the "COMPLETE RECOVERY, IMPLEMENTATION, TESTING AND
SANDBOX-READINESS DIRECTIVE" as carried in this session (priority
correctness/safety → order/data integrity → functional completion → testing →
docs; continuous execution to acceptance criteria; five mandatory reports;
Gate E honesty; operational boundary LOCAL PAPER → UPSTOX SANDBOX only).

Status values: **DONE** (implemented and verified here),
**VERIFIED** (re-inspected; already correct, tests pin it),
**BLOCKED-EXTERNAL** (cannot complete without provider/credentials/assets —
independent work finished), **HISTORICAL** (kept as labeled history by
design).

| ID | Requirement | Where implemented | Evidence (tests/artifacts) | Status |
|---|---|---|---|---|
| R1 | Work continuously through inventory→…→reassess; no stopping at plans/first green | Full session executed: inventory → fix waves → adversarial tests → mutation battery → E2E → build → reports | Commit chain `2679797 → ec60836 → 3f6b740 → …`; this ledger | DONE |
| R2 | Correctness/safety first; never weaken risk checks; live paths tested with mocks only | Unresolved-intent BUY gate; emergency/pause gates kept; `HardRiskLimits` untouched (mutation M9 proves tests bite); live broker tests all mocked | `tests/test_sandbox_history_capability.py` (gate test), `tests/test_live_hardening_v2.py`, mutations M3/M7/M9 killed | DONE |
| R3 | Sandbox token must never reach production hosts (`.env` order fix `d9ba2f3`, allowlist fix `1d34479` reverified) | History default/refusal at Settings + ctor; `hosts == {api-sandbox.upstox.com}` in sandbox mode | capability suite host-boundary class; `tests/test_v1_separation.py`; mutations M1/M2 killed | DONE |
| R4 | Reconciliation/401 global-availability defect (highest-priority known defect; verify against current Upstox docs) | Per-capability `history_capability` recorded before the 401 branch; order availability independent; global 401 fail-closed kept deliberately; production history host refused outright | capability isolation tests; mutations M3/M4 killed; docs+SDK evidence in `AXIOM_SANDBOX_READINESS.md` §1 | DONE |
| R5 | No silent signal discard (`SIGNAL_NOT_AN_ORDER`) | Re-audited `_act`: HOLD=debug no-op by design; EXIT-flat=info; actionable signals journaled (`record_signal`) or counted (`RISK_REJECT`, `rejected_by_risk`, `paper-test` risk_counters) | `app/engine/sandbox_engine.py` 1222/1242; `paper-test` 17/17 incl counters; journal tests `tests/test_v1_learning.py` | VERIFIED |
| R6 | Instrument-key validation fail-closed | `resolve_instrument_key` refuses ambiguous/missing identity; wizard prepare-paper validates keys (merged `f0f22e5`) | `tests/test_v1_sandbox.py`, `tests/test_wizard_upstox_validation.py` | VERIFIED |
| R7 | Fill/P&L idempotency | Transition identity includes child+status+filled+ts; reconcile-once tests; repo-backed second-reconcile no-double-credit; deterministic ledger replay | `tests/test_v1_sandbox.py`; `tests/test_adversarial_fail_closed.py` (idempotency, replay determinism); mutation-adjacent coverage | DONE |
| R8 | Kronos weights honesty (no fabricated forecasts) | Tests refuse random-weight substitution; skip reasons state exactly what is missing; artifact verify/mutate tools present | 6 skip messages "no genuine pretrained Kronos weights…"; `tools/verify_kronos_artifacts.py` | VERIFIED (weights external: BLOCKED-EXTERNAL) |
| R9 | Version drift 3.2.4 vs 3.0.5 vs Arena 3.2.0 | Branch/`pyproject` already 3.2.4; README + product manual now 3.2.4; workflow labels de-pinned/tag-derived | commit `ec60836`; `git grep "3.2.0"` in `.github/` returns none of the old labels | DONE |
| R10 | Fix every reproducible defect found | 9 defect rows in `AXIOM_COMPLETION_LEDGER.md` §2 (incl. orders-view corrupt-raw brittleness found while covering recovery branches) | commits `2679797`, `ec60836`, `3f6b740` | DONE |
| R11 | Full test suite run, skips accounted for, no weakened assertions | Full-suite runs logged; skip taxonomy enumerated; one former skip class (pandas/numpy) eliminated by installing optional deps; 2 frozen skips addressed by local build | `AXIOM_TEST_COVERAGE_REPORT.md` tables | DONE |
| R12 | Measured coverage: 100% line target first-party, 100% branch safety-critical | Branch-coverage run over `app/axiom/desktop/scripts/tools/run/arena/build_exe`; per-module table + explicit gap enumeration + safety-critical module list; gaps closed where safety-relevant (66 new tests) | `AXIOM_TEST_COVERAGE_REPORT.md`: line **79.23%** overall / **82.76%** production core; **100% line + 100% branch** on `hosts.py` and `limits.py`; **100% line** on `sandbox.py` and `envfile.py`; `state.py` 97.58% with its 8 concurrency-only lines named | DONE-with-honest-remainder (target not manufactured; measured state + enumerated remainder published) |
| R13 | Mutation testing on safety logic | 9 hand-applied mutations across hosts/settings/401/capability/gate/seed/live-gate/journal-guard/risk-limits; each restored after run | mutation table `AXIOM_COMPLETION_LEDGER.md` §3 (9/9 killed) | DONE |
| R14 | Reliable local paper trading, default `MODE=PAPER`, `LIVE_TRADING=false`, `BROKER=LOCAL_PAPER` | `initialize` idempotent; `paper-test` 17/17 `paper_only{PAPER,false}`; engine refuses non-sandbox brokers (exit 2); paper E2E on replay ends flat with recorded economics | ledger §4; `results/paper/result.json` | DONE |
| R15 | Upstox Sandbox verification as far as provider/credentials permit | Provider contract verified from primary sources; code+CLI+tests to the boundary; Gate E honestly BLOCKED (no token in this environment) | `AXIOM_SANDBOX_READINESS.md` | BLOCKED-EXTERNAL (credentials) |
| R16 | Frozen executable packaging gate | Local PyInstaller build via self-built toolchain (host lacked `libpython3.11.so.1.0`; built CPython 3.11.2 `--enable-shared` from official source + adopted distro exact-version extensions), `build_exe.py --test` smoke (19 PASS + 1 honest `SKIPPED-UPSTREAM`), frozen E2E suite | `dist/Arena` (27,965,216 B), `tests/test_v310_frozen_e2e.py` **2 passed** (was 2 skipped), harness commit `1d9d913` | DONE |
| R17 | Five workspace reports maintained | All five files at repo root; final numbers section filled after the last full run | this report set | DONE |
| R18 | Docs aligned with verified reality | 8 operator docs + workflow updated; historical verification snapshots (`V1_VERIFICATION.md`) left labeled | commit `ec60836` | DONE |
| R19 | Final answer in XVI.68 format (repo state, fixes, exact numbers, readiness, remaining problems, run commands, evidence) | Delivered as the closing chat answer; mirrored in `AXIOM_FINAL_VERIFICATION_REPORT.md` | final report | DONE |
| R20 | Git safety: no reset/force-push/deletes; push only session branch; focused commits | Read-only inventory first; `fetch --prune` only; three focused commits (+final); no force operations | `git log` chain; PR opened from `arena/6f2d964a-axiom` | DONE |
| R21 | No secrets printed/stored; `.env` git-ignored; no credential values in reports | No token exists in this environment; reports contain host names only; capability test asserts `.env.example` carries no credential values | capability suite `.env` test; `git status` shows no `.env` | DONE |
| R22 | A mock pass never satisfies Gate E; old evidence stays historical until reproduced | Gate E marked BLOCKED with reasoning; 1,846-test/audit-6-Oct evidence not reused as current; fresh numbers everywhere | `AXIOM_SANDBOX_READINESS.md` §3 | DONE |

## Notes on scope decisions

- **100% line coverage across everything was not manufactured.** The
  coverage report measures the real state, documents every excluded area
  (build/ops scripts, GUI module requiring Qt, generated protobuf) with
  reasons, enumerates the remaining uncovered lines in safety-critical
  modules, and adds tests where branches were both uncovered and meaningful
  (R12). Raising every operational-script line was judged lower-value than
  safety-branch closure — stated openly rather than papered over.
- **Global 401 fail-closed was kept** (not relaxed to "history-only"): a
  401 from the same environment that guards order endpoints is genuine
  credential evidence; only the *capability attribution* changed.
- **Provider announcement conflict** (sandbox data-retrieval fidelity) is
  documented, not resolved in AXIOM's favor: the code treats history as
  per-capability with an honest unknown, never assumes either answer.
