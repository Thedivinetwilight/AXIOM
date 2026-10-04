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


def _unavailable(name: str, why: str) -> Check:
    """A recorded-state check that could not run is NOT a pass.

    It is reported as ``ok=False`` with severity ``INFO``: it never fails the
    verdict on its own, but it can never be mistaken for "verified clean".
    """
    return Check(name=name, ok=False, severity=INFO,
                 detail=f"not checked: {why}")


def run_checks(settings, *, repository=None) -> list[Check]:
    """All offline live-readiness checks, in a stable order.

    Two families: configuration checks (does this process hold a valid, bounded
    live opt-in?) and recorded-state checks (is the database telling us the last
    session shut down cleanly?). Every check that cannot run is reported as a
    failure, never as a silent pass.
    """
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

    # 4b. the timeframe the live engine would actually run
    from app.data.factory import ENGINE_TIMEFRAMES

    timeframe = str(getattr(settings, "default_timeframe", "") or "")
    checks.append(_check(
        "engine_timeframe_intraday", timeframe in ENGINE_TIMEFRAMES, ERROR,
        (f"DEFAULT_TIMEFRAME={timeframe} (the engine and the data stream share "
         "this value)" if timeframe in ENGINE_TIMEFRAMES else
         f"DEFAULT_TIMEFRAME={timeframe} cannot drive a live engine (intraday "
         f"only: {', '.join(ENGINE_TIMEFRAMES)}); the live engine refuses to "
         "start rather than silently downgrading")))

    # 4c. market-data mode vs the timeframe the engine will run
    from app.data.factory import market_data_mode_error

    mode_error = market_data_mode_error(settings)
    checks.append(_check(
        "market_data_mode_matches_timeframe", not mode_error, ERROR,
        mode_error or (f"MARKET_DATA_MODE={getattr(settings, 'market_data_mode', '?')} "
                       f"can carry DEFAULT_TIMEFRAME="
                       f"{getattr(settings, 'default_timeframe', '?')}")))

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

    # 13. audit-trail integrity for the table this mode will use
    live_table = broker == "UPSTOX_LIVE"
    integrity = {"ok": True, "problems": 0, "orders": 0, "fills": 0}
    if repository is not None:
        from app.live.pnl import integrity_report

        try:
            integrity = integrity_report(repository, live=live_table)
        except Exception as exc:  # a check that cannot run is reported as a failure, never as clean
            integrity = {"ok": False, "problems": 1, "orders": 0, "fills": 0,
                         "error": type(exc).__name__}
    scope = "live" if live_table else "sandbox"
    if repository is None:
        checks.append(_unavailable("audit_trail_integrity", "no database"))
    else:
        checks.append(_check(
            "audit_trail_integrity", bool(integrity.get("ok")), ERROR if live_table else WARNING,
        (f"{scope} audit trail is internally consistent "
         f"({integrity.get('orders', 0)} orders, {integrity.get('fills', 0)} fill rows)"
         if integrity.get("ok") else
         f"{scope} audit trail has {integrity.get('problems')} integrity problem(s) "
         f"(orphan fills {len(integrity.get('orphan_fills', []))}, missing fills "
         f"{len(integrity.get('missing_fills', []))}, quantity mismatches "
         f"{len(integrity.get('quantity_mismatch', []))}) — reconcile before trading")))

    # 14. the persisted emergency stop must be clear
    if repository is None:
        checks.append(_unavailable("emergency_stop_clear", "no database"))
    else:
        try:
            stopped = bool((repository.get_engine_state("emergency_stop") or {})
                           .get("stopped"))
        except Exception as exc:
            stopped, problem = False, type(exc).__name__
        else:
            problem = ""
        checks.append(_check(
            "emergency_stop_clear", not stopped and not problem, ERROR,
            "no persisted EMERGENCY STOP" if not stopped and not problem
            else (f"a persisted EMERGENCY STOP is set: the engine will refuse to "
                  "trade until 'python -m app live-resume' clears it" if stopped
                  else f"the emergency-stop state could not be read ({problem})")))

    # 15. working orders and unresolved intents left over from the last run
    open_orders = 0
    unresolved = 0
    if repository is not None:
        try:
            open_orders = len(repository.open_live_orders() if live_table
                              else repository.open_sandbox_orders())
        except Exception:
            open_orders = 0
        try:
            from app.engine.state import SubmissionJournal

            unresolved = len(SubmissionJournal(repository).unresolved())
        except Exception:
            unresolved = -1  # a check that cannot run must not report "clean"
    if repository is None:
        checks.append(_unavailable("no_open_orders_left", "no database"))
        checks.append(_unavailable("no_unresolved_intents", "no database"))
    else:
        checks.append(_check(
            "no_open_orders_left", open_orders == 0, WARNING,
            "no working orders recorded" if open_orders == 0
            else f"{open_orders} working order(s) recorded: the engine will "
                 "reconcile them at startup — never run two engines against one "
                 "account"))
        checks.append(_check(
            "no_unresolved_intents", unresolved == 0, ERROR,
            "no unresolved submission intents" if unresolved == 0
            else (f"{unresolved} unresolved intent(s): the remote outcome is "
                  "genuinely unknown; reconcile manually before trading"
                  if unresolved > 0 else "the intent journal could not be read")))

    # 16. reconciliation liveness (only meaningful once the engine has stamped)
    if repository is None:
        checks.append(_unavailable("reconciliation_live", "no database"))
    else:
        last = repository.get_engine_state("last_reconcile_at")
        stamp = None
        if isinstance(last, dict):
            stamp = last.get("ts")
        elif last:
            stamp = last
        if not stamp:
            checks.append(_check(
                "reconciliation_live", True, INFO,
                "no reconciliation recorded yet (fresh or unused database)"))
        else:
            from datetime import datetime, timezone

            try:
                parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                parsed = None
            silence_min = int(getattr(
                settings, "live_reconcile_silence_minutes", 10) or 10)
            age_min = ((datetime.now(timezone.utc) - parsed).total_seconds() / 60.0
                       if parsed else None)
            checks.append(_check(
                "reconciliation_live", age_min is not None and age_min < silence_min,
                WARNING,
                f"last reconciliation {age_min:.1f} min ago" if age_min is not None
                else "last reconciliation timestamp is unreadable"))

    # 17. regulatory reminder (never a pass/fail on our side)
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


