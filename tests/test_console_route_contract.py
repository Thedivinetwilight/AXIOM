"""The operator console may only call routes the current server actually exposes.

The legacy Vantage order surface was removed, but ``terminal.js`` kept calling it
(``/api/trading/status`` first), so unlocking the terminal failed with "Not
Found". The fix routes the console at existing read-only endpoints; these tests
keep it that way:

* no removed route may reappear in the console,
* every path the console calls must resolve to a real OpenAPI route,
* token validation must use an authenticated, read-only route,
* wrong-token / missing-configuration / missing-route must stay distinguishable,
* the token may live in sessionStorage only,
* no live order route may be reachable from the console.

No server request is made here except through FastAPI's in-process TestClient.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
TERMINAL_JS = ROOT / "app" / "static" / "terminal" / "terminal.js"


def _source() -> str:
    return TERMINAL_JS.read_text(encoding="utf-8")


def _routes_map() -> dict[str, str]:
    """The console's single route table."""
    block = re.search(r"const ROUTES = Object\.freeze\(\{(.*?)\n\}\);", _source(), re.S)
    assert block, "ROUTES map is missing from terminal.js"
    pairs = re.findall(r'(\w+):\s*"([^"]+)"', block.group(1))
    assert pairs, "ROUTES map is empty"
    return dict(pairs)


def _console_paths() -> set[str]:
    """Every literal API path the console can call."""
    paths = set(_routes_map().values())
    for literal in re.findall(r'api\(\s*[`"]([^`"]+)[`"]', _source()):
        paths.add(literal)
    return {path.split("?")[0] for path in paths}


def _normalize(js_path: str) -> list[str]:
    """Turn a JS path into segments, treating ``${...}`` as one wildcard segment."""
    return [segment for segment in js_path.strip("/").split("/")]


def _resolves(js_path: str, openapi_paths: set[str]) -> bool:
    wanted = _normalize(js_path)
    for candidate in openapi_paths:
        parts = _normalize(candidate)
        if len(parts) != len(wanted):
            continue
        if all(
            ("${" in want and want.endswith("}")) or want == part or (part.startswith("{") and part.endswith("}"))
            for want, part in zip(wanted, parts)
        ):
            return True
    return False


# --------------------------------------------------------------------------- #
# Removed surface
# --------------------------------------------------------------------------- #
def test_no_removed_trading_route_is_called() -> None:
    source = _source()
    calls = re.findall(r'api\(\s*[`"]([^`"]*)[`"]', source)
    offenders = [path for path in calls if "/api/trading" in path]
    assert not offenders, f"console still calls removed routes: {offenders}"
    # Prose may mention the removal, but no call site may exist.
    assert '/api/trading' not in source


def test_legacy_router_stays_unmounted() -> None:
    paths = set(create_app().openapi()["paths"])
    assert not [path for path in paths if path.startswith("/api/trading")]


# --------------------------------------------------------------------------- #
# Every console path exists
# --------------------------------------------------------------------------- #
def test_every_console_path_resolves_to_a_real_route() -> None:
    openapi_paths = set(create_app().openapi()["paths"])
    unresolved = sorted(path for path in _console_paths() if not _resolves(path, openapi_paths))
    assert not unresolved, "console calls routes the server does not expose: " + ", ".join(unresolved)


def test_route_table_covers_the_state_loaders() -> None:
    routes = _routes_map()
    for key in (
        "authProbe", "paperStatus", "paperAccount", "paperPositions", "paperOrders",
        "paperTrades", "localHaltState", "auditEvents", "performance",
        "credentialStore", "emergencyStop", "emergencyReset", "engineStop", "engineResume",
    ):
        assert key in routes, f"ROUTES.{key} is missing"
    assert routes["authProbe"] == "/paper/status"


