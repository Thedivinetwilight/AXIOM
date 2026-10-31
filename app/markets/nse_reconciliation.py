"""NSE & Upstox Beginning-of-Day (BOD) normalization, search, and coverage reconciliation.

This module implements:
1. Unicode NFKC + zero-width/BOM stripping + whitespace normalization.
2. Exchange-prefix, Yahoo/Reuters-suffix, ISIN, display-symbol, and corporate-action
   ticker alias normalization (e.g. ``NSE:RELIANCE``, ``RELIANCE.NS``, ``ZOMATO`` -> ``ETERNAL``).
3. Subclass & settlement rule classification across NSE cash equities, ETFs (``INF*`` ISINs),
   Sovereign Gold Bonds (``SG``), Gilts/Debt (``GS``/``TB``), SME lots (``SM``/``ST``),
   Trade-for-Trade series (``BE``/``BZ``/``BT``), derivatives, and watch-only indices.
4. Deterministic ranked catalog search with explicit symbol-ambiguity reporting.
5. Reproducible coverage reconciliation against the official Upstox BOD master.
"""

from __future__ import annotations

from datetime import datetime, time as dt_time, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any, Iterable
import unicodedata
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
NSE_OPEN_IST = dt_time(9, 15)
NSE_CLOSE_IST = dt_time(15, 30)

OFFICIAL_UPSTOX_BOD_SOURCES: tuple[dict[str, str], ...] = (
    {
        "name": "Upstox Complete All-Exchange BOD Master",
        "url": "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz",
        "format": "gzip-compressed JSON array",
        "refresh_schedule": "Daily ~06:00 IST prior to 09:15 IST NSE session open",
        "auth_required": "false (public HTTPS GET for instrument metadata)",
    },
    {
        "name": "Upstox NSE Exchange-Specific BOD Master",
        "url": "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
        "format": "gzip-compressed JSON array",
        "refresh_schedule": "Daily ~06:00 IST prior to 09:15 IST NSE session open",
        "auth_required": "false (public HTTPS GET for instrument metadata)",
    },
    {
        "name": "Upstox Suspended Instruments Overlay",
        "url": "https://assets.upstox.com/market-quote/instruments/exchange/suspended-instrument.json.gz",
        "format": "gzip-compressed JSON array",
        "refresh_schedule": "Daily ~06:00 IST",
        "auth_required": "false",
    },
    {
        "name": "Upstox NSE MIS Intraday Eligibility Overlay",
        "url": "https://assets.upstox.com/market-quote/instruments/exchange/NSE_MIS.json.gz",
        "format": "gzip-compressed JSON array",
        "refresh_schedule": "Daily ~06:00 IST",
        "auth_required": "false",
    },
)

UPSTOX_LICENSING_AND_TERMS: dict[str, Any] = {
    "instrument_master_access": (
        "Public daily Beginning-of-Day (BOD) reference files published by Upstox "
        "Securities Pvt. Ltd. for API client symbol mapping and contract validation."
    ),
    "market_data_access": (
        "Real-time and historical quotes/candles/WebSocket streams require an "
        "authenticated personal Upstox v2/v3 OAuth access_token issued to the "
        "account holder and expire daily (~03:30 IST)."
    ),
    "redistribution_prohibited": True,
    "terms_summary": (
        "Authorized for personal/account-holder terminal visualization, local "
        "paper simulation, and gated personal execution only. Redistribution or "
        "rebroadcasting of NSE/BSE market data feeds is prohibited."
    ),
}

# Delivery-only cash series where NSE/SEBI surveillance or instrument structure
# prohibits intraday MIS leverage (trade-for-trade, SME T2T, SGB, G-Sec/T-Bill).
DELIVERY_ONLY_NSE_SERIES: frozenset[str] = frozenset({
    "BE", "BZ", "BT", "IT", "SM_T2T", "ST", "SZ", "SG", "GS", "TB", "GB",
    "N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8", "N9", "Y1", "Y2",
})

