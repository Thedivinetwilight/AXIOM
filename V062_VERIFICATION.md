# V062_VERIFICATION.md — V0.6.2 Real Upstox Market Data V3 Integration

> **Historical version report:** this V0.6.2 verification is retained for release history and does not describe the current AXIOM 3.2.0 source line. See [`AXIOM_CURRENT_AUDIT_REPORT.md`](AXIOM_CURRENT_AUDIT_REPORT.md).


**Scope:** production-hardened, real **market-DATA-only** Upstox V3 integration
inside the existing paper-trading platform. Architecture unchanged, extended
only. **Real-money order routing remains absent and disabled.** Docs verified
current on 2026-09-21/22 (Market Data Feed V3, Authorize V3, Authentication,
Sandbox, WebSocket implementation pages + official `MarketDataFeed.proto`,
sha256 `3e1c939d…`, byte-identical in `app/data/upstox_proto/`).

---

## 1. Master checklist (§25) — CHECK | STATUS | EVIDENCE

| CHECK | STATUS | EVIDENCE |
|---|---|---|
| Official documentation verified (V3, not V2) | VERIFIED | Pages fetched 2026-09-21/22; authorize = `GET /v3/feed/market-data-feed/authorize`; binary JSON sub frame `{"guid","method":"sub","data":{"mode","instrumentKeys"}}`; official proto shipped byte-identical with provenance README |
| Upstox authentication (real credentials) | **NOT VERIFIED** | No Upstox account/token exists in this environment. Nothing authenticated was claimed |
| OAuth authorization-code flow | VERIFIED (protocol-level, mock transport) + endpoint reachability | `app/data/upstox_auth.py` implements the CURRENT documented flow exactly (dialog `/v2/login/authorization/dialog?response_type=code…`; token exchange `POST /v2/login/authorization/token`, form-encoded `code/client_id/client_secret/redirect_uri/grant_type`); unit tests assert the exact wire form; real probes: dialog `HTTP 401`, token endpoint `HTTP 401 {"errors","status"}` with no credentials |
| Authorize endpoint reachability (live internet) | VERIFIED (unauthenticated) | `GET https://api.upstox.com/v3/feed/market-data-feed/authorize` → `HTTP 401 in 365 ms`, body `{"errors","status"}` |
| Upstox WebSocket (real, external) | **NOT VERIFIED** | Requires credentials. Local protocol conformance only (below) |
| Real market data (external) | **NOT VERIFIED** | Same. `verification_v061_live/real_upstox_check.py` run without token → exit 3, `performed:false`, fail-closed BEFORE any connection (`real_upstox_report.json`) |
| Protobuf decoding (official schema) | VERIFIED (local conformance) | 13 new tests; real loopback WSS frames decoded through `UpstoxProtobufDecoder`; malformed frames counted (`UPSTOX_DECODE_ERROR`), stream survives |
| Market-status handling | VERIFIED (local conformance) | `marketInfo` → normalized `market_info` event; gating: 0 candles before first status; 3+2 candles held while unknown/closed (`suppressed_by_market_status=5`); emission only after `NORMAL_OPEN`; `/market/status` shows `market_status`, `market_open`, raw segment map |
| Instrument key | VERIFIED against official instrument master | Downloaded official `complete.json.gz` (public, 3.4 MB): `NSE_EQ|INE002A01018` = **RELIANCE INDUSTRIES LTD** (NSE_EQ, EQ); also confirms `NSE_INDEX|Nifty 50` exists (still rejected as internal symbol by design — spaces) |
| Tick normalization | VERIFIED (local conformance) | `MarketEvent` with provider/instrument_key/kind/exchange_ts/received_at/ltp/close_price/ltq/bid/ask; absent fields stay None (never fabricated) |
| Candle builder (1m) | VERIFIED (local conformance + real-process) | ticks → completed 1m candles only; e2e: 6 candles from 7 real-socket ticks |
| Completed-candle rule / no look-ahead | VERIFIED | Incomplete candles never reach the strategy (controller guard + builder window semantics identical to backtest); signal on candle N executes at N+1 per existing broker model (unchanged, 75 original tests) |
| Strategy → risk → PaperBroker pipeline | VERIFIED (real-process) | Real `run.py --mode paper --source upstox`: 4 actionable momentum signals → 1 BUY order created, 3 risk-skips (`risk_counters`), FILLED once, equity `100219.6394074926` exact |
| Paper fill recorded + SQLite persistence | VERIFIED | Same run: fill persisted; restart on same DB → account restored, equity `100466.89210782168` matches analytic expectation (Δ < 1e-6) |
| Duplicate-order protection | VERIFIED | Post-restart duplicate tick suppressed (`duplicates:1`); order count stayed exactly 1; conflicting/out-of-order candles fail closed (pinned tests) |
| Reconnect / resubscribe / re-auth | VERIFIED (local conformance) | Forced disconnect over real TLS sockets → `UPSTOX_RECONNECT` → fresh authorize (Bearer on real HTTP) → `UPSTOX_RESUBSCRIBE` → `SYSTEM_RECOVERY`; bounded exponential backoff, no tight loop |
| Heartbeat | VERIFIED (local conformance) | Idle ping/pong cycle with no reconnect (library pong + explicit idle ping) |
| Rejected signals / risk decisions | VERIFIED | `risk_counters {actionable_signals:4, orders_created:1, skipped:3}` + `RISK_DECISION`/`PAPER_REJECT` structured events in real-process logs |
| Structured logging matrix (§19) | VERIFIED | All 17 event names emitted and observed in real-process JSONL logs (`cli_phase1.log`); `UPSTOX_TICK`/`CANDLE_COMPLETED` at DEBUG; credentials never logged |
| Health endpoints (§20) | VERIFIED | `/market/status`: connection_status, authenticated, subscribed, last_message_time, last_candle_time, market_status, market_open, segment_statuses, last_tick, reconnects, suppressed_by_market_status, connect_latency_ms, feed counters/latency; `/paper/status`: strategy, parameters, positions via `/paper/*`, `risk_counters`, processing latency; no credentials anywhere (asserted by tests) |
| CLI (§21) | VERIFIED | `python -m app upstox-test / market-stream / paper-live / upstox-login` implemented on the existing engine; `upstox-test` without token exits 2 with a safe message (tested) |
| Secrets / security audit (§23) | VERIFIED CLEAN | Repo-wide greps: token/secret references are plumbing only; zero secret-shaped literals; dummy tokens confined to test sources; `.env` absent + gitignored; logs/results/artifacts clean; `repr()`/status leak asserts in tests; OAuth errors never echo secrets (tested) |
| Sandbox (§13) | INVESTIGATED — **Market Data Feed V3 is NOT sandbox-enabled** | Current sandbox docs list sandbox-enabled APIs: Place/Modify/Cancel Order (V2/V3), Place Multi Order — order APIs only. No sandbox market-data path exists; none was faked |
| Real-money trading | **DISABLED** (does not exist) | No order endpoint anywhere; only PaperBroker executes; `Settings` rejects `MODE≠PAPER`/`LIVE_TRADING=true` (runtime-proven); safety grep: sole external calls are the read-only authorize GET + WSS feed |
| Android/Termux (§26) | **NOT VERIFIED IN CURRENT ENVIRONMENT** | Debian glibc x86_64 sandbox; no Android/Termux/ARM runtime. `protobuf` dependency is pure-Python at runtime, but nothing Termux was proven by execution |
| Performance (§27) | MEASURED (local, non-HFT) | Real-process: connect (authorize+TLS+WS open) 58.1 ms loopback; decode+normalize per frame tracked in `feed_latency_ms`; candle→signal→paper-order avg 0.17 ms/bar (max 0.25); strategy 0.14 ms, order 0.03 ms. Normal Python-process numbers; no exchange-latency claims |

