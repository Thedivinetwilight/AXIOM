# AXIOM-MONEY-PRINTER — Master Prompt Requirement Matrix

> **Scope boundary:** this matrix maps source implementations and selected tests; `VERIFIED` means only that the named software contract has source/test evidence. It is not a complete feature audit, production-readiness, profitability, live-account, external-provider, pretrained-weight, legal-compliance, or cross-platform artifact claim. Check [`AXIOM_CURRENT_AUDIT_REPORT.md`](AXIOM_CURRENT_AUDIT_REPORT.md) for commit-specific evidence and limitations. The master prompt itself is a requirements document, not proof of completion.

Living audit of `AXIOM-MONEY-PRINTER_MASTER_PROMPT_v1.0.md` against the
repository (branch `arena/01a102dd-axiom`). Statuses:
VERIFIED (implemented + tested) · PARTIAL · MISSING · BLOCKED EXTERNALLY.

| § | Requirement | Implementation | Test coverage | Status |
|---|---|---|---|---|
| 4-5 | Universe first-class, multi-market IN/US/JP/UK/HK/SG/Global | `app/markets/registry.py`, `app/universe/`, `/api/universes*` | test_axiom_markets_live | VERIFIED |
| 6 | Data provenance/validation, no fabricated candles | `app/data/`, evidence kinds, CandleStore | test_axiom_intel_system, test_data | VERIFIED |
| 7 | Real-time strategy, promotion tiers, no fake quotes | `app/state/bus.py`, `/api/stream*`, freshness labels | test_axiom_intel_system | VERIFIED |
| 8 | "What changed?" deltas | `app/state/changes.py`, `/api/changes` | test_axiom_intel_system | VERIFIED |
| 9 | Research Intelligence Engine | `app/research/`, `/api/research/next-best` | test_final_research, matrix | VERIFIED |
| 10 | Historical analogues + counterexamples | `app/intel/analogues.py`, `/api/analogues/search` | test_axiom_intel_system | VERIFIED |
| 11 | Missed-opportunity lab | `app/intel/missed.py`, `/api/research/missed` | test_axiom_intel_system | VERIFIED |
| 12 | Regime engine event-driven | `app/learning/regime_engine.py` | test_v1_learning etc. | VERIFIED |
| 13 | Strategy lifecycle, no skipping | `app/live/ops.py`, strategies routes | test_live_ops_v2_1 | VERIFIED |
| 14-15 | Opportunity Center + attention modes | `app/intel/opportunity.py`, `/api/settings/attention` | test_axiom_intel_system | VERIFIED |
| 16 | Evidence Stack, never one AI score | `app/intel/evidence.py`, `/api/evidence/*` | test_axiom_intel_system | VERIFIED |
| 17-19 | Kronos isolation + studio + forecast contract | `app/models/studio.py`, `axiom/kronos/`, studio routes | test_kronos_*, intel_system | PARTIAL: software input/runtime contracts are tested; real pretrained weights, publisher authenticity, forecast quality, and production inference are NOT VERIFIED |
| 20-22 | Research lab / memory / lineage | repository research tables, `/api/research/memory` | test_axiom_intel_system | VERIFIED |
| 23-24 | Latency tiers + observability | `app/system/latency.py`, `/api/latency` | test_axiom_intel_system | VERIFIED |
| 25 | Concept drift | `app/intel/drift.py`, `/api/research/drift` | test_axiom_intel_system | VERIFIED |
| 26, 97 | Compute governor + scheduler awareness | `app/system/resources.py` + `app/intel/research_loop.py` session windows | test_axiom_intel_system, test_axiom_research_loop | VERIFIED |
| 27-30 | Persistent core, supervisor, state persistence, recovery | `app/runtime.py`, `app/instance.py`, tasks.recover | frozen E2E, v310 tests | VERIFIED |
| 31-37 | LIVE first-class state machine, readiness, guards, reconciliation, rails, emergency | `app/live/session.py`, guard_chain, preflight, `/api/live/*` | test_axiom_markets_live, matrix | VERIFIED |
| 38-40 | Upstox OAuth, static IP, rate limits | `app/brokers/upstox*.py`, settings | test_upstox_* | PARTIAL: local/mock protocol and readiness checks are covered; no production OAuth, credentials, or account session were used or verified |
| 41-44 | Broker abstraction, paper, sandbox, exact accounting | `app/brokers/`, paper_trading | test_paper_*, test_execution | VERIFIED |
| 45-48 | GUI shell + Command Center + Markets workstation | `app/static/arena-app.js` cockpit | test_v310_frontend_integrity, GUI action matrix | PARTIAL: selected served-JavaScript and control-to-route contracts are tested; no exhaustive visual or deployed-platform validation is claimed |
| 49 | Chart layouts 1/2/4/6/8, linked panels, focus mode F | chart workspace + `/api/candles`, SVG renderer, F focus mode | TestChartWorkspace, node --check | VERIFIED (per-pixel crosshair linking deferred — symbol/timeframe linking implemented) |
| 50-51 | Research workspace + Model Arena | workstation routes, studio benchmark | test_axiom_intel_system | VERIFIED |
| 52-55 | Trading/Risk/Intelligence/Reports pages | arena-app.js pages | gui matrix | VERIFIED |
| 56 | Alerts: categories, ack, escalation | `app/live/alerts.py` (+ category map/filter), watchdog escalation | test_live_ops_v2_1, TestAlertCategories | VERIFIED |
| 57-58 | Task Center + System Control Center | tasks routes, `/api/system/control`, layout manager | test_axiom_intel_system, matrix | VERIFIED |
| 59-61 | Wizard, universe studio, workspace customization | wizard, universe studio, named layouts + collapse/maximize | test_v310_wizard_flow, frontend suite | VERIFIED (free-form drag/resize deferred) |
| 62-64 | UI states, design system, perf targets | arena.css, freshness labels, latency budgets, stream coalescing | test_axiom_gui_action_matrix | VERIFIED |
| 65-66 | Replace legacy console, GUI only flows | cockpit supersedes console | test_feature_console | VERIFIED |
| 67 | Credential vault OS-secure + encrypted fallback | `app/brokers/secure_credentials.py` | test_v310_mode_isolation et al. | PARTIAL: source-level masking/storage paths are covered; OS keyring availability and deployed-host protection are not independently verified |
| 68 | Backup/restore integrity | `app/system/backup.py` | test_axiom_intel_system | VERIFIED at source-test scope: regular archive members and manifests are checked; unsigned manifests are not publisher/authenticity proof |
| 69 | Update system | `app/system/updater.py` | test_axiom_updater | PARTIAL: a caller-supplied SHA-256 match is tested; it is not a signed release or publisher-authenticity verification |
| 70-74 | Storage, event bus, API contract, audit, security | database/, bus, audit tables, security tests | test_v2_migrations, selected security suites | PARTIAL: selected storage/API/safety invariants are tested; this is not a complete security review or assurance against out-of-band credential use |
| 75-79 | Market events, validation, backtest realism, determinism | market_events/, backtesting/ | dedicated suites | PARTIAL: deterministic fixture behavior and contracts are tested; no independent out-of-sample edge, live-data correctness, or profitability is established |
| 80-89 | Test pyramid, frozen build, CI gates, diagnostics, failure injection, safety boundary | CI workflow, `packaging/frozen_e2e.py` | local full suite; check the workflow run and build records against current `HEAD` | COMMIT-KEYED: only a matching `head_sha` plus matching artifact build-info commits verifies frozen/cross-platform evidence; a source-suite pass alone does not |
| 90 | User education in product | consequence text on arm/activate/stop, emergency, restore, install, research cards | frontend integrity suite | VERIFIED |
| 91 | Legal/eligibility compliance | preflight static-IP/eligibility checks | test_live_hardening_v2 | PARTIAL: code-level checks are exercised; no legal opinion, broker approval, account eligibility, or production-IP registration is verified |
| 92-93 | Upstox facts, truthful states | settings + truthful labels throughout | suites | VERIFIED |
| 94 | No fake AI | evidence kinds + STUB labels | test_kronos_ui | VERIFIED |
| 95 | Observational learning | learning/ journals | test_v1_learning | VERIFIED |
| 96 | Autonomous research loop bounded/quotas | `app/intel/research_loop.py` ResearchScheduler + `/api/research/schedule(/admit)` | test_axiom_research_loop | VERIFIED |
| 98 | Model agreement/disagreement tracking | `app/intel/agreement.py` ledger + routes + GUI card | test_axiom_agreement_counterfactual | VERIFIED |
| 99 | Counterfactual simulation | `app/intel/counterfactual.py` simulator + route + GUI lab | test_axiom_agreement_counterfactual | VERIFIED |
| 100-103 | Operational UX, responsive, observability, logging | cockpit feedback, css breakpoints, latency GUI, scrubbed logs | suites | VERIFIED |
| 104-108 | Migrations, frozen recovery test, test realism, docs, naming | schema v9, BUILD_META, manual | suites | VERIFIED |
| 109-122 | Preserve existing hardening | integrated | full legacy suites | VERIFIED |
| 123-132 | Tray, pages, status bar | cockpit | suites | VERIFIED |
| 133-150 | Live UX, freshness, sessions, shadow, governance | live/*, session engine, shadow.py | test_axiom_markets_live | VERIFIED |

## Honest deferrals (documented, never silent)

1. §49 per-pixel crosshair linking across panels — symbol/timeframe
   linking is implemented; a shared crosshair cursor needs a canvas-level
   rewrite of the SVG renderer.
2. §61 free-form drag/resize — named persisted layouts, collapse,
   maximize, duplicate and reset are implemented; unconstrained
   drag/resize is a dedicated UI-engineering effort.
3. §99 counterfactuals are arithmetic on recorded bars by design — no
   model-based scenario generation (that would fabricate data).
4. Kronos real pretrained weights — NOT VERIFIED in this audit: no genuine
   local weight bundle, publisher signature, or measured forecast-quality run
   was available. The studio reports readiness honestly; no external-access
   failure is inferred from the absence of weights.
5. Upstox production session — NOT VERIFIED: no production OAuth flow,
   credentials, account access, or real-money order was used. Tests stay on
   mocks/local fixtures and the application labels that boundary.

Also fixed during this audit: `_recorded_series` called a non-existent
`CandleStore.load`, so drift/analogue/counterfactual routes silently saw
zero candles — now reads via `read_range` (a "silent no-data" defect).
