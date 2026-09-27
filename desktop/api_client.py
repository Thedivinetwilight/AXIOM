"""HTTP client for the Arena backend (stdlib only; fully unit-testable).

Every read maps to an existing authenticated backend route. The two write
actions are the persisted emergency STOP/RESUME flags — the desktop app can
halt the system but can never bypass or weaken the hard risk layer.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class ArenaApiClient:
    def __init__(self, base_url: str, token: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, path: str, method: str = "GET") -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}", method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode())

    # reads ------------------------------------------------------------------
    def overview(self) -> dict:
        return self._request("/api/v2/overview")

    def trades(self) -> dict:
        return self._request("/api/v2/trades")

    def performance(self) -> dict:
        return self._request("/api/v2/performance")

    def strategies(self) -> dict:
        return self._request("/api/v2/strategies")

    def learning(self) -> dict:
        return self._request("/api/v2/learning")

    def experiments(self) -> dict:
        return self._request("/api/v2/experiments")

    def market_data(self) -> dict:
        return self._request("/api/v2/market_data")

    def risk(self) -> dict:
        return self._request("/api/v2/risk")

    def logs(self) -> dict:
        return self._request("/api/v2/logs")

    def live_market(self) -> dict:
        return self._request("/dashboard/data")

    def report_markdown(self) -> str:
        request = urllib.request.Request(
            f"{self.base_url}/api/v2/report/weekly",
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "text/markdown"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read().decode()

    # FINAL reads -------------------------------------------------------------
    def command_center(self, question: str) -> dict:
        from urllib.parse import quote

        return self._request(f"/api/v2/command_center?q={quote(question)}")

    def portfolio(self) -> dict:
        return self._request("/api/v2/portfolio")

    def attribution(self) -> dict:
        return self._request("/api/v2/attribution")

    def research_lab(self) -> dict:
        return self._request("/api/v2/research_lab")

    def market_intelligence(self) -> dict:
        return self._request("/api/v2/market_intelligence")

    def learning_detail(self) -> dict:
        return self._request("/api/v2/learning_detail")

    def report(self, period: str = "weekly") -> str:
        request = urllib.request.Request(
            f"{self.base_url}/api/v2/report?period={period}",
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "text/markdown"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read().decode()

    # actions (persisted safety flags only) ------------------------------------
    def emergency_stop(self) -> dict:
        return self._request("/engine/stop", method="POST")

    def resume(self) -> dict:
        return self._request("/engine/resume", method="POST")
