# Scan Capacity & Quotas — quota-aware 100-symbol read-only scan

Operator and auditor reference for the **Scan Capacity & Quotas** panel inside
workspace **12 · Ensemble & Allocation** (`#page-ensemble`) and the
`/api/ensemble/scan/*` routes that back it.

Everything in this document is about **reading market data**. No part of this
workflow places, modifies, cancels or closes an order, arms a gate, activates
live trading, or starts a strategy. Scanning a symbol does not mean it will be
traded.

---

## 1. What the panel is for

A watchlist can hold far more symbols than the portfolio can hold positions. The
panel answers one question before any provider request is made:

> With the provider quota windows this process actually enforces, how many of the
> selected symbols can one read-only cycle cover at the cadence I asked for, and
> what would that cycle cost?

* **Up to 100 selected symbols per cycle.** `100` is the scan ceiling, not a
  position count, not an order count, and not a lot-size change.
* **Two calls per symbol** (one completed-bar series, one quote), charged to the
  provider's documented endpoint categories.
* **Order path untouched.** Position (`8` allocator / `10` engine), daily-order
  (`40`), symbol exposure (`25 %`) and total exposure (`80 %`) limits are
  displayed **info-only** and are not read, written or relaxed by this panel.

## 2. Where it sits

`12 · Ensemble & Allocation` → **Scan Capacity & Quotas**, above the ensemble
result panels.

* Persistent badge: `READ-ONLY MARKET DATA · NO ORDER SUBMISSION`.
* Fixed line: *"Scanning a symbol does not mean it will be traded."*
* One card per market: **NSE · Upstox** and **NASDAQ · Alpaca**.

Each card shows the server's data only — no number in the UI is hard-coded:

| Field | Meaning |
|---|---|
| Selected symbols | Symbols this cycle would analyze (1–100) |
| Quota profile | `upstox_standard` / `alpaca_basic` / `alpaca_algo_trader_plus` (from the running settings) |
| Plan / verification | `DOCUMENTED` (provider publishes the windows) or `UNVERIFIED` (operator-declared Alpaca plan) |
| Market-data credential | `PRESENT` / `NOT CONFIGURED` — presence only, the value is never read into a response |
| Requested cadence | What the operator asked for, in seconds |
| Computed sustainable cadence | Slowest documented window's floor for this symbol count |
| Max symbols at requested cadence | How many symbols the requested cadence can carry |
| Requests per cycle | Exact per-category cost of one cycle |
| Expected dispatch pacing | Seconds the gate needs to release one cycle at the documented short-window rate |
| Max open positions / exposure / orders per day | Current limits, unchanged, info-only |

Status labels are always **text plus colour** (never colour-only):

`READY` · `SLOWER CADENCE REQUIRED` · `QUOTA DEFERRED` · `NO CREDENTIAL` ·
`PLAN UNVERIFIED` · `PROVIDER ERROR` · `SCAN IN PROGRESS`

Every rolling window is printed readably, for example:

```
Upstox GET /v2/market-quote/quotes
  0 / 50 requests this 1 second       limit 50 · paced by the gate
  0 / 500 requests this 1 minute      limit 500
  0 / 2,000 requests this 30 minute   limit 2,000
```

## 3. Controls

1. **Selected symbols per cycle** — 1…100. Exceeding 100 is refused server-side.
2. **Watchlist source** — the saved paper watchlist, or an explicit comma-separated
   list (also capped at 100).
3. **Requested cadence (seconds)** and the **server-computed minimum** (read-only;
   the minimum is computed from the documented windows, not from CPU timings).
4. **Preview capacity (offline)** — asks the server to re-plan with the current
   selection. It sends **no** provider request and consumes **no** quota.
5. **Start read-only scan** — enabled only when the server's plan says the cycle
   fits every documented window, a credential is present, and no scan for that
   market is already running. Starting runs **one** cycle through the existing
   ensemble evaluation with `execute_in_paper=False` and
   `persist_decisions=False`.

Before starting, the panel lists the *exact* expected dispatch: symbols per cycle,
requests per cycle per category, computed sustainable cadence, requested cadence,
`Orders on this path: 0`, and the current/limit pair for every window.

There is deliberately **no** "trade all", "open 100 positions", "activate", "arm"
or "start strategy" control anywhere in this panel.

## 4. Current sustainable cadence (from the configured profiles)

Derived from the documented windows only — `window_seconds × requests ÷ limit` for
every window of every category — and confirmed by the offline benchmark in
`docs/ENSEMBLE_PROFITABILITY_EVIDENCE_AND_BENCHMARKS.md` §3.1:

