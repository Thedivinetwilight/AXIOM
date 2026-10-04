"""Upstox LIVE (real-money) broker — the only code path that moves real money.

This subclass exists so that real-money routing is (a) reachable at all, and
(b) *always truthfully labelled*. In V1 the sandbox broker defaulted to the live
order host, so real orders could be placed while the broker, the audit tables,
the dashboard and the reports all said "SANDBOX" / "live_trading: false".

Design rules enforced here:
- ONLY a documented live order host may be used (``api-hft.upstox.com``);
  sandbox and auth/read hosts are refused at construction.
- ``MODE`` / ``LIVE`` / ``repr`` / ``status_view`` state the truth, so an
  operator reading logs, the API or the database cannot be misled.
- Orders and transitions are written to the dedicated ``live_orders`` /
  ``live_transitions`` tables, never into the sandbox tables.
- Construction ALSO demands an explicit, non-empty ``confirm`` argument, so a
  config mistake cannot instantiate a live broker by accident.

The four-switch configuration gate (MODE=LIVE + LIVE_TRADING=true +
BROKER=UPSTOX_LIVE + LIVE_CONFIRM) and the non-zero risk-limit requirement live
in ``app/config/settings.py`` and are validated before this class is reachable
from the CLI.
"""

import logging

from app.brokers.sandbox import SandboxLedger, UpstoxSandboxBroker
from app.config.settings import LIVE_CONFIRM_PHRASE
from app.config.hosts import (
    DEFAULT_LIVE_BASE_URL,
    DEFAULT_ORDER_HISTORY_URL,
    FORBIDDEN_IN_LIVE,
    LIVE_ORDER_HOSTS,
    host_of,
)

logger = logging.getLogger("bot.live")


class UpstoxLiveBroker(UpstoxSandboxBroker):
    """REAL-MONEY Upstox order client (place/modify/cancel at the live host).

    Same audited, idempotent reconciliation machinery as the sandbox broker —
    different host, different audit tables, and a hard requirement that the
    operator has explicitly confirmed real-money routing.
    """

    MODE = "UPSTOX_LIVE"
    LIVE = True
    ALLOWED_HOSTS = LIVE_ORDER_HOSTS
    FORBIDDEN_HOSTS = FORBIDDEN_IN_LIVE
    DEFAULT_BASE_URL = DEFAULT_LIVE_BASE_URL

    def __init__(
        self,
        access_token: str,
        instrument_key: str,
        *,
        confirm: str = "",
        base_url: str = DEFAULT_LIVE_BASE_URL,
        history_url: str = DEFAULT_ORDER_HISTORY_URL,
        product: str = "I",
        tag: str = "arena-live",
        repository=None,
        initial_capital: float = 100_000.0,
        http_timeout: float = 20.0,
        transport=None,
        algo_name: str = "",
    ):
        if str(confirm) != LIVE_CONFIRM_PHRASE:
            # Defense in depth (FINAL-11/10 §16): the broker boundary demands
            # the EXACT phrase — not "non-empty", not case-insensitive, no
            # whitespace forgiveness. A config mistake cannot instantiate a
            # live broker.
            raise ValueError(
                "UpstoxLiveBroker places REAL orders and requires the EXACT "
                f"confirmation phrase (LIVE_CONFIRM={LIVE_CONFIRM_PHRASE}); "
                "the supplied value does not match it precisely"
            )
        super().__init__(
            access_token, instrument_key,
            base_url=base_url, history_url=history_url, product=product, tag=tag,
            repository=repository, initial_capital=initial_capital,
            http_timeout=http_timeout, transport=transport, algo_name=algo_name,
        )
        # Truthful, unmissable startup record. Never contains the token.
        logger.warning(
            "LIVE_BROKER_READY",
            extra={"instrument_key": self.instrument_key,
                   "base_url": self._base_url,
                   "product": self._product, "tag": self._tag},
        )

    # -- audit routing: real money never lands in the sandbox tables --------

    def _record_order(self, record: dict) -> None:
        if self.repository is None:
            return
        self.repository.record_live_order(record)

    def _record_transition(self, order_id: str, upstox_order_id: str | None,
                           status: str, filled_quantity: float | None,
                           average_price: float | None, ts: str, raw: dict) -> bool:
        if self.repository is None:
            return True
        return self.repository.record_live_transition(
            order_id, upstox_order_id, status, filled_quantity, average_price, ts, raw)

    def status_view(self) -> dict:
        view = super().status_view()
        view.update(live=True, mode=self.MODE,
                    warning="REAL MONEY: live Upstox order host")
        return view


def live_broker_settings_ok(settings) -> tuple[bool, str]:
    """Read-only summary used by preflight output (never constructs a broker)."""
    host = host_of(settings.upstox_live_base_url)
    ok = (
        settings.broker.upper() == "UPSTOX_LIVE"
        and host in LIVE_ORDER_HOSTS
        and bool(settings.upstox_live_access_token.strip())
    )
    return ok, f"broker={settings.broker.upper()} host={host}"

# SandboxLedger is re-exported so callers that only import from this module can
# still build a ledger in tests; keeps the live surface self-describing.
__all__ = ["UpstoxLiveBroker", "SandboxLedger", "live_broker_settings_ok"]
