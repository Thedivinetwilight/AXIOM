"""V2.1 L-ops: alerts, entry guards, watchdog, breach monitor, preflight.

The theme of every test here is the same: an operational rail must fail CLOSED
for new entries, must NEVER block an exit, and must NEVER raise into the trading
loop. Nothing contacts a network: the webhook is exercised with an httpx mock
transport and everything else is pure.
"""

import json
from datetime import datetime, timedelta, timezone
from queue import Empty

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


def _race_live_engine_lease_worker(database_url, index, now_iso, barrier, queue):
    """Spawn-safe worker for the cross-process atomic lease race test.

    This must remain at module scope: macOS and Windows use spawn semantics,
    which cannot pickle a target nested inside the pytest test method.
    """
    from app.database.database import Database
    from app.database.repository import Repository
    from app.live.lock import EngineLock

    database = None
    try:
        database = Database(database_url)
        repository = Repository(database)
        lock = EngineLock(repository, live=True, owner=f"racer-{index}")
        barrier.wait(timeout=30)
        decision = lock.acquire(now=datetime.fromisoformat(now_iso))
        queue.put((index, bool(decision.granted), None))
    except BaseException as exc:
        # Report the worker error to the parent before re-raising so a failed
        # subprocess cannot masquerade as an ordinary losing contender.
        queue.put((index, False, type(exc).__name__))
        raise
    finally:
        if database is not None:
            database.close()


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

    def test_inspect_tolerates_a_broken_check_but_reports_unknown(self, tmp_path):
        repository, database = repo(tmp_path)

        class Exploding:
            @property
            def available(self):
                raise RuntimeError("credential=must-not-be-logged")

        watchdog = LiveWatchdog(repository, broker=Exploding(), now_fn=lambda: NOW)
        findings = watchdog.inspect(now=NOW)
        assert isinstance(findings, list)  # monitoring failure does not crash loop
        unavailable = [f for f in findings
                       if f.kind == "WATCHDOG_DATA_UNAVAILABLE"]
        assert unavailable
        assert all("must-not-be-logged" not in str(f.to_dict())
                   for f in unavailable)
        database.close()

    def test_failed_live_order_read_is_a_critical_unknown(self):
        class BrokenRepository:
            def list_live_orders(self, limit):
                raise RuntimeError("token=must-not-be-logged")

        class Live:
            LIVE = True

        watchdog = LiveWatchdog(BrokenRepository(), broker=Live())
        findings = watchdog.check_stuck_orders(now=NOW)
        assert len(findings) == 1
        assert findings[0].kind == "WATCHDOG_DATA_UNAVAILABLE"
        assert findings[0].severity == "CRITICAL"
        assert findings[0].payload["source"] == "live order audit"
        assert findings[0].payload["error_type"] == "RuntimeError"
        assert "must-not-be-logged" not in str(findings[0].to_dict())

    def test_malformed_working_order_timestamp_is_not_silently_ignored(self):
        class Repository:
            def list_live_orders(self, limit):
                return [{"id": "order-1", "status": "OPEN",
                         "created_ts": "not-a-timestamp"}]

        class Live:
            LIVE = True

        watchdog = LiveWatchdog(Repository(), broker=Live())
        findings = watchdog.check_stuck_orders(now=NOW)
        assert findings[0].kind == "WATCHDOG_DATA_UNAVAILABLE"
        assert findings[0].payload["source"] == "working-order timestamp"

    def test_failed_intent_journal_read_is_not_reported_as_clean(self):
        class Journal:
            def unresolved(self):
                raise RuntimeError("token=must-not-be-logged")

        class Live:
            LIVE = True

        watchdog = LiveWatchdog(object(), broker=Live())
        watchdog.intent_journal = Journal()
        findings = watchdog.check_unresolved_intents(now=NOW)
        assert findings[0].kind == "WATCHDOG_DATA_UNAVAILABLE"
        assert findings[0].severity == "CRITICAL"
        assert findings[0].payload["source"] == "submission-intent journal"
        assert "must-not-be-logged" not in str(findings[0].to_dict())

    def test_failed_reconciliation_and_ack_reads_are_visible(self):
        class BrokenRepository:
            def get_engine_state(self, key, default=None):
                raise RuntimeError("db-password=must-not-be-logged")

        class BrokenAcks:
            def outstanding_critical(self, **kwargs):
                raise RuntimeError("webhook-token=must-not-be-logged")

        class Live:
            LIVE = True

        watchdog = LiveWatchdog(BrokenRepository(), broker=Live(),
                                acknowledgements=BrokenAcks(),
                                now_fn=lambda: NOW)
        reconcile = watchdog.check_reconcile_liveness(now=NOW)
        alerts = watchdog.check_unacknowledged_critical(now=NOW)
        assert reconcile[0].kind == "WATCHDOG_DATA_UNAVAILABLE"
        assert alerts[0].kind == "WATCHDOG_DATA_UNAVAILABLE"
        assert "must-not-be-logged" not in str(reconcile + alerts)

    def test_missing_live_token_and_ack_monitors_are_unknown(self):
        class Live:
            LIVE = True
            available = True

        watchdog = LiveWatchdog(object(), broker=Live(), now_fn=lambda: NOW)
        token = watchdog.check_token(now=NOW)
        acks = watchdog.check_unacknowledged_critical(now=NOW)
        assert token[0].kind == "WATCHDOG_DATA_UNAVAILABLE"
        assert token[0].payload["source"] == "credential freshness"
        assert acks[0].kind == "WATCHDOG_DATA_UNAVAILABLE"
        assert acks[0].payload["source"] == \
            "critical-alert acknowledgement state"


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
                    default_timeframe="5m",
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


class TestStaticIpVerdict:
    """FINAL-11/10 §18: the static-IP readiness check is a GENUINE
    match / mismatch / cannot_check verdict — never a fabricated pass."""

    def _settings(self, **kwargs):
        base = dict(broker="UPSTOX_LIVE", mode="LIVE", live_trading=True,
                    live_confirm="I-ACCEPT-REAL-MONEY-RISK",
                    upstox_live_access_token="t", max_position_value=10_000,
                    max_open_positions=1, max_daily_loss=500,
                    max_trades_per_day=3, data_source="upstox",
                    market_calendar="nse-session", default_timeframe="5m",
                    database_url="sqlite:///trading_live.db")
        base.update(kwargs)
        return Settings(**base)

    def test_not_configured_is_honest_not_a_pass(self):
        from app.live.preflight import static_ip_verdict

        verdict = static_ip_verdict(self._settings())
        assert verdict["state"] == "not_configured"
        assert verdict["detected"] is None

    def test_match_when_egress_ip_is_registered(self):
        from app.live.preflight import static_ip_verdict

        settings = self._settings(upstox_static_ip_primary="203.0.113.7")
        verdict = static_ip_verdict(settings,
                                    fetch_public_ip=lambda timeout: "203.0.113.7")
        assert verdict["state"] == "match"

    def test_mismatch_is_an_error_not_a_warning(self):
        from app.live.preflight import run_readiness_center_checks, ERROR

        settings = self._settings(upstox_static_ip_primary="203.0.113.7")
        checks = run_readiness_center_checks(
            settings, context={"static_ip": {"state": "mismatch",
                                             "registered": ["203.0.113.7"],
                                             "detected": "198.51.100.9",
                                             "detail": "wrong source IP"}})
        check = next(c for c in checks if c.name == "static_ip_configuration")
        assert check.ok is False and check.severity == ERROR

    def test_cannot_check_is_reported_never_fabricated_as_match(self):
        from app.live.preflight import static_ip_verdict

        settings = self._settings(upstox_static_ip_primary="203.0.113.7")
        verdict = static_ip_verdict(settings,
                                    fetch_public_ip=lambda timeout: None)
        assert verdict["state"] == "cannot_check"
        assert verdict["detected"] is None

    def test_a_failing_probe_is_cannot_check(self):
        from app.live.preflight import static_ip_verdict

        def boom(timeout):
            raise RuntimeError("egress blocked")

        settings = self._settings(upstox_static_ip_primary="203.0.113.7")
        assert static_ip_verdict(settings, fetch_public_ip=boom)["state"] == \
            "cannot_check"

    def test_readiness_check_stays_optional_but_mismatch_is_an_error(
            self, tmp_path):
        from app.live.preflight import run_readiness_center_checks

        repository, database = repo(tmp_path, "staticip.db")
        settings = self._settings(upstox_static_ip_primary="203.0.113.7",
                                  database_url=f"sqlite:///{tmp_path}/staticip.db")
        checks = run_readiness_center_checks(
            settings, repository=repository,
            context={"static_ip": {"state": "match",
                                   "registered": ["203.0.113.7"],
                                   "detected": "203.0.113.7",
                                   "detail": "registered"}})
        check = next(c for c in checks if c.name == "static_ip_configuration")
        assert check.ok is True and check.required is False
        database.close()


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

    def test_unreadable_emergency_flag_stops_engine_check(self, tmp_path):
        engine, recording, repository, database, Candle = self._engine(
            tmp_path, None)
        original = repository.get_engine_state

        def broken(key, default=None):
            if key == "emergency_stop":
                raise RuntimeError("password=must-not-be-logged")
            return original(key, default)

        repository.get_engine_state = broken
        assert engine._check_emergency() is True
        database.close()

    def test_lost_live_lease_stops_loop_before_processing_or_submitting(
            self, tmp_path):
        from app.live.lock import EngineLock

        ops, ops_repo, ops_db = TestLiveOperationsFacade()._ops(tmp_path)
        engine, recording, repository, database, Candle = self._engine(
            tmp_path, ops)

        class OneCandleProvider:
            symbols = ["RELIANCE"]

            async def stream(self, symbols, timeframe):
                yield Candle(timestamp=NOW, open=100, high=101, low=99,
                             close=100, volume=1000, symbol="RELIANCE")

        engine.provider = OneCandleProvider()
        processed = []
        engine.process_candle = lambda candle: processed.append(candle)
        engine.live_lock = EngineLock(repository, live=True, owner="owner:1")
        repository.heartbeat_lease = lambda *args, **kwargs: False

        result = engine.run(max_candles=1)
        assert processed == []
        assert recording.submitted == []
        assert engine.state == "BLOCKED_LEASE_LOST"
        assert any(event["kind"] == "ENGINE_BLOCKED_LEASE_LOST"
                   for event in repository.list_system_events())
        assert result["engine_lock"]["held"] is False
        engine._submit("BUY", 1.0, 100.0,
                       Candle(timestamp=NOW, open=100, high=101, low=99,
                              close=100, volume=1000, symbol="RELIANCE"))
        assert recording.submitted == []
        database.close()
        ops_db.close()

    def test_malformed_emergency_flag_stops_engine_check(self, tmp_path):
        engine, recording, repository, database, Candle = self._engine(
            tmp_path, None)
        original = repository.get_engine_state

        def malformed(key, default=None):
            if key == "emergency_stop":
                return ["not", "a", "mapping"]
            return original(key, default)

        repository.get_engine_state = malformed
        assert engine._check_emergency() is True
        database.close()

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

    @pytest.mark.parametrize("gate", ["net_edge", "entry_timing", "alpha_decay"])
    def test_entry_gate_exception_refuses_without_broker_call(self, tmp_path, gate):
        from types import SimpleNamespace
        from app.strategies.base import Signal

        engine, recording, repository, database, Candle = self._engine(
            tmp_path, None)
        candle = Candle(timestamp=NOW, open=100, high=101, low=99, close=100,
                        volume=1000, symbol="RELIANCE")

        def fail(*args, **kwargs):
            raise RuntimeError("credential=must-not-be-logged")

        # Ensure each parametrized failure is reached after all preceding
        # gates have passed; the only fault is the named final decision.
        engine.net_edge_gate.evaluate = lambda **kwargs: SimpleNamespace(
            passes=True, gross_expected=2.0, net_expected=1.0,
            threshold=0.0, reason="pass")
        engine.quality_scorer.score = lambda **kwargs: SimpleNamespace()
        engine.quality_scorer.passes = lambda result: True
        engine.entry_timing.evaluate = lambda **kwargs: SimpleNamespace(
            action="TRADE_NOW", reasons=[])
        engine.alpha_decay.should_trade = lambda *args, **kwargs: (True, "pass")
        if gate == "net_edge":
            engine.net_edge_gate.evaluate = fail
        elif gate == "entry_timing":
            engine.entry_timing.evaluate = fail
        else:
            engine.alpha_decay.should_trade = fail

        engine._act(Signal.BUY, candle)
        assert recording.submitted == []
        failure = next(event for event in repository.list_system_events()
                       if event["kind"] == "ENTRY_GATE_FAILURE")
        assert failure["payload"]["gate"] == gate
        assert failure["payload"]["error_type"] == "RuntimeError"
        assert "must-not-be-logged" not in str(failure)
        database.close()

    def test_live_entry_refuses_missing_bid_ask_instead_of_fabricating_spread(
            self, tmp_path):
        from app.strategies.base import Signal

        engine, recording, repository, database, Candle = self._engine(
            tmp_path, None)
        recording.LIVE = True
        candle = Candle(timestamp=NOW, open=100, high=101, low=99, close=100,
                        volume=1000, symbol="RELIANCE")
        engine._act(Signal.BUY, candle)
        assert recording.submitted == []
        failure = next(event for event in repository.list_system_events()
                       if event["kind"] == "ENTRY_GATE_FAILURE")
        assert failure["payload"]["gate"] == "liquidity"
        assert failure["payload"]["error_type"] == "ValueError"
        database.close()

    def test_live_entry_uses_fresh_observed_quote_without_permanently_disabling(
            self, tmp_path):
        from types import SimpleNamespace
        from datetime import datetime, timezone
        from app.data.market_events import MarketEvent
        from app.data.upstox_stream import UpstoxStream
        from app.strategies.base import Signal

        engine, recording, repository, database, Candle = self._engine(
            tmp_path, None)
        recording.LIVE = True
        engine.sizing_fraction = 0.05
        provider = UpstoxStream(
            "mock-market-token", ["NSE_EQ|INE002A01018"],
            timeframe="1m", mode="full")
        quote_received = datetime.now(timezone.utc)
        provider._on_tick(MarketEvent(
            provider="upstox", instrument_key="NSE_EQ|INE002A01018",
            kind="live", received_at=quote_received,
            exchange_ts=quote_received, ltp=100.0, bid=99.95, ask=100.05),
            "RELIANCE", quote_received.timestamp(), 100.0, 100.0)
        engine.provider = provider
        engine.net_edge_gate.evaluate = lambda **kwargs: SimpleNamespace(
            passes=True, gross_expected=2.0, net_expected=1.0,
            threshold=0.0, reason="pass")
        engine.quality_scorer.score = lambda **kwargs: SimpleNamespace()
        engine.quality_scorer.passes = lambda result: True
        engine.entry_timing.evaluate = lambda **kwargs: SimpleNamespace(
            action="TRADE_NOW", reasons=[])
        engine.alpha_decay.should_trade = lambda *args, **kwargs: (True, "pass")
        candle = Candle(timestamp=NOW, open=100, high=101, low=99, close=100,
                        volume=1000, symbol="RELIANCE")

        engine._act(Signal.BUY, candle)
        assert len(recording.submitted) == 1
        assert recording.submitted[0][0] == "BUY"
        failures = [event for event in repository.list_system_events()
                    if event["kind"] == "ENTRY_GATE_FAILURE"]
        assert failures == []
        database.close()

    def test_live_entry_refuses_stale_observed_quote(self, tmp_path):
        from datetime import datetime, timedelta, timezone
        from app.data.market_events import MarketEvent
        from app.data.upstox_stream import UpstoxStream
        from app.strategies.base import Signal

        engine, recording, repository, database, Candle = self._engine(
            tmp_path, None)
        recording.LIVE = True
        now = datetime.now(timezone.utc)
        received = now - timedelta(seconds=10)
        provider = UpstoxStream(
            "mock-market-token", ["NSE_EQ|INE002A01018"],
            timeframe="1m", mode="full")
        provider._on_tick(MarketEvent(
            provider="upstox", instrument_key="NSE_EQ|INE002A01018",
            kind="live", received_at=received, exchange_ts=received,
            ltp=100.0, bid=99.95, ask=100.05),
            "RELIANCE", received.timestamp(), 100.0, 100.0)
        engine.provider = provider
        candle = Candle(timestamp=now, open=100, high=101, low=99, close=100,
                        volume=1000, symbol="RELIANCE")

        engine._act(Signal.BUY, candle)
        assert recording.submitted == []
        failure = next(event for event in repository.list_system_events()
                       if event["kind"] == "ENTRY_GATE_FAILURE")
        assert failure["payload"]["gate"] == "liquidity"
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
            # The API builds its TokenGuard WITHOUT now_fn, so it judges
            # freshness against the real clock. Record the verification at the
            # real current time: recording it at the module's fixed NOW made
            # this test fail the moment the wall clock passed NOW + 20h, which
            # is a property of the calendar rather than of the code.
            TokenGuard(repository).record_verified(
                when=datetime.now(timezone.utc))
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


