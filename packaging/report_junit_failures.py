#!/usr/bin/env python3
"""Print the failures of a JUnit XML report as GitHub Actions annotations.

The Windows workflow runs the full source suite on Windows. When it fails,
the reason must be readable without downloading the whole run log, so every
failing test case is turned into an ``::error::`` annotation.

    python packaging/report_junit_failures.py pytest-results.xml

Only the test NAME and the failure MESSAGE are printed: never a value from
the environment, never a secret.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _escape(message: str) -> str:
    return (message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"))


def report(path: Path, *, limit: int = 40) -> int:
    """Annotate every failing test case. Returns the number of failures."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        print(f"::error title=Pytest report::the report could not be read: {exc}")
        return 0

    failures = 0
    for case in root.iter("testcase"):
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
        if failure is None:
            continue
        failures += 1
        name = (case.get("classname", "") + "::" + case.get("name", "unknown"))
        message = " ".join((failure.get("message") or failure.text or "").split())
        print(f"::error title=Pytest failure::{_escape(name[:200])} - "
              f"{_escape(message[:350])}")
    if failures == 0:
        print("::notice title=Pytest report::the JUnit report lists no failing case")
    elif failures > limit:
        print(f"::error title=Pytest report::{failures} failing cases, "
              f"the first {limit} are shown")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="path to the JUnit XML report")
    parser.add_argument("--limit", type=int, default=40,
                        help="how many failing cases to annotate")
    args = parser.parse_args(argv)
    path = Path(args.report)
    if not path.is_file():
        print(f"::error title=Pytest report::the report is missing: {path}")
        return 1
    report(path, limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
