"""S6 producer goldens: state-carrying field-patch log entries (flagged, dark).

Covers the four acceptance axes from the plan's S6 producer half:

* flag off (default) → ZERO ``state.patched`` entries; the event stream a steer
  write produces is byte-identical to before S6 (the inertness golden);
* flag on → a steer write emits exactly one entry carrying exactly the changed
  fields; profile / task-transition / incident-close chokepoints likewise;
* an oversize value becomes an accounted ``{oversize, bytes}`` marker (never a
  silent drop) and the payload still fits the 4 KB EventLog cap;
* the ``state.patched`` contract validates strict-green and rejects a payload
  missing a summary field.
"""

from __future__ import annotations

import json

import pytest

from hermes_time import now

from agent_runtime import state_patches as sp
from agent_runtime.config import AgentRuntimeConfig, load_agent_runtime_config
from agent_runtime.decision_contract_registry import allowed_event_types, validate_event_payload
from agent_runtime.events import EVENT_PAYLOAD_LIMIT_BYTES, EventLog
from agent_runtime.models import AgentPersona, Event, Incident, Task
from agent_runtime.persona_assignments import PersonaInstanceStore
from agent_runtime.state_patches import (
    STATE_PATCHED_EVENT_TYPE,
    build_state_patch,
    delta_patches_enabled,
    emit_state_patch,
)
from agent_runtime.states import TaskState
from agent_runtime.store import IncidentStore, TaskStore


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def set_delta_patches(monkeypatch):
    """Flip ``read_model.delta_patches`` for the chokepoints under test.

    The chokepoints read the flag through ``state_patches.load_agent_runtime_config``;
    monkeypatching that one seam covers every emit site at once (mirrors the
    ``history_in_frame_config`` conftest fixture)."""

    def _apply(enabled: bool):
        def _loader(*args, **kwargs):
            cfg = load_agent_runtime_config(*args, **kwargs)
            cfg.read_model.delta_patches = enabled
            return cfg

        monkeypatch.setattr(sp, "load_agent_runtime_config", _loader)

    return _apply


def _persona(persona_id: str) -> AgentPersona:
    return AgentPersona(
        id=persona_id,
        display_name=f"{persona_id} worker",
        role="dev",
        model="gpt-test",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file"],
        system_prompt_path="agent_runtime/prompts/dev.md",
        hermes_profile=f"profile-{persona_id}",
    )


def _event_types() -> list[str]:
    return [evt.type for evt in EventLog().iter_all()]


def _patches() -> list[Event]:
    return [evt for evt in EventLog().iter_all() if evt.type == STATE_PATCHED_EVENT_TYPE]


def _steered_child(store: PersonaInstanceStore):
    """A fresh child steered under a fresh parent — the flagship steer case."""
    parent = store.create_free_floating("profile:parent")
    child = store.create_free_floating("profile:child")
    store.set_parents(child.id, [parent.id])
    return store.get(child.id), parent


# --------------------------------------------------------------------------- #
# Contract registration + validation (strict-green)
# --------------------------------------------------------------------------- #
def test_state_patched_contract_registered():
    assert STATE_PATCHED_EVENT_TYPE in allowed_event_types()
    ok = {"entity": "persona_instance", "id": "personainst_x", "changed": {"steered_by": ["p"]}}
    assert validate_event_payload(STATE_PATCHED_EVENT_TYPE, ok) == ()
    # A payload missing a summary field is caught (renders blank on consumers).
    assert validate_event_payload(STATE_PATCHED_EVENT_TYPE, {"entity": "x", "id": "y"}) == ("changed",)


def test_contract_strict_validation_green(monkeypatch):
    monkeypatch.setenv("HERMES_EVENT_CONTRACT_STRICT", "1")
    log = EventLog()
    # Valid state.patched appends without raising under strict CI posture.
    log.append(
        Event(
            ts=now(),
            type=STATE_PATCHED_EVENT_TYPE,
            task_id=None,
            run_id=None,
            persona_id=None,
            payload={"entity": "task", "id": "t1", "changed": {"state": "done"}},
        )
    )
    # A payload missing the `changed` summary field is rejected under strict mode.
    with pytest.raises(ValueError):
        log.append(
            Event(
                ts=now(),
                type=STATE_PATCHED_EVENT_TYPE,
                task_id=None,
                run_id=None,
                persona_id=None,
                payload={"entity": "task", "id": "t1"},
            )
        )


