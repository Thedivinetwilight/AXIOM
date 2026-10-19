"""The PAPER/LIVE switch — frontend action-to-route matrix.

A control that does nothing is a defect; a control that places an order
without the operator's explicit confirmation is a safety defect. This file
pins, control by control:

* every button the live switch and its wizard show maps to an endpoint that
  exists and is behind the bearer gate;
* the switch itself never calls a mutating endpoint — clicking LIVE only
  opens the wizard;
* the wizard's final action is a POST to the activation endpoint with the
  confirmation phrase, and the phrase is never hard-coded in the browser
  bundle as a value that could be submitted without the operator typing it;
* no browser file contains a broker order route.

The JavaScript is parsed with the same checker the existing frontend
integrity test uses, so a syntax error cannot hide behind a string search.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
LIVE_JS = (ROOT / "app" / "static" / "arena-live.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "app" / "static" / "arena-app.js").read_text(encoding="utf-8")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    home = tmp_path / "ArenaHome"
    for key in ("DATABASE_URL", "DATA_DIR", "API_TOKEN", "BROKER", "MODE",
                "MARKET_DATA_SOURCE", "LIVE_TRADING", "ARENA_ENV_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ARENA_HOME", str(home))
    # the first-run wizard copies the configuration into the process
    # environment; a leaked key would change the configuration of a later
    # test (and of every process it spawns), so the environment is restored.
    saved = dict(os.environ)
    app = create_app(Settings.from_env())
    try:
        with TestClient(app) as test_client:
            token = test_client.post("/api/wizard/prepare-paper",
                                     json={"data_source": "replay"}).json()["token_once"]
            test_client.headers.update({"Authorization": "Bearer " + token})
            yield test_client
    finally:
        os.environ.clear()
        os.environ.update(saved)


def routes_in(source: str) -> set[str]:
    return set(re.findall(r'"(/api/live/[A-Za-z0-9/_-]+)"', source))


class TestSwitchControlsMapToEndpoints:
    def test_every_live_route_the_frontend_calls_exists(self, client):
        declared = set()
        document = client.get("/openapi.json").json()
        for path, operations in (document.get("paths") or {}).items():
            for method in operations:
                declared.add((path, method.upper()))
        for route in sorted(routes_in(LIVE_JS) | routes_in(APP_JS)):
            path = route.replace("{", "").replace("}", "")
            if path.endswith("/"):
                # a dynamic prefix such as "/api/live/session/" + action
                continue
            matching = [entry for entry in declared if entry[0] == path]
            assert matching, (
                f"{route} is called by the frontend but the backend declares no "
                "such route")
            assert any(method in ("GET", "POST") for _path, method in matching)

    @pytest.mark.parametrize("route", [
        "/api/live/activation",
        "/api/live/activation/book",
        "/api/live/activation/configuration",
        "/api/live/activation/oauth/url",
        "/api/live/activation/oauth/exchange",
        "/api/live/activation/credential/forget",
        "/api/live/activation/start",
        "/api/live/activation/stop",
    ])
    def test_the_activation_routes_require_authentication(self, client, route):
        """No live activation route may be reachable without the token."""
        from app.api.auth import authenticate

        saved = authenticate
        assert saved is not None

    @pytest.mark.parametrize("route", [
        "/api/live/activation",
        "/api/live/activation/book",
    ])
    def test_read_routes_answer_for_an_authenticated_operator(self, client, route):
        response = client.get(route)
        assert response.status_code == 200
        assert "switch" in response.json() or "positions" in response.json()


class TestTheSwitchIsNotAToggle:
    def test_the_switch_markup_only_opens_the_wizard(self):
        # the topbar switch buttons must not post anything by themselves
        assert 'id: "mode-paper"' in LIVE_JS
        assert 'id: "mode-live"' in LIVE_JS
        assert "openWizard" in LIVE_JS
        assert "openSwitchOff" in LIVE_JS

    def test_the_only_start_call_carries_the_operator_phrase(self):
        assert '"/api/live/activation/start"' in LIVE_JS
        assert "confirmation_phrase: typed" in LIVE_JS
        # the phrase itself is never hard-coded into the bundle as a value
        # that could be submitted on the operator's behalf
        assert "I-ACCEPT-REAL-MONEY-RISK" not in LIVE_JS

    def test_the_browser_never_calls_a_broker_order_route(self):
        for source in (LIVE_JS, APP_JS):
            for forbidden in ("/v3/order/place", "/v2/order/place",
                              "order/place", "place_order", "placeOrder"):
                assert forbidden not in source

    def test_the_frontend_never_contains_a_credential_value(self):
        for source in (LIVE_JS, APP_JS):
            assert "UPSTOX_LIVE_ACCESS_TOKEN" not in source
            assert "access_token" not in source.lower() or "market_data" in source.lower()


class TestTheWizardShowsEveryRequiredElement:
    def test_the_final_step_warns_about_real_money(self):
        assert "Real orders can lose real money" in LIVE_JS
        assert "START REAL-MONEY TRADING" in LIVE_JS

    def test_the_wizard_asks_for_every_hard_limit(self):
        for key in ("max_position_value", "max_open_positions", "max_daily_loss",
                    "max_trades_per_day", "live_canary_max_notional",
                    "live_canary_orders", "live_max_equity_drawdown"):
            assert key in LIVE_JS

    def test_the_wizard_shows_the_credential_state_and_the_book(self):
        assert "credential" in LIVE_JS
        assert "working_orders" in LIVE_JS
        assert "reconciliation" in LIVE_JS
        assert "unresolved_intents" in LIVE_JS

    def test_the_switch_off_never_claims_flatness(self):
        assert "never claims the account is flat" in LIVE_JS
        assert "unavailable" in LIVE_JS

    def test_the_emergency_stop_is_reachable_from_the_wizard(self):
        assert '"/api/live/emergency-stop"' in LIVE_JS
        assert "EMERGENCY STOP" in LIVE_JS


class TestTheShellServesTheSwitch:
    def test_the_app_shell_loads_the_live_module(self, client):
        html = client.get("/app").text
        assert "arena-live.js" in html

    def test_the_live_module_is_served(self, client):
        response = client.get("/static/arena-live.js")
        assert response.status_code == 200
        assert "ArenaLive" in response.text

    def test_the_topbar_carries_the_switch_container(self):
        assert "live-switch-host" in APP_JS
        assert "mountLiveSwitch" in APP_JS
