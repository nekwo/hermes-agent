"""Per-actor read-model checkpoint (Stage S5, operator ruling 2026-07-16).

    "Entity classes, persisted per-actor, like Unreal: the store IS the
    checkpoint; wire bundles are transport envelopes, never a second
    persistence format."

Typed entity CLASSES define the schema; the CANONICAL persisted form is one
file per actor — which is exactly the fork's existing on-disk store layout
(``agent_runtime/paths.py``: ``persona_instances/``, ``flow_graphs/``,
``flow_graphs/``, ``boards/`` …). So no second checkpoint format is invented:
this module is a READ-ONLY bundler that reads those per-actor files verbatim
into one keyed transport envelope. It never re-projects a row and it writes
nothing anywhere.

Envelope shape (``build_checkpoint``)::

    {
      "checkpoint_version": 1,
      "generated_at": <iso>,
      "classes": {"<class>": {"<actor_id>": <row read verbatim>}},
      "counts":  {"<class>": <rows returned>},
      "bytes_estimate": <compact-JSON size of the envelope>,
      "watermark": {"event_offset": <bytes>, "last_event_ts": <iso|null>, ...},
      # present only when non-empty:
      "truncations":     {"<class>": {"truncated": true, "total": N, "returned": M}},
      "requested_absent": ["<class>", …],
    }

The ``watermark`` reuses the SAME authority the snapshot/parity envelope uses
(:func:`agent_runtime.parity.events_watermark`): ``event_offset`` is the
append-only event-log byte size — the cursor a streaming tailer resumes from
(``EventLog.iter_from_offset``). That makes a checkpoint orderable against log
entries for the future S6 fold: "state at watermark W, replay entries > W".

Discovery lists the classes it FOUND on the runtime root (a class whose store
directory does not exist is simply absent from the envelope) rather than
hardcoding a closed present-set. The registry below is the known vocabulary —
the schema — while the envelope reports what actually exists on disk.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from hermes_time import now

from . import paths
from .events import EventLog
from .parity import events_watermark

CHECKPOINT_VERSION = 1


def _flow_graphs_dir() -> Path:
    # FlowGraphStore persists one JSON file per graph id under this dir
    # (agent_runtime/flow_graph.py); there is no paths.py helper for it, so it
    # is resolved here off the same store root as every other class.
    return paths.store_root() / "flow_graphs"


@dataclass(frozen=True)
class EntityClass:
    """A typed entity class: a name + the on-disk per-actor store directory.

    ``recursive`` distinguishes the two store layouts the fork uses:

    * flat (``recursive=False``): ``<dir>/<actor_id>.json`` — the actor id is
      the filename stem (``persona_instances``, ``flow_graphs`` …).
    * nested (``recursive=True``): owner sub-directories hold the per-actor
      files (``boards/<board_id>/…``, ``repo_bundles/<task_id>/…``). The actor key is
      the POSIX relative path minus ``.json`` so every file is captured
      verbatim and uniquely — the truest "the store IS the checkpoint" form for
      a nested store.
    """

    name: str
    dir_fn: Callable[[], Path]
    recursive: bool = False


# The typed entity-class registry (the SCHEMA). Each class maps to the fork's
# existing per-actor store directory. Ordered persona/mission-first so a human
# `checkpoint classes` dump reads top-down. Adding a store here is additive;
# discovery only surfaces the ones whose directory exists on the live root.
ENTITY_CLASSES: tuple[EntityClass, ...] = (
    EntityClass("persona_instances", paths.persona_instances_dir),
    EntityClass("persona_assignments", paths.persona_assignments_dir),
    EntityClass("runs", paths.runs_dir),
    EntityClass("incidents", paths.incidents_dir),
    EntityClass("runtime_instances", paths.runtime_instances_dir),
    EntityClass("worker_sessions", paths.worker_sessions_dir),
    EntityClass("workspaces", paths.workspaces_dir),
    EntityClass("realms", paths.realms_dir),
    EntityClass("agents", paths.agents_dir),
    EntityClass("flow_graphs", _flow_graphs_dir),
    EntityClass("boards", paths.boards_root, recursive=True),
    EntityClass("repo_bundles", paths.repo_bundles_dir, recursive=True),
    # S44 retired the role_envelopes / role_checklists classes with their stores.
    # Both directories were archived aside as writer-less on 2026-07-30 and no
    # surviving code can fill them again, so the rows described a shape
    # `checkpoint fetch` could only ever report as absent — the same rule S23
    # applied to `proofs`.
    EntityClass("self_tests", paths.self_tests_dir, recursive=True),
    EntityClass("packet_artifacts", paths.packet_artifacts_dir, recursive=True),
)

ENTITY_CLASS_NAMES: tuple[str, ...] = tuple(entity.name for entity in ENTITY_CLASSES)


def _class_dir(entity: EntityClass) -> Path | None:
    try:
        directory = entity.dir_fn()
    except Exception:
        return None
    try:
        if directory.is_dir():
            return directory
    except OSError:
        return None
    return None


def _iter_actor_files(entity: EntityClass, directory: Path) -> Iterator[tuple[str, Path]]:
    """Yield ``(actor_id, path)`` for every per-actor file of a class, sorted by
    id so the envelope is deterministic (goldens, byte-parity)."""

    if entity.recursive:
        paths_found = sorted(p for p in directory.rglob("*.json") if p.is_file())
        for path in paths_found:
            actor_id = path.relative_to(directory).with_suffix("").as_posix()
            yield actor_id, path
    else:
        paths_found = sorted(p for p in directory.glob("*.json") if p.is_file())
        for path in paths_found:
            yield path.stem, path


def _read_actor_row(path: Path) -> Any:
    """Read one per-actor file verbatim into a jsonable row.

    A corrupt / unreadable file becomes a TYPED ``{"unreadable": true, "error":
    …}`` row — the bundle accounts for it and never aborts the whole fetch (a
    single bad file must not deny recovery of every other actor)."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"unreadable": True, "error": f"{type(exc).__name__}: {exc}"}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return {"unreadable": True, "error": f"{type(exc).__name__}: {exc}"}


