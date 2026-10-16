"""The feature console: catalogue fidelity, safety of the runner, and the
promise that the GUI can drive EVERY feature the platform offers.

The most important tests here are the ones that would catch the console
drifting away from the CLI — a GUI that shows a button for a command that no
longer exists, or misses one that does, is worse than no GUI.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.console.catalog import catalogue
from app.console.runner import ConsoleError, build_argv, run_command

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cat() -> dict:
    return catalogue()


# --------------------------------------------------------------------------
# The catalogue is derived from the parsers, not maintained by hand
# --------------------------------------------------------------------------


class TestCatalogueIsDerivedFromTheParsers:
    def test_every_cli_subcommand_is_present(self, cat):
        """Nothing the CLI offers may be missing from the GUI."""
        from app.__main__ import main  # noqa: F401  (import proves the module loads)

        names = {c["name"] for c in cat["cli"]}
        assert "live-status" in names
        assert "research" in names
        assert "backup" in names
        assert len(names) == len(cat["cli"]), "duplicate command names"

    def test_catalogue_matches_the_parser_exactly(self, cat):
        """Bidirectional: the GUI lists exactly what the parser accepts."""
        import argparse

        from app.__main__ import parser as app_parser

        parser = app_parser()
        action = next(
            a for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)
        )
        from_parser = set(action.choices)
        from_catalogue = {c["name"] for c in cat["cli"]}
        assert from_parser == from_catalogue

    def test_every_cli_command_has_a_description(self, cat):
        """A GUI full of blank buttons is not usable."""
        missing = [c["name"] for c in cat["cli"] if not c["summary"]]
        assert missing == [], f"commands with no help text: {missing}"

    def test_every_run_mode_is_present(self, cat):
        names = {m["name"] for m in cat["modes"]}
        assert names == {"backtest", "tournament", "validate", "walk-forward",
                         "robustness", "paper", "api"}

    def test_flags_and_choices_are_exposed(self, cat):
        """Options an operator needs must be reachable from the form."""
        research = next(c for c in cat["cli"] if c["name"] == "research")
        flags = {o["flag"] for o in research["options"]}
        assert "--periods" in flags

        backup = next(c for c in cat["cli"] if c["name"] == "backup")
        action_arg = next(p for p in backup["positionals"] if p["name"] == "action")
        assert set(action_arg["choices"]) == {"backup", "verify", "restore"}

    def test_choice_fields_carry_their_choices(self, cat):
        """Drop-downs are built from the parser's own `choices=`, so a form can
        never offer a value the command would reject."""
        for command in cat["cli"]:
            for positional in command["positionals"]:
                if positional["choices"]:
                    assert all(isinstance(c, str) for c in positional["choices"])

    def test_subcommands_are_flagged_as_variadic_actions(self, cat):
        """`history`, `dataset`, `backup` etc. take an action word."""
        for name in ("history", "dataset", "backup", "universe", "research",
                     "market-universe", "lifecycle"):
            command = next(c for c in cat["cli"] if c["name"] == name)
            assert command["positionals"], f"{name} should have a positional action"

    def test_hazard_flags_are_set(self, cat):
        by_name = {c["name"]: c for c in cat["cli"]}
        assert by_name["live-halt"]["state_changing"] is True
        assert by_name["sandbox-test"]["state_changing"] is True
        assert by_name["engine"]["reaches_network"] is True
        assert by_name["live-digest"]["reaches_network"] is False
        assert by_name["paper-live"]["long_running"] is not None


# --------------------------------------------------------------------------
# The runner refuses anything that is not a real feature, and never uses a shell
# --------------------------------------------------------------------------


class TestRunnerSafety:
    @pytest.mark.parametrize("command", [
        "rm -rf /",
        "live-status; rm -rf /",
        "$(whoami)",
        "engine && echo pwned",
        "live-status | cat",
        "../../bin/sh",
        "",
    ])
    def test_non_features_are_refused(self, command):
        with pytest.raises(ConsoleError):
            build_argv("app", command, [])

    def test_unknown_program_is_refused(self):
        with pytest.raises(ConsoleError):
            build_argv("bash", "live-status", [])

    def test_null_bytes_are_refused(self):
        with pytest.raises(ConsoleError):
            build_argv("app", "ask", ["what\x00now"])

    def test_overlong_argument_is_refused(self):
        with pytest.raises(ConsoleError):
            build_argv("app", "ask", ["x" * 5000])

    def test_non_string_arguments_are_refused(self):
        with pytest.raises(ConsoleError):
            build_argv("app", "ask", ["ok", 5])  # type: ignore[list-item]

    def test_argv_is_a_list_not_a_shell_string(self):
        argv = build_argv("app", "ask", ["what is my pnl?"])
        assert isinstance(argv, list)
        assert all(isinstance(part, str) for part in argv)
        assert argv[3:] == ["ask", "what is my pnl?"]

    def test_run_modes_translate_to_run_py(self):
        argv = build_argv("run", "backtest", ["--symbol", "X"])
        assert argv[1].endswith("run.py")
        assert argv[2:4] == ["--mode", "backtest"]

    def test_runner_does_not_use_a_shell(self):
        """Source-level guarantee: no `shell=True` anywhere in the runner."""
        source = (ROOT / "app" / "console" / "runner.py").read_text("utf-8")
        assert "shell=True" not in source
        assert "shell=False" in source

    def test_runner_sets_no_environment_variables(self):
        """The console must not be able to enable a mode the operator has not
        already enabled — so it never writes into the child environment."""
        source = (ROOT / "app" / "console" / "runner.py").read_text("utf-8")
        assert "env=os.environ.copy()" in source
        assert "os.environ[" not in source.replace("os.environ.copy()", "")
        assert "putenv" not in source

    def test_command_not_in_catalogue_is_refused(self):
        with pytest.raises(ConsoleError):
            build_argv("app", "definitely-not-a-command", [])


# --------------------------------------------------------------------------
# The console really can reach every feature
# --------------------------------------------------------------------------


class TestEveryFeatureIsReachable:
    def test_all_commands_build_a_runnable_argv(self, cat):
        for command in cat["cli"]:
            argv = build_argv("app", command["name"], [])
            assert argv[:3] == [sys.executable, "-m", "app"]
            assert argv[3] == command["name"]

    def test_all_modes_build_a_runnable_argv(self, cat):
        for mode in cat["modes"]:
            argv = build_argv("run", mode["name"], [])
            assert argv[2:4] == ["--mode", mode["name"]]

    def test_every_command_is_listed_in_the_gui_help_output(self, cat):
        """Guard against the CLI and the console disagreeing: every command the
        console shows must also appear in `python -m app --help`."""
        result = subprocess.run(
            [sys.executable, "-m", "app", "--help"],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0
        for command in cat["cli"]:
            assert command["name"] in result.stdout, f"{command['name']} missing from --help"


# --------------------------------------------------------------------------
# Running a real command end to end
# --------------------------------------------------------------------------


class TestRunningCommands:
    def test_read_only_command_runs_and_is_captured(self, tmp_path):
        result = run_command(
            "app", "live-status", [],
            timeout=120, cwd=ROOT,
        )
        assert result.exit_code == 0
        assert result.ok is True
        assert result.stdout.strip().startswith("{")
        assert result.duration_ms >= 0

    def test_failure_is_reported_not_raised(self):
        """A command that exits non-zero must come back as DATA (so the GUI can
        show the output), never as an exception."""
        result = run_command("app", "upstox-login", [], timeout=120, cwd=ROOT)
        assert result.exit_code != 0
        assert result.ok is False
        assert result.stderr or result.stdout

    def test_output_is_captured_from_stderr_too(self):
        result = run_command("app", "research", ["run", "--periods", "2,abc"],
                             timeout=120, cwd=ROOT)
        combined = result.stdout + result.stderr
        assert "comma-separated integers" in combined
        assert result.exit_code == 2

    def test_timeout_kills_the_command_and_says_so(self):
        """A genuinely long-running command must be stopped and labelled rather
        than hanging the server. `run.py --mode api` serves until killed, so it
        is the honest case to test: with a 3s budget the runner must kill the
        whole process group and report the timeout."""
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        result = run_command("run", "api", ["--host", "127.0.0.1", "--port", str(port)],
                             timeout=3, cwd=ROOT)
        assert result.timed_out is True, result
        assert result.ok is False
        assert "still running" in result.stderr

    def test_monitor_is_not_misclassified_as_long_running(self, cat):
        """`monitor` reads like a daemon but is a one-shot snapshot. Mislabeling
        it would tell operators to expect a process that never returns."""
        monitor = next(c for c in cat["cli"] if c["name"] == "monitor")
        assert monitor["long_running"] is None

    def test_concurrency_is_refused_rather_than_corrupting_the_database(self):
        import threading

        from app.console import runner

        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            runner._run_lock.acquire()
            try:
                release.wait(timeout=10)
            finally:
                runner._run_lock.release()

        thread = threading.Thread(target=slow, daemon=True)
        thread.start()
        assert started.wait(timeout=5)
        try:
            with pytest.raises(ConsoleError) as excinfo:
                run_command("app", "live-status", [], timeout=10, cwd=ROOT)
            assert "already running" in str(excinfo.value)
        finally:
            release.set()
            thread.join(timeout=5)


# --------------------------------------------------------------------------
# The page itself
# --------------------------------------------------------------------------


class TestThePage:
    def test_page_renders_with_the_version(self):
        from app.console.page import render

        html = render()
        assert "__VERSION__" not in html
        assert "<title>" in html

    def test_page_embeds_no_token_value(self):
        """The page constructs an Authorization header at runtime (that is the
        design), so "Bearer" appearing in the source is expected. What must
        NEVER happen is a token value being baked into the page."""
        import re as _re

        from app.console.page import render

        html = render()
        # a literal token assignment, e.g. var token = "abc123";
        baked = _re.findall(r'SET\s+TOKEN\s*=|[Tt]oken\s*=\s*"[^"]+"', html)
        assert baked == [], f"page has a baked-in token: {baked}"
        # NOTE: "password" is NOT forbidden — `type="password"` is how the
        # token input is masked, which is the desired behaviour. A credential
        # VALUE is what must never appear.
        for forbidden in ("api_token=", "client_secret", "client_id=", "refresh_token"):
            assert forbidden not in html.lower(), f"page leaks {forbidden!r}"
        assert "sessionStorage" in html, "the token must live in session storage only"
        assert 'type="password"' in html, "the token field must be masked"

    def test_page_has_no_duplicate_class_attributes(self):
        from app.console.page import render

        html = render()
        offenders = re.findall(r"<[^>]*\bclass=\"[^\"]*\"[^>]*\bclass=\"[^\"]*\"[^>]*>", html)
        assert offenders == [], offenders

    def test_page_has_no_invalid_python_escapes(self):
        """The page is a Python string literal, so a stray backslash is a
        Python bug even though it looks like JavaScript."""
        import warnings

        path = ROOT / "app" / "console" / "page.py"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            compile(path.read_text("utf-8"), str(path), "exec")
        offenders = [w for w in caught if "invalid escape" in str(w.message)]
        assert offenders == []

    def test_the_js_regex_survives_to_the_browser(self):
        """The only backslash in the page must reach JS as one backslash."""
        from app.console.page import render

        html = render()
        assert "/\\s+/" in html

    def test_page_asks_for_a_token_before_loading_the_catalogue(self):
        from app.console.page import render

        html = render()
        assert "sessionStorage.getItem(TOKEN_KEY)" in html
        assert "if (token) load();" in html
        assert "else askToken();" in html

    def test_page_can_be_used_from_a_keyboard(self):
        """Ctrl/Cmd+Enter reruns, the terminal reflex."""
        from app.console.page import render

        assert "e.key === \"Enter\"" in render()


class TestPageBugsFixedDuringDevelopment:
    """Four defects found by reading the page's own JavaScript. Each one is the
    kind that renders fine and misbehaves quietly, so each gets a pin."""

    def _page(self) -> str:
        from app.console.page import render

        return render()

    def test_form_is_built_before_the_command_preview(self):
        """preview() reads the live DOM. Rendering the command line before the
        form showed the PREVIOUS command's arguments."""
        html = self._page()
        select_body = html[html.index("function select(cmd)"):html.index("function fieldLabel")]
        assert select_body.index("buildFields(cmd)") < select_body.index("preview(cmd)")

    def test_recent_runs_store_a_real_payload(self):
        """A run used to push `payload: lastPayload` while nothing ever assigned
        it, so clicking a recent run threw on null."""
        html = self._page()
        assert "lastPayload = {program:" in html, "the payload must be captured"
        assert "&& lastPayload)" in html, "null payloads must not be recorded"

    def test_replaying_a_run_restores_flags_not_just_positionals(self):
        """The replay helper only filled positionals, silently dropping every
        flag — a replayed command could run with different meaning."""
        html = self._page()
        body = html[html.index("function applyPayload"):html.index("function load()")]
        assert 'data-flag="' in body or "data-flag=" in body
        assert "field.checked = true" in body

    def test_the_run_list_does_not_shadow_window_history(self):
        """`var history` at global scope shadows window.history; if that ever
        becomes non-writable the whole script dies before painting."""
        html = self._page()
        assert "var history" not in html
        assert "recentRuns" in html

    def test_script_is_in_strict_mode(self):
        """Strict mode turns accidental globals into errors instead of silent
        state leaks."""
        assert '"use strict"' in self._page()

    def test_token_can_be_entered_without_a_modal(self):
        """`prompt()` returns null both on cancel AND when the browser blocks
        modals (any iframe without allow-modals). Relying on it alone would
        leave the console unusable in an embedded preview, so there is an
        inline field that always works."""
        html = self._page()
        assert 'id="tokeninput"' in html
        assert 'id="tokenset"' in html
        assert "showTokenBar" in html
        # the inline path must not go through prompt()
        marker = 'getElementById("tokenset").onclick'
        assert marker in html
        setter = html[html.index(marker):]
        setter = setter[:setter.index("\n\n")]
        assert "prompt(" not in setter
        assert "applyToken(" in setter

    def test_token_is_masked_and_never_echoed(self):
        html = self._page()
        assert 'type="password"' in html

    def test_a_rejected_token_reveals_the_field_again(self):
        html = self._page()
        assert "err.status === 401" in html
        assert "showTokenBar(true)" in html
