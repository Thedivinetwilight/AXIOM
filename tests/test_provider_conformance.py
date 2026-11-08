"""Conformance tests for the Upstox and Alpaca market-data adapters.

Everything here is offline: deterministic HTTP doubles, official-schema protobuf
frames and synthetic fixtures. These tests do not prove provider connectivity,
production data quality or anything about profitability — the report they check
says so explicitly (``live_provider_connectivity: UNVERIFIED``).

The suite under test lives in :mod:`app.data.conformance` and is the same code
the operator CLI ``python -m app provider-check`` runs.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.data.conformance import (
    ALPACA_SYMBOL,
    CONFORMANCE_SCHEMA,
    DATA_SOURCE_VERSION,
    NSE_NOW,
    SUITE_VERSION,
    SYNTHETIC_ALPACA_KEY,
    SYNTHETIC_ALPACA_SECRET,
    SYNTHETIC_UPSTOX_TOKEN,
    UPSTOX_KEY,
    UPSTOX_SYMBOL,
    UPSTOX_TRADING_SYMBOL,
    US_NOW,
    _resolve_nasdaq,
    _resolve_nse,
    alpaca_snapshot_payload,
    assert_no_order_endpoints,
    contains_secret,
    live_probe,
    redact_payload,
    render_text,
    run_conformance,
    synthetic_minute_bars,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

UTC = timezone.utc


@pytest.fixture(scope="module")
def report() -> dict:
    return run_conformance()


def _checks(payload: dict, provider: str) -> dict[str, dict]:
    return {check["id"]: check for check in payload["providers"][provider]["checks"]}


# --------------------------------------------------------------------------- #
# Report contract
# --------------------------------------------------------------------------- #
def test_report_shape_and_versions(report: dict):
    assert report["schema"] == CONFORMANCE_SCHEMA
    assert report["suite_version"] == SUITE_VERSION
    assert report["data_source_version"] == DATA_SOURCE_VERSION
    assert report["status"] == "PASS"
    assert set(report["providers"]) == {"upstox", "alpaca"}
    assert report["network_calls"] == 0
    assert report["broker_order_api_calls"] == 0
    assert report["live_trading"] is False
    assert report["mode"] == "PAPER"
    assert isinstance(report["generated_at"], str)
    for provider in report["providers"].values():
        assert provider["status"] == "PASS"
        assert provider["checks"], "a provider reported no checks"
        for check in provider["checks"]:
            assert check["status"] in {"PASS", "FAIL", "UNVERIFIED"}
            assert check["capability"] and check["detail"]


def test_capabilities_cover_the_requested_scope(report: dict):
    upstox = set(report["providers"]["upstox"]["capabilities"])
    alpaca = set(report["providers"]["alpaca"]["capabilities"])
    required = {"candles", "error_handling", "freshness", "paper_path"}
    assert required <= upstox
    assert required <= alpaca
    assert "instrument_resolution" in upstox
    assert "feed_identity" in alpaca
    assert "historical_bars" in alpaca
    cross = {check["capability"] for check in report["cross_market"]["checks"]}
    assert {"cross_market_isolation", "corporate_actions"} <= cross


def test_report_is_deterministic():
    first = run_conformance()
    second = run_conformance()
    assert first["status"] == second["status"] == "PASS"
    for name in ("upstox", "alpaca"):
        assert [(c["id"], c["status"]) for c in first["providers"][name]["checks"]] == [
            (c["id"], c["status"]) for c in second["providers"][name]["checks"]
        ]


def test_report_states_its_own_limitations(report: dict):
    text = " ".join(report["limitations"]).lower()
    assert "no upstox or alpaca endpoint was contacted" in text
    assert "synthetic" in text
    assert "profitability" in text
    assert report["unverified"][0]["capability"] == "live_provider_connectivity"
    assert "provider-check --live" in report["unverified"][0]["reason"]


# --------------------------------------------------------------------------- #
# Secret hygiene
# --------------------------------------------------------------------------- #
def test_report_contains_no_synthetic_secret(report: dict):
    rendered = json.dumps(report)
    assert report["redaction"]["secrets_found"] is False
    for secret in (SYNTHETIC_UPSTOX_TOKEN, SYNTHETIC_ALPACA_KEY, SYNTHETIC_ALPACA_SECRET):
        assert secret not in rendered
    # The single-use authorize URL (wss://…?code=…) is never echoed, and the
    # redaction field list itself is the only place the key name appears.
    assert "wss://" not in rendered
    assert "authorized_redirect_uri" in report["redaction"]["fields"]


def test_redaction_and_detection_helpers():
    payload = {
        "token": "abc",
        "nested": [{"api_secret": "def", "keep": "visible"}],
        "account_id": "ZZ123",
    }
    redacted = redact_payload(payload)
    assert redacted["token"] == "[REDACTED]"
    assert redacted["nested"][0]["api_secret"] == "[REDACTED]"
    assert redacted["nested"][0]["keep"] == "visible"
    assert redacted["account_id"] == "[REDACTED]"
    assert contains_secret({"x": "abc"}, ("abc",)) is True
    assert contains_secret({"x": "zzz"}, ("abc",)) is False


def test_rendered_text_is_shareable(report: dict):
    text = render_text(report)
    assert report["status"] in text
    assert "Limitations:" in text
    for secret in (SYNTHETIC_UPSTOX_TOKEN, SYNTHETIC_ALPACA_KEY, SYNTHETIC_ALPACA_SECRET):
        assert secret not in text


# --------------------------------------------------------------------------- #
# Upstox: parsing, timezones, ordering, gaps, staleness, errors
# --------------------------------------------------------------------------- #
def test_upstox_instrument_key_resolution(report: dict):
    check = _checks(report, "upstox")["upstox.instrument_key_resolution"]
    assert check["status"] == "PASS"
    assert "refused 4" in check["detail"]


def test_upstox_candle_is_timezone_aware_and_ordered(report: dict):
    for check_id in ("upstox.tick_normalization", "upstox.interval_bar_normalization"):
        assert _checks(report, "upstox")[check_id]["status"] == "PASS"
    detail = _checks(report, "upstox")["upstox.tick_normalization"]["detail"]
    assert "+00:00" in detail  # UTC, never a naive timestamp


def test_upstox_duplicates_and_out_of_order_are_counted(report: dict):
    check = _checks(report, "upstox")["upstox.duplicates_and_ordering"]
    assert checkstatus(check) == "PASS"
    assert "duplicates=2" in check["detail"]
    assert "regressions=0" in check["detail"]


def test_upstox_missing_bars_are_counted_not_filled(report: dict):
    check = _checks(report, "upstox")["upstox.missing_bars_counted_not_filled"]
    assert checkstatus(check) == "PASS"
    assert "gaps=1" in check["detail"]
    assert "candles=3" in check["detail"]


def test_upstox_malformed_frames_fail_closed(report: dict):
    check = _checks(report, "upstox")["upstox.malformed_frames_fail_closed"]
    assert checkstatus(check) == "PASS"
    assert "candles=0" in check["detail"]


def test_upstox_auth_throttle_and_error_statuses(report: dict):
    checks = _checks(report, "upstox")
    assert checks["upstox.auth_failures_fail_closed"]["status"] == "PASS"
    assert "AUTH_REJECTED" in checks["upstox.provider_auth_status"]["detail"]
    assert "RATE_LIMITED" in checks["upstox.provider_throttled_status"]["detail"]
    assert checks["upstox.provider_server_error_status"]["status"] == "PASS"
    assert checks["upstox.timeout_bounded_no_retry_storm"]["status"] == "PASS"
    assert "attempts=1" in checks["upstox.timeout_bounded_no_retry_storm"]["detail"]


def test_upstox_invalid_and_stale_quotes_never_reach_a_paper_decision(report: dict):
    checks = _checks(report, "upstox")
    assert "STALE_QUOTE" in checks["upstox.stale_quote_refused"]["detail"]
    assert "NO_TRADED_PRICE" in checks["upstox.zero_price_refused"]["detail"]
    assert "MALFORMED_QUOTE" in checks["upstox.partial_quote_refused"]["detail"]
    assert "trade_eligible_now=False" in checks["upstox.provider_throttled_status"]["detail"]
    assert checks["upstox.quote_enters_paper"]["status"] == "PASS"


def checkstatus(check: dict) -> str:
    return str(check["status"])


# --------------------------------------------------------------------------- #
# Alpaca: feed identity, parsing, pagination, errors
# --------------------------------------------------------------------------- #
def test_alpaca_iex_is_never_presented_as_sip(report: dict):
    checks = _checks(report, "alpaca")
    assert checks["alpaca.feed_identity_labels"]["status"] == "PASS"
    assert "consolidated=False" in checks["alpaca.iex_feed_identity_on_quote"]["detail"]
    assert "feed=iex" in checks["alpaca.iex_feed_identity_on_quote"]["detail"]


def test_alpaca_historical_bars_are_normalized_and_bounded(report: dict):
    checks = _checks(report, "alpaca")
    assert checks["alpaca.historical_bars_normalized"]["status"] == "PASS"
    assert checks["alpaca.pagination_bounded"]["status"] == "PASS"
    assert checks["alpaca.pagination_no_infinite_loop"]["status"] == "PASS"


def test_alpaca_missing_and_malformed_payloads_are_refused(report: dict):
    checks = _checks(report, "alpaca")
    assert "refused" in checks["alpaca.missing_symbol_refused"]["detail"]
    assert checks["alpaca.malformed_bars_refused"]["status"] == "PASS"
    assert checks["alpaca.malformed_quotes_refused"]["status"] == "PASS"
    assert checks["alpaca.stream_decode_rules"]["status"] == "PASS"
    assert checks["alpaca.provider_error_statuses"]["status"] == "PASS"


def test_alpaca_throttle_and_timeout_are_observable_and_bounded(report: dict):
    checks = _checks(report, "alpaca")
    assert "RATE_LIMITED" in checks["alpaca.throttling_observable"]["detail"]
    assert "attempts=1" in checks["alpaca.timeout_bounded_no_retry_storm"]["detail"]


# --------------------------------------------------------------------------- #
# Paper path, order isolation, cross-market isolation, corporate actions
# --------------------------------------------------------------------------- #
def test_both_providers_reach_the_local_paper_engine(report: dict):
    assert _checks(report, "upstox")["upstox.quote_enters_paper"]["status"] == "PASS"
    assert _checks(report, "alpaca")["alpaca.quote_enters_paper"]["status"] == "PASS"


def test_zero_broker_order_calls_everywhere(report: dict):
    assert report["broker_order_api_calls"] == 0
    cross = {c["id"]: c for c in report["cross_market"]["checks"]}
    assert cross["paper.zero_broker_order_calls"]["status"] == "PASS"
    assert "live_trading_enabled=False" in cross["paper.zero_broker_order_calls"]["detail"]
    assert cross["paper.paper_engine_cannot_reach_orders"]["status"] == "PASS"


def test_every_provider_double_calls_only_read_only_endpoints(report: dict):
    assert report["read_only_requests"]["ok"] is True
    for provider in report["providers"].values():
        assert provider["request_methods"] == ["GET"], provider["request_methods"]
        for path in provider["request_paths"]:
            assert "order" not in path.lower()
            assert "position" not in path.lower()


def test_order_endpoint_guard_rejects_a_mutating_request():
    ok, detail = assert_no_order_endpoints(
        [
            {"method": "GET", "url": "https://data.alpaca.markets/v2/stocks/AAPL/snapshot"},
            {"method": "POST", "url": "https://api.alpaca.markets/v2/orders"},
        ]
    )
    assert ok is False
    assert "POST" in detail or "order" in detail
    ok, _ = assert_no_order_endpoints(
        [
            {"method": "GET", "url": "https://api.upstox.com/v2/market-quote/quotes?instrument_key=X"},
            {"method": "GET", "url": "https://api.alpaca.markets/v1/assets/AAPL"},
        ]
    )
    assert ok is True


def test_a_failing_provider_does_not_block_the_other_market(report: dict):
    cross = {c["id"]: c for c in report["cross_market"]["checks"]}
    check = cross["paper.failed_provider_does_not_block_other_market"]
    assert check["status"] == "PASS"
    assert "NASDAQ COMPLETED with 1 decision(s)" in check["detail"]
    assert "no cross-market blocking" in check["detail"]
    assert report["cross_market"]["status"] == "PASS"


def test_corporate_action_provenance_survives_and_adjusted_prices_never_execute(report: dict):
    cross = {c["id"]: c for c in report["cross_market"]["checks"]}
    provenance = cross["corporate_actions.provenance_survives_import"]
    assert provenance["status"] == "PASS"
    assert "status=SPLIT_ADJUSTED" in provenance["detail"]
    assert "actions_sha256=present" in provenance["detail"]
    adjusted = cross["corporate_actions.adjusted_never_executes"]
    assert adjusted["status"] == "PASS"
    assert "refused at the execution boundary=True" in adjusted["detail"]
    assert cross["corporate_actions.raw_dataset_stays_raw"]["status"] == "PASS"


def test_adjusted_series_is_refused_by_the_execution_guard():
    from app.data.corporate_actions import AdjustedSeries, AdjustedSeriesError, assert_raw_candles

    bars = synthetic_minute_bars(ALPACA_SYMBOL, end=US_NOW, count=5, minutes=5)
    series = AdjustedSeries(
        market="NASDAQ",
        symbol=ALPACA_SYMBOL,
        timeframe="5m",
        bars=bars,
        provenance={"status": "SPLIT_ADJUSTED", "prices_adjusted": True},
    )
    with pytest.raises(AdjustedSeriesError):
        series.for_execution()
    with pytest.raises(AdjustedSeriesError):
        assert_raw_candles(series, context="paper execution")
    raw = AdjustedSeries(
        market="NASDAQ",
        symbol=ALPACA_SYMBOL,
        timeframe="5m",
        bars=bars,
        provenance={"status": "RAW", "prices_adjusted": False},
    )
    assert assert_raw_candles(raw, context="paper execution") == bars


def test_paper_engine_has_no_order_transport():
    from app.markets.live_paper import LiveMarketPaperEngine

    assert LiveMarketPaperEngine.broker_order_endpoints_reachable is False


# --------------------------------------------------------------------------- #
# Adapter hardening covered by this work
# --------------------------------------------------------------------------- #
def test_alpaca_stream_decode_raises_value_error_for_truncated_bar():
    from app.data.alpaca import AlpacaStream

    with pytest.raises(ValueError):
        AlpacaStream.decode({"T": "b", "S": ALPACA_SYMBOL})  # no timestamp/prices
    with pytest.raises(ValueError):
        AlpacaStream.decode({"T": "b", "S": ALPACA_SYMBOL, "t": "2026-03-02T14:59:00Z", "o": "x"})


def test_upstox_quote_failures_are_classified_by_type():
    from app.autonomy.upstox import InvalidQuote, NoTradedQuote
    from app.markets.live_paper import LiveMarketPaperEngine as Engine

    classify = Engine._classify_fetch_exception
    assert classify("NSE", NoTradedQuote("last_price=0"))[0] == "NO_TRADED_PRICE"
    assert classify("NSE", InvalidQuote("missing last_price"))[0] == "MALFORMED_QUOTE"
    assert classify("NSE", TimeoutError("timed out"))[0] == "DISCONNECTED"
    assert classify("NASDAQ", ValueError("HTTP 429 rate limit reached"))[0] == "RATE_LIMITED"
    assert classify("NASDAQ", ValueError("credential rejected (401)"))[0] == "AUTH_REJECTED"


# --------------------------------------------------------------------------- #
# Live probe: never runs implicitly, never touches the network here
# --------------------------------------------------------------------------- #
class _NoNetworkClient:
    def __init__(self, *args, **kwargs):
        raise AssertionError("the live probe opened an HTTP client without credentials")

    def __enter__(self):  # pragma: no cover - defensive
        raise AssertionError("the live probe opened an HTTP client")

    def __exit__(self, *exc):  # pragma: no cover - defensive
        return False


def test_live_probe_without_credentials_makes_no_network_call(monkeypatch):
    from app.config.settings import Settings

    monkeypatch.setattr(httpx, "Client", _NoNetworkClient)
    monkeypatch.setattr(httpx, "AsyncClient", _NoNetworkClient)
    settings = Settings(
        api_token="conformance-token",
        database_url="sqlite:///:memory:",
        market_data_access_token="",
        alpaca_api_key="",
        alpaca_api_secret="",
    )
    result = live_probe(providers=("upstox", "alpaca"), settings=settings)
    assert result["status"] == "UNVERIFIED"
    assert result["network_calls"] == 0
    assert result["broker_order_api_calls"] == 0
    assert result["live_trading"] is False
    assert {p["status"] for p in result["providers"].values()} == {"UNVERIFIED"}
    assert result["redaction"]["secrets_found"] is False


def test_live_probe_never_prints_the_credential(monkeypatch):
    """With a credential present but a mocked transport, only a verdict is returned."""
    from app.config.settings import Settings

    secret_token = "synthetic-live-probe-token-value"

    async def fake_verify(token, *, authorize_url, transport=None, timeout=20.0):
        from app.data.upstox_auth import CredentialCheck

        assert token == secret_token
        return CredentialCheck(
            verdict="feed_ok",
            http_status=200,
            authenticated=True,
            guidance="authorized",
            checked_at="2026-03-02T05:30:00+00:00",
        )

    import app.data.upstox_auth as upstox_auth

    monkeypatch.setattr(upstox_auth, "verify_feed_credential", fake_verify)
    settings = Settings(
        api_token="conformance-token",
        database_url="sqlite:///:memory:",
        market_data_access_token=secret_token,
        alpaca_api_key="",
        alpaca_api_secret="",
    )
    result = live_probe(providers=("upstox",), settings=settings)
    rendered = json.dumps(result)
    assert result["providers"]["upstox"]["status"] == "PASS"
    assert result["providers"]["upstox"]["verdict"] == "feed_ok"
    assert secret_token not in rendered
    assert result["network_calls"] == 1


def test_run_conformance_rejects_unknown_provider():
    with pytest.raises(ValueError):
        run_conformance(providers=("nasdaq-direct",))


# --------------------------------------------------------------------------- #
# Operator CLI
# --------------------------------------------------------------------------- #
def _cli(*args: str) -> tuple[int, str]:
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "-m", "app", "provider-check", *args],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(repo),
    )
    return completed.returncode, completed.stdout + completed.stderr


def test_cli_offline_report_passes_and_writes_only_the_requested_file(tmp_path: Path):
    target = tmp_path / "conformance.json"
    code, output = _cli("--json", "--output", str(target))
    assert code == 0, output
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["status"] == "PASS"
    assert payload["network_calls"] == 0
    assert sorted(payload["providers"]) == ["alpaca", "upstox"]


def test_cli_provider_filter_and_text_output():
    code, output = _cli("--provider", "alpaca")
    assert code == 0, output
    assert "[alpaca] PASS" in output
    assert "[upstox]" not in output
    assert "network calls: 0" in output


def test_cli_live_without_credentials_reports_unverified_without_network():
    code, output = _cli("--live", "--json")
    payload = json.loads(output[output.index("{"):])
    assert code == 3, output
    assert payload["status"] == "UNVERIFIED"
    assert payload["network_calls"] == 0
    assert payload["broker_order_api_calls"] == 0
    for entry in payload["providers"].values():
        assert "nothing was contacted" in entry["detail"]


# --------------------------------------------------------------------------- #
# File-handle discipline (Windows acceptance blocker)
# --------------------------------------------------------------------------- #
# A plain `with sqlite3.connect(...) as conn:` commits but never closes, so
# every engine call used to leak a handle. POSIX tolerates it; Windows fails
# TemporaryDirectory cleanup with WinError 32 and keeps the database plus its
# WAL sidecars locked, which is what broke the Windows build gate. These tests
# fail on Linux too, by checking for the handle rather than for the symptom.
def _open_fds_under(path: Path) -> list[str]:
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.exists():  # not Linux: nothing to inspect here
        pytest.skip("no /proc/self/fd on this platform")
    found = []
    for entry in fd_dir.iterdir():
        try:
            target = entry.resolve()
        except OSError:
            continue
        if str(target).startswith(str(path)):
            found.append(str(target))
    return found


def test_engine_releases_its_database_handle_on_close(tmp_path: Path):
    from app.markets.live_paper import LiveMarketPaperEngine

    db = tmp_path / "handles.sqlite3"
    engine = LiveMarketPaperEngine(db)
    try:
        engine.list_watchlists()
        engine.market_snapshot("NSE")
        engine.market_snapshot("NASDAQ")
        # No handle may be held between calls, not just after close.
        assert _open_fds_under(tmp_path) == [], "a handle leaked between engine calls"
    finally:
        engine.close()
    assert _open_fds_under(tmp_path) == [], "a handle survived engine.close()"


def test_conformance_run_leaves_no_handle_on_its_workdir(tmp_path: Path):
    """The run must be able to delete its own temporary directory."""
    import tempfile

    workdir = Path(tempfile.mkdtemp(prefix="axiom-handle-check-", dir=tmp_path))
    try:
        report = run_conformance()
        assert report["status"] == "PASS"
        assert _open_fds_under(workdir) == []
    finally:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)
    assert not workdir.exists(), "the work directory could not be removed"


def test_connection_commit_and_rollback_semantics_are_unchanged(tmp_path: Path):
    """Closing the handle must not have replaced commit/rollback."""
    from app.markets.live_paper import LiveMarketPaperEngine

    db = tmp_path / "txn.sqlite3"
    engine = LiveMarketPaperEngine(db)
    try:
        before = engine.market_snapshot("NSE")["account"]["cash"]
        # A committed write survives a fresh connection.
        with engine._lock, engine._connect() as conn:
            conn.execute("UPDATE live_paper_accounts SET cash = cash - 1 WHERE market = 'NSE'")
        assert engine.market_snapshot("NSE")["account"]["cash"] == pytest.approx(before - 1)

        # A raised exception rolls back and still releases the handle.
        with pytest.raises(RuntimeError):
            with engine._lock, engine._connect() as conn:
                conn.execute("UPDATE live_paper_accounts SET cash = cash - 10 WHERE market = 'NSE'")
                raise RuntimeError("boom")
        assert engine.market_snapshot("NSE")["account"]["cash"] == pytest.approx(before - 1)
        assert _open_fds_under(tmp_path) == []
    finally:
        engine.close()
