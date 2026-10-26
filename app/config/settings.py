"""Environment configuration. There is deliberately no real-trading configuration."""

import os
import types
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Union, get_args, get_origin, get_type_hints

ROOT = Path(__file__).resolve().parents[2]


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
    market: str = "india"  # india | us; selects which session rules apply
    market_calendar: str = "continuous"
    default_symbol: str = "DEMO"
    default_timeframe: str = "1D"
    commission: float = 0.0005
    slippage: float = 0.0005
    spread: float = 0.0002
    position_size: float = 0.25
    stop_loss: float | None = 0.05
    take_profit: float | None = 0.10
    max_participation: float = 0.10
    # Dedicated paper entry limits. Zero disables a limit for existing replay
    # workflows; the US Alpaca paper mode below requires every cap explicitly.
    paper_max_position_value: float = 0.0
    paper_max_open_positions: int = 0
    paper_max_daily_loss: float = 0.0
    paper_max_drawdown: float = 0.0
    paper_max_trades_per_day: int = 0
    database_url: str = "sqlite:///trading_bot.db"
    api_token: str = field(default="", repr=False)
    # The HTML pages contain no secrets and every data route stays behind the
    # bearer token. By default a page is only served to a loopback client
    # because a local browser can be trusted with a local page; a reverse
    # proxy makes every client look remote, which would otherwise make the UI
    # unreachable. Set this when the deployment terminates TLS/HTTP in front
    # of AXIOM. It does NOT relax the data routes: without API_TOKEN they
    # still answer 503.
    ui_allow_proxy_pages: bool = False
    market_data_token: str = field(default="", repr=False)
    data_ws_url: str = field(default="", repr=False)
    data_source: str = "replay"
    # V0.6.1 real market DATA (read-only Upstox feed). No real-trading config exists.
    market_data_provider: str = "upstox"
    market_data_symbols: str = ""
    market_data_timeframe: str = ""
    market_data_mode: str = "ltpc"
    market_data_access_token: str = field(default="", repr=False)
    market_data_authorize_url: str = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
    # Alpaca is used only for read-only US equity market data. The paper
    # execution path remains the local PaperBroker; no Alpaca trading client is
    # configured or constructed.
    alpaca_api_key: str = field(default="", repr=False)
    alpaca_api_secret: str = field(default="", repr=False)
    alpaca_data_feed: str = "iex"
    alpaca_symbols: str = ""
    # --- V1 (Arena): explicit broker modes + sandbox + hard risk limits ---
    broker: str = "LOCAL_PAPER"  # LOCAL_PAPER | UPSTOX_SANDBOX (LIVE is not enabled)
    upstox_sandbox_access_token: str = field(default="", repr=False)
    upstox_sandbox_base_url: str = "https://api-hft.upstox.com"
    upstox_sandbox_order_history_url: str = "https://api.upstox.com/v2/order/history"
    sandbox_product: str = "I"  # intraday product per documented V3 order API
    sandbox_order_tag: str = "arena-v1"
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
        if self.mode.upper() != "PAPER" or self.live_trading:
            raise ValueError("Only MODE=PAPER and LIVE_TRADING=false are permitted")
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError("Only SQLite is supported")
        from app.backtesting.costs import ExecutionConfig

        ExecutionConfig(**self.execution_kwargs())
        if self.market_calendar not in {"continuous", "nse-session", "us-equities"}:
            raise ValueError("Unknown market calendar")
        if self.data_source not in {"replay", "binance", "generic", "upstox", "alpaca"}:
            raise ValueError("Unknown data source")
        # V1 broker modes: paper and documented Upstox SANDBOX orders only.
        broker = self.broker.upper()
        if broker == "LIVE":
            raise ValueError("LIVE trading is not enabled in V1")
        if broker not in {"LOCAL_PAPER", "UPSTOX_SANDBOX"}:
            raise ValueError("BROKER must be LOCAL_PAPER or UPSTOX_SANDBOX")
        if broker == "UPSTOX_SANDBOX":
            from app.brokers.sandbox import validate_official_sandbox_endpoints
            validate_official_sandbox_endpoints(
                self.upstox_sandbox_base_url, self.upstox_sandbox_order_history_url
            )
        if broker == "UPSTOX_SANDBOX" and not self.upstox_sandbox_access_token.strip():
            raise ValueError(
                "BROKER=UPSTOX_SANDBOX requires UPSTOX_SANDBOX_ACCESS_TOKEN in the environment"
            )
        if self.sandbox_product not in {"I", "D", "MTF"}:
            raise ValueError("SANDBOX_PRODUCT must be I, D or MTF (documented V3 values)")
        for name in ("max_position_value", "max_daily_loss"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name.upper()} cannot be negative")
        for name in ("max_open_positions", "max_trades_per_day"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name.upper()} cannot be negative")
        for name in ("paper_max_position_value", "paper_max_daily_loss"):
            if getattr(self, name) < 0 or getattr(self, name) != getattr(self, name):
                raise ValueError(f"{name.upper()} must be finite and non-negative")
        for name in ("paper_max_open_positions", "paper_max_trades_per_day"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name.upper()} cannot be negative")
        if not 0 <= self.paper_max_drawdown < 1:
            raise ValueError("PAPER_MAX_DRAWDOWN must be in [0, 1)")
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
        if self.data_source == "alpaca":
            if self.mode.upper() != "PAPER" or self.live_trading or broker != "LOCAL_PAPER":
                raise ValueError("Alpaca US market data is supported only with local PAPER execution")
            if self.market.lower() != "us" or self.currency.upper() != "USD":
                raise ValueError("DATA_SOURCE=alpaca requires MARKET=us and CURRENCY=USD")
            if self.market_calendar != "us-equities":
                raise ValueError("DATA_SOURCE=alpaca requires MARKET_CALENDAR=us-equities")
            if self.alpaca_data_feed not in {"iex", "sip", "delayed_sip"}:
                raise ValueError("ALPACA_DATA_FEED must be iex, sip or delayed_sip")
            from app.data.alpaca import validate_us_symbol

            alpaca_symbols = self.alpaca_symbol_list()
            if len(set(alpaca_symbols)) != len(alpaca_symbols):
                raise ValueError("ALPACA_SYMBOLS must contain distinct tickers")
            for ticker in alpaca_symbols:
                validate_us_symbol(ticker)
            if not self.alpaca_api_key.strip() or not self.alpaca_api_secret.strip():
                raise ValueError("DATA_SOURCE=alpaca requires ALPACA_API_KEY and ALPACA_API_SECRET")
            if self.default_timeframe != "1m":
                raise ValueError("Alpaca paper streaming currently supports DEFAULT_TIMEFRAME=1m only")
            if self.paper_max_position_value <= 0 or self.paper_max_open_positions <= 0:
                raise ValueError("US paper mode requires PAPER_MAX_POSITION_VALUE and PAPER_MAX_OPEN_POSITIONS > 0")
            if self.paper_max_daily_loss <= 0 or self.paper_max_trades_per_day <= 0:
                raise ValueError("US paper mode requires PAPER_MAX_DAILY_LOSS and PAPER_MAX_TRADES_PER_DAY > 0")
            if not 0 < self.paper_max_drawdown < 1:
                raise ValueError("US paper mode requires PAPER_MAX_DRAWDOWN in (0, 1)")

    def market_data_symbol_list(self) -> list[str]:
        """Comma-separated MARKET_DATA_SYMBOLS → instrument key list."""
        return [item.strip() for item in self.market_data_symbols.split(",") if item.strip()]

    def alpaca_symbol_list(self) -> list[str]:
        """Comma-separated plain US tickers for the read-only market-data feed."""
        return [item.strip().upper() for item in self.alpaca_symbols.split(",") if item.strip()]

    def paper_risk_limits(self) -> dict:
        """Entry-only paper limits, separate from execution-cost account schema."""
        return {
            "max_position_value": self.paper_max_position_value,
            "max_open_positions": self.paper_max_open_positions,
            "max_daily_loss": self.paper_max_daily_loss,
            "max_drawdown": self.paper_max_drawdown,
            "max_trades_per_day": self.paper_max_trades_per_day,
        }

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
    def from_env(cls):
        try:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env", override=False)
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
        # Explicit MARKET_DATA_* variables always win.
        aliases = {
            "market_data_access_token": "UPSTOX_ACCESS_TOKEN",
            "market_data_symbols": "UPSTOX_INSTRUMENT_KEYS",
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
        return cls(**values)
