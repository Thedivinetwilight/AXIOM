"""Adversarial coverage inside the existing LIVE execution boundary.

Everything here uses deterministic fake adapters — no broker endpoint, no
credential and no order. The suite covers the outcomes an operator cannot test
by hand: rate limits, ambiguous submissions, partial fills, every risk limit,
reconciliation and the kill switch. It also pins the default posture: an
unconfigured coordinator cannot reach a broker submit at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.markets.live_execution import (
    GateState,
    LiveBrokerRejected,
    LiveExecutionCoordinator,
    LiveGateRefused,
    LiveMarketAdapter,
    LiveRateLimited,
    LiveRiskLimits,
    LiveSubmissionUncertain,
    MarketLiveGate,
    MODE_LIVE,
    MODE_PAPER,
    RATE_LIMIT_COOLDOWN_SECONDS,
)

NOW = datetime(2026, 10, 29, 6, 0, tzinfo=timezone.utc)  # 11:30 IST, NSE open


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
    """Controllable broker: real gate semantics, scripted submit outcomes."""

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
        self.submit_result: dict | None = None
        self.pending_dependencies: list[str] = []

    # -- gate / reads ---------------------------------------------------------
    def missing_dependencies(self):
        return list(self.pending_dependencies)

    def readiness_checks(self, *, symbol=None):
        return {"fake_ready": True}

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

    def account(self):
        return {
            "market": self.market,
            "currency": self.currency,
            "equity": self.equity,
            "buying_power": self.equity,
            "day_pnl": self.day_pnl,
            "trading_blocked": False,
        }

    def positions(self):
        return list(self.positions_data)

    def quote(self, symbol):
        return {
            "symbol": symbol,
            "bid": self.price - 0.10,
            "ask": self.price + 0.10,
            "last": self.price,
            "timestamp": self.quote_timestamp.isoformat(),
            "source_timestamp": self.quote_timestamp.isoformat(),
            "is_delayed": self.quote_delayed,
        }

    def market_open(self, now):
        return self.open

    # -- writes ---------------------------------------------------------------
    def submit(self, *, client_order_id, symbol, side, qty, reference_price,
               stop_loss, take_profit, product, instrument_meta):
        self.submit_calls.append(
            {"client_order_id": client_order_id, "symbol": symbol, "side": side, "qty": qty}
        )
        if self.submit_error is not None:
            raise self.submit_error
        if self.submit_result is not None:
            return dict(self.submit_result, client_order_id=client_order_id)
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

    def reconcile(self, unresolved):
        return {"ok": True, "resolved_intents": len(unresolved), "adopted_orders": 0}

    def cancel(self, client_order_id):
        self.cancel_calls.append(client_order_id)
        return {"ok": True}

    def working_orders(self):
        return [
            {"client_order_id": cid, "status": "PARTIALLY_FILLED"}
            for cid in self.cancel_calls
        ]


def _coordinator(tmp_path: Path | None = None, *, clock=None, limits=None):
    clock = clock or (lambda: NOW)
    return LiveExecutionCoordinator(
        journal_path=(tmp_path / "live.sqlite3") if tmp_path else None,
        adapters={
            "NSE": FakeAdapter("NSE", "INR", clock=clock),
            "NASDAQ": FakeAdapter("NASDAQ", "USD", clock=clock, price=220.0),
        },
        limits=limits or {"NSE": _limits(), "NASDAQ": _limits(max_order_value=25_000.0)},
        clock=clock,
    )


def _armed_active(coordinator: LiveExecutionCoordinator, market: str = "NSE") -> FakeAdapter:
    coordinator.set_mode(market, MODE_LIVE)
    coordinator.arm(market)
    coordinator.activate(
        market,
        confirm_phrase="ACTIVATE LIVE REAL-MONEY TRADING",
        acknowledge_risk=True,
    )
    return coordinator.adapters[market]


# --------------------------------------------------------------------------- #
# Default posture: nothing can reach the broker
# --------------------------------------------------------------------------- #
def test_unconfigured_coordinator_never_reaches_the_broker():
    coordinator = _coordinator()
    adapter = coordinator.adapters["NSE"]
    assert adapter.gate_status()["state"] == GateState.DISABLED.value
    result = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert result["status"] == "SKIPPED_PAPER_MODE"
    assert result["broker_order_api_calls"] == 0
    assert adapter.submit_calls == []

    # Even with mode LIVE, an unconfigured gate refuses.
    coordinator.set_mode("NSE", MODE_LIVE)
    result = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert result["status"] == "SKIPPED_GATE_NOT_ACTIVE"
    assert adapter.submit_calls == []
    coordinator.close()


def test_unconfigured_limits_block_arming_and_submission():
    coordinator = _coordinator(limits={"NSE": LiveRiskLimits(), "NASDAQ": LiveRiskLimits()})
    coordinator.set_mode("NSE", MODE_LIVE)
    assert coordinator.readiness("NSE")["failed"].count("risk_limits_configured") == 1
    with pytest.raises(LiveGateRefused):
        coordinator.arm("NSE")
    assert coordinator.adapters["NSE"].submit_calls == []
    coordinator.close()


# --------------------------------------------------------------------------- #
# Rate limits
# --------------------------------------------------------------------------- #
def test_rate_limited_submission_is_not_an_unknown_outcome(tmp_path):
    coordinator = _coordinator(tmp_path)
    adapter = _armed_active(coordinator)
    adapter.submit_error = LiveRateLimited("broker answered 429; nothing was placed")

    result = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert result["status"] == "RATE_LIMITED"
    assert result["broker_order_api_calls"] == 1
    assert len(adapter.submit_calls) == 1

    state = coordinator.rate_limit_state("NSE")["NSE"]
    assert state["cooling_down"] is True
    assert state["rate_limited_total"] == 1
    assert state["remaining_seconds"] <= RATE_LIMIT_COOLDOWN_SECONDS

    # A rate limit is not an ambiguity: no unresolved intent, gate stays ACTIVE.
    assert coordinator.status("NSE")["unresolved_intents"] == []
    assert coordinator.status("NSE")["gate"]["state"] == GateState.ACTIVE.value
    assert coordinator.adapters["NSE"].gate_status()["live_available"] is True

    events = [e["event_kind"] for e in coordinator.journal.audit_log(market="NSE")]
    assert "LIVE_ORDER_RATE_LIMITED" in events
    coordinator.close()


def test_cooldown_blocks_new_orders_without_another_broker_call(tmp_path):
    coordinator = _coordinator(tmp_path)
    adapter = _armed_active(coordinator)
    adapter.submit_error = LiveRateLimited("429")
    assert coordinator.submit_decision("NSE", _decision(), session_open=True)["status"] == "RATE_LIMITED"
    calls_after_limit = len(adapter.submit_calls)

    # Different bar => different client_order_id, so this is not duplicate suppression.
    blocked = coordinator.submit_decision(
        "NSE", _decision(bar_timestamp="2026-10-29T05:35:00+00:00"), session_open=True
    )
    assert blocked["status"] == "RATE_LIMITED"
    assert blocked["broker_order_api_calls"] == 0
    assert "cooldown" in " ".join(blocked["reasons"]).lower()
    assert len(adapter.submit_calls) == calls_after_limit
    coordinator.close()


def test_cooldown_expires_and_the_next_bar_may_submit(tmp_path):
    clock = {"now": NOW}
    coordinator = _coordinator(tmp_path, clock=lambda: clock["now"])
    adapter = _armed_active(coordinator)
    adapter.submit_error = LiveRateLimited("429")
    assert coordinator.submit_decision("NSE", _decision(), session_open=True)["status"] == "RATE_LIMITED"

    adapter.submit_error = None
    clock["now"] = NOW + timedelta(seconds=RATE_LIMIT_COOLDOWN_SECONDS + 1)
    adapter.quote_timestamp = clock["now"]
    result = coordinator.submit_decision(
        "NSE", _decision(bar_timestamp="2026-10-29T05:40:00+00:00"), session_open=True
    )
    assert result["status"] == "SUBMITTED"
    assert coordinator.rate_limit_state("NSE")["NSE"]["cooling_down"] is False
    coordinator.close()


def test_rate_limit_on_one_market_does_not_block_the_other(tmp_path):
    coordinator = _coordinator(tmp_path)
    nse = _armed_active(coordinator, "NSE")
    nasdaq = _armed_active(coordinator, "NASDAQ")
    nse.submit_error = LiveRateLimited("429")
    assert coordinator.submit_decision("NSE", _decision(), session_open=True)["status"] == "RATE_LIMITED"

    result = coordinator.submit_decision(
        "NASDAQ",
        _decision(market="NASDAQ", symbol="AAPL", reference_price=220.0,
                  stop_loss=210.0, take_profit=235.0, selected_qty=5.0,
                  bar_timestamp="2026-10-29T13:30:00+00:00"),
        session_open=True,
    )
    assert result["status"] == "SUBMITTED"
    assert len(nasdaq.submit_calls) == 1
    coordinator.close()


# --------------------------------------------------------------------------- #
# Ambiguous submissions and reconciliation
# --------------------------------------------------------------------------- #
def test_ambiguous_submission_latches_and_requires_reconciliation(tmp_path):
    coordinator = _coordinator(tmp_path)
    adapter = _armed_active(coordinator)
    adapter.submit_error = LiveSubmissionUncertain("transport timeout after write")

    result = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert result["status"] == "SUBMISSION_UNCERTAIN"
    assert result["reconcile_required"] is True
    status = coordinator.status("NSE")
    assert status["blocked"] == "UNRESOLVED_ORDER_INTENT"
    assert status["unresolved_intents"]  # the intent is journaled, not forgotten
    assert status["gate"]["live_available"] is False  # ambiguous outcome disarms the market

    before = len(adapter.submit_calls)
    blocked = coordinator.submit_decision(
        "NSE", _decision(bar_timestamp="2026-10-29T05:50:00+00:00"), session_open=True
    )
    # The ambiguous outcome disarmed the market, so the refusal names the gate;
    # either way nothing reached the broker and the latch stays until reconcile.
    assert blocked["status"] in {"SKIPPED_BLOCKED", "SKIPPED_GATE_NOT_ACTIVE"}
    assert blocked["broker_order_api_calls"] == 0
    assert len(adapter.submit_calls) == before
    coordinator.reconcile("NSE")
    assert coordinator.status("NSE")["blocked"] is None
    coordinator.close()


def test_reconciliation_clears_the_latch_and_the_gate_must_be_rearmed(tmp_path):
    coordinator = _coordinator(tmp_path)
    adapter = _armed_active(coordinator)
    adapter.submit_error = LiveSubmissionUncertain("timeout")
    coordinator.submit_decision("NSE", _decision(), session_open=True)

    outcome = coordinator.reconcile("NSE")
    assert outcome["ok"] is True
    status = coordinator.status("NSE")
    assert status["blocked"] is None
    assert status["unresolved_intents"] == []
    # Clearing the latch must not silently restore live authority.
    assert status["gate"]["state"] != GateState.ACTIVE.value
    assert coordinator.submit_decision("NSE", _decision(), session_open=True)["status"] in {
        "SKIPPED_GATE_NOT_ACTIVE",
    }
    coordinator.close()


# --------------------------------------------------------------------------- #
# Partial fills
# --------------------------------------------------------------------------- #
def test_partial_fill_is_journaled_and_remains_working(tmp_path):
    coordinator = _coordinator(tmp_path)
    adapter = _armed_active(coordinator)
    adapter.submit_result = {
        "broker_order_id": "fake-partial-1",
        "symbol": "RELIANCE",
        "side": "BUY",
        "qty": 10.0,
        "status": "PARTIALLY_FILLED",
        "filled_qty": 4.0,
        "filled_avg_price": 1400.5,
        "real_money": True,
        "protection": {"ok": True},
    }
    result = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert result["status"] == "SUBMITTED"
    order = result["order"]
    assert order["status"] == "PARTIALLY_FILLED"
    assert order["filled_qty"] == 4.0

    journaled = coordinator.journal.journaled_orders("NSE")
    assert any(row.get("status") == "PARTIALLY_FILLED" for row in journaled)

    # A partial fill is not a completed order: the kill switch can still cancel it.
    stop = coordinator.emergency_stop("NSE", reason="partial fill drill", cancel_working=True)
    assert stop["canceled_orders"]  # the working order was canceled
    assert adapter.cancel_calls
    assert stop["other_market_untouched"] is True
    coordinator.close()


def test_partial_fill_does_not_allow_a_duplicate_resubmit_for_the_same_bar(tmp_path):
    coordinator = _coordinator(tmp_path)
    adapter = _armed_active(coordinator)
    adapter.submit_result = {
        "broker_order_id": "fake-partial-2",
        "symbol": "RELIANCE",
        "side": "BUY",
        "qty": 10.0,
        "status": "PARTIALLY_FILLED",
        "real_money": True,
        "protection": {"ok": True},
    }
    assert coordinator.submit_decision("NSE", _decision(), session_open=True)["status"] == "SUBMITTED"
    calls = len(adapter.submit_calls)
    duplicate = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert duplicate["status"] == "DUPLICATE_SUPPRESSED"
    assert duplicate["broker_order_api_calls"] == 0
    assert len(adapter.submit_calls) == calls
    coordinator.close()


# --------------------------------------------------------------------------- #
# Risk limits: every one refuses without touching the broker
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "limit_overrides, decision_overrides, expected_fragment",
    [
        ({"max_order_value": 1_000.0}, {}, "order value"),
        ({"max_symbol_exposure": 1_000.0}, {}, "exposure"),
        ({"max_total_exposure": 1_000.0}, {}, "exposure"),
        ({"max_price_deviation_bps": 1.0}, {"reference_price": 1500.0}, "deviation"),
    ],
)
def test_each_risk_limit_refuses_before_the_broker(tmp_path, limit_overrides, decision_overrides,
                                                   expected_fragment):
    coordinator = _coordinator(
        tmp_path, limits={"NSE": _limits(**limit_overrides), "NASDAQ": _limits()}
    )
    adapter = _armed_active(coordinator)
    result = coordinator.submit_decision("NSE", _decision(**decision_overrides), session_open=True)
    assert result["status"] == "REJECTED_RISK_GATE"
    assert result["broker_order_api_calls"] == 0
    assert adapter.submit_calls == []
    joined = " ".join(str(reason).lower() for reason in result["reasons"])
    assert expected_fragment in joined or result["reasons"], joined
    coordinator.close()


def test_daily_order_limit_refuses_the_next_order(tmp_path):
    coordinator = _coordinator(tmp_path, limits={"NSE": _limits(max_daily_orders=1), "NASDAQ": _limits()})
    adapter = _armed_active(coordinator)
    assert coordinator.submit_decision("NSE", _decision(), session_open=True)["status"] == "SUBMITTED"
    calls = len(adapter.submit_calls)
    second = coordinator.submit_decision(
        "NSE", _decision(bar_timestamp="2026-10-29T05:55:00+00:00"), session_open=True
    )
    assert second["status"] == "REJECTED_RISK_GATE"
    assert "cap=1" in " ".join(str(r).lower() for r in second["reasons"])
    assert len(adapter.submit_calls) == calls
    coordinator.close()


def test_open_position_limit_refuses_a_new_symbol(tmp_path):
    coordinator = _coordinator(tmp_path, limits={"NSE": _limits(max_open_positions=1), "NASDAQ": _limits()})
    adapter = _armed_active(coordinator)
    adapter.positions_data = [
        {"symbol": "TCS", "quantity": 5, "market_value": 20_000.0},
        {"symbol": "INFY", "quantity": 5, "market_value": 20_000.0},
    ]
    result = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert result["status"] == "REJECTED_RISK_GATE"
    assert adapter.submit_calls == []
    coordinator.close()


def test_stale_and_delayed_quotes_are_refused(tmp_path):
    clock = {"now": NOW}
    coordinator = _coordinator(tmp_path, clock=lambda: clock["now"])
    adapter = _armed_active(coordinator)
    adapter.quote_timestamp = NOW - timedelta(minutes=5)
    stale = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert stale["status"] == "REJECTED_RISK_GATE"
    assert adapter.submit_calls == []

    adapter.quote_timestamp = clock["now"]
    adapter.quote_delayed = True
    delayed = coordinator.submit_decision(
        "NSE", _decision(bar_timestamp="2026-10-29T05:45:00+00:00"), session_open=True
    )
    assert delayed["status"] == "REJECTED_RISK_GATE"
    assert adapter.submit_calls == []
    coordinator.close()


def test_broker_rejection_is_reported_and_leaves_no_unresolved_intent(tmp_path):
    coordinator = _coordinator(tmp_path)
    adapter = _armed_active(coordinator)
    adapter.submit_error = LiveBrokerRejected("exchange refused: price band")
    result = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert result["status"] == "REJECTED_BROKER"
    status = coordinator.status("NSE")
    assert status["unresolved_intents"] == []
    assert status["blocked"] is None
    assert status["gate"]["state"] == GateState.ACTIVE.value
    coordinator.close()


# --------------------------------------------------------------------------- #
# Kill switch
# --------------------------------------------------------------------------- #
def test_kill_switch_blocks_new_orders_and_documents_open_orders(tmp_path):
    from app.markets.live_readiness import open_order_policy

    coordinator = _coordinator(tmp_path)
    adapter = _armed_active(coordinator)
    stop = coordinator.emergency_stop("NSE", reason="operator drill")
    assert stop["gate"]["state"] == GateState.KILL_SWITCH.value

    calls = len(adapter.submit_calls)
    result = coordinator.submit_decision("NSE", _decision(), session_open=True)
    assert result["status"] in {"SKIPPED_GATE_NOT_ACTIVE", "SKIPPED_BLOCKED"}
    assert len(adapter.submit_calls) == calls

    # Reset requires an explicit acknowledgement and returns to DISABLED, not ACTIVE.
    with pytest.raises(LiveGateRefused):
        coordinator.reset_kill_switch("NSE", operator_ack=False)
    reset = coordinator.reset_kill_switch("NSE", operator_ack=True)
    assert reset["gate"]["state"] == GateState.DISABLED.value
    assert coordinator.submit_decision("NSE", _decision(), session_open=True)["status"] == (
        "SKIPPED_GATE_NOT_ACTIVE"
    )

    policy = open_order_policy()
    assert policy["blocks_new_orders"] is True
    assert "cancel_working" in policy["working_orders"]
    assert "partial" in policy["partial_fills"].lower()
    coordinator.close()


def test_kill_switch_on_one_market_leaves_the_other_trading(tmp_path):
    coordinator = _coordinator(tmp_path)
    _armed_active(coordinator, "NSE")
    nasdaq = _armed_active(coordinator, "NASDAQ")
    coordinator.emergency_stop("NSE", reason="drill")
    result = coordinator.submit_decision(
        "NASDAQ",
        _decision(market="NASDAQ", symbol="AAPL", reference_price=220.0,
                  stop_loss=210.0, take_profit=235.0, selected_qty=5.0,
                  bar_timestamp="2026-10-29T13:30:00+00:00"),
        session_open=True,
    )
    assert result["status"] == "SUBMITTED"
    assert len(nasdaq.submit_calls) == 1
    coordinator.close()


def test_restart_keeps_operator_mode_but_never_the_permit(tmp_path):
    coordinator = _coordinator(tmp_path)
    _armed_active(coordinator, "NSE")
    assert coordinator.status("NSE")["gate"]["state"] == GateState.ACTIVE.value
    coordinator.close()

    restarted = _coordinator(tmp_path)
    nse = restarted.status("NSE")
    assert nse["mode"] == MODE_LIVE          # operator intent persists
    assert nse["gate"]["state"] == GateState.DISABLED.value  # the permit never does
    assert restarted.status("NASDAQ")["mode"] == MODE_PAPER
    assert restarted.submit_decision("NSE", _decision(), session_open=True)["status"] == (
        "SKIPPED_GATE_NOT_ACTIVE"
    )
    assert restarted.adapters["NSE"].submit_calls == []
    restarted.close()
