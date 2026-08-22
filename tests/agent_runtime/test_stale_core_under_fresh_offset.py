"""MCF-Q1 — a core may never be shipped as authoritative for an offset its
content predates.

THE INCIDENT
============

The operator drags a second QA agent into Mission Control and messages it. The
NEW agent disappears from the canvas; the PRE-EXISTING QA agent is untouched.
The store is healthy throughout — instance row present, actor row present, chat
root durable, turn projected. Nothing on the write side is wrong.

The producer served stale bytes under fresh offsets. In the drop window the live
log (``profiles/base/logs/agent.log``, 2026-08-21 15:33) carries four
``snapshot_build role=cache reason=demote`` frames at offsets 89,849,656 /
89,849,871 / 89,851,071 / 89,851,462 — all strictly ahead of anything a client
could have folded — every one of them carrying a core built at 14:56, before the
agent existed. The only ``role=led`` frames that hour are at 15:52.

Two links made that possible and each has its own case below:

1. ``core_cache``'s boot consult memo is keyed on the stat of the persisted PAIR,
   never on the store, and the armed window that bounds it closed only at
   ``note_full_build_completed`` — which a cache-HIT boot never reached, because
   the shadow validation deliberately "does not close the lane". So the memo's
   own stated safety bound was vacuous on exactly the boots the memo optimizes,
   and every ``build_snapshot()`` for the rest of that serve process returned the
   boot-time core;
2. ``stream._full_core_batch_frames`` stamps the batch's LAST offset onto
   whatever core the builder hands back. The demote lane is the designated
   carrier of everything the patch lane cannot express, a design that assumes
   that lane is fresh; the memo broke the assumption, and the launcher applies a
   ``delta`` core wholesale, so a section present only via folded patches is
   replaced away by the older core's complete copy of it.

WHY THE PREVIOUS FIX DOES NOT COVER IT
======================================

``7204896978`` repinned a BUILD's own offset capture, and the launcher's
``05a122a9a`` guards the SAME-offset convergence window. These frames came
through the strict-``>`` front door at higher offsets carrying a core the builder
never built in this window, so neither guard is on this path. Both stay.

WHAT MAKES THESE NON-VACUOUS
============================

Every case asserts on ENTITY CONTENT, not on a provenance label: ``core_source``
is a field the producer writes and a mutant can forge, while "is the row the
operator just created in this frame's core" cannot be answered without the work.
Two entities are asserted in every direction — a case that only asserted the NEW
row would also pass under a "fix" that stopped shipping cores at all, and a case
that only asserted the OLD row would pass under one that pinned every row in
place forever (which is why the removal case is here).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from agent_runtime import core_cache, paths
from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime.snapshot import build_snapshot
from agent_runtime.store import WorkspaceStore
from agent_runtime.stream import stream_frames
from tests.agent_runtime.stream_liveness_helpers import drain_boot_liveness


#: The agent that already existed when the serve booted — the one the operator
#: reported as UNAFFECTED. Its survival is half of every assertion here.
WS_PRE = "ws_pre_existing_agent"

#: The agent dragged in during the serve's life — the one that disappeared.
WS_NEW = "ws_second_agent"


@pytest.fixture(autouse=True)
def fresh_cache_lane():
    """Every case starts and ends with a process that has built nothing.

    The lane and its memo are PROCESS state (see ``core_cache.reset_process_state``),
    so a case that left the lane closed would turn the next case's cache probe
    into an unconditional rebuild and pass for the wrong reason.
    """

    core_cache.reset_process_state()
    yield
    core_cache.reset_process_state()


@pytest.fixture
def shadow_requests(monkeypatch):
    """Capture the shadow-validation request instead of running it.

    Two different jobs, both deliberate. For the STREAM cases it reproduces the
    production window the frame-level guard exists for — the seconds between a
    cache-hit boot and its background validation finishing, in which the lane is
    legitimately still armed — without a real full build on a daemon thread
    outliving a ``tmp_path`` root pytest is about to delete. For the LANE cases it
    hands the test the real ``build`` callable the production wiring supplied, so
    the window can be run synchronously, on this thread, exactly as production
    runs it.
    """

    requests: list[dict] = []

    def record(cached, *, caller, build, adopt=None):
        requests.append(
            {"cached": cached, "caller": caller, "build": build, "adopt": adopt}
        )
        return True

    monkeypatch.setattr(core_cache, "maybe_start_shadow_validation", record)
    return requests


def _seed_workspace(workspace_id: str, name: str) -> None:
    WorkspaceStore().create(name=name, workspace_id=workspace_id)


def _delete_workspace(workspace_id: str) -> None:
    """Remove the durable row the way an operator's delete leaves the store."""

    paths.workspace_path(workspace_id).unlink()


