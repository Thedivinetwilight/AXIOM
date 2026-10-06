# Enabling Live Trading — Repository Analysis & Runbook

> **Historical runbook snapshot:** its repository branch, audit commit and test counts predate the current branch state. It is not current proof of live readiness; see [`AXIOM_CURRENT_AUDIT_REPORT.md`](AXIOM_CURRENT_AUDIT_REPORT.md).


**Repository:** `Thedivinetwilight/AXIOM` (branch `arena/01a0fcfb-axiom`)
**Audit commit:** `7dc3e1a` · **Audit test suite:** 949 passed / 37 skipped
**Last updated:** 2026-10-02 — **Route B is now implemented** (983 passed / 37 skipped)

---

## Implementation status (Route B — done)

The four fixes below were requested and implemented on this branch. Nothing is
active by default: the shipped defaults remain `MODE=PAPER`, `LIVE_TRADING=false`,
`BROKER=LOCAL_PAPER`.

| Item | Where | Status |
|---|---|---|
| Host classification corrected (live host is `api-hft…`, sandbox default `api-sandbox…`) | `app/config/hosts.py`, `app/brokers/sandbox.py`, `app/config/settings.py` | ✅ done |
| Sandbox broker refuses live order hosts (incl. `api.upstox.com`) | `app/brokers/sandbox.py` | ✅ done |
| Real live broker with truthful identity + live-only hosts | `app/brokers/live.py` (`UpstoxLiveBroker`) | ✅ done |
| Four-switch gate + non-zero risk-limit requirement | `app/config/settings.py` (`_validate_live_mode`) | ✅ done |
| Real orders audited in separate `live_orders`/`live_transitions` tables | `app/database/models.py`, `repository.py` | ✅ done |
| Truthful `/health`, dashboard, weekly report, engine status, desktop text | `app/api/routes/health.py`, `dashboard.py`, `app/reporting/weekly.py`, `app/engine/sandbox_engine.py`, `desktop/app.py` | ✅ done |
| Read-only live readiness command | `python -m app live-preflight` | ✅ done |
| Tests + docs | `tests/test_live_hardening_v2.py` (37 new tests), README, USER_MANUAL, `.env.example` | ✅ done |

Not done (deliberately, needs your decisions): static-IP enforcement in code,
canary/notional ramp, stuck-order paging, automation of token refresh. See §6–§7.

### How to enable live now (operator quickstart)

```ini
# .env — every line below is required; any omission aborts startup by name
MODE=LIVE
LIVE_TRADING=true
BROKER=UPSTOX_LIVE
LIVE_CONFIRM=I-ACCEPT-REAL-MONEY-RISK
UPSTOX_LIVE_ACCESS_TOKEN=<your live token>       # daily token; refresh it
MAX_POSITION_VALUE=10000        # all four limits must be NON-ZERO in live mode
MAX_OPEN_POSITIONS=1
MAX_DAILY_LOSS=2000
MAX_TRADES_PER_DAY=5
DATA_SOURCE=upstox
MARKET_DATA_ACCESS_TOKEN=<your live token>
MARKET_DATA_SYMBOLS=NSE_EQ|INE002A01018          # start with ONE instrument
MARKET_DATA_TIMEFRAME=1m
DEFAULT_TIMEFRAME=1m
MARKET_CALENDAR=nse-session
DATABASE_URL=sqlite:///trading_live.db           # dedicated DB for live
```

```bash
python -m app live-preflight    # READ-ONLY: hosts, locks, limits, credential verdict
python -m app engine            # starts only after Settings accepts the opt-in
```

To stop: press Ctrl-C (the engine reconciles then exits) or trigger EMERGENCY STOP from
the dashboard/desktop; the stop flag persists across restarts and needs an explicit resume.
To go back to paper, remove/rename those four switches — live is never the default.

---

## 0. Bottom line

The README, the user manual, the CLI docstring and the API all state that live trading is
impossible. That is **true for the `LIVE` mode name and false for the code as shipped.**

There are **three locks**, and they are all reachable:

