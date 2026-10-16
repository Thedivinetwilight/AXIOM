# DASHBOARD AUTH FIX REPORT — Arena 3.0.2

> **Historical snapshot only:** this report describes the 3.0.2 dashboard-auth fix, not current HEAD. See [`AXIOM_CURRENT_AUDIT_REPORT.md`](AXIOM_CURRENT_AUDIT_REPORT.md) for current verification.


**Problem:** `GET /dashboard` returned `{"detail":"Invalid or missing bearer token"}`
for a local browser: the HTML page sat behind the same router-level bearer
dependency as every protected API endpoint, so the page could never load long
enough to ask the operator for the token.

**Constraint honored:** protected API endpoints, engine controls, emergency
STOP/RESUME, order-adjacent and settings-changing routes keep the FULL bearer
gate — nothing was weakened. No trading/risk/broker/Upstox code touched.

## Files changed (3 + docs)

| File | Change |
|---|---|
| `app/api/auth.py` (NEW) | One shared implementation: `authenticate()` — the exact previous bearer gate (503 unconfigured / 401 wrong token / constant-time compare / `WWW-Authenticate: Bearer`), moved out of the `create_app` closure so routes can depend on it directly; `loopback_or_bearer()` — used ONLY by the dashboard HTML page: loopback clients (127.0.0.1/::1/localhost, plus `testclient` for the suite) pass without a header, everyone else gets the unchanged bearer check. Missing client info → bearer required (fail closed). |
| `app/api/server.py` | Imports `authenticate` from `app.api.auth` (closure removed; identical semantics via `app.state.settings`). Includes `dashboard.router` without the blanket dependency (dashboard.py applies per-route deps); all other routers keep `dependencies=[Depends(authenticate)]` exactly as before. |
| `app/api/routes/dashboard.py` | `/dashboard/data`, `/engine/stop`, `/engine/resume` → explicit `Depends(authenticate)` (same gate as before). `/dashboard` → `Depends(loopback_or_bearer)`. Page JS: unchanged Bearer mechanism (operator-prompted token in `sessionStorage`, `Authorization: Bearer` header on `/dashboard/data` and `/engine/stop` fetches) + one small addition: clicking the error text re-prompts for a rejected token. |
| `USER_MANUAL.md` | §5 paragraph updated (dashboard page is loopback-readable; JSON/control routes still require the token). |

## Why this is the smallest safe mechanism

- The frontend ALREADY implemented requirements 3–5: it prompts the operator
  for `API_TOKEN`, keeps it in `sessionStorage` (per-tab, dies with the tab),
  and sends `Authorization: Bearer <token>` on every protected fetch. The
  token is never rendered into HTML, never placed in URLs/query strings,
  never logged, and never returned by any endpoint.
- The only broken link was the page itself being token-gated locally. The
  fix exempts ONLY that page, ONLY for loopback clients. When the server is
  bound to 127.0.0.1/localhost every client is loopback → local browsers get
  the page. If the server is ever bound wider, remote clients still need the
  bearer to see the page. The page contains zero secrets, so serving it
  locally leaks nothing.

## Tests added (26 — tests/test_dashboard_auth_flow.py)

- Page open locally without Bearer; still loads with Bearer; `/health` + `/`
  stay public.
- Protected endpoints unchanged: `/dashboard/data` 401 without token / 200
  with correct token / 401 wrong token / 401 malformed Authorization forms;
  `/engine/stop` 401 without & wrong token, 200 with correct token (and the
  STOP flag actually persists); `/engine/resume` 401 without token;
  `/api/v2/overview` + `/paper/status` 401 without / 200 with token.
- Frontend contract asserted on the served page: `sessionStorage` token,
  `'Authorization':'Bearer ' + token`, `fetch('/dashboard/data',{headers:H})`,
  stop-button POST with headers; NO `?token=`-style URLs; configured token
  value absent from the page HTML and from served JSON.
- Loopback gate unit tests: 127.0.0.1/::1/localhost/testclient pass without
  header; remote client 401 without header, 401 wrong header, passes with
  correct header; missing client info → bearer required; 503 preserved when
  no API_TOKEN is configured.
- Leakage: token absent from captured logs across page/401/authed/401-wrong
  requests; deliberate `?token=<token>` query grants nothing (401).

## Executed results

- New battery: **26/26 passed**.
- Full suite: **471 passed, 0 failed, 0 skipped** (445 + 26).
- `paper-test`: `"ok": true`, final equity `99637.58463385397` (unchanged).
- Live loopback boot (`uvicorn app.main:create_app --factory` on
  127.0.0.1, with `API_TOKEN` set):
  `GET /dashboard` no header → **200** (and 0 occurrences of the token in
  the page); `GET /dashboard/data` no header → **401**; with correct Bearer →
  **200**; wrong token → **401**; `POST /engine/stop` no header → **401**;
  `/api/v2/overview` no header → **401**; `?token=<token>` in a URL → **401**.
  Server stopped, scratch `.env` removed.

## Not externally verified

Real-browser JS execution (the fetch/header behavior is verified by asserting
the served page's actual JS contract; no browser automation exists in this
environment). LAN/remote access behavior is covered by the loopback-gate unit
tests, not by a real second host.
