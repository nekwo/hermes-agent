"""Per-root shared secret for the future serve transport. Minted at boot; not
yet wired to anything.

Why this lands BEFORE the socket
--------------------------------

Today ``harness serve`` speaks NDJSON over an INHERITED stdio pipe. That pipe
is effectively unforgeable: only the process the Launcher spawned holds the
descriptors, so the transport IS the authentication, and the serve design doc
says exactly that ("no auth — a local stdio child IS the security model").

The durable runtime-root service replaces that pipe with a named pipe or a
localhost socket, which any local process can reach — and this runtime executes
agents with tools. The security envelope therefore changes at the same moment
the transport does, and a token minted after the socket exists is a window with
no lock in it. So the secret is minted, stored, and readable NOW, per runtime
root, and the socket slice consumes it on day one instead of shipping open and
retrofitting.

Contract
--------

* **The root is an INPUT.** Every function takes ``store_root``; this module
  never resolves it, never reads ``HERMES_HOME``. Multiple runtime roots
  legitimately coexist on this machine (QA lanes, isolated worktree roots) and
  each gets its own token — a module that re-derived the root would be free to
  mint into a different one than its caller is serving from, silently.
* **Mint iff absent.** An existing token file is never rewritten: reconnecting
  clients hold the value, and rotating it under them is a lockout, not a
  hardening. Rotation, when it lands, will be an explicit verb.
* **The value never leaves this module.** It appears in no frame, no log line,
  no event, no status block. Callers get a typed ``ServeAuthStatus`` whose
  ``token_file`` field is ``present`` / ``minted`` / ``error:<reason>`` — the
  posture, not the secret.
* **Never raises.** A runtime that cannot mint a token must still boot and say
  so; the typed error state is the observability.

File permissions, honestly
--------------------------

On POSIX the file is created ``0600`` and that means what it says.

**On Windows it does not.** ``os.chmod``/``O_CREAT`` mode bits map only onto
the read-only attribute; the file inherits its parent directory's ACL, so on a
default install the token is readable by any process running as this user (and
by anything with admin). This module does NOT pretend otherwise — it neither
chmods on Windows nor reports a permission posture it cannot enforce. The real
Windows control is an explicit DACL on the file (``icacls`` / pywin32) or, more
appropriately, an ACL on the named pipe itself, and it belongs with the
transport slice that introduces the exposure. Recorded here rather than fixed
here so the socket slice starts from a known gap instead of a false all-clear.
"""

from __future__ import annotations

import hmac
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "SERVE_AUTH_TOKEN_FILENAME",
    "TOKEN_BYTES",
    "ServeAuthStatus",
    "ensure_token",
    "read_token",
    "serve_auth_token_path",
    "verify",
]

#: Lives beside the other per-root runtime state, under ``<store_root>``.
#:
#: MUST NOT be added to any freshness fingerprint (serve's
#: ``_FINGERPRINT_ROOT_FILES``/``_FINGERPRINT_STORE_DIRS``, or
#: ``stream._scope_fingerprint``): the file APPEARS at first boot, which inside
#: a fingerprint would cold the read-model cache exactly when a fresh runtime
#: is warming up. Same rationale as ``dispatch_delivery.DRAIN_STATE_FILENAME``
#: and the per-session turn store's documented exclusion.
SERVE_AUTH_TOKEN_FILENAME = "serve_auth_token"

#: 256 bits, hex-encoded. Not configurable: a knob here can only ever be turned
#: down.
TOKEN_BYTES = 32

STATE_PRESENT = "present"
STATE_MINTED = "minted"


@dataclass(frozen=True, slots=True)
class ServeAuthStatus:
    """What happened to the token file — never the token."""

    #: ``present`` | ``minted`` | ``error:<reason>``
    state: str
    #: Where it lives, so an operator can inspect/rotate by hand.
    path: str

    @property
    def ok(self) -> bool:
        return self.state in (STATE_PRESENT, STATE_MINTED)

    def payload(self) -> dict[str, Any]:
        """The ``auth`` block on the serve ``ready`` frame."""

        return {"token_file": self.state}


def serve_auth_token_path(store_root: Path | str) -> Path:
    return Path(store_root) / SERVE_AUTH_TOKEN_FILENAME


