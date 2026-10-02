from app.data.realtime import BinanceStream, JSONWebSocketStream, ReplayStream


#: the only timeframes the order engine can consume (it is intraday-only)
ENGINE_TIMEFRAMES = ("1m", "5m", "15m", "30m")


def engine_timeframe(settings) -> str:
    """The one timeframe used by BOTH the stream provider and the engine.

    These used to be derived separately (``DEFAULT_TIMEFRAME`` for the stream,
    a silent downgrade to ``1m`` for the engine), which guaranteed a
    "Stream timeframe mismatch" crash whenever the two disagreed.
    """
    requested = str(getattr(settings, "default_timeframe", "") or "")
    return requested if requested in ENGINE_TIMEFRAMES else "1m"


def live_timeframe_error(settings, broker_mode: str) -> str:
    """Why a LIVE engine cannot start with this timeframe ("" when it can).

    The engine is intraday-only. Research/replay may legitimately run a daily
    default, so this is enforced only on the real-money path — and enforced by
    refusing, never by silently downgrading the engine while the data stream
    keeps the configured timeframe (that combination crashed at startup).
    """
    if str(broker_mode or "").upper() != "UPSTOX_LIVE":
        return ""
    requested = str(getattr(settings, "default_timeframe", "") or "")
    if requested in ENGINE_TIMEFRAMES:
        return ""
    return (f"DEFAULT_TIMEFRAME={requested or '(unset)'} cannot drive a live engine "
            f"(intraday only: {', '.join(ENGINE_TIMEFRAMES)}); set it to an "
            "intraday timeframe before trading")


def stream_provider(settings, source, *, bars=None, delay=0.0, symbol=None):
    if source == "replay":
        return ReplayStream(bars, delay)
    if source == "binance":
        if not symbol or not symbol.endswith(settings.currency):
            raise ValueError("CURRENCY must match Binance quote currency (e.g. USDT for BTCUSDT)")
        return BinanceStream()
    if source == "generic":
        if not settings.data_ws_url:
            raise ValueError("DATA_WS_URL must be configured")
        return JSONWebSocketStream(settings.data_ws_url, settings.market_data_token)
    if source == "upstox":
        # Real Indian market DATA (read-only). Orders stay simulated in PaperBroker.
        from app.data.upstox_stream import UpstoxStream, from_symbol

        keys = settings.market_data_symbol_list()
        if not keys:
            if symbol:
                keys = [from_symbol(symbol)]
            else:
                raise ValueError(
                    "Set MARKET_DATA_SYMBOLS (e.g. NSE_EQ|INE002A01018) for the Upstox data feed"
                )
        return UpstoxStream(
            settings.market_data_access_token,
            keys,
            # ONE definition of the engine timeframe: the engine refuses any
            # timeframe outside this set, so the provider must use the same one.
            timeframe=engine_timeframe(settings),
            mode=settings.market_data_mode,
            authorize_url=settings.market_data_authorize_url,
        )
    raise ValueError("Unknown market data source")
