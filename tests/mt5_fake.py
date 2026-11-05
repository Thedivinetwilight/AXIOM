"""In-memory MT5 terminal fake. It never imports or contacts MetaTrader5."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone


CONSTANTS = {
    "ORDER_TYPE_BUY": 0, "ORDER_TYPE_SELL": 1,
    "ORDER_TYPE_BUY_LIMIT": 2, "ORDER_TYPE_SELL_LIMIT": 3,
    "ORDER_TYPE_FILLING_FOK": 0, "ORDER_TYPE_FILLING_IOC": 1,
    "ORDER_TYPE_FILLING_RETURN": 2,
    "TRADE_ACTION_DEAL": 1, "TRADE_ACTION_PENDING": 5,
    "TRADE_ACTION_SLTP": 6, "TRADE_ACTION_MODIFY": 7, "TRADE_ACTION_REMOVE": 8,
    "ORDER_TIME_GTC": 0,
    "TIMEFRAME_M1": 1, "TIMEFRAME_M5": 5, "TIMEFRAME_M15": 15,
    "TIMEFRAME_M30": 30, "TIMEFRAME_H1": 16385, "TIMEFRAME_H4": 16388,
    "TIMEFRAME_D1": 16408,
}


def symbol_row(name="EURUSD", path="Forex\\Major", description="Euro vs US Dollar"):
    return {
        "name": name, "path": path, "description": description,
        "currency_base": name[:3] if len(name) >= 6 else name,
        "currency_profit": "USD", "volume_min": 0.01, "volume_max": 100.0,
        "volume_step": 0.01, "volume_limit": 0.0, "trade_contract_size": 100000.0,
        "trade_tick_size": 0.0001, "trade_tick_value": 1.0, "digits": 4,
        "point": 0.0001, "trade_mode": 4, "trade_exemode": 2,
        "filling_mode": 2, "order_mode": 127, "trade_stops_level": 10,
        "trade_freeze_level": 0, "margin_initial": 0.0, "margin_maintenance": 0.0,
        "swap_long": -0.1, "swap_short": -0.2, "swap_mode": 1,
        "swap_rollover3days": 3, "visible": True,
    }


class FakeMT5Gateway:
    def __init__(self, *, live=False):
        self.live = live
        self.login_value = 123456
        self.server = "VantageMarkets-Live 13" if live else "VantageMarkets-Demo"
        self.initialized = False
        self.connected = False
        self.trade_allowed = True
        self.terminal_trade_allowed = True
        self.balance = 10_000.0
        self.equity = 10_000.0
        self.margin_free = 9_000.0
        self.margin = 0.0
        self.behavior = "success"
        self.send_calls = []
        self.check_calls = []
        self.cancel_calls = []
        self.tick_age_seconds = 0
        self.tick_future_seconds = 0
        self.out_of_order_tick = False
        self._last_tick_msc = 0
        self._next_ticket = 1000
        self.symbols = [
            symbol_row(),
            symbol_row("AAPL", "CFD\\Shares\\US", "Apple share CFD"),
            symbol_row("MYSTERY", "Custom\\Unclassified", "Unclassified product"),
        ]
        self.positions = []
        self.orders = []
        self.history_orders = []
        self.deals = []
        self.fail_snapshot = False

    def constant(self, name, default=None):
        return CONSTANTS.get(name, default)

    def initialize(self, **kwargs):
        self.initialized = True
        self.connected = True
        self.initialize_kwargs = dict(kwargs)
        if self.behavior == "auth_fail":
            self.connected = False
            return False
        return True

    def shutdown(self):
        self.connected = False
        self.initialized = False

    def last_error(self):
        return (-1, "fake terminal error")

    def account_info(self):
        if not self.connected:
            return {}
        return {
            "login": self.login_value, "server": self.server,
            "currency": "USD", "balance": self.balance, "equity": self.equity,
            "margin": self.margin, "margin_free": self.margin_free,
            "leverage": 100, "trade_allowed": self.trade_allowed,
            "trade_expert": True, "trade_mode": 2 if self.live else 0,
        }

    def terminal_info(self):
        return {"connected": self.connected, "trade_allowed": self.terminal_trade_allowed}

    def symbols_get(self):
        return deepcopy(self.symbols) if self.connected else None

    def symbol_info(self, symbol):
        for row in self.symbols:
            if row["name"] == symbol:
                return deepcopy(row)
        return {}

    def symbol_info_tick(self, symbol):
        if not self.connected:
            return {}
        now = datetime.now(timezone.utc) - timedelta(seconds=self.tick_age_seconds) + timedelta(seconds=self.tick_future_seconds)
        msc = int(now.timestamp() * 1000)
        if self.out_of_order_tick and self._last_tick_msc:
            msc = self._last_tick_msc - 1000
        self._last_tick_msc = max(self._last_tick_msc, msc)
        return {"time": msc // 1000, "time_msc": msc, "bid": 1.0998, "ask": 1.1002, "last": 1.1}

    def symbol_select(self, symbol, enabled=True):
        return any(row["name"] == symbol for row in self.symbols)

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        if not self.connected:
            return None
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        rows = []
        for index in range(count):
            stamp = now - timedelta(minutes=count - index)
            price = 1.1 + index * 0.0001
            rows.append({
                "time": int(stamp.timestamp()), "open": price, "high": price + 0.0002,
                "low": price - 0.0002, "close": price + 0.0001,
                "tick_volume": 100 + index, "spread": 4,
            })
        return rows

    def order_calc_margin(self, order_type, symbol, volume, price):
        return round(volume * 100.0, 2)

    def order_check(self, request):
        self.check_calls.append(deepcopy(request))
        if self.behavior == "precheck_reject":
            return {"retcode": 10019, "comment": "not enough money"}
        return {"retcode": 0, "comment": "checks passed", "order": 0, "deal": 0}

    def _ticket(self):
        self._next_ticket += 1
        return self._next_ticket

    def _order_row(self, request, ticket, state=1):
        return {
            "ticket": ticket, "symbol": request["symbol"], "type": request["type"],
            "volume_initial": request["volume"], "volume_current": request["volume"],
            "price_open": request.get("price", 1.1002), "sl": request.get("sl", 0),
            "tp": request.get("tp", 0), "state": state,
            "time_setup": int(datetime.now(timezone.utc).timestamp()),
            "time_done": int(datetime.now(timezone.utc).timestamp()),
            "magic": request.get("magic", 0), "comment": request.get("comment", ""),
        }

    def _deal_row(self, request, order_ticket, volume, *, close=False):
        deal_ticket = self._ticket()
        position_id = request.get("position") or order_ticket
        deal = {
            "ticket": deal_ticket, "order": order_ticket, "position_id": position_id,
            "symbol": request["symbol"], "type": request["type"] if request["type"] in {0, 1} else (0 if request["type"] == 2 else 1),
            "entry": 1 if close else 0, "volume": volume, "price": request.get("price", 1.1002),
            "profit": 0.0, "commission": -0.01, "swap": 0.0, "fee": 0.0,
            "time": int(datetime.now(timezone.utc).timestamp()),
            "time_msc": int(datetime.now(timezone.utc).timestamp() * 1000),
            "magic": request.get("magic", 0), "comment": request.get("comment", ""),
        }
        self.deals.append(deal)
        return deal

    def _accept(self, request):
        order_ticket = self._ticket()
        partial = self.behavior == "partial"
        fill_volume = request["volume"] / 2 if partial else request["volume"]
        if "position" in request:
            position = next(item for item in self.positions if item["ticket"] == request["position"])
            position["volume"] -= fill_volume
            self._deal_row(request, order_ticket, fill_volume, close=True)
            if position["volume"] <= 1e-9:
                self.positions.remove(position)
            history = self._order_row(request, order_ticket, 4)
            history["volume_current"] = 0
            self.history_orders.append(history)
            return {"retcode": 10009, "order": order_ticket, "deal": self.deals[-1]["ticket"], "volume": fill_volume, "price": 1.1002, "comment": "done"}
        if request["action"] == CONSTANTS["TRADE_ACTION_PENDING"]:
            row = self._order_row(request, order_ticket, 1)
            self.orders.append(row)
            return {"retcode": 10008, "order": order_ticket, "deal": 0, "volume": 0, "price": request.get("price"), "comment": "placed"}
        self._deal_row(request, order_ticket, fill_volume)
        if partial:
            row = self._order_row(request, order_ticket, 3)
            row["volume_current"] = request["volume"] - fill_volume
            self.orders.append(row)
        else:
            history = self._order_row(request, order_ticket, 4)
            history["volume_current"] = 0
            self.history_orders.append(history)
        position = next((item for item in self.positions if item["symbol"] == request["symbol"] and item["type"] == request["type"]), None)
        if position:
            position["volume"] += fill_volume
        else:
            self.positions.append({
                "ticket": order_ticket, "identifier": order_ticket,
                "symbol": request["symbol"], "type": request["type"],
                "volume": fill_volume, "price_open": 1.1002, "price_current": 1.1,
                "sl": request.get("sl", 0), "tp": request.get("tp", 0),
                "profit": 0.0, "swap": 0.0,
                "time": int(datetime.now(timezone.utc).timestamp()),
                "magic": request.get("magic", 0), "comment": request.get("comment", ""),
            })
        return {
            "retcode": 10010 if partial else 10009, "order": order_ticket,
            "deal": self.deals[-1]["ticket"], "volume": fill_volume,
            "price": 1.1002, "comment": "partial" if partial else "done",
        }

    def _modify_position(self, request):
        position = next(
            (item for item in self.positions if item["ticket"] == request["position"]), None,
        )
        if position is None:
            return {"retcode": 10006, "comment": "position absent"}
        position["sl"] = request.get("sl", 0.0)
        position["tp"] = request.get("tp", 0.0)
        return {"retcode": 10009, "comment": "position protection modified"}

    def order_send(self, request):
        self.send_calls.append(deepcopy(request))
        if self.behavior == "timeout":
            raise TimeoutError("simulated timeout")
        if self.behavior == "disconnect":
            self.connected = False
            raise ConnectionError("simulated disconnect")
        if self.behavior == "rejection":
            return {"retcode": 10006, "order": 0, "deal": 0, "comment": "rejected"}
        if request["action"] == CONSTANTS["TRADE_ACTION_SLTP"]:
            if self.behavior == "timeout_after_accept":
                result = self._modify_position(request)
                self.behavior = "success"
                raise TimeoutError("simulated lost modification response")
            return self._modify_position(request)
        if request["action"] == CONSTANTS["TRADE_ACTION_REMOVE"]:
            self.cancel_calls.append(request["order"])
            row = next((item for item in self.orders if item["ticket"] == request["order"]), None)
            if row is None:
                return {"retcode": 10006, "comment": "order absent"}
            self.orders.remove(row)
            row["state"] = 2
            row["volume_current"] = 0
            self.history_orders.append(row)
            return {"retcode": 10009, "order": request["order"], "comment": "cancelled"}
        if self.behavior == "timeout_after_accept":
            result = self._accept(request)
            self.behavior = "success"
            raise TimeoutError("simulated lost response")
        return self._accept(request)

    def positions_get(self):
        return deepcopy(self.positions) if self.connected and not self.fail_snapshot else None

    def orders_get(self):
        return deepcopy(self.orders) if self.connected and not self.fail_snapshot else None

    def history_orders_get(self, start, end):
        return deepcopy(self.history_orders) if self.connected and not self.fail_snapshot else None

    def history_deals_get(self, start, end):
        return deepcopy(self.deals) if self.connected and not self.fail_snapshot else None