| # | Lock | Enforced by | Status |
|---|------|------------|--------|
| 1 | `MODE` must be `PAPER`, `LIVE_TRADING` must be `false` | `app/config/settings.py:145-146` | Hard, unconditional |
| 2 | `BROKER=LIVE` is rejected | `app/config/settings.py:158-159` | Hard, unconditional |
| 3 | Only two brokers exist: `LOCAL_PAPER`, `UPSTOX_SANDBOX` | `app/config/settings.py:160-161` | Hard, unconditional |

**But the mode named `UPSTOX_SANDBOX` does not, by default, talk to Upstox's sandbox.**
Its default order host is `https://api-hft.upstox.com` — which is Upstox's **live order
host for real money**. So there are two ways to "enable live trading":

- **Route A — configuration only, no code changes:** point `BROKER=UPSTOX_SANDBOX` at a
  *live* Upstox access token. The engine then places **real orders** at
  `https://api-hft.upstox.com/v3/order/place`, while every log line, database row, `repr`
  and dashboard still calls it "SANDBOX" and `live_trading: false`. This is the fastest
  path, and the dangerous one.
- **Route B — the honest implementation:** add a real `UPSTOX_LIVE` broker mode with
  truthful labelling, a triple-lock opt-in, a corrected host allow-list, and a fixed
  sandbox default. This is the recommended path if you intend to trade real money.

Both routes are documented below, along with what has to be fixed before real money moves.

---

## 1. What the system actually is

```
market data (Upstox feed / replay / synthetic)
        │
        ▼
   MarketEvent ──► candle builder ──► strategy (RouterStrategySource)
        │
        ▼
   hard risk gate  (app/risk/limits.py, app/risk/net_edge.py, quality/liquidity/volatility gates)
        │
        ▼
   execution backend (one of)
     ├── PaperBroker          app/paper_trading/broker.py     → in-process simulation, no network
     └── UpstoxSandboxBroker  app/brokers/sandbox.py          → real HTTP POST/DELETE/PUT to an order API
        │
        ▼
   SQLite (orders, transitions, ledger, engine_state, system_events)
```

Two facts matter for this document:

1. `UpstoxSandboxBroker` is **not a simulator**. It is a genuine order-placement client:
   `POST /v3/order/place`, `DELETE /v3/order/cancel`, `PUT /v3/order/modify`, with an
   idempotent intent journal and reconciliation loop. The only thing that makes it
   "sandbox" is *which host and which token you give it*.
2. Nothing else in the repo routes orders. `PaperBroker` never leaves the machine; the
   FastAPI app (`app/api/server.py:27`) and `run.py` only ever construct `PaperBroker`.

The safety gates that *do* work regardless of route, and are worth keeping:

- `HardRiskLimits` (`app/risk/limits.py`) — `MAX_POSITION_VALUE`, `MAX_OPEN_POSITIONS`,
  `MAX_DAILY_LOSS`, `MAX_TRADES_PER_DAY`. Learning code cannot mutate these.
- Deterministic intent IDs (`app/engine/sandbox_engine.py:45`) — the same candle cannot
  submit twice, even across restarts.
- `EMERGENCY_STOP` persisted in `engine_state`, survives restarts, explicit resume required
  (`app/engine/sandbox_engine.py:284-316`).
- Fail-closed auth: a rejected credential disables the broker.
- The token is never logged, printed, or put in an exception.

---

## 2. The complete lock map

