# Live execution readiness — Upstox/NSE and Alpaca/US

This document describes the live execution boundary as it exists in the tree:
what each broker documents, what AXIOM will and will not send, the readiness
gates an operator must clear, and exactly what has **not** been verified.

> Live execution is **OFF** by default. It requires an explicit per-market mode
> change, a passing readiness report, an Arm action and an Activate action with a
> typed confirmation phrase. A restart leaves both markets disarmed. Research
> qualification never activates execution, and no test in this repository has
> ever contacted a broker order endpoint or placed an order.

Two files implement the boundary:

* `app/markets/live_execution.py` — coordinator, per-market gate, order journal,
  risk gate, kill switch, adapters (`NseLiveAdapter`, `NasdaqLiveAdapter`).
* `app/markets/broker_capabilities.py` — documented capability registry used by
  the readiness report and by refusal paths. Reference data only; it cannot
  submit anything.

`app/alpaca_broker/` remains the only module allowed to name the Alpaca order
hosts; `app/autonomy/upstox.py` remains the token-scoped Upstox client.

---

## 1. Supported instruments, orders, sessions and account operations

Read from the official documentation on **2026-11-07**. Anything not listed under
"supported" is refused with a named refusal code rather than translated.

### NSE — Upstox

| Item | Documented support | AXIOM status |
| --- | --- | --- |
| Instrument class | cash equity, identified by `instrument_key` (e.g. `NSE_EQ|INE002A01018`) | supported, resolved from the authoritative same-day catalog |
| Order types | MARKET, LIMIT, SL, SL-M | supported |
| Time in force | DAY, IOC | supported |
| Products | Intraday (`I`), Delivery (`D`), MTF | `I` and `D` supported; MTF refused |
| Session | regular session 09:15–15:30 IST, Mon–Fri | enforced by `nse_session_status`; pre-open/AMO not used |
| Account operations | profile, funds & margin, positions, order book | read-only use |
| Order operations | place, modify, cancel, details by `order_id` or `tag`, order book | used by the adapter |
| Idempotency | `tag` (client-chosen, queryable) — **not** a duplicate-rejecting key | AXIOM looks the tag up before any retry |
| Not enabled by AXIOM | F&O, MTF, cover/OCO, GTD, after-market (AMO), multi/slice orders | refused with a named code |

AXIOM's NSE live entry is a **protected multi-leg GTT v3** order: `BUY` only,
carrying both a protective stop and a target. `SELL` exits must be placed in
Upstox by the operator; the adapter refuses and audits `exit_route_unsupported`.

### NASDAQ — Alpaca

| Item | Documented support | AXIOM status |
| --- | --- | --- |
| Instrument class | US listed equity, by ticker symbol | supported |
| Order types | market, limit, stop, stop_limit, trailing_stop | market, limit, stop, stop_limit used; trailing_stop refused |
| Time in force | day, gtc, ioc, fok | day and ioc used by the adapter path |
| Extended hours | limit + day only | refused (regular session only) |
| Account operations | account, clock, positions, activities | read-only use |
| Order operations | create, list, get by client order id, cancel, replace | used by the adapter |
| Idempotency | `client_order_id` ≤ 128 chars; a duplicate submission **is rejected** by the broker | AXIOM still reconciles before retrying |
| Not enabled by AXIOM | options, crypto, notional orders, fractional quantities, bracket/OCO/OTO, FOK | refused with a named code |

Alpaca's **paper environment** is a separate documented host; it is the official
non-live environment and is never treated as evidence that live works.

### Where the two brokers genuinely differ

| | Upstox (NSE) | Alpaca (NASDAQ) |
| --- | --- | --- |
| Instrument identifier | `instrument_key` | ticker symbol |
| Duplicate rejection by broker | **no** | **yes** (`client_order_id`) |
| Intraday product | yes (`I`) | not used |
| Protective order model | one multi-leg GTT | separate protective stop order |
| Session evaluation | `nse_session_status` (IST window) | `USEquitiesCalendar` + broker clock |

`idempotency_asymmetry()` returns this table from code so a reader cannot miss it.

---

## 2. Safety controls in the live path

