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
from pathlib import Path

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


def test_a_reentrant_release_that_arrives_after_the_outer_one_is_survivable(lock_path):
    """The registry entry is GONE by the time the nested exit looks for it.

    The arc w16/ha named and left unwritten (``draft_lock.py`` 189→191): the
    ``if held is not None`` guard in the re-entrant ``finally``. It was filed as
    a two-process race, and it is not one — the entry is process-local, and what
    removes it early is an exit order that is not LIFO. The outermost
    acquisition's own ``finally`` pops the key unconditionally, so ANY release
    that arrives after it finds nothing: an ``ExitStack`` unwound in the wrong
    order, a nested context manager kept alive past its owner, or a generator
    holding one that is closed at collection time rather than at its ``with``.

    Driven through the raw context-manager protocol because that is the only way
    to SPELL a non-LIFO release — nothing is faked, both objects are real locks
    over the real file and the registry is never touched by this test. Without
    the guard the nested release raises ``TypeError: 'NoneType' object is not
    subscriptable`` inside a ``finally``, which would replace whatever exception
    was already travelling.
    """

    outer = draft_generation_lock(lock_path, draft_id="d1", verb="auto")
    outer.__enter__()
    assert lock_path.is_file()

    nested = draft_generation_lock(lock_path, draft_id="d1", verb="rows")
    assert nested.__enter__()["reentered"] is True

    # Out of order: the OUTER one releases first, which unlinks the file and
    # drops the registry entry the nested release is about to look for.
    assert outer.__exit__(None, None, None) is not True
    assert not lock_path.exists()

    assert nested.__exit__(None, None, None) is not True
    assert not lock_path.exists()

    # And the lock is still usable afterwards — a survived mis-order must not
    # leave a depth count behind that refuses the next honest writer.
    with draft_generation_lock(lock_path, draft_id="d1", verb="turnaround"):
        assert lock_path.is_file()
    assert not lock_path.exists()


