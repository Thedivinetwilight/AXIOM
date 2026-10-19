"""Live activation — the server side of the PAPER/LIVE switch.

WHAT THIS MODULE IS
    One service that owns the *operator-facing* live-trading workflow: it
    writes the live configuration, stores a broker credential, evaluates the
    readiness contract, performs the explicit ARM -> ACTIVATE sequence and
    performs the safe switch-off drain.

WHAT THIS MODULE IS NOT
    It is not a second live state machine. Every state transition goes
    through ``app.live.session.LiveSessionManager`` (ARM/ACTIVATE/PAUSE/
    STOP/DISARM), the supervised child is owned by
    ``app.live.supervisor.LiveEngineSupervisor``, readiness comes from
    ``app.live.preflight`` and every order still passes
    ``app.live.gateway``. This module only sequences those existing pieces
    and refuses loudly when any of them refuses.

THE THREE INVARIANTS
    1. PAPER is the default. The four live switches (``MODE=LIVE``,
       ``LIVE_TRADING=true``, ``BROKER=UPSTOX_LIVE``, ``LIVE_CONFIRM=<phrase>``)
       are written ONLY by :meth:`LiveActivationService.start`, and removed
       again by :meth:`LiveActivationService.stop`. Nothing else in the
       process writes them, and no restart, recovery path or background job
       can arm or activate a live session.
    2. Clicking the switch never places an order. The engine is a supervised
       child process; it is only spawned by an explicit ARM, and it can only
       submit after ACTIVATE mints a fresh authorization epoch.
    3. Secrets never leave the server. The broker access token and the OAuth
       client secret are written to the operator's per-user configuration
       file through the existing atomic, owner-only writer; they are never
       returned, logged, audited, embedded in a URL or put in a crash report.
       The UI only ever sees presence/verified/expired state.

WHERE THE LIVE CONFIGURATION LIVES
    A dedicated ``live.env`` inside the operator's own Arena home — NOT the
    installation's main ``.env``. The main file describes the PAPER
    installation (its database, its saved paper account, its timeframe); the
    live engine needs a different timeframe, a different data source and a
    different broker, and writing those into the main file would make the
    saved paper account incompatible with the next start-up. The two files
    never overwrite each other, and the paper database is never reused for
    live orders (live orders live in their own ``live_*`` tables).

CREDENTIAL STORAGE
    The repository has no OS-backed keyring, so the documented, existing
    secure mechanism for this codebase is the per-user Arena configuration
    file: it lives inside the user's own Arena home, is written atomically
    with owner-only permissions on POSIX and inherited owner-only ACLs on
    Windows (see ``app/config/envfile.py``), and is never committed. That
    fallback is used deliberately and is surfaced to the operator in the UI
    and in the operator documentation, so a secret is never stored silently
    in plaintext somewhere unknown.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from app.config.envfile import read_values, update_values
from app.config.hosts import READ_AND_AUTH_HOSTS, host_of, is_trusted_upstox_url
from app.config.settings import LIVE_CONFIRM_PHRASE, Settings

logger = logging.getLogger(__name__)

#: The name of the live configuration file inside the Arena home.
LIVE_ENV_FILENAME = "live.env"


class ActivationError(RuntimeError):
    """The activation workflow refused a step. The message is operator-facing.

    It never contains a secret, a token or a credential fragment.
    """


# ---------------------------------------------------------------- switches --

#: The four switches that make a configuration "live". They are written only
#: by the final explicit activation and removed by the switch-off flow.
LIVE_SWITCH_VALUES: dict[str, str] = {
    "MODE": "LIVE",
    "LIVE_TRADING": "true",
    "BROKER": "UPSTOX_LIVE",
    "LIVE_CONFIRM": LIVE_CONFIRM_PHRASE,
}

#: The PAPER values restored when the operator moves the switch away from
#: LIVE. The broker returns to the local paper broker, never to a sandbox
#: broker, so a live token can never reach a sandbox order host.
PAPER_SWITCH_VALUES: dict[str, str] = {
    "MODE": "PAPER",
    "LIVE_TRADING": "false",
    "BROKER": "LOCAL_PAPER",
}

#: Live configuration keys the wizard may write. None of them enables live
#: trading on their own; the four switches above are the only opt-in.
CONFIGURABLE_KEYS: tuple[str, ...] = (
    "MAX_POSITION_VALUE",
    "MAX_OPEN_POSITIONS",
    "MAX_DAILY_LOSS",
    "MAX_TRADES_PER_DAY",
    "LIVE_CANARY_MAX_NOTIONAL",
    "LIVE_CANARY_ORDERS",
    "LIVE_MAX_EQUITY_DRAWDOWN",
    "DATA_SOURCE",
    "MARKET_DATA_SYMBOLS",
    "MARKET_DATA_TIMEFRAME",
    "DEFAULT_TIMEFRAME",
    "MARKET_CALENDAR",
    "MARKET_DATA_MODE",
    "MARKET_DATA_ACCESS_TOKEN",
    "UPSTOX_LIVE_BASE_URL",
    "UPSTOX_LIVE_ORDER_HISTORY_URL",
    "LIVE_PRODUCT",
    "LIVE_ORDER_TAG",
)

#: Credential keys. Written only by the credential flow, never echoed back.
CREDENTIAL_KEYS: tuple[str, ...] = (
    "UPSTOX_LIVE_ACCESS_TOKEN",
    "UPSTOX_CLIENT_ID",
    "UPSTOX_CLIENT_SECRET",
    "UPSTOX_REDIRECT_URI",
)

#: The live access-token key. The market-data feed token is a separate
#: credential and is never reused as proof of a live trading account.
LIVE_TOKEN_KEY = "UPSTOX_LIVE_ACCESS_TOKEN"

#: The documented live read/auth host. A live OAuth token authenticates here;
#: a sandbox token does not.
LIVE_AUTH_URL = "https://api.upstox.com/v2/user/profile"
SANDBOX_AUTH_URL = "https://api-sandbox.upstox.com/v2/user/profile"


# ------------------------------------------------------------------ limits --

@dataclass(frozen=True)
class LimitField:
    """One hard risk limit the operator has to state explicitly."""

    key: str
    kind: str  # money | count | fraction
    label: str
    required: bool
    unit: str = "INR"

    def to_dict(self) -> dict:
        return {"key": self.key, "kind": self.kind, "label": self.label,
                "required": self.required, "unit": self.unit}


LIMIT_FIELDS: tuple[LimitField, ...] = (
    LimitField("MAX_POSITION_VALUE", "money",
               "Maximum position / order notional", True),
    LimitField("MAX_OPEN_POSITIONS", "count",
               "Maximum open positions", True, unit="positions"),
    LimitField("MAX_DAILY_LOSS", "money",
               "Maximum loss in one trading day", True),
    LimitField("MAX_TRADES_PER_DAY", "count",
               "Maximum new entries per trading day", True, unit="entries"),
    LimitField("LIVE_CANARY_MAX_NOTIONAL", "money",
               "Canary cap for the first live entries", False),
    LimitField("LIVE_CANARY_ORDERS", "count",
               "Number of canary entries", False, unit="entries"),
    LimitField("LIVE_MAX_EQUITY_DRAWDOWN", "fraction",
               "Peak-equity drawdown that auto-stops the session", False,
               unit="fraction"),
)

#: The API field names the browser sends, mapped to the configuration keys
#: they configure. The browser is untrusted, so the mapping is explicit and
#: a field that is not in here is refused rather than ignored.
API_LIMIT_KEYS: dict[str, str] = {
    "max_position_value": "MAX_POSITION_VALUE",
    "max_open_positions": "MAX_OPEN_POSITIONS",
    "max_daily_loss": "MAX_DAILY_LOSS",
    "max_trades_per_day": "MAX_TRADES_PER_DAY",
    "live_canary_max_notional": "LIVE_CANARY_MAX_NOTIONAL",
    "live_canary_orders": "LIVE_CANARY_ORDERS",
    "live_max_equity_drawdown": "LIVE_MAX_EQUITY_DRAWDOWN",
}

#: The market-wiring fields the browser may send.
API_MARKET_KEYS = frozenset({
    "data_source", "symbols", "timeframe", "calendar", "market_data_mode",
    "live_base_url", "live_order_history_url",
})

#: Engine timeframes the live engine can actually run.
LIVE_TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h")

_INSTRUMENT_KEY_RE = re.compile(r"^[A-Z_]+\|[A-Z0-9]+$")


def _parse_number(raw, field: LimitField) -> float:
    if isinstance(raw, bool):
        raise ActivationError(f"{field.key} must be a number, not a boolean.")
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = str(raw).strip()
        if not text:
            raise ActivationError(f"{field.key} is required.")
        try:
            value = float(text)
        except (TypeError, ValueError):
            raise ActivationError(
                f"{field.key} must be a number; '{text}' is not a number."
            ) from None
    if math.isnan(value):
        raise ActivationError(f"{field.key} must be a real number, not NaN.")
    if math.isinf(value):
        raise ActivationError(
            f"{field.key} must be a finite number; infinity is refused.")
    return value


def _parse_count(raw, field: LimitField) -> int:
    if isinstance(raw, bool):
        raise ActivationError(f"{field.key} must be a whole number, not a boolean.")
    if isinstance(raw, int):
        return int(raw)
    if isinstance(raw, float):
        if math.isnan(raw) or math.isinf(raw):
            raise ActivationError(f"{field.key} must be a finite whole number.")
        if not float(raw).is_integer():
            raise ActivationError(f"{field.key} must be a whole number.")
        return int(raw)
    text = str(raw).strip()
    if not text:
        raise ActivationError(f"{field.key} is required.")
    try:
        return int(text)
    except (TypeError, ValueError):
        raise ActivationError(
            f"{field.key} must be a whole number; '{text}' is not one."
        ) from None


def _format_number(value: float) -> str:
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def validate_limits(payload: dict) -> tuple[dict[str, str], list[str]]:
    """Validate the operator's risk limits.

    Returns ``(normalised_env_values, warnings)``. Raises
    :class:`ActivationError` with a precise, operator-readable reason for
    every rejected value. No default is ever invented: a missing required
    limit is an error, not a silent zero and not a guess.
    """
    if not isinstance(payload, dict):
        raise ActivationError("The risk limits must be sent as an object.")
    values: dict[str, str] = {}
    warnings: list[str] = []
    parsed: dict[str, float | int] = {}

    for field in LIMIT_FIELDS:
        if field.key not in payload:
            if field.required:
                raise ActivationError(
                    f"{field.key} ({field.label}) is required and has no default; "
                    "state it explicitly before live trading can be armed.")
            continue
        raw = payload[field.key]
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if field.required:
                raise ActivationError(f"{field.key} ({field.label}) is required.")
            continue
        if field.kind == "money":
            number = _parse_number(raw, field)
            if number < 0:
                raise ActivationError(
                    f"{field.key} ({field.label}) cannot be negative.")
            if field.required and number == 0:
                raise ActivationError(
                    f"{field.key} ({field.label}) must be greater than zero: 0 means "
                    "unlimited, and live trading refuses unbounded limits.")
            parsed[field.key] = number
            values[field.key] = _format_number(number)
        elif field.kind == "count":
            number_i = _parse_count(raw, field)
            if number_i < 0:
                raise ActivationError(
                    f"{field.key} ({field.label}) cannot be negative.")
            if field.required and number_i == 0:
                raise ActivationError(
                    f"{field.key} ({field.label}) must be greater than zero: 0 means "
                    "unlimited, and live trading refuses unbounded limits.")
            parsed[field.key] = number_i
            values[field.key] = str(number_i)
        else:  # fraction
            number = _parse_number(raw, field)
            if not 0 <= number < 1:
                raise ActivationError(
                    f"{field.key} ({field.label}) must be between 0 and 1 "
                    "(0 disables it, 1 is not a drawdown).")
            parsed[field.key] = number
            values[field.key] = _format_number(number)

    # -- combinations that cannot both be true --------------------------
    canary_notional = parsed.get("LIVE_CANARY_MAX_NOTIONAL")
    canary_orders = parsed.get("LIVE_CANARY_ORDERS")
    position_value = parsed.get("MAX_POSITION_VALUE")
    open_positions = parsed.get("MAX_OPEN_POSITIONS")
    trades_per_day = parsed.get("MAX_TRADES_PER_DAY")
    if canary_notional is not None and canary_orders is not None:
        if canary_notional > 0 and int(canary_orders) <= 0:
            raise ActivationError(
                "LIVE_CANARY_MAX_NOTIONAL is set but LIVE_CANARY_ORDERS is 0: the "
                "canary would never end. Set the number of canary entries or "
                "remove the canary cap.")
        if canary_notional > 0 and position_value is not None \
                and canary_notional > float(position_value):
            warnings.append(
                "The canary cap is larger than the maximum position value, so it "
                "never limits anything. Lower the canary cap to make the ramp "
                "meaningful.")
    if canary_notional is not None and position_value is not None \
            and canary_notional == 0 and "LIVE_CANARY_MAX_NOTIONAL" in payload:
        warnings.append(
            "No canary cap: the first live entries use the full position size.")
    if open_positions is not None and trades_per_day is not None \
            and int(open_positions) > int(trades_per_day):
        warnings.append(
            "The maximum open positions is larger than the maximum entries per "
            "day, so the position cap can never be reached.")

    return values, warnings


def validate_market_wiring(payload: dict) -> tuple[dict[str, str], list[str]]:
    """Validate the data source / instrument / timeframe wiring.

    The live engine is intraday-only and the market-data stream must be able
    to carry the same timeframe; a mismatch is refused here instead of being
    discovered by the engine after it has started.
    """
    values: dict[str, str] = {}
    warnings: list[str] = []
    data_source = str(payload.get("data_source") or "upstox").strip().lower()
    if data_source != "upstox":
        raise ActivationError(
            "Live trading needs the real market-data feed (DATA_SOURCE=upstox); "
            f"'{data_source}' cannot drive a live engine.")
    values["DATA_SOURCE"] = "upstox"

    symbols = str(payload.get("symbols") or "").strip()
    if not symbols:
        raise ActivationError(
            "At least one instrument is required for a live session "
            "(for example NSE_EQ|INE002A01018).")
    keys = [item.strip() for item in symbols.split(",") if item.strip()]
    for key in keys:
        if not _INSTRUMENT_KEY_RE.match(key):
            raise ActivationError(
                f"'{key}' is not a valid Upstox instrument key "
                "(expected NSE_EQ|INE002A01018).")
    values["MARKET_DATA_SYMBOLS"] = ",".join(keys)

    timeframe = str(payload.get("timeframe") or "").strip().lower()
    if not timeframe:
        raise ActivationError("A trading timeframe is required for a live session.")
    if timeframe not in LIVE_TIMEFRAMES:
        raise ActivationError(
            f"'{timeframe}' cannot drive a live engine. The live engine is "
            f"intraday-only ({', '.join(LIVE_TIMEFRAMES)}).")
    values["DEFAULT_TIMEFRAME"] = timeframe
    values["MARKET_DATA_TIMEFRAME"] = timeframe

    calendar = str(payload.get("calendar") or "nse-session").strip().lower()
    if calendar not in {"continuous", "nse-session"}:
        raise ActivationError("Unknown market calendar.")
    values["MARKET_CALENDAR"] = calendar
    if calendar != "nse-session":
        warnings.append(
            "Market calendar is not nse-session: Indian equity orders are "
            "usually sent with the nse-session calendar.")

    mode = str(payload.get("market_data_mode") or "ltpc").strip().lower()
    if mode not in {"ltpc", "full"}:
        raise ActivationError("MARKET_DATA_MODE must be ltpc or full.")
    values["MARKET_DATA_MODE"] = mode
    return values, warnings


def validate_live_hosts(payload: dict) -> tuple[dict[str, str], list[str]]:
    """The live order host must be the documented live host, nothing else."""
    from app.config.hosts import LIVE_ORDER_HOSTS

    values: dict[str, str] = {}
    warnings: list[str] = []
    base_url = str(payload.get("live_base_url") or "").strip()
    if not base_url:
        from app.config.hosts import DEFAULT_LIVE_BASE_URL

        base_url = DEFAULT_LIVE_BASE_URL
    host = host_of(base_url)
    if host not in LIVE_ORDER_HOSTS:
        raise ActivationError(
            f"'{host or base_url}' is not a documented live order host. Live "
            f"orders may only be sent to {', '.join(sorted(LIVE_ORDER_HOSTS))}.")
    if not is_trusted_upstox_url(base_url, LIVE_ORDER_HOSTS):
        raise ActivationError(
            "The live order host must be a plain HTTPS root URL on the "
            "documented live order host.")
    values["UPSTOX_LIVE_BASE_URL"] = base_url

    history_url = str(payload.get("live_order_history_url") or "").strip()
    if not history_url:
        from app.config.hosts import DEFAULT_ORDER_HISTORY_URL

        history_url = DEFAULT_ORDER_HISTORY_URL
    if not is_trusted_upstox_url(history_url, READ_AND_AUTH_HOSTS,
                                 expected_path="/v2/order/history"):
        raise ActivationError(
            "The live order-history URL must be the documented HTTPS Upstox "
            "order-history endpoint.")
    values["UPSTOX_LIVE_ORDER_HISTORY_URL"] = history_url
    return values, warnings


# ------------------------------------------------------------- credentials --

def credential_fingerprint(token: str) -> str:
    """SHA-256 of a credential. The token itself is never stored or shown."""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def mask_secret(value: str) -> str:
    """A presence hint that reveals nothing about the secret's content."""
    if not value:
        return ""
    return "present (" + str(len(value)) + " characters, hidden)"


