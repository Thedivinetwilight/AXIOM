"""End-to-end verification of Kronos PAPER session lifecycle, decision pipeline,
conservative failure/conflict policies, warm subprocess worker lifecycle,
restart/recovery disarmed guarantees, secret redaction, and multi-filter
correlated structured logs.

NOTE ON GENUINE KRONOS WEIGHTS:
Tests in this module use deterministic controlled Kronos doubles (`Forecast`
objects) and a real isolated Python subprocess exercising `KronosRuntimeManager`'s
`--session-loop` JSON-line IPC/timeout/crash protocol. They prove AXIOM's
lifecycle, validation, gating, risk, persistence, redaction, and fallback
contracts — they are NOT claimed as proof of genuine NeoQuasar/Kronos-small
neural forecast accuracy or profitability.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from typing import Any

from app.autonomy.kronos_artifacts import ArtifactError
from app.autonomy.kronos_runtime import KronosRuntimeManager
from app.autonomy.models import Forecast
from app.data.models import Candle
from app.ensemble.service import StrategyEnsembleService
from app.markets.kronos_decision import (
    build_forecast_summary,
    combine_paper_strategy_decision,
)
from app.markets.live_paper import LiveMarketPaperEngine
from app.model_lab.kronos_provider import (
    KRONOS_LIFECYCLE_STATES,
    KronosForecastProvider,
)
from app.monitoring.paper_events import (
    filter_structured_events,
    normalize_structured_event,
    redact_sensitive_data,
)
from app.paper_trading.broker import PaperBroker
from app.paper_trading.simulator import BotState, PaperSimulator
from app.strategies import create_strategy


def _make_bars(
    count: int = 48,
    *,
    symbol: str = "AAPL",
    timeframe: str = "5m",
    start_price: float = 180.0,
    trend: str = "bull",
    base_ts: datetime | None = None,
) -> list[Candle]:
    t0 = base_ts or datetime(2026, 10, 30, 14, 0, tzinfo=timezone.utc)
    bars: list[Candle] = []
    for i in range(count):
        if trend == "bull":
            # Flat/slightly below until final bar so prev_fast <= prev_slow and fast_now > slow_now on last bar!
            if i < count - 1:
                px = start_price - (0.05 if i >= count - 6 else 0.0)
            else:
                px = start_price + 5.0
        elif trend == "bear":
            if i < count - 1:
                px = start_price + (0.05 if i >= count - 6 else 0.0)
            else:
                px = start_price - 5.0
        else:
            px = start_price + ((i % 2) * 0.05)
        bars.append(
            Candle(
                symbol=symbol,
                timestamp=t0 + timedelta(minutes=5 * i),
                open=round(px - 0.2, 4),
                high=round(px + 0.5, 4),
                low=round(px - 0.5, 4),
                close=round(px, 4),
                volume=15_000.0 + (i * 100.0),
            )
        )
    return bars


def _us_resolver(sym: str) -> dict[str, Any]:
    s = sym.strip().upper().replace("NASDAQ|", "")
    return {
        "symbol": s,
        "trading_symbol": s,
        "instrument_key": f"NASDAQ|{s}",
        "exchange": "NASDAQ",
        "currency": "USD",
        "lot_size": 1,
        "quantity_step": 1,
        "tick_size": 0.01,
        "product": "CASH",
        "tradable": True,
    }


class ControlledKronosService:
    """Controlled Kronos service double returning real `Forecast` objects."""

    def __init__(
        self,
        *,
        installed: bool = True,
        artifact_verified: bool = True,
        self_test_passed: bool = True,
        runtime_ready: bool = True,
        direction: str = "UP",
        confidence_p_up: float = 0.74,
        band_half_width: float = 1.5,
        mode: str = "ok",
    ) -> None:
        self.installed = installed
        self.artifact_verified = artifact_verified
        self.self_test_passed = self_test_passed
        self.runtime_ready = runtime_ready
        self.direction = direction
        self.confidence_p_up = confidence_p_up
        self.band_half_width = band_half_width
        self.mode = mode  # ok | timeout | crash | startup_crash | nan_output | inverted_quantiles | wrong_horizon | bad_prob
        self.worker_started_count = 0
        self.worker_stopped_count = 0
        self.worker_loaded = False
        self.forecast_calls = 0
        self.manifest = {
            "model": {"repository": "NeoQuasar/Kronos-small", "revision": "901c26c1332695a2a8f243eb2f37243a37bea320"},
            "tokenizer": {"repository": "NeoQuasar/Kronos-Tokenizer-base", "revision": "0e0117387f39004a9016484a186a908917b22426"},
        }

    def status(self) -> dict[str, Any]:
        avail = bool(self.installed and self.artifact_verified and self.runtime_ready)
        return {
            "status": "READY" if avail else "UNAVAILABLE",
            "available_for_inference": avail,
            "artifact_verified": self.artifact_verified,
            "self_test_verified": bool(self.artifact_verified and self.self_test_passed),
            "inference_runtime": "READY" if self.runtime_ready else "MISSING",
            "model_repository": "NeoQuasar/Kronos-small",
            "model_revision": "901c26c1332695a2a8f243eb2f37243a37bea320",
            "tokenizer_repository": "NeoQuasar/Kronos-Tokenizer-base",
            "tokenizer_revision": "0e0117387f39004a9016484a186a908917b22426",
            "manifest_sha256": "abcd1234ef567890abcd1234ef567890abcd1234ef567890abcd1234ef567890",
            "issues": [] if avail else ["WEIGHTS_OR_RUNTIME_MISSING"],
            "last_self_test": (
                {"status": "PASSED", "manifest_sha256": "abcd1234ef567890"}
                if self.self_test_passed
                else None
            ),
            "last_inference": {"load_ms": 42.5, "inference_ms": 84.2},
            "session_worker": {
                "alive": self.worker_loaded,
                "pid": 99999 if self.worker_loaded else None,
                "load_ms": 42.5 if self.worker_loaded else None,
            },
        }

    def verify(self) -> dict[str, Any]:
        return self.status()

    def install_plan(self) -> dict[str, Any]:
        return {
            "expected_total_bytes": 114_823_553,
            "upstream": {"repository": "shiyu-coder/Kronos", "commit": "67b630e67f6a", "license": "MIT"},
            "model": self.manifest["model"],
            "tokenizer": self.manifest["tokenizer"],
            "manifest_sha256": "abcd1234ef567890abcd1234ef567890abcd1234ef567890abcd1234ef567890",
        }

    def start_session_worker(self, *, cpu_threads: int = 2, timeout: float = 45.0) -> dict[str, Any]:
        if self.mode == "startup_crash":
            self.worker_loaded = False
            raise ArtifactError("Session worker crashed during predictor load")
        self.worker_started_count += 1
        self.worker_loaded = True
        return {"status": "SESSION_READY", "loaded": True, "load_ms": 38.0}

    def stop_session_worker(self, *, reason: str = "session_stopped") -> dict[str, Any]:
        self.worker_stopped_count += 1
        was_loaded = self.worker_loaded
        self.worker_loaded = False
        return {"stopped": True, "was_running": was_loaded, "reason": reason}

    def forecast(
        self,
        instrument_key: str,
        candles: list[Candle],
        **kwargs: Any,
    ) -> Forecast:
        self.forecast_calls += 1
        horizon = int(kwargs.get("horizon", 5))
        if self.mode == "timeout":
            raise TimeoutError("Kronos worker exceeded inference timeout")
        if self.mode == "crash":
            self.worker_loaded = False
            raise RuntimeError("Kronos session worker exited unexpectedly (returncode=-9)")

        last_close = float(candles[-1].close)
        delta = 2.4 if self.direction == "UP" else (-2.4 if self.direction == "DOWN" else 0.0)
        mean_val = round(last_close + delta, 4)
        p_up = self.confidence_p_up if self.direction == "UP" else (1.0 - self.confidence_p_up if self.direction == "DOWN" else 0.5)
        q10 = round(mean_val - self.band_half_width, 4)
        q90 = round(mean_val + self.band_half_width, 4)
        out_horizon = horizon

        if self.mode == "nan_output":
            mean_val = float("nan")
        elif self.mode == "inverted_quantiles":
            q10, q90 = round(mean_val + 4.0, 4), round(mean_val - 4.0, 4)
        elif self.mode == "wrong_horizon":
            out_horizon = horizon + 3
        elif self.mode == "bad_prob":
            p_up = 1.35

        return Forecast(
            instrument_key=instrument_key,
            input_end=candles[-1].timestamp,
            generated_at=datetime.now(timezone.utc),
            horizon=out_horizon,
            mean_close=mean_val,
            q10_close=q10,
            q90_close=q90,
            up_probability=p_up,
            artifact_digest="abcd1234ef567890abcd1234ef567890abcd1234ef567890abcd1234ef567890",
            model_revision="901c26c1332695a2a8f243eb2f37243a37bea320",
            tokenizer_revision="0e0117387f39004a9016484a186a908917b22426",
            validated=False,
        )

    def close(self) -> None:
        self.worker_loaded = False


class TestKronosPaperLifecycleAndDecisionChain(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="axiom_kronos_paper_test_", ignore_cleanup_errors=True
        )
        self.base = Path(self.tmp.name)
        # Open-session US regular market hours timestamp (15:00 UTC = 11:00 EDT on Friday 2026-10-30)
        self.now_dt = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
        self.bars_by_sym: dict[str, list[Candle]] = {
            "AAPL": _make_bars(48, symbol="AAPL", start_price=190.0, trend="bull", base_ts=self.now_dt - timedelta(minutes=245)),
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_quote(self, symbol: str) -> dict[str, Any]:
        sym = symbol.strip().upper()
        bars = self.bars_by_sym.get(sym) or self.bars_by_sym["AAPL"]
        px = float(bars[-1].close)
        return {
            "symbol": sym,
            "bid": round(px - 0.02, 4),
            "ask": round(px + 0.02, 4),
            "last": px,
            "volume": 250_000.0,
            "timestamp": (self.now_dt - timedelta(seconds=2)).isoformat(),
            "source_timestamp": (self.now_dt - timedelta(seconds=2)).isoformat(),
            "local_received_at": self.now_dt.isoformat(),
            "feed": "iex",
            "feed_entitlement_label": "Alpaca IEX Single-Venue Real-Time",
            "is_delayed": False,
            "source": "alpaca-iex-test",
        }

    def _make_engine(self, kronos_svc: ControlledKronosService, *, db_name: str = "paper.sqlite3"):
        prov = KronosForecastProvider(
            kronos_svc,
            config_path=self.base / f"{db_name}.kronos.json",
            clock=lambda: self.now_dt,
        )
        eng = LiveMarketPaperEngine(
            self.base / db_name,
            nasdaq_quote_provider=self._make_quote,
            nasdaq_bars_provider=lambda sym, tf: self.bars_by_sym[sym.strip().upper()],
            kronos_provider=prov,
            nasdaq_instrument_resolver=_us_resolver,
            clock=lambda: self.now_dt,
        )
        return prov, eng

    def test_ready_start_infer_decision_risk_fill_and_clean_stop(self) -> None:
        # Unconfigured / uninstalled Kronos starts in NOT_CONFIGURED
        prov_unconf = KronosForecastProvider(
            ControlledKronosService(installed=False, artifact_verified=False, runtime_ready=False, self_test_passed=False)
        )
        self.assertEqual(prov_unconf.status()["lifecycle_state"], "NOT_CONFIGURED")

        svc = ControlledKronosService(direction="UP", confidence_p_up=0.76)
        prov, eng = self._make_engine(svc)
        try:
            st0 = prov.status()
            self.assertEqual(st0["lifecycle_state"], "READY")
            self.assertEqual(prov.session_view("LIVE_MARKET_PAPER:NASDAQ")["lifecycle_state"], "NOT_CONFIGURED")
            self.assertIn(st0["lifecycle_state"], KRONOS_LIFECYCLE_STATES)
            self.assertTrue(st0["truth_matrix"]["installed"])
            self.assertTrue(st0["truth_matrix"]["validated"])
            self.assertFalse(st0["truth_matrix"]["loaded"])
            self.assertFalse(st0["truth_matrix"]["running"])
            self.assertFalse(st0["truth_matrix"]["forecast_produced"])
            self.assertFalse(st0["truth_matrix"]["forecast_consumed"])

            eng.remove_watchlist_symbol("NASDAQ", "MSFT")
            eng.remove_watchlist_symbol("NASDAQ", "NVDA")

            snap_start = eng.start_session(
                "NASDAQ",
                strategy="kronos_ensemble",
                timeframe="5m",
                symbols=["AAPL"],
                kronos_enabled=True,
                kronos_failure_policy="FAIL_CLOSED_NO_ENTRY",
            )
            self.assertEqual(snap_start["session_state"], "RUNNING")
            self.assertEqual(snap_start["kronos_session"]["lifecycle_state"], "RUNNING")
            self.assertEqual(svc.worker_started_count, 1)
            self.assertTrue(prov.status()["truth_matrix"]["loaded"])
            self.assertTrue(prov.status()["truth_matrix"]["running"])

            # First completed-bar evaluation: rule BUY + Kronos UP -> BUY order + simulated fill
            tick1 = eng.sync_live_tick("NASDAQ")
            self.assertEqual(len(tick1["decisions"]), 1)
            dec1 = tick1["decisions"][0]
            self.assertEqual(dec1["action"], "BUY")
            self.assertTrue(dec1["forecast_produced"])
            self.assertTrue(dec1["forecast_consumed"])
            self.assertEqual(dec1["policy_applied"], "KRONOS_CONFIRMED_ENTRY")
            self.assertIsNotNone(dec1["order_client_id"])

            st_after = prov.status()
            self.assertTrue(st_after["truth_matrix"]["forecast_produced"])
            self.assertTrue(st_after["truth_matrix"]["forecast_consumed"])
            self.assertEqual(st_after["truth_matrix"]["produced_count"], 1)
            self.assertEqual(st_after["truth_matrix"]["consumed_count"], 1)
            self.assertIsNotNone(st_after["diagnostics"]["last_latency_ms"])

            # Repeated sync on identical completed bar is strictly idempotent (0 duplicate orders)
            orders_count_1 = len(tick1["orders"])
            tick2 = eng.sync_live_tick("NASDAQ")
            self.assertEqual(len(tick2["orders"]), orders_count_1)
            self.assertEqual(svc.forecast_calls, 1)

            # Inspect persisted per-symbol decision explanation
            exp = tick2["decisions"][0]["explanation"]
            self.assertEqual(exp["rule_action"], "BUY")
            self.assertEqual(exp["forecast_summary"]["direction"], "UP")
            self.assertEqual(exp["forecast_summary"]["model_id"], "NeoQuasar/Kronos-small")
            self.assertEqual(exp["input_window"]["bar_count"], 48)
            self.assertTrue(exp["risk_checks"]["passed"])
            self.assertEqual(len(tick2["positions"]), 1)

            # Clean operator stop releases Kronos session worker
            snap_stop = eng.transition_session("NASDAQ", "stop", reason="operator_done")
            self.assertEqual(snap_stop["session_state"], "STOPPED")
            self.assertEqual(prov.status()["lifecycle_state"], "STOPPED")
            self.assertFalse(prov.status()["truth_matrix"]["running"])
            self.assertFalse(prov.status()["truth_matrix"]["loaded"])
            self.assertGreaterEqual(svc.worker_stopped_count, 1)
        finally:
            eng.close()

    def test_authoritative_risk_gate_blocks_oversized_kronos_agreed_order(self) -> None:
        svc = ControlledKronosService(direction="UP", confidence_p_up=0.80)
        prov, eng = self._make_engine(svc)
        try:
            eng.remove_watchlist_symbol("NASDAQ", "MSFT")
            eng.remove_watchlist_symbol("NASDAQ", "NVDA")
            # Set max_order_value below 1 share of AAPL (~$195) so risk gate rejects entry
            eng.update_config("NASDAQ", max_order_value=50.0)
            eng.start_session("NASDAQ", strategy="kronos_ensemble", timeframe="5m", symbols=["AAPL"])
            tick = eng.sync_live_tick("NASDAQ")
            self.assertEqual(len(tick["positions"]), 0)
            dec = tick["decisions"][0]
            self.assertEqual(dec["action"], "BUY")
            self.assertIsNone(dec["order_client_id"])
            self.assertFalse(dec["explanation"]["risk_checks"]["passed"])
            logs = eng.structured_logs(event_type="PAPER_RISK_LIMIT_EVENT")
            self.assertGreaterEqual(logs["total_matching"], 1)
        finally:
            eng.close()

    def test_failure_policies_fail_closed_vs_explicit_fallback_and_protective_exit(self) -> None:
        # 1. Missing self-test + FAIL_CLOSED_NO_ENTRY blocks new BUY entry
        svc_unverified = ControlledKronosService(self_test_passed=False)
        prov, eng = self._make_engine(svc_unverified, db_name="fail_closed.sqlite3")
        try:
            eng.remove_watchlist_symbol("NASDAQ", "MSFT")
            eng.remove_watchlist_symbol("NASDAQ", "NVDA")
            snap = eng.start_session(
                "NASDAQ",
                strategy="kronos_ensemble",
                timeframe="5m",
                symbols=["AAPL"],
                kronos_failure_policy="FAIL_CLOSED_NO_ENTRY",
            )
            self.assertEqual(snap["kronos_session"]["lifecycle_state"], "FAILED")
            self.assertTrue(prov.status()["truth_matrix"]["failed"])
            tick = eng.sync_live_tick("NASDAQ")
            dec = tick["decisions"][0]
            self.assertEqual(dec["action"], "HOLD")
            self.assertIn("fail-closed", dec["reason"].lower())
            self.assertFalse(dec["forecast_produced"])
            self.assertFalse(dec["forecast_consumed"])
            self.assertEqual(len(tick["orders"]), 0)
        finally:
            eng.close()

        # 2. EXPLICIT_NON_KRONOS_FALLBACK marks Kronos DEGRADED and uses rule signal without faking forecast
        svc_timeout = ControlledKronosService(mode="timeout")
        prov2, eng2 = self._make_engine(svc_timeout, db_name="fallback.sqlite3")
        try:
            eng2.remove_watchlist_symbol("NASDAQ", "MSFT")
            eng2.remove_watchlist_symbol("NASDAQ", "NVDA")
            eng2.start_session(
                "NASDAQ",
                strategy="moving_average",
                timeframe="5m",
                symbols=["AAPL"],
                kronos_enabled=True,
                kronos_failure_policy="EXPLICIT_NON_KRONOS_FALLBACK",
            )
            tick2 = eng2.sync_live_tick("NASDAQ")
            dec2 = tick2["decisions"][0]
            self.assertEqual(dec2["action"], "BUY")
            self.assertEqual(dec2["policy_applied"], "EXPLICIT_NON_KRONOS_FALLBACK")
            self.assertFalse(dec2["forecast_produced"])
            self.assertFalse(dec2["forecast_consumed"])
            st2 = prov2.status()
            self.assertEqual(st2["lifecycle_state"], "DEGRADED")
            self.assertTrue(st2["truth_matrix"]["degraded"])
        finally:
            eng2.close()

        # 3. Protective/exit SELL is never blocked even under FAIL_CLOSED_NO_ENTRY when Kronos fails
        bear_bars = _make_bars(48, symbol="AAPL", start_price=190.0, trend="bear", base_ts=self.now_dt - timedelta(minutes=245))
        comb_exit = combine_paper_strategy_decision(
            strategy="kronos_ensemble",
            bars=bear_bars,
            base_rule_signal={"action": "SELL", "reason": "bearish_cross_exit"},
            kronos_configured=True,
            kronos_outcome={"ok": False, "status": "TIMEOUT", "error_code": "INFERENCE_TIMEOUT", "error_reason": "Worker timed out"},
            policy={"failure_policy": "FAIL_CLOSED_NO_ENTRY"},
            current_position_qty=5.0,
        )
        self.assertEqual(comb_exit["action"], "SELL")
        self.assertEqual(comb_exit["policy_applied"], "PROTECTIVE_OR_EXIT_SELL_ALLOWED")

    def test_malformed_outputs_conflict_and_stale_handling(self) -> None:
        for bad_mode in ("nan_output", "inverted_quantiles", "wrong_horizon", "bad_prob"):
            svc = ControlledKronosService(mode=bad_mode)
            prov, _ = self._make_engine(svc, db_name=f"{bad_mode}.sqlite3")
            prov.start_for_paper_session(
                "LIVE_MARKET_PAPER:NASDAQ",
                session_id="sess-bad",
                market="NASDAQ",
                strategy="kronos_ensemble",
                kronos_requested=True,
                failure_policy="FAIL_CLOSED_NO_ENTRY",
            )
            res = prov.predict_for_session(
                "LIVE_MARKET_PAPER:NASDAQ",
                session_id="sess-bad",
                decision_id="dec-bad",
                market="NASDAQ",
                symbol="AAPL",
                timeframe="5m",
                bars=self.bars_by_sym["AAPL"],
            )
            self.assertFalse(res["ok"], f"Expected {bad_mode} to fail validation")
            self.assertEqual(res["failure_code"], "INVALID_OUTPUT")
            self.assertFalse(res["forecast_produced"])
            self.assertGreaterEqual(prov.status()["diagnostics"]["invalid_output_count"], 1)

        # Conflict policy VETO_CONFLICTING_ENTRY: rule BUY + eligible Kronos DOWN -> HOLD (produced=True, consumed=True)
        svc_down = ControlledKronosService(direction="DOWN", confidence_p_up=0.78)
        prov_c, _ = self._make_engine(svc_down, db_name="conflict.sqlite3")
        prov_c.start_for_paper_session(
            "LIVE_MARKET_PAPER:NASDAQ",
            session_id="sess-c",
            market="NASDAQ",
            strategy="moving_average",
            kronos_requested=True,
        )
        k_call = prov_c.predict_for_session(
            "LIVE_MARKET_PAPER:NASDAQ",
            session_id="sess-c",
            decision_id="dec-c",
            market="NASDAQ",
            symbol="AAPL",
            timeframe="5m",
            bars=self.bars_by_sym["AAPL"],
        )
        comb = combine_paper_strategy_decision(
            strategy="moving_average",
            bars=self.bars_by_sym["AAPL"],
            base_rule_signal={"action": "BUY", "reason": "sma_bull"},
            kronos_configured=True,
            kronos_outcome=k_call,
            policy=prov_c.config.to_dict(),
        )
        self.assertEqual(comb["action"], "HOLD")
        self.assertEqual(comb["policy_applied"], "VETO_CONFLICTING_ENTRY")
        self.assertTrue(comb["forecast_produced"])
        self.assertTrue(comb["forecast_consumed"])

        # Stale forecast is rejected under FAIL_CLOSED_NO_ENTRY
        stale_outcome = {
            **k_call,
            "forecast": {**k_call["forecast"], "freshness_state": "STALE"},
        }
        stale_comb = combine_paper_strategy_decision(
            strategy="kronos_ensemble",
            bars=self.bars_by_sym["AAPL"],
            base_rule_signal={"action": "BUY", "reason": "sma_bull"},
            kronos_configured=True,
            kronos_outcome=stale_outcome,
            policy={"failure_policy": "FAIL_CLOSED_NO_ENTRY"},
        )
        self.assertEqual(stale_comb["action"], "HOLD")
        self.assertFalse(stale_comb["forecast_consumed"])

    def test_restart_recovery_returns_disarmed_and_preserves_pnl_separation(self) -> None:
        db_file = self.base / "restart_recovery.sqlite3"
        cfg_file = self.base / "kronos_persisted.json"
        svc1 = ControlledKronosService(direction="UP", confidence_p_up=0.75)
        prov1 = KronosForecastProvider(svc1, config_path=cfg_file, clock=lambda: self.now_dt)
        prov1.update_config(paper_session_enabled=True, model_loaded_preference=True)
        eng1 = LiveMarketPaperEngine(
            db_file,
            nasdaq_quote_provider=self._make_quote,
            nasdaq_bars_provider=lambda s, tf: self.bars_by_sym[s.strip().upper()],
            kronos_provider=prov1,
            nasdaq_instrument_resolver=_us_resolver,
            clock=lambda: self.now_dt,
        )
        eng1.remove_watchlist_symbol("NASDAQ", "MSFT")
        eng1.remove_watchlist_symbol("NASDAQ", "NVDA")
        eng1.start_session("NASDAQ", strategy="kronos_ensemble", timeframe="5m", symbols=["AAPL"])
        eng1.sync_live_tick("NASDAQ")
        eng1.close()

        svc2 = ControlledKronosService(direction="UP", confidence_p_up=0.75)
        prov2 = KronosForecastProvider(svc2, config_path=cfg_file, clock=lambda: self.now_dt)
        eng2 = LiveMarketPaperEngine(
            db_file,
            nasdaq_quote_provider=self._make_quote,
            nasdaq_bars_provider=lambda s, tf: self.bars_by_sym[s.strip().upper()],
            kronos_provider=prov2,
            nasdaq_instrument_resolver=_us_resolver,
            clock=lambda: self.now_dt,
        )
        try:
            # Restart NEVER auto-loads Kronos weights or auto-resumes trading
            st2 = prov2.status()
            self.assertEqual(st2["lifecycle_state"], "READY")
            self.assertEqual(prov2.session_view("LIVE_MARKET_PAPER:NASDAQ")["lifecycle_state"], "NOT_CONFIGURED")
            self.assertFalse(st2["truth_matrix"]["loaded"])
            self.assertFalse(st2["truth_matrix"]["running"])
            self.assertEqual(svc2.worker_started_count, 0)

            snap2 = eng2.market_snapshot("NASDAQ")
            self.assertEqual(snap2["session_state"], "RECOVERED_PAUSED")
            self.assertNotEqual(snap2["session_state"], "RUNNING")
            snap_stopped = eng2.transition_session("NASDAQ", "stop", reason="operator_clear_recovered")
            self.assertEqual(snap_stopped["session_state"], "STOPPED")
            self.assertEqual(snap_stopped["account"]["current_session_total_pnl"], 0.0)
            self.assertEqual(len(snap_stopped["positions"]), 1)
        finally:
            eng2.close()


class TestWarmSubprocessWorkerAndSimulatorAndLogs(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="axiom_worker_sim_logs_", ignore_cleanup_errors=True
        )
        self.base = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_real_subprocess_session_worker_ready_infer_timeout_crash_and_stop(self) -> None:
        fake_worker = self.base / "fake_kronos_worker.py"
        fake_worker.write_text(
            textwrap.dedent(
                """
                import json, sys, time
                init_line = sys.stdin.readline()
                print(json.dumps({"status": "SESSION_READY", "load_ms": 11.8}), flush=True)
                for raw in sys.stdin:
                    msg = json.loads(raw)
                    op = msg.get("op")
                    req_id = msg.get("request_id")
                    if op == "shutdown":
                        print(json.dumps({"request_id": req_id, "status": "SHUTDOWN"}), flush=True)
                        break
                    payload = msg.get("payload") or {}
                    sym = payload.get("instrument_key") or ""
                    if "SLEEP" in sym:
                        time.sleep(2.0)
                    if "CRASH" in sym:
                        sys.exit(17)
                    print(json.dumps({
                        "request_id": req_id,
                        "status": "OK",
                        "inference_ms": 5.4,
                        "mean_close": 195.0,
                    }), flush=True)
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        lock_file = Path("config/kronos-runtime-requirements.txt").resolve()
        mgr = KronosRuntimeManager(
            self.base / "artifacts",
            lock_file,
            fake_worker,
            application_root=Path(".").resolve(),
            python_command=[sys.executable],
        )
        # Allow python() without requiring pip inside minimal sandbox
        mgr.python = lambda: {"executable": sys.executable, "version": [3, 11, 0], "system": "Linux", "machine": "x86_64"}  # type: ignore[method-assign]
        try:
            started = mgr.start_session_worker({"offline": True}, timeout=10.0)
            self.assertEqual(started["status"], "SESSION_READY")
            self.assertTrue(mgr.session_worker_status()["alive"])

            out = mgr.session_forecast({"instrument_key": "AAPL"}, timeout=5.0)
            self.assertEqual(out["status"], "OK")
            self.assertEqual(out["mean_close"], 195.0)

            # Timeout terminates hung worker cleanly without leaving false-healthy alive=True
            with self.assertRaises(ArtifactError):
                mgr.session_forecast({"instrument_key": "SLEEP_SYM"}, timeout=0.25)
            self.assertFalse(mgr.session_worker_status()["alive"])

            # Restart worker and verify mid-inference crash raises ArtifactError and cleans up
            mgr.start_session_worker({"offline": True}, timeout=10.0)
            with self.assertRaises(ArtifactError):
                mgr.session_forecast({"instrument_key": "CRASH_SYM"}, timeout=5.0)
            self.assertFalse(mgr.session_worker_status()["alive"])
        finally:
            mgr.stop_session_worker()

    def test_local_paper_simulator_kronos_lifecycle_and_ensemble_consumption(self) -> None:
        svc = ControlledKronosService(direction="UP", confidence_p_up=0.77)
        prov = KronosForecastProvider(svc, config_path=self.base / "sim_k.json")
        broker = PaperBroker(currency="USD", timeframe="5m", restore=False)
        sim = PaperSimulator(broker, kronos_provider=prov)
        candles = _make_bars(40, symbol="AAPL", timeframe="5m", start_price=150.0, trend="bull")

        from app.data.realtime import ReplayStream

        async def _exercise():
            st = await sim.start(
                ReplayStream(candles, delay=0.0),
                create_strategy("moving_average", {"fast_period": 5, "slow_period": 20}),
                ["AAPL"],
                kronos_enabled=True,
                kronos_failure_policy="FAIL_CLOSED_NO_ENTRY",
            )
            self.assertEqual(st["kronos_session"]["lifecycle_state"], "RUNNING")
            await sim.task
            self.assertEqual(sim.state, BotState.STOPPED)
            self.assertEqual(prov.status()["lifecycle_state"], "STOPPED")
            self.assertIn("AAPL", sim.kronos_decisions)
            self.assertGreaterEqual(prov.status()["truth_matrix"]["produced_count"], 1)
            self.assertGreaterEqual(prov.status()["truth_matrix"]["consumed_count"], 1)

        asyncio.run(_exercise())

        # Also verify StrategyEnsembleService records forecast consumption via predict_for_session
        ens = StrategyEnsembleService(
            ":memory:",
            kronos_provider=prov,
            nasdaq_quote_provider=lambda s: {
                "symbol": s,
                "bid": 159.9,
                "ask": 160.1,
                "last": 160.0,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            nasdaq_bars_provider=lambda s, tf: candles,
            nasdaq_instrument_resolver=_us_resolver,
        )
        try:
            prov.update_config(inference_enabled=True, paper_session_enabled=True)
            prov.start_for_paper_session(
                "LIVE_MARKET_PAPER:NASDAQ",
                session_id="ens-sess-1",
                market="NASDAQ",
                strategy="parallel_ensemble",
                kronos_requested=True,
            )
            before_consumed = prov.status()["truth_matrix"]["consumed_count"]
            cycle = ens.evaluate_watchlist_cycle(
                "NASDAQ",
                symbols=["AAPL"],
                timeframe="5m",
                bars_override={"AAPL": candles},
                session_open_override=True,
                execute_in_paper=False,
            )
            self.assertEqual(len(cycle["decisions"]), 1)
            self.assertEqual(cycle["decisions"][0]["kronos_contribution"]["status"], "VERIFIED_FRESH")
            self.assertTrue(cycle["decisions"][0]["kronos_contribution"]["advisory_used"])
            self.assertGreater(prov.status()["truth_matrix"]["consumed_count"], before_consumed)
        finally:
            ens.close()

    def test_structured_logs_redaction_correlation_filters_and_simulated_vs_broker_labels(self) -> None:
        now = datetime(2026, 10, 30, 15, 30, tzinfo=timezone.utc)
        svc = ControlledKronosService(direction="UP", confidence_p_up=0.79)
        prov = KronosForecastProvider(svc, clock=lambda: now)
        bars = _make_bars(48, symbol="AAPL", start_price=190.0, trend="bull", base_ts=now - timedelta(minutes=245))
        eng = LiveMarketPaperEngine(
            self.base / "audit_logs.sqlite3",
            nasdaq_quote_provider=lambda s: {
                "symbol": s,
                "bid": 199.9,
                "ask": 200.1,
                "last": 200.0,
                "timestamp": (now - timedelta(seconds=1)).isoformat(),
                "source_timestamp": (now - timedelta(seconds=1)).isoformat(),
            },
            nasdaq_bars_provider=lambda s, tf: bars,
            kronos_provider=prov,
            nasdaq_instrument_resolver=_us_resolver,
            clock=lambda: now,
        )
        try:
            eng.remove_watchlist_symbol("NASDAQ", "MSFT")
            eng.remove_watchlist_symbol("NASDAQ", "NVDA")
            with eng._lock, eng._connect() as conn:
                eng._append_audit_conn(
                    conn,
                    "NASDAQ",
                    "CONNECTION_PROBE",
                    {
                        "access_token": "upstox-super-secret-token-999999",
                        "api_secret": "alpaca-secret-key-abcdef123456",
                        "authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0In0.sig",
                        "endpoint": "https://example.test/v2?token=url_secret_xyz",
                    },
                )
                conn.commit()

            snap = eng.start_session("NASDAQ", strategy="kronos_ensemble", timeframe="5m", symbols=["AAPL"])
            sess_id = snap["account"]["current_session_run_id"]
            eng.sync_live_tick("NASDAQ")

            broker_ev = normalize_structured_event(
                event_id="brk-live-ev-1",
                timestamp=now.isoformat(),
                event_type="LIVE_BROKER_ORDER_CONFIRMED",
                market="NASDAQ",
                source="ALPACA_LIVE_BROKER",
                payload={"symbol": "AAPL", "order_id": "brk-live-1", "simulated": False, "real_money": True},
            )

            all_logs = eng.structured_logs(extra_events=[broker_ev], limit=300)
            serialized = json.dumps(all_logs)
            self.assertNotIn("upstox-super-secret-token-999999", serialized)
            self.assertNotIn("alpaca-secret-key-abcdef123456", serialized)
            self.assertNotIn("url_secret_xyz", serialized)
            self.assertIn("[REDACTED]", serialized)

            sess_logs = eng.structured_logs(session_id=sess_id, limit=200)
            kinds = {e["event_type"] for e in sess_logs["events"]}
            self.assertIn("KRONOS_FORECAST_PRODUCED", kinds)
            self.assertIn("KRONOS_FORECAST_CONSUMED", kinds)
            self.assertIn("PAPER_STRATEGY_DECISION", kinds)
            self.assertTrue(kinds.intersection({"PAPER_ORDER_SUBMITTED", "PAPER_ORDER_ACCEPTED"}))
            self.assertTrue(kinds.intersection({"PAPER_ORDER_FILLED", "PAPER_ORDER_FILL"}))
            self.assertIn("PAPER_POSITION_CHANGED", kinds)
            for ev in sess_logs["events"]:
                self.assertTrue(ev["simulated"])
                self.assertIn("0 BROKER ORDERS", ev["execution_badge"])
                self.assertEqual(ev["pnl_scope_label"], "SIMULATED_PAPER_PNL")

            brk_only = filter_structured_events(all_logs["events"], event_type="LIVE_BROKER_ORDER_CONFIRMED", now_utc=now)
            self.assertEqual(len(brk_only), 1)
            self.assertFalse(brk_only[0]["simulated"])
            self.assertIn("REAL MONEY", brk_only[0]["execution_badge"])
            self.assertEqual(brk_only[0]["pnl_scope_label"], "BROKER_CONFIRMED_REAL_PNL")

            by_sym = eng.structured_logs(symbol="AAPL", category="STRATEGY_DECISION", strategy="kronos_ensemble")
            self.assertEqual(by_sym["total_matching"], 1)
            dec_id = by_sym["events"][0]["decision_id"]
            self.assertTrue(dec_id.startswith("dec-"))
            by_corr = eng.structured_logs(query=dec_id)
            self.assertGreaterEqual(by_corr["total_matching"], 3)

            empty_res = eng.structured_logs(symbol="NONEXISTENT_TICKER_XYZ")
            self.assertEqual(empty_res["total_matching"], 0)
            self.assertEqual(empty_res["events"], [])
        finally:
            eng.close()

    def test_recursive_secret_redactor_preserves_safe_metrics(self) -> None:
        raw = {
            "symbol": "RELIANCE",
            "fill_price": 2950.25,
            "nested": {
                "UPSTOX_ACCESS_TOKEN": "tok_secret_12345",
                "password": "my_password_99",
                "headers": {"Authorization": "Bearer secret_bearer_token"},
            },
        }
        clean = redact_sensitive_data(raw)
        self.assertEqual(clean["symbol"], "RELIANCE")
        self.assertEqual(clean["fill_price"], 2950.25)
        self.assertEqual(clean["nested"]["UPSTOX_ACCESS_TOKEN"], "[REDACTED]")
        self.assertEqual(clean["nested"]["password"], "[REDACTED]")
        self.assertEqual(clean["nested"]["headers"]["Authorization"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
