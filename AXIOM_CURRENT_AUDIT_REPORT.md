# AXIOM Current Audit Report

**Audit snapshot date:** 2026-10-06 (Asia/Calcutta)
**Repository:** `Thedivinetwilight/AXIOM`
**Original audit branch:** `arena/01a102dd-axiom`
**Scope:** source, tests, operator/product documentation, updater/backup boundaries, and executable-build workflow at that snapshot. Sections 1–8 are not a re-audit of later HEADs. Section 9 records subsequent, commit-matched work from the AXIOM mission branch without extending the earlier audit's claims; full commit SHAs and CI/build records remain authoritative in Git/GitHub.

> **Verdict at the 2026-10-06 audit snapshot:** The source-level audit and implementation pass was complete, and the local full test suite passed. The live execution boundary was fail-closed within AXIOM's first-party broker path. This was **not** a claim of production readiness, legal approval, profitability, a live Upstox session, genuine Kronos weights, or a signed/published release. Frozen-build evidence is commit-specific; subsequent Windows results are recorded in §9 and must not be generalized to other platforms or real model inference.

## 1. Executive findings

| Area | Current finding | Evidence / limits |
|---|---|---|
| Live order authorization | First-party live mutations are routed through `LiveExecutionGateway`; final live HTTP mutation authorization requires an active submit dispatch bound to the exact gateway, broker, immutable intent, and capability. | Gateway, broker, lease/supervisor, adversarial and mock-exchange tests pass. This is an in-process application boundary, not control over arbitrary HTTP calls made outside AXIOM by another process or code that already has broker credentials. |
| Failure behavior | Unknown, malformed, stale, or unreadable state blocks new risk. Existing Phase 4 lease, supervisor, readiness, emergency-stop, reconciliation, freshness, and recovery protections remain in the code and tests. Risk-reduction/cancel behavior remains separately authorized rather than being implicitly disabled with entries. | `tests/test_axiom_execution_gateway.py`, `tests/test_live_supervisor_lifecycle.py`, `tests/test_live_ops_v2_1.py`, `tests/test_submission_journal_durability.py`, and live hardening tests. No production account was used. |
| Fill accounting / restart | Realized P&L is credited per unique fill delta, atomically and idempotently across recovery; historical cumulative ledger P&L is not re-added as a new fill. | `test_realized_fill_accounting_is_atomic_idempotent_and_cutover_aware` and `test_recovered_sell_fills_credit_each_delta_once_across_restart` in `tests/test_v3_0_1_execution_safety.py`, plus submission-journal/restart tests. |
| Kronos / forecast inputs | Market bars and forecast requests receive strict validation across market-data, pipeline, runtime/backend, and worker/IPC boundaries. Configured cache paths and service-composition status are propagated and reported distinctly. | Kronos contract/runtime/worker tests pass. The HTTP service-composition test uses a **fake** forecast service. No real weights, publisher signature, forecast-quality result, GPU performance, or production inference is claimed. |
| SQLite backup / restore | Snapshot consistency, archive structure/member hashes, path confinement, staged active-database restore, atomic-replace failure behavior, and a writable descriptor for cross-platform archive `fsync` have regression tests. | `TestBackupRestore` in `tests/test_axiom_intel_system.py`. Archive hashes are not signatures; an unsigned manifest does not establish who created the archive. |
| Updater | Confined staging, verification, install staging, and a writable descriptor for the rollback-copy `fsync` are tested. A successful checksum status means the bytes match the caller-supplied SHA-256 only. | `test_checksum_match_is_not_reported_as_publisher_authenticity`, path-confinement tests, and the writable-descriptor regression in `tests/test_axiom_updater.py`. No publisher-signature or release-authenticity check is claimed. |
| Research / GUI / API | Deterministic fixture contracts, selected GUI action-to-route contracts, and selected API workflows are tested. Recorded-data counterfactuals do not fabricate model scenarios. | These tests do not establish out-of-sample edge, profitability, exhaustive visual correctness, or every external service integration. |
| Release build | The workflow defines Ubuntu source tests, per-platform packaging/full-suite gates, frozen executable E2E, exact build identity, and per-artifact checksum/build-info records. | A local frozen binary was not built: PyInstaller is not installed in the sandbox. Do not attribute an artifact from an older run to this commit. Verify the GitHub run's `head_sha` and all uploaded build-info `commit` values against the final branch commit. A branch push uploads Actions artifacts; tag-gated release attachment is separate. |

