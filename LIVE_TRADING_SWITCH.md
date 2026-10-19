# The PAPER / LIVE switch — operator runbook

> **The switch does not make money safe.** It makes the *decision* explicit,
> auditable and reversible. Every real order still passes the execution
> gateway, the hard risk limits, the strategy lifecycle and the guard chain.
> Simulated or backtested returns do not guarantee live profits.

**Scope:** the operator-facing workflow that lives in the Arena workstation
(top bar switch → four-step activation wizard → supervised live engine) and
the safe way back to paper.

**Companion documents:**
[`LIVE_TRADING_ENABLEMENT.md`](LIVE_TRADING_ENABLEMENT.md) (how the live mode
is built and what the four switches mean) and
[`LIVE_OPERATIONS.md`](LIVE_OPERATIONS.md) (running the live engine, alerts,
canary, emergency stop).

---

## 1. Where the switch is

* **Top bar** (every view): a `PAPER | LIVE` segmented switch with the current
  effective mode next to it.
* **Trading view**: the same switch as a card, with the live session state,
  the last reconciliation, the token freshness and the emergency stop.

The switch is a **control, not a command**: moving it never places an order.

| Switch position | What it means |
|---|---|
| `PAPER` | Live trading is not enabled. Orders can only be simulated. |
| `LIVE · CONFIGURED` | The live configuration is installed, but no live engine is authorized. Nothing can place a real order until you complete the activation. |
| `LIVE · ACTIVE` | A supervised live engine is running with real money. Orders are real. |

`LIVE · ACTIVE` is shown **only** after the backend and the supervisor have
confirmed it. While the wizard is open, or after a refusal, the switch stays
`PAPER` and the session state line tells you what actually happened.

---

## 2. The activation workflow (one integrated flow)

1. **Move the switch to LIVE.** A dialog opens. Nothing else happens: no
   order, no engine, no configuration change.
2. **Step 1 — connect your broker account.**
   * Arena builds the broker's **official OAuth login URL** and opens it.
   * You approve access in the broker's own page; the broker redirects to
     your registered URI with an authorization code.
   * You paste the code (or the whole redirected URL) into Arena.
   * The backend exchanges the code for an access token, **verifies the
     credential against the live host** and stores it.
   * A credential that authenticates on the **sandbox** host only is refused
     with an explicit reason. A sandbox token can never prove a live account.
   * **Arena never asks for your broker website password, PIN, TOTP seed or a
     one-time code, and never stores them.**
3. **Step 2 — state your hard risk limits.** Every limit must be a finite,
   positive number. `0` means "unlimited" and is refused for live trading.
   Arena never invents a default for you.
4. **Step 3 — readiness review.** Every check of the existing readiness
   contract is listed with its verdict. Required checks that are BLOCKED or
   UNKNOWN stop the activation; nothing is suppressed or auto-overridden.
   "Recheck readiness" re-runs the contract against the live state.
5. **Step 4 — review and confirm.** A summary shows the broker/account
   identity (masked), the mode, the data source and instrument(s), every hard
   limit, the market session, the current positions and working orders, the
   reconciliation state, the token freshness and the emergency stop. You then
   type the **exact configured confirmation phrase**. Only then does Arena
   write the live switches, start the supervised child engine, ARM and
   ACTIVATE.

If any step is refused, the installation stays in PAPER, the reason is shown
in full, and nothing is left half-configured.

### The confirmation phrase

The phrase is configured in the live configuration
(`LIVE_CONFIRM=I-ACCEPT-REAL-MONEY-RISK`). It is **never hard-coded in the
interface**: the wizard reads the required phrase from the backend, and the
backend compares it exactly (no case folding, no whitespace forgiveness).
A wrong phrase changes nothing at all.

---

## 3. Reading the readiness review

| Verdict | Meaning | Effect |
|---|---|---|
| `READY` | Every check passed. | The start button is enabled. |
| `READY_WITH_WARNINGS` | Only known, accepted gaps (for example no alert webhook configured). | The start button is enabled; the warnings are listed. |
| `READY_WITH_UNVERIFIED` | An optional check could not run. | The start button is enabled, but the unverified items are listed and never read as "clean". |
| `BLOCKED` | A required check failed. | The start button is refused; the blockers are named. |
| `BLOCKED_UNVERIFIED` | A **required** check could not run. | The start button is refused. Unknown is never ready. |

The checks that matter most before real money moves:

| Check | What it proves |
|---|---|
| `four_switch_opt_in` | The four live switches are installed and consistent. |
| `order_host_is_live` | The order host is a documented **live** host over HTTPS, and not a sandbox host. |
| `credential_present` | A live access token is stored. |
| `hard_risk_limits_bounded` | All four hard limits are non-zero. |
| `engine_timeframe_intraday` | The timeframe can actually drive a live engine. |
| `market_data_mode_matches_timeframe` | The data mode can carry that timeframe. |
| `audit_trail_integrity` | The live audit trail adds up (no orphan fills, no lost fills, no quantity mismatch). |
| `emergency_stop_clear` / `kill_switch_available` | The persisted emergency stop is clear and the flag path works. |
| `no_open_orders_left` | Whether working orders are left over from the last run (the engine reconciles them at start-up). |
| `no_unresolved_intents` | No submission intent has an unknown remote outcome. |
| `reconciliation_live` | The last broker reconciliation is recent. |
| `single_instance_lock` | No other live engine holds the lease. |
| `clock_sanity` | The machine clock is plausible, so expiry maths can be trusted. |
| `data_freshness` | The market data is recent enough to act on. |
| `token_lifecycle` | The age of the access token (advisory; the broker is authoritative). |
| `static_ip_configuration` | Advisory: order traffic must come from the static IP registered with the broker. |

