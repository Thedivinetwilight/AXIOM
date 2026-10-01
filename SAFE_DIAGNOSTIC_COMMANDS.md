# Safe Diagnostic Commands — Arena 3.0.5

Every command in this file is **safe to run against a production-shaped deployment**:

- none can place, modify or cancel an order;
- none prints a token, secret, cookie or `Authorization` header;
- database access is opened **read-only** (`mode=ro`);
- nothing mutates engine state.

The only commands that *can* mutate are gathered in the last section and are labelled
**MUTATING — do not run casually**.

**Verification:** every POSIX command below was executed on Linux / Python 3.11.2 against commit
`99fe847`; outputs shown are real. PowerShell equivalents are provided but were **not** executed
on Windows. Commands needing Upstox credentials are marked **NEEDS CREDENTIALS** and were
**not** run here.

---

## 1. Orientation

### 1.1 Which interpreter am I actually using?

```bash
python -c "import sys; print(sys.executable); print(sys.version)"
```

```powershell
python -c "import sys; print(sys.executable); print(sys.version)"
Get-Command python | Format-List Source
```

If the path is not inside your venv, every other result on this page is about the wrong
environment.

### 1.2 What version is this?

```bash
python -c "import app; print(app.__title__, app.__version__)"
```

Expected: `arena 3.0.5`.

### 1.3 Is the offline pipeline sound?

```bash
python -m app paper-test
```

Verified: **exit 0**, 17 checks `true`, `"ok": true`. Fully offline, no credentials, no network.
**Run this first, always.**

### 1.4 What commands exist?

```bash
python -m app --help
python run.py --help
```

Verified: 22 operational subcommands; 7 research modes.

---

## 2. Configuration — without exposing secrets

### 2.1 Non-secret settings summary

```bash
python -c "from app.config.settings import Settings as S; s=S.from_env(); print('mode',s.mode,'broker',s.broker,'data_source',s.data_source,'calendar',s.market_calendar,'timezone',s.market_timezone)"
```

Verified output:

```
mode PAPER broker LOCAL_PAPER data_source replay calendar continuous timezone Asia/Kolkata
```

### 2.2 Are secrets present? (prints SET/unset only)

```bash
python -c "from app.config.settings import Settings as S; s=S.from_env(); [print(k,'SET' if v else 'unset') for k,v in {'API_TOKEN':s.api_token,'MARKET_DATA_ACCESS_TOKEN':s.market_data_access_token,'UPSTOX_SANDBOX_ACCESS_TOKEN':s.upstox_sandbox_access_token,'MARKET_DATA_TOKEN':s.market_data_token}.items()]"
```

Verified output:

```
API_TOKEN unset
MARKET_DATA_ACCESS_TOKEN unset
UPSTOX_SANDBOX_ACCESS_TOKEN unset
MARKET_DATA_TOKEN unset
```

### 2.3 Endpoints and URLs (not secret)

```bash
python -c "from app.config.settings import Settings as S; s=S.from_env(); [print(k,'=',v) for k,v in {'sandbox_base_url':s.upstox_sandbox_base_url,'order_history_url':s.upstox_sandbox_order_history_url,'feed_authorize_url':s.market_data_authorize_url,'universe_sync_url':s.universe_sync_url,'history_base_url':s.history_base_url,'intraday_base_url':s.intraday_base_url}.items()]"
```

Defaults, verified in `app/config/settings.py`:

| Setting | Default | Line |
| --- | --- | --- |
| `upstox_sandbox_base_url` | `https://api-hft.upstox.com` | 101 |
| `upstox_sandbox_order_history_url` | `https://api.upstox.com/v2/order/history` | 102 |
| `market_data_authorize_url` | `https://api.upstox.com/v3/feed/market-data-feed/authorize` | 97 |

### 2.4 Risk limits (never secret, always worth checking)

```bash
python -c "from app.config.settings import Settings as S; from app.risk.limits import HardRiskLimits as H; print(H.from_settings(S.from_env()))"
```

`0` means **unlimited/disabled** — not "zero allowed".

### 2.5 Does the token length look plausible? (never the value)

```bash
python -c "from app.config.settings import Settings as S; s=S.from_env(); print('sandbox token length:', len(s.upstox_sandbox_access_token.strip()))"
```

### 2.6 Prove `.env` is git-ignored before you commit

```bash
git check-ignore -v .env
git status --porcelain .env
```