**Mock/simulated vs real:** every “VERIFIED” above except the three live-internet
probes (authorize 401, dialog 401, token 401, instrument master) is a **local
protocol-conformance** result: real loopback TCP/TLS sockets speaking the
documented Upstox wire format, decoded with the official schema — explicitly
NOT external Upstox proof. **No authenticated external test was possible** (no
credentials); nothing was fabricated.

---

## 2. Deliverables (§30)

1. **Files created:** `app/data/market_events.py`, `app/data/upstox_auth.py`,
   `app/__main__.py`, `tests/test_upstox_v062.py`,
   `verification_v061_live/real_upstox_check.py`, this report.
2. **Files modified (additive):** `app/data/upstox_stream.py`
   (`UpstoxProtobufDecoder`, gating, logging, health, `UpstoxMarketDataProvider`
   alias), `app/data/market_events.py` (normalized `MarketEvent`),
   `app/paper_trading/simulator.py` (risk counters, structured events, status
   passthrough), `app/config/settings.py` (`UPSTOX_*` aliases),
   `app/api/server.py` + `app/api/routes/health.py` (version 0.6.2),
   `tests/test_upstox_live.py` (fixtures now send market-info frames; version
   assert; decoder counter), `.env.example`, `README.md`.
3. **Architecture changes:** none structural — new adapter capabilities behind
   the existing `WebSocketProvider` interface; normalized event layer between
   decoder and candle builder; strategy engine still does not know Upstox exists.
4. **Upstox API version:** Market Data Feed **V3** (+ Authorize V3; OAuth on the
   documented `/v2/login/authorization` endpoints).
5. **Authentication:** direct Bearer token from env
   (`MARKET_DATA_ACCESS_TOKEN` or `UPSTOX_ACCESS_TOKEN`); optional official
   OAuth 2.0 authorization-code helper (`python -m app upstox-login`) storing
   only into gitignored `.env` (chmod 600).
6. **WebSocket:** authorized wss via single-use redirect URI; binary JSON
   subscribe (`sub`/`ltpc|full`); verified-default TLS for wss; ping/pong
   heartbeat + idle pings; bounded exponential backoff reconnect with fresh
   auth + resubscribe.
