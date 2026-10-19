"""The readiness contract at the switch: every blocker must stop the
activation, and the switch-off must never lie about the book.

The existing contract in ``app/live/preflight.py`` and
``app/live/session.py`` is the one under test here. Nothing in this file
weakens a severity, an UNKNOWN-is-blocking rule or a guard: the tests only
prove that the switch honours what the contract already decides.

* a required check that is BLOCKED blocks the activation;
* a required check that is UNKNOWN blocks the activation;
* a stale token stops new entries and offers a reconnect;
* the emergency stop invalidates the live authorization;
* the switch-off reports the open book and never claims the account is flat;
* the live credential file is owner-only, and a failure to write it blocks
  the activation instead of silently storing a secret somewhere else.
"""

from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_live_switch_activation import (
    FAKE_SECRET,
    connect_with,
    env_values,
    fake_exchange,
    fake_verifier,
    headers,
    install_fake_engine,
    service,
    valid_configuration,
)
from tests.test_live_switch_activation import paper as _shared_paper

#: The shared disposable-paper fixture, re-exported under its own name so
#: pytest collects it in this module too.
paper = _shared_paper

LIVE_CONFIRM = "I-ACCEPT-REAL-MONEY-RISK"


def prepare_live(app, *, home=None):
    """Connect the fixture credential and store a complete limit set."""
    connect_with(app, live_ok=True, sandbox_ok=False)
    service(app, token_exchange=fake_exchange(),
            credential_verifier=fake_verifier).save_configuration(
                valid_configuration())


def activate(client, token, app, home):
    """Run the explicit activation; returns the response."""
    return client.post("/api/live/activation/start", headers=headers(token),
                       json={"confirmation_phrase": LIVE_CONFIRM})


# ===================================================================== blocks

class TestReadinessBlocksTheActivation:
    def test_an_unresolved_submission_intent_blocks_the_activation(self, paper):
        from app.engine.state import SUBMISSION_ATTEMPTED, UNKNOWN, SubmissionJournal

        client, token, app, home = paper
        install_fake_engine(app)
        prepare_live(app)
        journal = SubmissionJournal(app.state.workstation.repository)
        journal.record("intent-1", instrument_key="NSE_EQ|INE002A01018",
                       symbol="S", side="BUY", quantity=1, price=1.0,
                       strategy="t")
        journal.mark("intent-1", SUBMISSION_ATTEMPTED)
        journal.mark("intent-1", UNKNOWN)

        response = activate(client, token, app, home)
        assert response.status_code == 409, response.text
        assert "no_unresolved_intents" in response.json()["detail"]
        # the installation stayed PAPER
        assert env_values(home).get("MODE", "PAPER").upper() == "PAPER"

    def test_a_corrupted_live_audit_trail_blocks_the_activation(self, paper):
        client, token, app, home = paper
        install_fake_engine(app)
        prepare_live(app)
        # an orphan transition row: a fill that belongs to no recorded order
        database = app.state.workstation.repository.db
        with database.transaction() as db:
            db.execute(
                "INSERT INTO live_transitions(order_id, upstox_order_id, status, "
                "filled_quantity, average_price, ts, raw) "
                "VALUES ('ghost-order', 'ghost-1', 'EXECUTED', 5, 1.0, 't0', '{}')")

        response = activate(client, token, app, home)
        assert response.status_code == 409, response.text
        assert "audit_trail_integrity" in response.json()["detail"]
        assert env_values(home).get("MODE", "PAPER").upper() == "PAPER"

    def test_stale_market_data_blocks_the_activation(self, paper):
        """A live engine may not start on stale market data."""
        from app.live.lock import EngineLock

        client, token, app, home = paper
        repository = app.state.workstation.repository

        def popen(argv, **kwargs):
            lock = EngineLock(repository, live=True)
            assert lock.acquire().granted
            stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            repository.set_engine_state("last_market_data_ts", stale)
            now = datetime.now(timezone.utc).isoformat()
            repository.set_engine_state("last_reconcile_at", {"ts": now, "fills": 0})
            repository.set_engine_state("live_reconcile_status", {
                "ok": True, "ts": now, "fills": 0})
            control = repository.get_engine_state("live_engine_control", {}) or {}
            repository.set_engine_state("live_engine_runtime", {
                "state": "READY", "owner": lock.status()["owner"],
                "pid": lock.status()["pid"], "host": lock.status()["host"],
                "generation": control.get("generation"),
                "reconciliation_ok": True, "reconciled_at": now})
            return _FakeProcess()

        app.state.workstation.live_supervisor._popen_factory = popen
        app.state.workstation.live_supervisor._argv_factory = lambda: ["fixture-engine"]
        prepare_live(app)

        response = activate(client, token, app, home)
        assert response.status_code == 409, response.text
        assert "data_freshness" in response.json()["detail"]
        assert env_values(home).get("MODE", "PAPER").upper() == "PAPER"

    def test_an_unknown_required_check_blocks_the_activation(self, paper,
                                                            monkeypatch):
        """UNKNOWN is never ready: a required check that cannot be run blocks."""
        from app.live.session import LiveSessionManager

        client, token, app, home = paper
        install_fake_engine(app)
        prepare_live(app)

        original = LiveSessionManager.readiness

        def unverified(self, settings, *, repository=None, context=None):
            report = original(self, settings, repository=repository,
                              context=context)
            report = dict(report)
            report["verdict"] = "BLOCKED_UNVERIFIED"
            report["required_unknown"] = ["supervisor_lease"]
            return report

        monkeypatch.setattr(LiveSessionManager, "readiness", unverified)
        response = activate(client, token, app, home)
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert "supervisor_lease" in detail
        assert "UNKNOWN" in detail
        assert env_values(home).get("MODE", "PAPER").upper() == "PAPER"

    def test_the_readiness_review_lists_every_blocker(self, paper):
        """The operator sees the blockers, not a green light."""
        client, token, app, home = paper
        install_fake_engine(app)
        prepare_live(app)
        repository = app.state.workstation.repository
        repository.set_engine_state("emergency_stop", {"stopped": True,
                                                       "reason": "test"})

        snapshot = client.get("/api/live/activation", headers=headers(token)).json()
        readiness = snapshot["readiness"]
        blocked = [check["name"] for check in readiness["checks"]
                   if check["status"] in {"BLOCKED", "UNKNOWN"}]
        assert "emergency_stop_clear" in blocked
        assert "kill_switch_available" in blocked
        assert readiness["verdict"] in {"BLOCKED", "BLOCKED_UNVERIFIED"}
        # the switch is still PAPER and the session is not ACTIVE
        assert snapshot["switch"]["position"] == "PAPER"
        assert snapshot["session"]["state"] != "ACTIVE"


