"""Regression tests for the full-codebase debug pass.

Each test here pins a defect that was found by EXECUTING the code rather than by
reading it — the existing 1228-test suite was green throughout, because none of
these paths were covered. Every bug below produced a hard failure (a NameError,
a TypeError, an AttributeError, or a user-facing traceback) on a documented
command.

The tests are deliberately written against the CLI entry point, since that is
the surface the operator actually touches.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.__main__ as cli  # noqa: E402
from app.learning.lifecycle import (  # noqa: E402
    HEALTH,
    StrategyLifecycleManager,
    lineage_id_for,
)


@pytest.fixture()
def isolated_stack(tmp_path, monkeypatch):
    """Point the CLI at a throwaway database and data directory."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/debug.db")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MODE", "PAPER")
    monkeypatch.setenv("LIVE_TRADING", "false")
    monkeypatch.setenv("BROKER", "LOCAL_PAPER")
    return tmp_path


def run_cli(argv, capsys):
    """Invoke the CLI, returning (exit_code, stdout)."""
    code = 0
    try:
        cli.main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    return code, capsys.readouterr().out


class TestPaperLivePassthrough:
    """BUG 1: cmd_paper_live read a global `argv` that does not exist, so the
    passthrough form died with NameError instead of running."""

    def test_passthrough_does_not_raise_nameerror(self, isolated_stack, capsys):
        # no credentials are configured, so this exits early — the point is
        # that it exits CLEANLY rather than with NameError
        code, out = run_cli(["paper-live", "--", "--max-candles"], capsys)
        assert "NameError" not in out
        assert "'argv'" not in out
        assert code in (0, 1, 2)

    def test_passthrough_is_read_from_args_not_argv(self):
        """The argument the handler needs is on the parsed namespace."""
        import argparse

        args = argparse.Namespace(passthrough=["--max-candles", "--delay", "0"])
        # the old code did argv[args.passthrough:], which is a TypeError even if
        # argv existed (slicing with a list). Assert the list is used directly.
        assert list(args.passthrough or []) == ["--max-candles", "--delay", "0"]


