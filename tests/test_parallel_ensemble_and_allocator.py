"""Comprehensive tests for Parallel Strategy Ensemble, Empirical OOS Calibration,
Kronos Advisory Fail-Closed Policy, and Portfolio-Level Risk Allocator (v3.7.0a8).
"""

from __future__ import annotations

import math
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings
from app.data.models import Candle
from app.ensemble import (
    STRATEGY_FAMILIES,
    BoundedParallelEvaluator,
    EmpiricalCalibrator,
    PortfolioAllocationConfig,
    PortfolioRiskAllocator,
    SharedIndicatorContext,
    StrategyEnsembleService,
    StrategyRegistry,
    StrategyVariantSpec,
    validate_completed_bars,
)
from app.ensemble.registry import BarValidationError, build_preset_variants
from app.markets.live_paper import LiveMarketPaperEngine
from app.model_lab.datasets import DatasetManager
from app.model_lab.kronos_provider import KronosForecastProvider


def _make_candles(
    symbol: str = "RELIANCE",
    timeframe: str = "5m",
    n: int = 120,
    *,
    start_price: float = 1000.0,
    drift: float = 0.0018,
    tf_minutes: int = 5,
    base_time: datetime | None = None,
) -> list[Candle]:
    t0 = base_time or (datetime.now(UTC) - timedelta(minutes=(n + 3) * tf_minutes))
    bars: list[Candle] = []
    px = start_price
    for i in range(n):
        ts = (t0 + timedelta(minutes=i * tf_minutes)).astimezone(UTC)
        wave = math.sin(i / 4.5) * 0.004
        ret = drift + wave
        open_p = round(px, 4)
        close_p = round(max(1.0, px * (1.0 + ret)), 4)
        high_p = round(max(open_p, close_p) * 1.003, 4)
        low_p = round(min(open_p, close_p) * 0.997, 4)
        bars.append(
            Candle(
                symbol=symbol,
                timestamp=ts,
                open=open_p,
                high=high_p,
                low=low_p,
                close=close_p,
                volume=round(25_000.0 + (i % 9) * 1800.0, 2),
            )
        )
        px = close_p
    return bars


class TestRegistryDedupAndBoundedParallelism:
    def test_eight_families_and_1024_plus_bounded_parallel_evaluation(self) -> None:
        assert len(STRATEGY_FAMILIES) == 8
        variants = build_preset_variants("massive_1024")
        assert len(variants) == 1024

        # Add an exact duplicate variant and verify StrategyRegistry detects & deduplicates it
        first = variants[0]
        dup = StrategyVariantSpec(
            variant_id="duplicate_clone_v1",
            family=first.family,
            params=dict(first.params),
            version=first.version,
            enabled=True,
        )
        reg = StrategyRegistry([*variants, dup])
        summary = reg.summary()
        assert summary["total_variants"] == 1025
        assert summary["unique_canonical_variants"] == 1024
        assert summary["duplicate_variants_detected"] == 1
        assert set(summary["by_family"].keys()) == set(STRATEGY_FAMILIES.keys())

        now = datetime.now(UTC)
        bars_by_symbol = {
            f"SYM{idx}": _make_candles(
                symbol=f"SYM{idx}",
                timeframe="5m",
                n=95,
                start_price=500.0 + idx * 50.0,
                base_time=now - timedelta(minutes=98 * 5),
            )
            for idx in range(6)
        }

        baseline_threads = threading.active_count()
        evaluator = BoundedParallelEvaluator(max_workers=4, batch_timeout_seconds=30.0)
        batch = evaluator.evaluate_watchlist(
            market="NSE",
            timeframe="5m",
            bars_by_symbol=bars_by_symbol,
            variants=reg.list_variants(enabled_only=True),
            now_utc=now,
        )
        peak_threads = threading.active_count()

        # Worker pool must stay strictly bounded (never 1000 threads)
        assert peak_threads <= baseline_threads + 8
        assert batch["workers_used"] == 4
        assert batch["total_evaluations"] == 1025 * 6
        assert batch["rejected_instruments"] == {}

        sym0 = batch["instruments"]["SYM0"]
        assert sym0["unique_evaluated"] == 1024
        assert sym0["duplicates_skipped"] == 1
        sample_sig = sym0["signals"][0]
        assert sample_sig.version == "1.0.0"
        assert sample_sig.bar_timestamp
        assert isinstance(sample_sig.inputs, dict)
        assert sample_sig.rationale


