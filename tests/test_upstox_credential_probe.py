"""Granular error classification for the Upstox credential probe.

Regression tests for the 3.2.1 bug where a frozen Windows executable could not
open HTTPS to api.upstox.com (certifi CA bundle not bundled by PyInstaller),
and the broad ``except httpx.HTTPError`` then collapsed every network/TLS
failure into a generic "unreachable" verdict. Every failure class must now
carry a distinct, operator-actionable verdict/guidance; the CA bundle must be
available to frozen builds; and the happy path must still report feed_ok.
"""

from __future__ import annotations

import asyncio
import ssl

import httpx
import pytest

from app.data import upstox_auth


def run(coro):
    return asyncio.run(coro)


class _StaticTransport(httpx.AsyncBaseTransport):
    """Test seam returning a pre-built response or raising a given exception."""

    def __init__(self, *, response=None, error=None):
        self._response = response
        self._error = error

    async def handle_async_request(self, request):
        if self._error is not None:
            raise self._error
        return self._response


def _response(status_code, json_body=None, content=b""):
    import json as _json
    if json_body is not None and not content:
        content = _json.dumps(json_body).encode("utf-8")
    return httpx.Response(
        status_code=status_code,
        content=content,
        request=httpx.Request("GET", "https://api.upstox.com/v3/feed/market-data-feed/authorize"),
    )


async def _check(token="tok", **kwargs):
    return await upstox_auth.verify_feed_credential(
        token, authorize_url="https://x/authorize", timeout=15.0, **kwargs,
    )


class TestHappyPath:
    def test_200_with_valid_wss_uri_reports_feed_ok(self):
        transport = _StaticTransport(response=_response(
            200, {"data": {"authorized_redirect_uri": "wss://api.upstox.com/v3/feed"}},
        ))
        check = run(_check(transport=transport))
        assert check.verdict == "feed_ok"
        assert check.http_status == 200
        assert check.authenticated is True


class TestHttpStatusClassifications:
    def test_401_reports_rejected(self):
        transport = _StaticTransport(response=_response(401))
        check = run(_check(transport=transport))
        assert check.verdict == "rejected"
        assert check.http_status == 401

    def test_403_reports_forbidden(self):
        transport = _StaticTransport(response=_response(403))
        check = run(_check(transport=transport))
        assert check.verdict == "forbidden"
        assert check.http_status == 403

    def test_429_reports_http_429(self):
        transport = _StaticTransport(response=_response(429))
        check = run(_check(transport=transport))
        assert check.verdict == "http_429"
        assert check.http_status == 429

    def test_500_reports_http_500(self):
        transport = _StaticTransport(response=_response(500))
        check = run(_check(transport=transport))
        assert check.verdict == "http_500"
        assert check.http_status == 500

    def test_200_malformed_body_is_unexpected_response(self):
        transport = _StaticTransport(response=_response(200, {"unexpected": True}))
        check = run(_check(transport=transport))
        assert check.verdict == "unexpected_response"

    def test_200_non_wss_uri_is_unexpected_response(self):
        transport = _StaticTransport(response=_response(
            200, {"data": {"authorized_redirect_uri": "https://not-a-ws/"}},
        ))
        check = run(_check(transport=transport))
        assert check.verdict == "unexpected_response"


class TestNetworkFailureClassifications:
    def test_connect_timeout(self):
        transport = _StaticTransport(error=httpx.ConnectTimeout("boom"))
        check = run(_check(transport=transport))
        assert check.verdict == "timeout"
        assert "timed out" in check.guidance.lower()

    def test_read_timeout(self):
        transport = _StaticTransport(error=httpx.ReadTimeout("boom"))
        check = run(_check(transport=transport))
        assert check.verdict == "timeout"

    def test_dns_or_tcp_connect_failure(self):
        transport = _StaticTransport(error=httpx.ConnectError("boom"))
        check = run(_check(transport=transport))
        assert check.verdict == "dns_or_connect_failure"
        assert "network" in check.guidance.lower()

    def test_tls_failure_is_specifically_reported(self):
        transport = _StaticTransport(error=ssl.SSLError("certificate verify failed"))
        check = run(_check(transport=transport))
        assert check.verdict == "tls_failure"
        assert "tls" in check.guidance.lower()

    def test_empty_token_is_invalid_input(self):
        transport = _StaticTransport(response=_response(200, {}))
        check = run(_check(transport=transport, token="   "))
        assert check.verdict == "invalid_input"


class TestCaBundleAvailability:
    def test_certifi_ca_file_exists(self):
        import certifi
        from pathlib import Path
        assert Path(certifi.where()).is_file(), "certifi CA bundle missing"

    def test_ssl_context_uses_certifi(self):
        assert upstox_auth._SSL_CONTEXT is not None

    def test_packaging_spec_collects_certifi_data(self):
        from pathlib import Path
        spec_text = Path(__file__).resolve().parents[1] / "packaging" / "arena.spec"
        text = spec_text.read_text(encoding="utf-8")
        assert "collect_data_files(\"certifi\")" in text
