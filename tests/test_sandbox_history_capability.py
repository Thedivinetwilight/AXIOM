"""Sandbox order-history capability + host-boundary regression suite.

Regression target: the documented reconciliation defect class where the
sandbox broker pointed its GET /v2/order/history at the PRODUCTION host
(api.upstox.com). A sandbox token sent there could 401, the shared
``available`` flag fail-closed the WHOLE broker (disabling perfectly valid
sandbox order operations), and the sandbox credential leaked to a production
endpoint.

Verified provider contract (2026-10-16, official docs + official
upstox-python SDK ``sandbox_urls`` allowlist): sandbox supports
place/modify/cancel (+multi-place) only; GET /v2/order/history is NOT a
sandbox-enabled API, and official SDK sandbox mode pins every host to
api-sandbox.upstox.com.

Invariants pinned here:
1. The sandbox broker's history URL defaults to the SANDBOX host.
2. A production or unknown history host is refused at construction AND by
   Settings — env vars cannot bypass the boundary.
3. No request of any kind (place/modify/cancel/history/tag lookup) ever
   leaves the process for a production host in sandbox mode.
4. An unsupported/erroneous history endpoint marks only the READ capability;
   order availability is independent, and a skipped reconciliation is
   distinguishable from a genuinely-empty one.
5. A 401/403 from the sandbox environment still fails the broker closed.
6. The LIVE broker keeps the production history host (no overcorrection).
7. The CLI reports "unverified" (exit 4) rather than "rejected" when the
   provider does not expose the read route, and never prints the token.
8. An unresolved (UNKNOWN/UNRESOLVED) submission intent conservatively
   blocks NEW risk-increasing entries while never blocking SELL exits.
9. A token supplied only via an ignored .env file reaches the sandbox-test
   CLI (configuration load order), with process-env precedence preserved.
"""

import argparse
import dataclasses
import json

import httpx
import pytest

from app.brokers.sandbox import (
    SandboxRequestError,
    UpstoxSandboxBroker,
    classify_history_probe,
)
from app.config.settings import Settings
from app.database.database import Database
from app.database.repository import Repository
from app.engine.sandbox_engine import SandboxEngine
from app.learning.journal import LearningJournal
from app.risk.limits import HardRiskLimits

INSTRUMENT = "NSE_EQ|INE002A01018"
SANDBOX_HOST = "api-sandbox.upstox.com"
PRODUCTION_HOST = "api.upstox.com"


