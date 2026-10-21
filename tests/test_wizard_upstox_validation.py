"""Regression tests for Arena 3.2.1 wizard/provider instrument-key validation.

The Upstox wizard must accept provider-form instrument keys like
``NSE_EQ|INE002A01018`` (SEGMENT|IDENTIFIER) without wrongly applying Arena's
strict internal symbol charset (which forbids ``|``). It must still reject
empty/malformed input and must never reach the credential probe with invalid
keys.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes import wizard

INSTRUMENT = "NSE_EQ|INE002A01018"


def run(coro):
    return asyncio.run(coro)


# ---- low-level Upstox key validator ---------------------------------------


class TestValidateUpstoxInstrumentKeys:
    def test_valid_equity_key_accepted(self):
        assert wizard._validate_upstox_instrument_keys(INSTRUMENT) == [INSTRUMENT]

    def test_leading_and_trailing_whitespace_trimmed(self):
        assert wizard._validate_upstox_instrument_keys(
            f"   {INSTRUMENT}   ") == [INSTRUMENT]

    def test_multiple_comma_separated_keys(self):
        out = wizard._validate_upstox_instrument_keys(
            f"{INSTRUMENT}, NSE_EQ|INE003A01018")
        assert out == [INSTRUMENT, "NSE_EQ|INE003A01018"]

    def test_empty_string_rejected(self):
        with pytest.raises(ValueError, match="At least one"):
            wizard._validate_upstox_instrument_keys("")

    def test_only_commas_rejected(self):
        with pytest.raises(ValueError, match="At least one"):
            wizard._validate_upstox_instrument_keys("  , ,,  ")

    def test_missing_pipe_rejected(self):
        # Generic Arena symbols like "RELIANCE" are not valid provider keys.
        with pytest.raises(ValueError, match="NSE_EQ\\|INE002A01018"):
            wizard._validate_upstox_instrument_keys("RELIANCE")

    def test_empty_segment_after_pipe_rejected(self):
        with pytest.raises(ValueError):
            wizard._validate_upstox_instrument_keys("NSE_EQ|")

    def test_empty_segment_before_pipe_rejected(self):
        with pytest.raises(ValueError):
            wizard._validate_upstox_instrument_keys("|INE002A01018")

    def test_spaces_in_identifier_rejected(self):
        # Would fail the display-form symbol charset (no spaces).
        with pytest.raises(ValueError):
            wizard._validate_upstox_instrument_keys("NSE_EQ|BAD KEY")

    def test_arbitrary_pipe_string_not_automatically_valid(self):
        # A random string that happens to contain '|' must not pass unless it
        # actually matches the SEGMENT|IDENTIFIER structure and maps cleanly.
        with pytest.raises(ValueError):
            wizard._validate_upstox_instrument_keys("nope|also nope")

    def test_generic_symbol_validator_still_rejects_pipe(self):
        """Existing non-Upstox validation must NOT be weakened."""
        from app.data.models import validate_symbol
        with pytest.raises(ValueError):
            validate_symbol(INSTRUMENT)


# ---- validate-market-data endpoint ----------------------------------------


class TestValidateMarketDataEndpoint:
    def test_provider_key_reaches_credential_probe(self, monkeypatch):
        seen: dict = {}

        async def fake_probe(token):
            seen["token"] = token
            return SimpleNamespace(verdict="feed_ok", guidance="")

        monkeypatch.setattr(wizard, "_probe_market_data", fake_probe)

        payload = wizard.ValidateMarketDataRequest(
            token="test-token",
            symbols=f"  {INSTRUMENT}  ",
        )
        result = run(wizard.validate_market_data(payload, SimpleNamespace()))
        assert result["ok"] is True
        assert result["symbol"] == "NSE_EQ-INE002A01018"
        assert result["instrument_key"] == INSTRUMENT
        assert result["symbols"] == [INSTRUMENT]
        assert seen["token"] == "test-token"

    def test_multiple_keys_normalized_before_probe(self, monkeypatch):
        async def fake_probe(token):
            return SimpleNamespace(verdict="feed_ok", guidance="")

        monkeypatch.setattr(wizard, "_probe_market_data", fake_probe)

        payload = wizard.ValidateMarketDataRequest(
            token="test-token",
            symbols=f" {INSTRUMENT}, NSE_EQ|INE003A01018 ",
        )
        result = run(wizard.validate_market_data(payload, SimpleNamespace()))
        assert result["symbols"] == [INSTRUMENT, "NSE_EQ|INE003A01018"]

    def test_invalid_key_rejected_before_probe(self, monkeypatch):
        called = False

        async def fake_probe(token):
            nonlocal called
            called = True
            return SimpleNamespace(verdict="feed_ok", guidance="")

        monkeypatch.setattr(wizard, "_probe_market_data", fake_probe)

        payload = wizard.ValidateMarketDataRequest(
            token="test-token",
            symbols="NSE_EQ|INVALID SYMBOL!!!",
        )
        with pytest.raises(HTTPException) as exc:
            run(wizard.validate_market_data(payload, SimpleNamespace()))
        assert exc.value.status_code == 422
        assert "Instrument key invalid" in exc.value.detail
        assert called is False

    def test_old_error_string_is_not_emitted_for_valid_key(self, monkeypatch):
        """Regression guard for the 3.2.0 bug.

        The previous buggy code raised 'Instrument key invalid: Invalid symbol'
        for the perfectly valid key NSE_EQ|INE002A01018 because it ran the
        generic symbol validator on the provider-form key.
        """
        async def fake_probe(token):
            return SimpleNamespace(verdict="feed_ok", guidance="")

        monkeypatch.setattr(wizard, "_probe_market_data", fake_probe)

        payload = wizard.ValidateMarketDataRequest(
            token="test-token",
            symbols=INSTRUMENT,
        )
        result = run(wizard.validate_market_data(payload, SimpleNamespace()))
        assert result["ok"] is True

    def test_paper_safety_defaults_unchanged(self):
        """Sanity: wizard constants remain fail-closed for live."""
        assert "LIVE_TRADING" in wizard.LIVE_ENABLEMENT
        assert "UPSTOX_LIVE" in wizard.LIVE_BROKERS
        assert "LOCAL_PAPER" not in wizard.LIVE_BROKERS


class TestPreparePaperValidatesInstrumentKeys:
    """The /api/wizard/prepare-paper save path must reject malformed Upstox
    instrument keys BEFORE probing the network or writing the .env. Otherwise
    a typo is persisted and only fails later at stream-connect time."""

    def _request(self, symbols: str, monkeypatch, probe_verdict: str = "feed_ok"):
        probe_called = {"n": 0}

        async def fake_probe(token):
            probe_called["n"] += 1
            return SimpleNamespace(verdict=probe_verdict, guidance="ok")

        monkeypatch.setattr(wizard, "_probe_market_data", fake_probe)
        written = {}
        monkeypatch.setattr(wizard, "update_values",
                            lambda path, updates, comment="":
                                (written.update(updates) or {"path": str(path)}))
        monkeypatch.setattr(wizard, "_rebuild_engine_for", lambda req, s: None)
        monkeypatch.setattr(wizard, "_initialize_home", lambda s: {"ok": True})

        from app.config.settings import Settings
        req = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    settings=Settings(),
                    repository=SimpleNamespace(
                        get_engine_state=lambda k: None,
                        record_system_event=lambda **kw: None),
                    broker=SimpleNamespace(
                        timeframe="1m", close=lambda: None,
                        repository=None),
                    simulator=SimpleNamespace(task=None, stop=lambda: _fake_coro()),
                )
            )
        )
        payload = wizard.PreparePaperRequest(
            data_source="upstox",
            api_token="abc",
            market_data_access_token="tok",
            market_data_symbols=symbols,
        )
        return probe_called, written, req, payload

    def test_invalid_keys_rejected_before_probe(self, monkeypatch):
        probe_called, written, req, payload = self._request(
            "NSE_EQ|INVALID SYMBOL!!!", monkeypatch)
        with pytest.raises(HTTPException) as exc:
            run(wizard.prepare_paper(payload, req))
        assert exc.value.status_code == 422
        assert "Instrument key invalid" in exc.value.detail
        assert probe_called["n"] == 0, "network probe must not run for invalid keys"
        assert not written, "nothing must be persisted for invalid keys"

    def test_valid_keys_normalised_and_saved(self, monkeypatch):
        probe_called, written, req, payload = self._request(
            f" {INSTRUMENT} , NSE_EQ|INE003A01018 ", monkeypatch)
        run(wizard.prepare_paper(payload, req))
        assert probe_called["n"] == 1
        assert written["MARKET_DATA_SYMBOLS"] == f"{INSTRUMENT},NSE_EQ|INE003A01018"
        assert written["DEFAULT_SYMBOL"] == "NSE_EQ-INE002A01018"

    def test_empty_symbols_rejected(self, monkeypatch):
        probe_called, written, req, payload = self._request("  , ,, ", monkeypatch)
        with pytest.raises(HTTPException) as exc:
            run(wizard.prepare_paper(payload, req))
        assert exc.value.status_code == 422
        assert probe_called["n"] == 0


def _fake_coro():
    async def _stop():
        return None
    return _stop()
