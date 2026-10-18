"""V2.1: v2 API routes, CLI commands, initialize, reporting, platform bits."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings
from app.database.database import Database
from app.database.repository import Repository
from app.reporting.activity import explain, explain_all
from app.reporting.pdf import simple_pdf
from app.reporting.weekly import WeeklyReport

# Project root derived dynamically — tests run from any checkout location.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

TOKEN = {"Authorization": "Bearer v2-test-token-xyz"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/v2api.db")
    monkeypatch.setenv("API_TOKEN", "v2-test-token-xyz")
    monkeypatch.setenv("DATA_SOURCE", "generic")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings = Settings.from_env()
    app = create_app(settings)
    database = Database(settings.database_url)
    app.state.repository = Repository(database)
    with TestClient(app) as test_client:
        yield test_client
    database.close()


class TestV2Api:
    def test_all_pages_require_auth(self, client):
        for path in ("/api/v2/overview", "/api/v2/trades", "/api/v2/performance",
                     "/api/v2/strategies", "/api/v2/learning", "/api/v2/experiments",
                     "/api/v2/market_data", "/api/v2/risk", "/api/v2/logs"):
            assert client.get(path).status_code == 401, path

    def test_overview_shows_mode_and_live_disabled(self, client):
        data = client.get("/api/v2/overview", headers=TOKEN).json()
        assert data["execution_mode"] == "LOCAL_PAPER"
        assert data["live_trading"] is False
        assert "SIMULATED (PAPER)" in data["mode_display"]
        assert "REAL MONEY" not in data["mode_display"]

    def test_market_data_counts(self, client):
        repository = client.app.state.repository
        repository.upsert_instruments([
            {"instrument_key": "K1", "row_hash": "h", "exchange": "NSE"},
            {"instrument_key": "K2", "row_hash": "h2", "exchange": "NSE"},
        ], "test")
        data = client.get("/api/v2/market_data", headers=TOKEN).json()
        assert data["universe"]["total"] == 2

    def test_risk_page_reports_hard_limits(self, client):
        data = client.get("/api/v2/risk", headers=TOKEN).json()
        assert "limits" in data and "learning cannot change" in data["limits_note"]
        assert data["live"] if "live" in data else True

    def test_weekly_report_markdown_and_pdf(self, client):
        markdown = client.get("/api/v2/report/weekly", headers=TOKEN)
        assert markdown.status_code == 200
        assert "DISABLED" in markdown.text and "OBSERVED" in markdown.text
        pdf = client.get("/api/v2/report/weekly?format=pdf", headers=TOKEN)
        assert pdf.status_code == 200
        assert pdf.content.startswith(b"%PDF")

    def test_logs_translate_events(self, client):
        repository = client.app.state.repository
        repository.record_system_event("RISK_REJECT", {"reason": "max_trades_per_day"})
        data = client.get("/api/v2/logs", headers=TOKEN).json()
        assert data["raw_events"] and data["activity"]
        assert "Skipped a trade" in data["activity"][0]["headline"]


class TestReporting:
    def test_activity_unknown_kind_never_invents(self):
        entry = explain({"kind": "TOTALLY_UNKNOWN", "ts": "t", "payload": "{}"})
        assert "totally unknown" in entry["headline"]
        assert "See raw event detail" in entry["why"]

    def test_pdf_is_valid_and_multipage(self):
        lines = [f"line {i}: " + "x" * 90 for i in range(200)]
        pdf = simple_pdf("Multi", lines)
        assert pdf.count(b"/Type /Page") >= 3
        assert pdf.rstrip().endswith(b"%%EOF")

    def test_weekly_labels_all_sections(self, tmp_path):
        database = Database(f"sqlite:///{tmp_path}/weekly.db")
        repository = Repository(database)
        report = WeeklyReport(repository).markdown()
        assert "OBSERVED" in report and "CALCULATED" in report
        assert "MODEL INTERPRETATION" in report or "No model promotions" in report
        # Honest empty-data contract: with nothing recorded the report
        # discloses it explicitly instead of guessing (documented rule).
        assert "no recorded snapshots" in report
        assert "No recorded regime changes" in report
        database.close()


class TestInitializeAndPlatform:
    def test_initialize_idempotent_offline_safe(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/init.db")
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("UNIVERSE_SYNC_URL", "https://invalid.invalid/x.json.gz")
        from app.bootstrap import initialize

        first = initialize(Settings.from_env())
        assert first["ok"] and not first["idempotent"]
        assert first["universe_sync"]["status"] == "unavailable"
        second = initialize(Settings.from_env())
        assert second["idempotent"]
        assert (tmp_path / "data" / "history").exists()

    def test_streaming_universe_guard(self):
        from app.config.settings import Settings

        from app.engine.streaming_universe import resolve_streaming_universe

        assert resolve_streaming_universe(Settings.from_env()) == ["NSE_EQ|INE002A01018"]
        big = type("S", (), {"streaming_universe": ",".join(["K"] * 51),
                             "market_data_symbols": ""})()
        with pytest.raises(ValueError):
            resolve_streaming_universe(big)

    def test_maintenance_scheduler_deterministic(self):
        from datetime import datetime, timedelta

        from app.maintenance.scheduler import MaintenanceScheduler

        calls = []
        scheduler = MaintenanceScheduler([
            {"name": "job", "fn": lambda: calls.append(1), "interval": timedelta(days=1)},
        ])
        now = datetime(2026, 9, 25, 17, 0)
        assert scheduler.run_due(now) == {"job": {"status": "complete", "result": None}}
        assert calls == [1]
        assert scheduler.run_due(now + timedelta(hours=2)) == {}
        assert scheduler.run_due(now + timedelta(days=2)) != {}

    def test_maintenance_failed_job_backs_off(self):
        from datetime import datetime, timedelta

        from app.maintenance.scheduler import MaintenanceScheduler

        def boom():
            raise RuntimeError("x")

        scheduler = MaintenanceScheduler([
            {"name": "boom", "fn": boom, "interval": timedelta(hours=10)}])
        now = datetime(2026, 9, 25, 17, 0)
        result = scheduler.run_due(now)
        assert result["boom"]["status"] == "failed"
        # half-interval backoff: still due at +6h, not before
        assert scheduler.due(now + timedelta(hours=4)) == []
        assert scheduler.due(now + timedelta(hours=6)) == ["boom"]


class TestCli:
    def _run(self, *args, tmp_path=None, env_extra=None):
        import os
        import subprocess
        import sys

        env = dict(os.environ)
        env_file = tmp_path / "cli.env"
        env_file.write_text(
            "MODE=PAPER\nLIVE_TRADING=false\nBROKER=LOCAL_PAPER\n",
            encoding="utf-8",
        )
        env.update({
            "ARENA_HOME": str(tmp_path / "arena-home"),
            "ARENA_ENV_FILE": str(env_file),
            "DATABASE_URL": f"sqlite:///{tmp_path}/cli.db",
            "DATA_DIR": str(tmp_path / "data"),
            "DATA_SOURCE": "generic",
            "UNIVERSE_SYNC_URL": "https://invalid.invalid/x.json.gz",
            "MODE": "PAPER",
            "LIVE_TRADING": "false",
            "BROKER": "LOCAL_PAPER",
        })
        # These subprocesses are local CLI tests, not account diagnostics.
        # Never inherit an operator/CI token or load one back from a .env file.
        for key in (
            "MARKET_DATA_ACCESS_TOKEN", "UPSTOX_ACCESS_TOKEN", "MARKET_DATA_TOKEN",
            "UPSTOX_SANDBOX_ACCESS_TOKEN", "UPSTOX_LIVE_ACCESS_TOKEN",
        ):
            env[key] = ""
        env.update(env_extra or {})
        return subprocess.run([sys.executable, "-m", "app", *args],
                              capture_output=True, text=True, env=env, timeout=300,
                              cwd=str(PROJECT_ROOT))

    def test_initialize_offline(self, tmp_path):
        result = self._run("initialize", tmp_path=tmp_path)
        assert result.returncode == 0
        assert '"ok": true' in result.stdout

    def test_market_universe_offline_honest(self, tmp_path):
        result = self._run("market-universe", "sync", tmp_path=tmp_path)
        assert "unavailable" in result.stdout

    def test_history_blocked_without_token(self, tmp_path):
        result = self._run("history", "sync", "--instrument", "NSE_EQ|INE002A01018",
                           tmp_path=tmp_path)
        assert result.returncode == 1
        assert "blocked" in result.stdout

    def test_report_markdown(self, tmp_path):
        result = self._run("report", tmp_path=tmp_path)
        assert result.returncode == 0
        assert "Arena Weekly Report" in result.stdout

    def test_research_status(self, tmp_path):
        result = self._run("research", "status", tmp_path=tmp_path)
        assert result.returncode == 0
        assert "experiments" in result.stdout

    def test_dataset_audit_requires_known_id(self, tmp_path):
        result = self._run("dataset", "audit", "--dataset-id", "nope", tmp_path=tmp_path)
        assert result.returncode == 1
