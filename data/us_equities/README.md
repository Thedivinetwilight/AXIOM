# `data/us_equities` — research dataset drop point

**There is no dataset here. Nothing in this directory is a US equity history.**

`tools/us_equities_research.py` refuses to evaluate anything until every file
below exists and passes the contract check. It will not fall back to the generic
`DEMO`/`ALT` sample CSVs (they are synthetic daily series, not US equities) and
it will not generate prices. With the directory empty or incomplete it exits `2`,
writes `DATA_REQUIRED.json` into the output directory, and prints exactly what
is missing.

## Files the loader requires

| File | Purpose |
| --- | --- |
| `manifest.json` | Provenance, licence, adjustment policy, universe declaration |
| `bars.csv` | `symbol,timestamp,open,high,low,close,volume` |
| `universe.csv` | `symbol,start,end` point-in-time membership windows |
| `corporate_actions.csv` | `symbol,ex_date,type,ratio,amount` — required when `adjustment` is `raw` |

`manifest.example.json` in this directory is a **template with placeholder
values**. It is deliberately not named `manifest.json` so the loader cannot pick
it up by accident. Copy it, replace every placeholder with values you can
actually defend, and keep the file next to the CSVs.

## Rules the loader enforces

- `schema` must be `axiom.us.equities.dataset/1`.
- `source.name`, `source.license`, `source.url` and `source.retrieved_at` must be
  present: an unattributed price file is not evidence.
- `bar_timeframe` must be `1D` and `bar_timestamp_convention` must be
  `session_close`. Each bar must be stamped within five minutes of the real New
  York close for its date, early closes included. An open-stamped bar used as a
  close is look-ahead and is refused.
- `bar_symbols` are validated as plain US tickers.
- OHLC consistency and non-negative volume are checked per bar.
- A symbol's membership window must be present for every session it trades, so
  survivorship bias is visible in the data rather than assumed away.
- `universe.kind` must be `point_in_time`. A static list of today's index
  members back-applied over history (`kind: static_survivors`) is accepted only
  as a labelled limitation, and forces the verdict to `UNRESOLVED`.

## Where to get data that satisfies this

Any vendor whose licence lets you store and use the bars for research, for
example:

- Alpaca historical bars on a paid feed (`sip`), with `adjustment` recorded;
  the free `iex` feed covers only the IEX venue and is not a consolidated tape.
- A vendor that supplies delisted symbols and index membership history
  (CRSP, Sharadar, Norgate, Polygon with delisted coverage).

You must be able to describe, in the manifest, how splits and dividends are
applied, whether the universe is point-in-time, and what the timestamp on each
bar means. If you cannot, the honest state is "no dataset", which is the state
this repository ships in.

## What the harness does once the data is present

`docs/US_EQUITIES_PAPER_OPERATOR_MANUAL.md` §10 documents the method:
chronological train/validation/untouched-test split, walk-forward folds, regime
tagging, long-only construction, commission/spread/slippage/participation cost
model with a 3x stress case, buy-and-hold and momentum baselines, and the full
metric set. Selection uses net validation Sharpe only; the test split is read
once. A `SUPPORTED` verdict still reports `paper_ready: false`.