| Layer | File:line | Behaviour |
|---|---|---|
| Settings | `app/config/settings.py:71-72` | `mode="PAPER"`, `live_trading=False` defaults |
| Settings | `app/config/settings.py:145-146` | raises `"Only MODE=PAPER and LIVE_TRADING=false are permitted"` |
| Settings | `app/config/settings.py:158-159` | raises `"LIVE trading is not enabled in V1"` for `BROKER=LIVE` |
| Settings | `app/config/settings.py:160-161` | raises unless broker ∈ {`LOCAL_PAPER`, `UPSTOX_SANDBOX`} |
| Settings | `app/config/settings.py:99-102` | broker + **host defaults** (see §3) |
| Broker | `app/brokers/sandbox.py:52-63` | host allow-list / "forbidden" list (see §3) |
| Broker | `app/brokers/sandbox.py:225-243` | refuses unknown hosts at construction |
| Broker | `app/brokers/sandbox.py:262-265` | `repr` hardcodes `live=False` |
| Engine | `app/engine/sandbox_engine.py:20` | docstring: "nothing in this engine can enable live trading" |
| CLI | `app/__main__.py:30-31` | "There is no command that can place a real order." |
| CLI | `app/__main__.py:418-421` | `engine` requires `BROKER=UPSTOX_SANDBOX` |
| CLI | `app/__main__.py:443-452` | constructs the broker from `settings.upstox_sandbox_base_url` |
| API | `app/api/routes/health.py:12-13` | hardcodes `"mode": "PAPER"`, `"live_trading": false` |
| API | `app/api/routes/dashboard.py:127-131` | hardcodes PAPER + `"— PAPER/SANDBOX ONLY"` |
| API | `app/api/routes/v2.py:39` | hardcodes `"live_trading": false` |
| Reporting | `app/reporting/weekly.py:79,125` | hardcodes "not enabled in this build" |
| Desktop | `desktop/app.py:274` | UI states "LIVE trading is disabled in the core." |
| Tests | `tests/test_final_security.py:31-61` | `live_trading=True` and `BROKER=LIVE` must raise |
| Tests | `tests/test_settings_parser.py:84-131,163-165` | `BROKER=live` (any case) must raise; malformed `LIVE_TRADING` must raise |
| Tests | `tests/test_runtime_database_api.py:26-32,152,174` | env-based `LIVE_TRADING=true` must raise; API must report false |
| Tests | `tests/test_market_events_3_0_4.py:306-318` | the market-event listener may never set `LIVE_TRADING` |
| Tests | `tests/test_v1_cli_safety.py`, `test_v1_dashboard.py`, `test_v1_engine.py`, `test_v2_api_cli.py`, `test_v2_datasets_research.py` | assert the CLI/API surface paper-only |

`tests/test_v1_separation.py:60-68` is the most revealing test in the suite. It asserts
that all outbound traffic goes to `{api-hft.upstox.com, api.upstox.com}` and then asserts
`"No live order endpoint ever contacted."` — but `api-hft.upstox.com` **is** the live order
endpoint. The test enshrines the mislabelling instead of catching it.

---

## 3. ⚠️ Critical finding: the "sandbox" default is the live order host

### 3.1 What the code says

```python
# app/config/settings.py:99-102
broker: str = "LOCAL_PAPER"  # LOCAL_PAPER | UPSTOX_SANDBOX (LIVE is not enabled)
upstox_sandbox_access_token: str = field(default="", repr=False)
upstox_sandbox_base_url: str = "https://api-hft.upstox.com"          # ← LIVE order host
upstox_sandbox_order_history_url: str = "https://api.upstox.com/v2/order/history"
```

```python
# app/brokers/sandbox.py:52-63
SANDBOX_ALLOWED_HOSTS = frozenset({
    "api-sandbox.upstox.com",  # documented Upstox sandbox order host
    "api-hft.upstox.com",      # documented V3 order host (the shipped default)  ← LIVE
})
SANDBOX_FORBIDDEN_HOSTS = frozenset({
    "api.upstox.com",          # production API host; sandbox tokens 401 here
})
```

Verified empirically on this checkout:

```
Settings.upstox_sandbox_base_url default = https://api-hft.upstox.com
SANDBOX_ALLOWED_HOSTS   = ['api-hft.upstox.com', 'api-sandbox.upstox.com']
SANDBOX_FORBIDDEN_HOSTS = ['api.upstox.com']
constructed broker base = https://api-hft.upstox.com | repr: UpstoxSandboxBroker(..., live=False)
=> order POST would go to: https://api-hft.upstox.com/v3/order/place
```

### 3.2 What Upstox says

