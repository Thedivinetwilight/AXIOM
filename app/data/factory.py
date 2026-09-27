from app.data.realtime import BinanceStream, JSONWebSocketStream, ReplayStream


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
            timeframe=settings.default_timeframe,
            mode=settings.market_data_mode,
            authorize_url=settings.market_data_authorize_url,
        )
    raise ValueError("Unknown market data source")
