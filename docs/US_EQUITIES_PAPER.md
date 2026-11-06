# US Equities Paper Trading (Alpaca Market Data)

## What this mode does

AXIOM can subscribe to Alpaca US stock market data and send completed 1-minute
bars into its existing local `PaperBroker`. Every fill and position is simulated
inside AXIOM. The Alpaca integration in `app/data/alpaca.py` uses only the
market-data REST and WebSocket hosts; it has no broker order client or order
endpoint. Keep `MODE=PAPER`, `LIVE_TRADING=false`, and `BROKER=LOCAL_PAPER`.

This mode is a paper-trading feature, not evidence of a profitable strategy or
authorization to trade real money. A paper fill is a model: it cannot reproduce
queue position, market impact, all fee schedules, or actual execution.

## Before starting

1. Use an Alpaca account/key that is entitled to the selected market-data feed.
   The default `iex` feed is the IEX venue and does not represent a consolidated
   all-exchange tape. Availability and delay depend on the account's Alpaca
   market-data subscription.
2. Configure AXIOM's `.env` with the market-data key and secret. Do not paste
   them into chat or screenshots.
3. Set the US paper settings below. Limits are mandatory and have no hidden
   defaults in this mode; choose values appropriate for a disposable simulation
   account and verify the resulting config before running.
4. Restart AXIOM and check the status page. Confirm mode `PAPER`, broker
   `LOCAL_PAPER`, provider `alpaca`, feed `iex`/selected feed, connected,
   authenticated, subscribed, and fresh bar timestamps.
5. Start a paper session for a supported ticker and `1m`. Check the status,
   simulated orders, fills, positions, and risk events. Stop the session when
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
PAPER_MAX_POSITION_VALUE=1000
PAPER_MAX_OPEN_POSITIONS=2
PAPER_MAX_DAILY_LOSS=100
PAPER_MAX_DRAWDOWN=0.05
PAPER_MAX_TRADES_PER_DAY=4
```

The numbers above are examples for configuring bounded simulation risk, not
personalized sizing advice. AXIOM refuses the Alpaca paper source unless all
five limits are explicitly bounded. `PAPER_MAX_DRAWDOWN` is a fraction, so
`0.05` means a 5% simulated peak-equity drawdown stop. Once the daily-loss
limit is reached, new entries remain blocked through that New York trading day.
Once the drawdown cap is reached, new entries remain blocked until the paper
account is reset/reviewed. Exits and broker-side order management are not
blocked by these entry controls.

## Calendar and data behavior

- Session timestamps are interpreted in `America/New_York`, so daylight-saving
  changes are handled by the IANA timezone database.
- The regular session is 09:30–16:00 New York time, with scheduled 13:00 early
  closes, weekends, and standard US equity holidays.
- The offline holiday rules cover normal NYSE sessions; extraordinary exchange
  closures require an exchange notice and are not automatically discovered.
- Current streaming supports a configured list of plain US tickers and
  completed 1-minute bars only. Other timeframes are refused rather than
  silently resampled. AXIOM evaluates the selected single-symbol strategy
  independently on each subscribed symbol; this is not a relative-strength
  ranking portfolio.
- Invalid OHLCV, duplicate/conflicting bars, future bars, stale bars over two
  minutes old, and out-of-order bars fail closed. A gap pauses new signals under
  the existing paper simulator's data-gap behavior.
- Alpaca `updatedBars` are late-trade corrections to a prior minute. AXIOM
  ignores corrections after the original bar has been emitted; review data
  quality when comparing the paper log against later corrected history.
- `AlpacaHistoricalProvider` is a read-only, paginated historical-bars adapter
  requesting raw (unadjusted) bars for research tooling. Split/dividend cash
  flows are not applied by this adapter, so a research pipeline must model
  corporate actions before treating long-horizon returns as comparable. The
  adapter does not establish that a dataset is survivorship-bias-aware or
  point-in-time correct.

## Research evidence and limits

The repository's generic daily `DEMO`/`ALT` sample CSVs are not US equity
history and must not be used to claim US strategy performance. A valid strategy
selection still requires a documented point-in-time universe, corporate-action
policy, date-bounded historical data, chronological train/validation/untouched
test splits, walk-forward evaluation, multiple regimes, and conservative cost
stress tests against buy-and-hold and momentum baselines. Do not select a
"best" strategy from a single optimization window. No profitability claim is
made by this paper integration.

The app's independent live-trading controls remain separate. This US path is
paper-only by configuration and uses `LOCAL_PAPER`; do not change the four live
switches to run a US paper session.

## Chart terminal

Open `/chart` and pick a ticker from the symbol list, which is populated from
`ALPACA_SYMBOLS` even before the first bar arrives. The chart draws the bars the
paper engine actually processed, so a US chart is the read-only Alpaca stream
you subscribed to, drawn locally; the fills on it are simulated. `DEMO` and
other CSV previews remain available and are labelled
`DEMO REPLAY SAMPLE DATA · NOT MARKET DATA` — never compare strategy results
from those bars with a market-data session.

The header's session badge comes from the New York calendar, not from the
browser clock, so `CLOSED` after 16:00 (or 13:00 on an early close) explains a
quiet feed instead of looking like a fault. Bars outside the session are
dropped before they reach the engine and never appear on the chart.

Changing the paper limits is a change to the account's guard rails: an existing
paper database is refused when its saved risk limits differ, so use a new
`DATABASE_URL` when you change `PAPER_MAX_*` for a US account.