def _workspace_ids(core: dict) -> set[str]:
    return {
        row.get("id")
        for row in (core.get("workspaces") or [])
        if isinstance(row, dict)
    }


def _core_offset(core: dict) -> int | None:
    watermark = (core.get("parity") or {}).get("watermark") or {}
    value = watermark.get("event_offset")
    return None if value is None else int(value)


def _append_uncoverable_event(index: int) -> None:
    """One event the patch lane cannot express, so the batch DEMOTES.

    ``state.reconciled`` is not a covered domain entity, so
    ``batch_is_patch_coverable`` rejects it and the batch takes the full-core
    lane — which is the lane this file is about. Driving the demote through a
    real appended event rather than by forcing the branch keeps the case on the
    same path the incident took.
    """

    EventLog().append(
        Event(
            ts=datetime(2026, 8, 21, 15, 33, index % 60, tzinfo=timezone.utc),
            type="state.reconciled",
            task_id=f"task_mcfq1_{index}",
            run_id=None,
            persona_id="dev",
            payload={"fingerprint": f"mcfq1-{index}"},
        )
    )


def converge_persisted_core(*, limit: int = 4) -> None:
    """Build until the persisted key describes the SETTLED store.

    Same helper and same bound as ``test_core_fingerprint_cache``'s: two builds
    are normally needed on a virgin store because the build is not a pure reader
    (it materializes persona-instance rows and creates the chat SessionDB) while
    the key is taken pre-build on purpose. A cache that never converged would
    make every case here pass by rebuilding, so the bound fails loudly.
    """

    for _ in range(limit):
        core_cache.core_path().unlink(missing_ok=True)
        core_cache.sidecar_path().unlink(missing_ok=True)
        core_cache.reset_process_state()
        build_snapshot()
        if core_cache.read_persisted_core().matched:
            core_cache.reset_process_state()
            return
    raise AssertionError(
        "the persisted core's fingerprint never converged, so no case in this "
        "file can reach the cache-hit boot it is written about"
    )


@pytest.fixture
def booted_on_the_cache(shadow_requests):
    """A serve process whose boot was answered from the persisted core.

    The precondition of the whole incident, asserted rather than assumed: if this
    boot ever stops being a cache hit, every case below would be exercising an
    ordinary rebuild and would pass without touching the defect.
    """

    _seed_workspace(WS_PRE, "the-agent-that-was-already-here")
    converge_persisted_core()
    booted = build_snapshot(build_info={"caller": "hydrate"})
    assert booted["parity"]["core_source"] == core_cache.CORE_SOURCE_CACHE, (
        "the boot was not answered from the cache, so this fixture is not the "
        "situation the incident happened in"
    )
    assert WS_PRE in _workspace_ids(booted)
    assert shadow_requests, "the shadow-validation window was never requested"
    return shadow_requests[-1]


