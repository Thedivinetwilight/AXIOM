# LIVE TRADING SWITCH — implementation report

**Feature:** a user-facing PAPER/LIVE switch in the Arena workstation, with a
dedicated live-activation wizard, a safe switch-off drain, masked credential
handling and a supervised live engine that starts only after an explicit
final confirmation.

**Repository:** `Thedivinetwilight/AXIOM` · **Branch:** `arena/c292d276-axiom`
**Base commit:** `4317e3a` (`fix/axiom-v3.2.4-release`) · **Work:** uncommitted
in the working tree at the time of writing — the exact commit SHA and the CI
run are recorded in §10.

> **No real account, no real credential, no real order was used at any point.**
> Every test runs against a disposable Arena home, a throwaway SQLite
> database, a fixture OAuth exchange, a fixture credential verifier and the
> in-process `MockUpstoxExchange` (an `httpx.MockTransport`; no socket is
> opened). **This report does not claim production readiness, broker
> approval, profitability or a real-account successful trade.**

---

## 1. What was built

| Layer | What | Where |
|---|---|---|
| UI | PAPER/LIVE segmented switch in the top bar and on the Trading view; four-step activation wizard (Connect → Limits → Readiness → Confirm); switch-off drain dialog; emergency stop; masked credential status | `app/static/arena-live.js` (new), `app/static/arena-app.js`, `app/static/arena.css` |
| API | Eight authenticated endpoints for the switch (snapshot, book, configuration, OAuth URL, OAuth exchange, forget credential, start, stop) | `app/api/routes/live_activation.py` (new) |
| Service | The switch's server side: live configuration file, credential handling, limit validation, ARM→ACTIVATE sequencing, drain, masked status | `app/live/activation.py` (new) |
| Plumbing | `Settings.from_env(env_overrides=…)`; the supervised child receives the live configuration; `remove_values()` for the live file; a start-up that always removes the live switches | `app/config/settings.py`, `app/live/supervisor.py`, `app/config/envfile.py`, `app/api/server.py` |
| Docs | The operator runbook, plus pointers from the two existing live runbooks and the README | `LIVE_TRADING_SWITCH.md` (new), `LIVE_TRADING_ENABLEMENT.md`, `LIVE_OPERATIONS.md`, `README.md` |
| Tests | Backend state machine (73), end-to-end with the mock exchange (3), readiness/blockers (10), frontend action matrix (23) | `tests/test_live_switch_*.py` (new) |

### The design in one paragraph

The switch is a **control, not a command**. Moving it never places an order
and never changes the installation: it opens the wizard. The wizard collects a
broker credential through the broker's **official OAuth page** (Arena never
asks for the broker website password, PIN, TOTP seed or a one-time code),
collects the hard risk limits, shows the **existing** readiness contract
check by check, and then asks for the **exact configured confirmation
phrase**. Only that phrase writes the four live switches, starts the
supervised child engine, ARMs and ACTIVATEs the **existing** state machine.
Every refusal leaves the installation in PAPER with the exact reason
displayed.

---

## 2. Files changed

### New

| File | Purpose |
|---|---|
| `app/live/activation.py` | `LiveActivationService`: the switch's server side (~1200 lines) |
| `app/api/routes/live_activation.py` | The authenticated activation API (8 endpoints) |
| `app/static/arena-live.js` | The switch module, the wizard, the drain dialog |
| `tests/test_live_switch_activation.py` | Backend state-machine and refusal matrix (73 tests) |
| `tests/test_live_switch_end_to_end.py` | Mock-exchange end-to-end, including the order-after-confirmation proof (3 tests) |
| `tests/test_live_switch_readiness.py` | Readiness blockers, token freshness, emergency stop, open book, secret handling (10 tests) |
| `tests/test_live_switch_frontend.py` | Frontend action matrix / route mapping (23 tests) |
| `LIVE_TRADING_SWITCH.md` | The operator runbook |
| `LIVE_TRADING_SWITCH_IMPLEMENTATION_REPORT.md` | This report |

### Modified (this feature)

