"""Live configuration check — run this BEFORE ``live-preflight``.

WHY THIS EXISTS
---------------
``python -m app live-preflight`` is the authority on live readiness, but it can
only run once the configuration is good enough to build a ``Settings`` object.
A half-filled ``.env`` therefore fails in one of three unhelpful ways:

* a ``ValueError`` from ``Settings`` naming the *first* problem (never the
  rest), or
* a startup crash deep in another module (a mistyped boolean, an unparseable
  number), or
* worst of all, **nothing** — a key typo (``MAX_POSITION_SIZE`` for
  ``MAX_POSITION_VALUE``) is silently ignored, so the operator believes a limit
  is set when it is not.

This tool reads the configuration the same way the app does, reports *every*
problem at once, and never prints a secret. It is deliberately offline: it
makes no network call and places no order.

It reuses the application's own machinery rather than re-implementing it, so it
cannot drift from the code it describes:

* ``coerce_env_value`` for parsing (identical to ``Settings.from_env``),
* ``Settings(...)`` construction for the final verdict,
* ``host_of`` / ``LIVE_ORDER_HOSTS`` / ``FORBIDDEN_IN_LIVE`` for host checks,
* ``live_timeframe_error`` / ``market_data_mode_error`` for the engine gates.

``env_file`` discovery mirrors the app: it reads ``ROOT/.env`` because that is
what ``Settings.from_env`` loads. Exported environment variables take
precedence, exactly as they do at runtime (``load_dotenv(override=False)``).

Usage::

    python tools/live_config_check.py                    # check ./.env
    python tools/live_config_check.py --env-file /tmp/candidate.env
    python tools/live_config_check.py --json             # machine-readable
    python tools/live_config_check.py --target sandbox   # check sandbox setup

Exit code 0 when nothing blocks, 1 when a BLOCKER is found, 2 when the
configuration is not aiming at live/sandbox at all (nothing to check).

Findings are graded precisely:

* ``BLOCKER`` — the app itself refuses to start, or the value is unsafe.
* ``WARNING`` — the app starts, but the configuration is one you probably do
  not want.
* ``NOTE``    — cannot be verified from configuration (e.g. the static IP
  registration Upstox requires).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.hosts import (  # noqa: E402
    FORBIDDEN_IN_LIVE,
    FORBIDDEN_IN_SANDBOX,
    LIVE_ORDER_HOSTS,
    SANDBOX_ORDER_HOSTS,
    host_of,
)
from app.config.settings import (  # noqa: E402
    LIVE_CONFIRM_PHRASE,
    Settings,
    coerce_env_value,
)

BLOCKER = "BLOCKER"
WARNING = "WARNING"
NOTE = "NOTE"

def _default_env_file():
    """The .env the application will actually load.

    Not simply ROOT/.env: a packaged Arena reads a per-user file, so checking
    the repository one would validate a configuration the app never sees.
    """
    from app import runtime

    return runtime.env_file()


DEFAULT_ENV_FILE = _default_env_file()

#: Documented Upstox-named aliases; a mirror of the mapping inside
#: ``Settings.from_env`` (explicit MARKET_DATA_* variables always win).
ENV_ALIASES = {
    "market_data_access_token": "UPSTOX_ACCESS_TOKEN",
    "market_data_symbols": "UPSTOX_INSTRUMENT_KEYS",
}

#: A key whose name contains one of these is never printed, only described.
SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "APIKEY", "PASSWD")

#: Values that mean "not filled in yet".
PLACEHOLDER_MARKERS = (
    "CHANGE", "YOUR_", "YOUR-", "YOURTOKEN", "PLACEHOLDER", "TODO", "XXXX",
    "REHEARSAL-FAKE", "NOT-A-CREDENTIAL", "EXAMPLE", "<",
)

#: The paper database a live session must not share (audit rows for real money
#: and simulated money in one table is exactly the kind of untruth the project
#: refuses elsewhere).
PAPER_DATABASE_SUFFIX = "trading_bot.db"

MAX_UNKNOWN_KEYS_REPORTED = 8


# --------------------------------------------------------------------------- #
# env file handling
# --------------------------------------------------------------------------- #
def read_env_file(path: Path) -> dict[str, str]:
    """Values from an env file.

    Uses ``python-dotenv`` when installed; otherwise a minimal stdlib parser,
    because the core product must keep working without third-party packages.
    """
    if not path.exists():
        return {}
    try:
        from dotenv import dotenv_values
    except ImportError:
        return _minimal_env_parse(path)
    return {key: value for key, value in dotenv_values(path).items()
            if value is not None}


def _minimal_env_parse(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def effective_values(env_file: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(file values, the values the app would actually see)``.

    The app loads ``ROOT/.env`` with ``override=False``, so an exported
    environment variable beats a file entry. Mirroring that here means the
    report describes what ``python -m app engine`` will really use.
    """
    file_values = read_env_file(env_file)
    effective = dict(file_values)
    effective.update({key: value for key, value in os.environ.items()
                      if isinstance(value, str)})
    return file_values, effective