---

## 4. Going back to PAPER (the switch-off flow)

Moving the switch to PAPER opens a **drain dialog**, never a silent toggle:

* it shows the **open positions, the working orders and the reconciliation
  status** (and says "unavailable" when it cannot read them — it never shows
  a false zero);
* it **stops new entries first**, then asks the supervised engine to drain;
* it disarms the session and **invalidates the live authorization epoch**, so
  a later order cannot be authorized by the old confirmation;
* it removes the four live switches from the live configuration, so the next
  start is PAPER again.

**The switch-off never claims that the account is flat.** It reports what it
knows and what it does not; only broker fills can make the account flat.
Risk-reducing exits keep working while the engine drains.

---

## 5. Emergency stop

The **GLOBAL EMERGENCY STOP** button (Trading view → emergency controls, and
the wizard's confirmation step) uses the existing durable emergency-stop
flow:

* it persists the stop flag, so it survives a restart;
* it invalidates the live authorization, so no new entry can be authorized;
* it asks the supervised engine to cancel working orders through the live
  gateway;
* it reports what is and is not reconciled.

**Positions are never auto-closed.** Clearing the stop requires the exact
configured confirmation phrase, and after clearing it the session is still
STOPPED: you must ARM and ACTIVATE again, which re-runs the whole readiness
contract.

---

## 6. Credential handling

| Question | Answer |
|---|---|
| Where is the token stored? | In `<ARENA_HOME>/live.env`, written atomically with owner-only permissions (0600 on POSIX, owner-only ACL on Windows). |
| Is there a vault? | **No.** This repository has no OS-backed keyring. The documented, existing mechanism for this codebase is the per-user configuration file. Arena tells you this explicitly instead of silently storing a secret somewhere unknown. |
| What does the interface show? | Presence, verification state, age and a masked length hint. **Never the value.** |
| Is the token ever returned, logged, audited, embedded in a URL or in a crash report? | No. It is never placed in the frontend state, the audit trail, the system events or the test output. |
| How do I remove it? | "Forget stored credential" in step 1 of the wizard, or delete the token lines from `live.env`. |
| What about the client secret? | It is sent once to the local backend, stored in the same owner-only file, and never echoed. |
| What if the file cannot be written? | The activation is refused. There is no second, silent place where the secret could end up. |

**Daily tokens:** Upstox access tokens are daily. When the stored credential
ages out, the interface shows `expired`, marks *reconnect required*, and the
token guard blocks new entries until you reconnect.

---

## 7. The live configuration file

The live configuration lives in its own file, `<ARENA_HOME>/live.env` — **not**
in the installation's main `.env`. The main file describes the PAPER
installation (its database, its saved paper account, its timeframe); the live
engine needs a different broker, a different data source and a different
timeframe, and writing those into the main file would make the saved paper
account incompatible with the next start-up.

The four live switches — `MODE=LIVE`, `LIVE_TRADING=true`,
`BROKER=UPSTOX_LIVE`, `LIVE_CONFIRM=<phrase>` — are written **only** by the
final activation and removed again by the switch-off flow. Nothing else in
the process writes them: no restart, no recovery path, no background job.

A start-up always removes the switches: a restart can never come up live.

---

## 8. External requirements (not optional)

* A **broker developer application**: client id, client secret and a
  registered `https://` redirect URI.
* A **static IP registered with the broker** — order traffic must originate
  from it (SEBI/NSE requirement in force since 2025-10-01).
* A **market session**: Indian equity orders are placed in the `nse-session`
  calendar; intraday timeframes only.
* **One live engine per installation.** A second one is refused by the
  single-instance lock.
* A **dedicated live database** is recommended (`DATABASE_URL` containing
  `live`); the live orders and transitions are always in their own tables,
  and the paper database is never reused for live orders.

---

## 9. Troubleshooting — every refusal is actionable

| Message | What to do |
|---|---|
| "That credential authenticates on the SANDBOX host only." | Generate a **live** access token in the broker's official login page and reconnect. |
| "The stored credential is expired / changed / unverified." | Reconnect the account in step 1. New entries are blocked until you do. |
| "The live configuration is incomplete: …" | Fill in the named limit or field in step 2. |
| "Readiness contract refuses arming: …" | The named check is listed in the readiness review; fix it and press "Recheck readiness". |
| "The emergency stop is active." | Clear it deliberately in the emergency controls (the exact phrase is required), then arm again. |
| "another live engine (owner) holds the lock" | The previous engine is still running or its lease is fresh. Stop it deliberately; a stale lease does not block. |
| "the account is not flat" / open positions shown | Reconcile with the broker first. Arena will not close a position for you. |
| "the live configuration file could not be read/written" | Fix the file permissions in your Arena home; the activation is refused until then. |

---

## 10. What this workflow does **not** do

* It does not make a losing strategy profitable.
* It does not manage your risk for you: the limits are yours to state.
* It does not close positions automatically, and it does not promise that an
  order was filled until the broker says so.
* It does not bypass the broker's own risk checks, the exchange limits or the
  regulations.