## 2. Live execution boundary

### Implemented controls

- The registered gateway capability is minted once for a gateway and cannot be overwritten by a second registration. Possession of a structurally valid intent and the registered capability is not enough to mutate the broker directly.
- `UpstoxLiveBroker` refuses caller-owned HTTP transports. Protocol tests patch `httpx.request` at the **test boundary** and direct requests into a deterministic in-process exchange; that transport reference is never installed on the production live broker.
- At the mutating HTTP boundary, authorization requires the active `LiveExecutionGateway.submit` context to match the concrete gateway, broker, intent object, and capability. The context is set only around the final broker operation and is reset in `finally`.
- The final authorizer constrains supported method/endpoint/purpose combinations and re-runs current controls after local journal work and durable authorization/audit work, immediately before the broker request. Entry checks revalidate the configuration hash/epoch, ACTIVE session and live-engine control generation, owned live lease, readiness/supervisor and reconciliation evidence, emergency-stop state, exact confirmation phrase, broker/account identity, entry guard chain, freshness/token conditions, account exposure/working-order limits, and canary reservation. Reads/checks that fail, are stale, or have malformed values refuse entries.
- Exit, risk-reduction, and cancellation paths do not inherit permission from an entry epoch; they remain subject to explicit session/lease, intent, current-position, stop-semantics, broker-identity, endpoint, and final-boundary checks. Live modification remains refused.
- Regression coverage includes capability re-registration, a valid direct broker submit with capability but no active gateway dispatch (refused before HTTP), direct mutating method/base-class requests (refused before HTTP), caller-owned transport rejection, revalidation after a stop flips at the HTTP boundary, stale lease/reconciliation/token/control state, and entries blocked while allowed risk-reduction behavior remains available where the checks permit it.

### Boundary limit

These controls govern AXIOM's first-party broker path. They cannot prevent arbitrary out-of-band HTTP requests, a separate process using credentials, or hostile in-process code that already holds a bearer token. No claim that they do is made here. No real-money order, production OAuth flow, or production-account access occurred in this audit or test run.

## 3. Kronos, market data, and cache evidence

- Observed bars are validated for numeric/non-boolean OHLCV values, finite values, valid high/low/open/close relationships, positive/valid volume policy, instrument identity, timezone-aware timestamps, ordering/duplicates, timeframe consistency, and closed-candle/session rules. Input errors are rejected rather than repaired or replaced with fabricated candles.
- Forecast input checks cover requested instrument/timeframe/horizon/configuration and the handoff into backend/runtime and worker IPC. IPC status/config parsing is strict about malformed or unknown configuration fields.
- Explicit configured cache directories are propagated into the runtime/worker and exposed in status; the tests use temporary local directories.
- Service status distinguishes disabled, configured-but-uncomposed, running, ready, and error/unavailable states. The API integration test proving HTTP composition uses a fake service with a stub runtime. It does not prove that the app produced a real model forecast or that current user-facing production data wiring is complete.
- Cache SHA-256/file-manifest checks detect changes relative to local metadata. They do **not** prove publisher origin or cryptographic signature. Genuine pretrained weights, remote artifact authenticity, forecast quality, real credentials, and GPU behavior remain unverified. The 39 skips in the full suite include tests that deliberately require a real Kronos checkout/weights or optional numerical dependencies; no random-weight substitute was used.

## 4. Recovery, accounting, and persistence

- Per-fill realized-P&L tests exercise cutover handling, unique fill IDs, atomic persistence, duplicate replay refusal, and recovery after a simulated crash between broker reconciliation and engine accounting.
- Submission-journal transitions reject invalid state changes; uncertain submissions remain conservative and cannot be blindly retried as fresh orders. Duplicate intent state is tested across gateway restart.
- Lease acquisition/release, malformed or unreadable owner/heartbeat state, supervisor lifecycle, and stop/drain behavior have explicit failure-path coverage. Unknown ownership is not interpreted as permission to trade.
- SQLite tests cover online snapshot consistency, archive/member integrity checks, manifest/database binding, active restore staging until the next open, a failed atomic replacement that leaves the active database intact, and use of an update-capable descriptor for archive `fsync`. The initial commit-matched Windows run exposed `OSError [Errno 9]` because the archive was fsynced through a read-only descriptor; it was changed to `r+b` and the regression `test_archive_fsync_uses_a_writable_descriptor` was added. API backup/restore path traversal is refused.
- Updater path confinement, staged state, and checksum behavior are tested. The second Windows run also exposed `OSError [Errno 9]` from fsyncing the install rollback copy through a read-only descriptor; the code now opens that copy as `r+b`, with regression `test_rollback_backup_fsync_uses_a_writable_descriptor`. The verifier's digest is caller-supplied; no detached signature, trusted publisher key, or authenticity chain is implemented or asserted here. The updater does not execute the downloaded file.

