"""V1 dashboard: clean local HTML view + engine/emergency controls.

Clearly displays PAPER / SANDBOX mode and that LIVE trading is not enabled.
All data endpoints sit behind the existing bearer-token dependency; the HTML
page fetches /dashboard/data with the same token supplied at load time via a
one-time inline bootstrap (no credentials in logs; the token is provided by
the operator, never stored server-side beyond the running process settings).
"""

from app import __version__
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api.auth import authenticate, loopback_or_bearer
from app.live.alerts import recent_alerts

router = APIRouter()


def _paper_view(request: Request) -> dict:
    simulator = getattr(request.app.state, "simulator", None)
    broker = getattr(request.app.state, "broker", None)
    view = {
        "pipeline": "LOCAL_PAPER",
        "state": simulator.status()["state"] if simulator else "STOPPED",
        "positions": broker.get_positions() if broker else [],
        "equity": broker.get_balance()["equity"] if broker else None,
        "drawdown": broker.get_balance().get("drawdown") if broker else None,
        "realized_pnl": broker.get_balance().get("realized_pnl") if broker else None,
    }
    if simulator:
        status = simulator.status()
        view.update(
            connection=status.get("connection_status", status.get("connection")),
            market_status=status.get("market_status"),
            market_open=status.get("market_open"),
            strategy=status.get("strategy"),
            parameters=status.get("parameters"),
            signals=status.get("signals"),
            last_price=None,
        )
    return view


def _live_ops_view(request: Request, repository) -> dict | None:
    """Guards/canary/token/breach snapshot from recorded state (never raises)."""
    if repository is None:
        return None
    try:
        settings = request.app.state.settings
        from app.config.hosts import LIVE_ORDER_HOSTS, host_of
        from app.live.guards import CanaryGuard, TokenGuard
        from app.live.watchdog import BreachMonitor

        from app.live.lock import EngineLock

        live = (bool(settings.live_trading)
                and (settings.broker or "").upper() == "UPSTOX_LIVE")
        view = {
            "lock": EngineLock(
                repository,
                stale_after_seconds=settings.live_lock_stale_seconds,
                live=live).status(),
            "live_trading": live,
            "order_host": settings.upstox_live_base_url if live else None,
            "order_host_is_documented_live": (
                host_of(settings.upstox_live_base_url) in LIVE_ORDER_HOSTS
                if live else None),
            "token": TokenGuard(
                repository,
                max_age_hours=settings.live_token_max_age_hours,
                warn_minutes=settings.live_token_warn_minutes).status(),
            "canary": CanaryGuard(
                repository,
                max_notional=settings.live_canary_max_notional,
                first_orders=settings.live_canary_orders).status(),
            "breaches": BreachMonitor(
                repository, max_daily_loss=settings.max_daily_loss,
                max_equity_drawdown=settings.live_max_equity_drawdown,
                auto_stop=settings.live_auto_stop_on_breach).status(),
            "last_reconcile_at": repository.get_engine_state(
                "last_reconcile_at"),
            "open_live_orders": len(repository.open_live_orders()),
            # an alarm nobody has acknowledged is an open incident (V2.1)
            "unacknowledged_critical": sum(
                1 for alert in recent_alerts(repository, limit=200)
                if alert["severity"] == "CRITICAL" and not alert["acknowledged"]),
        }
        return view
    except Exception:
        return None


def _sandbox_view(request: Request, *, live: bool = False) -> dict:
    repository = request.app.state.repository
    engine = getattr(request.app.state, "sandbox_engine", None)
    broker = getattr(request.app.state, "sandbox_broker", None)
    from app.engine.state import read_equity_state

    if live:
        orders = repository.list_live_orders(limit=25) if repository else []
    else:
        orders = repository.list_sandbox_orders(limit=25) if repository else []
    equity_state = read_equity_state(repository) if repository else {}
    view = {
        "pipeline": "UPSTOX_LIVE" if live else "UPSTOX_SANDBOX",
        "state": engine.status()["state"] if engine else "IDLE",
        "sandbox_orders": orders,
        # Live orders are additionally exposed under their true name.
        "live_orders": orders if live else None,
        "open_orders": len([o for o in orders if o["status"] in {"SUBMITTED", "OPEN", "PARTIALLY_FILLED"}]),
        "ledger": broker.ledger.snapshot() if broker else equity_state.get("ledger"),
        "equity": equity_state.get("equity"),
        "drawdown": equity_state.get("drawdown"),
        "realized_pnl": (broker.ledger.realized_pnl if broker else None),
        "positions": (
            [{"quantity": broker.ledger.position_quantity,
              "avg_price": broker.ledger.position_avg_price}] if broker else []
        ),
    }
    if engine:
        status = engine.status()
        view.update(
            connection=status["broker"]["available"] and "CONNECTED" or "BROKER_DISABLED",
            market_status=None,
            strategy=status["strategy"]["name"],
            parameters=status["strategy"]["parameters"],
            signals=status["signals"],
            regime=status.get("regime"),
        )
    return view


