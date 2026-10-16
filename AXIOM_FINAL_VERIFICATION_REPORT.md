# AXIOM Final Verification Report

Date: 2026-10-16 (UTC). Branch `arena/6f2d964a-axiom`.
Format: XVI.68 (repo state · fixes · exact numbers · readiness · remaining
problems · run commands · evidence files).

## 1. Repository state

- Session branch: `arena/6f2d964a-axiom`, base `0314db2` (= `main`),
  fast-forwarded through `fix/axiom-v3.2.4-release` (`4317e3a`).
- Commits added this session (oldest → newest):
  `2679797` fix(sandbox) host/capability cluster → `ec60836` docs+ci
  version/host alignment → `3f6b740` adversarial fail-closed suite +
  orders-view tolerance → `1d9d913` frozen-gate egress honesty →
  `7962c57` final safety-branch closures → *(this reports commit)*.
- No branch/tag deleted, no force-push, no reset; `.env` never existed
  (no credentials in this environment) and remains git-ignored; coverage
  artifacts are git-ignored.
- Version: source line **3.2.4** everywhere that ships (`app/__init__.py`,
  `pyproject.toml`, README, product manual; workflow release name is
  tag-derived — no stale "3.2.0" labels remain in `.github/`).

## 2. Fixes (defect-by-defect detail: `AXIOM_COMPLETION_LEDGER.md` §2)

1. **Sandbox token can no longer reach production hosts.** Order-history
   default → `https://api-sandbox.upstox.com/v2/order/history`; production/
   unknown history hosts refused at `Settings` AND broker construction;
   env vars cannot bypass; live broker unchanged.
2. **Per-capability history state** (`history_capability`, transition event
   `SANDBOX_HISTORY_CAPABILITY`) recorded before the 401 branch: unsupported/
   failing reads never disable order operations; the deliberate global 401
   fail-closed is kept.
3. **`sandbox-test` verdict contract**: exit 0 accepted/order-placed, 1
   rejected/transient, 2 config-refused, 3 no token, 4 probe-unverified
   (read route not exposed) — never prints a token, never places on 401.
4. **Unresolved-intent entry gate**: BUY blocked with
   `RISK_REJECT/UNRESOLVED_INTENT_BLOCKS_ENTRY` while intents are
   unresolved; SELL exits never blocked.
5. **Research seed** is sha256-derived and reproducible (5 ported tests,
   cross-process `PYTHONHASHSEED` pin).
6. **Version drift eliminated** (README/manual/workflow).
7. **Eight operator documents** re-aligned to the verified provider
   contract; the sandbox guide's §5 "open gap" now carries a dated
   RESOLUTION section.
8. **Orders view survives a corrupt raw/payload row** (`_list_orders`
   degrades to `{}`; recover parses defensively — layers agree).
9. **66 new adversarial tests** pin every fail-closed validation branch
   found uncovered (journal, daily risk, place/history responses,
   remote-lookup row guards, slice adoption, reconcile basis anomalies,
   cancel/modify never-guess, envfile, risk-limit reasons, host parsers).
10. **Frozen gate honesty**: the upstream-HTTPS check consults host egress;
    with egress (CI) failures are still hard failures; without egress it
    records `SKIPPED-UPSTREAM` instead of a fabricated pass.

Re-investigated known defect areas and confirmed them already-correct on
this branch (tests pin): `.env` load order, host allowlist,
`SIGNAL_NOT_AN_ORDER` accounting, instrument-key validation, fill/P&L
idempotency, Kronos weights honesty (traceability R5–R8).

## 3. Exact test / coverage numbers

Suite (2026-10-16, commit `7962c57`, `pytest -q -rs --cov… --cov-branch`):

- **1997 passed, 27 skipped, 0 failed — 216.08 s.**
- Skips (all external/honesty): 18 × no Kronos checkout, 6 × no genuine
  Kronos weights (tests refuse substitution), 1 × torch not installed,
  1 × instrument-master HTTPS blocked by egress, 0 × frozen E2E (closed:
  **2 passed** with `dist/Arena`).
- Progression: 1886 baseline → 1889 → 1919 → 1929 → 1987 → **1997**.

Coverage (branch mode; sources `app axiom desktop scripts tools run arena
build_exe`):

- **Line 79.23%** (20,243/25,549); **Branch 68.74%** (5,319/7,738);
  combined **76.79%**.
- Production core `app/+axiom/`: **line 82.76%** (19,412/23,457),
  **branch 71.67%** (5,074/7,080).
- 100% line + 100% branch: `app/config/hosts.py`, `app/risk/limits.py`.
  100% line: `app/brokers/sandbox.py` (557/557), `app/config/envfile.py`
  (104/104). `app/engine/state.py` 97.58% line — the exact 8 remaining
  lines are concurrency-only in-transaction guards (enumerated in
  `AXIOM_TEST_COVERAGE_REPORT.md` §4).

Mutation testing: **9/9 mutations killed** (hosts default, settings
refusal, 401 fail-open, capability recording, unresolved gate, seed, live
gate, journal quantity guard, risk-limit gate); worktree verified clean
afterwards.

Lint: `ruff check --select E4,E7,E9,F,I` clean on every file touched this
session (pre-existing findings elsewhere untouched).

