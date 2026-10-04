# AXIOM-MONEY-PRINTER — Master Prompt Requirement Matrix

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
| 17-19 | Kronos isolation + studio + forecast contract | `app/models/studio.py`, `axiom/kronos/`, studio routes | test_kronos_*, intel_system | VERIFIED (weights download BLOCKED EXTERNALLY — HuggingFace unreachable; mocked+labelled) |
| 20-22 | Research lab / memory / lineage | repository research tables, `/api/research/memory` | test_axiom_intel_system | VERIFIED |
| 23-24 | Latency tiers + observability | `app/system/latency.py`, `/api/latency` | test_axiom_intel_system | VERIFIED |
| 25 | Concept drift | `app/intel/drift.py`, `/api/research/drift` | test_axiom_intel_system | VERIFIED |
| 26, 97 | Compute governor + scheduler awareness | `app/system/resources.py`, trading-critical flag | test_axiom_intel_system | PARTIAL → scheduler session/quota awareness MISSING |
| 27-30 | Persistent core, supervisor, state persistence, recovery | `app/runtime.py`, `app/instance.py`, tasks.recover | frozen E2E, v310 tests | VERIFIED |
| 31-37 | LIVE first-class state machine, readiness, guards, reconciliation, rails, emergency | `app/live/session.py`, guard_chain, preflight, `/api/live/*` | test_axiom_markets_live, matrix | VERIFIED |
| 38-40 | Upstox OAuth, static IP, rate limits | `app/brokers/upstox*.py`, settings | test_upstox_* | VERIFIED (production session BLOCKED EXTERNALLY — no credentials) |
| 41-44 | Broker abstraction, paper, sandbox, exact accounting | `app/brokers/`, paper_trading | test_paper_*, test_execution | VERIFIED |
| 45-48 | GUI shell + Command Center + Markets workstation | `app/static/arena-app.js` cockpit | test_v310_frontend_integrity, gui matrix | VERIFIED |
| 49 | Chart layouts 1/2/4/6/8, linked crosshair, focus mode F | single chart only | none | MISSING |
| 50-51 | Research workspace + Model Arena | workstation routes, studio benchmark | test_axiom_intel_system | VERIFIED |
| 52-55 | Trading/Risk/Intelligence/Reports pages | arena-app.js pages | gui matrix | VERIFIED |
| 56 | Alerts: categories, ack, escalation | `app/live/alerts.py`, `/api/v2/alerts(+ack)` | test_live_ops_v2_1 | PARTIAL → escalation policy for unacked criticals MISSING |
| 57-58 | Task Center + System Control Center | tasks routes, `/api/system/control` | test_axiom_intel_system, matrix | VERIFIED |
| 59-61 | Wizard, universe studio, workspace customization | wizard, renderUniverseStudio | test_v310_wizard_flow | VERIFIED (layouts saved per-browser) |
| 62-64 | UI states, design system, perf targets | arena.css, freshness labels, latency budgets | test_axiom_gui_action_matrix | VERIFIED |
| 65-66 | Replace legacy console, GUI-only flows | cockpit supersedes console | test_feature_console | VERIFIED |
| 67 | Credential vault OS-secure + encrypted fallback | `app/brokers/secure_credentials.py` | test_v310_mode_isolation et al. | VERIFIED |
| 68 | Backup/restore integrity | `app/system/backup.py` | test_axiom_intel_system | VERIFIED |
| 69 | Update system | `app/system/updater.py` | test_axiom_updater | VERIFIED |
| 70-74 | Storage, event bus, API contract, audit, security | database/, bus, audit tables, security tests | test_v2_migrations, test_final_security | VERIFIED |
| 75-79 | Market events, validation, backtest realism, determinism | market_events/, backtesting/ | dedicated suites | VERIFIED |
| 80-89 | Test pyramid, frozen build, CI gates, diagnostics, failure injection, safety boundary | CI workflow, frozen_e2e.py | CI green on 3 platforms | VERIFIED |
| 90 | User education in product | confirmations only; per-action consequence text | none | PARTIAL |
| 91 | Legal/eligibility compliance | preflight static-IP/eligibility checks | test_live_hardening_v2 | VERIFIED |
| 92-93 | Upstox facts, truthful states | settings + truthful labels throughout | suites | VERIFIED |
| 94 | No fake AI | evidence kinds + STUB labels | test_kronos_ui | VERIFIED |
| 95 | Observational learning | learning/ journals | test_v1_learning | VERIFIED |
| 96 | Autonomous research loop bounded/quotas | next-best suggestion only | none | MISSING (quota/bounded loop) |
| 98 | Model agreement/disagreement tracking | opportunity flag only | none | MISSING (service) |
| 99 | Counterfactual simulation | analogue forward returns + text field | none | MISSING (simulator) |
| 100-103 | Operational UX, responsive, observability, logging | cockpit feedback, css, latency GUI, scrubbed logs | suites | VERIFIED |
| 104-108 | Migrations, frozen recovery test, test realism, docs, naming | schema v9, BUILD_META, manual | suites | VERIFIED |
| 109-122 | Preserve existing hardening | integrated | full legacy suites | VERIFIED |
| 123-132 | Tray, pages, status bar | cockpit | suites | VERIFIED |
| 133-150 | Live UX, freshness, sessions, shadow, governance | live/*, session engine, shadow.py | test_axiom_markets_live | VERIFIED |

## Work queue (next priorities)

1. §98 Model agreement/disagreement service — `app/intel/agreement.py`.
2. §99 Counterfactual simulator — `app/intel/counterfactual.py`.
3. §56 Alert escalation for unacknowledged criticals + category filter.
4. §96/§97 Research scheduler quotas + session awareness.
5. §49 Chart layouts + focus mode.
6. §90 Consequence-explanation text on consequential actions.
