# AXIOM paper-release audit — `3.7.0a10` reviewed source

**Branch:** `arena/8d3d005e-axiom` · **Reviewed base commit:** `f9a640932fa64fc63509696aad987f4623a4375f`
· **This work:** the commit(s) after that base on the same branch (the code/tests/docs
commit is recorded in §8).

**Not a release.** No tag, installer, published wheel, GitHub release, merge or push
was produced by this work. Nothing was changed in any upstream repository, and no
credentials, real-money account, broker session or order was used. Live execution
stayed OFF for every command in this document.

**Scope.** Five closure items that stood between the source tree and a *reliable,
clearly documented paper-trading release* (see §2–§6), plus full re-validation (§7).

---

## 1. What this is (and is not)

- AXIOM is a **local, paper-first** application: NSE (Upstox) / NASDAQ (Alpaca)
  read-only market data, `LIVE_MARKET_LOCAL_PAPER` simulation on a local SQLite
  engine, optional Model-Lab (Kronos) advisory forecasts, and separately gated live
  execution that ships **OFF / DISARMED**.
- This audit does **not** claim profitability, real-money readiness, or that any
  broker integration is verified. It records what was executed locally and what
  remains unverified because it needs credentials or a broker (§9).

---

## 2. Cross-market PAPER timeout is per-market (and tested deterministically)

**Defect.** `LiveMarketPaperEngine.advance_selected_markets_concurrently`
(`app/markets/live_paper.py`) computed **one** `deadline = now + timeout` for the
whole batch and then waited on the futures **in submission order**:

```python
deadline = time.perf_counter() + timeout_s
for m, fut in futures_by_mkt.items():
    remaining = max(0.005, deadline - time.perf_counter())      # shared budget
    dur_ms, snap_out = fut.result(timeout=remaining)            # order-dependent
```

Time spent blocked on a slow NSE worker was deducted from NASDAQ's remaining
budget, so a NASDAQ cycle could be reported `TIMEOUT` for a reason that had nothing
to do with NASDAQ — and the verdict depended on `markets=[...]` ordering.

**Fix.** Each market owns the window `[its own submission time, + market_timeout_seconds]`.
The futures are awaited once as a batch (`concurrent.futures.wait`) and each verdict
is derived from that market's own future only; `TIMEOUT` reports the market's real
elapsed time. One market's latency can no longer spend another market's budget.

**Tests.** The previous regression test proved this with `time.sleep(1.5)` and
`assert elapsed < 1.0` — a timing-only assertion that can flake on a loaded machine.
It is replaced by event-driven tests in `tests/test_concurrent_multimarket_paper.py`:

| Test | Synchronisation |
|---|---|
| `test_slow_nse_provider_never_turns_a_finished_nasdaq_cycle_into_a_timeout` | NSE provider parks on a `threading.Event`; the engine wrapper fires a NASDAQ-completion event from the worker; the cycle must still be running (`out == []`); then NSE is released → both markets `COMPLETED` |
| `test_market_timeout_budget_is_per_market_and_order_independent[NSE,NASDAQ / NASDAQ,NSE]` | NSE never released → `TIMEOUT`; NASDAQ completion event proves it finished inside its own window → `COMPLETED`, identical for both market orders |
| `test_background_runner_isolates_a_blocked_market_without_sleeps` | Same gating through `MultiMarketPaperRunner.run_once` (async runner) |

No wall-clock thresholds and no `sleep()` calls remain in these tests; 8 consecutive
full-file runs were green.

---

## 3. `python -m app paper-live ...` argument forwarding

**Defect.** `cmd_paper_live` (`app/__main__.py`) read a module-level `argv`:

```python
passthrough = argv[args.passthrough:] if args.passthrough else []
```

`argv` does not exist in that module → `NameError: name 'argv' is not defined` for
every invocation that carried arguments (invocations without arguments appeared to
work). The `paper-live` sub-parser also used `nargs="*"`, which rejects flags it does
not declare — `--delay`, `--database`, `--symbol` belong to `run.py`, not to the app CLI.

**Fix.** The command line is split *before* argparse
(`split_forwarded_argv`), so everything after a documented full-line forwarder
(`paper-live`) reaches `run.py` verbatim; all other commands keep strict validation.
`cmd_paper_live` consumes `args.passthrough` (with a defensive `getattr`).

**Tests.** `tests/test_cli_paper_live_forwarding.py` (5 tests) drives the real
`app.__main__.main()` with `run.main` replaced by a recorder — no engine, feed,
database, broker or order path is touched:

- unknown flags/values forwarded in order (`--delay 0.5 --database sqlite:///paper.db`);
- no-argument invocation forwards only `--mode paper --source upstox`;
- verbatim token order preserved;
- never forwards a live-enable token (`--live` / `mode=live`);
- `cmd_paper_live` tolerates a `Namespace` without `passthrough`.

Manual check: `python -m app paper-live --help` prints `run.py`'s usage (exit 0).

---

## 4. Hidden Vantage/MT5 live API resolved — removed

`app/api/routes/vantage.py` was mounted in `create_app`, so **30 authenticated
`/api/trading` endpoints were reachable**: `VANTAGE_DEMO`/`VANTAGE_LIVE` session
selection, DPAPI credential entry, `arm`, `activate`, order review/submit/cancel,
position modify and emergency stop. None of it appeared in the product
documentation, and the operator UI for it was deleted in `v3.7.0a9`. It is therefore
**no longer intended** and was removed from the supported application rather than
documented as a supported workflow the team cannot verify (no MT5 terminal or broker
is available here, and documenting it would present an unverified live integration
as working).

- `app/api/server.py`: the router is no longer imported or mounted; the mount loop
  documents why, and a comment states the regression guard.
- Every legacy path returns **`404`**, authenticated or not.
- `/health` now reports `real_order_routing: "DISABLED_PAPER_ONLY"`,
  `vantage_api: "REMOVED"`, `vantage_startup: "REMOVED_UNMOUNTED"`.
- The removed surface also served the latency metrics used by the operator Settings
  panel; those are preserved on a first-class read-only route
  `GET /api/performance` (`app/api/routes/performance.py`: authenticated,
  `paper_only: true`, `live_trading_enabled: false`, `broker_order_api_calls: 0`).
- `tests/test_vantage_api.py` (which exercised the live routes) is replaced by
  `tests/test_vantage_live_api_removal.py` (21 tests): prefix scan, every legacy
  GET/POST path `404`, unauthenticated submit `404`, package still importable but
  absent from the mount loop, supported surfaces still answering with live OFF.
- Three tests that incidentally read `/api/trading/*` were re-pointed at supported
  surfaces (`/api/v2/logs`, `/paper/account`, `/paper/positions`,
  `/api/market-data/status`, `/api/performance`).
- `docs/VANTAGE_MT5_ISOLATION_AUDIT.md` §3 records the removal; README links it.

Still present and **documented as unsupported legacy** (no order capability):
`app/vantage/**` (unreachable), the read-only `/api/market-data/*` MT5 endpoints, and
the `source: "vantage_mt5"` local-paper feed. These require a locally installed MT5
terminal; they were **not** exercised against a real terminal in this work.

---

## 5. Dependency set patched and audited

`pip-audit -r requirements-lock.txt` **before** (baseline lock):

