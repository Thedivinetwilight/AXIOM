"""Cross-broker operator readiness report for LIVE execution.

One report for both markets, built from the existing execution boundary
(:mod:`app.markets.live_execution`) — it adds no gate, no journal and no second
order path. Each entry lists:

* the broker and the documented capabilities it is validated against;
* the current mode, gate state and every failed readiness check by name;
* **every required credential by name**, with a boolean "configured" flag and
  never a value, length or prefix;
* the account permissions/registrations the broker requires;
* platform checks that can be evaluated offline;
* the numbered acceptance steps an operator must complete before live trading
  can be called enabled.

Coverage is labelled honestly and in the payload itself:
``mock_only: true``, ``live_verified: false``, ``windows_acceptance: NOT_RUN``.
Nothing in this module contacts a broker.
"""

from __future__ import annotations

import os
import platform
import sys
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.markets.broker_capabilities import DOCUMENTED_AS_OF, capabilities_for
from app.markets.live_execution import MARKETS, MODE_LIVE, LiveExecutionCoordinator

READINESS_SCHEMA = "axiom.live.readiness.v1"

COVERAGE = {
    "mock_or_fake_transport_only": True,
    "live_verified": False,
    "windows_acceptance": "NOT_RUN",
    "note": "tests and readiness prove wiring and refusal behaviour, not broker behaviour in production",
}


# --------------------------------------------------------------------------- #
# Credentials: names and presence only
# --------------------------------------------------------------------------- #
def _configured(name: str) -> bool:
    """True when the environment variable holds a non-empty value. Never returns it."""
    return bool(str(os.environ.get(name, "")).strip())


def _credential_requirements(market: str, adapter: Any) -> list[dict[str, Any]]:
    """Required credential/enablement names for one market, presence only."""
    if market == "NSE":
        rows = [
            {
                "name": "AUTONOMY_LIVE_ENABLED",
                "kind": "enablement",
                "configured": _configured("AUTONOMY_LIVE_ENABLED"),
                "detail": "must be true for the NSE live adapter to be considered enabled",
            },
            {
                "name": "UPSTOX_LIVE_ACCESS_TOKEN",
                "kind": "credential",
                "configured": _adapter_token_present(adapter),
                "detail": "a separate live order token; the read-only market-data token is never reused",
            },
            {
                "name": "UPSTOX_ALGO_NAME",
                "kind": "registration",
                "configured": bool(str(getattr(adapter, "algo_name", "") or "").strip()),
                "detail": "algo name registered with Upstox for SEBI static-IP order tagging",
            },
            {
                "name": "AXIOM_UPSTOX_EXPECTED_PUBLIC_IP",
                "kind": "registration",
                "configured": bool(str(getattr(adapter, "expected_public_ip", "") or "").strip()),
                "detail": "the static public IP registered with Upstox; orders from another IP are refused",
            },
        ]
    else:
        rows = [
            {
                "name": "ALPACA_BROKER_ENABLED",
                "kind": "enablement",
                "configured": _configured("ALPACA_BROKER_ENABLED"),
                "detail": "must be true for the Alpaca broker package to be constructed",
            },
            {
                "name": "ALPACA_BROKER_API_KEY",
                "kind": "credential",
                "configured": _configured("ALPACA_BROKER_API_KEY"),
                "detail": "broker key; the market-data key is separate and never reused for orders",
            },
            {
                "name": "ALPACA_BROKER_API_SECRET",
                "kind": "credential",
                "configured": _configured("ALPACA_BROKER_API_SECRET"),
                "detail": "broker secret; stored in the broker secret store, never logged",
            },
            {
                "name": "ALPACA_BROKER_ENVIRONMENT",
                "kind": "selection",
                "configured": _configured("ALPACA_BROKER_ENVIRONMENT"),
                "detail": "paper | live; the paper environment is the documented non-live environment",
            },
        ]
    return rows


def _adapter_token_present(adapter: Any) -> bool:
    probe = getattr(adapter, "_live_token_present", None)
    if callable(probe):
        try:
            return bool(probe())
        except (RuntimeError, ValueError, KeyError):
            return False
    return False


