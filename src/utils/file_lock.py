"""Small cross-thread/process file lock used by JSON/Parquet stores."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

from src.utils.io import ensure_dir


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def file_lock(path: str | Path):
    """Acquire an exclusive lock represented by ``path``."""
    lock_path = Path(path)
    ensure_dir(lock_path)
    key = str(lock_path.resolve())
    with _LOCKS_GUARD:
        thread_lock = _LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        stream = lock_path.open("a+b")
        try:
            try:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            except ImportError:
                pass
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except ImportError:
                pass
            stream.close()


__all__ = ["file_lock"]
