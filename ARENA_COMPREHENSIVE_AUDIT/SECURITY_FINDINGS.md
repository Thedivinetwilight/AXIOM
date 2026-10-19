# Security Findings

**Generated:** 2026-10-19T19:30 UTC  
**Commit:** 4317e3a9f130db3f7fd03db53e00bdf66ac79e9f  
**Version:** 3.2.4  
**Scope:** Source audit + authenticated runtime probes (no external pentest).

## 1. Authentication & Authorization

### PASS: Bearer gate on protected endpoints
- **Finding:** All protected routes (`/dashboard/data`, `/engine/stop`, `/paper/*`, `/api/v2/*`, `/console/*`) require `Authorization: Bearer <API_TOKEN>` with constant-time compare (`hmac.compare_digest`).
- **Evidence:** Manual probes returned 401 on missing/wrong token, 503 when no token configured, 200 with correct token. Test suite `test_dashboard_auth_flow.py` covers 20+ cases.
- **Risk:** Low.

### PASS: Loopback relaxation for dashboard HTML page ONLY
- **Finding:** `loopback_or_bearer` allows `GET /dashboard` HTML without bearer **only** for loopback clients (127.0.0.1, ::1, localhost, testclient). Remote clients still require bearer. Page contains zero secrets — token is entered by operator into sessionStorage and sent as Bearer header.
- **Evidence:** `app/api/auth.py`, `tests/test_dashboard_auth_flow.py::TestLoopbackGate`.
- **Caveat:** If the server is bound to 0.0.0.0, any remote can fetch the (non-secret) HTML shell. This is intentional and documented; no account data leaks.

### PASS: No credential leakage in logs/responses/URLs
- **Finding:** Configured `API_TOKEN` never appears in page HTML, JSON responses, logs, or URLs (`?token=` not used). Frontend JS uses `sessionStorage.getItem('arena_token')` + `fetch('/dashboard/data',{headers:H})`.
- **Evidence:** `test_token_not_in_any_served_json`, `test_token_absent_from_logs`, manual probe asserts `TOKEN not in page`.
- **Residual risk:** Browser sessionStorage is not httpOnly; XSS could exfiltrate. Mitigated by static asset serving (no templating) and CSP should be checked in production hardening.

### PASS: Health endpoint truthfulness
- **Finding:** `GET /health` is public and reports `broker`, `mode`, `live_trading`, `real_order_routing` truthfully from actual Settings (not hardcoded). Cannot falsely indicate readiness.
- **Evidence:** `health.py::_routing_status`, manual probe confirms `LOCAL_PAPER`/`SIMULATED_LOCAL_PAPER` while paper-only.
- **Requirement met:** Wizard readiness state reflects actual authenticated request (preflight + readiness checks) — `GET /api/wizard/preflight` and `GET /api/live/readiness` are authenticated.

## 2. Secret Handling

### PASS: Secrets never in logs
- **Finding:** `api_token`, `market_data_access_token`, live tokens are `field(repr=False)` in Settings, excluded from `__repr__` and logs. `build_info.describe()` never contains secrets. No `print(token)` in routes.
- **Evidence:** Code review of `app/config/settings.py`, `app/api/routes/setup.py`, `app/api/routes/wizard.py`.
- **Note:** `.env` file permissions rely on OS user isolation; not tested here. Recommend `chmod 600`.

### PASS: Token replacement & wizard generation
- **Finding:** `POST /setup/generate-token` generates `secrets.token_urlsafe(32)` (32 bytes entropy), returned once to loopback caller, stored to `.env` atomically. Token replacement overwrites file; old token invalidated after reload.
- **Evidence:** `setup.py::generate_token`, manual probe `POST /setup/generate-token` returned 43-char token.

## 3. Live Trading Boundary (Paper-Only Safety)

### PASS: Fail-closed LIVE gating
- **Finding:** Four independent switches must agree (`MODE=LIVE` + `LIVE_TRADING=true` + `BROKER=UPSTOX_LIVE` + `LIVE_CONFIRM=I-ACCEPT-REAL-MONEY-RISK`) plus non-zero hard risk limits; missing switch raises at startup. `LIVE_TRADING_ENABLEMENT.md` documents this.
- **Evidence:** `settings.py::__post_init__` raises on incomplete LIVE config, `hosts.py` forbids `api-hft.upstox.com` for sandbox, `LiveExecutionGateway` adversarial tests.
- **Verification:** No live orders placed in this audit (all paper). Sandbox broker host is sandboxed; `FutureRealBroker` is non-instantiable marker.

### PASS: Credential-reveal protections
- **Finding:** `POST /api/credentials/reveal` requires `loopback_or_bearer`; reveals only to local caller. No remote exfiltration path.

## 4. Injection & Input Validation

### PASS: Input validation on market data
- **Finding:** `validate()` rejects non-finite, negative, malformed OHLCV, duplicate, unsorted timestamps. `parse_timestamp` enforces timezone-aware ISO8601.
- **Evidence:** Manual probes for negative price, NaN, out-of-order, duplicate all flagged as `report.errors`.

### PASS: SQL injection / path traversal
- **Finding:** SQLite usage is via safe parameterization in `database.py` (no string-interpolated queries observed). Path traversal `GET /static/../../etc/passwd` returns 404 (FastAPI static mount confinement).
- **Evidence:** Manual probe `GET /static/../../etc/passwd` → 404; `POST /paper/start` with `' OR 1=1 --` → 422 (validation rejects non-conforming symbol).

### INFORMATIONAL: Injection surface limited
- No `eval`/`exec`/`subprocess.shell=True` with user input found in critical paths. `subprocess.run(["git", "rev-parse"...])` uses fixed argv, no shell.

## 5. Dependency Vulnerabilities

- `fastapi==0.149.0`, `pydantic==2.14.1`, `uvicorn==0.54.0`, `httpx==0.28.1`, `protobuf==7.36.2`, `certifi==2026.10.18` — no known critical CVEs at audit time (offline check limited; recommend `pip-audit` in CI).
- `pyproject.toml` pins `protobuf>=5.29,<7` (tested 5.29 line) and `starlette>=0.46,<1` (avoids TestClient breakage).
- **Recommendation:** Add `pip-audit` or `dependabot` to CI; currently no automated CVE scanning in workflow.

## 6. Remaining Risks & Blocked Items

| Risk | Severity | Status | Action Needed |
|---|---|---|---|
| No publisher-signature check for Kronos weights; cache SHA-256 is not origin proof | Medium | Documented (AXIOM_CURRENT_AUDIT_REPORT.md) | Require signed artifact manifests before trusting weights |
| SessionStorage token (not httpOnly) reachable via XSS if dashboard ever rendered user-controlled HTML | Low | Mitigated by static assets not templated | Add CSP headers, audit any future user-content rendering |
| `.env` file permissions not enforced in code | Low | Known | Ensure install/docs advise `chmod 600` |
| No rate-limiting on auth endpoints (brute-force token guessing) | Low-Medium | Not implemented | Consider rate-limit / lockout for repeated 401s |
| No CORS allowlist; relies on loopback binding for wizard safety | Low | Documented | If binding to LAN, add explicit CORS config |

## Verdict

**C — SECURITY:** PASS with noted informational items. No unresolved critical security defect; authentication behaves correctly and secrets remain protected under the paper-only deployment tested.