# --------------------------------------------------------------------------- #
# 1. The frame-level invariant: a core must reach the offset it is stamped with
# --------------------------------------------------------------------------- #
def test_a_demoting_batch_never_ships_a_core_that_predates_its_offset(
    isolate_agent_runtime_root, shadow_requests, caplog
):
    """The regression, driven through the real producer.

    A store write lands during a serve's life; the batch that follows demotes to
    the full-core lane; the frame that lane emits must not be a core that lacks
    the write. BOTH entities are asserted: the pre-existing one survives (a fix
    that stopped shipping cores would fail here) and the new one arrives (the
    reported symptom).

    *Kill:* stamp the batch's offset onto whatever core the builder returns. The
    frame then carries the 14:56 core under a 15:33 offset, ``WS_NEW`` is absent,
    and the launcher — which applies a ``delta`` core wholesale — erases the
    agent the operator just dragged in.

    WHY THE STALE CORE HAS TO BE ARRANGED NOW (R1, 2026-08-21)
    =========================================================

    It did not, when this case was written: an append moved the store, the boot
    memo could not see it, and ``build_snapshot()`` on the demote lane handed
    back the boot-time core all by itself. R1 put the event log's logical tail
    into the consult stamp, so the very append that produces this batch now drops
    the memo and the demote lane pays for a real build — the shape this case
    reproduces has **no naturally reachable producer left in this scenario.**

    That is exactly what makes the frame-level guard belt-and-braces rather than
    dead, and it is why the arrangement below is a pin and not a stub: freezing
    the position core_cache reads restores the pre-R1 memo behaviour BYTE FOR
    BYTE — the same judgement, answering the same riders, about a store that has
    moved underneath it — which is the 15:33 producer and equally any future
    stale-core source (a shadow gap, a coalescer handing back an older build, the
    next cache bug). The guard has to refuse it whatever produced it. Everything
    downstream of the pin — the drain, the build, the guard, the frame — is the
    production path unmodified.
    """

    _seed_workspace(WS_PRE, "the-agent-that-was-already-here")
    converge_persisted_core()

    frames = stream_frames(
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=60,
        delta_debounce_seconds=0.01,
        max_frames=2,
    )
    hydrate = drain_boot_liveness(frames)
    assert hydrate["type"] == "hydrate"
    assert hydrate["core"]["parity"]["core_source"] == core_cache.CORE_SOURCE_CACHE, (
        "the boot hydrate was rebuilt, so this case never entered the window the "
        "incident happened in"
    )
    assert _workspace_ids(hydrate["core"]) == {WS_PRE}
    assert shadow_requests, "the shadow-validation window was never requested"

    # The boot's view of the store's position — what the memo recorded, and what
    # the pre-R1 stamp would keep answering from for the rest of the window.
    boot_position = core_cache._store_position()
    assert boot_position is not None

    # The operator's gesture: a durable row that did not exist at boot, and the
    # events that carry it into the stream.
    _seed_workspace(WS_NEW, "the-agent-dragged-in-at-15-33")
    _append_uncoverable_event(0)
    _append_uncoverable_event(1)

    with pytest.MonkeyPatch.context() as pre_r1_memo:
        pre_r1_memo.setattr(
            core_cache, "events_position", lambda: {"event_offset": boot_position}
        )
        with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
            delta = next(frames)
    assert delta["type"] == "delta", delta["type"]

    # The refusal is on the record with both numbers, because "the agent came
    # back" is not by itself evidence that anything was refused — a run where the
    # cache happened to miss would look identical from the frame alone.
    closed = [
        record.getMessage()
        for record in caplog.records
        if "snapshot_core_cache_lane_closed" in record.getMessage()
    ]
    assert closed, (
        "the lane was never closed, so the frame below is fresh for some reason "
        "other than the guard under test"
    )
    assert "reason=core_behind_frame" in closed[0], closed[0]
    assert "core_offset=" in closed[0] and "frame_offset=" in closed[0], closed[0]

    core = delta["core"]
    frame_offset = int(delta["watermark"]["event_offset"])
    core_offset = _core_offset(core)

    assert WS_NEW in _workspace_ids(core), (
        "the demote lane shipped a core built before the row existed, stamped at "
        f"offset {frame_offset} — the launcher applies this core wholesale, so "
        "the agent the operator just created is erased from the canvas"
    )
    assert WS_PRE in _workspace_ids(core), (
        "the pre-existing agent is missing too, so this is not the fix under "
        "test — a producer that ships nothing passes the assertion above"
    )
    assert core_offset is not None and core_offset >= frame_offset, (
        f"the frame claims offset {frame_offset} for a core whose own content "
        f"reaches only {core_offset}: authoritative-for-an-offset-it-predates is "
        "the whole defect, whatever the core happens to contain this run"
    )


