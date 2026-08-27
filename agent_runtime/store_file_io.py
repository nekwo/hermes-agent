"""Typed-outcome plumbing for per-root store files — ONE authority.

Three helpers that four store-file modules (``serve_auth``,
``gateway_identity``, ``gateway_tls``, ``serve_gateway_auth``) each restated
byte-for-byte until ``test_duplicate_helper_bodies`` said so (2026-08-27).
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
  inheritance removed and a single grant to the current user — and never
  fatal: a store that could not be narrowed is still a store, and refusing to
  write one would take the lane down over a hardening.

Importers keep their conventional private names via alias imports; the body
lives only here.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


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


def narrow_windows_acl(path: Path) -> str:
    """Best-effort DACL narrowing on Windows; the outcome, never a raise."""

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
                f"{user}:(R,W)",
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error:{type(exc).__name__}"
    return "narrowed" if completed.returncode == 0 else f"error:rc{completed.returncode}"
