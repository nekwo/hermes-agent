"""W3-H2 — two demote builds at one offset, and only one of them builds.

THE WASTE
=========

Live serve, 2026-08-22 10:50 local, pid 22588
(``profiles/base/logs/agent.log``): three ``snapshot_build reason=demote
role=led`` lines at the SAME offset 89961793 — generations 17 / 18 / 19,
``build_ms`` 3017 / 3210 / 2388, ``waited_ms`` 5452 / 5686 / 5453, callers
hub / cli / hub. That they were the same core is not an inference: each one
wrote back under the identical key, ``snapshot_core_cache_write ok=true …
fingerprint=9d655b54f622 offset=89961793``, three times. Five more such pairs
land in the two minutes around that drop, and the launcher's
``roster_confirmed=8309ms`` is the fold waiting behind them.

``build_snapshot``'s coalescer cannot merge them: it is deliberately strict —
a caller arriving mid-build waits for the NEXT build rather than riding the
in-flight one — and that rule is what turns three arrivals into three
sequential builds.

WHAT IS ASSERTED, AND WHAT WOULD MAKE IT VACUOUS
================================================

The counter is on ``_build_snapshot_uncoalesced``, the build BODY, so "the
second one did not build" is a count of real work and not a reading of a
provenance label the producer writes and a mutant could forge.

Every case asserts the FRAMES as well as the count, and that is the BO-1
convergence-pair invariant (``73d06f777f``) held here on purpose: the reuse is
of the core, never a dedupe of emissions. A "fix" that answered the second
consumer with nothing, or with a heartbeat, would satisfy a build count of one
and would be the exact defect BO-1 named — so both consumers are asserted to
receive a complete delta carrying the entity the batch was about.

And the negative arm is a real append, not an injected refusal: an event
landing between the two builds must make the second one build again.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from agent_runtime import core_cache, demote_core_reuse, snapshot as snapshot_module
from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime.store import WorkspaceStore
from agent_runtime.stream import stream_frames
from tests.agent_runtime.stream_liveness_helpers import drain_boot_liveness

WS = "ws_demote_reuse"


@pytest.fixture(autouse=True)
def fresh_process_lanes():
    """Both lanes this file depends on are PROCESS state.

    A case that left a core held would let the next one reuse it and pass
    without ever exercising the check; a case that left the cache lane armed
    would turn the next one's demote into a cache hit and count zero builds.
    """

    core_cache.reset_process_state()
    demote_core_reuse.reset_process_state()
    yield
    core_cache.reset_process_state()
    demote_core_reuse.reset_process_state()


@pytest.fixture
def build_bodies(monkeypatch):
    """Count executions of the build BODY, not calls to ``build_snapshot``.

    ``build_snapshot`` is entered by riders, by cache hits and by the reuse
    lane; only ``_build_snapshot_uncoalesced`` is the ~3 s of work this stage
    removes. Counting the outer call would count the thing that is supposed to
    stay and miss the thing that is supposed to go.
    """

    calls: list[float] = []
    real = snapshot_module._build_snapshot_uncoalesced

    def _counting(*args, **kwargs):
        calls.append(0.0)
        return real(*args, **kwargs)

    monkeypatch.setattr(snapshot_module, "_build_snapshot_uncoalesced", _counting)
    return calls


def _append_uncoverable_event(index: int) -> None:
    """One event the patch lane cannot express, so the batch DEMOTES.

    ``state.reconciled`` is not a covered domain entity, so
    ``batch_is_patch_coverable`` rejects it and the batch takes the full-core
    lane — the lane this file is about. Driving the demote through a real
    appended event rather than by forcing the branch keeps every case on the
    path the incident took.
    """

    EventLog().append(
        Event(
            ts=datetime(2026, 8, 22, 10, 50, index % 60, tzinfo=timezone.utc),
            type="state.reconciled",
            task_id=f"task_w3h2_{index}",
            run_id=None,
            persona_id="dev",
            payload={"fingerprint": f"w3h2-{index}"},
        )
    )


def _consumer():
    """One stream consumer, drained past its boot frame and ready to tail."""

    frames = stream_frames(
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=60,
        delta_debounce_seconds=0.01,
        max_frames=2,
    )
    boot = drain_boot_liveness(frames)
    assert boot["type"] == "hydrate", boot["type"]
    return frames


def _workspace_ids(core: dict) -> set[str]:
    return {
        row.get("id")
        for row in (core.get("workspaces") or [])
        if isinstance(row, dict)
    }


def _demote_lines(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("snapshot_build reason=demote ")
    ]


def _field(line: str, key: str) -> str:
    return next(part for part in line.split(" ") if part.startswith(f"{key}="))[
        len(key) + 1 :
    ]


# --------------------------------------------------------------------------- #
# the reuse
# --------------------------------------------------------------------------- #


def test_a_second_demote_at_the_same_offset_reuses_the_core_and_still_gets_its_frame(
    isolate_agent_runtime_root, build_bodies, caplog
):
    """THE GATE. Two consumers, one appended event, ONE build.

    This is the live shape with the wall clock taken out: hub and cli each tail
    the same log, each drains the same uncoverable batch, and each pays for a
    full core because the coalescer will not let the second ride the first.

    Both halves are load-bearing. The build count says the second consumer did
    not rebuild state somebody had just built; the two frames say it was still
    served — same watermark, same content, its own complete delta. A count of
    one with only one frame would be the BO-1 defect wearing this stage's name.
    """

    WorkspaceStore().create(name="reuse", workspace_id=WS)
    first, second = _consumer(), _consumer()

    _append_uncoverable_event(0)

    with caplog.at_level(logging.INFO, logger="agent_runtime.stream"):
        builds_before = len(build_bodies)
        frame_one = next(first)
        frame_two = next(second)
        builds = len(build_bodies) - builds_before

    assert frame_one["type"] == "delta" and frame_two["type"] == "delta"
    assert builds == 1, (
        f"the same offset was built {builds} times; the reuse did not fire "
        f"(demote receipts: {_demote_lines(caplog)})"
    )

    # BO-1: the reuse is of the CORE, never a dedupe of emissions.
    assert (
        frame_one["watermark"]["event_offset"]
        == frame_two["watermark"]["event_offset"]
    )
    assert _workspace_ids(frame_one["core"]) == {WS}
    assert _workspace_ids(frame_two["core"]) == {WS}, (
        "the second consumer's frame does not carry the workspace the first "
        "one's does — it was served something other than the reused core"
    )
    # Its OWN dict, never the first frame's: a consumer that stamps provenance
    # in place must not be able to reach a frame already emitted.
    assert frame_one["core"] is not frame_two["core"]

    lines = _demote_lines(caplog)
    assert len(lines) == 2, lines
    assert "core_source=" not in lines[0], lines[0]
    assert _field(lines[0], "role") == "led", lines[0]
    assert _field(lines[1], "core_source") == "reused_same_offset", lines[1]
    assert _field(lines[1], "role") == "reused", lines[1]
    # ``build_ms=0`` would be a lie about a build that really ran: the reusing
    # line reports the cost of the build UNDERNEATH its frame, which is the
    # first line's own number read off the same envelope.
    assert _field(lines[1], "build_ms") == _field(lines[0], "build_ms"), lines
    assert int(_field(lines[1], "waited_ms")) == 0, lines[1]
    # ``pid`` stays the last field on both shapes of the line.
    assert lines[0].split(" ")[-1].startswith("pid=")
    assert lines[1].split(" ")[-1].startswith("pid=")


def test_an_append_between_the_two_builds_makes_the_second_one_build_again(
    isolate_agent_runtime_root, build_bodies, caplog
):
    """THE VALIDITY ARM. The position moved, so the held core is not current.

    The append is real and lands where it lands in production — between the
    first consumer's frame and the second's — so the second consumer's batch
    reaches an offset the held core's watermark predates. A reuse there would
    ship a core that does not contain the event the frame is stamped with,
    which is precisely the MCF-Q1 shape (``test_stale_core_under_fresh_offset``)
    this lane must never reintroduce.
    """

    WorkspaceStore().create(name="reuse", workspace_id=WS)
    first, second = _consumer(), _consumer()

    _append_uncoverable_event(0)

    with caplog.at_level(logging.INFO, logger="agent_runtime.stream"):
        builds_before = len(build_bodies)
        frame_one = next(first)
        _append_uncoverable_event(1)
        frame_two = next(second)
        builds = len(build_bodies) - builds_before

    assert frame_one["type"] == "delta" and frame_two["type"] == "delta"
    assert builds == 2, (
        "the second demote reused a core built before an event that its own "
        f"frame is stamped with ({builds} builds; {_demote_lines(caplog)})"
    )
    assert (
        frame_two["watermark"]["event_offset"]
        > frame_one["watermark"]["event_offset"]
    ), "the second batch did not reach past the append, so nothing was tested"

    lines = _demote_lines(caplog)
    assert len(lines) == 2, lines
    assert all("core_source=" not in line for line in lines), lines


# --------------------------------------------------------------------------- #
# the module's own refusals
# --------------------------------------------------------------------------- #


def test_an_unreadable_log_position_refuses_the_reuse_rather_than_matching_itself(
    isolate_agent_runtime_root,
):
    """Two unknowns must not compare equal.

    ``None == None`` would read as "the log did not move" when what it says is
    "nobody could tell" — the fail-quiet default ``parity.events_watermark``
    exists to forbid, and the same refusal ``core_cache._stamp_still_stands``
    makes on the same authority.
    """

    core = {"parity": {"watermark": {"event_offset": 41}}}
    assert demote_core_reuse.remember(core) is True

    with pytest.MonkeyPatch.context() as unreadable:
        unreadable.setattr(
            demote_core_reuse,
            "events_position",
            lambda: {"event_offset": None, "event_offset_error": "OSError: locked"},
        )
        assert demote_core_reuse.consult(floor=41) is None


def test_a_core_with_no_watermark_is_never_held():
    """A core that cannot say where it reaches cannot be checked against the
    log, and a held-but-uncheckable core is one refactor away from being served
    under a position it never had."""

    assert demote_core_reuse.remember({"parity": {}}) is False
    assert demote_core_reuse.remember({}) is False
    assert demote_core_reuse.remember(None) is False


def test_a_rejected_consult_drops_the_entry_rather_than_holding_a_dead_core(
    isolate_agent_runtime_root,
):
    """Offsets only grow, so an entry the log has moved past can never match
    again. Dropping it on the rejecting consult is what bounds this module to
    one core between two same-offset builds instead of holding a stale one until
    the next demote happens by."""

    demote_core_reuse.remember({"parity": {"watermark": {"event_offset": 7}}})

    with pytest.MonkeyPatch.context() as moved:
        moved.setattr(
            demote_core_reuse, "events_position", lambda: {"event_offset": 9}
        )
        assert demote_core_reuse.consult(floor=9) is None

    assert demote_core_reuse._entry is None


def test_a_held_core_is_never_handed_back_to_two_callers_as_one_dict(
    isolate_agent_runtime_root,
):
    """The build coalescer deep-copies for exactly this reason: ``label_core``
    stamps provenance IN PLACE, so one shared dict would let a later consumer's
    label land on a frame an earlier one already emitted."""

    demote_core_reuse.remember(
        {"parity": {"watermark": {"event_offset": 12}}, "workspaces": [{"id": WS}]}
    )

    with pytest.MonkeyPatch.context() as still:
        still.setattr(
            demote_core_reuse, "events_position", lambda: {"event_offset": 12}
        )
        one = demote_core_reuse.consult(floor=12)
        two = demote_core_reuse.consult(floor=12)

    assert one == two
    assert one is not two
    assert one["workspaces"] is not two["workspaces"]
