"""Start & Monitor: the single-page launchpad projection is honest and read-only.

Two layers are covered:

* the pure projection (:mod:`app.markets.start_monitor`) against a deliberately
  hostile set of inputs — missing engine, kill switch engaged, unverified plan,
  a market routed LIVE, a paper market with a stale gate state;
* the authenticated route on a real app, asserting the payload carries no
  secret, cannot place anything, and refuses unauthenticated callers.

No provider is contacted here. The route only reads state the server already
holds, so a request count of zero provider calls is the expected outcome.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings
from app.markets.start_monitor import (
    AUTOPILOT_MAY,
    BOUNDED_CAP_FIELDS,
    CREDENTIAL_ROLES,
    INTEGER_CAP_FIELDS,
    _cadence_view,
    _credential_matrix,
    _credential_roles,
    _mode_credential_requirements,
    _validate_risk_limits,
    AUTOPILOT_MAY_NOT,
    BADGE_ERROR,
    BADGE_LIVE_ACTIVE,
    BADGE_LIVE_ARMED,
    BADGE_LIVE_DISABLED,
    BADGE_PAPER,
    build_launchpad,
)

NSE = "NSE"
NASDAQ = "NASDAQ"


# --------------------------------------------------------------- test doubles
class _FakePlan:
    def __init__(self, market: str, symbols: int, cadence: float) -> None:
        self.market = market
        self.symbols = symbols
        self.cadence = cadence

    def to_dict(self) -> dict:
        minimum = 90.0 if self.market == NSE else 60.0
        return {
            "market": self.market,
            "symbols": self.symbols,
            "cadence_requested_seconds": self.cadence,
            "cadence_sustainable_seconds": minimum,
            "max_symbols_at_requested_cadence": 16 if self.market == NSE else 25,
            "requests_per_cycle": (
                {"upstox.market_quote": self.symbols, "upstox.historical_candle": self.symbols}
                if self.market == NSE else {"alpaca.market_data": self.symbols * 2}
            ),
            "total_requests_per_cycle": self.symbols * 2,
            "expected_dispatch_seconds": 2.0,
            "status": "READY",
            "can_start": True,
            "reasons": [],
            "window_usage": {},
        }


class _FakeScan:
    def plan(self, market: str, *, symbols: int, cadence_s: float) -> _FakePlan:
        return _FakePlan(market, symbols, cadence_s)

    def quota_profile_payload(self, market: str) -> dict:
        return {
            "market": market,
            "profile": "upstox_standard" if market == NSE else "alpaca_basic",
            "verification": "DOCUMENTED" if market == NSE else "UNVERIFIED",
            "credential_present": True,
            "policies": [],
        }


LIMITS = {
    "market": "NSE",
    "max_order_value": 250_000.0,
    "max_symbol_exposure": 400_000.0,
    "max_total_exposure": 900_000.0,
    "max_open_positions": 10,
    "max_daily_loss": 25_000.0,
    "max_daily_orders": 40,
    "max_quote_age_seconds": 90.0,
    # A zero cost rate must NOT be read as an unset cap.
    "brokerage_bps": 0.0,
}

EXIT_NSE = {
    "routed": False,
    "summary": "No NSE exit is routed.",
    "closing_paths": ["protected GTT legs"],
    "operator_fallback": "close manually in the Upstox app, then reconcile",
}
EXIT_US = {"routed": True, "summary": "close-only exits documented", "closing_paths": [], "operator_fallback": ""}


class _FakeEngine:
    def __init__(self, *, states: dict | None = None, blocked_live: bool = False) -> None:
        self._states = states or {NSE: "STOPPED", NASDAQ: "STOPPED"}
        self._blocked_live = blocked_live

    def market_snapshot(self, market: str) -> dict:
        return {
            "market": market,
            "session_state": self._states.get(market, "STOPPED"),
            "state_reason": None,
            "strategy": "manual_only",
            "timeframe": "5m",
            "automation_state": "MANUAL_ONLY_NO_AUTO_ORDERS",
            "fill_type": "SIMULATED",
            "execution_destination": f"LOCAL_SQLITE_SIMULATOR ({market})",
            "positions": [],
            "orders": [],
            "fills": [],
            "watched_symbols": ["RELIANCE"] if market == NSE else ["AAPL"],
            "risk_limits": dict(LIMITS),
            "account": {"currency": "INR" if market == NSE else "USD"},
        }

    def list_watchlists(self) -> dict:
        return {
            NSE: [{"symbol": "RELIANCE", "instrument_key": "NSE_EQ|INE002A01018", "name": "Reliance"}],
            NASDAQ: [{"symbol": "AAPL", "instrument_key": "NASDAQ|AAPL", "name": "Apple"}],
        }


class _FakeCoordinator:
    def __init__(
        self,
        *,
        modes: dict | None = None,
        gates: dict | None = None,
        ready: bool = False,
        missing: list | None = None,
        blocked: str | None = None,
        unresolved: list | None = None,
        readiness_override: dict | None = None,
        risk_limits: dict | None = None,
    ) -> None:
        self._modes = modes or {NSE: "PAPER", NASDAQ: "PAPER"}
        self._gates = gates or {
            NSE: {"state": "DISABLED", "live_available": False},
            NASDAQ: {"state": "DISABLED", "live_available": False},
        }
        self._ready = ready
        self._missing = missing or []
        self._blocked = blocked
        self._unresolved = unresolved or []
        self._readiness_override = readiness_override
        self._risk_limits = risk_limits

    def status(self, market: str) -> dict:
        gate = {
            **self._gates[market],
            "kill_engaged": False,
            "kill_reason": None,
            "live_confirmation_phrase": "ACTIVATE LIVE REAL-MONEY TRADING",
        }
        return {
            "market": market,
            "mode": self._modes[market],
            "gate": gate,
            "blocked": self._blocked if market == NSE else None,
            "blocked_detail": "runtime fault" if (self._blocked and market == NSE) else None,
            "unresolved_intents": self._unresolved if market == NSE else [],
            "submitted_total": 0,
            "rejected_total": 0,
            "last_cycle_status": None,
            "risk_limits": (self._risk_limits if self._risk_limits is not None
                            else {"max_order_value": 1.0}),
        }

    def readiness(self, market: str) -> dict:
        if self._readiness_override is not None:
            return {"market": market, **self._readiness_override}
        return {
            "market": market,
            "ok": self._ready,
            "checks": {"configuration_complete": self._ready},
            "failed": [] if self._ready else ["configuration_complete"],
            "missing_dependencies": list(self._missing),
            "documented_capabilities": {"exit_route": EXIT_NSE if market == NSE else EXIT_US},
        }


def _launchpad(**overrides) -> dict:
    kwargs = {
        "engine": _FakeEngine(),
        "coordinator": _FakeCoordinator(),
        "scan_service": _FakeScan(),
        "watchlists": _FakeEngine().list_watchlists(),
        "data_credentials": {
            NSE: {"present": True, "source": "protected-store"},
            NASDAQ: {"present": False, "source": "none", "feed": "iex"},
        },
        "broker_credentials": {"paper": {"present": False}, "live": {"present": False}},
        "upstox_live_token_present": False,
        "readiness_matrix": {"modes": {}},
        "strategies": [{"name": "manual_only"}, {"name": "parallel_ensemble"}],
    }
    kwargs.update(overrides)
    return build_launchpad(**kwargs)


# ------------------------------------------------------------------- payload
def test_both_markets_present_with_server_derived_badges():
    payload = _launchpad()
    assert set(payload["markets"]) == {NSE, NASDAQ}
    assert payload["markets"][NSE]["badge"] == BADGE_PAPER
    assert payload["markets"][NASDAQ]["badge"] == BADGE_PAPER
    assert payload["markets"][NSE]["venue_label"].startswith("NSE · Upstox")


def test_live_gate_states_map_to_the_four_non_error_badges():
    armed = _launchpad(coordinator=_FakeCoordinator(
        modes={NSE: "LIVE", NASDAQ: "LIVE"},
        gates={NSE: {"state": "ARMED", "live_available": False},
               NASDAQ: {"state": "ACTIVE", "live_available": True}},
    ))
    assert armed["markets"][NSE]["badge"] == BADGE_LIVE_ARMED
    assert armed["markets"][NASDAQ]["badge"] == BADGE_LIVE_ACTIVE
    assert armed["evidence"]["real_money_enabled"] is True

    disabled = _launchpad(coordinator=_FakeCoordinator(modes={NSE: "LIVE", NASDAQ: "PAPER"}))
    assert disabled["markets"][NSE]["badge"] == BADGE_LIVE_DISABLED


def test_an_active_gate_on_a_paper_market_is_reported_as_error_not_success():
    # A gate can only be ACTIVE while routed LIVE; a paper-routed market claiming
    # ACTIVE is a contradiction and must never render as a reassuring badge.
    payload = _launchpad(coordinator=_FakeCoordinator(
        modes={NSE: "PAPER", NASDAQ: "PAPER"},
        gates={NSE: {"state": "ACTIVE", "live_available": True},
               NASDAQ: {"state": "DISABLED", "live_available": False}},
    ))
    assert payload["markets"][NSE]["badge"] == BADGE_ERROR


def test_blocked_runtime_reports_error_and_blocks_every_mode():
    payload = _launchpad(coordinator=_FakeCoordinator(blocked="UNRESOLVED_INTENT"))
    nse = payload["markets"][NSE]
    assert nse["badge"] == BADGE_ERROR
    codes = {gap["code"] for gap in nse["autopilot"]["blockers"]}
    assert "UNRESOLVED_INTENT" in codes
    assert nse["autopilot"]["paper"]["available"] is False
    assert nse["autopilot"]["live"]["available"] is False


def test_zero_cost_rate_is_not_mistaken_for_an_unset_cap():
    # brokerage_bps = 0.0 is a legitimate rate, not a missing limit.
    nse = _launchpad()["markets"][NSE]
    codes = {gap["code"] for gap in nse["autopilot"]["paper"]["gaps"]}
    assert "RISK_LIMITS_INCOMPLETE" not in codes


def test_missing_market_data_credential_blocks_paper_autopilot():
    nasdaq = _launchpad()["markets"][NASDAQ]
    codes = {gap["code"] for gap in nasdaq["autopilot"]["paper"]["gaps"]}
    assert "NO_MARKET_DATA_CREDENTIAL" in codes
    assert nasdaq["autopilot"]["paper"]["available"] is False
    # The caution is separate from the blocker: an unverified plan is not fatal.
    assert {c["code"] for c in nasdaq["autopilot"]["cautions"]} == {"PLAN_UNVERIFIED"}


def test_no_routed_exit_blocks_live_autopilot_only():
    nse = _launchpad()["markets"][NSE]
    live_codes = {gap["code"] for gap in nse["autopilot"]["live"]["gaps"]}
    paper_codes = {gap["code"] for gap in nse["autopilot"]["paper"]["gaps"]}
    assert "NO_ROUTED_EXIT" in live_codes
    assert "NO_ROUTED_EXIT" not in paper_codes
    assert nse["live"]["exit_route"]["routed"] is False
    assert "close manually" in nse["live"]["exit_route"]["operator_fallback"]


def test_autopilot_live_requires_an_active_gate_even_when_ready():
    ready_but_disarmed = _launchpad(coordinator=_FakeCoordinator(ready=True))
    assert ready_but_disarmed["markets"][NSE]["autopilot"]["live"]["available"] is False

    # NASDAQ documents a close-only exit route, so it is the market where a
    # ready + ACTIVE gate can actually enable LIVE automation. Give it a
    # market-data credential too: without one, no quote can be validated.
    ready_and_active = _launchpad(coordinator=_FakeCoordinator(
        modes={NSE: "PAPER", NASDAQ: "LIVE"}, ready=True,
        gates={NSE: {"state": "DISABLED", "live_available": False},
               NASDAQ: {"state": "ACTIVE", "live_available": True}},
    ), data_credentials={
        NSE: {"present": True, "source": "protected-store"},
        NASDAQ: {"present": True, "source": "protected-store", "feed": "iex"},
    })
    assert ready_and_active["markets"][NASDAQ]["autopilot"]["live"]["available"] is True
    # NSE stays blocked even in that state: no exit is routed.
    assert ready_and_active["markets"][NSE]["autopilot"]["live"]["available"] is False


def test_unresolved_intent_blocks_and_names_reconciliation():
    payload = _launchpad(coordinator=_FakeCoordinator(unresolved=["cid-1"]))
    gaps = payload["markets"][NSE]["autopilot"]["blockers"]
    text = " ".join(gap["text"] for gap in gaps)
    assert "reconcile" in text.lower() or "Reconcile" in text
    assert any(gap["code"] == "UNRESOLVED_ORDER_OUTCOME" for gap in gaps)


def test_cadence_and_quota_come_from_the_scan_service_not_the_ui():
    payload = _launchpad()
    nse = payload["markets"][NSE]
    assert nse["cadence"]["sustainable_seconds"] == 90.0
    assert nse["cadence"]["source"] == "server"
    assert payload["markets"][NASDAQ]["cadence"]["sustainable_seconds"] == 60.0
    assert nse["quota"]["profile"] == "upstox_standard"
    assert payload["markets"][NASDAQ]["quota"]["verification"] == "UNVERIFIED"


def test_limits_and_caps_are_copied_unchanged_from_the_server():
    nse = _launchpad()["markets"][NSE]
    assert nse["position_cap"] == LIMITS["max_open_positions"]
    assert nse["daily_order_cap"] == LIMITS["max_daily_orders"]
    assert nse["symbol_cap"] == 100
    # Selecting 100 symbols cannot turn into more positions:
    assert nse["position_cap"] < nse["symbol_cap"]


def test_autopilot_promise_is_bounded_and_never_claims_unrestricted():
    payload = _launchpad()
    autopilot = payload["markets"][NSE]["autopilot"]
    assert autopilot["may"] == list(AUTOPILOT_MAY)
    assert autopilot["may_not"] == list(AUTOPILOT_MAY_NOT)
    joined = " ".join(autopilot["may_not"]).lower()
    assert "raise any risk limit" in joined
    assert "100 orders" in joined
    assert payload["max_symbols_per_scan"] == 100
    assert payload["broker_order_api_calls"] == 0


def test_credential_matrix_keeps_every_slot_distinct():
    payload = _launchpad()
    slots = {cred["slot"] for cred in payload["markets"][NASDAQ]["credentials"]["rows"]}
    # The loopback API token is *not* a provider credential and must not appear
    # beside the broker slots, where it could be read as one.
    assert "AXIOM API TOKEN" not in slots
    assert "ALPACA MARKET DATA" in slots
    assert "ALPACA BROKER PAPER" in slots
    assert "ALPACA BROKER LIVE" in slots
    # Only the real-money slots are flagged as execution credentials.
    for cred in payload["markets"][NASDAQ]["credentials"]["rows"]:
        if cred["slot"] == "ALPACA BROKER LIVE":
            assert cred["affects_execution"] is True
        elif cred["slot"] == "ALPACA MARKET DATA":
            assert cred["affects_execution"] is False


def test_missing_engine_and_coordinator_degrade_instead_of_fabricating():
    payload = build_launchpad(
        engine=None,
        coordinator=None,
        scan_service=None,
        watchlists={},
        data_credentials={},
        broker_credentials=None,
        upstox_live_token_present=False,
        readiness_matrix=None,
        strategies=[],
    )
    for market in (NSE, NASDAQ):
        row = payload["markets"][market]
        assert row["paper"]["session_state"] == "UNAVAILABLE"
        assert row["live"]["available"] is False
        assert row["cadence"]["source"] == "unavailable"
        assert row["autopilot"]["paper"]["available"] is False


def test_payload_never_contains_a_secret_field():
    blob = json.dumps(_launchpad())
    for forbidden in ("api_secret", "access_token", "Authorization", "Bearer", "password"):
        assert forbidden not in blob


# ---------------------------------------------------------------- HTTP route
@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        api_token="start-secret",
        database_url=f"sqlite:///{tmp_path}/start.db",
        data_source="upstox",
    )
    app = create_app(settings, data_dir=tmp_path)
    with TestClient(app) as test_client:
        yield test_client


def test_route_requires_the_local_bearer_token(client: TestClient):
    assert client.get("/api/markets/start-monitor").status_code == 401


def test_route_returns_a_complete_read_only_payload(client: TestClient):
    response = client.get("/api/markets/start-monitor", headers={"Authorization": "Bearer start-secret"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "axiom.start_monitor.v1"
    assert set(payload["markets"]) == {NSE, NASDAQ}
    assert payload["broker_order_api_calls"] == 0
    assert payload["evidence"]["provider_network_calls"] == 0
    assert payload["execution_boundary"] == "app.markets.live_execution"
    for market, row in payload["markets"].items():
        assert row["badge"] in {
            BADGE_PAPER, BADGE_LIVE_DISABLED, BADGE_LIVE_ARMED, BADGE_LIVE_ACTIVE, BADGE_ERROR,
        }
        assert isinstance(row["credentials"]["rows"], list)
        assert row["symbol_cap"] == 100


def test_route_payload_carries_no_secret_or_token_value(client: TestClient):
    text = client.get(
        "/api/markets/start-monitor", headers={"Authorization": "Bearer start-secret"}
    ).text
    assert "start-secret" not in text
    for forbidden in ("api_secret", "access_token", "Authorization", "password"):
        assert forbidden not in text


def test_route_without_credentials_reports_not_configured_not_connected(client: TestClient):
    payload = client.get(
        "/api/markets/start-monitor", headers={"Authorization": "Bearer start-secret"}
    ).json()
    for market, row in payload["markets"].items():
        data_slot = next(c for c in row["credentials"]["rows"] if "MARKET DATA" in c["slot"])
        assert data_slot["present"] is False, f"{market} must not claim a credential it does not have"
        assert any(gap["code"] == "NO_MARKET_DATA_CREDENTIAL"
                   for gap in row["autopilot"]["paper"]["gaps"])


def test_readiness_and_limits_come_from_the_server(client: TestClient):
    payload = client.get(
        "/api/markets/start-monitor", headers={"Authorization": "Bearer start-secret"}
    ).json()
    nse = payload["markets"][NSE]
    assert nse["limits"]["max_open_positions"] > 0
    assert nse["limits"]["max_daily_orders"] > 0
    assert nse["readiness"]["overall_status"] in {"BLOCKED", "NOT_VERIFIED", "READY", "MARKET_CLOSED_READY"}


def test_route_is_registered_only_through_the_existing_markets_router(client: TestClient):
    paths = set(client.app.openapi()["paths"])
    assert "/api/markets/start-monitor" in paths
    assert list(paths & {"/api/trading/status", "/api/trading/orders/submit"}) == []
    methods = set(client.app.openapi()["paths"]["/api/markets/start-monitor"])
    assert methods == {"get"}, "the launchpad view must stay read-only"


def test_cadence_verdict_refuses_to_endorse_an_unsupported_cadence():
    unsupported = _cadence_view(
        {
            "cadence_requested_seconds": 15.0,
            "cadence_sustainable_seconds": 90.0,
            "max_symbols_at_requested_cadence": 16,
            "requests_per_cycle": {"upstox.market_quote": 200},
            "expected_dispatch_seconds": 2.0,
            "status": "READY",
        },
        requested=15.0,
        selected=100,
        cap=100,
    )
    assert unsupported["supportable"] is False
    assert "NOT supported" in unsupported["label"]
    assert "90" in unsupported["label"]
    assert unsupported["requests_per_cycle"] == {"upstox.market_quote": 200}


def test_cadence_verdict_states_the_symbol_ceiling_at_the_fast_cadence():
    # A one-symbol selection really can be scanned every 15 s: the quota floor is
    # a fraction of a second. The label must still say what 15 s can carry.
    supported = _cadence_view(
        {
            "cadence_requested_seconds": 15.0,
            "cadence_sustainable_seconds": 0.9,
            "max_symbols_at_requested_cadence": 16,
            "requests_per_cycle": {"upstox.market_quote": 2},
            "expected_dispatch_seconds": 0.02,
            "status": "NO CREDENTIAL",
        },
        requested=15.0,
        selected=1,
        cap=100,
    )
    assert supported["supportable"] is True
    assert "at most 16 of 100 symbols" in supported["label"]
    assert "0.9" in supported["label"]


def test_cadence_verdict_admits_when_no_plan_was_computed():
    missing = _cadence_view(None, requested=15.0, selected=3, cap=100)
    assert missing["supportable"] is False
    assert missing["source"] == "unavailable"
    assert "unavailable" in missing["label"].lower()


def test_every_market_exposes_the_same_cadence_contract(client: TestClient):
    payload = client.get(
        "/api/markets/start-monitor", headers={"Authorization": "Bearer start-secret"}
    ).json()
    for market in ("NSE", "NASDAQ"):
        cadence = payload["markets"][market]["cadence"]
        assert set(cadence) >= {
            "requested_seconds", "sustainable_seconds", "requests_per_cycle",
            "status", "source", "supportable", "label",
        }
        assert isinstance(cadence["supportable"], bool)
        assert cadence["label"].strip()


# --- Mode credential requirements --------------------------------------------
# These pin the *route* each mode actually uses, so step 3 in the console cannot
# drift into "market data is enough for LIVE" or "PAPER needs an order key".


def test_paper_requires_market_data_only():
    """PAPER reads quotes and fills locally; it never reads an order credential.

    Route evidence: POST /api/markets/paper/session/start -> _ensure_paper_routing
    (mode separation, no key) -> LivePaperEngine.start_session ->
    _fetch_live_quote, which raises unless the market-data credential resolves.
    """
    payload = _launchpad()
    for market, slot in (("NSE", "UPSTOX MARKET DATA"), ("NASDAQ", "ALPACA MARKET DATA")):
        req = payload["markets"][market]["credentials"]["requirements"]["PAPER"]
        assert req["required_slots"] == [slot]
        assert "market-data" in req["note"]
        assert "order credential" in req["note"]


def test_live_requires_market_data_and_the_separate_execution_credential():
    payload = _launchpad()
    nse = payload["markets"]["NSE"]["credentials"]["requirements"]["LIVE"]
    nasdaq = payload["markets"]["NASDAQ"]["credentials"]["requirements"]["LIVE"]
    assert nse["required_slots"] == ["UPSTOX MARKET DATA", "UPSTOX LIVE ACCESS TOKEN"]
    assert nasdaq["required_slots"] == ["ALPACA MARKET DATA", "ALPACA BROKER LIVE"]
    # The broker-paper sandbox key is never a live requirement.
    assert "ALPACA BROKER PAPER" not in nasdaq["required_slots"]


def test_live_requirements_are_a_strict_superset_of_paper():
    payload = _launchpad()
    for market in ("NSE", "NASDAQ"):
        reqs = payload["markets"][market]["credentials"]["requirements"]
        assert set(reqs["PAPER"]["required_slots"]) < set(reqs["LIVE"]["required_slots"])


def test_every_required_slot_names_a_slot_the_matrix_actually_reports():
    """A requirement the UI cannot look up would read as unknown forever."""
    payload = _launchpad()
    for market in ("NSE", "NASDAQ"):
        rows = payload["markets"][market]["credentials"]
        reported = {row["slot"] for row in rows["rows"]}
        for mode in ("PAPER", "LIVE"):
            for slot in rows["requirements"][mode]["required_slots"]:
                assert slot in reported, f"{market} {mode} requires an unreported slot {slot}"


def test_requirements_payload_carries_no_secret_material():
    payload = _launchpad()
    blob = json.dumps(payload["markets"], sort_keys=True)
    for forbidden in ("access_token", "api_secret", "api_key", "ciphertext", "password"):
        assert forbidden not in blob
    for market in ("NSE", "NASDAQ"):
        assert payload["markets"][market]["credentials"]["secret_returned"] is False


def test_route_exposes_requirements_for_both_modes(client: TestClient):
    payload = client.get(
        "/api/markets/start-monitor", headers={"Authorization": "Bearer start-secret"}
    ).json()
    for market in ("NSE", "NASDAQ"):
        reqs = payload["markets"][market]["credentials"]["requirements"]
        assert set(reqs) == {"PAPER", "LIVE"}
        for mode in reqs.values():
            assert mode["required_slots"]
            assert mode["source"].strip()
            assert mode["note"].strip()


# --- 3/6. Credential contract: roles, fail-closed validation ----------------

def _matrix(market="NASDAQ", **kw):
    args = dict(data_present=True, data_source="protected-store",
                broker_paper_present=True, broker_live_present=True)
    args.update(kw)
    return _credential_matrix(market, **args)


def test_every_row_declares_an_explicit_role():
    for market in (NSE, NASDAQ):
        rows = _matrix(market)["rows"]
        assert all(row.get("role") in CREDENTIAL_ROLES for row in rows)
        # Exactly one market_data and one live_execution row per market.
        roles = [row["role"] for row in rows]
        assert roles.count("market_data") == 1
        assert roles.count("live_execution") == 1


def test_nasdaq_paper_broker_role_is_never_a_market_data_or_live_slot():
    matrix = _matrix(NASDAQ)
    resolved = matrix["roles"]["resolved"]
    assert resolved["paper_broker"] == "ALPACA BROKER PAPER"
    assert resolved["market_data"] == "ALPACA MARKET DATA"
    assert resolved["live_execution"] == "ALPACA BROKER LIVE"
    # The paper broker key appears in no required-slot list.
    for mode in ("PAPER", "LIVE"):
        assert "ALPACA BROKER PAPER" not in matrix["requirements"][mode]["required_slots"]


def test_requirements_are_resolved_by_role_not_by_row_order():
    forward = _credential_matrix(
        NASDAQ, data_present=True, data_source="s",
        broker_paper_present=True, broker_live_present=True,
    )
    rows = list(forward["rows"])
    reversed_rows = list(reversed(rows))
    roles = _credential_roles(reversed_rows, NASDAQ)
    assert roles["consistent"] is True
    assert roles["resolved"] == forward["roles"]["resolved"]
    reqs = _mode_credential_requirements(NASDAQ, reversed_rows, roles)
    assert reqs["PAPER"]["required_slots"] == forward["requirements"]["PAPER"]["required_slots"]
    assert reqs["LIVE"]["required_slots"] == forward["requirements"]["LIVE"]["required_slots"]


@pytest.mark.parametrize("mutate,expect", [
    (lambda rows: rows + [dict(rows[0])], "appears"),                       # duplicate slot
    (lambda rows: [dict(rows[0], role="mystery")], "unknown credential role"),
    (lambda rows: [dict(r, role="paper_broker") for r in rows], "market_data"),
    (lambda rows: [], "market_data"),
])
def test_inconsistent_role_assignment_is_never_consistent(mutate, expect):
    rows = mutate(list(_matrix(NASDAQ)["rows"]))
    roles = _credential_roles(rows, NASDAQ)
    assert roles["consistent"] is False
    assert any(expect in p for p in roles["problems"])
    reqs = _mode_credential_requirements(NASDAQ, rows, roles)
    # An inconsistent assignment yields no usable requirement, never "nothing needed".
    assert reqs["PAPER"]["available"] is False
    assert reqs["PAPER"]["required_slots"] == []
    assert reqs["LIVE"]["available"] is False


def test_two_market_data_rows_do_not_silently_pick_one():
    rows = list(_matrix(NASDAQ)["rows"])
    rows.append({"slot": "EXTRA FEED", "role": "market_data", "present": True,
                 "scope": "s", "detail": "d", "affects_execution": False})
    roles = _credential_roles(rows, NASDAQ)
    assert roles["consistent"] is False
    assert roles["resolved"]["market_data"] is None
    assert any("market_data" in p for p in roles["problems"])


# --- 4. LIVE Autopilot requires positive readiness --------------------------

def _autopilot_for(**coordinator_kwargs):
    """NASDAQ Autopilot view with market-data credentials present.

    Without them NO_MARKET_DATA_CREDENTIAL blocks both modes, which would mask
    the readiness condition these tests are about.
    """
    creds = {
        NSE: {"present": True, "source": "protected-store"},
        NASDAQ: {"present": True, "source": "protected-store", "feed": "iex"},
    }
    payload = _launchpad(coordinator=_FakeCoordinator(**coordinator_kwargs),
                         data_credentials=creds)
    return payload["markets"][NASDAQ]["autopilot"]


def _ready_override(**over):
    base = {"ok": True, "checks": {"configuration_complete": True}, "failed": [],
            "missing_dependencies": [],
            "documented_capabilities": {"exit_route": EXIT_US}}
    base.update(over)
    return base


def _active_gate():
    return {NSE: {"state": "DISABLED", "live_available": False},
            NASDAQ: {"state": "ACTIVE", "live_available": True}}


def test_live_autopilot_available_only_when_ready_active_and_clean():
    live = _autopilot_for(
        modes={NSE: "PAPER", NASDAQ: "LIVE"},
        gates=_active_gate(), ready=True, readiness_override=_ready_override(),
    )["live"]
    assert live["available"] is True


def test_live_autopilot_rejected_when_gate_active_but_not_ready():
    live = _autopilot_for(
        modes={NSE: "PAPER", NASDAQ: "LIVE"},
        gates=_active_gate(), ready=False,
        readiness_override=_ready_override(ok=False, failed=["configuration_complete"]),
    )["live"]
    assert live["available"] is False
    assert any(gap["code"] == "LIVE_NOT_READY" for gap in live["gaps"])


def test_live_autopilot_rejected_when_failed_check_but_ok_true():
    """A failing check must block even if the coordinator's own flag is True."""
    live = _autopilot_for(
        modes={NSE: "PAPER", NASDAQ: "LIVE"},
        gates=_active_gate(), ready=True,
        readiness_override=_ready_override(failed=["calendar_session_open"]),
    )["live"]
    assert live["available"] is False
    texts = " ".join(gap["text"] for gap in live["gaps"])
    assert "calendar_session_open" in texts, "the failing check must be named in the blockers"


