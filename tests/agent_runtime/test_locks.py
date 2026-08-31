import threading
import time

import pytest

from agent_runtime.locks import HarnessLockUnavailable, _file_lock, task_lock


def test_task_lock_uses_task_specific_lock_file(isolate_agent_runtime_root):
    with task_lock("task_abc"):
        assert (isolate_agent_runtime_root / "locks" / "task_task_abc.lock").exists()


def _contend(path, *, timeout_seconds: float) -> list:
    """Take ``path`` on another thread and report what happened, never hang.

    A THREAD rather than a subprocess because ``flock`` is held by the open file
    description, not by the process: ``_file_lock`` opens a fresh handle per
    acquisition, so a second acquisition from this same interpreter contends
    with the first exactly as another process would. That is also the property
    the office lock's non-reentrancy rests on, so proving it here proves the
    thing ``upsert_actor`` relies on.
    """

    outcome: list = []

    def _run() -> None:
        try:
            with _file_lock(path, timeout_seconds=timeout_seconds):
                outcome.append("acquired")
        except BaseException as exc:  # noqa: BLE001 — the outcome IS the assertion
            outcome.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    # Generous against the deadline under test: this bound exists to turn a
    # BLOCKING acquisition into a failing assertion instead of a hung suite, not
    # to measure the deadline.
    thread.join(timeout=15)
    outcome.append(thread.is_alive())
    return outcome


def test_a_contended_lock_refuses_at_its_deadline_instead_of_blocking(tmp_path):
    """H-H6. The deadline is reachable on THIS host, whichever host it is.

    ``_file_lock`` computed a deadline on every platform and then, off Windows,
    took a bare blocking ``fcntl.flock(..., LOCK_EX)`` that never consulted it —
    so ``HarnessLockUnavailable`` was unreachable on POSIX and every caller that
    turns it into a typed refusal (``creation_in_progress``,
    ``turn_in_progress``) was Windows-only behaviour wearing a portable name.

    *Mutation:* restore the blocking acquire (drop ``| fcntl.LOCK_NB`` from
    ``_try_acquire``). The contending thread never returns, ``is_alive()`` stays
    True, and the run goes red on the alive assertion rather than hanging the
    suite.

    WHERE THAT CLAIM FIRES, stated because a Windows run reports it as a
    survivor. The mutation edits the POSIX arm of a platform ``if``, which a
    Windows interpreter never executes, so
    ``hh6-posix-file-lock-ignores-its-deadline`` can only be killed on a POSIX
    host — which is where the gate that enforces it lives (the
    ``mutation-claims`` job runs ``ubuntu-latest``). It is anchored at
    ``module`` scope for the same reason: the selector's symbol resolver walks
    definitions, not the branches of a conditional, so ``_try_acquire`` /
    ``_prepare`` / ``_release`` have no anchorable node. The two claims over
    ``_file_lock`` itself are the half a Windows host does pin, and they cover
    the shared deadline loop this stage actually added.
    """

    path = tmp_path / "office.lock"
    with _file_lock(path, timeout_seconds=0.2):
        *results, still_alive = _contend(path, timeout_seconds=0.2)

    assert still_alive is False, "the contended acquisition blocked past its deadline"
    assert len(results) == 1
    assert isinstance(results[0], HarnessLockUnavailable), results[0]
    assert "0.2s" in str(results[0])
    assert str(path) in str(results[0])


def test_the_deadline_is_a_budget_and_not_a_single_attempt(tmp_path):
    """The refusal comes AFTER the budget, not on the first contended attempt.

    A non-blocking acquire that gave up immediately would also make
    ``HarnessLockUnavailable`` reachable — and would break every caller that
    merely waits out a short holder. The retry loop is what keeps
    ``timeout_seconds`` a budget.

    *Mutation:* delete the ``time.sleep`` + retry and refuse on the first
    ``OSError``. The elapsed time collapses to ~0 and the lower bound fails.
    """

    path = tmp_path / "office.lock"
    with _file_lock(path, timeout_seconds=0.5):
        started = time.monotonic()
        *results, still_alive = _contend(path, timeout_seconds=0.5)
        elapsed = time.monotonic() - started

    assert still_alive is False
    assert isinstance(results[0], HarnessLockUnavailable)
    assert elapsed >= 0.4, f"refused after only {elapsed:.3f}s of a 0.5s budget"


def test_a_released_lock_is_immediately_takeable_again(tmp_path):
    """The retry loop still ACQUIRES — a deadline that refused a free lock would
    be the opposite defect.

    *Mutation:* drop the ``break`` after a successful ``_try_acquire`` so the
    loop never exits. Killed — as the suite timeout rather than as an
    assertion, because the mutant hangs the FIRST acquisition, the one this
    test takes itself.

    *Not* killed, stated rather than claimed: unwiring ``_release`` survives on
    POSIX, because ``with open(...)`` closes the descriptor on the way out and
    ``flock`` is released with it. ``_release`` is load-bearing on Windows,
    where ``msvcrt`` byte-range locks outlive nothing of the sort, and this
    host cannot pin that.
    """

    path = tmp_path / "office.lock"
    with _file_lock(path, timeout_seconds=0.2):
        pass
    *results, still_alive = _contend(path, timeout_seconds=0.2)
    assert still_alive is False
    assert results == ["acquired"]


def test_a_fault_that_is_not_contention_is_raised_unwrapped(tmp_path, monkeypatch):
    """An errno outside the contended set is a real fault and must not spend the
    budget pretending to be a busy peer.

    *Mutation:* drop the ``if exc.errno not in _CONTENDED_ERRNOS: raise`` arm.
    The mutant polls a permanent fault for the whole budget and then reports it
    as ``HarnessLockUnavailable`` — a wrong diagnosis after a wrong wait.
    """

    from agent_runtime import locks

    def _boom(handle):
        raise OSError(9, "bad file descriptor")

    monkeypatch.setattr(locks, "_try_acquire", _boom)
    with pytest.raises(OSError) as caught:
        with _file_lock(tmp_path / "office.lock", timeout_seconds=5):
            pass
    assert not isinstance(caught.value, HarnessLockUnavailable)
    assert caught.value.errno == 9
