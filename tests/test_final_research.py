"""FINAL: decision replay, research director, attribution, command center,
charts, period reports — over recorded rows only."""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.database.database import Database
from app.database.repository import Repository

BASE = datetime(2026, 1, 2, 9, 15, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


@pytest.fixture()
def env(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/research.db")
    repository = Repository(database)
    yield repository
    database.close()


def seed_trades(repository):
    repository.record_system_event("TRADE_OUTCOME",
                                   {"strategy": "momentum",
                                    "params_version": "momentum-v1",
                                    "symbol": "RELIANCE",
                                    "regime": "trend_up_normal",
                                    "duration_bars": 4, "pnl": 12.5,
                                    "fees": 0.5},
                                   ts=iso(BASE))
    repository.record_system_event("TRADE_OUTCOME",
                                   {"strategy": "rsi",
                                    "params_version": "rsi-v14",
                                    "symbol": "TCS",
                                    "regime": "range_low_vol",
                                    "duration_bars": 30, "pnl": -3.0,
                                    "fees": 0.5},
                                   ts=iso(BASE + timedelta(minutes=5)))


class TestDecisionReplay:
    def _signal(self, repository, signal_id, ts):
        return repository.record_signal({
            "id": signal_id, "ts": iso(ts), "strategy": "momentum",
            "params_version": "pv1", "symbol": "RELIANCE", "timeframe": "1m",
            "regime": "range_low_vol", "signal": "BUY", "price": 100.0,
            "meta": {"period": 2}})

    def test_replay_uses_only_at_decision_time_info(self, env):
        from app.research.replay import DecisionReplay

        self._signal(env, "sig-1", BASE)
        # later regime observation must NOT leak into the replay
        env.record_regime_observation(iso(BASE + timedelta(hours=1)),
                                      "RELIANCE", "trend_up_normal",
                                      {"trend": 1.0})
        env.record_system_event("RISK_REJECT", {"reason": "exposure cap"},
                                ts=iso(BASE + timedelta(seconds=30)))
        replay = DecisionReplay(env).replay_signal("sig-1")
        assert replay["found"] is True
        assert replay["signal"]["regime_recorded_then"] == "range_low_vol"
        assert replay["known_at_decision"]["price"] == 100.0
        assert replay["risk_decision"]["kind"] == "RISK_REJECT"
        assert "never" in replay["disclaimer"]

    def test_unknown_id_is_honest(self, env):
        from app.research.replay import DecisionReplay

        replay = DecisionReplay(env).replay_signal("nope")
        assert replay == {"found": False,
                          "reason": "signal id not present in recorded rows"}

    def test_risk_decision_outside_window_is_none(self, env):
        from app.research.replay import DecisionReplay

        self._signal(env, "sig-2", BASE)
        env.record_system_event("SANDBOX_ORDER_SUBMITTED", {"order_id": "x"},
                                ts=iso(BASE + timedelta(minutes=10)))
        replay = DecisionReplay(env).replay_signal("sig-2")
        assert replay["risk_decision"] is None


class TestAttribution:
    def test_dimensions_and_totals(self, env):
        from app.research.attribution import Attribution

        seed_trades(env)
        result = Attribution(env).attribute()
        assert result["total_trades"] == 2
        assert result["total_net_pnl"] == pytest.approx(9.5)
        strategy_dim = result["dimensions"]["strategy"]
        assert strategy_dim["momentum"]["net_pnl"] == pytest.approx(12.5)
        assert strategy_dim["rsi"]["net_pnl"] == pytest.approx(-3.0)
        assert result["dimensions"]["duration"]["short<=5"]["net_pnl"] == \
            pytest.approx(12.5)
        assert result["dimensions"]["regime"]["range_low_vol"]["net_pnl"] == \
            pytest.approx(-3.0)
        assert sum(v["fees"] for v in
                   result["dimensions"]["strategy"].values()) == pytest.approx(1.0)

    def test_empty_is_zero_not_crash(self, env):
        from app.research.attribution import Attribution

        result = Attribution(env).attribute()
        assert result["total_trades"] == 0


class TestCommandCenter:
    def test_why_losing_uses_recorded_rows(self, env):
        from app.research.command_center import CommandCenter

        seed_trades(env)
        answer = CommandCenter(env).ask("Why did Arena lose money today?")
        assert "9.50" in answer["answer"] or "9.5" in answer["answer"]
        assert answer["labels"]

    def test_contribution_and_unknown(self, env):
        from app.research.command_center import CommandCenter

        seed_trades(env)
        answer = CommandCenter(env).ask("What contributed to today's P&L?")
        assert "momentum" in answer["answer"]
        unknown = CommandCenter(env).ask("What is the meaning of life?")
        assert unknown["labels"] == ["HONEST"]
        assert "Ask about" in unknown["answer"]

    def test_answers_never_fabricate_when_empty(self, env):
        from app.research.command_center import CommandCenter

        answer = CommandCenter(env).ask("Why did Arena lose money today?")
        assert "No recorded trade outcomes" in answer["answer"] or \
            "no recorded" in answer["answer"].lower()


class TestDirector:
    def test_survey_creates_idempotent_questions(self, env):
        from app.research.director import ResearchDirector

        director = ResearchDirector(env)
        first = director.survey()
        assert first, "empty candidate/regime state must still raise questions"
        second = director.survey()
        assert second == [], "survey is idempotent by question text"
        assert len(env.list_research_questions(limit=50)) == len(first)

    def test_schedule_records_experiment_or_blocks_honestly(self, env):
        from app.research.director import ResearchDirector

        director = ResearchDirector(env)
        question = director.survey()[0]
        result = director.schedule(question["id"])  # no candles → honest block
        assert result["scheduled"] is False
        assert "blocked" in result["reason"] or "no local data" in result["reason"]


class TestCharts:
    def test_line_and_bar_svg(self):
        from app.reporting.charts import bar_chart_svg, line_chart_svg

        svg = line_chart_svg("Equity", [(1, 100.0), (2, 101.5), (3, 99.0)])
        assert svg.startswith("<svg") and "polyline" in svg
        empty = line_chart_svg("Equity", [])
        assert "no data" in empty
        bars = bar_chart_svg("NET by strategy", [("momentum", 9.5), ("rsi", -3.0)])
        assert "rect" in bars
        assert "<script" not in bars and "<script" not in svg  # no injection

    def test_labels_are_escaped(self):
        from app.reporting.charts import bar_chart_svg

        bars = bar_chart_svg("t", [("<b>x</b>", 1.0)])
        assert "&lt;b&gt;" in bars and "<b>" not in bars


class TestPeriodReports:
    def test_daily_markdown_adds_attribution_and_anomalies(self, env):
        from app.reporting.weekly import PeriodReport

        seed_trades(env)
        report = PeriodReport(env, days=1, title="Daily Report")
        text = report.markdown(today=date(2026, 1, 3))
        assert "## Attribution (Daily Report)" in text
        assert "momentum" in text
        assert "CALCULATED" in text

    def test_monthly_pdf_bytes(self, env):
        from app.reporting.weekly import PeriodReport

        report = PeriodReport(env, days=30, title="Monthly Report")
        payload = report.pdf(today=date(2026, 1, 31))
        assert payload[:5] == b"%PDF-"

    def test_weekly_report_unchanged_contract(self, env):
        from app.reporting.weekly import WeeklyReport

        report = WeeklyReport(env)
        assert hasattr(report, "markdown") and hasattr(report, "pdf")
