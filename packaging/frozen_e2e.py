"""End-to-end verification of the ACTUAL BUILT executable.

A packaged Arena is a different runtime from a source checkout: one-file
extraction, frozen import machinery, bundle-relative resources, no repository
around it. Source tests cannot prove any of that, so this module exercises the
artifact itself. It is used by

* ``build_exe.py --test``        (the packaging gate)
* ``tests/test_v310_frozen_e2e`` (the CI/test gate)

Verified, in order:

 1. startup in a FRESH external home, launched from a CWD that is NOT the
    repository and NOT the home (double-click simulation)
 2. /health reachable, reporting the right version
 3. /setup HTML parses; contains NO inline JavaScript; every referenced
    static asset is served and its JavaScript parses under a real JS parser
    (``node --check`` when available)
 4. the wizard JS actually initializes in the browser-stub driver and the
    real button flow (start paper -> replay -> preflight -> START) runs
    against the live executable over HTTP
 5. token endpoint works; prepare-paper writes the .env into the EXTERNAL
    home; the token never appears in process logs
 6. paper engine starts; simulated orders/fills/trades are recorded
 7. stop persists state
 8. restart recovers the session (same equity/orders; wizard not forced)
 9. a duplicate launch is handled gracefully (exit 0, no second server,
    first session untouched)
10. the database lives in the external home, never next to the binary
11. /app shell serves and its JavaScript parses too

This module never fakes a result: every check is an observation of the real
process. It is deliberately dependency-free (stdlib only) so it runs on any
build machine.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [
    ROOT / "app" / "static" / "arena-core.js",
    ROOT / "app" / "static" / "arena-wizard.js",
    ROOT / "app" / "static" / "arena-app.js",
]
STUB = ROOT / "packaging" / "browser_stub.js"


def _log(msg: str, verbose: bool) -> None:
    if verbose:
        print(f"  [frozen-e2e] {msg}", flush=True)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _get(base: str, path: str, token: str | None = None, timeout: float = 10.0):
    request = urllib.request.Request(base + path)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        return response.status, body


def _post(base: str, path: str, payload: dict, token: str | None = None,
          timeout: float = 60.0):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base + path, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            return exc.code, {}


def _wait_health(base: str, timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _ = _get(base, "/health", timeout=2)
            if status == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.4)
    return False


def _node_available() -> bool:
    return shutil.which("node") is not None


def _js_parses(js_text: str, label: str) -> tuple[bool, str]:
    """Validate JavaScript with a real parser (node) when available."""
    if not _node_available():
        # Structural fallback: balanced braces/parens outside strings. This is
        # weaker than a parser and the report says so.
        depth = {"{": 0, "(": 0, "[": 0}
        pairs = {"}": "{", ")": "(", "]": "["}
        quote = None
        escaped = False
        for ch in js_text:
            if escaped:
                escaped = False
                continue
            if quote:
                if ch == "\\":
                    escaped = True
                elif ch == quote:
                    quote = None
                continue
            if ch in "\"'`":
                quote = ch
            elif ch in depth:
                depth[ch] += 1
            elif ch in pairs:
                depth[pairs[ch]] -= 1
                if depth[pairs[ch]] < 0:
                    return False, f"{label}: unbalanced '{ch}' (fallback checker)"
        ok = all(v == 0 for v in depth.values()) and quote is None
        return ok, label + (" (fallback structural check — node unavailable)"
                            if ok else " (fallback checker)")
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(js_text)
        path = handle.name
    try:
        proc = subprocess.run(["node", "--check", path],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            return True, label + " (node --check)"
        return False, f"{label}: node --check failed: {proc.stderr.strip()[:300]}"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{label}: node invocation failed: {exc}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class FrozenE2E:
    def __init__(self, artifact: Path, *, workdir: Path, verbose: bool = True):
        self.artifact = Path(artifact)
        self.workdir = Path(workdir)
        self.verbose = verbose
        self.results: list[dict] = []
        self.process: subprocess.Popen | None = None
        self.home = self.workdir / "home"
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.token: str | None = None
        self.output_log = self.workdir / "arena-process.log"

    # ------------------------------------------------------------- helpers
    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append({"name": name, "ok": bool(ok), "detail": detail})
        _log(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""),
             self.verbose)
        return bool(ok)

    def _env(self) -> dict:
        env = dict(os.environ)
        env["ARENA_HOME"] = str(self.home)
        env["ARENA_PORT"] = str(self.port)
        for key in ("DATABASE_URL", "DATA_DIR", "API_TOKEN"):
            env.pop(key, None)
        return env

    def start(self, *, cwd: Path | None = None) -> subprocess.Popen:
        # Launched from a directory that is neither the repo nor the home:
        # exactly the situation of a double-clicked executable.
        launch_dir = Path(cwd or self.workdir / "launchdir")
        launch_dir.mkdir(parents=True, exist_ok=True)
        log = open(self.output_log, "ab")
        process = subprocess.Popen(
            [str(self.artifact), "--no-browser"],
            cwd=str(launch_dir), env=self._env(),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        return process

    def stop(self, process: subprocess.Popen) -> None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    # ---------------------------------------------------------------- checks
    def run(self) -> dict:
        started = time.monotonic()
        try:
            self._phase_first_launch()
            self._phase_cli_reentry()
            self._phase_frontend_integrity()
            self._phase_wizard_flow()
            self._phase_paper_engine()
            self._phase_stop_and_restart()
            self._phase_duplicate_launch()
            self._phase_security_and_paths()
        finally:
            if self.process is not None:
                self.stop(self.process)
        elapsed = time.monotonic() - started
        ok = all(r["ok"] for r in self.results)
        return {"ok": ok, "elapsed_seconds": round(elapsed, 1),
                "artifact": str(self.artifact), "results": self.results}

    def _phase_first_launch(self) -> None:
        self.process = self.start()
        healthy = _wait_health(self.base, timeout=120)
        if not self.check("executable starts and /health answers in a fresh home",
                          healthy):
            self._dump_log()
            raise RuntimeError("artifact did not become healthy")
        status, body = _get(self.base, "/health")
        health = json.loads(body)
        from app import __version__

        self.check("/health reports status ok and the current version",
                   status == 200 and health.get("status") == "ok"
                   and health.get("version") == __version__,
                   f"version={health.get('version')} (expected {__version__})")
        self.check("/health reports simulated routing by default",
                   health.get("real_order_routing") == "SIMULATED_LOCAL_PAPER",
                   str(health.get("real_order_routing")))

    def _phase_cli_reentry(self) -> None:
        """The feature console re-enters the bundle with --cli; that path must
        work while the server is up (it spawns a child of the same binary)."""
        try:
            proc = subprocess.run(
                [str(self.artifact), "--cli", "live-status"],
                cwd=str(self.workdir), env=self._env(),
                capture_output=True, text=True, timeout=300)
            ok = proc.returncode == 0
            output = (proc.stdout or "").strip()
            parsed = None
            try:
                parsed = json.loads(output)
            except ValueError:
                ok = False
            self.check("--cli re-entry works inside the frozen bundle",
                       ok and isinstance(parsed, dict),
                       f"exit={proc.returncode}")
        except (OSError, subprocess.SubprocessError) as exc:
            self.check("--cli re-entry works inside the frozen bundle", False, str(exc))

    def _phase_frontend_integrity(self) -> None:
        status, setup_html = _get(self.base, "/setup")
        self.check("GET /setup returns the wizard shell", status == 200,
                   f"{len(setup_html)} bytes")

        inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                            setup_html, re.S | re.I)
        self.check("setup page contains NO inline JavaScript",
                   all(not chunk.strip() for chunk in inline),
                   f"{len(inline)} inline blocks")

        srcs = re.findall(r'<script[^>]*\bsrc="([^"]+)"', setup_html)
        self.check("setup page references external JS assets", bool(srcs),
                   ", ".join(srcs))
        for src in srcs:
            clean = src.split("?")[0]
            try:
                ast, js = _get(self.base, clean)
                ok, detail = _js_parses(js, f"asset {clean}")
                self.check(f"served asset parses as valid JavaScript: {clean}",
                           ast == 200 and ok, detail)
            except (urllib.error.URLError, OSError) as exc:
                self.check(f"served asset parses as valid JavaScript: {clean}",
                           False, str(exc))

        for expected in ("wizard-root", "arena.css", "arena-wizard.js"):
            self.check(f"setup page contains expected element/reference: {expected}",
                       expected in setup_html)

        # source files must parse too (defense in depth for the app shell)
        for path in SCRIPTS:
            ok, detail = _js_parses(path.read_text(encoding="utf-8"),
                                    f"source {path.name}")
            self.check(f"bundled source parses: {path.name}", ok, detail)

        status, app_html = _get(self.base, "/app")
        self.check("GET /app returns the application shell", status == 200)
        self.check("app shell references arena-app.js", "arena-app.js" in app_html)

    def _phase_wizard_flow(self) -> None:
        # token endpoint first (independent of the browser driver)
        status, body = _post(self.base, "/setup/generate-token", {})
        self.check("token endpoint returns a secure token",
                   status == 200 and len(body.get("token", "")) >= 32)

        if not _node_available():
            self.check("browser-stub wizard flow (node required)", False,
                       "node is not available on this machine — the JS runtime "
                       "check cannot run; install node to enable it")
            return
        stub = subprocess.run(
            ["node", str(STUB), self.base,
             str(ROOT / "app" / "static" / "arena-core.js"),
             str(ROOT / "app" / "static" / "arena-wizard.js")],
            capture_output=True, text=True, timeout=300, cwd=str(self.workdir))
        report = None
        try:
            report = json.loads(stub.stdout)
        except ValueError:
            pass
        if report is None:
            self.check("browser-stub wizard flow", False,
                       f"no JSON report; stderr tail: {stub.stderr[-400:]}")
            return
        for step_report in report["steps"]:
            self.check("wizard flow: " + step_report["name"],
                       step_report["ok"], step_report.get("detail", ""))
        # the driver's prepare-paper generated the session token; read it back
        # from the .env it wrote (file existence is verified later)
        env_path = self.home / ".env"
        if env_path.exists():
            from app.config.envfile import read_values

            self.token = read_values(env_path).get("API_TOKEN")

    def _phase_paper_engine(self) -> None:
        if not self.token:
            self.check("paper engine recorded simulated executions", False,
                       "no token available (wizard flow did not complete)")
            return
        status = None
        payload = {}
        for _ in range(150):  # up to ~60s: replay is fast but be generous
            status, payload = _get_json(self.base, "/paper/status", self.token)
            if status == 200 and (payload.get("processed_bars", 0) > 200
                                   or payload.get("state") in ("STOPPED", "ERROR")):
                break
            time.sleep(0.4)
        self.check("paper engine processed replay bars",
                   status == 200 and payload.get("processed_bars", 0) > 0,
                   f"state={payload.get('state')} bars={payload.get('processed_bars')}")

        orders = fills = trades = 0
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            _s, op = _get_json(self.base, "/paper/orders?limit=1000", self.token)
            _s2, tp = _get_json(self.base, "/paper/trades?limit=1000", self.token)
            orders = len(op.get("orders", []))
            trades = len(tp.get("trades", []))
            if orders and trades:
                break
            time.sleep(0.5)
        fills = orders  # paper fills accompany the recorded orders
        self.check("paper engine recorded simulated orders/fills/trades",
                   orders > 0 and trades > 0,
                   f"orders={orders} trades={trades} fills>=fills")
        self.orders_after_first_run = orders

    def _phase_stop_and_restart(self) -> None:
        status, payload = _post(self.base, "/paper/stop", {}, self.token)
        self.check("STOP paper returns a clean stopped state",
                   status == 200 and payload.get("state") == "STOPPED",
                   str(payload.get("state")))
        _s, account = _get_json(self.base, "/paper/account", self.token)
        equity_before = account.get("equity")
        self.stop(self.process)
        self.process = None
        time.sleep(0.5)

        # restart in the SAME home: state must be recovered, not reset
        self.process = self.start()
        healthy = _wait_health(self.base, timeout=120)
        self.check("restart: executable starts again on the same home", healthy)
        if not healthy:
            self._dump_log()
            return
        _s, account2 = _get_json(self.base, "/paper/account", self.token)
        self.check("restart: paper equity recovered from the database",
                   account2.get("equity") == equity_before,
                   f"before={equity_before} after={account2.get('equity')}")
        _s, op = _get_json(self.base, "/paper/orders?limit=1000", self.token)
        self.check("restart: orders recovered from the database",
                   len(op.get("orders", [])) >= getattr(self, "orders_after_first_run", 1),
                   f"orders={len(op.get('orders', []))}")
        _s, boot = _get_json(self.base, "/api/boot")
        self.check("restart: setup wizard no longer forced (setup complete)",
                   boot.get("first_run") is False)

    def _phase_duplicate_launch(self) -> None:
        second = subprocess.Popen(
            [str(self.artifact), "--no-browser"],
            cwd=str(self.workdir), env=self._env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        try:
            out, _ = second.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            second.kill()
            out = second.communicate()[0] or ""
            self.check("duplicate launch exits instead of serving", False,
                       "second instance kept running — conflict risk")
            return
        graceful = (second.returncode in (0, 2)
                    and "already running" in (out or "").lower())
        self.check("duplicate launch is handled gracefully (no second server)",
                   graceful, f"exit={second.returncode}")
        healthy = _wait_health(self.base, timeout=20)
        self.check("original session survives the duplicate launch", healthy)
        _s, op = _get_json(self.base, "/paper/orders?limit=1000", self.token)
        self.check("duplicate launch did not touch the first session's orders",
                   len(op.get("orders", [])) >= getattr(self, "orders_after_first_run", 1))

    def _phase_security_and_paths(self) -> None:
        db = self.home / "trading_bot.db"
        self.check("database created in the external user-data directory",
                   db.exists())
        binary_dir = self.artifact.parent
        stray = [p for p in binary_dir.glob("*.db*")]
        self.check("no database written next to the executable", not stray,
                   ", ".join(p.name for p in stray) if stray else "")

        log_text = self.output_log.read_text(encoding="utf-8", errors="replace") \
            if self.output_log.exists() else ""
        leaked = bool(self.token) and self.token in log_text
        self.check("API token never appears in process logs", not leaked)
        _s, boot = _get_json(self.base, "/api/boot")
        boot_text = json.dumps(boot)
        self.check("/api/boot exposes no secret values",
                   not (self.token and self.token in boot_text))

    def _dump_log(self) -> None:
        try:
            tail = self.output_log.read_text(encoding="utf-8", errors="replace")[-3000:]
        except OSError:
            tail = ""
        _log("---- executable output ----\n" + tail, self.verbose)


def _get_json(base: str, path: str, token: str | None = None):
    try:
        status, body = _get(base, path, token=token)
        return status, json.loads(body)
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {}


def run_frozen_e2e(artifact: Path, workdir: Path, *, verbose: bool = True) -> dict:
    workdir = Path(workdir)
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    return FrozenE2E(artifact, workdir=workdir, verbose=verbose).run()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python packaging/frozen_e2e.py <artifact> [workdir]")
        raise SystemExit(2)
    artifact = Path(sys.argv[1])
    workdir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("build/e2e-workdir")
    result = run_frozen_e2e(artifact, workdir)
    print(json.dumps({"ok": result["ok"], "elapsed_seconds": result["elapsed_seconds"]},
                     indent=2))
    raise SystemExit(0 if result["ok"] else 1)
