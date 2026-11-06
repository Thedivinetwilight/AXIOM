"""Vantage Markets through the official MetaTrader 5 terminal integration.

No module in this package connects on import.  Live order placement requires the
explicit operator lifecycle in :mod:`app.vantage.service`; tests use a fake
terminal and never require a broker account.
"""

from app.vantage.models import ConnectionState, Lifecycle, SessionMode

__all__ = ["ConnectionState", "Lifecycle", "SessionMode"]
