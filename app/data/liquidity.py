"""Liquidity / Spread Intelligence — uses existing real-time market data.

Where available, uses best bid/ask, quantities, volume, volatility to calculate
spread, spread_bps, imbalance, liquidity estimate, execution quality.

Reject/defer/reduce quality when spread too wide or liquidity insufficient.
Only detailed calculations for candidates (via caching).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass


@dataclass
class LiquiditySnapshot:
    symbol: str
    timestamp: float
    bid: float | None
    ask: float | None
    bid_qty: float | None
    ask_qty: float | None
    last_price: float | None
    volume: float | None
    spread: float | None
    spread_bps: float | None
    imbalance: float | None  # (bid_qty - ask_qty)/(bid_qty+ask_qty)  -1..1
    liquidity_score: float | None  # 0..1
    execution_quality: str  # good / moderate / poor / unknown
    stale: bool
    reasons: list[str]


class LiquidityIntelligence:
    def __init__(self, max_spread_bps: float = 50.0, min_depth: float = 1000.0, cache_ttl: float = 5.0, max_cache: int = 500):
        self.max_spread_bps = float(max_spread_bps)
        self.min_depth = float(min_depth)
        self.cache_ttl = float(cache_ttl)
        self.max_cache = int(max_cache)
        self._cache: dict[str, tuple[float, LiquiditySnapshot]] = {}
        self._order = []  # lru

    @classmethod
    def from_settings(cls, settings):
        return cls(
            max_spread_bps=float(getattr(settings, "max_spread_bps", 50.0)),
            min_depth=1000.0,
            cache_ttl=5.0,
        )

    def _is_stale(self, ts: float | None, now: float) -> bool:
        if ts is None:
            return True
        try:
            # stale if older than 60s or in future by >5s
            age = now - float(ts)
            return age > 60 or age < -5
        except Exception:
            return True

    def _calc_spread_bps(self, bid: float | None, ask: float | None, mid: float | None) -> tuple[float | None, float | None]:
        if bid is None or ask is None:
            return None, None
        try:
            b = float(bid); a = float(ask)
            if b <= 0 or a <= 0 or a < b or math.isnan(b) or math.isnan(a) or math.isinf(b) or math.isinf(a):
                return None, None
            spread = a - b
            if mid is None or mid <= 0:
                mid = (a + b) / 2
            if mid <= 0 or math.isnan(mid) or math.isinf(mid):
                return spread, None
            bps = (spread / mid) * 10000
            if math.isnan(bps) or math.isinf(bps) or bps < 0:
                return spread, None
            return spread, bps
        except Exception:
            return None, None

    def assess(
        self,
        symbol: str,
        *,
        bid: float | None = None,
        ask: float | None = None,
        bid_qty: float | None = None,
        ask_qty: float | None = None,
        last_price: float | None = None,
        volume: float | None = None,
        volatility: float | None = None,
        timestamp: float | None = None,
    ) -> LiquiditySnapshot:
        now = time.time()
        # Check cache if fresh and same inputs? Use symbol as key, but we recompute each time if timestamp differs
        # Simple: if cached and age < ttl and inputs not stale, return cached
        cached = self._cache.get(symbol)
        if cached:
            ts, snap = cached
            if now - ts < self.cache_ttl:
                # if caller provides no new timestamp, use cache
                if timestamp is None or abs((timestamp or 0) - snap.timestamp) < 1:
                    return snap

        # Validate inputs
        reasons = []
        stale = self._is_stale(timestamp, now)
        if stale:
            reasons.append("stale_quote")

        # Spread
        mid = None
        if bid is not None and ask is not None:
            try:
                mid = (float(bid) + float(ask)) / 2
            except Exception:
                mid = last_price
        elif last_price is not None:
            mid = last_price

        spread, spread_bps = self._calc_spread_bps(bid, ask, mid)
        if spread_bps is not None:
            if spread_bps > self.max_spread_bps:
                reasons.append(f"wide_spread {spread_bps:.1f}bps")
            elif spread_bps < 10:
                reasons.append(f"tight_spread {spread_bps:.1f}bps")
        else:
            if bid is None or ask is None:
                reasons.append("missing_bid_ask")
            else:
                reasons.append("invalid_spread")

        # Imbalance
        imbalance = None
        if bid_qty is not None and ask_qty is not None:
            try:
                bq = float(bid_qty); aq = float(ask_qty)
                if bq < 0 or aq < 0 or math.isnan(bq) or math.isnan(aq) or math.isinf(bq) or math.isinf(aq):
                    reasons.append("invalid_depth")
                else:
                    total = bq + aq
                    if total > 0:
                        imbalance = (bq - aq) / total
                        if imbalance > 0.5:
                            reasons.append("strong_bid_imbalance")
                        elif imbalance < -0.5:
                            reasons.append("strong_ask_imbalance")
                    else:
                        reasons.append("zero_depth")
            except Exception:
                reasons.append("depth_calc_failed")
        else:
            reasons.append("missing_depth")

        # Liquidity score 0..1
        liquidity_score = None
        try:
            # depth component
            depth = None
            if bid_qty is not None and ask_qty is not None:
                depth = float(bid_qty) + float(ask_qty)
            elif volume is not None:
                # volume proxy: 1k is low, 10k is good liquidity
                depth = float(volume)  # proxy: direct use, 10k => 1.0
            # normalize: depth 0..5000 -> 0..1 (5000+ is high liquidity, 1000 is moderate)
            if depth is not None and not math.isnan(depth) and not math.isinf(depth) and depth >= 0:
                depth_score = min(1.0, depth / 5000.0)
            else:
                depth_score = 0.3  # unknown

            # spread component
            if spread_bps is not None:
                spread_score = max(0.0, 1.0 - (spread_bps / self.max_spread_bps))
            else:
                spread_score = 0.5

            # volatility penalty
            vol_penalty = 0.0
            if volatility is not None and not math.isnan(volatility) and volatility is not None:
                try:
                    vol = float(volatility)
                    if vol > 0.03:
                        vol_penalty = 0.2
                except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})

            liquidity_score = max(0.0, min(1.0, (depth_score * 0.5 + spread_score * 0.5) - vol_penalty))
            if stale:
                liquidity_score *= 0.5
                reasons.append("stale_penalty")
        except Exception:
            liquidity_score = 0.3
            reasons.append("liquidity_calc_failed")

        # Execution quality
        if stale or spread_bps is None:
            eq = "unknown"
        elif spread_bps > self.max_spread_bps or (liquidity_score is not None and liquidity_score < 0.2):
            eq = "poor"
        elif spread_bps > 20 or (liquidity_score is not None and liquidity_score < 0.5):
            eq = "moderate"
        else:
            eq = "good"

        snap = LiquiditySnapshot(
            symbol=symbol,
            timestamp=timestamp or now,
            bid=bid, ask=ask, bid_qty=bid_qty, ask_qty=ask_qty,
            last_price=last_price, volume=volume,
            spread=spread, spread_bps=spread_bps,
            imbalance=imbalance, liquidity_score=liquidity_score,
            execution_quality=eq, stale=stale, reasons=reasons,
        )

        # Cache
        self._cache[symbol] = (now, snap)
        self._order.append(symbol)
        if len(self._order) > self.max_cache:
            oldest = self._order.pop(0)
            self._cache.pop(oldest, None)

        return snap

    def should_reduce_or_reject(self, snap: LiquiditySnapshot) -> tuple[bool, bool, str]:
        # returns (should_reject, should_reduce, reason)
        if snap.stale:
            return True, False, "stale_quote"
        if snap.spread_bps is not None and snap.spread_bps > self.max_spread_bps:
            return True, False, f"spread {snap.spread_bps:.1f}bps > {self.max_spread_bps}"
        if snap.liquidity_score is not None and snap.liquidity_score < 0.2:
            return True, False, f"liquidity {snap.liquidity_score:.2f} < 0.2"
        if snap.execution_quality == "poor":
            return False, True, "poor execution quality"
        if snap.liquidity_score is not None and snap.liquidity_score < 0.4:
            return False, True, "moderate liquidity"
        return False, False, "ok"

    def invalidate(self, symbol: str | None = None):
        if symbol:
            self._cache.pop(symbol, None)
        else:
            self._cache.clear()
            self._order.clear()