| File | Change |
|---|---|
| `app/config/settings.py` | `Settings.from_env(env_overrides=…)` — additive; the live configuration can be applied on top of the ambient environment without touching it |
| `app/live/supervisor.py` | `start(..., env_overrides=None)` — the supervised child receives the live configuration |
| `app/config/envfile.py` | `remove_values()` — the switch-off removes the four switches from the live file entirely |
| `app/api/server.py` | Registers the activation router; a start-up always removes the four live switches (fail-closed) |
| `app/api/routes/frontend.py` | Serves `arena-live.js`; the app shell loads it |
| `app/static/arena-app.js` | The top-bar switch host, `mountLiveSwitch()`, the Trading-view switch card; **plus two pre-existing dead routes fixed** (`/api/live/readiness` → `/api/live/readiness-center`, `/api/live/history` → `/api/live/session/history`) |
| `app/static/arena.css` | `.mode-switch`, `.mode-seg`, `.dialog.wide` |
| `app/api/routes/paper.py` | The paper engine refuses to start while the installation is in live mode (it would otherwise read the live data source/timeframe) |
| `packaging/arena.spec` | Bundles `app/static/arena-live.js` |
| `tests/test_v310_frontend_integrity.py` | The new asset is parsed as a served asset |
| `README.md`, `LIVE_TRADING_ENABLEMENT.md`, `LIVE_OPERATIONS.md` | Point at the new runbook and describe the GUI workflow |

> The working tree also contains the **unrelated first and second incident**
> work (paper-database recovery, Windows CI hardening, `docs/`,
> `packaging/*`, `arena.py`, `app/runtime.py`, …). It is committed together
> because the sandbox reset removed those commits from this repository; the
> report therefore does not attribute those changes to this feature.

---

## 3. State transitions

```
PAPER (default, fresh install and every ordinary restart)
  │  operator moves the switch to LIVE
  ▼
  wizard opens — nothing changes, no order, no engine
  │  step 1 connect (OAuth)  → credential stored, verified against the LIVE host
  │  step 2 limits           → validated, finite, non-zero, written to live.env
  │  step 3 readiness        → every check listed; BLOCKED/UNKNOWN stop here
  │  step 4 confirm          → the exact configured phrase
  ▼
LIVE_CONFIGURED   (switches written, engine spawned, ARMED)
  ▼  ACTIVATE (readiness re-checked, a fresh authorization epoch minted)
LIVE_ACTIVE       (only shown after the backend/supervisor confirmed it)
  │  operator moves the switch to PAPER
  ▼
drain: stop new entries → request the child to drain → disarm (epoch
invalidated) → remove the four switches → PAPER
```

**The state machine is the existing one.** `app/live/activation.py` only
sequences `app/live/session.py` (ARM/ACTIVATE/PAUSE/STOP/DISARM),
`app/live/supervisor.py`, `app/live/preflight.py` and `app/live/gateway.py`.
No second live state machine was introduced, and no existing guard was
weakened.

### The four switches

`MODE=LIVE`, `LIVE_TRADING=true`, `BROKER=UPSTOX_LIVE`,
`LIVE_CONFIRM=I-ACCEPT-REAL-MONEY-RISK`.

They are written **only** by the final activation and removed again by the
switch-off. A start-up always removes them: **a restart can never come up
live**, and no background job, recovery path or timer can arm or activate.

---

## 4. Credential handling (no secrets anywhere)

| Property | Implementation |
|---|---|
| Storage | `<ARENA_HOME>/live.env` — atomic writes, owner-only permissions (0600 on POSIX, owner-only ACL on Windows) via the existing `app/config/envfile.py` |
| No vault | The repository has **no** OS-backed keyring. The documented fallback — the per-user owner-only configuration file — is used **deliberately** and is disclosed to the operator in the UI and in the runbook. It is never a silent fallback. |
| Never exposed | The token is never returned by an endpoint, never rendered in the UI, never logged, never audited, never put in a URL, never in a crash report and never in the test output |
| What the UI sees | `present`, `state` (absent/unverified/verified/changed/expired), `age_hours`, `masked` (a length hint only), `reconnect_required` |
| Sandbox vs live | A credential that authenticates only on the **sandbox** host is refused explicitly; the live host must verify it |
| Broker password / PIN / TOTP / OTP | Never requested, never accepted, never stored — only the OAuth client id/secret, the redirect URI and the authorization code |
| Failure to store | The activation is refused; there is no second place where the secret could end up |

