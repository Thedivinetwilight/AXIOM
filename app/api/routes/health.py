from app import __version__
from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health():
    return {
        "status": "ok",
        "version": __version__,
        "mode": "PAPER",
        "live_trading": False,
        "real_order_routing": "NOT_IMPLEMENTED",
    }


@router.get("/", include_in_schema=False)
def index():
    """Small public landing page; account data remains behind authentication."""
    from fastapi.responses import HTMLResponse

    return HTMLResponse(f"""<!doctype html><html lang="en\"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arena — Quantitative Research &amp; Trading Platform</title><style>
body{{margin:0;background:#101c2a;color:#eef5ff;font:16px/1.6 system-ui,sans-serif}}
main{{max-width:740px;margin:10vh auto;padding:32px}}h1{{font-size:40px;line-height:1.2}}
.badge{{color:#75e7b2;border:1px solid #3b735e;padding:6px 12px;border-radius:20px}}
a{{color:#8dd7ff}}section{{margin-top:32px;border-top:1px solid #344557;padding-top:20px}}
small{{color:#bac9d9}}code{{color:#75e7b2}}
</style><main><span class="badge">Arena {__version__} · PAPER ONLY</span>
<h1>Arena</h1><p>Historical research, chronological validation, and simulated execution.
Real-money order routing is not implemented.</p>
<p><a href="/chart">Open the chart &amp; trade terminal →</a> &nbsp;
<a href="/dashboard">Dashboard →</a> &nbsp;
<a href="/health">API health →</a> &nbsp; <a href="/docs">API docs →</a></p>
<section><h2>Safe by default</h2><p><code>MODE=PAPER</code> · <code>LIVE_TRADING=false</code></p>
<p>Set your own private <code>API_TOKEN</code> in your local environment to enable protected endpoints.
An empty token disables account access and bot controls.</p></section>
<section><small>The project archive includes source, 471 tests, synthetic example data, verification reports,
and PC, Termux and VPS setup instructions. Historical results are not guarantees of future performance.</small></section>
</main></html>""")
