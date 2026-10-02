from app import __version__
from fastapi import APIRouter, Request

router = APIRouter()


def _routing_status(settings) -> str:
    """Truthful routing status derived from the ACTUAL configured broker.

    V1 hardcoded "NOT_IMPLEMENTED" here, which was false: the sandbox broker
    talks to real order APIs and the live broker routes real money.
    """
    broker = (getattr(settings, "broker", "LOCAL_PAPER") or "").upper()
    if broker == "UPSTOX_LIVE":
        return "LIVE_REAL_MONEY"
    if broker == "UPSTOX_SANDBOX":
        return "SANDBOX_ORDERS_ONLY"
    return "SIMULATED_LOCAL_PAPER"


@router.get("/health")
def health(request: Request):
    settings = getattr(request.app.state, "settings", None)
    broker = (getattr(settings, "broker", "LOCAL_PAPER") or "LOCAL_PAPER").upper()
    live = bool(getattr(settings, "live_trading", False)) and broker == "UPSTOX_LIVE"
    return {
        "status": "ok",
        "version": __version__,
        "mode": (getattr(settings, "mode", "PAPER") or "PAPER").upper(),
        "live_trading": live,
        "broker": broker,
        "real_order_routing": _routing_status(settings),
        "warning": "REAL MONEY ROUTING ACTIVE" if live else None,
    }


@router.get("/", include_in_schema=False)
def index(request: Request):
    """Small public landing page; account data remains behind authentication."""
    from fastapi.responses import HTMLResponse

    settings = getattr(request.app.state, "settings", None)
    broker = (getattr(settings, "broker", "LOCAL_PAPER") or "LOCAL_PAPER").upper()
    live = bool(getattr(settings, "live_trading", False)) and broker == "UPSTOX_LIVE"
    if live:
        badge = f"Arena {__version__} · LIVE — REAL MONEY"
        headline = ("<strong>Real-money order routing is ACTIVE.</strong> Orders are "
                    "placed on the documented live Upstox order host.")
        safety = ("<p><code>MODE=LIVE</code> · <code>LIVE_TRADING=true</code> · "
                  f"<code>BROKER={broker}</code></p>")
    elif broker == "UPSTOX_SANDBOX":
        badge = f"Arena {__version__} · SANDBOX ORDERS"
        headline = ("Simulated execution. Orders are placed on Upstox's sandbox order "
                    "API — no real money moves.")
        safety = ("<p><code>MODE=PAPER</code> · <code>LIVE_TRADING=false</code> · "
                  f"<code>BROKER={broker}</code></p>")
    else:
        badge = f"Arena {__version__} · PAPER ONLY"
        headline = ("Historical research, chronological validation, and simulated "
                    "execution. Real-money order routing is not enabled.")
        safety = ("<p><code>MODE=PAPER</code> · <code>LIVE_TRADING=false</code> · "
                  f"<code>BROKER={broker}</code></p>")
    return HTMLResponse(f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arena — Quantitative Research &amp; Trading Platform</title><style>
body{{margin:0;background:#101c2a;color:#eef5ff;font:16px/1.6 system-ui,sans-serif}}
main{{max-width:740px;margin:10vh auto;padding:32px}}h1{{font-size:40px;line-height:1.2}}
.badge{{color:#75e7b2;border:1px solid #3b735e;padding:6px 12px;border-radius:20px}}
a{{color:#8dd7ff}}section{{margin-top:32px;border-top:1px solid #344557;padding-top:20px}}
small{{color:#bac9d9}}code{{color:#75e7b2}}
</style><main><span class="badge">{badge}</span>
<h1>Arena</h1><p>{headline}</p>
<p><a href="/health">Check API health →</a> &nbsp; <a href="/docs">Explore API documentation →</a></p>
<section><h2>Execution mode</h2>{safety}
<p>Set your own private <code>API_TOKEN</code> in your local environment to enable protected endpoints.
An empty token disables account access and bot controls.</p></section>
<section><small>The project archive includes source, the test suite, synthetic example data, verification reports,
and PC, Termux and VPS setup instructions. Historical results are not guarantees of future performance.</small></section>
</main></html>""")
