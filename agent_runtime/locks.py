from __future__ import annotations

import contextlib
import errno
import os
from pathlib import Path
import time
from typing import Iterator

from . import paths

#: How long the retry loop sleeps between two non-blocking attempts. One value
#: for both platforms: the deadline is the contract, this is only how finely it
#: is sampled.
_RETRY_SLEEP_SECONDS = 0.05

if os.name == "nt":
    import msvcrt

    #: What ``msvcrt.locking`` answers when somebody else holds the byte. The
    #: raw 13/36 sit beside the named constants because Windows reports the
    #: values without always mapping them onto the errno names.
    _CONTENDED_ERRNOS = frozenset({errno.EACCES, errno.EDEADLK, 13, 36})

    def _prepare(handle) -> None:
        # ``msvcrt.locking`` locks a BYTE RANGE, so an empty file has nothing to
        # lock. Seed one byte the first time, then rewind: every acquisition
        # locks byte 0 of the same file.
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)

    def _try_acquire(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _release(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    #: What ``flock(..., LOCK_NB)`` answers when somebody else holds the file.
    #: ``EAGAIN`` and ``EWOULDBLOCK`` are the same number on Linux and macOS but
    #: are spelled separately because POSIX does not require that, and
    #: ``EACCES`` is what some network filesystems answer instead.
    _CONTENDED_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})

    def _prepare(handle) -> None:
        # ``flock`` locks the whole open file description, so there is no byte
        # to seed and nothing to do. Present so the acquire loop below has one
        # shape on both platforms rather than a conditional inside it.
        return None

    def _try_acquire(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class HarnessLockUnavailable(RuntimeError):
    pass


@contextlib.contextmanager
def _file_lock(path: Path, *, timeout_seconds: float | None = None) -> Iterator[None]:
    """Hold an exclusive cross-process lock on ``path``, or refuse by deadline.

    ONE retry loop, on every platform. It used to be two arms: Windows polled
    ``LK_NBLCK`` against the deadline and refused :class:`HarnessLockUnavailable`
    when it ran out, while POSIX took a bare blocking ``flock(..., LOCK_EX)``
    that read the deadline it had just computed and then ignored it. So the same
    call had two contracts — bounded on one host, unbounded on the other — and
    ``timeout_seconds`` was a parameter that did nothing off Windows. Every
    caller that turns this refusal into a typed answer (``agent create``'s
    ``creation_in_progress``, the chat-turn reservation's ``turn_in_progress``,
    the persona chat mint) was therefore Windows-only behaviour that read as
    portable, and a stuck holder hung the process everywhere else.

    Now the platform supplies only its three primitives — seed, non-blocking
    acquire, release — and the deadline logic is shared. That is the whole of
    the change: contention is polled at :data:`_RETRY_SLEEP_SECONDS`, an errno
    outside :data:`_CONTENDED_ERRNOS` is a real fault and is re-raised
    unwrapped, and running out of time raises the refusal naming the budget it
    spent.

    NOT reentrant, and now uniformly so: a second acquisition from this same
    process opens a fresh handle — a fresh open file description under
    ``flock``, a second byte-range request under ``msvcrt`` — and contends with
    the first, so it refuses at the deadline rather than deadlocking forever.
    :meth:`OfficeStore.upsert_actor`'s ``position_policy`` hook exists because
    of that property: work that must see the locked state runs INSIDE the lock
    that already holds it, never by taking it again.
    """

    timeout_seconds = _lock_timeout_seconds(timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        _prepare(handle)
        while True:
            try:
                _try_acquire(handle)
                break
            except OSError as exc:
                if exc.errno not in _CONTENDED_ERRNOS:
                    raise
                if time.monotonic() >= deadline:
                    raise HarnessLockUnavailable(
                        f"lock unavailable after {timeout_seconds:g}s: {path}"
                    ) from exc
                time.sleep(_RETRY_SLEEP_SECONDS)
        try:
            yield
        finally:
            _release(handle)


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
