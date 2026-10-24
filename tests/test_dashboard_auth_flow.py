"""3.0.2 dashboard-authentication-flow regression battery.

Contract under test:
- GET /dashboard (the HTML PAGE) is readable WITHOUT a Bearer header by
  loopback clients (127.0.0.1/::1/localhost — and `testclient`, which is how
  the test suite appears to the ASGI app). When the server is bound to
  loopback, every client is loopback, so a local browser always gets the page.
- EVERY protected endpoint keeps the full bearer gate: /dashboard/data,
  /engine/stop, /engine/resume, /paper/*, /api/v2/* (503 without a configured
  API_TOKEN, 401 on missing/wrong token, constant-time compare).
- The page JS sends `Authorization: Bearer <API_TOKEN>` on protected calls
  (operator-entered token kept in sessionStorage — never rendered into the
  HTML, never placed in URLs, never logged).
"""

from __future__ import annotations

import logging

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.auth import LOCAL_CLIENTS, authenticate, loopback_or_bearer
from app.api.server import create_app
from app.config.settings import Settings
from app.database.database import Database
from app.database.repository import Repository

TOKEN = "dash-flow-token-987"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/dashflow.db")
    monkeypatch.setenv("API_TOKEN", TOKEN)
    monkeypatch.setenv("DATA_SOURCE", "generic")
    settings = Settings.from_env()
    app = create_app(settings)
    database = Database(settings.database_url)
    app.state.repository = Repository(database)
    with TestClient(app) as test_client:
        yield test_client
    database.close()


H = {"Authorization": f"Bearer {TOKEN}"}


# ------------------------------------------- page open locally, APIs gated --

class TestDashboardPageOpenLocally:
    def test_dashboard_loads_without_bearer_header(self, client):
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert "AXIOM" in response.text
        assert "LEGACY PAPER/SANDBOX ONLY" in response.text
        assert "VANTAGE OPERATOR" in response.text
        assert 'href="/terminal"' in response.text

    def test_dashboard_still_loads_with_bearer(self, client):
        assert client.get("/dashboard", headers=H).status_code == 200

    def test_landing_and_health_stay_public(self, client):
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 200