def ensure_token(store_root: Path | str) -> ServeAuthStatus:
    """Mint the per-root token if absent; report the posture either way."""

    path = serve_auth_token_path(store_root)
    try:
        existing = _read_raw(path)
    except OSError:
        return ServeAuthStatus(state="error:unreadable", path=str(path))
    if existing:
        return ServeAuthStatus(state=STATE_PRESENT, path=str(path))
    try:
        _mint(path)
    except FileExistsError:
        # Another serve for this root won the race; its value is as good as
        # ours would have been, and rewriting it would lock out its clients.
        # Unless the file that beat us is EMPTY — a process killed between the
        # O_EXCL create and the write — in which case there is no secret to
        # protect: nobody can hold the value of an empty file, so replacing it
        # locks nobody out. Reporting ``present`` would claim a token that does
        # not exist, and LEAVING it wedges the root permanently — mint-iff-
        # absent means every later boot takes this same branch and every
        # ``verify`` fails closed against a token nobody holds. So heal it.
        try:
            if _read_raw(path):
                return ServeAuthStatus(state=STATE_PRESENT, path=str(path))
        except OSError:
            return ServeAuthStatus(state="error:unreadable", path=str(path))
        if _heal_empty(path) is None:
            # Still nothing readable — the typed wedge state is kept for the
            # boot that observes it mid-heal, and for a root we cannot write.
            return ServeAuthStatus(state="error:empty_token_file", path=str(path))
        return ServeAuthStatus(state=STATE_MINTED, path=str(path))
    except OSError as exc:
        return ServeAuthStatus(
            state=f"error:{_error_token(exc)}", path=str(path)
        )
    except Exception:  # pragma: no cover - defensive; boot must not fail here
        return ServeAuthStatus(state="error:mint_failed", path=str(path))
    return ServeAuthStatus(state=STATE_MINTED, path=str(path))


def read_token(store_root: Path | str) -> str | None:
    """The token for *store_root*, or None when absent/unreadable/empty.

    None is deliberately not distinguished from "wrong": callers authenticate
    with :func:`verify`, which fails closed when there is nothing to compare
    against.
    """

    try:
        return _read_raw(serve_auth_token_path(store_root))
    except OSError:
        return None


def verify(presented: str | None, store_root: Path | str) -> bool:
    """Constant-time comparison of *presented* against the root's token.

    Fails CLOSED: an absent or unreadable token file rejects every caller
    rather than degrading to "no auth configured, let everyone in" — which is
    the failure mode that turns a hardening into a bypass.
    """

    expected = read_token(store_root)
    if not expected or not presented:
        return False
    # BYTES, never str: `hmac.compare_digest` RAISES TypeError when either str
    # operand is non-ASCII, so a hostile value turns a rejection into an
    # unhandled exception on the caller's thread. The socket lane hit exactly
    # that (see `serve_socket.verify_hello_proof`). This comparison has no
    # production caller today, and a latent copy of the same hazard is how it
    # comes back the day someone revives it.
    return hmac.compare_digest(
        str(presented).encode("utf-8", "replace"),
        expected.encode("utf-8", "replace"),
    )


def _read_raw(path: Path) -> str | None:
    if not path.is_file():
        return None
    # read_bytes + decode, never read_text: the repo's standing EOL rule, and a
    # token must survive a file someone saved with CRLF.
    value = path.read_bytes().decode("utf-8", errors="replace").strip()
    return value or None


def _mint(path: Path) -> None:
    """Create the token file exclusively, 0600 where that is meaningful."""

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(TOKEN_BYTES)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    # O_EXCL, not a write-if-missing: two serves booting against one root is a
    # real concurrency (that is the discovery slice's whole premise), and the
    # loser must adopt the winner's token rather than overwrite it.
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    if os.name != "nt":
        # Windows deliberately skipped — see the module docstring. chmod there
        # would only toggle the read-only attribute while reporting success,
        # which is worse than doing nothing openly.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _heal_empty(path: Path) -> str | None:
    """Replace a ZERO-BYTE token file with a fresh mint; return what won.

    Only ever called for a file already observed empty, and that precondition
    is what makes replacing it safe: an empty file's value is held by nobody,
    so this cannot lock out a reconnecting client the way rotating a real token
    would.

    The heal is itself concurrent-safe without a lock. Two healers write two
    distinct O_EXCL temp files and ``os.replace`` them in turn; the second
    rename wins the file, and BOTH then read the file back and return whatever
    is actually there. So neither reports a token it does not hold — the same
    read-after-write discipline the root anchor's post-write verification uses,
    for the same reason: a rename is atomic but not exclusive.

    Returns ``None`` when the file still has no readable value afterwards.
    """

    token = secrets.token_hex(TOKEN_BYTES)
    handle = None
    try:
        handle = tempfile.NamedTemporaryFile(
            "wb",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        )
        with handle:
            handle.write((token + "\n").encode("utf-8"))
        if os.name != "nt":
            # Same honesty as ``_mint``: chmod means something here and
            # nothing on Windows (see the module docstring).
            try:
                os.chmod(handle.name, 0o600)
            except OSError:
                pass
        os.replace(handle.name, path)
    except Exception:
        if handle is not None:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
        # Fall through to the read: another healer may have fixed it for us.
    try:
        return _read_raw(path)
    except OSError:
        return None


def _error_token(exc: OSError) -> str:
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, FileNotFoundError):
        return "root_missing"
    if isinstance(exc, NotADirectoryError):
        return "root_not_a_directory"
    return "unwritable"
