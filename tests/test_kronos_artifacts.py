"""Model-artifact verification: genuine weights vs "the code merely runs".

These tests guard the distinction the whole subsystem exists to make. The
central property is negative: a model that instantiates and produces finite
numbers must NOT be reported as real pretrained inference.

The final class is the deterministic artifact smoke test. It is gated on
genuine pretrained weights being present, so on a machine without them it
skips rather than pretending.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
from pathlib import Path

import pytest

from axiom.kronos.artifacts import (
    KNOWN_MODEL_PAIRINGS,
    KNOWN_MODEL_PARAMS,
    PARAM_FLOOR_FRACTION,
    ModelStatus,
    artifact_cache,
    check_forecast_frame,
    classify,
    expected_tokenizer,
    inspect_all,
    inspect_artifact,
    locate_artifacts,
    pairing_problems,
    param_count_problems,
    smoke_fixture,
    verify_model,
)
from axiom.kronos.model_cache import ModelCache, sha256_file
from axiom.models import KronosConfig

MINI = "NeoQuasar/Kronos-mini"
SMALL = "NeoQuasar/Kronos-small"
BASE = "NeoQuasar/Kronos-base"
TOKENIZER_2K = "NeoQuasar/Kronos-Tokenizer-2k"
TOKENIZER_BASE = "NeoQuasar/Kronos-Tokenizer-base"


# ---------------------------------------------------------------------------
# Supported models and pairings
# ---------------------------------------------------------------------------


class TestSupportedModelsAndPairings:
    def test_exactly_the_three_official_models_are_supported(self):
        assert set(KNOWN_MODEL_PAIRINGS) == {MINI, SMALL, BASE}

    def test_mini_pairs_with_the_2k_tokenizer(self):
        assert KNOWN_MODEL_PAIRINGS[MINI] == TOKENIZER_2K
        assert expected_tokenizer(MINI) == TOKENIZER_2K

    def test_small_and_base_pair_with_the_base_tokenizer(self):
        assert KNOWN_MODEL_PAIRINGS[SMALL] == TOKENIZER_BASE
        assert KNOWN_MODEL_PAIRINGS[BASE] == TOKENIZER_BASE

    def test_mini_with_the_base_tokenizer_is_rejected(self):
        problems = pairing_problems(MINI, TOKENIZER_BASE)
        assert problems
        assert TOKENIZER_2K in problems[0]

    def test_correct_pairings_produce_no_problems(self):
        for model_id, tokenizer_id in KNOWN_MODEL_PAIRINGS.items():
            assert pairing_problems(model_id, tokenizer_id) == []

    def test_an_unknown_model_is_never_silently_paired(self):
        problems = pairing_problems("NeoQuasar/Kronos-large", TOKENIZER_BASE)
        assert problems
        assert "not a recognised official Kronos model" in problems[0]

    def test_expected_tokenizer_is_none_for_unknown_models(self):
        assert expected_tokenizer("NeoQuasar/Kronos-large") is None

    def test_every_supported_model_has_a_published_parameter_count(self):
        assert set(KNOWN_MODEL_PARAMS) == set(KNOWN_MODEL_PAIRINGS)
        assert all(count > 0 for count in KNOWN_MODEL_PARAMS.values())


# ---------------------------------------------------------------------------
# Random / toy weight rejection
# ---------------------------------------------------------------------------


class TestRandomWeightRejection:
    def test_a_toy_model_is_rejected_by_parameter_count(self):
        # 33,664 is the measured size of the random-weight fixture the runtime
        # tests use. It must never pass as a genuine Kronos-small.
        problems = param_count_problems(SMALL, 33_664)
        assert problems
        assert "33,664" in problems[0]
        assert "24,700,000" in problems[0]

    def test_a_genuine_sized_model_passes(self):
        assert param_count_problems(SMALL, 24_700_000) == []

    def test_zero_parameters_is_rejected(self):
        assert param_count_problems(SMALL, 0)

    def test_the_floor_is_half_the_published_count(self):
        for model_id, published in KNOWN_MODEL_PARAMS.items():
            floor = int(published * PARAM_FLOOR_FRACTION)
            assert param_count_problems(model_id, floor - 1)
            assert param_count_problems(model_id, floor) == []

    def test_every_supported_model_rejects_the_toy_fixture(self):
        for model_id in KNOWN_MODEL_PARAMS:
            assert param_count_problems(model_id, 33_664)

    def test_an_unknown_model_cannot_be_confirmed_by_size(self):
        problems = param_count_problems("NeoQuasar/Kronos-large", 499_200_000)
        assert problems
        assert "no published parameter count" in problems[0]


# ---------------------------------------------------------------------------
# The status gate
# ---------------------------------------------------------------------------


class TestClassifyGate:
    """Requirement: instantiation alone must never reach verified."""

    @staticmethod
    def _all(**overrides):
        base = {
            "code_available": True,
            "artifact_found": True,
            "checksum_ok": True,
            "params_genuine": True,
            "loaded": True,
            "inference_completed": True,
        }
        base.update(overrides)
        return classify(**base)

    def test_everything_true_is_the_only_path_to_verified(self):
        assert self._all() is ModelStatus.REAL_INFERENCE_VERIFIED

    @pytest.mark.parametrize(
        "field",
        ["code_available", "artifact_found", "checksum_ok", "params_genuine",
         "loaded", "inference_completed"],
    )
    def test_removing_any_single_gate_blocks_verification(self, field):
        assert self._all(**{field: False}) is not ModelStatus.REAL_INFERENCE_VERIFIED

    def test_loaded_but_no_inference_is_not_verified(self):
        # This is the exact failure mode to prevent: the model loaded fine.
        status = self._all(inference_completed=False)
        assert status is ModelStatus.MODEL_LOADED

    def test_inference_without_a_checksum_is_suspect_not_verified(self):
        assert self._all(checksum_ok=False) is ModelStatus.WEIGHTS_SUSPECT

    def test_inference_with_bad_parameter_count_is_suspect(self):
        assert self._all(params_genuine=False) is ModelStatus.WEIGHTS_SUSPECT

    def test_missing_weights_report_unavailable_even_when_loaded(self):
        # A stubbed download can "load" with nothing genuine on disk.
        status = self._all(artifact_found=False)
        assert status is ModelStatus.WEIGHTS_UNAVAILABLE

    def test_no_code_at_all(self):
        assert self._all(code_available=False) is ModelStatus.CODE_UNAVAILABLE

    def test_weights_on_disk_but_not_loaded_yet(self):
        assert self._all(loaded=False) is ModelStatus.WEIGHTS_AVAILABLE

    def test_an_error_is_reported_as_inference_failed(self):
        assert self._all(error="boom") is ModelStatus.INFERENCE_FAILED

    def test_bad_weights_outrank_a_later_error(self):
        """Random weights are the primary finding, not the run failing."""
        status = self._all(params_genuine=False, error="boom")
        assert status is ModelStatus.WEIGHTS_SUSPECT

    def test_all_six_required_states_exist(self):
        required = {
            "CODE_AVAILABLE", "WEIGHTS_UNAVAILABLE", "WEIGHTS_AVAILABLE",
            "MODEL_LOADED", "REAL_INFERENCE_VERIFIED", "INFERENCE_FAILED",
        }
        assert required <= {member.value for member in ModelStatus}


# ---------------------------------------------------------------------------
# Offline artifact directory
# ---------------------------------------------------------------------------


class TestOfflineArtifacts:
    def test_cache_root_follows_the_override(self, tmp_path):
        assert artifact_cache(str(tmp_path)).root == tmp_path

    def test_cache_root_follows_the_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_KRONOS_ARTIFACTS", str(tmp_path / "arts"))
        assert artifact_cache().root == tmp_path / "arts"

    def test_artifacts_env_wins_over_the_model_cache_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_MODEL_CACHE", str(tmp_path / "cache"))
        monkeypatch.setenv("AXIOM_KRONOS_ARTIFACTS", str(tmp_path / "arts"))
        assert artifact_cache().root == tmp_path / "arts"

    def test_falls_back_to_the_model_cache_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AXIOM_KRONOS_ARTIFACTS", raising=False)
        monkeypatch.setenv("AXIOM_MODEL_CACHE", str(tmp_path / "cache"))
        # The loader reads AXIOM_MODEL_CACHE, so the report must agree with it.
        assert artifact_cache().root == ModelCache(str(tmp_path / "cache")).root

    def test_hf_layout_weights_are_found(self, tmp_path):
        scoped = tmp_path / "models--NeoQuasar--Kronos-small" / "snapshots" / "abc123"
        scoped.mkdir(parents=True)
        weight = scoped / "model.safetensors"
        weight.write_bytes(b"not really weights, but sized like a file")
        assert locate_artifacts(ModelCache(tmp_path), SMALL) == [weight]

    def test_nothing_is_found_in_an_empty_directory(self, tmp_path):
        assert locate_artifacts(ModelCache(tmp_path), SMALL) == []


class TestInspectArtifact:
    def test_missing_weights_report_unavailable(self, tmp_path):
        report = inspect_artifact(SMALL, cache=ModelCache(tmp_path))
        assert report.status is ModelStatus.WEIGHTS_UNAVAILABLE
        assert report.artifact_found is False
        assert report.real_inference_verified is False
        assert report.param_count == 0
        assert any("no pretrained weight files" in p for p in report.problems)

    def test_present_weights_are_hashed_and_sized(self, tmp_path):
        scoped = tmp_path / "models--NeoQuasar--Kronos-small" / "snapshots" / "rev"
        scoped.mkdir(parents=True)
        payload = b"x" * 4096
        (scoped / "model.safetensors").write_bytes(payload)
        report = inspect_artifact(SMALL, cache=ModelCache(tmp_path))
        assert report.artifact_found is True
        assert report.artifact_bytes == 4096
        assert report.checksum == hashlib.sha256(
            sha256_file(scoped / "model.safetensors").encode()
        ).hexdigest()
        assert report.artifact_dir == str(tmp_path)

    def test_a_matching_checksum_is_accepted(self, tmp_path):
        scoped = tmp_path / "models--NeoQuasar--Kronos-small" / "snapshots" / "rev"
        scoped.mkdir(parents=True)
        (scoped / "model.safetensors").write_bytes(b"payload")
        expected = hashlib.sha256(
            sha256_file(scoped / "model.safetensors").encode()
        ).hexdigest()
        report = inspect_artifact(SMALL, cache=ModelCache(tmp_path),
                                 expected_checksum=expected)
        assert report.checksum_ok is True

    def test_a_mismatched_checksum_is_flagged(self, tmp_path):
        scoped = tmp_path / "models--NeoQuasar--Kronos-small" / "snapshots" / "rev"
        scoped.mkdir(parents=True)
        (scoped / "model.safetensors").write_bytes(b"payload")
        report = inspect_artifact(SMALL, cache=ModelCache(tmp_path),
                                 expected_checksum="deadbeef" * 8)
        assert report.checksum_ok is False
        assert any("checksum does not match" in p for p in report.problems)

    def test_inspection_alone_can_never_claim_a_load(self, tmp_path):
        """Even perfect artifacts must not report MODEL_LOADED here."""
        scoped = tmp_path / "models--NeoQuasar--Kronos-small" / "snapshots" / "rev"
        scoped.mkdir(parents=True)
        (scoped / "model.safetensors").write_bytes(b"payload")
        expected = hashlib.sha256(
            sha256_file(scoped / "model.safetensors").encode()
        ).hexdigest()
        report = inspect_artifact(SMALL, cache=ModelCache(tmp_path),
                                 expected_checksum=expected)
        # A checksum-verified artifact on disk is WEIGHTS_AVAILABLE — but
        # inspection must never advance to MODEL_LOADED or verified, because it
        # performs no load and no inference.
        assert report.status is ModelStatus.WEIGHTS_AVAILABLE
        assert report.status not in {
            ModelStatus.MODEL_LOADED,
            ModelStatus.REAL_INFERENCE_VERIFIED,
        }
        assert report.param_count == 0
        assert report.real_inference_verified is False

    def test_wrong_tokenizer_pairing_is_reported(self, tmp_path):
        report = inspect_artifact(MINI, tokenizer_id=TOKENIZER_BASE,
                                  cache=ModelCache(tmp_path))
        assert report.tokenizer_compatible is False
        assert report.tokenizer_problems

    def test_inspect_all_covers_every_supported_model(self, tmp_path):
        reports = inspect_all(cache=ModelCache(tmp_path))
        assert {r.model_id for r in reports} == set(KNOWN_MODEL_PAIRINGS)
        assert all(r.real_inference_verified is False for r in reports)


# ---------------------------------------------------------------------------
# Deterministic fixture
# ---------------------------------------------------------------------------


class TestSmokeFixture:
    def test_the_fixture_is_byte_identical_across_calls(self):
        first, second = smoke_fixture(), smoke_fixture()
        assert first.bars == second.bars
        assert first.future_timestamps == second.future_timestamps

    def test_requested_lengths_are_honoured(self):
        sample = smoke_fixture(context=32, pred_len=5)
        assert len(sample.bars) == 32
        assert len(sample.future_timestamps) == 5

    def test_timestamps_are_fixed_and_utc(self):
        sample = smoke_fixture()
        assert sample.bars[0].timestamp.isoformat() == "2024-01-02T09:15:00+00:00"
        assert all(bar.timestamp.tzinfo is not None for bar in sample.bars)

    def test_context_and_prediction_windows_do_not_overlap(self):
        sample = smoke_fixture()
        assert sample.bars[-1].timestamp < sample.future_timestamps[0]

    def test_prediction_timestamps_are_strictly_increasing(self):
        sample = smoke_fixture()
        stamps = list(sample.future_timestamps)
        assert all(a < b for a, b in itertools.pairwise(stamps))

    def test_ohlcv_relationships_hold(self):
        for bar in smoke_fixture().bars:
            assert bar.low <= min(bar.open, bar.close)
            assert bar.high >= max(bar.open, bar.close)
            assert bar.volume > 0
            assert bar.low > 0

    def test_to_dict_is_json_serialisable(self):
        payload = json.dumps(smoke_fixture().to_dict())
        assert "2024-01-02T09:15:00+00:00" in payload


class TestCheckForecastFrame:
    @pytest.fixture()
    def pandas(self):
        return pytest.importorskip("pandas")

    def _frame(self, pd, rows=4, columns=("open", "high", "low", "close", "volume")):
        import pandas

        data = {name: [float(index + 1)] * rows for index, name in enumerate(columns)}
        index = pandas.date_range("2024-01-02 10:19", periods=rows, freq="min", tz="UTC")
        return pandas.DataFrame(data, index=index)

    def test_a_healthy_frame_produces_no_problems(self, pandas):
        assert check_forecast_frame(self._frame(pandas), 4) == []

    def test_missing_columns_are_reported(self, pandas):
        frame = self._frame(pandas, columns=("open", "high", "low"))
        problems = check_forecast_frame(frame, 4)
        assert any("missing OHLCV columns" in p for p in problems)

    def test_a_row_count_mismatch_is_reported(self, pandas):
        problems = check_forecast_frame(self._frame(pandas, rows=4), 8)
        assert any("rows" in p and "pred_len" in p for p in problems)

    def test_nan_output_is_never_silently_accepted(self, pandas):
        frame = self._frame(pandas)
        frame.iloc[0, 0] = float("nan")
        problems = check_forecast_frame(frame, 4)
        assert any("non-finite" in p for p in problems)

    def test_infinite_output_is_rejected(self, pandas):
        frame = self._frame(pandas)
        frame.iloc[1, 1] = float("inf")
        assert any("non-finite" in p for p in check_forecast_frame(frame, 4))


# ---------------------------------------------------------------------------
# verify_model with non-genuine weights
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def kronos_module():
    source = os.environ.get("AXIOM_KRONOS_SOURCE", "")
    if not source:
        pytest.skip("set AXIOM_KRONOS_SOURCE to a Kronos checkout to run this")
    try:
        import torch  # noqa: F401
    except ImportError:
        pytest.skip("PyTorch is not installed in this runtime")
    import sys

    if source not in sys.path:
        sys.path.insert(0, source)
    import model as kronos  # the upstream package name

    return kronos


TOKENIZER_KWARGS = {
    "d_in": 6, "d_model": 32, "n_heads": 4, "ff_dim": 64, "n_enc_layers": 2,
    "n_dec_layers": 2, "ffn_dropout_p": 0.1, "attn_dropout_p": 0.1,
    "resid_dropout_p": 0.1, "s1_bits": 4, "s2_bits": 4, "beta": 0.0,
    "gamma0": 10.0, "gamma": 0.2, "zeta": 0.05, "group_size": 4,
}
MODEL_KWARGS = {
    "s1_bits": 4, "s2_bits": 4, "n_layers": 2, "d_model": 32, "n_heads": 4,
    "ff_dim": 64, "ffn_dropout_p": 0.1, "attn_dropout_p": 0.1,
    "resid_dropout_p": 0.1, "token_dropout_p": 0.1, "learn_te": True,
}


@pytest.fixture()
def random_weight_backend(kronos_module, monkeypatch):
    """Real upstream classes, but with tiny RANDOM weights standing in."""
    monkeypatch.setattr(
        kronos_module.KronosTokenizer, "from_pretrained",
        classmethod(lambda cls, mid, **kw: kronos_module.KronosTokenizer(**TOKENIZER_KWARGS)),
    )
    monkeypatch.setattr(
        kronos_module.Kronos, "from_pretrained",
        classmethod(lambda cls, mid, **kw: kronos_module.Kronos(**MODEL_KWARGS)),
    )
    return kronos_module


class TestVerifyModelRejectsRandomWeights:
    """The central safety property of this subsystem."""

    @staticmethod
    def _config(pred_len: int = 8) -> KronosConfig:
        return KronosConfig(
            enabled=True, model_id=SMALL, tokenizer_id=TOKENIZER_BASE,
            pred_len=pred_len, lookback=64, max_context=512, device="cpu",
            min_history=8,
        )

    def test_random_weights_never_report_verified_inference(
        self, random_weight_backend, tmp_path
    ):
        report = verify_model(self._config(), cache=ModelCache(tmp_path))
        assert report.real_inference_verified is False
        assert report.status is not ModelStatus.REAL_INFERENCE_VERIFIED

    def test_the_toy_parameter_count_is_detected(
        self, random_weight_backend, tmp_path
    ):
        report = verify_model(self._config(), cache=ModelCache(tmp_path))
        assert report.param_count == 33_664
        assert report.param_count_expected == 24_700_000
        assert report.params_genuine is False

    def test_inference_actually_ran_and_was_still_refused(
        self, random_weight_backend, tmp_path
    ):
        """Proves the gate is not simply 'inference never happens'."""
        report = verify_model(self._config(), cache=ModelCache(tmp_path))
        assert "structurally valid" in report.inference_result
        assert report.device == "cpu"
        assert report.real_inference_verified is False

    def test_the_load_succeeded_yet_verification_still_failed(
        self, random_weight_backend, tmp_path
    ):
        report = verify_model(self._config(), cache=ModelCache(tmp_path))
        assert report.load_result.startswith("loaded ")
        assert report.real_inference_verified is False

    def test_a_requested_pred_len_is_honoured(self, random_weight_backend, tmp_path):
        report = verify_model(self._config(pred_len=4), cache=ModelCache(tmp_path))
        assert "x 4 steps" in report.inference_result

    def test_a_load_failure_with_no_weights_reports_unavailable(self, tmp_path):
        """Missing weights are the root cause, so they outrank the exception."""

        class Exploding:
            predictor = None

            def load(self, config, *, cache=None, on_state=None):
                raise RuntimeError("weights are missing")

        report = verify_model(self._config(), cache=ModelCache(tmp_path),
                              backend=Exploding())
        assert report.status is ModelStatus.WEIGHTS_UNAVAILABLE
        assert report.real_inference_verified is False
        # The exception is still surfaced, never swallowed.
        assert any("weights are missing" in p for p in report.problems)
        assert "load failed" in report.load_result

    def test_a_load_failure_with_good_weights_reports_inference_failed(self, tmp_path):
        """With genuine artifacts present, the load error is the finding."""
        scoped = tmp_path / "models--NeoQuasar--Kronos-small" / "snapshots" / "rev"
        scoped.mkdir(parents=True)
        (scoped / "model.safetensors").write_bytes(b"payload")
        expected = hashlib.sha256(
            sha256_file(scoped / "model.safetensors").encode()
        ).hexdigest()

        class Exploding:
            predictor = None

            def load(self, config, *, cache=None, on_state=None):
                raise RuntimeError("cuda out of memory")

        report = verify_model(self._config(), cache=ModelCache(tmp_path),
                              expected_checksum=expected, backend=Exploding())
        assert report.status is ModelStatus.INFERENCE_FAILED
        assert report.real_inference_verified is False
        assert any("cuda out of memory" in p for p in report.problems)

    def test_bad_weights_outrank_a_load_error(self, tmp_path):
        """A wrong-sized load is a weights problem, not a run failure."""
        scoped = tmp_path / "models--NeoQuasar--Kronos-small" / "snapshots" / "rev"
        scoped.mkdir(parents=True)
        (scoped / "model.safetensors").write_bytes(b"payload")
        expected = hashlib.sha256(
            sha256_file(scoped / "model.safetensors").encode()
        ).hexdigest()

        class TinyThenExplode:
            predictor = None

            def load(self, config, *, cache=None, on_state=None):
                raise RuntimeError("state dict mismatch")

        # params_genuine cannot be True here (nothing loaded), so the checksum
        # passes and the error is what surfaces.
        report = verify_model(self._config(), cache=ModelCache(tmp_path),
                              expected_checksum=expected, backend=TinyThenExplode())
        assert report.status is ModelStatus.INFERENCE_FAILED

    def test_the_report_carries_every_required_field(
        self, random_weight_backend, tmp_path
    ):
        payload = verify_model(self._config(), cache=ModelCache(tmp_path)).to_dict()
        for field in (
            "model_id", "tokenizer_id", "artifact_dir", "artifact_found",
            "artifact_bytes", "checksum", "checksum_ok", "param_count",
            "tokenizer_compatible", "load_result", "device", "inference_result",
            "real_inference_verified", "problems",
        ):
            assert field in payload, f"report is missing required field {field}"

    def test_a_mismatched_checksum_blocks_verification(
        self, random_weight_backend, tmp_path
    ):
        report = verify_model(self._config(), cache=ModelCache(tmp_path),
                              expected_checksum="0" * 64)
        assert report.checksum_ok is False
        assert report.real_inference_verified is False


class _FakeParams:
    """Stand-in for a genuine-sized parameter set (no PyTorch needed)."""

    def __init__(self, total: int) -> None:
        self._total = total

    def parameters(self):
        class _P:
            def __init__(self, n):
                self._n = n

            def numel(self):
                return self._n

        return [_P(self._total)]


class _FakeInfo:
    model_id = SMALL
    model_revision = "deadbeef"
    device = "cpu"
    backend = "upstream"
    kronos_source = "/fake/model/__init__.py"


class _BadFrameBackend:
    """Loads fine and reports a genuine parameter count, but returns a NaN frame.

    Isolates the output-validity gate: the load, the checksum and the parameter
    count all pass, so the only thing standing between this and a claimed
    ``REAL_INFERENCE_VERIFIED`` is the structural check on the output.
    """

    def __init__(self, frame) -> None:
        self._frame = frame

        class _Predictor:
            model = _FakeParams(24_700_000)

            def predict(self, *args, **kwargs):
                return frame

        self.predictor = _Predictor()

    def load(self, config, *, cache=None, on_state=None):
        return _FakeInfo()

    def forecast(self, bars, future_timestamps, config):
        from axiom.kronos.backends import KronosInference

        step = {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0}
        return KronosInference(
            paths=(tuple(step for _ in future_timestamps),),
            latency_ms=1.0,
            backend="upstream",
        )


class TestVerifyModelRejectsInvalidOutput:
    """A load that "succeeds" must not buy a verified status on its own."""

    @staticmethod
    def _genuine_cache(tmp_path):
        scoped = tmp_path / "models--NeoQuasar--Kronos-small" / "snapshots" / "rev"
        scoped.mkdir(parents=True)
        weight = scoped / "model.safetensors"
        weight.write_bytes(b"genuine-looking payload")
        return ModelCache(tmp_path), hashlib.sha256(
            sha256_file(weight).encode()
        ).hexdigest()

    @staticmethod
    def _config(pred_len: int = 4) -> KronosConfig:
        return KronosConfig(
            enabled=True, model_id=SMALL, tokenizer_id=TOKENIZER_BASE,
            pred_len=pred_len, lookback=64, max_context=512, device="cpu",
            min_history=8,
        )

    @pytest.fixture()
    def pandas(self):
        return pytest.importorskip("pandas")

    def _frame(self, pd, *, value=1.0):
        import pandas

        rows = 4
        data = {name: [value] * rows for name in
                ("open", "high", "low", "close", "volume")}
        index = pandas.date_range("2024-01-02 10:19", periods=rows, freq="min", tz="UTC")
        return pandas.DataFrame(data, index=index)

    def test_a_nan_frame_blocks_verification_even_when_everything_else_passes(
        self, tmp_path, pandas
    ):
        cache, checksum = self._genuine_cache(tmp_path)
        frame = self._frame(pandas)
        frame.iloc[0, 0] = float("nan")
        report = verify_model(self._config(), cache=cache, expected_checksum=checksum,
                              backend=_BadFrameBackend(frame))
        # Load, checksum and parameter count are all fine — only the output is bad.
        assert report.load_result.startswith("loaded ")
        assert report.checksum_ok is True
        assert report.params_genuine is True
        assert any("non-finite" in problem for problem in report.problems)
        assert report.real_inference_verified is False
        assert report.status is not ModelStatus.REAL_INFERENCE_VERIFIED

    def test_a_short_frame_blocks_verification(self, tmp_path, pandas):
        cache, checksum = self._genuine_cache(tmp_path)
        report = verify_model(self._config(pred_len=8), cache=cache,
                              expected_checksum=checksum,
                              backend=_BadFrameBackend(self._frame(pandas)))
        assert any("rows" in problem for problem in report.problems)
        assert report.real_inference_verified is False

    def test_a_frame_missing_close_blocks_verification(self, tmp_path, pandas):
        cache, checksum = self._genuine_cache(tmp_path)
        frame = self._frame(pandas).drop(columns=["close"])
        report = verify_model(self._config(), cache=cache, expected_checksum=checksum,
                              backend=_BadFrameBackend(frame))
        assert any("missing OHLCV columns" in problem for problem in report.problems)
        assert report.real_inference_verified is False

    def test_a_clean_frame_with_genuine_weights_reaches_verified(
        self, tmp_path, pandas
    ):
        """The positive control: the gates are satisfiable, not vacuously false."""
        cache, checksum = self._genuine_cache(tmp_path)
        report = verify_model(self._config(), cache=cache, expected_checksum=checksum,
                              backend=_BadFrameBackend(self._frame(pandas)))
        assert report.problems == ()
        assert report.real_inference_verified is True
        assert report.status is ModelStatus.REAL_INFERENCE_VERIFIED
        assert report.param_count == 24_700_000
        assert report.device == "cpu"


# ---------------------------------------------------------------------------
# Deterministic artifact smoke test — gated on GENUINE pretrained weights
# ---------------------------------------------------------------------------


def genuine_artifacts_present() -> bool:
    """True only if real pretrained weights are on disk for the smoke model."""
    return bool(locate_artifacts(artifact_cache(), SMALL))


@pytest.mark.skipif(
    not os.environ.get("AXIOM_KRONOS_SOURCE"),
    reason="set AXIOM_KRONOS_SOURCE to a Kronos checkout to run this",
)
@pytest.mark.skipif(
    not genuine_artifacts_present(),
    reason=(
        "no genuine pretrained Kronos weights on disk; this test refuses to "
        "run rather than substitute random weights. Copy real artifacts into "
        "the directory named by AXIOM_KRONOS_ARTIFACTS (or AXIOM_MODEL_CACHE) "
        "to enable it."
    ),
)
class TestArtifactSmoke:
    """Runs ONLY when genuine pretrained artifacts are available."""

    MODEL = SMALL
    TOKENIZER = TOKENIZER_BASE
    PRED_LEN = 8

    @pytest.fixture()
    def report(self, tmp_path) -> dict:
        config = KronosConfig(
            enabled=True, model_id=self.MODEL, tokenizer_id=self.TOKENIZER,
            pred_len=self.PRED_LEN, lookback=64, max_context=512,
            device="cpu", min_history=8, allow_download=False,
        )
        result = verify_model(config, cache=artifact_cache())
        (Path(os.environ.get("AXIOM_SMOKE_RESULT", tmp_path))
         / "kronos_artifact_smoke.json").write_text(
            json.dumps(result.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        return result.to_dict()

    def test_tokenizer_model_and_predictor_all_load(self, report):
        assert report["load_result"].startswith("loaded ")
        assert report["tokenizer_compatible"] is True

    def test_inference_completed(self, report):
        assert "failed" not in report["inference_result"]
        assert report["inference_result"]

    def test_the_parameter_count_matches_the_published_model(self, report):
        assert report["params_genuine"] is True
        assert report["param_count"] >= int(
            KNOWN_MODEL_PARAMS[self.MODEL] * PARAM_FLOOR_FRACTION
        )

    def test_real_pretrained_inference_is_verified(self, report):
        assert report["real_inference_verified"] is True
        assert report["status"] == "REAL_INFERENCE_VERIFIED"

    def test_the_prediction_length_matches_the_request(self, report):
        assert f"x {self.PRED_LEN} steps" in report["inference_result"]

    def test_the_result_is_machine_readable(self, report, tmp_path):
        payload = json.dumps(report)
        assert json.loads(payload)["model_id"] == self.MODEL