class TestAuditReplay:
    """Independently recomputed PnL from the audit trail (second opinion)."""

    def _repo_with_fills(self, tmp_path, fills, live=True):
        repository, database = repo(tmp_path, "pnl.db")
        record_order = (repository.record_live_order if live
                        else repository.record_sandbox_order)
        record_transition = (repository.record_live_transition if live
                             else repository.record_sandbox_transition)
        r = record_order
        t = record_transition
        for index, (side, qty, price) in enumerate(fills):
            order_id = f"ord-{index}"
            r({"id": order_id, "symbol": "RELIANCE", "side": side,
               "quantity": qty, "price": price, "order_type": "MARKET",
               "status": "FILLED", "tag": "t", "created_ts": f"2026-10-02T09:{index:02d}:00+00:00"})
            t(order_id, f"child-{index}", "FILLED", qty, price,
              f"2026-10-02T09:{index:02d}:00+00:00", {"ok": True})
        return repository, database

    def test_buy_then_sell_realizes_the_difference(self, tmp_path):
        from app.live.pnl import replay_fills

        repository, database = self._repo_with_fills(
            tmp_path, [("BUY", 10, 100.0), ("SELL", 10, 110.0)])
        result = replay_fills(repository, live=True)
        assert result.realized_pnl == pytest.approx(100.0)
        assert result.buys == 1 and result.sells == 1
        assert result.turnover == pytest.approx(2100.0)
        assert result.open_positions == {}
        assert result.per_symbol["RELIANCE"]["realized"] == pytest.approx(100.0)
        database.close()

    def test_partial_fills_of_one_child_use_the_incremental_basis(self, tmp_path):
        from app.live.pnl import replay_fills

        repository, database = repo(tmp_path, "partial.db")
        repository.record_live_order({
            "id": "o1", "symbol": "S", "side": "BUY", "quantity": 10,
            "price": 100.0, "status": "FILLED", "tag": "t",
            "created_ts": "2026-10-02T09:00:00+00:00"})
        # one child fills 5 @ 100 then 5 more @ 110 → average 105
        repository.record_live_transition("o1", "c1", "PARTIALLY_FILLED", 5, 100.0,
                                          "2026-10-02T09:00:00+00:00", {})
        repository.record_live_transition("o1", "c1", "FILLED", 10, 105.0,
                                          "2026-10-02T09:01:00+00:00", {})
        result = replay_fills(repository, live=True)
        # deltas: 5 @ 100 and 5 @ 110 → average cost 105
        assert result.turnover == pytest.approx(1050.0)
        assert result.open_positions["S"]["average_price"] == pytest.approx(105.0)
        assert result.fills[1]["price"] == pytest.approx(110.0)
        database.close()

    def test_partial_then_cancelled_child_keeps_its_credit(self, tmp_path):
        from app.live.pnl import replay_fills

        repository, database = repo(tmp_path, "cancel.db")
        repository.record_live_order({
            "id": "o1", "symbol": "S", "side": "BUY", "quantity": 10,
            "price": 100.0, "status": "CANCELLED", "tag": "t",
            "created_ts": "2026-10-02T09:00:00+00:00"})
        repository.record_live_transition("o1", "c1", "PARTIALLY_FILLED", 4, 100.0,
                                          "2026-10-02T09:00:00+00:00", {})
        repository.record_live_transition("o1", "c1", "CANCELLED", 4, 100.0,
                                          "2026-10-02T09:01:00+00:00", {})
        result = replay_fills(repository, live=True)
        assert result.fills[0]["quantity"] == 4      # exactly the executed part
        assert result.open_positions["S"]["quantity"] == 4
        database.close()

    def test_sandbox_and_live_trails_are_never_mixed(self, tmp_path):
        from app.live.pnl import replay_fills

        repository, database = repo(tmp_path, "mixed.db")
        repository.record_live_order({
            "id": "live-1", "symbol": "S", "side": "BUY", "quantity": 1,
            "price": 100.0, "status": "FILLED", "created_ts": "t0"})
        repository.record_live_transition("live-1", "c", "FILLED", 1, 100.0, "t0", {})
        repository.record_sandbox_order({
            "id": "sbox-1", "symbol": "S", "side": "BUY", "quantity": 5,
            "price": 50.0, "status": "FILLED", "created_ts": "t0"})
        repository.record_sandbox_transition("sbox-1", "c", "FILLED", 5, 50.0, "t0", {})

        live_only = replay_fills(repository, live=True)
        sandbox_only = replay_fills(repository, live=False)
        assert live_only.turnover == pytest.approx(100.0)
        assert sandbox_only.turnover == pytest.approx(250.0)
        database.close()

    def test_ledger_comparison_passes_within_tolerance(self, tmp_path):
        from app.live.pnl import compare_with_ledger, replay_fills

        repository, database = self._repo_with_fills(
            tmp_path, [("BUY", 10, 100.0), ("SELL", 10, 110.0)])

        class Ledger:
            realized_pnl = 100.5   # small timing/rounding difference

        result = replay_fills(repository, live=True)
        comparison = compare_with_ledger(result, Ledger(), tolerance=1.0)
        assert comparison["match"] is True
        database.close()

    def test_ledger_comparison_fails_on_a_real_divergence(self, tmp_path):
        from app.live.pnl import compare_with_ledger, replay_fills

        repository, database = self._repo_with_fills(
            tmp_path, [("BUY", 10, 100.0), ("SELL", 10, 110.0)])

        class Ledger:
            realized_pnl = 250.0   # the two implementations disagree

        result = replay_fills(repository, live=True)
        comparison = compare_with_ledger(result, Ledger(), tolerance=1.0)
        assert comparison["match"] is False
        assert "disagree" in comparison["explanation"]
        assert comparison["difference"] == pytest.approx(150.0)
        database.close()

    def test_replay_of_an_empty_trail_is_zero_and_serialisable(self, tmp_path):
        from app.live.pnl import replay_fills

        repository, database = repo(tmp_path, "empty.db")
        result = replay_fills(repository, live=True)
        assert result.fills == [] and result.realized_pnl == 0.0
        json.dumps(result.to_dict())
        database.close()

    def test_cumulative_realized_series_tracks_only_sells(self, tmp_path):
        from app.live.pnl import replay_fills

        repository, database = self._repo_with_fills(tmp_path, [
            ("BUY", 10, 100.0), ("SELL", 5, 110.0), ("SELL", 5, 120.0)])
        result = replay_fills(repository, live=True)
        series = result.realized_series()
        assert [round(value, 2) for _, value in series] == [50.0, 150.0]
        database.close()


