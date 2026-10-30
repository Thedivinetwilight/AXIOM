"""Phase 3 & 5 adversarial tests for Research Pipeline leakage, splits, stress gates, and Registry."""

from __future__ import annotations

import ast
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.data.models import Candle
from app.model_lab.datasets import DatasetManager
from app.model_lab.registry import ModelLabService
from app.model_lab.research_pipeline import (
    LeakageViolationError,
    compute_causal_feature_vector,
    execute_research_pipeline,
    fit_axiom_ridge_on_train,
    simulate_causal_segment,
    split_chronological,
)


def _synth_regime_bars(n: int = 360, *, symbol: str = "RELIANCE") -> list[Candle]:
    t0 = datetime(2026, 1, 2, 9, 15, tzinfo=UTC)
    bars: list[Candle] = []
    px = 1000.0
    for i in range(n):
        # Gentle deterministic wave + trend across regimes
        drift = 0.0028 * math.sin(i / 7.5) + 0.0005
        nxt = max(50.0, px * (1.0 + drift))
        lo = min(px, nxt) * 0.996
        hi = max(px, nxt) * 1.004
        bars.append(
            Candle(
                timestamp=t0 + timedelta(hours=i),
                open=round(px, 4),
                high=round(hi, 4),
                low=round(lo, 4),
                close=round(nxt, 4),
                volume=40_000 + (i % 15) * 1_500,
                symbol=symbol,
            )
        )
        px = nxt
    return bars


def test_adversarial_zero_lookahead_and_untouched_test_selection_invariance():
    """Mutating future bars after train_end / test_start MUST NOT alter causal features, train fit, or hyperparam selection."""
    base_bars = _synth_regime_bars(320)
    sp = split_chronological(base_bars, train_frac=0.60, val_frac=0.20, embargo_bars=5)
    assert sp.train_end + 5 == sp.val_start
    assert sp.val_end + 5 == sp.test_start

    # Construct adversarial future-spiked series where everything >= train_end jumps 50x!
    future_mutated = list(base_bars)
    for idx in range(sp.train_end, len(future_mutated)):
        b = future_mutated[idx]
        future_mutated[idx] = Candle(
            timestamp=b.timestamp,
            open=b.open * 50.0,
            high=b.high * 55.0,
            low=b.low * 45.0,
            close=b.close * 52.0,
            volume=b.volume * 10,
            symbol=b.symbol,
        )

    # 1. Causal feature vectors inside train [22..train_end) are bit-for-bit identical
    for t in range(22, sp.train_end):
        f_orig = compute_causal_feature_vector(base_bars[: t + 1])
        f_mut = compute_causal_feature_vector(future_mutated[: t + 1])
        assert f_orig == f_mut

    # 2. Ridge model fitted on train_bars is bit-for-bit identical (no boundary target leak into embargo/val!)
    m_orig = fit_axiom_ridge_on_train(base_bars[: sp.train_end], alpha=1.0, entry_threshold=0.0004)
    m_mut = fit_axiom_ridge_on_train(future_mutated[: sp.train_end], alpha=1.0, entry_threshold=0.0004)
    assert m_orig.to_artifact_dict() == m_mut.to_artifact_dict()

    # 3. Causal segment simulation on [22, train_end) is bit-for-bit identical
    sim_orig = simulate_causal_segment(
        base_bars, eval_start=22, eval_end=sp.train_end, market="NSE", timeframe="1h", signal_fn=lambda _p: 1
    )
    sim_mut = simulate_causal_segment(
        future_mutated, eval_start=22, eval_end=sp.train_end, market="NSE", timeframe="1h", signal_fn=lambda _p: 1
    )
    assert sim_orig == sim_mut

    # 4. Mutating untouched_test [test_start:] MUST NOT alter selected hyperparameters or train/val metrics!
    test_mutated = list(base_bars)
    for idx in range(sp.test_start, len(test_mutated)):
        b = test_mutated[idx]
        test_mutated[idx] = Candle(
            timestamp=b.timestamp,
            open=b.open * 0.15,
            high=b.high * 0.16,
            low=b.low * 0.14,
            close=b.close * 0.15,
            volume=b.volume,
            symbol=b.symbol,
        )
    ds_meta = {"dataset_id": "adv_ds", "symbol": "RELIANCE", "sha256": "f" * 64, "real_market_data": False}
    run_a = execute_research_pipeline(dataset_meta=ds_meta, bars=base_bars, market="NSE", timeframe="1h")
    run_b = execute_research_pipeline(dataset_meta=ds_meta, bars=test_mutated, market="NSE", timeframe="1h")

    for cand_a, cand_b in zip(run_a["candidates"], run_b["candidates"]):
        assert cand_a["candidate_id"] == cand_b["candidate_id"]
        if "parameters" in cand_a:
            assert cand_a["parameters"] == cand_b["parameters"]
        if "validation" in cand_a:
            assert cand_a["validation"] == cand_b["validation"]


def test_non_monotonic_timestamps_and_invalid_splits_rejected():
    bars = _synth_regime_bars(160)
    broken = list(bars)
    broken[80] = Candle(
        timestamp=broken[79].timestamp,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1000,
        symbol="RELIANCE",
    )
    with pytest.raises(LeakageViolationError, match="Non-monotonic"):
        split_chronological(broken)

    with pytest.raises(ValueError, match="embargo_bars must be >= 1"):
        split_chronological(bars, embargo_bars=0)


