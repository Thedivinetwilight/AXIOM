"""Current Upstox read-only, instrument and explicitly permitted LIVE adapters.

Documentation checked 2026-10-25:
- profile: GET /v2/user/profile
- funds: GET /v3/user/get-funds-and-margin
- positions: GET /v2/portfolio/short-term-positions
- margin calculation: POST /v2/charges/margin (not an order)
- protected entries: multi-leg GTT /v3/order/gtt/place
- protection modification/cancel: /v3/order/gtt/modify|cancel
- registered static IPs: GET /v2/user/ip

No write is retried.  Every write requires an in-memory ExecutionPermit issued
by the activated daily session.  A response is only an acknowledgement until
broker reconciliation confirms the actual GTT rules/positions/orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
import gzip
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import threading
import time
from typing import Any, Callable
from urllib.parse import quote as url_quote, urlsplit

import httpx

from app.autonomy.models import (
    AccountProfile, AccountSnapshot, ExecutionPermit, Instrument, Position,
    ProtectedOrder, Submission, validate_order_route,
)
from app.autonomy.risk import floor_tick, ceil_tick

OFFICIAL_API_BASE = "https://api.upstox.com"
API_BASE = OFFICIAL_API_BASE
IST = timezone(timedelta(hours=5, minutes=30))


def validate_official_upstox_request(path: str) -> None:
    """Reject forged bases/absolute URLs before credentials or network are used."""
    try:
        base = urlsplit(API_BASE)
        relative = urlsplit(str(path))
        base_port = base.port
    except (TypeError, ValueError):
        raise ValueError("Official Upstox API endpoint and relative path are required") from None
    if (API_BASE != OFFICIAL_API_BASE or base.scheme != "https"
            or base.hostname != "api.upstox.com" or base_port not in {None, 443}
            or base.username or base.password or base.path or base.query or base.fragment):
        raise ValueError("Upstox order/history requests require the official https://api.upstox.com base")
    if (relative.scheme or relative.netloc or relative.query or relative.fragment
            or not relative.path.startswith("/") or not relative.path.startswith("/v")):
        raise ValueError("Upstox order/history requests require a relative API path beginning /v")


class UpstoxError(RuntimeError):
    pass


class UpstoxAuthError(UpstoxError):
    pass


class UpstoxRejected(UpstoxError):
    pass


class AmbiguousSubmission(UpstoxError):
    pass


class ReconciliationFailure(UpstoxError):
    pass


@dataclass(frozen=True)
class Quote:
    instrument_key: str
    bid: float
    ask: float
    last: float
    volume: float
    timestamp: datetime


class UpstoxReadOnly:
    """Token-scoped GET client.  This class has no order mutation methods."""

    def __init__(self, token_provider: Callable[[], str], *,
                 transport: httpx.BaseTransport | None = None,
                 timeout: float = 20.0, clock: Callable[[], datetime] | None = None):
        self._token_provider = token_provider
        self._transport = transport
        self._timeout = timeout
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _headers(self) -> dict[str, str]:
        token = self._token_provider().strip()
        if not token:
            raise UpstoxAuthError("Daily Upstox authorization is missing or expired")
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json_body: dict | None = None, write: bool = False,
                 not_found_ok: bool = False, extra_headers: dict | None = None) -> dict:
        validate_official_upstox_request(path)
        try:
            with httpx.Client(base_url=API_BASE, transport=self._transport,
                              timeout=self._timeout) as client:
                response = client.request(method, path, params=params, json=json_body,
                                          headers=self._headers() | (extra_headers or {}))
        except httpx.HTTPError as exc:
            if write:
                raise AmbiguousSubmission(
                    "Upstox write outcome is unknown; reconcile before any further action"
                ) from None
            raise ReconciliationFailure("Upstox read/reconciliation endpoint is unreachable") from None
        if response.status_code in {401, 403}:
            raise UpstoxAuthError("Upstox rejected or expired the daily credential")
        if not_found_ok and response.status_code == 404:
            return {"status": "not_found", "data": None}
        if response.status_code == 429:
            if write:
                raise UpstoxRejected("Upstox rate limit rejected the write; reconcile and stop")
            raise ReconciliationFailure("Upstox read rate limit reached")
        if response.status_code >= 400:
            if write:
                raise UpstoxRejected(f"Upstox rejected the write with HTTP {response.status_code}")
            raise ReconciliationFailure(f"Upstox read failed with HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            if write:
                raise AmbiguousSubmission("Upstox write returned an unreadable outcome") from None
            raise ReconciliationFailure("Upstox returned a non-JSON response") from None
        if not isinstance(payload, dict) or payload.get("status") != "success":
            if write:
                raise UpstoxRejected("Upstox did not acknowledge a successful write")
            raise ReconciliationFailure("Upstox response did not report success")
        return payload

    def profile(self, *, include_static_ips: bool = False) -> AccountProfile:
        data = self._request("GET", "/v2/user/profile")["data"]
        if not isinstance(data, dict):
            raise ReconciliationFailure("Upstox profile shape is invalid")
        user = str(data.get("user_id") or "")
        fingerprint = hashlib.sha256(user.encode()).hexdigest()[:16] if user else ""
        static_ips: frozenset[str] = frozenset()
        if include_static_ips:
            ip_data = self._request("GET", "/v2/user/ip")["data"]
            values = [ip_data.get("primary_ip"), ip_data.get("secondary_ip")]
            parsed = set()
            for value in values:
                if value:
                    try:
                        parsed.add(str(ipaddress.ip_address(str(value))))
                    except ValueError:
                        raise ReconciliationFailure("Upstox returned an invalid registered IP") from None
            static_ips = frozenset(parsed)
        return AccountProfile(
            active=data.get("is_active") is True,
            exchanges=frozenset(map(str, data.get("exchanges") or [])),
            products=frozenset(map(str, data.get("products") or [])),
            order_types=frozenset(map(str, data.get("order_types") or [])),
            user_fingerprint=fingerprint,
            static_ips=static_ips,
        )

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        if isinstance(value, (int, float)) and value > 0:
            seconds = value / 1000 if value > 1e11 else value
            # Upstox timestamps can be milliseconds or microseconds.
            if seconds > 1e11:
                seconds /= 1000
            return datetime.fromtimestamp(seconds, timezone.utc)
        if isinstance(value, str) and value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=IST)
            return parsed.astimezone(timezone.utc)
        raise ReconciliationFailure("Fresh execution quote timestamp is missing")

    def quote(self, instrument_key: str) -> Quote:
        payload = self._request("GET", "/v2/market-quote/full",
                                params={"instrument_key": instrument_key})
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ReconciliationFailure("Upstox full-quote shape is invalid")
        raw = data.get(instrument_key)
        if raw is None and len(data) == 1:
            raw = next(iter(data.values()))
        if not isinstance(raw, dict):
            raise ReconciliationFailure("Exact instrument quote was not returned")
        depth = raw.get("depth") or {}
        buy, sell = depth.get("buy") or [], depth.get("sell") or []
        if not buy or not sell:
            raise ReconciliationFailure("Executable bid/ask depth is unavailable")
        try:
            bid = float(buy[0]["price"])
            ask = float(sell[0]["price"])
            last = float(raw["last_price"])
            volume = float(raw.get("volume") or 0)
            stamp = self._timestamp(raw.get("timestamp") or raw.get("last_trade_time"))
        except (KeyError, TypeError, ValueError):
            raise ReconciliationFailure("Full quote has invalid price/depth fields") from None
        if not all(math.isfinite(v) and v > 0 for v in (bid, ask, last)) or ask < bid \
                or volume < 0:
            raise ReconciliationFailure("Full quote failed validation")
        return Quote(instrument_key, bid, ask, last, volume, stamp)

    def market_timings(self, trading_day: str) -> dict[str, tuple[datetime, datetime]]:
        payload = self._request("GET", f"/v2/market/timings/{trading_day}")
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise ReconciliationFailure("Exchange session timings are unavailable")
        result = {}
        for raw in data:
            try:
                exchange = str(raw["exchange"])
                start = self._timestamp(raw["start_time"])
                end = self._timestamp(raw["end_time"])
            except (KeyError, TypeError, ValueError):
                raise ReconciliationFailure("Exchange session timing is incomplete") from None
            if end <= start:
                raise ReconciliationFailure("Exchange session timing is invalid")
            result[exchange] = (start, end)
        return result

    def intraday_candles(self, instrument_key: str, display_symbol: str,
                         timeframe: str) -> list:
        from app.data.models import Candle, TIMEFRAMES, parse_timestamp
        if timeframe not in TIMEFRAMES or timeframe == "1D":
            raise ValueError("Unsupported intraday timeframe")
        minutes = TIMEFRAMES[timeframe] // 60
        path = (f"/v3/historical-candle/intraday/{url_quote(instrument_key, safe='')}"
                f"/minutes/{minutes}")
        payload = self._request("GET", path)
        rows = (payload.get("data") or {}).get("candles")
        if not isinstance(rows, list):
            raise ReconciliationFailure("Intraday candle response is incomplete")
        now = self._clock().astimezone(timezone.utc)
        parsed_bars = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                raise ReconciliationFailure("Intraday candle row is malformed")
            try:
                timestamp = row[0]
                if timestamp is None or timestamp == "":
                    raise ValueError("missing candle timestamp")
                parsed_timestamp = parse_timestamp(timestamp)
            except (KeyError, TypeError, ValueError):
                raise ReconciliationFailure(
                    "Intraday candle timestamp is missing or invalid"
                ) from None
            try:
                bar = Candle.from_dict({
                    "timestamp": parsed_timestamp, "open": row[1], "high": row[2], "low": row[3],
                    "close": row[4], "volume": row[5], "symbol": display_symbol,
                })
            except (KeyError, TypeError, ValueError) as exc:
                raise ReconciliationFailure("Intraday candle values are invalid") from exc
            if bar.timestamp > now + timedelta(seconds=5):
                raise ReconciliationFailure("Future candle received")
            parsed_bars.append(bar)

        # V3 examples may return newest-first rows while the engine requires
        # ascending OPEN timestamps.  Normalize only after every timestamp has
        # parsed; never deduplicate or repair provider data at this boundary.
        parsed_bars.sort(key=lambda bar: bar.timestamp)
        for previous, current in zip(parsed_bars, parsed_bars[1:]):
            if current.timestamp == previous.timestamp:
                raise ReconciliationFailure("Duplicate candle timestamp received")

        # Any provider row may still be forming, regardless of whether it was
        # first or last on the wire.  Forming rows never reach strategy,
        # forecast, stops, execution, or downstream chronological validation.
        return [
            bar for bar in parsed_bars
            if bar.available_at(timeframe) <= now
        ]


class UpstoxLiveBroker(UpstoxReadOnly):
    broker_mutations_reachable = True
    authoritative = True
    # Process-wide guard: separate sessions/adapters cannot each spend the
    # account's full order-write rate allowance independently.
    _shared_write_lock = threading.Lock()
    _shared_last_write = 0.0

    def __init__(self, token_provider: Callable[[], str], *, algo_name: str,
                 write_rate_per_second: float = 5.0,
                 transport: httpx.BaseTransport | None = None,
                 timeout: float = 20.0, clock: Callable[[], datetime] | None = None):
        super().__init__(token_provider, transport=transport, timeout=timeout, clock=clock)
        if not algo_name.strip() or any(c in algo_name for c in "\r\n"):
            raise ValueError("A configured Upstox Algo Name is required for live order writes")
        self.algo_name = algo_name.strip()
        self._minimum_write_interval = 1.0 / write_rate_per_second

    def _write(self, method: str, path: str, body: dict, permit: ExecutionPermit,
               session_id: str) -> dict:
        permit.validate(session_id, self._clock())
        with UpstoxLiveBroker._shared_write_lock:
            elapsed = time.monotonic() - UpstoxLiveBroker._shared_last_write
            if elapsed < self._minimum_write_interval:
                time.sleep(self._minimum_write_interval - elapsed)
            UpstoxLiveBroker._shared_last_write = time.monotonic()
            return self._request(method, path, json_body=body, write=True,
                                 extra_headers={"X-Algo-Name": self.algo_name,
                                                "Content-Type": "application/json"})

    @staticmethod
    def _order_payload(order: ProtectedOrder) -> dict:
        return {
            "type": "MULTIPLE", "quantity": order.quantity, "product": order.product,
            "instrument_token": order.instrument_key, "transaction_type": order.side,
            "rules": [
                {"strategy": "ENTRY", "trigger_type": "IMMEDIATE",
                 "trigger_price": order.limit_price},
                {"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE",
                 "trigger_price": order.stop_price},
                {"strategy": "TARGET", "trigger_type": "IMMEDIATE",
                 "trigger_price": order.target_price},
            ],
        }

    @staticmethod
    def _ids(payload: dict, field: str) -> tuple[str, ...]:
        data = payload.get("data") or {}
        values = data.get(field) or []
        if isinstance(values, str):
            values = [values]
        if not values or not all(isinstance(value, str) and value.startswith("GTT-") for value in values):
            raise AmbiguousSubmission("Upstox acknowledgement lacks a valid GTT id")
        return tuple(values)

    def place_protected_entry(self, order: ProtectedOrder,
                              permit: ExecutionPermit, *, session_id: str,
                              instrument: Instrument) -> Submission:
        validate_order_route(order, instrument)
        payload = self._write("POST", "/v3/order/gtt/place", self._order_payload(order),
                              permit, session_id)
        ids = self._ids(payload, "gtt_order_ids")
        return Submission(ids, "ACKNOWLEDGED_UNRECONCILED")

    def get_gtt(self, identifier: str) -> dict | None:
        payload = self._request("GET", "/v3/order/gtt",
                                params={"gtt_order_id": identifier}, not_found_ok=True)
        data = payload.get("data")
        if data is None:
            return None
        records = data if isinstance(data, list) else [data]
        exact = [record for record in records if record.get("gtt_order_id") == identifier]
        if len(exact) != 1:
            raise ReconciliationFailure("GTT reconciliation did not return one exact record")
        return exact[0]

    def list_gtt(self) -> list[dict]:
        data = self._request("GET", "/v3/order/gtt").get("data")
        if not isinstance(data, list):
            raise ReconciliationFailure("GTT list shape is invalid")
        return data

    def find_matching_gtt(self, order: ProtectedOrder) -> tuple[str, ...]:
        """Locate a possibly acknowledged order after ambiguity; never submit again."""
        expected = self._order_payload(order)
        matches = []
        for record in self.list_gtt():
            if record.get("instrument_token") != order.instrument_key \
                    or record.get("quantity") != order.quantity \
                    or record.get("product") != order.product:
                continue
            rules = {rule.get("strategy"): rule for rule in record.get("rules") or []}
            if set(rules) != {"ENTRY", "STOPLOSS", "TARGET"}:
                continue
            if all(math.isclose(float(rules[name]["trigger_price"]), float(expected_rule["trigger_price"]),
                                rel_tol=0, abs_tol=1e-9)
                   for name, expected_rule in zip(("ENTRY", "STOPLOSS", "TARGET"), expected["rules"])):
                matches.append(str(record.get("gtt_order_id") or ""))
        return tuple(matches)

    def modify_stop(self, identifier: str, new_stop: float,
                    permit: ExecutionPermit, *, session_id: str,
                    instrument: Instrument) -> Submission:
        # Confirm the old record first.  Never cancel/recreate protection and
        # never send a loosening modification.  The target is rebuilt from the
        # current broker record; a derived value could silently change payoff.
        record = self.get_gtt(identifier)
        if record is None:
            raise ReconciliationFailure("Existing protection cannot be confirmed")
        by_strategy = {rule.get("strategy"): rule for rule in record.get("rules") or []}
        if set(by_strategy) != {"ENTRY", "STOPLOSS", "TARGET"}:
            raise ReconciliationFailure("Existing GTT protection rules are incomplete")
        entry = by_strategy["ENTRY"]
        target = by_strategy["TARGET"]
        stop = by_strategy["STOPLOSS"]
        entry_side = entry.get("transaction_type")
        if entry_side not in {"BUY", "SELL"}:
            raise ReconciliationFailure("GTT entry side is missing")
        old = float(stop["trigger_price"])
        if not math.isfinite(new_stop) or new_stop <= 0:
            raise UpstoxRejected("Protective stop is not a finite positive price")
        if (entry_side == "BUY" and new_stop <= old) or (entry_side == "SELL" and new_stop >= old):
            raise UpstoxRejected("Refusing to loosen or repeat a protective stop")
        # Tick validation is deliberately performed before any write.
        aligned = floor_tick(new_stop, instrument.tick_size) if entry_side == "BUY" \
            else ceil_tick(new_stop, instrument.tick_size)
        if not math.isclose(aligned, new_stop, rel_tol=0, abs_tol=instrument.tick_size * 1e-7):
            raise UpstoxRejected("Protective stop is not aligned to the contract tick")
        rules = [
            {"strategy": "ENTRY", "trigger_type": entry.get("trigger_type"),
             "trigger_price": entry.get("trigger_price")},
            {"strategy": "STOPLOSS", "trigger_type": stop.get("trigger_type") or "IMMEDIATE",
             "trigger_price": new_stop},
            {"strategy": "TARGET", "trigger_type": target.get("trigger_type"),
             "trigger_price": target.get("trigger_price")},
        ]
        if any(rule["trigger_price"] is None or rule["trigger_type"] is None for rule in rules):
            raise ReconciliationFailure("GTT rules have incomplete trigger metadata")
        if rules[1]["trigger_type"] != "IMMEDIATE" or rules[2]["trigger_type"] != "IMMEDIATE":
            raise ReconciliationFailure("GTT protection legs are not IMMEDIATE as required")
        body = {"type": "MULTIPLE", "quantity": record["quantity"],
                "gtt_order_id": identifier, "rules": rules}
        payload = self._write("PUT", "/v3/order/gtt/modify", body, permit, session_id)
        return Submission(self._ids(payload, "gtt_order_ids"), "MODIFIED_UNRECONCILED")

    def cancel_gtt(self, identifier: str, permit: ExecutionPermit, *,
                   session_id: str) -> Submission:
        record = self.get_gtt(identifier)
        if record is None:
            raise ReconciliationFailure("Cannot confirm that the GTT is safe to cancel")
        entry = next((rule for rule in record.get("rules") or []
                      if rule.get("strategy") == "ENTRY"), None)
        if entry is None or entry.get("status") not in {"SCHEDULED", "PENDING", "OPEN"}:
            raise UpstoxRejected("Refusing to cancel triggered/filled protection")
        positions = self._positions()
        if any(p.instrument_key == record.get("instrument_token") and p.quantity
               for p in positions):
            raise UpstoxRejected("Refusing to remove protection from an open position")
        payload = self._write("DELETE", "/v3/order/gtt/cancel",
                              {"gtt_order_id": identifier}, permit, session_id)
        return Submission(self._ids(payload, "gtt_order_ids"), "CANCELLED_UNRECONCILED")

    def margin_for(self, order: ProtectedOrder) -> float:
        payload = self._request("POST", "/v2/charges/margin", json_body={"instruments": [{
            "instrument_key": order.instrument_key, "quantity": order.quantity,
            "transaction_type": order.side, "product": order.product,
            "price": order.margin_price if order.margin_price is not None else order.limit_price,
        }]})
        try:
            margin = float(payload["data"]["final_margin"])
        except (KeyError, TypeError, ValueError):
            raise ReconciliationFailure("Upstox margin response is incomplete") from None
        if not math.isfinite(margin) or margin <= 0:
            raise ReconciliationFailure("Upstox margin is invalid for this exact route")
        return margin

    def _positions(self) -> tuple[Position, ...]:
        data = self._request("GET", "/v2/portfolio/short-term-positions")["data"]
        return self._positions_from_raw(data)

    def account(self) -> AccountSnapshot:
        funds_payload, positions_payload = (
            self._request("GET", "/v3/user/get-funds-and-margin",
                          extra_headers={"Api-Version": "3.0"}),
            self._request("GET", "/v2/portfolio/short-term-positions"),
        )
        try:
            funds = funds_payload["data"]["available_to_trade"]
            available = float(funds["total"])
            cash_used = float(funds["cash_available_to_trade"]["margin_used"]["total"])
            pledge_used = float(funds["pledge_available_to_trade"]["margin_used"]["total"])
            raw_positions = positions_payload["data"]
            day_pnl = sum(float(raw.get("pnl") or 0) for raw in raw_positions)
        except (KeyError, TypeError, ValueError):
            raise ReconciliationFailure("Funds/position account response is incomplete") from None
        positions = self._positions_from_raw(raw_positions)
        used = cash_used + pledge_used
        equity = available + used
        snapshot = AccountSnapshot(equity, available, used, day_pnl, positions,
                                   self._clock(), True)
        snapshot.validate()
        return snapshot

    @staticmethod
    def _positions_from_raw(data: Any) -> tuple[Position, ...]:
        if not isinstance(data, list):
            raise ReconciliationFailure("Position response is invalid")
        result = []
        for raw in data:
            try:
                quantity = float(raw.get("quantity") or 0)
                if not quantity:
                    continue
                average_raw = raw.get("average_price")
                if average_raw is None:
                    average_raw = ((raw.get("buy_value") if quantity > 0 else raw.get("sell_value"))
                                   / abs(quantity))
                position = Position(
                    instrument_key=str(raw["instrument_token"]), quantity=quantity,
                    average_price=float(average_raw), last_price=float(raw["last_price"]),
                    multiplier=float(raw.get("multiplier") or 1.0),
                    realised=float(raw.get("realised") or 0),
                    unrealised=float(raw.get("unrealised") or 0),
                )
                if position.average_price <= 0 or position.last_price <= 0 \
                        or position.multiplier <= 0 or not all(math.isfinite(v) for v in (
                            position.average_price, position.last_price, position.multiplier,
                            position.realised, position.unrealised)):
                    raise ValueError("Invalid position economics")
            except (KeyError, TypeError, ValueError):
                raise ReconciliationFailure("Position fields are invalid") from None
            result.append(position)
        return tuple(result)

    def orders(self) -> list[dict]:
        data = self._request("GET", "/v2/order/retrieve-all")["data"]
        if not isinstance(data, list):
            raise ReconciliationFailure("Order book response is invalid")
        return data

    def order_history(self, order_id: str) -> list[dict]:
        data = self._request("GET", "/v2/order/history", params={"order_id": order_id})["data"]
        if not isinstance(data, list) or not data:
            raise ReconciliationFailure("Order history is missing")
        return data


class UpstoxRecoveryReadOnly(UpstoxReadOnly):
    """Account/GTT reads for recovered sessions; no order-write methods exist."""
    broker_mutations_reachable = False
    authoritative = True
    _order_payload = staticmethod(UpstoxLiveBroker._order_payload)
    _positions_from_raw = staticmethod(UpstoxLiveBroker._positions_from_raw)
    get_gtt = UpstoxLiveBroker.get_gtt
    list_gtt = UpstoxLiveBroker.list_gtt
    find_matching_gtt = UpstoxLiveBroker.find_matching_gtt
    _positions = UpstoxLiveBroker._positions
    account = UpstoxLiveBroker.account
    orders = UpstoxLiveBroker.orders
    order_history = UpstoxLiveBroker.order_history


class InstrumentCatalog:
    """Daily BOD catalog restricted to equities and exchange currency F&O."""

    def __init__(self, settings, cache_path: Path, *,
                 transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.settings = settings
        self.cache_path = cache_path
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.instruments: dict[str, Instrument] = {}
        self.refreshed_at: datetime | None = None
        self.source_sha256 = ""
        self.warnings: list[str] = []
        self.stats: dict[str, Any] = {}

    def _download(self, url: str) -> tuple[list[dict], str]:
        try:
            with httpx.Client(transport=self.transport, timeout=45.0,
                              follow_redirects=False) as client:
                response = client.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError:
            raise UpstoxError("Instrument catalog download failed") from None
        if response.status_code != 200:
            raise UpstoxError(f"Instrument catalog returned HTTP {response.status_code}")
        digest = hashlib.sha256(response.content).hexdigest()
        try:
            raw = gzip.decompress(response.content) if response.content[:2] == b"\x1f\x8b" else response.content
            data = json.loads(raw)
        except (OSError, ValueError):
            raise UpstoxError("Instrument catalog is not valid JSON/gzip") from None
        if not isinstance(data, list):
            raise UpstoxError("Instrument catalog root must be a list")
        return data, digest

    @staticmethod
    def _expiry(value: Any) -> datetime | None:
        if value in (None, "", 0):
            return None
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(seconds, timezone.utc)
        text = str(value)
        if len(text) == 10:
            day = datetime.fromisoformat(text).date()
            return datetime.combine(day, dt_time(23, 59, 59), IST).astimezone(timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed).astimezone(timezone.utc)

    def refresh(self) -> dict:
        complete, digest = self._download(self.settings.catalog_url)
        suspended, _ = self._download(self.settings.suspended_url)
        mis_nse, _ = self._download(self.settings.mis_nse_url)
        mis_bse, _ = self._download(self.settings.mis_bse_url)
        suspended_keys = {str(raw.get("instrument_key")) for raw in suspended}
        mis_keys = {str(raw.get("instrument_key")) for raw in mis_nse + mis_bse}
        parsed: dict[str, Instrument] = {}
        warnings: list[str] = []
        stats: dict[str, Any] = {
            "source_rows": len(complete),
            "suspended_keys": len(suspended_keys),
            "kept_by_segment": {},
            "dropped_suspended": 0,
            "dropped_unsupported": 0,
            "dropped_invalid": 0,
            "dropped_invalid_samples": [],
        }
        for raw in complete:
            try:
                key = str(raw["instrument_key"])
                segment = str(raw["segment"])
                kind = str(raw.get("instrument_type") or "").strip()
                if key in suspended_keys:
                    stats["dropped_suspended"] += 1
                    continue
                # The live cash rows occasionally carry a blank series column
                # (production loaders match NSE_EQ rows as "EQ" or ""); a blank
                # series on an equity segment is a cash equity. Derivatives must
                # still name their exact contract type (FUT/CE/PE).
                if segment in {"NSE_EQ", "BSE_EQ"} and kind == "":
                    kind = "EQ"
                supported = (segment in {"NSE_EQ", "BSE_EQ"} and kind == "EQ") or (
                    segment in {"NCD_FO", "BCD_FO"} and kind in {"FUT", "CE", "PE"}
                )
                if not supported:
                    stats["dropped_unsupported"] += 1
                    continue
                # Upstox BOD tick values are paise (5.0 => ₹0.05).
                tick = float(raw["tick_size"]) / 100.0
                step = int(raw.get("minimum_lot") or raw.get("lot_size") or 0)
                freeze = int(float(raw.get("freeze_quantity") or 0))
                multiplier_raw = raw.get("qty_multiplier", raw.get("multiplier", raw.get("contract_size")))
                if segment in {"NSE_EQ", "BSE_EQ"}:
                    multiplier = float(multiplier_raw or 1.0)
                else:
                    multiplier = float(multiplier_raw) if multiplier_raw is not None else None
                # Upstox's complete.json.gz pads equity rows with an `expiry`
                # epoch value that is NOT a contract expiry — cash equities do
                # not expire. Only exchange-traded derivatives carry a real
                # contract expiry; without this, every NSE/BSE equity row is
                # silently dropped as an "expired contract" and exact keys
                # like NSE_EQ|INE002A01018 become unknown.
                if segment in {"NSE_EQ", "BSE_EQ"}:
                    expiry = None
                else:
                    expiry = self._expiry(raw.get("expiry"))
                instrument = Instrument(
                    instrument_key=key,
                    display_symbol=key.replace("|", "-"),
                    segment=segment,
                    exchange=str(raw["exchange"]),
                    trading_symbol=str(raw.get("trading_symbol") or raw.get("short_name") or ""),
                    instrument_type=kind,
                    tick_size=tick,
                    quantity_step=step,
                    freeze_quantity=freeze,
                    contract_multiplier=multiplier,
                    expiry=expiry,
                    underlying_key=str(raw.get("underlying_key") or ""),
                    underlying_symbol=str(raw.get("underlying_symbol") or ""),
                    correlation_group=str(raw.get("underlying_key") or raw.get("underlying_symbol") or ""),
                    short_route_metadata=(key in mis_keys) if segment.endswith("_EQ") else True,
                )
                # Structural checks that do not require a multiplier allow the
                # UI to show the metadata blocker without treating it tradable.
                if multiplier is not None:
                    instrument.validate(now=self.clock())
                elif instrument.currency_derivative:
                    warnings.append(f"{key}: exact qty_multiplier/contract multiplier missing")
                else:
                    raise ValueError("Equity multiplier missing")
                parsed[key] = instrument
                stats["kept_by_segment"][segment] = stats["kept_by_segment"].get(segment, 0) + 1
            except (KeyError, TypeError, ValueError, OverflowError) as error:
                # Bad/unsupported rows are not executable; keep valid exact rows.
                # Each drop is accounted so format drift is visible in the UI
                # instead of silently shrinking the catalog.
                stats["dropped_invalid"] += 1
                if len(stats["dropped_invalid_samples"]) < 5:
                    sample = raw.get("instrument_key") if isinstance(raw, dict) else "?"
                    stats["dropped_invalid_samples"].append(f"{sample}: {type(error).__name__}: {error}")
                continue
        if not parsed:
            raise UpstoxError("No valid supported instruments in the daily catalog")
        self.instruments = parsed
        self.refreshed_at = self.clock()
        self.source_sha256 = digest
        self.warnings = warnings
        self.stats = stats
        self._write_cache()
        return self.public()

    def _write_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        body = {"refreshed_at": self.refreshed_at.isoformat(), "source_sha256": self.source_sha256,
                "warnings": self.warnings, "stats": self.stats,
                "instruments": [item.public() for item in self.instruments.values()]}
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
        temporary.replace(self.cache_path)

    def load_cache(self, *, require_current_day: bool = True) -> bool:
        """Load public BOD metadata only; credentials are never cached here."""
        try:
            body = json.loads(self.cache_path.read_text(encoding="utf-8"))
            refreshed = datetime.fromisoformat(body["refreshed_at"])
            instruments = {}
            for raw in body["instruments"]:
                expiry = datetime.fromisoformat(raw["expiry"]) if raw.get("expiry") else None
                item = Instrument(
                    instrument_key=raw["instrument_key"], display_symbol=raw["display_symbol"],
                    segment=raw["segment"], exchange=raw["exchange"],
                    trading_symbol=raw["trading_symbol"], instrument_type=raw["instrument_type"],
                    tick_size=float(raw["tick_size"]), quantity_step=int(raw["quantity_step"]),
                    freeze_quantity=int(raw["freeze_quantity"]),
                    contract_multiplier=(float(raw["contract_multiplier"])
                                         if raw.get("contract_multiplier") is not None else None),
                    expiry=expiry, underlying_key=raw.get("underlying_key", ""),
                    underlying_symbol=raw.get("underlying_symbol", ""),
                    correlation_group=raw.get("correlation_group", ""),
                    short_route_metadata=bool(raw.get("short_route_metadata")),
                    product=raw.get("product", "I"),
                )
                instruments[item.instrument_key] = item
            if not instruments:
                return False
        except (OSError, KeyError, TypeError, ValueError):
            return False
        self.instruments = instruments
        self.refreshed_at = refreshed
        self.source_sha256 = str(body.get("source_sha256") or "")
        self.warnings = list(body.get("warnings") or [])
        self.stats = dict(body.get("stats") or {})
        if require_current_day and not self.fresh_for_day(self.clock().astimezone(IST).date().isoformat()):
            self.instruments = {}
            self.refreshed_at = None
            return False
        return True

    def fresh_for_day(self, trading_day: str) -> bool:
        return bool(self.refreshed_at and self.refreshed_at.astimezone(IST).date().isoformat() == trading_day)

    def public(self) -> dict:
        return {
            "refreshed_at": self.refreshed_at.isoformat() if self.refreshed_at else None,
            "source_sha256": self.source_sha256,
            "count": len(self.instruments),
            "warnings": list(self.warnings),
            "stats": dict(self.stats),
            "instruments": [item.public() for item in self.instruments.values()],
            "scope": "NSE/BSE equities and NCD/BCD exchange-listed currency futures/options; no spot forex",
        }
