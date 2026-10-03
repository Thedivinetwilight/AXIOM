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
        sample data would simply be missing from the shipped executable.

        ``app/BUILD_META.json`` is the one deliberate exception: it is
        generated by ``build_exe.py`` immediately before PyInstaller runs and
        is gitignored, so a clean checkout never contains it. The spec must
        guard it with an existence check (``app.build_info`` degrades to the
        git/source fallback when absent) and ``build_exe.py`` must write it —
        both are asserted here and elsewhere."""
        source = self._spec_source()
        start = source.index("datas = []")
        end = source.index("block_cipher", start)
        block = source[start:end]
        assert '"app/BUILD_META.json"' in block, (
            "BUILD_META.json must be bundled so the frozen exe reports its "
            "exact build id")
        assert "if source.exists()" in block, (
            "BUILD_META.json is generated at build time; the spec must guard "
            "its inclusion so a bare `pyinstaller` invocation still works")
        missing = [
            line.split('"')[1]
            for line in block.splitlines()
            if line.strip().startswith('("') and not (ROOT / line.split('"')[1]).exists()
        ]
        missing = [m for m in missing if m != "app/BUILD_META.json"]
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
        """The frozen verification must not run the executable from the
        project folder, or it would pass while hiding every relative-path bug.
        Since 3.1.0 that verification lives in packaging/frozen_e2e.py (run by
        `build_exe.py --test`), and it launches the artifact from a directory
        that is neither the repository nor the data home."""
        source = (ROOT / "packaging" / "frozen_e2e.py").read_text("utf-8")
        assert "launch_dir" in source
        assert "cwd=str(launch_dir)" in source
        assert "ARENA_HOME" in source
        assert "neither the repo nor the home" in source

    def test_script_checks_the_artifact_exists(self):
        source = (ROOT / "build_exe.py").read_text("utf-8")
        assert "ARTIFACT.exists()" in source


class TestWindowsPortability:
    """Bugs found by running the suite in the CI matrix — the only place this
    code is exercised on Windows and macOS at all."""

    def test_no_module_imports_posix_only_stdlib_at_the_top_level(self):
        """`resource` does not exist on Windows. Importing it unguarded made
        app/monitoring/system.py unimportable there, which broke
        `python -m app monitor` and every engine system snapshot."""
        offenders = []
        for path in sorted((ROOT / "app").rglob("*.py")):
            tree = ast.parse(path.read_text("utf-8"))
            for node in tree.body:  # top level only
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    if name in {"resource", "pwd", "grp", "fcntl", "termios",
                                "msvcrt", "tty"}:
                        offenders.append(f"{path.name}: imports {name}")
        assert offenders == [], offenders

    def test_monitoring_reports_memory_without_the_resource_module(self, monkeypatch):
        """Prove the fallback path works, not just that the import is guarded."""
        import builtins

        import app.monitoring.system as monitor

        monkeypatch.setattr(monitor, "_resource", None)
        # must return a value or None, but never raise
        value = monitor._resident_mb()
        assert value is None or isinstance(value, float)

    def test_the_monitor_snapshot_never_needs_resource(self, monkeypatch, tmp_path):
        """Takes a real snapshot with the POSIX module removed, which is the
        situation on Windows."""
        import app.monitoring.system as monitor_module
        from app.database.database import Database
        from app.database.repository import Repository
        from app.monitoring.system import SystemMonitor

        monkeypatch.setattr(monitor_module, "_resource", None)

        database = Database(f"sqlite:///{tmp_path}/monitor.db")
        try:
            monitor = SystemMonitor(Repository(database), data_dir=str(tmp_path))
            snapshot = monitor.snapshot()
        finally:
            database.close()

        assert "ts" in snapshot
        assert snapshot.get("rss_mb") is None or isinstance(snapshot["rss_mb"], float)
        assert "disk_used_pct" in snapshot


class TestOutputsSurviveTheBundle:
    """ROOT is PyInstaller's temporary extraction directory when frozen, and it
    is deleted on exit. Anything written there is lost."""

    def test_output_root_follows_the_home_when_portable(self, frozen):
        from app import runtime

        assert runtime.output_root() == frozen

    def test_output_root_is_the_repo_from_source(self, monkeypatch):
        monkeypatch.delenv("ARENA_HOME", raising=False)
        monkeypatch.delattr(sys, "frozen", raising=False)
        from app import runtime

        assert runtime.output_root() == ROOT

    def test_run_py_writes_outputs_outside_the_bundle(self):
        """results/, logs/ and data/cache are outputs: a backtest launched from
        the .exe must not print a path that disappears."""
        source = (ROOT / "run.py").read_text("utf-8")
        assert 'ROOT / "results"' not in source
        assert 'ROOT / "logs/bot.jsonl"' not in source
        assert 'ROOT / "data/cache"' not in source
        assert source.count("_output_root()") >= 3
        # bundled INPUT data must still be read from the bundle
        assert 'ROOT / "data/sample.csv"' in source

    def test_backtest_writes_where_it_says_it_did(self, frozen, tmp_path):
        """End-to-end: run a backtest in a packaged-style home and confirm the
        reported results file exists."""
        import subprocess

        env = dict(os.environ)
        env["ARENA_HOME"] = str(frozen)
        frozen.mkdir(parents=True, exist_ok=True)
        repo_copy = ROOT / "results" / "backtest" / "result.json"
        before = repo_copy.stat().st_mtime if repo_copy.exists() else 0.0
        result = subprocess.run(
            [sys.executable, "run.py", "--mode", "backtest",
             "--database", f"sqlite:///{tmp_path}/bt.db"],
            capture_output=True, text=True, timeout=600, cwd=str(ROOT), env=env,
        )
        assert result.returncode == 0, result.stderr[-600:]

        written = frozen / "results" / "backtest" / "result.json"
        assert written.exists(), (
            "the backtest wrote nothing into ARENA_HOME; it would have gone to "
            f"ROOT when frozen. Files found: {sorted(p.name for p in frozen.rglob('*'))}"
        )
        # and the project's own results directory was left untouched, so the
        # assertion cannot be broken by an earlier source-mode run
        repo_copy = ROOT / "results" / "backtest" / "result.json"
        assert not repo_copy.exists() or repo_copy.stat().st_mtime < before


class TestPaperShutdown:
    """Ctrl+C used to print a RuntimeError traceback at the end of every clean
    paper session: the signal handler created an unawaited stop() task that ran
    after broker.close() released the repository writer."""

    def test_stop_is_idempotent(self):
        import asyncio
        import inspect

        from app.paper_trading.simulator import PaperSimulator

        source = inspect.getsource(PaperSimulator.stop)
        assert "if self._stopped:" in source
        assert "self._stopped = True" in source

    def test_the_signal_handler_does_not_create_an_orphan_task(self):
        source = (ROOT / "run.py").read_text("utf-8")
        handler = source[source.index("def stop_requested():"):]
        handler = handler[:handler.index("for sig in")]
        assert "create_task" not in handler, "a fire-and-forget stop races the shutdown"
        assert "request_stop()" in handler

    def test_request_stop_does_no_io(self):
        """It must be safe to call from a signal handler, i.e. no repository
        writes — cancelling the stream task is all it may do."""
        import inspect

        from app.paper_trading.simulator import PaperSimulator

        import ast as _ast
        import textwrap

        source = inspect.getsource(PaperSimulator.request_stop)
        tree = _ast.parse(textwrap.dedent(source))
        # strip docstrings: the explanation legitimately mentions broker.close()
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                                 _ast.ClassDef, _ast.Module)):
                body = node.body
                if body and isinstance(body[0], _ast.Expr) and \
                        isinstance(body[0].value, _ast.Constant) and \
                        isinstance(body[0].value.value, str):
                    node.body = body[1:] or [_ast.Pass()]
        code = _ast.unparse(tree)

        assert "cancel()" in code
        for forbidden in ("cancel_all", "persist", "save_broker", "close()"):
            assert forbidden not in code, f"request_stop performs I/O: {forbidden}"

    def test_stopping_twice_is_safe_and_cancels_once(self, tmp_path, monkeypatch):
        """The exact failure: stop() ran twice, the second pass writing to a
        repository whose writer had been released. Driven directly rather than
        through a signal, so it is deterministic and runs on every platform."""
        import asyncio
        from datetime import timezone

        from app.backtesting.costs import ExecutionConfig
        from app.database.database import Database
        from app.database.repository import Repository
        from app.paper_trading.broker import PaperBroker
        from app.paper_trading.simulator import PaperSimulator

        monkeypatch.chdir(tmp_path)
        database = Database(f"sqlite:///{tmp_path}/shutdown.db")
        repository = Repository(database)
        try:
            broker = PaperBroker(
                ExecutionConfig(commission=0.0, slippage=0.0, spread=0.0),
                currency="INR", timeframe="1D", repository=repository,
            )
            simulator = PaperSimulator(broker)

            async def run():
                first = await simulator.stop()
                # broker.close() releases the repository writer — this happened
                # BEFORE the orphaned task ran, which is why the second stop
                # died with "Paper write requires exclusive ownership"
                broker.close()
                # the orphaned signal-handler task's stop() lands here
                second = await simulator.stop()
                return first, second

            first, second = asyncio.run(run())
            assert first["state"] == "STOPPED"
            assert second["state"] == "STOPPED", (
                "stopping after the writer was released must be a safe no-op"
            )
        finally:
            repository.release_writer()
            database.close()

    def test_the_signal_test_is_posix_only(self):
        """Windows cannot deliver SIGINT to a child process from a test, so the
        integration check below is POSIX-only. The behaviour it covers is
        pinned deterministically by the test above on every platform."""
        source = (ROOT / "tests" / "test_packaging_single_exe.py").read_text("utf-8")
        assert "cannot send SIGINT to a child process" in source

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="Windows cannot send SIGINT to a child process; "
                               "the test above covers the same contract directly")
    def test_a_real_session_ends_without_a_traceback(self, frozen, tmp_path):
        import signal
        import subprocess
        import time

        env = dict(os.environ)
        env["ARENA_HOME"] = str(frozen)
        frozen.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [sys.executable, "run.py", "--mode", "paper", "--source", "replay",
             "--symbol", "DEMO", "--delay", "0.05",
             "--database", f"sqlite:///{tmp_path}/paper.db"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            cwd=str(ROOT), env=env,
        )
        time.sleep(8)
        process.send_signal(signal.SIGINT)
        try:
            output, _ = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
            output, _ = process.communicate()

        assert process.returncode == 0, output[-600:]
        assert "Task exception was never retrieved" not in output
        assert "Paper write requires exclusive ownership" not in output


class TestCredentialPathsFollowTheHome:
    """Files that hold credentials must never be written to ROOT when frozen:
    it is PyInstaller's temporary extraction directory, deleted on exit."""

    def test_upstox_login_writes_to_the_runtime_env_file(self):
        """A refreshed daily token written to a temp directory would vanish,
        while the operator saw 'login complete' and the engine kept the stale
        credential."""
        source = (ROOT / "app" / "__main__.py").read_text("utf-8")
        assert 'run_login_flow(ROOT / ".env")' not in source
        assert "run_login_flow(runtime.env_file())" in source

    def test_the_config_checker_inspects_the_file_the_app_loads(self):
        """Validating the repository .env in a packaged build would check a
        configuration the application never sees."""
        source = (ROOT / "tools" / "live_config_check.py").read_text("utf-8")
        assert 'DEFAULT_ENV_FILE = ROOT / ".env"' not in source
        assert "runtime.env_file()" in source

    def test_the_checker_default_is_the_runtime_file(self, frozen, monkeypatch):
        import importlib

        monkeypatch.setenv("ARENA_ENV_FILE", str(frozen / ".env"))
        import tools.live_config_check as checker

        reloaded = importlib.reload(checker)
        try:
            assert Path(reloaded.DEFAULT_ENV_FILE) == frozen / ".env"
        finally:
            monkeypatch.delenv("ARENA_ENV_FILE", raising=False)
            importlib.reload(checker)

    def test_login_flow_defaults_are_unchanged_for_library_callers(self):
        """run_login_flow(env_path=None) still means 'the current directory',
        which is what the existing tests and any embedder expect."""
        import inspect

        from app.data.upstox_auth import run_login_flow

        source = inspect.getsource(run_login_flow)
        assert 'env_path or Path.cwd() / ".env"' in source