## 5. Research, GUI, API, and provenance

- Deterministic research-loop admission/quotas, evidence provenance/expiry, market identity, model-state truthfulness, counterfactual arithmetic on recorded data, and selected UI-to-API action contracts are covered by tests.
- These are software-contract results on controlled/local inputs. They are not evidence of statistical significance, independent out-of-sample performance, real external data correctness, regulatory suitability, or profitability. The GUI matrix covers selected contracts; no blanket claim of pixel-level, every-platform visual verification is made.
- Historical reports and old operator guides are retained. Their banners identify them as snapshots; their old test totals/version statements are not current evidence. `README.md`, `AXIOM_PRODUCT_MANUAL.md`, and this report point to the current evidence boundary.

## 6. Verification run

Run on the current source tree with dependencies from `requirements.txt` in Python 3.11:

```text
.venv/bin/python -m pytest -q -rs
1846 passed, 39 skipped, 1 warning in 151.27s
```

The one warning is Starlette's deprecation notice for `anyio.abc.BlockingPortal`. Skip reasons were reported by `-rs` and are limited to optional `pandas`/`numpy`/`torch` dependencies; absent Kronos source or genuine weights; a public instrument-file probe unavailable over TLS; and the two frozen-E2E cases requiring a built executable. CI is configured to build the executable and run the frozen gate rather than skip it. These skips are **not** counted as passes for real weights, external data, or a local frozen binary.

The focused gateway/mock-exchange/Kronos/API/backup/updater set passed after the cross-platform fixes (**513 passed, 14 skipped, 1 warning** in 49.04s). Core Ruff checks (`E4,E7,E9,F,I`) passed on the touched gateway, broker, workstation, market-provider, and regression-test files. A broader Ruff pass still reports 46 `BLE001`, `TRY004`, and `S110` diagnostics in the fail-closed gateway/API exception/type-handling code; they were not silently suppressed or represented as a clean broad-lint run. `git diff --check` passed before final staging.

## 7. Commit-matched CI and artifact verification

