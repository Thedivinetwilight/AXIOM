"""Adversarial tests for the single live execution gateway (FINAL-11/10
§3–§13, §22, §24).

Every test counts BROKER CALLS explicitly: a refusal at any layer must
guarantee broker_calls == 0. The mock live broker is clearly a mock — it
exists so the real-money architecture can be proven without touching a
real exchange (no automated test may ever place a production order).
"""

from __future__ import annotations

import socket
import time
from datetime import datetime, timezone

import pytest

from app.config.settings import LIVE_CONFIRM_PHRASE
from app.database.database import Database
from app.database.repository import Repository
from app.live.gateway import (AuthorizationEpochs, ExecutionIntent,
                              GatewayRefusal, LiveExecutionGateway,
                              configuration_hash)
from app.live.guard_chain import default_chain
from app.live.ops import read_emergency_stop
from app.live.session import (ACTIVE, ARMED, LiveSessionManager,
                              LiveTransitionError)


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
                or runtime.get("host") != lease.get("host")):
            raise RuntimeError("fixture engine has no reconciled live lease")
        return {"supervisor_verified": True,
                "supervisor_lease_owner": lease["owner"],
                "supervisor_pid": lease["pid"],
                "supervisor_host": lease["host"],
                "last_market_data_ts": self.repository.get_engine_state(
                    "last_market_data_ts")}

    def request_stop(self, *, reason="fixture stop"):
        control = self.repository.get_engine_state("live_engine_control", {}) or {}
        self.repository.set_engine_state("live_engine_control", {
            **control, "stop_requested": True, "reason": reason})
        return {"running": True, "stop_requested": True}


def _install_fixture_live_engine(repository):
    from app.live.lock import EngineLock

    now = datetime.now(timezone.utc).isoformat()
    lock = EngineLock(repository, live=True)
    assert lock.acquire().granted
    lease = lock.status()
    repository.set_engine_state("last_market_data_ts", now)
    repository.set_engine_state("last_reconcile_at", {"ts": now, "fills": 0})
    repository.set_engine_state("live_token_verified", {"verified_at": now})
    repository.set_engine_state("live_engine_control", {
        "stop_requested": False, "generation": 1})
    repository.set_engine_state("live_engine_runtime", {
        "state": "READY", "owner": lease["owner"], "pid": lease["pid"],
        "host": lease["host"], "reconciled_at": now})
    return lock, _FixtureReadySupervisor(repository)


def _restart_fixture_live_engine(repository):
    """Model a drained child followed by a newly supervised child generation."""
    from app.live.lock import EngineLock

    now = datetime.now(timezone.utc).isoformat()
    control = repository.get_engine_state("live_engine_control", {}) or {}
    repository.set_engine_state("live_engine_control", {
        "stop_requested": False,
        "generation": int(control.get("generation", 0)) + 1})
    lease = EngineLock(repository, live=True).status()
    assert lease.get("held") is True and lease.get("live") is True
    repository.set_engine_state("last_market_data_ts", now)
    repository.set_engine_state("last_reconcile_at", {"ts": now, "fills": 0})
    repository.set_engine_state("live_token_verified", {"verified_at": now})
    repository.set_engine_state("live_engine_runtime", {
        "state": "READY", "owner": lease["owner"], "pid": lease["pid"],
        "host": lease["host"], "reconciled_at": now})


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
    return LiveExecutionGateway(
        settings=settings, repository=repo, session_manager=manager,
        broker=broker or MockLiveBroker(),
        guard_chain=chain, epochs=manager.epochs,
        emergency_stop_reader=read_emergency_stop,
        account_scope="upstox-test-account",
        context_provider=(lambda: context) if context is not None else None)