def known_env_keys() -> set[str]:
    """Every environment key the application actually reads."""
    keys = {field.name.upper() for field in fields(Settings)}
    keys |= set(ENV_ALIASES.values())
    return keys


def coerce_settings_values(
    effective: dict[str, str],
) -> tuple[dict[str, object], list[tuple[str, str]]]:
    """Parse raw values with the app's own converter.

    Returns ``(values, errors)``; a value that cannot be parsed falls back to
    its default so the remaining checks can still run.
    """
    defaults = Settings()
    hints = get_type_hints(Settings)
    values: dict[str, object] = {}
    errors: list[tuple[str, str]] = []
    for field in fields(Settings):
        name = field.name
        annotation = hints.get(name, field.type)
        raw = effective.get(name.upper())
        if raw is None and name in ENV_ALIASES:
            raw = effective.get(ENV_ALIASES[name])
        if raw is None:
            values[name] = coerce_env_value(
                name, annotation, getattr(defaults, name))
            continue
        try:
            values[name] = coerce_env_value(name, annotation, raw)
        except ValueError as exc:
            errors.append((name.upper(), str(exc)))
            values[name] = coerce_env_value(
                name, annotation, getattr(defaults, name))
    return values, errors


# --------------------------------------------------------------------------- #
# findings
# --------------------------------------------------------------------------- #
def finding(level: str, key: str, message: str, hint: str = "") -> dict:
    return {"level": level, "key": key, "message": message, "hint": hint}


def _positive(values: dict, name: str) -> bool:
    try:
        return float(values.get(name) or 0) > 0
    except (TypeError, ValueError):
        return False


def _inspect_secret(findings: list[dict], key: str, value: object) -> None:
    """Placeholder / whitespace checks — never reveals the value itself."""
    text = str(value or "")
    if not text.strip():
        return
    upper = text.upper()
    if any(marker in upper for marker in PLACEHOLDER_MARKERS):
        findings.append(finding(
            WARNING, key, "looks like an unfilled placeholder, not a credential",
            "replace it with the real value from your Upstox app"))
    if text != text.strip() or "\n" in text or "\r" in text:
        findings.append(finding(
            WARNING, key,
            "has surrounding whitespace or a line break — it is sent verbatim",
            "trim the value; a trailing space is a valid-looking but wrong token"))


def _check_unknown_keys(
    findings: list[dict], file_values: dict[str, str],
) -> None:
    """Keys the app never reads are silently ignored — usually a typo."""
    known = known_env_keys()
    unknown = sorted(key for key in file_values if key not in known)
    for key in unknown[:MAX_UNKNOWN_KEYS_REPORTED]:
        findings.append(finding(
            WARNING, key, "not a setting the application reads (silently ignored)",
            "check the spelling — a mistyped limit looks configured but is not"))
    if len(unknown) > MAX_UNKNOWN_KEYS_REPORTED:
        findings.append(finding(
            WARNING, "(more)", f"{len(unknown) - MAX_UNKNOWN_KEYS_REPORTED} further "
            "unrecognised keys are not shown"))


def _engine_gate_findings(values: dict) -> list[dict]:
    """The two gates the engine enforces at startup (shared helpers)."""
    from app.data.factory import live_timeframe_error, market_data_mode_error

    findings: list[dict] = []
    probe = SimpleNamespace(
        default_timeframe=values.get("default_timeframe"),
        market_data_mode=values.get("market_data_mode"),
    )
    message = live_timeframe_error(probe, "UPSTOX_LIVE")
    if message:
        findings.append(finding(BLOCKER, "DEFAULT_TIMEFRAME", message))
    message = market_data_mode_error(probe)
    if message:
        findings.append(finding(BLOCKER, "MARKET_DATA_MODE", message))
    return findings


