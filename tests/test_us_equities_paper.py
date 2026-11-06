from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.data.alpaca import AlpacaHistoricalProvider, AlpacaStream
from app.data.models import Candle
from app.config.settings import Settings
from app.markets.us_calendar import USEquitiesCalendar, holidays
from app.paper_trading.execution import calendar_signature, make_calendar
from app.backtesting.costs import ExecutionConfig
from app.paper_trading.broker import PaperBroker
from app.strategies.base import Signal


def test_us_calendar_dst_regular_hours_and_early_close():
    calendar = USEquitiesCalendar()
    # 9:30 New York is 14:30 UTC in winter and 13:30 UTC in summer.
    assert calendar.is_open(datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc))
    assert calendar.is_open(datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc))
    assert not calendar.is_open(datetime(2026, 1, 5, 14, 29, tzinfo=timezone.utc))
    # 2026 Thanksgiving Friday closes at 13:00 New York (18:00 UTC).
    assert calendar.is_open(datetime(2026, 11, 27, 17, 59, tzinfo=timezone.utc))
    assert not calendar.is_open(datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc))
    assert not calendar.is_open(datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc))
    assert calendar.session_bounds(date(2026, 11, 27))[1].hour == 13
    assert make_calendar("us-equities", "1m").signature() == calendar_signature(calendar)
    new_york = ZoneInfo("America/New_York")
    early_close_status = calendar.market_state(
        datetime(2026, 11, 27, 12, 45, tzinfo=new_york))
    assert early_close_status["status"] == "OPEN"
    assert early_close_status["early_close"] is True
    assert early_close_status["next_transition"].endswith("13:00:00-05:00")
    # A holiday has no session at all, and the calendar says when trading resumes.
    holiday_status = calendar.market_state(datetime(2026, 11, 26, 12, 0, tzinfo=new_york))
    assert holiday_status["status"] == "CLOSED"
    assert holiday_status["next_transition"].endswith("2026-11-27T09:30:00-05:00")


def test_us_calendar_good_friday_and_juneteenth():
    assert date(2026, 4, 3) in holidays(2026)
    assert date(2026, 6, 19) in holidays(2026)


def test_alpaca_history_paginates_and_honors_exclusive_end():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.params.get("page_token") == "next":
            bars = [{"t": "2026-10-21T14:31:00Z", "o": 11, "h": 12,
                     "l": 10, "c": 11, "v": 50}]
            token = None
        else:
            bars = [{"t": "2026-10-21T14:30:00Z", "o": 10, "h": 11,
                     "l": 9, "c": 10, "v": 40},
                    {"t": "2026-10-21T14:32:00Z", "o": 12, "h": 13,
                     "l": 11, "c": 12, "v": 30}]
            token = "next"
        return httpx.Response(200, json={"bars": {"AAPL": bars}, "next_page_token": token})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AlpacaHistoricalProvider("key", "secret", client=client)
    bars = provider.historical("aapl", "1m", datetime(2026, 10, 21, 14, 30, tzinfo=timezone.utc),
                               datetime(2026, 10, 21, 14, 32, tzinfo=timezone.utc))
    assert [bar.close for bar in bars] == [10, 11]
    assert len(calls) == 2
    assert calls[0].headers["APCA-API-KEY-ID"] == "key"
    assert calls[0].url.params["feed"] == "iex"


def test_alpaca_history_requires_credentials_and_aware_range():
    with pytest.raises(ValueError, match="API_KEY"):
        AlpacaHistoricalProvider("", "secret")
    provider = AlpacaHistoricalProvider("key", "secret", client=httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"bars": {}}))))
    with pytest.raises(ValueError, match="timezone"):
        provider.historical("AAPL", "1m", datetime(2026, 1, 1),
                            datetime(2026, 1, 2, tzinfo=timezone.utc))


