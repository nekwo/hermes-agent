"""Acceptance: a narrow subscriber stops demoting the wide one beside it.

Gateway R10's recorded consequence, assigned to Stage 5 and closed here, proven
against a REAL ``serve_loop`` with the REAL producer, two real socket clients and
a real event log — not against the split gate's unit lane, which agrees with
itself by construction.

Read the two tests as one sentence and its control:

* **the split** — a desktop declaring ``office_actor`` and a phone declaring the
  chat-first subset sit in one room; an office write lands; the desktop is handed
  a ``patch`` and the phone a full-core ``delta``, off ONE producer and ONE
  build;
* **the pin** — the same write, one subscriber, and the frames are the ones this
  lane emitted before per-subscriber promotion existed. A change that made the
  split unconditional would pass the first test and fail this one, which is the
  whole reason it is here.

The phone's core is not a regression it pays for the desktop: it is exactly the
frame the intersection rule sent it, and the desktop's patch is the frame the
intersection rule took AWAY from the desktop. Nobody is worse off and one client
stops paying ~1 MB for its neighbour's declaration.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime.state_patches import (
    OFFICE_ACTOR_ENTITY,
    PATCH_OP_UPSERT,
    STATE_PATCHED_EVENT_TYPE,
)
from agent_runtime.stream import FOLD_VARIANTS_FRAME_TYPE
from tests.agent_runtime.test_serve_socket_lane import (
    WAIT,
    _read_until,
    client,
    running_serve,
)

#: What a Stage 5 phone declares: chat + roster + readiness, and nothing about an
#: office canvas it does not render.
PHONE_FOLD = ["persona_instance", "incident"]

#: What the desktop launcher declares today (`kMissionFoldDeclaredEntities`,
#: trimmed to the entities this test's write actually touches).
DESKTOP_FOLD = ["persona_instance", "incident", OFFICE_ACTOR_ENTITY]


@pytest.fixture
def patch_lane_on(monkeypatch):
    """Turn ``read_model.delta_patches`` on for the producer AND the stream lane.

    Same two symbols ``test_stream_patch.py``'s fixture moves, and for the same
    reason: the flag is read in two places and a test that flipped one would be
    measuring a lane that is half on.
    """

    from agent_runtime import state_patches as sp
    from agent_runtime import stream as st
    from agent_runtime.config import load_agent_runtime_config

    def _loader(*args, **kwargs):
        cfg = load_agent_runtime_config(*args, **kwargs)
        cfg.read_model.delta_patches = True
        return cfg

    monkeypatch.setattr(sp, "load_root_runtime_config", _loader)
    monkeypatch.setattr(st, "delta_patches_enabled", lambda config=None: True)


def _append_office_move(index: int = 0) -> None:
    """One office actor MOVE — the write a desktop folds and a phone does not.

    A plain move upsert deliberately: it stays coverable under bare
    ``office_actor`` with no capability token, so the test is about the ENTITY
    negotiation this stage changed rather than about the two lifecycle gates.
    """

    log = EventLog()
    ts = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=index)
    log.append(
        Event(
            ts=ts,
            type=STATE_PATCHED_EVENT_TYPE,
            task_id=None,
            run_id=None,
            persona_id=None,
            payload={
                "entity": OFFICE_ACTOR_ENTITY,
                "id": f"actor_{index}",
                "op": PATCH_OP_UPSERT,
                "changed": {"x": 4 + index, "y": 7},
            },
        )
    )
    log.append(
        Event(
            ts=ts,
            type="office.actor.upserted",
            task_id=None,
            run_id=None,
            persona_id=None,
            payload={"workspace_id": "ws", "actor_key": f"actor_{index}"},
        )
    )


def _collect(connection, wanted: set[str], *, limit: int = 200) -> list[dict]:
    """Read frames until one of *wanted* types arrives; return everything read.

    Bounded by FRAME COUNT rather than by a clock, like the lane suite's own
    ``_read_until``: the socket read already carries the connection's timeout, so
    a second deadline here would only make a slow build look like a missing
    frame.
    """

    seen: list[dict] = []
    for _ in range(limit):
        frame = connection.read_frame()
        if frame is None:
            raise AssertionError(
                f"connection closed before {sorted(wanted)}; saw "
                f"{[f.get('type') or f.get('event') for f in seen]}"
            )
        seen.append(frame)
        if frame.get("type") in wanted:
            return seen
    raise AssertionError(
        f"no {sorted(wanted)} within {limit} frames; saw "
        f"{[f.get('type') or f.get('event') for f in seen]}"
    )


def _drain_producers() -> None:
    """Wait out the real producer threads, for the reason the parity suite's own
    drain records: one parked past the test outlives the isolated root."""

    def _live():
        return [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("serve-stream-producer-") and thread.is_alive()
        ]

    if not _live():
        return
    _append_office_move(99)
    deadline = time.monotonic() + WAIT
    while _live() and time.monotonic() < deadline:
        time.sleep(0.02)


def test_a_narrow_phone_does_not_demote_the_wide_desktop_beside_it(patch_lane_on):
    try:
        with running_serve() as handle:
            with client(handle, name="desktop") as (desktop, _d), client(
                handle, name="phone"
            ) as (phone, _p):
                desktop.send(
                    {
                        "op": "subscribe",
                        "lane": "stream",
                        "fold_entities": DESKTOP_FOLD,
                    }
                )
                phone.send(
                    {"op": "subscribe", "lane": "stream", "fold_entities": PHONE_FOLD}
                )
                # Both baselines first, and the PHONE's is the ordering fact
                # that matters: its subscribe bumped the generation, so once its
                # hydrate has arrived the current producer has passed its
                # baseline and the append below lands in a BATCH rather than
                # inside a core still being built. The desktop may still have a
                # superseded generation's hydrate queued behind it; `_collect`
                # reads past anything that is not content, which is why this does
                # not have to know which generation it just read.
                _collect(desktop, {"hydrate"})
                _collect(phone, {"hydrate"})

                _append_office_move()

                desktop_frames = _collect(desktop, {"patch", "delta"})
                phone_frames = _collect(phone, {"patch", "delta"})

        desktop_content = desktop_frames[-1]
        phone_content = phone_frames[-1]

        # THE ROW. One write, one producer, one build — two answers.
        assert desktop_content["type"] == "patch"
        assert phone_content["type"] == "delta"

        # The desktop's patch really carries the office row (anti-vacuity: a
        # patch frame with no office entry would satisfy the type assertion and
        # mean the promotion never happened).
        entities = {row.get("entity") for row in desktop_content["patches"]}
        assert OFFICE_ACTOR_ENTITY in entities

        # The phone's core is a REAL core, and its watermark is the batch's, so
        # it is a baseline rather than a frame that advances a cursor over
        # nothing.
        assert phone_content.get("core")
        assert (
            phone_content["watermark"]["event_offset"]
            == desktop_content["watermark"]["event_offset"]
        )

        # And neither of them was ever shown the internal envelope.
        for frame in desktop_frames + phone_frames:
            assert frame.get("type") != FOLD_VARIANTS_FRAME_TYPE

        # The measurement the plan's R7 line asks for: what each side paid.
        import json

        desktop_bytes = len(json.dumps(desktop_content, default=str))
        phone_bytes = len(json.dumps(phone_content, default=str))
        # Printed rather than only asserted: the ratio is the number the plan's
        # R7 line is about, and a test that proves an inequality without ever
        # showing the magnitude leaves the receipt to be reconstructed by hand.
        print(
            f"[promotion] desktop patch={desktop_bytes}B  "
            f"phone core={phone_bytes}B  "
            f"ratio={phone_bytes / max(1, desktop_bytes):.1f}x"
        )
        assert desktop_bytes < phone_bytes, (
            f"promotion bought nothing: patch={desktop_bytes}B core={phone_bytes}B"
        )
    finally:
        _drain_producers()


def test_one_subscriber_gets_exactly_the_frames_it_always_got(patch_lane_on):
    """The pin. A room of one cannot disagree with itself, so the union and the
    floor are the same set and the split branch has no input that reaches it."""

    try:
        with running_serve() as handle:
            with client(handle, name="desktop") as (desktop, _d):
                desktop.send(
                    {
                        "op": "subscribe",
                        "lane": "stream",
                        "fold_entities": DESKTOP_FOLD,
                    }
                )
                ack = _read_until(desktop, "subscribed")
                # The per-connection ack is this client's own set, which for one
                # subscriber is what the intersection said too.
                assert ack["fold_entities"] == sorted(DESKTOP_FOLD)

                _collect(desktop, {"hydrate"})
                _append_office_move(1)
                frames = _collect(desktop, {"patch", "delta"})

        assert frames[-1]["type"] == "patch"
        assert all(f.get("type") != FOLD_VARIANTS_FRAME_TYPE for f in frames)
    finally:
        _drain_producers()


def test_a_batch_nobody_folds_still_reaches_both_as_one_core(patch_lane_on):
    """The union does not rescue an uncovered write — both sides take the core,
    which is the honest fallback and the behaviour that must not move."""

    try:
        with running_serve() as handle:
            with client(handle, name="desktop") as (desktop, _d), client(
                handle, name="phone"
            ) as (phone, _p):
                desktop.send(
                    {"op": "subscribe", "lane": "stream", "fold_entities": DESKTOP_FOLD}
                )
                phone.send(
                    {"op": "subscribe", "lane": "stream", "fold_entities": PHONE_FOLD}
                )
                _collect(desktop, {"hydrate"})
                _collect(phone, {"hydrate"})

                # A watchdog reconcile: coverable for nobody, by design.
                EventLog().append(
                    Event(
                        ts=datetime(2026, 8, 27, 12, 5, 0, tzinfo=timezone.utc),
                        type="state.reconciled",
                        task_id=None,
                        run_id=None,
                        persona_id=None,
                        payload={"fingerprint": "fp"},
                    )
                )

                desktop_frames = _collect(desktop, {"patch", "delta"})
                phone_frames = _collect(phone, {"patch", "delta"})

        assert desktop_frames[-1]["type"] == "delta"
        assert phone_frames[-1]["type"] == "delta"
    finally:
        _drain_producers()
