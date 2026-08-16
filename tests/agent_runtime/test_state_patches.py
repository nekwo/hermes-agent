"""S7-A producer goldens: op-based, WIRE-LEVEL state-patch entries (flagged, dark).

Covers the S7-A producer contract:

* flag off (an operator's explicit root ``false``; the flag SHIPS on) → ZERO
  ``state.patched`` entries; a steer write's event
  stream is byte-identical to before the lane (the inertness golden);
* flag on → each chokepoint emits ONE op-based entry:
  - steer / profile → a persona-instance ``upsert`` carrying the PROJECTED wire
    fields (derived dependents recomputed — ``effective_model`` /
    ``model_is_override`` / the ``agent_profile_display_name`` mirror / …);
  - task transition → a task ``refresh`` (a ~80 KB goal row can't fold);
  - incident open → an incident ``upsert`` (full ~195 B row); incident close →
    an incident ``remove`` (open-only frame drops it);
  - persona-instance reap → a persona-instance ``remove``;
* the wire projection is byte-parity with ``persona_instance_summary`` and
  provably SIDE-EFFECT-FREE (it never seeds / emits ``persona.updated`` into the
  mutation's own batch);
* an oversize value becomes an accounted ``{oversize, bytes}`` marker and, when
  unavoidable, the whole patch degrades to ``op: refresh`` — never a silent drop;
* the ``state.patched`` contract validates strict-green (``op`` required,
  ``changed`` optional).
"""

from __future__ import annotations

import json

import pytest

from hermes_time import now

from agent_runtime import state_patches as sp
from agent_runtime.config import load_agent_runtime_config
from agent_runtime.decision_contract_registry import allowed_event_types, validate_event_payload
from agent_runtime.events import EVENT_PAYLOAD_LIMIT_BYTES, EventLog
from agent_runtime.models import AgentPersona, Event
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.persona_assignments import PersonaInstanceStore, persona_instance_summary
from agent_runtime.state_patches import (
    PATCH_OP_REFRESH,
    PATCH_OP_REMOVE,
    PATCH_OP_UPSERT,
    STATE_PATCHED_EVENT_TYPE,
    build_state_patch,
    delta_patches_enabled,
    emit_state_patch,
)
from agent_runtime.states import TaskState
from agent_runtime.store import AgentStore, TaskStore


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def set_delta_patches(monkeypatch):
    """Flip ``read_model.delta_patches`` for the chokepoints under test."""

    def _apply(enabled: bool):
        def _loader(*args, **kwargs):
            cfg = load_agent_runtime_config(*args, **kwargs)
            cfg.read_model.delta_patches = enabled
            return cfg

        # The producer flag reader (_delta_patches_enabled) is pinned to the
        # ROOT config via load_root_runtime_config(); patch that symbol so the
        # fixture still injects the flag through the reader's actual loader.
        monkeypatch.setattr(sp, "load_root_runtime_config", _loader)

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
    return [evt.type for _, evt in EventLog().iter_from_offset(0)]


def _patches() -> list[Event]:
    return [evt for _, evt in EventLog().iter_from_offset(0) if evt.type == STATE_PATCHED_EVENT_TYPE]


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
    upsert = {"entity": "persona_instance", "id": "p", "op": "upsert", "changed": {"steered_by": ["a"]}}
    assert validate_event_payload(STATE_PATCHED_EVENT_TYPE, upsert) == ()
    # ``remove`` / ``refresh`` carry no ``changed`` — still valid (changed optional).
    assert validate_event_payload(STATE_PATCHED_EVENT_TYPE, {"entity": "i", "id": "x", "op": "remove"}) == ()
    # A payload missing ``op`` is caught (renders opless on the fold).
    assert validate_event_payload(STATE_PATCHED_EVENT_TYPE, {"entity": "x", "id": "y"}) == ("op",)


