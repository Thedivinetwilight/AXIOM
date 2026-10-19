"""Live activation API — the PAPER/LIVE switch, server side.

Every endpoint here is behind the normal bearer-token gate, validates its
input on the server (the browser is untrusted), and never returns a secret.
The workflow is one integrated path:

    connect account -> set/confirm limits -> review readiness
    -> START REAL-MONEY TRADING -> the supervised live engine runs

and the reverse path:

    move the switch away from LIVE -> pause entries, drain, disarm, PAPER

The state machine, the readiness contract, the supervisor, the gateway and
the durable emergency stop are all reused unchanged; this module only
sequences them.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import authenticate
from app.live.activation import ActivationError, LiveActivationService

logger = logging.getLogger(__name__)
router = APIRouter()


def _service(request: Request) -> LiveActivationService:
    workstation = getattr(request.app.state, "workstation", None)
    if workstation is None:
        raise HTTPException(503, "workstation services are not initialized")
    return LiveActivationService(workstation, app=request.app)


def _fail(exc: ActivationError) -> HTTPException:
    """Turn an activation refusal into a truthful HTTP error."""
    return HTTPException(409, str(exc))


# ------------------------------------------------------------------ read --

@router.get("/api/live/activation", dependencies=[Depends(authenticate)])
async def activation_snapshot(request: Request) -> dict:
    """The whole activation state: switch, credential, limits, readiness."""
    return _service(request).snapshot()


@router.get("/api/live/activation/book", dependencies=[Depends(authenticate)])
async def activation_book(request: Request) -> dict:
    """Positions, working orders and reconciliation evidence.

    Used by the switch-off flow so the operator sees what is actually open
    before anything is stopped. Unavailable data is reported as unavailable.
    """
    return _service(request).book_status()


# --------------------------------------------------------------- write --

class ConfigurationBody(BaseModel):
    # Unknown fields are refused, not ignored: the browser is untrusted and a
    # silently dropped limit would be a limit the operator believes is set.
    model_config = ConfigDict(extra="forbid")

    max_position_value: float | str | None = None
    max_open_positions: int | str | None = None
    max_daily_loss: float | str | None = None
    max_trades_per_day: int | str | None = None
    live_canary_max_notional: float | str | None = None
    live_canary_orders: int | str | None = None
    live_max_equity_drawdown: float | str | None = None
    data_source: str | None = None
    symbols: str | None = None
    timeframe: str | None = None
    calendar: str | None = None
    market_data_mode: str | None = None
    live_base_url: str | None = None
    live_order_history_url: str | None = None


@router.post("/api/live/activation/configuration",
             dependencies=[Depends(authenticate)])
async def save_configuration(body: ConfigurationBody, request: Request) -> dict:
    """Validate and store the live configuration.

    This does NOT enable live trading: the four live switches are written
    only by the final activation step.
    """
    payload = {key: value for key, value in body.model_dump().items()
               if value is not None}
    try:
        result = _service(request).save_configuration(payload)
    except ActivationError as exc:
        raise _fail(exc) from exc
    return result


class OAuthUrlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=200)
    redirect_uri: str = Field(min_length=1, max_length=500)


@router.post("/api/live/activation/oauth/url", dependencies=[Depends(authenticate)])
async def oauth_url(body: OAuthUrlBody, request: Request) -> dict:
    """The broker's official login URL (public client id only)."""
    try:
        return _service(request).authorization_url(body.model_dump())
    except ActivationError as exc:
        raise _fail(exc) from exc


class OAuthExchangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=4000)
    client_id: str = Field(min_length=1, max_length=500)
    client_secret: str = Field(min_length=1, max_length=500)
    redirect_uri: str = Field(min_length=1, max_length=500)


@router.post("/api/live/activation/oauth/exchange",
             dependencies=[Depends(authenticate)])
async def oauth_exchange(body: OAuthExchangeBody, request: Request) -> dict:
    """Exchange the broker's authorization code and store the live token.

    The code and the client secret are used in-process only. The response
    carries a masked credential state, never a token.
    """
    try:
        return _service(request).connect_account(body.model_dump())
    except ActivationError as exc:
        raise _fail(exc) from exc


@router.post("/api/live/activation/credential/forget",
             dependencies=[Depends(authenticate)])
async def forget_credential(request: Request) -> dict:
    """Remove the stored live access token."""
    try:
        return _service(request).forget_credential()
    except ActivationError as exc:
        raise _fail(exc) from exc


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_phrase: str = Field(min_length=1, max_length=200)


@router.post("/api/live/activation/start", dependencies=[Depends(authenticate)])
async def start_live(body: StartBody, request: Request) -> dict:
    """START REAL-MONEY TRADING.

    Requires the exact confirmation phrase. Writes the four live switches,
    starts the supervised child, then ARM -> ACTIVATE through the existing
    state machine. A refusal rolls the configuration back to PAPER.
    """
    try:
        return _service(request).start(confirmation_phrase=body.confirmation_phrase)
    except ActivationError as exc:
        raise _fail(exc) from exc


class StopBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = "operator moved the switch to PAPER"
    keep_configuration: bool = False


@router.post("/api/live/activation/stop", dependencies=[Depends(authenticate)])
async def stop_live(body: StopBody, request: Request) -> dict:
    """The safe switch-off: drain, disarm, return to PAPER.

    Never claims the account is flat; it reports what the broker confirmed.
    """
    try:
        return _service(request).stop(
            reason=body.reason or "operator moved the switch to PAPER",
            remove_configuration=not body.keep_configuration)
    except ActivationError as exc:
        raise _fail(exc) from exc
