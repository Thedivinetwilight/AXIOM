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

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable
import uuid

from app.data.models import Candle, parse_timestamp
from app.markets.nse_reconciliation import nse_session_status, tick_precision_decimals
from app.markets.us_calendar import USEquitiesCalendar

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
        clock: Callable[[], datetime] | None = None,
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
        self.us_calendar = USEquitiesCalendar()

        self.configs: dict[str, PaperMarketConfig] = {
            "NSE": PaperMarketConfig.default_nse(),
            "NASDAQ": PaperMarketConfig.default_nasdaq(),
        }
        self._init_schema()
        self._bootstrap_or_recover()

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
        enriched = {
            "simulated": True,
            "execution_mode": "LIVE_MARKET_PAPER",
            "market": market,
            **payload,
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

    def add_watchlist_symbol(self, market: str, symbol_or_key: str) -> dict[str, Any]:
        mkt = self._validate_market(market)
        inst = self._resolve_instrument(mkt, symbol_or_key)
        sym = str(inst.get("trading_symbol") or inst.get("symbol") or symbol_or_key).upper()
        ikey = str(inst.get("instrument_key") or f"{mkt}|{sym}")
        name = str(inst.get("name") or sym)
        now_iso = _utc_now(self._clock).isoformat()
        with self._lock, self._connect() as conn:
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

    # -- Session Controls ------------------------------------------------------

    def update_config(self, market: str, **updates: Any) -> dict[str, Any]:
        mkt = self._validate_market(market)
        current = asdict(self.configs[mkt])
        for key, val in updates.items():
            if key in current and key not in {"market", "currency"} and val is not None:
                current[key] = type(current[key])(val)
        cfg = PaperMarketConfig(**current)
        if not cfg.configured():
            raise ValueError("All paper risk and capital limits must be positive")
        self.configs[mkt] = cfg
        now_iso = _utc_now(self._clock).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE live_paper_accounts SET config_json = ?, updated_at = ? WHERE market = ?",
                (json.dumps(asdict(cfg), sort_keys=True), now_iso, mkt),
            )
            self._append_audit_conn(conn, mkt, "PAPER_CONFIG_UPDATED", {"config": asdict(cfg)})
        return asdict(cfg)

    def start_session(
        self,
        market: str,
        *,
        strategy: str = "manual_only",
        timeframe: str = "5m",
        symbols: list[str] | None = None,
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
        resolved_syms: list[str] = []
        for s in (symbols or []):
            inst = self._resolve_instrument(mkt, s)
            resolved_syms.append(str(inst.get("trading_symbol") or inst.get("symbol")))
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT session_state FROM live_paper_accounts WHERE market = ?", (mkt,)).fetchone()
            if row and str(row["session_state"]) == "EMERGENCY_STOPPED":
                raise RuntimeError("Emergency Stop is engaged; reset Emergency Stop before starting a session")
            conn.execute(
                """
                UPDATE live_paper_accounts
                SET session_state = ?, state_reason = ?, strategy = ?, timeframe = ?,
                    symbols_json = ?, started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE market = ?
                """,
                (
                    state,
                    reason,
                    strategy.strip() or "manual_only",
                    timeframe.strip() or "5m",
                    json.dumps(resolved_syms),
                    now.isoformat(),
                    now.isoformat(),
                    mkt,
                ),
            )
            self._append_audit_conn(
                conn,
                mkt,
                "PAPER_SESSION_STARTED",
                {"state": state, "strategy": strategy, "timeframe": timeframe, "symbols": resolved_syms},
            )
        return self.market_snapshot(mkt)

    def transition_session(self, market: str, action: str, *, reason: str = "") -> dict[str, Any]:
        mkt = self._validate_market(market)
        act = action.strip().lower()
        now = _utc_now(self._clock)
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT session_state FROM live_paper_accounts WHERE market = ?", (mkt,)).fetchone()
            curr_state = str(row["session_state"]) if row else "STOPPED"
            if act == "pause":
                next_state = "PAUSED"
                next_reason = reason or "Paused by operator"
            elif act == "resume":
                if curr_state == "EMERGENCY_STOPPED":
                    raise RuntimeError("Reset Emergency Stop explicitly before resuming")
                sess = self._market_session_info(mkt, now)
                next_state = "RUNNING" if sess["is_open"] else "MARKET_CLOSED_WAITING"
                next_reason = None if sess["is_open"] else f"{mkt} session is {sess['state']}"
            elif act == "stop":
                next_state = "STOPPED"
                next_reason = reason or "OPERATOR_STOP"
            else:
                raise ValueError(f"Unsupported session transition {action!r}")
            conn.execute(
                "UPDATE live_paper_accounts SET session_state = ?, state_reason = ?, updated_at = ? WHERE market = ?",
                (next_state, next_reason, now.isoformat(), mkt),
            )
            self._append_audit_conn(
                conn,
                mkt,
                f"PAPER_SESSION_{next_state}",
                {"previous_state": curr_state, "reason": next_reason},
            )
        return self.market_snapshot(mkt)

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
                    started_at = NULL, updated_at = ?
                WHERE market = ?
                """,
                (cfg.starting_cash, cfg.starting_cash, now.date().isoformat(), now.isoformat(), mkt),
            )
            self._append_audit_conn(conn, mkt, "PAPER_ACCOUNT_RESET", {"starting_cash": cfg.starting_cash})
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
        try:
            quote_data = self._fetch_live_quote(mkt, inst, quote_override=quote_override)
            q_last = float(quote_data.get("last") or 0.0)
            q_ts = parse_timestamp(str(quote_data["timestamp"]))
            quote_age = round(max(0.0, (now - q_ts).total_seconds()), 2)
            is_fresh = quote_age <= cfg.max_quote_age_seconds and not bool(quote_data.get("is_delayed", False))
            if otype_u == "MARKET":
                check(
                    "live_quote_fresh",
                    is_fresh and sess_info["is_open"],
                    f"fresh live {mkt} quote ({quote_age:.1f}s old)"
                    if (is_fresh and sess_info["is_open"])
                    else (
                        f"MARKET paper fill blocked: {mkt} session={sess_info['state']}, "
                        f"quote age={quote_age:.1f}s (max {cfg.max_quote_age_seconds:.0f}s)"
                    ),
                )
            else:
                check(
                    "live_quote_available",
                    q_last > 0 and (is_fresh or allow_off_hours_working or otype_u == "LIMIT"),
                    f"reference quote {q_last} ({quote_age:.1f}s old)",
                )
        except Exception as exc:
            q_last = float(limit_price or stop_price or 0.0)
            if otype_u == "LIMIT" and allow_off_hours_working and q_last > 0:
                check("live_quote_available", True, "working limit staged against limit_price while feed offline")
            else:
                check("live_quote_fresh", False, f"live {mkt} market data unavailable: {str(exc)[:180]}")

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
            "quote_source": quote_data.get("source") if quote_data else None,
            "quote_timestamp": quote_data.get("timestamp") if quote_data else None,
            "quote_age_seconds": quote_age,
            "session": sess_info,
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
            with self._lock, self._connect() as conn:
                self._append_audit_conn(
                    conn,
                    mkt,
                    "PAPER_ORDER_REJECTED",
                    {
                        "client_order_id": cid,
                        "symbol": preview.get("symbol", symbol),
                        "reasons": preview["rejection_reasons"],
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

        inst = self._resolve_instrument(mkt, preview["symbol"])
        quote_data = self._fetch_live_quote(mkt, inst, quote_override=quote_override) if preview.get("quote_timestamp") else None

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
                {"client_order_id": cid, "order_id": order_id, "symbol": preview["symbol"], "side": preview["side"], "qty": qty, "order_type": preview["order_type"]},
            )
            if quote_data is not None and self._market_session_info(mkt, now)["is_open"]:
                self._try_fill_order_conn(conn, cid, inst, quote_data, now)
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
        conn.execute(
            """
            INSERT INTO live_paper_fills
            (fill_id, client_order_id, order_id, market, currency, symbol, side,
             fill_qty, fill_price, mid_reference_price, slippage_amount, fees_total,
             fees_json, realized_pnl_delta, quote_source, quote_timestamp, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill_id,
                client_order_id,
                str(order["order_id"]),
                mkt,
                str(order["currency"]),
                sym,
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
                "client_order_id": client_order_id,
                "symbol": sym,
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
            inst = self._resolve_instrument(mkt, str(row["symbol"]))
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
        sym = symbol.strip().upper()
        inst = self._resolve_instrument(mkt, sym)
        tick_size = float(inst.get("tick_size") or (0.05 if mkt == "NSE" else 0.01))
        if stop_loss is not None and not _valid_tick(float(stop_loss), tick_size, market=mkt):
            raise ValueError("stop_loss violates exchange tick increment")
        if take_profit is not None and not _valid_tick(float(take_profit), tick_size, market=mkt):
            raise ValueError("take_profit violates exchange tick increment")
        now_iso = _utc_now(self._clock).isoformat()
        with self._lock, self._connect() as conn:
            pos = conn.execute(
                "SELECT * FROM live_paper_positions WHERE market = ? AND symbol = ?", (mkt, sym)
            ).fetchone()
            if pos is None:
                raise ValueError(f"No open {mkt} paper position for {sym}")
            conn.execute(
                "UPDATE live_paper_positions SET stop_loss = ?, take_profit = ?, updated_at = ? WHERE market = ? AND symbol = ?",
                (stop_loss, take_profit, now_iso, mkt, sym),
            )
            self._append_audit_conn(
                conn,
                mkt,
                "PAPER_POSITION_PROTECTION_UPDATED",
                {"symbol": sym, "stop_loss": stop_loss, "take_profit": take_profit},
            )
            updated = dict(
                conn.execute("SELECT * FROM live_paper_positions WHERE market = ? AND symbol = ?", (mkt, sym)).fetchone()
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
        sym = symbol.strip().upper()
        with self._lock, self._connect() as conn:
            pos = conn.execute(
                "SELECT * FROM live_paper_positions WHERE market = ? AND symbol = ?", (mkt, sym)
            ).fetchone()
            if pos is None:
                raise ValueError(f"No open {mkt} paper position for {sym}")
            qty = float(pos["qty"])
            prod = str(pos["product"])
        return self.submit_order(
            client_order_id=f"lmp-close-{mkt.lower()}-{uuid.uuid4().hex[:10]}",
            market=mkt,
            symbol=sym,
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
        cfg = self.configs[mkt]
        now = _utc_now(self._clock)
        sess = self._market_session_info(mkt, now)

        with self._lock, self._connect() as conn:
            acct = conn.execute("SELECT * FROM live_paper_accounts WHERE market = ?", (mkt,)).fetchone()
            curr_state = str(acct["session_state"])
            strategy = str(acct["strategy"])
            timeframe = str(acct["timeframe"])
            watched_syms = json.loads(acct["symbols_json"])
            pos_rows = [dict(r) for r in conn.execute("SELECT * FROM live_paper_positions WHERE market = ?", (mkt,))]
            open_orders = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM live_paper_orders WHERE market = ? AND status IN ('WORKING', 'PARTIALLY_FILLED')",
                    (mkt,),
                )
            ]

        if curr_state == "EMERGENCY_STOPPED":
            return self.market_snapshot(mkt)

        if not sess["is_open"] and curr_state == "RUNNING":
            with self._lock, self._connect() as conn:
                conn.execute(
                    "UPDATE live_paper_accounts SET session_state = 'MARKET_CLOSED_WAITING', state_reason = ?, updated_at = ? WHERE market = ?",
                    (f"{mkt} regular session closed ({sess['state']}); waiting for next open.", now.isoformat(), mkt),
                )
                self._append_audit_conn(conn, mkt, "PAPER_SESSION_MARKET_CLOSED", {"session": sess["state"]})
            return self.market_snapshot(mkt)

        active_symbols = sorted(
            {p["symbol"] for p in pos_rows}
            | {o["symbol"] for o in open_orders}
            | set(watched_syms)
        )
        fetched_quotes: dict[str, dict[str, Any]] = {}
        stale_detected = False
        disconnect_error: str | None = None

        for sym in active_symbols:
            try:
                inst = self._resolve_instrument(mkt, sym)
                q = (quotes_by_symbol or {}).get(sym) or self._fetch_live_quote(mkt, inst)
                q_ts = parse_timestamp(str(q["timestamp"]))
                age = (now - q_ts).total_seconds()
                if age > cfg.max_quote_age_seconds:
                    stale_detected = True
                else:
                    fetched_quotes[sym] = q
            except Exception as exc:
                disconnect_error = str(exc)[:180]

        if curr_state == "RUNNING":
            if disconnect_error and not fetched_quotes:
                with self._lock, self._connect() as conn:
                    conn.execute(
                        "UPDATE live_paper_accounts SET session_state = 'DISCONNECTED_PAUSED', state_reason = ?, updated_at = ? WHERE market = ?",
                        (f"Live feed unavailable: {disconnect_error}", now.isoformat(), mkt),
                    )
                    self._append_audit_conn(conn, mkt, "PAPER_SESSION_DISCONNECTED", {"detail": disconnect_error})
                return self.market_snapshot(mkt)
            if stale_detected and not fetched_quotes:
                with self._lock, self._connect() as conn:
                    conn.execute(
                        "UPDATE live_paper_accounts SET session_state = 'STALE_FEED_PAUSED', state_reason = 'Live quote exceeded freshness limit; new entries paused.', updated_at = ? WHERE market = ?",
                        (now.isoformat(), mkt),
                    )
                    self._append_audit_conn(conn, mkt, "PAPER_SESSION_STALE_FEED", {})
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
                        inst = self._resolve_instrument(mkt, sym)
                        self._try_fill_order_conn(conn, str(ord_row["client_order_id"]), inst, q, now)

        triggered_orders: list[str] = []
        for sym, qty_to_close, q, trig_reason in protective_triggers:
            if sess["is_open"]:
                res_close = self.close_position(mkt, sym, quote_override=q, reason=trig_reason)
                if res_close.get("order"):
                    triggered_orders.append(str(res_close["order"]["client_order_id"]))

        # Evaluate completed-bar strategy decisions if session is RUNNING and strategy != manual_only
        resolved_bars_map = bars_by_symbol
        if curr_state == "RUNNING" and sess["is_open"] and strategy != "manual_only" and resolved_bars_map is None:
            bar_fetcher = self.nse_bars_provider if mkt == "NSE" else self.nasdaq_bars_provider
            if bar_fetcher is not None:
                auto_bars: dict[str, list[Candle]] = {}
                for sym in watched_syms or sorted(fetched_quotes.keys()):
                    try:
                        fetched_bars = bar_fetcher(sym, timeframe)
                        if fetched_bars:
                            auto_bars[sym] = fetched_bars
                    except Exception as exc:
                        _ = exc
                resolved_bars_map = auto_bars

        if curr_state == "RUNNING" and sess["is_open"] and strategy != "manual_only" and resolved_bars_map:
            if strategy.strip().lower() == "parallel_ensemble" and self.ensemble_service is not None:
                ens_cycle = self.ensemble_service.evaluate_watchlist_cycle(
                    mkt,
                    symbols=sorted(resolved_bars_map.keys()),
                    timeframe=timeframe,
                    bars_override=resolved_bars_map,
                    quotes_override=fetched_quotes,
                    session_open_override=bool(sess["is_open"]),
                    execute_in_paper=True,
                    now_utc=now,
                )
                for dec_item in ens_cycle.get("decisions") or []:
                    if dec_item.get("idempotent_duplicate_skipped"):
                        continue
                    act_lbl = str(dec_item.get("final_action") or "NO_TRADE")
                    reasons_list = dec_item.get("no_trade_reasons") or []
                    reason_lbl = (
                        f"Ensemble {act_lbl} {dec_item.get('selected_qty', 0)}u (conf={(dec_item.get('calibration') or {}).get('calibrated_confidence')})"
                        if act_lbl in {"BUY", "SELL"}
                        else f"NO_TRADE: {'; '.join(reasons_list[:2]) or 'Thresholds not met'}"
                    )
                    with self._lock, self._connect() as conn:
                        conn.execute(
                            """
                            INSERT INTO live_paper_decisions
                            (decision_id, market, symbol, strategy, timeframe, action, reason, bar_timestamp, order_client_id, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(dec_item.get("decision_id") or f"dec-{uuid.uuid4().hex[:12]}"),
                                mkt,
                                str(dec_item["symbol"]),
                                "parallel_ensemble",
                                timeframe,
                                act_lbl,
                                reason_lbl,
                                str(dec_item["bar_timestamp"]),
                                dec_item.get("paper_order_id"),
                                now.isoformat(),
                            ),
                        )
                snap = self.market_snapshot(mkt)
                return {**snap, "triggered_orders": triggered_orders, "ensemble_cycle": ens_cycle}

            for sym, bars in resolved_bars_map.items():
                if not bars:
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

                k_fc: dict[str, Any] | None = None
                if strategy.strip().lower() in {"kronos_ensemble", "kronos_informed"} and self.kronos_provider is not None:
                    try:
                        k_fc = self.kronos_provider.predict(
                            market=mkt,
                            symbol=sym,
                            timeframe=timeframe,
                            bars=bars,
                            require_fresh=False,
                        )
                    except Exception as exc:
                        k_fc = None
                        _ = exc

                decision = evaluate_bar_strategy_signal(strategy, bars, kronos_forecast=k_fc)
                order_cid: str | None = None
                q = fetched_quotes.get(sym)
                if decision["action"] == "BUY" and q:
                    inst = self._resolve_instrument(mkt, sym)
                    lot = int(inst.get("quantity_step") or inst.get("lot_size") or 1)
                    sub = self.submit_order(
                        market=mkt,
                        symbol=sym,
                        side="BUY",
                        qty=float(lot),
                        order_type="MARKET",
                        product=str(inst.get("product") or ("D" if mkt == "NSE" else "CASH")),
                        quote_override=q,
                    )
                    if sub.get("ok") and sub.get("order"):
                        order_cid = str(sub["order"]["client_order_id"])
                elif decision["action"] == "SELL" and q:
                    with self._lock, self._connect() as conn:
                        held = conn.execute(
                            "SELECT qty FROM live_paper_positions WHERE market = ? AND symbol = ?", (mkt, sym)
                        ).fetchone()
                    if held and float(held["qty"]) > 0:
                        closed = self.close_position(mkt, sym, quote_override=q, reason=f"strategy:{strategy}")
                        if closed.get("ok") and closed.get("order"):
                            order_cid = str(closed["order"]["client_order_id"])

                with self._lock, self._connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO live_paper_decisions
                        (decision_id, market, symbol, strategy, timeframe, action, reason, bar_timestamp, order_client_id, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"dec-{uuid.uuid4().hex[:12]}",
                            mkt,
                            sym,
                            strategy,
                            timeframe,
                            decision["action"],
                            decision["reason"],
                            decision.get("bar_ts") or latest_bar_iso,
                            order_cid,
                            now.isoformat(),
                        ),
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

    def emergency_stop(self, reason: str = "operator emergency stop") -> dict[str, Any]:
        """Halt all new paper entries and cancel unfilled working entry orders while keeping position SL/TP."""
        now_iso = _utc_now(self._clock).isoformat()
        canceled_by_market: dict[str, int] = {}
        preserved_positions: dict[str, int] = {}
        with self._lock, self._connect() as conn:
            for mkt in MARKETS:
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
                        "canceled_working_orders": cur.rowcount,
                        "positions_with_preserved_sl_tp": int(pos_cnt),
                    },
                )
        return {
            "ok": True,
            "simulated": True,
            "canceled_working_orders": canceled_by_market,
            "positions_with_preserved_sl_tp": preserved_positions,
        }

    def reset_emergency_stop(self) -> dict[str, Any]:
        now_iso = _utc_now(self._clock).isoformat()
        with self._lock, self._connect() as conn:
            for mkt in MARKETS:
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
                self._append_audit_conn(conn, mkt, "PAPER_EMERGENCY_STOP_CLEARED", {})
        return {"ok": True, "nse": self.market_snapshot("NSE"), "nasdaq": self.market_snapshot("NASDAQ")}

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
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM live_paper_orders WHERE market = ? ORDER BY created_at DESC, rowid DESC LIMIT 100", (mkt,)
                )
            ]
            fills = [
                {**dict(r), "fee_breakdown": json.loads(r["fees_json"] or "{}")}
                for r in conn.execute(
                    "SELECT * FROM live_paper_fills WHERE market = ? ORDER BY created_at DESC, rowid DESC LIMIT 100", (mkt,)
                )
            ]
            decisions = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM live_paper_decisions WHERE market = ? ORDER BY created_at DESC LIMIT 50", (mkt,)
                )
            ]

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
                "market_value": mv,
                "unrealized_pnl": upnl,
                "unrealized_pnl_pct": upnl_pct,
            })

        cash = round(float(acct["cash"]), 2)
        equity = round(cash + market_value, 2)
        realized = round(float(acct["realized_pnl"]), 2)
        day_pnl = round(equity - float(acct["day_start_equity"]), 2)
        total_fees = round(sum(float(f["fees_total"]) for f in fills), 2)

        return {
            "market": mkt,
            "currency": acct["currency"],
            "simulated": True,
            "real_order_api_used": False,
            "execution_mode": "LIVE_MARKET_PAPER",
            "mode_banner": f"SIMULATED LOCAL PAPER ({mkt} · {acct['currency']}) — REAL MARKET DATA · ZERO BROKER ORDERS",
            "session_state": acct["session_state"],
            "state_reason": acct["state_reason"],
            "session": {
                "state": acct["session_state"],
                "reason": acct["state_reason"],
                "strategy": acct["strategy"],
                "timeframe": acct["timeframe"],
            },
            "exchange_session": sess,
            "strategy": acct["strategy"],
            "timeframe": acct["timeframe"],
            "watched_symbols": json.loads(acct["symbols_json"]),
            "account": {
                "currency": acct["currency"],
                "starting_cash": self.configs[mkt].starting_cash,
                "cash": cash,
                "market_value": round(market_value, 2),
                "equity": equity,
                "realized_pnl": realized,
                "unrealized_pnl": round(unrealized_pnl, 2),
                "day_pnl": day_pnl,
                "total_fees_paid": total_fees,
            },
            "risk_limits": asdict(self.configs[mkt]),
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
