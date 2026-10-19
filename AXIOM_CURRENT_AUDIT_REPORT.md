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
