"""Live watchdog + breach monitor (V2.1 L-ops).

Two independent responsibilities:

* :class:`LiveWatchdog` — *observes* and reports. It inspects the broker,
  the order audit tables and the intent journal for states that silently stop a
  24/7 engine from working: working orders that never reconcile, a broker
  disabled by an auth failure, reconciliation going quiet, a credential nearing
  expiry. Findings become alerts via the AlertRouter.
* :class:`BreachMonitor` — *decides*. Hard risk limits already block individual
  entries; this monitor is the escalation layer that catches the account-level
  cases (daily realized loss beyond the limit, peak-equity drawdown beyond the
  configured fraction) and asks the engine to trigger the persisted EMERGENCY
  STOP.

Neither class places, cancels or modifies an order. The engine owns the
emergency stop; the watchdog only reports the need for one. That separation is
deliberate: an observer that can also act is much harder to reason about.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

FINAL_STATUSES = {"FILLED", "CANCELLED", "REJECTED"}
WORKING_STATUSES = {"SUBMITTED", "OPEN", "PARTIALLY_FILLED", "CANCEL_UNKNOWN"}

CRITICAL = "CRITICAL"
WARNING = "WARNING"
INFO = "INFO"


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str
    message: str
    payload: dict

    def to_dict(self) -> dict:
        return {"kind": self.kind, "severity": self.severity,
                "message": self.message, "payload": self.payload}


class LiveWatchdog:
    """Observation-only checks over durable state. Never raises to the caller."""

    def __init__(self, repository, *, broker=None, token_guard=None,
                 stuck_order_minutes: int = 5,
                 stuck_order_escalate_minutes: int = 15,
                 reconcile_silence_minutes: int = 10, now_fn=None):
        self.repository = repository
        self.broker = broker
        self.token_guard = token_guard
        self.stuck_order_minutes = int(stuck_order_minutes)
        self.stuck_order_escalate_minutes = max(
            int(stuck_order_escalate_minutes), int(stuck_order_minutes))
        self.reconcile_silence_minutes = int(reconcile_silence_minutes)
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    # -- individual checks ---------------------------------------------------

    def _open_orders(self) -> list[dict]:
        """Working orders from the mode-appropriate audit table."""
        if self.repository is None:
            return []
        live = bool(getattr(self.broker, "LIVE", False))
        try:
            listed = (self.repository.list_live_orders(limit=500) if live
                      else self.repository.list_sandbox_orders(limit=500))
        except Exception:
            return []
        return [order for order in listed
                if str(order.get("status", "")).upper() in WORKING_STATUSES]

    def check_stuck_orders(self, *, now: datetime | None = None) -> list[Finding]:
        """Working orders older than the stuck threshold, on an urgency ladder.

        A working order just past the threshold is worth telling someone about;
        one that is still unresolved after ``stuck_order_escalate_minutes`` is a
        genuine operational problem (and the engine may act on CRITICAL findings
        when auto-cancel is enabled — see ``LIVE_AUTO_CANCEL_STUCK``).
        """
        now = now or self._now()
        findings: list[Finding] = []
        for order in self._open_orders():
            created = _parse(order.get("created_ts") or order.get("updated_ts"))
            if created is None:
                continue
            age_min = (now - created).total_seconds() / 60.0
            if age_min >= self.stuck_order_minutes:
                escalated = age_min >= self.stuck_order_escalate_minutes
                findings.append(Finding(
                    "STUCK_ORDER", CRITICAL if escalated else WARNING,
                    f"order {order.get('id')} ({order.get('symbol')} "
                    f"{order.get('side')}) has been working for "
                    f"{age_min:.1f} min without reaching a final state"
                    + (" and has ESCALATED" if escalated else ""),
                    {"order_id": order.get("id"), "status": order.get("status"),
                     "age_minutes": round(age_min, 1),
                     "escalated": escalated,
                     "escalate_minutes": self.stuck_order_escalate_minutes,
                     "intent_id": order.get("intent_id"),
                     "symbol": order.get("symbol"), "side": order.get("side"),
                     "quantity": order.get("quantity")}))
        return findings

    def check_broker_available(self, *, now=None) -> list[Finding]:
        """A broker that failed closed must be surfaced loudly."""
        broker = self.broker
        if broker is None or not hasattr(broker, "available"):
            return []
        if getattr(broker, "available"):
            return []
        return [Finding(
            "BROKER_DISABLED", CRITICAL,
            "the order broker is disabled (credential rejected or fail-closed "
            "state); no new orders can be placed",
            {"mode": getattr(broker, "mode", "?"),
             "base_url": getattr(broker, "_base_url", "")})]

    def check_unresolved_intents(self, *, now: datetime | None = None) -> list[Finding]:
        """Intents stuck in UNKNOWN/UNRESOLVED need a human."""
        journal = getattr(self, "intent_journal", None)
        if journal is None or self.repository is None:
            return []
        findings: list[Finding] = []
        try:
            unresolved = journal.unresolved()
        except Exception:
            return []
        now = now or self._now()
        for entry in unresolved:
            intent_id = entry.get("intent_id") if isinstance(entry, dict) else str(entry)
            state = (entry or {}).get("state", "UNKNOWN") if isinstance(entry, dict) else "UNKNOWN"
            ts = _parse((entry or {}).get("ts")) if isinstance(entry, dict) else None
            age_min = ((now - ts).total_seconds() / 60.0) if ts else None
            findings.append(Finding(
                "UNRESOLVED_INTENT", CRITICAL,
                f"intent {str(intent_id)[:12]} is {state}"
                + (f" for {age_min:.0f} min" if age_min is not None else "")
                + " — the remote outcome is unknown; reconcile manually",
                {"intent_id": intent_id, "state": state,
                 "age_minutes": None if age_min is None else round(age_min, 1)}))
        return findings

    def check_reconcile_liveness(self, *, now: datetime | None = None) -> list[Finding]:
        """Reconciliation going quiet is the classic silent 24/7 failure."""
        if self.repository is None:
            return []
        now = now or self._now()
        last = self.repository.get_engine_state("last_reconcile_at")
        stamp = _parse(last.get("ts") if isinstance(last, dict) else last)
        if stamp is None:
            return []
        age_min = (now - stamp).total_seconds() / 60.0
        if age_min >= self.reconcile_silence_minutes:
            return [Finding(
                "RECONCILE_STALLED", CRITICAL,
                f"no successful reconciliation for {age_min:.1f} min "
                f"(threshold {self.reconcile_silence_minutes} min)",
                {"age_minutes": round(age_min, 1)})]
        return []

    def check_token(self, *, now: datetime | None = None) -> list[Finding]:
        if self.token_guard is None:
            return []
        now = now or self._now()
        decision = self.token_guard.check(now=now)
        findings: list[Finding] = []
        if not decision.allowed:
            findings.append(Finding("TOKEN_BLOCK", CRITICAL,
                                    f"new entries are blocked: {decision.reason}",
                                    decision.to_dict()))
        elif self.token_guard.warn_due(now=now):
            remaining = self.token_guard.expires_in_minutes(now=now)
            findings.append(Finding(
                "TOKEN_EXPIRING", WARNING,
                f"credential verification expires in {remaining:.0f} min; "
                "refresh the token and re-run live-preflight",
                {"expires_in_minutes": round(remaining, 1)}))
        return findings

    # -- aggregate -----------------------------------------------------------

    def inspect(self, *, now: datetime | None = None) -> list[Finding]:
        """Run every check. Failures inside one check never hide the others."""
        now = now or self._now()
        findings: list[Finding] = []
        for check in (self.check_broker_available,
                      self.check_stuck_orders,
                      self.check_unresolved_intents,
                      self.check_reconcile_liveness,
                      self.check_token):
            try:
                findings.extend(check(now=now))
            except Exception:
                continue
        return findings


@dataclass(frozen=True)
class Breach:
    kind: str
    severity: str
    message: str
    payload: dict
    stop: bool

    def to_dict(self) -> dict:
        return {"kind": self.kind, "severity": self.severity,
                "message": self.message, "payload": self.payload,
                "stop": self.stop}


class BreachMonitor:
    """Account-level escalations that individual order gates cannot catch.

    ``max_daily_loss`` mirrors the hard limit's semantics (a positive amount);
    ``max_equity_drawdown`` is a fraction of peak equity (0.10 = 10%).
    """

    def __init__(self, repository, *, max_daily_loss: float = 0.0,
                 max_equity_drawdown: float = 0.0,
                 auto_stop: bool = True):
        self.repository = repository
        self.max_daily_loss = float(max_daily_loss)
        self.max_equity_drawdown = float(max_equity_drawdown)
        self.auto_stop = bool(auto_stop)

    def evaluate(self, *, realized_today: float, equity: float | None = None,
                 peak_equity: float | None = None) -> Breach | None:
        """Return the FIRST breached condition (most severe configured first)."""
        loss = -float(realized_today or 0.0)  # positive = a loss
        if self.max_daily_loss > 0 and loss >= self.max_daily_loss:
            return Breach(
                "DAILY_LOSS_BREACH", CRITICAL,
                f"realized loss today {loss:,.2f} reached the "
                f"{self.max_daily_loss:,.2f} hard daily limit",
                {"realized_today": round(float(realized_today), 6),
                 "max_daily_loss": self.max_daily_loss},
                stop=self.auto_stop)
        if (self.max_equity_drawdown > 0 and equity is not None
                and peak_equity and peak_equity > 0):
            drawdown = (peak_equity - equity) / peak_equity
            if drawdown >= self.max_equity_drawdown:
                return Breach(
                    "EQUITY_DRAWDOWN_BREACH", CRITICAL,
                    f"equity drawdown {drawdown:.2%} reached the configured "
                    f"{self.max_equity_drawdown:.2%} limit",
                    {"equity": equity, "peak_equity": peak_equity,
                     "drawdown": round(drawdown, 6),
                     "max_equity_drawdown": self.max_equity_drawdown},
                    stop=self.auto_stop)
        return None

    def status(self) -> dict:
        return {"max_daily_loss": self.max_daily_loss,
                "max_equity_drawdown": self.max_equity_drawdown,
                "auto_stop": self.auto_stop}
