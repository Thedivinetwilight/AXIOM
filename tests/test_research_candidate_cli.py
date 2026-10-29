"""Regression coverage for the research candidate generation command path."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import __main__ as cli
from app.backtesting.costs import ExecutionConfig
from app.backtesting.engine import BacktestEngine
from app.config.settings import Settings
from app.database.database import Database
from app.database.repository import Repository
from app.history.store import CandleStore
from app.learning.evaluator import out_of_sample_expectancy
from app.research.discovery import (
    Spec, StrategyDiscoveryEngine, evaluate_spec, generate_specs,
)
from tests.conftest import candle
from tests.test_final_discovery import series


def _store_rows(count=150, drift=0.2):
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i, bar in enumerate(series(count, drift=drift, seed=7)):
        # Use daily timestamps for the CLI's days:1 store partition.
        ts = base + timedelta(days=i)
        rows.append({
            "ts": ts.isoformat(), "open": bar.open, "high": bar.high,
            "low": bar.low, "close": bar.close, "volume": bar.volume, "oi": 0,
        })
    return rows


def _settings(tmp_path, candidates=1):
    return Settings(
        data_dir=str(tmp_path),
        database_url=f"sqlite:///{tmp_path}/research.db",
        parquet_enabled=False,
        discovery_max_candidates=candidates,
    )


def _args(instruments="NSE_EQ|AAA", output=None, periods="2,3,5"):
    return SimpleNamespace(
        name="regression", instruments=instruments, unit="days", interval=1,
        from_date=None, to_date=None, timeframe="1D", output=output,
        max_candidates=1, action="run", periods=periods,
    )


def _seed_store(tmp_path, *keys):
    store = CandleStore(Path(tmp_path) / "history", parquet_enabled=False)
    for index, key in enumerate(keys):
        store.upsert_candles(key, "days", 1, _store_rows(drift=0.2 + index * 0.1))


def test_oos_evaluation_uses_only_prior_warmup_and_research_labels():
    calls = []

    class SpyEngine(BacktestEngine):
        def run(self, candles, strategy, timeframe="1D", *, warmup=None, label="BACKTEST"):
            calls.append({
                "label": label, "candles": tuple(candles),
                "warmup": tuple(warmup or ()),
            })
            return super().run(candles, strategy, timeframe, warmup=warmup, label=label)

    bars = [candle(i, 100 + (0.4 if i % 3 else -0.2)) for i in range(60)]
    result = out_of_sample_expectancy(
        bars, "momentum", {"period": 2, "threshold": 0.0},
        SpyEngine(ExecutionConfig()), "1D", label="validation",
    )
    assert result["valid"] is True
    assert [call["label"] for call in calls] == ["TRAINING", "VALIDATION"]
    assert calls[0]["candles"] == tuple(bars[:42])
    assert calls[0]["warmup"] == ()
    assert calls[1]["candles"] == tuple(bars[42:])
    assert calls[1]["warmup"] == tuple(bars[:42])


def test_discovery_engine_injection_and_research_report(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/discovery.db")
    repository = Repository(database)
    try:
        engine = StrategyDiscoveryEngine(
            repository,
            engine=BacktestEngine(ExecutionConfig()),
        )
        run = engine.discover(
            "injected", series(100), "1m",
            specs=[Spec("momentum", {"period": 2, "threshold": 0.0})],
        )
        assert run["evaluated"] == 1
        assert isinstance(run["qualified"], list)
        assert run["research_only"] is True
        assert run["execution_authorized"] is False
        assert run["profitability_claim"] == "none"
        assert len(repository.list_candidates(limit=10)) == 1
    finally:
        database.close()


def test_discover_cli_separates_instruments_and_writes_json(tmp_path, monkeypatch, capfd):
    _seed_store(tmp_path, "NSE_EQ|AAA", "NSE_EQ|BBB")
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "_load_settings", lambda: settings)
    output = tmp_path / "candidate-report.json"

    rc = cli.cmd_discover(_args("NSE_EQ|AAA,NSE_EQ|BBB", output=output))
    captured = capfd.readouterr()
    report = json.loads(captured.out)

    assert rc == 0
    assert report["ok"] is True
    assert report["candidates"] == 2
    assert len(report["runs"]) == 2
    assert {run["instrument"] for run in report["runs"]} == {"NSE_EQ|AAA", "NSE_EQ|BBB"}
    assert {run["symbol"] for run in report["runs"]} == {"AAA", "BBB"}
    assert all(len(run["results"]) == 1 for run in report["runs"])
    assert report["qualified"] == len(report["qualified_detail"])
    assert report["execution_authorized"] is False
    assert report["profitability_claim"] == "none"
    assert json.loads(output.read_text())["experiment_ids"] == report["experiment_ids"]

    database = Database(settings.database_url)
    try:
        assert len(Repository(database).list_candidates(limit=10)) == 2
    finally:
        database.close()


def test_research_cli_parses_periods_and_preserves_single_run_fields(tmp_path, monkeypatch, capfd):
    _seed_store(tmp_path, "NSE_EQ|AAA")
    monkeypatch.setattr(cli, "_load_settings", lambda: _settings(tmp_path))

    rc = cli.cmd_research(_args())
    report = json.loads(capfd.readouterr().out)

    assert rc == 0
    assert report["ok"] is True
    assert report["experiment_id"]
    assert report["candidates"] == 3
    assert len(report["results"]) == 3
    assert [row["parameters"]["period"] for row in report["results"]] == [2, 3, 5]
    assert all(isinstance(row["parameters"]["period"], int) for row in report["results"])
    assert report["research_only"] is True
    assert report["execution_authorized"] is False


def test_research_cli_rejects_invalid_periods(tmp_path, monkeypatch, capfd):
    monkeypatch.setattr(cli, "_load_settings", lambda: _settings(tmp_path))
    rc = cli.cmd_research(_args(periods="2,not-a-number"))
    report = json.loads(capfd.readouterr().out)
    assert rc == 2
    assert report["ok"] is False
    assert "comma-separated integers" in report["error"]


def test_director_schedule_seed_is_stable_across_calls(tmp_path):
    from app.research.director import ResearchDirector

    database = Database(f"sqlite:///{tmp_path}/director.db")
    repository = Repository(database)
    try:
        question = "Which regime has reproducible evidence?"
        question_id = repository.record_research_question(question, {})
        director = ResearchDirector(repository, experiment_engine=object())
        first = director.schedule(question_id, candles=[None])
        second = director.schedule(question_id, candles=[None])
        expected = int.from_bytes(hashlib.sha256(question.encode()).digest()[:4], "big") % (2 ** 31)
        assert first["seed"] == second["seed"] == expected
    finally:
        database.close()


def test_zero_profit_factor_is_penalized_not_treated_as_undefined():
    from app.tournament.scoring import ScoringConfig, score

    metrics = {
        "total_return": -0.1, "sharpe": -1.0, "sortino": -1.0,
        "profit_factor": 0.0, "max_drawdown": 0.2, "number_of_trades": 20,
        "instability": 0.1, "volatility": 0.2, "cost_ratio": 0.01,
    }
    weights = {key: 0.0 for key in ScoringConfig().weights}
    weights["profit_factor"] = 1.0
    config = ScoringConfig(weights=weights)
    all_losses = score(metrics, config)
    undefined = score({**metrics, "profit_factor": None}, config)
    assert all_losses["components"]["profit_factor"] < 0
    assert undefined["components"]["profit_factor"] == 0
    assert all_losses["score"] < undefined["score"]


def test_discovery_missing_walk_forward_evidence_fails_closed():
    result = evaluate_spec(
        series(60), "1m", Spec("momentum", {"period": 2, "threshold": 0.0})
    )
    assert result["wf_expectancy"] is None
    assert result["gates"]["wf_expectancy>0"] is False
    assert result["tradable"] is False


def test_robustness_score_rejects_large_drop_from_positive_baseline(monkeypatch):
    import app.research.discovery as discovery

    report = {
        "baseline": {"total_return": 0.5},
        "trials": [
            {"metrics": {"total_return": 0.39}},
            {"metrics": {"total_return": 0.41}},
        ],
    }
    monkeypatch.setattr("app.validation.robustness.robustness", lambda *a, **k: report)
    value, _ = discovery._robustness_score(
        None, [], "1m", Spec("momentum", {"period": 2, "threshold": 0.0})
    )
    assert value == 0.5


@pytest.mark.parametrize("limit", [0, -1, 1.5, True, "2"])
def test_generate_specs_rejects_invalid_limits(limit):
    with pytest.raises(ValueError, match="positive integer"):
        generate_specs(limit)


def test_router_hard_blocks_weak_robustness_even_with_positive_oos():
    from app.learning.router import StrategyRouter

    candidate = {
        "strategy": "momentum", "parameters": {"period": 2, "threshold": 0.0},
        "oos": {"oos_expectancy": 0.02, "oos_trades": 10},
        "metrics": {"max_drawdown": 0.1}, "robustness_score": 0.249,
    }
    router = StrategyRouter()
    assert router.score(candidate) is None
    assert router.score({**candidate, "robustness_score": 0.25}) is not None


@pytest.mark.parametrize("field,value", [
    ("expectancy", float("nan")), ("expectancy", float("inf")),
    ("drawdown", float("nan")), ("robustness", float("nan")),
    ("robustness", 1.1), ("robustness", -0.1),
])
def test_router_rejects_nonfinite_or_out_of_range_evidence(field, value):
    from app.learning.router import StrategyRouter

    candidate = {
        "strategy": "momentum", "parameters": {"period": 2, "threshold": 0.0},
        "oos": {"oos_expectancy": 0.02, "oos_trades": 10},
        "metrics": {"max_drawdown": 0.1}, "robustness_score": 0.8,
    }
    if field == "expectancy":
        candidate["oos"]["oos_expectancy"] = value
    elif field == "drawdown":
        candidate["metrics"]["max_drawdown"] = value
    else:
        candidate["robustness_score"] = value
    router = StrategyRouter()
    assert router.score(candidate) is None
    assert router.select("range_low_vol", [candidate])["selected"] is False


@pytest.mark.parametrize("minimum", [0, -1, 1.5, True, "10", float("nan")])
def test_scoring_rejects_invalid_minimum_trade_threshold(minimum):
    from app.tournament.scoring import ScoringConfig

    with pytest.raises(ValueError, match="positive integer"):
        ScoringConfig(minimum_trades=minimum)