class TestOpsCli:
    """Halt must always work; resume must refuse to re-arm a broken config."""

    def _settings(self, tmp_path, **kwargs):
        base = dict(api_token="t", database_url=f"sqlite:///{tmp_path}/cli.db")
        base.update(kwargs)
        return Settings(**base)

    def test_halt_is_permitted_with_default_paper_settings(self, tmp_path, capsys, monkeypatch):
        from app import __main__ as cli

        settings = self._settings(tmp_path)
        monkeypatch.setattr(cli, "_load_settings", lambda: settings)
        code = cli.cmd_live_halt(type("A", (), {"reason": "test halt"})())
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["halted"] is True
        assert payload["was_already_halted"] is False
        # and it really is persisted
        database, repository = cli._open_ops_repository(settings)
        assert repository.get_engine_state("emergency_stop")["stopped"] is True
        database.close()

    def test_halt_is_idempotent(self, tmp_path, capsys, monkeypatch):
        from app import __main__ as cli

        settings = self._settings(tmp_path)
        monkeypatch.setattr(cli, "_load_settings", lambda: settings)
        cli.cmd_live_halt(type("A", (), {"reason": "first"})())
        capsys.readouterr()
        cli.cmd_live_halt(type("A", (), {"reason": "second"})())
        payload = json.loads(capsys.readouterr().out)
        assert payload["was_already_halted"] is True

    def test_halt_still_halts_when_the_configuration_is_rejected(
            self, tmp_path, capsys, monkeypatch):
        """Halting is the safe direction: a rejected config must not block it."""
        from app import __main__ as cli

        def boom():
            raise ValueError("BROKER=UPSTOX_LIVE requires ALL of: MODE=LIVE")

        monkeypatch.setattr(cli, "_load_settings", boom)
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/halt.db")
        code = cli.cmd_live_halt(type("A", (), {"reason": "broken config"})())
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["halted"] is True
        assert payload["configuration_rejected"] is True
        assert "rejected" in payload["config_reason"]
        # and the stop really is persisted in the database the env pointed at
        from app.database.database import Database
        from app.database.repository import Repository

        database = Database(f"sqlite:///{tmp_path}/halt.db")
        assert (Repository(database).get_engine_state("emergency_stop")
                or {}).get("stopped") is True
        database.close()

    def test_halt_reports_when_even_the_state_database_is_unusable(
            self, tmp_path, capsys, monkeypatch):
        from app import __main__ as cli

        def boom():
            raise ValueError("nope")

        monkeypatch.setattr(cli, "_load_settings", boom)
        # a directory path can never be opened as a database
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}")
        code = cli.cmd_live_halt(type("A", (), {"reason": ""})())
        payload = json.loads(capsys.readouterr().out)
        assert code == 2 and payload["halted"] is False
        assert "could not be opened" in payload["reason"]

    def test_resume_refuses_when_not_armed(self, tmp_path, capsys, monkeypatch):
        from app import __main__ as cli

        settings = self._settings(tmp_path)
        monkeypatch.setattr(cli, "_load_settings", lambda: settings)
        code = cli.cmd_live_resume(type("A", (), {"allow_non_live": False})())
        payload = json.loads(capsys.readouterr().out)
        assert code == 2 and payload["resumed"] is False
        assert "not an armed UPSTOX_LIVE session" in payload["reason"]

    def test_resume_clears_the_flag_when_armed(self, tmp_path, capsys, monkeypatch):
        from app import __main__ as cli

        settings = self._settings(
            tmp_path, broker="UPSTOX_LIVE", mode="LIVE", live_trading=True,
            live_confirm="I-ACCEPT-REAL-MONEY-RISK",
            upstox_live_access_token="t", max_position_value=10_000,
            max_open_positions=1, max_daily_loss=500, max_trades_per_day=3)
        monkeypatch.setattr(cli, "_load_settings", lambda: settings)
        cli.cmd_live_halt(type("A", (), {"reason": "armed halt"})())
        capsys.readouterr()
        code = cli.cmd_live_resume(type("A", (), {"allow_non_live": False})())
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["resumed"] is True
        assert payload["was_halted"] is True
        database, repository = cli._open_ops_repository(settings)
        assert repository.get_engine_state("emergency_stop")["stopped"] is False
        database.close()

    def test_resume_refuses_an_invalid_configuration(self, capsys, monkeypatch):
        from app import __main__ as cli

        def boom():
            raise ValueError("BROKER=UPSTOX_LIVE (real money) requires ALL of: ...")

        monkeypatch.setattr(cli, "_load_settings", boom)
        code = cli.cmd_live_resume(type("A", (), {"allow_non_live": True})())
        payload = json.loads(capsys.readouterr().out)
        assert code == 2 and payload["resumed"] is False
        assert "refusing to resume" in payload["reason"]

    def test_status_prints_the_snapshot_and_never_the_token(self, tmp_path, capsys, monkeypatch):
        from app import __main__ as cli

        settings = self._settings(tmp_path, upstox_live_access_token="SECRET-TOK")
        monkeypatch.setattr(cli, "_load_settings", lambda: settings)
        code = cli.cmd_live_status(type("A", (), {"audit": False})())
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert code == 0 and payload["scope"].startswith("LIVE STATUS")
        assert "SECRET-TOK" not in out
        assert payload["token"]["decision"]["allowed"] is False

    def test_status_can_include_the_audit_replay(self, tmp_path, capsys, monkeypatch):
        from app import __main__ as cli

        settings = self._settings(tmp_path)
        database, repository = cli._open_ops_repository(settings)
        repository.record_live_order({
            "id": "o1", "symbol": "S", "side": "BUY", "quantity": 1,
            "price": 100.0, "status": "FILLED", "created_ts": "t0"})
        repository.record_live_transition("o1", "c", "FILLED", 1, 100.0, "t0", {})
        database.close()
        monkeypatch.setattr(cli, "_load_settings", lambda: settings)
        cli.cmd_live_status(type("A", (), {"audit": True})())
        payload = json.loads(capsys.readouterr().out)
        assert payload["audit_replay"]["live"]["turnover"] == pytest.approx(100.0)
        assert payload["audit_replay"]["sandbox"]["turnover"] == 0.0

    def test_status_reports_open_orders_from_the_right_table(self, tmp_path, capsys, monkeypatch):
        from app import __main__ as cli

        settings = self._settings(tmp_path)
        database, repository = cli._open_ops_repository(settings)
        repository.record_sandbox_order({
            "id": "sbox-1", "symbol": "S", "side": "BUY", "quantity": 1,
            "price": 1.0, "status": "OPEN", "created_ts": "t0"})
        database.close()
        monkeypatch.setattr(cli, "_load_settings", lambda: settings)
        cli.cmd_live_status(type("A", (), {"audit": False})())
        payload = json.loads(capsys.readouterr().out)
        assert payload["open_orders"] == 1


class TestStuckOrderEscalation:
    """A stuck order is a WARNING, then a CRITICAL, then (optionally) cancelled."""

    def _watchdog(self, tmp_path, **kwargs):
        from app.live.watchdog import LiveWatchdog

        repository, database = repo(tmp_path, "stuck.db")
        repository.record_live_order({
            "id": "live-abc", "intent_id": "intent-1", "symbol": "S",
            "side": "BUY", "quantity": 1, "price": 100.0, "status": "OPEN",
            "created_ts": "2026-10-02T09:00:00+00:00"})
        class Live:
            LIVE = True

        watchdog = LiveWatchdog(repository, broker=Live(), stuck_order_minutes=5,
                                stuck_order_escalate_minutes=15, **kwargs)
        return repository, database, watchdog

    def test_just_past_the_threshold_is_a_warning(self, tmp_path):
        repository, database, watchdog = self._watchdog(tmp_path)
        findings = watchdog.check_stuck_orders(
            now=datetime(2026, 10, 2, 9, 6, tzinfo=timezone.utc))
        assert [f.severity for f in findings] == ["WARNING"]
        assert findings[0].payload["escalated"] is False
        assert findings[0].payload["escalate_minutes"] == 15
        database.close()

    def test_past_the_escalation_window_is_critical(self, tmp_path):
        repository, database, watchdog = self._watchdog(tmp_path)
        findings = watchdog.check_stuck_orders(
            now=datetime(2026, 10, 2, 9, 20, tzinfo=timezone.utc))
        assert [f.severity for f in findings] == ["CRITICAL"]
        assert findings[0].payload["escalated"] is True
        assert "ESCALATED" in findings[0].message
        database.close()

    def test_a_final_order_is_never_reported(self, tmp_path):
        repository, database, watchdog = self._watchdog(tmp_path)
        repository.record_live_transition("live-abc", "c1", "FILLED", 1, 100.0,
                                          "2026-10-02T09:02:00+00:00", {})
        findings = watchdog.check_stuck_orders(
            now=datetime(2026, 10, 2, 9, 30, tzinfo=timezone.utc))
        assert findings == []
        database.close()

    def test_escalate_below_stuck_is_clamped_not_inverted(self, tmp_path):
        from app.live.watchdog import LiveWatchdog

        repository, database = repo(tmp_path, "clamp.db")
        watchdog = LiveWatchdog(repository, stuck_order_minutes=10,
                                stuck_order_escalate_minutes=2)
        assert watchdog.stuck_order_escalate_minutes == 10
        database.close()

    def test_settings_reject_an_inverted_ladder(self, tmp_path):
        with pytest.raises(ValueError, match="ESCALATE_MINUTES"):
            Settings(api_token="t", database_url=f"sqlite:///{tmp_path}/x.db",
                     live_stuck_order_minutes=20,
                     live_stuck_order_escalate_minutes=5)

    def test_ops_exposes_the_escalated_ids_only(self):
        from app.live.ops import LiveOperations

        ops = LiveOperations(None)
        findings = [
            {"kind": "STUCK_ORDER", "severity": "WARNING",
             "payload": {"order_id": "live-warn"}},
            {"kind": "STUCK_ORDER", "severity": "CRITICAL",
             "payload": {"order_id": "live-critical"}},
            {"kind": "RECONCILE_STALLED", "severity": "CRITICAL", "payload": {}},
        ]
        assert ops.escalated_stuck_order_ids(findings) == {"live-critical"}
        assert ops.escalated_stuck_order_ids([]) == set()

    def test_auto_cancel_is_off_by_default(self, tmp_path):
        from app.live.ops import LiveOperations

        settings = Settings(api_token="t",
                            database_url=f"sqlite:///{tmp_path}/off.db")
        ops = LiveOperations.from_settings(settings, None)
        assert ops.auto_cancel_stuck is False

    def test_engine_does_nothing_when_the_flag_is_off(self, tmp_path):
        """The default must never cancel anything."""
        from app.engine.sandbox_engine import SandboxEngine

        repository, database = repo(tmp_path, "engine-off.db")
        repository.record_live_order({
            "id": "live-abc", "intent_id": "intent-1", "symbol": "S",
            "side": "BUY", "quantity": 1, "price": 100.0, "status": "OPEN",
            "created_ts": "2026-10-02T09:00:00+00:00"})

        class Ops:
            auto_cancel_stuck = False

            def observe(self, *, now=None):
                return [{"kind": "STUCK_ORDER", "severity": "CRITICAL",
                         "payload": {"order_id": "live-abc"}}]

        class Broker:
            orders = {"intent-1": type("S", (), {"our_id": "live-abc",
                                                 "status": "OPEN"})()}

        engine = SandboxEngine.__new__(SandboxEngine)
        engine.repository = repository
        engine.live_ops = Ops()
        engine.broker = Broker()
        cancelled = engine._auto_cancel_escalated_stuck_orders(
            [{"kind": "STUCK_ORDER", "severity": "CRITICAL",
              "payload": {"order_id": "live-abc"}}])
        assert cancelled == []
        database.close()

    def test_engine_cancels_an_escalated_order_when_enabled(self, tmp_path, caplog):
        from app.engine.sandbox_engine import SandboxEngine
        from app.live.ops import LiveOperations

        repository, database = repo(tmp_path, "engine-on.db")
        repository.record_live_order({
            "id": "live-abc", "intent_id": "intent-1", "symbol": "S",
            "side": "BUY", "quantity": 1, "price": 100.0, "status": "OPEN",
            "created_ts": "2026-10-02T09:00:00+00:00"})
        ops = LiveOperations.__new__(LiveOperations)
        ops.auto_cancel_stuck = True

        class Broker:
            def __init__(self):
                self.orders = {"intent-1": type(
                    "S", (), {"our_id": "live-abc", "status": "OPEN"})()}
                self.cancelled = []

            def cancel(self, intent_id):
                self.cancelled.append(intent_id)
                return {"status": "CANCELLED"}

        broker = Broker()
        engine = SandboxEngine.__new__(SandboxEngine)
        engine.repository = repository
        engine.live_ops = ops
        engine.broker = broker
        cancelled = engine._auto_cancel_escalated_stuck_orders(
            [{"kind": "STUCK_ORDER", "severity": "CRITICAL",
              "payload": {"order_id": "live-abc"}},
             {"kind": "STUCK_ORDER", "severity": "WARNING",
              "payload": {"order_id": "live-warn"}}])
        assert cancelled == ["live-abc"]
        assert broker.cancelled == ["intent-1"]
        events = [event["kind"] for event in repository.list_system_events(limit=50)]
        assert "STUCK_ORDER_AUTO_CANCEL" in events
        database.close()

    def test_a_failed_auto_cancel_is_recorded_not_swallowed(self, tmp_path):
        from app.engine.sandbox_engine import SandboxEngine
        from app.live.ops import LiveOperations

        repository, database = repo(tmp_path, "engine-fail.db")
        ops = LiveOperations.__new__(LiveOperations)
        ops.auto_cancel_stuck = True

        class Broker:
            def __init__(self):
                self.orders = {"intent-1": type(
                    "S", (), {"our_id": "live-abc", "status": "OPEN"})()}

            def cancel(self, intent_id):
                raise RuntimeError("network down")

        engine = SandboxEngine.__new__(SandboxEngine)
        engine.repository = repository
        engine.live_ops = ops
        engine.broker = Broker()
        cancelled = engine._auto_cancel_escalated_stuck_orders(
            [{"kind": "STUCK_ORDER", "severity": "CRITICAL",
              "payload": {"order_id": "live-abc"}}])
        assert cancelled == []
        events = [event["kind"] for event in repository.list_system_events(limit=50)]
        assert "STUCK_ORDER_AUTO_CANCEL_FAILED" in events
        database.close()


