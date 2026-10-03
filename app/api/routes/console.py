"""Feature console: a browser GUI over every command the platform offers.

WHAT THIS IS
    `GET  /console`         the page (same local-page rule as the v1 dashboard)
    `GET  /console/catalog` every command and flag, read from the live parsers
    `POST /console/run`     run one command and return its exact output
    `GET  /console/state`   a read-only snapshot for the header

WHAT IT IS NOT
    It is not a second implementation of anything. It shells out to the same
    `python -m app ...` and `run.py ...` entry points an operator types, so
    every gate behaves identically. In particular the console cannot enable
    live trading: it sets no environment variables, and the four-switch opt-in
    plus the risk limits are still evaluated by the app itself in the child
    process.

WHY IT IS BEHIND THE FULL BEARER GATE
    `/console/run` executes commands, so it takes the same `authenticate`
    dependency as every other protected route — never the relaxed
    `loopback_or_bearer` used for the read-only dashboard page. Only the HTML
    shell is served locally without a token, exactly like the v1 dashboard, and
    that shell contains no secrets.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.api.auth import authenticate, loopback_or_bearer
from app.console import page as page_module
from app.console.catalog import catalogue
from app.console.runner import ConsoleError, run_command

router = APIRouter()


@router.get("/console", response_class=HTMLResponse, dependencies=[Depends(loopback_or_bearer)])
async def console_page() -> HTMLResponse:
    """The console shell. Contains no secrets and no command output."""
    return HTMLResponse(page_module.render())


@router.get("/console/catalog", dependencies=[Depends(authenticate)])
async def console_catalog() -> dict:
    """Every feature, derived from the argument parsers actually in use."""
    return catalogue()


class RunRequest(BaseModel):
    program: str = Field(default="app", description="'app' or 'run'")
    command: str = Field(description="a subcommand name from /console/catalog")
    args: list[str] = Field(default_factory=list, description="arguments, as separate items")
    timeout: int = Field(default=120, ge=1, le=900)


@router.post("/console/run", dependencies=[Depends(authenticate)])
async def console_run(payload: RunRequest) -> dict:
    """Run one command and return its exit code, stdout, stderr and duration."""
    try:
        result = run_command(
            payload.program,
            payload.command,
            payload.args,
            timeout=payload.timeout,
        )
    except ConsoleError as exc:
        busy = "already running" in str(exc)
        raise HTTPException(status_code=409 if busy else 400, detail=str(exc)) from None
    return result.as_dict()


@router.get("/console/state", dependencies=[Depends(authenticate)])
async def console_state(request: Request) -> dict:
    """Read-only snapshot: mode, engine state, whether live is armed."""
    settings = getattr(request.app.state, "settings", None)
    simulator = getattr(request.app.state, "simulator", None)
    engine = getattr(request.app.state, "sandbox_engine", None)

    status: dict = {}
    if simulator is not None:
        try:
            status = simulator.status()
        except Exception:  # pragma: no cover - status must never 500 the page
            status = {}

    return {
        "mode": getattr(settings, "mode", None),
        "broker": getattr(settings, "broker", None),
        "live_trading": bool(getattr(settings, "live_trading", False)),
        "default_symbol": getattr(settings, "default_symbol", None),
        "default_timeframe": getattr(settings, "default_timeframe", None),
        "engine_state": status.get("state"),
        "engine_running": engine is not None,
        "market_open": status.get("market_open"),
    }