def _market_data_findings(values: dict, findings: list[dict],
                          *, require_upstox: bool) -> None:
    if require_upstox and str(values.get("data_source", "")).lower() != "upstox":
        findings.append(finding(
            BLOCKER, "DATA_SOURCE",
            f"DATA_SOURCE is {values.get('data_source')!r}, not 'upstox'",
            "real orders need the real market-data feed"))
    symbols = [item.strip() for item in
               str(values.get("market_data_symbols", "")).split(",")
               if item.strip()]
    if not symbols:
        findings.append(finding(
            BLOCKER, "MARKET_DATA_SYMBOLS", "no instrument is configured",
            "start with ONE liquid instrument, e.g. NSE_EQ|INE002A01018"))
    elif len(symbols) > 1:
        findings.append(finding(
            WARNING, "MARKET_DATA_SYMBOLS",
            f"{len(symbols)} instruments configured",
            "start with one; add instruments only once the pipeline is proven"))
    if not str(values.get("market_data_access_token", "")).strip():
        findings.append(finding(
            BLOCKER, "MARKET_DATA_ACCESS_TOKEN", "no market-data token set"))


def _ops_findings(values: dict, findings: list[dict]) -> None:
    """Rails that are inert unless configured — the app allows these silently."""
    if not _positive(values, "live_canary_max_notional"):
        findings.append(finding(
            WARNING, "LIVE_CANARY_MAX_NOTIONAL", "0 — the canary ramp is off",
            "cap the notional of the first entries so a misconfiguration "
            "cannot trade full size immediately"))
    if not _positive(values, "live_max_equity_drawdown"):
        findings.append(finding(
            WARNING, "LIVE_MAX_EQUITY_DRAWDOWN", "0 — the drawdown auto-stop is inert",
            "set a fraction, e.g. 0.05"))
    if values.get("live_auto_stop_on_breach") is not True:
        findings.append(finding(
            WARNING, "LIVE_AUTO_STOP_ON_BREACH", "false — a breach will not stop trading"))
    if not _positive(values, "live_token_max_age_hours"):
        findings.append(finding(
            WARNING, "LIVE_TOKEN_MAX_AGE_HOURS",
            "0 — the credential-freshness check is disabled",
            "a daily-expiring token is worth verifying"))


def _access_findings(values: dict, findings: list[dict]) -> None:
    """Reachability of the console and of a person — true in every broker mode."""
    if not str(values.get("alert_webhook_url", "")).strip():
        findings.append(finding(
            WARNING, "ALERT_WEBHOOK_URL", "not set",
            "without it a CRITICAL alert is only visible on the console you "
            "may not be watching"))
    if not str(values.get("api_token", "")).strip():
        findings.append(finding(
            WARNING, "API_TOKEN", "not set",
            "the dashboard and every protected endpoint return 503 without it"))
    if str(values.get("database_url", "")).rstrip("/").endswith(PAPER_DATABASE_SUFFIX):
        findings.append(finding(
            WARNING, "DATABASE_URL", "points at the paper database",
            "use a dedicated database: real-money and simulated audit rows in "
            "one table make the record ambiguous"))


def _live_notes() -> list[dict]:
    return [
        finding(NOTE, "STATIC_IP",
                "a static IP registered with Upstox is mandatory for order APIs "
                "(SEBI/NSE, in force since 2025-10-01)",
                "primary + backup, changeable once a week, registered on your "
                "Upstox Apps page — this cannot be verified from configuration"),
        finding(NOTE, "TOKEN_EXPIRY",
                "Upstox live access tokens expire daily",
                "python -m app upstox-login refreshes it; live-preflight is "
                "what records freshness"),
        finding(NOTE, "NEXT", "next command: python -m app live-preflight",
                "it is read-only and must report readiness.ok: true"),
    ]


