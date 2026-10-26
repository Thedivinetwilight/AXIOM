"""Fail-closed payload hygiene checks for Windows release artifacts.

Only the reviewed synthetic DEMO fixtures are eligible from ``data/``.  The
checks below are intentionally file/payload checks, not claims that a string
scanner can prove every possible secret absent.  Exact-clean-source builds,
explicit spec inputs, and per-user storage outside the bundle are the other
parts of that boundary.
"""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import sys
import zipfile


DEMO_DATA_FILES = (
    ".gitkeep",
    "example_grids.json",
    "example_scoring.json",
    "sample.csv",
    "sample_1h.csv",
    "sample_alt.csv",
    "sample_alt_1h.csv",
    "tournament_datasets.json",
)
SQLITE_MAGIC = b"SQLite format 3\x00"
FORBIDDEN_SUFFIXES = (
    ".db", ".db-shm", ".db-wal", ".db-journal",
    ".db3", ".sqlite", ".sqlite-shm", ".sqlite-wal", ".sqlite-journal",
    ".sqlite3", ".sqlite3-shm", ".sqlite3-wal", ".sqlite3-journal",
    ".safetensors", ".pt", ".pth", ".ckpt", ".onnx",
    ".paper.lock", ".jsonl",
)
FORBIDDEN_NAMES = {
    ".env", ".env.example", "axiom.env", "credentials.json",
    "credential.json", "secrets.json", "token.json", "dpapi.json",
}


class PayloadError(ValueError):
    """A release payload contains a forbidden or unreadable entry."""


def normalized_name(value: str | Path) -> str:
    name = str(value).replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return name.lstrip("/")


def forbidden_name_reason(value: str | Path) -> str | None:
    """Return a deterministic reason for a forbidden path, otherwise None."""
    name = normalized_name(value)
    lower = name.lower()
    path = PurePosixPath(lower)
    base = path.name
    padded = f"/{lower.strip('/')}/"
    if base in FORBIDDEN_NAMES or base.startswith(".env."):
        return "credential/environment file"
    if lower.endswith(FORBIDDEN_SUFFIXES):
        return "database, weight, lock, or local-log suffix"
    if "/data/autonomy/" in padded or "/data/cache/" in padded:
        return "autonomy/cache operator state directory"
    if "/results/" in padded:
        return "generated research/results directory"
    if "/logs/" in padded and lower.endswith((".log", ".txt", ".json", ".jsonl")):
        return "runtime log directory"
    return None


def check_payload(name: str | Path, prefix: bytes = b"") -> None:
    reason = forbidden_name_reason(name)
    if reason:
        raise PayloadError(f"{normalized_name(name)}: {reason}")
    if prefix.startswith(SQLITE_MAGIC):
        raise PayloadError(f"{normalized_name(name)}: embedded SQLite database header")


def demo_data_sources(root: Path) -> list[tuple[Path, str]]:
    """Return only reviewed DEMO inputs; never enumerate operator state."""
    root = Path(root).resolve()
    data_root = root / "data"
    if data_root.is_symlink() or not data_root.is_dir():
        raise PayloadError("data must be a real source directory, not a symlink")
    result = []
    for name in DEMO_DATA_FILES:
        source = data_root / name
        if source.is_symlink() or not source.is_file():
            raise PayloadError(f"reviewed DEMO fixture is missing or linked: data/{name}")
        check_payload(f"data/{name}", source.read_bytes()[:len(SQLITE_MAGIC)])
        result.append((source, "data"))
    return result


def assert_tree_clean(root: Path) -> int:
    root = Path(root)
    if not root.is_dir():
        raise PayloadError(f"payload directory is missing: {root}")
    files = [path for path in root.rglob("*") if path.is_file()]
    if not files:
        raise PayloadError(f"payload directory is empty: {root}")
    for path in files:
        relative = path.relative_to(root)
        with path.open("rb") as handle:
            prefix = handle.read(len(SQLITE_MAGIC))
        check_payload(relative, prefix)
    return len(files)


def assert_zip_clean(path: Path) -> int:
    path = Path(path)
    if not path.is_file():
        raise PayloadError(f"ZIP is missing: {path}")
    with zipfile.ZipFile(path) as archive:
        files = [item for item in archive.infolist() if not item.is_dir()]
        if not files:
            raise PayloadError(f"ZIP is empty: {path}")
        for item in files:
            with archive.open(item) as handle:
                prefix = handle.read(len(SQLITE_MAGIC))
            check_payload(item.filename, prefix)
    return len(files)


def assert_onefile_clean(path: Path) -> int:
    """Inspect every PyInstaller CArchive name and decompressed payload prefix."""
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as exc:  # pragma: no cover - Windows build dependency
        raise PayloadError("PyInstaller is required to inspect the onefile archive") from exc
    path = Path(path)
    if not path.is_file():
        raise PayloadError(f"onefile executable is missing: {path}")
    try:
        reader = CArchiveReader(str(path))
        entries = list(reader.toc)
        if not entries:
            raise PayloadError("onefile CArchive table of contents is empty")
        for name in entries:
            check_payload(name, reader.extract(name)[:len(SQLITE_MAGIC)])
    except PayloadError:
        raise
    except Exception as exc:
        raise PayloadError(f"cannot inspect onefile CArchive: {type(exc).__name__}: {exc}") from exc
    return len(entries)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("source", "tree", "zip", "onefile"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.kind == "source":
            count = len(demo_data_sources(args.path))
            detail = f"{count} explicit DEMO source fixtures"
        elif args.kind == "tree":
            count = assert_tree_clean(args.path)
            detail = f"{count} onedir files"
        elif args.kind == "zip":
            count = assert_zip_clean(args.path)
            detail = f"{count} ZIP entries"
        else:
            count = assert_onefile_clean(args.path)
            detail = f"{count} onefile CArchive entries"
    except Exception as exc:
        print(f"FORBIDDEN/INVALID PAYLOAD: {exc}", file=sys.stderr)
        return 1
    print(f"PASS payload hygiene: {args.kind}: {detail}; no forbidden names or SQLite payloads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
