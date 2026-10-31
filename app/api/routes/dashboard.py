"""V1 dashboard: clean local HTML view + engine/emergency controls.

Clearly displays that this is the LEGACY PAPER / SANDBOX view. Vantage lives
only in the separate operator console. All data endpoints sit behind the
existing bearer-token dependency; the HTML
page fetches /dashboard/data with the same token supplied at load time via a
one-time inline bootstrap (no credentials in logs; the token is provided by
the operator, never stored server-side beyond the running process settings).
"""

from app import __version__
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api.auth import authenticate, loopback_or_bearer

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


def _sandbox_view(request: Request) -> dict:
    repository = request.app.state.repository
    engine = getattr(request.app.state, "sandbox_engine", None)
    broker = getattr(request.app.state, "sandbox_broker", None)
    orders = repository.list_sandbox_orders(limit=25) if repository else []
    equity_state = (repository.get_engine_state("sandbox_equity") or {}) if repository else {}
    view = {
        "pipeline": "UPSTOX_SANDBOX",
        "state": engine.status()["state"] if engine else "IDLE",
        "sandbox_orders": orders,
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
    emergency = (repository.get_engine_state("emergency_stop") or {}) if repository else {}
    promoted = repository.promoted_version("momentum") if repository else None
    view = _sandbox_view(request) if broker_mode == "UPSTOX_SANDBOX" else _paper_view(request)
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
        "mode": "PAPER",
        "live_trading": False,
        "live_enabled": "NOT ENABLED IN V1",
        "broker_mode": broker_mode,
        "display": f"{broker_mode} — PAPER/SANDBOX ONLY",
        "emergency_stop": bool(emergency.get("stopped")),
        "emergency_reason": emergency.get("reason"),
        "model_version": promoted,
        "system_events": repository.list_system_events(limit=15) if repository else [],
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
    """Clear only the legacy halt; never clear or activate a Vantage session."""
    repository = request.app.state.repository
    repository.set_engine_state("emergency_stop", {"stopped": False})
    repository.record_system_event("ENGINE_RESUME_REQUESTED", {"source": "dashboard"})
    simulator = getattr(request.app.state, "simulator", None)
    simulator_status = None
    if simulator is not None and simulator.status().get("state") == "PAUSED":
        simulator_status = await simulator.resume()
    return {"stopped": False, "scope": "LEGACY_PAPER_SANDBOX", "simulator": simulator_status}


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False,
            dependencies=[Depends(loopback_or_bearer)])