def target_findings(target: str, values: dict, findings: list[dict],
                    unparsed: set[str] | None = None) -> None:
    """The target-specific checks (mutates ``findings``).

    ``unparsed`` holds keys the app could not parse; their values in
    ``values`` are fallback defaults, so saying anything further about them
    would describe a value the operator did not write.
    """
    unparsed = unparsed or set()
    if target == "live":
        if str(values.get("mode", "")).upper() != "LIVE":
            findings.append(finding(
                BLOCKER, "MODE", f"MODE is {values.get('mode')!r}, not LIVE",
                "one of the four independent live switches"))
        if "LIVE_TRADING" not in unparsed and values.get("live_trading") is not True:
            findings.append(finding(
                BLOCKER, "LIVE_TRADING",
                f"LIVE_TRADING is {values.get('live_trading')!r}, not true"))
        if str(values.get("live_confirm", "")).strip() != LIVE_CONFIRM_PHRASE:
            findings.append(finding(
                BLOCKER, "LIVE_CONFIRM",
                "does not match the required confirmation phrase",
                f"must be exactly {LIVE_CONFIRM_PHRASE}"))
        if not str(values.get("upstox_live_access_token", "")).strip():
            findings.append(finding(
                BLOCKER, "UPSTOX_LIVE_ACCESS_TOKEN", "no live access token set"))
        for name in ("max_position_value", "max_daily_loss"):
            if not _positive(values, name):
                findings.append(finding(
                    BLOCKER, name.upper(),
                    f"{values.get(name)} — 0 means unlimited and is refused in live",
                    "every hard limit must be explicitly non-zero before real money"))
        for name in ("max_open_positions", "max_trades_per_day"):
            try:
                ok = int(values.get(name) or 0) > 0
            except (TypeError, ValueError):
                ok = False
            if not ok:
                findings.append(finding(
                    BLOCKER, name.upper(),
                    f"{values.get(name)} — 0 means unlimited and is refused in live"))
        host = host_of(str(values.get("upstox_live_base_url", "")))
        if host in FORBIDDEN_IN_LIVE:
            findings.append(finding(
                BLOCKER, "UPSTOX_LIVE_BASE_URL",
                f"host {host!r} is a sandbox/auth host and cannot route real orders",
                "expected one of: " + ", ".join(sorted(LIVE_ORDER_HOSTS))))
        elif host not in LIVE_ORDER_HOSTS:
            findings.append(finding(
                BLOCKER, "UPSTOX_LIVE_BASE_URL",
                f"host {host!r} is not a documented live order host",
                "expected one of: " + ", ".join(sorted(LIVE_ORDER_HOSTS))))
        _market_data_findings(values, findings, require_upstox=True)
        if str(values.get("market_calendar", "")) != "nse-session":
            findings.append(finding(
                WARNING, "MARKET_CALENDAR",
                f"{values.get('market_calendar')!r} — Indian equities trade an NSE session",
                "MARKET_CALENDAR=nse-session"))
        findings.extend(_engine_gate_findings(values))
        findings.extend(_live_notes())
        _ops_findings(values, findings)
        _access_findings(values, findings)
        _inspect_secret(findings, "UPSTOX_LIVE_ACCESS_TOKEN",
                        values.get("upstox_live_access_token"))
        _inspect_secret(findings, "MARKET_DATA_ACCESS_TOKEN",
                        values.get("market_data_access_token"))
    elif target == "sandbox":
        if str(values.get("mode", "")).upper() != "PAPER" or values.get("live_trading") is True:
            findings.append(finding(
                BLOCKER, "MODE / LIVE_TRADING",
                f"MODE={values.get('mode')!r}, LIVE_TRADING={values.get('live_trading')!r}",
                "the sandbox broker runs in paper mode: MODE=PAPER and "
                "LIVE_TRADING=false — the app refuses anything else"))
        if not str(values.get("upstox_sandbox_access_token", "")).strip():
            findings.append(finding(
                BLOCKER, "UPSTOX_SANDBOX_ACCESS_TOKEN", "no sandbox token set"))
        host = host_of(str(values.get("upstox_sandbox_base_url", "")))
        if host in FORBIDDEN_IN_SANDBOX:
            findings.append(finding(
                BLOCKER, "UPSTOX_SANDBOX_BASE_URL",
                f"host {host!r} is not a sandbox host",
                "a live order host here would move real money while every "
                "label says sandbox"))
        elif host not in SANDBOX_ORDER_HOSTS:
            findings.append(finding(
                WARNING, "UPSTOX_SANDBOX_BASE_URL",
                f"host {host!r} is not a documented sandbox host",
                "expected one of: " + ", ".join(sorted(SANDBOX_ORDER_HOSTS))))
        _market_data_findings(values, findings, require_upstox=False)
        findings.extend(_engine_gate_findings(values))
        _access_findings(values, findings)
        _inspect_secret(findings, "UPSTOX_SANDBOX_ACCESS_TOKEN",
                        values.get("upstox_sandbox_access_token"))
        _inspect_secret(findings, "MARKET_DATA_ACCESS_TOKEN",
                        values.get("market_data_access_token"))
        findings.append(finding(
            NOTE, "NEXT", "next command: python -m app sandbox-test",
            "add --place-order --quantity 1 once the read-only probe passes"))


