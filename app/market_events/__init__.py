"""Market Event Listener — public internet intelligence, additive to Arena 3.0.4.

This package is an INFORMATION SOURCE only. It never directly places orders,
never bypasses Strategy Router, lifecycle gates, hard risk, emergency STOP,
broker safeguards, authentication, or LIVE_TRADING controls.

Flow: internet sources → detection → validation → normalization/dedup →
impact assessment → market/strategy context → Strategy Router → hard risk →
execution (existing) → record action + outcome.
"""

from app.market_events.models import MarketEvent, MarketEventSource, MarketObservation
from app.market_events.listener import MarketEventListener
from app.market_events.collector import EventCollector
from app.market_events.impact import assess, decide_arena_response, TRANSMISSION

__all__ = ["MarketEvent", "MarketEventSource", "MarketObservation", "MarketEventListener", "EventCollector", "assess", "decide_arena_response", "TRANSMISSION"]
