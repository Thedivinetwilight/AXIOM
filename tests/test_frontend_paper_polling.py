"""Behavioral coverage for the browser's bounded paper-status polling."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE_TEST = ROOT / "tests" / "frontend" / "test_paper_status_polling.test.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for frontend behavior tests")
def test_paper_status_polling_behavior():
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"Node behavior tests failed:\n{result.stdout}\n{result.stderr}"
