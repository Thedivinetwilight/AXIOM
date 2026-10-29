"""NASDAQ / US listed equity & ETF symbol discovery, normalization, and caching.

Uses read-only Alpaca Assets metadata (via :class:`app.data.alpaca.AlpacaAssetLookup`)
when credentials are configured, persists a per-user JSON cache, and provides an
explicitly labeled cold-start reference seed (`bundled_seed_reference`) so
symbol search works offline and outside US trading hours without pretending to
be a full live sync.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable
import unicodedata

from app.data.alpaca import AlpacaAssetLookup, validate_us_symbol
from app.markets.us_calendar import USEquitiesCalendar

_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad]")
_WHITESPACE_RE = re.compile(r"\s+")
_ETF_NAME_RE = re.compile(r"\b(ETF|ETN|EXCHANGE[- ]TRADED FUND|PROSHARES|ISHARES|INVESCO QQQ|VANECK|DIREXION|WISDOMTREE|SPDR)\b", re.IGNORECASE)

US_TICKER_ALIASES: dict[str, tuple[str, ...]] = {
    "GOOGLE": ("GOOGL", "GOOG"),
    "ALPHABET": ("GOOGL", "GOOG"),
    "FACEBOOK": ("META",),
    "FB": ("META",),
    "NASDAQ100": ("QQQ",),
    "NDX": ("QQQ",),
}


def clean_us_query(value: str) -> str:
    if not value:
        return ""
    norm = unicodedata.normalize("NFKC", str(value))
    stripped = _ZERO_WIDTH_RE.sub("", norm)
    return _WHITESPACE_RE.sub(" ", stripped).strip()


def normalize_us_search_tokens(query: str) -> dict[str, Any]:
    cleaned = clean_us_query(query)
    upper = cleaned.upper()
    if not upper:
        return {"raw": query, "cleaned": "", "canonical_symbol": "", "exchange_hint": "", "aliases": ()}

    exchange_hint = ""
    symbol = upper
    if ":" in symbol:
        prefix, rest = symbol.split(":", 1)
        prefix, rest = prefix.strip(), rest.strip()
        if prefix in {"NASDAQ", "NASD", "US", "NYSE", "ARCA", "AMEX", "BATS"}:
            exchange_hint = "NASDAQ" if prefix in {"NASDAQ", "NASD"} else prefix
            symbol = rest

    for suffix in (".O", ".OQ", ".NASDAQ", ".US"):
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            symbol = symbol[: -len(suffix)].strip()
            exchange_hint = exchange_hint or "NASDAQ"
            break

    if symbol.startswith("$") and len(symbol) > 1:
        symbol = symbol[1:].strip()

    aliases = list(US_TICKER_ALIASES.get(symbol, ()))
    return {
        "raw": query,
        "cleaned": cleaned,
        "canonical_symbol": symbol,
        "exchange_hint": exchange_hint,
        "aliases": tuple(aliases),
    }


def classify_us_subclass(symbol: str, name: str) -> str:
    name_clean = clean_us_query(name)
    if _ETF_NAME_RE.search(name_clean) or symbol.upper() in {"QQQ", "TQQQ", "SQQQ", "TLT", "IEF", "SMH", "SOXX", "IBIT"}:
        return "ETF"
    if "WARRANT" in name_clean.upper():
        return "WARRANT"
    if "ADR" in name_clean.upper() or "DEPOSITARY" in name_clean.upper():
        return "ADR"
    return "COMMON_STOCK"


@dataclass(frozen=True)
class NasdaqInstrument:
    symbol: str
    name: str
    exchange: str
    asset_class: str
    asset_subclass: str
    currency: str
    status: str
    tradable: bool
    fractionable: bool
    marginable: bool
    shortable: bool
    easy_to_borrow: bool
    tick_size: float = 0.01
    subdollar_tick_size: float = 0.0001
    lot_size: int = 1
    precision_decimals: int = 2

    @property
    def instrument_key(self) -> str:
        return f"{self.exchange}|{self.symbol}"

    def public(self) -> dict[str, Any]:
        out = asdict(self)
        out["instrument_key"] = self.instrument_key
        out["display_symbol"] = self.symbol
        out["market"] = "NASDAQ"
        return out


class NasdaqCatalog:
    """Searchable NASDAQ & US listed asset catalog with cache + labeled seed fallback."""

    def __init__(
        self,
        cache_path: Path | None = None,
        *,
        calendar: USEquitiesCalendar | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.cache_path = cache_path
        self.calendar = calendar or USEquitiesCalendar()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.instruments: dict[str, NasdaqInstrument] = {}
        self.source_kind: str = "empty"
        self.refreshed_at: datetime | None = None
        self.source_sha256: str = ""
        self.stats: dict[str, Any] = {}
        if self.cache_path and self.cache_path.exists():
            self.load_cache()
        if not self.instruments:
            self.load_seed_fallback()

    def _ingest_rows(self, rows: list[dict[str, Any]], *, source_kind: str, digest: str) -> None:
        parsed: dict[str, NasdaqInstrument] = {}
        stats: dict[str, Any] = {
            "source_rows": len(rows),
            "kept_total": 0,
            "kept_by_exchange": {},
            "kept_by_subclass": {},
            "dropped_inactive": 0,
            "dropped_non_equity": 0,
            "dropped_invalid": 0,
            "duplicate_symbols": [],
        }
        for raw in rows:
            if not isinstance(raw, dict):
                stats["dropped_invalid"] += 1
                continue
            try:
                symbol_raw = clean_us_query(str(raw.get("symbol") or "")).upper()
                symbol = validate_us_symbol(symbol_raw)
            except ValueError:
                stats["dropped_invalid"] += 1
                continue
            asset_class = clean_us_query(str(raw.get("class") or raw.get("asset_class") or "us_equity")).lower()
            if asset_class != "us_equity":
                stats["dropped_non_equity"] += 1
                continue
            status = clean_us_query(str(raw.get("status") or "active")).lower()
            if status != "active":
                stats["dropped_inactive"] += 1
                continue
            exchange = clean_us_query(str(raw.get("exchange") or "NASDAQ")).upper()
            name = clean_us_query(str(raw.get("name") or symbol))
            subclass = classify_us_subclass(symbol, name)
            if symbol in parsed:
                stats["duplicate_symbols"].append(symbol)
                continue
            inst = NasdaqInstrument(
                symbol=symbol,
                name=name,
                exchange=exchange,
                asset_class="us_equity",
                asset_subclass=subclass,
                currency="USD",
                status=status,
                tradable=bool(raw.get("tradable", True)),
                fractionable=bool(raw.get("fractionable", False)),
                marginable=bool(raw.get("marginable", False)),
                shortable=bool(raw.get("shortable", False)),
                easy_to_borrow=bool(raw.get("easy_to_borrow", False)),
            )
            parsed[symbol] = inst
            stats["kept_total"] += 1
            stats["kept_by_exchange"][exchange] = stats["kept_by_exchange"].get(exchange, 0) + 1
            stats["kept_by_subclass"][subclass] = stats["kept_by_subclass"].get(subclass, 0) + 1

        if parsed:
            self.instruments = parsed
            self.source_kind = source_kind
            self.refreshed_at = self.clock() if source_kind == "authoritative_alpaca_assets" else None
            self.source_sha256 = digest
            self.stats = stats

    def load_seed_fallback(self) -> bool:
        seed_path = Path(__file__).with_name("nasdaq_seed_catalog.json")
        if not seed_path.exists():
            return False
        raw_bytes = seed_path.read_bytes()
        payload = json.loads(raw_bytes.decode("utf-8"))
        rows = list(payload.get("records") or [])
        digest = hashlib.sha256(raw_bytes).hexdigest()
        self._ingest_rows(rows, source_kind="bundled_seed_reference", digest=digest)
        return bool(self.instruments)

    def sync_from_alpaca(
        self,
        api_key: str,
        api_secret: str,
        *,
        exchange: str = "NASDAQ",
        client: Any | None = None,
    ) -> dict[str, Any]:
        lookup = AlpacaAssetLookup(api_key, api_secret, client=client)
        try:
            rows = lookup.list_assets(status="active", asset_class="us_equity", exchange=exchange)
        finally:
            lookup.close()
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        self._ingest_rows(rows, source_kind="authoritative_alpaca_assets", digest=digest)
        if self.cache_path is not None:
            self._write_cache()
        return self.reconciliation_report()

    def _write_cache(self) -> None:
        if self.cache_path is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "refreshed_at": self.refreshed_at.isoformat() if self.refreshed_at else None,
            "source_kind": self.source_kind,
            "source_sha256": self.source_sha256,
            "stats": self.stats,
            "instruments": [item.public() for item in self.instruments.values()],
        }
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
        tmp.replace(self.cache_path)

    def load_cache(self) -> bool:
        if self.cache_path is None or not self.cache_path.exists():
            return False
        try:
            body = json.loads(self.cache_path.read_text(encoding="utf-8"))
            rows = list(body.get("instruments") or [])
            if not rows:
                return False
            parsed: dict[str, NasdaqInstrument] = {}
            for raw in rows:
                inst = NasdaqInstrument(
                    symbol=str(raw["symbol"]),
                    name=str(raw["name"]),
                    exchange=str(raw.get("exchange") or "NASDAQ"),
                    asset_class=str(raw.get("asset_class") or "us_equity"),
                    asset_subclass=str(raw.get("asset_subclass") or classify_us_subclass(str(raw["symbol"]), str(raw["name"]))),
                    currency="USD",
                    status=str(raw.get("status") or "active"),
                    tradable=bool(raw.get("tradable", True)),
                    fractionable=bool(raw.get("fractionable", False)),
                    marginable=bool(raw.get("marginable", False)),
                    shortable=bool(raw.get("shortable", False)),
                    easy_to_borrow=bool(raw.get("easy_to_borrow", False)),
                )
                parsed[inst.symbol] = inst
            self.instruments = parsed
            self.source_kind = str(body.get("source_kind") or "authoritative_alpaca_assets")
            self.refreshed_at = (
                datetime.fromisoformat(body["refreshed_at"]) if body.get("refreshed_at") else None
            )
            self.source_sha256 = str(body.get("source_sha256") or "")
            self.stats = dict(body.get("stats") or {})
            return True
        except (OSError, KeyError, TypeError, ValueError):
            return False

    def get(self, symbol: str) -> NasdaqInstrument | None:
        tokens = normalize_us_search_tokens(symbol)
        return self.instruments.get(tokens["canonical_symbol"])

    def search(
        self,
        query: str = "",
        *,
        subclass: str = "",
        tradable_only: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        tokens = normalize_us_search_tokens(query)
        canonical = tokens["canonical_symbol"]
        aliases = set(tokens["aliases"])
        sub_filter = clean_us_query(subclass).upper()

        scored: list[tuple[tuple[int, str], dict[str, Any]]] = []
        for inst in self.instruments.values():
            if tradable_only and not inst.tradable:
                continue
            if sub_filter and inst.asset_subclass.upper() != sub_filter:
                continue
            pub = inst.public()
            sym_u = inst.symbol.upper()
            name_u = inst.name.upper()
            if not canonical:
                scored.append(((50, sym_u), pub))
                continue
            rank: int | None = None
            if sym_u == canonical:
                rank = 0
            elif sym_u in aliases:
                pub = dict(pub)
                pub["matched_via_alias"] = canonical
                rank = 1
            elif sym_u.startswith(canonical):
                rank = 2
            elif canonical in sym_u or canonical in name_u:
                rank = 3
            if rank is not None:
                scored.append(((rank, sym_u), pub))

        scored.sort(key=lambda pair: pair[0])
        bounded = [item for _, item in scored[: max(1, min(int(limit), 250))]]
        return {
            "market": "NASDAQ",
            "currency": "USD",
            "source_kind": self.source_kind,
            "refreshed_at": self.refreshed_at.isoformat() if self.refreshed_at else None,
            "query": query,
            "normalized_query": canonical,
            "total_catalog": len(self.instruments),
            "total_matches": len(scored),
            "returned": len(bounded),
            "items": bounded,
        }

    def reconciliation_report(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or self.clock()
        session = self.calendar.market_state(current)
        authoritative = self.source_kind == "authoritative_alpaca_assets" and self.refreshed_at is not None
        return {
            "market": "NASDAQ",
            "currency": "USD",
            "source_kind": self.source_kind,
            "authoritative_reconciled": authoritative,
            "refreshed_at": self.refreshed_at.isoformat() if self.refreshed_at else None,
            "source_sha256": self.source_sha256 or None,
            "session": session,
            "counts": {
                "total": len(self.instruments),
                "tradable": sum(1 for i in self.instruments.values() if i.tradable),
                "fractionable": sum(1 for i in self.instruments.values() if i.fractionable),
                "by_exchange": dict(self.stats.get("kept_by_exchange") or {}),
                "by_subclass": dict(self.stats.get("kept_by_subclass") or {}),
            },
            "stats": dict(self.stats),
            "honest_coverage_statement": (
                f"Synced {len(self.instruments)} active NASDAQ US equities/ETFs from Alpaca Assets metadata "
                f"at {self.refreshed_at.isoformat()}."
                if authoritative
                else (
                    f"Showing bundled NASDAQ reference seed ({len(self.instruments)} core stocks & ETFs). "
                    "Save Alpaca Market Data keys and click 'Sync NASDAQ Universe' to load the full active NASDAQ list."
                )
            ),
        }