# Curated bidirectional corporate-rename & common operator ticker aliases.
CORPORATE_SYMBOL_ALIASES: dict[str, tuple[str, ...]] = {
    "ZOMATO": ("ETERNAL",),
    "ETERNAL": ("ZOMATO",),
    "MINDTREE": ("LTIM",),
    "LTI": ("LTIM",),
    "LTIM": ("LTI", "MINDTREE"),
    "MCDOWELL-N": ("UNITDSPR",),
    "UNITDSPR": ("MCDOWELL-N",),
    "AMARAJABAT": ("ARE&M",),
    "ARE&M": ("AMARAJABAT",),
    "TATAMOTORS": ("TMPV", "TML"),
    "NIFTY": ("NIFTY 50", "NIFTY50", "NIFTYBEES"),
    "NIFTY50": ("NIFTY 50", "NIFTYBEES"),
    "BANKNIFTY": ("NIFTY BANK", "BANKBEES"),
    "INDIAVIX": ("INDIA VIX",),
    "RELIANCEIND": ("RELIANCE",),
    "HDFC": ("HDFCBANK",),
}

_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad]")
_WHITESPACE_RE = re.compile(r"\s+")
_ISIN_RE = re.compile(r"^IN[A-Z0-9]{9}[0-9]$")


def clean_nse_identity(value: str) -> str:
    """Normalize Unicode compatibility forms, strip invisible codepoints, and collapse whitespace."""
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value))
    stripped = _ZERO_WIDTH_RE.sub("", normalized)
    return _WHITESPACE_RE.sub(" ", stripped).strip()


def is_valid_indian_isin(isin: str) -> bool:
    return bool(_ISIN_RE.fullmatch(clean_nse_identity(isin).upper()))


def tick_precision_decimals(tick_size: float) -> int:
    if tick_size <= 0:
        return 2
    text = format(Decimal(str(tick_size)).normalize(), "f")
    if "." not in text:
        return 0
    return min(6, len(text.split(".", 1)[1].rstrip("0")) or 2)


def classify_nse_subclass(
    *,
    segment: str,
    instrument_type: str,
    isin: str = "",
    trading_symbol: str = "",
    name: str = "",
    security_type: str = "",
) -> str:
    seg = segment.strip().upper()
    itype = instrument_type.strip().upper()
    isin_u = isin.strip().upper()
    sym_u = trading_symbol.strip().upper()
    name_u = name.strip().upper()
    sec_u = security_type.strip().upper()

    if seg in {"NSE_INDEX", "BSE_INDEX"} or itype == "INDEX":
        return "INDEX"
    if seg in {"NCD_FO", "BCD_FO"} and itype in {"FUT", "CE", "PE"}:
        return "CURRENCY_DERIVATIVE"
    if seg in {"NSE_FO", "BSE_FO"} and itype in {"FUT", "CE", "PE"}:
        return "EQUITY_DERIVATIVE"
    if seg in {"NSE_EQ", "BSE_EQ"}:
        if (
            isin_u.startswith("INF")
            or "ETF" in sec_u
            or "EXCHANGE TRADED FUND" in name_u
            or re.search(r"\bETF\b", name_u)
            or sym_u.endswith(("BEES", "IETF"))
        ):
            return "ETF"
        if itype == "SG" or sym_u.startswith("SGB"):
            return "SOVEREIGN_GOLD_BOND"
        if itype in {"GS", "TB", "GB"} or itype.startswith(("N", "Y")) and len(itype) == 2 and itype[1].isdigit():
            return "DEBT_OR_GILT"
        if itype in {"SM", "ST", "SZ", "M", "MT"}:
            return "SME_EQUITY"
        if itype in {"BE", "BZ", "BT", "IT"}:
            return "TRADE_FOR_TRADE_EQUITY"
        return "CASH_EQUITY"
    return "OTHER"


def default_product_for_series(segment: str, instrument_type: str) -> str:
    if segment.strip().upper() in {"NSE_EQ", "BSE_EQ"}:
        if instrument_type.strip().upper() in DELIVERY_ONLY_NSE_SERIES:
            return "D"
    return "I"


