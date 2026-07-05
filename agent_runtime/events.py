from __future__ import annotations

import json
from collections.abc import Collection, Iterator
from datetime import datetime

from . import paths
from .decision_contract_registry import allowed_event_types
from .errors import EventPayloadTooLarge
from .locks import events_lock
from .models import Event
from .serde import from_jsonable, to_jsonable

EVENT_PAYLOAD_LIMIT_BYTES = 4096

ALLOWED_EVENT_TYPES = allowed_event_types()


class EventLog:
    def append(self, evt: Event) -> None:
        if evt.type not in ALLOWED_EVENT_TYPES:
            raise ValueError(f"unknown event type: {evt.type}")
        payload_bytes = json.dumps(to_jsonable(evt.payload), ensure_ascii=False).encode("utf-8")
        if len(payload_bytes) > EVENT_PAYLOAD_LIMIT_BYTES:
            raise EventPayloadTooLarge(
                f"event payload is {len(payload_bytes)} bytes; limit is {EVENT_PAYLOAD_LIMIT_BYTES}"
            )
        path = paths.events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(to_jsonable(evt), ensure_ascii=False, separators=(",", ":"))
        with events_lock():
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()

    def tail(self, n: int) -> list[Event]:
        if n <= 0 or not paths.events_path().exists():
            return []
        lines = paths.events_path().read_text(encoding="utf-8").splitlines()[-n:]
        return [from_jsonable(Event, json.loads(line)) for line in lines if line.strip()]

    def iter_all(self) -> Iterator[Event]:
        if not paths.events_path().exists():
            return
        with open(paths.events_path(), encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield from_jsonable(Event, json.loads(line))

    def iter_from_offset(self, offset: int) -> Iterator[tuple[int, Event]]:
        path = paths.events_path()
        if not path.exists():
            return
        start = max(0, int(offset or 0))
        size = path.stat().st_size
        if start > size:
            start = size
        with open(path, "rb") as handle:
            handle.seek(start)
            for raw in handle:
                if not raw.strip():
                    continue
                new_offset = handle.tell()
                yield new_offset, from_jsonable(Event, json.loads(raw.decode("utf-8")))

    def for_task(
        self,
        task_id: str,
        *,
        limit: int = 50,
        since: datetime | None = None,
        types: Collection[str] | None = None,
    ) -> list[Event]:
        """Return the newest events for ``task_id``, oldest-first.

        When ``types`` is given, ``limit`` counts only events whose ``type`` is in
        that set — a busy task whose recent tail is flooded with non-matching rows
        (e.g. an incident loop) can no longer starve the window before the type
        filter runs. The type-token substring pre-filter keeps the reverse scan
        cheap even against such floods.
        """

        if not paths.events_path().exists():
            return []
        task_token = _task_id_json_token(task_id)
        type_tokens = _type_json_tokens(types)
        selected: list[Event] = []
        lines = paths.events_path().read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            if task_token not in line:
                continue
            if type_tokens is not None and not any(token in line for token in type_tokens):
                continue
            evt = from_jsonable(Event, json.loads(line))
            if evt.task_id != task_id:
                continue
            if types is not None and evt.type not in types:
                continue
            if since is not None and evt.ts < since:
                continue
            selected.append(evt)
            if limit > 0 and len(selected) >= limit:
                break
        return list(reversed(selected))

    def for_session(
        self,
        session_id: str,
        *,
        limit: int = 50,
        since: datetime | None = None,
        types: Collection[str] | None = None,
    ) -> list[Event]:
        """Return events bound to a conversational chat ``session_id``.

        Mirrors :meth:`for_task` but matches on the event ``session_id`` lineage
        instead of ``task_id``. Used by the snapshot trace projection to surface
        tool/progress events recorded during a (non-task) persona chat turn. The
        substring pre-filter keeps the reverse scan cheap; task-run events carry
        ``"session_id":null`` and are skipped by both the token and the
        post-decode equality check. ``types`` behaves as in :meth:`for_task`.
        """

        if not paths.events_path().exists():
            return []
        session_token = _session_id_json_token(session_id)
        type_tokens = _type_json_tokens(types)
        selected: list[Event] = []
        lines = paths.events_path().read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            if session_token not in line:
                continue
            if type_tokens is not None and not any(token in line for token in type_tokens):
                continue
            evt = from_jsonable(Event, json.loads(line))
            if evt.session_id != session_id:
                continue
            if types is not None and evt.type not in types:
                continue
            if since is not None and evt.ts < since:
                continue
            selected.append(evt)
            if limit > 0 and len(selected) >= limit:
                break
        return list(reversed(selected))

    def iter_since(self, ts: datetime) -> Iterator[Event]:
        if not paths.events_path().exists():
            return
        with open(paths.events_path(), encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                evt = from_jsonable(Event, json.loads(line))
                if evt.ts >= ts:
                    yield evt


class CachedEventLog(EventLog):
    """Build-scoped EventLog that reads ``events.jsonl`` ONCE and serves every
    read from the cached raw lines.

    A single snapshot build calls ``for_task`` / ``for_session`` / ``tail`` dozens
    of times; the base ``EventLog`` re-reads + re-splits the entire (45MB+) file on
    each call (the dominant repeated cost). This caches the split lines once and
    keeps the base's *selective* parse (substring pre-filter → ``json.loads`` only
    on matching lines), so it dedupes the file I/O without paying to parse every
    event. It is a point-in-time view — created per build and discarded, so appends
    made elsewhere during the build are intentionally not reflected.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lines: list[str] | None = None

    def _cached_lines(self) -> list[str]:
        if self._lines is None:
            path = paths.events_path()
            self._lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return self._lines

    def _scan(
        self,
        token: str,
        match,
        *,
        limit: int,
        since: datetime | None,
        types: Collection[str] | None = None,
    ) -> list[Event]:
        type_tokens = _type_json_tokens(types)
        selected: list[Event] = []
        for line in reversed(self._cached_lines()):
            if token not in line:
                continue
            if type_tokens is not None and not any(type_token in line for type_token in type_tokens):
                continue
            evt = from_jsonable(Event, json.loads(line))
            if not match(evt):
                continue
            if types is not None and evt.type not in types:
                continue
            if since is not None and evt.ts < since:
                continue
            selected.append(evt)
            if limit > 0 and len(selected) >= limit:
                break
        return list(reversed(selected))

    def for_task(
        self,
        task_id: str,
        *,
        limit: int = 50,
        since: datetime | None = None,
        types: Collection[str] | None = None,
    ) -> list[Event]:
        return self._scan(
            _task_id_json_token(task_id),
            lambda evt: evt.task_id == task_id,
            limit=limit,
            since=since,
            types=types,
        )

    def for_session(
        self,
        session_id: str,
        *,
        limit: int = 50,
        since: datetime | None = None,
        types: Collection[str] | None = None,
    ) -> list[Event]:
        return self._scan(
            _session_id_json_token(session_id),
            lambda evt: evt.session_id == session_id,
            limit=limit,
            since=since,
            types=types,
        )

    def tail(self, n: int) -> list[Event]:
        if n <= 0:
            return []
        return [from_jsonable(Event, json.loads(line)) for line in self._cached_lines()[-n:] if line.strip()]

    def iter_all(self) -> Iterator[Event]:
        for line in self._cached_lines():
            if line.strip():
                yield from_jsonable(Event, json.loads(line))

    def iter_from_offset(self, offset: int) -> Iterator[tuple[int, Event]]:
        current = 0
        start = max(0, int(offset or 0))
        for line in self._cached_lines():
            raw = (line + "\n").encode("utf-8")
            current += len(raw)
            if current <= start or not line.strip():
                continue
            yield current, from_jsonable(Event, json.loads(line))

    def iter_since(self, ts: datetime) -> Iterator[Event]:
        for line in self._cached_lines():
            if not line.strip():
                continue
            evt = from_jsonable(Event, json.loads(line))
            if evt.ts >= ts:
                yield evt


def _task_id_json_token(task_id: str) -> str:
    encoded = json.dumps(str(task_id), ensure_ascii=False, separators=(",", ":"))
    return f'"task_id":{encoded}'


def _session_id_json_token(session_id: str) -> str:
    encoded = json.dumps(str(session_id), ensure_ascii=False, separators=(",", ":"))
    return f'"session_id":{encoded}'


def _type_json_tokens(types: Collection[str] | None) -> tuple[str, ...] | None:
    """Literal ``"type":"…"`` substrings for the compact-JSON line pre-filter.

    Safe because :meth:`EventLog.append` writes every line with
    ``separators=(",", ":")`` — the token form is stable. ``None`` disables the
    pre-filter (untyped scan).
    """

    if types is None:
        return None
    return tuple(
        f'"type":{json.dumps(str(event_type), ensure_ascii=False, separators=(",", ":"))}'
        for event_type in sorted(types)
    )
