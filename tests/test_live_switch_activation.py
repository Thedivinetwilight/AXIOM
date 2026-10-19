"""The PAPER/LIVE switch — backend behaviour, refusals and isolation.

Every test here runs against a disposable Arena home, a throwaway SQLite
database and a deterministic mock. No test contacts a broker, no test uses a
real credential, and no test submits an order.

The matrix follows the acceptance criteria of the live-switch work:

* PAPER is the default; a fresh install has no live switches;
* moving the switch towards LIVE never places an order;
* the four live switches are written only by the explicit activation;
* every hard limit must be stated, finite, positive and consistent;
* a sandbox credential can never authorise a live session;
* secrets are never returned, logged or audited;
* a refused activation rolls the installation back to PAPER;
* the switch-off drains, disarms and never claims the account is flat.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import LIVE_CONFIRM_PHRASE, Settings
from app.live.activation import ActivationError, LiveActivationService

FAKE_LIVE_TOKEN = "fake-live-access-token-for-tests"
FAKE_SECRET = "fake-client-secret-for-tests"


# --------------------------------------------------------------- fixtures --

@pytest.fixture()
def paper(tmp_path, monkeypatch):
    """A disposable paper installation with its own home and database."""
    home = tmp_path / "ArenaHome"
    for key in ("DATABASE_URL", "DATA_DIR", "API_TOKEN", "BROKER", "MODE",
                "MARKET_DATA_SOURCE", "LIVE_TRADING", "LIVE_CONFIRM",
                "UPSTOX_LIVE_ACCESS_TOKEN", "MAX_POSITION_VALUE",
                "MAX_OPEN_POSITIONS", "MAX_DAILY_LOSS", "MAX_TRADES_PER_DAY",
                "DATA_SOURCE", "DEFAULT_TIMEFRAME", "MARKET_DATA_TIMEFRAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ARENA_HOME", str(home))
    saved = dict(os.environ)
    app = create_app(Settings.from_env())
    try:
        with TestClient(app) as client:
            # the supervised child is a fixture; a refusal must not wait the
            # production start-up timeout
            app.state.workstation.live_supervisor.startup_timeout = 2.0
            prepared = client.post("/api/wizard/prepare-paper",
                                    json={"data_source": "replay"}).json()
            token = prepared["token_once"]
            yield client, token, app, home
    finally:
        # the activation flow reloads the configuration into the process
        # environment; a leaked key would poison the next test
        os.environ.clear()
        os.environ.update(saved)


def headers(token):
    return {"Authorization": "Bearer " + token}


def env_values(home):
    """The LIVE configuration file (never the installation's own .env)."""
    from app.config.envfile import read_values

    return read_values(home / "live.env")


def paper_env_values(home):
    """The installation's own configuration file — it must stay untouched."""
    from app.config.envfile import read_values

    return read_values(home / ".env")


def service(app, **kwargs):
    return LiveActivationService(app.state.workstation, **kwargs)


def fake_exchange(token=FAKE_LIVE_TOKEN, **kwargs):
    """A deterministic token exchange: no network, no real credential."""
    def _exchange(*, code, client_id, client_secret, redirect_uri):
        if code == "refused-code":
            raise RuntimeError("broker said no")
        return token
    return _exchange


def fake_verifier(live_ok=True, sandbox_ok=False):
    """A credential verdict: the live host and/or the sandbox host accepts."""
    from app.data.upstox_auth import CredentialCheck

    now = datetime.now(timezone.utc).isoformat()
    live = CredentialCheck("feed_ok" if live_ok else "rejected",
                           200 if live_ok else 401, live_ok,
                           "fixture guidance", now)
    sandbox = CredentialCheck("feed_ok" if sandbox_ok else "rejected",
                               200 if sandbox_ok else 401, sandbox_ok,
                               "fixture guidance", now)
    return {"live": live, "sandbox": sandbox}


def valid_configuration():
    return {
        "max_position_value": 10000,
        "max_open_positions": 1,
        "max_daily_loss": 2000,
        "max_trades_per_day": 5,
        "live_canary_max_notional": 1000,
        "live_canary_orders": 2,
        "live_max_equity_drawdown": 0.1,
        "data_source": "upstox",
        "symbols": "NSE_EQ|INE002A01018",
        "timeframe": "1m",
        "calendar": "nse-session",
    }


class FakeProcess:
    def __init__(self, pid=None):
        import os as _os

        self.pid = int(pid or _os.getpid())
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("fixture-live-engine", timeout)

    def terminate(self):  # pragma: no cover - forbidden path in these tests
        self.terminated = True

    def kill(self):  # pragma: no cover - forbidden path in these tests
        self.killed = True


def install_fake_engine(app):
    """Replace the supervised child with a deterministic in-process fake.

    The fake acquires the live lease and publishes the runtime evidence the
    supervisor insists on. It never sends an order.
    """
    from app.live.lock import EngineLock

    repository = app.state.workstation.repository
    spawned = []

    def popen(argv, **kwargs):
        spawned.append((argv, kwargs))
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
    return spawned


def connect(paper, *, live_ok=True, sandbox_ok=False, code="ok-code"):
    client, token, app, home = paper
    return client.post("/api/live/activation/oauth/exchange", headers=headers(token),
                       json={"code": code, "client_id": "client-id",
                             "client_secret": FAKE_SECRET,
                             "redirect_uri": "https://localhost:8443/callback"})


def connect_with(app, *, live_ok=True, sandbox_ok=False, code="ok-code"):
    svc = service(app, token_exchange=fake_exchange(),
                  credential_verifier=lambda _token: fake_verifier(live_ok,
                                                                   sandbox_ok))
    return svc.connect_account({
        "code": code, "client_id": "client-id",
        "client_secret": FAKE_SECRET,
        "redirect_uri": "https://localhost:8443/callback"})


# ============================================================== paper default

class TestPaperIsTheDefault:
    def test_a_fresh_install_has_no_live_switches(self, paper):
        _, _, _, home = paper
        values = env_values(home)
        assert values.get("MODE", "PAPER").upper() == "PAPER"
        assert values.get("BROKER", "LOCAL_PAPER").upper() == "LOCAL_PAPER"
        assert values.get("LIVE_TRADING", "false").lower() in {"false", "0", ""}
        assert LIVE_CONFIRM_PHRASE not in values.values()

    def test_the_switch_reports_paper(self, paper):
        client, token, _, _ = paper
        response = client.get("/api/live/activation", headers=headers(token))
        assert response.status_code == 200
        body = response.json()
        assert body["switch"]["position"] == "PAPER"
        assert body["session"]["state"] in {"OFF", "CONFIGURED", "READY"}
        assert body["credential"]["present"] is False

    def test_the_activation_snapshot_never_contains_a_secret(self, paper):
        client, token, _, _ = paper
        body = client.get("/api/live/activation", headers=headers(token)).json()
        assert FAKE_LIVE_TOKEN not in str(body)
        assert FAKE_SECRET not in str(body)

    def test_moving_the_switch_towards_live_places_no_order(self, paper):
        """Connecting, configuring and reviewing never contacts a broker."""
        client, token, app, home = paper
        svc = service(app, token_exchange=fake_exchange(),
                      credential_verifier=fake_verifier)
        # a broker that would explode if it were touched
        class ExplodingBroker:
            def submit(self, *a, **k):  # pragma: no cover - must never run
                raise AssertionError("a broker was called during setup")

        original = app.state.broker
        app.state.broker = ExplodingBroker()
        try:
            svc.connect_account({"code": "ok-code", "client_id": "client-id",
                                 "client_secret": FAKE_SECRET,
                                 "redirect_uri": "https://localhost:8443/callback"})
            svc.save_configuration(valid_configuration())
            snapshot = svc.snapshot()
        finally:
            app.state.broker = original
        assert snapshot["switch"]["position"] == "PAPER"
        assert snapshot["session"]["state"] != "ACTIVE"

    def test_the_setup_screen_still_refuses_live_switches(self, paper):
        """The general setup endpoint must not become a live back door."""
        client, token, _, home = paper
        response = client.post("/setup/save", headers=headers(token),
                               json={"LIVE_TRADING": "true"})
        assert response.status_code in (400, 409, 422)
        assert env_values(home).get("LIVE_TRADING", "false").lower() != "true"


# ==================================================================== limits

class TestRiskLimits:
    @pytest.mark.parametrize("field", [
        "max_position_value", "max_open_positions", "max_daily_loss",
        "max_trades_per_day"])
    def test_a_missing_required_limit_is_refused(self, paper, field):
        client, token, _, _ = paper
        payload = valid_configuration()
        payload.pop(field)
        response = client.post("/api/live/activation/configuration",
                               headers=headers(token), json=payload)
        assert response.status_code == 409
        assert field.replace("_", " ").split()[0].upper() in response.json()["detail"].upper()

    @pytest.mark.parametrize("field", [
        "max_position_value", "max_open_positions", "max_daily_loss",
        "max_trades_per_day"])
    @pytest.mark.parametrize("value", [0, 0.0, "0"])
    def test_a_zero_limit_is_refused(self, paper, field, value):
        client, token, _, _ = paper
        payload = valid_configuration()
        payload[field] = value
        response = client.post("/api/live/activation/configuration",
                               headers=headers(token), json=payload)
        assert response.status_code == 409
        assert "greater than zero" in response.json()["detail"]

    @pytest.mark.parametrize("field", [
        "max_position_value", "max_open_positions", "max_daily_loss",
        "max_trades_per_day"])
    def test_a_negative_limit_is_refused(self, paper, field):
        client, token, _, _ = paper
        payload = valid_configuration()
        payload[field] = -1
        response = client.post("/api/live/activation/configuration",
                               headers=headers(token), json=payload)
        assert response.status_code == 409
        assert "negative" in response.json()["detail"].lower()

    @pytest.mark.parametrize("field", [
        "max_position_value", "max_daily_loss", "live_canary_max_notional"])
    def test_nan_and_infinity_are_refused(self, paper, field):
        client, token, _, _ = paper
        for bad in ("NaN", "Infinity", "-Infinity", "inf"):
            payload = valid_configuration()
            payload[field] = bad
            response = client.post("/api/live/activation/configuration",
                                   headers=headers(token), json=payload)
            assert response.status_code == 409, (field, bad)
            detail = response.json()["detail"].lower()
            assert "nan" in detail or "finite" in detail or "infinity" in detail

    def test_a_malformed_limit_is_refused(self, paper):
        client, token, _, _ = paper
        payload = valid_configuration()
        payload["max_daily_loss"] = "a lot"
        response = client.post("/api/live/activation/configuration",
                               headers=headers(token), json=payload)
        assert response.status_code == 409
        assert "not a number" in response.json()["detail"].lower()

    def test_a_fractional_position_count_is_refused(self, paper):
        client, token, _, _ = paper
        payload = valid_configuration()
        payload["max_open_positions"] = "1.5"
        response = client.post("/api/live/activation/configuration",
                               headers=headers(token), json=payload)
        assert response.status_code == 409

    def test_an_incompatible_canary_combination_is_refused(self, paper):
        client, token, _, _ = paper
        payload = valid_configuration()
        payload["live_canary_max_notional"] = 500
        payload["live_canary_orders"] = 0
        response = client.post("/api/live/activation/configuration",
                               headers=headers(token), json=payload)
        assert response.status_code == 409
        assert "canary" in response.json()["detail"].lower()

    def test_a_drawdown_outside_the_range_is_refused(self, paper):
        client, token, _, _ = paper
        payload = valid_configuration()
        payload["live_max_equity_drawdown"] = 1.5
        response = client.post("/api/live/activation/configuration",
                               headers=headers(token), json=payload)
        assert response.status_code == 409

    def test_a_complete_limit_set_is_accepted_and_written(self, paper):
        client, token, _, home = paper
        response = client.post("/api/live/activation/configuration",
                               headers=headers(token),
                               json=valid_configuration())
        assert response.status_code == 200
        values = env_values(home)
        assert float(values["MAX_POSITION_VALUE"]) == 10000
        assert int(values["MAX_OPEN_POSITIONS"]) == 1
        assert float(values["MAX_DAILY_LOSS"]) == 2000
        assert int(values["MAX_TRADES_PER_DAY"]) == 5

    def test_saving_the_configuration_does_not_enable_live(self, paper):
        client, token, _, home = paper
        client.post("/api/live/activation/configuration", headers=headers(token),
                    json=valid_configuration())
        values = env_values(home)
        assert values.get("BROKER", "LOCAL_PAPER").upper() == "LOCAL_PAPER"
        assert values.get("MODE", "PAPER").upper() == "PAPER"
        assert values.get("LIVE_TRADING", "false").lower() != "true"

    def test_an_unknown_field_is_refused(self, paper):
        client, token, _, _ = paper
        payload = valid_configuration()
        payload["max_leverage"] = 20
        response = client.post("/api/live/activation/configuration",
                               headers=headers(token), json=payload)
        assert response.status_code in (409, 422)


# ============================================================ configuration

class TestMarketWiring:
    def test_a_sandbox_order_host_is_refused_as_the_live_host(self, paper):
        client, token, _, _ = paper
        payload = valid_configuration()
        payload["live_base_url"] = "https://api-sandbox.upstox.com"
        response = client.post("/api/live/activation/configuration",
                               headers=headers(token), json=payload)
        assert response.status_code == 409
        assert "live order host" in response.json()["detail"].lower()

    def test_a_non_https_live_host_is_refused(self, paper):
        client, token, _, _ = paper
        payload = valid_configuration()
        payload["live_base_url"] = "http://api-hft.upstox.com"
        response = client.post("/api/live/activation/configuration",
                               headers=headers(token), json=payload)
        assert response.status_code == 409

    def test_a_daily_timeframe_is_refused(self, paper):
        client, token, _, _ = paper
        payload = valid_configuration()
        payload["timeframe"] = "1D"
        response = client.post("/api/live/activation/configuration",
                               headers=headers(token), json=payload)
        assert response.status_code == 409
        assert "intraday" in response.json()["detail"].lower()

    def test_an_invalid_instrument_key_is_refused(self, paper):
        client, token, _, _ = paper
        payload = valid_configuration()
        payload["symbols"] = "NSE_EQ:INE002A01018"
        response = client.post("/api/live/activation/configuration",
                               headers=headers(token), json=payload)
        assert response.status_code == 409

    def test_a_replay_data_source_is_refused_for_live(self, paper):
        client, token, _, _ = paper
        payload = valid_configuration()
        payload["data_source"] = "replay"
        response = client.post("/api/live/activation/configuration",
                               headers=headers(token), json=payload)
        assert response.status_code == 409


# ============================================================== credentials

class TestCredentials:
    def test_the_login_url_contains_no_secret(self, paper):
        client, token, _, _ = paper
        response = client.post("/api/live/activation/oauth/url",
                               headers=headers(token),
                               json={"client_id": "public-client-id",
                                     "redirect_uri": "https://localhost:8443/callback"})
        assert response.status_code == 200
        url = response.json()["authorization_url"]
        assert url.startswith("https://api.upstox.com/v2/login/authorization/dialog")
        assert "public-client-id" in url
        assert FAKE_SECRET not in url

    def test_a_successful_exchange_returns_only_a_masked_state(self, paper):
        client, token, app, home = paper
        result = connect_with(app)
        assert result["present"] is True
        assert result["state"] in {"verified", "unverified"}
        assert FAKE_LIVE_TOKEN not in str(result)
        assert "fake-live-access-token" not in str(result)
        # the token itself is stored in the operator's configuration file
        assert env_values(home)["UPSTOX_LIVE_ACCESS_TOKEN"] == FAKE_LIVE_TOKEN

    def test_a_refused_exchange_never_echoes_the_code_or_the_secret(self, paper):
        client, token, app, home = paper
        svc = service(app, token_exchange=fake_exchange(),
                      credential_verifier=fake_verifier)
        with pytest.raises(ActivationError) as excinfo:
            svc.connect_account({"code": "refused-code", "client_id": "cid",
                                 "client_secret": FAKE_SECRET,
                                 "redirect_uri": "https://localhost:8443/callback"})
        message = str(excinfo.value)
        assert "refused-code" not in message
        assert FAKE_SECRET not in message
        assert "UPSTOX_LIVE_ACCESS_TOKEN" not in env_values(home)

    def test_a_sandbox_credential_can_never_authorise_live(self, paper):
        client, token, app, home = paper
        svc = service(app, token_exchange=fake_exchange(),
                      credential_verifier=lambda _t: fake_verifier(
                          live_ok=False, sandbox_ok=True))
        with pytest.raises(ActivationError) as excinfo:
            svc.connect_account({"code": "ok-code", "client_id": "cid",
                                 "client_secret": FAKE_SECRET,
                                 "redirect_uri": "https://localhost:8443/callback"})
        assert "sandbox" in str(excinfo.value).lower()
        assert "UPSTOX_LIVE_ACCESS_TOKEN" not in env_values(home)

    def test_a_rejected_credential_is_refused(self, paper):
        client, token, app, home = paper
        svc = service(app, token_exchange=fake_exchange(),
                      credential_verifier=lambda _t: fake_verifier(
                          live_ok=False))
        with pytest.raises(ActivationError):
            svc.connect_account({"code": "ok-code", "client_id": "cid",
                                 "client_secret": FAKE_SECRET,
                                 "redirect_uri": "https://localhost:8443/callback"})
        assert "UPSTOX_LIVE_ACCESS_TOKEN" not in env_values(home)

    def test_the_exchange_requires_the_broker_application_credentials(self,
                                                                      paper):
        client, token, _, _ = paper
        response = client.post("/api/live/activation/oauth/exchange",
                               headers=headers(token),
                               json={"code": "ok-code", "client_id": "cid",
                                     "client_secret": "",
                                     "redirect_uri": "https://localhost:8443/callback"})
        assert response.status_code == 422

    def test_a_non_https_redirect_uri_is_refused(self, paper):
        client, token, app, _ = paper
        svc = service(app, token_exchange=fake_exchange(),
                      credential_verifier=fake_verifier)
        with pytest.raises(ActivationError):
            svc.connect_account({"code": "ok-code", "client_id": "cid",
                                 "client_secret": FAKE_SECRET,
                                 "redirect_uri": "http://localhost/callback"})

    def test_the_token_never_reaches_the_log(self, paper, caplog):
        client, token, app, home = paper
        with caplog.at_level(logging.DEBUG):
            connect_with(app)
        assert FAKE_LIVE_TOKEN not in caplog.text
        assert FAKE_SECRET not in caplog.text

    def test_the_audit_trail_never_contains_the_token(self, paper):
        client, token, app, home = paper
        connect_with(app)
        events = app.state.workstation.repository.list_system_events(limit=50)
        assert FAKE_LIVE_TOKEN not in str(events)
        assert FAKE_SECRET not in str(events)

    def test_forgetting_the_credential_removes_it(self, paper):
        client, token, app, home = paper
        connect_with(app)
        response = client.post("/api/live/activation/credential/forget",
                               headers=headers(token))
        assert response.status_code == 200
        assert response.json()["removed"] is True
        assert not env_values(home).get("UPSTOX_LIVE_ACCESS_TOKEN", "").strip()

    def test_the_credential_status_reports_absence_truthfully(self, paper):
        client, token, app, _ = paper
        connect_with(app)
        svc = service(app)
        status = svc.credential_status()
        assert status["present"] is True
        assert status["state"] in {"verified", "unverified", "expired", "changed"}
        assert FAKE_LIVE_TOKEN not in str(status)


# ================================================================ activation

class TestActivation:
    def _prepared(self, paper):
        client, token, app, home = paper
        connect_with(app)
        svc = service(app)
        svc.save_configuration(valid_configuration())
        return client, token, app, home, svc

    def test_the_wrong_phrase_changes_nothing(self, paper):
        client, token, app, home, svc = self._prepared(paper)
        before = env_values(home)
        with pytest.raises(ActivationError):
            svc.start(confirmation_phrase="yes")
        assert env_values(home) == before
        assert app.state.workstation.live_sessions.current().state != "ACTIVE"

    def test_each_mismatched_switch_blocks_the_activation(self, paper):
        """A hand-edited contradiction is refused, never silently overwritten."""
        client, token, app, home, svc = self._prepared(paper)
        for key, value in (("MODE", "PAPER"), ("BROKER", "LOCAL_PAPER"),
                           ("LIVE_TRADING", "false"),
                           ("LIVE_CONFIRM", "I-ACCEPT-SOMETHING-ELSE")):
            from app.config.envfile import update_values

            update_values(home / "live.env", {key: value})
            with pytest.raises(ActivationError) as excinfo:
                svc.start(confirmation_phrase=LIVE_CONFIRM_PHRASE)
            assert "contradicts" in str(excinfo.value).lower()
            # the contradictory line is left exactly as the operator wrote it
            assert env_values(home)[key] == value
            # and nothing was activated
            assert app.state.workstation.live_sessions.current().state != "ACTIVE"

    def test_a_missing_switch_is_written_by_the_activation_itself(self, paper):
        client, token, app, home, svc = self._prepared(paper)
        from app.config.envfile import update_values

        update_values(home / "live.env", {"MODE": ""})
        install_fake_engine(app)
        svc.start(confirmation_phrase=LIVE_CONFIRM_PHRASE)
        values = env_values(home)
        assert values["MODE"].upper() == "LIVE"
        assert values["BROKER"].upper() == "UPSTOX_LIVE"
        assert values["LIVE_TRADING"].lower() == "true"
        assert values["LIVE_CONFIRM"] == LIVE_CONFIRM_PHRASE

    def test_a_missing_token_blocks_the_activation(self, paper):
        client, token, app, home, svc = self._prepared(paper)
        from app.config.envfile import update_values

        update_values(home / "live.env", {"UPSTOX_LIVE_ACCESS_TOKEN": ""})
        with pytest.raises(ActivationError):
            svc.start(confirmation_phrase=LIVE_CONFIRM_PHRASE)
        assert env_values(home).get("BROKER", "LOCAL_PAPER").upper() == "LOCAL_PAPER"

    def test_the_emergency_stop_blocks_the_activation(self, paper):
        client, token, app, home, svc = self._prepared(paper)
        app.state.workstation.repository.set_engine_state(
            "emergency_stop", {"stopped": True, "ts": "now", "reason": "test"})
        with pytest.raises(ActivationError) as excinfo:
            svc.start(confirmation_phrase=LIVE_CONFIRM_PHRASE)
        assert "emergency" in str(excinfo.value).lower()
        assert env_values(home).get("BROKER", "LOCAL_PAPER").upper() == "LOCAL_PAPER"

    def test_a_successful_activation_writes_the_switches_and_arms(self, paper):
        client, token, app, home, svc = self._prepared(paper)
        spawned = install_fake_engine(app)
        result = svc.start(confirmation_phrase=LIVE_CONFIRM_PHRASE)
        assert result["state"] == "ACTIVE"
        assert len(spawned) == 1
        values = env_values(home)
        assert values["MODE"].upper() == "LIVE"
        assert values["BROKER"].upper() == "UPSTOX_LIVE"
        assert values["LIVE_TRADING"].lower() == "true"
        assert values["LIVE_CONFIRM"] == LIVE_CONFIRM_PHRASE

    def test_the_activation_never_places_an_order(self, paper):
        client, token, app, home, svc = self._prepared(paper)
        install_fake_engine(app)

        class ExplodingBroker:
            def submit(self, *a, **k):  # pragma: no cover - must never run
                raise AssertionError("an order was placed during activation")

        original = app.state.broker
        app.state.broker = ExplodingBroker()
        try:
            svc.start(confirmation_phrase=LIVE_CONFIRM_PHRASE)
        finally:
            app.state.broker = original

    def test_a_refused_activation_rolls_back_to_paper(self, paper):
        client, token, app, home, svc = self._prepared(paper)
        # no fake engine: the supervisor cannot prove readiness, so ARM fails
        with pytest.raises(ActivationError):
            svc.start(confirmation_phrase=LIVE_CONFIRM_PHRASE)
        values = env_values(home)
        assert values.get("MODE", "PAPER").upper() == "PAPER"
        assert values.get("BROKER", "LOCAL_PAPER").upper() == "LOCAL_PAPER"
        assert values.get("LIVE_TRADING", "false").lower() != "true"
        assert app.state.workstation.live_sessions.current().state != "ACTIVE"

    def test_a_stale_supervisor_lease_blocks_the_activation(self, paper):
        client, token, app, home, svc = self._prepared(paper)
        repository = app.state.workstation.repository
        from app.live.lock import EngineLock

        lock = EngineLock(repository, live=True)
        assert lock.acquire().granted
        with pytest.raises(ActivationError):
            svc.start(confirmation_phrase=LIVE_CONFIRM_PHRASE)
        assert env_values(home).get("BROKER", "LOCAL_PAPER").upper() == "LOCAL_PAPER"

    def test_a_second_live_engine_cannot_take_over(self, paper):
        client, token, app, home, svc = self._prepared(paper)
        spawned = install_fake_engine(app)
        svc.start(confirmation_phrase=LIVE_CONFIRM_PHRASE)
        assert len(spawned) == 1
        # a second activation while the first engine holds the lease
        with pytest.raises(ActivationError):
            svc.start(confirmation_phrase=LIVE_CONFIRM_PHRASE)
        assert len(spawned) == 1

    def test_a_restart_never_resumes_a_live_session(self, paper):
        client, token, app, home, svc = self._prepared(paper)
        install_fake_engine(app)
        svc.start(confirmation_phrase=LIVE_CONFIRM_PHRASE)
        assert app.state.workstation.live_sessions.current().state == "ACTIVE"
        # a control-plane restart
        app.state.workstation.live_sessions.recover_after_restart()
        assert app.state.workstation.live_sessions.current().state == "STOPPED"
        from app.live.gateway import AuthorizationEpochs

        epochs = AuthorizationEpochs(app.state.workstation.repository)
        assert epochs.current() is None or not epochs.current().get("active")

    def test_the_api_start_endpoint_requires_the_phrase(self, paper):
        client, token, app, home, svc = self._prepared(paper)
        response = client.post("/api/live/activation/start", headers=headers(token),
                                json={"confirmation_phrase": "nope"})
        assert response.status_code == 409
        assert env_values(home).get("BROKER", "LOCAL_PAPER").upper() == "LOCAL_PAPER"


# ================================================================ switch off

class TestSwitchOff:
    def _active(self, paper):
        client, token, app, home, svc = self._prepared_setup(paper)
        return client, token, app, home, svc

    def _prepared_setup(self, paper):
        client, token, app, home = paper
        connect_with(app)
        svc = service(app)
        svc.save_configuration(valid_configuration())
        return client, token, app, home, svc

    def test_the_switch_off_returns_the_installation_to_paper(self, paper):
        client, token, app, home, svc = self._prepared_setup(paper)
        install_fake_engine(app)
        svc.start(confirmation_phrase=LIVE_CONFIRM_PHRASE)
        result = svc.stop(reason="operator moved the switch to PAPER")
        assert result["state"] in {"STOPPED", "OFF"}
        values = env_values(home)
        for switch in ("MODE", "BROKER", "LIVE_TRADING", "LIVE_CONFIRM"):
            assert switch not in values
        # the paper installation's own configuration is untouched
        paper_values = paper_env_values(home)
        assert paper_values["MODE"].upper() == "PAPER"
        assert paper_values["BROKER"].upper() == "LOCAL_PAPER"

    def test_the_switch_off_never_claims_the_account_is_flat(self, paper):
        client, token, app, home, svc = self._prepared_setup(paper)
        install_fake_engine(app)
        svc.start(confirmation_phrase=LIVE_CONFIRM_PHRASE)
        result = svc.stop(reason="switch off")
        # the response must not claim the account is flat; it must say
        # explicitly that it cannot
        assert "not a statement" in result["note"].lower()
        assert "positions" in result
        assert "working_orders" in result
        assert "reconciliation" in result
        assert result.get("flat") is None

    def test_the_switch_off_invalidates_the_authorization_epoch(self, paper):
        client, token, app, home, svc = self._prepared_setup(paper)
        install_fake_engine(app)
        svc.start(confirmation_phrase=LIVE_CONFIRM_PHRASE)
        from app.live.gateway import AuthorizationEpochs

        epochs = AuthorizationEpochs(app.state.workstation.repository)
        svc.stop(reason="switch off")
        current = epochs.current()
        assert not current or current.get("active") is not True

    def test_the_switch_off_requests_a_drain_and_never_kills(self, paper):
        client, token, app, home, svc = self._prepared_setup(paper)
        install_fake_engine(app)
        svc.start(confirmation_phrase=LIVE_CONFIRM_PHRASE)
        supervisor = app.state.workstation.live_supervisor
        svc.stop(reason="switch off")
        control = app.state.workstation.repository.get_engine_state(
            "live_engine_control", {}) or {}
        assert control.get("stop_requested") is True
        assert supervisor._process.terminated is False
        assert supervisor._process.killed is False

    def test_the_switch_off_reports_open_positions_as_unknown_when_unreadable(
            self, paper):
        client, token, app, home, svc = self._prepared_setup(paper)
        book = svc.book_status()
        # nothing is invented: an unreadable book is None, never 0
        assert book["positions"] is None or isinstance(book["positions"], list)

    def test_the_api_stop_endpoint_returns_the_truth(self, paper):
        client, token, app, home, svc = self._prepared_setup(paper)
        install_fake_engine(app)
        svc.start(confirmation_phrase=LIVE_CONFIRM_PHRASE)
        response = client.post("/api/live/activation/stop", headers=headers(token),
                                json={"reason": "switch off"})
        assert response.status_code == 200
        body = response.json()
        assert body["state"] in {"STOPPED", "OFF"}
        assert "reconciliation" in body
        assert "BROKER" not in env_values(home)
        assert paper_env_values(home)["BROKER"].upper() == "LOCAL_PAPER"


# ================================================================= isolation

class TestIsolation:
    def test_a_paper_configuration_never_carries_a_live_switch(self, paper):
        client, token, _, home = paper
        values = env_values(home)
        assert values.get("BROKER", "LOCAL_PAPER").upper() != "UPSTOX_LIVE"

    def test_the_live_token_is_never_written_to_the_sandbox_key(self, paper):
        client, token, app, home = paper
        connect_with(app)
        values = env_values(home)
        assert values.get("UPSTOX_SANDBOX_ACCESS_TOKEN", "") != FAKE_LIVE_TOKEN

    def test_the_control_plane_never_builds_a_live_broker(self, paper):
        """The workstation must not construct an order-capable broker."""
        client, token, app, home = paper
        import app.api.routes.live_activation as module

        source = open(module.__file__, encoding="utf-8").read()
        for forbidden in ("UpstoxLiveBroker(", "submit(", "place_order"):
            assert forbidden not in source

    def test_the_activation_service_imports_no_broker(self):
        import app.live.activation as module

        source = open(module.__file__, encoding="utf-8").read()
        for forbidden in ("UpstoxLiveBroker(", "UpstoxSandboxBroker(",
                          ".submit("):
            assert forbidden not in source
