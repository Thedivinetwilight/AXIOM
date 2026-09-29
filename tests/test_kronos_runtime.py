"""Kronos runtime: configuration, lifecycle, model loading, cache, inference.

The CPU inference tests run against the REAL Kronos implementation when
``AXIOM_KRONOS_SOURCE`` points at a checkout of https://github.com/shiyu-coder/Kronos
and PyTorch is installed. Weights cannot be downloaded in an offline sandbox, so
those tests build the real architecture with small dimensions and random weights
(the download is stubbed, the tensor path is real).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from axiom.kronos.backends import (
    KNOWN_MODEL_CONTEXT,
    KronosModelMismatch,
    KronosRuntimeUnavailable,
    StubKronosBackend,
    UpstreamKronosBackend,
    build_backend,
    validate_context,
)
from axiom.kronos.model_cache import ModelCache, default_cache_root, sha256_file
from axiom.kronos.runtime import KronosRuntime
from axiom.kronos.samples import (
    MIN_PATHS_FOR_UNCERTAINTY,
    interval_coverage,
    mean_interval_width,
    percentile,
    summarize_paths,
)
from axiom.market.provider import MarketDataBar
from axiom.models import KronosConfig, KronosState, KronosStatus

UTC = timezone.utc


def bars(count: int = 40, *, instrument: str = "DEMO") -> list[MarketDataBar]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    out = []
    price = 100.0
    for index in range(count):
        close = price + index * 0.25
        out.append(
            MarketDataBar(
                start + timedelta(hours=index),
                close - 0.1,
                close + 0.3,
                close - 0.3,
                close,
                1000.0,
                instrument,
            )
        )
        price = close
    return out


def future(count: int, *, start: datetime | None = None) -> list[datetime]:
    origin = start or datetime(2024, 3, 1, tzinfo=UTC)
    return [origin + timedelta(hours=index) for index in range(count)]


class TestKronosConfig:
    def test_defaults_match_kronos_small(self):
        config = KronosConfig()
        assert config.model_id == "NeoQuasar/Kronos-small"
        assert config.tokenizer_id == "NeoQuasar/Kronos-Tokenizer-base"
        assert (config.lookback, config.pred_len, config.max_context) == (400, 120, 512)
        assert (config.temperature, config.top_k, config.top_p) == (1.0, 0, 0.9)
        assert config.sample_paths == 1
        assert config.device == "auto"
        assert config.update_on_closed_bar is True
        assert config.enabled is False, "Kronos must be off unless explicitly enabled"

    def test_lookback_may_not_exceed_max_context(self):
        with pytest.raises(ValueError, match="max_context"):
            KronosConfig(lookback=600, max_context=512).validate()

    def test_invalid_values_are_rejected(self):
        with pytest.raises(ValueError, match="temperature"):
            KronosConfig(temperature=0.0).validate()
        with pytest.raises(ValueError, match="top_p"):
            KronosConfig(top_p=1.5).validate()
        with pytest.raises(ValueError, match="sample_paths"):
            KronosConfig(sample_paths=0).validate()
        with pytest.raises(ValueError, match="device"):
            KronosConfig(device="tpu").validate()
        with pytest.raises(ValueError, match="min_history"):
            KronosConfig(min_history=900, lookback=400).validate()
        with pytest.raises(ValueError, match="backend"):
            KronosConfig(backend="magic").validate()

    def test_config_hash_is_stable_and_sensitive(self):
        first = KronosConfig().config_hash()
        assert first == KronosConfig().config_hash()
        assert first != KronosConfig(pred_len=121).config_hash()
        assert first != KronosConfig(model_revision="abc123").config_hash()
        assert first != KronosConfig(temperature=0.7).config_hash()

    def test_sampling_signature_ignores_unrelated_settings(self):
        assert KronosConfig().sampling_signature() == KronosConfig(
            lookback=200).sampling_signature()
        assert KronosConfig().sampling_signature() != KronosConfig(
            sample_paths=5).sampling_signature()


class TestRuntimeLifecycle:
    def test_disabled_runtime_never_reports_ready(self):
        runtime = KronosRuntime(KronosConfig(enabled=False))
        assert runtime.status.state is KronosState.DISABLED
        assert runtime.load().state is KronosState.DISABLED
        assert runtime.status.is_ready is False

    def test_load_transitions_to_ready_only_on_success(self):
        seen = []
        runtime = KronosRuntime(
            KronosConfig(enabled=True, backend="stub", lookback=40, pred_len=4,
                         min_history=4),
            on_state=lambda state, message: seen.append(state),
        )
        assert runtime.status.state is KronosState.INITIALIZING
        status = runtime.load()
        assert status.state is KronosState.READY
        assert KronosState.LOADING in seen
        assert status.model_id.startswith("stub:")
        assert status.device == "cpu"

    def test_load_failure_reports_error_never_ready(self):
        class Exploding(StubKronosBackend):
            def load(self, config, *, cache=None, on_state=None):
                raise KronosRuntimeUnavailable("weights missing")

        runtime = KronosRuntime(KronosConfig(enabled=True, backend="stub"),
                                backend=Exploding())
        status = runtime.load()
        assert status.state is KronosState.ERROR
        assert status.is_ready is False
        assert "weights missing" in status.message
        assert status.last_error == "weights missing"

    def test_unexpected_exception_is_also_an_error(self):
        class Broken(StubKronosBackend):
            def load(self, config, *, cache=None, on_state=None):
                raise RuntimeError("boom")

        status = KronosRuntime(KronosConfig(enabled=True, backend="stub"),
                              backend=Broken()).load()
        assert status.state is KronosState.ERROR
        assert "boom" in status.message

    def test_inference_requires_a_loaded_runtime(self):
        runtime = KronosRuntime(KronosConfig(enabled=True, backend="stub",
                                             lookback=40, pred_len=4, min_history=4))
        with pytest.raises(KronosRuntimeUnavailable, match="not ready"):
            runtime.forecast_paths(bars(), future(4))

    def test_inference_counts_and_keeps_the_runtime_ready_after_failure(self):
        class Flaky(StubKronosBackend):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def forecast(self, bars, future_timestamps, config):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("cuda oom")
                return super().forecast(bars, future_timestamps, config)

        backend = Flaky()
        runtime = KronosRuntime(
            KronosConfig(enabled=True, backend="stub", lookback=40, pred_len=4,
                         min_history=4),
            backend=backend,
        )
        runtime.load()
        with pytest.raises(RuntimeError):
            runtime.forecast_paths(bars(), future(4))
        assert runtime.status.state is KronosState.READY, "model is still loaded"
        assert "cuda oom" in runtime.status.message
        result = runtime.forecast_paths(bars(), future(4))
        assert result.sample_paths == 1
        assert runtime.status.inferences == 1
        assert runtime.status.last_error == ""

    def test_shutdown_marks_offline(self):
        runtime = KronosRuntime(KronosConfig(enabled=True, backend="stub"))
        runtime.load()
        runtime.shutdown()
        assert runtime.status.state is KronosState.OFFLINE
        assert runtime.info is None

    def test_stale_is_explicit(self):
        runtime = KronosRuntime(KronosConfig(enabled=True, backend="stub"))
        runtime.load()
        assert runtime.mark_stale("forecast is old").state is KronosState.STALE


class TestKronosStatus:
    def test_round_trip_over_ipc(self):
        status = KronosStatus(
            state=KronosState.READY,
            message="loaded",
            model_id="NeoQuasar/Kronos-small",
            model_revision="abc",
            tokenizer_id="NeoQuasar/Kronos-Tokenizer-base",
            device="cpu",
            loaded_at=datetime(2024, 1, 1, tzinfo=UTC),
            pid=4321,
            inferences=3,
            last_inference_ms=12.5,
        )
        restored = KronosStatus.from_dict(status.to_dict())
        assert restored.state is KronosState.READY
        assert restored.model_revision == "abc"
        assert restored.pid == 4321
        assert restored.loaded_at == status.loaded_at

    def test_ready_states(self):
        assert KronosStatus(state=KronosState.READY).available
        assert not KronosStatus(state=KronosState.ERROR).available
        assert not KronosStatus(state=KronosState.DISABLED).available
        assert KronosStatus(state=KronosState.STALE).available


class TestPairingAndContext:
    """Tokenizer/model pairing is checked without needing PyTorch."""

    def test_matching_pair_passes(self):
        model = SimpleNamespace(s1_bits=9, s2_bits=9)
        tokenizer = SimpleNamespace(s1_bits=9, s2_bits=9, d_in=6)
        UpstreamKronosBackend._check_pairing(model, tokenizer, KronosConfig())

    def test_bits_mismatch_is_rejected(self):
        model = SimpleNamespace(s1_bits=9, s2_bits=9)
        tokenizer = SimpleNamespace(s1_bits=8, s2_bits=9, d_in=6)
        with pytest.raises(KronosModelMismatch, match="s1_bits mismatch"):
            UpstreamKronosBackend._check_pairing(model, tokenizer, KronosConfig())

    def test_wrong_tokenizer_input_dim_is_rejected(self):
        model = SimpleNamespace(s1_bits=9, s2_bits=9)
        tokenizer = SimpleNamespace(s1_bits=9, s2_bits=9, d_in=4)
        with pytest.raises(KronosModelMismatch, match="d_in=4"):
            UpstreamKronosBackend._check_pairing(model, tokenizer, KronosConfig())

    def test_known_context_lengths(self):
        assert KNOWN_MODEL_CONTEXT["NeoQuasar/Kronos-small"] == 512
        assert KNOWN_MODEL_CONTEXT["NeoQuasar/Kronos-mini"] == 2048

    def test_context_over_the_model_limit_is_rejected(self):
        with pytest.raises(KronosModelMismatch, match="context length of 512"):
            validate_context(KronosConfig(model_id="NeoQuasar/Kronos-small",
                                          max_context=1024, lookback=400))

    def test_unknown_model_context_is_flagged_not_assumed(self):
        notes = validate_context(KronosConfig(model_id="NeoQuasar/Kronos-custom",
                                             max_context=256, lookback=200))
        assert any("no published context length" in note for note in notes)

    def test_backend_selection(self):
        assert build_backend(KronosConfig(backend="stub")).kind == "stub"
        assert build_backend(KronosConfig(backend="upstream")).kind == "upstream"

    def test_missing_kronos_source_is_reported(self, monkeypatch):
        monkeypatch.setenv("AXIOM_KRONOS_SOURCE", "/definitely/not/a/kronos/checkout")
        with pytest.raises(KronosRuntimeUnavailable, match="does not look like"):
            UpstreamKronosBackend().load(KronosConfig(enabled=True))


class TestModelCache:
    def test_windows_cache_location(self, tmp_path):
        from axiom.kronos.model_cache import cache_root_for

        root = cache_root_for(
            override=None,
            os_name="nt",
            platform="win32",
            localappdata="C:\\Users\\tester\\AppData\\Local",
            xdg_cache_home=None,
            home=tmp_path,
        )
        assert str(root).replace("\\", "/") == (
            "C:/Users/tester/AppData/Local/AXIOM/models/kronos")

        fallback = cache_root_for(
            override=None, os_name="nt", platform="win32", localappdata=None,
            xdg_cache_home=None, home=tmp_path)
        assert fallback == tmp_path / "AppData" / "Local" / "AXIOM" / "models" / "kronos"

        xdg = cache_root_for(
            override=None, os_name="posix", platform="linux", localappdata=None,
            xdg_cache_home=str(tmp_path / "xdg"), home=tmp_path)
        assert xdg == tmp_path / "xdg" / "AXIOM" / "models" / "kronos"

        darwin = cache_root_for(
            override=None, os_name="posix", platform="darwin", localappdata=None,
            xdg_cache_home=None, home=tmp_path)
        assert darwin == tmp_path / "Library" / "Caches" / "AXIOM" / "models" / "kronos"

    def test_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AXIOM_MODEL_CACHE", str(tmp_path / "weights"))
        assert default_cache_root() == tmp_path / "weights"

    def test_record_and_verify_round_trip(self, tmp_path):
        cache = ModelCache(tmp_path)
        weights = tmp_path / "model.safetensors"
        weights.write_bytes(b"fake-weights-bytes")
        entry = cache.record("NeoQuasar/Kronos-small", "rev-1", weights)
        assert entry.size_bytes == len(b"fake-weights-bytes")
        assert entry.sha256 == sha256_file(weights)
        report = cache.verify("NeoQuasar/Kronos-small", "rev-1")
        assert report.ok, report.problems

    def test_tampered_weights_are_detected(self, tmp_path):
        cache = ModelCache(tmp_path)
        weights = tmp_path / "model.safetensors"
        weights.write_bytes(b"original")
        cache.record("m", "r", weights)
        weights.write_bytes(b"tampered-with-longer-content")
        report = cache.verify("m", "r")
        assert not report.ok
        assert any("SHA-256 mismatch" in problem for problem in report.problems)

    def test_missing_weights_are_detected(self, tmp_path):
        cache = ModelCache(tmp_path)
        weights = tmp_path / "model.safetensors"
        weights.write_bytes(b"original")
        cache.record("m", "r", weights)
        weights.unlink()
        report = cache.verify("m", "r")
        assert not report.ok
        assert any("missing" in problem for problem in report.problems)

    def test_expected_digest_is_enforced(self, tmp_path):
        cache = ModelCache(tmp_path)
        weights = tmp_path / "model.safetensors"
        weights.write_bytes(b"payload")
        cache.record("m", "r", weights)
        report = cache.verify("m", "r", expected_sha256="0" * 64)
        assert not report.ok

    def test_untracked_model_is_a_note_not_a_failure(self, tmp_path):
        report = ModelCache(tmp_path).verify("NeoQuasar/Kronos-small", "")
        assert report.problems == ()
        assert any("not tracked" in note for note in report.notes)

    def test_load_refuses_to_use_a_corrupt_cache(self, tmp_path):
        cache = ModelCache(tmp_path)
        weights = tmp_path / "model.safetensors"
        weights.write_bytes(b"good")
        cache.record("NeoQuasar/Kronos-small", "", weights)
        weights.write_bytes(b"corrupted")
        runtime = KronosRuntime(
            KronosConfig(enabled=True, backend="stub"), cache=cache)
        status = runtime.load()
        assert status.state is KronosState.ERROR
        assert "integrity" in status.message


def make_path(steps: int, *, offset: float = 0.0, bad: str | None = None):
    """A synthetic trajectory used only to exercise forecast validation."""
    out = []
    for index in range(steps):
        close = 100.0 + index + offset
        values = {
            "open": close - 0.1,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": 1000.0,
        }
        if bad == "nan" and index == 1:
            values["close"] = float("nan")
        if bad == "ohlc" and index == 0:
            values["high"], values["low"] = 1.0, 900.0
        if bad == "volume" and index == 0:
            values["volume"] = -1.0
        out.append(values)
    return tuple(out)


class TestForecastShapeAndValidation:

    def test_valid_shape_passes(self):
        from axiom.forecast.validation import validate_forecast
        from axiom.kronos.backends import KronosInference

        config = KronosConfig(pred_len=6, sample_paths=2, lookback=40)
        stamps = future(6)
        inference = KronosInference((make_path(6), make_path(6, offset=1.0)), 12.0, "stub")
        check = validate_forecast(inference, stamps, config)
        assert check.ok, check.errors
        assert (check.paths, check.steps) == (2, 6)

    def test_wrong_path_count_is_rejected(self):
        from axiom.forecast.validation import validate_forecast
        from axiom.kronos.backends import KronosInference

        config = KronosConfig(pred_len=6, sample_paths=3, lookback=40)
        inference = KronosInference((make_path(6),), 1.0, "stub")
        check = validate_forecast(inference, future(6), config)
        assert not check.ok
        assert any("expected 3 sampled path" in error for error in check.errors)

    def test_wrong_horizon_is_rejected(self):
        from axiom.forecast.validation import validate_forecast
        from axiom.kronos.backends import KronosInference

        config = KronosConfig(pred_len=6, sample_paths=1, lookback=40)
        inference = KronosInference((make_path(5),), 1.0, "stub")
        check = validate_forecast(inference, future(6), config)
        assert not check.ok
        assert any("has 5 steps" in error for error in check.errors)

    def test_non_finite_output_is_rejected(self):
        from axiom.forecast.validation import validate_forecast
        from axiom.kronos.backends import KronosInference

        config = KronosConfig(pred_len=6, sample_paths=1, lookback=40)
        inference = KronosInference((make_path(6, bad="nan"),), 1.0, "stub")
        check = validate_forecast(inference, future(6), config)
        assert not check.ok
        assert any("non-finite" in error for error in check.errors)

    def test_structural_oddities_are_warnings_never_repairs(self):
        from axiom.forecast.validation import validate_forecast
        from axiom.kronos.backends import KronosInference

        config = KronosConfig(pred_len=6, sample_paths=1, lookback=40)
        check = validate_forecast(
            KronosInference((make_path(6, bad="ohlc"),), 1.0, "stub"), future(6), config)
        assert check.ok
        assert any("OHLC relationships" in warning for warning in check.warnings)

        check = validate_forecast(
            KronosInference((make_path(6, bad="volume"),), 1.0, "stub"), future(6), config)
        assert check.ok
        assert any("negative forecast volume" in warning for warning in check.warnings)

    def test_candles_are_labelled_as_forecast(self):
        from axiom.forecast.validation import build_forecast_candles

        candles = build_forecast_candles((make_path(3),), future(3))
        assert len(candles) == 3
        assert all(candle.origin == "KRONOS_FORECAST" for candle in candles)
        assert all(candle.is_forecast for candle in candles)
        assert set(candles[0].to_dict()) == {
            "timestamp", "forecast_open", "forecast_high", "forecast_low",
            "forecast_close", "forecast_volume", "origin"}

    def test_median_path_is_a_real_sampled_path(self):
        from axiom.forecast.validation import build_forecast_candles, median_path_index

        paths = (make_path(3, offset=-5.0), make_path(3), make_path(3, offset=5.0))
        assert median_path_index(paths) == 1
        candles = build_forecast_candles(paths, future(3))
        assert candles[-1].forecast_close == pytest.approx(102.0)

    def test_forecast_candle_rejects_a_market_origin_tag(self):
        from axiom.models import ForecastCandle

        with pytest.raises(ValueError, match="KRONOS_FORECAST"):
            ForecastCandle(datetime(2024, 1, 1, tzinfo=UTC), 1, 2, 0.5, 1.5, 10,
                           origin="MARKET_OBSERVED")


class TestSampling:
    def test_single_path_yields_no_uncertainty(self):
        assert summarize_paths([[100.0, 101.0]]) is None
        assert summarize_paths([[100.0], [102.0]]) is None

    def test_percentiles_are_ordered_and_uncalibrated(self):
        paths = [[100.0 + index * 0.1] for index in range(9)]
        summary = summarize_paths(paths)
        assert summary is not None
        assert summary.sample_count == 9
        assert summary.p10_close <= summary.p50_close <= summary.p90_close
        assert summary.calibrated is False
        assert summary.supported is True
        assert "not calibrated" in summary.label()
        assert len(summary.close_band) == 1

    def test_minimum_paths_required(self):
        assert MIN_PATHS_FOR_UNCERTAINTY == 3
        assert summarize_paths([[1.0], [2.0], [3.0]]) is not None

    def test_percentile_matches_linear_interpolation(self):
        assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)
        assert percentile([1.0, 2.0, 3.0, 4.0], 0.0) == 1.0
        assert percentile([1.0, 2.0, 3.0, 4.0], 1.0) == 4.0

    def test_coverage_and_width_are_empirical(self):
        paths = [[100.0, 90.0 + index * 10.0] for index in range(9)]
        covered = interval_coverage(paths, [100.0, 130.0])
        assert covered == 1.0
        missed = interval_coverage(paths, [100.0, 1000.0])
        assert missed == 0.5
        # step 0 has no spread, step 1 spans p10=98 … p90=162 → mean 32.
        assert mean_interval_width(paths) == pytest.approx(32.0)

    def test_non_finite_paths_yield_no_uncertainty(self):
        assert summarize_paths([[float("nan")], [1.0], [2.0]]) is None


# --- real Kronos implementation (requires torch + a Kronos checkout) ---------
TOKENIZER_KWARGS = dict(
    d_in=6, d_model=32, n_heads=4, ff_dim=64, n_enc_layers=2, n_dec_layers=2,
    ffn_dropout_p=0.1, attn_dropout_p=0.1, resid_dropout_p=0.1, s1_bits=4,
    s2_bits=4, beta=0.0, gamma0=10.0, gamma=0.2, zeta=0.05, group_size=4,
)
MODEL_KWARGS = dict(
    s1_bits=4, s2_bits=4, n_layers=2, d_model=32, n_heads=4, ff_dim=64,
    ffn_dropout_p=0.1, attn_dropout_p=0.1, resid_dropout_p=0.1,
    token_dropout_p=0.1, learn_te=True,
)


@pytest.fixture(scope="module")
def kronos_module():
    """The real Kronos implementation, if this machine has one."""
    source = os.environ.get("AXIOM_KRONOS_SOURCE", "")
    if not source:
        pytest.skip("set AXIOM_KRONOS_SOURCE to a Kronos checkout to run this")
    try:
        import torch  # noqa: F401
    except ImportError:
        pytest.skip("PyTorch is not installed in this runtime")
    if source not in sys.path:
        sys.path.insert(0, source)
    import model as kronos  # noqa: PLC0415 - the upstream package name

    return kronos


@pytest.fixture()
def stubbed_download(kronos_module, monkeypatch):
    """Replace the Hugging Face download with tiny random-weight modules."""

    def tokenizer_from_pretrained(cls, model_id, **kwargs):
        return kronos_module.KronosTokenizer(**TOKENIZER_KWARGS)

    def model_from_pretrained(cls, model_id, **kwargs):
        return kronos_module.Kronos(**MODEL_KWARGS)

    monkeypatch.setattr(kronos_module.KronosTokenizer, "from_pretrained",
                        classmethod(tokenizer_from_pretrained))
    monkeypatch.setattr(kronos_module.Kronos, "from_pretrained",
                        classmethod(model_from_pretrained))
    return kronos_module


class TestCpuInference:
    def test_real_kronos_predictor_runs_on_cpu(self, stubbed_download, tmp_path):
        config = KronosConfig(
            enabled=True, backend="upstream", lookback=32, pred_len=4,
            max_context=64, sample_paths=2, device="cpu", min_history=8,
            model_id="NeoQuasar/Kronos-custom",
        )
        backend = UpstreamKronosBackend()
        info = backend.load(config, cache=ModelCache(tmp_path))
        assert info.device == "cpu"
        assert info.torch_version
        assert info.max_context == 64

        window = bars(32)
        stamps = future(4, start=window[-1].timestamp + timedelta(hours=1))
        inference = backend.forecast(window, stamps, config)
        assert inference.backend == "upstream"
        assert inference.sample_paths == 2
        assert inference.pred_len == 4
        for path in inference.paths:
            assert len(path) == 4
            for step in path:
                assert set(step) >= {"open", "high", "low", "close", "volume"}
                assert all(
                    isinstance(value, float) for value in step.values())
        assert inference.latency_ms > 0
        assert any("single-path inference" in note for note in inference.notes)

    def test_real_kronos_output_survives_validation(self, stubbed_download, tmp_path):
        from axiom.forecast.validation import build_forecast_candles, validate_forecast
        from axiom.models import ForecastSnapshot, DataProvenance, MarketDataQuality

        config = KronosConfig(
            enabled=True, backend="upstream", lookback=32, pred_len=4,
            max_context=64, sample_paths=3, device="cpu", min_history=8,
            model_id="NeoQuasar/Kronos-custom",
        )
        backend = UpstreamKronosBackend()
        info = backend.load(config, cache=ModelCache(tmp_path))
        window = bars(32)
        stamps = future(4, start=window[-1].timestamp + timedelta(hours=1))
        inference = backend.forecast(window, stamps, config)

        check = validate_forecast(inference, stamps, config)
        assert check.ok, check.errors
        candles = build_forecast_candles(inference.paths, stamps)
        summary = summarize_paths(inference.close_paths())
        snapshot = ForecastSnapshot(
            instrument="DEMO",
            timeframe="1h",
            generated_at=datetime.now(UTC),
            candles=candles,
            config=config,
            provenance=DataProvenance(source="test", provider="replay",
                                      instrument="DEMO", timeframe="1h", bars=32),
            data_quality=MarketDataQuality(bars=32),
            model_id=info.model_id,
            model_revision=info.model_revision,
            device=info.device,
            backend=inference.backend,
            last_closed_timestamp=window[-1].timestamp,
            last_actual_close=window[-1].close,
            uncertainty=summary,
            latency_ms=inference.latency_ms,
        )
        assert snapshot.origin == "KRONOS_FORECAST"
        assert snapshot.horizon_return is not None
        assert summary is not None and summary.sample_count == 3
        payload = snapshot.to_dict()
        assert payload["candles"][0]["forecast_close"] == pytest.approx(
            candles[0].forecast_close)


class TestGpuInference:
    def test_gpu_inference_where_supported(self, stubbed_download, tmp_path):
        import torch

        if not torch.cuda.is_available():
            pytest.skip("CUDA is not available on this machine")
        config = KronosConfig(
            enabled=True, backend="upstream", lookback=32, pred_len=4,
            max_context=64, sample_paths=1, device="cuda:0", min_history=8,
            model_id="NeoQuasar/Kronos-custom",
        )
        backend = UpstreamKronosBackend()
        info = backend.load(config, cache=ModelCache(tmp_path))
        assert info.device.startswith("cuda")
        window = bars(32)
        stamps = future(4, start=window[-1].timestamp + timedelta(hours=1))
        assert backend.forecast(window, stamps, config).pred_len == 4

    def test_cuda_request_without_cuda_is_refused(self, monkeypatch):
        pytest.importorskip("torch")
        import torch

        from axiom.kronos.backends import resolve_device

        if torch.cuda.is_available():
            pytest.skip("CUDA is available; the refusal path cannot be exercised")
        monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
        with pytest.raises(KronosRuntimeUnavailable, match="CUDA is not available"):
            resolve_device("cuda:0")
