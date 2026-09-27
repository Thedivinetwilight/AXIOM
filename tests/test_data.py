from dataclasses import replace
import pytest
from app.data.cache import DataCache
from app.data.historical import CSVProvider, write_csv
from app.data.models import parse_timestamp
from app.data.validator import clean_rows, validate
from app.paper_trading.execution import SessionCalendar
from tests.conftest import candle


@pytest.mark.parametrize(
    "field,value",
    [
        ("open", 0),
        ("low", -1),
        ("high", 90),
        ("low", 110),
        ("volume", -1),
        ("close", float("nan")),
        ("open", float("inf")),
        ("symbol", "../X"),
    ],
)
def test_corruption(field, value):
    report = validate([replace(candle(), **{field: value})], "1D")
    assert report.errors
    with pytest.raises(ValueError):
        report.require_valid()


def test_timestamp_rules():
    with pytest.raises(ValueError):
        parse_timestamp("2024-01-01")
    assert parse_timestamp("2024-01-01T05:30:00+05:30").hour == 0
    assert validate([candle(), candle()], "1D").errors
    assert validate([candle(1), candle(0)], "1D").errors
    assert validate([], "1D").errors
    assert validate([candle()], "2m").errors
    assert validate([candle(), candle(3)], "1D").warnings
    assert validate([candle(), candle(3)], "1D", strict_gaps=True).errors
    assert validate(
        [replace(candle(), timestamp=candle().timestamp.replace(tzinfo=None))], "1D"
    ).errors


def test_cleaning_is_explicit(tmp_path):
    rows = [candle(1).to_dict(), candle().to_dict(), candle().to_dict()]
    bars, notes = clean_rows(rows)
    assert len(bars) == 2 and len(notes) == 2
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        clean_rows([candle().to_dict(), candle(price=101).to_dict()])
    path = tmp_path / "input.csv"
    write_csv(path, [candle(1), candle()])
    with pytest.raises(ValueError, match="Unsorted"):
        CSVProvider(path, "1D").historical("DEMO", "1D")
    assert len(CSVProvider(path, "1D", clean=True).historical("DEMO", "1D")) == 2


def test_cache_content_invalidation(tmp_path):
    path, cache = tmp_path / "input.csv", DataCache(tmp_path / "cache")
    write_csv(path, [candle(), candle(1)])
    provider = CSVProvider(path, "1D", cache)
    assert provider.historical("DEMO", "1D") == provider.historical("DEMO", "1D")
    assert len(list((tmp_path / "cache").glob("*.csv"))) == 1
    write_csv(path, [candle(), candle(1, 102)])
    assert provider.historical("DEMO", "1D")[-1].close == 102
    assert len(list((tmp_path / "cache").glob("*.csv"))) == 2
    with pytest.raises(ValueError):
        provider.historical("DEMO", "1m")
    with pytest.raises(ValueError):
        provider.historical("OTHER", "1D")


def test_missing_field_fails(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("timestamp,close,symbol\n2024-01-01T00:00:00Z,100,DEMO\n")
    with pytest.raises(ValueError, match="Missing/invalid"):
        CSVProvider(path, "1D").historical("DEMO", "1D")


def test_calendar_gap():
    friday = parse_timestamp("2024-01-05T15:25:00+05:30")
    monday = parse_timestamp("2024-01-08T09:15:00+05:30")
    bars = [replace(candle(), timestamp=friday), replace(candle(), timestamp=monday)]
    assert not validate(bars, "5m", strict_gaps=True, calendar=SessionCalendar()).errors
    assert validate(bars, "5m", strict_gaps=True).errors