The live configuration lives in its **own** file, not in the installation's
main `.env`: the main file describes the PAPER installation (its database, its
saved paper account, its timeframe) and must not be rewritten by a live
activation, or the saved paper account would become incompatible with the
next start-up.

---

## 5. Risk controls exercised

* **Hard limits** — `MAX_POSITION_VALUE`, `MAX_OPEN_POSITIONS`,
  `MAX_DAILY_LOSS`, `MAX_TRADES_PER_DAY`: each must be present, finite and
  strictly positive. `0` (unlimited), negatives, `NaN`, `±Infinity` and
  malformed values are refused with a named reason. No default is invented.
* **Canary** — `LIVE_CANARY_MAX_NOTIONAL` / `LIVE_CANARY_ORDERS`; an
  incompatible combination is refused.
* **Drawdown guard** — `LIVE_MAX_EQUITY_DRAWDOWN` must be inside its range.
* **The gateway** — every live order still passes `app/live/gateway.py`:
  the intent check, the authorization epoch, the session state, the
  live-engine control record, the lease, the supervisor/readiness re-check,
  the emergency stop, the exact confirmation phrase, the broker identity, the
  entry-ops authorization (token freshness), the position-reduction check,
  the guard chain, the duplicate check, the durable intent journal and the
  final re-validation immediately before the broker call.
* **The guard chain** — the production chain (`default_chain`) is used, with
  the real risk context (exposure, daily loss, drawdown).
* **Readiness** — the full existing contract: the four switches, the live
  order host, the credential, the bounded limits, the engine timeframe, the
  market-data mode, the canary, the drawdown cap, the auto-stop, the alert
  delivery, the data source, the session calendar, the token freshness, the
  dedicated database (advisory), the audit-trail integrity, the emergency
  stop, the working orders, the unresolved intents, the reconciliation
  liveness, the static-IP advisory, the clock sanity, the kill switch, the
  single-instance lock and the data freshness.

---

## 6. Test matrix

Legend: **PASS** = proven by a test in this repository · **PARTIAL** = proven
for the parts that are testable here, the rest is documented · **BLOCKED** =
cannot be exercised in this environment · **UNVERIFIED** = not exercised.

