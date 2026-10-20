"""Cleanup log: what happened, with secrets kept out of it.

Every line goes through ``redact`` before it reaches disk. The log records
paths, categories, reasons, sizes and outcomes — never file contents — so a
credential cannot leak even if a path or an OS error message happens to
contain one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from uninstaller.model import ActionReport, Inventory
from uninstaller.redact import redact


class Logbook:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lines: list[str] = []

    def write(self, text: str = "") -> None:
        self._lines.append(redact(text))

    def section(self, title: str) -> None:
        self._lines.append("")
        self._lines.append(f"=== {redact(title)} ===")

    def timestamped(self, text: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.write(f"[{stamp}] {text}")

    def log_inventory(self, inventory: Inventory) -> None:
        self.section("SCAN RESULTS")
        self.write(f"mode: {inventory.mode}")
        self.write(f"scanned roots: {len(inventory.scanned_roots)}")
        self.write(f"matches: {len(inventory.matches)}")
        for match in inventory.matches:
            flag = "selected" if match.selected else "preserved/unselected"
            self.write(f"  [{match.category}] {flag}  {match.path}")
            for reason in match.reasons:
                self.write(f"      reason: {reason}")
        if inventory.issues:
            self.section("SCAN ISSUES")
            for issue in inventory.issues:
                self.write(f"  [{issue.kind}] {issue.path}: {issue.detail}")

    def log_report(self, report: ActionReport) -> None:
        self.section("ACTION RESULTS")
        for result in report.results:
            self.write(f"  [{result.action}] [{result.category}] {result.path}")
            if result.detail:
                self.write(f"      {result.detail}")
            if result.quarantine_path:
                self.write(f"      quarantined at: {result.quarantine_path}")
        permanent = report.permanent_deletions
        if permanent:
            self.section("PERMANENT DELETIONS")
            for result in permanent:
                self.write(f"  {result.path} — {result.detail}")
        failures = report.failures
        if failures:
            self.section("FAILURES")
            for result in failures:
                self.write(f"  {result.path} — {result.detail}")
        for issue in report.issues:
            if issue.kind != "info":
                self.write(f"  [{issue.kind}] {issue.path}: {issue.detail}")

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(redact(line) for line in self._lines) + "\n"
        self.path.write_text(text, encoding="utf-8")
        return self.path
