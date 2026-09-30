"""Kronos market data layer: contract, validation, calendars, closed-candle gate.

Covers the invalid-data matrix required for the integration: valid OHLCV,
invalid OHLCV, NaN/Inf, duplicate timestamps, unordered timestamps, gaps, wrong
timeframe, insufficient history, timezone correctness — and the rule that
nothing is ever silently repaired or fabricated.
"""

from datetime import datetime, time, timedelta, timezone

import pytest

from axiom.market import (
    ClosedCandleGate,
    ContinuousCalendar,
    DailyCalendar,
    HttpJsonMarketDataProvider,
    MarketDataBar,
    MarketDataError,
    ReplayMarketDataProvider,
    SessionCalendar,
    validate_market_data,
)
from axiom.models import DataQualityState

UTC = timezone.utc
SAMPLE_CSV = "data/sample_1h.csv"
DAILY_CSV = "data/sample.csv"


def bar(hour: int, close: float = 100.0, *, open_=None, high=None, low=None,
        volume: float = 1000.0, instrument: str = "DEMO",
        day: int = 1, minute: int = 0) -> MarketDataBar:
    opening = close if open_ is None else open_
    return MarketDataBar(
        datetime(2024, 1, day, hour, minute, tzinfo=UTC),
        opening,
        max(opening, close) if high is None else high,
        min(opening, close) if low is None else low,
        close,
        volume,
        instrument,
    )


def window(count: int, *, start: datetime | None = None, step: int = 3600,
           instrument: str = "DEMO", price: float = 100.0) -> list[MarketDataBar]:
    origin = start or datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    bars = []
    for index in range(count):
        close = price + index * 0.1
        bars.append(
            MarketDataBar(
                origin + timedelta(seconds=index * step),
                close - 0.05,
                close + 0.2,
                close - 0.2,
                close,
                1000.0 + index,
                instrument,
            )
        )
    return bars


