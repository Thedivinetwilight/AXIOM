# Listed shares and ETFs: provider boundary and setup

## Current product boundary

AXIOM's Vantage integration is an MT5/CFD integration. A symbol such as a share
CFD or ETF CFD does **not** represent ownership of the underlying listed share or
ETF. A ticker alone is not a portable instrument identity.

This build includes the `listed-assets-v1` safety interface and authenticated,
read-only status/policy APIs. It does **not** include a verified listed execution
adapter or an entitled listed reference/quote provider. Consequently:

- listed data is unconfigured and fails closed at this boundary;
- listed execution is unsupported and cannot be enabled by configuration;
- MT5 routing and CFD substitution are always rejected by this boundary;
- no order, review, intent, connection, Arm or Activate action is created by a
  routing-policy request;
- AXIOM local paper remains a simulation, not owned-share execution.

The existing Alpaca integration is separate legacy **read-only US market data
for local paper fills**. It is not an Alpaca trading client and does not change
this listed-execution boundary.

## Required identity

A future listed provider must preserve all of these fields end to end:

```text
symbol + explicit exchange/MIC + currency + LISTED_SHARE or LISTED_ETF
```

Example policy identity (not a claim that a data entitlement exists):

```json
{
  "symbol": "AAPL",
  "exchange": "NASDAQ",
  "currency": "USD",
  "product_type": "LISTED_SHARE"
}
```

The canonical key is `LISTED:NASDAQ:AAPL:USD`. Unknown products, missing
exchange/currency, CFD product types and provider responses with a mismatched
key are rejected. Do not infer a primary exchange from a ticker, company name,
website instrument list, or similarly named MT5 symbol.

## Verify the current fail-closed interface

Run the local server as described in `USER_MANUAL.md`, with `AXIOM_API_TOKEN` set
only in the owner's local shell. Do not paste the token or any provider secret
into chat, Git, screenshots, logs, or issue reports.

```bash
curl -fsS \
  -H "Authorization: Bearer ${AXIOM_API_TOKEN}" \
  http://127.0.0.1:8000/api/listed-assets/status
```

Expected current facts include `data_configured: false`,
`execution_supported: false`, `mt5_routing_allowed: false`, and
`cfd_substitution_allowed: false`.

A read-only policy check can be requested with:

```bash
curl -fsS \
  -H "Authorization: Bearer ${AXIOM_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"AAPL","exchange":"NASDAQ","currency":"USD","product_type":"LISTED_SHARE"}' \
  http://127.0.0.1:8000/api/listed-assets/routing-policy
```

The response must have `order_created: false`, `vantage_mt5_allowed: false`, and
`execution.allowed: false`. With no configured listed data provider,
`data.allowed` is also false. There is no `LISTED_PROVIDER` environment switch in
this build; do not invent one or point these routes at Vantage.

## Existing Alpaca read-only local-paper setup

This optional legacy configuration is only for market data feeding local paper.
Use a dedicated local `.env`, never a committed file. Values below are
placeholders, not credentials:

```dotenv
MODE=PAPER
LIVE_TRADING=false
DATA_SOURCE=alpaca
MARKET=us
MARKET_CALENDAR=us-equities
CURRENCY=USD
DEFAULT_TIMEFRAME=1m
ALPACA_API_KEY=replace-locally
ALPACA_API_SECRET=replace-locally
ALPACA_DATA_FEED=iex
ALPACA_SYMBOLS=AAPL,SPY
PAPER_MAX_POSITION_VALUE=10000
PAPER_MAX_OPEN_POSITIONS=5
PAPER_MAX_DAILY_LOSS=1000
PAPER_MAX_DRAWDOWN=0.10
PAPER_MAX_TRADES_PER_DAY=20
```

Use only the feed (`iex`, `sip`, or `delayed_sip`) for which the owner has an
actual entitlement. Label delayed data as delayed; do not treat a configured key
as proof of entitlement, fresh quotes, trading permission, or execution support.
The local paper ticket still requires review and produces simulated fills.

## Acceptance required before a future listed data adapter

1. Use an official provider API and a separate credential/entitlement profile;
   never scrape a terminal or reuse Vantage MT5 credentials.
2. Verify reference metadata for exact symbol, exchange/MIC, currency and asset
   class. Preserve provider instrument IDs where available.
3. Distinguish shares, ETFs, CFDs and unsupported derivatives in the provider
   model. Reject ambiguous or incomplete identities.
4. Validate timestamps, sequence, stale/future/crossed quotes, trading sessions,
   adjustments and delayed-data rights. Fail closed without replay substitution.
5. Return the same canonical `asset_key` with every provider response. Add
   mismatch, entitlement-loss, reconnect and restart tests.
6. Keep credentials out of browser responses, audit records, exports, fixtures
   and Git. Use an owner-approved protected local secret store.

## Acceptance required before any future listed execution adapter

A listed execution adapter is a separate project gate, not a configuration
change. It requires explicit owned-instrument account capability, quantity and
fractional-share rules, currency/FX handling, sessions/order types, bounded risk,
review/final confirmation, durable idempotency, ambiguous-outcome reconciliation,
persistent emergency stop, broker-confirmed states, negative CFD-routing tests,
and owner-run external demo/sandbox evidence where the provider supports it.
Until that implementation and evidence exist, `execution_supported` must remain
false.

Tests, market-data connectivity, paper fills and demo executions establish
neither profitability nor live readiness.
