# AXIOM Daily Upstox Autonomy — Operator Manual

**Status of this document:** implementation and local/mock-test guidance, not a live-readiness certificate. The current build has not logged into an owner's Upstox account, placed an external DEMO order, placed a real-money order, downloaded Kronos weights, or completed a full-market-session shakedown. Those checks remain explicit owner-run work.

## 1. Boundary and scope

Autonomy is separate from AXIOM's legacy paper engine and separately guarded Vantage/MT5 code.

* **PAPER** means live-market inputs may be read from Upstox, but entries, GTTs, fills, positions, costs, gaps, partial fills, and exits are simulated locally. The paper executor has no HTTP client, token provider, or broker order/cancel/modify route.
* **UPSTOX_LIVE** means real-money Upstox execution. It is disabled by default and requires the independent `AUTONOMY_LIVE_ENABLED=true` environment switch plus every daily and account preflight below.
* Supported catalog scope is exact NSE/BSE listed equities (`NSE_EQ`/`BSE_EQ`, `EQ`) and exchange-listed currency futures/options (`NCD_FO`/`BCD_FO`, `FUT`/`CE`/`PE`). Currency derivatives are **not spot forex**. No CFD, universal-market, or universal-contract coverage is implied.
* An exact Upstox `instrument_key` is retained through catalog, feed, risk, intent, order, and reconciliation. Display names never select a contract.
* TradingView/Lightweight Charts is a local display renderer only. The bundled Lightweight Charts 5.2.1 runtime contains no market data. Upstox bars are supplied separately to the chart. TradingView platform data, alerts, and webhooks do not drive this automation; separate permission and a security review would be required before considering such a design.

Connecting data, refreshing BOD metadata, or drawing a chart does not change mode, Arm, Activate, or create an order.

## 2. Prerequisites and external account checks

The operator must verify, on the actual account and current official Upstox documentation:

1. Account is active and the required exchanges/products/order types are enabled. A quote connection alone proves nothing about execution permission.
2. A current standard daily OAuth access token is available. Analytics Tokens are not assumed to authorize order writes or the feed. Standard tokens have time-limited daily authorization; expiry is always treated as a halt condition.
3. The exact account/instrument route supports the intended product (`I`, `D`, or `MTF`), integer quantity/lot step, freeze limit, tick, expiry, multiplier, and short route. Missing currency multiplier metadata is non-tradable, not inferred.
4. Current static-IP registration and the configured `X-Algo-Name` requirements are satisfied. The service checks registered IPs and current public IP at LIVE activation. Current Upstox/regulatory algo registration, order-rate, and account-specific obligations remain the operator's responsibility; AXIOM does not claim compliance.
5. Margin is checked with the current Upstox margin endpoint for the exact instrument, side, quantity, product, and reference price. Funds and positions are read independently.
6. Owner-run external DEMO/sandbox checks are kept separate from production. Sandbox tokens cannot trade live and sandbox support is not production parity.

No agent or CI job may log into a real account, Arm/Activate LIVE, or place a real-money order. Do not put tokens, API secrets, account identifiers, screenshots containing them, or order credentials in Git, CI, fixtures, logs, exports, or chat.

## 3. Configuration

Legacy settings remain paper-only (`MODE=PAPER`, `LIVE_TRADING=false`, `BROKER=LOCAL_PAPER`). Do not use them as the autonomy LIVE switch.

The autonomy-specific environment settings include:

| Setting | Default | Meaning |
|---|---:|---|
| `AUTONOMY_LIVE_ENABLED` | `false` | Independent LIVE master switch; does not Arm or Activate |
| `AUTONOMY_TIMEFRAME` | `1m` | Completed intraday bars: 1m/5m/15m/30m |
| `AUTONOMY_STALE_AFTER_SECONDS` | `90` | Maximum completed-bar/quote age |
| `AUTONOMY_UPSTOX_ALGO_NAME` | empty | Required LIVE `X-Algo-Name`; newline values rejected |
| `AUTONOMY_EXPECTED_PUBLIC_IP` | empty | Required registered static public IP for LIVE |
| `AUTONOMY_WRITE_RATE_PER_SECOND` | `5` | Process-wide live write guard; kept below regular 10/s guidance |
| `AUTONOMY_WORKER_ENABLED` | `true` | Background scanning only; never lifecycle approval |
| `AUTONOMY_SCAN_INTERVAL_SECONDS` | `5` | Worker interval |
| `AUTONOMY_MAX_PARTICIPATION` | `0.10` | Maximum fraction of observed bar volume |
| `AUTONOMY_MAX_SPREAD_BPS` | `50` | Fresh executable-spread bound |
| `AUTONOMY_MAX_MARGIN_USAGE_FRACTION` | `0.50` | Aggregate margin bound |
| `AUTONOMY_CORRELATION_THRESHOLD` | `0.70` | Reserved correlation-control setting; unknown correlations remain conservative |
| `AUTONOMY_GAP_RISK_FRACTION` | `0.002` | Stop sizing gap allowance |
| `AUTONOMY_KRONOS_MANIFEST` | `config/kronos-small.json` | Pinned artifact manifest |
| `AUTONOMY_KRONOS_ROOT` | `data/autonomy/kronos` | Optional local artifact directory; weights are not bundled |

Runtime journals, catalog cache, research output, and optional artifacts belong in the per-user writable autonomy data directory. The manifest and vendored source belong to the application resource bundle. Do not edit the journal to make an unresolved order disappear.

## 4. Daily startup sequence

1. Start AXIOM. Startup loads public metadata and recovers interrupted journals to `HALTED`; it does not create an active session.
2. Open **Terminal → Daily Upstox autonomy**, or `/autonomy`. The page contains no broker credential. Protected API calls use the separate local AXIOM bearer token.
3. Refresh the current-day Upstox BOD catalog. Search by exact key, trading symbol, or underlying and select exact keys. Review tick, step, multiplier, expiry, and route warnings. Refresh is refused while a session is ARMED/ACTIVE.
4. Create either a PAPER or UPSTOX_LIVE session. Creating it changes no execution state beyond `UNCONFIGURED`.
5. Generate a fresh daily token locally using the existing OAuth helper, normally `python -m app upstox-login`, with the operator's own registered OAuth settings. Keep the resulting `.env` private. Then click **Authorize read-only today**. The API has no token input field and never returns the token; only a fingerprint and day are journaled.
6. Enter **new** daily maximum loss and per-trade equity risk. PAPER also requires explicit starting equity. LIVE equity comes from the current Upstox account snapshot, not a typed approximation. Values are bounded and are not silently reused on another day.
7. Review readiness blockers. Missing current BOD metadata, daily authorization/risk, active profile, static IP, exact permissions, or unresolved outcomes blocks activation.
8. Click **Arm**. This is only the first approval and issues no entry permit.
9. Click **Activate** separately. Activation repeats account, profile, IP, exchange-session, completed-bar, quote, margin/account, existing-position, and conflict preflights. Only now can the in-memory service issue short-lived permits. LIVE activation asks for confirmation of the session boundary, not confirmation of each subsequent strategy order.
10. The background worker scans only sessions already ACTIVE in this process. “Scan once” is an operator diagnostic action, not an order-approval workaround.

There is no API endpoint that accepts a broker token, and there is no per-order confirmation endpoint. Unattended orders are possible only inside this explicitly authorized daily ACTIVE session.

## 5. Signal and execution behavior

Each selected instrument is scanned independently. Trend/momentum, moving-average breakout, mean-reversion, and Kronos-informed families evaluate BUY and SELL separately. A candidate includes the completed-bar timestamp, family/side, indicators, regime, confirmed swing evidence, stop, target, and evidence ID.

Execution requires all of the following:

* Exact validated chronological research evidence for the instrument/family/side/timeframe/regime. Synthetic evidence can support PAPER tests but cannot enable LIVE.
* At least two walk-forward runs and one locked holdout, with costs, gaps, partial fills, rejections, and survivorship/source requirements satisfied. Missing/gapped live research data fails the evidence gate.
* A completed-bar confirmed swing stop. The final forming bar is never used. Stops only tighten; they never loosen.
* Stop-distance sizing with contract multiplier, cost and gap allowance, liquidity participation, freeze/lot limits, remaining daily loss budget, fresh bid/ask, current broker margin, aggregate margin, and correlation-aware portfolio risk. Unknown correlation is treated as concentrated risk, not free diversification.
* One active intent per instrument/session and cross-session LIVE conflict checks. Intent is durably recorded before submission. Signals and bars are deduplicated.
* A three-leg Upstox GTT containing ENTRY, STOPLOSS, and TARGET. Submission IDs are acknowledgements, not proof of fills or protection. Reconciliation checks exact GTT rules, child orders, positions, and quantities before calling a position protected.

