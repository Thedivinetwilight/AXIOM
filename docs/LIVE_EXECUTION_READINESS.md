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

## 3. Exit orders — what is routed, what is refused, and why

**There is no NSE exit route, and this is deliberate.** An exit is the most
dangerous half of the order path: a request that *looks* like a close but
executes while flat opens a short, and a request placed beside live protection
can sell the same shares twice. AXIOM therefore routes an exit only where it can
prove the request is close-only.

### Upstox / NSE — not routed (`exit_route_unsupported`)

`NseLiveAdapter.supports_exit = False`. `submit()` refuses every non-`BUY` side
before it touches the gate, the catalog or the broker client, and
`exit_route()` / `GET /api/markets/live/readiness` publish the same blockers
(`NSE_EXIT_BLOCKERS` in `app/markets/live_execution.py`):

| Blocker | What the documented broker/app surface actually says |
| --- | --- |
| `gtt_legs_are_the_close` | A protected GTT that has executed keeps its `STOPLOSS` and `TARGET` legs live as **SELL** orders for the same quantity. Those legs are the supported close. A separate exit order would be a second independent sell of the same holding — the legs are mutually exclusive only *inside one GTT*. |
| `cancel_protection_refused_with_open_position` | The only safe ordering is *cancel protection → place exit*. `app.autonomy.upstox.UpstoxLiveBroker.cancel_gtt` refuses to cancel while a position exists, because that would leave the position unprotected. |
| `edis_authorisation_not_verifiable` | Upstox requires **EDIS** authorisation (done once in the Upstox web/iOS/Android app) for any GTT `SELL` leg, and documents no API to read EDIS state. AXIOM cannot verify it, and an operator attestation is not verification. |
| `settlement_availability_not_exposed` | `/v2/portfolio/short-term-positions` reports net quantity and does not distinguish settled, EDIS-authorised holdings from same-day unsettled buys. The documented `/v2/portfolio/long-term-holdings` endpoint *does* expose `t1_quantity` and `cnc_used_quantity` (so settlement could be bounded), but it exposes **no EDIS field**, so quantity and authorisation still cannot be checked together. |
| `single_leg_sell_cannot_be_proven_closing` | A `SINGLE`-rule GTT with `transaction_type: SELL` has the same fields whether it closes a holding or opens a short (`product: I`). The existence of a documented SELL field is not evidence that the request can only close. |

**EDIS check (this session).** `GET /v2/portfolio/long-term-holdings` — the
only documented holdings read — returns `isin`, `quantity`, `t1_quantity`,
`cnc_used_quantity`, `collateral_quantity`, `collateral_update_quantity`,
`haircut`, `product`, prices, P&L and `instrument_token`. There is **no
authorisation-status field**, and Upstox documents EDIS authorisation as a
one-time action performed in the web/iOS/Android app, with no read endpoint to
query it. Confirmed EDIS state therefore cannot be obtained through an
officially supported read-only API, so every route that depends on it stays
disabled.

Documented SELL paths reviewed, and the verdict for each:

| Documented path | Closes or can open? | Routed? |
| --- | --- | --- |
| `STOPLOSS` leg of the entry GTT | closes (opposite side of the entry, same quantity) | placed with the entry, not on demand |
| `TARGET` leg of the entry GTT | closes (same GTT, mutually exclusive with `STOPLOSS`) | placed with the entry, not on demand |
| `SINGLE` GTT, `transaction_type: SELL`, product `D` | cannot be shown close-only; quantity is not bounded by holdings at the API, and EDIS state is unreadable | **no** |
| `MULTIPLE` GTT whose `ENTRY` rule is `SELL` | **can open** — an ENTRY rule is executable while flat | **no** |
| Order API `POST /v2/order/place` with `transaction_type: SELL`, product `I` | **can open** a short / intraday sell | **no** (this endpoint is not in the live path at all) |
| `modify` an existing protective `TARGET` leg to a marketable trigger | would close through the existing OCO structure, but the documented modify state machine and the resulting EDIS/settlement preconditions cannot be verified without a live account | **no**, recorded as the only candidate for a future route |

Operator fallback, printed by the readiness report: **close the position
manually in the Upstox app, then run the AXIOM reconcile step** so the journal
records the broker's state. Nothing in AXIOM places, modifies or cancels a real
exit order.

### Alpaca / US — routed, close-only, behind the held-long guard

