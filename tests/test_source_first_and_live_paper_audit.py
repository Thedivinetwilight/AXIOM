"""Comprehensive regression tests for AXIOM v3.7.0a10 audit fixes (Goals 1–6).

Covers:
1. Source-first local startup from repo root AND out-of-repo working directory
   without PYTHONPATH, dependency/tzdata preflight, loopback smoke check (0 broker
   calls), and existing %LOCALAPPDATA%\\AXIOM / private data preservation.
2. End-to-end NSE BOD reconciliation (`unexplained_nse_eq_row_gap == 0`), strict
   `CASH_EQUITY_OR_ETF` / `DERIVATIVE` / `INDEX` separation, BSE/NSE & multi-series
   disambiguation, and lossless `instrument_key` preservation through search,
   watchlist, quote, chart, and PAPER orders/positions/fills.
3. Deterministic read-only Upstox & Alpaca provider-fake verification across valid,
   missing, stale, future-skewed, out-of-order, malformed, closed-session,
   rate-limited (HTTP 429), `delayed_sip`, disconnected, and reconnect-recovery
   states with 0 broker order calls and 0 sample/replay fallback, plus honest
   `IEX` single-venue vs `SIP` consolidated US tape labeling.
4. Unambiguous 3-mode PAPER attribution, full session lifecycle (start -> fresh
   fills -> stale-feed pause -> reconnect -> market close -> stop -> SQLite restart
   recovery -> reconciliation -> SHA-256 audit export), and strict separation of
   current-session run P&L from persisted historical account P&L.
5. Strategy ensemble & Kronos audit: single `PortfolioRiskAllocator` non-bypass,
   `SYNTHETIC_ENGINEERING_EVIDENCE` labeling, causal regime-sliced baselines,
   `2.0x` worse-cost stress, and paired with/without-Kronos OOS evaluation.
6. Active multi-market runtime decoupling from legacy Vantage/MT5 modules via
   `app.security.dpapi_secrets`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from fastapi.testclient import TestClient
import pytest

from app.autonomy.upstox import InstrumentCatalog
from app.config.settings import Settings
from app.data.alpaca import AlpacaQuoteReader, feed_label
from app.data.models import Candle
from app.ensemble.service import StrategyEnsembleService
from app.main import create_app
from app.market_data.provider_verification import (
    verify_alpaca_market_data,
    verify_upstox_market_data,
)
from app.markets.live_paper import LiveMarketPaperEngine
from app.markets.nasdaq_catalog import NasdaqCatalog as NasdaqUniverseCatalog
from app.markets.nse_reconciliation import resolve_nse_instrument_strict
from app.source_launcher import (
    REPO_ROOT,
    _load_windows_launcher_module,
    check_runtime_dependencies,
    format_missing_dependencies_error,
    register_repo_in_active_site_packages,
)


# ---------------------------------------------------------------------------
# Goal 1: Source-first startup, dependency preflight, smoke & data safety
# ---------------------------------------------------------------------------


def test_source_launchers_work_from_out_of_repo_cwd_without_pythonpath(tmp_path: Path):
    outside_cwd = tmp_path / "outside_workdir"
    outside_cwd.mkdir(parents=True, exist_ok=True)
    clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    clean_env["AXIOM_PRIVATE_DATA_DIR"] = str(tmp_path / "priv_data")

    for cmd in (
        [sys.executable, str(REPO_ROOT / "axiom_launcher.py"), "--check-deps"],
        [sys.executable, str(REPO_ROOT / "packaging" / "windows" / "axiom_launcher.py"), "--check-deps"],
        [sys.executable, str(REPO_ROOT / "run.py"), "--mode", "operator", "--check-deps"],
    ):
        proc = subprocess.run(cmd, cwd=str(outside_cwd), env=clean_env, capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f"{cmd} failed: {proc.stderr}\n{proc.stdout}"
        payload = json.loads(proc.stdout)
        assert payload["ok"] is True
        assert payload["missing_packages"] == []

    # Verify site-packages .pth registration enables `python -m app operator` from outside repo
    fake_site = tmp_path / "fake_site"
    pth_file = register_repo_in_active_site_packages(site_dir=fake_site)
    assert pth_file.is_file()
    assert Path(pth_file.read_text(encoding="utf-8").strip()) == REPO_ROOT.resolve()

    # Register active venv temporarily and verify `python -m app operator --check-deps` from outside repo with NO PYTHONPATH!
    import sysconfig

    real_pth = Path(sysconfig.get_path("purelib")) / "axiom_source.pth"
    existed_before = real_pth.exists()
    prev_text = real_pth.read_text(encoding="utf-8") if existed_before else None
    try:
        register_repo_in_active_site_packages()
        mod_proc = subprocess.run(
            [sys.executable, "-m", "app", "operator", "--check-deps"],
            cwd=str(outside_cwd),
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert mod_proc.returncode == 0, mod_proc.stderr
        assert json.loads(mod_proc.stdout)["ok"] is True
    finally:
        if existed_before and prev_text is not None:
            real_pth.write_text(prev_text, encoding="utf-8")
        elif real_pth.exists():
            real_pth.unlink()


def test_source_dependency_preflight_reports_actionable_install_command_and_tzdata():
    from zoneinfo import ZoneInfoNotFoundError

    def fake_finder(mod_name: str) -> object | None:
        if mod_name in {"fastapi", "tzdata"}:
            return None
        return object()

    def fake_zone(tz_name: str) -> object:
        raise ZoneInfoNotFoundError(tz_name)

    rep = check_runtime_dependencies(
        platform_name="win32",
        spec_finder=fake_finder,
        zone_checker=fake_zone,
    )
    assert rep["ok"] is False
    assert "fastapi" in rep["missing_packages"]
    assert "tzdata" in rep["missing_packages"]
    msg = format_missing_dependencies_error(rep)
    assert "fastapi" in msg
    assert "tzdata" in msg
    assert "requirements-lock.txt" in msg
    assert "-m pip install -r" in msg


def test_source_smoke_check_preserves_existing_user_data_and_keeps_live_gates_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    priv = tmp_path / "existing_localappdata_axiom"
    (priv / "credentials").mkdir(parents=True)
    env_file = priv / "axiom.env"
    sentinel_env = "API_TOKEN=preserved-owner-token-1234567890abcdef\nCUSTOM_OWNER_NOTE=keep_me\n"
    env_file.write_text(sentinel_env, encoding="utf-8")
    cred_sentinel = priv / "credentials" / "upstox_access_token.dpapi"
    cred_sentinel.write_bytes(b"MOCK_ENCRYPTED_OWNER_BLOB_DO_NOT_TOUCH")

    monkeypatch.setenv("AXIOM_PRIVATE_DATA_DIR", str(priv))
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.delenv("AUTONOMY_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("ALPACA_BROKER_ENABLED", raising=False)

    env_snapshot = dict(os.environ)
    try:
        launcher_mod = _load_windows_launcher_module()
        cfg_path, created = launcher_mod.ensure_config(priv)
        assert cfg_path == env_file
        assert created is False
        cfg_vals = launcher_mod.load_configuration(priv)
        assert cfg_vals["created"] == "no"
        assert os.environ.get("API_TOKEN") == "preserved-owner-token-1234567890abcdef"
        assert env_file.read_text(encoding="utf-8") == sentinel_env
        assert cred_sentinel.read_bytes() == b"MOCK_ENCRYPTED_OWNER_BLOB_DO_NOT_TOUCH"
        assert os.environ.get("LIVE_TRADING") == "false"
    finally:
        os.environ.clear()
        os.environ.update(env_snapshot)

    outside_cwd = tmp_path / "smoke_outside"
    outside_cwd.mkdir()
    clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    clean_env["AXIOM_PRIVATE_DATA_DIR"] = str(priv)
    smoke_proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "axiom_launcher.py"), "--smoke"],
        cwd=str(outside_cwd),
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert smoke_proc.returncode == 0, f"smoke failed: {smoke_proc.stderr}\n{smoke_proc.stdout}"
    smoke_res = json.loads(smoke_proc.stdout)
    assert smoke_res["status"] == "PASS"
    assert smoke_res["broker_order_calls"] == 0
    assert smoke_res["user_data_modified"] is False
    assert smoke_res["checks"]["default_live_execution_off"] is True
    assert smoke_res["checks"]["nse_live_disarmed"] is True
    assert smoke_res["checks"]["nasdaq_live_disarmed"] is True
    # Confirm smoke check did NOT mutate existing user files
    assert env_file.read_text(encoding="utf-8") == sentinel_env
    assert cred_sentinel.read_bytes() == b"MOCK_ENCRYPTED_OWNER_BLOB_DO_NOT_TOUCH"


# ---------------------------------------------------------------------------
# Goal 2: End-to-end NSE BOD reconciliation & exact instrument_key preservation
# ---------------------------------------------------------------------------


def _sample_bod_records() -> list[dict[str, Any]]:
    # Note: Upstox BOD master encodes `tick_size` in paise (5.0 => ₹0.05, 1.0 => ₹0.01)
    # and requires positive `freeze_quantity`.
    return [
        # Valid primary cash equity
        {
            "segment": "NSE_EQ",
            "exchange": "NSE",
            "instrument_key": "NSE_EQ|INE002A01018",
            "isin": "INE002A01018",
            "trading_symbol": "RELIANCE",
            "name": "RELIANCE INDUSTRIES LTD",
            "instrument_type": "EQ",
            "lot_size": 1,
            "freeze_quantity": 100000.0,
            "tick_size": 5.0,
        },
        # Multi-series NSE cash row sharing trading_symbol RELIANCE with distinct ISIN/key
        {
            "segment": "NSE_EQ",
            "exchange": "NSE",
            "instrument_key": "NSE_EQ|INE002A01099",
            "isin": "INE002A01099",
            "trading_symbol": "RELIANCE",
            "name": "RELIANCE INDUSTRIES BLOCK SERIES",
            "instrument_type": "EQ",
            "lot_size": 1,
            "freeze_quantity": 100000.0,
            "tick_size": 5.0,
        },
        # Duplicate NSE_EQ instrument_key
        {
            "segment": "NSE_EQ",
            "exchange": "NSE",
            "instrument_key": "NSE_EQ|INE002A01018",
            "isin": "INE002A01018",
            "trading_symbol": "RELIANCE",
            "name": "RELIANCE DUPLICATE ROW",
            "instrument_type": "EQ",
            "lot_size": 1,
            "freeze_quantity": 100000.0,
            "tick_size": 5.0,
        },
        # Representative NSE equities & ETF
        {
            "segment": "NSE_EQ",
            "exchange": "NSE",
            "instrument_key": "NSE_EQ|INE467B01029",
            "isin": "INE467B01029",
            "trading_symbol": "TCS",
            "name": "TATA CONSULTANCY SERVICES LTD",
            "instrument_type": "EQ",
            "lot_size": 1,
            "freeze_quantity": 100000.0,
            "tick_size": 5.0,
        },
        {
            "segment": "NSE_EQ",
            "exchange": "NSE",
            "instrument_key": "NSE_EQ|INF204KB14I2",
            "isin": "INF204KB14I2",
            "trading_symbol": "NIFTYBEES",
            "name": "NIPPON INDIA ETF NIFTY 50 BEES",
            "instrument_type": "EQ",
            "lot_size": 1,
            "freeze_quantity": 100000.0,
            "tick_size": 1.0,
        },
        # Cross-exchange BSE duplicate (must NEVER mix into NSE cash search or NSE resolution)
        {
            "segment": "BSE_EQ",
            "exchange": "BSE",
            "instrument_key": "BSE_EQ|INE002A01018",
            "isin": "INE002A01018",
            "trading_symbol": "RELIANCE",
            "name": "RELIANCE BSE",
            "instrument_type": "A",
            "lot_size": 1,
            "freeze_quantity": 100000.0,
            "tick_size": 5.0,
        },
        # Derivative row (strictly separated as DERIVATIVE)
        {
            "segment": "NCD_FO",
            "exchange": "NSE",
            "instrument_key": "NCD_FO|54321",
            "trading_symbol": "USDINR26OCTFUT",
            "name": "USDINR FUT",
            "instrument_type": "FUT",
            "lot_size": 1,
            "freeze_quantity": 10000.0,
            "tick_size": 0.25,
            "qty_multiplier": 1000,
            "underlying_key": "NSE_INDEX|USDINR",
            "underlying_symbol": "USDINR",
            "expiry": 1793404800000,
        },
        # Watch-only Index row (strictly separated as INDEX)
        {
            "segment": "NSE_INDEX",
            "exchange": "NSE",
            "instrument_key": "NSE_INDEX|Nifty 50",
            "trading_symbol": "NIFTY 50",
            "name": "Nifty 50",
            "instrument_type": "INDEX",
            "lot_size": 1,
            "freeze_quantity": 100000.0,
            "tick_size": 5.0,
        },
        # Suspended NSE_EQ row
        {
            "segment": "NSE_EQ",
            "exchange": "NSE",
            "instrument_key": "NSE_EQ|INE999A01015",
            "isin": "INE999A01015",
            "trading_symbol": "SUSPCO",
            "name": "SUSPENDED CO LTD",
            "instrument_type": "EQ",
            "lot_size": 1,
            "freeze_quantity": 100000.0,
            "tick_size": 5.0,
        },
        # Invalid blank symbol NSE_EQ row
        {
            "segment": "NSE_EQ",
            "exchange": "NSE",
            "instrument_key": "NSE_EQ|INE888A01017",
            "isin": "INE888A01017",
            "trading_symbol": "   ",
            "name": "",
            "instrument_type": "EQ",
            "lot_size": 1,
            "freeze_quantity": 100000.0,
            "tick_size": 5.0,
        },
        # Invalid lot_size <= 0 NSE_EQ row
        {
            "segment": "NSE_EQ",
            "exchange": "NSE",
            "instrument_key": "NSE_EQ|INE777A01010",
            "isin": "INE777A01010",
            "trading_symbol": "BADLOT",
            "name": "ZERO LOT ROW",
            "instrument_type": "EQ",
            "lot_size": 0,
            "freeze_quantity": 100000.0,
            "tick_size": 5.0,
        },
        # Unsupported FUT type erroneously placed under NSE_EQ
        {
            "segment": "NSE_EQ",
            "exchange": "NSE",
            "instrument_key": "NSE_EQ|INE666A01012",
            "isin": "INE666A01012",
            "trading_symbol": "BADFUT",
            "name": "UNSUPPORTED FUT ON EQ",
            "instrument_type": "FUT",
            "lot_size": 1,
            "freeze_quantity": 100000.0,
            "tick_size": 5.0,
        },
    ]


def test_nse_bod_row_conservation_category_separation_and_lossless_instrument_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cat = InstrumentCatalog(Settings(), tmp_path / "bod_cache.json")
    cat._ingest_records(
        _sample_bod_records(),
        suspended_keys={"NSE_EQ|INE999A01015"},
        mis_keys={"NSE_EQ|INE002A01018", "NSE_EQ|INE467B01029"},
        digest="sha256:test-bod-master",
        source_kind="upstox_bod_complete",
    )

    rep = cat.reconciliation_report()
    nse_rec = rep["nse_cash_equity_reconciliation"]
    assert nse_rec["bod_nse_eq_source_rows"] == 9
    assert nse_rec["searchable_nse_eq_included"] == 4  # RELIANCE x2 keys, TCS, NIFTYBEES
    assert nse_rec["excluded_suspended_nse_eq"] == 1
    assert nse_rec["duplicate_nse_eq_keys_overwritten"] == 1
    assert nse_rec["excluded_invalid_nse_eq"] == 2  # blank symbol + lot_size=0
    assert nse_rec["excluded_unsupported_nse_eq"] == 1  # FUT on NSE_EQ
    assert nse_rec["unexplained_nse_eq_row_gap"] == 0
    assert nse_rec["reconciled_exact"] is True

    # Empty search + NSE_EQ scoping (never returns BSE_EQ or NSE_FO)
    empty_s = cat.search("", segment="NSE_EQ", include_indices=True)
    assert all(it["segment"] in {"NSE_EQ", "NSE_INDEX"} for it in empty_s["items"])
    rel_s = cat.search("RELIANCE", segment="NSE_EQ", include_indices=True)
    rel_keys = [it["instrument_key"] for it in rel_s["items"]]
    assert "NSE_EQ|INE002A01018" in rel_keys
    assert "NSE_EQ|INE002A01099" in rel_keys
    assert "BSE_EQ|INE002A01018" not in rel_keys
    assert all(it["asset_category"] == "CASH_EQUITY_OR_ETF" for it in rel_s["items"])

    idx_s = cat.search("NIFTY 50", segment="NSE_EQ", include_indices=True)
    idx_item = next(it for it in idx_s["items"] if it["instrument_key"] == "NSE_INDEX|Nifty 50")
    assert idx_item["asset_category"] == "INDEX"
    assert idx_item["tradable"] is False

    # Strict resolver rejects BSE, derivatives, and (for trading) indices
    with pytest.raises(ValueError, match="belongs to BSE_EQ"):
        resolve_nse_instrument_strict(cat, "BSE_EQ|INE002A01018", allow_index=False)
    with pytest.raises(ValueError, match="derivative contract"):
        resolve_nse_instrument_strict(cat, "NCD_FO|54321", allow_index=False)
    with pytest.raises(ValueError, match="watch-only"):
        resolve_nse_instrument_strict(cat, "NSE_INDEX|Nifty 50", allow_index=False)

    # Lossless exact instrument_key preservation for secondary series NSE_EQ|INE002A01099
    exact_alt = resolve_nse_instrument_strict(cat, "NSE_EQ|INE002A01099", allow_index=False)
    assert exact_alt["instrument_key"] == "NSE_EQ|INE002A01099"
    assert exact_alt["exchange"] == "NSE"

    # Verify lossless preservation through LiveMarketPaperEngine watchlist -> preview -> order -> fill -> position -> close
    now_open = datetime(2026, 10, 28, 5, 0, tzinfo=UTC)  # Wednesday 10:30 IST (NSE open)
    seen_quote_keys: list[str] = []

    def fake_nse_quote(sym_or_key: str) -> dict[str, Any]:
        seen_quote_keys.append(sym_or_key)
        return {
            "last": 1350.0 if "01099" in sym_or_key else 1400.0,
            "bid": 1349.9 if "01099" in sym_or_key else 1399.9,
            "ask": 1350.1 if "01099" in sym_or_key else 1400.1,
            "timestamp": (now_open - timedelta(seconds=2)).isoformat(),
            "local_received_at": now_open.isoformat(),
            "source": "fake_upstox_readonly",
            "feed_entitlement_label": "Upstox NSE Cash Read-Only Feed",
        }

    us_cat = NasdaqUniverseCatalog(cache_path=tmp_path / "us_cat.json")
    eng = LiveMarketPaperEngine(
        tmp_path / "paper_key.sqlite3",
        nse_quote_provider=fake_nse_quote,
        nse_instrument_resolver=lambda s: resolve_nse_instrument_strict(cat, s, allow_index=False),
        nasdaq_instrument_resolver=lambda s: us_cat.get(s).public() if us_cat.get(s) else None,
        clock=lambda: now_open,
    )
    wl = eng.add_watchlist_symbol("NSE", "NSE_EQ|INE002A01099")
    assert wl["instrument_key"] == "NSE_EQ|INE002A01099"
    assert any(w["instrument_key"] == "NSE_EQ|INE002A01099" for w in wl["watchlists"]["NSE"])

    prev = eng.preview_order(market="NSE", symbol="NSE_EQ|INE002A01099", side="BUY", qty=2, order_type="MARKET", product="D")
    assert prev["ok"] is True
    assert prev["instrument_key"] == "NSE_EQ|INE002A01099"
    assert prev["fill_type"] == "SIMULATED"
    assert "NSE_EQ|INE002A01099" in seen_quote_keys

    sub = eng.submit_order(market="NSE", symbol="NSE_EQ|INE002A01099", side="BUY", qty=2, order_type="MARKET", product="D")
    assert sub["ok"] is True
    assert sub["order"]["instrument_key"] == "NSE_EQ|INE002A01099"

    snap = eng.market_snapshot("NSE")
    pos = next(p for p in snap["positions"] if p["instrument_key"] == "NSE_EQ|INE002A01099")
    assert pos["symbol"] == "RELIANCE"
    assert snap["fills"][0]["instrument_key"] == "NSE_EQ|INE002A01099"
    assert snap["fills"][0]["fill_type"] == "SIMULATED"

    closed = eng.close_position("NSE", "NSE_EQ|INE002A01099")
    assert closed["ok"] is True
    assert eng.market_snapshot("NSE")["positions"] == []


# ---------------------------------------------------------------------------
# Goal 3: Upstox & Alpaca Live-Market PAPER fail-closed & entitlement checks
# ---------------------------------------------------------------------------


def test_alpaca_and_upstox_provider_fakes_cover_all_error_and_entitlement_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    now_us_open = datetime(2026, 10, 28, 14, 30, tzinfo=UTC)  # 10:30 EDT regular open

    # 1. Explicit IEX vs SIP vs delayed_sip labels
    assert "single-venue" in feed_label("iex").lower() or "not consolidated" in feed_label("iex").lower()
    assert "consolidated" in feed_label("sip").lower()
    assert "delayed" in feed_label("delayed_sip").lower()

    # 2. AlpacaQuoteReader rejects inverted ask < bid and non-finite quotes
    class FakeInvertedClient:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        def get(self, path: str, params: dict | None = None):
            _ = (path, params)
            return type(
                "FakeResp",
                (),
                {
                    "status_code": 200,
                    "raise_for_status": staticmethod(lambda: None),
                    "json": staticmethod(lambda: {"latestQuote": {"bp": 205.0, "ap": 204.0, "t": now_us_open.isoformat()}}),
                },
            )()

        def close(self) -> None:
            return None

    reader_inv = AlpacaQuoteReader("k", "s", feed="iex", client=FakeInvertedClient())
    with pytest.raises(ValueError, match="inverted"):
        reader_inv.latest_quote("AAPL")

    # 3. Alpaca HTTP 429 rate-limit classified cleanly as rate_limited (0 broker calls)
    class Fake429Reader:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            _ = (args, kwargs)

        def latest_quote(self, sym: str) -> dict:
            _ = sym
            raise ValueError("Alpaca market-data error (HTTP 429): too many requests")

        def close(self) -> None:
            return None

    monkeypatch.setattr("app.market_data.provider_verification.AlpacaQuoteReader", Fake429Reader)
    ver_429 = verify_alpaca_market_data("PKTEST", "SECTEST", feed="iex", symbol="AAPL", now=now_us_open)
    assert ver_429["verdict"] == "rate_limited"
    assert ver_429["connection_state"] == "UNAVAILABLE"
    assert ver_429["real_order_api_used"] is False

    # 4. Future-skewed Upstox quote rejected as future_skewed_quote
    class FakeFutureUpstox:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            _ = (args, kwargs)

        def profile(self, include_static_ips: bool = False):
            _ = include_static_ips
            return type(
                "Prof",
                (),
                {
                    "active": True,
                    "exchanges": {"NSE", "NSE_EQ"},
                    "products": {"D", "I"},
                    "user_fingerprint": "fp-test",
                },
            )()

        def quote_snapshot(self, key: str):
            return type(
                "Q",
                (),
                {
                    "instrument_key": key,
                    "bid": 1399.9,
                    "ask": 1400.1,
                    "last": 1400.0,
                    "volume": 1000.0,
                    "timestamp": now_us_open + timedelta(seconds=45),
                },
            )()

    monkeypatch.setattr("app.market_data.provider_verification.UpstoxReadOnly", FakeFutureUpstox)
    ver_skew = verify_upstox_market_data("dummy-token", instrument_key="NSE_EQ|INE002A01018", now=now_us_open)
    assert ver_skew["verdict"] == "malformed_future_timestamp"
    assert ver_skew["connection_state"] == "UNAVAILABLE"


def test_paper_engine_blocks_missing_stale_out_of_order_malformed_closed_delayed_ratelimited_and_recovers(
    tmp_path: Path,
):
    cur_now = datetime(2026, 10, 28, 14, 30, tzinfo=UTC)  # US regular open
    quote_mode = {"mode": "valid", "ts": cur_now - timedelta(seconds=3)}

    def fake_us_quote(sym: str) -> dict[str, Any] | None:
        _ = sym
        m = quote_mode["mode"]
        if m == "missing":
            return None
        if m == "rate_limit":
            raise RuntimeError("HTTP 429 rate limit exceeded")
        if m == "disconnect":
            raise ConnectionError("network unreachable")
        if m == "stale":
            # Quote timestamp >= watermark so it is NOT out-of-order, while local clock advanced +400s
            return {"last": 220.0, "bid": 219.9, "ask": 220.1, "timestamp": quote_mode["ts"].isoformat(), "feed": "iex", "source": "alpaca:iex"}
        if m == "malformed_nan":
            return {"last": float("nan"), "bid": 219.9, "ask": 220.1, "timestamp": quote_mode["ts"].isoformat(), "feed": "iex", "source": "alpaca:iex"}
        if m == "malformed_inverted":
            return {"last": 220.0, "bid": 221.0, "ask": 220.0, "timestamp": quote_mode["ts"].isoformat(), "feed": "iex", "source": "alpaca:iex"}
        if m == "delayed_sip":
            return {"last": 220.0, "bid": 219.9, "ask": 220.1, "timestamp": quote_mode["ts"].isoformat(), "feed": "delayed_sip", "source": "alpaca:delayed_sip"}
        return {
            "last": 220.0,
            "bid": 219.95,
            "ask": 220.05,
            "timestamp": quote_mode["ts"].isoformat(),
            "local_received_at": cur_now.isoformat(),
            "feed": "iex",
            "feed_entitlement_label": feed_label("iex"),
            "source": "alpaca:iex",
        }

    us_cat2 = NasdaqUniverseCatalog(cache_path=tmp_path / "us_cat2.json")
    eng = LiveMarketPaperEngine(
        tmp_path / "us_guard.sqlite3",
        nasdaq_quote_provider=lambda s: fake_us_quote(s),
        nasdaq_instrument_resolver=lambda s: us_cat2.get(s).public() if us_cat2.get(s) else None,
        clock=lambda: cur_now,
    )
    eng.add_watchlist_symbol("NASDAQ", "AAPL")

    # (a) Valid quote on order submission establishes monotonic watermark and passes preview
    sub_init = eng.submit_order(market="NASDAQ", symbol="AAPL", side="BUY", qty=1, order_type="MARKET")
    assert sub_init["ok"] is True

    # (b) Out-of-order older timestamp rejected against watermark
    quote_mode["ts"] = cur_now - timedelta(seconds=20)
    p_ooo = eng.preview_order(market="NASDAQ", symbol="AAPL", side="BUY", qty=1, order_type="MARKET")
    assert p_ooo["ok"] is False
    assert any("out-of-order" in r.lower() for r in p_ooo["rejection_reasons"])
    quote_mode["ts"] = cur_now - timedelta(seconds=1)

    # (c) Missing, stale, malformed NaN, inverted ask<bid, delayed_sip, rate_limit all block with exact reasons
    for bad_mode, expected_substr in (
        ("missing", "missing"),
        ("stale", "stale"),
        ("malformed_nan", "malformed"),
        ("malformed_inverted", "malformed"),
        ("delayed_sip", "delayed_sip"),
        ("rate_limit", "rate"),
    ):
        quote_mode["mode"] = bad_mode
        if bad_mode == "stale":
            saved_now = cur_now
            cur_now = cur_now + timedelta(seconds=400)
            res = eng.preview_order(market="NASDAQ", symbol="AAPL", side="BUY", qty=1, order_type="MARKET")
            cur_now = saved_now
        else:
            res = eng.preview_order(market="NASDAQ", symbol="AAPL", side="BUY", qty=1, order_type="MARKET")
        assert res["ok"] is False, f"mode {bad_mode} unexpectedly succeeded"
        assert any(expected_substr in r.lower() for r in res["rejection_reasons"]), (bad_mode, res["rejection_reasons"])

    # (d) Closed session blocks market orders even when quote is fresh
    quote_mode["mode"] = "valid"
    cur_now = datetime(2026, 10, 31, 14, 30, tzinfo=UTC)  # Saturday closed
    quote_mode["ts"] = cur_now - timedelta(seconds=1)
    p_closed = eng.preview_order(market="NASDAQ", symbol="AAPL", side="BUY", qty=1, order_type="MARKET")
    assert p_closed["ok"] is False
    assert any("closed" in r.lower() for r in p_closed["rejection_reasons"])

    # (e) Reconnect recovery: open session -> disconnect pauses -> fresh quote auto-recovers to RUNNING
    cur_now = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # Monday regular open (10:00 EST)
    quote_mode["ts"] = cur_now - timedelta(seconds=1)
    eng.update_config("NASDAQ", strategy="ema_crossover")
    eng.transition_session("NASDAQ", "start")
    assert eng.market_snapshot("NASDAQ")["session_state"] == "RUNNING"

    quote_mode["mode"] = "disconnect"
    sync_disc = eng.sync_live_tick("NASDAQ")
    assert sync_disc["session_state"] == "DISCONNECTED_PAUSED"

    quote_mode["mode"] = "valid"
    quote_mode["ts"] = cur_now
    sync_rec = eng.sync_live_tick("NASDAQ")
    assert sync_rec["session_state"] == "RUNNING"
    assert any(ev["event_kind"] == "PAPER_SESSION_RECONNECTED" for ev in eng.audit_log(market="NASDAQ", limit=10))


# ---------------------------------------------------------------------------
# Goal 4: 3 PAPER modes, session lifecycle & current-session vs historical P&L
# ---------------------------------------------------------------------------


def test_session_lifecycle_separates_current_session_pnl_from_persisted_historical_pnl_and_exports_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    priv = tmp_path / "priv_modes"
    monkeypatch.setenv("AXIOM_PRIVATE_DATA_DIR", str(priv))
    cur_now = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # Monday US open
    price_box = {"px": 100.0}

    def fake_quote(sym: str) -> dict[str, Any]:
        _ = sym
        px = price_box["px"]
        return {
            "last": px,
            "bid": px - 0.02,
            "ask": px + 0.02,
            "timestamp": cur_now.isoformat(),
            "local_received_at": cur_now.isoformat(),
            "feed": "iex",
            "feed_entitlement_label": feed_label("iex"),
            "source": "alpaca:iex",
        }

    db_file = tmp_path / "lifecycle.sqlite3"
    us_cat = NasdaqUniverseCatalog(cache_path=tmp_path / "us_cat3.json")
    us_res = lambda s: us_cat.get(s).public() if us_cat.get(s) else None
    eng1 = LiveMarketPaperEngine(db_file, nasdaq_quote_provider=fake_quote, nasdaq_instrument_resolver=us_res, clock=lambda: cur_now)

    # Run 1: earn ~+$100 realized profit, then stop session
    eng1.transition_session("NASDAQ", "start")
    assert eng1.submit_order(market="NASDAQ", symbol="AAPL", side="BUY", qty=10, order_type="MARKET")["ok"] is True
    cur_now = cur_now + timedelta(seconds=10)
    price_box["px"] = 112.0
    assert eng1.submit_order(market="NASDAQ", symbol="AAPL", side="SELL", qty=10, order_type="MARKET")["ok"] is True

    snap_run1 = eng1.market_snapshot("NASDAQ")
    hist_pnl_after_run1 = snap_run1["account"]["persisted_historical_realized_pnl"]
    assert hist_pnl_after_run1 > 100.0
    assert math.isclose(snap_run1["account"]["current_session_realized_pnl"], hist_pnl_after_run1, rel_tol=1e-6)
    eng1.transition_session("NASDAQ", "stop")

    # Stopped snapshot: current_session_realized_pnl MUST reset to 0 while persisted history remains intact
    snap_stopped = eng1.market_snapshot("NASDAQ")
    assert snap_stopped["account"]["current_session_run_id"] is None
    assert snap_stopped["account"]["current_session_realized_pnl"] == 0.0
    assert snap_stopped["account"]["current_session_total_pnl"] == 0.0
    assert math.isclose(snap_stopped["account"]["persisted_historical_realized_pnl"], hist_pnl_after_run1, rel_tol=1e-6)

    # Start Run 2 & simulate crash restart -> recovers to RECOVERED_PAUSED and still isolates Run 2 P&L == 0.0
    cur_now = cur_now + timedelta(seconds=10)
    eng1.transition_session("NASDAQ", "start")
    snap_run2 = eng1.market_snapshot("NASDAQ")
    assert snap_run2["account"]["current_session_run_id"] is not None
    assert snap_run2["account"]["current_session_realized_pnl"] == 0.0
    assert snap_run2["account"]["current_session_total_pnl"] == 0.0
    assert math.isclose(snap_run2["account"]["persisted_historical_realized_pnl"], hist_pnl_after_run1, rel_tol=1e-6)

    eng2 = LiveMarketPaperEngine(db_file, nasdaq_quote_provider=fake_quote, nasdaq_instrument_resolver=us_res, clock=lambda: cur_now)
    assert eng2.market_snapshot("NASDAQ")["session_state"] == "RECOVERED_PAUSED"
    rec = eng2.reconcile("NASDAQ")
    assert rec["audit_chain_valid"] is True

    bundle = eng2.export_audit_bundle(market="NASDAQ", limit=100)
    assert len(bundle["bundle_sha256"]) == 64
    assert bundle["reconciliation"]["NASDAQ"]["audit_chain_valid"] is True

    # Verify HTTP /api/markets/paper-modes and /api/markets/paper/audit/export
    app = create_app(Settings(api_token="tok-modes", database_url=f"sqlite:///{tmp_path / 'app.db'}"))
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer tok-modes"}
        modes_resp = client.get("/api/markets/paper-modes", headers=headers)
        assert modes_resp.status_code == 200
        mode_ids = [m["mode_id"] for m in modes_resp.json()["modes"]]
        assert mode_ids == ["LOCAL_REPLAY_PAPER", "LIVE_MARKET_LOCAL_PAPER", "ALPACA_BROKER_PAPER"]
        exp_resp = client.get("/api/markets/paper/audit/export?market=NSE", headers=headers)
        assert exp_resp.status_code == 200
        assert len(exp_resp.json()["bundle_sha256"]) == 64


# ---------------------------------------------------------------------------
# Goals 5 & 6: Strategy/Kronos synthetic-evidence audit & Vantage/MT5 decoupling
# ---------------------------------------------------------------------------


def test_ensemble_synthetic_evidence_regime_slices_cost_stress_and_kronos_paired_ablation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AXIOM_PRIVATE_DATA_DIR", str(tmp_path / "ens_priv"))
    app = create_app(Settings(api_token="tok-ens", database_url=f"sqlite:///{tmp_path / 'ens_app.db'}"))
    with TestClient(app) as client:
        _ = client
        svc: StrategyEnsembleService = app.state.ensemble_service
        # 1. Offline run without Kronos weights -> NOT VERIFIED & SYNTHETIC_ENGINEERING_EVIDENCE
        abl_off = svc.run_ablation_study(market="NASDAQ", dataset_id="bundled_nasdaq_synthetic_1h", timeframe="1h")
        assert abl_off["evidence_classification"]["evidence_type"] == "SYNTHETIC_ENGINEERING_EVIDENCE"
        assert abl_off["evidence_classification"]["expected_returns_claim"] is False
        assert abl_off["methodology_audit_disclosures"]["single_allocator_enforced"] is True
        assert abl_off["methodology_audit_disclosures"]["direct_strategy_order_submission_allowed"] is False
        assert abl_off["kronos_ablation_summary"]["evaluated"] is False
        assert "NOT VERIFIED" in abl_off["kronos_ablation_summary"]["verdict"]

        by_id = {c["candidate_id"]: c for c in abl_off["comparisons"]}
        for cid in ("buy_and_hold", "baseline_moving_average", "baseline_rsi", "ensemble_no_kronos"):
            assert "regime_breakdown" in by_id[cid]["untouched_test_1x"]
            assert "regime_breakdown" in by_id[cid]["stressed_test_2x"]

        # 2. Paired synthetic Kronos probe -> computes paired delta while STILL refusing to claim real-market value
        def deterministic_advisory_probe(bars_prefix: list[Candle]) -> dict[str, Any]:
            last_c = float(bars_prefix[-1].close)
            prev_c = float(bars_prefix[-5].close) if len(bars_prefix) >= 5 else last_c
            drift = 0.004 if last_c >= prev_c else -0.004
            return {
                "advisory_only": True,
                "mock_outputs_used": False,
                "freshness_state": "FRESH",
                "expected_return_bps": drift * 10_000.0,
                "predicted_close": last_c * (1.0 + drift),
                "confidence": 0.65,
            }

        abl_paired = svc.run_ablation_study(
            market="NASDAQ",
            dataset_id="bundled_nasdaq_synthetic_1h",
            timeframe="1h",
            kronos_bar_forecast_fn=deterministic_advisory_probe,
        )
        ks = abl_paired["kronos_ablation_summary"]
        assert ks["evaluated"] is True
        assert ks["kronos_adds_measurable_real_market_oos_value"] is False
        assert "synthetic" in ks["verdict"].lower()


def test_active_multimarket_packages_have_zero_vantage_or_mt5_imports():
    clean_dirs = (
        REPO_ROOT / "app" / "markets",
        REPO_ROOT / "app" / "autonomy",
        REPO_ROOT / "app" / "alpaca_broker",
        REPO_ROOT / "app" / "ensemble",
        REPO_ROOT / "app" / "model_lab",
        REPO_ROOT / "app" / "security",
    )
    offenders: list[str] = []
    for d in clean_dirs:
        for py in d.rglob("*.py"):
            txt = py.read_text(encoding="utf-8").lower()
            for term in ("vantage", "mt5", "metatrader"):
                if term in txt:
                    offenders.append(f"{py.relative_to(REPO_ROOT)}:{term}")
    assert offenders == []
    assert (REPO_ROOT / "docs" / "VANTAGE_MT5_ISOLATION_AUDIT.md").is_file()