GTT `IMMEDIATE` entry is a limit order at the specified price, not a guaranteed market fill. Price gaps, spread, slippage, rejected limits, and partial liquidity can prevent or reduce fills. A completed GTT may no longer be retrievable from the GTT-details endpoint; ordinary order history/book and positions are required. AXIOM does not infer a flat account when those records cannot prove the outcome.

## 6. Loss cap, trailing, faults, and recovery

* When realized plus current account day P&L reaches the operator's daily loss bound, the entry block is latched for the day. The check runs during reconciliation even when no new order is attempted. Scanning and confirmed protective management continue; there is no forced close solely because the cap was reached.
* A trailing modification reads the existing GTT, preserves its actual target, includes the required ENTRY rule, validates tick/direction, and never cancel/recreates protection. Local state changes only after reconciliation confirms the broker result.
* Stale/future/crossed/invalid/out-of-order/missing data, token expiry, disconnect, rejected protection, ambiguous submission, unknown positions, quantity mismatch, and reconciliation failure stop entries and create an alert. Existing confirmed protection is preserved. No blind retry, fill assumption, cancellation assumption, or provider substitution occurs.
* A network timeout after a write is **ambiguous**. The intent remains unresolved until exact reconciliation proves a terminal outcome. A matching-order search is not a resubmission.
* **Reconcile read-only** is available while halted. It does not Arm or Activate. If GTT/order/position evidence is insufficient, the session remains halted and unresolved; external owner reconciliation is required.
* **Pause**, **Emergency stop**, shutdown, restart, and recovery revoke in-memory permits and require fresh Arm + Activate. Emergency stop does not force-liquidate or cancel confirmed protection.
* **Cancel unfilled** is a separate explicit ACTIVE-session operation. It is refused for an open position. Upstox GTT cancellation discards all associated legs/triggers, so it is never an automatic fault response.

## 7. Kronos artifacts and fallback

Kronos is optional and isolated from execution, broker, account, and risk state. The application uses the pinned upstream source commit and exact model/tokenizer revisions recorded in `config/kronos-small.json`. Every required file is size- and SHA-256-verified locally before inference. Weights are not bundled in Git or the installer.

The current source/model pins and expected official weight hashes are in that manifest. Install artifacts only through the operator's trusted artifact process, then inspect the Kronos status panel. A missing file, wrong size/hash, dependency failure, stale input, or inference exception makes Kronos unavailable/unverified. There is no synthetic forecast fallback and no Kronos attribution on the fallback path.

Artifact verification and inference-runtime readiness are separate status fields. The standard Windows package does **not** bundle the large optional Torch/Pandas inference stack or model weights. A source/operator runtime must install reviewed compatible versions of the upstream numerical dependencies and complete a real inference smoke test before `available_for_inference` can become true. No such runtime or inference result is claimed for the current build.

Validated non-Kronos families can continue only with their own exact evidence. A real forecast alone is insufficient: it must be linked to frozen chronological validation and the same verified artifact digest. Forecasts receive only completed-bar prefixes and cannot modify broker/risk state.

## 8. Research and evidence

Use `app.autonomy.research` or its CLI with exact instrument metadata. Raw Upstox JSON must come from `api.upstox.com` and identify the exact instrument in the source URL. CSV input is research-only and cannot become authoritative Upstox LIVE evidence; `data/sample.csv` is synthetic demonstration data.

Example shape (paths are operator-selected; this does not download data):

```text
python -m app.autonomy.research \
  --upstox-json /private/research/raw-response.json \
  --instrument-metadata /private/research/instrument.json \
  --source-url "https://api.upstox.com/v3/historical-candle/NSE_EQ%7C.../minutes/1/..." \
  --timeframe 1m --output /private/research/run-01
```

