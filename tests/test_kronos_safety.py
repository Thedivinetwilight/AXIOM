"""Safety boundaries: Kronos forecasts, Arena trades, and nothing in between.

These are the guarantees the integration must never lose:

* AXIOM/Kronos never imports Arena (``app``) — the subsystems stay separable;
* Kronos has no path to place an order, change a risk limit or enable LIVE;
* forecast values can never be presented as observed market data;
* no market data is fabricated anywhere in the market layer;
* the base AXIOM runtime stays lightweight (no torch/pandas/Qt imports).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from axiom.kronos.backends import StubKronosBackend, UpstreamKronosBackend
from axiom.market.provider import MarketDataBar
from axiom.models import FORECAST_ORIGIN, MARKET_ORIGIN, ForecastCandle
from axiom.worker.protocol import REQUEST_KINDS

ROOT = Path(__file__).resolve().parents[1]
AXIOM_SOURCES = sorted((ROOT / "axiom").rglob("*.py"))
DESKTOP_FORECAST_SOURCES = [
    ROOT / "desktop" / "forecast_controller.py",
    ROOT / "desktop" / "forecast_viewmodels.py",
]

FORBIDDEN_IMPORTS = {"app", "app.api", "app.risk", "app.engine", "app.paper_trading",
                     "app.brokers", "app.database"}
TRADING_TOKENS = (
    "place_order", "submit_order", "send_order", "emergency_stop", "/engine/stop",
    "/engine/resume", "UPSTOX_LIVE", "enable_live", "set_risk_limit",
    "risk_limits", "PaperBroker", "ExecutionMode",
)


def module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class TestArenaIsolation:
    def test_axiom_never_imports_arena(self):
        offenders = {}
        for path in AXIOM_SOURCES:
            imported = module_names(path)
            bad = {name for name in imported
                   if name == "app" or name.startswith("app.")}
            if bad:
                offenders[str(path.relative_to(ROOT))] = sorted(bad)
        assert offenders == {}, f"axiom must not import Arena: {offenders}"

    def test_desktop_forecast_code_does_not_touch_arena_writes(self):
        for path in DESKTOP_FORECAST_SOURCES:
            source = path.read_text(encoding="utf-8")
            assert "ArenaApiClient" not in source
            for token in ("emergency_stop", "/engine/stop", "resume("):
                assert token not in source, f"{path.name} references {token}"

    def test_no_module_references_the_trading_surface(self):
        for path in AXIOM_SOURCES + DESKTOP_FORECAST_SOURCES:
            source = path.read_text(encoding="utf-8")
            for token in TRADING_TOKENS:
                assert token not in source, f"{path.name} contains {token!r}"

    def test_axiom_has_no_order_or_risk_api(self):
        from axiom.service import ForecastService
        from axiom.worker.manager import KronosManager

        forbidden = {"order", "orders", "trade", "trades", "risk", "live",
                     "position", "positions", "broker", "execution"}
        for cls in (ForecastService, KronosManager):
            for name in dir(cls):
                if name.startswith("__"):
                    continue
                words = set(name.lower().split("_"))
                overlap = words & forbidden
                assert not overlap, (
                    f"{cls.__name__}.{name} exposes a trading API: {overlap}")

    def test_the_ipc_surface_is_read_only_inference(self):
        assert REQUEST_KINDS == {"ping", "load", "status", "forecast", "shutdown"}

    def test_no_database_or_network_writes_from_kronos(self):
        for path in AXIOM_SOURCES:
            if path.name in {"http.py"}:  # the read-only market data provider
                continue
            imported = module_names(path)
            assert "sqlite3" not in imported, f"{path.name} opens a database"
            assert "app.database" not in imported


class TestForecastProvenance:
    def test_forecast_and_observation_types_are_distinct(self):
        candle = ForecastCandle(datetime(2024, 1, 1, tzinfo=timezone.utc), 1, 2, 0.5,
                                1.5, 10)
        bar = MarketDataBar(datetime(2024, 1, 1, tzinfo=timezone.utc), 1, 2, 0.5, 1.5,
                            10, "DEMO")
        assert not isinstance(candle, MarketDataBar)
        assert not isinstance(bar, ForecastCandle)
        assert candle.origin == FORECAST_ORIGIN
        assert bar.origin == MARKET_ORIGIN
        assert candle.is_forecast is True and bar.is_forecast is False

    def test_forecast_fields_are_prefixed(self):
        candle = ForecastCandle(datetime(2024, 1, 1, tzinfo=timezone.utc), 1, 2, 0.5,
                                1.5, 10)
        payload = candle.to_dict()
        for field in ("forecast_open", "forecast_high", "forecast_low",
                      "forecast_close", "forecast_volume"):
            assert field in payload
        for forbidden in ("open", "high", "low", "close", "volume"):
            assert forbidden not in payload

    def test_stub_backend_is_explicitly_labelled(self):
        from axiom.kronos.runtime import KronosRuntime
        from axiom.models import KronosConfig

        runtime = KronosRuntime(KronosConfig(enabled=True, backend="stub"))
        status = runtime.load()
        info = runtime.info
        assert status.backend == "stub"
        assert info.backend == "stub"
        assert info.model_id.startswith("stub:")
        assert any("STUB backend" in note for note in info.notes)

    def test_stub_and_upstream_backends_are_distinguishable(self):
        assert StubKronosBackend.kind == "stub"
        assert UpstreamKronosBackend.kind == "upstream"

    def test_a_forecast_cannot_disguise_its_origin(self):
        """Provenance is ENFORCED, not merely defaulted.

        The other tests in this class check that a well-formed object carries the
        right origin. This one checks the guard in ``__post_init__`` actually
        rejects a wrong one — without it, a caller could relabel model output as
        an observation and every downstream "is this real data?" check would lie.
        """
        from axiom.models import (DataProvenance, ForecastCandle, ForecastSnapshot,
                                  KronosConfig)

        stamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for bad in ("OBSERVED", "MARKET_DATA", "", "kronos_forecast", None):
            with pytest.raises(ValueError, match="origin must be"):
                ForecastCandle(stamp, 1.0, 2.0, 0.5, 1.5, 10.0, origin=bad)

        config = KronosConfig(enabled=True, backend="stub", pred_len=1)
        candle = ForecastCandle(stamp, 1.0, 2.0, 0.5, 1.5, 10.0)
        provenance = DataProvenance(source="test", provider="replay",
                                    instrument="DEMO", timeframe="1h", bars=1)
        with pytest.raises(ValueError, match="origin must be"):
            ForecastSnapshot(
                instrument="DEMO", timeframe="1h", generated_at=stamp,
                candles=(candle,), config=config, provenance=provenance,
                origin="OBSERVED",
            )

    def test_serialized_forecasts_keep_their_origin(self):
        """to_dict carries provenance, and there is no from_dict to drop it."""
        from axiom.models import (DataProvenance, ForecastCandle, ForecastSnapshot,
                                  KronosConfig)

        stamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
        candle = ForecastCandle(stamp, 1.0, 2.0, 0.5, 1.5, 10.0)
        assert candle.to_dict()["origin"] == FORECAST_ORIGIN

        snapshot = ForecastSnapshot(
            instrument="DEMO", timeframe="1h", generated_at=stamp,
            candles=(candle,),
            config=KronosConfig(enabled=True, backend="stub", pred_len=1),
            provenance=DataProvenance(source="test", provider="replay",
                                      instrument="DEMO", timeframe="1h", bars=1),
        )
        assert snapshot.to_dict()["origin"] == FORECAST_ORIGIN
        # No deserialization path exists, so no round-trip can silently strip it.
        assert not hasattr(ForecastSnapshot, "from_dict")


class TestNoFabricatedData:
    def test_market_layer_contains_no_random_generation(self):
        """No generator anywhere in the market layer (checked in the AST, so
        honest prose in docstrings is not a false positive)."""
        for path in sorted((ROOT / "axiom" / "market").rglob("*.py")):
            imported = module_names(path)
            for forbidden in ("random", "numpy", "pandas", "torch"):
                assert forbidden not in imported, (
                    f"{path.name} imports {forbidden} — it must not generate data")
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    for forbidden in ("generate", "simulate", "synthesize",
                                      "fabricate"):
                        assert forbidden not in node.name.lower(), (
                            f"{path.name}.{node.name} looks like a data generator")

    def test_replay_provider_only_serves_recorded_bars(self):
        from axiom.market.replay import ReplayMarketDataProvider

        provider = ReplayMarketDataProvider.from_csv("data/sample_1h.csv", "1h")
        recorded = provider.fetch("DEMO", "1h").bars
        assert len(recorded) == 1600
        # The provider cannot invent a series it was never given.
        with pytest.raises(Exception):
            provider.fetch("INVENTED", "1h")

    def test_future_timestamps_come_from_the_calendar_not_the_model(self):
        from axiom.market.calendar import ContinuousCalendar

        calendar = ContinuousCalendar()
        last = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
        stamps = calendar.project_future_timestamps(last, "1h", 5)
        assert stamps == [last + timedelta(hours=n) for n in range(1, 6)]
        assert all(stamp > last for stamp in stamps)

    def test_stub_backend_output_is_marked_in_the_snapshot(self):
        from axiom.forecast.pipeline import ForecastPipeline
        from axiom.forecast.store import ForecastStore
        from axiom.kronos.runtime import KronosRuntime
        from axiom.market.replay import ReplayMarketDataProvider
        from axiom.models import KronosConfig

        cfg = KronosConfig(enabled=True, backend="stub", lookback=120, pred_len=6,
                           min_history=32, sample_paths=1)
        runtime = KronosRuntime(cfg)
        runtime.load()
        provider = ReplayMarketDataProvider.from_csv("data/sample_1h.csv", "1h")
        bars = provider.fetch("DEMO", "1h").bars
        pipeline = ForecastPipeline(
            provider, cfg, store=ForecastStore(), runtime=runtime,
            clock=lambda: bars[-1].timestamp + timedelta(hours=1))
        snapshot = pipeline.run("DEMO", "1h").snapshot
        assert snapshot.backend == "stub"
        assert snapshot.origin == FORECAST_ORIGIN
        assert "MODEL FORECAST" in snapshot.note


class TestLightweightBaseRuntime:
    def test_importing_axiom_pulls_in_no_heavy_dependencies(self):
        code = (
            "import sys, axiom, axiom.models, axiom.market, axiom.forecast, "
            "axiom.kronos, axiom.worker, axiom.research, axiom.service, "
            "axiom.bootstrap;"
            "heavy = sorted(m for m in ('torch','pandas','numpy','PySide6','einops',"
            "'safetensors','huggingface_hub') if m in sys.modules);"
            "print('HEAVY:' + ','.join(heavy))"
        )
        result = subprocess.run(  # noqa: S603 - fixed interpreter
            [sys.executable, "-c", code], capture_output=True, text=True,
            cwd=str(ROOT), timeout=180)
        assert result.returncode == 0, result.stderr[-2000:]
        heavy = result.stdout.strip().splitlines()[-1]
        assert heavy == "HEAVY:", f"base AXIOM runtime pulled in heavy deps: {heavy}"

    def test_base_requirements_stay_free_of_the_kronos_stack(self):
        base = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        for package in ("torch", "pandas", "numpy", "pyside6", "einops",
                        "safetensors", "huggingface"):
            assert package not in base, f"{package} leaked into requirements.txt"