def test_cost_stress_and_min_trades_gates_reject_fragile_models(tmp_path: Path):
    svc = ModelLabService(tmp_path / "ml", repo_data_dir=Path("data"))
    bars = _synth_regime_bars(260, symbol="NASDAQ_FRAGILE")
    meta = svc.datasets.register_dataset(
        market="NASDAQ",
        symbol="AAPL",
        timeframe="1h",
        bars_or_csv=bars,
        source="Unit Test",
        feed="SYNTHETIC",
        real_market_data=False,
    )

    # Set extreme spread/slippage + high min_trades=50 so active strategies fail promotion gate!
    run = svc.start_research_run(
        dataset_id=meta["dataset_id"],
        market="NASDAQ",
        timeframe="1h",
        spread_bps=45.0,
        slippage_bps=45.0,
        stress_multiplier=3.0,
        min_trades=50,
        include_kronos=True,  # kronos_provider=None -> must report SKIPPED_KRONOS_UNAVAILABLE, never fake!
        async_execution=False,
    )
    assert run["status"] == "COMPLETED"
    cands = {c["candidate_id"]: c for c in run["manifest"]["candidates"]}
    assert {"buy_and_hold", "no_trade", "moving_average_tuned", "rsi_tuned", "axiom_linear_lagged", "kronos_ensemble"} <= set(cands.keys())
    assert cands["kronos_ensemble"]["status"] == "SKIPPED_KRONOS_UNAVAILABLE"

    models = svc.list_models(market="NASDAQ")
    assert len(models) >= 3
    for m in models:
        assert m["promotion_eligible"] is False
        assert m["registry_status"] == "GATE_REJECTED"
        with pytest.raises(ValueError, match="Cannot promote"):
            svc.promote_model(m["model_id"])


def test_registry_export_import_sha256_verification_and_crash_recovery(tmp_path: Path):
    storage = tmp_path / "ml_reg"
    svc = ModelLabService(storage, repo_data_dir=Path("data"))

    # Run on bundled NSE synthetic 1h fixture (1601 bars) with min_trades=1
    run = svc.start_research_run(
        dataset_id="bundled_nse_synthetic_1h",
        market="NSE",
        timeframe="1h",
        spread_bps=1.0,
        slippage_bps=1.0,
        min_trades=1,
        async_execution=False,
    )
    assert run["status"] == "COMPLETED"
    models = svc.list_models(market="NSE")
    assert len(models) >= 3

    target = models[0]
    bundle = svc.export_model_bundle(target["model_id"])
    assert len(bundle["bundle_sha256"]) == 64

    # Valid import succeeds and marks IMPORTED_PAPER_CANDIDATE with live_execution_authorized=False
    imported = svc.import_model_bundle(bundle)
    assert imported["registry_status"] == "IMPORTED_PAPER_CANDIDATE"
    assert imported["live_execution_authorized"] is False

    # Tampering with a single parameter or metric MUST fail SHA-256 verification!
    tampered = dict(bundle)
    tampered["market"] = "NASDAQ"
    with pytest.raises(ValueError, match="SHA-256 verification failed"):
        svc.import_model_bundle(tampered)

    # Simulate crashed RUNNING job in SQLite and verify safe startup recovery without auto-running!
    with sqlite3.connect(str(svc.db_path)) as conn:
        conn.execute(
            """
            INSERT INTO model_lab_runs
            (run_id, market, dataset_id, timeframe, status, phase, progress_pct, status_detail, config_json, created_at, updated_at)
            VALUES ('mlrun-crashed1', 'NSE', 'bundled_nse_synthetic_1h', '1h', 'RUNNING', 'RULE_TUNING', 40, 'Active before crash', '{}', '2026-10-30T00:00:00Z', '2026-10-30T00:00:00Z')
            """
        )
    recovered_svc = ModelLabService(storage, repo_data_dir=Path("data"))
    rec_run = recovered_svc.get_run("mlrun-crashed1")
    assert rec_run["status"] == "INTERRUPTED_RECOVERED"

    # Dataset manager rejects NaN / corrupted CSV imports
    dm = DatasetManager(tmp_path / "ds_bad")
    bad_csv = "timestamp,open,high,low,close,volume\n2026-01-01T00:00:00Z,100,101,99,NaN,1000\n"
    with pytest.raises(ValueError, match="bar corruption"):
        dm.register_dataset(market="NSE", symbol="INFY", timeframe="1h", bars_or_csv=bad_csv, source="bad", feed="bad")


def test_model_lab_package_has_zero_execution_imports():
    """Static AST guard ensuring app/model_lab and routes/model_lab never import or call broker order clients."""
    forbidden_modules = (
        "alpaca_broker",
        "vantage.connector",
        "vantage.service",
    )
    forbidden_symbols = (
        "AlpacaTradingClient",
        "UpstoxLiveGttExecutor",
        "submit_order",
    )
    checked_files = list(Path("app/model_lab").glob("*.py")) + [Path("app/api/routes/model_lab.py")]
    assert len(checked_files) >= 5
    for py_file in checked_files:
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not any(b in alias.name for b in forbidden_modules), f"{py_file} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not any(b in mod for b in forbidden_modules), f"{py_file} imports from {mod}"
                for alias in node.names:
                    assert alias.name not in forbidden_symbols, f"{py_file} imports forbidden {alias.name}"
            elif isinstance(node, ast.Attribute):
                assert node.attr not in forbidden_symbols, f"{py_file} accesses forbidden {node.attr}"
        assert "/v2/orders" not in source, f"{py_file} references live order URL /v2/orders"
