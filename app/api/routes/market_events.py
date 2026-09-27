"""Market Events API — internet event intelligence (additive, information-only).

All endpoints are behind the existing bearer-token gate (same as other /api).
The listener itself never places orders, never bypasses hard risk.
"""

from __future__ import annotations

import json
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/market-events")


def _repo(request: Request):
    return request.app.state.repository


@router.get("")
async def list_events(request: Request, limit: int = 20, event_type: str | None = None):
    """List recent market events — never exposes secrets."""
    repository = _repo(request)
    events = repository.list_market_events(event_type=event_type, limit=min(limit, 100))
    # Sanitize: ensure no token leakage, no raw HTML
    # We return the stored sanitized fields only; headline/summary already stripped of < >
    return {"events": events, "count": len(events)}


@router.get("/{event_id}")
async def get_event(request: Request, event_id: str):
    repository = _repo(request)
    event = repository.get_market_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="event not found")
    sources = repository.list_market_event_sources(event_id)
    observations = repository.list_market_observations(event_id, limit=20)
    return {"event": event, "sources": sources, "observations": observations}


@router.post("/ingest")
async def ingest_event(request: Request):
    """Ingest a raw article (for operator use / tests). Body: {headline, summary, source, source_url}"""
    repository = _repo(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="invalid json")
    headline = str(body.get("headline") or body.get("title") or "").strip()
    summary = str(body.get("summary") or body.get("description") or "").strip()
    source = str(body.get("source") or "operator").strip()
    source_url = str(body.get("source_url") or body.get("url") or "").strip()
    if not headline:
        raise HTTPException(status_code=422, detail="headline required")
    if not source_url:
        # allow without url for offline tests, but generate a placeholder
        source_url = f"https://example.local/event/{headline[:20].replace(' ', '-')}"
    # Use listener to ingest with full pipeline
    from app.market_events.listener import MarketEventListener
    from app.market_events.collector import EventCollector
    # Use no-op collector (direct ingest)
    listener = MarketEventListener(repository, collector=EventCollector(fetcher=None))
    # Pass strategy_source if available
    try:
        listener.strategy_source = getattr(request.app.state, "strategy_source", None) or getattr(request.app.state, "sandbox_engine", None)
        # If engine has strategy_source, use it
        if hasattr(listener.strategy_source, "strategy_source"):
            listener.strategy_source = listener.strategy_source.strategy_source
    except Exception as exc:
        import logging
        logging.getLogger("bot.market_events").debug("strategy_source fallback failed", extra={"error": type(exc).__name__})
    event = listener.ingest_raw_article({"headline": headline, "summary": summary}, source=source, source_url=source_url)
    if not event:
        raise HTTPException(status_code=422, detail="event ignored or malformed")
    return {"event_id": event.event_id, "arena_response": event.arena_response, "action_taken": event.action_taken}


@router.post("/{event_id}/observe")
async def observe_event(request: Request, event_id: str):
    """Record market response for an event (uses existing market-data infrastructure)."""
    repository = _repo(request)
    event = repository.get_market_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="event not found")
    from app.market_events.listener import MarketEventListener
    listener = MarketEventListener(repository)
    summary = listener.record_market_response(event_id)
    return {"event_id": event_id, "observations_recorded": summary}


@router.get("/{event_id}/observations")
async def list_observations(request: Request, event_id: str):
    repository = _repo(request)
    event = repository.get_market_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="event not found")
    obs = repository.list_market_observations(event_id, limit=50)
    return {"event_id": event_id, "observations": obs}
