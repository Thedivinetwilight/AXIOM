"""END-TO-END: the operator's LIVE switch, from a cold PAPER installation to a
supervised live session, against a deterministic mock exchange.

WHAT IS PROVEN HERE
    * A fresh installation is PAPER and the live switch is inert.
    * Clicking through the whole wizard (connect -> limits -> readiness ->
      confirm) sends NOTHING to the broker: the exchange log stays empty.
    * The final explicit confirmation (the exact phrase) is the ONLY thing
      that starts the supervised live engine and mints an authorization
      epoch. A wrong phrase changes nothing.
    * Only after that confirmation can a live order reach the broker, and it
      then travels the real gateway -> real guard chain -> real hard limits
      -> the real live broker, into the isolated live tables.
    * The switch-off flow stops the session, invalidates the epoch and
      returns the installation to PAPER; a later entry is refused.

NO REAL ACCOUNT, NO REAL CREDENTIAL, NO REAL ORDER
    The OAuth exchange is a fixture, the credential verifier is a fixture,
    the broker is the in-process ``MockUpstoxExchange`` (an httpx mock
    transport: no socket is opened), the database is a throwaway SQLite file
    in a temporary directory and the supervised child is an in-process fake
    that never sends anything.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import LIVE_CONFIRM_PHRASE, Settings
from app.live.activation import LiveActivationService

FAKE_LIVE_TOKEN = "fake-live-access-token-for-tests"
FAKE_SECRET = "fake-client-secret-for-tests"
KEY = "NSE_EQ|INE002A01018"


# --------------------------------------------------------------- fixtures --

@pytest.fixture()
def paper(tmp_path, monkeypatch):
    """A disposable paper installation with its own home and database."""
    home = tmp_path / "ArenaHome"
    for key in ("DATABASE_URL", "DATA_DIR", "API_TOKEN", "BROKER", "MODE",
                "MARKET_DATA_SOURCE", "LIVE_TRADING", "LIVE_CONFIRM",
                "UPSTOX_LIVE_ACCESS_TOKEN", "MAX_POSITION_VALUE",
                "MAX_OPEN_POSITIONS", "MAX_DAILY_LOSS", "MAX_TRADES_PER_DAY",
                "DATA_SOURCE", "DEFAULT_TIMEFRAME", "MARKET_DATA_TIMEFRAME",
                "MARKET_DATA_ACCESS_TOKEN", "UPSTOX_CLIENT_ID",
                "UPSTOX_CLIENT_SECRET", "UPSTOX_REDIRECT_URI"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ARENA_HOME", str(home))
    saved = dict(os.environ)
    app = create_app(Settings.from_env())
    try:
        with TestClient(app) as client:
            app.state.workstation.live_supervisor.startup_timeout = 2.0
            prepared = client.post("/api/wizard/prepare-paper",
                                    json={"data_source": "replay"}).json()
            token = prepared["token_once"]
            yield client, token, app, home
    finally:
        os.environ.clear()
        os.environ.update(saved)


def headers(token):
    return {"Authorization": "Bearer " + token}


def install_fake_engine(app):
    """Replace the supervised child with a deterministic in-process fake.

    The fake takes the live lease and publishes the runtime evidence the
    supervisor insists on. It never sends an order: the order in this file is
    placed by the test itself, through the real gateway, after the explicit
    confirmation.
    """
    from app.live.lock import EngineLock

    repository = app.state.workstation.repository

    def popen(argv, **kwargs):
        lock = EngineLock(repository, live=True)
        assert lock.acquire().granted
        now = datetime.now(timezone.utc).isoformat()
        repository.set_engine_state("last_market_data_ts", now)
        repository.set_engine_state("last_reconcile_at", {"ts": now, "fills": 0})
        repository.set_engine_state("live_reconcile_status", {
            "ok": True, "ts": now, "fills": 0})
        control = repository.get_engine_state("live_engine_control", {}) or {}
        repository.set_engine_state("live_engine_runtime", {
            "state": "READY", "owner": lock.status()["owner"],
            "pid": lock.status()["pid"], "host": lock.status()["host"],
            "generation": control.get("generation"),
            "reconciliation_ok": True, "reconciled_at": now})
        return FakeProcess()

    app.state.workstation.live_supervisor._popen_factory = popen
    app.state.workstation.live_supervisor._argv_factory = lambda: ["fixture-engine"]


class FakeProcess:
    def __init__(self):
        self.pid = int(os.getpid())
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def wait(self, timeout=None):
        import subprocess

        raise subprocess.TimeoutExpired("fixture-live-engine", timeout)

    def terminate(self):  # pragma: no cover - forbidden path in this test
        self.terminated = True

    def kill(self):  # pragma: no cover - forbidden path in this test
        self.killed = True


def install_fake_broker_identity(app, monkeypatch):
    """Make the OAuth exchange and the credential check deterministic."""
    from app.data.upstox_auth import CredentialCheck

    def _exchange(*, code, client_id, client_secret, redirect_uri):
        if code == "refused-code":
            raise RuntimeError("the broker said no")
        return FAKE_LIVE_TOKEN

    now = datetime.now(timezone.utc).isoformat()
    live = CredentialCheck("feed_ok", 200, True, "fixture", now)
    sandbox = CredentialCheck("rejected", 401, False, "fixture", now)

    monkeypatch.setattr(LiveActivationService, "_default_token_exchange",
                        staticmethod(_exchange))
    monkeypatch.setattr(LiveActivationService, "_default_verifier",
                        staticmethod(lambda _token: {"live": live,
                                                     "sandbox": sandbox}))


def route_http_to_exchange(exchange, monkeypatch):
    """Send every httpx.request to the in-process mock: no socket, no host."""
    def request(method, url, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        return exchange.transport().handle_request(
            httpx.Request(method, url, **kwargs))

    monkeypatch.setattr(httpx, "request", request)


def valid_configuration():
    return {
        "max_position_value": 100000,
        "max_open_positions": 5,
        "max_daily_loss": 20000,
        "max_trades_per_day": 20,
        "live_canary_max_notional": 5000,
        "live_canary_orders": 2,
        "live_max_equity_drawdown": 0.1,
        "data_source": "upstox",
        "symbols": "NSE_EQ|INE002A01018",
        "timeframe": "1m",
        "calendar": "nse-session",
    }


def connect(client, token):
    return client.post("/api/live/activation/oauth/exchange",
                        headers=headers(token),
                        json={"code": "ok-code", "client_id": "client-id",
                              "client_secret": FAKE_SECRET,
                              "redirect_uri": "https://localhost:8443/callback"})


def build_gateway(app, settings=None):
    """The production execution stack, wired to the control plane's session.

    Before the activation the control plane is still PAPER, so the settings
    the live engine WOULD run with are taken from the live configuration
    file. They are the same values the activation later installs.
    """
    from app.brokers.live import UpstoxLiveBroker
    from app.live.gateway import LiveExecutionGateway
    from app.live.gateway_broker import GatewayRoutedBroker
    from app.live.guard_chain import default_chain
    from app.live.ops import read_emergency_stop

    if settings is None:
        settings = LiveActivationService(app.state.workstation,
                                         app=app).prospective_live_settings()
    repository = app.state.workstation.repository
    sessions = app.state.workstation.live_sessions
    broker = UpstoxLiveBroker(
        str(settings.upstox_live_access_token), KEY,
        confirm=LIVE_CONFIRM_PHRASE, repository=repository)

    def risk_context():
        stopped, _why = read_emergency_stop(repository)
        snapshot = broker.ledger.snapshot()
        exposure = float(snapshot.get("position_quantity", 0.0) or 0.0) * float(
            snapshot.get("mark") or snapshot.get("position_avg_price") or 0.0)
        realized = float(snapshot.get("realized_pnl", 0.0) or 0.0)
        daily_limit = float(settings.max_daily_loss or 0.0)
        equity = float(snapshot.get("equity") or 0.0)
        peak = float(repository.get_engine_state("peak_equity", 0.0) or 0.0)
        drawdown_limit = float(getattr(settings, "live_max_equity_drawdown", 0.0)
                               or 0.0)
        return {
            "emergency_stop": bool(stopped),
            "current_exposure": exposure,
            "daily_loss_breached": bool(daily_limit and realized <= -daily_limit),
            "drawdown_breached": bool(
                drawdown_limit and peak and equity
                and (peak - equity) / peak >= drawdown_limit),
            "canary_exhausted": False,
        }

    gateway = LiveExecutionGateway(
        settings=settings, repository=repository, session_manager=sessions,
        broker=broker,
        guard_chain=default_chain(settings=settings,
                                  max_entry_age_s=float(
                                      getattr(settings, "live_max_entry_age_s",
                                              30.0) or 30.0)),
        epochs=sessions.epochs, emergency_stop_reader=read_emergency_stop,
        account_scope=broker.account_scope, context_provider=risk_context)
    facade = GatewayRoutedBroker(broker, gateway, market_id="NSE_EQ",
                                  strategy_id="engine",
                                  account_scope=broker.account_scope,
                                  universe="core")
    return gateway, facade, broker


# ---------------------------------------------------------------- the flow --

class TestLiveSwitchEndToEnd:
    def test_the_wizard_alone_never_touches_the_broker(self, paper, monkeypatch):
        from app.brokers.mock_exchange import MockUpstoxExchange

        client, token, app, home = paper
        exchange = MockUpstoxExchange(token=FAKE_LIVE_TOKEN)
        route_http_to_exchange(exchange, monkeypatch)
        install_fake_broker_identity(app, monkeypatch)

        # 1 — connect the account (the broker's official OAuth, in fixture form)
        connected = connect(client, token)
        assert connected.status_code == 200, connected.text
        assert connected.json()["present"] is True

        # 2 — set and confirm the hard limits
        configured = client.post("/api/live/activation/configuration",
                                 headers=headers(token),
                                 json=valid_configuration())
        assert configured.status_code == 200, configured.text

        # 3 — review readiness (still PAPER: nothing is armed)
        snapshot = client.get("/api/live/activation", headers=headers(token)).json()
        assert snapshot["switch"]["position"] == "PAPER"
        assert snapshot["session"]["state"] != "ACTIVE"
        assert snapshot["credential"]["present"] is True
        assert FAKE_LIVE_TOKEN not in str(snapshot)

        # a wrong confirmation phrase changes nothing
        refused = client.post("/api/live/activation/start", headers=headers(token),
                              json={"confirmation_phrase": "yes"})
        assert refused.status_code == 409
        assert client.get("/api/live/activation",
                          headers=headers(token)).json()["switch"]["position"] \
            == "PAPER"

        # not one request reached the broker
        assert exchange.request_log == []

    def test_the_explicit_confirmation_is_what_starts_live_trading(
            self, paper, monkeypatch):
        from app.brokers.mock_exchange import MockUpstoxExchange
        from app.live.gateway import GatewayRefusal

        client, token, app, home = paper
        exchange = MockUpstoxExchange(token=FAKE_LIVE_TOKEN)
        route_http_to_exchange(exchange, monkeypatch)
        install_fake_broker_identity(app, monkeypatch)
        install_fake_engine(app)

        assert connect(client, token).status_code == 200
        assert client.post("/api/live/activation/configuration",
                            headers=headers(token),
                            json=valid_configuration()).status_code == 200

        # an entry attempt BEFORE the confirmation is refused and sends nothing
        gateway, facade, _broker = build_gateway(app)
        with pytest.raises(GatewayRefusal) as refused:
            facade.submit("BUY", 1, 2900.0, intent_id="before-confirmation",
                          instrument_key=KEY, signal_ts=_now())
        assert refused.value.layer in {"INTENT", "SESSION STATE",
                                        "AUTHORIZATION EPOCH",
                                        "SUPERVISOR READINESS"}
        assert exchange.request_log == []

        # the final, explicit confirmation
        started = client.post("/api/live/activation/start", headers=headers(token),
                              json={"confirmation_phrase": LIVE_CONFIRM_PHRASE})
        assert started.status_code == 200, started.text
        assert started.json()["state"] == "ACTIVE"

        # the installation is now truthfully LIVE
        from app.config.envfile import read_values

        live_env = read_values(home / "live.env")
        assert live_env["MODE"] == "LIVE"
        assert live_env["LIVE_TRADING"] == "true"
        assert live_env["BROKER"] == "UPSTOX_LIVE"
        assert live_env["LIVE_CONFIRM"] == LIVE_CONFIRM_PHRASE
        # the PAPER installation file was not rewritten
        paper_env = read_values(home / ".env")
        assert paper_env.get("MODE", "PAPER").upper() == "PAPER"
        assert paper_env.get("BROKER", "LOCAL_PAPER").upper() == "LOCAL_PAPER"

        # NOW an order can travel the real gateway into the mock exchange
        order_id = facade.submit("BUY", 1, 2900.0, intent_id="after-confirmation",
                                  instrument_key=KEY, signal_ts=_now())
        assert order_id
        places = [entry for entry in exchange.request_log
                  if entry["path"].endswith("place")]
        assert len(places) == 1
        # the live tables carry it; the paper tables do not
        assert app.state.workstation.repository.list_live_orders()
        assert not app.state.workstation.repository.list_sandbox_orders()

        # the switch-off flow: stop, disarm, back to PAPER
        stopped = client.post("/api/live/activation/stop", headers=headers(token),
                              json={"reason": "operator moved the switch to PAPER"})
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["state"] in {"STOPPED", "OFF", "DISARMED"}
        after = read_values(home / "live.env")
        assert "MODE" not in after
        assert "LIVE_TRADING" not in after
        assert "BROKER" not in after
        assert "LIVE_CONFIRM" not in after

        # a later entry is refused: the epoch is gone
        with pytest.raises(GatewayRefusal):
            facade.submit("BUY", 1, 2900.0, intent_id="after-switch-off",
                          instrument_key=KEY, signal_ts=_now())
        assert len([entry for entry in exchange.request_log
                    if entry["path"].endswith("place")]) == 1

    def test_the_dashboard_and_the_api_show_the_real_effective_mode(
            self, paper, monkeypatch):
        """State, dashboard and API must agree with what actually runs."""
        client, token, app, home = paper
        install_fake_engine(app)
        install_fake_broker_identity(app, monkeypatch)
        assert connect(client, token).status_code == 200
        assert client.post("/api/live/activation/configuration",
                            headers=headers(token),
                            json=valid_configuration()).status_code == 200

        boot = client.get("/api/boot").json()
        assert boot["mode"] == "PAPER"
        assert boot["live_trading"] is False
        assert "SIMULATED" in boot["mode_display"]

        assert client.post("/api/live/activation/start", headers=headers(token),
                           json={"confirmation_phrase": LIVE_CONFIRM_PHRASE}
                           ).status_code == 200

        boot = client.get("/api/boot").json()
        assert boot["mode"] == "LIVE"
        assert boot["live_trading"] is True
        assert "REAL MONEY" in boot["mode_display"]

        assert client.post("/api/live/activation/stop", headers=headers(token),
                           json={"reason": "operator moved the switch to PAPER"}
                           ).status_code == 200
        boot = client.get("/api/boot").json()
        assert boot["mode"] == "PAPER"
        assert boot["live_trading"] is False


def _now():
    import time

    return time.time()