@router.get("/dashboard/data", dependencies=[Depends(authenticate)])
async def dashboard_data(request: Request):
    settings = request.app.state.settings
    repository = request.app.state.repository
    broker_mode = settings.broker.upper()
    live = bool(settings.live_trading) and broker_mode == "UPSTOX_LIVE"
    emergency = (repository.get_engine_state("emergency_stop") or {}) if repository else {}
    promoted = repository.promoted_version("momentum") if repository else None
    if broker_mode in {"UPSTOX_SANDBOX", "UPSTOX_LIVE"}:
        view = _sandbox_view(request, live=live)
    else:
        view = _paper_view(request)
    # Market Events — additive, never exposes secrets, never renders untrusted HTML
    try:
        market_events = repository.list_market_events(limit=10) if repository else []
        # ensure sanitized (already stripped of < > in listener, but double-ensure)
        for ev in market_events:
            ev["headline"] = str(ev.get("headline", ""))[:500]
            ev["summary"] = str(ev.get("summary", ""))[:2000]
    except Exception:
        market_events = []
    # 3.0.5: additive quality/market/execution/health panels (no secrets)
    try:
        # Recent quality scores (if any)
        quality_recent = repository.list_system_events(limit=15) if repository else []
        quality_events = [e for e in quality_recent if e["kind"] in ("QUALITY_REJECT", "NET_EDGE_REJECT", "LIQUIDITY_REJECT", "VOLATILITY_SIZING", "QUALITY_SCORE")]
    except Exception:
        quality_events = []
    try:
        # Cross-market context (if engine has it)
        engine_obj = getattr(request.app.state, "sandbox_engine", None)
        cross_ctx = None
        if engine_obj and hasattr(engine_obj, "cross_market"):
            try:
                cross_ctx = engine_obj.cross_market.context_for().__dict__ if hasattr(engine_obj.cross_market, "context_for") else None
            except Exception:
                cross_ctx = None
        # Hot path metrics
        hot_metrics = None
        if engine_obj and hasattr(engine_obj, "hot_path_monitor"):
            try:
                hot_metrics = engine_obj.hot_path_monitor.snapshot() if engine_obj.hot_path_monitor else None
            except Exception:
                hot_metrics = None
    except Exception:
        cross_ctx = None
        hot_metrics = None
    try:
        # Strategy health (degradation/MAE)
        health = repository.get_engine_state("strategy_health") if repository else None
    except Exception:
        health = None
    data = {
        "version": __version__,
        "mode": settings.mode.upper(),
        "live_trading": live,
        "live_enabled": ("ENABLED — REAL MONEY" if live
                         else "disabled" if broker_mode == "LOCAL_PAPER"
                         else "not enabled (sandbox orders only)"),
        "broker_mode": broker_mode,
        "display": (f"{broker_mode} — LIVE / REAL MONEY" if live
                    else f"{broker_mode} — SANDBOX ORDERS ONLY"
                    if broker_mode == "UPSTOX_SANDBOX"
                    else f"{broker_mode} — SIMULATED (PAPER)"),
        "emergency_stop": bool(emergency.get("stopped")),
        "emergency_reason": emergency.get("reason"),
        "model_version": promoted,
        "system_events": repository.list_system_events(limit=15) if repository else [],
        # V2.1 L-ops: alerts + the operational rails, visible even when the
        # engine process is stopped or has crashed.
        "alerts": recent_alerts(repository, limit=10),
        "live_ops": _live_ops_view(request, repository),
        "market_events": market_events,
        "quality_events": quality_events[:8],
        "market_context": cross_ctx if isinstance(cross_ctx, dict) else (cross_ctx.__dict__ if cross_ctx and hasattr(cross_ctx, "__dict__") else None),
        "execution_quality": {"liquidity": quality_events[:3], "hot_path": hot_metrics},
        "strategy_health": health,
        **view,
    }
    return data


@router.post("/engine/stop", dependencies=[Depends(authenticate)])
async def engine_stop(request: Request):
    """Emergency STOP: persists the flag; cancels open sandbox orders if any."""
    repository = request.app.state.repository
    repository.record_system_event("EMERGENCY_STOP_REQUESTED", {"source": "dashboard"})
    result = {"stopped": True}
    engine = getattr(request.app.state, "sandbox_engine", None)
    if engine is not None:
        result = engine.emergency_stop(reason="dashboard")
    simulator = getattr(request.app.state, "simulator", None)
    if simulator is not None:
        try:
            await simulator.pause()
        except ValueError:
            pass  # only pauseable while running
    repository.set_engine_state(
        "emergency_stop",
        {"stopped": True, "ts": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(), "reason": "dashboard"},
    )
    return result


@router.post("/engine/resume", dependencies=[Depends(authenticate)])
async def engine_resume(request: Request):
    repository = request.app.state.repository
    repository.set_engine_state("emergency_stop", {"stopped": False})
    repository.record_system_event("ENGINE_RESUME_REQUESTED", {"source": "dashboard"})
    return {"stopped": False}


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False,
            dependencies=[Depends(loopback_or_bearer)])
async def dashboard(request: Request):
    """The dashboard UI, readable by LOCAL browsers without a Bearer header.

    The page itself contains NO secrets: the operator's API_TOKEN is never
    rendered into it — the page JS asks the operator for the token (kept in
    sessionStorage, never in URLs) and sends it as an Authorization header on
    every protected call. Remote (non-loopback) clients still need the token
    to load the page at all.

    The mode badges are rendered SERVER-SIDE from Settings (so the truthful
    state is visible even with JavaScript disabled) and then refreshed by the
    page's polling loop."""

    settings = request.app.state.settings
    broker_mode = settings.broker.upper()
    live = bool(settings.live_trading) and broker_mode == "UPSTOX_LIVE"
    if live:
        mode_badge, live_badge, live_class = (
            f"{broker_mode} · LIVE — REAL MONEY", "ENABLED — REAL MONEY", "badge warn")
    elif broker_mode == "UPSTOX_SANDBOX":
        mode_badge, live_badge, live_class = (
            f"{broker_mode} · SANDBOX ORDERS", "disabled (sandbox orders only)", "badge warn")
    else:
        mode_badge, live_badge, live_class = (
            f"{broker_mode} · SIMULATED (PAPER)", "disabled", "badge warn")

    page = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Arena 3.0.5 — Command Console</title><style>
