from __future__ import annotations

import contextlib
import os
from pathlib import Path
import time
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
def _file_lock(path: Path, *, timeout_seconds: float | None = None) -> Iterator[None]:
    timeout_seconds = _lock_timeout_seconds(timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        if os.name == "nt":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EDEADLK, 13, 36}:
                        raise
                    if time.monotonic() >= deadline:
                        raise HarnessLockUnavailable(f"lock unavailable after {timeout_seconds:g}s: {path}") from exc
                    time.sleep(0.05)
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


# S54 removed ``tick_lock``: the ticker it guarded went with the mission lane.
# ``task_lock`` stays -- it still has callers.
#
# S66 removed ``run_lock`` and ``incident_lock``. They were orphaned by S65's
# own cuts, not by an older wave: ``RunStore``/``IncidentStore`` became
# historical READERS there (``update`` / ``list_for_task`` / ``open`` / ``close``
# retired), and a reader needs no write lock. Receiver-aware scan at that HEAD
# found zero references to either name outside its own definition — not even a
# test.

@contextlib.contextmanager
def task_lock(task_id: str) -> Iterator[None]:
    with _file_lock(paths.lock_dir() / f"task_{task_id}.lock"):
        yield


@contextlib.contextmanager
def board_lock(board_id: str) -> Iterator[None]:
    with _file_lock(paths.lock_dir() / "boards" / f"{paths.safe_path_token(board_id)}.lock"):
        yield


@contextlib.contextmanager
def office_lock(workspace_id: str) -> Iterator[None]:
    with _file_lock(paths.lock_dir() / "office" / f"{paths.safe_path_token(workspace_id)}.lock"):
        yield


@contextlib.contextmanager
def persona_chat_mint_lock(key_digest: str) -> Iterator[None]:
    """Serialize one chat-root mint idempotency key across CLI/serve processes."""
    with _file_lock(
        paths.lock_dir()
        / "persona_chat_mints"
        / f"{paths.safe_path_token(key_digest)}.lock"
    ):
        yield


@contextlib.contextmanager
def persona_chat_instance_lock(persona_instance_id: str) -> Iterator[None]:
    """Serialize chat-root selection changes for one persona instance."""
    with _file_lock(
        paths.lock_dir()
        / "persona_chat_instances"
        / f"{paths.safe_path_token(persona_instance_id)}.lock"
    ):
        yield


@contextlib.contextmanager
def agent_create_lock(key_digest: str) -> Iterator[None]:
    """Serialize one ``runtime.agent.create`` idempotency key across processes.

    Nothing else in the runtime takes this lock, which is what lets the office
    lock be acquired INSIDE it (``upsert_actor``) without minting a new
    deadlock order: no path ever waits on an agent-create lock while holding an
    office one.
    """
    with _file_lock(
        paths.lock_dir()
        / "agent_creates"
        / f"{paths.safe_path_token(key_digest)}.lock"
    ):
        yield


@contextlib.contextmanager
def chat_turn_reservation_lock(key_digest: str) -> Iterator[None]:
    """Serialize one ``turn_request_id``'s ACCEPT decision across processes.

    Held for microseconds and around no other lock. The chat-root lease — the
    one a turn actually contends on — is taken later, on the WORKER, long after
    this has been released, so this cannot participate in a lock order at all.
    That is deliberate: an accept that waited on the chat lease would turn the
    reader loop's fast ack into a blocking call for the exact duration of the
    turn already running, which is the property the RPC lane exists to avoid.
    """
    with _file_lock(
        paths.lock_dir()
        / "chat_turns"
        / f"{paths.safe_path_token(key_digest)}.lock"
    ):
        yield


@contextlib.contextmanager
def archive_lock() -> Iterator[None]:
    with _file_lock(paths.lock_dir() / "archive.lock"):
        yield


@contextlib.contextmanager
def events_lock() -> Iterator[None]:
    with _file_lock(paths.lock_dir() / "events.lock"):
        yield


def _lock_timeout_seconds(value: float | None) -> float:
    if value is not None:
        try:
            return max(0.01, float(value))
        except (TypeError, ValueError):
            return 15.0
    try:
        from .config import load_root_runtime_config

        return max(0.01, float(getattr(load_root_runtime_config(), "lock_acquire_timeout_seconds", 15) or 15))
    except Exception:
        return 15.0
