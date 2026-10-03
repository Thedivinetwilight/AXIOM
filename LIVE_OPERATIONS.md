# Live Operations (V2.1 "L-ops") — Running Real Money Safely

Companion to [LIVE_TRADING_ENABLEMENT.md](LIVE_TRADING_ENABLEMENT.md). That document is
about *turning live trading on*; this one is about *staying alive once it is on*.

**Scope:** every rail here gates **new entries only**. Exits, protective stops and
risk-reducing actions are never blocked by an operational guard — a guard that traps the
account in a losing position is worse than the risk it prevents. This is asserted by tests.

---

## 1. The rails at a glance

| Rail | Question it answers | Default | Behaviour |
|---|---|---|---|
| Token freshness (`TokenGuard`) | "Is the credential still good?" | 20 h, warn 120 min | Blocks new entries when unverified/stale; alerts before expiry |
| Canary ramp (`CanaryGuard`) | "Have we earned full size yet?" | off | Caps the first N entries to a notional ceiling (shrink or refuse) |
| Watchdog (`LiveWatchdog`) | "Is the engine actually working?" | 5 min stuck (→ 15 min CRITICAL) / 10 min silence | Alerts: stuck order (WARNING → CRITICAL), disabled broker, unresolved intent, reconcile stall, expiring token |
| Breach monitor (`BreachMonitor`) | "Is the account past its pain limit?" | daily loss; drawdown off | Daily-loss and peak-equity drawdown breach → auto EMERGENCY STOP |
| Alerts (`AlertRouter`) | "How would I even find out?" | durable sink on; webhook off | `system_events` (kind `ALERT`) + optional webhook, deduped, secrets scrubbed |
| Delivery retries (`ALERT_RETRY_*`) | "What if the page bounced?" | 3 attempts, 60 s backoff | A failed webhook POST is queued and retried on the ops tick; after the cap it is recorded as `ALERT_DELIVERY_ABANDONED` (the durable sink still has it) |
| Acknowledgements (`AlertAcknowledgements`) | "Did a human actually see it?" | 15 min, then it is an incident | An unattended CRITICAL escalates to `UNACKNOWLEDGED_ALERT`; `live-ack` (or `POST /api/v2/alerts/ack`) clears it |
| Single-engine lock (`EngineLock`) | "Is anything else trading this account?" | 180 s heartbeat | A LIVE engine refuses to start while another live engine is heartbeating (`ENGINE_REFUSED_DUPLICATE`); a stale heartbeat never blocks a restart; `--force-live-lock` overrides deliberately |

Two properties worth stating explicitly:

- **Fail-closed.** An operational rail that cannot evaluate its own state refuses entries
  rather than assuming everything is fine (e.g. an unverified credential blocks entries).
- **Never raises into the loop.** A broken rail logs and degrades; it can never take down
  order handling or reconciliation.

---

## 1b. Command cheat-sheet

| Command | What it does | Can it trade? |
|---|---|---|
| `python -m app live-preflight` | whole-system readiness: config **and** the state a fresh engine inherits | no (one read-only probe) |
| `python -m app live-drill` | offline fire drill: proves each rail fires, with evidence | no |
| `python -m app engine` | runs the engine (refuses a second LIVE engine) | yes, by design |
| `python -m app live-status [--audit]` | snapshot from recorded state; `--audit` replays both order trails | no |
| `python -m app live-digest [--hours 24] [--markdown] [--post]` | one-page roll-up of alerts/rails/orders/PnL | no |
| `python tools/determinism_check.py` | proves a replayed backtest reproduces (fresh processes, different hash seeds) | no |
| `python -m app live-ack --id ID \| --kind K \| --all` | acknowledges alerts (local bookkeeping) | no |
| `python -m app live-halt --reason "..."` | persisted EMERGENCY STOP; works even on a broken config | no (only ever halts) |
| `python -m app live-resume` | clears the stop; refuses unless the live opt-in is armed | no (only clears a stop) |

---

## 2. Recommended first live week

