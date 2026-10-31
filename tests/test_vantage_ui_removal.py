"""Regression tests verifying removal of Vantage MT5 UI surfaces and MT5 operator docs
while preserving Upstox (NSE INR) and Alpaca (NASDAQ USD) connections, risk gates,
paper/live separation, and execution behavior (v3.7.0a9).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-vantage-ui-removal-tok"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

REMOVED_DOM_IDS = (
    "credential-form",
    "mt5-login",
    "mt5-password",
    "mt5-server",
    "mt5-path",
    "mt5-label",
    "save-credential",
    "replace-credential",
    "delete-credential",
    "connection-state",
    "test-connection",
    "disconnect-connection",
    "connect-market-data",
    "disconnect-market-data",
    "session-mode",
    "apply-mode",
    "run-readiness",
    "arm-session",
    "activate-session",
    "risk-form",
    "save-risk",
    "instrument-rows",
    "watchlist-only",
    "contract-panel",
    "reconcile-now",
)

FORBIDDEN_UI_TERMS = (
    "VANTAGE_DEMO",
    "VANTAGE_LIVE",
    "VantageMarkets-Live 13",
    "VANTAGE OPERATOR",
    "MetaTrader",
    "terminal64.exe",
)


def _make_client(tmp_path: Path) -> TestClient:
    settings = replace(
        Settings(),
        api_token=TOKEN,
        database_url=f"sqlite:///{tmp_path / 'ui_removal.sqlite'}",
    )
    return TestClient(create_app(settings, data_dir=tmp_path))


def test_terminal_and_public_surfaces_have_no_vantage_mt5_ui(tmp_path: Path) -> None:
    with _make_client(tmp_path) as client:
        landing = client.get("/")
        assert landing.status_code == 200
        assert "NSE (UPSTOX)" in landing.text
        assert "NASDAQ (ALPACA)" in landing.text
        for forbidden in ("Vantage", "MT5", "MetaTrader"):
            assert forbidden not in landing.text

        dashboard = client.get("/dashboard")
        assert dashboard.status_code == 200
        assert "OPERATOR TERMINAL" in dashboard.text
        assert 'href="/terminal"' in dashboard.text
        for forbidden in ("VANTAGE OPERATOR", "Vantage", "MT5"):
            assert forbidden not in dashboard.text

        terminal = client.get("/terminal")
        assert terminal.status_code == 200
        for dom_id in REMOVED_DOM_IDS:
            assert f'id="{dom_id}"' not in terminal.text, f"#{dom_id} still present in /terminal HTML"
        for term in ("Vantage", "MT5", "MetaTrader", *FORBIDDEN_UI_TERMS):
            assert term not in terminal.text, f"{term!r} still present in /terminal HTML"

        # Present Upstox (NSE INR) and Alpaca (NASDAQ USD) cleanly across pages
        for required_id in (
            "mm-upstox-data-form",
            "mm-alpaca-data-form",
            "mm-alpaca-broker-cred-form",
            "mm-upstox-live-cred-form",
            "mm-nse-risk-form",
            "mm-nasdaq-risk-form",
            "mm-catalog-rows",
            "mm-watchlist-grid",
            "mm-chart-source-mode",
            "ticket-tab-mm",
            "mm-reconcile-paper-btn",
            "secret-store-status",
        ):
            assert f'id="{required_id}"' in terminal.text, f"#{required_id} missing from /terminal HTML"

        terminal_js = client.get("/static/terminal/terminal.js")
        assert terminal_js.status_code == 200
        for term in ("Vantage", "MT5", "MetaTrader", *FORBIDDEN_UI_TERMS):
            assert term not in terminal_js.text, f"{term!r} still present in terminal.js"

        terminal_css = client.get("/static/terminal/terminal.css")
        assert terminal_css.status_code == 200
        for term in ("VANTAGE_DEMO", "VANTAGE_LIVE", "Vantage", "MT5"):
            assert term not in terminal_css.text, f"{term!r} still present in terminal.css"

        manual = client.get("/manual")
        assert manual.status_code == 200
        assert "Upstox" in manual.text and "Alpaca" in manual.text
        for term in ("VantageMarkets-Live 13", "MetaTrader", "terminal64.exe", "VANTAGE_DEMO", "VANTAGE_LIVE"):
            assert term not in manual.text, f"{term!r} still present in /manual"


def test_upstox_and_alpaca_connections_and_paper_live_gates_remain_intact(tmp_path: Path) -> None:
    with _make_client(tmp_path) as client:
        # 1. Multi-market overview exposes NSE (Upstox) and NASDAQ (Alpaca) with live gates OFF
        mm = client.get("/api/markets/overview", headers=HEADERS)
        assert mm.status_code == 200
        body = mm.json()
        assert body["paper"]["NSE"]["account"]["currency"] == "INR"
        assert body["paper"]["NASDAQ"]["account"]["currency"] == "USD"
        assert body["live"]["default_live_execution"] == "OFF"
        assert body["live"]["nse"]["availability"] == "NOT_AVAILABLE"
        assert body["live"]["nasdaq"]["availability"] == "NOT_AVAILABLE"
        provider_ids = {p["id"] for p in body["providers"]["providers"]}
        assert "upstox_market_data" in provider_ids
        assert "alpaca_market_data" in provider_ids
        assert not any("vantage" in pid or "mt5" in pid for pid in provider_ids)

        # 2. Alpaca Broker fails closed (409) when ALPACA_BROKER_ENABLED is false by default
        alpaca = client.get("/api/alpaca-broker/status", headers=HEADERS)
        assert alpaca.status_code == 409

        # 3. Upstox Autonomy remains PAPER/disarmed by default
        autonomy = client.get("/api/autonomy/status", headers=HEADERS)
        assert autonomy.status_code == 200
        auto_data = autonomy.json()
        assert auto_data.get("live_capable", False) is False
        assert auto_data.get("live_active", False) is False

        # 4. Separate Alpaca market-data credential status endpoint still works cleanly
        alpaca_creds = client.get("/api/connections/alpaca/credentials", headers=HEADERS)
        assert alpaca_creds.status_code == 200
        assert alpaca_creds.json()["present"] is False

        # 5. Live-market paper preview for NSE (INR) and NASDAQ (USD) still enforces exact currency/fees
        nse_preview = client.post(
            "/api/markets/paper/preview",
            headers=HEADERS,
            json={
                "market": "NSE",
                "symbol": "RELIANCE",
                "side": "BUY",
                "order_type": "LIMIT",
                "qty": 2,
                "limit_price": 2950.0,
            },
        )
        assert nse_preview.status_code == 200
        assert nse_preview.json()["currency"] == "INR"

        us_preview = client.post(
            "/api/markets/paper/preview",
            headers=HEADERS,
            json={
                "market": "NASDAQ",
                "symbol": "AAPL",
                "side": "BUY",
                "order_type": "LIMIT",
                "qty": 2,
                "limit_price": 225.50,
            },
        )
        assert us_preview.status_code == 200
        assert us_preview.json()["currency"] == "USD"


def test_user_facing_docs_remove_mt5_configuration_instructions() -> None:
    assert not (ROOT / "docs" / "VANTAGE_NEXT_VERSION_PLAN.md").exists()
    doc_paths = (
        ROOT / "README.md",
        ROOT / "USER_MANUAL.md",
        ROOT / "packaging" / "windows" / "WINDOWS_INSTALL_MANUAL.md",
        ROOT / "docs" / "OWNER_SETUP_GUIDE.md",
        ROOT / "docs" / "LISTED_ASSETS.md",
        ROOT / ".env.example",
    )
    for path in doc_paths:
        text = path.read_text(encoding="utf-8")
        for forbidden in ("VantageMarkets-Live 13", "terminal64.exe", "MetaTrader 5", "VANTAGE_DEMO", "VANTAGE_LIVE"):
            assert forbidden not in text, f"{forbidden!r} found in {path.relative_to(ROOT)}"
