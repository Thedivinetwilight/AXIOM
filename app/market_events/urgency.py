"""Event Urgency — lightweight impact/confidence/novelty/urgency.

HIGH urgency triggers targeted re-evaluation of affected candidates.
Do NOT rescan entire universe on every news item; use affected instruments/sectors.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List


@dataclass
class UrgencyResult:
    impact: float  # 0..1
    confidence: float
    novelty: float
    urgency: str  # LOW / MEDIUM / HIGH / CRITICAL
    score: float  # 0..1
    reasons: List[str]
    affected: List[str]


class EventUrgencyScorer:
    def __init__(self):
        self._seen_headlines: dict[str, float] = {}  # headline hash -> ts

    def _novelty(self, headline: str, fingerprint: str | None = None) -> tuple[float, str]:
        # Simple: if headline seen recently (<1h), low novelty
        now = time.time()
        key = fingerprint or headline[:50]
        last = self._seen_headlines.get(key)
        if last and now - last < 3600:
            return 0.2, "duplicate_recent"
        if last and now - last < 86400:
            return 0.5, "seen_within_day"
        # New
        self._seen_headlines[key] = now
        # prune old
        if len(self._seen_headlines) > 500:
            oldest = min(self._seen_headlines, key=lambda k: self._seen_headlines[k])
            self._seen_headlines.pop(oldest)
        return 0.9, "novel"

    def score(
        self,
        *,
        event_type: str,
        confidence: float,
        relevance: float,
        affected_markets: List[str] | None = None,
        affected_sectors: List[str] | None = None,
        headline: str = "",
        fingerprint: str | None = None,
    ) -> UrgencyResult:
        reasons = []
        # Impact from event_type
        high_impact_types = {"war", "military_escalation", "oil_supply", "gas_supply", "sanctions", "tariffs", "central_bank"}
        medium_impact = {"ceasefire", "trade_restriction", "shipping_disruption", "regulatory", "geopolitical"}
        if event_type in high_impact_types:
            impact = 0.9
            reasons.append(f"high impact type {event_type}")
        elif event_type in medium_impact:
            impact = 0.6
            reasons.append(f"medium impact {event_type}")
        else:
            impact = 0.3
            reasons.append(f"low impact {event_type}")

        # Adjust by relevance
        impact = max(0.1, min(1.0, impact * (0.5 + relevance)))
        if relevance > 0.8:
            reasons.append(f"high relevance {relevance:.2f}")

        # Confidence
        conf = max(0.0, min(1.0, float(confidence)))
        if conf < 0.4:
            reasons.append(f"low confidence {conf:.2f} reduces urgency")

        # Novelty
        novelty, nov_reason = self._novelty(headline, fingerprint)
        reasons.append(nov_reason)
        if novelty < 0.3:
            impact *= 0.5

        # Score
        score = (impact * 0.4 + conf * 0.3 + novelty * 0.3)
        score = max(0.0, min(1.0, score))

        if score > 0.8 and conf > 0.6 and novelty > 0.7:
            urgency = "CRITICAL"
        elif score > 0.65:
            urgency = "HIGH"
        elif score > 0.4:
            urgency = "MEDIUM"
        else:
            urgency = "LOW"

        affected = list(affected_markets or []) + list(affected_sectors or [])
        # Deduplicate and limit
        affected = list(dict.fromkeys(affected))[:5]

        return UrgencyResult(impact, conf, novelty, urgency, score, reasons, affected)

    def should_trigger_reevaluation(self, result: UrgencyResult) -> tuple[bool, List[str]]:
        if result.urgency in ("HIGH", "CRITICAL"):
            # Targeted rescan only for affected
            return True, result.affected
        return False, []

    def targeted_rescan(self, universe: List[dict], affected: List[str]) -> List[dict]:
        # Narrow work to affected instruments/sectors
        if not affected:
            return []
        narrowed = []
        for u in universe:
            # Check if any affected term in symbol/sector
            text = f"{u.get('symbol','')} {u.get('sector','')} {u.get('instrument_key','')}".lower()
            if any(a.lower() in text for a in affected):
                narrowed.append(u)
        # If no match, return empty (don't scan whole universe)
        return narrowed
