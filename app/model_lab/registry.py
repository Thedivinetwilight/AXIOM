"""Model Lab Service, Background Research Runner & SHA-256 Model Registry.

Strictly isolated from trading execution:
- Reads historical datasets and writes research manifests / model artifacts only.
- Startup recovery marks interrupted runs ``INTERRUPTED_RECOVERED`` and NEVER
  auto-starts jobs or alters ``LIVE-MARKET PAPER`` / ``LIVE-MARKET LIVE`` state.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.model_lab.datasets import DatasetManager
from app.model_lab.kronos_provider import KronosForecastProvider
from app.model_lab.research_pipeline import (
    FEATURE_NAMES,
    ResearchCancelledError,
    execute_research_pipeline,
)

MODEL_BUNDLE_SCHEMA = "axiom.model_lab.bundle.v1"


def _canonical_bundle_sha256(core: dict[str, Any]) -> str:
    serialized = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class ModelLabService:
    """Unified coordinator for Kronos Provider, Dataset Manager, Research Runs, and Model Registry."""

    broker_order_endpoints_used = False

    def __init__(
        self,
        storage_dir: Path | str,
        *,
        kronos_provider: KronosForecastProvider | None = None,
        repo_data_dir: Path | None = None,
    ) -> None:
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.storage_dir / "model_lab.sqlite3"
        self.kronos_provider = kronos_provider
        self.datasets = DatasetManager(self.storage_dir, repo_data_dir=repo_data_dir)
        self._lock = threading.RLock()
        self._cancel_flags: set[str] = set()
        self._active_threads: dict[str, threading.Thread] = {}
        self._init_db()
        self._recover_interrupted_runs()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_lab_runs (
                    run_id TEXT PRIMARY KEY,
                    market TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    progress_pct INTEGER NOT NULL DEFAULT 0,
                    status_detail TEXT NOT NULL DEFAULT '',
                    config_json TEXT NOT NULL,
                    manifest_json TEXT,
                    manifest_sha256 TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS model_lab_models (
                    model_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    candidate_id TEXT NOT NULL,
                    family TEXT NOT NULL,
                    market TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    dataset_sha256 TEXT NOT NULL,
                    uses_kronos INTEGER NOT NULL DEFAULT 0,
                    promotion_eligible INTEGER NOT NULL DEFAULT 0,
                    registry_status TEXT NOT NULL,
                    rejection_reasons_json TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    metrics_summary_json TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    operator_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def _recover_interrupted_runs(self) -> None:
        now_iso = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE model_lab_runs
                SET status = 'INTERRUPTED_RECOVERED',
                    phase = 'RECOVERED',
                    status_detail = 'Process restarted while research job was active; recovered safely without auto-restarting.',
                    updated_at = ?
                WHERE status IN ('QUEUED', 'RUNNING')
                """,
                (now_iso,),
            )

    def start_research_run(
        self,
        *,
        dataset_id: str,
        market: str | None = None,
        timeframe: str | None = None,
        train_frac: float = 0.60,
        val_frac: float = 0.20,
        embargo_bars: int = 5,
        spread_bps: float = 4.0,
        slippage_bps: float = 5.0,
        stress_multiplier: float = 2.0,
        min_trades: int = 5,
        include_kronos: bool = False,
        async_execution: bool = True,
    ) -> dict[str, Any]:
        meta, bars = self.datasets.load_dataset(dataset_id)
        mkt = (market or str(meta.get("market") or "NSE")).strip().upper()
        tf = (timeframe or str(meta.get("timeframe") or "1h")).strip()
        if mkt not in {"NSE", "NASDAQ"}:
            raise ValueError(f"Unsupported market {mkt!r}")

        run_id = f"mlrun-{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(UTC).isoformat()
        cfg = {
            "dataset_id": dataset_id,
            "market": mkt,
            "timeframe": tf,
            "train_frac": float(train_frac),
            "val_frac": float(val_frac),
            "embargo_bars": int(embargo_bars),
            "spread_bps": float(spread_bps),
            "slippage_bps": float(slippage_bps),
            "stress_multiplier": float(stress_multiplier),
            "min_trades": int(min_trades),
            "include_kronos": bool(include_kronos),
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO model_lab_runs
                (run_id, market, dataset_id, timeframe, status, phase, progress_pct, status_detail, config_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'RUNNING', 'STARTING', 1, 'Initializing chronological research pipeline', ?, ?, ?)
                """,
                (run_id, mkt, dataset_id, tf, json.dumps(cfg, sort_keys=True), now_iso, now_iso),
            )

        def _worker() -> None:
            def _progress(phase: str, pct: int, detail: str) -> None:
                ts = datetime.now(UTC).isoformat()
                with self._lock, self._connect() as c:
                    c.execute(
                        "UPDATE model_lab_runs SET phase = ?, progress_pct = ?, status_detail = ?, updated_at = ? WHERE run_id = ?",
                        (phase, int(pct), detail, ts, run_id),
                    )

            def _is_cancelled() -> bool:
                with self._lock:
                    return run_id in self._cancel_flags

            try:
                manifest = execute_research_pipeline(
                    dataset_meta=meta,
                    bars=bars,
                    market=mkt,
                    timeframe=tf,
                    train_frac=float(train_frac),
                    val_frac=float(val_frac),
                    embargo_bars=int(embargo_bars),
                    spread_bps=float(spread_bps),
                    slippage_bps=float(slippage_bps),
                    stress_multiplier=float(stress_multiplier),
                    min_trades=int(min_trades),
                    include_kronos=bool(include_kronos),
                    kronos_provider=self.kronos_provider,
                    progress_cb=_progress,
                    cancel_check=_is_cancelled,
                )
                done_ts = datetime.now(UTC).isoformat()
                with self._lock, self._connect() as c:
                    c.execute(
                        """
                        UPDATE model_lab_runs
                        SET status = 'COMPLETED',
                            phase = 'COMPLETED',
                            progress_pct = 100,
                            status_detail = 'Completed walk-forward, untouched test, and cost-stress evaluation',
                            manifest_json = ?,
                            manifest_sha256 = ?,
                            updated_at = ?,
                            completed_at = ?
                        WHERE run_id = ?
                        """,
                        (
                            json.dumps(manifest, sort_keys=True),
                            manifest["manifest_sha256"],
                            done_ts,
                            done_ts,
                            run_id,
                        ),
                    )
                    self._register_run_candidates_conn(c, run_id, manifest, done_ts)
            except ResearchCancelledError as exc:
                ts = datetime.now(UTC).isoformat()
                with self._lock, self._connect() as c:
                    c.execute(
                        "UPDATE model_lab_runs SET status = 'CANCELLED', phase = 'CANCELLED', status_detail = ?, updated_at = ?, completed_at = ? WHERE run_id = ?",
                        (str(exc), ts, ts, run_id),
                    )
            except (ValueError, RuntimeError, OSError, sqlite3.Error, ArithmeticError, LookupError) as exc:
                ts = datetime.now(UTC).isoformat()
                with self._lock, self._connect() as c:
                    c.execute(
                        "UPDATE model_lab_runs SET status = 'FAILED', phase = 'ERROR', status_detail = ?, updated_at = ?, completed_at = ? WHERE run_id = ?",
                        (f"{type(exc).__name__}: {str(exc)[:220]}", ts, ts, run_id),
                    )
            finally:
                with self._lock:
                    self._cancel_flags.discard(run_id)
                    self._active_threads.pop(run_id, None)

        if async_execution:
            thread = threading.Thread(target=_worker, name=f"model-lab-{run_id}", daemon=True)
            with self._lock:
                self._active_threads[run_id] = thread
            thread.start()
        else:
            _worker()

        return self.get_run(run_id)

    def _register_run_candidates_conn(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        manifest: dict[str, Any],
        now_iso: str,
    ) -> None:
        ds = manifest.get("dataset") or {}
        for cand in manifest.get("candidates") or []:
            if cand.get("family") == "BASELINE":
                continue
            if "untouched_test" not in cand:
                continue
            model_id = f"mdl-{run_id[-8:]}-{cand['candidate_id']}"
            eligible = bool(cand.get("promotion_eligible", False))
            reg_status = "VALIDATED_CANDIDATE" if eligible else "GATE_REJECTED"
            metrics_summary = {
                "train": cand.get("train"),
                "validation": cand.get("validation"),
                "untouched_test": cand.get("untouched_test"),
                "stressed_test": cand.get("stressed_test"),
            }
            core = {
                "candidate_id": cand["candidate_id"],
                "family": cand["family"],
                "market": manifest["market"],
                "timeframe": manifest["timeframe"],
                "dataset_sha256": str(ds.get("sha256") or ""),
                "parameters": cand.get("parameters") or {},
                "metrics_summary": metrics_summary,
            }
            art_sha = _canonical_bundle_sha256(core)
            conn.execute(
                """
                INSERT OR REPLACE INTO model_lab_models
                (model_id, run_id, candidate_id, family, market, timeframe, dataset_id, dataset_sha256,
                 uses_kronos, promotion_eligible, registry_status, rejection_reasons_json, parameters_json,
                 metrics_summary_json, artifact_sha256, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model_id,
                    run_id,
                    cand["candidate_id"],
                    cand["family"],
                    manifest["market"],
                    manifest["timeframe"],
                    str(ds.get("dataset_id") or ""),
                    str(ds.get("sha256") or ""),
                    1 if cand.get("uses_kronos") else 0,
                    1 if eligible else 0,
                    reg_status,
                    json.dumps(cand.get("rejection_reasons") or []),
                    json.dumps(cand.get("parameters") or {}, sort_keys=True),
                    json.dumps(metrics_summary, sort_keys=True),
                    art_sha,
                    now_iso,
                    now_iso,
                ),
            )

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            self._cancel_flags.add(run_id)
        row = self.get_run(run_id)
        if row["status"] in {"QUEUED", "RUNNING"}:
            ts = datetime.now(UTC).isoformat()
            with self._lock, self._connect() as conn:
                conn.execute(
                    "UPDATE model_lab_runs SET status = 'CANCELLED', phase = 'CANCELLED', status_detail = 'Cancelled by operator', updated_at = ?, completed_at = ? WHERE run_id = ?",
                    (ts, ts, run_id),
                )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM model_lab_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown research run {run_id!r}")
        return self._format_run_row(dict(row), include_manifest=True)

    def list_runs(self, *, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM model_lab_runs ORDER BY created_at DESC LIMIT ?",
                    (int(limit),),
                )
            ]
        return [self._format_run_row(r, include_manifest=True) for r in rows]

    def _format_run_row(self, row: dict[str, Any], *, include_manifest: bool) -> dict[str, Any]:
        manifest = json.loads(row["manifest_json"]) if (include_manifest and row.get("manifest_json")) else None
        return {
            "run_id": row["run_id"],
            "market": row["market"],
            "dataset_id": row["dataset_id"],
            "timeframe": row["timeframe"],
            "status": row["status"],
            "phase": row["phase"],
            "progress_pct": row["progress_pct"],
            "status_detail": row["status_detail"],
            "config": json.loads(row["config_json"]),
            "manifest_sha256": row.get("manifest_sha256"),
            "manifest": manifest,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row.get("completed_at"),
        }

    def list_models(self, *, market: str = "") -> list[dict[str, Any]]:
        mkt = market.strip().upper()
        with self._lock, self._connect() as conn:
            if mkt:
                rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM model_lab_models WHERE market = ? ORDER BY created_at DESC",
                        (mkt,),
                    )
                ]
            else:
                rows = [
                    dict(r)
                    for r in conn.execute("SELECT * FROM model_lab_models ORDER BY created_at DESC")
                ]
        return [self._format_model_row(r) for r in rows]

    def get_model(self, model_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM model_lab_models WHERE model_id = ?", (model_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown model_id {model_id!r}")
        return self._format_model_row(dict(row))

    def _format_model_row(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "model_id": r["model_id"],
            "run_id": r.get("run_id"),
            "candidate_id": r["candidate_id"],
            "family": r["family"],
            "market": r["market"],
            "timeframe": r["timeframe"],
            "dataset_id": r["dataset_id"],
            "dataset_sha256": r["dataset_sha256"],
            "uses_kronos": bool(r["uses_kronos"]),
            "promotion_eligible": bool(r["promotion_eligible"]),
            "registry_status": r["registry_status"],
            "rejection_reasons": json.loads(r["rejection_reasons_json"]),
            "parameters": json.loads(r["parameters_json"]),
            "metrics_summary": json.loads(r["metrics_summary_json"]),
            "artifact_sha256": r["artifact_sha256"],
            "operator_note": r.get("operator_note") or "",
            "live_execution_authorized": False,
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }

    def promote_model(self, model_id: str, *, note: str = "") -> dict[str, Any]:
        """Manually promote an eligible research model to ``PROMOTED_PAPER_CANDIDATE`` (never starts trading)."""
        model = self.get_model(model_id)
        if not model["promotion_eligible"]:
            reasons = "; ".join(model["rejection_reasons"]) or "Did not pass validation & 2x cost-stress gates"
            raise ValueError(f"Cannot promote {model_id}: {reasons}")
        ts = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE model_lab_models
                SET registry_status = 'PROMOTED_PAPER_CANDIDATE',
                    operator_note = ?,
                    updated_at = ?
                WHERE model_id = ?
                """,
                (note or "Manually promoted for paper-only evaluation; zero live execution effect.", ts, model_id),
            )
        return self.get_model(model_id)

    def reject_model(self, model_id: str, *, reason: str = "Rejected by operator") -> dict[str, Any]:
        _ = self.get_model(model_id)
        ts = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE model_lab_models
                SET registry_status = 'REJECTED',
                    operator_note = ?,
                    updated_at = ?
                WHERE model_id = ?
                """,
                (reason, ts, model_id),
            )
        return self.get_model(model_id)

    def export_model_bundle(self, model_id: str) -> dict[str, Any]:
        model = self.get_model(model_id)
        core = {
            "schema": MODEL_BUNDLE_SCHEMA,
            "paper_and_research_only": True,
            "live_execution_authorized": False,
            "model_id": model["model_id"],
            "candidate_id": model["candidate_id"],
            "family": model["family"],
            "market": model["market"],
            "timeframe": model["timeframe"],
            "dataset_id": model["dataset_id"],
            "dataset_sha256": model["dataset_sha256"],
            "uses_kronos": model["uses_kronos"],
            "promotion_eligible": model["promotion_eligible"],
            "rejection_reasons": model["rejection_reasons"],
            "parameters": model["parameters"],
            "metrics_summary": model["metrics_summary"],
        }
        digest = _canonical_bundle_sha256(core)
        return {
            **core,
            "bundle_sha256": digest,
            "exported_at": datetime.now(UTC).isoformat(),
        }

    def import_model_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(bundle, dict) or bundle.get("schema") != MODEL_BUNDLE_SCHEMA:
            raise ValueError(f"Unsupported or missing bundle schema; expected {MODEL_BUNDLE_SCHEMA!r}")
        claimed_sha = str(bundle.get("bundle_sha256") or "").strip().lower()
        if len(claimed_sha) != 64:
            raise ValueError("Missing or malformed bundle_sha256")

        core = {
            "schema": bundle["schema"],
            "paper_and_research_only": bool(bundle.get("paper_and_research_only", True)),
            "live_execution_authorized": False,
            "model_id": str(bundle.get("model_id") or ""),
            "candidate_id": str(bundle.get("candidate_id") or ""),
            "family": str(bundle.get("family") or ""),
            "market": str(bundle.get("market") or "").upper(),
            "timeframe": str(bundle.get("timeframe") or ""),
            "dataset_id": str(bundle.get("dataset_id") or ""),
            "dataset_sha256": str(bundle.get("dataset_sha256") or ""),
            "uses_kronos": bool(bundle.get("uses_kronos", False)),
            "promotion_eligible": bool(bundle.get("promotion_eligible", False)),
            "rejection_reasons": list(bundle.get("rejection_reasons") or []),
            "parameters": bundle.get("parameters") or {},
            "metrics_summary": bundle.get("metrics_summary") or {},
        }
        actual_sha = _canonical_bundle_sha256(core)
        if actual_sha != claimed_sha:
            raise ValueError(
                f"Model bundle SHA-256 verification failed: claimed {claimed_sha[:12]}..., computed {actual_sha[:12]}..."
            )
        if core["market"] not in {"NSE", "NASDAQ"}:
            raise ValueError(f"Incompatible market {core['market']!r}")
        params = core["parameters"]
        if core["family"] == "AXIOM_DECISION_MODEL":
            if tuple(params.get("feature_names") or ()) != FEATURE_NAMES:
                raise ValueError("Incompatible AXIOM decision model feature vector schema")
            weights = params.get("weights") or []
            if len(weights) != len(FEATURE_NAMES) or not all(
                isinstance(w, (int, float)) and math.isfinite(float(w)) for w in weights
            ):
                raise ValueError("Corrupted or non-finite decision model weights")

        ts = datetime.now(UTC).isoformat()
        imp_id = f"imp-{actual_sha[:10]}"
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO model_lab_models
                (model_id, run_id, candidate_id, family, market, timeframe, dataset_id, dataset_sha256,
                 uses_kronos, promotion_eligible, registry_status, rejection_reasons_json, parameters_json,
                 metrics_summary_json, artifact_sha256, operator_note, created_at, updated_at)
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'IMPORTED_PAPER_CANDIDATE', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    imp_id,
                    core["candidate_id"],
                    core["family"],
                    core["market"],
                    core["timeframe"],
                    core["dataset_id"],
                    core["dataset_sha256"],
                    1 if core["uses_kronos"] else 0,
                    1 if core["promotion_eligible"] else 0,
                    json.dumps(core["rejection_reasons"]),
                    json.dumps(core["parameters"], sort_keys=True),
                    json.dumps(core["metrics_summary"], sort_keys=True),
                    actual_sha,
                    "Imported & SHA-256 verified for paper/research inspection only (no trading session enabled).",
                    ts,
                    ts,
                ),
            )
        return self.get_model(imp_id)

    def overview(self) -> dict[str, Any]:
        return {
            "paper_and_research_only": True,
            "live_execution_authorized": False,
            "warning_banner": (
                "AI & MODEL LAB IS STRICTLY RESEARCH & PAPER-ADVISORY ONLY. "
                "Historical backtests, walk-forward Sharpe, and Kronos probability bands NEVER guarantee future profitability "
                "and NEVER authorize or enable LIVE-MARKET LIVE real-money trading."
            ),
            "kronos": self.kronos_provider.status() if self.kronos_provider is not None else {"provider_state": "NOT_INSTALLED"},
            "datasets": self.datasets.list_datasets(),
            "runs": self.list_runs(limit=20),
            "models": self.list_models(),
        }