```
Found 12 known vulnerabilities in 2 packages
starlette 0.52.1   PYSEC-2026-161 / -248 / -249 / -2280 / -2281   (fixes: 1.0.1 … 1.3.1)
certifi   2026.7.22 PYSEC-2026-4320                              (fixes: 2026.10.18)
```

**After** (this work):

| Package | Before | After | Why |
|---|---|---|---|
| `fastapi` | `0.141.1` | **`0.162.0`** | current patched 0.x line; `Requires-Dist: starlette>=0.46.0`, `pydantic>=2.9.0` — compatible with the pins below |
| `starlette` | `0.52.1` | **`1.10.0`** | every `PYSEC-2026-*` fix lands in 1.x; the 0.5x line cannot be patched. The repo's previous `<1` cap was there out of caution about the TestClient stack — the full suite was re-run against 1.10.0 with `httpx 0.28.1` and passes (a `StarletteDeprecationWarning` suggests `httpx2`; not a failure) |
| `certifi` | `2026.7.22` | **`2026.10.23`** | clears `GHSA-6q48-6c7w-9gw2` / `PYSEC-2026-4320`: 2026.10.18 removed the revoked emSign roots, 2026.10.19 restored the unaffected ones |

`requirements.txt` constraints were raised accordingly (`fastapi>=0.162,<1`,
`starlette>=1.3.1,<2`, explicit `certifi>=2026.10.19`) with comments recording the
advisory IDs. `pydantic 2.13.4`, `httpx 0.28.1`, `uvicorn 0.53.0` and the rest of the
lock were unchanged — no forced incompatible jump.

Verification:

```
pip-audit -r requirements-lock.txt      → No known vulnerabilities found
pip-audit (installed env, after       → No known vulnerabilities found
  pip install --upgrade setuptools)     (before that, only the venv-bundled
                                         setuptools 66.1.1 was flagged; it is not
                                         an application dependency or a lock entry)
```

---

## 6. README / release information now points at the reviewed source

- Header states `3.7.0a10`, the **reviewed source branch `arena/8d3d005e-axiom`**, and a
  callout: **no release has been published**; the default branch (`main`) still holds
  the older Arena `3.0.5` source and does **not** contain this application; there is
  no installer, published wheel or tag for this version.
- Clone instructions fetch/checkout `arena/8d3d005e-axiom` (previously
  `arena/ffd46191-axiom`) and ask the operator to confirm the commit with
  `git log --oneline -1`.
- The update instructions use `git fetch origin arena/8d3d005e-axiom` +
  `git merge --ff-only origin/arena/8d3d005e-axiom`, and name the pinned dependency
  set that was audited.
- `docs/OWNER_SETUP_GUIDE.md` checkout line and the endpoint columns that referenced
  removed `/api/trading/*` paths were corrected (`/api/performance`,
  `/api/markets/paper/audit/events`).
- `.github/workflows/paper-checks.yml` also runs the Linux operator checks for
  `arena/8d3d005e-axiom`.

---

## 7. Validation actually executed (local, Linux, CPython 3.11.2)

Environment: fresh venv installed **from `requirements-lock.txt`**
(`fastapi 0.162.0`, `starlette 1.10.0`, `certifi 2026.10.23`, `pydantic 2.13.4`,
`httpx 0.28.1`, `uvicorn 0.53.0`, `pytest 9.0.3`).

| Check | Command | Result |
|---|---|---|
| Full Python suite | `python -m pytest -q` | **1101 passed, 4 skipped** in 109.52 s |
| New/changed test files | `pytest tests/test_vantage_live_api_removal.py tests/test_cli_paper_live_forwarding.py tests/test_concurrent_multimarket_paper.py` | 33 tests, all pass (21 + 5 + 7) |
| Concurrency stability | `pytest tests/test_concurrent_multimarket_paper.py` × 8 | 8/8 green, no timing assertions |
| JavaScript harnesses | `npm ci && npm test` | 9/9 harnesses PASS (chart, terminal, multimarket terminal, model lab, live trade monitor + ensemble, kronos paper/logs, concurrent multimarket, autonomy, live execution panel); reported **0 broker order leaks / 0 order endpoints** |
| Compilation | `python -m compileall -q app desktop run.py axiom_launcher.py` | exit 0 |
| Wheel build | `python -m build --wheel` | `arena-3.7.0a10-py3-none-any.whl` built |
| Dependency audit | `pip-audit -r requirements-lock.txt` / `pip-audit` | no known vulnerabilities |
| Offline operator smoke | `python axiom_launcher.py --smoke` | `status: PASS`, `mode: PAPER`, `live_trading: false`, `default_live_execution: OFF`, `broker_order_calls: 0`, `user_data_modified: false` |
| Offline paper self-test | `python -m app paper-test` | `ok: true`, deterministic expected equity/P&L exact, `paper_only.live_trading: false` |
| CLI smoke | `python -m app paper-live --help` | `run.py` usage printed, exit 0 (argument forwarding reached run.py) |

**Paper workflows make zero broker order calls, and live execution stays disabled:**
enforced by the smoke output above, by
`tests/test_vantage_live_api_removal.py::test_supported_surfaces_still_answer_and_live_stays_off`
(`default_live_execution: OFF`, both markets `NOT_AVAILABLE`), by
`tests/test_concurrent_multimarket_paper.py` (`broker_order_api_calls == 0`,
`live_trading_enabled is False` on every cycle payload) and by the JS harnesses'
order-endpoint counters.

Baseline for comparison (same tree before this work): 1082 passed / 4 skipped.
The delta is: `-1` legacy live-route test module (259 lines), `+21` removal guards,
`+5` CLI forwarding, `+4` concurrency (1 rewritten as 3 event-driven, 1 added),
`+1` performance route, and re-pointed assertions.

---

## 8. Branch, commit, and what ships where

- **Branch:** `arena/8d3d005e-axiom` (local clone). The reviewed base was
  `f9a640932fa64fc63509696aad987f4623a4375f` (= the previous task's fix and the
  newest source per the audit).
- **Code/tests/docs commits** (all local, in order, direct descendants of the reviewed
  base `f9a6409`):
  1. `05e52196351086fb2df9aab7e34f847e992af4c7` — `feat(corporate-actions): auditable splits + cash dividends for research and backtests`
     (carries both the paper-release fixes of §2–§7 and the corporate-action work of §10)
  2. `1835d9551c2e303bdfb4ab63dac1a30a460dae57` — `docs(audit): record the corporate-action commit SHA`
  3. `60962fb74ae146b68cc185f4384cdda14edec89d` — `fix(corporate-actions): validate the feed before writing; refuse ambiguous market scope`
     (plus the re-measured validation numbers in §10.3)
  This report is updated by the docs-only commit that follows them and only records
  these SHAs.
- **Nothing was pushed, merged, tagged or published.** The upstream repository is
  untouched, and the default branch does not contain this work.

Files changed by this work:

