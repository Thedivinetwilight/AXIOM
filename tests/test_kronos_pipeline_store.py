"""Forecast pipeline + ForecastStore: gating, cache correctness, degradation.

Everything here runs on the repository's deterministic replay fixture with the
labelled stub backend, so no model download and no network are involved.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from axiom.forecast.pipeline import CONTEXT_TAIL, ForecastPipeline
from axiom.forecast.store import ForecastStore
from axiom.kronos.backends import KronosInference, KronosRuntimeUnavailable
from axiom.kronos.runtime import KronosRuntime
from axiom.market.calendar import ContinuousCalendar, DailyCalendar
from axiom.market.provider import MarketDataBar, MarketDataError, MarketDataResult
from axiom.market.replay import ReplayMarketDataProvider
from axiom.models import (
    FORECAST_ORIGIN,
    DataQualityState,
    KronosConfig,
    KronosState,
)

UTC = timezone.utc
SAMPLE_CSV = "data/sample_1h.csv"
DAILY_CSV = "data/sample.csv"


def config(**overrides) -> KronosConfig:
    base = dict(
        enabled=True,
        backend="stub",
        lookback=200,
        pred_len=12,
        max_context=512,
        min_history=64,
        sample_paths=3,
    )
    base.update(overrides)
    return KronosConfig(**base).validate()


class CountingRuntime(KronosRuntime):
    """Stub runtime that counts inference calls (cache-correctness checks)."""

    def __init__(self, cfg: KronosConfig):
        super().__init__(cfg)
        self.calls = 0

    def forecast_paths(self, bars, future_timestamps):
        self.calls += 1
        return super().forecast_paths(bars, future_timestamps)


def make_pipeline(tmp_clock: datetime, *, cfg: KronosConfig | None = None,
                  csv: str = SAMPLE_CSV, timeframe: str = "1h",
                  calendar=None, runtime=None, store=None, provider=None):
    cfg = cfg or config()
    provider = provider or ReplayMarketDataProvider.from_csv(csv, timeframe)
    runtime = runtime or CountingRuntime(cfg)
    runtime.load()
    pipeline = ForecastPipeline(
        provider, cfg, calendar=calendar or ContinuousCalendar(),
        store=store or ForecastStore(), runtime=runtime, clock=lambda: tmp_clock,
    )
    return pipeline, runtime


def fixture(timeframe: str = "1h", csv: str = SAMPLE_CSV, upto: int | None = None):
    """(provider, all recorded bars, bars visible so far).

    Truncating simulates "history recorded up to now"; later tests can reveal
    more RECORDED bars to make the closed-candle window genuinely change.
    """
    provider = ReplayMarketDataProvider.from_csv(csv, timeframe)
    all_bars = list(provider.fetch("DEMO", timeframe).bars)
    visible = all_bars if upto is None else all_bars[:upto]
    if upto is not None:
        provider.add("DEMO", timeframe, visible)
    return provider, all_bars, visible


def closed_now(bars, timeframe: str = "1h") -> datetime:
    """A moment just after the last recorded bar has closed."""
    step = {"1h": timedelta(hours=1), "1D": timedelta(days=1)}[timeframe]
    return bars[-1].timestamp + step


def now_for(csv: str = SAMPLE_CSV, timeframe: str = "1h",
            extra: timedelta = timedelta(hours=1)) -> datetime:
    provider = ReplayMarketDataProvider.from_csv(csv, timeframe)
    bars = provider.fetch("DEMO", timeframe).bars
    step = {"1h": timedelta(hours=1), "1D": timedelta(days=1)}[timeframe]
    return bars[-1].timestamp + step


class TestPipelineHappyPath:
    def test_pipeline_rejects_a_runtime_with_different_configuration(self):
        cfg = config()
        mismatched_runtime = KronosRuntime(config(temperature=0.5))
        provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        with pytest.raises(ValueError, match="differs from its Kronos runtime"):
            ForecastPipeline(provider, cfg, runtime=mismatched_runtime)

    def test_generates_a_labelled_forecast_from_closed_bars(self):
        moment = now_for()
        pipeline, runtime = make_pipeline(moment)
        outcome = pipeline.run("DEMO", "1h")
        assert outcome.state is KronosState.READY, outcome.message
        snapshot = outcome.snapshot
        assert snapshot is not None
        assert snapshot.origin == FORECAST_ORIGIN
        assert len(snapshot.candles) == snapshot.config.pred_len
        assert snapshot.last_closed_timestamp is not None
        # Every forecast timestamp is AFTER the last closed candle.
        assert all(c.timestamp > snapshot.last_closed_timestamp for c in snapshot.candles)
        assert snapshot.forecast_endpoint == snapshot.candles[-1].timestamp
        assert snapshot.last_actual_close == pytest.approx(
            pipeline.provider.fetch("DEMO", "1h").bars[-1].close)
        assert snapshot.provenance.source.startswith("replay:")
        assert snapshot.provenance.bars == snapshot.config.lookback
        assert snapshot.data_quality.ok
        assert snapshot.context_closes and len(snapshot.context_closes) <= CONTEXT_TAIL
        assert "not observed market data" in snapshot.note

    def test_forecast_timestamps_follow_the_declared_timeframe(self):
        moment = now_for()
        pipeline, _ = make_pipeline(moment)
        snapshot = pipeline.run("DEMO", "1h").snapshot
        deltas = {
            (b.timestamp - a.timestamp).total_seconds()
            for a, b in zip(snapshot.candles, snapshot.candles[1:])
        }
        assert deltas == {3600.0}

    def test_daily_series_uses_the_trading_calendar(self):
        moment = now_for(DAILY_CSV, "1D", timedelta(days=1))
        pipeline, _ = make_pipeline(
            moment, csv=DAILY_CSV, timeframe="1D",
            calendar=DailyCalendar("weekdays", weekdays=range(5)))
        snapshot = pipeline.run("DEMO", "1D").snapshot
        assert all(stamp.weekday() < 5 for stamp in
                   [c.timestamp for c in snapshot.candles])

    def test_uncertainty_present_only_with_enough_sampled_paths(self):
        moment = now_for()
        pipeline, _ = make_pipeline(moment, cfg=config(sample_paths=4))
        snapshot = pipeline.run("DEMO", "1h").snapshot
        assert snapshot.uncertainty is not None
        assert snapshot.uncertainty.supported
        assert snapshot.uncertainty.calibrated is False

        single, _ = make_pipeline(moment, cfg=config(sample_paths=1))
        assert single.run("DEMO", "1h").snapshot.uncertainty is None

    def test_snapshot_serialisation_keeps_provenance(self):
        moment = now_for()
        pipeline, _ = make_pipeline(moment)
        payload = pipeline.run("DEMO", "1h").snapshot.to_dict()
        assert payload["origin"] == FORECAST_ORIGIN
        assert payload["provenance"]["input_hash"]
        assert payload["data_quality"]["state"] == "OK"
        assert payload["candles"][0]["forecast_close"] is not None
        assert "close" not in payload["candles"][0], "no unlabelled price fields"


class TestCacheCorrectness:
    def test_unchanged_window_is_served_from_cache(self):
        moment = now_for()
        pipeline, runtime = make_pipeline(moment)
        first = pipeline.run("DEMO", "1h")
        assert first.cache_hit is False
        second = pipeline.run("DEMO", "1h")
        assert second.cache_hit is True
        assert runtime.calls == 1, "no new inference for an unchanged window"
        assert second.snapshot.cache_key == first.snapshot.cache_key

    def test_new_closed_candle_triggers_a_rerun(self):
        provider, all_bars, visible = fixture(upto=300)
        moment = closed_now(visible)
        pipeline, runtime = make_pipeline(moment, provider=provider)
        first = pipeline.run("DEMO", "1h")
        assert first.cache_hit is False

        # One more recorded bar arrives and closes: the window really changed.
        provider.add("DEMO", "1h", all_bars[:301])
        pipeline.clock = lambda: closed_now(all_bars[:301])
        second = pipeline.run("DEMO", "1h")
        assert second.cache_hit is False
        assert runtime.calls == 2
        assert second.snapshot.last_closed_timestamp == all_bars[300].timestamp
        assert second.snapshot.last_closed_timestamp == (
            first.snapshot.last_closed_timestamp + timedelta(hours=1))

    def test_changed_input_values_invalidate_the_cache(self):
        moment = now_for()
        provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        pipeline, runtime = make_pipeline(moment, provider=provider)
        pipeline.run("DEMO", "1h")

        # Same last closed timestamp, revised close on the last bar.
        bars = list(provider.fetch("DEMO", "1h").bars)
        revised = MarketDataBar(bars[-1].timestamp, bars[-1].open, bars[-1].high + 1.0,
                                bars[-1].low, bars[-1].close + 0.5, bars[-1].volume,
                                "DEMO")
        provider.add("DEMO", "1h", bars[:-1] + [revised])
        outcome = pipeline.run("DEMO", "1h")
        assert outcome.cache_hit is False
        assert runtime.calls == 2

    def test_config_change_invalidates_the_cache(self):
        moment = now_for()
        store = ForecastStore()
        pipeline, runtime = make_pipeline(moment, cfg=config(pred_len=12), store=store)
        pipeline.run("DEMO", "1h")

        other = config(pred_len=24)
        runtime2 = CountingRuntime(other)
        runtime2.load()
        second = ForecastPipeline(
            pipeline.provider, other, calendar=ContinuousCalendar(), store=store,
            runtime=runtime2, clock=lambda: moment)
        outcome = second.run("DEMO", "1h")
        assert outcome.cache_hit is False
        assert runtime2.calls == 1

    def test_force_bypasses_the_cache(self):
        moment = now_for()
        pipeline, runtime = make_pipeline(moment)
        pipeline.run("DEMO", "1h")
        forced = pipeline.run("DEMO", "1h", force=True)
        assert forced.cache_hit is False
        assert runtime.calls == 2


class TestModelIdentityInCacheKey:
    """The cache must separate forecasts produced by different weights.

    In process mode the pipeline holds no runtime handle, so the resolved
    revision arrives through the ``model_identity`` accessor. Without it the
    key would fall back to the (usually empty) configured revision and a model
    upgrade would keep serving forecasts from the old weights.
    """

    @staticmethod
    def _infer(cfg: KronosConfig, revision: dict):
        """An infer callable that reports its own identity, like the real child.

        ``revision`` is read at call time so a test can swap weights between
        runs and check that the cache reacts.
        """
        runtime = CountingRuntime(cfg)
        runtime.load()

        def infer(bars, future_timestamps):
            result = runtime.forecast_paths(bars, future_timestamps)
            return KronosInference(
                paths=result.paths,
                latency_ms=result.latency_ms,
                backend=result.backend,
                notes=result.notes,
                model_id="NeoQuasar/Kronos-small",
                model_revision=revision["value"],
            )

        return infer, runtime

    def test_model_revision_change_invalidates_the_cache(self):
        moment = now_for()
        cfg = config()
        revision = {"value": "abc123"}
        infer, runtime = self._infer(cfg, revision)
        provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        store = ForecastStore()
        pipeline = ForecastPipeline(
            provider, cfg, calendar=ContinuousCalendar(), store=store, infer=infer,
            model_identity=lambda: ("NeoQuasar/Kronos-small", revision["value"]),
            clock=lambda: moment)

        first = pipeline.run("DEMO", "1h")
        assert first.cache_hit is False
        assert first.snapshot.model_revision == "abc123"
        first_key = first.snapshot.cache_key

        # Same window, same config: only the weights changed.
        revision["value"] = "def456"
        second = pipeline.run("DEMO", "1h")
        assert second.cache_hit is False, "a new revision must not reuse the old forecast"
        assert runtime.calls == 2
        assert second.snapshot.cache_key != first_key
        assert second.snapshot.model_revision == "def456"

        # And it is stable again for the unchanged revision.
        third = pipeline.run("DEMO", "1h")
        assert third.cache_hit is True
        assert runtime.calls == 2

    def test_unset_revision_stays_honest_and_separates_from_a_pinned_one(self):
        moment = now_for()
        cfg = config()
        revision = {"value": ""}
        infer, _ = self._infer(cfg, revision)
        store = ForecastStore()
        provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        identity = {"value": ("", "")}
        pipeline = ForecastPipeline(
            provider, cfg, calendar=ContinuousCalendar(), store=store, infer=infer,
            model_identity=lambda: identity["value"], clock=lambda: moment)

        unknown = pipeline.run("DEMO", "1h")
        assert unknown.snapshot.model_revision == "", "never invent a revision"
        identity["value"] = ("NeoQuasar/Kronos-small", "pinned")
        revision["value"] = "pinned"
        pinned = pipeline.run("DEMO", "1h")
        assert pinned.cache_hit is False
        assert pinned.snapshot.cache_key != unknown.snapshot.cache_key

    def test_cache_hit_reuses_the_identity_available_at_hit_time(self):
        """On a hit there is no inference, so the accessor is the only source."""
        moment = now_for()
        cfg = config()
        revision = {"value": "abc123"}
        infer, runtime = self._infer(cfg, revision)
        pipeline = ForecastPipeline(
            ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h"), cfg,
            calendar=ContinuousCalendar(), infer=infer,
            model_identity=lambda: ("NeoQuasar/Kronos-small", revision["value"]),
            clock=lambda: moment)
        pipeline.run("DEMO", "1h")
        hit = pipeline.run("DEMO", "1h")
        assert hit.cache_hit is True
        assert runtime.calls == 1
        assert hit.snapshot.model_revision == "abc123"

    def test_a_broken_identity_accessor_does_not_break_forecasting(self):
        moment = now_for()
        cfg = config()
        infer, runtime = self._infer(cfg, {"value": "abc123"})

        def explode():
            raise RuntimeError("worker not started")

        pipeline = ForecastPipeline(
            ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h"), cfg,
            calendar=ContinuousCalendar(), infer=infer, model_identity=explode,
            clock=lambda: moment)
        outcome = pipeline.run("DEMO", "1h")
        assert outcome.state is KronosState.READY, outcome.message
        assert runtime.calls == 1

    def test_inference_reported_revision_is_recorded_on_the_snapshot(self):
        """The child reports what it actually loaded; that wins for the record."""
        moment = now_for()
        cfg = config()
        runtime = CountingRuntime(cfg)
        runtime.load()

        def infer(bars, future_timestamps):
            result = runtime.forecast_paths(bars, future_timestamps)
            return KronosInference(
                paths=result.paths, latency_ms=result.latency_ms,
                backend=result.backend, notes=result.notes,
                model_id="NeoQuasar/Kronos-small", model_revision="901c26c")

        pipeline = ForecastPipeline(
            ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h"), cfg,
            calendar=ContinuousCalendar(), infer=infer,
            model_identity=lambda: ("NeoQuasar/Kronos-small", "stale-identity"),
            clock=lambda: moment)
        snapshot = pipeline.run("DEMO", "1h").snapshot
        assert snapshot.model_revision == "901c26c"
        assert "cache identity updated" in snapshot.note
        from axiom.forecast.cache import forecast_cache_key

        assert snapshot.cache_key == forecast_cache_key(
            instrument="DEMO", timeframe="1h",
            last_closed_timestamp=snapshot.last_closed_timestamp,
            input_hash_value=snapshot.input_hash,
            model_id="NeoQuasar/Kronos-small", model_revision="901c26c",
            config_hash=cfg.config_hash())


class TestCacheHitStaleness:
    def test_a_fresh_cache_hit_reports_ready(self):
        moment = now_for()
        pipeline, _ = make_pipeline(moment)
        pipeline.run("DEMO", "1h")
        hit = pipeline.run("DEMO", "1h")
        assert hit.cache_hit is True
        assert hit.state is KronosState.READY

    def test_an_aged_cache_hit_reports_stale_not_ready(self):
        """A hit is a hit, but it must not be dressed up as a fresh forecast."""
        cfg = config(stale_after_seconds=60.0)
        provider, all_bars, _ = fixture()
        moment = closed_now(all_bars)
        pipeline, runtime = make_pipeline(moment, cfg=cfg, provider=provider)
        first = pipeline.run("DEMO", "1h")
        assert first.state is KronosState.READY

        # 10 minutes later with no new closed candle: still the same window.
        pipeline.clock = lambda: moment + timedelta(minutes=10)
        aged = pipeline.run("DEMO", "1h")
        assert aged.cache_hit is True
        assert runtime.calls == 1, "no re-inference: the input window is unchanged"
        assert aged.state is KronosState.STALE
        assert "STALE" in aged.message
        assert aged.snapshot is first.snapshot
        assert aged.ok, "the previous forecast is still served, just labelled STALE"

    def test_force_refreshes_a_stale_cache_hit(self):
        cfg = config(stale_after_seconds=60.0)
        provider, all_bars, _ = fixture()
        moment = closed_now(all_bars)
        pipeline, runtime = make_pipeline(moment, cfg=cfg, provider=provider)
        pipeline.run("DEMO", "1h")
        pipeline.clock = lambda: moment + timedelta(minutes=10)
        forced = pipeline.run("DEMO", "1h", force=True)
        assert forced.state is KronosState.READY
        assert forced.cache_hit is False
        assert runtime.calls == 2


class TestPipelineFailures:
    def test_insufficient_history_is_reported(self):
        provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        bars = provider.fetch("DEMO", "1h").bars
        provider.add("DEMO", "1h", bars[:10])
        pipeline, _ = make_pipeline(now_for(), provider=provider,
                                    cfg=config(lookback=200, min_history=64))
        outcome = pipeline.run("DEMO", "1h")
        assert outcome.state is KronosState.INSUFFICIENT_HISTORY
        assert outcome.snapshot is None

    def test_corrupt_market_data_is_rejected_not_repaired(self):
        provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        bars = list(provider.fetch("DEMO", "1h").bars)
        broken = MarketDataBar(bars[-1].timestamp, float("nan"), 100.0, 99.0, 100.0,
                               10.0, "DEMO")
        provider.add("DEMO", "1h", bars[:-1] + [broken])
        pipeline, runtime = make_pipeline(now_for(), provider=provider)
        outcome = pipeline.run("DEMO", "1h")
        assert outcome.state is KronosState.DATA_ERROR
        assert "non-finite" in outcome.message
        assert outcome.quality.state is DataQualityState.DATA_ERROR
        assert runtime.calls == 0, "never run inference on rejected data"

    def test_provider_failure_degrades_instead_of_raising(self):
        class Broken:
            name = "broken"
            supported_timeframes = frozenset({"1h"})

            def fetch(self, instrument, timeframe, *, end=None, limit=None):
                raise MarketDataError("endpoint returned 503")

        pipeline, _ = make_pipeline(now_for(), provider=Broken())
        outcome = pipeline.run("DEMO", "1h")
        assert outcome.state is KronosState.DATA_ERROR
        assert "503" in outcome.message

    def test_provider_result_must_match_requested_instrument_and_timeframe(self):
        from axiom.market.provider import MarketDataProvider

        class MismatchedProvider(MarketDataProvider):
            name = "mismatch"

            def __init__(self, result_instrument, result_timeframe):
                super().__init__(source="fixture")
                self.result_instrument = result_instrument
                self.result_timeframe = result_timeframe

            def fetch(self, instrument, timeframe, *, end=None, limit=None):
                source_bars = ReplayMarketDataProvider.from_csv(
                    SAMPLE_CSV, "1h").fetch("DEMO", "1h").bars
                bars = tuple(MarketDataBar(
                    bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume,
                    self.result_instrument) for bar in source_bars)
                return MarketDataResult(
                    instrument=self.result_instrument, timeframe=self.result_timeframe,
                    bars=bars, source="fixture", provider="mismatch")

        for result_instrument, result_timeframe, expected in (
                ("OTHER", "1h", "result instrument"), ("DEMO", "30m", "result timeframe")):
            pipeline, runtime = make_pipeline(
                now_for(), provider=MismatchedProvider(result_instrument, result_timeframe))
            outcome = pipeline.run("DEMO", "1h")
            assert outcome.state is KronosState.DATA_ERROR
            assert expected in outcome.message
            assert runtime.calls == 0

    def test_naive_now_is_rejected_without_raising(self):
        pipeline, runtime = make_pipeline(datetime(2024, 1, 1))
        outcome = pipeline.run("DEMO", "1h")
        assert outcome.state is KronosState.DATA_ERROR
        assert "invalid forecast time" in outcome.message
        assert runtime.calls == 0

    def test_non_numeric_provider_bar_is_rejected_before_inference(self):
        provider, _all_bars, bars = fixture()
        object.__setattr__(bars[-5], "open", "not-a-price")
        pipeline, runtime = make_pipeline(now_for(), provider=provider)
        outcome = pipeline.run("DEMO", "1h")
        assert outcome.state is KronosState.DATA_ERROR
        assert "non-finite or non-numeric OHLCV" in outcome.message
        assert runtime.calls == 0

    def test_no_closed_candles_yet(self):
        _provider, _all_bars, visible = fixture(upto=5)
        # "now" is inside the only bar the provider holds: it has not closed.
        moment = visible[-1].timestamp + timedelta(minutes=10)
        single = ReplayMarketDataProvider.from_bars([visible[-1]], "1h")
        pipeline, runtime = make_pipeline(moment, provider=single)
        outcome = pipeline.run("DEMO", "1h")
        assert outcome.state is KronosState.INSUFFICIENT_HISTORY
        assert "no CLOSED candles" in outcome.message
        assert runtime.calls == 0

    def test_malformed_forecast_is_rejected_and_not_stored(self):
        moment = now_for()

        def bad_infer(bars, future_timestamps):
            return KronosInference((({"open": 1.0, "high": 2.0, "low": 0.5,
                                      "close": 1.5, "volume": 1.0},),), 1.0, "stub")

        cfg = config()
        provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        store = ForecastStore()
        pipeline = ForecastPipeline(provider, cfg, store=store, infer=bad_infer,
                                    clock=lambda: moment)
        outcome = pipeline.run("DEMO", "1h")
        assert outcome.state is KronosState.ERROR
        assert "forecast rejected" in outcome.message
        assert store.latest("DEMO", "1h") is None

    def test_a_snapshot_assembly_failure_degrades_instead_of_raising(self):
        """run() promises never to raise into the caller — assembly included.

        ForecastSnapshot re-validates its own invariants, so a construction
        failure is possible in principle. The UI thread calls this, so it must
        come back as an ERROR state rather than an exception.
        """
        import axiom.forecast.pipeline as pipeline_module

        moment = now_for()
        pipeline, runtime = make_pipeline(moment)
        assert runtime.load().state is KronosState.READY  # inference does happen

        def explode(*_args, **_kwargs):
            raise ValueError("provenance mismatch")

        monkeypatched = pipeline_module.ForecastSnapshot
        pipeline_module.ForecastSnapshot = explode
        try:
            outcome = pipeline.run("DEMO", "1h")
        finally:
            pipeline_module.ForecastSnapshot = monkeypatched

        assert outcome.state is KronosState.ERROR
        assert "could not assemble the forecast snapshot" in outcome.message
        assert "ValueError" in outcome.message
        assert outcome.snapshot is None
        assert runtime.calls == 1

    def test_a_store_failure_degrades_instead_of_raising(self):
        moment = now_for()
        pipeline, _ = make_pipeline(moment)

        def explode(_snapshot):
            raise RuntimeError("store is locked")

        pipeline.store.put = explode
        outcome = pipeline.run("DEMO", "1h")
        assert outcome.state is KronosState.ERROR
        assert "could not assemble the forecast snapshot" in outcome.message
        assert outcome.snapshot is None

    def test_runtime_unavailable_is_an_error_not_a_crash(self):
        moment = now_for()

        def dead_infer(bars, future_timestamps):
            raise KronosRuntimeUnavailable("Kronos process died")

        pipeline = ForecastPipeline(
            ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h"), config(),
            store=ForecastStore(), infer=dead_infer, clock=lambda: moment)
        outcome = pipeline.run("DEMO", "1h")
        assert outcome.state is KronosState.ERROR
        assert "died" in outcome.message

    def test_previous_forecast_survives_a_kronos_failure(self):
        provider, all_bars, visible = fixture(upto=300)
        moment = closed_now(visible)
        store = ForecastStore()
        pipeline, _ = make_pipeline(moment, store=store, provider=provider)
        good = pipeline.run("DEMO", "1h")
        assert good.snapshot is not None

        def dead_infer(bars, future_timestamps):
            raise KronosRuntimeUnavailable("gone")

        # A new closed bar forces a rerun, and Kronos is dead for it.
        provider.add("DEMO", "1h", all_bars[:301])
        pipeline.infer = dead_infer
        pipeline.clock = lambda: closed_now(all_bars[:301])
        failed = pipeline.run("DEMO", "1h")
        assert failed.state is KronosState.ERROR
        assert store.latest("DEMO", "1h") is good.snapshot


class TestForecastStore:
    def test_latest_and_history(self):
        provider, all_bars, visible = fixture(upto=300)
        store = ForecastStore(history_limit=3)
        pipeline, _ = make_pipeline(closed_now(visible), store=store, provider=provider)
        for count in range(4):
            # Each iteration a new recorded bar closes → a genuinely new window.
            provider.add("DEMO", "1h", all_bars[:301 + count])
            pipeline.clock = lambda count=count: closed_now(all_bars[:301 + count])
            outcome = pipeline.run("DEMO", "1h")
            assert outcome.cache_hit is False
        assert len(store.history("DEMO", "1h")) == 3, "history is capped"
        assert store.latest("DEMO", "1h") is not None
        assert store.keys() == [("DEMO", "1h")]

    def test_staleness(self):
        moment = now_for()
        store = ForecastStore()
        pipeline, _ = make_pipeline(moment, store=store)
        pipeline.run("DEMO", "1h")
        later = moment + timedelta(hours=1)
        assert store.stale_series(later, stale_after=10.0) == [("DEMO", "1h")]
        assert store.stale_series(moment, stale_after=10_000.0) == []
        assert store.state_for("DEMO", "1h", KronosState.READY, now=later,
                               stale_after=1.0) is KronosState.STALE
        assert store.state_for("DEMO", "1h", KronosState.READY, now=moment,
                               stale_after=10_000.0) is KronosState.READY

    def test_cache_key_bookkeeping_and_stats(self):
        moment = now_for()
        store = ForecastStore()
        pipeline, _ = make_pipeline(moment, store=store)
        pipeline.run("DEMO", "1h")
        pipeline.run("DEMO", "1h")
        stats = store.stats
        assert stats["series"] == 1
        assert stats["cache_hits"] == 1
        assert stats["cache_misses"] == 1
        assert store.cache_key("DEMO", "1h")

    def test_clear(self):
        moment = now_for()
        store = ForecastStore()
        pipeline, _ = make_pipeline(moment, store=store)
        pipeline.run("DEMO", "1h")
        store.clear()
        assert store.latest("DEMO", "1h") is None
        assert store.keys() == []
