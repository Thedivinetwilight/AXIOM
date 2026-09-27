"""FINAL: v2 FINAL API endpoints (command center, portfolio, attribution,
research lab, learning, market intelligence, SVG charts, reports) + desktop
viewmodels contract."""

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings
from app.database.database import Database
from app.database.repository import Repository

TOKEN = {"Authorization": "Bearer final-test-token-xyz"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/finalapi.db")
    monkeypatch.setenv("API_TOKEN", "final-test-token-xyz")
    monkeypatch.setenv("DATA_SOURCE", "generic")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings = Settings.from_env()
    app = create_app(settings)
    database = Database(settings.database_url)
    app.state.repository = Repository(database)
    with TestClient(app) as test_client:
        yield test_client
    database.close()


def seed(repository):
    repository.record_system_event(
        "TRADE_OUTCOME", {"strategy": "momentum", "params_version": "pv1",
                          "symbol": "RELIANCE", "regime": "trend_up_normal",
                          "duration_bars": 4, "pnl": 12.5, "fees": 0.5},
        ts="2026-01-02T09:30:00+00:00")
    repository.record_system_event(
        "TRADE_OUTCOME", {"strategy": "rsi", "params_version": "rsi-v14",
                          "symbol": "TCS", "regime": "range_low_vol",
                          "duration_bars": 30, "pnl": -3.0, "fees": 0.5},
        ts="2026-01-02T09:35:00+00:00")
    repository.record_regime_observation("2026-01-02T09:15:00+00:00",
                                         "RELIANCE", "trend_up_normal",
                                         {"trend": 1.0, "volatility": 0.2,
                                          "momentum": 0.5, "volume_trend": 1.0})
    repository.upsert_coverage("NSE_EQ|X", "days", 1, None, None, 0, [], 0.0)


class TestFinalApi:
    def test_command_center_endpoint_answers_from_rows(self, client):
        seed(client.app.state.repository)
        data = client.get("/api/v2/command_center",
                          params={"q": "What contributed to today's P&L?"},
                          headers=TOKEN).json()
        assert "momentum" in data["answer"]
        assert data["labels"]

    def test_attribution_endpoint(self, client):
        seed(client.app.state.repository)
        data = client.get("/api/v2/attribution", headers=TOKEN).json()
        assert data["total_trades"] == 2
        assert data["total_net_pnl"] == pytest.approx(9.5)
        assert "CALCULATED" in data["labels"]["net_pnl"]

    def test_portfolio_endpoint_uses_recorded_state(self, client):
        data = client.get("/api/v2/portfolio", headers=TOKEN).json()
        assert "snapshot" in data
        assert data["hard_cap"] == client.app.state.settings.max_strategy_allocation
        assert "note" in data["snapshot"]  # honest: no broker attached

    def test_research_lab_and_learning_detail(self, client):
        seed(client.app.state.repository)
        lab = client.get("/api/v2/research_lab", headers=TOKEN).json()
        assert {"experiments", "candidates", "questions",
                "lineage"} <= set(lab)
        learning = client.get("/api/v2/learning_detail", headers=TOKEN).json()
        assert learning["regime_history_count"] == 1
        assert "calibration" in learning

    def test_market_intelligence_carries_disclaimer(self, client):
        seed(client.app.state.repository)
        data = client.get("/api/v2/market_intelligence", headers=TOKEN).json()
        assert "not a guarantee" in data["regime_similarity_disclaimer"]
        assert data["regimes"] and data["coverage"]

    def test_svg_charts(self, client):
        seed(client.app.state.repository)
        repository = client.app.state.repository
        repository.upsert_daily_snapshot("2026-01-02", equity=100000.0,
                                         realized=9.5, unrealized=0.0, trades=2,
                                         regime=None)
        for path, marker in (("/api/v2/charts/equity.svg", "polyline"),
                             ("/api/v2/charts/drawdown.svg", "polyline"),
                             ("/api/v2/charts/attribution.svg", "rect")):
            response = client.get(path, headers=TOKEN)
            assert response.status_code == 200, path
            assert response.headers["content-type"].startswith("image/svg+xml")
            assert marker in response.text, path

    def test_report_endpoint_daily_markdown(self, client):
        seed(client.app.state.repository)
        response = client.get("/api/v2/report",
                              params={"period": "daily", "format": "markdown"},
                              headers=TOKEN)
        assert response.status_code == 200
        assert "Attribution" in response.text

    def test_report_pdf_format(self, client):
        response = client.get("/api/v2/report",
                              params={"period": "monthly", "format": "pdf"},
                              headers=TOKEN)
        assert response.content[:5] == b"%PDF-"

    def test_final_endpoints_require_auth(self, client):
        for path in ("/api/v2/command_center", "/api/v2/portfolio",
                     "/api/v2/attribution", "/api/v2/research_lab",
                     "/api/v2/learning_detail", "/api/v2/market_intelligence",
                     "/api/v2/charts/equity.svg", "/api/v2/report"):
            assert client.get(path).status_code == 401, path


class TestDesktopViewModels:
    def test_rows_and_values_contracts(self):
        from desktop import viewmodels as vm

        portfolio = vm.portfolio_rows({
            "snapshot": {"equity": 100000.0, "gross_exposure": 20000.0,
                         "exposure_ratio": 0.2, "max_concentration": 0.2,
                         "positions": 1, "realized_pnl": -362.42,
                         "unrealized_pnl": 1000.0},
            "hard_cap": 0.35, "allocation": {"allocations": {"a": 35000.0}}})
        assert ("allocated: a", "35,000.00") in portfolio
        values = vm.attribution_values(
            {"dimensions": {"strategy": {"momentum": {"net_pnl": 9.5},
                                         "rsi": {"net_pnl": -3.0}}}})
        assert values == [("momentum", 9.5), ("rsi", -3.0)]
        lab = vm.research_lab_rows({
            "experiments": [{"name": "e", "status": "complete",
                             "result": "{\"candidates\": 3}"}],
            "questions": [{"question": "q?", "status": "open"}],
            "lineage": [{"strategy": "momentum", "generation": 1, "id": "abc"}]})
        assert any("experiment" in row[0] for row in lab)
        intelligence = vm.market_intelligence_rows({
            "universe": {"total": 5}, "coverage": [],
            "regimes": [{"ts": "2026-01-02T09:15:00+00:00",
                         "regime": "trend_up_normal", "features": "{}"}],
            "regime_similarity_disclaimer": "similarity ... not a guarantee"})
        assert any("Regime similarity" == row[0] for row in intelligence)
        answer = vm.command_center_view({"answer": "NET 9.50", "labels": ["CALCULATED"]})
        assert answer[0][1] == "NET 9.50"


class TestDesktopContract:
    def test_app_defines_exactly_13_pages(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "desktop" / "app.py").read_text()
        start = source.index("PAGES = [")
        end = source.index("]", start)
        pages = source[start:end].count(",") + 1
        assert pages == 13
        for required in ("Command Center", "Research Lab", "Reports",
                         "Market Intelligence", "Learning", "Settings"):
            assert required in source[start:end]

    def test_client_has_final_read_methods(self):
        from desktop.api_client import ArenaApiClient

        for method in ("command_center", "portfolio", "attribution",
                       "research_lab", "market_intelligence", "learning_detail",
                       "report"):
            assert callable(getattr(ArenaApiClient, method)), method


class TestApiLatency:
    def test_latency_recorded_and_exposed(self, client):
        response = client.get("/api/v2/overview", headers=TOKEN)
        assert "x-api-latency-ms" in response.headers
        data = client.get("/api/v2/overview", headers=TOKEN).json()
        assert data["api_requests_observed"] >= 1
        assert data["api_latency_avg_ms"] is not None
        assert data["api_latency_avg_ms"] >= 0
