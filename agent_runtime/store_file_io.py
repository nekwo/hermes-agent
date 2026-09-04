"""Typed-outcome plumbing for per-root store files — ONE authority.

Three helpers that four store-file modules (``serve_auth``,
``gateway_identity``, ``gateway_tls``, ``serve_gateway_auth``) each restated
byte-for-byte until ``test_duplicate_helper_bodies`` said so (2026-08-27).
Gateway Stage 6 added a FIFTH importer (``gateway_peers``) and, with it, four
more helpers — the JSON read, the secure write, the cross-process lock and the
UTC stamp — hoisted out of ``serve_gateway_auth`` for exactly the reason the
first three were: the peer store is the device store's sibling, and a sibling
that restated those bodies would be the next group the gate names. Nothing was
rewritten on the way; ``serve_gateway_auth`` keeps its conventional private
names through alias imports, so its call sites and its tests read unchanged.

The rules the bodies carry are load-bearing, so they live here once:

- ``read_raw_text``: read_bytes + decode, never ``read_text`` — the repo's
  standing EOL rule; a record (or token) an operator saved with CRLF must
  still parse.
- ``os_error_reason``: the OSError→reason vocabulary the greeting blocks put
  on the wire (``permission_denied`` / ``root_missing`` /
  ``root_not_a_directory`` / ``unwritable``). Adding a word here changes what
  clients can read — keep it in step with the greeting docs.
- ``narrow_windows_acl``: best-effort DACL narrowing, outcome RETURNED never
  assumed. ``serve_auth.py`` recorded that Windows mode bits are not a
  permission and declined to fix it, on the grounds that the real control
  belongs with the transport slice that introduces the exposure; the gateway
  lane IS that slice, so the narrowing is attempted — ``icacls`` with
  inheritance removed and a single grant to the current user, read/write/DELETE
  since R-D9 because an atomic replace is a delete (see below) — and never
  fatal: a store that could not be narrowed is still a store, and refusing to
  write one would take the lane down over a hardening.

- ``read_json_object``: a store file as a dict, ``{}`` when it is absent,
  empty, or undecodable — and NEVER a raise. A corrupt credential store reads
  as empty, which is the fail-closed direction: an empty store authenticates
  nobody, while an ``OSError`` on the handshake path is a peer that learns
  nothing and a traceback on a stream ``serve_loop`` has redirected onto the
  NDJSON protocol.
- ``write_secure_json``: temp file + atomic replace, ``0600`` where that means
  something, ``icacls`` narrowing where it does not. Atomic because a reader on
  the handshake path must see either the whole old store or the whole new one —
  a half-written store reads as ``{}`` and fails every credential closed until
  the write finishes, which is correct and is the worst kind of bug to chase.
- ``store_lock``: the cross-process read-modify-write lock, over a lock path
  the CALLER supplies. It takes a path rather than a root because
  ``agent_runtime/locks.py`` holds the same OS primitive but resolves its
  directory through ``paths.lock_dir()`` — i.e. it re-derives a root — which is
  what every module in the gateway lane is forbidden to do.
- ``iso_stamp``: one UTC spelling for every stored timestamp, so two stores
  written in the same ceremony cannot disagree about what "now" looks like.

Importers keep their conventional private names via alias imports; the body
lives only here.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

if os.name == "nt":  # pragma: no cover - platform split
    import errno as _errno
    import msvcrt
else:  # pragma: no cover - platform split
    import fcntl


def read_raw_text(path: Path) -> str | None:
    """The file's text, ``None`` when it is absent or holds only whitespace."""

    if not path.is_file():
        return None
    value = path.read_bytes().decode("utf-8", errors="replace").strip()
    return value or None


def os_error_reason(exc: OSError) -> str:
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, FileNotFoundError):
        return "root_missing"
    if isinstance(exc, NotADirectoryError):
        return "root_not_a_directory"
    return "unwritable"