:root{
  --bg:#010806; --bg-2:#03130d; --panel:rgba(5,38,26,.52); --panel-2:rgba(2,16,11,.72);
  --line:rgba(0,255,156,.20); --line-2:rgba(0,255,156,.45); --line-3:rgba(0,255,156,.75);
  --cash:#14f195; --green:#00ff9c; --mint:#8affce; --dim:#3f7d63; --muted:#5e9c81;
  --text:#c6ffe4; --amber:#ffc861; --red:#ff5f6d; --cyan:#5ad2ff;
  --mono:ui-monospace,SFMono-Regular,"JetBrains Mono","Cascadia Mono",Menlo,Consolas,monospace;
  /* glow intensity is a single dial: every glow in the page reads these three
     stops (and the scanline / vignette strength), so one attribute retunes it */
  --g-low:rgba(20,241,149,.16); --g-mid:rgba(20,241,149,.34); --g-high:rgba(20,241,149,.58);
  --scan:.028; --vig:.48;
}
body[data-glow="calm"]{ --g-low:rgba(20,241,149,.05); --g-mid:rgba(20,241,149,.12);
  --g-high:rgba(20,241,149,.22); --scan:.008; --vig:.30}
body[data-glow="vivid"]{ --g-low:rgba(20,241,149,.28); --g-mid:rgba(20,241,149,.55);
  --g-high:rgba(20,241,149,.85); --scan:.055; --vig:.62}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:
    radial-gradient(1200px 600px at 50% -10%,var(--g-mid),transparent 70%),
    radial-gradient(900px 500px at 100% 100%,var(--g-low),transparent 70%),
    linear-gradient(180deg,var(--bg) 0%,var(--bg-2) 60%,#010705 100%);
  color:var(--text);font:13px/1.5 var(--mono);letter-spacing:.02em;min-height:100%}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:9;
  background:repeating-linear-gradient(0deg,rgba(0,255,156,var(--scan)) 0 1px,transparent 1px 3px);
  mix-blend-mode:screen}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:10;
  background:radial-gradient(120% 100% at 50% 50%,transparent 55%,rgba(0,0,0,var(--vig)) 100%)}

/* ---------------------------------------------------------------- command bar */
header{position:relative;display:flex;align-items:center;gap:16px;flex-wrap:wrap;
  padding:12px 20px;background:linear-gradient(180deg,var(--g-low),rgba(0,0,0,0));
  border-bottom:1px solid var(--line-2)}
header::after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:1px;
  background:linear-gradient(90deg,transparent,var(--green),transparent);
  animation:sweep 6s linear infinite;opacity:.7}
@keyframes sweep{0%{transform:translateX(-40%)}100%{transform:translateX(40%)}}
.brand{display:flex;align-items:center;gap:12px;min-width:230px}

/* the core reacts to order flow: rings accelerate and a shockwave fires whenever
   the poll observes new order / fill activity, and the pip is coloured by state */
.core{width:38px;height:38px;position:relative;flex:0 0 auto}
.core .ring{position:absolute;inset:0;border-radius:50%;
  border:1px solid var(--line-2);border-top-color:var(--green);border-right-color:var(--dim);
  animation:spin 4.5s linear infinite}
.core .ring.b{inset:6px;border-top-color:var(--dim);border-left-color:var(--cash);
  animation-duration:7s;animation-direction:reverse}
.core .pip{position:absolute;inset:14px;border-radius:50%;background:var(--cash);
  box-shadow:0 0 12px var(--g-high),0 0 26px var(--g-mid);animation:pulse 2.4s ease-in-out infinite}
.core .burst{position:absolute;inset:0;border-radius:50%;
  border:1px solid var(--green);opacity:0;pointer-events:none}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes pulse{0%,100%{opacity:.55;transform:scale(.85)}50%{opacity:1;transform:scale(1.12)}}
@keyframes burst{0%{opacity:.85;transform:scale(.55)}100%{opacity:0;transform:scale(2.3)}}
.core.flow .burst{animation:burst .95s ease-out}
.core.flow .ring{animation-duration:.85s}
.core.flow .ring.b{animation-duration:1.15s}
.core.flow .pip{animation-duration:.5s}
.core.live .ring{animation-duration:1.7s;border-top-color:var(--cash)}
.core.live .ring.b{animation-duration:2.5s}
.core.alert .pip{background:var(--amber);animation-duration:1s;
  box-shadow:0 0 12px var(--amber),0 0 26px rgba(255,200,97,.4)}
.core.alert .ring{border-top-color:var(--amber)}
.core.halt .pip{background:var(--red);animation-duration:.7s;
  box-shadow:0 0 12px var(--red),0 0 30px rgba(255,95,109,.5)}
