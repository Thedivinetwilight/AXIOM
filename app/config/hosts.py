"""Single source of truth for Upstox host classification (stdlib only).

WHY THIS MODULE EXISTS (V2 live-hardening audit, 2026-10-02)
------------------------------------------------------------
The V1 build treated ``api-hft.upstox.com`` as a *sandbox* order host and set it
as the default ``UPSTOX_SANDBOX_BASE_URL``. Upstox's own documentation says the
opposite:

* ``api-hft.upstox.com`` is the **live** order host (the "enhanced URL for
  orders" that replaced ``api.upstox.com`` for place/modify/cancel on the real
  trading platform);
* the sandbox base is ``api-sandbox.upstox.com`` (also seen as
  ``sandbox.upstox.com`` in current docs), enabled by the SDK's
  ``Configuration(sandbox=True)`` flag;
* sandbox access tokens are documented as *"exclusively for sandbox orders and
  cannot be used for live transactions"* — which is a safety property, not a
  guarantee the client respects.

Consequence of the V1 classification: a genuine sandbox token could not place
orders (wrong host), while a *live* token pasted into
``UPSTOX_SANDBOX_ACCESS_TOKEN`` placed **real** orders at the live host while the
broker, the database audit tables, the dashboard and the reports all said
"SANDBOX" / "live_trading: false".

This module is deliberately dependency-free (stdlib only) so
``app/config/settings.py`` can validate hosts without importing ``httpx``. The
core research/replay paths must keep working with the standard library alone.
"""

from urllib.parse import urlparse


def is_trusted_upstox_url(url: str, allowed_hosts, *, expected_path=None) -> bool:
    """Whether a broker endpoint is HTTPS, host-pinned, and structurally safe.

    Broker URLs carry a bearer token. Do not allow a custom base/history URL to
    redirect credentials to an arbitrary host, cleartext HTTP, nonstandard
    port, or URL with embedded user-info/query/fragment data.
    """
    try:
        parsed = urlparse(str(url))
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if (parsed.scheme.lower() != "https"
            or (parsed.hostname or "").lower() not in set(allowed_hosts)
            or parsed.username is not None or parsed.password is not None
            or port not in (None, 443) or parsed.query or parsed.fragment):
        return False
    path = parsed.path or ""
    if expected_path is None:
        return not path.strip("/")
    return path.rstrip("/") == str(expected_path).rstrip("/")


def is_trusted_upstox_request_url(url: str, allowed_hosts) -> bool:
    """Host/scheme validation for an already-built API request URL."""
    try:
        parsed = urlparse(str(url))
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() in set(allowed_hosts)
        and parsed.username is None and parsed.password is None
        and port in (None, 443) and not parsed.fragment)


#: Order-mutating hosts a SANDBOX broker may contact.
SANDBOX_ORDER_HOSTS = frozenset({
    "api-sandbox.upstox.com",  # documented sandbox order host (SDK sandbox=True)
    "sandbox.upstox.com",      # current docs spelling of the sandbox base
})

#: Order-mutating hosts that move REAL money.
LIVE_ORDER_HOSTS = frozenset({
    "api-hft.upstox.com",      # documented enhanced (live) order host
})

#: Auth / read-only / non-order production APIs (market data, history,
#: instrument master, order history reads).
READ_AND_AUTH_HOSTS = frozenset({
    "api.upstox.com",
})

#: A sandbox broker must never reach these, whatever the configuration says.
FORBIDDEN_IN_SANDBOX = READ_AND_AUTH_HOSTS | LIVE_ORDER_HOSTS

#: A live broker must never reach a sandbox host or a non-order production host.
FORBIDDEN_IN_LIVE = READ_AND_AUTH_HOSTS | SANDBOX_ORDER_HOSTS

DEFAULT_SANDBOX_BASE_URL = "https://api-sandbox.upstox.com"
DEFAULT_LIVE_BASE_URL = "https://api-hft.upstox.com"
#: Order-history reads for LIVE orders are a documented production (read-only)
#: endpoint: live orders exist on the production platform, so their history is
#: read from the production host.
DEFAULT_ORDER_HISTORY_URL = "https://api.upstox.com/v2/order/history"
#: SANDBOX order-history reads stay ON THE SANDBOX HOST (verified against the
#: official docs and the official upstox-python SDK sandbox allowlist,
#: 2026-10-16: /v2/order/history is NOT a documented sandbox-enabled API —
#: sandbox supports place/modify/cancel (+multi-place) only). Two rules follow:
#:
#: * a sandbox token must NEVER be sent to the production host
#:   ``api.upstox.com`` (directive: sandbox credentials never reach production
#:   endpoints; the provider documents sandbox tokens as sandbox-only);
#: * if the sandbox environment does not expose the history path, the request
#:   stays inside the sandbox boundary and the broker marks the read side as an
#:   unavailable capability instead of disabling order operations or guessing.
DEFAULT_SANDBOX_ORDER_HISTORY_URL = "https://api-sandbox.upstox.com/v2/order/history"


def host_of(url: str) -> str:
    """Lowercased hostname of ``url`` ("" when it cannot be determined)."""
    return (urlparse(url).hostname or "").lower()
