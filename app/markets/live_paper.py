"""Multi-market Local Paper Execution Engine driven by live read-only market data.

Supports ``NSE`` (``INR``) and ``NASDAQ`` (``USD``) side-by-side in isolated
currency ledgers persisted to SQLite.

Hard structural safety guarantees:
- Holds NO broker order client, NO live trading token, and NO order URL.
- Every order, fill, position, P&L figure, and audit row carries
  ``simulated=True`` and ``execution_mode="LIVE_MARKET_PAPER"``.
- Never fabricates or substitutes sample/replay prices: simulated fills require
  a real read-only provider quote/bar timestamp within ``max_quote_age_seconds``
  (unless an operator explicitly runs an offline historical bar evaluation,
  which is labeled accordingly).
"""

from __future__ import annotations

import asyncio
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    wait as wait_for_futures,
)
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable
import uuid

from app.data.models import Candle, parse_timestamp
from app.markets.fetch_gate import FetchGateConfig, ProviderFetchGate
from app.markets.kronos_decision import build_forecast_summary, combine_paper_strategy_decision
from app.markets.nse_reconciliation import nse_session_status, tick_precision_decimals
from app.markets.us_calendar import USEquitiesCalendar
from app.monitoring.paper_events import (
    filter_structured_events,
    normalize_structured_event,
    redact_sensitive_data,
)

MARKETS = ("NSE", "NASDAQ")
SESSION_STATES = (
    "STOPPED",
    "RUNNING",
    "PAUSED",
    "MARKET_CLOSED_WAITING",
    "STALE_FEED_PAUSED",
    "DISCONNECTED_PAUSED",
    "END_OF_DATA",
    "RECOVERED_PAUSED",
    "EMERGENCY_STOPPED",
)
OPEN_ORDER_STATUSES = frozenset({"WORKING", "PARTIALLY_FILLED"})
TERMINAL_ORDER_STATUSES = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED"})


def _utc_now(clock: Callable[[], datetime] | None = None) -> datetime:
    return (clock() if clock else datetime.now(timezone.utc)).astimezone(timezone.utc)


def _valid_tick(price: float, tick_size: float, *, market: str) -> bool:
    if not (math.isfinite(price) and price > 0):
        return False
    dec = Decimal(str(round(price, 6)))
    if market == "NASDAQ":
        quantum = Decimal("0.01") if dec >= Decimal("1.00") else Decimal("0.0001")
    else:
        quantum = Decimal(str(tick_size if tick_size > 0 else 0.05))
    return (dec % quantum) == 0


def _round_to_tick(price: float, tick_size: float, *, market: str, side: str) -> float:
    step = (0.01 if price >= 1.0 else 0.0001) if market == "NASDAQ" else (tick_size if tick_size > 0 else 0.05)
    if side.upper() == "BUY":
        val = math.ceil((price - step * 1e-9) / step) * step
    else:
        val = math.floor((price + step * 1e-9) / step) * step
    decimals = 4 if (market == "NASDAQ" and price < 1.0) else max(2, tick_precision_decimals(step))
    return round(max(step, val), decimals)


@dataclass
class PaperMarketConfig:
    market: str
    currency: str
    starting_cash: float
    max_order_value: float
    max_symbol_exposure: float
    max_total_exposure: float
    max_open_positions: int
    max_daily_loss: float
    max_daily_orders: int
    max_quote_age_seconds: float
    spread_bps: float
    slippage_bps: float
    brokerage_bps: float
    max_bar_participation: float = 0.25

    @classmethod
    def default_nse(cls) -> "PaperMarketConfig":
        return cls(
            market="NSE",
            currency="INR",
            starting_cash=1_000_000.0,
            max_order_value=250_000.0,
            max_symbol_exposure=400_000.0,
            max_total_exposure=900_000.0,
            max_open_positions=10,
            max_daily_loss=25_000.0,
            max_daily_orders=40,
            max_quote_age_seconds=90.0,
            spread_bps=2.5,
            slippage_bps=3.0,
            brokerage_bps=3.0,
            max_bar_participation=0.20,
        )

    @classmethod
    def default_nasdaq(cls) -> "PaperMarketConfig":
        return cls(
            market="NASDAQ",
            currency="USD",
            starting_cash=100_000.0,
            max_order_value=25_000.0,
            max_symbol_exposure=40_000.0,
            max_total_exposure=95_000.0,
            max_open_positions=10,
            max_daily_loss=2_500.0,
            max_daily_orders=40,
            max_quote_age_seconds=90.0,
            spread_bps=1.5,
            slippage_bps=2.0,
            brokerage_bps=0.0,
            max_bar_participation=0.25,
        )

    def configured(self) -> bool:
        return all(
            v > 0
            for v in (
                self.starting_cash,
                self.max_order_value,
                self.max_symbol_exposure,
                self.max_total_exposure,
                self.max_open_positions,
                self.max_daily_loss,
                self.max_daily_orders,
                self.max_quote_age_seconds,
                self.max_bar_participation,
            )
        )


def estimate_market_fees(
    *,
    market: str,
    side: str,
    qty: float,
    fill_price: float,
    product: str,
    brokerage_bps: float,
) -> dict[str, Any]:
    """Compute realistic market-specific simulated regulatory fees and brokerage."""
    notional = abs(float(qty) * float(fill_price))
    side_u = side.upper()
    if market == "NSE":
        is_delivery = product.upper() == "D"
        brokerage = min(notional * max(0.0, brokerage_bps) / 10_000.0, 20.0)
        if is_delivery:
            stt = notional * 0.001
            stamp = notional * 0.00015 if side_u == "BUY" else 0.0
        else:
            stt = notional * 0.00025 if side_u == "SELL" else 0.0
            stamp = notional * 0.00003 if side_u == "BUY" else 0.0
        nse_txn = notional * 0.0000297
        sebi = notional * 0.000001
        gst = 0.18 * (brokerage + nse_txn + sebi)
        total = round(brokerage + stt + nse_txn + sebi + stamp + gst, 2)
        breakdown = {
            "brokerage": round(brokerage, 2),
            "stt": round(stt, 2),
            "exchange_txn_charge": round(nse_txn, 2),
            "sebi_turnover_fee": round(sebi, 2),
            "stamp_duty": round(stamp, 2),
            "gst": round(gst, 2),
        }
        return {
            "currency": "INR",
            "total": total,
            "breakdown": breakdown,
            **breakdown,
        }

    # NASDAQ / US equities regulatory fee model (USD)
    commission = notional * max(0.0, brokerage_bps) / 10_000.0
    sec_fee = max(0.01, notional * 0.0000278) if side_u == "SELL" and notional > 0 else 0.0
    finra_taf = min(8.30, max(0.01, abs(float(qty)) * 0.000166)) if side_u == "SELL" and qty > 0 else 0.0
    total = round(commission + sec_fee + finra_taf, 4)
    breakdown_us = {
        "commission": round(commission, 4),
        "sec_section31_fee": round(sec_fee, 4),
        "finra_taf": round(finra_taf, 4),
    }
    return {
        "currency": "USD",
        "total": total,
        "breakdown": breakdown_us,
        **breakdown_us,
    }