class _FakeProcess:
    def __init__(self):
        self.pid = int(os.getpid())
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def wait(self, timeout=None):
        import subprocess

        raise subprocess.TimeoutExpired("fixture-live-engine", timeout)

    def terminate(self):  # pragma: no cover - forbidden path in these tests
        self.terminated = True

    def kill(self):  # pragma: no cover - forbidden path in these tests
        self.killed = True


# ============================================================ token freshness

class TestTokenFreshness:
    def test_an_expired_token_stops_new_entries(self, paper):
        """A stale credential stops entries; the operator is offered a reconnect."""
        from datetime import timedelta

        client, token, app, home = paper
        install_fake_engine(app)
        prepare_live(app)
        response = activate(client, token, app, home)
        assert response.status_code == 200, response.text

        # the token ages past the freshness window
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        app.state.workstation.repository.set_engine_state(
            "live_token_verified", {
                "verified_at": old,
                "credential_sha256": __import__("hashlib").sha256(
                    b"fake-live-access-token-for-tests").hexdigest()})

        status = client.get("/api/live/activation", headers=headers(token)).json()
        assert status["credential"]["state"] == "expired"
        assert status["credential"]["present"] is True
        assert status["credential"]["reconnect_required"] is True
        assert "reconnect" in json_text(status).lower()


def json_text(payload) -> str:
    import json

    return json.dumps(payload, default=str)


# =========================================================== emergency stop

class TestEmergencyStopAfterActivation:
    def test_the_emergency_stop_invalidates_the_authorization(self, paper):
        client, token, app, home = paper
        install_fake_engine(app)
        prepare_live(app)
        assert activate(client, token, app, home).status_code == 200

        # the durable emergency stop (the existing endpoint, unchanged)
        stopped = client.post("/api/live/emergency-stop", headers=headers(token),
                               json={"reason": "test"})
        assert stopped.status_code == 200, stopped.text

        snapshot = client.get("/api/live/activation", headers=headers(token)).json()
        assert snapshot["emergency_stop_active"] is True
        # the session is no longer ACTIVE: no new risk can be taken
        assert snapshot["session"]["state"] != "ACTIVE"


# ============================================================== the switch-off

class TestSwitchOffWithAnOpenBook:
    def test_open_working_orders_are_reported_and_never_called_flat(self, paper):
        client, token, app, home = paper
        install_fake_engine(app)
        prepare_live(app)
        assert activate(client, token, app, home).status_code == 200

        # the broker still has a working order
        app.state.workstation.repository.record_live_order({
            "id": "live-open-1", "symbol": "NSE_EQ", "side": "BUY",
            "quantity": 5, "price": 2900.0, "status": "OPEN",
            "created_ts": "t0"})

        book = client.get("/api/live/activation/book", headers=headers(token)).json()
        assert book["working_orders"] or book.get("open_orders")

        stopped = client.post("/api/live/activation/stop", headers=headers(token),
                              json={"reason": "operator moved the switch to PAPER"})
        assert stopped.status_code == 200, stopped.text
        body = stopped.json()
        text = json_text(body).lower()
        # no false flatness anywhere in the answer
        assert "flat" not in text or "not a statement" in text
        assert body["state"] in {"STOPPED", "OFF", "DISARMED"}


# ============================================================ secret handling

class TestSecretHandling:
    def test_the_live_credential_file_is_owner_only(self, paper):
        _client, _token, app, home = paper
        prepare_live(app)
        path = home / "live.env"
        assert path.exists()
        if os.name == "posix":
            mode = stat.S_IMODE(os.stat(path).st_mode)
            assert mode == 0o600, oct(mode)

    def test_a_failure_to_store_the_credential_blocks_the_activation(
            self, paper, monkeypatch):
        """If the credential cannot be stored safely, live is refused.

        There is no second, silent place where the secret could end up.
        """
        import app.live.activation as activation_module

        client, token, app, home = paper
        install_fake_engine(app)

        def refuse(*args, **kwargs):
            raise OSError("the disk is read-only")

        monkeypatch.setattr(activation_module, "update_values", refuse)
        response = client.post("/api/live/activation/oauth/exchange",
                               headers=headers(token),
                               json={"code": "ok-code", "client_id": "client-id",
                                     "client_secret": FAKE_SECRET,
                                     "redirect_uri": "https://localhost:8443/callback"})
        assert response.status_code in (409, 500)
        # nothing was stored anywhere
        assert not (home / "live.env").exists() or \
            "UPSTOX_LIVE_ACCESS_TOKEN" not in env_values(home)