# --------------------------------------------------------------------------- #
# Account permissions and platform checks
# --------------------------------------------------------------------------- #
def _account_permissions(market: str) -> list[str]:
    if market == "NSE":
        return [
            "Upstox account enabled for API order placement (not read-only)",
            "live order token issued for the current trading day",
            "account funded and permitted to trade the instrument",
            "static IP registered with Upstox; AXIOM refuses orders from another IP",
            "same-day BOD instrument catalog loaded so instrument_keys are authoritative",
        ]
    return [
        "Alpaca account active and able to trade (broker reports the account state)",
        "broker key pair has trading permission, not market-data-only",
        "paper environment selected first; live requires its own explicit activation",
        "account not restricted, no PDT/pattern-day-trade block for the intended activity",
        "clock endpoint reachable so session state comes from the broker",
    ]


def _platform_checks() -> list[dict[str, Any]]:
    tz_ok = True
    for zone in ("Asia/Kolkata", "America/New_York"):
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            tz_ok = False
    return [
        {
            "name": "python_version",
            "ok": sys.version_info >= (3, 11),
            "detail": platform.python_version(),
        },
        {
            "name": "exchange_timezones_available",
            "ok": tz_ok,
            "detail": "zoneinfo data for Asia/Kolkata and America/New_York",
        },
        {
            "name": "native_windows_packaging",
            "ok": False,
            "detail": "not evaluated in this environment; Linux only",
        },
        {
            "name": "broker_host_reachability",
            "ok": False,
            "detail": "deliberately not probed offline: the readiness report must not contact a broker",
        },
    ]


