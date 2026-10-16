# V064_VERIFICATION.md — V0.6.4 Termux Offline Market-Feed Verification

> **Historical version report:** this V0.6.4 verification is retained for release history and does not describe the current AXIOM 3.2.0 source line. See [`AXIOM_CURRENT_AUDIT_REPORT.md`](AXIOM_CURRENT_AUDIT_REPORT.md).


**Scope:** run the complete market-data → candles → strategies → risk →
PaperBroker pipeline **entirely offline** (Android Termux / any minimal
Python 3.11+) **without an Upstox credential**, using a deterministic
synthetic provider that emits the SAME normalized `MarketEvent` objects as the
real Upstox V3 adapter and flows through the SAME shared normalization
pipeline (`MarketEventApplier` → `LiveCandleBuilder` → market-status gate).
**No real-money order routing exists. MODE=PAPER, LIVE_TRADING=false.**

---

## 1. VERIFIED (executed in this environment, 2026-09-22)

| CHECK | STATUS | EVIDENCE |
|---|---|---|
| Synthetic deterministic provider | VERIFIED | `app/data/synthetic_feed.py`: fixed base epoch (2026-01-05T09:30Z), virtual clock, 8 live ticks + 1 snapshot + 2 market-status frames (CLOSED→OPEN), embedded exact duplicate tick, instrument key `NSE_EQ\|INE002A01018` |
| Emits the same `MarketEvent` objects as the Upstox adapter | VERIFIED | Same dataclass; folded through the SAME `MarketEventApplier` (refactored out of the Upstox adapter; all 36 prior Upstox tests re-passed unchanged) |
| Market status / snapshot / live updates / timestamps / instrument keys | VERIFIED | `market_info` frames drive the gate; `snapshot`/`live` kinds carry exchange+receive timestamps; provider labeled `synthetic-offline` everywhere |
| Candle construction (1m) | VERIFIED | closes exactly `[100.5, 101.0, 101.5, 102.0, 101.0, 100.75]`; 7 candles built, 6 emitted, 1 held by the gate |
| Market-status gate NOT bypassed | VERIFIED | candle completed while CLOSED → `suppressed_by_market_status=1`, never reached the strategy; execution began only after `NORMAL_OPEN` |
| Strategy / risk engine / PaperBroker | VERIFIED | momentum(2,0): BUY→SELL, `risk_counters {actionable_signals:4, orders_created:2, skipped:2}`; realistic costs: realized P&L **−362.41536614602916** (net of 24.94 fees) — the platform's `max_participation` partial-fill model observed working (that's why volume 10k/tick is used) |
| No duplicate orders | VERIFIED | exactly 2 orders, unique IDs, `[BUY, SELL]`; embedded duplicate tick counted (`duplicates=1`) and never double-counted |
| Position accounting / realized & unrealized P&L / equity | VERIFIED | position flat after round trip; `realized_pnl == trade.pnl`; `equity == 100000 + realized` exactly (**99637.58463385397**) |
| Restart/replay determinism | VERIFIED | persisted account restored; cursor duplicate suppressed; 2 continuation candles processed; equity identical; **0 new orders** |
| Cross-run determinism | VERIFIED | second independent run: economics identical (IDs excluded — UUIDs by design), equity exact |
| `python -m app paper-test` | VERIFIED | exit 0, 17/17 internal checks pass, JSON report with expected vs actual; isolated temp DB by default (user DB untouched) |
| Zero third-party dependencies for the core pipeline (Termux-relevant) | VERIFIED | test runs `paper-test` with `fastapi, pydantic, httpx, websockets, protobuf, dotenv, uvicorn, anyio, starlette` imports hard-BLOCKED → passes. Core pipeline = standard library only |
| CLI determinism across processes | VERIFIED | two `python -m app paper-test` subprocesses → identical equity, both `ok:true` |
| Missing Upstox credentials → safe refusal | VERIFIED | `python -m app upstox-test` with no tokens → exit 2, refusal before any connection; `real_upstox_check.py` exit 3 (fail-closed; carried over from V0.6.3) |
| Existing architecture preserved | VERIFIED | Upstox V3 adapter kept as the real provider adapter (36 Upstox tests unchanged/passing); full suite **118 passed, 0 failed** (111 + 7 new), `pip check` clean |
| Real-money trading | **DISABLED (does not exist)** | no order path added; synthetic feed is read-only market data into PaperBroker only |

## 2. NOT VERIFIED (honest)

- **Termux Python runtime on Android** — this environment is Debian glibc
  x86_64 with no Android/Termux/ARM runtime. The Termux-relevant *properties*
  (stdlib-only core, no mandatory FastAPI/Pydantic, deterministic offline run)
  are verified here; the actual Termux execution is not.
- **Python 3.14** — not available in this environment (CPython 3.13.14 used;
  project targets 3.11+). Nothing 3.14-specific was proven.
- **Authenticated external Upstox V3 WebSocket / real Upstox market data /
  live trading** — unchanged from V0.6.3: blocked on credentials; the
  synthetic feed is explicitly NOT a substitute and is labeled as such.
- Real session behaviors (market-status transitions in the wild, snapshot
  semantics on real reconnects, epoch-unit confirmation) remain pending the
  credential-gated real test (`verification_v061_live/real_upstox_check.py`).

## 3. Files changed

- **Created:** `app/data/synthetic_feed.py` (`SyntheticStream` /
  `SyntheticMarketDataProvider`, `build_scenario`, deterministic `BASE_EPOCH`),
  `tests/test_paper_test_v064.py`, `V064_VERIFICATION.md` (this file).
- **Modified:** `app/data/market_events.py` (+ `MarketEventApplier` — shared
  normalization used by BOTH providers), `app/data/upstox_stream.py`
  (delegates event folding to the shared applier; behavior identical, all
  Upstox tests re-pass), `app/__main__.py` (+ `paper-test` command, pinned
  expected constants, continuation scenario), `app/api/server.py` +
  `app/api/routes/health.py` (version 0.6.4), `tests/test_upstox_live.py`,
  `tests/test_upstox_v062.py` (version asserts), `README.md`.
- **Untouched:** strategies, indicators, backtest, tournament, validation,
  broker, portfolio, database cores; no test weakened (111 → 118).

## 4. Commands to run on Termux

```bash
pkg install python            # Python 3.11+ from Termux packages
cd trading_bot                # the extracted project directory
python -m app paper-test      # NO pip install required — pure standard library
```

Optional (only for the API/dashboard or the real Upstox adapter, not for
`paper-test`):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q                          # full suite
python -m app upstox-test                    # needs UPSTOX_ACCESS_TOKEN in .env
python -m app paper-live                     # real feed → paper pipeline
```

## 5. Exact limitations

- Synthetic data proves the *pipeline mechanics* (normalization, gating,
  candle building, execution accounting, determinism, persistence) — it proves
  nothing about strategy profitability or real-market behavior.
- The pinned expected values (equity/realized P&L) are exact for the default
  `ExecutionConfig` on this platform; float formatting could differ in exotic
  builds, so cross-platform runs compare with 1e-9 tolerance in tests.
- Determinism excludes order/fill/trade IDs (UUIDv4 by existing design);
  economics are compared instead.
- Same pending items as V0.6.3: authenticated external Upstox test,
  epoch-unit confirmation, Termux device run, Python 3.14.

Keep the system paper-only.
