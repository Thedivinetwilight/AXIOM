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
    "windows_acceptance_evidence": "docs/WINDOWS_ACCEPTANCE_STATUS.md",
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
# Owner-run, read-only broker preflight
# --------------------------------------------------------------------------- #
#: Evidence ladder. A market only moves up a level with evidence recorded for
#: that level; nothing here can be inferred from a lower level.
VERIFICATION_LEVELS: tuple[tuple[str, str], ...] = (
    ("MOCK", "deterministic fake-transport tests in this repository"),
    ("PAPER_SANDBOX", "the broker's own paper/sandbox environment, real API calls, no real money"),
    ("READ_ONLY_LIVE", "live credentials against documented read endpoints only, no order call"),
    ("ORDER_PATH_LIVE", "a real accepted order on the live account, operator-run"),
)

FORBIDDEN_IN_PREFLIGHT: tuple[str, ...] = (
    "placing, modifying or cancelling any order",
    "contacting an order, multi-order or GTT write endpoint",
    "printing, logging or persisting a credential value, token or account number",
    "switching a market to LIVE or arming a gate",
    "treating a passing preflight as evidence that the order path works",
)


def _upstox_preflight() -> list[dict[str, Any]]:
    return [
        {
            "step": "credential_configuration_status",
            "what": "confirm which required names are present without reading their values",
            "how": "python -m app live-readiness --json | jq '.markets[0].credential_requirements'",
            "expect": "configured=true for the names you set; the report never echoes a value",
            "read_only": True,
            "writes_orders": False,
            "level": "MOCK",
            "failure_means": "the adapter stays DISABLED; do not paste a token into a ticket or chat",
        },
        {
            "step": "account_identity",
            "what": "confirm the token belongs to the intended Upstox account",
            "how": "GET /v2/user/profile with the same token AXIOM uses; record only a masked "
            "identifier and the returned user_name/exchanges",
            "expect": "the expected account, and an exchanges list containing NSE",
            "read_only": True,
            "writes_orders": False,
            "level": "READ_ONLY_LIVE",
            "failure_means": "wrong account or token: stop, do not activate",
        },
        {
            "step": "permissions_and_funding",
            "what": "confirm the account may place orders and is funded",
            "how": "GET /v3/user/get-funds-and-margin (Api-Version 3.0) and "
            "GET /v2/portfolio/short-term-positions",
            "expect": "available_to_trade.total > 0, no trading block reported, positions readable",
            "read_only": True,
            "writes_orders": False,
            "level": "READ_ONLY_LIVE",
            "failure_means": "readiness fails and Arm is refused; AXIOM does not try to fix funding",
        },
        {
            "step": "edis_and_settlement_state",
            "what": "confirm holdings an exit would sell are EDIS-authorised and settled",
            "how": "Upstox web/iOS/Android app only: GET /v2/portfolio/long-term-holdings was "
            "checked and returns isin, quantity, t1_quantity, cnc_used_quantity, collateral "
            "quantities and prices - no EDIS or DDPI authorisation field - so this step must be "
            "confirmed in the app against what it shows as authorised for delivery",
            "expect": "operator confirms the app shows the holdings as authorised",
            "read_only": True,
            "writes_orders": False,
            "level": "READ_ONLY_LIVE",
            "failure_means": "no exit route exists in AXIOM anyway (readiness doc section 3); "
            "because EDIS state is unreadable through the documented API, any route depending on "
            "it stays disabled rather than trusting an attestation",
        },
        {
            "step": "market_and_session_eligibility",
            "what": "confirm the NSE session is open and today is a trading day",
            "how": "python -m app live-readiness, then compare the printed session window with "
            "app.markets.nse_reconciliation.nse_session_status for the current time",
            "expect": "session OPEN, trading_day equal to the loaded BOD catalog day",
            "read_only": True,
            "writes_orders": False,
            "level": "MOCK",
            "failure_means": "submissions are refused outside the regular session",
        },
        {
            "step": "platform_acceptance",
            "what": "confirm the SEBI static IP and algo name are registered for this account",
            "how": "Upstox console / SEBI registration records; compare with "
            "AXIOM_UPSTOX_EXPECTED_PUBLIC_IP and UPSTOX_ALGO_NAME",
            "expect": "both registered and identical to the configured values",
            "read_only": True,
            "writes_orders": False,
            "level": "READ_ONLY_LIVE",
            "failure_means": "AXIOM refuses orders from an unregistered IP; do not activate",
        },
    ]