```
app/api/server.py                     legacy /api/trading router unmounted; /api/performance mounted
app/api/routes/performance.py         NEW  authenticated read-only latency route
app/api/routes/health.py              real_order_routing / vantage_api / vantage_startup labels
app/markets/live_paper.py             per-market timeout scheduler (concurrent.futures.wait)
app/__main__.py                       argv NameError fix + split_forwarded_argv + paper-live forwarding
requirements.txt                      fastapi>=0.162, starlette>=1.3.1, certifi>=2026.10.19
requirements-lock.txt                 fastapi==0.162.0, starlette==1.10.0, certifi==2026.10.23
README.md                             reviewed branch, no-release statement, patched dependency note
CHANGELOG.md                          §7 entry for this work
docs/PAPER_RELEASE_AUDIT.md           NEW  this report
docs/VANTAGE_MT5_ISOLATION_AUDIT.md   §3 route-removal record
docs/OWNER_SETUP_GUIDE.md             reviewed branch + supported endpoints
docs/TERMINAL_PERFORMANCE.md          /api/performance
.github/workflows/paper-checks.yml    Linux checks on the reviewed branch
tests/test_vantage_live_api_removal.py        NEW  (21) removal guards
tests/test_cli_paper_live_forwarding.py       NEW  (5)  paper-live forwarding
tests/test_concurrent_multimarket_paper.py    rewritten timeout tests (deterministic)
tests/test_terminal_performance_listed.py     /api/performance + removal guard
tests/test_paper_replay_audit.py              audit via /api/v2/logs + /paper/account
tests/test_mt5_market_data.py                 supported status/positions endpoints
tests/test_upstox_live.py                     /health label assertions
tests/test_vantage_api.py                     DELETED (tested the removed live routes)
```

---

## 9. Remaining limitations / validation that this work could not perform

Recorded as **remaining validation**, not as completed development:

1. **Windows-only paths are unverified here.** `Start-Axiom.ps1` (including
   `-Smoke`), DPAPI credential storage (`app/security/dpapi_secrets.py`), the
   PyInstaller/Inno packaging scripts, `windows-release.yml` and
   `tests/test_windows_*.py` acceptance runs need a Windows host or CI runner. Only
   their Linux-importable logic was exercised.
2. **No credential or broker access was used or attempted.** Upstox OAuth/token
   flows, Upstox `UPSTOX_LIVE` (SEBI static IP, GTT v3), Alpaca market-data
   entitlement (`iex`/`sip`/`delayed_sip`), `ALPACA_BROKER_LIVE`/`ALPACA_BROKER_PAPER`
   and the Alpaca sandbox are **not verified** by this audit. They remain gated OFF.
3. **MT5 terminal paths are unverified and now unreachable for orders.** Legacy
   `app/vantage/**`, `/api/market-data/*` and the `vantage_mt5` paper feed need a
   real terminal; the order/session API is removed (404). Nothing here should be read
   as "MT5 works".
4. **Kronos Model Lab** needs downloaded model weights and an offline CPython runtime;
   the advisory path is covered by unit/contract tests only. No forecast was produced
   against real weights in this audit.
5. **`httpx2` migration**: `starlette 1.10.0` emits a deprecation warning suggesting
   `httpx2` for its TestClient. Tests pass with `httpx 0.28.1`; moving the test client
   stack is a separate, reviewed change.
6. **Performance/latency numbers** in the repo (including `/api/performance`) are
   local-process observations; they establish nothing about real broker or network
   latency.
7. **Recorded head:** the work is commit `60962fb74ae146b68cc185f4384cdda14edec89d` on `arena/8d3d005e-axiom`; the
   docs-only commit carrying this sentence is its child. Re-verify with
   `git log -4 --format='%h %s'` before handing the branch on. The local clone lost its
   commit objects twice during this work (the working tree survived both times); the
   branch was re-anchored on `f9a6409` and the surviving tree re-committed, so the three
   commits listed in §8 — not any earlier SHA — are authoritative.

---

## 10. Corporate actions (splits + cash dividends) — auditable handling

Scope: **stock splits and cash dividends only**, research/backtest surfaces only.
Design, format and conventions are documented in `docs/CORPORATE_ACTIONS.md`; this
section records what was implemented and what was actually executed.

### 10.1 What exists now

| Area | Change |
| --- | --- |
| `app/data/corporate_actions.py` (new) | Versioned `axiom.corporate-actions` v1 import, validation, revision/duplicate/conflict/ambiguity rejection, split + dividend arithmetic, point-in-time views, provenance + hashes |
| `app/model_lab/datasets.py` | `register_dataset(..., corporate_actions=..., corporate_action_policy=...)` stores a hash-pinned sidecar and a full provenance block; new `load_corporate_actions` (tamper-checked) and `load_adjusted_dataset(policy=..., as_of=...)`; `canonical_bars_sha256` now re-exported from the new module (one formula for dataset and action provenance) |
| `app/api/routes/model_lab.py` | Existing import endpoint accepts `corporate_actions` + `corporate_action_policy`; status/provenance returned in the dataset payload and in `GET /api/model-lab/datasets`. No new screen or route |
| `app/model_lab/research_pipeline.py` | Run manifest `dataset` block records `corporate_action_status`, `corporate_actions_verified` and the full `corporate_actions` provenance (policy, hashes, applied actions, limitations) |
| `app/ensemble/service.py` | `methodology_audit_disclosures.corporate_actions_status` is now derived from real verified status (`CORPORATE_ACTIONS_VERIFIED` / `PARTIALLY_VERIFIED_CORPORATE_ACTION_ADJUSTMENT` / `UNVERIFIED_CORPORATE_ACTION_ADJUSTMENT`) plus a `corporate_actions` disclosure block |
| `app/backtesting/engine.py` | `BacktestEngine.run` records the basis in `BacktestResult.corporate_actions`, refuses double-counted dividends, warns when fills are simulated on adjusted prices, and warns that cash dividends are not credited |
| `app/api/services.py` | Paper replay dataset loader refuses research-adjusted series (`assert_raw_candles`) |
| `app/static/terminal/terminal.js` + `index.html` | **Corp. Actions** column added to the existing Historical Dataset Manager table (status pill + hover detail); no new screen, no new route |

Two failure modes were closed during review rather than left implicit: a rejected
action feed now fails **before** any bar file or index entry is written (an import
that raises leaves the dataset store untouched), and a feed holding the same ticker in
two markets is refused with `AMBIGUOUS_MARKET_SCOPE` instead of guessing which action
set applies when no market is passed.

Conventions enforced: splits keep `price × volume` and holding value invariant; a cash
dividend is either folded into prices **or** exposed as a cash event, never both
(`assert_no_double_counting`); stored dataset bars are always raw and every adjusted
series is derived per call, with `AdjustedSeries.for_execution()` refusing to hand
adjusted prices to an execution path.

### 10.2 Explicitly unsupported (labelled, never adjusted)

`BONUS_ISSUE`, `RIGHTS_ISSUE`, `MERGER`, `DEMERGER`, `SPIN_OFF`, `DELISTING`,
`SYMBOL_CHANGE`. They are recorded, hashed, surfaced in
`corporate_actions.unsupported_actions`, and downgrade the series to
`PARTIAL_UNSUPPORTED` with an `UNSUPPORTED_ACTIONS_PRESENT` limitation. Unrecognised
action types reject the import. Missing action data leaves the dataset `UNVERIFIED`
with `NO_CORPORATE_ACTION_DATA`; a free-text adjustment claim without supporting data
is recorded, flagged `UNVERIFIED_ADJUSTMENT_CLAIM` and **not** honoured.

### 10.3 Validation actually executed (local, Linux, CPython 3.11.2)

