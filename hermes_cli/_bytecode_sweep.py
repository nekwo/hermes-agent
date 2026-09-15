"""Fork single-winner bytecode invalidation and boot receipts."""
import logging
import os
import time as _time
from pathlib import Path
from hermes_cli import _boot_clock
logger = logging.getLogger(__name__)

_BYTECODE_FINGERPRINT_FILE = ".bytecode-fingerprint"

_BYTECODE_SWEEP_LOCK_FILE = ".bytecode-sweep.lock"

_BYTECODE_SWEEP_LOCK_WAIT_SECONDS = 20.0

_BYTECODE_SWEEP_LOCK_STALE_SECONDS = 120.0

_SWEEP_OUTCOME_SWEPT = "swept"

_SWEEP_OUTCOME_WAITED = "waited_for_winner"

_SWEEP_OUTCOME_UNSWEPT = "proceeded_unswept"

def _bytecode_sweep_lock_path() -> Path:
    from hermes_cli.main import PROJECT_ROOT
    return PROJECT_ROOT / _BYTECODE_SWEEP_LOCK_FILE

def _break_stale_bytecode_sweep_lock(lock_path: Path) -> bool:
    """Remove a sweep lock old enough that its holder cannot still be alive.

    Age is read off the file's own mtime rather than a pid recorded inside it: a
    pid is only checkable on the machine that wrote it, and a checkout can be on
    a share. Returns whether a lock was removed.
    """

    try:
        age = _time.time() - lock_path.stat().st_mtime
    except OSError:
        return False
    if age < _BYTECODE_SWEEP_LOCK_STALE_SECONDS:
        return False
    try:
        lock_path.unlink()
    except OSError:
        # Somebody else broke it first, or the filesystem refused. Either way
        # this process is not the one that has to care.
        return False
    logger.debug(
        "Broke a stale bytecode-sweep lock (%.0fs old): %s", age, lock_path
    )
    return True

_SWEEP_CLAIM_CLAIMED = "claimed"

_SWEEP_CLAIM_CONTENDED = "contended"

_SWEEP_CLAIM_UNAVAILABLE = "unavailable"

def _claim_bytecode_sweep_lock(lock_path: Path) -> str:
    """Try to claim the right to sweep. See the three ``_SWEEP_CLAIM_*`` answers.

    ``O_EXCL`` and not a write-if-missing, exactly as ``serve_auth._mint`` does
    it and for the same reason: two hermes processes booting against one checkout
    is a REAL concurrency, and the loser must defer to the winner rather than
    overwrite its claim.
    """

    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        if not _break_stale_bytecode_sweep_lock(lock_path):
            return _SWEEP_CLAIM_CONTENDED
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            # Another process claimed it in the gap after we broke the stale one.
            return _SWEEP_CLAIM_CONTENDED
        except OSError:
            return _SWEEP_CLAIM_UNAVAILABLE
    except OSError:
        return _SWEEP_CLAIM_UNAVAILABLE
    try:
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
    except OSError:
        pass
    finally:
        os.close(fd)
    return _SWEEP_CLAIM_CLAIMED

def _release_bytecode_sweep_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except OSError:
        pass

def _await_bytecode_sweep_winner(lock_path: Path) -> bool:
    """Wait for the winner to release its lock. True if it did, in time.

    False means the wait expired (or the lock went stale under us), and the
    caller proceeds WITHOUT sweeping — see
    :data:`_BYTECODE_SWEEP_LOCK_WAIT_SECONDS` for why fail-open is the right
    direction here.
    """

    deadline = _time.monotonic() + _BYTECODE_SWEEP_LOCK_WAIT_SECONDS
    while _time.monotonic() < deadline:
        if not lock_path.exists():
            return True
        if _break_stale_bytecode_sweep_lock(lock_path):
            return False
        _time.sleep(0.05)
    return not lock_path.exists()

def _record_bytecode_fingerprint() -> None:
    """Persist the current checkout fingerprint after a bytecode sweep.

    Never raises. A failed write just means the next launch re-sweeps —
    safe, merely redundant.
    """
    from hermes_cli.main import PROJECT_ROOT, _read_git_revision_fingerprint
    try:
        fingerprint = _read_git_revision_fingerprint(PROJECT_ROOT)
        if not fingerprint:
            return
        stamp_path = PROJECT_ROOT / _BYTECODE_FINGERPRINT_FILE
        tmp_path = stamp_path.with_name(stamp_path.name + ".tmp")
        tmp_path.write_text(fingerprint, encoding="utf-8")
        tmp_path.replace(stamp_path)
    except OSError as exc:
        logger.debug("Could not record bytecode fingerprint: %s", exc)

def _log_bytecode_sweep_outcome(
    *,
    outcome: str,
    recorded: str,
    fingerprint: str,
    removed: int,
    started: float,
) -> None:
    """The one purge line, naming what this process actually did.

    Before BW-H2 this line only existed on the sweeping path, so a boot where two
    children contended was indistinguishable from a boot where one child swept
    twice — both looked like "two purge lines". Naming the outcome makes the
    lock's effect readable from the log the operator already has, which is what
    the stage's acceptance check reads.
    """

    logger.info(
        "Checkout changed since last launch (%s -> %s): cleared %d stale "
        "__pycache__ director%s outcome=%s swept_ms=%d",
        recorded or "unknown",
        fingerprint,
        removed,
        "y" if removed == 1 else "ies",
        outcome,
        int(max(0.0, _time.monotonic() - started) * 1000),
    )

