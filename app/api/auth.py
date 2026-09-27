"""API authentication dependencies (3.0.2 dashboard-auth fix).

One implementation, shared by every protected route:

- `authenticate` — the existing bearer-token gate (unchanged semantics):
  503 when no API_TOKEN is configured, 401 on a wrong/missing token, constant
  time comparison. Used by ALL protected API/engine/order routes.
- `loopback_or_bearer` — for the dashboard HTML PAGE only: the page is
  readable without a Bearer header when the CLIENT is on the loopback
  interface. When the server is bound to 127.0.0.1/localhost every client is
  loopback, so a local browser always gets the page. Remote clients (server
  bound wider) still need the bearer. The page contains NO secrets — the
  operator's API_TOKEN is never rendered into it (the page JS asks the
  operator for the token and sends it as an Authorization header on protected
  calls), so serving the page locally leaks nothing.

`testclient` is included in the local set so the test suite exercises the
same path a loopback browser takes.
"""

import hmac

from fastapi import HTTPException, Request

LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _api_token(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None)
    return (getattr(settings, "api_token", "") or "")


def authenticate(request: Request) -> None:
    """Bearer-token gate for protected API endpoints (semantics unchanged)."""
    token = _api_token(request)
    if not token:
        raise HTTPException(503, "Set API_TOKEN to enable protected API endpoints")
    authorization = request.headers.get("Authorization", "")
    if not hmac.compare_digest(
        authorization.encode(), ("Bearer " + token).encode()
    ):
        raise HTTPException(
            401, "Invalid or missing bearer token", headers={"WWW-Authenticate": "Bearer"}
        )


def loopback_or_bearer(request: Request) -> None:
    """Dashboard PAGE gate: local clients read the UI freely; everyone else
    must present the bearer exactly like any protected endpoint."""
    client = request.client.host if request.client is not None else ""
    if client in LOCAL_CLIENTS:
        return
    authenticate(request)
