"""Offline fire drill for the V2.1 live-operations rails.

An alert configuration that has never been exercised is a hope, not a control.
This module proves, deterministically and **without any network access**, that
each rail actually fires, and reports the recorded evidence for every step:

* the four-switch gate refuses a partial live opt-in;
* a sandbox broker refuses a documented live order host, and the live broker
  refuses a sandbox host (the inversion that would be catastrophic);
* the TokenGuard blocks entries before a verification is recorded, and blocks
  them again once that verification goes stale (it is not a one-way latch);
* the CanaryGuard shrinks the first entries and the counter survives a restart;
* a guarded entry is blocked while an exit is never blocked;
* a daily-loss breach triggers the persisted EMERGENCY STOP through the engine;
* a stuck order escalates WARNING → CRITICAL and, with auto-cancel enabled, the
  engine cancels it and records `STUCK_ORDER_AUTO_CANCEL`;
* audit-trail integrity detects injected corruption (the check is not vacuous);
* alerts are durable and secret-shaped keys are redacted before storage;
* a failed webhook page is queued and delivered on the retry;
* an unattended CRITICAL alert escalates, and acknowledging it clears that;
* a second LIVE engine is refused while the first is heartbeating, while a stale
  lock never blocks a restart;
* `live-halt` still halts when the configuration itself is rejected.

Nothing here places, modifies or cancels a real order; the only broker used is
a stub held in memory. The drill writes to its own throwaway SQLite database
unless the caller supplies one explicitly.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.config.hosts import LIVE_ORDER_HOSTS, host_of

SANDBOX_HOST = "https://api-sandbox.upstox.com"
DRILL_TOKEN = "drill-token-not-a-credential"  # never a real credential


@dataclass
class Step:
    name: str
    ok: bool
    detail: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "evidence": self.evidence}


class _DrillLedger:
    """The slice of the ledger interface the engine reads."""

    realized_pnl = 0.0
    fees = 0.0
    mark = None

    def equity(self) -> float:
        return 100_000.0

    def unrealized(self) -> float:
        return 0.0


class _DrillBroker:
    """In-memory stand-in for an order broker. Never touches a network."""

    LIVE = True
    MODE = "UPSTOX_LIVE"
    mode = "UPSTOX_LIVE"
    available = True

    def __init__(self, hosts_touched: list):
        self.orders: dict = {}
        self.cancelled: list = []
        self.hosts_touched = hosts_touched
        self.ledger = _DrillLedger()
        self._base_url = SANDBOX_HOST

    def cancel(self, intent_id: str) -> dict:
        self.cancelled.append(intent_id)
        state = self.orders.get(intent_id)
        if state is not None:
            state.status = "CANCELLED"
        return {"status": "CANCELLED"}


def _settings(**overrides):
    """A fully-armed LIVE Settings object with a throwaway database."""
    from app.config.settings import Settings

    base = dict(
        broker="UPSTOX_LIVE", mode="LIVE", live_trading=True,
        live_confirm="I-ACCEPT-REAL-MONEY-RISK",
        upstox_live_access_token=DRILL_TOKEN,
        max_position_value=10_000, max_open_positions=1, max_daily_loss=500,
        max_trades_per_day=3, data_source="upstox", market_calendar="nse-session",
        default_timeframe="1m",
    )
    base.update(overrides)
    return Settings(**base)


class _Context:
    def __init__(self, database_url: str):
        from app.database.database import Database
        from app.database.repository import Repository

        self.database_url = database_url
        self.database = Database(database_url)
        self.repository = Repository(self.database)
        self.hosts_touched: list[str] = []
        self.broker = _DrillBroker(self.hosts_touched)
        self.settings = _settings(database_url=database_url)

    def close(self) -> None:
        try:
            self.database.close()
        except Exception:  # closing a drill database must never raise
            return

    def engine(self):
        """An engine shell whose code paths are the real ones."""
        from app.engine.sandbox_engine import SandboxEngine

        engine = SandboxEngine.__new__(SandboxEngine)
        engine.repository = self.repository
        engine.broker = self.broker
        engine.live_lock = None
        engine.daily = None
        engine._last_realized = 0.0
        engine._peak_equity = 0.0
        engine.state = "IDLE"
        return engine


# -- individual steps --------------------------------------------------------

def _step_four_switch_gate(ctx: _Context) -> Step:
    from app.config.settings import Settings

    partial_error = ""
    try:
        Settings(broker="UPSTOX_LIVE", mode="LIVE", live_trading=True)
    except ValueError as exc:
        partial_error = str(exc)
    complete_ok = True
    try:
        _settings(database_url=ctx.database_url)
    except ValueError as exc:  # pragma: no cover - would be a real bug
        complete_ok = False
        partial_error += f" | complete config rejected: {exc}"
    ok = bool(partial_error) and "LIVE_CONFIRM" in partial_error and complete_ok
    return Step(
        "four_switch_gate", ok,
        "a partial live opt-in is refused and a complete one is accepted"
        if ok else "the live opt-in gate did not behave as required",
        {"partial_rejection": partial_error[:200], "complete_accepted": complete_ok})


def _step_host_guard(ctx: _Context) -> Step:
    from app.brokers.live import UpstoxLiveBroker
    from app.brokers.sandbox import UpstoxSandboxBroker
    from app.config.settings import LIVE_CONFIRM_PHRASE

    sandbox_refused_live_host = False
    try:
        UpstoxSandboxBroker(DRILL_TOKEN, "NSE_EQ|INE002A01018",
                            base_url="https://api-hft.upstox.com")
    except ValueError as exc:
        sandbox_refused_live_host = "api-hft.upstox.com" in str(exc)
    live_refused_sandbox_host = False
    try:
        UpstoxLiveBroker(DRILL_TOKEN, "NSE_EQ|INE002A01018", base_url=SANDBOX_HOST,
                         confirm=LIVE_CONFIRM_PHRASE)
    except ValueError as exc:
        live_refused_sandbox_host = "sandbox" in str(exc).lower()
    live_needs_confirm = False
    try:
        UpstoxLiveBroker(DRILL_TOKEN, "NSE_EQ|INE002A01018", base_url=SANDBOX_HOST)
    except ValueError as exc:
        live_needs_confirm = "confirm" in str(exc).lower()
    ctx.hosts_touched.append(SANDBOX_HOST)
    ok = sandbox_refused_live_host and live_refused_sandbox_host and live_needs_confirm
    return Step(
        "host_inversion_guard", ok,
        "each broker refuses the other's host and the live broker demands the "
        "explicit confirm phrase" if ok
        else "a broker accepted a host (or a missing confirm) that it must refuse",
        {"sandbox_refused_live_host": sandbox_refused_live_host,
         "live_refused_sandbox_host": live_refused_sandbox_host,
         "live_requires_confirm": live_needs_confirm})


def _step_token_guard(ctx: _Context) -> Step:
    from app.live.guards import TokenGuard

    guard = TokenGuard(ctx.repository, max_age_hours=20.0)
    before = guard.check(now=datetime.now(timezone.utc))
    guard.record_verified(source="drill")
    now = datetime.now(timezone.utc)
    fresh = guard.check(now=now)
    stale = guard.check(now=now + timedelta(hours=21))
    ok = (not before.allowed and "TOKEN_UNVERIFIED" in before.reason
          and fresh.allowed and not stale.allowed)
    return Step(
        "token_guard_lifecycle", ok,
        "unverified blocks; a fresh verification allows; staleness blocks again"
        if ok else "the token guard did not block/allow as required",
        {"before": before.reason[:120], "after": fresh.reason[:120],
         "stale_after_21h": stale.reason[:120]})


def _step_canary_ramp(ctx: _Context) -> Step:
    from app.live.guards import CanaryGuard

    guard = CanaryGuard(ctx.repository, max_notional=1000.0, first_orders=2)
    oversize = guard.check(quantity=50, price=100.0)   # 5,000 > 1,000 ceiling
    guard.record_submitted(quantity=oversize.adjusted_quantity or 0, price=100.0)
    # a NEW instance reads the persisted counter: a restart cannot restart the ramp
    restarted = CanaryGuard(ctx.repository, max_notional=1000.0, first_orders=2)
    remaining_after_restart = restarted.status()["remaining"]
    second = restarted.check(quantity=50, price=100.0)
    restarted.record_submitted(quantity=second.adjusted_quantity or 0, price=100.0)
    third = CanaryGuard(ctx.repository, max_notional=1000.0,
                        first_orders=2).check(quantity=50, price=100.0)
    ok = (oversize.adjusted_quantity == 10.0 and oversize.allowed
          and remaining_after_restart == 1
          and second.adjusted_quantity == 10.0
          and third.adjusted_quantity is None and third.allowed)
    return Step(
        "canary_ramp", ok,
        "an oversized entry is shrunk to the ceiling, the counter survives a "
        "restart, and the ramp ends after the configured number of orders" if ok
        else "the canary ramp did not cap then release as required",
        {"oversize_adjusted": oversize.adjusted_quantity,
         "remaining_after_restart": remaining_after_restart,
         "second_adjusted": second.adjusted_quantity,
         "third_reason": third.reason[:80],
         "third_adjusted": third.adjusted_quantity})


def _step_entry_vs_exit(ctx: _Context) -> Step:
    from app.live.ops import LiveOperations

    # the token IS verified (previous step); stand 25 hours in the future so the
    # verification is genuinely stale — the guard must block the entry, and the
    # same guard must still let a protective exit through.
    settings = _settings(database_url=ctx.database_url, live_token_max_age_hours=20.0)
    def future():
        return datetime.now(timezone.utc) + timedelta(hours=25)

    from app.live.guards import TokenGuard

    # record the verification on the REAL clock, then judge it 25 hours later:
    # stamping with the shifted clock would defeat the point of the probe.
    TokenGuard(ctx.repository, max_age_hours=20.0).record_verified(
        source="drill-then-wait-25h")
    ops = LiveOperations.from_settings(settings, ctx.repository, ctx.broker,
                                       now_fn=future)
    entry = ops.guard_entry(side="BUY", quantity=10, price=100.0)
    exit_decision = ops.guard_entry(side="SELL", quantity=10, price=100.0)
    ok = (not entry.allowed) and "TOKEN" in entry.reason \
        and exit_decision.allowed and exit_decision.reason == "NOT_AN_ENTRY"
    return Step(
        "entries_gated_exits_free", ok,
        "a stale credential blocks a new entry but never an exit" if ok
        else "the guard blocked an exit or failed to block the entry",
        {"entry": entry.to_dict(), "exit": exit_decision.to_dict()})


def _step_breach_stops(ctx: _Context) -> Step:
    from app.live.ops import LiveOperations

    ctx.repository.set_engine_state("emergency_stop", {"stopped": False})
    settings = _settings(database_url=ctx.database_url, max_daily_loss=100.0,
                         live_auto_stop_on_breach=True)
    ops = LiveOperations.from_settings(settings, ctx.repository, ctx.broker)
    engine = ctx.engine()
    engine.live_ops = ops
    engine._last_realized = -1_000.0   # realized loss far beyond the limit
    engine._ops_tick()
    flag = ctx.repository.get_engine_state("emergency_stop") or {}
    reason = str(flag.get("reason", ""))
    ok = bool(flag.get("stopped")) and "AUTO_STOP DAILY_LOSS_BREACH" in reason
    ctx.repository.set_engine_state("emergency_stop", {"stopped": False})
    return Step(
        "breach_triggers_emergency_stop", ok,
        "a daily-loss breach triggers the persisted stop" if ok
        else "the breach did not stop the engine",
        {"stopped": bool(flag.get("stopped")), "reason": reason[:160]})


def _step_stuck_order(ctx: _Context) -> Step:
    from app.live.ops import LiveOperations

    intent = "drill-stuck-intent"
    our_id = f"live-{intent[:12]}"
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
    ctx.repository.record_live_order({
        "id": our_id, "intent_id": intent, "symbol": "DRILL", "side": "BUY",
        "quantity": 1, "price": 100.0, "order_type": "LIMIT", "status": "OPEN",
        "created_ts": old_ts, "updated_ts": old_ts})
    ctx.broker.orders[intent] = type("State", (), {"our_id": our_id,
                                                   "status": "OPEN"})()
    settings = _settings(database_url=ctx.database_url, live_stuck_order_minutes=5,
                         live_stuck_order_escalate_minutes=15,
                         live_auto_cancel_stuck=True)
    ops = LiveOperations.from_settings(settings, ctx.repository, ctx.broker)
    early = ops.watchdog.check_stuck_orders(
        now=datetime.now(timezone.utc) - timedelta(minutes=54))  # 6 min old
    engine = ctx.engine()
    engine.live_ops = ops
    engine._ops_tick()
    events = [event["kind"] for event in ctx.repository.list_system_events(limit=100)]
    ok = (ctx.broker.cancelled.count(intent) == 1
          and "STUCK_ORDER_AUTO_CANCEL" in events
          and early and early[0].severity == "WARNING")
    return Step(
        "stuck_order_escalates_and_cancels", ok,
        "a 6-minute-old order warns, an hour-old one is cancelled by the engine"
        if ok else "stuck-order escalation or auto-cancel did not fire",
        {"early_severity": early[0].severity if early else None,
         "cancelled": ctx.broker.cancelled.count(intent),
         "event_recorded": "STUCK_ORDER_AUTO_CANCEL" in events})


def _step_audit_integrity(ctx: _Context) -> Step:
    from app.live.pnl import integrity_report

    clean_id = "live-drill-clean"
    ctx.repository.record_live_order({
        "id": clean_id, "intent_id": "drill-clean-intent", "symbol": "DRILL",
        "side": "BUY", "quantity": 1, "price": 100.0, "status": "FILLED",
        "created_ts": "2026-01-01T00:00:00+00:00"})
    ctx.repository.record_live_transition(clean_id, "child-clean", "FILLED", 1,
                                          100.0, "2026-01-01T00:00:00+00:00", {})
    clean = integrity_report(ctx.repository, live=True)
    ctx.repository.record_live_transition("live-drill-orphan", "child-orphan",
                                          "FILLED", 1, 100.0,
                                          "2026-01-01T00:00:00+00:00", {})
    dirty = integrity_report(ctx.repository, live=True)
    ok = clean["ok"] is True and dirty["ok"] is False \
        and "live-drill-orphan" in dirty["orphan_fills"]
    return Step(
        "audit_integrity_detects_corruption", ok,
        "integrity passes on a clean trail and catches an injected orphan fill"
        if ok else "audit integrity did not detect the injected corruption",
        {"clean_ok": clean["ok"], "after_injection_ok": dirty["ok"],
         "orphans": dirty["orphan_fills"]})


def _step_alerts_durable_and_scrubbed(ctx: _Context) -> Step:
    from app.live.alerts import AlertRouter

    # obviously-fake drill value: it MUST be redacted before it is stored
    fake_secret = "drill-fake-value-must-never-be-stored"
    router = AlertRouter(ctx.repository, webhook_url="")
    router.emit("DRILL_ALERT", "CRITICAL", "drill alert",
                access_token=fake_secret, access_token_present=True)
    blob = json.dumps(ctx.repository.list_system_events(limit=100), default=str)
    ok = ("DRILL_ALERT" in blob and fake_secret not in blob
          and "[redacted]" in blob)
    return Step(
        "alerts_durable_and_scrubbed", ok,
        "the alert is recorded and a secret-shaped key is redacted"
        if ok else "the alert was not recorded, or a secret was stored in clear",
        {"recorded": "DRILL_ALERT" in blob,
         "secret_absent": fake_secret not in blob,
         "redaction_present": "[redacted]" in blob})


def _step_alert_retry(ctx: _Context) -> Step:
    """A webhook that failed once must be retried, not silently dropped."""
    import httpx

    from app.live.alerts import AlertRouter

    calls: list[int] = []

    class Flaky(httpx.BaseTransport):
        def handle_request(self, request):
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ConnectError("drill: simulated outage")
            return httpx.Response(200)

    router = AlertRouter(
        ctx.repository, webhook_url="https://drill.invalid/hook",
        transport=httpx.Client(transport=Flaky()), retry_backoff_seconds=1)
    ctx.hosts_touched.append("https://drill.invalid")
    router.emit("DRILL_PAGE", "CRITICAL", "drill page")
    queued = router.status()["pending_retries"]
    result = router.retry_pending(
        now=datetime.now(timezone.utc) + timedelta(minutes=1))
    ok = (router.delivery_failures == 1 and queued == 1
          and result["delivered"] == 1 and len(calls) == 2)
    return Step(
        "failed_page_is_retried", ok,
        "a failed webhook delivery is queued, then delivered on the retry"
        if ok else "a failed webhook delivery was not retried",
        {"failed_attempts": router.delivery_failures, "queued": queued,
         "delivered_on_retry": result["delivered"], "total_posts": len(calls)})


def _step_unacknowledged_alert(ctx: _Context) -> Step:
    """A CRITICAL nobody acknowledged must become an incident of its own."""
    from app.live.alerts import AlertAcknowledgements
    from app.live.watchdog import LiveWatchdog

    acks = AlertAcknowledgements(ctx.repository)
    # a CRITICAL recorded 30 minutes ago and never acknowledged
    ctx.repository.record_system_event(
        "ALERT", {"kind": "DRILL_CRITICAL", "severity": "CRITICAL",
                  "message": "drill: unattended critical"},
        ts=(datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat())
    watchdog = LiveWatchdog(ctx.repository, acknowledgements=acks,
                            unacked_critical_minutes=15)
    findings = [finding for finding in watchdog.check_unacknowledged_critical()
                if finding.payload.get("alert_kind") == "DRILL_CRITICAL"]
    escalated = bool(findings) and findings[0].severity == "CRITICAL"
    acknowledged = "not attempted"
    cleared = False
    if findings:
        alert_id = findings[0].payload["alert_id"]
        acknowledged = acks.acknowledge(alert_id, by="drill", note="fire drill")["ok"]
        cleared = not [finding for finding in
                       watchdog.check_unacknowledged_critical()
                       if finding.payload.get("alert_kind") == "DRILL_CRITICAL"]
    ok = escalated and acknowledged is True and cleared
    return Step(
        "unacknowledged_alert_escalates", ok,
        "an unattended CRITICAL is escalated and acknowledging it clears it"
        if ok else "an unattended CRITICAL was not escalated, or ack did not clear it",
        {"escalated": escalated, "acknowledged": acknowledged, "cleared": cleared})


def _step_single_engine_lock(ctx: _Context) -> Step:
    """Two live engines on one account would double every entry."""
    from app.live.lock import EngineLock

    now = datetime.now(timezone.utc)
    first = EngineLock(ctx.repository, live=True, owner="drill-engine-a:1")
    second = EngineLock(ctx.repository, live=True, owner="drill-engine-b:2")
    third = EngineLock(ctx.repository, live=True, owner="drill-engine-c:3")
    non_live = EngineLock(ctx.repository, live=False, owner="drill-paper-d:4")

    granted = first.acquire(now=now).granted
    refused = second.acquire(now=now)
    forced = second.acquire(now=now, force=True)
    # after the stale window a crashed engine must never block a restart
    revived = third.acquire(now=now + timedelta(minutes=30))
    # and a paper/sandbox engine is never blocked by a live lock
    paper = non_live.acquire(now=now)
    first.release()
    ok = (granted and not refused.granted and "two live engines" in refused.reason
          and forced.granted and revived.granted and paper.granted)
    return Step(
        "single_engine_lock", ok,
        "a second live engine is refused, force works, and a stale lock never "
        "blocks a restart" if ok else "the single-engine lock did not hold",
        {"first": granted, "second_refused": not refused.granted,
         "refusal_reason": refused.reason[:120], "forced": forced.granted,
         "stale_lock_allows_restart": revived.granted,
         "paper_not_blocked": paper.granted})


def _step_halt_with_broken_config(ctx: _Context) -> Step:
    from app import __main__ as cli

    saved = {key: os.environ.get(key) for key in ("BROKER", "MODE", "DATABASE_URL")}
    payload: dict = {}
    try:
        os.environ["BROKER"] = "UPSTOX_LIVE"          # invalid on its own
        os.environ.pop("MODE", None)
        os.environ["DATABASE_URL"] = ctx.database_url
        import io

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.cmd_live_halt(type("Args", (), {"reason": "drill"})())
        payload = json.loads(buffer.getvalue() or "{}")
        payload["exit_code"] = code
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    flag = ctx.repository.get_engine_state("emergency_stop") or {}
    ctx.repository.set_engine_state("emergency_stop", {"stopped": False})
    ok = (bool(flag.get("stopped")) and payload.get("halted") is True
          and payload.get("configuration_rejected") is True)
    return Step(
        "halt_works_with_a_broken_config", ok,
        "live-halt still halts when the configuration is rejected" if ok
        else "live-halt did not halt (or did not report the broken configuration)",
        {"halted": payload.get("halted"),
         "configuration_rejected": payload.get("configuration_rejected"),
         "exit_code": payload.get("exit_code")})


def _step_no_live_host(ctx: _Context) -> Step:
    touched = sorted(set(ctx.hosts_touched))
    offending = sorted(set(touched) & LIVE_ORDER_HOSTS)
    ok = not offending
    return Step(
        "no_live_host_contacted", ok,
        "every host the drill touched is a sandbox/local host" if ok
        else "the drill touched a documented live order host",
        {"hosts_touched": touched, "live_hosts_avoided": sorted(LIVE_ORDER_HOSTS),
         "offending": offending})


STEPS = (
    _step_four_switch_gate,
    _step_host_guard,
    _step_token_guard,
    _step_canary_ramp,
    _step_entry_vs_exit,
    _step_breach_stops,
    _step_stuck_order,
    _step_audit_integrity,
    _step_alerts_durable_and_scrubbed,
    _step_alert_retry,
    _step_unacknowledged_alert,
    _step_single_engine_lock,
    _step_halt_with_broken_config,
    _step_no_live_host,
)


def run_drill(*, database_url: str | None = None) -> dict:
    """Run every step against a throwaway database. Never raises."""
    temporary = database_url is None
    cleanup_dir = None
    if temporary:
        cleanup_dir = tempfile.mkdtemp(prefix="axiom-live-drill-")
        database_url = f"sqlite:///{cleanup_dir}/drill.db"

    context = None
    steps: list[Step] = []
    try:
        context = _Context(database_url)
    except Exception as exc:
        return {"scope": "LIVE OPS DRILL (offline, no network, no orders)",
                "ok": False, "database_url": database_url, "steps": [],
                "error": f"could not open the drill database: {type(exc).__name__}"}
    try:
        for step_fn in STEPS:
            name = step_fn.__name__.replace("_step_", "")
            try:
                steps.append(step_fn(context))
            except Exception as exc:  # a failed step is reported, never hidden
                steps.append(Step(name, False,
                                  f"step raised {type(exc).__name__}: {exc}",
                                  {"error_type": type(exc).__name__}))
    finally:
        context.close()

    failed = [step.name for step in steps if not step.ok]
    report = {
        "scope": "LIVE OPS DRILL (offline, no network, no orders)",
        "ok": not failed,
        "database_url": database_url,
        "temporary_database": temporary,
        "steps": [step.to_dict() for step in steps],
        "failed": failed,
        "note": ("a fire drill for the safety rails; no broker order was placed, "
                 "modified or cancelled and no network request was made"),
    }
    if cleanup_dir and not failed:
        # a clean drill leaves nothing behind; a failed one keeps its database
        # so the operator can inspect the recorded evidence
        import shutil

        shutil.rmtree(cleanup_dir, ignore_errors=True)
        report["temporary_database_removed"] = True
    return report