| Market / profile | 100 symbols need | 60 s cadence carries | 15 s cadence carries |
|---|---|---|---|
| NSE · Upstox standard APIs | **90 s** | 66 symbols | 16 symbols |
| NASDAQ · Alpaca Basic (`alpaca_basic`) | **60 s** | 100 symbols | 25 symbols |
| NASDAQ · Alpaca Algo Trader Plus (only if the operator declares it) | 1.2 s | 5,000 | 1,250 |

**100 symbols every 15 s is not supported on the Upstox standard profile.** The
panel reports `SLOWER CADENCE REQUIRED` and names the lower supported cadence
(`90 s`) or symbol count (`16` at 15 s). It never describes 15 s as supported.

## 5. API (authenticated, read-only)

All routes require the bearer token (`authenticate`) and return no token, key,
secret or header value.

| Route | Method | Effect |
|---|---|---|
| `/api/ensemble/scan/capacity` | `GET` | Per-market quota profile, documented windows, live rolling usage, gate counters, plan for the default selection, current limits, recent cycles |
| `/api/ensemble/scan/preview` | `POST` | Offline re-plan for `{market, symbol_count, cadence_s, symbols?}`; no quota consumed |
| `/api/ensemble/scan/start` | `POST` | Starts **one** read-only cycle, or returns `{started: false, status, detail}` and sends nothing |
| `/api/ensemble/scan/history` | `GET` | Recent cycles for a market (or all), newest first |

`capacity` and `preview` are pure reads; `start` is the only route that can cause
market-data traffic, and it refuses unless the server-side plan allows it.

## 6. What a completed cycle reports

* `symbols_selected`, `symbols_analyzed`, `symbols_stale`, `symbols_failed`,
  `symbols_deferred` (deferred means **never sent**: `QUOTA_DEFERRED`).
* `candidates_risk_approved` — decisions the shared allocator would allow. These
  are **not** orders.
* `orders` — always `0` on this path, and `broker_order_api_calls` always `0`.
* `calls_by_category` — exactly what the gate charged, per documented category.
* `elapsed_ms` and `cadence_met` (elapsed ≤ requested cadence).

The result block and the history table keep the three quantities visually and
textually separate: **ANALYZED** vs **RISK-APPROVED CANDIDATES** vs **ORDERS (0)**.

## 7. Failure and edge states

| State | Behaviour |
|---|---|
| Loading | Cards render `LOADING`; no provider call is made |
| Empty | "No read-only scans have run in this process yet." |
| `NO CREDENTIAL` | Start disabled; the reason names the missing read-only credential |
| `PLAN UNVERIFIED` | Alpaca plan is the operator's declaration (default Basic); capacity is shown, entitlement is not verified |
| `QUOTA DEFERRED` | A rolling window would overflow; Start disabled and the estimated drain time is shown — the gate defers rather than breaches |
| `PROVIDER ERROR` | The cycle failed; the error text is shown, `orders` stays `0` |
| `SCAN IN PROGRESS` | A second start for the same market is refused until the first finishes |

Keyboard and focus behaviour is unchanged: the controls are ordinary labelled
form fields and buttons, status regions use `aria-live="polite"`, and the layout
collapses to one column on narrow desktop/tablet widths.

## 8. What this does not claim

* **No perfect timing and no fills.** `expected_dispatch_seconds` is the pacing the
  documented windows impose, not a latency promise. `MODELED OFFLINE` labels the
  modelled figures.
* **No live readiness.** Live execution is still off; nothing here arms or
  activates it.
* **No 100 simultaneous positions.** 100 analyzed symbols produce at most what the
  existing allocator and paper risk gate allow (`8` positions).
* **No inferred entitlements.** A paid Alpaca plan is never assumed; the Basic
  profile is used unless the operator declares another plan, and the card says
  `UNVERIFIED`.
* **No extrapolation** from the published ≤ 16-instrument benchmark.

## 9. Tests

* `tests/test_scan_capacity_quotas.py` — cadence math per documented window,
  category separation, per-second pacing vs rolling-window deferral, symbol cap,
  credential/plan states, service start/refusal paths, zero orders, API auth and
  no-secret-leak assertions.
* `tests/js/scan_capacity_smoke.mjs` — headless DOM: panel chrome, readable
  windows, text statuses, disabled Start when blocked, offline preview, read-only
  start, ANALYZED vs candidates vs ORDERS separation, and zero order endpoints.
