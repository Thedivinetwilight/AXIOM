"""Separately gated Alpaca brokerage path (U.S. equities/ETFs).

This package is the ONLY place in AXIOM that may reference Alpaca order
endpoints. It is distinct from the market-data module (``app/data/alpaca.py``,
read-only) and from local PAPER (``app/paper_trading``). LIVE is disabled by
default and unreachable without an explicit, armed+activated gate permit.
"""

from app.alpaca_broker.client import (
    AlpacaTradingClient,
    BrokerError,
    BrokerRejected,
    LiveEnvironmentBlocked,
    LivePermit,
    PAPER_HOST,
    LIVE_HOST,
)
from app.alpaca_broker.gate import BrokerLiveGate, GateError, GateState
from app.alpaca_broker.service import (
    AlpacaBrokerService,
    BrokerRiskLimits,
    BrokerStateBlocked,
)

__all__ = [
    "AlpacaTradingClient", "AlpacaBrokerService", "BrokerLiveGate", "BrokerRiskLimits",
    "BrokerError", "BrokerRejected", "BrokerStateBlocked", "GateError", "GateState",
    "LiveEnvironmentBlocked", "LivePermit", "PAPER_HOST", "LIVE_HOST",
]
