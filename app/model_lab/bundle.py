"""Portable research-run bundles: export, offline verify, deterministic replay.

A bundle is a self-contained evidence package for one completed Model Lab run.  It
pins the canonical research manifest, the raw dataset identity (bars + hash), the
corporate-action sidecar, the run configuration, the feature-schema fingerprint,
the search family, the software fingerprint, the dependency lock, the run outputs
(inside the manifest) and the qualification record — every payload addressed by
its own SHA-256 and enumerated in one index with size and hash.

Three containers carry the same document:

* a **directory** (``bundle.json`` + ``artifacts/<sha256>.<ext>``),
* a **zip archive** with the same layout,
* a **sealed JSON document** with the payloads inlined (used by the HTTP API).

Safety properties, all enforced here rather than by convention:

* **Offline.** Nothing in this module opens a socket, reads a credential, contacts
  a broker or submits an order.  Verification and replay touch only bundle bytes.
* **Content-addressed.** An artifact path is ``artifacts/<sha256>.<ext>``; a path
  that does not match its own hash, an absolute path, a ``..`` segment, a
  backslash, a symlink, a duplicate path or role, a file that the index does not
  enumerate, an oversized payload and a missing required role are all rejected
  with a specific reason code.
* **Integrity is not truth.** A matching hash proves the bytes are the bytes that
  were bundled.  It does not prove the provider's data is genuine, and this module
  never says otherwise.
* **No promotion.** A bundle or a successful replay can never upgrade a BLOCKED
  or UNVERIFIED qualification, never grants execution authority, and never claims
  profitability.  The effective status reported by a replay is always the *worse*
  of the bundled and the re-derived verdict.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Protocol

from app.data.corporate_actions import canonical_bars_sha256
from app.data.models import Candle, parse_timestamp
from app.model_lab.qualification import (
    QUALIFICATION_SCHEMA,
    STATUS_BLOCKED,
    STATUS_QUALIFIED,
    STATUS_UNVERIFIED,
    assert_no_execution_authority,
    evaluate_qualification,
    qualification_authority,
)
from app.model_lab.research_pipeline import (
    FEATURE_NAMES,
    execute_research_pipeline,
    manifest_digest,
)

BUNDLE_SCHEMA = "axiom.research.bundle.v1"
BUNDLE_FORMAT_VERSION = 1
SUPPORTED_FORMAT_VERSIONS = frozenset({1})

VERIFY_SCHEMA = "axiom.research.bundle.verify.v1"
REPLAY_SCHEMA = "axiom.research.bundle.replay.v1"

BUNDLE_INDEX_NAME = "bundle.json"
ARTIFACT_DIR = "artifacts"

#: Default replay tolerance.  The pipeline is deterministic, so an exact match is
#: expected; the tolerance only absorbs float formatting noise on other platforms.
DEFAULT_ABS_TOL = 1e-9
DEFAULT_REL_TOL = 1e-9

QUALIFICATION_RANK = {STATUS_QUALIFIED: 0, STATUS_UNVERIFIED: 1, STATUS_BLOCKED: 2}


# --------------------------------------------------------------------------- #
# Reason codes (stable; documented in docs/RESEARCH_BUNDLES.md)
# --------------------------------------------------------------------------- #
REASON_CODES: dict[str, str] = {
    # container / index
    "BUNDLE_NOT_FOUND": "the bundle path does not exist",
    "BUNDLE_MANIFEST_MISSING": "the bundle index (bundle.json) is absent",
    "BUNDLE_MANIFEST_UNREADABLE": "the bundle index is not valid JSON",
    "BUNDLE_MANIFEST_INVALID": "the bundle index is missing required fields",
    "UNSUPPORTED_BUNDLE_SCHEMA": "the document is not an axiom.research.bundle.v1 document",
    "UNSUPPORTED_BUNDLE_VERSION": "the bundle format_version is newer than this build supports",
    "BUNDLE_DIGEST_MISMATCH": "bundle_sha256 does not match the recomputed index digest",
    # artifact index
    "ARTIFACT_ROLE_MISSING": "a required artifact role is absent from the index",
    "ARTIFACT_NOT_IN_BUNDLE": "the index enumerates an artifact that the container does not hold",
    "ARTIFACT_UNREFERENCED": "the container holds a file the index does not enumerate",
    "ARTIFACT_HASH_MISMATCH": "an artifact's bytes do not hash to the enumerated SHA-256",
    "ARTIFACT_SIZE_MISMATCH": "an artifact's byte length differs from the enumerated size",
    "ARTIFACT_DUPLICATE_PATH": "two index entries share a path",
    "ARTIFACT_DUPLICATE_ROLE": "two index entries share a role",
    "ARTIFACT_PATH_UNSAFE": "an artifact path is absolute, traversing, or not normalised",
    "ARTIFACT_PATH_NOT_CONTENT_ADDRESSED": "an artifact path is not artifacts/<sha256>.<ext>",
    "ARTIFACT_PATH_HASH_MISMATCH": "the SHA-256 in an artifact path differs from its index entry",
    "ARTIFACT_SYMLINK": "an artifact is a symlink instead of a regular file",
    "ARTIFACT_OVERSIZED": "an artifact exceeds the configured size limit",
    "ARTIFACTS_TOO_MANY": "the bundle holds more artifacts than the configured limit",
    "BUNDLE_TOO_LARGE": "the bundle total payload exceeds the configured limit",
    # semantic consistency
    "MANIFEST_TAMPERED": "the bundled research manifest does not match its own digest",
    "DATASET_HASH_MISMATCH": "the bundled bars do not re-hash to the pinned dataset digest",
    "DATASET_META_MISMATCH": "the dataset metadata contradicts the research manifest",
    "ACTION_HASH_MISMATCH": "the corporate-action sidecar does not match its pinned hash",
    "SECTION_DIGEST_MISMATCH": "a pinned manifest section does not match the manifest",
    "SOFTWARE_FINGERPRINT_MISMATCH": "the software fingerprint artifact contradicts the manifest",
    "DEPENDENCY_LOCK_MISSING": "the bundle does not pin a dependency lock",
    "DEPENDENCY_LOCK_MISMATCH": "the dependency lock artifact does not match its pin",
    "RUN_CONFIG_MISMATCH": "the run configuration contradicts the manifest",
    "BARS_UNREADABLE": "the bundled bars are not a parseable bar series",
    # qualification
    "QUALIFICATION_RECORD_MISSING": "the bundle does not carry a qualification record",
    "QUALIFICATION_ALTERED": "the bundled qualification record does not re-derive from the bundle",
    "INDEX_RUN_MISMATCH": "the bundle index run block contradicts the pinned manifest",
    "QUALIFICATION_SCOPE_VIOLATION": "the qualification record claims execution authority",
    "QUALIFICATION_INPUTS_MISSING": "the bundle does not pin the qualification inputs",
    "CONFORMANCE_EVIDENCE_MISSING": "the bundle does not pin provider-conformance evidence",
    # secrets
    "SECRET_DETECTED": "an artifact contains what looks like a credential or token",
    # replay
    "REPLAY_MANIFEST_MISMATCH": "the replayed manifest digest differs from the pinned digest",
    "REPLAY_METRIC_DRIFT": "a replayed metric differs beyond the documented tolerance",
    "REPLAY_NOT_REPRODUCIBLE": "the run cannot be replayed from the pinned inputs",
    "REPLAY_REQUIRES_KRONOS": "the pinned run evaluated Kronos, which replay cannot reproduce offline",
}

#: Artifact roles every bundle must carry.
REQUIRED_ROLES: tuple[str, ...] = (
    "run_manifest",
    "dataset_meta",
    "dataset_bars",
    "run_config",
    "feature_schema",
    "software_fingerprint",
    "dependency_lock",
    "conformance_evidence",
    "qualification_inputs",
    "qualification_record",
)

#: Optional roles (present only when the run had them).
OPTIONAL_ROLES: tuple[str, ...] = ("corporate_actions",)

ALL_ROLES: tuple[str, ...] = REQUIRED_ROLES + OPTIONAL_ROLES

_EXT_BY_MEDIA = {"application/json": "json", "text/plain": "txt"}

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"\"(access_token|api_key|api_secret|client_secret|password|secret_key)\"\s*:\s*\"[^\"]{8,}\"", re.I),
    re.compile(
        r"\b(access_token|api_key|api_secret|client_secret|password|secret_key|bearer_token)"
        r"\s*=\s*[A-Za-z0-9._\-]{8,}",
        re.I,
    ),
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b(?=\s*\"?\s*$)"),  # long opaque token at end of line
)


def mask_secrets(text: str) -> str:
    """Best-effort masking used only to make an error message shareable."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def find_secret(text: str) -> str | None:
    """Return a description of the first credential-looking pattern, else ``None``."""
    if not isinstance(text, str):
        return None
    for pattern in _SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            return mask_secrets(match.group(0))[:60]
    return None


