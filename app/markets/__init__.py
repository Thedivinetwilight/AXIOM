"""AXIOM market abstractions (AXIOM-MONEY-PRINTER master spec section 5).

A market is the unit of context: exchange metadata, timezone, trading
sessions, holidays, currency, tick/lot rules, symbol identity, data sources
and broker mapping. Adding a market must not require redesigning the core —
each market is a dataclass entry in the registry.
"""

from app.markets.registry import (
    MARKET_IDS,
    MarketDefinition,
    SessionState,
    get_market,
    list_markets,
    market_session_state,
    parse_instrument_identity,
)

__all__ = [
    "MARKET_IDS",
    "MarketDefinition",
    "SessionState",
    "get_market",
    "list_markets",
    "market_session_state",
    "parse_instrument_identity",
]