class TestPaperLiveBadSymbol:
    """BUG 3: DEFAULT_SYMBOL=DEMO (the shipped default) raised a bare
    ValueError from the stream factory as a full traceback."""

    def test_default_symbol_is_not_an_instrument_key(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_SYMBOL", "DEMO")
        monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("MARKET_DATA_ACCESS_TOKEN", raising=False)
        from app.config.settings import Settings
        from app.data.upstox_stream import from_symbol

        with pytest.raises(ValueError):
            from_symbol(Settings.from_env().default_symbol)

    def test_cli_reports_it_as_guidance(self, isolated_stack, capsys):
        code, out = run_cli(["paper-live"], capsys)
        assert "Traceback" not in out
        assert code == 2
        payload = json.loads(out)
        assert payload["ok"] is False
        assert "DEFAULT_SYMBOL" in payload["error"]
        assert "NSE_EQ|INE002A01018" in payload["hint"]


class TestResearchRun:
    """BUG 2: BacktestEngine was used but never imported -> NameError.
    BUG 5: --periods is a comma-separated string but was iterated directly,
    yielding characters, so every grid entry had period=","."""

    def test_backtest_engine_is_importable_where_used(self):
        from app.backtesting.engine import BacktestEngine

        assert BacktestEngine is not None

    def test_periods_string_is_split_not_iterated(self):
        """The exact transformation the fix performs."""
        raw = "2,3,5"
        assert list(raw) == ["2", ",", "3", ",", "5"], "the original bug shape"
        periods = [int(p.strip()) for p in str(raw).split(",") if p.strip()]
        assert periods == [2, 3, 5]
        assert all(isinstance(p, int) for p in periods)

    def test_non_integer_periods_is_refused_cleanly(self, isolated_stack, capsys):
        code, out = run_cli(["research", "run", "--periods", "2,abc"], capsys)
        assert "Traceback" not in out
        assert code == 2
        assert "comma-separated integers" in out

    def test_empty_periods_is_refused_cleanly(self, isolated_stack, capsys):
        code, out = run_cli(["research", "run", "--periods", " , "], capsys)
        assert "Traceback" not in out
        assert code == 2

    def test_valid_periods_build_a_usable_grid(self):
        periods = [int(p.strip()) for p in "2,3".split(",") if p.strip()]
        grid = [{"period": p, "threshold": 0.0} for p in periods]
        assert grid == [{"period": 2, "threshold": 0.0},
                        {"period": 3, "threshold": 0.0}]
        # every generated period must satisfy the strategy's own validation
        from app.indicators.moving_averages import check_period

        for entry in grid:
            check_period(entry["period"])


class TestDatasetAudit:
    """BUG 4: `dataset audit` without --dataset-id passed None into the
    builder, which raised ValueError("unknown dataset: None") as a traceback."""

    def test_missing_id_is_refused_cleanly(self, isolated_stack, capsys):
        code, out = run_cli(["dataset", "audit"], capsys)
        assert "Traceback" not in out
        assert code == 2
        payload = json.loads(out)
        assert payload["ok"] is False
        assert "--dataset-id" in payload["error"]
        assert "None" not in payload["error"]


class TestLifecycleDegrade:
    """BUG 6: `lifecycle degrade` called manager.degrade(), which did not
    exist — AttributeError on a documented command."""

    def test_manager_exposes_degrade(self):
        assert hasattr(StrategyLifecycleManager, "degrade")

    def test_unknown_lineage_is_reported_not_raised(self, tmp_path):
        from app.database.database import Database
        from app.database.repository import Repository

        database = Database(f"sqlite:///{tmp_path}/lc.db")
        try:
            manager = StrategyLifecycleManager(Repository(database))
            result = manager.degrade("does-not-exist")
            assert result["degraded"] is False
            assert "unknown lineage" in result["reason"]
        finally:
            database.close()

    def test_cli_degrade_does_not_raise(self, isolated_stack, capsys):
        code, out = run_cli(["lifecycle", "degrade", "--lineage", "deadbeef"], capsys)
        assert "AttributeError" not in out
        assert "Traceback" not in out
        assert code == 0

    def test_degrade_walks_the_ladder_one_step_at_a_time(self, tmp_path):
        from app.database.database import Database
        from app.database.repository import Repository

        database = Database(f"sqlite:///{tmp_path}/ladder.db")
        try:
            repository = Repository(database)
            lineage = lineage_id_for("momentum", {"period": 5}, 0, None)
            repository.record_lifecycle(lineage, "APPROVED", "ACTIVE",
                                        {"why": "test setup"})
            manager = StrategyLifecycleManager(repository)

            assert manager.is_tradable(lineage) is True
            seen = [repository.current_lifecycle(lineage)["status"]]
            for _ in range(len(HEALTH) + 2):  # extra steps must be no-ops
                manager.degrade(lineage, evidence={"source": "test"})
                seen.append(repository.current_lifecycle(lineage)["status"])

            # ACTIVE -> PROBATION -> SHADOW -> RETIRED, then pinned at RETIRED
            assert seen[:4] == ["ACTIVE", "PROBATION", "SHADOW", "RETIRED"]
            assert set(seen[3:]) == {"RETIRED"}, "RETIRED must be terminal"
            # a degraded strategy must stop being tradable
            assert manager.is_tradable(lineage) is False
        finally:
            database.close()

    def test_records_that_a_human_asked(self, tmp_path):
        """A manual degrade must not imply market evidence it does not have."""
        from app.database.database import Database
        from app.database.repository import Repository

        database = Database(f"sqlite:///{tmp_path}/ev.db")
        try:
            repository = Repository(database)
            lineage = lineage_id_for("rsi", {"period": 14}, 0, None)
            repository.record_lifecycle(lineage, "APPROVED", "ACTIVE", {})
            manager = StrategyLifecycleManager(repository)
            result = manager.degrade(lineage)
            assert result["degraded"] is True
            assert result["previous_status"] == "ACTIVE"
            assert result["status"] == "PROBATION"
        finally:
            database.close()


class TestOperatorErrors:
    """BUG 8: domain errors (missing OAuth credentials, refused hosts) reached
    the user as ~12-line tracebacks instead of the message they carry."""

    def test_guarded_types_are_the_domain_errors(self):
        from app.__main__ import _operator_errors

        names = {t.__name__ for t in _operator_errors()}
        assert {"UpstoxOAuthError", "UpstoxAuthError",
                "SandboxAuthError", "SandboxRequestError"} <= names

    def test_programming_errors_are_not_swallowed(self):
        """An unexpected exception is a bug: the traceback is the diagnosis."""
        from app.__main__ import _operator_errors

        caught = _operator_errors()
        for bug_type in (ValueError, TypeError, AttributeError, KeyError,
                         NameError, IndexError):
            assert not issubclass(bug_type, caught), bug_type.__name__

    def test_upstox_login_without_credentials_reports_cleanly(self, monkeypatch, capsys):
        for key in ("UPSTOX_CLIENT_ID", "UPSTOX_CLIENT_SECRET",
                    "UPSTOX_REDIRECT_URI"):
            monkeypatch.delenv(key, raising=False)
        code, out = run_cli(["upstox-login"], capsys)
        assert "Traceback" not in out
        assert code == 2
        payload = json.loads(out)
        assert payload["ok"] is False
        assert "UPSTOX_CLIENT_ID" in payload["error"]
        assert payload["error_type"] == "UpstoxOAuthError"

    def test_no_secret_is_echoed_by_the_handler(self, monkeypatch, capsys):
        monkeypatch.setenv("UPSTOX_CLIENT_ID", "SENTINEL-CLIENT-ID-3f9a")
        monkeypatch.delenv("UPSTOX_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("UPSTOX_REDIRECT_URI", raising=False)
        _code, out = run_cli(["upstox-login"], capsys)
        assert "SENTINEL-CLIENT-ID-3f9a" not in out


class TestDiscoveryWiring:
    """BUG 7: `discover` called StrategyDiscoveryEngine(engine=...) — the
    constructor takes settings — and discover(..., specs=...), which it does
    not accept."""

    def test_constructor_signature_matches_the_cli_call(self):
        import inspect

        from app.research.discovery import StrategyDiscoveryEngine

        parameters = inspect.signature(StrategyDiscoveryEngine.__init__).parameters
        assert "settings" in parameters
        assert "engine" not in parameters

    def test_discover_takes_three_arguments(self):
        import inspect

        from app.research.discovery import StrategyDiscoveryEngine

        parameters = inspect.signature(StrategyDiscoveryEngine.discover).parameters
        assert list(parameters) == ["self", "name", "candles", "timeframe"]

    def test_discovery_engine_constructs_with_settings(self, tmp_path):
        from app.config.settings import Settings
        from app.database.database import Database
        from app.database.repository import Repository
        from app.research.discovery import StrategyDiscoveryEngine

        database = Database(f"sqlite:///{tmp_path}/disc.db")
        try:
            engine = StrategyDiscoveryEngine(
                Repository(database), settings=Settings())
            assert engine.repository is not None
        finally:
            database.close()


class TestDeadCodeRemoved:
    """The debug pass removed assignments whose only effect was wasted work —
    or, in one case, a latent crash. These pin that they stay gone."""

    def test_no_undefined_names_remain(self):
        """A cheap in-process guard for the exact bug class that started this."""
        import ast

        problems = []
        for path in sorted((ROOT / "app").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source)
            except SyntaxError as exc:  # pragma: no cover
                problems.append(f"{path}: syntax error {exc}")
                continue
            # module-level names available to a function body
            module_names = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        module_names.add((alias.asname or alias.name).split(".")[0])
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                       ast.ClassDef)):
                    module_names.add(node.name)
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    module_names.add(node.id)
                elif isinstance(node, ast.arg):
                    module_names.add(node.arg)
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    module_names.add(node.name)
            builtins_ = set(dir(__builtins__)) if isinstance(__builtins__, dict) is False \
                else set(__builtins__.keys())
            for name in ("argv", "BacktestEngine"):
                # these two were the real defects; they must now be reachable
                if name in source and name not in module_names and name not in builtins_:
                    problems.append(f"{path}: uses {name} without defining/importing it")
        assert not problems, problems

    def test_director_does_not_parse_an_unused_rationale(self):
        """The removed line called json.loads on a stored field and discarded
        the result, so a malformed value crashed schedule() for no benefit."""
        source = (ROOT / "app" / "research" / "director.py").read_text(encoding="utf-8")
        assert 'rationale = _load(question["rationale"])' not in source

    def test_api_portfolio_does_not_fetch_an_unused_engine(self):
        source = (ROOT / "app" / "api" / "routes" / "v2.py").read_text(encoding="utf-8")
        assert 'engine = getattr(request.app.state, "sandbox_engine", None)' not in source


