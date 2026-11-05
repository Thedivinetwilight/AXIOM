"""Runtime manager for Alpaca Broker credentials, risk limits, and service binding.

Keeps LIVE execution disabled by default on every startup, restart, recovery,
and credential change. Read-only market data and local Live-Market Paper remain
completely independent of this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.alpaca_broker.client import (
    AlpacaReadOnlyClient,
    AlpacaTradingClient,
    BrokerError,
)
from app.alpaca_broker.credentials import (
    BROKER_KEYS,
    BrokerSecretNotFound,
    MemoryBrokerSecretStore,
    default_store as default_broker_store,
    load_broker_credentials,
)
from app.alpaca_broker.journal import AlpacaBrokerJournal

SLOT_PAPER, SLOT_LIVE = BROKER_KEYS
from app.alpaca_broker.service import (
    AlpacaBrokerService,
    BrokerRiskLimits,
    LIVE_CONFIRM_PHRASE,
)
from app.config.settings import Settings
from app.markets.us_calendar import USEquitiesCalendar


class AlpacaBrokerRuntime:
    """Manages protected credential slots, risk limits, and gated service instances."""

    def __init__(
        self,
        *,
        settings: Settings,
        secret_dir: Path | None,
        journal_path: Path | None,
        repository=None,
        quote_provider: Callable[[str], dict[str, Any] | None] | None = None,
        asset_lookup: Callable[[str], dict[str, Any]] | None = None,
        calendar: USEquitiesCalendar | None = None,
    ) -> None:
        self.settings = settings
        self.store = MemoryBrokerSecretStore() if secret_dir is None else default_broker_store(secret_dir)
        self.journal = AlpacaBrokerJournal(journal_path) if journal_path is not None else None
        self.repository = repository
        self.quote_provider = quote_provider
        self.asset_lookup = asset_lookup
        self.calendar = calendar or USEquitiesCalendar()
        self.enabled: bool = bool(settings.alpaca_broker_enabled)
        self.environment: str = (
            settings.alpaca_broker_environment
            if settings.alpaca_broker_environment in {"paper", "live"}
            else "paper"
        )
        self.limits = BrokerRiskLimits(
            max_order_value=float(settings.alpaca_broker_max_order_value or 10_000.0 if settings.alpaca_broker_enabled else settings.alpaca_broker_max_order_value),
            max_open_positions=int(settings.alpaca_broker_max_open_positions or 5 if settings.alpaca_broker_enabled else settings.alpaca_broker_max_open_positions),
            max_daily_orders=int(settings.alpaca_broker_max_daily_orders or 20 if settings.alpaca_broker_enabled else settings.alpaca_broker_max_daily_orders),
        )
        self.credential_source: str = "none"
        self.service: AlpacaBrokerService | None = None
        if self.enabled:
            self._rebuild_service()

    def _rebuild_service(self) -> AlpacaBrokerService:
        if self.service is not None:
            self.service.disarm("service rebuild / configuration update")
        creds, source = load_broker_credentials(self.store, self.environment, self.settings)
        self.credential_source = source
        has_creds = bool(creds["api_key"] and creds["api_secret"])

        write_client = None
        live_factory = None
        read_client = None
        if has_creds:
            read_client = AlpacaReadOnlyClient(
                creds["api_key"],
                creds["api_secret"],
                environment=self.environment,
            )
            if self.environment == "paper":
                write_client = AlpacaTradingClient(
                    creds["api_key"],
                    creds["api_secret"],
                    environment="paper",
                )
            else:
                # LIVE never binds a write client until Arm + Activate mints a LivePermit.
                key_val, secret_val = creds["api_key"], creds["api_secret"]
                live_factory = lambda permit: AlpacaTradingClient(  # noqa: E731
                    key_val,
                    secret_val,
                    environment="live",
                    live_permit=permit,
                )

        self.service = AlpacaBrokerService(
            write_client,
            environment=self.environment,
            limits=self.limits,
            repository=self.repository,
            asset_lookup=self.asset_lookup,
            client_factory=live_factory,
            read_only_client=read_client,
            journal=self.journal,
            quote_provider=self.quote_provider,
            calendar=self.calendar,
            require_market_price_bound=True,
        )
        return self.service

    def credential_status(self) -> dict[str, Any]:
        paper_creds, paper_source = load_broker_credentials(self.store, "paper", self.settings)
        live_creds, live_source = load_broker_credentials(self.store, "live", self.settings)
        return {
            "enabled": self.enabled,
            "environment": self.environment,
            "live_master_flag": bool(self.settings.alpaca_broker_enabled),
            "live_confirmation_phrase": LIVE_CONFIRM_PHRASE,
            "paper": {
                "slot": SLOT_PAPER,
                "present": bool(paper_creds["api_key"] and paper_creds["api_secret"]),
                "source": paper_source,
                "secret_returned": False,
            },
            "live": {
                "slot": SLOT_LIVE,
                "present": bool(live_creds["api_key"] and live_creds["api_secret"]),
                "source": live_source,
                "secret_returned": False,
                "real_money": True,
            },
            "risk_limits": self.limits.to_dict(),
        }

    def save_credentials(self, environment: str, api_key: str, api_secret: str) -> dict[str, Any]:
        if environment not in {"paper", "live"}:
            raise ValueError("environment must be 'paper' or 'live'")
        key_clean = (api_key or "").strip()
        secret_clean = (api_secret or "").strip()
        if not key_clean or not secret_clean:
            raise ValueError("Both api_key and api_secret are required")
        slot = SLOT_PAPER if environment == "paper" else SLOT_LIVE
        if self.service is not None:
            self.service.disarm("credential updated")
        self.store.put(slot, {"api_key": key_clean, "api_secret": secret_clean})
        if self.enabled and self.environment == environment:
            self._rebuild_service()
        return self.credential_status()

    def delete_credentials(self, environment: str) -> dict[str, Any]:
        slot = SLOT_PAPER if environment == "paper" else SLOT_LIVE
        if self.service is not None:
            self.service.disarm("credential deleted")
        had_key = True
        try:
            self.store.get(slot)
        except BrokerSecretNotFound:
            had_key = False
        self.store.delete(slot)
        if self.enabled and self.environment == environment:
            self._rebuild_service()
        return {"deleted": had_key, **self.credential_status()}

    def update_limits(
        self,
        *,
        max_order_value: float,
        max_open_positions: int,
        max_daily_orders: int,
        max_daily_loss: float = 2_500.0,
        max_symbol_exposure: float = 50_000.0,
        max_total_exposure: float = 200_000.0,
        max_quote_age_seconds: float = 60.0,
        max_clock_skew_seconds: float = 30.0,
    ) -> dict[str, Any]:
        self.limits = BrokerRiskLimits(
            max_order_value=float(max_order_value),
            max_open_positions=int(max_open_positions),
            max_daily_orders=int(max_daily_orders),
            max_daily_loss=float(max_daily_loss),
            max_symbol_exposure=float(max_symbol_exposure),
            max_total_exposure=float(max_total_exposure),
            max_quote_age_seconds=float(max_quote_age_seconds),
            max_clock_skew_seconds=float(max_clock_skew_seconds),
        )
        if self.service is not None:
            self.service.disarm("risk limits updated")
            self.service.limits = self.limits
        return self.limits.to_dict()

    def configure_mode(self, *, enabled: bool, environment: str = "paper") -> dict[str, Any]:
        if environment not in {"paper", "live"}:
            raise ValueError("environment must be 'paper' or 'live'")
        if environment == "live" and enabled and not self.settings.alpaca_broker_enabled:
            raise PermissionError(
                "ALPACA_LIVE real-money mode is locked by environment policy: "
                "set ALPACA_BROKER_ENABLED=true in environment before arming LIVE."
            )
        if self.service is not None:
            self.service.disarm("mode changed")
        self.enabled = bool(enabled)
        self.environment = environment
        if self.enabled:
            if not self.limits.configured():
                self.limits = BrokerRiskLimits(
                    max_order_value=10_000.0,
                    max_open_positions=5,
                    max_daily_orders=20,
                )
            self._rebuild_service()
            return self.service.status()
        self.service = None
        return {"enabled": False, "mode": "DISABLED", "gate": {"state": "DISABLED"}}

    def probe_readonly(self, environment: str | None = None, *, transport=None) -> dict[str, Any]:
        """Harmless read-only broker connection check (GET /v2/account + /v2/clock)."""
        import httpx

        env = environment if environment in {"paper", "live"} else self.environment
        creds, source = load_broker_credentials(self.store, env, self.settings)
        if not creds["api_key"] or not creds["api_secret"]:
            return {
                "ok": False,
                "provider": f"alpaca_broker_{env}",
                "environment": env,
                "connection_state": "DISCONNECTED",
                "verdict": "missing_credentials",
                "credential_present": False,
                "credential_source": source,
                "real_money": env == "live",
                "order_endpoints_used": False,
                "guidance": (
                    f"Save distinct Alpaca Broker {'LIVE' if env == 'live' else 'PAPER'} "
                    "credentials in Connections before testing broker readiness."
                ),
            }
        http_client = httpx.Client(transport=transport, timeout=15.0) if transport is not None else None
        reader = AlpacaReadOnlyClient(
            creds["api_key"],
            creds["api_secret"],
            environment=env,
            client=http_client,
        )
        try:
            account = reader.get_account()
            clock = reader.get_clock()
        except BrokerError as exc:
            msg = str(exc)
            state = "REJECTED" if ("401" in msg or "403" in msg or "refused" in msg) else "UNAVAILABLE"
            return {
                "ok": False,
                "provider": f"alpaca_broker_{env}",
                "environment": env,
                "endpoint_host": reader.endpoint_host,
                "connection_state": state,
                "verdict": "rejected" if state == "REJECTED" else "unreachable",
                "credential_present": True,
                "credential_source": source,
                "real_money": env == "live",
                "order_endpoints_used": False,
                "detail": msg[:240],
                "guidance": (
                    "Verify that you entered Alpaca Trading API keys for the matching "
                    f"{env.upper()} account (paper keys fail on live and vice versa)."
                ),
            }
        finally:
            reader.close()

        trading_allowed = not (
            account.get("trading_blocked")
            or account.get("account_blocked")
            or account.get("trade_suspended_by_user")
        )
        return {
            "ok": trading_allowed,
            "provider": f"alpaca_broker_{env}",
            "environment": env,
            "endpoint_host": reader.endpoint_host,
            "connection_state": "CONNECTED_READ_ONLY" if trading_allowed else "UNAVAILABLE",
            "verdict": "account_ok" if trading_allowed else "account_restricted",
            "credential_present": True,
            "credential_source": source,
            "real_money": env == "live",
            "order_endpoints_used": False,
            "account": account,
            "clock": clock,
            "guidance": (
                f"Authenticated read-only against {reader.endpoint_host}; zero orders sent."
                if trading_allowed
                else "Account authenticated, but trading is currently blocked/restricted at Alpaca."
            ),
        }
