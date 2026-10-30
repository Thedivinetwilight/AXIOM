"""AXIOM AI & Model Lab — strictly PAPER / RESEARCH / ADVISORY ONLY.

Invariants enforced across this package:
1. Zero imports of live broker order clients (`AlpacaTradingClient`, Upstox GTT order client, MT5 order sender).
2. Forecasts, research runs, model promotions, and model imports NEVER create, arm, activate, or modify any trading session or risk limit.
3. Missing/corrupted Kronos artifacts or unavailable CPU runtime ALWAYS fail closed (`NOT_INSTALLED`, `NOT_VERIFIED`, `ERROR`, `STALE`) — mock forecasts are never substituted as real.
"""

from __future__ import annotations

PAPER_AND_RESEARCH_ONLY = True
BROKER_ORDER_ENDPOINTS_USED = False