| Check | Result |
| --- | --- |
| Full Python suite (`python -m pytest -q`) | **1175 passed, 4 skipped** in 106.53 s, re-run on the final tree in a freshly rebuilt venv (baseline before this task: 1101 passed, 4 skipped) |
| New corporate-action tests | **74 passed** (57 core + 17 integration; deterministic synthetic fixtures) |
| JS harnesses (`npm test`) | 9/9 PASS — chart, terminal, multimarket, model-lab, live-trade-monitor/ensemble, Kronos paper/logs, concurrent multimarket, autonomy, live-execution panel; reported **0 broker order calls / 0 order endpoints**. `model_lab_smoke.mjs` additionally asserts the new Corp. Actions column renders `UNVERIFIED` and `SPLIT_ADJUSTED` rows |
| `compileall -q app desktop run.py axiom_launcher.py` | OK |
| `pip-audit -r requirements-lock.txt` | No known vulnerabilities found |
| `python -m build --wheel` | `arena-3.7.0a10-py3-none-any.whl` built from the final tree (artifacts not committed) |
| `python axiom_launcher.py --smoke` | **PASS** — `broker_order_calls: 0`, `default_live_execution: OFF`, `live_trading: false` |
| `python -m app paper-test` | `ok: true`, `paper_only.live_trading: false` |

Note on the drill: `tests/test_vantage_api.py` reappeared during the local recovery of
this branch (see §8, where it is recorded as DELETED). It exercised the removed
`/api/trading/*` live routes and could only fail; it was deleted again, which is why
the suite total is 1174 rather than 1184.

### 10.4 What these results do **not** establish

No real corporate-action feed was used, no broker or credential was touched, and no
order was submitted. Passing synthetic split/dividend arithmetic tests demonstrates
bookkeeping, provenance and look-ahead discipline only — **not** financial accuracy,
alpha or profitability. Outstanding limitations are listed in
`docs/CORPORATE_ACTIONS.md` §7 (cash-dividend booking is a research-ledger
responsibility, no tax/withholding or FX conversion, no intraday ex-date cut-off,
unsupported action classes as above).

### 10.5 Changed files

```
app/data/corporate_actions.py                 NEW  import format, validation, adjustment math, provenance
docs/CORPORATE_ACTIONS.md                     NEW  conventions, unsupported cases, limitations
tests/test_corporate_actions.py               NEW  56 core tests
tests/test_corporate_actions_integration.py   NEW  17 dataset/API/manifest/backtest/paper-guard tests
app/model_lab/datasets.py                     sidecar storage, provenance block, load_adjusted_dataset
app/model_lab/research_pipeline.py            manifest corporate-action provenance
app/ensemble/service.py                       status + disclosure from verified data
app/api/routes/model_lab.py                   import fields (no new route)
app/api/services.py                           raw-price guard on the paper replay loader
app/backtesting/engine.py                     basis recorded, double-count refusal, honest warnings
app/static/terminal/terminal.js               Corp. Actions column + provenance tooltip (no new screen)
app/static/terminal/index.html                datasets table header/colspan for the new column
tests/js/model_lab_smoke.mjs                  asserts the corporate-action cell renders
docs/ENSEMBLE_PROFITABILITY_EVIDENCE_AND_BENCHMARKS.md  corporate-action disclosure row updated
README.md                                     link to docs/CORPORATE_ACTIONS.md
tests/test_vantage_api.py                     DELETED again (stale post-removal suite)
```

**Verification for the next reader:** the corporate-action work is local commit
`60962fb74ae146b68cc185f4384cdda14edec89d` on `arena/8d3d005e-axiom`, a direct descendant of the reviewed base `f9a6409`.
Re-run
`git log -3 --format='%h %s'`, `python -m pytest -q tests/test_corporate_actions.py
tests/test_corporate_actions_integration.py` and the full suite before relying on
these numbers.

---

## 11. Windows packaged-release hardening (2026-11-06)

Scope: make the **already supported** Windows path (build → install → smoke →
uninstall) fail closed and prove its safety claims. No publishing, tagging,
pushing, signing or GitHub change was performed. The Windows acceptance itself
**could not run** here — see §11.5 for exactly which checks remain open.

### 11.1 Defects found and fixed

| # | Defect | Impact before the fix | Fix |
|---|---|---|---|
| 1 | `build_windows.ps1`, `install_test.ps1`, `portable_test.ps1` asserted the **unmounted** Vantage `/api/trading/*` surface (`status`, `connection`, `session`, `workspace`, `performance`) | Every packaged gate could "pass" on a 404; the released console was not actually verified | Repointed to `/paper/status`, `/paper/account`, `/api/performance`, `/api/market-data/status`, and added explicit gates that all seven `/api/trading/*` paths answer **404/405** |
| 2 | `Start-Axiom.ps1` did not accept `-Smoke`, although `README.md`, `USER_MANUAL.md` and `docs/OWNER_SETUP_GUIDE.md` document it | The documented smoke path aborted on parameter binding | `[Alias("Smoke")]` on the smoke switch, non-interactive by definition, interpreter/deps pre-check, exit status propagated with an explanatory line |
| 3 | A bundle missing a C extension failed deep inside a request (`ImportError` on `ssl` in the autonomy path) | Operator-visible 500 with no actionable message | `kronos_artifacts` imports `ssl` lazily and raises `ArtifactError` naming the missing module; `--runtime-check` + `--dpapi-selftest` verify a frozen build at build/install time; `axiom.spec` requests the extensions explicitly |
| 4 | The uninstaller would `DelTree` the data folder even if it were a junction/symlink | A redirected data path could delete an unrelated tree | Installer refuses to delete a reparse point, `[UninstallDelete]` stays empty, silent uninstall always keeps data |
| 5 | No automated check tied the Windows gates to the routes that actually ship | Route drift was invisible until a Windows run | `tests/test_windows_packaging_static_gates.py` builds the OpenAPI inventory and fails on any unmounted route, plus structural, installer, manifest and frozen-diagnostic checks |
| 6 | `SHA256SUMS.txt` was not traceable on its own; `build-windows.log` was never uploaded | A downloaded manifest could not be tied to a commit; the build transcript was lost | `#` header lines carry `commit`/`version`/`built` (ignored by `sha256sum -c`); the artifact now retains `build-windows.log` and the frozen smoke logs |

### 11.2 The contract the Windows gates now assert

- **Paper only, disarmed:** `/paper/status` → `state: STOPPED`, `paper_mode: true`,
  `live_trading: false`, `processed_bars: 0`. Anonymous requests → `401`.
- **Local simulated account:** `/paper/account` (cash/equity/currency), not a
  broker snapshot.
- **Bounded diagnostics:** `/api/performance` → `sample_capacity_per_metric: 512`
  and a note that refuses any zero-latency claim.
- **DPAPI present:** `/api/market-data/status` → `credential_storage` names
  Windows DPAPI; the feed is `read_only: true`, `execution_mode: AXIOM_PAPER`,
  `live_trading: false`; `/api/market-data/connect` without a DEMO profile
  fails closed with `409`.
- **Listed execution stays read-only:** `/api/listed-assets/status` and
  `/api/listed-assets/routing-policy` never create an order and never route to
  MT5/CFD.
- **Removed surface:** `/api/trading/{status,connection,session,workspace,
  orders/review,orders/submit,performance}` → `404`/`405`.
- **Frozen runtime:** `--runtime-check` imports `ssl/_ssl`, `hashlib/_hashlib`,
  `sqlite3/_sqlite3`, `ctypes.wintypes`, `zlib`, `lzma`, `bz2`, `zoneinfo`,
  `encodings.idna` and prints `commit`/`version`/`frozen`.
