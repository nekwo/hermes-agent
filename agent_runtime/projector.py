from __future__ import annotations

import json
import os
import time
from contextlib import closing
from dataclasses import dataclass, field

from hermes_time import now

from .events import EventLog
from .parity import events_watermark
from .read_model import ReadModel
from .snapshot import build_snapshot


LEASE_TTL_SECONDS = 30


@dataclass(slots=True)
class ProjectorResult:
    applied_events: int = 0
    from_offset: int = 0
    to_offset: int = 0
    incremental_apply_ms: int = 0
    changed: dict[str, list[str]] = field(default_factory=dict)
    stale_sections: list[str] = field(default_factory=list)
    lease_acquired: bool = False


class Projector:
    """Project the surviving snapshot as one coherent chat-runtime frame.

    Mission entities formerly supported row-level deltas. After their removal,
    any new event can affect persona chat, boards, scope, or the runtime graph,
    so the safe incremental unit is the compact snapshot itself.
    """

    def __init__(self, read_model: ReadModel, *, config, event_log: EventLog | None = None):
        self.read_model = read_model
        self.config = config
        self.event_log = event_log or EventLog()

    def acquire_lease(self) -> bool:
        lease = {
            "pid": os.getpid(),
            "acquired_at": now(),
            "expires_at_monotonic": time.monotonic() + LEASE_TTL_SECONDS,
        }
        with closing(self.read_model.connect()) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", ("projector_lease",)).fetchone()
            if row is not None:
                try:
                    current = json.loads(row["value"])
                except Exception:
                    current = {}
                if (
                    float(current.get("expires_at_monotonic") or 0) > time.monotonic()
                    and int(current.get("pid") or 0) != os.getpid()
                ):
                    return False
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                ("projector_lease", json.dumps(lease, default=str, sort_keys=True)),
            )
            conn.commit()
        return True

    def apply_pending(self) -> ProjectorResult:
        started = time.perf_counter()
        if not self.acquire_lease():
            return ProjectorResult(lease_acquired=False)
        watermark = self.read_model.projection_watermark("snapshot")
        if watermark is None:
            self.full_rebuild()
            current = self.read_model.projection_watermark("snapshot") or {}
            return ProjectorResult(
                to_offset=int(current.get("event_offset") or 0),
                incremental_apply_ms=int((time.perf_counter() - started) * 1000),
                lease_acquired=True,
            )
        from_offset = int(watermark.get("event_offset") or 0)
        events = list(self.event_log.iter_from_offset(from_offset))
        if not events:
            return ProjectorResult(
                from_offset=from_offset,
                to_offset=from_offset,
                incremental_apply_ms=int((time.perf_counter() - started) * 1000),
                lease_acquired=True,
            )
        snapshot = build_snapshot()
        self.read_model.apply_full_rebuild(
            snapshot,
            watermark=(snapshot.get("parity") or {}).get("watermark") or events_watermark(),
        )
        current = self.read_model.projection_watermark("snapshot") or {}
        return ProjectorResult(
            applied_events=len(events),
            from_offset=from_offset,
            to_offset=int(current.get("event_offset") or 0),
            incremental_apply_ms=int((time.perf_counter() - started) * 1000),
            changed={"sections": ["snapshot"]},
            stale_sections=[],
            lease_acquired=True,
        )

    def full_rebuild(self) -> None:
        snapshot = build_snapshot()
        self.read_model.apply_full_rebuild(
            snapshot,
            watermark=(snapshot.get("parity") or {}).get("watermark") or events_watermark(),
        )