class TestReadinessRecordedState:
    """Readiness must also judge the RECORDED state a fresh engine will inherit."""

    def _settings(self, tmp_path, **kwargs):
        base = dict(broker="UPSTOX_LIVE", mode="LIVE", live_trading=True,
                    live_confirm="I-ACCEPT-REAL-MONEY-RISK",
                    upstox_live_access_token="t", max_position_value=10_000,
                    max_open_positions=1, max_daily_loss=500, max_trades_per_day=3,
                    data_source="upstox", market_calendar="nse-session",
                    default_timeframe="5m",
                    database_url=f"sqlite:///{tmp_path}/live.db")
        base.update(kwargs)
        return Settings(**base)

    def _named(self, checks, name):
        return next(check for check in checks if check.name == name)

    def test_a_clean_environment_passes_every_recorded_state_check(self, tmp_path):
        repository, database = repo(tmp_path, "clean.db")
        checks = run_checks(self._settings(tmp_path), repository=repository)
        for name in ("audit_trail_integrity", "emergency_stop_clear",
                     "no_open_orders_left", "no_unresolved_intents",
                     "reconciliation_live"):
            assert self._named(checks, name).ok is True, name
        database.close()

    def test_a_persisted_emergency_stop_is_an_error(self, tmp_path):
        repository, database = repo(tmp_path, "stopped.db")
        repository.set_engine_state("emergency_stop", {"stopped": True,
                                                       "reason": "test"})
        checks = run_checks(self._settings(tmp_path), repository=repository)
        check = self._named(checks, "emergency_stop_clear")
        assert check.ok is False and check.severity == "ERROR"
        assert "live-resume" in check.detail
        database.close()

    def test_working_orders_are_a_warning_not_an_error(self, tmp_path):
        repository, database = repo(tmp_path, "open.db")
        repository.record_live_order({
            "id": "live-1", "symbol": "S", "side": "BUY", "quantity": 1,
            "price": 1.0, "status": "OPEN", "created_ts": "t0"})
        checks = run_checks(self._settings(tmp_path), repository=repository)
        check = self._named(checks, "no_open_orders_left")
        assert check.ok is False and check.severity == "WARNING"
        assert "never run two engines" in check.detail
        database.close()

    def test_unresolved_intents_are_an_error(self, tmp_path):
        from app.engine.state import SubmissionJournal, UNKNOWN

        repository, database = repo(tmp_path, "intents.db")
        journal = SubmissionJournal(repository)
        journal.record("intent-1", instrument_key="NSE_EQ|INE1", symbol="S",
                       side="BUY", quantity=1, price=1.0, strategy="t")
        journal.mark("intent-1", UNKNOWN)
        checks = run_checks(self._settings(tmp_path), repository=repository)
        check = self._named(checks, "no_unresolved_intents")
        assert check.ok is False and check.severity == "ERROR"
        database.close()

    def test_a_stale_reconciliation_stamp_is_a_warning(self, tmp_path):
        from datetime import datetime, timedelta, timezone

        repository, database = repo(tmp_path, "stale.db")
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        repository.set_engine_state("last_reconcile_at", {"ts": old})
        checks = run_checks(self._settings(tmp_path), repository=repository)
        check = self._named(checks, "reconciliation_live")
        assert check.ok is False and check.severity == "WARNING"
        database.close()

    def test_a_fresh_reconciliation_stamp_passes(self, tmp_path):
        from datetime import datetime, timezone

        repository, database = repo(tmp_path, "fresh.db")
        repository.set_engine_state("last_reconcile_at",
                                    {"ts": datetime.now(timezone.utc).isoformat()})
        checks = run_checks(self._settings(tmp_path), repository=repository)
        assert self._named(checks, "reconciliation_live").ok is True
        database.close()

    def test_a_missing_database_never_reads_as_verified(self, tmp_path):
        """Recorded-state checks without a repository must not claim "clean"."""
        checks = run_checks(self._settings(tmp_path))
        assert self._named(checks, "four_switch_opt_in").ok is True
        for name in ("audit_trail_integrity", "emergency_stop_clear",
                     "no_open_orders_left", "no_unresolved_intents",
                     "reconciliation_live"):
            check = self._named(checks, name)
            assert check.ok is False, name
            assert check.severity == "INFO", name
            assert check.detail.startswith("not checked"), name


class TestAuditIntegrity:
    """The audit trail must be internally consistent before it is trusted."""

    def test_a_clean_trail_is_ok(self, tmp_path):
        from app.live.pnl import integrity_report

        repository, database = repo(tmp_path, "ok.db")
        repository.record_live_order({
            "id": "live-1", "symbol": "S", "side": "BUY", "quantity": 5,
            "price": 10.0, "status": "FILLED", "created_ts": "t0"})
        repository.record_live_transition("live-1", "c1", "FILLED", 5, 10.0, "t0", {})
        report = integrity_report(repository, live=True)
        assert report["ok"] is True and report["problems"] == 0
        assert report["orders"] == 1 and report["fills"] == 1
        database.close()

    def test_an_orphan_fill_is_reported(self, tmp_path):
        from app.live.pnl import integrity_report

        repository, database = repo(tmp_path, "orphan.db")
        repository.record_live_transition("live-ghost", "c1", "FILLED", 5, 10.0,
                                          "t0", {})
        report = integrity_report(repository, live=True)
        assert report["ok"] is False
        assert report["orphan_fills"] == ["live-ghost"]
        database.close()

    def test_a_filled_order_with_no_transitions_is_reported(self, tmp_path):
        from app.live.pnl import integrity_report

        repository, database = repo(tmp_path, "missing.db")
        repository.record_live_order({
            "id": "live-1", "symbol": "S", "side": "BUY", "quantity": 5,
            "price": 10.0, "status": "FILLED", "created_ts": "t0"})
        report = integrity_report(repository, live=True)
        assert report["missing_fills"] == ["live-1"]
        database.close()

    def test_a_quantity_mismatch_is_reported(self, tmp_path):
        from app.live.pnl import integrity_report

        repository, database = repo(tmp_path, "mismatch.db")
        repository.record_live_order({
            "id": "live-1", "symbol": "S", "side": "BUY", "quantity": 5,
            "price": 10.0, "status": "FILLED", "created_ts": "t0"})
        repository.record_live_transition("live-1", "c1", "FILLED", 3, 10.0, "t0", {})
        report = integrity_report(repository, live=True)
        assert report["quantity_mismatch"][0]["executed_quantity"] == 3
        assert report["quantity_mismatch"][0]["order_quantity"] == 5
        database.close()

    def test_working_orders_are_not_expected_to_have_fills(self, tmp_path):
        from app.live.pnl import integrity_report

        repository, database = repo(tmp_path, "working.db")
        repository.record_live_order({
            "id": "live-1", "symbol": "S", "side": "BUY", "quantity": 5,
            "price": 10.0, "status": "OPEN", "created_ts": "t0"})
        report = integrity_report(repository, live=True)
        assert report["ok"] is True
        database.close()


class TestLiveDrill:
    """The drill proves each rail fires — offline, and honestly."""

    def test_the_drill_passes_every_step(self, tmp_path):
        from app.live.drill import run_drill

        report = run_drill(database_url=f"sqlite:///{tmp_path}/drill.db")
        failed = [step["name"] for step in report["steps"] if not step["ok"]]
        assert report["ok"] is True, f"failed steps: {failed}"
        assert len(report["steps"]) == 14
        names = {step["name"] for step in report["steps"]}
        assert {"four_switch_gate", "host_inversion_guard", "token_guard_lifecycle",
                "canary_ramp", "entries_gated_exits_free",
                "breach_triggers_emergency_stop", "stuck_order_escalates_and_cancels",
                "audit_integrity_detects_corruption",
                "alerts_durable_and_scrubbed", "failed_page_is_retried",
                "unacknowledged_alert_escalates", "single_engine_lock",
                "halt_works_with_a_broken_config",
                "no_live_host_contacted"} == names
        # every step carries evidence, not just a verdict
        assert all(step["evidence"] for step in report["steps"])
        json.dumps(report, default=str)

    def test_the_drill_never_touches_a_live_host(self, tmp_path):
        from app.live.drill import run_drill

        report = run_drill(database_url=f"sqlite:///{tmp_path}/drill.db")
        step = next(s for s in report["steps"] if s["name"] == "no_live_host_contacted")
        assert step["ok"] is True and step["evidence"]["offending"] == []

    def test_a_temporary_database_is_removed_only_when_the_drill_is_clean(
            self, monkeypatch, tmp_path):
        from app.live import drill

        temporary = tmp_path / "throwaway"
        temporary.mkdir()

        def fake_mkdtemp(prefix=""):
            return str(temporary)

        monkeypatch.setattr(drill.tempfile, "mkdtemp", fake_mkdtemp)
        report = drill.run_drill()
        assert report["ok"] is True and report["temporary_database"] is True
        assert report.get("temporary_database_removed") is True
        assert not temporary.exists()

    def test_a_failing_step_is_reported_and_its_database_kept(
            self, monkeypatch, tmp_path):
        from app.live import drill

        temporary = tmp_path / "throwaway-fail"
        temporary.mkdir()
        monkeypatch.setattr(drill.tempfile, "mkdtemp", lambda prefix="": str(temporary))

        def broken(ctx):
            raise RuntimeError("rail did not fire")

        monkeypatch.setattr(drill, "STEPS", (broken,))
        report = drill.run_drill()
        assert report["ok"] is False and report["failed"] == ["broken"]
        assert report["steps"][0]["evidence"]["error_type"] == "RuntimeError"
        assert "did not fire" in report["steps"][0]["detail"]
        # the evidence database is deliberately left behind for inspection
        assert temporary.exists() and not report.get("temporary_database_removed")

    def test_the_cli_reports_success_and_failure_exit_codes(self, tmp_path, capsys,
                                                            monkeypatch):
        from app import __main__ as cli
        from app.live import drill

        args = type("A", (), {"database_url": f"sqlite:///{tmp_path}/drill.db"})()
        assert cli.cmd_live_drill(args) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True

        monkeypatch.setattr(drill, "STEPS", (
            lambda ctx: (_ for _ in ()).throw(RuntimeError("nope")),))
        assert cli.cmd_live_drill(args) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False


