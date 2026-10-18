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
import json
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

    def test_socket_timeout(self):
        transport = _StaticTransport(error=TimeoutError("socket timed out"))
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
        from pathlib import Path

        import certifi
        assert Path(certifi.where()).is_file(), "certifi CA bundle missing"

    def test_ssl_context_uses_certifi(self):
        assert upstox_auth._SSL_CONTEXT is not None

    def test_packaging_spec_collects_certifi_data(self):
        from pathlib import Path
        spec_text = Path(__file__).resolve().parents[1] / "packaging" / "arena.spec"
        text = spec_text.read_text(encoding="utf-8")
        assert "collect_data_files(\"certifi\")" in text
        assert "app.data.upstox_diagnostic" in text


class TestSafeUpstoxDiagnostic:
    """Every diagnostic case is mocked; no test uses a real Upstox credential."""

    def diagnose(self, *, token="synthetic-market-token", response=None, error=None, **kwargs):
        from app.data.upstox_diagnostic import run_upstox_diagnostic

        return run(run_upstox_diagnostic(
            token,
            authorize_url="https://api.upstox.com/v3/feed/market-data-feed/authorize",
            http_transport=_StaticTransport(response=response, error=error),
            **kwargs,
        ))

    def test_success_reports_https_and_authorization_separately_without_signed_url(self):
        token = "synthetic-market-token"
        signed_uri = "wss://feed.upstox.com/v3/feed?requestId=req-1&code=one-use-secret"
        report = self.diagnose(
            token=token,
            response=_response(200, {"data": {"authorized_redirect_uri": signed_uri}}),
        )
        assert report["https_reachability"] == {
            "status": "reachable", "tls_verified": True, "http_status": 200,
        }
        assert report["credential_authorization"]["status"] == "authorized"
        assert report["credential_authorization"]["authenticated"] is True
        assert report["websocket"] == "not_requested"
        assert report["ok"] is True
        serialized = json.dumps(report)
        assert token not in serialized
        assert "one-use-secret" not in serialized
        assert signed_uri not in serialized

    @pytest.mark.parametrize(
        ("status", "expected"),
        [(401, "rejected_401"), (403, "forbidden_403")],
    )
    def test_http_auth_rejections_are_not_network_failures(self, status, expected):
        secret = "fake-token-must-not-escape"
        report = self.diagnose(
            token=secret,
            response=_response(status, {"errors": [{"message": secret}]}),
            test_websocket=True,
        )
        assert report["https_reachability"]["status"] == "reachable"
        assert report["https_reachability"]["tls_verified"] is True
        assert report["credential_authorization"]["status"] == expected
        assert report["credential_authorization"]["http_status"] == status
        assert report["websocket"] == "not_tried"
        assert report["websocket_check"]["handshake"] == "not_attempted"
        assert secret not in json.dumps(report)

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (httpx.ConnectError("DNS lookup failed"), "dns_or_tcp_failure"),
            (httpx.ConnectTimeout("connect timed out"), "timeout"),
            (ssl.SSLCertVerificationError("certificate verify failed"), "tls_failure"),
        ],
    )
    def test_network_tls_and_timeout_categories_are_preserved(self, error, expected):
        report = self.diagnose(error=error, test_websocket=True)
        assert report["https_reachability"]["status"] == expected
        assert report["https_reachability"]["tls_verified"] is (
            False if expected == "tls_failure" else None
        )
        assert report["credential_authorization"]["status"] == "not_evaluated"
        assert report["credential_authorization"]["authenticated"] is False
        assert report["websocket"] == "not_tried"

    def test_tls_error_wrapped_by_httpx_connect_error_is_not_mislabelled_dns(self):
        wrapped = httpx.ConnectError("TLS connect failed")
        wrapped.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
        report = self.diagnose(error=wrapped)
        assert report["https_reachability"]["status"] == "tls_failure"
        assert report["credential_authorization"]["verdict"] == "tls_failure"

    def test_tls_error_wrapped_by_protocol_error_is_not_mislabelled_network(self):
        wrapped = httpx.RemoteProtocolError("TLS socket closed")
        wrapped.__cause__ = ssl.SSLError("TLS EOF")
        check = run(_check(transport=_StaticTransport(error=wrapped)))
        assert check.verdict == "tls_failure"

    @pytest.mark.parametrize(
        "response",
        [
            _response(200, content=b"not-json"),
            _response(200, {"data": {"authorized_redirect_uri": "https://not-a-websocket/"}}),
            _response(200, {"data": {"authorized_redirect_uri": "wss://attacker.invalid/?code=secret"}}),
        ],
    )
    def test_malformed_authorize_body_never_attempts_websocket(self, response):
        report = self.diagnose(response=response, test_websocket=True)
        assert report["https_reachability"]["status"] == "reachable"
        assert report["credential_authorization"]["status"] == "malformed_response"
        assert report["websocket"] == "not_tried"
        assert "secret" not in json.dumps(report)

    def test_empty_local_credential_does_not_attempt_network(self):
        report = self.diagnose(token="", error=httpx.ConnectError("must not be reached"))
        assert report["https_reachability"]["status"] == "not_attempted"
        assert report["credential_authorization"]["status"] == "missing_credential"
        assert report["ok"] is False

    @pytest.mark.parametrize("duration", [0, -1, 121, float("nan"), float("inf")])
    def test_websocket_duration_is_finite_positive_and_bounded_before_network(self, duration):
        from app.data.upstox_diagnostic import run_upstox_diagnostic

        with pytest.raises(ValueError, match="duration"):
            run(run_upstox_diagnostic(
                "synthetic-token",
                test_websocket=True,
                duration=duration,
                http_transport=_StaticTransport(error=httpx.ConnectError("must not run")),
            ))

    def test_configured_ca_bundle_verifies_hostnames(self):
        assert upstox_auth._SSL_CONTEXT.verify_mode == ssl.CERT_REQUIRED
        assert upstox_auth._SSL_CONTEXT.check_hostname is True

    def test_websocket_subscription_receives_a_valid_market_data_message_and_closes(self):
        import time

        from app.data.upstox_diagnostic import run_upstox_diagnostic
        from app.data.upstox_proto.MarketDataFeed_pb2 import FeedResponse
        from tests.test_upstox_live import KEY

        token = "synthetic-market-token"
        signed_uri = "wss://feed.upstox.com/v3/feed?code=one-use-secret"
        frame = FeedResponse(type=0, currentTs=int(time.time() * 1000))
        frame.feeds[KEY].ltpc.ltp = 101.25
        frame.feeds[KEY].ltpc.ltt = int(time.time() * 1000)

        class FakeSocket:
            def __init__(self):
                self.sent = []
                self.closed = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                self.closed = True
                return False

            async def send(self, payload):
                self.sent.append(payload)

            async def recv(self):
                return frame.SerializeToString()

        socket = FakeSocket()
        seen = {}

        def connector(uri, **kwargs):
            seen["uri"] = uri
            seen["kwargs"] = kwargs
            return socket

        report = run(run_upstox_diagnostic(
            token,
            instrument_keys=[KEY],
            mode="ltpc",
            test_websocket=True,
            duration=1.0,
            authorize_url="https://api.upstox.com/v3/feed/market-data-feed/authorize",
            http_transport=_StaticTransport(response=_response(
                200, {"data": {"authorized_redirect_uri": signed_uri}},
            )),
            websocket_connector=connector,
        ))
        assert report["ok"] is True
        assert report["websocket_check"]["handshake_completed"] is True
        assert report["market_data"]["received"] is True
        assert report["market_data"]["valid_messages"] == 1
        assert socket.closed is True  # async-with performed a clean close
        assert seen["uri"] == signed_uri  # used privately, never emitted
        assert seen["kwargs"]["ssl"] is upstox_auth._SSL_CONTEXT
        subscription = json.loads(socket.sent[0].decode("utf-8"))
        assert subscription["method"] == "sub"
        assert subscription["data"] == {"mode": "ltpc", "instrumentKeys": [KEY]}
        serialized = json.dumps(report)
        assert token not in serialized and "one-use-secret" not in serialized
        assert report["scope"]["order_endpoints_called"] is False
        assert report["scope"]["configuration_written"] is False

    def test_websocket_timeout_is_not_credential_rejection_and_closes(self):
        from app.data.upstox_diagnostic import run_upstox_diagnostic
        from tests.test_upstox_live import KEY

        class SilentSocket:
            closed = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                self.closed = True
                return False

            async def send(self, payload):
                self.subscription = payload

            async def recv(self):
                await asyncio.Future()

        socket = SilentSocket()
        uri = "wss://feed.upstox.com/v3/feed?code=one-use-secret"
        report = run(asyncio.wait_for(run_upstox_diagnostic(
            "synthetic-market-token",
            instrument_keys=[KEY],
            test_websocket=True,
            duration=0.02,
            http_transport=_StaticTransport(response=_response(
                200, {"data": {"authorized_redirect_uri": uri}},
            )),
            websocket_connector=lambda *_args, **_kwargs: socket,
        ), timeout=1.0))
        assert report["credential_authorization"]["status"] == "authorized"
        assert report["websocket"] == "connected"
        assert report["market_data"]["received"] is False
        assert report["market_data"]["reason"] == "no_valid_market_data_in_window"
        assert socket.closed is True

    def test_websocket_tls_failure_keeps_authorization_success_separate(self):
        from app.data.upstox_diagnostic import run_upstox_diagnostic
        from tests.test_upstox_live import KEY

        uri = "wss://feed.upstox.com/v3/feed?code=one-use-secret"

        def connector(*_args, **_kwargs):
            raise ssl.SSLCertVerificationError("certificate verify failed")

        report = run(run_upstox_diagnostic(
            "synthetic-market-token",
            instrument_keys=[KEY],
            test_websocket=True,
            duration=1.0,
            http_transport=_StaticTransport(response=_response(
                200, {"data": {"authorized_redirect_uri": uri}},
            )),
            websocket_connector=connector,
        ))
        assert report["credential_authorization"]["status"] == "authorized"
        assert report["websocket_check"]["handshake"] == "tls_failure"
        assert report["websocket_check"]["handshake_completed"] is False
        assert report["market_data"]["received"] is False