.core.halt .ring{border-top-color:var(--red);border-right-color:rgba(255,95,109,.4)}
.wordmark{font-size:19px;font-weight:600;letter-spacing:.42em;color:var(--cash);
  text-shadow:0 0 16px var(--g-mid)}
.wordmark small{display:block;font-size:9.5px;letter-spacing:.28em;color:var(--muted);
  font-weight:400;margin-top:3px}
.badges{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.badge,.warn{position:relative;padding:5px 12px;font-size:11px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--mint);background:rgba(0,255,156,.06);
  border:1px solid var(--line-2);clip-path:polygon(7px 0,100% 0,100% calc(100% - 7px),calc(100% - 7px) 100%,0 100%,0 7px)}
.badge.warn,.warn{color:var(--amber);border-color:rgba(255,200,97,.55);background:rgba(255,200,97,.07)}
#conn::before{content:"";display:inline-block;width:6px;height:6px;margin-right:8px;border-radius:50%;
  background:var(--green);box-shadow:0 0 8px var(--g-mid);animation:pulse 1.8s ease-in-out infinite;vertical-align:middle}
.spacer{flex:1}
.clock{font-size:12px;color:var(--mint);letter-spacing:.18em;min-width:132px;text-align:right}
.clock b{color:var(--cash);text-shadow:0 0 10px var(--g-mid)}
/* flow ticker: appears only when the poll actually saw order activity */
#flow-chip{font-size:10.5px;letter-spacing:.16em;color:var(--cash);opacity:0;
  padding:3px 9px;border:1px solid var(--line-3);text-transform:uppercase;white-space:nowrap;
  clip-path:polygon(6px 0,100% 0,100% calc(100% - 6px),calc(100% - 6px) 100%,0 100%,0 6px)}
#flow-chip.on{opacity:1;animation:chippulse 1.6s ease-out}
@keyframes chippulse{0%{background:rgba(20,241,149,.22)}100%{background:transparent}}
.ctrls{display:flex;gap:6px;align-items:center}
.chip{background:rgba(0,255,156,.04);border:1px solid var(--line);color:var(--muted);
  padding:5px 10px;font:500 10px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;
  cursor:pointer;clip-path:polygon(6px 0,100% 0,100% calc(100% - 6px),calc(100% - 6px) 100%,0 100%,0 6px)}
.chip:hover{color:var(--mint);border-color:var(--line-2);background:rgba(0,255,156,.09)}
.chip b{color:var(--cash);font-weight:600}
.stop{position:relative;background:rgba(255,95,109,.08);border:1px solid rgba(255,95,109,.6);
  color:#ffc9ce;padding:9px 16px;font:600 11.5px/1 var(--mono);letter-spacing:.18em;cursor:pointer;
  clip-path:polygon(9px 0,100% 0,100% calc(100% - 9px),calc(100% - 9px) 100%,0 100%,0 9px)}
.stop:hover{background:rgba(255,95,109,.18);box-shadow:0 0 18px rgba(255,95,109,.25)}
.stop:disabled{opacity:.45;cursor:not-allowed}
.stop.armed{animation:armed 1.6s ease-in-out infinite}
@keyframes armed{0%,100%{box-shadow:0 0 0 rgba(255,95,109,0)}50%{box-shadow:0 0 22px rgba(255,95,109,.35)}}
#err{font-size:11.5px;color:var(--red);letter-spacing:.06em;cursor:pointer;max-width:46ch}

/* ---------------------------------------------------------------------- vitals */
.vitals{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:1px;
  background:var(--line);border-bottom:1px solid var(--line-2)}