#: The grant ``narrow_windows_acl`` writes. ``D`` is R-D9 and it is not a
#: loosening: the owner of a file can re-grant itself DELETE at any time, so
#: withholding it bought no security and cost the store every write after the
#: first (see :func:`narrow_windows_acl`).
WINDOWS_STORE_GRANT = "(R,W,D)"


def narrow_windows_acl(path: Path) -> str:
    """Best-effort DACL narrowing on Windows; the outcome, never a raise.

    **The grant includes DELETE (R-D9), because replacing a file is deleting
    one.** Until 2026-09-04 it was ``(R,W)``, and that wedged every store this
    lane owns on any volume outside the user profile. ``os.replace`` renames the
    temp over the target, which needs DELETE on the target — or FILE_DELETE_CHILD
    on the directory, which only Full control carries. A profile directory grants
    the user Full control, so every test and every developer machine passed; a
    directory on another volume inherits ``Authenticated Users:(M)``, which has
    neither. Measured 2026-09-04 at ``X:/wt/acl_repro_d4h``: write 0 succeeded,
    the narrowing removed the file's DELETE, write 1 raised ``PermissionError
    [WinError 5]`` — which is why every peer handshake on the operator's PC
    completed and then failed to record its row for a week.

    Granting DELETE gives away nothing. The DACL names the file's OWNER, and an
    owner always holds WRITE_DAC — it could grant itself DELETE in one icacls
    call. The narrowing exists to keep OTHER principals out, and it still does:
    inheritance is removed and this user is the only ACE.
    """

    user = os.environ.get("USERNAME") or ""
    if not user:
        return "skipped:no_username"
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{user}:{WINDOWS_STORE_GRANT}",
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error:{type(exc).__name__}"
    return "narrowed" if completed.returncode == 0 else f"error:rc{completed.returncode}"


def prepare_windows_replace(temp_path: Path, target: Path) -> tuple[str, str]:
    """Give both halves of an ``os.replace`` the DELETE the rename needs.

    A no-op off Windows, and the second half of R-D9. Two files need DELETE for
    one atomic replace and they need it for different reasons:

    * the TARGET, because the rename removes it — and a target an OLDER build
      narrowed to ``(R,W)`` is still on disk with no DELETE on it. Repairing it
      here is what un-wedges a store already in that state, rather than leaving
      it broken forever behind a writer that can no longer touch it;
    * the TEMP, because a rename deletes the source name from its directory.
      Its ACL is whatever the directory handed down, and a directory that grants
      this user only ``(RX,W)`` hands down no DELETE — so the very FIRST write
      fails there, before any narrowing has happened. Measured in the same
      repro's case B.

    Returns the two outcome strings (``narrowed`` / ``skipped:*`` / ``error:*``)
    for a caller that wants to record them; raises nothing, on
    :func:`narrow_windows_acl`'s rule — a store that could not be prepared is
    still worth attempting to write, and the ``os.replace`` that follows reports
    the real verdict.
    """

    if os.name != "nt":
        return ("skipped:not_windows", "skipped:not_windows")
    target_outcome = "skipped:absent"
    try:
        if target.exists():
            target_outcome = narrow_windows_acl(target)
    except OSError:
        target_outcome = "skipped:absent"
    return (narrow_windows_acl(temp_path), target_outcome)


def iso_stamp(now: float | None) -> str:
    when = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(
        float(now), tz=timezone.utc
    )
    return when.isoformat()


def stamp_passed(value: Any, *, now: float | None = None) -> bool:
    """Has the ISO-8601 stamp *value* already gone by? ``False`` when unreadable.

    The reader half of :func:`iso_stamp`, and it lives beside it for the reason
    every derivation in this repo is written once: an expiry WRITTEN by one
    module and READ by another is exactly the pair that drifts — one side
    naive, one aware; one side ``fromisoformat``, one side a substring compare —
    and the failure mode is a credential that expires an hour early on one
    machine and never on the other.

    **Unreadable reads as NOT passed, deliberately.** An absent stamp means "no
    expiry" and must answer ``False``; a MALFORMED one could in principle fail
    the other way, and does not, because the blast radius is asymmetric. Reading
    a broken stamp as expired would refuse every credential in a store one bad
    write corrupted, at the door, with the wire collapsing the reason — an
    operator would see "bad proof" on a phone that is fine. Reading it as live
    leaves a credential working that should have lapsed, which the revocation
    path still answers and an operator can still see in ``devices list``.

    A naive stamp (no offset) is read as UTC, because that is what
    :func:`iso_stamp` writes and the only naive value that could appear here is
    one an editor typed.
    """

    text = str(value or "").strip()
    if not text:
        return False
    try:
        when = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    reference = (
        datetime.now(timezone.utc)
        if now is None
        else datetime.fromtimestamp(float(now), tz=timezone.utc)
    )
    return when <= reference


