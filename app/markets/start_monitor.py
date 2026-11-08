"""Read-only launchpad projection for the single-page Start & Monitor screen.

This module **composes** state that already exists. It adds no gate, no journal,
no order path and no second execution boundary: everything it returns is read
from

* the local ``LIVE_MARKET_PAPER`` engine (sessions, symbols, limits, orders),
* the ``LiveExecutionCoordinator`` (per-market mode, gate state, readiness,
  kill switch, documented exit route),
* the ``ScanCapacityService`` (documented quota windows and sustainable cadence),
* the credential stores (presence booleans only) and the owner-verification
  receipts.

Why it exists: the operator console previously spread "choose a mode", "pick
symbols", "save credentials", "start a session" and "watch what happened" across
six workspaces. The screen behind this payload puts them on one page, so the page
needs one server-computed answer per market instead of six browser-side guesses.
A browser cannot decide whether a market is ready, and it must never claim a
broker is connected when the server says otherwise.

Honesty rules enforced here (and asserted by tests):

* Every status string is derived from server state. ``ERROR`` never means "the
  page could not fetch"; that is reported separately by the route.
* ``credential_*`` fields are booleans plus a source label. No token, key,
  secret, header or prefix ever enters the payload.
* ``autopilot`` is an *evaluation* of the configured gates, not a switch. The
  only thing that can make automation act is starting a session with a strategy
  other than ``manual_only`` after its own gates pass.
* A market routed ``LIVE`` never reports paper activity, and vice versa.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

MARKETS: tuple[str, ...] = ("NSE", "NASDAQ")

#: Hard ceiling of one read-only scan cycle (same value the scan routes enforce).
MAX_SYMBOLS = 100
#: The fastest cadence the console offers; used only to ask the server what the
#: documented windows would allow at that speed.
FASTEST_CADENCE_SECONDS = 15.0

CURRENCY_BY_MARKET = {"NSE": "INR", "NASDAQ": "USD"}
BROKER_BY_MARKET = {"NSE": "Upstox", "NASDAQ": "Alpaca"}
VENUE_LABEL = {
    "NSE": "NSE · Upstox (INR)",
    "NASDAQ": "NASDAQ · Alpaca (USD)",
}

#: The five header badges. ``ERROR`` is reserved for a server-reported fault
#: (blocked runtime, engaged kill switch, unresolved intent), never for a
#: transport failure of this page.
BADGE_PAPER = "PAPER"
BADGE_LIVE_DISABLED = "LIVE · DISABLED"
BADGE_LIVE_ARMED = "LIVE · ARMED"
BADGE_LIVE_ACTIVE = "LIVE · ACTIVE"
BADGE_ERROR = "ERROR"

#: Fields of the server-side paper/live risk gate that are hard caps. Presence of
#: a positive value is what makes automation legal; cost rates are excluded.
BOUNDED_CAP_FIELDS: tuple[str, ...] = (
    "max_order_value",
    "max_symbol_exposure",
    "max_total_exposure",
    "max_open_positions",
    "max_daily_loss",
    "max_daily_orders",
    "max_quote_age_seconds",
)

#: Strategies that can act without a per-order prompt once a session is running.
AUTOPILOT_STRATEGIES = ("parallel_ensemble", "moving_average", "momentum", "mean_reversion",
                        "breakout", "rsi", "macd", "bollinger")

#: Documented limits of the automation promise. Printed verbatim in the UI so a
#: "what may automation do" statement is never the browser's own invention.
AUTOPILOT_MAY = (
    "analyse the selected symbols on completed bars only",
    "create SIMULATED paper orders and fills inside the local ledger while the session runs",
    "hold at most the current open-position cap, never one position per scanned symbol",
    "respect daily-order, daily-loss, per-symbol and total exposure caps from the server",
    "stop entering when the quote is stale, the provider throttles, or a cycle misses its cadence",
)
AUTOPILOT_MAY_NOT = (
    "raise any risk limit, including the open-position cap",
    "turn a 100-symbol scan into 100 orders",
    "submit a broker order while the market is in PAPER mode",
    "keep running after Stop, Emergency Stop, or a restart",
    "retry an order whose outcome is ambiguous — that requires reconciliation",
)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _badge(mode: str, gate_state: str, *, error: bool) -> str:
    """Header badge for one market, derived only from server state."""
    if error:
        return BADGE_ERROR
    state = str(gate_state or "DISABLED").upper()
    if str(mode or "PAPER").upper() == "LIVE":
        if state == "ACTIVE":
            return BADGE_LIVE_ACTIVE
        if state == "ARMED":
            return BADGE_LIVE_ARMED
        return BADGE_LIVE_DISABLED
    if state == "ACTIVE":
        # A gate can only be ACTIVE while the market is routed LIVE; report the
        # conflict rather than a reassuring badge.
        return BADGE_ERROR
    return BADGE_PAPER


def _symbol_rows(watchlist: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Watchlist rows in picker shape (no prices, no secrets)."""
    out: list[dict[str, Any]] = []
    for row in watchlist or ():
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        out.append(
            {
                "symbol": symbol,
                "instrument_key": str(row.get("instrument_key") or ""),
                "name": str(row.get("name") or symbol),
                "added_at": row.get("added_at"),
            }
        )
    return out


