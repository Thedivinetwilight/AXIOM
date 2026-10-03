"""FINAL: reality gap, anomaly detector + pause, system monitor, recovery.

Written against the ACTUAL signatures:
- RealityGapMonitor.record_stage(strategy, stage, metrics) / compare(strategy,
  earlier_stage, later_stage) — stage rows feed the comparison.
- AnomalyDetector(repository, pause_gate=…).detect() over recent events.
- SystemMonitor(repository, database_path=…).snapshot().
- RecoveryManager(database, repository, backup_dir=…).backup/verify/restore
  (restore REFUSES unverified backups — returns {"restored": False}).
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.data.models import Candle
from app.database.database import Database
from app.database.repository import Repository

NOW = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
BASE = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


@pytest.fixture()
def env(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/mon.db")
    repository = Repository(database)
    yield repository
    database.close()


def record_learning(repository, ts, kind, payload):
    repository.record_learning_event(kind, payload, ts=iso(ts))


class TestRealityGap:
    def test_healthy_progression_not_flagged(self, env):
        from app.monitoring.reality_gap import RealityGapMonitor

        monitor = RealityGapMonitor(env)
        monitor.record_stage("momentum", "backtest",
                             {"expectancy": 5.0, "max_drawdown": 0.05,
                              "trades": 20})
        monitor.record_stage("momentum", "walk_forward",
                             {"expectancy": 4.6, "max_drawdown": 0.06,
                              "trades": 18})
        monitor.record_stage("momentum", "paper",
                             {"expectancy": 4.0, "max_drawdown": 0.07,
                              "trades": 15})
        result = monitor.compare("momentum", "backtest", "paper")
        assert result["comparable"] is True and result["flags"] == {}
        assert result["flagged"] is False
        rows = env.list_reality_gaps(limit=10)
        assert any(row["flagged"] in (0, False) for row in rows)

    def test_degradation_flags_all_rules_and_raises_event(self, env):
        from app.monitoring.reality_gap import RealityGapMonitor

        monitor = RealityGapMonitor(env)
        monitor.record_stage("momentum", "backtest",
                             {"expectancy": 5.0, "max_drawdown": 0.05,
                              "trades": 20})
        monitor.record_stage("momentum", "paper",
                             {"expectancy": 1.0, "max_drawdown": 0.20,
                              "trades": 4})
        result = monitor.compare("momentum", "backtest", "paper")
        assert set(result["flags"]) == {"expectancy_drop", "drawdown_blowout",
                                        "frequency_collapse"}
        kinds = [e["kind"] for e in env.list_learning_events(limit=20)]
        assert "REALITY_GAP_FLAG" in kinds, "flag raises a learning event"
        flagged_rows = env.list_reality_gaps(flagged=True, limit=10)
        assert flagged_rows

    def test_live_is_a_recordable_name_never_produced_by_code(self, env):
        from app.monitoring.reality_gap import RealityGapMonitor, STAGES

        assert "live" in STAGES
        RealityGapMonitor(env).record_stage(
            "momentum", "live",
            {"expectancy": 1.0, "max_drawdown": 0.1, "trades": 5})
        rows = env.list_reality_gaps(limit=5)
        assert rows[0]["comparison"] == "live"  # recorded only if fed manually

    def test_missing_evidence_is_honest(self, env):
        from app.monitoring.reality_gap import RealityGapMonitor

        result = RealityGapMonitor(env).compare("ghost", "backtest", "paper")
        assert result["comparable"] is False


class TestAnomalies:
    def test_losing_streak_and_order_spike_with_auto_pause(self, env):
        from app.engine.events import PauseGate
        from app.monitoring.anomalies import AnomalyDetector

        for i in range(7):
            env.record_system_event("TRADE_OUTCOME",
                                    {"strategy": "momentum", "pnl": -10.0},
                                    ts=iso(NOW - timedelta(minutes=30 - i)))
        base = NOW - timedelta(minutes=35)
        for i in range(35):
            env.record_system_event("SANDBOX_ORDER_SUBMITTED",
                                    {"order_id": f"o{i}"},
                                    ts=iso(base + timedelta(seconds=i * 60)))
        detector = AnomalyDetector(env, pause_gate=PauseGate(env))
        anomalies = detector.detect()
        kinds = {a["kind"] for a in anomalies}
        assert "losing_streak" in kinds and "order_frequency_spike" in kinds
        assert PauseGate(env).is_paused("execution"), "auto-pause on spike"
        events = [e for e in env.list_learning_events(limit=50)
                  if e["kind"] == "ANOMALY_DETECTED"]
        assert events

    def test_feed_failures_detected(self, env):
        from app.monitoring.anomalies import AnomalyDetector

        for i in range(5):
            env.record_system_event("FEED_ERROR", {"attempt": i},
                                    ts=iso(NOW - timedelta(minutes=10 - i)))
        anomalies = AnomalyDetector(env).detect()
        assert any(a["kind"] == "feed_failures" for a in anomalies)

    def test_broker_mismatch_detected(self, env):
        from app.monitoring.anomalies import AnomalyDetector

        env.record_sandbox_order({"id": "stuck1", "symbol": "RELIANCE",
                                  "side": "BUY", "quantity": 10,
                                  "status": "SUBMITTED",
                                  "created_ts": iso(NOW - timedelta(hours=8))})
        anomalies = AnomalyDetector(env).detect()
        assert any(a["kind"] == "broker_mismatch" for a in anomalies)

    def test_missing_data_detected(self, env):
        from app.monitoring.anomalies import AnomalyDetector

        env.upsert_coverage("NSE_EQ|X", "days", 1, None, None, 0, [], 0.0)
        anomalies = AnomalyDetector(env).detect()
        assert any(a["kind"] == "missing_data" for a in anomalies)

    def test_healthy_system_no_anomalies(self, env):
        from app.monitoring.anomalies import AnomalyDetector

        env.record_system_event("TRADE_OUTCOME", {"strategy": "momentum",
                                                  "pnl": 25.0}, ts=iso(NOW))
        assert AnomalyDetector(env).detect() == []


class TestSystemMonitor:
    def test_snapshot_persists_status(self, tmp_path, env):
        from app.monitoring.system import SystemMonitor

        monitor = SystemMonitor(env, database_path=env.db.path)
        snapshot = monitor.snapshot()
        assert snapshot["database_ok"] is True
        assert snapshot["database_mb"] >= 0
        assert "memory_used_pct" in snapshot
        rows = env.list_system_status(limit=1)
        assert rows and "database_ok" in rows[0]["payload"]

    def test_disk_threshold_alert(self, tmp_path, env, monkeypatch):
        from collections import namedtuple

        from app.monitoring import system as system_mod
        from app.monitoring.system import SystemMonitor

        usage = namedtuple("usage", "used total free")(990, 1000, 10)
        monkeypatch.setattr(system_mod.shutil, "disk_usage", lambda path: usage)
        monitor = SystemMonitor(env, database_path=env.db.path)
        snapshot = monitor.snapshot()
        assert any("disk" in a for a in snapshot["alerts"])
        assert [e for e in env.list_system_events(limit=10)
                if e["kind"] == "SYSTEM_ALERT"]


class TestRecovery:
    def test_backup_is_verified_and_restorable(self, tmp_path, env):
        from app.database.recovery import RecoveryManager

        manager = RecoveryManager(env.db, env, backup_dir=str(tmp_path / "backups"))
        env.record_system_event("PRE_BACKUP", {"n": 1}, ts=iso(NOW))
        report = manager.backup(label="test")
        assert report["verified"] is True
        # destroy the live database, then restore the verified backup
        with open(env.db.path, "wb") as handle:
            handle.write(b"garbage" * 200)
        restored = manager.restore(report["backup"])
        assert restored["restored"] is True
        check = sqlite3.connect(env.db.path)
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        check.close()

    def test_restore_refuses_corrupt_backup(self, tmp_path, env):
        from app.database.recovery import RecoveryManager

        manager = RecoveryManager(env.db, env, backup_dir=str(tmp_path / "backups"))
        corrupt = tmp_path / "corrupt.db"
        corrupt.write_bytes(b"not a database at all")
        report = manager.verify(str(corrupt))
        assert report["ok"] is False
        result = manager.restore(corrupt)
        assert result["restored"] is False
        kinds = [e["kind"] for e in env.list_system_events(limit=20)]
        assert "RESTORE_REFUSED" in kinds

    def test_backup_event_recorded(self, tmp_path, env):
        from app.database.recovery import RecoveryManager

        manager = RecoveryManager(env.db, env, backup_dir=str(tmp_path / "backups"))
        report = manager.backup(label="x")
        assert report["backup"].endswith(".db")
        kinds = [e["kind"] for e in env.list_system_events(limit=20)]
        assert "BACKUP_CREATED" in kinds


class TestMaintenanceWiring:
    def test_final_jobs_run_and_persist(self, tmp_path, env):
        from datetime import datetime as dt

        from app.maintenance.final_jobs import build_maintenance_jobs
        from app.maintenance.scheduler import MaintenanceScheduler

        settings = type("S", (), {"data_dir": str(tmp_path)})()
        jobs = build_maintenance_jobs(env, settings, env.db.path,
                                      backup_dir=str(tmp_path / "backups"))
        names = {job["name"] for job in jobs}
        assert {"anomaly_scan", "system_snapshot", "research_survey",
                "reality_gap_review", "database_backup"} <= names
        scheduler = MaintenanceScheduler(jobs)
        ran = scheduler.run_due(now=dt.now(timezone.utc))
        assert set(ran) == names, "first run executes every job"
        assert ran["system_snapshot"]["result"]["database_ok"] is True
        assert ran["research_survey"]["result"]["questions_created"] >= 1
        assert ran["database_backup"]["result"]["verified"] is True
        backups = list((tmp_path / "backups").glob("arena-*.db"))
        assert backups

    def test_jobs_do_not_rerun_inside_interval(self, tmp_path, env):
        from datetime import datetime as dt, timedelta

        from app.maintenance.final_jobs import build_maintenance_jobs
        from app.maintenance.scheduler import MaintenanceScheduler

        settings = type("S", (), {"data_dir": str(tmp_path)})()
        scheduler = MaintenanceScheduler(build_maintenance_jobs(
            env, settings, env.db.path, backup_dir=str(tmp_path / "backups")))
        now = dt.now(timezone.utc)
        assert scheduler.run_due(now=now)
        assert scheduler.run_due(now=now + timedelta(minutes=5)) == {}

    def test_engine_calls_maintenance_on_candle(self, tmp_path, env):
        import httpx

        from app.brokers.sandbox import UpstoxSandboxBroker
        from app.config.settings import Settings
        from app.engine.sandbox_engine import SandboxEngine
        from app.learning.journal import LearningJournal
        from app.maintenance.final_jobs import build_maintenance_jobs
        from app.maintenance.scheduler import MaintenanceScheduler
        from app.risk.limits import HardRiskLimits

        repository = env

        class _P:
            symbols = ["RELIANCE"]

            def metrics(self):
                return {"market_open": True, "stale_feed": False}

            async def stream(self, symbols, timeframe):
                for i in range(3):
                    yield Candle(BASE + timedelta(minutes=i), 99.9, 100.1,
                                 99.8, 100.0, 1000, "RELIANCE")

        class Hold:
            name = "momentum"
            parameters = {"period": 2}
            history_limit = 30

            def generate_signal(self, candles):
                from app.strategies.base import Signal

                return Signal.HOLD

        def handler(request):
            return httpx.Response(400, json={"errors": [{"errorCode": "x"}]})

        broker = UpstoxSandboxBroker(
            "tok", "NSE_EQ|INE002A01018", transport=httpx.MockTransport(handler),
            repository=repository, initial_capital=100000.0)
        settings = type("S", (), {"data_dir": str(tmp_path)})()
        scheduler = MaintenanceScheduler(build_maintenance_jobs(
            repository, settings, repository.db.path,
            backup_dir=str(tmp_path / "backups")))
        engine = SandboxEngine(
            provider=_P(), strategy=Hold(), broker=broker, repository=repository,
            risk_limits=HardRiskLimits.from_settings(Settings()),
            journal=LearningJournal(repository), timeframe="1m",
            evaluate_every=10, maintenance_scheduler=scheduler)
        status = engine.run(max_candles=3)
        assert status["state"] in {"RUNNING", "STOPPED"}
        assert env.list_system_status(limit=1), "system snapshot job ran inline"


class TestBackupCliGuard:
    """`backup verify|restore` act on a named file. Without --path the CLI used
    to hand None to Path() and die inside pathlib with a TypeError traceback;
    it must instead refuse in plain language with a non-zero exit code."""

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        # keep the command away from the developer's real database
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/cli.db")
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    @pytest.mark.parametrize("action", ["verify", "restore"])
    def test_missing_path_is_refused_clearly(self, action, capsys):
        from app.__main__ import main

        with pytest.raises(SystemExit) as exit_info:
            main(["backup", action])
        assert exit_info.value.code == 1, "must fail, not silently succeed"
        out = capsys.readouterr().out
        assert "requires --path" in out
        assert action in out
        # the old failure mode: an unhandled TypeError escaping to the user
        assert "Traceback" not in out
        assert "TypeError" not in out

    def test_missing_path_does_not_create_a_backup(self, tmp_path, capsys):
        from app.__main__ import main

        backup_dir = tmp_path / "backups"
        with pytest.raises(SystemExit):
            main(["backup", "verify", "--backup-dir", str(backup_dir)])
        assert not list(backup_dir.glob("arena-*.db")), "no file should be written"

    def test_explicit_missing_file_reports_cleanly(self, tmp_path, capsys):
        from app.__main__ import main

        with pytest.raises(SystemExit) as exit_info:
            main(["backup", "verify", "--path", str(tmp_path / "nope.db")])
        assert exit_info.value.code == 1
        out = capsys.readouterr().out
        assert "missing" in out and "Traceback" not in out