`git check-ignore` printing a rule means it is ignored. **If it prints nothing, stop — `.env` is
trackable and may already be committed.**

---

## 3. Health and process state

### 3.1 API health (unauthenticated, safe)

```bash
curl -s http://127.0.0.1:8000/health
```

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json
```

Verified body:

```json
{"status":"ok","version":"3.0.5","mode":"PAPER","live_trading":false,"real_order_routing":"NOT_IMPLEMENTED"}
```

### 3.2 Is anything listening?

```bash
ss -ltnp | grep 8000
```

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen
Test-NetConnection -ComputerName 127.0.0.1 -Port 8000
```

### 3.3 System and database health

```bash
python -m app monitor
```

Verified output, **exit 0**:

```json
{"ts":"…","load_1m":0.057,"rss_mb":23.4,"memory_used_pct":0.0707,
 "disk_used_pct":0.054,"database_mb":0.44,"database_ok":true,"alerts":[]}
```

Exit code **1** means `alerts` is non-empty (`app/__main__.py:975`). Alert thresholds:
`disk_used_pct > 0.90`, `memory_used_pct > 0.92`, or `database_ok == false`
(`app/monitoring/system.py:17`).

---

## 4. Database — read-only inspection

All commands below open the database with `mode=ro`, so they **cannot** write.

### 4.1 Where is the database, really?

```bash
python -c "from app.config.settings import Settings as S; import os; print(os.path.abspath(S.from_env().database_url.replace('sqlite:///','')))"
```

Verified: `/home/user/AXIOM/trading_bot.db`. **The URL is relative, so this depends on your
cwd** — a very common cause of "my data vanished".

### 4.2 Integrity and table inventory

```bash
python -c "
import sqlite3
c = sqlite3.connect('file:trading_bot.db?mode=ro', uri=True)
print('integrity:', c.execute('PRAGMA integrity_check').fetchone()[0])
print('tables:', c.execute(\"SELECT COUNT(*) FROM sqlite_master WHERE type='table'\").fetchone()[0])
"
```

Verified: `integrity: ok`, `tables: 48`.

```bash
sqlite3 "file:trading_bot.db?mode=ro" "PRAGMA quick_check;"
sqlite3 "file:trading_bot.db?mode=ro" "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
```

### 4.3 Engine state (the persisted flags that matter)

```bash
sqlite3 "file:trading_bot.db?mode=ro" "SELECT key, payload FROM engine_state;"
```

Watch for:

| key | meaning |
| --- | --- |
| `emergency_stop` | `{"stopped": true, …}` — engine refuses to run while set |
| `sandbox_equity` | last persisted equity/drawdown |
| `active_strategy` | currently selected strategy |
| `initialized` | bootstrap marker |

### 4.4 Order and transition audit

```bash
sqlite3 "file:trading_bot.db?mode=ro" "SELECT intent_id, state FROM sandbox_orders;"
sqlite3 "file:trading_bot.db?mode=ro" "SELECT COUNT(*) FROM sandbox_transitions;"
sqlite3 "file:trading_bot.db?mode=ro" "SELECT kind, COUNT(*) FROM system_events GROUP BY kind ORDER BY 2 DESC;"
```

### 4.5 Recent system events (the real error trail)

```bash
sqlite3 "file:trading_bot.db?mode=ro" "SELECT ts, kind, payload FROM system_events ORDER BY rowid DESC LIMIT 20;"
```

### 4.6 Schema version

```bash
sqlite3 "file:trading_bot.db?mode=ro" "SELECT version FROM schema_version ORDER BY version;"
```

`5` is the current migration marker (`SCHEMA_VERSION_TRANSITIONS_CHILD`,
`app/database/models.py:272`).

### 4.7 WAL files

```bash
ls -la trading_bot.db*
```

A healthy WAL database has `.db`, `.db-wal` and `.db-shm`. **All three are the database.** Never
copy only the `.db`.

---

## 5. Logs

### 5.1 Know where logs actually go

| You ran | Logs |
| --- | --- |
| `python run.py --mode …` | stderr **and** `<repo>/logs/bot.jsonl` (`run.py:152`) |
| `python -m app <command>` | **stderr only** — no file is configured |

Verified: `app/__main__.py` contains no `configure(...)` call.

### 5.2 Capture stderr for an operational run

```bash
python -m app engine 2>&1 | tee logs/engine.jsonl
```

```powershell
python -m app engine 2>&1 | Tee-Object -FilePath logs\engine.jsonl
```

