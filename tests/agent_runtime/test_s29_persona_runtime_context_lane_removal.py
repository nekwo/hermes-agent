"""S29 retires ``persona_runtime``'s second-order ``AgentContext`` orphans.

S27 (``539bf5813``) removed ``build_context`` / ``render_context`` — the only
things that ever PRODUCED an ``AgentContext`` — and recorded the consequence as
follow-up debt in ``context_builder``'s own docstring: ``persona_runtime`` still
annotated eight helpers against a shape nothing could hand them. With no
producer they cannot be called on a real context, so the annotation was the last
thing keeping them upright. This is that follow-up.

The eight, and why each is caller-free (verified against the current tree, not
inherited from the S27 note):

* ``_repo_context_for_persona`` — the WORKER lane's repo grounding, called from
  ``_invoke_agent`` until S5 deleted it. Its only surviving mention outside the
  module is a DOCSTRING in ``mission_chat_workdir`` explaining which lane the
  chat lane was missing grounding relative to. A docstring is not a caller.
  Takes ``_stage_repo_scope_for_persona``, ``_handoff_repo_scope_for_persona``
  and ``_compatible_repo_scope`` with it — each reached from nothing else.
* ``_persona_run_uses_memory`` — the worker lane's memory-scope resolver. The
  LIVE chat lane gates memory directly on ``include_profile_memory``
  (``persona_runtime`` lines feeding ``skip_memory=``), never through this.
* ``_blocked_tool_names_for_run`` — takes ``_is_no_edit_context_stage`` with it.
  **This is the trap in the set**: ``profile_runner`` defines a same-named,
  fully LIVE ``_blocked_tool_names_for_run(request)`` that a text grep reports
  as a caller. It is a different function with a different signature on a
  different lane; the pin below records the collision.
* ``_tool_budget_limits`` — read the removed autonomy packet's inspection
  budget. Takes ``_prior_stage_progress_flags`` and ``_safe_positive_counter``.

KEEP, explicitly: ``_blocked_tool_names_for_chat`` (three live call sites in
this module — it is the chat lane's block list), ``_repo_context_for_render`` /
``_repo_context_progress_payload`` / ``_attach_repo_baseline`` (``RepoExecution
Context``-typed, NOT ``AgentContext``-typed — a separate run-lane cluster left
for its own ruling), and the ``RepoExecutionContext`` / ``capture_repo_baseline``
imports they need.
"""

from __future__ import annotations

import ast
import inspect

from agent_runtime import persona_runtime


#: The ``AgentContext``-typed helpers and the callees exclusive to them.
REMOVED_PERSONA_RUNTIME_SYMBOLS = (
    # Worker-lane repo grounding.
    "_repo_context_for_persona",
    "_stage_repo_scope_for_persona",
    "_handoff_repo_scope_for_persona",
    "_compatible_repo_scope",
    # Worker-lane memory scope.
    "_persona_run_uses_memory",
    # Worker-lane block list (NOT profile_runner's live namesake).
    "_blocked_tool_names_for_run",
    "_is_no_edit_context_stage",
    # Worker-lane tool budget.
    "_tool_budget_limits",
    "_prior_stage_progress_flags",
    "_safe_positive_counter",
)

#: Imports whose last use went with the cut. A surviving import is a surviving
#: dependency edge, so the gate is the import statement.
REMOVED_PERSONA_RUNTIME_IMPORTS = (
    "AgentContext",
    "DecisionPayloadInvalid",
    "repo_execution_context_for_task",
    "stage_requires_product_edit",
)

#: Imports on the SAME import lines that stay, because live code still uses them.
KEPT_PERSONA_RUNTIME_IMPORTS = (
    "RepoExecutionContext",
    "capture_repo_baseline",
    "AgentDecision",
    "parse_structured_decision",
    "blocked_tool_names",
    "role_from_persona",
)


def test_the_agent_context_typed_helpers_are_gone():
    assert [
        name for name in REMOVED_PERSONA_RUNTIME_SYMBOLS if hasattr(persona_runtime, name)
    ] == []


def test_the_only_use_imports_are_gone():
    assert [
        name for name in REMOVED_PERSONA_RUNTIME_IMPORTS if hasattr(persona_runtime, name)
    ] == []


def test_the_shared_import_lines_keep_their_live_names():
    """Negative gate: four of the dropped names ride import lines with siblings
    that live code still calls. Dropping the line instead of the name would take
    them out."""

    for name in KEPT_PERSONA_RUNTIME_IMPORTS:
        assert hasattr(persona_runtime, name), name


