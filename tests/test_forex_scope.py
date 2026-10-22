from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.forex.contracts import (
    CONTRACT_UNITS,
    ForexPaperBook,
    ObservedQuote,
    contract_from_upstox_record,
    weekday_session_hint,
)


AS_OF = date(2026, 10, 22)
NOW = datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc)  # 10:30 IST


def instrument(**overrides):
    record = {
        "instrument_key": "NCD_FO|123456",
        "segment": "NCD_FO",
        "exchange": "NSE",
        "expiry": "2026-12-31",
        "trading_symbol": "USDINR FUT 31 DEC 26",
        "tick_size": 0.0025,
        "lot_size": CONTRACT_UNITS,
        "instrument_type": "FUT",
        "underlying_type": "CUR",
        "underlying_symbol": "USDINR",
    }
    return record | overrides


def contract():
    return contract_from_upstox_record(instrument(), as_of=AS_OF)


def quote(*, bid="83.5000", ask="83.5025", received=NOW,
          exchange=NOW, status_time=NOW, status="NORMAL_OPEN", provenance="OBSERVED",
          instrument_key="NCD_FO|123456", provider="upstox"):
    return ObservedQuote.create(
        provider=provider,
        instrument_key=instrument_key,
        bid=bid,
        ask=ask,
        exchange_time=exchange,
        received_time=received,
        market_status=status,
        market_status_time=status_time,
        provenance=provenance,
    )


def test_contract_identity_and_tick_pip_notional_and_pnl_math():
    c = contract()
    assert c.identity == "IN:NSE:CDS:FUT:USDINR:2026-12-31"
    assert c.provider_format == "NCD_FO|123456"
    assert c.ticks_per_pip == 4
    assert c.tick_value_inr(1) == Decimal("2.5000")
    assert c.pip_value_inr(1) == Decimal("10.00")
    assert c.notional_inr("83.5000", 1) == Decimal("83500.0000")
    assert c.pnl_inr("LONG", "83.5000", "83.5100", 1) == Decimal("10.0000")
    assert c.pnl_inr("SHORT", "83.5000", "83.5100", 1) == Decimal("-10.0000")


@pytest.mark.parametrize("changes", [
    {"instrument_key": "NSE_EQ|INE0000000000"},
    {"segment": "NSE_FO"},
    {"exchange": "BSE"},
    {"instrument_type": "CE"},
    {"underlying_type": "EQUITY"},
    {"underlying_symbol": "EURUSD"},
    {"lot_size": 100},
    {"tick_size": 0.0001},
])
def test_only_exact_nse_usdinr_future_contract_is_accepted(changes):
    with pytest.raises(ValueError):
        contract_from_upstox_record(instrument(**changes), as_of=AS_OF)


def test_expired_or_missing_expiry_is_rejected():
    with pytest.raises(ValueError, match="expired"):
        contract_from_upstox_record(instrument(expiry="2026-10-22"), as_of=AS_OF)
    with pytest.raises(ValueError, match="ISO date"):
        contract_from_upstox_record(instrument(expiry="not-a-date"), as_of=AS_OF)


def test_contract_price_must_align_to_exchange_tick():
    with pytest.raises(ValueError, match="align"):
        contract().price("83.5010")


@pytest.mark.parametrize("kwargs, message", [
    ({"bid": "83.5050", "ask": "83.5025"}, "bid must not exceed ask"),
    ({"bid": "83.5010"}, "align to the exchange tick"),
    ({"status": "UNKNOWN"}, "NORMAL_OPEN"),
    ({"provenance": "MODELLED"}, "observed quote"),
    ({"provider": "binance"}, "provider must match"),
    ({"instrument_key": "NCD_FO|999"}, "does not match"),
    ({"received": NOW - timedelta(seconds=6)}, "stale"),
    ({"status_time": NOW - timedelta(seconds=6)}, "market-status observation is stale"),
    ({"exchange": NOW + timedelta(seconds=1)}, "stale, future-dated"),
])
def test_quote_fails_closed(kwargs, message):
    with pytest.raises(ValueError, match=message):
        quote(**kwargs).validate(contract(), now=NOW)


def test_paper_book_is_cash_collateralized_and_idempotent():
    c = contract()
    book = ForexPaperBook(Decimal("200000"), max_lots=1,
                          max_notional_inr=Decimal("100000"),
                          max_daily_loss_inr=Decimal("5000"))
    q = quote()
    first = book.open(contract=c, quote=q, side="LONG", lots=1,
                      idempotency_key="entry-1", now=NOW)
    duplicate = book.open(contract=c, quote=q, side="LONG", lots=1,
                          idempotency_key="entry-1", now=NOW)
    assert first["mode"] == "PAPER"
    assert first["status"] == "SIMULATED_FILLED"
    assert first["broker_mutation"] is False
    assert duplicate["idempotent_replay"] is True
    assert book.position.lots == 1
    assert book.cash_inr == Decimal("116497.5000")
    # An observed higher exit bid realizes price change × 1,000 USD.
    exit_quote = quote(bid="83.5100", ask="83.5125", received=NOW + timedelta(seconds=1),
                       exchange=NOW + timedelta(seconds=1),
                       status_time=NOW + timedelta(seconds=1))
    closed = book.close(contract=c, quote=exit_quote,
                        idempotency_key="exit-1", now=NOW + timedelta(seconds=1))
    assert closed["realized_pnl_inr"] == "7.5000"
    assert closed["broker_mutation"] is False
    assert book.position is None
    assert book.cash_inr == Decimal("200007.5000")
    assert book.realized_pnl_inr == Decimal("7.5000")


