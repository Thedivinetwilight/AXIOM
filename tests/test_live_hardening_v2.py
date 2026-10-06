"""V2 live hardening: host classification, the four-switch gate, live audit.

These tests are the regression barrier for the V1 defect found in the 2026-10-02
audit: ``api-hft.upstox.com`` (Upstox's LIVE order host) was listed as a SANDBOX
host and shipped as the sandbox default, so a live token placed real orders while
every label read "sandbox" / ``live_trading: false``.

Protocol tests patch ``httpx.request`` at the test boundary to redirect
requests to an in-process exchange; the production live broker is not given a
caller-owned transport. Settings validation is pure.
"""

import json
import time
from unittest.mock import patch

import httpx
import pytest

from app.brokers.live import UpstoxLiveBroker
from app.brokers.sandbox import UpstoxSandboxBroker
from app.config.hosts import (
    DEFAULT_LIVE_BASE_URL,
    LIVE_ORDER_HOSTS,
    SANDBOX_ORDER_HOSTS,
    host_of,
)
from app.config.settings import LIVE_CONFIRM_PHRASE, Settings
from app.database.database import Database
from app.database.repository import Repository

INSTRUMENT = "NSE_EQ|INE002A01018"

LIVE_KWARGS = {
    "broker": "UPSTOX_LIVE",
    "mode": "LIVE",
    "live_trading": True,
    "live_confirm": LIVE_CONFIRM_PHRASE,
    "upstox_live_access_token": "live-test-token",
    "max_position_value": 10_000.0,
    "max_open_positions": 1,
    "max_daily_loss": 500.0,
    "max_trades_per_day": 3,
}


class RecordingTransport(httpx.BaseTransport):
    """Records URLs; fakes the documented place/history endpoints."""

    def __init__(self):
        self.urls = []

    def handle_request(self, request):
        self.urls.append(str(request.url))
        if request.method == "POST" and request.url.path == "/v3/order/place":
            return httpx.Response(200, json={"status": "success",
                                             "data": {"order_ids": ["L1"]}})
        if request.method == "GET" and request.url.path == "/v2/order/history":
            return httpx.Response(200, json={"status": "success", "data": [
                {"status": "complete", "filled_quantity": 1, "average_price": 100.0,
                 "order_timestamp": "t", "status_message": ""}]})
        return httpx.Response(500, json={"errors": [{"errorCode": "X"}]})