def test_a_stale_lock_broken_and_taken_by_another_writer_first_names_that_writer(
    lock_path, monkeypatch
):
    """The break wins the unlink and loses the re-claim.

    The arc w16/ha named and left unwritten (``draft_lock.py`` 221→222): two
    writers break the SAME stale lock and only one of them gets it back. The
    window is between ``path.unlink()`` and the retry ``_claim`` — three lines
    with no seam of their own — so the INTERLEAVING is controlled here and
    nothing else is:

    * the rival is a real second thread taking the real lock through the public
      ``draft_generation_lock``, so its holder record is its own;
    * the file it leaves behind is a real file, so the retry ``_claim`` fails for
      the real reason (``O_EXCL`` on a path that exists);
    * the refusal is read back from that file by ``_read_holder``, which is what
      makes the message name ``compose`` and not the stale ``rows`` this call
      broke.

    Controlling WHEN the rival runs is what makes a race deterministic; a test
    that reached into ``_REGISTRY`` or stubbed ``_claim`` would instead be
    asserting the stub. The seam used is ``Path.unlink`` because that is the
    last instruction before the window opens.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"draft": "d1", "verb": "rows", "pid": 4321}), encoding="utf-8"
    )
    stale = time.time() - 600.0
    os.utime(lock_path, (stale, stale))

    took_it = threading.Event()
    release = threading.Event()
    rival_failure: list[BaseException] = []
    started: list[str] = []

    def _rival() -> None:
        try:
            with draft_generation_lock(lock_path, draft_id="d1", verb="compose"):
                took_it.set()
                release.wait(WAIT)
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            rival_failure.append(exc)
            took_it.set()

    rival = threading.Thread(target=_rival)
    real_unlink = Path.unlink

    def _unlink_then_lose_the_lock(self, *args, **kwargs):
        real_unlink(self, *args, **kwargs)
        if self == lock_path and not started:
            started.append("rival")
            rival.start()
            assert took_it.wait(WAIT), "the rival never took the broken lock"

    monkeypatch.setattr(Path, "unlink", _unlink_then_lose_the_lock)

    with pytest.raises(DraftBusy) as caught:
        with draft_generation_lock(
            lock_path, draft_id="d1", verb="rows", stale_after_seconds=60.0
        ):
            pass

    release.set()
    rival.join(WAIT)
    assert not rival.is_alive()
    assert rival_failure == []

    # A second break would be the bug: the stale holder was already cleared, so
    # what holds the file now is a LIVE writer and the answer is a refusal.
    assert "a stale lock was broken and another writer took it first" in str(caught.value)
    assert caught.value.safe_details["verb"] == "compose"
    assert caught.value.safe_details["pid"] == os.getpid()
    assert caught.value.safe_details["draft"] == "d1"
    assert caught.value.code == "draft_busy"
    # The rival held it to the end and released it; the refused caller wrote
    # nothing over it on the way out.
    assert not lock_path.exists()


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


@pytest.mark.parametrize("raised", [FileExistsError, PermissionError])
def test_an_occupied_lock_path_answers_taken_on_both_hosts(lock_path, monkeypatch, raised):
    """One condition, two errnos, and until 2026-09-06 two different answers.

    ``os.open(path, O_CREAT|O_EXCL|O_WRONLY)`` against a path a DIRECTORY sits
    on raises ``EEXIST`` on POSIX and ``EACCES`` on Windows — ``FileExistsError``
    and ``PermissionError``. ``_claim`` caught only the first, so the identical
    draft refused the identical generation with a typed ``DraftBusy`` on one host
    and an unhandled ``OSError`` out of the middle of the verb on the other. That
    is the defect ``agent_runtime.locks._file_lock``'s own docstring was written
    to remove — "the same call had two contracts" — reappearing one package over.

    The errno is PARAMETRIZED rather than left to the host so that both arms are
    proven wherever this runs: a Linux runner exercises the Windows arm and a
    Windows workstation exercises the POSIX one, and neither host can be the only
    place a regression shows up. The directory underneath is real either way,
    because the fix reads the path's existence and a fake that skipped it would
    be asserting the stub instead of the rule.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.mkdir()
    real_open = os.open

    def _open(path, flags, *args, **kwargs):
        if str(path) == str(lock_path):
            raise raised(str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _open)

    with pytest.raises(DraftBusy) as caught:
        with draft_generation_lock(lock_path, draft_id="d1", verb="rows"):
            pass
    assert caught.value.code == "draft_busy"
    # The path is in the refusal, because clearing what sits there by hand is
    # the recovery — and here it is a directory, which is not a lock this
    # runtime ever wrote.
    assert caught.value.safe_details["lock"] == str(lock_path)
    assert str(lock_path) in str(caught.value)
    assert lock_path.is_dir()


def test_a_directory_at_the_lock_path_is_refused_on_this_host_unfaked(lock_path):
    """The same rule with nothing patched at all — whatever errno THIS host
    raises, the answer is the typed refusal. The parametrized test above proves
    both arms; this one proves the real host is one of them."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.mkdir()

    with pytest.raises(DraftBusy):
        with draft_generation_lock(lock_path, draft_id="d1", verb="rows"):
            pass
    assert lock_path.is_dir()


def test_a_permission_error_with_nothing_at_the_lock_path_is_not_a_busy_draft(
    lock_path, monkeypatch
):
    """The narrowing, and why the fix is not a bare ``except PermissionError``.

    ``EACCES`` also means "this process may not create files in this directory"
    — a read-only draft, a bad ACL, a quarantined copy. Answering ``DraftBusy``
    there would tell an operator to wait for a holder that does not exist and
    then to delete a lock file that was never created. So the taken-answer is
    conditioned on something actually BEING at the path, and a permission fault
    over an empty path travels as itself.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    real_open = os.open

    def _open(path, flags, *args, **kwargs):
        if str(path) == str(lock_path):
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _open)

    with pytest.raises(PermissionError):
        with draft_generation_lock(lock_path, draft_id="d1", verb="rows"):
            pass
    assert not lock_path.exists()


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