- **Synthetic DPAPI round trip:** `--dpapi-selftest` saves, reads, validates
  `encrypted_at_rest`, deletes, refuses an unlisted key, keeps the store in a
  temporary folder and reports `secret_echoed: false`.

The same paper-only contract is asserted by the **source** smoke
(`python axiom_launcher.py --smoke`, run as `.\Start-Axiom.ps1 -Smoke`), so a
route change fails on Linux/macOS before a Windows run is spent.

### 11.3 Validation actually executed locally (Linux, CPython 3.11.2, commit `4bf0d85`)

Environment: `.venv` installed from `requirements.txt` plus
`pyinstaller==6.16.0`, `pyinstaller-hooks-contrib==2025.9`, `pip-audit`, `build`
(`fastapi 0.162.0`, `starlette 1.10.0`, `httpx 0.28.1`, `uvicorn 0.54.0`).

| Check | Command | Result |
|---|---|---|
| Full Python suite | `python -m pytest -q` | **1215 passed, 5 skipped** in 117.08 s |
| Skips (all expected) | `-rs` | 2× real owner Alpaca credentials absent, 1× public instrument host unreachable, 2× Windows-only DPAPI |
| New static gates | `pytest tests/test_windows_packaging_static_gates.py` | 38 passed, 1 skipped (Windows DPAPI round trip) |
| Release guard | `pytest tests/test_windows_release_guard.py tests/test_windows_app_control_packaging.py tests/test_vantage_dpapi_windows.py` | 33 passed, 3 skipped |
| JavaScript suites | `npm install && npm test` | 9/9 harnesses PASS, **0 broker order calls / 0 order endpoints** |
| Compilation | `python -m compileall -q app desktop run.py axiom_launcher.py packaging/windows/axiom_launcher.py app/source_launcher.py` | exit 0 |
| Dependency audit | `pip-audit -r requirements-lock.txt` | no known vulnerabilities |
| Wheel | `python -m build --wheel` | `arena-3.7.0a10-py3-none-any.whl`, 776,576 bytes, sha256 `6d1c4bfeba916e03b4babaa3cbe21bacea45f5f951a1a901db331991b99f5991` (not committed) |
| Source smoke | `python axiom_launcher.py --smoke` | `status: PASS`, 11 checks true, `broker_order_calls: 0`, `user_data_modified: false` |
| Frozen runtime diagnostic | `python packaging/windows/axiom_launcher.py --runtime-check` | `status: PASS`, all modules importable, exit 0 |
| Frozen DPAPI diagnostic | `python packaging/windows/axiom_launcher.py --dpapi-selftest` | `status: UNAVAILABLE`, exit 3 (no DPAPI on Linux — the documented branch), `isolated_directory: true`, `secret_echoed: false` |
| No-OpenSSL chain | import `app.autonomy.kronos_runtime` with `ssl`/`_ssl` blocked | imports cleanly; `tls_context()` raises `ArtifactError` naming `ssl` |

Paper/live separation: `mode: PAPER`, `live_trading: false`,
`default_live_execution: OFF`, both markets `NOT_AVAILABLE`, zero broker order
calls in every suite above. No credential was read, no broker was contacted and
no user profile was modified by these checks.

### 11.4 Starlette 1.x TestClient / HTTPX review (development item 7)

Pins: `starlette==1.10.0`, `httpx==0.28.1`, `fastapi==0.162.0`. The full suite
(1215 tests, most of them through `fastapi.testclient.TestClient`) passes. The
only signal is a `StarletteDeprecationWarning` that `starlette.testclient` would
prefer a newer HTTPX client package. That is a deprecation notice from a
transitive library, not a failure, and adopting a different HTTP client library
is an unrelated migration. **No dependency was changed.** Pin review should be
repeated when `fastapi` raises its minimum `starlette`.

### 11.5 What could NOT be verified here (and how to verify it)

This sandbox is Linux. `pwsh`, `wine`, `iscc`/`makensis` and real DPAPI are all
absent, so the following **were not run** and are *not* claimed:

1. `build_windows.ps1` end-to-end (PyInstaller onefile + onedir, Inno compile).
   PyInstaller cannot even start in this sandbox — its analysis aborts with
   `ERROR: Python shared library ('libpython3.11.so.1.0') was not found!`
   because the local CPython is not built with `--enable-shared`. The spec's
   own build-time decisions are instead executed by
   `tests/test_windows_packaging_static_gates.py` (all 8 reviewed DEMO fixtures
   resolve, every literal `ROOT / ...` payload path exists, no recursive
   `Tree()`/`collect_data_files` of `data/`), which catches missing-resource and
   path-assumption defects but not native-binary collection.
2. `install_test.ps1` (install, launch, gates, upgrade, uninstall).
3. `portable_test.ps1` (extracted onedir acceptance).
4. `--runtime-check` / `--dpapi-selftest` inside a frozen Windows build.
5. Real Windows DPAPI save/read/delete under a disposable profile.

Reproducible path (unchanged by this work, requires a Windows x64 runner with
CPython 3.11, Inno Setup 6, Node 20 and the repo `.venv`):

```powershell
# from a clean checkout of this exact commit
.\packaging\windows\build_windows.ps1 -ExpectedCommit <exact-sha> -Version 3.7.0a10
.\packaging\windows\install_test.ps1 -DistDir packaging\windows\dist -SetupPath <setup.exe>
.\packaging\windows\portable_test.ps1 -PackagePath <portable.zip> -ExpectedCommit <exact-sha>
```

or dispatch `windows release` (`.github/workflows/windows-release.yml`) with
`publish: false`, which now also triggers on `arena/8d3d005e-axiom` and retains
`build-windows.log`. Required artifact checks after a run:
`SHA256SUMS.txt` header names the same commit as `SOURCE_COMMIT.txt` and
`BUILD_METADATA.json`; `install-test-log.txt` contains `PASS` rows for the DPAPI
self-test, the runtime imports, the seven removed-route gates and the
uninstall-preservation checks; `secret_echoed` is `false` everywhere.

Static PS1 analysis is not equivalent to executing it: the new tests prove
balance, routing, wiring and intent, not Windows behaviour.

### 11.6 Changed files (this pass)

```
packaging/windows/axiom_launcher.py            --runtime-check, --dpapi-selftest
packaging/windows/PaperSmoke.ps1               Test-AxiomRuntimeImports, Test-AxiomDpapiStorage
packaging/windows/build_windows.ps1            paper-only routes, removed-route gates, commit-bound manifest
packaging/windows/install_test.ps1             same gates + uninstall preserves unrelated files
packaging/windows/portable_test.ps1            same gates
packaging/windows/axiom.spec                   explicit runtime C extensions
packaging/windows/axiom.iss                    junction/symlink guard when deleting operator data
packaging/windows/diagnostics/README.md        NEW historical-evidence note
Start-Axiom.ps1                                -Smoke alias, diagnostics, exit status
app/source_launcher.py                         source smoke asserts the same contract
app/autonomy/kronos_artifacts.py               lazy ssl + actionable ArtifactError
.github/workflows/windows-release.yml          branch, commit-bound manifest, retained build log
tests/test_windows_packaging_static_gates.py   NEW 38 static/behavioural gates
tests/test_windows_release_guard.py            branch, manifest header, build-log retention
docs/WINDOWS_APP_CONTROL.md                    self-check, route, uninstall, checksum sections
```