# --------------------------------------------------------------------------- #
# build_state_patch: shape, oversize accounting, 4 KB fit
# --------------------------------------------------------------------------- #
def test_build_state_patch_shape():
    patch = build_state_patch("persona_instance", "personainst_x", {"steered_by": ["a", "b"], "spawned_by": "a"})
    assert patch == {
        "entity": "persona_instance",
        "id": "personainst_x",
        "changed": {"steered_by": ["a", "b"], "spawned_by": "a"},
    }


def test_build_state_patch_oversize_value_becomes_accounted_marker():
    big = "x" * (EVENT_PAYLOAD_LIMIT_BYTES * 2)
    patch = build_state_patch("persona_instance", "personainst_x", {"blob": big, "spawned_by": "a"})
    marker = patch["changed"]["blob"]
    assert marker["oversize"] is True
    assert marker["bytes"] == len(json.dumps(big)) and marker["bytes"] > EVENT_PAYLOAD_LIMIT_BYTES
    # The within-budget sibling field is untouched (only the overflowing value marked).
    assert patch["changed"]["spawned_by"] == "a"
    # The whole payload now fits the hard cap.
    assert len(json.dumps(patch, ensure_ascii=False).encode("utf-8")) <= EVENT_PAYLOAD_LIMIT_BYTES


def test_build_state_patch_many_midsize_fields_still_fit():
    # Each value is under the per-value budget, but together they overflow — the
    # largest are marked (deterministically) until the payload fits.
    changed = {f"field_{i}": "y" * 1000 for i in range(8)}
    patch = build_state_patch("task", "t1", changed)
    assert len(json.dumps(patch, ensure_ascii=False).encode("utf-8")) <= EVENT_PAYLOAD_LIMIT_BYTES
    markers = [k for k, v in patch["changed"].items() if isinstance(v, dict) and v.get("oversize")]
    assert markers, "expected the largest mid-sized fields to be accounted as oversize markers"


def test_patch_fits_4096_for_realistic_steer_row():
    # A wide fan-in (many parents) is the realistic large steer payload.
    parents = [f"personainst_parent_{i:03d}" for i in range(40)]
    patch = build_state_patch("persona_instance", "personainst_child", {"steered_by": parents, "spawned_by": parents[0]})
    payload_bytes = len(json.dumps(patch, ensure_ascii=False).encode("utf-8"))
    assert payload_bytes <= EVENT_PAYLOAD_LIMIT_BYTES
    # Realistic rows fit inline — no marker needed.
    assert patch["changed"]["steered_by"] == parents


# --------------------------------------------------------------------------- #
# emit_state_patch: gate + inertness (unit)
# --------------------------------------------------------------------------- #
def test_emit_off_is_inert(isolate_agent_runtime_root):
    cfg = load_agent_runtime_config()
    cfg.read_model.delta_patches = False
    assert delta_patches_enabled(cfg) is False
    appended = emit_state_patch(
        EventLog(), entity="task", entity_id="t1", changed={"state": "done"}, config=cfg
    )
    assert appended is False
    assert list(EventLog().iter_all()) == []


def test_emit_empty_changed_is_noop_even_when_on(isolate_agent_runtime_root):
    cfg = load_agent_runtime_config()
    cfg.read_model.delta_patches = True
    assert emit_state_patch(EventLog(), entity="task", entity_id="t1", changed={}, config=cfg) is False
    assert list(EventLog().iter_all()) == []


def test_emit_on_appends_one_patch(isolate_agent_runtime_root):
    cfg = load_agent_runtime_config()
    cfg.read_model.delta_patches = True
    appended = emit_state_patch(
        EventLog(), entity="task", entity_id="t1", changed={"state": "done"}, task_id="t1", config=cfg
    )
    assert appended is True
    patches = _patches()
    assert len(patches) == 1
    assert patches[0].payload == {"entity": "task", "id": "t1", "changed": {"state": "done"}}


