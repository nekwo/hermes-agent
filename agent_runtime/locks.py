from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Iterator

from . import paths

if os.name == "nt":
    import errno
    import msvcrt
else:
    import fcntl


class HarnessLockUnavailable(RuntimeError):
    pass


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        if os.name == "nt":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EDEADLK, 13, 36}:
                    raise HarnessLockUnavailable(f"lock unavailable: {path}") from exc
                raise
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def tick_lock() -> Iterator[None]:
    with _file_lock(paths.lock_dir() / "tick.lock"):
        yield


@contextlib.contextmanager
def task_lock(task_id: str) -> Iterator[None]:
    with _file_lock(paths.lock_dir() / f"task_{task_id}.lock"):
        yield


@contextlib.contextmanager
def run_lock(run_id: str) -> Iterator[None]:
    with _file_lock(paths.lock_dir() / f"run_{run_id}.lock"):
        yield


@contextlib.contextmanager
def worker_session_lock(worker_session_id: str) -> Iterator[None]:
    with _file_lock(paths.lock_dir() / "worker_sessions" / f"{worker_session_id}.lock"):
        yield


@contextlib.contextmanager
def archive_lock() -> Iterator[None]:
    with _file_lock(paths.lock_dir() / "archive.lock"):
        yield


@contextlib.contextmanager
def events_lock() -> Iterator[None]:
    with _file_lock(paths.lock_dir() / "events.lock"):
        yield