class BundleError(ValueError):
    """Raised when a bundle cannot be exported, read or replayed."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class BundleLimits:
    """Hard limits applied on export, on read and on verification."""

    max_artifacts: int = 64
    max_artifact_bytes: int = 32 * 1024 * 1024
    max_total_bytes: int = 96 * 1024 * 1024
    max_index_bytes: int = 4 * 1024 * 1024

    def as_dict(self) -> dict[str, int]:
        return {
            "max_artifacts": self.max_artifacts,
            "max_artifact_bytes": self.max_artifact_bytes,
            "max_total_bytes": self.max_total_bytes,
            "max_index_bytes": self.max_index_bytes,
        }


DEFAULT_LIMITS = BundleLimits()


# --------------------------------------------------------------------------- #
# Canonical helpers
# --------------------------------------------------------------------------- #
def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def pretty(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, indent=2, default=str).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical(value).encode("utf-8"))


def bars_to_rows(bars: Iterable[Candle]) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": bar.timestamp.isoformat(),
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
            "symbol": bar.symbol,
        }
        for bar in bars
    ]


def rows_to_bars(rows: Any) -> list[Candle]:
    if not isinstance(rows, list) or not rows:
        raise BundleError("BARS_UNREADABLE", "the bars artifact is not a non-empty JSON array")
    bars: list[Candle] = []
    for index, row in enumerate(rows):
        try:
            bars.append(
                Candle(
                    timestamp=parse_timestamp(str(row["timestamp"])),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    symbol=str(row["symbol"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BundleError("BARS_UNREADABLE", f"bar {index} is malformed ({type(exc).__name__})") from exc
    return bars


# --------------------------------------------------------------------------- #
# Containers
# --------------------------------------------------------------------------- #
class BundleContainer(Protocol):
    def names(self) -> list[str]:  # pragma: no cover - protocol
        ...

    def read(self, name: str) -> bytes:  # pragma: no cover - protocol
        ...

    def kind(self) -> str:  # pragma: no cover - protocol
        ...


@dataclass
class DirectoryContainer:
    root: Path
    _names: list[str] = field(default_factory=list, repr=False)

    def kind(self) -> str:
        return "DIRECTORY"

    def names(self) -> list[str]:
        if self._names:
            return list(self._names)
        found: list[str] = []
        artifact_root = self.root / ARTIFACT_DIR
        if artifact_root.is_dir():
            for path in artifact_root.rglob("*"):
                if path.is_symlink():
                    found.append(path.relative_to(self.root).as_posix())
                    continue
                if path.is_file():
                    found.append(path.relative_to(self.root).as_posix())
        self._names = sorted(found)
        return list(self._names)

    def read(self, name: str) -> bytes:
        path = self.root / name
        # A symlink could point anywhere; refuse it outright.
        if path.is_symlink():
            raise BundleError("ARTIFACT_SYMLINK", name)
        return path.read_bytes()


@dataclass
class ZipContainer:
    path: Path
    _archive: zipfile.ZipFile | None = None

    def kind(self) -> str:
        return "ZIP"

    def _open(self) -> zipfile.ZipFile:
        if self._archive is None:
            self._archive = zipfile.ZipFile(self.path, "r")
        return self._archive

    def names(self) -> list[str]:
        return sorted(
            info.filename
            for info in self._open().infolist()
            if not info.is_dir() and info.filename != BUNDLE_INDEX_NAME
        )

    def read(self, name: str) -> bytes:
        return self._open().read(name)

    def close(self) -> None:
        if self._archive is not None:
            self._archive.close()
            self._archive = None


@dataclass
class SealedContainer:
    """A single JSON document with the payloads inlined."""

    document: dict[str, Any]

    def kind(self) -> str:
        return "SEALED_JSON"

    def names(self) -> list[str]:
        inline = self.document.get("inline")
        if not isinstance(inline, dict):
            return []
        return sorted(str(name) for name in inline)

    def read(self, name: str) -> bytes:
        inline = self.document.get("inline") or {}
        entry = inline.get(name)
        if not isinstance(entry, dict):
            raise BundleError("ARTIFACT_NOT_IN_BUNDLE", name)
        encoding = str(entry.get("encoding") or "utf-8")
        content = entry.get("content")
        if not isinstance(content, str):
            raise BundleError("ARTIFACT_NOT_IN_BUNDLE", f"{name} has no inline content")
        if encoding == "base64":
            return base64.b64decode(content.encode("ascii"))
        if encoding != "utf-8":
            raise BundleError("ARTIFACT_NOT_IN_BUNDLE", f"{name} uses unsupported encoding {encoding!r}")
        return content.encode("utf-8")


def lookahead_safe_name(name: str) -> bool:
    """True when ``name`` is a relative, normalised, non-traversing POSIX path."""
    if not isinstance(name, str) or not name or len(name) > 200:
        return False
    if "\x00" in name or "\\" in name or name.startswith("/"):
        return False
    # Percent-encoded separators or traversal are rejected outright: a bundle name is
    # never URL-decoded by AXIOM, and accepting one would invite a consumer that does
    # to escape the bundle directory.
    if "%" in name or ":" in name:
        return False
    pure = PurePosixPath(name)
    if pure.is_absolute():
        return False
    parts = pure.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return False
    if str(pure) != name:
        return False
    return True


def is_content_addressed(name: str, digest: str) -> bool:
    if not lookahead_safe_name(name):
        return False
    pure = PurePosixPath(name)
    if len(pure.parts) != 2 or pure.parts[0] != ARTIFACT_DIR:
        return False
    stem, _, ext = pure.parts[1].rpartition(".")
    if ext not in {"json", "txt"} or not stem:
        return False
    return stem == digest


def artifact_path(digest: str, media_type: str) -> str:
    return f"{ARTIFACT_DIR}/{digest}.{_EXT_BY_MEDIA.get(media_type, 'bin')}"


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
@dataclass
class RunInputs:
    """Everything an export needs, gathered from the local store."""

    run_id: str
    manifest: dict[str, Any]
    qualification_record: dict[str, Any]
    dataset_meta: dict[str, Any]
    bars: list[Candle]
    run_config: dict[str, Any]
    dependency_lock: bytes
    corporate_actions_bytes: bytes | None = None
    conformance_report: dict[str, Any] | None = None
    qualification_provenance: dict[str, Any] | None = None
    expected_software_commit: str = ""


def _stable_conformance(report: dict[str, Any] | None) -> dict[str, Any]:
    """Strip per-run timestamps so the evidence is byte-stable."""
    if not isinstance(report, dict):
        return {}
    volatile = {"generated_at", "created_at", "started_at", "finished_at", "duration_ms"}
    out = {key: value for key, value in report.items() if key not in volatile}
    providers = out.get("providers")
    if isinstance(providers, dict):
        out["providers"] = {
            str(name): {
                key: value
                for key, value in entry.items()
                if key not in volatile and key not in {"request_paths", "request_methods", "requests_issued"}
            }
            if isinstance(entry, dict)
            else entry
            for name, entry in providers.items()
        }
    return out


def _artifact(
    role: str,
    payload: bytes,
    media_type: str,
    *,
    required: bool,
) -> dict[str, Any]:
    digest = sha256_bytes(payload)
    return {
        "_payload": payload,
        "path": artifact_path(digest, media_type),
        "role": role,
        "sha256": digest,
        "size": len(payload),
        "media_type": media_type,
        "encoding": "utf-8",
        "required": required,
    }


def build_bundle_document(
    inputs: RunInputs,
    *,
    limits: BundleLimits | None = None,
    created_at: datetime | str | None = None,
    generator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a sealed bundle document from already-gathered run inputs."""
    limits = limits or DEFAULT_LIMITS
    manifest = inputs.manifest
    if not isinstance(manifest, dict) or manifest.get("schema") != "axiom.model_lab.research_run.v1":
        raise BundleError("MANIFEST_TAMPERED", "not an axiom.model_lab.research_run.v1 manifest")
    if manifest_digest(manifest) != str(manifest.get("manifest_sha256") or ""):
        raise BundleError("MANIFEST_TAMPERED", "the research manifest does not match its own digest")
    record = inputs.qualification_record
    if not isinstance(record, dict) or record.get("schema") != QUALIFICATION_SCHEMA:
        raise BundleError("QUALIFICATION_RECORD_MISSING", "no qualification record was supplied")
    assert_no_execution_authority(record)

    dataset_sha = str((manifest.get("dataset") or {}).get("sha256") or "")
    recomputed = canonical_bars_sha256(inputs.bars)
    if dataset_sha != recomputed:
        raise BundleError(
            "DATASET_HASH_MISMATCH",
            f"bars hash to {recomputed[:12]}… but the manifest pins {dataset_sha[:12]}…",
        )

    entries: list[dict[str, Any]] = []
    entries.append(_artifact("run_manifest", pretty(manifest), "application/json", required=True))
    entries.append(_artifact("dataset_meta", pretty(inputs.dataset_meta), "application/json", required=True))
    entries.append(
        _artifact("dataset_bars", pretty(bars_to_rows(inputs.bars)), "application/json", required=True)
    )
    entries.append(_artifact("run_config", pretty(inputs.run_config), "application/json", required=True))
    feature_schema = {
        "feature_names": list(FEATURE_NAMES),
        "feature_set_sha256": sha256_json(list(FEATURE_NAMES)),
        "manifest_feature_set_sha256": (manifest.get("software") or {}).get("feature_set_sha256"),
    }
    entries.append(_artifact("feature_schema", pretty(feature_schema), "application/json", required=True))
    entries.append(
        _artifact(
            "software_fingerprint",
            pretty(manifest.get("software") or {}),
            "application/json",
            required=True,
        )
    )
    if not inputs.dependency_lock:
        raise BundleError("DEPENDENCY_LOCK_MISSING", "requirements-lock.txt was empty or unavailable")
    entries.append(
        _artifact("dependency_lock", bytes(inputs.dependency_lock), "text/plain", required=True)
    )
    conformance = _stable_conformance(inputs.conformance_report)
    entries.append(
        _artifact("conformance_evidence", pretty(conformance), "application/json", required=True)
    )
    qual_inputs = {
        "provenance": inputs.qualification_provenance or None,
        "expected_software_commit": inputs.expected_software_commit or "",
        "recomputed_dataset_sha256": recomputed,
        "conformance_evidence_sha256": sha256_json(conformance),
    }
    entries.append(
        _artifact("qualification_inputs", pretty(qual_inputs), "application/json", required=True)
    )
    entries.append(
        _artifact("qualification_record", pretty(record), "application/json", required=True)
    )
    if inputs.corporate_actions_bytes:
        entries.append(
            _artifact(
                "corporate_actions",
                bytes(inputs.corporate_actions_bytes),
                "application/json",
                required=False,
            )
        )

    # Secrets never leave the process inside a bundle.
    for entry in entries:
        hit = find_secret(entry["_payload"].decode("utf-8", errors="replace"))
        if hit:
            raise BundleError("SECRET_DETECTED", f"{entry['role']} contains a credential-like value ({hit})")

    total = sum(entry["size"] for entry in entries)
    for entry in entries:
        if entry["size"] > limits.max_artifact_bytes:
            raise BundleError("ARTIFACT_OVERSIZED", f"{entry['role']} is {entry['size']} bytes")
    if len(entries) > limits.max_artifacts:
        raise BundleError("ARTIFACTS_TOO_MANY", f"{len(entries)} artifacts")
    if total > limits.max_total_bytes:
        raise BundleError("BUNDLE_TOO_LARGE", f"{total} bytes")

    sections = _sections(manifest, inputs.run_config, entries)
    index_entries = [
        {key: value for key, value in entry.items() if key != "_payload"} for entry in entries
    ]
    index_entries.sort(key=lambda item: item["role"])

    run_block = {
        "run_id": inputs.run_id,
        "market": manifest.get("market"),
        "timeframe": manifest.get("timeframe"),
        "dataset_id": (manifest.get("dataset") or {}).get("dataset_id"),
        "manifest_sha256": manifest.get("manifest_sha256"),
        "qualification_status": record.get("status"),
        "qualification_record_sha256": record.get("record_sha256"),
        "candidate_id": (record.get("candidate") or {}).get("candidate_id"),
    }
    core = {
        "schema": BUNDLE_SCHEMA,
        "format_version": BUNDLE_FORMAT_VERSION,
        "limits": limits.as_dict(),
        "run": run_block,
        "artifacts": index_entries,
        "sections": sections,
        "qualification": _qualification_view(record),
        "execution_view": qualification_authority(record),
        "content_policy": {
            "credentials_included": False,
            "user_data_dirs_included": False,
            "unrelated_files_included": False,
            "network_calls": 0,
            "broker_order_api_calls": 0,
            "live_trading": False,
        },
        "integrity_scope": (
            "SHA-256 proves these bytes are the bytes that were bundled. It does not prove the "
            "provider's data is genuine, and it does not upgrade an operator-attested provenance "
            "claim into an independently verified fact."
        ),
    }
    bundle_sha = sha256_json(core)
    stamp = created_at or datetime.now(UTC)
    if isinstance(stamp, str):
        stamp = parse_timestamp(stamp) if stamp else datetime.now(UTC)
    document = {
        **core,
        "bundle_sha256": bundle_sha,
        "created_at": stamp.isoformat(),
        "generator": dict(generator or {}),
        "inline": {
            entry["path"]: {
                "encoding": "utf-8",
                "content": entry["_payload"].decode("utf-8"),
            }
            for entry in entries
        },
    }
    return document


