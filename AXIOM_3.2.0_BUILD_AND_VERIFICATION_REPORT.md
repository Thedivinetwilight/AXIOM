# AXIOM-MONEY-PRINTER 3.2.0 — Build & Verification Report

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
- Windows CI build triggered by push (run 37191024299 pending at the time
  of writing; previous 3.1.0 CI run 37145248472 was all-success).

## 5. External blockers (recorded, not faked)

- **Kronos weights download** (HuggingFace) is unreachable from this
  sandbox: studio reports `DOWNLOAD_NOT_CONFIGURED`/`NOT_VERIFIED`
  honestly; verify/install/activate tested with locally recorded weights.
- **GitHub Actions log/artifact downloads** are blocked in this sandbox
  (EOF on results-receiver hosts): CI verification relies on run status +
  local reproduction of the same suite.
- **apt/debian mirrors, python.org, libffi sources** blocked: frozen
  toolchain built from codeload tarballs; `_ctypes` legitimately absent and
  guarded.
- One Windows CI run (37190918136) failed at the source-suite gate while
  the identical tree is green locally; its logs could not be fetched from
  this sandbox (blocker above). The follow-up run on the same tree plus
  the drift commit (37191024299) is the authoritative signal; if it fails,
  the failing test must be read from a machine with log access.

## 6. Remaining defects (honest list)

1. CI log access from this sandbox is blocked; a Windows-only failure
   cannot be diagnosed here beyond reproducing the suite locally.
2. Counterfactual simulation (§99) is covered by analogue forward-return
   accounting plus the backtest infrastructure; a dedicated
   "what-if-we-had-entered" replay UI is not built.
3. Opportunity candidates currently arise from engine-observed bars and
   manual observation; no autonomous broker-feed subscription exists in
   this environment (paper replay only — stated as such).
4. Update INSTALL replaces the binary in place on POSIX; on Windows the
   platform may refuse replacing a running executable — the refusal is
   surfaced honestly and the prepared artifact remains for the launcher.

## 7. Repository state

- Branch `arena/01a102dd-axiom` pushed at `e2001b7` (commits: `869bbdd`
  3.1.0 → `f8381bf` AXIOM-MONEY-PRINTER 3.2.0 → `df1e4d9` update system →
  `e2001b7` drift detection).
- Working tree clean; no secrets in the tree, logs, or artifacts
  (verified by tests and frozen E2E).
- Authoritative docs: `AXIOM_PRODUCT_MANUAL.md` (supersedes the listed
  pre-3.2 documents); README banner updated; version 3.2.0.
