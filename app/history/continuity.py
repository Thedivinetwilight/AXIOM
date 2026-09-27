"""Data continuity: merge historical + intraday + live candles without duplicates.

Precedence on identical timestamps: LIVE > INTRADAY > HISTORICAL (higher
resolution sources overwrite older snapshots of the same candle). Every
merged row carries its `source` label so research never confuses provenance.
"""

from __future__ import annotations

from app.history.store import parse_ts

SOURCE_RANK = {"historical": 0, "intraday": 1, "live": 2}


def merge(*series: tuple[str, list[dict]]) -> list[dict]:
    """series: [("historical", rows), ("intraday", rows), ("live", rows)]."""
    merged: dict[str, dict] = {}
    for source, rows in series:
        rank = SOURCE_RANK[source]
        for row in rows:
            key = parse_ts(row["ts"]).isoformat()
            existing = merged.get(key)
            if existing is None or rank >= SOURCE_RANK[existing["source"]]:
                merged[key] = {**row, "source": source}
    return [merged[key] for key in sorted(merged, key=lambda ts: parse_ts(ts))]


def deduplicated_count(series: tuple[str, list[dict]]) -> int:
    return len(merge(series))