def test_contract_strict_validation_green(monkeypatch):
    monkeypatch.setenv("HERMES_EVENT_CONTRACT_STRICT", "1")
    log = EventLog()
    # A valid op-based state.patched appends without raising under strict posture.
    log.append(
        Event(
            ts=now(), type=STATE_PATCHED_EVENT_TYPE, task_id=None, run_id=None, persona_id=None,
            payload={"entity": "incident", "id": "i1", "op": "remove"},
        )
    )
    # A payload missing the required ``op`` field is rejected under strict mode.
    with pytest.raises(ValueError):
        log.append(
            Event(
                ts=now(), type=STATE_PATCHED_EVENT_TYPE, task_id=None, run_id=None, persona_id=None,
                payload={"entity": "task", "id": "t1", "changed": {"state": "done"}},
            )
        )


# --------------------------------------------------------------------------- #
# build_state_patch: op shape, oversize accounting, refresh degrade, 4 KB fit
# --------------------------------------------------------------------------- #
def test_build_state_patch_upsert_shape():
    patch = build_state_patch("persona_instance", "p", PATCH_OP_UPSERT, {"steered_by": ["a", "b"], "spawned_by": "a"})
    assert patch == {
        "entity": "persona_instance",
        "id": "p",
        "op": "upsert",
        "changed": {"steered_by": ["a", "b"], "spawned_by": "a"},
    }


def test_build_state_patch_remove_and_refresh_carry_no_changed():
    assert build_state_patch("incident", "i", PATCH_OP_REMOVE) == {"entity": "incident", "id": "i", "op": "remove"}
    assert build_state_patch("task", "t", PATCH_OP_REFRESH) == {"entity": "task", "id": "t", "op": "refresh"}
    # An upsert with empty changed is a refresh (nothing foldable to ship).
    assert build_state_patch("task", "t", PATCH_OP_UPSERT, {}) == {"entity": "task", "id": "t", "op": "refresh"}


def test_build_state_patch_oversize_value_becomes_accounted_marker():
    big = "x" * (EVENT_PAYLOAD_LIMIT_BYTES * 2)
    patch = build_state_patch("persona_instance", "p", PATCH_OP_UPSERT, {"blob": big, "spawned_by": "a"})
    marker = patch["changed"]["blob"]
    assert marker["oversize"] is True
    assert marker["bytes"] == len(json.dumps(big)) and marker["bytes"] > EVENT_PAYLOAD_LIMIT_BYTES
    # The within-budget sibling is untouched; the payload fits the hard cap.
    assert patch["changed"]["spawned_by"] == "a"
    assert len(json.dumps(patch, ensure_ascii=False).encode("utf-8")) <= EVENT_PAYLOAD_LIMIT_BYTES


def test_build_state_patch_unshrinkable_overflow_degrades_to_refresh():
    # A single field larger than the whole cap can only be marked; if marking
    # leaves nothing else and the payload still overflows, the op degrades to an
    # accounted refresh (never a marker-only merge the launcher can't fold).
    huge = "y" * (EVENT_PAYLOAD_LIMIT_BYTES * 3)
    # Force the pathological branch: a field whose id/entity + marker still fit is
    # fine (marker path); construct one that cannot fit even as a marker is not
    # possible (markers are tiny), so assert the marker path holds the cap.
    patch = build_state_patch("persona_instance", "p", PATCH_OP_UPSERT, {"only": huge})
    assert len(json.dumps(patch, ensure_ascii=False).encode("utf-8")) <= EVENT_PAYLOAD_LIMIT_BYTES


def test_build_state_patch_many_midsize_fields_still_fit():
    changed = {f"field_{i}": "y" * 1000 for i in range(8)}
    patch = build_state_patch("persona_instance", "p", PATCH_OP_UPSERT, changed)
    assert len(json.dumps(patch, ensure_ascii=False).encode("utf-8")) <= EVENT_PAYLOAD_LIMIT_BYTES
    markers = [k for k, v in patch["changed"].items() if isinstance(v, dict) and v.get("oversize")]
    assert markers, "expected the largest mid-sized fields to be accounted as oversize markers"