**Verification for the next reader:** the packaging code change is local commit
`4bf0d85e03b2bfc3cae23d50fd48cc997d02ea54` on `arena/8d3d005e-axiom`, a direct
descendant of `b821c8e` (which is `f9a6409` + the corporate-action work). The
spec-input tests, the §11.4 review note and this section are follow-up commits on
the same branch; use `git log -3 --format='%h %s'` for the exact HEAD. All
changes are local: nothing was pushed, merged, tagged or published. **No Windows
release was built, uploaded or published by this work.**

---

## 12. Upstox / Alpaca market-data conformance suite (2026-11-06)

Scope: an automated, **read-only** conformance suite for the two market-data
adapters and their path into the local paper engine. No network call, no broker
connection, no credential read and no order was performed by this work.

### 12.1 What exists now

* `app/data/conformance.py` — the suite plus the machine-readable report
  (`schema: axiom.provider-conformance.v1`, `suite_version`, `data_source_version`,
  per-provider `status`/`capabilities`/`checks`/`limitations`, `cross_market`,
  `read_only_requests`, `capability_summary`, `unverified`, `redaction`).
* `python -m app provider-check` — operator command. Offline by default (zero
  socket, zero credential read, no write except an explicit `--output`).
  `--live` is the only mode that opens a socket: one documented read-only `GET`
  per provider, no retries, no order/account/position endpoint, nothing stored.
  Exit codes `0` PASS / `1` FAIL / `3` UNVERIFIED.
* `tests/test_provider_conformance.py` — 34 deterministic tests, including the
  zero-order-call invariant, cross-market isolation, secret redaction and a
  monkeypatched-HTTP proof that the default path cannot reach the network.
* `docs/PROVIDER_CONFORMANCE.md` — the case table, report format, exact
  permissions per mode, exit codes and remaining owner-machine checks.

### 12.2 Cases covered

Both providers: valid and malformed quotes, candles and historical bars;
timezone-awareness (UTC), timestamp ordering, duplicate and missing bars, stale
data, zero/negative/non-finite values, partial responses, provider error
statuses (401/403/429/500), transport timeouts, bounded (non-)retries and
fail-closed refusal of invalid or stale responses. Upstox: instrument-key
resolution (`NSE_EQ|…` ⇄ `NSE_EQ-…`, index keys refused), `ltpc`/`full` decoding,
official interval bars, market-status gating seam. Alpaca: IEX/SIP/delayed feed
identity on both label and request, paginated history with exclusive end, stream
decode rules (bars only, corrections ignored).

Path into paper: each adapter's normalized output is fed to the real
`LiveMarketPaperEngine.analyze_market`, which reports `SUCCEEDED` + `FRESH` for
valid data and explicit codes (`STALE_QUOTE`, `NO_TRADED_PRICE`,
`MALFORMED_QUOTE`, `AUTH_REJECTED`, `RATE_LIMITED`, `DISCONNECTED`) otherwise,
always with `broker_order_api_calls: 0` and `trade_eligible_now: false`.

Cross-market isolation: with a raising Upstox quote provider and a healthy Alpaca
provider, one concurrent cycle completes with `NASDAQ COMPLETED` and its decision
produced while `NSE` records `quote DISCONNECTED`; the cycle's
`broker_order_api_calls` is `0` and `live_trading_enabled` is `false`.

Corporate actions: a provider-normalized dataset imported with a corporate-action
book keeps `status`/`provider`/`actions_sha256` in its manifest
(`SPLIT_ADJUSTED`), the stored dataset stays at raw provider prices, and the
derived research series is refused by the execution guard
(`AdjustedSeries.for_execution` / `assert_raw_candles`).

### 12.3 Two adapter defects found and fixed

| Defect | Before | After |
| --- | --- | --- |
| `AlpacaStream.decode` raised `KeyError`/`TypeError` for a truncated bar | A corrupt stream bar surfaced as an unspecified `KeyError` | Raises the documented `ValueError("Alpaca stream bar is missing or malformed fields")` |
| `LiveMarketPaperEngine._classify_fetch_exception` classified Upstox typed read errors by text only | An illiquid/untraded snapshot (`NoTradedQuote`) was reported as `DISCONNECTED`, hiding the real cause | `NoTradedQuote → NO_TRADED_PRICE`, `InvalidQuote → MALFORMED_QUOTE`, `UpstoxAuthError → AUTH_REJECTED`, then the existing text fallbacks |

Both changes are additive; no safety gate was relaxed and no new route or screen
was added.

### 12.4 Validation actually executed (Linux, CPython 3.11.2, branch `arena/8d3d005e-axiom`)

| Check | Command | Result |
|---|---|---|
| Conformance suite (library) | `run_conformance()` | `status: PASS`, 16 Upstox + 14 Alpaca + 7 cross-market/corporate checks, `network_calls: 0`, `broker_order_api_calls: 0` |
| Conformance tests | `python -m pytest -q tests/test_provider_conformance.py` | 34 passed |
| Operator command, offline | `python -m app provider-check` | `status: PASS`, exit 0 |
| Operator command, JSON + file | `python -m app provider-check --json --output …` | report written, exit 0, `secrets_found: false` |
| Live probe without credentials | `python -m app provider-check --live` | `UNVERIFIED`, `network_calls: 0`, exit 3 (no socket opened) |
| Filtered run | `python -m app provider-check --provider alpaca` | `[alpaca] PASS`, no Upstox section |
| Paper/live separation | suite + cycle output | `live_trading: false`, `broker_order_api_calls: 0`, `trade_eligible_now: false` on every refusal path |

| Full Python suite | `python -m pytest -q` | **1249 passed, 5 skipped** in 129.60 s |
| JavaScript harnesses | `npm test` | 9 suites PASS, 0 broker order calls / 0 order endpoints |
| Compilation | `python -m compileall -q app desktop run.py axiom_launcher.py packaging/windows/axiom_launcher.py` | exit 0 |
| Dependency audit | `pip-audit -r requirements-lock.txt` | no known vulnerabilities |
| Wheel | `python -m build --wheel` | `arena-3.7.0a10-py3-none-any.whl`, 795,133 bytes, sha256 `4ceb39fab42d75838354dae1c6ccdf94ae9ec98e328d06e591c38316b09a2195` (not committed) |
| Source smoke | `python axiom_launcher.py --smoke` | `status: PASS`, 11 checks, `broker_order_calls: 0`, `user_data_modified: false` |
| Deterministic paper self-test | `python -m app paper-test` | `ok: true`, `paper_only.live_trading: false` |

The five skips are the pre-existing ones (2× real owner Alpaca credentials
absent, 1× public instrument host unreachable, 2× Windows-only DPAPI).

### 12.5 Nothing claimed about real providers

This suite contacts no provider. It therefore makes **no** claim about Upstox or
Alpaca connectivity, entitlement, feed coverage, data quality, latency or
strategy profitability. Live connectivity is reported as `UNVERIFIED` in every
report, with the exact command an owner can run on their own machine
(`python -m app provider-check --live …`). Native Windows package acceptance
remains **unverified** until it is run on Windows (§11.5).

