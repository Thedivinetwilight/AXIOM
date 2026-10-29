from app.data.realtime import BinanceStream, JSONWebSocketStream, ReplayStream


def stream_provider(settings, source, *, bars=None, delay=0.0, symbol=None,
                    timeframe=None, source_label=None, data_kind=None):
    if source == "replay":
        return ReplayStream(
            bars, delay,
            source_label=source_label or "local-csv-replay",
            data_kind=data_kind or "local-csv",
        )
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
            timeframe=settings.default_timeframe,
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
        if (timeframe or getattr(settings, "default_timeframe", "")) != "1m":
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