class TestAlertAcknowledgement:
    """An alarm nobody acknowledged is an open incident, not a closed one."""

    def _alerts(self, tmp_path, name="ack.db"):
        from app.live.alerts import AlertRouter

        repository, database = repo(tmp_path, name)
        router = AlertRouter(repository, webhook_url="")
        router.emit("STUCK_ORDER", "CRITICAL", "order is stuck")
        return repository, database

    def _ids(self, repository):
        from app.live.alerts import recent_alerts

        return recent_alerts(repository, limit=50)

    def test_acknowledging_marks_the_alert_and_records_it(self, tmp_path):
        from app.live.alerts import AlertAcknowledgements, recent_alerts

        repository, database = self._alerts(tmp_path)
        alert_id = recent_alerts(repository, limit=1)[0]["id"]
        result = AlertAcknowledgements(repository).acknowledge(
            alert_id, by="ops", note="checked the broker terminal")
        assert result["ok"] is True and result["already"] is False
        listed = recent_alerts(repository, limit=1)[0]
        assert listed["acknowledged"] is True
        assert listed["acknowledged_by"] == "ops"
        assert listed["acknowledged_at"]
        events = [e["kind"] for e in repository.list_system_events(limit=50)]
        assert "ALERT_ACKNOWLEDGED" in events
        database.close()

    def test_acknowledging_twice_is_idempotent(self, tmp_path):
        from app.live.alerts import AlertAcknowledgements, recent_alerts

        repository, database = self._alerts(tmp_path)
        alert_id = recent_alerts(repository, limit=1)[0]["id"]
        acks = AlertAcknowledgements(repository)
        first = acks.acknowledge(alert_id, by="ops")
        second = acks.acknowledge(alert_id, by="someone else")
        assert first["already"] is False and second["already"] is True
        assert recent_alerts(repository, limit=1)[0]["acknowledged_by"] == "ops"
        database.close()

    def test_an_empty_id_is_refused_not_silently_accepted(self, tmp_path):
        from app.live.alerts import AlertAcknowledgements

        repository, database = repo(tmp_path, "empty-id.db")
        result = AlertAcknowledgements(repository).acknowledge("")
        assert result["ok"] is False and "empty id" in result["reason"]
        database.close()

    def test_outstanding_critical_respects_the_age_threshold(self, tmp_path):
        from app.live.alerts import AlertAcknowledgements

        repository, database = self._alerts(tmp_path)
        acks = AlertAcknowledgements(repository)
        now = datetime.now(timezone.utc)
        assert acks.outstanding_critical(older_than_minutes=15, now=now) == []
        later = now + timedelta(minutes=20)
        outstanding = acks.outstanding_critical(older_than_minutes=15, now=later)
        assert len(outstanding) == 1 and outstanding[0]["kind"] == "STUCK_ORDER"
        assert outstanding[0]["age_minutes"] == pytest.approx(20, abs=0.5)
        database.close()

    def test_acknowledgement_clears_the_outstanding_list(self, tmp_path):
        from app.live.alerts import AlertAcknowledgements

        repository, database = self._alerts(tmp_path)
        acks = AlertAcknowledgements(repository)
        later = datetime.now(timezone.utc) + timedelta(minutes=20)
        acks.acknowledge(acks.outstanding_critical(
            older_than_minutes=15, now=later)[0]["id"])
        assert acks.outstanding_critical(older_than_minutes=15, now=later) == []
        database.close()

    def test_a_warning_never_becomes_an_outstanding_critical(self, tmp_path):
        from app.live.alerts import AlertAcknowledgements, AlertRouter

        repository, database = repo(tmp_path, "warn.db")
        AlertRouter(repository, webhook_url="").emit("SLOW", "WARNING", "just slow")
        later = datetime.now(timezone.utc) + timedelta(hours=2)
        assert AlertAcknowledgements(repository).outstanding_critical(
            older_than_minutes=15, now=later) == []
        database.close()

    def test_meta_kinds_cannot_feed_themselves(self, tmp_path):
        from app.live.alerts import AlertAcknowledgements, AlertRouter

        repository, database = repo(tmp_path, "meta.db")
        router = AlertRouter(repository, webhook_url="")
        router.emit("UNACKNOWLEDGED_ALERT", "CRITICAL", "meta")
        router.emit("ALERT_DELIVERY_FAILURE", "CRITICAL", "meta")
        later = datetime.now(timezone.utc) + timedelta(hours=2)
        assert AlertAcknowledgements(repository).outstanding_critical(
            older_than_minutes=15, now=later) == []
        database.close()

    def test_acknowledge_kind_clears_every_instance_of_one_issue(self, tmp_path):
        from app.live.alerts import AlertAcknowledgements, AlertRouter

        repository, database = repo(tmp_path, "kind.db")
        router = AlertRouter(repository, webhook_url="")
        # two identical CRITICALs would be deduped into one row, so use two
        # distinct kinds: acknowledging one kind must leave the other alone.
        router.emit("STUCK_ORDER", "CRITICAL", "one")
        router.emit("BROKER_DISABLED", "CRITICAL", "other")
        result = AlertAcknowledgements(repository).acknowledge_kind("STUCK_ORDER")
        assert result["acknowledged"] == 1
        later = datetime.now(timezone.utc) + timedelta(hours=2)
        remaining = AlertAcknowledgements(repository).outstanding_critical(
            older_than_minutes=15, now=later)
        assert [item["kind"] for item in remaining] == ["BROKER_DISABLED"]
        database.close()

    def test_the_acknowledgement_store_is_bounded(self, tmp_path):
        from app.live.alerts import (ACK_KEY, MAX_ACKNOWLEDGEMENTS,
                                     AlertAcknowledgements)

        repository, database = repo(tmp_path, "bounded.db")
        state = {f"id-{i}": {"ts": f"2026-01-01T00:00:{i:02d}+00:00", "by": "x"}
                 for i in range(MAX_ACKNOWLEDGEMENTS + 50)}
        repository.set_engine_state(ACK_KEY, state)
        AlertAcknowledgements(repository).acknowledge("newest", by="ops")
        stored = repository.get_engine_state(ACK_KEY, {})
        assert len(stored) <= MAX_ACKNOWLEDGEMENTS + 1
        assert "newest" in stored
        database.close()

    def test_the_watchdog_reports_an_unattended_critical(self, tmp_path):
        from app.live.alerts import AlertAcknowledgements
        from app.live.watchdog import LiveWatchdog

        repository, database = self._alerts(tmp_path)
        watchdog = LiveWatchdog(repository, acknowledgements=(
            AlertAcknowledgements(repository)),
            unacked_critical_minutes=15)
        later = datetime.now(timezone.utc) + timedelta(minutes=20)
        findings = watchdog.check_unacknowledged_critical(now=later)
        assert len(findings) == 1
        assert findings[0].kind == "UNACKNOWLEDGED_ALERT"
        assert findings[0].severity == "CRITICAL"
        assert "live-ack --id" in findings[0].payload["how_to_acknowledge"]
        # and it surfaces through the aggregate the engine actually calls
        kinds = {finding.kind for finding in watchdog.inspect(now=later)}
        assert "UNACKNOWLEDGED_ALERT" in kinds
        database.close()

    def test_the_watchdog_never_reports_when_disabled(self, tmp_path):
        from app.live.alerts import AlertAcknowledgements
        from app.live.watchdog import LiveWatchdog

        repository, database = self._alerts(tmp_path)
        watchdog = LiveWatchdog(repository, acknowledgements=(
            AlertAcknowledgements(repository)), unacked_critical_minutes=0)
        later = datetime.now(timezone.utc) + timedelta(hours=5)
        assert watchdog.check_unacknowledged_critical(now=later) == []
        database.close()

    def test_the_cli_lists_and_acknowledges(self, tmp_path, capsys, monkeypatch):
        from app import __main__ as cli

        repository, database = self._alerts(tmp_path, "cli-ack.db")
        settings = Settings(api_token="t",
                            database_url=f"sqlite:///{tmp_path}/cli-ack.db")
        database.close()
        monkeypatch.setattr(cli, "_load_settings", lambda: settings)

        cli.cmd_live_ack(type("A", (), {"id": [], "kind": "", "all": False,
                                        "by": "", "note": ""})())
        listed = json.loads(capsys.readouterr().out)
        assert len(listed["outstanding_critical"]) == 1  # nothing acknowledged yet
        assert "hint" in listed

        cli.cmd_live_ack(type("A", (), {"id": [], "kind": "STUCK_ORDER",
                                        "all": False, "by": "ops",
                                        "note": "checked"})())
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True and payload["acknowledged"] == 1
        assert payload["status"]["recorded"] == 1

    def test_the_api_exposes_and_updates_acknowledgement(self, tmp_path):
        from app.api.server import create_app
        from app.live.alerts import AlertRouter
        from fastapi.testclient import TestClient

        settings = Settings(
            api_token="t", database_url=f"sqlite:///{tmp_path}/api-ack.db")
        headers = {"Authorization": "Bearer t"}
        with TestClient(create_app(settings, data_dir=tmp_path)) as client:
            AlertRouter(client.app.state.repository, webhook_url="").emit(
                "STUCK_ORDER", "CRITICAL", "stuck")
            listed = client.get("/api/v2/alerts", headers=headers).json()
            assert listed["unacknowledged_critical"] == 1
            alert_id = listed["alerts"][0]["id"]

            response = client.post("/api/v2/alerts/ack",
                                   json={"ids": [alert_id], "by": "api-test"},
                                   headers=headers)
            body = response.json()
            assert response.status_code == 200 and body["acknowledged"] == 1
            after = client.get("/api/v2/alerts", headers=headers).json()
            assert after["unacknowledged_critical"] == 0
            assert after["alerts"][0]["acknowledged"] is True

            nothing = client.post("/api/v2/alerts/ack", json={}, headers=headers)
            assert nothing.json()["ok"] is False
            # the ack surface is authenticated like every other v2 route
            assert client.post("/api/v2/alerts/ack", json={"all": True}
                               ).status_code in (401, 403)