def test_a_removal_made_during_the_serve_still_reaches_the_client(
    isolate_agent_runtime_root, shadow_requests
):
    """The other direction, and the reason this file is not a row-pinning fix.

    A guard that carried the held roster forward — or that merged the cached core
    with whatever it was missing — would satisfy the case above and would freeze
    every DELETE out of the stream forever. The demoting frame has to be the
    store's current answer, including its absences.

    *Kill:* fix the case above by carrying rows the fresh core no longer has.
    ``WS_PRE`` then survives its own deletion and the operator cannot remove an
    agent from Mission Control at all.
    """

    _seed_workspace(WS_PRE, "the-agent-that-was-already-here")
    _seed_workspace(WS_NEW, "the-agent-to-be-removed")
    converge_persisted_core()

    frames = stream_frames(
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=60,
        delta_debounce_seconds=0.01,
        max_frames=2,
    )
    hydrate = drain_boot_liveness(frames)
    assert hydrate["type"] == "hydrate"
    assert _workspace_ids(hydrate["core"]) == {WS_PRE, WS_NEW}

    _delete_workspace(WS_NEW)
    _append_uncoverable_event(0)

    delta = next(frames)
    assert delta["type"] == "delta", delta["type"]
    served = _workspace_ids(delta["core"])
    assert WS_NEW not in served, (
        "a deleted row survived the demoting frame, so this lane pins rows in "
        "place instead of shipping the store's current answer"
    )
    assert WS_PRE in served, "the surviving row was dropped as well"


# --------------------------------------------------------------------------- #
# 2. The root: the armed window must END on a cache-HIT boot too
# --------------------------------------------------------------------------- #
def test_the_shadow_validation_closes_the_armed_window_when_it_agrees(
    isolate_agent_runtime_root, booted_on_the_cache
):
    """The memo's stated bound, made true.

    ``_consult_memo``'s soundness argument rests on "the window is a BOOT — it
    ends at the first completed full build of the process". On a cache-hit boot
    no build completes through ``build_snapshot``, and the only full build that
    DOES run — the shadow validation — closed the lane on divergence alone. So on
    an AGREEING validation the window never ended, and the memo answered every
    later build in that serve process with the boot-time core.

    *Kill:* let an ok=true verdict return without closing the lane. The row
    created after the boot is then invisible to every later build for the life of
    the process — 9 minutes in the incident, indefinitely in principle.
    """

    request = booted_on_the_cache

    # The window as production runs it, on this thread: the real ``build``
    # callable the wiring handed over, compared against the core that was served.
    section = core_cache.shadow_validate(
        request["cached"], caller=request["caller"], build=request["build"]
    )
    assert section is None, (
        f"the shadow validation diverged on {section!r}, so this case is about "
        "the divergent path, which already closed the lane"
    )
    assert not core_cache.lane_armed(), (
        "the armed window survived a completed full build, so the memo's own "
        "safety bound is still vacuous on a cache-hit boot"
    )

    _seed_workspace(WS_NEW, "the-agent-dragged-in-after-the-boot")
    served = _workspace_ids(build_snapshot(build_info={"caller": "demote"}))
    assert WS_NEW in served, "a store write after the boot is still invisible"
    assert WS_PRE in served, "the pre-existing row was dropped by the rebuild"


def test_a_shadow_validation_that_could_not_run_also_closes_the_window(
    isolate_agent_runtime_root, booted_on_the_cache
):
    """A validation that RAISED is not a licence to serve the cache forever.

    The build can fail for reasons that have nothing to do with the cache — a
    transient store fault, a share violation on this runtime's platform. Leaving
    the lane armed there would reinstate the exact defect through the one path
    that has no receipt saying the core was ever checked.

    *Kill:* close the lane only on the two verdicts that produced a core. The
    failure path then keeps the boot-time core authoritative for the life of the
    process, and the only evidence is one ``ok=false`` line.
    """

    request = booted_on_the_cache

    def explodes() -> dict:
        raise RuntimeError("the shadow build could not run")

    assert (
        core_cache.shadow_validate(
            request["cached"], caller=request["caller"], build=explodes
        )
        is None
    )
    assert not core_cache.lane_armed(), (
        "a shadow build that raised left the lane armed, so this process would "
        "keep serving a core nothing ever validated"
    )

    _seed_workspace(WS_NEW, "the-agent-dragged-in-after-the-boot")
    served = _workspace_ids(build_snapshot(build_info={"caller": "demote"}))
    assert WS_NEW in served, "a store write after the boot is still invisible"
    assert WS_PRE in served, "the pre-existing row was dropped by the rebuild"


