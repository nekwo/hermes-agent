"""The secure writer's Windows half: an atomic replace needs DELETE (R-D9).

This file exists because ``store_file_io`` had no test of its own and the one
defect it shipped could only be seen from a directory nobody's test suite ever
used. ``write_secure_json`` narrows each store file with ``icacls
/inheritance:r /grant:r <user>:(R,W)``. On Windows the rename inside
``os.replace`` needs DELETE on the name it removes — or FILE_DELETE_CHILD on the
directory, which only Full control carries. A directory under ``C:/Users/<user>``
grants that user Full control, so every test, on every developer machine, wrote
its second store file and passed. A directory on any other volume inherits
``Authenticated Users:(M)``, which has neither, so the SECOND write raised
``PermissionError [WinError 5]`` — and D3 run #1 (2026-09-04) watched every peer
handshake on the operator's PC complete and then fail to record its row.

So the integration cases below build their directory OUTSIDE the profile, on the
volume this checkout lives on, and strip its inheritance down to what the
operator's store actually had. A test that used ``tmp_path`` would be green
against the bug, which is the whole reason the bug reached hardware.

The second half of the file is R-D27 and is the same shape of story: the
module's OTHER Windows-shaped helper, ``store_lock``, polled to its deadline and
then ran the caller's read-modify-write anyway, while its POSIX arm ignored the
deadline outright and blocked forever. Those cases assert on the refusal and on
the block never running — never on elapsed time alone, which the defect would
have satisfied.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest

from agent_runtime import store_file_io
from agent_runtime.store_file_io import (
    WINDOWS_STORE_GRANT,
    HarnessLockUnavailable,
    narrow_windows_acl,
    prepare_windows_replace,
    store_lock,
    write_secure_json,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

windows_only = pytest.mark.skipif(
    os.name != "nt",
    reason=(
        "R-D9 is a Windows DACL rule: os.replace needs DELETE on the name it "
        "removes, and icacls is the only tool that grants it. There is no "
        "POSIX analogue to assert — the 0600 path is exercised everywhere else "
        "in this suite."
    ),
)


# ── the argv, everywhere ─────────────────────────────────────────────────────


class _Recorder:
    """A ``subprocess`` stand-in that answers success and remembers the argv."""

    SubprocessError = subprocess.SubprocessError

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._returncode = returncode

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self._returncode, b"", b"")


def test_the_narrowing_grants_read_write_and_delete(monkeypatch, tmp_path):
    """R-D9's argv, pinned where every platform can read it.

    The Windows cases below cannot run on the CI machines that would notice a
    silent revert of this string, so the string itself is asserted here."""

    recorder = _Recorder()
    monkeypatch.setenv("USERNAME", "someone")
    monkeypatch.setattr(
        store_file_io,
        "subprocess",
        types.SimpleNamespace(
            run=recorder.run, SubprocessError=subprocess.SubprocessError
        ),
    )

    outcome = narrow_windows_acl(tmp_path / "peers.json")

    assert outcome == "narrowed"
    assert recorder.calls == [
        [
            "icacls",
            str(tmp_path / "peers.json"),
            "/inheritance:r",
            "/grant:r",
            "someone:(R,W,D)",
        ]
    ]
    assert WINDOWS_STORE_GRANT == "(R,W,D)", (
        "the grant is R-D9. Dropping D re-wedges every store on a volume whose "
        "directory ACE is Modify rather than Full control."
    )


def test_a_narrowing_that_fails_is_an_outcome_string_and_never_a_raise(
    monkeypatch, tmp_path
):
    """The file's standing rule, restated for the new argv: a store that could
    not be narrowed is still a store, and refusing to write one would take the
    pairing lane down over a hardening."""

    monkeypatch.setenv("USERNAME", "someone")
    monkeypatch.setattr(
        store_file_io,
        "subprocess",
        types.SimpleNamespace(
            run=_Recorder(returncode=5).run, SubprocessError=subprocess.SubprocessError
        ),
    )

    assert narrow_windows_acl(tmp_path / "peers.json") == "error:rc5"


@pytest.mark.skipif(os.name == "nt", reason="the no-op half is what POSIX sees")
def test_preparing_a_replace_is_a_no_op_off_windows(tmp_path):
    assert prepare_windows_replace(tmp_path / "a.tmp", tmp_path / "a") == (
        "skipped:not_windows",
        "skipped:not_windows",
    )


def test_the_replace_is_prepared_before_it_happens(monkeypatch, tmp_path):
    """Ordering, asserted without a DACL: the helper must run while the OLD
    target is still on disk. Called after ``os.replace`` it would narrow the
    right file and repair nothing, because the write it had to unblock has
    already raised."""

    target = tmp_path / "peers.json"
    write_secure_json(target, {"write": 0})
    seen: list[tuple[bool, str]] = []

    def _spy(temp_path: Path, path: Path):
        seen.append((Path(temp_path).is_file(), path.read_text(encoding="utf-8")))
        return ("skipped:test", "skipped:test")

    monkeypatch.setattr(store_file_io, "prepare_windows_replace", _spy)
    write_secure_json(target, {"write": 1})

    assert len(seen) == 1
    temp_existed, target_text_at_call = seen[0]
    assert temp_existed, "the rename's SOURCE must exist when it is prepared"
    assert json.loads(target_text_at_call) == {"write": 0}, (
        "the target still held the OLD bytes, so the helper ran before the "
        "replace rather than after it"
    )
    assert json.loads(target.read_text(encoding="utf-8")) == {"write": 1}


# ── the volume the operator's store is actually on ───────────────────────────


def _icacls(*argv: str) -> subprocess.CompletedProcess:
    # ``icacls`` is a Windows-native CLI and prints in the console codepage, so
    # the decode is pinned rather than left to ``locale.getpreferredencoding()``.
    return subprocess.run(
        ["icacls", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@pytest.fixture
def hostile_directory(monkeypatch):
    """A directory shaped like ``X:/Eternia/.hermes/agent-runtime/gateway``.

    Under the REPO, never under ``tmp_path``: pytest's temp lands in ``%TEMP%``,
    i.e. inside the user profile, whose Full-control ACE is exactly what hid
    this defect. Inheritance is then stripped and the current user is granted
    ``(RX,W)`` only — no DELETE and no FILE_DELETE_CHILD, the state the
    operator's store was in.

    **USERNAME is re-established here, and that is the second reason this
    defect had no test.** ``scripts/run_tests.sh`` runs every file under
    ``env -i`` and forwards only location variables; USERNAME is not among them.
    ``narrow_windows_acl`` reads USERNAME and returns ``skipped:no_username``
    without it — so under the canonical runner the narrowing never happens, and
    a test written against it would be green because the code under test did
    nothing. The principal is recovered from USERPROFILE (which IS forwarded)
    and put back, so the production writer narrows exactly as it does for an
    operator.

    Torn down through a Full-control re-grant, because a directory that cannot
    delete its own children cannot be removed by the suite that made it.
    """

    profile = os.environ.get("USERPROFILE") or ""
    user = os.environ.get("USERNAME") or (Path(profile).name if profile else "")
    if not user:
        pytest.skip(
            "neither USERNAME nor USERPROFILE names a principal, so there is "
            "nothing for icacls to grant and nothing for the writer to narrow to"
        )
    monkeypatch.setenv("USERNAME", user)
    directory = Path(tempfile.mkdtemp(prefix=".acl-probe-", dir=str(REPO_ROOT)))
    stripped = _icacls(
        str(directory), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)(RX,W)"
    )
    if stripped.returncode != 0:
        shutil.rmtree(directory, ignore_errors=True)
        pytest.skip(f"could not strip inheritance here: {stripped.stdout.strip()}")
    try:
        yield directory
    finally:
        _icacls(str(directory), "/grant", f"{user}:(OI)(CI)(F)", "/T", "/C")
        shutil.rmtree(directory, ignore_errors=True)


def _file_acl(path: Path) -> str:
    return _icacls(str(path)).stdout


@windows_only
def test_three_writes_land_in_a_directory_that_grants_no_delete(hostile_directory):
    """The measured defect, as a test: write 0 passed and write 1 raised.

    Three rather than two because two proves only that the FIRST narrowing did
    not wedge the file — a writer that re-widened the ACL and then re-narrowed
    it wrongly would still fail on the third."""

    target = hostile_directory / "peers.json"

    for index in range(3):
        write_secure_json(target, {"contract": 1, "write": index})

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "contract": 1,
        "write": 2,
    }
    assert WINDOWS_STORE_GRANT in _file_acl(target), (
        "the store file must end each write holding DELETE, or the NEXT write "
        f"is the one that fails. icacls said: {_file_acl(target)!r}"
    )


@windows_only
def test_a_store_an_older_build_wedged_is_repaired_by_its_next_write(
    hostile_directory,
):
    """R-D9's second half. Installs that ran the ``(R,W)`` build have a
    ``peers.json`` on disk with no DELETE on it, and a fix that only changed
    the grant for NEW files would leave every one of them wedged forever —
    the writer could no longer touch the file it had to replace."""

    user = os.environ.get("USERNAME") or ""
    target = hostile_directory / "peers.json"
    target.write_text('{"contract": 1, "peers": {}}\n', encoding="utf-8")
    seeded = _icacls(str(target), "/inheritance:r", "/grant:r", f"{user}:(R,W)")
    assert seeded.returncode == 0, seeded.stdout
    assert "(R,W)" in _file_acl(target) and WINDOWS_STORE_GRANT not in _file_acl(target)

    write_secure_json(target, {"contract": 1, "repaired": True})

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "contract": 1,
        "repaired": True,
    }
    assert WINDOWS_STORE_GRANT in _file_acl(target)


@windows_only
def test_the_narrowing_still_names_exactly_one_principal(hostile_directory):
    """The hardening the grant exists for, unchanged by R-D9: inheritance is
    removed and this user is the only ACE. DELETE is not a loosening — an owner
    holds WRITE_DAC and could grant itself DELETE in one call — but "we widened
    the grant" is the kind of change that quietly acquires a second ACE."""

    target = hostile_directory / "peers.json"
    write_secure_json(target, {"contract": 1})

    # icacls prints the path on the first ACE's line, so the principal is the
    # last whitespace-separated token before ``:(`` — not ``split(":")[0]``,
    # which on this repo's volume would answer "X".
    principals = [
        line.split(":(")[0].split()[-1]
        for line in _file_acl(target).splitlines()
        if ":(" in line and "Successfully processed" not in line
    ]
    assert len(principals) == 1, _file_acl(target)
    assert (os.environ.get("USERNAME") or "").lower() in principals[0].lower()


