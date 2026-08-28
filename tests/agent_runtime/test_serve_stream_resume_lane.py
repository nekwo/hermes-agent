"""Acceptance: a resuming client attaches without re-paying for the hydrate.

The resolver's own suite (``test_stream_resume.py``) proves what a span is worth.
This proves the thing that actually saves the megabyte, and it is a different
claim: that a honoured resume attaches to the RUNNING producer instead of
restarting it, so no hydrate is manufactured for the joiner and none is charged
to the subscribers already in the room.

The arrangement is the real one, not a convenient one. A desktop stays attached
throughout — which is what a phone's serve looks like, since the launcher on that
machine never leaves — and the phone backgrounds and foregrounds against it. A
resume into an EMPTY room is deliberately NOT the case tested: the hub's own
floor starts a producer for a subscriber attached to nothing, that producer opens
with a hydrate, and the resume correctly buys nothing. Said here so the bound is
recorded rather than discovered.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from agent_runtime import event_rotation
from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime.state_patches import PATCH_OP_UPSERT, STATE_PATCHED_EVENT_TYPE
from tests.agent_runtime.test_serve_promotion_two_subscribers import (
    PHONE_FOLD,
    _collect,
    _drain_producers,
    patch_lane_on,  # noqa: F401 - fixture, used by name
)
from tests.agent_runtime.test_serve_socket_lane import _read_until, client, running_serve

_TS = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)


def _append_roster_write(index: int) -> int:
    """One persona-instance upsert — a write BOTH declarations fold, so this
    file measures the resume and never accidentally the promotion."""

    log = EventLog()
    log.append(
        Event(
            ts=_TS + timedelta(seconds=index),
            type=STATE_PATCHED_EVENT_TYPE,
            task_id=None,
            run_id=None,
            persona_id=None,
            payload={
                "entity": "persona_instance",
                "id": f"personainst_{index}",
                "op": PATCH_OP_UPSERT,
                "changed": {"model": f"m{index}"},
            },
        )
    )
    log.append(
        Event(
            ts=_TS + timedelta(seconds=index),
            type="persona_instance.steered",
            task_id=None,
            run_id=None,
            persona_id=None,
            payload={"instance_id": f"personainst_{index}"},
        )
    )
    return event_rotation.log_end_offset()


def test_a_subscribe_that_asks_for_nothing_is_acked_exactly_as_it_always_was():
    """The fixture-stability pin. No ``resume`` key appears anywhere on an ack
    for a subscribe that did not ask for one, which is what keeps the launcher's
    byte-pinned ``subscribed.json`` capture from moving."""

    try:
        with running_serve() as handle:
            with client(handle, name="legacy") as (connection, _r):
                connection.send({"op": "subscribe", "lane": "stream"})
                ack = _read_until(connection, "subscribed")
        assert "resume" not in ack
        assert set(ack) == {
            "event",
            "lane",
            "connection",
            "buffer_limit",
            "fold_entities",
        }
    finally:
        _drain_producers()


def test_a_malformed_resume_is_refused_by_name_rather_than_read_as_absent():
    """A client that meant to resume and was silently re-baselined would pay the
    megabyte it asked not to, with nothing to grep for."""

    try:
        with running_serve() as handle:
            with client(handle, name="phone") as (connection, _r):
                connection.send(
                    {"op": "subscribe", "lane": "stream", "resume": "somewhere"}
                )
                denied = _read_until(connection, "subscribe_denied")
        assert denied["reason"] == "invalid_resume"
    finally:
        _drain_producers()


def test_a_phone_resuming_at_the_tail_gets_no_hydrate_at_all(patch_lane_on):  # noqa: F811
    """The R7 mitigation, end to end, with a real producer nobody restarted."""

    try:
        with running_serve() as handle:
            with client(handle, name="desktop") as (desktop, _d):
                desktop.send(
                    {"op": "subscribe", "lane": "stream", "fold_entities": PHONE_FOLD}
                )
                _collect(desktop, {"hydrate"})

                with client(handle, name="phone") as (phone, _p):
                    phone.send(
                        {
                            "op": "subscribe",
                            "lane": "stream",
                            "fold_entities": PHONE_FOLD,
                        }
                    )
                    _read_until(phone, "subscribed")
                    baseline = _collect(phone, {"hydrate"})[-1]
                    watermark = baseline["watermark"]["event_offset"]
                    # The desktop paid a re-baseline for that join; drain it so
                    # the assertion below is about the RESUME and not about a
                    # frame still in flight from before it.
                    _collect(desktop, {"hydrate"})
                    phone.send({"op": "unsubscribe"})
                    _read_until(phone, "unsubscribed")

                # The phone comes back on a NEW connection, exactly as a
                # foregrounded app does — a resumed lane is not a socket that
                # stayed open.
                with client(handle, name="phone") as (phone, _p2):
                    phone.send(
                        {
                            "op": "subscribe",
                            "lane": "stream",
                            "fold_entities": PHONE_FOLD,
                            "resume": {"event_offset": watermark},
                        }
                    )
                    ack = _read_until(phone, "subscribed")
                    assert ack["resume"]["honored"] is True, ack["resume"]
                    assert ack["resume"]["events"] == 0
                    assert ack["resume"]["frames"] == 0

                    # Nothing was manufactured for it. Prove that with a WRITE
                    # rather than with a silence: an assertion that no hydrate
                    # arrived within N frames is vacuous if no frames arrive at
                    # all, so the next content frame is forced and inspected.
                    _append_roster_write(1)
                    frames = _collect(phone, {"patch", "delta", "hydrate"})

        content = frames[-1]
        assert content["type"] == "patch", (
            "a resumed subscriber was re-baselined: "
            f"{[f.get('type') for f in frames]}"
        )
        assert all(f.get("type") != "hydrate" for f in frames)
        # And it chains onto the position the phone came back with, so its own
        # gap gate had something to check.
        assert content["base_offset"] == watermark
    finally:
        _drain_producers()


def test_a_stale_watermark_takes_the_hydrate_and_says_which_reason(patch_lane_on):  # noqa: F811
    """The fallback is correct, not a failure — and it is NAMED, because a
    resume that silently fell back is indistinguishable from one that worked."""

    try:
        with running_serve() as handle:
            with client(handle, name="desktop") as (desktop, _d):
                desktop.send({"op": "subscribe", "lane": "stream"})
                _collect(desktop, {"hydrate"})

                with client(handle, name="phone") as (phone, _p):
                    phone.send(
                        {
                            "op": "subscribe",
                            "lane": "stream",
                            "fold_entities": PHONE_FOLD,
                            # Past the tail: a position this runtime never had.
                            "resume": {"event_offset": 10_000_000},
                        }
                    )
                    ack = _read_until(phone, "subscribed")
                    frames = _collect(phone, {"hydrate", "patch", "delta"})

        assert ack["resume"]["honored"] is False
        assert ack["resume"]["reason"] == "watermark_ahead_of_journal"
        assert ack["resume"]["from_offset"] == 10_000_000
        # The hydrate really arrived — the fallback is a lane, not a message.
        assert frames[-1]["type"] == "hydrate"
    finally:
        _drain_producers()