def test_live_autopilot_rejected_when_readiness_state_is_absent():
    payload = _launchpad(coordinator=_FakeCoordinator(
        modes={NSE: "PAPER", NASDAQ: "LIVE"}, gates=_active_gate(), ready=True,
        readiness_override={},  # no "ok" key at all
    ))
    live = payload["markets"][NASDAQ]["autopilot"]["live"]
    assert live["available"] is False


def test_live_autopilot_rejected_with_missing_dependencies():
    live = _autopilot_for(
        modes={NSE: "PAPER", NASDAQ: "LIVE"},
        gates=_active_gate(), ready=True,
        readiness_override=_ready_override(missing_dependencies=["ALPACA_BROKER_ENABLED"]),
    )["live"]
    assert live["available"] is False


def test_failed_readiness_checks_are_listed_as_blockers():
    payload = _launchpad(coordinator=_FakeCoordinator(
        modes={NSE: "PAPER", NASDAQ: "LIVE"}, gates=_active_gate(), ready=True,
        readiness_override=_ready_override(failed=["risk_limits_configured"]),
    ))
    codes = {b["code"] for b in payload["markets"][NASDAQ]["autopilot"]["blockers"]}
    assert "LIVE_READINESS_CHECK_FAILED" in codes


# --- 5. Every mandatory risk cap must be present and valid ------------------