def _sections(
    manifest: dict[str, Any],
    run_config: dict[str, Any],
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    by_role = {entry["role"]: entry for entry in entries}
    dataset = manifest.get("dataset") or {}
    splits = manifest.get("splits") or {}
    search_family = manifest.get("search_family") or {}
    sections: dict[str, Any] = {
        "dataset": {"sha256": sha256_json(dataset), "dataset_sha256": dataset.get("sha256")},
        "strategy_versions": {
            "sha256": sha256_json(manifest.get("strategy_versions") or {}),
            "versions": manifest.get("strategy_versions") or {},
        },
        "run_config": {"sha256": sha256_json(run_config), "artifact_sha256": by_role["run_config"]["sha256"]},
        "feature_schema": {
            "sha256": sha256_json(
                {
                    "feature_names": list(FEATURE_NAMES),
                    "feature_set_sha256": sha256_json(list(FEATURE_NAMES)),
                }
            ),
            "artifact_sha256": by_role["feature_schema"]["sha256"],
        },
        "search_family": {
            "sha256": sha256_json(search_family),
            "variants_declared": search_family.get("variants_declared"),
            "variants_evaluated": search_family.get("variants_evaluated"),
            "family_sha256": search_family.get("family_sha256"),
        },
        "folds": {
            "sha256": sha256_json(splits),
            "train_bars": (splits.get("train") or {}).get("bars"),
            "validation_bars": (splits.get("validation") or {}).get("bars"),
            "holdout_bars": (splits.get("untouched_test") or {}).get("bars"),
        },
        "benchmark": {
            "sha256": sha256_json(manifest.get("benchmark") or {}),
            "candidate_id": (manifest.get("benchmark") or {}).get("candidate_id"),
        },
        "universe": {
            "sha256": sha256_json(manifest.get("universe") or {}),
            "symbols": (manifest.get("universe") or {}).get("symbols"),
        },
        "costs": {"sha256": sha256_json(manifest.get("cost_assumptions") or {})},
        "software": {"sha256": sha256_json(manifest.get("software") or {})},
        "dependency_lock": {"sha256": by_role["dependency_lock"]["sha256"]},
        "corporate_actions": {
            "sha256": sha256_json(dataset.get("corporate_actions") or {}),
            "actions_sha256": dataset.get("actions_sha256"),
        },
        "run_outputs": {
            "sha256": sha256_json(manifest.get("candidates") or []),
            "candidates": [c.get("candidate_id") for c in (manifest.get("candidates") or [])],
        },
    }
    return sections


def _qualification_view(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": record.get("schema"),
        "status": record.get("status"),
        "record_sha256": record.get("record_sha256"),
        "rules_version": record.get("rules_version"),
        "verification_level": record.get("verification_level"),
        "counts": record.get("counts"),
        "blocked": [item.get("reason") for item in (record.get("blocked") or [])],
        "unverified": [item.get("reason") for item in (record.get("unverified") or [])],
        "execution_authority": "NONE",
        "upgrade_effect": "NONE — a bundle never re-scores, upgrades or authorizes a qualification",
    }


# --------------------------------------------------------------------------- #
# Writing containers
# --------------------------------------------------------------------------- #
def write_bundle(document: dict[str, Any], destination: Path | str, *, archive: bool = False) -> Path:
    """Materialise a sealed document as a directory or a zip archive."""
    dest = Path(destination)
    inline = document.get("inline") or {}
    index = {key: value for key, value in document.items() if key != "inline"}
    if archive:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive_file:
            archive_file.writestr(BUNDLE_INDEX_NAME, pretty(index))
            for name, entry in sorted(inline.items()):
                payload = (
                    base64.b64decode(entry["content"].encode("ascii"))
                    if entry.get("encoding") == "base64"
                    else str(entry.get("content") or "").encode("utf-8")
                )
                archive_file.writestr(name, payload)
        return dest
    if dest.exists():
        if dest.is_dir() and not any(dest.iterdir()):
            pass
        elif dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    (dest / ARTIFACT_DIR).mkdir(parents=True, exist_ok=True)
    (dest / BUNDLE_INDEX_NAME).write_bytes(pretty(index))
    for name, entry in sorted(inline.items()):
        if not lookahead_safe_name(name):
            raise BundleError("ARTIFACT_PATH_UNSAFE", name)
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            base64.b64decode(entry["content"].encode("ascii"))
            if entry.get("encoding") == "base64"
            else str(entry.get("content") or "").encode("utf-8")
        )
        target.write_bytes(payload)
    return dest