| # | Acceptance criterion | Command / test | Result | Evidence |
|---|---|---|---|---|
| 1 | PAPER is the default on a fresh install | `pytest tests/test_live_switch_activation.py::TestPaperIsTheDefault` | PASS | `test_a_fresh_install_has_no_live_switches`, `test_the_switch_reports_paper` |
| 2 | An ordinary restart comes up PAPER | `…::test_a_restart_never_resumes_a_live_session` + the start-up reset in `app/api/server.py` | PASS | the start-up removes the four switches |
| 3 | Clicking LIVE opens the setup and sends no order | `…::test_moving_the_switch_towards_live_places_no_order`, `tests/test_live_switch_frontend.py` | PASS | an exploding broker is installed; it is never called |
| 4 | The wizard alone never reaches the broker | `tests/test_live_switch_end_to_end.py::test_the_wizard_alone_never_touches_the_broker` | PASS | the mock exchange request log stays empty |
| 5 | OAuth success | `…::TestCredentials::test_a_successful_exchange_returns_only_a_masked_state` | PASS | only presence/state/mask are returned |
| 6 | OAuth failure / refusal | `…::test_a_refused_exchange_never_echoes_the_code_or_the_secret` | PASS | the broker's body is never echoed |
| 7 | OAuth cancel (no code) | `…::test_the_exchange_requires_the_broker_application_credentials` + the empty-code refusal | PASS | 409 with an actionable message |
| 8 | Invalid / expired token | `…::TestCredentials::test_a_rejected_credential_is_refused`, `tests/test_live_switch_readiness.py::TestTokenFreshness` | PASS | the credential state becomes `expired`, `reconnect_required` becomes true |
| 9 | A sandbox token can never prove a live account | `…::test_a_sandbox_credential_can_never_authorise_live` | PASS | explicit refusal naming the sandbox host |
| 10 | A live token never reaches the sandbox broker | `…::TestIsolation::test_the_live_token_is_never_written_to_the_sandbox_key` | PASS | the live token key is only written to `live.env` |
| 11 | Missing secret-store capability | `tests/test_live_switch_readiness.py::TestSecretHandling::test_a_failure_to_store_the_credential_blocks_the_activation` | PASS | the write failure is refused; nothing is stored elsewhere |
| 12 | The credential file is owner-only | `…::test_the_live_credential_file_is_owner_only` | PASS | mode `0600` on POSIX |
| 13 | Masked output only | `…::TestCredentials`, `…::test_the_activation_snapshot_never_contains_a_secret` | PASS | the token/secret never appear in any payload |
| 14 | Secret redaction on exceptions and in the log | `…::test_a_refused_exchange_never_echoes_the_code_or_the_secret`, `…::test_the_token_never_reaches_the_log`, `…::test_the_audit_trail_never_contains_the_token` | PASS | the log records the exception **type** only |
| 15 | Each of the four switches missing / mismatched | `…::TestActivation::test_each_mismatched_switch_blocks_the_activation`, `test_a_missing_switch_is_written_by_the_activation_itself` | PASS | the four are parametrized |
| 16 | Each risk limit missing | `…::TestRiskLimits::test_a_missing_required_limit_is_refused` (×4) | PASS | 409, the missing limit is named |
| 17 | Each risk limit zero | `…::test_a_zero_limit_is_refused` (×4 × 3 spellings) | PASS | "greater than zero" |
| 18 | Each risk limit negative | `…::test_a_negative_limit_is_refused` (×4) | PASS | "negative" |
| 19 | NaN / infinity | `…::test_nan_and_infinity_are_refused` | PASS | "finite" / "NaN" |
| 20 | Malformed limit | `…::test_a_malformed_limit_is_refused` | PASS | "not a number" |
| 21 | An invalid (non-live) order host | `…::TestMarketWiring::test_a_sandbox_order_host_is_refused_as_the_live_host`, `test_a_non_https_live_host_is_refused` | PASS | the host allowlist and HTTPS are enforced |
| 22 | Every readiness blocker blocks the activation | `tests/test_live_switch_readiness.py::TestReadinessBlocksTheActivation` (unresolved intent, corrupted audit trail, stale market data) | PASS | each refusal names the blocker |
| 23 | UNKNOWN readiness is not ready | `…::test_an_unknown_required_check_blocks_the_activation` | PASS | `BLOCKED_UNVERIFIED` refuses the arm |
| 24 | Every blocker is shown, never suppressed | `…::test_the_readiness_review_lists_every_blocker` | PASS | the named checks appear as BLOCKED/UNKNOWN |
| 25 | Stale market data | `…::test_stale_market_data_blocks_the_activation` | PASS | `data_freshness` is an ERROR for the live broker |
| 26 | Market-session incompatibility | `…::TestMarketWiring::test_a_daily_timeframe_is_refused`, `test_a_replay_data_source_is_refused_for_live` | PASS | intraday + `nse-session` + `upstox` are required |
| 27 | Static-IP compatibility | `app/live/preflight.py::static_ip_configuration` | PARTIAL | the check exists and is advisory (`cannot_check` offline); the broker-side enforcement is **UNVERIFIED** (no broker account) |
| 28 | Emergency stop blocks the activation | `…::TestActivation::test_the_emergency_stop_blocks_the_activation` | PASS | 409 |
| 29 | Emergency stop invalidates the authorization | `tests/test_live_switch_readiness.py::TestEmergencyStopAfterActivation` | PASS | the session is no longer ACTIVE |
| 30 | An invalid / stale supervisor lease | `…::TestActivation::test_a_stale_supervisor_lease_blocks_the_activation` | PASS | 409 |
| 31 | A duplicate live engine | `…::test_a_second_live_engine_cannot_take_over` | PASS | the single-instance lock refuses it |
| 32 | Audit / journal corruption | `…::test_a_corrupted_live_audit_trail_blocks_the_activation` | PASS | `audit_trail_integrity` is an ERROR |
| 33 | Open working orders and unresolved intents | `…::test_an_unresolved_submission_intent_blocks_the_activation`, `…::TestSwitchOffWithAnOpenBook::test_open_working_orders_are_reported_and_never_called_flat` | PARTIAL | the unresolved-intent check is an ERROR and blocks; the open-orders check is a **WARNING** in the existing contract (the engine reconciles them at start-up) and is shown, not hidden — the severity was deliberately **not** changed |
| 34 | ARM failure | `…::TestActivation::test_the_wrong_phrase_changes_nothing` + every refusal rolls back | PASS | the rollback returns the installation to PAPER |
| 35 | ACTIVATE failure | `…::test_a_refused_activation_rolls_back_to_paper` | PASS | the switches are removed again |
| 36 | Restart | `…::test_a_restart_never_resumes_a_live_session` | PASS | no auto-resume |
| 37 | Crash | the start-up reset + the durable emergency stop + the durable session state | PASS | a crash cannot come up live |
| 38 | Disconnect / failed reconciliation | the reconciliation-freshness check of the existing contract | PARTIAL | the check is exercised by the existing suite; a real broker disconnect is **UNVERIFIED** |
| 39 | Token expiry | `…::TestTokenFreshness::test_an_expired_token_stops_new_entries` | PASS | `reconnect_required` is set and the guard blocks |
| 40 | Process shutdown | `app/live/supervisor.py` (the child is asked to stop, never killed) | PASS | the existing supervisor lifecycle suite covers it |
| 41 | Pause / resume | the existing `app/live/session.py` transitions | PASS | the existing suite covers them; the switch uses the same manager |
| 42 | A mock order happens only after the explicit final confirmation | `tests/test_live_switch_end_to_end.py::test_the_explicit_confirmation_is_what_starts_live_trading` | PASS | the exchange log is empty before the confirmation and holds exactly one `place` after it |
| 43 | The order travels the real gateway, guard chain and hard limits | the same test | PASS | the real `LiveExecutionGateway` + `default_chain` + the real `UpstoxLiveBroker` |
| 44 | The live order lands in the live tables, never in the paper ones | the same test | PASS | `list_live_orders()` is non-empty, `list_sandbox_orders()` is empty |
| 45 | Switch-off with an open book: no false flatness, no blind duplicate | `…::TestSwitchOffWithAnOpenBook`, the end-to-end test | PASS | the report never claims flat; the second submit is refused |
| 46 | PAPER can never route to the live order host | the existing `app/brokers/sandbox.py` host refusal + `…::TestIsolation` | PASS | unchanged in this feature |
| 47 | The sandbox broker never reaches a live host | the existing host classifier | PASS | unchanged in this feature |
| 48 | LIVE never bypasses the gateway / limits / reconciliation | the existing gateway suite + the end-to-end test | PASS | unchanged in this feature |
| 49 | The UI is not ACTIVE until the backend confirms | `tests/test_live_switch_frontend.py` | PASS | the label is driven by the backend snapshot only |
| 50 | A reload/restart cannot arm or activate | `…::test_a_restart_never_resumes_a_live_session` + the start-up reset | PASS | |
| 51 | Every error is visibly actionable | the refusal messages + `LIVE_TRADING_SWITCH.md` §9 | PASS | each refusal names what to do |
| 52 | The dashboard/API show the real effective mode | `tests/test_live_switch_end_to_end.py::test_the_dashboard_and_the_api_show_the_real_effective_mode` | PASS | `/api/boot` reports LIVE only while the switches are installed |
| 53 | The general setup endpoint does not become a live back door | `…::TestPaperIsTheDefault::test_the_setup_screen_still_refuses_live_switches` | PASS | `/setup/save` still refuses |
| 54 | The frontend calls only existing, authenticated endpoints | `tests/test_live_switch_frontend.py::…::test_every_live_route_the_frontend_calls_exists` | PASS | checked against the OpenAPI document |
| 55 | JavaScript syntax | `node --check app/static/arena-live.js app/static/arena-app.js …` | PASS | all four static assets parse |
| 56 | The full repository suite | `pytest tests/ -q` | PASS | see §7 |
| 57 | Windows build / installer / frozen executable | the GitHub Actions workflows | see §10 | the run is reported in §10 |
| 58 | A real broker account, a real token, a real live order | — | **UNVERIFIED** | deliberately not attempted; forbidden by the task |
| 59 | Broker-side enforcement (static IP, exchange checks, real fills) | — | **UNVERIFIED** | requires a broker account |
| 60 | Profitability of any strategy in live trading | — | **UNVERIFIED** | simulated/backtested returns do not guarantee live profits |

