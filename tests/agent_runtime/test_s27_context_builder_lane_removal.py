"""S27 retires the tick-context builder: ``build_context`` / ``render_context``.

S5 deleted the dispatch loop and ``persona_runtime.run_tick`` / ``_invoke_agent``
— the only things that ever built a tick context. What survived was the import
line at ``persona_runtime.py:22``, which named all three symbols while the module
used exactly one of them (``AgentContext``, and only as an annotation). An import
is not a call site: with no producer, ``build_context`` and ``render_context``
were reachable from tests alone.

Removing them takes the whole private helper set with them — the renderer, the
repair-hint table, the proof/incident record readers, the packet projections and
the recent-event selector — because every one of those was reachable only from
those two functions (verified by intra-module reachability, below).

``AgentContext`` itself is KEPT: ``persona_runtime`` still annotates its
repo-grounding / tool-budget helpers with it. That is a *second-order* orphan —
with no producer left, those annotated helpers cannot be called on a real
context either — and is recorded as follow-up debt rather than removed here,
because the annotated helpers are outside this slice.

The one thing that must NOT move: ``prompt_observability``'s contract-45
``mission_hud`` field. It is a separate producer the Launcher reads by key, and
it is pinned below so this cut cannot be mistaken for that one.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect

from agent_runtime import context_builder, persona_runtime


#: The removed lane. Public entry points first, then the private helpers only
#: they could reach.
REMOVED_CONTEXT_BUILDER_SYMBOLS = (
    "build_context",
    "render_context",
    "_stage_records",
    "_context_json",
    "_prompt_visible_mission_hud",
    "_validation_repair_hud",
    "_corrected_shape_for_repair",
    "_corrected_shape_from_menu",
    "_parse_repair_payload",
    "_repair_hint_for_message",
    "_is_visual_proof_repair_message",
    "_visual_proof_invalid_field",
    "_proof_records",
    "_format_proof_ids",
    "_incident_records",
    "_incident_run_terminal_state",
    "_safe_repo_context",
    "_safe_task_snapshot",
    "_safe_proof_record",
    "_safe_proof_metadata",
    "_context_objective_stage",
    "_objective_input_artifact",
    "_stage_role",
    "_stage_output_type",
    "_stage_proof_gate",
    "_mission_hud",
    "_terminal_feedback",
    "_latest_context_request_feedback",
    "_skill_reference_for_action",
    "_hud_owner",
    "_safe_packet_projection",
    "_safe_packet_body",
    "_add_cross_stage_source_delivery",
    "_add_cross_stage_qa_review",
    "_packet_targets_persona",
    "_RECENT_CONTEXT_EVENT_TYPES",
    "_recent_relevant_events",
    "_safe_event_projection",
    "_packet_event_relevant",
    "_truncate_packet_values",
    "_safe_finding",
)


def test_the_tick_context_lane_is_gone():
    assert [name for name in REMOVED_CONTEXT_BUILDER_SYMBOLS if hasattr(context_builder, name)] == []


def test_persona_runtime_imports_only_the_surviving_type():
    """The import line was the lane's ONLY liveness. It must not keep naming
    functions the module never called."""

    source = inspect.getsource(persona_runtime)
    assert "from .context_builder import AgentContext\n" in source
    assert "build_context" not in source
    assert "render_context" not in source


def test_nothing_module_level_is_unreachable_from_agent_context():
    """Same gate as the snapshot island: after the cut, every module-level name
    must be reachable from the module's one surviving export."""

    tree = ast.parse(inspect.getsource(context_builder))
    defs: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defs[target.id] = node

    def referenced(node) -> set[str]:
        return {
            inner.id
            for inner in ast.walk(node)
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load) and inner.id in defs
        }

    seen: set[str] = set()
    stack = ["AgentContext"]
    while stack:
        current = stack.pop()
        if current in seen or current not in defs:
            continue
        seen.add(current)
        stack.extend(referenced(defs[current]) - seen)

    assert sorted(set(defs) - seen) == []


def test_agent_context_survives_with_its_fields():
    """KEEP: ``persona_runtime`` annotates against this shape."""

    assert dataclasses.is_dataclass(context_builder.AgentContext)
    fields = {field.name for field in dataclasses.fields(context_builder.AgentContext)}
    for name in ("task", "run", "current_stage", "mission_hud", "proof_ids", "repo_context"):
        assert name in fields, name


def test_the_contract_45_mission_hud_observability_field_is_untouched():
    """Negative gate. ``context_builder._mission_hud`` (removed) is NOT
    ``prompt_observability``'s ``mission_hud`` row (kept): the Launcher reads
    that key off the snapshot, so it survives this cut regardless of the name
    collision."""

    from agent_runtime import prompt_observability

    source = inspect.getsource(prompt_observability)
    assert '"mission_hud"' in source