def load_bundle(source: Path | str | dict[str, Any]) -> tuple[BundleContainer, dict[str, Any]]:
    """Open any supported container and return ``(container, index)``."""
    if isinstance(source, dict):
        return SealedContainer(document=source), source
    path = Path(source)
    if not path.exists():
        raise BundleError("BUNDLE_NOT_FOUND", str(path))
    if path.is_dir():
        index_path = path / BUNDLE_INDEX_NAME
        if not index_path.is_file():
            raise BundleError("BUNDLE_MANIFEST_MISSING", str(index_path))
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BundleError("BUNDLE_MANIFEST_UNREADABLE", f"{type(exc).__name__}") from exc
        return DirectoryContainer(root=path), index
    if zipfile.is_zipfile(path):
        container = ZipContainer(path=path)
        try:
            index = json.loads(container.read(BUNDLE_INDEX_NAME))
        except (KeyError, ValueError, zipfile.BadZipFile) as exc:
            raise BundleError("BUNDLE_MANIFEST_MISSING", f"{type(exc).__name__}") from exc
        return container, index
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError("BUNDLE_MANIFEST_UNREADABLE", f"{type(exc).__name__}") from exc
    return SealedContainer(document=document), document


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
@dataclass
class Finding:
    code: str
    detail: str
    role: str = ""
    artifact: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "role": self.role,
            "artifact": self.artifact,
            "message": REASON_CODES.get(self.code, ""),
        }