# --------------------------------------------------------------------------- #
# 3. The saving this must not give up
# --------------------------------------------------------------------------- #
def test_the_memo_still_answers_a_boot_where_nothing_moved(
    isolate_agent_runtime_root, shadow_requests, monkeypatch
):
    """The perf win is bounded, not surrendered.

    The memo exists because a boot asks the same question four or five times
    within about a second (prewarm, hydrate, hub, cli) and each ask used to walk
    the whole store. Narrowing the window must not turn those riders back into
    full builds — that is the ~2 s cache-hit boot the operator measures against
    6.5–7.6 s of rebuild.

    The witness is a COUNT of reads of the persisted pair, taken in this test's
    own wrapper, because the number of times the shared computation ran is the
    thing the memo is for and it cannot be forged by a provenance label.
    """

    _seed_workspace(WS_PRE, "the-agent-that-was-already-here")
    converge_persisted_core()

    reads: list[int] = []
    real_read_pair = core_cache._read_pair

    def counted_read_pair():
        reads.append(1)
        return real_read_pair()

    monkeypatch.setattr(core_cache, "_read_pair", counted_read_pair)

    riders = ["prewarm", "hydrate", "hub", "cli"]
    for rider in riders:
        core = build_snapshot(build_info={"caller": rider})
        assert core["parity"]["core_source"] == core_cache.CORE_SOURCE_CACHE, (
            f"rider {rider!r} paid for a full build, so the boot's shared "
            "consult no longer holds and the cache-hit boot is gone"
        )
        assert _workspace_ids(core) == {WS_PRE}

    assert len(reads) == 1, (
        f"the persisted pair was read {len(reads)} times for {len(riders)} "
        "riders of one boot: the shared consult is no longer shared"
    )


def test_the_memo_is_dropped_the_moment_the_window_closes(
    isolate_agent_runtime_root, booted_on_the_cache
):
    """The narrowing is structural, not a re-judgement each ask.

    A closed lane that left the memoised bytes standing would be a second copy of
    the same hazard waiting for a caller that reaches ``_armed_window_read``
    another way.

    *Kill:* close the lane without dropping the memo.
    """

    assert core_cache._consult_memo is not None, (
        "the boot never filled the shared consult, so there is nothing here to "
        "prove is dropped"
    )
    request = booted_on_the_cache
    core_cache.shadow_validate(
        request["cached"], caller=request["caller"], build=request["build"]
    )
    assert core_cache._consult_memo is None


# --------------------------------------------------------------------------- #
# 4. R1 — the memo may not answer "current" about a store it never consulted
# --------------------------------------------------------------------------- #
def test_an_append_inside_the_armed_window_drops_the_boot_consult_memo(
    isolate_agent_runtime_root, shadow_requests, monkeypatch
):
    """The other half of §1's chain, and the half the window fix does not reach.

    Closing the armed window on an agreeing shadow bounds the memo's LIFETIME; it
    says nothing about what the memo may claim INSIDE that window. A boot's
    riders arrive over about a second — the stale-first read, then prewarm,
    hydrate, hub and cli — and the store is live the whole time. A memo keyed on
    the persisted pair's own stat cannot see an append: the pair does not move
    when the log does, so the judgement computed for the first rider keeps
    answering ``matched`` for every rider behind it, about a store that has since
    moved. That is the mtime anti-pattern keyed on the ARTIFACT rather than on
    the source — one step worse than the classic one, because the artifact is not
    even the thing being asked about.

    The witness is a COUNT of reads of the persisted pair, the same observable
    the memo's own perf case uses, because "did this ask look again" is exactly
    what a memo decides and it cannot be forged by a provenance label.

    *Kill:* drop the event log's logical tail out of the consult stamp. The
    second ask is then answered from the pre-append judgement — one read for two
    asks, across a write — which is the 15:33 shape at boot scale.
    """

    _seed_workspace(WS_PRE, "the-agent-that-was-already-here")
    converge_persisted_core()

    reads: list[int] = []
    real_read_pair = core_cache._read_pair

    def counted_read_pair():
        reads.append(1)
        return real_read_pair()

    monkeypatch.setattr(core_cache, "_read_pair", counted_read_pair)

    first = core_cache.consult(caller="hydrate")
    assert first.core is not None, (
        "the boot's first consult was not a cache hit, so there is no warm memo "
        "here for an append to have to invalidate"
    )
    assert len(reads) == 1, reads

    # The operator's gesture, mid-boot: the store moves, the persisted pair does
    # not. Nothing republishes the cache, so the pair's three stats are
    # byte-identical to what the memo recorded.
    before = core_cache._pair_stamp()
    _append_uncoverable_event(0)
    assert core_cache._pair_stamp() == before, (
        "the append moved the persisted pair, so this case would pass on the "
        "pre-R1 stamp and proves nothing about the store being consulted"
    )

    core_cache.consult(caller="hub")
    assert len(reads) == 2, (
        f"the persisted pair was read {len(reads)} time(s) across an append: the "
        "second rider was answered by a judgement taken before the write, so the "
        "memo is claiming 'current' about a store it never consulted"
    )


