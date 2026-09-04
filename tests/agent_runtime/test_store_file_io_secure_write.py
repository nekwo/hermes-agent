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
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import types
from pathlib import Path

import pytest

from agent_runtime import store_file_io
from agent_runtime.store_file_io import (
    WINDOWS_STORE_GRANT,
    narrow_windows_acl,
    prepare_windows_replace,
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