```ini
# .env (in addition to the four-switch live opt-in)
LIVE_CANARY_MAX_NOTIONAL=5000     # ₹5,000 per entry for the first …
LIVE_CANARY_ORDERS=10             # … 10 entries, then full size
LIVE_MAX_EQUITY_DRAWDOWN=0.05     # stop everything at -5% from peak equity
LIVE_AUTO_STOP_ON_BREACH=true
LIVE_TOKEN_MAX_AGE_HOURS=20
LIVE_STUCK_ORDER_MINUTES=5
LIVE_STUCK_ORDER_ESCALATE_MINUTES=15
LIVE_RECONCILE_SILENCE_MINUTES=10
MAX_POSITION_VALUE=10000
MAX_OPEN_POSITIONS=1
MAX_DAILY_LOSS=2000
MAX_TRADES_PER_DAY=5
ALERT_WEBHOOK_URL=https://your-ops-endpoint/hook   # optional but recommended
```

```bash
python -m app live-preflight     # verifies the credential AND records freshness
python -m app engine             # prints the active rails, then trades
# refuses to start if another LIVE engine is heartbeating on the same database
```

The canary ends by itself after `LIVE_CANARY_ORDERS` entries — no operator action is needed
to "go full size", and a restart cannot restart the ramp (the counter is persisted).

---

## 3. What you will see

- **Startup:** the engine prints the live-ops block (canary, token decision, kill switches,
  alert config) before it consumes a single candle.
- **Blocks and reductions:** `ENTRY_GUARD_BLOCKED` / `ENTRY_GUARD_REDUCED` in
  `system_events`, plus an alert. A blocked entry is **not** an error — it is the rail doing
  its job.
- **Auto stop:** `EMERGENCY_STOP` with reason `AUTO_STOP <KIND>: …`. The stop is persisted
  and survives restarts; resuming is explicit (`python -m app live-resume` or
  `/engine/resume`).
- **Stuck orders:** a working order past `LIVE_STUCK_ORDER_MINUTES` alerts as WARNING; past
  `LIVE_STUCK_ORDER_ESCALATE_MINUTES` it becomes CRITICAL. With
  `LIVE_AUTO_CANCEL_STUCK=true` the engine then cancels it and records
  `STUCK_ORDER_AUTO_CANCEL` (a failed cancel records `STUCK_ORDER_AUTO_CANCEL_FAILED` and
  leaves the order working — never assume a cancel succeeded).
- **Dashboard:** an "Alerts & Live Ops" panel showing execution mode, reconcile liveness,
  token state, canary progress, kill switches and the last alerts. Rendered from recorded
  state, so it is accurate even if the engine process died. Unacknowledged CRITICAL alerts
  are counted, and each alert row shows whether a human has seen it.
- **API:** `GET /api/v2/alerts` and `GET /api/v2/live/status` (bearer auth), both additive
  and read-only. Neither can place an order or change a limit.

---

## 4. Operating procedures

**Daily review (30 seconds).**
```bash
python -m app live-digest --hours 24 --markdown
```
Read the headline block: if "Entries blocked by a rail" is climbing, the rails are working —
look at why (usually the token). If "Unacknowledged CRITICAL alerts" is non-zero, someone
has not looked yet. If "Audit trail consistent" is False, stop and reconcile before trading.

**Daily (tokens expire daily).**
```bash
python -m app live-preflight     # must report credential_accepted: true, readiness.ok: true
```
This is also what unblocks the TokenGuard. Without it, the engine will (correctly) refuse
new entries and say so via `ENTRY_BLOCKED_TOKEN`. Exits still work, so an existing position
is never trapped.

`readiness` has two families of checks:

- **configuration** — the four switches, a documented live order host, a credential, all
  four hard limits non-zero, canary/drawdown/alert hygiene, the data source, the session
  calendar, `DEFAULT_TIMEFRAME` (the engine is intraday-only and refuses to start live
  with a daily default instead of silently downgrading while the stream keeps its timeframe),
  and the `MARKET_DATA_MODE` ↔ timeframe pairing (`full` carries official 1m/30m bars only;
  every other timeframe is built locally from ticks, so use `ltpc` for 5m/15m);
