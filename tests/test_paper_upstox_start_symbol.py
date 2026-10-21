"""Paper startup must translate Upstox provider keys at the API boundary.

Provider/configuration identifiers retain Upstox's ``SEGMENT|IDENTIFIER``
form. PaperStartRequest, candles, and the paper engine use Arena's canonical
``SEGMENT-IDENTIFIER`` symbol. This pins the complete one-click startup path
without opening a network feed or creating a real-order route.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.schemas import PaperStartRequest
from app.api.server import create_app
from app.config.settings import Settings
from app.data.upstox_stream import from_symbol, to_symbol

INSTRUMENT_KEY = "NSE_EQ|INE002A01018"
INTERNAL_SYMBOL = "NSE_EQ-INE002A01018"


@pytest.mark.parametrize(
    ("key", "symbol"),
    [(INSTRUMENT_KEY, INTERNAL_SYMBOL),
     ("NSE_EQ|INE003A01018", "NSE_EQ-INE003A01018")],
)
def test_supported_upstox_keys_map_to_internal_symbol(key, symbol):
    assert to_symbol(key) == symbol
    assert from_symbol(symbol) == key


def test_paper_request_keeps_the_internal_symbol_contract():
    body = PaperStartRequest(source="upstox", symbol=INTERNAL_SYMBOL, timeframe="1m")
    assert body.symbol == INTERNAL_SYMBOL
    assert PaperStartRequest(source="replay", symbol="RELIANCE").symbol == "RELIANCE"
    assert PaperStartRequest(source="replay", symbol="DEMO").symbol == "DEMO"

    # The paper-start API accepts the provider's key for Upstox and normalizes
    # it before validation; non-Upstox requests retain the canonical contract.
    assert PaperStartRequest(
        source="upstox", symbol=INSTRUMENT_KEY, timeframe="1m"
    ).symbol == INTERNAL_SYMBOL
    with pytest.raises(ValidationError, match="Invalid symbol"):
        PaperStartRequest(source="replay", symbol=INSTRUMENT_KEY, timeframe="1m")


@pytest.mark.parametrize(
    "key",
    ["NSE_EQ|", "|INE002A01018", "NSE_EQ|BAD KEY", "NSE_EQ|A|B", "bad|key"],
)
def test_malformed_or_unrepresentable_upstox_keys_are_rejected(key):
    with pytest.raises(ValueError):
        to_symbol(key)


def test_frontend_one_click_request_reaches_start_default_and_starts_paper(
    tmp_path, monkeypatch,
):
    """Reproduce the wizard's {source: upstox} request with its default key."""
    token = "paper-start-test-token"
    settings = Settings(
        api_token=token,
        broker="LOCAL_PAPER",
        mode="PAPER",
        data_source="upstox",
        default_symbol=INTERNAL_SYMBOL,
        default_timeframe="1m",
        market_data_provider="upstox",
        market_data_symbols=INSTRUMENT_KEY,
        market_data_access_token="market-data-test-token",
        database_url=f"sqlite:///{tmp_path / 'paper.db'}",
    )
    app = create_app(settings)
    calls = {}
    provider = SimpleNamespace(symbols=[INTERNAL_SYMBOL], timeframe="1m")

    def fake_stream_provider(settings_arg, source, **kwargs):
        calls["provider"] = provider
        calls["source"] = source
        calls["provider_symbol"] = kwargs["symbol"]
        return provider

    async def fake_simulator_start(provider_arg, strategy, symbols):
        calls["started_provider"] = provider_arg
        calls["symbols"] = symbols
        return {"state": "STARTING", "symbols": symbols}

    from app.api.routes import paper as paper_routes

    monkeypatch.setattr(paper_routes, "stream_provider", fake_stream_provider)
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.simulator, "start", fake_simulator_start)
        response = client.post(
            "/api/paper/start-default",
            json={"source": "upstox"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200, response.text
        assert calls["source"] == "upstox"
        assert calls["provider_symbol"] == INTERNAL_SYMBOL
        assert calls["started_provider"] is provider
        assert calls["symbols"] == [INTERNAL_SYMBOL]
        assert response.json()["symbols"] == [INTERNAL_SYMBOL]

        # Paper remains LOCAL_PAPER; this path never constructs a live broker
        # or submits an order.
        assert app.state.settings.broker == "LOCAL_PAPER"
        assert app.state.settings.mode == "PAPER"
        assert app.state.broker.__class__.__name__ == "PaperBroker"
        assert app.state.broker.get_orders() == []

    frontend = Path("app/static/arena-wizard.js").read_text(encoding="utf-8")
    assert 'Arena.post("/api/paper/start-default", { source: paperPlan ? paperPlan.data_source : "replay" })' in frontend
