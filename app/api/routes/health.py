from app import __version__
from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health():
    """Public process health, never current account or activation state.

    The legacy engine remains configured as local paper. Vantage is a separate
    authenticated in-app session and always starts disconnected/disarmed.
    """
    return {
        "status": "ok",
        "version": __version__,
        "mode": "PAPER",
        "live_trading": False,
        "real_order_routing": "VANTAGE_MT5_GUARDED",
        "vantage_startup": "DISCONNECTED_DISARMED",
        "profitability_claim": "none",
    }


@router.get("/", include_in_schema=False)
def index():
    """Mode-neutral entry page; account data remains behind authentication."""
    from fastapi.responses import HTMLResponse

    return HTMLResponse(f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AXIOM Operator</title><style>
body{{margin:0;background:#081018;color:#edf4fb;font:16px/1.6 system-ui,sans-serif}}
main{{max-width:780px;margin:8vh auto;padding:32px}}h1{{font-size:40px;line-height:1.2}}
.badge{{color:#66d9c2;border:1px solid #3b735e;padding:6px 12px;border-radius:8px}}
a{{color:#77b9ff}}section{{margin-top:30px;border-top:1px solid #26384a;padding-top:20px}}
small{{color:#9cafc1}}code{{color:#66d9c2}}.primary{{display:inline-block;background:#20b894;color:#041c17;padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:700}}
</style><main><span class="badge">AXIOM {__version__} · PAPER DEFAULT · VANTAGE DISARMED</span>
<h1>AXIOM Operator</h1><p>Local research and simulated paper execution, plus a separately authenticated Vantage MT5 session with reviewed, guarded demo/live capability.</p>
<p><a class="primary" href="/terminal">Open operator console →</a></p>
<p><a href="/manual">User manual</a> · <a href="/chart">Legacy paper chart</a> · <a href="/dashboard">Legacy research dashboard</a> · <a href="/health">API health</a> · <a href="/docs">API docs</a></p>
<section><h2>Safe by default</h2><p>Startup, recovery and credential entry never connect, Arm or Activate Vantage. Local paper fills are simulated and prominently labeled. CFD products do not represent share ownership.</p>
<p>The private local <code>API_TOKEN</code> protects account APIs and is separate from MT5 and data-provider credentials. Empty token configuration disables protected access.</p></section>
<section><small>Tests and demo fills establish neither profitability nor live readiness. Product availability and execution must be verified on the operator's own account. Historical results are not guarantees of future performance.</small></section>
</main></html>""")
