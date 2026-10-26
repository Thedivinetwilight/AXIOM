"""Read-only in-memory MT5 market-data fake. No terminal, account or network."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import time


CONSTANTS = {
    "TIMEFRAME_M1": 1, "TIMEFRAME_M5": 5, "TIMEFRAME_M15": 15,
    "TIMEFRAME_M30": 30, "TIMEFRAME_H1": 16385, "TIMEFRAME_H4": 16388,
    "TIMEFRAME_D1": 16408,
}
SECONDS = {1: 60, 5: 300, 15: 900, 30: 1800, 16385: 3600, 16388: 14400, 16408: 86400}


def symbol_row(name="EURUSD", path="Forex\\Major", description="Euro vs US Dollar"):
    return {
        "name": name, "path": path, "description": description,
        "currency_base": name[:3], "currency_profit": "USD",
        "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
        "volume_limit": 0.0, "trade_contract_size": 100000.0,
        "trade_tick_size": 0.0001, "trade_tick_value": 1.0,
        "digits": 4, "point": 0.0001, "trade_mode": 4,
        "trade_exemode": 2, "filling_mode": 2, "order_mode": 127,
        "trade_stops_level": 10, "trade_freeze_level": 0,
        "margin_initial": 0.0, "margin_maintenance": 0.0,
        "swap_long": -0.1, "swap_short": -0.2, "swap_mode": 1,
        "swap_rollover3days": 3, "visible": True,
    }


class FakeReadOnlyMT5Gateway:
    """No order/position/history/margin methods exist on this fake."""

    def __init__(self):
        self.login = 998877
        self.server = "VantageMarkets-Demo-UnitTest"
        self.trade_mode = 0
        self.connected = False
        self.initialized = False
        self.auth_ok = True
        self.symbols = [
            symbol_row(),
            symbol_row("AAPL", "CFD\\Shares\\US", "Apple share CFD"),
            symbol_row("aBc.Def", "Forex\\Custom", "Case-sensitive test symbol"),
            symbol_row("MYSTERY", "Custom\\Unclassified", "Unclassified product"),
        ]
        self.tick_behavior = "success"
        self.bar_behavior = "success"
        self.subscription_ok = True
        self._tick_ms = int((time.time() - 1) * 1000)
        self._last_emitted_tick = 0
        self._bar_offset = 0
        self.copy_calls = []
        self.shutdown_calls = 0
        self.initialize_kwargs = None

    def constant(self, name, default=None):
        if name not in CONSTANTS:
            raise ValueError("read-only fake exposes timeframe constants only")
        return CONSTANTS.get(name, default)

    def initialize(self, **kwargs):
        self.initialize_kwargs = dict(kwargs)
        self.initialized = True
        self.connected = self.auth_ok
        return self.auth_ok

    def shutdown(self):
        self.shutdown_calls += 1
        self.connected = False
        self.initialized = False

    def last_error(self):
        return (-1, "safe fake error")

    def account_info(self):
        if not self.connected:
            return {}
        return {
            "login": self.login, "server": self.server, "currency": "USD",
            "trade_mode": self.trade_mode,
        }

    def terminal_info(self):
        return {"connected": self.connected}

    def symbols_get(self):
        return deepcopy(self.symbols) if self.connected else None

    def symbol_info(self, symbol):
        return next((deepcopy(row) for row in self.symbols if row["name"] == symbol), {})

    def symbol_select(self, symbol, enabled=True):
        return self.connected and self.subscription_ok and any(row["name"] == symbol for row in self.symbols)

    def symbol_info_tick(self, symbol):
        if not self.connected or self.tick_behavior == "missing":
            return {}
        now = datetime.now(timezone.utc)
        if self.tick_behavior == "stale":
            stamp = now - timedelta(seconds=120)
            ms = int(stamp.timestamp() * 1000)
        elif self.tick_behavior == "future":
            stamp = now + timedelta(seconds=60)
            ms = int(stamp.timestamp() * 1000)
        elif self.tick_behavior == "same":
            ms = self._tick_ms
        elif self.tick_behavior == "out_of_order" and self._last_emitted_tick:
            ms = self._last_emitted_tick - 1000
        else:
            self._tick_ms += 100
            ms = self._tick_ms
        self._last_emitted_tick = max(self._last_emitted_tick, ms)
        if self.tick_behavior == "invalid":
            return {"time_msc": ms, "bid": 0.0, "ask": 1.1, "last": 1.1}
        if self.tick_behavior == "crossed":
            return {"time_msc": ms, "bid": 1.2, "ask": 1.1, "last": 1.1}
        return {"time": ms // 1000, "time_msc": ms, "bid": 1.0998, "ask": 1.1002, "last": 1.1}

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        self.copy_calls.append({"symbol": symbol, "timeframe": timeframe, "start": start, "count": count})
        if not self.connected or self.bar_behavior == "none":
            return None
        if self.bar_behavior == "empty":
            return []
        seconds = SECONDS[timeframe]
        now = datetime.now(timezone.utc)
        latest = now - timedelta(seconds=seconds + self._bar_offset * seconds)
        rows = []
        for index in range(count):
            stamp = latest - timedelta(seconds=(count - index - 1) * seconds)
            price = 1.1 + index * 0.0001
            rows.append({
                "time": int(stamp.timestamp()), "open": price,
                "high": price + 0.0002, "low": price - 0.0002,
                "close": price + 0.0001, "tick_volume": 100 + index,
            })
        if self.bar_behavior == "invalid":
            rows[-1]["high"] = rows[-1]["low"] - 1
        elif self.bar_behavior == "unsorted" and len(rows) > 1:
            rows[0], rows[-1] = rows[-1], rows[0]
        elif self.bar_behavior == "duplicate" and len(rows) > 1:
            rows[-1] = dict(rows[0])
        elif self.bar_behavior == "future":
            rows[-1]["time"] = int((now + timedelta(seconds=seconds * 2)).timestamp())
        elif self.bar_behavior == "nan":
            rows[-1]["close"] = float("nan")
        return rows

    def advance_bar(self):
        self._bar_offset -= 1
