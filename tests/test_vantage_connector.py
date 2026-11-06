from datetime import datetime, timedelta, timezone
import json

import pytest

from app.database.database import Database
from app.vantage.connector import (
    IdentityMismatch, InvalidMarketData, MT5Connector, normalize_contract,
)
from app.vantage.models import (
    AccountSnapshot, Lifecycle, OrderInput, Quote, RiskLimits, SessionMode, mask_login,
)
from app.vantage.risk import ContractViolation, evaluate_entry, normalize_order
from app.vantage.secrets import MemorySecretStore, redact
from app.vantage.service import OrderBlocked, VantageService
from app.vantage.store import VantageStore
from tests.mt5_fake import FakeMT5Gateway, symbol_row


RISK = {
    "max_order_volume": 10.0, "max_symbol_volume": 20.0,
    "max_order_margin": 10_000.0, "max_total_margin": 20_000.0,
    "max_open_positions": 5, "max_daily_loss": 1_000.0,
    "max_drawdown_pct": 20.0, "max_trades_per_day": 10,
    "max_margin_usage_pct": 80.0, "max_spread_bps": 100.0,
    "max_slippage_points": 10, "max_quote_age_seconds": 30,
}


def make_service(fake=None, mode=SessionMode.DEMO, database=None):
    fake = fake or FakeMT5Gateway(live=mode == SessionMode.LIVE)
    database = database or Database("sqlite:///:memory:")
    store = VantageStore(database)
    secrets = MemorySecretStore()
    service = VantageService(store, secrets, lambda: fake, lambda: True)
    credentials = {
        "login": str(fake.login_value), "password": "fake-only-not-a-real-password",
        "server": fake.server, "terminal_path": "", "label": "fake test account",
    }
    service.save_credentials(mode, credentials)
    service.select_mode(mode)
    return service, fake, store, secrets, database


def connect_and_configure(service, symbols=("EURUSD",)):
    service.test_connection()
    service.save_risk_limits(RISK)
    service.select_instruments(list(symbols))


def activate(service):
    readiness = service.readiness()
    assert readiness["ready"], readiness["blockers"]
    armed = service.arm(readiness["readiness_id"], readiness["arm_phrase"])
    assert armed["orders_submitted"] == 0
    result = service.activate(armed["activation_id"], armed["activation_phrase"])
    assert result["status"]["active"] is True


def order(key="order-test-0001", **changes):
    values = dict(
        symbol="EURUSD", side="BUY", order_type="MARKET", volume=0.1,
        idempotency_key=key,
    )
    values.update(changes)
    return OrderInput(**values)


def submit(service, request):
    review = service.review(request)
    assert review["risk"]["allowed"], review["risk"]["issues"]
    return service.submit(request, review["review_id"], review["confirmation_phrase"])


def test_contract_product_labels_and_incomplete_fail_closed():
    share = normalize_contract(symbol_row("AAPL", "CFD\\Shares\\US", "Apple share CFD"), "USD")
    assert share.product_type == "SHARE_CFD"
    assert share.is_cfd is True
    assert share.tradable is True
    unknown = normalize_contract(symbol_row("MYSTERY", "Custom\\Other", "Mystery"), "USD")
    assert unknown.product_type == "UNKNOWN"
    assert unknown.tradable is False
    assert any("product type" in issue for issue in unknown.metadata_issues)
    incomplete = symbol_row()
    incomplete["volume_step"] = 0
    contract = normalize_contract(incomplete, "USD")
    assert contract.metadata_complete is False
    assert any("volume step" in issue for issue in contract.metadata_issues)


def test_mask_and_recursive_redaction_never_expose_secrets():
    assert mask_login("123456789") == "••••89"
    value = {
        "password": "do-not-export", "nested": {"API_TOKEN": "local-token"},
        "message": "accidental do-not-export and local-token", "server": "VantageMarkets-Live 13",
    }
    encoded = json.dumps(redact(value, secrets=("do-not-export", "local-token")))
    assert "do-not-export" not in encoded
    assert "local-token" not in encoded
    assert "VantageMarkets-Live 13" in encoded


def configured_contract():
    return normalize_contract(symbol_row(), "USD")


def quote_now():
    now = datetime.now(timezone.utc)
    return Quote("EURUSD", 1.0998, 1.1002, 1.1, now.isoformat(), int(now.timestamp() * 1000), now.isoformat())


def test_volume_tick_and_stop_rules_are_exact():
    contract = configured_contract()
    quote = quote_now()
    with pytest.raises(ContractViolation, match="whole number"):
        normalize_order(order(volume=0.105), contract, quote)
    with pytest.raises(ContractViolation, match="tick size"):
        normalize_order(order(order_type="LIMIT", limit_price=1.09005), contract, quote)
    with pytest.raises(ContractViolation, match="stop-loss"):
        normalize_order(order(stop_loss=1.1003), contract, quote)
    with pytest.raises(ContractViolation, match="BUY LIMIT"):
        normalize_order(order(order_type="LIMIT", limit_price=1.1003), contract, quote)
    valid = normalize_order(order(order_type="LIMIT", limit_price=1.09, stop_loss=1.08, take_profit=1.11), contract, quote)
    assert valid.volume == 0.1
    assert valid.limit_price == 1.09


