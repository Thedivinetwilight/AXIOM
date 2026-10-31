"""Explainable long/short candidates with exact, per-instrument evidence gates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Iterable

from app.autonomy.models import Candidate, Forecast, StrategyEvidence
from app.autonomy.risk import initial_stop, target_for
from app.data.models import Candle, parse_timestamp

FAMILIES = ("TREND_MOMENTUM", "MA_BREAKOUT", "MEAN_REVERSION", "KRONOS_INFORMED")


def _sma(values: list[float], length: int) -> float:
    return sum(values[-length:]) / length


def _ema(values: list[float], length: int) -> float:
    alpha = 2 / (length + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def _rsi(values: list[float], length: int = 14) -> float:
    changes = [values[i] - values[i - 1] for i in range(len(values) - length, len(values))]
    gains = sum(max(0, change) for change in changes) / length
    losses = sum(max(0, -change) for change in changes) / length
    if losses == 0:
        return 100.0 if gains else 50.0
    return 100 - 100 / (1 + gains / losses)


def classify_regime(candles: list[Candle]) -> str:
    if len(candles) < 21:
        return "INSUFFICIENT"
    closes = [bar.close for bar in candles]
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(len(closes) - 20, len(closes)))
    efficiency = abs(closes[-1] - closes[-21]) / path if path else 0.0
    ranges = [max(bar.high - bar.low, 1e-12) / bar.close for bar in candles[-20:]]
    if len(candles) >= 40:
        prior = [max(bar.high - bar.low, 1e-12) / bar.close for bar in candles[-40:-20]]
        if statistics.mean(ranges) > max(1e-12, statistics.mean(prior)) * 1.8:
            return "VOLATILE"
    return "TREND" if efficiency >= 0.35 else "RANGE"


def indicator_setup(family: str, side: str, candles: list[Candle],
                    forecast: Forecast | None = None, *,
                    require_forecast_validation: bool = True) -> tuple[bool, str, float]:
    if len(candles) < 55:
        return False, "At least 55 completed bars are required", 0.0
    closes = [bar.close for bar in candles]
    last = closes[-1]
    if family == "TREND_MOMENTUM":
        fast, slow = _ema(closes[-55:], 20), _ema(closes[-55:], 50)
        momentum = last / closes[-21] - 1
        triggered = fast > slow and momentum > 0 if side == "BUY" else fast < slow and momentum < 0
        strength = min(1.0, abs(momentum) * 20 + abs(fast / slow - 1) * 10)
        return triggered, f"EMA20/EMA50={fast:.4f}/{slow:.4f}; 20-bar momentum={momentum:.2%}", strength
    if family == "MA_BREAKOUT":
        fast, slow = _sma(closes, 10), _sma(closes, 30)
        prior = candles[-21:-1]
        resistance, support = max(bar.high for bar in prior), min(bar.low for bar in prior)
        triggered = fast > slow and last > resistance if side == "BUY" else fast < slow and last < support
        boundary = resistance if side == "BUY" else support
        strength = min(1.0, abs(last / boundary - 1) * 100 + abs(fast / slow - 1) * 10)
        return triggered, f"SMA10/SMA30={fast:.4f}/{slow:.4f}; breakout boundary={boundary:.4f}", strength
    if family == "MEAN_REVERSION":
        rsi = _rsi(closes)
        mean = _sma(closes, 20)
        deviation = statistics.pstdev(closes[-20:])
        upper, lower = mean + 2 * deviation, mean - 2 * deviation
        triggered = rsi < 30 and last < lower if side == "BUY" else rsi > 70 and last > upper
        distance = abs(last - mean) / max(deviation, 1e-12)
        return triggered, f"RSI14={rsi:.1f}; close={last:.4f}; Bollinger={lower:.4f}/{upper:.4f}", min(1.0, distance / 4)
    if family == "KRONOS_INFORMED":
        if forecast is None:
            return False, "Kronos forecast unavailable; non-Kronos families remain separate", 0.0
        if require_forecast_validation and (not forecast.validated or not forecast.validation_id):
            return False, "Kronos forecast is not linked to frozen chronological validation", 0.0
        # Paired ablation: the baseline is this exact same trend/momentum setup
        # without the forecast filter, not an unrelated strategy's results.
        base_triggered, base_explanation, base_strength = indicator_setup(
            "TREND_MOMENTUM", side, candles, None,
            require_forecast_validation=require_forecast_validation,
        )
        forecast.validate(last)
        expected = forecast.mean_close / last - 1
        probability = forecast.up_probability if side == "BUY" else 1 - forecast.up_probability
        forecast_confirms = expected > 0 and probability >= 0.55 if side == "BUY" \
            else expected < 0 and probability >= 0.55
        triggered = base_triggered and forecast_confirms
        interval = max(1e-12, forecast.q90_close - forecast.q10_close)
        calibration_confidence = min(1.0, abs(expected) * last / interval)
        return triggered, (
            f"paired trend ablation: {base_explanation}; verified Kronos horizon={forecast.horizon}; "
            f"expected={expected:.2%}; directional probability={probability:.1%}; "
            f"artifact={forecast.artifact_digest[:12]}"
        ), min(1.0, (base_strength + probability * calibration_confidence) / 2)
    return False, "Unknown strategy family", 0.0


@dataclass(frozen=True)
class StrategyOutput:
    candidates: tuple[Candidate, ...]
    diagnostics: tuple[dict, ...]


def generate_candidates(instrument_key: str, candles: list[Candle], *, tick_size: float,
                        timeframe: str, swing_bars: int, reward_risk: float,
                        forecast: Forecast | None, kronos_status: str,
                        evidence: Iterable[StrategyEvidence] = (),
                        require_forecast_validation: bool = True) -> StrategyOutput:
    """Evaluate every family and side independently; never merge their evidence."""
    bar = candles[-1]
    regime = classify_regime(candles)
    evidence_by_key = {(e.strategy_family, e.side): e for e in evidence
                       if e.instrument_key == instrument_key and e.timeframe == timeframe}
    candidates: list[Candidate] = []
    diagnostics: list[dict] = []
    for family in FAMILIES:
        if family == "KRONOS_INFORMED" and forecast is None:
            diagnostics.append({"family": family, "status": kronos_status,
                                "fallback": "validated non-Kronos candidates only"})
            continue
        for side in ("BUY", "SELL"):
            triggered, explanation, strength = indicator_setup(
                family, side, candles, forecast,
                require_forecast_validation=require_forecast_validation,
            )
            matched = evidence_by_key.get((family, side))
            diagnostics.append({"family": family, "side": side, "regime": regime,
                                "triggered": triggered, "evidence_id": matched.evidence_id if matched else "",
                                "explanation": explanation})
            if not triggered:
                continue
            try:
                stop, swing_evidence = initial_stop(candles, side, bar.close, tick_size,
                                                    lookback=swing_bars)
                target = target_for(side, bar.close, stop, reward_risk, tick_size)
            except ValueError as exc:
                diagnostics.append({"family": family, "side": side, "rejected": str(exc)})
                continue
            signal_id = hashlib.sha256(
                f"{instrument_key}|{bar.timestamp.isoformat()}|{family}|{side}".encode()
            ).hexdigest()[:32]
            candidates.append(Candidate(
                instrument_key=instrument_key,
                signal_id=signal_id,
                bar_timestamp=bar.timestamp,
                family=family,
                side=side,
                entry_price=bar.close,
                stop_price=stop,
                target_price=target,
                regime=regime,
                score=matched.risk_adjusted_score if matched else strength,
                explanation=f"{explanation}; {swing_evidence}",
                evidence_id=matched.evidence_id if matched else "",
                kronos_used=family == "KRONOS_INFORMED",
                fallback_reason="" if family == "KRONOS_INFORMED" else kronos_status,
            ))
    candidates.sort(key=lambda item: item.score, reverse=True)
    return StrategyOutput(tuple(candidates), tuple(diagnostics))


class EvidenceRegistry:
    """Load immutable research reports; malformed or synthetic-live evidence fails closed."""

    def __init__(self, evidence: Iterable[StrategyEvidence] = ()):
        self._items: dict[str, StrategyEvidence] = {}
        for item in evidence:
            self.add(item)

    def add(self, item: StrategyEvidence) -> None:
        if item.evidence_id in self._items:
            raise ValueError("Duplicate strategy evidence id")
        for value in (item.data_sha256, item.config_sha256):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value.lower()):
                raise ValueError("Evidence data/config hashes must be SHA-256")
        if item.generated_at.tzinfo is None or item.data_end.tzinfo is None:
            raise ValueError("Evidence timestamps must be timezone-aware")
        if not isinstance(item.breakdown, dict):
            raise ValueError("Evidence breakdown must be an object")
        self._items[item.evidence_id] = item

    def for_instrument(self, instrument_key: str) -> tuple[StrategyEvidence, ...]:
        return tuple(item for item in self._items.values() if item.instrument_key == instrument_key)

    def match(self, candidate: Candidate, timeframe: str, *, live: bool,
              kronos_digest: str = "") -> tuple[bool, str]:
        item = self._items.get(candidate.evidence_id)
        if item is None:
            return False, "No exact validated evidence for this instrument/family/side"
        if item.kronos_required != candidate.kronos_used:
            return False, "Kronos attribution differs from frozen evidence"
        return item.executable(candidate.instrument_key, candidate.family, candidate.side,
                               timeframe, candidate.regime, live=live,
                               kronos_digest=kronos_digest)

    def public(self) -> list[dict]:
        return [{
            "evidence_id": item.evidence_id,
            "instrument_key": item.instrument_key,
            "strategy_family": item.strategy_family,
            "side": item.side,
            "timeframe": item.timeframe,
            "regime": item.regime,
            "walk_forward_runs": item.walk_forward_runs,
            "holdout_runs": item.holdout_runs,
            "trades": item.trades,
            "net_expectancy": item.net_expectancy,
            "risk_adjusted_score": item.risk_adjusted_score,
            "baseline_risk_adjusted_score": item.baseline_risk_adjusted_score,
            "synthetic": item.synthetic,
            "provenance_class": item.provenance_class,
            "provenance_verified": item.provenance_verified,
            "provenance_effect": (
                "research only; no PAPER or LIVE execution" if item.synthetic else
                "PAPER structure only; LIVE blocked" if not item.provenance_verified else
                "trusted in-process verification; all other gates still apply"
            ),
            "paper_eligible_structure": not item.synthetic,
            "paper_execution_effect": (
                "BLOCKED_SYNTHETIC_RESEARCH_ONLY" if item.synthetic
                else "PAPER_ONLY_AFTER_ALL_QUALITY_GATES"
            ),
            "kronos_required": item.kronos_required,
            "live_eligible_structure": not item.synthetic and item.provenance_verified,
            "live_execution_effect": (
                "BLOCKED_UNLESS_INDEPENDENTLY_VERIFIED_AND_ALL_QUALITY_GATES_PASS"
            ),
        } for item in self._items.values()]

    @classmethod
    def load(cls, directory: Path) -> "EvidenceRegistry":
        registry = cls()
        if not directory.exists():
            return registry
        for path in sorted(directory.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("schema") != "axiom.autonomy.evidence.v1":
                raise ValueError(f"Unsupported evidence schema in {path.name}")
            if raw.get("provenance_verified") is True:
                raise ValueError(
                    f"Independently verified provenance cannot be asserted by operator JSON: {path.name}"
                )
            item = StrategyEvidence(
                evidence_id=raw["evidence_id"], instrument_key=raw["instrument_key"],
                strategy_family=raw["strategy_family"], side=raw["side"],
                timeframe=raw["timeframe"], regime=raw["regime"],
                generated_at=parse_timestamp(raw["generated_at"]),
                data_end=parse_timestamp(raw["data_end"]), data_sha256=raw["data_sha256"],
                config_sha256=raw["config_sha256"], walk_forward_runs=raw["walk_forward_runs"],
                holdout_runs=raw["holdout_runs"], trades=raw["trades"],
                net_expectancy=raw["net_expectancy"], max_drawdown=raw["max_drawdown"],
                risk_adjusted_score=raw["risk_adjusted_score"],
                baseline_risk_adjusted_score=raw["baseline_risk_adjusted_score"],
                costs_modelled=raw["costs_modelled"], gaps_modelled=raw["gaps_modelled"],
                partial_fills_modelled=raw["partial_fills_modelled"],
                rejections_modelled=raw["rejections_modelled"],
                survivorship_aware=raw["survivorship_aware"],
                upstox_authoritative=raw["upstox_authoritative"],
                holdout_locked=raw["holdout_locked"], synthetic=raw.get("synthetic", False),
                kronos_required=raw.get("kronos_required", False),
                kronos_artifact_digest=raw.get("kronos_artifact_digest", ""),
                breakdown=raw.get("breakdown", {}),
            )
            registry.add(item)
        return registry
