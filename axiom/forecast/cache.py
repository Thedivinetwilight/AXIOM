"""Forecast cache identity.

A forecast is cached by everything that can change its output, so a cached
result is only reused when the *entire* input is identical:

    instrument · timeframe · last closed candle timestamp · input window hash
    · resolved model id/revision · configuration hash

``input_hash`` covers the exact OHLCV values and timestamps of the window, so a
revised candle (late correction, different provider) invalidates the cache even
when the last closed timestamp is unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from axiom.market.provider import MarketDataBar
from axiom.models import canonical_json, sha256_text


def input_hash(bars: Sequence[MarketDataBar]) -> str:
    """SHA-256 over the exact input window (values and timestamps)."""
    payload = canonical_json(
        [
            [
                bar.timestamp.isoformat(),
                repr(bar.open),
                repr(bar.high),
                repr(bar.low),
                repr(bar.close),
                repr(bar.volume),
                bar.instrument,
            ]
            for bar in bars
        ]
    )
    return sha256_text(payload)


def forecast_cache_key(
    *,
    instrument: str,
    timeframe: str,
    last_closed_timestamp: datetime | None,
    input_hash_value: str,
    model_revision: str,
    config_hash: str,
    model_id: str = "",
) -> str:
    payload = canonical_json(
        {
            "instrument": instrument,
            "timeframe": timeframe,
            "last_closed_timestamp": (
                last_closed_timestamp.isoformat() if last_closed_timestamp else None
            ),
            "input_hash": input_hash_value,
            "model_id": model_id,
            "model_revision": model_revision,
            "config_hash": config_hash,
        }
    )
    return sha256_text(payload)


def window_signature(bars: Sequence[MarketDataBar]) -> dict[str, object]:
    """Compact description of a window, useful in logs and provenance."""
    return {
        "bars": len(bars),
        "first": bars[0].timestamp.isoformat() if bars else None,
        "last": bars[-1].timestamp.isoformat() if bars else None,
        "input_hash": input_hash(bars),
    }
