"""Single-instance lease for the autonomy execution boundary.

Activation and Armed sessions must belong to exactly one running AXIOM
process per data directory. The lease is an OS-level lock (msvcrt on
Windows, flock on POSIX) on a small lock file beside the autonomy journal;
a second process cannot acquire it and must fail closed (never arm).

The lease is process-local: it is released automatically when the process
exits or crashes, so a stale lease can never block a fresh start.
"""

from __future__ import annotations

import os
from pathlib import Path


class LeaseUnavailable(RuntimeError):
    """Another process holds the single-instance lease."""


class SingleInstanceLease:
    def __init__(self, directory: Path, name: str = "autonomy-single-instance.lock"):
        self.path = Path(directory) / name
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise LeaseUnavailable(
                "Another AXIOM instance holds the autonomy single-instance lease; "
                "Arm and Activate are unavailable in this process"
            ) from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
