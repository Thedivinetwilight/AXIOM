# Error Diagnostic Playbook — Arena 3.0.5

Every entry uses the same eight-part template:

> **SYMPTOM → WHAT IT MEANS → LIKELY ROOT CAUSES → EXACT DIAGNOSTIC COMMANDS →
> HOW TO INTERPRET OUTPUT → FIX → VERIFICATION → WHAT NOT TO CHANGE**

Find your symptom in the index, work the entry top to bottom. Do not skip **WHAT NOT TO CHANGE** —
in a trading system, an unnecessary edit is often more expensive than the original fault.

**Verification:** commands and outputs marked *verified* were executed on Linux / Python 3.11.2
against commit `99fe847`. Entries requiring Upstox credentials are marked **UNVERIFIED — no live
provider response was observed while writing this**; their logic is transcribed from source.

---

## Index

| # | Symptom |
| --- | --- |
| 1 | `ModuleNotFoundError` on startup |
| 2 | `ValueError: Only MODE=PAPER and LIVE_TRADING=false are permitted` |
| 3 | `ValueError: BROKER=UPSTOX_SANDBOX requires UPSTOX_SANDBOX_ACCESS_TOKEN…` |
| 4 | Every protected endpoint returns 503 |
| 5 | Every protected endpoint returns 401 |
| 6 | API returns 500 `Internal error; review local logs` |
| 7 | `SandboxAuthError: Sandbox credential rejected (HTTP 401)` |
| 8 | `SandboxRequestError: Sandbox broker disabled after an auth failure` (repeated) |
| 9 | `SandboxRequestError: Sandbox API unreachable (network error)` |
| 10 | Sandbox probe returns 404 / 400 |
| 11 | Signals are generated but no orders are submitted |
| 12 | Orders stuck in a non-final state after a crash |
| 13 | Engine refuses to start after an emergency stop |
| 14 | No candles arriving from the Upstox feed |
| 15 | Feed connects, then drops repeatedly |
| 16 | `UPSTOX_DECODE_ERROR` flooding the log |
| 17 | Database appears empty / data "vanished" |
| 18 | `database is locked` |
| 19 | Desktop UI shows stale or blank data |
| 20 | Desktop cannot reach the backend |
| 21 | A test fails in the full suite but passes alone |
| 22 | `AttributeError: 'State' object has no attribute 'repository'` |
| 23 | Logs are missing entirely |
| 24 | Daily loss limit resets at the wrong time |

---

## 1. `ModuleNotFoundError` on startup

**SYMPTOM**
```
ModuleNotFoundError: No module named 'httpx'
```
(or `fastapi`, `pydantic`, `protobuf`, `PySide6`, `dotenv`)

**WHAT IT MEANS**
The interpreter running the command does not have that package installed. This is an environment
fault, never a code fault — Arena's imports are unconditional and covered by `requirements.txt`.

**LIKELY ROOT CAUSES**
1. Dependencies never installed in this environment.
2. A venv exists but is not active — the system interpreter is being used.
3. Two interpreters installed; the wrong one is first on `PATH`.
4. `requirements-kronos.txt` / `requirements-desktop.txt` needed but only `requirements.txt`
   installed (e.g. `PySide6`, `torch`).
5. A fresh machine/container where nothing was set up.

**EXACT DIAGNOSTIC COMMANDS**
```bash
python -c "import sys; print(sys.executable)"
python -m pip list 2>/dev/null | grep -iE "httpx|fastapi|pydantic|protobuf|pyside6"
grep -rn "httpx" requirements*.txt
```
```powershell
python -c "import sys; print(sys.executable)"
python -m pip list | Select-String "httpx|fastapi|pydantic"
Select-String -Path requirements*.txt -Pattern "httpx"
```

**HOW TO INTERPRET OUTPUT**
- `sys.executable` outside your venv → cause 2. Activate the venv; do not `pip install` globally.
- Package absent from `pip list` but present in a requirements file → cause 1 or 4.
- `PySide6` missing while the backend imports fine → you only need `requirements-desktop.txt`, and
  the backend is unaffected.
- If `Starlette` shows `1.x`, that is a separate problem — `requirements.txt` pins
  `starlette>=0.46,<1` because *"Starlette 1.x changes its client stack"*, which breaks the test
  suite's `TestClient`.

**FIX**
```bash
python -m venv .venv && . .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-desktop.txt   # only for the GUI
```
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements.txt
```

**VERIFICATION**
```bash
python -c "import app.brokers.sandbox, app.api.server; print('imports OK')"
python -m app paper-test        # must exit 0
```

**WHAT NOT TO CHANGE**
Do not add the missing package to `requirements.txt` — it is almost certainly already there and
your environment is the problem. Do not `pip install --break-system-packages` on a machine that
has a venv; that is how mixed environments are created. Do not downgrade Starlette below the pin
to "fix" something unrelated.

---

## 2. `ValueError: Only MODE=PAPER and LIVE_TRADING=false are permitted`

**SYMPTOM**
Any command fails immediately with that message.

**WHAT IT MEANS**
`Settings.__post_init__` (`app/config/settings.py:144`) rejected the configuration. This is a
**deliberate safety guard**, raised before any database, network or broker object exists. Nothing
else in the process has run yet.

**LIKELY ROOT CAUSES**
1. `MODE` set to something other than `PAPER`.
2. `LIVE_TRADING` set to `true`.
3. A stale `.env` copied from another project or an older branch.
4. An environment variable exported in your shell profile overriding `.env`.

**EXACT DIAGNOSTIC COMMANDS**
```bash
python -c "from app.config.settings import Settings as S; s=S.from_env(); print('mode',repr(s.mode),'live',s.live_trading)"
env | grep -iE "^(MODE|LIVE_TRADING)="
grep -nE "^(MODE|LIVE_TRADING)=" .env
```
```powershell
python -c "from app.config.settings import Settings as S; s=S.from_env(); print('mode',repr(s.mode),'live',s.live_trading)"
Get-ChildItem Env: | Where-Object { $_.Name -in 'MODE','LIVE_TRADING' }
```

**HOW TO INTERPRET OUTPUT**
Because `load_dotenv(..., override=False)` (`app/config/settings.py:240`), **a process environment
variable beats `.env`**. If `env` shows `MODE=LIVE` but `.env` says `PAPER`, the environment wins
and that is your bug.

Note this guard cannot be bypassed by configuration at all — there is no value of `MODE` other
than `PAPER` that is accepted.

**FIX**
```bash
# in .env
MODE=PAPER
LIVE_TRADING=false
```
and unset any shell override:
```bash
unset MODE LIVE_TRADING
```
```powershell
Remove-Item Env:MODE, Env:LIVE_TRADING -ErrorAction SilentlyContinue
```

**VERIFICATION**
```bash
python -c "from app.config.settings import Settings as S; s=S.from_env(); print('OK', s.mode, s.live_trading)"
```
Verified expected output: `OK PAPER False`.

**WHAT NOT TO CHANGE**
**Do not edit `app/config/settings.py` to relax this guard.** It is the single control that makes
the rest of the system's safety claims true, and `GET /health` independently reports
`"real_order_routing": "NOT_IMPLEMENTED"`. If you believe you need live trading, that is a project
decision, not a configuration fix — and Arena 3.0.5 does not implement it.

---

## 3. `ValueError: BROKER=UPSTOX_SANDBOX requires UPSTOX_SANDBOX_ACCESS_TOKEN in the environment`

**SYMPTOM**
Startup fails with that message.

**WHAT IT MEANS**
You selected the sandbox broker but no sandbox token is present. Fail-closed by design
(`app/config/settings.py`, broker validation block).

**LIKELY ROOT CAUSES**
1. `UPSTOX_SANDBOX_ACCESS_TOKEN` never set.
2. It is set but empty or whitespace-only (the check is `.strip()`).
3. You set `MARKET_DATA_ACCESS_TOKEN` instead — **a different token for a different purpose**.
4. The token is in a `.env` that is not at the repository root. `ROOT` is
   `Path(__file__).resolve().parents[2]` (`app/config/settings.py:9`), so the file must be
   `<repo>/.env`.
5. The token is exported in a different shell session than the one running the command.

**EXACT DIAGNOSTIC COMMANDS**
```bash
python -c "from app.config.settings import Settings as S; s=S.from_env(); t=s.upstox_sandbox_access_token; print('length', len(t.strip()))"
python -c "from app.config.settings import Settings as S; s=S.from_env(); print('sandbox', bool(s.upstox_sandbox_access_token.strip()), '| market_data', bool(s.market_data_access_token.strip()))"
ls -la .env
```

**HOW TO INTERPRET OUTPUT**
- `length 0` → the variable is unset or blank.
- `sandbox False | market_data True` → cause 3. You have a market-data token, not a sandbox token.
  They are not interchangeable.
- No `.env` at the repo root → cause 4.

**FIX**
```bash
cp .env.example .env        # if you have no .env yet
# then set, in .env:
#   UPSTOX_SANDBOX_ACCESS_TOKEN=<your sandbox token>
```
or export it:
```bash
export UPSTOX_SANDBOX_ACCESS_TOKEN="<your sandbox token>"
```

**VERIFICATION**
```bash
python -c "from app.config.settings import Settings as S; s=S.from_env(); print('broker', s.broker, 'token_len', len(s.upstox_sandbox_access_token.strip()))"
python -m app sandbox-test      # read-only probe; see entry 10
```

**WHAT NOT TO CHANGE**
Do not paste the token into a command line where it lands in shell history; put it in `.env` or
export it from a file. Do not substitute the market-data token — the sandbox order API will reject
it and the broker will latch closed (entry 8). Do not commit `.env`; verify with
`git check-ignore -v .env`.

---

## 4. Every protected endpoint returns 503

**SYMPTOM**
```json
{"detail":"Set API_TOKEN to enable protected API endpoints"}
```

**WHAT IT MEANS**
`authenticate` (`app/api/auth.py:33`) found **no token configured on the server**. This is a
server-side configuration gap, not a client credential problem. *Verified by execution.*

**LIKELY ROOT CAUSES**
1. `API_TOKEN` is empty or unset in the server's environment.
2. The server was started from a shell that did not load `.env`.
3. `API_TOKEN=` is present in `.env` but blank — `.env.example` ships it blank on purpose.

**EXACT DIAGNOSTIC COMMANDS**
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/v2/overview
python -c "from app.config.settings import Settings as S; print('API_TOKEN', 'SET' if S.from_env().api_token.strip() else 'unset')"
```

