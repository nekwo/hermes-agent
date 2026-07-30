"""S27 retires the ~78-name orphan tree left inside ``snapshot.py``.

S18 cut ``goal_detail_for_task`` -> ``_goal_projection_from_task`` ->
``_task_summary``, the only entry point into the mission projection. Everything
that chain reached survived the cut as an unreachable island: the archived-task
transcript readers, the stage-verification / proof-lane builders, the mission
actor + lifecycle summarizers, the role/stage stream projectors, and the
snapshot-local copies of ``_run_summary`` / ``_event_display_*``.

The island is verified caller-free from the module's REAL external surface,
which is exactly five names (``build_snapshot``, ``write_snapshot``,
``_parity_envelope``, ``_default_persona_session_db``,
``persona_instance_detail_for_id``). A plain text grep does NOT prove this: nine
of the removed names collide with same-named private helpers in OTHER modules
(``observability._run_summary`` / ``._event_display_kind`` /
``._event_display_title`` / ``._event_display_projection``,
``board_store._read_json``, ``budget_approval._safe_int``,
``runtime_commands._archived_task_summary``, and the ``status.py`` COMMENT that
names ``_stopped_progress`` / ``_has_budget_incident``). Those collisions are
what kept this island looking alive; the pins below record them so a future pass
does not re-derive the same false liveness.

S18's own keep-side pin is corrected here, not deleted: it justified
``_archived_task_summaries`` as "the fetch lane behind ``harness task
history``". S8 removed that CLI verb (``tests/agent_runtime/
test_snapshot_history_eviction.py`` asserts the parser now rejects the whole
history verb family), so the reader had no lane left to serve.
"""

from __future__ import annotations

import ast
import inspect

from agent_runtime import snapshot


#: The unreachable island. Grouped by the chain that used to reach it.
REMOVED_SNAPSHOT_SYMBOLS = (
    # Caps whose only readers were the removed builders.
    "STAGE_VERIFICATION_STAGE_CAP",
    "STAGE_VERIFICATION_PROOF_ID_CAP",
    "STAGE_VERIFICATION_PATH_CAP",
    "MISSION_FLOW_TIMELINE_ITEM_CAP",
    "ARCHIVED_TASKS_REF_RECENT_CAP",
    # Archived-mission readers + the transcript family behind them.
    "_archived_task_summaries",
    "_archived_task_summary",
    "_archived_conversation_text",
    "_archived_conversation_list",
    "_archived_conversation_message_sort_key",
    "_dedupe_archived_conversation_messages",
    "_latest_archived_message_timestamp",
    "_parse_archived_time",
    "_archived_run_summaries",
    "_archived_proof_summaries",
    "_archived_role_envelope_summaries",
    "_archived_role_checklist_summaries",
    "_archived_persona_assignment_summaries",
    "_archived_repo_bundle_summaries",
    "_archived_persona_streams",
    "_archived_event_log_events",
    "_safe_archive_task_filename",
    "_dedupe_archived_events",
    "_archived_transcript_events",
    "_archived_role_streams",
    "_archived_event_stream_item",
    "_coalesced_archived_progress_events",
    "_empty_archived_role_stream_item",
    "_archived_event_display_kind",
    "_archived_event_display_title",
    "_archived_role_current_stage",
    "_run_summary_from_mapping",
    "_persona_timing_summaries",
    "_timing_total",
    "_add_int",
    "_duration_ms",
    "_proof_summary_from_mapping",
    "_read_json",
    # Stage verification / proof-lane projection.
    "_verification_status",
    "_stage_verification",
    "_stage_owner_by_id",
    "_bounded_projection_strings",
    "_proof_ref",
    "_proof_lane_status",
    "_proof_status",
    "_stage_tamper_flag",
    "_task_current_stage_id",
    "_safe_int",
    "_proof_visibility_summary",
    # Mission lifecycle / actor / capability summarizers.
    "_mission_lifecycle_state",
    "_mission_level_state",
    "_operator_capabilities",
    "_actor_state_label",
    "_latest_actor_event",
    "_actor_budget_summary",
    "_runtime_lane_summary",
    "_persona_streams",
    # Execution routing: "can a run start / why not / what next".
    "_execution_status",
    "_can_start_run",
    "_run_blocked_reason",
    "_next_action_summary",
    "_why_not_done",
    "_stopped_progress",
    "_has_budget_incident",
    # Role / stage / timeline stream projection.
    "_task_timeline",
    "_coalesced_progress_events",
    "_role_streams",
    "_stage_streams",
    "_event_stream_item",
    "_empty_role_stream_item",
    "_display_name_for_persona",
    "_role_current_stage",
    "_event_display_projection",
    "_event_display_kind",
    "_event_display_title",
    # Run projection (orphaned when the run rows left the frame).
    "_run_summary",
    "_safe_llm",
    "_public_decision_value",
)

