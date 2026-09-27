"""Trade Quality Scorer — lightweight, deterministic, transparent ranking layer.

Combines existing information; does NOT override hard risk. Returns structured
result: quality_score, confidence, reasons, penalties, supporting_features.

Deterministic: no opaque ML, no random, bounded 0..1 score.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class QualityResult:
    quality_score: float  # 0..1
    confidence: float  # 0..1
    reasons: list[str] = field(default_factory=list)
    penalties: list[str] = field(default_factory=list)
    supporting_features: dict = field(default_factory=dict)
    raw_score: float = 0.0


class TradeQualityScorer:
    def __init__(self, min_score: float = 0.3):
        self.min_score = float(min_score)

    @classmethod
    def from_settings(cls, settings):
        return cls(min_score=float(getattr(settings, "min_quality_score", 0.3)))

    def score(
        self,
        *,
        strategy_confidence: float | None = None,
        expected_net_edge: float | None = None,
        regime_compatibility: float | None = None,
        volatility: float | None = None,
        volatility_regime: str | None = None,
        liquidity_score: float | None = None,
        spread_bps: float | None = None,
        trend_alignment: float | None = None,
        market_context: dict | None = None,
        event_context: dict | None = None,
        recent_oos_expectancy: float | None = None,
        reward_risk: float | None = None,
        stop_distance: float | None = None,
        signal_age: int | None = None,
        alpha_decay_factor: float | None = None,
        portfolio_correlation: float | None = None,
    ) -> QualityResult:
        reasons = []
        penalties = []
        features = {}
        # Base
        score = 0.5
        confidence = 0.5

        # Strategy confidence (0..1)
        if strategy_confidence is not None and not (isinstance(strategy_confidence, float) and (math.isnan(strategy_confidence) or math.isinf(strategy_confidence))):
            sc = max(0.0, min(1.0, float(strategy_confidence)))
            score += (sc - 0.5) * 0.3
            features["strategy_confidence"] = sc
            if sc > 0.7:
                reasons.append(f"high strategy confidence {sc:.2f}")
            elif sc < 0.3:
                penalties.append(f"low strategy confidence {sc:.2f}")
                score -= 0.1
            confidence = max(confidence, sc * 0.8)

        # Expected net edge
        if expected_net_edge is not None and not math.isnan(float(expected_net_edge)) and not math.isinf(float(expected_net_edge)):
            edge = float(expected_net_edge)
            features["expected_net_edge"] = edge
            if edge > 0.002:
                score += 0.15
                reasons.append(f"positive net edge {edge:.4f}")
            elif edge <= 0:
                penalties.append(f"non-positive edge {edge:.4f}")
                score -= 0.25
            # scale
            score += max(-0.1, min(0.1, edge * 10))

        # Regime compatibility
        if regime_compatibility is not None:
            rc = max(0.0, min(1.0, float(regime_compatibility)))
            features["regime_compatibility"] = rc
            score += (rc - 0.5) * 0.2
            if rc < 0.3:
                penalties.append(f"poor regime fit {rc:.2f}")

        # Volatility
        if volatility is not None and not math.isnan(volatility) and volatility is not None:
            try:
                vol = float(volatility)
                features["volatility"] = vol
                if vol > 0.03:
                    penalties.append(f"high volatility {vol:.4f}")
                    score -= 0.1
                elif vol < 0.005:
                    penalties.append(f"very low volatility {vol:.4f} (no opportunity)")
                    score -= 0.05
                else:
                    reasons.append(f"normal volatility {vol:.4f}")
            except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})

        # Liquidity
        if liquidity_score is not None:
            ls = max(0.0, min(1.0, float(liquidity_score)))
            features["liquidity_score"] = ls
            if ls < 0.3:
                penalties.append(f"low liquidity {ls:.2f}")
                score -= 0.2
            elif ls > 0.7:
                reasons.append(f"good liquidity {ls:.2f}")
                score += 0.1

        # Spread
        if spread_bps is not None:
            try:
                sp = float(spread_bps)
                features["spread_bps"] = sp
                if sp > 50:
                    penalties.append(f"wide spread {sp:.1f} bps")
                    score -= 0.2
                elif sp < 10:
                    reasons.append(f"tight spread {sp:.1f} bps")
                    score += 0.05
            except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})

        # Trend alignment
        if trend_alignment is not None:
            ta = max(-1.0, min(1.0, float(trend_alignment)))
            features["trend_alignment"] = ta
            score += ta * 0.1
            if ta > 0.5:
                reasons.append(f"trend aligned {ta:.2f}")
            elif ta < -0.5:
                penalties.append(f"trend opposed {ta:.2f}")
                score -= 0.15

        # Market context
        if market_context:
            features["market_context"] = market_context
            # simple: if broad market direction aligns, small boost
            md = market_context.get("market_direction")
            if md == "up" and trend_alignment is not None and trend_alignment > 0:
                reasons.append("market alignment")
                score += 0.05
            elif md == "down" and trend_alignment is not None and trend_alignment < 0:
                reasons.append("market alignment")
                score += 0.05

        # Event context
        if event_context:
            features["event_context"] = event_context
            resp = event_context.get("arena_response")
            if resp == "NO_TRADE":
                penalties.append("event NO_TRADE context")
                score -= 0.3
            elif resp == "IGNORE":
                pass
            elif resp in ("MONITOR", "EVALUATE"):
                reasons.append(f"event {resp}")

        # Recent OOS
        if recent_oos_expectancy is not None and not math.isnan(recent_oos_expectancy):
            oos = float(recent_oos_expectancy)
            features["recent_oos_expectancy"] = oos
            if oos > 0:
                score += min(0.15, oos * 5)
                reasons.append(f"positive OOS {oos:.4f}")
            else:
                penalties.append(f"negative OOS {oos:.4f}")
                score -= 0.2

        # Reward/risk
        if reward_risk is not None:
            try:
                rr = float(reward_risk)
                features["reward_risk"] = rr
                if rr > 2:
                    score += 0.1
                    reasons.append(f"good R/R {rr:.2f}")
                elif rr < 1:
                    penalties.append(f"poor R/R {rr:.2f}")
                    score -= 0.1
            except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})

        # Stop distance
        if stop_distance is not None:
            try:
                sd = float(stop_distance)
                features["stop_distance"] = sd
                if sd <= 0 or math.isnan(sd) or math.isinf(sd):
                    penalties.append("invalid stop distance")
                    score -= 0.2
                elif sd > 0.1:
                    penalties.append(f"wide stop {sd:.3f}")
                    score -= 0.05
            except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})

        # Signal age / freshness
        if signal_age is not None:
            try:
                age = int(signal_age)
                features["signal_age"] = age
                if age > 5:
                    penalties.append(f"stale signal age {age}")
                    score -= 0.15
                elif age == 0:
                    reasons.append("fresh signal")
                    score += 0.05
                if alpha_decay_factor is not None:
                    ad = float(alpha_decay_factor)
                    features["alpha_decay_factor"] = ad
                    score *= max(0.5, min(1.0, ad))
                    if ad < 0.7:
                        penalties.append(f"decayed signal {ad:.2f}")
            except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})

        # Portfolio correlation
        if portfolio_correlation is not None:
            try:
                pc = float(portfolio_correlation)
                features["portfolio_correlation"] = pc
                if pc > 0.7:
                    penalties.append(f"high correlation {pc:.2f}")
                    score -= 0.2
                elif pc < 0.3:
                    reasons.append(f"diversifying {pc:.2f}")
                    score += 0.05
            except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})

        # Missing feature penalty (transparency)
        if strategy_confidence is None and expected_net_edge is None:
            penalties.append("missing core features")
            score -= 0.1
            confidence *= 0.7

        # Clamp
        raw = score
        score = max(0.0, min(1.0, score))
        # Confidence based on penalties vs reasons
        if penalties:
            confidence = max(0.1, confidence - len(penalties) * 0.05)
        if reasons:
            confidence = min(1.0, confidence + len(reasons) * 0.03)

        features["raw_score"] = raw

        return QualityResult(
            quality_score=score,
            confidence=float(max(0.0, min(1.0, confidence))),
            reasons=reasons,
            penalties=penalties,
            supporting_features=features,
            raw_score=raw,
        )

    def passes(self, result: QualityResult) -> bool:
        return result.quality_score >= self.min_score
