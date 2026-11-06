"""Read-only latency metrics for the supported (paper) application.

The legacy ``/api/trading/performance`` route lived in the removed Vantage MT5
surface. The operator still needs to see local process timing, so the same
``LatencyTracker`` summary is exposed here on a first-class, authenticated,
read-only route. It reports observations only: no broker call, no order, and no
"zero latency" claim.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.auth import authenticate

router = APIRouter()


@router.get("/api/performance")
def performance(request: Request, _: None = Depends(authenticate)) -> dict:
    tracker = getattr(request.app.state, "performance", None)
    summary = tracker.summary() if tracker is not None else {"metrics": {}}
    return {
        **summary,
        "read_only": True,
        "paper_only": True,
        "live_trading_enabled": False,
        "broker_order_api_calls": 0,
        "evidence": (
            "Fake-provider and in-process timings only; no real DEMO or LIVE "
            "broker latency is established by these numbers."
        ),
    }
