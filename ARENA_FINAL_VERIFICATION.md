# ARENA FINAL VERIFICATION — 3.0.5

> **Historical snapshot only:** this 3.0.5 verification predates the current 3.2.0 source audit. Its test counts, release identity and artifact claims do not describe current HEAD. See [`AXIOM_CURRENT_AUDIT_REPORT.md`](AXIOM_CURRENT_AUDIT_REPORT.md) for current evidence.


> **PATCH 3.0.5 (2026-09-26) + PATCH 3.0.3 (2026-09-26) + FINAL HARDENING PASS (2026-09-25).** 3.0.5 adds the additive Market Event Listener (internet intelligence, transmission-path impact, Router context, dashboard panel, observational memory, 20 deterministic tests) atop the 3.0.3 sliced-order correctness; 3.0.3 fixes sliced-order accounting, per-child recovery, incremental fill pricing and adaptive cadence on top of the 3.0.2 hardening; 3.0.2 eliminated the remaining execution-path defects found in the full-code audit of 3.0.1. It is an additive release: no feature or working V1/V2.1/3.0/3.0.1/3.0.3 behavior removed, no risk control weakened. Historical version references (V0.6.x, V1, V2.1, FINAL 3.0.0/3.0.1/3.0.3 notes) are preserved as history; all *current-release* identity surfaces read **3.0.5**.

Date: 2026-09-25 · Vocabulary: **VERIFIED / NOT VERIFIED / NOT ATTEMPTED / FAILED** only.

## VERSION STATUS

**VERIFIED — canonical release identity is consistent:**