def mis_intraday_allowed_for(segment: str, instrument_type: str) -> bool:
    seg = segment.strip().upper()
    if seg in {"NSE_INDEX", "BSE_INDEX"}:
        return False
    if seg in {"NSE_EQ", "BSE_EQ"} and instrument_type.strip().upper() in DELIVERY_ONLY_NSE_SERIES:
        return False
    return True


def normalize_nse_search_tokens(query: str) -> dict[str, Any]:
    """Expand an operator search query into canonical symbol, key, ISIN, and alias candidates."""
    cleaned = clean_nse_identity(query)
    upper = cleaned.upper()
    if not upper:
        return {
            "raw": query,
            "cleaned": "",
            "canonical_symbol": "",
            "key_candidate": "",
            "isin_candidate": "",
            "segment_hint": "",
            "series_hint": "",
            "aliases": (),
        }

    segment_hint = ""
    series_hint = ""
    key_candidate = ""
    symbol = upper

    # Display-symbol form NSE_EQ-INE002A01018 -> canonical key NSE_EQ|INE002A01018
    for prefix in ("NSE_EQ", "BSE_EQ", "NSE_INDEX", "BSE_INDEX", "NSE_FO", "BSE_FO", "NCD_FO", "BCD_FO"):
        if upper.startswith(f"{prefix}|"):
            key_candidate = f"{prefix}|{cleaned[len(prefix) + 1:].strip()}"
            segment_hint = prefix
            break
        if upper.startswith(f"{prefix}-"):
            key_candidate = f"{prefix}|{cleaned[len(prefix) + 1:].strip()}"
            segment_hint = prefix
            break

    # Exchange prefix forms: NSE:RELIANCE, BSE:RELIANCE, NSE-RELIANCE
    if not key_candidate and ":" in symbol:
        ex_prefix, rest = symbol.split(":", 1)
        ex_prefix = ex_prefix.strip()
        rest = rest.strip()
        if ex_prefix in {"NSE", "NSE_EQ"}:
            segment_hint = "NSE_EQ"
            symbol = rest
        elif ex_prefix in {"BSE", "BSE_EQ"}:
            segment_hint = "BSE_EQ"
            symbol = rest
        elif ex_prefix in {"NSE_INDEX", "INDEX"}:
            segment_hint = "NSE_INDEX"
            symbol = rest

    # Yahoo/Reuters/TV suffixes: RELIANCE.NS, RELIANCE.NSE, RELIANCE.BO
    for suffix, seg in ((".NS", "NSE_EQ"), (".NSE", "NSE_EQ"), (".BO", "BSE_EQ"), (".BSE", "BSE_EQ")):
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            segment_hint = segment_hint or seg
            symbol = symbol[: -len(suffix)].strip()
            break

    # Explicit series suffix: RELIANCE-EQ, IDEA-BE, NIFTYBEES-EQ
    if "-" in symbol and not key_candidate:
        base, tail = symbol.rsplit("-", 1)
        if tail in {"EQ", "BE", "BZ", "SM", "ST", "SG", "GS", "BL"} and base and base not in {"MCDOWELL", "BAJAJ"}:
            symbol = base
            series_hint = tail

    if symbol.startswith("$") and len(symbol) > 1:
        symbol = symbol[1:].strip()

    isin_candidate = symbol if is_valid_indian_isin(symbol) else ""
    if not isin_candidate and key_candidate and "|" in key_candidate:
        rhs = key_candidate.split("|", 1)[1].strip().upper()
        if is_valid_indian_isin(rhs):
            isin_candidate = rhs

    compact = symbol.replace(" ", "")
    aliases: list[str] = []
    for candidate in (symbol, compact):
        for alias in CORPORATE_SYMBOL_ALIASES.get(candidate, ()):
            if alias not in aliases:
                aliases.append(alias)

    return {
        "raw": query,
        "cleaned": cleaned,
        "canonical_symbol": symbol,
        "key_candidate": key_candidate,
        "isin_candidate": isin_candidate,
        "segment_hint": segment_hint,
        "series_hint": series_hint,
        "aliases": tuple(aliases),
    }


