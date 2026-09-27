"""Small JSON-lines logger. Never accepts arbitrary credential/config payloads."""

import json
import logging
from datetime import datetime, timezone

FIELDS = {"symbol", "strategy", "signal", "order", "price", "quantity", "error"}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "component": record.name,
            "event": record.getMessage(),
        }
        payload.update({key: getattr(record, key, None) for key in FIELDS})
        return json.dumps(payload, default=str)


def configure(level: str = "INFO", path=None):
    logger = logging.getLogger("bot")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        if path:
            from pathlib import Path

            Path(path).parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(path)
            file_handler.setFormatter(JsonFormatter())
            logger.addHandler(file_handler)
    logger.propagate = False
    return logger
