from dataclasses import replace
import pytest
from app.backtesting.orders import Order
from app.paper_trading.broker import PaperBroker, FutureRealBroker
from app.paper_trading.execution import SessionCalendar
from app.strategies.base import Signal
from tests.conftest import candle


def submit(broker, side="BUY", quantity=5, at=0, **kwargs):
    return broker.place_order(
        Order("DEMO", side, quantity, candle(at).timestamp.isoformat(), **kwargs)
    )


def test_cash_fees_slippage_roundtrip(zero_cost):
    config = replace(zero_cost, commission=0.01, slippage=0.01, spread=0.02)
    broker = PaperBroker(config)
    submit(broker)
    broker.on_bar(candle())
    entry = 102
    assert broker.portfolio.cash == pytest.approx(1000 - 5 * entry * 1.01)
    assert broker.portfolio.fees == pytest.approx(5 * entry * 0.01)
    submit(broker, "SELL", at=1)
    broker.on_bar(candle(1, 110))
    exit_price = 110 * 0.98
    expected = 5 * (exit_price - entry) - 5 * (entry + exit_price) * 0.01
    assert broker.portfolio.realized_pnl == pytest.approx(expected)
    assert broker.portfolio.equity == pytest.approx(1000 + expected)
    assert broker.portfolio.trades[0]["pnl"] == pytest.approx(expected)
    assert not broker.get_positions()


def test_position_sizing(zero_cost):
    broker = PaperBroker(replace(zero_cost, position_size=0.25))
    broker.on_bar(candle())
    broker.handle_signal(Signal.BUY, "DEMO", candle(1).timestamp.isoformat(), "s")
    assert broker.get_orders()[0]["quantity"] == pytest.approx(2.5)
    broker.on_bar(candle(1))
    broker.handle_signal(Signal.BUY, "DEMO", candle(2).timestamp.isoformat(), "s")
    assert len(broker.get_orders()) == 1  # No pyramiding.


def test_liquidity_partial_and_cancellation(zero_cost):
    broker = PaperBroker(replace(zero_cost, max_participation=0.1))
    order = submit(broker, quantity=5)
    broker.on_bar(candle(volume=10))
    assert broker.get_order(order["id"])["filled_quantity"] == 1
    assert broker.get_order(order["id"])["status"] == "PARTIALLY_FILLED"
    broker.cancel_order(order["id"])
    broker.on_bar(candle(1))
    assert broker.get_positions()[0]["quantity"] == 1
    with pytest.raises(ValueError):
        broker.cancel_order(order["id"])


def test_insufficient_cash_and_no_short(zero_cost):
    broker = PaperBroker(zero_cost)
    rejected = submit(broker, "SELL")
    assert rejected["status"] == "REJECTED"
    order = submit(broker, quantity=20)
    broker.on_bar(candle())
    assert broker.get_positions()[0]["quantity"] == pytest.approx(10)
    assert broker.get_balance()["cash"] >= -1e-8
    assert broker.get_order(order["id"])["status"] == "CANCELLED"
    no_cash = submit(broker, quantity=1, at=1)
    broker.on_bar(candle(1))
    assert broker.get_order(no_cash["id"])["reason"] == "insufficient_cash"


def test_limit_and_no_early_fill(zero_cost):
    broker = PaperBroker(zero_cost)
    order = submit(broker, at=1, order_type="LIMIT", limit_price=95)
    broker.on_bar(candle(0, 100, low=90))
    assert broker.get_order(order["id"])["status"] == "NEW"
    broker.on_bar(candle(1, 100, low=96))
    assert broker.get_order(order["id"])["status"] == "NEW"
    broker.on_bar(candle(2, 100, low=94))
    assert broker.get_order(order["id"])["average_price"] == 95


def test_stop_worst_case_and_gap(zero_cost):
    broker = PaperBroker(replace(zero_cost, stop_loss=0.05, take_profit=0.10))
    submit(broker)
    broker.on_bar(candle(0, 100))
    broker.on_bar(candle(1, 100, high=120, low=90))
    assert broker.portfolio.trades[-1]["exit"] == 95
    assert broker.portfolio.trades[-1]["reason"] == "stop_loss"
    other = PaperBroker(replace(zero_cost, stop_loss=0.05))
    submit(other)
    other.on_bar(candle())
    other.on_bar(candle(1, 80))
    assert other.portfolio.trades[-1]["exit"] == 80


def test_stop_partial_respects_volume(zero_cost):
    broker = PaperBroker(replace(zero_cost, stop_loss=0.05, max_participation=0.1))
    submit(broker)
    broker.on_bar(candle())
    broker.on_bar(candle(1, 90, volume=10))
    assert broker.get_positions()[0]["quantity"] == 4
    broker.on_bar(candle(2, 89))
    assert not broker.get_positions()
    assert sum(f.quantity for f in broker.fills if f.timestamp.startswith("2024-01-02")) <= 1


def test_take_profit_not_on_entry_bar(zero_cost):
    broker = PaperBroker(replace(zero_cost, take_profit=0.1))
    submit(broker)
    broker.on_bar(candle(0, 100, high=120))
    assert broker.get_positions()
    broker.on_bar(candle(1, 100, high=120))
    assert broker.portfolio.trades[-1]["exit"] == pytest.approx(110)


def test_duplicate_bar_and_market_closed(zero_cost):
    broker = PaperBroker(zero_cost)
    submit(broker)
    broker.on_bar(candle())
    assert broker.on_bar(candle()) is None
    assert len(broker.fills) == 1
    broker = PaperBroker(zero_cost, calendar=SessionCalendar())
    order = submit(broker)
    broker.on_bar(candle())  # UTC midnight is outside NSE intraday session.
    assert broker.get_order(order["id"])["status"] == "NEW"


def test_account_reconciliation_partial_sales(zero_cost):
    broker = PaperBroker(replace(zero_cost, commission=0.005))
    submit(broker, quantity=4)
    broker.on_bar(candle())
    submit(broker, quantity=2, at=1)
    broker.on_bar(candle(1, 110))
    submit(broker, "SELL", quantity=3, at=2)
    broker.on_bar(candle(2, 120))
    account = broker.get_balance()
    assert account["equity"] - 1000 == pytest.approx(
        account["realized_pnl"] + account["unrealized_pnl"]
    )
    assert account["daily_pnl"] == pytest.approx(
        account["equity"] - broker.portfolio.day_start_equity
    )


def test_bad_order_and_real_broker(zero_cost):
    with pytest.raises((TypeError, RuntimeError)):
        FutureRealBroker()
    for quantity in [-1, 0, float("nan")]:
        with pytest.raises(ValueError):
            Order("DEMO", "BUY", quantity, candle().timestamp.isoformat())
    broker = PaperBroker(zero_cost)
    order = Order("DEMO", "BUY", 1, candle().timestamp.isoformat())
    broker.place_order(order)
    order.quantity = 999999
    assert broker.get_order(order.id)["quantity"] == 1
    with pytest.raises(ValueError, match="Duplicate"):
        broker.place_order(order)
