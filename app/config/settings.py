"""Environment configuration, including the gated real-money (LIVE) mode.

Real-money routing is OFF by default and requires FOUR independent switches to
agree (V2 live-hardening, see LIVE_TRADING_ENABLEMENT.md):

    MODE=LIVE  +  LIVE_TRADING=true  +  BROKER=UPSTOX_LIVE  +  LIVE_CONFIRM=<phrase>

and, additionally, explicit NON-ZERO hard risk limits (``0`` means "unlimited"
and is refused in live mode). Any single missing switch raises at startup, so a
stale ``.env``, a typo or a leftover shell export cannot move real money.

``MODE=PAPER`` / ``LIVE_TRADING=false`` remain the defaults and the only
permitted combination for every other broker. Broker hosts are classified in
``app/config/hosts.py``; the sandbox broker can never be pointed at a live
order host.
"""

import os
import types
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Union, get_args, get_origin, get_type_hints

from app.config.hosts import (
    DEFAULT_LIVE_BASE_URL,
    DEFAULT_ORDER_HISTORY_URL,
    DEFAULT_SANDBOX_BASE_URL,
    FORBIDDEN_IN_LIVE,
    FORBIDDEN_IN_SANDBOX,
    LIVE_ORDER_HOSTS,
    READ_AND_AUTH_HOSTS,
    SANDBOX_ORDER_HOSTS,
    host_of,
    is_trusted_upstox_url,
)

ROOT = Path(__file__).resolve().parents[2]

#: Exact phrase required in LIVE_CONFIRM before real money can be routed. It is
#: deliberately long and awkward: it must never be guessable from a template.
LIVE_CONFIRM_PHRASE = "I-ACCEPT-REAL-MONEY-RISK"


def _unwrap_optional(annotation):
    """(is_optional, base_type) for an annotation (handles `X | None`)."""
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return True, args[0]
        return True, annotation  # unusual multi-arg union: handled verbatim
    return False, annotation


def coerce_env_value(name: str, annotation, raw):
    """Convert one configuration value according to the DECLARED field type.

    This is the single source of truth for env/.env parsing (3.0.2 fix): the
    previous parser branched on the runtime type of the DEFAULT value, so
    int-typed fields (and float fields with int defaults, e.g.
    `initial_capital: float = 100_000`) leaked raw strings into validation
    and execution code (`TypeError: must be real number, not str`,
    `'<' not supported between instances of 'str' and 'int'`).

    Supports: str, bool, int, float, `X | None` (with the documented
    `none` → None special case for the optional-float protective exits), and
    `field(default=...)` declarations (conversion is annotation-driven, never
    default-driven). Malformed values raise a clean, field-named ValueError
    here — never a TypeError deep in unrelated modules.
    """
    if not isinstance(raw, str):
        # already a runtime value (e.g. a programmatic default): normalize
        # numeric types to the declared annotation where it matters
        _is_optional, base = _unwrap_optional(annotation)
        if base is float and isinstance(raw, int) and not isinstance(raw, bool):
            return float(raw)
        return raw
    is_optional, base = _unwrap_optional(annotation)
    if isinstance(base, type) and issubclass(base, bool):
        lowered = raw.strip().lower()
        if lowered not in {"true", "false"}:
            raise ValueError(f"{name.upper()} must be true or false")
        return lowered == "true"
    if base is int:
        try:
            return int(raw)
        except ValueError:
            raise ValueError(
                f"{name.upper()} must be an integer (got {raw!r})") from None
    if base is float:
        if is_optional and raw.strip().lower() == "none":
            return None  # documented: STOP_LOSS=none / TAKE_PROFIT=none
        try:
            return float(raw)
        except ValueError:
            raise ValueError(
                f"{name.upper()} must be a number (got {raw!r})") from None
    return raw  # str and any other annotation: verbatim


