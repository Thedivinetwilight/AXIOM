"""V2 live hardening: host classification, the four-switch gate, live audit.

These tests are the regression barrier for the V1 defect found in the 2026-10-02
audit: ``api-hft.upstox.com`` (Upstox's LIVE order host) was listed as a SANDBOX
host and shipped as the sandbox default, so a live token placed real orders while
every label read "sandbox" / ``live_trading: false``.

Nothing here contacts a network: the broker is exercised with an httpx mock
transport, and Settings validation is pure.
"""

import json

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
                UpstoxLiveBroker("tok", INSTRUMENT, confirm="y",
                                 base_url=f"https://{host}")

    def test_live_broker_refuses_the_auth_host(self):
        with pytest.raises(ValueError):
            UpstoxLiveBroker("tok", INSTRUMENT, confirm="y",
                             base_url="https://api.upstox.com")

    def test_live_broker_labels_itself_truthfully(self):
        broker = UpstoxLiveBroker("tok", INSTRUMENT, confirm="y")
        assert broker.mode == "UPSTOX_LIVE"
        assert broker.LIVE is True
        assert "live=True" in repr(broker)
        assert "UPSTOX_LIVE" in repr(broker)
        assert broker.status_view()["live"] is True
        assert "REAL MONEY" in broker.status_view()["warning"]

    def test_sandbox_broker_keeps_stating_live_is_off(self):
        broker = UpstoxSandboxBroker("tok", INSTRUMENT)
        assert broker.LIVE is False
        assert "live=False" in repr(broker)
        assert broker.status_view()["live"] is False

    def test_live_broker_never_contacts_a_sandbox_host(self):
        transport = RecordingTransport()
        broker = UpstoxLiveBroker(
            "tok", INSTRUMENT, confirm="y", repository=None, transport=transport)
        broker.instrument_map["RELIANCE"] = INSTRUMENT
        broker.submit("BUY", 1, 0.0, intent_id="live1", symbol="RELIANCE")
        broker.reconcile()
        hosts = {httpx.URL(u).host for u in transport.urls}
        assert hosts <= {"api-hft.upstox.com", "api.upstox.com"}
        assert not (hosts & set(SANDBOX_ORDER_HOSTS))


class TestLiveAuditSeparation:
    def test_live_orders_land_in_live_tables_only(self, tmp_path):
        database = Database(f"sqlite:///{tmp_path}/live.db")
        repository = Repository(database)
        transport = RecordingTransport()
        broker = UpstoxLiveBroker(
            "tok", INSTRUMENT, confirm="y", repository=repository,
            transport=transport)
        broker.instrument_map["RELIANCE"] = INSTRUMENT
        broker.submit("BUY", 1, 0.0, intent_id="live-1", symbol="RELIANCE")
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
        broker = UpstoxLiveBroker("tok", INSTRUMENT, confirm="y",
                                  repository=repository, transport=transport)
        broker.instrument_map["RELIANCE"] = INSTRUMENT
        broker.submit("BUY", 1, 0.0, intent_id="live-2", symbol="RELIANCE")
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
        from app.api.server import create_app
        from fastapi.testclient import TestClient

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
        import logging
        import io

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
        from app import __main__ as cli

        # Settings itself refuses a live broker without a token, so the state is
        # simulated defensively: preflight must still never build the broker.
        import types

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
        from app.api.server import create_app
        from fastapi.testclient import TestClient

        settings = Settings(**dict(
            LIVE_KWARGS, api_token="t",
            database_url=f"sqlite:///{tmp_path}/html.db"))
        with TestClient(create_app(settings, data_dir=tmp_path)) as client:
            page = client.get("/dashboard",
                              headers={"Authorization": "Bearer t"}).text
        assert "LIVE: ENABLED — REAL MONEY" in page
        assert "UPSTOX_LIVE · LIVE — REAL MONEY" in page

    def test_paper_badge_says_simulated(self, tmp_path):
        from app.api.server import create_app
        from fastapi.testclient import TestClient

        settings = Settings(api_token="t",
                            database_url=f"sqlite:///{tmp_path}/html2.db")
        with TestClient(create_app(settings, data_dir=tmp_path)) as client:
            page = client.get("/dashboard",
                              headers={"Authorization": "Bearer t"}).text
        assert "LIVE: disabled" in page
        assert "SIMULATED (PAPER)" in page
