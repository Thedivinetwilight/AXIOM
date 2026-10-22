"""US equities paper-session contract: calendar, data hygiene, risk, safety.

These tests exercise the parts a live US paper session depends on, using mocks
for every network boundary. Nothing here contacts Alpaca, and no test asserts
that a live Alpaca connection works — that can only be checked with real
market-data credentials outside this suite.
"""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.backtesting.costs import ExecutionConfig
from app.config.settings import Settings
from app.data.alpaca import AlpacaHistoricalProvider, AlpacaStream, validate_us_symbol
from app.data.models import Candle
from app.markets.registry import market_session_state
from app.markets.us_calendar import USEquitiesCalendar, early_close, holidays
from app.paper_trading.broker import PaperBroker, PaperRiskLimits
from app.paper_trading.execution import calendar_signature, make_calendar
from app.paper_trading.simulator import BotState, PaperSimulator
from app.strategies.base import Signal

NY = ZoneInfo("America/New_York")
UTC = timezone.utc


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --------------------------------------------------------------------- calendar


def test_us_calendar_handles_dst_without_fixed_offsets():
    calendar = USEquitiesCalendar()
    # 09:30 New York is 14:30 UTC in winter and 13:30 UTC in summer.
    assert calendar.is_open(utc(2026, 1, 5, 14, 30))
    assert calendar.is_open(utc(2026, 7, 6, 13, 30))
    assert not calendar.is_open(utc(2026, 1, 5, 14, 29))
    assert not calendar.is_open(utc(2026, 7, 6, 13, 29))
    # The same wall-clock time is inside the session on one date and outside on
    # the other, which is exactly what a fixed UTC offset would get wrong.
    assert calendar.is_open(utc(2026, 1, 5, 13, 45)) is False
    assert calendar.is_open(utc(2026, 7, 6, 13, 45)) is True
    with pytest.raises(ValueError, match="timezone-aware"):
        calendar.is_open(datetime(2026, 1, 5, 14, 30))


def test_us_calendar_holidays_follow_the_published_nyse_table():
    assert date(2026, 1, 1) in holidays(2026)
    assert date(2026, 1, 19) in holidays(2026)   # MLK
    assert date(2026, 2, 16) in holidays(2026)   # Washington's Birthday
    assert date(2026, 4, 3) in holidays(2026)    # Good Friday
    assert date(2026, 5, 25) in holidays(2026)   # Memorial Day
    assert date(2026, 6, 19) in holidays(2026)   # Juneteenth
    assert date(2026, 7, 3) in holidays(2026)    # Independence Day observed
    assert date(2026, 9, 7) in holidays(2026)    # Labor Day
    assert date(2026, 11, 26) in holidays(2026)  # Thanksgiving
    assert date(2026, 12, 25) in holidays(2026)
    # 2027: July 4 is a Sunday (observed Monday), Christmas is observed Friday.
    assert date(2027, 7, 5) in holidays(2027)
    assert date(2027, 12, 24) in holidays(2027)
    assert date(2027, 6, 18) in holidays(2027)   # Juneteenth observed (Sat -> Fri)
    # 2028: January 1 is a Saturday and NYSE observes NO New Year holiday, so
    # 2027-12-31 is a normal trading day.
    assert date(2028, 1, 1) not in holidays(2028)
    assert date(2027, 12, 31) not in holidays(2027)
    assert USEquitiesCalendar().is_trading_day(date(2027, 12, 31))
    # 2028 July 3 is the published early close.
    assert date(2028, 7, 3) in holidays(2028) or early_close(date(2028, 7, 3))


def test_us_calendar_early_closes_match_the_published_list():
    assert early_close(date(2026, 11, 27))          # day after Thanksgiving
    assert early_close(date(2026, 12, 24))          # Christmas Eve (Fri holiday)
    # 2026 has NO July half day: July 3 is the full holiday and July 2 is normal.
    assert not early_close(date(2026, 7, 2))
    assert not early_close(date(2026, 7, 3))
    # 2027 has only the Thanksgiving half day.
    assert early_close(date(2027, 11, 26))
    assert not early_close(date(2027, 7, 2))
    assert not early_close(date(2027, 12, 23))
    # 2028 has July 3 plus the Thanksgiving half day.
    assert early_close(date(2028, 7, 3))
    assert early_close(date(2028, 11, 24))
    # A weekend is never a half day.
    assert not early_close(date(2026, 11, 28))
    calendar = USEquitiesCalendar()
    assert calendar.close_time(date(2026, 11, 27)) == time(13, 0)
    assert calendar.close_time(date(2026, 11, 30)) == time(16, 0)