def _credential_matrix(
    market: str,
    *,
    data_present: bool,
    data_source: str,
    broker_paper_present: bool,
    broker_live_present: bool,
) -> dict[str, Any]:
    """Presence-only credential view for one market.

    ``AXIOM API token`` is deliberately absent: it authenticates *this browser
    session* to the loopback API, not a provider, and conflating the two is the
    mistake this matrix exists to prevent.
    """
    # No AXIOM API token row here: it authenticates this browser session to the
    # loopback API, not a provider. It is listed once, in the page's
    # session-auth panel, so it cannot be confused with a broker credential.
    rows = [
        {
            "slot": f"{BROKER_BY_MARKET[market].upper()} MARKET DATA",
            "scope": "read-only prices and bars",
            "present": bool(data_present),
            "detail": (f"configured via {data_source}" if data_present
                       else "not configured — quotes and scans cannot run"),
            "affects_execution": False,
        },
    ]
    if market == "NASDAQ":
        rows.extend(
            [
                {
                    "slot": "ALPACA BROKER PAPER",
                    "scope": "broker paper sandbox orders",
                    "present": bool(broker_paper_present),
                    "detail": ("separate paper-environment key pair"
                               if broker_paper_present else "not configured"),
                    "affects_execution": False,
                },
                {
                    "slot": "ALPACA BROKER LIVE",
                    "scope": "real money · order writes",
                    "present": bool(broker_live_present),
                    "detail": ("separate live-environment key pair"
                               if broker_live_present else "not configured"),
                    "affects_execution": True,
                },
            ]
        )
    else:
        rows.append(
            {
                "slot": "UPSTOX LIVE ACCESS TOKEN",
                "scope": "real money · order writes",
                "present": bool(broker_live_present),
                "detail": ("separate live token slot" if broker_live_present
                           else "not configured — the read-only token is never reused"),
                "affects_execution": True,
            }
        )
    return {"rows": rows, "secret_returned": False}


