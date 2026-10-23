"""Chart terminal API.

``GET /chart`` serves the page (loopback-open, like ``/dashboard``); the data
routes keep the full bearer gate. Nothing in this module can place an order:
orders live in ``POST /paper/order`` so that every order path in the project
goes through the same paper-broker module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.api.auth import authenticate, loopback_or_bearer
from app.charting import markers as chart_markers
from app.charting import series as chart_series
from app.charting import source as chart_source

router = APIRouter()

PAGE = Path(__file__).resolve().parents[2] / "static" / "chart" / "index.html"

TIMEFRAME_ORDER = ["1m", "5m", "15m", "30m", "1h", "4h", "1D"]

# What the drawn bars actually are. A replay/DEMO chart is sample data and must
# never be read as a market-data result, so every payload names its feed and
# the terminal shows that name next to the symbol.
FEED_LABELS = {
    "alpaca": "ALPACA US MARKET DATA · LOCAL SIMULATED FILLS",
    "upstox": "UPSTOX MARKET DATA · LOCAL SIMULATED FILLS",
    "replay": "DEMO REPLAY SAMPLE DATA · NOT MARKET DATA",
    "dataset": "LOCAL CSV PREVIEW · NOT MARKET DATA",
    "none": "NO DATA",
}


def _provider_name(simulator) -> str:
    """The feed currently attached to the paper session, if any."""
    provider = getattr(simulator, "provider", None)
    metrics = getattr(provider, "metrics", None)
    if not callable(metrics):
        return ""
    try:
        return str((metrics() or {}).get("provider") or "")
    except Exception:  # metrics are telemetry; never fail a chart request
        return ""


def _feed_identity(request, symbol: str, *, dataset: bool = False) -> tuple[str, str]:
    """(feed key, label) for bars belonging to `symbol`."""
    settings = request.app.state.settings
    simulator = getattr(request.app.state, "simulator", None)
    provider = _provider_name(simulator)
    configured = str(getattr(settings, "data_source", "") or "").lower()
    if provider in {"alpaca", "upstox"}:
        key = provider
    elif configured in {"alpaca", "upstox"}:
        key = configured
    elif dataset and configured == "replay":
        # Under DATA_SOURCE=replay this CSV is exactly what the engine will
        # stream, so it is the DEMO sample data — and it says so.
        key = "replay"
    elif dataset:
        key = "dataset"
    else:
        key = "replay"
    label = FEED_LABELS.get(key, key.upper())
    if key == "alpaca" and provider != "alpaca":
        label += " · NO SESSION RUNNING"
    return key, label


def _us_market_state() -> dict:
    from app.markets.us_calendar import USEquitiesCalendar

    state = USEquitiesCalendar().market_state(datetime.now(timezone.utc))
    return {
        "market_status": state["status"],
        "market_open": state["market_open"],
        "market_next_transition": state["next_transition"],
        "market_timezone": state["timezone"],
        "market_session": state["session"],
    }


def _timeframes_for(source_timeframe: str) -> list[str]:
    if not source_timeframe:
        return []
    return [
        timeframe
        for timeframe in TIMEFRAME_ORDER
        if timeframe == source_timeframe
        or chart_series.can_aggregate(source_timeframe, timeframe)
    ]


def _indicator_keys(raw: str) -> tuple[list[str], list[str]]:
    keys = [key.strip() for key in (raw or "").split(",") if key.strip()]
    unknown = [
        key
        for key in keys
        if key not in chart_series.OVERLAYS and key not in chart_series.OSCILLATORS
    ]
    if unknown:
        raise ValueError(f"Unknown indicator(s): {', '.join(sorted(unknown))}")
    overlays = [key for key in keys if key in chart_series.OVERLAYS]
    oscillators = [key for key in keys if key in chart_series.OSCILLATORS]
    return overlays, oscillators


@router.get("/chart", response_class=HTMLResponse, include_in_schema=False,
            dependencies=[Depends(loopback_or_bearer)])
async def chart_page():
    """The terminal page. It contains no secret and no remote asset."""
    if not PAGE.is_file():
        raise HTTPException(500, "chart assets are missing from this installation")
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


@router.get("/chart/data", dependencies=[Depends(authenticate)])
async def chart_data(
    request: Request,
    symbol: str = Query(default="", max_length=40),
    timeframe: str = Query(default="", max_length=8),
    limit: int = Query(default=700, ge=30, le=2000),
    indicators: str = Query(default="sma20,sma50,rsi14", max_length=200),
):
    settings = request.app.state.settings
    broker = request.app.state.broker
    simulator = request.app.state.simulator

    resolved_symbol = (symbol or settings.default_symbol or "").strip().upper()
    if not resolved_symbol:
        raise ValueError("symbol is required (or set DEFAULT_SYMBOL)")
    resolved_timeframe = timeframe or broker.timeframe
    spec = chart_series.ChartRequest(
        symbol=resolved_symbol, timeframe=resolved_timeframe, limit=limit
    ).validated()
    overlay_keys, oscillator_keys = _indicator_keys(indicators)

    resolved = chart_source.resolve(request.app, spec.symbol, spec.timeframe, spec.limit)
    if (
        not resolved.candles
        and resolved.source == "none"
        and str(getattr(settings, "data_source", "")).lower() == "alpaca"
        and spec.symbol in settings.alpaca_symbol_list()
    ):
        # Configured but empty: say what will make bars appear, not just
        # "no data" — the stream is the only thing that can fill this chart.
        resolved = chart_source.ResolvedSeries(
            source=resolved.source,
            source_label=resolved.source_label,
            timeframe=spec.timeframe,
            source_timeframe=spec.timeframe,
            note=(
                f"no Alpaca bars for {spec.symbol} yet: start the paper session; "
                "bars are drawn as the read-only 1-minute stream delivers closed candles"
            ),
            available_timeframes=chart_source.symbol_timeframes(request.app, spec.symbol, None),
        )
    if not resolved.candles and resolved.note and resolved.source_timeframe != spec.timeframe:
        # Asking for 5m when the only data is daily is a request we cannot
        # honour; say why instead of quietly drawing something else.
        raise ValueError(resolved.note)

    candles = resolved.candles
    dataset_backed = str(resolved.source).startswith("dataset")
    feed_key, feed_label = _feed_identity(request, spec.symbol, dataset=dataset_backed)
    status = simulator.status() if simulator else {}
    session = {
        "state": status.get("state"),
        "connection": status.get("connection"),
        "market_status": status.get("market_status"),
        "market_open": status.get("market_open"),
        "strategy": status.get("strategy"),
        "timeframe": status.get("timeframe") or getattr(broker, "timeframe", None),
        "provider": status.get("provider"),
    }
    if feed_key == "alpaca":
        # The New York calendar is the authority here: a silent feed during a
        # closed session is expected, not a fault.
        session.update(_us_market_state())

    payload = {
        "symbol": spec.symbol,
        "timeframe": spec.timeframe,
        "currency": getattr(broker, "currency", None),
        "paper_only": True,
        "live_trading": False,
        "source": resolved.source,
        "source_label": resolved.source_label,
        "data_feed": feed_key,
        "data_feed_label": feed_label,
        "market_data": feed_key in {"alpaca", "upstox"} and bool(candles),
        "source_timeframe": resolved.source_timeframe,
        "live": resolved.live,
        "note": resolved.note,
        "bars": chart_series.bar_payload(candles),
        "count": len(candles),
        "bounds": chart_series.series_bounds(candles),
        "overlays": chart_series.overlay_series(candles, overlay_keys),
        "oscillators": chart_series.oscillator_series(candles, oscillator_keys),
        "markers": chart_markers.trade_markers(broker, spec.symbol, candles, spec.timeframe),
        "orders": chart_markers.order_markers(broker, spec.symbol, candles, spec.timeframe),
        "position": chart_markers.position_levels(broker, spec.symbol),
        "session": session,
        "available": {
            "timeframes": chart_source.symbol_timeframes(
                request.app, spec.symbol, getattr(broker, "timeframe", None)
            ),
            "indicators": {
                "overlays": [
                    {"key": key, **specification}
                    for key, specification in chart_series.OVERLAYS.items()
                ],
                "oscillators": [
                    {
                        "key": key,
                        "label": specification["label"],
                        "min": specification["min"],
                        "max": specification["max"],
                    }
                    for key, specification in chart_series.OSCILLATORS.items()
                ],
            },
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return payload


@router.get("/chart/symbols", dependencies=[Depends(authenticate)])
async def chart_symbols(request: Request):
    """Instruments that actually have something to draw, plus their timeframes."""
    settings = request.app.state.settings
    broker = request.app.state.broker
    simulator = request.app.state.simulator
    engine_timeframe = getattr(broker, "timeframe", None) or settings.default_timeframe
    datasets = chart_source.dataset_index(request.app)

    entries: dict[str, dict] = {}

    def add(symbol: str, *, source: str, source_timeframe: str, live: bool = False):
        if not symbol:
            return
        feed_key, feed_label = _feed_identity(
            request, symbol, dataset=str(source).startswith("dataset")
        )
        entry = entries.get(symbol)
        if entry is None:
            entries[symbol] = {
                "symbol": symbol,
                "source": source,
                "source_timeframe": source_timeframe,
                "timeframes": _timeframes_for(source_timeframe),
                "live": live,
                "data_feed": feed_key,
                "data_feed_label": feed_label,
            }
        elif source == "engine" and not entry["live"]:
            entry.update(source=source, source_timeframe=source_timeframe,
                         timeframes=_timeframes_for(source_timeframe), live=live,
                         data_feed=feed_key, data_feed_label=feed_label)

    for symbol in getattr(simulator, "symbols", []) or []:
        add(symbol, source="engine", source_timeframe=engine_timeframe,
            live=bool(getattr(simulator, "task", None)))
    for symbol in chart_source.engine_bars_symbols(request.app):
        add(symbol, source="engine", source_timeframe=engine_timeframe,
            live=bool(getattr(simulator, "task", None)))
    for symbol, files in datasets.items():
        declared = chart_source.declared_timeframe(Path(request.app.state.data_dir) / files[0])
        if not declared:
            continue
        add(symbol, source=f"dataset:{files[0]}", source_timeframe=declared)
        # The same symbol may be present at several timeframes in several
        # files; the entry must advertise all of them.
        entries[symbol]["timeframes"] = chart_source.symbol_timeframes(
            request.app, symbol, engine_timeframe
        )
    for symbol in (getattr(getattr(broker, "portfolio", None), "positions", {}) or {}):
        add(symbol, source="position", source_timeframe=engine_timeframe)
    for symbol in (getattr(broker, "last_candles", None) or {}):
        add(symbol, source="engine:last-bar", source_timeframe=engine_timeframe)
    if str(getattr(settings, "data_source", "")).lower() == "alpaca":
        # Subscribe before the first bar arrives: the operator can pick AAPL
        # and see the honest empty state instead of an empty dropdown.
        for symbol in settings.alpaca_symbol_list():
            add(symbol, source="alpaca", source_timeframe=settings.default_timeframe or "1m")

    ordered = sorted(entries.values(), key=lambda item: (not item["live"], item["symbol"]))
    default = (settings.default_symbol or "").strip().upper()
    if default not in entries and ordered:
        default = ordered[0]["symbol"]
    return {
        "symbols": ordered,
        "default": default or None,
        "timeframes": (entries.get(default) or {}).get("timeframes", []),
        "paper_only": True,
        "live_trading": False,
    }