def _acceptance_steps(market: str) -> list[str]:
    broker = capabilities_for(market).broker
    steps = [
        f"Configure every {broker} credential above; confirm none is echoed in logs or the UI.",
        f"Run the {market} readiness probe and resolve every failed check by name.",
        "Configure all risk limits for this market (order value, exposure, daily orders, "
        "daily loss, open positions) - a partial set is refused.",
        "Set mode LIVE for this market only, then Arm; both are explicit operator actions.",
        "Activate with the exact confirmation phrase and the risk acknowledgement.",
        "Exercise the emergency stop and confirm the kill switch blocks new orders for this "
        "market without touching the other market.",
        "Reconcile any unresolved intent and confirm the journal shows the resolution.",
        "Only then is the market 'live enabled' in this process; a restart returns it to "
        "DISABLED and the steps must be repeated.",
    ]
    if market == "NSE":
        steps.append("Verify the SEBI static IP and algo name are registered for this account.")
    else:
        steps.append(
            "Exercise the full cycle against the Alpaca paper environment first; the paper "
            "host is documented, non-live and never used as evidence that live works."
        )
    return steps


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def build_readiness_report(
    coordinator: LiveExecutionCoordinator,
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """One operator report covering both markets. Read-only; no broker call."""
    now = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    markets: list[dict[str, Any]] = []
    for market in MARKETS:
        adapter = coordinator.adapters.get(market)
        readiness = coordinator.readiness(market)
        status = coordinator.status(market)
        capabilities = capabilities_for(market)
        blockers: list[str] = list(readiness.get("failed") or [])
        missing = list(readiness.get("missing_dependencies") or [])
        credentials = _credential_requirements(market, adapter)
        credentials_missing = [row["name"] for row in credentials if not row["configured"]]
        live_enabled = status.get("mode") == MODE_LIVE and bool(
            (status.get("gate") or {}).get("live_available")
        )
        markets.append(
            {
                "market": market,
                "broker": capabilities.broker,
                "broker_label": capabilities.label,
                "mode": status.get("mode"),
                "gate": status.get("gate"),
                "gate_state": (status.get("gate") or {}).get("state"),
                "ready": bool(readiness.get("ok")),
                "live_enabled": live_enabled,
                "failed_checks": blockers,
                "missing_requirements": missing,
                "credential_requirements": credentials,
                "credentials_missing": credentials_missing,
                "account_permissions": _account_permissions(market),
                "platform_checks": _platform_checks(),
                "acceptance_steps": _acceptance_steps(market),
                "documented_capabilities": capabilities.as_dict(),
                "kill_switch": _kill_switch_state(coordinator, market),
            }
        )
    blocking = [row["market"] for row in markets if not row["ready"] or not row["live_enabled"]]
    return {
        "schema": READINESS_SCHEMA,
        "generated_at": now.isoformat(),
        "execution_boundary": "app.markets.live_execution",
        "broadcast_defaults": {
            "live_trading_default": "OFF",
            "modes": "PAPER unless an operator sets LIVE per market",
            "gates_after_restart": "DISABLED",
        },
        "documented_capabilities_as_of": DOCUMENTED_AS_OF,
        "markets": markets,
        "blocking_markets": blocking,
        "all_markets_live_enabled": not blocking,
        "coverage": dict(COVERAGE),
        "open_order_policy": open_order_policy(),
        "notes": [
            "No credential value is included: every entry reports presence only.",
            "This report is read-only and never places, modifies or cancels an order.",
            "A passing readiness report is not a claim that live trading works; only a real "
            "operator-accepted session can establish that.",
        ],
    }


def _kill_switch_state(coordinator: LiveExecutionCoordinator, market: str) -> dict[str, Any]:
    gate = coordinator.adapters[market].gate_status()
    return {
        "engaged": bool(gate.get("kill_engaged")),
        "reason": gate.get("kill_reason"),
        "open_order_policy": open_order_policy(),
    }


def open_order_policy() -> dict[str, Any]:
    """What the kill switch does to orders that are already working."""
    return {
        "blocks_new_orders": True,
        "scope": "the affected market only; the other market keeps its own safeguards",
        "working_orders": "left untouched unless the operator passes cancel_working=true, which "
        "attempts a documented cancel for each working order and records any failure",
        "partial_fills": "an order canceled after a partial fill leaves the filled quantity "
        "filled; the position is shown from broker-reported positions on the next cycle",
        "unresolved_intents": "must be reconciled before the market can submit again; the "
        "kill switch reset does not clear them",
        "reset": "requires an explicit operator acknowledgement and returns the gate to DISABLED",
    }


def render_readiness_text(report: dict[str, Any]) -> str:
    """Compact operator rendering of the readiness report."""
    lines = [
        f"AXIOM live readiness  {report['schema']}  generated {report['generated_at']}",
        f"documented capabilities as of {report['documented_capabilities_as_of']}",
        f"coverage: mock/fake transports only={report['coverage']['mock_or_fake_transport_only']} "
        f"live_verified={report['coverage']['live_verified']} "
        f"windows_acceptance={report['coverage']['windows_acceptance']}",
        "",
    ]
    for row in report["markets"]:
        lines.append(f"{row['market']} - {row['broker_label']}")
        lines.append(f"  mode={row['mode']} gate={row['gate_state']} ready={row['ready']} "
                     f"live_enabled={row['live_enabled']}")
        if row["failed_checks"]:
            lines.append(f"  failed checks: {', '.join(row['failed_checks'])}")
        if row["credentials_missing"]:
            lines.append(f"  missing credentials: {', '.join(row['credentials_missing'])}")
        for item in row["missing_requirements"]:
            lines.append(f"  requirement: {item}")
        lines.append(f"  documented order types: {', '.join(row['documented_capabilities']['order_types'])}")
        lines.append(
            "  idempotency: "
            f"{row['documented_capabilities']['idempotency']['field']} "
            f"(broker rejects duplicates: "
            f"{row['documented_capabilities']['idempotency']['rejects_duplicates']})"
        )
        lines.append("")
    lines.append(f"blocking markets: {', '.join(report['blocking_markets']) or 'none'}")
    lines.append(
        "A readiness report is not a live-verification: no broker order endpoint was contacted."
    )
    return "\n".join(lines)
