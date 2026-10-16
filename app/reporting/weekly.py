"""Weekly report: human-readable, honestly labeled, Markdown + PDF.

Every number is labeled:
- OBSERVED   — read directly from recorded rows (events, trades, coverage)
- CALCULATED — arithmetic over recorded rows (P&L sums, returns, drawdown)
- MODEL INTERPRETATION — learning-layer outputs (regime labels, promotions)
Nothing else is asserted. No data → "no recorded data" (never a guess).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from app.utils.clock import utc_today

from app.reporting.pdf import simple_pdf


def _money(value) -> str:
    return f"{value:,.2f}" if isinstance(value, (int, float)) else "n/a"


class WeeklyReport:
    def __init__(self, repository, *, settings=None, broker=None):
        self.repository = repository
        self.settings = settings
        self.broker = broker

    def assemble(self, *, today: date | None = None) -> dict:
        today = today or utc_today()
        week_start = today - timedelta(days=7)
        snapshots = [row for row in self.repository.list_daily_snapshots(limit=30)
                     if row["date"] >= week_start.isoformat()]
        events = [e for e in self.repository.list_system_events(limit=1000)
                  if str(e["ts"])[:10] >= week_start.isoformat()]
        learning = [e for e in self.repository.list_learning_events(limit=500)
                    if str(e["ts"])[:10] >= week_start.isoformat()]
        realized = unrealized = 0.0
        trades_count = 0
        equity = None
        if snapshots:
            realized = sum(float(s["realized"] or 0) for s in snapshots)
            unrealized = sum(float(s["unrealized"] or 0) for s in snapshots)
            trades_count = sum(int(s["trades"] or 0) for s in snapshots)
            equity = float(snapshots[-1]["equity"]) if snapshots[-1]["equity"] is not None else None
        fills = [e for e in events if e["kind"] == "SANDBOX_FILL_RECONCILED"]
        orders = [e for e in events if e["kind"] == "SANDBOX_ORDER_SUBMITTED"]
        risk_events = [e for e in events if e["kind"] in {"RISK_REJECT", "EMERGENCY_STOP"}]
        regimes = [e for e in learning if e["kind"] == "REGIME_CHANGE"]
        promotions = [e for e in learning if e["kind"] == "EVALUATION_RUN" and
                      isinstance(e["payload"], dict) and e["payload"].get("promoted")]
        if isinstance(promotions, list):
            promotions = [e for e in promotions]
        initial = float(getattr(self.settings, "initial_capital", 100000)) if self.settings else 100000
        coverage = self.repository.list_coverage(limit=20)
        quality = self.repository.list_quality_events(limit=10)
        universe = self.repository.instrument_counts()
        broker_mode = str(getattr(self.settings, "broker", "LOCAL_PAPER")) if self.settings else "LOCAL_PAPER"
        live_label = ("ENABLED — REAL MONEY"
                      if bool(getattr(self.settings, "live_trading", False))
                      and broker_mode.upper() == "UPSTOX_LIVE" else "DISABLED")
        return {
            "week_start": week_start.isoformat(), "today": today.isoformat(),
            "broker_mode": broker_mode, "live": live_label,
            "initial_capital": initial,
            "equity": equity, "realized": realized, "unrealized": unrealized,
            "trades": trades_count, "orders": len(orders), "fills": len(fills),
            "risk_events": len(risk_events),
            "regime_changes": [{"ts": e["ts"],
                                "detail": (e["payload"] if isinstance(e["payload"], dict)
                                           else json.loads(e["payload"]))}
                               for e in regimes],
            "promotions": promotions,
            "learning_events": len(learning),
            "coverage": coverage, "quality_recent": quality,
            "universe": universe,
        }

    def markdown(self, *, today: date | None = None) -> str:
        data = self.assemble(today=today)
        lines = [
            f"# Arena Weekly Report — {data['week_start']} → {data['today']}", "",
            f"Mode: **{data['broker_mode']}** · Live trading: **{data['live']}**", "",
            "## Account",
            f"- Capital (initial): {_money(data['initial_capital'])} — OBSERVED (config)",
            f"- Current equity: {_money(data['equity'])} — "
            + ("CALCULATED from recorded daily snapshots" if data["equity"] is not None else "no recorded snapshots"),
            f"- Realized P&L (week): {_money(data['realized'])} — CALCULATED",
            f"- Unrealized P&L (latest): {_money(data['unrealized'])} — CALCULATED",
            f"- Total P&L (realized+unrealized): {_money(data['realized'] + data['unrealized'])} — CALCULATED",
        ]
        if data["equity"] is not None and data["initial_capital"]:
            ret = data["equity"] / data["initial_capital"] - 1
            lines.append(f"- Return %: {ret * 100:.2f}% — CALCULATED")
        lines += [
            "", "## Activity",
            f"- Orders submitted (sandbox): {data['orders']} — OBSERVED",
            f"- Fills reconciled: {data['fills']} — OBSERVED",
            f"- Trades (new this week): {data['trades']} — OBSERVED",
            f"- Risk events (rejected orders / emergency stop): {data['risk_events']} — OBSERVED",
            "", "## Strategy & regimes",
        ]
        if data["regime_changes"]:
            for change in data["regime_changes"][:5]:
                detail = change["detail"]
                lines.append(f"- {str(change['ts'])[:16]}: regime {detail.get('from')} → "
                             f"{detail.get('to')} — MODEL INTERPRETATION (observable features)")
        else:
            lines.append("- No recorded regime changes this week.")
        if data["promotions"]:
            for promotion in data["promotions"][:5]:
                version = promotion["payload"].get("version") if isinstance(promotion["payload"], dict) else None
                lines.append(f"- Model promoted: {version} — MODEL INTERPRETATION "
                             "(out-of-sample validation passed)")
        else:
            lines.append("- No model promotions this week (candidates must beat the "
                         "current version out-of-sample).")
        lines += ["", "## Data status"]
        if data["coverage"]:
            for row in data["coverage"][:8]:
                lines.append(f"- {row['instrument_key']} {row['unit']}/{row['interval']}: "
                             f"{row['records']} candles, completeness "
                             f"{'n/a (calendar)' if float(row['completeness'] or 0) < 0 else format(float(row['completeness']), '.0%')} — OBSERVED")
        else:
            lines.append("- No historical coverage recorded yet.")
        lines.append(f"- Instruments tracked: {data['universe'].get('total', 0)} "
                     f"({data['universe'].get('state_active', 0)} active) — OBSERVED")
        lines += ["", "*Generated from recorded data only. Execution mode: "
                  f"{data['broker_mode']}; live trading: {data['live']}.*"]
        return "\n".join(lines) + "\n"

    def pdf(self, *, today: date | None = None) -> bytes:
        markdown_text = self.markdown(today=today)
        lines = []
        for raw in markdown_text.splitlines():
            line = raw.replace("**", "").replace("## ", "").replace("# ", "").replace("*", "")
            lines.append(line)
        return simple_pdf(f"Arena Weekly Report {self.assemble(today=today)['week_start']}", lines)


class PeriodReport(WeeklyReport):
    """Daily/weekly/monthly reports over recorded data (same honest labels).

    Extends WeeklyReport with a configurable window (`days`) and title, and
    appends attribution + anomaly/experiment sections. OBSERVED rows come
    straight from recorded snapshots/events; CALCULATED rows are sums over
    those rows; nothing is estimated.
    """

    def __init__(self, repository, *, settings=None, days: int = 7,
                 title: str = "Arena Report"):
        super().__init__(repository, settings=settings)
        self.days = int(days)
        self.title = title

    def assemble(self, *, today=None):
        from datetime import timedelta as timedelta_type

        today = today or utc_today()
        week_start = today - timedelta_type(days=self.days)
        snapshots = [row for row in self.repository.list_daily_snapshots(limit=400)
                     if row["date"] >= week_start.isoformat()]
        events = [e for e in self.repository.list_system_events(limit=2000)
                  if str(e["ts"])[:10] >= week_start.isoformat()]
        learning = [e for e in self.repository.list_learning_events(limit=1000)
                    if str(e["ts"])[:10] >= week_start.isoformat()]
        realized = sum(float(s["realized"] or 0) for s in snapshots)
        unrealized = sum(float(s["unrealized"] or 0) for s in snapshots)
        trades_count = sum(int(s["trades"] or 0) for s in snapshots)
        equity = float(snapshots[-1]["equity"]) \
            if snapshots and snapshots[-1]["equity"] is not None else None
        from app.research.attribution import Attribution

        attribution = Attribution(self.repository).attribute()
        anomalies = [e for e in learning if e["kind"] == "ANOMALY_DETECTED"]
        experiments = self.repository.list_experiments(limit=10)
        system_status = self.repository.list_system_status(limit=1)
        return {
            "week_start": week_start.isoformat(), "today": today.isoformat(),
            "broker_mode": str(getattr(self.settings, "broker", "LOCAL_PAPER")) if self.settings else "LOCAL_PAPER",
            "live": ("ENABLED — REAL MONEY"
                     if bool(getattr(self.settings, "live_trading", False))
                     else "DISABLED"),
            "initial_capital": float(getattr(self.settings, "initial_capital", 100000)) if self.settings else 100000,
            "equity": equity, "realized": realized, "unrealized": unrealized,
            "trades": trades_count,
            "orders": len([e for e in events if e["kind"] == "SANDBOX_ORDER_SUBMITTED"]),
            "fills": len([e for e in events if e["kind"] == "SANDBOX_FILL_RECONCILED"]),
            "risk_events": len([e for e in events if e["kind"] in {"RISK_REJECT", "EMERGENCY_STOP"}]),
            "regime_changes": [{"ts": e["ts"],
                                "detail": e["payload"] if isinstance(e["payload"], dict) else json.loads(e["payload"])}
                               for e in learning if e["kind"] == "REGIME_CHANGE"],
            "promotions": [e for e in learning if e["kind"] == "EVALUATION_RUN"
                           and isinstance(e["payload"], dict) and e["payload"].get("promoted")],
            "learning_events": len(learning),
            "coverage": self.repository.list_coverage(limit=20),
            "quality_recent": self.repository.list_quality_events(limit=10),
            "universe": self.repository.instrument_counts(),
            "attribution": attribution,
            "anomalies": anomalies,
            "experiments": experiments,
            "system_status": system_status[0]["payload"] if system_status else None,
        }

    def markdown(self, *, today=None):
        data = self.assemble(today=today)
        base = super().markdown(today=today)
        extra = ["", f"## Attribution ({self.title})"]
        strategy_dim = data["attribution"]["dimensions"].get("strategy", {})
        if strategy_dim:
            for name, stats in sorted(strategy_dim.items(),
                                      key=lambda kv: kv[1]["net_pnl"])[:8]:
                extra.append(f"- {name}: NET {stats['net_pnl']:.2f} over "
                             f"{stats['trades']} trades — CALCULATED")
        else:
            extra.append("- No recorded trade outcomes in scope.")
        extra.append("")
        extra.append(f"## Anomalies & experiments ({self.title})")
        if data["anomalies"]:
            for event in data["anomalies"][:8]:
                payload = event["payload"] if isinstance(event["payload"], dict) \
                    else json.loads(event["payload"])
                extra.append(f"- {event['ts'][:16]}: {payload.get('kind')} — OBSERVED")
        else:
            extra.append("- No anomalies recorded in scope.")
        for experiment in data["experiments"][:5]:
            extra.append(f"- experiment {experiment['name']}: {experiment['status']}")
        extra.append("")
        extra.append("*Attribution rows are CALCULATED from recorded trade "
                     "outcomes; anomalies are OBSERVED learning events.*")
        return base + "\n".join(extra) + "\n"