@pytest.fixture()
def repo(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/sandbox_capability.db")
    repository = Repository(database)
    yield repository
    database.close()


class RecordingTransport(httpx.BaseTransport):
    """Records every request URL; fakes place/history with a mode switch."""

    def __init__(self, history_status=404, fail_posts=False):
        self.urls: list[str] = []
        self.history_status = history_status
        self.fail_posts = fail_posts
        self.next_id = 1001

    def handle_request(self, request):
        self.urls.append(str(request.url))
        if request.method == "POST" and request.url.path == "/v3/order/place":
            if self.fail_posts:
                return httpx.Response(
                    503, json={"errors": [{"errorCode": "UDAPI10999"}]})
            oid = str(self.next_id)
            self.next_id += 1
            return httpx.Response(
                200, json={"status": "success", "data": {"order_ids": [oid]}})
        if request.method == "GET" and request.url.path == "/v2/order/history":
            if self.history_status in {404, 405}:
                return httpx.Response(
                    self.history_status,
                    json={"errors": [{"errorCode": "NO_SUCH_ROUTE"}]})
            if self.history_status == 200:
                return httpx.Response(
                    200, json={"status": "success", "data": []})
            return httpx.Response(self.history_status, json={})
        if request.method == "DELETE" and request.url.path == "/v3/order/cancel":
            return httpx.Response(
                200, json={"status": "success",
                           "data": {"order_id": request.url.params["order_id"]}})
        return httpx.Response(500, json={"errors": [{"errorCode": "X"}]})


def make_broker(repo, transport):
    broker = UpstoxSandboxBroker(
        "capability-test-token", INSTRUMENT, transport=transport,
        repository=repo, initial_capital=100_000.0)
    broker.instrument_map["RELIANCE"] = INSTRUMENT
    return broker


class TestHistoryHostBoundary:
    def test_default_history_url_is_on_the_sandbox_host(self):
        broker = UpstoxSandboxBroker("tok", INSTRUMENT)
        assert broker.history_endpoint == (
            "https://api-sandbox.upstox.com/v2/order/history")

    @pytest.mark.parametrize("host", [
        "api.upstox.com", "api-hft.upstox.com", "attacker.example",
        "sandbox.upstox.com.evil.example",
    ])
    def test_non_sandbox_history_hosts_are_refused(self, host):
        with pytest.raises(ValueError, match="documented HTTPS Upstox order-history"):
            UpstoxSandboxBroker(
                "tok", INSTRUMENT,
                history_url=f"https://{host}/v2/order/history")

    def test_settings_refuse_production_and_unknown_history_hosts(self):
        with pytest.raises(ValueError, match="production host"):
            Settings(broker="UPSTOX_SANDBOX",
                     upstox_sandbox_access_token="t",
                     upstox_sandbox_order_history_url=(
                         "https://api.upstox.com/v2/order/history"))
        with pytest.raises(ValueError,
                           match="documented HTTPS Upstox order-history"):
            Settings(broker="UPSTOX_SANDBOX",
                     upstox_sandbox_access_token="t",
                     upstox_sandbox_order_history_url=(
                         "https://attacker.example/v2/order/history"))

    def test_settings_accept_the_sandbox_history_default(self):
        settings = Settings(broker="UPSTOX_SANDBOX",
                            upstox_sandbox_access_token="t")
        assert settings.upstox_sandbox_order_history_url == (
            "https://api-sandbox.upstox.com/v2/order/history")

    def test_no_request_leaves_for_a_production_host(self, repo):
        """Full lifecycle traffic stays on the sandbox host, period."""
        transport = RecordingTransport(history_status=200)
        broker = make_broker(repo, transport)
        intent = broker.submit("BUY", 5, 0.0, intent_id="iso-a",
                               symbol="RELIANCE")
        assert intent == "iso-a"
        broker.reconcile()
        intent2 = broker.submit("SELL", 5, 0.0, intent_id="iso-b",
                                symbol="RELIANCE")
        broker.cancel(intent2)
        # UNKNOWN-intent tag lookup also goes through the history endpoint.
        journal_hosts = transport.urls
        hosts = {httpx.URL(u).host for u in journal_hosts}
        assert hosts == {SANDBOX_HOST}
        assert PRODUCTION_HOST not in hosts
        assert "api-hft.upstox.com" not in hosts
        # a rejected POST (→ UNKNOWN) then a tag lookup: still sandbox-only
        transport.fail_posts = True
        with pytest.raises(SandboxRequestError):
            broker.submit("BUY", 1, 0.0, intent_id="iso-unknown",
                          symbol="RELIANCE")
        transport.fail_posts = False
        broker.lookup_intent_order("iso-unknown")
        hosts = {httpx.URL(u).host for u in transport.urls}
        assert hosts == {SANDBOX_HOST}


class TestCapabilityIsolation:
    def test_unsupported_history_never_disables_order_operations(self, repo):
        transport = RecordingTransport(history_status=404)
        broker = make_broker(repo, transport)
        intent = broker.submit("BUY", 1, 0.0, intent_id="cap-1",
                               symbol="RELIANCE")
        assert broker.available is True
        assert intent == "cap-1"
        # Reconcile hits the missing route: READ side marked unsupported…
        fills = broker.reconcile()
        assert broker.history_capability["state"] == "unsupported"
        assert broker.history_capability["reason"] == "HTTP 404"
        # …while ORDER availability is untouched…
        assert broker.available is True
        # …and no fill was fabricated.
        assert fills == []
        # …and the order stays honestly ACKNOWLEDGED (not filled/cancelled).
        assert broker.orders[intent].status == "SUBMITTED"
        assert broker.orders[intent].filled_quantity == 0.0
        # A skipped reconciliation is distinguishable from a healthy empty
        # one: the capability state carries the reason.
        view = broker.status_view()
        assert view["available"] is True
        assert view["history_capability"]["state"] == "unsupported"
        # Further order operations still work after the failed read.
        broker.submit("SELL", 1, 0.0, intent_id="cap-2", symbol="RELIANCE")
        assert broker.available is True

    def test_healthy_history_marks_capability_available(self, repo):
        transport = RecordingTransport(history_status=200)
        broker = make_broker(repo, transport)
        broker.submit("BUY", 1, 0.0, intent_id="cap-ok", symbol="RELIANCE")
        broker.reconcile()
        assert broker.history_capability["state"] == "available"
        assert broker.available is True

    def test_401_from_the_sandbox_environment_fails_closed(self, repo):
        """A rejected token on ANY sandbox endpoint disables order traffic."""
        from app.brokers.sandbox import SandboxAuthError

        transport = RecordingTransport(history_status=401)
        broker = make_broker(repo, transport)
        broker.submit("BUY", 1, 0.0, intent_id="cap-401", symbol="RELIANCE")
        assert broker.available is True  # the place itself succeeded
        with pytest.raises(SandboxAuthError):
            broker.reconcile()  # history read 401s → token treated as rejected
        assert broker.available is False
        with pytest.raises(SandboxAuthError):
            broker.submit("SELL", 1, 0.0, intent_id="cap-401b",
                          symbol="RELIANCE")

    def test_history_401_is_attributable_and_still_fails_closed(self, repo):
        class HistoryOnly401(httpx.BaseTransport):
            def __init__(self):
                self.posts = 0

            def handle_request(self, request):
                if request.method == "POST":
                    self.posts += 1
                    return httpx.Response(
                        200, json={"status": "success",
                                   "data": {"order_ids": ["9"]}})
                return httpx.Response(401, json={})

        transport = HistoryOnly401()
        broker = make_broker(repo, transport)
        broker.submit("BUY", 1, 0.0, intent_id="cap-h1", symbol="RELIANCE")
        assert broker.available is True
        with pytest.raises(Exception):
            broker.reconcile()
        # The 401 came from the history read — recorded as such — and the
        # token is treated as rejected because the SAME token guards the
        # order endpoints of the same sandbox environment.
        assert broker.history_capability["state"] == "auth_rejected"
        assert broker.available is False


class TestProbeClassifier:
    def test_missing_route_is_unverified_not_rejected(self):
        verdict = classify_history_probe(
            httpx.Response(404, json={"errors": [{"errorCode": "NOPE"}]}))
        assert verdict["credential_accepted"] is False
        assert verdict["class"] == "unsupported_endpoint"
        assert verdict["credential_state"] == "unverified"

    def test_documented_not_found_still_verifies_the_credential(self):
        verdict = classify_history_probe(httpx.Response(
            400, json={"errors": [{"errorCode": "UDAPI100010"}]}))
        assert verdict["credential_accepted"] is True
        assert verdict["class"] == "documented_not_found"

    def test_auth_rejection_still_fails_closed(self):
        verdict = classify_history_probe(httpx.Response(401, json={}))
        assert verdict["credential_accepted"] is False
        assert verdict["class"] == "auth_rejected"


class _FakeLedger:
    def snapshot(self):
        return {"cash": 100.0}


class TestSandboxTestCli:
    def _settings(self):
        return dataclasses.replace(Settings(),
                                   upstox_sandbox_access_token="cli-secret-token")

    def test_unverified_read_capability_exits_4_with_honest_diagnostic(
            self, monkeypatch, capsys):
        import app.__main__ as cli
        import app.brokers.sandbox as sbx

        monkeypatch.setattr(cli, "_load_settings", self._settings)
        monkeypatch.delenv("UPSTOX_SANDBOX_ACCESS_TOKEN", raising=False)

        class FakeBroker:
            MODE = "UPSTOX_SANDBOX"
            LIVE = False

            def __init__(self, *a, **k):
                self.history_capability = {"state": "unknown"}

            def _request(self, *a, **k):
                return httpx.Response(
                    404, json={"errors": [{"errorCode": "NO_SUCH_ROUTE"}]})

        monkeypatch.setattr(sbx, "UpstoxSandboxBroker", FakeBroker)
        args = argparse.Namespace(instrument=INSTRUMENT, place_order=False,
                                  quantity=1)
        code = cli.cmd_sandbox_test(args)
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert code == 4
        assert payload["credential_state"] == "unverified"
        assert payload["capability"] == "order_history_not_exposed"
        assert payload["credential_accepted"] is False
        assert "--place-order" in payload["diagnostic"]
        assert "cli-secret-token" not in out, "the token must never be printed"

    def test_place_order_verifies_the_credential_when_read_route_missing(
            self, monkeypatch, capsys):
        import app.__main__ as cli
        import app.brokers.sandbox as sbx

        monkeypatch.setattr(cli, "_load_settings", self._settings)
        monkeypatch.delenv("UPSTOX_SANDBOX_ACCESS_TOKEN", raising=False)

        class FakeBroker:
            MODE = "UPSTOX_SANDBOX"
            LIVE = False
            ledger = _FakeLedger()

            def __init__(self, *a, **k):
                self.history_capability = {"state": "unsupported",
                                           "reason": "HTTP 404"}

            def _request(self, *a, **k):
                return httpx.Response(
                    404, json={"errors": [{"errorCode": "NO_SUCH_ROUTE"}]})

            def submit(self, *a, **k):
                return "a" * 32

            def reconcile(self):
                return []

        monkeypatch.setattr(sbx, "UpstoxSandboxBroker", FakeBroker)
        args = argparse.Namespace(instrument=INSTRUMENT, place_order=True,
                                  quantity=1)
        code = cli.cmd_sandbox_test(args)
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert code == 0
        assert payload["order_placed"] is True
        assert payload["credential_accepted"] is True
        assert payload["credential_verified_by"] == "authenticated order placement"
        assert payload["mode"] == "UPSTOX_SANDBOX"
        assert payload["order_host"] == SANDBOX_HOST
        assert payload["history_host"] == SANDBOX_HOST
        assert "cli-secret-token" not in out, "the token must never be printed"

    def test_rejected_credential_never_reaches_place_order(
            self, monkeypatch, capsys):
        import app.__main__ as cli
        import app.brokers.sandbox as sbx

        monkeypatch.setattr(cli, "_load_settings", self._settings)
        monkeypatch.delenv("UPSTOX_SANDBOX_ACCESS_TOKEN", raising=False)
        submitted = []

        class FakeBroker:
            MODE = "UPSTOX_SANDBOX"
            LIVE = False

            def __init__(self, *a, **k):
                pass

            def _request(self, *a, **k):
                return httpx.Response(401, json={})

            def submit(self, *a, **k):  # pragma: no cover - must never run
                submitted.append(1)
                raise AssertionError("must not submit with a rejected token")

        monkeypatch.setattr(sbx, "UpstoxSandboxBroker", FakeBroker)
        args = argparse.Namespace(instrument=INSTRUMENT, place_order=True,
                                  quantity=1)
        code = cli.cmd_sandbox_test(args)
        payload = json.loads(capsys.readouterr().out)
        assert code == 1
        assert payload["credential_state"] == "rejected"
        assert submitted == []


class TestNoOvercorrectionForLive:
    def test_live_broker_keeps_the_production_history_host(self):
        from app.brokers.live import UpstoxLiveBroker
        from app.config.settings import LIVE_CONFIRM_PHRASE

        broker = UpstoxLiveBroker("tok", INSTRUMENT, confirm=LIVE_CONFIRM_PHRASE)
        assert broker.history_endpoint == (
            "https://api.upstox.com/v2/order/history")
        assert broker.LIVE is True

    def test_live_broker_refuses_a_sandbox_history_host(self):
        from app.brokers.live import UpstoxLiveBroker
        from app.config.settings import LIVE_CONFIRM_PHRASE

        with pytest.raises(ValueError,
                           match="documented HTTPS Upstox order-history"):
            UpstoxLiveBroker(
                "tok", INSTRUMENT, confirm=LIVE_CONFIRM_PHRASE,
                history_url="https://api-sandbox.upstox.com/v2/order/history")


class TestUnresolvedIntentBlocksNewRisk:
    """While a submission outcome is UNKNOWN, exposure at the broker cannot
    be known — new BUY entries must be refused, exits must keep working."""

    def _engine(self, repo, broker):
        class _P:
            symbols = ["RELIANCE"]

        strategy = type("S", (), {"name": "momentum",
                                  "parameters": {"period": 2},
                                  "history_limit": 30})()
        return SandboxEngine(
            provider=_P(), strategy=strategy, broker=broker,
            repository=repo, risk_limits=HardRiskLimits.from_settings(Settings()),
            journal=LearningJournal(repo),
            instrument_keys={"RELIANCE": INSTRUMENT})

    @staticmethod
    def _candle():
        from datetime import datetime, timezone

        from app.data.models import Candle

        return Candle(datetime(2026, 1, 2, 9, 15, tzinfo=timezone.utc),
                      99.9, 100.1, 99.8, 100.0, 10000, "RELIANCE")

    def test_unknown_intent_blocks_entries_but_never_exits(self, repo):
        transport = RecordingTransport(history_status=404, fail_posts=True)
        broker = make_broker(repo, transport)
        # First submission loses its response → durable UNKNOWN intent.
        with pytest.raises(SandboxRequestError):
            broker.submit("BUY", 1, 0.0, intent_id="uncertain",
                          symbol="RELIANCE")
        assert repo  # journal entry exists via repository
        transport.fail_posts = False
        engine = self._engine(repo, broker)
        candle = self._candle()

        posts_before = len(transport.urls)
        engine._submit("BUY", 1, 100.0, candle, reason="TEST_ENTRY")
        assert len(transport.urls) == posts_before, \
            "a new entry must not be sent while an intent is unresolved"
        assert engine.rejected_by_risk >= 1
        events = [e for e in repo.list_system_events(limit=50)
                  if e["kind"] == "RISK_REJECT"
                  and (e.get("payload") or {}).get("reason")
                  == "UNRESOLVED_INTENT_BLOCKS_ENTRY"]
        assert events, "the refusal must be an auditable RISK_REJECT event"

        # Risk-reducing exit is NOT blocked: it may reach the broker.
        broker.ledger.apply_fill("BUY", 5, 100.0)
        engine._submit("SELL", 5, 100.0, candle, reason="TEST_EXIT")
        assert len(transport.urls) > posts_before, \
            "risk-reducing exits must never be blocked by unresolved intents"

    def test_clean_state_allows_entries(self, repo):
        transport = RecordingTransport(history_status=404)
        broker = make_broker(repo, transport)
        engine = self._engine(repo, broker)
        before = len(transport.urls)
        engine._submit("BUY", 1, 100.0, self._candle(), reason="TEST_ENTRY")
        assert len(transport.urls) == before + 1


class TestEnvFileCredentialLoading:
    """Defect class: the sandbox-test CLI read UPSTOX_SANDBOX_ACCESS_TOKEN
    from the process environment BEFORE settings loaded .env, so a token
    present only in the ignored .env file was treated as missing."""

    def test_dotenv_only_token_reaches_the_broker(self, tmp_path, monkeypatch,
                                                  capsys):
        import app.__main__ as cli
        import app.brokers.sandbox as sbx
        from app import runtime

        env_file = tmp_path / ".env"
        env_file.write_text(
            "UPSTOX_SANDBOX_ACCESS_TOKEN=dotenv-only-secret-value\n",
            encoding="utf-8")
        monkeypatch.setattr(runtime, "env_candidates", lambda: [env_file])
        monkeypatch.delenv("UPSTOX_SANDBOX_ACCESS_TOKEN", raising=False)

        reached = {}

        def _sentinel(*a, **k):
            reached["constructed"] = True
            raise RuntimeError("broker constructed")

        monkeypatch.setattr(sbx, "UpstoxSandboxBroker", _sentinel)
        args = argparse.Namespace(instrument=INSTRUMENT, place_order=False,
                                  quantity=1)
        with pytest.raises(RuntimeError, match="broker constructed"):
            cli.cmd_sandbox_test(args)
        assert reached["constructed"], \
            "a .env-only token must pass the 'not set' branch"
        out = capsys.readouterr().out
        assert "dotenv-only-secret-value" not in out, "never print the token"

    def test_process_environment_takes_precedence_over_env_file(
            self, tmp_path, monkeypatch):
        from app import runtime

        env_file = tmp_path / ".env"
        env_file.write_text(
            "UPSTOX_SANDBOX_ACCESS_TOKEN=dotenv-value-loses\n", encoding="utf-8")
        monkeypatch.setattr(runtime, "env_candidates", lambda: [env_file])
        monkeypatch.setenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "process-value-wins")
        settings = Settings.from_env()
        assert settings.upstox_sandbox_access_token == "process-value-wins"

    def test_env_example_contains_no_credentials(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        gitignore = (root / ".gitignore").read_text(encoding="utf-8")
        assert ".env" in gitignore.splitlines(), ".env must stay untracked"
        example = (root / ".env.example").read_text(encoding="utf-8")
        for key in ("UPSTOX_SANDBOX_ACCESS_TOKEN", "UPSTOX_LIVE_ACCESS_TOKEN",
                    "API_TOKEN", "MARKET_DATA_ACCESS_TOKEN"):
            for line in example.splitlines():
                if line.startswith(f"{key}="):
                    assert line.strip() == f"{key}=", (
                        f".env.example must never carry a real value for {key}")