def verify_bundle(
    source: Path | str | dict[str, Any],
    *,
    limits: BundleLimits | None = None,
) -> dict[str, Any]:
    """Verify a bundle offline. Never raises for a bad bundle: it reports FAIL."""
    limits = limits or DEFAULT_LIMITS
    findings: list[Finding] = []
    warnings: list[str] = []
    report: dict[str, Any] = {
        "schema": VERIFY_SCHEMA,
        "bundle_schema": None,
        "format_version": None,
        "status": "FAIL",
        "reason_codes": [],
        "findings": [],
        "warnings": warnings,
        "checked": {"artifacts": 0, "sections": 0, "bytes": 0},
        "bundle_sha256": None,
        "manifest_sha256": None,
        "container": None,
        "qualification": {},
        "network_calls": 0,
        "broker_order_api_calls": 0,
        "live_trading": False,
        "execution_authority": "NONE",
        "limits": limits.as_dict(),
        "verified_at": datetime.now(UTC).isoformat(),
    }
    try:
        container, index = load_bundle(source)
    except BundleError as exc:
        findings.append(Finding(exc.code, exc.detail))
        return _finalise_report(report, findings)
    report["container"] = container.kind()

    # ---- index shape -------------------------------------------------------
    if not isinstance(index, dict):
        findings.append(Finding("BUNDLE_MANIFEST_INVALID", "the index is not a JSON object"))
        return _finalise_report(report, findings)
    report["bundle_schema"] = index.get("schema")
    report["format_version"] = index.get("format_version")
    if index.get("schema") != BUNDLE_SCHEMA:
        findings.append(
            Finding("UNSUPPORTED_BUNDLE_SCHEMA", f"schema is {index.get('schema')!r}, expected {BUNDLE_SCHEMA!r}")
        )
    version = index.get("format_version")
    if version not in SUPPORTED_FORMAT_VERSIONS:
        findings.append(
            Finding(
                "UNSUPPORTED_BUNDLE_VERSION",
                f"format_version {version!r} is not in {sorted(SUPPORTED_FORMAT_VERSIONS)}",
            )
        )
    artifacts_index = index.get("artifacts")
    if not isinstance(artifacts_index, list) or not artifacts_index:
        findings.append(Finding("BUNDLE_MANIFEST_INVALID", "the artifact index is missing or empty"))
        return _finalise_report(report, findings)

    recorded_bundle_sha = str(index.get("bundle_sha256") or "")
    report["bundle_sha256"] = recorded_bundle_sha or None
    core = {
        key: value
        for key, value in index.items()
        if key not in {"bundle_sha256", "created_at", "generator", "inline"}
    }
    if sha256_json(core) != recorded_bundle_sha:
        findings.append(
            Finding("BUNDLE_DIGEST_MISMATCH", f"recomputed {sha256_json(core)[:16]}… != {recorded_bundle_sha[:16]}…")
        )

    # ---- artifact index integrity -----------------------------------------
    if len(artifacts_index) > limits.max_artifacts:
        findings.append(Finding("ARTIFACTS_TOO_MANY", f"{len(artifacts_index)} entries"))
    seen_paths: set[str] = set()
    seen_roles: set[str] = set()
    names_in_container = set(container.names())
    payloads: dict[str, bytes] = {}
    total = 0
    for raw in artifacts_index:
        if not isinstance(raw, dict):
            findings.append(Finding("BUNDLE_MANIFEST_INVALID", "an artifact entry is not an object"))
            continue
        role = str(raw.get("role") or "")
        path = str(raw.get("path") or "")
        digest = str(raw.get("sha256") or "")
        size = raw.get("size")
        if not role or not path:
            findings.append(Finding("BUNDLE_MANIFEST_INVALID", f"entry {raw!r} lacks a role or path"))
            continue
        if role in seen_roles:
            findings.append(Finding("ARTIFACT_DUPLICATE_ROLE", role, role=role))
        seen_roles.add(role)
        if path in seen_paths:
            findings.append(Finding("ARTIFACT_DUPLICATE_PATH", path, role=role, artifact=path))
        seen_paths.add(path)
        if not lookahead_safe_name(path):
            findings.append(
                Finding("ARTIFACT_PATH_UNSAFE", f"{path!r} is absolute, traversing or not normalised", role=role)
            )
            continue
        if not is_content_addressed(path, digest):
            if any(part == ".." for part in PurePosixPath(path).parts):
                findings.append(Finding("ARTIFACT_PATH_UNSAFE", path, role=role))
            else:
                findings.append(
                    Finding(
                        "ARTIFACT_PATH_NOT_CONTENT_ADDRESSED",
                        f"{path!r} does not encode its own SHA-256",
                        role=role,
                        artifact=path,
                    )
                )
            continue
        if not isinstance(size, int) or size < 0:
            findings.append(Finding("BUNDLE_MANIFEST_INVALID", f"{role} has no valid size", role=role))
            continue
        if size > limits.max_artifact_bytes:
            findings.append(
                Finding("ARTIFACT_OVERSIZED", f"{role} claims {size} bytes > {limits.max_artifact_bytes}", role=role)
            )
            continue
        total += size
        if path not in names_in_container:
            findings.append(
                Finding("ARTIFACT_NOT_IN_BUNDLE", f"{path} is enumerated but absent", role=role, artifact=path)
            )
            continue
        try:
            payload = container.read(path)
        except BundleError as exc:
            findings.append(Finding(exc.code, f"{path}: {exc.detail}", role=role, artifact=path))
            continue
        except (OSError, KeyError, zipfile.BadZipFile) as exc:
            findings.append(
                Finding("ARTIFACT_NOT_IN_BUNDLE", f"{path}: {type(exc).__name__}", role=role, artifact=path)
            )
            continue
        actual = sha256_bytes(payload)
        if actual != digest:
            findings.append(
                Finding(
                    "ARTIFACT_HASH_MISMATCH",
                    f"{role}: {actual[:16]}… != {digest[:16]}…",
                    role=role,
                    artifact=path,
                )
            )
            continue
        if len(payload) != size:
            findings.append(
                Finding("ARTIFACT_SIZE_MISMATCH", f"{role}: {len(payload)} != {size}", role=role, artifact=path)
            )
        payloads[role] = payload

    if total > limits.max_total_bytes:
        findings.append(Finding("BUNDLE_TOO_LARGE", f"{total} bytes > {limits.max_total_bytes}"))
    for name in sorted(names_in_container - seen_paths):
        findings.append(
            Finding("ARTIFACT_UNREFERENCED", f"{name} is in the container but not in the index", artifact=name)
        )
    for role in REQUIRED_ROLES:
        if role not in payloads:
            findings.append(
                Finding("ARTIFACT_ROLE_MISSING", f"required role {role!r} is unavailable", role=role)
            )
    report["checked"]["artifacts"] = len(payloads)
    report["checked"]["bytes"] = total

    # ---- secrets -----------------------------------------------------------
    for role, payload in payloads.items():
        hit = find_secret(payload.decode("utf-8", errors="replace"))
        if hit:
            findings.append(Finding("SECRET_DETECTED", f"{role} contains {hit}", role=role))

    # ---- semantic verification --------------------------------------------
    manifest = _json_payload(payloads, "run_manifest", findings)
    dataset_meta = _json_payload(payloads, "dataset_meta", findings)
    run_config = _json_payload(payloads, "run_config", findings)
    feature_schema = _json_payload(payloads, "feature_schema", findings)
    software = _json_payload(payloads, "software_fingerprint", findings)
    conformance = _json_payload(payloads, "conformance_evidence", findings)
    qual_inputs = _json_payload(payloads, "qualification_inputs", findings)
    record = _json_payload(payloads, "qualification_record", findings)

    bars: list[Candle] = []
    if "dataset_bars" in payloads:
        try:
            bars = rows_to_bars(json.loads(payloads["dataset_bars"]))
        except (ValueError, BundleError) as exc:
            findings.append(Finding("BARS_UNREADABLE", str(exc)))
    if "dependency_lock" in payloads and not payloads["dependency_lock"].strip():
        findings.append(Finding("DEPENDENCY_LOCK_MISSING", "the lock artifact is empty", role="dependency_lock"))

    if isinstance(manifest, dict):
        pinned = str(manifest.get("manifest_sha256") or "")
        report["manifest_sha256"] = pinned or None
        if manifest_digest(manifest) != pinned:
            findings.append(Finding("MANIFEST_TAMPERED", "the manifest does not match its own digest"))
        dataset = manifest.get("dataset") or {}
        pinned_dataset_sha = str(dataset.get("sha256") or "")
        if bars:
            actual = canonical_bars_sha256(bars)
            if actual != pinned_dataset_sha:
                findings.append(
                    Finding(
                        "DATASET_HASH_MISMATCH",
                        f"bars hash to {actual[:16]}… != manifest pin {pinned_dataset_sha[:16]}…",
                    )
                )
        if isinstance(dataset_meta, dict):
            meta_sha = str(dataset_meta.get("sha256") or "")
            if meta_sha and pinned_dataset_sha and meta_sha != pinned_dataset_sha:
                findings.append(
                    Finding(
                        "DATASET_META_MISMATCH",
                        f"dataset_meta pins {meta_sha[:16]}… != manifest {pinned_dataset_sha[:16]}…",
                    )
                )
            if str(dataset_meta.get("dataset_id") or "") != str(dataset.get("dataset_id") or ""):
                findings.append(Finding("DATASET_META_MISMATCH", "dataset_id differs from the manifest"))
            action_block = dataset.get("corporate_actions") or {}
            meta_actions = dataset_meta.get("corporate_actions") or {}
            if action_block and meta_actions:
                if str(action_block.get("actions_sha256") or "") != str(meta_actions.get("actions_sha256") or ""):
                    findings.append(
                        Finding("ACTION_HASH_MISMATCH", "the manifest and dataset metadata pin different action books")
                    )
        if "corporate_actions" in payloads:
            expected = str(dataset.get("actions_sha256") or "") or str(
                (dataset.get("corporate_actions") or {}).get("actions_sha256") or ""
            )
            if not expected:
                findings.append(
                    Finding("ACTION_HASH_MISMATCH", "a corporate-action sidecar is bundled but nothing pins it")
                )
            else:
                try:
                    from app.data.corporate_actions import CorporateActionsBook

                    book = CorporateActionsBook.from_payload(payloads["corporate_actions"])
                except (ValueError, TypeError) as exc:
                    findings.append(Finding("ACTION_HASH_MISMATCH", f"sidecar is unreadable ({type(exc).__name__})"))
                else:
                    if book.actions_sha256() != expected:
                        findings.append(
                            Finding(
                                "ACTION_HASH_MISMATCH",
                                f"sidecar hashes to {book.actions_sha256()[:16]}… != pin {expected[:16]}…",
                            )
                        )
        elif str(dataset.get("corporate_actions_file") or ""):
            warnings.append(
                "the manifest names a corporate-action sidecar that the bundle does not carry; "
                "the action book cannot be re-verified from this bundle"
            )

        index_run = index.get("run") if isinstance(index.get("run"), dict) else {}
        if index_run:
            if str(index_run.get("manifest_sha256") or "") != str(manifest.get("manifest_sha256") or ""):
                findings.append(
                    Finding("INDEX_RUN_MISMATCH", "the index run block pins a different manifest digest")
                )
            if str(index_run.get("dataset_id") or "") != str(dataset.get("dataset_id") or ""):
                findings.append(Finding("INDEX_RUN_MISMATCH", "the index run block pins a different dataset"))
        index_qual = index.get("qualification") if isinstance(index.get("qualification"), dict) else {}
        if index_qual and isinstance(record, dict):
            if str(index_qual.get("record_sha256") or "") != str(record.get("record_sha256") or ""):
                findings.append(
                    Finding("INDEX_RUN_MISMATCH", "the index qualification block pins a different record")
                )
        if isinstance(software, dict) and (manifest.get("software") or {}):
            if sha256_json(software) != sha256_json(manifest.get("software") or {}):
                findings.append(
                    Finding("SOFTWARE_FINGERPRINT_MISMATCH", "the fingerprint artifact differs from the manifest")
                )
        if isinstance(feature_schema, dict):
            names = feature_schema.get("feature_names")
            if names != list(FEATURE_NAMES):
                findings.append(
                    Finding("SECTION_DIGEST_MISMATCH", "the feature schema does not match this build", role="feature_schema")
                )
            elif feature_schema.get("manifest_feature_set_sha256") and str(
                feature_schema.get("manifest_feature_set_sha256")
            ) != str((manifest.get("software") or {}).get("feature_set_sha256") or ""):
                findings.append(
                    Finding(
                        "SECTION_DIGEST_MISMATCH",
                        "the manifest's feature-schema hash differs from the bundled schema",
                        role="feature_schema",
                    )
                )
        if isinstance(run_config, dict):
            costs = manifest.get("cost_assumptions") or {}
            for key in ("spread_bps", "slippage_bps", "stress_multiplier", "min_trades"):
                if key in run_config and key in costs and run_config[key] != costs[key]:
                    findings.append(
                        Finding("RUN_CONFIG_MISMATCH", f"{key}: config {run_config[key]!r} != manifest {costs[key]!r}")
                    )

        # §sections
        expected_sections = _sections(manifest, run_config if isinstance(run_config, dict) else {}, [
            {"role": "run_config", "sha256": _entry_sha(index, "run_config")},
            {"role": "feature_schema", "sha256": _entry_sha(index, "feature_schema")},
            {"role": "dependency_lock", "sha256": _entry_sha(index, "dependency_lock")},
        ])
        recorded_sections = index.get("sections") if isinstance(index.get("sections"), dict) else {}
        for name, expected in expected_sections.items():
            recorded = recorded_sections.get(name)
            if not isinstance(recorded, dict):
                findings.append(Finding("SECTION_DIGEST_MISMATCH", f"section {name!r} is not pinned"))
                continue
            if str(recorded.get("sha256") or "") != str(expected.get("sha256") or ""):
                findings.append(
                    Finding(
                        "SECTION_DIGEST_MISMATCH",
                        f"{name}: {str(recorded.get('sha256'))[:16]}… != {str(expected.get('sha256'))[:16]}…",
                    )
                )
        report["checked"]["sections"] = len(expected_sections)

    # ---- qualification -----------------------------------------------------
    report["qualification"] = _verify_qualification(
        manifest, record, qual_inputs, conformance, dataset_meta, bars, findings
    )

    return _finalise_report(report, findings)


