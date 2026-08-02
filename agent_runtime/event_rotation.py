"""Size-gated, offset-safe rotation of the append-only event log (Stage C6a).

The event log (``events.jsonl``) is append-only and, historically, never
rotated — the live root reached 81 MB / 130k lines. But a naive rotate breaks
the platform: **byte-offset tailing is load-bearing.** ``EventLog.iter_from_offset``
resumes a streaming tailer from a byte cursor — today that is ``stream.py``
alone. (This list used to read ``stream.py``, ``projector.py``, ``liveness.py``,
``child_events.py``. S46 removed ``projector.py`` with the incremental
projection lane and re-checked the rest while it was here: ``liveness.py`` no
longer exists and ``child_events.py`` does not tail by offset at all, so both
were already stale.) The checkpoint watermark keys on
``event_offset`` (``checkpoint.py`` / ``parity.events_watermark``), which is the
log's total byte size. Renaming or truncating the live file resets that cursor
to zero and silently drops or duplicates every event.

This module preserves offset semantics with **monotonic logical offsets that
span slices** (design (i) of the C6 plan). The current byte offset is already
monotonic; a rotation just records where one slice ends and the next begins.

    logical_offset(x) = slice.start_offset + byte_position_within(slice)

A manifest lists every slice as ``(file, start_offset, end_offset)`` plus the
open-ended live slice ``(file, base_offset)``. ``iter_from_offset(L)`` resolves
which slice ``L`` lives in and seeks there; the yielded offsets stay logical, so
**every existing reader keeps working unmodified** and stored watermarks resolve
exactly as before. When no rotation has occurred the manifest is absent, the
single live slice is ``events.jsonl`` at base 0, and ``logical == byte offset in
events.jsonl`` — byte-for-byte identical to the pre-rotation world.

**Windows-safe rotation (the load-bearing implementation detail).** A concurrent
reader may hold the live file open — a paused ``iter_from_offset`` generator does
so across yields. On Windows, ``os.replace``/rename of a file with an open handle
raises ``PermissionError`` (WinError 32), and truncating a file out from under a
reader corrupts its in-flight reads. So rotation here **never renames, truncates,
or replaces the live file.** It *seals it in place* — the sealed file keeps its
existing path, recorded verbatim in the manifest — and directs new appends to a
freshly created live file. A reader mid-iteration keeps reading the now-sealed
file to its end (its handle is untouched); its next ``iter_from_offset`` call
re-resolves the manifest and continues into the new live slice. No gap, no
duplicate, no corruption, no PermissionError.

Archive-never-delete: sealed slices live under ``events_archive/`` (distinct from
``deleted_archive/``, which holds per-task compaction batches) and are immutable
and offset-load-bearing — nothing rewrites them.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths

MANIFEST_VERSION = 1

# Default live-slice cap. Configurable via ``event_log.rotation_cap_bytes``
# (agent_runtime config) or the ``HERMES_EVENT_LOG_ROTATION_CAP_BYTES`` env
# override; no config is required for this default.
DEFAULT_ROTATION_CAP_BYTES = 16 * 1024 * 1024

# Manifest write is atomic (tmp + os.replace). The manifest is small and read by
# open/read/close, so a reader could momentarily hold it open when a rotation
# replaces it — bounded retry absorbs the rare Windows collision.
_MANIFEST_REPLACE_RETRIES = 40
_MANIFEST_REPLACE_SLEEP_SECONDS = 0.005


@dataclass(frozen=True)
class SliceRef:
    """One event-log slice.

    ``start_offset`` is the logical offset of the slice's byte 0. ``end_offset``
    is the exclusive logical end for a sealed slice, or ``None`` for the
    open-ended live slice. ``rel`` is the slice's store-root-relative POSIX path
    (its manifest form); ``path`` is the absolute path.
    """

    path: Path
    rel: str
    start_offset: int
    end_offset: int | None
    live: bool


def _store_root() -> Path:
    return paths.store_root()


def _abs(rel: str) -> Path:
    # rel is a manifest-recorded store-root-relative posix path.
    return _store_root() / rel


def manifest_path() -> Path:
    return paths.events_manifest_path()


def archive_dir() -> Path:
    return paths.events_archive_dir()


def _default_live() -> SliceRef:
    """The pristine live slice: ``events.jsonl`` at logical base 0."""
    path = paths.events_path()
    return SliceRef(path=path, rel="events.jsonl", start_offset=0, end_offset=None, live=True)


def _load_manifest() -> dict[str, Any] | None:
    p = manifest_path()
    try:
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A corrupt/half-read manifest must never crash an append or a read; the
        # pristine fallback keeps the live file readable (worst case a rotation
        # re-writes a fresh manifest).
        return None
    return data if isinstance(data, dict) else None


def _manifest_to_slices(data: dict[str, Any] | None) -> tuple[list[SliceRef], SliceRef]:
    """Return ``(sealed_slices_oldest_first, live_slice)`` for a manifest dict,
    or the pristine single-live-slice state when ``data`` is falsy."""

    if not data:
        return [], _default_live()
    sealed: list[SliceRef] = []
    for item in data.get("slices") or []:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("file") or "").strip()
        if not rel:
            continue
        start = _as_int(item.get("start_offset"))
        end = _as_int(item.get("end_offset"))
        sealed.append(SliceRef(path=_abs(rel), rel=rel, start_offset=start, end_offset=end, live=False))
    live_raw = data.get("live") if isinstance(data.get("live"), dict) else {}
    live_rel = str(live_raw.get("file") or "events.jsonl").strip() or "events.jsonl"
    base = _as_int(live_raw.get("base_offset"))
    live = SliceRef(path=_abs(live_rel), rel=live_rel, start_offset=base, end_offset=None, live=True)
    return sealed, live


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _live_size(live: SliceRef) -> int:
    try:
        return live.path.stat().st_size if live.path.exists() else 0
    except OSError:
        return 0


def slices() -> list[SliceRef]:
    """All slices oldest-first, ending with the live (open-ended) slice."""
    sealed, live = _manifest_to_slices(_load_manifest())
    return [*sealed, live]


def slice_count() -> int:
    return len(slices())


def live_path() -> Path:
    """The current file to append to (``events.jsonl`` until the first rotation)."""
    _sealed, live = _manifest_to_slices(_load_manifest())
    return live.path


# S54 removed ``live_base_offset``: a rotation-offset accessor with no reader.

def log_end_offset() -> int:
    """Logical tail offset = the total bytes ever appended = the cursor a tailer
    that has consumed everything holds. Equals ``getsize(events.jsonl)`` in the
    pristine state, so it is a drop-in for the old
    ``os.path.getsize(paths.events_path())`` watermark authority."""
    _sealed, live = _manifest_to_slices(_load_manifest())
    return live.start_offset + _live_size(live)


def offset_reads(start: int) -> list[tuple[Path, int, int]]:
    """Resolve a logical ``start`` offset into the slice reads needed to iterate
    forward from it, as ``(slice_path, slice_start_offset, seek_within)`` tuples
    in order.

    Mirrors the legacy ``iter_from_offset`` clamp: a ``start`` past the live tail
    seeks to the live slice's end (yields nothing) rather than erroring.
    """
    start = max(0, int(start or 0))
    reads: list[tuple[Path, int, int]] = []
    for sl in slices():
        if sl.live:
            size = _live_size(sl)
            slice_end = sl.start_offset + size
            if start >= slice_end:
                seek = size  # at/past the tail → seek to EOF, yield nothing
            elif start <= sl.start_offset:
                seek = 0
            else:
                seek = start - sl.start_offset
            reads.append((sl.path, sl.start_offset, seek))
        else:
            end = sl.end_offset if sl.end_offset is not None else sl.start_offset
            if start >= end:
                continue  # this sealed slice is entirely before start
            seek = 0 if start <= sl.start_offset else (start - sl.start_offset)
            reads.append((sl.path, sl.start_offset, seek))
    return reads


def ordered_line_sources() -> list[Path]:
    """Slice files oldest-first for logical-offset scans across the whole log."""
    return [sl.path for sl in slices()]


def reversed_slices() -> list[SliceRef]:
    """Slices newest-first (live slice first) for newest-N reverse scans."""
    return list(reversed(slices()))


def _write_manifest(data: dict[str, Any]) -> None:
    p = manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    last_exc: OSError | None = None
    for attempt in range(_MANIFEST_REPLACE_RETRIES):
        try:
            os.replace(tmp, p)
            return
        except PermissionError as exc:  # Windows: target momentarily open by a reader
            last_exc = exc
            time.sleep(_MANIFEST_REPLACE_SLEEP_SECONDS)
    # Exhausted retries: surface the failure rather than silently leaving the tmp
    # behind and the manifest stale (no silent drops).
    if last_exc is not None:
        raise last_exc


def _rotate(sealed: list[SliceRef], live: SliceRef, size: int) -> tuple[Path, dict[str, Any]]:
    """Seal ``live`` in place and open a fresh live slice. Caller holds
    ``events_lock()``. Returns ``(new_live_path, info)``.

    Windows-safe: creates a new file and rewrites the manifest only — the sealed
    file (which a reader may hold open) is never touched.
    """
    base = live.start_offset
    new_base = base + size
    new_live_rel = f"{archive_dir().name}/events.{new_base}.jsonl"
    new_live_path = _abs(new_live_rel)
    archive_dir().mkdir(parents=True, exist_ok=True)
    # Create the fresh (empty) live file. Mode 'a' creates-if-absent and never
    # truncates, so a crash-retry (fresh file made, manifest not yet written)
    # reuses the same empty file idempotently.
    with open(new_live_path, "a", encoding="utf-8", newline="\n"):
        pass
    new_slices = [
        {"file": s.rel, "start_offset": s.start_offset, "end_offset": s.end_offset}
        for s in sealed
    ]
    new_slices.append({"file": live.rel, "start_offset": base, "end_offset": new_base})
    _write_manifest(
        {
            "version": MANIFEST_VERSION,
            "slices": new_slices,
            "live": {"file": new_live_rel, "base_offset": new_base},
        }
    )
    info = {
        "rotated": True,
        "sealed_file": live.rel,
        "sealed_bytes": size,
        "sealed_range": [base, new_base],
        "live_file": new_live_rel,
        "base_offset": new_base,
        "slice_count": len(new_slices) + 1,
    }
    return new_live_path, info


def _normalize_cap(cap_bytes: Any) -> int:
    try:
        cap = int(cap_bytes)
    except (TypeError, ValueError):
        return DEFAULT_ROTATION_CAP_BYTES
    return cap if cap >= 0 else 0


def prepare_live_for_append(cap_bytes: int) -> Path:
    """Rotate the live slice if it has reached ``cap_bytes``, then return the
    path the next line should be appended to. One manifest read.

    MUST be called while holding ``events_lock()`` (the append chokepoint does).
    ``cap_bytes <= 0`` disables rotation.
    """
    sealed, live = _manifest_to_slices(_load_manifest())
    cap = _normalize_cap(cap_bytes)
    if cap > 0:
        size = _live_size(live)
        if size >= cap:
            new_live_path, _info = _rotate(sealed, live, size)
            return new_live_path
    return live.path


def rotation_health() -> dict[str, Any]:
    """Accounting for ``event_log_health``: slice census + logical tail. Stat-only
    for bytes; reads slice files for line counts (a maintenance-path call)."""
    ordered = slices()
    live = ordered[-1]
    total_bytes = 0
    total_lines = 0
    for sl in ordered:
        if not sl.path.exists():
            continue
        try:
            total_bytes += sl.path.stat().st_size
        except OSError:
            pass
        try:
            with open(sl.path, "rb") as handle:
                total_lines += sum(1 for line in handle if line.strip())
        except OSError:
            pass
    live_bytes = _live_size(live)
    live_lines = 0
    if live.path.exists():
        try:
            with open(live.path, "rb") as handle:
                live_lines = sum(1 for line in handle if line.strip())
        except OSError:
            live_lines = 0
    return {
        "rotated_slice_count": len(ordered) - 1,
        "total_bytes": total_bytes,
        "total_lines": total_lines,
        "live_slice_file": live.rel,
        "live_slice_bytes": live_bytes,
        "live_slice_lines": live_lines,
        "log_end_offset": live.start_offset + live_bytes,
    }
