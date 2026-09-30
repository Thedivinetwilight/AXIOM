# Quick Diagnostic Flowchart — "Something is broken. Start here."

Every command below is safe: none can place an order, and none prints a secret.
Full explanations live in `FULL_TROUBLESHOOTING_GUIDE.md`; symptom-by-symptom fixes live in
`ERROR_DIAGNOSTIC_PLAYBOOK.md`.

**Verification note:** the exit codes and outputs quoted in this flowchart were produced by
running the commands on Linux / Python 3.11.2 against commit `60113ce`. PowerShell forms are
given for convenience but were **not** executed on Windows.

---

## Stage 0 — Before anything else (60 seconds)

```bash
python -c "import sys; print(sys.executable); print(sys.version)"
python -m app paper-test
```

| Result | Go to |
| --- | --- |
| `paper-test` exits **0** with `"ok": true` | Stage 1 — your environment is sound |
| `paper-test` fails or `python` is the wrong interpreter | **[A] Environment** |
| `ModuleNotFoundError` | **[A] Environment** |

`paper-test` is offline and deterministic. **If it fails, nothing downstream is trustworthy** —
do not diagnose the broker, the feed or the API until it passes.

---

## Stage 1 — The main tree

```
                          SOMETHING IS BROKEN
                                   │
                    ┌──────────────┴──────────────┐
                    │ Is the desktop UI involved?  │
                    └──────────────┬──────────────┘
                        yes ◄──────┴──────► no
                         │                  │
                 [D] AXIOM/UI          ┌────┴─────────────────┐
                                       │ Is there a Python     │
                                       │ traceback?            │
                                       └────┬─────────────────┘
                                 yes ◄──────┴──────► no
                                  │                  │
                       ┌──────────┴───┐        ┌─────┴──────────────┐
                       │ Where is it  │        │ What is NOT working │
                       │ raised?      │        │ as expected?        │
                       └──────────┬───┘        └─────┬──────────────┘
                                  │                  │
        ┌────────────┬────────────┼────────┐         ├──────────────┐
        │            │            │        │         │              │
   app/config   app/brokers   app/api   app/engine  no data     wrong numbers
   /settings.py /sandbox.py   /…        /…          [C] Data    [E] Logic
   [B] Config   [F] Broker    [G] API   [E] Logic
```

---

## [A] Environment / dependencies

```bash
python -c "import sys; print(sys.executable)"
python -m pip show httpx fastapi pydantic protobuf
python -c "from app.config.settings import Settings; Settings.from_env(); print('settings OK')"
python -c "import app.brokers.sandbox; print('broker imports OK')"
```

| Observation | Meaning | Section |
| --- | --- | --- |
| `sys.executable` is not your venv | venv not active — you are fixing the wrong interpreter | Guide §5.3 |
| `ModuleNotFoundError: httpx` (or fastapi/pydantic) | `requirements.txt` not installed in this env | §5.2 |
| `ImportError: PySide6` / `libGL.so.1` | desktop-only dependency; backend is unaffected | §5.5 |
| `DecodeError` / protobuf version complaint | generated bindings vs runtime mismatch | §5.6 |
| `Starlette` 1.x installed | breaks the httpx-based `TestClient` — documented trap | §5.1 |

Repair, then re-run `python -m app paper-test`.

---

## [B] Configuration

```bash
python -c "from app.config.settings import Settings as S; s=S.from_env(); print('mode',s.mode,'broker',s.broker,'data_source',s.data_source,'calendar',s.market_calendar)"
python -c "from app.config.settings import Settings as S; s=S.from_env(); print('api_token', bool(s.api_token.strip()), '| sandbox_token', bool(s.upstox_sandbox_access_token.strip()), '| md_token', bool(s.market_data_access_token.strip()))"
```

**Verified rejection messages — if you see one, it is configuration, not code:**

| Message | Fix |
| --- | --- |
| `Only MODE=PAPER and LIVE_TRADING=false are permitted` | `MODE=PAPER`, `LIVE_TRADING=false` |
| `Only SQLite is supported` | `DATABASE_URL=sqlite:///…` |
| `LIVE trading is not enabled in V1` | `BROKER=LOCAL_PAPER` or `UPSTOX_SANDBOX` |
| `BROKER must be LOCAL_PAPER or UPSTOX_SANDBOX` | one of those two |
| `BROKER=UPSTOX_SANDBOX requires UPSTOX_SANDBOX_ACCESS_TOKEN in the environment` | export the token |
| `Unknown market calendar` | `continuous` or `nse-session` |
| `Unknown data source` | `replay`, `binance`, `generic`, `upstox` |
| `MARKET_DATA_MODE must be ltpc or full` | one of those two |
| `NET_EDGE_THRESHOLD must be 0..0.1` | value in range |
| `<NAME> must be a number (got '…')` | numeric field got text |

