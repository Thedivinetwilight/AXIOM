"""Thin wrapper around the official MetaTrader5 Python package.

Documentation: https://www.mql5.com/en/docs/python_metatrader5

The import is lazy so Linux CI and paper-only use do not need the Windows MT5
package. No method in this wrapper logs its arguments or results.
"""

from __future__ import annotations

from datetime import datetime
import importlib.util
import os
from typing import Any


class MT5PackageUnavailable(RuntimeError):
    pass


def as_dict(row: Any) -> dict:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "_asdict"):
        return dict(row._asdict())
    return {
        name: getattr(row, name)
        for name in dir(row)
        if not name.startswith("_") and not callable(getattr(row, name))
    }


def mt5_package_available() -> bool:
    """Report module presence only; never initialize or contact a terminal."""
    if os.name != "nt":
        return False
    try:
        return importlib.util.find_spec("MetaTrader5") is not None
    except (ImportError, ValueError):
        return False


class OfficialMT5Gateway:
    """Production gateway. A terminal is required; this is not a web API."""

    def __init__(self):
        if os.name != "nt":
            raise MT5PackageUnavailable(
                "The official MetaTrader5 integration requires Windows and an MT5 terminal"
            )
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise MT5PackageUnavailable(
                "MetaTrader5 Python package is not installed in this AXIOM build"
            ) from exc
        self.mt5 = mt5

    def constant(self, name: str, default=None):
        return getattr(self.mt5, name, default)

    def initialize(
        self, *, login: int, password: str, server: str,
        terminal_path: str = "", timeout_ms: int = 15_000,
    ) -> bool:
        kwargs = {
            "login": int(login), "password": password, "server": server,
            "timeout": int(timeout_ms), "portable": False,
        }
        if terminal_path:
            return bool(self.mt5.initialize(terminal_path, **kwargs))
        return bool(self.mt5.initialize(**kwargs))

    def shutdown(self) -> None:
        self.mt5.shutdown()

    def last_error(self) -> tuple:
        try:
            return tuple(self.mt5.last_error())
        except Exception:
            return (-1, "MT5 error unavailable")

    def account_info(self) -> dict:
        return as_dict(self.mt5.account_info())

    def terminal_info(self) -> dict:
        return as_dict(self.mt5.terminal_info())

    def symbols_get(self) -> list[dict] | None:
        rows = self.mt5.symbols_get()
        return [as_dict(row) for row in rows] if rows is not None else None

    def symbol_info(self, symbol: str) -> dict:
        return as_dict(self.mt5.symbol_info(symbol))

    def symbol_info_tick(self, symbol: str) -> dict:
        return as_dict(self.mt5.symbol_info_tick(symbol))

    def symbol_select(self, symbol: str, enabled: bool = True) -> bool:
        return bool(self.mt5.symbol_select(symbol, enabled))

    def order_calc_margin(self, order_type: int, symbol: str, volume: float, price: float):
        return self.mt5.order_calc_margin(order_type, symbol, volume, price)

    def order_check(self, request: dict) -> dict:
        return as_dict(self.mt5.order_check(request))

    def order_send(self, request: dict) -> dict:
        return as_dict(self.mt5.order_send(request))

    def positions_get(self) -> list[dict] | None:
        rows = self.mt5.positions_get()
        return [as_dict(row) for row in rows] if rows is not None else None

    def orders_get(self) -> list[dict] | None:
        rows = self.mt5.orders_get()
        return [as_dict(row) for row in rows] if rows is not None else None

    def history_orders_get(self, start: datetime, end: datetime) -> list[dict] | None:
        rows = self.mt5.history_orders_get(start, end)
        return [as_dict(row) for row in rows] if rows is not None else None

    def history_deals_get(self, start: datetime, end: datetime) -> list[dict] | None:
        rows = self.mt5.history_deals_get(start, end)
        return [as_dict(row) for row in rows] if rows is not None else None

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start: int, count: int) -> list[dict] | None:
        rows = self.mt5.copy_rates_from_pos(symbol, timeframe, start, count)
        if rows is None:
            return None
        names = getattr(getattr(rows, "dtype", None), "names", None)
        result = []
        for row in rows:
            if names:
                result.append({name: row[name].item() if hasattr(row[name], "item") else row[name] for name in names})
            elif hasattr(row, "_asdict"):
                result.append(as_dict(row))
            else:
                result.append(dict(row))
        return result
