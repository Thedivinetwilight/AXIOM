"""V2.1: MarketUniverseManager — sync diffing, honest offline, audit integrity."""

import json

import pytest

from app.database.database import Database
from app.database.repository import Repository
from app.universe.manager import MarketUniverseManager, normalize_record


@pytest.fixture()
def repo(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/universe.db")
    repository = Repository(database)
    yield repository
    database.close()


def fake_file(rows):
    def hook(url):
        return json.dumps(rows).encode()
    return hook


class TestSync:
    def test_add_change_remove_lifecycle(self, repo):
        manager = MarketUniverseManager(repo, transport_hook=fake_file([
            {"instrument_key": "NSE_EQ|A", "trading_symbol": "A", "exchange": "NSE",
             "lot_size": 1, "segment": "NSE_EQ"},
            {"instrument_key": "NSE_EQ|B", "trading_symbol": "B", "exchange": "NSE",
             "lot_size": 1, "segment": "NSE_EQ"},
        ]))
        result = manager.sync()
        assert result["status"] == "complete" and result["added"] == 2
        # change one row, drop the other, add a new one
        manager2 = MarketUniverseManager(repo, transport_hook=fake_file([
            {"instrument_key": "NSE_EQ|A", "trading_symbol": "A", "exchange": "NSE",
             "lot_size": 5, "segment": "NSE_EQ"},
            {"instrument_key": "NSE_EQ|C", "trading_symbol": "C", "exchange": "NSE",
             "lot_size": 1, "segment": "NSE_EQ"},
        ]))
        result2 = manager2.sync()
        assert result2["added"] == 1 and result2["changed"] == 1
        assert result2["removed"] == 1
        states = {row["instrument_key"]: row["state"]
                  for row in repo.list_instruments(state=None, limit=10)}
        assert states == {"NSE_EQ|A": "active", "NSE_EQ|C": "active",
                          "NSE_EQ|B": "removed"}

    def test_unchanged_resync_touches_nothing(self, repo):
        rows = [{"instrument_key": "NSE_EQ|A", "trading_symbol": "A",
                 "exchange": "NSE", "lot_size": 1}]
        MarketUniverseManager(repo, transport_hook=fake_file(rows)).sync()
        result = MarketUniverseManager(repo, transport_hook=fake_file(rows)).sync()
        assert result == {**result, "added": 0, "changed": 0, "unchanged": 1}

    def test_offline_is_honest_and_stateless(self, repo):
        def broken(url):
            raise ConnectionError("no network")

        result = MarketUniverseManager(repo, transport_hook=broken).sync()
        assert result["status"] == "unavailable" and result["performed"] is False
        assert repo.list_instruments(state=None, limit=10) == []
        syncs = repo.list_universe_syncs()
        assert syncs[0]["status"] == "unavailable"

    def test_live_public_file_schema(self, repo):
        """VERIFIED against the REAL public instrument file (network available)."""
        import urllib.request

        try:
            with urllib.request.urlopen(
                "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
                timeout=120,
            ) as response:
                import gzip

                head = gzip.decompress(response.read()).decode()[:2000]
        except Exception as exc:  # offline → honest skip
            pytest.skip(f"public instrument file unreachable: {exc}")
        decoder = json.JSONDecoder()
        first = decoder.raw_decode(head[head.index("{"):])[0]
        normalized = normalize_record(first)
        assert normalized["instrument_key"].startswith(("NSE_", "BSE_", "MCX_"))
        assert normalized["row_hash"]


class TestAudit:
    def test_audit_detects_tampering(self, repo):
        rows = [{"instrument_key": "NSE_EQ|A", "trading_symbol": "A",
                 "exchange": "NSE", "lot_size": 2}]
        MarketUniverseManager(repo, transport_hook=fake_file(rows)).sync()
        audit = MarketUniverseManager(repo).audit()
        assert audit["ok"]
        with repo.db.transaction() as db:
            db.execute("UPDATE instruments SET lot_size=99 WHERE instrument_key='NSE_EQ|A'")
        audit2 = MarketUniverseManager(repo).audit()
        assert not audit2["ok"]
        assert any(p["issue"] == "hash_mismatch" for p in audit2["problems"])
