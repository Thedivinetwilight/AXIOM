# AXIOM-MONEY-PRINTER — Product Manual (3.2.4 code line)

**Display name:** AXIOM · **Codename:** MONEY-PRINTER (brand only; no profit promise) · **Configured version:** 3.2.4

> **Evidence boundary.** This manual describes product surfaces and intended behavior; it is not a certification that every feature is production-ready or present in a released binary. The authoritative current-code evidence, exact test results, CI/artifact state, and known limits are in [`AXIOM_CURRENT_AUDIT_REPORT.md`](AXIOM_CURRENT_AUDIT_REPORT.md). The repository's master prompt is a requirements document, not implementation or verification evidence. Older final reports and run logs are historical snapshots only.

---

## 1. Product overview

AXIOM is a market-intelligence, research, forecasting, and trading-workstation codebase. Its source includes market-scoped instrument/universe services, an evidence and opportunity workflow, research tools, Kronos forecasting interfaces, paper trading, and an opt-in Upstox live-broker integration.

These are separate code surfaces, not a blanket claim of operational readiness:

- Market/session services describe supported market identifiers, calendars, and instrument identities. Their behavior is covered by source tests; exchange holidays, external data correctness, and live vendor availability still depend on external sources.
- Forecast snapshots retain input provenance and label model forecasts separately from observed candles. Kronos is disabled by default. Real pretrained weights, forecast quality, and GPU performance are **not verified** unless the current audit report explicitly says otherwise.
- Research and opportunity records are decision-support data. They do not establish strategy profitability or live eligibility.
- Paper/sandbox/mock configurations are for development and testing. They are not live accounts.
- Live routing exists in source. It is not a claim that a production account is connected, that real-money trading is approved, or that any particular binary has been released.

## 2. Workstation surfaces

The API/static frontend and desktop client expose dashboard, markets, research, models/Kronos, trading, risk, intelligence, reports, and system areas. The UI action-matrix tests cover selected control-to-route contracts. They do **not** prove that every visible control is complete, that every platform renders identically, or that an external service is available.

Unavailable or unverified services should be reported as unavailable/unknown rather than as ready. In particular, Kronos `configured` means the orchestration service was composed; `available` means the service is running and reports a ready runtime. Neither status proves real-weight integrity or forecast quality.

## 3. Live execution and safety boundary

The first-party live path is composed as:

`engine → GatewayRoutedBroker → LiveExecutionGateway → UpstoxLiveBroker → Upstox HTTPS API`

The gateway validates the immutable execution intent and rechecks live authorization at the concrete mutating HTTP boundary, after broker-local journal work. The production broker also rejects direct submit/cancel/modify calls without a gateway-issued capability and refuses unsupported live modification. Entry authorization checks include the lease/epoch, supervisor and readiness state, reconciliation and market-data freshness, emergency stop, broker/account identity, token freshness, risk guards, and account exposure/working-order data. Failures in these reads or checks block new risk; the gateway retains risk-reduction/cancel handling subject to its explicit checks.

Tests use a deterministic in-process mock exchange and test-side HTTPX request patching; the production live broker refuses caller-owned transports. **No real-money order, production OAuth flow, or production-account access is evidence in this repository audit.** The Python boundary is an application architecture control, not a sandbox against arbitrary in-process code that already holds a bearer token and can issue its own HTTP request. No statement in this manual should be read as claiming otherwise.

Live mode remains opt-in and fail-closed. The required settings/confirmation phrase are not a substitute for current readiness, account reconciliation, broker-side controls, operator procedures, or external approval. Do not enable live mode for development, tests, packaging, or frozen E2E runs.

## 4. Forecast, model, and cache boundaries

The forecast path validates market-data identity, OHLCV shape, ordering, timestamps/timezones, closed-candle status, requested horizon, and runtime configuration at the pipeline/runtime/worker boundaries. Those software contracts are tested with controlled inputs.

Model-cache SHA-256 checks protect against local file changes relative to a locally recorded manifest. They do **not** establish that weights came from the publisher or that the publisher signed them. No real pretrained weights or live forecast-quality result is claimed by this manual. The current report states exactly which backends and tests ran.

## 5. Storage, backup, restore, and updates

SQLite schema migrations and backup/restore behavior have source-level tests. Backup manifests hash regular archive members and reject malformed/tampered content. These hashes detect changes against the archive's own manifest; the manifest is unsigned and does not prove who created the backup. Active-database restores are staged for startup, and an offline restore preserves a pre-restore snapshot.

The updater uses a caller-supplied SHA-256 expectation. A successful result means **the bytes matched that supplied digest only**; it is not a publisher signature or cryptographic release-authenticity verification. The updater reports this distinction in its responses and state manifest. Downloaded bytes are not executed by the updater. Source-mode installation is refused unless an explicit frozen-app target is configured.

## 6. Testing and releases

See [`AXIOM_CURRENT_AUDIT_REPORT.md`](AXIOM_CURRENT_AUDIT_REPORT.md) for the current HEAD, clean-tree/commit identity, full and focused test results, skipped tests, packaging checks, CI state, and external blockers. Historical reports such as `ARENA_FINAL_STATUS.md`, `ARENA_FINAL_RUN_LOG.md`, and `AXIOM_3.2.0_BUILD_AND_VERIFICATION_REPORT.md` document earlier snapshots and must not be used as current evidence.

A source test pass is not the same as a cross-platform release-artifact pass. Do not infer a successful Windows/macOS frozen build, a published release, real broker connectivity, or real Kronos model verification unless the current audit report contains the matching commit-specific artifact/CI evidence.

## 7. Historical documents

The older Arena 3.1/3.2 reports, the earlier Kronos integration notes, and the pre-3.2 product guides are retained for historical context. Their version-specific test counts, file maps, and readiness statements are not descriptions of current HEAD. Consult the current audit report for current evidence.