**Verification for the next reader:** the suite, the CLI, the two adapter fixes
and its tests landed as local commit `ca5b4be5c40fe3b137f05468646c75a3b7369092`
on `arena/8d3d005e-axiom`, a direct descendant of `8106a67`. Re-run
`python -m pytest -q tests/test_provider_conformance.py`,
`python -m app provider-check` (expect `PASS`, exit 0, `network_calls: 0`) and
`python -m app provider-check --live` **without** credentials (expect
`UNVERIFIED`, exit 3, `network_calls: 0`). Nothing was pushed, merged, tagged or
published, and no provider was contacted.

---

## 13. Strategy research qualification gate (2026-11-06)

Scope: connect the existing research safeguards (chronological splits, walk-
forward, untouched holdout, multiple-testing haircut, cost stress, dataset and
corporate-action provenance) into one enforceable, machine-readable
qualification decision. Offline only: no credential, no provider call, no broker
connection, no order, no live trading.

### 13.1 What exists now

* `app/model_lab/qualification.py` — the gate. 20 requirements
  (`app.model_lab.qualification.RULE_SPECS`), a published rule document, an
  immutable record (`axiom.research.qualification.v1`) with `record_sha256`,
  `verification_level`, per-requirement evidence, explicit `blocked` /
  `unverified` reason codes, `execution_authority: NONE`, and the execution
  guards `qualification_authority` / `assert_no_execution_authority` /
  `require_qualified`.
* `app/model_lab/provenance.py` — software fingerprint (git commit read from
  `.git/HEAD` or `packed-refs`, cleanliness, package version, python) pinned into
  every manifest; a dirty tree is reported as such, never as clean.
* `app/model_lab/research_pipeline.py` — extended, not duplicated:
  * `MA_GRID` / `RSI_GRID` / `RIDGE_GRID` and `declared_search_family()` fix the
    search family **before** evaluation (15 variants, +1 for a genuine Kronos
    ablation);
  * the existing `multiple_testing_sharpe_haircut` produces the family-wide
    threshold, is recorded in `search_family.correction`, and appears as a
    rejection reason for tuned candidates that do not clear it;
  * the manifest now also pins `universe`, `data_range`, `actions_sha256`,
    `benchmark`, `strategy_versions`, `software` and `search_family`;
  * `manifest_core()` / `manifest_digest()` make the digest re-derivable.
* `app/model_lab/registry.py` — a new `model_lab_qualifications` table stores one
  immutable record per completed run; a gate failure stores a BLOCKED
  `QUALIFICATION_ERROR` record rather than nothing. Run rows, model rows,
  `get_run`, `list_runs` and `overview` expose the verdict.
* `app/api/routes/model_lab.py` — `GET /qualification/rules`,
  `GET /runs/{run_id}/qualification`.
* `app/__main__.py` — `python -m app research-qualify` (exit 0 QUALIFIED /
  1 BLOCKED / 3 UNVERIFIED / 2 usage), read-only.
* Terminal → Model Lab: a Qualification column in the run table and a verdict
  banner + detail list in the run-inspection panel. No new screen.
* `docs/RESEARCH_QUALIFICATION.md` — the exact rules, thresholds, reason codes,
  surfaces, commands and limitations.

### 13.2 Rules (summary — the normative table is in RESEARCH_QUALIFICATION.md)

Verdict is `BLOCKED` if any requirement failed, `UNVERIFIED` if nothing failed
but evidence is missing, `QUALIFIED` only when all 20 pass. Data: identity, hash
re-verification, real-data provenance, corporate-action status, provider
conformance, recency. Method: chronological splits with embargo and an untouched
holdout, minimum observations, declared search family, multiple-testing
correction, out-of-sample consistency, the pipeline's own gate, benchmark.
Costs: positive spread/slippage, ≥ 1.25× stress, positive stressed return. Risk:
holdout drawdown within 25 %. Repro: strategy/parameter/feature identity,
universe, software commit, manifest integrity. Synthetic, unverified, stale,
future-dated and corporate-action-incomplete data cannot qualify.

### 13.3 Tests (deterministic, offline)

`tests/test_research_qualification.py` — 44 tests: split ordering/embargo; a
holdout-mutation invariance test proving feature scaling, selected parameters and
the full validation ranking do not move when every holdout bar is tripled;
pinned hashes/versions/commit; synthetic → `SYNTHETIC_DATA`; unattested real data
→ `PROVENANCE_ATTESTATION_MISSING`; stale/future data → `STALE_DATA`; missing and
unsupported corporate actions; tampered manifest → `MANIFEST_TAMPERED`; family
completeness and correction coverage; candidate below the haircut; missing costs,
benchmark, folds, observations and drawdown breaches; the qualified mechanics
path with `execution_authority: NONE`; a forged record claiming live authority
being rejected; promote/arm refusal; identical re-evaluation digests; the API and
CLI surfaces.

### 13.4 Validation executed (Linux, CPython 3.11.2, branch `arena/8d3d005e-axiom`)

| Check | Command | Result |
|---|---|---|
| Qualification tests | `python -m pytest -q tests/test_research_qualification.py` | 44 passed |
| Research/Mode-Lab regression | `pytest tests/test_research_pipeline_leakage.py tests/test_kronos_model_lab.py tests/test_v2_datasets_research.py tests/test_final_research.py tests/test_autonomy_research.py tests/test_research_candidate_cli.py` | 71 passed |
| Live run through the registry | bundled synthetic dataset, `async_execution=False` | `COMPLETED`, verdict `BLOCKED` (`SYNTHETIC_DATA`, `STALE_DATA`), models `execution_authority: NONE` |
| CLI, stored run | `python -m app research-qualify --run <id> --store <dir>` | `BLOCKED`, exit 1 |
| CLI, manifest file | `python -m app research-qualify --manifest … --json` | full record, exit 1/3 never 0 without evidence |

| Full Python suite | `python -m pytest -q` | **1293 passed, 5 skipped** |
| JavaScript harnesses | `npm test` | 9 suites PASS, 0 broker order calls, 0 order endpoints |
| Compilation | `python -m compileall -q app desktop run.py axiom_launcher.py packaging/windows/axiom_launcher.py` | exit 0 |
| Dependency audit | `pip-audit -r requirements-lock.txt` | no known vulnerabilities |
| Wheel | `python -m build --wheel` | `arena-3.7.0a10-py3-none-any.whl`, 818,465 bytes, sha256 `2c576e4973a8a1fa3b9b9ae8bec78c9ad23f5d4f02fbe2d15c544645e33ba89d` (not committed) |
| Source smoke | `python axiom_launcher.py --smoke` | `status: PASS`, 11 checks, `broker_order_calls: 0`, `live_trading: false`, `user_data_modified: false` |
| Deterministic paper self-test | `python -m app paper-test` | `ok: true`, `paper_only.live_trading: false` |

One self-caused failure was found and fixed during the sweep: the repository's
static scan rejects `except OSError: pass`, which `app/model_lab/provenance.py`
used when a loose git ref is unreadable; it now falls back to `packed-refs`
explicitly. No other suite needed changes beyond the new tests.

### 13.5 What is still not claimed

* No provider was contacted and no credential was read: live connectivity,
  entitlement and production data quality remain **unverified**.
* Provenance is operator-attested, not independently audited. The record embeds
  `INDEPENDENT_PROVENANCE_REVIEW` in `open_reviews` and states the verification
  level explicitly.
* Nothing here supports a profitability, slippage, capacity or drawdown claim;
  the qualification record itself repeats that limitation.
