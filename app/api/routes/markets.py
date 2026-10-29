"""Unified Multi-Market Terminal API (NSE INR + NASDAQ USD, Paper & Gated Live).

Powers the AXIOM multi-market operator desktop terminal across:
- NSE (Upstox read-only market data + BOD master; separate gated UPSTOX_LIVE)
- NASDAQ (Alpaca read-only market data + asset catalog; separate gated ALPACA_LIVE)
- Local ``LIVE_MARKET_PAPER`` simulation on real read-only feeds (zero broker orders)
- Global atomic Emergency Stop preserving confirmed position protection.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.autonomy.config import AutonomySettings
from app.autonomy.upstox import InstrumentCatalog, ReconciliationFailure, UpstoxAuthError, UpstoxReadOnly
from app.autonomy.upstox_token import resolve_live_token, resolve_token
from app.data.alpaca import AlpacaHistoricalProvider, feed_label, validate_us_symbol
from app.data.models import TIMEFRAMES
from app.market_data.provider_verification import (
    build_provider_matrix,
    verify_alpaca_market_data,
    verify_upstox_market_data,
)
from app.markets.alpaca_credentials import resolve_alpaca_data_credentials
from app.markets.nse_reconciliation import nse_session_status
from app.markets.us_calendar import USEquitiesCalendar

router = APIRouter(prefix="/api/markets")


class WatchlistItemBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    symbol: str = Field(min_length=1, max_length=64)


class PaperSessionStartBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    strategy: str = Field(default="manual_only", max_length=40)
    timeframe: str = Field(default="5m", max_length=8)
    symbols: list[str] = Field(default_factory=list)


class PaperSessionTransitionBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    action: str = Field(pattern="^(pause|resume|stop)$")
    reason: str = Field(default="", max_length=200)


class PaperResetBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    confirm: bool = False


class PaperConfigBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    starting_cash: float | None = Field(default=None, gt=0)
    max_order_value: float | None = Field(default=None, gt=0)
    max_symbol_exposure: float | None = Field(default=None, gt=0)
    max_total_exposure: float | None = Field(default=None, gt=0)
    max_open_positions: int | None = Field(default=None, gt=0)
    max_daily_loss: float | None = Field(default=None, gt=0)
    max_daily_orders: int | None = Field(default=None, gt=0)
    max_quote_age_seconds: float | None = Field(default=None, gt=0)
    spread_bps: float | None = Field(default=None, ge=0)
    slippage_bps: float | None = Field(default=None, ge=0)
    brokerage_bps: float | None = Field(default=None, ge=0)


class PaperOrderBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    symbol: str = Field(min_length=1, max_length=64)
    side: str = Field(pattern="^(BUY|SELL|buy|sell)$")
    qty: float = Field(gt=0)
    order_type: str = Field(default="MARKET", pattern="^(MARKET|LIMIT|STOP|STOP_LIMIT|market|limit|stop|stop_limit)$")
    product: str = Field(default="D", max_length=12)
    time_in_force: str = Field(default="DAY", pattern="^(DAY|GTC|IOC|day|gtc|ioc)$")
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    client_order_id: str | None = Field(default=None, max_length=64)
    allow_off_hours_working: bool = False


class PaperReplaceBody(BaseModel):
    qty: float | None = Field(default=None, gt=0)
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)


class PaperProtectionBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    symbol: str = Field(min_length=1, max_length=64)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)


class GlobalEmergencyBody(BaseModel):
    reason: str = Field(default="operator global emergency stop", max_length=200)


class GlobalResetBody(BaseModel):
    confirm_phrase: str = Field(default="", max_length=80)


def _paper_engine(request: Request):
    eng = getattr(request.app.state, "live_paper_engine", None)
    if eng is None:
        raise HTTPException(503, "Live-Market Paper engine is not initialized")
    return eng


def _nasdaq_catalog(request: Request):
    cat = getattr(request.app.state, "nasdaq_catalog", None)
    if cat is None:
        raise HTTPException(503, "NASDAQ catalog is not initialized")
    return cat


def _nse_search_catalog(request: Request) -> InstrumentCatalog:
    autonomy = getattr(request.app.state, "autonomy", None)
    if autonomy is not None and autonomy.catalog.instruments:
        return autonomy.catalog
    seed_cat = getattr(request.app.state, "nse_seed_catalog", None)
    if seed_cat is not None:
        return seed_cat
    fallback = InstrumentCatalog(AutonomySettings(), request.app.state.data_dir / "nse-seed-fallback.json")
    fallback.load_seed_fallback()
    request.app.state.nse_seed_catalog = fallback
    return fallback


def _upstox_readonly_token(request: Request) -> tuple[str, str]:
    tok, src = resolve_token(getattr(request.app.state, "upstox_token", None))
    if tok:
        return tok, src
    env_tok = str(getattr(request.app.state.settings, "market_data_access_token", "") or "").strip()
    if env_tok:
        return env_tok, "environment"
    return "", "none"


def _alpaca_readonly_creds(request: Request) -> tuple[dict[str, str], str]:
    return resolve_alpaca_data_credentials(
        getattr(request.app.state, "alpaca_data_store", None),
        request.app.state.settings,
    )


def _live_status_summary(request: Request) -> dict[str, Any]:
    autonomy = getattr(request.app.state, "autonomy", None)
    upstox_live_tok, upstox_live_src = resolve_live_token(getattr(request.app.state, "upstox_live_token", None))
    autonomy_sessions = autonomy.sessions() if autonomy is not None else []
    nse_live_flag = bool(getattr(getattr(autonomy, "settings", None), "live_enabled", False))
    nse_algo_name = bool(str(getattr(getattr(autonomy, "settings", None), "algo_name", "") or "").strip())

    broker_rt = getattr(request.app.state, "alpaca_broker_runtime", None)
    alpaca_svc = getattr(request.app.state, "alpaca_broker", None) or (broker_rt.service if broker_rt else None)
    alpaca_creds = broker_rt.credential_status() if broker_rt else None
    us_live_flag = bool(request.app.state.settings.alpaca_broker_enabled)
    us_live_creds = bool(alpaca_creds and alpaca_creds["live"]["present"])

    return {
        "default_live_execution": "OFF",
        "nse": {
            "market": "NSE",
            "currency": "INR",
            "broker": "Upstox (Protected Multi-Leg GTT v3)",
            "mode_id": "UPSTOX_LIVE",
            "live_master_flag": nse_live_flag,
            "live_credential_present": bool(upstox_live_tok),
            "live_credential_source": upstox_live_src,
            "algo_name_configured": nse_algo_name,
            "availability": (
                "AVAILABLE_BEHIND_GATE"
                if (nse_live_flag and upstox_live_tok and nse_algo_name)
                else "NOT_AVAILABLE"
            ),
            "missing_dependencies": [
                dep
                for ok, dep in (
                    (nse_live_flag, "Set AUTONOMY_LIVE_ENABLED=true in host environment"),
                    (bool(upstox_live_tok), "Save separate UPSTOX_LIVE_ACCESS_TOKEN in Connections"),
                    (nse_algo_name, "Set UPSTOX_ALGO_NAME and register SEBI static IP on Upstox"),
                )
                if not ok
            ],
            "autonomy_session": autonomy_sessions[0] if autonomy_sessions else None,
        },
        "nasdaq": {
            "market": "NASDAQ",
            "currency": "USD",
            "broker": "Alpaca Trading API v2",
            "mode_id": "ALPACA_LIVE",
            "live_master_flag": us_live_flag,
            "live_credential_present": us_live_creds,
            "live_credential_source": alpaca_creds["live"]["source"] if alpaca_creds else "none",
            "availability": (
                "AVAILABLE_BEHIND_GATE"
                if (us_live_flag and us_live_creds)
                else "NOT_AVAILABLE"
            ),
            "missing_dependencies": [
                dep
                for ok, dep in (
                    (us_live_flag, "Set ALPACA_BROKER_ENABLED=true in host environment"),
                    (us_live_creds, "Save separate ALPACA_BROKER_LIVE key & secret in Connections"),
                )
                if not ok
            ],
            "broker_status": alpaca_svc.status() if alpaca_svc is not None else None,
        },
    }


@router.get("/overview")
def markets_overview(request: Request):
    eng = _paper_engine(request)
    nse_cat = _nse_search_catalog(request)
    nasdaq_cat = _nasdaq_catalog(request)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "product_modes": [
            {
                "id": "LIVE_MARKET_PAPER",
                "label": "LIVE-MARKET PAPER (NSE INR + NASDAQ USD)",
                "simulated": True,
                "real_money": False,
                "default": True,
                "description": "Local simulated execution driven by live read-only Upstox & Alpaca market data; zero broker order calls.",
            },
            {
                "id": "LIVE_MARKET_LIVE",
                "label": "LIVE-MARKET LIVE (Gated Real-Money Execution)",
                "simulated": False,
                "real_money": True,
                "default": False,
                "description": "Separate broker execution behind distinct credentials, bounded risk limits, fresh preflight, Arm, and Activate.",
            },
        ],
        "paper": {
            "NSE": eng.market_snapshot("NSE"),
            "NASDAQ": eng.market_snapshot("NASDAQ"),
        },
        "live": _live_status_summary(request),
        "providers": build_provider_matrix(request.app.state),
        "watchlists": eng.list_watchlists(),
        "reconciliation": {
            "NSE": nse_cat.reconciliation_report(),
            "NASDAQ": nasdaq_cat.reconciliation_report(),
        },
        "recent_audit": eng.audit_log(limit=40),
    }


@router.get("/search")
def search_instruments(
    request: Request,
    query: str = Query(default="", max_length=80),
    market: str = Query(default="ALL", pattern="^(ALL|NSE|NASDAQ|all|nse|nasdaq)$"),
    subclass: str = Query(default="", max_length=40),
    include_indices: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
):
    mkt = market.upper()
    out_nse = None
    out_nasdaq = None
    combined: list[dict[str, Any]] = []

    if mkt in {"ALL", "NSE"}:
        nse_cat = _nse_search_catalog(request)
        out_nse = nse_cat.search(
            query,
            subclass=subclass,
            include_indices=include_indices,
            limit=limit,
        )
        for row in out_nse["items"]:
            combined.append({**row, "market": "NSE", "venue": row.get("exchange", "NSE")})

    if mkt in {"ALL", "NASDAQ"}:
        nasdaq_cat = _nasdaq_catalog(request)
        out_nasdaq = nasdaq_cat.search(query, subclass=subclass, limit=limit)
        for row in out_nasdaq["items"]:
            combined.append({**row, "market": "NASDAQ", "venue": row.get("exchange", "NASDAQ")})

    return {
        "query": query,
        "market": mkt,
        "items": combined[:limit],
        "nse": {
            "source_kind": _nse_search_catalog(request).source_kind,
            "ambiguous": out_nse["ambiguous"] if out_nse else False,
            "ambiguity_detail": out_nse["ambiguity_detail"] if out_nse else None,
            "total_matches": out_nse["total_matches"] if out_nse else 0,
        }
        if out_nse is not None
        else None,
        "nasdaq": {
            "source_kind": _nasdaq_catalog(request).source_kind,
            "total_matches": out_nasdaq["total_matches"] if out_nasdaq else 0,
        }
        if out_nasdaq is not None
        else None,
    }


@router.get("/reconciliation")
def market_reconciliation(request: Request):
    return {
        "NSE": _nse_search_catalog(request).reconciliation_report(),
        "NASDAQ": _nasdaq_catalog(request).reconciliation_report(),
    }


@router.post("/nse/refresh")
def refresh_nse_bod(request: Request):
    autonomy = getattr(request.app.state, "autonomy", None)
    if autonomy is None:
        raise HTTPException(503, "Upstox BOD catalog service is unavailable")
    try:
        autonomy.refresh_catalog()
    except Exception as exc:
        raise HTTPException(502, f"Upstox BOD master download failed: {str(exc)[:220]}") from exc
    return autonomy.catalog.reconciliation_report()


@router.post("/nasdaq/sync")
def sync_nasdaq_catalog(request: Request):
    creds, _ = _alpaca_readonly_creds(request)
    if not creds["api_key"] or not creds["api_secret"]:
        raise HTTPException(
            409,
            "Save Alpaca Market Data API Key & Secret in Connections before syncing the full NASDAQ universe.",
        )
    cat = _nasdaq_catalog(request)
    try:
        return cat.sync_from_alpaca(creds["api_key"], creds["api_secret"], exchange="NASDAQ")
    except Exception as exc:
        raise HTTPException(502, f"Alpaca NASDAQ asset sync failed: {str(exc)[:220]}") from exc


@router.get("/watchlists")
def get_watchlists(request: Request):
    return {"watchlists": _paper_engine(request).list_watchlists()}


@router.post("/watchlists")
def add_watchlist(body: WatchlistItemBody, request: Request):
    try:
        return _paper_engine(request).add_watchlist_symbol(body.market, body.symbol)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/watchlists/{market}/{symbol:path}")
def remove_watchlist(market: str, symbol: str, request: Request):
    try:
        return _paper_engine(request).remove_watchlist_symbol(market, symbol)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/quote")
def live_readonly_quote(
    request: Request,
    market: str = Query(pattern="^(NSE|NASDAQ|nse|nasdaq)$"),
    symbol: str = Query(min_length=1, max_length=64),
):
    mkt = market.upper()
    if mkt == "NSE":
        cat = _nse_search_catalog(request)
        searched = cat.search(symbol, include_indices=True, limit=1)
        if not searched["items"]:
            raise HTTPException(404, f"Unknown NSE symbol or key: {symbol}")
        inst = searched["items"][0]
        tok, src = _upstox_readonly_token(request)
        ver = verify_upstox_market_data(tok, credential_source=src, instrument_key=inst["instrument_key"])
        return {
            "market": "NSE",
            "currency": "INR",
            "instrument": inst,
            "connection_state": ver["connection_state"],
            "authenticated": ver["authenticated"],
            "session": ver["session"],
            "quote": ver.get("quote"),
            "guidance": ver.get("guidance"),
            "real_order_api_used": False,
        }

    cat = _nasdaq_catalog(request)
    inst_obj = cat.get(symbol)
    sym_clean = inst_obj.symbol if inst_obj else validate_us_symbol(symbol.strip().upper())
    creds, src = _alpaca_readonly_creds(request)
    ver = verify_alpaca_market_data(
        creds["api_key"],
        creds["api_secret"],
        feed=creds["feed"],
        credential_source=src,
        symbol=sym_clean,
    )
    return {
        "market": "NASDAQ",
        "currency": "USD",
        "instrument": inst_obj.public() if inst_obj else {"symbol": sym_clean, "exchange": "NASDAQ", "currency": "USD"},
        "connection_state": ver["connection_state"],
        "authenticated": ver["authenticated"],
        "session": ver["session"],
        "quote": ver.get("quote"),
        "guidance": ver.get("guidance"),
        "real_order_api_used": False,
    }


@router.get("/chart")
def live_readonly_chart(
    request: Request,
    market: str = Query(pattern="^(NSE|NASDAQ|nse|nasdaq)$"),
    symbol: str = Query(min_length=1, max_length=64),
    timeframe: str = Query(default="5m"),
):
    """Fetch real read-only OHLCV bars for NSE (Upstox) or NASDAQ (Alpaca).

    Never substitutes synthetic/replay candles as live market data when credentials
    are missing or offline — returns explicit connection_state and empty bars with
    actionable guidance.
    """
    if timeframe not in TIMEFRAMES:
        raise HTTPException(422, f"Unsupported timeframe {timeframe!r}")
    mkt = market.upper()
    now = datetime.now(timezone.utc)

    if mkt == "NSE":
        cat = _nse_search_catalog(request)
        searched = cat.search(symbol, include_indices=True, limit=1)
        if not searched["items"]:
            raise HTTPException(404, f"Unknown NSE symbol: {symbol}")
        inst = searched["items"][0]
        sess = nse_session_status(now)
        tok, src = _upstox_readonly_token(request)
        if not tok:
            return {
                "ok": False,
                "market": "NSE",
                "currency": "INR",
                "symbol": inst["trading_symbol"],
                "instrument": inst,
                "timeframe": timeframe,
                "connection_state": "DISCONNECTED",
                "session": sess,
                "data_source": "upstox-market-data-v3 (DISCONNECTED)",
                "data_feed_label": "UPSTOX DISCONNECTED · SAVE DAILY ACCESS TOKEN FOR REAL NSE BARS (NO SAMPLE FALLBACK)",
                "bars": [],
                "quote": None,
                "simulated_bars": False,
                "real_order_api_used": False,
                "generated_at": now.isoformat(),
            }
        reader = UpstoxReadOnly(lambda: tok, clock=lambda: now)
        bars = []
        error_detail = None
        try:
            if timeframe != "1D" and sess["is_open"]:
                bars = reader.intraday_candles(inst["instrument_key"], inst["display_symbol"], timeframe)
            if not bars:
                bars = reader.historical_candles(inst["instrument_key"], inst["display_symbol"], timeframe)
        except (UpstoxAuthError, ReconciliationFailure, ValueError) as exc:
            error_detail = str(exc)[:220]
        quote_ver = verify_upstox_market_data(tok, credential_source=src, instrument_key=inst["instrument_key"], now=now)
        conn_state = quote_ver["connection_state"] if bars or quote_ver.get("quote") else ("REJECTED" if "rejected" in (error_detail or "").lower() else "UNAVAILABLE")
        return {
            "ok": bool(bars),
            "market": "NSE",
            "currency": "INR",
            "symbol": inst["trading_symbol"],
            "instrument": inst,
            "timeframe": timeframe,
            "connection_state": conn_state,
            "session": sess,
            "data_source": "upstox-historical-v3",
            "data_feed_label": (
                f"UPSTOX NSE V3 · {'LIVE SESSION' if sess['is_open'] else sess['state']} · READ-ONLY MARKET DATA"
            ),
            "bars": [b.to_dict() for b in bars[-300:]],
            "quote": quote_ver.get("quote"),
            "error": error_detail,
            "simulated_bars": False,
            "real_order_api_used": False,
            "generated_at": now.isoformat(),
        }

    # NASDAQ via read-only Alpaca Market Data
    if timeframe == "4h":
        timeframe_fetch = "1h"
    else:
        timeframe_fetch = timeframe
    cat = _nasdaq_catalog(request)
    inst_obj = cat.get(symbol)
    try:
        sym_clean = inst_obj.symbol if inst_obj else validate_us_symbol(symbol.strip().upper())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    us_cal = USEquitiesCalendar()
    sess = us_cal.market_state(now)
    creds, src = _alpaca_readonly_creds(request)
    if not creds["api_key"] or not creds["api_secret"]:
        return {
            "ok": False,
            "market": "NASDAQ",
            "currency": "USD",
            "symbol": sym_clean,
            "instrument": inst_obj.public() if inst_obj else {"symbol": sym_clean, "exchange": "NASDAQ", "currency": "USD"},
            "timeframe": timeframe,
            "connection_state": "DISCONNECTED",
            "session": sess,
            "data_source": "alpaca-market-data (DISCONNECTED)",
            "data_feed_label": "ALPACA MARKET DATA DISCONNECTED · SAVE API KEY & SECRET FOR REAL NASDAQ BARS (NO SAMPLE FALLBACK)",
            "bars": [],
            "quote": None,
            "simulated_bars": False,
            "real_order_api_used": False,
            "generated_at": now.isoformat(),
        }

    lookback_days = 240 if timeframe_fetch == "1D" else (30 if timeframe_fetch == "1h" else 7)
    start = now - timedelta(days=lookback_days)
    bars = []
    error_detail = None
    hist = AlpacaHistoricalProvider(creds["api_key"], creds["api_secret"], feed=creds["feed"])
    try:
        bars = hist.get_candles(sym_clean, timeframe_fetch, start, now)
    except Exception as exc:
        error_detail = str(exc)[:220]
    finally:
        hist.close()

    quote_ver = verify_alpaca_market_data(
        creds["api_key"],
        creds["api_secret"],
        feed=creds["feed"],
        credential_source=src,
        symbol=sym_clean,
        now=now,
    )
    conn_state = quote_ver["connection_state"] if (bars or quote_ver.get("quote")) else "UNAVAILABLE"
    return {
        "ok": bool(bars),
        "market": "NASDAQ",
        "currency": "USD",
        "symbol": sym_clean,
        "instrument": inst_obj.public() if inst_obj else {"symbol": sym_clean, "exchange": "NASDAQ", "currency": "USD"},
        "timeframe": timeframe,
        "connection_state": conn_state,
        "session": sess,
        "feed": creds["feed"],
        "data_source": f"alpaca-market-data:{creds['feed']}",
        "data_feed_label": f"ALPACA {creds['feed'].upper()} · {feed_label(creds['feed'])} · {sess['status']}",
        "bars": [b.to_dict() for b in bars[-300:]],
        "quote": quote_ver.get("quote"),
        "error": error_detail,
        "simulated_bars": False,
        "real_order_api_used": False,
        "generated_at": now.isoformat(),
    }


# -- Live-Market Paper Endpoints -----------------------------------------------


@router.get("/paper/{market}")
def paper_snapshot(market: str, request: Request):
    try:
        return _paper_engine(request).market_snapshot(market)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/paper/session/start")
def paper_session_start(body: PaperSessionStartBody, request: Request):
    try:
        return _paper_engine(request).start_session(
            body.market,
            strategy=body.strategy,
            timeframe=body.timeframe,
            symbols=body.symbols,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/paper/session/transition")
def paper_session_transition(body: PaperSessionTransitionBody, request: Request):
    try:
        return _paper_engine(request).transition_session(body.market, body.action, reason=body.reason)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/paper/reset")
def paper_reset(body: PaperResetBody, request: Request):
    try:
        return _paper_engine(request).reset_paper_account(body.market, confirm=body.confirm)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/paper/config")
def paper_config_update(body: PaperConfigBody, request: Request):
    updates = body.model_dump(exclude={"market"}, exclude_none=True)
    try:
        return {"ok": True, "config": _paper_engine(request).update_config(body.market, **updates)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/paper/preview")
def paper_preview(body: PaperOrderBody, request: Request):
    return _paper_engine(request).preview_order(
        market=body.market,
        symbol=body.symbol,
        side=body.side,
        qty=body.qty,
        order_type=body.order_type,
        product=body.product,
        time_in_force=body.time_in_force,
        limit_price=body.limit_price,
        stop_price=body.stop_price,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        allow_off_hours_working=body.allow_off_hours_working,
    )


@router.post("/paper/orders")
def paper_submit_order(body: PaperOrderBody, request: Request):
    return _paper_engine(request).submit_order(
        client_order_id=body.client_order_id,
        market=body.market,
        symbol=body.symbol,
        side=body.side,
        qty=body.qty,
        order_type=body.order_type,
        product=body.product,
        time_in_force=body.time_in_force,
        limit_price=body.limit_price,
        stop_price=body.stop_price,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        allow_off_hours_working=body.allow_off_hours_working,
    )


@router.delete("/paper/orders/{client_order_id}")
def paper_cancel_order(client_order_id: str, request: Request):
    try:
        return _paper_engine(request).cancel_order(client_order_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.patch("/paper/orders/{client_order_id}")
def paper_replace_order(client_order_id: str, body: PaperReplaceBody, request: Request):
    try:
        return _paper_engine(request).replace_order(
            client_order_id,
            qty=body.qty,
            limit_price=body.limit_price,
            stop_price=body.stop_price,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/paper/positions/protection")
def paper_modify_protection(body: PaperProtectionBody, request: Request):
    try:
        return _paper_engine(request).modify_position_protection(
            body.market,
            body.symbol,
            stop_loss=body.stop_loss,
            take_profit=body.take_profit,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/paper/positions/{market}/{symbol:path}/close")
def paper_close_position(market: str, symbol: str, request: Request):
    try:
        return _paper_engine(request).close_position(market, symbol)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/paper/{market}/sync")
def paper_sync(market: str, request: Request):
    try:
        return _paper_engine(request).sync_live_tick(market)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/paper/{market}/reconcile")
def paper_reconcile(market: str, request: Request):
    try:
        return _paper_engine(request).reconcile(market)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/paper/audit/events")
def paper_audit(request: Request, market: str = "", limit: int = Query(default=150, ge=1, le=500)):
    return {"events": _paper_engine(request).audit_log(market=market, limit=limit)}


# -- Live Status & Atomic Global Emergency Stop --------------------------------


@router.get("/live/status")
def live_status(request: Request):
    return _live_status_summary(request)


@router.post("/emergency-stop")
def global_emergency_stop(body: GlobalEmergencyBody, request: Request):
    """Atomic Emergency Stop across Local Paper, Upstox Autonomy, Alpaca Broker, and Vantage."""
    reason = body.reason or "operator global emergency stop"
    results: dict[str, Any] = {}

    eng = getattr(request.app.state, "live_paper_engine", None)
    if eng is not None:
        results["live_market_paper"] = eng.emergency_stop(reason)

    autonomy = getattr(request.app.state, "autonomy", None)
    if autonomy is not None:
        try:
            active_sessions = [
                s for s in autonomy.sessions()
                if isinstance(s, dict) and s.get("session_id") and s.get("state") in {"ARMED", "ACTIVE"}
            ]
            if active_sessions:
                results["upstox_autonomy"] = [
                    autonomy.emergency_stop(str(s["session_id"]), reason=reason, cancel_entries=True)
                    for s in active_sessions
                ]
            else:
                results["upstox_autonomy"] = {"ok": True, "detail": "no active autonomy session"}
        except Exception as exc:
            results["upstox_autonomy"] = {"ok": False, "detail": str(exc)[:180]}

    alpaca_svc = getattr(request.app.state, "alpaca_broker", None)
    if alpaca_svc is not None:
        results["alpaca_broker"] = alpaca_svc.emergency_stop(reason)

    vantage = getattr(request.app.state, "vantage", None)
    if vantage is not None:
        try:
            results["vantage"] = vantage.emergency_stop(reason=reason, cancel_pending=True)
        except Exception as exc:
            results["vantage"] = {"ok": False, "detail": str(exc)[:180]}

    return {"ok": True, "reason": reason, "subsystems": results}


@router.post("/emergency-stop/reset")
def global_emergency_stop_reset(body: GlobalResetBody, request: Request):
    if (body.confirm_phrase or "").strip() != "CLEAR EMERGENCY STOP":
        raise HTTPException(422, "Typed confirmation phrase 'CLEAR EMERGENCY STOP' is required")
    results: dict[str, Any] = {}
    eng = getattr(request.app.state, "live_paper_engine", None)
    if eng is not None:
        results["live_market_paper"] = eng.reset_emergency_stop()
    alpaca_svc = getattr(request.app.state, "alpaca_broker", None)
    if alpaca_svc is not None and alpaca_svc.gate.kill_engaged:
        results["alpaca_broker"] = alpaca_svc.gate.reset_kill_switch(operator_ack=True)
    vantage = getattr(request.app.state, "vantage", None)
    if vantage is not None:
        try:
            results["vantage"] = vantage.clear_emergency_stop(confirmation="CLEAR EMERGENCY STOP")
        except Exception as exc:
            results["vantage"] = {"ok": False, "detail": str(exc)[:180]}
    return {"ok": True, "live_execution_remains_off": True, "subsystems": results}