def nse_session_status(now: datetime | None = None) -> dict[str, Any]:
    """Return IST market session state without claiming holiday exchange authority when offline."""
    current = (now or datetime.now(timezone.utc)).astimezone(IST)
    weekday = current.weekday()
    local_time = current.time()
    is_weekend = weekday >= 5
    if is_weekend:
        state = "WEEKEND_CLOSED"
        is_open = False
    elif local_time < dt_time(9, 0):
        state = "PRE_OPEN_CLOSED"
        is_open = False
    elif local_time < NSE_OPEN_IST:
        state = "PRE_OPEN_AUCTION"
        is_open = False
    elif local_time <= NSE_CLOSE_IST:
        state = "REGULAR_OPEN"
        is_open = True
    else:
        state = "POST_CLOSE"
        is_open = False
    return {
        "exchange": "NSE",
        "timezone": "Asia/Kolkata",
        "ist_timestamp": current.isoformat(),
        "trading_day": current.date().isoformat(),
        "state": state,
        "is_open": is_open,
        "regular_hours_ist": "09:15-15:30 IST",
    }


def load_nse_seed_records() -> list[dict[str, Any]]:
    seed_path = Path(__file__).with_name("nse_seed_catalog.json")
    if not seed_path.exists():
        return []
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    return list(payload.get("records") or [])


