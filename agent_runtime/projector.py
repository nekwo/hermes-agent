from __future__ import annotations

import json
import os
import time
from contextlib import closing
from dataclasses import dataclass

from hermes_time import now

from .events import EventLog
from .read_model import ReadModel
from .snapshot import build_snapshot


LEASE_TTL_SECONDS = 30


@dataclass(slots=True)
class ProjectorResult:
    """What one projection pass actually did.

    B4 removed two fields that could only ever hold one value: ``changed`` was
    the literal ``{"sections": ["snapshot"]}`` whenever anything was applied
    (the projection unit has been the whole frame since mission rows left), and
    ``stale_sections`` was ``[]`` on every path — a partial-refresh accounting
    field left behind by the row-delta design it belonged to. Reporting a
    constant as if it were a measurement is a lie a caller can act on;
    ``applied_events`` already carries the fact both were standing in for.
    """

    applied_events: int = 0
    from_offset: int = 0
    to_offset: int = 0
    incremental_apply_ms: int = 0
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
        applied_events = self._count_pending(from_offset)
        if not applied_events:
            return ProjectorResult(
                from_offset=from_offset,
                to_offset=from_offset,
                incremental_apply_ms=int((time.perf_counter() - started) * 1000),
                lease_acquired=True,
            )
        self.full_rebuild()
        current = self.read_model.projection_watermark("snapshot") or {}
        return ProjectorResult(
            applied_events=applied_events,
            from_offset=from_offset,
            to_offset=int(current.get("event_offset") or 0),
            incremental_apply_ms=int((time.perf_counter() - started) * 1000),
            lease_acquired=True,
        )

    def _count_pending(self, from_offset: int) -> int:
        """How many events sit past ``from_offset`` — counted, not collected.

        The pass only ever needs the COUNT (the projection unit is the whole
        frame), so materializing the tail into a list held every pending event
        in memory to call ``len`` on it. On a multi-hundred-MB log after a long
        idle that is the whole tail resident for one integer.
        """

        pending = iter(self.event_log.iter_from_offset(from_offset))
        if next(pending, None) is None:
            return 0
        return 1 + sum(1 for _ in pending)

    def full_rebuild(self) -> None:
        self.read_model.apply_full_rebuild(build_snapshot())
