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
| Watchdog (`LiveWatchdog`) | "Is the engine actually working?" | 5 min stuck / 10 min silence | CRITICAL alerts: stuck order, disabled broker, unresolved intent, reconcile stall, expiring token |
| Breach monitor (`BreachMonitor`) | "Is the account past its pain limit?" | daily loss; drawdown off | Daily-loss and peak-equity drawdown breach → auto EMERGENCY STOP |
| Alerts (`AlertRouter`) | "How would I even find out?" | durable sink on; webhook off | `system_events` (kind `ALERT`) + optional webhook, deduped, secrets scrubbed |

Two properties worth stating explicitly:

- **Fail-closed.** An operational rail that cannot evaluate its own state refuses entries
  rather than assuming everything is fine (e.g. an unverified credential blocks entries).
- **Never raises into the loop.** A broken rail logs and degrades; it can never take down
  order handling or reconciliation.

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
  and survives restarts; resuming is explicit (`/engine/resume`).
- **Dashboard:** an "Alerts & Live Ops" panel showing execution mode, reconcile liveness,
  token state, canary progress, kill switches and the last alerts. Rendered from recorded
  state, so it is accurate even if the engine process died.
- **API:** `GET /api/v2/alerts` and `GET /api/v2/live/status` (bearer auth), both additive
  and read-only. Neither can place an order or change a limit.

---

## 4. Operating procedures

**Daily (tokens expire daily).**
```bash
python -m app live-preflight     # must report credential_accepted: true
```
This is also what unblocks the TokenGuard. Without it, the engine will (correctly) refuse
new entries and say so via `ENTRY_BLOCKED_TOKEN`. Exits still work, so an existing position
is never trapped.

**When an alert fires.**
1. `GET /api/v2/alerts` or the dashboard panel — read the message and payload.
2. `STUCK_ORDER` → check `/api/v2/live/status` and the reconciliation log; the order is
   still working and the engine will keep reconciling it. Never resubmit.
3. `UNRESOLVED_INTENT` → the remote outcome is genuinely unknown. Reconcile manually in the
   Upstox app before doing anything else.
4. `BROKER_DISABLED` → credential rejected; the broker fails closed. Re-verify, then resume.
5. `RECONCILE_STALLED` → the engine is not making progress; treat as a serious incident.
6. `DAILY_LOSS_BREACH` / `EQUITY_DRAWDOWN_BREACH` → the engine has already stopped itself.

**Emergency.**
- Dashboard/desktop `EMERGENCY STOP` (persists, cancels working orders it knows about), or
- Ctrl-C the engine (reconciles, then exits), or
- the exchange/broker terminal itself — always the final authority.

**Sizing sanity.** `MAX_POSITION_VALUE` is per-order notional; `MAX_OPEN_POSITIONS` counts
open positions; `MAX_DAILY_LOSS` is a realized-loss amount; `MAX_TRADES_PER_DAY` counts
entries. All four must be non-zero in live mode, by design.

---

## 5. Deliberately not automated

These are operational decisions, not code gaps:

- **Token refresh.** The daily OAuth dance needs your client id/secret and a redirect URI;
  automating it is a deployment choice (cron + `python -m app upstox-login`) and a security
  decision (where the token lives). The rails tell you loudly when you must re-run it.
- **Paging.** The webhook is a generic POST; wire it to PagerDuty/Slack/ntfy as you prefer.
  The durable sink is always there if the webhook is not.
- **Exchange-side kill switch.** Your broker's terminal can always flatten and block; that
  authority is intentionally outside this codebase.
- **Static IP.** Register it with Upstox; order APIs reject unregistered origins. Low
  frequency (<10 orders/sec, which this engine is by construction) does not require exchange
  algo registration.

---

## 6. Verification status

- `tests/test_live_ops_v2_1.py` — 68 tests: alert dedup/severity/scrubbing/webhook failure,
  token guard (fresh/stale/unverified/disabled), canary (reduce/refuse/complete/restart),
  watchdog (stuck order, disabled broker, reconcile stall, token, aggregate resilience),
  breach monitor, preflight checks, API surfaces, and engine integration (guards never block
  exits, a blocked entry never reaches the broker, a breach triggers the persisted stop,
  and an engine with `live_ops=None` behaves exactly as before).
- Full suite: **1059 passed / 37 skipped** · `tools/security_scan.py`: CLEAN.
- No test contacts a network; the webhook is exercised with an httpx mock transport.
- Real-money end-to-end execution remains **not attempted** (no live credentials here).