@dataclass(frozen=True)
class Settings:
    mode: str = "PAPER"
    live_trading: bool = False
    initial_capital: float = 100_000.0  # float per annotation (3.0.2 audit;
    # the env parser is annotation-driven regardless of the default literal)
    currency: str = "INR"
    market_calendar: str = "continuous"
    default_symbol: str = "DEMO"
    # AXIOM-MONEY-PRINTER (master spec sections 4-5, 15): the selected market
    # scopes universes/calendars/execution context; attention mode tunes
    # ANALYTICAL thresholds only — it can never relax safety controls.
    market: str = "india"
    attention_mode: str = "balanced"  # conservative | balanced | aggressive
    # The engine is intraday-only (1m/5m/15m/30m). A daily default is fine for
    # research/replay, but a live engine cannot consume it — see the live-mode
    # validation below, which refuses instead of silently downgrading.
    default_timeframe: str = "1D"
    commission: float = 0.0005
    slippage: float = 0.0005
    spread: float = 0.0002
    position_size: float = 0.25
    stop_loss: float | None = 0.05
    take_profit: float | None = 0.10
    max_participation: float = 0.10
    database_url: str = "sqlite:///trading_bot.db"
    api_token: str = field(default="", repr=False)
    market_data_token: str = field(default="", repr=False)
    data_ws_url: str = field(default="", repr=False)
    data_source: str = "replay"
    # V0.6.1 real market DATA (read-only Upstox feed). Execution config for
    # real money lives in the V2 LIVE block further down (four-switch gated).
    market_data_provider: str = "upstox"
    market_data_symbols: str = ""
    market_data_timeframe: str = ""
    market_data_mode: str = "ltpc"
    market_data_access_token: str = field(default="", repr=False)
    market_data_authorize_url: str = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
    # --- V1 (Arena): explicit broker modes + sandbox + hard risk limits ---
    broker: str = "LOCAL_PAPER"  # LOCAL_PAPER | UPSTOX_SANDBOX | UPSTOX_LIVE (gated)
    upstox_sandbox_access_token: str = field(default="", repr=False)
    # Sandbox order host. This MUST stay a sandbox host: api-hft.upstox.com is
    # the live order host and is refused by the sandbox broker.
    upstox_sandbox_base_url: str = DEFAULT_SANDBOX_BASE_URL
    upstox_sandbox_order_history_url: str = DEFAULT_ORDER_HISTORY_URL
    sandbox_product: str = "I"  # intraday product per documented V3 order API
    sandbox_order_tag: str = "arena-v1"
    # --- V2 (Arena LIVE): real-money routing, gated by four switches ---
    # Nothing here is active unless MODE=LIVE + LIVE_TRADING=true +
    # BROKER=UPSTOX_LIVE + LIVE_CONFIRM (phrase) all agree AND every hard risk
    # limit is explicitly non-zero.
    upstox_live_access_token: str = field(default="", repr=False)
    upstox_live_base_url: str = DEFAULT_LIVE_BASE_URL
    upstox_live_order_history_url: str = DEFAULT_ORDER_HISTORY_URL
    live_product: str = "I"  # documented V3 product values: I | D | MTF
    live_order_tag: str = "arena-live"
    live_algo_name: str = ""  # optional X-Algo-Name for exchange-approved algos
    live_confirm: str = field(default="", repr=False)  # must equal LIVE_CONFIRM_PHRASE
    # --- AXIOM-MONEY-PRINTER (master spec section 39): static-IP compliance.
    # Upstox enforces that live order traffic originates from user-registered
    # static IPs; these fields record the REGISTERED values so the readiness
    # center can surface configuration state. They are metadata, never a
    # bypass: the broker still rejects unregistered sources itself.
    upstox_static_ip_primary: str = ""
    upstox_static_ip_secondary: str = ""
    # Broker API rate-limit profile for live order traffic (master spec
    # section 40). Values: REGULAR_ALGO | REGISTERED_ALGO | STANDARD.
    live_rate_limit_profile: str = "REGULAR_ALGO"
    # --- V2.1 (Arena L-ops): live operations safety rails -------------------
    # ALL of these act on NEW ENTRIES only. Exits and protective stops are
    # never blocked by an operational guard (a guard must not trap the account
    # in a position); see app/live/.
    # Canary ramp: while the first ``live_canary_orders`` live entries have not
    # been placed, an entry is capped to ``live_canary_max_notional`` (0 = off).
    live_canary_max_notional: float = 0.0
    live_canary_orders: int = 10
    # Token freshness: Upstox access tokens expire daily. The engine refuses
    # NEW ENTRIES when the last verified credential is older than this many
    # hours, and warns this many minutes before that (0 disables the check).
    live_token_max_age_hours: float = 20.0
    live_token_warn_minutes: int = 120
    # Watchdog: an order left working for longer than this raises an alert
    # (WARNING), escalating to CRITICAL after live_stuck_order_escalate_minutes.
    live_stuck_order_minutes: int = 5
    live_stuck_order_escalate_minutes: int = 15
    # When True the engine cancels orders the watchdog has escalated to
    # CRITICAL. Off by default: cancelling is an action, and actions need an
    # explicit opt-in (matching the rest of this section).
    live_auto_cancel_stuck: bool = False
    # Reconciliation silence (no successful reconcile) longer than this raises
    # an alert.
    live_reconcile_silence_minutes: int = 10
    # An unacknowledged CRITICAL alert older than this is itself an incident
    # (0 disables). Acknowledge with `python -m app live-ack`.
    live_unacked_critical_minutes: int = 15
    # Single-engine lock: a LIVE engine refuses to start while another live
    # engine's heartbeat is this fresh (a crashed engine never blocks a restart).
    live_lock_stale_seconds: int = 180
    # Auto-stop: when a live breach is detected (daily loss / equity drawdown)
    # the engine triggers the persisted EMERGENCY STOP itself. Fail-closed
    # default is ON for live mode.
    live_auto_stop_on_breach: bool = True
    # Equity drawdown kill switch (fraction of peak equity, 0 = disabled).
    live_max_equity_drawdown: float = 0.0
    # --- Alerts (any mode; the durable sink is always the system_events table) --
    # Optional HTTP POST sink for CRITICAL/WARNING alerts. Empty = disabled.
    alert_webhook_url: str = field(default="", repr=False)
    alert_min_severity: str = "WARNING"  # INFO | WARNING | CRITICAL
    alert_dedup_seconds: int = 300  # identical alerts are collapsed in this window
    # A failed webhook POST is retried with exponential backoff this many times
    # (0 = do not retry). The durable sink already holds the alert either way.
    alert_retry_attempts: int = 3
    alert_retry_backoff_seconds: int = 60
    # Hard risk limits (outside any learning system; 0 = unlimited/disabled).
    max_position_value: float = 0.0
    max_open_positions: int = 0
    max_daily_loss: float = 0.0
    max_trades_per_day: int = 0
    log_level: str = "INFO"
    # --- V2.1 (Arena): market intelligence + data platform (no trading changes) ---
    data_dir: str = "data"
    universe_sync_url: str = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
    history_base_url: str = "https://api.upstox.com/v3/historical-candle"
    intraday_base_url: str = "https://api.upstox.com/v3/historical-candle/intraday"
    parquet_enabled: bool = True  # used only if pyarrow/fastparquet is importable
    streaming_universe: str = ""  # comma-separated instrument keys; NEVER the whole universe
    maintenance_hour: int = 17    # UTC hour for daily maintenance jobs
    max_download_retries: int = 3
    research_batch_size: int = 30  # rolling-batch learning size (bars/trades)

    # --- FINAL: portfolio/scanner/event knobs (hard cap is NOT learning-editable) ---
    max_strategy_allocation: float = 0.35  # hard cap per strategy, outside learning
    event_move_threshold: float = 0.003    # |return| triggering an event decision
    event_volume_spike: float = 2.0        # volume multiple triggering an event
    market_timezone: str = "Asia/Kolkata"  # trading-day boundary for daily risk
    scanner_top_k: int = 5
    discovery_max_candidates: int = 12
    # --- 3.0.5: performance / trade-quality gates (additive, fail-closed) ---
    net_edge_threshold: float = 0.001  # 10 bps minimum net edge after costs
    min_quality_score: float = 0.3  # minimum quality to trade
    max_spread_bps: float = 50.0  # reject if spread > 50 bps
    min_liquidity_score: float = 0.2
    volatility_lookback: int = 20
    max_volatility_factor: float = 3.0
    cross_market_cache_ttl: int = 60  # seconds
    entry_timing_lookback: int = 50
    alpha_decay_min_samples: int = 20
    mae_mfe_enabled: bool = True
    correlation_threshold: float = 0.7
    scanner_cache_ttl: int = 300
    hot_path_max_latency_ms: float = 100.0

    def __post_init__(self):
        # V1 broker modes: paper, documented Upstox SANDBOX orders, and the
        # V2 gated real-money mode. The live host/token pair is validated only
        # for BROKER=UPSTOX_LIVE; every other broker stays paper/sandbox.
        broker = self.broker.upper()
        if broker == "LIVE":
            # Legacy alias kept fail-closed: the name alone is never enough.
            raise ValueError(
                "LIVE trading is not enabled under the alias BROKER=LIVE; "
                "real-money routing requires BROKER=UPSTOX_LIVE together with "
                "MODE=LIVE, LIVE_TRADING=true, LIVE_CONFIRM and non-zero risk "
                "limits (see LIVE_TRADING_ENABLEMENT.md)"
            )
        if broker == "UPSTOX_LIVE":
            self._validate_live_mode()
        elif self.mode.upper() != "PAPER" or self.live_trading:
            raise ValueError("Only MODE=PAPER and LIVE_TRADING=false are permitted")
        if broker not in {"LOCAL_PAPER", "UPSTOX_SANDBOX", "UPSTOX_LIVE"}:
            raise ValueError(
                "BROKER must be LOCAL_PAPER, UPSTOX_SANDBOX or UPSTOX_LIVE")
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError("Only SQLite is supported")
        from app.backtesting.costs import ExecutionConfig

        ExecutionConfig(**self.execution_kwargs())
        if self.market_calendar not in {"continuous", "nse-session"}:
            raise ValueError("Unknown market calendar")
        if self.data_source not in {"replay", "binance", "generic", "upstox"}:
            raise ValueError("Unknown data source")
        if broker == "UPSTOX_SANDBOX":
            self._validate_sandbox_host()
            if not self.upstox_sandbox_access_token.strip():
                raise ValueError(
                    "BROKER=UPSTOX_SANDBOX requires UPSTOX_SANDBOX_ACCESS_TOKEN "
                    "in the environment"
                )
        if self.sandbox_product not in {"I", "D", "MTF"}:
            raise ValueError("SANDBOX_PRODUCT must be I, D or MTF (documented V3 values)")
        if self.live_product not in {"I", "D", "MTF"}:
            raise ValueError("LIVE_PRODUCT must be I, D or MTF (documented V3 values)")
        # V2.1 L-ops: operational rails that can silently do nothing are worse
        # than an explicit refusal, so malformed values fail closed here.
        if self.live_canary_max_notional < 0:
            raise ValueError("LIVE_CANARY_MAX_NOTIONAL cannot be negative")
        if self.live_canary_orders < 0:
            raise ValueError("LIVE_CANARY_ORDERS cannot be negative")
        if self.live_canary_max_notional > 0 and self.live_canary_orders <= 0:
            raise ValueError(
                "LIVE_CANARY_MAX_NOTIONAL is set but LIVE_CANARY_ORDERS=0: the "
                "canary would never end; set LIVE_CANARY_ORDERS>0 or unset the "
                "canary")
        if self.live_token_max_age_hours < 0 or self.live_token_max_age_hours > 24:
            raise ValueError("LIVE_TOKEN_MAX_AGE_HOURS must be 0..24 (0 disables)")
        if self.live_token_warn_minutes < 0 or self.live_token_warn_minutes > 1440:
            raise ValueError("LIVE_TOKEN_WARN_MINUTES must be 0..1440")
        if self.live_stuck_order_minutes <= 0:
            raise ValueError("LIVE_STUCK_ORDER_MINUTES must be > 0")
        if self.live_stuck_order_escalate_minutes < self.live_stuck_order_minutes:
            raise ValueError(
                "LIVE_STUCK_ORDER_ESCALATE_MINUTES must be >= "
                "LIVE_STUCK_ORDER_MINUTES (an escalation cannot precede the "
                "warning it escalates)")
        if self.live_reconcile_silence_minutes <= 0:
            raise ValueError("LIVE_RECONCILE_SILENCE_MINUTES must be > 0")
        if self.live_lock_stale_seconds < 30:
            raise ValueError("LIVE_LOCK_STALE_SECONDS must be >= 30")
        if not 0 <= self.live_unacked_critical_minutes <= 1440:
            raise ValueError("LIVE_UNACKED_CRITICAL_MINUTES must be 0..1440 "
                             "(0 disables the acknowledgement check)")
        if not 0 <= self.live_max_equity_drawdown < 1:
            raise ValueError("LIVE_MAX_EQUITY_DRAWDOWN must be 0..<1 (0 disables)")
        if self.alert_min_severity.upper() not in {"INFO", "WARNING", "CRITICAL"}:
            raise ValueError("ALERT_MIN_SEVERITY must be INFO, WARNING or CRITICAL")
        if self.alert_dedup_seconds < 0:
            raise ValueError("ALERT_DEDUP_SECONDS cannot be negative")
        if not 0 <= self.alert_retry_attempts <= 10:
            raise ValueError("ALERT_RETRY_ATTEMPTS must be 0..10")
        if self.alert_retry_backoff_seconds <= 0:
            raise ValueError("ALERT_RETRY_BACKOFF_SECONDS must be > 0")
        if self.alert_webhook_url and not self.alert_webhook_url.startswith(
                ("http://", "https://")):
            raise ValueError("ALERT_WEBHOOK_URL must be an http(s) URL or empty")
        if self.alert_webhook_url and host_of(self.alert_webhook_url) in (
                LIVE_ORDER_HOSTS | SANDBOX_ORDER_HOSTS):
            raise ValueError(
                "ALERT_WEBHOOK_URL must not point at a trading host")
        for name in ("max_position_value", "max_daily_loss"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name.upper()} cannot be negative")
        for name in ("max_open_positions", "max_trades_per_day"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name.upper()} cannot be negative")
        # 3.0.5 gates: fail-closed validation
        if self.net_edge_threshold < 0 or self.net_edge_threshold > 0.1:
            raise ValueError("NET_EDGE_THRESHOLD must be 0..0.1")
        if not 0 <= self.min_quality_score <= 1:
            raise ValueError("MIN_QUALITY_SCORE must be 0..1")
        if self.max_spread_bps <= 0 or self.max_spread_bps > 1000:
            raise ValueError("MAX_SPREAD_BPS must be 0..1000")
        if not 0 <= self.min_liquidity_score <= 1:
            raise ValueError("MIN_LIQUIDITY_SCORE must be 0..1")
        if self.volatility_lookback <= 0 or self.volatility_lookback > 500:
            raise ValueError("VOLATILITY_LOOKBACK must be 1..500")
        if self.max_volatility_factor <= 0:
            raise ValueError("MAX_VOLATILITY_FACTOR must be > 0")
        if self.cross_market_cache_ttl <= 0:
            raise ValueError("CROSS_MARKET_CACHE_TTL must be > 0")
        if self.entry_timing_lookback <= 0:
            raise ValueError("ENTRY_TIMING_LOOKBACK must be > 0")
        if self.alpha_decay_min_samples <= 0:
            raise ValueError("ALPHA_DECAY_MIN_SAMPLES must be > 0")
        if not 0 <= self.correlation_threshold <= 1:
            raise ValueError("CORRELATION_THRESHOLD must be 0..1")
        if self.scanner_cache_ttl <= 0:
            raise ValueError("SCANNER_CACHE_TTL must be > 0")
        if self.hot_path_max_latency_ms <= 0:
            raise ValueError("HOT_PATH_MAX_LATENCY_MS must be > 0")
        if self.data_source == "upstox":
            if self.market_data_provider != "upstox":
                raise ValueError("MARKET_DATA_PROVIDER must be upstox for DATA_SOURCE=upstox")
            if self.market_data_mode not in {"ltpc", "full"}:
                raise ValueError("MARKET_DATA_MODE must be ltpc or full")
            if self.market_data_timeframe:
                if self.market_data_timeframe not in {"1m", "5m", "15m", "30m", "1h", "1D"}:
                    raise ValueError("Unsupported MARKET_DATA_TIMEFRAME")
                if self.market_data_timeframe != self.default_timeframe:
                    raise ValueError("MARKET_DATA_TIMEFRAME must equal DEFAULT_TIMEFRAME")
            from app.data.upstox_stream import INSTRUMENT_KEY

            for key in self.market_data_symbol_list():
                if not INSTRUMENT_KEY.fullmatch(key):
                    raise ValueError(
                        "MARKET_DATA_SYMBOLS must be comma-separated Upstox instrument "
                        "keys, e.g. NSE_EQ|INE002A01018"
                    )

    def _validate_sandbox_host(self) -> None:
        """The sandbox broker must never be pointed at a live order host."""
        host = host_of(self.upstox_sandbox_base_url)
        if host in FORBIDDEN_IN_SANDBOX:
            raise ValueError(
                f"UPSTOX_SANDBOX_BASE_URL host {host!r} is not a sandbox host "
                f"(sandbox hosts: {', '.join(sorted(SANDBOX_ORDER_HOSTS))}); "
                "refusing to configure a sandbox broker against a live/order host"
            )
        if not is_trusted_upstox_url(
                self.upstox_sandbox_base_url, SANDBOX_ORDER_HOSTS):
            raise ValueError(
                "UPSTOX_SANDBOX_BASE_URL must be an HTTPS root URL on a "
                "documented sandbox host")
        if not is_trusted_upstox_url(
                self.upstox_sandbox_order_history_url, READ_AND_AUTH_HOSTS,
                expected_path="/v2/order/history"):
            raise ValueError(
                "UPSTOX_SANDBOX_ORDER_HISTORY_URL must be the documented "
                "HTTPS Upstox order-history endpoint")

    def _validate_live_mode(self) -> None:
        """All four locks + non-zero risk limits, or refuse to start.

        Fails closed with a single message naming every missing switch, so a
        half-configured live mode can never boot "successfully" and then place
        real orders.
        """
        missing: list[str] = []
        if self.mode.upper() != "LIVE":
            missing.append("MODE=LIVE")
        if self.live_trading is not True:
            missing.append("LIVE_TRADING=true")
        if self.live_confirm.strip() != LIVE_CONFIRM_PHRASE:
            missing.append(f"LIVE_CONFIRM={LIVE_CONFIRM_PHRASE}")
        if not self.upstox_live_access_token.strip():
            missing.append("UPSTOX_LIVE_ACCESS_TOKEN")
        # 0 means "unlimited/disabled" everywhere else; in live mode every
        # hard limit must be explicit, so a runaway loop cannot be unbounded.
        for name in ("max_position_value", "max_daily_loss"):
            if not float(getattr(self, name)) > 0:
                missing.append(f"{name.upper()}>0 (0 = unlimited, refused in live)")
        for name in ("max_open_positions", "max_trades_per_day"):
            if not int(getattr(self, name)) > 0:
                missing.append(f"{name.upper()}>0 (0 = unlimited, refused in live)")
        if missing:
            raise ValueError(
                "BROKER=UPSTOX_LIVE (real money) requires ALL of: "
                + "; ".join(missing)
            )
        host = host_of(self.upstox_live_base_url)
        if host not in LIVE_ORDER_HOSTS:
            raise ValueError(
                f"UPSTOX_LIVE_BASE_URL host {host!r} is not the documented live "
                f"order host ({', '.join(sorted(LIVE_ORDER_HOSTS))})"
            )
        if host in FORBIDDEN_IN_LIVE:
            raise ValueError(
                f"UPSTOX_LIVE_BASE_URL host {host!r} is a sandbox/auth host and "
                "cannot route real orders"
            )
        if not is_trusted_upstox_url(
                self.upstox_live_base_url, LIVE_ORDER_HOSTS):
            raise ValueError(
                "UPSTOX_LIVE_BASE_URL must be an HTTPS root URL on the "
                "documented live order host")
        if not is_trusted_upstox_url(
                self.upstox_live_order_history_url, READ_AND_AUTH_HOSTS,
                expected_path="/v2/order/history"):
            raise ValueError(
                "UPSTOX_LIVE_ORDER_HISTORY_URL must be the documented "
                "HTTPS Upstox order-history endpoint")

    def market_data_symbol_list(self) -> list[str]:
        """Comma-separated MARKET_DATA_SYMBOLS → instrument key list."""
        return [item.strip() for item in self.market_data_symbols.split(",") if item.strip()]

    def execution_kwargs(self) -> dict:
        names = (
            "initial_capital",
            "commission",
            "slippage",
            "spread",
            "position_size",
            "stop_loss",
            "take_profit",
            "max_participation",
        )
        return {name: getattr(self, name) for name in names}

    @classmethod
    def from_env(cls, *, apply_paper_database_override: bool = True):
        # Load every existing .env in priority order. Process environment
        # variables already present always win (override=False); then the
        # first file wins for duplicate keys. In a frozen install this is the
        # per-user/ARENA_ENV_FILE candidate, the executable-adjacent .env, and
        # finally the bundled/repository-root candidate.
        try:
            from dotenv import load_dotenv

            from app import runtime

            for candidate in runtime.env_candidates():
                if candidate.exists():
                    load_dotenv(candidate, override=False)
        except ImportError:
            pass  # Core research/replay also works with stdlib and exported env vars.
        # 3.0.2 fix: conversion follows the DECLARED FIELD ANNOTATION
        # (dataclasses.fields + get_type_hints), never the runtime type of
        # the default value. Every field — including int fields and
        # field(default=...) declarations — is coerced the same way.
        defaults = cls()
        hints = get_type_hints(cls)
        values = {}
        # V0.6.2: documented UPSTOX_* aliases for the market-data fields.
        # Explicit MARKET_DATA_* variables always win. 3.1.0: MARKET_DATA_SOURCE
        # is accepted as an alias for DATA_SOURCE so legacy .env files and old
        # setup screens keep meaning what they say.
        aliases = {
            "market_data_access_token": "UPSTOX_ACCESS_TOKEN",
            "market_data_symbols": "UPSTOX_INSTRUMENT_KEYS",
            "data_source": "MARKET_DATA_SOURCE",
        }
        for f in fields(cls):
            name = f.name
            annotation = hints.get(name, f.type)
            raw = os.getenv(name.upper())
            if raw is None and name in aliases:
                raw = os.getenv(aliases[name])
            if raw is not None:
                values[name] = coerce_env_value(name, annotation, raw)
            else:
                # normalize defaults through the same converter so no
                # int-lit default survives under a float annotation
                values[name] = coerce_env_value(
                    name, annotation, getattr(defaults, name))

        # Packaged/portable installs must not depend on the working directory.
        # A double-clicked .exe on Windows often starts in C:\Windows\System32,
        # so `sqlite:///trading_bot.db` and `data` would resolve somewhere
        # unpredictable and the database would look like it reset each launch.
        # Only UNSET values are redirected: anything the operator wrote in .env
        # is honoured exactly as given.
        # Imported rather than assumed: the core research/replay path is
        # documented to work with a minimal stdlib environment. If the module
        # is somehow absent we simply keep the classic defaults; any real
        # failure inside it is allowed to surface rather than being swallowed.
        try:
            from app import runtime
        except ImportError:  # pragma: no cover - optional import
            runtime = None  # type: ignore[assignment]

        if runtime is not None and runtime.is_portable():
            if not (os.getenv("DATABASE_URL") or "").strip():
                values["database_url"] = runtime.default_database_path()
            if not (os.getenv("DATA_DIR") or "").strip():
                values["data_dir"] = runtime.default_data_dir()

        # An explicitly selected recovery database is a persistent pointer,
        # not a one-launch environment override. It takes precedence over
        # DATABASE_URL and all .env files so a later launch cannot silently
        # fall back to the incompatible original account. The dedicated CLI
        # clear action removes only this pointer; it never deletes either DB.
        if runtime is not None and apply_paper_database_override:
            override = runtime.read_paper_database_override()
            if override is not None:
                if (
                    str(values.get("mode", "PAPER")).upper() != "PAPER"
                    or bool(values.get("live_trading", False))
                    or str(values.get("broker", "LOCAL_PAPER")).upper()
                    != "LOCAL_PAPER"
                ):
                    raise ValueError(
                        "A paper database recovery selection is valid only with "
                        "MODE=PAPER, LIVE_TRADING=false, BROKER=LOCAL_PAPER; "
                        "Arena will not change or enable an execution mode."
                    )
                values["database_url"] = runtime.sqlite_url_for_path(
                    override["database_path"]
                )

        return cls(**values)