# ---------------------------------------------------------------------------
# AXIOM-MONEY-PRINTER readiness center (master spec section 32): the base
# offline checks plus operational checks. A check that cannot run reports
# ok=False with INFO severity so it can never be read as "healthy" — it maps
# to UNKNOWN/NOT CHECKED in the readiness API.
# ---------------------------------------------------------------------------

_CLOCK_SANITY_MIN_YEAR = 2020
_CLOCK_SANITY_MAX_YEAR = 2035


def run_readiness_center_checks(settings, *, repository=None,
                                context: dict | None = None) -> list[Check]:
    """Base checks + operational readiness checks, in stable order."""
    checks = list(run_checks(settings, repository=repository))
    context = context or {}

    # Clock sanity (spec section 144): impossible local clocks make token
    # expiry math and reconciliation timestamps meaningless.
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    sane = _CLOCK_SANITY_MIN_YEAR <= now.year <= _CLOCK_SANITY_MAX_YEAR
    checks.append(_check(
        "clock_sanity", sane, ERROR,
        f"system clock {now.isoformat(timespec='seconds')} is within the sane window"
        if sane else f"system clock year {now.year} is implausible; token/expiry "
                     "math cannot be trusted"))

    # Kill-switch availability (spec section 37): the emergency-stop flag path
    # must be readable/writable before live starts.
    try:
        from app.live.ops import read_emergency_stop

        if repository is not None:
            active, reason = read_emergency_stop(repository)
            checks.append(_check(
                "kill_switch_available", not active, ERROR,
                "emergency-stop flag readable and clear" if not active
                else f"emergency stop is ACTIVE ({reason}); clear it deliberately "
                     "before any live session"))
        else:
            checks.append(_unavailable("kill_switch_available", "no repository attached"))
    except Exception as exc:  # pragma: no cover — defensive
        checks.append(_check(
            "kill_switch_available", False, ERROR,
            f"emergency-stop flag path could not be verified: {exc}"))

    # Single-engine lock (spec section 28): a second live engine must be
    # detectable before the first one starts. A STALE lock (dead engine) does
    # not block; a fresh live lock does.
    try:
        from app.live.lock import EngineLock

        if repository is not None:
            status = EngineLock(repository).status()
            blocking = bool(status.get("held")) and not status.get("stale") \
                and bool(status.get("live"))
            checks.append(_check(
                "single_instance_lock", not blocking, ERROR,
                "no other live engine holds a fresh lock" if not blocking
                else f"another live engine ({status.get('owner')}) holds the lock"))
        else:
            checks.append(_unavailable("single_instance_lock", "no repository attached"))
    except Exception as exc:  # pragma: no cover — defensive
        checks.append(_check("single_instance_lock", False, ERROR,
                             f"lock state could not be verified: {exc}"))

    # Static-IP compliance (spec section 39): we can only verify that the
    # operator has recorded registered IPs; actual source-IP matching happens
    # on the broker side and is reported UNKNOWN here, never healthy.
    primary = str(getattr(settings, "upstox_static_ip_primary", "") or "").strip()
    secondary = str(getattr(settings, "upstox_static_ip_secondary", "") or "").strip()
    if primary or secondary:
        checks.append(_check(
            "static_ip_configuration", True, INFO,
            f"registered static IP on file (primary={'set' if primary else 'unset'}, "
            f"secondary={'set' if secondary else 'unset'}); broker-side source-IP "
            "matching is verified by Upstox at request time"))
    else:
        checks.append(_check(
            "static_ip_configuration", False, WARNING,
            "no registered static IP recorded in settings; Upstox live order "
            "traffic may be rejected if the account enforces static-IP checks"))

    # Data freshness (spec section 32): only verifiable when the caller
    # supplies the latest market-data timestamp.
    last_data_ts = context.get("last_market_data_ts")
    if last_data_ts:
        try:
            parsed = datetime.fromisoformat(str(last_data_ts).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age_s = (now - parsed).total_seconds()
            stale = age_s > float(context.get("data_stale_after_s", 300))
            checks.append(_check(
                "data_freshness", not stale, WARNING,
                f"last market data {age_s:.0f}s ago"))
        except ValueError:
            checks.append(_check("data_freshness", False, WARNING,
                                 f"last market data timestamp unreadable: {last_data_ts!r}"))
    else:
        checks.append(_unavailable("data_freshness", "no market-data timestamp supplied"))

    # Token lifecycle (spec section 38): presence is checked by the base
    # suite; freshness is only knowable with an issued-at timestamp.
    issued_at = context.get("token_issued_ts")
    if issued_at:
        try:
            parsed = datetime.fromisoformat(str(issued_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age_h = (now - parsed).total_seconds() / 3600.0
            # Upstox access tokens expire at 03:30 IST the next day; treat
            # >20h as EXPIRING for advisory purposes (authoritative expiry is
            # the broker's).
            state = "VALID" if age_h < 20 else ("EXPIRING" if age_h < 26 else "EXPIRED")
            checks.append(_check(
                "token_lifecycle", state != "EXPIRED", WARNING,
                f"access token age {age_h:.1f}h -> {state} (broker is authoritative)"))
        except ValueError:
            checks.append(_unavailable("token_lifecycle", "token issued-at unreadable"))
    else:
        checks.append(_unavailable("token_lifecycle", "no token issued-at timestamp supplied"))

    return checks
