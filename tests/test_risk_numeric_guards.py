"""Regression tests for fail-closed numeric risk and edge validation.

These controls operate only on in-memory objects. They neither open the
operator's databases nor submit orders.
"""

from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app.engine.sandbox_engine import _gross_expected_for_decision
from app.risk.limits import HardRiskLimits, OrderIntent
from app.risk.net_edge import NetEdgeGate

NONFINITE = (float("nan"), float("inf"), float("-inf"))


@pytest.mark.parametrize(
    "field",
    [
        "max_position_value",
        "max_daily_loss",
        "net_edge_threshold",
        "max_volatility_factor",
        "max_spread_bps",
        "live_canary_max_notional",
    ],
)
@pytest.mark.parametrize("value", NONFINITE)
def test_settings_reject_nonfinite_numeric_controls(field, value):
    with pytest.raises(ValueError, match=field.upper()):
        Settings(**{field: value})


def test_settings_reject_mistyped_integer_risk_limits():
    with pytest.raises(ValueError, match="MAX_OPEN_POSITIONS"):
        Settings(max_open_positions=1.5)
    with pytest.raises(ValueError, match="MAX_TRADES_PER_DAY"):
        Settings(max_trades_per_day=True)


@pytest.mark.parametrize("field", ["max_position_value", "max_daily_loss"])
@pytest.mark.parametrize("value", NONFINITE)
def test_hard_risk_limits_reject_nonfinite_caps(field, value):
    with pytest.raises(ValueError, match=field.upper()):
        HardRiskLimits(**{field: value})


def test_from_settings_does_not_truncate_fractional_integer_caps():
    values = SimpleNamespace(
        max_position_value=1000.0,
        max_open_positions=1.5,
        max_daily_loss=100.0,
        max_trades_per_day=2,
    )
    with pytest.raises(ValueError, match="MAX_OPEN_POSITIONS"):
        HardRiskLimits.from_settings(values)


def _check(limits, intent, **overrides):
    state = {
        "open_positions": 0,
        "open_position_value": 0.0,
        "trades_today": 0,
        "realized_today": 0.0,
        "unrealized": 0.0,
    }
    state.update(overrides)
    return limits.check(intent, **state)


@pytest.mark.parametrize(
    "intent",
    [
        OrderIntent("TEST", "BUY", float("nan"), 100.0),
        OrderIntent("TEST", "BUY", 1.0, float("inf")),
        OrderIntent("TEST", "BUY", 1.0, 100.0, estimated_fees=float("nan")),
        OrderIntent("TEST", "SHORT", 1.0, 100.0),
    ],
)
def test_invalid_order_intents_fail_closed(intent):
    decision = _check(HardRiskLimits(), intent)
    assert decision.allowed is False
    assert decision.reason == "invalid_order_intent"


@pytest.mark.parametrize(
    "field,value",
    [
        ("open_positions", float("nan")),
        ("open_position_value", float("inf")),
        ("trades_today", -1),
        ("realized_today", float("nan")),
        ("unrealized", float("inf")),
    ],
)
def test_invalid_buy_risk_state_fails_closed(field, value):
    decision = _check(
        HardRiskLimits(), OrderIntent("TEST", "BUY", 1.0, 100.0),
        **{field: value},
    )
    assert decision.allowed is False
    assert decision.reason == "invalid_risk_state"


def test_valid_sell_remains_allowed_even_when_account_snapshot_is_invalid():
    decision = _check(
        HardRiskLimits(max_position_value=1000.0),
        OrderIntent("TEST", "SELL", 1.0, 100.0),
        open_position_value=float("nan"),
        realized_today=float("nan"),
    )
    assert decision.allowed is True
    assert decision.reason == "risk_reducing_exit_allowed"


def test_engine_does_not_invent_an_edge_from_router_confidence():
    assert _gross_expected_for_decision(None) is None
    assert _gross_expected_for_decision({"confidence": 1.0}) is None
    assert _gross_expected_for_decision({"gross_expected_pnl": 25.0}) == 25.0


