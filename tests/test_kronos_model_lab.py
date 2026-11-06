"""Phase 2 & 5 tests for KronosForecastProvider, Readiness Checklists, and Paper Sync."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.autonomy.models import Forecast
from app.config.settings import Settings
from app.data.models import Candle
from app.markets.live_paper import LiveMarketPaperEngine, evaluate_bar_strategy_signal
from app.model_lab.kronos_provider import (
    KronosForecastProvider,
    KronosInputError,
    KronosUnavailableError,
)


def _make_bars(count: int = 64, *, start_price: float = 100.0, step_pct: float = 0.002) -> list[Candle]:
    t0 = datetime(2026, 10, 29, 9, 30, tzinfo=UTC)
    out: list[Candle] = []
    px = start_price
    for i in range(count):
        nxt = px * (1.0 + step_pct)
        lo = min(px, nxt) * 0.998
        hi = max(px, nxt) * 1.002
        out.append(
            Candle(
                timestamp=t0 + timedelta(minutes=5 * i),
                open=round(px, 4),
                high=round(hi, 4),
                low=round(lo, 4),
                close=round(nxt, 4),
                volume=25_000 + i * 100,
                symbol="AAPL",
            )
        )
        px = nxt
    return out


class FakeKronosEngine:
    """Deterministic test double for KronosService exercising provider states & failure modes."""

    def __init__(self, *, ready: bool = True, self_tested: bool = True, delay_s: float = 0.0, corrupt: bool = False) -> None:
        self.ready = ready
        self.self_tested = self_tested
        self.delay_s = delay_s
        self.corrupt = corrupt
        self.closed_count = 0
        self.manifest = {
            "model": {"repository": "NeoQuasar/Kronos-small", "revision": "901c26c1332695a2a8f243eb2f37243a37bea320"},
            "tokenizer": {"repository": "NeoQuasar/Kronos-Tokenizer-base", "revision": "0e0117387f39004a9016484a186a908917e22426"},
        }

    def status(self) -> dict[str, Any]:
        issues = []
        if self.corrupt:
            issues.append("SHA256_MISMATCH: model.safetensors corrupted")
        elif not self.ready:
            issues.append("WEIGHTS_MISSING: model.safetensors not installed")
        return {
            "status": "READY" if (self.ready and not self.corrupt) else "UNAVAILABLE",
            "available_for_inference": bool(self.ready and not self.corrupt),
            "artifact_verified": bool(self.ready and not self.corrupt),
            "inference_runtime": "READY" if self.ready else "MISSING",
            "manifest_sha256": "a" * 64,
            "model_revision": "901c26c1332695a2a8f243eb2f37243a37bea320",
            "tokenizer_revision": "0e0117387f39004a9016484a186a908917e22426",
            "issues": issues,
            "last_self_test": {"status": "PASSED"} if self.self_tested else None,
            "last_inference": {"load_ms": 42.5, "inference_ms": 118.4},
        }

    def verify(self) -> dict[str, Any]:
        return self.status()

    def install_plan(self) -> dict[str, Any]:
        return {
            "manifest_sha256": "a" * 64,
            "expected_total_bytes": 114_823_553,
            "upstream": {"repository": "https://github.com/shiyu-coder/Kronos", "commit": "67b630e67f6a"},
            "model": self.manifest["model"],
            "tokenizer": self.manifest["tokenizer"],
        }

    def close(self) -> None:
        self.closed_count += 1

    def forecast(self, instrument_key: str, candles: list[Candle], **kwargs: Any) -> Forecast:
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        last = candles[-1].close
        return Forecast(
            instrument_key=instrument_key,
            input_end=candles[-1].timestamp,
            generated_at=datetime.now(UTC),
            horizon=int(kwargs.get("horizon", 5)),
            mean_close=round(last * 1.012, 4),
            q10_close=round(last * 0.991, 4),
            q90_close=round(last * 1.031, 4),
            up_probability=0.68,
            artifact_digest="a" * 64,
            model_revision="901c26c1332695a2a8f243eb2f37243a37bea320",
            tokenizer_revision="0e0117387f39004a9016484a186a908917e22426",
            validated=False,
        )


def test_kronos_provider_six_states_validation_latency_and_restart_isolation(tmp_path: Path):
    cfg_file = tmp_path / "kronos-lab-config.json"

    # 1. NOT_INSTALLED
    p_missing = KronosForecastProvider(FakeKronosEngine(ready=False, self_tested=False), config_path=cfg_file)
    assert p_missing.status()["provider_state"] == "NOT_INSTALLED"
    p_missing.set_inference_enabled(True)
    with pytest.raises(KronosUnavailableError, match="NOT_INSTALLED"):
        p_missing.predict(market="NASDAQ", symbol="AAPL", timeframe="5m", bars=_make_bars(40))

    # 2. NOT_VERIFIED (weights exist, self-test not run)
    p_unver = KronosForecastProvider(FakeKronosEngine(ready=True, self_tested=False), config_path=cfg_file)
    assert p_unver.status()["provider_state"] == "NOT_VERIFIED"

    # 3. ERROR on corrupted weight SHA-256
    p_corrupt = KronosForecastProvider(FakeKronosEngine(ready=False, corrupt=True), config_path=cfg_file)
    assert p_corrupt.status()["provider_state"] == "ERROR"

    # 4. READY -> Load -> Predict -> p50/p95 Latency -> STALE transition
    now_holder = {"t": datetime(2026, 10, 29, 15, 0, tzinfo=UTC)}
    eng = FakeKronosEngine(ready=True, self_tested=True)
    prov = KronosForecastProvider(eng, config_path=cfg_file, clock=lambda: now_holder["t"])
    prov.set_inference_enabled(True)
    prov.load_model()
    st_ready = prov.status()
    assert st_ready["provider_state"] == "READY"
    assert st_ready["model_loaded"] is True

    bars = _make_bars(48)
    fc = prov.predict(market="NASDAQ", symbol="AAPL", timeframe="5m", bars=bars, horizon=5, samples=8)
    assert fc["is_forecast"] is True
    assert fc["trade_instruction"] is False
    assert fc["mock_outputs_used"] is False
    assert fc["freshness_state"] == "FRESH"
    diag = prov.status()["diagnostics"]
    assert diag["inference_count"] == 1
    assert diag["load_ms_p50"] == 42.5
    assert diag["inference_ms_p95"] == 118.4

    # Advance clock past max_forecast_age_seconds -> STALE
    now_holder["t"] = now_holder["t"] + timedelta(seconds=1200)
    assert prov.status()["provider_state"] == "STALE"
    cached = prov.get_cached_forecast("NASDAQ", "AAPL", "5m")
    assert cached is not None and cached["freshness_state"] == "STALE"

    # Restarting provider from persisted config NEVER auto-loads worker
    restarted = KronosForecastProvider(eng, config_path=cfg_file, clock=lambda: now_holder["t"])
    assert restarted.status()["model_loaded"] is False
    restarted.unload_model()
    assert eng.closed_count >= 1


def test_kronos_provider_rejects_bad_bars_and_enforces_timeout():
    eng = FakeKronosEngine(ready=True, self_tested=True)
    prov = KronosForecastProvider(eng)
    prov.set_inference_enabled(True)

    # Too few bars (<32)
    with pytest.raises(KronosInputError, match="At least 32"):
        prov.predict(market="NSE", symbol="RELIANCE", timeframe="5m", bars=_make_bars(16))

    # Unsupported market
    with pytest.raises(KronosInputError, match="Unsupported market"):
        prov.predict(market="FOREX", symbol="EURUSD", timeframe="5m", bars=_make_bars(40))

    # Non-monotonic / duplicate timestamps
    dup = _make_bars(36)
    dup[10] = Candle(
        timestamp=dup[9].timestamp,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1000,
        symbol="AAPL",
    )
    with pytest.raises(KronosInputError, match="strictly chronological"):
        prov.predict(market="NASDAQ", symbol="AAPL", timeframe="5m", bars=dup)

    # NaN / negative values
    bad_dicts = [
        {"timestamp": b.timestamp.isoformat(), "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
        for b in _make_bars(36)
    ]
    bad_dicts[5]["close"] = float("nan")
    with pytest.raises(KronosInputError, match="NaN"):
        prov.predict(market="NASDAQ", symbol="AAPL", timeframe="5m", bars=bad_dicts)

    # Worker timeout -> fail-closed ERROR state & worker closed
    slow_eng = FakeKronosEngine(ready=True, self_tested=True, delay_s=1.4)
    slow_prov = KronosForecastProvider(slow_eng)
    slow_prov.set_inference_enabled(True)
    slow_prov.update_config(timeout_seconds=1.0)
    with pytest.raises(KronosUnavailableError, match="timeout"):
        slow_prov.predict(market="NASDAQ", symbol="AAPL", timeframe="5m", bars=_make_bars(40))
    assert slow_prov.status()["provider_state"] == "ERROR"
    assert slow_eng.closed_count >= 1


def test_live_paper_auto_bar_sync_deduplication_and_fail_closed_kronos_strategy(tmp_path: Path):
    us_open = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)  # Thursday 11:00 ET (regular session OPEN)
    bars = _make_bars(48, start_price=195.0, step_pct=0.004)
    quote = {
        "symbol": "AAPL",
        "bid": 235.0,
        "ask": 235.10,
        "last": 235.05,
        "volume": 500_000,
        "timestamp": us_open.isoformat(),
        "source": "alpaca-test-live",
    }

    us_resolver = lambda s: {
        "symbol": s,
        "trading_symbol": s,
        "instrument_key": f"NASDAQ|{s}",
        "tick_size": 0.01,
        "lot_size": 1,
        "fractionable": True,
        "tradable": True,
    }

    # Case A: Kronos unavailable -> kronos_ensemble fails closed to HOLD (0 orders)
    unavail_prov = KronosForecastProvider(FakeKronosEngine(ready=False, self_tested=False), clock=lambda: us_open)
    eng_fail_closed = LiveMarketPaperEngine(
        tmp_path / "paper_fc.sqlite3",
        nasdaq_quote_provider=lambda _s: quote,
        nasdaq_bars_provider=lambda _s, _tf: bars,
        nasdaq_instrument_resolver=us_resolver,
        kronos_provider=unavail_prov,
        clock=lambda: us_open,
    )
    eng_fail_closed.start_session("NASDAQ", strategy="kronos_ensemble", timeframe="5m", symbols=["AAPL"])
    snap_fc = eng_fail_closed.sync_live_tick("NASDAQ")
    assert len(snap_fc["orders"]) == 0
    assert snap_fc["decisions"][0]["action"] == "HOLD"
    assert "fail-closed" in snap_fc["decisions"][0]["reason"]
    eng_fail_closed.close()

    # Case B: Kronos READY & enabled -> auto-fetches bars on sync_live_tick() without bars_by_symbol,
    # submits 1 simulated paper order, and deduplicates repeated syncs on the same bar_timestamp!
    ready_prov = KronosForecastProvider(FakeKronosEngine(ready=True, self_tested=True), clock=lambda: us_open)
    ready_prov.set_inference_enabled(True)
    eng_ready = LiveMarketPaperEngine(
        tmp_path / "paper_ready.sqlite3",
        nasdaq_quote_provider=lambda _s: quote,
        nasdaq_bars_provider=lambda _s, _tf: bars,
        nasdaq_instrument_resolver=us_resolver,
        kronos_provider=ready_prov,
        clock=lambda: us_open,
    )
    eng_ready.start_session("NASDAQ", strategy="kronos_ensemble", timeframe="5m", symbols=["AAPL"])
    snap1 = eng_ready.sync_live_tick("NASDAQ")
    assert snap1["simulated"] is True
    assert snap1["real_order_api_used"] is False
    assert len(snap1["orders"]) == 1
    assert snap1["orders"][0]["status"] == "FILLED"
    assert snap1["decisions"][0]["action"] == "BUY"

    # Calling sync_live_tick again on the exact same completed bar must NOT duplicate the order!
    snap2 = eng_ready.sync_live_tick("NASDAQ")
    assert len(snap2["orders"]) == 1
    assert len(snap2["decisions"]) == 1
    eng_ready.close()


def test_readiness_checklists_and_model_lab_http_isolation(tmp_path: Path):
    settings = replace(Settings(), api_token="lab-tok", database_url=f"sqlite:///{tmp_path}/lab.db")
    headers = {"Authorization": "Bearer lab-tok"}
    with TestClient(create_app(settings, data_dir=tmp_path)) as web:
        # 1. Readiness reports NOT_VERIFIED / BLOCKED before owner verifies credentials
        rd = web.get("/api/markets/readiness", headers=headers)
        assert rd.status_code == 200
        modes = rd.json()["modes"]
        assert modes["NSE_PAPER"]["owner_connection_verified"] is False
        assert modes["NASDAQ_PAPER"]["owner_connection_verified"] is False
        assert modes["NSE_LIVE"]["overall_status"] == "DISABLED"
        assert modes["NASDAQ_LIVE"]["overall_status"] == "DISABLED"

        # 2. Model Lab overview + toggling Kronos never touches live trading gates
        ml_ov = web.get("/api/model-lab/overview", headers=headers)
        assert ml_ov.status_code == 200
        assert ml_ov.json()["paper_and_research_only"] is True
        assert ml_ov.json()["live_execution_authorized"] is False

        tog = web.post("/api/model-lab/kronos/toggle", headers=headers, json={"enabled": True})
        assert tog.status_code == 200
        assert tog.json()["inference_enabled"] is True

        # Forecast fails closed (409) when Kronos weights/runtime are not installed in tmp_path
        fc = web.post(
            "/api/model-lab/kronos/forecast",
            headers=headers,
            json={"market": "NSE", "symbol": "DEMO_NSE_SYNTH", "timeframe": "1h", "dataset_id": "bundled_nse_synthetic_1h"},
        )
        assert fc.status_code == 409
        assert "NOT_INSTALLED" in fc.text or "NOT_VERIFIED" in fc.text

        # Live execution remains 100% OFF after Model Lab actions
        live_after = web.get("/api/markets/live/status", headers=headers).json()
        assert live_after["default_live_execution"] == "OFF"
        assert live_after["nse"]["availability"] == "NOT_AVAILABLE"
        assert live_after["nasdaq"]["availability"] == "NOT_AVAILABLE"
        assert evaluate_bar_strategy_signal("kronos_ensemble", _make_bars(30), kronos_forecast=None)["action"] == "HOLD"