- **recorded state** — will a fresh engine inherit a clean world? It verifies the audit
  trail is internally consistent (no orphan fills, no FILLED order without executions, no
  quantity mismatches), that no persisted EMERGENCY STOP is set, that no working orders or
  unresolved intents are left over, and that reconciliation was recent.

A recorded-state check that cannot run (no database) is reported as `not checked`, never as
clean. An **ERROR** in either family means do not go live; a **WARNING** is an accepted gap.

**When an alert fires.**
1. `GET /api/v2/alerts` or the dashboard panel — read the message and payload, then
   **acknowledge it**: `python -m app live-ack --id <id> --by you --note "checked the
   terminal"` (or `--kind <KIND>` for a known issue). An alert that stays unacknowledged for
   `LIVE_UNACKED_CRITICAL_MINUTES` becomes `UNACKNOWLEDGED_ALERT`: the system cannot tell
   "fixed" from "nobody looked", so it keeps asking. Acknowledgement is local bookkeeping
   only — it never touches orders, limits or switches.
2. `STUCK_ORDER` → check `python -m app live-status` (or the API) and the reconciliation
   log; the order is still working and the engine will keep reconciling it. Never resubmit.
   A WARNING is early notice; a CRITICAL that survives auto-cancel needs the broker terminal.
3. `UNRESOLVED_INTENT` → the remote outcome is genuinely unknown. Reconcile manually in the
   Upstox app before doing anything else.
4. `BROKER_DISABLED` → credential rejected; the broker fails closed. Re-verify, then resume.
5. `RECONCILE_STALLED` → the engine is not making progress; treat as a serious incident.
6. `DAILY_LOSS_BREACH` / `EQUITY_DRAWDOWN_BREACH` → the engine has already stopped itself.

**Emergency.**
- `python -m app live-halt --reason "..."` — always permitted, works even if the engine is
  down or the configuration is broken (halting is the safe direction).
- Dashboard/desktop `EMERGENCY STOP` (persists, cancels working orders it knows about), or
- Ctrl-C the engine (reconciles, then exits), or
- the exchange/broker terminal itself — always the final authority.

**Fire drill (run this before the first live order, and after changing any rail).**
```bash
python -m app live-drill          # 14 steps, all offline, no orders, no network
```
Proves each rail actually fires and prints the recorded evidence: the four-switch gate
refuses a partial opt-in; each broker refuses the other mode's host; the TokenGuard blocks
an unverified credential *and* blocks it again once stale; the canary shrinks an oversized
entry, survives a restart and then releases; a stale credential blocks a new entry but never
an exit; a daily-loss breach triggers the persisted stop; a stuck order warns at 5 minutes
and is auto-cancelled when enabled; audit integrity catches injected corruption; alerts are
durable and secret-shaped keys are redacted; a failed page is retried; an unattended
CRITICAL escalates and acknowledging it clears that; a second LIVE engine is refused while
the first is heartbeating (and a stale lock does not block a restart); `live-halt` still
halts while the configuration is rejected. It writes to a throwaway database (removed when the drill is clean, kept when a
step fails so you can inspect the evidence) and it never contacts a network.

**Reading the state of the world.**
```bash
python -m app live-digest --hours 24 --markdown   # what actually happened, in one page
python -m app live-status            # mode, stops, token, canary, breaches, findings, alerts
python -m app live-status --audit    # … plus PnL recomputed from the audit trail alone
python -m app live-resume            # clear the stop (refuses unless the live opt-in is armed)
```
`live-status` reads recorded state, so it works while the engine is stopped or crashed.
`--audit` replays `live_transitions` + `live_orders` link-by-link and reports a
*pre-fee* realized PnL next to the engine ledger's own figure; a difference beyond the
tolerance is flagged as a disagreement about money and should be investigated before
trusting either number.

