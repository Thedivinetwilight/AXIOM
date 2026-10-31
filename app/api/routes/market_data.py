"""Authenticated read-only MT5 market-data API. No order routes exist here."""

from contextlib import nullcontext
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request

from app.charting import series as chart_series
from app.market_data.mt5 import (
    MarketDataUnavailable, PROVIDER_NAME, SUPPORTED_TIMEFRAMES, TIMEFRAME_CHOICES,
)


router = APIRouter(prefix="/api/market-data")


def service(request: Request):
    value = getattr(request.app.state, "market_data", None)
    if value is None:
        raise MarketDataUnavailable("Independent market-data runtime is unavailable")
    return value


@router.get("/status")
def status(request: Request):
    return service(request).status()


@router.post("/connect")
def connect(request: Request):
    # No credential or execution-mode input is accepted. The service reads only
    # the existing DPAPI VANTAGE_DEMO profile and cannot select an execution mode.
    return service(request).connect()


@router.post("/disconnect")
def disconnect(request: Request):
    return service(request).disconnect()


@router.get("/instruments")
def instruments(
    request: Request,
    q: str = Query(default="", max_length=100),
    product_type: str = Query(default="", max_length=40),
    supported_only: bool = False,
    limit: int = Query(default=5000, ge=1, le=5000),
):
    svc = service(request)
    if not svc.authenticated:
        raise MarketDataUnavailable(
            "Connect read-only VANTAGE DEMO market data; no hard-coded instrument list is available"
        )
    needle = q.strip().casefold()
    rows = []
    for item in svc.instrument_list():
        if supported_only and not item["market_data_supported"]:
            continue
        if product_type and item["product_type"] != product_type:
            continue
        haystack = " ".join((
            item["symbol"], item.get("display_name") or "",
            item.get("description") or "", item.get("path") or "",
        )).casefold()
        if needle and needle not in haystack:
            continue
        rows.append(item)
    return {
        "instruments": rows[:limit],
        "total_matching": len(rows),
        "account_specific": True,
        "hard_coded_universe": False,
        "selected": svc.selected_symbol,
        "timeframes": list(TIMEFRAME_CHOICES),
        "provider": PROVIDER_NAME,
        "execution_mode": "AXIOM_PAPER",
        "read_only": True,
    }


@router.get("/chart")
def chart(
    request: Request,
    symbol: str = Query(min_length=1, max_length=40),
    timeframe: str = Query(default="1m", max_length=8),
    limit: int = Query(default=500, ge=30, le=2000),
):
    svc = service(request)
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise MarketDataUnavailable(
            "Unsupported MT5 chart timeframe; choose 1m, 5m, 15m, 30m, 1h, 4h or 1D"
        )
    # This request is explicitly an MT5 request. Failure raises; it never
    # falls through to charting.source, CSV, DEMO, replay, or cached bars.
    tracker = getattr(request.app.state, "performance", None)
    timer = tracker.measure("market_data.chart.backend") if tracker else nullcontext()
    with timer:
        quote = svc.ensure_advancing(symbol)
        candles, warnings = svc.copy_bars(symbol, timeframe, limit)
    instrument = svc.instrument(symbol)
    quote_view = instrument["quote"]
    generated_at = datetime.now(timezone.utc).isoformat()
    note = (
        "MT5 closed bars from the connected demo account; forming bar excluded. "
        "Current tick and bar timestamps are distinct. No interpolation or replay fallback."
    )
    if warnings:
        note += " Source gaps are preserved and reported; no candles were invented."
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": [candle.to_dict() for candle in candles],
        "count": len(candles),
        "bounds": chart_series.series_bounds(candles),
        "quote": quote_view,
        "instrument": instrument,
        "product_type": instrument["product_type"],
        "is_cfd": instrument["is_cfd"],
        "currency_profit": instrument.get("currency_profit"),
        "account_currency": instrument.get("account_currency"),
        "volume_kind": "BROKER_TICK_VOLUME",
        "source": PROVIDER_NAME,
        "source_label": "VANTAGE DEMO MT5 · closed bars + current tick",
        "data_feed": "vantage_mt5",
        "data_feed_label": "VANTAGE DEMO MT5 · FRESH READ-ONLY MARKET DATA · LOCAL PAPER FILLS",
        "market_data": bool(quote_view and quote_view["fresh"] and candles),
        "live": True,
        "paper_only": True,
        "live_trading": False,
        "execution_mode": "AXIOM_PAPER",
        "automation_enabled": False,
        "read_only": True,
        "note": note,
        "validation_warnings": warnings,
        "quote_source_timestamp": quote.timestamp,
        "generated_at": generated_at,
        "profitability_claim": "none",
    }