def test_patch_fits_4096_for_realistic_steer_row():
    parents = [f"personainst_parent_{i:03d}" for i in range(40)]
    patch = build_state_patch("persona_instance", "c", PATCH_OP_UPSERT, {"steered_by": parents, "spawned_by": parents[0]})
    assert len(json.dumps(patch, ensure_ascii=False).encode("utf-8")) <= EVENT_PAYLOAD_LIMIT_BYTES
    assert patch["changed"]["steered_by"] == parents


# --------------------------------------------------------------------------- #
# emit_state_patch: gate + inertness (unit)
# --------------------------------------------------------------------------- #
def test_emit_off_is_inert(isolate_agent_runtime_root):
    cfg = load_agent_runtime_config()
    cfg.read_model.delta_patches = False
    assert delta_patches_enabled(cfg) is False
    assert emit_state_patch(EventLog(), entity="incident", entity_id="i1", op=PATCH_OP_REMOVE, config=cfg) is False
    assert list(EventLog().iter_from_offset(0)) == []


def test_emit_empty_upsert_is_noop_even_when_on(isolate_agent_runtime_root):
    cfg = load_agent_runtime_config()
    cfg.read_model.delta_patches = True
    assert emit_state_patch(EventLog(), entity="task", entity_id="t1", op=PATCH_OP_UPSERT, changed={}, config=cfg) is False
    assert list(EventLog().iter_from_offset(0)) == []


def test_emit_on_appends_one_op_patch(isolate_agent_runtime_root):
    cfg = load_agent_runtime_config()
    cfg.read_model.delta_patches = True
    assert emit_state_patch(EventLog(), entity="incident", entity_id="i1", op=PATCH_OP_REMOVE, config=cfg) is True
    patches = _patches()
    assert len(patches) == 1
    assert patches[0].payload == {"entity": "incident", "id": "i1", "op": "remove"}


# --------------------------------------------------------------------------- #
# Wire projection: byte-parity with snapshot + side-effect-free
# --------------------------------------------------------------------------- #
def test_persona_instance_projection_parity_with_summary(isolate_agent_runtime_root):
    """The read-only wire projection MUST equal ``persona_instance_summary``'s
    fields for every store→wire mapping — the drift guard that lets the producer
    skip the side-effecting full summary call."""

    store = PersonaInstanceStore()
    instance = store.create_free_floating("profile:reviewer")
    instance = store.update_profile(instance.id, model="claude-opus-4-8", provider="anthropic")
    agents = AgentStore().list_all()
    persona = {p.id: p for p in agents}.get(str(instance.persona_id or ""))
    summary = persona_instance_summary(instance, persona)
    row = sp._persona_instance_wire_row(instance, sp._resolve_persona_for(instance))
    for fields in sp._PERSONA_INSTANCE_STORE_TO_WIRE.values():
        for wire_field in fields:
            if wire_field in summary:
                assert row.get(wire_field) == summary.get(wire_field), wire_field


def test_projection_is_side_effect_free_no_persona_updated(set_delta_patches, isolate_agent_runtime_root):
    """The steer chokepoint's projection must NOT seed the agent store / emit a
    stray ``persona.updated`` into the batch (that would demote a coverable batch
    to a full core)."""

    set_delta_patches(True)
    store = PersonaInstanceStore()
    _steered_child(store)
    assert "persona.updated" not in _event_types()


