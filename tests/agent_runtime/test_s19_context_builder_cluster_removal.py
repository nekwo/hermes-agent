"""S19 retires the dead goal/task HUD cluster inside ``context_builder``.

Every symbol named here lost its callers when the mission lane came out
(S4-S12) and the orphan modules followed (S13-S18): the decision menus, the
shape index, the "next required move" role table, the simplified agent HUD and
its evidence/verification readers, the stage/visual predicates, and the
``Task``-declared delivery-directive HUD line. They could only be reached from
each other, so they are removed as a cluster rather than refactored.

Three hollow rings go with them:

* ``mission_hud_preview`` — its two HUD entry points
  (``prompt_observability.snapshot_prompt_observability`` and
  ``runtime_hud.resolve_situational_hud``) can only ever pass ``task=None``
  (``snapshot.py`` builds with ``tasks = []``; the chat lane resolves through
  the permanent ``TaskStoreStub``, whose ``.get()`` always raises ``NotFound``).
  The ``mission_hud`` HUD field row goes with the producer.
* ``_delivery_directive_line`` — residue per the liveness ruling in
  ``docs/agent-runtime-harness/delivery-directive.md``: with no ``Task`` to
  declare one, it always rendered ``DEFAULT_DELIVERY_DIRECTIVE``.
* ``_RECENT_CONTEXT_EVENT_TYPES`` — seven of its eight rows name event types
  S15 de-registered, so no surviving code can emit them.

The keep-side name that survives is the live chat-lane HUD: it must keep
rendering steering edges.

**Corrected by S27.** This stage also kept a "tick-context builder keep set"
(``_mission_hud`` / ``_stage_records`` / ``_safe_packet_projection`` /
``_recent_relevant_events`` / ``_skill_reference_for_action``) on the premise
that they sat on a live render path. They did not: ``build_context`` /
``render_context`` lost their only caller in S5, so the whole path was
test-reachable only. S27 removed the lane; the affected pins below now assert
the correction instead of the superseded premise.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

from agent_runtime import context_builder, prompt_observability, runtime_hud
from agent_runtime.runtime_hud import render_situational_hud_block, resolve_situational_hud


#: The removed cluster. Public name first, then the private helpers that only it
#: and its siblings reached.
REMOVED_CONTEXT_BUILDER_SYMBOLS = (
    "mission_hud_preview",
    "_active_assignment_for_run",
    "_agent_hud_options",
    "_context_expansion_menu",
    "_current_stage_command_hints",
    "_decision_menu",
    "_decision_shape_index",
    "_delivery_directive_line",
    "_diagnostic_persona",
    "_forbidden_decisions",
    "_has_visual_proof_id",
    "_neko_diagnostic_ack_payload",
    "_next_move_from_worker_action",
    "_next_required_move",
    "_proof_gate_status",
    "_recommended_action",
    "_registry_decision_shape_index",
    "_replace_shape_placeholders",
    "_required_next_decision",
    "_role_shape_ids",
    "_safe_proof_event_payload",
    "_simplified_agent_hud",
    "_stage_outgoing_edges",
    "_stage_self_heal_state",
    "_strip_payload_fill_surface",
    "_strip_shape_fill_surface",
    "_task_evidence_stack",
    "_task_has_qa_stage",
    "_task_or_stage_mentions_visual",
    "_task_or_stage_requires_visual",
    "_task_verification_status",
    "_truncate_command_hint",
    "_worker_action_decision_menu",
    "_worker_action_shape_ids",
)

#: Imports the cluster was the only consumer of. A surviving import is a
#: surviving dependency edge, so the gate is the import statement, not the call.
REMOVED_CONTEXT_BUILDER_IMPORTS = (
    "decision_contract_registry",
    "decision_payload_contracts",
    "repo_bundles",
    "role_checklists",
    "role_contracts",
    "simplified_contract",
    "stage_intent",
)


def _instance(**overrides) -> SimpleNamespace:
    base = dict(
        id="personainst_neko",
        persona_id="neko_supervisor",
        role="supervisor",
        display_name="Neko Mission Lead",
        goal_id=None,
        current_task_id=None,
        state="idle",
        mode="configured",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── the removal ─────────────────────────────────────────────────────────────


def test_the_dead_context_builder_cluster_is_gone():
    assert [name for name in REMOVED_CONTEXT_BUILDER_SYMBOLS if hasattr(context_builder, name)] == []


def test_the_cluster_only_imports_are_gone():
    source = Path(context_builder.__file__).read_text(encoding="utf-8")
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    surviving = [
        module
        for module in REMOVED_CONTEXT_BUILDER_IMPORTS
        if any(module in line for line in imports)
    ]
    assert surviving == []


def test_the_mission_hud_preview_entry_points_are_gone():
    """Both HUD entry points could only pass ``task=None``; the field row goes
    with its only producer, so nothing advertises a HUD slice nothing fills."""

    # Gate on the CODE forms (import + call), not on any mention: the removal
    # rationale is recorded in ``runtime_hud``'s module docstring and naming the
    # retired function there is the point.
    for module in (prompt_observability, runtime_hud):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "import mission_hud_preview" not in source, module.__name__
        assert "mission_hud_preview(" not in source, module.__name__
    assert runtime_hud.hud_field("mission_hud") is None
    assert "mission_hud" not in {field.key for field in runtime_hud.HUD_FIELDS}


def test_the_delivery_directive_line_leaves_the_tick_context():
    """Residue per the delivery-directive liveness ruling: with no ``Task`` to
    declare one, the line could only ever restate the contract default.

    S27 finished the job — the renderer this used to inspect (``render_context``)
    went with the whole tick-context lane — so the gate is now that no producer
    of the line survives anywhere in the module."""

    assert not hasattr(context_builder, "_delivery_directive_line")
    assert "delivery_directive" not in Path(context_builder.__file__).read_text(encoding="utf-8")


def test_recent_context_event_types_went_with_the_selector():
    """S19 bounded this set to event types a producer can still emit. S27
    removed ``_recent_relevant_events``, its only consumer, so the set itself is
    gone rather than left advertising a shape nothing selects."""

    assert not hasattr(context_builder, "_RECENT_CONTEXT_EVENT_TYPES")
    assert not hasattr(context_builder, "_recent_relevant_events")


# ── the keep set ────────────────────────────────────────────────────────────


def test_the_live_chat_lane_hud_still_renders_steering_edges():
    """The single HUD authority. Removing the mission slice must not touch the
    steering edges an ordinary chat turn renders."""

    lead = _instance(id="personainst_neko", display_name="Neko Mission Lead")
    child = _instance(id="personainst_dev", display_name="Dev", steered_by=["personainst_neko"])

    child_block = render_situational_hud_block(resolve_situational_hud(child, roster=[lead, child]))
    assert "- Steered by: Neko Mission Lead (@personainst_neko)" in child_block

    lead_block = render_situational_hud_block(resolve_situational_hud(lead, roster=[lead, child]))
    assert "- Steers: Dev (@personainst_dev)" in lead_block


def test_the_s19_tick_context_keep_set_was_superseded_by_s27():
    """S19 kept ``_mission_hud`` / ``_stage_records`` / ``_safe_packet_projection``
    / ``_recent_relevant_events`` / ``_skill_reference_for_action`` because they
    were on the "live render path". They were not: the renderer's own entry
    points (``build_context`` / ``render_context``) had lost their only caller in
    S5, so the whole path was reachable from tests alone. S27 removed the lane;
    this pin records the correction rather than silently dropping it.

    See ``test_s27_context_builder_lane_removal.py`` for the full removal
    contract; ``AgentContext`` is the one surviving export."""

    for name in (
        "_mission_hud",
        "_stage_records",
        "_safe_packet_projection",
        "_recent_relevant_events",
        "_skill_reference_for_action",
    ):
        assert not hasattr(context_builder, name), name
    assert dataclasses.is_dataclass(context_builder.AgentContext)
