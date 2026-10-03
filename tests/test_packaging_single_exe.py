"""The single-executable build: what must be true for it to work.

A packaged Arena is a different runtime from a source checkout, and the
failures it can have are specific — a data directory that resets every launch,
a console that cannot run commands because `sys.executable` is the .exe, a
missing hidden import that only breaks on the first HTTP request. None of that
is covered by the ordinary suite, so it is pinned here.

The frozen runtime is SIMULATED (`sys.frozen = True`) rather than executed,
because PyInstaller cannot be run on every machine that runs these tests. The
simulation exercises exactly the code paths a real bundle takes — app/runtime.py
and app/console/runner.py branch on `getattr(sys, "frozen", False)` and on
nothing else.
"""

from __future__ import annotations

import ast
import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "arena.spec"


def posix(text) -> str:
    """Compare paths without caring about the separator.

    A sqlite URL must use forward slashes even on Windows
    (`sqlite:///C:/Users/...`), while `str(Path)` uses backslashes there, so a
    naive `in` check passes on Linux and fails on Windows for the wrong reason.
    """
    return str(text).replace("\\", "/")


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """Pretend to be a PyInstaller bundle with a per-user data directory."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("ARENA_HOME", str(tmp_path / "Arena"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATA_DIR", raising=False)
    return tmp_path / "Arena"


# --------------------------------------------------------------------------
# Where state lives
# --------------------------------------------------------------------------


class TestFrozenPaths:
    def test_home_is_a_per_user_directory_not_the_bundle(self, frozen):
        """The bundle unpacks into a temp dir that is deleted on exit."""
        from app import runtime

        home = runtime.arena_home()
        assert home == frozen
        assert posix(home) != posix(ROOT)

    def test_home_is_created_with_the_subdirectories_the_app_needs(self, frozen):
        from app import runtime

        runtime.ensure_home()
        for child in ("data", "data/history", "logs", "results", "backups"):
            assert (frozen / child).is_dir(), child

    def test_database_and_data_are_absolute_and_inside_home(self, frozen):
        from app.config.settings import Settings

        settings = Settings.from_env()
        assert posix(frozen) in posix(settings.database_url)
        assert Path(settings.data_dir).is_absolute()
        assert posix(Path(settings.data_dir)) == posix((frozen / "data").resolve())

    def test_paths_do_not_depend_on_the_working_directory(self, frozen, monkeypatch):
        """A double-clicked .exe on Windows often starts in C:\\Windows\\System32,
        so a relative `sqlite:///trading_bot.db` would land somewhere random and
        the database would look like it reset every launch."""
        from app import runtime
        from app.config.settings import Settings

        runtime.ensure_home()
        monkeypatch.chdir(frozen)
        first = Settings.from_env().database_url

        elsewhere = frozen / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        second = Settings.from_env().database_url

        assert first == second
        assert posix(frozen) in posix(second)

    def test_an_explicit_setting_still_wins(self, frozen, monkeypatch):
        """The redirection must only apply to values the operator did NOT set."""
        monkeypatch.setenv("DATABASE_URL", "sqlite:////custom/mine.db")
        monkeypatch.setenv("DATA_DIR", "/custom/data")
        from app.config.settings import Settings

        settings = Settings.from_env()
        assert settings.database_url == "sqlite:////custom/mine.db"
        assert settings.data_dir == "/custom/data"

    def test_env_file_is_writable_location_not_the_bundle(self, frozen):
        from app import runtime

        assert runtime.env_file() == frozen / ".env"
        assert runtime.env_file().parent == frozen

    def test_diagnostics_report_the_real_locations(self, frozen):
        from app import runtime

        described = runtime.describe()
        assert described["frozen"] is True
        assert described["portable"] is True
        assert described["arena_home"] == str(frozen)


# --------------------------------------------------------------------------
# A source checkout must be completely unaffected
# --------------------------------------------------------------------------


class TestSourceModeIsUnchanged:
    def test_not_frozen_means_repo_root(self, monkeypatch):
        monkeypatch.delenv("ARENA_HOME", raising=False)
        monkeypatch.delattr(sys, "frozen", raising=False)
        from app import runtime

        assert runtime.is_frozen() is False
        assert runtime.is_portable() is False
        assert runtime.arena_home() == ROOT

    def test_default_paths_are_untouched_when_running_from_source(self, monkeypatch):
        """The 1300 existing tests and every documented workflow depend on
        these defaults. Packaging must not change them."""
        monkeypatch.delenv("ARENA_HOME", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("DATA_DIR", raising=False)
        monkeypatch.delattr(sys, "frozen", raising=False)
        from app.config.settings import Settings

        settings = Settings.from_env()
        assert settings.database_url == "sqlite:///trading_bot.db"
        assert settings.data_dir == "data"


# --------------------------------------------------------------------------
# The console must still be able to run commands inside the .exe
# --------------------------------------------------------------------------


class TestConsoleWorksWhenFrozen:
    def test_argv_re_enters_the_executable(self, frozen, monkeypatch):
        """`sys.executable` is Arena.exe, which cannot be asked to run
        `-m app`. The launcher exposes --cli/--run for exactly this."""
        from app.console.runner import build_argv

        argv = build_argv("app", "live-status", ["--audit"])
        assert argv == [sys.executable, "--cli", "live-status", "--audit"]

        argv = build_argv("run", "backtest", ["--symbol", "X"])
        assert argv == [sys.executable, "--run", "backtest", "--symbol", "X"]

    def test_source_mode_still_uses_module_and_script_forms(self, monkeypatch):
        monkeypatch.delattr(sys, "frozen", raising=False)
        from app.console.runner import build_argv

        assert build_argv("app", "live-status", [])[1:3] == ["-m", "app"]
        assert build_argv("run", "backtest", [])[1].endswith("run.py")

    def test_the_launcher_handles_the_cli_reentry_point(self, capsys):
        """`arena.py --cli <command>` must dispatch to the CLI without starting
        a server."""
        import arena

        code = arena.main(["--cli", "live-status"])
        captured = capsys.readouterr().out
        assert code == 0
        assert captured.strip().startswith("{")

    def test_the_launcher_refuses_nonsense_through_the_cli_gate(self, capsys):
        import arena

        code = arena.main(["--cli", "definitely-not-a-command"])
        assert code != 0

    def test_runner_still_never_uses_a_shell_when_frozen(self, frozen):
        source = (ROOT / "app" / "console" / "runner.py").read_text("utf-8")
        assert "shell=True" not in source


# --------------------------------------------------------------------------
# The launcher
# --------------------------------------------------------------------------


class TestLauncher:
    def test_port_falls_through_when_the_preferred_one_is_taken(self):
        """The holder must call listen().

        A socket that is bound but NOT listening can be bound a second time on
        Linux even with SO_REUSEADDR, so an unrealistic holder would assert
        nothing. A real server listens — and against a listening socket the
        probe correctly fails and the picker moves on.
        """
        import socket

        import arena

        holder = socket.socket()
        try:
            # Reserving a port means something different on each platform.
            # POSIX: SO_REUSEADDR + listen is enough. Windows: the default is
            # the opposite — SO_REUSEADDR there ALLOWS a second bind, so a
            # holder must use SO_EXCLUSIVEADDRUSE to truly own the port.
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                holder.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            holder.bind(("127.0.0.1", 0))
            taken = holder.getsockname()[1]
            holder.listen(5)

            chosen = arena._free_port(taken, "127.0.0.1")
            assert chosen != taken
            assert chosen > taken

            # and the choice must be genuinely bindable by a real server
            with socket.socket() as check:
                check.bind(("127.0.0.1", chosen))
                check.listen(1)
        finally:
            holder.close()

    def test_probe_does_not_use_so_reuseaddr_on_windows(self):
        """On Windows SO_REUSEADDR PERMITS double-binding, so setting it in the
        probe would make every port look free and Arena would then fail to
        start with "address already in use" — the error the picker exists to
        avoid."""
        source = (ROOT / "arena.py").read_text("utf-8")
        assert 'if os.name != "nt":' in source
        assert "SO_REUSEADDR" in source
        # the guard must come BEFORE the setsockopt call
        guard = source.index('if os.name != "nt":')
        reuse = source.index("SO_REUSEADDR", guard)
        assert guard < reuse

    def test_first_run_detection(self, frozen, monkeypatch):
        import arena

        frozen.mkdir(parents=True, exist_ok=True)
        assert arena._first_run() is True
        (frozen / ".env").write_text("API_TOKEN=x\n")
        assert arena._first_run() is False

    def test_diagnostics_mode_prints_locations_and_exits(self, frozen, capsys):
        import json

        import arena

        assert arena.main(["--diagnostics"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["arena_home"] == str(frozen)

    def test_launcher_defaults_to_loopback(self):
        """Setup writes credentials, so the default bind must be local-only."""
        import arena

        assert arena.DEFAULT_HOST in {"127.0.0.1", "localhost"}


# --------------------------------------------------------------------------
# The spec itself
# --------------------------------------------------------------------------


class TestWindowsConsoleEncoding:
    """Windows consoles and pipes default to a legacy code page (cp1252), which
    cannot encode characters Arena prints routinely. Python raises
    UnicodeEncodeError at the first one and the whole command dies — so
    `python -m app --help` exited 1 on Windows, and the packaged launcher would
    die before printing its address."""

    def test_entry_points_make_output_utf8_safe(self):
        for name in ("app/__main__.py", "run.py", "arena.py"):
            source = (ROOT / name).read_text("utf-8")
            assert "ensure_utf8_output" in source, f"{name} does not guard its output"

    def test_the_helper_reconfigures_both_streams(self):
        import inspect

        from app import runtime

        body = inspect.getsource(runtime.ensure_utf8_output)
        assert "sys.stdout" in body and "sys.stderr" in body
        assert "reconfigure" in body
        assert "errors=\"replace\"" in body or "errors='replace'" in body

    def test_the_helper_tolerates_streams_that_cannot_be_reconfigured(self, monkeypatch):
        """pytest capture objects, closed pipes and detached stdout do not all
        support reconfigure(). Raising from here would break every command on
        exactly the platforms this exists to protect."""
        import io
        import sys as _sys

        from app import runtime

        class NoReconfigure(io.StringIO):
            def reconfigure(self, **kwargs):
                raise ValueError("stream does not support reconfigure")

        class NoAttribute(io.StringIO):
            # a stream without reconfigure at all (pre-3.7 style object)
            pass

        monkeypatch.setattr(_sys, "stdout", NoReconfigure())
        monkeypatch.setattr(_sys, "stderr", NoAttribute())
        runtime.ensure_utf8_output()  # must not raise

        # and the happy path does call through
        calls = []

        class Recording(io.StringIO):
            def reconfigure(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(_sys, "stdout", Recording())
        monkeypatch.setattr(_sys, "stderr", Recording())
        runtime.ensure_utf8_output()
        assert len(calls) == 2
        assert all(call.get("encoding") == "utf-8" for call in calls)
        assert all(call.get("errors") == "replace" for call in calls)

    def test_help_survives_a_cp1252_console(self):
        """The real reproduction: a legacy-encoded console must not kill the
        CLI. Before the fix this exited 1 with UnicodeEncodeError."""
        import subprocess

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "cp1252:strict"
        result = subprocess.run(
            [sys.executable, "-m", "app", "--help"],
            capture_output=True, text=True, timeout=180, cwd=str(ROOT), env=env,
        )
        assert result.returncode == 0, result.stderr[-400:]


class TestBuildSpec:
    def _spec_source(self) -> str:
        return SPEC.read_text("utf-8")

    def test_spec_exists_and_is_valid_python(self):
        ast.parse(self._spec_source())

    def test_entry_point_is_the_launcher(self):
        assert 'ROOT / "arena.py"' in self._spec_source()

    def test_every_hidden_import_resolves_to_a_real_module(self):
        """A typo here produces a bundle that builds and then dies on first
        use — the worst possible failure mode for a shipped artifact."""
        source = self._spec_source()
        start = source.index("hiddenimports = [")
        end = source.index("]", start)
        listed = [
            line.strip().strip('",')
            for line in source[start:end].splitlines()[1:]
            if line.strip().startswith('"')
        ]
        assert listed, "no hidden imports were parsed"

        importable = []
        for module in listed:
            if module.startswith("app.") or module.startswith("uvicorn"):
                importlib.import_module(module)
                importable.append(module)
        assert len(importable) == len(listed), listed

    def test_uvicorn_dynamic_internals_are_covered(self):
        """uvicorn picks its loop, protocol and lifespan implementations from
        strings, which static analysis cannot follow."""
        source = self._spec_source()
        for needed in (
            "uvicorn.loops.auto",
            "uvicorn.protocols.http.auto",
            "uvicorn.protocols.http.h11_impl",
            "uvicorn.protocols.websockets.auto",
            "uvicorn.lifespan.on",
        ):
            assert needed in source, f"{needed} missing from the spec"

    def test_submodule_collection_is_enabled(self):
        source = self._spec_source()
        assert 'collect_submodules("uvicorn")' in source
        assert 'collect_submodules("websockets")' in source

    def test_every_bundled_data_file_exists(self):
        """A typo silences the file silently (`if source.exists()`), so the
        sample data would simply be missing from the shipped executable."""
        source = self._spec_source()
        start = source.index("datas = []")
        end = source.index("block_cipher", start)
        block = source[start:end]
        missing = [
            line.split('"')[1]
            for line in block.splitlines()
            if line.strip().startswith('("') and not (ROOT / line.split('"')[1]).exists()
        ]
        assert missing == [], f"bundled files that do not exist: {missing}"

    def test_sample_data_is_bundled_so_backtests_work_immediately(self):
        assert '"data/sample.csv"' in self._spec_source()

    def test_the_desktop_gui_is_excluded(self):
        """PySide6 is not a declared dependency; bundling it would double the
        download for an optional component."""
        assert '"PySide6"' in self._spec_source()

    def test_upx_is_disabled(self):
        """UPX-compressed executables are flagged by most antivirus vendors."""
        assert "upx=False" in self._spec_source()

    def test_console_is_enabled_so_the_operator_can_see_status(self):
        assert "console=True" in self._spec_source()


class TestBuildScript:
    def test_script_exists_and_parses(self):
        ast.parse((ROOT / "build_exe.py").read_text("utf-8"))

    def test_script_smoke_tests_from_a_foreign_working_directory(self):
        """The smoke test must not run the executable from the project folder,
        or it would pass while hiding every relative-path bug."""
        source = (ROOT / "build_exe.py").read_text("utf-8")
        assert "cwd=str(home)" in source
        assert "ARENA_HOME" in source

    def test_script_checks_the_artifact_exists(self):
        source = (ROOT / "build_exe.py").read_text("utf-8")
        assert "ARTIFACT.exists()" in source