def test_cli_reads_the_local_env_file_without_echoing_or_modifying_it(tmp_path, monkeypatch, capsys):
    from app.data import upstox_diagnostic

    secret = "local-only-synthetic-token"
    env_file = tmp_path / "operator.env"
    original_contents = (
        "MODE=PAPER\n"
        "LIVE_TRADING=false\n"
        "BROKER=LOCAL_PAPER\n"
        f"UPSTOX_ACCESS_TOKEN={secret}\n"
        "MARKET_DATA_SYMBOLS=NSE_EQ|INE002A01018\n"
    )
    env_file.write_text(original_contents, encoding="utf-8")
    monkeypatch.setenv("ARENA_ENV_FILE", str(env_file))
    for key in (
        "MARKET_DATA_ACCESS_TOKEN", "UPSTOX_ACCESS_TOKEN", "MARKET_DATA_SYMBOLS",
        "UPSTOX_INSTRUMENT_KEYS", "DATA_SOURCE", "MODE", "LIVE_TRADING", "BROKER",
    ):
        monkeypatch.delenv(key, raising=False)

    captured = {}
    original = upstox_diagnostic.run_upstox_diagnostic

    async def mocked_diagnostic(token, **kwargs):
        captured["token"] = token
        return await original(
            token,
            authorize_url="https://api.upstox.com/v3/feed/market-data-feed/authorize",
            http_transport=_StaticTransport(response=_response(401)),
            **kwargs,
        )

    monkeypatch.setattr(upstox_diagnostic, "run_upstox_diagnostic", mocked_diagnostic)
    from app.__main__ import main

    with pytest.raises(SystemExit) as excinfo:
        main(["upstox-test", "--no-websocket"])
    assert excinfo.value.code == 1
    output = capsys.readouterr().out
    assert captured["token"] == secret
    assert secret not in output
    assert "Authorization" not in output
    assert "authorized_redirect_uri" not in output
    assert json.loads(output)["https_reachability"]["status"] == "reachable"
    assert env_file.read_text(encoding="utf-8") == original_contents
    assert "MODE=PAPER" in original_contents
    assert "LIVE_TRADING=false" in original_contents
    assert "BROKER=LOCAL_PAPER" in original_contents
