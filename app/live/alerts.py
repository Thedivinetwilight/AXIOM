"""Alert routing for operational events (V2.1 L-ops).

One durable sink, one optional outbound sink:

* ``system_events`` (always, kind ``ALERT``) — survives restarts, is readable by
  the API/dashboard/reports and needs no network.
* an optional HTTP webhook (``ALERT_WEBHOOK_URL``) for pages/ops tooling.

Design rules that make this safe to call from the trading loop:

- ``emit()`` NEVER raises. A failed alert must not become a failed order path,
  so every sink failure is captured and degraded to a recorded event.
- Alerts are deduplicated: the same ``kind`` within ``alert_dedup_seconds`` is
  counted, not re-sent. A tight loop cannot flood a webhook.
- Payloads are scrubbed of anything secret-shaped before they leave the process
  (tokens are never passed in, but this is belt-and-braces).
- The webhook is only ever called for alerts at/above ``min_severity``.

Nothing here imports httpx at module level: the core research/replay paths must
keep working with the standard library alone.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("bot.alerts")

SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}

#: substrings that mark a key as secret-bearing; such keys are dropped from
#: outbound payloads (defence in depth — callers should never pass them).
_SECRET_MARKERS = ("token", "secret", "password", "authorization", "api_key",
                   "apikey", "passwd")


def _scrub(payload: dict) -> dict:
    """Drop secret-shaped keys, recursively. Never raises."""
    if not isinstance(payload, dict):
        return {"value": str(payload)}
    clean: dict = {}
    for key, value in payload.items():
        if any(marker in str(key).lower() for marker in _SECRET_MARKERS):
            clean[key] = "[redacted]"
            continue
        if isinstance(value, dict):
            clean[key] = _scrub(value)
        elif isinstance(value, (list, tuple)):
            clean[key] = [_scrub(v) if isinstance(v, dict) else v for v in value]
        else:
            clean[key] = value
    return clean


@dataclass(frozen=True)
class Alert:
    kind: str
    severity: str
    message: str
    payload: dict = field(default_factory=dict)
    ts: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AlertRouter:
    """Dedup + severity filtering + delivery, with a durable fallback.

    ``transport`` accepts an httpx-compatible client (used by tests); when None
    a client is created lazily per delivery from httpx if importable.
    """

    EVENT_KIND = "ALERT"
    DEDUP_KEY = "alert_dedup"

    def __init__(self, repository, *, webhook_url: str = "",
                 min_severity: str = "WARNING", dedup_seconds: int = 300,
                 title: str = "Arena", transport=None, now_fn=None):
        self.repository = repository
        self.webhook_url = (webhook_url or "").strip()
        self.min_severity = (min_severity or "WARNING").upper()
        self.dedup_seconds = int(dedup_seconds)
        self.title = title
        self._transport = transport
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._seen: dict[str, datetime] = {}
        # counters are informative only; they are what the dashboard shows
        self.emitted = 0
        self.suppressed = 0
        self.delivery_failures = 0

    # -- severity ------------------------------------------------------------

    def _passes(self, severity: str) -> bool:
        return SEVERITY_ORDER.get(severity, 0) >= \
            SEVERITY_ORDER.get(self.min_severity, 1)

    def _deduped(self, kind: str, now: datetime) -> bool:
        """True when this kind was already emitted inside the window."""
        if self.dedup_seconds <= 0:
            return False
        last = self._seen.get(kind)
        if last is None:
            return False
        return (now - last) < timedelta(seconds=self.dedup_seconds)

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=max(self.dedup_seconds, 1))
        for kind in [k for k, ts in self._seen.items() if ts < cutoff]:
            self._seen.pop(kind, None)

    # -- emission ------------------------------------------------------------

    def emit(self, kind: str, severity: str, message: str, **payload) -> Alert | None:
        """Record (and optionally deliver) one alert. Never raises.

        Returns the Alert when it was emitted, or None when it was deduped.
        """
        severity = (severity or "INFO").upper()
        now = self._now()
        try:
            self._prune(now)
            if self._deduped(kind, now):
                self.suppressed += 1
                return None
            alert = Alert(kind=kind, severity=severity, message=str(message),
                          payload=_scrub(payload), ts=now.isoformat())
            self._seen[kind] = now
            self.emitted += 1
            self._record(alert)
            if self._passes(severity):
                self._deliver(alert)
            # NB: `message` is reserved on LogRecord — never pass it via extra.
            logger.log(
                logging.ERROR if severity == "CRITICAL" else logging.WARNING,
                "ALERT_%s" % severity,
                extra={"alert_kind": kind, "alert_message": alert.message})
            return alert
        except Exception as exc:  # an alert must never break the caller
            try:
                if self.repository is not None:
                    self.repository.record_system_event(
                        "ALERT_ROUTER_FAILURE", {"error": type(exc).__name__,
                                                 "alert_kind": str(kind)[:80]})
            except Exception as nested:
                # The fallback sink failed too — say so out loud rather than
                # pretending the alert was handled.
                logger.error("ALERT_ROUTER_FALLBACK_FAILURE",
                             extra={"error": type(nested).__name__,
                                    "original_error": type(exc).__name__})
            logger.error("ALERT_ROUTER_FAILURE", extra={"error": type(exc).__name__})
            return None

    def _record(self, alert: Alert) -> None:
        if self.repository is None:
            return
        self.repository.record_system_event(self.EVENT_KIND, {
            "kind": alert.kind, "severity": alert.severity,
            "message": alert.message, "details": alert.payload,
            "title": self.title,
        })

    def _deliver(self, alert: Alert) -> None:
        if not self.webhook_url:
            return
        try:
            import httpx

            body = {"title": f"[{self.title}] {alert.severity}",
                    "kind": alert.kind, "severity": alert.severity,
                    "message": alert.message, "payload": alert.payload,
                    "ts": alert.ts}
            client = self._transport
            if client is None:
                with httpx.Client(timeout=5.0) as fresh:
                    response = fresh.post(self.webhook_url, json=body)
            else:
                response = client.post(self.webhook_url, json=body)
            if getattr(response, "status_code", 0) >= 400:
                self._delivery_failure(alert, f"HTTP {response.status_code}")
        except Exception as exc:
            self._delivery_failure(alert, type(exc).__name__)

    def _delivery_failure(self, alert: Alert, reason: str) -> None:
        self.delivery_failures += 1
        logger.warning("ALERT_DELIVERY_FAILURE",
                       extra={"alert_kind": alert.kind, "reason": reason})
        if self.repository is not None:
            try:
                self.repository.record_system_event(
                    "ALERT_DELIVERY_FAILURE",
                    {"alert_kind": alert.kind, "severity": alert.severity,
                     "reason": reason})
            except Exception as nested:
                logger.error("ALERT_DELIVERY_RECORD_FAILURE",
                             extra={"error": type(nested).__name__,
                                    "alert_kind": alert.kind})

    # -- read side -----------------------------------------------------------

    def status(self) -> dict:
        return {"emitted": self.emitted, "suppressed": self.suppressed,
                "delivery_failures": self.delivery_failures,
                "webhook_enabled": bool(self.webhook_url),
                "min_severity": self.min_severity,
                "dedup_seconds": self.dedup_seconds}


def recent_alerts(repository, limit: int = 50) -> list[dict]:
    """Alerts from the durable sink, newest first (dashboard/report helper)."""
    if repository is None:
        return []
    try:
        events = repository.list_system_events(limit=max(limit * 4, 200))
    except Exception:
        return []
    alerts = []
    for event in events:
        if event.get("kind") != AlertRouter.EVENT_KIND:
            continue
        payload = event.get("payload") or {}
        alerts.append({"ts": event.get("ts"), "kind": payload.get("kind", "?"),
                       "severity": payload.get("severity", "INFO"),
                       "message": payload.get("message", "")})
        if len(alerts) >= limit:
            break
    return alerts