def test_us_calendar_session_bounds_and_transitions():
    calendar = USEquitiesCalendar()
    # 2026 Thanksgiving Friday closes at 13:00 New York (18:00 UTC).
    assert calendar.is_open(utc(2026, 11, 27, 17, 59))
    assert not calendar.is_open(utc(2026, 11, 27, 18, 0))
    assert not calendar.is_open(utc(2026, 11, 26, 15, 0))
    assert calendar.session_bounds(date(2026, 11, 27))[1].hour == 13
    assert calendar.session_bounds(date(2026, 11, 26)) is None
    early = market_session_state("us", datetime(2026, 11, 27, 12, 45))
    assert early.status == "OPEN"
    assert early.next_transition_iso.endswith("13:00:00-05:00")
    holiday = market_session_state("us", datetime(2026, 12, 25, 12, 0))
    assert holiday.status == "HOLIDAY"
    weekend = market_session_state("us", datetime(2026, 11, 28, 12, 0))
    assert weekend.status == "WEEKEND"
    assert make_calendar("us-equities", "1m").signature() == calendar_signature(calendar)


def test_us_calendar_operator_overrides_are_explicit_and_audited():
    calendar = USEquitiesCalendar(
        closed_dates=["2026-10-23"], early_close_dates=["2026-10-22"]
    )
    assert not calendar.is_open(utc(2026, 10, 23, 15, 0))
    assert calendar.session_bounds(date(2026, 10, 23)) is None
    assert calendar.close_time(date(2026, 10, 22)) == time(13, 0)
    signature = calendar.signature()
    assert signature["closed_date_overrides"] == ["2026-10-23"]
    assert signature["early_close_overrides"] == ["2026-10-22"]
    # A date cannot be both closed and a half day.
    with pytest.raises(ValueError, match="both"):
        USEquitiesCalendar(closed_dates=["2026-10-23"],
                           early_close_dates=["2026-10-23"])
    with pytest.raises(ValueError, match="Invalid"):
        USEquitiesCalendar(closed_dates=["not-a-date"])


# ------------------------------------------------------------------- alpaca data


def test_us_symbol_validation_rejects_non_us_identities():
    assert validate_us_symbol(" aapl ") == "AAPL"
    assert validate_us_symbol("BRK.B") == "BRK.B"
    assert validate_us_symbol("BF-B") == "BF-B"
    with pytest.raises(ValueError, match="ticker"):
        validate_us_symbol("NASDAQ|AAPL")
    with pytest.raises(ValueError, match="ticker"):
        validate_us_symbol("")
    with pytest.raises(ValueError, match="ticker"):
        validate_us_symbol("1234ABCDEFGHIJKLMNOP")


def _history_provider(bars_by_page, calls):
    def handler(request):
        calls.append(request)
        token = request.url.params.get("page_token")
        page = 1 if token == "next" else 0
        bars = bars_by_page[page]
        return httpx.Response(200, json={"bars": {"AAPL": bars},
                                          "next_page_token": "next" if page == 0 else None})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AlpacaHistoricalProvider("key", "secret", client=client)


def test_alpaca_history_paginates_and_honors_exclusive_end():
    calls = []
    provider = _history_provider([
        [{"t": "2026-10-21T14:30:00Z", "o": 10, "h": 11, "l": 9, "c": 10, "v": 40},
         {"t": "2026-10-21T14:32:00Z", "o": 12, "h": 13, "l": 11, "c": 12, "v": 30}],
        [{"t": "2026-10-21T14:31:00Z", "o": 11, "h": 12, "l": 10, "c": 11, "v": 50}],
    ], calls)
    bars = provider.historical("aapl", "1m", utc(2026, 10, 21, 14, 30),
                               utc(2026, 10, 21, 14, 32))
    assert [bar.close for bar in bars] == [10, 11]
    assert len(calls) == 2
    assert calls[0].headers["APCA-API-KEY-ID"] == "key"
    assert calls[0].url.params["feed"] == "iex"
    assert calls[0].url.params["adjustment"] == "split"


def test_alpaca_history_accepts_an_empty_range_without_inventing_bars():
    def handler(request):
        return httpx.Response(200, json={"bars": {}})

    provider = AlpacaHistoricalProvider(
        "key", "secret", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    bars = provider.historical("AAPL", "1m", utc(2026, 1, 5, 14, 30),
                               utc(2026, 1, 5, 15, 30))
    assert bars == []


def test_alpaca_history_requires_credentials_and_aware_range():
    with pytest.raises(ValueError, match="API_KEY"):
        AlpacaHistoricalProvider("", "secret")
    with pytest.raises(ValueError, match="adjustment"):
        AlpacaHistoricalProvider("k", "s", adjustment="magic")
    provider = AlpacaHistoricalProvider(
        "key", "secret",
        client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"bars": {}}))),
    )
    with pytest.raises(ValueError, match="timezone"):
        provider.historical("AAPL", "1m", datetime(2026, 1, 1),
                            datetime(2026, 1, 2, tzinfo=UTC))
    with pytest.raises(ValueError, match="after start"):
        provider.historical("AAPL", "1m", utc(2026, 1, 2), utc(2026, 1, 1))


