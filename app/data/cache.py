"""Content-addressed local cache; atomic replacement, no pickle."""

import csv
import hashlib
import io
import json
import os
import tempfile
from pathlib import Path
from app.data.models import Candle


class DataCache:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(content: bytes, options: dict) -> str:
        return hashlib.sha256(
            b"schema-v1" + content + json.dumps(options, sort_keys=True).encode()
        ).hexdigest()

    def get(self, key: str) -> list[Candle] | None:
        path = self.root / f"{key}.csv"
        if not path.exists():
            return None
        with path.open(newline="") as file:
            return [Candle.from_dict(row) for row in csv.DictReader(file)]

    def put(self, key: str, candles: list[Candle]):
        if not candles:
            return
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(candles[0].to_dict()))
        writer.writeheader()
        writer.writerows(c.to_dict() for c in candles)
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as file:
                file.write(buffer.getvalue())
            os.replace(tmp, self.root / f"{key}.csv")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
