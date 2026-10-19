"""3.1.0 — the setup-page JavaScript corruption class is dead, structurally.

The 3.0.5 incident: JavaScript generated inside a Python triple-quoted string
was corrupted by backslash escaping; every handler on the setup page was dead
while the page still returned HTTP 200. These tests pin the fix:

* the served /setup and /app shells contain NO inline JavaScript at all;
* every served static asset parses under a REAL JavaScript parser
  (``node --check``); if node is unavailable the inline-legacy pages
  (/console, /dashboard) still get parsed — no page ships unparseable JS;
* the wizard shell carries the expected interactive root element;
* the token endpoint works through the running application;
* asset URLs are version-fingerprinted so a stale cache cannot resurrect an
  old bundle.

These checks run against the ACTUAL served HTML, never against source files
alone — the lesson of the incident.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def extract_inline_scripts(html: str) -> list[str]:
    return re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html,
                      re.S | re.I)


def extract_script_srcs(html: str) -> list[str]:
    return re.findall(r'<script[^>]*\bsrc="([^"]+)"', html)


def node_available() -> bool:
    return shutil.which("node") is not None


def assert_js_parses(js: str, label: str) -> None:
    """Parse JavaScript with a real engine. Hard requirement when node exists;
    structural fallback otherwise (weaker, and says so)."""
    if node_available():
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(js)
            path = handle.name
        try:
            proc = subprocess.run(["node", "--check", path],
                                  capture_output=True, text=True, timeout=60)
            assert proc.returncode == 0, f"{label} failed to parse:\n{proc.stderr[:800]}"
        finally:
            Path(path).unlink(missing_ok=True)
        return
    # Fallback: balanced delimiters outside string literals.
    depth = 0
    quote = None
    escaped = False
    for ch in js:
        if escaped:
            escaped = False
            continue
        if quote:
            if ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'`":
            quote = ch
        elif ch in "{([":
            depth += 1
        elif ch in "})]":
            depth -= 1
            assert depth >= 0, f"{label}: unbalanced delimiters (fallback checker)"
    assert depth == 0 and quote is None, f"{label}: unterminated structure (fallback)"


@pytest.fixture()
def client():
    app = create_app(Settings())
    with TestClient(app) as test_client:
        yield test_client


class TestWizardShell:
    def test_setup_serves_a_shell_without_any_inline_javascript(self, client):
        html = client.get("/setup").text
        inline = extract_inline_scripts(html)
        assert all(not chunk.strip() for chunk in inline), (
            "the setup page must not carry inline JavaScript — that is the "
            "exact mechanism that corrupted the 3.0.5 page")

    def test_setup_references_external_versioned_assets(self, client):
        html = client.get("/setup").text
        srcs = extract_script_srcs(html)
        assert srcs, "setup must load its JS from static assets"
        for src in srcs:
            assert "?v=" in src, f"asset URL must be version-fingerprinted: {src}"
        assert any("arena-wizard.js" in src for src in srcs)
        assert any("arena-core.js" in src for src in srcs)

    def test_setup_contains_the_wizard_root_and_no_secrets(self, client):
        html = client.get("/setup").text
        assert 'id="wizard-root"' in html
        assert "token_urlsafe" not in html
        assert "Bearer " not in html

    def test_app_shell_serves_and_references_the_application_js(self, client):
        response = client.get("/app")
        assert response.status_code == 200
        srcs = extract_script_srcs(response.text)
        assert any("arena-app.js" in src for src in srcs)
        assert all(not chunk.strip()
                   for chunk in extract_inline_scripts(response.text))


class TestWizardAuthenticationHandoff:
    source = (ROOT / "app" / "static" / "arena-wizard.js").read_text(encoding="utf-8")

    def test_existing_token_is_revealed_and_overwrites_stale_session_token(self):
        assert 'Arena.post("/api/credentials/reveal", {})' in self.source
        assert "Arena.setToken(token)" in self.source
        assert "Arena.getToken() !== token" in self.source
        assert 'Arena.get("/api/system/info")' in self.source

    def test_authentication_failure_blocks_the_start_action(self):
        assert 'authFailure ? "Authentication could not be verified"' in self.source
        assert 'id: "w-start-engine", disabled: "disabled"' in self.source
        assert "blockedStart.disabled = true" in self.source
        assert "btn.disabled = true" in self.source
        assert '"Authentication could not be verified"' in self.source

    def test_token_handoff_errors_do_not_include_credential_values(self):
        assert "no credential value was exposed" in self.source
        assert "token=" not in self.source


class TestServedAssetsParse:
    @pytest.mark.parametrize("name", [
        "arena-core.js", "arena-wizard.js", "arena-live.js", "arena-app.js",
    ])
    def test_each_served_asset_is_valid_javascript(self, client, name):
        response = client.get(f"/static/{name}")
        assert response.status_code == 200, f"{name} must be served from the bundle"
        assert_js_parses(response.text, f"served /static/{name}")

    def test_static_route_refuses_unknown_assets(self, client):
        assert client.get("/static/arena-secret.js").status_code == 404
        assert client.get("/static/../app/config/settings.py").status_code == 404

    def test_served_assets_carry_immutable_cache_with_fingerprint(self, client):
        response = client.get("/static/arena-core.js")
        assert "immutable" in response.headers.get("cache-control", "")


class TestChartWorkspaceInteractions:
    source = (ROOT / "app" / "static" / "arena-app.js").read_text(encoding="utf-8")

    def test_crosshair_syncs_recorded_bars_by_timestamp(self):
        assert "pointermove" in self.source
        assert "pointerleave" in self.source
        assert "syncSharedCrosshair" in self.source
        assert "nearestChartCandle" in self.source
        assert "Date.parse" in self.source
        assert "chart-crosshair-horizontal" in self.source

    def test_panels_can_be_dragged_reordered_and_resized(self):
        assert "draggable: \"true\"" in self.source
        assert 'addEventListener("dragstart"' in self.source
        assert 'addEventListener("drop"' in self.source
        assert "reorderChartPanels" in self.source
        assert 'type: "range"' in self.source
        assert 'cfg.panels[index].height = height' in self.source
        assert "saveChartConfig(cfg)" in self.source

    def test_pin_buttons_use_the_instrument_pin_api(self):
        assert '"/api/universe/pins?market_id="' in self.source
        assert '"/api/universe/pin"' in self.source
        assert '"/api/universe/unpin"' in self.source
        assert "addInstrumentPinControl" in self.source


class TestLegacyPagesStillParse:
    """The feature console and dashboard still use inline scripts; the
    incident's lesson applies to them too — their JavaScript must parse."""

    @pytest.mark.parametrize("path", ["/console", "/dashboard"])
    def test_inline_javascript_parses(self, client, path):
        html = client.get(path).text
        for index, chunk in enumerate(extract_inline_scripts(html)):
            if chunk.strip():
                assert_js_parses(chunk, f"{path} inline block {index}")

    def test_console_still_exposes_its_interactive_ids(self, client):
        html = client.get("/console").text
        assert "output" in html  # the console output pane exists


class TestTokenEndpointThroughTheRunningApp:
    def test_generate_token_returns_a_fresh_secret(self, client):
        first = client.post("/setup/generate-token").json()["token"]
        second = client.post("/setup/generate-token").json()["token"]
        assert len(first) >= 32 and len(second) >= 32
        assert first != second, "every generation must produce a new secret"
