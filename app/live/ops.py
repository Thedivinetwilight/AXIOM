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

from datetime import datetime, timezone

from app.live.alerts import AlertRouter
from app.live.guards import CanaryGuard, GuardDecision, TokenGuard
from app.live.watchdog import BreachMonitor, LiveWatchdog


class LiveOperations:
    def __init__(self, repository, *, broker=None, alerts: AlertRouter | None = None,
                 token_guard: TokenGuard | None = None,
                 canary: CanaryGuard | None = None,
                 watchdog: LiveWatchdog | None = None,
                 breaches: BreachMonitor | None = None,
                 now_fn=None):
        self.repository = repository
        self.broker = broker
        self.alerts = alerts
        self.token_guard = token_guard
        self.canary = canary
        self.watchdog = watchdog
        self.breaches = breaches
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
        watchdog = LiveWatchdog(
            repository, broker=broker, token_guard=token_guard,
            stuck_order_minutes=getattr(settings, "live_stuck_order_minutes", 5),
            reconcile_silence_minutes=getattr(
                settings, "live_reconcile_silence_minutes", 10),
            now_fn=now_fn)
        breaches = BreachMonitor(
            repository,
            max_daily_loss=getattr(settings, "max_daily_loss", 0.0),
            max_equity_drawdown=getattr(settings, "live_max_equity_drawdown", 0.0),
            auto_stop=getattr(settings, "live_auto_stop_on_breach", True))
        ops = cls(repository, broker=broker, alerts=alerts, token_guard=token_guard,
                  canary=canary, watchdog=watchdog, breaches=breaches, now_fn=now_fn)
        # the watchdog needs the journal, which the engine owns; the engine
        # attaches it after construction (see SandboxEngine.__init__).
        return ops

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
            "alerts": self.alerts.status() if self.alerts else None,
        }
