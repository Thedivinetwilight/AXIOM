"""Corporate-action handling: format, validation, adjustment arithmetic, look-ahead.

All fixtures here are SYNTHETIC. Passing these tests demonstrates deterministic,
auditable bookkeeping — it says nothing about real-market accuracy or profitability.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.data.corporate_actions import (
    FORMAT_ID,
    AdjustmentPolicy,
    AdjustedSeriesError,
    AdjustmentStatus,
    AmbiguousCorporateAction,
    CorporateActionError,
    CorporateActionsBook,
    ConflictingCorporateAction,
    DuplicateCorporateAction,
    MalformedCorporateAction,
    apply_split_to_position,
    assert_no_double_counting,
    assert_raw_candles,
    block_for_meta,
    build_series,
    canonical_bars_sha256,
    dividend_cash_events,
)
from app.data.models import Candle

START = datetime(2024, 6, 3, tzinfo=UTC)
EX_SPLIT = datetime(2024, 6, 13, tzinfo=UTC)
EX_DIV = datetime(2024, 6, 17, tzinfo=UTC)


def make_bars(count: int = 12, *, price: float = 100.0, volume: float = 1000.0) -> list[Candle]:
    return [
        Candle(
            timestamp=START + timedelta(days=i),
            open=price,
            high=price * 1.01,
            low=price * 0.99,
            close=price,
            volume=volume,
            symbol="DEMO",
        )
        for i in range(count)
    ]


def split_record(**overrides):
    record = {
        "record_id": "NSE-DEMO-SPLIT-2024-06-13",
        "revision": 1,
        "market": "NSE",
        "symbol": "DEMO",
        "action_type": "SPLIT",
        "ex_date": EX_SPLIT.date().isoformat(),
        "announced_at": datetime(2024, 5, 2, tzinfo=UTC).date().isoformat(),
        "ratio_from": 1,
        "ratio_to": 2,
        "source": "NSE corporate-action filing",
        "source_version": "2024-06-13",
        "provenance": "operator transcription of the exchange filing",
    }
    record.update(overrides)
    return record


def dividend_record(**overrides):
    record = {
        "record_id": "NSE-DEMO-DIV-2024-06-17",
        "revision": 1,
        "market": "NSE",
        "symbol": "DEMO",
        "action_type": "CASH_DIVIDEND",
        "ex_date": EX_DIV.date().isoformat(),
        "announced_at": datetime(2024, 5, 20, tzinfo=UTC).date().isoformat(),
        "cash_per_share": 5.0,
        "currency": "INR",
        "source": "NSE corporate-action filing",
        "source_version": "2024-06-17",
        "provenance": "operator transcription of the exchange filing",
    }
    record.update(overrides)
    return record


def payload(*actions, **provider) -> dict:
    provider_block = {"name": "operator-reviewed", "version": "2024-07-01"}
    provider_block.update(provider)
    return {
        "format": FORMAT_ID,
        "format_version": 1,
        "provider": provider_block,
        "actions": list(actions) or [split_record()],
    }


def book_with(*actions) -> CorporateActionsBook:
    return CorporateActionsBook.from_payload(payload(*actions))


# --------------------------------------------------------------------------- #
# Import format and validation
# --------------------------------------------------------------------------- #
def test_valid_payload_parses_with_provenance_and_hashes():
    book = CorporateActionsBook.from_payload(payload(split_record(), dividend_record()))
    assert len(book.actions) == 2
    assert book.provider["name"] == "operator-reviewed"
    assert book.format_version == 1
    assert len(book.actions_sha256()) == 64
    assert len(book.history_sha256()) == 64
    first = book.actions[0].to_dict()
    for key in ("record_id", "ex_date", "source", "source_version", "provenance"):
        assert first[key]


def test_source_hash_is_recorded_for_json_and_bytes_payloads():
    from_bytes = CorporateActionsBook.from_payload(json.dumps(payload()))
    assert len(from_bytes.source_sha256) == 64
    from_dict = CorporateActionsBook.from_payload(payload())
    assert len(from_dict.source_sha256) == 64
    # Same content, different serialisation -> same canonical action hash.
    assert from_bytes.actions_sha256() == from_dict.actions_sha256()


def test_effective_hash_is_independent_of_provider_label_and_ordering():
    a = CorporateActionsBook.from_payload(payload(split_record(), dividend_record()))
    b = CorporateActionsBook.from_payload(
        payload(dividend_record(), split_record(), name="another-provider", version="9")
    )
    assert a.actions_sha256() != b.actions_sha256()  # provider identity is part of provenance
    c = CorporateActionsBook.from_payload(payload(dividend_record(), split_record()))
    assert a.actions_sha256() == c.actions_sha256()  # ordering is not


@pytest.mark.parametrize(
    "bad, exc",
    [
        ({"format": "something-else", "format_version": 1, "provider": {"name": "m", "version": "1"}}, MalformedCorporateAction),
        ({"format": FORMAT_ID, "format_version": 99, "provider": {"name": "m", "version": "1"}}, MalformedCorporateAction),
        ({"format": FORMAT_ID, "format_version": 1}, MalformedCorporateAction),
        ({"format": FORMAT_ID, "format_version": 1, "provider": {"name": "", "version": "1"}}, MalformedCorporateAction),
    ],
)
def test_payload_level_rejections(bad, exc):
    with pytest.raises(exc):
        CorporateActionsBook.from_payload(bad)


@pytest.mark.parametrize(
    "overrides",
    [
        {"record_id": ""},
        {"action_type": "STOCK_OPTIONS"},
        {"market": "LSE"},
        {"ex_date": "not-a-date"},
        {"ex_date": "2024-06-13T00:00:00"},  # naive timestamp
        {"ratio_from": 0},
        {"ratio_to": -2},
        {"ratio_from": 2, "ratio_to": 2},
        {"source": ""},
        {"source_version": ""},
        {"provenance": ""},
        {"cash_per_share": 1.0},  # a split must not carry dividend cash
        {"revision": 0},
    ],
)
def test_record_level_rejections_for_splits(overrides):
    with pytest.raises(MalformedCorporateAction):
        CorporateActionsBook.from_payload(payload(split_record(**overrides)))


@pytest.mark.parametrize(
    "overrides",
    [
        {"cash_per_share": 0},
        {"cash_per_share": -3.0},
        {"currency": "USD"},  # NSE dividend must be INR
        {"currency": ""},
        {"ratio_to": 3},  # a dividend must not carry split ratios
    ],
)
def test_record_level_rejections_for_cash_dividends(overrides):
    with pytest.raises(MalformedCorporateAction):
        CorporateActionsBook.from_payload(payload(dividend_record(**overrides)))


def test_first_publication_cannot_be_announced_after_the_ex_date():
    with pytest.raises(MalformedCorporateAction):
        CorporateActionsBook.from_payload(
            payload(split_record(announced_at="2024-06-20"))
        )


def test_correction_may_be_published_after_the_ex_date():
    book = CorporateActionsBook.from_payload(
        payload(
            split_record(revision=1, announced_at="2024-05-02"),
            split_record(revision=2, ratio_to=4, announced_at="2024-07-01"),
        )
    )
    assert [a.revision for a in book.actions] == [2]
    assert [a.revision for a in book.superseded] == [1]


# --------------------------------------------------------------------------- #
# Duplicate / conflicting / ambiguous / unsupported records
# --------------------------------------------------------------------------- #
def test_identical_duplicate_record_is_rejected():
    with pytest.raises(DuplicateCorporateAction):
        CorporateActionsBook.from_payload(payload(split_record(), split_record()))


def test_duplicate_record_with_divergent_values_is_rejected():
    with pytest.raises(DuplicateCorporateAction):
        CorporateActionsBook.from_payload(
            payload(split_record(), split_record(ratio_to=3))
        )


def test_two_records_claiming_the_same_event_with_different_values_conflict():
    with pytest.raises(ConflictingCorporateAction):
        CorporateActionsBook.from_payload(
            payload(split_record(), split_record(record_id="OTHER-ID", ratio_to=3))
        )


def test_two_records_claiming_the_same_event_with_equal_values_are_ambiguous():
    with pytest.raises(AmbiguousCorporateAction):
        CorporateActionsBook.from_payload(
            payload(split_record(), split_record(record_id="OTHER-ID"))
        )


def test_correction_supersedes_earlier_revision_instead_of_conflicting():
    book = book_with(
        split_record(revision=1, ratio_to=2),
        split_record(revision=2, ratio_to=5),
    )
    assert book.actions[0].ratio_to == 5.0
    assert book.superseded[0].ratio_to == 2.0
    assert book.actions[0].source_version == "2024-06-13"


def test_unsupported_actions_are_labeled_and_never_applied():
    rights = {
        "record_id": "NSE-DEMO-RIGHTS-2024-06-14",
        "market": "NSE",
        "symbol": "DEMO",
        "action_type": "RIGHTS_ISSUE",
        "ex_date": "2024-06-14",
        "source": "filing",
        "source_version": "v1",
        "provenance": "operator transcription",
    }
    book = book_with(rights)
    assert [a.action_type for a in book.unsupported_actions()] == ["RIGHTS_ISSUE"]
    series = build_series(make_bars(), book, policy=AdjustmentPolicy.SPLIT_ADJUSTED)
    assert series.status == AdjustmentStatus.PARTIAL_UNSUPPORTED
    assert series.provenance["actions_applied"] == []
    assert any("UNSUPPORTED_ACTIONS_PRESENT" in note for note in series.provenance["limitations"])
    assert series.provenance["unsupported_actions"][0]["action_type"] == "RIGHTS_ISSUE"
    assert series.provenance["verified"] is False


def test_unsupported_action_carrying_adjustment_values_is_rejected():
    with pytest.raises(MalformedCorporateAction):
        CorporateActionsBook.from_payload(
            payload(
                {
                    "record_id": "NSE-DEMO-MERGER-1",
                    "market": "NSE",
                    "symbol": "DEMO",
                    "action_type": "MERGER",
                    "ex_date": "2024-06-14",
                    "cash_per_share": 10.0,
                    "source": "filing",
                    "source_version": "v1",
                    "provenance": "p",
                }
            )
        )


def test_unknown_action_type_is_rejected_rather_than_guessed():
    with pytest.raises(MalformedCorporateAction):
        CorporateActionsBook.from_payload(payload(split_record(action_type="REVERSE_SPLIT_ISH")))


# --------------------------------------------------------------------------- #
# Split arithmetic
# --------------------------------------------------------------------------- #
def test_split_preserves_economic_value_and_updates_price_and_volume():
    bars = make_bars()
    book = book_with(split_record())
    series = build_series(bars, book, policy=AdjustmentPolicy.SPLIT_ADJUSTED)
    pre = [b for b in series.bars if b.timestamp < EX_SPLIT]
    post = [b for b in series.bars if b.timestamp >= EX_SPLIT]
    assert pre and post
    for bar in pre:
        assert bar.close == pytest.approx(50.0)
        assert bar.volume == pytest.approx(2000.0)
        # price x volume is unchanged by the split
        assert bar.close * bar.volume == pytest.approx(100.0 * 1000.0)
    for bar in post:  # bars on/after the ex-date are untouched
        assert bar.close == pytest.approx(100.0)
        assert bar.volume == pytest.approx(1000.0)
    assert series.provenance["volume_adjusted"] is True
    assert series.provenance["actions_applied"][0]["bars_adjusted"] == len(pre)


def test_split_updates_simulated_holdings_without_changing_position_value():
    book = book_with(split_record())
    quantity, average_price = apply_split_to_position(100, 200.0, book.actions[0])
    assert quantity == pytest.approx(200.0)
    assert average_price == pytest.approx(100.0)
    assert quantity * average_price == pytest.approx(20000.0)


def test_raw_policy_leaves_split_untouched():
    bars = make_bars()
    series = build_series(bars, book_with(split_record()), policy=AdjustmentPolicy.RAW)
    assert [b.close for b in series.bars] == [b.close for b in bars]
    assert series.provenance["actions_applied"] == []
    assert series.status == AdjustmentStatus.RAW
    assert series.provenance["prices_adjusted"] is False


# --------------------------------------------------------------------------- #
# Dividends: price basis vs cash, never both
# --------------------------------------------------------------------------- #
def test_dividend_price_adjustment_is_consistent_and_credits_no_cash():
    bars = make_bars()
    book = book_with(dividend_record())
    series = build_series(bars, book, policy=AdjustmentPolicy.TOTAL_RETURN_ADJUSTED)
    prev_close = 100.0
    expected = prev_close * (1 - 5.0 / prev_close)
    pre = [b for b in series.bars if b.timestamp < EX_DIV]
    assert all(b.close == pytest.approx(expected) for b in pre)
    assert series.provenance["dividend_price_adjustment"] is True
    assert series.provenance["dividends_as_cash"] is False
    assert series.provenance["cash_dividend_convention"] == "PRICE_ADJUSTED_NO_CASH"
    # No cash is credited when the dividend already sits in the price basis.
    assert series.provenance["cash_dividend_events"] == []


def test_dividend_cash_convention_never_touches_prices():
    bars = make_bars()
    book = book_with(dividend_record())
    series = build_series(bars, book, policy=AdjustmentPolicy.SPLIT_ADJUSTED)
    assert [b.close for b in series.bars] == [b.close for b in bars]
    assert series.provenance["dividends_as_cash"] is True
    assert series.provenance["dividend_price_adjustment"] is False
    events = dividend_cash_events(book, market="NSE", symbol="DEMO")
    assert events == [
        {
            "record_id": "NSE-DEMO-DIV-2024-06-17",
            "ex_date": EX_DIV.isoformat(),
            "cash_per_share": 5.0,
            "currency": "INR",
            "source": "NSE corporate-action filing",
            "source_version": "2024-06-17",
        }
    ]


def test_double_counting_dividends_is_refused():
    with pytest.raises(CorporateActionError):
        assert_no_double_counting(
            {"dividend_price_adjustment": True, "dividends_as_cash": True}
        )
    assert_no_double_counting({"dividend_price_adjustment": True, "dividends_as_cash": False})
    assert_no_double_counting({"dividend_price_adjustment": False, "dividends_as_cash": True})


def test_dividend_without_a_prior_close_is_not_adjusted_and_is_reported():
    bars = [b for b in make_bars() if b.timestamp >= EX_DIV]
    series = build_series(
        bars, book_with(dividend_record()), policy=AdjustmentPolicy.TOTAL_RETURN_ADJUSTED
    )
    assert series.provenance["actions_applied"] == []
    assert series.status == AdjustmentStatus.PARTIAL_UNSUPPORTED
    assert any(
        "ADJUSTMENT_NOT_DEFENSIBLE" in note for note in series.provenance["limitations"]
    )


def test_dividend_larger_than_prior_close_is_refused_not_invented():
    bars = make_bars(price=2.0)
    series = build_series(
        bars,
        book_with(dividend_record(cash_per_share=5.0)),
        policy=AdjustmentPolicy.TOTAL_RETURN_ADJUSTED,
    )
    assert series.provenance["actions_applied"] == []
    assert series.status == AdjustmentStatus.PARTIAL_UNSUPPORTED
    assert any("ADJUSTMENT_NOT_DEFENSIBLE" in n for n in series.provenance["limitations"])
    assert [b.close for b in series.bars] == [b.close for b in bars]


# --------------------------------------------------------------------------- #
# Raw vs adjusted cannot be confused by an execution path
# --------------------------------------------------------------------------- #
def test_adjusted_series_refuses_to_be_used_as_execution_prices():
    adjusted = build_series(make_bars(), book_with(split_record()), policy="SPLIT_ADJUSTED")
    with pytest.raises(AdjustedSeriesError):
        adjusted.for_execution()
    with pytest.raises(AdjustedSeriesError):
        assert_raw_candles(adjusted, context="paper")

    raw = build_series(make_bars(), book_with(split_record()), policy="RAW")
    assert raw.for_execution() == raw.bars
    assert assert_raw_candles(raw) == raw.bars


def test_plain_candle_lists_pass_the_execution_guard():
    bars = make_bars()
    assert assert_raw_candles(bars) == bars


# --------------------------------------------------------------------------- #
# Look-ahead safety
# --------------------------------------------------------------------------- #
def test_action_not_yet_effective_cannot_adjust_earlier_bars():
    before_ex = START + timedelta(days=5)  # ex-date is 8 days after START
    series = build_series(
        make_bars(), book_with(split_record()), policy="SPLIT_ADJUSTED", as_of=before_ex
    )
    assert [b.close for b in series.bars] == [b.close for b in make_bars()]
    assert series.provenance["actions_applied"] == []
    assert series.provenance["pending_announced_actions"] == [
        {
            "record_id": "NSE-DEMO-SPLIT-2024-06-13",
            "action_type": "SPLIT",
            "ex_date": EX_SPLIT.isoformat(),
            "announced_at": datetime(2024, 5, 2, tzinfo=UTC).isoformat(),
        }
    ]


def test_action_becomes_applicable_at_its_ex_date():
    at_ex = EX_SPLIT
    series = build_series(
        make_bars(), book_with(split_record()), policy="SPLIT_ADJUSTED", as_of=at_ex
    )
    pre = [b for b in series.bars if b.timestamp < EX_SPLIT]
    assert all(b.close == pytest.approx(50.0) for b in pre)
    assert series.provenance["actions_excluded_point_in_time"] == []


def test_undated_announcement_still_requires_the_ex_date_to_have_passed():
    record = split_record(announced_at=None)
    early = build_series(
        make_bars(),
        book_with(record),
        policy="SPLIT_ADJUSTED",
        as_of=START + timedelta(days=5),
    )
    assert early.provenance["actions_applied"] == []
    assert [b.close for b in early.bars] == [b.close for b in make_bars()]

    at_ex = build_series(make_bars(), book_with(record), policy="SPLIT_ADJUSTED", as_of=EX_SPLIT)
    assert [a["action_type"] for a in at_ex.provenance["actions_applied"]] == ["SPLIT"]


def test_later_correction_does_not_rewrite_earlier_decisions():
    book = book_with(
        split_record(revision=1, ratio_to=2, announced_at="2024-05-02"),
        split_record(revision=2, ratio_to=4, announced_at="2024-07-01"),
    )
    early = book.as_of(datetime(2024, 6, 20, tzinfo=UTC))  # after ex-date, before correction
    assert [a.ratio_to for a in early.actions] == [2.0]
    late = book.as_of(datetime(2024, 8, 1, tzinfo=UTC))
    assert [a.ratio_to for a in late.actions] == [4.0]

    early_series = build_series(
        make_bars(20), book, policy="SPLIT_ADJUSTED", as_of=datetime(2024, 6, 20, tzinfo=UTC)
    )
    late_series = build_series(
        make_bars(20), book, policy="SPLIT_ADJUSTED", as_of=datetime(2024, 8, 1, tzinfo=UTC)
    )
    assert early_series.bars[0].close == pytest.approx(50.0)
    assert late_series.bars[0].close == pytest.approx(25.0)


def test_same_ticker_in_two_markets_without_a_market_is_never_guessed():
    book = book_with(split_record(market="NSE"), split_record(market="NASDAQ", record_id="NDQ-1"))
    series = build_series(make_bars(), book, policy="SPLIT_ADJUSTED")
    assert series.status == AdjustmentStatus.UNVERIFIED
    assert series.provenance["actions_applied"] == []
    assert [b.close for b in series.bars] == [b.close for b in make_bars()]
    assert any("AMBIGUOUS_MARKET_SCOPE" in n for n in series.provenance["limitations"])

    scoped = build_series(make_bars(), book, policy="SPLIT_ADJUSTED", market="NSE")
    assert scoped.status == AdjustmentStatus.SPLIT_ADJUSTED
    assert [b.close for b in scoped.bars[:1]] == [50.0]


def test_provenance_states_the_look_ahead_rule():
    series = build_series(make_bars(), book_with(split_record()), policy="RAW")
    assert "ex_date <= as_of" in series.provenance["look_ahead_rule"]


# --------------------------------------------------------------------------- #
# Missing / unverified data
# --------------------------------------------------------------------------- #
def test_missing_action_data_is_explicitly_unverified():
    series = build_series(make_bars(), None)
    assert series.status == AdjustmentStatus.UNVERIFIED
    assert series.provenance["verified"] is False
    assert any(
        "NO_CORPORATE_ACTION_DATA" in note for note in series.provenance["limitations"]
    )
    assert series.provenance["actions_sha256"] == ""


def test_feed_without_records_for_the_instrument_stays_unverified():
    from app.data.corporate_actions import dataset_block

    book = book_with(split_record(symbol="OTHER"))
    block = dataset_block(
        make_bars(), book, policy="SPLIT_ADJUSTED", market="NSE", symbol="DEMO"
    )
    assert block["status"] == AdjustmentStatus.UNVERIFIED.value
    assert any("NO_CORPORATE_ACTION_DATA_FOR_INSTRUMENT" in n for n in block["limitations"])


def test_claim_without_verified_data_is_not_honoured():
    from app.data.corporate_actions import dataset_block

    block = dataset_block(
        make_bars(), None, policy="RAW", market="NSE", symbol="DEMO",
        claim="SPLIT_AND_DIVIDEND_ADJUSTED",
    )
    assert block["claim_honoured"] is False
    assert block["verified"] is False
    assert any("UNVERIFIED_ADJUSTMENT_CLAIM" in n for n in block["limitations"])


def test_legacy_meta_without_a_block_is_reported_as_unverified():
    block = block_for_meta({"corporate_action_adjustment": "PROVIDER_DEFAULT_UNVERIFIED"})
    assert block["status"] == "UNVERIFIED"
    assert block["claim_honoured"] is True  # the claim itself said "unverified"
    assert any("NO_CORPORATE_ACTION_DATA" in n for n in block["limitations"])
    synthetic = block_for_meta({"corporate_action_adjustment": "NOT_APPLICABLE_SYNTHETIC"})
    assert any("NOT_APPLICABLE_SYNTHETIC" in n for n in synthetic["limitations"])


def test_canonical_bars_hash_is_order_and_format_sensitive():
    bars = make_bars()
    assert canonical_bars_sha256(bars) == canonical_bars_sha256(list(bars))
    assert canonical_bars_sha256(bars) != canonical_bars_sha256(bars[:-1])


def test_dataset_block_records_hashes_and_conventions():
    from app.data.corporate_actions import dataset_block

    block = dataset_block(
        make_bars(),
        book_with(split_record(), dividend_record()),
        policy="SPLIT_ADJUSTED",
        market="NSE",
        symbol="DEMO",
        claim="SPLIT_ADJUSTED",
    )
    assert block["status"] == AdjustmentStatus.SPLIT_ADJUSTED.value
    assert block["verified"] is True
    assert len(block["actions_sha256"]) == 64
    assert len(block["actions_history_sha256"]) == 64
    assert len(block["dataset_bars_sha256"]) == 64
    assert block["adjusted_bars_sha256"] != block["dataset_bars_sha256"]
    assert block["raw_bars_for_execution"] is True
    assert block["dividends_as_cash"] is True
    assert "RAW_PRICES_PLUS_DIVIDEND_CASH_EVENTS" in block["dividend_accounting"]
    assert block["cash_dividend_events"][0]["cash_per_share"] == 5.0
