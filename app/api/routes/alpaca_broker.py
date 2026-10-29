"""Alpaca brokerage API — gated order lifecycle for U.S. equities/ETFs.

Every response shows the execution destination (mode + endpoint host). Local
PAPER, Alpaca PAPER and Alpaca LIVE are distinct destinations; LIVE requires
the gate's explicit Arm + Activate on top of configuration. No market-data
fallback exists here, and no secret is ever accepted in a response body.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.alpaca_broker.client import BrokerError, BrokerRejected
from app.alpaca_broker.gate import GateError
from app.alpaca_broker.service import BrokerStateBlocked

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
    client_order_id: str | None = Field(default=None, max_length=64)


class ReplaceRequest(BaseModel):
    qty: float | None = Field(default=None, gt=0)
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)
    time_in_force: str | None = Field(default=None, pattern="^(day|gtc|ioc|fok|opg|cls)$")


class KillRequest(BaseModel):
    reason: str = Field(default="operator emergency stop", max_length=200)


def service(request: Request):
    svc = getattr(request.app.state, "alpaca_broker", None)
    if svc is None:
        raise HTTPException(
            409,
            "Alpaca broker path is disabled (set ALPACA_BROKER_ENABLED=true and configure "
            "broker credentials). Market data and local PAPER are unaffected.",
        )
    return svc


@router.get("/status")
def status(request: Request):
    return service(request).status()


@router.post("/preview")
def preview(body: OrderRequest, request: Request):
    return service(request).preview_order(
        symbol=body.symbol, side=body.side, qty=body.qty, notional=body.notional,
        order_type=body.order_type, time_in_force=body.time_in_force,
        limit_price=body.limit_price, stop_price=body.stop_price,
    )


@router.post("/orders")
def submit(body: OrderRequest, request: Request):
    try:
        return service(request).submit_order(
            client_order_id=body.client_order_id, symbol=body.symbol, side=body.side,
            qty=body.qty, notional=body.notional, order_type=body.order_type,
            time_in_force=body.time_in_force, limit_price=body.limit_price,
            stop_price=body.stop_price,
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
    except BrokerStateBlocked as exc:
        raise HTTPException(409, str(exc)) from exc
    except BrokerError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.patch("/orders/{client_order_id}")
def replace(client_order_id: str, body: ReplaceRequest, request: Request):
    try:
        return service(request).replace_order(
            client_order_id, qty=body.qty, limit_price=body.limit_price,
            stop_price=body.stop_price, time_in_force=body.time_in_force,
        )
    except BrokerStateBlocked as exc:
        raise HTTPException(409, str(exc)) from exc
    except (BrokerError, ValueError) as exc:
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
def gate_arm(request: Request):
    svc = service(request)
    try:
        return svc.gate.arm(svc.readiness_checks())
    except GateError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/gate/activate")
def gate_activate(request: Request):
    svc = service(request)
    try:
        return svc.activate_live()
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
