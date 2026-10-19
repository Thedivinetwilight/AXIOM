"""3.1.0 — the packaged executable, verified end-to-end.

The final authority for a release is the BUILT artifact, never the source.
This module runs packaging/frozen_e2e.py against the real one-file build:

* fresh external home, launch from an unrelated CWD
* /health, /setup, /app served; NO inline JavaScript; every served asset
  parses under a real JavaScript parser
* the real wizard button flow drives the real engine over HTTP
  (start paper -> replay -> preflight -> START PAPER TRADING)
* simulated orders/fills/trades recorded
* stop -> restart -> exact state recovery
* duplicate launch handled gracefully
* database in the external home, token absent from every log

Locating the artifact:
1. $ARENA_FROZEN_ARTIFACT if set;
2. dist/Arena(.exe) if already built;
3. built on demand when $ARENA_FROZEN_E2E=build (slow; used by the
   packaging gate);
4. otherwise the module is skipped with an explicit reason — CI always
   builds first, so the gate itself never silently disappears.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))


def _artifact() -> Path | None:
    explicit = os.environ.get("ARENA_FROZEN_ARTIFACT")
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None
    for name in ("Arena.exe", "Arena"):
        candidate = ROOT / "dist" / name
        if candidate.exists():
            return candidate
    return None


def _build_artifact() -> Path:
    sys.path.insert(0, str(ROOT))
    try:
        import build_exe
    finally:
        sys.path.pop(0)
    return build_exe.build(onefile=True)


@pytest.fixture(scope="module")
def artifact():
    found = _artifact()
    if found is None and os.environ.get("ARENA_FROZEN_E2E") == "build":
        found = _build_artifact()
    if found is None:
        pytest.skip(
            "no built executable found; build it (python build_exe.py) or set "
            "ARENA_FROZEN_ARTIFACT=/path/to/Arena. The CI packaging job builds "
            "and then runs this gate — it is never skipped there.")
    return found


@pytest.fixture(scope="module")
def e2e_result(artifact, tmp_path_factory):
    from frozen_e2e import run_frozen_e2e

    workdir = tmp_path_factory.mktemp("frozen-e2e")
    return run_frozen_e2e(artifact, workdir, verbose=True)


def test_every_frozen_check_passed(e2e_result):
    failed = [r for r in e2e_result["results"] if not r["ok"]]
    node_missing = shutil.which("node") is None
    if node_missing:
        # Without a JavaScript runtime the browser-stub flow cannot run; the
        # structural fallbacks still executed. CI runners always have node, so
        # the strict gate applies there; locally we refuse to hide it.
        failed = [r for r in failed
                  if "node" not in r["detail"].lower()]
        if failed:
            pytest.fail(_failure_text(failed))
        pytest.skip("node unavailable: browser-stub wizard flow not executed "
                    "(all other frozen checks passed)")
    assert not failed, _failure_text(failed)


def _failure_text(failed):
    lines = ["the packaged executable failed verification:"]
    lines += [f"  - {r['name']}: {r['detail']}" for r in failed]
    return "\n".join(lines)


def test_core_frozen_gates_individually(e2e_result):
    """Individual hard gates, named so a failure points at the right cause."""
    by_name = {r["name"]: r for r in e2e_result["results"]}

    def gate(prefix):
        matches = [r for name, r in by_name.items() if name.startswith(prefix)]
        assert matches, f"expected gate missing: {prefix}"
        assert all(r["ok"] for r in matches), \
            "; ".join(f"{r['name']}: {r['detail']}" for r in matches if not r["ok"])

    gate("executable starts and /health answers")
    gate("existing-token setup launch opens the wizard GUI")
    gate("GET /setup returns the wizard shell")
    gate("setup page contains NO inline JavaScript")
    gate("frozen app asset serves and parses")
    gate("app shell references the bundled stylesheet")
    gate("frozen stylesheet serves")
    gate("token endpoint returns a secure token")
    gate("frozen Upstox key validation reaches upstream HTTPS")
    gate("dummy Upstox credential is absent from frozen process logs")
    gate("frozen runtime reports exact build identity")
    gate("frozen preflight blocks START after bearer rejection")
    gate("protected paper-start rejects invalid bearer")
    gate("invalid bearer is absent from rejection body")
    gate("invalid bearer is absent from frozen process logs")
    gate("paper engine processed replay bars")
    gate("paper engine recorded simulated orders/fills/trades")
    gate("STOP paper returns a clean stopped state")
    gate("restart: paper equity recovered from the database")
    gate("restart opens the main application GUI")
    gate("duplicate launch is handled gracefully")
    gate("database created in the external user-data directory")
    gate("API token never appears in process logs")
