# V1 (Arena) Verification Report

Date: 2026-09-25 · Version: 1.0.0 · Base: V0.6.4 (intact, preserved)
Environment: CPython 3.13.14, Linux sandbox, project venv (requirements installed via pip)

## Summary

V1 adds the Upstox **sandbox** execution path, a 24/7 **engine**, a validated
**learning** layer, and a **dashboard** on top of the untouched V0.6.4
platform. LIVE trading is not present in any code path: `BROKER=LIVE` raises
`ValueError` at Settings construction.

| Area | Result |
|---|---|
| Test suite | **170/170 passed** (118 preserved from V0.6.4 + 52 new V1 tests) |
| `python -m app paper-test` | **exit 0, ok:true, 17/17 checks** (pinned economics unchanged) |
| `pip check` | clean |
| Secret scan | clean (only pre-existing test-literal tokens in tests/) |
| Real sandbox orders | **NOT VERIFIED** — no sandbox credentials available |
| Real market data feed | unchanged from V0.6.1–V0.6.3 evidence (still **NOT VERIFIED** live, no credentials) |

## Exact commands executed

```
.venv/bin/python -m pytest -q                 # 170 passed, 1 warning (pre-existing Starlette deprecation)
.venv/bin/python -m app paper-test            # exit 0; ok:true; 17/17 checks
.venv/bin/pip check                           # No broken requirements found.
env -u UPSTOX_SANDBOX_ACCESS_TOKEN .venv/bin/python -m app sandbox-test   # exit 3, nothing attempted
.venv/bin/python -m app engine                # exit 2 (refuses without explicit sandbox config)
BROKER=UPSTOX_SANDBOX UPSTOX_SANDBOX_ACCESS_TOKEN=sbx DATA_SOURCE=generic \
  .venv/bin/python -m app engine              # exit 2 (refuses non-upstox data source)
.venv/bin/python -m py_compile app/brokers/sandbox.py app/engine/sandbox_engine.py
```

## VERIFIED (executed in this environment)

1. **V0.6.4 preservation** — full prior suite passes; paper-test pinned
   economics identical (realized −362.41536614602916, equity
   99637.58463385397); restart/replay dedup checks still pass.
2. **UpstoxSandboxBroker (mocked, deterministic)** — 16 tests:
   documented place body (product/validity/tag/instrument_token),
   order-id + intent persistence, duplicate-intent never re-POSTs,
   reconcile idempotency, REJECTED persistence on HTTP 400, restart-safe
   restoration (never resubmits), cancel flows (incl. documented
   UDAPI100040 handled harmlessly), modify on confirmed
   `PUT /v3/order/modify` with documented body only, fail-closed on
   401/403 (broker disables itself, later calls raise), token never in
   errors/logs, persisted emergency stop cancels open orders and halts,
   duplicate signal after restart ignored.
3. **Engine** — market-closed and stale-feed gating, hard risk gate
   (max_trades_per_day blocks and records RISK_REJECT), full
   buy→reconcile→sell→reconcile loop with journaled signals and positive
   realized P&L in the sandbox ledger, processed-signal persistence,
   duplicate-candle suppression, evaluation writes versioned model rows,
   promotion cannot change risk limits or broker settings.
4. **Learning layer** — deterministic observable-data regime labels,
   journal writes (signals + trade outcomes), rolling-batch metrics,
   chronological train/OOS expectancy split, versioned promotion only on
   strictly better OOS expectancy, `Settings` untouched by learning.
5. **Dashboard** — auth enforced (401 without token), PAPER badge default,
   "LIVE: NOT ENABLED IN V1" present in page and data, SANDBOX display when
   configured, EMERGENCY STOP persists flag + event, RESUME clears.
6. **Live boundary** — `BROKER=LIVE` (any case) rejected; sandbox broker
   requests constrained to documented sandbox hosts
   (api-hft.upstox.com + api.upstox.com/v2/order/history) — enforced by a
   recording-transport test; no live order endpoint ever contacted.
7. **Partial-fill accounting** — delta-based reconciliation: incremental
   partials (3→7→10) credit each delta exactly once; partial-then-cancelled
   keeps the executed quantity; transient history failure (503) never
   finalizes orders and recovery applies the fill exactly once.
8. **CLI safety** — engine refuses LOCAL_PAPER and non-upstox data source
   (exit 2); sandbox-test without credentials exits 3 with
   "nothing attempted, nothing claimed".

## NOT VERIFIED (honestly unavailable — documented, not fabricated)

1. **Real Upstox SANDBOX order placement/reconciliation** — no sandbox
   credentials in this environment. The read-only check
   (`python -m app sandbox-test` / `verification_v061_live/real_sandbox_order_check.py`)
   exits 3 without claiming anything. When credentials exist: run the
   driver; expected probe path is HTTP 400 UDAPI100010 (order-not-found ⇒
   credential accepted); `--place-order` then submits ONE small MARKET
   sandbox order and reconciles it from documented order history.
2. **Real market data feed behavior in the engine** — depends on live
   credentials (same as V0.6.1–V0.6.3 status; feed adapter itself is the
   preserved, previously verified one).
3. **Static-IP restriction (UDAPI1154) behavior** — cannot be observed
   without a real sandbox deployment on a non-whitelisted IP.
4. **Termux/Android and Python 3.14** — not available in this environment
   (CPython 3.13.14 used; project targets 3.11+).
5. **Long-running 24/7 soak** — engine loop verified with bounded runs in
   tests; multi-day soak requires live credentials and time.

## Upstox documentation verification (fetched during implementation)

- Place Order V3: POST https://api-hft.upstox.com/v3/order/place — body and
  `data.order_ids[]` response as implemented.
- Cancel Order V3: DELETE https://api-hft.upstox.com/v3/order/cancel?order_id=
  — UDAPI100040 (finalized) treated as already-closed; UDAPI100010 not found;
  UDAPI1154 static-IP documented.
- **Modify Order V3: PUT https://api-hft.upstox.com/v3/order/modify —
  CONFIRMED from official docs this session** (previously inferred).
  Documented body: quantity, validity (required), price (required),
  order_id (required), order_type (required), disclosed_quantity,
  trigger_price, market_protection. Implemented body matches exactly —
  earlier inferred `instrument_token`/`transaction_type` fields REMOVED.
- Get Order History: GET https://api.upstox.com/v2/order/history?order_id= —
  ordered transitions with filled_quantity/average_price; one-day retention.
- Sandbox scope: order APIs only; Market Data Feed V3 is NOT sandbox-enabled
  (engine consumes the real feed, orders flow to sandbox endpoints).

## Deliverables

- Project: `/home/user/trading_bot` (V1 additive; V0.6.4 files intact)
- Package: `trading_bot_v1.0.0.zip` + sha256 (see workspace)
- New modules: `app/brokers/sandbox.py`, `app/engine/sandbox_engine.py`,
  `app/learning/{regime,journal,evaluator}.py`, `app/risk/limits.py`,
  `app/api/routes/dashboard.py`
- New tests: `tests/test_v1_{sandbox,engine,learning,dashboard,cli_safety,separation}.py`
- External driver: `verification_v061_live/real_sandbox_order_check.py`
- Config: `.env.example` (BROKER, UPSTOX_SANDBOX_*, SANDBOX_PRODUCT/TAG,
  MAX_POSITION_VALUE/MAX_OPEN_POSITIONS/MAX_DAILY_LOSS/MAX_TRADES_PER_DAY)