def test_stale_quote_and_risk_bounds_block_entries():
    contract = configured_contract()
    old = datetime.now(timezone.utc) - timedelta(seconds=120)
    quote = Quote("EURUSD", 1.0998, 1.1002, 1.1, old.isoformat(), int(old.timestamp() * 1000), old.isoformat())
    account = AccountSnapshot(
        "••••56", "VantageMarkets-Demo", "USD", 10_000, 10_000, 0, 9_000,
        100, True, True, "DEMO", True, True, datetime.now(timezone.utc).isoformat(),
    )
    decision = evaluate_entry(
        order(), contract, quote, account, RiskLimits(**RISK), positions=[], working_orders=[],
        current_margin_exposure=0, estimated_margin=10, trades_today=0,
        day_start_equity=10_000, peak_equity=10_000,
    )
    assert decision["allowed"] is False
    assert any("stale" in issue for issue in decision["issues"])
    unconfigured = evaluate_entry(
        order(), contract, quote_now(), account, RiskLimits(), positions=[], working_orders=[],
        current_margin_exposure=0, estimated_margin=10, trades_today=0,
        day_start_equity=10_000, peak_equity=10_000,
    )
    assert unconfigured["allowed"] is False
    assert len(unconfigured["issues"]) >= 5


def test_out_of_order_and_future_quotes_are_rejected():
    connector = MT5Connector(FakeMT5Gateway())
    connector.connect({"login": "123456", "password": "fake", "server": "VantageMarkets-Demo"}, SessionMode.DEMO)
    connector.quote("EURUSD")
    connector.gateway.out_of_order_tick = True
    with pytest.raises(InvalidMarketData, match="out-of-order"):
        connector.quote("EURUSD")
    connector.gateway.out_of_order_tick = False
    connector.gateway.tick_future_seconds = 60
    with pytest.raises(InvalidMarketData, match="future"):
        connector.quote("EURUSD")


def test_read_only_connection_discovers_account_and_redacts_identity():
    service, fake, store, _, _ = make_service()
    result = service.test_connection()
    assert result["read_only"] is True
    assert result["orders_modified"] is False
    assert result["instruments_discovered"] == 3
    assert result["account"]["login_masked"] == "••••56"
    assert fake.send_calls == []
    public = json.dumps(result) + json.dumps(store.list_events())
    assert "fake-only-not-a-real-password" not in public
    assert '"123456"' not in public
    instruments = service.instruments()["instruments"]
    assert {item["symbol"] for item in instruments} == {"EURUSD", "AAPL", "MYSTERY"}
    apple = next(item for item in instruments if item["symbol"] == "AAPL")
    assert apple["product_type"] == "SHARE_CFD" and apple["is_cfd"] is True
    mystery = next(item for item in instruments if item["symbol"] == "MYSTERY")
    assert mystery["tradable"] is False


def test_demo_live_identity_cannot_be_inferred_from_credentials():
    fake = FakeMT5Gateway(live=False)
    service, _, _, _, _ = make_service(fake=fake, mode=SessionMode.LIVE)
    with pytest.raises(IdentityMismatch, match="real account"):
        service.test_connection()
    assert service.status()["active"] is False
    assert fake.send_calls == []


def test_missing_configuration_readiness_is_fail_closed():
    service, _, _, _, _ = make_service()
    readiness = service.readiness()
    assert readiness["ready"] is False
    assert any("MT5" in blocker or "connection" in blocker.lower() for blocker in readiness["blockers"])
    assert service.lifecycle == Lifecycle.DISARMED


def test_full_fake_demo_market_order_and_duplicate_reconciliation():
    service, fake, _, _, _ = make_service()
    connect_and_configure(service)
    activate(service)
    request = order()
    result = submit(service, request)
    assert result["submission_attempted"] is True
    assert result["order"]["status"] == "FILLED", result
    assert result["order"]["confirmed_ts"]
    assert len(fake.send_calls) == 1
    duplicate = service.submit(request, "expired-review", "wrong")
    assert duplicate["duplicate"] is True
    assert duplicate["submission_attempted"] is False
    assert len(fake.send_calls) == 1
    workspace = service.workspace()
    assert workspace["positions"][0]["volume"] == pytest.approx(0.1)
    assert workspace["deals"][0]["order_id"] == result["order"]["broker_order"]
    assert workspace["simulated"] is True