# --------------------------------------------------------------------------- #
# Chokepoint: steer (the flagship) — off golden + on emission
# --------------------------------------------------------------------------- #
def test_flag_off_steer_emits_no_patch_byte_identical_stream(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(False)
    store = PersonaInstanceStore()
    child, parent = _steered_child(store)

    # The inertness golden: with the flag off, the steer chokepoint emits exactly
    # what it always did — two creates + one steered — and NOT a single
    # state.patched. The producer diff is provably inert when dark.
    assert _event_types() == [
        "persona_instance.created",
        "persona_instance.created",
        "persona_instance.steered",
    ]
    assert _patches() == []


def test_flag_on_steer_emits_patch_with_exact_changed_fields(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(True)
    store = PersonaInstanceStore()
    child, parent = _steered_child(store)

    patches = _patches()
    assert len(patches) == 1
    patch = patches[0]
    assert patch.payload["entity"] == "persona_instance"
    assert patch.payload["id"] == child.id
    # Exactly the fields the steer wrote: the parent set + its scalar mirror.
    assert patch.payload["changed"] == {"steered_by": [parent.id], "spawned_by": parent.id}
    # The steer chokepoint's own event still rides alongside the patch (additive).
    assert "persona_instance.steered" in _event_types()


def test_flag_on_steer_patch_fits_cap(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(True)
    store = PersonaInstanceStore()
    _steered_child(store)
    patch = _patches()[0]
    payload_bytes = len(json.dumps(patch.payload, ensure_ascii=False).encode("utf-8"))
    assert payload_bytes <= EVENT_PAYLOAD_LIMIT_BYTES


# --------------------------------------------------------------------------- #
# Chokepoint: persona-instance profile/model update
# --------------------------------------------------------------------------- #
def test_flag_on_profile_update_emits_only_changed_field(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(True)
    store = PersonaInstanceStore()
    instance = store.create_free_floating("profile:reviewer")

    store.update_profile(instance.id, display_name="Renamed Reviewer")

    patches = _patches()
    assert len(patches) == 1
    assert patches[0].payload["entity"] == "persona_instance"
    assert patches[0].payload["id"] == instance.id
    assert patches[0].payload["changed"] == {"display_name": "Renamed Reviewer"}


def test_flag_on_profile_model_override_emits_patch(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(True)
    store = PersonaInstanceStore()
    instance = store.create_free_floating("profile:reviewer")

    store.update_profile(instance.id, model="claude-opus-4-8", provider="anthropic")

    changed = _patches()[0].payload["changed"]
    assert changed == {"model": "claude-opus-4-8", "provider": "anthropic"}


def test_flag_off_profile_update_emits_no_patch(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(False)
    store = PersonaInstanceStore()
    instance = store.create_free_floating("profile:reviewer")
    store.update_profile(instance.id, display_name="Renamed Reviewer")
    assert _patches() == []


# --------------------------------------------------------------------------- #
# Chokepoint: task state transition
# --------------------------------------------------------------------------- #
def _task(task_id: str, state: TaskState) -> Task:
    ts = now()
    return Task(
        id=task_id,
        title="patch task",
        description="exercise the transition funnel",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        affected_repos=["hermes-agent"],
        current_stage_id="stage_1",
    )


def test_flag_on_task_transition_emits_state_patch(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(True)
    tasks = TaskStore()
    task = tasks.create(_task("task_patch", TaskState.RUNNING))
    task.state = TaskState.DONE
    tasks.update(task, actor="harness", reason="done")

    patches = [p for p in _patches() if p.payload["entity"] == "task"]
    assert len(patches) == 1
    assert patches[0].payload["id"] == "task_patch"
    assert patches[0].payload["changed"] == {"state": str(TaskState.DONE)}


def test_flag_off_task_transition_emits_no_patch(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(False)
    tasks = TaskStore()
    task = tasks.create(_task("task_patch", TaskState.RUNNING))
    task.state = TaskState.DONE
    tasks.update(task, actor="harness", reason="done")
    assert _patches() == []


# --------------------------------------------------------------------------- #
# Chokepoint: incident close (open-only frame → close is the removal patch)
# --------------------------------------------------------------------------- #
def _incident(incident_id: str) -> Incident:
    return Incident(
        id=incident_id,
        task_id=None,
        run_id=None,
        kind="tool_failure",
        summary="command failed",
        detail_path=None,
        opened_at=now(),
    )


def test_flag_on_incident_close_emits_state_patch(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(True)
    store = IncidentStore()
    store.open(_incident("inc_patch"))
    store.close("inc_patch", reason="resolved")

    patches = [p for p in _patches() if p.payload["entity"] == "incident"]
    assert len(patches) == 1
    assert patches[0].payload["id"] == "inc_patch"
    assert "closed_at" in patches[0].payload["changed"]


def test_flag_off_incident_close_emits_no_patch(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(False)
    store = IncidentStore()
    store.open(_incident("inc_patch"))
    store.close("inc_patch", reason="resolved")
    assert _patches() == []