def test_the_profile_runner_namesake_is_untouched():
    """The collision that makes a text grep report this cut as unsafe:
    ``profile_runner._blocked_tool_names_for_run`` is a DIFFERENT function on a
    LIVE lane — it takes an ``AgentRunRequest``, and ``profile_runner`` calls it
    on every agent construction."""

    from agent_runtime import profile_runner

    assert callable(profile_runner._blocked_tool_names_for_run)
    parameters = list(inspect.signature(profile_runner._blocked_tool_names_for_run).parameters)
    assert parameters == ["request"]
    assert "_blocked_tool_names_for_run(request)" in inspect.getsource(profile_runner)


def test_the_chat_lane_block_list_survives():
    """``_blocked_tool_names_for_chat`` is one character away from the removed
    ``_blocked_tool_names_for_run`` and is the LIVE chat lane's block list."""

    source = inspect.getsource(persona_runtime)
    assert callable(persona_runtime._blocked_tool_names_for_chat)
    assert source.count("_blocked_tool_names_for_chat(") >= 4


def test_the_repo_execution_context_cluster_survives():
    """KEEP: ``RepoExecutionContext``-typed helpers are a different cluster from
    the ``AgentContext``-typed one and are not part of this cut."""

    for name in (
        "_repo_context_for_render",
        "_repo_context_progress_payload",
        "_attach_repo_baseline",
    ):
        assert callable(getattr(persona_runtime, name)), name


def test_the_stage_intent_module_is_not_collateral():
    """``stage_requires_product_edit`` loses its ``persona_runtime`` importer,
    not its life: ``stage_intent`` still calls it three times internally."""

    from agent_runtime import stage_intent

    assert callable(stage_intent.stage_requires_product_edit)
    assert inspect.getsource(stage_intent).count("stage_requires_product_edit(") >= 3


def test_no_module_level_name_is_unreachable_from_the_external_surface():
    """The defect class this sweep retires: helpers held upright by a type
    annotation after their producer was deleted.

    The roots are the names production code outside this module actually
    references as code (not in a docstring or comment). Everything unreachable
    from them is unreachable by construction — EXCEPT the run-lane
    LLM-metadata/timing cluster, which is a separate pre-existing orphan set
    with its own cause (S5 deleted the run lane, not the context builder) and is
    listed here rather than silently swept into this cut.
    """

    roots = {
        "GPTPersonaRuntime",
        "MISSION_CHAT_WORKSPACE_AGENTS_PREAMBLE",
        "_mission_chat_identity_prompt",
        "_mission_chat_operative_rules",
        "_mission_chat_soul_overlay",
        "apply_chat_lane_tool_scope",
        "chat_lane_capability_drops",
        "chat_runtime_tool_contract",
        "mission_chat_admission_line",
        "mission_chat_operating_skills",
    }
    #: NOT part of this cut — reported, not removed. See the docstring above.
    run_lane_metadata_cluster = {
        "_apply_llm_metadata",
        "_attach_repo_baseline",
        "_decision_metric_reason",
        "_decision_metrics",
        "_emit_timing",
        "_finish_reason_from_result",
        "_record_timing_value",
        "_repo_context_for_render",
        "_repo_context_progress_payload",
        "_safe_base_url_host",
        "_safe_nonnegative_int",
        "_safe_run_budget_block",
        "_safe_timing_map",
    }

    tree = ast.parse(inspect.getsource(persona_runtime))
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
        return {
            inner.id
            for inner in ast.walk(node)
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load) and inner.id in defs
        }

    seen: set[str] = set()
    stack = list((roots | module_level | run_lane_metadata_cluster) & set(defs))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(referenced(defs[current]) - seen)

    assert sorted(set(defs) - seen) == []


def test_the_module_still_imports_and_the_chat_lane_entry_points_resolve():
    """Negative gate: this module is LIVE — the mission-chat lane runs through
    it. The cut must not disturb the entry points its importers bind."""

    for name in (
        "GPTPersonaRuntime",
        "apply_chat_lane_tool_scope",
        "chat_lane_capability_drops",
        "chat_runtime_tool_contract",
        "mission_chat_admission_line",
        "mission_chat_operating_skills",
    ):
        assert hasattr(persona_runtime, name), name
    assert callable(persona_runtime.GPTPersonaRuntime.mission_chat_reply)
