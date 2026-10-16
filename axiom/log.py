"""AXIOM logging conventions.

AXIOM never adds handlers or changes the root logger on import: libraries that
do that fight the host application. Modules call :func:`get_logger` and log at
the level that matches the consequence:

* ``debug``    — cache decisions, per-job progress
* ``info``     — Kronos lifecycle transitions (LOADING → READY, shutdown)
* ``warning``  — a forecast was rejected, data was degraded, a request timed out
* ``error``    — inference or load failed; the reason is always in the message
* ``exception``— an unexpected bug (traceback included)

Every message that reaches the UI also lands in ``KronosStatus.message`` /
``last_error``, so a user can see the reason without reading a log file.
"""

from __future__ import annotations

import logging
import sys

ROOT_NAME = "axiom"


def get_logger(name: str = "") -> logging.Logger:
    """``get_logger("kronos.runtime")`` → logger ``axiom.kronos.runtime``."""
    return logging.getLogger(f"{ROOT_NAME}.{name}" if name else ROOT_NAME)


def configure(level: int = logging.INFO, stream=None, *, force: bool = False) -> logging.Logger:
    """Attach a stderr handler for standalone runs (child process, tests).

    Idempotent: repeated calls do not duplicate handlers unless ``force``.
    """
    logger = get_logger()
    logger.setLevel(level)
    if force:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
    if not logger.handlers:
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.propagate = False
    return logger
