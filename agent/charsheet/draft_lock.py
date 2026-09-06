"""One writer per draft, across processes and across serve pool threads.

The hole this closes, in the plan's words: *"hermes has no per-draft lock: two
``characters`` generations on one draft interleave into one revision store."*
The serve child runs requests on a ``ThreadPoolExecutor(max_workers=4)``, one
worker per request, so two ``characters rows --draft X`` calls genuinely run at
the same time in ONE process; and the launcher's own latch cannot see a
generation an agent started from a chat turn at all. Both writers then go
last-writer-wins per item through :class:`~agent.charsheet.revisions
.ImageRevisionStore`, which is atomic per file and says nothing about two
writers.

**Mechanism: an ``O_EXCL`` lock file carrying its holder, plus an age ceiling.**
The alternative — an OS advisory lock (``msvcrt.locking`` / ``fcntl.flock``, the
shape :mod:`agent_runtime.locks` uses) — was considered and rejected here for
two reasons that are specific to this lane:

* **The refusal has to name the holder.** A byte-range lock answers "taken" and
  nothing else. What an operator staring at a refused generate click needs is
  *which verb*, *since when* and *which pid* — a 15-minute ``rows`` batch and a
  crashed one look identical without it, and the launcher's ``next`` hint has
  nothing to say either. A file whose CONTENT is the holder answers that; a
  lock whose content is a byte cannot.
* **Pid liveness is not available on both hosts, so it is used on neither.**
  ``os.kill(pid, 0)`` KILLS the process on Windows — this repo records that
  finding in ``tool/test_quality/README.md``, where the mutation gate refuses a
  liveness probe for exactly that reason. The tempting fix (probe on POSIX, age
  out on Windows) is the defect :func:`agent_runtime.locks._file_lock`'s own
  docstring was written to remove: "the same call had two contracts — bounded
  on one host, unbounded on the other". So there is ONE policy on both hosts,
  and it is the ceiling below.

**The ceiling.** :data:`STALE_HOLDER_SECONDS` is 45 minutes. It is chosen to sit
ABOVE every honest generation and BELOW an operator's patience:

* hermes's own rate estimate is 1–2 min per generation, so a ten-strip ``rows``
  batch is 10–20 min and a full ``auto`` on a fifteen-item draft is 11–22 min
  (``hermes_cli/harness.py::_characters_auto_write`` and
  ``_characters_auto_plan`` both state the 10–20 figure);
* the launcher's long-run lane gives one call 30 minutes
  (``kHarnessLongRunCeiling``) before it releases its own ticket and reports
  ``long_run_ceiling``. A hermes ceiling BELOW that would break a live run whose
  launcher ticket is still valid, which is the one thing this lock must never
  do.

45 min clears both with room, and a crashed generation therefore clears itself
inside one operator break rather than wedging a draft forever. It is not a
timeout on the work: nothing interrupts a holder, and a live generation past 45
minutes simply loses its exclusivity — the honest cost of having no way to ask
the OS whether the holder is alive.

**Reentrancy is per THREAD, and that is the whole point.** ``characters auto``
holds this lock across its four steps and then calls the very methods that take
it, so the same thread must be able to re-enter. A DIFFERENT thread in the same
process is a second writer — that is the serve pool case — and takes the file
path like any other process. So the in-process registry keys on
``threading.get_ident()`` and only ever short-circuits for the holder itself;
everything else contends on the file.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from agent.charsheet.errors import DraftBusy

logger = logging.getLogger(__name__)

#: The lock file's name, beside ``draft.json`` in the draft directory. Inside
#: the draft rather than in a shared lock directory so a draft carries its own
#: lock wherever it is copied, backed up or quarantined to — the same reasoning
#: that puts the revision store there.
LOCK_FILENAME = "generation.lock"

#: How old a holder must be before another writer may break it. See the module
#: docstring for why this is a ceiling and not a liveness probe.
STALE_HOLDER_SECONDS = 45.0 * 60.0

#: Path string → ``[owning thread ident, depth]``. Guarded by
#: :data:`_REGISTRY_LOCK`. Only the OWNING thread ever reads its own entry; a
#: different thread falls through to the file and contends there.
_REGISTRY: dict[str, list[int]] = {}
_REGISTRY_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_holder(path: Path) -> dict:
    """The holder's own account of itself, or the best that can be said.

    A holder file is created and written in two steps, so a reader can catch it
    empty; and a truncated or hand-edited one decodes to nothing. Neither is a
    reason to say "not held" — the file EXISTS, which is the lock. The age
    falls back to the file's mtime, which is what the ceiling actually needs.
    """

    holder: dict = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"lock": str(path)}
    try:
        decoded = json.loads(text)
    except ValueError:
        decoded = None
    if isinstance(decoded, dict):
        holder.update(decoded)
    holder["lock"] = str(path)
    try:
        age = max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        age = 0.0
    holder["age_seconds"] = round(age, 1)
    return holder


def _claim(path: Path, payload: dict) -> bool:
    """Create *path* exclusively and write *payload* into it; False if taken.

    "Taken" is ONE condition with two errnos. ``O_EXCL`` against a path a
    DIRECTORY sits on raises ``EEXIST`` on POSIX and ``EACCES`` on Windows, so
    catching only ``FileExistsError`` gave the identical draft two contracts —
    a typed :class:`~agent.charsheet.errors.DraftBusy` on one host, an unhandled
    ``OSError`` out of the middle of the verb on the other. That is precisely
    the split :func:`agent_runtime.locks._file_lock`'s docstring exists to
    forbid, and it is the module docstring's own rule above: one policy on both
    hosts.

    The ``EACCES`` arm is conditioned on the path EXISTING, and the condition is
    the whole care. ``EACCES`` also means "this process may not create files
    here" — a read-only draft, a bad ACL — which is not a held lock: answering
    "taken" there would tell an operator to wait for a holder that does not
    exist and then delete a lock file nobody ever wrote. So a permission fault
    over an empty path travels as itself.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except PermissionError:
        if path.exists():
            return False
        raise
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        # A claim that cannot record WHO holds it is still a claim — the file
        # exists and excludes everybody. Leave it and carry on rather than
        # unlinking a lock this call now owns.
        logger.warning("charsheet draft lock %s: holder record not written", path)
    return True


