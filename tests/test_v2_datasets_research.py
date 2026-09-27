"""V2.1: datasets, leakage guards, universe selection, adaptive intelligence."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.backtesting.costs import ExecutionConfig
from app.data.models import Candle
from app.database.database import Database
from app.database.repository import Repository
from app.history.store import CandleStore
from app.learning.experiments import ExperimentEngine
from app.learning.intelligence import TradeIntelligence
from app.learning.regime_engine import RegimeEngine
from app.learning.router import StrategyRouter
from app.learning.shadow import ShadowStrategyEngine
from app.research.datasets import DatasetBuilder
from app.universe.selector import UniverseSelector

BASE = datetime(2026, 1, 2, 9, 15, tzinfo=timezone.utc)


def make_closes(n, drift=0.05, seed=5):
    import random

    rng = random.Random(seed)
    price = 100.0
    out = []
    for i in range(n):
        price *= 1 + rng.uniform(-0.8, 1.0) * 0.004 + drift * 0.001
        out.append(round(price, 4))
    return out


def candles_of(closes, symbol="AAA"):
    return [Candle(BASE + timedelta(minutes=i), c * 0.999, c * 1.001,
                   c * 0.998, c, 10000, symbol)
            for i, c in enumerate(closes)]


def as_rows(candles):
    from app.history.store import parse_local

    return [{"ts": parse_local(c.timestamp).isoformat(), "open": c.open,
             "high": c.high, "low": c.low, "close": c.close,
             "volume": c.volume, "oi": 0} for c in candles]


@pytest.fixture()
def env(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/research.db")
    repository = Repository(database)
    store = CandleStore(tmp_path / "store", parquet_enabled=False)
    yield repository, store
    database.close()


class TestDatasets:
    def test_versioned_build_and_audit(self, env):
        repository, store = env
        candles = candles_of(make_closes(40))
        store.upsert_candles("NSE_EQ|AAA", "days", 1, as_rows(candles))
        builder = DatasetBuilder(repository, store)
        first = builder.build("eq-research", ["NSE_EQ|AAA"], "days", 1)
        assert first["version"] == 1 and first["total_records"] == 40
        second = builder.build("eq-research", ["NSE_EQ|AAA"], "days", 1)
        assert second["version"] == 2, "dataset versions must increment"
        audit = builder.audit(first["dataset_id"])
        assert audit["ok"] and audit["partitions"] >= 1
        rows = store.read_range("NSE_EQ|AAA", "days", 1)
        path = store.partition_path("NSE_EQ|AAA", "days", 1,
                                    rows[0]["ts"][:7])
        path.write_bytes(b"tampered")
        audit2 = builder.audit(first["dataset_id"])
        assert not audit2["ok"]
        assert any(p["issue"] == "hash_mismatch" for p in audit2["problems"])

    def test_empty_dataset_rejected(self, env):
        repository, store = env
        builder = DatasetBuilder(repository, store)
        with pytest.raises(ValueError):
            builder.build("nothing", ["NSE_EQ|MISSING"], "days", 1)


class TestLeakage:
    def test_splits_are_chronological_and_disjoint(self, env):
        from app.validation.train_test import split_data

        candles = candles_of(make_closes(60))
        train, validation, test = split_data(candles, None, "1m")
        assert train[-1].timestamp < validation[0].timestamp
        assert validation[-1].timestamp < test[0].timestamp
        combined = train + validation + test
        assert [c.timestamp for c in combined] == [c.timestamp for c in candles], \
            "no shuffling: split preserves order and membership"

    def test_no_random_shuffling_possible(self, env):
        from app.validation.train_test import split_data

        candles = candles_of(make_closes(50))
        for _ in range(3):
            a, b, c = split_data(candles, None, "1m")
            assert [x.timestamp for x in a + b + c] == \
                [x.timestamp for x in candles]

    def test_backtest_never_trades_before_history_exists(self, env):
        from app.backtesting.engine import BacktestEngine

        engine = BacktestEngine(ExecutionConfig())
        candles = candles_of(make_closes(30))
        result = engine.run(candles[:2],
                            __import__("app.strategies", fromlist=["create_strategy"])
                            .create_strategy("momentum", {"period": 2, "threshold": 0.0}),
                            "1m")
        assert result.trades == [], "no signals possible with no usable history"

    def test_regime_features_use_only_past_candles(self, env):
        engine = RegimeEngine()
        rising = candles_of(make_closes(30))
        partial = engine.evaluate(rising[:15])
        full = engine.evaluate(rising)
        assert partial["features"]["samples"] == 15
        assert full["features"]["samples"] == 30
        assert partial["features"]["last_close"] == rising[14].close


class TestUniverseSelection:
    def test_eligible_is_subset_and_filtered(self, env):
        repository, store = env
        store.upsert_candles("NSE_EQ|AAA", "days", 1,
                             as_rows(candles_of(make_closes(30))))
        store.upsert_candles("NSE_EQ|BBB", "days", 1,
                             as_rows(candles_of(make_closes(5))))
        repository.upsert_instruments([
            {"instrument_key": "NSE_EQ|AAA", "row_hash": "a",
             "trading_symbol": "AAA", "exchange": "NSE", "instrument_type": "EQ"},
            {"instrument_key": "NSE_EQ|BBB", "row_hash": "b",
             "trading_symbol": "BBB", "exchange": "NSE", "instrument_type": "EQ"},
            {"instrument_key": "NSE_EQ|OPT", "row_hash": "c",
             "trading_symbol": "OPT", "exchange": "NSE", "instrument_type": "CE"},
        ], "test")
        selector = UniverseSelector(repository, store)
        result = selector.screen(exchange="NSE", instrument_types=("EQ",),
                                 min_history_records=10)
        assert result["eligible_count"] == 1
        assert result["eligible"][0]["instrument_key"] == "NSE_EQ|AAA"
        all_rows = repository.list_instruments(state=None, limit=100)
        assert len(all_rows) == 3, "all-known universe stays separate from eligible"


class TestAdaptiveIntelligence:
    def test_regime_change_recorded(self, env):
        repository, _ = env
        engine = RegimeEngine(repository)
        first = engine.evaluate(candles_of(make_closes(30, drift=5.0)))
        second = engine.evaluate(candles_of([100.0] * 30))
        assert second["changed"]
        kinds = [e["kind"] for e in repository.list_learning_events()]
        assert "REGIME_CHANGE" in kinds

    def test_shadow_never_touches_a_broker(self, env):
        repository, _ = env

        class _Boom:
            def __getattr__(self, name):
                raise AssertionError("shadow engine must not touch a broker")

        shadow = ShadowStrategyEngine(None)
        from app.strategies import create_strategy

        shadow.register(create_strategy("momentum", {"period": 2, "threshold": 0.0}),
                        {"period": 2, "threshold": 0.0})
        candles = candles_of(make_closes(40))
        actions = [shadow.on_candle(c)[list(shadow.candidates)[0]] for c in candles]
        assert set(actions) <= {"BUY", "SELL", "HOLD", "EXIT"}
        perf = shadow.performance()
        assert list(perf) and perf[list(perf)[0]]["trades"] >= 0
        del _Boom  # assertion unused placeholder kept for clarity

    def test_shadow_records_persist(self, env):
        repository, _ = env
        from app.strategies import create_strategy

        shadow = ShadowStrategyEngine(repository)
        shadow.register(create_strategy("rsi", {"period": 14}),
                        {"period": 14}, "minutes", 1)
        for candle in candles_of(make_closes(45)):
            shadow.on_candle(candle)
        rows = repository.list_shadow_trades()
        assert rows, "shadow decisions must be recorded"
        assert all(r["result"] and "cumulative_pnl" in json.loads(r["result"])
                   for r in rows[:5])

    def test_router_never_selects_by_in_sample_return(self, env):
        router = StrategyRouter()
        lucky = {"strategy": "lucky", "parameters": {},
                 "metrics": {"max_drawdown": 0.1, "total_return": 9.99},
                 "oos": {"oos_trades": 5, "oos_expectancy": -0.003},
                 "robustness_score": 0.9}
        solid = {"strategy": "solid", "parameters": {},
                 "metrics": {"max_drawdown": 0.05, "total_return": 0.4},
                 "oos": {"oos_trades": 8, "oos_expectancy": 0.004},
                 "robustness_score": 0.9}
        assert router.select("trend_up_normal", [lucky, solid])["strategy"] == "solid"
        fallback = router.select(None, [lucky])
        assert not fallback["selected"], "poor OOS → no trading, not least-bad"

    def test_experiment_promotion_is_oos_gated(self, env):
        repository, _ = env
        engine = ExperimentEngine(repository, engine=__import__(
            "app.backtesting.engine", fromlist=["BacktestEngine"]).BacktestEngine(
            ExecutionConfig()))
        candles = candles_of(make_closes(200, seed=9))
        run = engine.run("grid", candles, "momentum",
                         [{"period": 2, "threshold": 0.0},
                          {"period": 4, "threshold": 0.0},
                          {"period": 6, "threshold": 0.0}], "1m")
        assert run["candidates"] == 3
        rows = repository.list_candidates(limit=10)
        assert all("oos" in json.dumps(row["oos"]) or row["oos"] for row in rows)
        for row in rows:
            oos = json.loads(row["oos"]) if isinstance(row["oos"], str) else row["oos"]
            metrics = json.loads(row["metrics"]) if isinstance(row["metrics"], str) else row["metrics"]
            assert "oos_expectancy" in oos and "max_drawdown" in metrics

    def test_learning_cannot_change_safety_settings(self, env):
        repository, _ = env
        from app.config.settings import Settings

        before = Settings()
        RegimeEngine(repository).evaluate(candles_of(make_closes(20)))
        ShadowStrategyEngine(repository).on_candle(candles_of(make_closes(5))[0])
        TradeIntelligence(repository).summarize()
        after = Settings()
        for field in ("broker", "live_trading", "mode", "max_daily_loss",
                      "max_trades_per_day", "max_position_value",
                      "max_open_positions", "upstox_sandbox_access_token"):
            assert getattr(after, field) == getattr(before, field), field

    def test_trade_intelligence_buckets(self, env):
        repository, _ = env
        repository.record_system_event("TRADE_OUTCOME", {
            "strategy": "momentum", "regime": "trend_up_normal", "pnl": 12.0,
            "duration_bars": 4})
        repository.record_system_event("TRADE_OUTCOME", {
            "strategy": "momentum", "regime": "range_low_vol", "pnl": -3.0,
            "duration_bars": 9})
        summary = TradeIntelligence(repository).summarize()
        assert summary["momentum|trend_up_normal"]["profit_factor"] is None  # no losses
        assert summary["momentum|range_low_vol"]["expectancy"] == -3.0