def _entry_sha(index: dict[str, Any], role: str) -> str:
    for entry in index.get("artifacts") or []:
        if isinstance(entry, dict) and entry.get("role") == role:
            return str(entry.get("sha256") or "")
    return ""


def _json_payload(
    payloads: dict[str, bytes], role: str, findings: list[Finding]
) -> dict[str, Any] | None:
    payload = payloads.get(role)
    if payload is None:
        return None
    try:
        parsed = json.loads(payload)
    except ValueError:
        findings.append(Finding("BUNDLE_MANIFEST_INVALID", f"{role} is not valid JSON", role=role))
        return None
    if not isinstance(parsed, dict):
        findings.append(Finding("BUNDLE_MANIFEST_INVALID", f"{role} is not a JSON object", role=role))
        return None
    return parsed


def record_content_digest(record: dict[str, Any]) -> str:
    """The digest a qualification record carries over its own body.

    ``evaluated_at`` and ``run_completed_at`` are excluded because
    :func:`app.model_lab.qualification.evaluate_qualification` excludes them too.
    """
    core = {
        key: value
        for key, value in record.items()
        if key not in {"record_sha256", "evaluated_at", "run_completed_at"}
    }
    return sha256_json(core)


def _verify_qualification(
    manifest: dict[str, Any] | None,
    record: dict[str, Any] | None,
    qual_inputs: dict[str, Any] | None,
    conformance: dict[str, Any] | None,
    dataset_meta: dict[str, Any] | None,
    bars: list[Candle],
    findings: list[Finding],
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "bundled_status": None,
        "record_sha256": None,
        "rederived_status": None,
        "rederived_record_sha256": None,
        "matches": None,
        "execution_authority": "NONE",
    }
    if not isinstance(record, dict):
        findings.append(Finding("QUALIFICATION_RECORD_MISSING", "no qualification record in the bundle"))
        return summary
    summary["bundled_status"] = record.get("status")
    summary["record_sha256"] = record.get("record_sha256")
    self_digest = record_content_digest(record)
    summary["record_content_digest"] = self_digest
    if self_digest != str(record.get("record_sha256") or ""):
        findings.append(
            Finding(
                "QUALIFICATION_ALTERED",
                "the record body does not hash to the record_sha256 it carries "
                f"({self_digest[:16]}… != {str(record.get('record_sha256'))[:16]}…)",
            )
        )
    try:
        assert_no_execution_authority(record)
    except Exception as exc:  # QualificationBlocked
        findings.append(Finding("QUALIFICATION_SCOPE_VIOLATION", str(exc)))
    if not isinstance(qual_inputs, dict):
        findings.append(Finding("QUALIFICATION_INPUTS_MISSING", "qualification inputs are not pinned"))
        return summary
    if not isinstance(conformance, dict) or not conformance:
        findings.append(
            Finding("CONFORMANCE_EVIDENCE_MISSING", "provider-conformance evidence is not pinned")
        )
    if not isinstance(manifest, dict):
        return summary
    try:
        rederived = evaluate_qualification(
            manifest,
            dataset_meta=dataset_meta,
            recomputed_dataset_sha256=str(qual_inputs.get("recomputed_dataset_sha256") or "")
            or (canonical_bars_sha256(bars) if bars else ""),
            conformance=conformance or None,
            provenance=qual_inputs.get("provenance") or None,
            expected_software_commit=str(qual_inputs.get("expected_software_commit") or ""),
        )
    except (ValueError, KeyError, TypeError) as exc:
        findings.append(
            Finding("QUALIFICATION_ALTERED", f"the record cannot be re-derived ({type(exc).__name__})")
        )
        return summary
    summary["rederived_status"] = rederived.get("status")
    summary["rederived_record_sha256"] = rederived.get("record_sha256")
    rederived_digest = record_content_digest(rederived)
    summary["rederived_content_digest"] = rederived_digest
    status_matches = rederived.get("status") == record.get("status")
    summary["matches"] = bool(status_matches and rederived_digest == self_digest)
    if not summary["matches"]:
        findings.append(
            Finding(
                "QUALIFICATION_ALTERED",
                f"the bundled verdict ({record.get('status')}, {self_digest[:16]}…) does not re-derive "
                f"({rederived.get('status')}, {rederived_digest[:16]}…)",
            )
        )
    return summary


def _finalise_report(report: dict[str, Any], findings: list[Finding]) -> dict[str, Any]:
    report["findings"] = [item.as_dict() for item in findings]
    codes: list[str] = []
    for item in findings:
        if item.code not in codes:
            codes.append(item.code)
    report["reason_codes"] = codes
    report["status"] = "PASS" if not findings else "FAIL"
    report["execution_authority"] = "NONE"
    return report


def render_verify_text(report: dict[str, Any]) -> str:
    lines = [
        f"AXIOM research bundle verification  status={report['status']}  "
        f"container={report.get('container') or 'unknown'}  artifacts={report['checked']['artifacts']}",
        f"bundle sha256:   {report.get('bundle_sha256')}",
        f"manifest sha256: {report.get('manifest_sha256')}",
    ]
    qual = report.get("qualification") or {}
    if qual.get("bundled_status"):
        lines.append(
            f"qualification:   {qual.get('bundled_status')} (record {str(qual.get('record_sha256'))[:16]}…, "
            f"re-derived match={qual.get('matches')})"
        )
    lines.append("")
    if report["findings"]:
        for item in report["findings"]:
            lines.append(f"  [FAIL] {item['code']:<34} {item['detail']}")
    else:
        lines.append("  all artifacts, sections and the qualification record verified")
    for warning in report.get("warnings") or []:
        lines.append(f"  [warn] {warning}")
    lines.append("")
    lines.append("  execution authority: NONE — a bundle never authorizes or arms trading")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Export from the local Model Lab store
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDENCY_LOCK_CANDIDATES = ("requirements-lock.txt", "requirements.txt")


def read_dependency_lock(root: Path | str | None = None) -> tuple[str, bytes]:
    """Return ``(filename, bytes)`` for the dependency lock, never guessing content."""
    base = Path(root) if root is not None else REPO_ROOT
    for name in DEPENDENCY_LOCK_CANDIDATES:
        path = base / name
        if path.is_file():
            return name, path.read_bytes()
    raise BundleError(
        "DEPENDENCY_LOCK_MISSING",
        f"no dependency lock found in {base} (looked for {', '.join(DEPENDENCY_LOCK_CANDIDATES)})",
    )


