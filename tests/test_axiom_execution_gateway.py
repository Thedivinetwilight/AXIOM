"""Adversarial tests for the single live execution gateway (FINAL-11/10
§3–§13, §22, §24).

Every test counts BROKER CALLS explicitly: a refusal at any layer must
guarantee broker_calls == 0. The mock live broker is clearly a mock — it
exists so the real-money architecture can be proven without touching a
real exchange (no automated test may ever place a production order).
"""

from __future__ import annotations

import hashlib
import socket
import time
from datetime import datetime, timezone

import pytest

from app.config.settings import LIVE_CONFIRM_PHRASE
from app.database.database import Database
from app.database.repository import Repository
from app.live.gateway import (
    AuthorizationEpochs,
    ExecutionIntent,
    GatewayRefusal,
    LiveExecutionGateway,
    configuration_hash,
)
from app.live.guard_chain import default_chain
from app.live.ops import read_emergency_stop
from app.live.session import ACTIVE, ARMED, LiveSessionManager, LiveTransitionError

# =====================================================================
# fixtures & mock broker
# =====================================================================

def live_settings(**overrides):
    from app.config.settings import Settings

    values = dict(
        mode="LIVE", live_trading=True, broker="UPSTOX_LIVE",
        live_confirm=LIVE_CONFIRM_PHRASE,
        upstox_live_access_token="live-token-under-test",
        max_position_value=1_000_000.0, max_open_positions=5,
        max_daily_loss=50_000.0, max_trades_per_day=100,
        default_timeframe="1m", market_data_mode="realtime",
    )
    values.update(overrides)
    return Settings(**values)


class _MockLedger:
    """Tiny ledger the engine can size against (cash only, no position)."""

    cash = 1_000_000.0
    position_quantity = 0.0
    position_avg_price = 0.0
    mark = 0.0
    realized_pnl = 0.0
    fees = 0.0

    def equity(self) -> float:
        return self.cash

    def unrealized(self) -> float:
        return 0.0

    def snapshot(self) -> dict:
        return {"cash": self.cash, "position_quantity": self.position_quantity,
                "position_avg_price": self.position_avg_price,
                "realized_pnl": self.realized_pnl,
                "unrealized_pnl": 0.0, "fees": self.fees,
                "equity": self.equity(), "mark": self.mark}


class MockLiveBroker:
    """Production-shaped MOCK, explicitly labelled. Counts every call."""

    LIVE = True
    MODE = "UPSTOX_LIVE"

    def __init__(self, *, fail: Exception | None = None):
        self.calls: list[dict] = []
        self.fail = fail
        self.ledger = _MockLedger()
        self._product = "I"
        self.positions = []
        self.working_orders = []

    def submit(self, side, quantity, price, *, order_type="MARKET",
               intent_id=None, symbol=None, instrument_key=None, **extra):
        self.calls.append({"side": side, "quantity": quantity, "price": price,
                           "order_type": order_type, "intent_id": intent_id,
                           "symbol": symbol})
        if self.fail is not None:
            raise self.fail
        return f"mock-order-{len(self.calls)}"

    def open_orders(self):
        return []

    def get_positions(self):
        return list(self.positions)

    def get_account_positions(self):
        return list(self.positions)

    def get_open_orders(self):
        return list(self.working_orders)

    def reconcile(self):
        return []