def test_idempotency_key_cannot_be_reused_for_a_different_paper_request():
    c = contract()
    book = ForexPaperBook(Decimal("200000"), max_notional_inr=Decimal("100000"))
    book.open(contract=c, quote=quote(), side="LONG", lots=1,
              idempotency_key="same-key", now=NOW)
    with pytest.raises(ValueError, match="different paper request"):
        book.open(contract=c, quote=quote(), side="SHORT", lots=1,
                  idempotency_key="same-key", now=NOW)


def test_emergency_stop_blocks_new_paper_entries_but_not_risk_reducing_close():
    c = contract()
    book = ForexPaperBook(Decimal("200000"), max_notional_inr=Decimal("100000"))
    book.open(contract=c, quote=quote(), side="LONG", lots=1,
              idempotency_key="open", now=NOW)
    book.emergency_stop = True
    later = NOW + timedelta(seconds=1)
    q = quote(bid="83.4975", ask="83.5000", received=later, exchange=later,
              status_time=later)
    with pytest.raises(ValueError, match="emergency stop"):
        book.open(contract=c, quote=q, side="LONG", lots=1,
                  idempotency_key="second", now=later)
    closed = book.close(contract=c, quote=q, idempotency_key="risk-close", now=later)
    assert closed["side"] == "CLOSE"
    assert book.position is None


def test_risk_limits_reject_entries_without_mutating_position():
    c = contract()
    limited_lots = ForexPaperBook(Decimal("500000"), max_lots=1,
                                  max_notional_inr=Decimal("500000"))
    with pytest.raises(ValueError, match="max_lots"):
        limited_lots.open(contract=c, quote=quote(), side="LONG", lots=2,
                          idempotency_key="too-many", now=NOW)
    limited_notional = ForexPaperBook(Decimal("500000"), max_lots=2,
                                      max_notional_inr=Decimal("80000"))
    with pytest.raises(ValueError, match="max_notional"):
        limited_notional.open(contract=c, quote=quote(), side="LONG", lots=1,
                              idempotency_key="too-large", now=NOW)
    assert limited_lots.position is None
    assert limited_notional.position is None


def test_daily_loss_limit_blocks_further_entries_after_a_realized_paper_loss():
    c = contract()
    book = ForexPaperBook(Decimal("200000"), max_notional_inr=Decimal("100000"),
                          max_daily_loss_inr=Decimal("500"))
    book.open(contract=c, quote=quote(), side="LONG", lots=1,
              idempotency_key="loss-open", now=NOW)
    later = NOW + timedelta(seconds=1)
    loss_quote = quote(bid="82.5025", ask="82.5050", received=later,
                       exchange=later, status_time=later)
    closed = book.close(contract=c, quote=loss_quote,
                        idempotency_key="loss-close", now=later)
    assert Decimal(closed["realized_pnl_inr"]) < Decimal("-500")
    next_time = NOW + timedelta(seconds=2)
    next_quote = quote(received=next_time, exchange=next_time,
                       status_time=next_time)
    with pytest.raises(ValueError, match="daily-loss limit"):
        book.open(contract=c, quote=next_quote, side="LONG", lots=1,
                  idempotency_key="after-loss", now=next_time)


def test_forex_paper_module_has_no_network_or_broker_order_imports():
    import ast
    from pathlib import Path

    source = Path("app/forex/contracts.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    assert not any(name.startswith("app.brokers") for name in imports)
    assert not any(name.split(".")[0] in {"httpx", "requests", "urllib", "websockets"}
                   for name in imports)


def test_market_clock_is_hint_only_and_does_not_assume_holiday_calendar():
    hint = weekday_session_hint(NOW)
    assert hint["timezone"] == "Asia/Kolkata"
    assert hint["holiday_status"] == "UNKNOWN"
    assert hint["provider_market_status"] == "NOT_OBSERVED"
    assert hint["execution_authorized"] is False
    assert hint["dst"] == "not_applicable_fixed_IST"


def test_scope_endpoint_reports_blocked_and_no_live_or_connected_feed():
    from app.api.routes.workstation import forex_scope

    result = __import__("asyncio").run(forex_scope())
    assert result["support_status"] == "BLOCKED_UNVERIFIED"
    assert result["live"]["enabled"] is False
    assert result["live"]["supported"] is False
    assert result["market_data"]["connection"] == "NOT_TESTED"
    assert result["paper"]["durable_state"] == "NOT_IMPLEMENTED"
    assert "OTC spot" in result["excluded_products"]
    assert "CFDs" in result["excluded_products"]