def _gather_run_inputs(service: Any, run_id: str) -> RunInputs:
    run = service.get_run(run_id)
    manifest = run.get("manifest")
    if not isinstance(manifest, dict) or not manifest:
        raise BundleError(
            "BUNDLE_MANIFEST_MISSING",
            f"run {run_id} has no manifest yet (status={run.get('status')})",
        )
    try:
        record = service.get_qualification(run_id)
    except KeyError as exc:
        raise BundleError("QUALIFICATION_RECORD_MISSING", f"run {run_id} has no qualification record") from exc
    dataset_id = str(run.get("dataset_id") or "")
    meta, bars = service.datasets.load_dataset(dataset_id)
    actions_bytes: bytes | None = None
    filename = str(meta.get("corporate_actions_file") or "")
    if filename:
        sidecar = Path(service.datasets.storage_dir) / filename
        if not sidecar.is_file():
            raise BundleError("ACTION_HASH_MISMATCH", f"corporate-action sidecar {filename} is missing")
        actions_bytes = sidecar.read_bytes()
    _, lock_bytes = read_dependency_lock()
    return RunInputs(
        run_id=run_id,
        manifest=manifest,
        qualification_record=record,
        dataset_meta=meta,
        bars=bars,
        run_config=run.get("config") or {},
        dependency_lock=lock_bytes,
        corporate_actions_bytes=actions_bytes,
        conformance_report=service.conformance_report(),
    )


