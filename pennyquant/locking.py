"""Non-blocking process locks for Windows, macOS and Linux.

Lock a separate sidecar file so Windows readers can keep reading the data file.
Use the same lock pathname in every cooperating process; do not unlink it.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from filelock import FileLock, Timeout


@contextmanager
def exclusive_lock(path: str | Path, *, message: str,
                   error_type: type[Exception] = RuntimeError) -> Iterator[None]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path), timeout=0, mode=0o600)
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise error_type(message) from exc
    try:
        yield
    finally:
        lock.release()