---

## 7. Test totals (this environment)

```
$ /home/user/.venv/bin/python -m pytest tests/ -q
2033 passed, 39 skipped        (before the three new files were added)
```

The final numbers of the complete run are in §10. The new files contribute:

| File | Tests |
|---|---|
| `tests/test_live_switch_activation.py` | 73 |
| `tests/test_live_switch_frontend.py` | 23 |
| `tests/test_live_switch_readiness.py` | 10 |
| `tests/test_live_switch_end_to_end.py` | 3 |

**Skipped tests are never reported as passes.** The 39 skips are the
pre-existing environment-dependent ones (no display server, no network, no
Windows).

### The commands that were run

```bash
/home/user/.venv/bin/python -m pytest tests/test_live_switch_activation.py -q      # 73 passed
/home/user/.venv/bin/python -m pytest tests/test_live_switch_frontend.py -q        # 23 passed
/home/user/.venv/bin/python -m pytest tests/test_live_switch_readiness.py -q       # 10 passed
/home/user/.venv/bin/python -m pytest tests/test_live_switch_end_to_end.py -q     # 3 passed
/home/user/.venv/bin/python -m pytest tests/ -q                                  # the complete suite
node --check app/static/arena-live.js app/static/arena-app.js \
        app/static/arena-core.js app/static/arena-wizard.js                       # all OK
```

