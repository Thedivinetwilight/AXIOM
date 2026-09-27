"""Streaming-universe resolution: NEVER subscribe to the whole instrument universe.

Priority: explicit UPSTOX_STREAMING_UNIVERSE env (comma-separated instrument
keys) > MARKET_DATA_SYMBOLS > default instrument. Hard cap (50 keys) with a
documented error if exceeded — subscribing to every instrument is refused,
not truncated silently.
"""

from __future__ import annotations

MAX_STREAMING_KEYS = 50
DEFAULT_KEY = "NSE_EQ|INE002A01018"  # RELIANCE (verified in the instrument master)


def resolve_streaming_universe(settings, repository=None) -> list[str]:
    raw = (getattr(settings, "streaming_universe", "") or "").strip()
    keys = [item.strip() for item in raw.split(",") if item.strip()]
    if not keys:
        keys = [item.strip() for item in
                (getattr(settings, "market_data_symbols", "") or "").split(",")
                if item.strip()]
    if not keys:
        keys = [DEFAULT_KEY]
    if len(keys) > MAX_STREAMING_KEYS:
        raise ValueError(
            f"streaming universe refuses {len(keys)} keys (cap {MAX_STREAMING_KEYS}); "
            "subscribe explicitly to a working set, not the whole universe")
    return keys