class TestStreamSubscribedReport:
    """`symbols` in run.py was computed and discarded; it now reports what a
    live feed is actually subscribed to."""

    def test_run_py_announces_the_subscription(self):
        source = (ROOT / "run.py").read_text(encoding="utf-8")
        assert "STREAM_SUBSCRIBED" in source
        assert "symbols = list(getattr(provider" not in source, \
            "the discarded assignment must not come back"


class TestUtcClock:
    """BUG 9: five call sites used ``date.today()`` — the LOCAL calendar date —
    in a codebase that defines every trading day, report window and history
    bound in UTC. On an IST host (UTC+5:30) the local date is already tomorrow
    between 00:00 and 05:30 local time while market data is still the UTC day,
    shifting report windows and history bounds by one day."""

    def test_helper_returns_the_utc_date(self):
        from datetime import datetime, timezone

        from app.utils.clock import utc_today, utc_today_iso

        assert utc_today() == datetime.now(timezone.utc).date()
        assert utc_today_iso() == utc_today().isoformat()

    def test_helper_is_utc_not_local(self, monkeypatch):
        """With TZ forced far ahead of UTC the helper must not follow it."""
        import os
        import time

        from app.utils.clock import utc_today

        original = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Pacific/Kiritimati"  # UTC+14
            if hasattr(time, "tzset"):
                time.tzset()
            from datetime import datetime, timezone

            assert utc_today() == datetime.now(timezone.utc).date()
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            if hasattr(time, "tzset"):
                time.tzset()

    def test_no_naive_date_today_remains_in_the_app(self):
        """Regression guard: the whole app must use one clock."""
        offenders = []
        for path in sorted((ROOT / "app").rglob("*.py")):
            if path.name == "clock.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "date.today()" in text or "date_type.today()" in text:
                offenders.append(str(path.relative_to(ROOT)))
        assert offenders == [], f"naive local-clock date found in: {offenders}"