| Control | Behaviour |
| --- | --- |
| Default posture | every market starts `PAPER`, gate `DISABLED`, zero broker submit calls |
| Activation | mode `LIVE` → readiness all-pass → Arm → Activate with the exact phrase `ACTIVATE LIVE REAL-MONEY TRADING` and `acknowledge_risk=True`; only `ACTIVE` mints a 300 s in-memory permit |
| Limits | all eight limits are mandatory and positive: order value, daily orders, daily loss, open positions, symbol exposure, total exposure, quote age, price deviation. A partial set is refused (`422`/`LiveGateRefused`) |
| Instrument allow-list | NSE resolves the authorized catalog entry; unknown keys are refused |
| Session | submissions are refused outside the regular session |
| Quote quality | missing, stale, delayed, crossed or zero-bid/ask quotes are refused; a reference price far from the quote is refused (price deviation) |
| Research prices | adjusted/split-adjusted research prices never reach an adapter; execution prices come from the live quote only |
| Account state | unreadable or blocked accounts fail readiness, so Arm is refused |
| Ambiguous outcome | a timeout/ambiguous acknowledgement latches `UNRESOLVED_ORDER_INTENT`, disarms that market and blocks submissions until Reconcile; never retried by hand |
| Rate limit | a documented 429 removes the intent, records `LIVE_ORDER_RATE_LIMITED`, and starts a 60 s per-market cooldown with no further broker call; it is not treated as an ambiguity |
| Kill switch | per market; blocks new orders immediately, leaves working orders unless `cancel_working=true`, and a reset requires an acknowledgement and returns the gate to `DISABLED` |
| Credentials | never logged, never in API payloads, never in the journal; the readiness report prints names and a configured boolean only |

### Kill switch and open orders

`open_order_policy()` (also embedded in the readiness report) states:

* blocks new orders: **yes**, for the affected market only;
* working orders: untouched unless the operator passes `cancel_working=true`, which
  attempts a documented cancel per working order and records every failure;
* partial fills: a cancel after a partial fill leaves the filled quantity filled;
  positions come from broker-reported positions on the next cycle;
* unresolved intents: must be reconciled before the market can submit again — a
  kill-switch reset does not clear them;
* reset: requires an explicit operator acknowledgement and returns the gate to
  `DISABLED` (never `ACTIVE`).

---

## 3. Operator readiness report

`GET /api/markets/live/readiness` (authenticated) and
`python -m app live-readiness` produce the same document:

```bash
python -m app live-readiness            # text
python -m app live-readiness --json     # machine readable (caveats on stderr)
python -m app live-readiness --base-url http://127.0.0.1:8000   # running instance
```

Per market it lists: broker, mode, gate state, every failed readiness check by
name, missing requirements, **required credentials by name** with a configured
flag, account permissions/registrations, offline platform checks, numbered
acceptance steps, and the documented capabilities. Coverage is stated in the
payload:

```json
{"mock_or_fake_transport_only": true, "live_verified": false, "windows_acceptance": "NOT_RUN"}
```

The report is read-only. It never contacts a broker, and a passing report is not
a claim that live trading works.

### Required credentials (names only)

| Market | Names |
| --- | --- |
| NSE | `AUTONOMY_LIVE_ENABLED`, `UPSTOX_LIVE_ACCESS_TOKEN`, `UPSTOX_ALGO_NAME`, `AXIOM_UPSTOX_EXPECTED_PUBLIC_IP`, plus a same-day BOD catalog |
| NASDAQ | `ALPACA_BROKER_ENABLED`, `ALPACA_BROKER_API_KEY`, `ALPACA_BROKER_API_SECRET`, `ALPACA_BROKER_ENVIRONMENT` |

### Acceptance steps (per market)

1. Configure every credential above; confirm none appears in a log or the UI.
2. Run readiness; resolve every failed check by name.
3. Configure all eight limits (a partial set is refused).
4. Set mode `LIVE` for that market only, then Arm.
5. Activate with the exact phrase and the risk acknowledgement.
6. Exercise Emergency Stop; confirm the other market is untouched; reset with the
   acknowledgement and confirm the gate returns to `DISABLED`.
7. Reconcile any unresolved intent and confirm the journal shows the resolution.
8. Repeat after any restart — the permit never survives a restart.

NSE additionally requires the SEBI static-IP and algo-name registration. NASDAQ
requires exercising a full cycle against the Alpaca **paper** environment first.

---

## 4. What has and has not been verified

| Claim | Status |
| --- | --- |
| Gate, journal, risk gate, kill switch, rate-limit and ambiguity behaviour | verified by deterministic fake-adapter tests |
| Request construction for both adapters | verified against fakes and the existing adapter/client tests |
| Upstox live order in production | **not verified** — no credential, no live call, no order |
| Alpaca paper or live order | **not verified** — no credential, no live call, no order |
| Windows packaging / acceptance of the readiness CLI | **not run** — Linux only in this environment |
| Research qualification authorizing execution | impossible by design; qualification grants no authority |

Nicknames matter here: "live-capable", "gated", "readiness passing" and "tests
green" are all weaker than "verified against the broker". Only an operator-run
session with real credentials and an accepted order establishes the last one, and
nothing in this repository has done that.
