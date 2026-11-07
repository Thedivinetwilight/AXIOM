"""Owner-run broker preflight: read-only by construction, no secrets, no order calls.

The preflight is *documentation the operator runs*, so the tests pin its
structure: every step is read-only unless it is the explicitly-labelled paper
sandbox cycle, nothing in it can write an order, and no step asks for a
credential value to be pasted anywhere.
"""

from __future__ import annotations

import re

import pytest

from app.markets.live_readiness import (
    FORBIDDEN_IN_PREFLIGHT,
    VERIFICATION_LEVELS,
    broker_preflight,
    verification_state,
)

MARKETS = ("NSE", "NASDAQ")
LEVEL_NAMES = [name for name, _ in VERIFICATION_LEVELS]


@pytest.mark.parametrize("market", MARKETS)
def test_every_step_is_structured_and_safe(market: str) -> None:
    steps = broker_preflight(market)
    assert len(steps) >= 5
    required = {"step", "what", "how", "expect", "read_only", "writes_orders", "level", "failure_means"}
    names = set()
    for step in steps:
        assert required <= set(step), (market, step.get("step"))
        assert step["step"] not in names, f"duplicate preflight step {step['step']}"
        names.add(step["step"])
        assert isinstance(step["read_only"], bool)
        assert isinstance(step["writes_orders"], bool)
        assert step["level"] in LEVEL_NAMES
        assert all(str(step[key]).strip() for key in ("what", "how", "expect", "failure_means"))


@pytest.mark.parametrize("market", MARKETS)
def test_no_preflight_step_claims_read_only_while_writing_orders(market: str) -> None:
    for step in broker_preflight(market):
        if step["writes_orders"]:
            assert step["read_only"] is False
            # The only order-writing step allowed is the broker's own paper host.
            assert step["level"] == "PAPER_SANDBOX"
    # Live levels are read-only: no live order is ever part of the preflight.
    for step in broker_preflight(market):
        if step["level"] in {"READ_ONLY_LIVE", "MOCK"}:
            assert step["read_only"] is True and step["writes_orders"] is False


@pytest.mark.parametrize("market", MARKETS)
def test_preflight_covers_the_five_owner_questions(market: str) -> None:
    steps = {step["step"] for step in broker_preflight(market)}
    assert "credential_configuration_status" in steps
    assert "account_identity" in steps
    assert "market_and_session_eligibility" in steps
    assert "platform_acceptance" in steps
    assert any("permission" in name for name in steps), steps


def test_nse_preflight_states_the_edis_and_static_ip_requirements() -> None:
    steps = {step["step"]: step for step in broker_preflight("NSE")}
    edis = steps["edis_and_settlement_state"]
    assert "long-term-holdings" in edis["how"]
    assert "no edis" in edis["how"].lower()
    assert "t1_quantity" in edis["how"]
    assert "app" in edis["how"].lower()
    platform = steps["platform_acceptance"]
    assert "static ip" in platform["what"].lower()
    assert "SEBI" in platform["what"]
    # The Upstox cash-equity path must never be described as an exit-capable route.
    assert all("exit" not in step["step"] or step["writes_orders"] is False
               for step in broker_preflight("NSE"))


@pytest.mark.parametrize("market", MARKETS)
def test_preflight_never_asks_for_a_credential_value(market: str) -> None:
    text = " ".join(
        f"{step['what']} {step['how']} {step['expect']} {step['failure_means']}"
        for step in broker_preflight(market)
    ).lower()
    for banned in ("paste your token", "paste the token", "enter your api key",
                   "send the secret", "copy the secret", "share the token"):
        assert banned not in text
    assert "masked" in text or "never echoes" in text or "never printed" in text


def test_preflight_mentions_no_order_write_endpoint() -> None:
    forbidden_paths = (
        "/v2/order/place", "/v2/order/multi/place", "/v3/order/gtt",
        "/v3/order/gtt/modify", "/v3/order/gtt/cancel", "/v2/orders", "/v2/order/modify",
    )
    for market in MARKETS:
        blob = " ".join(step["how"] for step in broker_preflight(market))
        for path in forbidden_paths:
            assert path not in blob, (market, path)


def test_unknown_market_has_no_preflight() -> None:
    assert broker_preflight("LSE") == []
    assert broker_preflight("") == []


@pytest.mark.parametrize("market", MARKETS)
def test_verification_ladder_only_credits_mock(market: str) -> None:
    state = verification_state(market)
    assert state["current_level"] == "MOCK"
    assert state["order_path_verified"] is False
    levels = {row["level"]: row for row in state["levels"]}
    assert list(levels) == LEVEL_NAMES
    assert levels["MOCK"]["state"] == "achieved"
    for name in ("PAPER_SANDBOX", "READ_ONLY_LIVE", "ORDER_PATH_LIVE"):
        assert levels[name]["state"] == "not_achieved"
        assert levels[name]["evidence"]


def test_forbidden_list_names_the_order_surface_and_secrets() -> None:
    text = " ".join(FORBIDDEN_IN_PREFLIGHT).lower()
    assert "placing" in text and "cancelling" in text
    assert "credential" in text
    assert "live" in text
    assert len(FORBIDDEN_IN_PREFLIGHT) >= 4


def test_report_payload_carries_preflight_and_ladder() -> None:
    from app.markets.live_execution import LiveExecutionCoordinator
    from app.markets.live_readiness import build_readiness_report

    class _Stub:
        market = "NSE"

        def readiness_checks(self, *, symbol=None):
            return {"stub": True}

        def gate_status(self):
            return {"market": self.market, "state": "DISABLED", "live_available": False}

        def permissions(self):
            return []

        def missing_dependencies(self):
            return ["stub adapter"]

    class _Stub2(_Stub):
        market = "NASDAQ"

    class _Coordinator:
        adapters = {"NSE": _Stub(), "NASDAQ": _Stub2()}

        def readiness(self, market):
            return {"ok": False, "failed": ["stub"], "missing_dependencies": ["stub adapter"]}

        def status(self, market):
            return {"mode": "PAPER", "gate": self.adapters[market].gate_status()}

    report = build_readiness_report(_Coordinator())  # type: ignore[arg-type]
    assert report["verification_levels"] == LEVEL_NAMES
    assert report["preflight_rules"]["read_only_required"] is True
    assert report["preflight_rules"]["forbidden"] == list(FORBIDDEN_IN_PREFLIGHT)
    for row in report["markets"]:
        assert row["broker_preflight"]
        assert row["verification"]["current_level"] == "MOCK"
        assert row["exit_route"]["routed"] is (row["market"] == "NASDAQ")
        blob = repr(row)
        assert not re.search(r"(token|secret|password)\s*[:=]\s*['\"][^'\"]{12,}", blob, re.I)
    assert LiveExecutionCoordinator is not None  # imported for type identity only