def test_alpaca_history_rejects_malformed_payloads():
    def bad(request):
        return httpx.Response(200, json={"bars": {"AAPL": [{"t": "nonsense"}]}})

    provider = AlpacaHistoricalProvider(
        "key", "secret", client=httpx.Client(transport=httpx.MockTransport(bad))
    )
    with pytest.raises(ValueError):
        provider.historical("AAPL", "1m", utc(2026, 1, 5, 14, 30), utc(2026, 1, 5, 15, 0))


def test_alpaca_stream_decodes_bars_and_ignores_corrections():
    event = {"T": "b", "S": "AAPL", "t": "2026-10-21T14:30:00Z",
             "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 3}
    candle = AlpacaStream.decode(event)
    assert candle.symbol == "AAPL" and candle.close == 10.5
    assert AlpacaStream.decode({**event, "T": "u"}) is None      # correction
    assert AlpacaStream.decode({**event, "T": "t"}) is None      # trade print
    with pytest.raises(ValueError, match="ticker"):
        AlpacaStream.decode({**event, "S": "NASDAQ|AAPL"})
    with pytest.raises(ValueError, match="invalid bar"):
        AlpacaStream.decode({**event, "c": "not-a-number"})
    with pytest.raises(ValueError, match="Invalid Alpaca stream message"):
        AlpacaStream.decode(["not", "a", "dict"])


def test_alpaca_stream_is_read_only_and_1m_only():
    stream = AlpacaStream("key", "secret", ["AAPL"], feed="iex")
    assert stream.supported_timeframes == frozenset({"1m"})
    assert "order" not in " ".join(dir(stream)).lower()
    assert "submit" not in " ".join(dir(stream)).lower()
    with pytest.raises(ValueError, match="does not support"):
        stream.check_timeframe("5m")
    with pytest.raises(ValueError, match="distinct"):
        AlpacaStream("key", "secret", ["AAPL", "AAPL"])
    with pytest.raises(ValueError, match="ALPACA_DATA_FEED"):
        AlpacaStream("key", "secret", ["AAPL"], feed="nasdaq")


class FakeSocket:
    """Minimal websockets stand-in: a scripted list of frames then silence."""

    def __init__(self, frames, *, fail_after=None):
        self.frames = list(frames)
        self.sent = []
        self.closed = False
        self.fail_after = fail_after

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def recv(self):
        if self.fail_after is not None and len(self.frames) <= self.fail_after:
            raise TimeoutError("scripted silence")
        if not self.frames:
            await asyncio.sleep(3600)
        return self.frames.pop(0)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def test_alpaca_handshake_reads_past_the_connected_frame():
    """The first frame is ``connected``; treating it as the auth answer broke
    every real handshake."""
    stream = AlpacaStream("key", "secret", ["AAPL"], feed="iex")
    socket = FakeSocket([
        json.dumps([{"T": "success", "msg": "connected"}]),
        json.dumps([{"T": "success", "msg": "authenticated"}]),
    ])
    asyncio.run(stream._await_confirmation(socket, "authenticated"))
    assert stream.errors == []


def test_alpaca_handshake_accepts_subscription_without_updated_bars():
    stream = AlpacaStream("key", "secret", ["AAPL", "MSFT"], feed="iex")
    socket = FakeSocket([
        json.dumps([{"T": "subscription", "bars": ["AAPL", "MSFT"]}]),
    ])
    asyncio.run(stream._await_confirmation(socket, "subscription", kind="subscription"))


def test_alpaca_handshake_rejects_partial_subscription():
    stream = AlpacaStream("key", "secret", ["AAPL", "MSFT"], feed="iex")
    socket = FakeSocket([
        json.dumps([{"T": "subscription", "bars": ["AAPL"], "updatedBars": ["AAPL"]}]),
    ])
    with pytest.raises(ValueError, match="bar symbols"):
        asyncio.run(stream._await_confirmation(socket, "subscription", kind="subscription"))


def test_alpaca_handshake_surfaces_an_error_frame_and_times_out():
    stream = AlpacaStream("key", "secret", ["AAPL"], feed="iex")
    socket = FakeSocket([json.dumps([{"T": "error", "code": 401,
                                      "msg": "unauthorized"}])])
    with pytest.raises(ValueError, match="rejected"):
        asyncio.run(stream._await_confirmation(socket, "authenticated"))
    assert stream.errors == ["401"]

    stream.handshake_timeout = 0.01
    silent = FakeSocket([], fail_after=0)
    with pytest.raises(ValueError, match="did not confirm"):
        asyncio.run(stream._await_confirmation(silent, "authenticated"))


def test_alpaca_stream_reconnects_and_yields_the_next_bar(monkeypatch):
    """A dropped socket is counted and retried; the bar after the reconnect is
    still delivered, and every failure is counted rather than swallowed."""
    import websockets.asyncio.client as client_module
    from websockets.exceptions import ConnectionClosed

    bar = {"T": "b", "S": "AAPL", "t": "2026-10-21T14:30:00Z",
           "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 3}
    attempts = {"n": 0}

    class ReconnectingSocket(FakeSocket):
        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.frames:
                return self.frames.pop(0)
            raise StopAsyncIteration

    class Connection:
        def __init__(self, socket):
            self.socket = socket

        async def __aenter__(self):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionClosed(None, None)
            return self.socket

        async def __aexit__(self, *exc):
            return False

    socket = ReconnectingSocket([
        json.dumps([{"T": "success", "msg": "connected"}]),
        json.dumps([{"T": "success", "msg": "authenticated"}]),
        json.dumps([{"T": "subscription", "bars": ["AAPL"],
                     "updatedBars": ["AAPL"]}]),
        json.dumps([bar]),
    ])
    monkeypatch.setattr(client_module, "connect", lambda *a, **k: Connection(socket))
    monkeypatch.setattr("asyncio.sleep", _instant_sleep)

    stream = AlpacaStream("key", "secret", ["AAPL"], feed="iex")

    async def consume():
        received = []
        async for candle in stream.stream(["AAPL"], "1m"):
            received.append(candle)
            await stream.close()
        return received

    received = asyncio.run(consume())
    assert [candle.close for candle in received] == [10.5]
    assert stream.reconnects == 1
    # close() clears the subscription flags: a closed feed must never report
    # "subscribed" in the status payload.
    assert stream.subscribed is False
    assert stream.authenticated is False
    assert stream.status == "DISCONNECTED"


async def _instant_sleep(*args, **kwargs):
    return None


def test_alpaca_warmup_returns_only_completed_session_bars(monkeypatch):
    stream = AlpacaStream("key", "secret", ["AAPL"], feed="iex")

    # Five complete bars that ended a few minutes ago: still in-session, and
    # already available at "now", so nothing is filtered as future data.
    anchor = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=10)

    def fake_historical(self, symbol, timeframe, start=None, end=None):
        return [
            Candle(anchor + timedelta(minutes=n),
                   100 + n, 101 + n, 99 + n, 100.5 + n, 1000, symbol)
            for n in range(5)
        ]

    monkeypatch.setattr(AlpacaHistoricalProvider, "historical", fake_historical)
    bars = asyncio.run(stream.warmup_bars(3))
    # The warm-up is bounded by the requested count, and every bar it returns is
    # already complete at "now".
    assert len(bars) == 3
    assert all(bar.available_at("1m") <= datetime.now(UTC) for bar in bars)
    assert stream.backfill["status"] == "ok"
    assert stream.backfill["loaded"] == 3


def test_alpaca_warmup_failure_degrades_instead_of_inventing_bars(monkeypatch):
    stream = AlpacaStream("key", "secret", ["AAPL"], feed="iex")

    def exploding(self, *args, **kwargs):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(AlpacaHistoricalProvider, "historical", exploding)
    assert asyncio.run(stream.warmup_bars(50)) == []
    assert stream.backfill["status"] == "failed"
    assert stream.backfill["error"] == "ConnectError"


def test_alpaca_stream_metrics_never_leak_credentials():
    stream = AlpacaStream("super-secret-key", "super-secret-value", ["AAPL"])
    payload = json.dumps(stream.metrics())
    assert "super-secret" not in payload
    assert set(stream.metrics()) >= {
        "connection_status", "authenticated", "subscribed", "stale_feed",
        "paper_mode", "warmup", "market_status",
    }


# ------------------------------------------------------------------ paper broker


def _broker(**limits) -> PaperBroker:
    return PaperBroker(ExecutionConfig(initial_capital=10_000, position_size=0.5),
                       currency="USD", timeframe="1m",
                       calendar=USEquitiesCalendar(), risk_limits=limits)


def test_paper_position_cap_bounds_a_single_fill():
    broker = _broker(max_position_value=1_000, max_open_positions=1)
    broker.portfolio.marks.update({"AAPL": 100, "MSFT": 100})
    stamp = "2026-10-21T14:31:00+00:00"
    broker.handle_signal(Signal.BUY, "AAPL", stamp, "test", persist=False)
    entry = next(iter(broker.orders.values()))
    assert entry.quantity * broker.config.fill_price(100, "BUY") <= 1_000
    broker.handle_signal(Signal.BUY, "MSFT", stamp, "test", persist=False)
    assert len(broker.orders) == 1


def test_paper_daily_loss_blocks_new_entries_for_the_new_york_session():
    broker = _broker(max_daily_loss=100)
    broker.portfolio.marks["AAPL"] = 100
    broker.portfolio.cash = 9_800
    stamp = "2026-10-21T14:31:00+00:00"
    broker.portfolio.snapshot(stamp, "USD")
    broker.handle_signal(Signal.BUY, "AAPL", stamp, "test", persist=False)
    assert not broker.orders
    assert broker.context["paper_risk_halts"]["daily_loss_date"] == "2026-10-21"
    assert any(event["kind"] == "daily_loss_halt"
               for event in broker.context["risk_events"])


def test_paper_drawdown_halt_latches_and_flatten_policy_closes_inventory():
    broker = PaperBroker(
        ExecutionConfig(initial_capital=10_000, position_size=0.5),
        currency="USD", timeframe="1m", calendar=USEquitiesCalendar(),
        risk_limits={"max_drawdown": 0.05, "halt_policy": "FLATTEN"},
    )
    broker.portfolio.marks = {"AAPL": 100}
    broker.portfolio.positions["AAPL"] = __import__(
        "app.backtesting.portfolio", fromlist=["Position"]
    ).Position("AAPL", 10, 100, 0.0, "2026-10-21T14:30:00+00:00", "test")
    broker.portfolio.cash = 9_000
    broker.portfolio.peak = 11_000
    candle = Candle(datetime(2026, 10, 21, 14, 32, tzinfo=UTC),
                    80, 81, 79, 80, 1_000, "AAPL")
    broker.on_bar(candle, persist=False)
    halts = broker.context["paper_risk_halts"]
    assert halts["drawdown"] is True
    exits = [order for order in broker.orders.values()
             if order.side == "SELL" and order.reason == "risk_halt_flatten"]
    assert exits, "FLATTEN must submit a simulated exit for the open position"
    # The exit is a normal paper order: it fills from this bar's open at the
    # modelled cost, and the position is gone.
    assert exits[0].status in {"NEW", "PARTIALLY_FILLED", "FILLED"}
    assert "AAPL" not in broker.portfolio.positions


def test_paper_gross_exposure_is_bounded_across_symbols():
    broker = _broker(max_gross_exposure=0.3)
    broker.portfolio.marks.update({"AAPL": 100, "MSFT": 100, "NVDA": 100})
    stamp = "2026-10-21T14:31:00+00:00"
    for symbol in ("AAPL", "MSFT", "NVDA"):
        broker.handle_signal(Signal.BUY, symbol, stamp, "test", persist=False)
    committed = sum(order.quantity for order in broker.orders.values()) * 100
    assert committed <= 0.3 * broker.portfolio.equity + 1
    # Only the first entry fits inside a 30% exposure budget.
    assert len(broker.orders) == 1


def test_paper_trade_count_limit_counts_entries_per_session():
    broker = _broker(max_trades_per_day=1)
    broker.portfolio.marks.update({"AAPL": 100, "MSFT": 100})
    stamp = "2026-10-21T14:31:00+00:00"
    broker.handle_signal(Signal.BUY, "AAPL", stamp, "test", persist=False)
    broker.handle_signal(Signal.BUY, "MSFT", stamp, "test", persist=False)
    assert len(broker.orders) == 1
    assert any(event["detail"].get("reason") == "paper_max_trades_per_day"
               for event in broker.context["risk_events"])


def test_paper_position_cap_still_holds_after_an_adverse_open_gap():
    broker = _broker(max_position_value=1_000)
    broker.portfolio.marks["AAPL"] = 100
    broker.handle_signal(Signal.BUY, "AAPL", "2026-10-21T14:30:00+00:00",
                         "test", persist=False)
    order = next(iter(broker.orders.values()))
    broker.on_bar(Candle(datetime(2026, 10, 21, 14, 31, tzinfo=UTC),
                         200, 201, 199, 200, 1_000, "AAPL"), persist=False)
    assert order.status == "PARTIALLY_FILLED"
    assert order.filled_quantity * order.average_price <= 1_000.01


def test_paper_orders_are_idempotent_across_a_restart_replay():
    broker = _broker()
    broker.portfolio.marks["AAPL"] = 100
    stamp = "2026-10-21T14:31:00+00:00"
    broker.handle_signal(Signal.BUY, "AAPL", stamp, "momentum", persist=False)
    first = len(broker.orders)
    broker.handle_signal(Signal.BUY, "AAPL", stamp, "momentum", persist=False)
    assert len(broker.orders) == first
    assert any(event["kind"] == "order_deduplicated"
               for event in broker.context["risk_events"])


def test_paper_risk_limits_reject_impossible_configurations():
    with pytest.raises(ValueError, match="max_drawdown"):
        PaperRiskLimits({"max_drawdown": 1.0})
    with pytest.raises(ValueError, match="gross_exposure"):
        PaperRiskLimits({"max_gross_exposure": 2})
    with pytest.raises(ValueError, match="halt_policy"):
        PaperRiskLimits({"halt_policy": "PANIC"})
    with pytest.raises(ValueError, match="finite"):
        PaperRiskLimits({"max_daily_loss": float("nan")})


# ------------------------------------------------------------------ simulator


class StubProvider:
    status = "CONNECTED"
    last_message = None
    stale_after_seconds = 300
    symbols = ["AAPL"]

    def metrics(self):
        return {}

    async def close(self):
        return None


def _running_simulator(**limits):
    from app.strategies.moving_average import MovingAverageStrategy

    broker = _broker(**limits)
    simulator = PaperSimulator(broker)
    simulator.provider = StubProvider()
    simulator.strategy = MovingAverageStrategy()
    simulator.symbols = ["AAPL"]
    simulator.state = BotState.RUNNING
    return simulator


def _minute_bar(minutes_ago: int = 1, close: float = 100.0, **overrides) -> Candle:
    moment = datetime.now(UTC).replace(second=0, microsecond=0)
    moment -= timedelta(minutes=minutes_ago)
    values = dict(o=close, h=close + 1, l=close - 1, c=close, v=1_000)
    values.update(overrides)
    return Candle(moment, values["o"], values["h"], values["l"], values["c"],
                  values["v"], "AAPL")


def test_simulator_accepts_a_fresh_completed_bar():
    simulator = _running_simulator()
    simulator.process_bar(_minute_bar(1))
    assert simulator.broker.last_bars.get("AAPL")
    assert simulator.processed == 1


def test_simulator_rejects_a_future_or_unfinished_bar():
    simulator = _running_simulator()
    with pytest.raises(ValueError, match="unfinished/future"):
        simulator.process_bar(_minute_bar(-5))
    assert simulator.risk_counters["rejected_bars"] == 1


def test_simulator_rejects_a_stale_bar():
    simulator = _running_simulator()
    with pytest.raises(ValueError, match="stale"):
        simulator.process_bar(_minute_bar(10))  # 10 minutes old, window is 5
    assert simulator.risk_counters["rejected_bars"] == 1


def test_simulator_ignores_out_of_session_bars_instead_of_crashing():
    simulator = _running_simulator()
    # 03:00 New York: real feed artefact, not a tradable session bar.
    pre_market = Candle(datetime(2026, 10, 21, 7, 0, tzinfo=UTC),
                        100, 101, 99, 100, 10, "AAPL")
    simulator.process_bar(pre_market)
    assert simulator.processed == 0
    assert simulator.risk_counters["out_of_session_bars"] == 1
    assert simulator.state == BotState.RUNNING


def test_simulator_deduplicates_and_refuses_conflicting_duplicates():
    simulator = _running_simulator()
    bar = _minute_bar(1)
    simulator.process_bar(bar)
    simulator.process_bar(bar)
    assert simulator.risk_counters["deduplicated_bars"] == 1
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        simulator.process_bar(Candle(bar.timestamp, 1, 2, 0.5, 1.5, 5, "AAPL"))


def test_simulator_refuses_out_of_order_bars():
    simulator = _running_simulator()
    simulator.process_bar(_minute_bar(1))
    with pytest.raises(ValueError, match="Out-of-order"):
        simulator.process_bar(_minute_bar(2))


def test_simulator_pauses_on_a_missing_bar_instead_of_guessing():
    simulator = _running_simulator()
    simulator.process_bar(_minute_bar(5))
    simulator.process_bar(_minute_bar(1))  # three minutes are missing
    assert simulator.state == BotState.PAUSED
    assert "Data gap" in (simulator.error or "")


def test_simulator_status_reports_paper_only_risk_and_feed_state():
    simulator = _running_simulator(max_position_value=1_000, max_daily_loss=50,
                                   max_gross_exposure=0.5)
    status = simulator.status()
    assert status["mode"] == "PAPER"
    assert status["live_trading"] is False
    assert status["simulated_fills"] is True
    assert status["paper_mode"] is True
    assert status["session_timezone"] == "America/New_York"
    assert status["paper_risk_limits"]["max_gross_exposure"] == 0.5
    assert "paper_risk_events" in status
    assert "rejected_bars" in status["risk_counters"]


def test_simulator_stop_is_idempotent_and_cancels_orders():
    simulator = _running_simulator()
    simulator.broker.portfolio.marks["AAPL"] = 100
    simulator.broker.handle_signal(Signal.BUY, "AAPL", "2026-10-21T14:30:00+00:00",
                                   "test", persist=False)
    asyncio.run(simulator.stop())
    asyncio.run(simulator.stop())
    assert simulator.state == BotState.STOPPED
    assert all(order["status"] == "CANCELLED"
               for order in simulator.broker.get_orders())


# -------------------------------------------------------------------- settings


REQUIRED_US = dict(
    data_source="alpaca", market="us", currency="USD",
    market_calendar="us-equities", default_timeframe="1m",
    alpaca_api_key="key", alpaca_api_secret="secret",
    alpaca_symbols="AAPL,MSFT", default_symbol="AAPL",
    paper_max_position_value=1_000, paper_max_open_positions=2,
    paper_max_daily_loss=100, paper_max_drawdown=0.05,
    paper_max_trades_per_day=4, paper_max_gross_exposure=0.5,
)


def test_alpaca_settings_require_local_paper_and_every_bounded_limit():
    settings = Settings(**REQUIRED_US)
    assert settings.mode == "PAPER" and settings.broker == "LOCAL_PAPER"
    assert settings.execution_kwargs().keys() == {
        "initial_capital", "commission", "slippage", "spread", "position_size",
        "stop_loss", "take_profit", "max_participation",
    }
    assert settings.paper_risk_limits()["max_drawdown"] == 0.05
    assert settings.paper_risk_limits()["max_gross_exposure"] == 0.5
    with pytest.raises(ValueError, match="PAPER_MAX_POSITION_VALUE"):
        Settings(**{**REQUIRED_US, "paper_max_position_value": 0})
    with pytest.raises(ValueError, match="PAPER_MAX_GROSS_EXPOSURE"):
        Settings(**{**REQUIRED_US, "paper_max_gross_exposure": 0})
    with pytest.raises(ValueError, match="PAPER_MAX_DAILY_LOSS"):
        Settings(**{**REQUIRED_US, "paper_max_daily_loss": 0})
    with pytest.raises(ValueError, match="PAPER_MAX_TRADES_PER_DAY"):
        Settings(**{**REQUIRED_US, "paper_max_trades_per_day": 0})
    with pytest.raises(ValueError, match="local PAPER"):
        Settings(**{**REQUIRED_US, "broker": "UPSTOX_SANDBOX",
                    "upstox_sandbox_access_token": "token"})


def test_alpaca_settings_refuse_live_trading_and_wrong_market():
    with pytest.raises(ValueError):
        Settings(**{**REQUIRED_US, "live_trading": True})
    with pytest.raises(ValueError, match="MARKET=us"):
        Settings(**{**REQUIRED_US, "market": "india"})
    with pytest.raises(ValueError, match="MARKET_CALENDAR"):
        Settings(**{**REQUIRED_US, "market_calendar": "continuous"})
    with pytest.raises(ValueError, match="DEFAULT_TIMEFRAME"):
        Settings(**{**REQUIRED_US, "default_timeframe": "5m"})
    with pytest.raises(ValueError, match="ALPACA_DATA_FEED"):
        Settings(**{**REQUIRED_US, "alpaca_data_feed": "consolidated"})
    with pytest.raises(ValueError, match="ALPACA_SYMBOLS"):
        Settings(**{**REQUIRED_US, "alpaca_symbols": ""})
    with pytest.raises(ValueError, match="DEFAULT_SYMBOL must be one of"):
        Settings(**{**REQUIRED_US, "default_symbol": "TSLA"})
    assert Settings(**{**REQUIRED_US, "default_symbol": "aapl"}).default_symbol == "aapl"


def test_settings_validate_us_session_overrides_and_halt_policy():
    settings = Settings(**{**REQUIRED_US,
                           "us_closed_dates": "2026-10-23",
                           "us_early_close_dates": "2026-12-24"})
    assert settings.calendar_overrides() == {
        "closed_dates": ["2026-10-23"], "early_close_dates": ["2026-12-24"]
    }
    with pytest.raises(ValueError, match="Invalid US session override"):
        Settings(**{**REQUIRED_US, "us_closed_dates": "23-10-2026"})
    with pytest.raises(ValueError, match="PAPER_HALT_POLICY"):
        Settings(**{**REQUIRED_US, "paper_halt_policy": "HOLD"})
    assert Settings(**REQUIRED_US).paper_halt_policy == "BLOCK_ENTRIES"


# ------------------------------------------------------------------------- api


def test_paper_start_rejects_an_upstox_key_as_a_us_ticker():
    from app.api.schemas import PaperStartRequest

    # Either the generic symbol validator or the US-ticker validator rejects it;
    # both are refusals, and neither silently coerces an Indian instrument key
    # into a US ticker.
    with pytest.raises(Exception, match="[Ss]ymbol|[Tt]icker"):
        PaperStartRequest(source="alpaca", symbol="NSE_EQ|INE002A01018",
                          timeframe="1m", strategy="moving_average")
    assert PaperStartRequest(source="alpaca", symbol="aapl", timeframe="1m",
                             strategy="moving_average").symbol == "AAPL"


def test_paper_start_route_rechecks_paper_only_switches():
    """Defence in depth: even if a Settings object is mutated after validation,
    the start route refuses to attach Alpaca data to anything but local PAPER."""
    from types import SimpleNamespace

    from app.api.routes.paper import do_start
    from app.api.schemas import PaperStartRequest

    settings = Settings(**REQUIRED_US)
    object.__setattr__(settings, "live_trading", True)
    broker = PaperBroker(ExecutionConfig(initial_capital=10_000), currency="USD",
                         timeframe="1m", calendar=USEquitiesCalendar())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=settings, broker=broker, simulator=PaperSimulator(broker))))
    body = PaperStartRequest(source="alpaca", symbol="AAPL", timeframe="1m",
                             strategy="moving_average")
    with pytest.raises(ValueError, match="local PAPER"):
        asyncio.run(do_start(body, request))