Precedence reminder: **process env beats `.env`** (`load_dotenv(..., override=False)`,
`app/config/settings.py:240`).

→ Guide §6.

---

## [C] Data / market feed

```bash
python -m app monitor                       # exit 0 + database_ok:true expected
python -m app market-universe status        # instrument master state
```

| Symptom | First check | Section |
| --- | --- | --- |
| No candles at all | `MARKET_DATA_SYMBOLS` set? `python -m app upstox-test` | §11.2 |
| Candles then silence | `feed_stale` in the log — expected during quiet periods | §11.3 |
| Flood of `UPSTOX_DECODE_ERROR` | protobuf runtime vs generated bindings | §5.6 |
| Constant reconnects | `UPSTOX_RECONNECT` count; bounded backoff caps at 60 s | §11.3 |
| "Instrument not found" | instrument master not synced (`initialize` records `UNIVERSE_SYNC_UNAVAILABLE` offline) | §11.5 |
| Subscription refused | `streaming_universe` must be explicit keys, never the whole universe | §11.6 |

**Do not conclude "the feed is down" before checking staleness and decode errors** — both suppress
candles without raising.

→ Guide §11.

---

## [D] AXIOM desktop / UI

Test the transport **without Qt** (`desktop/api_client.py` does not import PySide6):

```bash
python -c "
from desktop.api_client import ArenaApiClient
c = ArenaApiClient('http://127.0.0.1:8000', '<token>')
try: print(c.overview())
except Exception as e: print(type(e).__name__, e)
"
```

| Exception | Meaning |
| --- | --- |
| `ConnectionRefusedError` / `URLError` | nothing listening at `ARENA_BACKEND_URL` |
| `HTTPError 401` | `ARENA_API_TOKEN` ≠ server `API_TOKEN` |
| `HTTPError 503` | server has **no** `API_TOKEN` configured |
| `HTTPError 404` | wrong base URL or path |
| timeout | server slow or firewalled (client default 10 s) |

If curl works but the UI disagrees → **the UI is showing stale data**, it does not clear on
backend failure.

Config lives at `desktop/app.py:37–38`: `ARENA_BACKEND_URL` (default
`http://127.0.0.1:8000`), `ARENA_API_TOKEN` (default `""`).

→ Guide §13.

---

## [E] Trading logic / execution

```bash
python -m app monitor
# engine state, read-only:
sqlite3 "file:trading_bot.db?mode=ro" "SELECT key, payload FROM engine_state;"
sqlite3 "file:trading_bot.db?mode=ro" "SELECT intent_id, state FROM sandbox_orders;"
```

| Symptom | Look at | Section |
| --- | --- | --- |
| Signals but no orders | risk gate reason (`RISK_REJECT`), `rejected_by_risk` in `status()` | §10.2 |
| Orders stuck non-final | `unresolved_intents` > 0 → reconcile before restarting | §10.4 |
| Duplicate orders suspected | `SubmissionJournal.resubmission_forbidden()` — do not clear the journal | §10.4 |
| Engine won't start after a stop | `engine_state['emergency_stop']` is persisted on purpose | §17.2 |
| Daily loss limit resets oddly | `MARKET_TIMEZONE` drives the trading-day boundary | §10.6 |
| Equity wrong after restart | `recover_ledger_state()` replay | §10.5 |

**Risk-gate reasons are a closed set:** `risk_reducing_exit_allowed`,
`max_position_value_exceeded`, `max_open_positions_reached`, `max_daily_loss_reached`,
`max_trades_per_day_reached`, `within_limits`. A limit of `0` means **unlimited**, not zero.

→ Guide §10.

---

## [F] Broker / Upstox sandbox

```bash
python -m app sandbox-test          # read-only; exit 3 = no token, nothing attempted
python -c "from app.config.settings import Settings as S; s=S.from_env(); print(s.upstox_sandbox_base_url); print(s.upstox_sandbox_order_history_url)"
```