The [GitHub Actions **Build executable** workflow](https://github.com/Thedivinetwilight/AXIOM/actions/workflows/build-executable.yml) (`.github/workflows/build-executable.yml`) is configured to run on this branch and gates:

1. the full source suite on Ubuntu;
2. packaging/frontend/wizard/instance/mode-isolation/UI tests on each build OS;
3. the full suite on each build OS;
4. a PyInstaller build and frozen end-to-end run against the actual executable on Windows, Linux, and macOS;
5. a build-identity assertion that `app/BUILD_META.json` contains the workflow's `github.sha`;
6. upload of the binary, SHA-256 sums, and JSON build record including commit, platform, byte size, and artifact digest.

For final verification, match the workflow run's `head_sha` and every artifact JSON `commit` to the exact branch `HEAD`; a successful earlier run on another SHA does not count. Branch builds produce Actions artifacts, not an attached GitHub release; the workflow attaches to a release only for a tag. Artifact SHA-256 values establish byte equality with the recorded digest, not who published the artifact or whether it is signed. At the original audit snapshot, commit-matched results were pending; later Windows results for the mission branch are recorded in §9.

## 8. Explicitly unverified / not claimed

- Production Upstox credentials, OAuth, real account data, a real-money order, broker approval, production IP registration, or live-account readiness.
- Real Kronos weights, publisher signature/origin, measured forecast quality, or production GPU/model runtime.
- Statistical edge, profitable strategy, independent OOS validation, or legal/regulatory compliance.
- Control of arbitrary out-of-band HTTP calls or hostile code already holding credentials.
- A local sandbox-built Windows/macOS/Linux frozen artifact; PyInstaller was absent locally. The mission branch's commit-matched Windows binaries and installer are recorded separately in §9; no new commit-matched macOS/Linux frozen artifact is established there.
- Signed updater/release authenticity. Caller-provided checksum matching is the only updater digest claim.

**Bottom line at the 2026-10-06 snapshot:** source-level safety and regression gates passed; default testing was paper/mock/sandbox only. Live mode remained fail-closed and unverified for production. Treat this report as a software audit, not authorization to trade.

## 9. Subsequent mission-branch verification (2026-10-19 Asia/Calcutta)

This addendum records selected follow-up work after the 2026-10-06 snapshot. It is not a blanket re-audit of every claim in Sections 1–8. The branch is `arena/f97e3dd9-axiom`; the Windows builds and latest targeted UI/packaging tests were verified at code SHA `8288ff615f521c4ba2fe8c33b71b4688c6e44c9c`, the latest code commit before this documentation-only update.

### Code fixes and local regressions

- Fee-aware net-edge gating now applies the hurdle to proposed notional; invalid numeric/cost inputs or missing expectancy fail closed. Router confidence alone is not treated as expected gross P&L.
- Risk/config/live-config numeric boundaries reject non-finite and malformed values. Regression tests use an explicitly labelled test-only passing gate when edge estimation is outside that test's scope.
- Walk-forward research rejects malformed/non-finite market bars before scoring and retains gap warnings; it does not silently repair or invent data.
- The full local non-slow suite passed on commit `22ba167`: **1,992 passed, 39 skipped, 1 warning**. After the GUI smoke isolation fix on `8288ff6`, the targeted UI/packaging/API set passed **69 tests**. The full local suite was not rerun after that last UI-only change.

### Commit-matched Windows verification

- [Windows pair workflow run 39158169376](https://github.com/Thedivinetwilight/AXIOM/actions/runs/39158169376), head SHA `8288ff615f521c4ba2fe8c33b71b4688c6e44c9c`, passed on Windows x64. It built the GUI and worker in separate environments, verified the GUI environment had no PyTorch, ran the selected risk/numeric/desktop/Kronos regressions, launched the actual `AXIOM.exe --smoke` offscreen, and exercised the actual `AXIOM-Kronos.exe` JSON-lines process using the explicitly labelled stub backend. The worker smoke used a synthetic fixture and a temporary cache, and verified ping/load/forecast/status/clean shutdown; it is not genuine model inference.
- The `AXIOM-Windows-executable-pair` Actions artifact is **223,174,721 bytes**, archive SHA-256 `a316983336edb4ba56c3d3ec23953f7674a097e6e997fa3beaaf7f6aa367f811`, expires 2026-11-01 UTC. Its build-info file records individual executable sizes and digests. Artifact byte hashes are not a publisher signature.
- [Windows build/installer run 39158169240](https://github.com/Thedivinetwilight/AXIOM/actions/runs/39158169240), also at the final mission-branch SHA, passed the full Windows non-slow test suite, frozen Arena executable test, installer build, and install/recovery/restart/uninstall E2E. Artifacts: `AXIOM-Arena-Windows-x64` (19,740,003 bytes; SHA-256 `dc712f9eefddf8c6bcb8c1d97732f5d73a5501f89780ddb054d28ad7c7aefad1`) and `AXIOM-Setup-Windows-x64` (21,192,851 bytes; SHA-256 `4b51c3d122c4537c76bd7a0034bf619f1eea69688701c10d3a80f03af21b9505`), both expire 2027-01-16 UTC.

### Remaining limits and safety

- Genuine pretrained weights remain **0/3 verified**; real weight-backed inference and forecast quality remain unverified. The Windows stub smoke does not change this.
- No GPU inference, genuine external market-data execution, or persistent trading process was tested.
- No live order was placed or modified, and no paper-trading process was launched. No existing user/account database was touched; the installer recovery E2E uses an isolated CI work directory and generated test databases only. The packaged GUI smoke was explicitly configured as `MODE=PAPER`, `LIVE_TRADING=false`, `BROKER=LOCAL_PAPER`. No credentials were added to source, CI, or artifacts.

## 10. Verified-fixes audit update (2026-10-19, commit-matched)

This addendum checks the live source and regression tests after the 2026-10-06 snapshot. Code fixes and local full-suite evidence are at `0c7da26a9b1872689580054937ee940842a88005`; the explicit job-level safe-CI-environment adjustment is at `99eafb06ec3c8c40bde4853d8faddabedad2f6b9`. No application source changed between those SHAs. The three listed GitHub Actions runs all report that exact workflow SHA. A later report-only commit does not change the code or workflow evidence.

**Safety boundary:** Every local test command and every relevant CI job used `MODE=PAPER`, `LIVE_TRADING=false`, `BROKER=LOCAL_PAPER`. No live or external broker was contacted and no real or broker-sandbox order was sent or modified; tests use local mocks and generated paper-only test state. No credentials or existing user/paper-account database were used. A safe test environment is not itself evidence of a sustained paper run or paper-readiness.

### Component separation

- **Arena GUI and installer:** the standalone `Arena.exe` and `AXIOM-Setup.exe` build/install/recovery evidence is from the installer workflow. It is distinct from the desktop pair below.
- **Desktop executable pair:** `AXIOM.exe` and sibling `AXIOM-Kronos.exe` are separate processes and a separate build/artifact. The GUI-to-sibling and worker IPC checks use an explicitly configured stub backend.
- **Strategy / risk / execution:** engine, sizing, allocation, liquidity, edge and reconciliation fixes are assessed on their source paths and local-paper test doubles. These do not authorize or demonstrate live execution.
- **Optional read-only Kronos forecasting:** Kronos is not the strategy order executor. The Windows smoke establishes packaging/process/JSON-lines behavior with a stub; it does not establish model inference, forecast quality, or real weights.

### Findings — strategy, risk, and execution

- **Status: Confirmed defect. Finding:** exceptions in the quality scorer or volatility sizer no longer silently pass an entry. They reject the entry and emit a gate-failure audit record; protective exits remain on their separate path. **Evidence:** `app/engine/sandbox_engine.py`; `test_quality_scorer_failures_block_entries`, `test_volatility_sizing_failure_blocks_entries`, and `test_quality_gate_failure_does_not_trap_protective_stop` in `tests/test_engine_fail_closed_audit.py`. **Changed paths:** `app/engine/sandbox_engine.py`, `tests/test_engine_fail_closed_audit.py`.
- **Status: Confirmed defect. Finding:** configured position sizing and volatility lookback are honored; ATR uses each bar's immediately preceding close; invalid sizer output rejects, and a valid sizer cannot expand the allocation-derived quantity. **Evidence:** `test_sandbox_engine_uses_configured_position_size`, `test_configured_volatility_lookback_is_used_by_engine`, `test_atr_uses_each_recent_candle_previous_close`, and `test_volatility_sizer_cannot_expand_past_sizing_budget` in `tests/test_engine_fail_closed_audit.py`. **Changed paths:** `app/engine/sandbox_engine.py`, `tests/test_engine_fail_closed_audit.py`.
- **Status: Confirmed defect. Finding:** when an allocator is explicitly injected, a missing, malformed, non-finite, or negative required plan blocks the entry and is audited; the read-failure branch is coded to fail closed as well. Without an injected allocator, the prior sizing-fraction policy remains in force. **Evidence:** `test_required_allocator_missing_or_invalid_plan_blocks_entries` and `test_no_configured_allocator_keeps_ordinary_sizing_policy`; implementation in `SandboxEngine._allocator_allowance`. **Changed paths:** `app/engine/sandbox_engine.py`, `tests/test_engine_fail_closed_audit.py`.
- **Status: Verification gap. Finding:** `app/__main__.py` constructs the default `SandboxEngine` without an allocator. Whether product policy requires allocator injection in that composition is not established, so the injected-allocator fix must not be read as proof that the default app enforces an allocation plan. **Evidence:** `app/__main__.py:619-637` and the no-allocator regression above. **Changed path reviewed:** `app/__main__.py`.
- **Status: Confirmed defect. Finding:** confidence is not treated as expected P&L; the engine accepts only an explicit quote-currency `gross_expected_pnl` field for the proposal and the net-edge gate rejects missing evidence. No proposal-sized producer was found, so the real path remains fail-closed rather than manufacturing a return. **Evidence:** `_gross_expected_for_decision` in `app/engine/sandbox_engine.py`; `test_engine_net_edge_rejects_confidence_without_proposal_sized_pnl`; net-edge tests in `tests/test_performance_hardening_3_0_5.py`. **Changed paths:** `app/engine/sandbox_engine.py`, `app/risk/net_edge.py`, `tests/test_engine_fail_closed_audit.py`. Related net-edge unit coverage also exists in `tests/test_performance_hardening_3_0_5.py`.
- **Status: Confirmed defect. Finding:** missing, invalid, or stale cross-market inputs no longer become invented flat/neutral/normal context; quality features remain `None` unless supported by current explicit measurements. **Evidence:** `test_cross_market_cache_does_not_invent_neutral_or_stale_context` and `test_cross_market_quality_context_requires_explicit_current_evidence` in `tests/test_engine_fail_closed_audit.py`. **Changed paths:** `app/data/cross_market.py`, `app/engine/sandbox_engine.py`, `tests/test_engine_fail_closed_audit.py`.
- **Status: Confirmed defect. Finding:** the engine no longer ignores an unsupported liquidity `should_reduce` recommendation or guesses a reduction factor; it rejects and records the requested reduction before the hard-risk check. The configured minimum liquidity score is applied. **Evidence:** `test_liquidity_reduction_recommendation_is_enforced_before_hard_risk` and `test_min_liquidity_setting_controls_reject_floor`. **Changed paths:** `app/data/liquidity.py`, `app/engine/sandbox_engine.py`, `app/config/settings.py`, `tests/test_engine_fail_closed_audit.py`.
- **Status: Verification gap. Finding:** `app/data/liquidity.py` still assigns `0.3` when depth/volume is unknown or liquidity calculation fails, and uses `0.5` for an unavailable spread component. The meaning and provenance of those fallback scores need policy review; they are not evidence that the market was observed to be moderately liquid. **Evidence:** `app/data/liquidity.py:175-205`. This limitation is not presented as a verified fix.
- **Status: Confirmed defect. Finding:** SELL reconciliation and recovery retain the pre-SELL entry basis, report the actual exit fill separately, and preserve missing journal prices as `None`; realized SELL fill deltas are replayed once in the tested recovery scenario. **Evidence:** `app/engine/sandbox_engine.py`, `app/learning/journal.py`, focused SELL-price regressions (2/2), and `test_recovered_sell_fills_credit_each_delta_once_across_restart` in `tests/test_v3_0_1_execution_safety.py`. The broader tests also cover atomic/idempotent fill accounting. **Changed paths:** `app/engine/sandbox_engine.py`, `app/engine/state.py`, `app/learning/journal.py`, `app/paper_trading/broker.py`, `app/paper_trading/recovery.py`, `tests/test_v3_0_1_execution_safety.py`.
- **Status: Verification gap. Finding:** the crash-replayed `SELL` fill to `TRADE_OUTCOME` journal path has not been proven to append exactly once across a crash at every write boundary. Current engine attribution supplies aggregate ledger fees, `slippage=0.0`, and `duration_bars=self.processed`; per-trade fee/slippage and duration attribution are not established. **Evidence:** `app/engine/sandbox_engine.py:1957-1976` and `app/learning/journal.py:37-60`. The fill-ledger idempotency test does not close this journal-attribution gap.
- **Status: Confirmed defect. Finding:** the hot-path monitor now records the duration of the full `process_candle` pass in a `finally` block, while metric failures remain observational and do not change order handling. **Evidence:** `test_hot_path_setting_records_candle_latency_and_is_dashboard_visible`. **Changed paths:** `app/engine/sandbox_engine.py`, `app/config/settings.py`, `tests/test_engine_fail_closed_audit.py`.
- **Status: Confirmed defect. Finding:** `packaging/installer_e2e.py` no longer recursively deletes a caller-supplied work directory. It rejects repository/ancestor, symlink, missing-parent, and pre-existing paths and atomically creates only a fresh disposable child. **Evidence:** `tests/test_installer_e2e_safety.py`; the script was inspected before the Windows workflow exercised it with a fresh CI work directory. It was not run locally against caller data. **Changed paths:** `packaging/installer_e2e.py`, `tests/test_installer_e2e_safety.py`, `.github/workflows/windows-build-installer.yml`.

### Findings — remaining research and runtime wiring gaps

- **Status: Verification gap. Finding:** entry-timing decisions are evaluated and `WAIT`/`NO_TRADE` events are recorded, but outcome attribution/learning from those decisions was not verified. **Evidence:** `app/engine/sandbox_engine.py:1649-1660`.
- **Status: Verification gap. Finding:** `MAEMFEAnalyzer` is constructed, but a production call site/consumer that records completed-trade excursions was not verified. **Evidence:** construction in `app/engine/sandbox_engine.py` and implementation in `app/research/mae_mfe.py`; tests of the helper do not establish wiring.
- **Status: Verification gap. Finding:** `CorrelationMonitor` is constructed but an engine call site applying it to current portfolio evidence was not verified. Quality features are deliberately `None` absent evidence. **Evidence:** `app/engine/sandbox_engine.py` and `test_quality_features_do_not_claim_unavailable_positive_evidence`.
- **Status: Verification gap. Finding:** `max_volatility_factor` is accepted by settings/sizer construction, but an exercised behavior proving that it affects engine sizing was not established. **Evidence:** `app/config/settings.py`, `app/learning/volatility_sizing.py`, and `tests/test_risk_numeric_guards.py`.
- **Status: Verification gap. Finding:** replay-data provenance still needs review to distinguish synthetic test fixtures from observations labelled `MARKET_OBSERVED`; no replay fixture or result is promoted here as observed market evidence. **Evidence:** market-origin checks in `axiom/market/validation.py` and replay/research call sites; no provenance-complete end-to-end replay was run for this audit.

### Windows GUI, installer, and optional Kronos verification

All three runs below completed successfully at `head_sha=99eafb06ec3c8c40bde4853d8faddabedad2f6b9`:

- [Build executable run 39198575540](https://github.com/Thedivinetwilight/AXIOM/actions/runs/39198575540): the Ubuntu full source-suite hard gate passed. The multi-OS packaging matrix was **skipped**, as configured for ordinary pushes; it runs only on manual dispatch or a version tag.
- [AXIOM Windows executable pair run 39198575460](https://github.com/Thedivinetwilight/AXIOM/actions/runs/39198575460): selected risk/numeric/desktop/Kronos tests passed; separate GUI and worker build environments were created; `AXIOM.exe` smoke, frozen GUI-to-sibling lifecycle smoke, actual `AXIOM-Kronos.exe` JSON-lines IPC smoke, hash/size recording, and pair artifact upload passed. The Kronos backend was a stub with downloads disabled. This is process/packaging evidence, not visual UI QA or model inference.
- [Windows build/installer run 39198575469](https://github.com/Thedivinetwilight/AXIOM/actions/runs/39198575469): the Windows non-slow suite, actual frozen `Arena.exe` smoke, installer build, packaged install/recovery/restart/uninstall E2E, artifact uploads, and artifact summary all passed. This is separate from the desktop-pair workflow and artifacts.

Actions artifacts are recorded here as **archive digests**, not as individual executable checksums or publisher signatures:

| Artifact (ID) | Run | Size | Archive digest | Expires (UTC) |
|---|---:|---:|---|---|
| `AXIOM-Windows-executable-pair` (`12163555517`; `AXIOM.exe` + `AXIOM-Kronos.exe`) | 39198575460 | 223,175,702 bytes | `sha256:eddd5c391fb6f29135918128ade8ff9ff108af6c09b2ecfa1c0df1535ffa8beb` | 2026-11-02 04:22:22 |
| `AXIOM-Arena-Windows-x64` (`12162987171`; `Arena.exe`) | 39198575469 | 19,739,383 bytes | `sha256:17932129f3643b78453d01f95af9e56486f44e2fe001f8e81803d431526f8d5a` | 2027-01-17 04:15:39 |
| `AXIOM-Setup-Windows-x64` (`12163521024`; `AXIOM-Setup.exe`) | 39198575469 | 21,193,807 bytes | `sha256:64e6613eeb35f2f303e9c36cf48ed0645b339c32f19e536db7c4e26db14bafce` | 2027-01-17 04:15:39 |

The artifact names and contents remain separate: the pair is not the Arena installer, and the Arena executable is not `AXIOM.exe`. The digests above are GitHub Actions artifact archive digests; they must not be substituted for the executable checksums inside each build record. No claim of signing or publisher authenticity is made.

### Local verification and explicit safe CI environment

- Focused engine/risk/SELL/journal/installer/Kronos regressions: **415 passed, 1 warning**. The focused SELL-price checks separately passed **2/2**.
- Full local non-slow suite, run as `MODE=PAPER LIVE_TRADING=false BROKER=LOCAL_PAPER .venv/bin/python -m pytest -q -k "not slow"`: **2,027 passed, 39 skipped, 1 warning in 149.63s**. Skips are not passes for optional dependencies, genuine model weights, real external services, or a frozen binary.
- `compileall` passed after the source edits. After the workflow environment edit, `git diff --check` and PyYAML parsing of `.github/workflows/build-executable.yml`, `.github/workflows/kronos-windows-pair.yml`, and `.github/workflows/windows-build-installer.yml` passed.
- Job-level CI environment now explicitly sets `MODE: PAPER`, `LIVE_TRADING: "false"`, `BROKER: LOCAL_PAPER` in the full-suite and build jobs of `build-executable.yml`, the pair job in `kronos-windows-pair.yml`, and the installer job in `windows-build-installer.yml`. The three commit-matched runs above passed under that configuration.
- `mypy`, `pyright`, `ruff`, and `actionlint` were not available/configured for this pass; no clean type-check or broad-lint claim is made.

### Changed-path inventory

Exact branch delta from base `4317e3a9f130db3f7fd03db53e00bdf66ac79e9f` through the code/workflow verification SHA `99eafb06ec3c8c40bde4853d8faddabedad2f6b9` (63 paths):

```text
.github/workflows/build-executable.yml
.github/workflows/kronos-windows-pair.yml
.github/workflows/windows-build-installer.yml
.gitignore
AXIOM_CURRENT_AUDIT_REPORT.md
AXIOM_KRONOS.env.example
KRONOS_INTEGRATION.md
README.md
USER_MANUAL.md
app/__main__.py
app/api/server.py
app/brokers/sandbox.py
app/config/settings.py
app/data/cross_market.py
app/data/liquidity.py
app/data/upstox_auth.py
app/data/upstox_diagnostic.py
app/database/database.py
app/engine/sandbox_engine.py
app/engine/state.py
app/learning/journal.py
app/paper_trading/broker.py
app/paper_trading/compatibility.py
app/paper_trading/recovery.py
app/risk/limits.py
app/risk/net_edge.py
app/runtime.py
arena.py
axiom/kronos/model_cache.py
axiom/research/walkforward.py
axiom/worker/manager.py
build_exe.py
desktop/app.py
desktop/run_kronos_windows.bat
docs/PAPER_ACCOUNT_RECOVERY.md
packaging/arena.spec
packaging/build_installer.py
packaging/frozen_e2e.py
packaging/frozen_recovery_e2e.py
packaging/installer.iss
packaging/installer_e2e.py
tests/support.py
tests/test_engine_fail_closed_audit.py
tests/test_final_engine_events.py
tests/test_installer_e2e_safety.py
tests/test_kronos_packaging.py
tests/test_kronos_research.py
tests/test_kronos_ui.py
tests/test_live_config_check.py
tests/test_paper_database_recovery.py
tests/test_risk_numeric_guards.py
tests/test_upstox_credential_probe.py
tests/test_upstox_v063.py
tests/test_v1_engine.py
tests/test_v1_sandbox.py
tests/test_v2_api_cli.py
tests/test_v2_engine_integration.py
tests/test_v310_frozen_e2e.py
tests/test_v3_0_1_execution_safety.py
tests/test_v3_0_2_hardening.py
tools/build_windows_executables.py
tools/kronos_executable_e2e.py
tools/live_config_check.py
```

### Final status and limitations

- Genuine Kronos inference remains unverified: artifact verification is **0/3 REAL_INFERENCE_VERIFIED**. No model weights were downloaded/committed, no signed publisher origin was verified, and no real forecast or forecast-quality result was produced.
- No sustained paper process or paper-account validation was run. Safe CI/local tests, stub IPC, and installer recovery do not establish paper-readiness, trading readiness, profitability, or regulatory suitability.
- No pixel-level visual QA was run. Frozen smoke checks establish only the exact automated launch/lifecycle behavior described above.
- **No findings in this addendum are labelled `Unconfirmed suspicion`;** the open items are explicitly recorded as `Verification gap` rather than upgraded to defects without evidence.

**Bottom line:** the reviewed defects listed as `Confirmed defect` have source-backed regression coverage, and all current-SHA required Windows and source-suite workflows passed with the explicit safe test environment. The open integration/data-attribution gaps and absence of sustained paper/model evidence remain material. **This report does not claim paper-readiness or authorize live trading.**
