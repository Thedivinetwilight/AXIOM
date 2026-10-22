# US Equities Paper-Trading Operator Manual

**Scope: US-listed stocks and ETFs, paper trading only.** Every order, fill,
position and P&L number produced by this subsystem is simulated inside AXIOM by
`app/paper_trading/broker.py`. Alpaca is used **only as a read-only market-data
feed**. There is no Alpaca trading client, no order endpoint, and no account
mutation anywhere in this code path.

**This system is not approved for real-money trading.** A paper session is a
test of *plumbing and risk controls*, not proof of a profitable strategy. Read
§10 before drawing any conclusion from a paper session.

---

## 1. What runs, and what is deliberately absent

| Component | Where | Paper behaviour |
| --- | --- | --- |
| Market data (REST history) | `app/data/alpaca.py::AlpacaHistoricalProvider` | `GET https://data.alpaca.markets/v2/stocks/bars` only |
| Market data (stream) | `app/data/alpaca.py::AlpacaStream` | `wss://stream.data.alpaca.markets/v2/{feed}` only |
| Session calendar | `app/markets/us_calendar.py` | NYSE rules in `America/New_York` |
| Execution | `app/paper_trading/broker.py::PaperBroker` | Local simulated fills |
| Orchestration | `app/paper_trading/simulator.py::PaperSimulator` | Async bar loop, health, risk |
| Research | `app/research/us_equities/` | Offline evaluation, no network |

Absent by design: order placement, cancellation, position/account APIs at a
broker, short selling, margin, options, and any pre/post-market execution.

Three configuration switches are jointly required and are enforced in
`Settings.__post_init__` **and again** in the start route:

```
MODE=PAPER
LIVE_TRADING=false
BROKER=LOCAL_PAPER
```

`DATA_SOURCE=alpaca` with any other value is rejected at startup, not at the
first order.

---

## 2. Prerequisites

* AXIOM checked out or installed from this branch/commit.
* Python 3.11+ (`python -m app` or the packaged executable).
* An Alpaca account with **market-data** access. A funded brokerage account is
  *not* required for data; a free account gets the IEX feed.
* No Indian-market configuration left over: `MARKET=us`, `CURRENCY=USD`,
  `MARKET_CALENDAR=us-equities`.

---

## 3. Getting Alpaca data-only credentials

The goal is a key you cannot place an order with. Alpaca separates trading keys
from data keys in its dashboard; do both steps:

1. Create/sign in at `https://alpaca.markets`.
2. **Do not enable live trading.** If the account already offers live trading,
   leave it disabled. Use the paper environment.
3. Open **Home → API Keys** (paper environment) and generate a key pair. Copy
   both values once — the secret is shown only once.
4. Confirm the key's scope. A paper-environment key is a *paper trading* key; it
   is used here **only** as an HTTP basic-auth credential on the market-data
   host. AXIOM never calls an order host with it. If you want the strongest
   separation available, create the key on an account whose trading is disabled
   entirely.
5. Check your data entitlement:
   * `ALPACA_DATA_FEED=iex` — free/standard. **Single venue.** Bars exist only
     when IEX itself printed. Illiquid names will have missing minutes.
   * `ALPACA_DATA_FEED=sip` — consolidated tape; requires a paid
     market-data subscription. Use this for anything other than a smoke test.
   * `ALPACA_DATA_FEED=delayed_sip` — 15-minute delayed consolidated data. Safe
     for paper, but stale-by-design; the freshness indicators will show it.
6. Put the values in `.env` (never in a file that is committed, never in a
   screenshot, never in chat):

```dotenv
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
```

If a key is ever exposed, revoke it in the Alpaca dashboard immediately and
generate a new pair. AXIOM never logs, returns or persists the key or secret:
`Settings` marks both with `repr=False`, `/paper/status` allowlists its fields,
and the provider only ever reports the *error code* of an auth failure.

---

## 4. Configuration reference

Minimum working US paper configuration (`.env`):

