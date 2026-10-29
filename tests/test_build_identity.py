"""The running app must name its own build and the exact UI/API files it serves.

The Windows freeze writes ``app/_build_identity.json`` (commit, version, and
SHA-256 of the reviewed source files) into the bundle; ``/health`` reports it
alongside runtime hashes of the served static assets. Together with the
no-store cache policy this lets an operator verify — from the running process,
in a cache-proof way — which build is actually running and which UI bytes it
serves, instead of trusting an install path or a browser cache. The /catalog
stats contract is pinned here because the UI diagnostics line renders those
exact fields and a missing key silently hides every parse drop.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings

REPO = Path(__file__).resolve().parents[1]


def client(tmp_path):
    settings = replace(Settings(), api_token="local-api-test", database_url=f"sqlite:///{tmp_path}/app.db")
    return TestClient(create_app(settings, data_dir=tmp_path))


def test_health_reports_build_identity_and_served_file_hashes(tmp_path):
    with client(tmp_path) as web:
        response = web.get("/health")
        assert response.status_code == 200
        build = response.json()["build"]
        assert build["frozen"] is False
        assert build["commit"] == "dev"  # no _build_identity.json in a dev tree
        served = build["served_files"]
        assert "static/autonomy/autonomy.js" in served
        for relative, digest in served.items():
            actual = hashlib.sha256((REPO / "app" / relative).read_bytes()).hexdigest()
            assert digest == actual, f"served hash must match the file on disk: {relative}"


def test_served_hashes_match_the_bytes_the_server_serves(tmp_path):
    with client(tmp_path) as web:
        build = web.get("/health").json()["build"]
        page = web.get("/static/autonomy/autonomy.js")
        assert page.status_code == 200
        served = hashlib.sha256(page.content).hexdigest()
        assert build["served_files"]["static/autonomy/autonomy.js"] == served


def test_embedded_identity_is_reported_verbatim(tmp_path, monkeypatch):
    import app.build_info as build_info

    payload = {
        "commit": "7e00db4fe5b883ab964c19b223af211690b8e745",
        "version": "3.7.0a6",
        "built_utc": "2026-10-29T05:00:00Z",
        "source_hashes": {"app/autonomy/upstox.py": "0" * 64},
    }
    path = build_info.APP_DIR / "_build_identity.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        with client(tmp_path) as web:
            build = web.get("/health").json()["build"]
            assert build["commit"] == payload["commit"]
            assert build["source_hashes"]["app/autonomy/upstox.py"] == "0" * 64
            assert build["version"] == "3.7.0a6"
    finally:
        path.unlink(missing_ok=True)


def test_static_assets_are_served_no_store(tmp_path):
    with client(tmp_path) as web:
        response = web.get("/static/autonomy/autonomy.js")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"


def test_catalog_response_carries_the_parse_stats_the_ui_renders(tmp_path):
    from datetime import datetime, timezone

    row = {
        "segment": "NSE_EQ", "name": "RELIANCE INDUSTRIES LTD",
        "exchange": "NSE", "isin": "INE002A01018",
        "instrument_type": "EQ", "instrument_key": "NSE_EQ|INE002A01018",
        "lot_size": 1, "freeze_quantity": 100000.0, "exchange_token": "2885",
        "tick_size": 5.0, "trading_symbol": "RELIANCE", "expiry": 1735689000000,
    }
    with client(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        catalog = web.app.state.autonomy.catalog

        def fake_download(url):
            return ([row] if "complete" in str(url) else []), "bod-digest"

        catalog._download = fake_download
        catalog.clock = lambda: datetime(2026, 10, 29, 5, 0, tzinfo=timezone.utc)
        web.post("/api/autonomy/catalog/refresh", headers=headers)
        response = web.get("/api/autonomy/catalog", params={"query": "RELIANCE"}, headers=headers)
        assert response.status_code == 200
        stats = response.json()["stats"]
        # The exact fields the UI diagnostics line reads; a missing key here
        # silently hides parse drops from the operator.
        for field in ("source_rows", "suspended_keys", "kept_by_segment",
                      "dropped_suspended", "dropped_unsupported", "dropped_unsupported_by",
                      "dropped_invalid", "dropped_invalid_samples"):
            assert field in stats, f"/catalog stats must expose {field} for the UI"
        assert stats["source_rows"] == 1
        assert stats["kept_by_segment"] == {"NSE_EQ": 1}