def _cadence_view(
    plan: dict[str, Any] | None,
    *,
    requested: float,
    selected: int,
    cap: int,
) -> dict[str, Any]:
    """Turn a capacity plan into one unambiguous cadence statement.

    ``cadence_sustainable_seconds`` from the service is the *shortest period the
    provider quota can carry this selection at*: a small selection legitimately
    yields a fraction of a second. Reporting that alone next to a 15 s request
    reads as "0.9 s is supported", which is the opposite of the truth for a
    100-symbol scan. So the verdict is computed here, once, in the server, and
    the browser prints the sentence rather than doing quota arithmetic.
    """
    if not plan:
        return {
            "requested_seconds": requested,
            "sustainable_seconds": None,
            "max_symbols_at_cadence": None,
            "requests_per_cycle": {},
            "dispatch_seconds": None,
            "status": "UNAVAILABLE",
            "source": "unavailable",
            "supportable": False,
            "label": "Cadence unavailable: the capacity service did not answer.",
        }
    sustainable = plan.get("cadence_sustainable_seconds")
    max_at = plan.get("max_symbols_at_requested_cadence")
    requested_s = float(plan.get("cadence_requested_seconds") or requested or 0.0)
    try:
        sustainable_f = float(sustainable) if sustainable is not None else None
    except (TypeError, ValueError):
        sustainable_f = None
    supportable = bool(
        sustainable_f is not None and requested_s > 0 and sustainable_f <= requested_s
    )
    # Never imply the fastest cadence carries a full scan: state the ceiling at
    # the requested cadence as well as the period the quota needs.
    scope = f"for {selected} selected symbol(s)" if selected else "for the current selection"
    ceiling = (
        f"At {requested_s:g} s the provider quota carries at most {max_at} of {cap} symbols."
        if max_at is not None else ""
    )
    if sustainable_f is None:
        label = "Cadence unavailable: no quota plan for this market."
    elif sustainable_f > requested_s:
        label = (
            f"{requested_s:g} s is NOT supported {scope}: the provider quota needs at least "
            f"{sustainable_f:g} s per cycle. Slow the cadence down or select fewer symbols."
        )
    elif supportable:
        label = (
            f"{requested_s:g} s every cycle is possible {scope}: the provider quota needs only "
            f"{sustainable_f:g} s per cycle. {ceiling}"
        ).strip()
    else:
        label = (
            f"{requested_s:g} s every cycle is possible {scope}: the provider quota needs only "
            f"{sustainable_f:g} s per cycle."
        )
    return {
        "requested_seconds": requested_s,
        "sustainable_seconds": sustainable_f,
        "max_symbols_at_cadence": max_at,
        "requests_per_cycle": plan.get("requests_per_cycle") or {},
        "dispatch_seconds": plan.get("expected_dispatch_seconds"),
        "status": plan.get("status"),
        "source": "server",
        "supportable": supportable,
        "label": label,
    }


