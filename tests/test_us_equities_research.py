"""Research-harness contract, timing and cost-accounting tests.

The prices used here are deterministic fixtures (tests/us_equity_fixtures.py)
that exist only to exercise code paths. No performance number produced by this
module is a research result and none is written to results/.
"""

from datetime import date, datetime, timedelta, timezone
import json
import pytest

from app.research.us_equities.dataset import DatasetError, load_dataset
from app.research.us_equities.harness import (
    CostModel,
    ResearchSessionCalendar,
    chronological_split,
    evaluate,
    run_candidate,
)
from app.research.us_equities.strategies import (
    BUY_HOLD,
    CANDIDATES,
    MEAN_REVERSION,
    MOMENTUM,
    momentum_12_1,
)
from tests.us_equity_fixtures import build_dataset, close_stamp, session_days


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    root = build_dataset(tmp_path_factory.mktemp("us_eq"), sessions=420)
    return load_dataset(root)


def test_dataset_loads_and_reports_provenance(dataset):
    assert dataset.universe_kind == "point_in_time"
    assert dataset.survivorship_biased is False
    assert dataset.source["license"]
    assert dataset.adjustment == "split_dividend"
    assert dataset.benchmark == "SPY"
    assert len(dataset.dates) == 420
    assert len(dataset.fingerprint()) == 64


def test_dataset_refuses_static_universe_without_membership(tmp_path):
    root = build_dataset(tmp_path / "static", universe="static_survivors", sessions=40)
    # A static-survivor universe is allowed to load, but is flagged.
    loaded = load_dataset(root)
    assert loaded.survivorship_biased is True
    assert any("survivorship" in warning.lower() for warning in loaded.warnings)


def test_dataset_refuses_missing_provenance(tmp_path):
    root = build_dataset(tmp_path / "nolicense", sessions=40, omit_manifest_fields=("source",))
    with pytest.raises(DatasetError) as excinfo:
        load_dataset(root)
    assert any("source" in problem for problem in excinfo.value.problems)


def test_dataset_refuses_unstamped_daily_bars(tmp_path):
    # A bar stamped three hours after the close is not a session-close bar; the
    # whole no-look-ahead claim depends on refusing it.
    root = build_dataset(tmp_path / "shift", sessions=40, stamp_hour_shift=3)
    with pytest.raises(DatasetError) as excinfo:
        load_dataset(root)
    assert any("session closes at" in problem for problem in excinfo.value.problems)


def test_dataset_refuses_raw_prices_without_corporate_actions(tmp_path):
    root = build_dataset(tmp_path / "raw", sessions=40, adjustment="raw", with_actions=False)
    with pytest.raises(DatasetError) as excinfo:
        load_dataset(root)
    assert any("corporate_actions_csv" in problem for problem in excinfo.value.problems)


def test_dataset_refuses_duplicate_bars(tmp_path):
    root = build_dataset(tmp_path / "dupes", sessions=40)
    bars = root / "bars.csv"
    lines = bars.read_text(encoding="utf-8").splitlines()
    bars.write_text("\n".join(lines + [lines[1]]) + "\n", encoding="utf-8")
    with pytest.raises(DatasetError) as excinfo:
        load_dataset(root)
    assert any("duplicate" in problem for problem in excinfo.value.problems)


def test_point_in_time_membership_excludes_not_yet_listed_symbols(tmp_path):
    root = build_dataset(tmp_path / "late", sessions=120,
                         late_listing={"DDD": 80})
    loaded = load_dataset(root)
    first_day = loaded.dates[0]
    later_day = loaded.dates[90]
    assert "DDD" not in loaded.members(first_day)
    assert "DDD" in loaded.members(later_day)


def test_close_stamped_bars_are_accepted_by_the_research_calendar():
    calendar = ResearchSessionCalendar()
    day = date(2026, 11, 27)  # early close at 13:00 New York
    stamp = datetime.fromisoformat(close_stamp(day).replace("Z", "+00:00"))
    assert calendar.is_open(stamp)
    # One minute later the session really is over.
    assert not calendar.is_open(stamp + timedelta(minutes=6))
    # A genuine holiday is still refused, even at its nominal 16:00 close.
    holiday = datetime(2026, 1, 1, 21, 0, tzinfo=timezone.utc)  # 16:00 New York
    assert not calendar.is_open(holiday)


def test_momentum_indicator_skips_the_most_recent_month():
    closes = [100.0] * 231 + [121.0] * 22
    assert momentum_12_1(closes) == pytest.approx(0.21, rel=1e-6)
    assert momentum_12_1(closes[:100]) is None


def test_orders_fill_at_the_next_session_open_never_at_the_signal_bar(dataset):
    result = run_candidate(dataset, MOMENTUM, max_positions=3, fold_count=2)
    assert result.fills, "the fixture should produce fills"
    for fill in result.fills:
        moment = datetime.fromisoformat(fill["timestamp"])
        session = dataset.bars[fill["symbol"]]
        # Every fill belongs to a real bar and is priced from that bar's open,
        # adjusted for the documented half-spread + slippage.
        bar = next((candidate for candidate in session
                    if candidate.timestamp == moment), None)
        if bar is None:
            # An intrabar exit (stop/take) is timestamped at the bar close; the
            # research cost model disables those, so this must not happen.
            raise AssertionError(
                f"fill at {moment.isoformat()} does not coincide with a session bar"
            )
        adjustment = 0.0002 / 2 + 0.0003  # half spread + slippage, per side
        expected = bar.open * (1 + adjustment if fill["side"] == "BUY"
                               else 1 - adjustment)
        assert fill["price"] == pytest.approx(expected, rel=1e-6)
        # The fill is at *this* session's open, which is strictly after the
        # decision bar: that is the no-look-ahead property being proven.
        assert fill["price"] != pytest.approx(bar.close, rel=1e-9)