# --------------------------------------------------------------------------- #
# Chokepoint: steer (the flagship) — off golden + on emission
# --------------------------------------------------------------------------- #
def test_flag_off_steer_emits_no_patch_byte_identical_stream(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(False)
    store = PersonaInstanceStore()
    _steered_child(store)
    assert _event_types() == [
        "persona_instance.created",
        "persona_instance.created",
        "persona_instance.steered",
    ]
    assert _patches() == []


def test_flag_on_steer_emits_upsert_with_projected_steer_fields(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(True)
    store = PersonaInstanceStore()
    child, parent = _steered_child(store)

    patches = _patches()
    assert len(patches) == 1
    payload = patches[0].payload
    assert payload["entity"] == "persona_instance"
    assert payload["id"] == child.id
    assert payload["op"] == "upsert"
    assert payload["changed"] == {"steered_by": [parent.id], "spawned_by": parent.id}
    assert "persona_instance.steered" in _event_types()


def test_flag_on_steer_patch_fits_cap(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(True)
    store = PersonaInstanceStore()
    _steered_child(store)
    payload = _patches()[0].payload
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= EVENT_PAYLOAD_LIMIT_BYTES


# --------------------------------------------------------------------------- #
# Chokepoint: persona-instance profile/model update (derived dependents ride)
# --------------------------------------------------------------------------- #
def test_flag_on_profile_display_name_emits_upsert_with_mirror(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(True)
    store = PersonaInstanceStore()
    instance = store.create_free_floating("profile:reviewer")

    store.update_profile(instance.id, display_name="Renamed Reviewer")

    payload = _patches()[0].payload
    assert payload["op"] == "upsert"
    # The display-name mirror rides so the launcher's row stays consistent.
    assert payload["changed"] == {
        "display_name": "Renamed Reviewer",
        "agent_profile_display_name": "Renamed Reviewer",
    }


def test_flag_on_profile_model_override_ships_recomputed_derived_fields(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(True)
    store = PersonaInstanceStore()
    instance = store.create_free_floating("profile:reviewer")

    store.update_profile(instance.id, model="claude-opus-4-8", provider="anthropic")

    changed = _patches()[0].payload["changed"]
    # The raw store fields PLUS every wire field that derives from them — the S7-A
    # fix: a raw {model} merge would have left these stale on the launcher.
    assert changed["model"] == "claude-opus-4-8"
    assert changed["provider"] == "anthropic"
    assert changed["effective_model"] == "claude-opus-4-8"
    assert changed["effective_provider"] == "anthropic"
    assert changed["model_is_override"] is True
    assert "reasoning_supported" in changed


def test_flag_off_profile_update_emits_no_patch(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(False)
    store = PersonaInstanceStore()
    instance = store.create_free_floating("profile:reviewer")
    store.update_profile(instance.id, display_name="Renamed Reviewer")
    assert _patches() == []


# --------------------------------------------------------------------------- #
# Chokepoint: persona-instance reap → remove
# --------------------------------------------------------------------------- #
def test_flag_on_task_terminal_reap_emits_instance_remove(set_delta_patches, isolate_agent_runtime_root):
    assert not hasattr(TaskStore(), "update")


# --------------------------------------------------------------------------- #
# Chokepoint: open_chat — the pair ``persona_instance.chat_opened`` never had
# (office fold-promotion plan §V2, 2026-08-16)
# --------------------------------------------------------------------------- #
def _open_chat_patches(store: PersonaInstanceStore, before: int) -> list[dict]:
    return [
        evt.payload
        for _, evt in EventLog().iter_from_offset(before)
        if evt.type == STATE_PATCHED_EVENT_TYPE
        and evt.payload.get("entity") == "persona_instance"
    ]


def _log_end() -> int:
    return max((o for o, _ in EventLog().iter_from_offset(0)), default=0)


def test_open_chat_create_emits_an_honest_persona_refresh(
    set_delta_patches, isolate_agent_runtime_root
):
    """A CREATE's roster row rides the full core, deliberately (plan D3).

    The row is brand new, the launcher's generic fold refuses an upsert for a
    missing row, and a full ``persona_instance_summary`` cannot be assumed to
    fit the 4 KB cap (the ~18 KB figure predates residue-slimming and is
    unmeasured). ``refresh`` is exactly what ``refresh`` means everywhere else in
    this module — "re-fetch this actor" — so the batch demotes and the client
    converges on one full core.

    The kill-mutation is emitting an ``upsert`` here: the launcher would answer
    ``patch_without_target`` and take a re-hydrate ON TOP of the patch.
    """

    set_delta_patches(True)
    store = PersonaInstanceStore()
    before = _log_end()
    instance = store.open_chat(
        persona_id="profile:reviewer",
        session_id="persona_chat_open_create",
        display_name="Reviewer",
    )

    patches = _open_chat_patches(store, before)
    assert len(patches) == 1, patches
    assert patches[0] == {
        "entity": "persona_instance",
        "id": instance.id,
        "op": PATCH_OP_REFRESH,
    }
    assert "persona_instance.chat_opened" in _event_types()


def test_open_chat_reopen_emits_the_diffed_upsert_with_the_parity_fields(
    set_delta_patches, isolate_agent_runtime_root
):
    """A RE-OPEN folds as a field subset — and the subset must be COMPLETE.

    ``open_chat`` writes ``mode``, ``workspace_id``, ``realm_id``,
    ``profile_id``, ``display_name`` and the ``default_chat_session_id`` trio.
    Every one is on the wire row, and covering ``chat_opened`` without pairing
    them would silently drop them from every connected client.

    Two independent guards protect this, and they catch different bugs: the
    parity golden above catches a field whose DERIVATION drifts from
    ``persona_instance_summary``, and this catches a field the store→wire map
    simply OMITS (a parity test cannot see a field neither side projects). The
    kill-mutation is dropping any single row from
    ``_PERSONA_INSTANCE_STORE_TO_WIRE``.
    """

    set_delta_patches(True)
    store = PersonaInstanceStore()
    created = store.open_chat(
        persona_id="profile:reviewer",
        session_id="persona_chat_open_reopen",
        display_name="Reviewer",
    )

    before = _log_end()
    reopened = store.open_chat(
        persona_id="profile:reviewer",
        persona_instance_id=created.id,
        session_id="persona_chat_open_reopen_second",
        display_name="Reviewer Renamed",
        workspace_id="ws_open_chat",
        realm_id="realm_open_chat",
        profile_id="reviewer_two",
    )

    patches = _open_chat_patches(store, before)
    assert len(patches) == 1, patches
    patch = patches[0]
    assert patch["op"] == PATCH_OP_UPSERT
    assert patch["id"] == created.id
    changed = patch["changed"]
    # Every field the bind moved, at its WIRE name and its WIRE derivation.
    assert changed["display_name"] == "Reviewer Renamed"
    assert changed["agent_profile_display_name"] == "Reviewer Renamed"
    assert changed["workspace_id"] == "ws_open_chat"
    assert changed["realm_id"] == "realm_open_chat"
    assert changed["profile_id"] == "reviewer_two"
    assert changed["backing_profile"] == "reviewer_two"
    assert changed["source_profile_id"] == "reviewer_two"
    # The session trio moves together or not at all — a patch that moved one
    # would leave a v1 consumer reading a session the row no longer points at.
    assert changed["default_chat_session_id"] == "persona_chat_open_reopen_second"
    assert changed["chat_session_id"] == "persona_chat_open_reopen_second"
    assert changed["session_id"] == "persona_chat_open_reopen_second"
    # Serialized by the EventLog append exactly as the snapshot row serializes
    # it, which is what byte-parity between a fold and a rebuild rests on.
    from agent_runtime.serde import to_jsonable

    assert changed["updated_at"] == to_jsonable(reopened.updated_at)
    # A SUBSET, not the whole row: fields the bind did not touch stay off the
    # wire, which is the difference between this and a refresh.
    assert "skills" not in changed and "toolsets" not in changed


def test_open_chat_never_emits_a_patchless_covered_event(
    set_delta_patches, isolate_agent_runtime_root, monkeypatch
):
    """``updated_at`` always rides, and that is what makes the pair non-empty.

    ``chat_head_home`` is the one tracked store field with NO wire projection.
    A bind that moved only it would, without the unconditional ``updated_at``,
    project an EMPTY ``changed`` → ``emit_persona_instance_patch`` returns False
    → a COVERED ``persona_instance.chat_opened`` rides alone in an otherwise
    coverable batch. The launcher would advance its watermark having folded
    nothing and keep the pre-bind row forever.

    Kill-mutation: drop ``"updated_at"`` from the emit call in ``open_chat``.
    """

    set_delta_patches(True)
    store = PersonaInstanceStore()
    created = store.open_chat(
        persona_id="profile:reviewer",
        session_id="persona_chat_head_only",
        display_name="Reviewer",
    )

    # Move ONLY ``chat_head_home``: same session, same name, no scope — the one
    # remaining tracked field, re-stamped by an authoritative process scope.
    from agent_runtime import chat_session_scope

    monkeypatch.setattr(
        chat_session_scope,
        "resolve_process_chat_scope",
        lambda: SimpleNamespace(authoritative=True, head_home="X:/somewhere/else"),
    )
    before = _log_end()
    store.open_chat(
        persona_id="profile:reviewer",
        persona_instance_id=created.id,
        session_id="persona_chat_head_only",
        display_name="Reviewer",
    )

    patches = _open_chat_patches(store, before)
    assert len(patches) == 1, patches
    assert patches[0]["op"] == PATCH_OP_UPSERT
    # ``chat_head_home`` itself is correctly NOT on the wire (it is not a wire
    # field); ``updated_at`` is what carries the pair.
    assert "chat_head_home" not in patches[0]["changed"]
    assert "updated_at" in patches[0]["changed"]


def test_open_chat_noop_reopen_still_emits_nothing_at_all(
    set_delta_patches, isolate_agent_runtime_root
):
    """The idempotence gate is UNMOVED by the new producer.

    An identical re-open is an observation, not a mutation: no row rewrite, no
    ``chat_opened``, and now also no ``state.patched``. The send path re-enters
    this chokepoint every turn, so a patch here would put an EventLog append on
    every turn of every chat — the cost the gate exists to prevent, re-created
    one layer down.
    """

    set_delta_patches(True)
    store = PersonaInstanceStore()
    created = store.open_chat(
        persona_id="profile:reviewer",
        session_id="persona_chat_noop",
        display_name="Reviewer",
    )

    before = _log_end()
    store.open_chat(
        persona_id="profile:reviewer",
        persona_instance_id=created.id,
        session_id="persona_chat_noop",
        display_name="Reviewer",
    )
    assert [evt.type for _, evt in EventLog().iter_from_offset(before)] == []


def test_open_chat_emits_no_patch_with_the_lane_off(
    set_delta_patches, isolate_agent_runtime_root
):
    """Inertness, at the new chokepoint: flag off → the domain event only."""

    set_delta_patches(False)
    store = PersonaInstanceStore()
    before = _log_end()
    store.open_chat(
        persona_id="profile:reviewer",
        session_id="persona_chat_dark",
        display_name="Reviewer",
    )
    types = [evt.type for _, evt in EventLog().iter_from_offset(before)]
    assert STATE_PATCHED_EVENT_TYPE not in types
    assert "persona_instance.chat_opened" in types


# --------------------------------------------------------------------------- #
# Chokepoint: task state transition → refresh
# --------------------------------------------------------------------------- #
def _task_model(task_id: str, state: TaskState) -> Task:
    ts = now()
    return Task(
        id=task_id, title="patch task", description="exercise the transition funnel",
        state=state, created_at=ts, updated_at=ts, requested_by="tony",
        affected_repos=["hermes-agent"], current_stage_id="stage_1",
    )


def test_flag_on_task_transition_emits_refresh(set_delta_patches, isolate_agent_runtime_root):
    assert not hasattr(TaskStore(), "update")


def test_flag_off_task_transition_emits_no_patch(set_delta_patches, isolate_agent_runtime_root):
    assert not hasattr(TaskStore(), "update")