# ── the lock, on both platforms (R-D27) ──────────────────────────────────────
#
# ``store_lock`` used to be a second copy of ``locks._file_lock`` carrying the
# defect that module was rewritten to remove. Windows polled to the deadline and
# then yielded WITHOUT the lock, so a caller that lost the race did its
# read-modify-write of ``peers.json`` ten seconds late and unsynchronised, and
# nothing — not the store, not the return value, not the wire — could tell that
# write from one that held the lock. POSIX took a bare blocking
# ``flock(fd, LOCK_EX)`` and never reached a deadline at all.
#
# So these assert on the ERROR and on the block never running, never on timing
# alone: a test that only measured elapsed time would have passed against the
# Windows defect, which spent the identical ten seconds and then wrote anyway.

_LOCK_TIMEOUT_SECONDS = 0.5


@contextlib.contextmanager
def _held_in_a_thread(path: Path):
    """Hold ``path``'s lock on ANOTHER thread for the body of the ``with``.

    Another thread rather than another process because the lock has to be real
    on both platforms and a subprocess would need an interpreter start inside
    the half-second budget these tests measure. It contends either way: ``flock``
    conflicts per open file description and ``msvcrt`` per byte-range request,
    so a second acquisition from this same process is exactly the contention a
    second process would raise — which is also the property
    ``locks._file_lock``'s docstring calls out as deliberate.
    """

    acquired = threading.Event()
    release = threading.Event()
    failure: list[BaseException] = []

    def _hold() -> None:
        try:
            with store_lock(path, timeout_seconds=_LOCK_TIMEOUT_SECONDS):
                acquired.set()
                release.wait(30)
        except BaseException as exc:  # pragma: no cover - the fixture failing
            failure.append(exc)
            acquired.set()

    holder = threading.Thread(target=_hold, name="store-lock-holder", daemon=True)
    holder.start()
    try:
        assert acquired.wait(10), "the holder thread never took the lock"
        assert not failure, f"the holder thread could not take the lock: {failure[0]!r}"
        yield
    finally:
        release.set()
        holder.join(10)


