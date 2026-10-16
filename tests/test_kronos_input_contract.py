"""Kronos inference input boundary regressions.

The forecast pipeline additionally validates timeframe/session gaps and closed
bars. These tests pin the layer-independent contracts at the runtime, upstream
backend, and IPC child boundaries so direct callers cannot bypass shape,
chronology, timezone, or configuration checks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from axiom.kronos.backends import (
    KronosRuntimeInfo,
    StubKronosBackend,
    UpstreamKronosBackend,
)
from axiom.kronos.runtime import KronosRuntime
from axiom.market.provider import MarketDataBar, MarketDataError
from axiom.market.validation import validate_inference_inputs
from axiom.models import KronosConfig
from axiom.worker.child import ChildRuntime
from axiom.worker.protocol import (
    KronosRequest,
    encode_bars,
    encode_timestamps,
)

UTC = timezone.utc


def config(**overrides):
    values = dict(enabled=True, backend="stub", lookback=4, pred_len=2,
                  max_context=8, min_history=2, sample_paths=1)
    values.update(overrides)
    return KronosConfig(**values).validate()


def bars(*, timezone_offset=timezone.utc):
    start = datetime(2024, 1, 1, tzinfo=timezone_offset)
    return [
        MarketDataBar(start, 100.0, 102.0, 99.0, 101.0, 10.0, "NSE|X"),
        MarketDataBar(start + timedelta(minutes=1), 101.0, 103.0, 100.0,
                      102.0, 12.0, "NSE|X"),
    ]


def future():
    return [datetime(2024, 1, 1, 0, 2, tzinfo=UTC),
            datetime(2024, 1, 1, 0, 3, tzinfo=UTC)]


def test_valid_contract_accepts_utc_ordered_window_and_horizon():
    validate_inference_inputs(bars(), future(), config(), expected_instrument="NSE|X")


def test_future_timestamp_count_must_match_pred_len():
    with pytest.raises(MarketDataError, match=r"future timestamps \(1\) != pred_len \(2\)"):
        validate_inference_inputs(bars(), future()[:1], config())


def test_future_stamps_must_be_utc_ordered_and_after_last_input():
    non_utc = [datetime(2024, 1, 1, 0, 2, tzinfo=timezone(timedelta(hours=1))),
               datetime(2024, 1, 1, 0, 3, tzinfo=timezone(timedelta(hours=1)))]
    with pytest.raises(MarketDataError, match="must be UTC"):
        validate_inference_inputs(bars(), non_utc, config())

    reversed_stamps = future()[::-1]
    with pytest.raises(MarketDataError, match="strictly increasing"):
        validate_inference_inputs(bars(), reversed_stamps, config())

    overlapping = [bars()[-1].timestamp, future()[1]]
    with pytest.raises(MarketDataError, match="strictly after"):
        validate_inference_inputs(bars(), overlapping, config())


def test_forecast_origin_and_malformed_instrument_cannot_be_reused_as_market_input():
    reused = bars()
    object.__setattr__(reused[0], "origin", "KRONOS_FORECAST")
    with pytest.raises(MarketDataError, match="origin must be MARKET_OBSERVED"):
        validate_inference_inputs(reused, future(), config())

    padded = bars()
    object.__setattr__(padded[0], "instrument", " NSE|X")
    with pytest.raises(MarketDataError, match="valid instrument identity"):
        validate_inference_inputs(padded, future(), config())


def test_bar_count_order_instrument_and_shape_are_checked():
    cfg = config()
    with pytest.raises(MarketDataError, match="min_history"):
        validate_inference_inputs(bars()[:1], future(), cfg)

    good = bars()
    with pytest.raises(MarketDataError, match="strictly increasing"):
        validate_inference_inputs([good[1], good[0]], future(), cfg)
    with pytest.raises(MarketDataError, match="more than one instrument"):
        validate_inference_inputs([good[0], MarketDataBar(
            good[1].timestamp, 101.0, 103.0, 100.0, 102.0, 12.0, "NSE|Y")],
            future(), cfg)

    corrupt = list(good)
    object.__setattr__(corrupt[0], "high", float("nan"))
    with pytest.raises(MarketDataError, match="non-finite"):
        validate_inference_inputs(corrupt, future(), cfg)


def test_runtime_rejects_bad_inputs_before_calling_custom_backend():
    class CountingStub(StubKronosBackend):
        def __init__(self):
            super().__init__()
            self.forecast_calls = 0

        def forecast(self, bars, future_timestamps, cfg):
            self.forecast_calls += 1
            return super().forecast(bars, future_timestamps, cfg)

    backend = CountingStub()
    runtime = KronosRuntime(config(), backend=backend)
    assert runtime.load().state.value == "READY"
    with pytest.raises(MarketDataError, match="future timestamps"):
        runtime.forecast_paths(bars(), future()[:1])
    assert backend.forecast_calls == 0
    assert runtime.status.state.value == "READY"


def test_upstream_backend_checks_count_before_touching_predictor_or_pandas():
    class MustNotPredict:
        def __init__(self):
            self.calls = 0

        def predict(self, **kwargs):
            self.calls += 1
            raise AssertionError("invalid timestamp count reached predictor")

    backend = UpstreamKronosBackend()
    predictor = MustNotPredict()
    backend.predictor = predictor
    backend.info = KronosRuntimeInfo(
        model_id="NeoQuasar/Kronos-small", model_revision="rev",
        tokenizer_id="NeoQuasar/Kronos-Tokenizer-base", tokenizer_revision="rev",
        device="cpu", backend="upstream")
    with pytest.raises(MarketDataError, match="future timestamps"):
        backend.forecast(bars(), future()[:1], config(backend="upstream"))
    assert predictor.calls == 0


def test_child_honors_explicit_model_cache_directory(tmp_path):
    cache_dir = tmp_path / "configured-model-cache"
    cfg = config(cache_dir=str(cache_dir))
    child = ChildRuntime(config=cfg)
    response = child.handle(KronosRequest(
        kind="load", request_id="cache-dir", payload={"config": cfg.to_dict()}))
    assert response.ok is True
    assert response.result["status"]["cache_dir"] == str(cache_dir)


def test_worker_ipc_rejects_bad_timestamp_count_before_backend():
    class CountingStub(StubKronosBackend):
        def __init__(self):
            super().__init__()
            self.forecast_calls = 0

        def forecast(self, bars, future_timestamps, cfg):
            self.forecast_calls += 1
            return super().forecast(bars, future_timestamps, cfg)

    backend = CountingStub()
    runtime = KronosRuntime(config(), backend=backend)
    runtime.load()
    child = ChildRuntime(runtime=runtime)
    request = KronosRequest(
        kind="forecast", request_id="count-mismatch",
        payload={"config": config().to_dict(),
                 "bars": encode_bars(bars()),
                 "future_timestamps": encode_timestamps(future()[:1])})
    response = child.handle(request)
    assert response.ok is False
    assert "future timestamps" in response.error
    assert backend.forecast_calls == 0
