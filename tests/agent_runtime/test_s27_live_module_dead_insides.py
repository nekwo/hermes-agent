"""S27 cuts the dead insides of four modules that are themselves LIVE.

These are not orphan modules — each still has real importers — so a
module-level gate (S13/S20) cannot reach them. What died is everything inside
that the mission lane used to call. Verified by intra-module reachability from
each module's actual external surface, which is small and exact:

===================== =========================================================
module                surviving external surface
===================== =========================================================
dev_discipline        ``update_progress_telemetry``   (progress.py)
simplified_contract   ``public_decision_type_value``  (operator_channels, store)
scope_control         ``validate_discovery_payload``, ``validate_triage_payload``
                      (decision_contracts), ``untriaged_issue_discoveries``
                      (observability), ``find_discovery_task`` (harness.py)
role_checklists       ``validate_checklist_payload_structure``
                      (decision_contract_registry), ``RoleChecklistStore``,
                      ``checklist_summary``, ``normalize_role_id``
                      (role_envelopes)
===================== =========================================================

Two symbols were mislabelled in the plan for this wave; both corrections are
pinned below so the record is not lost:

* ``role_checklists.checklist_for_task_stage`` was listed for removal. It is
  **LIVE** — ``RoleChecklistStore.open_or_create`` calls it and
  ``role_envelopes.py:91`` calls that. Skipped.
* ``role_checklists.stage_checklist_hud`` was listed as KEEP. It is **DEAD** —
  its last caller left in ``8fa9ee283`` (the S19 context_builder HUD cluster
  cut), and no test referenced it. Removed.

``scope_control.issue_discovery_counts`` was flagged for re-checking and is
likewise dead: its caller was inside the ``snapshot.py`` region cut in wave 1.

Two of the removed functions were already latent ``NameError``s:
``scope_control.apply_issue_triage`` constructs ``Task(...)`` and
``role_checklists``' removed trio annotates ``task: Task`` — the ``Task`` record
was deleted in S8 and neither module imports it. ``from __future__ import
annotations`` hid the annotation half; the constructor half would have raised on
the first call that never came.
"""

from __future__ import annotations

import ast
import inspect

from agent_runtime import dev_discipline, role_checklists, scope_control, simplified_contract


REMOVED = {
    "dev_discipline": (
        "needs_supervisor_slicing",
        "validate_dev_progress_gate",
        "_BROAD_TITLE_MARKERS",
        "_BROAD_DESCRIPTION_MARKERS",
        "_BACKEND_FIRST_FLAGS",
        "_BACKEND_SLICE_MARKERS",
        "_HARNESS_SUPPORT_REPO_MARKERS",
        "_PROGRESS_OK_DECISIONS",
        "_BUDGET_PRESSURE_OK_DECISIONS",
        "_repos_that_require_specialist_slicing",
        "_is_harness_support_repo",
        "_has_bounded_specialist_handoff_packet",
        "_is_backend_first_slice",
        "_has_empirical_progress",
        "_has_budget_pressure",
        "_decision_has_proof_ids",
        "_validate_failed_proof_reuse",
        "_dedupe_strings",
        "_safe_string_list",
        "_environment_changed",
    ),
    "simplified_contract": (
        "COLLAPSED_SIGNAL_TYPES",
        "DecisionProjection",
        "simplified_contract_enabled",
        "expose_only_simplified_actions",
        "keep_internal_state_machine",
        "collapsed_signal_for",
        "project_decision_for_execution",
        "_internal_execution_decision",
        "legacy_acceptance_decision_from_scope_route",
        "legacy_qa_review_decision_from_qa_verdict",
        "legacy_issue_decision_from_escalate",
        "_record_parity",
    ),
    "scope_control": (
        "TERMINAL_TRIAGE_STATUSES",
        "BOUNDED_TEST_FIX_FLAG",
        "FINAL_GAP_REPORT_FLAG_PREFIX",
        "record_issue_discovery",
        "has_untriaged_issue_discovery",
        "needs_pm_triage_before_dev",
        "issue_discovery_counts",
        "find_discovery",
        "child_mission_depth",
        "direct_child_count",
        "should_report_gap_instead_of_forking",
        "mark_discovery_for_final_report",
        "mark_bounded_test_fix_pass",
        "apply_issue_triage",
    ),
    "role_checklists": (
        "stage_checklist_hud",
        "validate_decision_checklist_payload",
        "sanitize_decision_checklist_payload",
        "apply_decision_checklist_updates",
    ),
}

#: The verified external surface each module must stay reachable from.
EXTERNAL_SURFACE = {
    "dev_discipline": {"update_progress_telemetry"},
    "simplified_contract": {"public_decision_type_value"},
    "scope_control": {
        "validate_discovery_payload",
        "validate_triage_payload",
        "untriaged_issue_discoveries",
        "find_discovery_task",
    },
    "role_checklists": {
        "validate_checklist_payload_structure",
        "RoleChecklistStore",
        "checklist_summary",
        "normalize_role_id",
    },
}

MODULES = {
    "dev_discipline": dev_discipline,
    "simplified_contract": simplified_contract,
    "scope_control": scope_control,
    "role_checklists": role_checklists,
}


def _unreachable(module, roots: set[str]) -> list[str]:
    tree = ast.parse(inspect.getsource(module))
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
    stack = [name for name in roots if name in defs]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(referenced(defs[current]) - seen)
    return sorted(set(defs) - seen)


def test_every_dead_inside_is_gone():
    offenders = {
        name: [symbol for symbol in symbols if hasattr(MODULES[name], symbol)]
        for name, symbols in REMOVED.items()
    }
    assert {name: found for name, found in offenders.items() if found} == {}


def test_nothing_is_left_unreachable_from_the_external_surface():
    leftovers = {
        name: _unreachable(module, EXTERNAL_SURFACE[name])
        for name, module in MODULES.items()
    }
    assert {name: found for name, found in leftovers.items() if found} == {}


def test_checklist_for_task_stage_is_live_and_stays():
    """CORRECTION: listed for removal, but ``RoleChecklistStore.open_or_create``
    calls it and ``role_envelopes`` calls that."""

    assert callable(role_checklists.checklist_for_task_stage)
    assert "checklist_for_task_stage(" in inspect.getsource(
        role_checklists.RoleChecklistStore.open_or_create
    )
    from agent_runtime import role_envelopes

    assert "open_or_create(" in inspect.getsource(role_envelopes)


def test_the_live_external_surface_of_each_module_still_works():
    """Negative gate: the four names one bare-word grep away from the removals."""

    assert dev_discipline.update_progress_telemetry({}, "run.tool.finished", {})["tool_call_count"] == 1
    assert simplified_contract.public_decision_type_value("hand_off") == "hand_off"
    assert scope_control.untriaged_issue_discoveries(object()) == []
    assert role_checklists.normalize_role_id("launcher_dev") == "dev"
    # Structure validation still rejects a malformed checklist payload.
    from agent_runtime.decision_schema import DecisionPayloadInvalid
    import pytest

    with pytest.raises(DecisionPayloadInvalid):
        role_checklists.validate_checklist_payload_structure({"checklist_updates": "not-a-list"})
