"""Configuration for the separate Upstox autonomy boundary.

The legacy MODE/LIVE_TRADING/BROKER settings deliberately remain unchanged.
This boundary has its own master switch, defaulting to false, and the switch
alone never creates a session, Arms, Activates, or issues an execution permit.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def _float(name: str, default: float, low: float, high: float) -> float:
    raw = os.getenv(name)
    value = float(raw) if raw is not None else default
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _int(name: str, default: int, low: int, high: int) -> int:
    value = int(os.getenv(name, default))
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class AutonomySettings:
    live_enabled: bool = False
    timeframe: str = "1m"
    stale_after_seconds: float = 90.0
    max_bar_future_skew_seconds: float = 5.0
    paper_starting_equity: float = 1_000_000.0
    paper_commission_rate: float = 0.0005
    paper_slippage_rate: float = 0.0005
    paper_spread_rate: float = 0.0002
    max_participation: float = 0.10
    max_spread_bps: float = 50.0
    max_margin_usage_fraction: float = 0.50
    correlation_threshold: float = 0.70
    gap_risk_fraction: float = 0.002
    swing_bars: int = 2
    reward_risk: float = 1.5
    write_rate_per_second: float = 5.0
    max_write_attempts: int = 3
    # Server-side hard limits. All must be positive and bounded: missing,
    # zero or invalid values fail startup closed; limits are never auto-relaxed.
    max_order_quantity: float = 1_000.0
    max_order_notional: float = 500_000.0
    max_per_symbol_exposure: float = 1_000_000.0
    max_aggregate_exposure: float = 2_000_000.0
    max_open_positions: int = 10
    max_daily_loss_ceiling: float = 100_000.0
    max_daily_drawdown: float = 50_000.0
    max_daily_order_submits: int = 200
    max_clock_skew_seconds: float = 30.0
    worker_enabled: bool = True
    scan_interval_seconds: float = 5.0
    expected_public_ip: str = ""
    upstox_algo_name: str = ""
    catalog_url: str = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
    suspended_url: str = "https://assets.upstox.com/market-quote/instruments/exchange/suspended-instrument.json.gz"
    mis_nse_url: str = "https://assets.upstox.com/market-quote/instruments/exchange/NSE_MIS.json.gz"
    mis_bse_url: str = "https://assets.upstox.com/market-quote/instruments/exchange/BSE_MIS.json.gz"
    evidence_dir: str = "data/autonomy/evidence"
    kronos_manifest: str = "config/kronos-small.json"
    kronos_root: str = "data/autonomy/kronos"

    def __post_init__(self) -> None:
        if self.timeframe not in {"1m", "5m", "15m", "30m"}:
            raise ValueError("Autonomy supports completed intraday bars: 1m/5m/15m/30m")
        if not 0 < self.max_margin_usage_fraction <= 1:
            raise ValueError("Invalid aggregate margin bound")
        if not 0 < self.correlation_threshold <= 1:
            raise ValueError("Invalid correlation threshold")
        if not 0 < self.reward_risk <= 10:
            raise ValueError("Invalid reward/risk ratio")
        if self.write_rate_per_second > 10:
            raise ValueError("Regular Upstox algos must remain below 10 order writes/second")
        if not 1 <= self.max_write_attempts <= 5:
            raise ValueError("max_write_attempts must be between 1 and 5")
        hard_limits = {
            "max_order_quantity": self.max_order_quantity,
            "max_order_notional": self.max_order_notional,
            "max_per_symbol_exposure": self.max_per_symbol_exposure,
            "max_aggregate_exposure": self.max_aggregate_exposure,
            "max_daily_loss_ceiling": self.max_daily_loss_ceiling,
            "max_daily_drawdown": self.max_daily_drawdown,
        }
        for name, value in hard_limits.items():
            if not isinstance(value, (int, float)) or not 0 < value < 1e12:
                raise ValueError(f"{name} must be a positive bounded hard limit")
        for name, value in (("max_open_positions", self.max_open_positions),
                            ("max_daily_order_submits", self.max_daily_order_submits)):
            if not isinstance(value, int) or not 0 < value <= 10_000:
                raise ValueError(f"{name} must be a positive bounded hard limit")
        if not 1 <= self.max_clock_skew_seconds <= 3600:
            raise ValueError("max_clock_skew_seconds must be between 1 and 3600")
        if self.max_per_symbol_exposure > self.max_aggregate_exposure:
            raise ValueError("max_per_symbol_exposure may not exceed max_aggregate_exposure")
        if not 0.5 <= self.scan_interval_seconds <= 300:
            raise ValueError("Autonomy scan interval must be between 0.5 and 300 seconds")
        for name in ("expected_public_ip", "upstox_algo_name"):
            value = getattr(self, name)
            if any(c in value for c in "\r\n"):
                raise ValueError(f"Invalid {name}")

    @classmethod
    def from_env(cls) -> "AutonomySettings":
        return cls(
            live_enabled=_bool("AUTONOMY_LIVE_ENABLED", False),
            timeframe=os.getenv("AUTONOMY_TIMEFRAME", "1m"),
            stale_after_seconds=_float("AUTONOMY_STALE_AFTER_SECONDS", 90.0, 10.0, 900.0),
            paper_starting_equity=_float("AUTONOMY_PAPER_STARTING_EQUITY", 1_000_000.0, 1000.0, 1e12),
            paper_commission_rate=_float("AUTONOMY_PAPER_COMMISSION_RATE", 0.0005, 0.0, 0.1),
            paper_slippage_rate=_float("AUTONOMY_PAPER_SLIPPAGE_RATE", 0.0005, 0.0, 0.1),
            paper_spread_rate=_float("AUTONOMY_PAPER_SPREAD_RATE", 0.0002, 0.0, 0.1),
            max_participation=_float("AUTONOMY_MAX_PARTICIPATION", 0.10, 0.0001, 1.0),
            max_spread_bps=_float("AUTONOMY_MAX_SPREAD_BPS", 50.0, 0.1, 1000.0),
            max_margin_usage_fraction=_float("AUTONOMY_MAX_MARGIN_USAGE_FRACTION", 0.50, 0.01, 1.0),
            correlation_threshold=_float("AUTONOMY_CORRELATION_THRESHOLD", 0.70, 0.01, 1.0),
            gap_risk_fraction=_float("AUTONOMY_GAP_RISK_FRACTION", 0.002, 0.0, 0.2),
            swing_bars=_int("AUTONOMY_SWING_BARS", 2, 1, 10),
            reward_risk=_float("AUTONOMY_REWARD_RISK", 1.5, 0.1, 10.0),
            write_rate_per_second=_float("AUTONOMY_WRITE_RATE_PER_SECOND", 5.0, 0.1, 10.0),
            max_write_attempts=_int("AUTONOMY_MAX_WRITE_ATTEMPTS", 3, 1, 5),
            max_order_quantity=_float("AUTONOMY_MAX_ORDER_QUANTITY", 1_000.0, 1.0, 1e9),
            max_order_notional=_float("AUTONOMY_MAX_ORDER_NOTIONAL", 500_000.0, 1.0, 1e12),
            max_per_symbol_exposure=_float("AUTONOMY_MAX_PER_SYMBOL_EXPOSURE", 1_000_000.0, 1.0, 1e12),
            max_aggregate_exposure=_float("AUTONOMY_MAX_AGGREGATE_EXPOSURE", 2_000_000.0, 1.0, 1e12),
            max_open_positions=_int("AUTONOMY_MAX_OPEN_POSITIONS", 10, 1, 500),
            max_daily_loss_ceiling=_float("AUTONOMY_MAX_DAILY_LOSS_CEILING", 100_000.0, 1.0, 1e12),
            max_daily_drawdown=_float("AUTONOMY_MAX_DAILY_DRAWDOWN", 50_000.0, 1.0, 1e12),
            max_daily_order_submits=_int("AUTONOMY_MAX_DAILY_ORDER_SUBMITS", 200, 1, 10_000),
            max_clock_skew_seconds=_float("AUTONOMY_MAX_CLOCK_SKEW_SECONDS", 30.0, 1.0, 3600.0),
            worker_enabled=_bool("AUTONOMY_WORKER_ENABLED", True),
            scan_interval_seconds=_float("AUTONOMY_SCAN_INTERVAL_SECONDS", 5.0, 0.5, 300.0),
            expected_public_ip=os.getenv("AUTONOMY_EXPECTED_PUBLIC_IP", "").strip(),
            upstox_algo_name=os.getenv("AUTONOMY_UPSTOX_ALGO_NAME", "").strip(),
            evidence_dir=os.getenv("AUTONOMY_EVIDENCE_DIR", "data/autonomy/evidence"),
            kronos_manifest=os.getenv("AUTONOMY_KRONOS_MANIFEST", "config/kronos-small.json"),
            kronos_root=os.getenv("AUTONOMY_KRONOS_ROOT", "data/autonomy/kronos"),
        )


def resolve_data_path(data_dir: Path, configured: str) -> Path:
    path = Path(configured)
    return path if path.is_absolute() else data_dir / path.name if path.parent == Path(".") else path
