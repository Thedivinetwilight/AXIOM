"""Spec §98 (model agreement/disagreement) and §99 (counterfactual
simulation) — unit, persistence and HTTP coverage.

These are adversarial by design: single-source "agreement", NaN stances,
fabricating bars beyond recorded history, and restart persistence all must
fail visibly, never silently.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.api.server import create_app
from app.intel.agreement import (AgreementLedger, load_votes, save_votes)
from app.intel.counterfactual import CounterfactualSimulator


# =====================================================================
# §98 — AgreementLedger
# =====================================================================

class TestAgreementLedger:
    def test_agreement_surfaces_opposing_camps(self):
        ledger = AgreementLedger()
        ledger.record("india", "NSE|RELIANCE",
                      {"STRATEGY": 1, "ANALOGUE": 1, "KRONOS": -1},
                      horizon=5, regime="TREND_UP",
                      provenance="unit")
        summary = ledger.summary("india", "NSE|RELIANCE")
        assert summary["verdict"] == "DISAGREEMENT"
        assert summary["opposed_camps"] == {"bullish": ["ANALOGUE", "STRATEGY"],
                                            "bearish": ["KRONOS"]}
        assert "never treated as failure" in summary["note"]

    def test_directional_agreement(self):
        ledger = AgreementLedger()
        ledger.record("us", "NYSE|AAPL", {"STRATEGY": 1, "ANALOGUE": 1,
                                          "STATISTICAL_BASELINE": 0})
        summary = ledger.summary("us", "NYSE|AAPL")
        assert summary["verdict"] == "AGREEMENT"
        assert summary["opposed_camps"]["bearish"] == []

    def test_single_source_is_not_a_comparison(self):
        ledger = AgreementLedger()
        with pytest.raises(ValueError, match="at least two"):
            ledger.record("india", "NSE|X", {"STRATEGY": 1})

    def test_unknown_source_rejected(self):
        ledger = AgreementLedger()
        with pytest.raises(ValueError, match="unknown source"):
            ledger.record("india", "NSE|X", {"STRATEGY": 1, "GUT_FEEL": 1})

    @pytest.mark.parametrize("bad", [2, -3, 0.5, None, "up", True])
    def test_invalid_stance_rejected(self, bad):
        ledger = AgreementLedger()
        with pytest.raises(ValueError, match="stance"):
            ledger.record("india", "NSE|X", {"STRATEGY": bad, "ANALOGUE": 1})

    def test_no_edge_consensus_is_honest(self):
        ledger = AgreementLedger()
        ledger.record("india", "NSE|X", {"STRATEGY": 0, "ANALOGUE": 0,
                                         "KRONOS": 0})
        summary = ledger.summary("india", "NSE|X")
        assert summary["verdict"] == "NO_EDGE_CONSENSUS"

    def test_empty_ledger_reports_insufficient(self):
        summary = AgreementLedger().summary("india", "NSE|X")
        assert summary["verdict"] == "INSUFFICIENT"
        assert summary["votes"] == 0

    def test_watchlists_only_disagreements(self):
        ledger = AgreementLedger()
        ledger.record("india", "NSE|A", {"STRATEGY": 1, "ANALOGUE": 1})
        ledger.record("india", "NSE|B", {"STRATEGY": 1, "KRONOS": -1})
        watch = ledger.disagreement_watchlist()
        assert [w["identity"] for w in watch] == ["NSE|B"]

    def test_votes_survive_restart_roundtrip(self, tmp_path):
        path = tmp_path / "agreement_votes.json"
        first = AgreementLedger()
        first.record("india", "NSE|RELIANCE", {"STRATEGY": 1, "KRONOS": -1},
                     horizon=10, regime="CHOP")
        save_votes(path, first)
        second = AgreementLedger()
        assert load_votes(path, second) == 1
        assert second.summary("india", "NSE|RELIANCE")["verdict"] == "DISAGREEMENT"

    def test_corrupt_vote_file_starts_empty_not_fabricated(self, tmp_path):
        path = tmp_path / "agreement_votes.json"
        path.write_text("{not json", encoding="utf-8")
        ledger = AgreementLedger()
        assert load_votes(path, ledger) == 0
        assert ledger.summary("india", "NSE|X")["verdict"] == "INSUFFICIENT"


# =====================================================================
# §99 — CounterfactualSimulator
# =====================================================================

CLOSES = [100.0, 102.0, 101.0, 105.0, 104.0, 108.0, 107.0, 110.0, 109.0, 112.0]


class TestCounterfactualSimulator:
    def test_baseline_return_is_exact(self):
        result = CounterfactualSimulator(CLOSES).simulate_entry(
            "baseline", 0, horizon=2)
        assert result.return_pct == pytest.approx(1.0)          # 100 -> 101
        assert result.label == "SIMULATED"

    def test_size_multiplier_scales_return_and_drawdown(self):
        sim = CounterfactualSimulator(CLOSES)
        full = sim.simulate_entry("full", 2, horizon=3, size=1.0)
        double = sim.simulate_entry("double", 2, horizon=3, size=2.0)
        assert double.return_pct == pytest.approx(full.return_pct * 2)
        assert double.max_drawdown_pct == pytest.approx(
            full.max_drawdown_pct * 2)

    def test_no_trade_is_zero_and_labelled(self):
        nothing = CounterfactualSimulator(CLOSES).no_trade()
        assert nothing.return_pct == 0.0 and nothing.label == "SIMULATED"

    def test_compare_battery_is_labelled_simulation(self):
        battery = CounterfactualSimulator(CLOSES).compare(4, horizon=2)
        assert battery["label"] == "SIMULATED"
        names = {v["name"] for v in battery["variants"]}
        assert "baseline" in names and "no-trade" in names
        assert every_variant_labelled(battery)

    def test_window_beyond_recorded_history_is_refused(self):
        with pytest.raises(ValueError, match="fabricating"):
            CounterfactualSimulator(CLOSES).simulate_entry("late", 9, horizon=5)

    @pytest.mark.parametrize("bad", [[], [100.0], [100.0, float("nan")],
                                     [100.0, -1.0], [100.0, 0.0]])
    def test_invalid_series_rejected(self, bad):
        with pytest.raises(ValueError):
            CounterfactualSimulator(bad)

    def test_entry_outside_range_rejected(self):
        with pytest.raises(ValueError, match="outside recorded"):
            CounterfactualSimulator(CLOSES).simulate_entry("x", -1)


def every_variant_labelled(battery: dict) -> bool:
    return all(v["label"] == "SIMULATED" for v in battery["variants"])


# =====================================================================
# HTTP integration
# =====================================================================

@pytest.fixture()
def client(tmp_path, monkeypatch):
    for key in ("API_TOKEN", "DATABASE_URL", "DATA_DIR", "ARENA_HOME",
                "ARENA_ENV_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("API_TOKEN", "workstation-token")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'arena.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    with TestClient(create_app(Settings.from_env())) as test_client:
        test_client.headers.update({"Authorization": "Bearer workstation-token"})
        yield test_client


class TestAgreementApi:
    def test_vote_summary_and_watchlist(self, client):
        voted = client.post("/api/research/agreement", json={
            "market_id": "india", "identity": "NSE|RELIANCE",
            "stances": {"STRATEGY": 1, "KRONOS": -1}, "horizon": 5,
            "provenance": "unit-http"}).json()
        assert voted["ok"] and voted["summary"]["verdict"] == "DISAGREEMENT"
        summary = client.get("/api/research/agreement",
                             params={"market_id": "india",
                                     "identity": "NSE|RELIANCE"}).json()
        assert summary["verdict"] == "DISAGREEMENT"
        watch = client.get("/api/research/agreement/watchlist").json()
        assert watch["count"] == 1

    def test_single_source_vote_refused_over_http(self, client):
        response = client.post("/api/research/agreement", json={
            "market_id": "india", "identity": "NSE|X",
            "stances": {"STRATEGY": 1}})
        assert response.status_code == 422

    def test_opportunity_observation_derives_disagreement(self, client):
        client.post("/api/research/agreement", json={
            "market_id": "india", "identity": "NSE|TATA",
            "stances": {"STRATEGY": 1, "ANALOGUE": -1}})
        observed = client.post("/api/opportunities/observe", json={
            "market_id": "india", "identity": "NSE|TATA",
            "price_change_pct": 4.0}).json()
        assert observed["detected"]
        assert "model disagreement" in observed["opportunity"]["trigger"]
        assert observed["opportunity"]["evidence"]["model_disagreement"] is True

    def test_votes_persist_across_app_restart(self, tmp_path, monkeypatch):
        # Sequential lifecycles — the instance guard rightly forbids two
        # apps on one database, so "restart" means: stop, then start again.
        for key in ("API_TOKEN", "DATABASE_URL", "DATA_DIR", "ARENA_HOME",
                    "ARENA_ENV_FILE"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("API_TOKEN", "workstation-token")
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'arena.db'}")
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        with TestClient(create_app(Settings.from_env())) as first:
            first.headers.update({"Authorization": "Bearer workstation-token"})
            first.post("/api/research/agreement", json={
                "market_id": "india", "identity": "NSE|RELIANCE",
                "stances": {"STRATEGY": 1, "KRONOS": -1}})
        with TestClient(create_app(Settings.from_env())) as restarted:
            restarted.headers.update(
                {"Authorization": "Bearer workstation-token"})
            summary = restarted.get("/api/research/agreement", params={
                "market_id": "india", "identity": "NSE|RELIANCE"}).json()
            assert summary["verdict"] == "DISAGREEMENT"


class TestCounterfactualApi:
    def test_no_recorded_bars_is_honest(self, client):
        result = client.post("/api/research/counterfactual", json={
            "market_id": "india", "identity": "NSE|UNRECORDED",
            "entry_index": 0}).json()
        assert result["label"] == "NO_DATA"
        assert result["variants"] == []

    @staticmethod
    def _seed(client, symbol: str, count: int, base: float = 100.0):
        from datetime import datetime, timedelta, timezone

        from app.history.store import CandleStore

        store = CandleStore(client.app.state.workstation.data_dir / "candles")
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        candles = []
        for i in range(count):
            close = base + i
            candles.append({"ts": (start + timedelta(days=i)).isoformat(),
                            "open": close - 0.5, "high": close + 2.0,
                            "low": close - 1.0, "close": close, "volume": 10})
        store.upsert_candles(symbol, "days", 1, candles)

    def test_window_past_history_refused_over_http(self, client):
        self._seed(client, "SHORT", 4)
        response = client.post("/api/research/counterfactual", json={
            "market_id": "india", "identity": "NSE|SHORT",
            "entry_index": 3, "horizon": 5})
        assert response.status_code == 422

    def test_simulated_battery_labelled(self, client):
        self._seed(client, "LONG", 20)
        result = client.post("/api/research/counterfactual", json={
            "market_id": "india", "identity": "NSE|LONG",
            "entry_index": 5, "horizon": 3}).json()
        assert result["label"] == "SIMULATED"
        assert result["variants"] and all(
            v["label"] == "SIMULATED" for v in result["variants"])
        baseline = next(v for v in result["variants"] if v["name"] == "baseline")
        assert baseline["return_pct"] == pytest.approx(
            (108.0 / 105.0 - 1.0) * 100.0)