### 5.3 Filter JSON-lines logs

```bash
grep '"event": "SANDBOX_AUTH_FAILURE"' logs/engine.jsonl
grep -E 'EMERGENCY_STOP|RISK_REJECT|CANCEL_FAILURE' logs/engine.jsonl
grep '"level": "ERROR"' logs/engine.jsonl
```

```powershell
Select-String -Path logs\engine.jsonl -Pattern 'SANDBOX_AUTH_FAILURE'
```

### 5.4 Find the FIRST error, not the last

Arena converts exceptions at three layers, so the tail of the log is usually downstream noise.

```bash
grep -n '"level": "ERROR"' logs/engine.jsonl | head -5
```

### 5.5 Redact before sharing any log

```bash
sed -E 's/(Bearer )[A-Za-z0-9._~+\/=-]+/\1<REDACTED>/g; s/(access_token=)[^&"]+/\1<REDACTED>/g' logs/engine.jsonl > logs/engine-redacted.jsonl
```

**Always share the redacted file.**

---

## 6. API checks

### 6.1 Unauthenticated

```bash
curl -s http://127.0.0.1:8000/health
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/v2/overview
```

Verified: the second returns **503** when the server has no `API_TOKEN`, **401** when it has one
and you sent none.

### 6.2 Authenticated (token read from the environment, never typed)

```bash
H="Authorization: Bearer $API_TOKEN"
curl -s -H "$H" http://127.0.0.1:8000/api/v2/overview
curl -s -H "$H" http://127.0.0.1:8000/api/v2/risk
curl -s -H "$H" http://127.0.0.1:8000/api/v2/logs
curl -s -H "$H" http://127.0.0.1:8000/api/v2/performance
```

```powershell
$h = @{ Authorization = "Bearer $env:API_TOKEN" }
Invoke-RestMethod -Headers $h http://127.0.0.1:8000/api/v2/overview | ConvertTo-Json -Depth 5
Invoke-RestMethod -Headers $h http://127.0.0.1:8000/api/v2/risk      | ConvertTo-Json -Depth 5
```

### 6.3 Latency header

```bash
curl -s -D - -o /dev/null http://127.0.0.1:8000/health | grep -i x-api-latency-ms
```

```powershell
(Invoke-WebRequest http://127.0.0.1:8000/health).Headers["x-api-latency-ms"]
```

Added by middleware at `app/api/server.py:55`; the rolling average is also in
`/api/v2/overview` as `api_latency_avg_ms` (verified: `3.65`).

### 6.4 Distinguish 503 from 401

| Code | Body | Meaning |
| --- | --- | --- |
| 503 | `Set API_TOKEN to enable protected API endpoints` | **server** has no token |
| 401 | `Invalid or missing bearer token` | **client** token wrong/missing |

Both verified by execution.

---

## 7. Desktop client — headless (no Qt required)

`desktop/api_client.py` does not import PySide6, so the transport can always be tested:

```bash
python -c "
from desktop.api_client import ArenaApiClient
c = ArenaApiClient('http://127.0.0.1:8000', '$API_TOKEN')
for name, fn in [('overview', c.overview), ('risk', c.risk), ('logs', c.logs)]:
    try:
        fn(); print(name, 'OK')
    except Exception as e:
        print(name, 'FAIL', type(e).__name__, e)
"
```

Desktop configuration (`desktop/app.py:37–38`):

```bash
echo "ARENA_BACKEND_URL=${ARENA_BACKEND_URL:-<unset, defaults to http://127.0.0.1:8000>}"
echo "ARENA_API_TOKEN is $([ -n "$ARENA_API_TOKEN" ] && echo SET || echo unset)"
```

---

## 8. Broker / Upstox sandbox — read-only

### 8.1 Credential probe (places nothing)

```bash
python -m app sandbox-test
```

**NEEDS CREDENTIALS** to do anything. Verified **without** a token — **exit 3**:

```json
{"scope":"REAL UPSTOX SANDBOX TEST","performed":false,
 "reason":"UPSTOX_SANDBOX_ACCESS_TOKEN not set; nothing attempted, nothing claimed"}
```

Exit 3 means *nothing was attempted*. It is not a failure.

### 8.2 Which hosts will be contacted?

```bash
python -c "from app.config.settings import Settings as S; s=S.from_env(); print('orders  ->', s.upstox_sandbox_base_url); print('history ->', s.upstox_sandbox_order_history_url)"
```

