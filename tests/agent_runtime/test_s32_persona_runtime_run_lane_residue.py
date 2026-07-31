"""S32 — the S5 run-lane residue in ``persona_runtime``, verified name by name.

S29 (``4a56bb546``) cut the ``AgentContext``-typed cluster and deliberately did
NOT sweep the run-lane LLM-metadata/timing cluster in with it: that set has a
different cause (S5 deleted the run lane, not the context builder), so it was
enumerated in ``test_s29_persona_runtime_context_lane_removal`` as reported-not-
removed. This stage verifies those thirteen names individually — intra-module
reachability from the module's real external entrypoints, plus a repo-wide grep
per name — and acts only where the evidence is complete.

**Removed here: one name.**

* ``_emit_timing`` — the run lane's per-phase timing emitter. Zero callers of any
  kind anywhere in the tree: not ``persona_runtime`` itself, not
  ``agent_runtime``/``hermes_cli``/``tools``/``scripts``, not one test. Its last
  intra-module caller went with ``_invoke_agent`` at S5. Two imports were its
  last use and go by NAME, not by line: ``import time`` (``time.perf_counter``
  appeared exactly once in the module, inside this body) and ``RunProgressSink``
  off the ``.progress`` line — whose sibling ``ChatProgressSink`` is LIVE on the
  chat lane and must survive the drop.

**Kept here, retired at S34 — the nine-name ``_apply_llm_metadata``
sub-cluster.**

At S32, ``_apply_llm_metadata`` had no PRODUCTION caller but was not residue
that sweep could cut, for two independent reasons:

1. It was the SOLE WRITER of ``AgentRun.llm`` — a declared model field that four
   live modules read (``observability``, ``status``, ``store``, and
   ``worker_sessions``). ``store`` goes further: a whole decision-recording
   branch is gated on ``run.llm`` already BEING a dict, which only this writer
   can make true. Removing the only writer of a field live readers consume is a
   lane-level ruling about ``AgentRun.llm`` itself, not a helper cut — and two of
   those readers are owned by a concurrent session this round.
2. It had eight call sites across two test modules
   (``test_persona_runtime_fake``, ``test_run_budget``) that are its OWN unit
   tests — they pin the timing count/total/max accumulation and the run-record
   accounting-block lift. They cannot be retargeted onto a surviving seam, only
   deleted, and this wave does not delete coverage to make a removal fit.

Its eight callees were reachable only from that sole writer. S34 owns the
lockstep field-retirement ruling and removes all nine together.

**Deferred here, retired at S33 — the three ``RepoExecutionContext``-typed
helpers.**

``_repo_context_for_render``, ``_repo_context_progress_payload`` and
``_attach_repo_baseline`` ARE proven caller-free (zero code-form references
repo-wide; ``test_s28_status_observe_shrink``'s own docstring already records
that ``_attach_repo_baseline`` "has had zero callers since S5"). The cut is
blocked on a CONTESTED file, not on evidence: dropping them makes
``capture_repo_baseline`` and ``RepoExecutionContext`` last-use imports, and
``tests/agent_runtime/test_s20_small_module_removal.py`` asserted both were still
bound on ``persona_runtime``. S33 owns the follow-up ruling and retargets that
pin in the same commit as the cut.
"""

from __future__ import annotations

import ast
import inspect

from agent_runtime import persona_runtime

#: Cut at S32: proven caller-free, with no contested pin standing on it.
REMOVED_RUN_LANE_SYMBOLS = ("_emit_timing",)

#: Imports whose LAST use went with it. Dropped by name — the ``.progress`` line
#: keeps a live sibling.
REMOVED_RUN_LANE_IMPORTS = ("RunProgressSink", "time")

#: Deferred at S32 and retired together at S34.
REMOVED_LLM_METADATA_CLUSTER = (
    "_apply_llm_metadata",
    "_decision_metric_reason",
    "_decision_metrics",
    "_finish_reason_from_result",
    "_record_timing_value",
    "_safe_base_url_host",
    "_safe_nonnegative_int",
    "_safe_run_budget_block",
    "_safe_timing_map",
)

#: Deferred at S32 and retired together at S33.
REMOVED_REPO_EXECUTION_CLUSTER = (
    "_attach_repo_baseline",
    "_repo_context_for_render",
    "_repo_context_progress_payload",
)


def test_the_caller_free_timing_emitter_is_gone():
    assert [
        name for name in REMOVED_RUN_LANE_SYMBOLS if hasattr(persona_runtime, name)
    ] == []


def test_its_last_use_imports_went_with_it():
    assert [
        name for name in REMOVED_RUN_LANE_IMPORTS if hasattr(persona_runtime, name)
    ] == []


def test_the_shared_progress_import_line_keeps_its_live_sibling():
    """Negative gate: ``RunProgressSink`` rode the same import line as
    ``ChatProgressSink``, which the LIVE chat lane constructs. Dropping the line
    instead of the name would take the chat trace sink out."""

    assert hasattr(persona_runtime, "ChatProgressSink")
    assert "ChatProgressSink(" in inspect.getsource(persona_runtime)


def test_the_llm_metadata_cluster_was_retired_with_its_writerless_field():
    """S34 removed the sole writer, its exclusive callees, field, and readers
    as one lockstep contract."""

    for name in REMOVED_LLM_METADATA_CLUSTER:
        assert not hasattr(persona_runtime, name), name

    from agent_runtime.models import AgentRun

    assert "llm" not in AgentRun.__dataclass_fields__
    assert "run.llm" not in inspect.getsource(persona_runtime)


def test_the_repo_execution_cluster_was_retired_by_its_follow_up():
    """S33 retargeted the contested pin and retired all three names together."""

    for name in REMOVED_REPO_EXECUTION_CLUSTER:
        assert not hasattr(persona_runtime, name), name

    assert not hasattr(persona_runtime, "capture_repo_baseline")
    assert not hasattr(persona_runtime, "RepoExecutionContext")


def test_no_module_level_name_is_unreachable_from_the_external_surface():
    """The S29 reachability gate, re-run with the S32 delta applied.

    The roots are the names production code outside this module references as
    code. The two clusters kept above are still listed as roots rather than
    silently swept in, exactly as S29 listed them — the difference is that this
    stage says WHY each is still there.
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
    kept_with_cause: set[str] = set()

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
    stack = list((roots | module_level | kept_with_cause) & set(defs))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(referenced(defs[current]) - seen)

    assert sorted(set(defs) - seen) == []


def test_the_chat_lane_entry_points_still_resolve():
    """Negative gate: this module is LIVE — the mission-chat lane runs through
    it. A residue cut must not disturb the entry points its importers bind."""

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
