"""Read-Only Live Trade Monitor Service (`11 · Live Trade Monitor`).

Aggregates actively monitored & traded NSE (INR) and NASDAQ (USD) instruments
across `LIVE_MARKET_PAPER` and separately gated `LIVE_MARKET_LIVE` journals,
with server-side filtering, pagination for large watchlists, unmistakable
`PAPER` vs `LIVE` separation, explicit `NOT TRADING` banners on inactive/scanning
sessions, and 8 visually distinct chart event markers.

Hard structural safety guarantees:
- 100% READ-ONLY: exposes zero order creation, modification, cancel, or close methods.
- Never fabricates or substitutes sample/demo candles when market data is stale,
  disconnected, or unavailable.
- Never labels a simulated paper order/fill as broker-confirmed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

MARKER_SHAPES: dict[str, str] = {
    "SIGNAL": "diamond_hollow",
    "ORDER_SUBMITTED": "square_hollow",
    "PARTIAL_FILL": "circle_half",
    "COMPLETED_FILL": "triangle_solid",
    "CANCELED": "cross_x",
    "STOP_LOSS": "shield_sl",
    "TAKE_PROFIT": "target_tp",
    "EXIT": "chevron_exit",
}


def compute_session_trading_banner(
    *,
    execution_mode: str,
    session_state: str,
    strategy: str,
    open_positions_count: int,
    working_orders_count: int,
    recent_fills_count: int = 0,
) -> dict[str, Any]:
    """Determine truthfully whether the selected session is actively trading or NOT TRADING.

    Never claims trading is active unless BOTH session state AND order/position/fill
    records confirm active execution (scanning-only sessions report NOT TRADING).
    """
    mode = execution_mode.strip().upper()
    st = (session_state or "STOPPED").strip().upper()
    strat = (strategy or "manual_only").strip().lower()
    has_confirmed_activity = (open_positions_count + working_orders_count + recent_fills_count) > 0

    if mode == "LIVE_MARKET_LIVE":
        if st == "ACTIVE" and has_confirmed_activity:
            return {
                "actively_trading": True,
                "banner_status": "TRADING ACTIVE · LIVE BROKER SESSION",
                "banner_tone": "live",
                "reason": "Live broker gate is explicitly ACTIVE with confirmed broker order/fill records.",
            }
        if st == "ACTIVE":
            return {
                "actively_trading": False,
                "banner_status": "NOT TRADING (SCANNING ONLY — NO LIVE ORDERS OR FILLS)",
                "banner_tone": "inactive",
                "reason": "Live gate is ACTIVE, but 0 broker orders, positions, or fills are recorded.",
            }
        return {
            "actively_trading": False,
            "banner_status": "NOT TRADING",
            "banner_tone": "inactive",
            "reason": f"Live execution gate is {st} (default OFF; requires Readiness + Arm + phrase-confirmed Activate).",
        }

    # LIVE_MARKET_PAPER / AXIOM_PAPER
    inactive_states = {
        "STOPPED",
        "PAUSED",
        "DISARMED",
        "DISABLED",
        "IDLE",
        "MARKET_CLOSED_WAITING",
        "STALE_FEED_PAUSED",
        "DISCONNECTED_PAUSED",
        "RECOVERED_PAUSED",
        "EMERGENCY_STOPPED",
        "END_OF_DATA",
        "HALTED",
    }
    if st in inactive_states:
        return {
            "actively_trading": False,
            "banner_status": f"NOT TRADING ({st})",
            "banner_tone": "inactive",
            "reason": f"Paper session is {st} — no automated orders are executing.",
        }
    if st == "RUNNING" and not has_confirmed_activity:
        return {
            "actively_trading": False,
            "banner_status": "NOT TRADING (SCANNING ONLY)",
            "banner_tone": "inactive",
            "reason": f"Paper session is RUNNING ({strat}), but only scanning — 0 open positions, working orders, or fills recorded.",
        }
    if st == "RUNNING" and has_confirmed_activity:
        return {
            "actively_trading": True,
            "banner_status": "TRADING ACTIVE · PAPER (SIMULATED FILLS ONLY)",
            "banner_tone": "paper",
            "reason": f"Simulated paper session RUNNING ({strat}) with confirmed local paper order/fill records; zero real broker orders.",
        }
    return {
        "actively_trading": False,
        "banner_status": "NOT TRADING",
        "banner_tone": "inactive",
        "reason": f"Session state {st!r} is not actively trading.",
    }


def _collect_paper_market_items(
    snap: dict[str, Any],
    ensemble_decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    mkt = str(snap.get("market") or "NSE").upper()
    ccy = str(snap.get("currency") or ("INR" if mkt == "NSE" else "USD"))
    sess_state = str(snap.get("session_state") or "STOPPED")
    strategy = str(snap.get("strategy") or "manual_only")
    tf = str(snap.get("timeframe") or "5m")

    positions_by_sym = {str(p["symbol"]).upper(): p for p in (snap.get("positions") or []) if float(p.get("qty", 0)) > 0}
    orders_by_sym: dict[str, list[dict[str, Any]]] = {}
    for o in snap.get("orders") or []:
        orders_by_sym.setdefault(str(o["symbol"]).upper(), []).append(o)

    fills_by_sym: dict[str, list[dict[str, Any]]] = {}
    for f in snap.get("fills") or []:
        fills_by_sym.setdefault(str(f["symbol"]).upper(), []).append(f)

    decs_by_sym: dict[str, list[dict[str, Any]]] = {}
    for d in snap.get("decisions") or []:
        decs_by_sym.setdefault(str(d["symbol"]).upper(), []).append(d)

    ens_by_sym: dict[str, list[dict[str, Any]]] = {}
    for ed in ensemble_decisions:
        if str(ed.get("market", "")).upper() == mkt:
            ens_by_sym.setdefault(str(ed["symbol"]).upper(), []).append(ed)

    watch_syms = {str(w.get("symbol", "")).upper() for w in (snap.get("watchlist") or []) if w.get("symbol")}
    all_syms = sorted(
        set(positions_by_sym.keys())
        | set(orders_by_sym.keys())
        | set(fills_by_sym.keys())
        | set(decs_by_sym.keys())
        | set(ens_by_sym.keys())
        | watch_syms
    )

    banner = compute_session_trading_banner(
        execution_mode="LIVE_MARKET_PAPER",
        session_state=sess_state,
        strategy=strategy,
        open_positions_count=len(positions_by_sym),
        working_orders_count=sum(
            1
            for o in (snap.get("orders") or [])
            if str(o.get("status", "")).upper() in {"WORKING", "PARTIALLY_FILLED"}
        ),
        recent_fills_count=len(snap.get("fills") or []),
    )

    items: list[dict[str, Any]] = []
    for sym in all_syms:
        if not sym:
            continue
        pos = positions_by_sym.get(sym)
        sym_orders = orders_by_sym.get(sym) or []
        working = [o for o in sym_orders if str(o.get("status", "")).upper() in {"WORKING", "PARTIALLY_FILLED"}]
        sym_fills = fills_by_sym.get(sym) or []
        sym_decs = decs_by_sym.get(sym) or []
        sym_ens = ens_by_sym.get(sym) or []

        states: list[str] = []
        if pos is not None:
            states.append("OPEN_POSITION")
        if working:
            states.append("WORKING_ORDER")
        if sym_fills:
            states.append("RECENT_FILL")
        latest_sig_action = None
        if sym_ens:
            latest_sig_action = sym_ens[0].get("final_action") or sym_ens[0].get("raw_intent")
            if latest_sig_action in {"BUY", "SELL", "NO_TRADE"}:
                states.append("ACTIVE_SIGNAL")
        elif sym_decs:
            latest_sig_action = sym_decs[0].get("action")
            if latest_sig_action in {"BUY", "SELL"}:
                states.append("ACTIVE_SIGNAL")
        if not states:
            states.append("WATCHING")

        primary_state = states[0]
        last_px = (
            float(pos["last_price"])
            if pos and pos.get("last_price") is not None
            else (
                float(sym_fills[0]["fill_price"])
                if sym_fills and sym_fills[0].get("fill_price") is not None
                else (float(sym_ens[0]["reference_price"]) if sym_ens and sym_ens[0].get("reference_price") else None)
            )
        )
        last_ts = (
            (pos.get("last_quote_ts") if pos else None)
            or (sym_fills[0].get("created_at") if sym_fills else None)
            or (sym_ens[0].get("bar_timestamp") if sym_ens else None)
        )

        avg_entry = (
            float(pos["avg_entry_price"])
            if pos and pos.get("avg_entry_price") is not None
            else (float(pos["avg_price"]) if pos and pos.get("avg_price") is not None else None)
        )
        items.append(
            {
                "row_key": f"LIVE_MARKET_PAPER:{mkt}:{sym}",
                "market": mkt,
                "currency": ccy,
                "symbol": sym,
                "execution_mode": "LIVE_MARKET_PAPER",
                "mode_badge": "PAPER · SIMULATED",
                "simulated": True,
                "verification_origin": "SIMULATED (LOCAL PAPER LEDGER)",
                "session_state": sess_state,
                "strategy": strategy,
                "timeframe": tf,
                "actively_trading": banner["actively_trading"],
                "trading_banner": banner["banner_status"],
                "primary_state": primary_state,
                "states": states,
                "position_qty": float(pos["qty"]) if pos else 0.0,
                "avg_entry_price": avg_entry,
                "stop_loss": float(pos["stop_loss"]) if pos and pos.get("stop_loss") is not None else None,
                "take_profit": float(pos["take_profit"]) if pos and pos.get("take_profit") is not None else None,
                "unrealized_pnl": round(
                    (float(pos["last_price"]) - avg_entry) * float(pos["qty"]), 2
                )
                if pos and pos.get("last_price") is not None and avg_entry is not None
                else 0.0,
                "working_orders_count": len(working),
                "recent_fills_count": len(sym_fills),
                "latest_signal": latest_sig_action or "NONE",
                "last_price": last_px,
                "last_timestamp": last_ts,
            }
        )
    return items


def _collect_alpaca_live_items(alpaca_status: dict[str, Any] | None, journal_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not alpaca_status:
        return []
    gate_state = str((alpaca_status.get("gate") or {}).get("state") or "DISABLED").upper()
    env_label = str(alpaca_status.get("environment") or "ALPACA_LIVE").upper()
    is_real_live = env_label == "ALPACA_LIVE"
    mode_name = "LIVE_MARKET_LIVE" if is_real_live else "ALPACA_BROKER_PAPER"

    by_sym: dict[str, list[dict[str, Any]]] = {}
    for o in journal_orders:
        sym = str(o.get("symbol") or "").upper()
        if sym:
            by_sym.setdefault(sym, []).append(o)

    banner = compute_session_trading_banner(
        execution_mode="LIVE_MARKET_LIVE",
        session_state=gate_state,
        strategy="operator_reviewed",
        open_positions_count=0,
        working_orders_count=sum(
            1 for o in journal_orders if str(o.get("state", "")).upper() in {"SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED"}
        ),
    )

    items: list[dict[str, Any]] = []
    for sym, ords in sorted(by_sym.items()):
        working = [o for o in ords if str(o.get("state", "")).upper() in {"SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED"}]
        filled = [o for o in ords if str(o.get("state", "")).upper() == "FILLED"]
        states: list[str] = []
        if working:
            states.append("WORKING_ORDER")
        if filled:
            states.append("RECENT_FILL")
        if not states:
            states.append("WATCHING")
        items.append(
            {
                "row_key": f"{mode_name}:NASDAQ:{sym}",
                "market": "NASDAQ",
                "currency": "USD",
                "symbol": sym,
                "execution_mode": "LIVE_MARKET_LIVE",
                "mode_badge": "LIVE · BROKER GATE" if is_real_live else "BROKER DEMO",
                "simulated": False,
                "verification_origin": "BROKER_CONFIRMED (ALPACA JOURNAL)",
                "session_state": gate_state,
                "strategy": "operator_reviewed",
                "timeframe": "5m",
                "actively_trading": banner["actively_trading"],
                "trading_banner": banner["banner_status"],
                "primary_state": states[0],
                "states": states,
                "position_qty": 0.0,
                "avg_entry_price": None,
                "stop_loss": None,
                "take_profit": None,
                "unrealized_pnl": 0.0,
                "working_orders_count": len(working),
                "recent_fills_count": len(filled),
                "latest_signal": "NONE",
                "last_price": float(ords[0]["filled_avg_price"]) if ords[0].get("filled_avg_price") else None,
                "last_timestamp": ords[0].get("updated_at") or ords[0].get("created_at"),
            }
        )
    return items


def build_trade_monitor_summary(
    *,
    paper_engine: Any,
    ensemble_service: Any = None,
    alpaca_service: Any = None,
    market_filter: str = "ALL",
    mode_filter: str = "ALL",
    state_filter: str = "ALL",
    search: str = "",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Build filtered, paginated read-only summary of all monitored & traded instruments."""
    ens_decs = ensemble_service.list_recent_decisions(limit=150) if ensemble_service is not None else []
    all_items: list[dict[str, Any]] = []

    market_banners: dict[str, Any] = {}
    for mkt in ("NSE", "NASDAQ"):
        snap = paper_engine.market_snapshot(mkt)
        all_items.extend(_collect_paper_market_items(snap, ens_decs))
        market_banners[f"{mkt}_PAPER"] = compute_session_trading_banner(
            execution_mode="LIVE_MARKET_PAPER",
            session_state=str(snap.get("session_state") or "STOPPED"),
            strategy=str(snap.get("strategy") or "manual_only"),
            open_positions_count=len(snap.get("positions") or []),
            working_orders_count=sum(
                1
                for o in (snap.get("orders") or [])
                if str(o.get("status", "")).upper() in {"WORKING", "PARTIALLY_FILLED"}
            ),
            recent_fills_count=len(snap.get("fills") or []),
        )

    if alpaca_service is not None:
        try:
            alp_st = alpaca_service.status()
            j_orders = alpaca_service.journal.list_orders(limit=100) if getattr(alpaca_service, "journal", None) else []
            all_items.extend(_collect_alpaca_live_items(alp_st, j_orders))
            market_banners["NASDAQ_LIVE"] = compute_session_trading_banner(
                execution_mode="LIVE_MARKET_LIVE",
                session_state=str((alp_st.get("gate") or {}).get("state") or "DISABLED"),
                strategy="operator_reviewed",
                open_positions_count=0,
                working_orders_count=0,
            )
        except Exception as exc:  # noqa: BLE001
            _ = exc

    market_banners.setdefault(
        "NSE_LIVE",
        compute_session_trading_banner(
            execution_mode="LIVE_MARKET_LIVE",
            session_state="DISABLED",
            strategy="operator_reviewed",
            open_positions_count=0,
            working_orders_count=0,
        ),
    )

    mf = (market_filter or "ALL").strip().upper()
    modf = (mode_filter or "ALL").strip().upper()
    sf = (state_filter or "ALL").strip().upper()
    q = (search or "").strip().upper()

    filtered: list[dict[str, Any]] = []
    for item in all_items:
        if mf != "ALL" and item["market"] != mf:
            continue
        if modf != "ALL" and item["execution_mode"] != modf:
            continue
        if sf != "ALL" and sf not in item["states"]:
            continue
        if q and q not in item["symbol"] and q not in item["market"]:
            continue
        filtered.append(item)

    # Prioritize open positions > working orders > recent fills > active signals > watching
    priority_rank = {
        "OPEN_POSITION": 0,
        "WORKING_ORDER": 1,
        "RECENT_FILL": 2,
        "ACTIVE_SIGNAL": 3,
        "WATCHING": 4,
    }
    filtered.sort(key=lambda r: (priority_rank.get(r["primary_state"], 9), r["market"], r["symbol"]))

    p_size = max(1, min(200, int(page_size)))
    total = len(filtered)
    total_pages = max(1, (total + p_size - 1) // p_size)
    cur_page = max(1, min(int(page), total_pages))
    start = (cur_page - 1) * p_size
    sliced = filtered[start : start + p_size]

    return {
        "read_only": True,
        "broker_order_api_calls": 0,
        "generated_at": datetime.now(UTC).isoformat(),
        "banners": market_banners,
        "filters": {
            "market": mf,
            "execution_mode": modf,
            "state": sf,
            "search": q,
        },
        "pagination": {
            "page": cur_page,
            "page_size": p_size,
            "total_items": total,
            "total_pages": total_pages,
            "has_prev": cur_page > 1,
            "has_next": cur_page < total_pages,
        },
        "counts": {
            "total_unfiltered": len(all_items),
            "open_positions": sum(1 for x in all_items if "OPEN_POSITION" in x["states"]),
            "working_orders": sum(1 for x in all_items if "WORKING_ORDER" in x["states"]),
            "recent_fills": sum(1 for x in all_items if "RECENT_FILL" in x["states"]),
            "active_signals": sum(1 for x in all_items if "ACTIVE_SIGNAL" in x["states"]),
        },
        "items": sliced,
    }


def build_instrument_event_markers(
    *,
    market: str,
    symbol: str,
    execution_mode: str,
    paper_engine: Any,
    ensemble_service: Any = None,
    alpaca_service: Any = None,
    latest_bar_ts: str | None = None,
) -> dict[str, Any]:
    """Construct distinct, non-ambiguous event markers for a selected instrument chart.

    Distinct marker types (`marker_type` / `shape`):
    - `SIGNAL`          (`diamond_hollow`) : strategy or ensemble signal (NEVER styled as order/fill)
    - `ORDER_SUBMITTED` (`square_hollow`)  : submitted/working order
    - `PARTIAL_FILL`    (`circle_half`)    : partial order execution
    - `COMPLETED_FILL`  (`triangle_solid`) : completed BUY/SELL execution
    - `CANCELED`        (`cross_x`)        : canceled or rejected order
    - `STOP_LOSS`       (`shield_sl`)      : active or triggered stop-loss
    - `TAKE_PROFIT`     (`target_tp`)      : active or triggered take-profit
    - `EXIT`            (`chevron_exit`)   : position exit / close fill
    """
    mkt = market.strip().upper()
    sym = symbol.strip().upper()
    mode = execution_mode.strip().upper()
    ccy = "INR" if mkt == "NSE" else "USD"
    markers: list[dict[str, Any]] = []
    reference_lines: list[dict[str, Any]] = []

    if mode == "LIVE_MARKET_LIVE":
        gate_state = "DISABLED"
        if alpaca_service is not None and mkt == "NASDAQ":
            st = alpaca_service.status()
            gate_state = str((st.get("gate") or {}).get("state") or "DISABLED").upper()
            j_orders = alpaca_service.journal.list_orders(limit=200) if getattr(alpaca_service, "journal", None) else []
            for o in j_orders:
                if str(o.get("symbol", "")).upper() != sym:
                    continue
                ost = str(o.get("state") or "UNKNOWN").upper()
                side = str(o.get("side") or "BUY").upper()
                qty = float(o.get("qty") or 0.0)
                filled_qty = float(o.get("filled_qty") or 0.0)
                px = float(o.get("filled_avg_price") or o.get("limit_price") or 0.0)
                ts = str(o.get("updated_at") or o.get("created_at") or datetime.now(UTC).isoformat())
                if ost == "FILLED":
                    m_type = "EXIT" if side == "SELL" else "COMPLETED_FILL"
                elif ost == "PARTIALLY_FILLED":
                    m_type = "PARTIAL_FILL"
                elif ost in {"CANCELED", "REJECTED", "EXPIRED"}:
                    m_type = "CANCELED"
                else:
                    m_type = "ORDER_SUBMITTED"
                markers.append(
                    {
                        "marker_id": f"live-ord-{o.get('client_order_id')}",
                        "marker_type": m_type,
                        "shape": MARKER_SHAPES[m_type],
                        "event_time": ts,
                        "side": side,
                        "quantity": filled_qty if m_type in {"COMPLETED_FILL", "PARTIAL_FILL", "EXIT"} else qty,
                        "ordered_quantity": qty,
                        "price": px,
                        "currency": ccy,
                        "fees": 0.0,
                        "strategy": "operator_reviewed_live",
                        "reason": str(o.get("reject_reason") or "Operator reviewed broker order"),
                        "order_or_fill_status": ost,
                        "execution_mode": "LIVE_MARKET_LIVE",
                        "simulated": False,
                        "verification_origin": "BROKER_CONFIRMED (ALPACA JOURNAL)",
                    }
                )
        banner = compute_session_trading_banner(
            execution_mode="LIVE_MARKET_LIVE",
            session_state=gate_state,
            strategy="operator_reviewed",
            open_positions_count=0,
            working_orders_count=sum(1 for m in markers if m["marker_type"] == "ORDER_SUBMITTED"),
        )
        markers.sort(key=lambda m: str(m["event_time"]))
        return {
            "market": mkt,
            "symbol": sym,
            "currency": ccy,
            "execution_mode": "LIVE_MARKET_LIVE",
            "simulated": False,
            "session_banner": banner,
            "reference_lines": reference_lines,
            "markers": markers,
        }

    # LIVE_MARKET_PAPER path
    snap = paper_engine.market_snapshot(mkt)
    sess_state = str(snap.get("session_state") or "STOPPED")
    strat_name = str(snap.get("strategy") or "manual_only")
    ref_ts = latest_bar_ts or datetime.now(UTC).isoformat()

    pos = next((p for p in (snap.get("positions") or []) if str(p.get("symbol", "")).upper() == sym and float(p.get("qty", 0)) > 0), None)
    if pos is not None:
        raw_avg = pos.get("avg_entry_price") if pos.get("avg_entry_price") is not None else pos.get("avg_price")
        avg_px = float(raw_avg or pos.get("last_price") or 0.0)
        reference_lines.append({"kind": "AVG_ENTRY", "price": avg_px, "label": f"PAPER AVG ENTRY {avg_px:.2f}"})
        if pos.get("stop_loss") is not None:
            sl_val = float(pos["stop_loss"])
            reference_lines.append({"kind": "STOP_LOSS", "price": sl_val, "label": f"STOP LOSS {sl_val:.2f}"})
            markers.append(
                {
                    "marker_id": f"paper-sl-{mkt}-{sym}",
                    "marker_type": "STOP_LOSS",
                    "shape": MARKER_SHAPES["STOP_LOSS"],
                    "event_time": str(pos.get("updated_at") or ref_ts),
                    "side": "SELL",
                    "quantity": float(pos["qty"]),
                    "ordered_quantity": float(pos["qty"]),
                    "price": sl_val,
                    "currency": ccy,
                    "fees": 0.0,
                    "strategy": strat_name,
                    "reason": "Active protective stop-loss level on simulated paper position",
                    "order_or_fill_status": "ACTIVE_PROTECTION",
                    "execution_mode": "LIVE_MARKET_PAPER",
                    "simulated": True,
                    "verification_origin": "SIMULATED (LOCAL PAPER LEDGER)",
                }
            )
        if pos.get("take_profit") is not None:
            tp_val = float(pos["take_profit"])
            reference_lines.append({"kind": "TAKE_PROFIT", "price": tp_val, "label": f"TAKE PROFIT {tp_val:.2f}"})
            markers.append(
                {
                    "marker_id": f"paper-tp-{mkt}-{sym}",
                    "marker_type": "TAKE_PROFIT",
                    "shape": MARKER_SHAPES["TAKE_PROFIT"],
                    "event_time": str(pos.get("updated_at") or ref_ts),
                    "side": "SELL",
                    "quantity": float(pos["qty"]),
                    "ordered_quantity": float(pos["qty"]),
                    "price": tp_val,
                    "currency": ccy,
                    "fees": 0.0,
                    "strategy": strat_name,
                    "reason": "Active protective take-profit level on simulated paper position",
                    "order_or_fill_status": "ACTIVE_PROTECTION",
                    "execution_mode": "LIVE_MARKET_PAPER",
                    "simulated": True,
                    "verification_origin": "SIMULATED (LOCAL PAPER LEDGER)",
                }
            )

    orders_by_cid: dict[str, dict[str, Any]] = {}
    for o in snap.get("orders") or []:
        if str(o.get("symbol", "")).upper() != sym:
            continue
        cid = str(o["client_order_id"])
        orders_by_cid[cid] = o
        ost = str(o.get("status") or "").upper()
        px = float(o.get("limit_price") or o.get("stop_price") or o.get("avg_fill_price") or 0.0)
        # Always emit ORDER_SUBMITTED marker for working/partial orders, and CANCELED for canceled/rejected
        if ost in {"WORKING", "PARTIALLY_FILLED"}:
            markers.append(
                {
                    "marker_id": f"paper-ord-{cid}",
                    "marker_type": "ORDER_SUBMITTED",
                    "shape": MARKER_SHAPES["ORDER_SUBMITTED"],
                    "event_time": str(o.get("created_at") or ref_ts),
                    "side": str(o.get("side") or "BUY").upper(),
                    "quantity": float(o.get("qty") or 0.0),
                    "ordered_quantity": float(o.get("qty") or 0.0),
                    "price": px,
                    "currency": ccy,
                    "fees": float(o.get("fees_total") or 0.0),
                    "strategy": strat_name,
                    "reason": f"Simulated {o.get('order_type', 'LIMIT')} order working in local paper ledger",
                    "order_or_fill_status": ost,
                    "execution_mode": "LIVE_MARKET_PAPER",
                    "simulated": True,
                    "verification_origin": "SIMULATED (LOCAL PAPER LEDGER)",
                }
            )
        elif ost in {"CANCELED", "REJECTED", "EXPIRED"}:
            markers.append(
                {
                    "marker_id": f"paper-cancel-{cid}",
                    "marker_type": "CANCELED",
                    "shape": MARKER_SHAPES["CANCELED"],
                    "event_time": str(o.get("updated_at") or o.get("created_at") or ref_ts),
                    "side": str(o.get("side") or "BUY").upper(),
                    "quantity": float(o.get("qty") or 0.0),
                    "ordered_quantity": float(o.get("qty") or 0.0),
                    "price": px,
                    "currency": ccy,
                    "fees": float(o.get("fees_total") or 0.0),
                    "strategy": strat_name,
                    "reason": str(o.get("reject_reason") or f"Simulated paper order {ost.lower()}"),
                    "order_or_fill_status": ost,
                    "execution_mode": "LIVE_MARKET_PAPER",
                    "simulated": True,
                    "verification_origin": "SIMULATED (LOCAL PAPER LEDGER)",
                }
            )

    for f in snap.get("fills") or []:
        if str(f.get("symbol", "")).upper() != sym:
            continue
        cid = str(f.get("client_order_id") or "")
        parent = orders_by_cid.get(cid) or {}
        parent_qty = float(parent.get("qty") or f.get("fill_qty") or 0.0)
        fill_qty = float(f.get("fill_qty") or 0.0)
        side = str(f.get("side") or "BUY").upper()
        is_partial = parent_qty > 0 and fill_qty + 1e-9 < parent_qty and str(parent.get("status", "")).upper() == "PARTIALLY_FILLED"

        if "lmp-close-" in cid:
            m_type = "EXIT"
            reason_txt = "Simulated position exit fill"
        elif is_partial:
            m_type = "PARTIAL_FILL"
            reason_txt = f"Simulated partial fill ({fill_qty}/{parent_qty} units; bar participation cap)"
        elif side == "SELL":
            m_type = "EXIT"
            reason_txt = "Simulated SELL exit execution"
        else:
            m_type = "COMPLETED_FILL"
            reason_txt = f"Simulated {side} fill from read-only quote ({f.get('quote_source') or 'live-readonly'})"

        markers.append(
            {
                "marker_id": f"paper-fill-{f.get('fill_id')}",
                "marker_type": m_type,
                "shape": MARKER_SHAPES[m_type],
                "event_time": str(f.get("created_at") or ref_ts),
                "side": side,
                "quantity": fill_qty,
                "ordered_quantity": parent_qty,
                "price": float(f.get("fill_price") or 0.0),
                "currency": ccy,
                "fees": float(f.get("fees_total") or 0.0),
                "strategy": strat_name,
                "reason": reason_txt,
                "order_or_fill_status": "PARTIALLY_FILLED" if is_partial else "FILLED",
                "execution_mode": "LIVE_MARKET_PAPER",
                "simulated": True,
                "verification_origin": "SIMULATED (LOCAL PAPER LEDGER)",
            }
        )

    # Strategy & Ensemble Signals (strictly SIGNAL / diamond_hollow — never disguised as orders/fills)
    for d in snap.get("decisions") or []:
        if str(d.get("symbol", "")).upper() != sym:
            continue
        markers.append(
            {
                "marker_id": f"paper-sig-{d.get('decision_id')}",
                "marker_type": "SIGNAL",
                "shape": MARKER_SHAPES["SIGNAL"],
                "event_time": str(d.get("bar_timestamp") or d.get("created_at") or ref_ts),
                "side": str(d.get("action") or "HOLD").upper(),
                "quantity": 0.0,
                "ordered_quantity": 0.0,
                "price": float(pos["last_price"]) if pos and pos.get("last_price") else 0.0,
                "currency": ccy,
                "fees": 0.0,
                "strategy": str(d.get("strategy") or strat_name),
                "reason": str(d.get("reason") or "Completed-bar strategy evaluation"),
                "order_or_fill_status": "SIGNAL_ONLY (NOT AN ORDER)",
                "execution_mode": "LIVE_MARKET_PAPER",
                "simulated": True,
                "verification_origin": "ADVISORY_SIGNAL (LOCAL EVALUATION)",
            }
        )

    if ensemble_service is not None:
        for ed in ensemble_service.list_recent_decisions(market=mkt, symbol=sym, limit=30):
            act = str(ed.get("final_action") or "NO_TRADE").upper()
            reasons = ed.get("no_trade_reasons") or []
            reason_summary = (
                f"Portfolio allocator {act} ({ed.get('selected_qty', 0)} units; conf={(ed.get('calibration') or {}).get('calibrated_confidence')})"
                if act in {"BUY", "SELL"}
                else f"NO_TRADE: {'; '.join(reasons[:2]) or 'Thresholds not met'}"
            )
            markers.append(
                {
                    "marker_id": f"ens-sig-{ed.get('decision_id') or ed.get('bar_timestamp')}",
                    "marker_type": "SIGNAL",
                    "shape": MARKER_SHAPES["SIGNAL"],
                    "event_time": str(ed.get("bar_timestamp") or ed.get("evaluated_at") or ref_ts),
                    "side": act,
                    "quantity": float(ed.get("selected_qty") or 0.0),
                    "ordered_quantity": 0.0,
                    "price": float(ed.get("reference_price") or 0.0),
                    "currency": ccy,
                    "fees": float(ed.get("estimated_fees") or 0.0),
                    "strategy": "parallel_ensemble_v1",
                    "reason": reason_summary,
                    "order_or_fill_status": "SIGNAL_ONLY (PORTFOLIO ALLOCATOR)",
                    "execution_mode": "LIVE_MARKET_PAPER",
                    "simulated": True,
                    "verification_origin": "ADVISORY_SIGNAL (PORTFOLIO ALLOCATOR)",
                }
            )

    markers.sort(key=lambda m: str(m["event_time"]))
    working_cnt = sum(1 for o in orders_by_cid.values() if str(o.get("status", "")).upper() in {"WORKING", "PARTIALLY_FILLED"})
    fill_cnt = sum(1 for m in markers if m["marker_type"] in {"COMPLETED_FILL", "PARTIAL_FILL", "EXIT"})
    banner = compute_session_trading_banner(
        execution_mode="LIVE_MARKET_PAPER",
        session_state=sess_state,
        strategy=strat_name,
        open_positions_count=1 if pos is not None else 0,
        working_orders_count=working_cnt,
        recent_fills_count=fill_cnt,
    )
    return {
        "market": mkt,
        "symbol": sym,
        "currency": ccy,
        "execution_mode": "LIVE_MARKET_PAPER",
        "simulated": True,
        "session_banner": banner,
        "reference_lines": reference_lines,
        "markers": markers,
    }