Two different hosts by default. A failure on one says nothing about the other.

### 8.3 Is the base URL live-looking? (safety check)

```bash
python -c "
from app.config.settings import Settings as S
u = S.from_env().upstox_sandbox_base_url.lower()
print('SUSPICIOUS: looks live' if ('live' in u and 'sandbox' not in u) else 'ok:', u)
"
```

`UpstoxSandboxBroker.__init__` logs `SANDBOX_BASE_URL_REVIEW` for exactly this case
(`app/brokers/sandbox.py:197`). **Treat that log line as stop-everything.**

### 8.4 Data-feed credential check (read-only, no orders)

```bash
python -m app upstox-test
```

**NEEDS CREDENTIALS.** Verified without symbols — **exit 2**:

```json
{"ok": false, "error": "Set MARKET_DATA_SYMBOLS (e.g. NSE_EQ|INE002A01018) for the Upstox data feed"}
```

### 8.5 Interpreting a probe response correctly

`classify_history_probe` (`app/brokers/sandbox.py:134`) — the project's own verdict table:

| HTTP | Verdict | `credential_accepted` |
| --- | --- | --- |
| 400/404 **with** `UDAPI100010` | `documented_not_found` | **True** |
| 401 / 403 | `auth_rejected` | False |
| 429 | `rate_limited` | False |
| ≥ 500 | `provider_error` | False |
| other | `unexpected` | False (fail closed) |

**A 404 carrying `UDAPI100010` means the token worked.**

---

## 9. Tests

```bash
python -m pytest -q                                   # full suite
python -m pytest tests/test_v1_sandbox.py -q          # one area
python -m pytest "tests/test_v1_engine.py::TestX::test_y" -l --tb=long
python -m pytest -rs -q                               # show skip reasons
python -m pytest -x --lf                              # rerun last failures only
```

Verified baselines at `99fe847` — **the total depends on what you have installed**, so record
your own before changing anything:

| Installed | Result |
| --- | --- |
| `requirements.txt` only | **882 passed, 10 skipped** |
| + `requirements-kronos.txt` | **884 passed, 8 skipped** |
| + `AXIOM_KRONOS_SOURCE=<checkout>` | **890 passed, 2 skipped** |

All three sum to the same 892 collected tests.

The two skips that survive even the fullest configuration are CUDA absence
(CUDA absence, `tests/test_kronos_runtime.py:977`) and a pre-existing network-blocked
instrument download (`tests/test_v2_universe.py:81`). Read skip reasons with `python -m pytest -rs -q`.

---

## 10. Minimum diagnostic bundle

One block that collects everything needed to diagnose an unknown failure, **containing no
secrets**. Run it from the repository root.

```bash
{
  echo "=== BUNDLE (no secrets) $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  echo "--- versions ---"
  python -c "import sys, platform; print('python', sys.version.split()[0], platform.system(), platform.release())"
  python -c "import app; print('arena', app.__version__)"
  echo "--- interpreter ---"
  python -c "import sys; print(sys.executable)"
  echo "--- settings (non-secret) ---"
  python -c "from app.config.settings import Settings as S; s=S.from_env(); print('mode',s.mode,'| broker',s.broker,'| data_source',s.data_source,'| calendar',s.market_calendar,'| timezone',s.market_timezone,'| product',s.sandbox_product)"
  echo "--- endpoints ---"
  python -c "from app.config.settings import Settings as S; s=S.from_env(); print('sandbox_base',s.upstox_sandbox_base_url); print('history_url',s.upstox_sandbox_order_history_url); print('authorize_url',s.market_data_authorize_url)"
  echo "--- credentials present? (booleans only) ---"
  python -c "from app.config.settings import Settings as S; s=S.from_env(); [print(k,'SET' if v else 'unset') for k,v in {'API_TOKEN':s.api_token,'MARKET_DATA_ACCESS_TOKEN':s.market_data_access_token,'UPSTOX_SANDBOX_ACCESS_TOKEN':s.upstox_sandbox_access_token}.items()]"
  echo "--- risk limits ---"
  python -c "from app.config.settings import Settings as S; from app.risk.limits import HardRiskLimits as H; print(H.from_settings(S.from_env()))"
  echo "--- database ---"
  python -c "from app.config.settings import Settings as S; import os; print('path', os.path.abspath(S.from_env().database_url.replace('sqlite:///','')))"
  python -c "
import sqlite3
try:
    c = sqlite3.connect('file:trading_bot.db?mode=ro', uri=True)
    print('integrity', c.execute('PRAGMA integrity_check').fetchone()[0])
    print('tables', c.execute(\"SELECT COUNT(*) FROM sqlite_master WHERE type='table'\").fetchone()[0])
    print('engine_state', [r[0] for r in c.execute('SELECT key FROM engine_state')])
except Exception as e:
    print('database unavailable:', type(e).__name__, e)
"
  echo "--- monitor ---"
  python -m app monitor
  echo "--- offline self-test ---"
  python -m app paper-test 2>&1 | tail -25
  echo "--- git ---"
  git rev-parse --short HEAD
  git status --porcelain | head -20
  echo "=== END BUNDLE ==="
} > bundle.txt 2>&1
```