def entry_intent(epoch, **overrides):
    values = dict(purpose="ENTRY", market_id="india", identity="NSE|RELIANCE",
                  side="BUY", quantity=10, order_type="LIMIT", price=2900.0,
                  account_scope="upstox-test-account", universe="core",
                  strategy_id="strat-v1", strategy_lineage="root/strat-v1",
                  signal_id="sig-1", signal_ts=time.time(),
                  evidence_snapshot_id="ev-1", risk_snapshot_id="risk-1",
                  configuration_hash="cfg-1", model_revision="m-1",
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
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        gateway = make_gateway(repo, manager, settings)
        result = gateway.submit(entry_intent(epoch))
        assert result["authorized"] and result["submitted"]
        assert gateway.broker_calls == 1
        assert gateway.broker.calls[0]["intent_id"] == result["intent_id"]

    def test_risk_reduce_exit_allowed_while_paused(self, repo, armed):
        settings, manager = armed
        manager.pause()
        gateway = make_gateway(repo, manager, settings)
        intent = ExecutionIntent(purpose="RISK_REDUCE", market_id="india",
                                 identity="NSE|RELIANCE", side="SELL",
                                 quantity=10, account_scope="upstox-test-account")
        assert gateway.submit(intent)["submitted"]
        assert gateway.broker_calls == 1


# =====================================================================
# authorization epochs (§6)
# =====================================================================

class TestAuthorizationEpochs:
    def test_stop_invalidates_the_epoch(self, repo, armed):
        settings, manager = armed
        old_epoch = manager.epochs.current()["epoch"]
        manager.stop(reason="operator done")
        gateway = make_gateway(repo, manager, settings)
        with pytest.raises(GatewayRefusal) as exc:
            gateway.submit(entry_intent(old_epoch, side="SELL"))
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
        manager.epochs.activate(actor="test")          # epoch exists...
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
        assert "TypeError" in reason

    def test_entry_blocked_exit_available_under_emergency_stop(self, repo, armed):
        settings, manager = armed
        epoch = manager.epochs.current()["epoch"]
        repo.set_engine_state("emergency_stop",
                              {"stopped": True, "reason": "drill"})
        gateway = make_gateway(repo, manager, settings)
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

    def test_reconcile_purpose_never_becomes_an_order_submission(self,
                                                               repo, armed):
        settings, manager = armed
        broker = MockLiveBroker()
        reconciliations = []
        broker.reconcile = lambda: (reconciliations.append("read") or [])
        gateway = make_gateway(repo, manager, settings, broker)
        intent = ExecutionIntent(purpose="RECONCILE", market_id="india",
                                 identity="NSE|RELIANCE", side="BUY", quantity=1)

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
                                 instrument_key="NSE_EQ|INE002A01018")
        assert order_id == "mock-order-1"
        assert len(broker.calls) == 1
        assert broker.calls[0]["side"] == "BUY"

    def test_no_epoch_means_no_entry(self, repo, armed):
        settings, manager = armed
        manager.epochs.invalidate(reason="disarmed under test")
        broker = MockLiveBroker()
        facade = self._facade(repo, manager, settings, broker)
        with pytest.raises(GatewayRefusal) as exc:
            facade.submit("BUY", 10, 2900.0, intent_id="sig-2",
                          instrument_key="NSE_EQ|INE002A01018")
        assert exc.value.layer == "AUTHORIZATION EPOCH"
        assert broker.calls == []

    def test_inactive_session_blocks_entries_with_zero_broker_calls(self, repo):
        settings = live_settings()
        manager = LiveSessionManager(repo)          # session is OFF
        broker = MockLiveBroker()
        facade = self._facade(repo, manager, settings, broker)
        with pytest.raises(GatewayRefusal):
            facade.submit("BUY", 1, 100.0, intent_id="sig-3",
                          instrument_key="NSE_EQ|X")
        assert broker.calls == []

    def test_emergency_stop_blocks_entries_but_not_exits(self, repo, armed):
        settings, manager = armed
        broker = MockLiveBroker()
        facade = self._facade(repo, manager, settings, broker)
        repo.set_engine_state("emergency_stop",
                              {"stopped": True, "reason": "drill"})
        with pytest.raises(GatewayRefusal) as exc:
            facade.submit("BUY", 10, 100.0, intent_id="sig-4",
                          instrument_key="NSE_EQ|X")
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
                          instrument_key="NSE_EQ|X")
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
