# Provider conformance — Upstox & Alpaca market data (read-only)

`app/data/conformance.py` is a deterministic, **offline** conformance suite for
AXIOM's two market-data adapters and for their path into the local paper engine.
It exists so adapter regressions (parser drift, silent fixture substitution,
stale/zero prices becoming tradable, an adjusted research series leaking into
execution, one market's outage stalling the other) fail a test instead of
reaching an operator.

Nothing in this document is a claim about live provider connectivity,
production data quality, latency or profitability. Those stay `UNVERIFIED`
until an owner runs the opt-in live probe on their own machine.

## What is checked

| Capability | Upstox | Alpaca |
| --- | --- | --- |
| `instrument_resolution` | `NSE_EQ\|INE002A01018` ⇄ `NSE_EQ-INE002A01018` round trip; malformed keys and `NSE_INDEX\|Nifty 50` refused | — (plain US tickers validated as `[A-Z][A-Z0-9.-]{0,14}`) |
| `feed_identity` | — | `iex` / `sip` / `delayed_sip` labels; an IEX quote is never described as SIP and the request itself carries `feed=iex` |
| `candles` | `ltpc` tick frames and official `full` interval bars decoded with the shipped protobuf schema | stream `b` bar frames; `u` corrections and control frames ignored |
| `historical_bars` | — | paginated `/v2/stocks/bars`, exclusive end, ascending UTC, `adjustment=raw` |
| `freshness` | stale feed counters, gap counters (never filled), 30-minute-old quote refused | 45-minute-old quote refused |
| `error_handling` | 401/403/429/500, transport timeout, malformed protobuf, zero/negative price, missing `last_price` | 401/403/429/500, timeout, truncated bar, missing symbol, inverted/non-finite quote, repeated page token |
| `paper_path` | normalized quote → `LiveMarketPaperEngine.analyze_market` → `SUCCEEDED`/`FRESH`, `broker_order_api_calls: 0` | same |
| `cross_market_isolation` | a raising Upstox quote provider and a healthy Alpaca provider advance in the same cycle; NASDAQ still produces its decision | same |
| `corporate_actions` | — | dataset import keeps `status`/`provider`/`actions_sha256`; the derived `SPLIT_ADJUSTED` series is refused at the execution boundary; the stored dataset stays raw |

### Behaviour that is asserted, not assumed

* **Timezones** — every normalized timestamp is UTC (`+00:00`), never naive.
* **Ordering** — duplicates and out-of-order ticks are counted and dropped; a
  completed candle never moves backwards (`regressions == 0`).
* **Gaps** — missing intervals are counted (`gaps`) and reported; no synthetic
  candle is ever created to fill them.
* **No substitution** — an empty `bars` object, a missing `last_price` or an
  unentitled feed raises; nothing falls back to samples, replay data or a
  synthesised price. `ALPACA_DATA_FEED` never silently becomes `sip`.
* **Retries** — none are implicit: a timeout produces exactly one recorded
  request, and the symbol is reported as failed rather than retried silently.
* **Zero order calls** — every HTTP double records its request; the suite fails
  if any recorded request is not a `GET` on a documented market-data path, and
  `LiveMarketPaperEngine.broker_order_endpoints_reachable` is `False`.

## Machine-readable result

`python -m app provider-check --json` (or `run_conformance()`) returns:

```json
{
  "schema": "axiom.provider-conformance.v1",
  "suite_version": "1.1.0",
  "data_source_version": "axiom-synthetic-fixtures-2026-11-06",
  "generated_at": "2026-11-06T07:54:04+00:00",
  "status": "PASS",
  "network_calls": 0,
  "broker_order_api_calls": 0,
  "live_trading": false,
  "mode": "PAPER",
  "providers": {
    "upstox": {
      "provider": "upstox",
      "status": "PASS",
      "capabilities": {"candles": {"status": "PASS", "checks": [...]}, ...},
      "checks": [{"id": "upstox.tick_normalization", "capability": "candles",
                  "status": "PASS", "detail": "1 completed 1m candle(s) ... UTC=..."}],
      "limitations": ["..."],
      "requests_issued": 4,
      "request_methods": ["GET"],
      "request_paths": ["/v2/market-quote/quotes", "/v3/feed/market-data-feed/authorize"]
    },
    "alpaca": {"...": "..."}
  },
  "cross_market": {"status": "PASS", "checks": [...]},
  "read_only_requests": {"ok": true, "detail": "..."},
  "capability_summary": {"upstox": {"candles": "PASS", "...": "PASS"}},
  "limitations": ["Offline deterministic doubles: no Upstox or Alpaca endpoint was contacted, ..."],
  "unverified": [{"capability": "live_provider_connectivity", "reason": "..."}],
  "redaction": {"fields": ["access_token", "api_key", "api_secret", "token", "..."],
                 "secrets_found": false}
}
```

* `status` is `PASS`, `FAIL` or `UNVERIFIED`. Any failing check fails the
  provider; any `UNVERIFIED` capability leaves the overall status `UNVERIFIED`.
* `data_source_version` identifies the synthetic fixture set. Bump it whenever a
  fixture changes so a stored report is reproducible.
* `redaction.fields` lists the field names scrubbed from the payload;
  `secrets_found` is a self-check that no synthetic credential appears anywhere
  in the rendered report. The Upstox single-use `wss://…?code=…` authorize URL is
  never included at all.

## Operator command

```bash
# Offline conformance (default): no socket, no credential read, no file write
python -m app provider-check
python -m app provider-check --provider upstox
python -m app provider-check --json --output provider-conformance.json

# Explicit, opt-in network probe (owner machine, owner credentials)
python -m app provider-check --live --provider upstox
python -m app provider-check --live --provider alpaca --symbol AAPL --json
```

### Exact permissions

| Mode | Network | Reads credentials | Writes | Broker orders |
| --- | --- | --- | --- | --- |
| default (offline) | **none** | none | nothing, unless `--output PATH` is given | impossible |
| `--live` | one `GET` per selected provider: `api.upstox.com/v3/feed/market-data-feed/authorize` and/or `data.alpaca.markets/v2/stocks/{symbol}/snapshot` | `MARKET_DATA_ACCESS_TOKEN`, `ALPACA_API_KEY`, `ALPACA_API_SECRET`, `ALPACA_DATA_FEED` already in the environment | nothing | impossible |
| `--output PATH` | — | — | that one JSON file | impossible |

The live probe:

* performs **one attempt per provider** with a bounded timeout; there is no
  retry loop,
* never calls an order, position, holdings or account-mutation endpoint,
* never stores a token, never writes `.env`, never touches the DPAPI store, and
  prints only a verdict (`feed_ok` / `quote_ok` / `rejected` / `forbidden` /
  `unreachable` / `UNVERIFIED`),
* reports `UNVERIFIED` and contacts nothing when a credential is absent.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | `PASS` — every selected check passed (offline suite, or a successful live probe) |
| `1` | `FAIL` — at least one check failed |
| `3` | `UNVERIFIED` — nothing failed, but a required capability could not be checked (e.g. no credential for `--live`, or offline-only data) |

## Running the tests

```bash
python -m pytest -q tests/test_provider_conformance.py
```

The tests never open a socket. `test_live_probe_without_credentials_makes_no_network_call`
monkeypatches `httpx.Client`/`httpx.AsyncClient` to raise if constructed, which
proves the default path is genuinely network-free.

## Limitations (also embedded in every report)

1. All provider I/O is a deterministic double; **no Upstox or Alpaca endpoint is
   contacted**, so connectivity, entitlement and production data quality remain
   unverified.
2. Fixture prices, volumes and timestamps are synthetic: nothing here supports a
   profitability, slippage or liquidity claim.
3. WebSocket behaviour is checked at the decoder/candle-builder seam, not over a
   live socket; TLS pinning and venue latency are not exercised.
4. IEX conformance says nothing about SIP entitlement or consolidated-tape
   coverage.
5. Real credentials, real accounts and the Windows DPAPI credential store are
   never touched.

## Remaining real-provider checks (owner machine only)

1. `python -m app provider-check --live --provider upstox` with a daily access
   token — expect `feed_ok`.
2. `--live --provider alpaca --symbol AAPL` with market-data keys — expect
   `quote_ok` and an honest `feed` label (`iex` unless SIP is entitled).
3. A full market session of `python -m app paper-live --source upstox` for
   candle completion, reconnect and market-status suppression.
4. Native Windows package acceptance (installer/portable) — still **unverified**
   on this Linux machine; see `docs/PAPER_RELEASE_AUDIT.md` §11.5.
