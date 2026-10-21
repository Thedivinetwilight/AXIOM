"""Paper/Upstox lifecycle regressions; all credentials and transports are fake."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.backtesting.costs import ExecutionConfig
from app.config.settings import Settings
from app.data.factory import engine_timeframe, stream_provider
from app.data.upstox_stream import UpstoxAuthError, UpstoxStream, to_symbol
from app.paper_trading.broker import PaperBroker
from app.paper_trading.simulator import BotState, PaperSimulator
from app.strategies import create_strategy


KEY = "NSE_EQ|INE002A01018"
SYMBOL = to_symbol(KEY)
LOCAL_API_TOKEN = "paper-upstox-lifecycle-test-api-token"
SYNTHETIC_FEED_TOKEN = "synthetic-market-data-token-never-send"


def upstox_settings(tmp_path, *, timeframe="1h", mode="ltpc"):
    return Settings(
        api_token=LOCAL_API_TOKEN,
        broker="LOCAL_PAPER",
        mode="PAPER",
        data_source="upstox",
        default_symbol=SYMBOL,
        default_timeframe=timeframe,
        market_data_provider="upstox",
        market_data_mode=mode,
        market_data_timeframe=timeframe,
        market_data_symbols=KEY,
        market_data_access_token=SYNTHETIC_FEED_TOKEN,
        database_url=f"sqlite:///{tmp_path / 'paper-upstox.db'}",
    )


def test_paper_factory_uses_explicit_broker_timeframe_without_widening_engine():
    settings = SimpleNamespace(
        currency="INR",
        market_data_symbol_list=lambda: [KEY],
        market_data_access_token=SYNTHETIC_FEED_TOKEN,
        market_data_mode="ltpc",
        market_data_authorize_url="https://api.upstox.com/v3/feed/market-data-feed/authorize",
        default_timeframe="1h",
    )

    paper_provider = stream_provider(settings, "upstox", symbol=SYMBOL, timeframe="1h")
    default_provider = stream_provider(settings, "upstox", symbol=SYMBOL)

    assert paper_provider.timeframe == "1h"
    assert default_provider.timeframe == engine_timeframe(settings) == "1m"
    assert paper_provider.timeframe != default_provider.timeframe


def test_one_click_paper_start_passes_broker_timeframe_to_upstox(tmp_path, monkeypatch):
    settings = upstox_settings(tmp_path, timeframe="1h")
    app = create_app(settings)
    captured = {}
    fake_provider = SimpleNamespace(symbols=[SYMBOL], timeframe="1h")

    def fake_stream_provider(settings_arg, source, **kwargs):
        captured.update(source=source, timeframe=kwargs.get("timeframe"), symbol=kwargs.get("symbol"))
        return fake_provider

    async def fake_start(provider, strategy, symbols):
        captured.update(provider=provider, strategy=strategy.name, symbols=symbols)
        return {"state": "STARTING", "symbols": symbols}

    from app.api.routes import paper as paper_routes

    monkeypatch.setattr(paper_routes, "stream_provider", fake_stream_provider)
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.simulator, "start", fake_start)
        response = client.post(
            "/api/paper/start-default",
            json={"source": "upstox"},
            headers={"Authorization": f"Bearer {LOCAL_API_TOKEN}"},
        )

    assert response.status_code == 200, response.text
    assert captured["source"] == "upstox"
    assert captured["timeframe"] == "1h"
    assert captured["provider"] is fake_provider
    assert captured["symbols"] == [SYMBOL]
    assert captured["strategy"] == "moving_average"


def test_mismatched_provider_timeframe_is_refused_before_task(tmp_path, monkeypatch):
    settings = upstox_settings(tmp_path, timeframe="1h")
    app = create_app(settings)
    wrong_provider = SimpleNamespace(symbols=[SYMBOL], timeframe="1m")

    from app.api.routes import paper as paper_routes

    monkeypatch.setattr(
        paper_routes, "stream_provider",
        lambda settings_arg, source, **kwargs: wrong_provider,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/paper/start-default",
            json={"source": "upstox"},
            headers={"Authorization": f"Bearer {LOCAL_API_TOKEN}"},
        )
        assert response.status_code == 422
        assert "provider timeframe must match" in response.json()["detail"]
        assert app.state.simulator.task is None


@pytest.mark.parametrize(
    ("timeframe", "mode", "message"),
    [
        ("1h", "full", "supports 1m/30m only"),
        ("1D", "ltpc", "timeframes 1m, 5m, 15m, 30m, 1h"),
    ],
)
def test_unsupported_upstox_paper_pair_is_rejected_before_task(
    tmp_path, timeframe, mode, message,
):
    settings = upstox_settings(tmp_path, timeframe=timeframe, mode=mode)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/paper/start-default",
            json={"source": "upstox"},
            headers={"Authorization": f"Bearer {LOCAL_API_TOKEN}"},
        )
        assert response.status_code == 422, response.text
        assert message in response.json()["detail"]
        assert app.state.simulator.task is None


def test_auth_failure_is_sanitized_and_terminal_status_survives_cleanup(tmp_path, caplog):
    provider = UpstoxStream(
        SYNTHETIC_FEED_TOKEN,
        [KEY],
        timeframe="1h",
        mode="ltpc",
    )

    async def fake_authorize():
        raise UpstoxAuthError("synthetic response included " + SYNTHETIC_FEED_TOKEN)

    provider._authorize = fake_authorize
    broker = PaperBroker(ExecutionConfig(), timeframe="1h")
    simulator = PaperSimulator(broker)

    async def run():
        await simulator.start(provider, create_strategy("moving_average", {}), [SYMBOL])
        await simulator.task

    with caplog.at_level("ERROR", logger="bot.controller"):
        asyncio.run(run())

    status = simulator.status()
    assert status["state"] == BotState.ERROR.value
    assert status["error"] == "UpstoxAuthError"
    assert status["error_phase"] == "provider_stream"
    assert status["error_reason"] == "provider_authentication_failed"
    assert status["connection"] == "AUTH_FAILED"
    assert status["connection_status"] == "AUTH_FAILED"
    assert status["processed_bars"] == 0
    assert SYNTHETIC_FEED_TOKEN not in repr(status)
    assert SYNTHETIC_FEED_TOKEN not in caplog.text


def test_non_auth_failure_uses_safe_reason_and_does_not_log_raw_message(caplog):
    secret = "synthetic-secret-not-for-status-or-logs"

    class FailingProvider:
        status = "CONNECTED"
        last_message = None
        symbols = [SYMBOL]
        timeframe = "1m"

        async def stream(self, symbols, timeframe):
            raise RuntimeError("unsafe provider detail: " + secret)
            yield  # pragma: no cover - makes this an async generator

        async def close(self):
            self.status = "DISCONNECTED"

    broker = PaperBroker(ExecutionConfig(), timeframe="1m")
    simulator = PaperSimulator(broker)

    async def run():
        await simulator.start(FailingProvider(), create_strategy("moving_average", {}), [SYMBOL])
        await simulator.task

    with caplog.at_level("ERROR", logger="bot.controller"):
        asyncio.run(run())

    status = simulator.status()
    assert status["state"] == BotState.ERROR.value
    assert status["error"] == "RuntimeError"
    assert status["error_phase"] == "provider_stream"
    assert status["error_reason"] == "provider_stream_failed"
    assert secret not in repr(status)
    assert secret not in caplog.text
