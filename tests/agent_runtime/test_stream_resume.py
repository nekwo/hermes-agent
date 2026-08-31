"""Server-side watermark resume: the journal tail, or an honest hydrate.

Gateway Stage 2 recorded the gap it left — resume is CLIENT-side only, hermes
answers every reattach with a full hydrate — and flagged it as Stage 5's, because
the R7 line names watermark resume as the mitigation to ship BEFORE any snapshot
diet. This is the resolver half: what a client resuming at a watermark is owed,
and every reason it is owed a hydrate instead.

The two headline cases, in the order a phone hits them:

* **already current** — the honoured resume with ZERO frames. A quiet ninety
  seconds in the background used to cost a ~1 MB core on return; it now costs
  nothing, and that is the whole R7 mitigation in one assertion.
* **a short span** — the events the client missed, as the ordinary ``patch``
  frames it already folds, chained ``base_offset``→``watermark`` so its own gap
  gate still applies to them.

Every refusal is asserted by NAME. A resume that silently fell back would be
indistinguishable from one that worked and cost a megabyte, which is exactly the
false-all-clear class this workstream retires.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_runtime import event_rotation
from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime.state_patches import (
    OFFICE_ACTOR_ENTITY,
    PATCH_OP_REFRESH,
    PATCH_OP_UPSERT,
    STATE_PATCHED_EVENT_TYPE,
)
from agent_runtime.stream_resume import (
    STREAM_RESUME_MAX_EVENTS,
    resolve_stream_resume,
)

_TS = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

DESKTOP_FOLD = frozenset({"persona_instance", "incident", OFFICE_ACTOR_ENTITY})
PHONE_FOLD = frozenset({"persona_instance", "incident"})


def _set_patch_lane(monkeypatch, enabled: bool) -> None:
    """Move ``read_model.delta_patches`` through the reader's OWN loader.

    Patching ``delta_patches_enabled`` itself would not reach this module: it is
    bound at import (``from .state_patches import delta_patches_enabled``), so a
    test that rebound the name on ``state_patches`` would be flipping a flag the
    resolver never reads and calling the result a pass.
    """

    from agent_runtime import state_patches as sp
    from agent_runtime.config import load_agent_runtime_config

    def _loader(*args, **kwargs):
        cfg = load_agent_runtime_config(*args, **kwargs)
        cfg.read_model.delta_patches = enabled
        return cfg

    monkeypatch.setattr(sp, "load_root_runtime_config", _loader)


@pytest.fixture
def patch_lane_on(monkeypatch):
    _set_patch_lane(monkeypatch, True)


@pytest.fixture
def patch_lane_off(monkeypatch):
    _set_patch_lane(monkeypatch, False)


def _append_patch(index: int, entity: str = "persona_instance") -> int:
    log = EventLog()
    log.append(
        Event(
            ts=_TS + timedelta(seconds=index),
            type=STATE_PATCHED_EVENT_TYPE,
            task_id=None,
            run_id=None,
            persona_id=None,
            payload={
                "entity": entity,
                "id": f"{entity}_{index}",
                "op": PATCH_OP_UPSERT,
                "changed": {"model": f"m{index}"},
            },
        )
    )
    return event_rotation.log_end_offset()


def _append_uncoverable(index: int) -> int:
    log = EventLog()
    log.append(
        Event(
            ts=_TS + timedelta(seconds=index),
            type="state.reconciled",
            task_id=None,
            run_id=None,
            persona_id=None,
            payload={"fingerprint": f"fp{index}"},
        )
    )
    return event_rotation.log_end_offset()


# --------------------------------------------------------------------------- #
# The floor the journal can actually serve
# --------------------------------------------------------------------------- #
def test_a_pristine_journal_can_be_replayed_from_its_head():
    _append_patch(0)
    assert event_rotation.resume_floor_offset() == 0


def test_a_journal_with_no_events_yet_still_has_a_floor():
    """A brand new install must not be the one runtime that can never resume.

    ``events.jsonl`` does not exist until the first append, and a slice spanning
    zero bytes is not a hole — there is nothing in it to have lost.
    """

    assert event_rotation.resume_floor_offset() == 0


def test_a_deleted_live_slice_is_caught_by_the_TAIL_rather_than_by_the_floor(
    patch_lane_on,
):
    """Measured rather than assumed, and the answer moved the assertion.

    The floor cannot see this one: once the live file is gone, the bytes that
    would say how much it held are the bytes that are missing, so the floor has
    no honest way to distinguish "deleted with content" from "not created yet".
    What DOES catch it is the tail — ``log_end_offset`` collapses to the live
    slice's start — and a client holding a real position is then refused as
    ahead of the journal, which is the true statement about the runtime it is
    talking to.

    The bound this leaves, stated: a client whose watermark is at or below the
    collapsed tail is told there is nothing to replay, and is re-baselined by the
    producer's next core rather than by this path. That is a store somebody
    deleted underneath a running serve, not a case this lane can repair.
    """

    tail = _append_patch(0)
    event_rotation.live_path().unlink()

    resume = resolve_stream_resume(tail, fold_entities=PHONE_FOLD)
    assert not resume.honored
    assert resume.reason == "watermark_ahead_of_journal"


# --------------------------------------------------------------------------- #
# The honoured cases
# --------------------------------------------------------------------------- #
def test_a_client_already_at_the_tail_is_sent_nothing_at_all(patch_lane_on):
    """The R7 mitigation, in the case a phone's foreground actually hits.

    Zero frames is not a degenerate answer — it is the RIGHT one, and it is the
    difference between a resume that saves the megabyte and one that only
    re-spells it.
    """

    tail = _append_patch(0)
    resume = resolve_stream_resume(tail, fold_entities=PHONE_FOLD)
    assert resume.honored
    assert resume.reason is None
    assert resume.frames == []
    assert resume.events == 0
    assert resume.from_offset == resume.to_offset == tail


def test_a_short_span_comes_back_as_the_patch_frames_the_client_folds(patch_lane_on):
    baseline = _append_patch(0)
    _append_patch(1)
    tail = _append_patch(2)

    resume = resolve_stream_resume(baseline, fold_entities=PHONE_FOLD)
    assert resume.honored
    assert resume.events == 2
    assert resume.to_offset == tail
    assert [f["type"] for f in resume.frames] == ["patch"]

    frame = resume.frames[0]
    # Chained from the client's OWN position, so its gap gate still applies:
    # a base_offset that did not equal the held watermark is what makes a
    # resumed span refusable rather than silently mis-applied.
    assert frame["base_offset"] == baseline
    assert frame["watermark"]["event_offset"] == tail
    assert len(frame["patches"]) == 2


def test_the_resumed_span_is_smaller_than_the_hydrate_it_replaces(patch_lane_on):
    """The number the plan asks for, measured rather than assumed."""

    import json

    from agent_runtime.stream import hydrate_frame

    baseline = _append_patch(0)
    _append_patch(1)

    resume = resolve_stream_resume(baseline, fold_entities=PHONE_FOLD)
    assert resume.honored
    resumed_bytes = sum(len(json.dumps(f, default=str)) for f in resume.frames)
    hydrate_bytes = len(json.dumps(hydrate_frame(), default=str))
    print(
        f"[resume] span={resumed_bytes}B  hydrate={hydrate_bytes}B  "
        f"saved={hydrate_bytes - resumed_bytes}B"
    )
    assert resumed_bytes < hydrate_bytes


# --------------------------------------------------------------------------- #
# Every refusal, by name
# --------------------------------------------------------------------------- #
def test_the_patch_lane_being_off_refuses_by_name(patch_lane_off):
    tail = _append_patch(0)
    resume = resolve_stream_resume(tail, fold_entities=PHONE_FOLD)
    assert not resume.honored
    assert resume.reason == "patch_lane_disabled"


def test_a_position_past_the_tail_is_refused_rather_than_clamped(patch_lane_on):
    tail = _append_patch(0)
    resume = resolve_stream_resume(tail + 1000, fold_entities=PHONE_FOLD)
    assert not resume.honored
    assert resume.reason == "watermark_ahead_of_journal"


def test_a_position_older_than_the_journal_takes_the_hydrate(patch_lane_on, monkeypatch):
    tail = _append_patch(0)
    monkeypatch.setattr(event_rotation, "resume_floor_offset", lambda: tail)
    resume = resolve_stream_resume(0, fold_entities=PHONE_FOLD)
    assert not resume.honored
    assert resume.reason == "journal_truncated"


def test_an_unreadable_journal_takes_the_hydrate(patch_lane_on, monkeypatch):
    _append_patch(0)
    monkeypatch.setattr(event_rotation, "resume_floor_offset", lambda: None)
    resume = resolve_stream_resume(0, fold_entities=PHONE_FOLD)
    assert not resume.honored
    assert resume.reason == "journal_unreadable"


def test_a_span_with_an_uncoverable_event_takes_the_hydrate(patch_lane_on):
    """The demote-to-core a live batch would take IS the hydrate at resume time,
    so the fallback is the same mechanism rather than a second one."""

    baseline = _append_patch(0)
    _append_patch(1)
    _append_uncoverable(2)

    resume = resolve_stream_resume(baseline, fold_entities=PHONE_FOLD)
    assert not resume.honored
    assert resume.reason == "span_not_foldable"


def test_a_span_naming_an_entity_this_client_did_not_declare_takes_the_hydrate(
    patch_lane_on,
):
    """Per-subscriber, at the join: the desktop resumes and the phone does not,
    off the same span — which is the same rule the live lane now applies."""

    baseline = _append_patch(0)
    _append_patch(1, entity=OFFICE_ACTOR_ENTITY)

    phone = resolve_stream_resume(baseline, fold_entities=PHONE_FOLD)
    assert not phone.honored
    assert phone.reason == "span_not_foldable"

    desktop = resolve_stream_resume(baseline, fold_entities=DESKTOP_FOLD)
    assert desktop.honored
    assert desktop.events == 1


def test_a_refresh_op_takes_the_hydrate_for_everyone(patch_lane_on):
    baseline = _append_patch(0)
    EventLog().append(
        Event(
            ts=_TS,
            type=STATE_PATCHED_EVENT_TYPE,
            task_id=None,
            run_id=None,
            persona_id=None,
            payload={"entity": "task", "id": "t", "op": PATCH_OP_REFRESH},
        )
    )
    resume = resolve_stream_resume(baseline, fold_entities=DESKTOP_FOLD)
    assert not resume.honored
    assert resume.reason == "span_not_foldable"


def test_a_client_far_enough_behind_is_re_baselined_instead(patch_lane_on):
    """Past the cap the replay stops being cheaper than the core it avoids."""

    baseline = event_rotation.log_end_offset()
    for index in range(STREAM_RESUME_MAX_EVENTS + 2):
        _append_patch(index)
    resume = resolve_stream_resume(baseline, fold_entities=PHONE_FOLD)
    assert not resume.honored
    assert resume.reason == "backlog_exceeds_cap"


@pytest.mark.parametrize("bad", [None, "12", 3.5, True, -1, {"event_offset": 1}])
def test_a_malformed_watermark_is_refused_and_never_read_as_zero(patch_lane_on, bad):
    """An unknown resume position is not byte 0 — the rule ``stream_frames``
    records for its own resume, held at the door a client can reach."""

    _append_patch(0)
    resume = resolve_stream_resume(bad, fold_entities=PHONE_FOLD)
    assert not resume.honored
    assert resume.reason == "invalid_watermark"


# --------------------------------------------------------------------------- #
# The ack payload
# --------------------------------------------------------------------------- #
def test_the_payload_names_the_refusal_and_carries_no_counts(patch_lane_on):
    tail = _append_patch(0)
    refused = resolve_stream_resume(tail + 99, fold_entities=PHONE_FOLD).payload()
    assert refused == {
        "honored": False,
        "reason": "watermark_ahead_of_journal",
        "from_offset": tail + 99,
    }


def test_the_payload_of_an_honoured_resume_says_what_it_carried(patch_lane_on):
    baseline = _append_patch(0)
    tail = _append_patch(1)
    payload = resolve_stream_resume(baseline, fold_entities=PHONE_FOLD).payload()
    assert payload == {
        "honored": True,
        "from_offset": baseline,
        "to_offset": tail,
        "events": 1,
        "frames": 1,
    }