class TestMarketDataContract:
    def test_valid_ohlcv_window_passes(self):
        quality = validate_market_data(window(20), "1h", min_history=5)
        assert quality.ok, quality.errors
        assert quality.bars == 20
        assert quality.state is DataQualityState.OK

    def test_bar_rejects_naive_timestamp(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            MarketDataBar(datetime(2024, 1, 1), 1, 2, 0.5, 1.5, 10, "DEMO")

    def test_bar_from_row_rejects_naive_timestamp(self):
        with pytest.raises(MarketDataError, match="malformed OHLCV row"):
            MarketDataBar.from_row({
                "timestamp": "2024-01-01T00:00:00", "open": 1, "high": 2,
                "low": 0.5, "close": 1.5, "volume": 10, "symbol": "DEMO"})

    def test_bar_from_row_parses_zulu_timestamps(self):
        parsed = MarketDataBar.from_row({
            "timestamp": "2024-01-01T00:00:00Z", "open": 1, "high": 2,
            "low": 0.5, "close": 1.5, "volume": 10, "symbol": "DEMO"})
        assert parsed.timestamp.tzinfo is not None
        assert parsed.origin == "MARKET_OBSERVED"

    def test_available_at_uses_declared_timeframe(self):
        candle = bar(0)
        assert candle.available_at("1h") == candle.timestamp + timedelta(hours=1)
        assert not candle.is_closed_at("1h", candle.timestamp + timedelta(minutes=30))
        assert candle.is_closed_at("1h", candle.timestamp + timedelta(hours=1))


class TestMarketDataValidation:
    def test_nan_and_inf_are_rejected(self):
        bars = window(5)
        broken = MarketDataBar(bars[2].timestamp, float("nan"), 101.0, 99.0, 100.0,
                               10.0, "DEMO")
        quality = validate_market_data(bars[:2] + [broken] + bars[3:], "1h", min_history=2)
        assert quality.state is DataQualityState.DATA_ERROR
        assert any("non-finite" in error for error in quality.errors)

        infinite = MarketDataBar(bars[2].timestamp, 100.0, float("inf"), 99.0, 100.0,
                                 10.0, "DEMO")
        quality = validate_market_data([bars[0], bars[1], infinite], "1h", min_history=2)
        assert any("non-finite" in error for error in quality.errors)

    def test_duplicate_timestamps_are_rejected(self):
        bars = window(4)
        quality = validate_market_data(bars + [bars[-1]], "1h", min_history=2)
        assert any("duplicate timestamp" in error for error in quality.errors)

    def test_unordered_timestamps_are_rejected(self):
        bars = window(4)
        quality = validate_market_data([bars[0], bars[2], bars[1], bars[3]], "1h",
                                       min_history=2)
        assert any("not strictly increasing" in error for error in quality.errors)

    def test_gaps_are_reported_not_filled(self):
        bars = window(3)
        # Skip 4 hours: the validator must report the gap, not fill it.
        shifted = MarketDataBar(bars[1].timestamp + timedelta(hours=5), 100.0, 101.0,
                                99.0, 100.0, 10.0, "DEMO")
        quality = validate_market_data([bars[0], bars[1], shifted], "1h", min_history=2)
        assert any("gap before" in error for error in quality.errors)
        assert quality.state is DataQualityState.DATA_ERROR

    def test_gap_is_a_warning_when_the_calendar_explains_it(self):
        # Friday close → Monday open is not a missing candle.
        calendar = DailyCalendar("weekdays", weekdays=range(5))
        friday = MarketDataBar(datetime(2024, 1, 5, tzinfo=UTC), 100, 101, 99, 100, 10,
                               "DEMO")
        monday = MarketDataBar(datetime(2024, 1, 8, tzinfo=UTC), 100, 101, 99, 100, 10,
                               "DEMO")
        quality = validate_market_data([friday, monday], "1D", min_history=2,
                                       calendar=calendar)
        assert quality.ok, quality.errors

    def test_wrong_timeframe_spacing_is_rejected(self):
        bars = window(6, step=1800)  # 30-minute spacing declared as 1h
        quality = validate_market_data(bars, "1h", min_history=2)
        assert any("wrong timeframe" in error for error in quality.errors)

    def test_ohlc_relationships_are_enforced(self):
        base = bar(0, 100.0)
        high_too_low = MarketDataBar(base.timestamp, 100.0, 99.0, 98.0, 100.5, 10.0,
                                     "DEMO")
        quality = validate_market_data([high_too_low, bar(1)], "1h", min_history=2)
        assert any("high" in error and "max(open, close)" in error
                   for error in quality.errors)

        low_too_high = MarketDataBar(base.timestamp, 100.0, 101.0, 100.9, 99.5, 10.0,
                                     "DEMO")
        quality = validate_market_data([low_too_high, bar(1)], "1h", min_history=2)
        assert any("low" in error for error in quality.errors)

        inverted = MarketDataBar(base.timestamp, 100.0, 99.0, 101.0, 100.0, 10.0, "DEMO")
        quality = validate_market_data([inverted, bar(1)], "1h", min_history=2)
        assert any("high" in error and "low" in error for error in quality.errors)

    def test_negative_volume_is_rejected(self):
        bars = window(3)
        negative = MarketDataBar(bars[1].timestamp, 100.0, 101.0, 99.0, 100.0, -5.0,
                                 "DEMO")
        quality = validate_market_data([bars[0], negative, bars[2]], "1h", min_history=2)
        assert any("volume must be >= 0" in error for error in quality.errors)

    def test_insufficient_history_is_its_own_verdict(self):
        quality = validate_market_data(window(3), "1h", min_history=64)
        assert quality.state is DataQualityState.INSUFFICIENT_HISTORY
        assert any("insufficient history" in error for error in quality.errors)

    def test_empty_window_is_a_data_error(self):
        quality = validate_market_data([], "1h")
        assert quality.state is DataQualityState.DATA_ERROR

    def test_instrument_mismatch_is_rejected(self):
        quality = validate_market_data(window(3, instrument="OTHER"), "1h",
                                       min_history=2, expected_instrument="DEMO")
        assert any("instrument" in error for error in quality.errors)

    def test_validation_never_mutates_input(self):
        bars = window(4)
        snapshot = [b.to_dict() for b in bars]
        validate_market_data(bars, "1h", min_history=2)
        assert [b.to_dict() for b in bars] == snapshot


class TestReplayProvider:
    def test_replays_the_repository_fixture_deterministically(self):
        first = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        second = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        assert first.instruments("1h") == ["DEMO"]
        assert [b.to_dict() for b in first.fetch("DEMO", "1h").bars] == \
               [b.to_dict() for b in second.fetch("DEMO", "1h").bars]

    def test_replay_does_not_repair_recorded_data(self):
        provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        bars = provider.fetch("DEMO", "1h").bars
        quality = validate_market_data(bars, "1h", min_history=64)
        assert quality.ok, quality.errors
        assert len(bars) == 1600

    def test_end_is_an_exclusive_upper_bound(self):
        provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        bars = provider.fetch("DEMO", "1h").bars
        cut = bars[100].timestamp
        limited = provider.fetch("DEMO", "1h", end=cut).bars
        assert len(limited) == 100
        assert all(b.timestamp < cut for b in limited)

    def test_limit_returns_the_most_recent_bars(self):
        provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        limited = provider.fetch("DEMO", "1h", limit=50).bars
        assert len(limited) == 50
        assert limited[-1].timestamp == provider.fetch("DEMO", "1h").bars[-1].timestamp

    def test_unknown_series_raises_instead_of_inventing(self):
        provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        with pytest.raises(MarketDataError, match="no replayed series"):
            provider.fetch("NOPE", "1h")

    def test_declared_timeframe_mismatch_is_caught_by_validation(self):
        provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        bars = provider.fetch("DEMO", "1h").bars
        quality = validate_market_data(bars, "5m", min_history=10)
        assert quality.state is DataQualityState.DATA_ERROR
        assert any("wrong timeframe" in error for error in quality.errors)

    def test_daily_fixture_is_available_for_research(self):
        provider = ReplayMarketDataProvider.from_csv(DAILY_CSV, "1D")
        assert len(provider.fetch("DEMO", "1D").bars) == 1600

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(MarketDataError, match="not found"):
            ReplayMarketDataProvider.from_csv(tmp_path / "nope.csv", "1h")

    def test_malformed_csv_raises(self, tmp_path):
        path = tmp_path / "bad.csv"
        path.write_text("timestamp,open,high,low,close,volume,symbol\n"
                        "2024-01-01T00:00:00+00:00,1,2,0.5,not-a-number,10,DEMO\n")
        with pytest.raises(MarketDataError, match="malformed"):
            ReplayMarketDataProvider.from_csv(path, "1h")


class TestHttpProvider:
    def test_parses_a_json_rest_payload(self):
        payload = (
            b'{"candles": ['
            b'{"t": "2024-01-01T00:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10},'
            b'{"t": "2024-01-01T01:00:00Z", "o": 1.5, "h": 2, "l": 1, "c": 1.8, "v": 12}]}'
        )

        def transport(url, headers, timeout):
            assert "DEMO" in url
            return payload

        provider = HttpJsonMarketDataProvider(
            "https://example.invalid/candles?symbol={instrument}&tf={timeframe}",
            mapping={"timestamp": "t", "open": "o", "high": "h", "low": "l",
                     "close": "c", "volume": "v"},
            transport=transport,
        )
        result = provider.fetch("DEMO", "1h")
        assert len(result.bars) == 2
        assert result.bars[0].close == 1.5
        assert result.provider == "http-json"

    def test_rejects_timestamps_without_timezone(self):
        def transport(url, headers, timeout):
            return b'[{"timestamp": "2024-01-01T00:00:00", "open": 1, "high": 2,' \
                   b' "low": 0.5, "close": 1.5, "volume": 10}]'

        provider = HttpJsonMarketDataProvider(
            "https://example.invalid/{instrument}", transport=transport)
        with pytest.raises(MarketDataError, match="no timezone"):
            provider.fetch("DEMO", "1h")

    def test_rejects_malformed_payload(self):
        provider = HttpJsonMarketDataProvider(
            "https://example.invalid/{instrument}",
            transport=lambda url, headers, timeout: b"not json")
        with pytest.raises(MarketDataError, match="not valid JSON"):
            provider.fetch("DEMO", "1h")

    def test_epoch_millisecond_timestamps_are_utc(self):
        def transport(url, headers, timeout):
            return b'[[1704067200000, 1, 2, 0.5, 1.5, 10]]'

        provider = HttpJsonMarketDataProvider(
            "https://example.invalid/{instrument}", transport=transport)
        result = provider.fetch("DEMO", "1h")
        assert result.bars[0].timestamp == datetime(2024, 1, 1, tzinfo=UTC)


class TestJsonBoundaryStrictness:
    """A REST payload is untrusted input: nothing may be coerced into a price.

    ``float()`` alone turns ``true`` into ``1.0`` and accepts the non-JSON
    literals ``NaN``/``Infinity`` that ``json.loads`` lets through, so the edge
    needs its own coercion rules.
    """

    @staticmethod
    def _provider(payload: bytes) -> HttpJsonMarketDataProvider:
        return HttpJsonMarketDataProvider(
            "https://example.invalid/{instrument}",
            transport=lambda url, headers, timeout: payload)

    def test_a_boolean_price_is_refused_not_read_as_one(self):
        payload = (
            b'[{"timestamp": "2024-01-01T00:00:00Z", "open": 1, "high": 2,'
            b' "low": 0.5, "close": true, "volume": 10}]'
        )
        with pytest.raises(MarketDataError, match="close is a boolean"):
            self._provider(payload).fetch("DEMO", "1h")

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_json_literals_are_refused(self, literal):
        # json.loads accepts these even though they are not valid JSON.
        payload = (
            b'[{"timestamp": "2024-01-01T00:00:00Z", "open": 1, "high": 2,'
            b' "low": 0.5, "close": ' + literal.encode() + b', "volume": 10}]'
        )
        with pytest.raises(MarketDataError, match="close is not finite"):
            self._provider(payload).fetch("DEMO", "1h")

    @pytest.mark.parametrize("text", ["nan", "inf", "Infinity", ""])
    def test_non_finite_strings_are_refused(self, text):
        payload = (
            b'[{"timestamp": "2024-01-01T00:00:00Z", "open": 1, "high": 2,'
            b' "low": 0.5, "close": "' + text.encode() + b'", "volume": 10}]'
        )
        with pytest.raises(MarketDataError, match="close is not a number"):
            self._provider(payload).fetch("DEMO", "1h")

    def test_a_nested_object_price_is_refused(self):
        payload = (
            b'[{"timestamp": "2024-01-01T00:00:00Z", "open": 1, "high": 2,'
            b' "low": 0.5, "close": {"value": 1.5}, "volume": 10}]'
        )
        with pytest.raises(MarketDataError, match="close has unsupported type dict"):
            self._provider(payload).fetch("DEMO", "1h")

    def test_numeric_strings_are_still_accepted(self):
        payload = (
            b'[{"timestamp": "2024-01-01T00:00:00Z", "open": "1", "high": "2",'
            b' "low": "0.5", "close": "1.5", "volume": "10"}]'
        )
        result = self._provider(payload).fetch("DEMO", "1h")
        assert result.bars[0].close == 1.5
        assert result.bars[0].volume == 10.0

    def test_array_rows_get_the_same_treatment(self):
        payload = b'[[1704067200000, 1, 2, 0.5, true, 10]]'
        with pytest.raises(MarketDataError, match="close is a boolean"):
            self._provider(payload).fetch("DEMO", "1h")

    def test_parse_number_field_unit_contract(self):
        from axiom.market.http import parse_number_field

        assert parse_number_field(1.5, "close") == 1.5
        assert parse_number_field(2, "close") == 2.0
        assert parse_number_field(" 1.25 ", "close") == 1.25
        for bad in (True, False, None, [], {}, float("nan"), float("inf"), "abc", ""):
            with pytest.raises(MarketDataError):
                parse_number_field(bad, "close")


class TestCalendars:
    def test_continuous_calendar_projects_every_slot(self):
        calendar = ContinuousCalendar()
        last = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
        stamps = calendar.project_future_timestamps(last, "1h", 3)
        assert stamps == [last + timedelta(hours=n) for n in (1, 2, 3)]

    def test_daily_calendar_skips_weekends_without_fabricating(self):
        calendar = DailyCalendar("weekdays", weekdays=range(5))
        friday = datetime(2024, 1, 5, tzinfo=UTC)
        stamps = calendar.project_future_timestamps(friday, "1D", 2)
        assert stamps == [datetime(2024, 1, 8, tzinfo=UTC),
                          datetime(2024, 1, 9, tzinfo=UTC)]
        assert all(stamp.weekday() < 5 for stamp in stamps)

    def test_session_calendar_never_places_bars_outside_the_session(self):
        calendar = SessionCalendar(
            "nse", open_time=time(9, 15), close_time=time(15, 30), weekdays=range(5))
        last = datetime(2024, 1, 1, 15, 0, tzinfo=UTC)  # Monday, last bar of session
        stamps = calendar.project_future_timestamps(last, "1h", 3)
        assert all(time(9, 15) <= stamp.time() < time(15, 30) for stamp in stamps)
        assert all(stamp.weekday() < 5 for stamp in stamps)
        assert stamps[0] > last

    def test_projection_raises_rather_than_padding(self):
        calendar = DailyCalendar("never", weekdays=())
        with pytest.raises(MarketDataError, match="no valid 1D slot"):
            calendar.project_future_timestamps(datetime(2024, 1, 1, tzinfo=UTC), "1D", 3)


class TestCalendarTimezones:
    """Timestamp handling across DST and non-UTC sessions.

    Regression coverage for a real defect: projecting daily session opens
    stepped by an absolute 86400s, so crossing a DST transition moved 09:30 EST
    to 10:30 EDT. The local-time slot check then matched nothing until the
    offset reverted, and the scan returned a stamp 241 days out — plausible
    looking, and accepted downstream because validate_forecast only checks that
    timestamps strictly increase.
    """

    @staticmethod
    def eastern():
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")

    def test_daily_session_opens_survive_a_dst_transition(self):
        calendar = SessionCalendar(
            "nyse", open_time=time(9, 30), close_time=time(16, 0),
            weekdays=range(5), timezone=self.eastern())
        eastern = self.eastern()
        # Thu 2024-03-07 09:30 EST; the US DST switch lands on Sun 2024-03-10.
        last = datetime(2024, 3, 7, 14, 30, tzinfo=UTC)
        stamps = calendar.project_future_timestamps(last, "1D", 3)

        locals_ = [stamp.astimezone(eastern) for stamp in stamps]
        assert [s.strftime("%a") for s in locals_] == ["Fri", "Mon", "Tue"]
        # Every projection is a genuine session open in LOCAL time, on both
        # sides of the transition (EST before, EDT after).
        assert all(s.time() == time(9, 30) for s in locals_)
        assert [s.utcoffset().total_seconds() / 3600 for s in locals_] == [-5, -4, -4]
        # And they are consecutive trading days, not months away.
        assert (stamps[-1] - last).days <= 7

    def test_intraday_session_projection_survives_a_dst_transition(self):
        calendar = SessionCalendar(
            "nyse", open_time=time(9, 30), close_time=time(16, 0),
            weekdays=range(5), timezone=self.eastern())
        eastern = self.eastern()
        last = datetime(2024, 3, 8, 20, 30, tzinfo=UTC)  # Fri 15:30 EST
        stamps = calendar.project_future_timestamps(last, "1h", 3)
        for stamp in stamps:
            local = stamp.astimezone(eastern)
            assert time(9, 30) <= local.time() < time(16, 0)
            assert local.weekday() < 5

    def test_a_drifted_projection_fails_loudly_instead_of_returning_stamps(self):
        """The gap guard is the backstop if a calendar ever drifts again."""
        from axiom.market.calendar import MAX_GAP_DAYS, TradingCalendar

        class Drifting(DailyCalendar):
            def advance(self, timestamp, timeframe, *, direction=1):
                # Simulate the old defect: jump a year on every step.
                return super().advance(timestamp, timeframe, direction=direction) \
                    + timedelta(days=365)

        calendar = Drifting("drifting", weekdays=range(5))
        with pytest.raises(MarketDataError, match="refusing to emit implausible"):
            calendar.project_future_timestamps(datetime(2024, 1, 1, tzinfo=UTC), "1D", 3)
        assert calendar.max_gap("1D") == timedelta(days=MAX_GAP_DAYS)
        assert isinstance(calendar, TradingCalendar)

    def test_a_continuous_calendar_gap_scales_with_the_timeframe(self):
        """24/7 markets have no closed periods, so the bound is step-relative."""
        calendar = ContinuousCalendar()
        assert calendar.max_gap("1h") == timedelta(hours=10)
        assert calendar.max_gap("1m") == timedelta(minutes=10)

    def test_a_long_holiday_break_is_still_allowed(self):
        """The guard must not reject a legitimate run of closed days."""
        calendar = DailyCalendar(
            "holidays", weekdays=range(5),
            holidays=[datetime(2024, 1, 2 + n, tzinfo=UTC) for n in range(8)])
        stamps = calendar.project_future_timestamps(datetime(2024, 1, 1, tzinfo=UTC), "1D", 2)
        assert len(stamps) == 2
        assert all(stamp.weekday() < 5 for stamp in stamps)

    def test_the_same_instant_gets_the_same_verdict_regardless_of_offset(self):
        """A trading day must not depend on the offset a caller happened to use."""
        from zoneinfo import ZoneInfo

        calendar = DailyCalendar("weekdays", weekdays=range(5))
        moment_utc = datetime(2024, 1, 5, 23, 30, tzinfo=UTC)          # Friday
        moment_ist = moment_utc.astimezone(ZoneInfo("Asia/Kolkata"))   # Saturday 05:00
        assert calendar.is_valid_bar_time(moment_utc, "1D") is True
        assert calendar.is_valid_bar_time(moment_ist, "1D") is True, (
            "same instant, different verdict: the calendar must normalise to UTC"
        )

    @pytest.mark.parametrize("calendar_factory", [
        lambda: ContinuousCalendar(),
        lambda: DailyCalendar("weekdays"),
        lambda: SessionCalendar("s", open_time=time(9, 30), close_time=time(16, 0)),
    ])
    def test_naive_timestamps_are_refused_not_reinterpreted(self, calendar_factory):
        """A naive stamp would mean different things on different machines."""
        calendar = calendar_factory()
        timeframe = "1h" if isinstance(calendar, ContinuousCalendar) else "1D"
        naive = datetime(2024, 1, 1, 1, 0)
        with pytest.raises(MarketDataError, match="no timezone"):
            calendar.is_valid_bar_time(naive, timeframe)
        with pytest.raises(MarketDataError, match="no timezone"):
            calendar.next_valid(naive, timeframe)
        with pytest.raises(MarketDataError, match="no timezone"):
            calendar.project_future_timestamps(naive, timeframe, 3)


class TestClosedCandleGate:
    def test_incomplete_bar_is_excluded(self):
        bars = window(5)
        now = bars[-1].timestamp + timedelta(minutes=30)  # last bar still forming
        gate = ClosedCandleGate("1h")
        split = gate.apply(bars, now)
        assert len(split.closed) == 4
        assert len(split.incomplete) == 1
        assert split.last_closed_timestamp == bars[3].timestamp

    def test_last_closed_open_time_respects_the_calendar(self):
        gate = ClosedCandleGate("1D", DailyCalendar("weekdays", weekdays=range(5)))
        saturday = datetime(2024, 1, 6, 12, 0, tzinfo=UTC)
        assert gate.last_closed_open_time(saturday) == datetime(2024, 1, 5, tzinfo=UTC)

    def test_window_changed_only_on_a_new_closed_candle(self):
        gate = ClosedCandleGate("1h")
        first = datetime(2024, 1, 1, 3, tzinfo=UTC)
        assert gate.window_changed(None, first) is True
        assert gate.window_changed(first, first) is False
        assert gate.window_changed(first, first + timedelta(hours=1)) is True
