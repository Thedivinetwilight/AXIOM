"""Market event data models — deterministic, no external dependencies."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from uuid import uuid4


# Event types that could materially affect financial markets (objective, not political judgment)
EVENT_TYPES = {
    "war",
    "military_escalation",
    "ceasefire",
    "peace_negotiation",
    "sanctions",
    "sanctions_relief",
    "tariffs",
    "trade_restriction",
    "trade_deal",
    "policy_decision",
    "central_bank",
    "oil_supply",
    "gas_supply",
    "shipping_disruption",
    "regulatory",
    "geopolitical",
    "other",
}

# Valid arena responses (information-only; never directly trades)
ARENA_RESPONSES = {"IGNORE", "MONITOR", "EVALUATE", "NO_TRADE"}

# Action taken vocabulary (observational, existing architecture decides)
ACTIONS = {"NONE", "MONITORED", "EVALUATED", "NO_TRADE", "CONTEXT_PROVIDED", "ROUTER_CONSULTED"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for dedup comparison."""
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\s]", "", text)
    return text[:200]


def event_fingerprint(headline: str, event_type: str, affected_countries: list) -> str:
    """Stable dedup fingerprint: headline core + type + countries."""
    core = _normalize_text(headline)
    countries = ",".join(sorted([c.lower() for c in (affected_countries or [])]))
    raw = f"{core}|{event_type}|{countries}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class MarketEventSource:
    """One source article for an event (multiple sources → one event)."""
    source: str
    source_url: str
    fetched_ts: str = field(default_factory=_now_iso)
    headline: str = ""
    raw_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MarketEventSource":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class MarketObservation:
    """Post-event market response for one window."""
    window: str  # 5m,30m,1h,1d,3d,5d
    observed_ts: str = field(default_factory=_now_iso)
    price_movement: float | None = None
    volatility: float | None = None
    volume: float | None = None
    affected_instrument: str | None = None
    arena_action: str | None = None
    result: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MarketEvent:
    """Canonical market-relevant event (deduplicated)."""
    event_id: str = field(default_factory=lambda: uuid4().hex)
    timestamp: str = field(default_factory=_now_iso)
    headline: str = ""
    summary: str = ""
    event_type: str = "other"
    affected_countries: list = field(default_factory=list)
    affected_markets: list = field(default_factory=list)
    affected_assets: list = field(default_factory=list)
    affected_sectors: list = field(default_factory=list)
    confidence: float = 0.5  # 0-1, reduced on conflict / weak source
    relevance: float = 0.5  # 0-1, high for market-moving
    expected_market_effect: str = ""
    arena_response: str = "IGNORE"  # IGNORE|MONITOR|EVALUATE|NO_TRADE
    action_taken: str = "NONE"
    strategy_used: str | None = None
    instruments_affected: list = field(default_factory=list)
    result_after_event: dict | None = None
    sources: list = field(default_factory=list)  # list[MarketEventSource dict]
    raw_payload: dict = field(default_factory=dict)
    created_ts: str = field(default_factory=_now_iso)
    # internal: fingerprint for dedup, transmission paths
    fingerprint: str = ""
    transmission_paths: list = field(default_factory=list)
    conflict_note: str | None = None

    def __post_init__(self):
        if not self.fingerprint and self.headline:
            self.fingerprint = event_fingerprint(self.headline, self.event_type, self.affected_countries)
        # clamp
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.relevance = max(0.0, min(1.0, float(self.relevance)))
        if self.event_type not in EVENT_TYPES:
            self.event_type = "other"
        if self.arena_response not in ARENA_RESPONSES:
            self.arena_response = "IGNORE"

    def to_dict(self) -> dict:
        d = asdict(self)
        # ensure JSON-safe
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "MarketEvent":
        # filter unknown keys
        known = set(cls.__dataclass_fields__.keys())
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    def to_record(self) -> dict:
        """To repository row dict (JSON payload)."""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "headline": self.headline,
            "summary": self.summary,
            "event_type": self.event_type,
            "affected_countries": self.affected_countries,
            "affected_markets": self.affected_markets,
            "affected_assets": self.affected_assets,
            "affected_sectors": self.affected_sectors,
            "confidence": self.confidence,
            "relevance": self.relevance,
            "expected_market_effect": self.expected_market_effect,
            "arena_response": self.arena_response,
            "action_taken": self.action_taken,
            "strategy_used": self.strategy_used,
            "instruments_affected": self.instruments_affected,
            "result_after_event": self.result_after_event,
            "sources": self.sources,
            "raw_payload": self.raw_payload,
            "created_ts": self.created_ts,
            "fingerprint": self.fingerprint,
            "transmission_paths": self.transmission_paths,
            "conflict_note": self.conflict_note,
        }
