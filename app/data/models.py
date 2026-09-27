"""Normalized candles use timezone-aware UTC OPEN timestamps."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import re

TIMEFRAMES = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1D": 86400}


def parse_timestamp(value: str | datetime) -> datetime:
    dt = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("Timestamps must include timezone, e.g. 2025-01-01T00:00:00Z")
    return dt.astimezone(timezone.utc)


def validate_symbol(symbol: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,39}", symbol):
        raise ValueError("Invalid symbol")
    return symbol


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    symbol: str

    @classmethod
    def from_dict(cls, row: dict):
        return cls(
            parse_timestamp(row["timestamp"]),
            *(float(row[k]) for k in ("open", "high", "low", "close", "volume")),
            validate_symbol(str(row["symbol"]).strip()),
        )

    def to_dict(self) -> dict:
        result = asdict(self)
        result["timestamp"] = self.timestamp.isoformat()
        return result

    def available_at(self, timeframe: str) -> datetime:
        return self.timestamp + timedelta(seconds=TIMEFRAMES[timeframe])
