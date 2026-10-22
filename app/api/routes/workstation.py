"""AXIOM-MONEY-PRINTER workstation API (master spec sections 4-5, 8, 14-16,
18, 21, 24, 29, 31-40, 57, 58, 67-68, 71).

Every endpoint here is backed by the workstation composition root
(``app.state.workstation``); nothing is a placeholder. Secrets never appear
in responses: broker connection metadata, token lifecycle states and masked
hints only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.auth import authenticate
from app.markets import get_market, list_markets, market_session_state, parse_instrument_identity

logger = logging.getLogger(__name__)
router = APIRouter()


def ws(request: Request):
    workstation = getattr(request.app.state, "workstation", None)
    if workstation is None:
        raise HTTPException(503, "workstation services are not initialized")
    return workstation


# =====================================================================
# MARKETS + SESSIONS (spec sections 5, 143, 144)
# =====================================================================

@router.get("/api/markets", dependencies=[Depends(authenticate)])
async def markets(request: Request) -> dict:
    return {"markets": list_markets(),
            "selected": getattr(ws(request).settings, "market", "india")}


@router.get("/api/markets/{market_id}/session", dependencies=[Depends(authenticate)])
async def market_session(market_id: str) -> dict:
    try:
        return market_session_state(market_id).to_dict()
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@router.get("/api/forex/scope", dependencies=[Depends(authenticate)])
async def forex_scope() -> dict:
    """Expose the bounded research specification without implying support.

    This endpoint performs no broker or provider calls. All unknown external
    readiness stays blocked; only the USDINR exchange-futures paper math is
    described. OTC spot, CFDs, and live orders are explicitly out of scope.
    """
    from app.forex.contracts import (
        CONTRACT_UNITS, EXCHANGE, EXCHANGE_SEGMENT, MVP_PAIR, PIP_SIZE,
        PRODUCT, SESSION_CLOSE, SESSION_OPEN, TICK_SIZE,
    )

    return {
        "asset_class": "FX",
        "jurisdiction": "IN",
        "support_status": "BLOCKED_UNVERIFIED",
        "scope_type": "candidate_paper_research_only",
        "candidate_product": PRODUCT,
        "candidate_pair": MVP_PAIR,
        "exchange": EXCHANGE,
        "provider_segment": EXCHANGE_SEGMENT,
        "contract_units_per_lot": CONTRACT_UNITS,
        "tick_size_quote_currency": str(TICK_SIZE),
        "display_pip_size_quote_currency": str(PIP_SIZE),
        "ticks_per_display_pip": int(PIP_SIZE / TICK_SIZE),
        "quote_currency": "INR",
        "account_currency": "INR",
        "session_hint": {"timezone": "Asia/Kolkata", "open": SESSION_OPEN,
                          "close": SESSION_CLOSE, "holiday_status": "UNKNOWN",
                          "dst": "not_applicable_fixed_IST",
                          "provider_status_required": True},
        "paper": {"mode": "PAPER", "broker_endpoint_calls": False,
                  "accounting_module": "domain_only_not_wired_to_simulator",
                  "durable_state": "NOT_IMPLEMENTED"},
        "market_data": {"provider": "upstox", "account_entitlement": "UNVERIFIED",
                         "connection": "NOT_TESTED", "quote_freshness": "NOT_OBSERVED"},
        "live": {"enabled": False, "supported": False,
                 "reason": "No NCD_FO live broker adapter or account/legal readiness evidence; existing NSE_EQ-only live guards remain unchanged."},
        "excluded_products": ["OTC spot", "CFDs", "offshore leveraged FX"],
        "blockers": [
            "No user-specific RBI/FEMA eligibility or permitted-purpose verification.",
            "No Upstox NCD_FO account entitlement, current contract record, or authorized feed observation has been verified.",
            "Paper accounting is a tested domain component only; it is not yet connected to the app simulator, durable journal, or UI order lifecycle.",
            "Current exchange holiday calendar and live NCD_FO segment status are not verified here.",
        ],
        "source_urls": [
            "https://rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=10485",
            "https://www.nseindia.com/static/products-services/currency-derivatives-contract-specification-inr",
            "https://upstox.com/developer/api-documentation/instrument-search/",
            "https://upstox.com/developer/api-documentation/v3/get-market-data-feed/",
        ],
    }

# =====================================================================
# UNIVERSES (spec sections 4, 60) — market-scoped, persisted
# =====================================================================

class UniverseBody(BaseModel):
    market_id: str
    name: str = Field(min_length=1, max_length=80)
    symbols: list[str]
    note: str | None = None


@router.get("/api/universes", dependencies=[Depends(authenticate)])
async def list_universes(request: Request, market_id: str | None = None) -> dict:
    return {"universes": ws(request).repository.list_universes(market_id=market_id)}


@router.post("/api/universes", dependencies=[Depends(authenticate)])
async def save_universe(body: UniverseBody, request: Request) -> dict:
    workstation = ws(request)
    market_id = body.market_id.strip().lower()
    try:
        get_market(market_id)
    except KeyError as exc:
        raise HTTPException(422, str(exc))
    if not body.symbols:
        raise HTTPException(422, "a universe needs at least one instrument")
    seen, cleaned = set(), []
    for raw in body.symbols:
        parsed = parse_instrument_identity(raw, market_id)
        if not parsed["valid"]:
            raise HTTPException(
                422, f"instrument '{raw}' rejected for market '{market_id}': "
                     f"{parsed['reason']}")
        identity = parsed["identity"]
        if identity not in seen:
            seen.add(identity)
            cleaned.append(identity)
    record = workstation.repository.save_universe(
        market_id=market_id, name=body.name.strip(), symbols=cleaned, note=body.note)
    workstation.changes.observe("universe", record["id"],
                                {"name": record["name"], "size": len(cleaned),
                                 "market_id": market_id},
                                evidence="universe saved through the GUI")
    return record


@router.get("/api/universes/{universe_id}", dependencies=[Depends(authenticate)])
async def get_universe(universe_id: str, request: Request) -> dict:
    record = ws(request).repository.get_universe(universe_id)
    if record is None:
        raise HTTPException(404, "universe not found")
    return record


@router.delete("/api/universes/{universe_id}", dependencies=[Depends(authenticate)])
async def delete_universe(universe_id: str, request: Request) -> dict:
    deleted = ws(request).repository.delete_universe(universe_id)
    if not deleted:
        raise HTTPException(404, "universe not found")
    return {"deleted": True, "id": universe_id}


@router.get("/api/universes/{universe_id}/export", dependencies=[Depends(authenticate)])
async def export_universe(universe_id: str, request: Request) -> dict:
    record = ws(request).repository.get_universe(universe_id)
    if record is None:
        raise HTTPException(404, "universe not found")
    return {"format": "axiom-universe/v1",
            "market_id": record["market_id"], "name": record["name"],
            "symbols": record["symbols"], "note": record.get("note"),
            "exported_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


class UniverseImport(BaseModel):
    format: str = "axiom-universe/v1"
    market_id: str
    name: str
    symbols: list[str]
    note: str | None = None


@router.post("/api/universes/import", dependencies=[Depends(authenticate)])
async def import_universe(body: UniverseImport, request: Request) -> dict:
    if body.format != "axiom-universe/v1":
        raise HTTPException(422, f"unsupported universe format '{body.format}'")
    return await save_universe(UniverseBody(market_id=body.market_id, name=body.name,
                                            symbols=body.symbols, note=body.note),
                               request)


class PinBody(BaseModel):
    market_id: str
    identity: str


@router.post("/api/universe/pin", dependencies=[Depends(authenticate)])
async def pin_instrument(body: PinBody, request: Request) -> dict:
    parsed = parse_instrument_identity(body.identity, body.market_id)
    if not parsed["valid"]:
        raise HTTPException(422, f"cannot pin: {parsed['reason']}")
    ws(request).repository.pin_instrument(body.market_id, parsed["identity"])
    return {"pinned": parsed["identity"], "market_id": body.market_id}


@router.post("/api/universe/unpin", dependencies=[Depends(authenticate)])
async def unpin_instrument(body: PinBody, request: Request) -> dict:
    parsed = parse_instrument_identity(body.identity, body.market_id)
    identity = parsed.get("identity", body.identity)
    ws(request).repository.unpin_instrument(body.market_id, identity)
    return {"unpinned": identity, "market_id": body.market_id}


@router.get("/api/universe/pins", dependencies=[Depends(authenticate)])
async def list_pins(request: Request, market_id: str) -> dict:
    return {"pins": ws(request).repository.list_pinned(market_id)}


# =====================================================================
# WHAT CHANGED (spec section 8)
# =====================================================================

@router.get("/api/changes", dependencies=[Depends(authenticate)])
async def changes(request: Request, domain: str | None = None,
                  limit: int = 100) -> dict:
    return {"changes": ws(request).changes.recent(domain=domain,
                                                   limit=min(limit, 500))}


# =====================================================================
# EVENT STREAM (spec section 71) — fetch-streamed, bearer-authenticated
# =====================================================================

@router.get("/api/stream", dependencies=[Depends(authenticate)])
async def event_stream(request: Request, topics: str | None = None,
                       last_seq: int | None = None):
    workstation = ws(request)
    wanted = tuple(t.strip() for t in (topics or "").split(",") if t.strip())

    async def generate():
        if last_seq is not None:
            for event in workstation.bus.replay_since(last_seq):
                if not wanted or event["type"] in wanted:
                    yield _sse(event)
        async for event in workstation.bus.iterate(wanted, idle_timeout=600):
            yield _sse(event)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache",
                                      "x-accel-buffering": "no"})


@router.get("/api/stream/health", dependencies=[Depends(authenticate)])
async def stream_health(request: Request) -> dict:
    return ws(request).bus.health()


def _sse(event: dict) -> str:
    return f"id: {event['seq']}\nevent: {event['type']}\n" \
           f"data: {json.dumps(event, default=str)}\n\n"


# =====================================================================
# OPPORTUNITY CENTER (spec sections 14, 139, 140)
# =====================================================================

class ObserveBody(BaseModel):
    market_id: str
    identity: str
    price_change_pct: float | None = None
    volume_ratio: float | None = None
    volatility_expansion: bool = False
    regime_change: str | None = None
    # None -> derive from the agreement ledger (spec §98); explicit values
    # from real observations are still honoured.
    model_disagreement: bool | None = None
    user_focus: bool = False
    evidence_source: str = "manual"


@router.get("/api/opportunities", dependencies=[Depends(authenticate)])
async def opportunities(request: Request, market_id: str | None = None,
                        state: str | None = None) -> dict:
    workstation = ws(request)
    expired = workstation.opportunities.expire_stale()
    for opportunity in expired:
        workstation.expired_recorder.record_expired(
            opportunity=opportunity.to_dict(now=workstation.opportunities.clock()))
    return {"opportunities": workstation.opportunities.list(market_id=market_id,
                                                            state=state),
            "counts": workstation.opportunities.counts(),
            "expired_this_call": len(expired)}


@router.post("/api/opportunities/observe", dependencies=[Depends(authenticate)])
async def observe_opportunity(body: ObserveBody, request: Request) -> dict:
    """Record an OBSERVED trigger. The body must carry real observations —
    fabricated inputs only produce fabricated research (and are the caller's
    responsibility); nothing here invents a trigger on its own."""
    workstation = ws(request)
    disagreement = body.model_disagreement
    if disagreement is None:
        # Derived, never invented: the ledger's latest multi-source verdict.
        disagreement = workstation.agreement.summary(
            body.market_id, body.identity).get("verdict") == "DISAGREEMENT"
    opportunity = workstation.opportunities.observe(
        market_id=body.market_id, identity=body.identity,
        price_change_pct=body.price_change_pct, volume_ratio=body.volume_ratio,
        volatility_expansion=body.volatility_expansion,
        regime_change=body.regime_change,
        model_disagreement=disagreement, user_focus=body.user_focus)
    if opportunity is None:
        return {"detected": False,
                "reason": "no trigger crossed the attention thresholds",
                "attention_mode": workstation.opportunities.attention_mode}
    return {"detected": True, "opportunity": opportunity.to_dict(now=time.time())}


class TransitionBody(BaseModel):
    state: str
    reason: str = ""


@router.post("/api/opportunities/{opportunity_id}/transition",
             dependencies=[Depends(authenticate)])
async def transition_opportunity(opportunity_id: str, body: TransitionBody,
                                 request: Request) -> dict:
    try:
        opportunity = ws(request).opportunities.transition(
            opportunity_id, body.state, reason=body.reason)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return opportunity.to_dict(now=time.time())


# =====================================================================
# EVIDENCE STACK + ANALOGUES (spec sections 10, 16, 141)
# =====================================================================

class EvidenceInput(BaseModel):
    layer: str
    kind: str
    value: dict | None = None
    provenance: str
    status: str | None = None


@router.get("/api/evidence/{market_id}/{identity}", dependencies=[Depends(authenticate)])
async def evidence_view(market_id: str, identity: str, request: Request) -> dict:
    """Assemble the evidence stack for one instrument from recorded data.

    Layers with no backing data are reported UNAVAILABLE with the reason —
    never fabricated (spec section 93).
    """
    workstation = ws(request)
    stack = workstation.evidence_stack(market_id, identity)
    closes, volumes = _recorded_series(workstation, identity)
    if closes:
        stack.set("market_state", kind="OBSERVED",
                  value={"bars": len(closes), "last_close": closes[-1],
                         "source": "recorded candles"},
                  provenance="history candle store")
        try:
            features = workstation.analogues.features_for(closes, volumes or None)
            stack.set("feature_state", kind="CALCULATED", value=features,
                      provenance="analogue feature extractor over recorded closes")
        except ValueError as exc:
            stack.set("feature_state", kind="CALCULATED", value=None,
                      provenance="analogue feature extractor",
                      unavailable_reason=str(exc))
        try:
            search = workstation.analogues.search(identity, closes, volumes or None)
            stack.set("historical_analogues", kind="HISTORICAL",
                      value={**search.to_dict(),
                             "summary": workstation.analogues.summarize(search)},
                      provenance=search.methodology)
        except ValueError as exc:
            stack.set("historical_analogues", kind="HISTORICAL", value=None,
                      provenance="analogue engine", unavailable_reason=str(exc))
    else:
        for layer in ("market_state", "feature_state", "historical_analogues"):
            stack.set(layer, kind="OBSERVED" if layer == "market_state" else "CALCULATED",
                      value=None, provenance="recorded candles",
                      unavailable_reason="no recorded bars for this instrument")
    # Kronos: truthful status only.
    try:
        service = getattr(request.app.state, "forecast_service", None)
        snapshot = service.latest(identity, "1D") if service else None
    except Exception:
        snapshot = None
    if snapshot is not None:
        stack.set("kronos_forecast", kind="MODEL",
                  value={"note": "MODEL FORECAST — not observed market data",
                         "snapshot": getattr(snapshot, "to_dict", dict)()},
                  provenance="isolated Kronos forecast service")
    else:
        stack.set("kronos_forecast", kind="MODEL", value=None,
                  provenance="isolated Kronos forecast service",
                  unavailable_reason="no forecast available (weights not "
                                     "verified or no run yet)")
    # Risk layer from settings.
    stack.set("risk", kind="CALCULATED",
              value={"max_position_value": workstation.settings.max_position_value,
                     "max_daily_loss": workstation.settings.max_daily_loss,
                     "max_open_positions": workstation.settings.max_open_positions},
              provenance="configured hard risk limits")
    # Strategy evidence + regime: explicit UNAVAILABLE until the research
    # pipeline feeds them (honest gaps beat fabricated values).
    stack.set("strategy_evidence", kind="CALCULATED", value=None,
              provenance="strategy router", unavailable_reason="not evaluated yet")
    stack.set("regime", kind="CALCULATED", value=None,
              provenance="regime engine", unavailable_reason="not evaluated yet")
    return stack.to_dict()


@router.post("/api/evidence/{market_id}/{identity}/recommend",
             dependencies=[Depends(authenticate)])
async def evidence_recommend(market_id: str, identity: str, request: Request,
                             state: str, rationale: str = "") -> dict:
    workstation = ws(request)
    stack = workstation.evidence_stack(market_id, identity)
    try:
        return stack.recommend(state, rationale=rationale or "manual classification")
    except ValueError as exc:
        raise HTTPException(422, str(exc))


class AnalogueBody(BaseModel):
    market_id: str
    identity: str
    lookback: int = 20
    horizon: int = 5
    top_k: int = 5
    include_negatives: bool = True


@router.post("/api/analogues/search", dependencies=[Depends(authenticate)])
async def analogue_search(body: AnalogueBody, request: Request) -> dict:
    from app.intel.analogues import AnalogueEngine

    workstation = ws(request)
    closes, volumes = _recorded_series(workstation, body.identity)
    if not closes:
        return {"identity": body.identity, "results": [], "sample_size": 0,
                "limitations": ["no recorded bars for this instrument"],
                "enough_history": False}
    engine = AnalogueEngine(lookback=body.lookback, horizon=body.horizon,
                            top_k=body.top_k)
    try:
        search = engine.search(body.identity, closes, volumes or None,
                               include_negatives=body.include_negatives)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {**search.to_dict(), "summary": engine.summarize(search)}


def _recorded_series(workstation, identity: str) -> tuple[list[float], list[float]]:
    """Best-effort recorded close/volume series for an instrument identity.

    Returns ([], []) when nothing is recorded — callers must treat that as a
    real absence, never as zero data.
    """
    from app.history.store import CandleStore

    store = CandleStore(workstation.data_dir / "candles")
    symbol = identity.split("|")[-1]
    rows = store.read_range(symbol, unit="days", interval=1)
    closes = [float(r["close"]) for r in rows]
    volumes = [float(r.get("volume", 0.0)) for r in rows]
    return closes, volumes


# =====================================================================
# RESEARCH MEMORY + MISSED OPPORTUNITIES (spec sections 11, 20, 21)
# =====================================================================

class MemoryBody(BaseModel):
    topic: str
    kind: str
    payload: dict
    provenance: str
    confidence: float = Field(ge=0.0, le=1.0)
    market_id: str | None = None
    experiment_id: str | None = None


@router.post("/api/research/memory", dependencies=[Depends(authenticate)])
async def research_memory_add(body: MemoryBody, request: Request) -> dict:
    memory_id = ws(request).repository.record_research_memory(
        topic=body.topic, kind=body.kind, payload=body.payload,
        provenance=body.provenance, confidence=body.confidence,
        market_id=body.market_id, experiment_id=body.experiment_id)
    return {"id": memory_id}


@router.get("/api/research/memory", dependencies=[Depends(authenticate)])
async def research_memory_search(request: Request, q: str = "",
                                 market_id: str | None = None,
                                 kind: str | None = None, limit: int = 50) -> dict:
    return {"results": ws(request).repository.search_research_memory(
        query=q, market_id=market_id, kind=kind, limit=min(limit, 200))}


@router.get("/api/research/missed", dependencies=[Depends(authenticate)])
async def missed_opportunities(request: Request) -> dict:
    workstation = ws(request)
    return {"missed": workstation.missed.list(), "stats": workstation.missed.stats()}


class MissedBody(BaseModel):
    kind: str
    payload: dict
    knowable: bool
    market_id: str | None = None
    identity: str | None = None
    counterfactual: str | None = None
    research_question: str | None = None


@router.post("/api/research/missed", dependencies=[Depends(authenticate)])
async def missed_record(body: MissedBody, request: Request) -> dict:
    try:
        missed_id = ws(request).missed.record(
            kind=body.kind, payload=body.payload, knowable=body.knowable,
            market_id=body.market_id, identity=body.identity,
            counterfactual=body.counterfactual,
            research_question=body.research_question)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"id": missed_id}


@router.get("/api/research/next-best", dependencies=[Depends(authenticate)])
async def next_best_experiment(request: Request) -> dict:
    """Deterministic next-best-experiment proposal (spec section 20).

    Derived from recorded signals only: unresolved misses, failed experiments
    and opportunity density. Never fabricates expected information gain.
    """
    workstation = ws(request)
    stats = workstation.missed.stats()
    experiments = workstation.repository.list_experiments(limit=20) \
        if hasattr(workstation.repository, "list_experiments") else []
    failed = [e for e in experiments if str(e.get("status", "")).lower() == "failed"]
    suggestions = []
    by_kind = stats.get("by_kind", {})
    if by_kind.get("false_negative"):
        suggestions.append({
            "hypothesis": "detection thresholds miss setups that later qualified",
            "motivation": f"{by_kind['false_negative']} recorded false negatives",
            "expected_information_gain": "qualitative (threshold sensitivity study)",
            "compute_budget": "low"})
    if by_kind.get("late_promotion"):
        suggestions.append({
            "hypothesis": "pipeline latency lets opportunities expire before action",
            "motivation": f"{by_kind['late_promotion']} expired candidates",
            "expected_information_gain": "qualitative (latency budget audit)",
            "compute_budget": "low"})
    if failed:
        suggestions.append({
            "hypothesis": f"re-run the last failed experiment "
                          f"({failed[0].get('experiment_id')}) with diagnostics",
            "motivation": f"{len(failed)} failed experiments on record",
            "expected_information_gain": "failure-mode classification",
            "compute_budget": "medium"})
    if not suggestions:
        suggestions.append({"hypothesis": "no research debt recorded",
                            "motivation": "missed-opportunity store is empty and "
                                          "no failed experiments",
                            "expected_information_gain": "none recorded",
                            "compute_budget": "none"})
    return {"suggestions": suggestions,
            "inputs": {"missed_total": stats.get("total", 0),
                       "failed_experiments": len(failed)},
            "note": "proposals are deterministic summaries of recorded evidence"}


# =====================================================================
# ATTENTION MODE (spec section 15)
# =====================================================================

class AttentionBody(BaseModel):
    mode: str


@router.post("/api/settings/attention", dependencies=[Depends(authenticate)])
async def set_attention(body: AttentionBody, request: Request) -> dict:
    workstation = ws(request)
    try:
        profile = workstation.opportunities.set_attention_mode(body.mode)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    workstation.repository.set_engine_state("attention_mode",
                                            profile["attention_mode"])
    workstation.changes.observe("settings", "attention_mode",
                                {"mode": profile["attention_mode"]},
                                evidence="operator changed attention mode")
    return profile


# =====================================================================
# LIVE SESSION STATE MACHINE (spec sections 31-37, 135-137)
# =====================================================================

@router.get("/api/live/session", dependencies=[Depends(authenticate)])
async def live_session(request: Request) -> dict:
    workstation = ws(request)
    current = workstation.live_sessions.current()
    derived = workstation.live_sessions.derived_state(
        workstation.settings, repository=workstation.repository,
        emergency_stop=workstation.emergency_stop_active())
    return {"state": current.state, "reason": current.reason,
            "ts_entered": current.ts_entered,
            "derived_configuration_state": derived,
            "emergency_stop_active": workstation.emergency_stop_active(),
            "supervisor": workstation.live_supervisor.status(),
            "states": ["OFF", "CONFIGURED", "READY", "ARMED", "ACTIVE",
                       "PAUSED", "RECONCILING", "STOPPED", "BLOCKED"],
            "note": "ARMED/ACTIVE require explicit operator authorization; no "
                    "restart, recovery or background job can arm live trading"}


class ArmBody(BaseModel):
    confirmation_phrase: str


@router.post("/api/live/session/arm", dependencies=[Depends(authenticate)])
async def live_arm(body: ArmBody, request: Request) -> dict:
    workstation = ws(request)
    from app.config.settings import LIVE_CONFIRM_PHRASE
    from app.live.session import CONFIGURED, OFF, READY, STOPPED, LiveTransitionError
    from app.live.supervisor import LiveSupervisorError

    if body.confirmation_phrase != LIVE_CONFIRM_PHRASE:
        raise HTTPException(
            409, "Arming live trading requires the explicit confirmation phrase.")
    current = workstation.live_sessions.current()
    if current.state not in (OFF, CONFIGURED, READY, STOPPED):
        raise HTTPException(
            409, f"Cannot arm live trading from state {current.state}; "
                 "resolve or explicitly release the current session first.")
    if workstation.emergency_stop_active():
        raise HTTPException(409, "Cannot arm while the emergency stop is active.")

    try:
        # The child is spawned only by this explicit ARM action, never at
        # application startup. Readiness is tied to its real PID/lease and
        # completed broker reconciliation before the state machine can arm.
        await asyncio.to_thread(workstation.live_supervisor.start)
        snapshot = workstation.live_sessions.arm(
            workstation.settings, confirmation_phrase=body.confirmation_phrase,
            repository=workstation.repository)
    except (LiveTransitionError, LiveSupervisorError) as exc:
        try:
            await asyncio.to_thread(
                workstation.live_supervisor.request_stop,
                reason="ARM was refused; drain the supervised engine safely")
        except Exception as stop_exc:
            logger.error("ARM refusal stop request failed: %s",
                         type(stop_exc).__name__)
        raise HTTPException(409, str(exc))
    return {"state": snapshot.state, "reason": snapshot.reason,
            "supervisor": workstation.live_supervisor.status()}


@router.post("/api/live/session/activate", dependencies=[Depends(authenticate)])
async def live_activate(request: Request) -> dict:
    from app.live.session import LiveTransitionError

    try:
        snapshot = ws(request).live_sessions.activate()
    except LiveTransitionError as exc:
        raise HTTPException(409, str(exc))
    return {"state": snapshot.state, "reason": snapshot.reason}


@router.post("/api/live/session/pause", dependencies=[Depends(authenticate)])
async def live_pause(request: Request) -> dict:
    from app.live.session import LiveTransitionError

    try:
        snapshot = ws(request).live_sessions.pause()
    except LiveTransitionError as exc:
        raise HTTPException(409, str(exc))
    return {"state": snapshot.state, "reason": snapshot.reason}


@router.post("/api/live/session/resume", dependencies=[Depends(authenticate)])
async def live_resume(request: Request) -> dict:
    from app.live.session import LiveTransitionError

    try:
        snapshot = ws(request).live_sessions.resume()
    except LiveTransitionError as exc:
        raise HTTPException(409, str(exc))
    return {"state": snapshot.state, "reason": snapshot.reason}


class StopBody(BaseModel):
    reason: str = "operator stop"


@router.post("/api/live/session/stop", dependencies=[Depends(authenticate)])
async def live_stop(body: StopBody, request: Request) -> dict:
    snapshot = ws(request).live_sessions.stop(reason=body.reason)
    return {"state": snapshot.state, "reason": snapshot.reason}


@router.post("/api/live/session/disarm", dependencies=[Depends(authenticate)])
async def live_disarm(request: Request) -> dict:
    from app.live.session import LiveTransitionError

    try:
        snapshot = ws(request).live_sessions.disarm()
    except LiveTransitionError as exc:
        raise HTTPException(409, str(exc))
    return {"state": snapshot.state, "reason": snapshot.reason}


class EmergencyStopBody(BaseModel):
    reason: str = "operator emergency stop"


@router.post("/api/live/emergency-stop", dependencies=[Depends(authenticate)])
async def live_emergency_stop(body: EmergencyStopBody, request: Request) -> dict:
    """Persist the kill switch, invalidate live authority and request a safe drain.

    This control plane never sends a broker order itself. The supervised child
    cancels working orders when reachable and remains alive for risk-reducing
    exits until the live book is flat.
    """
    workstation = ws(request)
    from datetime import datetime

    reason = (body.reason or "operator emergency stop").strip()[:240]
    if not reason:
        reason = "operator emergency stop"
    try:
        workstation.repository.set_engine_state("emergency_stop", {
            "stopped": True, "ts": datetime.now(UTC).isoformat(),
            "reason": reason})
        workstation.repository.record_system_event(
            "EMERGENCY_STOP", {"reason": reason, "source": "workstation GUI"})
        snapshot = workstation.live_sessions.block(reason=f"emergency stop: {reason}")
    except Exception as exc:
        # A failed stop-flag read inside the engine/gateway is fail-closed.
        # Report the persistence failure without pretending the write landed.
        logger.error("GUI emergency stop persistence failed: %s",
                     type(exc).__name__)
        raise HTTPException(503, "Emergency stop state could not be confirmed; "
                                 "the engine fails closed on an unreadable flag.")
    return {"stopped": True, "state": snapshot.state,
            "reason": snapshot.reason,
            "note": "No position was auto-closed; risk-reducing exits remain enabled."}


class EmergencyClearBody(BaseModel):
    confirmation_phrase: str


@router.post("/api/live/emergency-stop/clear",
             dependencies=[Depends(authenticate)])
async def live_emergency_stop_clear(body: EmergencyClearBody,
                                    request: Request) -> dict:
    workstation = ws(request)
    from datetime import datetime

    from app.config.settings import LIVE_CONFIRM_PHRASE
    from app.live.session import ACTIVE, ARMED, BLOCKED, PAUSED, RECONCILING, LiveTransitionError

    if body.confirmation_phrase != LIVE_CONFIRM_PHRASE:
        raise HTTPException(
            409, "Clearing the emergency stop requires the explicit confirmation phrase.")
    manager = workstation.live_sessions
    current = manager.current()
    should_release = current.state in (ACTIVE, ARMED, PAUSED, RECONCILING) or (
        current.state == BLOCKED and "emergency stop" in current.reason.lower())
    try:
        # Revoke entry authority BEFORE making the flag clear. Clearing a
        # kill-switch can never resume an old ACTIVE epoch.
        if current.state in (ACTIVE, ARMED, PAUSED, RECONCILING):
            manager.block(reason="emergency stop cleared; fresh ARM required")
        workstation.repository.set_engine_state("emergency_stop", {
            "stopped": False, "ts": datetime.now(UTC).isoformat(),
            "reason": "cleared by operator; fresh ARM/ACTIVATE required"})
        workstation.repository.record_system_event(
            "EMERGENCY_STOP_CLEARED",
            {"source": "workstation GUI",
             "fresh_arm_required": True})
        if should_release and manager.current().state == BLOCKED:
            snapshot = manager.release_block(
                destination="STOPPED",
                reason="emergency stop cleared; fresh ARM and ACTIVATE required")
        else:
            snapshot = manager.current()
    except LiveTransitionError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        logger.error("GUI emergency-stop clear failed: %s", type(exc).__name__)
        raise HTTPException(503, "Emergency-stop clear could not be confirmed.")
    return {"stopped": False, "state": snapshot.state,
            "reason": snapshot.reason,
            "note": (f"Emergency flag cleared; session remains {snapshot.state}. "
                     "No prior authorization was restored; a new live cycle "
                     "requires readiness, fresh ARM and explicit ACTIVATE.")}


@router.get("/api/live/session/history", dependencies=[Depends(authenticate)])
async def live_history(request: Request, limit: int = 50) -> dict:
    return {"history": ws(request).live_sessions.history(limit=min(limit, 200))}


@router.get("/api/live/readiness-center", dependencies=[Depends(authenticate)])
async def live_readiness_center(request: Request) -> dict:
    from app.live import preflight as live_preflight

    workstation = ws(request)
    context: dict = {}
    try:
        last_ts = workstation.repository.get_engine_state("last_market_data_ts", None)
        if last_ts:
            context["last_market_data_ts"] = last_ts
    except Exception as exc:
        logger.debug("readiness context without last market data ts: %s", exc)
    checks = live_preflight.run_readiness_center_checks(
        workstation.settings, repository=workstation.repository, context=context)
    return _serialize_readiness(checks)


def _serialize_readiness(checks) -> dict:
    from app.live import preflight as live_preflight

    serialized = []
    for check in checks:
        if check.ok:
            status = "READY"
        elif check.severity == live_preflight.ERROR:
            status = "BLOCKED"
        elif check.severity == live_preflight.WARNING:
            status = "WARNING"
        else:
            status = "UNKNOWN"
        entry = check.to_dict()
        entry["status"] = status
        serialized.append(entry)
    blocked = [c for c in serialized if c["status"] == "BLOCKED"]
    unknown = [c for c in serialized if c["status"] == "UNKNOWN"]
    warnings = [c for c in serialized if c["status"] == "WARNING"]
    verdict = "READY"
    if blocked:
        verdict = "BLOCKED"
    elif unknown:
        verdict = "NOT_READY_UNVERIFIED"
    elif warnings:
        verdict = "READY_WITH_WARNINGS"
    return {"verdict": verdict, "checks": serialized,
            "required_phrase": live_preflight.LIVE_CONFIRM_PHRASE,
            "counts": {"ready": len(serialized) - len(blocked) - len(unknown) - len(warnings),
                       "warning": len(warnings), "blocked": len(blocked),
                       "unknown": len(unknown)}}


@router.get("/api/live/start-summary", dependencies=[Depends(authenticate)])
async def live_start_summary(request: Request) -> dict:
    workstation = ws(request)
    return workstation.live_sessions.start_confirmation_summary(
        workstation.settings, repository=workstation.repository,
        emergency_stop=workstation.emergency_stop_active())


@router.get("/api/live/guard-chain", dependencies=[Depends(authenticate)])
async def live_guard_chain(request: Request) -> dict:
    workstation = ws(request)
    chain = workstation.guard_chain()
    return {"guards": chain.guard_names,
            "live_state": workstation.live_sessions.current().state,
            "note": "every live ENTRY passes all guards in order; exits skip "
                    "entry-only guards so held positions are never trapped"}


@router.get("/api/live/rate-limit", dependencies=[Depends(authenticate)])
async def live_rate_limit(request: Request) -> dict:
    limiter = ws(request).rate_limiter
    return {**limiter.stats(), "current": limiter.counts(),
            "note": "limits are configurable policy, not eternal constants"}


# =====================================================================
# KRONOS STUDIO (spec section 18)
# =====================================================================

def _forecast_service_payload(request: Request) -> dict:
    environment = getattr(request.app.state, "kronos_environment", None)
    service = getattr(request.app.state, "forecast_service", None)
    setup_error = getattr(request.app.state, "kronos_service_error", "")
    if service is not None:
        running = False
        try:
            running = bool(service.running)
            status = service.status()
            status_payload = status.to_dict()
            available = bool(running and status.is_ready)
        except Exception as exc:
            available = False
            status_payload = {"state": "ERROR",
                              "message": f"forecast status unavailable ({type(exc).__name__})"}
        return {
            "configured": True,
            "available": available,
            "running": running,
            "queued": int(service.queued),
            "status": status_payload,
            "series": ([{"instrument": instrument, "timeframe": timeframe}
                        for instrument, timeframe in environment.series]
                       if environment is not None else []),
            "notes": list(environment.notes) if environment is not None else [],
        }
    if environment is not None:
        return {"configured": environment.configured,
                "available": False, "running": False, "queued": 0,
                "status": environment.status.to_dict(),
                "series": [{"instrument": instrument, "timeframe": timeframe}
                           for instrument, timeframe in environment.series],
                "notes": list(environment.notes)}
    return {"configured": False, "available": False, "running": False,
            "queued": 0,
            "status": {"state": "ERROR" if setup_error else "DISABLED",
                       "message": (f"forecast service composition failed ({setup_error})"
                                   if setup_error else "Kronos service is not configured")},
            "series": [], "notes": []}


@router.get("/api/kronos/studio", dependencies=[Depends(authenticate)])
async def kronos_studio(request: Request) -> dict:
    overview = ws(request).studio.overview()
    overview["forecast_service"] = _forecast_service_payload(request)
    return overview


@router.get("/api/kronos/service", dependencies=[Depends(authenticate)])
async def kronos_service_status(request: Request) -> dict:
    return _forecast_service_payload(request)


class ForecastRefreshBody(BaseModel):
    instrument: str = Field(min_length=1, max_length=200)
    timeframe: str = Field(min_length=2, max_length=4)
    force: bool = False


@router.post("/api/kronos/forecast/refresh", dependencies=[Depends(authenticate)])
async def kronos_forecast_refresh(body: ForecastRefreshBody, request: Request) -> dict:
    from axiom.models import TIMEFRAME_SECONDS

    instrument = body.instrument.strip()
    service = getattr(request.app.state, "forecast_service", None)
    if not instrument:
        raise HTTPException(422, "instrument must not be blank")
    if body.timeframe not in TIMEFRAME_SECONDS:
        raise HTTPException(422, "unsupported timeframe")
    if service is None:
        current = _forecast_service_payload(request)
        return {"ok": False, "accepted": False,
                "status": current["status"].get("state", "DISABLED"),
                "detail": current["status"].get("message", "forecast service unavailable")}
    accepted = service.request_refresh(instrument, body.timeframe, force=body.force)
    return {"ok": accepted, "accepted": accepted,
            "status": service.status().state.value,
            "queued": service.queued,
            "detail": ("forecast request queued" if accepted
                       else "forecast queue is full; request was not accepted")}


class ModelAction(BaseModel):
    model_id: str


@router.post("/api/kronos/studio/download", dependencies=[Depends(authenticate)])
async def kronos_download(body: ModelAction, request: Request) -> dict:
    try:
        return ws(request).studio.download(body.model_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@router.post("/api/kronos/studio/verify", dependencies=[Depends(authenticate)])
async def kronos_verify(body: ModelAction, request: Request) -> dict:
    try:
        return ws(request).studio.verify(body.model_id)
    except Exception as exc:
        raise HTTPException(422, str(exc))


@router.post("/api/kronos/studio/install", dependencies=[Depends(authenticate)])
async def kronos_install(body: ModelAction, request: Request) -> dict:
    try:
        return ws(request).studio.install(body.model_id)
    except Exception as exc:
        raise HTTPException(422, str(exc))


@router.post("/api/kronos/studio/benchmark", dependencies=[Depends(authenticate)])
async def kronos_benchmark(body: ModelAction, request: Request) -> dict:
    # No inference runner is attached in this build: the honest result is
    # NOT_ATTEMPTED (benchmarks are never fabricated).
    try:
        return ws(request).studio.benchmark(body.model_id)
    except Exception as exc:
        raise HTTPException(422, str(exc))


@router.post("/api/kronos/studio/activate", dependencies=[Depends(authenticate)])
async def kronos_activate(body: ModelAction, request: Request) -> dict:
    try:
        return ws(request).studio.activate(body.model_id)
    except Exception as exc:
        raise HTTPException(422, str(exc))


# =====================================================================
# SYSTEM CONTROL CENTER (spec sections 57, 58, 102)
# =====================================================================

@router.get("/api/system/control", dependencies=[Depends(authenticate)])
async def system_control(request: Request) -> dict:
    workstation = ws(request)
    from app.monitoring.system import SystemMonitor

    monitor = SystemMonitor(workstation.repository, data_dir=str(workstation.data_dir))
    try:
        host = monitor.snapshot()
    except Exception as exc:
        host = {"error": f"system snapshot unavailable: {exc}"}
    return {
        "host": host,
        "resources": workstation.governor.snapshot(),
        "pressure": workstation.governor.under_pressure(),
        "latency": workstation.latency.overview(),
        "tasks": workstation.tasks.counts(),
        "stream": workstation.bus.health(),
        "components": {
            "core": "RUNNING",
            "market_engine": "RUNNING",
            "kronos": "ISOLATED_WORKER",
            "database": "RUNNING",
            "live_session": workstation.live_sessions.current().state,
            "emergency_stop": workstation.emergency_stop_active(),
        },
    }


@router.get("/api/tasks", dependencies=[Depends(authenticate)])
async def tasks_list(request: Request, state: str | None = None) -> dict:
    return {"tasks": ws(request).tasks.list(state=state),
            "counts": ws(request).tasks.counts()}


class TaskBody(BaseModel):
    name: str
    type: str
    payload: dict | None = None


@router.post("/api/tasks", dependencies=[Depends(authenticate)])
async def task_submit(body: TaskBody, request: Request) -> dict:
    try:
        task = ws(request).tasks.submit(body.name, body.type, payload=body.payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return task.to_dict()


class TaskAction(BaseModel):
    action: str  # start | pause | resume | cancel | complete | fail


@router.post("/api/tasks/{task_id}", dependencies=[Depends(authenticate)])
async def task_action(task_id: str, body: TaskAction, request: Request) -> dict:
    tasks = ws(request).tasks
    try:
        if body.action == "start":
            allowed = tasks.may_start(task_id)
            return {"started": allowed, "task": tasks.get(task_id)}
        if body.action == "pause":
            return tasks.pause(task_id).to_dict()
        if body.action == "resume":
            return tasks.resume(task_id).to_dict()
        if body.action == "cancel":
            return tasks.cancel(task_id).to_dict()
        if body.action == "complete":
            return tasks.complete(task_id).to_dict()
        if body.action == "fail":
            return tasks.fail(task_id, "operator marked failed").to_dict()
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    raise HTTPException(422, f"unknown task action '{body.action}'")


@router.get("/api/latency", dependencies=[Depends(authenticate)])
async def latency_overview(request: Request) -> dict:
    workstation = ws(request)
    return {"stages": workstation.latency.overview(),
            "daily": workstation.repository.list_latency_summaries(limit=100)}


# =====================================================================
# BACKUP / RESTORE (spec section 68)
# =====================================================================

class BackupBody(BaseModel):
    note: str | None = None


@router.post("/api/backup/create", dependencies=[Depends(authenticate)])
async def backup_create(body: BackupBody, request: Request) -> dict:
    try:
        return ws(request).backup.create(note=body.note)
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(409, str(exc))


@router.get("/api/backup/list", dependencies=[Depends(authenticate)])
async def backup_list(request: Request) -> dict:
    return {"backups": ws(request).backup.list()}


class BackupTarget(BaseModel):
    path: str


@router.post("/api/backup/verify", dependencies=[Depends(authenticate)])
async def backup_verify(body: BackupTarget, request: Request) -> dict:
    path = _confine_backup_path(ws(request), body.path)
    return ws(request).backup.verify(path)


@router.post("/api/backup/restore", dependencies=[Depends(authenticate)])
async def backup_restore(body: BackupTarget, request: Request) -> dict:
    path = _confine_backup_path(ws(request), body.path)
    return ws(request).backup.restore(path)


def _confine_backup_path(workstation, raw: str):
    from pathlib import Path

    candidate = Path(raw)
    backup_dir = workstation.backup.backup_dir.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(backup_dir)
    except ValueError:
        raise HTTPException(422, "backup path must stay inside the backup directory") from None
    if not resolved.exists():
        raise HTTPException(404, "backup archive not found")
    return resolved


# =====================================================================
# GUI UPDATE SYSTEM (spec section 69): CHECK→DOWNLOAD→VERIFY→PREPARE→INSTALL
# =====================================================================

class UpdateDownloadBody(BaseModel):
    url: str
    filename: str


class UpdateVerifyBody(BaseModel):
    path: str
    expected_sha256: str


class UpdatePrepareBody(BaseModel):
    path: str


@router.get("/api/system/update/check", dependencies=[Depends(authenticate)])
async def update_check(request: Request) -> dict:
    return ws(request).updater.check()


@router.post("/api/system/update/download", dependencies=[Depends(authenticate)])
async def update_download(body: UpdateDownloadBody, request: Request) -> dict:
    return ws(request).updater.download(body.url, filename=body.filename)


@router.post("/api/system/update/verify", dependencies=[Depends(authenticate)])
async def update_verify(body: UpdateVerifyBody, request: Request) -> dict:
    return ws(request).updater.verify(body.path, body.expected_sha256)


@router.post("/api/system/update/prepare", dependencies=[Depends(authenticate)])
async def update_prepare(body: UpdatePrepareBody, request: Request) -> dict:
    return ws(request).updater.prepare(body.path)


@router.post("/api/system/update/install", dependencies=[Depends(authenticate)])
async def update_install(request: Request) -> dict:
    return ws(request).updater.install()


@router.get("/api/system/update/status", dependencies=[Depends(authenticate)])
async def update_status(request: Request) -> dict:
    return ws(request).updater.status()


# =====================================================================
# DRIFT DETECTION (spec section 25)
# =====================================================================

@router.get("/api/research/drift", dependencies=[Depends(authenticate)])
async def research_drift(market_id: str, identity: str, request: Request) -> dict:
    from app.intel.drift import DriftDetector

    workstation = ws(request)
    closes, _ = _recorded_series(workstation, identity)
    report = DriftDetector().evaluate(identity, closes)
    return report.to_dict()


# =====================================================================
# RESEARCH SCHEDULER — BOUNDED LOOP (spec sections 96, 97)
# =====================================================================

class ResearchAdmitBody(BaseModel):
    kind: str = "backtest"


@router.get("/api/research/schedule", dependencies=[Depends(authenticate)])
async def research_schedule(request: Request) -> dict:
    return ws(request).research_scheduler.status()


@router.post("/api/research/schedule/admit", dependencies=[Depends(authenticate)])
async def research_schedule_admit(body: ResearchAdmitBody, request: Request) -> dict:
    return ws(request).research_scheduler.admit(body.kind)


# =====================================================================
# RECORDED CANDLES FOR CHARTS (spec sections 49, 6)
# =====================================================================

@router.get("/api/candles/{market_id}/{identity}",
            dependencies=[Depends(authenticate)])
async def recorded_candles(market_id: str, identity: str, request: Request,
                           unit: str = "days", interval: int = 1,
                           limit: int = 300) -> dict:
    """Recorded candles only — never resampled from nothing, never
    interpolated, never synthesised. An empty result means no recording."""
    workstation = ws(request)
    from app.history.store import CandleStore

    store = CandleStore(workstation.data_dir / "candles")
    symbol = identity.split("|")[-1]
    rows = store.read_range(symbol, unit=unit, interval=interval)
    bounded = max(1, min(int(limit), 2000))
    tail = rows[-bounded:]
    return {"market_id": market_id, "identity": identity, "unit": unit,
            "interval": interval, "count": len(tail),
            "candles": tail,
            "source": "recorded candles (OBSERVED)" if tail else "none recorded"}


# =====================================================================
# MODEL AGREEMENT / DISAGREEMENT (spec section 98)
# =====================================================================

class AgreementVoteBody(BaseModel):
    market_id: str
    identity: str
    stances: dict                       # source -> -1 | 0 | +1
    horizon: int = Field(default=5, ge=1)
    regime: str | None = None
    provenance: str = "unspecified"


@router.post("/api/research/agreement", dependencies=[Depends(authenticate)])
async def research_agreement_vote(body: AgreementVoteBody, request: Request) -> dict:
    from app.intel.agreement import save_votes

    workstation = ws(request)
    try:
        record = workstation.agreement.record(
            body.market_id, body.identity, body.stances, horizon=body.horizon,
            regime=body.regime, provenance=body.provenance)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    save_votes(workstation.data_dir / "agreement_votes.json",
               workstation.agreement)
    return {"ok": True, "record": record.to_dict(),
            "summary": workstation.agreement.summary(body.market_id,
                                                     body.identity,
                                                     body.horizon)}


@router.get("/api/research/agreement", dependencies=[Depends(authenticate)])
async def research_agreement_summary(market_id: str, identity: str,
                                     request: Request,
                                     horizon: int | None = None) -> dict:
    return ws(request).agreement.summary(market_id, identity, horizon)


@router.get("/api/research/agreement/watchlist",
            dependencies=[Depends(authenticate)])
async def research_agreement_watchlist(request: Request) -> dict:
    items = ws(request).agreement.disagreement_watchlist()
    return {"count": len(items), "disagreements": items,
            "note": "disagreement is surfaced for research attention, "
                    "never treated as a failure"}


# =====================================================================
# COUNTERFACTUAL SIMULATION (spec section 99)
# =====================================================================

class CounterfactualBody(BaseModel):
    market_id: str
    identity: str
    entry_index: int = Field(ge=0)
    horizon: int = Field(default=5, ge=1)
    size: float = Field(default=1.0, gt=0)
    timing_offsets: list[int] = [-2, -1, 1, 2]
    sizes: list[float] = [0.5, 2.0]


@router.post("/api/research/counterfactual",
             dependencies=[Depends(authenticate)])
async def research_counterfactual(body: CounterfactualBody,
                                  request: Request) -> dict:
    from app.intel.counterfactual import CounterfactualSimulator

    workstation = ws(request)
    closes, _ = _recorded_series(workstation, body.identity)
    if not closes:
        return {"identity": body.identity, "label": "NO_DATA",
                "variants": [],
                "note": "no recorded bars for this instrument — nothing was "
                        "simulated and nothing was fabricated"}
    try:
        result = CounterfactualSimulator(closes).compare(
            body.entry_index, horizon=body.horizon, size=body.size,
            timing_offsets=tuple(body.timing_offsets),
            sizes=tuple(body.sizes))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"identity": body.identity, "market_id": body.market_id, **result}
