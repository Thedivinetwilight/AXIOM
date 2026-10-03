# Arena — Full-Codebase Debug Report

**Date:** 2026-10-03
**Repository:** `Thedivinetwilight/AXIOM` · branch `arena/01a0fcfb-axiom`
**Baseline commit:** `733c2a8` (HEAD == remote at start of pass)
**Scope:** the entire repository — not just the test suite. Every Python module,
every CLI entry point, every mode in `run.py`, static analysis, and the security
scanner.
**Verdict:** **12 defects found and fixed. Suite is fully green. No trading
behaviour changed** — the strategy determinism fingerprint is byte-identical
before and after the entire pass.

---

## 1. Summary

| # | Defect | Severity | Impact | Status |
|---|--------|----------|--------|--------|
| 1 | `cmd_paper_live` — `NameError: argv` | **Critical** | Command crashed instantly | Fixed |
| 2 | `cmd_research` — `NameError: BacktestEngine` | **Critical** | Command crashed instantly | Fixed |
| 3 | `--periods` iterated as a string | **High** | Silent wrong maths | Fixed |
| 4 | `dataset audit` — raw `ValueError` | High | Traceback instead of message | Fixed |
| 5 | `cmd_discover` — wrong constructor signature | **Critical** | Command crashed instantly | Fixed |
| 6 | `StrategyLifecycleManager.degrade()` missing | High | Documented operation absent | Fixed |
| 7 | `UpstoxOAuthError` family uncaught | High | Traceback on ordinary auth failure | Fixed |
| 8 | Naive `date.today()` × 5 sites | **High** | Wrong date on non-UTC hosts | Fixed |
| 9 | Late-binding closure in scanner | Medium (latent) | Silent wrong scores if deferred | Fixed |
| 10 | Escape sequence `\/` in dashboard | Medium (latent) | Future `SyntaxError` on import | Fixed |
| 11 | Generated protobuf file stripped by lint | **Critical (self-inflicted)** | Broke feed decoding | Caught & reverted |
| 12 | Dead code / unused imports | Low | Maintenance noise | Cleaned |

Defects 1–7 were the "eight defects" of the initial bug hunt (the eighth,
`--timeframe 1D` on minute data, was investigated and is **not a bug** — see
§4). Defects 8–12 surfaced in deeper passes: the timezone bug by running an
unusually broad lint rule set, the closure and escape issues by reading the
warnings instead of silencing them, and #11 was **caused by my own cleanup** and
caught by the test suite.

---

## 2. Method

Four independent instruments were used, because each catches a different class
of defect:

1. **Static analysis** — `compileall` across all targets, an import walk of every
   module, and `ruff`.
2. **CLI execution harness** — 51 command-line paths exercised end-to-end
   (`/tmp/smoke.sh` 31 paths, `/tmp/smoke2.sh` 20 paths), checking for
   tracebacks rather than merely for exit codes.
3. **The test suite** — 1302 tests.
4. **Behavioural tools** — `security_scan.py`, `determinism_check.py`,
   `paper-test`, `live-drill`.

Crucially, **counting crashes is not enough**: defects 3 and 8 exit cleanly with
code 0 and produce plausible-looking output. They were found by reading the
code, not by running it. Equally, the static sweep had to be run *twice* — a
second, broader rule set was what surfaced defect 8, which the first pass's
narrow rule selection could not see.

---

## 3. Findings

### Defect 1 — `paper-live` crashed with `NameError: argv`

**Symptom**
```
$ python -m app paper-live -- --max-candles 5
NameError: name 'argv' is not defined
```

**Cause.** The handler passed an argument name that was never in scope; the
value it wanted had been renamed earlier in the function.

**Fix.** Referenced the in-scope argument.

**Verification.** `paper-live` now runs the live-style loop against recorded
candles and produces a full JSON result.

---

### Defect 2 — `research run` crashed with `NameError: BacktestEngine`

**Symptom**
```
$ python -m app research run --periods 2,3
NameError: name 'BacktestEngine' is not defined
```

**Cause.** `BacktestEngine` is a valid class in the codebase but was never
imported into `app/__main__.py`. The line only executes when a research run
reaches the scoring stage, so `--help` and dry syntax checks looked fine.

**Fix.** Added the import in the correct place.

**Verification.**
```
$ python -m app research run --unit minutes --interval 1 \
    --from-date 2026-01-01 --to-date 2026-01-02 \
    --timeframe 1m --periods 2,3
```
→ full experiment JSON, exit code 0.

---

### Defect 3 — `--periods` was iterated as a *string*