| Surface | Identity |
|---|---|
| Package metadata (`pyproject.toml`) | `name = "arena"`, `version = "3.0.5"` |
| `app.__version__` / `python -m app --version` | `arena` / `Arena 3.0.5` |
| CLI banner (`python -m app --help`) | "Arena 3.0.5 — PAPER ONLY trading research" |
| FastAPI title/version (`/openapi.json`, `/docs`) | `Arena` / `3.0.5` |
| `/health` + dashboard + `/api/v2/overview` | `3.0.5` (dynamic `__version__`) |
| Desktop window title | `Arena 3.0.5 — …` |
| README | "Arena — Quantitative Research & Trading Platform (FINAL 3.0.5)" + 3.0.5 patch highlights + 3.0.2 preserved block |
| Package archive | `trading_bot_final.zip` (+ `.sha256` sidecar) |
| Historical docs (V0.6-era, 3.0.0/3.0.1 notes, results/*.json) | preserved as history — intentionally unchanged |

## TEST COUNT (exact)

- Collected/executed: **491 passed, 0 failed, 0 skipped** (`pytest -q`,
  Python 3.13 venv; ×2 consecutive clean runs; 1 starlette-DeprecationWarning
  from the test client dependency — environmental, not a test outcome).
- Composition: 471 tests carried from 3.0.3 + **20 new tests** including
  `tests/test_settings_parser.py` (41) + `tests/test_dashboard_auth_flow.py` (26)
  and the 3.0.3 hardening (sliced accounting, per-child migration, incremental
  pricing, adaptive cadence) — plus fixes to existing `or True` / hardcoded-cwd tests.
- Test-count references corrected everywhere for this release (this file and
  README); the three release version pins
  (`test_upstox_live.py`, `test_upstox_v062.py`, `test_v1_dashboard.py`) now
  assert `__version__` dynamically (`3.0.5`).
- 3.0.1 tests updated ONLY where the 3.0.2 contract is strictly stronger
  (full lifecycle required for tradability; unmapped symbols rejected; the
  POST `tag` is the intent-derived reference; the single-instrument default
  key is gone). No assertion was weakened.

## FIXES IMPLEMENTED (implementation first; regression tests after each)

1. **Fail-closed multi-instrument routing** — `resolve_instrument_key` no
   longer falls back to the broker default key for an unmapped symbol
   (removed the documented-as-dangerous single-instrument leniency); missing
   identity, symbol/key mismatch and ambiguity all REJECT. The engine's
   `_act` BUY path AND `_submit` (both sides) reject with
   `RISK_REJECT:INSTRUMENT_IDENTITY_MISSING`; the engine never fabricates a
   mapping from `broker.instrument_key` (constructor fallback removed —
   identity comes only from the explicit map / provider mapping).
   Reconciliation and attribution use the order's own recorded identity
   (`fill symbol` fallback to `provider.symbols[0]` removed; honest
   `"UNKNOWN"` instead of a guess).
2. **Full lifecycle before trading** — `RouterStrategySource.lifecycle_
   eligibility` requires the COMPLETE recorded ladder
   RESEARCH → VALIDATION → WALK_FORWARD → APPROVED with current status
   ACTIVE at the FINAL EXECUTION BOUNDARY. promoted=True alone is NOT
   tradable; approved-but-inactive, degraded (PROBATION), shadowed, retired
   and unrecorded candidates are all blocked.
3. **No qualified candidate ⇒ no entry** — the engine now has an explicit
   authorization state (`_router_authorized`): with a strategy source,
   entries require an eligible router selection THIS cycle; zero/invalid/
   negative-OOS/degraded-only candidate sets produce ZERO entry orders
   (`RISK_REJECT:NO_QUALIFIED_CANDIDATE` is recorded for observability).
   The fallback tuple remains diagnostics-only and is never instantiated for
   execution. Risk-reducing exits are never blocked by the gate. A candidate
   that degrades mid-session stops entries immediately.
4. **UNKNOWN remote-order recovery (documented tag lookup)** — every intent
   now carries a durable intent-derived broker `tag`
   (`intent_id[:12]`, ≤20 chars) generated and durably persisted BEFORE the
   POST (documented V3 place-order `tag` field; Order History API accepts
   lookups by tag). State machine extended:
   UNKNOWN → REMOTE_LOOKUP → VERIFIED_MATCH / VERIFIED_ABSENCE / UNRESOLVED.
   On UNKNOWN, reconciliation performs the tag lookup and adopts the remote
   order ONLY after strict attribute matching (tag, transaction_type,
   quantity, instrument_token, price for LIMIT). Exactly one match → adopt +
   apply fills exactly once; zero matches on an authoritative response →
   VERIFIED_ABSENCE (final for that intent, never auto-resubmitted);
   multiple matches → UNRESOLVED (fail closed); unavailable/malformed lookup
   → stays UNKNOWN for a later attempt. All states are durable and
   resubmission-forbidden across restart. A blind re-POST is impossible by
   construction (NO_RESUBMIT_STATES covers every new state).
5. **Cancellation never lies** — `cancel()` rewritten: DELETE 200 → child
   CONFIRMED cancelled; rejected DELETE / network error → child reconciled
   via the documented history API; a still-working remote child keeps the
   logical order working (per-child status tracking on the order state).
   The logical order becomes CANCELLED only when EVERY child is confirmed;
   a child whose history shows FILLED reconciles to FILLED, never CANCELLED.
   States persist across restart; remote orders are never hidden.
6. **Adaptive promotion uses the FULL research gate** — `evaluate_and_
   maybe_promote` now runs `_promotion_gate` (fail closed): separated
   chronological validation split, minimum OOS trades, strict validation
   improvement, robustness ≥ 0.5, walk-forward (≥3 folds, ≥50% profitable),
   max drawdown ≤ 0.5 (router parity), lifecycle APPROVED+ACTIVE with the
   complete ladder. Missing evidence FAILS; the locked test remains
   report-only and can never enter any gate input (unchanged from 3.0.1).
   The rolling engine path records the gate outcome (no evidence ⇒ blocked).
7. **Hidden-bypass sweep** — searched and fixed: broker-default instrument
   substitutions (3 sites), `or True` (none in app), unconditional success
   classifications (probe classifier unchanged, fail-closed), silent
   exception swallows (only the two documented asyncio patterns remain),
   state changes after failed remote actions (cancel), direct broker calls
   outside OrderIntent (none in autonomous paths; the operator-invoked
   `sandbox-test --place-order` requires an explicit instrument key),
   production paths skipping router/lifecycle/risk (cmd_engine's engine is
   now router-gated; LOCAL_PAPER `paper-live` is the separate documented V1
   pipeline), hardcoded cwd assumptions (none found in app code).

8. **PATCH 3.0.3 — sliced execution correctness (forensic audit fix)**
   - **Per-child transition identity:** `sandbox_transitions` primary key now
     `(order_id, upstox_order_id, status, filled_quantity, ts)` — two children
     of one logical order with equal status/fill/timestamp never collide.
     A backward-compatible `schema_version 5` migration rebuilds the table
     and preserves every legacy row (idempotent, fresh DBs already have the
     new key).
   - **Repository idempotency:** `record_sandbox_transition` now checks
     `upstox_order_id` in its dedupe query.
   - **Sliced order accounting:** `SandboxOrderState` carries `child_filled`
     / `child_avg_price` baselines per child; `reconcile()` now treats every
     Upstox child id independently, aggregates `filled_quantity` as the sum
     over children, applies only the per-child delta at the **incremental**
     basis `(f_now·avg_now − f_prev·avg_prev)/Δ` (never re-applying a
     cumulative average), and prevents duplicate application of a child
     transition (idempotent). Partial-then-cancel correctly credits exactly
     the executed quantity.
   - **UNKNOWN recovery:** `lookup_intent_order` is slice-aware (multiple
     tag-matches whose quantities sum to the logical total are adopted as the
     complete child set; a single mismatch → VERIFIED_ABSENCE; otherwise
     → UNRESOLVED fail-closed). `recover_ledger_state` replays per-child
     increments chronologically at the incremental basis, including CANCELLED
     rows that carry executed quantity. `SandboxEngine.restore_from_repository`
     restores the COMPLETE `upstox_order_ids` list plus per-child fill/status
     baselines so restart + reconciliation neither loses nor duplicates.
   - **Adaptive cadence:** removed the hard-coded `register_cadence("momentum", 1)`
     in `cmd_engine`; `SandboxEngine._sync_cadence()` now ensures the ACTIVE
     strategy's cadence is registered (every-candle default when none
     declared, explicit declared `strategy.cadence` otherwise) and re-runs
     on every router strategy swap. An explicitly registered cadence is
     never clobbered. `EventDrivenDecisionEngine.cadence_for()` exposes the
     current registration.
   - **Test quality:** removed every `or True` tautology (now honest
     assertions over real report/evaluator contracts), replaced hard-coded
     `/home/user/trading_bot` cwd with dynamic `PROJECT_ROOT`, and
     updated `test_all_schema_versions_recorded` to `[1,2,3,4,5]`.
   - **Release identity:** bumped to 3.0.3 (`pyproject.toml`, `app.__version__`,
     dynamic `__version__` in all API surfaces, README/USER_MANUAL/desktop
     title, and this verification doc). API version fields now import
     `__version__` directly — they cannot drift.
9. **PATCH 3.0.5 — Market Event Listener (additive, information-only)**

   - **Collector** (`app/market_events/collector.py`): injectable fetcher, timeout/retry/backoff bounded at 2.0s, 30/min rate limit per-source, TTL cache, per-URL isolation, `safe_parse` blocks eval/exec/shell/dynamic code on downloaded content.
   - **Detector** (`detector.py`): deterministic `is_relevant/validate_source/normalize_event_type/extract_affected` with RELEVANT_KEYWORDS/IRRELEVANT_MARKERS, reputable-domain allowlist, confidence/relevance scoring.
   - **Impact** (`impact.py`): `TRANSMISSION` map for 13 event_types → paths/markets/sectors/effect, `assess()` merges, `decide_arena_response()` implements IGNORE/MONITOR/EVALUATE/NO_TRADE (low confidence/missing thesis/priced-in/uncertain/no strategy → NO_TRADE, contradicts → NO_TRADE, no qualified → NO_TRADE).
   - **Listener** (`listener.py`): orchestrates collection→detection→validation→dedup via `event_fingerprint` (SHA-256 headline+type+countries)→impact→Router context (`_has_qualified_strategy`)→hard-risk check (emergency STOP)→record; never `submit_order`/`place_order`/`modify_risk_limits`/`enable_live`; forbids direct order methods (`submit_order = _forbidden`).
   - **Persistence** (`app/database/models.py` schema_version 6, `repository.py` `MarketEventRepositoryMixin`): `market_events` (22 cols + fingerprint/transmission_paths/conflict_note, PK event_id, indexes type/timestamp), `market_event_sources` (PK event_id+source_url), `market_event_observations` (PK id, event_id/window, 5m/30m/1h/1d/3d/5d windows via `record_market_response` using existing snapshots).
   - **API** (`app/api/routes/market_events.py`, included in `server.py` with `Depends(authenticate)`): `GET /api/market-events`, `GET /{id}`, `POST /ingest`, `POST /{id}/observe`, `GET /{id}/observations`; bearer-protected, never exposes secrets, sanitizes `< >`.
   - **Dashboard** (`app/api/routes/dashboard.py`): `dashboard_data` now includes `market_events` (sanitized, 10 latest), HTML adds Market Events panel (EVENT/WHY/AFFECTED/CONFIDENCE/RESPONSE/ACTION/RESULT, escaped, no secrets, no raw HTML), JS renders with `esc()` and shows NO_TRADE etc.
   - **Testing** (20 deterministic, offline fixtures, no live fetch): relevant/irrelevant/duplicate/conflicting/low-confidence/timeout/failure/malformed/impact-oil/NO_TRADE→Router/no-direct-order/no-bypass-risk/no-modify-limits/no-enable-LIVE/persistence/restart-recovery/market-response/dashboard/no-secret-leakage + rate-limit/cache/isolation/no-eval checks in `tests/test_market_events_3_0_4.py`.
   - **Verification**: `or True` removed, pycache excluded, `/home/user` paths removed, Settings parser/dashboard-auth/sliced-order/recovery/hard-risk invariants preserved.



## NEW REGRESSION TESTS (exact, tests/test_v3_0_2_hardening.py — 42)

- TestFailClosedRouting (6): unmapped_symbol_rejected_at_broker,
  mismatched_symbol_key_rejected,
  tcs_signal_never_becomes_reliance_order_and_vice_versa,
  engine_blocks_unmapped_symbol_without_orders,
  reconciliation_uses_intent_identity_not_provider_default,
  broker_rejects_none_key_with_unmapped_symbol
- TestLifecycleBeforeTrading (5): promoted_but_not_approved_not_tradable,
  approved_but_not_active_not_tradable, retired_never_tradable,
  fully_approved_active_tradable, incomplete_ladder_blocked
- TestNoQualifiedCandidateNoEntry (5): zero_candidates_zero_entries,
  all_candidates_negative_oos_zero_entries,
  promoted_only_candidates_zero_entries,
  authorized_then_degraded_stops_future_entries,
  exits_still_allowed_without_authorization
- TestUnknownRecovery (7): tag_persisted_before_post,
  remote_accepted_response_lost_restart_lookup_adopts,
  no_match_marks_verified_absence_never_resubmits,
  multiple_matches_fail_closed_unresolved,
  strict_matching_rejects_wrong_attributes,
  lookup_unavailable_leaves_unknown_for_retry,
  unresolved_persists_across_restart
- TestHonestCancellation (7): successful_cancel_confirmed,
  rejected_cancel_keeps_order_working,
  rejected_cancel_with_filled_history_becomes_filled,
  cancel_timeout_keeps_working_until_reconciled,
  partial_slice_cancel_not_marked_cancelled,
  all_children_confirmed_then_cancelled, cancel_states_survive_restart
- TestFullPromotionGate (6): full_evidence_promotes,
  missing_robustness_blocks_despite_better_validation (integration proof),
  weak_walk_forward_blocks, drawdown_breach_blocks,
  unapproved_lifecycle_blocks, baseline_recording_stays_report_only
- TestFinalExecutionGraph (1): market_event_to_attribution_full_graph —
  MarketEvent → features/regime → OpportunityScanner → StrategyRouter →
  lifecycle eligibility → selected strategy → CapitalAllocator → final
  quantity → HARD risk gate → exact instrument → durable submission intent
  (tag) → broker POST → reconciliation → ledger → attribution; strategy
  version, instrument, quantity, allocated capital and decision metadata
  asserted at EVERY boundary.
- TestStaticBypassScans (5): no_or_true_in_app, no_bare_except,
  no_silent_exception_swallow, engine_never_defaults_to_broker_key,
  unresolved_states_forbid_resubmission

## EXTERNAL TEST STATUS

- Real Upstox REST/WSS connectivity, authenticated order placement,
  cancellation against the real sandbox, live market data, TOTP login:
  **NOT ATTEMPTED** (no credentials in this environment). The tag-lookup and
  cancel flows are implemented strictly per the CURRENT published Upstox
  documentation (V3 place order `tag` field; Order History by order_id or
  tag; DELETE cancel semantics) and verified against fakes only.
- Termux / Android, Python 3.14, Qt desktop GUI boot: **NOT ATTEMPTED**.
- No external success is claimed anywhere in this document.

## LIVE-DISABLED CONFIRMATION

**VERIFIED** — `Settings().mode == "PAPER"`, `live_trading is False` by
default; `LIVE_TRADING=true` fails closed at settings validation; paper-test
reports `paper_only: {mode: PAPER, live_trading: false}`. Nothing in 3.0.5
adds or enables a live path (3.0.2 hardening preserved).

## VERIFICATION PERFORMED THIS RELEASE

- `pytest -q`: **491 passed / 0 failed / 0 skipped** (×2 consecutive) — VERIFIED.
- `compileall app desktop tests`: clean — VERIFIED.
- Import checks: fresh-venv `import app` → 3.0.5 — VERIFIED.
- CLI: `--version`, `--help`, `initialize` ×2 (idempotent), `paper-test`
  (offline; final equity 99637.58463385397 = exact expected invariant) — VERIFIED.
- Fresh virtual-environment editable install (`pip install -e .`) — VERIFIED.
- Secret scan (hardcoded credential patterns): none — VERIFIED.
- Tautology/bypass scans (`or True`, bare except, broker-key defaults in
  engine): none — VERIFIED (also enforced as live tests).
- Package extraction + tests from the extracted tree — VERIFIED (below).
- Manual production-path trace of every changed function (broker submit/
  resolve/cancel/lookup/reconcile; engine _act/_submit/_maybe_swap_strategy/
  process_candle; adaptive select/lifecycle_eligibility; evaluator gate) —
  VERIFIED against the autonomous `cmd_engine` wiring.

## PACKAGE PATH

- Archive: `trading_bot_final.zip` (exact file list at build time)
- SHA-256: recorded verbatim in the sidecar `trading_bot_final.zip.sha256`
  (`sha256sum trading_bot_final.zip` must match it after download).
- Excludes: `.venv`, `.env`, `data/{cache,history,universe,datasets}`, logs,
  backups, results, temp, databases, `__pycache__`.

## KNOWN LIMITATIONS

- Core `pyproject.toml` declares no runtime dependencies by design; optional
  subsystems need fastapi/pydantic/google-auth/protobuf/websockets/httpx.
- Real broker partial-fill/cancel behavior is emulated by the sandbox fake;
  only fakes were exercised — real-API behavior NOT ATTEMPTED.
- Desktop UI ships as source; GUI boot requires Qt + display (NOT ATTEMPTED).
- With NO lifecycle-approved active candidate, the sandbox engine will not
  enter at all (by design); research must approve a candidate first.
- The rolling engine evaluator cannot auto-promote (no robustness/walk-
  forward evidence exists at runtime); promotions come from the research
  pipeline — conservative by design.

## FINAL RULE COMPLIANCE (3.0.5 acceptance A–N — 3.0.2 A–N preserved)

- A no wrong-instrument fallback — VERIFIED · B only approved+active trade —
  VERIFIED · C no candidate ⇒ no entry — VERIFIED · D final qty before risk
  gate — VERIFIED (3.0.1, kept) · E UNKNOWN never duplicated — VERIFIED ·
  F failed cancellations never become CANCELLED — VERIFIED · G promotion
  requires the complete gate — VERIFIED · H locked-test isolation — VERIFIED ·
  I full execution graph e2e — VERIFIED · J existing tests pass (362) —
  VERIFIED · K new adversarial tests pass (42) — VERIFIED · L LIVE disabled —
  VERIFIED · M no unearned external claims — VERIFIED · N documentation
  matches the release — VERIFIED.
