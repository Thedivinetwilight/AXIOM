# AXIOM terminal redesign — audit and acceptance checklist

Baseline checked on 2026-10-25: branch `arena/19f4cba3-axiom`, draft PR #8 and
prerelease `v3.4.0a1` all referenced `83e371a29a3eb95f853224089d55fe5f87a59ad5`.
The source tree matched that commit before editing. No real Vantage account was
available to the agent and no credential or live action is part of this plan.

## Phase 0 audit

| Area | Existing implementation | Evidence boundary / extension decision |
|---|---|---|
| Eight operator pages | Overview, Instruments, Charts/order, Orders/positions, Risk/lifecycle, Connections, Activity/audit, Settings/help in `app/static/terminal/` | Real HTML/CSS/JS with authenticated jsdom actions. Redesign every page while preserving element/API contracts where useful. |
| Local paper | `PaperBroker`, `PaperSimulator`, manual review, idempotency and SQLite state | Real local simulation with deterministic tests; fills are not broker fills. Preserve and label. |
| Independent DEMO data | `app/market_data/` read-only bindings, exact discovery, fresh ticks, closed bars, fail-closed MT5 paper provider | Real Windows implementation, fake-provider tested; no real terminal verified by agent. Preserve no-order surface. |
| Vantage execution | `OfficialMT5Gateway` → `MT5Connector` → `VantageService` → authenticated routes | Real official-terminal code for review/submit/cancel/close/modify/reconcile; DEMO/LIVE externally unverified. Extend diagnostics, not the safety model. |
| Risk/lifecycle | Server risk limits, readiness, separate Arm/Activate, persistent emergency stop, disarm-on-change | Real code with fake success/failure/restart tests. No bypass or unattended strategy execution. |
| Credentials | Per-environment Windows DPAPI blobs, masked metadata, local bearer token | Real DPAPI verified on Windows with dummy values. Never expose or substitute plaintext. |
| Charts/data | MT5 bars/ticks for Vantage; independent DEMO MT5 for paper; legacy replay/CSV/Alpaca paths | Replay explicitly labeled. No silent substitution. Reuse normalized bars/quotes and canvas rendering. |
| Listed US data | Read-only Alpaca historical/1m stream for local US paper | Separate from MT5/CFDs; external entitlement unverified and no listed execution router. Add explicit asset-identity/provider boundary only. |
| APIs | Authenticated paper, market-data and `/api/trading/*` routes; legacy research APIs | Keep bearer protection and relative browser URLs. Add read-only diagnostics/watchlist-safe UI without secret persistence. |
| Tests | Python unit/integration/restart/negative suites and two jsdom/canvas harnesses | Fakes are not external connectivity. Expand coverage for every new major control and metric. |
| Windows release | Exact-commit tests, PyInstaller, frozen smoke, Inno install/upgrade/uninstall, hashes and prerelease | Preserve per-user data and both artifacts. No signing claim. |
| Manuals | Operator and Windows guides, local DEMO procedure | Update page behavior, benchmark conditions, listed-asset setup and separate LIVE checklist. |

## Acceptance checklist

- [x] Coherent responsive visual system and working loading/empty/error/focus states on all eight pages.
- [x] Searchable account-discovered instruments plus editable watchlists that never alter execution selection.
- [x] Fluid bounded candle chart with timeframe, zoom/pan, crosshair, quote bid/ask/spread/source age, optional indicators and drawing tools.
- [x] Docked reviewed ticket with product, side, MT5 lots or paper units, estimated value/currency/spread/cost/margin/risk context.
- [x] Explicit LOCAL PAPER / VANTAGE DEMO / VANTAGE LIVE identity and independent data/execution/risk states.
- [x] Existing live review → final confirmation, risk, freshness, margin, idempotency, ambiguity and reconciliation paths retained; no automatic order or live agent test.
- [x] Feed receive, chart API/render, order API, MT5 acknowledgement and reconciliation measured separately with bounded samples.
- [x] Documented load benchmark reports actual p50/p95/p99 and identifies fake/local conditions; no zero-latency or universal claim.
- [x] Listed-share/ETF provider interface keeps asset identity and routing explicit; no CFD substitution or unsupported execution claim.
- [x] Deterministic backend/UI/security/restart tests and complete Python/JavaScript/static gates pass.
- [ ] Real-Windows frozen/install/upgrade/uninstall gates pass and both unsigned artifacts/manuals/metadata/hashes are published from exact HEAD.
- [ ] PR remains draft/unmerged; final report separates fake, DEMO and LIVE evidence and includes local DEMO and LIVE-owner checklists.

Local gates on 2026-10-25: **699 passed, 3 skipped**; both JavaScript harnesses passed;
terminal harness: **85 authenticated fake API calls, 1,085 candle/background draw calls**;
benchmark harness: **85 calls, 6,005 fillRect calls**, two expected reviewed submissions and
zero unexpected submissions. The fake backend benchmark made exactly 12 `order_send`
calls and reconciled all 12 as FILLED. The Windows/publication item remains intentionally
unchecked until the exact committed source passes the real Windows runner. The final
PR/release report supplies that evidence.

Tests and any later demo results establish neither profitability nor live readiness.
