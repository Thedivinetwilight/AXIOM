from app.data.realtime import BinanceStream, JSONWebSocketStream, ReplayStream


#: the only timeframes the order engine can consume (it is intraday-only)
ENGINE_TIMEFRAMES = ("1m", "5m", "15m", "30m")

# The local PAPER simulator and the separate supervised/live engine have
# different timeframe contracts. Upstox's tick-built PAPER path can consume
# 1h bars, but it intentionally does not accept daily bars. Keep this list
# paper-specific so adding 1h here cannot widen live-engine behavior.
PAPER_UPSTOX_TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h")


def paper_upstox_compatibility_error(timeframe: str, mode: str) -> str:
    """Return a safe, actionable error for an unsupported PAPER feed pair."""
    from app.data.upstox_stream import INTERVAL_MODE_TIMEFRAMES, INTERVAL_MODES

    if timeframe not in PAPER_UPSTOX_TIMEFRAMES:
        supported = ", ".join(PAPER_UPSTOX_TIMEFRAMES)
        return (f"Upstox paper supports timeframes {supported}; "
                f"{timeframe or '(unset)'} is not supported")
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode in INTERVAL_MODES and timeframe not in INTERVAL_MODE_TIMEFRAMES:
        supported = "/".join(sorted(INTERVAL_MODE_TIMEFRAMES))
        return (f"Upstox {normalized_mode} mode supports {supported} only; "
                f"{timeframe} requires ltpc mode")
    return ""


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


def market_data_mode_error(settings) -> str:
    """Why this MARKET_DATA_MODE + timeframe pair cannot run ("" when it can).

    ``full`` carries official aggregate bars, which Upstox only publishes for
    1m/30m; every other timeframe is built locally from ticks, i.e. ``ltpc``.
    Without this check the combination was accepted by readiness and then
    refused by the data adapter — a config that passes preflight must be a
    config the engine accepts.
    """
    from app.data.upstox_stream import INTERVAL_MODE_TIMEFRAMES, INTERVAL_MODES

    mode = str(getattr(settings, "market_data_mode", "") or "").strip().lower()
    timeframe = engine_timeframe(settings)
    if mode in INTERVAL_MODES and timeframe not in INTERVAL_MODE_TIMEFRAMES:
        return (f"MARKET_DATA_MODE={mode} carries official "
                f"{'/'.join(sorted(INTERVAL_MODE_TIMEFRAMES))} bars only, but the "
                f"engine timeframe is {timeframe}; set MARKET_DATA_MODE=ltpc to "
                f"build {timeframe} bars from ticks, or use "
                f"{'/'.join(sorted(INTERVAL_MODE_TIMEFRAMES))}")
    return ""


def stream_provider(settings, source, *, bars=None, delay=0.0, symbol=None,
                    timeframe: str | None = None):
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
            # Non-paper callers retain the supervised engine's timeframe
            # contract. The paper caller passes its broker timeframe explicitly
            # so its provider cannot silently downgrade (e.g. 1h -> 1m).
            timeframe=timeframe if timeframe is not None else engine_timeframe(settings),
            mode=settings.market_data_mode,
            authorize_url=settings.market_data_authorize_url,
        )
    if source == "alpaca":
        from app.data.alpaca import AlpacaStream

        if str(getattr(settings, "mode", "PAPER")).upper() != "PAPER" or bool(
            getattr(settings, "live_trading", False)
        ) or str(getattr(settings, "broker", "LOCAL_PAPER")).upper() != "LOCAL_PAPER":
            raise ValueError("Alpaca US data can only feed the local paper simulator")
        if str(getattr(settings, "market", "")).lower() != "us" or str(
            getattr(settings, "currency", "")
        ).upper() != "USD":
            raise ValueError("Alpaca stream requires the US market and USD paper account")
        if timeframe != "1m":
            raise ValueError("Alpaca live paper stream currently supports 1m bars only")
        symbols = settings.alpaca_symbol_list() or ([symbol] if symbol else [])
        if not symbols:
            raise ValueError("Specify a US ticker to subscribe to Alpaca")
        if symbol and symbol.upper() not in symbols:
            raise ValueError("Requested ticker is not in ALPACA_SYMBOLS")
        return AlpacaStream(
            settings.alpaca_api_key,
            settings.alpaca_api_secret,
            symbols,
            feed=settings.alpaca_data_feed,
        )
    raise ValueError("Unknown market data source")
