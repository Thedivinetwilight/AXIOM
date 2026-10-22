# US Equities Paper Trading (Alpaca Market Data)

> **Operator documentation lives in
> [`US_EQUITIES_PAPER_OPERATOR_MANUAL.md`](US_EQUITIES_PAPER_OPERATOR_MANUAL.md).**
> That manual covers configuration, data-only credentials, market hours, start
> and stop procedures, status interpretation, risk controls, troubleshooting,
> restart/recovery and the exact limits of the evidence. This page is the short
> technical summary.

## What this mode does

AXIOM can subscribe to Alpaca US stock market data and send completed 1-minute
bars into its existing local `PaperBroker`. Every fill and position is simulated
inside AXIOM. The Alpaca integration in `app/data/alpaca.py` uses only the
market-data REST and WebSocket hosts; it has no broker order client, no order
endpoint, and no account-mutation call. Keep `MODE=PAPER`,
`LIVE_TRADING=false`, and `BROKER=LOCAL_PAPER` — the configuration validator and
the start route both refuse anything else.

This mode is a paper-trading feature, not evidence of a profitable strategy or
authorization to trade real money. A paper fill is a model: it cannot reproduce
queue position, market impact, all fee schedules, or actual execution.

## Before starting

1. Use an Alpaca key that is entitled to the selected market-data feed. The
   default `iex` feed is the IEX venue and does not represent a consolidated
   all-exchange tape; liquid names only is the honest way to use it.
2. Configure AXIOM's `.env` with the market-data key and secret. Do not paste
   them into chat, logs, screenshots or commits.
3. Set the US paper settings below. All ten limits are mandatory for
   `DATA_SOURCE=alpaca`; there are no hidden defaults in this mode.
4. Restart AXIOM and check the status page. Confirm mode `PAPER`, broker
   `LOCAL_PAPER`, provider `alpaca`, feed `iex`/selected feed, connected,
   authenticated, subscribed, and a fresh bar timestamp.
5. Start a paper session for a supported ticker and `1m`. Check status,
   simulated orders, fills, positions and risk events. Stop the session when
   finished. No trade is sent to Alpaca.

## Environment settings

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
ALPACA_API_KEY=your-market-data-key
ALPACA_API_SECRET=your-market-data-secret
ALPACA_DATA_FEED=iex
ALPACA_SYMBOLS=AAPL,MSFT,SPY
ALPACA_BACKFILL_BARS=240
PAPER_MAX_POSITION_VALUE=1000
PAPER_MAX_OPEN_POSITIONS=2
PAPER_MAX_DAILY_LOSS=100
PAPER_MAX_DRAWDOWN=0.05
PAPER_MAX_TRADES_PER_DAY=4
PAPER_MAX_GROSS_EXPOSURE=0.60
PAPER_HALT_POLICY=FLATTEN
```

The numbers above are examples for configuring bounded simulation risk, not
personalized sizing advice. `PAPER_MAX_DRAWDOWN` is a fraction, so `0.05` means
a 5% simulated peak-equity drawdown stop. Once the daily-loss limit is reached,
new entries remain blocked through that New York trading day. Once the drawdown
cap is reached, new entries remain blocked until the paper account is
reset/reviewed. `PAPER_HALT_POLICY=FLATTEN` also submits simulated exits for the
open positions, so the bound is a bound rather than a suggestion.

## Calendar and data behavior

- Session timestamps are interpreted in `America/New_York`, so daylight-saving
  changes are handled by the IANA timezone database.
- The regular session is 09:30–16:00 New York time, with scheduled 13:00 early
  closes, weekends, and the NYSE holiday rules (including the rule that a
  Saturday New Year's Day is not observed at all).
- The offline rule set covers normal NYSE sessions. Extraordinary exchange
  closures require an exchange notice: apply them with `US_CLOSED_DATES` /
  `US_EARLY_CLOSE_DATES`, which enter the calendar signature.
- Streaming supports a configured list of plain US tickers and completed
  1-minute bars only. Other timeframes are refused rather than silently
  resampled. AXIOM evaluates the selected single-symbol strategy independently
  on each subscribed symbol; this is not a relative-strength ranking portfolio.
- Invalid OHLCV, duplicate/conflicting bars, future bars, stale bars over two
  minutes old, and out-of-order bars fail closed. Bars outside the regular
  session are dropped and counted rather than traded. A gap pauses new signals
  and cancels resting entries (`risk_counters.out_of_session_bars` and the
  `Data gap` error state make both visible).
- Alpaca `updatedBars` are late-trade corrections to a prior minute. AXIOM
  ignores corrections after the original bar has been emitted; review data
  quality when comparing the paper log against later corrected history.
  Ignoring them is deliberate: replaying a correction into a decision that has
  already been made would be a look-ahead hazard.
- On start, up to `ALPACA_BACKFILL_BARS` completed bars prime the indicators.
  Only bars that are already complete and inside a real session are used, so the
  backfill cannot look ahead. A failed backfill degrades to an empty warm-up and
  is reported as `warmup.status = failed`.
- `AlpacaHistoricalProvider` is a read-only, paginated historical-bars adapter.
  It defaults to `adjustment=split` so an overnight split cannot make the warm-up
  window incomparable with live bars.

## Research evidence and limits

The repository's generic daily `DEMO`/`ALT` sample CSVs are not US equity
history and must not be used to claim US strategy performance.

`tools/us_equities_research.py` implements the full harness:
chronological train/validation/untouched-test splits, walk-forward folds,
regime tagging, cost modelling (commission, spread, slippage, participation
partial fills, gaps), a 3× spread/slippage stress case, buy-and-hold and
momentum baselines, and the full metric set. It executes through the same
`PaperBroker` used by live paper trading.

**It refuses to run without a compliant dataset.** This repository does not
ship one, because no US equity history with a documented licence, adjustment
policy and point-in-time universe is bundled or reproducible offline here. In
that state the tool exits `2`, writes `results/us_equities/DATA_REQUIRED.json`,
and states that nothing was evaluated. No synthetic or demo prices are
substituted and no strategy is selected.

### Research dataset contract

```
data/us_equities/
├── manifest.json          # source/licence/adjustment/universe declaration
├── bars.csv               # symbol,timestamp,open,high,low,close,volume
├── universe.csv           # symbol,start,end  (point-in-time membership)
└── corporate_actions.csv  # symbol,ex_date,type,ratio,amount
```

`manifest.json`:

```json
{
  "schema": "axiom.us.equities.dataset/1",
  "source": {
    "name": "Alpaca Markets historical bars",
    "license": "Alpaca Market Data terms — <your plan>",
    "url": "https://alpaca.markets/data",
    "retrieved_at": "2026-10-01T00:00:00+00:00",
    "feed": "sip"
  },
  "adjustment": "split_dividend",
  "price_basis": "total_return_prices",
  "bar_timeframe": "1D",
  "bar_timestamp_convention": "session_close",
  "timezone": "UTC",
  "benchmark": "SPY",
  "universe": {
    "kind": "point_in_time",
    "membership_csv": "universe.csv",
    "description": "S&P 500 membership as of each date, from <source>"
  },
  "bars_csv": "bars.csv",
  "corporate_actions_csv": "corporate_actions.csv"
}
```

Each daily bar must be stamped within five minutes of the real New York session
close for its date (early closes included). An open-stamped bar used as a close
is look-ahead, and the loader refuses it.

No profitability claim is made by this paper integration. The app's independent
live-trading controls remain separate; do not change the four live switches to
run a US paper session.