```dotenv
MODE=PAPER
LIVE_TRADING=false
BROKER=LOCAL_PAPER

MARKET=us
CURRENCY=USD
MARKET_CALENDAR=us-equities
DATA_SOURCE=alpaca
DEFAULT_SYMBOL=AAPL
DEFAULT_TIMEFRAME=1m

ALPACA_API_KEY=...
ALPACA_API_SECRET=...
ALPACA_DATA_FEED=iex
ALPACA_SYMBOLS=SPY,QQQ,AAPL,MSFT
ALPACA_BACKFILL_BARS=240

PAPER_MAX_POSITION_VALUE=1000
PAPER_MAX_OPEN_POSITIONS=3
PAPER_MAX_DAILY_LOSS=100
PAPER_MAX_DRAWDOWN=0.05
PAPER_MAX_TRADES_PER_DAY=6
PAPER_MAX_GROSS_EXPOSURE=0.60
PAPER_HALT_POLICY=FLATTEN
```

All ten `PAPER_*` limits are **mandatory** for `DATA_SOURCE=alpaca`. Zero or
missing values are a startup error. The numbers above are illustrative sizing
for a disposable simulation; choose your own, but choose them deliberately.

| Variable | Meaning | Validation |
| --- | --- | --- |
| `ALPACA_SYMBOLS` | Comma-separated plain US tickers subscribed on the stream | distinct, `^[A-Z][A-Z0-9.\-]{0,14}$` |
| `ALPACA_DATA_FEED` | `iex` \| `sip` \| `delayed_sip` | enumerated |
| `ALPACA_BACKFILL_BARS` | Completed 1m bars fetched before the live loop starts, to prime indicators | integer 0–10000 |
| `PAPER_MAX_POSITION_VALUE` | Max marked value of one symbol's position (USD) | > 0 |
| `PAPER_MAX_OPEN_POSITIONS` | Max distinct open positions | > 0 |
| `PAPER_MAX_DAILY_LOSS` | Max New-York-day equity loss (USD) before entries stop | > 0 |
| `PAPER_MAX_DRAWDOWN` | Max peak-to-trough fraction of paper equity | in (0,1) |
| `PAPER_MAX_TRADES_PER_DAY` | Max entry orders per New-York day | > 0 |
| `PAPER_MAX_GROSS_EXPOSURE` | Max fraction of equity deployed (positions **plus** resting entries) | in (0,1] |
| `PAPER_HALT_POLICY` | `BLOCK_ENTRIES` or `FLATTEN` | enumerated |
| `US_CLOSED_DATES` | Extra full closures, ISO dates, comma-separated (exchange notice) | parseable dates |
| `US_EARLY_CLOSE_DATES` | Extra 13:00 half days, ISO dates | parseable dates; cannot overlap `US_CLOSED_DATES` |

Execute `python tools/live_config_check.py` (if present in your build) or simply
start AXIOM: any invalid combination raises at startup with the name of the
offending variable. A configuration that passes startup is one the engine will
accept — readiness and the engine share the same validators.

---

## 5. Market hours

All session logic runs in `America/New_York` through the IANA database, so
daylight-saving transitions need no configuration:

| Session | New York | UTC, winter (EST) | UTC, summer (EDT) |
| --- | --- | --- | --- |
| Regular open | 09:30 | 14:30 | 13:30 |
| Early close | 13:00 | 18:00 | 17:00 |
| Regular close | 16:00 | 21:00 | 20:00 |

Implemented rules (verified against the NYSE Group 2026–2028 holiday and early
closing calendar, published 2025-12-23):

* **Full closures** — New Year's Day, MLK Day, Washington's Birthday, Good
  Friday, Memorial Day, Juneteenth (from 2022), Independence Day, Labor Day,
  Thanksgiving, Christmas. Weekend holidays move to the preceding Friday or the
  following Monday.
* **New Year's Day is the exception**: when 1 January is a Saturday, NYSE
  observes **no** holiday at all. 2027-12-31 is therefore a normal trading day.
* **Early closes (13:00)** — the day after Thanksgiving; 24 December *only when
  25 December is a weekday*; 3 July *only when 4 July is a weekday*. There is no
  July half day in 2026 or 2027, and no December half day in 2027.
* Bars outside the regular session are **dropped, not traded**. The counter
  `risk_counters.out_of_session_bars` records them.
* Extraordinary closures (national day of mourning, weather, exchange halt) are
  **not discovered automatically**. Apply them with `US_CLOSED_DATES` /
  `US_EARLY_CLOSE_DATES`, which are recorded in the account's calendar
  signature so a restart can never silently continue under different hours.