.vital{background:linear-gradient(180deg,rgba(5,38,26,.72),rgba(2,16,11,.9));padding:11px 16px;position:relative;overflow:hidden}
.vital::after{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--green);opacity:.6}
.vital .k{font-size:9.5px;letter-spacing:.24em;color:var(--muted);text-transform:uppercase}
.vital .v{font-size:21px;color:var(--cash);font-variant-numeric:tabular-nums;margin-top:3px;
  text-shadow:0 0 14px var(--g-mid);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vital .v.sm{font-size:15px;padding-top:4px}
.vital .v.neg{color:var(--red);text-shadow:0 0 14px rgba(255,95,109,.35)}
.vital .v.pos{color:var(--cash)}
.vital .v.warnc{color:var(--amber);text-shadow:0 0 14px rgba(255,200,97,.35)}

/* ---------------------------------------------------------------------- panels */
main{display:grid;grid-template-columns:repeat(12,1fr);gap:12px;padding:14px 20px 26px;max-width:1680px;margin:0 auto}
section{grid-column:span 4;position:relative;padding:13px 15px 15px;background:
    linear-gradient(180deg,var(--panel),var(--panel-2));
  border:1px solid var(--line);box-shadow:inset 0 0 40px var(--g-low)}
section::before,section::after{content:"";position:absolute;width:13px;height:13px;border:1px solid var(--line-3)}
section::before{top:-1px;left:-1px;border-right:0;border-bottom:0}
section::after{bottom:-1px;right:-1px;border-left:0;border-top:0}
section.w6{grid-column:span 6}section.w8{grid-column:span 8}
section.w3{grid-column:span 3}section.w12{grid-column:span 12}
h2{margin:0 0 10px;font-size:10.5px;font-weight:600;letter-spacing:.22em;text-transform:uppercase;
  color:var(--mint);display:flex;align-items:center;gap:9px}
h2::before{content:"◈";color:var(--green);font-size:11px;text-shadow:0 0 10px var(--g-mid)}
h2::after{content:"";flex:1;height:1px;background:linear-gradient(90deg,var(--line-2),transparent)}
table{width:100%;border-collapse:collapse;font-size:12px}
td,th{padding:5px 7px;border-bottom:1px solid rgba(0,255,156,.09);text-align:left;vertical-align:top}
tr:last-child td{border-bottom:0}
th{font-size:9.5px;letter-spacing:.2em;text-transform:uppercase;color:var(--muted);font-weight:500}
td:first-child{color:var(--muted);letter-spacing:.04em}
td.num,td:last-child{text-align:right;font-variant-numeric:tabular-nums;color:var(--text)}
td.num{color:var(--green)}
small{color:var(--muted);font-size:11px}
b{color:var(--cash);font-weight:600}
td .pos,.pos{color:var(--cash)}
td .neg,.neg{color:var(--red)}
tr.al-CRITICAL td{border-left:2px solid var(--red);background:rgba(255,95,109,.06)}
tr.al-WARNING td{border-left:2px solid var(--amber)}
tr.al-INFO td{border-left:2px solid var(--green)}
tr.al-CRITICAL.unacked td:last-child,tr.unacked b{color:var(--red);animation:blink 1.5s step-end infinite}
@keyframes blink{50%{opacity:.35}}
footer{max-width:1680px;margin:0 auto;padding:0 20px 24px;color:var(--dim);font-size:10.5px;letter-spacing:.16em;
  display:flex;gap:18px;flex-wrap:wrap;text-transform:uppercase}

/* ------------------------------------------------------------------- density */
body[data-density="compact"] main{padding:9px 14px 18px;gap:8px}
body[data-density="compact"] section{padding:8px 10px 10px}
body[data-density="compact"] h2{margin-bottom:6px}
body[data-density="compact"] table{font-size:11px}
body[data-density="compact"] td,body[data-density="compact"] th{padding:2px 6px}
body[data-density="compact"] .vitals{grid-template-columns:repeat(auto-fit,minmax(146px,1fr))}
body[data-density="compact"] .vital{padding:6px 11px}
body[data-density="compact"] .vital .v{font-size:17px;margin-top:1px}
body[data-density="compact"] .vital .v.sm{font-size:12.5px;padding-top:2px}
body[data-density="dense"] main{padding:6px 10px 14px;gap:6px}
body[data-density="dense"] section{padding:6px 8px 7px}
body[data-density="dense"] h2{margin-bottom:4px;font-size:9.5px}
body[data-density="dense"] table{font-size:10.5px}
body[data-density="dense"] td,body[data-density="dense"] th{padding:1px 5px}
body[data-density="dense"] small{font-size:9.5px}
body[data-density="dense"] .vitals{grid-template-columns:repeat(auto-fit,minmax(124px,1fr))}
body[data-density="dense"] .vital{padding:4px 9px}
body[data-density="dense"] .vital .k{font-size:8.5px;letter-spacing:.16em}
body[data-density="dense"] .vital .v{font-size:14px;margin-top:0}
body[data-density="dense"] .vital .v.sm{font-size:11px;padding-top:2px}

@media (max-width:1180px){section{grid-column:span 6}section.w8,section.w12{grid-column:span 12}}
@media (max-width:820px){main{grid-template-columns:1fr;padding:12px}
  section,section.w3,section.w6,section.w8,section.w12{grid-column:1/-1}
  .wordmark{letter-spacing:.24em;font-size:16px}
  .ctrls{order:9}}
</style>
<header>
  <div class="brand">
    <div class="core" id="core"><div class="ring"></div><div class="ring b"></div><div class="burst"></div><div class="pip"></div></div>
    <div class="wordmark">ARENA<small>COMMAND CONSOLE · 3.0.5</small></div>
  </div>
  <div class="badges">
    <span class="badge" id="mode">__MODE_BADGE__</span>
    <span class="__LIVE_CLASS__" id="live">LIVE: __LIVE_BADGE__</span>
    <span class="badge" id="conn">…</span>
  </div>
  <span class="spacer"></span>
  <span id="flow-chip"></span>
  <div class="clock" id="clock">—</div>
  <div class="ctrls">
    <button class="chip" id="c-glow" title="Cycle green glow intensity">glow <b>std</b></button>
    <button class="chip" id="c-dens" title="Cycle panel density">density <b>normal</b></button>
  </div>
  <button class="stop" id="stop">⬢ EMERGENCY STOP</button>
  <span id="err"></span>
</header>
<div class="vitals" id="vitals">
  <div class="vital"><div class="k">Equity</div><div class="v" id="v-equity">—</div></div>
  <div class="vital"><div class="k">Realized P&amp;L</div><div class="v" id="v-pnl">—</div></div>
  <div class="vital"><div class="k">Drawdown</div><div class="v" id="v-dd">—</div></div>
  <div class="vital"><div class="k">Open positions</div><div class="v" id="v-pos">—</div></div>
  <div class="vital"><div class="k">Unacked CRITICAL</div><div class="v" id="v-crit">—</div></div>
  <div class="vital"><div class="k">Engine lock</div><div class="v sm" id="v-lock">—</div></div>
</div>
<main>
<section class="w4"><h2>Account</h2><table id="account"></table></section>
<section class="w4"><h2>Feed / Market</h2><table id="feed"></table></section>
<section class="w4"><h2>Strategy &amp; Signals</h2><table id="strategy"></table></section>
<section class="w6"><h2>Positions &amp; Sandbox Orders</h2><table id="orders"></table></section>
<section class="w3"><h2>Learning / Model</h2><table id="learning"></table></section>
<section class="w3"><h2>Execution (3.0.5)</h2><table id="execution"></table></section>
<section class="w8"><h2>Alerts &amp; Live Ops (V2.1)</h2><table id="alerts"></table></section>
<section class="w4"><h2>Strategy Health (3.0.5)</h2><table id="health"></table></section>
<section class="w6"><h2>Market Events</h2><table id="market_events"></table></section>
<section class="w6"><h2>Market Context (3.0.5)</h2><table id="market_context"></table></section>
<section class="w6"><h2>Quality (3.0.5)</h2><table id="quality"></table></section>
<section class="w6"><h2>Recent Events</h2><table id="events"></table></section>
</main>
<footer>
  <span>Arena 3.0.5</span><span id="f-mode">—</span><span id="f-src">data: recorded state</span>
  <span>poll 5s</span><span id="f-warn"></span>
</footer>
<script>
const token = sessionStorage.getItem('arena_token') || prompt('API token:');
const esc = (s) => String(s ?? '—').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').slice(0,300);
if (token) sessionStorage.setItem('arena_token', token);
const H = {'Authorization':'Bearer ' + token};
const $ = (id) => document.getElementById(id);
function clock(){
  const d = new Date();
  const p = (n) => String(n).padStart(2,'0');
  $('clock').innerHTML = '<b>' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds()) + '</b>'
    + ' · ' + d.toISOString().slice(0,10);
}
clock(); setInterval(clock, 1000);