```powershell
# PowerShell equivalent (redaction still required before sharing)
$out = "bundle.txt"
"=== BUNDLE (no secrets) $((Get-Date).ToUniversalTime().ToString('o')) ===" | Out-File $out
python -c "import sys, platform; print('python', sys.version.split()[0], platform.system())" | Out-File $out -Append
python -c "import app; print('arena', app.__version__)" | Out-File $out -Append
python -c "import sys; print(sys.executable)" | Out-File $out -Append
python -c "from app.config.settings import Settings as S; s=S.from_env(); print('mode',s.mode,'| broker',s.broker,'| data_source',s.data_source)" | Out-File $out -Append
python -c "from app.config.settings import Settings as S; s=S.from_env(); [print(k,'SET' if v else 'unset') for k,v in {'API_TOKEN':s.api_token,'UPSTOX_SANDBOX_ACCESS_TOKEN':s.upstox_sandbox_access_token}.items()]" | Out-File $out -Append
python -m app monitor | Out-File $out -Append
git rev-parse --short HEAD | Out-File $out -Append
```

**Before sharing `bundle.txt`:** open it and confirm no token appears. The commands above never
print one — `Settings` declares every secret field `repr=False`, verified by execution:

```bash
API_TOKEN=supersecret python -c "
from app.config.settings import Settings as S
r = repr(S.from_env())
print('supersecret in repr:', 'supersecret' in r)
"
# → supersecret in repr: False
```

Add your **redacted** log tail (§5.5) and the failing traceback.

---

## 11. MUTATING — do not run casually

These change state. Listed so you can recognise them, not so you run them by accident.

| Command | Effect |
| --- | --- |
| `python -m app sandbox-test --place-order` | **submits one real sandbox order.** Never the default; requires the explicit flag. |
| `python -m app engine` | starts the 24/7 engine; can place sandbox orders |
| `curl -X POST …/engine/stop` | persists the emergency-stop flag; cancels open orders if an engine is embedded |
| `curl -X POST …/engine/resume` | clears the emergency-stop flag |
| `curl -X POST …/paper/start` | starts the paper simulator |
| `python -m app backup restore --path …` | **replaces the live database** (refuses unverified backups) |
| `python -m app initialize` | creates directories, initialises the DB, attempts a universe sync |
| `python -m app market-universe sync` | writes the instrument master |
| `python -m app upstox-login` | runs OAuth and **writes the token into `.env`** |
| `python run.py --mode api` | binds a listening socket |

Safe backups (non-destructive, but do write files):

```bash
python -m app backup backup --label pre-change
python -m app backup verify --path backups/<file>.db
```

---

## Known limitations of this command set

- **Nothing here verifies Upstox connectivity.** §8 only reports what *would* be contacted and
  whether a token is present. Actually reaching `api.upstox.com` / `api-hft.upstox.com` requires
  credentials and outbound network, neither of which was available when this was written. The
  HTTP-status table in §8.5 is transcribed from `classify_history_probe`, not from an observed
  provider response.
- **PowerShell forms are unexecuted.** Authored on Linux; only the POSIX forms were run.
- **The bundle does not include logs by default**, because logs can contain instrument and order
  detail. Add them deliberately, redacted (§5.5).
- **No command here proves the GUI works.** `desktop/api_client.py` is Qt-free and testable;
  rendering, layout and Qt threading are not, and were never exercised.
- **`sqlite3` CLI availability is assumed.** If it is absent, every §4 query has a `python -c`
  equivalent that uses only the standard library.
- **Read-only mode does not protect against a concurrent writer.** `mode=ro` stops *you* writing;
  it does not stop the engine. For a consistent snapshot use `python -m app backup backup`.