def _autopilot_view(
    market: str,
    *,
    paper: dict[str, Any],
    live: dict[str, Any],
    quota: dict[str, Any],
    facts: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate — never enable — Autopilot for one market.

    Autopilot for PAPER means "this local session may create simulated orders
    without a per-ticket prompt". Autopilot for LIVE means "the ACTIVE gate may
    route broker orders on its own cycle". Both are conclusions drawn from gates
    that already exist; nothing here flips one.

    Each blocker names the modes it blocks, because the same fact can be fatal
    for one mode and irrelevant for the other: AXIOM routes no NSE exit, which
    disables LIVE automation on a strategy needing an AXIOM-routed close, while
    a *paper* NSE session closes positions in the local simulator and is not
    affected.
    """
    blockers: list[dict[str, Any]] = []
    cautions: list[dict[str, Any]] = []
    limits = facts.get("limits") or {}
    live_blockers = list(live.get("blockers") or ())
    exit_route = live.get("exit_route") or {}
    routed_exit = bool(exit_route.get("routed"))
    paper_state = str(paper.get("session_state") or "STOPPED").upper()
    live_gate = str(live.get("gate_state") or "DISABLED").upper()

    if not facts.get("credential_present"):
        blockers.append({
            "code": "NO_MARKET_DATA_CREDENTIAL",
            "blocks": ["PAPER", "LIVE"],
            "text": f"{BROKER_BY_MARKET[market]} market-data credential is not configured, "
                    "so no quote or bar can be validated.",
        })
    if str(quota.get("verification") or "") == "UNVERIFIED":
        cautions.append({
            "code": "PLAN_UNVERIFIED",
            "text": "The market-data plan entitlement is operator-declared and not verified "
                    "against the provider account; planning uses the conservative documented "
                    "profile, so the real ceiling may be lower than the plan allows.",
        })
    if market == "NSE" and not routed_exit:
        blockers.append({
            "code": "NO_ROUTED_EXIT",
            "blocks": ["LIVE"],
            "text": "AXIOM cannot close an NSE position: no exit is routed. LIVE automation may "
                    "not run a strategy that relies on an AXIOM-routed exit; close manually in "
                    "the Upstox app, then Reconcile.",
        })
    if not limits.get("configured", True):
        blockers.append({
            "code": "RISK_LIMITS_INCOMPLETE",
            "blocks": ["PAPER", "LIVE"],
            "text": "One or more server-side risk limits is unset or zero, so automation "
                    "fails closed.",
        })
    if facts.get("blocked"):
        blockers.append({
            "code": str(facts.get("blocked") or "RUNTIME_BLOCKED"),
            "blocks": ["PAPER", "LIVE"],
            "text": str(facts.get("blocked_detail") or "The runtime has blocked this market."),
        })
    if facts.get("kill_engaged"):
        blockers.append({
            "code": "KILL_SWITCH_ENGAGED",
            "blocks": ["PAPER", "LIVE"],
            "text": f"Kill switch engaged: {facts.get('kill_reason') or 'reset required'}.",
        })
    if facts.get("unresolved_intents"):
        blockers.append({
            "code": "UNRESOLVED_ORDER_OUTCOME",
            "blocks": ["PAPER", "LIVE"],
            "text": "At least one order outcome is ambiguous. Reconcile before any new action; "
                    "automatic retries are refused.",
        })
    # Readiness repeats a fact in several checks (a missing token shows up once
    # per prerequisite), so the union is de-duplicated: the screen must list each
    # unmet requirement once, not five times.
    seen = {b["text"] for b in blockers}
    for dep in live_blockers:
        text = str(dep)
        if text in seen:
            continue
        seen.add(text)
        blockers.append({"code": "LIVE_READINESS", "blocks": ["LIVE"], "text": text})

    def gaps(mode: str) -> list[dict[str, Any]]:
        return [b for b in blockers if mode in (b.get("blocks") or ())]

    paper_gaps = gaps("PAPER")
    live_gaps = gaps("LIVE")
    paper_ok = not paper_gaps
    live_ok = not live_gaps and live_gate == "ACTIVE"

    return {
        "paper": {
            "available": paper_ok,
            "active": paper_ok and paper_state in {"RUNNING", "MARKET_CLOSED_WAITING"},
            "gaps": paper_gaps,
            "reason": (
                "A started local session may create SIMULATED orders without a per-order prompt."
                if paper_ok else "Blocked until every listed requirement is met."
            ),
        },
        "live": {
            "available": live_ok,
            "active": live_ok,
            "gaps": live_gaps,
            "reason": (
                "The LIVE gate is ACTIVE and every readiness check passed: the broker cycle may "
                "route orders on its own. This is not a profitability claim."
                if live_ok else
                "LIVE Autopilot stays disabled until readiness passes, the gate is ACTIVE, and "
                "the operator has explicitly started this session."
            ),
        },
        "blockers": blockers,
        "cautions": cautions,
        "may": list(AUTOPILOT_MAY),
        "may_not": list(AUTOPILOT_MAY_NOT),
        "limits_source": "server",
    }


def build_launchpad(
    *,
    engine: Any,
    coordinator: Any,
    scan_service: Any,
    watchlists: dict[str, list[dict[str, Any]]],
    data_credentials: dict[str, dict[str, Any]],
    broker_credentials: dict[str, Any] | None,
    upstox_live_token_present: bool,
    readiness_matrix: dict[str, Any] | None,
    strategies: Iterable[dict[str, Any]],
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """One read-only payload for the Start & Monitor screen.

    ``engine`` may be ``None`` (local paper unavailable) and ``coordinator`` may be
    ``None`` (live path not constructed); each market then reports
    ``ERROR``-free ``UNAVAILABLE`` detail instead of a fabricated state.
    """
    now = (clock or (lambda: datetime.now(timezone.utc)))()

    broker_creds = broker_credentials or {}
    receipts = (readiness_matrix or {}).get("modes", {})
    strategy_rows = list(strategies or ())
    strategy_names = [str(row.get("name")) for row in strategy_rows] or list(AUTOPILOT_STRATEGIES)

    markets: dict[str, Any] = {}
    for market in MARKETS:
        currency = CURRENCY_BY_MARKET[market]
        snapshot = engine.market_snapshot(market) if engine is not None else {}
        live_status = coordinator.status(market) if coordinator is not None else {}
        live_ready = coordinator.readiness(market) if coordinator is not None else {}

        mode = str(live_status.get("mode") or "PAPER").upper()
        gate = dict(live_status.get("gate") or {})
        gate_state = str(gate.get("state") or "DISABLED").upper()
        kill_engaged = bool(gate.get("kill_engaged"))
        unresolved = list(live_status.get("unresolved_intents") or ())
        blocked = live_status.get("blocked")
        if mode != "LIVE":
            # Paper-routed markets must not inherit a stale live gate state.
            gate_state = "DISABLED" if not gate.get("live_available") else gate_state

        readiness_row = receipts.get(f"{market}_PAPER") or {}
        data_cred = data_credentials.get(market) or {}
        data_present = bool(data_cred.get("present"))
        broker_paper = bool((broker_creds.get("paper") or {}).get("present"))
        broker_live = (bool((broker_creds.get("live") or {}).get("present"))
                       if market == "NASDAQ" else bool(upstox_live_token_present))

        quota = scan_service.quota_profile_payload(market) if scan_service is not None else {
            "market": market, "profile": "none", "verification": "UNAVAILABLE",
            "policies": [], "credential_present": data_present,
        }
        selection = list(snapshot.get("watched_symbols") or ())
        selected_count = len(selection)
        plan = None
        requested_cadence = 0.0
        if scan_service is not None:
            try:
                # Ask the server for the cadence this symbol count actually
                # needs, and for how many symbols the fastest UI cadence (15 s)
                # could carry. Both numbers come from the documented quota
                # windows; neither is invented here or in the browser.
                plan = scan_service.plan(
                    market,
                    symbols=max(1, min(MAX_SYMBOLS, selected_count or 1)),
                    cadence_s=FASTEST_CADENCE_SECONDS,
                ).to_dict()
                requested_cadence = float(plan.get("cadence_requested_seconds") or 0.0)
            except (ValueError, KeyError, TypeError):
                plan = None

        limits = dict((snapshot.get("risk_limits") or {}))
        # A limit counts only if the server actually enforces it. Cost *rates*
        # (brokerage/slippage/spread bps) may legitimately be zero and are not
        # caps, so testing "every number is positive" would invent a blocker.
        enforced_caps = [k for k in BOUNDED_CAP_FIELDS if k in limits]
        facts = {
            "credential_present": data_present,
            "limits": {
                "configured": bool(enforced_caps) and all(
                    isinstance(limits[k], (int, float))
                    and not isinstance(limits[k], bool)
                    and float(limits[k]) > 0
                    for k in enforced_caps
                ),
                "checked_fields": enforced_caps,
            },
            "blocked": blocked,
            "blocked_detail": live_status.get("blocked_detail"),
            "kill_engaged": kill_engaged,
            "kill_reason": gate.get("kill_reason"),
            "unresolved_intents": unresolved,
        }

        paper_block = {
            "engine_available": engine is not None,
            "session_state": str(snapshot.get("session_state") or "UNAVAILABLE").upper(),
            "state_reason": snapshot.get("state_reason"),
            "strategy": snapshot.get("strategy"),
            "timeframe": snapshot.get("timeframe"),
            "automation_state": snapshot.get("automation_state"),
            "fill_type": snapshot.get("fill_type") or "SIMULATED",
            "execution_destination": snapshot.get("execution_destination"),
            "real_money": False,
            "positions": len(snapshot.get("positions") or ()),
            "working_orders": sum(
                1 for order in (snapshot.get("orders") or ())
                if str(order.get("status") or "").upper() in {"WORKING", "PARTIALLY_FILLED"}
            ),
            "fills": len(snapshot.get("fills") or ()),
            "account": snapshot.get("account"),
            "readiness_status": readiness_row.get("overall_status"),
            "readiness_blockers": list(readiness_row.get("blockers") or ()),
        }
        live_block = {
            "mode": mode,
            "mode_label": live_status.get("mode_label"),
            "gate_state": gate_state,
            "gate_live_available": bool(gate.get("live_available")),
            "kill_engaged": kill_engaged,
            "kill_reason": gate.get("kill_reason"),
            "armed_at": gate.get("armed_at"),
            "activated_at": gate.get("activated_at"),
            "ready": bool(live_ready.get("ok")) if coordinator is not None else False,
            "failed_checks": list(live_ready.get("failed") or ()),
            "checks": dict(live_ready.get("checks") or {}),
            "blockers": list(live_ready.get("missing_dependencies") or ()),
            "risk_limits": live_status.get("risk_limits"),
            "blocked": blocked,
            "blocked_detail": live_status.get("blocked_detail"),
            "unresolved_intents": unresolved,
            "submitted_total": live_status.get("submitted_total"),
            "rejected_total": live_status.get("rejected_total"),
            "last_cycle_status": live_status.get("last_cycle_status"),
            "confirmation_phrase": gate.get("live_confirmation_phrase"),
            "exit_route": (live_ready.get("documented_capabilities") or {}).get("exit_route")
                          or (live_status.get("exit_route")),
            "available": coordinator is not None,
        }
        autopilot = _autopilot_view(market, paper=paper_block, live=live_block, quota=quota, facts=facts)

        markets[market] = {
            "market": market,
            "venue_label": VENUE_LABEL[market],
            "broker": BROKER_BY_MARKET[market],
            "currency": currency,
            "badge": _badge(mode, gate_state, error=bool(blocked or kill_engaged)),
            "mode": mode,
            "selected_symbols": _symbol_rows(watchlists.get(market) or ()),
            "selected_symbol_count": selected_count,
            "session_symbols": [str(s) for s in selection],
            "symbol_cap": 100,
            "cadence": _cadence_view(
                plan,
                requested=requested_cadence,
                selected=selected_count,
                cap=MAX_SYMBOLS,
            ),
            "quota": quota,
            "credentials": _credential_matrix(
                market,
                data_present=data_present,
                data_source=str(data_cred.get("source") or "none"),
                broker_paper_present=broker_paper,
                broker_live_present=broker_live,
            ),
            "paper": paper_block,
            "live": live_block,
            "autopilot": autopilot,
            "limits": limits,
            "position_cap": limits.get("max_open_positions"),
            "daily_order_cap": limits.get("max_daily_orders"),
            "readiness": readiness_row,
            "cycle_missed": _cycle_missed(snapshot, plan),
            "stale_policy": _stale_policy(snapshot),
        }

    return {
        "schema": "axiom.start_monitor.v1",
        "generated_at": now.isoformat(),
        "server": {"connection": "LOCAL", "secret_returned": False},
        "mode_choices": [
            {"id": "PAPER", "label": "PAPER", "real_money": False,
             "detail": "Local simulated fills on read-only market data. Zero broker orders."},
            {"id": "LIVE", "label": "LIVE", "real_money": True,
             "detail": "Real broker orders, per market, behind readiness + Arm + typed phrase."},
        ],
        "markets": markets,
        "strategies": strategy_rows,
        "strategy_names": strategy_names,
        "autopilot_strategies": [n for n in strategy_names if n in AUTOPILOT_STRATEGIES],
        "max_symbols_per_scan": 100,
        "execution_boundary": "app.markets.live_execution",
        "broker_order_api_calls": 0,
        "evidence": {
            "provider_network_calls": 0,
            "real_money_enabled": any(m["mode"] == "LIVE" and m["live"]["gate_state"] == "ACTIVE"
                                      for m in markets.values()),
            "note": "Read-only projection. Starting a session or moving a gate uses the existing "
                    "routes; nothing on this payload can place, modify or cancel an order.",
        },
    }


def _cycle_missed(snapshot: dict[str, Any], plan: dict[str, Any] | None) -> dict[str, Any]:
    """What the server will do when a cycle cannot keep its cadence."""
    sustainable = (plan or {}).get("cadence_sustainable_seconds")
    return {
        "requested_seconds": (plan or {}).get("cadence_requested_seconds"),
        "sustainable_seconds": sustainable,
        "behavior": (
            "A cycle that cannot get quota before its deadline is deferred, not sent. "
            "Symbols are reported deferred and the next cycle re-plans."
        ),
        "stale_behavior": _stale_policy(snapshot).get("behavior"),
    }


def _stale_policy(snapshot: dict[str, Any]) -> dict[str, Any]:
    limits = snapshot.get("risk_limits") or {}
    age = limits.get("max_quote_age_seconds")
    return {
        "max_quote_age_seconds": age,
        "behavior": (
            f"Quotes older than {age}s are refused fail-closed; no sample or replay bar is "
            "substituted." if age else "Quote-age limit unavailable from the server."
        ),
    }
