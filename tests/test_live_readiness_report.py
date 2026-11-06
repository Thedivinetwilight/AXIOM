"""Cross-broker readiness report: names and states, never secret values."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from app.markets.live_execution import (
    LiveExecutionCoordinator,
    LiveMarketAdapter,
    LiveRiskLimits,
    MarketLiveGate,
    MODE_LIVE,
)
from app.markets.live_readiness import (
    READINESS_SCHEMA,
    build_readiness_report,
    open_order_policy,
    render_readiness_text,
)

NOW = datetime(2026, 10, 29, 6, 0, tzinfo=timezone.utc)
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _limits(**overrides) -> LiveRiskLimits:
    base = dict(
        max_order_value=250_000.0,
        max_daily_orders=20,
        max_daily_loss=25_000.0,
        max_open_positions=10,
        max_symbol_exposure=250_000.0,
        max_total_exposure=1_000_000.0,
        max_quote_age_seconds=60.0,
        max_price_deviation_bps=150.0,
    )
    base.update(overrides)
    return LiveRiskLimits(**base)


class _Adapter(LiveMarketAdapter):
    """Gate-backed adapter with no broker behaviour (readiness only)."""

    def __init__(self, market: str, broker: str) -> None:
        self.market = market
        self.broker_name = broker
        self.algo_name = ""
        self.expected_public_ip = ""
        self.gate = MarketLiveGate(market, clock=lambda: NOW)
        self.live_token = False
        self.dependencies: list[str] = []

    def missing_dependencies(self):
        return list(self.dependencies)

    def readiness_checks(self, *, symbol=None):
        return {"probe": True}

    def gate_status(self):
        return self.gate.status()

    def arm(self, checks):
        return self.gate.arm(checks)

    def activate(self, *, confirm_phrase, acknowledge_risk):
        permit = self.gate.activate(confirm_phrase=confirm_phrase, acknowledge_risk=acknowledge_risk)
        return {"active": True, "permit_issued_at": permit.issued_at.isoformat()}

    def disarm(self, reason):
        return self.gate.disarm(reason)

    def engage_kill_switch(self, reason):
        return self.gate.engage_kill_switch(reason)

    def reset_kill_switch(self, *, operator_ack):
        return self.gate.reset_kill_switch(operator_ack=operator_ack)

    def _live_token_present(self) -> bool:
        return self.live_token


def _coordinator(limits=None) -> LiveExecutionCoordinator:
    return LiveExecutionCoordinator(
        journal_path=None,
        adapters={"NSE": _Adapter("NSE", "UPSTOX"), "NASDAQ": _Adapter("NASDAQ", "ALPACA")},
        limits=limits or {"NSE": _limits(), "NASDAQ": _limits()},
        clock=lambda: NOW,
    )


# --------------------------------------------------------------------------- #
# Report content
# --------------------------------------------------------------------------- #
def test_report_covers_both_brokers_and_is_not_ready_by_default():
    coordinator = _coordinator()
    report = build_readiness_report(coordinator, clock=lambda: NOW)
    assert report["schema"] == READINESS_SCHEMA
    assert [row["market"] for row in report["markets"]] == ["NSE", "NASDAQ"]
    assert [row["broker"] for row in report["markets"]] == ["UPSTOX", "ALPACA"]
    assert report["blocking_markets"] == ["NSE", "NASDAQ"]
    assert report["all_markets_live_enabled"] is False
    for row in report["markets"]:
        assert row["mode"] != MODE_LIVE
        assert row["live_enabled"] is False
        assert row["gate_state"] == "DISABLED"
        assert "mode_is_live" in row["failed_checks"]
    coordinator.close()


def test_report_lists_every_credential_by_name_only(monkeypatch):
    monkeypatch.setenv("UPSTOX_LIVE_ACCESS_TOKEN", "SECRET-NSE-TOKEN-VALUE")
    monkeypatch.setenv("ALPACA_BROKER_API_SECRET", "SECRET-ALPACA-VALUE")
    monkeypatch.setenv("AUTONOMY_LIVE_ENABLED", "true")
    coordinator = _coordinator()
    coordinator.adapters["NSE"].live_token = True
    report = build_readiness_report(coordinator, clock=lambda: NOW)
    blob = json.dumps(report, default=str)

    assert "UPSTOX_LIVE_ACCESS_TOKEN" in blob
    assert "ALPACA_BROKER_API_SECRET" in blob
    assert "SECRET-NSE-TOKEN-VALUE" not in blob
    assert "SECRET-ALPACA-VALUE" not in blob

    nse = next(row for row in report["markets"] if row["market"] == "NSE")
    rows = {row["name"]: row for row in nse["credential_requirements"]}
    assert rows["UPSTOX_LIVE_ACCESS_TOKEN"]["configured"] is True
    assert rows["AUTONOMY_LIVE_ENABLED"]["configured"] is True
    assert rows["UPSTOX_ALGO_NAME"]["configured"] is False
    assert "AUTONOMY_LIVE_ENABLED" not in nse["credentials_missing"]

    nasdaq = next(row for row in report["markets"] if row["market"] == "NASDAQ")
    nasdaq_rows = {row["name"]: row for row in nasdaq["credential_requirements"]}
    assert nasdaq_rows["ALPACA_BROKER_API_SECRET"]["configured"] is True
    assert "ALPACA_BROKER_API_KEY" in nasdaq["credentials_missing"]
    coordinator.close()


def test_report_lists_account_permissions_and_acceptance_steps():
    coordinator = _coordinator()
    report = build_readiness_report(coordinator, clock=lambda: NOW)
    nse = next(row for row in report["markets"] if row["market"] == "NSE")
    nasdaq = next(row for row in report["markets"] if row["market"] == "NASDAQ")

    assert any("static IP" in item for item in nse["account_permissions"])
    assert any("paper" in item.lower() for item in nasdaq["account_permissions"])
    assert len(nse["acceptance_steps"]) >= 8
    assert any("SEBI static IP" in step for step in nse["acceptance_steps"])
    assert any("Alpaca paper" in step for step in nasdaq["acceptance_steps"])
    assert nse["acceptance_steps"] != nasdaq["acceptance_steps"]
    assert any("native_windows" in check["name"] for check in nse["platform_checks"])
    coordinator.close()


def test_report_states_its_coverage_limits():
    coordinator = _coordinator()
    report = build_readiness_report(coordinator, clock=lambda: NOW)
    coverage = report["coverage"]
    assert coverage["mock_or_fake_transport_only"] is True
    assert coverage["live_verified"] is False
    assert coverage["windows_acceptance"] == "NOT_RUN"
    assert "not broker behaviour" in coverage["note"]
    assert any("not a claim" in note for note in report["notes"])
    coordinator.close()


def test_readiness_alone_never_means_live_enabled():
    coordinator = _coordinator()
    coordinator.set_mode("NSE", MODE_LIVE)
    coordinator.arm("NSE")
    report = build_readiness_report(coordinator, clock=lambda: NOW)
    nse = next(row for row in report["markets"] if row["market"] == "NSE")
    assert nse["ready"] is True          # every readiness check passed
    assert nse["live_enabled"] is False  # but the gate is only ARMED
    assert nse["gate_state"] == "ARMED"
    assert "NSE" in report["blocking_markets"]
    coordinator.close()


def test_activated_market_is_reported_live_with_its_gate_state():
    coordinator = _coordinator()
    coordinator.set_mode("NSE", MODE_LIVE)
    coordinator.arm("NSE")
    coordinator.activate("NSE", confirm_phrase="ACTIVATE LIVE REAL-MONEY TRADING", acknowledge_risk=True)
    report = build_readiness_report(coordinator, clock=lambda: NOW)
    nse = next(row for row in report["markets"] if row["market"] == "NSE")
    assert nse["live_enabled"] is True
    assert nse["gate_state"] == "ACTIVE"
    assert report["blocking_markets"] == ["NASDAQ"]
    coordinator.close()


def test_kill_switch_appears_in_the_report_with_the_open_order_policy():
    coordinator = _coordinator()
    coordinator.set_mode("NSE", MODE_LIVE)
    coordinator.arm("NSE")
    coordinator.activate("NSE", confirm_phrase="ACTIVATE LIVE REAL-MONEY TRADING", acknowledge_risk=True)
    coordinator.emergency_stop("NSE", reason="drill")
    report = build_readiness_report(coordinator, clock=lambda: NOW)
    nse = next(row for row in report["markets"] if row["market"] == "NSE")
    assert nse["kill_switch"]["engaged"] is True
    assert "drill" in str(nse["kill_switch"]["reason"])
    assert nse["live_enabled"] is False
    assert nse["kill_switch"]["open_order_policy"]["blocks_new_orders"] is True
    coordinator.close()


def test_open_order_policy_documents_working_orders_and_partial_fills():
    policy = open_order_policy()
    assert policy["blocks_new_orders"] is True
    assert "cancel_working" in policy["working_orders"]
    assert "partial" in policy["partial_fills"].lower()
    assert "reconcil" in policy["unresolved_intents"].lower()
    assert "acknowledgement" in policy["reset"].lower()


def test_report_renders_as_operator_text():
    coordinator = _coordinator()
    report = build_readiness_report(coordinator, clock=lambda: NOW)
    text = render_readiness_text(report)
    assert "AXIOM live readiness" in text
    assert "NSE - Upstox" in text
    assert "NASDAQ - Alpaca" in text
    assert "not a live-verification" in text
    assert "token=" not in text.lower()
    coordinator.close()


# --------------------------------------------------------------------------- #
# API and CLI surfaces
# --------------------------------------------------------------------------- #
def test_api_exposes_the_readiness_report_without_secrets(tmp_path, monkeypatch):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from app.api.server import create_app
    from app.config.settings import Settings

    monkeypatch.setenv("ALPACA_BROKER_API_KEY", "SECRET-API-KEY-VALUE")
    settings = replace(Settings(), api_token="readiness-token", database_url=f"sqlite:///{tmp_path}/r.db")
    with TestClient(create_app(settings, data_dir=tmp_path)) as web:
        denied = web.get("/api/markets/live/readiness")
        assert denied.status_code in {401, 403}
        response = web.get(
            "/api/markets/live/readiness", headers={"Authorization": "Bearer readiness-token"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["schema"] == READINESS_SCHEMA
        assert body["coverage"]["live_verified"] is False
        blob = json.dumps(body)
        assert "SECRET-API-KEY-VALUE" not in blob
        assert "readiness-token" not in blob


def test_cli_prints_the_readiness_report(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run(
        [sys.executable, "-m", "app", "live-readiness", "--json"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300, check=False, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout[proc.stdout.index("{"):])
    assert payload["schema"] == READINESS_SCHEMA
    assert payload["coverage"]["mock_or_fake_transport_only"] is True
    assert [row["market"] for row in payload["markets"]] == ["NSE", "NASDAQ"]
    # stdout stays machine-readable; the caveats go to stderr.
    assert "not live verification" in proc.stderr.lower()
    assert proc.stdout.strip().startswith("{") and proc.stdout.strip().endswith("}")