def export_run_bundle(
    service: Any,
    run_id: str,
    *,
    limits: BundleLimits | None = None,
    created_at: datetime | str | None = None,
    generator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a sealed bundle document for one completed run in the local store."""
    from app.model_lab.provenance import software_fingerprint

    inputs = _gather_run_inputs(service, run_id)
    fingerprint = software_fingerprint()
    gen = generator or {
        "generator": "axiom.model_lab.bundle",
        "package_version": fingerprint.get("package_version"),
        "commit": fingerprint.get("commit"),
        "tree_dirty": fingerprint.get("tree_dirty"),
        "python": fingerprint.get("python"),
        "platform": fingerprint.get("platform"),
    }
    return build_bundle_document(inputs, limits=limits, created_at=created_at, generator=gen)


def export_run_bundle_to(
    service: Any,
    run_id: str,
    destination: Path | str,
    *,
    archive: bool = False,
    limits: BundleLimits | None = None,
    created_at: datetime | str | None = None,
) -> dict[str, Any]:
    document = export_run_bundle(service, run_id, limits=limits, created_at=created_at)
    written = write_bundle(document, destination, archive=archive)
    return {"path": str(written), "archive": archive, "bundle": document}


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #
def _collect_payloads(container: BundleContainer, index: dict[str, Any]) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    for entry in index.get("artifacts") or []:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "")
        path = str(entry.get("path") or "")
        if not role or not path:
            continue
        try:
            payloads[role] = container.read(path)
        except (BundleError, OSError, KeyError, zipfile.BadZipFile):
            continue
    return payloads


def _compare_values(
    pinned: Any,
    reproduced: Any,
    path: str,
    *,
    abs_tol: float,
    rel_tol: float,
    out: list[dict[str, Any]],
    limit: int = 200,
) -> None:
    if len(out) >= limit:
        return
    if isinstance(pinned, dict) and isinstance(reproduced, dict):
        for key in sorted(set(pinned) | set(reproduced)):
            if key not in pinned or key not in reproduced:
                out.append(
                    {
                        "path": f"{path}.{key}",
                        "pinned": pinned.get(key, "<missing>"),
                        "reproduced": reproduced.get(key, "<missing>"),
                        "delta": None,
                        "within_tolerance": False,
                    }
                )
                continue
            _compare_values(
                pinned[key],
                reproduced[key],
                f"{path}.{key}",
                abs_tol=abs_tol,
                rel_tol=rel_tol,
                out=out,
                limit=limit,
            )
        return
    if isinstance(pinned, list) and isinstance(reproduced, list):
        if len(pinned) != len(reproduced):
            out.append(
                {
                    "path": f"{path}.length",
                    "pinned": len(pinned),
                    "reproduced": len(reproduced),
                    "delta": None,
                    "within_tolerance": False,
                }
            )
            return
        for idx, (left, right) in enumerate(zip(pinned, reproduced)):
            _compare_values(
                left,
                right,
                f"{path}[{idx}]",
                abs_tol=abs_tol,
                rel_tol=rel_tol,
                out=out,
                limit=limit,
            )
        return
    if isinstance(pinned, (int, float)) and isinstance(reproduced, (int, float)) and not isinstance(pinned, bool):
        delta = float(reproduced) - float(pinned)
        tolerance = max(abs_tol, rel_tol * max(abs(float(pinned)), abs(float(reproduced))))
        if abs(delta) > tolerance:
            out.append(
                {
                    "path": path,
                    "pinned": pinned,
                    "reproduced": reproduced,
                    "delta": delta,
                    "within_tolerance": False,
                }
            )
        return
    if pinned != reproduced:
        out.append(
            {
                "path": path,
                "pinned": pinned,
                "reproduced": reproduced,
                "delta": None,
                "within_tolerance": False,
            }
        )


_ENVIRONMENT_FIELDS = ("commit", "tree_dirty", "python", "platform", "package_version", "commit_source", "frozen")


def environment_drift(pinned: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    drift: list[dict[str, Any]] = []
    for fieldname in _ENVIRONMENT_FIELDS:
        if fieldname not in pinned and fieldname not in current:
            continue
        if pinned.get(fieldname) != current.get(fieldname):
            drift.append(
                {"field": fieldname, "pinned": pinned.get(fieldname), "current": current.get(fieldname)}
            )
    return drift


def _worst_status(left: str | None, right: str | None) -> str:
    ranks = {name: rank for name, rank in QUALIFICATION_RANK.items()}
    candidates = [name for name in (left, right) if name]
    if not candidates:
        return STATUS_UNVERIFIED
    return max(candidates, key=lambda name: ranks.get(str(name), 3))


def replay_bundle(
    source: Path | str | dict[str, Any],
    *,
    abs_tol: float = DEFAULT_ABS_TOL,
    rel_tol: float = DEFAULT_REL_TOL,
    limits: BundleLimits | None = None,
    strict_environment: bool = False,
) -> dict[str, Any]:
    """Re-run the pinned research pipeline from bundle inputs only.

    The replay never fetches data, never asks a provider, never submits an order
    and never upgrades a qualification: the reported effective status is the worse
    of the bundled and the re-derived verdict.
    """
    from app.model_lab.provenance import software_fingerprint

    report: dict[str, Any] = {
        "schema": REPLAY_SCHEMA,
        "status": "UNVERIFIED",
        "reason_codes": [],
        "findings": [],
        "verification": {},
        "pinned": {},
        "reproduced": {},
        "comparisons": [],
        "environment": {"pinned": {}, "current": {}, "drift": []},
        "qualification": {},
        "tolerance": {"abs": abs_tol, "rel": rel_tol},
        "network_calls": 0,
        "broker_order_api_calls": 0,
        "live_trading": False,
        "execution_authority": "NONE",
        "replayed_at": datetime.now(UTC).isoformat(),
    }
    verification = verify_bundle(source, limits=limits)
    report["verification"] = {
        "status": verification["status"],
        "reason_codes": verification["reason_codes"],
        "findings": verification["findings"],
        "bundle_sha256": verification["bundle_sha256"],
        "manifest_sha256": verification["manifest_sha256"],
        "qualification": verification.get("qualification"),
    }
    if verification["status"] != "PASS":
        report["reason_codes"] = ["REPLAY_NOT_REPRODUCIBLE"] + list(verification["reason_codes"])
        report["findings"] = [
            {"code": "REPLAY_NOT_REPRODUCIBLE", "detail": "the bundle does not verify; replay refuses to guess"}
        ]
        report["status"] = "UNVERIFIED"
        return report

    container, index = load_bundle(source)
    payloads = _collect_payloads(container, index)
    manifest = json.loads(payloads["run_manifest"])
    run_config = json.loads(payloads["run_config"])
    dataset_meta = json.loads(payloads["dataset_meta"])
    conformance = json.loads(payloads["conformance_evidence"])
    qual_inputs = json.loads(payloads["qualification_inputs"])
    record = json.loads(payloads["qualification_record"])
    bars = rows_to_bars(json.loads(payloads["dataset_bars"]))
    pinned_software = manifest.get("software") or {}

    report["pinned"] = {
        "manifest_sha256": manifest.get("manifest_sha256"),
        "dataset_sha256": (manifest.get("dataset") or {}).get("sha256"),
        "bars": len(bars),
        "run_id": index.get("run", {}).get("run_id"),
        "qualification_status": record.get("status"),
        "qualification_record_sha256": record.get("record_sha256"),
    }

    kronos_candidate = next(
        (c for c in (manifest.get("candidates") or []) if c.get("candidate_id") == "kronos_ensemble"),
        None,
    )
    if isinstance(kronos_candidate, dict) and kronos_candidate.get("status") == "EVALUATED_GENUINE_KRONOS":
        report["status"] = "UNVERIFIED"
        report["reason_codes"] = ["REPLAY_REQUIRES_KRONOS"]
        report["findings"] = [
            {
                "code": "REPLAY_REQUIRES_KRONOS",
                "detail": (
                    "the pinned run evaluated the genuine Kronos ablation; Kronos weights/runtime are "
                    "not part of a research bundle, so replay stays UNVERIFIED instead of substituting "
                    "a mock forecast"
                ),
            }
        ]
        report["qualification"] = {
            "bundled_status": record.get("status"),
            "rederived_status": None,
            "effective_status": record.get("status"),
            "upgraded": False,
        }
        return report

    dataset_sha = canonical_bars_sha256(bars)
    reproduced = execute_research_pipeline(
        dataset_meta=dataset_meta,
        bars=bars,
        market=str(manifest.get("market") or "NSE"),
        timeframe=str(manifest.get("timeframe") or "1h"),
        train_frac=float(run_config.get("train_frac", 0.60)),
        val_frac=float(run_config.get("val_frac", 0.20)),
        embargo_bars=int(run_config.get("embargo_bars", 5)),
        spread_bps=float(run_config.get("spread_bps", 4.0)),
        slippage_bps=float(run_config.get("slippage_bps", 5.0)),
        stress_multiplier=float(run_config.get("stress_multiplier", 2.0)),
        min_trades=int(run_config.get("min_trades", 5)),
        include_kronos=False,
        kronos_provider=None,
        software=dict(pinned_software),
        universe=[str(symbol) for symbol in ((manifest.get("universe") or {}).get("symbols") or [])] or None,
        benchmark_candidate=str((manifest.get("benchmark") or {}).get("candidate_id") or "buy_and_hold"),
    )
    reproduced_sha = manifest_digest(reproduced)
    report["reproduced"] = {
        "manifest_sha256": reproduced_sha,
        "dataset_sha256": dataset_sha,
        "candidates": [c.get("candidate_id") for c in reproduced.get("candidates") or []],
        "selected_variants": (reproduced.get("search_family") or {}).get("selected_variants"),
    }

    findings: list[dict[str, Any]] = []
    pinned_sha = str(manifest.get("manifest_sha256") or "")
    if reproduced_sha != pinned_sha:
        findings.append(
            {
                "code": "REPLAY_MANIFEST_MISMATCH",
                "detail": f"replayed {reproduced_sha[:16]}… != pinned {pinned_sha[:16]}…",
            }
        )
    drift: list[dict[str, Any]] = []
    _compare_values(
        manifest.get("candidates") or [],
        reproduced.get("candidates") or [],
        "candidates",
        abs_tol=abs_tol,
        rel_tol=rel_tol,
        out=drift,
    )
    _compare_values(
        manifest.get("search_family") or {},
        reproduced.get("search_family") or {},
        "search_family",
        abs_tol=abs_tol,
        rel_tol=rel_tol,
        out=drift,
    )
    _compare_values(
        manifest.get("splits") or {},
        reproduced.get("splits") or {},
        "splits",
        abs_tol=abs_tol,
        rel_tol=rel_tol,
        out=drift,
    )
    report["comparisons"] = drift
    if drift:
        findings.append(
            {
                "code": "REPLAY_METRIC_DRIFT",
                "detail": f"{len(drift)} value(s) differ beyond tolerance "
                f"(abs={abs_tol}, rel={rel_tol}); first: {drift[0]['path']}",
            }
        )

    # Re-derive the qualification from the *reproduced* manifest, with the same
    # pinned inputs. The reported status is always the worse of the two.
    try:
        rederived = evaluate_qualification(
            reproduced,
            dataset_meta=dataset_meta,
            recomputed_dataset_sha256=dataset_sha,
            conformance=conformance or None,
            provenance=qual_inputs.get("provenance") or None,
            expected_software_commit=str(qual_inputs.get("expected_software_commit") or ""),
        )
    except (ValueError, KeyError, TypeError) as exc:
        rederived = {"status": None, "record_sha256": None, "error": type(exc).__name__}
        findings.append(
            {"code": "REPLAY_METRIC_DRIFT", "detail": f"qualification could not be re-derived ({type(exc).__name__})"}
        )
    effective = _worst_status(record.get("status"), rederived.get("status"))
    report["qualification"] = {
        "bundled_status": record.get("status"),
        "bundled_record_sha256": record.get("record_sha256"),
        "rederived_status": rederived.get("status"),
        "rederived_record_sha256": rederived.get("record_sha256"),
        "effective_status": effective,
        "upgraded": QUALIFICATION_RANK.get(str(effective), 3) < QUALIFICATION_RANK.get(
            str(record.get("status")), 3
        ),
        "authority": "NONE",
    }

    current = software_fingerprint()
    env_drift = environment_drift(pinned_software, current)
    report["environment"] = {
        "pinned": {name: pinned_software.get(name) for name in _ENVIRONMENT_FIELDS},
        "current": {name: current.get(name) for name in _ENVIRONMENT_FIELDS},
        "drift": env_drift,
    }

    hard_failures = [item for item in findings if item["code"] in {"REPLAY_MANIFEST_MISMATCH", "REPLAY_METRIC_DRIFT"}]
    if hard_failures:
        report["status"] = "DIVERGED"
    elif env_drift or strict_environment:
        report["status"] = "DIVERGED" if (strict_environment and env_drift) else "REPRODUCED_WITH_ENVIRONMENT_DRIFT"
    else:
        report["status"] = "REPRODUCED"
    for item in env_drift:
        findings.append(
            {
                "code": "ENVIRONMENT_DRIFT",
                "detail": f"{item['field']}: pinned {item['pinned']!r} != current {item['current']!r}",
            }
        )
    codes: list[str] = []
    for item in findings:
        if item["code"] not in codes:
            codes.append(item["code"])
    report["reason_codes"] = codes
    report["findings"] = findings
    return report


def render_replay_text(report: dict[str, Any]) -> str:
    qual = report.get("qualification") or {}
    lines = [
        f"AXIOM research bundle replay  status={report['status']}  "
        f"bundle verification={report['verification'].get('status')}",
        f"pinned manifest:    {report['pinned'].get('manifest_sha256')}",
        f"reproduced manifest:{report['reproduced'].get('manifest_sha256')}",
        f"qualification:      bundled={qual.get('bundled_status')} re-derived={qual.get('rederived_status')} "
        f"effective={qual.get('effective_status')} upgraded={qual.get('upgraded')}",
        f"tolerance:          abs={report['tolerance']['abs']} rel={report['tolerance']['rel']}",
    ]
    drift = report.get("environment", {}).get("drift") or []
    if drift:
        lines.append("environment drift:")
        for item in drift:
            lines.append(f"  [drift] {item['field']}: pinned={item['pinned']!r} current={item['current']!r}")
    lines.append("")
    if report["findings"]:
        for item in report["findings"]:
            lines.append(f"  [{('FAIL' if item['code'] != 'ENVIRONMENT_DRIFT' else 'warn')}] "
                         f"{item['code']:<28} {item['detail']}")
    else:
        lines.append("  the pinned inputs reproduced the manifest and the qualification record exactly")
    lines.append("")
    lines.append("  execution authority: NONE — a replay never arms, sizes or authorizes trading")
    return "\n".join(lines)
