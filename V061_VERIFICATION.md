# V061_VERIFICATION.md — V0.6.1 Real Market-Data Integration

**Scope of V0.6.1:** connect the existing, unchanged paper-trading pipeline to a
**real external market-DATA provider** (Upstox Market Stream Feed V3). Only
market *data* is real. Execution remains 100% simulated in the existing
`PaperBroker`. No real-money order routing was added; none exists.

```
REAL EXTERNAL MARKET DATA (Upstox V3 WSS, read-only)
        ↓  authorize (HTTPS GET, Bearer token) → single-use wss:// URL
UpstoxStream adapter  (app/data/upstox_stream.py — NEW)
        ↓  binary JSON subscribe → protobuf frames decoded with the OFFICIAL schema
        ↓  (app/data/upstox_proto/MarketDataFeed.proto, byte-identical, sha256 3e1c939d…)
Data validation + completed-candle builder  (app/data/candles.py — NEW)
        ↓  completed candles ONLY
EXISTING strategy engine → EXISTING risk engine → EXISTING PaperBroker
        ↓
SQLite → FastAPI (/health, /market/status, /paper/*)
```

`MODE=PAPER` and `LIVE_TRADING=false` remain hard-enforced (`Settings.__post_init__`
raises on anything else — re-proven during this work). Upstox **order** endpoints
are never imported, referenced, or called anywhere in the project.

---

## 1. Verified (all actually executed in this environment)