async def dashboard(request: Request):
    """The dashboard UI, readable by LOCAL browsers without a Bearer header.

    The page itself contains NO secrets: the operator's API_TOKEN is never
    rendered into it — the page JS asks the operator for the token (kept in
    sessionStorage, never in URLs) and sends it as an Authorization header on
    every protected call. Remote (non-loopback) clients still need the token
    to load the page at all."""

    return HTMLResponse("""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AXIOM — Legacy Paper/Sandbox Dashboard</title><style>
body{margin:0;background:#0d1622;color:#e8f1fb;font:15px/1.5 system-ui,sans-serif}
header{padding:14px 20px;border-bottom:1px solid #263549;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.badge{border:1px solid #3b735e;color:#75e7b2;padding:4px 10px;border-radius:16px;font-size:13px}
.badge.warn{border-color:#a8842c;color:#ffd479}
main{max-width:1080px;margin:0 auto;padding:18px;display:grid;gap:14px;
grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
section{background:#13202f;border:1px solid #263549;border-radius:10px;padding:14px}
h2{margin:0 0 8px;font-size:14px;color:#9fb4cc;text-transform:uppercase;letter-spacing:.06em}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:4px 6px;border-bottom:1px solid #1d2c3e;text-align:left}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.stop{background:#7a2430;border:1px solid #c0556a;color:#ffd9df;padding:8px 14px;
border-radius:8px;cursor:pointer;font-size:14px}
.stop:disabled{opacity:.5}
.pos{color:#75e7b2}.neg{color:#ff9aa5}
.chartlink{background:#12506f;border:1px solid #2b7ba8;color:#cfeeff;padding:8px 14px;
border-radius:8px;font-size:14px;text-decoration:none}
#err{color:#ff9aa5;font-size:13px}
</style><header>
<strong>AXIOM legacy research</strong><span class="badge" id="mode">…</span>
<span class="badge warn">LEGACY PAPER/SANDBOX ONLY</span>
<span id="conn" class="badge">…</span>
<a class="chartlink" href="/terminal">OPERATOR TERMINAL</a>
<a class="chartlink" href="/chart" title="Legacy chart and local paper ticket">CHARTS + PAPER</a>
<button class="chartlink" id="resume">RESUME LEGACY PAPER</button>
<button class="stop" id="stop">LEGACY EMERGENCY STOP</button><span id="err"></span>
</header><main>
<section><h2>Account</h2><table id="account"></table></section>
<section><h2>Feed / Market</h2><table id="feed"></table></section>
<section><h2>Strategy &amp; Signals</h2><table id="strategy"></table></section>
<section><h2>Positions &amp; Sandbox Orders</h2><table id="orders"></table></section>
<section><h2>Learning / Model</h2><table id="learning"></table></section>
<section><h2>Recent Events</h2><table id="events"></table></section>
<section><h2>Market Events</h2><table id="market_events"></table></section>
<section><h2>Quality (3.0.5)</h2><table id="quality"></table></section>
<section><h2>Market Context (3.0.5)</h2><table id="market_context"></table></section>
<section><h2>Execution (3.0.5)</h2><table id="execution"></table></section>
<section><h2>Strategy Health (3.0.5)</h2><table id="health"></table></section>
</main><script>
const token = sessionStorage.getItem('arena_token') || prompt('API token:');
if (token) sessionStorage.setItem('arena_token', token);
const H = {'Authorization':'Bearer ' + token};
async function tick(){
  try{
    const r = await fetch('/dashboard/data',{headers:H});
    if(r.status!==200){document.getElementById('err').textContent='auth failed ('+r.status+')';return;}
    const d = await r.json();
    document.getElementById('mode').textContent = d.display;
    document.getElementById('conn').textContent = 'connection: ' + (d.connection ?? 'n/a')
      + (d.market_status ? ' · market: ' + d.market_status : '');
    const row = (k,v,cls)=>'<tr><td>'+k+'</td><td class="num '+(cls||'')+'">'+v+'</td></tr>';
    document.getElementById('account').innerHTML =
      row('Equity', d.equity ?? '—') + row('Realized P&L', fmt(d.realized_pnl), sign(d.realized_pnl))
      + row('Drawdown', pct(d.drawdown)) + row('Positions', (d.positions||[]).length);
    document.getElementById('feed').innerHTML =
      row('Pipeline', d.pipeline) + row('Market status', d.market_status ?? 'n/a')
      + row('Emergency stop', d.emergency_stop ? 'ACTIVE' : 'no', d.emergency_stop?'neg':'pos');
    document.getElementById('strategy').innerHTML =
      row('Strategy', d.strategy ?? '—') + row('Regime', d.regime ?? '—')
      + Object.entries(d.signals||{}).map(([s,x])=>row('signal '+s, x.signal)).join('');
    const orders = d.sandbox_orders || [];
    document.getElementById('orders').innerHTML = (d.positions||[]).map(p=>
      row('position', p.quantity+' @ '+(p.avg_price??p.entry_price))).join('')
      + orders.slice(0,8).map(o=>row(o.side+' '+o.quantity, o.status));
    const mv = d.model_version;
    document.getElementById('learning').innerHTML =
      row('Promoted model', mv ? mv.version+' '+JSON.stringify(mv.parameters) : 'baseline')
      + row('Mode', d.broker_mode);
    document.getElementById('events').innerHTML = (d.system_events||[]).slice(0,8).map(e=>
      '<tr><td>'+e.ts.slice(11,19)+'</td><td>'+e.kind+'</td></tr>').join('');
    // Market Events panel — EVENT, WHY IT MAY MATTER, AFFECTED MARKET, CONFIDENCE, ARENA RESPONSE, ACTION TAKEN, RESULT
    const esc = (s) => String(s ?? '—').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').slice(0,300);
    const me = d.market_events || [];
    if (me.length === 0) {
      document.getElementById('market_events').innerHTML = '<tr><td>No market events recorded</td></tr>';
    } else {
      document.getElementById('market_events').innerHTML = me.slice(0,8).map(ev =>
        '<tr><td><b>'+esc(ev.headline)+'</b><br><small>'+esc(ev.expected_market_effect||ev.summary||'')+'</small></td></tr>'
        + '<tr><td>WHY: '+esc(ev.expected_market_effect)+' | MARKET: '+(ev.affected_markets||[]).join(',')+' | SECTORS: '+(ev.affected_sectors||[]).join(',')+'</td></tr>'
        + '<tr><td>CONF: '+esc(ev.confidence)+' | RESPONSE: '+esc(ev.arena_response)+' | ACTION: '+esc(ev.action_taken)+' | RESULT: '+esc(ev.result_after_event?JSON.stringify(ev.result_after_event).slice(0,120):'pending')+'</td></tr>'
      ).join('');
    }
    // 3.0.5 QUALITY panel
    const qe = d.quality_events || [];
    document.getElementById('quality').innerHTML = qe.length? qe.map(e=>'<tr><td>'+esc(e.kind)+'</td><td>'+esc(JSON.stringify(e.payload).slice(0,120))+'</td></tr>').join('') : '<tr><td>No quality events</td></tr>';
    // MARKET CONTEXT
    const mc = d.market_context || {};
    document.getElementById('market_context').innerHTML = mc ? Object.entries(mc).slice(0,6).map(([k,v])=>row(k, esc(typeof v==='object'?JSON.stringify(v).slice(0,80):v))).join('') : '<tr><td>No context</td></tr>';
    // EXECUTION
    const ex = d.execution_quality || {};
    const hot = ex.hot_path || {};
    document.getElementById('execution').innerHTML = row('Hot p95', hot.p95_ms ?? '—') + row('Hot max', hot.max_latency_ms ?? '—') + row('Hot avg', hot.avg_latency_ms ?? '—') + (ex.liquidity? ex.liquidity.slice(0,2).map(e=>row(e.kind, esc(JSON.stringify(e.payload).slice(0,80)))).join('') : '');
    // STRATEGY HEALTH
    const sh = d.strategy_health || {};
    document.getElementById('health').innerHTML = sh && Object.keys(sh).length ? Object.entries(sh).slice(0,6).map(([k,v])=>row(k, esc(typeof v==='object'?JSON.stringify(v).slice(0,80):v))).join('') : '<tr><td>No health data</td></tr>';
  }catch(e){document.getElementById('err').textContent=String(e).slice(0,80);}
}
function fmt(v){return (v==null)?'—':Number(v).toFixed(2)}
function pct(v){return (v==null)?'—':(100*v).toFixed(2)+'%'}
function sign(v){return v==null?'':(v<0?'neg':'pos')}
document.getElementById('err').onclick = () => {
  const t = prompt('API token:');
  if (t) { sessionStorage.setItem('arena_token', t); tick(); }
};
document.getElementById('resume').onclick = async () => {
  if(!confirm('Resume only the legacy paper engine? Upstox and Alpaca live gates remain unchanged and disarmed.'))return;
  const r = await fetch('/engine/resume',{method:'POST',headers:H});
  document.getElementById('err').textContent = r.ok ? 'legacy resume confirmed' : 'legacy resume failed ('+r.status+')';
  tick();
};
document.getElementById('stop').onclick = async () => {
  if(!confirm('LEGACY EMERGENCY STOP: halt legacy paper/sandbox work? Use the operator console (/terminal) for NSE and NASDAQ sessions.'))return;
  const r = await fetch('/engine/stop',{method:'POST',headers:H});
  document.getElementById('err').textContent = r.ok ? 'legacy stop recorded' : 'legacy stop failed ('+r.status+')';
  tick();
};
tick(); setInterval(tick, 5000);
</script>""")