def test_a_lock_another_holder_owns_refuses_at_the_deadline(tmp_path):
    """The contract, in one call: refuse by the budget, and never yield unlocked.

    ``ran`` is the assertion that matters. The old Windows arm reached this same
    line after the same wait and set it to ``True``, having decided that a lost
    update was cheaper than a refusal — a trade nobody could audit, because the
    caller had no way to learn which of the two had happened."""

    lock_path = tmp_path / "gateway" / "devices.lock"
    ran = False
    with _held_in_a_thread(lock_path):
        started = time.monotonic()
        with pytest.raises(HarnessLockUnavailable) as refusal:
            with store_lock(lock_path, timeout_seconds=_LOCK_TIMEOUT_SECONDS):
                ran = True  # pragma: no cover - the defect, if it comes back
        elapsed = time.monotonic() - started

    assert not ran, (
        "the block ran while another holder owned the lock — this is the "
        "unsynchronised read-modify-write R-D27 exists to stop"
    )
    assert elapsed < _LOCK_TIMEOUT_SECONDS + 0.5, (
        f"the refusal took {elapsed:.2f}s against a {_LOCK_TIMEOUT_SECONDS}s "
        "budget, so the deadline is not the thing bounding the wait"
    )
    assert str(lock_path) in str(refusal.value), (
        "the refusal names the lock file, which is the only way an operator "
        f"reading the CLI's message learns WHICH store waited: {refusal.value!r}"
    )