class TestProtectedEndpointsStayAuthenticated:
    def test_dashboard_data_without_token_401(self, client):
        response = client.get("/dashboard/data")
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid or missing bearer token"
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_dashboard_data_with_correct_token_200(self, client):
        response = client.get("/dashboard/data", headers=H)
        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "PAPER" and data["live_trading"] is False

    def test_wrong_token_rejected(self, client):
        response = client.get("/dashboard/data",
                              headers={"Authorization": "Bearer wrong-token"})
        assert response.status_code == 401

    def test_malformed_authorization_rejected(self, client):
        for header in ({"Authorization": TOKEN},
                       {"Authorization": f"bearer {TOKEN}"},
                       {"Authorization": f"Bearer {TOKEN} extra"}):
            assert client.get("/dashboard/data", headers=header).status_code == 401

    def test_engine_stop_without_token_401(self, client):
        assert client.post("/engine/stop").status_code == 401

    def test_engine_stop_wrong_token_401(self, client):
        response = client.post("/engine/stop",
                               headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401

    def test_engine_resume_without_token_401(self, client):
        assert client.post("/engine/resume").status_code == 401

    def test_engine_stop_with_correct_token_succeeds(self, client):
        assert client.post("/engine/stop", headers=H).status_code == 200
        data = client.get("/dashboard/data", headers=H).json()
        assert data["emergency_stop"] is True

    def test_other_protected_routers_unchanged(self, client):
        assert client.get("/api/v2/overview").status_code == 401
        assert client.get("/api/v2/overview", headers=H).status_code == 200
        assert client.get("/paper/status").status_code == 401
        assert client.get("/paper/status", headers=H).status_code == 200


# --------------------------------- frontend sends the bearer on API calls ---

class TestFrontendSendsBearer:
    def test_page_js_uses_bearer_header_for_data_calls(self, client):
        page = client.get("/dashboard").text
        assert "sessionStorage.getItem('arena_token')" in page
        assert "'Authorization':'Bearer ' + token" in page
        assert "fetch('/dashboard/data',{headers:H})" in page

    def test_page_js_sends_bearer_for_engine_stop(self, client):
        page = client.get("/dashboard").text
        assert "fetch('/engine/stop',{method:'POST',headers:H})" in page

    def test_no_query_string_token_in_frontend(self, client):
        page = client.get("/dashboard").text
        # the token never travels in a URL: no ?token=… anywhere in the JS
        assert "token=" not in page.replace("arena_token", "").replace(
            "api_token", "").replace("APITOKEN", "")
        assert "fetch('/dashboard/data?'" not in page

    def test_token_input_not_server_rendered(self, client):
        """The configured API_TOKEN value must never appear in the page."""
        page = client.get("/dashboard").text
        assert TOKEN not in page

    def test_token_not_in_any_served_json(self, client):
        data = client.get("/dashboard/data", headers=H).json()
        assert TOKEN not in repr(data)


# ------------------------------------------- loopback gate behavior ---------

def _fake_app(api_token=TOKEN):
    settings = type("Settings", (), {"api_token": api_token})()
    state = type("State", (), {"settings": settings})()
    return type("App", (), {"state": state})()


class TestLoopbackGate:
    def _request(self, client_host, headers=None):
        scope = {
            "type": "http", "method": "GET", "path": "/dashboard",
            "headers": [(k.lower().encode(), v.encode())
                        for k, v in (headers or {}).items()],
            "client": (client_host, 50000), "server": ("127.0.0.1", 8000),
            "app": _fake_app(),
        }
        from starlette.requests import Request

        return Request(scope)

    def test_loopback_clients_pass_without_bearer(self):
        for host in ("127.0.0.1", "::1", "localhost", "testclient"):
            loopback_or_bearer(self._request(host))  # must not raise

    def test_remote_client_requires_bearer(self):
        with pytest.raises(HTTPException) as excinfo:
            loopback_or_bearer(self._request("203.0.113.9"))
        assert excinfo.value.status_code == 401

    def test_remote_client_with_correct_bearer_passes(self):
        loopback_or_bearer(self._request("203.0.113.9",
                                         {"Authorization": f"Bearer {TOKEN}"}))

    def test_remote_client_with_wrong_bearer_rejected(self):
        with pytest.raises(HTTPException) as excinfo:
            loopback_or_bearer(self._request("203.0.113.9",
                                             {"Authorization": "Bearer bad"}))
        assert excinfo.value.status_code == 401

    def test_missing_client_info_requires_bearer(self):
        scope = {
            "type": "http", "method": "GET", "path": "/dashboard", "headers": [],
            "client": None, "server": ("127.0.0.1", 8000),
            "app": _fake_app(),
        }
        from starlette.requests import Request

        with pytest.raises(HTTPException):
            loopback_or_bearer(Request(scope))


# --------------------------------------------- token never in URLs/logs ----

class TestNoTokenLeakage:
    def test_token_absent_from_logs(self, client, caplog):
        with caplog.at_level(logging.DEBUG):
            client.get("/dashboard")                      # page (no token sent)
            client.get("/dashboard/data")                 # 401 (no token sent)
            client.get("/dashboard/data", headers=H)      # authed
            client.get("/dashboard/data",
                       headers={"Authorization": "Bearer wrong"})  # 401
        assert TOKEN not in caplog.text

    def test_token_absent_from_url_query(self, client):
        # even a deliberate query-string attempt is just ignored data
        response = client.get(f"/dashboard/data?token={TOKEN}")
        assert response.status_code == 401

    def test_authenticate_preserves_503_when_token_unconfigured(self):
        scope = {
            "type": "http", "method": "GET", "path": "/dashboard/data",
            "headers": [], "client": ("testclient", 50000),
            "server": ("127.0.0.1", 8000),
            "app": _fake_app(api_token=""),
        }
        from starlette.requests import Request

        with pytest.raises(HTTPException) as excinfo:
            authenticate(Request(scope))
        assert excinfo.value.status_code == 503

    def test_local_clients_documented(self):
        assert {"127.0.0.1", "::1", "localhost"} <= LOCAL_CLIENTS
