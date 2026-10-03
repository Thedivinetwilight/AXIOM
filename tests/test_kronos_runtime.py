"""Kronos runtime: configuration, lifecycle, model loading, cache, inference.

The CPU inference tests run against the REAL Kronos implementation when
``AXIOM_KRONOS_SOURCE`` points at a checkout of https://github.com/shiyu-coder/Kronos
and PyTorch is installed. Weights cannot be downloaded in an offline sandbox, so
those tests build the real architecture with small dimensions and random weights
(the download is stubbed, the tensor path is real).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from axiom.models import FORECAST_ORIGIN, KronosConfig, KronosState, KronosStatus

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
        """A bogus AXIOM_KRONOS_SOURCE must be reported, whichever check fires.

        ``load()`` imports torch before it resolves the Kronos source, so on a
        host without PyTorch the dependency error surfaces first. Both outcomes
        are honest refusals; neither may load a model.
        """
        monkeypatch.setenv("AXIOM_KRONOS_SOURCE", "/definitely/not/a/kronos/checkout")
        with pytest.raises(KronosRuntimeUnavailable) as excinfo:
            UpstreamKronosBackend().load(KronosConfig(enabled=True))
        assert ("does not look like" in str(excinfo.value)
                or "PyTorch is not installed" in str(excinfo.value)), str(excinfo.value)

    def test_a_bogus_source_is_rejected_without_needing_torch(self, monkeypatch):
        """The source contract itself, independent of the Kronos dependencies."""
        from axiom.kronos.backends import kronos_checkout_root

        monkeypatch.setenv("AXIOM_KRONOS_SOURCE", "/definitely/not/a/kronos/checkout")
        with pytest.raises(KronosRuntimeUnavailable, match="does not look like"):
            kronos_checkout_root("/definitely/not/a/kronos/checkout")


class TestKronosModuleDiscovery:
    """The model implementation may only come from explicitly trusted locations.

    ``model`` is a generic module name, so scanning sys.path would let any
    unrelated package that happens to define Kronos/KronosTokenizer/
    KronosPredictor stand in for the real implementation — code that runs in the
    process which loads the weights.
    """

    DECOY = (
        "class Kronos:\n    pass\n"
        "class KronosTokenizer:\n    pass\n"
        "class KronosPredictor:\n    pass\n"
    )

    def test_a_decoy_package_on_sys_path_is_never_imported(self, tmp_path, monkeypatch):
        import axiom.kronos.backends as backends

        (tmp_path / "model.py").write_text(self.DECOY)
        monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
        monkeypatch.delenv("AXIOM_KRONOS_SOURCE", raising=False)
        monkeypatch.setattr(sys, "modules", {k: v for k, v in sys.modules.items()
                                             if not k.startswith("model")})
        monkeypatch.setattr(backends, "installed_kronos_available", lambda: False)

        with pytest.raises(KronosRuntimeUnavailable, match="not found"):
            backends.import_kronos_modules()
        assert "model" not in sys.modules, "the decoy must not be executed at all"

    def test_a_cached_foreign_model_module_is_refused(self, tmp_path, monkeypatch):
        """A different 'model' already in sys.modules must not be trusted."""
        import axiom.kronos.backends as backends

        source = os.environ.get("AXIOM_KRONOS_SOURCE", "")
        if not source:
            pytest.skip("needs a real Kronos checkout (AXIOM_KRONOS_SOURCE)")

        foreign = tmp_path / "model.py"
        foreign.write_text(self.DECOY)
        spec = importlib.util.spec_from_file_location("model", foreign)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setitem(sys.modules, "model", module)

        with pytest.raises(KronosRuntimeUnavailable, match="outside AXIOM_KRONOS_SOURCE"):
            backends.import_kronos_modules()

    def test_the_trusted_checkout_is_still_used(self, monkeypatch):
        import axiom.kronos.backends as backends

        source = os.environ.get("AXIOM_KRONOS_SOURCE", "")
        if not source:
            pytest.skip("needs a real Kronos checkout (AXIOM_KRONOS_SOURCE)")
        root = backends.kronos_checkout_root(source)

        Kronos, KronosTokenizer, KronosPredictor, location = (
            backends.import_kronos_modules()
        )
        assert callable(Kronos) and callable(KronosTokenizer) and callable(KronosPredictor)
        assert Path(location).resolve().is_relative_to(root), location

    def test_checkout_root_accepts_the_model_directory_itself(self, tmp_path):
        import axiom.kronos.backends as backends

        (tmp_path / "model").mkdir()
        (tmp_path / "model" / "kronos.py").write_text("# kronos\n")
        assert backends.kronos_checkout_root(str(tmp_path / "model")) == tmp_path.resolve()
        assert backends.kronos_checkout_root(str(tmp_path)) == tmp_path.resolve()

    def test_an_empty_source_falls_back_to_the_installed_distribution(self, monkeypatch):
        import axiom.kronos.backends as backends

        monkeypatch.delenv("AXIOM_KRONOS_SOURCE", raising=False)
        monkeypatch.setattr(backends, "installed_kronos_available", lambda: False)
        with pytest.raises(KronosRuntimeUnavailable, match="install a 'kronos' distribution"):
            backends.import_kronos_modules()

    def test_an_incomplete_package_is_rejected_not_partially_used(self, tmp_path, monkeypatch):
        """A module exposing only some of the names is not a Kronos implementation."""
        import axiom.kronos.backends as backends

        (tmp_path / "model").mkdir()
        (tmp_path / "model" / "__init__.py").write_text("class Kronos:\n    pass\n")
        (tmp_path / "model" / "kronos.py").write_text("# kronos\n")
        monkeypatch.setenv("AXIOM_KRONOS_SOURCE", str(tmp_path))
        monkeypatch.setattr(sys, "modules", {k: v for k, v in sys.modules.items()
                                             if not k.startswith("model")})
        with pytest.raises(KronosRuntimeUnavailable, match="does not export"):
            backends.import_kronos_modules()


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

    def _point_home_at(self, monkeypatch, path):
        """Aim `~` at `path` the way this operating system resolves it.

        posixpath.expanduser() reads HOME; ntpath.expanduser() reads
        USERPROFILE first, so patching HOME alone is a no-op on Windows and
        the test would silently assert the runner's real profile instead.
        """
        monkeypatch.setenv("HOME", str(path))
        if os.name == "nt":
            monkeypatch.setenv("USERPROFILE", str(path))

    def test_a_tilde_in_the_override_is_expanded(self, monkeypatch, tmp_path):
        """AXIOM_MODEL_CACHE=~/weights must not create a literal '~' folder.

        Without expanduser() the cache would be created at '<cwd>/~/weights',
        which is a real directory named '~' rather than the user's home.
        """
        self._point_home_at(monkeypatch, tmp_path)
        monkeypatch.setenv("AXIOM_MODEL_CACHE", "~/axiom-weights")
        assert default_cache_root() == tmp_path / "axiom-weights"

    def test_a_tilde_in_xdg_cache_home_is_expanded(self, monkeypatch, tmp_path):
        from axiom.kronos.model_cache import cache_root_for

        self._point_home_at(monkeypatch, tmp_path)
        root = cache_root_for(
            override=None, os_name="posix", platform="linux",
            localappdata=None, xdg_cache_home="~/.cache-axiom", home=tmp_path)
        assert root == tmp_path / ".cache-axiom" / "AXIOM" / "models" / "kronos"

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

    def test_a_truncated_file_reports_the_size_mismatch(self, tmp_path):
        """Size and digest are reported separately; truncation names itself.

        Found by mutation: neutralising the size check left the suite green, because
        SHA-256 also fires on any content change — so the size check can never be the
        sole detector. It is still worth pinning: 'recorded 16 bytes, found 5' tells an
        operator the file was truncated, whereas a bare digest mismatch does not
        distinguish truncation from tampering.
        """
        cache = ModelCache(tmp_path)
        weights = tmp_path / "model.safetensors"
        weights.write_bytes(b"original-payload")
        cache.record("m", "r", weights)
        weights.write_bytes(b"short")
        report = cache.verify("m", "r")
        assert not report.ok
        assert any("size mismatch" in p and "recorded 16 bytes, found 5" in p
                   for p in report.problems), report.problems
        # Both signals are present: size is diagnostic, SHA-256 is authoritative.
        assert any("SHA-256 mismatch" in p for p in report.problems)

    def test_untracked_weights_with_a_configured_digest_is_a_problem(self, tmp_path):
        """A configured digest with nothing cached must be a PROBLEM, not a note.

        Found by mutation: dropping this branch left the suite green. Without it the
        untracked case falls into the informational branch and reports 'integrity will
        be recorded after download' — reassuring wording for an operator who explicitly
        pinned a digest and is getting no verification at all. ``ok`` is False either
        way, so the danger is a misread, not a bypass.
        """
        report = ModelCache(tmp_path).verify(
            "NeoQuasar/Kronos-small", "rev", expected_sha256="0" * 64)
        assert not report.ok
        assert any("no cached weights" in p for p in report.problems), report.problems
        assert report.notes == (), "must not be softened into a note"

        # Contrast: with no digest configured the same state IS only informational.
        plain = ModelCache(tmp_path).verify("NeoQuasar/Kronos-small", "rev")
        assert plain.problems == ()
        assert any("not tracked" in n for n in plain.notes)

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


class TestModelCachePathConfinement:
    """The manifest is untrusted input; verification must stay inside the root.

    Regression coverage for a real defect: verify() built its path with
    ``self.root / entry.relative_path``, and ``Path`` discards the left operand
    when the right side is absolute. A manifest naming an absolute path — or a
    ``../`` chain — made the integrity check read, and pass, a file planted
    anywhere on disk. Since the digest is compared against the same file, the
    check could not fail: attacker-chosen weights verified as legitimate.
    """

    @staticmethod
    def _tamper(root: Path, target: Path, relative_path: str) -> None:
        import json

        payload = {
            "schema": 1,
            "entries": {
                "NeoQuasar/Kronos-small@planted": {
                    "model_id": "NeoQuasar/Kronos-small",
                    "revision": "planted",
                    "file_name": target.name,
                    "relative_path": relative_path,
                    "size_bytes": target.stat().st_size,
                    "sha256": sha256_file(target),
                    "downloaded_at": "2024-01-01T00:00:00+00:00",
                }
            },
        }
        (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_an_absolute_manifest_path_is_refused(self, tmp_path):
        root = tmp_path / "cache"
        root.mkdir()
        planted = tmp_path / "planted.bin"
        planted.write_bytes(b"attacker controlled weights")
        self._tamper(root, planted, str(planted))

        report = ModelCache(root).verify("NeoQuasar/Kronos-small", "planted")
        assert not report.ok, "verification passed for a file outside the cache"
        assert any("outside the model cache" in p for p in report.problems)

    def test_a_traversal_manifest_path_is_refused(self, tmp_path):
        root = tmp_path / "cache"
        root.mkdir()
        planted = tmp_path / "planted.bin"
        planted.write_bytes(b"attacker controlled weights")
        self._tamper(root, planted, "../" + planted.name)

        report = ModelCache(root).verify("NeoQuasar/Kronos-small", "planted")
        assert not report.ok
        assert any("outside the model cache" in p for p in report.problems)

    def test_a_home_relative_manifest_path_is_expanded_then_refused(self, tmp_path):
        """``~`` is expanded before the confinement check, so it cannot hide inside.

        Found by mutation: dropping ``.expanduser()`` left the suite green. Both forms
        fail closed, but for different reasons — unexpanded, ``~/planted.bin`` resolves
        to ``<cache>/~/planted.bin``, i.e. INSIDE the cache root, where it passes
        confinement and is then reported merely as a missing file. Expanded, it resolves
        to a real path outside the cache and is correctly refused as an escape. Only the
        expanded form tells the operator that a manifest tried to point outside.
        """
        root = tmp_path / "cache"
        root.mkdir()
        planted = tmp_path / "planted.bin"
        planted.write_bytes(b"attacker controlled weights")
        self._tamper(root, planted, "~/planted.bin")

        with pytest.raises(ValueError, match="outside the model cache"):
            ModelCache(root).resolve_within_root("~/planted.bin")

        report = ModelCache(root).verify("NeoQuasar/Kronos-small", "planted")
        assert not report.ok
        assert any("outside the model cache" in p for p in report.problems), report.problems

    def test_a_tampered_manifest_blocks_the_load(self, tmp_path):
        """The runtime must refuse, not quietly load the planted file."""
        root = tmp_path / "cache"
        root.mkdir()
        planted = tmp_path / "planted.bin"
        planted.write_bytes(b"attacker controlled weights")
        self._tamper(root, planted, str(planted))

        runtime = KronosRuntime(
            KronosConfig(enabled=True, backend="stub", model_revision="planted"),
            cache=ModelCache(root))
        status = runtime.load()
        assert status.state is KronosState.ERROR
        assert "integrity" in status.message

    def test_resolve_within_root_accepts_a_genuine_cached_file(self, tmp_path):
        cache = ModelCache(tmp_path)
        nested = tmp_path / "models--NeoQuasar--Kronos-small" / "blobs"
        nested.mkdir(parents=True)
        weights = nested / "model.safetensors"
        weights.write_bytes(b"real weights")
        assert cache.resolve_within_root(
            "models--NeoQuasar--Kronos-small/blobs/model.safetensors") == \
            weights.resolve()

    @pytest.mark.parametrize("relative_path", [
        "/etc/passwd",
        "../secret.bin",
        "../../outside.bin",
        "subdir/../../outside.bin",
    ])
    def test_resolve_within_root_rejects_escapes(self, tmp_path, relative_path):
        cache = ModelCache(tmp_path / "cache")
        with pytest.raises(ValueError, match="outside the model cache"):
            cache.resolve_within_root(relative_path)

    def test_recording_a_file_outside_the_root_is_still_verifiable(self, tmp_path):
        """record() may track an external file; verify() must still refuse it.

        This keeps the two halves honest: recording stays permissive for
        diagnostics, but a load-time integrity check never trusts a path that
        leaves the cache.
        """
        root = tmp_path / "cache"
        root.mkdir()
        external = tmp_path / "external.safetensors"
        external.write_bytes(b"downloaded elsewhere")
        cache = ModelCache(root)
        entry = cache.record("NeoQuasar/Kronos-small", "ext", external)
        assert Path(entry.relative_path).is_absolute()

        report = cache.verify("NeoQuasar/Kronos-small", "ext")
        assert not report.ok
        assert any("outside the model cache" in p for p in report.problems)


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

    def test_an_empty_trajectory_is_rejected_outright(self):
        """No paths at all is a hard failure, not an empty-but-valid forecast.

        Found by mutation: replacing this early return with a passing result left
        the whole suite green, so nothing pinned it. An empty trajectory means the
        backend produced nothing; letting it through would hand the pipeline a
        forecast with zero candles to assemble.
        """
        from axiom.forecast.validation import validate_forecast
        from axiom.kronos.backends import KronosInference

        config = KronosConfig(pred_len=6, sample_paths=1, lookback=40)
        check = validate_forecast(KronosInference((), 1.0, "stub"), future(6), config)
        assert not check.ok
        assert check.errors == ("model returned no trajectory",)

    def test_a_horizon_that_disagrees_with_the_config_is_rejected(self):
        """The timestamp count is checked independently of the path step count.

        Found by mutation: neutralising this check left the suite green. Without
        it a caller could pass the wrong number of future timestamps and still get
        a 'valid' forecast whenever the paths happened to match that wrong number.
        """
        from axiom.forecast.validation import validate_forecast
        from axiom.kronos.backends import KronosInference

        config = KronosConfig(pred_len=6, sample_paths=1, lookback=40)
        # Five timestamps and a five-step path: internally consistent, but the
        # config asked for six. Only this check catches it.
        check = validate_forecast(
            KronosInference((make_path(5),), 1.0, "stub"), future(5), config)
        assert not check.ok
        assert any("expected 6 future timestamps, got 5" in e for e in check.errors)

    def test_non_increasing_timestamps_are_rejected(self):
        """Forecast timestamps must advance; a flat or backwards step is invalid.

        Found by mutation: neutralising this check left the suite green. The
        calendar is the only legitimate source of these stamps, so a repeated or
        reversed one means something upstream is wrong.
        """
        from axiom.forecast.validation import validate_forecast
        from axiom.kronos.backends import KronosInference

        config = KronosConfig(pred_len=3, sample_paths=1, lookback=40)
        origin = datetime(2024, 3, 1, tzinfo=UTC)
        for bad_stamps in (
            [origin, origin, origin + timedelta(hours=1)],              # repeated
            [origin, origin + timedelta(hours=1), origin],              # backwards
        ):
            check = validate_forecast(
                KronosInference((make_path(3),), 1.0, "stub"), bad_stamps, config)
            assert not check.ok, bad_stamps
            assert any("strictly increasing" in e for e in check.errors), bad_stamps

    def test_a_step_missing_a_field_is_rejected(self):
        """Every step must carry all five OHLCV fields.

        Found by mutation: neutralising this check left the suite green. The guard
        also short-circuits the rest of the per-step checks, so without it a
        missing key would surface later as a confusing KeyError instead.
        """
        from axiom.forecast.validation import FORECAST_FIELDS, validate_forecast
        from axiom.kronos.backends import KronosInference

        config = KronosConfig(pred_len=6, sample_paths=1, lookback=40)
        path = list(make_path(6))
        del path[2]["high"]
        check = validate_forecast(
            KronosInference((tuple(path),), 1.0, "stub"), future(6), config)
        assert not check.ok
        assert any("step 2 is missing high" in e for e in check.errors)
        # Sanity: the field list the guard walks is the documented OHLCV set.
        assert FORECAST_FIELDS == ("open", "high", "low", "close", "volume")

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

    def test_percentile_matches_numpy_default_method(self):
        """The documented claim is numpy's default 'linear' interpolation.

        AXIOM must not need numpy at runtime, so percentile() is hand-written;
        this pins it to numpy's semantics so the two cannot drift apart.
        """
        np = pytest.importorskip("numpy")
        random = pytest.importorskip("random")
        random.seed(7)
        worst = 0.0
        for _ in range(500):
            count = random.randint(2, 40)
            values = [random.uniform(-50, 50) for _ in range(count)]
            quantile = random.choice([0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0])
            mine = percentile(values, quantile)
            theirs = float(np.percentile(values, quantile * 100.0))
            worst = max(worst, abs(mine - theirs))
        assert worst < 1e-9, f"diverged from numpy by {worst}"

    def test_percentile_rejects_bad_input(self):
        with pytest.raises(ValueError, match="at least one value"):
            percentile([], 0.5)
        with pytest.raises(ValueError, match="within"):
            percentile([1.0, 2.0], 1.5)

    def test_quantiles_are_order_independent(self):
        """P10/P50/P90 describe the sample set, not the order paths arrived in."""
        paths = [[10.0], [30.0], [20.0]]
        shuffled = [[20.0], [10.0], [30.0]]
        first = summarize_paths(paths)
        second = summarize_paths(shuffled)
        assert (first.p10_close, first.p50_close, first.p90_close) == (
            second.p10_close, second.p50_close, second.p90_close)
        # Linear interpolation on [10, 20, 30]: p10 sits at position 0.2 of the
        # sorted index range, not on the minimum.
        assert (first.p10_close, first.p50_close, first.p90_close) == (
            12.0, 20.0, 28.0)

    def test_the_band_never_inverts(self):
        """high >= low must hold for every step even with few paths."""
        paths = [[100.0, 50.0, 200.0], [101.0, 49.0, 199.0], [99.0, 51.0, 201.0]]
        summary = summarize_paths(paths)
        assert summary is not None
        for lower, upper in summary.close_band:
            assert lower <= upper

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

    def test_upstream_averages_its_samples_so_one_call_has_no_dispersion(
        self, stubbed_download, tmp_path
    ):
        """P10/P50/P90 rest on this: sample_count>1 returns a MEAN, not paths.

        Verified two ways: the upstream source averages over the sample axis,
        and a single predict() call returns exactly one trajectory no matter
        how large sample_count is.
        """
        import inspect

        import pandas as pd
        import torch

        source = inspect.getsource(stubbed_download.kronos.auto_regressive_inference)
        assert "np.mean(preds, axis=1)" in source, (
            "upstream no longer averages its samples; AXIOM's repeated "
            "single-path inference needs re-review"
        )

        config = KronosConfig(
            enabled=True, backend="upstream", lookback=32, pred_len=4,
            max_context=64, sample_paths=1, device="cpu", min_history=8,
            model_id="NeoQuasar/Kronos-custom",
        )
        backend = UpstreamKronosBackend()
        backend.load(config, cache=ModelCache(tmp_path))
        window = bars(32)
        stamps = future(4, start=window[-1].timestamp + timedelta(hours=1))
        frame = pd.DataFrame({
            "open": [b.open for b in window], "high": [b.high for b in window],
            "low": [b.low for b in window], "close": [b.close for b in window],
            "volume": [b.volume for b in window]})
        x_timestamp = pd.Series([b.timestamp for b in window])
        y_timestamp = pd.Series(stamps)

        for sample_count in (1, 5):
            predicted = backend.predictor.predict(
                df=frame, x_timestamp=x_timestamp, y_timestamp=y_timestamp,
                pred_len=4, sample_count=sample_count, verbose=False)
            assert len(predicted) == 4, (
                f"sample_count={sample_count} returned {len(predicted)} rows; "
                "one call must yield exactly one trajectory"
            )
        del torch  # imported above only to fail fast if torch is missing

    def test_repeated_single_path_calls_produce_real_dispersion(
        self, stubbed_download, tmp_path
    ):
        """Dispersion must come from genuinely different samples.

        Same seed -> identical path (deterministic, nothing random fabricated).
        Different seed -> different path (so P10/P50/P90 describe real spread).
        """
        import torch

        config = KronosConfig(
            enabled=True, backend="upstream", lookback=32, pred_len=4,
            max_context=64, sample_paths=1, device="cpu", min_history=8,
            model_id="NeoQuasar/Kronos-custom",
        )
        backend = UpstreamKronosBackend()
        backend.load(config, cache=ModelCache(tmp_path))
        window = bars(32)
        stamps = future(4, start=window[-1].timestamp + timedelta(hours=1))

        def one_path(seed: int):
            torch.manual_seed(seed)
            return backend.forecast(window, stamps, config).paths[0]

        same_a = [step["close"] for step in one_path(123)]
        same_b = [step["close"] for step in one_path(123)]
        other = [step["close"] for step in one_path(999)]
        assert same_a == pytest.approx(same_b), "sampling is not reproducible"
        assert same_a != pytest.approx(other), (
            "different seeds gave identical paths: no dispersion to report"
        )

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


# --- validated market data -> REAL upstream Kronos -> structured result ------


def _validated_forecast(backend, config, window, stamps, instrument="DEMO"):
    """The production order: validate the window, and only then touch the model.

    Mirrors ForecastPipeline.run, which validates before inference so invalid
    data can never reach Kronos.
    """
    from axiom.market.validation import require_valid, validate_market_data

    quality = validate_market_data(
        window, "1h", min_history=config.min_history, expected_instrument=instrument
    )
    require_valid(quality)
    return quality, backend.forecast(window, stamps, config)


class TestValidatedChainIntoRealKronos:
    """market fixture -> preprocessing -> real KronosPredictor -> ForecastSnapshot.

    ``stubbed_download`` replaces only the Hugging Face *download*. The
    tokenizer, the model and ``KronosPredictor`` are the genuine upstream
    classes from ``AXIOM_KRONOS_SOURCE``, instantiated with tiny random
    weights. So the integration boundary is exercised for real, while the
    numbers are meaningless. Inference with real pretrained weights is NOT
    verified by this suite and must not be reported as passing.
    """

    CONFIG = dict(
        enabled=True, backend="upstream", lookback=32, pred_len=4,
        max_context=64, sample_paths=3, device="cpu", min_history=8,
        model_id="NeoQuasar/Kronos-custom",
    )

    def _loaded(self, stubbed_download, tmp_path):
        config = KronosConfig(**self.CONFIG)
        backend = UpstreamKronosBackend()
        info = backend.load(config, cache=ModelCache(tmp_path))
        return config, backend, info

    def test_a_validated_window_produces_a_real_upstream_forecast(
        self, stubbed_download, tmp_path
    ):
        from axiom.forecast.validation import build_forecast_candles, validate_forecast
        from axiom.models import DataProvenance, ForecastSnapshot

        config, backend, info = self._loaded(stubbed_download, tmp_path)
        window = bars(32)
        stamps = future(4, start=window[-1].timestamp + timedelta(hours=1))

        quality, inference = _validated_forecast(backend, config, window, stamps)
        assert quality.ok, quality.errors
        assert quality.bars == 32

        # The predictor that ran is the upstream one, from a real checkout.
        # kronos_source is the imported module's __file__, so it must exist and
        # live inside the configured checkout.
        assert inference.backend == "upstream"
        assert info.kronos_source, "the upstream source location must be recorded"
        source_path = Path(info.kronos_source)
        assert source_path.is_file(), source_path
        assert str(source_path).startswith(os.environ["AXIOM_KRONOS_SOURCE"])

        check = validate_forecast(inference, stamps, config)
        assert check.ok, check.errors

        candles = build_forecast_candles(inference.paths, stamps)
        summary = summarize_paths(inference.close_paths())
        snapshot = ForecastSnapshot(
            instrument="DEMO", timeframe="1h",
            generated_at=datetime(2024, 6, 1, tzinfo=UTC),
            candles=candles, config=config,
            provenance=DataProvenance(source="test", provider="replay",
                                      instrument="DEMO", timeframe="1h", bars=32),
            data_quality=quality,
            model_id=info.model_id, model_revision=info.model_revision,
            device=info.device, backend=inference.backend,
            last_closed_timestamp=window[-1].timestamp,
            last_actual_close=window[-1].close,
            uncertainty=summary, latency_ms=inference.latency_ms,
        )
        assert snapshot.origin == FORECAST_ORIGIN
        assert snapshot.data_quality.ok
        assert len(snapshot.candles) == 4

    def test_forecast_timestamps_survive_the_whole_chain(
        self, stubbed_download, tmp_path
    ):
        """Requirement: predicted bars must carry the requested future stamps."""
        from axiom.forecast.validation import build_forecast_candles

        config, backend, _ = self._loaded(stubbed_download, tmp_path)
        window = bars(32)
        stamps = future(4, start=window[-1].timestamp + timedelta(hours=1))

        _, inference = _validated_forecast(backend, config, window, stamps)
        candles = build_forecast_candles(inference.paths, stamps)
        assert [c.timestamp for c in candles] == stamps

    def test_no_input_bar_is_at_or_after_the_first_predicted_step(
        self, stubbed_download, tmp_path
    ):
        """Requirement 6: the prediction input must never contain future data."""
        config, backend, _ = self._loaded(stubbed_download, tmp_path)
        window = bars(32)
        stamps = future(4, start=window[-1].timestamp + timedelta(hours=1))

        _, inference = _validated_forecast(backend, config, window, stamps)
        assert max(bar.timestamp for bar in window) < stamps[0]
        assert all(step["close"] == step["close"] for path in inference.paths
                   for step in path)  # no NaN slipped through

    def test_invalid_market_data_never_reaches_the_predictor(
        self, stubbed_download, tmp_path
    ):
        """The validation gate is what stands between bad data and the model."""
        config, backend, _ = self._loaded(stubbed_download, tmp_path)
        good = bars(32)
        stamps = future(4, start=good[-1].timestamp + timedelta(hours=1))

        calls = []
        real_predict = backend.predictor.predict

        def spy(*args, **kwargs):
            calls.append(kwargs)
            return real_predict(*args, **kwargs)

        backend.predictor.predict = spy

        # Non-monotonic timestamps.
        shuffled = good[:16] + good[16:][::-1]
        with pytest.raises(ValueError):
            _validated_forecast(backend, config, shuffled, stamps)

        # Duplicate timestamps.
        duplicated = good + [good[-1]]
        with pytest.raises(ValueError):
            _validated_forecast(backend, config, duplicated,
                                future(4, start=good[-1].timestamp + timedelta(hours=2)))

        # NaN in a price.
        corrupt = list(good)
        object.__setattr__(corrupt[5], "close", float("nan"))
        with pytest.raises(ValueError):
            _validated_forecast(backend, config, corrupt, stamps)

        # Insufficient lookback.
        with pytest.raises(ValueError):
            _validated_forecast(backend, config, good[:3], stamps)

        assert calls == [], "the predictor must not run on a rejected window"

    def test_model_identity_and_device_are_tracked(self, stubbed_download, tmp_path):
        """Requirement 4: model/tokenizer/version/device must be reported."""
        config, backend, info = self._loaded(stubbed_download, tmp_path)
        payload = info.to_dict()
        assert payload["model_id"] == "NeoQuasar/Kronos-custom"
        assert payload["tokenizer_id"] == config.tokenizer_id
        assert payload["device"] == "cpu"
        assert payload["backend"] == "upstream"
        assert payload["torch_version"]
        assert payload["max_context"] == 64