def test_the_lock_is_free_again_the_moment_its_holder_leaves(tmp_path):
    """A refusal is not a wedge: the next caller after a release gets in.

    Pinned because the fix moved the release into ``locks._file_lock``'s
    ``finally`` and a lock that refused forever would pass the test above."""

    lock_path = tmp_path / "gateway" / "devices.lock"
    with _held_in_a_thread(lock_path):
        with pytest.raises(HarnessLockUnavailable):
            with store_lock(lock_path, timeout_seconds=_LOCK_TIMEOUT_SECONDS):
                pass  # pragma: no cover - refused above

    ran = False
    with store_lock(lock_path, timeout_seconds=_LOCK_TIMEOUT_SECONDS):
        ran = True
    assert ran, "the lock stayed taken after its holder released it"


posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "this pins the POSIX arm's own defect: it was a bare blocking "
        "flock(fd, LOCK_EX) that read timeout_seconds and ignored it, and "
        "flock conflicts per OPEN FILE DESCRIPTION — so a second acquisition "
        "inside one process did not stall for the budget, it never returned. "
        "Windows reaches the same refusal through msvcrt's byte-range lock and "
        "is pinned by test_the_windows_arm_refuses_rather_than_writing_unlocked."
    ),
)


@posix_only
def test_a_nested_acquire_refuses_instead_of_deadlocking_forever(tmp_path):
    """The Mac's version of the bug, which is worse than the Windows one.

    One thread, one lock, taken twice. Under the old arm this call never
    returned — and the two-machine lane has a Mac in it, so "it stalls for ten
    seconds" was never the whole story. The bound below is the test's own
    watchdog as much as an assertion: it can only be reached at all if the
    deadline is honoured."""

    lock_path = tmp_path / "gateway" / "devices.lock"
    started = time.monotonic()
    with store_lock(lock_path, timeout_seconds=_LOCK_TIMEOUT_SECONDS):
        with pytest.raises(HarnessLockUnavailable):
            with store_lock(lock_path, timeout_seconds=_LOCK_TIMEOUT_SECONDS):
                pass  # pragma: no cover - refused above
    assert time.monotonic() - started < _LOCK_TIMEOUT_SECONDS + 0.5