**HOW TO INTERPRET OUTPUT**
- `/health` returns 200 while `/api/v2/overview` returns 503 → the server is fine, the token is
  missing. `/health` is deliberately unauthenticated.
- `API_TOKEN unset` in the **server's** environment confirms it. Running that command in a
  different shell proves nothing about the server process.

**FIX**
Set a private random value in `.env`:
```
API_TOKEN=<a long random string>
```
then restart the server. Generate one:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**VERIFICATION**
```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/api/v2/overview
```
Expect **200**. *Verified: 200 with a correct bearer, 401 without.*

**WHAT NOT TO CHANGE**
Do not remove the `Depends(authenticate)` dependencies from
`app/api/server.py:91` to "get it working" — that would expose account data, engine controls and
the emergency-stop endpoint to anything that can reach the port. Do not set `API_TOKEN` to a short
or guessable value. Do not confuse this with 401 (entry 5) — 503 is server-side, 401 is client-side.

---

## 5. Every protected endpoint returns 401

**SYMPTOM**
```json
{"detail":"Invalid or missing bearer token"}
```
with a `WWW-Authenticate: Bearer` response header.

**WHAT IT MEANS**
The server **has** a token and yours does not match. Comparison is `hmac.compare_digest`
(`app/api/auth.py`), so it is exact and constant-time — there is no partial match and no timing
side channel. *Verified by execution.*

**LIKELY ROOT CAUSES**
1. Client `ARENA_API_TOKEN` differs from server `API_TOKEN`.
2. The `Authorization` header is malformed — it must be exactly `Bearer <token>` with one space.
3. A trailing newline or quote was captured into the variable.
4. The server was restarted with a new token and the client kept the old one.
5. A proxy rewrote or stripped the header.