class TestAlertDeliveryRetry:
    """A page that failed once must be tried again — bounded and observable."""

    def _flaky(self, outcomes, repository, **kwargs):
        import httpx

        from app.live.alerts import AlertRouter

        calls = []

        class Flaky(httpx.BaseTransport):
            def handle_request(self, request):
                calls.append(request)
                outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
                if isinstance(outcome, Exception):
                    raise outcome
                return httpx.Response(outcome)

        router = AlertRouter(
            repository, webhook_url="https://ops.example/hook",
            transport=httpx.Client(transport=Flaky()), **kwargs)
        return router, calls

    def test_a_failed_delivery_is_queued_then_delivered(self, tmp_path):
        import httpx

        repository, database = repo(tmp_path, "retry.db")
        router, calls = self._flaky([httpx.ConnectError("down"), 200], repository,
                                    retry_backoff_seconds=60)
        router.emit("K", "CRITICAL", "m")
        assert router.delivery_failures == 1 and router.queued_for_retry == 1
        assert router.status()["pending_retries"] == 1

        # not due yet
        early = router.retry_pending(now=datetime.now(timezone.utc))
        assert early == {"attempted": 0, "delivered": 0, "dropped": 0, "pending": 1}

        # 2 minutes later the backoff has elapsed and the page lands
        later = datetime.now(timezone.utc) + timedelta(minutes=2)
        result = router.retry_pending(now=later)
        assert result["delivered"] == 1 and result["pending"] == 0
        assert router.retried_deliveries == 1
        assert len(calls) == 2
        database.close()

    def test_a_persistently_failing_webhook_is_abandoned_after_the_cap(
            self, tmp_path):
        import httpx

        repository, database = repo(tmp_path, "abandon.db")
        router, _ = self._flaky([httpx.ConnectError("down")], repository,
                                retry_max_attempts=3, retry_backoff_seconds=1)
        router.emit("K", "CRITICAL", "m")
        now = datetime.now(timezone.utc)
        for _ in range(5):
            now = now + timedelta(minutes=5)
            router.retry_pending(now=now)
        # the queue is empty (bounded) and the abandonment is recorded
        assert router.status()["pending_retries"] == 0
        kinds = {e["kind"] for e in repository.list_system_events(limit=100)}
        assert "ALERT_DELIVERY_ABANDONED" in kinds
        database.close()

    def test_zero_attempts_disables_retries_entirely(self, tmp_path):
        import httpx

        repository, database = repo(tmp_path, "no-retry.db")
        router, calls = self._flaky([httpx.ConnectError("down")], repository,
                                    retry_max_attempts=0)
        router.emit("K", "CRITICAL", "m")
        assert router.status()["pending_retries"] == 0
        assert router.retry_pending()["attempted"] == 0
        assert len(calls) == 1  # only the original attempt
        database.close()

    def test_the_retry_tick_is_wired_into_the_engine(self, tmp_path):
        """The engine's ops tick is what gives a failed page another chance."""
        import httpx

        from app.engine.sandbox_engine import SandboxEngine
        from app.live.ops import LiveOperations

        repository, database = repo(tmp_path, "engine-retry.db")
        router, calls = self._flaky([httpx.ConnectError("down"), 200], repository,
                                    retry_backoff_seconds=1)
        ops = LiveOperations(repository, alerts=router)
        router.emit("K", "CRITICAL", "m")

        engine = SandboxEngine.__new__(SandboxEngine)
        engine.repository = repository
        engine.live_ops = ops
        later = datetime.now(timezone.utc) + timedelta(minutes=2)
        result = engine._retry_failed_alerts_once(now=later)
        assert result["delivered"] == 1
        assert len(calls) == 2
        database.close()

    def test_the_queue_is_bounded(self, tmp_path):
        import httpx

        from app.live.alerts import MAX_PENDING

        repository, database = repo(tmp_path, "bounded-retry.db")
        router, _ = self._flaky([httpx.ConnectError("down")], repository,
                                retry_backoff_seconds=3600)
        for index in range(MAX_PENDING + 20):
            router.emit(f"K{index}", "CRITICAL", "m")
        assert router.status()["pending_retries"] <= MAX_PENDING
        database.close()

    def test_a_recovered_webhook_leaves_no_pending_state(self, tmp_path):
        import httpx

        repository, database = repo(tmp_path, "recovered.db")
        router, _ = self._flaky([200], repository, retry_backoff_seconds=1)
        router.emit("K", "CRITICAL", "m")
        assert router.delivery_failures == 0
        assert router.status()["pending_retries"] == 0
        database.close()