**Severity note: this is the most dangerous class of bug found.** It crashes
nothing and reports no error. It silently produces wrong numbers.

**Cause.** `--periods 2,3` arrives as the single string `"2,3"`. The code
iterated it directly, so `for period in "2,3"` yielded `"2"`, `","`, `"3"` — the
comma was treated as a period. Periods are the windows over which strategy
performance is measured, so the resulting metrics were quietly invalid.

**Fix.** Split on commas, parse each element as an integer, then iterate the
list.

**Also fixed in the same area:** the validation ran *after* an early exit for
"no local candles", so a malformed `--periods` produced a confusing
"no candles" error instead of naming the real problem. Validation was hoisted
**ahead of all I/O**, so malformed input is refused immediately with the right
message and exit code 2, regardless of data state. Parsing now happens once and
the result is reused.

**Verification.**
```
$ python -m app research run --periods 2,abc
{"ok": false, "error": "--periods must be a comma-separated integers ..."}
$ echo $?
2
```

---

### Defect 4 — `dataset audit` emitted a raw traceback

**Cause.** An argument was optional in the parser but required by the handler;
the resulting `ValueError` escaped to the top level.

**Fix.** The handler now validates its own inputs and returns a usage error with
a concrete remedy and exit code 2.

---

### Defect 5 — `discover` crashed with a `TypeError`

**Cause.** The call site was written against an older API shape: it passed an
`engine` where the constructor now takes `settings`, called `discover()` with
the wrong argument order, and read a `qualified` key that had been removed from
the result. Three separate mismatches in one call, all invisible until run.

**Fix.** Rewrote the handler against the real API.

**Verification.** On a seeded window → `{"candidates": 12, "qualified": 0}`, exit
code 0.

---

### Defect 6 — `StrategyLifecycleManager.degrade()` did not exist

**Cause.** Strategy lifecycle management documented four states and an operator
command to move a strategy down them, but only the promotion direction was
implemented. Calling the downgrade path failed.

**Fix.** Implemented `degrade()` as the exact inverse of `promote()`, walking
`ACTIVE → PROBATION → SHADOW → RETIRED`, with `RETIRED` terminal.

**Verification.** Full ladder tested; the terminal state is asserted in the
regression suite.

---

### Defect 7 — Authentication failures escaped as tracebacks

**Cause.** `UpstoxOAuthError`, `UpstoxAuthError`, `SandboxAuthError` and
`SandboxRequestError` are **ordinary, expected outcomes** — a daily-expiring
token guarantees they happen routinely. They fell through to the interpreter's
default handler, so the operator saw a stack trace instead of the reason.

**Fix.** Added an operator-error boundary. The important part is the *scope*:
this was verified to catch **exactly those four types and nothing else**. In
particular `ValueError`, `TypeError` and `AttributeError` are *not* subclassed,
so genuine programming bugs still produce a full traceback. A blanket
`except Exception` would have converted every future bug into a friendly
message — turning a detectable crash into a silent wrong answer.

**Verification.**
```
$ python -m app upstox-login
{"ok": false,
 "error": "Set UPSTOX_CLIENT_ID, UPSTOX_CLIENT_SECRET and UPSTOX_REDIRECT_URI ...",
 "error_type": "UpstoxOAuthError"}
$ echo $?
2
```
No traceback, no credential values printed.

---

### Defect 8 — Naive `date.today()` in a UTC-only system

**Found by:** running an unusually broad lint rule set, which flagged
`DTZ011` at five sites.

**Cause.** Every trading day, report window and history bound in this codebase
is defined in UTC — `datetime.now(timezone.utc)` appears in **36 modules**. Five
call sites used `date.today()`, which returns the **local** calendar date. On a
host set to IST (UTC+5:30 — the natural choice for Indian equities) the local
date is *already tomorrow* between 00:00 and 05:30 local time, while market data
is still the UTC day. Affected: the weekly report window (both implementations),
the daily snapshot date, the default `history sync --to-date` bound, and the
weekly PDF filename.

**Why this matters.** `--to-date` silently targeting tomorrow means a sync asks
the broker for a day that has not happened; the weekly report silently covers
the wrong seven days. Both produce perfectly plausible output.

**Fix.** Added `app/utils/clock.py` — a single clock module with `utc_now()`,
`utc_today()` and `utc_today_iso()`, each documented with the reasoning above —
and routed all five sites through it. Two imports that became unused were
removed. A regression test asserts **no** `date.today()` remains anywhere in
`app/`, so the convention cannot drift back.