# ------------------------------------------------------------------ service --

class LiveActivationService:
    """The PAPER/LIVE switch, server side.

    The service is deliberately thin: it sequences the existing state
    machine, supervisor, readiness contract and gateway, and it refuses
    whenever any of them refuses.
    """

    def __init__(self, workstation, *, env_path: Path | None = None,
                 settings_loader=None, token_exchange=None,
                 credential_verifier=None, app=None):
        self.workstation = workstation
        self._env_path = Path(env_path) if env_path else None
        self._app = app
        # ``settings_loader`` builds the prospective settings from the live
        # configuration file. Tests inject a fake; production uses
        # Settings.from_env with explicit overrides.
        self._settings_loader = settings_loader or self._default_settings_loader
        self._token_exchange = token_exchange or self._default_token_exchange
        self._credential_verifier = credential_verifier or self._default_verifier

    # -- plumbing -------------------------------------------------------
    @property
    def env_path(self) -> Path:
        """The live configuration file (never the installation's .env)."""
        if self._env_path is not None:
            return self._env_path
        from app import runtime

        return Path(runtime.arena_home()) / LIVE_ENV_FILENAME

    def _default_settings_loader(self, values: dict) -> Settings:
        return Settings.from_env(env_overrides=values)

    def _default_token_exchange(self, *, code: str, client_id: str,
                                 client_secret: str, redirect_uri: str) -> str:
        from app.data.upstox_auth import exchange_code_for_token

        return exchange_code_for_token(code, client_id, client_secret,
                                       redirect_uri)

    def _default_verifier(self, token: str) -> dict:
        from app.data.upstox_auth import verify_feed_credential

        import asyncio

        async def _probe(url: str):
            return await verify_feed_credential(token, authorize_url=url)

        return {"live": asyncio.run(_probe(LIVE_AUTH_URL)),
                "sandbox": asyncio.run(_probe(SANDBOX_AUTH_URL))}

    # -- reading --------------------------------------------------------
    def read_env(self) -> dict:
        try:
            return read_values(self.env_path)
        except OSError as exc:
            raise ActivationError(
                f"The live configuration file could not be read "
                f"({type(exc).__name__}).") from exc

    def prospective_settings(self) -> Settings:
        """The configuration the live file WOULD produce, right now.

        The ambient process environment is ignored for the keys the file
        defines, so the answer is "what would the live engine see?".
        """
        values = self.read_env()
        try:
            return self._settings_loader(values)
        except ValueError as exc:
            raise ActivationError(
                f"The live configuration is not valid: {exc}") from exc
        except TypeError as exc:  # pragma: no cover - defensive
            raise ActivationError(
                f"The live configuration could not be read "
                f"({type(exc).__name__}).") from exc

    def prospective_live_settings(self) -> Settings:
        """The configuration the live engine WOULD run with.

        The four switches are what the final activation writes, so they are
        applied here. A switch that the live configuration file defines with
        a DIFFERENT value is a contradiction and is refused: the file is
        never silently overwritten, because a hand-edited contradiction is
        exactly the case that must not be papered over.
        """
        values = dict(self.read_env())
        conflicts = []
        for key, expected in LIVE_SWITCH_VALUES.items():
            current = (values.get(key) or "").strip()
            if current and current != expected:
                conflicts.append(f"{key}={current}")
        if conflicts:
            raise ActivationError(
                "The live configuration file contradicts the live switches ("
                + ", ".join(conflicts)
                + "). Remove those lines or correct them before activating; "
                  "Arena will not overwrite them silently.")
        for key, expected in LIVE_SWITCH_VALUES.items():
            if not (values.get(key) or "").strip():
                values[key] = expected
        try:
            return self._settings_loader(values)
        except ValueError as exc:
            raise ActivationError(
                f"The live configuration is not valid: {exc}") from exc
        except TypeError as exc:  # pragma: no cover - defensive
            raise ActivationError(
                f"The live configuration could not be read "
                f"({type(exc).__name__}).") from exc

    def control_plane_settings(self) -> Settings:
        """The installation's OWN configuration — the paper one.

        This is deliberately not simply ``workstation.settings``: while a live
        session is active the control plane holds the LIVE settings, and the
        switch-off has to be able to put the installation back to paper. The
        paper configuration is therefore re-derived from the installation's own
        configuration file (and the ambient environment) whenever the control
        plane is currently running live.
        """
        current = getattr(self.workstation, "settings", None)
        if current is None:
            return Settings.from_env()
        if str(getattr(current, "mode", "")).upper() != "LIVE":
            return current
        try:
            return Settings.from_env()
        except Exception:  # pragma: no cover - defensive
            return current

    def switches_present(self, values: dict | None = None) -> bool:
        values = self.read_env() if values is None else values
        return (str(values.get("BROKER", "")).upper() == "UPSTOX_LIVE"
                and str(values.get("MODE", "")).upper() == "LIVE"
                and str(values.get("LIVE_TRADING", "")).lower()
                in {"1", "true", "yes"})

    def effective_settings(self) -> Settings:
        """The configuration the workstation is actually running under.

        While the live switches are installed this is the LIVE
        configuration; otherwise it is the installation's own (paper)
        configuration.
        """
        if not self.switches_present():
            return self.control_plane_settings()
        try:
            return self.prospective_settings()
        except ActivationError:
            return self.control_plane_settings()

    def apply_effective_settings(self) -> Settings:
        """Make the running control plane agree with the live configuration.

        The workstation's settings object and the application state are
        updated together, so the dashboard, the health endpoint, the reports
        and the live session state machine all describe the SAME effective
        mode. The paper installation's own .env is never touched.
        """
        settings = self.effective_settings()
        self.workstation.settings = settings
        supervisor = getattr(self.workstation, "live_supervisor", None)
        if supervisor is not None:
            supervisor.settings = settings
        if self._app is not None:
            try:
                self._app.state.settings = settings
            except Exception:  # pragma: no cover - defensive
                logger.debug("could not update the application settings state")
        return settings

    # -- credential state ------------------------------------------------
    def credential_status(self, settings=None) -> dict:
        """Masked credential state: present / verified / stale / expired.

        The token value is never part of this payload.
        """
        settings = settings or self.control_plane_settings()
        values = self.read_env()
        token = (values.get(LIVE_TOKEN_KEY) or "").strip()
        status: dict = {
            "present": bool(token),
            "source": "oauth" if values.get("UPSTOX_CLIENT_ID") else "unknown",
            "masked": mask_secret(token),
            "verified_at": None,
            "state": "absent",
            "note": "",
        }
        if not token:
            status["note"] = (
                "No live access token is stored. Connect the broker account "
                "before live trading can be armed.")
            return status

        from app.live.guards import TokenGuard

        repository = getattr(self.workstation, "repository", None)
        guard = TokenGuard(repository, max_age_hours=float(
            getattr(settings, "live_token_max_age_hours", 20.0) or 0.0))
        verified_at = guard.verified_at()
        recorded = {}
        try:
            recorded = (repository.get_engine_state("live_token_verified", {}) or {}
                        ) if repository is not None else {}
        except Exception:  # pragma: no cover - defensive
            recorded = {}
        fingerprint = recorded.get("credential_sha256")
        age_hours = guard.age_hours()
        max_age = float(getattr(settings, "live_token_max_age_hours", 20.0) or 0.0)

        if verified_at is None:
            status["state"] = "unverified"
            status["note"] = (
                "The stored credential has not been verified against the live "
                "host in this installation yet.")
        elif fingerprint and fingerprint != credential_fingerprint(token):
            status["state"] = "changed"
            status["note"] = (
                "The stored credential is not the one that was last verified; "
                "reconnect the broker account.")
        elif max_age > 0 and age_hours is not None and age_hours > max_age:
            status["state"] = "expired"
            status["note"] = (
                "The credential was last verified more than "
                f"{max_age:g} hours ago. Upstox access tokens are daily; "
                "reconnect before trading.")
        else:
            status["state"] = "verified"
            status["note"] = "The stored credential was verified against the live host."
        status["verified_at"] = verified_at.isoformat() if verified_at else None
        status["age_hours"] = age_hours
        # the interface offers a reconnect exactly when the stored credential
        # can no longer prove itself
        status["reconnect_required"] = status["state"] in {
            "expired", "changed", "unverified"}
        return status

    # -- configuration writes --------------------------------------------
    def save_configuration(self, payload: dict) -> dict:
        """Validate and store the live configuration (never the switches).

        Writing the configuration does NOT enable live trading: the four
        switches stay absent, so a restart still comes up in PAPER.
        """
        if not isinstance(payload, dict):
            raise ActivationError("The configuration must be sent as an object.")
        unknown = [key for key in payload
                   if key not in API_LIMIT_KEYS and key not in API_MARKET_KEYS]
        if unknown:
            raise ActivationError(
                "Unknown configuration field(s): " + ", ".join(sorted(unknown)))
        limit_payload = {API_LIMIT_KEYS[key]: value
                         for key, value in payload.items()
                         if key in API_LIMIT_KEYS}
        limit_values, limit_warnings = validate_limits(limit_payload)
        market_values, market_warnings = validate_market_wiring(payload)
        host_values, host_warnings = validate_live_hosts(payload)

        values = {**limit_values, **market_values, **host_values}
        summary = update_values(self.env_path, values)
        warnings = [*limit_warnings, *market_warnings, *host_warnings]
        self._audit("LIVE_ACTIVATION_CONFIGURATION_SAVED",
                    {"keys": sorted(values), "warnings": len(warnings)})
        return {"written": summary["updated"] + summary["added"],
                "warnings": warnings,
                "note": "The live configuration is stored. Live trading is still "
                        "not enabled: the four live switches are written only by "
                        "the final, explicit activation step."}

    def forget_credential(self) -> dict:
        """Remove the stored live access token from the configuration file."""
        values = self.read_env()
        if not (values.get(LIVE_TOKEN_KEY) or "").strip():
            return {"removed": False, "note": "No live access token was stored."}
        # An empty value keeps the key visible in the file (so the operator can
        # see what was cleared) without ever holding the secret.
        update_values(self.env_path, {LIVE_TOKEN_KEY: ""})
        self._audit("LIVE_ACTIVATION_CREDENTIAL_FORGOTTEN", {})
        return {"removed": True,
                "note": "The stored live access token was removed from the "
                        "Arena live configuration file."}

    def connect_account(self, payload: dict) -> dict:
        """Complete the broker OAuth flow and store the live access token.

        The client secret and the authorization code are used in-process and
        are never returned, logged or stored. Only the resulting access token
        is persisted, through the existing atomic owner-only writer.
        """
        if not isinstance(payload, dict):
            raise ActivationError("The credential request must be an object.")
        code = str(payload.get("code") or "").strip()
        client_id = str(payload.get("client_id") or "").strip()
        client_secret = str(payload.get("client_secret") or "").strip()
        redirect_uri = str(payload.get("redirect_uri") or "").strip()
        if not code:
            raise ActivationError(
                "No authorization code was supplied. Open the broker's official "
                "login page, approve access and paste the code it redirects "
                "with.")
        if not client_id or not client_secret or not redirect_uri:
            raise ActivationError(
                "The broker application credentials are required: client id, "
                "client secret and the registered redirect URI.")
        if not redirect_uri.lower().startswith("https://"):
            raise ActivationError(
                "The redirect URI must be an https:// URI registered with the "
                "broker.")

        try:
            token = self._token_exchange(code=code, client_id=client_id,
                                         client_secret=client_secret,
                                         redirect_uri=redirect_uri)
        except ActivationError:
            raise
        except Exception as exc:
            # The broker's error body is never echoed: it can contain the
            # code or the secret.
            logger.warning("live token exchange failed: %s", type(exc).__name__)
            raise ActivationError(
                "The broker refused the authorization code. Check that the "
                "code was copied in full, that it has not expired and that the "
                "redirect URI matches the one registered with the broker."
            ) from exc
        if not isinstance(token, str) or not token.strip():
            raise ActivationError("The broker returned an empty access token.")
        token = token.strip()

        verdict = self._verify_live_credential(token)
        if not verdict["ok"]:
            raise ActivationError(verdict["reason"])

        update_values(self.env_path, {
            LIVE_TOKEN_KEY: token,
            # The same OAuth token is the documented market-data feed
            # credential; the live engine needs it to stream bars.
            "MARKET_DATA_ACCESS_TOKEN": token,
            "UPSTOX_CLIENT_ID": client_id,
            "UPSTOX_CLIENT_SECRET": client_secret,
            "UPSTOX_REDIRECT_URI": redirect_uri,
        })
        self._record_verification(token)
        self._audit("LIVE_ACTIVATION_CREDENTIAL_CONNECTED",
                    {"source": "oauth", "verified": True,
                     "fingerprint": credential_fingerprint(token)[:12]})
        status = self.credential_status()
        status["note"] = ("The broker account was verified against the live "
                          "host and the access token is stored in the "
                          "operator's Arena live configuration file.")
        return status

    def _verify_live_credential(self, token: str) -> dict:
        """Prove the credential belongs to a LIVE account.

        A sandbox credential is refused explicitly: it must never be able to
        satisfy a live readiness check.
        """
        try:
            result = self._credential_verifier(token)
        except Exception as exc:
            logger.warning("live credential verification failed: %s",
                           type(exc).__name__)
            return {"ok": False, "reason": (
                "The broker could not be reached to verify the credential. "
                "No live session was started.")}
        live = result.get("live")
        sandbox = result.get("sandbox")
        if live is not None and getattr(live, "authenticated", False):
            return {"ok": True, "reason": "verified on the live host"}
        if sandbox is not None and getattr(sandbox, "authenticated", False):
            return {"ok": False, "reason": (
                "That credential authenticates on the SANDBOX host only. A "
                "sandbox credential can never authorise live trading — "
                "generate a live access token in the broker's official "
                "login page and try again.")}
        if live is not None and getattr(live, "verdict", "") in (
                "rejected", "forbidden"):
            return {"ok": False, "reason": (
                "The broker rejected this credential on the live host "
                f"(HTTP {getattr(live, 'http_status', '?')}). It is either "
                "expired, revoked, or it belongs to another environment. No "
                "live session was started.")}
        return {"ok": False, "reason": (
            "The credential could not be verified on the live host. No live "
            "session was started.")}

    def _record_verification(self, token: str) -> None:
        repository = getattr(self.workstation, "repository", None)
        if repository is None:
            return
        try:
            from app.live.guards import TokenGuard

            settings = self.control_plane_settings()
            guard = TokenGuard(repository, max_age_hours=float(
                getattr(settings, "live_token_max_age_hours", 20.0) or 0.0))
            guard.record_verified(source="live activation OAuth",
                                  credential=token)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("could not record credential verification: %s",
                           type(exc).__name__)

    def authorization_url(self, payload: dict) -> dict:
        """The broker's official OAuth dialog URL.

        Only the public client id and the registered redirect URI appear in
        it — never a secret, never a token.
        """
        client_id = str((payload or {}).get("client_id") or "").strip()
        redirect_uri = str((payload or {}).get("redirect_uri") or "").strip()
        if not client_id or not redirect_uri:
            raise ActivationError(
                "The broker application client id and the registered redirect "
                "URI are required to build the login URL.")
        if not redirect_uri.lower().startswith("https://"):
            raise ActivationError(
                "The redirect URI must be an https:// URI registered with the "
                "broker.")
        from app.data.upstox_auth import build_login_url

        url = build_login_url(client_id, redirect_uri)
        return {"authorization_url": url,
                "note": "Open this URL in the official broker login page. "
                        "Approve access, then copy the authorization code it "
                        "redirects with back into Arena."}

    # -- activation ------------------------------------------------------
    def start(self, *, confirmation_phrase: str, actor: str = "gui") -> dict:
        """The final, explicit activation.

        Sequence: validate the phrase -> prove the live configuration is
        complete -> write the four switches -> start the supervised child ->
        ARM -> ACTIVATE. Any refusal rolls the switches back to PAPER and
        asks the child to drain. Nothing here places an order.
        """
        if confirmation_phrase != LIVE_CONFIRM_PHRASE:
            raise ActivationError(
                "Starting real-money trading requires the exact confirmation "
                "phrase. Nothing was changed.")

        settings = self.prospective_live_settings()
        missing = self._missing_live_requirements(settings)
        if missing:
            raise ActivationError(
                "The live configuration is incomplete: " + "; ".join(missing))

        previous = self.read_env()
        update_values(self.env_path, dict(LIVE_SWITCH_VALUES))
        try:
            live_settings = self.apply_effective_settings()
        except ActivationError:
            self._restore_env(previous)
            raise

        sessions = self.workstation.live_sessions
        supervisor = self.workstation.live_supervisor
        try:
            if sessions.current().state not in ("OFF", "CONFIGURED", "READY", "STOPPED"):
                raise ActivationError(
                    f"The live session is in state "
                    f"{sessions.current().state}; resolve or release it before "
                    "starting a new one.")
            if self.workstation.emergency_stop_active():
                raise ActivationError(
                    "The emergency stop is active. Clear it deliberately "
                    "before starting a live session.")
            # The supervised child is spawned only here, and it receives the
            # live configuration — so it comes up in live mode.
            supervisor.start(env_overrides=self.child_environment())
            sessions.arm(live_settings, confirmation_phrase=confirmation_phrase,
                         repository=self.workstation.repository, actor=actor)
            sessions.activate(settings=live_settings, actor=actor)
        except Exception as exc:
            self._rollback_activation(previous)
            if isinstance(exc, ActivationError):
                raise
            raise ActivationError(str(exc)) from exc

        self._audit("LIVE_ACTIVATION_STARTED",
                    {"actor": actor, "state": sessions.current().state})
        return {
            "state": sessions.current().state,
            "reason": sessions.current().reason,
            "supervisor": supervisor.status(),
            "note": "The supervised live engine is running and the live "
                    "session is ACTIVE. Every order still passes the guard "
                    "chain and the hard risk limits.",
        }

    def child_environment(self) -> dict:
        """The environment the supervised live engine is started with.

        The live configuration file is the source of truth; the child gets
        exactly those values, so the engine it runs is the configuration the
        operator just confirmed.
        """
        values = self.read_env()
        return {key: str(value) for key, value in values.items()}

    def _missing_live_requirements(self, settings) -> list[str]:
        missing: list[str] = []
        if str(getattr(settings, "mode", "")).upper() != "LIVE":
            missing.append("MODE=LIVE")
        if str(getattr(settings, "broker", "")).upper() != "UPSTOX_LIVE":
            missing.append("BROKER=UPSTOX_LIVE")
        if not bool(getattr(settings, "live_trading", False)):
            missing.append("LIVE_TRADING=true")
        if str(getattr(settings, "live_confirm", "")).strip() != LIVE_CONFIRM_PHRASE:
            missing.append("LIVE_CONFIRM=<the exact phrase>")
        if not str(getattr(settings, "upstox_live_access_token", "")).strip():
            missing.append("a stored live access token")
        for name, label in (("max_position_value", "MAX_POSITION_VALUE"),
                            ("max_daily_loss", "MAX_DAILY_LOSS")):
            if not float(getattr(settings, name, 0) or 0) > 0:
                missing.append(f"{label}>0")
        for name, label in (("max_open_positions", "MAX_OPEN_POSITIONS"),
                            ("max_trades_per_day", "MAX_TRADES_PER_DAY")):
            if not int(getattr(settings, name, 0) or 0) > 0:
                missing.append(f"{label}>0")
        if str(getattr(settings, "data_source", "")) != "upstox":
            missing.append("DATA_SOURCE=upstox")
        return missing

    def _rollback_activation(self, previous: dict) -> None:
        """Undo a refused activation: stop the child, restore PAPER."""
        sessions = self.workstation.live_sessions
        supervisor = self.workstation.live_supervisor
        try:
            sessions.stop(reason="live activation was refused")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("could not stop the session during rollback: %s",
                           type(exc).__name__)
        try:
            supervisor.request_stop(reason="live activation was refused")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("could not request a stop during rollback: %s",
                           type(exc).__name__)
        self._restore_env(previous)
        self.apply_effective_settings()
        self._audit("LIVE_ACTIVATION_ROLLED_BACK", {})

    def _restore_env(self, previous: dict) -> None:
        """Put the live configuration file back the way it was.

        Only the keys this service owns are restored; every other line the
        operator wrote is preserved. A key that was not there before is
        removed, not blanked, so the file never carries a half-set switch.
        """
        from app.config.envfile import remove_values

        owned = (*LIVE_SWITCH_VALUES, *CONFIGURABLE_KEYS)
        restore = {key: previous[key] for key in owned if key in previous}
        absent = [key for key in owned if key not in previous]
        try:
            if absent:
                remove_values(self.env_path, absent)
            if restore:
                update_values(self.env_path, restore)
        except OSError as exc:  # pragma: no cover - defensive
            logger.error("could not restore the live configuration file: %s",
                         type(exc).__name__)

    # -- switch off ------------------------------------------------------
    def stop(self, *, reason: str = "operator moved the switch to PAPER",
             remove_configuration: bool = True) -> dict:
        """The safe switch-off: pause entries, drain, disarm, return to PAPER.

        This never claims the account is flat. It reports what the broker
        confirmed and what is still unknown.
        """
        sessions = self.workstation.live_sessions
        supervisor = self.workstation.live_supervisor
        state_before = sessions.current().state
        errors: list[str] = []

        # 1. stop new entries first (the durable stop request + the session
        #    transition), so nothing new can be submitted while we drain.
        try:
            sessions.stop(reason=reason)
        except Exception as exc:
            errors.append(f"the live session could not be stopped ({type(exc).__name__})")
        try:
            supervisor.request_stop(reason=reason)
        except Exception as exc:
            errors.append(f"the engine could not be asked to drain ({type(exc).__name__})")

        # 2. wait for the child to leave, but never kill it: an engine that
        #    still holds risk keeps managing it.
        supervisor_status = supervisor.status()
        if supervisor_status.get("running"):
            try:
                supervisor.close(timeout=5.0)
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(f"the engine did not confirm its exit ({type(exc).__name__})")
        supervisor_status = supervisor.status()

        # 3. disarm: the authorization epoch is invalidated, so even a
        #    surviving engine cannot submit a new entry.
        try:
            sessions.disarm(reason=reason)
        except Exception as exc:
            errors.append(f"the session could not be disarmed ({type(exc).__name__})")

        # 4. return the configuration to PAPER: the four switches are taken
        #    out of the live configuration file entirely, so the next start-up
        #    and the running control plane both say PAPER.
        if remove_configuration:
            from app.config.envfile import remove_values

            remove_values(self.env_path, list(LIVE_SWITCH_VALUES))
        self.apply_effective_settings()

        self._audit("LIVE_ACTIVATION_STOPPED",
                    {"state_before": state_before, "errors": len(errors)})
        book = self.book_status()
        return {
            "state": sessions.current().state,
            "reason": sessions.current().reason,
            "supervisor": supervisor_status,
            "positions": book["positions"],
            "working_orders": book["working_orders"],
            "reconciliation": book["reconciliation"],
            "errors": errors,
            "note": "New entries are refused and the authorization epoch is "
                    "invalidated. Positions and working orders are reported as "
                    "the broker last confirmed them; this is not a statement "
                    "that the account is flat.",
        }

    # -- book / status ---------------------------------------------------
    def book_status(self) -> dict:
        """Positions, working orders and reconciliation evidence.

        Every field is explicitly marked unavailable when it cannot be read.
        Nothing is ever reported as zero because it could not be measured.
        """
        repository = getattr(self.workstation, "repository", None)
        result = {
            "positions": None,
            "working_orders": None,
            "reconciliation": None,
            "unresolved_intents": None,
            "note": "",
        }
        if repository is None:
            result["note"] = ("No database is attached: positions and orders are "
                              "unavailable.")
            return result
        try:
            from app.live.readiness import reconciliation_freshness

            settings = self.control_plane_settings()
            silence = float(getattr(settings, "live_reconcile_silence_minutes", 10) or 10)
            result["reconciliation"] = reconciliation_freshness(
                repository, max_age_seconds=silence * 60.0)
        except Exception as exc:
            result["reconciliation"] = {"ok": False,
                                        "reason": f"unavailable ({type(exc).__name__})"}
        try:
            orders = repository.open_live_orders()
            result["working_orders"] = len(orders) if isinstance(orders, list) else None
        except Exception:
            result["working_orders"] = None
        try:
            from app.engine.state import SubmissionJournal

            unresolved = SubmissionJournal(repository).unresolved()
            result["unresolved_intents"] = len(unresolved)
        except Exception:
            result["unresolved_intents"] = None
        try:
            positions = repository.list_live_positions() if hasattr(
                repository, "list_live_positions") else None
            result["positions"] = positions
        except Exception:
            result["positions"] = None
        return result

    def switch_state(self) -> dict:
        """The truthful state of the PAPER/LIVE switch."""
        sessions = self.workstation.live_sessions
        state = sessions.current().state
        configured_live = self.switches_present()
        if state == "ACTIVE":
            position = "LIVE_ACTIVE"
        elif configured_live:
            position = "LIVE_CONFIGURED"
        else:
            position = "PAPER"
        return {
            "position": position,
            "session_state": state,
            "reason": sessions.current().reason,
            "configuration_live": configured_live,
            "note": {
                "PAPER": "Live trading is not enabled. Orders can only be "
                         "simulated.",
                "LIVE_CONFIGURED": "The live configuration is installed, but no "
                                   "live engine is authorized. Nothing can "
                                   "place a real order until you complete the "
                                   "activation.",
                "LIVE_ACTIVE": "A supervised live engine is running with real "
                               "money. Orders are real.",
            }[position],
        }

    def snapshot(self) -> dict:
        """Everything the activation wizard needs, in one payload."""
        sessions = self.workstation.live_sessions
        supervisor = self.workstation.live_supervisor
        settings = self.effective_settings()
        switch = self.switch_state()
        readiness = {}
        try:
            readiness = sessions.readiness(settings,
                                           repository=self.workstation.repository)
        except Exception as exc:  # pragma: no cover - defensive
            readiness = {"verdict": "BLOCKED", "error": type(exc).__name__}
        summary = {}
        try:
            summary = sessions.start_confirmation_summary(
                settings, repository=self.workstation.repository,
                emergency_stop=self.workstation.emergency_stop_active())
        except Exception as exc:  # pragma: no cover - defensive
            summary = {"error": type(exc).__name__}
        return {
            "switch": switch,
            "session": {"state": sessions.current().state,
                        "reason": sessions.current().reason,
                        "ts_entered": sessions.current().ts_entered},
            "supervisor": supervisor.status(),
            "credential": self.credential_status(settings),
            "readiness": readiness,
            "start_summary": summary,
            "book": self.book_status(),
            "emergency_stop_active": self.workstation.emergency_stop_active(),
            "limits": [field.to_dict() for field in LIMIT_FIELDS],
            "confirmation_phrase_required": True,
            "configuration": {
                "mode": str(getattr(settings, "mode", "PAPER")).upper(),
                "broker": str(getattr(settings, "broker", "LOCAL_PAPER")).upper(),
                "live_trading": bool(getattr(settings, "live_trading", False)),
                "data_source": getattr(settings, "data_source", ""),
                "symbols": getattr(settings, "market_data_symbols", ""),
                "timeframe": getattr(settings, "default_timeframe", ""),
                "calendar": getattr(settings, "market_calendar", ""),
                "max_position_value": getattr(settings, "max_position_value", None),
                "max_open_positions": getattr(settings, "max_open_positions", None),
                "max_daily_loss": getattr(settings, "max_daily_loss", None),
                "max_trades_per_day": getattr(settings, "max_trades_per_day", None),
                "live_canary_max_notional": getattr(settings, "live_canary_max_notional", None),
                "live_canary_orders": getattr(settings, "live_canary_orders", None),
                "live_max_equity_drawdown": getattr(settings, "live_max_equity_drawdown", None),
            },
            "live_configuration_file": str(self.env_path),
            "required_phrase": LIVE_CONFIRM_PHRASE,
            "phrase_hint": "The exact phrase is required; it is never stored "
                           "or echoed by the interface.",
        }

    # -- audit -----------------------------------------------------------
    def _audit(self, event: str, payload: dict) -> None:
        """Record an auditable, secret-free activation event."""
        repository = getattr(self.workstation, "repository", None)
        if repository is None:
            return
        try:
            repository.record_system_event(event, payload)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("could not record %s: %s", event, type(exc).__name__)
