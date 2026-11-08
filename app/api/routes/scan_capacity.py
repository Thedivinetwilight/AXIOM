"""Authenticated, read-only scan-capacity routes (`/api/ensemble/scan/*`).

These routes answer one question for the operator console: *given the provider
quotas this process is enforcing right now, how many symbols can a read-only
scan cover at the requested cadence, and what would one cycle consume?*

Guarantees:

* ``GET /capacity`` and ``POST /preview`` are pure reads. They consume no
  quota, make no provider request, and never touch a credential value — only
  whether one is present.
* ``POST /start`` is the only route that can cause market-data traffic, and it
  refuses unless the server-side plan says the cycle fits every documented
  window. It calls the existing ensemble evaluation with ``execute_in_paper``
  forced off and ``persist_decisions`` off, so it cannot submit a paper or
  broker order; the response always reports ``orders`` 0.
* No route returns a token, key, secret, or request header.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.auth import authenticate
from app.markets.scan_capacity import MAX_SCAN_SYMBOLS, ScanCapacityService

router = APIRouter(
    prefix="/api/ensemble/scan",
    tags=["scan-capacity"],
    dependencies=[Depends(authenticate)],
)

_MARKET_PATTERN = "^(NSE|NASDAQ|nse|nasdaq)$"

BASIS = {
    "quota_math": "DOCUMENTED",
    "quota_math_detail": (
        "Per-endpoint-category rolling windows from the provider documentation, "
        "as enforced by the same ProviderFetchGate the engine uses."
    ),
    "provider_network_calls": 0,
    "broker_order_calls": 0,
    "timing_model": "MODELED OFFLINE",
    "timing_model_detail": (
        "Cadence floors are derived from documented quota windows only. "
        "No production latency, fill, or timing guarantee is implied."
    ),
}

NOTICE = (
    "Scanning a symbol does not mean it will be traded. Scanning is read-only "
    "market data; order submission stays disabled on this path."
)


def _svc(request: Request) -> ScanCapacityService:
    svc = getattr(request.app.state, "scan_capacity_service", None)
    if svc is None:
        raise HTTPException(503, "Scan capacity service is unavailable")
    return svc


class ScanPreviewBody(BaseModel):
    market: str = Field(pattern=_MARKET_PATTERN)
    symbol_count: int = Field(default=MAX_SCAN_SYMBOLS, ge=1, le=MAX_SCAN_SYMBOLS)
    symbols: list[str] | None = Field(default=None, max_length=MAX_SCAN_SYMBOLS)
    cadence_s: float = Field(default=60.0, ge=1.0, le=3600.0)


class ScanStartBody(BaseModel):
    market: str = Field(pattern=_MARKET_PATTERN)
    symbol_count: int = Field(default=MAX_SCAN_SYMBOLS, ge=1, le=MAX_SCAN_SYMBOLS)
    symbols: list[str] | None = Field(default=None, max_length=MAX_SCAN_SYMBOLS)
    cadence_s: float = Field(default=60.0, ge=1.0, le=3600.0)
    timeframe: str | None = Field(default=None, max_length=8)


def _selected_count(body_symbols: list[str] | None, symbol_count: int) -> int:
    if body_symbols is None:
        return int(symbol_count)
    cleaned = [s for s in (str(x).strip() for x in body_symbols) if s]
    if not cleaned:
        raise HTTPException(422, "symbols was provided but contained no symbol")
    return len(cleaned)


@router.get("/capacity")
def scan_capacity(
    request: Request,
    market: str = Query(default="", max_length=16),
    symbols: int = Query(default=MAX_SCAN_SYMBOLS, ge=1, le=MAX_SCAN_SYMBOLS),
    cadence_s: float = Query(default=60.0, ge=1.0, le=3600.0),
) -> dict[str, Any]:
    """Read-only snapshot: quota profile, rolling usage, plan, limits, history."""
    svc = _svc(request)
    wanted = [m.strip().upper() for m in market.split(",") if m.strip()] or ["NSE", "NASDAQ"]
    markets: dict[str, Any] = {}
    history: dict[str, Any] = {}
    for mkt in wanted:
        if mkt not in {"NSE", "NASDAQ"}:
            raise HTTPException(404, f"Unsupported market {mkt!r}")
        snapshot = svc.snapshot(mkt, symbols=symbols, cadence_s=cadence_s)
        snapshot["history"] = svc.history(mkt, limit=10)
        snapshot["selected_symbols"] = snapshot.get("selected_symbols")
        markets[mkt] = snapshot
        history[mkt] = snapshot["history"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "basis": dict(BASIS),
        "notice": NOTICE,
        "max_symbols_per_scan": MAX_SCAN_SYMBOLS,
        "markets": markets,
        "scan_status": svc.status(),
    }


@router.post("/preview")
def scan_preview(request: Request, body: ScanPreviewBody) -> dict[str, Any]:
    """Offline capacity preview for one market. Consumes no quota."""
    svc = _svc(request)
    mkt = body.market.strip().upper()
    n = _selected_count(body.symbols, body.symbol_count)
    plan = svc.plan(mkt, symbols=n, cadence_s=body.cadence_s)
    snapshot = svc.snapshot(mkt, symbols=n, cadence_s=body.cadence_s)
    return {
        "market": mkt,
        "requested_symbols": n,
        "basis": dict(BASIS),
        "notice": NOTICE,
        "plan": plan.to_dict(),
        "quota": snapshot["quota"],
        "gate": snapshot["gate"],
        "limits": snapshot["limits"],
        "dispatch_plan": _dispatch_lines(plan.to_dict()),
    }


@router.post("/start")
def scan_start(request: Request, body: ScanStartBody) -> dict[str, Any]:
    """Start ONE read-only scan cycle, or return the reason it cannot run."""
    svc = _svc(request)
    mkt = body.market.strip().upper()
    n = _selected_count(body.symbols, body.symbol_count)
    try:
        result = svc.start(
            mkt,
            symbols=body.symbols,
            cadence_s=body.cadence_s,
            timeframe=body.timeframe,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    result["market"] = mkt
    result["requested_symbols"] = n
    result["basis"] = dict(BASIS)
    result["notice"] = NOTICE
    if result.get("started"):
        result["dispatch_plan"] = _dispatch_lines(result.get("plan") or {})
    return result


@router.get("/history")
def scan_history(
    request: Request,
    market: str = Query(default="", max_length=16),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    svc = _svc(request)
    return {
        "market": (market or "ALL").upper(),
        "runs": svc.history(market, limit=limit),
        "scan_status": svc.status(),
        "basis": dict(BASIS),
        "notice": NOTICE,
    }


def _dispatch_lines(plan: dict[str, Any]) -> list[str]:
    """Operator-readable, exact statements about what a start would do."""
    if not plan:
        return []
    lines = [
        f"Symbols per cycle: {plan.get('symbols')} (each symbol is analyzed; "
        f"at most the risk-allocator limits can become trades)",
        f"Provider requests per cycle: {plan.get('total_requests_per_cycle')} "
        f"({', '.join(f'{k}={v}' for k, v in (plan.get('requests_per_cycle') or {}).items())})",
        f"Computed sustainable cadence: every {plan.get('cadence_sustainable_seconds')}s",
        f"Requested cadence: every {plan.get('cadence_requested_seconds')}s",
        f"Orders on this path: 0 (read-only market data)",
    ]
    for category, rows in (plan.get("window_usage") or {}).items():
        for row in rows:
            lines.append(
                f"{category} · {row.get('label')} window: {row.get('current')} / "
                f"{row.get('limit')} used, this cycle +{row.get('requests_this_cycle')}"
            )
    return lines