---

### Defect 9 — Late-binding closure in the scanner funnel

**Found by:** lint rule `B023`.

**Cause.** Inside a `for u in stage:` loop, a closure captured the loop variable
`u` rather than binding it. This is the classic Python late-binding trap.

**Why it is only *latent*.** `_cached_feature()` calls the closure synchronously
on a cache miss and stores only the resulting *value*, never the callable. So
the late binding currently yields the correct answer. But it is one refactor
away from silently scoring every instrument in the universe using whichever
candidate the loop finished on — a wrong answer with no error, in the component
that ranks the whole market.

**Fix.** Bound the loop variable as a default argument (`def compute(candidate=u)`),
with a comment explaining exactly why. Zero behavioural change, hazard removed.

**Verification.** With five candidates closing at 100…500, the top-3 are exactly
`[500, 400, 300]` on both the default and the `deep_scorer` path — proving each
closure sees its own candidate.

---

### Defect 10 — Invalid escape sequence in the dashboard template

**Symptom.** `DeprecationWarning: invalid escape sequence '\/'` while parsing
app sources.

**Cause.** The dashboard's inline HTML/JS page is a non-raw Python string
containing the JavaScript regex `/<\/b>/`. A single-backslash `\/` is not a
recognised Python escape. Python currently preserves it, so the page renders
correctly — **but this becomes a hard `SyntaxError` in a future Python**, at
which point the dashboard module stops importing entirely.

**Investigation note.** An earlier automated backslash scanner pointed at the
wrong line and was untrustworthy inside a multi-line string; the location was
pinned down by compiling individual files and reading `warning.lineno`. The
`\/` is deliberately meaningful to JavaScript, so the fix had to preserve it
exactly.

**Fix.** Doubled the backslash in the Python source (`\\/`), which the language
collapses to the single `\/` that JavaScript needs.

**Verification.** Zero invalid-escape warnings remain across `app/` and
`tools/`; the module imports; the served JS regex is confirmed **byte-identical**
to before (`/\<b\>.*<\/b\>/` — exactly one backslash). A regression test now
asserts both the absence of all invalid escapes and the exact served output.

---

### Defect 11 — An automated cleanup broke the feed decoder

**This one was self-inflicted, and the test suite caught it.** Recorded here in
full because it is the pass's most important process lesson.

**Cause.** `ruff check --select F401 --fix` removed 44 unused imports from 28
files. One of them — in the **generated** file
`app/data/upstox_proto/MarketDataFeed_pb2.py` — looked unused but is *required*
for protobuf descriptor registration. Removing it caused:

```
TypeError: Couldn't build proto file into descriptor pool
```

breaking test collection in three files. This is the worst possible class of
failure for a trading system: it silently disables market-data decoding.

**Fix.** Restored the file from git and added it to `[tool.ruff] exclude` so no
future sweep can touch it again.

**Lesson.** Auto-fix sweeps are only safe behind a full test run, and generated
code must be excluded from linting, not "fixed".

---

### Defect 12 — Dead code and unused imports

Six dead local assignments were inspected **before** removal, not deleted on
sight — two of them were not actually dead:

- `director.py`'s `rationale` line was removed, because the discarded value came
  from `json.loads()` and could raise `JSONDecodeError` on malformed input — so
  it was a latent crash, not just noise.
- `run.py`'s `symbols` variable had a comment stating that live adapters report
  their subscribed symbols. Rather than delete it, it was given a purpose: the
  paper/API entry point now prints a `STREAM_SUBSCRIBED` JSON line (source,
  timeframe, symbols) before the event loop, so operators can confirm what the
  engine actually subscribed to.

Five genuinely dead assignments were removed, and 44 unused imports were cleaned
across 28 files (subject to the caution above).

**A bug I introduced during this cleanup and caught:** the new `STREAM_SUBSCRIBED`
print initially referenced `timeframe`, a variable assigned *later* in `run.py` —
`F821 Undefined name 'timeframe'`. Fixed to `settings.default_timeframe`. Ruff
`F841` and `F821` are now both clean.

---

## 4. Investigated and deliberately *not* changed