@windows_only
def test_the_windows_arm_refuses_rather_than_writing_unlocked(tmp_path):
    """The measured defect, as a test: it polled to the deadline and yielded.

    Two claims, and the second is the one the old code failed. That the wait is
    bounded BY the deadline — the elapsed floor proves the poll actually ran to
    it rather than refusing on the first contended attempt — and that what
    follows the wait is a refusal and not the block."""

    lock_path = tmp_path / "gateway" / "devices.lock"
    ran = False
    with _held_in_a_thread(lock_path):
        started = time.monotonic()
        with pytest.raises(HarnessLockUnavailable):
            with store_lock(lock_path, timeout_seconds=_LOCK_TIMEOUT_SECONDS):
                ran = True  # pragma: no cover - the defect, if it comes back
        elapsed = time.monotonic() - started

    assert not ran
    assert elapsed >= _LOCK_TIMEOUT_SECONDS * 0.9, (
        f"the refusal came after {elapsed:.2f}s, well inside the "
        f"{_LOCK_TIMEOUT_SECONDS}s budget — a contended lock must be POLLED to "
        "the deadline, because the holder usually leaves before it expires"
    )
    assert elapsed < _LOCK_TIMEOUT_SECONDS + 0.5


# ── the caller's half: the refusal reaches the operator's vocabulary ──────────


def test_record_peer_refuses_the_unwritable_family_rather_than_writing_unlocked(
    tmp_path, monkeypatch
):
    """R-D27 through a real caller, to the word the CLI puts on the wire.

    ``record_peer`` is the door D3 run #1 watched fail — the handshake completed
    and the row was never recorded — so it is the one to prove twice over: the
    refusal is typed (a ``StoreRefusal``, not a traceback out of a verb), its
    reason is in the family D4h routes to ``store_unwritable``, and
    ``peers.json`` is still ABSENT. The last is the point. The old lock let this
    call write the file ten seconds late with another holder believing it had
    exclusive access, and a test that only checked the return value would have
    been just as green against that.

    The budget is shortened through the module's own binding rather than faked:
    the lock, the contention and the raise are all real, and only the ten
    seconds a test cannot afford to wait are not."""

    from agent_runtime import gateway_peers
    from agent_runtime.gateway_identity import gateway_dir
    from agent_runtime.gateway_peers import PeerRecord, peer_store_path

    real_store_lock = gateway_peers._store_lock
    monkeypatch.setattr(
        gateway_peers,
        "_store_lock",
        lambda root, **kwargs: real_store_lock(
            root, timeout_seconds=_LOCK_TIMEOUT_SECONDS
        ),
    )

    with _held_in_a_thread(gateway_dir(tmp_path) / "devices.lock"):
        refusal = gateway_peers.record_peer(
            tmp_path,
            peer_install_id="inst_a1b2c3d4",
            secret="f" * 64,
            display_name="workstation",
        )

    assert not isinstance(refusal, PeerRecord), (
        "the write went through while another holder had the lock"
    )
    assert not peer_store_path(tmp_path).exists(), (
        "peers.json was written by a call that never held the lock"
    )
    assert refusal.reason == "unwritable"

    from hermes_cli.harness_parts.gateway_commands import (
        _REFUSAL_CODES,
        _STORE_WRITE_REASONS,
    )

    assert refusal.reason in _STORE_WRITE_REASONS
    assert _REFUSAL_CODES[refusal.reason] == "store_unwritable", (
        "a lock this machine could not take is a store this machine could not "
        "write (family 1), never the network word the sheet used to print"
    )
