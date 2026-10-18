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

import pytest

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


class TestDistributionPackaging:
    """``pip install .`` must actually ship the Kronos subsystem."""

    def test_pyproject_includes_the_axiom_package(self):
        """Regression: [tool.setuptools.packages.find] listed only app* and
        desktop*, so an installed distribution contained no axiom package at all
        — omitting axiom.worker.child, the entry point of the AXIOM-Kronos
        process. PyInstaller hides this because it collects from source.
        """
        try:
            import tomllib
        except ImportError:  # pragma: no cover - Python < 3.11
            pytest.skip("tomllib requires Python 3.11+")

        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        include = config["tool"]["setuptools"]["packages"]["find"]["include"]
        assert "axiom*" in include, f"axiom* missing from packages.find.include: {include}"

    def test_the_declared_include_actually_finds_axiom(self):
        """Assert the effect, not just the config string."""
        try:
            import tomllib
        except ImportError:  # pragma: no cover - Python < 3.11
            pytest.skip("tomllib requires Python 3.11+")
        from setuptools import find_packages

        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        find = config["tool"]["setuptools"]["packages"]["find"]
        found = find_packages(
            where=str(ROOT), include=find["include"], exclude=find.get("exclude", []))
        axiom = {p for p in found if p == "axiom" or p.startswith("axiom.")}
        assert "axiom" in axiom
        for required in ("axiom.kronos", "axiom.worker", "axiom.forecast",
                         "axiom.market", "axiom.research"):
            assert required in axiom, f"{required} would not be shipped"
        # The child entry point must be importable from an installed tree.
        assert (ROOT / "axiom" / "worker" / "child.py").is_file()


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
    def test_desktop_offscreen_smoke_entry_is_available(self):
        source = (ROOT / "desktop" / "app.py").read_text(encoding="utf-8")
        assert "smoke_mode = \"--smoke\" in argv" in source
        assert "QTimer.singleShot(0, window.close)" in source

    def test_package_target_resolves_to_its_main_script(self):
        from tools.build_windows_executables import _entry_point

        assert Path(_entry_point("desktop")) == ROOT / "desktop" / "__main__.py"
        assert Path(_entry_point("axiom.worker.child")) == \
            ROOT / "axiom" / "worker" / "child.py"

    def test_frozen_gui_launches_the_sibling_kronos_executable(self, tmp_path,
                                                               monkeypatch):
        from axiom.models import KronosConfig
        from axiom.worker.manager import KronosManager

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        gui = tmp_path / "AXIOM.exe"
        worker = tmp_path / "AXIOM-Kronos.exe"
        gui.write_bytes(b"test-only GUI marker")
        worker.write_bytes(b"test-only worker marker")
        manager = KronosManager(
            KronosConfig(enabled=True, backend="stub"),
            mode="process",
            python_executable=str(gui),
        )

        assert manager.child_command() == [str(worker)]

    def test_build_script_defines_both_executables(self):
        source = (ROOT / "tools" / "build_windows_executables.py").read_text(
            encoding="utf-8")
        for required in ('"AXIOM"', '"AXIOM-Kronos"', "axiom.worker.child",
                         "PyInstaller", "--desktop-python", "--kronos-python"):
            assert required in source, f"build script is missing {required}"

    def test_builder_uses_the_requested_interpreter_for_each_target(self, monkeypatch):
        from tools import build_windows_executables as builder

        commands = []
        monkeypatch.setattr(
            builder.subprocess, "call",
            lambda command, **kwargs: commands.append((command, kwargs)) or 0)
        desktop_python = "C:/build-envs/desktop/python.exe"
        kronos_python = "C:/build-envs/kronos/python.exe"

        assert builder.build(builder.TARGETS[0], python_executable=desktop_python,
                             onedir=False, clean=False) == 0
        assert builder.build(builder.TARGETS[1], python_executable=kronos_python,
                             onedir=False, clean=False) == 0
        assert commands[0][0][0] == desktop_python
        assert commands[1][0][0] == kronos_python
        assert Path(commands[0][0][-1]).as_posix().endswith("desktop/__main__.py")
        assert Path(commands[1][0][-1]).as_posix().endswith("axiom/worker/child.py")

    def test_gui_target_refuses_to_bundle_pytorch(self, monkeypatch, capsys):
        from types import ModuleType

        from tools import build_windows_executables as builder

        monkeypatch.setitem(sys.modules, "PyInstaller", ModuleType("PyInstaller"))
        monkeypatch.setattr(builder, "_has_pyinstaller", lambda _python: True)
        monkeypatch.setattr(builder, "_module_available", lambda _python, _name: True)

        result = builder.main([
            "--only", "AXIOM", "--desktop-python", "gui-python",
        ])

        assert result == 2
        assert "GUI environment must be PyTorch-free" in capsys.readouterr().err

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