def _httpx_request_via(transport):
    """Build a test-only ``httpx.request`` replacement around a fake transport."""
    def request(method, url, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        return transport.handle_request(httpx.Request(method, url, **kwargs))
    return request


def _through_test_transport(transport, function, *args, **kwargs):
    with patch("httpx.request", side_effect=_httpx_request_via(transport)):
        return function(*args, **kwargs)


def _mock_protocol_authorization(broker, transport):
    """Route protocol-only test calls through a real gateway submit dispatch.

    The policy-heavy authorization readers are stubbed to keep these tests
    focused on wire behavior; tests in test_axiom_execution_gateway exercise
    the complete live-control checks. The live broker still receives the
    exact gateway-created dispatch context and uses only the default HTTP core.
    """
    from types import SimpleNamespace

    from app.config.settings import Settings
    from app.database.database import Database
    from app.database.repository import Repository
    from app.live.gateway import ExecutionIntent, LiveExecutionGateway, configuration_hash

    database = None
    repository = broker.repository
    if repository is None:
        database = Database("sqlite:///:memory:")
        repository = Repository(database)
        broker.repository = repository
    repository.set_engine_state("live_engine_control", {
        "stop_requested": False, "generation": 1})

    settings = Settings(**LIVE_KWARGS)
    sessions = SimpleNamespace(current=lambda: SimpleNamespace(state="ACTIVE"))

    class AcceptCanary:
        @staticmethod
        def reserve_submission(**_kwargs):
            return SimpleNamespace(allowed=True, reason="test-only protocol allowance")

    gateway = LiveExecutionGateway(
        settings=settings, repository=repository, session_manager=sessions,
        broker=broker, epochs=SimpleNamespace(
            validate=lambda _epoch, **_kwargs: (True, "protocol-only test")),
        emergency_stop_reader=lambda: False,
        live_ops=SimpleNamespace(canary=AcceptCanary()),
    )
    # Bypass policy IO only. The production dispatcher/context and the broker's
    # final mutation authorization remain in the call path.
    gateway._final_revalidate = lambda _intent: {}
    gateway._require_live_lease = lambda _intent: {"owner": "protocol-test"}
    gateway._require_entry_readiness = lambda _intent, _lease: {}
    gateway._emergency_stop_state = lambda _intent: (False, "protocol test")
    gateway._require_broker_identity = lambda _intent: None
    gateway._authorize_live_entry_ops = lambda _intent: None
    gateway._authorize_position_reduction = lambda _intent: None
    gateway._evaluate_entry_guards = lambda *_args: None

    def make_intent(purpose, side, quantity, price, order_type, intent_id,
                    identity, *, cancel_order_id=None):
        return ExecutionIntent(
            purpose=purpose, market_id="test-market", identity=identity,
            side=side, quantity=float(quantity), order_type=order_type,
            price=float(price), account_scope=broker.account_scope,
            universe="protocol-test", strategy_id="protocol-test",
            strategy_lineage="protocol-test", signal_id=str(intent_id),
            signal_ts=time.time(), configuration_hash=configuration_hash(settings),
            authorization_epoch=1, build_identity="test-build",
            cancel_order_id=cancel_order_id, intent_id=str(intent_id))

    class ProtocolHarness:
        def __init__(self):
            self._broker = broker
            self.gateway = gateway
            self._database = database

        def submit(self, side, quantity, price, *, order_type="MARKET", intent_id=None,
                   symbol=None, instrument_key=None, **_extra):
            identity = broker.resolve_instrument_key(symbol, instrument_key)
            purpose = "ENTRY" if side == "BUY" else "EXIT"
            intent_id = str(intent_id or "protocol-intent")
            intent = make_intent(
                purpose, side, quantity, price, order_type, intent_id, identity)
            result = _through_test_transport(
                transport, gateway.submit, intent)
            return str(result.get("order_id", ""))

        def cancel(self, intent_id, *args, **kwargs):
            state = broker.orders[str(intent_id)]
            identity = (state.symbol if isinstance(state.symbol, str)
                        and "|" in state.symbol else
                        broker.resolve_instrument_key(state.symbol, None))
            intent = make_intent(
                "CANCEL", state.side, state.quantity, state.price, "MARKET",
                "protocol-cancel:" + str(intent_id), identity,
                cancel_order_id=str(intent_id))
            result = _through_test_transport(transport, gateway.submit, intent)
            return result.get("cancelled") or {"status": "UNKNOWN"}

        def modify(self, *args, **kwargs):
            # There is no gateway-authorized live modify intent yet.
            return broker.modify(*args, **kwargs)

        def reconcile(self, *args, **kwargs):
            return _through_test_transport(transport, broker.reconcile, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(broker, name)

    return ProtocolHarness()


class TestHostClassification:
    def test_live_and_sandbox_hosts_are_disjoint(self):
        assert not (LIVE_ORDER_HOSTS & SANDBOX_ORDER_HOSTS)

    def test_sandbox_default_is_not_the_live_order_host(self):
        assert host_of(DEFAULT_LIVE_BASE_URL) in LIVE_ORDER_HOSTS
        assert _default_sandbox_host() in SANDBOX_ORDER_HOSTS

    def test_settings_default_never_points_at_a_live_host(self):
        settings = Settings()
        assert host_of(settings.upstox_sandbox_base_url) in SANDBOX_ORDER_HOSTS

    def test_sandbox_settings_refuse_a_live_host(self):
        with pytest.raises(ValueError, match="not a sandbox host"):
            Settings(broker="UPSTOX_SANDBOX", upstox_sandbox_access_token="t",
                     upstox_sandbox_base_url="https://api-hft.upstox.com")


def _default_sandbox_host() -> str:
    return host_of(Settings(broker="UPSTOX_SANDBOX",
                            upstox_sandbox_access_token="t").upstox_sandbox_base_url)


class TestFourSwitchGate:
    """Each switch alone must be insufficient; only all four together open."""

    def test_broker_alone_is_insufficient(self):
        with pytest.raises(ValueError, match="requires ALL of"):
            Settings(broker="UPSTOX_LIVE")

    def test_broker_plus_mode_is_insufficient(self):
        kwargs = dict(LIVE_KWARGS, live_trading=False)
        with pytest.raises(ValueError, match="LIVE_TRADING=true"):
            Settings(**kwargs)

    def test_wrong_confirm_phrase_is_refused(self):
        kwargs = dict(LIVE_KWARGS, live_confirm="yes")
        with pytest.raises(ValueError, match="LIVE_CONFIRM"):
            Settings(**kwargs)

    def test_missing_token_is_refused(self):
        kwargs = dict(LIVE_KWARGS, upstox_live_access_token="")
        with pytest.raises(ValueError, match="UPSTOX_LIVE_ACCESS_TOKEN"):
            Settings(**kwargs)

    @pytest.mark.parametrize("field", [
        "max_position_value", "max_daily_loss", "max_open_positions",
        "max_trades_per_day",
    ])
    def test_zero_risk_limits_are_refused(self, field):
        """0 means unlimited elsewhere; in live mode it must fail closed."""
        kwargs = dict(LIVE_KWARGS, **{field: 0})
        with pytest.raises(ValueError, match=f"{field.upper()}>0"):
            Settings(**kwargs)

    def test_all_four_switches_together_are_accepted(self):
        settings = Settings(**LIVE_KWARGS)
        assert settings.mode.upper() == "LIVE"
        assert settings.live_trading is True
        assert settings.broker.upper() == "UPSTOX_LIVE"

    def test_live_base_url_must_be_the_documented_live_host(self):
        kwargs = dict(LIVE_KWARGS, upstox_live_base_url="https://api-sandbox.upstox.com")
        with pytest.raises(ValueError, match="not the documented live order host"):
            Settings(**kwargs)

    @pytest.mark.parametrize(("field", "value", "message"), [
        ("upstox_live_base_url", "http://api-hft.upstox.com",
         "HTTPS root URL"),
        ("upstox_live_order_history_url", "https://attacker.example/v2/order/history",
         "documented HTTPS Upstox order-history endpoint"),
    ])
    def test_live_broker_urls_are_tls_and_host_pinned(self, field, value, message):
        kwargs = dict(LIVE_KWARGS, **{field: value})
        with pytest.raises(ValueError, match=message):
            Settings(**kwargs)

    def test_sandbox_order_history_url_cannot_leak_the_token(self):
        with pytest.raises(ValueError, match="documented HTTPS Upstox order-history"):
            Settings(broker="UPSTOX_SANDBOX", upstox_sandbox_access_token="test",
                     upstox_sandbox_order_history_url=(
                         "https://attacker.example/v2/order/history"))

    def test_legacy_broker_live_alias_still_raises(self):
        with pytest.raises(ValueError, match="LIVE trading is not enabled"):
            Settings(broker="LIVE")


class TestLiveBrokerIdentity:
    def test_live_broker_requires_explicit_confirmation(self):
        with pytest.raises(ValueError, match="confirm"):
            UpstoxLiveBroker("tok", INSTRUMENT)  # no confirm= argument

    def test_live_broker_refuses_sandbox_hosts(self):
        for host in sorted(SANDBOX_ORDER_HOSTS):
            with pytest.raises(ValueError, match="not permitted for UPSTOX_LIVE"):
                UpstoxLiveBroker("tok", INSTRUMENT, confirm=LIVE_CONFIRM_PHRASE,
                                 base_url=f"https://{host}")

    def test_live_broker_refuses_the_auth_host(self):
        with pytest.raises(ValueError):
            UpstoxLiveBroker("tok", INSTRUMENT, confirm=LIVE_CONFIRM_PHRASE,
                             base_url="https://api.upstox.com")

    def test_live_broker_labels_itself_truthfully(self):
        broker = UpstoxLiveBroker("tok", INSTRUMENT, confirm=LIVE_CONFIRM_PHRASE)
        assert broker.mode == "UPSTOX_LIVE"
        assert broker.LIVE is True
        assert "live=True" in repr(broker)
        assert "UPSTOX_LIVE" in repr(broker)
        assert broker.status_view()["live"] is True
        assert "REAL MONEY" in broker.status_view()["warning"]

    def test_production_live_broker_writes_require_gateway_capability(self, monkeypatch):
        from app.brokers.sandbox import SandboxRequestError
        from app.live.gateway import (
            ExecutionIntent,
            GatewayRefusal,
            LiveExecutionGateway,
            _gateway_capability,
            _register_gateway_capability,
        )

        broker = UpstoxLiveBroker(
            "live-test-token", INSTRUMENT, confirm=LIVE_CONFIRM_PHRASE)
        gateway = LiveExecutionGateway(
            settings=object(), repository=None, session_manager=None, broker=broker)
        registered = _gateway_capability(gateway)
        with pytest.raises(TypeError):
            _register_gateway_capability(gateway, object())
        with pytest.raises(RuntimeError, match="already registered"):
            _register_gateway_capability(gateway)
        assert _gateway_capability(gateway) is registered
        assert not hasattr(gateway, "_broker_capability")
        assert not hasattr(broker, "_gateway_write_scope")
        assert not hasattr(broker, "_gateway_write_required")
        assert not hasattr(broker, "_gateway_capability")
        assert not hasattr(broker, "_bind_gateway_capability")
        with pytest.raises(SandboxRequestError,
                           match="only through the execution gateway"):
            broker._begin_gateway_write(object(), None)
        with pytest.raises(SandboxRequestError, match="only through the execution gateway"):
            broker.submit("BUY", 1, 100.0, intent_id="direct-live-submit",
                          symbol="RELIANCE")
        # Even a caller who reaches the private capability reader and supplies
        # a structurally valid intent cannot call the broker directly: the
        # gateway's active submit-dispatch context is required at the HTTP edge.
        attempt_log = []
        monkeypatch.setattr(httpx, "request",
                            lambda *args, **kwargs: attempt_log.append(args))
        direct_intent = ExecutionIntent(
            purpose="ENTRY", market_id="test-market", identity=INSTRUMENT,
            side="BUY", quantity=1, price=100.0,
            account_scope=broker.account_scope, strategy_id="direct-test",
            signal_id="direct-signal", signal_ts=time.time(),
            configuration_hash="direct-test-config", authorization_epoch=1,
            intent_id="direct-valid-intent")
        with pytest.raises(GatewayRefusal, match="active submit dispatch"):
            broker.submit(
                "BUY", 1, 100.0, intent_id=direct_intent.intent_id,
                symbol=INSTRUMENT, instrument_key=INSTRUMENT,
                _gateway_capability=registered, _gateway_intent=direct_intent)
        assert attempt_log == []
        with pytest.raises(SandboxRequestError, match="only through the execution gateway"):
            broker._request("POST", "https://api-hft.upstox.com/v3/order/place",
                            json={"quantity": 1})

    def test_protocol_harness_uses_the_default_live_http_core(self):
        transport = RecordingTransport()
        broker = UpstoxLiveBroker(
            "live-test-token", INSTRUMENT, confirm=LIVE_CONFIRM_PHRASE)
        broker.instrument_map["RELIANCE"] = INSTRUMENT
        broker = _mock_protocol_authorization(broker, transport)
        broker.submit("BUY", 1, 100.0, intent_id="reentrant-outer",
                      symbol="RELIANCE")
        assert len(transport.urls) == 1

    def test_live_mutation_http_methods_require_gateway_before_network(self, monkeypatch):
        from app.brokers.sandbox import SandboxRequestError, UpstoxSandboxBroker

        transport = RecordingTransport()
        broker = UpstoxLiveBroker(
            "live-test-token", INSTRUMENT, confirm=LIVE_CONFIRM_PHRASE)
        attempted = []
        monkeypatch.setattr(httpx, "request", lambda *args, **kwargs: attempted.append(args))
        assert broker._transport is None
        for method in ("POST", "PUT", "DELETE", "PATCH", "PROPFIND"):
            with pytest.raises(SandboxRequestError, match="only through the execution gateway"):
                broker._request(method, "https://api-hft.upstox.com/v3/order/place")
            # Calling the base class directly must still dispatch the broker's
            # transport authorization hook before reaching httpx.request.
            with pytest.raises(SandboxRequestError, match="only through the execution gateway"):
                UpstoxSandboxBroker._request(
                    broker, method, "https://api-hft.upstox.com/v3/order/place")
        with pytest.raises(SandboxRequestError, match="only through the execution gateway"):
            broker.submit("BUY", 1, 100.0, intent_id="direct-submit",
                          instrument_key=INSTRUMENT)
        with pytest.raises(SandboxRequestError, match="only through the execution gateway"):
            broker.cancel("direct-cancel")
        with pytest.raises(SandboxRequestError, match="only through the execution gateway"):
            broker.modify("direct-modify", quantity=1)
        with pytest.raises(SandboxRequestError, match="configured Upstox host"):
            broker._request("GET", "https://attacker.example/collect")
        assert attempted == []
        assert transport.urls == []

    def test_caller_owned_transports_are_not_retained_by_the_live_broker(self):
        fake = RecordingTransport()
        with pytest.raises(ValueError, match="refuses caller-owned HTTP transports"):
            UpstoxLiveBroker(
                "live-test-token", INSTRUMENT, confirm=LIVE_CONFIRM_PHRASE,
                transport=fake)
        with pytest.raises(ValueError, match="refuses caller-owned HTTP transports"):
            UpstoxLiveBroker(
                "live-test-token", INSTRUMENT, confirm=LIVE_CONFIRM_PHRASE,
                transport=httpx.HTTPTransport())
        # Construction fails before storing a caller-owned reference or sending
        # a request. Live requests use only the broker's checked HTTP core.
        assert fake.urls == []

    def test_sandbox_raw_mutation_cannot_target_the_live_order_host(self):
        from app.brokers.sandbox import SandboxRequestError

        transport = RecordingTransport()
        broker = UpstoxSandboxBroker("tok", INSTRUMENT, transport=transport)
        with pytest.raises(SandboxRequestError, match="configured Upstox host"):
            broker._request("POST", "https://api-hft.upstox.com/v3/order/place")
        assert transport.urls == []

    def test_sandbox_broker_keeps_stating_live_is_off(self):
        broker = UpstoxSandboxBroker("tok", INSTRUMENT)
        assert broker.LIVE is False
        assert "live=False" in repr(broker)
        assert broker.status_view()["live"] is False

    def test_live_broker_never_contacts_a_sandbox_host(self):
        transport = RecordingTransport()
        broker = _mock_protocol_authorization(UpstoxLiveBroker(
            "tok", INSTRUMENT, confirm=LIVE_CONFIRM_PHRASE, repository=None), transport)
        broker.instrument_map["RELIANCE"] = INSTRUMENT
        broker.submit("BUY", 1, 100.0, intent_id="live1", symbol="RELIANCE")
        broker.reconcile()
        hosts = {httpx.URL(u).host for u in transport.urls}
        assert hosts <= {"api-hft.upstox.com", "api.upstox.com"}
        assert not (hosts & set(SANDBOX_ORDER_HOSTS))


class TestLiveAuditSeparation:
    def test_live_orders_land_in_live_tables_only(self, tmp_path):
        database = Database(f"sqlite:///{tmp_path}/live.db")
        repository = Repository(database)
        transport = RecordingTransport()
        broker = _mock_protocol_authorization(UpstoxLiveBroker(
            "tok", INSTRUMENT, confirm=LIVE_CONFIRM_PHRASE, repository=repository), transport)
        broker.instrument_map["RELIANCE"] = INSTRUMENT
        broker.submit("BUY", 1, 100.0, intent_id="live-1", symbol="RELIANCE")
        broker.reconcile()

        live_orders = repository.list_live_orders()
        sandbox_orders = repository.list_sandbox_orders()
        assert [order["intent_id"] for order in live_orders] == ["live-1"]
        assert live_orders[0]["id"].startswith("live-")
        assert sandbox_orders == [], "a real order must never be recorded as sandbox"
        with database.transaction() as db:
            live_count = db.execute("SELECT COUNT(*) FROM live_transitions").fetchone()[0]
            sandbox_count = db.execute(
                "SELECT COUNT(*) FROM sandbox_transitions").fetchone()[0]
        assert live_count >= 1 and sandbox_count == 0
        database.close()

    def test_sandbox_orders_stay_in_sandbox_tables(self, tmp_path):
        database = Database(f"sqlite:///{tmp_path}/both.db")
        repository = Repository(database)
        transport = RecordingTransport()
        broker = UpstoxSandboxBroker("tok", INSTRUMENT, repository=repository,
                                     transport=transport)
        broker.instrument_map["RELIANCE"] = INSTRUMENT
        broker.instrument_map["RELIANCE"] = INSTRUMENT
        broker.submit("BUY", 1, 0.0, intent_id="sbox-1", symbol="RELIANCE")
        assert repository.list_live_orders() == []
        sandbox_orders = repository.list_sandbox_orders()
        assert [o["intent_id"] for o in sandbox_orders] == ["sbox-1"]
        assert sandbox_orders[0]["id"].startswith("sbox-")
        database.close()

    def test_ledger_recovery_is_mode_scoped(self, tmp_path):
        from app.brokers.sandbox import SandboxLedger
        from app.engine.state import recover_ledger_state

        database = Database(f"sqlite:///{tmp_path}/ledger.db")
        repository = Repository(database)
        transport = RecordingTransport()
        broker = _mock_protocol_authorization(UpstoxLiveBroker(
            "tok", INSTRUMENT, confirm=LIVE_CONFIRM_PHRASE,
            repository=repository), transport)
        broker.instrument_map["RELIANCE"] = INSTRUMENT
        broker.submit("BUY", 1, 100.0, intent_id="live-2", symbol="RELIANCE")
        broker.reconcile()

        sandbox_ledger = SandboxLedger(100_000.0)
        report = recover_ledger_state(sandbox_ledger, repository, live=False)
        assert report["increments_replayed"] == 0, \
            "sandbox replay must not see live fills"

        live_ledger = SandboxLedger(100_000.0)
        report = recover_ledger_state(live_ledger, repository, live=True)
        assert report["increments_replayed"] == 1
        assert live_ledger.position_quantity == 1
        database.close()


class TestLiveReportingTruthfulness:
    def _app(self, tmp_path, settings):
        from fastapi.testclient import TestClient

        from app.api.server import create_app

        return TestClient(create_app(settings, data_dir=tmp_path))

    def test_health_reports_live_when_live(self, tmp_path):
        settings = Settings(**dict(
            LIVE_KWARGS, api_token="t",
            database_url=f"sqlite:///{tmp_path}/h.db"))
        with self._app(tmp_path, settings) as client:
            health = client.get("/health").json()
        assert health["mode"] == "LIVE"
        assert health["live_trading"] is True
        assert health["broker"] == "UPSTOX_LIVE"
        assert health["real_order_routing"] == "LIVE_REAL_MONEY"
        assert health["warning"] == "REAL MONEY ROUTING ACTIVE"

    def test_health_reports_paper_by_default(self, tmp_path):
        settings = Settings(api_token="t",
                            database_url=f"sqlite:///{tmp_path}/p.db")
        with self._app(tmp_path, settings) as client:
            health = client.get("/health").json()
        assert health["mode"] == "PAPER"
        assert health["live_trading"] is False
        assert health["real_order_routing"] == "SIMULATED_LOCAL_PAPER"

    def test_health_reports_sandbox_orders_not_paper(self, tmp_path):
        settings = Settings(broker="UPSTOX_SANDBOX",
                            upstox_sandbox_access_token="t", api_token="t",
                            database_url=f"sqlite:///{tmp_path}/s.db")
        with self._app(tmp_path, settings) as client:
            health = client.get("/health").json()
        assert health["mode"] == "PAPER"
        assert health["live_trading"] is False
        assert health["real_order_routing"] == "SANDBOX_ORDERS_ONLY"

    def test_dashboard_reports_live_when_live(self, tmp_path):
        settings = Settings(**dict(
            LIVE_KWARGS, api_token="t",
            database_url=f"sqlite:///{tmp_path}/d.db"))
        with self._app(tmp_path, settings) as client:
            data = client.get("/dashboard/data",
                              headers={"Authorization": "Bearer t"}).json()
        assert data["live_trading"] is True
        assert data["mode"] == "LIVE"
        assert "REAL MONEY" in data["live_enabled"]
        assert "REAL MONEY" in data["display"]

    def test_weekly_report_states_live_when_live(self, tmp_path):
        from app.reporting.weekly import WeeklyReport

        settings = Settings(**dict(
            LIVE_KWARGS, database_url=f"sqlite:///{tmp_path}/w.db"))
        database = Database(settings.database_url)
        repository = Repository(database)
        report = WeeklyReport(repository, settings=settings)
        data = report.assemble()
        assert data["live"] == "ENABLED — REAL MONEY"
        assert "ENABLED — REAL MONEY" in report.markdown()
        database.close()


class TestSchemaIsAdditiveForLive:
    def test_live_tables_exist_and_are_empty_on_a_fresh_database(self, tmp_path):
        database = Database(f"sqlite:///{tmp_path}/schema.db")
        with database.transaction() as db:
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            versions = [r[0] for r in db.execute(
                "SELECT version FROM schema_version ORDER BY version").fetchall()]
        assert {"live_orders", "live_transitions"} <= tables
        assert 8 in versions
        repository = Repository(database)
        assert repository.list_live_orders() == []
        assert json.loads(json.dumps(repository.list_live_orders())) == []
        database.close()


class TestSandboxEventHandlerNamesUnchanged:
    """Sandbox log/audit event names stay exactly as before (no label churn)."""

    def test_sandbox_submit_logs_the_sandbox_event_name(self, tmp_path):
        import io
        import logging

        from app.brokers.sandbox import logger as sandbox_logger

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        sandbox_logger.addHandler(handler)
        sandbox_logger.setLevel(logging.DEBUG)
        try:
            broker = UpstoxSandboxBroker("tok", INSTRUMENT, transport=RecordingTransport())
            broker.instrument_map["RELIANCE"] = INSTRUMENT
            broker.submit("BUY", 1, 0.0, intent_id="s1", symbol="RELIANCE")
        finally:
            sandbox_logger.removeHandler(handler)
        assert "SANDBOX_ORDER_SUBMITTED" in stream.getvalue()


class TestCliGates:
    """The CLI refuses to start a live engine unless Settings accepted it."""

    def test_engine_refuses_local_paper(self, monkeypatch, capsys):
        from app import __main__ as cli

        monkeypatch.setattr(cli, "_load_settings", lambda: Settings())
        code = cli.cmd_engine(type("A", (), {"max_candles": 1})())
        assert code == 2
        assert "BROKER=UPSTOX_SANDBOX or BROKER=UPSTOX_LIVE" in capsys.readouterr().out

    def test_preflight_refuses_a_non_live_broker(self, monkeypatch, capsys):
        from app import __main__ as cli

        monkeypatch.setattr(cli, "_load_settings", lambda: Settings())
        code = cli.cmd_live_preflight(type("A", (), {"instrument": INSTRUMENT})())
        payload = json.loads(capsys.readouterr().out)
        assert code == 2
        assert payload["ok"] is False and payload["performed"] is False
        assert "BROKER is not UPSTOX_LIVE" in payload["reason"]

    def test_preflight_reports_a_rejected_configuration(self, monkeypatch, capsys):
        from app import __main__ as cli

        def _reject():
            raise ValueError("BROKER=UPSTOX_LIVE (real money) requires ALL of: MODE=LIVE")

        monkeypatch.setattr(cli, "_load_settings", _reject)
        code = cli.cmd_live_preflight(type("A", (), {"instrument": INSTRUMENT})())
        payload = json.loads(capsys.readouterr().out)
        assert code == 2
        assert payload["ok"] is False and payload["performed"] is False
        assert "configuration rejected" in payload["reason"]

    def test_preflight_classifies_a_good_credential(self, monkeypatch, capsys):
        from app import __main__ as cli

        monkeypatch.setattr(cli, "_load_settings", lambda: Settings(**LIVE_KWARGS))
        monkeypatch.setattr(
            UpstoxLiveBroker, "_request",
            lambda self, method, url, **kw: httpx.Response(
                400, json={"errors": [{"errorCode": "UDAPI100010"}]}))
        code = cli.cmd_live_preflight(type("A", (), {"instrument": INSTRUMENT})())
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["credential_accepted"] is True
        assert payload["order_host_is_documented_live"] is True
        assert payload["hard_risk_limits"]["max_position_value"] == 10_000.0

    def test_preflight_rejects_a_bad_credential(self, monkeypatch, capsys):
        from app import __main__ as cli

        monkeypatch.setattr(cli, "_load_settings", lambda: Settings(**LIVE_KWARGS))
        monkeypatch.setattr(
            UpstoxLiveBroker, "_request",
            lambda self, method, url, **kw: httpx.Response(
                401, json={"errors": [{"errorCode": "UDAPI100050"}]}))
        code = cli.cmd_live_preflight(type("A", (), {"instrument": INSTRUMENT})())
        payload = json.loads(capsys.readouterr().out)
        assert code == 1 and payload["credential_accepted"] is False

    def test_preflight_never_constructs_a_broker_without_a_token(
            self, monkeypatch, capsys):
        # Settings itself refuses a live broker without a token, so the state is
        # simulated defensively: preflight must still never build the broker.
        import types

        from app import __main__ as cli

        fake = types.SimpleNamespace(
            broker="UPSTOX_LIVE", mode="LIVE", live_trading=True,
            upstox_live_base_url="https://api-hft.upstox.com",
            upstox_live_order_history_url="https://api.upstox.com/v2/order/history",
            database_url="sqlite:///x.db", max_position_value=1.0,
            max_open_positions=1, max_daily_loss=1.0, max_trades_per_day=1,
            live_confirm=LIVE_CONFIRM_PHRASE, upstox_live_access_token="")
        monkeypatch.setattr(cli, "_load_settings", lambda: fake)
        code = cli.cmd_live_preflight(type("A", (), {"instrument": INSTRUMENT})())
        payload = json.loads(capsys.readouterr().out)
        assert code == 3 and payload["credential_checked"] is False


class TestLiveDashboardHtml:
    def test_live_badge_is_rendered_server_side(self, tmp_path):
        """Truthful mode must be visible even with JavaScript disabled."""
        from fastapi.testclient import TestClient

        from app.api.server import create_app

        settings = Settings(**dict(
            LIVE_KWARGS, api_token="t",
            database_url=f"sqlite:///{tmp_path}/html.db"))
        with TestClient(create_app(settings, data_dir=tmp_path)) as client:
            page = client.get("/dashboard",
                              headers={"Authorization": "Bearer t"}).text
        assert "LIVE: ENABLED — REAL MONEY" in page
        assert "UPSTOX_LIVE · LIVE — REAL MONEY" in page

    def test_paper_badge_says_simulated(self, tmp_path):
        from fastapi.testclient import TestClient

        from app.api.server import create_app

        settings = Settings(api_token="t",
                            database_url=f"sqlite:///{tmp_path}/html2.db")
        with TestClient(create_app(settings, data_dir=tmp_path)) as client:
            page = client.get("/dashboard",
                              headers={"Authorization": "Bearer t"}).text
        assert "LIVE: disabled" in page
        assert "SIMULATED (PAPER)" in page