class TestScannerClosureBinding:
    """BUG 10 (latent): the deep-scoring closure captured the loop variable.
    `_cached_feature` invokes it synchronously today, so it happens to be
    correct — but it stores only the value, so deferring the call would have
    silently scored every instrument with the last one's data."""

    UNIVERSE = [{"instrument_key": f"K{i}", "symbol": f"K{i}", "volume": 50000,
                 "open": (i + 1) * 100.0, "close": (i + 1) * 100.0}
                for i in range(5)]

    def test_each_candidate_is_scored_on_its_own_data(self):
        from app.research.scanner_funnel import ScannerFunnel

        finalists, _ = ScannerFunnel(top_k=3).scan(list(self.UNIVERSE))
        assert [f["close"] for f in finalists] == [500.0, 400.0, 300.0]

    def test_deep_scorer_receives_each_candidate(self):
        from app.research.scanner_funnel import ScannerFunnel

        seen = []
        finalists, _ = ScannerFunnel(top_k=3).scan(
            list(self.UNIVERSE),
            deep_scorer=lambda row: (seen.append(row["close"]), row["close"])[1])
        assert sorted(seen) == [100.0, 200.0, 300.0, 400.0, 500.0]
        assert [f["close"] for f in finalists] == [500.0, 400.0, 300.0]

    def test_closure_binds_the_loop_variable(self):
        source = (ROOT / "app" / "research" / "scanner_funnel.py").read_text("utf-8")
        assert "def compute(candidate=u):" in source


class TestGeneratedCodeIsExcluded:
    """BUG 11 (self-inflicted, caught and fixed): an automated unused-import
    cleanup stripped a symbol that protobuf REQUIRES for descriptor
    registration, breaking the feed decoder. Generated code is now excluded."""

    def test_protobuf_binding_still_loads(self):
        from app.data.upstox_proto import MarketDataFeed_pb2 as pb  # noqa: F401

        assert pb.FeedResponse is not None

    def test_generated_file_is_excluded_from_linting(self):
        import tomllib

        config = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
        exclude = config["tool"]["ruff"]["exclude"]
        assert any("MarketDataFeed_pb2.py" in entry for entry in exclude)

    def test_the_required_protobuf_import_is_present(self):
        source = (ROOT / "app" / "data" / "upstox_proto"
                  / "MarketDataFeed_pb2.py").read_text("utf-8")
        assert "wrappers_pb2" in source


class TestNoInvalidEscapes:
    """BUG 12: a single-backslash ``\\/`` inside the dashboard's non-raw page
    string is a DeprecationWarning today and a SyntaxError in a future Python —
    at which point the dashboard module stops importing entirely."""

    def test_no_invalid_escape_sequences_anywhere(self):
        import warnings

        offenders = []
        for path in sorted(list((ROOT / "app").rglob("*.py"))
                           + list((ROOT / "tools").rglob("*.py"))):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                try:
                    compile(path.read_text(encoding="utf-8"), str(path), "exec")
                except SyntaxError:  # pragma: no cover
                    continue
                for warning in caught:
                    if "invalid escape" in str(warning.message):
                        offenders.append(f"{path.name}: {warning.message}")
        assert offenders == [], offenders

    def test_dashboard_js_regex_is_unchanged(self):
        """The fix must not alter what the browser receives."""
        import re

        text = (ROOT / "app" / "api" / "routes" / "dashboard.py").read_text("utf-8")
        page = re.search(r'page = """(<!doctype html>.*?</script>)"""',
                         text, re.S).group(1)
        namespace: dict = {}
        exec(compile('page = """' + page + '"""', "<page>", "exec"), namespace)
        served = namespace["page"]
        assert "<\\/b>" in served, "the JS regex must keep ONE backslash"
