# Research candidate generation — evidence, not a profit promise

AXIOM 3.6.0a1 repairs the local research candidate path. It does **not** create a
guaranteed profit generator, enable unattended trading, or authorize DEMO/LIVE
orders. Candidate output is historical research evidence with
`execution_authorized: false` and `profitability_claim: none`.

## Repaired behavior

- `python -m app discover` now uses the actual `StrategyDiscoveryEngine` API,
  supports configured execution costs and optional JSON report output.
- Discovery and parameter experiments process each instrument independently.
  Candles from different symbols are never combined in one backtest.
- `--periods 2,3,5` is parsed as positive integers, not characters.
- Chronological validation and locked-test runs receive only prior candles as
  indicator warmup. Warmup creates no inherited position or pre-evaluation order.
- Robustness subperiods likewise receive only history available before each
  subperiod.
- A baseline measurement is reference-only and is not promoted automatically.
  Promotion requires positive validation expectancy, strict improvement over the
  baseline, minimum trade evidence, robustness and drawdown checks. It does not
  bypass the separate execution lifecycle.
- Research Director seeds are SHA-256-derived and stable across processes.

## Discovery from local history

First obtain and store lawful historical data with the existing history tools.
Then run, for example:

```bash
python -m app discover \
  --instruments 'NSE_EQ|INE002A01018' \
  --unit days --interval 1 --timeframe 1D \
  --max-candidates 12 --output results/discovery.json
```

Multiple comma-separated instrument keys produce separate experiments. Each key
needs at least 60 local candles; insufficient keys are reported in `errors` and
cannot produce candidates. The JSON includes every result, rejection reasons,
OOS metrics, robustness evidence, qualified candidates and experiment IDs.

Parameter experiments use:

```bash
python -m app research run \
  --instruments 'NSE_EQ|INE002A01018' \
  --unit days --interval 1 --timeframe 1D --periods 2,3,5
```

## Chronological CSV validation

The existing validator remains available for an explicit CSV:

```bash
python run.py --mode validate \
  --csv data/sample.csv --symbol DEMO --timeframe 1D \
  --output results/validation-sample
```

The bundled CSV is **synthetic sample data**, not actual DEMO or exchange prices.
A positive result on it is only a software/research example. For walk-forward
research, freeze the grid and windows before examining results:

```bash
python run.py --mode walk-forward \
  --csv data/sample.csv --symbol DEMO --timeframe 1D \
  --windows 360 120 120 120 --output results/walk-forward-sample
```

Selection ranks training candidates, evaluates a shortlist on validation data,
freezes one candidate, and evaluates the locked test once for the final report.
Changing a strategy after viewing that test makes the period seen; reserve new
out-of-sample data rather than repeatedly tuning to the same holdout.

## Synthetic development smoke result

One local `run.py --mode validate` smoke run over the bundled synthetic CSV
completed with 960 training, 320 validation and 320 locked-test bars. It selected
`momentum` with `period=10` and `threshold=0.01`. The report-only locked-test leg
showed a simulated total return of 15.414%, maximum drawdown of 1.960%, 11 closed
trades and INR 692.90 modeled transaction costs on the synthetic INR 100,000
initial account. An open position remained marked at the final close rather than
being artificially liquidated. The run also emitted `Large training/test return
gap; sample lengths also affect this comparison`.

These numbers describe only that generated sample and its cost model. They are
not actual DEMO/exchange performance, a statistically validated edge, a forecast,
or a recommended allocation. No real-money or external DEMO order was placed.
The example must not be quoted without its synthetic-data and warning context.

## Qualification boundaries

A qualified result is not an executable strategy approval. It does not prove
future profitability, live readiness, market access, liquidity, slippage,
execution quality or suitability for an account. Backtests and paper fills are
simulations. Multiple testing, overfitting, survivorship bias, gaps and changing
market conditions can invalidate historical edge.

The generic OHLCV researcher uses configured proportional costs and paper
accounting. It does **not** establish Vantage/MT5 contract-specific lots, ticks,
margin, currency conversion, sessions, filling rules or CFD financing. Its output
is not a Vantage routing instruction and cannot be imported automatically into
the Vantage execution path. Listed shares/ETFs also retain their separate
provider and identity boundary; no CFD substitution is allowed.

No research command changes execution mode, Arm/Activate, automation, emergency
stop or broker credentials. Vantage automation remains OFF. Owner-run local DEMO
verification and every existing readiness/review/risk/reconciliation gate remain
separate requirements; no agent or CI process performs LIVE activity.