class TestStrictCompletedBarValidation:
    def test_rejects_stale_duplicate_out_of_order_unclosed_and_mismatched_bars(self) -> None:
        now = datetime.now(UTC)
        valid_bars = _make_candles("RELIANCE", "5m", 60, base_time=now - timedelta(minutes=63 * 5))

        validated, meta = validate_completed_bars(
            valid_bars,
            expected_market="NSE",
            expected_symbol="RELIANCE",
            timeframe="5m",
            now_utc=now,
            require_fresh_bars=True,
        )
        assert len(validated) == 60
        assert meta["latest_bar_timestamp"]

        # 1. Mismatched symbol in candle series
        bad_sym_bars = list(valid_bars[:-1]) + [
            Candle(
                symbol="AAPL",
                timestamp=valid_bars[-1].timestamp,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1000.0,
            )
        ]
        with pytest.raises(BarValidationError) as exc_sym:
            validate_completed_bars(
                bad_sym_bars,
                expected_market="NSE",
                expected_symbol="RELIANCE",
                timeframe="5m",
                now_utc=now,
            )
        assert "SYMBOL" in exc_sym.value.code

        # 2. Duplicate timestamp
        dup_bars = list(valid_bars) + [valid_bars[-1]]
        with pytest.raises(BarValidationError) as exc_dup:
            validate_completed_bars(
                dup_bars,
                expected_market="NSE",
                expected_symbol="RELIANCE",
                timeframe="5m",
                now_utc=now,
            )
        assert "DUPLICATE" in exc_dup.value.code or "ORDER" in exc_dup.value.code

        # 3. Out-of-order timestamps
        ooo_bars = list(valid_bars[:-2]) + [valid_bars[-1], valid_bars[-2]]
        with pytest.raises(BarValidationError) as exc_ooo:
            validate_completed_bars(
                ooo_bars,
                expected_market="NSE",
                expected_symbol="RELIANCE",
                timeframe="5m",
                now_utc=now,
            )
        assert exc_ooo.value.code

        # 4. Incomplete / unclosed bar (bar opened 30s ago on 5m timeframe):
        # Must be stripped so strategies never evaluate unclosed bars, and raise if < min_bars remain
        unclosed_tail = list(valid_bars) + [
            Candle(
                symbol="RELIANCE",
                timestamp=now - timedelta(seconds=30),
                open=1000.0,
                high=1002.0,
                low=999.0,
                close=1001.0,
                volume=5000.0,
            )
        ]
        val_stripped, meta_stripped = validate_completed_bars(
            unclosed_tail,
            expected_market="NSE",
            expected_symbol="RELIANCE",
            timeframe="5m",
            now_utc=now,
        )
        assert len(val_stripped) == len(valid_bars)
        assert meta_stripped["dropped_unclosed_bars"] == 1
        assert val_stripped[-1].timestamp == valid_bars[-1].timestamp

        with pytest.raises(BarValidationError) as exc_inc:
            validate_completed_bars(
                unclosed_tail[-34:],
                expected_market="NSE",
                expected_symbol="RELIANCE",
                timeframe="5m",
                now_utc=now,
                min_bars=35,
            )
        assert "INSUFFICIENT" in exc_inc.value.code

        # 5. Excessive missing intraday bars gap (> max_intraday_gap_bars)
        gapped = list(valid_bars[:30]) + _make_candles(
            "RELIANCE", "5m", 30, base_time=valid_bars[29].timestamp + timedelta(minutes=5 * 20)
        )
        with pytest.raises(BarValidationError) as exc_gap:
            validate_completed_bars(
                gapped,
                expected_market="NSE",
                expected_symbol="RELIANCE",
                timeframe="5m",
                now_utc=now + timedelta(hours=3),
                max_intraday_gap_bars=8,
            )
        assert "MISSING" in exc_gap.value.code

        # 5. Stale bars when require_fresh_bars=True
        stale_bars = _make_candles("AAPL", "5m", 60, base_time=now - timedelta(days=4))
        with pytest.raises(BarValidationError) as exc_stale:
            validate_completed_bars(
                stale_bars,
                expected_market="NASDAQ",
                expected_symbol="AAPL",
                timeframe="5m",
                now_utc=now,
                require_fresh_bars=True,
                max_bar_age_seconds=900.0,
            )
        assert "STALE" in exc_stale.value.code


