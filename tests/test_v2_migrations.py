"""V2.1: additive migrations — V1 data survives schema v3."""

import sqlite3

from app.database.database import Database
from app.database.repository import Repository


class TestMigrations:
    def test_all_schema_versions_recorded(self, tmp_path):
        database = Database(f"sqlite:///{tmp_path}/m.db")
        with database.transaction() as db:
            versions = [row[0] for row in db.execute(
                "SELECT version FROM schema_version ORDER BY version").fetchall()]
        database.close()
        assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12], \
            "migrations only ever add versions"

    def test_final_tables_additive(self, tmp_path):
        database = Database(f"sqlite:///{tmp_path}/m.db")
        with database.transaction() as db:
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        database.close()
        assert {"strategy_lineage", "strategy_lifecycle", "regime_history",
                "opportunity_scans", "research_questions", "system_status",
                "reality_gap", "confidence_calibration", "submission_intents",
                "daily_risk_fills"} <= tables

    def test_v1_tables_and_v2_tables_coexist(self, tmp_path):
        database = Database(f"sqlite:///{tmp_path}/m.db")
        repository = Repository(database)
        # V1 writes still work
        repository.record_signal({"id": "s1", "ts": "t", "strategy": "x",
                                  "symbol": "S", "timeframe": "1m",
                                  "signal": "BUY", "price": 1, "meta": {}})
        repository.record_sandbox_order({
            "id": "sbox-1", "intent_id": "i1", "symbol": "S", "side": "BUY",
            "quantity": 1, "order_type": "MARKET", "status": "SUBMITTED",
            "tag": "arena-v1", "created_ts": "t"})
        # V2 writes work
        repository.upsert_instruments([{"instrument_key": "K", "row_hash": "h"}], "t")
        repository.record_learning_event("X", {})
        with database.transaction() as db:
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        database.close()
        assert {"signals", "sandbox_orders", "sandbox_transitions", "model_versions",
                "system_events", "engine_state"} <= tables, "V1 tables intact"
        assert {"instruments", "universe_syncs", "download_jobs", "coverage",
                "quality_events", "dataset_versions", "experiments", "candidates",
                "shadow_trades", "learning_events", "daily_snapshots"} <= tables

    def test_reopen_preserves_rows(self, tmp_path):
        path = f"sqlite:///{tmp_path}/m.db"
        database = Database(path)
        Repository(database).upsert_instruments(
            [{"instrument_key": "K1", "row_hash": "h1"}], "t")
        database.close()
        database2 = Database(path)
        repository = Repository(database2)
        assert [row["instrument_key"] for row in
                repository.list_instruments(state=None, limit=10)] == ["K1"]
        database2.close()

    def test_v1_paper_test_pins_untouched(self):
        """The V0.6.4 pinned economics file must be unmodified in spirit."""
        from pathlib import Path

        source = Path("app/__main__.py").read_text()
        assert "EXPECTED_REALIZED_PNL" in source and "EXPECTED_EQUITY" in source