# --------------------------------------------------------------------------- #
# Token validation uses an authenticated, read-only route
# --------------------------------------------------------------------------- #
def _app_with_token(token: str, data_dir):
    """Settings is frozen: replace the instance rather than mutating it."""
    settings = replace(Settings.from_env(), api_token=token,
                       database_url=f"sqlite:///{data_dir}/console-contract.db")
    return create_app(settings, data_dir=data_dir)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    # Context manager runs the app lifespan, which wires app.state (simulator, broker).
    with TestClient(_app_with_token("unit-test-token", tmp_path_factory.mktemp("console"))) as live_client:
        yield live_client


def test_auth_probe_is_authenticated_and_read_only(client: TestClient) -> None:
    probe = _routes_map()["authProbe"]
    assert client.get(probe).status_code == 401
    assert client.get(probe, headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get(probe, headers={"Authorization": "Bearer unit-test-token"})
    assert ok.status_code == 200
    spec = client.app.openapi()["paths"][probe]
    assert set(spec) == {"get"}, "token validation must use a read-only (GET) route"


def test_missing_token_configuration_is_503_not_401(tmp_path) -> None:
    with TestClient(_app_with_token("", tmp_path)) as unconfigured:
        response = unconfigured.get("/paper/status")
    assert response.status_code == 503
    assert "API_TOKEN" in response.json()["detail"]


def test_console_distinguishes_wrong_token_missing_config_and_missing_route() -> None:
    source = _source()
    block = re.search(r"function authFailure\(error\) \{(.*?)\n\}", source, re.S)
    assert block, "authFailure() classification is missing"
    body = block.group(1)
    assert "401" in body and "wrong_token" in body
    assert "503" in body and "token_not_configured" in body
    assert "404" in body and "route_missing" in body
    assert "network" in body and "unreachable" in body
    # Messages must be distinct strings, not one generic failure.
    messages = set(re.findall(r'message:\s*[`"]([^`"]{20,})', body))
    assert len(messages) >= 4


# --------------------------------------------------------------------------- #
# Token storage and order-surface safety
# --------------------------------------------------------------------------- #
def test_token_is_kept_in_session_storage_only() -> None:
    source = _source()
    assert "localStorage" not in source
    assert "sessionStorage.setItem" in source
    assert 'storageSet("axiom-api-token", token)' in source


def test_console_never_calls_an_order_route() -> None:
    """No order submission may be reachable from the console's own code.

    Reading the paper order book is fine; submitting or cancelling a *live*
    order, or running a live cycle, is not. The gate controls the live panel
    does use (arm/activate/disarm/kill) are control routes, not order routes.
    """
    source = _source()
    for forbidden in (
        "/api/alpaca-broker/orders",   # live/paper broker order create/modify/cancel
        "/api/markets/live/\u0024{",     # templated live action path: checked below
        "/api/trading",
        "/api/autonomy/sessions",
    ):
        if forbidden.endswith("{"):
            continue
        assert forbidden not in source, f"console must not call {forbidden}"
    # The templated live-control path may only carry non-order actions.
    actions = set(re.findall(r'liveAction\([^,]+,\s*"([a-z-]+)"', source))
    assert actions, "expected the live panel's control actions"
    assert "run-cycle" not in actions, "running a live cycle can place orders"
    assert actions <= {
        "readiness", "arm", "activate", "disarm", "emergency-stop",
        "limits", "mode", "reconcile", "reset-kill-switch",
    }, f"unexpected live actions: {sorted(actions)}"


def test_unavailable_data_is_labelled_not_zeroed() -> None:
    source = _source()
    assert "UNAVAILABLE_DATA" in source
    assert "intents: null" in source
    assert "intents_available: false" in source
    # The unresolved-intent count must not render as a bare number when unknown.
    assert "intentsKnown ? unresolved :" in source


def test_emergency_controls_use_existing_routes() -> None:
    routes = _routes_map()
    assert routes["engineStop"] == "/engine/stop"
    assert routes["engineResume"] == "/engine/resume"
    assert routes["emergencyStop"] == "/api/markets/emergency-stop"
    assert routes["emergencyReset"] == "/api/markets/emergency-stop/reset"
    source = _source()
    assert 'confirm_phrase: "CLEAR EMERGENCY STOP"' in source