class MockPaperBroker:
    LIVE = False

    def submit(self, *a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("a non-live broker must never receive live intents")


@pytest.fixture()
def repo(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'gateway.db'}")
    return Repository(database)


class _FixtureReadySupervisor:
    """Test double for a child that owns a fresh, reconciled live lease.

    The broker in this module is an in-memory mock and no test places an order
    at an exchange. Readiness is nevertheless backed by the same durable lease,
    runtime identity, reconciliation stamp, market-data time and token record
    required by the production state machine.
    """

    def __init__(self, repository):
        self.repository = repository

    def readiness_context(self):
        from app.live.lock import EngineLock

        lease = EngineLock(self.repository, live=True).status()
        runtime = self.repository.get_engine_state("live_engine_runtime", {}) or {}
        control = self.repository.get_engine_state("live_engine_control", {}) or {}
        if (control.get("stop_requested") is True
                or lease.get("held") is not True or lease.get("live") is not True
                or lease.get("stale") is not False
                or runtime.get("state") != "READY"
                or runtime.get("owner") != lease.get("owner")
                or runtime.get("pid") != lease.get("pid")
                or runtime.get("host") != lease.get("host")
                or runtime.get("generation") != control.get("generation")
                or runtime.get("reconciliation_ok") is not True):
            raise RuntimeError("fixture engine has no reconciled live lease")
        return {"supervisor_verified": True,
                "supervisor_lease_owner": lease["owner"],
                "supervisor_pid": lease["pid"],
                "supervisor_host": lease["host"],
                "supervisor_generation": control.get("generation"),
                "last_market_data_ts": self.repository.get_engine_state(
                    "last_market_data_ts")}

    def request_stop(self, *, reason="fixture stop"):
        control = self.repository.get_engine_state("live_engine_control", {}) or {}
        self.repository.set_engine_state("live_engine_control", {
            **control, "stop_requested": True, "reason": reason})
        return {"running": True, "stop_requested": True}


def _install_fixture_live_engine(repository, credential="live-token-under-test"):
    from app.live.lock import EngineLock

    now = datetime.now(timezone.utc).isoformat()
    lock = EngineLock(repository, live=True)
    assert lock.acquire().granted
    lease = lock.status()
    repository.set_engine_state("last_market_data_ts", now)
    repository.set_engine_state("last_reconcile_at", {"ts": now, "fills": 0})
    repository.set_engine_state("live_reconcile_status", {
        "ok": True, "ts": now, "fills": 0})
    repository.set_engine_state("live_token_verified", {
        "verified_at": now,
        "credential_sha256": hashlib.sha256(
            credential.encode("utf-8")).hexdigest()})
    repository.set_engine_state("live_engine_control", {
        "stop_requested": False, "generation": 1})
    repository.set_engine_state("live_engine_runtime", {
        "state": "READY", "owner": lease["owner"], "pid": lease["pid"],
        "host": lease["host"], "generation": 1,
        "reconciliation_ok": True, "reconciled_at": now})
    return lock, _FixtureReadySupervisor(repository)


def _restart_fixture_live_engine(repository):
    """Model a drained child followed by a newly supervised child generation."""
    from app.live.lock import EngineLock

    now = datetime.now(timezone.utc).isoformat()
    control = repository.get_engine_state("live_engine_control", {}) or {}
    generation = int(control.get("generation", 0)) + 1
    repository.set_engine_state("live_engine_control", {
        "stop_requested": False, "generation": generation})
    lease = EngineLock(repository, live=True).status()
    assert lease.get("held") is True and lease.get("live") is True
    repository.set_engine_state("last_market_data_ts", now)
    repository.set_engine_state("last_reconcile_at", {"ts": now, "fills": 0})
    repository.set_engine_state("live_reconcile_status", {
        "ok": True, "ts": now, "fills": 0})
    repository.set_engine_state("live_token_verified", {
        "verified_at": now,
        "credential_sha256": hashlib.sha256(
            b"live-token-under-test").hexdigest()})
    repository.set_engine_state("live_engine_runtime", {
        "state": "READY", "owner": lease["owner"], "pid": lease["pid"],
        "host": lease["host"], "generation": generation,
        "reconciliation_ok": True, "reconciled_at": now})


@pytest.fixture()
def armed(repo):
    """A mock broker plus a supervised, reconciled fixture engine."""
    settings = live_settings()
    _lock, supervisor = _install_fixture_live_engine(repo)
    manager = LiveSessionManager(repo, engine_supervisor=supervisor)
    manager.arm(settings, confirmation_phrase=LIVE_CONFIRM_PHRASE,
                repository=repo)
    manager.activate(actor="test")
    return settings, manager


def make_gateway(repo, manager, settings, broker=None, *, chain=None,
                 context=None):
    from app.live.guard_chain import default_chain

    risk_context = {"current_exposure": 0.0,
                    "daily_loss_breached": False,
                    "drawdown_breached": False,
                    "canary_exhausted": False}
    risk_context.update(context or {})
    return LiveExecutionGateway(
        settings=settings, repository=repo, session_manager=manager,
        broker=broker or MockLiveBroker(),
        guard_chain=chain or default_chain(
            settings=settings, session_state="ACTIVE", max_entry_age_s=3600.0),
        epochs=manager.epochs,
        emergency_stop_reader=read_emergency_stop,
        account_scope="upstox-test-account",
        context_provider=lambda: dict(risk_context))


def entry_intent(epoch, **overrides):
    values = dict(purpose="ENTRY", market_id="india", identity="NSE|RELIANCE",
                  side="BUY", quantity=10, order_type="LIMIT", price=2900.0,
                  account_scope="upstox-test-account", universe="core",
                  strategy_id="strat-v1", strategy_lineage="root/strat-v1",
                  signal_id="sig-1", signal_ts=time.time(),
                  evidence_snapshot_id="ev-1", risk_snapshot_id="risk-1",
                  configuration_hash=configuration_hash(live_settings()),
                  model_revision="m-1",
                  authorization_epoch=epoch, build_identity="test-build")
    values.update(overrides)
    return ExecutionIntent(**values)


# =====================================================================
# happy path
# =====================================================================

class TestGatewayHappyPath:
    def test_active_session_without_its_live_lease_never_calls_broker(
            self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        lease = repo.read_lease("engine_lock")
        assert lease and repo.release_lease("engine_lock", lease["owner"])
        broker = MockLiveBroker()
        gateway = make_gateway(repo, manager, settings, broker)

        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "ENGINE LEASE"
        assert gateway.broker_calls == 0
        assert broker.calls == []

    def test_lease_owned_by_another_process_never_calls_broker(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        with repo.db.transaction() as connection:
            connection.execute(
                "UPDATE engine_lease SET owner=?, pid=?, host=? WHERE name=?",
                ("another-host:9999", 9999, "another-host", "engine_lock"))
        broker = MockLiveBroker()
        gateway = make_gateway(repo, manager, settings, broker)

        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "ENGINE LEASE"
        assert gateway.broker_calls == 0
        assert broker.calls == []

    def test_authorized_entry_reaches_the_broker_exactly_once(self, repo, armed):
        from app.engine.state import ACKNOWLEDGED, SubmissionJournal

        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings)
        result = gateway.submit(entry_intent(epoch))
        assert result["authorized"] and result["submitted"]
        assert gateway.broker_calls == 1
        assert gateway.broker.calls[0]["intent_id"] == result["intent_id"]
        assert SubmissionJournal(repo).get(result["intent_id"])["state"] == ACKNOWLEDGED

    def test_final_boundary_refuses_a_stale_reconciliation(self, repo, armed):
        from datetime import timedelta

        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        old = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        repo.set_engine_state("last_reconcile_at", {"ts": old, "fills": 0})
        repo.set_engine_state("live_reconcile_status", {"ok": True, "ts": old})
        runtime = repo.get_engine_state("live_engine_runtime")
        runtime["reconciled_at"] = old
        repo.set_engine_state("live_engine_runtime", runtime)
        gateway = make_gateway(repo, manager, settings)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "SUPERVISOR READINESS"
        assert gateway.broker.calls == []

    def test_final_boundary_refuses_a_malformed_control_record(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        repo.set_engine_state("live_engine_control", {
            "stop_requested": "false", "generation": 1})
        gateway = make_gateway(repo, manager, settings)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "LIVE ENGINE CONTROL"
        assert gateway.broker.calls == []

    def test_final_boundary_rechecks_token_freshness(self, repo, armed):
        from datetime import timedelta

        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        old = datetime.now(timezone.utc) - timedelta(days=1)
        repo.set_engine_state("live_token_verified", {"verified_at": old.isoformat()})
        gateway = make_gateway(repo, manager, settings)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "TOKEN FRESHNESS"
        assert gateway.broker.calls == []

    def test_live_token_must_match_the_credential_that_passed_preflight(
            self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        repo.set_engine_state("live_token_verified", {
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "credential_sha256": hashlib.sha256(b"a-different-token").hexdigest()})
        gateway = make_gateway(repo, manager, settings)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "TOKEN FRESHNESS"
        assert "CHANGED_OR_UNBOUND" in exc.value.reason
        assert gateway.broker.calls == []

    def test_guard_chain_is_mandatory_for_live_entries(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings)
        gateway.guard_chain = None
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "GUARD CHAIN"
        assert gateway.broker.calls == []

    def test_emergency_stop_flipped_inside_final_guard_blocks_the_order(
            self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings)
        calls = 0

        def flip_stop_during_final_context():
            nonlocal calls
            calls += 1
            if calls == 2:
                repo.set_engine_state("emergency_stop", {
                    "stopped": True, "reason": "concurrent final-boundary regression"})
            return {"current_exposure": 0.0,
                    "daily_loss_breached": False,
                    "drawdown_breached": False,
                    "canary_exhausted": False}

        gateway.context_provider = flip_stop_during_final_context
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "EMERGENCY STOP"
        assert gateway.broker.calls == []

    def test_emergency_stop_after_durable_authorization_audit_blocks_broker(
            self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        record_event = repo.record_system_event

        def flip_stop_after_authorization(kind, payload):
            record_event(kind, payload)
            if kind == "LIVE_INTENT_AUTHORIZED":
                repo.set_engine_state("emergency_stop", {
                    "stopped": True, "reason": "audit-boundary regression"})

        repo.record_system_event = flip_stop_after_authorization
        gateway = make_gateway(repo, manager, settings)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "EMERGENCY STOP"
        assert gateway.broker.calls == []

    def test_settings_change_during_final_guard_invalidates_authorization(
            self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings)
        calls = 0

        def change_configuration_during_final_context():
            nonlocal calls
            calls += 1
            if calls == 2:
                object.__setattr__(settings, "max_daily_loss", 1.0)
            return {"current_exposure": 0.0,
                    "daily_loss_breached": False,
                    "drawdown_breached": False,
                    "canary_exhausted": False}

        gateway.context_provider = change_configuration_during_final_context
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "CONFIGURATION"
        assert gateway.broker.calls == []

    def test_final_entry_cap_uses_authoritative_account_holdings(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        broker = MockLiveBroker()
        broker.positions = [{
            "instrument_token": "NSE|RELIANCE", "product": "D",
            "quantity": 400, "last_price": 2500.0}]
        gateway = make_gateway(repo, manager, settings, broker=broker)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "ACCOUNT RISK"
        assert "MAX_POSITION_VALUE" in exc.value.reason
        assert broker.calls == []

    def test_final_entry_blocks_when_account_positions_are_unreadable(
            self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        broker = MockLiveBroker()
        broker.get_account_positions = lambda: (_ for _ in ()).throw(
            RuntimeError("positions unavailable"))
        gateway = make_gateway(repo, manager, settings, broker=broker)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "ACCOUNT RISK"
        assert broker.calls == []

    def test_final_boundary_rejects_oversized_canary_intent(self, repo):
        settings = live_settings(live_canary_max_notional=100.0,
                                 live_canary_orders=2)
        _lock, supervisor = _install_fixture_live_engine(repo)
        manager = LiveSessionManager(repo, engine_supervisor=supervisor)
        manager.arm(settings, confirmation_phrase=LIVE_CONFIRM_PHRASE,
                    repository=repo)
        manager.activate(settings=settings, actor="canary-test")
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings)
        intent = entry_intent(
            epoch, quantity=1, price=500.0,
            configuration_hash=configuration_hash(settings),
            intent_id="oversized-canary-intent")
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(intent)
        assert exc.value.layer == "CANARY GUARD"
        assert gateway.broker.calls == []
        assert gateway.live_ops.canary.submitted() == 0

    def test_canary_attempt_is_reserved_before_uncertain_broker_result(
            self, repo, armed):
        from app.engine.state import UNKNOWN, SubmissionJournal

        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        broker = MockLiveBroker(fail=RuntimeError("mock transport failure"))
        gateway = make_gateway(repo, manager, settings, broker=broker)
        intent = entry_intent(epoch, intent_id="uncertain-canary-intent")
        with pytest.raises(RuntimeError):
            gateway.submit(intent)
        assert gateway.live_ops.canary.submitted() == 1
        assert SubmissionJournal(repo).get(intent.intent_id)["state"] == UNKNOWN
        second = make_gateway(repo, manager, settings, broker=broker)
        with pytest.raises(GatewayRefusal) as exc:
            second.submit(intent)
        assert exc.value.layer == "DUPLICATE CHECK"
        assert len(broker.calls) == 1

    def test_risk_reduce_exit_allowed_while_paused(self, repo, armed):
        settings, manager = armed
        manager.pause()
        broker = MockLiveBroker()
        broker._product = "I"
        broker.get_positions = lambda: [{
            "instrument_token": "NSE|RELIANCE", "product": "I", "quantity": 10}]
        broker.get_open_orders = lambda: []
        gateway = make_gateway(repo, manager, settings, broker=broker)
        intent = ExecutionIntent(purpose="RISK_REDUCE", market_id="india",
                                 identity="NSE|RELIANCE", side="SELL",
                                 quantity=10, account_scope="upstox-test-account")
        assert gateway.submit(intent)["submitted"]
        assert gateway.broker_calls == 1

    def test_long_only_risk_reduction_rejects_buy_direction(self, repo, armed):
        settings, manager = armed
        manager.pause()
        gateway = make_gateway(repo, manager, settings)
        intent = ExecutionIntent(purpose="RISK_REDUCE", market_id="india",
                                 identity="NSE|RELIANCE", side="BUY",
                                 quantity=1, account_scope="upstox-test-account")
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(intent)
        assert exc.value.layer == "INTENT"
        assert gateway.broker_calls == 0


# =====================================================================
# authorization epochs (§6)
# =====================================================================

class TestAuthorizationEpochs:
    def test_concurrent_activations_mint_unique_monotonic_epochs(self, repo):
        from concurrent.futures import ThreadPoolExecutor

        epochs = AuthorizationEpochs(repo)
        with ThreadPoolExecutor(max_workers=8) as pool:
            states = list(pool.map(
                lambda index: epochs.activate(
                    actor=f"test-{index}", configuration_hash="config-hash"),
                range(16)))
        assert sorted(state["epoch"] for state in states) == list(range(1, 17))

    def test_malformed_epoch_is_not_repaired_into_live_permission(self, repo):
        repo.set_engine_state("live_authorization_epoch", "not-an-epoch")
        epochs = AuthorizationEpochs(repo)
        with pytest.raises(ValueError, match="malformed"):
            epochs.activate(configuration_hash="config-hash")
        assert repo.get_engine_state("live_authorization_epoch") == "not-an-epoch"
        assert epochs.validate(1)[0] is False

    def test_stop_invalidates_the_epoch(self, repo, armed):
        settings, manager = armed
        old_epoch = manager.epochs.current()["epoch"]
        manager.stop(reason="operator done")
        gateway = make_gateway(repo, manager, settings)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(old_epoch))
        assert exc.value.layer == "AUTHORIZATION EPOCH"
        assert gateway.broker_calls == 0

    def test_stale_engine_with_old_epoch_is_refused_after_reactivation(
            self, repo, armed):
        settings, manager = armed
        stale_epoch = manager.epochs.current()["epoch"]
        manager.stop(reason="cycle 1 done")
        # Model completion of the supervised drain and a fresh child generation.
        _restart_fixture_live_engine(repo)
        # re-arm + re-activate mints a FRESH epoch
        manager.arm(settings, confirmation_phrase=LIVE_CONFIRM_PHRASE,
                    repository=repo)
        manager.activate(actor="test")
        fresh_epoch = manager.epochs.current()["epoch"]
        assert fresh_epoch == stale_epoch + 1
        gateway = make_gateway(repo, manager, settings)
        with pytest.raises(GatewayRefusal):
            gateway.submit(entry_intent(stale_epoch))
        assert gateway.submit(entry_intent(fresh_epoch))["submitted"]
        assert gateway.broker_calls == 1

    def test_epoch_survives_restart_but_not_reauthorization(self, repo, armed):
        _, manager = armed
        epoch_before = manager.epochs.current()["epoch"]
        # A fresh manager instance = simulated process restart: the durable
        # epoch must still be visible (and still valid while ACTIVE).
        reborn = LiveSessionManager(repo)
        assert reborn.epochs.current()["epoch"] == epoch_before
        assert reborn.epochs.validate(epoch_before)[0] is True

    def test_no_activation_means_no_epoch(self, repo):
        manager = LiveSessionManager(repo)
        ok, why = manager.epochs.validate(1)
        assert ok is False and "no authorization epoch" in why

    def test_pause_keeps_epoch_valid_for_risk_management(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        manager.pause()
        assert manager.epochs.validate(epoch)[0] is True


# =====================================================================
# session state (§7)
# =====================================================================

class TestSessionStateGate:
    def test_entry_refused_when_not_active(self, repo):
        settings = live_settings()
        _lock, supervisor = _install_fixture_live_engine(repo)
        manager = LiveSessionManager(repo, engine_supervisor=supervisor)
        manager.arm(settings, confirmation_phrase=LIVE_CONFIRM_PHRASE,
                    repository=repo)
        manager.epochs.activate(
            actor="test", configuration_hash=configuration_hash(settings))
        gateway = make_gateway(repo, manager, settings)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(1))            # ...but state is ARMED
        assert exc.value.layer == "SESSION STATE"
        assert gateway.broker_calls == 0

    def test_exit_refused_when_no_live_session_exists(self, repo):
        settings = live_settings()
        manager = LiveSessionManager(repo)
        gateway = make_gateway(repo, manager, settings)
        intent = ExecutionIntent(purpose="EXIT", market_id="india",
                                 identity="NSE|X", side="SELL", quantity=1)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(intent)
        assert exc.value.layer == "SESSION STATE"


# =====================================================================
# emergency-stop semantics (§12)
# =====================================================================

class TestEmergencyStopSemantics:
    def test_persisted_emergency_read_fails_closed_without_leaking_error_text(self):
        class BrokenRepository:
            def get_engine_state(self, key, default=None):
                raise RuntimeError("database-password=do-not-expose")

        stopped, reason = read_emergency_stop(BrokenRepository())
        assert stopped is True
        assert "unreadable" in reason and "fail closed" in reason
        assert "do-not-expose" not in reason

    def test_malformed_emergency_record_fails_closed(self):
        class MalformedRepository:
            def get_engine_state(self, key, default=None):
                return "not-a-record"

        stopped, reason = read_emergency_stop(MalformedRepository())
        assert stopped is True
        assert "malformed" in reason.lower()

    def test_entry_blocked_exit_available_under_emergency_stop(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        repo.set_engine_state("emergency_stop",
                              {"stopped": True, "reason": "drill"})
        broker = MockLiveBroker()
        broker.positions = [{"instrument_token": "NSE|RELIANCE",
                             "product": "I", "quantity": 10}]
        gateway = make_gateway(repo, manager, settings, broker=broker)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "EMERGENCY STOP"
        assert gateway.broker_calls == 0
        # risk-reducing exit must NOT be trapped
        exit_intent = ExecutionIntent(purpose="RISK_REDUCE", market_id="india",
                                      identity="NSE|RELIANCE", side="SELL",
                                      quantity=10,
                                      account_scope="upstox-test-account")
        assert gateway.submit(exit_intent)["submitted"]
        assert gateway.broker_calls == 1

    def test_unreadable_emergency_flag_fails_closed(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]

        def broken_reader(repository):
            raise RuntimeError("flag store unreadable")

        gateway = make_gateway(repo, manager, settings)
        gateway.emergency_stop_reader = broken_reader
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "EMERGENCY STOP"
        assert gateway.broker_calls == 0

    def test_missing_emergency_record_fails_closed(self, repo):
        with repo.db.transaction() as connection:
            connection.execute(
                "DELETE FROM engine_state WHERE key='emergency_stop'")

        stopped, reason = read_emergency_stop(repo)
        assert stopped is True
        assert "missing" in reason.lower()

    def test_fresh_database_has_an_explicit_clear_emergency_record(self, repo):
        flag = repo.get_engine_state("emergency_stop", None)
        assert isinstance(flag, dict)
        assert flag.get("stopped") is False

    @pytest.mark.parametrize("stopped_value", [0, 1, "false", None])
    def test_non_boolean_emergency_value_is_unknown_and_fails_closed(
            self, repo, stopped_value):
        repo.set_engine_state("emergency_stop", {"stopped": stopped_value})
        stopped, reason = read_emergency_stop(repo)
        assert stopped is True
        assert "malformed" in reason.lower()

    def test_reconcile_purpose_never_becomes_an_order_submission(self,
                                                               repo, armed):
        settings, manager = armed
        broker = MockLiveBroker()
        reconciliations = []
        broker.reconcile = lambda: (reconciliations.append("read") or [])
        gateway = make_gateway(repo, manager, settings, broker)
        intent = ExecutionIntent(purpose="RECONCILE", market_id="india",
                                 identity="NSE|RELIANCE", side="BUY", quantity=1,
                                 account_scope="upstox-test-account")

        result = gateway.submit(intent)

        assert result["authorized"] is True
        assert result["submitted"] is False
        assert reconciliations == ["read"]
        assert broker.calls == []
        assert gateway.broker_calls == 1


# =====================================================================
# exact confirmation phrase — four independent layers (§13)
# =====================================================================

class TestExactPhraseLayers:
    def test_gateway_layer_refuses_wrong_configured_phrase(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        # Simulate configuration drift/corruption AFTER validated construction:
        # the gateway layer must catch it independently of the settings layer.
        drifted = live_settings()
        object.__setattr__(drifted, "live_confirm", "i-accept-real-money-risk")
        gateway = make_gateway(repo, manager, drifted)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "CONFIRMATION PHRASE"
        assert gateway.broker_calls == 0

    @pytest.mark.parametrize("phrase", [
        "", "  ", "I-ACCEPT-REAL-MONEY-RISK ", " I-ACCEPT-REAL-MONEY-RISK",
        "i-accept-real-money-risk", "I_ACCEPT_REAL_MONEY_RISK",
        "I-ACCEPT-REAL-MONEY-RISK\n",
    ])
    def test_session_layer_refuses_whitespace_and_case_tricks(self, repo, phrase):
        manager = LiveSessionManager(repo)
        with pytest.raises(LiveTransitionError):
            manager.arm(live_settings(), confirmation_phrase=phrase)

    def test_broker_constructor_requires_the_exact_phrase(self):
        from app.brokers.live import UpstoxLiveBroker

        with pytest.raises(ValueError):
            UpstoxLiveBroker("token", "key", confirm="")
        with pytest.raises(ValueError):
            UpstoxLiveBroker("token", "key", confirm="something-else")
        with pytest.raises(ValueError):
            UpstoxLiveBroker("token", "key", confirm="  ")

    def test_settings_layer_refuses_wrong_phrase(self):
        from app.config.settings import Settings

        # The settings layer refuses to even construct a live configuration
        # with a wrong phrase (validated construction).
        with pytest.raises(ValueError, match="LIVE_CONFIRM"):
            Settings(mode="LIVE", live_trading=True, broker="UPSTOX_LIVE",
                     live_confirm="wrong", upstox_live_access_token="tok",
                     max_position_value=1000.0, max_open_positions=1,
                     max_daily_loss=100.0, max_trades_per_day=5)


# =====================================================================
# broker identity & account scope (§4, §16)
# =====================================================================

class TestBrokerIdentity:
    def test_non_live_broker_refused(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings, broker=MockPaperBroker())
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "BROKER IDENTITY"

    def test_account_scope_mismatch_refused(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch, account_scope="other-account"))
        assert exc.value.layer == "ACCOUNT SCOPE"
        assert gateway.broker_calls == 0


# =====================================================================
# guard chain as the real final gate (§10)
# =====================================================================

class TestGuardChainIntegration:
    def _chain(self, settings):
        return default_chain(settings=settings, session_state="ACTIVE",
                             max_entry_age_s=3600.0)

    def test_passing_chain_submits(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings, chain=self._chain(settings))
        assert gateway.submit(entry_intent(epoch))["submitted"]
        assert gateway.broker_calls == 1

    def test_stale_signal_refused_by_chain_with_zero_broker_calls(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings, chain=self._chain(settings))
        stale = entry_intent(epoch, signal_ts=time.time() - 9999)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(stale)
        assert exc.value.layer == "GUARD CHAIN"
        assert "FRESHNESS" in exc.value.reason.upper()
        assert gateway.broker_calls == 0

    def test_future_signal_refused_by_chain(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings, chain=self._chain(settings))
        future = entry_intent(epoch, signal_ts=time.time() + 60.0)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(future)
        assert exc.value.layer == "GUARD CHAIN"
        assert "FRESHNESS" in exc.value.reason.upper()
        assert gateway.broker_calls == 0

    def test_daily_loss_breach_refused_by_chain(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings,
                               chain=self._chain(settings),
                               context={"daily_loss_breached": True})
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch))
        assert exc.value.layer == "GUARD CHAIN"
        assert gateway.broker_calls == 0

    def test_context_provider_failure_fails_closed(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings,
                               chain=self._chain(settings))

        def broken():
            raise RuntimeError("risk state unreadable")

        gateway.context_provider = broken
        with pytest.raises(GatewayRefusal):
            gateway.submit(entry_intent(epoch))
        assert gateway.broker_calls == 0

    def test_non_finite_order_values_are_refused(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        broker = MockLiveBroker()
        gateway = make_gateway(repo, manager, settings, broker=broker)
        for quantity in (float("nan"), float("inf")):
            with pytest.raises(GatewayRefusal) as exc:
                gateway.submit(entry_intent(epoch, quantity=quantity))
            assert exc.value.layer == "INTENT"
        assert broker.calls == []

    def test_non_finite_signal_timestamp_is_refused(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        broker = MockLiveBroker()
        gateway = make_gateway(repo, manager, settings, broker=broker)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch, signal_ts=float("nan")))
        assert exc.value.layer == "INTENT"
        assert broker.calls == []


# =====================================================================
# duplicates & structural integrity (§5, §22)
# =====================================================================

class TestDuplicatesAndStructure:
    def test_duplicate_intent_refused_second_time(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings)
        intent = entry_intent(epoch)
        assert gateway.submit(intent)["submitted"]
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(intent)
        assert exc.value.layer == "DUPLICATE CHECK"
        assert gateway.broker_calls == 1

    def test_duplicate_intent_stays_blocked_after_gateway_restart(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        broker = MockLiveBroker()
        first = make_gateway(repo, manager, settings, broker=broker)
        second = make_gateway(repo, manager, settings, broker=broker)
        intent = entry_intent(epoch, intent_id="durable-signal-1")
        assert first.submit(intent)["submitted"]
        with pytest.raises(GatewayRefusal) as exc:
            second.submit(intent)
        assert exc.value.layer == "DUPLICATE CHECK"
        assert len(broker.calls) == 1

    @pytest.mark.parametrize("overrides,fragment", [
        ({"strategy_id": ""}, "strategy_id"),
        ({"signal_id": ""}, "signal_id"),
        ({"signal_ts": None}, "signal_ts"),
        ({"configuration_hash": ""}, "configuration_hash"),
        ({"account_scope": ""}, "account_scope"),
        ({"authorization_epoch": None}, "epoch"),
        ({"quantity": 0}, "quantity"),
        ({"side": "HOLD"}, "side"),
    ])
    def test_incomplete_intents_refused(self, repo, armed, overrides, fragment):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch, **overrides))
        assert exc.value.layer == "INTENT"
        assert fragment in exc.value.reason
        assert gateway.broker_calls == 0

    def test_entry_sell_without_universe_refused(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(epoch, side="SELL", universe=""))
        assert exc.value.layer == "INTENT"

    def test_audit_written_before_submission(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings)
        gateway.submit(entry_intent(epoch))
        kinds = [e["kind"] for e in repo.list_system_events(limit=20)]
        assert "LIVE_INTENT_AUTHORIZED" in kinds
        assert "LIVE_INTENT_SUBMITTED" in kinds
        assert kinds.index("LIVE_INTENT_AUTHORIZED") > kinds.index(
            "LIVE_INTENT_SUBMITTED")  # newest first -> authorized recorded first

    def test_broker_failure_does_not_report_success(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        failing = MockLiveBroker(fail=RuntimeError("network died mid-submit"))
        gateway = make_gateway(repo, manager, settings, broker=failing)
        with pytest.raises(RuntimeError):
            gateway.submit(entry_intent(epoch))
        kinds = [e["kind"] for e in repo.list_system_events(limit=20)]
        assert "LIVE_INTENT_FAILED" in kinds
        assert "LIVE_INTENT_SUBMITTED" not in kinds

    def test_unreadable_live_open_orders_blocks_readiness(self, repo, monkeypatch):
        from app.live.preflight import ERROR, run_readiness_center_checks

        def broken_open_orders():
            raise RuntimeError("order table unavailable")

        monkeypatch.setattr(repo, "open_live_orders", broken_open_orders)
        checks = run_readiness_center_checks(live_settings(), repository=repo)
        check = next(c for c in checks if c.name == "no_open_orders_left")
        assert check.ok is False
        assert check.severity == ERROR
        assert "could not be read" in check.detail.lower()


def test_configuration_hash_is_deterministic():
    settings = live_settings()
    assert configuration_hash(settings) == configuration_hash(live_settings())
    assert configuration_hash(settings) != configuration_hash(
        live_settings(max_daily_loss=1.0))


# =====================================================================
# the gateway-routed broker facade (FINAL-11/10 §3/§4 engine wiring)
# =====================================================================

RISK_CONTEXT = {"current_exposure": 0.0, "daily_loss_breached": False,
                "drawdown_breached": False, "emergency_stop": False,
                "canary_exhausted": False}


class TestGatewayRoutedBroker:
    """The engine's broker facade must make every refusal un-submittable."""

    def _facade(self, repo, manager, settings, broker):
        from app.live.gateway_broker import GatewayRoutedBroker

        gateway = LiveExecutionGateway(
            settings=settings, repository=repo, session_manager=manager,
            broker=broker, guard_chain=default_chain(settings=settings),
            epochs=manager.epochs, emergency_stop_reader=read_emergency_stop,
            account_scope="upstox-test-account",
            context_provider=lambda: dict(RISK_CONTEXT))
        return GatewayRoutedBroker(
            broker, gateway, market_id="NSE_EQ", strategy_id="strat-v1",
            account_scope="upstox-test-account", universe="core")

    def test_authorized_entry_reaches_the_broker_exactly_once(self, repo, armed):
        settings, manager = armed
        broker = MockLiveBroker()
        facade = self._facade(repo, manager, settings, broker)
        order_id = facade.submit("BUY", 10, 2900.0, intent_id="sig-1",
                                 instrument_key="NSE_EQ|INE002A01018",
                                 signal_ts=time.time())
        assert order_id == "mock-order-1"
        assert len(broker.calls) == 1
        assert broker.calls[0]["side"] == "BUY"

    def test_stale_strategy_timestamp_is_not_replaced_with_submission_time(
            self, repo, armed):
        settings, manager = armed
        broker = MockLiveBroker()
        facade = self._facade(repo, manager, settings, broker)
        with pytest.raises(GatewayRefusal) as exc:
            facade.submit("BUY", 10, 2900.0, intent_id="stale-signal",
                          instrument_key="NSE_EQ|INE002A01018",
                          signal_ts=time.time() - 60.0)
        assert exc.value.layer == "GUARD CHAIN"
        assert "FRESHNESS" in exc.value.reason.upper()
        assert broker.calls == []

    def test_exit_without_confirmed_long_position_is_refused(self, repo, armed):
        settings, manager = armed
        broker = MockLiveBroker()
        facade = self._facade(repo, manager, settings, broker)
        repo.set_engine_state("emergency_stop", {
            "stopped": True, "reason": "exit must remain possible only against a holding"})
        with pytest.raises(GatewayRefusal) as exc:
            facade.submit("SELL", 10, 100.0, intent_id="no-position-exit",
                          instrument_key="NSE_EQ|INE002A01018")
        assert exc.value.layer == "POSITION REDUCTION"
        assert broker.calls == []

    def test_exit_cannot_exceed_position_after_pending_sells(self, repo, armed):
        settings, manager = armed
        broker = MockLiveBroker()
        broker._product = "I"
        broker.get_positions = lambda: [{
            "instrument_token": "NSE_EQ|INE002A01018", "product": "I",
            "quantity": 10}]
        broker.get_open_orders = lambda: [{
            "instrument_token": "NSE_EQ|INE002A01018", "product": "I",
            "transaction_type": "SELL", "status": "open",
            "pending_quantity": 4}]
        facade = self._facade(repo, manager, settings, broker)
        with pytest.raises(GatewayRefusal) as exc:
            facade.submit("SELL", 7, 100.0, intent_id="overdraw-exit",
                          instrument_key="NSE_EQ|INE002A01018")
        assert exc.value.layer == "POSITION REDUCTION"
        assert "available" in exc.value.reason.lower()
        assert broker.calls == []

    def test_no_epoch_means_no_entry(self, repo, armed):
        settings, manager = armed
        manager.epochs.invalidate(reason="disarmed under test")
        broker = MockLiveBroker()
        facade = self._facade(repo, manager, settings, broker)
        with pytest.raises(GatewayRefusal) as exc:
            facade.submit("BUY", 10, 2900.0, intent_id="sig-2",
                          instrument_key="NSE_EQ|INE002A01018",
                          signal_ts=time.time())
        assert exc.value.layer == "AUTHORIZATION EPOCH"
        assert broker.calls == []

    def test_inactive_session_blocks_entries_with_zero_broker_calls(self, repo):
        settings = live_settings()
        manager = LiveSessionManager(repo)          # session is OFF
        broker = MockLiveBroker()
        facade = self._facade(repo, manager, settings, broker)
        with pytest.raises(GatewayRefusal):
            facade.submit("BUY", 1, 100.0, intent_id="sig-3",
                          instrument_key="NSE_EQ|X", signal_ts=time.time())
        assert broker.calls == []

    def test_emergency_stop_blocks_entries_but_not_exits(self, repo, armed):
        settings, manager = armed
        broker = MockLiveBroker()
        broker.positions = [{"instrument_token": "NSE_EQ|X",
                             "product": "I", "quantity": 10}]
        facade = self._facade(repo, manager, settings, broker)
        repo.set_engine_state("emergency_stop",
                              {"stopped": True, "reason": "drill"})
        with pytest.raises(GatewayRefusal) as exc:
            facade.submit("BUY", 10, 100.0, intent_id="sig-4",
                          instrument_key="NSE_EQ|X", signal_ts=time.time())
        assert exc.value.layer == "EMERGENCY STOP"
        assert broker.calls == []                    # entry never left
        order_id = facade.submit("SELL", 10, 100.0, intent_id="sig-5",
                                 instrument_key="NSE_EQ|X")
        assert order_id and len(broker.calls) == 1   # exit escapes the stop

    def test_guard_chain_refusal_never_reaches_the_broker(self, repo, armed):
        settings, manager = armed
        broker = MockLiveBroker()
        facade = self._facade(repo, manager, settings, broker)
        ctx = dict(RISK_CONTEXT, daily_loss_breached=True)
        facade.gateway.context_provider = lambda: ctx
        with pytest.raises(GatewayRefusal) as exc:
            facade.submit("BUY", 10, 100.0, intent_id="sig-6",
                          instrument_key="NSE_EQ|X", signal_ts=time.time())
        assert exc.value.layer == "GUARD CHAIN"
        assert broker.calls == []

    def test_cancel_uses_gateway_and_remains_available_during_emergency(self,
                                                                       repo, armed):
        from types import SimpleNamespace

        settings, manager = armed
        broker = MockLiveBroker()
        broker.orders = {"working-1": SimpleNamespace(
            symbol="NSE|X", side="BUY", quantity=1.0, price=100.0,
            status="OPEN", our_id="mock-working-1")}
        broker.cancel_calls = []
        broker.cancel = lambda order_id: (
            broker.cancel_calls.append(order_id) or {"status": "CANCELLED"})
        facade = self._facade(repo, manager, settings, broker)
        # A stop invalidates the epoch and sets the durable drain request, but
        # it must not block a cancellation routed through the gateway.
        manager.block(reason="emergency stop: test")
        repo.set_engine_state("emergency_stop", {
            "stopped": True, "reason": "test"})
        facade.gateway.context_provider = lambda: (_ for _ in ()).throw(
            RuntimeError("entry risk context unavailable"))
        result = facade.cancel("working-1")
        assert result["status"] == "CANCELLED"
        assert broker.cancel_calls == ["working-1"]
        assert broker.calls == []  # no submit path was called
        assert facade.gateway.broker_calls == 1
        events = [e["kind"] for e in repo.list_system_events(limit=50)]
        assert "LIVE_INTENT_AUTHORIZED" in events
        assert "LIVE_ORDER_CANCEL_RESULT" in events

    def test_cancel_refuses_without_the_owned_live_lease(self, repo, armed):
        from types import SimpleNamespace

        from app.live.lock import EngineLock

        settings, manager = armed
        broker = MockLiveBroker()
        broker.orders = {"working-2": SimpleNamespace(
            symbol="NSE|X", side="BUY", quantity=1.0, price=100.0,
            status="OPEN", our_id="mock-working-2")}
        broker.cancel_calls = []
        broker.cancel = lambda order_id: (
            broker.cancel_calls.append(order_id) or {"status": "CANCELLED"})
        facade = self._facade(repo, manager, settings, broker)
        lease = EngineLock(repo, live=True).status()
        assert repo.release_lease("engine_lock", lease["owner"])
        with pytest.raises(GatewayRefusal) as exc:
            facade.cancel("working-2")
        assert exc.value.layer == "ENGINE LEASE"
        assert broker.cancel_calls == []
        assert facade.gateway.broker_calls == 0

    # ----------------------------------------------- engine-level wiring ----

    def _engine_with_facade(self, repo, facade):
        from datetime import datetime, timezone

        from app.data.models import Candle
        from app.engine.sandbox_engine import SandboxEngine
        from app.learning.journal import LearningJournal
        from app.risk.limits import HardRiskLimits
        from app.strategies import create_strategy

        class Provider:
            symbols = ["RELIANCE"]

        engine = SandboxEngine(
            provider=Provider(),
            strategy=create_strategy("momentum", {"period": 2,
                                                  "threshold": 0.0}),
            broker=facade, repository=repo, journal=LearningJournal(repo),
            risk_limits=HardRiskLimits(max_position_value=10_000),
            timeframe="1m",
            instrument_keys={"RELIANCE": "NSE_EQ|INE002A01018"})
        engine.daily = None
        candle = Candle(timestamp=datetime.now(timezone.utc), open=100,
                        high=101, low=99, close=100, volume=1000,
                        symbol="RELIANCE")
        engine._history.append(candle)
        return engine, candle

    def test_engine_entry_never_reaches_broker_when_session_off(self, repo):
        settings = live_settings()
        manager = LiveSessionManager(repo)
        broker = MockLiveBroker()
        facade = self._facade(repo, manager, settings, broker)
        engine, candle = self._engine_with_facade(repo, facade)
        engine._submit("BUY", 1.0, 100.0, candle)   # refused inside gateway
        assert broker.calls == []
        assert engine.submitted == 0

    def test_engine_forwards_completed_candle_time_into_gateway(self, repo, armed):
        from datetime import timedelta

        from app.data.models import Candle

        settings, manager = armed
        broker = MockLiveBroker()
        facade = self._facade(repo, manager, settings, broker)
        engine, _candle = self._engine_with_facade(repo, facade)
        stale = Candle(timestamp=datetime.now(timezone.utc) - timedelta(minutes=5),
                       open=100, high=101, low=99, close=100, volume=1000,
                       symbol="RELIANCE")
        engine._submit("BUY", 1.0, 100.0, stale)
        assert broker.calls == []
        assert engine.submitted == 0

    def test_live_risk_only_candle_never_evaluates_strategy_entries(self,
                                                                   repo, armed):
        settings, manager = armed
        broker = MockLiveBroker()
        facade = self._facade(repo, manager, settings, broker)
        engine, candle = self._engine_with_facade(repo, facade)
        engine.provider.metrics = lambda: {
            "market_open": True, "stale_feed": False}
        strategy_calls = []
        engine.strategy.generate_signal = lambda history: (
            strategy_calls.append(len(history)) or type("Signal", (), {"value": "BUY"})())
        engine.process_candle(candle, risk_reducing_only=True)
        assert strategy_calls == []
        assert broker.calls == []
        assert engine.submitted == 0
        assert engine.signals[candle.symbol]["reason"] == "live risk-reducing-only mode"

    def test_engine_protective_exit_survives_the_emergency_stop(self, repo,
                                                                 armed):
        # FINAL-11/10 §12: the stop blocks new risk at the engine layer, but
        # a risk-reducing exit still flows through the gateway to the broker.
        settings, manager = armed
        broker = MockLiveBroker()
        broker.positions = [{"instrument_token": "NSE_EQ|INE002A01018",
                             "product": "I", "quantity": 10}]
        facade = self._facade(repo, manager, settings, broker)
        engine, candle = self._engine_with_facade(repo, facade)
        repo.set_engine_state("emergency_stop", {"stopped": True,
                                                 "reason": "breach"})
        engine._submit("BUY", 1.0, 100.0, candle)
        assert broker.calls == []                    # entry blocked by engine
        engine._submit("SELL", 1.0, 100.0, candle,
                       reason="PROTECTIVE_STOP_LOSS")
        assert len(broker.calls) == 1                # exit still escapes
        kinds = {e["kind"] for e in repo.list_system_events(limit=100)}
        assert "EXECUTION_STOPPED_SKIP" in kinds
        assert "EXECUTION_STOPPED_EXIT_ALLOWED" in kinds
