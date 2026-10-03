"""Arena desktop application (PySide6) — FINAL.

Presentation ONLY: every number comes from the Arena backend API; the write
actions are limited to the persisted emergency STOP/RESUME flags. The app
cannot place orders, change risk limits, or alter execution modes — the hard
risk layer lives in the core. Charts are painted with QPainter (no extra
dependencies beyond PySide6).

Run:  python -m desktop   (needs: pip install -r requirements-desktop.txt,
a running Arena backend via ARENA_BACKEND_URL and ARENA_API_TOKEN).
"""

from __future__ import annotations

import os


def main() -> int:
    try:
        from PySide6.QtCore import QPoint, QRect, Qt
        from PySide6.QtGui import QPainter, QPen
        from PySide6.QtWidgets import (
            QApplication, QComboBox, QHBoxLayout, QHeaderView, QLabel,
            QLineEdit, QMainWindow, QMessageBox, QPushButton, QStackedWidget,
            QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
        )
    except ImportError as exc:
        print("PySide6 is required for the desktop app:\n"
              "  pip install -r requirements-desktop.txt\n"
              f"({exc})")
        return 2

    from desktop import viewmodels as vm
    from desktop.api_client import ArenaApiClient
    from desktop.forecast_controller import ForecastUiState

    base_url = os.getenv("ARENA_BACKEND_URL", "http://127.0.0.1:8000")
    token = os.getenv("ARENA_API_TOKEN", "")

    class ChartWidget(QWidget):
        """Minimal dependency-free line/bar chart (QPainter).

        ``forecast_points`` is drawn as a clearly distinct dashed magenta line:
        it is MODEL output and must never look like the observed series.
        """

        def __init__(self, title: str, *, bars: bool = False):
            super().__init__()
            self.title = title
            self.points: list[tuple[float, float]] = []
            self.forecast_points: list[tuple[float, float]] = []
            self.legend = ""
            self.bars = bars
            self.setMinimumHeight(170)

        def set_points(self, points):
            self.points = points or []
            self.update()

        def set_forecast(self, points, legend: str = ""):
            self.forecast_points = points or []
            self.legend = legend
            self.update()

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.fillRect(self.rect(), Qt.black)
            painter.setPen(QPen(Qt.gray))
            painter.drawText(10, 16, self.title)
            if not self.points:
                painter.drawText(10, 40, "no recorded data")
                return
            margins = QRect(34, 26, self.width() - 44, self.height() - 46)
            values = [value for _, value in self.points]
            values += [value for _, value in self.forecast_points]
            low, high = min(values), max(values)
            if high == low:
                high = low + 1.0
            painter.setPen(QPen(Qt.darkGray))
            painter.drawLine(margins.left(), margins.bottom(),
                             margins.right(), margins.bottom())
            step = max((margins.width()) / max(len(self.points) - 1, 1), 1)
            if self.bars:
                width = max(4, margins.width() // max(len(self.points), 1) - 4)
                for index, (_, value) in enumerate(self.points):
                    height = abs(value) / (abs(high) + 1e-9) * (margins.height() - 10)
                    x = margins.left() + int(index * (width + 4))
                    bar = QRect(x, margins.bottom() - int(height), width, int(height))
                    painter.setPen(QPen(Qt.green if value >= 0 else Qt.red))
                    painter.fillRect(bar, Qt.green if value >= 0 else Qt.red)
                    painter.drawRect(bar)
            else:
                def plot(series, pen, offset=0):
                    painter.setPen(pen)
                    previous = None
                    total = max(len(self.points) + len(self.forecast_points) - 1, 1)
                    span = margins.width() / total if self.forecast_points else step
                    for index, (_, value) in enumerate(series):
                        y = margins.bottom() - int((value - low) / (high - low)
                                                   * (margins.height() - 10))
                        x = margins.left() + int((offset + index) * span)
                        point = QPoint(x, y)
                        if previous is not None:
                            painter.drawLine(previous, point)
                        previous = point
                    return previous

                plot(self.points, QPen(Qt.cyan, 2))
                if self.forecast_points:
                    # Distinct colour + dashes + a label: this is MODEL output.
                    pen = QPen(Qt.magenta, 2)
                    pen.setStyle(Qt.DashLine)
                    plot(self.forecast_points, pen, offset=max(len(self.points) - 1, 0))
                    painter.setPen(QPen(Qt.magenta))
                    painter.drawText(
                        margins.left() + 4, margins.top() + 8,
                        "dashed = MODEL FORECAST (not actual)")
            painter.setPen(QPen(Qt.gray))
            painter.drawText(2, margins.top() + 8, f"{high:.2f}")
            painter.drawText(2, margins.bottom(), f"{low:.2f}")

    class ArenaWindow(QMainWindow):
        PAGES = ["Command Center", "Live Market", "Forecast", "Portfolio", "Trades",
                 "Performance", "Strategies", "Research Lab", "Learning",
                 "Market Intelligence", "Risk", "Logs", "Reports", "Settings"]

        def __init__(self):
            super().__init__()
            self.setWindowTitle("Arena 3.0.5 — Quantitative Research & Trading Platform")
            self.resize(1240, 800)
            self.client = ArenaApiClient(base_url, token)
            self.forecast = ForecastUiState()
            self.mode_badge = QLabel("connecting…")
            self.mode_badge.setStyleSheet("font-weight:bold; padding:4px 10px;")
            self.stop_button = QPushButton("EMERGENCY STOP")
            self.stop_button.setStyleSheet(
                "background:#7a2430; color:#ffd9df; font-weight:bold; padding:6px 14px;")
            self.stop_button.clicked.connect(self._on_emergency_stop)
            self.resume_button = QPushButton("Resume")
            self.resume_button.clicked.connect(self._on_resume)
            self.page_switcher = QComboBox()
            self.page_switcher.addItems(self.PAGES)
            self.page_switcher.currentTextChanged.connect(self._show_page)

            header = QHBoxLayout()
            header.addWidget(self.mode_badge)
            header.addWidget(self.stop_button)
            header.addWidget(self.resume_button)
            header.addStretch(2)
            header.addWidget(QLabel("Page:"))
            header.addWidget(self.page_switcher)

            self.stack = QStackedWidget()
            self.tables: dict[str, QTableWidget] = {}
            self.texts: dict[str, QTextEdit] = {}
            self.charts: dict[str, ChartWidget] = {}
            self.query_box: QLineEdit | None = None

            # Command Center: NL query + answer + status table
            command_page = QWidget()
            command_layout = QVBoxLayout(command_page)
            query_row = QHBoxLayout()
            self.query_box = QLineEdit()
            self.query_box.setPlaceholderText(
                "Ask: Why did Arena lose money today? · What changed? · "
                "Which strategies are degrading?")
            ask_button = QPushButton("Ask")
            ask_button.clicked.connect(self._on_ask)
            query_row.addWidget(self.query_box)
            query_row.addWidget(ask_button)
            command_layout.addLayout(query_row)
            self.texts["Command Center"] = QTextEdit()
            self.texts["Command Center"].setReadOnly(True)
            command_layout.addWidget(self.texts["Command Center"])
            command_table = QTableWidget(0, 2)
            self.tables["Command Center"] = command_table
            command_layout.addWidget(command_table)
            self.stack.addWidget(command_page)

            for name in self.PAGES[1:]:
                if name in {"Risk", "Portfolio", "Market Intelligence",
                            "Live Market"}:
                    page = QWidget()
                    layout = QVBoxLayout(page)
                    table = QTableWidget(0, 2)
                    table.horizontalHeader().setSectionResizeMode(
                        0, QHeaderView.ResizeToContents)
                    layout.addWidget(table)
                    if name == "Portfolio":
                        self.charts["attribution"] = ChartWidget(
                            "NET P&L by strategy (CALCULATED)", bars=True)
                        layout.addWidget(self.charts["attribution"])
                    if name == "Live Market":
                        # MARKETS overlay: observed closes plus a clearly
                        # distinguished Kronos forecast, never merged.
                        layout.addWidget(QLabel(
                            "Overlay: solid = ACTUAL market data · dashed = "
                            "MODEL FORECAST (Kronos) — not observations"))
                        self.charts["markets_forecast"] = ChartWidget(
                            "MARKETS · ACTUAL close + MODEL FORECAST close")
                        layout.addWidget(self.charts["markets_forecast"])
                    self.tables[name] = table
                    self.stack.addWidget(page)
                elif name == "Forecast":
                    page = QWidget()
                    layout = QVBoxLayout(page)
                    banner = QLabel(self.forecast.banner())
                    banner.setStyleSheet(
                        "background:#3a2a4d; color:#e9d5ff; font-weight:bold; "
                        "padding:6px 10px;")
                    layout.addWidget(banner)
                    selector_row = QHBoxLayout()
                    self.forecast_selector = QComboBox()
                    self.forecast_selector.addItems(self.forecast.series_labels())
                    self.forecast_selector.currentTextChanged.connect(
                        self.forecast.select)
                    refresh_button = QPushButton("Force refresh")
                    refresh_button.clicked.connect(self._on_force_forecast)
                    selector_row.addWidget(QLabel("Series:"))
                    selector_row.addWidget(self.forecast_selector)
                    selector_row.addWidget(refresh_button)
                    selector_row.addStretch(1)
                    layout.addLayout(selector_row)
                    self.charts["forecast_overlay"] = ChartWidget(
                        "FORECAST · ACTUAL close (solid) vs MODEL FORECAST (dashed)")
                    layout.addWidget(self.charts["forecast_overlay"])
                    panel = QTableWidget(0, 2)
                    panel.horizontalHeader().setSectionResizeMode(
                        0, QHeaderView.ResizeToContents)
                    self.tables["Forecast"] = panel
                    layout.addWidget(panel, 3)
                    candles = QTableWidget(0, 6)
                    self.tables["Forecast candles"] = candles
                    layout.addWidget(candles, 2)
                    self.stack.addWidget(page)
                elif name == "Performance":
                    page = QWidget()
                    layout = QVBoxLayout(page)
                    self.charts["equity"] = ChartWidget("Equity")
                    self.charts["drawdown"] = ChartWidget("Drawdown")
                    layout.addWidget(self.charts["equity"])
                    layout.addWidget(self.charts["drawdown"])
                    table = QTableWidget(0, 2)
                    self.tables[name] = table
                    layout.addWidget(table)
                    self.stack.addWidget(page)
                elif name == "Reports":
                    page = QWidget()
                    layout = QVBoxLayout(page)
                    period_row = QHBoxLayout()
                    self.report_period = QComboBox()
                    self.report_period.addItems(["daily", "weekly", "monthly"])
                    load_button = QPushButton("Load report")
                    load_button.clicked.connect(self._on_load_report)
                    period_row.addWidget(QLabel("Period:"))
                    period_row.addWidget(self.report_period)
                    period_row.addWidget(load_button)
                    period_row.addStretch(1)
                    layout.addLayout(period_row)
                    self.texts["Reports"] = QTextEdit()
                    self.texts["Reports"].setReadOnly(True)
                    layout.addWidget(self.texts["Reports"])
                    self.stack.addWidget(page)
                elif name == "Settings":
                    page = QWidget()
                    layout = QVBoxLayout(page)
                    self.texts["Settings"] = QTextEdit()
                    self.texts["Settings"].setReadOnly(True)
                    layout.addWidget(QLabel(
                        "Backend/token are configured via environment:\n"
                        "ARENA_BACKEND_URL, ARENA_API_TOKEN.\n"
                        "The desktop app is a viewer + emergency stop only; risk "
                        "limits and execution modes live in the Arena core (.env).\n"
                        "Real-money routing is OFF by default and requires the "
                        "four-switch opt-in (MODE=LIVE, LIVE_TRADING=true, "
                        "BROKER=UPSTOX_LIVE, LIVE_CONFIRM) plus non-zero risk "
                        "limits. Check /health for the ACTUAL mode."))
                    layout.addWidget(self.texts["Settings"])
                    self.stack.addWidget(page)
                else:
                    table = QTableWidget(0, 3)
                    self.tables[name] = table
                    self.stack.addWidget(table)

            central = QWidget()
            layout = QVBoxLayout(central)
            layout.addLayout(header)
            layout.addWidget(self.stack)
            self.setCentralWidget(central)
            self._show_page("Command Center")
            # Kronos stack: built from env, off by default, never fatal.
            self.forecast.bootstrap()
            if self.forecast.series_labels():
                self.forecast_selector.addItems([])
                self.forecast_selector.clear()
                self.forecast_selector.addItems(self.forecast.series_labels())
            self._refresh()
            from PySide6.QtCore import QTimer

            self.timer = QTimer(self)
            self.timer.timeout.connect(self._refresh)
            self.timer.start(5000)

        def closeEvent(self, event):
            # Clean Kronos worker shutdown; the Arena client needs none.
            self.forecast.shutdown()
            super().closeEvent(event)

        def _on_force_forecast(self):
            selected = self.forecast.selected()
            if selected is not None and self.forecast.service is not None:
                self.forecast.service.request_refresh(*selected, force=True)
            self._refresh_forecast()

        def _show_page(self, name: str):
            index = self.PAGES.index(name) if name in self.PAGES else 0
            self.stack.setCurrentIndex(index)

        def _on_ask(self):
            if self.query_box is None:
                return
            question = self.query_box.text().strip()
            if not question:
                return
            answer = self._safe(lambda: self.client.command_center(question), {})
            self.texts["Command Center"].setPlainText(
                f"Q: {question}\n\n{answer.get('answer', 'backend unreachable')}\n\n"
                f"Labels: {', '.join(answer.get('labels', []))}")

        def _on_load_report(self):
            period = self.report_period.currentText()
            text = self._safe(lambda: self.client.report(period), "")
            self.texts["Reports"].setPlainText(text or "backend unreachable")

        def _fill_table(self, name: str, rows, headers=None):
            table = self.tables.get(name)
            if table is None or not rows:
                return
            width = max(len(row) for row in rows)
            table.setColumnCount(width)
            table.setRowCount(len(rows))
            if headers:
                table.setHorizontalHeaderLabels(headers)
            for r, row in enumerate(rows):
                for c, value in enumerate(row):
                    table.setItem(r, c, QTableWidgetItem(str(value)))

        def _safe(self, call, default):
            try:
                return call()
            except Exception as exc:
                self.mode_badge.setText(f"backend unreachable ({type(exc).__name__})")
                return default

        def _refresh(self):
            overview = self._safe(self.client.overview, {})
            if overview:
                self.mode_badge.setText(str(overview.get("mode_display", "")))
                self.mode_badge.setStyleSheet(
                    "font-weight:bold; padding:4px 10px; color:#ff9aa5;"
                    if overview.get("emergency_stop") else
                    "font-weight:bold; padding:4px 10px; color:#75e7b2;")
            self._fill_table("Command Center", [
                ("Mode", str(overview.get("mode_display", "—"))),
                ("Emergency stop", "ACTIVE" if overview.get("emergency_stop") else "clear"),
                ("Equity", vm.money(overview.get("equity"))),
                ("Activity events", str(len(overview.get("activity") or [])))])

            trades = self._safe(self.client.trades, {})
            self._fill_table("Trades", vm.trade_rows(trades))

            performance = self._safe(self.client.performance, {})
            curve = [(point["date"], float(point["equity"]))
                     for point in performance.get("equity_curve", [])]
            if curve:
                peak = None
                dd = []
                for date, equity in curve:
                    peak = equity if peak is None else max(peak, equity)
                    dd.append((date, 1 - equity / peak if peak else 0.0))
                self.charts["equity"].set_points(curve)
                self.charts["drawdown"].set_points(dd)
            perf_rows = [("Realized P&L", vm.money(performance.get("realized"))),
                         ("Unrealized P&L", vm.money(performance.get("unrealized"))),
                         ("Total return",
                          vm.pct(None if performance.get("total_return_pct") is None
                                 else performance["total_return_pct"] / 100)),
                         ("Drawdown", vm.pct(performance.get("drawdown")))]
            self._fill_table("Performance", perf_rows)

            strategies = self._safe(self.client.strategies, {})
            strat_rows = [(v.get("version", ""), "PROMOTED",
                           str(v.get("parameters", "")))
                          for v in strategies.get("promoted_versions", [])]
            strat_rows += [(c.get("strategy", ""), "candidate",
                            str(c.get("parameters", "")))
                           for c in strategies.get("candidates", [])[:10]]
            self._fill_table("Strategies", strat_rows,
                             headers=["version/strategy", "state", "parameters"])

            learning = self._safe(self.client.learning_detail, {})
            self._fill_table("Learning", vm.learning_detail_rows(learning))

            lab = self._safe(self.client.research_lab, {})
            self._fill_table("Research Lab", vm.research_lab_rows(lab))

            market = self._safe(self.client.market_intelligence, {})
            self._fill_table("Market Intelligence", vm.market_intelligence_rows(market))

            portfolio = self._safe(self.client.portfolio, {})
            self._fill_table("Portfolio", vm.portfolio_rows(portfolio))
            attribution = self._safe(self.client.attribution, {})
            self.charts["attribution"].set_points(vm.attribution_values(attribution))

            risk = self._safe(self.client.risk, {})
            self._fill_table("Risk", vm.risk_rows(risk))
            self.stop_button.setVisible(not risk.get("emergency_stop"))
            self.resume_button.setVisible(bool(risk.get("emergency_stop")))

            logs = self._safe(self.client.logs, {})
            self.texts["Logs"].setPlainText(
                "\n".join(f"{e['ts']} {e['kind']} {e.get('payload', '')}"
                          for e in logs.get("raw_events", [])[:60]) or "no raw events")
            self.texts["Activity"].setPlainText("\n".join(
                f"{e['ts']} — {e['headline']} ({e.get('why', '')})"
                for e in logs.get("activity", [])) or "no events recorded")

            live = self._safe(self.client.live_market, {})
            if live:
                self._fill_table("Live Market", [
                    ("Pipeline", live.get("pipeline")),
                    ("Connection", str(live.get("connection", "—"))),
                    ("Market status", str(live.get("market_status", "—"))),
                    ("Strategy", str(live.get("strategy", "—"))),
                    ("Regime", str(live.get("regime", "—")))])
            self.texts["Settings"].setPlainText(
                f"backend: {base_url}\nmode: {overview.get('mode_display', '—')}\n"
                "reports: Reports page; CLI: python -m app report --format pdf")
            # Kronos polling is independent of the Arena polling above: it only
            # queues work and drains results, so it cannot stall this timer.
            self._refresh_forecast()

        def _refresh_forecast(self):
            """Non-blocking Kronos refresh of the FORECAST panel + overlay."""
            self.forecast.refresh()
            rows = self.forecast.panel_rows()
            table = self.tables.get("Forecast")
            if table is not None:
                table.setColumnCount(2)
                table.setRowCount(len(rows))
                table.setHorizontalHeaderLabels(["field", "value"])
                for r, (label, value) in enumerate(rows):
                    table.setItem(r, 0, QTableWidgetItem(str(label)))
                    table.setItem(r, 1, QTableWidgetItem(str(value)))
            candles = self.tables.get("Forecast candles")
            if candles is not None:
                candle_rows = self.forecast.forecast_candles()
                width = max((len(row) for row in candle_rows), default=6)
                candles.setColumnCount(width)
                candles.setRowCount(len(candle_rows))
                for r, row in enumerate(candle_rows):
                    for c, value in enumerate(row):
                        candles.setItem(r, c, QTableWidgetItem(str(value)))
            actual, predicted = self.forecast.chart_series()
            legend = self.forecast.legend()
            for key in ("forecast_overlay", "markets_forecast"):
                chart = self.charts.get(key)
                if chart is not None:
                    chart.set_points(actual)
                    chart.set_forecast(predicted, legend)

        def _on_emergency_stop(self):
            answer = QMessageBox.question(
                self, "EMERGENCY STOP",
                "Halt trading and cancel open sandbox orders?\n"
                "(Persisted; survives restarts until explicitly resumed.)")
            if answer == QMessageBox.Yes:
                self._safe(self.client.emergency_stop, {})
                self._refresh()

        def _on_resume(self):
            self._safe(self.client.resume, {})
            self._refresh()

    app = QApplication([])
    window = ArenaWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