| # | Item | Result |
|---|------|--------|
| 1 | Existing test suite | **75/75 passed** (unchanged code paths) |
| 2 | New V0.6.1 integration tests | **18/18 passed** (`tests/test_upstox_live.py`) |
| 3 | Total | **93 passed, 0 failed**, 1 warning (pre-existing upstream Starlette→AnyIO `BlockingPortal` deprecation, not ours to fix) |
| 4 | Clean install | fresh venv → `pip install -r requirements.txt` OK, `pip check` clean; **lock file validated**: separate venv from `requirements-lock.txt` → install OK, `pip check` clean, official protobuf bindings import and round-trip |
| 5 | Provider | Upstox Market Stream Feed **V3** — official docs (authorize V3, market-data-feed V3) and official `MarketDataFeed.proto` (fetched 2026-09-21, sha256 `3e1c939dce2c83a3fef91405c5ca268dfa089aae973319f56685f12a5afcf612`, kept byte-identical) |
| 6 | External endpoint reachability | `GET https://api.upstox.com/v3/feed/market-data-feed/authorize` with **no credentials** → **HTTP 401 in 474 ms**, body `{"errors", "status"}` — confirms the documented URL/protocol shape. **This is NOT an authenticated test** (no credentials exist here) |
| 7 | Authenticated connection / subscription / live messages | **Verified against a real loopback protocol fixture only**: real TCP, real TLS (temporary self-signed cert trusted via `SSL_CERT_FILE`), real HTTP authorize exchange (Bearer header asserted on the wire), binary subscription frame asserted (`method:"sub"`, `mode`, `instrumentKeys`, `guid`), protobuf frames built/decoded with the official schema. **Zero external network calls; the token used is a synthetic dummy** |
| 8 | Instrument / timeframe / mode tested | `NSE_EQ|INE002A01018` (internal symbol `NSE_EQ-INE002A01018`; ISIN of Reliance Industries), **1m**, `ltpc` mode — one liquid instrument, as required |
| 9 | Completed-candle rule | ticks only form candles; a candle is emitted **only after** its window closes or a newer bar supersedes it; incomplete candles never reach the strategy (unit + pipeline + controller-guard tests); identical semantics to backtest/replay |
| 10 | Paper execution through existing pipeline | real `run.py --mode paper --source upstox` process: **6 completed candles → momentum signal → exactly 1 BUY order → FILLED once** (qty 247.25270032907548), equity `100219.6394074926` with default fees/slippage — same `ExecutionConfig`, accounting, order lifecycle as backtest |
| 11 | Reconnect | forced disconnect mid-stream over real sockets → bounded exponential backoff → **fresh authorize (Bearer re-sent over real HTTP) → resubscribe** (2nd binary sub frame asserted) → stream resumes; heartbeat: idle ping/pong cycle survived without reconnect; reconnects counted in status |
| 12 | Duplicate-order protection | duplicate tick replay after reconnect → `duplicates` counter, **no second candle, no second order**; duplicate candle at the committed cursor (incl. after restart) → ignored; conflicting duplicate or out-of-order candle → `ValueError`, account unchanged |
| 13 | Restart | second real CLI process on the same SQLite DB: account/positions/orders restored, replayed last candle suppressed, **order count stayed exactly 1**, continuation candles processed, equity `100466.89210782168` matches the analytic mark-to-market expectation `100466.892108` (delta < 1e-6) |
| 14 | Data-quality protection | malformed protobuf frames counted, stream continues; invalid price/volume/timestamp rejected (`malformed`, `invalid_timestamps` counters); missing candle → **gap detected → new orders PAUSED, pending cancelled, gap flagged** in `/market/status`; stale feed flagged; older-than-cursor replay after restart fails closed (documented, pinned by test) |
| 15 | Health monitoring | `/market/status` (auth-gated) reports `connection_status`, `authenticated`, `subscribed`, `last_message_time`, `last_candle_time`, `data_gap_detected`, `stale_feed`, `paper_mode`, `reconnects`, `feed_counters`, `feed_latency_ms`, `instrument_keys`; `/health` reports `version 0.6.1`, `mode PAPER`, `live_trading false`, `real_order_routing: NOT_IMPLEMENTED`. **No token or secret in any status/repr/log** (asserted by tests and grep) |
| 16 | Latency (local machine, NOT exchange latency) | stage timestamps recorded per bar. Real-process run: `avg_total_ms 0.166` (max `0.246`) for validate→portfolio→strategy→paper-order per candle; strategy `0.135` ms, order `0.032` ms avg; feed decode→candle latency tracked in `feed_latency_ms`. Normal-process Python numbers only; **not** an HFT claim |
| 17 | Credentials | never in source (only `.env` via `MARKET_DATA_ACCESS_TOKEN`, `repr=False`), never logged (controller logs exception *types* only), never in API responses, never in URLs we construct (the provider's single-use `wss://…code=…` URL is used in-memory and never logged); repo grep + artifact grep clean; `.env` gitignored and absent |
| 18 | Safety audit | greps for `place_order` / `orders` / `broker` / `LIVE_TRADING` / `real` / `production`: the only order call-sites are the PaperBroker/simulator (simulation) and the non-instantiable `FutureRealBroker` stub; new data modules contain **no order/broker logic**; only network egress is the read-only authorize GET + WSS feed; unsafe env (`MODE=LIVE`, `LIVE_TRADING=true`) still rejected at runtime |

### Real-process end-to-end evidence (local protocol conformance)

Driver: `/home/user/verification_v061_live/e2e_cli_local_fixture.py`
Evidence: `e2e_summary.json`, `cli_phase1.log`, `cli_phase2.log`

- Phase 1 — fresh DB, real CLI process, real TLS WSS + real HTTP authorize on
  loopback: exit 0, `STOPPED`, 6 candles, 1 BUY FILLED, equity exact.
- Phase 2 — restart on same DB, fixture resends the last committed tick
  (suppressed: `duplicates:1`) and two fresh candles: exit 0, `STOPPED`,
  processed 2, **still exactly 1 order**, equity matches analytic expectation.
- 3 authorize requests observed across runs (per-connection re-authentication).

---

## 2. Exact commands executed

```bash
# Environment (sandbox: Debian 13, x86_64, CPython 3.13.14 — NOT Android/Termux)
python3 --version
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt "grpcio-tools~=1.68.0"   # tools only for binding generation
.venv/bin/python -m pip check

# Official provider schema (fetched, byte-identical, committed with provenance)
curl -sL https://assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto \
  -o app/data/upstox_proto/MarketDataFeed.proto
sha256sum app/data/upstox_proto/MarketDataFeed.proto
.venv/bin/python -m grpc_tools.protoc --proto_path=app/data/upstox_proto \
  --python_out=app/data/upstox_proto app/data/upstox_proto/MarketDataFeed.proto

# Tests (iterating during development, then final)
.venv/bin/python -m pytest tests/test_upstox_live.py -v
.venv/bin/python -m pytest -q                       # 93 passed, 1 warning
.venv/bin/python -m pip check

# Real-process end-to-end (two actual `run.py --mode paper --source upstox`
# subprocesses against a local documented-protocol fixture; SIGTERM shutdown)
openssl req -x509 -newkey rsa:2048 -keyout tls_key.pem -out tls_cert.pem -days 2 \
  -nodes -subj "/CN=127.0.0.1" -addext "subjectAltName=IP:127.0.0.1"
.venv/bin/python /home/user/verification_v061_live/e2e_cli_local_fixture.py

# External reachability probe (NO credentials sent; expects 401)
.venv/bin/python -c "import httpx; print(httpx.get('https://api.upstox.com/v3/feed/market-data-feed/authorize', headers={'Accept':'application/json'}, timeout=15).status_code)"

# Lock-file validation (separate throwaway venv)
python3 -m venv /tmp/lockcheck
/tmp/lockcheck/bin/pip install -q -r requirements-lock.txt
/tmp/lockcheck/bin/pip check

# Safety audit greps + runtime enforcement proofs
grep -RIn "place_order\|\.cancel_order\|Order(" app/ --include="*.py"
LIVE_TRADING=true .venv/bin/python -c "from app.config.settings import Settings; Settings.from_env()"   # ValueError
MODE=LIVE .venv/bin/python -c "from app.config.settings import Settings; Settings.from_env()"           # ValueError
```

Driver orchestration details (fixture plans, SIGTERM timing, SQLite cursor
polling) are in the driver source; it imports only the project's own test
helpers and speaks the documented Upstox wire format on loopback.

---

## 3. Not verified (honestly unavailable here)

- **Android/Termux execution** — this sandbox is Debian glibc x86_64, not
  Android/Bionic. No Termux, adb, device, or ARM runtime was available. The
  added dependency (`protobuf`) ships pure-Python fallbacks, but nothing about
  Termux was proven by execution.
- **Authenticated external feed** — no Upstox account/token exists in this
  environment. Every "live" execution above is a **local protocol conformance
  run**, clearly labeled; the ONLY external observation is the unauthenticated
  401 reachability probe. Connection, subscription, and candle decoding against
  the real Upstox cloud remain **untested until someone supplies their own
  token** and runs `python run.py --mode paper --source upstox`.
- **Epoch units against the real feed** — provider int64 timestamps are
  normalized by magnitude (`>1e11` → ms), documented in code; must be confirmed
  on a real authenticated session.
- **`full` mode official 1m/30m bars against live data** — implemented per the
  official proto/docs and tested against the protocol fixture only.
- **Exchange-side behavior** — market hours/pre-open snapshots, auction session
  fields, rate limits, token expiry/refresh (Upstox tokens are typically
  daily), server-initiated disconnect patterns, order-status market info
  frames: unobserved.
- **Real order execution** — not implemented, therefore not testable. This is
  by design and is listed here for completeness.
- Long soak/7-day stability, multi-instrument scale (only 1 instrument tested,
  as required), other platforms/interpreters (only CPython 3.13.14 here).

## 4. Limitations (honest)

- No profitability claim of any kind. The pipeline successfully *processing*
  live-shaped data says **nothing** about whether the strategies make money.
  Strategy quality remains a research question (backtest → validation →
  unseen test → paper), and past/synthetic results never guarantee anything.
- `ltpc` candles sum observed trade sizes (`ltq`) for volume — an
  approximation of true bucket volume (missed/off-feed trades are absent).
- Tick-built candle completion uses wall clock + bucket rollover; the
  controller keeps its existing 2-second clock-skew tolerance.
- Keys containing spaces (index keys like `NSE_INDEX|Nifty 50`) match the
  provider pattern but cannot map into the project's symbol charset and are
  rejected with a clear error instead of being silently mangled.
- A provider that replays candles **older than the committed cursor** after a
  restart fails closed (`Out-of-order live candle`) — identical older candles
  at the cursor are ignored, conflicting ones raise. This is deliberate:
  unverifiable history must not silently re-enter the pipeline.
- Gap → PAUSED requires operator review/resume (or restart); there is no
  automatic backfill from Upstox (the provider's historical REST API is
  deliberately not wired in V0.6.1).
- Still-true platform limitations from V0.6: single paper writer per SQLite
  DB, growing full checkpoints, NSE holiday calendar incomplete, no partial
  fill microstructure, controller starts STOPPED after restart, etc.

## 5. Changed / added files (nothing was rebuilt)

**Added:** `app/data/upstox_stream.py`, `app/data/candles.py`,
`app/data/upstox_proto/{MarketDataFeed.proto,MarketDataFeed_pb2.py,README.md,__init__.py}`,
`tests/test_upstox_live.py`, `V061_VERIFICATION.md` (this file).
**Extended (additive only):** `app/config/settings.py` (6 config fields + upstox
validation), `app/data/factory.py` (`upstox` branch), `app/api/schemas.py`
(source literal), `app/api/routes/paper.py` (symbol/provider agreement check),
`app/paper_trading/simulator.py` (status fields + latency stages), `run.py`
(CLI source choice + provider symbols), `.env.example`, `requirements.txt`,
`requirements-lock.txt`, `README.md`, `app/api/server.py` + health version
0.6.0 → 0.6.1.
**Untouched:** all strategy, indicator, backtest, tournament, validation,
broker, portfolio, database core modules — verified by the 75 pre-existing tests
passing without modification.

## 6. Reproduce

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && cp .env.example .env   # fill your own token
python -m pytest -q
DATA_SOURCE=upstox MARKET_DATA_SYMBOLS=NSE_EQ|INE002A01018 \
MARKET_DATA_TIMEFRAME=1m DEFAULT_TIMEFRAME=1m \
MARKET_DATA_ACCESS_TOKEN=<your token> python run.py --mode paper --source upstox
```

Anything not listed under “Verified” should be assumed unverified.
Keep the system paper-only.
