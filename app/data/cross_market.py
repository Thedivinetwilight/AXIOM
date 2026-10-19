"""Cross-Market Context Cache — lightweight in-memory, uses existing market-data paths.

Maintains context for broad indices, sector indices, VIX, USD/INR, crude, gold,
major global indices — only what is required. For each candidate, exposes
market_direction, sector_direction, volatility_context, etc.

Do NOT create second market-data system; use existing candles/coverage.
Do NOT create trading signal from cross-market alone.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, Any


@dataclass
class MarketContext:
    timestamp: float
    market_direction: str | None = None  # up / down / flat
    sector_direction: str | None = None
    volatility_context: str | None = None  # low / normal / high / extreme
    commodity_context: str | None = None
    currency_context: str | None = None
    breadth: float | None = None
    vix_level: float | None = None
    details: Dict[str, Any] = field(default_factory=dict)


class CrossMarketCache:
    # Relevant context keys — only subscribe/compute what is required
    TRACKED = {
        "NIFTY": "broad_market",
        "BANKNIFTY": "sector_bank",
        "INDIA_VIX": "volatility",
        "USDINR": "currency",
        "CRUDE": "commodity_oil",
        "GOLD": "commodity_gold",
        "SPX": "global_us",
    }

    def __init__(self, ttl: int = 60, max_size: int = 200):
        self.ttl = int(ttl)
        self.max_size = int(max_size)
        self._cache: dict[str, tuple[float, dict]] = {}
        self._order: list[str] = []

    @classmethod
    def from_settings(cls, settings):
        return cls(ttl=int(getattr(settings, "cross_market_cache_ttl", 60)))

    def _evict(self):
        while len(self._cache) > self.max_size:
            oldest = self._order.pop(0)
            self._cache.pop(oldest, None)

    def update(self, key: str, data: dict, ts: float | None = None):
        now = ts or time.time()
        # Normalize key
        k = str(key).upper()
        if k not in self.TRACKED and k not in self._cache and len(self._cache) >= self.max_size:
            # Don't track unknown keys beyond limit
            return
        self._cache[k] = (now, dict(data))
        if k not in self._order:
            self._order.append(k)
        else:
            self._order.remove(k)
            self._order.append(k)
        self._evict()

    def get(self, key: str) -> tuple[float, dict] | None:
        k = str(key).upper()
        entry = self._cache.get(k)
        if not entry:
            return None
        ts, data = entry
        if time.time() - ts > self.ttl:
            # stale — keep but mark
            return (ts, {**data, "_stale": True})
        return entry

    def context_for(self, candidate_symbol: str | None = None, sector: str | None = None) -> MarketContext:
        now = time.time()

        def fresh_entry(key: str):
            entry = self.get(key)
            if entry is None or entry[1].get("_stale"):
                return None
            return entry

        # Only current, explicitly supplied measurements contribute context.
        broad = fresh_entry("NIFTY")
        market_dir = None
        vol_ctx = None
        if broad:
            _, data = broad
            raw_return = data.get("return")
            if raw_return is None:
                raw_return = data.get("change_pct")
            if raw_return is not None and not isinstance(raw_return, bool):
                try:
                    market_return = float(raw_return)
                    if math.isfinite(market_return):
                        if market_return > 0.005:
                            market_dir = "up"
                        elif market_return < -0.005:
                            market_dir = "down"
                        else:
                            market_dir = "flat"
                except (OverflowError, TypeError, ValueError):
                    market_dir = None

        vix = fresh_entry("INDIA_VIX")
        vix_level = None
        if vix:
            _, data = vix
            raw_vix = next((data.get(name) for name in ("close", "value", "vix")
                            if data.get(name) is not None), None)
            if raw_vix is not None and not isinstance(raw_vix, bool):
                try:
                    value = float(raw_vix)
                    if math.isfinite(value) and value > 0:
                        vix_level = value
                        if value > 25:
                            vol_ctx = "high"
                        elif value > 20:
                            vol_ctx = "normal"
                        else:
                            vol_ctx = "low"
                except (OverflowError, TypeError, ValueError):
                    vix_level = None
                    vol_ctx = None

        if vol_ctx is None and broad:
            _, data = broad
            raw_volatility = data.get("volatility")
            if raw_volatility is not None and not isinstance(raw_volatility, bool):
                try:
                    volatility = float(raw_volatility)
                    if math.isfinite(volatility) and volatility >= 0:
                        if volatility > 0.025:
                            vol_ctx = "high"
                        elif volatility > 0.015:
                            vol_ctx = "normal"
                        else:
                            vol_ctx = "low"
                except (OverflowError, TypeError, ValueError):
                    vol_ctx = None

        # Sector
        sector_dir = None
        if sector:
            sec_key = sector.upper()
            # try sector index
            sec = fresh_entry(sec_key) or fresh_entry("BANKNIFTY")
            if sec:
                _, data = sec
                raw_return = data.get("return")
                if raw_return is None:
                    raw_return = data.get("change_pct")
                if raw_return is not None and not isinstance(raw_return, bool):
                    try:
                        sector_return = float(raw_return)
                        if math.isfinite(sector_return):
                            sector_dir = (
                                "up" if sector_return > 0.005
                                else "down" if sector_return < -0.005
                                else "flat")
                    except (OverflowError, TypeError, ValueError):
                        sector_dir = None

        # Commodity
        commodity = None
        crude = fresh_entry("CRUDE")
        gold = fresh_entry("GOLD")
        if crude or gold:
            parts = []
            for name, entry in (("crude", crude), ("gold", gold)):
                if not entry:
                    continue
                _, data = entry
                raw_return = data.get("return")
                if raw_return is None or isinstance(raw_return, bool):
                    continue
                try:
                    value = float(raw_return)
                    if math.isfinite(value):
                        parts.append(f"{name}:{value:+.2%}")
                except (OverflowError, TypeError, ValueError):
                    continue
            commodity = ",".join(parts) if parts else None

        # Currency
        usdinr = fresh_entry("USDINR")
        curr = None
        if usdinr:
            _, data = usdinr
            raw_value = data.get("close")
            if raw_value is None:
                raw_value = data.get("value")
            if raw_value is not None and not isinstance(raw_value, bool):
                try:
                    value = float(raw_value)
                    if math.isfinite(value) and value > 0:
                        curr = f"USDINR {value:.2f}"
                except (OverflowError, TypeError, ValueError):
                    curr = None

        # Breadth (if available)
        breadth = None
        # Could be derived from market data's advancing stocks, but keep None if not available

        return MarketContext(
            timestamp=now,
            market_direction=market_dir,
            sector_direction=sector_dir,
            volatility_context=vol_ctx,
            commodity_context=commodity,
            currency_context=curr,
            breadth=breadth,
            vix_level=vix_level,
            details={k: v for k, (_, v) in self._cache.items()},
        )

    def invalidate(self, key: str | None = None):
        if key:
            self._cache.pop(str(key).upper(), None)
            try:
                self._order.remove(str(key).upper())
            except ValueError as exc:
                    import logging as _lg2
                    _lg2.getLogger(__name__).debug("ignored_value", extra={"error": type(exc).__name__})
        else:
            self._cache.clear()
            self._order.clear()
