"""One writer per draft — the refusal, the re-entry, and the stale ceiling.

The defect these pin, in the long-run lane design's words: *"hermes has no
per-draft lock: two ``characters`` generations on one draft interleave into one
revision store."* The serve child runs four pool workers in ONE process, so the
second writer is as likely to be another THREAD as another process — both cases
are here.

Pure stdlib: this file drives :mod:`agent.charsheet.draft_lock` directly and
never touches Pillow, matching the module it tests (the whole charsheet lock is
stdlib by the same packaging boundary that keeps ``agent_runtime`` out of this
package).
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from agent.charsheet.draft_lock import (
    LOCK_FILENAME,
    STALE_HOLDER_SECONDS,
    draft_generation_lock,
)
from agent.charsheet.errors import CharsheetRefusal, DraftBusy

WAIT = 20.0


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "draft" / LOCK_FILENAME


def test_a_second_writer_on_one_draft_is_refused_and_told_who_holds_it(lock_path):
    """The row itself: two generations, one draft, and the second is refused.

    A second THREAD, because that is the shape the serve pool actually produces
    — ``ThreadPoolExecutor(max_workers=4)``, one worker per request, so two
    ``characters rows --draft X`` calls run concurrently inside one process. The
    in-process re-entry registry must NOT mistake it for the holder.
    """

    refused: list[BaseException] = []
    entered = threading.Event()
    release = threading.Event()

    def _second() -> None:
        try:
            with draft_generation_lock(lock_path, draft_id="d1", verb="rows"):
                refused.append(AssertionError("the second writer was admitted"))
        except BaseException as exc:  # noqa: BLE001 - the assertion is the type
            refused.append(exc)

    with draft_generation_lock(lock_path, draft_id="d1", verb="turnaround"):
        entered.set()
        worker = threading.Thread(target=_second)
        worker.start()
        worker.join(WAIT)
        assert not worker.is_alive()
        release.set()

    assert entered.is_set()
    assert len(refused) == 1
    exc = refused[0]
    assert isinstance(exc, DraftBusy)
    # A refusal that says only "busy" leaves an operator with no next move.
    assert exc.safe_details["verb"] == "turnaround"
    assert exc.safe_details["pid"] == os.getpid()
    assert exc.safe_details["lock"] == str(lock_path)
    # The lock path is in the MESSAGE too: there is no pid liveness on Windows,
    # so clearing a crashed holder by hand is a real recovery and the operator
    # must not have to guess the file.
    assert str(lock_path) in str(exc)
    assert exc.code == "draft_busy"
    # It is a typed refusal, not an invalid request: `_CHARACTERS_EXPECTED`
    # catches the base class, and nothing maps this onto `ValueError`.
    assert isinstance(exc, CharsheetRefusal)
    assert not isinstance(exc, ValueError)


def test_the_lock_is_released_when_the_verb_ends_and_when_it_raises(lock_path):
    with draft_generation_lock(lock_path, draft_id="d1", verb="rows"):
        assert lock_path.is_file()
    assert not lock_path.exists()

    with pytest.raises(RuntimeError, match="the provider fell over"):
        with draft_generation_lock(lock_path, draft_id="d1", verb="rows"):
            raise RuntimeError("the provider fell over")
    # A generation that dies must not wedge the draft until the ceiling laps it.
    assert not lock_path.exists()

    with draft_generation_lock(lock_path, draft_id="d1", verb="turnaround"):
        pass


def test_the_same_thread_re_enters_and_the_nested_exit_does_not_release(lock_path):
    """``characters auto`` holds the lock and then calls the verbs that take it.

    The nested exit must not unlink the file: that would hand the draft to a
    second writer while ``auto`` was still three steps from done.
    """

    refused: list[BaseException] = []

    def _outsider() -> None:
        try:
            with draft_generation_lock(lock_path, draft_id="d1", verb="rows"):
                refused.append(AssertionError("admitted while auto held it"))
        except BaseException as exc:  # noqa: BLE001
            refused.append(exc)

    with draft_generation_lock(lock_path, draft_id="d1", verb="auto"):
        with draft_generation_lock(lock_path, draft_id="d1", verb="turnaround") as inner:
            assert inner["reentered"] is True
        # The nested `with` has exited. The file must still be held.
        assert lock_path.is_file()
        worker = threading.Thread(target=_outsider)
        worker.start()
        worker.join(WAIT)
        assert not worker.is_alive()

    assert not lock_path.exists()
    assert [type(exc) for exc in refused] == [DraftBusy]
    # The holder named is the OUTERMOST verb, which is the one still running.
    assert refused[0].safe_details["verb"] == "auto"


def test_a_stale_holder_is_broken_by_the_age_ceiling_and_not_by_a_pid_probe(lock_path):
    """A crashed generation clears itself; a live one never does.

    Deliberately NOT a liveness probe. ``os.kill(pid, 0)`` KILLS the process on
    Windows, so the policy is one ceiling on both hosts (see the module
    docstring). The pid written below is this very process — alive, and still
    broken once it is old enough, which is the honest cost of the ceiling and
    the thing this test states out loud.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"draft": "d1", "verb": "rows", "pid": os.getpid()}),
        encoding="utf-8",
    )
    old = time.time() - 120.0
    os.utime(lock_path, (old, old))

    # Under the ceiling: refused, however dead the holder may be.
    with pytest.raises(DraftBusy):
        with draft_generation_lock(
            lock_path, draft_id="d1", verb="rows", stale_after_seconds=600.0
        ):
            pass

    # Over it: broken and re-taken, with the new holder recorded.
    with draft_generation_lock(
        lock_path, draft_id="d1", verb="turnaround", stale_after_seconds=60.0
    ):
        holder = json.loads(lock_path.read_text(encoding="utf-8"))
    assert holder["verb"] == "turnaround"
    assert not lock_path.exists()


def test_an_unreadable_holder_still_holds_and_ages_off_its_mtime(lock_path):
    """The file EXISTS, which is the lock — its content is only the account.

    A holder is created and then written, so a reader can catch it empty; and a
    truncated one decodes to nothing. Neither may be read as "not held", which
    would admit the second writer at exactly the moment the first was starting.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(DraftBusy) as caught:
        with draft_generation_lock(lock_path, draft_id="d1", verb="rows"):
            pass
    assert caught.value.safe_details["lock"] == str(lock_path)

    old = time.time() - 10.0
    os.utime(lock_path, (old, old))
    with draft_generation_lock(
        lock_path, draft_id="d1", verb="rows", stale_after_seconds=1.0
    ):
        pass


def test_the_ceiling_clears_the_launcher_long_run_ticket_it_must_not_break():
    """A contract about two numbers, not a snapshot of one.

    The launcher gives a long-run call 30 minutes before it releases its ticket
    (`kHarnessLongRunCeiling`). A hermes ceiling at or below that would break a
    generation whose launcher ticket is still valid — the one thing this lock
    must never do — so the ceiling is stated as a relationship to that bound and
    to hermes's own worst-case `auto` (22 min by its own rate estimate).
    """

    launcher_long_run_ceiling_seconds = 30.0 * 60.0
    slowest_estimated_auto_seconds = 22.0 * 60.0
    assert STALE_HOLDER_SECONDS > launcher_long_run_ceiling_seconds
    assert STALE_HOLDER_SECONDS > slowest_estimated_auto_seconds
