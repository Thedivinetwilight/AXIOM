# Terminal performance measurement and load tests

## What is measured

AXIOM does not collapse feed, rendering, submission and broker confirmation into
one "latency" number. The authenticated `/api/trading/performance` endpoint
returns bounded, payload-free server observations. The Settings page combines
those with bounded browser observations.

| Metric | Boundary |
|---|---|
| `vantage.feed.gateway_receipt` | Local duration of `symbol_info_tick`; not one-way network latency. |
| `vantage.feed.observed_source_age` | Broker source timestamp to local observation; includes quote age and clock skew. |
| `market_data.feed.*` | Same distinctions for the independent read-only DEMO data interface. |
| `vantage.chart.backend` / `market_data.chart.backend` | Server chart acquisition/handler work. |
| `browser.chart.load` | Browser fetch and response handling for a chart request. |
| `browser.chart.render` | JavaScript canvas rendering duration in the current tab. |
| `vantage.order.review_api` | Read-only review, reconciliation, contract/risk checks and MT5 precheck. |
| `vantage.order.submit_api` | Full reviewed submission API, including one broker attempt and reconciliation where possible. |
| `vantage.broker.submission_ack` | One `order_send` call; an acknowledgement is not a fill confirmation. |
| `vantage.reconciliation` | Broker account/order/position/history read plus local intent reconciliation. |
| `browser.order.submit_roundtrip` | Browser submission request/response, distinct from the server and broker measurements. |

Server samples retain the newest 512 per metric with at most 64 metric names;
browser samples retain the newest 128. Total observation counts and recent-window counts are distinct.
Empty means **not measured**, never zero. Invalid durations are ignored and
metrics never participate in an order decision. No payload, symbol, credential,
full account login, or API token is accepted by the latency registry.

## Reproduce the local fake-provider load test

Use the pinned environment from `requirements-lock.txt` and run from the
repository root:

```bash
python scripts/benchmark_terminal.py --output docs/TERMINAL_BENCHMARK.json
npm install --no-save --no-package-lock jsdom@27.0.0
AXIOM_UI_BENCHMARK_OUTPUT=docs/TERMINAL_UI_BENCHMARK.json node tests/js/terminal_smoke.mjs
```

Defaults are 4 read workers × 60 requests per worker (240 reads: 120 chart and
120 quote), 12 sequential reviewed fake-DEMO submissions, and 6 explicit
reconciliations. Setup and warm-up observations are cleared before measurement.
The UI run performs the complete functional workflow plus 120 additional chart
renders. Both scripts assert expected order-call counts and use no external
network, real terminal, real DEMO account, or LIVE account.

## Development run — 2026-10-25

These are actual measurements from the checked-in JSON reports, not targets.
They were made at development commit
`7c7de22abdf98cef47af53a02f8072342b9e7331` with the working tree dirty; they are
**not** the final exact-commit release evidence. The Windows release separately
regenerates both JSON reports from its exact clean tested commit; use those release
assets and `BUILD_METADATA.json` instead of this development table for that build.

### Backend / HTTP conditions

Linux x86_64, Python 3.11.2, two logical Intel Xeon CPUs, in-memory SQLite,
`FakeMT5Gateway`, four concurrent read workers, sequential reviewed writes. The
fake gateway adds no broker, network, terminal, or market delay.

| HTTP observation | n | p50 ms | p95 ms | p99 ms | max ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Chart update | 120 | 28.769 | 39.572 | 45.635 | 55.624 |
| Feed quotes | 120 | 27.668 | 39.245 | 46.052 | 50.120 |
| Read-only order review | 12 | 3.781 | 4.455 | 4.622 | 4.663 |
| Full reviewed submit | 12 | 5.211 | 7.166 | 7.672 | 7.799 |
| Explicit reconcile | 6 | 3.799 | 4.324 | 4.394 | 4.412 |

Separately instrumented server percentiles from the same run:

| Server metric | n | p50 ms | p95 ms | p99 ms |
| --- | ---: | ---: | ---: | ---: |
| Chart backend | 120 | 2.969 | 11.239 | 15.160 |
| Feed gateway receipt | 264 | 0.009 | 0.016 | 0.031 |
| Observed quote source age | 264 | 0.504 | 0.951 | 1.005 |
| Review API | 12 | 1.257 | 1.667 | 1.685 |
| Full submit API | 12 | 2.199 | 3.613 | 3.912 |
| Fake `order_send` acknowledgement | 12 | 0.026 | 0.033 | 0.034 |
| Reconciliation | 42 | 0.749 | 1.481 | 1.537 |

The sub-millisecond fake acknowledgement is only an in-process function call.
It says nothing about Vantage, MT5, a network, a real demo, or live execution.
All 12 fake submissions reconciled as FILLED and exactly 12 `order_send` calls
occurred.

### UI conditions

Node 22.22.3 on the same two-CPU Linux environment, jsdom and a local
command-counting fake 2D canvas. This measures JavaScript and fake canvas
commands, **not** GPU/raster time, browser frame rate, or human-perceived delay.

| Browser metric | n | p50 ms | p95 ms | p99 ms | max ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Chart load/response handling | 6 | 0.186 | 0.368 | 0.400 | 0.408 |
| Chart canvas command render | 128 | 0.891 | 1.310 | 2.098 | 2.948 |
| Paper submit round trip | 2 | 0.107 | 0.122 | 0.123 | 0.123 |
| Vantage fake submit round trip | 2 | 0.305 | 0.479 | 0.494 | 0.498 |

The functional harness also passed with 85 authenticated fake API calls and no
unexpected order submissions. The terminal's Settings panel is the way to
collect real local browser render observations; it still does not measure GPU
presentation latency.

## Interpretation limits

- Percentiles describe only the stated run and recent sample windows. They are
  not universal values, zero-latency claims, or service-level objectives.
- Read concurrency and sequential reviewed writes do not model every operator,
  terminal, account, market, or network condition.
- Real DEMO verification must be run separately by the owner on Windows with an
  actual terminal/account. LIVE remains unverified unless the owner performs
  and documents the separate local readiness process.
- Tests, benchmarks, paper fills and demo results establish neither
  profitability nor live readiness.