| Report | Finding | Why unchanged |
|--------|---------|---------------|
| `--timeframe 1D` default on 1-minute data | Looks like a bug | **Not a bug.** `1D` is the correct default for a daily bar; on minute data you pass `--timeframe 1m`. Changing it would break the daily path. |
| 138 × "blind `except Exception`" | Looks alarming | Deliberate defensive boundaries at subsystem edges. Narrowing them all would be a large behavioural change with real regression risk, outside a debug pass. Documented as residual debt in §6. |
| 75 × import-order, 71 × `UP017` (datetime style), 43 × `UP006` (typing) | Lint noise | Style-only. Not defects, and a mass rewrite would create a large unreviewable diff. |
| `live-preflight` refuses in PAPER mode | Looks like failure | **Correct behaviour**, and part of the safety design. |
| `--timeframe` / `ScannerFunnel(top_k=…)` mismatches during investigation | — | Errors in *my test harness*, not the product. Corrected and retested. |

---

## 5. Final verification results

Every check below was run against the fixed tree.

| Check | Command | Result |
|-------|---------|--------|
| Test suite | `pytest -q` | **1265 passed, 37 skipped, 1 warning** |
| New regression tests | `pytest tests/test_debug_pass_regressions.py -q` | **37 passed** |
| Static analysis (bug rules) | `ruff --select F841,F821,F401` | **All checks passed** |
| Byte-compile | `compileall app tests tools run.py desktop scripts` | **Clean** |
| Import walk | every module imported | **0 failures** |
| CLI harness | 51 command paths | **0 tracebacks** |
| Security scan | `tools/security_scan.py` | **CLEAN** (178 files) |
| Determinism | `tools/determinism_check.py` | **ok: true** |
| Paper self-test | `python -m app paper-test` | **ok: true** |
| Live drill | `python -m app live-drill` | **ok: true** (14/14) |
| Live preflight | `python -m app live-preflight` | clean refusal in PAPER mode |

### The headline result

```
fingerprint: 9f1fd21fe87cbb5b732e81dcbe5ce15ad03941d9370be41ae2a6ea1afb5f4884
```

The determinism fingerprint is **byte-identical to the pre-fix baseline**. This
was captured before any change and re-checked after. It proves the entire debug
pass altered **no trading behaviour** — every fix was to error handling, input
validation, time handling, or code hygiene. Nothing in the strategy, execution
or risk path moved.

The single remaining warning is pre-existing and third-party
(starlette/anyio `_PortalFactoryType`). The clean-baseline warning count is
exactly 1; it is 1 now.

### About the new regression tests

`tests/test_debug_pass_regressions.py` (37 tests) pins every fix to prevent
regression. Notably it includes an **AST guard** asserting that the removed dead
code cannot come back *and* that the code introduced to replace it stays
reachable — text-matching tests would pass on a file that no longer executes.
The suite exercises real CLI entry points with an isolated stack
(`DATABASE_URL`/`DATA_DIR`/`MODE`/`LIVE_TRADING`/`BROKER`), so it tests the
operator's actual experience rather than internal functions.

---

## 6. Residual debt and honest caveats

1. **138 broad exception handlers remain.** Each is a deliberate boundary, but
   collectively they can mask future bugs. Recommend narrowing them
   incrementally as each subsystem is touched, rather than in one sweep.
2. **Import cycle risk.** Adding `app.utils.clock` to `app/__main__.py` and
   `app.history.manager` was verified to import cleanly, but any future
   `app.utils` module should stay dependency-free to avoid cycles.
3. **The desktop GUI is not boot-verified** in this environment (no display
   server). It is presentation-only and cannot place orders.
4. **`pyproject.toml` — nearly lost.** I rewrote this tracked file wholesale
   early in the pass, destroying the packaging configuration and failing
   `test_kronos_packaging.py`. It was recovered from git and the original keys
   restored verbatim; the only addition is the ruff exclusion for generated
   protobuf code. The test suite caught this — which is the argument for a
   full-suite gate on any config change.
5. **No order has ever been placed from this environment.** All execution-path
   testing used the fake-credential rehearsal fixture and the sandbox drill.

---

## 7. What this pass does *not* change

The live-trading answer is unchanged. The code is ready; three blockers are
user-side:

1. A **live Upstox app** and `UPSTOX_LIVE_ACCESS_TOKEN` (expires daily; refresh
   via `python -m app upstox-login`).
2. A **static IP registered with Upstox** — mandatory for order APIs under the
   SEBI/NSE framework since 2025-10-01. **Start this first; it has a queue.**
3. The `.env` live block.

Then: `sandbox-test --place-order` → `live-preflight` (`readiness.ok: true`) →
`live-drill` → `engine` with canary caps. Set `ALERT_WEBHOOK_URL`.

Everything remains off by default and cannot be enabled by a single environment
variable. See `ARENA_GUIDE.md` for the full operator procedure and
`tools/live_config_check.py` for the pre-flight config checker.
