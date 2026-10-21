import pytest
from pydantic import ValidationError

from app.api.schemas import PaperStartRequest


def test_upstox_paper_start_normalizes_provider_instrument_key():
    request = PaperStartRequest(
        source="upstox",
        symbol="NSE_EQ|INE002A01018",
        timeframe="1m",
    )

    assert request.symbol == "NSE_EQ-INE002A01018"


def test_non_upstox_paper_start_rejects_provider_instrument_key():
    with pytest.raises(ValidationError, match="Invalid symbol"):
        PaperStartRequest(source="replay", symbol="NSE_EQ|INE002A01018")


def test_upstox_paper_start_rejects_malformed_instrument_key():
    with pytest.raises(ValidationError, match="Invalid symbol"):
        PaperStartRequest(source="upstox", symbol="NSE_EQ|INE002A01018|EXTRA")
