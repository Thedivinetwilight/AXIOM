"""LiveOperations — one object the engine consults for operational safety.

Keeps the engine's constructor small and the behaviour testable:

    ops = LiveOperations.from_settings(settings, repository, broker)
    ...
    decision = ops.guard_entry(side="BUY", quantity=..., price=...)
    for alert in ops.observe(...): ...            # watchdog findings → alerts
    breach = ops.breach(realized_today=..., equity=..., peak_equity=...)

Everything is optional and side-effect free unless a guard actually blocks or an
alert actually fires, so the paper/sandbox paths are unaffected when
``live_ops=None``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.live.alerts import AlertAcknowledgements, AlertRouter
from app.live.guards import CanaryGuard, GuardDecision, TokenGuard
from app.live.watchdog import BreachMonitor, LiveWatchdog

logger = logging.getLogger(__name__)


class LiveOperations:
    def __init__(self, repository, *, broker=None, alerts: AlertRouter | None = None,
                 token_guard: TokenGuard | None = None,
                 canary: CanaryGuard | None = None,
                 watchdog: LiveWatchdog | None = None,
                 breaches: BreachMonitor | None = None,
                 acknowledgements: AlertAcknowledgements | None = None,
                 auto_cancel_stuck: bool = False,
                 lock_stale_seconds: int = 180,
                 now_fn=None):
        self.repository = repository
        self.broker = broker
        self.alerts = alerts
        self.token_guard = token_guard
        self.canary = canary
        self.watchdog = watchdog
        self.breaches = breaches
        self.acknowledgements = acknowledgements
        #: when True the ENGINE (not this observer) cancels findings the
        #: watchdog has escalated to CRITICAL. Off unless explicitly enabled.
        self.auto_cancel_stuck = bool(auto_cancel_stuck)
        #: a lock whose heartbeat is older than this is stale (its engine died)
        self.lock_stale_seconds = max(30, int(lock_stale_seconds))
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        #: counters for the dashboard/status
        self.blocked_entries = 0
        self.reduced_entries = 0
        self.findings_seen = 0
        self.breaches_seen = 0

    # -- construction --------------------------------------------------------

    @classmethod
    def from_settings(cls, settings, repository, broker=None, *,
                      transport=None, now_fn=None) -> "LiveOperations":
        alerts = AlertRouter(
            repository,
            webhook_url=getattr(settings, "alert_webhook_url", ""),
            min_severity=getattr(settings, "alert_min_severity", "WARNING"),
            dedup_seconds=getattr(settings, "alert_dedup_seconds", 300),
            retry_max_attempts=getattr(settings, "alert_retry_attempts", 3),
            retry_backoff_seconds=getattr(
                settings, "alert_retry_backoff_seconds", 60),
            transport=transport, now_fn=now_fn)
        token_guard = TokenGuard(
            repository,
            max_age_hours=getattr(settings, "live_token_max_age_hours", 20.0),
            warn_minutes=getattr(settings, "live_token_warn_minutes", 120),
            now_fn=now_fn)
        canary = CanaryGuard(
            repository,
            max_notional=getattr(settings, "live_canary_max_notional", 0.0),
            first_orders=getattr(settings, "live_canary_orders", 10),
            now_fn=now_fn)
        acknowledgements = AlertAcknowledgements(repository, now_fn=now_fn)
        watchdog = LiveWatchdog(
            repository, broker=broker, token_guard=token_guard,
            acknowledgements=acknowledgements,
            unacked_critical_minutes=getattr(
                settings, "live_unacked_critical_minutes", 15),
            stuck_order_minutes=getattr(settings, "live_stuck_order_minutes", 5),
            stuck_order_escalate_minutes=getattr(
                settings, "live_stuck_order_escalate_minutes", 15),
            reconcile_silence_minutes=getattr(
                settings, "live_reconcile_silence_minutes", 10),
            now_fn=now_fn)
        breaches = BreachMonitor(
            repository,
            max_daily_loss=getattr(settings, "max_daily_loss", 0.0),
            max_equity_drawdown=getattr(settings, "live_max_equity_drawdown", 0.0),
            auto_stop=getattr(settings, "live_auto_stop_on_breach", True))
        ops = cls(repository, broker=broker, alerts=alerts, token_guard=token_guard,
                  canary=canary, watchdog=watchdog, breaches=breaches,
                  acknowledgements=acknowledgements,
                  auto_cancel_stuck=getattr(settings, "live_auto_cancel_stuck", False),
                  lock_stale_seconds=getattr(
                      settings, "live_lock_stale_seconds", 180),
                  now_fn=now_fn)
        # the watchdog needs the journal, which the engine owns; the engine
        # attaches it after construction (see SandboxEngine.__init__).
        return ops

    def make_lock(self):
        """The single-engine lock for this session (live engines enforce it)."""
        from app.live.lock import EngineLock

        live = bool(getattr(self.broker, "LIVE", False))
        return EngineLock(self.repository,
                          stale_after_seconds=self.lock_stale_seconds,
                          live=live, now_fn=self._now)

    def attach_journal(self, journal) -> None:
        """Give the watchdog visibility of unresolved submission intents."""
        if self.watchdog is not None:
            self.watchdog.intent_journal = journal

    # -- entry guard ---------------------------------------------------------

    def guard_entry(self, *, side: str, quantity: float,
                    price: float) -> GuardDecision:
        """Gate a NEW ENTRY. Never call this for exits/risk-reducing orders.

        Order of evaluation is deliberate: the credential check comes first
        (nothing is trustworthy if the session is stale), then the canary.
        """
        if str(side).upper() != "BUY":
            return GuardDecision(True, "NOT_AN_ENTRY")
        if self.token_guard is not None:
            decision = self.token_guard.check(now=self._now())
            if not decision.allowed:
                self.blocked_entries += 1
                self._alert("ENTRY_BLOCKED_TOKEN", "CRITICAL",
                            f"new entry blocked: {decision.reason}",
                            side=side, quantity=quantity, price=price,
                            **decision.to_dict())
                return decision
        if self.canary is not None:
            decision = self.canary.check(quantity=quantity, price=price)
            if not decision.allowed:
                self.blocked_entries += 1
                self._alert("ENTRY_BLOCKED_CANARY", "WARNING",
                            f"new entry blocked by the canary ramp: {decision.reason}",
                            side=side, quantity=quantity, price=price)
                return decision
            if decision.adjusted_quantity is not None:
                self.reduced_entries += 1
                self._alert("ENTRY_REDUCED_CANARY", "INFO",
                            f"entry reduced by the canary ramp: {decision.reason}",
                            side=side, quantity=quantity, price=price,
                            adjusted_quantity=decision.adjusted_quantity)
            return decision
        return GuardDecision(True, "NO_GUARDS")

    def record_submitted_entry(self, *, quantity: float, price: float) -> None:
        """Count a ramped entry AFTER the broker accepted the submission."""
        if self.canary is not None and self.canary.enabled:
            self.canary.record_submitted(quantity=quantity, price=price)

    # -- observation ---------------------------------------------------------

    def observe(self, *, now: datetime | None = None) -> list[dict]:
        """Run the watchdog, route findings as alerts, return the findings."""
        if self.watchdog is None:
            return []
        findings = []
        for finding in self.watchdog.inspect(now=now):
            self.findings_seen += 1
            self._alert(finding.kind, finding.severity, finding.message,
                        **finding.payload)
            findings.append(finding.to_dict())
        return findings

    def escalated_stuck_order_ids(self, findings: list[dict]) -> set[str]:
        """Audit order ids the watchdog escalated to CRITICAL (decision only).

        Kept here so the *policy* (which findings warrant action) is testable
        without a broker, while the *action* stays in the engine, which owns
        order submission and cancellation.
        """
        return {
            str(finding.get("payload", {}).get("order_id"))
            for finding in findings or []
            if finding.get("kind") == "STUCK_ORDER"
            and finding.get("severity") == "CRITICAL"
            and finding.get("payload", {}).get("order_id")
        }

    def retry_failed_alerts(self, *, now=None, limit: int = 10) -> dict:
        """Retry pages whose webhook delivery failed (bounded, never raises)."""
        if self.alerts is None:
            return {"attempted": 0, "delivered": 0, "dropped": 0, "pending": 0}
        try:
            return self.alerts.retry_pending(now=now, limit=limit)
        except Exception as exc:  # a retry failure must never break the loop
            from app.live.alerts import logger as _logger

            _logger.error("ALERT_RETRY_TICK_FAILURE",
                          extra={"error": type(exc).__name__})
            return {"attempted": 0, "delivered": 0, "dropped": 0, "pending": -1}

    def breach(self, *, realized_today: float, equity: float | None = None,
               peak_equity: float | None = None) -> dict | None:
        """Evaluate account-level escalations; returns the breach to act on."""
        if self.breaches is None:
            return None
        found = self.breaches.evaluate(realized_today=realized_today,
                                       equity=equity, peak_equity=peak_equity)
        if found is None:
            return None
        self.breaches_seen += 1
        self._alert(found.kind, found.severity, found.message, **found.payload)
        return found.to_dict()

    def _alert(self, kind: str, severity: str, message: str, **payload) -> None:
        if self.alerts is not None:
            self.alerts.emit(kind, severity, message, **payload)

    # -- reporting -----------------------------------------------------------

    def status(self) -> dict:
        return {
            "blocked_entries": self.blocked_entries,
            "reduced_entries": self.reduced_entries,
            "findings_seen": self.findings_seen,
            "breaches_seen": self.breaches_seen,
            "canary": self.canary.status() if self.canary else None,
            "token": self.token_guard.status(now=self._now())
            if self.token_guard else None,
            "breaches": self.breaches.status() if self.breaches else None,
            "auto_cancel_stuck": self.auto_cancel_stuck,
            "lock": (self.make_lock().status() if self.repository is not None
                     else None),
            "acks": (self.acknowledgements.status()
                     if self.acknowledgements else None),
            "alerts": self.alerts.status() if self.alerts else None,
        }


def read_emergency_stop(repository) -> tuple[bool, str]:
    """Independent read of the persisted emergency-stop flag (master spec §37).

    Emergency controls must not depend on the main engine object being alive,
    so this reads the durable flag directly. Returns ``(active, reason)``.

    FINAL-11/10 §63: an UNREADABLE flag behaves as STOPPED, never as clear —
    failing open on the emergency stop would be the worst possible direction.
    The reason string carries the word "unreadable" so callers can still
    distinguish "clear" from "fail-closed default" in diagnostics.
    """
    try:
        flag = repository.get_engine_state("emergency_stop", {}) or {}
        if not isinstance(flag, dict):
            raise TypeError("emergency-stop record has an invalid shape")
    except Exception as exc:
        error_type = type(exc).__name__
        logger.error("EMERGENCY_FLAG_UNREADABLE: fail closed (stopped)",
                     extra={"error_type": error_type})
        return True, ("emergency-stop flag unreadable "
                      f"({error_type}); fail closed")
    if bool(flag.get("stopped")):
        return True, str(flag.get("reason") or "no reason recorded")
    return False, "clear"
