"""LIVE-MARKET LIVE execution: separation from paper, per-market gates, risk, reconciliation."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import httpx
from fastapi.testclient import TestClient

from app.alpaca_broker.client import LIVE_HOST, AlpacaReadOnlyClient, AlpacaTradingClient
from app.alpaca_broker.service import AlpacaBrokerService

from app.alpaca_broker.gate import BrokerLiveGate
from app.alpaca_broker.service import BrokerRiskLimits
from app.api.server import create_app
from app.autonomy.config import AutonomySettings
from app.autonomy.models import AccountSnapshot, Submission
from app.autonomy.upstox import InstrumentCatalog
from app.config.settings import Settings
from app.markets.live_execution import (
    MODE_LIVE,
    MODE_PAPER,
    GateState,
    LiveBrokerRejected,
    LiveExecutionCoordinator,
    LiveGateRefused,
    LiveMarketAdapter,
    LiveRiskLimits,
    LiveSubmissionUncertain,
    MarketLiveGate,
    MultiMarketLiveRunner,
    NasdaqLiveAdapter,
    NseLiveAdapter,
)
from app.markets.live_paper import LiveMarketPaperEngine, MultiMarketPaperRunner

NOW = datetime(2026, 10, 29, 6, 0, tzinfo=timezone.utc)  # 11:30 IST (NSE open)


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


def _decision(**overrides) -> dict:
    base = {
        "market": "NSE",
        "symbol": "RELIANCE",
        "final_action": "BUY",
        "raw_intent": "BUY",
        "selected_qty": 10.0,
        "reference_price": 1400.0,
        "stop_loss": 1380.0,
        "take_profit": 1450.0,
        "product": "D",
        "bar_timestamp": "2026-10-29T05:30:00+00:00",
        "decision_id": "dec-1",
    }
    base.update(overrides)
    return base


class FakeAdapter(LiveMarketAdapter):
    """Deterministic adapter: real gate semantics, controllable broker behaviour."""

    def __init__(self, market: str, currency: str, *, clock, price: float = 1400.0):
        self.market = market
        self.currency = currency
        self.broker_name = f"{market} fake broker"
        self.destination_label = f"{market} · FAKE"
        self.gate = MarketLiveGate(market, clock=clock)
        self._clock = clock
        self.price = price
        self.quote_timestamp = clock()
        self.quote_delayed = False
        self.open = True
        self.equity = 1_000_000.0
        self.day_pnl = 0.0
        self.positions_data: list[dict] = []
        self.submit_calls: list[dict] = []
        self.cancel_calls: list[str] = []
        self.submit_error: Exception | None = None
        self.submit_delay = 0.0
        self.pending_dependencies: list[str] = []

    def missing_dependencies(self) -> list[str]:
        return list(self.pending_dependencies)

    def readiness_checks(self, *, symbol=None) -> dict[str, bool]:
        return {"fake_ready": True}

    def gate_status(self):
        return self.gate.status()

    def arm(self, checks):
        return self.gate.arm(checks)

    def activate(self, *, confirm_phrase: str, acknowledge_risk: bool):
        permit = self.gate.activate(confirm_phrase=confirm_phrase, acknowledge_risk=acknowledge_risk)
        return {"active": True, "permit_issued_at": permit.issued_at.isoformat()}

    def disarm(self, reason: str):
        return self.gate.disarm(reason)

    def engage_kill_switch(self, reason: str):
        return self.gate.engage_kill_switch(reason)

    def reset_kill_switch(self, *, operator_ack: bool):
        return self.gate.reset_kill_switch(operator_ack=operator_ack)

    def account(self) -> dict:
        return {
            "market": self.market,
            "currency": self.currency,
            "equity": self.equity,
            "buying_power": self.equity,
            "day_pnl": self.day_pnl,
            "trading_blocked": False,
        }

    def positions(self) -> list[dict]:
        return list(self.positions_data)

    def quote(self, symbol: str):
        return {
            "symbol": symbol,
            "bid": self.price - 0.10,
            "ask": self.price + 0.10,
            "last": self.price,
            "timestamp": self.quote_timestamp.isoformat(),
            "source_timestamp": self.quote_timestamp.isoformat(),
            "is_delayed": self.quote_delayed,
        }

    def market_open(self, now: datetime) -> bool:
        return self.open

    def submit(self, *, client_order_id, symbol, side, qty, reference_price,
               stop_loss, take_profit, product, instrument_meta):
        if self.submit_delay:
            import time as _time

            _time.sleep(self.submit_delay)
        if self.submit_error is not None:
            raise self.submit_error
        self.submit_calls.append(
            {
                "client_order_id": client_order_id,
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "reference_price": reference_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }
        )
        return {
            "client_order_id": client_order_id,
            "broker_order_id": f"fake-{len(self.submit_calls)}",
            "symbol": symbol,
            "side": side.upper(),
            "qty": qty,
            "status": "ACCEPTED",
            "real_money": True,
            "protection": {"ok": True},
        }

    def reconcile(self, unresolved: dict[str, str]) -> dict:
        return {"ok": True, "resolved_intents": len(unresolved), "adopted_orders": 0}

    def cancel(self, client_order_id: str) -> dict:
        self.cancel_calls.append(client_order_id)
        return {"ok": True}


class StubEnsemble:
    def __init__(self, decisions: dict[str, list[dict]]):
        self.decisions = decisions
        self.calls: list[tuple[str, dict]] = []

    def evaluate_watchlist_cycle(self, market: str, **kwargs):
        self.calls.append((market, kwargs))
        return {"decisions": self.decisions.get(market, []), "enabled_variants_count": 12}


class StubEngine:
    def __init__(self, symbols: dict[str, list[str]]):
        self.symbols = symbols

    def _effective_symbols_for_market(self, market: str) -> list[str]:
        return self.symbols.get(market, [])

    def market_snapshot(self, market: str) -> dict:
        return {"session": {"timeframe": "5m"}, "session_state": "RUNNING"}


def _coordinator(tmp_path: Path | None = None, *, nse=None, nasdaq=None, clock=lambda: NOW,
                 limits=None, paper_engine=None) -> LiveExecutionCoordinator:
    nse = nse or FakeAdapter("NSE", "INR", clock=clock)
    nasdaq = nasdaq or FakeAdapter("NASDAQ", "USD", clock=clock, price=220.0)
    return LiveExecutionCoordinator(
        journal_path=(tmp_path / "live.sqlite3") if tmp_path else None,
        adapters={"NSE": nse, "NASDAQ": nasdaq},
        limits=limits or {"NSE": _limits(), "NASDAQ": _limits(max_order_value=25_000.0)},
        paper_engine=paper_engine,
        clock=clock,
    )


def _armed_active(coordinator: LiveExecutionCoordinator, market: str = "NSE") -> None:
    coordinator.set_mode(market, MODE_LIVE)
    coordinator.arm(market)
    coordinator.activate(
        market,
        confirm_phrase="ACTIVATE LIVE REAL-MONEY TRADING",
        acknowledge_risk=True,
    )


# ---------------------------------------------------------------------------
# Default posture: off, disarmed, zero broker calls
# ---------------------------------------------------------------------------


def test_defaults_are_paper_with_disabled_gates_and_zero_broker_calls():
    coordinator = _coordinator()
    status = coordinator.status()
    assert status["NSE"]["mode"] == MODE_PAPER
    assert status["NASDAQ"]["mode"] == MODE_PAPER
    assert status["NSE"]["gate"]["state"] == GateState.DISABLED.value
    assert coordinator.live_status_summary()["real_money_enabled"] is False
    adapter = coordinator.adapters["NSE"]
    result = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert result["status"] == "SKIPPED_PAPER_MODE"
    assert adapter.submit_calls == []
    cycle = coordinator.advance_market("NSE", StubEnsemble({}), symbols=["RELIANCE"])
    assert cycle["status"] == "SKIPPED_MODE_NOT_LIVE"
    assert adapter.submit_calls == []
    coordinator.close()


def test_zero_unconfigured_limits_fail_closed_and_must_be_explicit():
    coordinator = _coordinator(limits={"NSE": LiveRiskLimits(), "NASDAQ": LiveRiskLimits()})
    coordinator.set_mode("NSE", MODE_LIVE)
    report = coordinator.readiness("NSE")
    assert report["ok"] is False
    assert "risk_limits_configured" in report["failed"]
    with pytest.raises(LiveGateRefused):
        coordinator.arm("NSE")
    with pytest.raises(Exception):
        coordinator.update_limits("NSE", max_order_value=1000.0)  # partial => fail closed
    coordinator.update_limits(
        "NSE",
        max_order_value=250_000.0,
        max_daily_orders=20,
        max_daily_loss=25_000.0,
        max_open_positions=10,
        max_symbol_exposure=250_000.0,
        max_total_exposure=1_000_000.0,
        max_quote_age_seconds=60.0,
        max_price_deviation_bps=150.0,
    )
    assert coordinator.readiness("NSE")["checks"]["risk_limits_configured"] is True
    coordinator.close()


# ---------------------------------------------------------------------------
# Arm / Activate
# ---------------------------------------------------------------------------


def test_activation_requires_mode_readiness_arm_and_the_exact_phrase():
    coordinator = _coordinator()
    with pytest.raises(LiveGateRefused):
        coordinator.activate(
            "NSE",
            confirm_phrase="ACTIVATE LIVE REAL-MONEY TRADING",
            acknowledge_risk=True,
        )
    coordinator.set_mode("NSE", MODE_LIVE)
    with pytest.raises(LiveGateRefused):
        coordinator.activate(
            "NSE",
            confirm_phrase="ACTIVATE LIVE REAL-MONEY TRADING",
            acknowledge_risk=True,
        )
    coordinator.arm("NSE")
    with pytest.raises(LiveGateRefused):
        coordinator.activate("NSE", confirm_phrase="activate live", acknowledge_risk=True)
    with pytest.raises(LiveGateRefused):
        coordinator.activate(
            "NSE",
            confirm_phrase="ACTIVATE LIVE REAL-MONEY TRADING",
            acknowledge_risk=False,
        )
    result = coordinator.activate(
        "NSE",
        confirm_phrase="ACTIVATE LIVE REAL-MONEY TRADING",
        acknowledge_risk=True,
    )
    assert result["gate"]["state"] == GateState.ACTIVE.value
    assert result["gate"]["live_available"] is True
    assert coordinator.adapters["NSE"].submit_calls == []
    coordinator.close()


def test_readiness_failure_blocks_arm_by_name():
    coordinator = _coordinator()
    coordinator.adapters["NSE"].pending_dependencies = ["missing live token"]
    coordinator.set_mode("NSE", MODE_LIVE)
    report = coordinator.readiness("NSE")
    assert report["ok"] is False
    with pytest.raises(LiveGateRefused, match="readiness failed"):
        coordinator.arm("NSE")
    coordinator.close()


# ---------------------------------------------------------------------------
# Submission, idempotency, restart disarming
# ---------------------------------------------------------------------------


def test_active_live_gate_submits_once_and_suppresses_duplicate_bars():
    coordinator = _coordinator()
    _armed_active(coordinator)
    adapter = coordinator.adapters["NSE"]
    first = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert first["status"] == "SUBMITTED", first
    assert first["broker_order_id"] == "fake-1"
    assert len(adapter.submit_calls) == 1
    duplicate = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert duplicate["status"] == "DUPLICATE_SUPPRESSED"
    assert len(adapter.submit_calls) == 1
    coordinator.close()


def test_restart_leaves_live_disarmed_and_blocks_new_orders(tmp_path: Path):
    coord1 = _coordinator(tmp_path)
    _armed_active(coord1)
    assert coord1.status("NSE")["gate"]["state"] == GateState.ACTIVE.value
    coord1.close()

    coord2 = _coordinator(tmp_path)
    status = coord2.status("NSE")
    assert status["mode"] == MODE_LIVE  # operator intent persists...
    assert status["gate"]["state"] == GateState.DISABLED.value  # ...but the permit never does
    result = coord2.submit_decision("NSE", _decision(), session_open=True)
    assert result["status"] == "SKIPPED_GATE_NOT_ACTIVE"
    assert coord2.adapters["NSE"].submit_calls == []
    coord2.close()


def test_idempotency_survives_a_restart_of_the_coordinator(tmp_path: Path):
    coord1 = _coordinator(tmp_path)
    _armed_active(coord1)
    first = coord1.submit_decision("NSE", _decision(), session_open=True)
    assert first["status"] == "SUBMITTED"
    coord1.close()

    coord2 = _coordinator(tmp_path)
    adapter = coord2.adapters["NSE"]
    # Even after the operator re-arms and re-activates, the persisted journal suppresses
    # a replay of an already-sent bar (idempotency is not process-local).
    _armed_active(coord2)
    replay = coord2.submit_decision("NSE", _decision(), session_open=True)
    assert replay["status"] == "DUPLICATE_SUPPRESSED"
    assert replay["client_order_id"] == first["client_order_id"]
    assert adapter.submit_calls == []
    # The journal still knows about the previously sent order.
    assert coord2.journal.has_order("NSE", first["client_order_id"]) is True
    coord2.close()


# ---------------------------------------------------------------------------
# Central risk gate
# ---------------------------------------------------------------------------


def test_risk_gate_rejects_oversized_order_without_touching_the_broker():
    coordinator = _coordinator(limits={"NSE": _limits(max_order_value=1_000.0), "NASDAQ": _limits()})
    _armed_active(coordinator)
    result = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert result["status"] == "REJECTED_RISK_GATE"
    failed = {c["check"] for c in result["checks"] if not c["ok"]}
    assert "max_order_value" in failed
    assert result["broker_order_api_calls"] == 0
    assert coordinator.adapters["NSE"].submit_calls == []
    coordinator.close()


def test_risk_gate_rejects_stale_delayed_and_mispriced_quotes():
    coordinator = _coordinator()
    _armed_active(coordinator)
    adapter = coordinator.adapters["NSE"]
    adapter.quote_timestamp = NOW - timedelta(minutes=10)
    stale = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert stale["status"] == "REJECTED_RISK_GATE"
    assert "quote_freshness" in {c["check"] for c in stale["checks"] if not c["ok"]}

    adapter.quote_timestamp = NOW
    adapter.quote_delayed = True
    delayed = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert delayed["status"] == "REJECTED_RISK_GATE"

    adapter.quote_delayed = False
    mispriced = coordinator.submit_decision(
        "NSE", _decision(reference_price=1700.0), session_open=True
    )
    assert mispriced["status"] == "REJECTED_RISK_GATE"
    assert adapter.submit_calls == []
    coordinator.close()


def test_risk_gate_rejects_daily_loss_daily_orders_and_position_caps():
    coordinator = _coordinator(limits={"NSE": _limits(max_daily_orders=1), "NASDAQ": _limits()})
    _armed_active(coordinator)
    adapter = coordinator.adapters["NSE"]
    adapter.day_pnl = -30_000.0
    loss = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert loss["status"] == "REJECTED_RISK_GATE"
    adapter.day_pnl = 0.0
    first = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert first["status"] == "SUBMITTED"
    second = coordinator.submit_decision(
        "NSE", _decision(bar_timestamp="2026-10-29T05:35:00+00:00"), session_open=True
    )
    assert second["status"] == "REJECTED_RISK_GATE"
    assert "max_daily_orders" in {c["check"] for c in second["checks"] if not c["ok"]}
    coordinator.close()


def test_closed_market_analyzes_but_never_submits():
    coordinator = _coordinator()
    _armed_active(coordinator)
    coordinator.adapters["NSE"].open = False
    result = coordinator.submit_decision("NSE", _decision(), session_open=False)
    assert result["status"] == "SKIPPED_MARKET_CLOSED"
    assert result["broker_order_api_calls"] == 0
    assert coordinator.adapters["NSE"].submit_calls == []
    coordinator.close()


def test_naked_short_is_refused_without_an_existing_long():
    coordinator = _coordinator()
    _armed_active(coordinator)
    result = coordinator.submit_decision(
        "NSE",
        _decision(final_action="SELL", stop_loss=None, take_profit=None),
        session_open=True,
    )
    assert result["status"] == "REJECTED_RISK_GATE"
    assert "naked shorts" in result["reasons"][0].lower()
    assert coordinator.adapters["NSE"].submit_calls == []
    coordinator.close()


def test_closed_market_cycle_analyzes_every_symbol_and_submits_nothing():
    coordinator = _coordinator()
    _armed_active(coordinator)
    coordinator.adapters["NSE"].open = False
    ensemble = StubEnsemble({"NSE": [_decision(), _decision(symbol="TCS", decision_id="dec-2")]})
    coordinator.paper_engine = StubEngine({"NSE": ["RELIANCE", "TCS"]})
    out = coordinator.advance_market("NSE", ensemble, symbols=["RELIANCE", "TCS"])
    assert out["status"] == "ANALYZED_ONLY"
    assert out["session_open"] is False
    assert out["symbols"] == ["RELIANCE", "TCS"]
    assert out["decisions"] == 2
    assert out["submitted"] == 0
    assert out["broker_order_api_calls"] == 0
    kwargs = ensemble.calls[0][1]
    assert kwargs["execute_in_paper"] is False
    assert kwargs["session_open_override"] is False
    assert kwargs["persist_decisions"] is False
    assert coordinator.adapters["NSE"].submit_calls == []
    coordinator.close()


def test_open_market_cycle_routes_the_ensemble_decision_through_the_risk_gate():
    coordinator = _coordinator()
    _armed_active(coordinator)
    ensemble = StubEnsemble({"NSE": [_decision()]})
    coordinator.paper_engine = StubEngine({"NSE": ["RELIANCE"]})
    out = coordinator.advance_market("NSE", ensemble, symbols=["RELIANCE"])
    assert out["status"] == "EXECUTED"
    assert out["session_open"] is True
    assert out["decisions"] == 1
    assert out["submitted"] == 1
    assert out["broker_order_api_calls"] == 1
    kwargs = ensemble.calls[0][1]
    assert kwargs["execute_in_paper"] is False, "live cycles must never execute inside the paper engine"
    assert kwargs["invoke_kronos"] is True, "Kronos stays advisory inside the ensemble evaluation"
    assert kwargs["persist_decisions"] is True
    assert len(coordinator.adapters["NSE"].submit_calls) == 1
    coordinator.close()


def test_readiness_probe_failure_fails_closed_without_leaking_details():
    coordinator = _coordinator()
    coordinator.set_mode("NSE", MODE_LIVE)

    def broken(*, symbol=None):
        raise RuntimeError("upstream provider 500 token=leaky-value")

    coordinator.adapters["NSE"].readiness_checks = broken  # type: ignore[assignment]
    report = coordinator.readiness("NSE")
    assert report["ok"] is False
    assert "readiness_probe" in report["failed"]
    assert "leaky-value" not in str(report)
    with pytest.raises(LiveGateRefused):
        coordinator.arm("NSE")
    coordinator.close()


def test_missing_or_failing_quote_provider_blocks_before_the_broker():
    coordinator = _coordinator()
    _armed_active(coordinator)
    adapter = coordinator.adapters["NSE"]

    def boom(symbol: str):
        raise RuntimeError("quote provider HTTP 503")

    adapter.quote = boom  # type: ignore[assignment]
    failed = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert failed["status"] == "REJECTED_RISK_GATE"
    assert "quote_available" in {c["check"] for c in failed["checks"] if not c["ok"]}

    adapter.quote = lambda symbol: {}  # type: ignore[assignment]
    empty = coordinator.submit_decision(
        "NSE", _decision(bar_timestamp="2026-10-29T05:55:00+00:00"), session_open=True
    )
    assert empty["status"] == "REJECTED_RISK_GATE"
    assert "quote_available" in {c["check"] for c in empty["checks"] if not c["ok"]}
    assert adapter.submit_calls == []
    coordinator.close()


def test_crossed_or_zero_bid_ask_never_reaches_the_broker():
    coordinator = _coordinator()
    _armed_active(coordinator)
    adapter = coordinator.adapters["NSE"]
    adapter.quote = lambda symbol: {  # type: ignore[assignment]
        "bid": 1401.0,
        "ask": 1399.0,
        "last": 1400.0,
        "timestamp": NOW.isoformat(),
        "source_timestamp": NOW.isoformat(),
    }
    result = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert result["status"] == "REJECTED_RISK_GATE"
    assert adapter.submit_calls == []
    coordinator.close()


# ---------------------------------------------------------------------------
# Uncertain / rejected broker outcomes + reconciliation
# ---------------------------------------------------------------------------


def test_uncertain_submission_latches_unresolved_disarms_and_requires_reconcile():
    coordinator = _coordinator()
    _armed_active(coordinator)
    adapter = coordinator.adapters["NSE"]
    adapter.submit_error = LiveSubmissionUncertain("timeout after send")
    result = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert result["status"] == "SUBMISSION_UNCERTAIN"
    assert result["reconcile_required"] is True
    status = coordinator.status("NSE")
    assert status["gate"]["state"] == GateState.DISABLED.value
    assert status["blocked"] == "UNRESOLVED_ORDER_INTENT"
    assert status["unresolved_intents"]
    blocked = coordinator.submit_decision(
        "NSE", _decision(bar_timestamp="2026-10-29T05:40:00+00:00"), session_open=True
    )
    assert blocked["status"] in {"SKIPPED_GATE_NOT_ACTIVE", "SKIPPED_BLOCKED"}
    assert blocked["broker_order_api_calls"] == 0
    # The unresolved intent also blocks re-arming until reconciliation proves broker state.
    with pytest.raises(LiveGateRefused, match="no_unresolved_intents"):
        coordinator.arm("NSE")
    reconciled = coordinator.reconcile("NSE")
    assert reconciled["ok"] is True
    assert coordinator.status("NSE")["blocked"] is None
    adapter.submit_error = None
    _armed_active(coordinator)
    resumed = coordinator.submit_decision(
        "NSE", _decision(bar_timestamp="2026-10-29T05:40:00+00:00"), session_open=True
    )
    assert resumed["status"] == "SUBMITTED"
    coordinator.close()


def test_broker_rejection_is_reported_and_does_not_latch_uncertainty():
    coordinator = _coordinator()
    _armed_active(coordinator)
    adapter = coordinator.adapters["NSE"]
    adapter.submit_error = LiveBrokerRejected("exchange rejected the GTT route")
    result = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert result["status"] == "REJECTED_BROKER"
    assert coordinator.status("NSE")["blocked"] is None
    assert coordinator.status("NSE")["unresolved_intents"] == []
    assert coordinator.status("NSE")["gate"]["state"] == GateState.ACTIVE.value
    coordinator.close()


# ---------------------------------------------------------------------------
# Per-market isolation and emergency stop
# ---------------------------------------------------------------------------


def test_market_failure_and_emergency_stop_do_not_affect_the_other_market():
    coordinator = _coordinator()
    _armed_active(coordinator, "NSE")
    _armed_active(coordinator, "NASDAQ")
    nse = coordinator.adapters["NSE"]
    nasdaq = coordinator.adapters["NASDAQ"]

    nse.submit_error = RuntimeError("provider exploded")
    with pytest.raises(RuntimeError):
        coordinator.submit_decision("NSE", _decision(), session_open=True)

    us = coordinator.submit_decision(
        "NASDAQ",
        _decision(symbol="AAPL", reference_price=220.0, stop_loss=210.0, take_profit=240.0, selected_qty=5.0),
        session_open=True,
    )
    assert us["status"] == "SUBMITTED"
    assert len(nasdaq.submit_calls) == 1

    stopped = coordinator.emergency_stop("NSE", "NSE-only halt")
    assert stopped["market"] == "NSE"
    assert stopped["other_market_untouched"] is True
    assert coordinator.status("NSE")["gate"]["kill_engaged"] is True
    assert coordinator.status("NASDAQ")["gate"]["state"] == GateState.ACTIVE.value
    assert coordinator.status("NASDAQ")["gate"]["kill_engaged"] is False

    # The other market keeps trading while NSE is halted.
    us2 = coordinator.submit_decision(
        "NASDAQ",
        _decision(
            symbol="AAPL",
            reference_price=220.0,
            stop_loss=210.0,
            take_profit=240.0,
            selected_qty=5.0,
            bar_timestamp="2026-10-29T05:45:00+00:00",
        ),
        session_open=True,
    )
    assert us2["status"] == "SUBMITTED"
    assert len(nasdaq.submit_calls) == 2
    assert nse.submit_calls == []
    coordinator.close()


def test_live_mode_disables_paper_simulation_for_that_market_only():
    engine = LiveMarketPaperEngine(
        ":memory:", execution_mode_provider=lambda m: "LIVE" if m == "NSE" else "PAPER"
    )
    tick = engine.sync_live_tick("NSE")
    assert tick["paper_simulator_skipped"] is True
    assert tick["broker_order_api_calls"] == 0
    assert "LIVE" in tick["skip_reason"]

    runner = MultiMarketPaperRunner(
        engine, paper_blocked=lambda m: m == "NSE", enabled=True
    )
    out = asyncio.run(runner.run_once())
    assert out["markets"]["NSE"]["status"] == "SKIPPED_LIVE_ROUTED"
    asyncio.run(runner.stop())

    # An operator-triggered paper cycle (explicit markets) also refuses to simulate NSE.
    cycle = engine.advance_selected_markets_concurrently(markets=["NSE", "NASDAQ"], only_active=False)
    assert cycle["markets"]["NSE"]["status"] == "SKIPPED_LIVE_ROUTED"
    assert cycle["markets"]["NSE"]["broker_order_api_calls"] == 0
    assert cycle["broker_order_api_calls"] == 0
    assert cycle["markets"]["NASDAQ"]["status"] != "SKIPPED_LIVE_ROUTED"
    engine.close()


# ---------------------------------------------------------------------------
# Concurrent live runner isolation
# ---------------------------------------------------------------------------


def test_concurrent_live_runner_isolates_slow_market_from_fast_market():
    nse = FakeAdapter("NSE", "INR", clock=lambda: NOW)
    nasdaq = FakeAdapter("NASDAQ", "USD", clock=lambda: NOW, price=220.0)
    coordinator = _coordinator(nse=nse, nasdaq=nasdaq)
    _armed_active(coordinator, "NSE")
    _armed_active(coordinator, "NASDAQ")
    nse.submit_delay = 2.0  # slow broker on NSE

    ensemble = StubEnsemble(
        {
            "NSE": [_decision()],
            "NASDAQ": [
                _decision(
                    symbol="AAPL",
                    reference_price=220.0,
                    stop_loss=210.0,
                    take_profit=240.0,
                    selected_qty=5.0,
                )
            ],
        }
    )
    engine = StubEngine({"NSE": ["RELIANCE"], "NASDAQ": ["AAPL"]})
    coordinator.paper_engine = engine
    runner = MultiMarketLiveRunner(
        coordinator, ensemble, interval_seconds=30.0, market_timeout_seconds=0.4
    )
    started = datetime.now(timezone.utc)
    out = asyncio.run(runner.run_once())
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    assert out["markets"]["NASDAQ"]["status"] == "EXECUTED"
    assert out["markets"]["NSE"]["status"] == "TIMEOUT"
    assert out["markets"]["NSE"]["broker_calls_unknown"] is True
    assert elapsed < 1.5
    assert len(nasdaq.submit_calls) == 1

    # A timed-out market may still have a broker write in flight: it must latch as
    # unreconciled and accept no further orders until an operator reconciles.
    deadline = time.time() + 3.0
    while not nse.submit_calls and time.time() < deadline:
        time.sleep(0.05)
    assert len(nse.submit_calls) == 1
    time.sleep(0.1)
    latched = coordinator.status("NSE")
    assert latched["blocked"] == "CYCLE_TIMEOUT_RECONCILE_REQUIRED"
    assert coordinator.advance_market(
        "NSE", ensemble, symbols=["RELIANCE"]
    )["status"] == "SKIPPED_BLOCKED"
    reconciled = coordinator.reconcile("NSE")
    assert reconciled["ok"] is True
    assert coordinator.status("NSE")["blocked"] is None
    asyncio.run(runner.stop())
    coordinator.close()


# ---------------------------------------------------------------------------
# Adapter contracts (NSE protected GTT, NASDAQ broker service)
# ---------------------------------------------------------------------------


def _nse_catalog(tmp_path: Path) -> InstrumentCatalog:
    catalog = InstrumentCatalog(AutonomySettings(), tmp_path / "nse.json")
    fixture = Path(__file__).resolve().parent / "fixtures" / "upstox_bod_sanitized_master.json"
    catalog._ingest_records(
        json.loads(fixture.read_text(encoding="utf-8"))["complete"],
        digest="fixture",
        suspended_keys=frozenset(),
        mis_keys=frozenset(),
        source_kind="authoritative_upstox_bod",
    )
    return catalog


class FakeNseBroker:
    def __init__(self):
        self.placed: list = []
        self.gtt: list[dict] = []

    def account(self):
        return AccountSnapshot(1_000_000.0, 1_000_000.0, 0.0, 0.0, (), NOW, True)

    def _positions(self):
        return ()

    def place_protected_entry(self, order, permit, *, session_id, instrument):
        permit.validate(session_id, NOW)
        self.placed.append(order)
        return Submission(("GTT-1",), "ACKNOWLEDGED_UNRECONCILED")

    def list_gtt(self):
        return list(self.gtt)

    def get_gtt(self, identifier):
        return None


def test_nse_adapter_places_tick_aligned_protected_gtt_and_refuses_naked_exits(tmp_path: Path):
    ist = timezone(timedelta(hours=5, minutes=30))
    trading_day = datetime.now(timezone.utc).astimezone(ist).date().isoformat()
    broker = FakeNseBroker()
    adapter = NseLiveAdapter(
        _nse_catalog(tmp_path),
        live_token_provider=lambda: "live-token",
        algo_name="AXIOM",
        live_enabled=True,
        expected_public_ip="203.0.113.9",
        quote_provider=lambda key: {
            "bid": 1399.90,
            "ask": 1400.10,
            "last": 1400.0,
            "timestamp": NOW.isoformat(),
            "source_timestamp": NOW.isoformat(),
        },
        broker_factory=lambda provider: broker,
        session_provider=lambda now: {"is_open": True, "trading_day": trading_day},
    )
    coordinator = LiveExecutionCoordinator(
        journal_path=None,
        adapters={"NSE": adapter, "NASDAQ": FakeAdapter("NASDAQ", "USD", clock=lambda: NOW)},
        limits={"NSE": _limits(), "NASDAQ": _limits()},
        clock=lambda: NOW,
    )
    coordinator.set_mode("NSE", MODE_LIVE)
    assert coordinator.readiness("NSE")["ok"] is True
    coordinator.arm("NSE")
    coordinator.activate(
        "NSE", confirm_phrase="ACTIVATE LIVE REAL-MONEY TRADING", acknowledge_risk=True
    )
    result = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert result["status"] == "SUBMITTED", result
    assert len(broker.placed) == 1
    order = broker.placed[0]
    assert order.instrument_key.startswith("NSE_EQ|")
    assert order.quantity == 10
    assert order.stop_price < order.limit_price < order.target_price
    assert abs(round(order.limit_price / 0.05) - order.limit_price / 0.05) < 1e-6

    sell = coordinator.submit_decision(
        "NSE", _decision(final_action="SELL"), session_open=True
    )
    assert sell["status"] == "REJECTED_BROKER"
    assert len(broker.placed) == 1
    coordinator.close()


class FakeAlpacaService:
    environment = "live"
    limits = BrokerRiskLimits(
        max_order_value=25_000.0,
        max_open_positions=10,
        max_daily_orders=50,
    )

    def __init__(self):
        self.gate = BrokerLiveGate()
        self.account = {
            "currency": "USD",
            "equity": "100000",
            "last_equity": "100000",
            "buying_power": "100000",
            "cash": "100000",
            "trading_blocked": False,
        }
        self.positions: dict = {}
        self.orders: dict = {}
        self.submissions: list[dict] = []
        self.fail_protection = False

    def readiness_checks(self, *, symbol=None):
        return {"account_verified": True, "broker_enabled": True}

    def activate_live_confirmed(self, *, confirm_live_trading, acknowledge_risk):
        assert confirm_live_trading == "ACTIVATE LIVE REAL-MONEY TRADING"
        assert acknowledge_risk is True
        self.gate.arm({"ready": True})
        permit = self.gate.activate()
        return {"active": True, "permit_issued_at": permit.issued_at}

    def submit_order(self, **fields):
        self.submissions.append(dict(fields))
        if fields.get("order_type") == "stop" and self.fail_protection:
            return {"ok": False, "rejected": "broker", "detail": "stop rejected by broker"}
        return {"ok": True, "duplicate": False, "order": {"broker_order_id": f"b{len(self.submissions)}", "status": "accepted"}}

    def recover(self):
        return {"ok": True, "resolved_intents": 0, "adopted_orders": 0, "positions": 0}

    def cancel_order(self, client_order_id):
        return {"ok": True}

    def disarm(self, reason="disarm"):
        return self.gate.disarm(reason)


def test_nasdaq_adapter_submits_bounded_limit_plus_protective_stop():
    svc = FakeAlpacaService()
    adapter = NasdaqLiveAdapter(
        lambda: svc,
        quote_provider=lambda symbol: {
            "symbol": symbol,
            "bid": 219.90,
            "ask": 220.10,
            "last": 220.0,
            "timestamp": NOW.isoformat(),
            "source_timestamp": NOW.isoformat(),
        },
        calendar=None,
    )
    adapter._calendar = type("Cal", (), {"is_open": lambda self, now: True})()
    coordinator = LiveExecutionCoordinator(
        journal_path=None,
        adapters={"NASDAQ": adapter, "NSE": FakeAdapter("NSE", "INR", clock=lambda: NOW)},
        limits={"NASDAQ": _limits(max_order_value=25_000.0), "NSE": _limits()},
        clock=lambda: NOW,
    )
    coordinator.set_mode("NASDAQ", MODE_LIVE)
    assert coordinator.readiness("NASDAQ")["ok"] is True
    coordinator.arm("NASDAQ")
    coordinator.activate(
        "NASDAQ", confirm_phrase="ACTIVATE LIVE REAL-MONEY TRADING", acknowledge_risk=True
    )
    result = coordinator.submit_decision(
        "NASDAQ",
        _decision(
            symbol="AAPL",
            reference_price=220.0,
            stop_loss=210.0,
            take_profit=240.0,
            selected_qty=5.0,
        ),
        session_open=True,
    )
    assert result["status"] == "SUBMITTED", result
    assert len(svc.submissions) == 2
    entry, protection = svc.submissions
    assert entry["order_type"] == "limit" and entry["limit_price"] == 220.0
    assert protection["order_type"] == "stop" and protection["stop_price"] == 210.0
    assert result["protection_ok"] is True

    svc.fail_protection = True
    blocked = coordinator.submit_decision(
        "NASDAQ",
        _decision(
            symbol="MSFT",
            reference_price=220.0,
            stop_loss=210.0,
            take_profit=240.0,
            selected_qty=5.0,
            bar_timestamp="2026-10-29T05:50:00+00:00",
        ),
        session_open=True,
    )
    assert blocked["status"] == "SUBMITTED"
    assert blocked["protection_ok"] is False
    assert coordinator.status("NASDAQ")["blocked"] == "PROTECTION_FAILED"
    assert coordinator.status("NASDAQ")["gate"]["state"] == GateState.DISABLED.value
    coordinator.close()


class _OpenUsSession:
    """Calendar stub: the regular US session is open at NOW."""

    def is_open(self, now: datetime) -> bool:
        return True


class _LiveAlpacaHTTP:
    """Minimal Alpaca LIVE transport (mock transport, no network) that records order POSTs."""

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.by_client: dict[str, str] = {}
        self.posts: list[dict] = []
        self.requests: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url.host) == LIVE_HOST, f"unexpected host {request.url.host}"
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        if (method, path) == ("GET", "/v2/account"):
            return httpx.Response(200, json={
                "id": "acct-live-1", "account_number": "PA00000001", "status": "ACTIVE",
                "currency": "USD", "cash": "100000", "buying_power": "100000",
                "equity": "100000", "last_equity": "100000", "portfolio_value": "100000",
                "trading_blocked": False, "account_blocked": False, "trade_suspended_by_user": False,
            })
        if (method, path) == ("GET", "/v2/clock"):
            return httpx.Response(200, json={"timestamp": NOW.isoformat(), "is_open": True})
        if (method, path) == ("GET", "/v2/positions"):
            if not self.orders:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{
                "symbol": "AAPL", "qty": "5", "side": "long", "market_value": "1100",
                "avg_entry_price": "220", "current_price": "220",
            }])
        if (method, path) == ("GET", "/v2/orders"):
            return httpx.Response(200, json=[
                o for o in self.orders.values()
                if o["status"] in {"accepted", "new", "partially_filled"}
            ])
        if (method, path) == ("GET", "/v2/orders:by_client_order_id"):
            cid = request.url.params.get("client_order_id", "")
            oid = self.by_client.get(cid)
            if oid is None:
                return httpx.Response(404, json={"message": "order not found"})
            return httpx.Response(200, json=self.orders[oid])
        if method == "GET" and path.startswith("/v2/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=order) if order else httpx.Response(404, json={})
        if (method, path) == ("POST", "/v2/orders"):
            body = json.loads(request.content.decode("utf-8"))
            self.posts.append(body)
            oid = f"brk-{len(self.posts)}"
            order = {
                "id": oid, "client_order_id": body["client_order_id"], "symbol": body["symbol"],
                "side": body["side"], "qty": body.get("qty"), "type": body["type"],
                "time_in_force": body["time_in_force"], "limit_price": body.get("limit_price"),
                "stop_price": body.get("stop_price"), "status": "accepted",
                "filled_qty": "0", "filled_avg_price": None,
                "submitted_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
            }
            self.orders[oid] = order
            self.by_client[body["client_order_id"]] = oid
            return httpx.Response(200, json=order)
        raise AssertionError(f"unexpected Alpaca LIVE request {method} {path}")


def _fresh_us_quote(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "bid": 219.90,
        "ask": 220.10,
        "last": 220.0,
        "timestamp": NOW.isoformat(),
        "source_timestamp": NOW.isoformat(),
        "is_delayed": False,
        "feed": "iex",
    }


def test_nasdaq_adapter_drives_the_real_gated_alpaca_service_without_network(tmp_path: Path):
    transport = _LiveAlpacaHTTP()
    service = AlpacaBrokerService(
        None,
        environment="live",
        limits=BrokerRiskLimits(max_order_value=25_000.0, max_open_positions=10, max_daily_orders=50),
        read_only_client=AlpacaReadOnlyClient(
            "live-key", "live-secret", environment="live", client=httpx.Client(transport=httpx.MockTransport(transport.handler))
        ),
        client_factory=lambda permit: AlpacaTradingClient(
            "live-key", "live-secret", environment="live", live_permit=permit,
            client=httpx.Client(transport=httpx.MockTransport(transport.handler)),
        ),
        quote_provider=_fresh_us_quote,
        calendar=_OpenUsSession(),
        clock=lambda: NOW,
    )
    adapter = NasdaqLiveAdapter(
        lambda: service,
        quote_provider=_fresh_us_quote,
        calendar=_OpenUsSession(),
        clock=lambda: NOW,
    )
    coordinator = LiveExecutionCoordinator(
        journal_path=tmp_path / "nasdaq_live.sqlite3",
        adapters={"NASDAQ": adapter, "NSE": FakeAdapter("NSE", "INR", clock=lambda: NOW)},
        limits={"NASDAQ": _limits(max_order_value=25_000.0), "NSE": _limits()},
        clock=lambda: NOW,
    )
    coordinator.set_mode("NASDAQ", MODE_LIVE)
    report = coordinator.readiness("NASDAQ")
    assert report["ok"] is True, report["failed"]
    coordinator.arm("NASDAQ")
    coordinator.activate(
        "NASDAQ", confirm_phrase="ACTIVATE LIVE REAL-MONEY TRADING", acknowledge_risk=True
    )
    assert transport.posts == [], "readiness/arm/activate must never submit an order"

    result = coordinator.submit_decision(
        "NASDAQ",
        _decision(
            symbol="AAPL", reference_price=220.0, stop_loss=210.0, take_profit=240.0,
            selected_qty=5.0, product="CASH",
        ),
        session_open=True,
    )
    assert result["status"] == "SUBMITTED", result
    assert result["protection_ok"] is True
    assert len(transport.posts) == 2
    entry, stop = transport.posts
    assert entry["type"] == "limit" and entry["limit_price"] == "220.0"
    assert entry["client_order_id"].startswith("live-nasdaq-aapl-")
    assert stop["type"] == "stop" and stop["stop_price"] == "210.0"
    assert stop["client_order_id"] == f"{entry['client_order_id']}-sl"
    assert stop["side"] == "sell"
    assert coordinator.status("NASDAQ")["gate"]["kill_engaged"] is False
    coordinator.close()
    service.disarm("test complete")


# ---------------------------------------------------------------------------
# Secrets never reach the live journal/audit
# ---------------------------------------------------------------------------


def test_live_audit_redacts_secrets_and_labels_real_money():
    coordinator = _coordinator()
    coordinator.journal.audit(
        "NSE",
        "LIVE_ORDER_REJECTED",
        {
            "symbol": "RELIANCE",
            "authorization": "Bearer super-secret-token-value",
            "api_key": "AKIA-SECRET-123456",
            "note": "access_token=leaked-token-value",
        },
    )
    events = coordinator.journal.audit_log(market="NSE")
    blob = json.dumps(events)
    assert "super-secret-token-value" not in blob
    assert "AKIA-SECRET-123456" not in blob
    assert "leaked-token-value" not in blob
    assert all(e["payload"]["real_money"] is True for e in events)
    coordinator.close()


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def test_live_api_defaults_are_disarmed_and_paper_routing_is_guarded(tmp_path: Path):
    settings = replace(Settings(), api_token="live-tok", database_url=f"sqlite:///{tmp_path}/live.db")
    headers = {"Authorization": "Bearer live-tok"}
    with TestClient(create_app(settings, data_dir=tmp_path)) as web:
        status = web.get("/api/markets/live/status", headers=headers)
        assert status.status_code == 200
        payload = status.json()
        assert payload["default_live_execution"] == "OFF"
        assert payload["execution"]["real_money_enabled"] is False
        assert payload["execution"]["markets"]["NSE"]["gate"]["state"] == "DISABLED"
        assert payload["execution"]["markets"]["NASDAQ"]["gate"]["state"] == "DISABLED"

        overview = web.get("/api/markets/overview", headers=headers).json()
        assert overview["live"]["default_live_execution"] == "OFF"
        assert overview["execution_modes"] == {"NSE": "PAPER", "NASDAQ": "PAPER"}
        assert overview["live_execution"]["markets"]["NSE"]["mode"] == "PAPER"

        readiness = web.get("/api/markets/live/NSE/readiness", headers=headers)
        assert readiness.status_code == 200
        assert readiness.json()["ok"] is False
        assert "risk_limits_configured" in readiness.json()["failed"]

        arm = web.post("/api/markets/live/NSE/arm", headers=headers)
        assert arm.status_code == 409

        activate = web.post(
            "/api/markets/live/NSE/activate",
            headers=headers,
            json={"confirm_live_trading": "ACTIVATE LIVE REAL-MONEY TRADING", "acknowledge_risk": True},
        )
        assert activate.status_code == 409

        mode = web.post("/api/markets/live/NSE/mode", headers=headers, json={"mode": "LIVE"})
        assert mode.status_code == 200
        assert mode.json()["mode"] == "LIVE"
        assert mode.json()["gate"]["state"] == "DISABLED"

        # LIVE routing refuses local paper simulation for that market (no silent conversion).
        blocked = web.post(
            "/api/markets/paper/orders",
            headers=headers,
            json={"market": "NSE", "symbol": "RELIANCE", "side": "BUY", "qty": 1},
        )
        assert blocked.status_code == 409

        # The other market is untouched and remains paper-routed.
        other = web.get("/api/markets/overview", headers=headers).json()
        assert other["execution_modes"]["NASDAQ"] == "PAPER"

        estop = web.post(
            "/api/markets/live/NSE/emergency-stop", headers=headers, json={"reason": "test halt"}
        )
        assert estop.status_code == 200
        assert estop.json()["market"] == "NSE"
        assert estop.json()["gate"]["state"] == "KILL_SWITCH"
        nasdaq_after = web.get("/api/markets/live/status", headers=headers).json()
        assert nasdaq_after["execution"]["markets"]["NASDAQ"]["gate"]["state"] == "DISABLED"

        web.post("/api/markets/live/NSE/mode", headers=headers, json={"mode": "PAPER"})
        audit = web.get("/api/markets/live/audit", headers=headers).json()
        assert audit["real_money"] is True
        assert any(e["event_kind"] == "LIVE_EMERGENCY_STOP" for e in audit["events"])

        # Limits are all-or-nothing: a partial set must never be accepted (fail closed).
        partial = web.post(
            "/api/markets/live/NASDAQ/limits", headers=headers, json={"max_order_value": 1000.0}
        )
        assert partial.status_code == 422
        assert web.get("/api/markets/live/status", headers=headers).json()[
            "execution"
        ]["markets"]["NASDAQ"]["risk_limits"]["configured"] is False
        full = web.post(
            "/api/markets/live/NASDAQ/limits",
            headers=headers,
            json={
                "max_order_value": 25_000.0,
                "max_daily_orders": 50,
                "max_daily_loss": 2_500.0,
                "max_open_positions": 10,
                "max_symbol_exposure": 25_000.0,
                "max_total_exposure": 100_000.0,
                "max_quote_age_seconds": 60.0,
                "max_price_deviation_bps": 150.0,
            },
        )
        assert full.status_code == 200
        assert full.json()["risk_limits"]["configured"] is True
        assert full.json()["gate"]["state"] == "DISABLED"
        assert full.json()["market"] == "NASDAQ"

        # Live endpoints require the local API token like every other market endpoint.
        assert web.get("/api/markets/live/status").status_code in {401, 403}