#: Imports the island was the sole consumer of. A surviving import is a
#: surviving dependency edge, so the gate is the import statement.
REMOVED_SNAPSHOT_IMPORTS = (
    "role_checklists",
    "simplified_contract",
)


def test_the_snapshot_orphan_island_is_gone():
    assert [name for name in REMOVED_SNAPSHOT_SYMBOLS if hasattr(snapshot, name)] == []


def test_the_island_only_imports_are_gone():
    source = inspect.getsource(snapshot)
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    surviving = [
        module for module in REMOVED_SNAPSHOT_IMPORTS if any(module in line for line in imports)
    ]
    assert surviving == []


def test_no_module_level_name_is_unreachable_from_the_external_surface():
    """The defect class this stage retires: an island that survives a cut because
    every reference into it comes from inside itself.

    The roots are the module's verified external surface. Anything not reachable
    from them (or from module-level executable code) is unreachable by
    construction, regardless of what a text grep says.
    """

    roots = {
        "build_snapshot",
        "write_snapshot",
        "_parity_envelope",
        "_default_persona_session_db",
        "persona_instance_detail_for_id",
        # S27 seeded two more roots here — ``_open_incidents_frame`` and
        # ``snapshot_section_bytes`` — on the grounds that S18 and
        # test_snapshot_history_eviction pinned them. A test pin is not a caller:
        # S29 established both were production-caller-free and removed them, so
        # the roots are back to the module's five real external names. See
        # tests/agent_runtime/test_s29_snapshot_dead_local_removal.py.
    }
    tree = ast.parse(inspect.getsource(snapshot))
    defs: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defs[target.id] = node
    module_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load):
                module_level.add(inner.id)

    def referenced(node) -> set[str]:
        names = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load) and inner.id in defs:
                names.add(inner.id)
        return names

    seen: set[str] = set()
    stack = list((roots | module_level) & set(defs))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(referenced(defs[current]) - seen)

    assert sorted(set(defs) - seen) == []


def test_the_lookalike_keep_set_survives():
    """Nine removed names collide with live private helpers in other modules —
    the exact collisions that made a text grep report this island as live."""

    from agent_runtime import board_store, budget_approval, observability

    assert callable(observability._run_summary)
    assert callable(observability._event_display_kind)
    assert callable(observability._event_display_title)
    assert callable(observability._event_display_projection)
    assert callable(board_store._read_json)
    assert callable(budget_approval._safe_int)
    # ``status.py`` only NAMES snapshot's ``_stopped_progress`` /
    # ``_has_budget_incident`` in a comment; its own chain went in S21.
    from agent_runtime import status

    assert not hasattr(status, "_stopped_progress")
    assert not hasattr(status, "_has_budget_incident")
    # The live frames the launcher renders every tick.
    assert callable(snapshot.build_snapshot)
    assert callable(snapshot._boards_summary)
    assert callable(snapshot._realm_summary)
    assert callable(snapshot._workspace_summary)
    assert callable(snapshot._offices_summary)
    assert callable(snapshot.persona_instance_detail_for_id)
    assert callable(snapshot._agent_tool_detail)


def test_the_live_frame_is_unchanged(isolate_agent_runtime_root):
    """Negative gate: cutting the island must not move the contract or drop a
    section the Launcher reads."""

    frame = snapshot.build_snapshot()
    assert frame["parity"]["contract_version"] == 45
    for section in ("boards", "workspaces", "realms", "agents"):
        assert section in frame, f"{section} is a KEEP frame and must survive"
    for section in ("goals", "archived_tasks", "proofs", "incidents", "runs", "stage_verification"):
        assert section not in frame, f"{section} must not be a top-level frame section"


def test_the_workspace_goals_wire_field_survives(isolate_agent_runtime_root):
    """Contract field, not a mission row: ``_workspace_summary`` publishes
    ``goals`` as a COUNT the Launcher reads. It is one bare-word grep away from
    the removed mission lane and must not go with it."""

    source = inspect.getsource(snapshot._workspace_summary)
    assert '"goals": len(goals)' in source