`NasdaqLiveAdapter.supports_exit = True` because the shared submit path proves
the request is a close before the adapter is called:
`LiveExecutionCoordinator.submit` refuses a `SELL` without protective prices
unless the broker position for that symbol covers the quantity
(`naked_short_refused`, `broker_order_api_calls: 0`). The exit shares every entry
gate — limits, quote quality, session, journal, kill switch, ambiguity handling.

## 4. Operator readiness report

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

### Owner-run read-only preflight (per broker)

The report's `broker_preflight` array is the checklist an operator runs on their
own account. It is **documentation, not automation**: AXIOM never runs these
steps, and the readiness report itself never contacts a broker. Every step is
`read_only: true` and `writes_orders: false` except Alpaca's paper-sandbox cycle,
which is explicitly labelled `PAPER_SANDBOX` and touches no real money.

| Owner question | Upstox / NSE | Alpaca / US |
| --- | --- | --- |
| Credential configuration status | `live-readiness --json` → `credential_requirements` (presence only) | same, `ALPACA_BROKER_API_KEY` / `_SECRET` |
| Account identity | `GET /v2/user/profile` — record a **masked** id + exchanges | `GET /v2/account` — masked `account_number`, `status` |
| Permissions / blocks | `/v3/user/get-funds-and-margin`, positions readable, no block | `trading_blocked`, `account_blocked`, `buying_power` |
| Market & session eligibility | printed session window vs `nse_session_status`; BOD catalog day | `GET /v2/clock`, `/v2/calendar` |
| Platform acceptance | SEBI static IP + algo name match the configured values | clean cycle on the **paper** host first |
| Position for an exit | not applicable — no NSE exit route | `GET /v2/positions` qty ≥ intended exit |

Rules the report states and the tests pin (`tests/test_broker_preflight.py`):

```json
{"read_only_required": true,
 "forbidden": ["placing, modifying or cancelling any order",
               "contacting an order, multi-order or GTT write endpoint",
               "printing, logging or persisting a credential value, token or account number",
               "switching a market to LIVE or arming a gate",
               "treating a passing preflight as evidence that the order path works"],
 "owner_run": "these steps are for the operator on their own account; AXIOM never runs them"}
```

Record only a masked identifier. A credential value, token or full account number
must never appear in a ticket, chat, log, report or screenshot.

### Verification ladder

| Level | Meaning | Status here |
| --- | --- | --- |
| `MOCK` | deterministic fake-transport tests in this repository | **achieved** |
| `PAPER_SANDBOX` | the broker's own paper/sandbox environment, real API calls | not achieved |
| `READ_ONLY_LIVE` | live credentials, documented read endpoints only | not achieved |
| `ORDER_PATH_LIVE` | a real accepted order on the live account, operator-run | not achieved |

A level is only achieved with evidence recorded for that level. The report emits
`current_level` and `order_path_verified: false`; nothing infers a higher level
from tests, paper fills or a passing readiness report.

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

## 5. What has and has not been verified

| Claim | Status |
| --- | --- |
| Gate, journal, risk gate, kill switch, rate-limit and ambiguity behaviour | verified by deterministic fake-adapter tests |
| Request construction for both adapters | verified against fakes and the existing adapter/client tests |
| Upstox live order in production | **not verified** — no credential, no live call, no order |
| Alpaca paper or live order | **not verified** — no credential, no live call, no order |
| NSE exit order | **not implemented** — see section 3; the documented limitation is recorded, not worked around |
| Alpaca close-only exit | verified against fakes only; the held-long guard is unit-tested, no live order exists |
| Windows packaging / acceptance of the readiness CLI | **NOT RUN** — no Windows host; `uname` reports Linux `e2b.local`, and `wine`, `pwsh`/`powershell` and `makensis` are all absent, so no Windows substitute was attempted |
| Research qualification authorizing execution | impossible by design; qualification grants no authority |

The dated record of the last read-only verification attempt — which credentials
were present (none), which endpoints were contacted (none), and why — is
`docs/BROKER_VERIFICATION_EVIDENCE.md`. Windows probes are recorded in
`docs/WINDOWS_ACCEPTANCE_STATUS.md`.

Nicknames matter here: "live-capable", "gated", "readiness passing" and "tests
green" are all weaker than "verified against the broker". Only an operator-run
session with real credentials and an accepted order establishes the last one, and
nothing in this repository has done that.
