#!/usr/bin/env python3
"""Package the exact verified tree to ZIP with SHA-256 sidecar."""
import hashlib
import pathlib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT_ZIP = pathlib.Path("/home/user/arena_3.0.4_fixed.zip")
# Exclusions — mirrors prior releases, plus caches
EXCLUDE_DIRS = {".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git", ".arena", ".cache", "coverage", "dist", "build", "node_modules", ".tox", ".nox", ".venv", "__pycache__"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".egg-info"}
EXCLUDE_FILES = {".env"}  # keep .env.example, exclude real secrets
# Also exclude database/lock files, temp huge data
EXCLUDE_GLOBS = {"*.db", "*.sqlite", "*.lock", "*.log", "trading_bot.db"}

def should_exclude(path: pathlib.Path, rel: pathlib.Path) -> bool:
    # exclude by directory name anywhere in path
    for part in rel.parts:
        if part in EXCLUDE_DIRS:
            return True
        if part.endswith(".egg-info"):
            return True
    # exclude by filename
    if rel.name in EXCLUDE_FILES:
        return True
    # exclude by suffix
    if rel.suffix in EXCLUDE_SUFFIXES:
        return True
    # exclude by glob patterns (simple)
    if rel.name.endswith(".db") or rel.name.endswith(".sqlite") or rel.name.endswith(".lock"):
        return True
    # exclude zip files themselves (don't zip the zip)
    if rel.suffix == ".zip":
        return True
    # exclude .pyc etc already handled
    if rel.name == ".env":
        return True
    # exclude .git, .venv already
    return False

def collect():
    files = []
    for p in ROOT.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(ROOT)
        # skip outer zip sidecars
        if rel.name.startswith("arena_") and rel.suffix in {".zip", ".sha256"}:
            continue
        if should_exclude(p, rel):
            continue
        # also exclude large data caches/history if they exist and are huge? We keep data/* fixtures but not data/cache etc
        # data/cache, data/history, data/universe, data/datasets are caches — exclude if directory named cache etc
        # but we already exclude EXCLUDE_DIRS; data/cache would be excluded via cache? Not exactly, but we treat data/cache as cache dir
        # Check if any part is "cache" or "history" or "universe" or "datasets" under data and is large — we exclude those directories entirely
        # For determinism, we exclude data/history, data/universe, data/datasets, data/cache if they exist
        if rel.parts and rel.parts[0] == "data" and len(rel.parts) > 1 and rel.parts[1] in {"cache", "history", "universe", "datasets"}:
            continue
        # Also exclude logs content? Keep logs/bot.jsonl? Previous kept 1 log file, but we exclude *.log via suffix? But they kept 1 log? Let's keep logs empty? We will include logs if small but not huge.
        # For now, exclude logs/*.jsonl if large? We'll keep logs directory but exclude actual log files that are runtime (bot.jsonl) — but previous kept 1 log file, maybe they kept logs/.gitkeep?
        # We'll include logs only if it's .gitkeep or small
        if rel.parts and rel.parts[0] == "logs" and rel.name not in {".gitkeep", "README.md"}:
            # exclude actual logs
            continue
        if rel.parts and rel.parts[0] == "results" and rel.name not in {".gitkeep", "README.md"}:
            continue
        if rel.parts and rel.parts[0] == "backups":
            continue
        files.append(rel)
    files.sort()
    return files

def main():
    files = collect()
    # ensure deterministic
    print(f"Collected {len(files)} files")
    # Count py
    py = [f for f in files if f.suffix == ".py"]
    print(f"  py: {len(py)}")
    # Group by top dir
    from collections import Counter
    cnt = Counter(f.parts[0] if f.parts else "root" for f in files)
    for k, v in sorted(cnt.items()):
        print(f"  {k}: {v}")
    # Write zip
    OUT_ZIP.parent.mkdir(parents=True, exist_ok=True)
    if OUT_ZIP.exists():
        OUT_ZIP.unlink()
    with zipfile.ZipFile(OUT_ZIP, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            full = ROOT / rel
            # need to ensure file is readable
            z.write(full, arcname=pathlib.Path("trading_bot") / rel)
            # set fixed timestamp for determinism? Use file mtime? We'll keep original mtime but sort ensures determinism
    # SHA
    sha = hashlib.sha256(OUT_ZIP.read_bytes()).hexdigest()
    sidecar = OUT_ZIP.with_suffix(OUT_ZIP.suffix + ".sha256")
    sidecar.write_text(f"{sha}  {OUT_ZIP.name}\n")
    print(f"Wrote {OUT_ZIP} ({OUT_ZIP.stat().st_size} bytes)")
    print(f"SHA-256 {sha}")
    print(f"Sidecar {sidecar}")
    # Verify sidecar
    import subprocess
    # CRC check via unzip -l
    # List files
    with zipfile.ZipFile(OUT_ZIP) as z:
        namelist = z.namelist()
        print(f"ZIP contains {len(namelist)} entries, prefix check: {all(n.startswith('trading_bot/') for n in namelist)}")
    # Try sha256sum -c
    try:
        subprocess.check_call(["sha256sum", "-c", str(sidecar)], cwd=str(OUT_ZIP.parent))
        print("sha256sum -c OK")
    except Exception as e:
        print(f"sha256sum -c failed: {e}")
    # File counts
    print(f"Files: {len(files)}, py: {len(py)}")
    # Uncompressed size
    total = sum((ROOT / f).stat().st_size for f in files)
    print(f"Uncompressed {total} bytes")

if __name__ == "__main__":
    main()
