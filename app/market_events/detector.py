"""Relevant-event detection and source validation — deterministic."""

from __future__ import annotations

import re


# Keywords that indicate market-relevant events (objective, not political judgment)
RELEVANT_KEYWORDS = {
    # wars etc
    "war", "military", "escalation", "ceasefire", "peace negotiation", "sanctions", "sanction",
    "tariff", "trade restriction", "trade deal", "trade war", "u.s.-china", "us-china",
    "economic policy", "central bank", "federal reserve", "fed decision", "interest rate",
    "oil supply", "gas supply", "opec", "crude", "shipping disruption", "supply chain",
    "regulatory", "geopolitical", "government",
    # broader
    "inflation", "energy sector", "commodity", "currency", "equity",
}

IRRELEVANT_MARKERS = {
    "sports", "celebrity", "entertainment", "local weather", "recipe", "horoscope",
    "gossip",
}

# Source validation: reputable domains (allowlist example, not exhaustive)
REPUTABLE_DOMAINS = {
    "reuters.com", "apnews.com", "bloomberg.com", "wsj.com", "ft.com",
    "bbc.com", "nytimes.com", "economist.com", "centralbank.org", "federalreserve.gov",
    "gov", "reuters", "ap", "bloomberg",
}


def is_relevant(headline: str, summary: str = "") -> tuple[bool, float, str]:
    """Determine if an article is market-relevant. Returns (relevant, relevance_score, reason)."""
    text = (headline + " " + summary).lower()
    # Check irrelevant first
    for marker in IRRELEVANT_MARKERS:
        if marker in text:
            return False, 0.1, f"irrelevant marker: {marker}"
    # Count relevant keywords
    hits = []
    for kw in RELEVANT_KEYWORDS:
        if kw.lower() in text:
            hits.append(kw)
    if not hits:
        return False, 0.2, "no market-relevant keywords"
    # relevance scales with hits but capped
    relevance = min(0.95, 0.4 + len(hits) * 0.15)
    return True, relevance, f"hits: {hits[:3]}"


def validate_source(source: str, source_url: str) -> tuple[bool, str, float]:
    """Validate source — returns (valid, reason, confidence_adjustment)."""
    if not source or not source_url:
        return False, "missing source/url", -0.3
    if not (source_url.startswith("http://") or source_url.startswith("https://")):
        return False, "invalid url scheme", -0.4
    # Check for obviously fake source
    if "fake" in source.lower() or "example.com" in source_url:
        return False, "fake source marker", -0.5
    # Reputable check — not required, but boosts confidence
    domain = source_url.lower()
    reputable = any(d in domain for d in REPUTABLE_DOMAINS)
    if reputable:
        return True, "reputable source", 0.1
    # Unknown but not invalid → valid with neutral
    return True, "unverified but syntactically valid", 0.0


def normalize_event_type(headline: str, summary: str) -> str:
    """Map text to event_type."""
    text = (headline + " " + summary).lower()
    mapping = [
        ("ceasefire", "ceasefire"),
        ("peace negotiation", "peace_negotiation"),
        ("war", "war"),
        ("military escalation", "military_escalation"),
        ("military", "military_escalation"),
        ("sanctions relief", "sanctions_relief"),
        ("sanction", "sanctions"),
        ("tariff", "tariffs"),
        ("trade restriction", "trade_restriction"),
        ("trade deal", "trade_deal"),
        ("central bank", "central_bank"),
        ("federal reserve", "central_bank"),
        ("interest rate", "central_bank"),
        ("oil supply", "oil_supply"),
        ("opec", "oil_supply"),
        ("gas supply", "gas_supply"),
        ("shipping disruption", "shipping_disruption"),
        ("supply chain", "shipping_disruption"),
        ("regulatory", "regulatory"),
        ("geopolitical", "geopolitical"),
        ("economic policy", "policy_decision"),
        ("government", "policy_decision"),
    ]
    for kw, typ in mapping:
        if kw in text:
            return typ
    return "other"


def extract_affected(summary: str, headline: str) -> tuple[list, list, list, list]:
    """Heuristic extraction of affected countries/markets/assets/sectors from text."""
    text = (headline + " " + summary).lower()
    countries = []
    if "u.s." in text or "us" in text or "america" in text:
        countries.append("US")
    if "china" in text:
        countries.append("China")
    if "russia" in text:
        countries.append("Russia")
    if "saudi" in text:
        countries.append("Saudi Arabia")
    if "iran" in text:
        countries.append("Iran")
    if "europe" in text or "eu " in text:
        countries.append("EU")
    if "uk " in text or "britain" in text:
        countries.append("UK")

    markets = []
    if "oil" in text or "crude" in text:
        markets.append("commodities")
    if "gas" in text:
        markets.append("energy")
    if "equity" in text or "stock" in text:
        markets.append("equities")
    if "currency" in text or "forex" in text or "dollar" in text:
        markets.append("FX")
    if "bond" in text:
        markets.append("bonds")

    assets = []
    if "crude" in text or "oil" in text:
        assets.append("crude_oil")
    if "gas" in text:
        assets.append("natural_gas")
    if "gold" in text:
        assets.append("gold")
    if "usd" in text or "dollar" in text:
        assets.append("USD")

    sectors = []
    if "energy" in text or "oil" in text or "gas" in text:
        sectors.append("energy")
    if "shipping" in text:
        sectors.append("shipping")
    if "technology" in text or "tech " in text:
        sectors.append("technology")
    if "finance" in text or "bank" in text:
        sectors.append("financials")
    if "inflation" in text:
        sectors.append("inflation_sensitive")

    return countries, markets, assets, sectors

