"""Local diff artifacts for patch tool calls.

A patch call already knows exactly what it changed — ``PatchResult.diff`` is a
real unified diff and it rides the tool result all the way to the trace
producer. What it could never do was reach an operator: the diff body is
precisely the kind of content the observability lanes withhold (machine
paths, file contents, whatever a secret-bearing file happened to contain), and
the 4KB event cap could not hold one anyway.

So the body stays HERE, on the machine that produced it, and the trace carries
its PATH plus two integers. The console renders ``+N``/``−N`` inline and opens
the file locally when the operator asks for it; a console on another machine
resolves nothing and says so. Nothing about a diff is ever shipped.

Written at the agent_runtime trace boundary only (never in ``tools/``, which is
upstream-shared surface and runs in contexts that have no store root at all), so
an artifact is a runtime-observability behavior rather than a global side effect
of the tool.

Retention mirrors :mod:`agent_runtime.prompt_observability`'s shape exactly:
newest :data:`PATCH_DIFF_RETAIN` stay live, older ones MOVE to the archive dir
(archive-never-delete), the archive side is unbounded, and a move that fails
leaves the file live to be retried by the next write. The one difference is the
lane key: an observability row belongs to an (instance, session) lane and needs
an index to find its siblings; a diff's filename IS its sort key, so the live
directory listing is the whole ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths

#: Per-diff ceiling. A diff over this is truncated at a line boundary with
#: :data:`PATCH_DIFF_TRUNCATION_MARKER` appended — never skipped, because an
#: operator staring at a 900KB refactor is exactly who needs to read the first
#: 256KB of it. The COUNTS are computed from the full diff before truncation,
#: so the numbers on the tile stay honest about what actually changed.
PATCH_DIFF_MAX_BYTES = 256 * 1024

#: Live-dir bound. Older artifacts move to :func:`paths.patch_diffs_archive_dir`.
PATCH_DIFF_RETAIN = 400

#: Appended as its own final line to a truncated artifact. The viewer renders it
#: muted; its job is to stop a reader concluding the patch ended where the file
#: does.
PATCH_DIFF_TRUNCATION_MARKER = "…(diff truncated)…"

#: Filename shape: a UTC timestamp (so a lexical filename sort IS a recency
#: sort, which is what retention walks) plus a content hash (so a re-emitted
#: identical diff targets the same file instead of accumulating duplicates).
_FILENAME_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_CONTENT_HASH_CHARS = 12


def _utc_now() -> datetime:
    """Seam for tests: pinning this makes the content-hash idempotency of a
    repeated write observable without racing the second boundary."""

    return datetime.now(timezone.utc)


def result_diff(result: Any) -> str | None:
    """The unified diff carried by a patch tool result, or ``None``.

    Accepts BOTH shapes the result reaches the runner in: the parsed dict, and
    the JSON string ``patch_tool`` actually returns (``json.dumps`` of the
    result dict). Which one arrives is lane-dependent, and a lane that hands us
    something else entirely — a truncated string past the tool's result cap, a
    plain error message, ``None`` — yields no artifact rather than an error.
    """

    payload: Any = result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    if not isinstance(payload, dict):
        return None
    diff = payload.get("diff")
    if not isinstance(diff, str) or not diff.strip():
        return None
    return diff


def diff_counts(diff: str) -> tuple[int, int]:
    """``(added, deleted)`` line counts for a unified diff.

    ``+++``/``---`` are file headers, not content, so they are excluded. A V4A
    multi-file patch concatenates per-file diffs; the counts are the totals
    across all of them, which is the grade the tile's one-line summary reads at.
    """

    adds = 0
    dels = 0
    for line in diff.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            adds += 1
        elif line.startswith("-") and not line.startswith("---"):
            dels += 1
    return adds, dels


def _bounded_diff(diff: str) -> str:
    """The bytes to write: *diff* unchanged, or its first whole lines up to
    :data:`PATCH_DIFF_MAX_BYTES` with the truncation marker as a final line."""

    text = diff.replace("\r\n", "\n").replace("\r", "\n")
    if len(text.encode("utf-8", "replace")) <= PATCH_DIFF_MAX_BYTES:
        return text
    marker = f"{PATCH_DIFF_TRUNCATION_MARKER}\n"
    budget = PATCH_DIFF_MAX_BYTES - len(marker.encode("utf-8"))
    kept: list[str] = []
    used = 0
    for line in text.split("\n"):
        cost = len(line.encode("utf-8", "replace")) + 1
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    return "".join(f"{line}\n" for line in kept) + marker


def _artifact_name(diff: str) -> str:
    digest = hashlib.sha256(diff.encode("utf-8", "replace")).hexdigest()
    stamp = _utc_now().strftime(_FILENAME_TIMESTAMP_FORMAT)
    return f"{stamp}_{digest[:_CONTENT_HASH_CHARS]}.diff"


def _retain_after_write(root: Path) -> None:
    """Bound the live dir, one owner (this runs only at write time).

    Straight from ``prompt_observability._index_and_retain_after_persist``:
    eviction MOVES rather than deletes, the archive is created lazily and left
    unbounded, and an ``OSError`` on a move leaves that file LIVE — losing track
    of an artifact is worse than an over-full directory, and the next write
    retries it.
    """

    try:
        names = sorted(
            (item.name for item in root.iterdir() if item.suffix == ".diff"),
            reverse=True,
        )
    except OSError:
        return
    archive_dir = paths.patch_diffs_archive_dir()
    for stale in names[PATCH_DIFF_RETAIN:]:
        source = root / stale
        try:
            if source.exists():
                archive_dir.mkdir(parents=True, exist_ok=True)
                os.replace(source, archive_dir / stale)
        except OSError:
            continue


def record_patch_diff(result: Any) -> dict[str, Any] | None:
    """Persist the patch's diff locally; return the trace fields that name it.

    Returns ``{"patch_artifact": <abs path>, "patch_adds": N, "patch_dels": N}``
    or ``None``. ``None`` is the honest answer for every degraded case — no diff
    in the result (a failed or no-op patch), a result shape we cannot read, a
    store that will not take a write — and the tile simply renders without the
    affordance. The whole function is bounded: observability must never be able
    to break a turn, which is the same doctrine
    :class:`agent_runtime.progress.ChatProgressSink` runs on.
    """

    try:
        diff = result_diff(result)
        if diff is None:
            return None
        adds, dels = diff_counts(diff)
        root = paths.patch_diffs_dir()
        root.mkdir(parents=True, exist_ok=True)
        target = root / _artifact_name(diff)
        from utils import atomic_write_text

        # newline="" keeps the artifact LF on every platform: the line ending is
        # part of a unified diff's grammar, not the host's convention.
        atomic_write_text(target, _bounded_diff(diff), newline="")
        _retain_after_write(root)
        return {
            "patch_artifact": str(target),
            "patch_adds": adds,
            "patch_dels": dels,
        }
    except Exception:
        return None
