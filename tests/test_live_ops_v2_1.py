"""V2.1 L-ops: alerts, entry guards, watchdog, breach monitor, preflight.

The theme of every test here is the same: an operational rail must fail CLOSED
for new entries, must NEVER block an exit, and must NEVER raise into the trading
loop. Nothing contacts a network: the webhook is exercised with an httpx mock
transport and everything else is pure.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config.settings import Settings
from app.database.database import Database
from app.database.repository import Repository
from app.live.alerts import AlertRouter, recent_alerts
from app.live.guards import CANARY_KEY, TOKEN_KEY, CanaryGuard, TokenGuard
from app.live.ops import LiveOperations
from app.live.preflight import ERROR, WARNING, run_checks, summarize
from app.live.watchdog import BreachMonitor, LiveWatchdog

NOW = datetime(2026, 10, 2, 9, 15, tzinfo=timezone.utc)


def repo(tmp_path, name="lops.db"):
    database = Database(f"sqlite:///{tmp_path}/{name}")
    return Repository(database), database


class TestAlertRouter:
    def test_alert_is_recorded_in_the_durable_sink(self, tmp_path):
        repository, database = repo(tmp_path)
        router = AlertRouter(repository, now_fn=lambda: NOW)
        alert = router.emit("TEST_KIND", "CRITICAL", "something happened", x=1)
        assert alert is not None and alert.severity == "CRITICAL"

        events = [e for e in repository.list_system_events()
                  if e["kind"] == AlertRouter.EVENT_KIND]
        assert len(events) == 1
        assert events[0]["payload"]["kind"] == "TEST_KIND"
        assert events[0]["payload"]["message"] == "something happened"
        assert events[0]["payload"]["details"]["x"] == 1
        assert recent_alerts(repository)[0]["kind"] == "TEST_KIND"
        database.close()

    def test_identical_alerts_are_deduplicated_inside_the_window(self, tmp_path):
        repository, database = repo(tmp_path)
        clock = {"now": NOW}
        router = AlertRouter(repository, dedup_seconds=300,
                             now_fn=lambda: clock["now"])
        assert router.emit("FLOOD", "CRITICAL", "first") is not None
        assert router.emit("FLOOD", "CRITICAL", "second") is None
        assert router.suppressed == 1
        # a different kind is NOT suppressed
        assert router.emit("OTHER", "CRITICAL", "third") is not None
        # and the window expires
        clock["now"] = NOW + timedelta(seconds=301)
        assert router.emit("FLOOD", "CRITICAL", "fourth") is not None
        database.close()

    def test_webhook_only_receives_alerts_at_or_above_min_severity(self, tmp_path):
        repository, database = repo(tmp_path)
        calls = []

        class Transport(httpx.BaseTransport):
            def handle_request(self, request):
                calls.append(json.loads(request.content))
                return httpx.Response(200, json={"ok": True})

        router = AlertRouter(repository, webhook_url="https://ops.example/hook",
                             min_severity="WARNING",
                             transport=httpx.Client(transport=Transport()),
                             now_fn=lambda: NOW)
        router.emit("INFO_ONLY", "INFO", "not delivered")
        router.emit("WARN", "WARNING", "delivered")
        assert [c["kind"] for c in calls] == ["WARN"]
        assert calls[0]["severity"] == "WARNING"
        database.close()

    def test_webhook_failure_is_recorded_and_never_raises(self, tmp_path):
        repository, database = repo(tmp_path)

        class Boom(httpx.BaseTransport):
            def handle_request(self, request):
                raise httpx.ConnectError("no route")

        router = AlertRouter(repository, webhook_url="https://ops.example/hook",
                             transport=httpx.Client(transport=Boom()),
                             now_fn=lambda: NOW)
        assert router.emit("K", "CRITICAL", "m") is not None  # no exception
        assert router.delivery_failures == 1
        kinds = {e["kind"] for e in repository.list_system_events()}
        assert "ALERT_DELIVERY_FAILURE" in kinds
        database.close()

    def test_webhook_http_error_is_a_delivery_failure(self, tmp_path):
        repository, database = repo(tmp_path)

        class Bad(httpx.BaseTransport):
            def handle_request(self, request):
                return httpx.Response(500, text="nope")

        router = AlertRouter(repository, webhook_url="https://ops.example/hook",
                             transport=httpx.Client(transport=Bad()),
                             now_fn=lambda: NOW)
        router.emit("K", "CRITICAL", "m")
        assert router.delivery_failures == 1
        database.close()

    def test_secret_shaped_keys_are_scrubbed_before_delivery(self, tmp_path):
        repository, database = repo(tmp_path)
        sent = {}

        class Transport(httpx.BaseTransport):
            def handle_request(self, request):
                sent.update(json.loads(request.content))
                return httpx.Response(200)

        router = AlertRouter(repository, webhook_url="https://ops.example/hook",
                             transport=httpx.Client(transport=Transport()),
                             now_fn=lambda: NOW)
        router.emit("K", "CRITICAL", "m",
                    access_token="SECRET-VALUE", nested={"api_secret": "ALSO-SECRET"},
                    safe="visible")
        assert sent["payload"]["access_token"] == "[redacted]"
        assert sent["payload"]["nested"]["api_secret"] == "[redacted]"
        assert sent["payload"]["safe"] == "visible"
        assert "SECRET-VALUE" not in json.dumps(sent)
        database.close()

    def test_emit_without_a_repository_still_works(self):
        router = AlertRouter(None, now_fn=lambda: NOW)
        assert router.emit("K", "INFO", "m") is not None

    def test_status_reports_counters(self, tmp_path):
        repository, database = repo(tmp_path)
        router = AlertRouter(repository, now_fn=lambda: NOW)
        router.emit("A", "INFO", "m")
        router.emit("A", "INFO", "m")  # deduped
        status = router.status()
        assert status["emitted"] == 1 and status["suppressed"] == 1
        assert status["webhook_enabled"] is False
        database.close()


class TestTokenGuard:
    def test_unverified_credential_blocks_entries(self, tmp_path):
        repository, database = repo(tmp_path)
        guard = TokenGuard(repository, max_age_hours=20, now_fn=lambda: NOW)
        decision = guard.check()
        assert decision.allowed is False
        assert decision.reason.startswith("TOKEN_UNVERIFIED")
        database.close()

    def test_fresh_credential_allows_entries(self, tmp_path):
        repository, database = repo(tmp_path)
        guard = TokenGuard(repository, max_age_hours=20, now_fn=lambda: NOW)
        guard.record_verified(when=NOW, source="preflight")
        assert guard.check().allowed is True
        assert repository.get_engine_state(TOKEN_KEY)["source"] == "preflight"
        database.close()

    def test_stale_credential_blocks_entries(self, tmp_path):
        repository, database = repo(tmp_path)
        guard = TokenGuard(repository, max_age_hours=20, now_fn=lambda: NOW)
        guard.record_verified(when=NOW - timedelta(hours=20, minutes=1))
        decision = guard.check()
        assert decision.allowed is False
        assert decision.reason.startswith("TOKEN_STALE")
        database.close()

    def test_warning_window_precedes_the_block(self, tmp_path):
        repository, database = repo(tmp_path)
        guard = TokenGuard(repository, max_age_hours=20, warn_minutes=120,
                           now_fn=lambda: NOW)
        guard.record_verified(when=NOW - timedelta(hours=19))
        assert guard.check().allowed is True     # still usable
        assert guard.warn_due() is True          # but inside the warning window
        assert guard.expires_in_minutes() == pytest.approx(60, abs=0.1)
        database.close()

    def test_zero_max_age_disables_the_check(self, tmp_path):
        repository, database = repo(tmp_path)
        guard = TokenGuard(repository, max_age_hours=0, now_fn=lambda: NOW)
        assert guard.check().allowed is True
        database.close()

    def test_no_repository_blocks_rather_than_guesses(self):
        guard = TokenGuard(None, max_age_hours=20, now_fn=lambda: NOW)
        assert guard.check().allowed is False

    def test_status_is_serialisable(self, tmp_path):
        repository, database = repo(tmp_path)
        guard = TokenGuard(repository, max_age_hours=20, now_fn=lambda: NOW)
        guard.record_verified(when=NOW)
        json.dumps(guard.status())
        database.close()


class TestCanaryGuard:
    def test_disabled_canary_allows_everything(self, tmp_path):
        repository, database = repo(tmp_path)
        guard = CanaryGuard(repository, max_notional=0, first_orders=10,
                            now_fn=lambda: NOW)
        assert guard.check(quantity=1000, price=500).allowed is True
        database.close()

    def test_oversized_first_entry_is_reduced_not_refused(self, tmp_path):
        repository, database = repo(tmp_path)
        guard = CanaryGuard(repository, max_notional=10_000, first_orders=3,
                            now_fn=lambda: NOW)
        decision = guard.check(quantity=100, price=500.0)  # 50,000 notional
        assert decision.allowed is True
        assert decision.adjusted_quantity == 20.0          # 20 * 500 = 10,000
        assert "CANARY_REDUCED" in decision.reason
        database.close()

    def test_entry_below_the_ceiling_is_untouched(self, tmp_path):
        repository, database = repo(tmp_path)
        guard = CanaryGuard(repository, max_notional=10_000, first_orders=3,
                            now_fn=lambda: NOW)
        decision = guard.check(quantity=5, price=100.0)
        assert decision.allowed is True and decision.adjusted_quantity is None
        database.close()

    def test_ceiling_below_one_unit_refuses_the_entry(self, tmp_path):
        repository, database = repo(tmp_path)
        guard = CanaryGuard(repository, max_notional=50, first_orders=3,
                            now_fn=lambda: NOW)
        decision = guard.check(quantity=1, price=500.0)
        assert decision.allowed is False
        assert "CANARY_BLOCKED" in decision.reason
        database.close()

    def test_ramp_ends_after_the_configured_number_of_entries(self, tmp_path):
        repository, database = repo(tmp_path)
        guard = CanaryGuard(repository, max_notional=10_000, first_orders=2,
                            now_fn=lambda: NOW)
        assert guard.check(quantity=100, price=500.0).adjusted_quantity == 20.0
        guard.record_submitted(quantity=20, price=500.0)
        assert guard.check(quantity=100, price=500.0).adjusted_quantity == 20.0
        guard.record_submitted(quantity=20, price=500.0)
        # ramp complete: full size is allowed from here
        decision = guard.check(quantity=100, price=500.0)
        assert decision.allowed is True
        assert decision.adjusted_quantity is None
        assert "CANARY_COMPLETE" in decision.reason
        assert guard.status()["complete"] is True
        database.close()

    def test_ramp_state_survives_a_restart(self, tmp_path):
        repository, database = repo(tmp_path)
        CanaryGuard(repository, max_notional=10_000, first_orders=5,
                    now_fn=lambda: NOW).record_submitted()
        # a fresh object reads the persisted counter, so a restart cannot
        # silently restart the ramp
        restarted = CanaryGuard(repository, max_notional=10_000, first_orders=5,
                                now_fn=lambda: NOW)
        assert restarted.submitted() == 1
        database.close()

    def test_invalid_input_is_refused_during_the_ramp(self, tmp_path):
        repository, database = repo(tmp_path)
        guard = CanaryGuard(repository, max_notional=10_000, first_orders=5,
                            now_fn=lambda: NOW)
        assert guard.check(quantity=0, price=100).allowed is False
        assert guard.check(quantity=1, price=0).allowed is False
        database.close()


class TestWatchdog:
    def _live_repo_with_order(self, tmp_path, created_ts):
        repository, database = repo(tmp_path)
        repository.record_live_order({
            "id": "live-abc", "symbol": "RELIANCE", "side": "BUY",
            "quantity": 1, "price": 100.0, "order_type": "MARKET",
            "status": "SUBMITTED", "tag": "t", "created_ts": created_ts})
        return repository, database

    def test_stuck_order_is_reported(self, tmp_path):
        created = (NOW - timedelta(minutes=30)).isoformat()
        repository, database = self._live_repo_with_order(tmp_path, created)

        class Live:
            LIVE = True

        watchdog = LiveWatchdog(repository, broker=Live(), stuck_order_minutes=5,
                                now_fn=lambda: NOW)
        findings = watchdog.check_stuck_orders(now=NOW)
        assert len(findings) == 1
        assert findings[0].kind == "STUCK_ORDER"
        assert findings[0].severity == "CRITICAL"
        assert findings[0].payload["age_minutes"] == pytest.approx(30, abs=0.1)
        database.close()

    def test_recent_order_is_not_reported(self, tmp_path):
        created = (NOW - timedelta(minutes=1)).isoformat()
        repository, database = self._live_repo_with_order(tmp_path, created)

        class Live:
            LIVE = True

        watchdog = LiveWatchdog(repository, broker=Live(), stuck_order_minutes=5,
                                now_fn=lambda: NOW)
        assert watchdog.check_stuck_orders(now=NOW) == []
        database.close()

    def test_final_statuses_are_never_stuck(self, tmp_path):
        repository, database = repo(tmp_path)
        for status in ("FILLED", "CANCELLED", "REJECTED"):
            repository.record_live_order({
                "id": f"live-{status}", "symbol": "S", "side": "BUY",
                "quantity": 1, "price": 1.0, "status": status,
                "created_ts": (NOW - timedelta(days=1)).isoformat()})

        class Live:
            LIVE = True

        watchdog = LiveWatchdog(repository, broker=Live(), stuck_order_minutes=5,
                                now_fn=lambda: NOW)
        assert watchdog.check_stuck_orders(now=NOW) == []
        database.close()

    def test_watchdog_reads_the_sandbox_table_for_a_sandbox_broker(self, tmp_path):
        repository, database = repo(tmp_path)
        repository.record_sandbox_order({
            "id": "sbox-1", "symbol": "S", "side": "BUY", "quantity": 1,
            "price": 1.0, "status": "OPEN",
            "created_ts": (NOW - timedelta(minutes=30)).isoformat()})

        class Sandbox:
            LIVE = False

        watchdog = LiveWatchdog(repository, broker=Sandbox(),
                                stuck_order_minutes=5, now_fn=lambda: NOW)
        findings = watchdog.check_stuck_orders(now=NOW)
        assert [f.payload["order_id"] for f in findings] == ["sbox-1"]
        database.close()

    def test_disabled_broker_is_critical(self, tmp_path):
        repository, database = repo(tmp_path)

        class Dead:
            available = False
            mode = "UPSTOX_LIVE"
            _base_url = "https://api-hft.upstox.com"

        watchdog = LiveWatchdog(repository, broker=Dead(), now_fn=lambda: NOW)
        findings = watchdog.check_broker_available()
        assert findings[0].kind == "BROKER_DISABLED"
        assert findings[0].severity == "CRITICAL"
        database.close()

    def test_reconciliation_silence_is_reported(self, tmp_path):
        repository, database = repo(tmp_path)
        repository.set_engine_state(
            "last_reconcile_at",
            {"ts": (NOW - timedelta(minutes=45)).isoformat()})
        watchdog = LiveWatchdog(repository, stuck_order_minutes=5,
                                reconcile_silence_minutes=10, now_fn=lambda: NOW)
        findings = watchdog.check_reconcile_liveness(now=NOW)
        assert findings[0].kind == "RECONCILE_STALLED"
        database.close()

    def test_recent_reconciliation_is_not_reported(self, tmp_path):
        repository, database = repo(tmp_path)
        repository.set_engine_state(
            "last_reconcile_at", {"ts": (NOW - timedelta(minutes=1)).isoformat()})
        watchdog = LiveWatchdog(repository, reconcile_silence_minutes=10,
                                now_fn=lambda: NOW)
        assert watchdog.check_reconcile_liveness(now=NOW) == []
        database.close()

    def test_missing_liveness_stamp_is_not_a_false_alarm(self, tmp_path):
        repository, database = repo(tmp_path)
        watchdog = LiveWatchdog(repository, now_fn=lambda: NOW)
        assert watchdog.check_reconcile_liveness(now=NOW) == []
        database.close()

    def test_token_expiry_warning_and_block_are_reported(self, tmp_path):
        repository, database = repo(tmp_path)
        guard = TokenGuard(repository, max_age_hours=20, warn_minutes=120,
                           now_fn=lambda: NOW)
        guard.record_verified(when=NOW - timedelta(hours=19))
        watchdog = LiveWatchdog(repository, token_guard=guard, now_fn=lambda: NOW)
        findings = watchdog.check_token(now=NOW)
        assert [f.kind for f in findings] == ["TOKEN_EXPIRING"]
        assert findings[0].severity == "WARNING"

        guard.record_verified(when=NOW - timedelta(hours=21))
        findings = watchdog.check_token(now=NOW)
        assert [f.kind for f in findings] == ["TOKEN_BLOCK"]
        assert findings[0].severity == "CRITICAL"
        database.close()

    def test_inspect_aggregates_and_never_raises(self, tmp_path):
        repository, database = repo(tmp_path)
        repository.record_live_order({
            "id": "live-x", "symbol": "S", "side": "BUY", "quantity": 1,
            "price": 1.0, "status": "OPEN",
            "created_ts": (NOW - timedelta(minutes=30)).isoformat()})
        repository.set_engine_state(
            "last_reconcile_at", {"ts": (NOW - timedelta(minutes=60)).isoformat()})

        class Dead:
            LIVE = True
            available = False
            mode = "UPSTOX_LIVE"

        watchdog = LiveWatchdog(repository, broker=Dead(), stuck_order_minutes=5,
                                reconcile_silence_minutes=10, now_fn=lambda: NOW)
        kinds = {f.kind for f in watchdog.inspect(now=NOW)}
        assert {"STUCK_ORDER", "BROKER_DISABLED", "RECONCILE_STALLED"} <= kinds
        database.close()

    def test_inspect_tolerates_a_broken_check(self, tmp_path):
        repository, database = repo(tmp_path)

        class Exploding:
            @property
            def available(self):
                raise RuntimeError("boom")

        watchdog = LiveWatchdog(repository, broker=Exploding(), now_fn=lambda: NOW)
        watchdog.observe = None  # not used here
        assert isinstance(watchdog.inspect(now=NOW), list)  # no raise
        database.close()


class TestBreachMonitor:
    def test_daily_loss_breach(self, tmp_path):
        repository, database = repo(tmp_path)
        monitor = BreachMonitor(repository, max_daily_loss=1000, auto_stop=True)
        breach = monitor.evaluate(realized_today=-1500.0)
        assert breach is not None and breach.kind == "DAILY_LOSS_BREACH"
        assert breach.stop is True
        database.close()

    def test_daily_loss_within_limit_is_fine(self, tmp_path):
        repository, database = repo(tmp_path)
        monitor = BreachMonitor(repository, max_daily_loss=1000)
        assert monitor.evaluate(realized_today=-999.0) is None
        database.close()

    def test_profit_never_breaches(self, tmp_path):
        repository, database = repo(tmp_path)
        monitor = BreachMonitor(repository, max_daily_loss=1000,
                                max_equity_drawdown=0.05)
        assert monitor.evaluate(realized_today=5000.0, equity=200_000,
                                peak_equity=100_000) is None
        database.close()

    def test_equity_drawdown_breach(self, tmp_path):
        repository, database = repo(tmp_path)
        monitor = BreachMonitor(repository, max_equity_drawdown=0.10)
        breach = monitor.evaluate(realized_today=0.0, equity=85_000,
                                  peak_equity=100_000)
        assert breach is not None and breach.kind == "EQUITY_DRAWDOWN_BREACH"
        assert breach.payload["drawdown"] == pytest.approx(0.15)
        database.close()

    def test_drawdown_disabled_by_default(self, tmp_path):
        repository, database = repo(tmp_path)
        monitor = BreachMonitor(repository)
        assert monitor.evaluate(realized_today=0.0, equity=1.0,
                                peak_equity=1_000_000) is None
        database.close()

    def test_auto_stop_can_be_disabled(self, tmp_path):
        repository, database = repo(tmp_path)
        monitor = BreachMonitor(repository, max_daily_loss=100, auto_stop=False)
        breach = monitor.evaluate(realized_today=-500.0)
        assert breach is not None and breach.stop is False
        database.close()


class TestLiveOperationsFacade:
    def _ops(self, tmp_path, **kwargs):
        repository, database = repo(tmp_path)
        settings = Settings(broker="UPSTOX_LIVE", mode="LIVE", live_trading=True,
                            live_confirm="I-ACCEPT-REAL-MONEY-RISK",
                            upstox_live_access_token="t",
                            max_position_value=10_000, max_open_positions=1,
                            max_daily_loss=500, max_trades_per_day=3,
                            live_canary_max_notional=5_000,
                            live_canary_orders=2, **kwargs)
        ops = LiveOperations.from_settings(settings, repository, now_fn=lambda: NOW)
        return ops, repository, database

    def test_entries_are_blocked_until_the_token_is_verified(self, tmp_path):
        ops, repository, database = self._ops(tmp_path)
        decision = ops.guard_entry(side="BUY", quantity=1, price=100.0)
        assert decision.allowed is False
        assert ops.blocked_entries == 1
        assert "ENTRY_BLOCKED_TOKEN" in {
            e["payload"]["kind"] for e in repository.list_system_events()
            if e["kind"] == AlertRouter.EVENT_KIND}
        database.close()

    def test_exits_are_never_blocked_by_a_guard(self, tmp_path):
        ops, repository, database = self._ops(tmp_path)
        decision = ops.guard_entry(side="SELL", quantity=999, price=100.0)
        assert decision.allowed is True
        assert decision.reason == "NOT_AN_ENTRY"
        assert decision.adjusted_quantity is None
        assert ops.blocked_entries == 0
        database.close()

    def test_verified_token_then_canary_reduces_the_entry(self, tmp_path):
        ops, repository, database = self._ops(tmp_path)
        ops.token_guard.record_verified(when=NOW)
        decision = ops.guard_entry(side="BUY", quantity=100, price=100.0)
        assert decision.allowed is True
        assert decision.adjusted_quantity == 50.0   # 5,000 / 100
        assert ops.reduced_entries == 1
        database.close()

    def test_submitted_entries_advance_the_ramp(self, tmp_path):
        ops, repository, database = self._ops(tmp_path)
        ops.token_guard.record_verified(when=NOW)
        ops.record_submitted_entry(quantity=50, price=100.0)
        ops.record_submitted_entry(quantity=50, price=100.0)
        assert ops.canary.status()["complete"] is True
        decision = ops.guard_entry(side="BUY", quantity=100, price=100.0)
        assert decision.allowed and decision.adjusted_quantity is None
        database.close()

    def test_observe_routes_findings_as_alerts(self, tmp_path):
        ops, repository, database = self._ops(tmp_path)
        ops.token_guard.record_verified(when=NOW - timedelta(hours=21))
        findings = ops.observe(now=NOW)
        assert any(f["kind"] == "TOKEN_BLOCK" for f in findings)
        alerts = recent_alerts(repository)
        assert any(a["kind"] == "TOKEN_BLOCK" for a in alerts)
        database.close()

    def test_breach_is_reported_and_alerted(self, tmp_path):
        ops, repository, database = self._ops(tmp_path)
        breach = ops.breach(realized_today=-900.0)
        assert breach is not None and breach["kind"] == "DAILY_LOSS_BREACH"
        assert any(a["kind"] == "DAILY_LOSS_BREACH"
                   for a in recent_alerts(repository))
        database.close()

    def test_status_is_json_serialisable(self, tmp_path):
        ops, repository, database = self._ops(tmp_path)
        json.dumps(ops.status())
        database.close()


class TestPreflightChecks:
    def _live_settings(self, **kwargs):
        base = dict(broker="UPSTOX_LIVE", mode="LIVE", live_trading=True,
                    live_confirm="I-ACCEPT-REAL-MONEY-RISK",
                    upstox_live_access_token="t", max_position_value=10_000,
                    max_open_positions=1, max_daily_loss=500, max_trades_per_day=3,
                    data_source="upstox", market_calendar="nse-session",
                    database_url="sqlite:///trading_live.db")
        base.update(kwargs)
        return Settings(**base)

    def test_a_fully_configured_session_has_no_errors(self):
        checks = run_checks(self._live_settings(
            live_canary_max_notional=5000, live_canary_orders=5,
            live_max_equity_drawdown=0.1,
            alert_webhook_url="https://ops.example/hook"))
        errors = [c for c in checks if not c.ok and c.severity == ERROR]
        assert errors == []

    def test_missing_switches_and_unbounded_limits_are_errors(self):
        settings = Settings()  # paper defaults
        report = summarize(run_checks(settings))
        assert report["ok"] is False
        failed = {c["name"] for c in report["errors"]}
        assert {"four_switch_opt_in", "credential_present",
                "hard_risk_limits_bounded"} <= failed

    def test_an_invalid_order_host_is_an_error(self):
        import types

        fake = types.SimpleNamespace(
            broker="UPSTOX_LIVE", mode="LIVE", live_trading=True,
            live_confirm="I-ACCEPT-REAL-MONEY-RISK",
            upstox_live_access_token="t", max_position_value=10_000,
            max_open_positions=1, max_daily_loss=500, max_trades_per_day=3,
            upstox_live_base_url="https://api-sandbox.upstox.com")
        report = summarize(run_checks(fake))
        assert "order_host_is_live" in {c["name"] for c in report["errors"]}

    def test_operational_gaps_are_warnings_not_errors(self):
        report = summarize(run_checks(self._live_settings()))
        assert report["ok"] is True
        warned = {c["name"] for c in report["warnings"]}
        assert {"canary_ramp", "equity_drawdown_cap", "alert_delivery",
                "token_freshness"} <= warned
        # a live-specific DATABASE_URL passes this check
        passed = {c["name"] for c in report["checks"] if c["ok"]}
        assert "dedicated_live_database" in passed

    def test_canary_and_drawdown_present_clears_those_warnings(self):
        report = summarize(run_checks(self._live_settings(
            live_canary_max_notional=5000, live_canary_orders=5,
            live_max_equity_drawdown=0.1,
            alert_webhook_url="https://ops.example/hook",
            database_url="sqlite:///trading_live.db")))
        warned = {c["name"] for c in report["warnings"]}
        assert "canary_ramp" not in warned
        assert "equity_drawdown_cap" not in warned
        assert "alert_delivery" not in warned

    def test_static_ip_reminder_is_always_present(self):
        checks = run_checks(self._live_settings())
        info = [c for c in checks if c.name == "static_ip_registered"]
        assert len(info) == 1 and info[0].severity == "INFO"

    def test_checks_are_json_serialisable(self):
        json.dumps(summarize(run_checks(self._live_settings())))


class TestEngineIntegration:
    """The rails must bind to the real engine paths, not just exist."""

    def _engine(self, tmp_path, ops):
        from app.backtesting.costs import ExecutionConfig
        from app.brokers.sandbox import UpstoxSandboxBroker
        from app.data.models import Candle
        from app.engine.sandbox_engine import SandboxEngine
        from app.learning.journal import LearningJournal
        from app.risk.limits import HardRiskLimits
        from app.strategies import create_strategy

        repository, database = repo(tmp_path, "engine.db")

        class Provider:
            symbols = ["RELIANCE"]
            _symbol_for = {"NSE_EQ|INE002A01018": "RELIANCE"}

            def metrics(self):
                return {"market_open": True}

            def status(self):
                return "CONNECTED"

        broker = UpstoxSandboxBroker("tok", "NSE_EQ|INE002A01018",
                                     repository=repository)
        broker.instrument_map["RELIANCE"] = "NSE_EQ|INE002A01018"

        class RecordingBroker:
            """Wrap the real broker so we can assert nothing was submitted."""
            LIVE = False
            mode = "UPSTOX_SANDBOX"

            def __init__(self):
                self.submitted = []
                self.ledger = broker.ledger
                self.orders = broker.orders

            def submit(self, side, quantity, price, **kwargs):
                self.submitted.append((side, quantity, price))
                return "intent"

            def reconcile(self):
                return []

            def status_view(self):
                return {"mode": self.mode, "live": False, "available": True,
                        "orders_tracked": 0, "open_orders": 0}

            def open_orders(self):
                return []

        recording = RecordingBroker()
        engine = SandboxEngine(
            provider=Provider(),
            strategy=create_strategy("momentum", {"period": 2, "threshold": 0.0}),
            broker=recording, repository=repository,
            journal=LearningJournal(repository),
            risk_limits=HardRiskLimits(max_position_value=10_000),
            timeframe="1m", live_ops=ops,
            instrument_keys={"RELIANCE": "NSE_EQ|INE002A01018"},
            stop_loss=0.05, take_profit=0.10)
        engine.daily = None  # keep the test independent of daily accounting
        return engine, recording, repository, database, Candle

    def test_blocked_entry_never_reaches_the_broker(self, tmp_path):
        ops, ops_repo, ops_db = TestLiveOperationsFacade()._ops(tmp_path)
        engine, recording, repository, database, Candle = self._engine(tmp_path, ops)
        candle = Candle(timestamp=NOW, open=100, high=101, low=99, close=100,
                        volume=1000, symbol="RELIANCE")
        engine._history.append(candle)
        engine._submit("BUY", 1.0, 100.0, candle)
        assert recording.submitted == []
        assert any(e["kind"] == "ENTRY_GUARD_BLOCKED"
                   for e in repository.list_system_events())
        database.close()
        ops_db.close()

    def test_guard_never_blocks_a_protective_exit(self, tmp_path):
        ops, ops_repo, ops_db = TestLiveOperationsFacade()._ops(tmp_path)
        engine, recording, repository, database, Candle = self._engine(tmp_path, ops)
        candle = Candle(timestamp=NOW, open=100, high=101, low=99, close=100,
                        volume=1000, symbol="RELIANCE")
        engine._submit("SELL", 5.0, 100.0, candle, reason="PROTECTIVE_EXIT")
        assert recording.submitted == [("SELL", 5.0, 100.0)]
        database.close()
        ops_db.close()

    def test_engine_status_exposes_live_ops(self, tmp_path):
        ops, ops_repo, ops_db = TestLiveOperationsFacade()._ops(tmp_path)
        engine, recording, repository, database, Candle = self._engine(tmp_path, ops)
        status = engine.status()
        assert status["live_ops"]["canary"]["enabled"] is True
        database.close()
        ops_db.close()

    def test_ops_tick_triggers_the_emergency_stop_on_a_breach(self, tmp_path):
        from app.live.watchdog import BreachMonitor

        ops, ops_repo, ops_db = TestLiveOperationsFacade()._ops(tmp_path)
        ops.breaches = BreachMonitor(ops_repo, max_daily_loss=100, auto_stop=True)
        engine, recording, repository, database, Candle = self._engine(tmp_path, ops)
        engine._last_realized = -500.0
        engine._ops_tick(now=NOW)
        assert engine._check_emergency() is True
        reasons = [e["payload"].get("reason", "")
                   for e in repository.list_system_events()
                   if e["kind"] == "EMERGENCY_STOP"]
        assert any("AUTO_STOP DAILY_LOSS_BREACH" in r for r in reasons)
        database.close()
        ops_db.close()

    def test_ops_tick_does_not_stop_when_auto_stop_is_off(self, tmp_path):
        from app.live.watchdog import BreachMonitor

        ops, ops_repo, ops_db = TestLiveOperationsFacade()._ops(tmp_path)
        ops.breaches = BreachMonitor(ops_repo, max_daily_loss=100, auto_stop=False)
        engine, recording, repository, database, Candle = self._engine(tmp_path, ops)
        engine._last_realized = -500.0
        engine._ops_tick(now=NOW)
        assert engine._check_emergency() is False
        database.close()
        ops_db.close()

    def test_reconcile_stamps_liveness(self, tmp_path):
        engine, recording, repository, database, Candle = self._engine(tmp_path, None)
        assert repository.get_engine_state("last_reconcile_at") is None
        engine.reconcile()
        stamp = repository.get_engine_state("last_reconcile_at")
        assert stamp is not None and "ts" in stamp
        database.close()

    def test_engine_without_ops_is_unchanged(self, tmp_path):
        engine, recording, repository, database, Candle = self._engine(tmp_path, None)
        candle = Candle(timestamp=NOW, open=100, high=101, low=99, close=100,
                        volume=1000, symbol="RELIANCE")
        engine._submit("BUY", 1.0, 100.0, candle)
        assert recording.submitted == [("BUY", 1.0, 100.0)]
        database.close()


class TestApiSurfaces:
    """The operator must be able to SEE the rails, not just trust they exist."""

    def _client(self, tmp_path, settings):
        from app.api.server import create_app
        from fastapi.testclient import TestClient

        return TestClient(create_app(settings, data_dir=tmp_path))

    def _live_settings(self, tmp_path, **kwargs):
        base = dict(broker="UPSTOX_LIVE", mode="LIVE", live_trading=True,
                    live_confirm="I-ACCEPT-REAL-MONEY-RISK",
                    upstox_live_access_token="t", api_token="t",
                    max_position_value=10_000, max_open_positions=1,
                    max_daily_loss=500, max_trades_per_day=3,
                    live_canary_max_notional=5_000, live_canary_orders=5,
                    live_max_equity_drawdown=0.1,
                    database_url=f"sqlite:///{tmp_path}/api.db")
        base.update(kwargs)
        return Settings(**base)

    def test_alerts_endpoint_lists_recorded_alerts(self, tmp_path):
        settings = self._live_settings(tmp_path)
        with self._client(tmp_path, settings) as client:
            repository = client.app.state.repository
            AlertRouter(repository, now_fn=lambda: NOW).emit(
                "TEST_ALERT", "CRITICAL", "visible in the API")
            data = client.get("/api/v2/alerts",
                              headers={"Authorization": "Bearer t"}).json()
        assert data["alerts"][0]["kind"] == "TEST_ALERT"
        assert data["alerts"][0]["severity"] == "CRITICAL"
        assert data["webhook_enabled"] is False

    def test_alerts_endpoint_requires_auth(self, tmp_path):
        settings = self._live_settings(tmp_path)
        with self._client(tmp_path, settings) as client:
            assert client.get("/api/v2/alerts").status_code == 401

    def test_alerts_limit_is_clamped(self, tmp_path):
        settings = self._live_settings(tmp_path)
        with self._client(tmp_path, settings) as client:
            response = client.get("/api/v2/alerts?limit=100000",
                                  headers={"Authorization": "Bearer t"})
        assert response.status_code == 200

    def test_live_status_exposes_guards_and_host(self, tmp_path):
        settings = self._live_settings(tmp_path)
        with self._client(tmp_path, settings) as client:
            data = client.get("/api/v2/live/status",
                              headers={"Authorization": "Bearer t"}).json()
        assert data["live_trading"] is True
        assert data["order_host"] == "https://api-hft.upstox.com"
        assert data["order_host_is_documented_live"] is True
        assert data["token"]["decision"]["allowed"] is False  # not verified yet
        assert data["canary"]["enabled"] is True
        assert data["breaches"]["auto_stop"] is True
        assert data["open_live_orders"] == 0

    def test_live_status_reflects_a_verified_token(self, tmp_path):
        settings = self._live_settings(tmp_path)
        with self._client(tmp_path, settings) as client:
            repository = client.app.state.repository
            TokenGuard(repository, now_fn=lambda: NOW).record_verified(when=NOW)
            data = client.get("/api/v2/live/status",
                              headers={"Authorization": "Bearer t"}).json()
        assert data["token"]["decision"]["allowed"] is True

    def test_live_status_never_leaks_the_token(self, tmp_path):
        settings = self._live_settings(tmp_path, upstox_live_access_token="SECRET-TOK")
        with self._client(tmp_path, settings) as client:
            response = client.get("/api/v2/live/status",
                                  headers={"Authorization": "Bearer t"})
        assert "SECRET-TOK" not in response.text

    def test_dashboard_payload_carries_alerts_and_rails(self, tmp_path):
        settings = self._live_settings(tmp_path)
        with self._client(tmp_path, settings) as client:
            repository = client.app.state.repository
            AlertRouter(repository, now_fn=lambda: NOW).emit(
                "DASH_ALERT", "WARNING", "shown on the dashboard")
            data = client.get("/dashboard/data",
                              headers={"Authorization": "Bearer t"}).json()
        assert data["alerts"][0]["kind"] == "DASH_ALERT"
        assert data["live_ops"]["live_trading"] is True
        assert data["live_ops"]["canary"]["enabled"] is True

    def test_dashboard_html_has_the_alerts_panel(self, tmp_path):
        settings = Settings(api_token="t",
                            database_url=f"sqlite:///{tmp_path}/dash.db")
        with self._client(tmp_path, settings) as client:
            page = client.get("/dashboard",
                              headers={"Authorization": "Bearer t"}).text
        assert 'id="alerts"' in page
        assert "Alerts &amp; Live Ops" in page