class TestOpsDigest:
    """One readable artifact per window: alerts, rails, orders, audit PnL."""

    def _populated(self, tmp_path, name="digest.db"):
        from app.live.alerts import AlertRouter

        repository, database = repo(tmp_path, name)
        NOW_ISO = datetime.now(timezone.utc).isoformat()
        AlertRouter(repository, webhook_url="").emit(
            "STUCK_ORDER", "CRITICAL", "an order is stuck")
        AlertRouter(repository, webhook_url="", now_fn=lambda: datetime.now(
            timezone.utc) + timedelta(seconds=1)).emit(
            "ENTRY_BLOCKED_TOKEN", "CRITICAL", "entry blocked")
        repository.record_system_event("ENTRY_GUARD_BLOCKED",
                                       {"reason": "TOKEN_UNVERIFIED"})
        repository.record_system_event("ENTRY_GUARD_REDUCED",
                                       {"adjusted_quantity": 3})
        repository.record_live_order({
            "id": "live-dig-1", "intent_id": "intent-dig", "symbol": "S",
            "side": "BUY", "quantity": 10, "price": 100.0,
            "status": "FILLED", "created_ts": NOW_ISO})
        repository.record_live_transition("live-dig-1", "child-1", "FILLED", 10,
                                          100.0, NOW_ISO, {})
        repository.record_live_order({
            "id": "live-dig-2", "intent_id": "intent-dig-2", "symbol": "S",
            "side": "SELL", "quantity": 10, "price": 110.0,
            "status": "FILLED", "created_ts": NOW_ISO})
        repository.record_live_transition("live-dig-2", "child-2", "FILLED", 10,
                                          110.0, NOW_ISO, {})
        repository.set_engine_state("emergency_stop",
                                    {"stopped": True, "reason": "AUTO_STOP X"})
        repository.record_system_event("EMERGENCY_STOP", {"reason": "AUTO_STOP X"})
        return repository, database

    def test_the_digest_folds_the_window(self, tmp_path):
        from app.live.digest import build_digest

        repository, database = self._populated(tmp_path)
        digest = build_digest(repository, hours=24, live=True)
        assert digest["alerts"]["total"] == 2
        assert digest["alerts"]["by_severity"] == {"CRITICAL": 2}
        assert digest["alerts"]["unacknowledged_critical"] == 2
        assert digest["guard_actions"] == {"blocked": 1, "reduced": 1}
        assert digest["stops"]["count"] == 1
        assert digest["orders"]["by_status"] == {"FILLED": 2}
        assert digest["pnl"]["realized_pre_fee"] == pytest.approx(100.0)
        assert digest["pnl"]["fills_total"] == 2
        assert digest["audit_integrity"]["ok"] is True
        assert digest["headline"]["stops"] == 1
        database.close()

    def test_the_window_excludes_older_events(self, tmp_path):
        from app.live.digest import build_digest

        repository, database = self._populated(tmp_path, "window.db")
        now = datetime.now(timezone.utc)
        # a one-second window starting "now" sees none of the events above
        digest = build_digest(repository, hours=1 / 3600, live=True,
                              now=now - timedelta(minutes=5))
        assert digest["alerts"]["total"] == 0
        assert digest["guard_actions"] == {"blocked": 0, "reduced": 0}
        assert digest["headline"]["stops"] == 0
        database.close()

    def test_an_acknowledged_critical_is_no_longer_unacknowledged(self, tmp_path):
        from app.live.alerts import AlertAcknowledgements
        from app.live.digest import build_digest

        repository, database = self._populated(tmp_path, "ack-digest.db")
        acks = AlertAcknowledgements(repository)
        for alert in recent_alerts(repository, limit=10):
            acks.acknowledge(alert["id"], by="ops")
        digest = build_digest(repository, hours=24, live=True)
        assert digest["alerts"]["unacknowledged_critical"] == 0
        assert digest["acks"]["recorded"] == 2
        database.close()

    def test_the_sandbox_trail_is_used_for_a_sandbox_digest(self, tmp_path):
        from app.live.digest import build_digest

        repository, database = self._populated(tmp_path, "mixed-digest.db")
        digest = build_digest(repository, hours=24, live=False)
        assert digest["mode"] == "sandbox"
        assert digest["pnl"]["realized_pre_fee"] == 0.0
        assert "sandbox_transitions" in digest["pnl"]["source"]
        database.close()

    def test_markdown_renders_the_headline_numbers(self, tmp_path):
        from app.live.digest import build_digest, digest_markdown

        repository, database = self._populated(tmp_path, "md.db")
        text = digest_markdown(build_digest(repository, hours=24, live=True))
        assert "# LIVE OPS DIGEST (live)" in text
        assert "Unacknowledged CRITICAL alerts: **2**" in text
        assert "Audit trail consistent: **True**" in text
        assert "Realized PnL (pre-fee, audit trail): **100.0**" in text
        database.close()

    def test_an_empty_database_still_produces_a_valid_digest(self, tmp_path):
        from app.live.digest import build_digest, digest_markdown

        repository, database = repo(tmp_path, "empty-digest.db")
        digest = build_digest(repository, hours=24)
        assert digest["headline"]["critical_unacknowledged"] == 0
        assert digest["audit_integrity"]["ok"] is True
        assert digest_markdown(digest).startswith("# LIVE OPS DIGEST")
        json.dumps(digest, default=str)
        database.close()

    def test_posting_reports_honestly_when_there_is_no_webhook(self, tmp_path):
        from app.live.alerts import AlertRouter
        from app.live.digest import build_digest, post_digest

        repository, database = self._populated(tmp_path, "nopost.db")
        result = post_digest(AlertRouter(repository, webhook_url=""),
                             build_digest(repository))
        assert result["posted"] is False
        assert "no ALERT_WEBHOOK_URL" in result["reason"]
        database.close()

    def test_posting_uses_the_tested_webhook_path(self, tmp_path):
        import httpx

        from app.live.alerts import AlertRouter
        from app.live.digest import build_digest, post_digest

        repository, database = self._populated(tmp_path, "post.db")
        seen = []

        class Capture(httpx.BaseTransport):
            def handle_request(self, request):
                seen.append(json.loads(request.content.decode()))
                return httpx.Response(200)

        router = AlertRouter(repository, webhook_url="https://ops.example/hook",
                             transport=httpx.Client(transport=Capture()))
        result = post_digest(router, build_digest(repository, hours=24))
        assert result["posted"] is True
        assert seen[0]["kind"] == "OPS_DIGEST"
        assert seen[0]["payload"]["headline"]["stops"] == 1
        database.close()

    def test_a_failed_post_is_reported_not_raised(self, tmp_path):
        import httpx

        from app.live.alerts import AlertRouter
        from app.live.digest import build_digest, post_digest

        repository, database = self._populated(tmp_path, "badpost.db")

        class Boom(httpx.BaseTransport):
            def handle_request(self, request):
                raise httpx.ConnectError("down")

        router = AlertRouter(repository, webhook_url="https://ops.example/hook",
                             transport=httpx.Client(transport=Boom()))
        result = post_digest(router, build_digest(repository))
        assert result["posted"] is False and "failed" in result["reason"]
        database.close()

    def test_the_cli_prints_json_markdown_and_writes_a_file(
            self, tmp_path, capsys, monkeypatch):
        from app import __main__ as cli

        self._populated(tmp_path, "cli-digest.db")
        settings = Settings(api_token="t",
                            database_url=f"sqlite:///{tmp_path}/cli-digest.db")
        monkeypatch.setattr(cli, "_load_settings", lambda: settings)

        def args(**kwargs):
            base = dict(hours=24.0, post=False, markdown=False, out="")
            base.update(kwargs)
            return type("A", (), base)()

        assert cli.cmd_live_digest(args()) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["headline"]["stops"] == 1

        target = tmp_path / "digest.md"
        assert cli.cmd_live_digest(args(markdown=True, out=str(target))) == 0
        assert "LIVE OPS DIGEST" in capsys.readouterr().out
        assert "LIVE OPS DIGEST" in target.read_text()

        assert cli.cmd_live_digest(args(post=True)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["post"]["posted"] is False  # no webhook configured


class TestSingleEngineLock:
    """One account, one live engine: a per-order gate cannot catch this."""

    def _lock(self, tmp_path, name="lock.db", **kwargs):
        from app.live.lock import EngineLock

        repository, database = repo(tmp_path, name)
        return EngineLock(repository, **kwargs), repository, database

    def _prove_dead_local_owner(self, repository, database):
        """Model a crashed PID on this host; stale time alone is insufficient."""
        import socket
        from app.live.lock import LOCK_KEY

        with database.transaction() as connection:
            connection.execute(
                "UPDATE engine_lease SET pid=?, host=? WHERE name=?",
                (2_147_483_647, socket.gethostname(), LOCK_KEY))

    def test_a_second_live_engine_is_refused(self, tmp_path):
        now = datetime.now(timezone.utc)
        first, repository, database = self._lock(tmp_path, owner="host-a:1")
        second = first.__class__(repository, live=True, owner="host-b:2")
        assert first.acquire(now=now).granted is True
        decision = second.acquire(now=now)
        assert decision.granted is False
        assert "two live engines" in decision.reason
        assert decision.existing.owner == "host-a:1"
        database.close()

    def test_heartbeat_keeps_the_lock_alive_and_release_frees_it(self, tmp_path):
        now = datetime.now(timezone.utc)
        first, repository, database = self._lock(tmp_path, owner="host-a:1")
        second = first.__class__(repository, live=True, owner="host-b:2")
        first.acquire(now=now)
        first.heartbeat(now=now + timedelta(minutes=1))
        assert second.acquire(now=now + timedelta(minutes=1)).granted is False
        assert first.release() is True
        assert second.acquire(now=now + timedelta(minutes=1)).granted is True
        database.close()

    def test_a_stale_lock_never_blocks_a_restart(self, tmp_path):
        now = datetime.now(timezone.utc)
        first, repository, database = self._lock(tmp_path, owner="host-a:1",
                                                 stale_after_seconds=180)
        second = first.__class__(repository, live=True, owner="host-b:2",
                                 stale_after_seconds=180)
        first.acquire(now=now)                      # then the process dies
        self._prove_dead_local_owner(repository, database)
        decision = second.acquire(now=now + timedelta(minutes=5))
        assert decision.granted is True
        assert second.status(now=now + timedelta(minutes=5))["owner"] == "host-b:2"
        database.close()

    def test_reused_owner_label_cannot_renew_from_a_different_pid(self, tmp_path):
        import socket
        from app.live.lock import EngineLock, LOCK_KEY

        now = datetime.now(timezone.utc)
        first, repository, database = self._lock(
            tmp_path, name="same-owner.db", owner="stable-label")
        assert first.acquire(now=now).granted
        with database.transaction() as connection:
            connection.execute(
                "UPDATE engine_lease SET pid=?, host=? WHERE name=?",
                (2_147_483_646, socket.gethostname(), LOCK_KEY))
        second = EngineLock(repository, live=True, owner="stable-label")
        decision = second.acquire(now=now)
        assert decision.granted is False
        assert "two live engines" in decision.reason
        database.close()

    def test_stale_heartbeat_with_live_local_pid_is_not_takeover_proof(
            self, tmp_path):
        now = datetime.now(timezone.utc)
        first, repository, database = self._lock(
            tmp_path, name="live-pid.db", owner="host-a:1")
        second = first.__class__(repository, live=True, owner="host-b:2")
        assert first.acquire(now=now).granted
        decision = second.acquire(now=now + timedelta(hours=2))
        assert decision.granted is False
        assert "local PID is alive" in decision.reason
        database.close()

    def test_stale_remote_owner_is_not_automatically_taken_over(self, tmp_path):
        import socket
        from app.live.lock import LOCK_KEY

        now = datetime.now(timezone.utc)
        first, repository, database = self._lock(
            tmp_path, name="remote-owner.db", owner="remote:1")
        assert first.acquire(now=now).granted
        with database.transaction() as connection:
            connection.execute(
                "UPDATE engine_lease SET host=?, heartbeat_ts=? WHERE name=?",
                ("unreachable-host", (now - timedelta(hours=2)).isoformat(),
                 LOCK_KEY))
        second = first.__class__(repository, live=True, owner="local:2")
        decision = second.acquire(now=now)
        assert decision.granted is False
        assert "remote owner PID cannot be verified" in decision.reason
        assert socket.gethostname() != "unreachable-host"
        database.close()

    def test_force_cannot_override_a_fresh_heartbeat(self, tmp_path):
        # FINAL-11/10 §15: force is NOT a casual bypass. Against a fresh
        # heartbeat the takeover must be refused even under force.
        now = datetime.now(timezone.utc)
        first, repository, database = self._lock(tmp_path, owner="host-a:1")
        second = first.__class__(repository, live=True, owner="host-b:2")
        first.acquire(now=now)
        decision = second.acquire(now=now, force=True)
        assert decision.granted is False
        assert "two live engines" in decision.reason
        assert first.status(now=now)["owner"] == "host-a:1"
        database.close()

    def test_force_can_take_over_a_stale_owner(self, tmp_path):
        now = datetime.now(timezone.utc)
        first, repository, database = self._lock(tmp_path, owner="host-a:1",
                                                 stale_after_seconds=180)
        second = first.__class__(repository, live=True, owner="host-b:2",
                                 stale_after_seconds=180)
        first.acquire(now=now)
        self._prove_dead_local_owner(repository, database)
        decision = second.acquire(now=now + timedelta(minutes=5), force=True)
        assert decision.granted is True
        assert "stale" in decision.reason
        database.close()

    def test_takeover_is_visible_to_the_old_owner(self, tmp_path):
        # After a stale takeover the dead owner's heartbeat must fail, so a
        # zombie process notices it lost ownership instead of trading anyway.
        now = datetime.now(timezone.utc)
        first, repository, database = self._lock(tmp_path, owner="host-a:1",
                                                 stale_after_seconds=60)
        second = first.__class__(repository, live=True, owner="host-b:2",
                                 stale_after_seconds=60)
        first.acquire(now=now)
        self._prove_dead_local_owner(repository, database)
        assert second.acquire(now=now + timedelta(minutes=10)).granted is True
        assert first.heartbeat(now=now + timedelta(minutes=10)) is False
        assert first.held is False
        database.close()

    def test_two_racing_engines_yield_exactly_one_owner(self, tmp_path):
        # FINAL-11/10 §15 atomicity: independent spawn-started processes use
        # separate database connections and contend at the same barrier. The
        # conditional SQL lease must grant exactly one owner on every OS.
        import multiprocessing

        db_path = tmp_path / "race.db"
        repository, database = repo(tmp_path, "race.db")  # creates the schema
        now_iso = datetime.now(timezone.utc).isoformat()
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        contenders = 6
        barrier = context.Barrier(contenders)
        database_url = f"sqlite:///{db_path}"
        processes = [
            context.Process(
                target=_race_live_engine_lease_worker,
                args=(database_url, index, now_iso, barrier, queue))
            for index in range(contenders)
        ]
        results = []
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(45)
            still_running = [process for process in processes if process.is_alive()]
            if still_running:
                for process in still_running:
                    process.terminate()
                for process in still_running:
                    process.join(5)
                pytest.fail("spawned lease contenders did not finish within 45 seconds")
            assert [process.exitcode for process in processes] == [0] * contenders
            try:
                results = [queue.get(timeout=10) for _ in processes]
            except Empty:
                pytest.fail("a lease contender exited without reporting its result")
            errors = [(index, error) for index, _, error in results if error]
            assert not errors, f"lease contender errors: {errors}"
            winners = [index for index, granted, _ in results if granted]
            assert len(winners) == 1, f"expected exactly one owner, got {winners}"
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(5)
            queue.close()
            queue.join_thread()
            database.close()

    def test_paper_engines_are_never_blocked(self, tmp_path):
        now = datetime.now(timezone.utc)
        live, repository, database = self._lock(tmp_path, name="mixed-lock.db",
                                                live=True, owner="live:1")
        live.acquire(now=now)
        paper = live.__class__(repository, live=False, owner="paper:2")
        assert paper.acquire(now=now).granted is True  # never refuses paper
        # …and never disturbs the live owner's lease either.
        assert live.status(now=now)["owner"] == "live:1"
        database.close()

    def test_a_foreign_release_is_refused(self, tmp_path):
        now = datetime.now(timezone.utc)
        first, repository, database = self._lock(tmp_path, name="release.db",
                                                 owner="host-a:1")
        second = first.__class__(repository, live=True, owner="host-b:2")
        first.acquire(now=now)
        assert second.release() is False           # never steal someone's lock
        assert first.status(now=now)["owner"] == "host-a:1"
        database.close()

    def test_status_reports_the_heartbeat_age(self, tmp_path):
        now = datetime.now(timezone.utc)
        lock, repository, database = self._lock(tmp_path, name="status.db",
                                                owner="host-a:1")
        lock.acquire(now=now)
        status = lock.status(now=now + timedelta(seconds=90))
        assert status["held"] is True and status["stale"] is False
        assert status["heartbeat_age_seconds"] == pytest.approx(90, abs=1)
        aged_but_live = lock.status(now=now + timedelta(hours=2))
        assert aged_but_live["stale"] is False
        assert aged_but_live["owner_liveness"] == "alive"
        database.close()

    def test_live_lock_refuses_repository_without_atomic_lease_api(self):
        from app.live.lock import EngineLock

        class LegacyRepository:
            def get_engine_state(self, key, default=None):
                return default

            def set_engine_state(self, key, value):
                return None

        lock = EngineLock(LegacyRepository(), live=True, owner="live:1")
        decision = lock.acquire(now=NOW)
        assert decision.granted is False
        assert "atomic" in decision.reason
        status = lock.status(now=NOW)
        assert status["readable"] is True
        assert status["atomic_supported"] is False

    def test_unreadable_live_lease_refuses_start_and_reports_unknown(self):
        from app.live.lock import EngineLock

        class BrokenRepository:
            def try_acquire_lease(self, *args, **kwargs):
                raise RuntimeError("db-secret=must-not-leak")

            def read_lease(self, name):
                raise RuntimeError("db-secret=must-not-leak")

            def get_engine_state(self, key, default=None):
                raise RuntimeError("db-secret=must-not-leak")

        lock = EngineLock(BrokenRepository(), live=True, owner="live:1")
        decision = lock.acquire(now=NOW)
        assert decision.granted is False
        assert "must-not-leak" not in decision.reason
        status = lock.status(now=NOW)
        assert status["readable"] is False and status["known"] is False
        assert status["held"] is None
        assert "must-not-leak" not in str(status)

    def test_corrupt_heartbeat_is_not_treated_as_proof_of_stale_owner(
            self, tmp_path):
        from app.live.lock import EngineLock, LOCK_KEY

        now = datetime.now(timezone.utc)
        first, repository, database = self._lock(
            tmp_path, name="corrupt-heartbeat.db", owner="host-a:1")
        assert first.acquire(now=now).granted
        with database.transaction() as connection:
            connection.execute(
                "UPDATE engine_lease SET heartbeat_ts=? WHERE name=?",
                ("not-a-timestamp", LOCK_KEY))
        second = EngineLock(repository, live=True, owner="host-b:2")
        decision = second.acquire(now=now + timedelta(days=10))
        assert decision.granted is False
        status = second.status(now=now + timedelta(days=10))
        assert status["held"] is True and status["stale"] is False
        assert status["heartbeat_age_seconds"] is None
        database.close()

    def test_preflight_blocks_when_lease_read_is_unknown(self, tmp_path):
        from app.live.preflight import ERROR, run_readiness_center_checks

        repository, database = repo(tmp_path, "unreadable-lease.db")

        class BrokenLeaseView:
            def __getattr__(self, name):
                return getattr(repository, name)

            def read_lease(self, name):
                raise RuntimeError("database unavailable")

        checks = run_readiness_center_checks(
            TestPreflightChecks()._live_settings(),
            repository=BrokenLeaseView())
        check = next(item for item in checks
                     if item.name == "single_instance_lock")
        assert check.ok is False and check.severity == ERROR
        assert "UNKNOWN/unreadable" in check.detail
        database.close()

    def test_the_engine_refuses_to_run_beside_another_live_engine(self, tmp_path):
        from app.live.lock import EngineLock

        ops, ops_repo, ops_db = TestLiveOperationsFacade()._ops(tmp_path)
        engine, recording, repository, database, Candle = (
            TestEngineIntegration()._engine(tmp_path, ops))
        now = datetime.now(timezone.utc)
        rival = EngineLock(repository, live=True, owner="rival:999")
        rival.acquire(now=now)
        engine.live_lock = EngineLock(repository, live=True, owner="this:1000")
        result = engine.run(max_candles=1)
        assert result["ok"] is False
        assert engine.state == "REFUSED_DUPLICATE_ENGINE"
        assert "two live engines" in result["reason"]
        assert recording.submitted == []       # nothing was ever sent
        kinds = {e["kind"] for e in repository.list_system_events(limit=50)}
        assert "ENGINE_REFUSED_DUPLICATE" in kinds
        database.close()
        ops_db.close()

    def test_stale_takeover_forces_reconcile_and_kills_the_old_epoch(self, tmp_path):
        # FINAL-11/10 §15: after taking over a stale lease the new engine must
        # reconcile BEFORE trading and must NOT inherit the dead owner's epoch.
        from app.live.gateway import AuthorizationEpochs
        from app.live.lock import EngineLock

        ops, ops_repo, ops_db = TestLiveOperationsFacade()._ops(tmp_path)
        engine, recording, repository, database, Candle = (
            TestEngineIntegration()._engine(tmp_path, ops))

        class EmptyProvider:
            symbols = ["RELIANCE"]

            async def stream(self, symbols, timeframe):
                return
                yield  # async generator that emits nothing

        engine.provider = EmptyProvider()

        # The dead owner had been ACTIVE: it left a valid authorization epoch.
        epochs = AuthorizationEpochs(repository)
        epochs.activate(actor="dead-owner")
        assert epochs.current()["valid"] is True

        # Dead owner's lease, heartbeat far older than the stale window.
        rival = EngineLock(repository, live=True, owner="dead:1",
                           stale_after_seconds=60)
        rival.acquire(now=datetime.now(timezone.utc) - timedelta(minutes=30))
        import socket
        from app.live.lock import LOCK_KEY
        with database.transaction() as connection:
            connection.execute(
                "UPDATE engine_lease SET pid=?, host=? WHERE name=?",
                (2_147_483_647, socket.gethostname(), LOCK_KEY))

        reconcile_calls = {"n": 0}
        real_reconcile = engine.reconcile

        def spy_reconcile():
            reconcile_calls["n"] += 1
            return real_reconcile()

        engine.reconcile = spy_reconcile

        engine.live_lock = EngineLock(repository, live=True, owner="new:2",
                                      stale_after_seconds=60)
        engine.run(max_candles=1)

        assert engine.state == "STOPPED"          # ran, was not refused
        assert reconcile_calls["n"] >= 1          # reconciled before trading
        assert epochs.current()["valid"] is False  # dead epoch unusable
        kinds = {e["kind"] for e in repository.list_system_events(limit=200)}
        assert "LIVE_TAKEOVER_RECONCILE" in kinds
        assert "LIVE_EPOCH_INVALIDATED_ON_TAKEOVER" in kinds
        database.close()
        ops_db.close()

    def test_the_lock_is_exposed_in_the_engine_status(self, tmp_path):
        ops, ops_repo, ops_db = TestLiveOperationsFacade()._ops(tmp_path)
        engine, recording, repository, database, Candle = (
            TestEngineIntegration()._engine(tmp_path, ops))
        engine.live_lock = ops.make_lock()
        engine.live_lock.acquire()
        status = engine.status()
        assert status["engine_lock"]["held"] is True
        assert status["engine_lock"]["owner"]
        database.close()
        ops_db.close()

    def test_settings_require_a_sane_staleness_window(self, tmp_path):
        with pytest.raises(ValueError, match="LIVE_LOCK_STALE_SECONDS"):
            Settings(api_token="t", database_url=f"sqlite:///{tmp_path}/x.db",
                     live_lock_stale_seconds=5)


class TestLockAndAcksOnTheOperatorSurfaces:
    """The status surfaces must show the two things that need a human."""

    def _client(self, tmp_path):
        from app.api.server import create_app
        from fastapi.testclient import TestClient

        settings = Settings(
            api_token="t", broker="UPSTOX_LIVE", mode="LIVE", live_trading=True,
            live_confirm="I-ACCEPT-REAL-MONEY-RISK", upstox_live_access_token="t",
            max_position_value=10_000, max_open_positions=1, max_daily_loss=500,
            max_trades_per_day=3, database_url=f"sqlite:///{tmp_path}/ops-api.db")
        return TestClient(create_app(settings, data_dir=tmp_path)), settings

    def test_the_api_reports_the_lock_and_the_unacknowledged_count(self, tmp_path):
        from app.live.alerts import AlertRouter
        from app.live.lock import EngineLock

        client, settings = self._client(tmp_path)
        headers = {"Authorization": "Bearer t"}
        with client:
            repository = client.app.state.repository
            EngineLock(repository, live=True, owner="engine-under-test:1").acquire()
            AlertRouter(repository, webhook_url="").emit(
                "STUCK_ORDER", "CRITICAL", "unattended")
            body = client.get("/api/v2/live/status", headers=headers).json()
        assert body["lock"]["held"] is True
        assert body["lock"]["owner"] == "engine-under-test:1"
        assert body["lock"]["stale"] is False
        assert body["unacknowledged_critical"] == 1
        assert body["pending_alert_retries"] == 0

    def test_live_status_cli_includes_lock_acks_and_retries(
            self, tmp_path, capsys, monkeypatch):
        from app import __main__ as cli
        from app.live.lock import EngineLock

        settings = Settings(api_token="t",
                            database_url=f"sqlite:///{tmp_path}/status-cli.db")
        database, repository = cli._open_ops_repository(settings)
        EngineLock(repository, live=True, owner="cli-lock:7").acquire()
        database.close()
        monkeypatch.setattr(cli, "_load_settings", lambda: settings)
        assert cli.cmd_live_status(type("A", (), {"audit": False})()) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["lock"]["owner"] == "cli-lock:7"
        assert payload["unacknowledged_critical"] == 0
        assert payload["pending_alert_retries"] == 0


class TestEngineTimeframeCoherence:
    """The stream provider and the engine must agree — they used not to."""

    def test_one_definition_is_shared_by_both_sides(self, tmp_path):
        from app.data.factory import engine_timeframe, live_timeframe_error

        for timeframe in ("1m", "5m", "15m", "30m"):
            settings = Settings(api_token="t",
                               database_url=f"sqlite:///{tmp_path}/tf.db",
                                default_timeframe=timeframe)
            assert engine_timeframe(settings) == timeframe
            assert live_timeframe_error(settings, "UPSTOX_LIVE") == ""

    def test_a_daily_default_is_refused_on_the_live_path(self, tmp_path):
        from app.data.factory import engine_timeframe, live_timeframe_error

        settings = Settings(api_token="t",
                            database_url=f"sqlite:///{tmp_path}/tf-live.db",
                            default_timeframe="1D")
        # research/paper still runs (downgraded, as before) …
        assert engine_timeframe(settings) == "1m"
        assert live_timeframe_error(settings, "UPSTOX_SANDBOX") == ""
        # … but the real-money path refuses instead of crashing at startup
        error = live_timeframe_error(settings, "UPSTOX_LIVE")
        assert "cannot drive a live engine" in error
        assert "DEFAULT_TIMEFRAME=1D" in error

    def test_preflight_flags_the_timeframe_before_anyone_starts(self, tmp_path):
        settings = Settings(
            broker="UPSTOX_LIVE", mode="LIVE", live_trading=True,
            live_confirm="I-ACCEPT-REAL-MONEY-RISK", upstox_live_access_token="t",
            max_position_value=10_000, max_open_positions=1, max_daily_loss=500,
            max_trades_per_day=3, data_source="upstox",
            market_calendar="nse-session", default_timeframe="1D",
            database_url=f"sqlite:///{tmp_path}/tf-preflight.db")
        report = summarize(run_checks(settings))
        names = {check["name"] for check in report["errors"]}
        assert "engine_timeframe_intraday" in names

    def test_the_live_engine_command_refuses_a_daily_default(self, tmp_path,
                                                             capsys, monkeypatch):
        from app import __main__ as cli
        from app.data.factory import live_timeframe_error

        settings = Settings(
            broker="UPSTOX_LIVE", mode="LIVE", live_trading=True,
            live_confirm="I-ACCEPT-REAL-MONEY-RISK", upstox_live_access_token="t",
            max_position_value=10_000, max_open_positions=1, max_daily_loss=500,
            max_trades_per_day=3, data_source="upstox",
            market_calendar="nse-session", default_timeframe="1D",
            database_url=f"sqlite:///{tmp_path}/tf-cli.db")
        monkeypatch.setattr(cli, "_load_settings", lambda: settings)
        monkeypatch.setenv("MARKET_DATA_SYMBOLS", "NSE_EQ|INE002A01018")
        monkeypatch.setenv("MARKET_DATA_ACCESS_TOKEN", "t")
        code = cli.cmd_engine(type("A", (), {"max_candles": 1,
                                             "force_live_lock": False})())
        payload = json.loads(capsys.readouterr().out)
        assert code == 2 and payload["ok"] is False
        assert payload["error"] == live_timeframe_error(settings, "UPSTOX_LIVE")


class TestMarketDataModeCoherence:
    """Readiness and the data adapter must agree on which pairs can run.

    `full` carries official aggregate bars, which Upstox publishes for 1m/30m
    only; every other timeframe is built locally from ticks (`ltpc`). Before
    this check, readiness said "ready" for a pairing the adapter then refused.
    """

    def _settings(self, tmp_path, **kwargs):
        base = dict(broker="UPSTOX_LIVE", mode="LIVE", live_trading=True,
                    live_confirm="I-ACCEPT-REAL-MONEY-RISK",
                    upstox_live_access_token="t", max_position_value=10_000,
                    max_open_positions=1, max_daily_loss=500, max_trades_per_day=3,
                    data_source="upstox", market_calendar="nse-session",
                    database_url=f"sqlite:///{tmp_path}/mdm.db")
        base.update(kwargs)
        return Settings(**base)

    def test_full_mode_is_fine_on_the_timeframes_it_carries(self, tmp_path):
        from app.data.factory import market_data_mode_error

        for timeframe in ("1m", "30m"):
            settings = self._settings(tmp_path, market_data_mode="full",
                                      default_timeframe=timeframe)
            assert market_data_mode_error(settings) == "", timeframe

    def test_full_mode_is_refused_on_a_locally_built_timeframe(self, tmp_path):
        from app.data.factory import market_data_mode_error

        settings = self._settings(tmp_path, market_data_mode="full",
                                  default_timeframe="5m")
        error = market_data_mode_error(settings)
        assert "full" in error and "1m/30m" in error and "ltpc" in error

    def test_ltpc_mode_can_build_any_engine_timeframe(self, tmp_path):
        from app.data.factory import market_data_mode_error

        for timeframe in ("1m", "5m", "15m", "30m"):
            settings = self._settings(tmp_path, market_data_mode="ltpc",
                                      default_timeframe=timeframe)
            assert market_data_mode_error(settings) == "", timeframe

    def test_the_check_is_bound_to_the_adapters_own_rule(self, tmp_path):
        """If the adapter's rule changes, this check must change with it."""
        from app.data.factory import market_data_mode_error
        from app.data.upstox_stream import UpstoxStream

        settings = self._settings(tmp_path, market_data_mode="full",
                                  default_timeframe="5m")
        assert market_data_mode_error(settings) != ""
        with pytest.raises(ValueError, match="1m/30m"):
            UpstoxStream("token", ["NSE_EQ|INE002A01018"], timeframe="5m",
                         mode="full")
        # and the pair the check allows must NOT raise from the adapter
        UpstoxStream("token", ["NSE_EQ|INE002A01018"], timeframe="1m", mode="full")

    def test_preflight_reports_it_as_an_error(self, tmp_path):
        settings = self._settings(tmp_path, market_data_mode="full",
                                  default_timeframe="5m")
        report = summarize(run_checks(settings))
        assert report["ok"] is False
        names = {check["name"] for check in report["errors"]}
        assert "market_data_mode_matches_timeframe" in names

    def test_a_valid_pairing_passes_readiness(self, tmp_path):
        settings = self._settings(tmp_path, market_data_mode="full",
                                  default_timeframe="1m")
        report = summarize(run_checks(settings))
        assert "market_data_mode_matches_timeframe" not in {
            check["name"] for check in report["errors"]}

    def test_the_engine_refuses_before_building_anything(self, tmp_path, capsys,
                                                        monkeypatch):
        from app import __main__ as cli
        from app.data.factory import market_data_mode_error

        settings = self._settings(tmp_path, market_data_mode="full",
                                  default_timeframe="5m")
        monkeypatch.setattr(cli, "_load_settings", lambda: settings)
        monkeypatch.setenv("MARKET_DATA_SYMBOLS", "NSE_EQ|INE002A01018")
        monkeypatch.setenv("MARKET_DATA_ACCESS_TOKEN", "t")
        code = cli.cmd_engine(type("A", (), {"max_candles": 1,
                                             "force_live_lock": False})())
        payload = json.loads(capsys.readouterr().out)
        assert code == 2 and payload["ok"] is False
        # the message an operator sees is the same one readiness shows
        assert payload["error"] == market_data_mode_error(settings)