class TestCalibrationKronosFailClosedAndPortfolioRiskAllocator:
    def _trained_calibrator(self) -> EmpiricalCalibrator:
        cal = EmpiricalCalibrator(num_bins=5)
        obs: list[tuple[float, int, str]] = []
        regimes = ("TREND", "RANGE", "VOLATILE")
        for i in range(90):
            s = (i + 1) / 90.0
            win = 1 if (s >= 0.25 and i % 5 != 0) else 0
            obs.append((s, win, regimes[i % 3]))
        cal.fit_oos(obs)
        return cal

    def test_raw_score_never_treated_as_probability_when_uncalibrated(self) -> None:
        now = datetime.now(UTC)
        empty_cal = EmpiricalCalibrator()
        est = empty_cal.estimate(0.95, regime="TREND", min_oos_samples=20)
        assert est["calibrated_confidence"] is None
        assert est["calibration_quality"] == "UNCALIBRATED"

        bars = _make_candles("INFY", "5m", 80, drift=0.004, base_time=now - timedelta(minutes=83 * 5))
        reg = StrategyRegistry(build_preset_variants("compact"))
        ev = BoundedParallelEvaluator(max_workers=2)
        inst = ev.evaluate_instrument(
            market="NSE",
            symbol="INFY",
            timeframe="5m",
            bars=bars,
            variants=reg.list_variants(enabled_only=True),
            now_utc=now,
        )
        alloc = PortfolioRiskAllocator(PortfolioAllocationConfig(min_calibrated_confidence=0.53, min_oos_samples=20))
        dec = alloc.allocate_symbol(
            market="NSE",
            symbol="INFY",
            timeframe="5m",
            ctx=inst["context"],
            signals=inst["signals"],
            weights={s.strategy_id: 1.0 for s in inst["signals"]},
            calibrator=empty_cal,
            account_state={"equity": 1_000_000.0, "cash": 1_000_000.0, "starting_cash": 1_000_000.0, "day_pnl": 0.0},
            positions=[],
            open_orders=[],
            instrument_meta={"symbol": "INFY", "lot_size": 1, "tick_size": 0.05, "product": "D"},
            quote={"price": bars[-1].close, "bid": bars[-1].close - 0.05, "ask": bars[-1].close + 0.05, "timestamp": now.isoformat()},
            session_is_open=True,
            now_utc=now,
        )
        assert dec["final_action"] == "NO_TRADE"
        assert dec["selected_qty"] == 0
        assert any("calibrated" in r.lower() or "oos" in r.lower() for r in dec["no_trade_reasons"])

    def test_duplicate_dedup_opposing_signals_kronos_fail_closed_and_risk_breaches(self) -> None:
        now = datetime.now(UTC)
        cal = self._trained_calibrator()
        bars = _make_candles("NVDA", "5m", 85, start_price=200.0, drift=0.0045, base_time=now - timedelta(minutes=88 * 5))
        preset_v = build_preset_variants("compact")
        dup_v = StrategyVariantSpec(
            variant_id="dup_clone_alloc",
            family=preset_v[0].family,
            params=dict(preset_v[0].params),
            version=preset_v[0].version,
        )
        reg = StrategyRegistry([*preset_v, dup_v])
        ev = BoundedParallelEvaluator(max_workers=2)
        inst = ev.evaluate_instrument(
            market="NASDAQ",
            symbol="NVDA",
            timeframe="5m",
            bars=bars,
            variants=reg.list_variants(enabled_only=True),
            now_utc=now,
        )
        sigs = inst["signals"]
        ctx: SharedIndicatorContext = inst["context"]
        bull_weights = {s.strategy_id: (1.0 if s.direction > 0 else 0.02) for s in sigs}
        fresh_quote = {
            "price": bars[-1].close,
            "bid": round(bars[-1].close - 0.02, 2),
            "ask": round(bars[-1].close + 0.02, 2),
            "timestamp": (now - timedelta(seconds=2)).isoformat(),
        }
        inst_meta = {"symbol": "NVDA", "lot_size": 1, "tick_size": 0.01, "product": "CASH"}
        healthy_acct = {"equity": 100_000.0, "cash": 95_000.0, "starting_cash": 100_000.0, "peak_equity": 100_000.0, "day_pnl": 0.0}

        # Case A: Kronos enabled + REQUIRE_KRONOS + missing Kronos -> fail-closed NO_TRADE
        cfg_req = PortfolioAllocationConfig(
            kronos_enabled=True,
            kronos_fail_closed_policy="REQUIRE_KRONOS",
            min_calibrated_confidence=0.52,
            min_oos_samples=20,
        )
        alloc_req = PortfolioRiskAllocator(cfg_req)
        dec_fail_closed = alloc_req.allocate_symbol(
            market="NASDAQ",
            symbol="NVDA",
            timeframe="5m",
            ctx=ctx,
            signals=sigs,
            weights=bull_weights,
            calibrator=cal,
            account_state=healthy_acct,
            positions=[],
            open_orders=[],
            instrument_meta=inst_meta,
            quote=fresh_quote,
            session_is_open=True,
            kronos_forecast=None,
            now_utc=now,
        )
        assert dec_fail_closed["final_action"] == "NO_TRADE"
        assert dec_fail_closed["aggregation"]["duplicate_variants_deduped"] >= 1
        assert any("kronos" in r.lower() for r in dec_fail_closed["no_trade_reasons"])

        # Case B: DEGRADE_TO_NON_KRONOS -> proceeds cleanly on calibrated non-Kronos ensemble without fabricating forecasts
        cfg_deg = PortfolioAllocationConfig(
            kronos_enabled=True,
            kronos_fail_closed_policy="DEGRADE_TO_NON_KRONOS",
            min_calibrated_confidence=0.52,
            min_oos_samples=20,
            max_position_correlation=0.75,
        )
        alloc_deg = PortfolioRiskAllocator(cfg_deg)
        dec_ok = alloc_deg.allocate_symbol(
            market="NASDAQ",
            symbol="NVDA",
            timeframe="5m",
            ctx=ctx,
            signals=sigs,
            weights=bull_weights,
            calibrator=cal,
            account_state=healthy_acct,
            positions=[],
            open_orders=[],
            instrument_meta=inst_meta,
            quote=fresh_quote,
            session_is_open=True,
            kronos_forecast=None,
            now_utc=now,
        )
        assert dec_ok["final_action"] == "BUY"
        assert dec_ok["selected_qty"] >= 1
        assert dec_ok["stop_loss"] < dec_ok["reference_price"]
        assert dec_ok["take_profit"] > dec_ok["reference_price"]
        assert dec_ok["kronos_contribution"]["usable"] is False

        # Case C: High correlation with existing AMD position -> blocked by correlation check!
        sym_rets = [0.01, -0.008, 0.012, -0.004, 0.009] * 6
        dec_corr = alloc_deg.allocate_symbol(
            market="NASDAQ",
            symbol="NVDA",
            timeframe="5m",
            ctx=ctx,
            signals=sigs,
            weights=bull_weights,
            calibrator=cal,
            account_state=healthy_acct,
            positions=[{"symbol": "AMD", "qty": 100.0, "last_price": 180.0}],
            open_orders=[],
            instrument_meta=inst_meta,
            quote=fresh_quote,
            session_is_open=True,
            returns_by_symbol={"NVDA": sym_rets, "AMD": list(sym_rets)},
            kronos_forecast=None,
            now_utc=now,
        )
        assert dec_corr["final_action"] == "NO_TRADE"
        assert any("correlation" in r.lower() for r in dec_corr["no_trade_reasons"])

        # Case D: Stale quote + wide spread + drawdown breach + already_decided_for_bar idempotency
        stale_wide_quote = {
            "price": bars[-1].close,
            "bid": round(bars[-1].close * 0.98, 2),
            "ask": round(bars[-1].close * 1.02, 2),
            "timestamp": (now - timedelta(minutes=30)).isoformat(),
        }
        dec_breach = alloc_deg.allocate_symbol(
            market="NASDAQ",
            symbol="NVDA",
            timeframe="5m",
            ctx=ctx,
            signals=sigs,
            weights=bull_weights,
            calibrator=cal,
            account_state={"equity": 85_000.0, "cash": 85_000.0, "starting_cash": 100_000.0, "peak_equity": 100_000.0, "day_pnl": -5_000.0},
            positions=[],
            open_orders=[{"symbol": "NVDA", "side": "BUY", "qty": 5, "status": "WORKING", "limit_price": 200.0}],
            instrument_meta=inst_meta,
            quote=stale_wide_quote,
            session_is_open=False,
            already_decided_for_bar=True,
            kronos_forecast=None,
            now_utc=now,
        )
        assert dec_breach["final_action"] == "NO_TRADE"
        failed_ids = {c["check_id"] for c in dec_breach["risk_checks"] if not c["passed"]}
        assert {
            "idempotency_bar_lock",
            "market_session_open",
            "quote_freshness",
            "daily_loss_and_drawdown",
            "existing_orders_and_positions",
            "liquidity_and_spread",
        }.issubset(failed_ids)


