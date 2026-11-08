"""Quota-aware 100-symbol read-only scan: capacity math, service and API contracts.

Everything here is offline. No provider is contacted and no broker call is made;
the "start" path is exercised with an injected evaluator so the test asserts the
service's own guarantees (symbol cap, ``execute_in_paper=False``, orders 0)
without any network.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings
from app.markets.fetch_gate import (
    alpaca_market_data_policies,
    upstox_standard_policies,
)
from app.markets.scan_capacity import (
    ScanCapacityService,
    endpoints_per_category,
    max_symbols_at_cadence,
    min_interval_seconds,
    plan_capacity,
    requests_per_cycle,
)

NSE = "NSE"
NASDAQ = "NASDAQ"
NSE_POLICIES = upstox_standard_policies()
NASDAQ_BASIC = alpaca_market_data_policies("basic")


# --------------------------------------------------------------- capacity math

def test_endpoint_categories_keep_provider_pools_separate():
    # Upstox publishes per-API limits: quote and candle are separate pools.
    assert endpoints_per_category(NSE) == {"upstox.market_quote": 1, "upstox.historical_candle": 1}
    # Alpaca publishes one market-data figure per plan: bars + snapshot share it.
    assert endpoints_per_category(NASDAQ) == {"alpaca.market_data": 2}


def test_requests_per_cycle_counts_one_request_per_symbol_per_upstox_pool():
    assert requests_per_cycle(NSE, 100) == {
        "upstox.market_quote": 100,
        "upstox.historical_candle": 100,
    }
    assert requests_per_cycle(NASDAQ, 100) == {"alpaca.market_data": 200}
    assert requests_per_cycle(NSE, 0) == {"upstox.market_quote": 0, "upstox.historical_candle": 0}


def test_upstox_100_symbols_needs_ninety_seconds_not_fifteen():
    # 2,000 requests / 30 min = 1.111 req/s per category -> 100 requests = 90 s.
    assert min_interval_seconds(NSE, NSE_POLICIES, 100) == 90.0
    assert min_interval_seconds(NSE, NSE_POLICIES, 16) == 14.4
    assert max_symbols_at_cadence(NSE, NSE_POLICIES, 15.0) == 16
    assert max_symbols_at_cadence(NSE, NSE_POLICIES, 60.0) == 66
    assert max_symbols_at_cadence(NSE, NSE_POLICIES, 90.0) == 100


def test_alpaca_basic_100_symbols_needs_sixty_seconds():
    assert min_interval_seconds(NASDAQ, NASDAQ_BASIC, 100) == 60.0
    assert max_symbols_at_cadence(NASDAQ, NASDAQ_BASIC, 60.0) == 100
    assert max_symbols_at_cadence(NASDAQ, NASDAQ_BASIC, 15.0) == 25


def test_plan_marks_fifteen_second_100_symbol_scan_as_unsupported():
    plan = plan_capacity(
        market=NSE,
        symbols=100,
        cadence_s=15,
        policies=NSE_POLICIES,
        quota_profile="upstox_standard",
        usage={},
        credential_present=True,
        plan_state="verified",
        plan_detail="documented",
    )
    assert plan.can_start is False
    assert plan.status == "SLOWER CADENCE REQUIRED"
    assert plan.min_interval_s == 90.0
    assert plan.max_symbols_at_cadence == 16
    assert plan.requests_per_cycle == {
        "upstox.market_quote": 100,
        "upstox.historical_candle": 100,
    }
    assert any("90.0s" in reason for reason in plan.reasons)


def test_plan_ready_at_documented_cadence():
    plan = plan_capacity(
        market=NSE,
        symbols=100,
        cadence_s=90,
        policies=NSE_POLICIES,
        quota_profile="upstox_standard",
        usage={},
        credential_present=True,
        plan_state="verified",
        plan_detail="documented",
    )
    assert plan.can_start is True
    assert plan.status == "READY"
    assert plan.reasons == []


def test_per_second_window_is_paced_not_blocking():
    # 100 requests against a 50/s bucket: the gate spreads them over ~2 s, so the
    # 1-second window must not be reported as a hard block.
    plan = plan_capacity(
        market=NSE,
        symbols=100,
        cadence_s=90,
        policies=NSE_POLICIES,
        quota_profile="upstox_standard",
        usage={"upstox.market_quote": {"1s": 50}, "upstox.historical_candle": {"1s": 50}},
        credential_present=True,
        plan_state="verified",
        plan_detail="documented",
    )
    rows = plan.windows["upstox.market_quote"]
    assert rows[0]["window"] == "1s"
    assert rows[0]["paced"] is True
    assert rows[0]["blocks"] is False
    assert plan.status == "READY"
    assert plan.can_start is True
    assert plan.dispatch_seconds == pytest.approx(2.0)


def test_rolling_window_overflow_is_quota_deferred_with_wait_estimate():
    plan = plan_capacity(
        market=NSE,
        symbols=100,
        cadence_s=90,
        policies=NSE_POLICIES,
        quota_profile="upstox_standard",
        usage={"upstox.market_quote": {"1s": 0, "1m": 450, "30m": 0}},
        credential_present=True,
        plan_state="verified",
        plan_detail="documented",
    )
    assert plan.can_start is False
    assert plan.status == "QUOTA DEFERRED"
    assert plan.blocking_window == "upstox.market_quote · 1m"
    # (550 - 500) * 60 / 500 = 6 s of uniform-ageing estimate, never the whole window.
    assert plan.wait_seconds == pytest.approx(6.0)
    minute = next(row for row in plan.windows["upstox.market_quote"] if row["window"] == "1m")
    assert minute["blocks"] is True
    assert minute["readable"] == "450 / 500 requests this 1 minute"


def test_missing_credential_blocks_start():
    plan = plan_capacity(
        market=NASDAQ,
        symbols=100,
        cadence_s=60,
        policies=NASDAQ_BASIC,
        quota_profile="alpaca_basic",
        usage={},
        credential_present=False,
        plan_state="unverified",
        plan_detail="Basic assumed",
    )
    assert plan.can_start is False
    assert plan.status == "NO CREDENTIAL"


def test_unverified_plan_is_a_state_not_a_blocker():
    plan = plan_capacity(
        market=NASDAQ,
        symbols=100,
        cadence_s=60,
        policies=NASDAQ_BASIC,
        quota_profile="alpaca_basic",
        usage={},
        credential_present=True,
        plan_state="unverified",
        plan_detail="Basic (free) profile assumed; entitlement not verified",
    )
    assert plan.can_start is True
    assert plan.status == "PLAN UNVERIFIED"
    assert any("not verified" in reason for reason in plan.reasons)


def test_symbol_count_is_capped_at_one_hundred():
    plan = plan_capacity(
        market=NSE,
        symbols=500,
        cadence_s=600,
        policies=NSE_POLICIES,
        quota_profile="upstox_standard",
        usage={},
        credential_present=True,
        plan_state="verified",
        plan_detail="documented",
    )
    assert plan.symbols == 100
    assert plan.requests_per_cycle == {
        "upstox.market_quote": 100,
        "upstox.historical_candle": 100,
    }


def test_quota_profile_off_blocks_planning_instead_of_claiming_ready():
    plan = plan_capacity(
        market=NSE,
        symbols=100,
        cadence_s=90,
        policies=(),
        quota_profile="off",
        usage={},
        credential_present=True,
        plan_state="verified",
        plan_detail="documented",
    )
    assert plan.can_start is False
    assert plan.status == "CONFIG REQUIRED"
    assert plan.windows == {}
    assert any("MARKET_DATA_QUOTA_PROFILE" in reason for reason in plan.reasons)


def test_running_scan_blocks_a_second_start():
    plan = plan_capacity(
        market=NSE,
        symbols=100,
        cadence_s=90,
        policies=NSE_POLICIES,
        quota_profile="upstox_standard",
        usage={},
        credential_present=True,
        plan_state="verified",
        plan_detail="documented",
        scan_busy=True,
    )
    assert plan.can_start is False
    assert plan.status == "SCAN IN PROGRESS"


# ------------------------------------------------------------------- service

class _FakeGate:
    """Minimal gate stand-in: records calls, reports usage from a dict."""

    def __init__(self, policies, usage=None):
        self._policies = policies
        self._usage = usage or {}
        self._calls = {p.category: 0 for p in policies}

    def stats(self):
        return {"requests": sum(self._calls.values()), "deferred": 0}

    def quota_usage(self, category):
        return dict(self._usage.get(category, {}))

    def quota_report(self):
        return {
            "categories": {
                p.category: {
                    "requests": self._calls[p.category],
                    "windows": {
                        w.name: {"current": self._usage.get(p.category, {}).get(w.name, 0), "limit": w.limit}
                        for w in p.windows
                    },
                }
                for p in self._policies
            }
        }


def _service(*, credential=True, evaluate=None, limits=None, watchlist=None):
    gates = {"NSE": _FakeGate(NSE_POLICIES), "NASDAQ": _FakeGate(NASDAQ_BASIC)}
    return ScanCapacityService(
        gates=gates,
        policies={"NSE": NSE_POLICIES, "NASDAQ": NASDAQ_BASIC},
        profiles={"NSE": "upstox_standard", "NASDAQ": "alpaca_basic"},
        plan_states={"NSE": ("verified", "documented"), "NASDAQ": ("unverified", "Basic assumed")},
        credential_present=lambda market: credential,
        evaluate=evaluate or (lambda market, **kwargs: {"decisions": [], "rejected_instruments": {}}),
        limits=limits,
        watchlist=watchlist,
    )


def test_service_refuses_start_without_credential():
    svc = _service(credential=False)
    result = svc.start(NSE, symbols=["RELIANCE"], cadence_s=90)
    assert result["started"] is False
    assert result["status"] == "NO CREDENTIAL"


def test_service_refuses_start_when_cadence_exceeds_documented_budget():
    calls = []
    svc = _service(evaluate=lambda market, **kw: calls.append(kw) or {"decisions": []})
    result = svc.start(NSE, symbols=[f"S{i}" for i in range(100)], cadence_s=15)
    assert result["started"] is False
    assert result["status"] == "SLOWER CADENCE REQUIRED"
    assert calls == []


def test_service_refuses_more_than_one_hundred_symbols():
    svc = _service()
    with pytest.raises(ValueError, match="at most 100 symbols"):
        svc.start(NSE, symbols=[f"S{i}" for i in range(101)], cadence_s=90)


def test_started_scan_is_read_only_and_reports_zero_orders():
    seen = {}

    def evaluate(market, **kwargs):
        seen.update(kwargs)
        decisions = [
            {"final_action": "BUY", "selected_qty": 3},
            {"final_action": "NO_TRADE", "selected_qty": 0},
            {"final_action": "SELL", "selected_qty": 2},
        ]
        return {
            "decisions": decisions,
            "rejected_instruments": {
                "S1": {"code": "QUOTA_DEFERRED", "reason": "deferred"},
                "S2": {"code": "STALE_DATA", "reason": "stale"},
                "S3": {"code": "FETCH_ERROR", "reason": "boom"},
            },
            "broker_order_api_calls": 0,
            "orders_submitted_simulated": 0,
        }

    svc = _service(evaluate=evaluate)
    result = svc.start(NSE, symbols=["A", "B"], cadence_s=90)
    assert result["started"] is True
    assert seen["execute_in_paper"] is False
    assert seen["persist_decisions"] is False
    assert seen["symbols"] == ["A", "B"]

    deadline = time.time() + 5
    run = None
    while time.time() < deadline:
        history = svc.history(NSE)
        if history and history[0]["state"] != "SCANNING":
            run = history[0]
            break
        time.sleep(0.02)
    assert run is not None, "scan did not finish"
    assert run["state"] == "COMPLETED"
    assert run["symbols_analyzed"] == 3
    assert run["symbols_deferred"] == 1
    assert run["symbols_stale"] == 1
    assert run["symbols_failed"] == 1
    assert run["candidates_risk_approved"] == 2
    assert run["orders"] == 0
    assert run["orders_submitted"] == 0
    assert run["broker_order_api_calls"] == 0
    assert svc.status()["running"] == {}


def test_evaluate_failure_is_surfaced_as_provider_error_not_a_crash():
    def evaluate(market, **kwargs):
        raise RuntimeError("provider said no")

    svc = _service(evaluate=evaluate)
    assert svc.start(NSE, symbols=["A"], cadence_s=90)["started"] is True
    deadline = time.time() + 5
    while time.time() < deadline:
        history = svc.history(NSE)
        if history and history[0]["state"] != "SCANNING":
            assert history[0]["state"] == "PROVIDER ERROR"
            assert "provider said no" in history[0]["error"]
            assert history[0]["orders"] == 0
            return
        time.sleep(0.02)
    pytest.fail("scan never finished")


def test_snapshot_is_read_only_and_never_starts_traffic():
    gate = _FakeGate(NSE_POLICIES)
    svc = _service()
    svc._gates = {"NSE": gate, "NASDAQ": _FakeGate(NASDAQ_BASIC)}
    snapshot = svc.snapshot(NSE, symbols=100, cadence_s=90)
    assert snapshot["plan"]["status"] == "READY"
    assert snapshot["quota"]["verification"] == "DOCUMENTED"
    assert snapshot["quota"]["policies"][0]["source_url"].startswith("https://")
    assert gate.stats()["requests"] == 0
    assert svc.history(NSE) == []


# ----------------------------------------------------------------------- API

@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        api_token="scan-secret",
        database_url=f"sqlite:///{tmp_path}/scan.db",
        data_source="upstox",
    )
    app = create_app(settings, data_dir=tmp_path)
    with TestClient(app) as test_client:
        test_client.app = app
        yield test_client


def _auth():
    return {"Authorization": "Bearer scan-secret"}


def test_capacity_route_requires_bearer(client):
    assert client.get("/api/ensemble/scan/capacity").status_code == 401
    assert client.post("/api/ensemble/scan/preview", json={"market": "NSE"}).status_code == 401
    assert client.post("/api/ensemble/scan/start", json={"market": "NSE"}).status_code == 401
    assert client.get("/api/ensemble/scan/history").status_code == 401


def test_capacity_route_reports_both_markets_from_server_state(client):
    body = client.get("/api/ensemble/scan/capacity", headers=_auth()).json()
    assert set(body["markets"]) == {"NSE", "NASDAQ"}
    assert body["max_symbols_per_scan"] == 100
    assert body["basis"]["provider_network_calls"] == 0
    assert "does not mean it will be traded" in body["notice"]
    nse = body["markets"]["NSE"]
    assert nse["quota"]["profile"] == "upstox_standard"
    assert nse["quota"]["verification"] == "DOCUMENTED"
    # Documented windows are readable, exactly as the provider publishes them.
    windows = nse["plan"]["window_usage"]["upstox.market_quote"]
    assert [(w["window"], w["limit"]) for w in windows] == [("1s", 50), ("1m", 500), ("30m", 2000)]
    alpaca = body["markets"]["NASDAQ"]
    assert alpaca["quota"]["profile"] == "alpaca_basic"
    assert alpaca["quota"]["verification"] == "UNVERIFIED"
    assert alpaca["plan"]["window_usage"]["alpaca.market_data"][0]["limit"] == 200


def test_preview_consumes_no_quota_and_no_provider_calls(client):
    svc = client.app.state.scan_capacity_service
    before = {m: svc.gate_stats(m).get("requests") for m in ("NSE", "NASDAQ")}
    body = client.post(
        "/api/ensemble/scan/preview",
        headers=_auth(),
        json={"market": "NSE", "symbol_count": 100, "cadence_s": 15},
    ).json()
    assert body["plan"]["status"] in {"SLOWER CADENCE REQUIRED", "NO CREDENTIAL"}
    assert body["plan"]["cadence_sustainable_seconds"] == 90.0
    assert body["plan"]["max_symbols_at_requested_cadence"] == 16
    assert any("used, this cycle" in line for line in body["dispatch_plan"])
    after = {m: svc.gate_stats(m).get("requests") for m in ("NSE", "NASDAQ")}
    assert before == after
    assert svc.history("NSE") == []


def test_start_route_refuses_and_sends_no_request_without_credential(client):
    svc = client.app.state.scan_capacity_service
    calls = []
    svc._evaluate = lambda market, **kw: calls.append(market) or {"decisions": []}
    svc._credential_present = lambda market: False
    body = client.post(
        "/api/ensemble/scan/start",
        headers=_auth(),
        json={"market": "NSE", "symbol_count": 100, "cadence_s": 90},
    ).json()
    assert body["started"] is False
    assert body["status"] == "NO CREDENTIAL"
    assert calls == []


def test_start_route_runs_read_only_and_never_leaks_credentials(client):
    svc = client.app.state.scan_capacity_service
    svc._credential_present = lambda market: True
    svc._evaluate = lambda market, **kw: {
        "decisions": [{"final_action": "BUY", "selected_qty": 1}],
        "rejected_instruments": {},
        "broker_order_api_calls": 0,
    }
    res = client.post(
        "/api/ensemble/scan/start",
        headers=_auth(),
        json={"market": "NSE", "symbols": ["RELIANCE", "TCS"], "cadence_s": 90},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["started"] is True
    assert body["requested_symbols"] == 2
    assert body["dispatch_plan"]
    assert "scan-secret" not in res.text

    deadline = time.time() + 5
    runs = []
    while time.time() < deadline:
        runs = client.get("/api/ensemble/scan/history", headers=_auth(), params={"market": "NSE"}).json()["runs"]
        if runs and runs[0]["state"] != "SCANNING":
            break
        time.sleep(0.02)
    assert runs and runs[0]["state"] == "COMPLETED"
    assert runs[0]["orders"] == 0
    assert runs[0]["candidates_risk_approved"] == 1
    assert runs[0]["live_execution"] == "DISABLED"


def test_start_route_rejects_more_than_one_hundred_symbols(client):
    svc = client.app.state.scan_capacity_service
    svc._credential_present = lambda market: True
    res = client.post(
        "/api/ensemble/scan/start",
        headers=_auth(),
        json={"market": "NSE", "symbols": [f"S{i}" for i in range(101)], "cadence_s": 90},
    )
    assert res.status_code == 422


def test_terminal_page_exposes_the_scan_panel():
    html = (Path(__file__).resolve().parents[1] / "app/static/terminal/index.html").read_text(encoding="utf-8")
    assert 'id="ens-scan-panel"' in html
    assert "Scan Capacity &amp; Quotas" in html
    assert "READ-ONLY MARKET DATA · NO ORDER SUBMISSION" in html
    assert "Scanning a symbol does not mean it will be traded." in html
    # The panel sits inside the Ensemble page, before the ensemble result panels.
    ensemble = html.split('id="page-ensemble"', 1)[1]
    assert ensemble.index('id="ens-scan-panel"') < ensemble.index("1. Strategy Families")