/* ---------------------------------------------------------------- appearance */
/* Both dials are pure presentation and persist locally. They change CSS custom
   properties / a body attribute only — never any trading state. */
const GLOWS = [['calm','lo'],['std','std'],['vivid','hi']];
const DENS  = [['comfortable','normal'],['compact','compact'],['dense','dense']];
function cycle(list, key, attr, store, dflt){
  let i = list.findIndex(x => x[0] === (localStorage.getItem(store) || dflt));
  if (i < 0) i = 0;
  return () => {
    i = (i + 1) % list.length;
    localStorage.setItem(store, list[i][0]);
    document.body.setAttribute(attr, list[i][0]);
    $(key).innerHTML = $(key).innerHTML.replace(/<b>.*<\/b>/, '<b>' + list[i][1] + '</b>');
  };
}
(function applyAppearance(){
  const g = localStorage.getItem('arena_glow') || 'std';
  const d = localStorage.getItem('arena_density') || 'comfortable';
  document.body.setAttribute('data-glow', g);
  document.body.setAttribute('data-density', d);
  const gl = GLOWS.find(x => x[0] === g) || GLOWS[1];
  const dn = DENS.find(x => x[0] === d) || DENS[0];
  $('c-glow').innerHTML = 'glow <b>' + gl[1] + '</b>';
  $('c-dens').innerHTML = 'density <b>' + dn[1] + '</b>';
  $('c-glow').onclick = cycle(GLOWS, 'c-glow', 'data-glow', 'arena_glow', 'std');
  $('c-dens').onclick = cycle(DENS, 'c-dens', 'data-density', 'arena_density', 'comfortable');
})();

/* ------------------------------------------------------------------ order flow
   The core is driven by what the poll ACTUALLY observed changing, not by a
   decorative timer: new orders / fills accelerate the rings and fire a
   shockwave, and a ticker names the change. If nothing moved, the core idles. */
let prevFlow = null, flowTimer = null, burstTimer = null;
function flowCounts(d){
  const lo = d.live_ops || {}, ca = lo.canary || {};
  return {
    orders: (d.sandbox_orders || []).length + (d.live_orders || []).length,
    canary: ca.submitted || 0,
    open: d.open_orders || 0,
    events: (d.system_events || []).length
  };
}
function fireFlow(label){
  const core = $('core');
  core.classList.remove('flow');
  void core.offsetWidth;            // force reflow so the animation restarts
  core.classList.add('flow');
  clearTimeout(flowTimer);
  flowTimer = setTimeout(() => core.classList.remove('flow'), 1400);
  const chip = $('flow-chip');
  chip.textContent = label;
  chip.classList.remove('on');
  void chip.offsetWidth;
  chip.classList.add('on');
  clearTimeout(burstTimer);
  burstTimer = setTimeout(() => chip.classList.remove('on'), 4000);
}
function coreState(d){
  const core = $('core');
  const lo = d.live_ops || {};
  core.classList.toggle('halt', !!d.emergency_stop);
  core.classList.toggle('alert', !d.emergency_stop && (lo.unacknowledged_critical || 0) > 0);
  core.classList.toggle('live', !!d.live_trading && !d.emergency_stop);
}
function flow(d){
  const now = flowCounts(d);
  if (prevFlow) {
    const dOrders = now.orders - prevFlow.orders;
    const dCanary = now.canary - prevFlow.canary;
    const dOpen   = now.open - prevFlow.open;
    const dEvents = now.events - prevFlow.events;
    if (dOrders > 0 || dCanary > 0) {
      fireFlow('+' + (Math.max(dOrders, 0) + Math.max(dCanary, 0)) + ' order');
    } else if (dOpen > 0) {
      fireFlow('+' + dOpen + ' open');
    } else if (dOpen < 0) {
      fireFlow(dOpen + ' open');
    } else if (dOrders < 0) {
      fireFlow(dOrders + ' closed');
    } else if (dEvents > 0) {
      fireFlow('+' + dEvents + ' event');
    }
  }
  prevFlow = now;
}