class TestRestartPersistenceWalkForwardAblationAndPaperIntegration:
    def test_service_restart_persists_weights_calibration_and_bar_idempotency(self, tmp_path: Path) -> None:
        ml_dir = tmp_path / "ml"
        ens_db = tmp_path / "ens" / "ensemble.sqlite3"
        paper_db = tmp_path / "paper" / "paper.sqlite3"

        ds_mgr = DatasetManager(ml_dir, repo_data_dir=Path("data"))
        kronos = KronosForecastProvider(None)
        paper_engine = LiveMarketPaperEngine(paper_db)

        svc1 = StrategyEnsembleService(
            db_path=ens_db,
            dataset_manager=ds_mgr,
            kronos_provider=kronos,
            paper_engine=paper_engine,
        )
        paper_engine.ensemble_service = svc1
        try:
            abl = svc1.run_ablation_study(
                market="NSE",
                dataset_id="bundled_nse_synthetic_1h",
                timeframe="1h",
                persist_learned_weights=True,
            )
            assert abl["dataset"]["real_market_data"] is False
            comp_ids = [c["candidate_id"] for c in abl["comparisons"]]
            assert "no_trade" in comp_ids
            assert "buy_and_hold" in comp_ids
            assert "ensemble_no_kronos" in comp_ids
            assert "ensemble_with_kronos" in comp_ids
            ens_cmp = next(c for c in abl["comparisons"] if c["candidate_id"] == "ensemble_no_kronos")
            assert ens_cmp["status"] in {"EVALUATED", "COMPLETED"}
            assert "net_return_pct" in ens_cmp["untouched_test_1x"]
            assert "net_return_pct" in ens_cmp["stressed_test_2x"]

            # Evaluate a valid bar and persist decision
            now = datetime.now(UTC)
            bars = _make_candles("RELIANCE", "5m", 80, start_price=1500.0, drift=0.003, base_time=now - timedelta(minutes=83 * 5))
            q = {"price": bars[-1].close, "bid": bars[-1].close - 0.05, "ask": bars[-1].close + 0.05, "timestamp": now.isoformat()}
            ev1 = svc1.evaluate_watchlist_cycle(
                "NSE",
                symbols=["RELIANCE"],
                timeframe="5m",
                bars_override={"RELIANCE": bars},
                quotes_override={"RELIANCE": q},
                session_open_override=True,
                execute_in_paper=False,
                now_utc=now,
            )
            assert ev1["broker_order_api_calls"] == 0
            assert len(ev1["decisions"]) == 1
            assert ev1["decisions"][0]["idempotent_duplicate_skipped"] is False
        finally:
            svc1.close()

        # Re-instantiate service on same SQLite DB (simulating process restart)
        svc2 = StrategyEnsembleService(
            db_path=ens_db,
            dataset_manager=ds_mgr,
            kronos_provider=kronos,
            paper_engine=paper_engine,
        )
        try:
            ov2 = svc2.overview()
            assert ov2["calibration"]["NSE"]["total_oos_samples"] > 0
            # Re-evaluating exact same bar after restart must hit persisted idempotent bar ledger!
            ev2 = svc2.evaluate_watchlist_cycle(
                "NSE",
                symbols=["RELIANCE"],
                timeframe="5m",
                bars_override={"RELIANCE": bars},
                quotes_override={"RELIANCE": q},
                session_open_override=True,
                execute_in_paper=False,
                now_utc=now,
            )
            assert ev2["decisions"][0]["idempotent_duplicate_skipped"] is True
            assert ev2["decisions"][0]["final_action"] == "NO_TRADE"
        finally:
            svc2.close()

    def test_ensemble_api_endpoints_and_zero_live_activation(self, tmp_path: Path) -> None:
        settings = replace(Settings(), api_token="ens-tok", database_url=f"sqlite:///{tmp_path}/ens_web.db")
        headers = {"Authorization": "Bearer ens-tok"}
        with TestClient(create_app(settings, data_dir=tmp_path)) as client:
            ov = client.get("/api/ensemble/overview", headers=headers)
            assert ov.status_code == 200
            body = ov.json()
            assert body["paper_and_research_only"] is True
            assert body["live_execution_authorized"] is False
            assert body["registry"]["enabled_variants"] == 24

            # Switch to massive_1024 preset via API
            cfg_res = client.post(
                "/api/ensemble/config",
                headers=headers,
                json={"preset": "massive_1024", "concurrency_limit": 4},
            )
            assert cfg_res.status_code == 200
            assert cfg_res.json()["registry"]["enabled_variants"] == 1024

            # Run scaling benchmark via API
            bench = client.post(
                "/api/ensemble/benchmark",
                headers=headers,
                json={"variant_counts": [24, 256], "instrument_counts": [1, 4], "concurrency_limit": 4},
            )
            assert bench.status_code == 200
            b_data = bench.json()
            assert len(b_data["results"]) == 4
            assert all(r["missed_instruments"] == 0 and r["duplicate_bars_processed"] == 0 for r in b_data["results"])

            # Verify live autonomy & Alpaca live gates remain strictly disarmed
            live_after = client.get("/api/markets/live/status", headers=headers).json()
            assert live_after["default_live_execution"] == "OFF"
            assert live_after["nse"]["availability"] == "NOT_AVAILABLE"
            assert live_after["nasdaq"]["availability"] == "NOT_AVAILABLE"

    def test_opposing_signals_kronos_mismatch_and_live_paper_engine_parallel_ensemble(self, tmp_path: Path) -> None:
        now = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)  # Thursday US regular session open
        bars = _make_candles("AAPL", "5m", 85, start_price=220.0, drift=0.004, base_time=now - timedelta(minutes=88 * 5))
        preset_v = build_preset_variants("compact")
        reg = StrategyRegistry(preset_v)
        ev = BoundedParallelEvaluator(max_workers=2)
        inst = ev.evaluate_instrument(market="NASDAQ", symbol="AAPL", timeframe="5m", bars=bars, variants=reg.list_variants(enabled_only=True), now_utc=now)
        sigs = inst["signals"]
        ctx = inst["context"]

        cal = EmpiricalCalibrator(num_bins=5)
        cal.fit_oos([((i + 1) / 80.0, 1 if i > 20 else 0, "TREND") for i in range(80)])
        quote = {"last": bars[-1].close, "bid": bars[-1].close - 0.01, "ask": bars[-1].close + 0.01, "timestamp": now.isoformat()}
        acct = {"equity": 100_000.0, "cash": 100_000.0, "starting_cash": 100_000.0, "day_pnl": 0.0}
        meta = {"symbol": "AAPL", "lot_size": 1, "tick_size": 0.01, "product": "CASH"}

        # 1. Nearly equal opposing weights -> rejected by signal_consensus conflict ratio
        bull_ids = [s.strategy_id for s in sigs if s.direction > 0]
        bear_ids = [s.strategy_id for s in sigs if s.direction < 0]
        if bull_ids and bear_ids:
            conflict_weights = {}
            for sid in bull_ids:
                conflict_weights[sid] = 1.0 / len(bull_ids)
            for sid in bear_ids:
                conflict_weights[sid] = 0.85 / len(bear_ids)
            alloc_conf = PortfolioRiskAllocator(PortfolioAllocationConfig(max_conflict_ratio=0.30, min_consensus_score=0.05))
            dec_conf = alloc_conf.allocate_symbol(
                market="NASDAQ",
                symbol="AAPL",
                timeframe="5m",
                ctx=ctx,
                signals=sigs,
                weights=conflict_weights,
                calibrator=cal,
                account_state=acct,
                positions=[],
                open_orders=[],
                instrument_meta=meta,
                quote=quote,
                session_is_open=True,
                now_utc=now,
            )
            assert dec_conf["final_action"] == "NO_TRADE"

        # 2. Mismatched Kronos manifest SHA-256 -> rejected as IDENTITY_MISMATCH (never fabricated)
        alloc_k = PortfolioRiskAllocator(PortfolioAllocationConfig(kronos_enabled=True, kronos_fail_closed_policy="REQUIRE_KRONOS"))
        forged_kronos = {
            "mock_outputs_used": False,
            "advisory_only": True,
            "freshness_state": "FRESH",
            "model_identity": {"manifest_sha256": "deadbeef" * 8},
            "last_input_bar_ts": ctx.latest_bar.timestamp.isoformat(),
            "summary": {"expected_return_pct": 1.2, "up_probability": 0.72, "q10_close": 218.0, "q90_close": 228.0},
        }
        dec_mismatch = alloc_k.allocate_symbol(
            market="NASDAQ",
            symbol="AAPL",
            timeframe="5m",
            ctx=ctx,
            signals=sigs,
            weights={s.strategy_id: 1.0 for s in sigs},
            calibrator=cal,
            account_state=acct,
            positions=[],
            open_orders=[],
            instrument_meta=meta,
            quote=quote,
            session_is_open=True,
            kronos_forecast=forged_kronos,
            expected_kronos_manifest_sha256="abcdef01" * 8,
            now_utc=now,
        )
        assert dec_mismatch["final_action"] == "NO_TRADE"
        assert dec_mismatch["kronos_contribution"]["status"] == "IDENTITY_MISMATCH"

        # 3. End-to-end LiveMarketPaperEngine.sync_live_tick with strategy='parallel_ensemble'
        paper_eng = LiveMarketPaperEngine(
            tmp_path / "lmp_ens.sqlite3",
            nasdaq_quote_provider=lambda _s: quote,
            nasdaq_bars_provider=lambda _s, _tf: bars,
            nasdaq_instrument_resolver=lambda s: {"symbol": s, "trading_symbol": s, "instrument_key": f"NASDAQ|{s}", "lot_size": 1, "tick_size": 0.01, "tradable": True},
            clock=lambda: now,
        )
        ens_svc = StrategyEnsembleService(
            db_path=tmp_path / "ens_lmp.sqlite3",
            paper_engine=paper_eng,
            nasdaq_quote_provider=lambda _s: quote,
            nasdaq_bars_provider=lambda _s, _tf: bars,
            clock=lambda: now,
        )
        ens_svc.calibrators["NASDAQ"] = cal
        ens_svc.weights["NASDAQ"] = {s.strategy_id: (1.0 if s.direction > 0 else 0.0) for s in sigs}
        ens_svc.allocators["NASDAQ"].config.min_calibrated_confidence = 0.52
        paper_eng.ensemble_service = ens_svc

        try:
            paper_eng.start_session("NASDAQ", strategy="parallel_ensemble", timeframe="5m", symbols=["AAPL"])
            snap1 = paper_eng.sync_live_tick("NASDAQ")
            assert snap1["simulated"] is True
            assert snap1["real_order_api_used"] is False
            assert len(snap1["orders"]) == 1
            assert snap1["orders"][0]["status"] == "FILLED"

            # Second sync on the exact same completed bar must NOT submit a second order!
            snap2 = paper_eng.sync_live_tick("NASDAQ")
            assert len(snap2["orders"]) == 1
        finally:
            ens_svc.close()
            paper_eng.close()
