"""Operational digest: what actually happened, in one readable artifact.

A live session produces three kinds of evidence — alerts, guard actions and
fills — and they arrive continuously. Evaluating them one page at a time is how
a slow degradation goes unnoticed. This module folds a time window into a single
report an operator can read in a minute:

* alerts by kind and severity, and how many CRITICALs are still unacknowledged;
* guard activity (entries blocked/reduced) — the rails earning their keep;
* stops and resumes (who halted what, and why);
* orders/fills in the window, open orders now, unresolved intents;
* realized PnL **pre-fee**, recomputed from the audit trail (never the ledger's
  own number), plus the audit-integrity verdict.

Nothing here trades, cancels or changes a limit. Posting the digest is optional
and goes through the same tested webhook path as the alerts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.live.alerts import AlertAcknowledgements, AlertRouter, recent_alerts
from app.live.pnl import integrity_report, replay_fills

#: system_events kinds that represent a rail doing its job
GUARD_KINDS = ("ENTRY_GUARD_BLOCKED", "ENTRY_GUARD_REDUCED")
STOP_KINDS = ("EMERGENCY_STOP", "AUTO_STOP")


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _in_window(ts, start: datetime, end: datetime) -> bool:
    """True when ``ts`` falls inside [start, end]. Unparseable timestamps are
    kept (a record we cannot date is more likely to matter than one we can)."""
    parsed = _parse(ts)
    if parsed is None:
        return True
    return start <= parsed <= end


def _window(events: list[dict], start: datetime, end: datetime) -> list[dict]:
    return [event for event in events
            if _in_window(event.get("ts"), start, end)]


def build_digest(repository, *, hours: float = 24.0, live: bool = True,
                 now: datetime | None = None, event_limit: int = 2000) -> dict:
    """Fold the last ``hours`` of recorded state into one report."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max(0.0, float(hours)))
    digest: dict = {
        "scope": "LIVE OPS DIGEST",
        "window": {"since": cutoff.isoformat(), "until": now.isoformat(),
                   "hours": float(hours)},
        "mode": "live" if live else "sandbox",
        "note": "read-only roll-up of recorded state; PnL is pre-fee and comes "
                "from the audit trail, not from the engine's ledger",
    }
    if repository is None:
        digest["error"] = "no database"
        return digest

    events = _window(repository.list_system_events(limit=event_limit), cutoff, now)

    # -- alerts --------------------------------------------------------------
    acks = AlertAcknowledgements(repository)
    alerts = [alert for alert in recent_alerts(repository, limit=event_limit,
                                              acknowledgements=acks)
              if _in_window(alert.get("ts"), cutoff, now)]
    by_kind: dict = {}
    by_severity: dict = {}
    for alert in alerts:
        by_kind[alert["kind"]] = by_kind.get(alert["kind"], 0) + 1
        by_severity[alert["severity"]] = by_severity.get(alert["severity"], 0) + 1
    digest["alerts"] = {
        "total": len(alerts),
        "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])[:10]),
        "by_severity": by_severity,
        "unacknowledged_critical": sum(
            1 for alert in alerts
            if alert["severity"] == "CRITICAL" and not alert["acknowledged"]),
        "latest_critical": next(
            (alert["message"] for alert in alerts
             if alert["severity"] == "CRITICAL"), None),
    }

    # -- rails in action -----------------------------------------------------
    guard_events = [event for event in events if event["kind"] in GUARD_KINDS]
    digest["guard_actions"] = {
        "blocked": sum(1 for e in guard_events if e["kind"] == "ENTRY_GUARD_BLOCKED"),
        "reduced": sum(1 for e in guard_events if e["kind"] == "ENTRY_GUARD_REDUCED"),
    }
    stops = [event for event in events
             if event["kind"] in STOP_KINDS
             or str(event.get("payload", {}).get("reason", "")).startswith("AUTO_STOP")]
    digest["stops"] = {
        "count": len(stops),
        "reasons": [str(e.get("payload", {}).get("reason", ""))[:160]
                    for e in stops[:10]],
        "resumed": sum(1 for event in events if event["kind"] == "ENGINE_RESUME"),
    }
    digest["acks"] = acks.status()

    # -- orders and fills ----------------------------------------------------
    orders = (repository.list_live_orders(limit=2000) if live
              else repository.list_sandbox_orders(limit=2000))
    recent_orders = [order for order in orders
                     if _in_window(order.get("created_ts"), cutoff, now)]
    status_counts: dict = {}
    for order in recent_orders:
        status = str(order.get("status", "?")).upper()
        status_counts[status] = status_counts.get(status, 0) + 1
    digest["orders"] = {
        "placed_in_window": len(recent_orders),
        "by_status": status_counts,
        "open_now": len(repository.open_live_orders() if live
                        else repository.open_sandbox_orders()),
    }

    # -- money, from the audit trail only ------------------------------------
    replay = replay_fills(repository, live=live)
    digest["pnl"] = {
        "source": "audit trail (live_transitions + live_orders)" if live
                  else "audit trail (sandbox_transitions + sandbox_orders)",
        "realized_pre_fee": round(replay.realized_pnl, 2),
        "turnover": round(replay.turnover, 2),
        "fills_total": len(replay.fills),
        "buys": replay.buys, "sells": replay.sells,
        "open_positions": replay.open_positions,
        "last_fill_ts": replay.last_ts,
    }
    digest["audit_integrity"] = integrity_report(repository, live=live)

    # -- unresolved work -----------------------------------------------------
    try:
        from app.engine.state import SubmissionJournal

        unresolved = SubmissionJournal(repository).unresolved()
    except Exception:
        unresolved = {}
    digest["unresolved_intents"] = {"count": len(unresolved),
                                    "ids": list(unresolved)[:10]}

    # -- the numbers an operator scans first ---------------------------------
    digest["headline"] = {
        "critical_unacknowledged": digest["alerts"]["unacknowledged_critical"],
        "stops": digest["stops"]["count"],
        "blocked_entries": digest["guard_actions"]["blocked"],
        "open_orders": digest["orders"]["open_now"],
        "unresolved_intents": digest["unresolved_intents"]["count"],
        "realized_pre_fee": digest["pnl"]["realized_pre_fee"],
        "audit_ok": bool(digest["audit_integrity"].get("ok")),
    }
    return digest


