"""End-to-end verification of the ACTUAL Arena-Uninstall executable.

Runs the built binary (Windows ``Arena-Uninstall.exe`` or a locally built
binary on another platform) against a complete fake machine: an installation
tree, a per-user home with trading history, a setup package in Downloads,
look-alike files that must be rejected, and a quarantine area. It then proves
the whole user-visible flow:

    scan (nothing changes)  ->  cancel  ->  wrong confirmation refused
    ->  confirmed run  ->  quarantine  ->  restore  ->  locked file reported
    ->  plan/apply  ->  own files never removed

Nothing on the real machine is scanned: every well-known location is pointed
into a temporary directory and the drive walk is disabled.

Called by ``build_uninstaller.py --test`` and by
``tests/test_arena_uninstaller_exe.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PHRASE = "DELETE"


def _result(name: str, ok: bool, detail: str = "") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _run(artifact: Path, args: list[str], env: dict[str, str],
         stdin_text: str | None = None, timeout: int = 300):
    return subprocess.run(
        [str(artifact)] + args,
        env=env, capture_output=True, text=True, timeout=timeout,
        input=stdin_text,
    )


def _world(tmp: Path):
    from tests.uninstaller_fixtures import make_world
    return make_world(tmp, false_positives=True)


def _env_for(world) -> dict[str, str]:
    from tests.uninstaller_fixtures import merged_env
    env = merged_env(world)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_uninstaller_e2e(artifact: Path, workdir: Path) -> dict:
    results: list[dict] = []
    artifact = Path(artifact).resolve()
    workdir = Path(workdir)
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)

    results.append(_result("artifact exists", artifact.is_file(),
                           str(artifact)))
    if not artifact.is_file():
        return {"ok": False, "results": results}

    # -- identity ---------------------------------------------------------
    world = _world(workdir)
    env = _env_for(world)
    proc = _run(artifact, ["--version"], env)
    results.append(_result(
        "version banner", proc.returncode == 0 and "Arena-Uninstall" in proc.stdout,
        (proc.stdout or proc.stderr).strip()[:200]))

    proc = _run(artifact, ["--list-categories"], env)
    categories_ok = proc.returncode == 0 and all(
        name in proc.stdout for name in ("installation", "trading-history",
                                         "credentials", "source-repository",
                                         "ambiguous"))
    results.append(_result("category catalogue", categories_ok,
                           (proc.stdout or proc.stderr).strip()[:200]))

    common = ["--mode", "deep", "--scan-path", str(world.root), "--no-drives",
              "--home", str(world.tool_home), "--no-elevation"]

    # -- 1. scan: complete inventory, nothing touched ---------------------
    plan = workdir / "inventory.json"
    snapshot = _snapshot(world.root)
    proc = _run(artifact, common + ["--scan-only", "--json", str(plan)], env)
    data = _read_json(plan)
    ok_scan = (proc.returncode == 0 and data is not None
               and _has_category(data, "installation")
               and _has_category(data, "setup-installer")
               and _has_category(data, "ambiguous")
               and _snapshot(world.root) == snapshot
               and all(m.get("reasons") for m in (data or {}).get("matches", [])
                       if m.get("category") != "ambiguous"))
    results.append(_result(
        "scan builds a complete, reasoned inventory and changes nothing",
        ok_scan,
        f"exit={proc.returncode} matches={len((data or {}).get('matches', []))} "
        f"{(proc.stderr or '')[-200:]}"))

    protected_untouched = bool(data) and not [
        m for m in data["matches"]
        if m["category"] in ("trading-history", "logs", "configuration",
                             "credentials", "source-repository", "research",
                             "development") and m.get("selected")]
    results.append(_result("nothing preserved is pre-selected",
                           protected_untouched, ""))

    ambiguous_consistent = bool(data) and all(
        m.get("selected") is False for m in data["matches"]
        if m.get("category") == "ambiguous")
    results.append(_result("ambiguous matches are never pre-selected",
                           ambiguous_consistent, ""))

    # -- 2. cancellation and confirmation --------------------------------
    proc = _run(artifact, common, env)
    results.append(_result(
        "deletion without confirmation is refused",
        proc.returncode == 3 and (world.install_root / "Arena.exe").exists(),
        f"exit={proc.returncode}"))

    proc = _run(artifact, common + ["--confirm-delete", "yes"], env)
    results.append(_result(
        "wrong confirmation phrase is refused",
        proc.returncode == 3 and (world.install_root / "Arena.exe").exists(),
        f"exit={proc.returncode}"))

    proc = _run(artifact, common + ["--interactive"], env, stdin_text="q\n")
    results.append(_result(
        "interactive cancel changes nothing",
        proc.returncode == 3 and (world.install_root / "Arena.exe").exists(),
        f"exit={proc.returncode}"))

    # -- 3. the confirmed run -------------------------------------------
    report_path = workdir / "report.json"
    proc = _run(artifact, common + ["--confirm-delete", PHRASE,
                                    "--report", str(report_path)], env)
    report = _read_json(report_path)
    results.append(_result(
        "confirmed deep clean succeeds",
        proc.returncode == 0 and report is not None,
        f"exit={proc.returncode} {(proc.stdout or '')[-200:]}{proc.stderr[-200:]}"))

    results.append(_result(
        "installed executable was quarantined",
        not (world.install_root / "Arena.exe").exists()
        and report is not None
        and any(r["action"] == "quarantined" for r in report["results"]),
        ""))

    kept = [
        world.install_root / "trading_bot.db",
        world.install_root / "data" / "history" / "DEMO.csv",
        world.install_root / "logs" / "bot.jsonl",
        world.install_root / ".env",
        world.runtime_home / "trading_bot.db",
        world.runtime_home / "data" / "history" / "AAA.csv",
        world.downloads / "arena-notes.txt",
        world.root / "Games" / "AXIOM-notes.txt",
        world.root / "Games" / "Arena Games" / "readme.txt",
    ]
    missing = [str(p) for p in kept if not p.exists()]
    results.append(_result("trading history, logs, config and unrelated files "
                           "are preserved", not missing,
                           f"missing: {missing}" if missing else ""))

    secret_hits = _find_secrets(world.tool_home, "SECRET-TOKEN-VALUE",
                                "live-secret-123")
    results.append(_result("cleanup log contains no credentials",
                           not secret_hits, str(secret_hits[:3])))

    # -- 4. quarantine restore ------------------------------------------
    qdirs = sorted((world.tool_home / "quarantine").glob("*"))
    proc = _run(artifact, ["--restore", str(qdirs[-1])] if qdirs else
                ["--restore", str(world.tool_home / "missing")], env)
    results.append(_result(
        "quarantined items restore to their exact paths",
        bool(qdirs) and proc.returncode == 0
        and (world.install_root / "Arena.exe").exists(),
        f"exit={proc.returncode}"))

    # -- 5. the tool's own files ----------------------------------------
    own_copy = world.downloads / "Arena-Uninstall.exe"
    shutil.copy2(artifact, own_copy)
    inventory2 = workdir / "inventory2.json"
    proc = _run(artifact, common + ["--scan-only", "--json", str(inventory2)], env)
    data2 = _read_json(inventory2) or {"matches": []}
    own = [m for m in data2["matches"]
           if os.path.normcase(m["path"]) == os.path.normcase(str(own_copy))]
    # A real (PE) build is recognised by its version resource; a launcher
    # shim used for harness testing is not a PE and may only ever be listed
    # as ambiguous. In both cases it must never be pre-selected.
    is_pe_build = artifact.read_bytes()[:2] == b"MZ"
    own_ok = bool(own) and own[0]["selected"] is False
    if is_pe_build:
        own_ok = own_ok and own[0]["category"] in ("uninstall-tool", "self")
    results.append(_result(
        "a copy of this tool is identified and never pre-selected",
        own_ok, str(own[:1])))

    # -- 6. locked file (Windows only) ----------------------------------
    if os.name == "nt":
        locked = world.downloads / "AXIOM-Setup-3.2.4.exe"
        handle = open(locked, "rb")
        try:
            proc = _run(artifact, common + ["--permanent", "--select-category",
                                            "setup-installer", "--confirm-delete",
                                            PHRASE], env)
        finally:
            handle.close()
        results.append(_result(
            "a locked file is reported, not fatal",
            proc.returncode == 4 and locked.exists()
            and "failed" in proc.stdout.lower(),
            f"exit={proc.returncode}"))

    # -- 7. plan / apply round trip -------------------------------------
    plan2 = workdir / "plan2.json"
    _run(artifact, common + ["--plan", str(plan2)], env)
    before = (world.install_root / "Arena.exe").exists()
    proc = _run(artifact, ["--apply", str(plan2), "--confirm-delete", PHRASE,
                           "--home", str(world.tool_home), "--no-elevation"], env)
    results.append(_result(
        "reviewed plan applies and removes the executable",
        plan2.exists() and proc.returncode == 0
        and not (world.install_root / "Arena.exe").exists(),
        f"before={before} exit={proc.returncode}"))

    return {"ok": all(r["ok"] for r in results), "results": results}


def _snapshot(root: Path) -> dict[str, float]:
    out = {}
    for path in root.rglob("*"):
        try:
            if path.is_file():
                out[str(path)] = path.stat().st_mtime
        except OSError:
            continue
    return out


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _has_category(data: dict, category: str) -> bool:
    return any(m.get("category") == category for m in data.get("matches", []))


def _find_secrets(root: Path, *needles: str) -> list[str]:
    hits = []
    for path in root.rglob("*"):
        try:
            if not path.is_file() or path.suffix not in (".log", ".json", ".txt"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for needle in needles:
            if needle in text:
                hits.append(f"{path}: {needle}")
    return hits


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--workdir", type=Path, default=None)
    args = parser.parse_args(argv)
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="arena-uninstall-e2e-"))
    result = run_uninstaller_e2e(args.artifact, workdir)
    for entry in result["results"]:
        mark = "ok  " if entry["ok"] else "FAIL"
        print(f"[{mark}] {entry['name']}: {entry['detail']}")
    print("\nE2E:", "PASSED" if result["ok"] else "FAILED")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