Verify any date without starting a session:

```python
from datetime import datetime
from app.markets.registry import market_session_state
print(market_session_state("us", datetime(2026, 11, 27, 12, 45)).to_dict())
```

---

## 6. Starting a session

### 6.1 Desktop / browser UI

1. Start AXIOM (`python -m app` or the executable) and open the local URL.
2. The mode badge must read **`PAPER — ALPACA US DATA / LOCAL SIMULATED ORDERS`**.
3. Command Center → **US paper session · simulated fills** shows session clock,
   feed, connection, freshness and risk limits before you start anything.
4. Press **START PAPER**. If the engine is already running the button is
   replaced by **STOP PAPER**.
5. Watch the same card: `Connection: CONNECTED`, `authenticated /
   subscribed`, `Last completed bar` advancing each minute, `Feed freshness:
   FRESH`.

### 6.2 HTTP API

```bash
curl -X POST http://127.0.0.1:8000/api/paper/start-default \
     -H "Authorization: Bearer $AXIOM_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"source":"alpaca"}'
```

or the explicit form:

```bash
curl -X POST http://127.0.0.1:8000/paper/start \
     -H "Authorization: Bearer $AXIOM_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"source":"alpaca","symbol":"AAPL","timeframe":"1m","strategy":"moving_average"}'
```

`symbol` must be one of `ALPACA_SYMBOLS`; the engine subscribes to all of them
and evaluates the selected strategy per symbol. `timeframe` must equal
`DEFAULT_TIMEFRAME` and the Alpaca adapter supports `1m` only — anything else is
refused rather than silently resampled.

### 6.3 What happens during startup

1. The saved paper account is inspected read-only for compatibility (currency,
   timeframe, cost model, calendar signature). A mismatch refuses startup and
   changes nothing.
2. The provider connects, authenticates, and subscribes; the handshake is
   confirmed by reading Alpaca's success frames, not assumed.
3. Up to `ALPACA_BACKFILL_BARS` **completed** 1-minute bars are fetched per
   symbol so indicators are warm immediately. Bars that are not already
   complete, or not inside a real session, are discarded. A failed backfill
   degrades to an empty warm-up and is reported as `warmup.status = failed`.
4. The bar loop starts. Nothing is traded until a closed bar arrives.

---

## 7. Reading the status

`GET /paper/status` (and the UI card) exposes:

| Field | Meaning |
| --- | --- |
| `mode` / `live_trading` | Always `PAPER` / `false` |
| `simulated_fills`, `paper_mode` | `true` — every fill is simulated |
| `state` | `STOPPED`, `STARTING`, `RUNNING`, `PAUSED`, `ERROR` |
| `connection` | Provider socket state: `DISCONNECTED`, `CONNECTING`, `CONNECTED`, `RECONNECTING` |
| `authenticated`, `subscribed` | Handshake and subscription confirmed by the feed |
| `provider`, `feed_mode`, `timeframe` | `alpaca`, `iex`/`sip`, `1m` |
| `market_status`, `market_open` | Session clock from the US calendar |
| `last_candle_time` | Completion time of the newest accepted bar |
| `stale_feed`, `stale_reason` | `true` when no completed bar arrived within 120 s **while the market is open**; a closed market reports `stale_reason: market_closed` instead of a misleading red badge |
| `reconnects` | Counted socket reconnects |
| `warmup` | `{requested, loaded, status, error}` for the indicator backfill |
| `risk_counters` | `actionable_signals`, `orders_created`, `skipped`, `rejected_bars`, `out_of_session_bars`, `deduplicated_bars` |
| `paper_risk_limits` | The live limits the broker is enforcing |
| `paper_risk_halts` | `daily_loss_date`, `drawdown` latch state |
| `paper_risk_events` | Newest 10 risk events (bounded log, 200 entries) |
| `gross_exposure`, `paper_account_equity` | Current marked exposure and equity |
| `error_reason` | Allowlisted reason code, never raw provider text |