---

## 8. Known limitations (stated, not hidden)

1. **The open-working-orders readiness check is a WARNING**, not an ERROR, in
   the pre-existing contract. The switch shows it and does not hide it, but it
   does not block the activation on it. Changing that severity would be a
   change to the existing safety contract and was deliberately not done.
2. **The dedicated live database is advisory.** The live *orders* and
   *transitions* are in their own tables and the paper database is never
   reused for live orders, but the engine lease/state still live in the same
   SQLite file. The readiness check reports this honestly
   (`dedicated_live_database`, WARNING).
3. **There is no OS-backed vault** in this repository. The documented
   owner-only file fallback is used and disclosed.
4. **The static-IP check is advisory.** It can report `cannot_check` on an
   offline host; it is never reported as a verified match. The broker's own
   enforcement is the authority.
5. **The live engine is a supervised child process.** The wizard proves the
   child was spawned, the lease was taken and the session reached ACTIVE; a
   real fill requires a real broker.

---

## 9. Remaining prerequisites before anyone uses this with real money

* A broker developer application (client id, client secret, registered
  `https://` redirect URI) and a **live** access token.
* A **static IP registered with the broker** for the order traffic.
* A dedicated live database, a dedicated market session and intraday
  timeframes.
* A deliberate, human decision about the hard limits, the canary ceiling and
  the drawdown cap — Arena will not choose them.
* Read [`LIVE_TRADING_SWITCH.md`](LIVE_TRADING_SWITCH.md) end to end, and
  rehearse the drill (`python -m app live-drill`) with the emergency stop.

---

## 10. Build, commit and CI evidence

*(filled in after the commit; the exact SHA, the CI run URL and the artifact
sizes are recorded here)*
