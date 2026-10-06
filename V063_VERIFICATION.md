# V063_VERIFICATION.md — V0.6.3 Real Upstox → Paper Trading (credential-type verification)

> **Historical version report:** this V0.6.3 verification is retained for release history and does not describe the current AXIOM 3.2.0 source line. See [`AXIOM_CURRENT_AUDIT_REPORT.md`](AXIOM_CURRENT_AUDIT_REPORT.md).


**Scope:** V0.6.3 continues V0.6.2 with one focused goal — **never assume the
credential type**. The official Analytics Token documentation (verified current
2026-09-22) shows the 1-year read-only Analytics Token supports GET APIs across
listed categories **including a “Websocket” category**, while the standard
OAuth login token remains the documented full-API path for Market Data Feed V3.
Tokens are opaque strings, so the only honest verification is behavioral: the
documented `GET /v3/feed/market-data-feed/authorize` probe with an explicit
verdict BEFORE any WebSocket connection. **Real-money order routing still does
not exist. MODE=PAPER, LIVE_TRADING=false.**

---

## 1. What was verified (executed 2026-09-22)

| CHECK | STATUS | EVIDENCE |
|---|---|---|
| Analytics Token docs verified (current, not assumed) | VERIFIED | Page fetched 2026-09-22: 1-year token, strictly read-only GET only, “Websocket” listed among supported categories (no static IP needed); order operations explicitly unsupported. Implication recorded honestly: *may* work for the feed, must be proven per-token |
| Credential-type verification before connecting | VERIFIED | `verify_feed_credential` (`app/data/upstox_auth.py`): documented GET probe → verdicts `feed_ok / rejected / forbidden / unexpected_response / unreachable / invalid_input` + guidance; unit-tested across all verdicts incl. body-shape defense (200 without `authorized_redirect_uri` refuses to connect) |
| Rejected credential ⇒ NO connection | VERIFIED (against the REAL endpoint) | `real_upstox_check.py` with a clearly-fake token vs `api.upstox.com`: verdict `rejected`, `performed:false`, `final_connection_status: NOT_CONNECTED`, exit 2, exactly one authorize probe, zero WebSocket attempts (`real_upstox_report.json`) |
| No-token fail-closed | VERIFIED | Same harness without credentials: exit 3, no connection (`real_upstox_report.json` overwritten) |
| `python -m app upstox-test` gate | VERIFIED | Real loopback 401 authorize server: CLI prints `credential_check.verdict="rejected"`, `websocket:"not_tried"`, exit 1 (pinned by test) |
| Adapter auth failures carry guidance | VERIFIED | 401/403 in `_authorize` raise `UpstoxAuthError` including the Analytics-Token guidance text; secrets/response bodies never included (tests) |
| Upstox authentication (real credentials) | **NOT VERIFIED** | No Upstox account/token exists in this environment (presence-only check: all `UPSTOX_*`/`MARKET_DATA_*` false, no `.env`). Nothing authenticated was claimed |
| Real market data / WebSocket / candles / strategy / paper order / persistence **externally** | **NOT VERIFIED — BLOCKED ON CREDENTIALS** | Identical to V0.6.2: the external chain requires a real token; every mechanism is in place (`real_upstox_check.py` proves the full §14 checklist automatically once a token is supplied), but it was not possible to execute |
| Full paper pipeline (local protocol conformance) | VERIFIED (unchanged, re-run) | Real-process e2e re-run after V0.6.3 changes: phase 1 exit 0, 6 candles, 1 BUY FILLED, equity `100219.6394074926`; phase 2 restart, duplicate suppressed, 1 order, equity `100466.89210782168` = analytic expectation; market-status gating active (`market_open:true` from `marketInfo` frames) |
| Test suite | VERIFIED | **111 passed, 0 failed** (75 original + 18 V0.6.1 + 13 V0.6.2 + 5 V0.6.3), 1 pre-existing upstream warning; `pip check` clean |
| Instrument key (1 verified instrument) | VERIFIED | Official instrument master re-downloaded: `NSE_EQ|INE002A01018` = **RELIANCE INDUSTRIES LTD** (NSE_EQ) |
| Documented endpoints live (unauthenticated) | VERIFIED | authorize `401` (`errors`,`status`); login dialog `401`; token endpoint `401` — URLs real, access untested |
| Secrets hygiene | VERIFIED CLEAN | New code stores/returns no token material (`CredentialCheck` holds verdict/guidance only; tested); repo greps clean; fake-token evidence runs used an obviously synthetic string and are labeled as such |
| Android/Termux | **NOT VERIFIED IN CURRENT ENVIRONMENT** | Debian x86_64 sandbox; no Android runtime |
| Real-money trading | **DISABLED (does not exist)** | No order path added in V0.6.3; the only new network call is the same read-only GET authorize probe; `Settings` still hard-rejects unsafe env (runtime-proven in V0.6.2, unchanged) |