# --------------------------------------------------------------------------- #
# the check
# --------------------------------------------------------------------------- #
def check(env_file: Path = DEFAULT_ENV_FILE, target: str = "auto") -> dict:
    """Return the full report (also the JSON payload)."""
    file_values, effective = effective_values(env_file)
    values, coerce_errors = coerce_settings_values(effective)

    broker = str(values.get("broker", "")).upper()
    resolved = target
    if resolved == "auto":
        resolved = {"UPSTOX_LIVE": "live", "UPSTOX_SANDBOX": "sandbox"}.get(
            broker, "paper")

    findings: list[dict] = []
    if not env_file.exists():
        findings.append(finding(
            NOTE, "env file", f"{env_file} does not exist",
            "configuration is being read from exported environment variables; "
            "copy .env.example to .env to keep it in one place"))

    for key, message in coerce_errors:
        findings.append(finding(
            BLOCKER, key, message,
            "the application cannot parse this value and will refuse to start"))

    _check_unknown_keys(findings, file_values)
    unparsed = {key for key, _ in coerce_errors}
    if resolved in ("live", "sandbox"):
        target_findings(resolved, values, findings, unparsed)

    # The application is the authority: ask it directly.
    app_verdict, app_error = "accepted", ""
    if coerce_errors:
        app_verdict = "refused"
        app_error = "an unparseable value (listed above)"
    else:
        try:
            Settings(**values)
        except ValueError as exc:
            app_verdict, app_error = "refused", str(exc)
            # If the app refuses for a reason the checks above did not name,
            # say so rather than reporting a clean bill of health.
            if not any(f["level"] == BLOCKER for f in findings):
                findings.append(finding(
                    BLOCKER, "Settings", app_error,
                    "the application refuses this configuration"))

    blockers = [f for f in findings if f["level"] == BLOCKER]
    warnings = [f for f in findings if f["level"] == WARNING]
    notes = [f for f in findings if f["level"] == NOTE]
    not_configured = resolved == "paper" or broker not in {
        "UPSTOX_LIVE", "UPSTOX_SANDBOX"}

    return {
        "tool": "live_config_check",
        "env_file": str(env_file),
        "env_file_exists": env_file.exists(),
        "target": resolved,
        "broker": broker or "(unset)",
        "app_verdict": app_verdict,
        "app_error": app_error,
        "ok": not blockers and not not_configured,
        "not_configured": not_configured,
        "counts": {"blockers": len(blockers), "warnings": len(warnings),
                   "notes": len(notes)},
        "blockers": blockers,
        "warnings": warnings,
        "notes": notes,
        "next_command": ("python -m app live-preflight" if resolved == "live"
                         else "python -m app sandbox-test" if resolved == "sandbox"
                         else "python -m app paper-test"),
    }


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
_TARGET_LABEL = {
    "live": "LIVE (real money)",
    "sandbox": "SANDBOX (no real money)",
    "paper": "PAPER (no live requirements apply)",
}


def render(report: dict) -> str:
    lines: list[str] = []
    lines.append("Arena live configuration check")
    lines.append(f"  env file : {report['env_file']}"
                 + ("" if report["env_file_exists"] else "  (absent)"))
    lines.append(f"  target   : {_TARGET_LABEL.get(report['target'], report['target'])}")
    lines.append(f"  broker   : {report['broker']}")
    lines.append("")

    blocks = (
        (BLOCKER, report["blockers"],
         "BLOCKERS — the application will refuse to start"),
        (WARNING, report["warnings"],
         "WARNINGS — it starts, but check these"),
        (NOTE, report["notes"], "NOTES — cannot be verified from configuration"),
    )
    for level, items, title in blocks:
        if not items:
            continue
        lines.append(f"{title} ({len(items)})")
        for item in items:
            lines.append(f"  {item['key']:<28} {item['message']}")
            if item.get("hint"):
                lines.append(f"  {'':<28} → {item['hint']}")
        lines.append("")

    if report["not_configured"]:
        lines.append("Verdict: nothing to check — this is not a live or sandbox "
                     "configuration.")
        lines.append("To go live, see ARENA_GUIDE.md section 12 and "
                     "LIVE_TRADING_ENABLEMENT.md.")
    elif report["blockers"]:
        lines.append(f"Verdict: NOT READY — {len(report['blockers'])} blocker(s) "
                     "above.")
    else:
        lines.append("Verdict: configuration is acceptable."
                     + (f" ({len(report['warnings'])} warning(s) to consider.)"
                        if report["warnings"] else ""))
    lines.append(f"App verdict: {report['app_verdict']}"
                 + (f" — {report['app_error']}" if report["app_error"] else ""))
    lines.append(f"Next: {report['next_command']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the live/sandbox configuration before live-preflight")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE),
                        help="env file to check (default: the .env the app loads)")
    parser.add_argument("--target", choices=["auto", "live", "sandbox"],
                        default="auto", help="auto = infer from BROKER")
    parser.add_argument("--json", action="store_true",
                        help="print the report as JSON")
    args = parser.parse_args(argv)

    report = check(Path(args.env_file), args.target)
    print(json.dumps(report, indent=2) if args.json else render(report))
    if report["not_configured"]:
        return 2
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