7. **Protobuf:** official `MarketDataFeed.proto` (sha256 `3e1c939d…`),
   generated `MarketDataFeed_pb2.py`, runtime `protobuf>=5.29,<7`;
   `UpstoxProtobufDecoder` → `MarketEvent`s.
8. **Instruments tested:** `NSE_EQ|INE002A01018` (Reliance Industries —
   verified in the official instrument master), timeframe `1m`, mode `ltpc`.
   One instrument, as required.
9. **Real external tests performed:** unauthenticated reachability probes only
   (authorize 401/365 ms; dialog 401; token endpoint 401) + official
   instrument-master verification. **No authenticated external session.**
10. **Mock/local tests:** 13 new deterministic tests (`tests/test_upstox_v062.py`)
    + 18 updated V0.6.1 tests over real loopback sockets.
11. **Test count:** **106 passed, 0 failed** (75 original + 31 Upstox), 1
    pre-existing upstream warning.
12. **Failures:** none outstanding. (Two timing-flaky test drafts were fixed by
    using deep-past buckets; the controller's future-candle guard caused one —
    the safety behavior itself is correct.)
13. **Performance measurements:** see checklist row 22; per-stage latencies
    exposed at runtime (`feed_latency_ms`, `connect_latency_ms`,
    `processing_latency_ms`).
14. **Reconnect test results:** disconnect→backoff→re-auth→resubscribe→resume
    verified over real TLS loopback sockets; reconnect counter + `SYSTEM_RECOVERY`
    event; duplicate replay after reconnect produces no duplicate orders.
15. **Persistence/recovery:** real-process restart on the same SQLite DB:
    account/positions/orders restored; duplicate at cursor ignored; equity
    matches analytic continuation exactly; controller starts STOPPED (by design).
16. **Security audit:** clean (see checklist); §23 greps documented above.
17. **Termux:** NOT VERIFIED IN CURRENT ENVIRONMENT.
18. **Sandbox:** investigated — order APIs only; **market data feed is not
    sandbox-enabled**; no sandbox path implemented; nothing faked.
19. **Exact remaining blockers for full external verification:** (a) supply
    `UPSTOX_ACCESS_TOKEN` (live login token; sandbox tokens are order-APIs
    only per current docs), (b) run during NSE market hours or accept
    market-status-gated waiting, (c) optional Android/Termux device for
    mobile verification. Then run:
    `python -m app upstox-test` and
    `UPSTOX_ACCESS_TOKEN=… python verification_v061_live/real_upstox_check.py`
    (writes `real_upstox_report.json` with the §14 checklist).
20. **Confirmation:** real-money order routing does not exist in V0.6.2.
    MODE=PAPER, LIVE_TRADING=false, execution terminates at PaperBroker only.

---

## 3. Exact commands executed (abridged; full history in shell logs)

```bash
python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m pytest -q                                   # 106 passed, 1 warning
.venv/bin/python -m pytest tests/test_upstox_v062.py -q         # 13 passed (×2 for timing stability)
openssl req -x509 -newkey rsa:2048 -keyout tls_key.pem -out tls_cert.pem …   # loopback TLS fixture
.venv/bin/python /home/user/verification_v061_live/e2e_cli_local_fixture.py  # real-process e2e
env -u UPSTOX_ACCESS_TOKEN trading_bot/.venv/bin/python verification_v061_live/real_upstox_check.py
                                                              # exit 3, fail-closed, not performed
.venv/bin/python -c "… httpx GET api.upstox.com/v3/feed/market-data-feed/authorize …"   # 401, 365 ms
.venv/bin/python -c "… dialog …" ; .venv/bin/python -c "… token …"                      # 401 / 401
curl -sL https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz …  # key verified
# safety/runtime proofs
LIVE_TRADING=true .venv/bin/python -c "from app.config.settings import Settings; Settings.from_env()"  # ValueError
MODE=LIVE .venv/bin/python -c "from app.config.settings import Settings; Settings.from_env()"          # ValueError
# security greps (UPSTOX_ACCESS_TOKEN, client_secret, access_token, api_key …) — clean
```

## 4. Honest limitations

- Everything market-state-dependent (market-status transitions in the wild,
  auction segments, snapshot semantics on real reconnects, epoch-unit
  confirmation on real frames) is implemented per the current official
  documentation and the shipped proto, but observed only against local
  protocol fixtures.
- The market-status segment map keys are matched by instrument exchange
  prefix (e.g. `NSE`) with an any-open fallback; the exact provider key names
  should be confirmed against a real session and the mapping adjusted if
  needed (raw map is always visible in `/market/status`).
- `market-stream` prints normalized **completed-candle** events plus
  market-status changes (not raw per-tick events); per-tick structure is
  observable via DEBUG logs. Raw-tick printing can be added later without
  architecture change.
- Trading-signal quality is unaffected by this version: no strategy was tuned,
  retrained, or selected based on any feed sample (§18 respected). No
  profitability is claimed or implied.
- Still-true platform limitations from V0.6/V0.6.1 carry over (single paper
  writer per DB, NSE holiday calendar, no automatic backfill from Upstox, etc.).

Keep the system paper-only.