def test_the_reconsult_after_an_append_is_a_fresh_look_not_a_forced_rebuild(
    isolate_agent_runtime_root, shadow_requests, monkeypatch
):
    """R1 re-CONSULTS. It does not make the event log a validity authority.

    The module header's standing rule is that the fingerprint decides validity
    full stop and ``event_offset`` is never an input to the match decision. The
    stamp obeys it: the position decides whether the memoised ANSWER may be
    reused, and when it may not, the walk runs and the walk decides. So a store
    whose log moved while no build input the walk cares about did gets a fresh
    judgement and, if that judgement matches, a cache HIT — not a rebuild.

    Driven by moving the position alone: the append lands in a store the
    fingerprint has been asked to ignore, so the two questions are separated on
    real bytes rather than by argument.

    *Kill:* invalidate on position by DEMOTING instead of by re-judging. Every
    boot on a live store then pays a full rebuild and the ~2 s cache-hit boot is
    gone.
    """

    _seed_workspace(WS_PRE, "the-agent-that-was-already-here")
    converge_persisted_core()

    reads: list[int] = []
    real_read_pair = core_cache._read_pair

    def counted_read_pair():
        reads.append(1)
        return real_read_pair()

    monkeypatch.setattr(core_cache, "_read_pair", counted_read_pair)

    assert core_cache.consult(caller="hydrate").core is not None
    assert len(reads) == 1, reads

    # A position that moved without any build input moving. Real bytes on the
    # real authority the stamp reads through, so the two questions are told apart
    # by the code under test rather than by the test's own arrangement.
    moved = int(core_cache._store_position() or 0) + 4096
    with pytest.MonkeyPatch.context() as position:
        position.setattr(
            core_cache, "events_position", lambda: {"event_offset": moved}
        )
        again = core_cache.consult(caller="hub")

    assert len(reads) == 2, (
        "the moved position did not force a fresh look, so this case is not "
        "exercising the invalidation it is written about"
    )
    assert again.core is not None and not again.demoted, (
        "a store position that moved while no build input did was answered with "
        "a demote: the event log has become a second validity authority, which "
        "is the design this module's header refuses"
    )
    assert WS_PRE in _workspace_ids(again.core)


def test_a_build_key_is_never_inherited_across_an_append(
    isolate_agent_runtime_root, shadow_requests
):
    """``pre_build_fingerprint`` reuses the consult's key — but only its OWN store.

    The key a build persists is deliberately taken BEFORE the build, so it is at
    worst older than the core it describes. Inheriting it from a judgement made
    before an append breaks that direction in the one way the function's own
    docstring calls unsafe: the persisted key would describe a store the build
    was no longer reading, and the NEXT process would be served a core missing
    the write as authoritative.

    *Kill:* compare only the pair's stats here. The pre-append key is then handed
    to a post-append build.
    """

    _seed_workspace(WS_PRE, "the-agent-that-was-already-here")
    converge_persisted_core()

    assert core_cache.consult(caller="hydrate").core is not None
    inherited = core_cache.pre_build_fingerprint()
    assert inherited is not None

    _append_uncoverable_event(0)
    after = core_cache.pre_build_fingerprint()

    assert after is not None
    assert after.digest != inherited.digest, (
        "the build was handed the key computed before the append: that key "
        "describes a store this build is not reading, and write_back would "
        "persist it as the description of the core it produces"
    )
