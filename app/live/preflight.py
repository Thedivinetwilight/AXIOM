"""Live readiness checks (V2.1 L-ops) — offline, deterministic, no orders.

``run_checks`` performs every verification that needs no network; the caller may
then add a credential probe. Each check returns a :class:`Check` with an
explicit pass/fail and a human-readable detail, so the CLI can print a
structured report and the API can expose the same information.

Severity semantics:

* ``ERROR`` — live trading must not start.
* ``WARNING`` — live trading may start, but the operator is accepting a known
  operational gap (no canary, no drawdown cap, no alert sink, ...).
* ``INFO`` — context, including regulatory reminders.

Nothing here contacts a network. Nothing here places an order.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from app.config.hosts import LIVE_ORDER_HOSTS, host_of
from app.config.settings import LIVE_CONFIRM_PHRASE

ERROR = "ERROR"
WARNING = "WARNING"
INFO = "INFO"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    severity: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def _check(name: str, ok: bool, severity: str, detail: str) -> Check:
    return Check(name=name, ok=bool(ok), severity=severity, detail=detail)


def run_checks(settings, *, repository=None) -> list[Check]:
    """All offline live-readiness checks, in a stable order."""
    checks: list[Check] = []
    broker = (getattr(settings, "broker", "") or "").upper()
    mode = (getattr(settings, "mode", "") or "").upper()

    # 1. the four switches
    switches = {
        "MODE=LIVE": mode == "LIVE",
        "LIVE_TRADING=true": bool(getattr(settings, "live_trading", False)),
        "BROKER=UPSTOX_LIVE": broker == "UPSTOX_LIVE",
        "LIVE_CONFIRM": str(getattr(settings, "live_confirm", "")).strip()
        == LIVE_CONFIRM_PHRASE,
    }
    missing = [name for name, ok in switches.items() if not ok]
    checks.append(_check(
        "four_switch_opt_in", not missing, ERROR,
        "all four switches present" if not missing
        else f"missing: {', '.join(missing)}"))

    # 2. order host
    host = host_of(getattr(settings, "upstox_live_base_url", ""))
    checks.append(_check(
        "order_host_is_live", host in LIVE_ORDER_HOSTS, ERROR,
        f"order host {host or '(unset)'}; documented live host(s): "
        f"{', '.join(sorted(LIVE_ORDER_HOSTS))}"))

    # 3. credential present
    token = str(getattr(settings, "upstox_live_access_token", "") or "").strip()
    checks.append(_check(
        "credential_present", bool(token), ERROR,
        "UPSTOX_LIVE_ACCESS_TOKEN is set" if token
        else "UPSTOX_LIVE_ACCESS_TOKEN is empty"))

    # 4. hard risk limits
    limits = {
        "MAX_POSITION_VALUE": float(getattr(settings, "max_position_value", 0) or 0),
        "MAX_OPEN_POSITIONS": int(getattr(settings, "max_open_positions", 0) or 0),
        "MAX_DAILY_LOSS": float(getattr(settings, "max_daily_loss", 0) or 0),
        "MAX_TRADES_PER_DAY": int(getattr(settings, "max_trades_per_day", 0) or 0),
    }
    unbounded = [name for name, value in limits.items() if value <= 0]
    checks.append(_check(
        "hard_risk_limits_bounded", not unbounded, ERROR,
        ("all four limits are non-zero" if not unbounded
         else f"unbounded (0 = unlimited, refused in live): {', '.join(unbounded)}")))

    # 5. canary ramp
    canary_notional = float(getattr(settings, "live_canary_max_notional", 0) or 0)
    canary_orders = int(getattr(settings, "live_canary_orders", 0) or 0)
    checks.append(_check(
        "canary_ramp", canary_notional > 0 and canary_orders > 0, WARNING,
        (f"first {canary_orders} entries capped at {canary_notional:,.2f}"
         if canary_notional > 0 and canary_orders > 0
         else "no canary ramp: the first live entries use full size")))

    # 6. equity drawdown kill switch
    drawdown = float(getattr(settings, "live_max_equity_drawdown", 0) or 0)
    checks.append(_check(
        "equity_drawdown_cap", drawdown > 0, WARNING,
        (f"auto-stop at {drawdown:.2%} peak-equity drawdown" if drawdown > 0
         else "no peak-equity drawdown cap; only the daily-loss limit applies")))

    # 7. auto-stop
    auto_stop = bool(getattr(settings, "live_auto_stop_on_breach", True))
    checks.append(_check(
        "auto_stop_on_breach", auto_stop, WARNING,
        "a breach triggers the persisted EMERGENCY STOP"
        if auto_stop else "a breach only blocks entries; no automatic stop"))

    # 8. alert delivery
    webhook = str(getattr(settings, "alert_webhook_url", "") or "")
    checks.append(_check(
        "alert_delivery", bool(webhook), WARNING,
        f"webhook configured (min severity {getattr(settings, 'alert_min_severity', '?')})"
        if webhook else
        "no ALERT_WEBHOOK_URL: alerts are only visible in system_events/API"))

    # 9. market data wiring used by the engine
    data_source = str(getattr(settings, "data_source", "") or "")
    checks.append(_check(
        "data_source_upstox", data_source == "upstox", WARNING,
        "DATA_SOURCE=upstox" if data_source == "upstox"
        else f"DATA_SOURCE={data_source or '(unset)'}; 'python -m app engine' "
             "refuses to run without upstox"))

    # 10. session calendar
    calendar = str(getattr(settings, "market_calendar", "") or "")
    checks.append(_check(
        "indian_session_calendar", calendar == "nse-session", WARNING,
        "MARKET_CALENDAR=nse-session" if calendar == "nse-session"
        else f"MARKET_CALENDAR={calendar or '(unset)'} — Indian equity orders "
             "should use nse-session"))

    # 11. token freshness recorded (what the TokenGuard will decide)
    token_state = {}
    if repository is not None:
        try:
            token_state = repository.get_engine_state("live_token_verified", {}) or {}
        except Exception:
            token_state = {}
    verified_at = str(token_state.get("verified_at", "") or "")
    max_age = float(getattr(settings, "live_token_max_age_hours", 20.0) or 0)
    if max_age <= 0:
        checks.append(_check("token_freshness", True, INFO,
                             "token age check disabled (LIVE_TOKEN_MAX_AGE_HOURS=0)"))
    else:
        checks.append(_check(
            "token_freshness", bool(verified_at), WARNING,
            (f"last verified {verified_at}" if verified_at
             else "no verification recorded: the TokenGuard will REFUSE new "
                  "entries until 'live-preflight' records one")))

    # 12. dedicated database for live
    database_url = str(getattr(settings, "database_url", "") or "")
    dedicated = "live" in database_url.lower()
    checks.append(_check(
        "dedicated_live_database", dedicated, WARNING,
        f"DATABASE_URL looks live-specific ({database_url})" if dedicated
        else "DATABASE_URL is not live-specific; live and sandbox engine state "
             "would share one file (audit rows stay separate either way)"))

    # 13. regulatory reminder (never a pass/fail on our side)
    checks.append(_check(
        "static_ip_registered", True, INFO,
        "Order APIs must originate from a static IP registered with Upstox "
        "(SEBI/NSE requirement in force since 2025-10-01); low-frequency algos "
        "do not need exchange registration, but the IP still must be registered."))

    return checks


def summarize(checks: list[Check]) -> dict:
    """Roll checks up into a machine-readable verdict."""
    errors = [c for c in checks if not c.ok and c.severity == ERROR]
    warnings = [c for c in checks if not c.ok and c.severity == WARNING]
    return {
        "ok": not errors,
        "errors": [c.to_dict() for c in errors],
        "warnings": [c.to_dict() for c in warnings],
        "checks": [c.to_dict() for c in checks],
    }