def _normalize_class_filter(classes: list[str] | None) -> list[str] | None:
    """Normalize the ``--classes`` filter. ``None``/empty → all discovered
    classes; otherwise the de-duplicated, order-preserving requested names."""

    if not classes:
        return None
    seen: set[str] = set()
    names: list[str] = []
    for candidate in classes:
        name = str(candidate or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names or None


def _checkpoint_watermark(event_log: EventLog | None) -> dict[str, Any]:
    """The single global source-position marker for this checkpoint.

    Reuses the snapshot/parity authority (:func:`events_watermark`): the event
    log is a single append-only stream, so the checkpoint carries ONE watermark
    (byte offset + last-event ts), not a per-class one — a fold replays every
    entry past ``event_offset`` regardless of which store it mutated."""

    log = event_log or EventLog()
    try:
        tail = log.tail(1)
    except Exception:
        tail = []
    last_ts = getattr(tail[-1], "ts", None) if tail else None
    return events_watermark(last_event_ts=last_ts)


def _bytes_estimate(payload: Any) -> int:
    try:
        return len(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
    except Exception:
        return 0


def build_checkpoint(
    classes: list[str] | None = None,
    *,
    row_cap: int | None = None,
    event_log: EventLog | None = None,
) -> dict[str, Any]:
    """Bundle the per-actor store files into a keyed transport envelope.

    Read-only: every row is the file's content read verbatim, keyed by actor id
    within its entity class. ``classes`` filters to a subset (unknown / absent
    names are accounted in ``requested_absent``); ``row_cap`` bounds each class
    with accounted truncation (never a silent drop)."""

    requested = _normalize_class_filter(classes)
    result_classes: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    truncations: dict[str, dict[str, Any]] = {}

    for entity in ENTITY_CLASSES:
        if requested is not None and entity.name not in requested:
            continue
        directory = _class_dir(entity)
        if directory is None:
            continue
        actor_files = list(_iter_actor_files(entity, directory))
        total = len(actor_files)
        selected = actor_files
        truncated = False
        if row_cap is not None and row_cap >= 0 and total > row_cap:
            selected = actor_files[:row_cap]
            truncated = True
        rows: dict[str, Any] = {}
        for actor_id, path in selected:
            rows[actor_id] = _read_actor_row(path)
        result_classes[entity.name] = rows
        counts[entity.name] = len(rows)
        if truncated:
            truncations[entity.name] = {
                "truncated": True,
                "total": total,
                "returned": len(rows),
            }

    envelope: dict[str, Any] = {
        "checkpoint_version": CHECKPOINT_VERSION,
        "generated_at": now(),
        "classes": result_classes,
        "counts": counts,
        "watermark": _checkpoint_watermark(event_log),
    }
    if truncations:
        envelope["truncations"] = truncations
    if requested is not None:
        absent = sorted(name for name in requested if name not in result_classes)
        if absent:
            envelope["requested_absent"] = absent
    # Measured over the whole envelope (minus this field): the honest transport
    # size, dominated by the keyed class payload.
    envelope["bytes_estimate"] = _bytes_estimate(envelope)
    return envelope


def discover_classes() -> list[str]:
    """Entity classes whose store directory exists on the runtime root, in
    registry order. The envelope lists what it FOUND, never a closed set."""

    return [entity.name for entity in ENTITY_CLASSES if _class_dir(entity) is not None]


def class_manifest(*, event_log: EventLog | None = None) -> dict[str, Any]:
    """Cheap per-class census for ``harness checkpoint classes``: discovered
    classes with per-class actor counts and byte totals (``stat`` only — never
    reads file contents), plus the shared watermark."""

    entries: list[dict[str, Any]] = []
    for entity in ENTITY_CLASSES:
        directory = _class_dir(entity)
        if directory is None:
            continue
        count = 0
        byte_total = 0
        for _actor_id, path in _iter_actor_files(entity, directory):
            count += 1
            try:
                byte_total += path.stat().st_size
            except OSError:
                continue
        entries.append(
            {
                "class": entity.name,
                "count": count,
                "bytes": byte_total,
                "recursive": entity.recursive,
            }
        )
    return {
        "checkpoint_version": CHECKPOINT_VERSION,
        "generated_at": now(),
        "discovered": [entry["class"] for entry in entries],
        "classes": entries,
        "watermark": _checkpoint_watermark(event_log),
    }