def test_alpaca_stream_decodes_bars_and_ignores_corrections():
    event = {"T": "b", "S": "AAPL", "t": "2026-10-21T14:30:00Z",
             "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 3}
    candle = AlpacaStream.decode(event)
    assert candle.symbol == "AAPL" and candle.close == 10.5
    assert AlpacaStream.decode({**event, "T": "u"}) is None
    # A vendor-style instrument key is not a plain US ticker, even though it
    # passes the generic symbol validator.
    with pytest.raises(ValueError, match="ticker"):
        AlpacaStream.decode({**event, "S": "NASDAQ:AAPL"})
    with pytest.raises(ValueError, match="ticker"):
        AlpacaStream.decode({**event, "S": "AAPL_X"})


def test_alpaca_stream_is_read_only_and_1m_only():
    stream = AlpacaStream("key", "secret", ["AAPL"], feed="iex")
    assert stream.supported_timeframes == frozenset({"1m"})
    assert "order" not in " ".join(dir(stream)).lower()
    with pytest.raises(ValueError, match="does not support"):
        stream.check_timeframe("5m")


def test_paper_risk_caps_bound_position_and_open_position_count():
    broker = PaperBroker(ExecutionConfig(initial_capital=10_000, position_size=0.5,
                                         ), currency="USD", timeframe="1m",
                         calendar=USEquitiesCalendar(),
                         risk_limits={"max_position_value": 1_000, "max_open_positions": 1})
    broker.portfolio.marks.update({"AAPL": 100, "MSFT": 100})
    timestamp = "2026-10-21T14:31:00+00:00"
    broker.handle_signal(Signal.BUY, "AAPL", timestamp, "test", persist=False)
    entry = next(iter(broker.orders.values()))
    assert entry.quantity * broker.config.fill_price(100, "BUY") <= 1_000
    broker.handle_signal(Signal.BUY, "MSFT", timestamp, "test", persist=False)
    assert len(broker.orders) == 1


def test_paper_daily_loss_latches_entry_stop_for_session():
    broker = PaperBroker(ExecutionConfig(initial_capital=10_000), currency="USD",
                         timeframe="1m", calendar=USEquitiesCalendar(),
                         risk_limits={"max_daily_loss": 100})
    broker.portfolio.marks["AAPL"] = 100
    broker.portfolio.cash = 9_800
    timestamp = "2026-10-21T14:31:00+00:00"
    broker.portfolio.snapshot(timestamp, "USD")
    broker.handle_signal(Signal.BUY, "AAPL", timestamp, "test", persist=False)
    assert not broker.orders
    assert broker.context["paper_risk_halts"]["daily_loss_date"] == "2026-10-21"


def test_paper_position_cap_still_holds_after_an_adverse_open_gap():
    broker = PaperBroker(ExecutionConfig(initial_capital=10_000, position_size=0.5,
                                         max_participation=1), currency="USD", timeframe="1m",
                         calendar=USEquitiesCalendar(),
                         risk_limits={"max_position_value": 1_000})
    broker.portfolio.marks["AAPL"] = 100
    broker.handle_signal(Signal.BUY, "AAPL", "2026-10-21T14:30:00+00:00", "test", persist=False)
    order = next(iter(broker.orders.values()))
    broker.on_bar(Candle(datetime(2026, 10, 21, 14, 31, tzinfo=timezone.utc),
                         200, 201, 199, 200, 1_000, "AAPL"), persist=False)
    assert order.status == "PARTIALLY_FILLED"
    assert order.filled_quantity * order.average_price <= 1_000.01


def test_alpaca_settings_require_local_paper_and_five_bounded_limits():
    required = dict(data_source="alpaca", market="us", currency="USD",
                    market_calendar="us-equities", default_timeframe="1m",
                    alpaca_api_key="key", alpaca_api_secret="secret",
                    paper_max_position_value=1_000, paper_max_open_positions=2,
                    paper_max_daily_loss=100, paper_max_drawdown=0.05,
                    paper_max_trades_per_day=4)
    settings = Settings(**required)
    assert settings.mode == "PAPER" and settings.broker == "LOCAL_PAPER"
    assert settings.execution_kwargs().keys() == {
        "initial_capital", "commission", "slippage", "spread", "position_size",
        "stop_loss", "take_profit", "max_participation",
    }
    assert settings.paper_risk_limits()["max_drawdown"] == 0.05
    with pytest.raises(ValueError, match="PAPER_MAX_POSITION_VALUE"):
        Settings(**{**required, "paper_max_position_value": 0})
    with pytest.raises(ValueError, match="local PAPER"):
        Settings(**{**required, "broker": "UPSTOX_SANDBOX",
                    "upstox_sandbox_access_token": "token"})