def digest_markdown(digest: dict) -> str:
    """Render a digest for a human (chat message, commit comment, email)."""
    window = digest.get("window", {})
    headline = digest.get("headline", {})
    alerts = digest.get("alerts", {})
    lines = [
        f"# {digest.get('scope', 'LIVE OPS DIGEST')} ({digest.get('mode', '?')})",
        f"_Window: {window.get('since', '?')} → {window.get('until', '?')} "
        f"({window.get('hours', '?')} h)_",
        "",
        "## Headline",
        f"- Unacknowledged CRITICAL alerts: **{headline.get('critical_unacknowledged', 0)}**",
        f"- Stops in window: **{headline.get('stops', 0)}**",
        f"- Entries blocked by a rail: **{headline.get('blocked_entries', 0)}**",
        f"- Open orders now: **{headline.get('open_orders', 0)}** · "
        f"unresolved intents: **{headline.get('unresolved_intents', 0)}**",
        f"- Realized PnL (pre-fee, audit trail): **{headline.get('realized_pre_fee', 0)}**",
        f"- Audit trail consistent: **{headline.get('audit_ok', False)}**",
        "",
        "## Alerts",
        f"- Total: {alerts.get('total', 0)} · by severity: "
        f"{alerts.get('by_severity', {}) or '{}'}",
        f"- By kind: {alerts.get('by_kind', {}) or '{}'}",
    ]
    if alerts.get("latest_critical"):
        lines.append(f"- Latest CRITICAL: {alerts['latest_critical']}")
    stops = digest.get("stops", {})
    lines += ["", "## Stops", f"- Count: {stops.get('count', 0)}"]
    for reason in stops.get("reasons", []):
        lines.append(f"  - {reason}")
    orders = digest.get("orders", {})
    lines += ["", "## Orders", f"- Placed in window: {orders.get('placed_in_window', 0)}",
              f"- By status: {orders.get('by_status', {}) or '{}'}"]
    pnl = digest.get("pnl", {})
    lines += ["", "## PnL (pre-fee, audit trail)",
              f"- Realized: {pnl.get('realized_pre_fee', 0)} · "
              f"turnover: {pnl.get('turnover', 0)}",
              f"- Fills: {pnl.get('fills_total', 0)} "
              f"(buys {pnl.get('buys', 0)} / sells {pnl.get('sells', 0)})",
              f"- Open positions: {pnl.get('open_positions', {}) or '{}'}",
              f"- Integrity: {digest.get('audit_integrity', {}).get('ok')} "
              f"(problems: {digest.get('audit_integrity', {}).get('problems', 0)})"]
    lines += ["", f"_{digest.get('note', '')}_"]
    return "\n".join(lines)


def post_digest(router: AlertRouter, digest: dict) -> dict:
    """POST the digest through the alert webhook. Never raises.

    Reports honestly whether anything was attempted: with no
    ``ALERT_WEBHOOK_URL`` there is no sink to post to.
    """
    if router is None or not getattr(router, "webhook_url", ""):
        return {"posted": False,
                "reason": "no ALERT_WEBHOOK_URL configured; nothing was sent"}
    posted = router.post_json({
        "title": "[Arena] ops digest",
        "kind": "OPS_DIGEST", "severity": "INFO",
        "message": (f"{digest.get('headline', {}).get('blocked_entries', 0)} blocked, "
                    f"{digest.get('headline', {}).get('stops', 0)} stops, "
                    f"{digest.get('headline', {}).get('realized_pre_fee', 0)} realized "
                    f"(pre-fee)"),
        "payload": digest,
        "ts": digest.get("window", {}).get("until"),
    })
    return {"posted": posted,
            "reason": "" if posted else "the webhook POST failed"}