def _alpaca_preflight() -> list[dict[str, Any]]:
    return [
        {
            "step": "credential_configuration_status",
            "what": "confirm which required names are present without reading their values",
            "how": "python -m app live-readiness --json | jq '.markets[1].credential_requirements'",
            "expect": "configured=true for ALPACA_BROKER_API_KEY / _SECRET; values never printed",
            "read_only": True,
            "writes_orders": False,
            "level": "MOCK",
            "failure_means": "the adapter path stays disabled",
        },
        {
            "step": "account_identity",
            "what": "confirm the key pair belongs to the intended Alpaca account",
            "how": "GET /v2/account; record only a masked account_number and the status",
            "expect": "status ACTIVE and the expected account",
            "read_only": True,
            "writes_orders": False,
            "level": "READ_ONLY_LIVE",
            "failure_means": "wrong account: stop, do not activate",
        },
        {
            "step": "permissions_and_blocks",
            "what": "confirm trading permission and that the account is not blocked",
            "how": "the same GET /v2/account response: trading_blocked, account_blocked, "
            "pattern_day_trader and buying_power",
            "expect": "trading_blocked=false, account_blocked=false, buying_power > 0",
            "read_only": True,
            "writes_orders": False,
            "level": "READ_ONLY_LIVE",
            "failure_means": "readiness fails and Arm is refused",
        },
        {
            "step": "market_and_session_eligibility",
            "what": "take session state from the broker, not the local clock",
            "how": "GET /v2/clock and GET /v2/calendar",
            "expect": "is_open agrees with the intended session; next_open/next_close sane",
            "read_only": True,
            "writes_orders": False,
            "level": "READ_ONLY_LIVE",
            "failure_means": "submissions are refused outside the session",
        },
        {
            "step": "platform_acceptance",
            "what": "exercise a full cycle on the paper host before any live claim",
            "how": "set ALPACA_BROKER_ENVIRONMENT=paper, run the same readiness and preflight, "
            "and expect real API calls against the documented paper host",
            "expect": "paper cycle completes; this proves the transport, not the live order path",
            "read_only": False,
            "writes_orders": True,
            "level": "PAPER_SANDBOX",
            "failure_means": "do not attempt live until the paper cycle is clean",
        },
        {
            "step": "position_read_for_the_symbol",
            "what": "confirm the held long an exit would close",
            "how": "GET /v2/positions (read only) for the symbol the coordinator would sell",
            "expect": "qty >= the intended exit quantity, side long",
            "read_only": True,
            "writes_orders": False,
            "level": "READ_ONLY_LIVE",
            "failure_means": "the coordinator refuses the SELL as a naked short",
        },
    ]


def broker_preflight(market: str) -> list[dict[str, Any]]:
    """Ordered, read-only, owner-run steps. Never places, changes or cancels an order."""
    key = str(getattr(market, "value", market)).upper()
    if key == "NSE":
        return _upstox_preflight()
    if key == "NASDAQ":
        return _alpaca_preflight()
    return []


def verification_state(market: str) -> dict[str, Any]:
    """Where the evidence actually stops for this market."""
    levels = []
    for name, meaning in VERIFICATION_LEVELS:
        if name == "MOCK":
            state, evidence = "achieved", "pytest suite (deterministic fakes)"
        elif name == "PAPER_SANDBOX":
            state, evidence = "not_achieved", "no broker paper/sandbox session was run here"
        else:
            state, evidence = "not_achieved", "no live broker session, credential or order"
        levels.append({"level": name, "state": state, "meaning": meaning, "evidence": evidence})
    return {
        "current_level": "MOCK",
        "levels": levels,
        "order_path_verified": False,
        "note": "a level is only achieved with evidence recorded for that level",
    }


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
                "exit_route": capabilities.exit_route.as_dict(),
                "broker_preflight": broker_preflight(market),
                "verification": verification_state(market),
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
        "verification_levels": [name for name, _ in VERIFICATION_LEVELS],
        "preflight_rules": {
            "read_only_required": True,
            "forbidden": list(FORBIDDEN_IN_PREFLIGHT),
            "owner_run": "these steps are for the operator on their own account; AXIOM never runs them",
        },
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
        exit_route = row.get("exit_route") or {}
        lines.append(
            f"  exit route: routed={exit_route.get('routed')} - {exit_route.get('summary', '')}"
        )
        lines.append(f"  verification level: {row['verification']['current_level']} "
                     f"(order path verified: {row['verification']['order_path_verified']})")
        if exit_route.get("operator_fallback"):
            lines.append(f"  exit fallback: {exit_route['operator_fallback']}")
        steps = row.get("broker_preflight") or []
        if steps:
            reads = sum(1 for step in steps if step["read_only"] and not step["writes_orders"])
            lines.append(f"  broker preflight: {len(steps)} owner-run steps, "
                         f"{reads} read-only / no order writes")
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
