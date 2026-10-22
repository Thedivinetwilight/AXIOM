"""India onshore exchange currency-derivative research primitives.

This package deliberately does not implement OTC spot, CFDs, broker orders, or
live activation. The only instrument definition is the NSE USDINR currency
future; provider/account entitlement is an external precondition.
"""

from app.forex.contracts import (
    MVP_PAIR,
    ForexContract,
    ForexPaperBook,
    ObservedQuote,
    contract_from_upstox_record,
)

__all__ = [
    "MVP_PAIR",
    "ForexContract",
    "ForexPaperBook",
    "ObservedQuote",
    "contract_from_upstox_record",
]
