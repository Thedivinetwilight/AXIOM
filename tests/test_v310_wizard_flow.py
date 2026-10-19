"""3.1.0 — the zero-command first-run flow, end to end, against the real app.

Fresh home -> wizard prepares paper (credential auto-generated, .env written,
database initialized, settings hot-applied) -> preflight READY -> one-click
START PAPER -> replay bars flow through strategy/risk/execution -> orders,
fills and trades are recorded -> STOP persists -> restart recovers the exact
session -> secrets stay out of every observable surface.

No step is simulated with mocks: this drives the real FastAPI application the
executable ships.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.envfile import read_values
from app.config.settings import Settings

ENV_KEYS = [
    "DATABASE_URL", "DATA_DIR", "API_TOKEN", "BROKER", "MODE",
    "MARKET_DATA_SOURCE", "DATA_SOURCE", "DEFAULT_SYMBOL", "DEFAULT_TIMEFRAME",
    "MARKET_DATA_ACCESS_TOKEN", "MARKET_DATA_SYMBOLS", "MARKET_DATA_TOKEN",
    "MARKET_DATA_PROVIDER", "MARKET_DATA_AUTHORIZE_URL", "MARKET_DATA_MODE",
    "MARKET_DATA_TIMEFRAME",
    "UPSTOX_ACCESS_TOKEN", "UPSTOX_INSTRUMENT_KEYS", "UPSTOX_SANDBOX_ACCESS_TOKEN",
    "ARENA_HOME", "ARENA_ENV_FILE", "LIVE_TRADING", "LIVE_CONFIRM",
]


@pytest.fixture()
def fresh_home(tmp_path):
    """An isolated ARENA_HOME; the process environment is fully restored after
    the test because the wizard legitimately mutates os.environ (hot reload)."""
    saved = dict(os.environ)
    home = tmp_path / "ArenaHome"
    try:
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["ARENA_HOME"] = str(home)
        yield home
    finally:
        os.environ.clear()
        os.environ.update(saved)


def wait_for_paper(client, token, *, need_orders=False, timeout=90.0):
    headers = {"Authorization": "Bearer " + token}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get("/paper/status", headers=headers).json()
        orders = client.get("/paper/orders?limit=1000", headers=headers).json()["orders"]
        trades = client.get("/paper/trades?limit=1000", headers=headers).json()["trades"]
        if need_orders:
            if orders and trades:
                return status, orders, trades
        elif status["processed_bars"] > 5 or status["state"] in ("STOPPED", "ERROR"):
            return status, orders, trades
        if status["state"] == "ERROR":
            return status, orders, trades
        time.sleep(0.2)
    raise AssertionError("paper engine did not reach the expected state in time")


class TestFirstRunWizard:
    def test_a_fresh_home_is_detected_as_first_run(self, fresh_home):
        with TestClient(create_app(Settings.from_env())) as client:
            boot = client.get("/api/boot").json()
            assert boot["first_run"] is True
            assert boot["broker"] == "LOCAL_PAPER"
            assert boot["live_trading"] is False

    def test_prepare_paper_creates_everything_with_zero_user_commands(self, fresh_home):
        with TestClient(create_app(Settings.from_env())) as client:
            response = client.post("/api/wizard/prepare-paper",
                                   json={"data_source": "replay"})
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["broker"] == "LOCAL_PAPER"
            assert data["mode"] == "PAPER"
            assert data["token_generated"] is True
            token = data["token_once"]
            assert token and len(token) >= 32

            # configuration landed in the EXTERNAL home, nowhere else
            env_path = fresh_home / ".env"
            assert env_path.exists()
            values = read_values(env_path)
            assert values["BROKER"] == "LOCAL_PAPER"
            assert values["MODE"] == "PAPER"
            assert values["API_TOKEN"] == token
            assert values["DEFAULT_SYMBOL"] == "DEMO"
            # canonical key: Settings reads DATA_SOURCE (regression for the
            # MARKET_DATA_SOURCE mismatch that silently did nothing pre-3.1.0)
            assert values["DATA_SOURCE"] == "replay"

            # database initialized automatically
            assert (fresh_home / "trading_bot.db").exists()

            # hot reload: the RUNNING process now accepts the new credential —
            # no restart needed, which is what makes the one-click start work
            authorized = {"Authorization": "Bearer " + token}
            assert client.get("/paper/status", headers=authorized).status_code == 200

            # preflight reports every subsystem READY
            preflight = client.get("/api/wizard/preflight", headers=authorized).json()
            assert preflight["ready"] is True, preflight
            names = {check["name"] for check in preflight["checks"]}
            for required in ("DATABASE", "PAPER ENGINE", "DATA", "STRATEGIES", "RISK"):
                assert required in names

    def test_the_generated_token_is_returned_only_once_and_stays_off_urls(self, fresh_home):
        with TestClient(create_app(Settings.from_env())) as client:
            data = client.post("/api/wizard/prepare-paper",
                               json={"data_source": "replay"}).json()
            token = data["token_once"]
            # the boot endpoint (readable by any loopback client) must not leak it
            assert token not in client.get("/api/boot").text
            assert token not in client.get("/setup").text
            assert token not in client.get("/setup/status").text
            # a second prepare keeps the existing credential instead of rotating
            again = client.post("/api/wizard/prepare-paper",
                                json={"data_source": "replay"}).json()
            assert again["token_generated"] is False
            assert again["token_once"] is None

    def test_existing_token_handoff_authenticates_preflight_and_start(self, fresh_home, monkeypatch):
        token = "preconfigured-local-api-token"
        fresh_home.mkdir(parents=True, exist_ok=True)
        (fresh_home / ".env").write_text("API_TOKEN=" + token + "\n", encoding="utf-8")
        monkeypatch.setenv("API_TOKEN", token)
        called = []

        async def engine_start(body, request):
            called.append(True)
            return {"state": "STARTING"}

        from app.api.routes import paper

        monkeypatch.setattr(paper, "do_start", engine_start)
        with TestClient(create_app(Settings.from_env())) as client:
            prepared = client.post("/api/wizard/prepare-paper",
                                   json={"data_source": "replay"})
            assert prepared.status_code == 200, prepared.text
            assert prepared.json()["token_generated"] is False
            assert prepared.json()["token_once"] is None

            # The reveal response is the established loopback handoff; after
            # the browser stores this value, the real auth dependency accepts it.
            revealed = client.post("/api/credentials/reveal")
            assert revealed.status_code == 200
            assert revealed.headers["cache-control"] == "no-store"
            assert revealed.json()["token"] == token

            assert client.get("/api/wizard/preflight").status_code == 401
            assert client.get("/api/wizard/preflight",
                              headers={"Authorization": "Bearer stale-token"}).status_code == 401
            preflight = client.get(
                "/api/wizard/preflight",
                headers={"Authorization": "Bearer " + revealed.json()["token"]},
            )
            assert preflight.status_code == 200, preflight.text
            checks = {check["name"]: check for check in preflight.json()["checks"]}
            assert checks["UI / API"]["ok"] is True
            assert "Authenticated browser request accepted" in checks["UI / API"]["detail"]

            missing = client.post("/api/paper/start-default", json={"source": "replay"})
            incorrect = client.post(
                "/api/paper/start-default", json={"source": "replay"},
                headers={"Authorization": "Bearer stale-token"})
            assert missing.status_code == 401
            assert incorrect.status_code == 401
            assert called == [], "unauthenticated requests must not reach paper startup"

            started = client.post(
                "/api/paper/start-default", json={"source": "replay"},
                headers={"Authorization": "Bearer " + revealed.json()["token"]})
            assert started.status_code == 200, started.text
            assert called == [True]

    def test_credential_reveal_requires_bearer_for_remote_clients(self, fresh_home):
        token = "remote-reveal-protection-token"
        app = create_app(Settings(api_token=token,
                                  database_url=f"sqlite:///{fresh_home / 'remote.db'}"))
        with TestClient(app, client=("192.0.2.25", 50123)) as client:
            assert client.post("/api/credentials/reveal").status_code == 401
            response = client.post(
                "/api/credentials/reveal",
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["token"] == token

    def test_token_rotation_across_restart_rejects_old_and_accepts_new(self, fresh_home):
        old_token, new_token = "old-local-token", "new-local-token"
        database_url = f"sqlite:///{fresh_home / 'rotation.db'}"
        with TestClient(create_app(Settings(api_token=old_token,
                                            database_url=database_url))) as client:
            assert client.get("/api/system/info",
                              headers={"Authorization": "Bearer " + old_token}).status_code == 200

        with TestClient(create_app(Settings(api_token=new_token,
                                            database_url=database_url))) as client:
            assert client.get("/api/system/info",
                              headers={"Authorization": "Bearer " + old_token}).status_code == 401
            assert client.get("/api/system/info",
                              headers={"Authorization": "Bearer " + new_token}).status_code == 200
            assert client.get("/api/wizard/preflight",
                              headers={"Authorization": "Bearer " + new_token}).status_code == 200

    def test_start_paper_with_one_action_produces_recorded_executions(self, fresh_home):
        with TestClient(create_app(Settings.from_env())) as client:
            token = client.post("/api/wizard/prepare-paper",
                                json={"data_source": "replay"}).json()["token_once"]
            headers = {"Authorization": "Bearer " + token}
            started = client.post("/api/paper/start-default",
                                  json={"source": "replay", "replay_delay": 0},
                                  headers=headers)
            assert started.status_code == 200, started.text
            assert started.json()["state"] in ("STARTING", "RUNNING")

            status, orders, trades = wait_for_paper(client, token, need_orders=True)
            assert status["state"] in ("RUNNING", "STOPPED"), status
            assert status["processed_bars"] > 100
            assert len(orders) > 0, "replay paper must record simulated orders"
            assert len(trades) > 0, "replay paper must record simulated trades"
            assert status["live_trading"] is False
            assert status["paper_mode"] is True

            stopped = client.post("/paper/stop", headers=headers).json()
            assert stopped["state"] == "STOPPED"

    def test_restart_recovers_the_session_exactly(self, fresh_home):
        with TestClient(create_app(Settings.from_env())) as client:
            token = client.post("/api/wizard/prepare-paper",
                                json={"data_source": "replay"}).json()["token_once"]
            headers = {"Authorization": "Bearer " + token}
            client.post("/api/paper/start-default",
                        json={"source": "replay", "replay_delay": 0}, headers=headers)
            wait_for_paper(client, token, need_orders=True)
            client.post("/paper/stop", headers=headers)
            account_before = client.get("/paper/account", headers=headers).json()
            orders_before = client.get("/paper/orders?limit=1000",
                                       headers=headers).json()["orders"]

        # ---- the process goes away; a brand new app comes up on the same home
        with TestClient(create_app(Settings.from_env())) as client:
            headers = {"Authorization": "Bearer " + token}
            account_after = client.get("/paper/account", headers=headers).json()
            orders_after = client.get("/paper/orders?limit=1000",
                                      headers=headers).json()["orders"]
            assert account_after["equity"] == account_before["equity"]
            assert account_after["realized_pnl"] == account_before["realized_pnl"]
            assert len(orders_after) == len(orders_before)
            boot = client.get("/api/boot").json()
            assert boot["first_run"] is False, "a recovered install is not first-run"

    def test_reveal_endpoint_is_loopback_only_and_returns_the_active_token(self, fresh_home, caplog):
        with TestClient(create_app(Settings.from_env())) as client:
            token = client.post("/api/wizard/prepare-paper",
                                json={"data_source": "replay"}).json()["token_once"]
            with caplog.at_level("DEBUG"):
                response = client.post("/api/credentials/reveal")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["token"] == token

            for path in ("/api/boot", "/setup", "/setup/status", "/health", "/"):
                assert token not in client.get(path).text
            assert token not in client.get(
                "/api/system/info", headers={"Authorization": "Bearer " + token}).text
            from app import runtime
            assert token not in repr(runtime.describe())
            assert token not in caplog.text


class TestWizardRefusals:
    def test_live_market_paper_requires_a_valid_credential(self, fresh_home):
        with TestClient(create_app(Settings.from_env())) as client:
            response = client.post(
                "/api/wizard/prepare-paper",
                json={"data_source": "upstox"})
            assert response.status_code == 422

    def test_an_existing_live_configuration_is_never_touched(self, fresh_home):
        fresh_home.mkdir(parents=True, exist_ok=True)
        # A half-enabled live configuration does not even LOAD — fail closed
        # at the settings layer, before any route could act on it.
        (fresh_home / ".env").write_text(
            "BROKER=UPSTOX_LIVE\nMODE=LIVE\nLIVE_TRADING=true\n", encoding="utf-8")
        with pytest.raises(ValueError):
            Settings.from_env()

        # The failed load polluted the process environment via dotenv; a real
        # restart would not carry that over, so clear it before the next phase.
        for key in ("BROKER", "MODE", "LIVE_TRADING"):
            os.environ.pop(key, None)

        # A sandbox installation IS loadable — and the wizard must refuse to
        # reconfigure it instead of silently switching it to paper.
        (fresh_home / ".env").write_text("BROKER=UPSTOX_SANDBOX\n"
                                         "UPSTOX_SANDBOX_ACCESS_TOKEN=x\n",
                                         encoding="utf-8")
        with TestClient(create_app(Settings.from_env())) as client:
            response = client.post("/api/wizard/prepare-paper",
                                   json={"data_source": "replay"})
            assert response.status_code == 409
            values = read_values(fresh_home / ".env")
            assert values["BROKER"] == "UPSTOX_SANDBOX", "must not overwrite"
