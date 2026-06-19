from __future__ import annotations

import json
from collections.abc import Iterator
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

    def for_task(self, task_id: str, *, limit: int = 50, since: datetime | None = None) -> list[Event]:
        if not paths.events_path().exists():
            return []
        task_token = _task_id_json_token(task_id)
        selected: list[Event] = []
        lines = paths.events_path().read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            if task_token not in line:
                continue
            evt = from_jsonable(Event, json.loads(line))
            if evt.task_id != task_id:
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


def _task_id_json_token(task_id: str) -> str:
    encoded = json.dumps(str(task_id), ensure_ascii=False, separators=(",", ":"))
    return f'"task_id":{encoded}'