- `api-hft.upstox.com` is the **live** order host. Upstox's own announcement
  ["New Enhanced URL for Orders"](https://upstox.com/developer/api-documentation/announcements/enhanced-url/)
  states that place/modify/cancel for the **trading platform** moved from `api.upstox.com`
  to `https://api-hft.upstox.com`. Every live order example in the current V3 docs targets
  `https://api-hft.upstox.com/v3/order/place`.
- The sandbox base is `api-sandbox.upstox.com` (SDK `Configuration(sandbox=True)`;
  Upstox staff confirmation), with `sandbox.upstox.com` also appearing in current docs.
- Upstox sandbox docs: *"Sandbox access tokens are exclusively for sandbox orders and
  cannot be used for live transactions."*

So the allow-list is **inverted relative to reality**:

| Host | Real purpose | Repo's classification |
|---|---|---|
| `api-hft.upstox.com` | **LIVE** order placement (real money) | `SANDBOX_ALLOWED_HOSTS`, default, "live=False" |
| `api-sandbox.upstox.com` | genuine sandbox orders | allowed (correct) |
| `api.upstox.com` | auth + read/market-data + non-order APIs | `SANDBOX_FORBIDDEN_HOSTS`, labelled "production" and refused |
| `sandbox.upstox.com` | sandbox (current docs) | **not in the allow-list → refused** |

### 3.3 The two consequences

**Consequence A — the sandbox mode is broken by default (functional bug).**
With a genuine *sandbox* token and the shipped default base URL, order placement targets a
live host and the token will be rejected (documented behaviour; not verifiable here without
credentials). Reconciling against `api.upstox.com/v2/order/history` — a production read
endpoint — would also never show sandbox orders. To make sandbox actually work today you
must set `UPSTOX_SANDBOX_BASE_URL=https://api-sandbox.upstox.com`.

**Consequence B — real money is one token away, and everything says "sandbox" (safety bug).**
If you paste a **live** access token into `UPSTOX_SANDBOX_ACCESS_TOKEN` (the variable name
invites exactly that), then:

- orders route to `api-hft.upstox.com/v3/order/place` → **real orders, real money**;
- the broker's `mode` is `"UPSTOX_SANDBOX"`, its `repr` says `live=False`;
- audit rows are written by `record_sandbox_order` / `record_sandbox_transition`
  (`app/database/repository.py:191,204`);
- `/health` and `/dashboard` report `"mode": "PAPER"`, `"live_trading": false`;
- desktop shows "LIVE trading is disabled in the core";
- the weekly report says live trading is "not enabled in this build".

That combination — real executions recorded as sandbox and reported as paper — is the most
dangerous thing in this repository. It is also the shortest answer to "how do I enable live
trading".

---

## 4. Route A — the old config-only path (NO LONGER AVAILABLE)

> ✅ **Closed by Route B.** This section documents how V1 *could* have been aimed at real
> money with nothing but a token. Route B deliberately removed it: the sandbox broker now
> refuses `api-hft.upstox.com` at construction, so a live token pasted into
> `UPSTOX_SANDBOX_ACCESS_TOKEN` fails closed instead of trading. The text below is kept as
> the historical explanation of the defect. Use **§5** (now implemented) instead.

> ⚠️ This section describes a real-money path. Read §6 and §7 first.

**Step 1 — Credentials and account**
1. Upstox account, KYC, funds, and a **live** (non-sandbox) app on the Developer Apps page.
2. Register a **static IP** for order APIs (§7 — in force since 2025-10-01).
3. Generate a live access token. The repo ships an OAuth helper:
   `python -m app upstox-login` (needs `UPSTOX_CLIENT_ID`, `UPSTOX_CLIENT_SECRET`,
   `UPSTOX_REDIRECT_URI`). Tokens are daily — you will need to refresh them.

**Step 2 — `.env` (historical V1 form; this now fails closed)**

```ini
# --- execution: this configuration is REFUSED by the V2 sandbox broker ---
BROKER=UPSTOX_SANDBOX                       # V1: the mode name lied; see §3
UPSTOX_SANDBOX_ACCESS_TOKEN=<LIVE_TOKEN>    # ← V1 accepted a LIVE token here
UPSTOX_SANDBOX_BASE_URL=https://api-hft.upstox.com   # ← V2: refused at construction

# --- real market data ---
DATA_SOURCE=upstox
MARKET_DATA_ACCESS_TOKEN=<LIVE_TOKEN>
MARKET_DATA_SYMBOLS=NSE_EQ|INE002A01018     # start with ONE liquid instrument
MARKET_DATA_TIMEFRAME=1m
DEFAULT_TIMEFRAME=1m
MARKET_CALENDAR=nse-session

# --- hard risk limits: SET THESE. 0 means UNLIMITED/disabled. ---
MAX_POSITION_VALUE=10000        # max notional per order (₹)
MAX_OPEN_POSITIONS=1
MAX_DAILY_LOSS=2000             # stops new entries for the day
MAX_TRADES_PER_DAY=5
```

**Step 3 — verify read access only** (no orders)

```bash
python -m app sandbox-test --instrument NSE_EQ|INE002A01018
# read-only history probe. Add --place-order ONLY when you mean it:
#   python -m app sandbox-test --place-order --quantity 1
```

**Step 4 — run the live engine**

```bash
python -m app engine            # 24/7 loop: data → strategy → risk → real orders
```

**What you get and what you do not:**

| ✅ Working | ❌ Not available |
|---|---|
| Hard risk gate before every order | Any confirmation prompt or dry-run flag (none exists) |
| Intent dedup across restarts | Truthful "live" labelling anywhere |
| Emergency stop + pause gate | A live-specific reconciliation endpoint |
| Real fills reconciled to a local ledger | Static-IP / `X-Algo-Name` wiring (pass `algo_name=` manually) |
| Full decision audit trail | Any test coverage of a live path |

**Caveat that will bite first:** with a sandbox token this fails with 401 — the sandbox
token is documented as unusable on live hosts. With a live token it executes. There is no
middle state.

---

## 5. Route B — a truthful live mode ✅ IMPLEMENTED

If you are going to trade real money, do it so the system cannot lie to you. This plan is
now implemented; the shapes below match the shipped code (see the status table at the top
for file-by-file pointers):

**B1. Correct the host classification** (`app/brokers/sandbox.py`)

```python
SANDBOX_HOSTS = frozenset({"api-sandbox.upstox.com", "sandbox.upstox.com"})
LIVE_HOSTS    = frozenset({"api-hft.upstox.com"})
```
- `UpstoxSandboxBroker` refuses `LIVE_HOSTS` **unconditionally** (as it already does for
  `api.upstox.com`), so a mis-set `UPSTOX_SANDBOX_ACCESS_TOKEN` can never place a real order.
- Add `UpstoxLiveBroker(UpstoxSandboxBroker)` that requires `LIVE_HOSTS`, sets
  `mode="UPSTOX_LIVE"`, and has a truthful `repr`.
- Fix the default: `UPSTOX_SANDBOX_BASE_URL=https://api-sandbox.upstox.com`.

**B2. Triple-lock the Settings switch** (`app/config/settings.py`)

```python
broker: str = "LOCAL_PAPER"   # LOCAL_PAPER | UPSTOX_SANDBOX | UPSTOX_LIVE
mode:   str = "PAPER"         # PAPER | LIVE
live_trading: bool = False
```

Keep the existing fail-closed validation, but replace the blanket refusal with an explicit
conjunction, so no single variable can go live:

```python
if broker == "UPSTOX_LIVE":
    if not (self.mode.upper() == "LIVE" and self.live_trading is True):
        raise ValueError(
            "BROKER=UPSTOX_LIVE requires MODE=LIVE and LIVE_TRADING=true together")
elif self.mode.upper() != "PAPER" or self.live_trading:
    raise ValueError("Only MODE=PAPER and LIVE_TRADING=false are permitted "
                     "unless BROKER=UPSTOX_LIVE")
```

**B3. Make the reporting truthful**
- `app/api/routes/health.py:12-13`, `dashboard.py:127-131`, `v2.py:39` → read
  `settings.mode` / `settings.live_trading` instead of hardcoding.
- `app/reporting/weekly.py:79,125`; `desktop/app.py:274` → same.
- DB: write to `live_orders` / `live_transitions` (or add a `live` column) so the audit
  trail is not called "sandbox".

**B4. Live-specific safety (new work, not present today)**
- Start-up confirmation: require `ARENA_LIVE_CONFIRM=<the account's client id>` or similar
  before the engine enters its loop.
- A hard non-zero `MAX_POSITION_VALUE` / `MAX_DAILY_LOSS` requirement in live mode
  (fail closed if left at 0 = unlimited).
- Pass `algo_name` (→ `X-Algo-Name`) and enforce the static-IP origin.
- Reconcile with the live order-history endpoint, with a stuck-order alarm.
- A canary mode: first N orders capped at a tiny notional.

**B5. Tests and docs**
- Update the ~14 test files listed in §2 that assert paper-only (notably
  `test_final_security.py`, `test_settings_parser.py`, `test_runtime_database_api.py`,
  `test_v1_separation.py`).
- Fix `test_v1_separation.py:60-68`, whose "no live order endpoint ever contacted"
  assertion is currently false.
- Rewrite the live-trading claims in `README.md:43-53,231,1146`, `USER_MANUAL.md:53,618-635,977`,
  `app/__main__.py:30-31`, `app/engine/sandbox_engine.py:20`.
- Add a `tools/security_scan.py` rule: a live host must never appear in sandbox config.

Rough scale: ~40 lines in `settings.py`, ~80 in `sandbox.py` (+ a live subclass), ~20 in
the API routes, sizeable doc/test updates.

---

## 6. Pre-flight checklist before real money

- [ ] Strategy validated **out-of-sample** and walk-forward; costs, slippage and spread modelled.
- [ ] You have watched it run in `paper-live` against the same live data feed for weeks, not days.
- [ ] Risk limits are **non-zero** (`MAX_POSITION_VALUE`, `MAX_OPEN_POSITIONS`, `MAX_DAILY_LOSS`, `MAX_TRADES_PER_DAY`).
- [ ] `EMERGENCY_STOP` tested end-to-end (trigger, restart, resume).
- [ ] The instrument identity map is correct — a missing mapping is fail-closed today, keep it that way.
- [ ] Token refresh is automated and monitored (daily expiry).
- [ ] A stuck/unreconciled-order alarm exists and someone is paged.
- [ ] Sizing is canary-sized for the first weeks; one instrument, one strategy.
- [ ] You accept that a bug in *your* adapter can lose money faster than any research edge earns it.
- [ ] The strategy layer is gated by the promotion machinery (`app/learning/evaluator.py`), which only promotes out-of-sample-positive candidates — start from a promoted version, not a raw one.

## 7. Regulatory reality (India) — as of this audit

- SEBI/NSE require **order APIs to originate from a registered static IP** (primary +
  backup), in force since 2025-10-01. Orders from unregistered/dynamic IPs are rejected.
  Register the IP on the Upstox Developer Apps page (changeable once a week).
- **Low-frequency algos (<10 orders/sec) do not need exchange registration**, but brokers
  apply risk checks and tag orders with a generic algo ID; higher frequencies require NSE
  registration. The repo's engine is low-frequency by construction.
- `X-Algo-Name` is optional and only required if you have an exchange-approved algo name;
  `UpstoxSandboxBroker` already accepts `algo_name=` and sends it.
- Verify current rules with Upstox/NSE before going live — this is a summary, not legal advice.

---

## 8. What changed in this work

**Audit phase (analysis only):** added this document. No production code, configuration
or safety default was modified at that point.

**Route B phase (implemented on this branch):** fixed the host classification, added
`UpstoxLiveBroker` with live-only hosts and separate audit tables, added the four-switch
gate plus the non-zero risk-limit requirement, made every label (`/health`, dashboard,
weekly report, engine status, desktop) derive the truth from Settings, added
`python -m app live-preflight` (read-only), and added 37 tests
(`tests/test_live_hardening_v2.py`) covering the gate, host refusal, audit separation and
truthful reporting.

Live trading is still **OFF** in the shipped defaults: no token, no confirm phrase, no
non-zero limits, `MODE=PAPER`. Enabling it is an explicit operator action.

**Verification:** `.venv` recreated locally (git-ignored), full suite **983 passed /
37 skipped**, `tools/security_scan.py` → CLEAN, `python -m app engine` and
`python -m app live-preflight` both refuse to proceed under default settings with no
network access.

**Recommended next steps, in priority order:**

1. Read §6 and §7 before enabling; register the static IP; use a **dedicated**
   `DATABASE_URL` for live so sandbox and live engine state can never mix.
2. Run `python -m app live-preflight` with your live token — it places nothing.
3. Start canary-sized: one instrument, one strategy, small non-zero limits, and watch
   `live_orders`/`live_transitions` and the reconciliation log closely.
4. Still outstanding if you want them (needs your input): automated token refresh,
   stuck-order paging, canary notional ramp, hard exchange-side kill switch.