Order and fill state is at `/paper/orders`, `/paper/trades`,
`/paper/positions`, `/paper/account`. Every order carries
`status` (`NEW`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`) and a
`reason`:

| `reason` | Meaning |
| --- | --- |
| `signal` | Strategy entry/exit filled |
| `insufficient_cash` / `insufficient_position` | Rejected at fill time |
| `paper_max_position_value` | Per-symbol cap reached; residual cancelled |
| `paper_max_gross_exposure` | Portfolio exposure budget exhausted |
| `exit_signal` | Strategy exit |
| `risk_halt_flatten` | Exit issued because a daily-loss / drawdown halt fired |
| `bot_stopped` | Cancelled by stop / shutdown |
| `paper_risk_limit_breach` | Entry cancelled when a halt latched |

A fill is **always** simulated. There is no code path in this subsystem that can
transmit an order.

---

## 8. Risk controls

Entry-side limits are checked before an order is created; the per-symbol cap is
also re-checked at fill time so an adverse gap cannot overshoot it.

| Control | Enforcement |
| --- | --- |
| Position size | `equity × POSITION_SIZE`, then `PAPER_MAX_POSITION_VALUE` |
| Gross exposure | Positions **plus resting entry notional** may not exceed `PAPER_MAX_GROSS_EXPOSURE × equity` |
| Open positions | Counted across all symbols; pending entries count |
| Daily loss | Equity vs. session-start equity in New York time; latches for the day |
| Drawdown | Peak-to-current equity fraction; latches until the account is reset |
| Trade count | Entry orders created per New York day |
| Market risk per fill | `STOP_LOSS` / `TAKE_PROFIT` from the execution config, stop wins when both are touched in one bar |

`PAPER_HALT_POLICY` decides what a breach does:

* `BLOCK_ENTRIES` (default for non-US replay) cancels resting entries and keeps
  inventory, relying on the stop loss.
* `FLATTEN` (recommended for US paper) additionally submits a simulated market
  exit for every open position on the next processed bar. Use this if you want
  "daily loss" to mean a bound rather than a suggestion.

Exits are never blocked by an entry limit — a control that can trap inventory is
not a control.

To clear a latched drawdown halt you must reset the paper account (use a fresh
`DATABASE_URL`); the halt is intentionally not clearable from the UI.

---

## 9. Stopping, pausing, restarting

### 9.1 Kill / stop control

The Command Center engine controls always expose a stop button whenever the
engine is `RUNNING`, `PAUSED`, or the status poll is stale but the last known
state was running. It asks for confirmation, then `POST /paper/stop`.

Stopping: cancels every working order (`reason=bot_stopped`), stops the bar
loop, persists the account and releases the database writer. Calling it twice is
safe (idempotent).

`POST /paper/pause` is softer: it cancels pending **entries** only and keeps
protective exits running (`PAUSED`). `POST /paper/resume` returns to `RUNNING`.

If you cannot reach the UI, the CLI equivalent is:

```bash
curl -X POST http://127.0.0.1:8000/paper/stop -H "Authorization: Bearer $AXIOM_API_TOKEN"
```

### 9.2 Restart and recovery

The paper account is a single SQLite row plus orders/fills/trades. Recovery is
designed to be boring:

* **Bar cursor** — `last_bars`/`last_candles` persist, so a re-delivered bar is
  ignored (`deduplicated_bars` increments).
* **Deterministic order identity** — every strategy order carries
  `dedupe_key = strategy|symbol|side|decision-time`; replaying the same decision
  after a crash is a no-op (`risk_events: order_deduplicated`).
* **One transaction per bar** — the account snapshot, fills and orders for a bar
  commit together, so a crash mid-bar leaves no half-applied state.
* **Compatibility gate** — a saved account created under a different currency,
  timeframe, cost model or calendar signature is refused, naming the differing
  fields. Nothing is migrated or overwritten.
* **Stale status** — if the UI cannot reach `/paper/status`, it shows
  `PAPER STATUS STALE` and keeps a stop button for the last known running state
  rather than offering a start.

After a restart, verify: `state=RUNNING`, `connection=CONNECTED`, risk limits
unchanged, positions/fills identical to before the stop, and no duplicate order
timestamps.

---

## 10. Research evidence — what it can and cannot tell you

`tools/us_equities_research.py` evaluates explainable long-only rules on a
real, licensed, point-in-time dataset. It **refuses to run** without one.

```bash
python tools/us_equities_research.py --dataset data/us_equities --out results/us_equities
```

Exit code `2` means no valid dataset: the tool writes
`results/us_equities/DATA_REQUIRED.json` listing exactly what is missing and
prints:

```
NO VALID US EQUITY DATASET — nothing was evaluated.
```

No synthetic, demo or replay price series is ever substituted, and no
out-of-sample number is invented. Until a compliant dataset is supplied,
**strategy selection is explicitly unresolved**.

### 10.1 Dataset contract

```
data/us_equities/
├── manifest.json          # provenance, adjustment, universe kind
├── bars.csv               # symbol,timestamp,open,high,low,close,volume
├── universe.csv           # symbol,start,end   (point-in-time membership)
└── corporate_actions.csv  # symbol,ex_date,type,ratio,amount
```

Requirements enforced by `app/research/us_equities/dataset.py`:

* `source.name`, `source.license`, `source.url`, `source.retrieved_at` present —
  an unattributed dataset is not evidence;
* `bar_timeframe = 1D` and `bar_timestamp_convention = session_close`; every
  timestamp must sit within five minutes of the real New York close for its
  date, early closes included (an open-stamped bar used as a close is
  look-ahead);
* `adjustment ∈ {raw, split, split_dividend}` plus an explicit `price_basis`;
  `raw` is refused without a corporate-actions file;
* point-in-time membership with a start and end date per symbol; a fixed
  survivor list is accepted only as `universe.kind = static_survivors`, is
  flagged, and can never produce a selection verdict;
* no duplicate, unsorted, non-finite or impossible OHLCV rows;
* a benchmark series (e.g. `SPY`) for the regime split and the buy-and-hold
  comparison.

### 10.2 Method

* Chronological split: 50% train, 20% validation, 30% untouched test — the test
  window is opened once.
* Candidate selection uses the **validation** window only, by net Sharpe.
* Walk-forward: the test window is sliced into contiguous folds and each fold is
  reported separately, so one lucky quarter cannot stand in for the whole.
* Regime tagging: benchmark trend (200-day average) and 21-day realized
  volatility per ~monthly block.
* Execution goes through the **same `PaperBroker`** as live paper trading, so
  commission, half-spread, slippage, participation-limited partial fills, gaps
  and cash conservation are identical by construction. Signals are formed at a
  session close and can only fill at the *next* session's open.
* Costs: commission per side, half-spread per side, slippage per side, 2% of bar
  volume participation; a stress case multiplies spread and slippage by 3×.
* Metrics reported: net return, CAGR, Sharpe, Sortino, maximum drawdown, profit
  factor, win rate, trade count, exposure, one-way annualized turnover, maximum
  single-position weight, concentration (HHI) and total transaction costs.

### 10.3 Candidates

| Key | Rule |
| --- | --- |
| `buy_hold` | Equal-weight the point-in-time universe, never rebalance |
| `momentum_trend` | 12-1 month momentum, positive names only, top N, monthly |
| `relative_strength` | Same ranking without the sign filter (control) |
| `moving_average` | Close above its 200-day SMA, ranked by 3-month return |
| `breakout` | Close above the prior 20-session high |
| `mean_reversion` | 20-day z-score ≤ −1, most oversold first, weekly |
| `momentum_regime` | Momentum only while the benchmark is above its 200-day SMA |
| `mean_reversion_regime` | Mean reversion gated by the same regime filter |

All are long-only. Short trades are not modelled: credible shorting needs
historical borrow availability, borrow fees and locate/recall behaviour, and
without those the numbers could not be defended.

---

## 11. Troubleshooting

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Startup: `DATA_SOURCE=alpaca requires MARKET=us and CURRENCY=USD` | leftover Indian configuration | set `MARKET=us`, `CURRENCY=USD`, `MARKET_CALENDAR=us-equities` |
| Startup: `requires PAPER_MAX_...` | a limit left at 0 | set every `PAPER_*` limit explicitly |
| Startup: `paper account startup refused: ... differing fields ...` | existing account was created under another currency/timeframe/cost model/calendar | use a fresh `DATABASE_URL`; nothing was modified |
| `error_reason = provider_authentication_failed` | wrong/expired key, or a feed you are not entitled to | re-issue the key; check `ALPACA_DATA_FEED` against your subscription |
| `error_reason = provider_stream_failed` | network, DNS, proxy, or repeated disconnects (the adapter gives up after 50 attempts) | check egress; verify the host is reachable |
| `error_reason = stale_live_candle` | the feed delivered a bar older than 120 s | check system clock (NTP); a badly lagging feed is not tradable |
| `error_reason = unfinished_live_candle` | clock skew or a partial-bar feed | fix the clock; never disable this check |
| `error_reason = conflicting_duplicate_candle` | two different bars for the same minute | stop, review `last_candles`, use a fresh account if you cannot explain it |
| State `PAUSED`, error mentions `Data gap` | the feed skipped a minute (common on the IEX single-venue feed) | entries are cancelled automatically; review, then `POST /paper/resume` |
| No trades at all | strategy needs `history_limit` bars; check `warmup` | raise `ALPACA_BACKFILL_BARS`, or wait |
| `paper_risk_halts.drawdown = true` and entries keep being refused | latched drawdown stop | reset the account with a fresh `DATABASE_URL` after review |
| `gross_exposure` stuck near its cap | positions plus resting entries count | expected; entries are blocked, exits are not |
| UI shows `PAPER STATUS STALE` | the status poll failed three times | the stop button still works against the last known state; check the server process |

---

## 12. Verification status — read this before trusting anything

Verified in this repository by automated tests and CI:

* New York session behaviour across DST, holidays, early closes and operator
  overrides, cross-checked against the published NYSE calendar.
* Rejection of stale, missing, future, malformed, duplicate, conflicting and
  out-of-order bars; out-of-session bars are dropped and counted.
* No look-ahead: decisions use only bars closed at or before the decision time;
  research orders fill at the next session's open.
* Dry-run provider handshake, subscription confirmation, reconnect and backfill
  behaviour, all against **mocks**.
* Risk limits, halt policy, idempotent order identity, restart/recovery, and
  stop/pause idempotency.
* Read-only guarantee: no order/cancel/account call exists in the Alpaca adapter.

**Not verified here, and requiring your credentials:** an actual live Alpaca
market-data connection, your feed entitlement, and real streaming behaviour
during a US session. No test in this repository claims otherwise.

---

## 13. Known limitations

1. **IEX is one venue.** With `ALPACA_DATA_FEED=iex`, a minute with no IEX print
   has no bar, which the gap policy treats as a data gap and pauses on. Use
   `sip` for anything beyond a smoke test, or restrict yourself to liquid names.
2. **Corrections are ignored.** Alpaca's `updatedBars` are not replayed onto an
   already emitted minute. That is deliberate (replaying them would be a
   look-ahead hazard) but it means the paper log can differ from later corrected
   history.
3. **Partial fills are volume-participation approximations**, not a queue model.
4. **No short selling, margin, leverage, options, or extended-hours trading.**
5. **Fill realism**: no auction modelling, no latency, no market impact beyond
   the configured slippage. Real fills would be worse.
6. **The offline holiday table cannot see an unscheduled closure**; use the
   override variables and an exchange notice.
7. **Research is unresolved until a compliant dataset is supplied.** No
   strategy has been selected and no out-of-sample result is claimed.
8. **Paper performance does not imply live profitability.** Costs, latency,
   liquidity and your own execution discipline will all be worse than a
   simulation.

---

## 14. Quick reference

```bash
# Start (UI is preferred)
python -m app

# Status / orders / fills
curl -s -H "Authorization: Bearer $AXIOM_API_TOKEN" localhost:8000/paper/status | jq
curl -s -H "Authorization: Bearer $AXIOM_API_TOKEN" localhost:8000/paper/orders | jq
curl -s -H "Authorization: Bearer $AXIOM_API_TOKEN" localhost:8000/paper/trades | jq

# Kill
curl -X POST -H "Authorization: Bearer $AXIOM_API_TOKEN" localhost:8000/paper/stop

# Research (requires a real dataset; otherwise exits 2 and explains what is missing)
python tools/us_equities_research.py --dataset data/us_equities --out results/us_equities

# Tests
python -m pytest -q -k "not slow"
```