**Read this before blaming a token:**

| HTTP | `classify_history_probe` verdict | Actually means |
| --- | --- | --- |
| 400/404 **with** `UDAPI100010` | `credential_accepted = True` | **Token is fine** — probe id just doesn't exist |
| 401 / 403 | `auth_rejected` | Rejected — but check host, IP allowlist (`UDAPI1154`), token *type*, expiry |
| 429 | `rate_limited` | Back off; GETs retry once, writes never |
| 5xx | `provider_error` | Upstox side |
| anything else | `unexpected` | Fail closed — inspect the body |

**A 404 with `UDAPI100010` is success.** Concluding "token invalid" from it is the most common
mis-diagnosis in this system.

Also remember: after **one** 401/403 the broker sets `available = False` permanently and every
later call raises `"Sandbox broker disabled after an auth failure"`. Find the **first**
`SANDBOX_AUTH_FAILURE` in the log — everything after it is downstream noise.

→ Guide §7.

---

## [G] API / server

```bash
curl -s http://127.0.0.1:8000/health
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/v2/overview
curl -s -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/api/v2/risk
```

| Status | Meaning | Fix |
| --- | --- | --- |
| connection refused | server not running | `python run.py --mode api` |
| **503** `Set API_TOKEN to enable protected API endpoints` | server has no token | set `API_TOKEN` server-side |
| **401** `Invalid or missing bearer token` | your token doesn't match | fix the client token |
| **200** | fine | — |
| **422** | a `ValueError` was raised in the route | read `detail` — it is the real message |
| **500** `Internal error; review local logs` | unhandled exception | **the body has no traceback** — check the `bot.api` log |

**503 vs 401 is the whole distinction.** 503 = server unconfigured. 401 = client wrong.

Writing your own test? `app.state.repository` is only set inside the `lifespan` block, so you must
use `with TestClient(create_app(...)) as c:` — otherwise you get
`AttributeError: 'State' object has no attribute 'repository'`.

→ Guide §9.

---

## [H] Database

```bash
python -m app monitor                                        # database_ok?
sqlite3 "file:trading_bot.db?mode=ro" "PRAGMA quick_check;"
python -c "from app.config.settings import Settings as S; import os; print(os.path.abspath(S.from_env().database_url.replace('sqlite:///','')))"
```

| Observation | Meaning | Section |
| --- | --- | --- |
| absolute path is not where you expected | `sqlite:///trading_bot.db` is **relative to cwd** | §12.1 |
| `database_ok: false` | corruption — restore from a verified backup | §12.5–12.6 |
| `database is locked` | another process holds the writer; single-writer by design | §18.3 |
| data "missing" | you are pointed at a different database file | §12.1 |

Backup with the project's own tool, **not** a file copy (the DB is in WAL mode):

```bash
python -m app backup backup --label pre-change
python -m app backup verify --path backups/<file>.db
```

→ Guide §12.

---

## Stage 2 — Still stuck?

Collect the bundle (see `SAFE_DIAGNOSTIC_COMMANDS.md` §"Minimum diagnostic bundle"), then:

1. Re-read the **first** error in the log, not the last. Arena converts exceptions at three
   layers, and the outermost message is rarely the cause.
2. Confirm you are looking at logs that exist: **`python -m app …` writes no log file** — only
   `run.py` configures `logs/bot.jsonl`. Capture stderr if you ran the operational CLI.
3. Reproduce with the smallest command: `python -m app paper-test`, then the single failing
   pytest node with `-l --tb=long`.

---

## Known limitations of this flowchart

- It routes you to subsystems; it cannot tell you *why* Upstox rejected a specific request. No
  live Upstox response was observed while writing this, so the HTTP-status table in **[F]** is
  the code's contract (`classify_history_probe`, `app/brokers/sandbox.py:134`), not an observed
  provider behaviour.
- The **[D]** branch covers the HTTP transport only. Qt rendering, layout and threading were
  never exercised here (no display server), so "the window is blank/frozen" has no verified
  procedure beyond the headless client test.
- Windows/PowerShell branches are unverified — authored on Linux.
- There is no branch for "live trading is broken": Arena 3.0.5 has no live order path
  (`BROKER=LIVE` is rejected, `/health` reports `real_order_routing: NOT_IMPLEMENTED`).
