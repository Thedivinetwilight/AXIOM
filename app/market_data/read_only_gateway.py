"""Minimal terminal-hosted MT5 gateway for market data only.

The production object deliberately has no trading, trading-history, position,
margin, or trade-precheck methods.  The official package is imported lazily so
paper/replay and Linux tests do not need a Windows terminal.
"""

from __future__ import annotations

import os
from typing import Any

from app.vantage.gateway import MT5PackageUnavailable


_TIMEFRAME_CONSTANTS = {
    "TIMEFRAME_M1", "TIMEFRAME_M5", "TIMEFRAME_M15", "TIMEFRAME_M30",
    "TIMEFRAME_H1", "TIMEFRAME_H4", "TIMEFRAME_D1",
}


def _as_dict(row: Any) -> dict:
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


class ReadOnlyMT5Gateway:
    """The only production MT5 object supplied to the market-data service."""

    def __init__(self):
        if os.name != "nt":
            raise MT5PackageUnavailable(
                "Read-only MT5 market data requires Windows and a local MT5 terminal"
            )
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise MT5PackageUnavailable(
                "MetaTrader5 Python package is not installed in this AXIOM build"
            ) from exc
        # Do not retain the full module: even private ``_mt5.order_send``
        # traversal would contradict this interface's market-data-only scope.
        # Only these bound read-only functions and timeframe constants remain.
        self._api = {
            "initialize": mt5.initialize,
            "shutdown": mt5.shutdown,
            "last_error": mt5.last_error,
            "account_info": mt5.account_info,
            "terminal_info": mt5.terminal_info,
            "symbols_get": mt5.symbols_get,
            "symbol_info": mt5.symbol_info,
            "symbol_select": mt5.symbol_select,
            "symbol_info_tick": mt5.symbol_info_tick,
            "copy_rates_from_pos": mt5.copy_rates_from_pos,
        }
        self._constants = {
            name: getattr(mt5, name, None) for name in _TIMEFRAME_CONSTANTS
        }

    def constant(self, name: str, default=None):
        # Do not expose arbitrary trade constants through this data interface.
        if name not in _TIMEFRAME_CONSTANTS:
            raise ValueError("This read-only gateway exposes timeframe constants only")
        return self._constants.get(name, default)

    def initialize(
        self, *, login: int, password: str, server: str,
        terminal_path: str = "", timeout_ms: int = 15_000,
    ) -> bool:
        kwargs = {
            "login": int(login), "password": password, "server": server,
            "timeout": int(timeout_ms), "portable": False,
        }
        if terminal_path:
            return bool(self._api["initialize"](terminal_path, **kwargs))
        return bool(self._api["initialize"](**kwargs))

    def shutdown(self) -> None:
        self._api["shutdown"]()

    def last_error(self) -> tuple:
        try:
            return tuple(self._api["last_error"]())
        except Exception:
            return (-1, "MT5 error unavailable")

    def account_info(self) -> dict:
        return _as_dict(self._api["account_info"]())

    def terminal_info(self) -> dict:
        return _as_dict(self._api["terminal_info"]())

    def symbols_get(self) -> list[dict] | None:
        rows = self._api["symbols_get"]()
        return [_as_dict(row) for row in rows] if rows is not None else None

    def symbol_info(self, symbol: str) -> dict:
        return _as_dict(self._api["symbol_info"](symbol))

    def symbol_select(self, symbol: str, enabled: bool = True) -> bool:
        return bool(self._api["symbol_select"](symbol, enabled))

    def symbol_info_tick(self, symbol: str) -> dict:
        return _as_dict(self._api["symbol_info_tick"](symbol))

    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start: int, count: int,
    ) -> list[dict] | None:
        rows = self._api["copy_rates_from_pos"](symbol, timeframe, start, count)
        if rows is None:
            return None
        names = getattr(getattr(rows, "dtype", None), "names", None)
        result = []
        for row in rows:
            if names:
                result.append({
                    name: row[name].item() if hasattr(row[name], "item") else row[name]
                    for name in names
                })
            elif hasattr(row, "_asdict"):
                result.append(_as_dict(row))
            else:
                result.append(dict(row))
        return result