VALID_LIMITS = {
    "max_order_value": 250000, "max_symbol_exposure": 400000, "max_total_exposure": 900000,
    "max_open_positions": 10, "max_daily_loss": 25000, "max_daily_orders": 40,
    "max_quote_age_seconds": 90, "brokerage_bps": 0,
}


def test_complete_valid_limits_report_configured():
    report = _validate_risk_limits(dict(VALID_LIMITS))
    assert report["configured"] is True
    assert report["problems"] == []
    assert set(report["checked_fields"]) == set(BOUNDED_CAP_FIELDS)


def test_zero_cost_rate_is_preserved_and_not_a_missing_cap():
    report = _validate_risk_limits(dict(VALID_LIMITS, brokerage_bps=0, slippage_bps=0))
    assert report["configured"] is True
    assert report["cost_rates"]["brokerage_bps"] == 0
    assert report["cost_rate_problems"] == []


@pytest.mark.parametrize("field", BOUNDED_CAP_FIELDS)
def test_each_missing_cap_blocks_configuration(field):
    limits = dict(VALID_LIMITS)
    del limits[field]
    report = _validate_risk_limits(limits)
    assert report["configured"] is False
    assert any(field in p for p in report["problems"]), f"missing {field} must be named"


@pytest.mark.parametrize("value", [0, -1, -0.01, True, False, "100", float("nan"), float("inf"), None])
def test_invalid_cap_values_are_rejected(value):
    report = _validate_risk_limits(dict(VALID_LIMITS, max_order_value=value))
    assert report["configured"] is False
    assert report["problems"]