* The qualified mechanics fixture in the tests uses synthetic prices registered
  as an imported capture: it proves the gate's wiring, not that any strategy is
  good. Real qualification requires a real captured dataset with a verified
  corporate-action book, a passing provider-conformance report and a clean-tree
  commit.

**Verification for the next reader:** the gate, the pipeline/registry/API/UI
changes and the tests landed as local commit
`d85b6d1434010a69a7ceca8ea5b96d830b3e9b52` on `arena/8d3d005e-axiom`, a direct
descendant of `d434cc5`. Re-run
`python -m pytest -q tests/test_research_qualification.py`,
`python -m app research-qualify --run <completed run_id> --store <model_lab dir>`
(expect `BLOCKED` with `SYNTHETIC_DATA` for a bundled fixture, exit 1) and
`GET /api/model-lab/qualification/rules`. Nothing was pushed, merged, tagged or
published; no provider was contacted and live trading stayed disabled.

## 14. Portable research-run bundles (2026-11-07)

### 14.1 What exists now

A completed run can leave the machine with its evidence intact and be checked
by somebody who does not have AXIOM's store, its dataset directory, or a
network connection.

* `app/model_lab/bundle.py` — `export_run_bundle`, `write_bundle`,
  `verify_bundle`, `replay_bundle`, three containers (`DirectoryContainer`,
  `ZipContainer`, `SealedContainer`), a single published `REASON_CODES` table
  and `BundleLimits`.
* Content addressing: every payload is stored as `artifacts/<sha256>.<ext>`;
  the index enumerates each file's role, path, SHA-256, size, media type and
  requiredness, and `bundle_sha256` covers the entire index.
* The bundle pins the canonical run manifest, dataset identity and bars, the
  corporate-action sidecar (when the dataset has one), run config, feature
  schema, benchmark, universe, folds and variant definitions, dependency lock,
  software commit/cleanliness, run outputs, provider-conformance evidence,
  qualification inputs and the qualification record.
* Verification is offline and total: a single finding fails the bundle. It
  re-derives the manifest digest, the bars hash and the **whole qualification
  record**, and rejects missing, duplicate, unreferenced, oversized,
  traversing, symlinked, non-content-addressed and credential-bearing
  artifacts with specific reason codes.
* Replay re-runs `execute_research_pipeline` in-process from the bundle's own
  artifacts, reports `REPRODUCED` / `REPRODUCED_WITH_ENVIRONMENT_DRIFT` /
  `DIVERGED` / `UNVERIFIED` at a documented tolerance (default `1e-9`), lists
  environment drift field by field, and refuses to replay Kronos-evaluated runs
  rather than substituting a mock.
* Surfaces: `python -m app research-bundle export|verify|replay`;
  `GET /api/model-lab/bundles/format`, `POST /api/model-lab/runs/{id}/bundle`,
  `POST /api/model-lab/bundles/verify`, `POST /api/model-lab/bundles/replay`.
* `docs/RESEARCH_RUN_BUNDLE.md` — the format, the compatibility rules, the full
  reason-code table, the attack models that are rejected, the commands and the
  limits.

This is an extension of the existing research pipeline, qualification gate,
corporate-action sidecars and software provenance. It introduces no second
research engine and no second qualification rules: verification calls
`evaluate_qualification` and compares the result with the bundled record.

### 14.2 What a bundle cannot do

A bundle never upgrades a qualification, never grants execution authority, never
arms paper trading, never places an order, never contacts a provider, and never
claims profitability. The replay report carries
`qualification.effective_status = worse_of(bundled, re-derived)`,
`upgraded: false`, `execution_authority: "NONE"` and `live_trading: false`. A
bundled synthetic fixture exports, verifies and replays as `BLOCKED`
(`SYNTHETIC_DATA`, `STALE_DATA`) at every step.

Hashes are integrity evidence: they prove the bytes are the bytes that were
bundled. They do not prove the provider's data is genuine, and they do not
upgrade `OPERATOR_ATTESTED_NOT_INDEPENDENTLY_VERIFIED` to an independent review —
that remains in the record's `open_reviews`. The document states this in
`integrity_scope` so no reader has to infer it.

### 14.3 Tests (deterministic, offline)

`tests/test_research_bundles.py` — 65 tests: deterministic export; a complete
artifact table with sizes and hashes; the pinned inputs re-checked; no
credential-shaped payload can be exported (`SECRET_DETECTED`, masked);
verification across sealed, directory and zip containers and after a round trip
through disk; raw tampering with **every required role** plus the optional
corporate-action sidecar; re-signed tampering of the manifest, the bars, the run
config, the software fingerprint, a section pin and the index run block; missing
roles, missing files, duplicate roles/paths and unreferenced files; path
traversal (`..`, absolute, percent-encoded, backslash), a zip-slip member, a
symlinked artifact and a non-content-addressed path; newer `format_version` and
a foreign schema; artifact-size, artifact-count and total-size limits; a forged
`QUALIFIED` record both naive and fully re-signed (still `QUALIFICATION_ALTERED`);
a record claiming execution authority; the preserved attestation level; replay
reproducing the pinned digest, never upgrading, reporting drift, refusing an
unverified bundle, catching substituted bars, honouring tolerance and returning
`REPLAY_REQUIRES_KRONOS` for a Kronos run; zero sockets during export, verify and
replay (with `socket.connect` patched to raise) and no order surface in the
module; and CLI/API round trips including the failure exit code and the `404`.

### 14.4 Validation executed (Linux, CPython 3.11.9, branch `arena/8d3d005e-axiom`)

| Check | Command | Result |
| --- | --- | --- |
| Bundle tests | `python -m pytest -q tests/test_research_bundles.py` | 65 passed |
| Export from a real run | bundled synthetic dataset, `async_execution=False` | 10 artifacts, sealed document, `bundle_sha256` `5e4e9aa1…` (first export) |
| Verify | `python -m app research-bundle verify --bundle <dir>` | `PASS`, 10 artifacts, record `042a8730…` re-derived `match=True` |
| Replay | `python -m app research-bundle replay --bundle <dir>` | identical manifest `54030b3d…`, `BLOCKED` → `BLOCKED`, `upgraded: false` |
| CLI/API | `research-bundle export/verify/replay`, `POST /api/model-lab/bundles/*` | round trip OK, `404` for an unknown run, `413` for an oversized body |

### 14.5 What is still not claimed

* No real provider dataset was bundled. Every bundle in this pass came from the
  bundled synthetic fixture, which is ineligible by design; a real bundle would
  still be `UNVERIFIED` until the operator supplies an attested capture.
* Replay reproduces the research pipeline, not the original database; it cannot
  validate a claim that the capture was honest.
* Bundle integrity depends on the recipient learning `bundle_sha256` out of
  band. There is no signing key and no bundle-level PKI.
* Kronos-evaluated runs remain unreplayable offline.
* Native Windows packaging of the new commands is unverified; only Linux was
  exercised. The commands use the standard library plus the existing app.

**Verification for the next reader:** the module, the CLI, the API routes, the
tests and the documentation landed as local commit `ca6f53f` on
`arena/8d3d005e-axiom`, on top of `d912770`. Re-run
`python -m pytest -q tests/test_research_bundles.py`, then
`python -m app research-bundle export --run <completed run_id> --store <model_lab dir> --output ./b && python -m app research-bundle verify --bundle ./b && python -m app research-bundle replay --bundle ./b`.
Nothing was pushed, merged, tagged or published; no provider was contacted and
live trading stayed disabled.