def read_json_object(path: Path) -> dict[str, Any]:
    """The file as a dict, ``{}`` when absent/empty/undecodable. Never raises.

    A corrupt store reads as EMPTY rather than as an exception, and that is the
    fail-closed direction: an empty credential store authenticates nobody, while
    a raised OSError on the handshake path is a peer that learns nothing and a
    traceback on a stream ``serve_loop`` has redirected onto the NDJSON protocol.
    """

    try:
        if not path.is_file():
            return {}
        # read_bytes + decode, never read_text: the repo's standing EOL rule.
        raw = path.read_bytes().decode("utf-8", errors="replace").strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_secure_json(path: Path, payload: dict[str, Any]) -> None:
    """Temp file + atomic replace, ``0600`` where that is meaningful.

    Atomic because a reader on the handshake path must see either the whole old
    store or the whole new one — a half-written store reads as ``{}`` (see
    :func:`read_json_object`), which fails every credential closed until the
    write finishes. Correct, but a paired device or peer that intermittently
    cannot connect is the worst kind of bug to chase.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, default=str, indent=2) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "wb", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        with handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            # Meaningful here and NOT on Windows, where it only toggles the
            # read-only attribute while reporting success — ``serve_auth.py``'s
            # note, unchanged. The Windows narrowing is ``narrow_windows_acl``.
            try:
                os.chmod(handle.name, 0o600)
            except OSError:
                pass
        # R-D9. On Windows the rename needs DELETE on BOTH names, and neither
        # is guaranteed to have it: see :func:`prepare_windows_replace`. Off
        # Windows this is a no-op and the call costs one `os.name` compare.
        prepare_windows_replace(Path(handle.name), path)
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    if os.name == "nt":
        narrow_windows_acl(path)


@contextlib.contextmanager
def store_lock(path: Path, *, timeout_seconds: float = 10.0) -> Iterator[None]:
    """Serialise read-modify-write across PROCESSES, on the caller's lock path.

    A real concurrency, not a theoretical one: ``harness gateway pair`` runs in
    the operator's shell while the serve process redeems and stamps ``last_seen``
    from its own. ``agent_runtime/locks.py`` holds the same OS primitive but
    resolves its directory through ``paths.lock_dir()`` — i.e. it re-derives a
    root — which is exactly what every module in the gateway lane is forbidden to
    do, so the primitive is restated here over a path the caller supplied.

    Falls through WITHOUT the lock rather than raising if it cannot be taken:
    the alternative is a pairing verb that fails because a lock file is on a
    filesystem that will not lock, and the writes it guards are atomic-replace
    either way, so the loss is a lost update and not a corrupt store.
    """

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+b")
    except OSError:
        yield
        return
    try:
        deadline = time.monotonic() + float(timeout_seconds)
        locked = False
        if os.name == "nt":  # pragma: no cover - platform split
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError as exc:
                    if exc.errno not in {_errno.EACCES, _errno.EDEADLK, 13, 36}:
                        break
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
        else:  # pragma: no cover - platform split
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                locked = True
            except OSError:
                locked = False
        try:
            yield
        finally:
            if locked:
                try:
                    if os.name == "nt":  # pragma: no cover - platform split
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:  # pragma: no cover - platform split
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    finally:
        try:
            handle.close()
        except OSError:
            pass