def test_limit_cancel_only_reports_broker_confirmed_cancel():
    service, fake, _, _, _ = make_service()
    connect_and_configure(service)
    activate(service)
    request = order("limit-order-0001", order_type="LIMIT", limit_price=1.09)
    result = submit(service, request)
    assert result["order"]["status"] == "PENDING"
    cancelled = service.cancel(request.idempotency_key, "CANCEL DEMO ORDER")
    assert cancelled["cancel_confirmed"] is True
    assert cancelled["order"]["status"] == "CANCELLED"
    assert len(fake.cancel_calls) == 1


def test_partial_fill_and_close_use_broker_truth():
    service, fake, _, _, _ = make_service()
    connect_and_configure(service)
    activate(service)
    fake.behavior = "partial"
    request = order("partial-order-001")
    result = submit(service, request)
    assert result["order"]["status"] == "PARTIALLY_FILLED"
    position = service.workspace()["positions"][0]
    fake.behavior = "success"
    close = order(
        "close-position-001", side="SELL", volume=position["volume"],
        position_id=position["position_id"],
    )
    closed = submit(service, close)
    assert closed["order"]["status"] == "FILLED"
    assert service.workspace()["positions"] == []


def test_rejection_is_final_and_not_retried():
    service, fake, _, _, _ = make_service()
    connect_and_configure(service)
    activate(service)
    fake.behavior = "rejection"
    result = submit(service, order("rejected-order-01"))
    assert result["order"]["status"] == "REJECTED"
    assert len(fake.send_calls) == 1


def test_timeout_after_accept_is_unknown_then_strictly_reconciled_not_resent():
    service, fake, store, _, _ = make_service()
    connect_and_configure(service)
    activate(service)
    fake.behavior = "timeout_after_accept"
    request = order("timeout-order-001")
    result = submit(service, request)
    assert result["order"]["status"] == "UNKNOWN"
    assert service.lifecycle == Lifecycle.DISARMED
    assert len(fake.send_calls) == 1
    assert store.unresolved(SessionMode.DEMO.value)
    snapshot = service.reconcile(reason="test reconnect/reconcile")
    resolved = store.get_intent(request.idempotency_key)
    assert resolved["status"] == "FILLED"
    assert resolved["broker_order"]
    assert len(fake.send_calls) == 1
    assert snapshot["positions"]


def test_stale_data_blocks_before_order_send():
    service, fake, _, _, _ = make_service()
    connect_and_configure(service)
    activate(service)
    fake.tick_age_seconds = 120
    service.connector.quotes.clear()  # isolate age validation from the separate out-of-order guard
    review = service.review(order("stale-order-0001"))
    assert review["risk"]["allowed"] is False
    assert any("stale" in issue for issue in review["risk"]["issues"])
    with pytest.raises(OrderBlocked):
        service.submit(order("stale-order-0001"), review["review_id"], review["confirmation_phrase"])
    assert fake.send_calls == []


def test_emergency_stop_is_durable_and_restart_does_not_reactivate():
    service, fake, store, secrets, database = make_service()
    connect_and_configure(service)
    activate(service)
    emergency = service.emergency("test halt")
    assert emergency["status"]["emergency_stop"] is True
    assert service.lifecycle == Lifecycle.STOPPED
    blocked = order("blocked-emergency")
    review = service.review(blocked)
    assert review["risk"]["allowed"] is False
    assert any("emergency" in issue for issue in review["risk"]["issues"])
    with pytest.raises(OrderBlocked):
        service.submit(blocked, review["review_id"], review["confirmation_phrase"])
    assert fake.send_calls == []
    restarted = VantageService(store, secrets, lambda: fake, lambda: True)
    assert restarted.mode == SessionMode.DEMO
    assert restarted.emergency_stop is True
    assert restarted.lifecycle == Lifecycle.DISARMED
    assert restarted.status()["automation_enabled"] is False
    cleared = restarted.clear_emergency("CLEAR EMERGENCY STOP")
    assert cleared["emergency_stop"] is False
    assert cleared["active"] is False and cleared["armed"] is False


def test_mode_instrument_and_risk_changes_invalidate_arm():
    service, _, _, _, _ = make_service()
    connect_and_configure(service)
    readiness = service.readiness()
    armed = service.arm(readiness["readiness_id"], readiness["arm_phrase"])
    assert service.status()["armed"] is True
    service.save_risk_limits({**RISK, "max_order_volume": 9.0})
    assert service.lifecycle == Lifecycle.DISARMED
    connect = service.test_connection()
    assert connect["active"] is False
    service.save_risk_limits(RISK)
    service.select_instruments(["EURUSD"])
    readiness = service.readiness()
    service.arm(readiness["readiness_id"], readiness["arm_phrase"])
    service.select_instruments(["AAPL"])
    assert service.lifecycle == Lifecycle.DISARMED
    service.select_mode(SessionMode.PAPER)
    assert service.status()["mode"] == "AXIOM_PAPER"
    assert service.status()["connected"] is False