@pytest.mark.parametrize("field", INTEGER_CAP_FIELDS)
def test_count_limits_must_be_whole_numbers(field):
    assert _validate_risk_limits(dict(VALID_LIMITS, **{field: 3.5}))["configured"] is False
    assert _validate_risk_limits(dict(VALID_LIMITS, **{field: 3}))["configured"] is True


def test_runtime_fault_reports_error_and_blocks_every_mode():
    payload = _launchpad(coordinator=_FakeCoordinator(
        blocked="UNRESOLVED_INTENT", risk_limits=dict(VALID_LIMITS)))
    row = payload["markets"][NSE]
    assert row["badge"] == BADGE_ERROR


# --- 1. Every focused-test path referenced by a gate must exist --------------

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_FILES = (
    REPO_ROOT / ".github" / "workflows" / "paper-checks.yml",
    REPO_ROOT / ".github" / "workflows" / "windows-release.yml",
    REPO_ROOT / "packaging" / "windows" / "build_windows.ps1",
)


@pytest.mark.parametrize("gate", GATE_FILES, ids=lambda p: p.name)
def test_every_referenced_test_path_exists(gate):
    """A gate that names a deleted suite fails every run. Catch it statically."""
    assert gate.exists(), f"{gate} is missing"
    body = gate.read_text(encoding="utf-8")
    referenced = sorted(set(re.findall(r"tests/[\w./-]+\.py", body)))
    if not referenced:
        # A workflow may delegate its suites to a script that is itself checked
        # by the parametrisation above; it must not silently run none.
        delegates = [name for name in ("build_windows.ps1", "install_test.ps1", "portable_test.ps1")
                     if name in body]
        assert delegates, f"{gate.name} references no test paths and delegates to no known script"
        return
    missing = [path for path in referenced if not (REPO_ROOT / path).exists()]
    assert missing == [], f"{gate.name} references test paths that do not exist: {missing}"


