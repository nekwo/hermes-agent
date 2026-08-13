"""Which CODE is this process running? — the runtime's self-reported build stamp.

Why this exists
---------------

The live hermes runtime is an EDITABLE INSTALL of the primary checkout: today
every launcher restart spawns a fresh ``harness serve`` child, so landed fixes
are picked up for free and nobody has ever had to ask what code a runtime is
on. The durable runtime-root service retires that accident — a service that
survives launcher restarts silently keeps running last week's code.

This repo already knows what a week-stale capability looks like. The
``agent_chat_send(wait=false)`` dead-flag-proxy incident ran green for a week
while the capability was gated on a cache flag nobody had set, and the `.pyd`
lock that forced an awaited serve shutdown is the same shape: a process
outliving the assumption that it matches the checkout. So the FIRST slice of
the service migration ships the handshake — the runtime stamps its own build,
a client compares it against the install, and "the service is stale" becomes a
measurement instead of a theory.

Contract
--------

* **Never raises.** No repo, no ``git`` on PATH, a hung ``git`` — every one of
  those yields a typed *unknown* stamp carrying the reason. A stamp is
  emitted on the boot frame of a process whose job is to come up; an
  instrument may not be why it does not.
* **Absent means NOT MEASURED.** ``commit``/``dirty`` are ``None`` when they
  could not be resolved — never ``""``, never ``"unknown"``, never
  ``False``-by-default. A fabricated value here would be indistinguishable
  from a real one, which is precisely the false-all-clear class this whole
  workstream exists to retire (a wrong root answers ``ok: true, count: 0``;
  a fabricated ``dirty: false`` answers "you are current" for a runtime that
  is not).
* **Resolved once per process**, behind a lock, and cached: the stamp is a
  property of the code this interpreter LOADED, and re-measuring it per
  request would answer for the checkout as it is now — a different question,
  and one that costs two subprocesses to ask.

The process facts (``pid``, ``boot_at``, ``uptime_ms``) are deliberately NOT
cached: uptime is computed from a monotonic baseline captured at import, so a
wall-clock adjustment (NTP step, DST, a VM resume) cannot make a live service
look like it booted in the future or ran for negative time.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "GIT_TIMEOUT_SECONDS",
    "SOURCE_BUILD_SHA_FILE",
    "SOURCE_GIT",
    "SOURCE_UNKNOWN",
    "BuildStamp",
    "build_stamp",
    "reset_build_stamp_cache",
]

#: Hard bound on either git probe. A stamp is boot-path work; a git that hangs
#: on a network-mounted or locked repo must cost the boot two seconds, not the
#: boot itself.
GIT_TIMEOUT_SECONDS = 2.0

#: The commit came from a live ``git`` in the checkout this module lives in.
SOURCE_GIT = "git"
#: The commit came from ``<repo>/.hermes_build_sha`` — the Docker image path,
#: where ``.dockerignore`` excludes ``.git`` entirely (see
#: ``hermes_cli/build_info.py``). Dirty state is unknowable there and stays None.
SOURCE_BUILD_SHA_FILE = "build_sha_file"
#: Nothing could be measured. ``reason`` says what was tried.
SOURCE_UNKNOWN = "unknown"

_BUILD_SHA_FILENAME = ".hermes_build_sha"

# Captured at import — as close to process start as this module can observe.
# The pair is the point: the epoch value is what a human reads, the monotonic
# value is what uptime is computed FROM, so uptime survives clock steps.
_BOOT_EPOCH = time.time()
_BOOT_MONOTONIC = time.monotonic()

_cache_lock = threading.Lock()
_cached_stamp: "BuildStamp | None" = None


@dataclass(frozen=True, slots=True)
class BuildStamp:
    """What code this process is running, and how confidently we know it."""

    #: Full 40-char commit hash, or None when it could not be measured.
    commit: str | None
    #: True when the checkout has ANY tracked modification. None when the
    #: question could not be answered (no git, timeout, image build).
    dirty: bool | None
    #: One of SOURCE_GIT / SOURCE_BUILD_SHA_FILE / SOURCE_UNKNOWN.
    source: str
    #: Empty when fully resolved; otherwise a typed token naming what failed
    #: (``no_repo_root``, ``git_missing``, ``git_timeout``, ``git_failed``,
    #: ``dirty_unknown:<token>``…).
    reason: str
    #: The checkout the answer is about, or None when none was found.
    repo_root: str | None
    #: When the resolution ran (UTC ISO-8601, ``Z``).
    resolved_at: str

    @property
    def resolved(self) -> bool:
        return self.commit is not None

    def frame_payload(self) -> dict[str, Any]:
        """The ``build`` block on the serve ``ready`` frame.

        Deliberately the four keys a client needs to answer "is this service
        running my install?" — the rest is available from the ``version`` op
        at any time, so the boot frame does not carry a report nobody reads.
        """

        return {
            "commit": self.commit,
            "dirty": self.dirty,
            "source": self.source,
            "resolved_at": self.resolved_at,
        }

    def payload(self) -> dict[str, Any]:
        """The full stamp: the frame block plus provenance and process facts."""

        return {
            **self.frame_payload(),
            "reason": self.reason,
            "repo_root": self.repo_root,
            "commit_short": self.commit[:8] if self.commit else None,
            "pid": os.getpid(),
            "boot_at": _iso(_BOOT_EPOCH),
            "uptime_ms": int((time.monotonic() - _BOOT_MONOTONIC) * 1000),
        }


def build_stamp(*, refresh: bool = False) -> BuildStamp:
    """This process's build stamp, resolved once and cached.

    ``refresh=True`` re-measures — for tests, and for a future explicit
    operator verb. Nothing on the request path should pass it: the stamp
    answers "what code did this interpreter load", which cannot change while
    the process lives.
    """

    global _cached_stamp
    with _cache_lock:
        if _cached_stamp is not None and not refresh:
            return _cached_stamp
        stamp = _resolve()
        _cached_stamp = stamp
        return stamp


def reset_build_stamp_cache() -> None:
    """Drop the memo. Tests only — a live process's stamp does not change."""

    global _cached_stamp
    with _cache_lock:
        _cached_stamp = None


