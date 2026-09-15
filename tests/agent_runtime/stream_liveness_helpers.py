"""Reading a stream past the BOOT build's liveness (MC-4 / P6).

Since the boot hydrate runs its build on ``_SnapshotBuildJob`` and heartbeats on
the cadence while it runs, the first frame of a stream is no longer always the
hydrate. In production that is invisible — the cadence is 5 s and a boot build
that finishes sooner emits nothing, which is also why the committed frame
goldens are unaffected — but every case that drives a sub-second interval to
force the TAIL loop's heartbeats now sees boot liveness first.

Four suites needed the same skip, which is one too many to copy. It lives here
instead, with the two properties that make it safe:

* it identifies boot liveness by its ``snapshot_build`` ACTIVITY BLOCK, never by
  position — a positional skip would silently eat a real frame the day the
  ordering changes again;
* it asserts the honest null watermark in passing, so every caller carries that
  pin for free (``heartbeat_frame``: liveness without a position must not be
  stamped ``0``).

Order-dependence is the reason this is not optional. A case that took the first
frame as the hydrate passed alone — a warm boot build can finish inside 50 ms —
and failed in a batch run where the same build was slower. That is a flake, not
a failure, and it would have been diagnosed as one.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def is_boot_liveness(frame: dict[str, Any]) -> bool:
    activity = frame.get("activity")
    return (
        frame.get("type") == "heartbeat"
        and isinstance(activity, dict)
        and activity.get("kind") == "snapshot_build"
    )


def drain_boot_liveness(frames: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """The first frame carrying a CORE, past any liveness the boot build emitted."""

    for frame in frames:
        if is_boot_liveness(frame):
            assert frame["watermark"]["event_offset"] is None, frame["watermark"]
            continue
        return frame
    raise AssertionError("the stream ended before delivering a core frame")