## 4. Readiness

- **Local paper trading (default `MODE=PAPER`, `LIVE_TRADING=false`,
  `BROKER=LOCAL_PAPER`): RELIABLE.** `initialize` idempotent (exit 0;
  universe sync honestly `unavailable` without egress); `paper-test`
  **17/17** with `paper_only {PAPER, false}`; paper E2E over replay: 1600
  bars, 8 orders/8 skips, flat finish, equity ₹121,452.58, realized
  +₹21,452.58 (`results/paper/result.json`); `python -m app engine`
  correctly refuses non-sandbox brokers (exit 2).
- **Frozen executable: BUILT AND VERIFIED LOCALLY** — `dist/Arena`
  (27,965,216 B); smoke 19 PASS + 1 explicit `SKIPPED-UPSTREAM` (host
  egress cannot TLS to `api.upstox.com` — reproduced from the host python);
  `tests/test_v310_frozen_e2e.py` **2 passed**.
- **Upstox Sandbox (Gate E): BLOCKED — credentials absent, honestly
  unverified.** Provider contract verified from primary sources
  (docs + official SDK): place/modify/cancel only; order history NOT a
  documented sandbox capability (one 2024 announcement claims otherwise —
  conflict documented). All code/config/CLI/tests are at the boundary; no
  request in this session ever left for a production host, and a mock pass
  is explicitly NOT claimed as Gate E. Operator runbook:
  `AXIOM_SANDBOX_READINESS.md` §4–5.
- **Live trading: untouched and locked** (four-switch + limits; mock-only
  tests; no live-credential fallback, no weakening of any risk check).

## 5. Remaining problems (honest list)

1. **Gate E (real sandbox token) unverified** — external blocker: no
   `UPSTOX_SANDBOX_ACCESS_TOKEN` exists here. Runbook ready.
2. **Provider doc conflict** (sandbox read fidelity) — unresolved
   upstream; handled defensively either way.
3. **Coverage remainder** — `sandbox_engine.py` 68.60% line (rare flows),
   `live.py`/`gateway.py` live-network arms, `__main__.py` CLI wrappers,
   ops/build tools, GUI module (Qt optional by design), generated
   protobuf — enumerated with reasons in the coverage report; targets met
   exactly where mandated (safety-critical modules at/near 100%).
4. **Hosted CI unusable** — GitHub billing failure (run `38505883907`);
   local pytest is the verification path; workflows not dispatched.
5. **External assets** — 27 skips need: Kronos upstream checkout (18),
   genuine pretrained weights (6), torch (1), instrument-master egress (1).
6. **Frozen upstream check in offline envs** — `SKIPPED-UPSTREAM` note;
   CI with egress must enforce it (documented in the harness).

## 6. Run commands (reproduce everything)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-lock.txt
.venv/bin/pip install pytest-cov pandas numpy ruff      # optional-dep skips closed

# full suite + coverage (authoritative)
.venv/bin/python -m pytest -q -rs --cov=app --cov=axiom --cov=desktop \
  --cov=scripts --cov=tools --cov=run --cov=arena --cov=build_exe \
  --cov-branch --cov-report=term --cov-report=xml:coverage.xml \
  --cov-report=json:coverage.json --tb=short

# lint (scoped; whole-tree style churn is pre-existing)
.venv/bin/ruff check --select E4,E7,E9,F,I <touched files>

# local paper E2E
.venv/bin/python -m app initialize
.venv/bin/python -m app paper-test                 # 17/17
.venv/bin/python run.py --mode paper --source replay \
  --csv data/sample.csv --delay 0 --symbol DEMO    # results/paper/result.json

# frozen executable (requires libpython3.11.so.1.0; see ledger §5 for the
# toolchain built in this sandbox)
python build_exe.py --test
.venv/bin/python -m pytest tests/test_v310_frozen_e2e.py -v   # 2 passed

# with a real sandbox token (Gate E)
python -m app sandbox-test                 # exit 0/1/2/3/4 semantics
python -m app sandbox-test --place-order   # if exit 4 (read route absent)
```

## 7. Evidence files (repo root)

| File | Contents |
|---|---|
| `AXIOM_COMPLETION_LEDGER.md` | inventory, 9-fix defect table with commits, test-run table, 9/9 mutation table, paper-E2E results, frozen-build results, final numbers |
| `AXIOM_TEST_COVERAGE_REPORT.md` | commands, 1997/27 run, skip taxonomy, per-scope + per-module coverage tables, enumerated remainder, exclusions with reasons |
| `AXIOM_REQUIREMENTS_TRACEABILITY.md` | R1–R22 requirement → implementation → evidence → status |
| `AXIOM_SANDBOX_READINESS.md` | provider contract (dated sources), guarantees, Gate E BLOCKED rationale, operator runbook, verdict slot |
| `AXIOM_FINAL_VERIFICATION_REPORT.md` | this file |
| `coverage.xml` / `coverage.json` | machine-readable coverage (git-ignored) |
| `results/paper/result.json` | paper E2E economics |
| `dist/Arena` + `build/arena/` | frozen artifact and build tree |
| `tests/test_sandbox_history_capability.py`, `tests/test_adversarial_fail_closed.py` | the 22+66 tests added this session |