The runner uses expanding chronological folds with an embargo and a reserved holdout. It writes the holdout lock before evaluating the holdout. A second run in the same output directory is refused rather than allowing repeated holdout tuning. Use a new, genuinely independent dataset/config for new research; do not delete locks to optimize the same holdout.

Reports include per-family/side and instrument/regime net-of-cost expectancy, drawdown, trade counts, fills/rejections, and a **paired ablation**: Kronos-informed entries require the identical trend/momentum setup plus forecast confirmation, while the baseline runs that same setup without the forecast filter. Paired order identities make applicable fill/rejection schedules comparable. Forecast reports include MAE, RMSE, direction accuracy, interval coverage, Brier score, calibration bins, and real-artifact identity where available. Even this paired delta is not a causal guarantee or a promise of future performance. No report is a profitability or live-readiness claim.

To enable real Kronos research, add `--kronos` and provide verified local artifacts. Without them the report says `NO_REAL_FORECAST`; it never invents predictions.

Install only reviewed evidence JSON files into the per-user autonomy evidence directory. Review `data_sha256`, `config_sha256`, holdout lock, source, survivorship, gaps, costs, and artifact attribution. Evidence is a gate, not a promise of future returns.

## 9. Audit and data protection

The SQLite journal uses WAL and FULL synchronous mode. It stores session state, risk values, token fingerprints (not tokens), intent-before-submission transitions, broker IDs, reconciliation metadata, and a SHA-256 audit hash chain. Sensitive-looking fields are redacted. Execution permits are in-memory only and are never accepted from JSON or persisted.

Audit events can be downloaded/viewed through the authenticated UI. Verify the hash-chain indicator after recovery or suspected tampering. Do not export raw account profiles, access tokens, private IPs, or unredacted broker responses.

## 10. Owner-run complete-session PAPER shakedown

A PAPER shakedown requires operational acceptance and at least one complete market session with the actual data path. It is not a fixed profitability gate. No such complete session is asserted by this manual.

1. On a trading day, use a fresh read-only Upstox daily access setup and newly entered PAPER risk values. Select only exact, supported instruments. Keep the mode **PAPER**; connecting data must not change it.
2. Record the exchange session open/close from Upstox market timings. Begin observation before the open and keep the same process/session under observation through the close. Do not restart to conceal a fault or discard unresolved intents.
3. Explicitly Arm and Activate PAPER in the UI. Review scans, completed-bar counts, simulated order/GTT lifecycle, duplicate prevention, protection, reconciliation and the audit hash chain. Exercise stale data, disconnect, rejected/ambiguous outcomes and daily-loss entry blocking in a controlled way; existing exits/protection must continue and reactivation must require fresh approval.
4. At the end, reconcile and export the audit. Confirm there are no unresolved intents/faults, every open simulated position has confirmed protection or the account is flat, and a restart remains disarmed. A profitable or unprofitable day does not change operational acceptance.
5. Copy `docs/autonomy-shakedown.template.json` to a private working file. Replace every placeholder and zero/false value only with observed evidence. Never add access tokens, credentials, account numbers, private IPs or unredacted broker responses. Then run:

```text
python -m app.autonomy.shakedown /private/shakedown-evidence.json --output /private/shakedown-assessment.json
```

The offline validator requires observation across the complete exchange session, positive scan/completed-bar counts, a valid audit chain, zero unresolved intents/faults, zero broker order requests, unreachable broker mutations, and every operational check. It rejects credential/account-like fields and never echoes their values. Its report is explicitly **owner-attested, not independently verified**, has no profitability gate, and always says `live_ready: false` and `external_demo_verified: false`. It does not connect to Upstox, Arm/Activate, or create any order.

## 11. Verification and release limitations

Local tests use synthetic bars, fake accounts, and `httpx.MockTransport`; no real order endpoint is called. Python, JavaScript, Windows frozen/installer/portable checks must be reported separately with the exact commit and artifact hashes. Windows artifacts are unsigned unless an actual signing/verification step is shown. Smart App Control and owner-device behavior are not inferred from CI.

**Do not call the system live-ready** until the owner has supplied account-specific evidence for instrument/route permissions, current regulatory/static-IP/algo obligations, token expiry behavior, protection/reconciliation under faults, Windows operation, and a separately accepted external DEMO/shakedown. Real-money profitability is never guaranteed.