def test_engine_blocks_entry_without_explicit_gross_estimate(tmp_path):
    from datetime import datetime, timezone

    import httpx

    from app.brokers.sandbox import UpstoxSandboxBroker
    from app.data.models import Candle
    from app.database.database import Database
    from app.database.repository import Repository
    from app.engine.sandbox_engine import SandboxEngine
    from app.learning.journal import LearningJournal
    from app.strategies.base import Signal

    instrument_key = "NSE_EQ|TEST"
    transport_calls = []

    def mock_transport(request):
        transport_calls.append(request)
        return httpx.Response(500)

    class Provider:
        symbols = ["TEST"]

        def metrics(self):
            return {"market_open": True, "stale_feed": False}

    class AlwaysBuy:
        name = "test-strategy"
        parameters = {}
        history_limit = 5

        def generate_signal(self, _candles):
            return Signal.BUY

    database = Database(f"sqlite:///{tmp_path / 'paper-risk.db'}")
    repository = Repository(database)
    broker = UpstoxSandboxBroker(
        "test-only-token",
        instrument_key,
        transport=httpx.MockTransport(mock_transport),
        repository=repository,
        initial_capital=100_000.0,
    )
    broker.instrument_map["TEST"] = instrument_key
    settings = Settings(commission=0.001, slippage=0.002, spread=0.004)
    engine = SandboxEngine(
        provider=Provider(),
        strategy=AlwaysBuy(),
        broker=broker,
        repository=repository,
        risk_limits=HardRiskLimits(),
        journal=LearningJournal(repository),
        timeframe="1m",
        settings=settings,
        instrument_keys={"TEST": instrument_key},
        net_edge_gate=NetEdgeGate(),
    )
    assert engine.commission_rate == pytest.approx(settings.commission)
    assert engine.slippage_rate == pytest.approx(settings.slippage)
    assert engine.cost_rate == pytest.approx(
        settings.commission + settings.slippage + settings.spread / 2.0)
    # A high router score alone is not expected P&L evidence.
    engine.last_router_decision = {"confidence": 1.0}
    engine.process_candle(Candle(
        datetime(2026, 10, 19, 9, 15, tzinfo=timezone.utc),
        99.9, 100.1, 99.8, 100.0, 10_000, "TEST"))

    rejects = [event for event in repository.list_system_events(limit=20)
               if event["kind"] == "NET_EDGE_REJECT"]
    assert rejects
    assert "missing gross_expected" in rejects[-1]["payload"]["reason"]
    assert transport_calls == []  # the mocked broker never received a request
    database.close()


def test_net_edge_threshold_is_applied_to_notional_in_currency_units():
    gate = NetEdgeGate(threshold=0.001)  # 10 basis points of notional
    costs = {
        "price": 100.0,
        "quantity": 1000.0,
        "commission_rate": 0.0,
        "slippage_rate": 0.0,
        "spread_bps": 0.0,
    }

    one_basis_point = gate.evaluate(gross_expected=10.0, **costs)
    fifteen_basis_points = gate.evaluate(gross_expected=150.0, **costs)

    assert one_basis_point.net_expected == pytest.approx(10.0)
    assert one_basis_point.threshold == pytest.approx(0.001)
    assert one_basis_point.threshold_amount == pytest.approx(100.0)
    assert one_basis_point.passes is False
    assert fifteen_basis_points.threshold_amount == pytest.approx(100.0)
    assert fifteen_basis_points.passes is True


def test_net_edge_costs_match_per_side_execution_config():
    result = NetEdgeGate(threshold=0.0).evaluate(
        gross_expected=30.0,
        price=100.0,
        quantity=100.0,
        commission_rate=0.0005,
        slippage_rate=0.0005,
        spread_bps=2.0,
    )
    assert result.fees == pytest.approx(10.0)  # 5 bps each way
    assert result.slippage == pytest.approx(10.0)  # 5 bps each way
    assert result.spread_cost == pytest.approx(2.0)  # full 2 bps spread
    assert result.net_expected == pytest.approx(8.0)


def test_positive_net_edge_threshold_requires_a_valid_notional():
    result = NetEdgeGate(threshold=0.001).evaluate(gross_expected=1.0)
    assert result.passes is False
    assert "notional" in result.reason.lower()


@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), -0.001])
def test_net_edge_gate_rejects_invalid_threshold_configuration(threshold):
    with pytest.raises(ValueError, match="threshold"):
        NetEdgeGate(threshold=threshold)


def test_invalid_cost_type_fails_closed_without_exception():
    result = NetEdgeGate(threshold=0.0).evaluate(
        gross_expected=10.0,
        price=100.0,
        quantity=1.0,
        fees="not-a-number",
    )
    assert result.passes is False
    assert "cost" in result.reason.lower()
