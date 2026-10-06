# AXIOM Current Audit Report

**Audit date:** 2026-10-06 (Asia/Calcutta)
**Repository:** `Thedivinetwilight/AXIOM`
**Session branch:** `arena/01a102dd-axiom`
**Scope:** source, tests, operator/product documentation, updater/backup boundaries, and executable-build workflow. This report is the current evidence index for the commit that contains it; the full commit SHA and commit-matched CI/build records are authoritative in Git/GitHub.

> **Verdict:** The source-level audit and implementation pass is complete, and the local full test suite passes. The live execution boundary is fail-closed within AXIOM's first-party broker path. This is **not** a claim of production readiness, legal approval, profitability, a live Upstox session, genuine Kronos weights, or a signed/published release. Cross-platform frozen-build evidence is valid only when the GitHub `Build executable` run and each build record name the exact current commit.

## 1. Executive findings

| Area | Current finding | Evidence / limits |
|---|---|---|
| Live order authorization | First-party live mutations are routed through `LiveExecutionGateway`; final live HTTP mutation authorization requires an active submit dispatch bound to the exact gateway, broker, immutable intent, and capability. | Gateway, broker, lease/supervisor, adversarial and mock-exchange tests pass. This is an in-process application boundary, not control over arbitrary HTTP calls made outside AXIOM by another process or code that already has broker credentials. |
| Failure behavior | Unknown, malformed, stale, or unreadable state blocks new risk. Existing Phase 4 lease, supervisor, readiness, emergency-stop, reconciliation, freshness, and recovery protections remain in the code and tests. Risk-reduction/cancel behavior remains separately authorized rather than being implicitly disabled with entries. | `tests/test_axiom_execution_gateway.py`, `tests/test_live_supervisor_lifecycle.py`, `tests/test_live_ops_v2_1.py`, `tests/test_submission_journal_durability.py`, and live hardening tests. No production account was used. |
| Fill accounting / restart | Realized P&L is credited per unique fill delta, atomically and idempotently across recovery; historical cumulative ledger P&L is not re-added as a new fill. | `test_realized_fill_accounting_is_atomic_idempotent_and_cutover_aware` and `test_recovered_sell_fills_credit_each_delta_once_across_restart` in `tests/test_v3_0_1_execution_safety.py`, plus submission-journal/restart tests. |
| Kronos / forecast inputs | Market bars and forecast requests receive strict validation across market-data, pipeline, runtime/backend, and worker/IPC boundaries. Configured cache paths and service-composition status are propagated and reported distinctly. | Kronos contract/runtime/worker tests pass. The HTTP service-composition test uses a **fake** forecast service. No real weights, publisher signature, forecast-quality result, GPU performance, or production inference is claimed. |
| SQLite backup / restore | Snapshot consistency, archive structure/member hashes, path confinement, staged active-database restore, atomic-replace failure behavior, and a writable descriptor for cross-platform archive `fsync` have regression tests. | `TestBackupRestore` in `tests/test_axiom_intel_system.py`. Archive hashes are not signatures; an unsigned manifest does not establish who created the archive. |
| Updater | Confined staging and verification are tested. A successful checksum status means the bytes match the caller-supplied SHA-256 only. | `test_checksum_match_is_not_reported_as_publisher_authenticity` and path-confinement tests in `tests/test_axiom_updater.py`. No publisher-signature or release-authenticity check is claimed. |
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
- Updater path confinement, staged state, and checksum behavior are tested. The verifier's digest is caller-supplied; no detached signature, trusted publisher key, or authenticity chain is implemented or asserted here. The updater does not execute the downloaded file.

## 5. Research, GUI, API, and provenance

- Deterministic research-loop admission/quotas, evidence provenance/expiry, market identity, model-state truthfulness, counterfactual arithmetic on recorded data, and selected UI-to-API action contracts are covered by tests.
- These are software-contract results on controlled/local inputs. They are not evidence of statistical significance, independent out-of-sample performance, real external data correctness, regulatory suitability, or profitability. The GUI matrix covers selected contracts; no blanket claim of pixel-level, every-platform visual verification is made.
- Historical reports and old operator guides are retained. Their banners identify them as snapshots; their old test totals/version statements are not current evidence. `README.md`, `AXIOM_PRODUCT_MANUAL.md`, and this report point to the current evidence boundary.

## 6. Verification run

Run on the current source tree with dependencies from `requirements.txt` in Python 3.11:

```text
.venv/bin/python -m pytest -q -rs
1845 passed, 39 skipped, 1 warning in 155.57s
```

The one warning is Starlette's deprecation notice for `anyio.abc.BlockingPortal`. Skip reasons were reported by `-rs` and are limited to optional `pandas`/`numpy`/`torch` dependencies; absent Kronos source or genuine weights; a public instrument-file probe unavailable over TLS; and the two frozen-E2E cases requiring a built executable. CI is configured to build the executable and run the frozen gate rather than skip it. These skips are **not** counted as passes for real weights, external data, or a local frozen binary.

The focused gateway/mock-exchange/Kronos/API/backup set also passed during audit (**494 passed, 14 skipped, 1 warning**). Core Ruff checks (`E4,E7,E9,F,I`) passed on the touched gateway, broker, workstation, market-provider, and regression-test files. A broader Ruff pass still reports 46 `BLE001`, `TRY004`, and `S110` diagnostics in the fail-closed gateway/API exception/type-handling code; they were not silently suppressed or represented as a clean broad-lint run. `git diff --check` passed before final staging.

## 7. Commit-matched CI and artifact verification

The [GitHub Actions **Build executable** workflow](https://github.com/Thedivinetwilight/AXIOM/actions/workflows/build-executable.yml) (`.github/workflows/build-executable.yml`) is configured to run on this branch and gates:

1. the full source suite on Ubuntu;
2. packaging/frontend/wizard/instance/mode-isolation/UI tests on each build OS;
3. the full suite on each build OS;
4. a PyInstaller build and frozen end-to-end run against the actual executable on Windows, Linux, and macOS;
5. a build-identity assertion that `app/BUILD_META.json` contains the workflow's `github.sha`;
6. upload of the binary, SHA-256 sums, and JSON build record including commit, platform, byte size, and artifact digest.

For final verification, match the workflow run's `head_sha` and every artifact JSON `commit` to the exact branch `HEAD`; a successful earlier run on another SHA does not count. Branch builds produce Actions artifacts, not an attached GitHub release; the workflow attaches to a release only for a tag. Artifact SHA-256 values establish byte equality with the recorded digest, not who published the artifact or whether it is signed. The final commit-specific CI result and artifact names are reported in the completion message after GitHub finishes the matching run.

## 8. Explicitly unverified / not claimed

- Production Upstox credentials, OAuth, real account data, a real-money order, broker approval, production IP registration, or live-account readiness.
- Real Kronos weights, publisher signature/origin, measured forecast quality, or production GPU/model runtime.
- Statistical edge, profitable strategy, independent OOS validation, or legal/regulatory compliance.
- Control of arbitrary out-of-band HTTP calls or hostile code already holding credentials.
- A local Windows/macOS/Linux frozen artifact before the commit-matched GitHub matrix completes; PyInstaller was absent locally.
- Signed updater/release authenticity. Caller-provided checksum matching is the only updater digest claim.

**Bottom line:** source-level safety and regression gates pass; default testing is paper/mock/sandbox only. Live mode remains fail-closed and unverified for production. Treat this report as a software audit, not authorization to trade.