# ------------------------------------------------- end-to-end (fully mocked)


class ScriptedUSStream:
    """A US feed stand-in: yields a scripted list of completed bars, no network."""

    status = "CONNECTED"
    last_message = None
    # The scripted bars span eight minutes of wall clock, so the freshness
    # window has to cover the oldest one; real feeds use 120 s.
    stale_after_seconds = 900
    supported_timeframes = frozenset({"1m"})

    def __init__(self, symbols, bars):
        self.symbols = list(symbols)
        self.bars = list(bars)
        self.stop = asyncio.Event()
        self.closed = False

    def metrics(self):
        return {"provider": "scripted", "connection_status": self.status,
                "paper_mode": True, "paper_only": True}

    async def stream(self, symbols, timeframe):
        for bar in self.bars:
            yield bar
        await self.stop.wait()

    async def close(self):
        self.closed = True
        self.stop.set()


def test_mocked_us_session_generates_only_simulated_orders_and_fills(monkeypatch):
    """The whole paper path end to end, with a scripted feed and no network.

    This is what "verify that orders are simulated" means in this repository:
    the loop, the strategy, the broker, the risk limits and the persistence
    layer all run, and every resulting order is a local PaperBroker row.
    """
    from app.paper_trading.execution import ContinuousCalendar
    from app.strategies.momentum import MomentumStrategy

    # Any attempt to reach Alpaca during this test must fail loudly.
    def forbidden(*args, **kwargs):
        raise AssertionError("the paper session must not construct a network client")

    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr("app.data.alpaca.AlpacaHistoricalProvider", forbidden)

    broker = PaperBroker(
        ExecutionConfig(initial_capital=10_000, position_size=0.5,
                        commission=0.0005, spread=0.0002, slippage=0.0005),
        currency="USD", timeframe="1m", calendar=ContinuousCalendar(),
        risk_limits={"max_position_value": 2_000, "max_open_positions": 1,
                     "max_gross_exposure": 0.5},
    )
    simulator = PaperSimulator(broker)
    start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=8)
    bars = [
        Candle(start + timedelta(minutes=index), 100 + index, 101.5 + index,
               99.5 + index, 100.5 + index, 50_000, "AAPL")
        for index in range(8)
    ]
    provider = ScriptedUSStream(["AAPL"], bars)
    strategy = MomentumStrategy(period=2, threshold=0.0001)

    async def run():
        await simulator.start(provider, strategy, ["AAPL"])
        for _ in range(200):
            if broker.fills:
                break
            await asyncio.sleep(0.01)
        status = await simulator.stop()
        return status

    status = asyncio.run(run())

    assert broker.fills, "the scripted session should have produced a simulated fill"
    # Every order and fill is a local paper record; nothing was routed anywhere.
    assert all(order.side in {"BUY", "SELL"} for order in broker.orders.values())
    assert {fill.order_id for fill in broker.fills} <= set(broker.orders)
    for fill in broker.fills:
        assert fill.fee >= 0
        assert fill.price > 0
    assert status["mode"] == "PAPER"
    assert status["live_trading"] is False
    assert status["simulated_fills"] is True
    # The BTC/Upstox live broker classes are not involved in this path at all.
    assert type(broker).__name__ == "PaperBroker"
    assert broker.currency == "USD"
    assert broker.timeframe == "1m"
    # Risk caps held for the whole session.
    for position in broker.portfolio.positions.values():
        assert position.quantity * position.entry_price <= 2_000 + 1e-6