**EXACT DIAGNOSTIC COMMANDS**
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/v2/overview
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/api/v2/overview
python -c "from app.config.settings import Settings as S; t=S.from_env().api_token; print('server token length', len(t), '| has whitespace', t != t.strip())"
printf '%s' "$API_TOKEN" | wc -c
```

**HOW TO INTERPRET OUTPUT**
- First command 401, second 200 → the client variable is wrong, the server is fine.
- Both 401 → the token value itself is wrong, or the header is malformed.
- `has whitespace True` → the `.env` value has a trailing space or newline; that alone causes 401.
- The two lengths differing → you are comparing different values.

**FIX**
Make client and server agree:
```bash
export API_TOKEN="<the server's token>"          # for curl
export ARENA_API_TOKEN="$API_TOKEN"             # for the desktop client
```
For the desktop app, `ARENA_API_TOKEN` is read at `desktop/app.py:38`.

**VERIFICATION**
```bash
python -c "
from desktop.api_client import ArenaApiClient
import os
c = ArenaApiClient('http://127.0.0.1:8000', os.environ['API_TOKEN'])
print(c.overview().get('version'))
"
```
Expect `3.0.5`.

**WHAT NOT TO CHANGE**
Do not weaken `authenticate` to accept an empty or partial token. Do not put the token in a URL
query string — it will be logged by proxies and land in access logs. Do not disable auth by
binding to `0.0.0.0` and hoping the network is trusted.

---

## 6. API returns 500 `Internal error; review local logs`

**SYMPTOM**
```json
{"detail":"Internal error; review local logs"}
```

**WHAT IT MEANS**
The catch-all handler at `app/api/server.py:77` fired. **The response body deliberately contains no
traceback.** The handler logs to the `bot.api` logger with event `request_failed` and the exception
*class name only* — so even the log line is terse, and you need the server's stderr for the stack.

**LIKELY ROOT CAUSES**
1. A genuine bug in a route.
2. A database schema mismatch after an upgrade.
3. `app.state.repository` / `app.state.broker` missing because the lifespan never ran (entry 22).
4. An unhandled provider response shape.

**EXACT DIAGNOSTIC COMMANDS**
Run the server in the foreground so stderr is visible:
```bash
LOG_LEVEL=DEBUG python run.py --mode api --host 127.0.0.1 --port 8000 2>&1 | tee logs/api-trace.txt
```
Reproduce in a second shell, then:
```bash
grep -n "Traceback" logs/api-trace.txt | head
grep -n '"event": "request_failed"' logs/api-trace.txt | head
```

**HOW TO INTERPRET OUTPUT**
- The **first** `Traceback` after your request is the root cause. Later ones are usually the same
  failure retried.
- `AttributeError: 'State' object has no attribute ...` → entry 22, not a route bug.
- `sqlite3.OperationalError: no such table` / `no such column` → schema/migration problem; check
  `SELECT version FROM schema_version`.
- Note `ValueError` is **not** a 500 — it is mapped to **422** with the real message
  (`app/api/server.py:73`). If you got a 500, the exception was something else.

**FIX**
Depends on the traceback. For a schema mismatch, restore a verified backup:
```bash
python -m app backup verify --path backups/<file>.db
```

**VERIFICATION**
```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/api/v2/overview
python -m pytest tests/test_runtime_database_api.py -q
```

**WHAT NOT TO CHANGE**
Do not replace the 500 handler with one that returns the traceback to the client — that leaks
internals and possibly paths or identifiers to any caller. Do not "fix" it by broadening the
`ValueError` handler; a 500 means the exception type was genuinely unexpected and worth
understanding.

---

## 7. `SandboxAuthError: Sandbox credential rejected (HTTP 401)`

**SYMPTOM**
```
SandboxAuthError: Sandbox credential rejected (HTTP 401); broker disabled
```
and a `SANDBOX_AUTH_FAILURE` log line.

**WHAT IT MEANS**
`UpstoxSandboxBroker._request` (`app/brokers/sandbox.py:240`) received 401 or 403 and **latched the
broker closed**. It is an authentication rejection — but *not necessarily an invalid token.*

> **UNVERIFIED:** no live Upstox response was observed while writing this. The classification below
> is transcribed from `classify_history_probe` (`app/brokers/sandbox.py:134`) and the module
> docstring.

**LIKELY ROOT CAUSES — check in this order**
1. **Wrong host.** A sandbox token presented to a live-looking base URL. The constructor logs
   `SANDBOX_BASE_URL_REVIEW` when `base_url` contains `live` but not `sandbox` (line 197).
2. **Static-IP restriction.** The module docstring records documented caveat `UDAPI1154`: order
   APIs can be restricted to a static IP. A new machine, container or VPN can be rejected with a
   perfectly valid token.
3. **Wrong token type.** `UPSTOX_SANDBOX_ACCESS_TOKEN` is not `MARKET_DATA_ACCESS_TOKEN`.
4. **Expired token.** Upstox OAuth tokens are daily.
5. **Whitespace or quoting** in the `.env` value.
6. Genuinely revoked credentials — check last, not first.

**EXACT DIAGNOSTIC COMMANDS**
```bash
python -c "
from app.config.settings import Settings as S
s = S.from_env()
u = s.upstox_sandbox_base_url.lower()
print('orders  ->', s.upstox_sandbox_base_url)
print('history ->', s.upstox_sandbox_order_history_url)
print('SUSPICIOUS live-looking host' if ('live' in u and 'sandbox' not in u) else 'host looks sandbox-appropriate')
t = s.upstox_sandbox_access_token
print('token length', len(t.strip()), '| has surrounding whitespace', t != t.strip())
"
grep -c "SANDBOX_BASE_URL_REVIEW" logs/*.jsonl 2>/dev/null
python -m app sandbox-test
```

**HOW TO INTERPRET OUTPUT**
- `SUSPICIOUS live-looking host` → cause 1. Fix `UPSTOX_SANDBOX_BASE_URL` (default
  `https://api-hft.upstox.com`) before anything else.
- `has surrounding whitespace True` → cause 5; that alone produces 401.
- `sandbox-test` reporting `credential_accepted: true` while the engine 401s → the *history*
  endpoint accepts the token but the *order* endpoint does not. That points at IP restriction or
  order-API entitlement, not the token value.
- `sandbox-test` also rejected → the credential itself, or the host.

**FIX**
Correct the specific cause found. For the host:
```
UPSTOX_SANDBOX_BASE_URL=https://api-hft.upstox.com
```
For IP restriction, register the current egress IP with Upstox — there is no client-side fix.

**VERIFICATION**
```bash
python -m app sandbox-test     # read-only; look for "credential_accepted": true
```
A restart is required regardless, because the latch is per-object (entry 8).

**WHAT NOT TO CHANGE**
**Do not remove the `available = False` latch or the 401/403 check.** It is what stops the engine
hammering a rejecting API and what prevents an order being sent with credentials the provider has
already refused. Do not add retry-on-401. Do not switch to a live-looking host to "test whether it
is the sandbox that is broken" — that is precisely the mistake the `SANDBOX_BASE_URL_REVIEW`
warning exists to catch.

---

## 8. `Sandbox broker disabled after an auth failure` (repeated)

**SYMPTOM**
Every subsequent broker call raises:
```
SandboxAuthError: Sandbox broker disabled after an auth failure
```

**WHAT IT MEANS**
This is **not a new failure**. `_request` begins with
`if not self.available: raise SandboxAuthError(...)`, and `available` was set to `False` by an
earlier 401/403. Every message after the first is downstream noise.

**LIKELY ROOT CAUSES**
1. You are reading the *last* error instead of the *first*.
2. The engine was never restarted after fixing the underlying credential problem.

**EXACT DIAGNOSTIC COMMANDS**
```bash
grep -n '"event": "SANDBOX_AUTH_FAILURE"' logs/*.jsonl | head -3
grep -n "Sandbox credential rejected" logs/*.jsonl | head -3
```

**HOW TO INTERPRET OUTPUT**
The **first** `SANDBOX_AUTH_FAILURE` carries the real `http_status` (401 or 403). Everything after
it is the latch. Diagnose entry 7 using that first event.

**FIX**
Fix the underlying cause (entry 7), then **restart the process** — the latch lives for the lifetime
of the `UpstoxSandboxBroker` object and there is no re-authentication path.

**VERIFICATION**
```bash
python -m app sandbox-test
# then restart and confirm no new SANDBOX_AUTH_FAILURE appears
```

**WHAT NOT TO CHANGE**
Do not add an auto-re-auth or a latch reset. Failing closed until an operator restarts is the
intended behaviour for a rejected credential; silently retrying with refused credentials is how an
account ends up in a bad state. Do not clear the submission journal to work around it (entry 12).

---

## 9. `SandboxRequestError: Sandbox API unreachable (network error)`

**SYMPTOM**
```
SandboxRequestError: Sandbox API unreachable (network error)
```

**WHAT IT MEANS**
`httpx` raised an `HTTPError` — DNS failure, TLS failure, timeout or connection refused. Crucially,
**writes never retry** (`attempts = 2 if method == "GET" else 1`, line 247), so if this happened on
`POST /v3/order/place`, **the remote outcome is unknown**: the order may or may not exist.

**LIKELY ROOT CAUSES**
1. No outbound network / firewall blocking `api-hft.upstox.com` or `api.upstox.com`.
2. TLS verification failure (corporate proxy, missing CA bundle).
3. DNS resolution failure.
4. Timeout under load (the broker uses `self._timeout`).
5. Upstox-side outage.

**EXACT DIAGNOSTIC COMMANDS**
```bash
python -c "import socket; print(socket.gethostbyname('api-hft.upstox.com'))"
python -c "import httpx; r=httpx.get('https://api-hft.upstox.com', timeout=10); print('reachable', r.status_code)"
python -c "
from app.config.settings import Settings as S
s=S.from_env()
for h in (s.upstox_sandbox_base_url, s.upstox_sandbox_order_history_url):
    print(h)
"
```
```powershell
Test-NetConnection api-hft.upstox.com -Port 443
Resolve-DnsName api.upstox.com
```

**HOW TO INTERPRET OUTPUT**
- DNS raises → cause 3.
- `httpx.ConnectError` with an SSL message → cause 2; a proxy is intercepting TLS.
- Connects but times out → cause 4 or 5.
- Both hosts resolve and connect, yet the broker fails → check whether a proxy env var
  (`HTTPS_PROXY`) is set only in some shells.

**FIX**
Restore connectivity, or configure the proxy consistently. **Then reconcile before resuming** —
see entry 12, because a failed POST leaves an ambiguous order.

**VERIFICATION**
```bash
python -m app sandbox-test
sqlite3 "file:trading_bot.db?mode=ro" "SELECT intent_id, state FROM sandbox_orders WHERE state NOT IN ('FILLED','CANCELLED','REJECTED');"
```

**WHAT NOT TO CHANGE**
**Do not add retries to POST/PUT/DELETE.** The single-attempt behaviour is explicit duplicate-order
protection — the docstring states *"the remote outcome could already exist (duplicate-order
safety)"*. Retrying a place-order is how you buy twice. Do not increase the timeout so far that the
engine stalls; use reconciliation instead.

---

## 10. Sandbox probe returns 404 / 400

**SYMPTOM**
`sandbox-test` or a reconciliation call returns HTTP 404 or 400.

**WHAT IT MEANS**
**Probably success.** `classify_history_probe` (`app/brokers/sandbox.py:134`) treats
`400`/`404` **carrying the documented `UDAPI100010`** as:
```python
{"credential_accepted": True, "class": "documented_not_found", ...}
```
The credential was accepted; the probe order id simply does not exist. *This is the most
mis-diagnosed Upstox response in the system.*

> **UNVERIFIED:** no live response was observed here. The mapping is read from source.

**LIKELY ROOT CAUSES**
1. Nothing is wrong — this is the expected probe outcome.
2. A genuine 404 *without* `UDAPI100010` → wrong URL path or host.
3. Order history retention expired — documented as **one trading day**
   (`app/brokers/sandbox.py` docstring line 16), so yesterday's orders legitimately 404.
4. The history endpoint host differs from the order host: history is
   `https://api.upstox.com/v2/order/history`, orders are `https://api-hft.upstox.com`.

**EXACT DIAGNOSTIC COMMANDS**
```bash
python -m app sandbox-test
python -c "from app.config.settings import Settings as S; s=S.from_env(); print('orders ->', s.upstox_sandbox_base_url); print('history ->', s.upstox_sandbox_order_history_url)"
sqlite3 "file:trading_bot.db?mode=ro" "SELECT ts, kind, payload FROM system_events WHERE kind LIKE '%SANDBOX%' ORDER BY rowid DESC LIMIT 10;"
```

**HOW TO INTERPRET OUTPUT**
- `"credential_accepted": true` with `class: documented_not_found` → **your credentials work.**
  Stop diagnosing auth.
- `credential_accepted: false` with `class: unexpected` and a 404 → the URL or host is wrong; the
  body did not carry `UDAPI100010`.
- A 404 only for orders older than one trading day → cause 3, expected.

**FIX**
Usually none. For cause 2, restore the documented defaults:
```
UPSTOX_SANDBOX_ORDER_HISTORY_URL=https://api.upstox.com/v2/order/history
UPSTOX_SANDBOX_BASE_URL=https://api-hft.upstox.com
```

**VERIFICATION**
```bash
python -m app sandbox-test     # expect credential_accepted true
```

**WHAT NOT TO CHANGE**
**Do not rotate the token in response to a 404.** You will burn a working credential chasing a
non-existent problem. Do not "fix" `classify_history_probe` to treat 404 as failure — that would
make every successful credential probe look broken. Do not assume a 404 on the *history* host says
anything about the *order* host; they are different servers.

---

## 11. Signals are generated but no orders are submitted

**SYMPTOM**
Strategies evaluate, `signals` increments, but `submitted` stays 0 and no orders appear.

**WHAT IT MEANS**
The hard risk gate rejected the order, or sizing produced zero quantity. The engine records the
reason rather than silently dropping it.

**LIKELY ROOT CAUSES**
1. A hard limit was hit — see the closed set of reasons below.
2. `position_size` or the allocator allowance produced a zero quantity.
3. The 3.0.5 quality gates rejected the trade (net edge, quality score, spread, liquidity).
4. An emergency stop is persisted.
5. The protective-exit path handled the candle instead.

**EXACT DIAGNOSTIC COMMANDS**
```bash
sqlite3 "file:trading_bot.db?mode=ro" "SELECT ts, kind, payload FROM system_events WHERE kind IN ('RISK_REJECT','EMERGENCY_STOP') ORDER BY rowid DESC LIMIT 20;"
sqlite3 "file:trading_bot.db?mode=ro" "SELECT payload FROM engine_state WHERE key='emergency_stop';"
python -c "from app.config.settings import Settings as S; from app.risk.limits import HardRiskLimits as H; print(H.from_settings(S.from_env()))"
curl -s -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/api/v2/risk
```

**HOW TO INTERPRET OUTPUT**
`HardRiskLimits.check` (`app/risk/limits.py:53`) returns exactly one of:

| reason | meaning |
| --- | --- |
| `max_position_value_exceeded` | `open_position_value + notional > max_position_value` |
| `max_open_positions_reached` | `open_positions >= max_open_positions` |
| `max_daily_loss_reached` | `realized_today + unrealized <= -max_daily_loss` |
| `max_trades_per_day_reached` | `trades_today >= max_trades_per_day` |
| `within_limits` | allowed |
| `risk_reducing_exit_allowed` | a SELL — always allowed |

**A limit of `0` means unlimited/disabled, not zero.** So `HardRiskLimits(max_position_value=0.0,
max_open_positions=0, ...)` blocks nothing.

If all limits are 0 and orders still do not appear, look at the 3.0.5 gates:
`net_edge_threshold`, `min_quality_score`, `max_spread_bps`, `min_liquidity_score`
(`app/config/settings.py`), implemented in `app/risk/net_edge.py`. These are documented as
*"additive, fail-closed"* — they reject by default when data is insufficient.

**FIX**
Adjust the specific limit that fired, or accept the rejection as correct behaviour. To relax:
```
MAX_POSITION_VALUE=200000
MAX_OPEN_POSITIONS=5
```

**VERIFICATION**
```bash
curl -s -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/api/v2/risk
python -m pytest tests/test_v1_engine.py -q
```

**WHAT NOT TO CHANGE**
**Do not edit `app/risk/limits.py` to make a rejection pass.** `HardRiskLimits` is a frozen
dataclass built only from settings precisely so no learning output can mutate it — the module
docstring states limits are *"enforced OUTSIDE any learning/adaptive layer"*. Do not remove the
SELL bypass either; it exists so limits *"cannot trap the account in a position"*. If a gate is
wrong, change the configured threshold, not the gate.

---

## 12. Orders stuck in a non-final state after a crash

**SYMPTOM**
After a restart, some orders are neither `FILLED`, `CANCELLED` nor `REJECTED`, and
`unresolved_intents` is non-zero.

**WHAT IT MEANS**
The process died between sending an order and recording its outcome. `SubmissionJournal`
(`app/engine/state.py:53`) recorded the intent *before* placement, so the ambiguity is visible
rather than lost. **This is the safety mechanism working**, not a corruption.

**LIKELY ROOT CAUSES**
1. Crash, `kill -9`, or power loss mid-POST.
2. A network error on `POST /v3/order/place` (entry 9) — writes never retry, so the outcome is
   genuinely unknown.
3. Reconciliation never ran before shutdown.

**EXACT DIAGNOSTIC COMMANDS**
```bash
sqlite3 "file:trading_bot.db?mode=ro" "SELECT intent_id, state FROM sandbox_orders;"
sqlite3 "file:trading_bot.db?mode=ro" "SELECT * FROM sandbox_transitions ORDER BY rowid DESC LIMIT 20;"
python -m app sandbox-test
```

**HOW TO INTERPRET OUTPUT**
- `FINAL_STATUSES = {"FILLED", "CANCELLED", "REJECTED"}`
  (`app/engine/sandbox_engine.py:42`). Anything else is unresolved.
- `SandboxEngine.status()` exposes `unresolved_intents` (line 1005), backed by
  `SubmissionJournal.unresolved()` (line 134).
- If `sandbox_transitions` shows a later state than `sandbox_orders`, reconciliation partially ran.
- **Remember order history is retained for one trading day.** If the crash was yesterday, the
  remote record may be gone and the local journal is all you have.

**FIX**
Reconcile, do not guess:
```bash
python -c "from app.config.settings import Settings as S; print(S.from_env().upstox_sandbox_order_history_url)"
```
`SandboxEngine.reconcile()` (`app/engine/sandbox_engine.py:903`) walks open orders against the
history endpoint and records transitions. On startup,
`_surface_unresolved_intents()` (line 266) reports what is still ambiguous.

**VERIFICATION**
```bash
sqlite3 "file:trading_bot.db?mode=ro" "SELECT COUNT(*) FROM sandbox_orders WHERE state NOT IN ('FILLED','CANCELLED','REJECTED');"
```
Expect `0` after reconciliation, or a documented reason why an order cannot be resolved.

**WHAT NOT TO CHANGE**
**Do not delete rows from `sandbox_orders` or clear the journal to "clean up".**
`resubmission_forbidden()` (line 130) uses those rows to prevent duplicate orders; clearing them
means a restart can re-send an order that already exists at the broker. Do not hand-edit `state`
to `CANCELLED` — that fabricates a broker outcome you never observed. Back up first
(`python -m app backup backup --label pre-recovery`).

---

## 13. Engine refuses to start after an emergency stop

**SYMPTOM**
The engine exits immediately or does nothing on start.

**WHAT IT MEANS**
The emergency stop is **persisted**, and `_check_emergency()`
(`app/engine/sandbox_engine.py:312`) reads it on every loop iteration. A restart does not clear it
— by design, so a stop survives a crash.

**LIKELY ROOT CAUSES**
1. A stop was pressed (dashboard, desktop app, or API) and never resumed.
2. The engine itself called `emergency_stop()`.
3. A stop was persisted by a previous incident and forgotten.

**EXACT DIAGNOSTIC COMMANDS**
```bash
sqlite3 "file:trading_bot.db?mode=ro" "SELECT payload FROM engine_state WHERE key='emergency_stop';"
sqlite3 "file:trading_bot.db?mode=ro" "SELECT ts, kind, payload FROM system_events WHERE kind IN ('EMERGENCY_STOP','EMERGENCY_STOP_REQUESTED','ENGINE_RESUME','ENGINE_RESUME_REQUESTED') ORDER BY rowid DESC LIMIT 10;"
curl -s -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/api/v2/risk | python -c "import sys,json; print('emergency_stop =', json.load(sys.stdin)['emergency_stop'])"
```

**HOW TO INTERPRET OUTPUT**
`{"stopped": true, "ts": "...", "reason": "..."}` — the `reason` field tells you who stopped it
(`manual`, `dashboard`, …). The `system_events` trail shows whether a resume was ever requested.

**FIX**
Deliberately resume — see `FULL_TROUBLESHOOTING_GUIDE.md` §17.5 for the full safe-restart
sequence:
```bash
python -m app paper-test          # confirm the pipeline is sane
python -m app monitor             # confirm host/DB health
python -m app sandbox-test        # confirm credentials
curl -s -X POST -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/engine/resume
python -m app engine --max-candles 20     # bounded first run
```

**VERIFICATION**
```bash
sqlite3 "file:trading_bot.db?mode=ro" "SELECT payload FROM engine_state WHERE key='emergency_stop';"
```
Expect `{"stopped": false}` and an `ENGINE_RESUME_REQUESTED` event.

**WHAT NOT TO CHANGE**
**Do not delete the `emergency_stop` row directly in SQLite.** Use the resume path so the action is
recorded in `system_events`; an unaudited manual clear leaves no trace of who restarted a trading
engine or when. Do not resume before verifying broker state if the stop was caused by an incident
(entry 12).

---

## 14. No candles arriving from the Upstox feed

**SYMPTOM**
`market-stream` or the engine connects but emits nothing.

**WHAT IT MEANS**
Either no data is arriving, or data is arriving and being **deliberately suppressed**. The second
case is common and is not a fault.

**LIKELY ROOT CAUSES**
1. Market closed — the feed is quiet and `LiveCandleBuilder.stale()` suppresses emission.
2. `MARKET_DATA_SYMBOLS` empty or malformed.
3. Credential rejected at the authorize step.
4. `MARKET_DATA_TIMEFRAME` mismatched with `DEFAULT_TIMEFRAME`.
5. Protobuf decode failures dropping every frame (entry 16).
6. Subscription refused because the streaming universe was too broad.

**EXACT DIAGNOSTIC COMMANDS**
```bash
python -c "from app.config.settings import Settings as S; s=S.from_env(); print('symbols', repr(s.market_data_symbols)); print('timeframe', repr(s.market_data_timeframe)); print('mode', s.market_data_mode); print('token', 'SET' if s.market_data_access_token.strip() else 'unset')"
python -m app upstox-test
python -m app market-stream
```
```bash
grep -cE "feed_stale|UPSTOX_DECODE_ERROR|UPSTOX_RECONNECT" logs/*.jsonl 2>/dev/null
```

**HOW TO INTERPRET OUTPUT**
- Verified without symbols, **exit 2**:
  `{"ok": false, "error": "Set MARKET_DATA_SYMBOLS (e.g. NSE_EQ|INE002A01018) for the Upstox data feed"}`
- `feed_stale` present → the feed is quiet, not broken. `stale_after` defaults to **90 s**
  (`app/data/upstox_stream.py:235`); the loop logs `feed_stale` and emits nothing (line 540).
- `MARKET_DATA_TIMEFRAME` empty → candles cannot be built; `.env.example` notes it *"Must equal
  DEFAULT_TIMEFRAME"* and `1m` is the smallest supported live timeframe.
- `mode` must be `ltpc` or `full`; anything else is rejected by `Settings`.

**FIX**
```
MARKET_DATA_SYMBOLS=NSE_EQ|INE002A01018
MARKET_DATA_TIMEFRAME=1m
DEFAULT_TIMEFRAME=1m
MARKET_DATA_ACCESS_TOKEN=<your token>
```

**VERIFICATION**
```bash
python -m app upstox-test     # read-only credential + connectivity check
python -m app market-stream   # should print normalized events during market hours
```

**WHAT NOT TO CHANGE**
**Do not disable the staleness gate** to force candles out — a candle built from a stale price is
fabricated data, and it will flow into strategy decisions. Do not subscribe to the whole instrument
universe to "make sure you get something"; `Settings.streaming_universe` is documented as
*"NEVER the whole universe"* and broad subscriptions are refused by design. Do not switch
`MARKET_DATA_MODE` blindly — `ltpc` and `full` build candles differently.

---

## 15. Feed connects, then drops repeatedly

**SYMPTOM**
`UPSTOX_RECONNECT` appears many times; `reconnects` climbs.

**WHAT IT MEANS**
The WebSocket is being closed and re-established. `UpstoxStream.stream()`
(`app/data/upstox_stream.py:470`) handles this with re-auth, resubscribe and **bounded exponential
backoff**.

**LIKELY ROOT CAUSES**
1. Network instability or a proxy closing idle sockets.
2. The single-use `wss://` URL expiring — re-authorization is required each reconnect.
3. Provider-side disconnects.
4. A subscription the provider rejects, causing an immediate close.

**EXACT DIAGNOSTIC COMMANDS**
```bash
grep -E '"event": "(UPSTOX_RECONNECT|UPSTOX_RESUBSCRIBE)"' logs/*.jsonl | tail -20
grep -o '"connect_latency_ms": [0-9.]*' logs/*.jsonl | tail -10
```

**HOW TO INTERPRET OUTPUT**
- Backoff is `min(max_backoff, 2 ** min(attempts, 6)) + random()` (line 590), capped at
  `max_backoff = 60.0` (line 234). So gaps between attempts should grow and then plateau near 60 s.
  **Rapid, evenly-spaced reconnects are not this backoff** — look for a second failure mode.
- `UPSTOX_RESUBSCRIBE` immediately after each connect → the connection succeeds but the
  subscription is rejected. Check the instrument keys.
- Rising `connect_latency_ms` → network degradation, not application logic.
- The provider exposes `reconnects` and `connect_latency` in its status dict (lines 456, 462).

**FIX**
Fix the network/proxy, or correct the subscription. For an idle-timeout proxy, the reconnect loop
already recovers — the question is whether candles are being lost in the gaps (check
`stale_feed`).

**VERIFICATION**
```bash
python -m app market-stream   # watch for a stable connection through a candle boundary
```

**WHAT NOT TO CHANGE**
Do not remove the `+ random()` jitter — it prevents a thundering-herd reconnect. Do not raise
`max_backoff` so high that a genuinely recovered feed is not rediscovered for minutes. Do not
cache and reuse the `wss://` URL; the docstring states it is **single-use** and never logged.

---

## 16. `UPSTOX_DECODE_ERROR` flooding the log

**SYMPTOM**
Many `UPSTOX_DECODE_ERROR` lines, and few or no candles.

**WHAT IT MEANS**
Protobuf decoding failed. `UpstoxProtobufDecoder.decode`
(`app/data/upstox_stream.py:110`) catches `DecodeError`, logs
`{"reason": "protobuf"}` (line 117) and **drops the frame** — the stream continues. A separate
`{"reason": "text_frame"}` variant exists at line 545.

**LIKELY ROOT CAUSES**
1. `protobuf` runtime major version incompatible with the generated bindings.
2. The provider changed its schema relative to the bundled `.proto`.
3. A text frame arrived where binary was expected (`reason: text_frame`).
4. Truncated frames from an unstable connection (entry 15).

**EXACT DIAGNOSTIC COMMANDS**
```bash
python -c "import google.protobuf as p; print('protobuf runtime', p.__version__)"
python -c "from app.data.upstox_proto import MarketDataFeed_pb2; print('bindings import OK')"
grep -o '"reason": "[a-z_]*"' logs/*.jsonl | sort | uniq -c
ls -la app/data/upstox_proto/
```

**HOW TO INTERPRET OUTPUT**
- `reason: protobuf` dominant → schema/runtime mismatch (cause 1 or 2).
- `reason: text_frame` dominant → the connection is delivering text, usually an error or
  keep-alive message rather than market data; check what the provider is actually sending.
- `requirements.txt` pins `protobuf>=5.29,<7` and comments that the bindings were *"generated with
  protoc from the 5.29 line"*. A runtime far outside that range is the first suspect.
- `app/data/upstox_stream.py:13` records the schema file
  `app/data/upstox_proto/MarketDataFeed.proto` and its sha256 — compare it if you suspect the
  schema changed.

**FIX**
```bash
python -m pip install "protobuf>=5.29,<7"
python -c "from app.data.upstox_proto import MarketDataFeed_pb2; print('OK')"
```

**VERIFICATION**
```bash
python -m app market-stream     # decode errors should stop; candles should appear
python -m pytest tests/test_upstox_live.py tests/test_upstox_v063.py -q
```

**WHAT NOT TO CHANGE**
**Do not silence the warning.** A dropped frame is silently lost data; the log line is the only
evidence it happened. Do not hand-edit `MarketDataFeed_pb2.py` — it is generated, and the
companion `.proto` plus its recorded sha256 are the authority. Note there is **no circuit-breaker**
that stops the stream after repeated decode failures (§18.9 of the guide), so a systematic mismatch
looks like "no candles" rather than a loud error — the log count is your only signal.

---

## 17. Database appears empty / data "vanished"

**SYMPTOM**
Queries return nothing, or previously visible history is gone.

**WHAT IT MEANS**
You are almost certainly connected to a **different database file**. `DATABASE_URL` defaults to
`sqlite:///trading_bot.db` (`app/config/settings.py:86`) — a **relative** path resolved against the
current working directory, not the repo root.

**LIKELY ROOT CAUSES**
1. The command was run from a different directory, creating a second empty database.
2. `DATABASE_URL` was overridden in the environment.
3. A backup was restored over the live file.
4. The `-wal` file was lost (a bare `.db` copy), losing committed transactions.

**EXACT DIAGNOSTIC COMMANDS**
```bash
python -c "from app.config.settings import Settings as S; import os; print(os.path.abspath(S.from_env().database_url.replace('sqlite:///','')))"
find / -name "trading_bot.db*" -not -path "*/proc/*" 2>/dev/null
ls -la trading_bot.db*
sqlite3 "file:trading_bot.db?mode=ro" "SELECT COUNT(*) FROM sandbox_orders; SELECT COUNT(*) FROM signals;"
```

**HOW TO INTERPRET OUTPUT**
- Verified: the default resolves to `/home/user/AXIOM/trading_bot.db` **when run from the repo
  root**. Run the same command elsewhere and the path changes — that is the bug.
- `find` returning **multiple** `trading_bot.db` files confirms cause 1. Compare their sizes and
  mtimes; the larger, older one is your data.
- A `.db` with no accompanying `-wal`/`-shm` in a directory where the engine recently ran suggests
  cause 4.

**FIX**
Use an absolute path so cwd cannot matter:
```
DATABASE_URL=sqlite:////full/path/to/trading_bot.db
```
(note four slashes: `sqlite:///` plus an absolute path).

**VERIFICATION**
```bash
python -c "from app.config.settings import Settings as S; import os; print(os.path.abspath(S.from_env().database_url.replace('sqlite:///','')))"
sqlite3 "file:<that path>?mode=ro" "SELECT COUNT(*) FROM sandbox_orders;"
```

**WHAT NOT TO CHANGE**
**Do not delete the "empty" database before confirming it is not the one in use.** Do not copy only
the `.db` file when moving a database — WAL means committed data can live in `-wal`; use
`python -m app backup backup` instead, which uses SQLite's online-backup API and verifies the
result. Do not assume a restore succeeded — `RecoveryManager.restore` refuses unverified backups,
so a "failed" restore may have correctly left your data alone.

---

## 18. `database is locked`

**SYMPTOM**
```
sqlite3.OperationalError: database is locked
```

**WHAT IT MEANS**
Another connection holds the write lock beyond the 10-second timeout. `Database.__init__`
(`app/database/database.py:9`) uses `sqlite3.connect(path, timeout=10, check_same_thread=False)`
and serializes writes on an `RLock` — so this is **cross-process** contention, not intra-process.

**LIKELY ROOT CAUSES**
1. The engine and the API server both running against the same file.
2. A `sqlite3` CLI session left open with a write transaction.
3. A crashed process still holding the lock (rare; WAL recovers).
4. An editor or sync tool holding the file.

**EXACT DIAGNOSTIC COMMANDS**
```bash
python -m app monitor
ls -la trading_bot.db*
sqlite3 "file:trading_bot.db?mode=ro" "PRAGMA journal_mode;"
```
```powershell
Get-Process python | Select-Object Id, StartTime, Path
```

**HOW TO INTERPRET OUTPUT**
- `journal_mode` should be `wal` (set at `app/database/database.py:18`).
- More than one long-running `python` process → cause 1. Arena is single-writer by design; the API
  server calls `Repository.release_writer()` on shutdown (`app/api/server.py:40`).
- A `-wal` file that never shrinks suggests a writer that is not checkpointing.

**FIX**
Run **one** writer. Keep the API server read-mostly, or point it at a copy:
```bash
python -m app backup backup --label api-copy
```
Close stray `sqlite3` sessions.

**VERIFICATION**
```bash
python -m app monitor          # database_ok must be true
python -m pytest tests/test_runtime_database_api.py tests/test_v2_migrations.py -q
```

**WHAT NOT TO CHANGE**
Do not raise the `timeout` to a large value to paper over contention — that converts a fast,
diagnosable error into a hung process. Do not switch `journal_mode` away from WAL, and do not lower
`PRAGMA synchronous` from `FULL` (`app/database/database.py:19`) to gain speed; that trades
durability of your order audit trail for throughput. Do not delete `-wal`/`-shm` while a writer is
live.

---

## 19. Desktop UI shows stale or blank data

**SYMPTOM**
Pages display old numbers, or nothing, while the backend is healthy.

**WHAT IT MEANS**
The desktop client is a **polling viewer** with no cache invalidation on failure — if a request
fails, the page keeps whatever it last rendered. `desktop/app.py` docstring (line 4) describes the
app as limited to viewing plus the persisted STOP/RESUME flags, and line 272 states it is
*"a viewer + emergency stop only"*.

**LIKELY ROOT CAUSES**
1. The backend stopped and the UI kept the last payload.
2. A request is failing silently (401/503) and the page was never updated.
3. The wrong `ARENA_BACKEND_URL`.
4. A slow poll blocking the GUI thread.

**EXACT DIAGNOSTIC COMMANDS**
Test the transport without Qt (`desktop/api_client.py` does not import PySide6):
```bash
python -c "
from desktop.api_client import ArenaApiClient
import os
c = ArenaApiClient(os.getenv('ARENA_BACKEND_URL','http://127.0.0.1:8000'), os.getenv('ARENA_API_TOKEN',''))
for name, fn in [('overview',c.overview),('risk',c.risk),('performance',c.performance)]:
    try: print(name,'OK', str(fn())[:60])
    except Exception as e: print(name,'FAIL',type(e).__name__,e)
"
curl -s -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/api/v2/overview
```

**HOW TO INTERPRET OUTPUT**
- Client `OK` and matching curl → the data is current; the UI is not refreshing.
- Client `FAIL 401` → token mismatch (entry 5).
- Client `FAIL 503` → server has no `API_TOKEN` (entry 4).
- Client `OK` but curl differs → you are pointed at different backends; check `ARENA_BACKEND_URL`.
- `desktop/app.py:37–38` reads `ARENA_BACKEND_URL` (default `http://127.0.0.1:8000`) and
  `ARENA_API_TOKEN` (default `""`) **once, at startup** — changing them requires an app restart.

**FIX**
Restart the desktop app after correcting the environment:
```powershell
$env:ARENA_BACKEND_URL = "http://127.0.0.1:8000"
$env:ARENA_API_TOKEN   = "<server API_TOKEN>"
python -m desktop
```

**VERIFICATION**
The Command Center page's *Emergency stop* row (`desktop/app.py:362`, rendering `ACTIVE` or
`clear`) must match the API:
```bash
curl -s -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/api/v2/risk
```
Both derive from the same persisted flag, so agreement proves the UI is live.

**WHAT NOT TO CHANGE**
**Do not add order entry, risk overrides or strategy editing to the desktop client.** It is a
viewer by design; Arena owns trading state, risk and execution. Do not have the UI cache and
"replay" data to avoid showing gaps — an explicitly offline state is more honest than a plausible
stale one.

> **UNVERIFIED:** Qt rendering, layout and GUI-thread behaviour were never exercised while writing
> this (no display server available). If the window freezes during a slow poll, instrument the poll
> path yourself; there is no verified procedure here for it.

---

## 20. Desktop cannot reach the backend

**SYMPTOM**
`ConnectionRefusedError`, `URLError`, or a timeout in the desktop app.

**WHAT IT MEANS**
`ArenaApiClient._request` (`desktop/api_client.py:21`) calls
`urllib.request.urlopen(request, timeout=self.timeout)` (line 26). The default timeout is **10.0 s**
(line 16). Nothing is listening, or the network path is blocked.

**LIKELY ROOT CAUSES**
1. The API server is not running.
2. Wrong `ARENA_BACKEND_URL` (port, host, or scheme).
3. The server is bound to `127.0.0.1` but the client is on another machine.
4. A firewall blocking the port.
5. The server is up but slow (10 s exceeded).

**EXACT DIAGNOSTIC COMMANDS**
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/health
ss -ltnp | grep 8000
python -c "
from desktop.api_client import ArenaApiClient
try: ArenaApiClient('http://127.0.0.1:8000','x').overview()
except Exception as e: print(type(e).__name__, e)
"
```
```powershell
Test-NetConnection -ComputerName 127.0.0.1 -Port 8000
Get-NetTCPConnection -LocalPort 8000 -State Listen
```

**HOW TO INTERPRET OUTPUT**
| Exception | Meaning |
| --- | --- |
| `ConnectionRefusedError` / `URLError` | nothing listening — start the server |
| `HTTPError 401` | reachable, token wrong (entry 5) |
| `HTTPError 503` | reachable, server unconfigured (entry 4) |
| `TimeoutError` / socket timeout | slow or firewalled |
| `HTTPError 404` | wrong base URL or path |

`run.py:153` starts uvicorn with defaults `--host 127.0.0.1 --port 8000`. A client on another
machine cannot reach a loopback bind — that is cause 3.

**FIX**
```bash
python run.py --mode api --host 127.0.0.1 --port 8000
```
For a remote client, bind deliberately and protect the port:
```bash
python run.py --mode api --host 0.0.0.0 --port 8000
```

**VERIFICATION**
```bash
curl -s http://127.0.0.1:8000/health
```
Expect the verified body with `"version":"3.0.5"`.

**WHAT NOT TO CHANGE**
**Do not bind `0.0.0.0` without setting `API_TOKEN`.** Note that `loopback_or_bearer`
(`app/api/auth.py:47`) serves the dashboard **page** to loopback clients without a bearer — binding
wider removes that convenience but also removes the assumption that every client is local. Do not
raise the client timeout so far that the UI hangs on every failed poll. Do not put the token in the
URL.

---

## 21. A test fails in the full suite but passes alone

**SYMPTOM**
`pytest tests/<FILE>.py` passes; `pytest` fails on the same test.

**WHAT IT MEANS**
Test pollution — shared state leaking between tests. In this project the usual suspects are the
filesystem, the environment, and the default database path.

**LIKELY ROOT CAUSES**
1. A test created `trading_bot.db` in the repo root (the default is relative — entry 17).
2. A test set an environment variable that was not restored.
3. A test wrote into `data/`, `logs/` or `results/`.
4. Module-level import side effects.

**EXACT DIAGNOSTIC COMMANDS**
Substitute the real file and test ids for `<FILE>` / `<TEST>` below — they are placeholders, not
files in this repository.

```bash
python -m pytest "tests/<FILE>.py::<TEST>" -q                  # alone
python -m pytest tests/<FILE>.py -q                            # whole file
python -m pytest -q -x --lf                                    # last failures only
python -m pytest -q -p no:cacheprovider                        # rule out the cache plugin
git status --porcelain                                         # what did the run create?
```

**HOW TO INTERPRET OUTPUT**
- Passes alone, fails in the file → pollution within the file; bisect by test.
- Passes per-file, fails in the suite → cross-file pollution; bisect by file.
- `git status` showing new `trading_bot.db` / `data/*` after a run → a test is writing to the
  default relative path.
- Verified baselines: **864 passed, 10 skipped** with `requirements.txt` alone; **866 passed,
  8 skipped** once `requirements-kronos.txt` is installed; **872 passed, 2 skipped** with
  `AXIOM_KRONOS_SOURCE` also set. All three sum to 874 collected tests. **The total depends on
  installed dependencies** — installing `numpy` or `torch` un-skips tests via
  `pytest.importorskip`.
  Compare against a baseline taken in the *same* environment, and read skip reasons with
  `python -m pytest -rs -q` before treating a changed total as a regression.

**FIX**
Make the test isolate itself — use `tmp_path` for any database, and `monkeypatch.setenv` (which
pytest reverts automatically) for environment changes.

**VERIFICATION**
```bash
python -m pytest -q                      # full suite, matches your baseline
python -m pytest -q -p no:randomly       # order-independent?
```

**WHAT NOT TO CHANGE**
Do not add `@pytest.mark.skip` to make the suite green — that hides the pollution. Do not reorder
tests to dodge it. Do not commit generated `trading_bot.db` or `data/` artifacts; check
`.gitignore`.

---

## 22. `AttributeError: 'State' object has no attribute 'repository'`

**SYMPTOM**
Your own API test or script fails with that `AttributeError`.

**WHAT IT MEANS**
`app.state.repository`, `app.state.broker` and `app.state.simulator` are assigned **inside the
`lifespan` context manager** (`app/api/server.py:34`). If the lifespan never runs, they do not
exist. This was reproduced while writing this guide.

**LIKELY ROOT CAUSES**
1. `TestClient` used without a `with` block — the lifespan only runs on context entry.
2. Calling route functions directly instead of through the ASGI app.

**EXACT DIAGNOSTIC COMMANDS**
```bash
grep -n "app.state.repository" app/api/server.py
grep -rn "TestClient(" tests/ | head -5
```

**HOW TO INTERPRET OUTPUT**
`app/api/server.py:34` is the only assignment, and it is inside
`async def lifespan(app)` (line 23). Any code path that reaches a protected route without entering
that context will fail this way.

**FIX**
```python
from fastapi.testclient import TestClient
from app.api.server import create_app
from app.config.settings import Settings

with TestClient(create_app(Settings.from_env())) as client:   # `with` is required
    r = client.get("/api/v2/overview", headers={"Authorization": "Bearer <token>"})
    print(r.status_code, r.json())
```
Verified working: `/health` → 200, `/api/v2/overview` → 200 with a correct bearer, 401 without.

**VERIFICATION**
```bash
python -m pytest tests/test_runtime_database_api.py tests/test_v2_api_cli.py -q
```

**WHAT NOT TO CHANGE**
Do not move the `app.state` assignments out of `lifespan` to make a broken harness work — the
lifespan is also what closes the database and releases the writer
(`repository.release_writer()`, `database.close()` at lines 40–41). Bypassing it leaks connections.
Fix the harness.

---

## 23. Logs are missing entirely

**SYMPTOM**
`logs/bot.jsonl` does not exist, or is not being written.

**WHAT IT MEANS**
Only `run.py` configures a file handler. `run.py:152` is the single call site:
```python
configure(settings.log_level, ROOT / "logs/bot.jsonl")
```
`app/__main__.py` contains **no** `configure(...)` call (verified by grep). So operational CLI
commands log to **stderr only**.

**LIKELY ROOT CAUSES**
1. You ran `python -m app …` and expected a file.
2. `configure()` was already called, so the `if not logger.handlers` guard
   (`app/logging/logger.py:22`) skipped adding handlers.
3. `LOG_LEVEL` set above the level you are looking for.
4. The process crashed before the first log line.

**EXACT DIAGNOSTIC COMMANDS**
```bash
grep -n "configure(" run.py app/__main__.py app/logging/logger.py
ls -la logs/
python -c "import logging; l=logging.getLogger('bot'); print('handlers', l.handlers, 'level', l.level, 'propagate', l.propagate)"
```

**HOW TO INTERPRET OUTPUT**
- `configure(` appearing only in `run.py` and `logger.py` confirms the asymmetry.
- An empty `logs/` directory after `python -m app` runs is **expected**, not a fault.
- `logger.propagate` is set to `False` in `configure()`, so records do not reach the root logger —
  a root-logger handler will not capture Arena events.

**FIX**
Capture stderr:
```bash
python -m app engine 2>&1 | tee logs/engine.jsonl
```
```powershell
python -m app engine 2>&1 | Tee-Object -FilePath logs\engine.jsonl
```
Or run through `run.py`, which writes the file itself.

**VERIFICATION**
```bash
wc -l logs/engine.jsonl
head -1 logs/engine.jsonl | python -m json.tool
```
Each line must parse as JSON with `timestamp`, `level`, `component`, `event`.

**WHAT NOT TO CHANGE**
Do not widen `FIELDS` in `app/logging/logger.py` to log arbitrary `extra=` keys. The whitelist is
a deliberate leak guard — the docstring says it *"Never accepts arbitrary credential/config
payloads"*. Do not set `logger.propagate = True` to route logs elsewhere; that risks credentials
reaching a root handler with a different formatter. Capture stderr instead.

---

## 24. Daily loss limit resets at the wrong time

**SYMPTOM**
`max_daily_loss` appears to reset at an unexpected hour, or trades are blocked/not blocked across
midnight.

**WHAT IT MEANS**
`DailyRiskAccount` (`app/engine/state.py:146`) computes the trading-day boundary from
`market_timezone`, which defaults to `Asia/Kolkata` (`app/config/settings.py:126`) — commented
*"trading-day boundary for daily risk"*.

**LIKELY ROOT CAUSES**
1. `MARKET_TIMEZONE` unset, so the default `Asia/Kolkata` is used while you expect UTC or local.
2. Missing timezone database — **Windows ships no IANA database for `zoneinfo`**, which is why
   `requirements.txt` includes `tzdata` under `platform_system == "Windows"`.
3. `rollover()` not called because the engine was not running across the boundary.

**EXACT DIAGNOSTIC COMMANDS**
```bash
python -c "from app.config.settings import Settings as S; print('market_timezone =', S.from_env().market_timezone)"
python -c "from zoneinfo import ZoneInfo; from datetime import datetime; print(datetime.now(ZoneInfo('Asia/Kolkata')))"
sqlite3 "file:trading_bot.db?mode=ro" "SELECT payload FROM engine_state WHERE key LIKE '%daily%' OR key LIKE '%risk%';"
```

**HOW TO INTERPRET OUTPUT**
- If `ZoneInfo('Asia/Kolkata')` raises `ZoneInfoNotFoundError`, the timezone database is missing —
  on Windows that means `tzdata` is not installed.
- `DailyRiskAccount.trading_date()` (line 168) and `rollover()` (line 171) are the functions that
  decide the boundary; `state()` (line 197) shows the current accounting.
- The engine only rolls over when it processes a candle, so a stopped engine will roll over on its
  next start rather than at the clock boundary.

**FIX**
```bash
python -m pip install tzdata        # Windows, or any host without a system zoneinfo database
```
```
MARKET_TIMEZONE=Asia/Kolkata
```

**VERIFICATION**
```bash
python -c "from zoneinfo import ZoneInfo; print(ZoneInfo('Asia/Kolkata'))"
python -m pytest tests/test_v1_engine.py -q
```

**WHAT NOT TO CHANGE**
Do not hard-code a UTC offset instead of a timezone name — India has no DST but other markets do,
and an absolute offset silently breaks across a DST transition. Do not disable `max_daily_loss` to
avoid the confusion; set the timezone correctly. Do not edit `DailyRiskAccount` to use the host's
local time — the trading-day boundary is a market property, not a machine property.

---

## Known limitations of this playbook

- **No entry can diagnose a live Upstox rejection from an observed response.** No Upstox call was
  made while writing this — no credentials, no outbound access. Entries 7–10 and 14–16 describe the
  code's documented contract (`classify_history_probe`, the broker docstring, the stream loop), not
  behaviour witnessed from the provider. Verify against your own responses before acting.
- **There is no entry for live-trading failures.** Arena 3.0.5 has no live order path: `BROKER=LIVE`
  is rejected by `Settings`, `/health` reports `real_order_routing: NOT_IMPLEMENTED`, and
  `app/__main__.py` states *"There is no command that can place a real order."*
- **Windows/PowerShell commands were not executed.** Authored on Linux.
- **No entry covers Qt rendering, layout or GUI threading** — no display server was available, so no
  window was ever created. Entry 19 covers the HTTP transport only.
- **Symptoms are indexed by message or observable behaviour.** If your failure has no message — a
  wrong number, a missing candle, a silently skipped order — start from
  `QUICK_DIAGNOSTIC_FLOWCHART.md` Stage 1 instead, because this playbook keys on identifiable
  symptoms.
- **Root causes are ranked, not exhaustive.** Each entry lists the causes that the source makes
  plausible; an unusual deployment can produce something not listed. When that happens, capture the
  bundle (`SAFE_DIAGNOSTIC_COMMANDS.md` §10) and work from the first error in the log, not the last.
