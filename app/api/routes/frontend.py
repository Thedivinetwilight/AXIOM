"""The redesigned frontend: static assets + application shells + boot API.

3.1.0 redesign lesson, applied structurally:

* **No JavaScript is ever templated through Python strings.** The browser
  applications live in ``app/static/*.js`` as real files, served byte-for-byte.
  A Python string can corrupt quotes/escapes (the 3.0.5 setup-page incident);
  a static file cannot. The page shells below contain ZERO inline ``<script>``
  bodies — only ``<script src>`` references — so there is no inline code that
  could ever fail to parse.
* **Cache busting**: asset URLs carry ``?v=<version>-<commit>``, so an upgraded
  Arena can never render a stale cached bundle, and every screen shows the same
  build identifier the backend reports.
* **Boot configuration** is JSON from ``/api/boot`` — no secrets, only what a
  loopback browser already knows by being on this machine.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app import build_info
from app.api.auth import loopback_or_bearer
from app.runtime import static_dir

router = APIRouter()

# Only these files may ever be served from the static directory; anything else
# is a 404. (No directory traversal, no arbitrary reads.)
ALLOWED_STATIC = {
    "arena.css": "text/css; charset=utf-8",
    "arena-core.js": "text/javascript; charset=utf-8",
    "arena-wizard.js": "text/javascript; charset=utf-8",
    "arena-app.js": "text/javascript; charset=utf-8",
}

_VERSIONED_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{title}</title>
<link rel="stylesheet" href="/static/arena.css?v={build}">
</head>
<body>
<div id="{root}" style="max-width:1120px;margin:0 auto;padding:28px 20px">
  <noscript>
    <div style="border:1px solid #2e3950;border-radius:10px;padding:18px;font-family:sans-serif;color:#e6ebf4;background:#121826">
      <b>Arena needs JavaScript enabled in this browser.</b><br>
      The interface is served locally by Arena itself; enabling scripts for
      127.0.0.1 is safe and required.
    </div>
  </noscript>
  <div style="color:#7d89a3;font-family:sans-serif;padding:40px 0">Loading Arena…</div>
</div>
<script src="/static/arena-core.js?v={build}"></script>
<script src="/static/{app}.js?v={build}"></script>
</body>
</html>
"""


def _shell(title: str, app: str, root: str) -> str:
    return _VERSIONED_SHELL.format(
        title=title, app=app, root=root, build=build_info.build_id())


@router.get("/static/{name}", include_in_schema=False)
async def static_asset(name: str) -> Response:
    media_type = ALLOWED_STATIC.get(name)
    if media_type is None:
        raise HTTPException(status_code=404, detail="Unknown static asset")
    path: Path = static_dir() / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Static asset missing from build")
    data = path.read_bytes()
    headers = {
        # The URL is version-fingerprinted, so a hit here is exactly the bytes
        # this build intends; long caching is safe and keeps the UI snappy.
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Arena-Build": build_info.build_id(),
    }
    return Response(content=data, media_type=media_type, headers=headers)


@router.get("/app", response_class=HTMLResponse,
            dependencies=[Depends(loopback_or_bearer)])
async def app_page() -> HTMLResponse:
    """The main application shell (command center and all panels)."""
    return HTMLResponse(_shell("Arena — Command Center", "arena-app", "app-root"))


@router.get("/api/boot")
async def boot(request: Request) -> dict:
    """Everything the frontend needs to initialize. NEVER contains secrets:
    no tokens, no credentials, no webhook URLs — only identity and mode."""
    from app import runtime

    settings = getattr(request.app.state, "settings", None)
    broker = (getattr(settings, "broker", "LOCAL_PAPER") or "LOCAL_PAPER").upper()
    live = bool(getattr(settings, "live_trading", False)) and broker == "UPSTOX_LIVE"
    home = runtime.arena_home()
    database = getattr(settings, "database_url", "")
    first_run = not runtime.env_file().exists()
    if not first_run:
        from app.config.envfile import read_values

        try:
            first_run = not (read_values(runtime.env_file()).get("API_TOKEN") or "").strip()
        except OSError:
            first_run = True
    if live:
        display = f"{broker} · LIVE — REAL MONEY"
    elif broker == "UPSTOX_SANDBOX":
        display = f"{broker} · SANDBOX ORDERS"
    else:
        display = f"{broker} · SIMULATED (PAPER)"
    return {
        **build_info.describe(),
        "mode": (getattr(settings, "mode", "PAPER") or "PAPER").upper(),
        "mode_display": display,
        "broker": broker,
        "live_trading": live,
        "data_source": getattr(settings, "data_source", "replay"),
        "first_run": first_run,
        "arena_home": str(home),
        "env_file": str(runtime.env_file()),
        "database": database,
        "settings": {
            "initial_capital": getattr(settings, "initial_capital", None),
            "default_symbol": getattr(settings, "default_symbol", None),
            "default_timeframe": getattr(settings, "default_timeframe", None),
            "currency": getattr(settings, "currency", None),
        },
    }
