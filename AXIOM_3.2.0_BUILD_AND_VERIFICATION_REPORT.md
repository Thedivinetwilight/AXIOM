# AXIOM-MONEY-PRINTER 3.2.0 — Build & Verification Report (HISTORICAL)

> **Historical snapshot only:** this report describes commit `e2001b7` and its 2026-10-04 verification. It is not evidence for current HEAD or the current release artifact. See [`AXIOM_CURRENT_AUDIT_REPORT.md`](AXIOM_CURRENT_AUDIT_REPORT.md).

**Date:** 2026-10-04 · **Branch:** `arena/01a102dd-axiom` · **HEAD:** `e2001b7`
**Spec executed:** `AXIOM-MONEY-PRINTER_MASTER_PROMPT_v1.0.md` (v1.0, 150 sections)
**Display name:** AXIOM · codename MONEY-PRINTER is brand only; no profit
promise is made anywhere in the product.

---

## 1. What was implemented (this release)

### Markets & universes (§4–5, §60, §143)
- `app/markets/registry.py`: seven market scopes (india, us, japan, uk,
  hongkong, singapore, global-context) with exchanges, currencies,
  timezones, broker mappings; deterministic session engine with holiday
  calendars, weekends, pre-open windows, market-local clocks;
  instrument-identity parser accepting Upstox segment suffixes
  (`NSE_EQ|INE…` → exchange NSE, segment EQ) and refusing cross-market
  symbols.
- Universes: persisted, market-scoped, CRUD + export/import
  (`axiom-universe/v1`), instrument pins (`app/database` schema v9).

### State & "what changed" (§8, §71)
- `app/state/bus.py`: event bus with monotonic sequence, replay-after-gap
  (`replay_since`), SSE transport `/api/stream` + health.
- `app/state/changes.py`: change engine persisting every material state
  delta with provenance; first observation is a snapshot, never a change.

### Intelligence (§9–16, §20–22, §25, §139–141)
- `app/intel/opportunity.py`: Opportunity Center — attention profiles
  (conservative/balanced/aggressive) that tune detection, never safety;
  lifecycle DETECTED→…→EXECUTED/PAPERED/SHADOWED with terminal
  EXPIRED/INVALIDATED states that never revive; TTL-based evidence expiry.
- `app/intel/evidence.py`: per-instrument evidence stack; missing layers
  report UNAVAILABLE with a reason (never fabricated).
- `app/intel/analogues.py`: historical analogues without look-ahead,
  counterexamples included, bad data (NaN/inf/≤0) refused not repaired.
- `app/intel/missed.py`: missed-opportunity/false-negative lab with
  counterfactual and research-question fields.
- `app/intel/drift.py`: deterministic distribution-drift detector
  (location 3-sigma band, dispersion, tail); INSUFFICIENT_DATA makes no
  claim.
- `app/intel/regime_bridge.py`: bridges the existing RegimeEngine into
  what-changed + opportunities without duplicating the classifier.
- Research memory (topic/kind/payload/provenance/confidence) and
  deterministic next-best-experiment proposals from recorded debt only.

### Live trading — first-class, fail-closed (§31–40, §135–137, §145)
- `app/live/session.py`: OFF→CONFIGURED→READY→ARMED→ACTIVE⇄PAUSED/
  RECONCILING→STOPPED (+BLOCKED from any state). Arming requires the exact
  confirmation phrase AND a passing readiness contract AND a readable-clear
  emergency-stop flag (unreadable ⇒ fail closed). No restart, recovery,
  background job or config change can arm or activate — verified by tests.
- Readiness center: READY/WARNING/BLOCKED/UNKNOWN with explicit verdicts;
  not-verifiable checks are shown, never treated as passing.
- `app/live/guard_chain.py`: SIGNAL→PORTFOLIO→RISK→POSITION→ORDER
  VALIDATION→BROKER RULE→FRESHNESS→DUPLICATE→FINAL AUTH→BROKER; exits skip
  entry-only guards so held positions are never trapped.
- `app/live/rate_limit.py`: configurable per-second/minute/30-minute
  profiles (REGULAR_ALGO etc.).

### Kronos studio (§17–19)
- `app/models/studio.py`: DOWNLOAD→VERIFY→INSTALL→BENCHMARK→ACTIVATE with
  truthful statuses only (`DOWNLOAD_NOT_CONFIGURED`, `NOT_VERIFIED`,
  `INSTALL_REFUSED`, `NOT_ATTEMPTED`, `ACTIVATE_REFUSED`); activation
  records the verified model for the isolated forecast worker.

### System (§23–28, §57–58, §68–69, §102)
- `app/system/latency.py`: P50/P95/P99/P99.9 hot-path tracking with
  deadlines and miss counters, daily persistence.
- `app/system/tasks.py`: task center with restart recovery and governor
  admission.
- `app/system/resources.py`: resource governor (disk pressure etc.).
- `app/system/backup.py`: backup/restore with manifest, SHA-256 integrity
  refs, corruption refusal, path confinement.
- `app/system/updater.py`: GUI update pipeline
  CHECK→DOWNLOAD→VERIFY→PREPARE→INSTALL with honest statuses, SHA-256
  verification, tamper detection between prepare and install; never
  executes downloaded code.

### Engine integration
- Paper simulator and sandbox engine feed OBSERVED bars into the
  intelligence layer via fail-safe telemetry hooks (`workstation=None`
  keeps historic behaviour byte-for-byte).

### GUI (§45–58, §61–63, §81, §124–132)
- AXIOM cockpit in `app/static/arena-app.js`: COMMAND / MARKETS / RESEARCH
  / MODELS / TRADING / RISK / INTELLIGENCE / REPORTS / SYSTEM, Ctrl+K
  palette, status strip (Feed/Engine/Research/Kronos/Risk/Tasks +
  freshness), SSE with reconnect and replay, evidence drawer, universe
  studio, live state-machine panel with readiness center + start summary +
  emergency controls, task center, backup center, update center.
