"""Cross-Market Context Cache — lightweight in-memory, uses existing market-data paths.

Maintains context for broad indices, sector indices, VIX, USD/INR, crude, gold,
major global indices — only what is required. For each candidate, exposes
market_direction, sector_direction, volatility_context, etc.

Do NOT create second market-data system; use existing candles/coverage.
Do NOT create trading signal from cross-market alone.
"""
from __future__ import annotations

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
        # Build context from cached data
        # Broad market
        broad = self.get("NIFTY")
        market_dir = None
        vol_ctx = None
        if broad:
            _, d = broad
            # direction from recent return
            ret = d.get("return") or d.get("change_pct") or 0
            try:
                r = float(ret)
                if r > 0.005:
                    market_dir = "up"
                elif r < -0.005:
                    market_dir = "down"
                else:
                    market_dir = "flat"
            except Exception:
                market_dir = "flat"
            # vol from vix
        vix = self.get("INDIA_VIX")
        vix_level = None
        if vix:
            _, d = vix
            try:
                vix_level = float(d.get("close") or d.get("value") or d.get("vix") or 0)
                if vix_level > 25:
                    vol_ctx = "high"
                elif vix_level > 20:
                    vol_ctx = "normal"
                elif vix_level > 0:
                    vol_ctx = "low"
            except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})
        if not vol_ctx:
            # fallback to broad vol
            if broad:
                _, d = broad
                vol = d.get("volatility")
                try:
                    if vol is not None:
                        v = float(vol)
                        if v > 0.025:
                            vol_ctx = "high"
                        elif v > 0.015:
                            vol_ctx = "normal"
                        else:
                            vol_ctx = "low"
                except Exception:
                    vol_ctx = "normal"
            else:
                vol_ctx = "normal"

        # Sector
        sector_dir = None
        if sector:
            sec_key = sector.upper()
            # try sector index
            sec = self.get(sec_key) or self.get("BANKNIFTY")
            if sec:
                _, d = sec
                ret = d.get("return") or 0
                try:
                    r = float(ret)
                    sector_dir = "up" if r > 0.005 else "down" if r < -0.005 else "flat"
                except Exception:
                    sector_dir = "flat"

        # Commodity
        commodity = None
        crude = self.get("CRUDE")
        gold = self.get("GOLD")
        if crude or gold:
            parts = []
            if crude:
                _, d = crude
                try:
                    parts.append(f"crude:{float(d.get('return') or 0):+.2%}")
                except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})
            if gold:
                _, d = gold
                try:
                    parts.append(f"gold:{float(d.get('return') or 0):+.2%}")
                except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})
            commodity = ",".join(parts) if parts else None

        # Currency
        usdinr = self.get("USDINR")
        curr = None
        if usdinr:
            _, d = usdinr
            try:
                curr = f"USDINR {float(d.get('close') or d.get('value') or 0):.2f}"
            except Exception:
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
