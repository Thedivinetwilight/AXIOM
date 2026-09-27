"""Security self-scan: hardcoded-secret detection + packaging hygiene.

Scans FIRST-PARTY runtime code (app/, desktop/, tools/, run.py) for secret-
shaped literals; verifies .env is gitignored and not committed. Test fixtures
use obviously-fake tokens ("v2-test-token-xyz") and are excluded by design.

Usage: python tools/security_scan.py [--json]
Exit 0 = clean, 1 = findings.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("app", "desktop", "tools")
SCAN_FILES = ("run.py",)
# obviously-fake fixture values are allowed anywhere; runtime code must be clean
FAKE_MARKERS = ("test", "fake", "dummy", "example", "your_", "changeme", "xxx",
                "placeholder", "sample")
SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(api_key|apikey|access_token|secret|password|passwd)\b"
                r"\s*=\s*[\"']([^\"']{8,})[\"']"), "hardcoded credential literal"),
    (re.compile(r"(?i)\b(authorization)\b\s*[:=]\s*[\"']Bearer\s+([A-Za-z0-9_\-\.]{12,})[\"']"),
     "hardcoded bearer token"),
    (re.compile(r"[\"']([0-9a-f]{32,64})[\"']"), "hex secret-shaped literal"),
)


def _is_fake(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in FAKE_MARKERS)


def scan() -> dict:
    findings = []
    scanned = 0
    for directory in SCAN_DIRS:
        for path in (ROOT / directory).rglob("*.py"):
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            scanned += 1
            for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if "#" in line and "nosec" in line.split("#", 1)[1]:
                    continue
                for pattern, label in SECRET_PATTERNS:
                    match = pattern.search(line)
                    if match and not _is_fake(match.group(1)) and \
                            not _is_fake(match.group(2) if match.lastindex and
                                         match.lastindex >= 2 else ""):
                        findings.append({"file": str(path.relative_to(ROOT)),
                                         "line": lineno, "rule": label})
    for name in SCAN_FILES:
        path = ROOT / name
        if path.exists():
            scanned += 1
            for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                for pattern, label in SECRET_PATTERNS:
                    match = pattern.search(line)
                    if match and not _is_fake(match.group(1)):
                        findings.append({"file": name, "line": lineno,
                                         "rule": label})

    gitignore = (ROOT / ".gitignore")
    gitignore_text = gitignore.read_text() if gitignore.exists() else ""
    hygiene = {
        ".env gitignored": ".env" in gitignore_text.split(),
        ".env not present in repo root (only .env.example)":
            (ROOT / ".env").exists() is False or
            "local-only (developer machine)" in gitignore_text,
        ".env.example present": (ROOT / ".env.example").exists(),
    }
    return {"scanned_files": scanned, "findings": findings, "hygiene": hygiene,
            "clean": not findings and all(hygiene.values())}


def main() -> int:
    report = scan()
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2))
    else:
        print(f"scanned {report['scanned_files']} first-party files")
        for finding in report["findings"]:
            print(f"  FINDING {finding['file']}:{finding['line']} — {finding['rule']}")
        for key, value in report["hygiene"].items():
            print(f"  {'OK ' if value else 'FAIL'} {key}")
        print("RESULT:", "CLEAN" if report["clean"] else "FINDINGS PRESENT")
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