**REAL vs MOCK separation:** the only REAL external executions this version are
the unauthenticated 401 probes, the official instrument-master download, and
the fake-token preflight rejection (explicitly labeled; it proves the gate, not
feed access). All pipeline executions are LOCAL protocol conformance (real
loopback TLS sockets, official schema, clearly labeled) — never presented as
Upstox proof.

## 2. Files

- **Created:** `tests/test_upstox_v063.py`, `V063_VERIFICATION.md` (this file).
- **Modified:** `app/data/upstox_auth.py` (`CredentialCheck`,
  `verify_feed_credential`, guidance constants, docs links),
  `app/data/upstox_stream.py` (guidance-bearing auth errors),
  `app/__main__.py` (`upstox-test` preflight gate before connecting),
  `verification_v061_live/real_upstox_check.py` (preflight + verdict-recorded
  report + exit codes 2/3),
  `app/api/server.py`, `app/api/routes/health.py` (version 0.6.3),
  `tests/test_upstox_live.py`, `tests/test_upstox_v062.py` (version asserts),
  `.env.example`, `README.md`.
- **Unchanged:** all strategy/risk/broker/database/API-core modules; no test
  was weakened (suite grew 106 → 111).

## 3. Errors encountered during development

- Test-harness asyncio-scope bug (server loop closed before CLI ran) and an
  `Event.set(payload)` misuse — fixed by switching to a threaded raw-socket 401
  responder. No production impact; documented for honesty.
- One version-assert lag (`0.6.2` → `0.6.3`) caught by the suite and fixed.

## 4. Exact blockers for the REAL external test

1. **Credentials** (the only hard blocker): export a real
   `UPSTOX_ACCESS_TOKEN` (or `MARKET_DATA_ACCESS_TOKEN`) in the environment —
   either a standard OAuth login token (`python -m app upstox-login`) or an
   Analytics Token *if* you have verified your token’s feed access; the gate
   will tell you either way, before connecting.
2. **Market hours** (for ticks/candles/signals): NSE 09:15–15:30 IST on a
   trading day, or accept the market-status-gated waiting mode.
3. Then run:
   ```bash
   python -m app upstox-test
   UPSTOX_ACCESS_TOKEN=<token> python verification_v061_live/real_upstox_check.py \
       --instruments NSE_EQ|INE002A01018 --duration 90
   # → real_upstox_report.json with the §14 checklist and evidence counters
   ```
   Success criteria appear automatically: auth ✅, WS ✅, real data ✅, protobuf ✅,
   candles ✅, then `python -m app paper-live` for strategy→risk→PaperBroker→SQLite.

## 5. Confirmation

Real-money order routing does not exist in V0.6.3. Execution terminates at the
PaperBroker. No profitability claims are made; nothing was fabricated; every
“VERIFIED” row above maps to an executed command or test in this environment.

Keep the system paper-only.
