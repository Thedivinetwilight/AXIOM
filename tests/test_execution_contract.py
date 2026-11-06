"""Shared execution contract: intents, lifecycle transitions, documented profiles."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.execution import contract as C
from app.execution import profiles as P
from app.execution.sessions import SessionWindow


def _intent(**overrides):
    payload = dict(
        intent_id="intent-0001",
        broker=C.BROKER_UPSTOX,
        account_ref="acct-masked",
        symbol="RELIANCE",
        instrument_key="NSE_EQ|INE002A01018",
        side="BUY",
        order_type="LIMIT",
        quantity=10,
        time_in_force="DAY",
        product="DELIVERY",
        limit_price=2500.0,
        quote_price=2499.5,
        quote_at="2026-11-07T04:00:00+00:00",
    )
    payload.update(overrides)
    return C.ExecutionOrderIntent(**payload)


# --------------------------------------------------------------------------- #
# Intent validation
# --------------------------------------------------------------------------- #
def test_valid_intent_normalises_and_has_no_secrets():
    intent = _intent(side="buy", order_type="limit").validate()
    assert intent.side is C.Side.BUY
    assert intent.order_type is C.OrderType.LIMIT
    assert intent.broker == C.BROKER_UPSTOX
    assert intent.symbol == "RELIANCE"
    assert intent.notional() == pytest.approx(25000.0)
    payload = intent.as_dict()
    assert "token" not in str(payload).lower()


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"quantity": 0}, "INVALID_INTENT"),
        ({"quantity": float("nan")}, "INVALID_INTENT"),
        ({"quantity": float("inf")}, "INVALID_INTENT"),
        ({"symbol": ""}, "INVALID_INTENT"),
        ({"account_ref": ""}, "ACCOUNT_UNRESOLVED"),
        ({"broker": "SOMEWHERE"}, "INVALID_INTENT"),
        ({"price_basis": "adjusted_research_close"}, "ADJUSTED_RESEARCH_PRICE"),
        ({"quote_price": None}, "QUOTE_MALFORMED"),
        ({"quote_price": -1}, "QUOTE_MALFORMED"),
        ({"quote_at": None}, "QUOTE_STALE"),
        ({"limit_price": None}, "INVALID_INTENT"),
        ({"order_type": "STOP", "limit_price": None}, "INVALID_INTENT"),
        ({"order_type": "STOP_MARKET", "trigger_price": None}, "INVALID_INTENT"),
    ],
)
def test_invalid_intents_are_refused_with_specific_codes(overrides, code):
    with pytest.raises(C.ExecutionRefused) as exc:
        _intent(**overrides).validate()
    assert exc.value.code == code
    assert exc.value.code in C.REFUSAL_CODES


def test_research_prices_can_never_be_sent():
    with pytest.raises(C.ExecutionRefused) as exc:
        _intent(price_basis="split_adjusted_research_close").validate()
    assert exc.value.code == "ADJUSTED_RESEARCH_PRICE"
    assert "research" in exc.value.detail.lower()


# --------------------------------------------------------------------------- #
# Lifecycle transitions
# --------------------------------------------------------------------------- #
def test_documented_lifecycle_paths_are_legal():
    path = [
        C.OrderState.RECORDED,
        C.OrderState.SUBMITTING,
        C.OrderState.ACKNOWLEDGED,
        C.OrderState.PARTIALLY_FILLED,
        C.OrderState.FILLED,
    ]
    for current, target in zip(path, path[1:]):
        C.assert_transition(current, target)

    C.assert_transition(C.OrderState.ACKNOWLEDGED, C.OrderState.CANCEL_REQUESTED)
    C.assert_transition(C.OrderState.CANCEL_REQUESTED, C.OrderState.CANCELLED)
    C.assert_transition(C.OrderState.SUBMITTING, C.OrderState.AMBIGUOUS)
    C.assert_transition(C.OrderState.AMBIGUOUS, C.OrderState.ACKNOWLEDGED)
    C.assert_transition(C.OrderState.AMBIGUOUS, C.OrderState.NEEDS_OPERATOR_REVIEW)


@pytest.mark.parametrize(
    "current, target",
    [
        (C.OrderState.RECORDED, C.OrderState.FILLED),
        (C.OrderState.FILLED, C.OrderState.CANCELLED),
        (C.OrderState.CANCELLED, C.OrderState.ACKNOWLEDGED),
        (C.OrderState.REJECTED, C.OrderState.FILLED),
        (C.OrderState.NEEDS_OPERATOR_REVIEW, C.OrderState.SUBMITTING),
        (C.OrderState.EXPIRED, C.OrderState.RECORDED),
    ],
)
def test_illegal_transitions_are_refused(current, target):
    with pytest.raises(C.ExecutionRefused) as exc:
        C.assert_transition(current, target)
    assert exc.value.code == "ILLEGAL_TRANSITION"


def test_terminal_states_are_terminal():
    for state in C.TERMINAL_STATES:
        assert C.TRANSITIONS[state] == frozenset()


def test_states_do_not_overlap():
    assert not (C.OPEN_STATES & C.TERMINAL_STATES)
    assert not (C.BLOCKING_STATES & C.TERMINAL_STATES)
    assert not (C.BLOCKING_STATES & C.OPEN_STATES)


# --------------------------------------------------------------------------- #
# Broker profiles
# --------------------------------------------------------------------------- #
def test_upstox_profile_matches_the_documented_order_parameters():
    profile = P.profile_for("upstox")
    assert profile.broker == C.BROKER_UPSTOX
    assert profile.instrument_identifier.startswith("instrument_key")
    assert set(profile.order_types) == {
        C.OrderType.MARKET,
        C.OrderType.LIMIT,
        C.OrderType.STOP,
        C.OrderType.STOP_MARKET,
    }
    assert set(profile.time_in_force) == {C.TimeInForce.DAY, C.TimeInForce.IOC}
    assert set(profile.products) == {C.Product.INTRADAY, C.Product.DELIVERY}
    assert profile.idempotency.field == "tag"
    assert profile.idempotency.max_length == 20
    assert profile.idempotency.rejects_duplicates is False
    assert "tag" in profile.idempotency.query_by_key
    assert profile.session.timezone == "Asia/Kolkata"
    assert profile.session.open_time.strftime("%H:%M") == "09:15"
    assert profile.session.close_time.strftime("%H:%M") == "15:30"
    joined = " ".join(profile.unsupported).lower()
    for feature in ("fnb" if False else "derivatives", "mtf", "after-market", "gtd"):
        assert feature in joined


def test_alpaca_profile_matches_the_documented_order_parameters():
    profile = P.profile_for("alpaca")
    assert profile.broker == C.BROKER_ALPACA
    assert profile.instrument_identifier == "ticker symbol"
    assert profile.idempotency.field == "client_order_id"
    assert profile.idempotency.max_length == 128
    assert profile.idempotency.rejects_duplicates is True
    assert profile.session.timezone == "America/New_York"
    assert profile.session.open_time.strftime("%H:%M") == "09:30"
    assert profile.session.close_time.strftime("%H:%M") == "16:00"
    assert C.Product.INTRADAY not in profile.products
    joined = " ".join(profile.unsupported).lower()
    for feature in ("options", "crypto", "notional", "trailing_stop", "extended_hours", "bracket"):
        assert feature in joined


def test_profiles_are_not_treated_as_equivalent():
    upstox = P.profile_for("upstox")
    alpaca = P.profile_for("alpaca")
    assert upstox.idempotency.rejects_duplicates != alpaca.idempotency.rejects_duplicates
    assert set(upstox.products) != set(alpaca.products)
    assert upstox.instrument_identifier != alpaca.instrument_identifier
    assert upstox.sources != alpaca.sources


def test_every_profile_records_its_sources_and_date():
    for profile in P.PROFILES.values():
        assert profile.sources and all(url.startswith("https://") for url in profile.sources)
        assert profile.documented_as_of == P.DOCUMENTED_AS_OF
        payload = profile.as_dict()
        assert payload["sources"] == list(profile.sources)


def test_profile_refuses_unsupported_requests_instead_of_translating():
    alpaca = P.profile_for("alpaca")
    intent = _intent(broker=C.BROKER_ALPACA, symbol="AAPL", instrument_key="AAPL").validate()
    alpaca.check(intent)  # limit/day is fine on Alpaca

    with pytest.raises(C.ExecutionRefused) as exc:
        alpaca.check(_intent(broker=C.BROKER_ALPACA, symbol="AAPL", instrument_key="AAPL",
                             time_in_force="GTC").validate())
    assert exc.value.code == "UNSUPPORTED_ORDER_TYPE"

    with pytest.raises(C.ExecutionRefused) as exc:
        alpaca.check(_intent(broker=C.BROKER_ALPACA, symbol="AAPL", instrument_key="AAPL",
                             product="INTRADAY").validate())
    assert exc.value.code == "UNSUPPORTED_ORDER_TYPE"

    with pytest.raises(C.ExecutionRefused) as exc:
        alpaca.check(_intent().validate())  # Upstox intent, Alpaca adapter
    assert exc.value.code == "UNSUPPORTED_INSTRUMENT"


def test_unknown_broker_is_refused():
    with pytest.raises(C.ExecutionRefused) as exc:
        P.profile_for("interactive_brokers")
    assert exc.value.code == "INVALID_INTENT"


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def test_upstox_session_window_is_the_nse_regular_session():
    window = P.profile_for("upstox").session
    assert window.state(_at("2026-11-06T04:00:00+00:00"))["open"] is True   # 09:30 IST
    assert window.state(_at("2026-11-06T03:30:00+00:00"))["reason"] == "before_open"
    assert window.state(_at("2026-11-06T10:30:00+00:00"))["reason"] == "after_close"
    assert window.state(_at("2026-11-07T04:00:00+00:00"))["reason"] == "weekend"


def test_alpaca_session_window_is_the_us_regular_session():
    window = P.profile_for("alpaca").session
    assert window.state(_at("2026-11-06T15:00:00+00:00"))["open"] is True   # 10:00 EST
    assert window.state(_at("2026-11-06T13:00:00+00:00"))["reason"] == "before_open"
    assert window.state(_at("2026-11-06T21:30:00+00:00"))["reason"] == "after_close"


def test_session_boundaries_are_exact_and_report_next_open():
    window = SessionWindow("test", "Asia/Kolkata", open_time=__import__("datetime").time(9, 15),
                           close_time=__import__("datetime").time(15, 30))
    state = window.state(_at("2026-11-06T03:30:00+00:00"))
    assert state["open"] is False
    assert state["next_open"].startswith("2026-11-06T09:15")
    assert window.state(_at("2026-11-06T03:45:00+00:00"))["open"] is True   # exactly 09:15 local
    assert window.state(_at("2026-11-06T10:00:00+00:00"))["open"] is False  # exactly 15:30 local


def test_session_refuses_naive_datetimes():
    with pytest.raises(ValueError):
        P.profile_for("alpaca").session.state(datetime(2026, 11, 6, 15, 0))


def test_session_payload_admits_holidays_are_not_modelled():
    payload = P.profile_for("alpaca").session.as_dict()
    assert payload["holidays_known"] is False
    assert "holiday" in payload["note"].lower()
