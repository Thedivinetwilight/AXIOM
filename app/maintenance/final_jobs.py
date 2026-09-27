"""FINAL maintenance jobs: research survey, anomalies, system snapshots,
reality-gap review, and scheduled database backups.

These jobs are APPENDED to the V2.1 maintenance set. They are research and
monitoring only — none of them can change risk limits, position sizing,
STOP state, auth, or execution mode (the V2.1 learning boundary holds).
"""

from __future__ import annotations

from datetime import timedelta


def _reality_gap_review(repository):
    """Compare recorded stage metrics for every lineage strategy.

    Only compares when BOTH stages have recorded rows; missing evidence is
    reported honestly, never fabricated.
    """
    from app.monitoring.reality_gap import RealityGapMonitor

    monitor = RealityGapMonitor(repository)
    compared = flagged = 0
    seen = set()
    for lineage in repository.list_lineage():
        name = lineage["strategy"]
        if name in seen:
            continue
        seen.add(name)
        result = monitor.compare(name, "backtest", "paper")
        if result.get("comparable"):
            compared += 1
            flagged += 1 if result.get("flagged") else 0
    return {"compared": compared, "flagged": flagged,
            "strategies": len(seen)}


def _research_survey(repository):
    from app.research.director import ResearchDirector

    created = ResearchDirector(repository).survey()
    return {"questions_created": len(created)}


def _anomaly_scan(repository):
    from app.engine.events import PauseGate
    from app.monitoring.anomalies import AnomalyDetector

    detector = AnomalyDetector(repository, pause_gate=PauseGate(repository))
    anomalies = detector.detect()
    return {"anomalies": len(anomalies),
            "kinds": sorted({a["kind"] for a in anomalies})}


def _system_snapshot(repository, database_path):
    from app.monitoring.system import SystemMonitor

    snapshot = SystemMonitor(repository, database_path=database_path).snapshot()
    return {"alerts": snapshot.get("alerts", []),
            "database_ok": snapshot.get("database_ok")}


class _PathAsDatabase:
    """Adapts a plain sqlite path to the `database` object RecoveryManager expects."""

    def __init__(self, path):
        self.path = str(path).replace("sqlite:///", "")


def _database_backup(repository, database_path, backup_dir):
    from app.database.recovery import RecoveryManager

    manager = RecoveryManager(_PathAsDatabase(database_path), repository,
                              backup_dir=str(backup_dir))
    report = manager.backup(label="scheduled")
    return {"verified": report.get("verified"),
            "backup": report.get("backup")}


def build_maintenance_jobs(repository, settings, database_path, backup_dir="backups"):
    """FINAL jobs for MaintenanceScheduler (see app.maintenance.scheduler)."""
    return [
        {"name": "anomaly_scan", "fn": lambda: _anomaly_scan(repository),
         "interval": timedelta(minutes=15)},
        {"name": "system_snapshot",
         "fn": lambda: _system_snapshot(repository, database_path),
         "interval": timedelta(minutes=15)},
        {"name": "research_survey", "fn": lambda: _research_survey(repository),
         "interval": timedelta(hours=6)},
        {"name": "reality_gap_review",
         "fn": lambda: _reality_gap_review(repository),
         "interval": timedelta(hours=6)},
        {"name": "database_backup",
         "fn": lambda: _database_backup(repository, database_path, backup_dir),
         "interval": timedelta(days=1)},
    ]