def _sweep_stale_bytecode_if_checkout_changed() -> None:
    """Clear ``__pycache__`` at launch when the checkout changed underneath us.

    The stale-bytecode bug class (issues #6207, #60242; Dhruv's WhatsApp
    ``cannot import name 'parse_model_flags_detailed'`` report) has one
    shared shape: the checkout's ``.py`` files change (git pull inside
    ``hermes update``, a manual ``git pull``, a ZIP update, a file-sync
    restore) while ``__pycache__`` retains bytecode from the previous
    revision, and a later process trusts the stale ``.pyc`` instead of the
    fresh source.

    Update-time clears alone can never close this class: ``hermes update``
    always executes the PRE-pull updater code, so any hardening added to it
    only takes effect one update late, and manual ``git pull`` never runs
    the updater at all. This launch-time guard closes the loop: every
    ``hermes`` entry point compares the checkout fingerprint (cheap file
    reads, no git subprocess) against the last-validated stamp and sweeps
    the bytecode cache once when they diverge.

    Never raises — a failure here must not block launch.

    BW-0: the sweep reports its own duration, both on the log line
    (``swept_ms=``) and into ``_boot_clock`` so the serve child's ``booting``
    frame can carry it. The line logged a directory count and no duration at all,
    so the share of the 2026-08-17 boot's 20.4 s import tax owed to this function
    was pure inference. It is now recorded.

    BW-H2: exactly ONE process per checkout change does the work. That boot had
    two children reach this guard 12 ms apart, each delete ~175 ``__pycache__``
    directories, and each then recompile the import set the other had just
    deleted. The loser now waits briefly on the winner's lock and proceeds
    WITHOUT sweeping — see :data:`_BYTECODE_SWEEP_LOCK_WAIT_SECONDS` for why
    fail-open rather than fail-closed. The outcome is named on the log line
    (``outcome=swept`` / ``waited_for_winner`` / ``proceeded_unswept``), because
    "how many purge lines appeared" was never a readable account of a
    multi-child boot.
    """
    from hermes_cli.main import PROJECT_ROOT, _read_git_revision_fingerprint, _clear_bytecode_cache
    _started = _time.monotonic()
    try:
        fingerprint = _read_git_revision_fingerprint(PROJECT_ROOT)
        if not fingerprint:
            return  # non-git install — the ZIP update path clears explicitly
        stamp_path = PROJECT_ROOT / _BYTECODE_FINGERPRINT_FILE
        try:
            recorded = stamp_path.read_text(encoding="utf-8").strip()
        except OSError:
            recorded = ""
        if recorded == fingerprint:
            return
        # The fingerprint check is deliberately OUTSIDE the lock: it is two cheap
        # file reads, and on the overwhelmingly common path (nothing changed) it
        # returns before any process touches the lock at all. Only a genuine
        # divergence contends.
        lock_path = _bytecode_sweep_lock_path()
        claim = _claim_bytecode_sweep_lock(lock_path)
        if claim == _SWEEP_CLAIM_CONTENDED:
            waited = _await_bytecode_sweep_winner(lock_path)
            # Fail-open either way: the winner restamped (so a re-read would
            # return early) or it did not (so this process proceeds on possibly
            # stale bytecode, exactly as every launch did before this guard
            # existed). Not sweeping is the whole point — a second full purge is
            # what BW-H2 exists to remove.
            _log_bytecode_sweep_outcome(
                outcome=(
                    _SWEEP_OUTCOME_WAITED if waited else _SWEEP_OUTCOME_UNSWEPT
                ),
                recorded=recorded,
                fingerprint=fingerprint,
                removed=0,
                started=_started,
            )
            return
        try:
            # ``claimed`` or ``unavailable``. The second case sweeps too, and
            # that is deliberate — see the ``_SWEEP_CLAIM_*`` constants: a
            # filesystem nobody can lock must keep the guard, not lose it.
            removed = _clear_bytecode_cache(PROJECT_ROOT)
            if removed:
                _log_bytecode_sweep_outcome(
                    outcome=_SWEEP_OUTCOME_SWEPT,
                    recorded=recorded,
                    fingerprint=fingerprint,
                    removed=removed,
                    started=_started,
                )
            _record_bytecode_fingerprint()
        finally:
            # Released only after the restamp, so a loser that wakes up on the
            # released lock re-reads a fingerprint that already matches. Only the
            # process that CLAIMED releases — an ``unavailable`` claim holds
            # nothing, and unlinking on its behalf could take out a lock a
            # concurrent winner does hold.
            if claim == _SWEEP_CLAIM_CLAIMED:
                _release_bytecode_sweep_lock(lock_path)
    except Exception as exc:
        logger.debug("Stale-bytecode launch sweep failed: %s", exc)
    finally:
        # Recorded even on the early returns and the exception path: "the sweep
        # decided in 4 ms that it had nothing to do" is exactly as much an
        # answer as "the sweep took 9 s", and a key that appears only on the
        # expensive path would make every cheap boot look unmeasured.
        _boot_clock.record_bytecode_sweep_ms(
            int(max(0.0, _time.monotonic() - _started) * 1000)
        )
