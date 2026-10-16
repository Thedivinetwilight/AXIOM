"""Gateway-routed broker facade (FINAL-11/10 §3, §4).

The engine receives THIS object as its broker. Every ``submit()`` call is
turned into a complete :class:`ExecutionIntent` and routed through the
:class:`LiveExecutionGateway` — the single source of truth that decides
whether a real-money order may leave the building. Reads that do not move
money (ledger, orders, reconcile, status) delegate straight to the real
live broker so operational behaviour is unchanged.

This is what enforces "no alternate submission path": the engine cannot
reach the live broker's ``submit`` directly, because it never holds the
live broker — only this facade, whose ONLY submission route is the gateway.
"""

from __future__ import annotations

import time

from app.live.gateway import ExecutionIntent, GatewayRefusal, configuration_hash
from app.live.build_identity import build_identity_string


class GatewayRoutedBroker:
    """Live broker facade: every order goes through the execution gateway."""

    LIVE = True
    MODE = "UPSTOX_LIVE"

    def __init__(self, inner_broker, gateway, *, market_id: str,
                 strategy_id="engine", account_scope: str = "",
                 universe: str = "", clock=time.time):
        self._inner = inner_broker
        self.gateway = gateway
        self.market_id = str(market_id or "")
        # a callable strategy_id tracks the router's active strategy
        self._strategy_id = strategy_id
        self.account_scope = str(account_scope or "")
        self.universe = str(universe or "")
        self.clock = clock

    @property
    def strategy_id(self) -> str:
        sid = self._strategy_id
        if callable(sid):
            try:
                sid = sid()
            except Exception:
                sid = ""
        return str(sid or "engine")

    # ------------------------------------------------------------- submit --
    def submit(self, side: str, quantity: float, price: float, *,
               order_type: str = "MARKET", intent_id: str | None = None,
               symbol: str | None = None,
               instrument_key: str | None = None, **extra) -> str:
        """Authorize via the gateway, then (and only then) submit.

        Raises :class:`GatewayRefusal` if any layer refuses — the engine
        treats that as a failed submission and nothing reaches the broker.
        """
        # Long-only intraday engine: BUY adds risk (ENTRY), SELL reduces it
        # (EXIT). Exits are never gated by the epoch so an invalidated
        # authorization can never trap a held position.
        purpose = "EXIT" if side == "SELL" else "ENTRY"
        epoch = self.gateway.epochs.current() or {}
        signal_id = str(intent_id or extra.get("signal_id") or "")
        signal_ts = extra.get("signal_ts")
        intent_fields = dict(
            purpose=purpose,
            market_id=self.market_id,
            identity=(instrument_key or symbol or self.market_id),
            side=side,
            quantity=float(quantity),
            order_type=order_type,
            price=price,
            account_scope=self.account_scope,
            universe=self.universe,
            strategy_id=self.strategy_id,
            strategy_lineage=self.strategy_id,
            signal_id=signal_id,
            signal_ts=signal_ts,
            configuration_hash=configuration_hash(self.gateway.settings),
            authorization_epoch=(int(epoch.get("epoch"))
                                 if epoch.get("epoch") is not None else None),
            build_identity=build_identity_string(),
        )
        if intent_id is not None:
            intent_fields["intent_id"] = str(intent_id)
        intent = ExecutionIntent(**intent_fields)
        result = self.gateway.submit(intent)   # raises GatewayRefusal
        return str(result.get("order_id", ""))

    def cancel(self, intent_id: str) -> dict:
        """Route cancellation through the gateway; never delegate it directly.

        Cancel is risk-reducing, but it still needs a live lease, valid broker
        identity, emergency-stop semantics and a durable audit record.
        """
        state = getattr(self._inner, "orders", {}).get(str(intent_id))
        if state is None:
            raise ValueError("Unknown live order intent")
        epoch = self.gateway.epochs.current() or {}
        identity = str(getattr(state, "symbol", "") or self.market_id)
        intent = ExecutionIntent(
            purpose="CANCEL", market_id=self.market_id, identity=identity,
            side=str(getattr(state, "side", "")),
            quantity=float(getattr(state, "quantity", 0) or 0),
            order_type="MARKET", price=float(getattr(state, "price", 0) or 0),
            account_scope=self.account_scope or self.gateway.account_scope,
            universe=self.universe, strategy_id=self.strategy_id,
            strategy_lineage=self.strategy_id,
            signal_id="cancel:" + str(intent_id), signal_ts=self.clock(),
            configuration_hash=configuration_hash(self.gateway.settings),
            authorization_epoch=(int(epoch.get("epoch"))
                                 if epoch.get("epoch") is not None else None),
            build_identity=build_identity_string(),
            cancel_order_id=str(intent_id))
        result = self.gateway.submit(intent)
        return result.get("cancelled") or {"status": "UNKNOWN"}

    def modify(self, *_args, **_kwargs):
        """Fail closed until modifications have an explicit gateway intent."""
        raise GatewayRefusal(
            "BROKER IDENTITY",
            "live order modification is unavailable without a gateway-authorized intent")

    # --------------------------------------- non-submission passthrough ----
    def __getattr__(self, name):
        # Only reached for attributes NOT found on the facade. Order submission
        # and cancellation are explicit methods above; neither can pass through.
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (f"GatewayRoutedBroker(market={self.market_id}, "
                f"strategy={self.strategy_id})")


__all__ = ["GatewayRoutedBroker", "ExecutionIntent", "GatewayRefusal"]