def search_nse_catalog(
    instruments: Iterable[Any],
    *,
    indices: Iterable[dict[str, Any]] = (),
    query: str = "",
    segment: str = "",
    subclass: str = "",
    include_indices: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Deterministically rank NSE/Upstox instruments & indices with ambiguity metadata."""
    tokens = normalize_nse_search_tokens(query)
    seg_filter = clean_nse_identity(segment).upper() or tokens["segment_hint"]
    sub_filter = clean_nse_identity(subclass).upper()
    canonical = tokens["canonical_symbol"]
    key_cand = tokens["key_candidate"].upper()
    isin_cand = tokens["isin_candidate"].upper()
    series_hint = tokens["series_hint"]
    aliases = set(tokens["aliases"])

    rows: list[tuple[tuple[int, int, str, str], dict[str, Any]]] = []
    exact_symbol_hits_nse: list[str] = []
    exact_symbol_hits_all: list[str] = []

    for inst in instruments:
        pub = inst.public() if hasattr(inst, "public") else dict(inst)
        inst_seg = str(pub.get("segment") or "").upper()
        inst_sub = str(pub.get("asset_subclass") or "").upper()
        if seg_filter and inst_seg != seg_filter:
            continue
        if sub_filter and inst_sub != sub_filter:
            continue

        key_u = str(pub.get("instrument_key") or "").upper()
        disp_u = str(pub.get("display_symbol") or "").upper()
        sym_u = str(pub.get("trading_symbol") or "").upper()
        und_u = str(pub.get("underlying_symbol") or "").upper()
        name_u = str(pub.get("name") or "").upper()
        isin_u = str(pub.get("isin") or "").upper()
        itype_u = str(pub.get("instrument_type") or "").upper()

        seg_rank = 0 if inst_seg == "NSE_EQ" else (1 if inst_seg == "BSE_EQ" else 2)
        series_rank = 0 if itype_u == (series_hint or "EQ") else 1

        if not canonical and not key_cand and not isin_cand:
            rows.append(((50, seg_rank * 10 + series_rank, sym_u, key_u), pub))
            continue

        rank: int | None = None
        if key_cand and key_u == key_cand:
            rank = 0
        elif disp_u == tokens["cleaned"].upper() or key_u == tokens["cleaned"].upper():
            rank = 0
        elif isin_cand and isin_u == isin_cand:
            rank = 1
        elif sym_u == canonical:
            rank = 2
            exact_symbol_hits_all.append(pub["instrument_key"])
            if inst_seg == "NSE_EQ":
                exact_symbol_hits_nse.append(pub["instrument_key"])
        elif und_u and und_u == canonical:
            rank = 2
        elif sym_u in aliases:
            pub = dict(pub)
            pub["matched_via_alias"] = canonical
            rank = 3
        elif canonical and (sym_u.startswith(canonical) or und_u.startswith(canonical)):
            rank = 4
        elif canonical and (
            canonical in sym_u
            or canonical in und_u
            or canonical in name_u
            or canonical in isin_u
            or canonical in key_u
        ):
            rank = 5
        elif any(a in sym_u or a in name_u for a in aliases):
            pub = dict(pub)
            pub["matched_via_alias"] = canonical
            rank = 6

        if rank is not None:
            rows.append(((rank, seg_rank * 10 + series_rank, sym_u, key_u), pub))

    if include_indices:
        for idx in indices:
            idx_pub = dict(idx)
            idx_pub.setdefault("asset_category", "INDEX")
            idx_seg = str(idx_pub.get("segment") or "").upper()
            if seg_filter:
                allowed_idx_segs = {"NSE_INDEX"} if seg_filter == "NSE_EQ" else ({"BSE_INDEX"} if seg_filter == "BSE_EQ" else {seg_filter})
                if idx_seg not in allowed_idx_segs:
                    continue
            if sub_filter and sub_filter != "INDEX":
                continue
            key_u = str(idx_pub.get("instrument_key") or "").upper()
            disp_u = str(idx_pub.get("display_symbol") or "").upper()
            sym_u = str(idx_pub.get("trading_symbol") or "").upper()
            name_u = str(idx_pub.get("name") or "").upper()
            if not canonical and not key_cand:
                rows.append(((60, 30, sym_u, key_u), idx_pub))
                continue
            rank = None
            if (key_cand and key_u == key_cand) or disp_u == tokens["cleaned"].upper() or key_u == tokens["cleaned"].upper():
                rank = 0
            elif sym_u == canonical or name_u == canonical:
                rank = 2
            elif sym_u in aliases or name_u in aliases:
                idx_pub["matched_via_alias"] = canonical
                rank = 3
            elif canonical and (canonical in sym_u or canonical in name_u or canonical in key_u):
                rank = 5
            if rank is not None:
                rows.append(((rank, 30, sym_u, key_u), idx_pub))

    rows.sort(key=lambda pair: pair[0])
    bounded = [item for _, item in rows[: max(1, min(int(limit), 250))]]

    ambiguous = len(exact_symbol_hits_nse) > 1 or (
        not seg_filter and len(exact_symbol_hits_all) > 1 and len(exact_symbol_hits_nse) == 0
    )
    ambiguity_detail = None
    if len(exact_symbol_hits_nse) > 1:
        ambiguity_detail = (
            f"Multiple NSE_EQ series share trading_symbol {canonical!r}: "
            f"{', '.join(exact_symbol_hits_nse[:5])}. Verify exact ISIN / instrument_key."
        )
    elif len(exact_symbol_hits_all) > 1 and not seg_filter:
        ambiguity_detail = (
            f"Symbol {canonical!r} exists across multiple segments/exchanges "
            f"({', '.join(exact_symbol_hits_all[:5])}); NSE_EQ primary series ranked first."
        )

    return {
        "query": query,
        "normalized_query": tokens["canonical_symbol"] or tokens["key_candidate"],
        "total_matches": len(rows),
        "returned": len(bounded),
        "ambiguous": ambiguous,
        "ambiguity_detail": ambiguity_detail,
        "exact_nse_matches": exact_symbol_hits_nse,
        "items": bounded,
    }


def resolve_nse_instrument_strict(
    catalog: Any,
    symbol_or_key: str,
    *,
    allow_index: bool = False,
) -> dict[str, Any]:
    """Resolve an NSE cash equity/ETF (or watch-only NSE index when allowed) with zero key loss.

    - Exact `NSE_EQ|INE...` keys resolve directly from `catalog.instruments` (`O(1)`),
      preserving the exact ISIN/series even when multiple NSE series share `trading_symbol`.
    - Explicitly rejects `BSE_EQ|...`, derivative segments (`NCD_FO|...`, `BCD_FO|...`,
      `NSE_FO|...`, `BSE_FO|...`, `MCX_FO|...`), and watch-only `NSE_INDEX|...` (when
      `allow_index=False`) so categories are never silently substituted.
    """
    cleaned = clean_nse_identity(symbol_or_key)
    if not cleaned:
        raise ValueError("NSE instrument symbol or instrument_key cannot be empty")

    instruments_map: dict[str, Any] = getattr(catalog, "instruments", None) or {}
    indices_map: dict[str, Any] = getattr(catalog, "indices", None) or {}

    tokens = normalize_nse_search_tokens(cleaned)
    key_cand = tokens["key_candidate"]

    # Case-insensitive exact key map for O(1) exact Upstox instrument_key preservation
    if "|" in cleaned or key_cand:
        raw_probe = key_cand or cleaned
        raw_probe_upper = raw_probe.upper()
        seg_prefix = raw_probe_upper.split("|", 1)[0].strip() if "|" in raw_probe_upper else ""
        if seg_prefix == "BSE_EQ" or seg_prefix == "BSE_INDEX":
            raise ValueError(
                f"Rejected {raw_probe}: belongs to {seg_prefix} (BSE), not NSE cash equities (NSE_EQ)"
            )
        if seg_prefix in {"NCD_FO", "BCD_FO", "NSE_FO", "BSE_FO", "MCX_FO"}:
            raise ValueError(
                f"Rejected {raw_probe}: {seg_prefix} is a derivative contract, not an NSE cash equity/ETF"
            )
        if seg_prefix == "NSE_INDEX":
            for k, idx_val in indices_map.items():
                if k.upper() == raw_probe_upper:
                    if not allow_index:
                        raise ValueError(
                            f"Rejected {k}: watch-only NSE benchmark index (INDEX) is not tradable in NSE cash-equity PAPER"
                        )
                    out_idx = dict(idx_val)
                    out_idx.setdefault("asset_category", "INDEX")
                    return out_idx
            raise ValueError(f"Unknown NSE benchmark index key: {raw_probe}")
        if seg_prefix == "NSE_EQ":
            for k, inst_obj in instruments_map.items():
                if k.upper() == raw_probe_upper:
                    pub = inst_obj.public() if hasattr(inst_obj, "public") else dict(inst_obj)
                    pub.setdefault("asset_category", "CASH_EQUITY_OR_ETF")
                    return pub
            raise ValueError(f"Unknown or excluded NSE_EQ instrument_key: {raw_probe}")

    # Plain symbol or ISIN query — search strictly within NSE_EQ (and NSE_INDEX if allow_index)
    res = search_nse_catalog(
        instruments_map.values(),
        indices=indices_map.values(),
        query=cleaned,
        segment="NSE_EQ",
        include_indices=allow_index,
        limit=10,
    )
    if res["items"]:
        top = dict(res["items"][0])
        top.setdefault(
            "asset_category",
            "INDEX" if str(top.get("segment") or "").upper().endswith("INDEX") else "CASH_EQUITY_OR_ETF",
        )
        return top

    # Check if the symbol exists ONLY as an index, derivative, or BSE equity so the error is exact
    if not allow_index:
        idx_probe = search_nse_catalog(
            [],
            indices=indices_map.values(),
            query=cleaned,
            segment="NSE_EQ",
            include_indices=True,
            limit=1,
        )
        if idx_probe["items"]:
            hit = idx_probe["items"][0]
            raise ValueError(
                f"Rejected {hit['instrument_key']} ({hit['trading_symbol']}): watch-only NSE index (INDEX) is not tradable"
            )
    other_probe = search_nse_catalog(
        instruments_map.values(),
        indices=[],
        query=cleaned,
        segment="",
        include_indices=False,
        limit=1,
    )
    if other_probe["items"]:
        hit = other_probe["items"][0]
        raise ValueError(
            f"Rejected {hit['instrument_key']}: belongs to {hit.get('segment')} ({hit.get('asset_category', 'NON_NSE_EQ')}), not NSE cash equities (NSE_EQ)"
        )
    raise ValueError(f"Unknown or unsupported NSE instrument: {cleaned}")


def build_nse_reconciliation_report(catalog: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Build an auditable NSE coverage & exclusion reconciliation report."""
    stats = dict(getattr(catalog, "stats", None) or {})
    source_kind = str(getattr(catalog, "source_kind", "") or "empty")
    refreshed_at = getattr(catalog, "refreshed_at", None)
    instruments = list((getattr(catalog, "instruments", None) or {}).values())
    indices = list((getattr(catalog, "indices", None) or {}).values())

    nse_eq = [i for i in instruments if i.segment == "NSE_EQ"]
    bse_eq = [i for i in instruments if i.segment == "BSE_EQ"]
    ccy_deriv = [i for i in instruments if i.currency_derivative]
    nse_indices = [idx for idx in indices if str(idx.get("segment") or "").upper() == "NSE_INDEX"]
    bse_indices = [idx for idx in indices if str(idx.get("segment") or "").upper() == "BSE_INDEX"]

    subclass_counts: dict[str, int] = {}
    nse_series_counts: dict[str, int] = {}
    delivery_only_count = 0
    mis_eligible_count = 0
    missing_isin_keys: list[str] = []
    symbol_map: dict[str, list[str]] = {}

    for item in nse_eq:
        sub = item.asset_subclass
        subclass_counts[sub] = subclass_counts.get(sub, 0) + 1
        nse_series_counts[item.instrument_type] = nse_series_counts.get(item.instrument_type, 0) + 1
        if item.mis_intraday_allowed:
            mis_eligible_count += 1
        else:
            delivery_only_count += 1
        if not is_valid_indian_isin(item.isin):
            missing_isin_keys.append(item.instrument_key)
        symbol_map.setdefault(item.trading_symbol.upper(), []).append(item.instrument_key)

    ambiguous_nse_symbols = {
        sym: keys for sym, keys in sorted(symbol_map.items()) if sym and len(keys) > 1
    }

    authoritative_reconciled = source_kind == "authoritative_upstox_bod" and refreshed_at is not None
    session = nse_session_status(now)
    trading_day = session["trading_day"]
    fresh_today = bool(
        authoritative_reconciled
        and hasattr(catalog, "fresh_for_day")
        and catalog.fresh_for_day(trading_day)
    )

    return {
        "market": "NSE",
        "currency": "INR",
        "authoritative_sources": list(OFFICIAL_UPSTOX_BOD_SOURCES),
        "licensing_and_terms": dict(UPSTOX_LICENSING_AND_TERMS),
        "source_kind": source_kind,
        "authoritative_reconciled": authoritative_reconciled,
        "fresh_for_current_ist_day": fresh_today,
        "refreshed_at": refreshed_at.isoformat() if refreshed_at else None,
        "source_sha256": getattr(catalog, "source_sha256", "") or None,
        "session": session,
        "counts": {
            "source_rows": int(stats.get("source_rows", len(instruments) + len(indices))),
            "tradable_total": len(instruments),
            "nse_eq_tradable": len(nse_eq),
            "bse_eq_tradable": len(bse_eq),
            "currency_derivatives_tradable": len(ccy_deriv),
            "watch_only_indices": len(indices),
            "watch_only_nse_indices": len(nse_indices),
            "watch_only_bse_indices": len(bse_indices),
            "nse_eq_by_subclass": subclass_counts,
            "nse_eq_by_series": nse_series_counts,
            "nse_eq_mis_intraday_allowed": mis_eligible_count,
            "nse_eq_delivery_only": delivery_only_count,
        },
        "nse_cash_equity_reconciliation": _build_nse_eq_reconciliation_block(
            stats=stats,
            nse_eq_count=len(nse_eq),
            nse_indices_count=len(nse_indices),
            bse_eq_count=len(bse_eq),
            derivatives_count=len(ccy_deriv),
        ),
        "exclusions_and_anomalies": {
            "dropped_suspended": int(stats.get("dropped_suspended", 0)),
            "dropped_unsupported": int(stats.get("dropped_unsupported", 0)),
            "dropped_unsupported_by": dict(stats.get("dropped_unsupported_by") or {}),
            "dropped_invalid": int(stats.get("dropped_invalid", 0)),
            "dropped_invalid_reasons": dict(stats.get("dropped_invalid_reasons") or {}),
            "dropped_invalid_samples": list(stats.get("dropped_invalid_samples") or []),
            "duplicate_instrument_keys": list(stats.get("duplicate_keys") or []),
            "duplicate_instrument_key_count": len(stats.get("duplicate_keys") or []),
            "segment_field_mismatch": int(stats.get("segment_field_mismatch", 0)),
            "nse_eq_missing_or_invalid_isin_count": len(missing_isin_keys),
            "nse_eq_missing_or_invalid_isin_samples": missing_isin_keys[:10],
            "nse_eq_multi_series_symbol_count": len(ambiguous_nse_symbols),
            "nse_eq_multi_series_symbol_samples": dict(list(ambiguous_nse_symbols.items())[:10]),
        },
        "honest_coverage_statement": (
            "Reconciled against downloaded official Upstox Beginning-of-Day JSON master "
            f"(sha256={getattr(catalog, 'source_sha256', '')[:12]}... at "
            f"{refreshed_at.isoformat()}). Suspended, expired, malformed, and unsupported "
            "derivative segments are explicitly excluded above."
            if authoritative_reconciled
            else (
                "Full NSE BOD coverage is NOT YET RECONCILED in this data directory "
                f"(current catalog state: {source_kind}). Trigger 'Refresh NSE BOD Master' "
                "to download and reconcile the full daily Upstox NSE/complete JSON.gz universe."
            )
        ),
    }


def _build_nse_eq_reconciliation_block(
    *,
    stats: dict[str, Any],
    nse_eq_count: int,
    nse_indices_count: int,
    bse_eq_count: int,
    derivatives_count: int,
) -> dict[str, Any]:
    """Reconcile BOD `NSE_EQ` source rows against searchable `NSE_EQ` catalog with zero unexplained gap."""
    ex_susp = int(stats.get("nse_eq_excluded_suspended", 0))
    ex_unsup = int(stats.get("nse_eq_excluded_unsupported", 0))
    ex_inv = int(stats.get("nse_eq_excluded_invalid", 0))
    dup_keys = list(stats.get("nse_eq_duplicate_keys") or [])
    dup_cnt = len(dup_keys)
    bod_nse_eq = int(
        stats.get("nse_eq_bod_source_rows")
        or (nse_eq_count + ex_susp + ex_unsup + ex_inv + dup_cnt)
    )
    accounted = nse_eq_count + ex_susp + ex_unsup + ex_inv + dup_cnt
    gap = bod_nse_eq - accounted
    return {
        "bod_nse_eq_source_rows": bod_nse_eq,
        "searchable_nse_eq_included": nse_eq_count,
        "excluded_suspended_nse_eq": ex_susp,
        "excluded_unsupported_nse_eq": ex_unsup,
        "excluded_invalid_nse_eq": ex_inv,
        "excluded_invalid_nse_eq_reasons": dict(stats.get("nse_eq_excluded_invalid_reasons") or {}),
        "duplicate_nse_eq_keys_overwritten": dup_cnt,
        "duplicate_nse_eq_keys": dup_keys,
        "accounted_nse_eq_rows": accounted,
        "unexplained_nse_eq_row_gap": gap,
        "reconciled_exact": gap == 0,
        "category_separation": {
            "nse_cash_equities_and_etfs": nse_eq_count,
            "nse_watch_only_indices": nse_indices_count,
            "bse_cash_equities_isolated": bse_eq_count,
            "currency_derivatives_isolated": derivatives_count,
        },
    }

