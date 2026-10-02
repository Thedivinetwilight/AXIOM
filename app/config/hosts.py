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
#: Order-history reads are a documented production (read-only) endpoint for both
#: modes; sandbox order history is not part of the documented sandbox API
#: surface, so sandbox reconciliation may legitimately return no rows.
DEFAULT_ORDER_HISTORY_URL = "https://api.upstox.com/v2/order/history"


def host_of(url: str) -> str:
    """Lowercased hostname of ``url`` ("" when it cannot be determined)."""
    return (urlparse(url).hostname or "").lower()