def test_external_positions_block_live_readiness():
    service, fake, _, _, _ = make_service()
    connect_and_configure(service)
    fake.positions.append({
        "ticket": 999999, "symbol": "EURUSD", "type": 0, "volume": 1.0,
        "price_open": 1.0, "price_current": 1.1, "profit": 0, "swap": 0,
        "time": 1, "magic": 0, "comment": "manual",
    })
    readiness = service.readiness()
    assert readiness["ready"] is False
    assert any("unexplained" in blocker.lower() for blocker in readiness["blockers"])


def test_mt5_chart_uses_same_broker_source_and_ordered_bars():
    service, _, _, _, _ = make_service()
    connect_and_configure(service)
    chart = service.chart("EURUSD", "1m", 40)
    assert chart["source"] == "VANTAGE_MT5"
    assert chart["data_feed_label"].startswith("VANTAGE_DEMO")
    assert chart["count"] == 40
    times = [bar["timestamp"] for bar in chart["bars"]]
    assert times == sorted(times) and len(times) == len(set(times))


def test_pause_stop_and_activation_phrases_do_not_submit_orders():
    service, fake, _, _, _ = make_service()
    connect_and_configure(service)
    activate(service)
    paused = service.pause()
    assert paused["status"]["paused"] is True
    resumed = service.activate(paused["activation_id"], paused["activation_phrase"])
    assert resumed["status"]["active"] is True
    stopped = service.stop()
    assert stopped["status"]["active"] is False
    assert stopped["status"]["armed"] is False
    assert fake.send_calls == []


def test_disconnected_workspace_is_configuration_view_not_flat_account():
    service, _, _, _, _ = make_service()
    workspace = service.workspace()
    assert workspace["broker_state_available"] is False
    assert workspace["reconciled_at"] is None
    assert workspace["positions"] == [] and workspace["orders"] == []
    assert "do not prove a flat account" in workspace["note"]


def test_position_sltp_modification_is_reviewed_and_broker_confirmed():
    service, fake, _, _, _ = make_service()
    connect_and_configure(service)
    activate(service)
    submit(service, order("modify-source-order"))
    position = service.workspace()["positions"][0]
    key = "modify-position-001"
    review = service.review_position_modification(
        position["position_id"], 1.09, 1.12, key,
    )
    assert review["risk"]["allowed"], review["risk"]["issues"]
    assert review["confirmation_phrase"] == "MODIFY DEMO POSITION"
    result = service.submit_position_modification(
        position["position_id"], 1.09, 1.12, key,
        review["review_id"], review["confirmation_phrase"],
    )
    assert result["order"]["status"] == "MODIFIED", result
    assert result["order"]["confirmed_ts"]
    confirmed = service.workspace()["positions"][0]
    assert confirmed["stop_loss"] == pytest.approx(1.09)
    assert confirmed["take_profit"] == pytest.approx(1.12)
    assert len(fake.send_calls) == 2  # entry + one modification, no retry
    duplicate = service.submit_position_modification(
        position["position_id"], 1.09, 1.12, key, "expired", "wrong",
    )
    assert duplicate["duplicate"] is True and duplicate["submission_attempted"] is False
    assert len(fake.send_calls) == 2


def test_modification_timeout_is_reconciled_without_resending():
    service, fake, store, _, _ = make_service()
    connect_and_configure(service)
    activate(service)
    submit(service, order("modify-timeout-source"))
    position = service.workspace()["positions"][0]
    key = "modify-timeout-0001"
    review = service.review_position_modification(position["position_id"], 1.09, None, key)
    fake.behavior = "timeout_after_accept"
    result = service.submit_position_modification(
        position["position_id"], 1.09, None, key,
        review["review_id"], review["confirmation_phrase"],
    )
    assert result["order"]["status"] == "UNKNOWN"
    assert service.lifecycle == Lifecycle.DISARMED
    service.reconcile(reason="test modification recovery")
    assert store.get_intent(key)["status"] == "MODIFIED"
    assert len(fake.send_calls) == 2


def test_modification_requires_fresh_review_if_position_changed():
    service, fake, _, _, _ = make_service()
    connect_and_configure(service)
    activate(service)
    submit(service, order("modify-stale-review-source"))
    position = service.workspace()["positions"][0]
    key = "modify-stale-review-01"
    review = service.review_position_modification(position["position_id"], 1.09, 1.12, key)
    fake.positions[0]["sl"] = 1.08
    with pytest.raises(OrderBlocked, match="changed since review"):
        service.submit_position_modification(
            position["position_id"], 1.09, 1.12, key,
            review["review_id"], review["confirmation_phrase"],
        )
    assert len(fake.send_calls) == 1