@contextlib.contextmanager
def draft_generation_lock(
    path,
    *,
    draft_id: str,
    verb: str,
    stale_after_seconds: float | None = None,
) -> Iterator[dict]:
    """Hold *path* for the duration of one generation verb, or refuse.

    Yields the holder record this call wrote. Raises
    :class:`~agent.charsheet.errors.DraftBusy` when somebody else holds it,
    naming them.

    *stale_after_seconds* overrides :data:`STALE_HOLDER_SECONDS` (a test lowers
    it). A holder older than the ceiling is broken exactly ONCE per attempt: the
    file is unlinked and the claim retried, and if that retry loses too, the
    winner is a live writer and the answer is a refusal, not a second break.
    """

    path = Path(path)
    key = str(path)
    ident = threading.get_ident()
    ceiling = STALE_HOLDER_SECONDS if stale_after_seconds is None else float(stale_after_seconds)

    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(key)
        if entry is not None and entry[0] == ident:
            entry[1] += 1
            reentered = True
        else:
            reentered = False
    if reentered:
        # ``auto`` calling ``run_rows``: the outermost acquisition owns the file
        # and this one owns nothing, so the exit below must not unlink it.
        try:
            yield {"reentered": True, "draft": draft_id, "verb": verb}
        finally:
            with _REGISTRY_LOCK:
                held = _REGISTRY.get(key)
                if held is not None:
                    held[1] -= 1
        return

    record = {
        "draft": draft_id,
        "verb": verb,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started": _utc_now(),
    }
    if not _claim(path, record):
        holder = _read_holder(path)
        age = float(holder.get("age_seconds") or 0.0)
        if age < ceiling:
            raise DraftBusy(
                f"draft {draft_id} is busy: {holder.get('verb') or 'a generation'} "
                f"has held it for {age:.0f}s (pid {holder.get('pid', '?')} on "
                f"{holder.get('host', '?')}, since {holder.get('started', '?')}). "
                "Wait for it to finish, or — if that process is gone — delete "
                f"{path}.",
                safe_details=holder,
            )
        logger.warning(
            "charsheet draft %s: breaking a stale generation lock held %.0fs by %s (%s)",
            draft_id,
            age,
            holder.get("verb", "?"),
            holder.get("pid", "?"),
        )
        with contextlib.suppress(OSError):
            path.unlink()
        if not _claim(path, record):
            holder = _read_holder(path)
            raise DraftBusy(
                f"draft {draft_id} is busy: a stale lock was broken and another "
                f"writer took it first ({holder.get('verb') or 'a generation'}, "
                f"pid {holder.get('pid', '?')}).",
                safe_details=holder,
            )

    with _REGISTRY_LOCK:
        _REGISTRY[key] = [ident, 1]
    try:
        yield dict(record)
    finally:
        # Reached ONLY by the acquisition that created the file — a nested one
        # returned above, before this block existed for it — so the unlink is
        # unconditional and there is no depth to consult. A `released = depth <=
        # 0` guard here read as caution and was neither: nothing can reach this
        # line at depth > 0, and a branch that cannot be false is a claim about
        # the runtime that is not true. The mutation gate is what said so — the
        # guard's mutant survived, because no test could tell the two apart.
        with _REGISTRY_LOCK:
            _REGISTRY.pop(key, None)
        with contextlib.suppress(OSError):
            path.unlink()
