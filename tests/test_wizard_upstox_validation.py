"""Regression tests for Arena 3.2.0 wizard/provider instrument-key validation."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes import wizard

INSTRUMENT = "NSE_EQ|INE002A01018"


def run(coro):
    return asyncio.run(coro)


def test_provider_instrument_key_reaches_credential_probe(monkeypatch):
    seen = {}

    async def fake_probe(token):
        seen["token"] = token
        return SimpleNamespace(
            verdict="feed_ok",
            guidance="",
        )

    monkeypatch.setattr(wizard, "_probe_market_data", fake_probe)

    payload = wizard.ValidateMarketDataRequest(
        token="test-token",
        symbols=f"  {INSTRUMENT}  ",
    )
    result = run(wizard.validate_market_data(payload, SimpleNamespace()))

    assert result["ok"] is True
    assert result["symbol"] == INSTRUMENT
    assert result["symbols"] == [INSTRUMENT]
    assert seen["token"] == "test-token"


def test_multiple_provider_instrument_keys_are_normalized(monkeypatch):
    async def fake_probe(token):
        return SimpleNamespace(verdict="feed_ok", guidance="")

    monkeypatch.setattr(wizard, "_probe_market_data", fake_probe)

    payload = wizard.ValidateMarketDataRequest(
        token="test-token",
        symbols=f" {INSTRUMENT}, NSE_EQ|INE003A01018 ",
    )
    result = run(wizard.validate_market_data(payload, SimpleNamespace()))

    assert result["symbols"] == [
        INSTRUMENT,
        "NSE_EQ|INE003A01018",
    ]


def test_invalid_provider_key_is_rejected_before_credential_probe(monkeypatch):
    called = False

    async def fake_probe(token):
        nonlocal called
        called = True
        return SimpleNamespace(verdict="feed_ok", guidance="")

    monkeypatch.setattr(wizard, "_probe_market_data", fake_probe)

    payload = wizard.ValidateMarketDataRequest(
        token="test-token",
        symbols="NSE_EQ|INVALID SYMBOL",
    )
    with pytest.raises(HTTPException) as exc:
        run(wizard.validate_market_data(payload, SimpleNamespace()))

    assert exc.value.status_code == 422
    assert "Instrument key invalid" in str(exc.value.detail)
    assert called is False
