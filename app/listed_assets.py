"""Fail-closed boundary for listed shares/ETFs.

MT5/Vantage instruments never enter this module. A listed identity requires an
explicit exchange and currency; a ticker alone is not a routable identity.
No execution adapter is shipped or implied by this interface.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol

from app.data.alpaca import validate_us_symbol


class ListedProviderUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ListedAssetIdentity:
    symbol: str
    exchange: str
    currency: str
    product_type: str

    def __post_init__(self):
        symbol = validate_us_symbol(self.symbol)
        exchange = self.exchange.strip().upper()
        currency = self.currency.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,24}", exchange):
            raise ValueError("An explicit listed exchange/MIC is required")
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("A three-letter listed trading currency is required")
        if self.product_type not in {"LISTED_SHARE", "LISTED_ETF"}:
            raise ValueError("This boundary accepts only LISTED_SHARE or LISTED_ETF")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "currency", currency)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange,
            "currency": self.currency,
            "product_type": self.product_type,
            "asset_key": f"LISTED:{self.exchange}:{self.symbol}:{self.currency}",
            "ownership": "Underlying listed instrument; not a CFD",
        }


class ListedMarketDataProvider(Protocol):
    """Contract for a separately entitled listed reference/quote provider."""

    name: str

    def status(self) -> dict: ...

    def snapshot(self, identity: ListedAssetIdentity) -> dict: ...


class ListedAssetService:
    def __init__(self, data_provider: ListedMarketDataProvider | None = None, *, execution_provider=None):
        if execution_provider is not None:
            raise ListedProviderUnavailable(
                "No listed execution adapter is certified in this build; refusing execution routing"
            )
        self.data_provider = data_provider
        self.execution_provider = None

    def status(self) -> dict:
        configured = self.data_provider is not None
        provider_status = self.data_provider.status() if configured else {}
        return {
            "interface": "listed-assets-v1",
            "identity_contract": "symbol + explicit exchange/MIC + currency + LISTED_SHARE/LISTED_ETF",
            "data_provider": getattr(self.data_provider, "name", None),
            "data_configured": configured,
            "data_status": provider_status,
            "execution_provider": None,
            "execution_configured": False,
            "execution_supported": False,
            "mt5_routing_allowed": False,
            "cfd_substitution_allowed": False,
            "note": (
                "No listed execution adapter is available. Configure and verify a separate "
                "listed provider before requesting data; never substitute a Vantage CFD."
            ),
            "setup_document": "docs/LISTED_ASSETS.md",
        }

    def routing_policy(self, identity: ListedAssetIdentity) -> dict:
        return {
            "identity": identity.to_dict(),
            "data": {
                "provider": getattr(self.data_provider, "name", None),
                "allowed": self.data_provider is not None,
                "reason": None if self.data_provider else "No entitled listed market-data provider is configured",
            },
            "execution": {
                "provider": None,
                "allowed": False,
                "reason": "Listed execution is not implemented or verified; local paper is the only AXIOM fill path",
            },
            "vantage_mt5_allowed": False,
            "order_created": False,
            "read_only_policy_check": True,
        }

    def snapshot(self, identity: ListedAssetIdentity) -> dict:
        if self.data_provider is None:
            raise ListedProviderUnavailable(
                "Listed data is unconfigured; see docs/LISTED_ASSETS.md. No Vantage/CFD fallback exists."
            )
        result = self.data_provider.snapshot(identity)
        if not isinstance(result, dict) or result.get("asset_key") != identity.to_dict()["asset_key"]:
            raise ListedProviderUnavailable("Listed provider returned an invalid or mismatched asset identity")
        return result
