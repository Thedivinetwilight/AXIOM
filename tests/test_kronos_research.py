"""Walk-forward research: baselines, metrics, and the no-leakage guarantee.

Forecast quality only — nothing here touches P&L, fees or positions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from axiom.market.provider import MarketDataBar
from axiom.market.replay import ReplayMarketDataProvider
from axiom.models import KronosConfig
from axiom.research.baselines import (
    BASELINES,
    baseline_names,
    drift_forecast,
    last_close_forecast,
    moving_average_forecast,
    run_baseline,
    seasonal_naive_forecast,
)
from axiom.research.metrics import (
    aggregate,
    compute_metrics,
    directional_accuracy,
    horizon_return_error,
    mean_absolute_error,
    mean_absolute_percentage_error,
    range_error,
    root_mean_squared_error,
)
from axiom.research.walkforward import WalkForwardEvaluator, runtime_forecaster

UTC = timezone.utc
DAILY_CSV = "data/sample.csv"


def series(closes, *, start=None, instrument="DEMO"):
    origin = start or datetime(2024, 1, 1, tzinfo=UTC)
    bars = []
    previous = closes[0]
    for index, close in enumerate(closes):
        high = max(previous, close) * 1.002
        low = min(previous, close) * 0.998
        bars.append(MarketDataBar(origin + timedelta(days=index), previous, high, low,
                                  close, 1000.0, instrument))
        previous = close
    return bars


def fixture_bars(count: int = 200):
    provider = ReplayMarketDataProvider.from_csv(DAILY_CSV, "1D")
    return list(provider.fetch("DEMO", "1D").bars)[:count]


class TestBaselines:
    def test_last_close_is_flat(self):
        bars = series([100.0, 101.0, 102.0])
        assert last_close_forecast(bars, 4) == [102.0] * 4

    def test_drift_extrapolates_the_average_change(self):
        bars = series([100.0, 101.0, 102.0, 103.0])
        assert drift_forecast(bars, 2) == [104.0, 105.0]

    def test_moving_average_reverts_to_the_mean(self):
        bars = series([100.0] * 10 + [120.0])
        forecast = moving_average_forecast(bars, 5, window=10)
        assert forecast[0] < 120.0
        assert forecast[-1] < forecast[0], "converges toward the trailing mean"

    def test_seasonal_naive_repeats_the_last_season(self):
        closes = [100.0 + index for index in range(48)]
        bars = series(closes)
        forecast = seasonal_naive_forecast(bars, 30, season=24)
        assert forecast[0] == closes[-24]
        assert len(forecast) == 30

    def test_registry(self):
        assert baseline_names() == ["drift", "last_close", "moving_average"]
        assert set(BASELINES) == {"last_close", "drift", "moving_average"}
        with pytest.raises(ValueError, match="unknown baseline"):
            run_baseline("oracle", series([1.0, 2.0]), 2)

    def test_baselines_never_see_the_future(self):
        bars = series([100.0, 101.0, 99.0, 105.0])
        for name in baseline_names():
            truncated = run_baseline(name, bars[:3], 3)
            again = run_baseline(name, bars[:3], 3)
            assert truncated == again
            assert len(truncated) == 3


class TestMetrics:
    def test_point_metrics(self):
        predicted = [101.0, 102.0, 103.0]
        actual = [100.0, 103.0, 102.0]
        metrics = compute_metrics(predicted, actual, last_actual=100.0)
        assert metrics.mae == pytest.approx(mean_absolute_error(predicted, actual))
        assert metrics.rmse == pytest.approx(
            root_mean_squared_error(predicted, actual))
        assert metrics.mape_pct == pytest.approx(
            mean_absolute_percentage_error(predicted, actual))
        assert metrics.n == 3

    def test_directional_accuracy(self):
        # last actual 100: predicted up,up,down vs actual up,down,down → 2/3
        assert directional_accuracy([101.0, 101.0, 99.0], [102.0, 99.0, 98.0],
                                    100.0) == pytest.approx(2 / 3)

    def test_horizon_return_error(self):
        error = horizon_return_error([110.0], [105.0], 100.0)
        assert error == pytest.approx(0.10 - 0.05)

    def test_range_error(self):
        assert range_error([105.0], [95.0], [104.0], [96.0]) == pytest.approx(2.0)

    def test_mape_is_undefined_for_zero_actuals(self):
        assert mean_absolute_percentage_error([1.0], [0.0]) is None

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="length mismatch"):
            compute_metrics([1.0, 2.0], [1.0])

    def test_aggregate_macro_averages(self):
        first = compute_metrics([1.0], [2.0], last_actual=1.0)
        second = compute_metrics([3.0], [2.0], last_actual=1.0)
        combined = aggregate([first, second])
        assert combined.n == 2
        assert combined.mae == pytest.approx((first.mae + second.mae) / 2)


class TestWalkForward:
    def test_kronos_is_compared_against_all_baselines(self):
        bars = fixture_bars(200)
        config = KronosConfig(enabled=True, backend="stub", lookback=64, pred_len=5,
                              min_history=32, sample_paths=3)

        def fake_kronos(window, future):
            # Three paths around the last close, deterministic for the test.
            last = window[-1].close
            return [
                [last * (1 + 0.001 * step) for step in range(len(future))],
                [last * (1 + 0.002 * step) for step in range(len(future))],
                [last * (1 - 0.001 * step) for step in range(len(future))],
            ]

        report = WalkForwardEvaluator(
            bars, config, instrument="DEMO", timeframe="1D",
            kronos_forecaster=fake_kronos, stride=25,
        ).run()
        assert report.leakage_ok is True
        assert report.folds > 1
        assert set(report.models) == {"kronos", "last_close", "drift", "moving_average"}
        kronos = report.models["kronos"]
        assert kronos.mae >= 0 and kronos.rmse >= 0
        # Uncertainty metrics exist only for the sampled model.
        assert kronos.coverage is not None
        assert kronos.dispersion is not None
        assert report.models["last_close"].coverage is None

    def test_every_fold_is_checked_for_leakage(self):
        bars = fixture_bars(160)
        config = KronosConfig(enabled=True, backend="stub", lookback=48, pred_len=4,
                              min_history=24)
        report = WalkForwardEvaluator(
            bars, config, timeframe="1D",
            kronos_forecaster=lambda window, future: [[window[-1].close] * len(future)],
            stride=20,
        ).run()
        assert report.leakage_ok
        for step in report.steps:
            assert step.leakage_ok
            assert step.origin_timestamp < step.future_timestamps[0]
            assert len(step.actual) == 4

    def test_the_forecaster_only_receives_pre_origin_data(self):
        """A canary forecaster proves no future bar reaches the model."""
        bars = fixture_bars(140)
        config = KronosConfig(enabled=True, backend="stub", lookback=48, pred_len=4,
                              min_history=24)
        seen: list[datetime] = []

        def canary(window, future):
            seen.append(window[-1].timestamp)
            assert window[-1].timestamp < future[0]
            return [[window[-1].close] * len(future)]

        report = WalkForwardEvaluator(bars, config, timeframe="1D",
                                      kronos_forecaster=canary, stride=30).run()
        assert report.leakage_ok
        assert seen, "the forecaster was never called"
        assert seen == sorted(seen)

    def test_insufficient_history_is_reported(self):
        bars = fixture_bars(20)
        config = KronosConfig(enabled=True, backend="stub", lookback=16, pred_len=10,
                              min_history=16)
        report = WalkForwardEvaluator(bars, config, timeframe="1D").run()
        assert report.folds == 0
        assert any("not enough history" in note for note in report.notes)

    def test_baselines_only_when_no_model_is_supplied(self):
        bars = fixture_bars(120)
        config = KronosConfig(enabled=True, backend="stub", lookback=32, pred_len=4,
                              min_history=16)
        report = WalkForwardEvaluator(bars, config, timeframe="1D", stride=30).run()
        assert set(report.models) == {"drift", "last_close", "moving_average"}
        assert any("no Kronos forecaster" in note for note in report.notes)

    def test_unknown_baseline_is_rejected(self):
        config = KronosConfig(enabled=True, backend="stub", lookback=16, pred_len=2,
                              min_history=8)
        with pytest.raises(ValueError, match="unknown baseline"):
            WalkForwardEvaluator(fixture_bars(60), config, baselines=("oracle",))

    def test_report_serialises(self):
        bars = fixture_bars(120)
        config = KronosConfig(enabled=True, backend="stub", lookback=32, pred_len=4,
                              min_history=16)
        report = WalkForwardEvaluator(
            bars, config, instrument="DEMO", timeframe="1D", stride=40,
            kronos_forecaster=lambda w, f: [[w[-1].close] * len(f)] * 3,
        ).run()
        payload = report.to_dict()
        assert payload["instrument"] == "DEMO"
        assert payload["leakage_ok"] is True
        assert payload["models"]["kronos"]["mae"] >= 0
        assert payload["steps"][0]["metrics"]["kronos"]["n"] == 4

    def test_runtime_forecaster_adapter(self):
        class FakeRunner:
            def forecast_paths(self, bars, future):
                from axiom.kronos.backends import KronosInference

                path = tuple({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.0,
                              "volume": 1.0} for _ in future)
                return KronosInference((path, path), 1.0, "stub")

        config = KronosConfig(enabled=True, backend="stub", lookback=32, pred_len=3,
                              min_history=16, sample_paths=2)
        forecaster = runtime_forecaster(FakeRunner(), config)
        paths = forecaster(fixture_bars(40), [datetime(2024, 1, 1, tzinfo=UTC)] * 3)
        assert len(paths) == 2
        assert all(len(path) == 3 for path in paths)