def test_the_removed_live_vantage_api_is_tested_as_a_removal_contract():
    """The old live-API suite must not be reinstated; its intent lives on."""
    assert not (REPO_ROOT / "tests" / "test_vantage_api.py").exists()
    removal = REPO_ROOT / "tests" / "test_vantage_live_api_removal.py"
    assert removal.exists(), "the removal contract suite must exist"
    text = removal.read_text(encoding="utf-8")
    assert "404" in text, "the removal contract must assert 404 on the removed routes"
    # Both Linux and Windows gates must include it beside the connector suite.
    for gate in GATE_FILES:
        body = gate.read_text(encoding="utf-8")
        if "test_vantage_connector.py" in body:
            assert "test_vantage_live_api_removal.py" in body, (
                f"{gate.name} runs the Vantage connector suite without the removal contract"
            )


# --- 7. Release gates: a branch push must never publish ---------------------

def test_a_branch_push_cannot_publish_a_release():
    """The publish step must require an event AND an explicit input."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "windows-release.yml").read_text(encoding="utf-8")
    lines = [ln for ln in workflow.splitlines() if "gh release create" in ln]
    assert lines, "the publish step was not found"
    # Find the step guard immediately preceding the publish run block.
    publish_idx = workflow.index("gh release create")
    guard = workflow[:publish_idx]
    step_start = guard.rindex("- name: Publish")
    step_guard = guard[step_start:publish_idx]
    assert "github.event_name == 'workflow_dispatch'" in step_guard, (
        "publication must be gated on an explicit dispatch, not a push"
    )
    assert "inputs.publish" in step_guard, "publication must be gated on the publish input"
    # And the build script itself must not publish anything.
    build = (REPO_ROOT / "packaging" / "windows" / "build_windows.ps1").read_text(encoding="utf-8")
    for forbidden in ("gh release create", "gh release upload", "git tag", "git push"):
        assert forbidden not in build, f"build_windows.ps1 must not run {forbidden!r}"


def test_push_triggers_are_limited_to_development_branches():
    workflow = (REPO_ROOT / ".github" / "workflows" / "windows-release.yml").read_text(encoding="utf-8")
    head = workflow[:workflow.index("permissions:")]
    branches = re.findall(r"^\s+- (arena/[\w.-]+)$", head, re.MULTILINE)
    assert branches, "the push trigger lists no branches"
    assert "main" not in branches, "a push to main must not start a build/publish workflow"
    assert all(b.startswith("arena/") for b in branches)