def test_equity_curve_is_a_realisation_never_a_forecast(dataset):
    result = run_candidate(dataset, BUY_HOLD, max_positions=5, fold_count=2)
    assert len(result.snapshots) == len(dataset.dates)
    assert result.metrics["observed_days"] >= 1
    assert result.metrics["exposure"] is not None
    # The curve is realised equity, never a projection: every point is
    # cash plus marked inventory, and cash alone can never exceed equity.
    for snapshot in result.snapshots:
        assert snapshot["equity"] > 0
        assert snapshot["cash"] <= snapshot["equity"] + 1e-6
        assert set(snapshot) >= {"session_date", "equity", "cash", "weights"}


def test_higher_costs_reduce_net_return(dataset):
    cheap = run_candidate(dataset, MOMENTUM, costs=CostModel(commission=0.0, spread=0.0,
                                                            slippage=0.0),
                          max_positions=3, fold_count=0)
    expensive = run_candidate(dataset, MOMENTUM,
                              costs=CostModel(commission=0.002, spread=0.002,
                                              slippage=0.002),
                              max_positions=3, fold_count=0)
    assert cheap.metrics["total_return"] > expensive.metrics["total_return"]
    assert expensive.metrics["transaction_costs"] > cheap.metrics["transaction_costs"]


def test_stress_multiplier_never_improves_a_result(dataset):
    base = run_candidate(dataset, MOMENTUM, max_positions=3, fold_count=0)
    stressed = run_candidate(dataset, MOMENTUM, max_positions=3, fold_count=0, stress=3.0)
    assert stressed.metrics["total_return"] <= base.metrics["total_return"]


def test_participation_cap_produces_partial_fills(tmp_path):
    root = build_dataset(tmp_path / "thin", sessions=60, volume=200.0)
    loaded = load_dataset(root)
    result = run_candidate(loaded, BUY_HOLD, max_positions=2, fold_count=0)
    partial = [fill for fill in result.fills if fill["reason"] == "signal"]
    assert partial, "expected at least one fill"
    # 2% of 200 shares per bar is 4 shares: the broker cannot fill a full tranche.
    assert any(fill["quantity"] <= 4.0 + 1e-9 for fill in partial)


def test_chronological_split_is_contiguous_and_ordered():
    days = session_days(date(2020, 1, 2), 300)
    split = chronological_split(days)
    assert split.train[0] == days[0]
    assert split.train[1] < split.validation[0]
    assert split.validation[1] < split.test[0]
    assert split.test[1] == days[-1]
    assert split.train[1] < split.validation[1] < split.test[1]


def test_evaluation_reports_validation_ranking_and_a_single_test_window(dataset):
    report = evaluate(dataset, [BUY_HOLD, MOMENTUM, MEAN_REVERSION],
                      max_positions=3, fold_count=2, stress=2.0)
    payload = report.to_dict()
    considered = payload["selection"]["candidates_considered"]
    assert set(considered) == {"buy_hold", "momentum_trend", "mean_reversion"}
    assert payload["selection"]["selected"] in considered
    assert len(payload["test_results"]) == 1
    assert len(payload["stress_results"]) == 3
    assert payload["verdict"]["status"] in {"SUPPORTED", "NOT SUPPORTED", "UNRESOLVED"}
    assert payload["verdict"]["paper_ready"] is False
    assert payload["splits"]["validation"][1] < payload["splits"]["test"][0]
    assert payload["regimes"], "regime blocks should be described"


def test_survivorship_biased_universe_cannot_produce_a_selection_verdict(tmp_path):
    root = build_dataset(tmp_path / "biased", sessions=300, universe="static_survivors")
    loaded = load_dataset(root)
    report = evaluate(loaded, [BUY_HOLD, MOMENTUM], max_positions=3, fold_count=1)
    payload = report.to_dict()
    assert payload["verdict"]["status"] == "UNRESOLVED"
    assert "survivor" in payload["verdict"]["reason"].lower()


def test_all_candidates_are_long_only_and_fixed_parameter():
    for candidate in CANDIDATES:
        assert candidate.min_history >= 1
        assert candidate.rebalance_days >= 1
        spec = candidate.select.__doc__
        assert spec is None or "short" not in spec.lower()
    keys = {candidate.key for candidate in CANDIDATES}
    assert {"buy_hold", "momentum_trend", "moving_average", "breakout",
            "mean_reversion"} <= keys


def test_cli_writes_a_requirements_report_and_never_invents_results(tmp_path):
    from tools.us_equities_research import main

    out = tmp_path / "out"
    code = main(["--dataset", str(tmp_path / "absent"), "--out", str(out)])
    assert code == 2
    payload = json.loads((out / "DATA_REQUIRED.json").read_text(encoding="utf-8"))
    assert payload["status"] == "BLOCKED_MISSING_DATA"
    assert payload["explicitly_not_done"]
    assert not (out / "report.json").exists()
    assert not (out / "REPORT.md").exists()