def evaluate_bar_strategy_signal(
    strategy: str,
    candles: list[Candle],
    *,
    kronos_forecast: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic completed-bar signal evaluator for Live-Market Paper strategies."""
    name = (strategy or "manual_only").strip().lower()
    if name == "manual_only":
        return {"action": "HOLD", "reason": "manual_only mode; automated strategy entries disabled"}
    if len(candles) < 21:
        return {"action": "HOLD", "reason": f"insufficient completed bars ({len(candles)}/21)"}
    closes = [bar.close for bar in candles]
    last_bar = candles[-1]
    prev_bar = candles[-2]

    if name in {"moving_average", "ema_crossover"}:
        fast_now = sum(closes[-5:]) / 5.0
        slow_now = sum(closes[-20:]) / 20.0
        fast_prev = sum(closes[-6:-1]) / 5.0
        slow_prev = sum(closes[-21:-1]) / 20.0
        if fast_prev <= slow_prev and fast_now > slow_now:
            return {"action": "BUY", "reason": f"SMA5 ({fast_now:.2f}) crossed above SMA20 ({slow_now:.2f})", "bar_ts": last_bar.timestamp.isoformat()}
        if fast_prev >= slow_prev and fast_now < slow_now:
            return {"action": "SELL", "reason": f"SMA5 ({fast_now:.2f}) crossed below SMA20 ({slow_now:.2f})", "bar_ts": last_bar.timestamp.isoformat()}
        return {"action": "HOLD", "reason": f"SMA5={fast_now:.2f}, SMA20={slow_now:.2f} (no cross)"}

    if name == "rsi":
        gains, losses = [], []
        for prev_c, curr_c in zip(closes[-15:-1], closes[-14:]):
            diff = curr_c - prev_c
            gains.append(max(diff, 0.0))
            losses.append(max(-diff, 0.0))
        avg_gain = sum(gains) / 14.0
        avg_loss = sum(losses) / 14.0
        rsi = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
        if rsi < 30.0:
            return {"action": "BUY", "reason": f"RSI14 oversold ({rsi:.1f} < 30)", "bar_ts": last_bar.timestamp.isoformat()}
        if rsi > 70.0:
            return {"action": "SELL", "reason": f"RSI14 overbought ({rsi:.1f} > 70)", "bar_ts": last_bar.timestamp.isoformat()}
        return {"action": "HOLD", "reason": f"RSI14 neutral ({rsi:.1f})"}

    if name == "breakout":
        prior_high = max(b.high for b in candles[-21:-1])
        prior_low = min(b.low for b in candles[-21:-1])
        if last_bar.close > prior_high:
            return {"action": "BUY", "reason": f"20-bar breakout close {last_bar.close:.2f} > {prior_high:.2f}", "bar_ts": last_bar.timestamp.isoformat()}
        if last_bar.close < prior_low:
            return {"action": "SELL", "reason": f"20-bar breakdown close {last_bar.close:.2f} < {prior_low:.2f}", "bar_ts": last_bar.timestamp.isoformat()}
        return {"action": "HOLD", "reason": f"inside 20-bar range [{prior_low:.2f}, {prior_high:.2f}]"}

    if name in {"mean_reversion", "bollinger"}:
        mean20 = sum(closes[-20:]) / 20.0
        variance = sum((c - mean20) ** 2 for c in closes[-20:]) / 20.0
        std20 = math.sqrt(variance)
        lower = mean20 - 2.0 * std20
        upper = mean20 + 2.0 * std20
        if last_bar.close < lower:
            return {"action": "BUY", "reason": f"close {last_bar.close:.2f} below lower band {lower:.2f}", "bar_ts": last_bar.timestamp.isoformat()}
        if last_bar.close > upper:
            return {"action": "SELL", "reason": f"close {last_bar.close:.2f} above upper band {upper:.2f}", "bar_ts": last_bar.timestamp.isoformat()}
        return {"action": "HOLD", "reason": f"within Bollinger bands [{lower:.2f}, {upper:.2f}]"}

    if name in {"momentum", "macd"}:
        roc = ((last_bar.close - closes[-11]) / closes[-11]) * 100.0 if closes[-11] > 0 else 0.0
        prev_roc = ((prev_bar.close - closes[-12]) / closes[-12]) * 100.0 if closes[-12] > 0 else 0.0
        if prev_roc <= 0 < roc:
            return {"action": "BUY", "reason": f"10-bar momentum turned positive ({roc:.2f}%)", "bar_ts": last_bar.timestamp.isoformat()}
        if prev_roc >= 0 > roc:
            return {"action": "SELL", "reason": f"10-bar momentum turned negative ({roc:.2f}%)", "bar_ts": last_bar.timestamp.isoformat()}
        return {"action": "HOLD", "reason": f"10-bar ROC={roc:.2f}%"}

    if name in {"kronos_ensemble", "kronos_informed"}:
        if (
            not isinstance(kronos_forecast, dict)
            or bool(kronos_forecast.get("mock_outputs_used"))
            or kronos_forecast.get("freshness_state") == "STALE"
        ):
            return {
                "action": "HOLD",
                "reason": "Kronos forecast unavailable or stale; fail-closed no trade",
                "bar_ts": last_bar.timestamp.isoformat(),
            }
        fast_now = sum(closes[-5:]) / 5.0
        slow_now = sum(closes[-20:]) / 20.0
        up_p = float(kronos_forecast.get("up_probability") or 0.0)
        exp_r = float(kronos_forecast.get("expected_return_pct") or 0.0)
        sha_tag = str(((kronos_forecast.get("model_identity") or {}).get("manifest_sha256") or ""))[:10]
        if fast_now > slow_now and up_p >= 0.55 and exp_r > 0.0:
            return {
                "action": "BUY",
                "reason": f"Kronos ensemble BUY: SMA5>{slow_now:.2f}, P(up)={up_p:.2f}, E[ret]={exp_r:+.2f}% [sha:{sha_tag}]",
                "bar_ts": last_bar.timestamp.isoformat(),
            }
        if fast_now < slow_now and up_p <= 0.45 and exp_r < 0.0:
            return {
                "action": "SELL",
                "reason": f"Kronos ensemble SELL: SMA5<{slow_now:.2f}, P(up)={up_p:.2f}, E[ret]={exp_r:+.2f}% [sha:{sha_tag}]",
                "bar_ts": last_bar.timestamp.isoformat(),
            }
        return {
            "action": "HOLD",
            "reason": f"Kronos advisory neutral: P(up)={up_p:.2f}, E[ret]={exp_r:+.2f}% [sha:{sha_tag}]",
            "bar_ts": last_bar.timestamp.isoformat(),
        }

    return {"action": "HOLD", "reason": f"unrecognized strategy {name!r}; holding"}


class LiveMarketPaperEngine:
    """SQLite-persisted multi-market local paper trading engine (NSE INR + NASDAQ USD)."""

    broker_order_endpoints_reachable = False

    def __init__(
        self,
        db_path: Path | str,
        *,
        nse_quote_provider: Callable[[str], dict[str, Any]] | None = None,
        nasdaq_quote_provider: Callable[[str], dict[str, Any]] | None = None,
        nse_bars_provider: Callable[[str, str], list[Candle]] | None = None,
        nasdaq_bars_provider: Callable[[str, str], list[Candle]] | None = None,
        kronos_provider: Any | None = None,
        ensemble_service: Any | None = None,
        nse_instrument_resolver: Callable[[str], dict[str, Any] | None] | None = None,
        nasdaq_instrument_resolver: Callable[[str], dict[str, Any] | None] | None = None,
        execution_mode_provider: Callable[[str], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        nse_fetch_gate: ProviderFetchGate | None = None,
        nasdaq_fetch_gate: ProviderFetchGate | None = None,
    ) -> None:
        self._in_memory = str(db_path) == ":memory:"
        self.db_path = Path(":memory:") if self._in_memory else Path(db_path)
        self._lock = threading.RLock()
        self._mem_conn: sqlite3.Connection | None = None
        if self._in_memory:
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.nse_quote_provider = nse_quote_provider
        self.nasdaq_quote_provider = nasdaq_quote_provider
        self.nse_bars_provider = nse_bars_provider
        self.nasdaq_bars_provider = nasdaq_bars_provider
        self.kronos_provider = kronos_provider
        self.ensemble_service = ensemble_service
        self.nse_instrument_resolver = nse_instrument_resolver
        self.nasdaq_instrument_resolver = nasdaq_instrument_resolver
        # LIVE-mode markets are executed only by app.markets.live_execution; the
        # local paper engine must never simulate fills for a market routed LIVE.
        self.execution_mode_provider = execution_mode_provider
        # One bounded, rate-limit-aware gate per provider: every per-symbol fetch
        # in this engine (scan probes, quote marks, bar history) shares it, so a
        # 100-symbol cycle is paced instead of fired and one 429 backs the whole
        # provider off instead of being retried by every worker.
        self.fetch_gates: dict[str, ProviderFetchGate] = {
            "NSE": nse_fetch_gate or ProviderFetchGate(FetchGateConfig()),
            "NASDAQ": nasdaq_fetch_gate or ProviderFetchGate(FetchGateConfig()),
        }
        self.us_calendar = USEquitiesCalendar()
        self._last_quote_ts_by_key: dict[tuple[str, str], datetime] = {}

        self.configs: dict[str, PaperMarketConfig] = {
            "NSE": PaperMarketConfig.default_nse(),
            "NASDAQ": PaperMarketConfig.default_nasdaq(),
        }
        self._market_cycle_locks: dict[str, threading.RLock] = {
            m: threading.RLock() for m in MARKETS
        }
        self._closed = False
        self._init_schema()
        self._bootstrap_or_recover()
        if self.kronos_provider is not None and hasattr(self.kronos_provider, "register_event_sink"):
            self.kronos_provider.register_event_sink(self._on_kronos_lifecycle_event)

    def _on_kronos_lifecycle_event(self, ev: dict[str, Any]) -> None:
        if self._closed:
            return
        mkt = str(ev.get("market") or "").upper()
        if mkt not in MARKETS:
            return
        payload = dict(ev.get("payload") or {})
        with self._lock, self._connect() as conn:
            self._append_audit_conn(conn, mkt, str(ev.get("event_type") or "KRONOS_EVENT"), payload)

    def _connect(self) -> sqlite3.Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS live_paper_accounts (
                    market TEXT PRIMARY KEY,
                    currency TEXT NOT NULL,
                    cash REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    day_start_equity REAL NOT NULL,
                    trading_day TEXT NOT NULL,
                    session_state TEXT NOT NULL,
                    state_reason TEXT,
                    strategy TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    symbols_json TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    session_run_id TEXT,
                    session_start_equity REAL,
                    session_start_realized_pnl REAL,
                    started_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_paper_positions (
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    qty REAL NOT NULL,
                    avg_entry_price REAL NOT NULL,
                    last_price REAL NOT NULL,
                    last_quote_ts TEXT,
                    last_quote_source TEXT,
                    product TEXT NOT NULL,
                    stop_loss REAL,
                    take_profit REAL,
                    opened_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (market, symbol)
                );
                CREATE TABLE IF NOT EXISTS live_paper_orders (
                    client_order_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    market TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    product TEXT NOT NULL,
                    time_in_force TEXT NOT NULL,
                    qty REAL NOT NULL,
                    filled_qty REAL NOT NULL,
                    remaining_qty REAL NOT NULL,
                    limit_price REAL,
                    stop_price REAL,
                    stop_loss REAL,
                    take_profit REAL,
                    avg_fill_price REAL,
                    fees_total REAL NOT NULL,
                    status TEXT NOT NULL,
                    reject_reason TEXT,
                    quote_source TEXT,
                    quote_timestamp TEXT,
                    order_date TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_paper_fills (
                    fill_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    market TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    instrument_key TEXT,
                    session_run_id TEXT,
                    side TEXT NOT NULL,
                    fill_qty REAL NOT NULL,
                    fill_price REAL NOT NULL,
                    mid_reference_price REAL NOT NULL,
                    slippage_amount REAL NOT NULL,
                    fees_total REAL NOT NULL,
                    fees_json TEXT NOT NULL,
                    realized_pnl_delta REAL NOT NULL,
                    quote_source TEXT NOT NULL,
                    quote_timestamp TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_paper_decisions (
                    decision_id TEXT PRIMARY KEY,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    bar_timestamp TEXT,
                    order_client_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_paper_watchlists (
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY (market, symbol)
                );
                CREATE TABLE IF NOT EXISTS live_paper_audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    market TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            # Idempotent column migrations so existing %LOCALAPPDATA%\AXIOM DBs are preserved intact
            acct_cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(live_paper_accounts)").fetchall()
            }
            for col_name, col_def in (
                ("session_run_id", "TEXT"),
                ("session_start_equity", "REAL"),
                ("session_start_realized_pnl", "REAL"),
                ("kronos_enabled", "INTEGER"),
                ("kronos_failure_policy", "TEXT"),
                ("selected_for_multi", "INTEGER NOT NULL DEFAULT 1"),
                ("last_analysis_json", "TEXT"),
            ):
                if col_name not in acct_cols:
                    conn.execute(f"ALTER TABLE live_paper_accounts ADD COLUMN {col_name} {col_def}")

            fill_cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(live_paper_fills)").fetchall()
            }
            for col_name, col_def in (
                ("instrument_key", "TEXT"),
                ("session_run_id", "TEXT"),
            ):
                if col_name not in fill_cols:
                    conn.execute(f"ALTER TABLE live_paper_fills ADD COLUMN {col_name} {col_def}")

            dec_cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(live_paper_decisions)").fetchall()
            }
            for col_name, col_def in (
                ("session_run_id", "TEXT"),
                ("kronos_status", "TEXT"),
                ("forecast_produced", "INTEGER DEFAULT 0"),
                ("forecast_consumed", "INTEGER DEFAULT 0"),
                ("policy_applied", "TEXT"),
                ("explanation_json", "TEXT"),
            ):
                if col_name not in dec_cols:
                    conn.execute(f"ALTER TABLE live_paper_decisions ADD COLUMN {col_name} {col_def}")

    def _bootstrap_or_recover(self) -> None:
        now = _utc_now(self._clock)
        today = now.date().isoformat()
        with self._lock, self._connect() as conn:
            for mkt in MARKETS:
                cfg = self.configs[mkt]
                row = conn.execute(
                    "SELECT * FROM live_paper_accounts WHERE market = ?", (mkt,)
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO live_paper_accounts
                        (market, currency, cash, realized_pnl, day_start_equity, trading_day,
                         session_state, state_reason, strategy, timeframe, symbols_json,
                         config_json, started_at, updated_at)
                        VALUES (?, ?, ?, 0.0, ?, ?, 'STOPPED', NULL, 'manual_only', '5m', '[]', ?, NULL, ?)
                        """,
                        (
                            mkt,
                            cfg.currency,
                            cfg.starting_cash,
                            cfg.starting_cash,
                            today,
                            json.dumps(asdict(cfg), sort_keys=True),
                            now.isoformat(),
                        ),
                    )
                else:
                    saved_cfg = json.loads(row["config_json"])
                    self.configs[mkt] = PaperMarketConfig(**saved_cfg)
                    prior_state = str(row["session_state"])
                    if prior_state in {"RUNNING", "MARKET_CLOSED_WAITING", "STALE_FEED_PAUSED", "DISCONNECTED_PAUSED"}:
                        conn.execute(
                            """
                            UPDATE live_paper_accounts
                            SET session_state = 'RECOVERED_PAUSED',
                                state_reason = 'Recovered persisted paper session after process restart; click Resume to continue.',
                                updated_at = ?
                            WHERE market = ?
                            """,
                            (now.isoformat(), mkt),
                        )
                        self._append_audit_conn(
                            conn,
                            mkt,
                            "PAPER_SESSION_RECOVERED",
                            {"prior_state": prior_state, "recovered_at": now.isoformat()},
                        )

            # Seed default watchlists on first boot so operator immediately sees NSE + NASDAQ symbols
            existing_watch = conn.execute("SELECT COUNT(*) AS n FROM live_paper_watchlists").fetchone()["n"]
            if existing_watch == 0:
                defaults = [
                    ("NSE", "RELIANCE", "NSE_EQ|INE002A01018", "Reliance Industries Ltd"),
                    ("NSE", "TCS", "NSE_EQ|INE467B01029", "Tata Consultancy Services"),
                    ("NSE", "HDFCBANK", "NSE_EQ|INE040A01034", "HDFC Bank Ltd"),
                    ("NSE", "INFY", "NSE_EQ|INE009A01021", "Infosys Ltd"),
                    ("NSE", "NIFTYBEES", "NSE_EQ|INF204KB14I2", "Nippon Nifty 50 BeES ETF"),
                    ("NASDAQ", "AAPL", "NASDAQ|AAPL", "Apple Inc."),
                    ("NASDAQ", "MSFT", "NASDAQ|MSFT", "Microsoft Corporation"),
                    ("NASDAQ", "NVDA", "NASDAQ|NVDA", "NVIDIA Corporation"),
                    ("NASDAQ", "AMZN", "NASDAQ|AMZN", "Amazon.com Inc."),
                    ("NASDAQ", "QQQ", "NASDAQ|QQQ", "Invesco QQQ Trust ETF"),
                ]
                for mkt, sym, ikey, label in defaults:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO live_paper_watchlists
                        (market, symbol, instrument_key, name, added_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (mkt, sym, ikey, label, now.isoformat()),
                    )

    def _append_audit_conn(
        self,
        conn: sqlite3.Connection,
        market: str,
        event_kind: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        now_iso = _utc_now(self._clock).isoformat()
        safe_payload = redact_sensitive_data(payload)
        if not isinstance(safe_payload, dict):
            safe_payload = {"detail": str(safe_payload)}
        enriched = {
            "simulated": True,
            "real_money": False,
            "execution_mode": "LIVE_MARKET_PAPER",
            "market": market,
            **safe_payload,
        }
        serialized = json.dumps(enriched, sort_keys=True)
        last = conn.execute(
            "SELECT entry_hash FROM live_paper_audit ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = str(last["entry_hash"]) if last else "GENESIS"
        digest = hashlib.sha256(
            f"{prev_hash}|{market}|{event_kind}|{now_iso}|{serialized}".encode("utf-8")
        ).hexdigest()
        conn.execute(
            """
            INSERT INTO live_paper_audit (market, event_kind, payload_json, prev_hash, entry_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (market, event_kind, serialized, prev_hash, digest, now_iso),
        )
        return {"event_kind": event_kind, "entry_hash": digest, "created_at": now_iso}

    def _validate_market(self, market: str) -> str:
        mkt = (market or "").strip().upper()
        if mkt not in MARKETS:
            raise ValueError(f"Unsupported market {market!r}; expected NSE or NASDAQ")
        return mkt

    def _market_session_info(self, market: str, now: datetime | None = None) -> dict[str, Any]:
        current = now or _utc_now(self._clock)
        if market == "NSE":
            info = nse_session_status(current)
            return {"is_open": bool(info["is_open"]), "state": info["state"], "detail": info}
        us_info = self.us_calendar.market_state(current)
        return {"is_open": bool(us_info["market_open"]), "state": us_info["status"], "detail": us_info}

    def _resolve_instrument(self, market: str, symbol_or_key: str) -> dict[str, Any]:
        if market == "NSE":
            if self.nse_instrument_resolver is not None:
                found = self.nse_instrument_resolver(symbol_or_key)
                if found:
                    return found
            raise ValueError(f"Unknown or unsupported NSE instrument: {symbol_or_key}")
        if self.nasdaq_instrument_resolver is not None:
            found = self.nasdaq_instrument_resolver(symbol_or_key)
            if found:
                return found
        raise ValueError(f"Unknown or unsupported NASDAQ instrument: {symbol_or_key}")

    def _fetch_live_quote(self, market: str, inst: dict[str, Any], *, quote_override: dict[str, Any] | None = None) -> dict[str, Any]:
        if quote_override is not None:
            return dict(quote_override)
        if market == "NSE":
            if self.nse_quote_provider is None:
                raise RuntimeError("Upstox NSE read-only quote provider is not configured")
            return self.nse_quote_provider(str(inst["instrument_key"]))
        if self.nasdaq_quote_provider is None:
            raise RuntimeError("Alpaca NASDAQ read-only quote provider is not configured")
        return self.nasdaq_quote_provider(str(inst["symbol"]))

    # -- Watchlists ------------------------------------------------------------

    def list_watchlists(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {"NSE": [], "NASDAQ": []}
        with self._lock, self._connect() as conn:
            for row in conn.execute("SELECT * FROM live_paper_watchlists ORDER BY market, symbol"):
                out.setdefault(str(row["market"]), []).append(dict(row))
        return out

    def _validate_quote_for_paper(
        self,
        market: str,
        inst: dict[str, Any],
        quote_data: dict[str, Any] | None,
        now: datetime,
        *,
        sess_info: dict[str, Any],
        record_monotonic_watermark: bool = True,
    ) -> dict[str, Any]:
        """Validate a read-only provider quote fail-closed for live-market PAPER decisions/fills."""
        cfg = self.configs[market]
        ikey = str(inst.get("instrument_key") or f"{market}|{inst.get('symbol')}")
        if not isinstance(quote_data, dict) or not quote_data:
            return {
                "valid_for_fill": False,
                "reason_code": "MISSING_QUOTE",
                "reason_detail": f"live {market} quote is missing (zero sample/replay fallback)",
                "quote_age_seconds": None,
                "source_timestamp": None,
                "local_received_at": now.isoformat(),
                "feed": None,
                "feed_entitlement_label": None,
                "is_delayed": False,
            }
        try:
            q_last = float(quote_data.get("last") if quote_data.get("last") is not None else 0.0)
            q_bid = float(quote_data.get("bid") if quote_data.get("bid") is not None else q_last)
            q_ask = float(quote_data.get("ask") if quote_data.get("ask") is not None else q_last)
            q_vol = float(quote_data.get("volume") if quote_data.get("volume") is not None else 0.0)
        except (TypeError, ValueError):
            return {
                "valid_for_fill": False,
                "reason_code": "MALFORMED_QUOTE",
                "reason_detail": f"malformed non-numeric quote fields for {ikey}",
                "quote_age_seconds": None,
                "source_timestamp": None,
                "local_received_at": now.isoformat(),
                "feed": quote_data.get("feed"),
                "feed_entitlement_label": quote_data.get("feed_entitlement_label") or quote_data.get("feed_label"),
                "is_delayed": bool(quote_data.get("is_delayed", False)),
            }

        if (
            not all(math.isfinite(v) for v in (q_last, q_bid, q_ask, q_vol))
            or q_last <= 0.0
            or q_bid < 0.0
            or q_ask < 0.0
            or q_vol < 0.0
            or (q_bid > 0.0 and q_ask > 0.0 and q_bid > q_ask)
        ):
            return {
                "valid_for_fill": False,
                "reason_code": "MALFORMED_QUOTE",
                "reason_detail": (
                    f"malformed {market} quote for {ikey}: last={q_last}, bid={q_bid}, ask={q_ask}, volume={q_vol}"
                ),
                "quote_age_seconds": None,
                "source_timestamp": str(quote_data.get("timestamp") or "") or None,
                "local_received_at": now.isoformat(),
                "feed": quote_data.get("feed"),
                "feed_entitlement_label": quote_data.get("feed_entitlement_label") or quote_data.get("feed_label"),
                "is_delayed": bool(quote_data.get("is_delayed", False)),
            }

        raw_ts = quote_data.get("timestamp")
        if raw_ts is None or str(raw_ts).strip() == "":
            return {
                "valid_for_fill": False,
                "reason_code": "MALFORMED_QUOTE",
                "reason_detail": f"missing source timestamp in {market} quote for {ikey}",
                "quote_age_seconds": None,
                "source_timestamp": None,
                "local_received_at": now.isoformat(),
                "feed": quote_data.get("feed"),
                "feed_entitlement_label": quote_data.get("feed_entitlement_label") or quote_data.get("feed_label"),
                "is_delayed": bool(quote_data.get("is_delayed", False)),
            }
        try:
            q_ts = parse_timestamp(str(raw_ts))
        except (TypeError, ValueError) as exc:
            return {
                "valid_for_fill": False,
                "reason_code": "MALFORMED_QUOTE",
                "reason_detail": f"invalid quote timestamp for {ikey}: {exc}",
                "quote_age_seconds": None,
                "source_timestamp": str(raw_ts),
                "local_received_at": now.isoformat(),
                "feed": quote_data.get("feed"),
                "feed_entitlement_label": quote_data.get("feed_entitlement_label") or quote_data.get("feed_label"),
                "is_delayed": bool(quote_data.get("is_delayed", False)),
            }

        future_skew = (q_ts - now).total_seconds()
        if future_skew > 5.0:
            return {
                "valid_for_fill": False,
                "reason_code": "MALFORMED_QUOTE",
                "reason_detail": f"future-skewed quote timestamp (+{future_skew:.1f}s ahead of local clock) for {ikey}",
                "quote_age_seconds": 0.0,
                "source_timestamp": q_ts.isoformat(),
                "local_received_at": str(quote_data.get("local_received_at") or now.isoformat()),
                "feed": quote_data.get("feed"),
                "feed_entitlement_label": quote_data.get("feed_entitlement_label") or quote_data.get("feed_label"),
                "is_delayed": bool(quote_data.get("is_delayed", False)),
            }

        prev_ts = self._last_quote_ts_by_key.get((market, ikey))
        if prev_ts is not None and q_ts < prev_ts:
            return {
                "valid_for_fill": False,
                "reason_code": "OUT_OF_ORDER_QUOTE",
                "reason_detail": (
                    f"out-of-order quote timestamp for {ikey}: {q_ts.isoformat()} < previous {prev_ts.isoformat()}"
                ),
                "quote_age_seconds": round(max(0.0, (now - q_ts).total_seconds()), 2),
                "source_timestamp": q_ts.isoformat(),
                "local_received_at": str(quote_data.get("local_received_at") or now.isoformat()),
                "feed": quote_data.get("feed"),
                "feed_entitlement_label": quote_data.get("feed_entitlement_label") or quote_data.get("feed_label"),
                "is_delayed": bool(quote_data.get("is_delayed", False)),
            }

        feed_val = str(quote_data.get("feed") or ("upstox_nse_v2" if market == "NSE" else "iex")).lower()
        is_delayed = bool(quote_data.get("is_delayed", False)) or feed_val == "delayed_sip"
        entitlement_lbl = str(
            quote_data.get("feed_entitlement_label")
            or quote_data.get("feed_label")
            or (
                "Upstox NSE Cash Read-Only Feed (NSE_EQ)"
                if market == "NSE"
                else (
                    "IEX single-venue feed — NOT the consolidated SIP tape"
                    if feed_val == "iex"
                    else (
                        "SIP consolidated feed delayed 15 minutes — NOT real-time"
                        if is_delayed
                        else "SIP consolidated feed (all US exchanges)"
                    )
                )
            )
        )
        quote_age = round(max(0.0, (now - q_ts).total_seconds()), 2)

        if is_delayed:
            return {
                "valid_for_fill": False,
                "reason_code": "DELAYED_FEED_BLOCKED",
                "reason_detail": f"delayed feed ({feed_val}: {entitlement_lbl}) cannot drive live-market PAPER entries",
                "quote_age_seconds": quote_age,
                "source_timestamp": q_ts.isoformat(),
                "local_received_at": str(quote_data.get("local_received_at") or now.isoformat()),
                "feed": feed_val,
                "feed_entitlement_label": entitlement_lbl,
                "is_delayed": True,
            }

        if not sess_info["is_open"]:
            return {
                "valid_for_fill": False,
                "reason_code": "CLOSED_MARKET_SESSION",
                "reason_detail": f"{market} regular market session is closed ({sess_info['state']})",
                "quote_age_seconds": quote_age,
                "source_timestamp": q_ts.isoformat(),
                "local_received_at": str(quote_data.get("local_received_at") or now.isoformat()),
                "feed": feed_val,
                "feed_entitlement_label": entitlement_lbl,
                "is_delayed": False,
            }

        if quote_age > cfg.max_quote_age_seconds:
            return {
                "valid_for_fill": False,
                "reason_code": "STALE_QUOTE",
                "reason_detail": (
                    f"stale {market} quote for {ikey}: age={quote_age:.1f}s exceeds max_quote_age_seconds={cfg.max_quote_age_seconds:.0f}s"
                ),
                "quote_age_seconds": quote_age,
                "source_timestamp": q_ts.isoformat(),
                "local_received_at": str(quote_data.get("local_received_at") or now.isoformat()),
                "feed": feed_val,
                "feed_entitlement_label": entitlement_lbl,
                "is_delayed": False,
            }

        if record_monotonic_watermark:
            self._last_quote_ts_by_key[(market, ikey)] = q_ts

        return {
            "valid_for_fill": True,
            "reason_code": "OK",
            "reason_detail": f"fresh live {market} quote ({quote_age:.1f}s old · {entitlement_lbl})",
            "quote_age_seconds": quote_age,
            "source_timestamp": q_ts.isoformat(),
            "local_received_at": str(quote_data.get("local_received_at") or now.isoformat()),
            "feed": feed_val,
            "feed_entitlement_label": entitlement_lbl,
            "is_delayed": False,
        }

    @staticmethod
    def _classify_fetch_exception(market: str, exc: Exception) -> tuple[str, str]:
        msg = str(exc)
        low = msg.lower()
        # Upstox raises typed read-only errors (never a generic HTTPError). Their
        # class names are stable, so map them before falling back to text matching:
        # an illiquid snapshot is NOT a disconnect and must not be labelled one.
        exc_name = type(exc).__name__
        if exc_name == "NoTradedQuote":
            return "NO_TRADED_PRICE", f"{market} market-data returned no traded price: {msg[:180]}"
        if exc_name == "InvalidQuote":
            return "MALFORMED_QUOTE", f"{market} market-data returned an invalid quote: {msg[:180]}"
        if exc_name == "UpstoxAuthError" or "expired the daily credential" in low:
            return "AUTH_REJECTED", f"{market} market-data credential rejected/expired: {msg[:180]}"
        if "429" in low or "rate limit" in low:
            return "RATE_LIMITED", f"{market} market-data rate limited (HTTP 429): {msg[:180]}"
        if "rejected" in low or "401" in low or "403" in low or "expired" in low:
            return "AUTH_REJECTED", f"{market} market-data credential rejected/expired: {msg[:180]}"
        if "not configured" in low or "missing" in low:
            return "DISCONNECTED", f"{market} market-data disconnected: {msg[:180]}"
        if "malformed" in low or "invalid" in low or "future" in low or "no usable quote" in low:
            return "MALFORMED_QUOTE", f"{market} market-data returned malformed quote: {msg[:180]}"
        return "DISCONNECTED", f"live {market} market data unavailable: {msg[:180]}"

    def add_watchlist_symbol(self, market: str, symbol_or_key: str) -> dict[str, Any]:
        mkt = self._validate_market(market)
        inst = self._resolve_instrument(mkt, symbol_or_key)
        base_sym = str(inst.get("trading_symbol") or inst.get("symbol") or symbol_or_key).upper()
        ikey = str(inst.get("instrument_key") or f"{mkt}|{base_sym}")
        series = str(inst.get("instrument_type") or "EQ").upper()
        name = str(inst.get("name") or base_sym)
        now_iso = _utc_now(self._clock).isoformat()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT instrument_key FROM live_paper_watchlists WHERE market = ? AND symbol = ?",
                (mkt, base_sym),
            ).fetchone()
            sym = (
                f"{base_sym}-{series}"
                if (existing is not None and str(existing["instrument_key"]) != ikey and mkt == "NSE")
                else base_sym
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO live_paper_watchlists (market, symbol, instrument_key, name, added_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (mkt, sym, ikey, name, now_iso),
            )
        wlists = self.list_watchlists()
        return {
            "added": True,
            "market": mkt,
            "symbol": sym,
            "instrument_key": ikey,
            "name": name,
            "items": [r["symbol"] for r in wlists.get(mkt, [])],
            "watchlists": wlists,
        }

    def remove_watchlist_symbol(self, market: str, symbol: str) -> dict[str, Any]:
        mkt = self._validate_market(market)
        sym = (symbol or "").strip().upper()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM live_paper_watchlists WHERE market = ? AND (symbol = ? OR instrument_key = ?)",
                (mkt, sym, symbol.strip()),
            )
        return {"removed": cur.rowcount > 0, "market": mkt, "symbol": sym}

    # -- Multi-Market Selection & Session Controls -----------------------------

    def selected_markets(self) -> list[str]:
        """Return markets currently selected for concurrent analysis & paper execution."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT market, selected_for_multi FROM live_paper_accounts"
            ).fetchall()
        by_mkt = {str(r["market"]).upper(): bool(r["selected_for_multi"]) for r in rows}
        return [m for m in MARKETS if by_mkt.get(m, True)]

    def set_selected_markets(self, markets: list[str]) -> dict[str, Any]:
        """Update selected markets without resetting any account, position, order, or watchlist."""
        cleaned: list[str] = []
        for raw in markets or []:
            m = self._validate_market(raw)
            if m not in cleaned:
                cleaned.append(m)
        if not cleaned:
            raise ValueError("Select at least one market (NSE and/or NASDAQ)")
        now_iso = _utc_now(self._clock).isoformat()
        with self._lock, self._connect() as conn:
            for mkt in MARKETS:
                sel_flag = 1 if mkt in cleaned else 0
                conn.execute(
                    "UPDATE live_paper_accounts SET selected_for_multi = ?, updated_at = ? WHERE market = ?",
                    (sel_flag, now_iso, mkt),
                )
                self._append_audit_conn(
                    conn,
                    mkt,
                    "PAPER_SELECTED_MARKETS_UPDATED",
                    {"selected": bool(sel_flag), "selected_markets": cleaned},
                )
        return {"ok": True, "selected_markets": cleaned}

    def _effective_symbols_for_market(
        self,
        market: str,
        explicit_symbols: list[str] | None = None,
    ) -> list[str]:
        """Resolve selected symbols for a market: explicit list -> saved session symbols -> pinned watchlist."""
        mkt = self._validate_market(market)
        has_explicit = bool(explicit_symbols and any(s and str(s).strip() for s in explicit_symbols))
        candidates: list[str] = [s for s in (explicit_symbols or []) if s and str(s).strip()]
        wl_symbol_by_candidate: dict[str, str] = {}
        if not candidates:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT symbols_json FROM live_paper_accounts WHERE market = ?",
                    (mkt,),
                ).fetchone()
                saved = json.loads(row["symbols_json"] or "[]") if row else []
                if saved:
                    candidates = [str(s) for s in saved if s and str(s).strip()]
                else:
                    wl_rows = conn.execute(
                        "SELECT symbol, instrument_key FROM live_paper_watchlists WHERE market = ? ORDER BY added_at ASC, symbol ASC",
                        (mkt,),
                    ).fetchall()
                    for wr in wl_rows:
                        sym_txt = str(wr["symbol"] or "").strip()
                        ikey_txt = str(wr["instrument_key"] or "").strip()
                        if mkt == "NSE" and ikey_txt:
                            candidates.append(ikey_txt)
                            wl_symbol_by_candidate[ikey_txt] = sym_txt or ikey_txt
                        elif sym_txt:
                            candidates.append(sym_txt)
                            wl_symbol_by_candidate[sym_txt] = sym_txt
        has_resolver = (
            self.nse_instrument_resolver is not None
            if mkt == "NSE"
            else self.nasdaq_instrument_resolver is not None
        )
        resolved: list[str] = []
        seen: set[str] = set()
        for s in candidates:
            if not has_resolver and not has_explicit:
                fb_sym = wl_symbol_by_candidate.get(s, str(s).split("|")[-1]).upper()
                if fb_sym and fb_sym not in seen:
                    seen.add(fb_sym)
                    resolved.append(fb_sym)
                continue
            try:
                inst = self._resolve_instrument(mkt, s)
            except ValueError:
                if has_explicit:
                    raise
                continue
            if mkt == "NSE" and "|" in str(s) and has_explicit:
                key_out = str(inst.get("instrument_key") or inst.get("trading_symbol"))
            else:
                key_out = str(inst.get("trading_symbol") or inst.get("symbol"))
            canon = str(inst.get("trading_symbol") or inst.get("symbol") or key_out).upper()
            if canon not in seen:
                seen.add(canon)
                resolved.append(key_out)
        return resolved

    def update_config(self, market: str, **updates: Any) -> dict[str, Any]:
        mkt = self._validate_market(market)
        current = asdict(self.configs[mkt])
        strat_update = str(updates["strategy"]).strip() if updates.get("strategy") else None
        tf_update = str(updates["timeframe"]).strip() if updates.get("timeframe") else None
        for key, val in updates.items():
            if key in current and key not in {"market", "currency"} and val is not None:
                current[key] = type(current[key])(val)
        cfg = PaperMarketConfig(**current)
        if not cfg.configured():
            raise ValueError("All paper risk and capital limits must be positive")
        self.configs[mkt] = cfg
        now_iso = _utc_now(self._clock).isoformat()
        with self._lock, self._connect() as conn:
            if strat_update and tf_update:
                conn.execute(
                    "UPDATE live_paper_accounts SET config_json = ?, strategy = ?, timeframe = ?, updated_at = ? WHERE market = ?",
                    (json.dumps(asdict(cfg), sort_keys=True), strat_update, tf_update, now_iso, mkt),
                )
            elif strat_update:
                conn.execute(
                    "UPDATE live_paper_accounts SET config_json = ?, strategy = ?, updated_at = ? WHERE market = ?",
                    (json.dumps(asdict(cfg), sort_keys=True), strat_update, now_iso, mkt),
                )
            elif tf_update:
                conn.execute(
                    "UPDATE live_paper_accounts SET config_json = ?, timeframe = ?, updated_at = ? WHERE market = ?",
                    (json.dumps(asdict(cfg), sort_keys=True), tf_update, now_iso, mkt),
                )
            else:
                conn.execute(
                    "UPDATE live_paper_accounts SET config_json = ?, updated_at = ? WHERE market = ?",
                    (json.dumps(asdict(cfg), sort_keys=True), now_iso, mkt),
                )
            audit_payload: dict[str, Any] = {"config": asdict(cfg)}
            if strat_update:
                audit_payload["strategy"] = strat_update
            if tf_update:
                audit_payload["timeframe"] = tf_update
            self._append_audit_conn(conn, mkt, "PAPER_CONFIG_UPDATED", audit_payload)
        return asdict(cfg)

    def _session_key(self, market: str) -> str:
        return f"LIVE_MARKET_PAPER:{market.upper()}"

    def _resolve_kronos_requested(
        self,
        market: str,
        strategy: str,
        explicit_kronos_enabled: bool | None,
    ) -> bool:
        strat = (strategy or "manual_only").strip().lower()
        ens_kronos = False
        if strat == "parallel_ensemble" and self.ensemble_service is not None:
            alloc = (getattr(self.ensemble_service, "allocators", None) or {}).get(market.upper())
            if alloc is not None:
                ens_kronos = bool(getattr(alloc.config, "kronos_enabled", False))
        if self.kronos_provider is not None and hasattr(self.kronos_provider, "is_configured_for_paper_strategy"):
            return bool(
                self.kronos_provider.is_configured_for_paper_strategy(
                    strat,
                    explicit_kronos_enabled=explicit_kronos_enabled,
                    ensemble_kronos_enabled=ens_kronos,
                )
            )
        if explicit_kronos_enabled is not None:
            return bool(explicit_kronos_enabled)
        return strat in {"kronos_ensemble", "kronos_informed"} or ens_kronos

    def start_session(
        self,
        market: str,
        *,
        strategy: str = "manual_only",
        timeframe: str = "5m",
        symbols: list[str] | None = None,
        kronos_enabled: bool | None = None,
        kronos_failure_policy: str | None = None,
        analyze_immediately: bool = False,
        execute_immediately: bool = False,
    ) -> dict[str, Any]:
        mkt = self._validate_market(market)
        now = _utc_now(self._clock)
        sess = self._market_session_info(mkt, now)
        state = "RUNNING" if sess["is_open"] else "MARKET_CLOSED_WAITING"
        reason = (
            None
            if sess["is_open"]
            else f"{mkt} regular session is currently {sess['state']}; waiting for live session open."
        )
        resolved_syms = self._effective_symbols_for_market(mkt, symbols)
        strat_clean = strategy.strip() or "manual_only"
        tf_clean = timeframe.strip() or "5m"
        k_requested = self._resolve_kronos_requested(mkt, strat_clean, kronos_enabled)
        eff_policy = (
            str(kronos_failure_policy).strip()
            if kronos_failure_policy
            else (
                str(self.kronos_provider.config.failure_policy)
                if self.kronos_provider is not None and hasattr(self.kronos_provider, "config")
                else "FAIL_CLOSED_NO_ENTRY"
            )
        )
        run_id = f"run-{mkt.lower()}-{uuid.uuid4().hex[:12]}"
        today_str = str((sess.get("detail") or {}).get("trading_day") or now.date().isoformat())
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM live_paper_accounts WHERE market = ?", (mkt,)).fetchone()
            if row and str(row["session_state"]) == "EMERGENCY_STOPPED":
                raise RuntimeError("Emergency Stop is engaged; reset Emergency Stop before starting a session")
            pos_val = sum(
                float(r["qty"]) * float(r["last_price"])
                for r in conn.execute("SELECT qty, last_price FROM live_paper_positions WHERE market = ?", (mkt,))
            )
            curr_eq = round(float(row["cash"]) + pos_val, 4)
            curr_realized = round(float(row["realized_pnl"]), 4)
            day_eq = curr_eq if str(row["trading_day"]) != today_str else float(row["day_start_equity"])
            conn.execute(
                """
                UPDATE live_paper_accounts
                SET session_state = ?, state_reason = ?, strategy = ?, timeframe = ?,
                    symbols_json = ?, session_run_id = ?, session_start_equity = ?,
                    session_start_realized_pnl = ?, day_start_equity = ?, trading_day = ?,
                    kronos_enabled = ?, kronos_failure_policy = ?,
                    started_at = ?, updated_at = ?
                WHERE market = ?
                """,
                (
                    state,
                    reason,
                    strat_clean,
                    tf_clean,
                    json.dumps(resolved_syms),
                    run_id,
                    curr_eq,
                    curr_realized,
                    day_eq,
                    today_str,
                    1 if k_requested else 0,
                    eff_policy,
                    now.isoformat(),
                    now.isoformat(),
                    mkt,
                ),
            )
            self._append_audit_conn(
                conn,
                mkt,
                "PAPER_SESSION_STARTED",
                {
                    "session_run_id": run_id,
                    "state": state,
                    "strategy": strat_clean,
                    "timeframe": tf_clean,
                    "symbols": resolved_syms,
                    "kronos_configured": k_requested,
                    "kronos_failure_policy": eff_policy,
                    "session_start_equity": curr_eq,
                    "persisted_historical_realized_pnl": curr_realized,
                },
            )
        if self.kronos_provider is not None and hasattr(self.kronos_provider, "start_for_paper_session"):
            self.kronos_provider.start_for_paper_session(
                self._session_key(mkt),
                session_id=run_id,
                market=mkt,
                strategy=strat_clean,
                kronos_requested=k_requested,
                failure_policy=eff_policy,
            )
        initial_analysis: dict[str, Any] | None = None
        if analyze_immediately:
            initial_analysis = self.analyze_market(mkt, trigger="session_start")
        has_succeeded_data = initial_analysis is None or int((initial_analysis.get("counts") or {}).get("succeeded") or 0) > 0
        if execute_immediately and sess["is_open"] and strat_clean.lower() != "manual_only" and has_succeeded_data:
            tick_out = self.sync_live_tick(mkt)
            if initial_analysis is not None:
                tick_out["initial_analysis"] = tick_out.get("last_analysis") or initial_analysis
            return tick_out
        snap = self.market_snapshot(mkt)
        if initial_analysis is not None:
            snap["initial_analysis"] = initial_analysis
        return snap

    def transition_session(
        self,
        market: str,
        action: str,
        *,
        reason: str = "",
        analyze_immediately: bool = False,
        execute_immediately: bool = False,
    ) -> dict[str, Any]:
        mkt = self._validate_market(market)
        act = action.strip().lower()
        if act == "start":
            with self._lock, self._connect() as conn:
                r_cfg = conn.execute(
                    "SELECT strategy, timeframe, symbols_json, kronos_enabled, kronos_failure_policy FROM live_paper_accounts WHERE market = ?",
                    (mkt,),
                ).fetchone()
                strat = str(r_cfg["strategy"]) if r_cfg and r_cfg["strategy"] else "manual_only"
                tf = str(r_cfg["timeframe"]) if r_cfg and r_cfg["timeframe"] else "5m"
                saved_syms = json.loads(r_cfg["symbols_json"] or "[]") if r_cfg else None
                k_en = bool(r_cfg["kronos_enabled"]) if (r_cfg and r_cfg["kronos_enabled"] is not None) else None
                k_pol = str(r_cfg["kronos_failure_policy"]) if (r_cfg and r_cfg["kronos_failure_policy"]) else None
            return self.start_session(
                mkt,
                strategy=strat,
                timeframe=tf,
                symbols=saved_syms or None,
                kronos_enabled=k_en,
                kronos_failure_policy=k_pol,
                analyze_immediately=analyze_immediately,
                execute_immediately=execute_immediately,
            )
        now = _utc_now(self._clock)
        prior_run_id = ""
        active_run_id = ""
        strat_for_k = "manual_only"
        k_req_resume = False
        k_pol_resume = "FAIL_CLOSED_NO_ENTRY"
        sess_for_resume: dict[str, Any] | None = None
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM live_paper_accounts WHERE market = ?", (mkt,)).fetchone()
            curr_state = str(row["session_state"]) if row else "STOPPED"
            prior_run_id = str(row["session_run_id"] or "") if row else ""
            strat_for_k = str(row["strategy"] or "manual_only") if row else "manual_only"
            saved_syms_resume = json.loads(row["symbols_json"] or "[]") if row else []
            k_req_resume = bool(row["kronos_enabled"]) if (row and row["kronos_enabled"] is not None) else self._resolve_kronos_requested(mkt, strat_for_k, None)
            k_pol_resume = str(row["kronos_failure_policy"] or "FAIL_CLOSED_NO_ENTRY") if row else "FAIL_CLOSED_NO_ENTRY"
            if act == "pause":
                next_state = "PAUSED"
                next_reason = reason or "Paused by operator"
                active_run_id = prior_run_id
            elif act == "resume":
                if curr_state == "EMERGENCY_STOPPED":
                    raise RuntimeError("Reset Emergency Stop explicitly before resuming")
                sess_for_resume = self._market_session_info(mkt, now)
                next_state = "RUNNING" if sess_for_resume["is_open"] else "MARKET_CLOSED_WAITING"
                next_reason = None if sess_for_resume["is_open"] else f"{mkt} session is {sess_for_resume['state']}"
                active_run_id = prior_run_id or f"run-{mkt.lower()}-{uuid.uuid4().hex[:12]}"
            elif act == "stop":
                next_state = "STOPPED"
                next_reason = reason or "OPERATOR_STOP"
            else:
                raise ValueError(f"Unsupported session transition {action!r}")
            if next_state == "STOPPED":
                conn.execute(
                    "UPDATE live_paper_accounts SET session_state = ?, state_reason = ?, session_run_id = NULL, updated_at = ? WHERE market = ?",
                    (next_state, next_reason, now.isoformat(), mkt),
                )
            elif act == "resume" and not saved_syms_resume:
                eff_syms_resume = self._effective_symbols_for_market(mkt)
                conn.execute(
                    "UPDATE live_paper_accounts SET session_state = ?, state_reason = ?, session_run_id = ?, symbols_json = ?, updated_at = ? WHERE market = ?",
                    (next_state, next_reason, active_run_id or None, json.dumps(eff_syms_resume), now.isoformat(), mkt),
                )
            else:
                conn.execute(
                    "UPDATE live_paper_accounts SET session_state = ?, state_reason = ?, session_run_id = ?, updated_at = ? WHERE market = ?",
                    (next_state, next_reason, active_run_id or None, now.isoformat(), mkt),
                )
            self._append_audit_conn(
                conn,
                mkt,
                f"PAPER_SESSION_{next_state}",
                {
                    "session_run_id": active_run_id or prior_run_id,
                    "strategy": strat_for_k,
                    "previous_state": curr_state,
                    "reason": next_reason,
                },
            )
        if self.kronos_provider is not None:
            if act == "stop" and hasattr(self.kronos_provider, "stop_for_paper_session"):
                self.kronos_provider.stop_for_paper_session(
                    self._session_key(mkt),
                    session_id=prior_run_id,
                    reason=next_reason or "operator stop",
                )
            elif act == "pause" and hasattr(self.kronos_provider, "pause_for_paper_session"):
                self.kronos_provider.pause_for_paper_session(
                    self._session_key(mkt),
                    session_id=prior_run_id,
                    reason=next_reason or "operator pause",
                )
            elif act == "resume" and hasattr(self.kronos_provider, "start_for_paper_session"):
                self.kronos_provider.start_for_paper_session(
                    self._session_key(mkt),
                    session_id=active_run_id,
                    market=mkt,
                    strategy=strat_for_k,
                    kronos_requested=k_req_resume,
                    failure_policy=k_pol_resume,
                )
        initial_analysis: dict[str, Any] | None = None
        if act == "resume" and analyze_immediately:
            initial_analysis = self.analyze_market(mkt, trigger="session_resume")
        has_succeeded_resume = initial_analysis is None or int((initial_analysis.get("counts") or {}).get("succeeded") or 0) > 0
        if (
            act == "resume"
            and execute_immediately
            and sess_for_resume is not None
            and sess_for_resume["is_open"]
            and strat_for_k.lower() != "manual_only"
            and has_succeeded_resume
        ):
            tick_out = self.sync_live_tick(mkt)
            if initial_analysis is not None:
                tick_out["initial_analysis"] = tick_out.get("last_analysis") or initial_analysis
            return tick_out
        snap = self.market_snapshot(mkt)
        if initial_analysis is not None:
            snap["initial_analysis"] = initial_analysis
        return snap

    def _normalize_raw_bars(self, sym: str, raw_bars_list: Any) -> tuple[list[Candle], str | None]:
        """Convert Candle or dict list into validated monotonic Candle objects."""
        if not raw_bars_list:
            return [], "EMPTY_BARS"
        bars: list[Candle] = []
        for raw_b in raw_bars_list:
            if isinstance(raw_b, Candle):
                bars.append(raw_b)
            elif isinstance(raw_b, dict):
                try:
                    c_px = float(raw_b["close"])
                    bars.append(
                        Candle(
                            symbol=str(raw_b.get("symbol") or sym).replace("|", "_"),
                            timestamp=parse_timestamp(str(raw_b["timestamp"])),
                            open=float(raw_b.get("open", c_px)),
                            high=float(raw_b.get("high", c_px)),
                            low=float(raw_b.get("low", c_px)),
                            close=c_px,
                            volume=float(raw_b.get("volume", 0.0)),
                        )
                    )
                except (KeyError, ValueError, TypeError):
                    return [], "MALFORMED_BAR_OHLCV"
            else:
                return [], "MALFORMED_BAR_OHLCV"
        prev_bts: datetime | None = None
        for b in bars:
            if (
                not all(math.isfinite(v) and v > 0 for v in (b.open, b.high, b.low, b.close))
                or not math.isfinite(b.volume)
                or b.volume < 0
                or b.low > min(b.open, b.close)
                or b.high < max(b.open, b.close)
            ):
                return [], "MALFORMED_BAR_OHLCV"
            if prev_bts is not None and b.timestamp <= prev_bts:
                return [], "OUT_OF_ORDER_OR_DUPLICATE_BAR"
            prev_bts = b.timestamp
        return bars, None

    def analyze_market(
        self,
        market: str,
        *,
        trigger: str = "manual",
        symbols: list[str] | None = None,
        quotes_by_symbol: dict[str, dict[str, Any]] | None = None,
        bars_by_symbol: dict[str, list[Candle]] | None = None,
    ) -> dict[str, Any]:
        """Read-only per-symbol analysis across all enabled strategies for a market.

        Works during both OPEN and CLOSED exchange sessions when historical bar data
        is available, clearly reporting succeeded, missing-data, stale-data, and failed
        symbols while placing ZERO simulated or broker orders.
        """
        mkt = self._validate_market(market)
        t0 = time.perf_counter()
        with self._market_cycle_locks[mkt]:
            now = _utc_now(self._clock)
            sess = self._market_session_info(mkt, now)
            with self._lock, self._connect() as conn:
                acct = conn.execute("SELECT * FROM live_paper_accounts WHERE market = ?", (mkt,)).fetchone()
                curr_state = str(acct["session_state"]) if acct else "STOPPED"
                strategy = str(acct["strategy"] or "manual_only") if acct else "manual_only"
                timeframe = str(acct["timeframe"] or "5m") if acct else "5m"
                active_run_id = str(acct["session_run_id"] or "") if (acct and "session_run_id" in acct.keys()) else ""
                pos_syms = [
                    str(r["symbol"])
                    for r in conn.execute("SELECT symbol FROM live_paper_positions WHERE market = ?", (mkt,))
                ]

            eff_syms = self._effective_symbols_for_market(mkt, symbols)
            for ps in pos_syms:
                if ps not in eff_syms and not any(ps == x.split("|")[-1] for x in eff_syms):
                    eff_syms.append(ps)

            bar_fetcher = self.nse_bars_provider if mkt == "NSE" else self.nasdaq_bars_provider
            quote_fetcher = self.nse_quote_provider if mkt == "NSE" else self.nasdaq_quote_provider

            def _probe_symbol(raw_sym: str, *, raise_rate_limit: bool = False) -> dict[str, Any]:
                try:
                    inst = self._resolve_instrument(mkt, raw_sym)
                except Exception as exc:
                    return {
                        "symbol": str(raw_sym).upper(),
                        "instrument_key": str(raw_sym),
                        "status": "FAILED",
                        "code": "INSTRUMENT_RESOLUTION_ERROR",
                        "reason": str(redact_sensitive_data(str(exc)))[:200],
                        "bars": [],
                        "quote": None,
                    }
                canon = str(inst.get("trading_symbol") or inst.get("symbol") or raw_sym).upper()
                ikey = str(inst.get("instrument_key") or canon)
                q_obj: dict[str, Any] | None = None
                q_status = "UNAVAILABLE"
                q_reason = ""
                q_age: float | None = None
                if quotes_by_symbol and (canon in quotes_by_symbol or raw_sym in quotes_by_symbol or ikey in quotes_by_symbol):
                    q_obj = quotes_by_symbol.get(canon) or quotes_by_symbol.get(raw_sym) or quotes_by_symbol.get(ikey)
                elif quote_fetcher is not None:
                    try:
                        q_obj = self._fetch_live_quote(mkt, inst)
                    except Exception as exc:
                        ec, ed = self._classify_fetch_exception(mkt, exc)
                        # A throttle must reach the provider gate so it can back
                        # the whole market off; swallowing it here would make
                        # every worker retry into the same 429.
                        if raise_rate_limit and ec == "RATE_LIMITED":
                            raise
                        q_status = ec
                        q_reason = str(redact_sensitive_data(ed))[:180]
                if q_obj is not None:
                    q_val = self._validate_quote_for_paper(
                        mkt, inst, q_obj, now, sess_info=sess, record_monotonic_watermark=False
                    )
                    q_age = q_val.get("quote_age_seconds")
                    if q_val["valid_for_fill"]:
                        q_status = "FRESH"
                    elif not sess["is_open"] and q_val.get("reason_code") in {"MARKET_CLOSED", "STALE_QUOTE"}:
                        q_status = "CLOSED_LAST_QUOTE"
                        q_reason = str(q_val.get("reason_detail") or "")
                    else:
                        q_status = str(q_val.get("reason_code") or "INVALID_QUOTE")
                        q_reason = str(q_val.get("reason_detail") or "")

                raw_bars: Any = None
                b_err_code: str | None = None
                b_err_detail: str = ""
                if bars_by_symbol is not None and (canon in bars_by_symbol or raw_sym in bars_by_symbol or ikey in bars_by_symbol):
                    raw_bars = (
                        bars_by_symbol.get(canon)
                        if canon in bars_by_symbol
                        else (bars_by_symbol.get(raw_sym) if raw_sym in bars_by_symbol else bars_by_symbol.get(ikey))
                    )
                elif bar_fetcher is not None:
                    try:
                        raw_bars = bar_fetcher(raw_sym, timeframe)
                    except Exception as exc:
                        ec, ed = self._classify_fetch_exception(mkt, exc)
                        if raise_rate_limit and ec == "RATE_LIMITED":
                            raise
                        b_err_code = ec
                        b_err_detail = str(redact_sensitive_data(ed))[:200]
                else:
                    b_err_code = "NO_BARS_PROVIDER"
                    b_err_detail = f"Historical bar provider is not configured for {mkt}"

                if b_err_code is not None:
                    is_missing_cred = b_err_code in {"MISSING_CREDENTIALS", "NO_BARS_PROVIDER"} or "not configured" in b_err_detail.lower()
                    return {
                        "symbol": canon,
                        "instrument_key": ikey,
                        "status": "MISSING_DATA" if is_missing_cred else "FAILED",
                        "code": b_err_code,
                        "reason": b_err_detail,
                        "quote_status": q_status,
                        "quote_age_seconds": q_age,
                        "quote_note": q_reason,
                        "bars": [],
                        "quote": q_obj,
                    }

                norm_bars, norm_err = self._normalize_raw_bars(canon, raw_bars)
                if norm_err == "EMPTY_BARS":
                    return {
                        "symbol": canon,
                        "instrument_key": ikey,
                        "status": "MISSING_DATA",
                        "code": "EMPTY_BARS",
                        "reason": f"Provider returned 0 completed {timeframe} bars for {mkt}:{canon}",
                        "quote_status": q_status,
                        "quote_age_seconds": q_age,
                        "quote_note": q_reason,
                        "bars": [],
                        "quote": q_obj,
                    }
                if norm_err is not None:
                    return {
                        "symbol": canon,
                        "instrument_key": ikey,
                        "status": "FAILED",
                        "code": norm_err,
                        "reason": f"Invalid OHLCV bar payload ({norm_err}) for {mkt}:{canon}",
                        "quote_status": q_status,
                        "quote_age_seconds": q_age,
                        "quote_note": q_reason,
                        "bars": [],
                        "quote": q_obj,
                    }
                if len(norm_bars) < 21:
                    return {
                        "symbol": canon,
                        "instrument_key": ikey,
                        "status": "MISSING_DATA",
                        "code": "INSUFFICIENT_BARS",
                        "reason": f"Only {len(norm_bars)} completed {timeframe} bar(s) available (need >= 21)",
                        "bar_count": len(norm_bars),
                        "latest_bar_timestamp": norm_bars[-1].timestamp.isoformat() if norm_bars else None,
                        "quote_status": q_status,
                        "quote_age_seconds": q_age,
                        "quote_note": q_reason,
                        "bars": norm_bars,
                        "quote": q_obj,
                    }

                sym_status = "SUCCEEDED"
                if sess["is_open"] and q_status in {"STALE_QUOTE", "DELAYED_FEED_BLOCKED", "OUT_OF_ORDER_QUOTE"}:
                    sym_status = "STALE_DATA"

                return {
                    "symbol": canon,
                    "instrument_key": ikey,
                    "status": sym_status,
                    "code": "OK_OPEN_SESSION" if (sess["is_open"] and sym_status == "SUCCEEDED") else ("OK_CLOSED_SESSION_ANALYZED" if not sess["is_open"] else q_status),
                    "reason": (
                        f"Analyzed {len(norm_bars)} completed {timeframe} bars"
                        + ("" if sess["is_open"] else f" ({mkt} {sess['state']} — simulated orders gated until open session)")
                        + (f"; quote {q_reason}" if (sym_status == "STALE_DATA" and q_reason) else "")
                    ),
                    "bar_count": len(norm_bars),
                    "latest_bar_timestamp": norm_bars[-1].timestamp.isoformat(),
                    "last_close": round(float(norm_bars[-1].close), 4),
                    "quote_status": q_status,
                    "quote_age_seconds": q_age,
                    "quote_note": q_reason,
                    "bars": norm_bars,
                    "quote": q_obj,
                }

            probed_rows: list[dict[str, Any]] = []
            gate = self.fetch_gates[mkt]
            if len(eff_syms) > 1 and bars_by_symbol is None and quotes_by_symbol is None:
                # Bounded + paced fan-out: one slow or throttled symbol must not
                # serialise the whole watchlist, and one 429 must not be retried
                # by every worker at once.
                outcomes = gate.map(
                    eff_syms, lambda raw_sym: _probe_symbol(raw_sym, raise_rate_limit=True)
                )
                for raw_sym in eff_syms:
                    outcome = outcomes.get(raw_sym)
                    if outcome is None:
                        continue
                    if outcome.ok:
                        probed_rows.append(outcome.value)
                    else:
                        probed_rows.append(
                            {
                                "symbol": str(raw_sym).split("|")[-1].upper(),
                                "instrument_key": str(raw_sym),
                                "status": "FAILED",
                                "code": outcome.error_code,
                                "reason": outcome.error_detail,
                                "bars": [],
                                "quote": None,
                            }
                        )
            else:
                probed_rows = [_probe_symbol(s) for s in eff_syms]

            valid_bars_map: dict[str, list[Candle]] = {
                r["symbol"]: r["bars"] for r in probed_rows if len(r.get("bars") or []) >= 21
            }
            valid_quotes_map: dict[str, dict[str, Any]] = {
                r["symbol"]: r["quote"] for r in probed_rows if isinstance(r.get("quote"), dict)
            }

            ens_by_sym: dict[str, dict[str, Any]] = {}
            enabled_variants_count = 0
            if self.ensemble_service is not None and valid_bars_map:
                try:
                    ens_preview = self.ensemble_service.evaluate_watchlist_cycle(
                        mkt,
                        symbols=sorted(valid_bars_map.keys()),
                        timeframe=timeframe,
                        bars_override=valid_bars_map,
                        quotes_override=valid_quotes_map,
                        session_open_override=bool(sess["is_open"]),
                        execute_in_paper=False,
                        persist_decisions=False,
                        invoke_kronos=False,
                        now_utc=now,
                    )
                    enabled_variants_count = int(ens_preview.get("enabled_variants_count") or 0)
                    for d_item in ens_preview.get("decisions") or []:
                        ens_by_sym[str(d_item.get("symbol") or "").upper()] = d_item
                except Exception as exc:
                    _ = exc
            elif self.ensemble_service is not None:
                try:
                    enabled_variants_count = len(self.ensemble_service.registry.list_variants(enabled_only=True))
                except Exception as exc:
                    _ = exc

            succeeded_syms: list[str] = []
            missing_syms: list[str] = []
            stale_syms: list[str] = []
            failed_syms: list[str] = []
            symbol_reports: list[dict[str, Any]] = []

            for r in probed_rows:
                sym = r["symbol"]
                st = r["status"]
                if st == "SUCCEEDED":
                    succeeded_syms.append(sym)
                elif st == "MISSING_DATA":
                    missing_syms.append(sym)
                elif st == "STALE_DATA":
                    stale_syms.append(sym)
                else:
                    failed_syms.append(sym)

                ens_dec = ens_by_sym.get(sym)
                rule_sig = None
                if r.get("bars") and len(r["bars"]) >= 21 and strategy.lower() not in {"manual_only", "parallel_ensemble"}:
                    rule_sig = evaluate_bar_strategy_signal(strategy, r["bars"])

                agg = (ens_dec or {}).get("aggregation") or {}
                cal = (ens_dec or {}).get("calibration") or {}
                symbol_reports.append(
                    {
                        "symbol": sym,
                        "instrument_key": r["instrument_key"],
                        "status": st,
                        "code": r["code"],
                        "reason": r["reason"],
                        "bar_count": r.get("bar_count", 0),
                        "latest_bar_timestamp": r.get("latest_bar_timestamp"),
                        "last_close": r.get("last_close"),
                        "quote_status": r.get("quote_status", "UNAVAILABLE"),
                        "quote_age_seconds": r.get("quote_age_seconds"),
                        "trade_eligible_now": bool(
                            sess["is_open"]
                            and curr_state == "RUNNING"
                            and strategy.lower() != "manual_only"
                            and st == "SUCCEEDED"
                        ),
                        "ensemble_raw_intent": (ens_dec or {}).get("raw_intent"),
                        "ensemble_final_action": (ens_dec or {}).get("final_action"),
                        "ensemble_consensus": agg.get("post_kronos_consensus"),
                        "active_weighted_variants": agg.get("active_weighted_variants"),
                        "calibrated_confidence": cal.get("calibrated_confidence"),
                        "no_trade_reasons": (ens_dec or {}).get("no_trade_reasons") or [],
                        "rule_signal": (rule_sig or {}).get("action") if rule_sig else None,
                    }
                )

            wall_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            report = {
                "market": mkt,
                "currency": self.configs[mkt].currency,
                "trigger": trigger,
                "analyzed_at": now.isoformat(),
                "wall_ms": wall_ms,
                "session_run_id": active_run_id or None,
                "session_state": curr_state,
                "exchange_session": sess,
                "strategy": strategy,
                "timeframe": timeframe,
                "enabled_strategy_variants": enabled_variants_count,
                "trade_execution_permitted_now": bool(
                    sess["is_open"] and curr_state == "RUNNING" and strategy.lower() != "manual_only"
                ),
                "counts": {
                    "selected_symbols": len(eff_syms),
                    "succeeded": len(succeeded_syms),
                    "missing_data": len(missing_syms),
                    "stale_data": len(stale_syms),
                    "failed": len(failed_syms),
                },
                "succeeded_symbols": succeeded_syms,
                "missing_data_symbols": missing_syms,
                "stale_data_symbols": stale_syms,
                "failed_symbols": failed_syms,
                "symbols": symbol_reports,
                "simulated_only": True,
                "broker_order_api_calls": 0,
                "provider_fetch": gate.stats(),
            }

            with self._lock, self._connect() as conn:
                conn.execute(
                    "UPDATE live_paper_accounts SET last_analysis_json = ?, updated_at = ? WHERE market = ?",
                    (json.dumps(report, sort_keys=True), now.isoformat(), mkt),
                )
                self._append_audit_conn(
                    conn,
                    mkt,
                    "PAPER_MARKET_ANALYSIS_REPORT",
                    {
                        "session_run_id": active_run_id,
                        "trigger": trigger,
                        "strategy": strategy,
                        "timeframe": timeframe,
                        "exchange_session_state": sess.get("state"),
                        "exchange_open": bool(sess.get("is_open")),
                        "counts": report["counts"],
                        "succeeded_symbols": succeeded_syms,
                        "missing_data_symbols": missing_syms,
                        "stale_data_symbols": stale_syms,
                        "failed_symbols": failed_syms,
                        "wall_ms": wall_ms,
                    },
                )
                for sr in symbol_reports:
                    if sr["status"] in {"MISSING_DATA", "STALE_DATA", "FAILED"}:
                        self._append_audit_conn(
                            conn,
                            mkt,
                            "PAPER_SYMBOL_SKIPPED",
                            {
                                "session_run_id": active_run_id,
                                "symbol": sr["symbol"],
                                "instrument_key": sr["instrument_key"],
                                "strategy": strategy,
                                "timeframe": timeframe,
                                "status": sr["status"],
                                "code": sr["code"],
                                "reason": sr["reason"],
                            },
                        )
            return report

    def reset_paper_account(self, market: str, *, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            raise ValueError("Explicit confirmation is required to reset a local paper account")
        mkt = self._validate_market(market)
        cfg = self.configs[mkt]
        now = _utc_now(self._clock)
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM live_paper_positions WHERE market = ?", (mkt,))
            conn.execute("DELETE FROM live_paper_orders WHERE market = ?", (mkt,))
            conn.execute("DELETE FROM live_paper_fills WHERE market = ?", (mkt,))
            conn.execute("DELETE FROM live_paper_decisions WHERE market = ?", (mkt,))
            conn.execute(
                """
                UPDATE live_paper_accounts
                SET cash = ?, realized_pnl = 0.0, day_start_equity = ?, trading_day = ?,
                    session_state = 'STOPPED', state_reason = 'Account reset by operator',
                    session_run_id = NULL, session_start_equity = ?, session_start_realized_pnl = 0.0,
                    started_at = NULL, updated_at = ?
                WHERE market = ?
                """,
                (cfg.starting_cash, cfg.starting_cash, now.date().isoformat(), cfg.starting_cash, now.isoformat(), mkt),
            )
            self._append_audit_conn(conn, mkt, "PAPER_ACCOUNT_RESET", {"starting_cash": cfg.starting_cash})
        if self.kronos_provider is not None and hasattr(self.kronos_provider, "stop_for_paper_session"):
            self.kronos_provider.stop_for_paper_session(self._session_key(mkt), reason="paper account reset")
        return self.market_snapshot(mkt)

    # -- Order Preview & Simulated Execution -----------------------------------

    def preview_order(
        self,
        *,
        market: str,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "MARKET",
        product: str = "D",
        time_in_force: str = "DAY",
        limit_price: float | None = None,
        stop_price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        quote_override: dict[str, Any] | None = None,
        allow_off_hours_working: bool = False,
    ) -> dict[str, Any]:
        mkt = self._validate_market(market)
        cfg = self.configs[mkt]
        now = _utc_now(self._clock)
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, detail: str) -> None:
            checks.append({"check": name, "ok": bool(ok), "detail": detail})

        side_u = (side or "").strip().upper()
        otype_u = (order_type or "MARKET").strip().upper()
        tif_u = (time_in_force or "DAY").strip().upper()
        prod_u = (product or ("D" if mkt == "NSE" else "CASH")).strip().upper()

        check("side", side_u in {"BUY", "SELL"}, f"side={side_u}")
        check("order_type", otype_u in {"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}, f"order_type={otype_u}")
        check("time_in_force", tif_u in {"DAY", "GTC", "IOC"}, f"tif={tif_u}")
        check("risk_limits_configured", cfg.configured(), "paper risk limits configured")

        try:
            inst = self._resolve_instrument(mkt, symbol)
            tradable = bool(inst.get("tradable", True))
            check("instrument_resolved", tradable, f"{inst.get('instrument_key')} tradable={tradable}")
        except ValueError as exc:
            check("instrument_resolved", False, str(exc))
            return {
                "ok": False,
                "simulated": True,
                "execution_mode": "LIVE_MARKET_PAPER",
                "market": mkt,
                "currency": cfg.currency,
                "symbol": symbol,
                "checks": checks,
                "rejection_reasons": [c["detail"] for c in checks if not c["ok"]],
            }

        sym_clean = str(inst.get("trading_symbol") or inst.get("symbol"))
        ikey = str(inst.get("instrument_key") or f"{mkt}|{sym_clean}")
        tick_size = float(inst.get("tick_size") or (0.05 if mkt == "NSE" else 0.01))
        lot_size = int(inst.get("quantity_step") or inst.get("lot_size") or 1)
        freeze_qty = int(inst.get("freeze_quantity") or 0)
        fractionable = bool(inst.get("fractionable", False)) if mkt == "NASDAQ" else False

        qty_ok = isinstance(qty, (int, float)) and math.isfinite(qty) and qty > 0
        if qty_ok:
            is_int = abs(qty - round(qty)) < 1e-9
            if not is_int and not fractionable:
                check("lot_and_fractional", False, f"{sym_clean} requires whole-share multiples of lot_size={lot_size}")
            elif is_int and lot_size > 1 and (int(round(qty)) % lot_size != 0):
                check("lot_and_fractional", False, f"quantity {qty} must be a multiple of lot_size={lot_size}")
            elif freeze_qty > 0 and qty > freeze_qty:
                check("lot_and_fractional", False, f"quantity {qty} exceeds NSE freeze_quantity={freeze_qty}")
            else:
                check("lot_and_fractional", True, f"qty={qty} valid (lot_size={lot_size})")
        else:
            check("lot_and_fractional", False, "quantity must be > 0")

        mis_ok = bool(inst.get("intraday_allowed", inst.get("mis_intraday_allowed", True)))
        if mkt == "NSE" and prod_u == "I" and not mis_ok:
            check(
                "settlement_series",
                False,
                f"{sym_clean} ({inst.get('instrument_type')}) is delivery-only on NSE; switch product from MIS (I) to Delivery (D)",
            )
        else:
            check("settlement_series", True, f"product={prod_u}")

        if otype_u in {"LIMIT", "STOP_LIMIT"}:
            lp_ok = limit_price is not None and _valid_tick(float(limit_price), tick_size, market=mkt)
            check("limit_price", lp_ok, "valid tick-aligned limit_price required" if not lp_ok else "ok")
        if otype_u in {"STOP", "STOP_LIMIT"}:
            sp_ok = stop_price is not None and _valid_tick(float(stop_price), tick_size, market=mkt)
            check("stop_price", sp_ok, "valid tick-aligned stop_price required" if not sp_ok else "ok")
        if stop_loss is not None:
            check("stop_loss_tick", _valid_tick(float(stop_loss), tick_size, market=mkt), "protective stop_loss tick check")
        if take_profit is not None:
            check("take_profit_tick", _valid_tick(float(take_profit), tick_size, market=mkt), "protective take_profit tick check")

        # Live read-only quote verification (never sample/replay disguised as live)
        quote_data: dict[str, Any] | None = None
        quote_age = None
        sess_info = self._market_session_info(mkt, now)
        quote_val: dict[str, Any] = {
            "valid_for_fill": False,
            "reason_code": "UNCHECKED",
            "reason_detail": "",
            "quote_age_seconds": None,
            "source_timestamp": None,
            "local_received_at": now.isoformat(),
            "feed": None,
            "feed_entitlement_label": None,
            "is_delayed": False,
        }
        try:
            quote_data = self._fetch_live_quote(mkt, inst, quote_override=quote_override)
            quote_val = self._validate_quote_for_paper(
                mkt,
                inst,
                quote_data,
                now,
                sess_info=sess_info,
                record_monotonic_watermark=False,
            )
            quote_age = quote_val["quote_age_seconds"]
            hard_integrity_failure = quote_val["reason_code"] in {
                "MALFORMED_QUOTE",
                "OUT_OF_ORDER_QUOTE",
                "DELAYED_FEED_BLOCKED",
            }
            if otype_u == "LIMIT" and allow_off_hours_working and not hard_integrity_failure and limit_price is not None:
                check(
                    "live_quote_available",
                    True,
                    f"off-hours working LIMIT staged ({quote_val['reason_code']}: {quote_val['reason_detail']})",
                )
            else:
                check(
                    "live_quote_fresh",
                    bool(quote_val["valid_for_fill"]),
                    quote_val["reason_detail"]
                    if quote_val["valid_for_fill"]
                    else f"[{quote_val['reason_code']}] {quote_val['reason_detail']}",
                )
        except Exception as exc:
            err_code, err_detail = self._classify_fetch_exception(mkt, exc)
            quote_val["reason_code"] = err_code
            quote_val["reason_detail"] = err_detail
            q_last = float(limit_price or stop_price or 0.0)
            if (
                otype_u == "LIMIT"
                and allow_off_hours_working
                and q_last > 0
                and err_code in {"DISCONNECTED"}
                and not sess_info["is_open"]
            ):
                check("live_quote_available", True, "working limit staged against limit_price while feed offline off-hours")
            else:
                check("live_quote_fresh", False, f"[{err_code}] {err_detail}")

        ref_price = float(quote_data["last"]) if (quote_data and float(quote_data.get("last") or 0) > 0) else float(limit_price or stop_price or 0.0)
        half_spread = ref_price * (cfg.spread_bps / 20_000.0)
        slip = ref_price * (cfg.slippage_bps / 10_000.0)
        raw_exec = (ref_price + half_spread + slip) if side_u == "BUY" else max(tick_size, ref_price - half_spread - slip)
        if otype_u == "LIMIT" and limit_price is not None:
            if side_u == "BUY":
                est_price = min(float(limit_price), _round_to_tick(raw_exec, tick_size, market=mkt, side=side_u)) if ref_price <= float(limit_price) else float(limit_price)
            else:
                est_price = max(float(limit_price), _round_to_tick(raw_exec, tick_size, market=mkt, side=side_u)) if ref_price >= float(limit_price) else float(limit_price)
        else:
            est_price = _round_to_tick(raw_exec, tick_size, market=mkt, side=side_u) if ref_price > 0 else 0.0

        fees = estimate_market_fees(
            market=mkt,
            side=side_u,
            qty=qty if qty_ok else 0.0,
            fill_price=est_price,
            product=prod_u,
            brokerage_bps=cfg.brokerage_bps,
        )
        notional = round((qty if qty_ok else 0.0) * est_price, 2)

        with self._lock, self._connect() as conn:
            acct = conn.execute("SELECT * FROM live_paper_accounts WHERE market = ?", (mkt,)).fetchone()
            positions = {
                str(r["symbol"]): dict(r)
                for r in conn.execute("SELECT * FROM live_paper_positions WHERE market = ?", (mkt,))
            }
            today_orders = conn.execute(
                "SELECT COUNT(*) AS n FROM live_paper_orders WHERE market = ? AND order_date = ?",
                (mkt, now.date().isoformat()),
            ).fetchone()["n"]

        cash = float(acct["cash"])
        sess_state = str(acct["session_state"])
        check("emergency_stop_clear", sess_state != "EMERGENCY_STOPPED", f"session_state={sess_state}")

        pos = positions.get(sym_clean)
        pos_qty = float(pos["qty"]) if pos else 0.0
        is_reducing = (pos_qty > 0 and side_u == "SELL" and qty <= pos_qty + 1e-9)

        if not is_reducing:
            equity = cash + sum(float(p["qty"]) * float(p["last_price"]) for p in positions.values())
            day_pnl = equity - float(acct["day_start_equity"])
            check(
                "max_daily_loss",
                day_pnl > -abs(cfg.max_daily_loss),
                f"day P&L {day_pnl:.2f} {cfg.currency} vs -max_daily_loss {-abs(cfg.max_daily_loss):.2f}",
            )
            check(
                "max_order_value",
                notional <= cfg.max_order_value,
                f"order value {notional:.2f} {cfg.currency} vs cap {cfg.max_order_value:.2f}",
            )
            check(
                "max_daily_orders",
                today_orders < cfg.max_daily_orders,
                f"{today_orders} orders today vs cap {cfg.max_daily_orders}",
            )
            if side_u == "BUY":
                needed = notional + fees["total"]
                check("cash_buying_power", cash >= needed, f"cash {cash:.2f} vs required {needed:.2f} {cfg.currency}")
                sym_exp = abs(pos_qty * ref_price) + notional
                tot_exp = sum(abs(float(p["qty"]) * float(p["last_price"])) for p in positions.values()) + notional
                check("max_symbol_exposure", sym_exp <= cfg.max_symbol_exposure, f"symbol exposure {sym_exp:.2f} vs cap {cfg.max_symbol_exposure:.2f}")
                check("max_total_exposure", tot_exp <= cfg.max_total_exposure, f"total exposure {tot_exp:.2f} vs cap {cfg.max_total_exposure:.2f}")
                if pos is None:
                    check("max_open_positions", len(positions) < cfg.max_open_positions, f"{len(positions)} open vs cap {cfg.max_open_positions}")
            else:
                # Cash-equity short check: NSE delivery (D) forbids naked shorts
                if mkt == "NSE" and prod_u == "D" and qty > pos_qty + 1e-9:
                    check("short_selling_rule", False, f"NSE Delivery (CNC) cannot short {sym_clean} without holdings ({pos_qty} held)")

        ok = all(c["ok"] for c in checks)
        return {
            "ok": ok,
            "simulated": True,
            "real_order_api_used": False,
            "execution_mode": "LIVE_MARKET_PAPER",
            "market": mkt,
            "currency": cfg.currency,
            "symbol": sym_clean,
            "instrument_key": ikey,
            "exchange": str(inst.get("exchange") or mkt),
            "asset_category": str(inst.get("asset_category") or "CASH_EQUITY_OR_ETF"),
            "side": side_u,
            "qty": qty,
            "order_type": otype_u,
            "product": prod_u,
            "time_in_force": tif_u,
            "limit_price": limit_price,
            "stop_price": stop_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "reference_price": round(ref_price, 4) if ref_price else None,
            "estimated_fill_price": est_price if est_price else None,
            "estimated_notional": notional,
            "estimated_fees": fees,
            "provider": "upstox_market_data" if mkt == "NSE" else "alpaca_market_data",
            "feed": quote_val.get("feed"),
            "feed_entitlement_label": quote_val.get("feed_entitlement_label"),
            "quote_source": quote_data.get("source") if quote_data else None,
            "quote_timestamp": quote_val.get("source_timestamp") or (quote_data.get("timestamp") if quote_data else None),
            "source_timestamp": quote_val.get("source_timestamp") or (quote_data.get("timestamp") if quote_data else None),
            "local_received_at": quote_val.get("local_received_at"),
            "quote_age_seconds": quote_age,
            "quote_validation_code": quote_val.get("reason_code"),
            "market_session_status": sess_info["state"],
            "session": sess_info,
            "fill_type": "SIMULATED",
            "checks": checks,
            "rejection_reasons": [c["detail"] for c in checks if not c["ok"]],
            "checked_at": now.isoformat(),
        }

    def submit_order(
        self,
        *,
        client_order_id: str | None = None,
        market: str,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "MARKET",
        product: str = "D",
        time_in_force: str = "DAY",
        limit_price: float | None = None,
        stop_price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        quote_override: dict[str, Any] | None = None,
        allow_off_hours_working: bool = False,
        decision_id: str | None = None,
        strategy_source: str | None = None,
    ) -> dict[str, Any]:
        mkt = self._validate_market(market)
        cid = (client_order_id or f"lmp-{mkt.lower()}-{uuid.uuid4().hex[:14]}").strip()

        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM live_paper_orders WHERE client_order_id = ?", (cid,)
            ).fetchone()
            if existing is not None:
                return {
                    "ok": existing["status"] != "REJECTED",
                    "duplicate": True,
                    "simulated": True,
                    "idempotent_replay": True,
                    "real_order_api_used": False,
                    "execution_mode": "LIVE_MARKET_PAPER",
                    "order": dict(existing),
                }
            acct_row = conn.execute(
                "SELECT session_run_id, strategy FROM live_paper_accounts WHERE market = ?",
                (mkt,),
            ).fetchone()
            active_run_id = str(acct_row["session_run_id"] or "") if acct_row else ""
            active_strat = strategy_source or (str(acct_row["strategy"] or "") if acct_row else "")

        preview = self.preview_order(
            market=mkt,
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            product=product,
            time_in_force=time_in_force,
            limit_price=limit_price,
            stop_price=stop_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            quote_override=quote_override,
            allow_off_hours_working=allow_off_hours_working,
        )
        now = _utc_now(self._clock)
        now_iso = now.isoformat()
        order_id = f"ord-{uuid.uuid4().hex[:12]}"

        if not preview["ok"]:
            risk_gate_codes = {
                "max_order_value",
                "max_symbol_exposure",
                "max_total_exposure",
                "max_open_positions",
                "max_daily_loss",
                "max_daily_orders",
                "emergency_stop",
                "cash_balance",
            }
            violated_risk_checks = [
                c for c in (preview.get("checks") or [])
                if not c.get("ok") and str(c.get("check") or "").lower() in risk_gate_codes
            ]
            with self._lock, self._connect() as conn:
                self._append_audit_conn(
                    conn,
                    mkt,
                    "PAPER_ORDER_REJECTED",
                    {
                        "client_order_id": cid,
                        "order_id": order_id,
                        "decision_id": decision_id or "",
                        "session_run_id": active_run_id,
                        "strategy": active_strat,
                        "symbol": preview.get("symbol", symbol),
                        "side": preview.get("side", side),
                        "qty": qty,
                        "reasons": preview["rejection_reasons"],
                    },
                )
                if violated_risk_checks:
                    self._append_audit_conn(
                        conn,
                        mkt,
                        "PAPER_RISK_LIMIT_EVENT",
                        {
                            "client_order_id": cid,
                            "decision_id": decision_id or "",
                            "session_run_id": active_run_id,
                            "strategy": active_strat,
                            "symbol": preview.get("symbol", symbol),
                            "side": preview.get("side", side),
                            "qty": qty,
                            "violated_limits": [c.get("detail") for c in violated_risk_checks],
                            "checks": violated_risk_checks,
                        },
                    )
            return {
                "ok": False,
                "duplicate": False,
                "simulated": True,
                "real_order_api_used": False,
                "execution_mode": "LIVE_MARKET_PAPER",
                "client_order_id": cid,
                "preview": preview,
            }

        lookup_target = preview["instrument_key"] if mkt == "NSE" else preview["symbol"]
        inst = self._resolve_instrument(mkt, lookup_target)
        quote_data = self._fetch_live_quote(mkt, inst, quote_override=quote_override) if preview.get("quote_timestamp") else None
        sess_now = self._market_session_info(mkt, now)
        quote_fill_val = (
            self._validate_quote_for_paper(
                mkt,
                inst,
                quote_data,
                now,
                sess_info=sess_now,
                record_monotonic_watermark=True,
            )
            if quote_data is not None
            else {"valid_for_fill": False}
        )

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO live_paper_orders
                (client_order_id, order_id, market, currency, symbol, instrument_key,
                 side, order_type, product, time_in_force, qty, filled_qty, remaining_qty,
                 limit_price, stop_price, stop_loss, take_profit, avg_fill_price,
                 fees_total, status, reject_reason, quote_source, quote_timestamp,
                 order_date, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?, ?, NULL, 0.0,
                        'WORKING', NULL, ?, ?, ?, ?, ?)
                """,
                (
                    cid,
                    order_id,
                    mkt,
                    preview["currency"],
                    preview["symbol"],
                    preview["instrument_key"],
                    preview["side"],
                    preview["order_type"],
                    preview["product"],
                    preview["time_in_force"],
                    float(qty),
                    float(qty),
                    limit_price,
                    stop_price,
                    stop_loss,
                    take_profit,
                    preview.get("quote_source"),
                    preview.get("quote_timestamp"),
                    now.date().isoformat(),
                    now_iso,
                    now_iso,
                ),
            )
            self._append_audit_conn(
                conn,
                mkt,
                "PAPER_ORDER_ACCEPTED",
                {
                    "client_order_id": cid,
                    "order_id": order_id,
                    "decision_id": decision_id or "",
                    "session_run_id": active_run_id,
                    "strategy": active_strat,
                    "symbol": preview["symbol"],
                    "instrument_key": preview["instrument_key"],
                    "side": preview["side"],
                    "qty": qty,
                    "order_type": preview["order_type"],
                },
            )
            if quote_data is not None and bool(quote_fill_val.get("valid_for_fill")):
                self._try_fill_order_conn(
                    conn,
                    cid,
                    inst,
                    quote_data,
                    now,
                    decision_id=decision_id,
                    strategy_source=active_strat,
                )
            elif preview["time_in_force"] == "IOC":
                conn.execute(
                    "UPDATE live_paper_orders SET status = 'EXPIRED', updated_at = ? WHERE client_order_id = ?",
                    (now_iso, cid),
                )
            final_order = dict(
                conn.execute("SELECT * FROM live_paper_orders WHERE client_order_id = ?", (cid,)).fetchone()
            )

        return {
            "ok": True,
            "duplicate": False,
            "idempotent_replay": False,
            "simulated": True,
            "real_order_api_used": False,
            "execution_mode": "LIVE_MARKET_PAPER",
            "order": final_order,
            "preview": preview,
        }

    def _try_fill_order_conn(
        self,
        conn: sqlite3.Connection,
        client_order_id: str,
        inst: dict[str, Any],
        quote: dict[str, Any],
        now: datetime,
        *,
        decision_id: str | None = None,
        strategy_source: str | None = None,
    ) -> dict[str, Any] | None:
        order = conn.execute(
            "SELECT * FROM live_paper_orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        if order is None or str(order["status"]) not in OPEN_ORDER_STATUSES:
            return None

        mkt = str(order["market"])
        cfg = self.configs[mkt]
        side = str(order["side"])
        otype = str(order["order_type"])
        rem_qty = float(order["remaining_qty"])
        last = float(quote.get("last") or 0.0)
        bid = float(quote.get("bid") or last)
        ask = float(quote.get("ask") or last)
        if last <= 0:
            return None

        tick_size = float(inst.get("tick_size") or (0.05 if mkt == "NSE" else 0.01))
        slip = last * (cfg.slippage_bps / 10_000.0)
        half_spread = last * (cfg.spread_bps / 20_000.0)
        ref_side_px = (ask if ask > 0 else last + half_spread) if side == "BUY" else (bid if bid > 0 else max(tick_size, last - half_spread))

        executable = False
        candidate_px = ref_side_px + slip if side == "BUY" else max(tick_size, ref_side_px - slip)

        if otype == "MARKET":
            executable = True
        elif otype == "LIMIT":
            lim = float(order["limit_price"])
            if side == "BUY" and ref_side_px <= lim:
                executable = True
                candidate_px = min(lim, candidate_px)
            elif side == "SELL" and ref_side_px >= lim:
                executable = True
                candidate_px = max(lim, candidate_px)
        elif otype == "STOP":
            stp = float(order["stop_price"])
            if (side == "BUY" and last >= stp) or (side == "SELL" and last <= stp):
                executable = True
        elif otype == "STOP_LIMIT":
            stp = float(order["stop_price"])
            lim = float(order["limit_price"])
            if side == "BUY" and last >= stp and ref_side_px <= lim:
                executable = True
                candidate_px = min(lim, candidate_px)
            elif side == "SELL" and last <= stp and ref_side_px >= lim:
                executable = True
                candidate_px = max(lim, candidate_px)

        if not executable:
            if str(order["time_in_force"]) == "IOC":
                conn.execute(
                    "UPDATE live_paper_orders SET status = 'EXPIRED', updated_at = ? WHERE client_order_id = ?",
                    (now.isoformat(), client_order_id),
                )
            return None

        fill_px = _round_to_tick(candidate_px, tick_size, market=mkt, side=side)
        vol = float(quote.get("volume") or 0.0)
        lot = int(inst.get("quantity_step") or inst.get("lot_size") or 1)
        if vol > 0 and cfg.max_bar_participation > 0:
            max_liq_qty = vol * cfg.max_bar_participation
            if max_liq_qty < rem_qty:
                if bool(inst.get("fractionable", False)) and mkt == "NASDAQ":
                    fill_qty = round(max(0.01, max_liq_qty), 4)
                else:
                    lots = max(1, int(max_liq_qty // lot))
                    fill_qty = min(rem_qty, float(lots * lot))
            else:
                fill_qty = rem_qty
        else:
            fill_qty = rem_qty

        fees_info = estimate_market_fees(
            market=mkt,
            side=side,
            qty=fill_qty,
            fill_price=fill_px,
            product=str(order["product"]),
            brokerage_bps=cfg.brokerage_bps,
        )
        fee_total = float(fees_info["total"])

        acct = conn.execute("SELECT * FROM live_paper_accounts WHERE market = ?", (mkt,)).fetchone()
        cash = float(acct["cash"])
        realized_pnl = float(acct["realized_pnl"])
        sym = str(order["symbol"])
        pos = conn.execute(
            "SELECT * FROM live_paper_positions WHERE market = ? AND symbol = ?", (mkt, sym)
        ).fetchone()
        realized_delta = 0.0

        if side == "BUY":
            cost = fill_qty * fill_px + fee_total
            if cost > cash + 1e-6:
                conn.execute(
                    "UPDATE live_paper_orders SET status = 'REJECTED', reject_reason = ?, updated_at = ? WHERE client_order_id = ?",
                    ("Insufficient simulated cash at execution time", now.isoformat(), client_order_id),
                )
                return None
            cash -= cost
            if pos is None:
                conn.execute(
                    """
                    INSERT INTO live_paper_positions
                    (market, symbol, instrument_key, currency, qty, avg_entry_price, last_price,
                     last_quote_ts, last_quote_source, product, stop_loss, take_profit, opened_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mkt,
                        sym,
                        str(order["instrument_key"]),
                        str(order["currency"]),
                        fill_qty,
                        fill_px,
                        last,
                        str(quote["timestamp"]),
                        str(quote.get("source") or "live-readonly"),
                        str(order["product"]),
                        order["stop_loss"],
                        order["take_profit"],
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            else:
                old_qty = float(pos["qty"])
                new_qty = old_qty + fill_qty
                new_avg = ((old_qty * float(pos["avg_entry_price"])) + (fill_qty * fill_px)) / new_qty
                conn.execute(
                    """
                    UPDATE live_paper_positions
                    SET qty = ?, avg_entry_price = ?, last_price = ?, last_quote_ts = ?,
                        last_quote_source = ?, stop_loss = COALESCE(?, stop_loss),
                        take_profit = COALESCE(?, take_profit), updated_at = ?
                    WHERE market = ? AND symbol = ?
                    """,
                    (
                        new_qty,
                        round(new_avg, 4),
                        last,
                        str(quote["timestamp"]),
                        str(quote.get("source") or "live-readonly"),
                        order["stop_loss"],
                        order["take_profit"],
                        now.isoformat(),
                        mkt,
                        sym,
                    ),
                )
        else:
            proceeds = fill_qty * fill_px - fee_total
            cash += proceeds
            if pos is not None:
                old_qty = float(pos["qty"])
                closed_qty = min(old_qty, fill_qty)
                realized_delta = round((fill_px - float(pos["avg_entry_price"])) * closed_qty - fee_total, 4)
                rem_pos_qty = round(old_qty - closed_qty, 6)
                if rem_pos_qty <= 1e-8:
                    conn.execute("DELETE FROM live_paper_positions WHERE market = ? AND symbol = ?", (mkt, sym))
                else:
                    conn.execute(
                        "UPDATE live_paper_positions SET qty = ?, last_price = ?, last_quote_ts = ?, updated_at = ? WHERE market = ? AND symbol = ?",
                        (rem_pos_qty, last, str(quote["timestamp"]), now.isoformat(), mkt, sym),
                    )
            else:
                realized_delta = round(-fee_total, 4)

        realized_pnl = round(realized_pnl + realized_delta, 4)
        conn.execute(
            "UPDATE live_paper_accounts SET cash = ?, realized_pnl = ?, updated_at = ? WHERE market = ?",
            (round(cash, 4), realized_pnl, now.isoformat(), mkt),
        )

        prev_filled = float(order["filled_qty"])
        new_filled = round(prev_filled + fill_qty, 6)
        new_rem = round(max(0.0, float(order["qty"]) - new_filled), 6)
        prev_avg = float(order["avg_fill_price"] or 0.0)
        new_avg_fill = ((prev_filled * prev_avg) + (fill_qty * fill_px)) / new_filled
        new_status = "FILLED" if new_rem <= 1e-8 else ("EXPIRED" if str(order["time_in_force"]) == "IOC" else "PARTIALLY_FILLED")
        new_fees = round(float(order["fees_total"]) + fee_total, 4)

        conn.execute(
            """
            UPDATE live_paper_orders
            SET filled_qty = ?, remaining_qty = ?, avg_fill_price = ?, fees_total = ?,
                status = ?, quote_source = ?, quote_timestamp = ?, updated_at = ?
            WHERE client_order_id = ?
            """,
            (
                new_filled,
                0.0 if new_status == "EXPIRED" else new_rem,
                round(new_avg_fill, 4),
                new_fees,
                new_status,
                str(quote.get("source") or "live-readonly"),
                str(quote["timestamp"]),
                now.isoformat(),
                client_order_id,
            ),
        )

        fill_id = f"fill-{uuid.uuid4().hex[:12]}"
        slip_amt = round(abs(fill_px - last) * fill_qty, 4)
        active_run_id = acct["session_run_id"] if "session_run_id" in acct.keys() else None
        active_strat = strategy_source or (str(acct["strategy"] or "") if "strategy" in acct.keys() else "")
        prev_pos_qty = float(pos["qty"]) if pos is not None else 0.0
        cur_pos_row = conn.execute(
            "SELECT qty, avg_entry_price FROM live_paper_positions WHERE market = ? AND symbol = ?",
            (mkt, sym),
        ).fetchone()
        new_pos_qty = float(cur_pos_row["qty"]) if cur_pos_row is not None else 0.0
        pos_change_kind = (
            "OPENED"
            if prev_pos_qty <= 1e-8 and new_pos_qty > 1e-8
            else "CLOSED"
            if prev_pos_qty > 1e-8 and new_pos_qty <= 1e-8
            else "INCREASED"
            if new_pos_qty > prev_pos_qty
            else "REDUCED"
        )
        conn.execute(
            """
            INSERT INTO live_paper_fills
            (fill_id, client_order_id, order_id, market, currency, symbol, instrument_key,
             session_run_id, side, fill_qty, fill_price, mid_reference_price, slippage_amount,
             fees_total, fees_json, realized_pnl_delta, quote_source, quote_timestamp, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill_id,
                client_order_id,
                str(order["order_id"]),
                mkt,
                str(order["currency"]),
                sym,
                str(order["instrument_key"]),
                active_run_id,
                side,
                fill_qty,
                fill_px,
                last,
                slip_amt,
                fee_total,
                json.dumps(fees_info, sort_keys=True),
                realized_delta,
                str(quote.get("source") or "live-readonly"),
                str(quote["timestamp"]),
                now.isoformat(),
            ),
        )
        self._append_audit_conn(
            conn,
            mkt,
            "PAPER_ORDER_FILL",
            {
                "fill_id": fill_id,
                "fill_type": "SIMULATED",
                "client_order_id": client_order_id,
                "order_id": str(order["order_id"]),
                "decision_id": decision_id or "",
                "strategy": active_strat,
                "symbol": sym,
                "instrument_key": str(order["instrument_key"]),
                "session_run_id": active_run_id,
                "side": side,
                "fill_qty": fill_qty,
                "fill_price": fill_px,
                "fees_total": fee_total,
                "realized_pnl_delta": realized_delta,
                "status": new_status,
                "quote_source": quote.get("source"),
                "quote_timestamp": quote["timestamp"],
            },
        )
        self._append_audit_conn(
            conn,
            mkt,
            "PAPER_POSITION_CHANGED",
            {
                "change_type": pos_change_kind,
                "symbol": sym,
                "instrument_key": str(order["instrument_key"]),
                "client_order_id": client_order_id,
                "decision_id": decision_id or "",
                "session_run_id": active_run_id,
                "strategy": active_strat,
                "prev_qty": round(prev_pos_qty, 6),
                "new_qty": round(new_pos_qty, 6),
                "avg_entry_price": round(float(cur_pos_row["avg_entry_price"]), 4) if cur_pos_row is not None else 0.0,
                "realized_pnl_delta": realized_delta,
            },
        )
        return {"fill_id": fill_id, "fill_qty": fill_qty, "fill_price": fill_px, "status": new_status}

    def cancel_order(self, client_order_id: str) -> dict[str, Any]:
        now_iso = _utc_now(self._clock).isoformat()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM live_paper_orders WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown paper order {client_order_id}")
            if str(row["status"]) not in OPEN_ORDER_STATUSES:
                raise ValueError(f"Order {client_order_id} is already terminal ({row['status']})")
            conn.execute(
                "UPDATE live_paper_orders SET status = 'CANCELED', remaining_qty = 0.0, updated_at = ? WHERE client_order_id = ?",
                (now_iso, client_order_id),
            )
            self._append_audit_conn(
                conn,
                str(row["market"]),
                "PAPER_ORDER_CANCELED",
                {"client_order_id": client_order_id, "symbol": row["symbol"]},
            )
            updated = dict(
                conn.execute("SELECT * FROM live_paper_orders WHERE client_order_id = ?", (client_order_id,)).fetchone()
            )
        return {"ok": True, "simulated": True, "order": updated}

    def replace_order(
        self,
        client_order_id: str,
        *,
        qty: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> dict[str, Any]:
        now_iso = _utc_now(self._clock).isoformat()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM live_paper_orders WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown paper order {client_order_id}")
            if str(row["status"]) not in OPEN_ORDER_STATUSES:
                raise ValueError(f"Cannot replace terminal paper order ({row['status']})")
            mkt = str(row["market"])
            lookup_key = str(row["instrument_key"] or row["symbol"]) if mkt == "NSE" else str(row["symbol"])
            inst = self._resolve_instrument(mkt, lookup_key)
            tick_size = float(inst.get("tick_size") or (0.05 if mkt == "NSE" else 0.01))
            new_qty = float(qty) if qty is not None else float(row["qty"])
            if new_qty <= float(row["filled_qty"]):
                raise ValueError("New quantity must exceed already filled quantity")
            new_lim = float(limit_price) if limit_price is not None else row["limit_price"]
            new_stp = float(stop_price) if stop_price is not None else row["stop_price"]
            if new_lim is not None and not _valid_tick(float(new_lim), tick_size, market=mkt):
                raise ValueError("Replacement limit_price violates exchange tick increment")
            if new_stp is not None and not _valid_tick(float(new_stp), tick_size, market=mkt):
                raise ValueError("Replacement stop_price violates exchange tick increment")
            new_rem = round(new_qty - float(row["filled_qty"]), 6)
            conn.execute(
                """
                UPDATE live_paper_orders
                SET qty = ?, remaining_qty = ?, limit_price = ?, stop_price = ?, updated_at = ?
                WHERE client_order_id = ?
                """,
                (new_qty, new_rem, new_lim, new_stp, now_iso, client_order_id),
            )
            self._append_audit_conn(
                conn,
                mkt,
                "PAPER_ORDER_REPLACED",
                {"client_order_id": client_order_id, "qty": new_qty, "limit_price": new_lim, "stop_price": new_stp},
            )
            updated = dict(
                conn.execute("SELECT * FROM live_paper_orders WHERE client_order_id = ?", (client_order_id,)).fetchone()
            )
        return {"ok": True, "simulated": True, "order": updated}

    def modify_position_protection(
        self,
        market: str,
        symbol: str,
        *,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> dict[str, Any]:
        mkt = self._validate_market(market)
        inst = self._resolve_instrument(mkt, symbol.strip())
        sym = str(inst.get("trading_symbol") or inst.get("symbol") or symbol).strip().upper()
        tick_size = float(inst.get("tick_size") or (0.05 if mkt == "NSE" else 0.01))
        if stop_loss is not None and not _valid_tick(float(stop_loss), tick_size, market=mkt):
            raise ValueError("stop_loss violates exchange tick increment")
        if take_profit is not None and not _valid_tick(float(take_profit), tick_size, market=mkt):
            raise ValueError("take_profit violates exchange tick increment")
        now_iso = _utc_now(self._clock).isoformat()
        with self._lock, self._connect() as conn:
            pos = conn.execute(
                "SELECT * FROM live_paper_positions WHERE market = ? AND (symbol = ? OR instrument_key = ?)",
                (mkt, sym, str(inst.get("instrument_key") or "")),
            ).fetchone()
            if pos is None:
                raise ValueError(f"No open {mkt} paper position for {sym}")
            pos_sym = str(pos["symbol"])
            conn.execute(
                "UPDATE live_paper_positions SET stop_loss = ?, take_profit = ?, updated_at = ? WHERE market = ? AND symbol = ?",
                (stop_loss, take_profit, now_iso, mkt, pos_sym),
            )
            self._append_audit_conn(
                conn,
                mkt,
                "PAPER_POSITION_PROTECTION_UPDATED",
                {"symbol": pos_sym, "instrument_key": pos["instrument_key"], "stop_loss": stop_loss, "take_profit": take_profit},
            )
            updated = dict(
                conn.execute("SELECT * FROM live_paper_positions WHERE market = ? AND symbol = ?", (mkt, pos_sym)).fetchone()
            )
        return {"ok": True, "simulated": True, "position": updated}

    def close_position(
        self,
        market: str,
        symbol: str,
        *,
        quote_override: dict[str, Any] | None = None,
        reason: str = "operator explicit close",
    ) -> dict[str, Any]:
        mkt = self._validate_market(market)
        sym_raw = symbol.strip()
        sym = sym_raw.upper()
        with self._lock, self._connect() as conn:
            pos = conn.execute(
                "SELECT * FROM live_paper_positions WHERE market = ? AND (symbol = ? OR instrument_key = ?)",
                (mkt, sym, sym_raw),
            ).fetchone()
            if pos is None:
                raise ValueError(f"No open {mkt} paper position for {sym}")
            qty = float(pos["qty"])
            prod = str(pos["product"])
            order_target = str(pos["instrument_key"]) if mkt == "NSE" and pos["instrument_key"] else str(pos["symbol"])
        return self.submit_order(
            client_order_id=f"lmp-close-{mkt.lower()}-{uuid.uuid4().hex[:10]}",
            market=mkt,
            symbol=order_target,
            side="SELL",
            qty=qty,
            order_type="MARKET",
            product=prod,
            time_in_force="DAY",
            quote_override=quote_override,
        )

    def sync_live_tick(
        self,
        market: str,
        *,
        quotes_by_symbol: dict[str, dict[str, Any]] | None = None,
        bars_by_symbol: dict[str, list[Candle]] | None = None,
    ) -> dict[str, Any]:
        """Mark open positions to live quotes, execute working orders/SL/TP, and evaluate strategy."""
        mkt = self._validate_market(market)
        if self.execution_mode_provider is not None:
            try:
                routing_mode = str(self.execution_mode_provider(mkt) or "PAPER").upper()
            except (ValueError, RuntimeError):
                routing_mode = "PAPER"
            if routing_mode == "LIVE":
                return {
                    "market": mkt,
                    "currency": self.configs[mkt].currency,
                    "session_state": self.market_snapshot(mkt).get("session_state"),
                    "execution_mode": "LIVE_MARKET_LIVE",
                    "paper_simulator_skipped": True,
                    "skip_reason": (
                        "This market is routed LIVE by app.markets.live_execution; the local "
                        "paper simulator does not execute here (mode separation)."
                    ),
                    "simulated": False,
                    "real_order_api_used": False,
                    "broker_order_api_calls": 0,
                }
        with self._market_cycle_locks[mkt]:
            return self._sync_live_tick_locked(
                mkt,
                quotes_by_symbol=quotes_by_symbol,
                bars_by_symbol=bars_by_symbol,
            )

    def _sync_live_tick_locked(
        self,
        mkt: str,
        *,
        quotes_by_symbol: dict[str, dict[str, Any]] | None = None,
        bars_by_symbol: dict[str, list[Candle]] | None = None,
    ) -> dict[str, Any]:
        now = _utc_now(self._clock)
        sess = self._market_session_info(mkt, now)

        with self._lock, self._connect() as conn:
            acct = conn.execute("SELECT * FROM live_paper_accounts WHERE market = ?", (mkt,)).fetchone()
            curr_state = str(acct["session_state"])
            strategy = str(acct["strategy"])
            timeframe = str(acct["timeframe"])
            active_run_id = str(acct["session_run_id"] or "") if "session_run_id" in acct.keys() else ""
            acct_k_en = acct["kronos_enabled"] if "kronos_enabled" in acct.keys() else None
            acct_k_pol = str(acct["kronos_failure_policy"] or "") if "kronos_failure_policy" in acct.keys() else ""
            watched_syms = json.loads(acct["symbols_json"])
            pos_rows = [dict(r) for r in conn.execute("SELECT * FROM live_paper_positions WHERE market = ?", (mkt,))]
            open_orders = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM live_paper_orders WHERE market = ? AND status IN ('WORKING', 'PARTIALLY_FILLED')",
                    (mkt,),
                )
            ]

        if not watched_syms and quotes_by_symbol is None and bars_by_symbol is None:
            watched_syms = self._effective_symbols_for_market(mkt)

        if curr_state == "EMERGENCY_STOPPED":
            return self.market_snapshot(mkt)

        bar_fetcher = self.nse_bars_provider if mkt == "NSE" else self.nasdaq_bars_provider

        if not sess["is_open"]:
            if curr_state == "RUNNING":
                with self._lock, self._connect() as conn:
                    conn.execute(
                        "UPDATE live_paper_accounts SET session_state = 'MARKET_CLOSED_WAITING', state_reason = ?, updated_at = ? WHERE market = ?",
                        (f"{mkt} regular session closed ({sess['state']}); waiting for next open.", now.isoformat(), mkt),
                    )
                    self._append_audit_conn(conn, mkt, "PAPER_SESSION_MARKET_CLOSED", {"session": sess["state"]})
                curr_state = "MARKET_CLOSED_WAITING"
            if (
                curr_state == "MARKET_CLOSED_WAITING"
                and strategy != "manual_only"
                and (bars_by_symbol is not None or bar_fetcher is not None)
            ):
                closed_analysis = self.analyze_market(
                    mkt,
                    trigger="closed_session_cycle",
                    symbols=list(bars_by_symbol.keys()) if bars_by_symbol else (watched_syms or None),
                    quotes_by_symbol=quotes_by_symbol,
                    bars_by_symbol=bars_by_symbol,
                )
                snap_closed = self.market_snapshot(mkt)
                return {**snap_closed, "triggered_orders": [], "closed_market_analysis": closed_analysis}
            return self.market_snapshot(mkt)

        active_symbols = sorted(
            {p["symbol"] for p in pos_rows}
            | {o["symbol"] for o in open_orders}
            | set(watched_syms)
        )
        ikey_by_sym: dict[str, str] = {}
        for p in pos_rows:
            if p.get("instrument_key"):
                ikey_by_sym[str(p["symbol"])] = str(p["instrument_key"])
        for o in open_orders:
            if o.get("instrument_key"):
                ikey_by_sym.setdefault(str(o["symbol"]), str(o["instrument_key"]))

        fetched_quotes: dict[str, dict[str, Any]] = {}
        stale_reasons: dict[str, str] = {}
        quote_errors_by_symbol: dict[str, str] = {}
        disconnect_error: str | None = None

        def _quotes_overridden(raw_sym: str) -> bool:
            if not quotes_by_symbol:
                return False
            try:
                probe_inst = self._resolve_instrument(
                    mkt, ikey_by_sym.get(raw_sym, raw_sym) if mkt == "NSE" else raw_sym
                )
            except Exception:  # noqa: BLE001 - resolution failures are reported below
                return False
            probe_canon = str(probe_inst.get("trading_symbol") or probe_inst.get("symbol") or raw_sym).upper()
            return any(
                quotes_by_symbol.get(key)
                for key in (raw_sym, probe_canon, str(probe_inst.get("instrument_key") or ""))
                if key
            )

        def _fetch_quote_for(raw_sym: str) -> dict[str, Any]:
            lookup_ref = ikey_by_sym.get(raw_sym, raw_sym) if mkt == "NSE" else raw_sym
            return self._fetch_live_quote(mkt, self._resolve_instrument(mkt, lookup_ref))

        # One bounded fan-out per cycle: quotes for positions, working orders and
        # the whole watchlist are fetched concurrently but paced by the provider
        # gate, so a 100-symbol market no longer costs 100 sequential round trips.
        pending_quote_syms = [s for s in active_symbols if not _quotes_overridden(s)]
        quote_outcomes = (
            self.fetch_gates[mkt].map(
                pending_quote_syms,
                _fetch_quote_for,
                classify=lambda exc: self._classify_fetch_exception(mkt, exc),
            )
            if pending_quote_syms
            else {}
        )

        for sym in active_symbols:
            try:
                lookup_ref = ikey_by_sym.get(sym, sym) if mkt == "NSE" else sym
                inst = self._resolve_instrument(mkt, lookup_ref)
                canon_sym = str(inst.get("trading_symbol") or inst.get("symbol") or sym).upper()
                explicit_quote = (
                    (quotes_by_symbol or {}).get(sym)
                    or (quotes_by_symbol or {}).get(canon_sym)
                    or (quotes_by_symbol or {}).get(str(inst.get("instrument_key") or ""))
                )
                if explicit_quote:
                    q = explicit_quote
                else:
                    outcome = quote_outcomes.get(sym)
                    if outcome is None or not outcome.ok:
                        # Keep the gate's own code: re-classifying here would
                        # relabel a rate limit as a disconnect.
                        err_code = outcome.error_code if outcome is not None else "NO_FETCH_OUTCOME"
                        err_detail = (
                            outcome.error_detail if outcome is not None
                            else f"no quote fetch outcome for {sym}"
                        )
                        disconnect_error = f"[{err_code}] {err_detail}"
                        quote_errors_by_symbol[sym.split("|")[-1].upper()] = disconnect_error
                        continue
                    q = outcome.value
                q_val = self._validate_quote_for_paper(
                    mkt,
                    inst,
                    q,
                    now,
                    sess_info=sess,
                    record_monotonic_watermark=True,
                )
                if q_val["valid_for_fill"]:
                    fetched_quotes[sym] = q
                    fetched_quotes.setdefault(canon_sym, q)
                elif q_val["reason_code"] in {"STALE_QUOTE", "OUT_OF_ORDER_QUOTE", "DELAYED_FEED_BLOCKED", "MALFORMED_QUOTE"}:
                    stale_reasons[canon_sym] = f"[{q_val['reason_code']}] {q_val['reason_detail']}"
            except Exception as exc:
                err_code, err_detail = self._classify_fetch_exception(mkt, exc)
                disconnect_error = f"[{err_code}] {err_detail}"
                quote_errors_by_symbol[sym.split("|")[-1].upper()] = disconnect_error

        if curr_state in {"STALE_FEED_PAUSED", "DISCONNECTED_PAUSED", "MARKET_CLOSED_WAITING"}:
            if sess["is_open"] and (not active_symbols or (fetched_quotes and not stale_reasons and not disconnect_error)):
                with self._lock, self._connect() as conn:
                    conn.execute(
                        "UPDATE live_paper_accounts SET session_state = 'RUNNING', state_reason = NULL, updated_at = ? WHERE market = ?",
                        (now.isoformat(), mkt),
                    )
                    self._append_audit_conn(
                        conn,
                        mkt,
                        "PAPER_SESSION_RECONNECTED",
                        {"previous_state": curr_state, "resumed_at": now.isoformat()},
                    )
                curr_state = "RUNNING"

        if curr_state == "RUNNING":
            if disconnect_error and not fetched_quotes:
                with self._lock, self._connect() as conn:
                    conn.execute(
                        "UPDATE live_paper_accounts SET session_state = 'DISCONNECTED_PAUSED', state_reason = ?, updated_at = ? WHERE market = ?",
                        (f"Live feed unavailable: {disconnect_error}", now.isoformat(), mkt),
                    )
                    self._append_audit_conn(conn, mkt, "PAPER_SESSION_DISCONNECTED", {"detail": disconnect_error})
                return self.market_snapshot(mkt)
            if stale_reasons and not fetched_quotes:
                first_stale = next(iter(stale_reasons.values()))
                with self._lock, self._connect() as conn:
                    conn.execute(
                        "UPDATE live_paper_accounts SET session_state = 'STALE_FEED_PAUSED', state_reason = ?, updated_at = ? WHERE market = ?",
                        (f"New entries paused: {first_stale}", now.isoformat(), mkt),
                    )
                    self._append_audit_conn(conn, mkt, "PAPER_SESSION_STALE_FEED", {"reasons": stale_reasons})
                return self.market_snapshot(mkt)

        protective_triggers: list[tuple[str, float, dict[str, Any], str]] = []
        with self._lock, self._connect() as conn:
            for pos in pos_rows:
                sym = pos["symbol"]
                q = fetched_quotes.get(sym)
                if not q:
                    continue
                last = float(q["last"])
                conn.execute(
                    "UPDATE live_paper_positions SET last_price = ?, last_quote_ts = ?, last_quote_source = ?, updated_at = ? WHERE market = ? AND symbol = ?",
                    (last, str(q["timestamp"]), str(q.get("source") or "live-readonly"), now.isoformat(), mkt, sym),
                )
                sl = pos.get("stop_loss")
                tp = pos.get("take_profit")
                if sl is not None and last <= float(sl):
                    protective_triggers.append((sym, float(pos["qty"]), q, "STOP_LOSS_TRIGGERED"))
                elif tp is not None and last >= float(tp):
                    protective_triggers.append((sym, float(pos["qty"]), q, "TAKE_PROFIT_TRIGGERED"))

            if sess["is_open"]:
                for ord_row in open_orders:
                    sym = ord_row["symbol"]
                    q = fetched_quotes.get(sym)
                    if q:
                        lookup_ref = str(ord_row["instrument_key"] or sym) if mkt == "NSE" else sym
                        inst = self._resolve_instrument(mkt, lookup_ref)
                        self._try_fill_order_conn(conn, str(ord_row["client_order_id"]), inst, q, now)

        triggered_orders: list[str] = []
        for sym, qty_to_close, q, trig_reason in protective_triggers:
            if sess["is_open"]:
                res_close = self.close_position(mkt, sym, quote_override=q, reason=trig_reason)
                if res_close.get("order"):
                    triggered_orders.append(str(res_close["order"]["client_order_id"]))

        # Evaluate completed-bar strategy decisions if session is RUNNING and strategy != manual_only
        resolved_bars_map = bars_by_symbol
        bar_errors_by_symbol: dict[str, str] = {}
        missing_bar_syms: list[str] = []
        target_strategy_syms = list(bars_by_symbol.keys()) if bars_by_symbol is not None else (watched_syms or sorted(fetched_quotes.keys()))

        if curr_state == "RUNNING" and sess["is_open"] and strategy != "manual_only" and resolved_bars_map is None:
            if bar_fetcher is not None:
                auto_bars: dict[str, list[Candle]] = {}
                # Same bounded, paced fan-out as the quotes: 100 symbols = 100
                # requests, not 100 sequential round trips.
                bar_outcomes = self.fetch_gates[mkt].map(
                    target_strategy_syms,
                    lambda raw_sym: bar_fetcher(raw_sym, timeframe),
                    classify=lambda exc: self._classify_fetch_exception(mkt, exc),
                )
                for sym in target_strategy_syms:
                    canon_b = sym.split("|")[-1].upper()
                    outcome = bar_outcomes.get(sym)
                    if outcome is None or not outcome.ok:
                        missing_bar_syms.append(canon_b)
                        bar_errors_by_symbol[canon_b] = (
                            f"[{outcome.error_code}] {redact_sensitive_data(outcome.error_detail)}"
                            if outcome is not None
                            else f"No result for {canon_b}"
                        )
                    elif outcome.value:
                        auto_bars[sym] = outcome.value
                    else:
                        missing_bar_syms.append(canon_b)
                        bar_errors_by_symbol[canon_b] = f"Empty {timeframe} bar history returned by provider"
                resolved_bars_map = auto_bars
            else:
                for sym in target_strategy_syms:
                    canon_b = sym.split("|")[-1].upper()
                    missing_bar_syms.append(canon_b)
                    bar_errors_by_symbol[canon_b] = f"No historical bar provider configured for {mkt}"

        if curr_state == "RUNNING" and sess["is_open"] and strategy != "manual_only":
            skipped_to_log: list[tuple[str, str, str]] = []
            for s_err, msg_err in bar_errors_by_symbol.items():
                st_kind = "MISSING_DATA" if s_err in missing_bar_syms else "FAILED"
                skipped_to_log.append((s_err, st_kind, msg_err))
            for s_qerr, msg_qerr in quote_errors_by_symbol.items():
                if s_qerr not in bar_errors_by_symbol:
                    skipped_to_log.append((s_qerr, "FAILED", f"Quote fetch failed: {msg_qerr}"))
            for s_st, msg_st in stale_reasons.items():
                if s_st not in bar_errors_by_symbol and s_st not in quote_errors_by_symbol:
                    skipped_to_log.append((s_st, "STALE_DATA", msg_st))
            if skipped_to_log:
                with self._lock, self._connect() as conn:
                    for s_sym, s_st, s_rsn in skipped_to_log:
                        self._append_audit_conn(
                            conn,
                            mkt,
                            "PAPER_SYMBOL_SKIPPED",
                            {
                                "session_run_id": active_run_id,
                                "symbol": s_sym,
                                "strategy": strategy,
                                "timeframe": timeframe,
                                "status": s_st,
                                "reason": s_rsn,
                            },
                        )

        if curr_state == "RUNNING" and sess["is_open"] and strategy != "manual_only" and (resolved_bars_map or (strategy.strip().lower() == "parallel_ensemble" and self.ensemble_service is not None and target_strategy_syms)):
            if strategy.strip().lower() == "parallel_ensemble" and self.ensemble_service is not None:
                ens_cycle = self.ensemble_service.evaluate_watchlist_cycle(
                    mkt,
                    symbols=target_strategy_syms or sorted((resolved_bars_map or {}).keys()),
                    timeframe=timeframe,
                    bars_override=resolved_bars_map,
                    quotes_override=fetched_quotes,
                    session_open_override=bool(sess["is_open"]),
                    execute_in_paper=True,
                    pre_fetch_errors=bar_errors_by_symbol,
                    now_utc=now,
                )
                for dec_item in ens_cycle.get("decisions") or []:
                    if dec_item.get("idempotent_duplicate_skipped"):
                        continue
                    dec_id = str(dec_item.get("decision_id") or f"dec-{uuid.uuid4().hex[:12]}")
                    act_lbl = str(dec_item.get("final_action") or "NO_TRADE")
                    reasons_list = dec_item.get("no_trade_reasons") or []
                    reason_lbl = (
                        f"Ensemble {act_lbl} {dec_item.get('selected_qty', 0)}u (conf={(dec_item.get('calibration') or {}).get('calibrated_confidence')})"
                        if act_lbl in {"BUY", "SELL"}
                        else f"NO_TRADE: {'; '.join(reasons_list[:2]) or 'Thresholds not met'}"
                    )
                    k_contrib = dict(dec_item.get("kronos_contribution") or {})
                    fc_used = bool(k_contrib.get("advisory_used"))
                    fc_prod = bool(k_contrib.get("forecast_produced") or fc_used or k_contrib.get("status") in {"OK", "STALE", "WIDE_BAND_REJECTED", "LOW_CONFIDENCE"})
                    k_st = str(k_contrib.get("status") or ("CONSUMED_CONFIRMED" if fc_used else "NOT_CONFIGURED"))
                    pol_app = str(k_contrib.get("policy") or "PARALLEL_ENSEMBLE_ALLOCATOR")
                    explanation_obj = {
                        "decision_id": dec_id,
                        "session_run_id": active_run_id,
                        "strategy": "parallel_ensemble",
                        "symbol": str(dec_item["symbol"]),
                        "timeframe": timeframe,
                        "kronos_status": k_st,
                        "forecast_produced": fc_prod,
                        "forecast_consumed": fc_used,
                        "policy_applied": pol_app,
                        "forecast_summary": k_contrib,
                        "strategy_signals": {
                            "primary_strategy": "parallel_ensemble",
                            "aggregation": dec_item.get("aggregation"),
                            "calibration": dec_item.get("calibration"),
                            "regime": dec_item.get("regime"),
                        },
                        "risk_checks": dec_item.get("risk_checks") or [],
                        "final_action": act_lbl,
                        "order_client_id": dec_item.get("paper_order_id"),
                    }
                    with self._lock, self._connect() as conn:
                        conn.execute(
                            """
                            INSERT INTO live_paper_decisions
                            (decision_id, market, symbol, strategy, timeframe, action, reason, bar_timestamp,
                             order_client_id, created_at, session_run_id, kronos_status, forecast_produced,
                             forecast_consumed, policy_applied, explanation_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                dec_id,
                                mkt,
                                str(dec_item["symbol"]),
                                "parallel_ensemble",
                                timeframe,
                                act_lbl,
                                reason_lbl,
                                str(dec_item["bar_timestamp"]),
                                dec_item.get("paper_order_id"),
                                now.isoformat(),
                                active_run_id,
                                k_st,
                                1 if fc_prod else 0,
                                1 if fc_used else 0,
                                pol_app,
                                json.dumps(explanation_obj, sort_keys=True),
                            ),
                        )
                        self._append_audit_conn(
                            conn,
                            mkt,
                            "PAPER_STRATEGY_DECISION",
                            {
                                "decision_id": dec_id,
                                "session_run_id": active_run_id,
                                "symbol": str(dec_item["symbol"]),
                                "strategy": "parallel_ensemble",
                                "timeframe": timeframe,
                                "action": act_lbl,
                                "reason": reason_lbl,
                                "kronos_status": k_st,
                                "forecast_produced": fc_prod,
                                "forecast_consumed": fc_used,
                                "order_client_id": dec_item.get("paper_order_id"),
                            },
                        )
                succ_s = [str(d["symbol"]) for d in (ens_cycle.get("decisions") or [])]
                rej_map = dict(ens_cycle.get("rejected_instruments") or {})
                miss_s = [s for s, v in rej_map.items() if str(v.get("code") or "") in {"MISSING_DATA", "INSUFFICIENT_BARS"}]
                fail_s = [s for s in rej_map if s not in miss_s]
                stale_s = [s for s in stale_reasons if s not in rej_map]
                live_cov = {
                    "market": mkt,
                    "currency": self.configs[mkt].currency,
                    "trigger": "open_session_cycle",
                    "analyzed_at": now.isoformat(),
                    "wall_ms": ens_cycle.get("wall_ms", 0.0),
                    "session_run_id": active_run_id or None,
                    "session_state": curr_state,
                    "exchange_session": sess,
                    "strategy": "parallel_ensemble",
                    "timeframe": timeframe,
                    "enabled_strategy_variants": ens_cycle.get("enabled_variants_count", 0),
                    "trade_execution_permitted_now": True,
                    "counts": {
                        "selected_symbols": len(set(succ_s) | set(rej_map.keys())),
                        "succeeded": len(succ_s),
                        "missing_data": len(miss_s),
                        "stale_data": len(stale_s),
                        "failed": len(fail_s),
                    },
                    "succeeded_symbols": succ_s,
                    "missing_data_symbols": miss_s,
                    "stale_data_symbols": stale_s,
                    "failed_symbols": fail_s,
                    "symbols": [
                        {
                            "symbol": str(d["symbol"]),
                            "status": "STALE_DATA" if str(d["symbol"]) in stale_s else "SUCCEEDED",
                            "code": "IDEMPOTENT_BAR" if d.get("idempotent_duplicate_skipped") else "EVALUATED",
                            "reason": (
                                "Bar already evaluated (idempotent skip)"
                                if d.get("idempotent_duplicate_skipped")
                                else f"Action={d.get('final_action')} · qty={d.get('selected_qty', 0)}"
                            ),
                            "latest_bar_timestamp": d.get("bar_timestamp"),
                            "last_close": d.get("reference_price"),
                            "trade_eligible_now": True,
                            "ensemble_raw_intent": d.get("raw_intent"),
                            "ensemble_final_action": d.get("final_action"),
                            "ensemble_consensus": (d.get("aggregation") or {}).get("post_kronos_consensus"),
                            "active_weighted_variants": (d.get("aggregation") or {}).get("active_weighted_variants"),
                            "calibrated_confidence": (d.get("calibration") or {}).get("calibrated_confidence"),
                            "no_trade_reasons": d.get("no_trade_reasons") or [],
                        }
                        for d in (ens_cycle.get("decisions") or [])
                    ] + [
                        {
                            "symbol": r_sym,
                            "status": "MISSING_DATA" if r_sym in miss_s else "FAILED",
                            "code": str(r_info.get("code") or "REJECTED"),
                            "reason": str(r_info.get("reason") or "Unavailable"),
                            "trade_eligible_now": False,
                            "no_trade_reasons": [str(r_info.get("reason") or "Unavailable")],
                        }
                        for r_sym, r_info in rej_map.items()
                    ],
                    "simulated_only": True,
                    "broker_order_api_calls": 0,
                }
                with self._lock, self._connect() as conn:
                    conn.execute(
                        "UPDATE live_paper_accounts SET last_analysis_json = ?, updated_at = ? WHERE market = ?",
                        (json.dumps(live_cov, sort_keys=True), now.isoformat(), mkt),
                    )
                snap = self.market_snapshot(mkt)
                return {**snap, "triggered_orders": triggered_orders, "ensemble_cycle": ens_cycle}

            resolved_bars_map = resolved_bars_map or {}

            k_configured = (
                bool(acct_k_en)
                if acct_k_en is not None
                else self._resolve_kronos_requested(mkt, strategy, None)
            )
            policy_dict: dict[str, Any] = {}
            if self.kronos_provider is not None and hasattr(self.kronos_provider, "paper_policy_view"):
                policy_dict = dict(self.kronos_provider.paper_policy_view())
            if acct_k_pol:
                policy_dict["failure_policy"] = acct_k_pol

            for sym, raw_bars_list in resolved_bars_map.items():
                if not raw_bars_list:
                    continue
                bars: list[Candle] = []
                bar_err: str | None = None
                for raw_b in raw_bars_list:
                    if isinstance(raw_b, Candle):
                        bars.append(raw_b)
                    elif isinstance(raw_b, dict):
                        try:
                            c_px = float(raw_b["close"])
                            bars.append(
                                Candle(
                                    symbol=str(raw_b.get("symbol") or sym).replace("|", "_"),
                                    timestamp=parse_timestamp(str(raw_b["timestamp"])),
                                    open=float(raw_b.get("open", c_px)),
                                    high=float(raw_b.get("high", c_px)),
                                    low=float(raw_b.get("low", c_px)),
                                    close=c_px,
                                    volume=float(raw_b.get("volume", 0.0)),
                                )
                            )
                        except (KeyError, ValueError, TypeError):
                            bar_err = "MALFORMED_BAR_OHLCV"
                            break
                    else:
                        bar_err = "MALFORMED_BAR_OHLCV"
                        break
                if bar_err is None and not bars:
                    continue
                prev_bts: datetime | None = None
                for b in bars:
                    if bar_err is not None:
                        break
                    if (
                        not all(math.isfinite(v) and v > 0 for v in (b.open, b.high, b.low, b.close))
                        or not math.isfinite(b.volume)
                        or b.volume < 0
                        or b.low > min(b.open, b.close)
                        or b.high < max(b.open, b.close)
                    ):
                        bar_err = "MALFORMED_BAR_OHLCV"
                        break
                    if prev_bts is not None and b.timestamp <= prev_bts:
                        bar_err = "OUT_OF_ORDER_OR_DUPLICATE_BAR"
                        break
                    prev_bts = b.timestamp
                if bar_err is not None:
                    bad_dec_id = f"dec-{uuid.uuid4().hex[:12]}"
                    with self._lock, self._connect() as conn:
                        conn.execute(
                            """
                            INSERT INTO live_paper_decisions
                            (decision_id, market, symbol, strategy, timeframe, action, reason, bar_timestamp,
                             order_client_id, created_at, session_run_id, kronos_status, forecast_produced,
                             forecast_consumed, policy_applied)
                            VALUES (?, ?, ?, ?, ?, 'HOLD', ?, ?, NULL, ?, ?, 'BAR_INTEGRITY_BLOCKED', 0, 0, 'BAR_VALIDATION_FAIL_CLOSED')
                            """,
                            (
                                bad_dec_id,
                                mkt,
                                sym,
                                strategy,
                                timeframe,
                                f"BLOCKED [{bar_err}]: bar series failed integrity validation",
                                bars[-1].timestamp.isoformat(),
                                now.isoformat(),
                                active_run_id,
                            ),
                        )
                    continue
                latest_bar_iso = bars[-1].timestamp.isoformat()
                with self._lock, self._connect() as conn:
                    already_evaluated = conn.execute(
                        """
                        SELECT decision_id FROM live_paper_decisions
                        WHERE market = ? AND symbol = ? AND strategy = ? AND timeframe = ? AND bar_timestamp = ?
                        LIMIT 1
                        """,
                        (mkt, sym, strategy, timeframe, latest_bar_iso),
                    ).fetchone()
                if already_evaluated is not None:
                    continue

                decision_id = f"dec-{uuid.uuid4().hex[:12]}"
                strat_lower = strategy.strip().lower()
                base_rule_sig = (
                    {"action": "HOLD", "reason": ""}
                    if strat_lower in {"kronos_ensemble", "kronos_informed"}
                    else evaluate_bar_strategy_signal(strategy, bars)
                )

                k_outcome: dict[str, Any] | None = None
                if k_configured:
                    if self.kronos_provider is None:
                        k_outcome = {
                            "ok": False,
                            "status": "UNAVAILABLE",
                            "forecast": None,
                            "error_code": "PROVIDER_MISSING",
                            "error_reason": "Kronos provider is not configured on this engine",
                            "latency_ms": None,
                            "lifecycle_state": "FAILED",
                        }
                    elif hasattr(self.kronos_provider, "predict_for_session"):
                        k_outcome = self.kronos_provider.predict_for_session(
                            self._session_key(mkt),
                            session_id=active_run_id,
                            decision_id=decision_id,
                            market=mkt,
                            symbol=sym,
                            timeframe=timeframe,
                            bars=bars,
                        )
                    else:
                        try:
                            fc_raw = self.kronos_provider.predict(
                                market=mkt,
                                symbol=sym,
                                timeframe=timeframe,
                                bars=bars,
                                require_fresh=False,
                            )
                            k_outcome = {"ok": True, "status": "OK", "forecast": fc_raw, "lifecycle_state": "RUNNING"}
                        except Exception as exc:
                            k_outcome = {
                                "ok": False,
                                "status": "FAILED",
                                "forecast": None,
                                "error_code": type(exc).__name__,
                                "error_reason": str(exc)[:180],
                                "lifecycle_state": "FAILED",
                            }

                with self._lock, self._connect() as conn:
                    cur_held_row = conn.execute(
                        "SELECT qty FROM live_paper_positions WHERE market = ? AND (symbol = ? OR instrument_key = ?)",
                        (mkt, sym, sym),
                    ).fetchone()
                cur_held_qty = float(cur_held_row["qty"]) if cur_held_row is not None else 0.0

                decision = combine_paper_strategy_decision(
                    strategy=strategy,
                    bars=bars,
                    base_rule_signal=base_rule_sig,
                    kronos_configured=k_configured,
                    kronos_outcome=k_outcome,
                    policy=policy_dict,
                    current_position_qty=cur_held_qty,
                )
                if (
                    decision.get("forecast_consumed")
                    and isinstance(k_outcome, dict)
                    and isinstance(k_outcome.get("forecast"), dict)
                    and self.kronos_provider is not None
                    and hasattr(self.kronos_provider, "record_forecast_consumption")
                ):
                    self.kronos_provider.record_forecast_consumption(
                        session_key=self._session_key(mkt),
                        session_id=active_run_id,
                        decision_id=decision_id,
                        market=mkt,
                        symbol=sym,
                        timeframe=timeframe,
                        strategy=strategy,
                        forecast=k_outcome["forecast"],
                        final_action=decision["action"],
                        contribution_detail=decision["reason"],
                    )

                order_cid: str | None = None
                risk_checks: list[dict[str, Any]] = []
                order_outcome = "NO_ORDER_HOLD"
                final_action = str(decision["action"])
                final_reason = str(decision["reason"])
                q = fetched_quotes.get(sym)
                if final_action == "BUY":
                    if not q:
                        order_outcome = "BLOCKED_NO_VALID_QUOTE"
                        final_reason = f"{final_reason} · [Paper Risk Gate: no fresh quote for execution]"
                    else:
                        inst = self._resolve_instrument(mkt, sym)
                        lot = int(inst.get("quantity_step") or inst.get("lot_size") or 1)
                        sub = self.submit_order(
                            market=mkt,
                            symbol=str(inst.get("instrument_key") or sym) if mkt == "NSE" else sym,
                            side="BUY",
                            qty=float(lot),
                            order_type="MARKET",
                            product=str(inst.get("product") or ("D" if mkt == "NSE" else "CASH")),
                            quote_override=q,
                            decision_id=decision_id,
                            strategy_source=strategy,
                        )
                        risk_checks = list((sub.get("preview") or {}).get("checks") or [])
                        if sub.get("ok") and sub.get("order"):
                            order_cid = str(sub["order"]["client_order_id"])
                            order_outcome = f"SIMULATED_{sub['order']['status']}"
                        else:
                            rej = "; ".join((sub.get("preview") or {}).get("rejection_reasons") or ["Paper risk gate rejected order"])
                            order_outcome = "REJECTED_BY_PAPER_RISK_GATE"
                            final_reason = f"{final_reason} · [Paper Risk Rejected: {rej}]"
                elif final_action == "SELL":
                    with self._lock, self._connect() as conn:
                        held = conn.execute(
                            "SELECT symbol, qty, instrument_key, product FROM live_paper_positions WHERE market = ? AND (symbol = ? OR instrument_key = ?)",
                            (mkt, sym, sym),
                        ).fetchone()
                    if held and float(held["qty"]) > 0 and q:
                        target_sym = str(held["instrument_key"]) if mkt == "NSE" and held["instrument_key"] else str(held["symbol"])
                        sub_close = self.submit_order(
                            client_order_id=f"lmp-close-{mkt.lower()}-{uuid.uuid4().hex[:10]}",
                            market=mkt,
                            symbol=target_sym,
                            side="SELL",
                            qty=float(held["qty"]),
                            order_type="MARKET",
                            product=str(held["product"]),
                            time_in_force="DAY",
                            quote_override=q,
                            decision_id=decision_id,
                            strategy_source=strategy,
                        )
                        risk_checks = list((sub_close.get("preview") or {}).get("checks") or [])
                        if sub_close.get("ok") and sub_close.get("order"):
                            order_cid = str(sub_close["order"]["client_order_id"])
                            order_outcome = f"SIMULATED_CLOSE_{sub_close['order']['status']}"
                    else:
                        order_outcome = "NO_LONG_POSITION_TO_CLOSE"

                risk_passed = order_outcome != "REJECTED_BY_PAPER_RISK_GATE"
                explanation_obj = {
                    "decision_id": decision_id,
                    "session_run_id": active_run_id,
                    "market": mkt,
                    "symbol": sym,
                    "strategy": strategy,
                    "timeframe": timeframe,
                    "bar_timestamp": decision.get("bar_ts") or latest_bar_iso,
                    "rule_action": (decision.get("strategy_signals") or {}).get("rule_action"),
                    "input_window": {
                        "bar_count": len(bars),
                        "input_start": bars[0].timestamp.isoformat(),
                        "input_end": bars[-1].timestamp.isoformat(),
                        "timeframe": timeframe,
                    },
                    "kronos_configured": k_configured,
                    "kronos_status": decision["kronos_status"],
                    "forecast_produced": bool(decision["forecast_produced"]),
                    "forecast_consumed": bool(decision["forecast_consumed"]),
                    "policy_applied": decision["policy_applied"],
                    "forecast_summary": decision["forecast_summary"],
                    "strategy_signals": decision["strategy_signals"],
                    "contribution_policy": decision["contribution_policy"],
                    "risk_checks": {
                        "passed": risk_passed,
                        "checks": risk_checks,
                    },
                    "order_outcome": order_outcome,
                    "order_client_id": order_cid,
                    "final_action": final_action,
                    "simulated": True,
                    "real_money": False,
                }

                with self._lock, self._connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO live_paper_decisions
                        (decision_id, market, symbol, strategy, timeframe, action, reason, bar_timestamp,
                         order_client_id, created_at, session_run_id, kronos_status, forecast_produced,
                         forecast_consumed, policy_applied, explanation_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            decision_id,
                            mkt,
                            sym,
                            strategy,
                            timeframe,
                            final_action,
                            final_reason,
                            decision.get("bar_ts") or latest_bar_iso,
                            order_cid,
                            now.isoformat(),
                            active_run_id,
                            str(decision["kronos_status"]),
                            1 if decision["forecast_produced"] else 0,
                            1 if decision["forecast_consumed"] else 0,
                            str(decision["policy_applied"]),
                            json.dumps(explanation_obj, sort_keys=True),
                        ),
                    )
                    self._append_audit_conn(
                        conn,
                        mkt,
                        "PAPER_STRATEGY_DECISION",
                        {
                            "decision_id": decision_id,
                            "session_run_id": active_run_id,
                            "symbol": sym,
                            "strategy": strategy,
                            "timeframe": timeframe,
                            "action": final_action,
                            "reason": final_reason,
                            "kronos_status": decision["kronos_status"],
                            "forecast_produced": bool(decision["forecast_produced"]),
                            "forecast_consumed": bool(decision["forecast_consumed"]),
                            "policy_applied": decision["policy_applied"],
                            "order_outcome": order_outcome,
                            "order_client_id": order_cid,
                        },
                    )
        snap = self.market_snapshot(mkt)
        return {**snap, "triggered_orders": triggered_orders}

    def evaluate_strategy_bars(
        self,
        market: str,
        symbol: str,
        bars: list[Any],
        *,
        strategy: str = "moving_average",
        auto_execute: bool = False,
    ) -> dict[str, Any]:
        mkt = self._validate_market(market)
        inst = self._resolve_instrument(mkt, symbol)
        sym = str(inst["symbol"])
        candles: list[Candle] = []
        for row in bars:
            if isinstance(row, Candle):
                candles.append(row)
            elif isinstance(row, dict):
                c = float(row["close"])
                candles.append(
                    Candle(
                        timestamp=parse_timestamp(str(row["timestamp"])),
                        open=float(row.get("open", c)),
                        high=float(row.get("high", c)),
                        low=float(row.get("low", c)),
                        close=c,
                        volume=float(row.get("volume", 1000.0)),
                        symbol=sym.replace("|", "-"),
                    )
                )
        res = evaluate_bar_strategy_signal(strategy, candles)
        return {
            "market": mkt,
            "symbol": sym,
            "strategy": strategy,
            "signal": res["action"],
            "action": res["action"],
            "reason": res["reason"],
            "simulated": True,
        }

    def emergency_stop(
        self,
        reason: str = "operator emergency stop",
        *,
        market: str | None = None,
    ) -> dict[str, Any]:
        """Halt new paper entries and cancel unfilled working entry orders while keeping position SL/TP.

        When ``market`` is provided (``NSE`` or ``NASDAQ``), only that market's paper
        session and working orders are halted, preserving independent operation and
        safeguards for the other market. When ``market`` is ``None``, all markets are halted.
        """
        target_markets = (self._validate_market(market),) if market else MARKETS
        now_iso = _utc_now(self._clock).isoformat()
        canceled_by_market: dict[str, int] = {}
        preserved_positions: dict[str, int] = {}
        with self._lock, self._connect() as conn:
            for mkt in target_markets:
                cur = conn.execute(
                    """
                    UPDATE live_paper_orders
                    SET status = 'CANCELED', remaining_qty = 0.0, reject_reason = ?, updated_at = ?
                    WHERE market = ? AND status IN ('WORKING', 'PARTIALLY_FILLED')
                    """,
                    (f"Canceled by Emergency Stop: {reason}", now_iso, mkt),
                )
                canceled_by_market[mkt] = cur.rowcount
                pos_cnt = conn.execute(
                    "SELECT COUNT(*) AS n FROM live_paper_positions WHERE market = ? AND (stop_loss IS NOT NULL OR take_profit IS NOT NULL)",
                    (mkt,),
                ).fetchone()["n"]
                preserved_positions[mkt] = int(pos_cnt)
                conn.execute(
                    "UPDATE live_paper_accounts SET session_state = 'EMERGENCY_STOPPED', state_reason = ?, updated_at = ? WHERE market = ?",
                    (reason, now_iso, mkt),
                )
                self._append_audit_conn(
                    conn,
                    mkt,
                    "PAPER_EMERGENCY_STOP",
                    {
                        "reason": reason,
                        "scope": mkt if market else "ALL_MARKETS",
                        "canceled_working_orders": cur.rowcount,
                        "positions_with_preserved_sl_tp": int(pos_cnt),
                    },
                )
        if self.kronos_provider is not None and hasattr(self.kronos_provider, "stop_for_paper_session"):
            for mkt in target_markets:
                self.kronos_provider.stop_for_paper_session(
                    self._session_key(mkt),
                    reason=f"emergency stop: {reason}",
                )
        return {
            "ok": True,
            "simulated": True,
            "execution_mode": "LIVE_MARKET_PAPER",
            "scope": target_markets[0] if market else "ALL_MARKETS",
            "markets_halted": list(target_markets),
            "canceled_working_orders": canceled_by_market,
            "positions_with_preserved_sl_tp": preserved_positions,
        }

    def reset_emergency_stop(self, *, market: str | None = None) -> dict[str, Any]:
        target_markets = (self._validate_market(market),) if market else MARKETS
        now_iso = _utc_now(self._clock).isoformat()
        with self._lock, self._connect() as conn:
            for mkt in target_markets:
                conn.execute(
                    """
                    UPDATE live_paper_accounts
                    SET session_state = 'STOPPED',
                        state_reason = 'Emergency Stop cleared by operator; session remains STOPPED until explicitly started.',
                        updated_at = ?
                    WHERE market = ? AND session_state = 'EMERGENCY_STOPPED'
                    """,
                    (now_iso, mkt),
                )
                self._append_audit_conn(
                    conn,
                    mkt,
                    "PAPER_EMERGENCY_STOP_CLEARED",
                    {"scope": mkt if market else "ALL_MARKETS"},
                )
        return {
            "ok": True,
            "scope": target_markets[0] if market else "ALL_MARKETS",
            "nse": self.market_snapshot("NSE"),
            "nasdaq": self.market_snapshot("NASDAQ"),
        }

    def reconcile(self, market: str) -> dict[str, Any]:
        """Verify local SQLite cash + fill ledger + audit hash chain integrity."""
        mkt = self._validate_market(market)
        with self._lock, self._connect() as conn:
            acct = dict(conn.execute("SELECT * FROM live_paper_accounts WHERE market = ?", (mkt,)).fetchone())
            positions = [dict(r) for r in conn.execute("SELECT * FROM live_paper_positions WHERE market = ?", (mkt,))]
            audit_rows = [
                dict(r) for r in conn.execute("SELECT * FROM live_paper_audit ORDER BY seq ASC")
            ]

        prev = "GENESIS"
        chain_valid = True
        for row in audit_rows:
            expected = hashlib.sha256(
                f"{prev}|{row['market']}|{row['event_kind']}|{row['created_at']}|{row['payload_json']}".encode("utf-8")
            ).hexdigest()
            if row["prev_hash"] != prev or row["entry_hash"] != expected:
                chain_valid = False
                break
            prev = str(row["entry_hash"])

        cash_finite = math.isfinite(float(acct["cash"])) and float(acct["cash"]) >= -0.01
        ok = chain_valid and cash_finite
        return {
            "ok": ok,
            "simulated": True,
            "execution_mode": "LIVE_MARKET_PAPER",
            "market": mkt,
            "currency": acct["currency"],
            "audit_chain_valid": chain_valid,
            "audit_events_verified": len(audit_rows),
            "cash_balance_valid": cash_finite,
            "open_positions": len(positions),
            "checked_at": _utc_now(self._clock).isoformat(),
        }

    def close(self) -> None:
        self._closed = True
        if self.kronos_provider is not None and hasattr(self.kronos_provider, "stop_for_paper_session"):
            for mkt in MARKETS:
                self.kronos_provider.stop_for_paper_session(self._session_key(mkt), reason="engine close")
        if self._mem_conn is not None:
            self._mem_conn.close()
            self._mem_conn = None

    def market_snapshot(self, market: str) -> dict[str, Any]:
        mkt = self._validate_market(market)
        now = _utc_now(self._clock)
        sess = self._market_session_info(mkt, now)
        with self._lock, self._connect() as conn:
            acct = dict(conn.execute("SELECT * FROM live_paper_accounts WHERE market = ?", (mkt,)).fetchone())
            positions = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM live_paper_positions WHERE market = ? ORDER BY symbol ASC", (mkt,)
                )
            ]
            orders = [
                {
                    **dict(r),
                    "simulated": True,
                    "real_money": False,
                    "execution_mode": "LIVE_MARKET_PAPER",
                    "mode_label": "LIVE-MARKET PAPER · SIMULATED",
                }
                for r in conn.execute(
                    "SELECT * FROM live_paper_orders WHERE market = ? ORDER BY created_at DESC, rowid DESC LIMIT 100", (mkt,)
                )
            ]
            fills = [
                {
                    **dict(r),
                    "simulated": True,
                    "real_money": False,
                    "fill_type": "SIMULATED",
                    "execution_mode": "LIVE_MARKET_PAPER",
                    "mode_label": "LIVE-MARKET PAPER · SIMULATED",
                    "execution_destination": f"LOCAL_PAPER_SQLITE ({mkt})",
                    "fee_breakdown": json.loads(r["fees_json"] or "{}"),
                }
                for r in conn.execute(
                    "SELECT * FROM live_paper_fills WHERE market = ? ORDER BY created_at DESC, rowid DESC LIMIT 100", (mkt,)
                )
            ]
            raw_decisions = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM live_paper_decisions WHERE market = ? ORDER BY created_at DESC LIMIT 50", (mkt,)
                )
            ]
            decisions = []
            for d in raw_decisions:
                exp_raw = d.get("explanation_json")
                exp_parsed = json.loads(exp_raw) if exp_raw else None
                decisions.append({
                    **d,
                    "simulated": True,
                    "real_money": False,
                    "execution_mode": "LIVE_MARKET_PAPER",
                    "mode_label": "LIVE-MARKET PAPER · SIMULATED",
                    "forecast_produced": bool(d.get("forecast_produced")),
                    "forecast_consumed": bool(d.get("forecast_consumed")),
                    "explanation": exp_parsed,
                })

        enriched_positions = []
        market_value = 0.0
        unrealized_pnl = 0.0
        for p in positions:
            mv = round(float(p["qty"]) * float(p["last_price"]), 2)
            upnl = round((float(p["last_price"]) - float(p["avg_entry_price"])) * float(p["qty"]), 2)
            upnl_pct = round(((float(p["last_price"]) - float(p["avg_entry_price"])) / float(p["avg_entry_price"])) * 100.0, 2) if float(p["avg_entry_price"]) > 0 else 0.0
            market_value += mv
            unrealized_pnl += upnl
            enriched_positions.append({
                **p,
                "simulated": True,
                "real_money": False,
                "fill_type": "SIMULATED",
                "execution_mode": "LIVE_MARKET_PAPER",
                "mode_label": "LIVE-MARKET PAPER · SIMULATED",
                "market_value": mv,
                "unrealized_pnl": upnl,
                "unrealized_pnl_pct": upnl_pct,
            })

        cash = round(float(acct["cash"]), 2)
        equity = round(cash + market_value, 2)
        persisted_realized = round(float(acct["realized_pnl"]), 2)
        today_str = str((sess.get("detail") or {}).get("trading_day") or now.date().isoformat())
        if str(acct["trading_day"]) != today_str:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "UPDATE live_paper_accounts SET day_start_equity = ?, trading_day = ? WHERE market = ?",
                    (equity, today_str, mkt),
                )
            day_start_eq = equity
        else:
            day_start_eq = float(acct["day_start_equity"])
        day_pnl = round(equity - day_start_eq, 2)

        active_run_id = acct.get("session_run_id")
        session_active = bool(active_run_id) and str(acct["session_state"]) not in {"STOPPED"}
        sess_start_eq = float(acct["session_start_equity"]) if acct.get("session_start_equity") is not None else equity
        sess_start_real = float(acct["session_start_realized_pnl"]) if acct.get("session_start_realized_pnl") is not None else persisted_realized
        current_session_realized = round(persisted_realized - sess_start_real, 2) if session_active else 0.0
        current_session_total = round(equity - sess_start_eq, 2) if session_active else 0.0
        total_fees = round(sum(float(f["fees_total"]) for f in fills), 2)
        strat_name = str(acct["strategy"] or "manual_only")
        auto_state = (
            "ENABLED_COMPLETED_BARS"
            if (session_active and strat_name.lower() != "manual_only")
            else "MANUAL_ONLY_NO_AUTO_ORDERS"
        )

        return {
            "market": mkt,
            "currency": acct["currency"],
            "simulated": True,
            "fill_type": "SIMULATED",
            "real_order_api_used": False,
            "execution_mode": "LIVE_MARKET_PAPER",
            "market_data_source": (
                "upstox_market_data (read-only NSE REST/BOD)"
                if mkt == "NSE"
                else "alpaca_market_data (read-only NASDAQ REST IEX/SIP)"
            ),
            "execution_destination": f"LOCAL_SQLITE_SIMULATOR ({mkt} · {acct['currency']} · zero broker orders)",
            "automation_state": auto_state,
            "mode_banner": f"SIMULATED LOCAL PAPER ({mkt} · {acct['currency']}) — REAL MARKET DATA · ZERO BROKER ORDERS",
            "session_state": acct["session_state"],
            "state_reason": acct["state_reason"],
            "session": {
                "state": acct["session_state"],
                "reason": acct["state_reason"],
                "strategy": strat_name,
                "timeframe": acct["timeframe"],
                "automation_state": auto_state,
                "session_run_id": active_run_id if session_active else None,
                "fill_type": "SIMULATED",
            },
            "exchange_session": sess,
            "selected_for_multi": bool(acct.get("selected_for_multi", 1) if acct.get("selected_for_multi") is not None else True),
            "strategy": strat_name,
            "timeframe": acct["timeframe"],
            "watched_symbols": json.loads(acct["symbols_json"]),
            "effective_symbols": self._effective_symbols_for_market(mkt),
            "last_analysis": json.loads(acct["last_analysis_json"]) if acct.get("last_analysis_json") else None,
            "account": {
                "currency": acct["currency"],
                "starting_cash": self.configs[mkt].starting_cash,
                "cash": cash,
                "market_value": round(market_value, 2),
                "equity": equity,
                "realized_pnl": persisted_realized,
                "persisted_historical_realized_pnl": persisted_realized,
                "current_session_run_id": active_run_id if session_active else None,
                "current_session_realized_pnl": current_session_realized,
                "current_session_total_pnl": current_session_total,
                "unrealized_pnl": round(unrealized_pnl, 2),
                "day_pnl": day_pnl,
                "total_fees_paid": total_fees,
                "pnl_scope_note": (
                    "current_session_* tracks strictly the active session run; "
                    "persisted_historical_realized_pnl tracks cumulative local history across prior runs."
                ),
            },
            "pnl_attribution": {
                "current_session_active": session_active,
                "current_session_run_id": active_run_id if session_active else None,
                "current_session_realized_pnl": current_session_realized,
                "current_session_total_pnl": current_session_total,
                "persisted_historical_realized_pnl": persisted_realized,
                "open_position_unrealized_pnl": round(unrealized_pnl, 2),
                "trading_day": today_str,
                "trading_day_pnl": day_pnl,
            },
            "risk_limits": asdict(self.configs[mkt]),
            "kronos_session": (
                self.kronos_provider.session_view(self._session_key(mkt))
                if self.kronos_provider is not None and hasattr(self.kronos_provider, "session_view")
                else {
                    "session_key": self._session_key(mkt),
                    "kronos_configured": bool(acct.get("kronos_enabled")),
                    "lifecycle_state": "NOT_CONFIGURED",
                    "failure_policy": str(acct.get("kronos_failure_policy") or "FAIL_CLOSED_NO_ENTRY"),
                }
            ),
            "positions": enriched_positions,
            "orders": orders,
            "fills": fills,
            "decisions": decisions,
            "updated_at": acct["updated_at"],
        }

    def audit_log(self, *, market: str = "", limit: int = 150) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 500))
        with self._lock, self._connect() as conn:
            if market and market.upper() in MARKETS:
                rows = conn.execute(
                    "SELECT * FROM live_paper_audit WHERE market = ? ORDER BY seq DESC LIMIT ?",
                    (market.upper(), lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM live_paper_audit ORDER BY seq DESC LIMIT ?",
                    (lim,),
                ).fetchall()
        return [
            {
                "seq": int(r["seq"]),
                "market": str(r["market"]),
                "event_kind": str(r["event_kind"]),
                "payload": json.loads(r["payload_json"]),
                "prev_hash": str(r["prev_hash"]),
                "entry_hash": str(r["entry_hash"]),
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

    def structured_logs(
        self,
        *,
        window: str = "",
        since: str = "",
        until: str = "",
        market: str = "",
        symbol: str = "",
        category: str = "",
        event_type: str = "",
        severity: str = "",
        strategy: str = "",
        session_id: str = "",
        query: str = "",
        limit: int = 250,
        extra_events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return durable, secret-redacted, correlated, filterable structured paper & Kronos logs."""
        raw_rows = self.audit_log(market=market if market.upper() in MARKETS else "", limit=500)
        normalized = [
            normalize_structured_event(
                event_id=f"lpa-{row['seq']}",
                seq=int(row["seq"]),
                timestamp=str(row["created_at"]),
                event_type=str(row["event_kind"]),
                market=str(row["market"]),
                source="LIVE_MARKET_PAPER",
                payload=dict(row["payload"] or {}),
                entry_hash=str(row["entry_hash"]),
            )
            for row in raw_rows
        ]
        if extra_events:
            seen_ids = {str(e.get("event_id")) for e in normalized}
            for ev in extra_events:
                eid = str(ev.get("event_id") or "")
                if eid and eid not in seen_ids:
                    normalized.append(ev)
                    seen_ids.add(eid)
        filtered = filter_structured_events(
            normalized,
            now_utc=_utc_now(self._clock),
            window=window,
            since=since,
            until=until,
            market=market,
            symbol=symbol,
            category=category,
            event_type=event_type,
            severity=severity,
            strategy=strategy,
            session_id=session_id,
            query=query,
            limit=limit,
        )
        observed_sessions = sorted({str(e["session_id"]) for e in normalized if e.get("session_id")})
        observed_strategies = sorted({str(e["strategy"]) for e in normalized if e.get("strategy")})
        observed_symbols = sorted({str(e["symbol"]) for e in normalized if e.get("symbol")})
        return {
            "ok": True,
            "simulated_paper_scope": True,
            "real_money_events_included": any(bool(e.get("real_money")) for e in filtered),
            "total_unfiltered": len(normalized),
            "total_matching": len(filtered),
            "returned": len(filtered),
            "returned_count": len(filtered),
            "observed_sessions": observed_sessions,
            "observed_strategies": observed_strategies,
            "observed_symbols": observed_symbols,
            "events": filtered,
        }

    def export_audit_bundle(self, *, market: str = "", limit: int = 500) -> dict[str, Any]:
        """Export an auditable SHA-256 chain-verified bundle of paper sessions, fills, and events."""
        now_iso = _utc_now(self._clock).isoformat()
        target_markets = [self._validate_market(market)] if market else list(MARKETS)
        snapshots = {m: self.market_snapshot(m) for m in target_markets}
        reconciliations = {m: self.reconcile(m) for m in target_markets}
        events = self.audit_log(market=market, limit=limit)
        bundle_core = {
            "exported_at": now_iso,
            "execution_mode": "LIVE_MARKET_PAPER",
            "simulated": True,
            "fill_type": "SIMULATED",
            "real_order_api_used": False,
            "markets_included": target_markets,
            "all_audit_chains_valid": all(r["audit_chain_valid"] for r in reconciliations.values()),
            "reconciliation": reconciliations,
            "snapshots": snapshots,
            "audit_events": events,
        }
        digest = hashlib.sha256(json.dumps(bundle_core, sort_keys=True).encode("utf-8")).hexdigest()
        return {"bundle_sha256": digest, **bundle_core}

    def advance_selected_markets_concurrently(
        self,
        *,
        markets: list[str] | None = None,
        only_active: bool = True,
        market_timeout_seconds: float = 12.0,
        quotes_by_market: dict[str, dict[str, dict[str, Any]]] | None = None,
        bars_by_market: dict[str, dict[str, list[Candle]]] | None = None,
    ) -> dict[str, Any]:
        """Advance all eligible selected markets concurrently without cross-market blocking.

        Each market executes in an isolated worker with its own timeout so a slow or
        failing provider on one market never delays or aborts another market's cycle.
        """
        t_start = time.perf_counter()
        now_iso = _utc_now(self._clock).isoformat()
        target_markets = (
            [self._validate_market(m) for m in markets]
            if markets
            else self.selected_markets()
        )
        eligible_states = {"RUNNING", "MARKET_CLOSED_WAITING", "STALE_FEED_PAUSED", "DISCONNECTED_PAUSED"}
        state_by_mkt: dict[str, str] = {}
        with self._lock, self._connect() as conn:
            for mkt in target_markets:
                row = conn.execute(
                    "SELECT session_state FROM live_paper_accounts WHERE market = ?",
                    (mkt,),
                ).fetchone()
                state_by_mkt[mkt] = str(row["session_state"]) if row else "STOPPED"

        to_run = [
            m for m in target_markets
            if (not only_active) or (state_by_mkt.get(m) in eligible_states)
        ]
        # A market routed to the LIVE execution path is never simulated here, not even
        # for an operator-triggered paper cycle: the two paths stay strictly separate.
        live_routed: set[str] = set()
        if self.execution_mode_provider is not None:
            for m in target_markets:
                try:
                    if str(self.execution_mode_provider(m) or "PAPER").upper() == "LIVE":
                        live_routed.add(m)
                except (ValueError, RuntimeError):
                    continue
        to_run = [m for m in to_run if m not in live_routed]
        results: dict[str, dict[str, Any]] = {}
        for m in target_markets:
            if m not in to_run:
                if m in live_routed:
                    results[m] = {
                        "market": m,
                        "status": "SKIPPED_LIVE_ROUTED",
                        "session_state": state_by_mkt.get(m, "STOPPED"),
                        "detail": "Market execution mode is LIVE; the paper simulator does not run here.",
                        "wall_ms": 0.0,
                        "broker_order_api_calls": 0,
                    }
                    continue
                results[m] = {
                    "market": m,
                    "status": "SKIPPED_INACTIVE",
                    "session_state": state_by_mkt.get(m, "STOPPED"),
                    "wall_ms": 0.0,
                    "broker_order_api_calls": 0,
                }

        if to_run:
            timeout_s = max(0.05, float(market_timeout_seconds))
            pool = ThreadPoolExecutor(max_workers=len(to_run), thread_name_prefix="axiom-mm")
            futures_by_mkt = {}

            def _run_single(m_code: str) -> tuple[float, dict[str, Any]]:
                m_t0 = time.perf_counter()
                out = self.sync_live_tick(
                    m_code,
                    quotes_by_symbol=(quotes_by_market or {}).get(m_code),
                    bars_by_symbol=(bars_by_market or {}).get(m_code),
                )
                return round((time.perf_counter() - m_t0) * 1000.0, 2), out

            submitted_at: dict[str, float] = {}
            try:
                for m in to_run:
                    submitted_at[m] = time.perf_counter()
                    futures_by_mkt[m] = pool.submit(_run_single, m)
                # Per-market deadlines. Every market is submitted at ~the same
                # instant and each one owns the window [its submission, +timeout].
                # The earlier implementation kept ONE shared deadline and waited on
                # the futures in list order, so the time spent waiting on a slow NSE
                # worker was deducted from NASDAQ's remaining budget and a fast
                # NASDAQ cycle could be reported TIMEOUT purely because NSE was
                # slow. Waiting on the batch instead makes each verdict depend only
                # on that market's own elapsed time.
                batch_deadline = max(submitted_at.values()) + timeout_s
                wait_for_futures(
                    list(futures_by_mkt.values()),
                    timeout=max(0.0, batch_deadline - time.perf_counter()),
                )
                for m, fut in futures_by_mkt.items():
                    try:
                        dur_ms, snap_out = fut.result(timeout=0)
                        results[m] = {
                            "market": m,
                            "status": "COMPLETED",
                            "session_state": snap_out.get("session_state"),
                            "exchange_open": bool((snap_out.get("exchange_session") or {}).get("is_open")),
                            "wall_ms": dur_ms,
                            "decisions_count": len(snap_out.get("decisions") or []),
                            "orders_count": len(snap_out.get("orders") or []),
                            "last_analysis_counts": ((snap_out.get("last_analysis") or {}).get("counts")),
                            "snapshot": snap_out,
                            "broker_order_api_calls": 0,
                        }
                    except FuturesTimeoutError:
                        elapsed_ms = (time.perf_counter() - submitted_at[m]) * 1000.0
                        results[m] = {
                            "market": m,
                            "status": "TIMEOUT",
                            "error": (
                                f"{m} provider cycle exceeded {timeout_s:.2f}s timeout; "
                                "other markets continued independently"
                            ),
                            "wall_ms": round(max(elapsed_ms, timeout_s * 1000.0), 2),
                            "broker_order_api_calls": 0,
                        }
                    except Exception as exc:
                        err_clean = str(redact_sensitive_data(str(exc)))[:220]
                        results[m] = {
                            "market": m,
                            "status": "FAILED",
                            "error": err_clean,
                            "wall_ms": round((time.perf_counter() - t_start) * 1000.0, 2),
                            "broker_order_api_calls": 0,
                        }
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

        return {
            "ok": True,
            "simulated_only": True,
            "live_trading_enabled": False,
            "broker_order_api_calls": 0,
            "evaluated_at": now_iso,
            "selected_markets": target_markets,
            "markets_advanced": to_run,
            "wall_ms": round((time.perf_counter() - t_start) * 1000.0, 2),
            "markets": results,
        }


class MultiMarketPaperRunner:
    """Non-blocking background coordinator advancing all active selected paper markets concurrently.

    Safety invariants:
    - Local paper simulation only (``live_trading_enabled = False``, ``broker_order_api_calls = 0``).
    - Never touches ``STOPPED``, ``PAUSED``, ``RECOVERED_PAUSED``, or ``EMERGENCY_STOPPED`` sessions.
    - Isolates each market with independent asyncio/thread workers and timeouts so a slow or
      broken provider on NSE never stalls NASDAQ (and vice versa).
    """

    live_trading_enabled = False
    broker_order_api_calls = 0

    def __init__(
        self,
        engine: LiveMarketPaperEngine,
        *,
        interval_seconds: float = 10.0,
        market_timeout_seconds: float = 12.0,
        enabled: bool = True,
        paper_blocked: Callable[[str], bool] | None = None,
    ) -> None:
        self.engine = engine
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.market_timeout_seconds = max(0.1, float(market_timeout_seconds))
        self.enabled = bool(enabled)
        # Markets routed to LIVE execution must never be simulated by this runner.
        self.paper_blocked = paper_blocked
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._executor = ThreadPoolExecutor(max_workers=len(MARKETS) * 2, thread_name_prefix="axiom-mm-bg")
        self._in_flight: dict[str, bool] = {m: False for m in MARKETS}
        self._stats_lock = threading.Lock()
        self._cycles_total = 0
        self._last_tick_at: str | None = None
        self._per_market_telemetry: dict[str, dict[str, Any]] = {
            m: {
                "market": m,
                "cycles_completed": 0,
                "last_cycle_at": None,
                "last_duration_ms": None,
                "last_status": "IDLE",
                "last_error": None,
                "in_flight": False,
            }
            for m in MARKETS
        }

    def start(self) -> None:
        if not self.enabled or (self._task is not None and not self._task.done()):
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="axiom-multimarket-paper-runner")

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def _advance_one_market(self, mkt: str) -> dict[str, Any]:
        with self._stats_lock:
            if self._in_flight.get(mkt):
                return {"market": mkt, "status": "SKIPPED_IN_FLIGHT", "broker_order_api_calls": 0}
            self._in_flight[mkt] = True
            self._per_market_telemetry[mkt]["in_flight"] = True

        t0 = time.perf_counter()
        loop = asyncio.get_running_loop()
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            snap = await asyncio.wait_for(
                loop.run_in_executor(self._executor, self.engine.sync_live_tick, mkt),
                timeout=self.market_timeout_seconds,
            )
            dur_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            with self._stats_lock:
                tel = self._per_market_telemetry[mkt]
                tel["cycles_completed"] = int(tel["cycles_completed"]) + 1
                tel["last_cycle_at"] = now_iso
                tel["last_duration_ms"] = dur_ms
                tel["last_status"] = str(snap.get("session_state") or "COMPLETED")
                tel["last_error"] = None
            return {
                "market": mkt,
                "status": "COMPLETED",
                "session_state": snap.get("session_state"),
                "wall_ms": dur_ms,
                "broker_order_api_calls": 0,
            }
        except asyncio.TimeoutError:
            dur_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            err = f"{mkt} cycle timed out after {self.market_timeout_seconds:.1f}s"
            with self._stats_lock:
                tel = self._per_market_telemetry[mkt]
                tel["last_cycle_at"] = now_iso
                tel["last_duration_ms"] = dur_ms
                tel["last_status"] = "TIMEOUT"
                tel["last_error"] = err
            return {"market": mkt, "status": "TIMEOUT", "error": err, "wall_ms": dur_ms, "broker_order_api_calls": 0}
        except Exception as exc:
            dur_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            err = str(redact_sensitive_data(str(exc)))[:220]
            with self._stats_lock:
                tel = self._per_market_telemetry[mkt]
                tel["last_cycle_at"] = now_iso
                tel["last_duration_ms"] = dur_ms
                tel["last_status"] = "FAILED"
                tel["last_error"] = err
            return {"market": mkt, "status": "FAILED", "error": err, "wall_ms": dur_ms, "broker_order_api_calls": 0}
        finally:
            with self._stats_lock:
                self._in_flight[mkt] = False
                self._per_market_telemetry[mkt]["in_flight"] = False

    async def run_once(self, *, markets: list[str] | None = None, only_active: bool = True) -> dict[str, Any]:
        if getattr(self.engine, "_closed", False):
            return {"ok": False, "reason": "engine_closed", "markets": {}, "broker_order_api_calls": 0}
        selected = [self.engine._validate_market(m) for m in markets] if markets else self.engine.selected_markets()
        eligible_states = {"RUNNING", "MARKET_CLOSED_WAITING", "STALE_FEED_PAUSED", "DISCONNECTED_PAUSED"}
        eligible: list[str] = []
        skipped: dict[str, dict[str, Any]] = {}
        for m in selected:
            if self.paper_blocked is not None:
                try:
                    routed_live = bool(self.paper_blocked(m))
                except (ValueError, RuntimeError):
                    routed_live = False
                if routed_live:
                    skipped[m] = {
                        "market": m,
                        "status": "SKIPPED_LIVE_ROUTED",
                        "detail": "Market execution mode is LIVE; the paper simulator does not run here.",
                        "broker_order_api_calls": 0,
                    }
                    continue
            snap = self.engine.market_snapshot(m)
            st = str(snap.get("session_state") or "STOPPED")
            if (not only_active) or (st in eligible_states):
                eligible.append(m)
            else:
                skipped[m] = {"market": m, "status": "SKIPPED_INACTIVE", "session_state": st, "broker_order_api_calls": 0}

        t0 = time.perf_counter()
        coros = [self._advance_one_market(m) for m in eligible]
        ran_list = await asyncio.gather(*coros) if coros else []
        out_map = {**skipped, **{r["market"]: r for r in ran_list}}
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._stats_lock:
            self._cycles_total += 1
            self._last_tick_at = now_iso
        return {
            "ok": True,
            "simulated_only": True,
            "live_trading_enabled": False,
            "broker_order_api_calls": 0,
            "evaluated_at": now_iso,
            "selected_markets": selected,
            "markets_advanced": eligible,
            "wall_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            "markets": out_map,
        }

    async def _loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            interval_elapsed = False
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
                break
            except asyncio.TimeoutError:
                interval_elapsed = True
            if not interval_elapsed or self._stop_event.is_set() or getattr(self.engine, "_closed", False):
                break
            try:
                await self.run_once(only_active=True)
            except Exception as exc:
                _ = exc

    def status(self) -> dict[str, Any]:
        with self._stats_lock:
            per_mkt = {k: dict(v) for k, v in self._per_market_telemetry.items()}
            cycles = self._cycles_total
            last_at = self._last_tick_at
        running = self._task is not None and not self._task.done()
        return {
            "enabled": self.enabled,
            "background_loop_running": running,
            "interval_seconds": self.interval_seconds,
            "market_timeout_seconds": self.market_timeout_seconds,
            "cycles_total": cycles,
            "last_tick_at": last_at,
            "selected_markets": self.engine.selected_markets() if not getattr(self.engine, "_closed", False) else [],
            "live_trading_enabled": False,
            "broker_order_api_calls": 0,
            "markets": per_mkt,
        }


