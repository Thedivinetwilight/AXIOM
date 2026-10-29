"""Alpaca brokerage API — gated order lifecycle for U.S. equities/ETFs.

Every response shows the execution destination (mode + endpoint host). Local
PAPER, Alpaca PAPER and Alpaca LIVE are distinct destinations; LIVE requires
the gate's explicit Arm + Activate on top of configuration and typed operator
confirmation. No market-data fallback exists here, and no secret is ever
included in a response body.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.alpaca_broker.client import BrokerError
from app.alpaca_broker.gate import GateError
from app.alpaca_broker.service import BrokerStateBlocked, LIVE_CONFIRM_PHRASE

router = APIRouter(prefix="/api/alpaca-broker")


class OrderRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    side: str = Field(pattern="^(buy|sell)$")
    qty: float | None = Field(default=None, gt=0)
    notional: float | None = Field(default=None, gt=0)
    order_type: str = Field(default="market", pattern="^(market|limit|stop|stop_limit)$")
    time_in_force: str = Field(default="day", pattern="^(day|gtc|ioc|fok|opg|cls)$")
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)
    reference_price: float | None = Field(default=None, gt=0)
    client_order_id: str | None = Field(default=None, max_length=64)


class ReplaceRequest(BaseModel):
    qty: float | None = Field(default=None, gt=0)
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)
    time_in_force: str | None = Field(default=None, pattern="^(day|gtc|ioc|fok|opg|cls)$")


class KillRequest(BaseModel):
    reason: str = Field(default="operator emergency stop", max_length=200)


class LiveActivateRequest(BaseModel):
    confirm_live_trading: str = Field(default="", max_length=120)
    acknowledge_risk: bool = Field(default=False)
    symbol: str | None = Field(default=None, max_length=20)


class BrokerCredentialBody(BaseModel):
    api_key: str = Field(min_length=1, max_length=256)
    api_secret: str = Field(min_length=1, max_length=256)


class BrokerModeBody(BaseModel):
    enabled: bool = True
    environment: str = Field(default="paper", pattern="^(paper|live)$")


class BrokerLimitsBody(BaseModel):
    max_order_value: float = Field(gt=0)
    max_open_positions: int = Field(gt=0)
    max_daily_orders: int = Field(gt=0)
    max_daily_loss: float = Field(default=2_500.0, gt=0)
    max_symbol_exposure: float = Field(default=50_000.0, gt=0)
    max_total_exposure: float = Field(default=200_000.0, gt=0)
    max_quote_age_seconds: float = Field(default=60.0, gt=0)
    max_clock_skew_seconds: float = Field(default=30.0, gt=0)


class PositionCloseBody(BaseModel):
    reason: str = Field(default="operator explicit position close", max_length=200)


def _runtime(request: Request):
    rt = getattr(request.app.state, "alpaca_broker_runtime", None)
    if rt is None:
        raise HTTPException(503, "Alpaca broker runtime is unavailable on this host")
    return rt


def service(request: Request):
    svc = getattr(request.app.state, "alpaca_broker", None)
    if svc is None:
        rt = getattr(request.app.state, "alpaca_broker_runtime", None)
        if rt is not None and rt.service is not None:
            svc = rt.service
    if svc is None:
        raise HTTPException(
            409,
            "Alpaca broker path is disabled (enable Alpaca Broker PAPER or configure "
            "ALPACA_BROKER_ENABLED=true for LIVE). Market data and local PAPER are unaffected.",
        )
    return svc


@router.get("/credentials")
def credentials_status(request: Request):
    return _runtime(request).credential_status()


@router.post("/credentials/{environment}")
def credentials_save(environment: str, body: BrokerCredentialBody, request: Request):
    rt = _runtime(request)
    try:
        out = rt.save_credentials(environment, body.api_key, body.api_secret)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    request.app.state.alpaca_broker = rt.service
    return {"saved": True, **out}


@router.delete("/credentials/{environment}")
def credentials_delete(environment: str, request: Request):
    rt = _runtime(request)
    if environment not in {"paper", "live"}:
        raise HTTPException(422, "environment must be 'paper' or 'live'")
    out = rt.delete_credentials(environment)
    request.app.state.alpaca_broker = rt.service
    return out


@router.post("/mode")
def configure_mode(body: BrokerModeBody, request: Request):
    rt = _runtime(request)
    try:
        out = rt.configure_mode(enabled=body.enabled, environment=body.environment)
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    request.app.state.alpaca_broker = rt.service
    return out


@router.post("/limits")
def configure_limits(body: BrokerLimitsBody, request: Request):
    rt = _runtime(request)
    out = rt.update_limits(
        max_order_value=body.max_order_value,
        max_open_positions=body.max_open_positions,
        max_daily_orders=body.max_daily_orders,
        max_daily_loss=body.max_daily_loss,
        max_symbol_exposure=body.max_symbol_exposure,
        max_total_exposure=body.max_total_exposure,
        max_quote_age_seconds=body.max_quote_age_seconds,
        max_clock_skew_seconds=body.max_clock_skew_seconds,
    )
    request.app.state.alpaca_broker = rt.service
    return out


@router.post("/test")
def test_readonly(request: Request, environment: str | None = None):
    return _runtime(request).probe_readonly(environment)


@router.get("/readiness")
def readiness(request: Request, symbol: str | None = None):
    svc = service(request)
    checks = svc.readiness_checks(symbol=symbol)
    failed = sorted(k for k, v in checks.items() if not v)
    return {
        "ok": len(failed) == 0,
        "checks": checks,
        "failed": failed,
        "gate": svc.gate.status(),
        "live_confirmation_phrase": LIVE_CONFIRM_PHRASE if svc.environment == "live" else None,
    }


@router.get("/status")
def status(request: Request):
    return service(request).status()


@router.post("/preview")
def preview(body: OrderRequest, request: Request):
    return service(request).preview_order(
        symbol=body.symbol,
        side=body.side,
        qty=body.qty,
        notional=body.notional,
        order_type=body.order_type,
        time_in_force=body.time_in_force,
        limit_price=body.limit_price,
        stop_price=body.stop_price,
        reference_price=body.reference_price,
    )


@router.post("/orders")
def submit(body: OrderRequest, request: Request):
    try:
        return service(request).submit_order(
            client_order_id=body.client_order_id,
            symbol=body.symbol,
            side=body.side,
            qty=body.qty,
            notional=body.notional,
            order_type=body.order_type,
            time_in_force=body.time_in_force,
            limit_price=body.limit_price,
            stop_price=body.stop_price,
            reference_price=body.reference_price,
        )
    except BrokerStateBlocked as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/orders")
def orders(request: Request):
    return {"orders": service(request).status()["orders"]}


@router.delete("/orders/{client_order_id}")
def cancel(client_order_id: str, request: Request):
    try:
        return service(request).cancel_order(client_order_id)
    except (BrokerStateBlocked, BrokerError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.patch("/orders/{client_order_id}")
def replace(client_order_id: str, body: ReplaceRequest, request: Request):
    try:
        return service(request).replace_order(
            client_order_id,
            qty=body.qty,
            limit_price=body.limit_price,
            stop_price=body.stop_price,
            time_in_force=body.time_in_force,
        )
    except (BrokerStateBlocked, BrokerError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/positions/{symbol}/close")
def close_position(symbol: str, body: PositionCloseBody, request: Request):
    try:
        return service(request).close_position(symbol, reason=body.reason)
    except (BrokerStateBlocked, BrokerError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/sync-fills")
def sync_fills(request: Request):
    return service(request).sync_fills()


@router.post("/reconcile")
def reconcile(request: Request):
    return service(request).reconcile()


@router.post("/recover")
def recover(request: Request):
    return service(request).recover()


@router.post("/gate/arm")
def gate_arm(request: Request, symbol: str | None = None):
    svc = service(request)
    try:
        return svc.gate.arm(svc.readiness_checks(symbol=symbol))
    except GateError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/gate/activate")
def gate_activate(body: LiveActivateRequest, request: Request):
    svc = service(request)
    try:
        return svc.activate_live_confirmed(
            confirm_live_trading=body.confirm_live_trading,
            acknowledge_risk=body.acknowledge_risk,
            symbol=body.symbol,
        )
    except (GateError, BrokerStateBlocked) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/gate/disarm")
def gate_disarm(request: Request):
    return service(request).disarm("operator disarm")


@router.post("/gate/reset-kill-switch")
def gate_reset(request: Request):
    try:
        return service(request).gate.reset_kill_switch(operator_ack=True)
    except GateError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/emergency-stop")
def emergency_stop(body: KillRequest, request: Request):
    return service(request).emergency_stop(body.reason)
