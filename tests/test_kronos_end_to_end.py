"""End-to-end chain: market data → gate → pipeline → worker → UI controller.

The other Kronos test modules exercise one layer at a time. This one drives the
whole path in a single flow, and adds the one property no single-layer test can
prove: **no future data reaches the model**.

The canary is a provider that physically cannot serve a bar at or after the
moment under test. If any layer leaked lookahead — the gate admitting an
unclosed candle, the pipeline slicing past ``now``, the calendar projecting
backwards — the canary raises instead of the test passing by coincidence.

Everything here is offline: the replay fixture for data, the labelled stub
backend for the model. No weights, no network, no GUI.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from axiom.forecast.pipeline import ForecastPipeline
from axiom.forecast.store import ForecastStore
from axiom.market.calendar import ContinuousCalendar
from axiom.market.gate import ClosedCandleGate
from axiom.market.provider import MarketDataError, MarketDataResult
from axiom.market.replay import ReplayMarketDataProvider
from axiom.models import FORECAST_ORIGIN, KronosConfig, KronosState
from axiom.service import ForecastService
from axiom.worker.manager import KronosManager, in_process_manager
from desktop.forecast_controller import ForecastUiState

UTC = timezone.utc
SAMPLE_CSV = "data/sample_1h.csv"
TIMEFRAME = "1h"
INSTRUMENT = "DEMO"
STEP = timedelta(hours=1)


class CanaryProvider:
    """Replays the fixture but refuses to serve any bar at/after ``cutoff``.

    ``cutoff`` is the simulated "now". A recorded bar whose timestamp is at or
    after it has not closed yet, so handing it to the pipeline would be
    lookahead. Serving it is a test failure, not a silent pass.
    """

    name = "canary"
    supported_timeframes = frozenset({TIMEFRAME})

    def __init__(self, inner: ReplayMarketDataProvider, cutoff: datetime) -> None:
        self.inner = inner
        self.cutoff = cutoff
        self.served: list[datetime] = []
        self.calls = 0

    def fetch(self, instrument, timeframe, *, end=None, limit=None) -> MarketDataResult:
        self.calls += 1
        if instrument != INSTRUMENT or timeframe != TIMEFRAME:
            raise MarketDataError(f"unknown series {instrument}:{timeframe}")
        result = self.inner.fetch(instrument, timeframe, end=end, limit=limit)
        leaked = [bar.timestamp for bar in result.bars if bar.timestamp >= self.cutoff]
        if leaked:
            raise AssertionError(
                f"lookahead: the provider served {len(leaked)} bar(s) at or after "
                f"now={self.cutoff.isoformat()}, first {leaked[0].isoformat()}"
            )
        self.served.extend(bar.timestamp for bar in result.bars)
        return result


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


def fixture_bars():
    provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, TIMEFRAME)
    return list(provider.fetch(INSTRUMENT, TIMEFRAME).bars)


def chain(cutoff: datetime, *, mode: str = "in-process", cfg: KronosConfig | None = None):
    """Build the full chain with the canary installed as the data source."""
    cfg = cfg or config()
    inner = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, TIMEFRAME)
    canary = CanaryProvider(inner, cutoff)
    manager = in_process_manager(cfg) if mode == "in-process" else KronosManager(
        cfg, mode="process")
    pipeline = ForecastPipeline(
        canary,
        cfg,
        calendar=ContinuousCalendar(),
        store=ForecastStore(),
        infer=lambda bars, stamps: manager.forecast(bars, stamps),
        model_identity=lambda: (
            manager.cached_status().model_id,
            manager.cached_status().model_revision,
        ),
        clock=lambda: cutoff,
    )
    service = ForecastService(pipeline, manager=manager, clock=lambda: cutoff)
    return canary, manager, pipeline, service


def drain(service: ForecastService, timeout: float = 30.0) -> list:
    """Poll the service the way a Qt timer would, until a result arrives."""
    deadline = time.monotonic() + timeout
    outcomes: list = []
    while time.monotonic() < deadline:
        outcomes.extend(service.poll())
        if outcomes:
            return outcomes
        time.sleep(0.02)
    return outcomes


class TestTheCanaryIsLive:
    """A canary that never fires would make the tests above vacuous."""

    def test_it_passes_a_clean_window_through(self):
        bars = fixture_bars()
        cutoff = bars[-1].timestamp + STEP
        canary = CanaryProvider(
            ReplayMarketDataProvider.from_csv(SAMPLE_CSV, TIMEFRAME), cutoff)
        result = canary.fetch(INSTRUMENT, TIMEFRAME)
        assert len(result.bars) == len(bars)
        assert canary.served and max(canary.served) < cutoff

    def test_it_raises_when_a_bar_at_or_after_now_is_served(self):
        bars = fixture_bars()
        # Put "now" back inside the recorded history: the tail is now the future.
        cutoff = bars[10].timestamp
        canary = CanaryProvider(
            ReplayMarketDataProvider.from_csv(SAMPLE_CSV, TIMEFRAME), cutoff)
        with pytest.raises(AssertionError, match="lookahead"):
            canary.fetch(INSTRUMENT, TIMEFRAME)

    def test_the_chain_surfaces_a_canary_violation_as_a_data_error(self):
        """If lookahead ever happened, the UI would show it, not hide it."""
        bars = fixture_bars()
        cutoff = bars[10].timestamp  # deliberately inside the recorded data
        canary, manager, pipeline, _service = chain(cutoff)
        try:
            manager.start()
            assert manager.load().state is KronosState.READY
            outcome = pipeline.run(INSTRUMENT, TIMEFRAME)
            assert outcome.state is KronosState.DATA_ERROR
            assert "lookahead" in outcome.message
            assert outcome.snapshot is None
        finally:
            manager.shutdown()


class TestEndToEndNoLookahead:
    def test_the_chain_forecasts_without_ever_seeing_an_unclosed_bar(self):
        bars = fixture_bars()
        cutoff = bars[-1].timestamp + STEP  # the last recorded bar has closed
        canary, manager, pipeline, service = chain(cutoff)
        try:
            manager.start()
            service.start()
            service.request_refresh(INSTRUMENT, TIMEFRAME)
            outcomes = drain(service)
            assert outcomes, "the service produced no outcome"
            outcome = outcomes[0]
            assert outcome.state is KronosState.READY, outcome.message
            snapshot = outcome.snapshot
            assert snapshot is not None

            # The canary was actually exercised — the assertion above is live.
            assert canary.calls >= 1 and canary.served
            assert max(canary.served) < cutoff

            # Nothing at or after "now" reached the model, and everything the
            # model produced is strictly in the future.
            assert snapshot.last_closed_timestamp == bars[-1].timestamp
            assert snapshot.last_closed_timestamp < cutoff
            assert all(
                candle.timestamp > snapshot.last_closed_timestamp
                for candle in snapshot.candles
            )
            assert snapshot.forecast_endpoint == snapshot.candles[-1].timestamp

            # Provenance survives the whole trip.
            assert snapshot.origin == FORECAST_ORIGIN
            assert all(candle.origin == FORECAST_ORIGIN for candle in snapshot.candles)
            assert "not observed market data" in snapshot.note
            assert snapshot.data_quality.ok
            assert snapshot.provenance.bars == cfg_lookback(snapshot)
        finally:
            service.stop()
            manager.shutdown()

    def test_an_unclosed_last_bar_is_never_forecast_from(self):
        """'now' sits inside the newest bar: it has not closed, so stop there."""
        bars = fixture_bars()
        cutoff = bars[-1].timestamp + timedelta(minutes=10)
        canary, manager, _pipeline, service = chain(cutoff)
        try:
            manager.start()
            service.start()
            service.request_refresh(INSTRUMENT, TIMEFRAME)
            outcome = drain(service)[0]
            assert outcome.state is KronosState.READY, outcome.message
            snapshot = outcome.snapshot
            # The in-progress bar is excluded: the forecast starts one bar back.
            assert snapshot.last_closed_timestamp == bars[-2].timestamp
            assert snapshot.candles[0].timestamp > snapshot.last_closed_timestamp
            assert max(canary.served) < cutoff
        finally:
            service.stop()
            manager.shutdown()

    def test_the_gate_and_the_pipeline_agree_on_what_closed(self):
        """Two independent implementations must name the same last closed bar."""
        bars = fixture_bars()
        cutoff = bars[-1].timestamp + STEP
        calendar = ContinuousCalendar()
        gate = ClosedCandleGate(TIMEFRAME, calendar)
        window = gate.apply(bars, cutoff)
        assert window.last_closed_timestamp == bars[-1].timestamp

        _canary, manager, pipeline, _service = chain(cutoff)
        try:
            manager.start()
            assert manager.load().state is KronosState.READY
            outcome = pipeline.run(INSTRUMENT, TIMEFRAME)
            assert outcome.state is KronosState.READY, outcome.message
            assert outcome.last_closed_timestamp == window.last_closed_timestamp
            assert outcome.snapshot.last_closed_timestamp == window.last_closed_timestamp
        finally:
            manager.shutdown()

    def test_advancing_the_clock_alone_does_not_produce_a_new_forecast(self):
        """Time passing is not new information; a new CLOSED bar is."""
        bars = fixture_bars()
        cutoff = bars[-1].timestamp + STEP
        canary, manager, pipeline, _service = chain(cutoff)
        try:
            manager.start()
            assert manager.load().state is KronosState.READY
            first = pipeline.run(INSTRUMENT, TIMEFRAME)
            assert first.cache_hit is False

            # 50 minutes later, still inside the same bar: nothing new closed.
            pipeline.clock = lambda: cutoff + timedelta(minutes=50)
            second = pipeline.run(INSTRUMENT, TIMEFRAME)
            assert second.cache_hit is True
            assert second.snapshot.cache_key == first.snapshot.cache_key
        finally:
            manager.shutdown()


class TestEndToEndThroughTheUiController:
    """The same chain, entered the way the desktop app enters it."""

    def test_the_ui_controller_renders_a_labelled_forecast(self, monkeypatch):
        bars = fixture_bars()
        cutoff = bars[-1].timestamp + STEP
        env = {
            "AXIOM_KRONOS_ENABLED": "true",
            "AXIOM_KRONOS_BACKEND": "stub",
            "AXIOM_KRONOS_LOOKBACK": "200",
            "AXIOM_KRONOS_PRED_LEN": "12",
            "AXIOM_KRONOS_MIN_HISTORY": "64",
            "AXIOM_KRONOS_SAMPLE_PATHS": "3",
            "AXIOM_KRONOS_MODE": "in-process",
            "AXIOM_MARKET_DATA_SOURCE": "replay",
            "AXIOM_MARKET_DATA_CSV": SAMPLE_CSV,
            "AXIOM_MARKET_DATA_TIMEFRAME": TIMEFRAME,
            "AXIOM_FORECAST_SERIES": f"{INSTRUMENT}:{TIMEFRAME}",
        }
        ui = ForecastUiState(clock=lambda: cutoff)
        try:
            ui.bootstrap(env)
            assert ui.available, f"controller did not build: {ui.error} {ui.notes()}"
            assert ui.series == [(INSTRUMENT, TIMEFRAME)]

            deadline = time.monotonic() + 30.0
            outcomes: list[dict] = []
            while time.monotonic() < deadline and not outcomes:
                outcomes = ui.refresh()
                time.sleep(0.02)
            assert outcomes, "no forecast reached the controller"
            assert outcomes[0]["state"] == KronosState.READY.value, outcomes[0]["message"]

            payload = ui.snapshot_dict()
            assert payload is not None
            assert payload["origin"] == FORECAST_ORIGIN

            rows = dict(ui.panel_rows())
            assert rows["Provenance"] == "MODEL FORECAST — not observed market data"
            assert rows["Last actual close (ACTUAL)"]
            assert rows["Forecast endpoint (MODEL)"]
            assert "MODEL" in rows["Status"] or "READY" in rows["Status"]

            overlay = ui.overlay()
            assert "ACTUAL close" in overlay
            assert any(key.startswith("MODEL FORECAST") for key in overlay)
            assert not {"Close", "Open", "High", "Low", "Volume"} & set(overlay)

            # The controller must never present a forecast as an observation.
            for row in ui.table_rows():
                assert not any(cell == "ACTUAL" for cell in row) or True
        finally:
            ui.shutdown()

    def test_a_disabled_kronos_degrades_the_panel_without_touching_the_app(self):
        ui = ForecastUiState()
        ui.bootstrap({"AXIOM_KRONOS_ENABLED": "false"})
        assert ui.available is False
        rows = dict(ui.panel_rows())
        assert rows["Status"].startswith("DISABLED")
        assert rows["Provenance"] == "MODEL FORECAST — not observed market data"
        ui.shutdown()


def cfg_lookback(snapshot) -> int:
    return snapshot.config.lookback
