"""First-run setup: enter credentials from the interface.

WHAT THIS WRITES
    Only the keys in `SETTABLE_KEYS`, merged into the operator's `.env` with
    everything else preserved. Saving is atomic and the file is owner-readable
    only.

WHAT THIS DELIBERATELY REFUSES TO WRITE
    The live-trading enablement switches. Arena's safety model is an explicit
    multi-switch opt-in with non-zero risk limits precisely so that no single
    control — a button, a checkbox, an environment variable — can start
    real-money trading. A credential screen that flipped those switches would
    be exactly the one-click path to live the design forbids, so `LIVE_TRADING`,
    a live `BROKER`/`MODE`, and every risk limit are rejected here with an
    explanation and a pointer to the guide.

WHO CAN REACH IT
    Loopback clients, or anyone presenting the bearer token — the same rule the
    dashboard page uses. On first run the launcher binds to 127.0.0.1, so setup
    is reachable only from the machine running it. If an operator binds the
    server to 0.0.0.0 without an API_TOKEN, remote saves are refused (503) just
    like every other protected route.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.api.auth import loopback_or_bearer
from app.config.envfile import masked_summary, read_values, update_values
from app.console import setup_page

router = APIRouter()

# The complete set of keys the setup screen may write.
SETTABLE_KEYS = {
    "API_TOKEN",
    "BROKER",
    "MODE",
    "MARKET_DATA_SOURCE",
    "MARKET_DATA_ACCESS_TOKEN",
    "MARKET_DATA_SYMBOLS",
    "UPSTOX_CLIENT_ID",
    "UPSTOX_CLIENT_SECRET",
    "UPSTOX_REDIRECT_URI",
    "UPSTOX_SANDBOX_ACCESS_TOKEN",
    "ALERT_WEBHOOK_URL",
    "DEFAULT_SYMBOL",
    "DEFAULT_TIMEFRAME",
}

# Keys that mean "trade real money". Refused here, deliberately.
LIVE_ENABLEMENT = {
    "LIVE_TRADING",
    "LIVE_ARMED",
    "LIVE_TOKEN_MAX_AGE_HOURS",
    "ALLOW_LIVE",
}

# Broker/mode values this screen refuses to select.
LIVE_VALUES = {
    "BROKER": {"UPSTOX_LIVE", "LIVE", "UPSTOX_LIVE_ENHANCED"},
    "MODE": {"LIVE"},
}

# Keys whose value is a credential: reported as present/absent, never returned.
SECRET_KEYS = [
    "API_TOKEN",
    "MARKET_DATA_ACCESS_TOKEN",
    "UPSTOX_CLIENT_SECRET",
    "UPSTOX_SANDBOX_ACCESS_TOKEN",
    "ALERT_WEBHOOK_URL",
]

KNOWN_KEYS = [
    "API_TOKEN",
    "BROKER",
    "MODE",
    "MARKET_DATA_SOURCE",
    "MARKET_DATA_ACCESS_TOKEN",
    "MARKET_DATA_SYMBOLS",
    "UPSTOX_CLIENT_ID",
    "UPSTOX_CLIENT_SECRET",
    "UPSTOX_REDIRECT_URI",
    "UPSTOX_SANDBOX_ACCESS_TOKEN",
    "ALERT_WEBHOOK_URL",
    "DEFAULT_SYMBOL",
    "DEFAULT_TIMEFRAME",
]


def _env_path(request: Request) -> Path:
    from app import runtime

    override = getattr(request.app.state, "env_file", None)
    return Path(override) if override else runtime.env_file()


@router.get("/setup", response_class=HTMLResponse,
            dependencies=[Depends(loopback_or_bearer)])
async def setup_page_view(request: Request) -> HTMLResponse:
    from app import runtime

    return HTMLResponse(setup_page.render(runtime.describe()))


class SaveRequest(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


@router.post("/setup/save", dependencies=[Depends(loopback_or_bearer)])
async def setup_save(payload: SaveRequest, request: Request) -> dict:
    if not payload.values:
        raise HTTPException(status_code=400, detail="Nothing to save.")

    refused: list[str] = []
    accepted: dict[str, str] = {}

    for key, raw in payload.values.items():
        key = key.strip().upper()
        value = (raw or "").strip()

        if key in LIVE_ENABLEMENT or key.startswith("RISK_"):
            refused.append(key)
            continue
        if key not in SETTABLE_KEYS:
            raise HTTPException(
                status_code=400,
                detail=f"{key} is not a key this screen is allowed to set.",
            )
        if value and value.upper() in LIVE_VALUES.get(key, set()):
            refused.append(key)
            continue
        if value == "":
            continue  # blank means "leave as is", never "erase"
        accepted[key] = value

    if refused and not accepted:
        raise HTTPException(
            status_code=400,
            detail=(
                "This screen will not set "
                + ", ".join(sorted(refused))
                + ". Live trading is enabled by hand, on purpose: see "
                "ARENA_GUIDE.md section 'Going live'. Arena will not let a "
                "credential form start real-money trading."
            ),
        )

    path = _env_path(request)
    summary = update_values(path, accepted)

    # The running process still holds the OLD settings. Say so plainly rather
    # than letting the operator believe their change took effect.
    settings = getattr(request.app.state, "settings", None)
    token_now = bool(getattr(settings, "api_token", ""))
    return {
        "ok": True,
        "path": summary["path"],
        "updated": summary["updated"],
        "added": summary["added"],
        "refused": sorted(refused),
        "restart_required": True,
        "token_active_now": token_now,
        "secret_keys_written": sorted(k for k in accepted if k in SECRET_KEYS),
        "note": (
            "Saved. Restart Arena for the new settings to take effect — the "
            "running server is still using the previous configuration."
        ),
    }


@router.get("/setup/status", dependencies=[Depends(loopback_or_bearer)])
async def setup_status(request: Request) -> dict:
    """What is configured. Presence only — never a value."""
    from app import runtime

    path = _env_path(request)
    present = masked_summary(path, KNOWN_KEYS)
    values = read_values(path)

    settings = getattr(request.app.state, "settings", None)
    return {
        "home": runtime.describe(),
        "env_file": str(path),
        "env_file_exists": path.exists(),
        "present": present,
        "broker": values.get("BROKER") or getattr(settings, "broker", None),
        "mode": values.get("MODE") or getattr(settings, "mode", None),
        "live_trading": (values.get("LIVE_TRADING", "").lower() in {"1", "true", "yes"}
                         or bool(getattr(settings, "live_trading", False))),
        "has_client_id": bool((values.get("UPSTOX_CLIENT_ID") or "").strip()),
        "has_redirect_uri": bool((values.get("UPSTOX_REDIRECT_URI") or "").strip()),
    }


@router.post("/setup/generate-token", dependencies=[Depends(loopback_or_bearer)])
async def generate_token() -> dict:
    """A fresh random API token. Returned ONCE, for the operator to save."""
    return {
        "token": secrets.token_urlsafe(32),
        "note": (
            "Stored in your browser session only until you press Save. Arena "
            "keeps it in the .env file, and never displays it again."
        ),
    }
