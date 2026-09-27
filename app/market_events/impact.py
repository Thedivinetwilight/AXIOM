"""Market-impact assessment — transmission paths, not simplistic buy/sell."""

from __future__ import annotations

from app.market_events.models import MarketEvent


# Transmission paths: event_type → plausible affected markets/assets/sectors + narrative
TRANSMISSION = {
    "oil_supply": {
        "paths": ["crude supply → crude price → energy sector → inflation-sensitive assets"],
        "markets": ["commodities", "energy"],
        "sectors": ["energy", "inflation_sensitive"],
        "effect": "Potential crude-price volatility; energy equities and inflation-linked assets may be affected",
    },
    "gas_supply": {
        "paths": ["gas supply → gas price → utilities → inflation"],
        "markets": ["commodities", "energy"],
        "sectors": ["energy", "utilities"],
        "effect": "Potential natural-gas price impact; utilities and energy-sensitive markets",
    },
    "war": {
        "paths": ["conflict → risk-off → safe havens, commodities, regional equities"],
        "markets": ["equities", "commodities", "FX"],
        "sectors": ["energy", "defense"],
        "effect": "Geopolitical risk may affect risk appetite, commodities, and regional markets",
    },
    "military_escalation": {
        "paths": ["escalation → risk-off → commodities/FX"],
        "markets": ["commodities", "equities"],
        "sectors": ["energy", "defense"],
        "effect": "Potential risk-off; commodities and regional equities sensitive",
    },
    "ceasefire": {
        "paths": ["de-escalation → risk-on → commodities normalize"],
        "markets": ["commodities", "equities"],
        "sectors": ["energy"],
        "effect": "Potential easing of risk premium; commodities may normalize",
    },
    "sanctions": {
        "paths": ["sanctions → trade/commodity flows → affected country assets"],
        "markets": ["commodities", "FX", "equities"],
        "sectors": ["energy", "financials"],
        "effect": "Trade/financial flows may be disrupted; affected country and commodity exposures",
    },
    "tariffs": {
        "paths": ["tariffs → input costs → affected industry margins → consumer prices"],
        "markets": ["equities", "FX", "commodities"],
        "sectors": ["technology", "industrials"],
        "effect": "Potential margin impact on affected industries; FX and consumer price implications",
    },
    "trade_restriction": {
        "paths": ["restriction → supply chain → sector costs"],
        "markets": ["equities", "commodities"],
        "sectors": ["industrials", "technology"],
        "effect": "Supply-chain implications for restricted industries",
    },
    "central_bank": {
        "paths": ["policy → rates → discount rates → equities/bonds/FX"],
        "markets": ["bonds", "equities", "FX"],
        "sectors": ["financials", "inflation_sensitive"],
        "effect": "Rate expectations may affect discount rates across asset classes",
    },
    "shipping_disruption": {
        "paths": ["shipping → logistics costs → goods prices → inflation"],
        "markets": ["commodities", "equities"],
        "sectors": ["shipping", "industrials"],
        "effect": "Logistics cost and supply implications",
    },
    "regulatory": {
        "paths": ["regulation → sector compliance costs / market structure"],
        "markets": ["equities"],
        "sectors": ["financials", "technology"],
        "effect": "Sector-specific regulatory impact",
    },
    "geopolitical": {
        "paths": ["geopolitical → broad risk sentiment"],
        "markets": ["equities", "FX"],
        "sectors": ["energy"],
        "effect": "Broad sentiment and regional exposure effects",
    },
    "other": {
        "paths": ["uncertain transmission — monitor"],
        "markets": [],
        "sectors": [],
        "effect": "Uncertain market transmission; monitor for further information",
    },
}


def assess(event: MarketEvent, *, market_context: dict | None = None) -> MarketEvent:
    """Enrich event with impact assessment; does NOT decide trade, only context.

    Sets: affected_*, expected_market_effect, transmission_paths, confidence/relevance adjustments.
    """
    market_context = market_context or {}
    # Base from event_type
    meta = TRANSMISSION.get(event.event_type, TRANSMISSION["other"])
    # Merge with heuristic extracted values (already in event from detector)
    # Ensure transmission paths set
    event.transmission_paths = meta["paths"]
    event.expected_market_effect = meta["effect"]
    # If already has affected markets/sectors from detection, merge
    for m in meta["markets"]:
        if m not in event.affected_markets:
            event.affected_markets.append(m)
    for s in meta["sectors"]:
        if s not in event.affected_sectors:
            event.affected_sectors.append(s)

    # Confidence adjustments: if already priced in (market_context says flat), reduce
    # If market_context indicates no current reaction, keep as is; don't force trade
    # This is where we avoid simplistic bad=SELL

    # Do not claim certainty when source uncertain
    if event.confidence < 0.4:
        event.expected_market_effect += " (low confidence — transmission uncertain)"
    if event.relevance < 0.3:
        event.expected_market_effect += " (low relevance — may not be market-moving)"

    return event


def decide_arena_response(
    event: MarketEvent,
    *,
    has_qualified_strategy: bool = False,
    market_reaction_contradicts: bool = False,
    already_priced_in: bool = False,
) -> str:
    """Decide arena response: IGNORE, MONITOR, EVALUATE, NO_TRADE — deterministic, not simplistic.

    Rules:
    - irrelevant or very low relevance → IGNORE
    - low confidence → MONITOR (don't trade)
    - already priced in or contradicts thesis → NO_TRADE
    - no qualified strategy → NO_TRADE (existing Strategy Router gate)
    - otherwise → EVALUATE (pass to router, but still needs hard risk)
    """
    if event.relevance < 0.25 or event.event_type == "other" and event.relevance < 0.4:
        return "IGNORE"
    if event.confidence < 0.35:
        return "MONITOR"
    if already_priced_in:
        return "NO_TRADE"
    if market_reaction_contradicts:
        return "NO_TRADE"
    if not has_qualified_strategy:
        # Must be able to decide NO_TRADE when no strategy qualifies
        return "NO_TRADE"
    if event.confidence < 0.5 or event.relevance < 0.5:
        return "MONITOR"
    # High relevance + medium-high confidence + qualified strategy → evaluate via router
    return "EVALUATE"

