"""First-run initializer: `python -m app initialize` (idempotent).

Creates directories, initializes the database (migrations are additive),
runs a best-effort market-universe sync and records an initialization
marker. Safe to re-run at any time; every step reports honestly what it did
(network unavailable or no credentials → recorded, never fabricated).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("bot.bootstrap")


def initialize(settings) -> dict:
    from app.database.database import Database
    from app.database.repository import Repository
    from app.universe.manager import MarketUniverseManager

    root = Path(settings.data_dir)
    dirs = {
        "data": root,
        "history": root / "history",
        "universe": root / "universe",
        "datasets": root / "datasets",
        "logs": Path("logs"),
        "results": Path("results"),
    }
    created = []
    for name, path in dirs.items():
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created.append(str(path))

    database = Database(settings.database_url)
    repository = Repository(database)
    with database.transaction() as db:
        versions = [row[0] for row in db.execute(
            "SELECT version FROM schema_version ORDER BY version").fetchall()]

    universe = MarketUniverseManager(repository, url=settings.universe_sync_url)
    sync = universe.sync()

    already = repository.get_engine_state("initialized")
    repository.set_engine_state("initialized", {
        "runs": (int((already or {}).get("runs", 0)) + 1),
        "last_sync_status": sync.get("status"),
    })
    result = {
        "ok": True,
        "idempotent": bool(already),
        "directories_created": created,
        "schema_versions": versions,
        "universe_sync": sync,
        "database": settings.database_url,
    }
    logger.info("INITIALIZE_DONE", extra={"universe": sync.get("status")})
    print(json.dumps(result, indent=2, default=str))
    return result