def repo_root_for(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default: this module) to the checkout root.

    ``.git`` is matched with ``exists()``, not ``is_dir()``: in a linked
    worktree — which is how every agent in this stack is supposed to work —
    ``.git`` is a FILE pointing at the common dir, and an ``is_dir()`` probe
    would report "no repo" for exactly the checkouts most likely to be running
    unlanded code.
    """

    try:
        current = Path(start if start is not None else __file__).resolve()
    except OSError:  # pragma: no cover - defensive
        return None
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _resolve() -> BuildStamp:
    resolved_at = _iso(time.time())
    try:
        root = repo_root_for()
    except Exception:  # pragma: no cover - defensive; must never raise
        root = None

    if root is None:
        # No checkout above this module. A Docker image is the expected case
        # (``.dockerignore`` drops ``.git``), so try the baked sha before
        # declaring the build unmeasurable.
        commit = _baked_sha(_fallback_repo_root())
        if commit:
            return BuildStamp(
                commit=commit,
                dirty=None,
                source=SOURCE_BUILD_SHA_FILE,
                reason="no_repo_root",
                repo_root=str(_fallback_repo_root() or ""),
                resolved_at=resolved_at,
            )
        return BuildStamp(
            commit=None,
            dirty=None,
            source=SOURCE_UNKNOWN,
            reason="no_repo_root",
            repo_root=None,
            resolved_at=resolved_at,
        )

    commit, commit_reason = _git(root, ["rev-parse", "HEAD"])
    if commit is None:
        baked = _baked_sha(root)
        if baked:
            return BuildStamp(
                commit=baked,
                dirty=None,
                source=SOURCE_BUILD_SHA_FILE,
                reason=commit_reason,
                repo_root=str(root),
                resolved_at=resolved_at,
            )
        return BuildStamp(
            commit=None,
            dirty=None,
            source=SOURCE_UNKNOWN,
            reason=commit_reason,
            repo_root=str(root),
            resolved_at=resolved_at,
        )

    # ``--no-optional-locks`` + ``-uno``: a status probe on the boot path must
    # not take the index lock (it would race a human's git in the same
    # checkout), and untracked files are not "this runtime is running edited
    # code" — tracked modifications are.
    status, status_reason = _git(
        root, ["--no-optional-locks", "status", "--porcelain", "-uno"], allow_empty=True
    )
    if status is None:
        return BuildStamp(
            commit=commit,
            dirty=None,
            source=SOURCE_GIT,
            reason=f"dirty_unknown:{status_reason}",
            repo_root=str(root),
            resolved_at=resolved_at,
        )
    return BuildStamp(
        commit=commit,
        dirty=bool(status.strip()),
        source=SOURCE_GIT,
        reason="",
        repo_root=str(root),
        resolved_at=resolved_at,
    )


def _git(root: Path, args: list[str], *, allow_empty: bool = False) -> tuple[str | None, str]:
    """Run one git command in *root*. Returns ``(stdout, reason)``.

    ``stdout`` is None on every failure; ``reason`` is a typed token, never a
    raw error string (which would put machine-local paths on a boot frame).
    ``allow_empty`` distinguishes "git answered nothing" (a clean ``status``)
    from "git did not answer" — without it a clean checkout is indistinguishable
    from a failed probe, and would be reported as ``dirty: None``.
    """

    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            # stdin pinned to the null device: serve's handlers already learned
            # this the hard way — a child inheriting serve's stdin pipe blocks
            # forever against the Launcher's open pipe (see
            # ``_claim_protocol_pipes`` in harness_parts/serve.py).
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
        )
    except FileNotFoundError:
        return None, "git_missing"
    except subprocess.TimeoutExpired:
        return None, "git_timeout"
    except Exception:
        return None, "git_error"
    if completed.returncode != 0:
        return None, "git_failed"
    out = (completed.stdout or "").strip()
    if not out and not allow_empty:
        return None, "git_empty"
    return out, ""


def _fallback_repo_root() -> Path | None:
    """``<project root>`` derived structurally, for the no-``.git`` image case."""

    try:
        return Path(__file__).resolve().parents[1]
    except Exception:  # pragma: no cover - defensive
        return None


def _baked_sha(root: Path | None) -> str | None:
    """``<repo>/.hermes_build_sha``, the Docker build's recorded commit."""

    if root is None:
        return None
    try:
        path = root / _BUILD_SHA_FILENAME
        if not path.is_file():
            return None
        # read_bytes + decode, never read_text: the repo's standing EOL rule.
        sha = path.read_bytes().decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    return sha or None


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