/* ---------------------------------------------------------------------- data */
function vitals(d){
  const lo = d.live_ops || {};
  $('v-equity').textContent = d.equity == null ? '—' : d.equity;
  $('v-pnl').textContent = fmt(d.realized_pnl);
  $('v-pnl').className = 'v ' + (d.realized_pnl == null ? '' : sign(d.realized_pnl));
  $('v-dd').textContent = pct(d.drawdown);
  $('v-pos').textContent = (d.positions || []).length;
  const crit = lo.unacknowledged_critical || 0;
  $('v-crit').textContent = crit;
  $('v-crit').className = 'v' + (crit > 0 ? ' neg' : '');
  const lk = lo.lock;
  $('v-lock').textContent = (lk && lk.held)
    ? (lk.owner || 'held') + (lk.stale ? ' · STALE' : ' · live')
    : 'not held';
  $('v-lock').className = 'v sm' + (lk && lk.held && lk.stale ? ' warnc' : '');
  $('f-mode').textContent = (d.display || '') + ' · ' + (d.broker_mode || '');
  const warn = d.live_ops && d.live_ops.order_host_is_documented_live === false
    ? 'ORDER HOST NOT A LIVE HOST' : '';
  $('f-warn').textContent = warn;
  $('stop').classList.toggle('armed', !!d.emergency_stop === false && !!d.live_trading);
}
async function tick(){
  try{
    const r = await fetch('/dashboard/data',{headers:H});
    if(r.status!==200){$('err').textContent='auth failed ('+r.status+')';return;}
    const d = await r.json();
    $('mode').textContent = d.display;
    const liveBadge = $('live');
    liveBadge.textContent = 'LIVE: ' + d.live_enabled;
    liveBadge.className = d.live_trading ? 'badge' : 'badge warn';
    $('conn').textContent = 'connection: ' + (d.connection ?? 'n/a')
      + (d.market_status ? ' · market: ' + d.market_status : '');
    const row = (k,v,cls)=>'<tr><td>'+k+'</td><td class="num '+(cls||'')+'">'+v+'</td></tr>';
    $('account').innerHTML =
      row('Equity', d.equity ?? '—') + row('Realized P&L', fmt(d.realized_pnl), sign(d.realized_pnl))
      + row('Drawdown', pct(d.drawdown)) + row('Positions', (d.positions||[]).length);
    $('feed').innerHTML =
      row('Pipeline', d.pipeline) + row('Market status', d.market_status ?? 'n/a')
      + row('Emergency stop', d.emergency_stop ? 'ACTIVE' : 'no', d.emergency_stop?'neg':'pos');
    $('strategy').innerHTML =
      row('Strategy', d.strategy ?? '—') + row('Regime', d.regime ?? '—')
      + Object.entries(d.signals||{}).map(([s,x])=>row('signal '+s, x.signal)).join('');
    const orders = d.sandbox_orders || [];
    $('orders').innerHTML = (d.positions||[]).map(p=>
      row('position', p.quantity+' @ '+(p.avg_price??p.entry_price))).join('')
      + orders.slice(0,8).map(o=>row(o.side+' '+o.quantity, o.status));
    const mv = d.model_version;
    $('learning').innerHTML =
      row('Promoted model', mv ? mv.version+' '+JSON.stringify(mv.parameters) : 'baseline')
      + row('Mode', d.broker_mode);
    $('events').innerHTML = (d.system_events||[]).slice(0,8).map(e=>
      '<tr><td>'+e.ts.slice(11,19)+'</td><td>'+e.kind+'</td></tr>').join('');
    // Alerts + operational rails (V2.1 L-ops)
    (() => {
      const lo = d.live_ops || null;
      const rows = [];
      if (lo) {
        const tk = lo.token || {}, ca = lo.canary || {};
        rows.push('<tr><td><b>Execution</b></td><td>'
          + (lo.live_trading ? '<span class="neg">LIVE — REAL MONEY</span>' : 'not live')
          + ' · reconcile: ' + (lo.last_reconcile_at ? String(lo.last_reconcile_at.ts).slice(11,19) : 'never') + '</td></tr>');
        rows.push('<tr><td>Token</td><td>'
          + ((tk.decision && tk.decision.reason) || '—')
          + (tk.expires_in_minutes != null ? ' · expires in ' + tk.expires_in_minutes + ' min' : '')
          + '</td></tr>');
        if (ca.enabled) rows.push('<tr><td>Canary</td><td>'
          + (ca.submitted||0) + '/' + ca.first_orders + ' entries · cap ' + ca.max_notional
          + ' · ' + (ca.complete ? 'complete' : ca.remaining + ' remaining') + '</td></tr>');
        if (lo.breaches) rows.push('<tr><td>Kill switches</td><td>daily loss '
          + lo.breaches.max_daily_loss + ' · drawdown '
          + (lo.breaches.max_equity_drawdown || 'off')
          + ' · auto-stop ' + (lo.breaches.auto_stop ? 'ON' : 'OFF') + '</td></tr>');
        rows.push('<tr><td>Open live orders</td><td>' + (lo.open_live_orders||0) + '</td></tr>');
        rows.push('<tr><td>Unacknowledged CRITICAL alerts</td><td>'
          + '<b>' + (lo.unacknowledged_critical||0) + '</b>'
          + ' <small>(live-ack --all)</small></td></tr>');
        if (lo.lock && lo.lock.held) {
          rows.push('<tr><td>Engine lock</td><td>' + esc(lo.lock.owner || '?')
            + (lo.lock.stale ? ' <b>(STALE)</b>' : ' (heartbeating)')
            + ' <small>' + (lo.lock.heartbeat_age_seconds==null ? ''
                            : esc(String(lo.lock.heartbeat_age_seconds)) + 's ago')
            + '</small></td></tr>');
        } else {
          rows.push('<tr><td>Engine lock</td><td>not held</td></tr>');
        }
      }
      const al = d.alerts || [];
      if (al.length === 0) {
        rows.push('<tr><td>No alerts recorded</td></tr>');
      } else {
        rows.push('<tr><th>When</th><th>Severity / alert</th></tr>');
        for (const a of al.slice(0,8)) {
          const unacked = (a.severity === 'CRITICAL' && !a.acknowledged);
          rows.push('<tr class="al-' + esc(a.severity) + (unacked ? ' unacked' : '') + '"><td>'
            + esc(a.ts).slice(11,19) + '</td><td>'
            + esc(a.severity) + ' · <b>' + esc(a.kind) + '</b>'
            + (unacked ? ' <b>[UNACKNOWLEDGED]</b>' : '')
            + '<br><small>' + esc(a.message) + '</small></td></tr>');
        }
      }
      $('alerts').innerHTML = rows.join('');
    })();
    // Market Events panel — EVENT, WHY IT MAY MATTER, AFFECTED MARKET, CONFIDENCE, ARENA RESPONSE, ACTION TAKEN, RESULT
    const me = d.market_events || [];
    if (me.length === 0) {
      $('market_events').innerHTML = '<tr><td>No market events recorded</td></tr>';
    } else {
      $('market_events').innerHTML = me.slice(0,8).map(ev =>
        '<tr><td><b>'+esc(ev.headline)+'</b><br><small>'+esc(ev.expected_market_effect||ev.summary||'')+'</small></td></tr>'
        + '<tr><td>WHY: '+esc(ev.expected_market_effect)+' | MARKET: '+(ev.affected_markets||[]).join(',')+' | SECTORS: '+(ev.affected_sectors||[]).join(',')+'</td></tr>'
        + '<tr><td>CONF: '+esc(ev.confidence)+' | RESPONSE: '+esc(ev.arena_response)+' | ACTION: '+esc(ev.action_taken)+' | RESULT: '+esc(ev.result_after_event?JSON.stringify(ev.result_after_event).slice(0,120):'pending')+'</td></tr>'
      ).join('');
    }
    // 3.0.5 QUALITY panel
    const qe = d.quality_events || [];
    $('quality').innerHTML = qe.length? qe.map(e=>'<tr><td>'+esc(e.kind)+'</td><td>'+esc(JSON.stringify(e.payload).slice(0,120))+'</td></tr>').join('') : '<tr><td>No quality events</td></tr>';
    // MARKET CONTEXT
    const mc = d.market_context || {};
    $('market_context').innerHTML = mc ? Object.entries(mc).slice(0,6).map(([k,v])=>row(k, esc(typeof v==='object'?JSON.stringify(v).slice(0,80):v))).join('') : '<tr><td>No context</td></tr>';
    // EXECUTION
    const ex = d.execution_quality || {};
    const hot = ex.hot_path || {};
    $('execution').innerHTML = row('Hot p95', hot.p95_ms ?? '—') + row('Hot max', hot.max_latency_ms ?? '—') + row('Hot avg', hot.avg_latency_ms ?? '—') + (ex.liquidity? ex.liquidity.slice(0,2).map(e=>row(e.kind, esc(JSON.stringify(e.payload).slice(0,80)))).join('') : '');
    // STRATEGY HEALTH
    const sh = d.strategy_health || {};
    $('health').innerHTML = sh && Object.keys(sh).length ? Object.entries(sh).slice(0,6).map(([k,v])=>row(k, esc(typeof v==='object'?JSON.stringify(v).slice(0,80):v))).join('') : '<tr><td>No health data</td></tr>';
    coreState(d);
    flow(d);
    vitals(d);
  }catch(e){$('err').textContent=String(e).slice(0,80);}
}
function fmt(v){return (v==null)?'—':Number(v).toFixed(2)}
function pct(v){return (v==null)?'—':(100*v).toFixed(2)+'%'}
function sign(v){return v==null?'':(v<0?'neg':'pos')}
$('err').onclick = () => {
  const t = prompt('API token:');
  if (t) { sessionStorage.setItem('arena_token', t); tick(); }
};
$('stop').onclick = async () => {
  if(!confirm('EMERGENCY STOP: halt trading and cancel open sandbox orders?'))return;
  await fetch('/engine/stop',{method:'POST',headers:H});
  tick();
};
tick(); setInterval(tick, 5000);
</script>"""
    return HTMLResponse(
        page.replace("__MODE_BADGE__", mode_badge)
            .replace("__LIVE_CLASS__", live_class)
            .replace("__LIVE_BADGE__", live_badge))