- Every labeled control maps 1:1 to a backend endpoint, pinned by
  `tests/test_axiom_gui_action_matrix.py`.

## 2. What was tested

| Suite | Result |
|---|---|
| Full local suite (Linux) | **1554 passed / 37 skipped / 0 failed** |
| `tests/test_axiom_markets_live.py` (markets/sessions/live machine) | 40/40 |
| `tests/test_axiom_intel_system.py` (intel + HTTP integration) | 52/52 |
| `tests/test_axiom_gui_action_matrix.py` (every GUI control) | 25/25 |
| `tests/test_axiom_updater.py` (update pipeline) | 11/11 |
| Frozen executable E2E (`packaging/frozen_e2e.py`) | **ALL PASS, ok=true** |

Adversarial coverage included: cross-market symbol smuggling, NaN/inf data
refusal, terminal-state revival attempts, path traversal (backup + update
staging), tamper-between-prepare-and-install, wrong confirmation phrase,
activation without arming, emergency-stop races, corrupt backup refusal,
silent-swallow and Windows-encoding static gates.

## 3. Benchmarks (observed, not targets)

Hot path `PaperSimulator.process_bar` over the shipped 1600-bar sample
dataset (full validation→risk→broker→persist path):

```
p50 = 0.581 ms · p95 = 0.940 ms · p99 = 1.050 ms · p99.9 = 1.102 ms
```

## 4. Build verification

- Frozen toolchain rebuilt from source this session: zlib 1.3.1 (PIC),
  SQLite amalgamation, OpenSSL 3.0.14, CPython 3.11.9 (`--enable-shared`,
  `--without-ensurepip`), pip bootstrapped from bundled wheels.
- PyInstaller 6.22.3 inside `.venv-frozen`; `_ctypes` absent by design
  (no libffi): guarded in `PyInstaller.depend.{bindepend,utils}` and in
  `packaging/arena.spec` (ctypes excluded only when `_ctypes` is missing —
  Windows CI keeps it).
- **Built artifact:** `dist/Arena` (Linux, 32.1 MB),
  sha256 `59eb2448c6e9a75ba912f847d0c2360eceaf8808709eaf18074170b59879eabc`,
  build id `3.2.0-e2001b75e4`.
- The built binary itself was verified: 38/38 frozen E2E checks
  (fresh-home start, wizard end-to-end, replay run with orders/fills,
  stop, restart recovery, duplicate-launch guard, data-directory
  placement, secret scrubbing) and live spot-checks of the new routes
  (`/api/research/drift`, `/api/system/update/status`) served by the
  binary — not by the source tree.
- **CI run `37192747375` (commit `7ae5904`): SUCCESS on every platform.**
  - Full source suite on ubuntu: passed.
  - windows-latest: packaging gates + full suite + `build_exe.py --test`
    (frozen end-to-end against the ACTUAL `Arena.exe`) passed.
  - ubuntu-latest and macos-latest: same gates passed.
  - Per-platform `SHA256SUMS-*.txt` recorded in the run artifacts.

Two real defects were caught and fixed en route to green:
1. The repo's unanchored `models/` gitignore pattern silently swallowed
   `app/models/studio.py`, so `import app.models` failed on every clean
   checkout (CI) while passing locally. Fixed by anchoring the
   weight-directory ignore to the repo root; verified with a clean
   `git archive` checkout full-suite run.
2. The resource governor refused research tasks whenever the host's
   1-minute load exceeded its core count — normal on busy CI runners.
   Admission now stops at sustained 2× overload, and task-lifecycle tests
   branch deterministically on the system's own reported pressure.

## 5. External blockers (recorded, not faked)

- **Kronos weights download** (HuggingFace) is unreachable from this
  sandbox: the studio reports `DOWNLOAD_NOT_CONFIGURED`/`NOT_VERIFIED`
  honestly; verify/install/activate are tested with locally recorded
  weights and clearly-labelled synthetic candidates.
- **A truly production Upstox live session**: no production credentials
  exist in this environment; everything up to the broker boundary is
  exercised with explicitly-labelled mocks.
- **apt/debian mirrors, python.org, libffi sources** blocked here: the
  frozen toolchain was built from codeload tarballs; `_ctypes` is
  legitimately absent in the frozen venv and guarded in the spec
  (Windows CI, where `_ctypes` exists, keeps the normal DLL scan).

## 6. Remaining defects (honest list)

1. Counterfactual simulation (§99) is covered by analogue forward-return
   accounting plus the backtest infrastructure; a dedicated
   "what-if-we-had-entered" replay UI is not built.
2. Opportunity candidates currently arise from engine-observed bars and
   manual observation; no autonomous broker-feed subscription exists in
   this environment (paper replay only — stated as such).
3. OS-locked files defer instead of completing in place: on Windows the
   update INSTALL and a backup restore of a database held open by a
   running server surface `INSTALL_REFUSED` /
   `RESTORE_PREPARED_RESTART_REQUIRED` with the verified artifact staged
   — refusal paths are tested via a simulated lock.

## 7. Repository state

- Branch `arena/01a102dd-axiom`; final full suite **1555 passed / 37
  skipped / 0 failed**; CI run `37192747375` SUCCESS (ubuntu, Windows,
  macOS hard gates).
- Working tree clean; no secrets in the tree, logs, or artifacts
  (verified by tests and frozen E2E).
- Authoritative docs: `AXIOM_PRODUCT_MANUAL.md` (supersedes the listed
  pre-3.2 documents); README banner updated; version 3.2.0.