`live-digest` folds a window (default 24 h) into one artifact: alerts by kind/severity,
unacknowledged CRITICALs, entries blocked/reduced by the rails, stops and their reasons,
orders by status, the audit-trail PnL and the integrity verdict. Add `--post` to send it to
`ALERT_WEBHOOK_URL` (through the same retrying HTTP path the alerts use) and `--out FILE`
to keep a copy; it is read-only either way.

**Sizing sanity.** `MAX_POSITION_VALUE` is per-order notional; `MAX_OPEN_POSITIONS` counts
open positions; `MAX_DAILY_LOSS` is a realized-loss amount; `MAX_TRADES_PER_DAY` counts
entries. All four must be non-zero in live mode, by design.

---

## 5. Deliberately not automated

These are operational decisions, not code gaps:

- **Token refresh.** The daily OAuth dance needs your client id/secret and a redirect URI;
  automating it is a deployment choice (cron + `python -m app upstox-login`) and a security
  decision (where the token lives). The rails tell you loudly when you must re-run it.
- **Placing a drill order.** `live-drill` is a *tabletop* exercise: it proves the rails fire
  without sending anything to a broker. A real end-to-end order rehearsal is available
  separately (`python -m app sandbox-test --place-order`) and requires the explicit flag.
- **Two live engines.** The lock refuses the second one, but it is a *soft* lock: if you
  genuinely need to override it (a wedged process on another host), `--force-live-lock` is
  explicit and recorded in `system_events`. Never override it just to "make it start".
- **Auto-cancelling stuck orders by default.** `LIVE_AUTO_CANCEL_STUCK` exists but ships off:
  cancelling a working order is an action with market consequences, so the default is to
  page a human. Enable it only once you trust the stuck-order threshold on your own feed.
- **Paging.** The webhook is a generic POST; wire it to PagerDuty/Slack/ntfy as you prefer.
  The durable sink is always there if the webhook is not, and a failed delivery is retried
  a bounded number of times before being abandoned (and recorded as abandoned).
- **Exchange-side kill switch.** Your broker's terminal can always flatten and block; that
  authority is intentionally outside this codebase.
- **Static IP.** Register it with Upstox; order APIs reject unregistered origins. Low
  frequency (<10 orders/sec, which this engine is by construction) does not require exchange
  algo registration.

---

## 6. Verification status

- `tests/test_live_ops_v2_1.py` — alert dedup/severity/scrubbing/webhook failure, token
  guard (fresh/stale/unverified/disabled), canary (reduce/refuse/complete/restart), watchdog
  (stuck order + escalation ladder, disabled broker, reconcile stall, token, aggregate
  resilience), breach monitor, preflight checks, API surfaces, audit-trail replay (including
  partial/cancelled children and the ledger comparison), the halt/resume/status CLI
  (halt always permitted; resume refuses an unarmed or broken config), and engine integration
  (guards never block exits, a blocked entry never reaches the broker, a breach triggers the
  persisted stop, auto-cancel only when enabled and failures are recorded, and an engine
  with `live_ops=None` behaves exactly as before).
- Delivery retries are tested with a flaky httpx transport: failure → queue → backoff →
  delivery, the abandon-after-cap path, `ALERT_RETRY_ATTEMPTS=0` disabling retries, the
  queue bound, and the engine tick that drives it.
- Acknowledgement is tested end to end: idempotent ack, bounded store, age threshold,
  warnings never counted, meta-kinds never feeding back, CLI listing/acking, and the API
  (`GET /api/v2/alerts` exposes `unacknowledged_critical`; `POST /api/v2/alerts/ack` clears
  it; both authenticated).
- `live-drill` is exercised by tests too, including the failure path: a broken step is
  reported with its evidence and its database is deliberately kept.
- Full suite: see `README.md` for the current count · `tools/security_scan.py`: CLEAN.
- No test contacts a network; the webhook is exercised with an httpx mock transport.
- Real-money end-to-end execution remains **not attempted** (no live credentials here).
