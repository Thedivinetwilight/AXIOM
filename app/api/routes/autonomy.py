"""Authenticated autonomy API and local operator page.

There is deliberately no token/credential request model and no order-confirm
endpoint.  Daily OAuth remains a local operator action; this API only asks the
service to re-read the locally configured token for read-only verification.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import authenticate, loopback_or_bearer
from app.autonomy.service import NotReady, SessionError
from app.autonomy.upstox import ReconciliationFailure, UpstoxAuthError, UpstoxError

api = APIRouter(prefix="/api/autonomy", dependencies=[Depends(authenticate)])
page = APIRouter()


class CreateSessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["PAPER", "UPSTOX_LIVE"]
    instrument_keys: list[str] = Field(min_length=1, max_length=500)


class RiskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_daily_loss: float = Field(gt=0)
    per_trade_risk_fraction: float = Field(gt=0, le=0.01)
    equity_at_entry: float | None = Field(default=None, gt=0)


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_key: str = Field(min_length=3, max_length=160)


def service(request: Request):
    value = getattr(request.app.state, "autonomy", None)
    if value is None:
        detail = getattr(request.app.state, "autonomy_error", "Autonomy service unavailable")
        raise HTTPException(503, detail)
    return value


def call(request: Request, operation):
    try:
        return operation(service(request))
    except NotReady as exc:
        raise HTTPException(409, str(exc)) from None
    except SessionError as exc:
        raise HTTPException(409, str(exc)) from None
    except UpstoxAuthError as exc:
        raise HTTPException(503, str(exc)) from None
    except ReconciliationFailure as exc:
        raise HTTPException(503, str(exc)) from None
    except UpstoxError as exc:
        raise HTTPException(502, str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from None


@api.get("/status")
def status(request: Request):
    value = service(request)
    return {
        "sessions": value.sessions(),
        "kronos": value.kronos_status,
        "catalog": {
            "refreshed_at": value.catalog.refreshed_at.isoformat() if value.catalog.refreshed_at else None,
            "source_sha256": value.catalog.source_sha256,
            "count": len(value.catalog.instruments),
            "warnings": value.catalog.warnings,
        },
        "credential_api": "none; use local OAuth then POST authorize without a token body",
        "automatic_arm_or_activate": False,
        "profitability_claim": "none",
    }


@api.get("/catalog")
def catalog(request: Request, query: str = "", limit: int = 100):
    value = service(request)
    limit = max(1, min(limit, 500))
    needle = query.strip().upper()
    items = [item.public() for item in value.catalog.instruments.values()
             if not needle or needle in item.instrument_key.upper()
             or needle in item.trading_symbol.upper()
             or needle in item.underlying_symbol.upper()]
    return {
        "scope": "NSE/BSE equities and NCD/BCD exchange-listed currency derivatives; no spot forex",
        "refreshed_at": value.catalog.refreshed_at.isoformat() if value.catalog.refreshed_at else None,
        "source_sha256": value.catalog.source_sha256,
        "total": len(value.catalog.instruments), "returned": len(items[:limit]),
        "warnings": value.catalog.warnings, "instruments": items[:limit],
    }


@api.post("/catalog/refresh")
def refresh_catalog(request: Request):
    # Data-only operation: does not create a session, change mode, Arm,
    # Activate, or issue an execution permit.
    return call(request, lambda value: value.refresh_catalog())


@api.post("/sessions", status_code=201)
def create_session(body: CreateSessionBody, request: Request):
    return call(request, lambda value: value.create_session(body.mode, body.instrument_keys))


@api.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request):
    return call(request, lambda value: value.public(session_id))


@api.post("/sessions/{session_id}/recover-view")
def recover_view(session_id: str, request: Request):
    # Browser/session restoration is a local lifecycle stop, never a broker
    # order action. It clears approval and exposes only the durable view.
    return call(request, lambda value: value.recover_view(session_id))


@api.post("/sessions/{session_id}/authorize")
def authorize(session_id: str, request: Request):
    # No body, no credential field, no credential in logs/responses.
    return call(request, lambda value: value.authorize(session_id))


@api.post("/sessions/{session_id}/risk")
def risk(session_id: str, body: RiskBody, request: Request):
    return call(request, lambda value: value.set_risk(
        session_id, max_daily_loss=body.max_daily_loss,
        per_trade_risk_fraction=body.per_trade_risk_fraction,
        equity_at_entry=body.equity_at_entry,
    ))


@api.post("/sessions/{session_id}/arm")
def arm(session_id: str, request: Request):
    return call(request, lambda value: value.arm(session_id))


@api.post("/sessions/{session_id}/activate")
def activate(session_id: str, request: Request):
    return call(request, lambda value: value.activate(session_id))


@api.post("/sessions/{session_id}/pause")
def pause(session_id: str, request: Request):
    return call(request, lambda value: value.pause(session_id))


@api.post("/sessions/{session_id}/emergency-stop")
def emergency_stop(session_id: str, request: Request):
    return call(request, lambda value: value.emergency_stop(session_id))


@api.post("/sessions/{session_id}/scan")
def scan_read_only(session_id: str, request: Request):
    # This route is deliberately bound only to the read-only service method.
    # The worker's order-eligible execute_once method is not exposed by any API.
    return call(request, lambda value: value.scan_once(session_id))


@api.post("/sessions/{session_id}/reconcile")
def reconcile(session_id: str, request: Request):
    return call(request, lambda value: value.reconcile(session_id))


@api.post("/sessions/{session_id}/cancel-unfilled")
def cancel_unfilled(session_id: str, body: CancelBody, request: Request):
    return call(request, lambda value: value.cancel_unfilled(session_id, body.instrument_key))


@api.get("/sessions/{session_id}/audit")
def audit(session_id: str, request: Request, limit: int = 200):
    return call(request, lambda value: value.audit(session_id, limit))


@api.get("/sessions/{session_id}/bars/{instrument_key:path}")
def bars(session_id: str, instrument_key: str, request: Request):
    def operation(value):
        runtime = value._runtime(session_id)
        if instrument_key not in runtime.instruments:
            raise SessionError("Instrument is not selected in this session")
        bars = runtime.bars.get(instrument_key, [])
        return {
            "instrument_key": instrument_key, "source": "UPSTOX_COMPLETED_BARS",
            "display_only": True, "tradingview_data_used": False,
            "bars": [bar.to_dict() for bar in bars],
        }
    return call(request, operation)


@page.get("/autonomy", include_in_schema=False, dependencies=[Depends(loopback_or_bearer)])
def autonomy_page():
    return FileResponse(Path(__file__).resolve().parent.parent.parent / "static" / "autonomy" / "index.html")


@page.get("/autonomy/manual", include_in_schema=False, dependencies=[Depends(loopback_or_bearer)])
def autonomy_manual():
    path = Path(__file__).resolve().parents[3] / "docs" / "AUTONOMY.md"
    if not path.is_file():
        return PlainTextResponse("Autonomy manual is missing from this installation", status_code=500)
    return PlainTextResponse(path.read_text(encoding="utf-8"))
