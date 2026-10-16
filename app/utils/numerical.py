"""Numerical / Data Quality Hardening — audit all financial calculations.

Reject or safely handle NaN, Inf, negative quantities, zero prices, impossible
timestamps, negative volume, invalid spreads, stale quotes, missing/duplicates.
Consistent decimal/float handling.
"""
from __future__ import annotations

import math
from datetime import datetime


def is_valid_price(v) -> bool:
    try:
        f = float(v)
        return f > 0 and not math.isnan(f) and not math.isinf(f) and f < 1e9
    except Exception:
        return False


def is_valid_quantity(v) -> bool:
    try:
        f = float(v)
        return f >= 0 and not math.isnan(f) and not math.isinf(f) and f < 1e9
    except Exception:
        return False


def is_valid_volume(v) -> bool:
    try:
        f = float(v)
        return f >= 0 and not math.isnan(f) and not math.isinf(f)
    except Exception:
        return False


def is_valid_timestamp(ts) -> bool:
    try:
        if ts is None:
            return False
        if isinstance(ts, (int, float)):
            if math.isnan(float(ts)) or math.isinf(float(ts)):
                return False
            # reasonable epoch: 2000-01-01 to 2100-01-01
            return 946684800 <= float(ts) <= 4102444800
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            # must have tz
            if dt.tzinfo is None:
                return False
            epoch = dt.timestamp()
            return 946684800 <= epoch <= 4102444800
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                return False
            epoch = ts.timestamp()
            return 946684800 <= epoch <= 4102444800
        return False
    except Exception:
        return False


def is_valid_spread(bid, ask) -> bool:
    try:
        b = float(bid); a = float(ask)
        if not is_valid_price(b) or not is_valid_price(a):
            return False
        if a < b:
            return False
        spread = a - b
        if math.isnan(spread) or math.isinf(spread) or spread < 0:
            return False
        # spread bps < 1000 is reasonable (10%)
        mid = (a + b) / 2
        bps = (spread / mid) * 10000 if mid > 0 else 0
        return 0 <= bps <= 1000
    except Exception:
        return False


def is_stale_quote(quote_ts: float, now: float | None = None, max_age: float = 60) -> bool:
    import time
    now = now or time.time()
    try:
        age = now - float(quote_ts)
        return age > max_age or age < -5
    except Exception:
        return True


def sanitize_candle(candle: dict) -> tuple[bool, str]:
    # candle expected keys: timestamp, open, high, low, close, volume, symbol
    try:
        ts = candle.get("timestamp")
        if not is_valid_timestamp(ts):
            return False, "invalid_timestamp"
        for k in ("open", "high", "low", "close"):
            if not is_valid_price(candle.get(k)):
                return False, f"invalid_{k}"
        if not is_valid_volume(candle.get("volume")):
            return False, "invalid_volume"
        # OHLC relationships
        o = float(candle["open"]); h = float(candle["high"]); l = float(candle["low"]); c = float(candle["close"])
        if not (l <= o <= h and l <= c <= h and l <= h):
            return False, "ohlc_violation"
        if math.isnan(o) or math.isnan(h) or math.isnan(l) or math.isnan(c):
            return False, "nan_price"
        if math.isinf(o) or math.isinf(h) or math.isinf(l) or math.isinf(c):
            return False, "inf_price"
        return True, "ok"
    except Exception as exc:
        return False, f"exception_{type(exc).__name__}"


def safe_divide(a, b, default: float = 0.0) -> float:
    try:
        af = float(a); bf = float(b)
        if bf == 0 or math.isnan(af) or math.isnan(bf) or math.isinf(af) or math.isinf(bf):
            return default
        res = af / bf
        if math.isnan(res) or math.isinf(res):
            return default
        return res
    except Exception:
        return default


def reject_nan_inf(value, name: str = "value"):
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise ValueError(f"{name} is NaN/Inf")
    return value
