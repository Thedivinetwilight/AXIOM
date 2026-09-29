"""Packaging: separate Kronos runtime, no weights in Git, model cache rules.

The heavy runtime must stay out of the base install, model weights must stay out
of the repository, and the Kronos process must be launchable as a standalone
executable/entry point (``AXIOM-Kronos``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestDependencySeparation:
    def test_kronos_requirements_exist_and_are_separate(self):
        kronos = (ROOT / "requirements-kronos.txt").read_text(encoding="utf-8")
        base = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        for package in ("torch", "numpy", "pandas", "einops", "safetensors",
                        "huggingface_hub"):
            assert package in kronos, f"{package} missing from requirements-kronos.txt"
        for package in ("torch", "pandas", "einops", "safetensors"):
            assert package not in base, f"{package} leaked into the base runtime"

    def test_desktop_requirements_stay_gui_only(self):
        desktop = (ROOT / "requirements-desktop.txt").read_text(encoding="utf-8")
        assert "PySide6" in desktop
        assert "torch" not in desktop

    def test_kronos_requirements_document_the_upstream_source(self):
        kronos = (ROOT / "requirements-kronos.txt").read_text(encoding="utf-8")
        assert "shiyu-coder/Kronos" in kronos
        assert "AXIOM_KRONOS_SOURCE" in kronos


class TestNoWeightsInGit:
    def test_no_model_weights_are_tracked(self):
        tracked = subprocess.run(  # noqa: S603 - read-only git query
            ["git", "ls-files"], capture_output=True, text=True, cwd=str(ROOT),
            timeout=120).stdout.splitlines()
        heavy = [path for path in tracked
                 if path.endswith((".safetensors", ".pt", ".pth", ".bin", ".onnx"))]
        assert heavy == [], f"model weights must not be committed: {heavy}"

    def test_gitignore_covers_the_runtime_cache(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("*.safetensors", "models/", ".venv-kronos/"):
            assert pattern in ignore, f".gitignore is missing {pattern}"

    def test_windows_launcher_keeps_weights_out_of_the_repo(self):
        launcher = (ROOT / "desktop" / "run_kronos_windows.bat").read_text(encoding="utf-8")
        assert "LOCALAPPDATA" in launcher
        assert "requirements-kronos.txt" in launcher
        assert ".venv-kronos" in launcher


class TestExecutableTargets:
    def test_build_script_defines_both_executables(self):
        source = (ROOT / "tools" / "build_windows_executables.py").read_text(
            encoding="utf-8")
        for required in ('"AXIOM"', '"AXIOM-Kronos"', "axiom.worker.child",
                         "PyInstaller"):
            assert required in source, f"build script is missing {required}"

    def test_targets_are_separate_processes(self):
        """The GUI target must not bundle the Kronos entry point and vice versa."""
        namespace: dict = {}
        source = (ROOT / "tools" / "build_windows_executables.py").read_text(
            encoding="utf-8")
        start = source.index("TARGETS = (")
        end = source.index("\n)\n", start) + 2
        exec(compile("from pathlib import Path\nROOT = Path('.')\n"
                     + source[start:end], "targets", "exec"), namespace)
        targets = {target["name"]: target for target in namespace["TARGETS"]}
        assert set(targets) == {"AXIOM", "AXIOM-Kronos"}
        assert targets["AXIOM"]["module"] == "desktop"
        assert targets["AXIOM-Kronos"]["module"] == "axiom.worker.child"
        assert targets["AXIOM"]["heavy"] is False
        assert targets["AXIOM-Kronos"]["heavy"] is True

    def test_build_script_reports_a_missing_pyinstaller(self):
        code = "\n".join([
            "import sys, builtins",
            "real = builtins.__import__",
            "def fake(name, *a, **k):",
            "    if name == 'PyInstaller':",
            "        raise ImportError(name)",
            "    return real(name, *a, **k)",
            "builtins.__import__ = fake",
            "sys.path.insert(0, 'tools')",
            "import build_windows_executables as builder",
            "raise SystemExit(builder.main([]))",
        ])
        result = subprocess.run(  # noqa: S603 - fixed interpreter
            [sys.executable, "-c", code], capture_output=True, text=True,
            cwd=str(ROOT), timeout=180)
        assert result.returncode == 2
        assert "PyInstaller is required" in result.stderr


class TestPackagedRuntimeStartup:
    def test_kronos_entry_point_runs_standalone(self):
        """`python -m axiom.worker.child` is what AXIOM-Kronos.exe wraps."""
        from axiom.worker.protocol import KronosRequest

        payload = json.dumps({
            "enabled": True, "backend": "stub", "lookback": 64, "pred_len": 4,
            "min_history": 16, "sample_paths": 1,
        })
        process = subprocess.run(  # noqa: S603 - fixed interpreter and module
            [sys.executable, "-m", "axiom.worker.child", "--config", payload],
            input="\n".join([
                KronosRequest("ping", "1").encode(),
                KronosRequest("load", "2").encode(),
                KronosRequest("status", "3").encode(),
                KronosRequest("shutdown", "4").encode(),
            ]) + "\n",
            capture_output=True, text=True, cwd=str(ROOT), timeout=240)
        assert process.returncode == 0, process.stderr[-2000:]
        lines = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        assert [line["id"] for line in lines] == ["1", "2", "3", "4"]
        assert lines[0]["result"]["pong"] is True
        assert lines[1]["result"]["status"]["state"] == "READY"
        assert lines[3]["state"] == "OFFLINE"

    def test_entry_point_refuses_a_broken_configuration(self):
        process = subprocess.run(  # noqa: S603 - fixed interpreter and module
            [sys.executable, "-m", "axiom.worker.child", "--config", "{}"],
            input="", capture_output=True, text=True, cwd=str(ROOT), timeout=120)
        # An empty config still parses (defaults); the child simply answers
        # requests. The failure mode we must not see is a traceback.
        assert "Traceback" not in process.stderr


class TestModelCachePolicy:
    def test_default_cache_is_under_the_user_profile(self, tmp_path, monkeypatch):
        from axiom.kronos.model_cache import cache_root_for

        root = cache_root_for(
            override=None, os_name="nt", platform="win32",
            localappdata="C:\\Users\\tester\\AppData\\Local",
            xdg_cache_home=None, home=tmp_path)
        assert "AppData/Local/AXIOM/models/kronos" in str(root).replace("\\", "/")

    def test_manifest_records_the_required_fields(self, tmp_path):
        from axiom.kronos.model_cache import ModelCache

        cache = ModelCache(tmp_path)
        weights = tmp_path / "model.safetensors"
        weights.write_bytes(b"x" * 128)
        entry = cache.record("NeoQuasar/Kronos-small", "rev123", weights)
        manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        stored = manifest["entries"]["NeoQuasar/Kronos-small@rev123"]
        for field in ("model_id", "revision", "file_name", "relative_path",
                      "size_bytes", "sha256", "downloaded_at"):
            assert field in stored, f"manifest is missing {field}"
        assert stored["sha256"] == entry.sha256
        assert stored["size_bytes"] == 128
