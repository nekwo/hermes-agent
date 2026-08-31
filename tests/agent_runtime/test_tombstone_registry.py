"""THE tombstone registry — one data-driven table for every "this name must not
come back" row from the s1-s57 removal campaigns.

=============================================================================
WHY THIS FILE EXISTS
=============================================================================

Between S40 and S57 the same test kept being rewritten. Twenty ``test_sNN_*``
files each re-implemented the same question — *is this removed symbol still
absent from production source?* — with its own scanner, its own package tuple
and its own exclusions. Two defects rode along with the duplication:

1. **The vacuous-gate class, three times in one week.** A removal test that
   greps raw source cannot tell a re-grown reference from the retirement
   COMMENT the cut just wrote. S48 found it first (``test_s46``'s token-join
   helper renders ``card.title`` as ``card\\n.\\ntitle``, so every dotted
   assertion through it passed vacuously). S57 found three assertions going RED
   against a *correct* tree because they matched their own prose. Each time the
   fix was the same: parse to an AST and re-render. Writing that fix once, in a
   shared scanner, is what this file is.

2. **Twenty different answers to "what counts as production source."** Six
   different package tuples were in use across the removal tests
   (``agent_runtime`` alone; ``+ hermes_cli + tools``; ``+ gateway + agent +
   acp_adapter``; the twelve-package tuple; ``pkgutil.iter_modules`` over
   ``agent_runtime`` top level only, which does not descend into
   subpackages...). A row's protection was whatever tuple its author happened
   to pick.

=============================================================================
THE SCANNER — ONE IMPLEMENTATION, EVERY ROW
=============================================================================

``_code_only`` parses with ``ast.parse``, strips docstrings on the tree, and
re-renders with ``ast.unparse``. Comments never survive ``ast.parse`` at all and
docstrings are removed explicitly, so the result is **comment- and
docstring-immune by construction** rather than by a ``#``-prefix heuristic. It
is S48's mechanism, not S46's: ``ast.unparse`` emits real syntax, so dotted
attributes render AS dotted attributes and a ``models.RepoBundle``-shaped row
can actually match.

STRING LITERALS SURVIVE THE ROUND TRIP, ON PURPOSE. Event kinds, wire keys and
capability ids are invoked BY NAME, never as identifiers — that is the S44/S55
lesson (a de-registered event type is a string; a retired MCP capability id is a
string). A scanner that only saw identifiers would protect half the surface.

=============================================================================
ROW FORMS
=============================================================================

``MODULE``      the module must not be importable (``find_spec`` is ``None``).
``ATTR``        ``module.name`` must not exist (``not hasattr``).
``CLASS_ATTR``  ``module.Class.name`` must not exist.
``EVENT``       the type must not be in ``ALLOWED_EVENT_TYPES`` /
                ``event_catalog()``, and ``EventLog.append`` must refuse it.
``CODE``        the name must not appear in the comment-and-docstring-stripped
                code of any production file in the row's scope. Covers plain
                names, dotted attributes and string-literal forms in one row,
                because ``ast.unparse`` renders all three.
``PATH``        the file or directory must not exist.
``IMPORT``      a named top-level import binding must not exist in the scoped
                source file; used for exec-namespace duplicate bindings where
                the symbol itself legitimately survives in a command part.

=============================================================================
WHAT IS DELIBERATELY *NOT* A ROW HERE
=============================================================================

Absence assertions whose subject is a runtime SHAPE rather than a name:

* **parameter absence** — "passing ``tasks=`` must raise ``TypeError``". That is
  a signature fact, checked by calling; a name scan would fire on any unrelated
  local called ``tasks``.
* **wire-key absence** — "``build_status()`` must not emit ``repo_locks``". That
  is a fact about a produced dict, and the only honest way to check it is to
  build the frame. Every future wire-key cut MUST therefore add or identify a
  producer-frame behaviour pin (exact absence or exact key set); a CODE row is
  not a substitute. Cross-stack readers additionally belong under the
  Launcher's AST producer-presence gate, which derives its key set from the
  byte-pinned real hydrate/delta/heartbeat frames.
* **exact key-set / count pins** — ``migration.counts``, ``RunStore``'s public
  surface, ``OPERATOR_SUMMARY_EVENT_TYPES``.

Private helpers are rowed only when they are name/string-dispatched, their
owner module survives and resurrection could bypass a public tombstone, or a
ruling explicitly treats the spelling as stable vocabulary. Ordinary private
implementation churn is excluded: its invariant is the surviving public
behaviour pin, not permanent reservation of every underscore-prefixed name.
This is why some same-file private siblings have rows and others do not; the
difference is policy, not an incomplete batch.

Those stay in their per-wave files alongside the behaviour pins. Nothing was
dropped in the consolidation; the split is by what a row can honestly assert.

Files kept WHOLE and deliberately not absorbed, each because it is a rule or an
authority rather than a list: ``test_s15_event_contract_pruning`` (the single
``SURVIVING_EVENT_COUNT`` / ``contract_hash`` authority, imported by six other
files), ``test_s55_registered_events_have_emitters`` (the registered-event ->
emitter structural gate), ``test_s56_runtime_config_reader_gate`` (the
``RuntimeConfig``-field -> reader gate, whose ``UNRULED_DEBT`` s57 imports),
``test_s47_wire_constant_field_removal`` (``CURRENT_CONTRACT_VERSION``),
``test_s53_lane_write_lane_removal`` (exports ``seed_lane_row`` to s21), and the
characterization suites.

=============================================================================
RETIREMENT RULE — ROWS ARE NEVER DROPPED SILENTLY
=============================================================================

A hermes row may be dropped only after **one upstream sync has merged cleanly
over that symbol's region**, and the sync commit is recorded on the row when it
goes. Rationale: this is a FORK. A tombstone here is not only "we deleted it" —
it is "upstream may still carry it, and a sync could hand it back". Until a sync
has passed over that region without conflict, the row is doing work.

Dropping a row EDITS THIS VISIBLE TABLE. There is no expiry, no allowlist and no
silent decay, and the next removal wave adds a ROW, not a FILE.

(The Launcher's registry —
``test/features/mission_control/mission_control_tombstone_registry_test.dart`` —
carries the same table shape with a laxer rule: one stable month, because that
repo has no upstream.)
"""

from __future__ import annotations

import ast
import functools
import importlib
import importlib.util
import subprocess
import sys
import textwrap
import warnings
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pytest

from agent_runtime.events import ALLOWED_EVENT_TYPES

HERMES_ROOT = Path(__file__).resolve().parents[2]

#: Production packages every row is enforced over unless it narrows the scope.
#:
#: This is S55's tuple — the WIDEST any migrated row used — plus the repo-root
#: modules, so consolidation could only widen protection and never narrow it.
#: Picking a narrower tuple was a real hazard, not a hypothetical: S40's rows
#: were gated over every ``.py`` in the repo, so a six-package tuple would have
#: silently dropped ``cron`` / ``mobile_core`` / ``providers`` / ``tui_gateway``
#: / ``apps`` and the root modules from that row's reach.
#:
#: ``tests`` is excluded on purpose (S55's rule: a symbol kept alive only by the
#: test written to exercise it is a closed loop, not coverage) and so is this
#: file, which names every banned symbol by definition.
PRODUCTION_PACKAGES = (
    "agent_runtime",
    "hermes_cli",
    "tools",
    "agent",
    "acp_adapter",
    "gateway",
    "scripts",
    "cron",
    "mobile_core",
    "providers",
    "tui_gateway",
    "apps",
    ".",  # repo-root modules (cli.py, hermes_state*.py, …), non-recursive
)

_SKIP_DIRS = {"__pycache__", ".venv", "venvs", "node_modules", ".git"}


class Form(Enum):
    MODULE = "module"
    ATTR = "attr"
    CLASS_ATTR = "class_attr"
    EVENT = "event"
    CODE = "code"
    PATH = "path"
    IMPORT = "import"


@dataclass(frozen=True)
class Tombstone:
    """One retired symbol: what it is, when it went, and why."""

    text: str
    form: Form
    wave: str
    commit: str
    reason: str
    scope: tuple[str, ...] = PRODUCTION_PACKAGES

    @property
    def label(self) -> str:
        return f"{self.wave} {self.text} ({self.form.value})"


def rows(
    wave: str,
    commit: str,
    form: Form,
    reason: str,
    *names: str,
    scope: tuple[str, ...] = PRODUCTION_PACKAGES,
) -> tuple[Tombstone, ...]:
    """Many rows sharing a wave, a commit and one reason.

    The reason is per-CLUSTER, not per-name, because that is how the cuts were
    actually ruled: a family goes because the lane that fed it went, and
    repeating a paraphrase thirty times would make the table less honest, not
    more.
    """

    return tuple(
        Tombstone(name, form, wave, commit, reason, scope) for name in names
    )


# =========================================================================
# THE REGISTRY
# =========================================================================

_AR = ("agent_runtime",)

TOMBSTONES: tuple[Tombstone, ...] = (
    # -- S1-S39 legacy removal contracts (Wave 4 registry migration) -----
    *rows(
        "s11",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'default_personas',
        'seed_personas',
        'BUNDLED_PERSONA_PROFILES',
        'BUNDLED_PERSONA_IDS',
        'DEFAULT_PERSONA_IDS',
        'BASE_PERSONA_ID',
        'DEFAULT_SUPERVISOR_PERSONA_ID',
        'ALLOWED_TOOLSETS_BY_ROLE',
        'PER_ROLE_TOOL_DENIES',
        scope=('agent_runtime.personas',),
    ),
    *rows(
        "s5",
        "5a1267ef6",
        Form.MODULE,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'agent_runtime.autonomy',
        'agent_runtime.goal_runner',
        'agent_runtime.liveness',
        'agent_runtime.no_freeze_monitor',
        'agent_runtime.node_tools',
        'agent_runtime.planning',
        'agent_runtime.reconciler',
        'agent_runtime.recovery',
        'agent_runtime.root_node_engine',
        'agent_runtime.supervision',
        'agent_runtime.ticker',
        'agent_runtime.worker_actions',
    ),
    *rows(
        "s6",
        "5a1267ef6",
        Form.MODULE,
        "the mission proof runner, gates, recipes, burn-in, and visual capture modules retired together",
        "agent_runtime.gates",
        "agent_runtime.final_gate",
        "agent_runtime.proof_batches",
        "agent_runtime.proof_command_policy",
        "agent_runtime.promotion_gates",
        "agent_runtime.proof_runner",
        "agent_runtime.proof_rules",
        "agent_runtime.proof_recipes",
        "agent_runtime.proof_gates",
        "agent_runtime.burn_in",
        "agent_runtime.smoke",
        "agent_runtime.replay_scenarios",
        "agent_runtime.visual_proof",
        "agent_runtime.visual_trace_evidence",
    ),
    *rows(
        "s7",
        "5a1267ef6",
        Form.MODULE,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'agent_runtime.default_plan',
        'agent_runtime.mission_plan',
        'agent_runtime.state_machine',
    ),
    *rows(
        "s13",
        "5a1267ef6",
        Form.MODULE,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'agent_runtime.preflight',
        'agent_runtime.worklog',
        'agent_runtime.plan_review',
        'agent_runtime.actions',
        'agent_runtime.snapshot_audit',
        'agent_runtime.missing_input',
        'agent_runtime.skill_context',
        'agent_runtime.transitions',
    ),
    *rows(
        "s14",
        "5a1267ef6",
        Form.MODULE,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'agent_runtime.stagec_mcp_visual_provider',
        'agent_runtime.stagec_command_policy',
        'agent_runtime.stagec_trace_parsers',
        'agent_runtime.proof_capture',
    ),
    *rows(
        "s20",
        "5a1267ef6",
        Form.MODULE,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'agent_runtime.role_sessions',
    ),
    *rows(
        "s23",
        "5a1267ef6",
        Form.MODULE,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'tools.node_control_tool',
    ),
    *rows(
        "s27",
        "5a1267ef6",
        Form.MODULE,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'agent_runtime.recovery_flags',
    ),
    *rows(
        "s29",
        "5a1267ef6",
        Form.MODULE,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'agent_runtime.context_builder',
    ),
    *rows(
        "s17",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        '_enrich_proof_lane_attribution',
        '_safe_proof_actor',
        '_safe_run_id',
        '_safe_proof_status',
        '_safe_archive_actor',
        '_safe_archive_persona',
        '_safe_archive_reason',
        '_archive_result',
        '_archive_batch_name',
        '_move_if_exists',
        '_active_worker_sessions_for_task',
        '_archive_worker_evidence',
        '_archive_persona_assignment_evidence',
        '_archive_persona_instance_evidence',
        '_archive_repo_bundle_evidence',
        '_archive_runtime_instance_evidence',
        '_archive_packet_artifacts',
        '_archive_role_envelope_evidence',
        '_archive_self_test_evidence',
        '_latest_run_is_invalid',
        '_run_session_is_reusable',
        '_run_has_approved_continuation',
        '_ANY_STAGE',
        'TERMINAL_TASK_STATES',
        'RELEASE_ASSIGNMENT_TASK_STATES',
        'ARCHIVABLE_TASK_STATES',
        'timedelta',
        'TaskState',
        'Proof',
        'GoalRuntimeInstance',
        'PersonaAssignmentStore',
        'PersonaInstanceStore',
        'archive_task_events',
        'archive_lock',
        'task_lock',
        'emit_task_refresh',
        scope=('agent_runtime.store',),
    ),
    *rows(
        "s18",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'goal_detail_for_task',
        '_goal_projection_from_task',
        '_task_summary',
        '_goal_head',
        'GOAL_DETAIL_ONLY_FIELDS',
        '_archived_tasks_frame',
        '_archived_operator_channels',
        '_archived_operator_channel',
        '_archived_goal_input_message',
        '_archived_assignment_message',
        '_archived_event_message',
        '_roll_up_gate_state',
        '_actor_presence',
        '_proof_requirement_status',
        '_proof_evidence_ref',
        '_incident_summary',
        '_proof_summary',
        scope=('agent_runtime.snapshot',),
    ),
    *rows(
        "s23",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'tasks_dir',
        'legacy_task_path',
        'task_storage_candidates',
        'existing_task_path',
        'proofs_dir',
        'proof_record_path',
        'proof_sandbox_task_dir',
        'proof_sandbox_dir',
        'incident_detail_path',
        'daemon_status_path',
        'daemon_lease_path',
        'queued_skills_dir',
        scope=('agent_runtime.paths',),
    ),
    *rows(
        "s24",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'DIRECTIVE_KEY',
        'DEFAULT_DELIVERY_DIRECTIVE',
        'PROMOTE_MODES',
        'PRESERVE_DIFF_MODES',
        'WORKTREE_MODES',
        'DeliveryDirectiveInvalid',
        'normalize_delivery_directive',
        'task_delivery_directive',
        'execute_delivery_directive',
        'execute_task_delivery_directives',
        'execute_task_worktree_delivery_directives',
        'reap_task_run_worktrees',
        '_reap_bundle_worktrees',
        '_emit_task_reap_event',
        '_promote_patch_to_repo',
        '_open_promotion_incident',
        '_write_promotion_record',
        '_synthetic_worktree_bundle',
        '_bundle_run_id',
        '_dirty_paths',
        '_run_git',
        '_emit',
        'capture_bundle_patch',
        'bundle_patch_path',
        scope=('agent_runtime.delivery_directive',),
    ),
    *rows(
        "s24",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'command_workdir_for_task',
        'existing_run_worktrees_in_bases',
        'harness_worktree_dirs',
        'git_diff_since_baseline',
        'diff_weakens_tests',
        'known_repo_scope_labels',
        'canonical_repo_scope_label',
        'explicit_repo_mentions',
        '_dirty_paths_from_status',
        scope=('agent_runtime.repo_context',),
    ),
    *rows(
        "s27",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'STAGE_VERIFICATION_STAGE_CAP',
        'STAGE_VERIFICATION_PROOF_ID_CAP',
        'STAGE_VERIFICATION_PATH_CAP',
        'MISSION_FLOW_TIMELINE_ITEM_CAP',
        'ARCHIVED_TASKS_REF_RECENT_CAP',
        '_archived_task_summaries',
        '_archived_task_summary',
        '_archived_conversation_text',
        '_archived_conversation_list',
        '_archived_conversation_message_sort_key',
        '_dedupe_archived_conversation_messages',
        '_latest_archived_message_timestamp',
        '_parse_archived_time',
        '_archived_run_summaries',
        '_archived_proof_summaries',
        '_archived_role_envelope_summaries',
        '_archived_role_checklist_summaries',
        '_archived_persona_assignment_summaries',
        '_archived_repo_bundle_summaries',
        '_archived_persona_streams',
        '_archived_event_log_events',
        '_safe_archive_task_filename',
        '_dedupe_archived_events',
        '_archived_transcript_events',
        '_archived_role_streams',
        '_archived_event_stream_item',
        '_coalesced_archived_progress_events',
        '_empty_archived_role_stream_item',
        '_archived_event_display_kind',
        '_archived_event_display_title',
        '_archived_role_current_stage',
        '_run_summary_from_mapping',
        '_persona_timing_summaries',
        '_timing_total',
        '_add_int',
        '_duration_ms',
        '_proof_summary_from_mapping',
        '_read_json',
        '_verification_status',
        '_stage_verification',
        '_stage_owner_by_id',
        '_bounded_projection_strings',
        '_proof_ref',
        '_proof_lane_status',
        '_proof_status',
        '_stage_tamper_flag',
        '_task_current_stage_id',
        '_safe_int',
        '_proof_visibility_summary',
        '_mission_lifecycle_state',
        '_mission_level_state',
        '_operator_capabilities',
        '_actor_state_label',
        '_latest_actor_event',
        '_actor_budget_summary',
        '_runtime_lane_summary',
        '_persona_streams',
        '_execution_status',
        '_can_start_run',
        '_run_blocked_reason',
        '_next_action_summary',
        '_why_not_done',
        '_stopped_progress',
        '_has_budget_incident',
        '_task_timeline',
        '_coalesced_progress_events',
        '_role_streams',
        '_stage_streams',
        '_event_stream_item',
        '_empty_role_stream_item',
        '_display_name_for_persona',
        '_role_current_stage',
        '_event_display_projection',
        '_event_display_kind',
        '_event_display_title',
        '_run_summary',
        '_safe_llm',
        '_public_decision_value',
        scope=('agent_runtime.snapshot',),
    ),
    *rows(
        "s27",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'role_checklists',
        'simplified_contract',
        scope=('agent_runtime.snapshot',),
    ),
    *rows(
        "s29",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        '_repo_context_for_persona',
        '_stage_repo_scope_for_persona',
        '_handoff_repo_scope_for_persona',
        '_compatible_repo_scope',
        '_persona_run_uses_memory',
        '_blocked_tool_names_for_run',
        '_is_no_edit_context_stage',
        '_tool_budget_limits',
        '_prior_stage_progress_flags',
        '_safe_positive_counter',
        scope=('agent_runtime.persona_runtime',),
    ),
    *rows(
        "s29",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'AgentContext',
        'DecisionPayloadInvalid',
        'repo_execution_context_for_task',
        'stage_requires_product_edit',
        scope=('agent_runtime.persona_runtime',),
    ),
    *rows(
        "s29",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        '_open_incidents_frame',
        'snapshot_section_bytes',
        scope=('agent_runtime.snapshot',),
    ),
    *rows(
        "s32",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        '_emit_timing',
        scope=('agent_runtime.persona_runtime',),
    ),
    *rows(
        "s32",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'RunProgressSink',
        'time',
        scope=('agent_runtime.persona_runtime',),
    ),
    *rows(
        "s32",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        '_apply_llm_metadata',
        '_decision_metric_reason',
        '_decision_metrics',
        '_finish_reason_from_result',
        '_record_timing_value',
        '_safe_base_url_host',
        '_safe_nonnegative_int',
        '_safe_run_budget_block',
        '_safe_timing_map',
        scope=('agent_runtime.persona_runtime',),
    ),
    *rows(
        "s32",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        '_attach_repo_baseline',
        '_repo_context_for_render',
        '_repo_context_progress_payload',
        scope=('agent_runtime.persona_runtime',),
    ),
    *rows(
        "s33",
        "0c9d48d9f",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'RepoExecutionContext',
        'RunStore',
        'capture_repo_baseline',
        scope=('agent_runtime.persona_runtime',),
    ),
    *rows(
        "s16",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'allowed_decisions_for_role',
        'prompt_contract_markdown',
        '_PM_QA',
        '_COMMON_SHAPE_IDS',
        scope=('agent_runtime.decision_contract_registry',),
    ),
    *rows(
        "s16",
        "5a1267ef6",
        Form.CLASS_ATTR,
        "role gating was removed from the decision contract model; roles remain "
        "first-class HUD data. SUPERSEDED at S66 by the s65 ATTR row banning "
        "DecisionContract itself: f9aa0faab deleted the whole class while the "
        "module survived, so this row's owner no longer resolves and the row "
        "could never fail again. Kept rather than dropped — the fork retirement "
        "rule requires a clean upstream sync over the region first, and the "
        "member-level history is what records WHY role gating went",
        "decision_contract_registry.DecisionContract.allowed_roles",
        scope=_AR,
    ),
    *rows(
        "s23",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'emit',
        'emit_repo_bundle',
        'emit_handoff',
        'emit_scope_change',
        scope=('agent_runtime.packets',),
    ),
    *rows(
        "s7",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'MissionPlan',
        'MissionPlanStage',
        'TaskStage',
        scope=('agent_runtime.models',),
    ),
    *rows(
        "s8",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'Task',
        'Goal',
        scope=('agent_runtime.models',),
    ),
    *rows(
        "s27",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'Proof',
        'ProofType',
        scope=('agent_runtime.models',),
    ),
    *rows(
        "s27",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'Proof',
        scope=('agent_runtime',),
    ),
    *rows(
        "s27",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'Proof',
        scope=('agent_runtime.observability',),
    ),
    *rows(
        "s28",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'TaskState',
        scope=('agent_runtime.status',),
    ),
    *rows(
        "s5",
        "5a1267ef6",
        Form.CLASS_ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'persona_runtime.GPTPersonaRuntime.run_tick',
        'persona_runtime.GPTPersonaRuntime._invoke_agent',
        scope=('agent_runtime',),
    ),
    *rows(
        "s17",
        "5a1267ef6",
        Form.CLASS_ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'store.RunStore.open_run',
        'store.RunStore.heartbeat',
        'store.RunStore.approve_continuation',
        'store.RunStore.latest_session_id',
        'store.RunStore.find_stale',
        'store.RunStore.find_active',
        scope=('agent_runtime',),
    ),
    *rows(
        "s25",
        "5a1267ef6",
        Form.EVENT,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'run.opened',
    ),
    *rows(
        "s32",
        "5a1267ef6",
        Form.EVENT,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'decision_contract.parity',
    ),
    *rows(
        "s36",
        "0c9d48d9f",
        Form.EVENT,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'packet.recorded',
    ),
    *rows(
        "s37",
        "0c9d48d9f",
        Form.EVENT,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'packet.duplicate',
        'packet.normalized',
    ),
    *rows(
        "s27",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'needs_supervisor_slicing',
        'validate_dev_progress_gate',
        '_BROAD_TITLE_MARKERS',
        '_BROAD_DESCRIPTION_MARKERS',
        '_BACKEND_FIRST_FLAGS',
        '_BACKEND_SLICE_MARKERS',
        '_HARNESS_SUPPORT_REPO_MARKERS',
        '_PROGRESS_OK_DECISIONS',
        '_BUDGET_PRESSURE_OK_DECISIONS',
        '_repos_that_require_specialist_slicing',
        '_is_harness_support_repo',
        '_has_bounded_specialist_handoff_packet',
        '_is_backend_first_slice',
        '_has_empirical_progress',
        '_has_budget_pressure',
        '_decision_has_proof_ids',
        '_validate_failed_proof_reuse',
        '_dedupe_strings',
        '_safe_string_list',
        '_environment_changed',
        scope=('agent_runtime.dev_discipline',),
    ),
    *rows(
        "s27",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'COLLAPSED_SIGNAL_TYPES',
        'DecisionProjection',
        'simplified_contract_enabled',
        'expose_only_simplified_actions',
        'keep_internal_state_machine',
        'collapsed_signal_for',
        'project_decision_for_execution',
        '_internal_execution_decision',
        'legacy_acceptance_decision_from_scope_route',
        'legacy_qa_review_decision_from_qa_verdict',
        'legacy_issue_decision_from_escalate',
        '_record_parity',
        scope=('agent_runtime.simplified_contract',),
    ),
    *rows(
        "s27",
        "5a1267ef6",
        Form.ATTR,
        "migrated from the pre-registry removal contract; exact scoped "
        "absence remains protected across upstream syncs",
        'TERMINAL_TRIAGE_STATUSES',
        'BOUNDED_TEST_FIX_FLAG',
        'FINAL_GAP_REPORT_FLAG_PREFIX',
        'record_issue_discovery',
        'has_untriaged_issue_discovery',
        'needs_pm_triage_before_dev',
        'issue_discovery_counts',
        'find_discovery',
        'child_mission_depth',
        'direct_child_count',
        'should_report_gap_instead_of_forking',
        'mark_discovery_for_final_report',
        'mark_bounded_test_fix_pass',
        'apply_issue_triage',
        scope=('agent_runtime.scope_control',),
    ),
    *rows(
        "s17",
        "5a1267ef6",
        Form.EVENT,
        "the write-dead run lifecycle appenders were removed with their store methods",
        "run.heartbeat",
        "run.approved",
    ),
    *rows(
        "s19",
        "5a1267ef6",
        Form.CODE,
        "the task-only HUD preview lost both entry points; the scoped AST gate replaces raw source scans",
        "mission_hud_preview",
        "mission_hud",
        scope=("agent_runtime.prompt_observability", "agent_runtime.runtime_hud"),
    ),
    *rows(
        "s23",
        "5a1267ef6",
        Form.CODE,
        "the deleted node-control modules must not regain tool or toolset registration",
        "node_control",
        "run_node",
        "steer_node",
    ),
    *rows(
        "s24",
        "5a1267ef6",
        Form.EVENT,
        "the task and bundle worktree reapers lost their only emitters",
        "worktree.task_reaped",
        "bundle.worktree_reaped",
    ),
    *rows(
        "s28",
        "5a1267ef6",
        Form.CODE,
        "daemon/task intervention families lost every input; AST scanning replaces the vacuous raw-source gate",
        "daemon_offline",
        "daemon_error",
        "daemon_stale",
        "context_request_loop",
        "context_request_unfulfilled",
        "issue_discovery_triage_needed",
        "start_daemon",
        "mission_daemon",
        scope=("agent_runtime.observability",),
    ),
    *rows(
        "s20",
        "5a1267ef6",
        Form.ATTR,
        "last-use imports retired from the surviving modules",
        "public_decision_type_value",
        scope=("agent_runtime.snapshot",),
    ),
    *rows(
        "s20",
        "5a1267ef6",
        Form.ATTR,
        "the issue-discovery read alias left observability with its producer lane",
        "untriaged_issue_discoveries",
        scope=("agent_runtime.observability",),
    ),
    *rows(
        "s20",
        "5a1267ef6",
        Form.ATTR,
        "repo-baseline imports retired while the worktree creators remain live",
        "capture_repo_baseline",
        scope=("agent_runtime.repo_context",),
    ),
    *rows(
        "s29",
        "5a1267ef6",
        Form.ATTR,
        "the run-only worktree alias and model import left persona_runtime with the context lane",
        "isolated_repo_context_for_run",
        "AgentRun",
        scope=("agent_runtime.persona_runtime",),
    ),
    *rows(
        "s21",
        "5a1267ef6",
        Form.ATTR,
        "status helpers computed only over the retired mission task list",
        "_has_budget_approval_path",
        "_has_budget_scope_recovery_path",
        "_next_action",
        "_owner_for_action",
        scope=("agent_runtime.status",),
    ),
    *rows(
        "s21",
        "5a1267ef6",
        Form.CODE,
        "retired task/proof/daemon delta prefixes; the AST gate replaces raw source scanning",
        "task.",
        "proof.attached",
        "daemon.",
        "proof.added",
        "daemon.status",
        scope=("agent_runtime.stream",),
    ),
    *rows(
        "s21",
        "5a1267ef6",
        Form.CODE,
        "unreachable display-title arms; exact AST-rendered shapes replace quote-sensitive source scans",
        "kind == 'proof'",
        "kind == 'qa_verdict'",
        "qa.verdict_recorded",
        scope=("agent_runtime.observability",),
    ),
    *rows(
        "s27",
        "5a1267ef6",
        Form.ATTR,
        "the recovery marker helper left store with the deleted recovery_flags module",
        "mark_incident_closed_for_recovery",
        scope=("agent_runtime.store",),
    ),
    *rows(
        "s24",
        "5a1267ef6",
        Form.CODE,
        "status no longer projects the deleted repo-bundle read lane",
        "RepoBundleStore",
        "repo_bundle_summary",
        "repo_bundle_delivery_summary",
        "bundle_queue_summary",
        "repo_lock_summary",
        scope=("agent_runtime.status",),
    ),
    *rows(
        "s27",
        "5a1267ef6",
        Form.CODE,
        "continuity return and child-return emission no longer stamp a stage row",
        "stage_id",
        scope=("agent_runtime.continuity", "agent_runtime.child_events"),
    ),
    *rows(
        "s27",
        "5a1267ef6",
        Form.CODE,
        "the deleted recovery_flags module must not regain a store import",
        "recovery_flags",
        scope=("agent_runtime.store",),
    ),
    *rows(
        "s33",
        "0c9d48d9f",
        Form.ATTR,
        "baseline-only repo-context helpers lost their last production caller",
        "_baseline_snapshot_dir",
        "_is_harness_litter_path",
        "_paths_from_status_line",
        "_snapshot_tracked_dirty_files",
        "_status_manifest",
        scope=("agent_runtime.repo_context",),
    ),
    # Wave 4 migrated rows in this block: 391.
    # -- S40 — objective templates ---------------------------------------
    *rows(
        "s40",
        "25ea4439a",
        Form.MODULE,
        "the renderer had no production caller; templates moved into skills",
        "agent_runtime.objective_templates",
    ),
    *rows(
        "s40",
        "25ea4439a",
        Form.CODE,
        "the renderer and its module, banned repo-wide — this row's original "
        "gate was already RED once for matching its own removal note, which is "
        "the defect the shared AST scanner retires",
        "objective_templates",
        "render_objective",
    ),
    *rows(
        "s40",
        "25ea4439a",
        Form.PATH,
        "the module file itself",
        "agent_runtime/objective_templates.py",
    ),
    # -- S41 — dead import bindings ---------------------------------------
    *rows(
        "s41",
        "25ea4439a",
        Form.ATTR,
        "import bindings with no remaining use in hermes_cli.harness; the "
        "harness parts are exec'd into that module's globals, so the NAMESPACE "
        "is the gate and per-file scanning would miss them",
        "human_task_line",
        "task_summary",
        "operator_takeover_worker",
        "LegacyOrchestratorRemoved",
        "launcher_visual_cleanup_needed",
        "OPERATOR_RESOLVABLE_TURN_STATES",
        "find_discovery_task",
        "worker_session_summary",
        scope=("hermes_cli.harness",),
    ),
    *rows(
        "s41",
        "25ea4439a",
        Form.ATTR,
        "dead import bindings on persona_runtime after the decision lane moved",
        "Protocol",
        "AgentDecision",
        "parse_structured_decision",
        "validate_decision_for_role",
        "validate_planning_decision",
        "TERMINAL_ENVELOPE_LANE_MISSION_WORKER",
        "RunBudgetExceeded",
        scope=("agent_runtime.persona_runtime",),
    ),
    *rows(
        "s41",
        "25ea4439a",
        Form.ATTR,
        "the two cli_format helpers whose only callers were the retired task "
        "lane; their absence is asserted directly rather than through a grep",
        "human_task_line",
        "task_summary",
        scope=("agent_runtime.cli_format",),
    ),
    # -- S42 — scope_control + harness part helpers ------------------------
    *rows(
        "s42",
        "25ea4439a",
        Form.ATTR,
        "the issue-discovery lane went with the mission lane",
        "untriaged_issue_discoveries",
        "find_discovery_task",
        scope=("agent_runtime.scope_control",),
    ),
    *rows(
        "s42",
        "25ea4439a",
        Form.ATTR,
        "harness-part helpers reachable only from retired verbs",
        "_resolve_board_id_for_read",
        "_event_value",
        "_task_events",
        "_clear_task_recovery_markers",
        "_safe_operator_text",
        "_safe_issue_summary",
        "_incident_history_row",
        "_incident_cursor_ts",
        "_archived_task_summary",
        scope=("hermes_cli.harness",),
    ),
    # -- S43 — individual dead symbols ------------------------------------
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "individually dead: zero production references at the cut",
        "is_default_board_id",
        scope=("agent_runtime.board_models",),
    ),
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "individually dead: zero production references at the cut",
        "FieldContract",
        scope=("agent_runtime.decision_contract_registry",),
    ),
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "individually dead: zero production references at the cut",
        "_extract_first_json_blob",
        scope=("agent_runtime.decision_schema",),
    ),
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "individually dead: zero production references at the cut",
        "rotate_if_needed",
        scope=("agent_runtime.event_rotation",),
    ),
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "individually dead: zero production references at the cut",
        "repo_land_lock",
        scope=("agent_runtime.locks",),
    ),
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "a SELF-RECURSIVE orphan — a naive reference count reports it as used, "
        "which is why the row exists at all",
        "_truncate_free_fields",
        scope=("agent_runtime.packets",),
    ),
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "path helpers for stores the mission-lane retirement removed",
        "stagec_artifacts_task_dir",
        "packet_artifacts_task_dir",
        "packet_raw_artifact_path",
        scope=("agent_runtime.paths",),
    ),
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "individually dead: zero production references at the cut",
        "MEMBER_STATE_KINDS",
        "_LEGACY_SEGMENTS",
        scope=("agent_runtime.profile_artifact_sync",),
    ),
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "individually dead: zero production references at the cut",
        "SYNC_STATES",
        scope=("agent_runtime.realm_sync",),
    ),
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "individually dead: zero production references at the cut",
        "RedactionScanner",
        scope=("agent_runtime.redaction",),
    ),
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "diff-heuristic regexes for the retired weakens-tests check",
        "_DIFF_TEST_FILE_RE",
        "_DIFF_REMOVED_ASSERT_RE",
        "_DIFF_ADDED_SKIP_RE",
        scope=("agent_runtime.repo_context",),
    ),
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "individually dead: zero production references at the cut",
        "BACKGROUND_LANE",
        scope=("agent_runtime.runtime_instances",),
    ),
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "individually dead: zero production references at the cut",
        "_relative_runtime_path",
        scope=("agent_runtime.self_test_evidence",),
    ),
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "individually dead: zero production references at the cut",
        "harness_skill_installed_ok",
        scope=("agent_runtime.skill_install",),
    ),
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "individually dead: zero production references at the cut",
        "RESULT_ACTIONS",
        scope=("agent_runtime.skill_promotion",),
    ),
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "individually dead: zero production references at the cut",
        "PUBLISHABLE_REASONS",
        scope=("agent_runtime.skill_publishability",),
    ),
    *rows(
        "s43",
        "25ea4439a",
        Form.ATTR,
        "individually dead: zero production references at the cut",
        "_profile_readiness_cache_clear",
        scope=("agent_runtime.tool_visibility",),
    ),
    # -- S44 — the role-envelope / role-checklist store family --------------
    *rows(
        "s44",
        "4e7aa0066",
        Form.MODULE,
        "the store family was ruled CUT: 275 lines, six events, two writer-less "
        "checkpoint EntityClass rows and eight orphaned path helpers",
        "agent_runtime.role_envelopes",
    ),
    *rows(
        "s44",
        "4e7aa0066",
        Form.ATTR,
        "role_checklists.py went 420 -> 113 lines around a surviving "
        "validate_checklist_payload_structure. THAT SURVIVAL REASON HAS SINCE "
        "EXPIRED and the row is kept on a corrected one (S66): S65 (f9aa0faab) "
        "deleted decision_contract_registry.validate_payload_keys — the caller "
        "the reason named — and the `hermes harness contracts verify-examples` "
        "verb that reached it, then deleted agent_runtime.role_checklists "
        "whole. The s65 MODULE row is now the stronger absence; these member "
        "rows stay because a fork sync could hand any one of them back "
        "individually",
        "RoleChecklistStore",
        "RoleChecklist",
        "RoleChecklistItem",
        "checklist_for_task_stage",
        "checklist_summary",
        "item_summary",
        "normalize_role_id",
        "TaskLike",
        "_typed_stage_for_checklist",
        "_promotion_rule",
        "_template_items",
        "_item",
        "_safe_payload",
        "_dedupe",
        "stage_checklist_hud",
        "validate_decision_checklist_payload",
        "sanitize_decision_checklist_payload",
        "apply_decision_checklist_updates",
        scope=("agent_runtime.role_checklists",),
    ),
    *rows(
        "s44",
        "4e7aa0066",
        Form.EVENT,
        "de-registered with their emitters in the same commit — the S55 gate "
        "makes splitting those two halves red, on purpose",
        "role_envelope.opened",
        "role_envelope.continued",
        "role_envelope.paused",
        "role_envelope.closed",
        "role_checklist.created",
        "role_checklist.item_updated",
    ),
    *rows(
        "s44",
        "4e7aa0066",
        Form.ATTR,
        "eight path helpers orphaned by the store cut",
        "role_envelopes_dir",
        "role_envelopes_task_dir",
        "role_envelope_path",
        "role_checklists_dir",
        "role_checklists_task_dir",
        "role_checklist_path",
        "role_checklist_events_dir",
        "role_checklist_event_path",
        scope=("agent_runtime.paths",),
    ),
    # -- S45 — test-only whole modules --------------------------------------
    *rows(
        "s45",
        "be759935c",
        Form.MODULE,
        "SETTLED RULE: a module whose entire importer set is the test written "
        "to exercise it is a closed loop, not covered code",
        "agent_runtime.budget_approval",
        "agent_runtime.context_requests",
        "agent_runtime.role_contracts",
        "agent_runtime.stage_intent",
    ),
    *rows(
        "s45",
        "be759935c",
        Form.PATH,
        "the four dedicated test files went with their modules (21 tests)",
        "tests/agent_runtime/test_budget_approval.py",
        "tests/agent_runtime/test_context_requests.py",
        "tests/agent_runtime/test_stage_intent.py",
        "tests/agent_runtime/test_stage53_contracts.py",
    ),
    *rows(
        "s45",
        "be759935c",
        Form.ATTR,
        "the context-request lane's whole public surface",
        "add_context_request",
        "has_unresolved_context_request",
        "fulfilled_context_bundles",
        "_fulfill_request",
        "_allowed_roots",
        "_resolve_allowed_path",
        "_mask_secret_lines",
        scope=("agent_runtime",),
    ),
    *rows(
        "s45",
        "be759935c",
        Form.ATTR,
        "StageStatus went with stage_intent; importing it from the package "
        "must raise rather than resolve",
        "StageStatus",
        scope=("agent_runtime.states", "agent_runtime"),
    ),
    # -- S46 — the incremental projection lane ------------------------------
    *rows(
        "s46",
        "3d0935e51",
        Form.ATTR,
        "the INCREMENTAL projection lane was reached only from five test call "
        "sites; full_rebuild() is the one production entry and it SURVIVES",
        "ProjectorResult",
        "LEASE_TTL_SECONDS",
        scope=("agent_runtime.projector",),
    ),
    *rows(
        "s46",
        "3d0935e51",
        Form.CLASS_ATTR,
        "the lease had NO other live consumer — full_rebuild never acquired "
        "it, so it died whole with its one caller and nothing about "
        "single-writer safety changed",
        "projector.Projector.apply_pending",
        "projector.Projector.acquire_lease",
        "projector.Projector._count_pending",
        scope=_AR,
    ),
    *rows(
        "s46",
        "3d0935e51",
        Form.CODE,
        "the lane's distinctive names, banned repo-wide. NOT `update`/`get`: "
        "S52 established that gating ordinary store method names is a false "
        "positive machine",
        "ProjectorResult",
        "projector_lease",
        "LEASE_TTL_SECONDS",
        "SLO_INCREMENTAL_APPLY_MS",
    ),
    # -- S47 — the wire fields that could only report a constant ------------
    *rows(
        "s47",
        "d88ea8b55",
        Form.ATTR,
        "the config block S44's store cut left governing nothing; it shipped "
        "on the live wire reading enabled: true, which no code implemented",
        "RoleEnvelopeConfig",
        scope=("agent_runtime.runtime_config", "agent_runtime.config"),
    ),
    *rows(
        "s47",
        "d88ea8b55",
        Form.ATTR,
        "the loader for a config block nothing governs",
        "_role_envelope_config",
        scope=("agent_runtime.config",),
    ),
    *rows(
        "s47",
        "d88ea8b55",
        Form.CODE,
        'the config KEY itself. S47 gated it as THREE separate source SHAPES '
        'scoped to two modules — `raw.get("role_envelope")` and '
        '`role_envelope=` in config.py, `getattr(cfg, "role_envelope"` in '
        'migrations.py. One name row subsumes all three and widens them '
        'repo-wide, which the text scanner could not do: `role_envelope` still '
        'appears in SIX production files today, every one of them a comment '
        'recording its own retirement. By prefix it also catches the S44 wire '
        'key `role_envelopes` and the eight `role_envelope*` path helpers',
        "role_envelope",
    ),
    *rows(
        "s47",
        "d88ea8b55",
        Form.CODE,
        "RoleEnvelopeConfig and the five migrations range validators that "
        "range-checked its dead knobs — a relationship between unread fields "
        "is not governance",
        "RoleEnvelopeConfig",
        "role_envelope.max_same_session_continuations",
        "role_envelope.max_no_progress_repeats",
        "role_envelope.max_fix_envelopes_per_stage",
        "role_envelope.max_checklist_items_rendered",
        "role_envelope.max_foreign_checklist_summaries",
    ),
    *rows(
        "s47",
        "d88ea8b55",
        Form.ATTR,
        "the operator-channel task lookup; status.build_status passed its OWN "
        "tasks = [] literal, so no production caller could supply a task",
        "_TaskLookup",
        scope=("agent_runtime.operator_channels",),
    ),
    # -- S48 — CLI entity-row consolidation ---------------------------------
    *rows(
        "s48",
        "71a96b517",
        Form.ATTR,
        "the hand-written CLI twins are gone: every `hermes harness` entity row "
        "is now a RE-KEY of the snapshot builder that already owned the "
        "question, so the CLI can no longer leak what the wire masks",
        "_office_item_row",
        "read_realm_sync_sidecar",
        "exact_scoped_instance_ids",
        scope=("hermes_cli.harness",),
    ),
    # -- S49 — operator_control + production_envelope -----------------------
    *rows(
        "s49",
        "c58d759b9",
        Form.MODULE,
        "145 lines whole; three operator.takeover.* contracts went with it",
        "agent_runtime.operator_control",
    ),
    *rows(
        "s49",
        "be6e2efb8",
        Form.MODULE,
        "every H6/H8/H9 claim in it was checked against the tree and was FALSE "
        "— hand-written prose keyed on flags that no longer exist",
        "agent_runtime.production_envelope",
    ),
    *rows(
        "s49",
        "c58d759b9",
        Form.EVENT,
        "de-registered with the module",
        "operator.takeover.requested",
        "operator.takeover.approval_required",
        "operator.takeover.applied",
    ),
    *rows(
        "s49",
        "c58d759b9",
        Form.CODE,
        "the takeover worker and its capability id",
        "operator_takeover_worker",
        "worker.takeover",
    ),
    # -- S50 — launcher process hygiene -------------------------------------
    *rows(
        "s50",
        "c58d759b9",
        Form.MODULE,
        "164 lines whole; no registered event type may carry `launcher` either",
        "agent_runtime.launcher_process_hygiene",
    ),
    *rows(
        "s50",
        "c58d759b9",
        Form.CODE,
        "the module's whole surface, banned repo-wide",
        "clean_launcher_visual_processes",
        "launcher_visual_cleanup_needed",
        "_parse_tasklist_csv",
        "_safe_process_names",
    ),
    # -- S52 — the repo-bundle WRITE lane -----------------------------------
    *rows(
        "s52",
        "15ee23b21",
        Form.EVENT,
        "seven contracts de-registered with the write lane",
        "repo_bundle.created",
        "repo_bundle.updated",
        "repo_bundle.assigned",
        "repo_bundle.running",
        "repo_bundle.verified",
        "repo_bundle.rejected",
        "repo_bundle.woke",
    ),
    *rows(
        "s25",
        "25ea4439a",
        Form.EVENT,
        "retired one commit BEHIND the S24 cut that deleted its writer — the "
        "exact recurrence S55's structural gate now catches in-commit",
        "repo_bundle.delivered",
    ),
    *rows(
        "s52",
        "15ee23b21",
        Form.CODE,
        "the DISTINCTIVE subset of the removed write lane. Deliberately not "
        "`update` / `get` / `_write`: those are ordinary names every store has, "
        "and gating them is a false positive machine",
        "create_or_update_from_task",
        "wake_ready_dependencies",
        "cancel_superseded",
        "acquire_repo_bundle_locks",
        "release_repo_bundle_locks",
        "desired_bundles_for_task",
        "merge_desired_bundle",
        "_repo_lock_conflicts",
        "_write_repo_locks",
        "bundle_id_for",
        "safe_bundle_state",
        "owner_for_repo",
        "qa_waiting_on",
        "REPO_BUNDLE_STATES",
        "TERMINAL_REPO_BUNDLE_STATES",
        "DELIVERED_REPO_BUNDLE_STATES",
        "REPO_LOCK_MODES",
        "WAKE_DEPENDENCY_DELIVERED",
        "_REPO_OWNER_RULES",
    ),
    # -- S53 — the lane WRITE lane ------------------------------------------
    *rows(
        "s53",
        "2f8f74b9e",
        Form.CLASS_ATTR,
        "no writer can create a GoalRuntimeInstance row any more; the "
        "runtime_instances[\"lanes\"] PROJECTION survives, only the top-level "
        "duplicate went",
        "runtime_instances.GoalRuntimeInstanceStore.create_lane",
        "runtime_instances.GoalRuntimeInstanceStore.transition",
        "runtime_instances.GoalRuntimeInstanceStore.park_lane",
        "runtime_instances.GoalRuntimeInstanceStore.resume_lane",
        "runtime_instances.GoalRuntimeInstanceStore.park_open_task",
        "runtime_instances.GoalRuntimeInstanceStore.mark_terminal_for_task",
        "runtime_instances.GoalRuntimeInstanceStore.save",
        "runtime_instances.GoalRuntimeInstanceStore.active_foreground",
        "runtime_instances.GoalRuntimeInstanceStore.park_foreground_except",
        scope=_AR,
    ),
    *rows(
        "s53",
        "2f8f74b9e",
        Form.ATTR,
        "the lane state machine went with its writers",
        "LANE_STATES",
        "_ALLOWED_TRANSITIONS",
        "TERMINAL_STATE",
        "_safe_reason",
        "_safe_token",
        scope=("agent_runtime.runtime_instances",),
    ),
    *rows(
        "s53",
        "2f8f74b9e",
        Form.EVENT,
        "four contracts de-registered with the lane write lane",
        "lane.created",
        "lane.transitioned",
        "lane.transition_rejected",
        "foreground_runtime.closed",
    ),
    # -- S54 — 30 individually dead symbols across 21 modules ---------------
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead: reachability re-verified per symbol immediately "
        "before the cut, and checked for dynamic dispatch",
        "needs_rebalance",
        scope=("agent_runtime.board_order",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead at the cut",
        "discover_classes",
        scope=("agent_runtime.checkpoint",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "S11/S16 retired role-shaped decision filtering and the object-contract "
        "surface; these three were the accessors left pointing at it",
        "all_decision_contracts",
        "role_shape_ids",
        "object_contract",
        scope=("agent_runtime.decision_contract_registry",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead at the cut",
        "to_decision_jsonable",
        scope=("agent_runtime.decision_schema",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead at the cut",
        "no_product_edit_dirty_check",
        scope=("agent_runtime.dirty_state",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead at the cut",
        "LegacyOrchestratorRemoved",
        scope=("agent_runtime.errors",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead at the cut",
        "live_base_offset",
        scope=("agent_runtime.event_rotation",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "the task-event archive lane went with the task lane",
        "archive_task_events",
        "compact_archived_task_events",
        "_safe_event_task_filename",
        "_line_is_compacted_event",
        "_safe_int",
        scope=("agent_runtime.events",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead at the cut",
        "desired_parents_by_agent",
        scope=("agent_runtime.flow_graph",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead at the cut. NOTE the deliberate KEEP alongside "
        "these: incidents.MODEL_INVALID_OUTPUT stays, because its VALUE is "
        "live as a bare literal in observability.py",
        "classify_exception",
        "IncidentClassification",
        "_safe_budget_summary",
        "_safe_exception_summary",
        scope=("agent_runtime.incidents",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead at the cut",
        "tick_lock",
        scope=("agent_runtime.locks",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead at the cut",
        "latest_packets_for_task",
        "make_packet",
        "record_packet",
        "record_decision_packets",
        "UNSUPPORTED_HANDOFF_MODES",
        "_reject_unknown_packet_keys",
        scope=("agent_runtime.packets",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead at the cut",
        "stagec_artifacts_dir",
        scope=("agent_runtime.paths",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead at the cut",
        "current_profile_context_rows",
        "mcp_owner_profile_name",
        "MCP_SERVER_PERSONA_OWNERS",
        scope=("agent_runtime.profile_context",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead at the cut",
        "_missing_skill_names",
        scope=("agent_runtime.profile_readiness",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "an ADVERTISED SEAM WITH NO CALLER — its docstring claimed the launcher "
        "fetch lane and a future CLI verb both called it; the Launcher repo has "
        "zero references and the verb was never wired",
        "load_final_model_input_for_context",
        "_mission_chat_template_prompt_chars",
        scope=("agent_runtime.prompt_observability",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead at the cut",
        "assert_pinned",
        "_normalized_path",
        scope=("agent_runtime.resolution",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead at the cut",
        "self_test_summary",
        scope=("agent_runtime.self_test_evidence",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead at the cut",
        "promotion_provenance",
        scope=("agent_runtime.skill_promotion",),
    ),
    *rows(
        "s54",
        "90dbe908a",
        Form.ATTR,
        "individually dead at the cut",
        "emit_task_refresh",
        scope=("agent_runtime.state_patches",),
    ),
    # -- S56 — the worker-session lane, seven config blocks -----------------
    *rows(
        "s56",
        "be6e2efb8",
        Form.MODULE,
        "618 lines whole. The guarded sub-step was re-verified on the LIVE "
        "runtime root paths.store_root() resolves to: fifteen persona "
        "instances, every one active_worker_session_id: null, and NO "
        "worker_sessions/ directory. METHOD NOTE: a profile directory that "
        "LOOKS like a store root is not evidence about the store root",
        "agent_runtime.worker_sessions",
    ),
    *rows(
        "s56",
        "be6e2efb8",
        Form.ATTR,
        "the model and its path/lock helpers went with the store",
        "WorkerSession",
        scope=("agent_runtime.models",),
    ),
    *rows(
        "s56",
        "be6e2efb8",
        Form.ATTR,
        "path helpers for a store that no longer exists",
        "worker_sessions_dir",
        "worker_session_path",
        "worker_context_dir",
        "proof_sandbox_root",
        scope=("agent_runtime.paths",),
    ),
    *rows(
        "s56",
        "be6e2efb8",
        Form.ATTR,
        "the lock for a store that no longer exists",
        "worker_session_lock",
        scope=("agent_runtime.locks",),
    ),
    *rows(
        "s56",
        "be6e2efb8",
        Form.EVENT,
        "all ten worker_session.* contracts de-registered in the SAME commit "
        "as their emitters — S55 makes splitting them red, on purpose",
        "worker_session.opened",
        "worker_session.assigned",
        "worker_session.resumed",
        "worker_session.heartbeat",
        "worker_session.context_absorbed",
        "worker_session.steered",
        "worker_session.possessed",
        "worker_session.released",
        "worker_session.watchdog_warning",
        "worker_session.closed",
        "worker_session.compressed",
        "worker_session.possession_requested",
    ),
    *rows(
        "s56",
        "be6e2efb8",
        Form.ATTR,
        "the worker-derived persona surface; derive_from_workers became the "
        "worker-free ensure_for_personas(personas) with the same behaviour",
        "ACTIVE_PERSONA_WORKER_STATES",
        "_worker_carries_live_binding",
        "_live_chat_bindings",
        "_terminate_live_chat_bindings",
        "persona_instance_runtime_enabled",
        "persona_assignment_store_enabled",
        scope=("agent_runtime.persona_assignments",),
    ),
    *rows(
        "s56",
        "be6e2efb8",
        Form.CLASS_ATTR,
        "the worker branch of the persona-instance derivation was unreachable "
        "on the live tree — build_snapshot had been feeding it a workers = [] "
        "literal since S47",
        "persona_assignments.PersonaInstanceStore.derive_from_workers",
        "persona_assignments.PersonaInstanceStore.update_from_worker",
        "persona_assignments.PersonaInstanceStore._goal_id_for_worker",
        scope=_AR,
    ),
    *rows(
        "s56",
        "be6e2efb8",
        Form.ATTR,
        "the busy reason for a worker state nothing can enter",
        "BUSY_ACTIVE_WORKER",
        scope=("agent_runtime.persona_profile_binding",),
    ),
    *rows(
        "s56",
        "be6e2efb8",
        Form.ATTR,
        "six runtime_config blocks removed whole, plus the compat shim that "
        "mapped one unread block onto another",
        "ContinuousRoleSessionConfig",
        "EnterpriseWorkerSessionsConfig",
        "NormalWorkerFlowConfig",
        "RepoBundleRoutingConfig",
        "SimplifiedAgentContractConfig",
        "SwarmConfig",
        scope=("agent_runtime.runtime_config",),
    ),
    *rows(
        "s56",
        "be6e2efb8",
        Form.ATTR,
        "the six block loaders and the enterprise/role-session compat shim",
        "_continuous_role_sessions_config",
        "_enterprise_worker_sessions_config",
        "_normal_worker_flow_config",
        "_repo_bundle_routing_config",
        "_simplified_agent_contract_config",
        "_swarm_config",
        "_apply_enterprise_role_session_compat",
        scope=("agent_runtime.config",),
    ),
    *rows(
        "s56",
        "be6e2efb8",
        Form.CODE,
        "the six removed config blocks and the three SupervisionConfig fields "
        "PRUNED down to child_events_enabled, the one field continuity.py "
        "reads. A stale operator yaml that still sets them loads and is "
        "IGNORED (pinned separately) — this row bans the READER",
        "continuous_role_sessions",
        "enterprise_worker_sessions",
        "normal_worker_flow",
        "repo_bundle_routing",
        "simplified_agent_contract",
        "supervision.recursive_enabled",
        "supervision.hierarchical_budget_enabled",
        "supervision.deploy_verification_enabled",
    ),
    *rows(
        "s56",
        "be6e2efb8",
        Form.CODE,
        "the roster gate is retired: the persona-instance roster and the "
        "persona-assignments section are UNCONDITIONAL in both build_snapshot "
        "and build_status. Six live profiles GAIN the roster",
        "persona_instance_runtime_enabled",
        "persona_assignment_store_enabled",
    ),
    *rows(
        "s56",
        "be6e2efb8",
        Form.CODE,
        "the constant-only status projections: after S52 deleted every writer "
        "these could only report {lock_count: 0, locks: []}",
        "repo_lock_summary",
        "bundle_queue_summary",
        "repo_bundle_summary",
        "repo_bundle_delivery_summary",
        "REPO_BUNDLE_DELIVERY_CONTRACT",
        "REPO_BUNDLE_CHECKOUT_STATUS",
        "_swarm_budget_summary",
    ),
    # -- S57 — the 29-field config ledger and the repo-bundle store ---------
    *rows(
        "s57",
        "23be05e00",
        Form.MODULE,
        "status.py was the last importer; the model, the path helper, the "
        "migration count row and the serve fingerprint entry went with it",
        "agent_runtime.repo_bundles",
    ),
    *rows(
        "s57",
        "23be05e00",
        Form.PATH,
        "the module file itself",
        "agent_runtime/repo_bundles.py",
    ),
    *rows(
        "s57",
        "23be05e00",
        Form.ATTR,
        "the 31-field model and its path helper",
        "RepoBundle",
        scope=("agent_runtime.models",),
    ),
    *rows(
        "s57",
        "23be05e00",
        Form.ATTR,
        "the 31-field model's path helper",
        "repo_bundle_path",
        scope=("agent_runtime.paths",),
    ),
    *rows(
        "s57",
        "23be05e00",
        Form.CODE,
        "the store class itself, banned repo-wide",
        "RepoBundleStore",
    ),
    *rows(
        "s57",
        "23be05e00",
        Form.CODE,
        "TWENTY-NINE RuntimeConfig scalars with no production reader, each "
        "re-verified by hand (AST attribute form, getattr string form, plain "
        "text scan) before the cut. NOT ONE survived with a reader the gate "
        "had missed. The neighbour lock_acquire_timeout_seconds is KEPT and is "
        "the whole argument for the gate's shape: it reads identically and is "
        "LIVE through the getattr STRING form at locks.py:133",
        "heartbeat_ttl_seconds",
        "max_actions_per_tick",
        "daemon_enabled",
        "daemon_interval_seconds",
        "daemon_idle_interval_seconds",
        "daemon_heartbeat_seconds",
        "task_create_auto_start_daemon",
        "preferred_goal_execution_mode",
        "live_run_max_wall_seconds",
        "live_run_max_api_calls",
        "live_run_max_total_tokens",
        "live_run_iteration_budget",
        "scope_wait_deadline_seconds",
        "run_lease_seconds",
        "tool_wait_timeout_seconds",
        "liveness_enabled",
        "liveness_poll_seconds",
        "liveness_quiet_strikes",
        "liveness_hung_seconds",
        "child_progress_min_interval_seconds",
        "deploy_timeout_seconds",
        "mission_max_total_tokens",
        "mission_wall_clock_deadline_seconds",
        "neko_recovery_attempt_cap",
        "neko_extension_cap",
        "artifact_storage_low_watermark_mb",
        "artifact_storage_high_watermark_mb",
        "artifact_storage_critical_watermark_mb",
    ),
    # -- S58 / campaign Wave 3 — wave orphans + duplicate wire block -------
    *rows(
        "s58",
        "30f527180",
        Form.ATTR,
        "receiver-aware production scan found no caller",
        "_worker_is_active",
        scope=("agent_runtime.dirty_state",),
    ),
    *rows(
        "s58",
        "30f527180",
        Form.ATTR,
        "receiver-aware production scan found no caller",
        "context_dir",
        scope=("agent_runtime.paths",),
    ),
    *rows(
        "s58",
        "30f527180",
        Form.ATTR,
        "the packet emitter lane is retired and the historical accessor had no production caller",
        "latest_packet",
        scope=("agent_runtime.packets",),
    ),
    *rows(
        "s58",
        "30f527180",
        Form.ATTR,
        "receiver-aware production scan found no caller",
        "persona_bound_profile_name",
        scope=("agent_runtime.profile_context",),
    ),
    *rows(
        "s58",
        "30f527180",
        Form.CLASS_ATTR,
        "the list_for_task alias was never called in production",
        "runtime_instances.GoalRuntimeInstanceStore.latest_for_task",
        scope=_AR,
    ),
    *rows(
        "s58",
        "30f527180",
        Form.ATTR,
        "the enum's last models import was itself unused; WorkerSessionState remains live",
        "PossessionState",
        scope=("agent_runtime.states",),
    ),
    *rows(
        "s58",
        "30f527180",
        Form.ATTR,
        "the rendered HUD wrapper had zero production callers",
        "situational_hud_content_for_instance",
        scope=("agent_runtime.runtime_hud",),
    ),
    *rows(
        "s58",
        "30f527180",
        Form.CLASS_ATTR,
        "the private chat-binding guard had zero callers",
        "persona_assignments.PersonaInstanceStore._guard_or_replace_chat",
        scope=_AR,
    ),
    *rows(
        "s58",
        "30f527180",
        Form.IMPORT,
        "dead duplicate top-level harness import; real consumers bind their own dependencies",
        "AgentRuntimeError",
        "AlreadyExists",
        "EventPayloadTooLarge",
        "InvalidTransition",
        "ProofMissing",
        "RuntimeRootMismatch",
        "StaleRevision",
        "StaleRun",
        "StoreCorrupt",
        "SyncConflict",
        "initialize_persona_chat_runtime_registry",
        "build_snapshot",
        "write_snapshot",
        "_apply_fields",
        "_error_code_for_exception",
        "_error_hint",
        "_load_request_json",
        "_quiet_output",
        "_redact_paths",
        "_safe_error_message",
        "_table_output",
        scope=("hermes_cli/harness.py",),
    ),
    # -- S59 / Round 2 — closed-loop readers and chat-busy residue ---------
    *rows(
        "s59",
        "799249fbf",
        Form.ATTR,
        "the S58 guard removal left no constructor or caller; the live "
        "PersonaChatBusyError lease class is a separate continuity lane",
        "ChatBusyError",
        "_live_chat_binding",
        "_terminate_live_chat_binding",
        scope=("agent_runtime.persona_assignments",),
    ),
    *rows(
        "s59",
        "799249fbf",
        Form.ATTR,
        "the payload formatter was reachable only from dead ChatBusyError catches",
        "_chat_busy_payload",
        scope=("hermes_cli.harness",),
    ),
    *rows(
        "s59",
        "799249fbf",
        Form.IMPORT,
        "dead import bindings for an exception no production path can raise",
        "ChatBusyError",
        scope=(
            "hermes_cli/harness.py",
            "hermes_cli/harness_support.py",
            "hermes_cli/harness_parts/persona_commands.py",
        ),
    ),
    *rows(
        "s59",
        "799249fbf",
        Form.CODE,
        "all five exception arms were unreachable after S58 removed the sole raiser",
        "except ChatBusyError",
    ),
    *rows(
        "s59",
        "799249fbf",
        Form.ATTR,
        "the last production caller left at S58; its sole remaining importer was its own test",
        "get_persisted_persona",
        scope=("agent_runtime.config",),
    ),
    *rows(
        "s59",
        "799249fbf",
        Form.CODE,
        "distinctive closed-loop names are banned repo-wide after their test-only consumers were removed",
        "get_persisted_persona",
        "read_bundle_promotion_record",
        "bundle_promotion_record_path",
        "repo_bundles_dir",
        "repo_bundles_task_dir",
        "_live_chat_binding",
        "_terminate_live_chat_binding",
    ),
    # -- S60 / Round 3 — Round-2 residue ---------------------------------
    *rows(
        "s60",
        "pending",
        Form.CODE,
        "Round 2 removed the only agent_busy producer; the exit-code consumer "
        "and its cross-stack parser retired together while chat_busy remains live",
        "agent_busy",
    ),
    *rows(
        "s60",
        "pending",
        Form.ATTR,
        "the last reader (_live_chat_binding) retired in Round 2",
        "LIVE_RUN_STATES",
        scope=("agent_runtime.persona_assignments",),
    ),
    # -- S61 / Round 4 — persona authority + closed-loop read APIs -------
    *rows(
        "s61",
        "c4a1fdef5",
        Form.CLASS_ATTR,
        "the free-chat execution island had no production caller; mission_chat_reply is the sole live chat runtime",
        "persona_runtime.GPTPersonaRuntime.chat_reply",
        scope=_AR,
    ),
    *rows(
        "s61",
        "c4a1fdef5",
        Form.ATTR,
        "the free-chat prompt builder and compiled role voice died with the callerless free-chat lane; profile SOUL/config owns behavioral identity",
        "_persona_chat_system_prompt",
        "_persona_chat_voice",
        scope=("agent_runtime.persona_runtime",),
    ),
    *rows(
        "s61",
        "c4a1fdef5",
        Form.CODE,
        "compiled Neko/QA/Dev behavioral instructions must not return to persona_runtime; profile SOUL/config is the only behavioral prompt authority",
        "Chief-of-staff energy",
        "You are the quality gate",
        "You are a senior engineer",
        scope=("agent_runtime.persona_runtime",),
    ),
    *rows(
        "s61",
        "c4a1fdef5",
        Form.ATTR,
        "mission_chat is the only runtime that binds a terminal-envelope scope; the retired mission and free-chat lane vocabulary advertised branches no producer constructed",
        "LANE_MISSION_WORKER",
        "LANE_MISSION_NODE",
        "LANE_MISSION_ROOT_NODE",
        "LANE_PERSONA_CHAT",
        "HARNESS_LANES",
        "ENVELOPE_LANE_NOT_GOVERNED",
        "_ungoverned_lane_fix_hint",
        scope=("agent_runtime.terminal_envelope",),
    ),
    *rows(
        "s61",
        "c4a1fdef5",
        Form.CLASS_ATTR,
        "task-era persona store readers and close helpers were called only by their own tests",
        "persona_assignments.PersonaInstanceStore.list_for_task",
        "persona_assignments.PersonaInstanceStore.close_for_task",
        "persona_assignments.PersonaAssignmentStore.contention_warnings",
        "persona_assignments.PersonaAssignmentStore._release_if_owning_goal_terminal",
        "persona_assignments.PersonaAssignmentStore.list_for_task",
        "persona_assignments.PersonaAssignmentStore.close_for_task",
        scope=_AR,
    ),
    *rows(
        "s61",
        "c4a1fdef5",
        Form.CLASS_ATTR,
        "historical runtime-instance task election helpers had no production caller; get/list_all remain the live projection read path",
        "runtime_instances.GoalRuntimeInstanceStore.list_for_task",
        "runtime_instances.GoalRuntimeInstanceStore.active_for_task",
        scope=_AR,
    ),
    *rows(
        "s61",
        "c4a1fdef5",
        Form.ATTR,
        "the three decorative runtime-instance states were read only by the callerless task election helper",
        "ACTIVE_STATE",
        "PARKED_STATE",
        "WAITING_STATE",
        scope=("agent_runtime.runtime_instances",),
    ),
    *rows(
        "s61",
        "c4a1fdef5",
        Form.CLASS_ATTR,
        "board convenience readers with no production or test caller",
        "board_store.BoardStore.default_board_for_workspace",
        "board_store.BoardStore.archive_board",
        scope=_AR,
    ),
    *rows(
        "s61",
        "c4a1fdef5",
        Form.CLASS_ATTR,
        "whole-log convenience iterators were test-only; tests now observe the live logical-offset reader",
        "events.EventLog.iter_all",
        "events.EventLog.iter_since",
        "events.CachedEventLog.iter_all",
        "events.CachedEventLog.iter_since",
        scope=_AR,
    ),
    *rows(
        "s61",
        "c4a1fdef5",
        Form.CLASS_ATTR,
        "test-only convenience accessors were reasserted through their live underlying authorities",
        "read_model.ReadModel.integrity_check",
        "store.IncidentStore.list_open",
        "volatile_tail.VolatileTail.shortfall_rows",
        scope=_AR,
    ),
    *rows(
        "s61",
        "c4a1fdef5",
        Form.ATTR,
        "test-only or compatibility wrappers with no production caller",
        "default_chat_session_id_for_instance",
        scope=("agent_runtime.persona_assignments",),
    ),
    *rows(
        "s61",
        "c4a1fdef5",
        Form.ATTR,
        "test-only or compatibility wrappers with no production caller",
        "diverged_bindings",
        scope=("agent_runtime.persona_profile_binding",),
    ),
    *rows(
        "s61",
        "c4a1fdef5",
        Form.ATTR,
        "the one-skill queue wrapper had no caller; the live command uses the atomic batch API",
        "queue_skill_for_next_turn",
        scope=("agent_runtime.queued_skills",),
    ),
    *rows(
        "s61",
        "c4a1fdef5",
        Form.ATTR,
        "the batch-frame selector duplicated the live liveness-aware stream branch and was called only by direct unit tests",
        "select_batch_frame",
        scope=("agent_runtime.stream",),
    ),
    *rows(
        "s61",
        "c4a1fdef5",
        Form.ATTR,
        "the artifact scanner implementation and its private vocabulary were a test-only closed loop; shared live redaction patterns remain",
        "BasicRedactionScanner",
        "RedactionStatus",
        "SCANNER_SECRET_ASSIGNMENT_RE",
        "SECRET_PATTERNS",
        scope=("agent_runtime.redaction",),
    ),
    # -- S62 / Round 4 follow-up — compiled topology residue ------------
    *rows(
        "s62",
        "f813115c8",
        Form.ATTR,
        "operator conversation ancestry now follows persisted steered_by relationships; no persona id receives synthetic root authority",
        "_mirror_child_assignment_trace_to_roots",
        "_builder_is_neko_root",
        "_builder_runtime_ids",
        "_row_runtime_ids",
        "_entry_runtime_ids",
        scope=("agent_runtime.operator_channels",),
    ),
    *rows(
        "s62",
        "f813115c8",
        Form.CODE,
        "prompt observability and conversation presentation contain no compiled Neko topology or voice fallback",
        "neko_two_dev_default",
        "Neko update",
        "task:{task_id}:neko_supervisor",
        scope=("agent_runtime.prompt_observability", "agent_runtime.operator_channels"),
    ),
    *rows(
        "s62",
        "f813115c8",
        Form.CODE,
        "a raw profile visibility persona preserves its configured or instance role instead of being coerced to the legacy supervisor role",
        'role="alice_supervisor"',
        scope=("agent_runtime.persona_assignments",),
    ),
    # -- S63 / Round 4 — operator-ruled contract cleanup ----------------
    *rows(
        "s63",
        "6502b87f8",
        Form.MODULE,
        "the task progress lane was production-callerless; its telemetry and "
        "self-test evidence islands retired with it",
        "agent_runtime.dev_discipline",
        "agent_runtime.self_test_evidence",
    ),
    *rows(
        "s63",
        "6502b87f8",
        Form.ATTR,
        "caller-less task-run write surface removed after the operator contract ruling",
        "RunProgressSink",
        scope=("agent_runtime.progress",),
    ),
    *rows(
        "s63",
        "6502b87f8",
        Form.CLASS_ATTR,
        "caller-less close/cancel writer removed with the run.closed write contract",
        "store.RunStore.close_run",
        "store.RunStore.cancel",
        scope=("agent_runtime",),
    ),
    *rows(
        "s63",
        "6502b87f8",
        Form.CLASS_ATTR,
        "profile declarations replaced the ignored role/lane MCP config language",
        "runtime_config.McpAdmissionConfig.roles",
        scope=("agent_runtime",),
    ),
    *rows(
        "s63",
        "6502b87f8",
        Form.EVENT,
        "registered writers retired; historical rows remain readable and renderable",
        "run.closed",
        "self_test.recorded",
        "self_test.loop_detected",
    ),
    *rows(
        "s63",
        "6502b87f8",
        Form.CODE,
        "retired configuration and packet/event vocabulary must not regrow in production",
        "MCP_NOT_ADMITTED_FOR_ROLE",
        "mcp_not_admitted_for_role",
        "self_test_evidence_ids",
    ),
    # -- S64 / integrated review — persona authority + inert guard cleanup --
    *rows(
        "s64",
        "3619e0f66",
        Form.ATTR,
        "raw profiles no longer receive a compiled privileged toolset fallback",
        "PROFILE_CHAT_FALLBACK_TOOLSETS",
        scope=("agent_runtime.personas",),
    ),
    *rows(
        "s64",
        "3619e0f66",
        Form.CLASS_ATTR,
        "the only production AgentRunRequest caller never set the retired read/search guard controls",
        "profile_runner.AgentRunRequest.stop_on_repeated_read_search",
        "profile_runner.AgentRunRequest.tool_budget_limits",
        scope=_AR,
    ),
    *rows(
        "s64",
        "3619e0f66",
        Form.ATTR,
        "the production-disabled read/search guard branch and helpers retired as one closed loop",
        "_enforce_aggregate_read_search_budget",
        "_read_search_warning_payload",
        "_update_guard_progress",
        "_interrupt_agent_for_budget",
        scope=("agent_runtime.profile_runner",),
    ),
    *rows(
        "s64",
        "3619e0f66",
        Form.CLASS_ATTR,
        "the read/search budget kind and trip reasons had no producer after the guard retired",
        "run_budget.RunBudgetKind.READ_SEARCH",
        "run_budget.RunBudgetTripReason.REPEATED_READ_SEARCH_LOOP",
        "run_budget.RunBudgetTripReason.AGGREGATE_READ_SEARCH_EXCEEDED",
        scope=_AR,
    ),
    # -- S65 / final dead-code closeout ---------------------------------
    *rows(
        "s65",
        "f9aa0faab",
        Form.MODULE,
        "the structured decision, packet, scope, and role-checklist modules formed a closed contract island with no production entry point",
        "agent_runtime.decision_schema",
        "agent_runtime.decision_contracts",
        "agent_runtime.decision_payload_contracts",
        "agent_runtime.decision_contract_examples",
        "agent_runtime.packets",
        "agent_runtime.scope_control",
        "agent_runtime.role_checklists",
        "agent_runtime.simplified_contract",
    ),
    *rows(
        "s65",
        "f9aa0faab",
        Form.CLASS_ATTR,
        "task-era persona mutation and lookup APIs had no production callers after the chat-owned runtime became authoritative",
        "persona_assignments.PersonaInstanceStore.ensure_for_goal",
        "persona_assignments.PersonaInstanceStore.list_for_goal",
        "persona_assignments.PersonaInstanceStore.record_context",
        "persona_assignments.PersonaAssignmentStore.attach_run",
        "persona_assignments.PersonaAssignmentStore.attach_proof",
        scope=("agent_runtime",),
    ),
    *rows(
        "s65",
        "f9aa0faab",
        Form.CLASS_ATTR,
        "run and incident stores now expose historical readers only; their caller-less mutation surfaces retired",
        "store.RunStore.update",
        "store.RunStore.list_for_task",
        "store.IncidentStore.open",
        "store.IncidentStore.close",
        scope=("agent_runtime",),
    ),
    *rows(
        "s65",
        "f9aa0faab",
        Form.ATTR,
        "task/goal and packet-artifact directories lost their final production readers",
        "goals_dir",
        "goal_path",
        "task_path",
        "packet_artifacts_dir",
        scope=("agent_runtime.paths",),
    ),
    *rows(
        "s65",
        "f9aa0faab",
        Form.EVENT,
        "the final writer for each event retired; historical log rows remain readable",
        "persona_instance.reaped",
        "persona_instance.attributed",
        "incident.opened",
        "incident.closed",
    ),
    *rows(
        "s65",
        "f9aa0faab",
        Form.CODE,
        "test-only run projection, packet checkpointing, and inactive coordinator actions must not regrow into production",
        "run_summaries",
        "packet_artifacts",
        "worker.nudge",
        "worker.resume",
        "use_output",
        "re_prompt",
        "re_scope",
        "run.cancel",
        scope=_AR,
    ),
    # -- S65 BACKFILL (filed at S66) ------------------------------------
    #
    # An independent audit of `4a21f0779..597715ba5` found 104 of 382 named cut
    # symbols with no covering row. The largest single hole is below, and its
    # cause is worth stating because it will recur: s65's `Form.MODULE` rows
    # protect modules that were DELETED WHOLE. `decision_contract_registry.py`
    # was GUTTED — 918 lines removed, the file kept — so not one of the twelve
    # public names it lost is covered by a module row. A survivor module needs
    # symbol rows.
    *rows(
        "s65",
        "f9aa0faab",
        Form.ATTR,
        "the structured AgentDecision contract island was cut out of a module "
        "that SURVIVED as the event-contract authority; no MODULE row can "
        "cover these, so each retired public name is rowed individually",
        "DecisionContract",
        "HudShape",
        "ObjectContract",
        "agent_decision_json_schema",
        "canonical_role_value",
        "context_expansion_shape_ids",
        "decision_contract",
        "hud_shape",
        "hud_shape_index_for_stage",
        "payload_contract",
        "validate_object_payload",
        "validate_payload_keys",
        scope=("agent_runtime.decision_contract_registry",),
    ),
    *rows(
        "s65",
        "05798135e",
        Form.CLASS_ATTR,
        "THE CAMPAIGN'S CENTRAL CUT, previously with no resurrection guard: "
        "S61/S64 made SOUL/profile/persona configuration the sole behavioral "
        "authority and retired the compiled role constants. Rowed as scoped "
        "CLASS_ATTR because the bare names DEV / QA are un-rowable — they are "
        "ordinary words that appear throughout live code",
        "personas.AgentRole.ALICE_SUPERVISOR",
        "personas.AgentRole.DEV",
        "personas.AgentRole.QA",
        scope=_AR,
    ),
    *rows(
        "s65",
        "f9aa0faab",
        Form.ATTR,
        "the self-test evidence store went with the task-progress island; these "
        "path helpers are the exact siblings of goals_dir / goal_path / "
        "task_path / packet_artifacts_dir, which were rowed in the SAME commit",
        "self_tests_dir",
        "self_test_task_dir",
        "self_test_record_path",
        "self_test_artifacts_dir",
        scope=("agent_runtime.paths",),
    ),
    *rows(
        "s65",
        "f9aa0faab",
        Form.ATTR,
        "RunStore became a historical reader; the terminal-state vocabulary its "
        "retired write lane branched on went with it",
        "TERMINAL_RUN_STATES",
        scope=("agent_runtime.store",),
    ),
    *rows(
        "s64",
        "3619e0f66",
        Form.ATTR,
        "the repeated read/search policy fields and their closed guard branches "
        "retired; these two tool partitions were their only inputs. NOT a CODE "
        "row: READ_SEARCH_TOOLS is a live name in model_tools.py, so a "
        "repo-wide ban would fail on correct code — the scoped attribute "
        "absence is what actually discriminates",
        "PATCH_TOOLS",
        "READ_SEARCH_TOOLS",
        scope=("agent_runtime.profile_runner",),
    ),
    *rows(
        "s65",
        "f9aa0faab",
        Form.CLASS_ATTR,
        "the prompt-contract hash rode the retired decision-contract island; no "
        "writer survives to stamp it onto a persisted instance row",
        "models.PersonaInstance.prompt_contract_hash",
        scope=_AR,
    ),
    # -- S66 — the surviving caller of an S65 cut, and this wave's own ---
    *rows(
        "s66",
        "HEAD",
        Form.CODE,
        "`hermes harness persona-instance sweep-orphans` was BROKEN ON MAIN, "
        "not merely dead: S65 deleted the store method, the owning-task release "
        "inference and the goals/ path helpers it decided on, but the CLI "
        "handler and its subparser survived, so the verb raised AttributeError "
        "on every invocation while the Launcher's committed CLI contract still "
        "advertised it. It is also unrestorable without a contract move — S65 "
        "de-registered `persona_instance.reaped`, the event the reap emitted. "
        "The missing row is the whole point: a verb string and a handler name "
        "are exactly what a symbol-only cut sweep misses",
        "sweep_orphaned_task_bound_instances",
        "_cmd_persona_instance_sweep_orphans",
        "sweep-orphans",
    ),
    *rows(
        "s66",
        "HEAD",
        Form.CODE,
        "`hermes harness contracts verify-examples` went with the decision-"
        "contract island at S65 — verb string and handler together, the same "
        "pair shape the sweep-orphans defect was missing",
        "verify-examples",
        "_cmd_contracts_verify_examples",
    ),
    *rows(
        "s66",
        "HEAD",
        Form.ATTR,
        "orphaned by S65's OWN cuts, receiver-verified at zero production "
        "references repo-wide: RunStore/IncidentStore became historical "
        "READERS, and a reader takes no write lock; IncidentStore.close was "
        "emit_incident_remove's only chokepoint",
        "run_lock",
        "incident_lock",
        scope=("agent_runtime.locks",),
    ),
    *rows(
        "s66",
        "HEAD",
        Form.ATTR,
        "the incident `remove` patch had one chokepoint, IncidentStore.close, "
        "retired at S65 together with the paired incident.closed contract",
        "emit_incident_remove",
        scope=("agent_runtime.state_patches",),
    ),
    *rows(
        "s66",
        "HEAD",
        Form.ATTR,
        "test-only after S65 took the contracts-verify lane: its single caller "
        "restated an event count the same test already asserted off "
        "event_catalog(). The ledger's settled closed-loop rule — a symbol "
        "whose whole importer set is its own test is not covered code",
        "verify_registry",
        scope=("agent_runtime.decision_contract_registry",),
    ),
    # -- S70 — the free-floating assignment lane (2026-08-09) ------------
    # `persona instance create --message/--auto-run` and `persona instance
    # message` fed a "free-floating persona assignment" queue whose only
    # durable consumer was the tick loop the 2026-07-30 chat-only purge
    # removed: a queued row dead-ended forever (the `run-once` verb the
    # envelope advertised as next step never existed), the `--auto-run`
    # in-process runner was a second, parallel turn authority beside
    # `mission-chat message`, and the display-name branch of `create`
    # silently DISCARDED the then-required `--message` (verified live
    # 2026-08-09: create --add-instance --message --auto-run landed no turn
    # anywhere). `--message` outlived the lane as launcher wire-compat and was
    # cut in S-DUP5 once the launcher stopped emitting it; argparse now rejects
    # it beside the other four, pinned by
    # tests/hermes_cli/test_harness_cli.py::test_persona_instance_create_has_no_message_flag.
    # The READ/CLOSE store side, the `persona_assignments` wire block and the
    # close/archive maintenance verbs survive for residual rows (ledger S70).
    *rows(
        "s70",
        "HEAD",
        Form.CODE,
        "free-floating queue verb chain: the CLI verb string, its handler, "
        "the queue producer and the auto-run runner — verb string and handler "
        "rowed together (the sweep-orphans lesson)",
        "_cmd_persona_instance_message",
        "_queue_free_floating_assignment",
        "_run_free_floating_assignment_once",
        "_bind_free_floating_chat_session",
    ),
    *rows(
        "s70",
        "HEAD",
        Form.ATTR,
        "assignment MINT side removed with the queue lane; the read/close "
        "side and the model fields stay for residual on-disk rows",
        "PersonaAssignmentSpec",
        "assignment_evidence_kind",
        "assignment_archive_scope",
        "assignment_signal_hash",
        "assignment_signal_hash_from_parts",
        scope=("agent_runtime.persona_assignments",),
    ),
    *rows(
        "s70",
        "HEAD",
        Form.CLASS_ATTR,
        "the store's row-mint entry points went with the spec; residual rows "
        "are settled through complete(), never re-minted",
        "persona_assignments.PersonaAssignmentStore.create",
        "persona_assignments.PersonaAssignmentStore.create_or_resume",
        scope=_AR,
    ),
    *rows(
        "s70",
        "HEAD",
        Form.CODE,
        "the queue's assignment kind and its wire vocabulary are retired: "
        "nothing may mint the kind or emit the states/kinds again "
        "(ExecutionState.QUEUED and both error kinds had ONLY free-floating "
        "emitters — the 'every owned member has a producer' gate is the rule "
        "that took them with the lane; no launcher reader existed)",
        "free_floating_message",
        "ExecutionState.QUEUED",
        "CHAT_TRANSCRIPT_PERSIST_FAILED",
        "POST_TURN_PERSIST_FAILED",
    ),
    # -- S70 (second half) — the persona-instance wire prune, contract 54 -
    # The ledger's parked batch (5), landed with the snapshot contract bump the
    # wire removal requires. Four fields were writer-less since the worker/goal
    # lanes died — `ensure_for_personas` only ever RESET them — and had no
    # consumer past a Launcher model copy, so they leave both the wire row and
    # `PersonaInstance` itself. Two more were duplicate wire ALIASES carrying
    # byte-identical values to the canonical key beside them.
    #
    # `token_budget_used` and `last_heartbeat_at` are just as writer-less and are
    # deliberately NOT rowed: both still have live readers (the Launcher's
    # token-total fallback; its roster-recency tiebreak and gateway state frame;
    # this repo's orphan heartbeat HOLD). Writer-less is not reader-less, and a
    # tombstone on a name something still reads would be a false contract.
    *rows(
        "s70",
        "HEAD",
        Form.CLASS_ATTR,
        "writer-less persona-instance record fields whose only assignment was "
        "the configured/idle RESET; dropped from the record as well as the "
        "wire, since serde._coerce builds kwargs from the dataclass fields and "
        "ignores the stale keys still on persisted rows",
        "models.PersonaInstance.context_receipt_id",
        "models.PersonaInstance.compression_receipt_id",
        "models.PersonaInstance.tool_budget_used",
        "models.PersonaInstance.watchdog_warning_count",
        scope=_AR,
    ),
    *rows(
        "s70",
        "HEAD",
        Form.CODE,
        "duplicate persona-instance wire ALIASES: each projected the exact "
        "value of the canonical key beside it (current_assignment_id / "
        "current_task_id), so no reader could tell them apart. Rowed as CODE "
        "because they are wire-key STRINGS, and scoped repo-wide so neither "
        "the snapshot row, the state_patches projection, nor the orphan "
        "classifier's alias slot can re-grow one. NOTE the ledger called "
        "attached_task_id writer-less: it was not — current_task_id is written "
        "live by the steer/goal-id lane — it was merely redundant, which is "
        "why cutting it loses no value from the frame",
        "current_work_assignment_id",
        "attached_task_id",
    ),
    # -- S71 = Plan EG's EG-0.3 Class-A reap 2 — the usage-lane fall-through
    #    (2026-08-17). Filed under the S-wave numbering because
    #    `test_every_row_carries_a_wave_a_commit_and_a_reason` makes the `sNN`
    #    spelling an invariant of this table; the stage id lives in the reason.
    # --------------------------------------------------------------------
    # `_fetch_usage_lane` ended with `return fetch_account_usage(provider_id)`
    # after its four per-provider arms. EG-0.2 §3.2 proved it unreachable —
    # `_USAGE_LANE_PROVIDERS` is the sole id producer and
    # `_detect_usage_candidates` only FILTERS it, so an unknown `--provider`
    # yields an empty candidate list that `_cmd_usage` returns on before any
    # dispatch — and it was simultaneously the ONE surviving route back into
    # `agent/account_usage.py`'s blanket `except Exception: return None`, the
    # swallow the S1 direct-dispatch work exists to route around. Dead code that
    # was also a loaded gun: adding a fifth provider to the tuple without its
    # fetcher would have silently re-entered the swallow and rendered every
    # failure as the unfalsifiable "no usage data". The arm now raises
    # `UnknownUsageLaneError`, which `_usage_failure_reason` reports by class
    # AND by id.
    #
    # Scoped to `hermes_cli`, NOT repo-wide: `fetch_account_usage` is an
    # upstream-owned public entry point with live readers in `cli.py` and
    # `gateway/slash_commands.py` (fork boundary — route around it, never delete
    # it). What is tombstoned is this fork's harness calling it. CODE rather than
    # IMPORT because the reaped import was function-local, and the IMPORT scanner
    # only reads top-level bindings.
    *rows(
        "s71",
        "HEAD",
        Form.CODE,
        "EG-0.3 Class-A reap 2 — the usage-lane fall-through into upstream's "
        "blanket swallow: nothing "
        "under hermes_cli may call fetch_account_usage again — the per-provider "
        "fetchers are dispatched directly so the failure class reaches "
        "_fetch_usage_lanes' honest per-lane handler, and an id outside "
        "_USAGE_LANE_PROVIDERS raises UnknownUsageLaneError instead of "
        "degrading into 'no usage data'",
        "fetch_account_usage",
        scope=("hermes_cli",),
    ),
    # -- S72 = dead-code audit pass 2 (2026-08-19), stage HB-1. -----------
    # `agent_runtime/risk_flags.py` was an ISLAND BEHIND A FOLDED PREDICATE.
    # Its only production reader was one arm of `serde._coerce`:
    #
    #     if annotation.__name__ == "Task" and isinstance(upgraded, dict):
    #
    # `agent_runtime` has had no `Task` dataclass since the mission-lane
    # removal — `test_models_serde` asserts `not hasattr(runtime_models,
    # "Task")` — and an AST class-definition walk over every production
    # package finds exactly ONE `class Task` in the tree, upstream's
    # `hermes_cli/kanban_db.py`, which imports nothing from `serde` and is
    # never handed to `_coerce`. The predicate could not fire, so the whole
    # module (a 40-name flag vocabulary, a prefix set, and the normalizer)
    # was reachable only from itself.
    #
    # CODE rows as well as the MODULE row because the vocabulary names are
    # STRING-shaped on the wire (`"risk_flags"` was a persisted Task key) and
    # a re-grown normalizer would most likely arrive as a private copy in
    # another module rather than as this import.
    *rows(
        "s72",
        "HEAD",
        Form.MODULE,
        "dead-code audit pass 2 HB-1 — the task risk-flag vocabulary was an "
        "island behind a folded predicate: its only reader tested "
        "`annotation.__name__ == \"Task\"` and no `Task` dataclass has "
        "existed in agent_runtime since the mission-lane removal",
        "agent_runtime.risk_flags",
    ),
    *rows(
        "s72",
        "HEAD",
        Form.CODE,
        "dead-code audit pass 2 HB-1 — the task risk-flag normalizer and its "
        "two vocabularies went with the module; nothing in production may "
        "re-grow the migration lane without a Task dataclass to migrate",
        "normalize_task_risk_flags",
        "is_known_risk_flag",
        "KNOWN_RISK_FLAGS",
        "PARAMETERIZED_RISK_FLAG_PREFIXES",
    ),
    # -- S72 stage HA-3 — write-only fields on live dataclasses. -----------
    # Each was declared, persisted and read by NOTHING: an AST `Name(Load)` /
    # attribute walk over all thirteen production packages found the
    # declaration and nothing else, the stream goldens carry none of them, and
    # the launcher reads none. `child_events_offset` is the fifth field of the
    # block S70 pruned four fields from — same block, same argument, missed.
    #
    # CLASS_ATTR and not CODE: the risk being fenced is the FIELD coming back
    # on the dataclass, and `serde._coerce` builds kwargs from `fields()` and
    # ignores unknown keys, so a persisted row that still carries one of these
    # loads unchanged — which is why no contract bump was needed and why a
    # producer-frame pin would have nothing to assert.
    *rows(
        "s72",
        "HEAD",
        Form.CLASS_ATTR,
        "dead-code audit pass 2 HA-3 — declared-and-never-read dataclass "
        "fields; none reached a wire, a golden or a launcher reader, and a "
        "field nothing loads is a promise the frame does not keep",
        "models.PersonaInstance.child_events_offset",
        "models.GoalRuntimeInstance.started_by",
        "models.GoalRuntimeInstance.lease_expires_at",
        "models.AgentRun.iterations_used",
        scope=_AR,
    ),
    *rows(
        "s72",
        "HEAD",
        Form.CLASS_ATTR,
        "dead-code audit pass 2 HA-3 — a SECOND authority for a number "
        "`ProjectionAccountant.summary()` already carries as `reasons` + "
        "`by_design`. Only tests ever asked it, and they now do the "
        "subtraction a real reader has to do",
        "parity.ProjectionAccountant.dropped_by_design",
        scope=_AR,
    ),
    # `realm_sync._board_artifacts` / `._office_artifacts` were the pre-ML-8
    # publish walks. Their callers moved to `_board_publish_scan` /
    # `_office_publish_scan` — the single-walk chokepoints that exist BECAUSE
    # two independent walks of the same directories decided "which offices
    # travel" and "which persona definitions are pinned" and disagreed, so an
    # undecodable actor file travelled and every peer archived that desk
    # (MISSION_CONTROL_LEDGER_REFACTOR_PLAN §3, marked FIXED). Rowed even
    # though they are private, because resurrection would restore exactly the
    # second walk the fix removed — this is the "resurrection could bypass a
    # public tombstone" case the registry reserves rows for.
    *rows(
        "s72",
        "HEAD",
        Form.CODE,
        "dead-code audit pass 2 HA-3 — the pre-ML-8 second publish walk; the "
        "typed single-scan chokepoints replaced them and a re-grown copy "
        "would reinstate the two-walks-disagree defect",
        "_board_artifacts",
        "_office_artifacts",
        scope=("agent_runtime.realm_sync",),
    ),
    # -- S72 stage HB-2 — vocabularies that enforced nothing. -------------
    # Four roll-up constants (a frozenset / a tuple over constants that are
    # each ALREADY used at their own site) with zero executable readers between
    # them. A set that names a vocabulary but is consulted by no branch does
    # not make the vocabulary closed — it makes a second place to forget.
    # `PLAN_ACTIONS` proved the point: it listed a FIFTH action,
    # `refuse_ambiguous_source`, that nothing in either repo had ever
    # constructed (an AST walk finds `PromotionPlan(...)` only inside
    # `skill_promotion.py`, and every site spells `refuse_invalid`), so the
    # constant claiming to pin the vocabulary was itself the only thing
    # asserting a value outside it.
    *rows(
        "s72",
        "HEAD",
        Form.CODE,
        "dead-code audit pass 2 HB-2 — roll-up vocabularies with no reader; "
        "the authority for each vocabulary is its production construction "
        "sites, which are the things a branch can actually disagree with",
        "PLAN_ACTIONS",
        "PROMOTION_BLOCK_REASONS",
        "WIRE_USER_MESSAGE_UNAVAILABLE_REASONS",
        "DISPATCH_SESSION_REASONS",
        "refuse_ambiguous_source",
    ),
    # -- S72 stage HB-3 — one spelling per question. ----------------------
    # `personas.MOTHBALLED_ROLES` was a THIRD spelling of a fact
    # `persona_lifecycle.MOTHBALLED_ROLE_TOKENS` already owns and
    # `is_runtime_persona` already reads — while `personas.py` imported the two
    # live sets and used neither (the repo's single genuinely unused import).
    # `chat_live_log_root` was a wrapper around `capture_chat_live_log_root`
    # that added a lock-read fast path and nothing else.
    # `McpAdmissionOutcome.registered_tool_names` was write-only: constructed
    # from `box["tools"]`, carried on a frozen dataclass, read by no one.
    # `AgentCreateOutcome.ok` was asked only by tests; production spells the
    # same question `outcome.refusal is not None`, and two spellings of one
    # predicate is how they drift.
    # NOTE the SHAPE, learned the hard way on 2026-08-19: an ATTR row's text
    # must be a BARE attribute name and its scope the owning module. Written as
    # `"personas.MOTHBALLED_ROLES"` under `scope=_AR` the assertion becomes
    # `hasattr(agent_runtime, "personas.MOTHBALLED_ROLES")`, which is False for
    # every tree that ever existed — a row that passes because it asks an
    # impossible question. It survived a full registry run green and was caught
    # only by re-adding the symbol and watching the row NOT go red. CLASS_ATTR
    # is the form that takes the dotted `module.Class.attr` spelling; ATTR is
    # not.
    *rows(
        "s72",
        "HEAD",
        Form.ATTR,
        "dead-code audit pass 2 HB-3 — a THIRD spelling of the retired-role "
        "fact `persona_lifecycle.MOTHBALLED_ROLE_TOKENS` already owns and "
        "`is_runtime_persona` already reads",
        "MOTHBALLED_ROLES",
        scope=("agent_runtime.personas",),
    ),
    *rows(
        "s72",
        "HEAD",
        Form.ATTR,
        "dead-code audit pass 2 HB-3 — a wrapper around "
        "`capture_chat_live_log_root` that added a lock-read fast path and no "
        "answer of its own; one capture authority per mirror root",
        "chat_live_log_root",
        scope=("agent_runtime.chat_live_log",),
    ),
    *rows(
        "s72",
        "HEAD",
        Form.CLASS_ATTR,
        "dead-code audit pass 2 HB-3 — a write-only outcome field and a "
        "test-only predicate with a production twin",
        "mcp_admission.McpAdmissionOutcome.registered_tool_names",
        "agent_create.AgentCreateOutcome.ok",
        scope=_AR,
    ),
    # -- S72 stage HB-4 — orphans from ML-8's chokepoint. -----------------
    # `_list_active_cards` / `_list_archived_cards` lost their callers when
    # `_ordering_cards` became THE read every order-key decision goes through.
    # Rowed despite being private because their resurrection is precisely the
    # defect `CardsUnreadable` exists to prevent: they return `.cards` from a
    # scan and DROP the scan's unreadable count, so an allocator computing
    # order keys from one of them cannot see the card it must not overwrite.
    *rows(
        "s72",
        "HEAD",
        Form.CODE,
        "dead-code audit pass 2 HB-4 — the pre-ML-8 card reads that discard "
        "the unreadable count; `_ordering_cards` is the one read that refuses, "
        "and a re-grown silent lister re-opens the order-key corruption "
        "CardsUnreadable was raised to stop",
        "_list_active_cards",
        "_list_archived_cards",
        scope=("agent_runtime.board_store",),
    ),
    *rows(
        "s72",
        "HEAD",
        Form.PATH,
        "dead-code audit pass 2 HB-4 — a line-count exception for a file that "
        "has been under the bar for a wave: it cited 3,170 lines against a "
        "snapshot.py of 2,621, and of the four seams it named as the split "
        "plan only `_parity_envelope` still exists",
        "agent_runtime/docs/snapshot_line_count_exception.md",
    ),
    # -- S72 stages H-CLI-2 / H-P1 / H-P2 ---------------------------------
    # `certification_ladder.py` drove `harness burn-in`, a verb S6 retired
    # along with `scripts/cert_streak.py` — its twin, deleted then, pinned by
    # `tests/scripts/test_cert_streak.py` ever since. That pin named ONE
    # module, so the surviving driver was invisible to it for two months. The
    # pin is now parameterized over BOTH names (a family, not a name), and this
    # PATH row is the second half.
    *rows(
        "s72",
        "HEAD",
        Form.PATH,
        "dead-code audit pass 2 H-P1 — the second driver of the retired "
        "`harness burn-in` verb; its twin went at S6 and the single-name pin "
        "guarding that removal could not see this one",
        "scripts/certification_ladder.py",
    ),
    *rows(
        "s72",
        "HEAD",
        Form.CODE,
        "dead-code audit pass 2 H-P1 — two never-adopted exports on the "
        "dispatch lane. `shutdown_dispatch_executor`'s own docstring said "
        "'tests; never on a live lane' and no test called it; "
        "`is_supervised_here` was born in the same commit as the live "
        "`supervised_dispatch_ids` and never adopted, so the orphan sweep has "
        "one way to ask its question, not two",
        "shutdown_dispatch_executor",
        "is_supervised_here",
    ),
    # `--all-persona-profiles` (a self-described "compatibility flag" whose
    # dest no handler read) and the `cursor` / `since` control branches (no
    # call site named either token, across all 58 `_add_stage42_global_args`
    # sites, ever). Rowed as CODE because both are argparse STRINGS, which is
    # the form `ast.unparse` preserves and an identifier scan would miss.
    *rows(
        "s72",
        "HEAD",
        Form.CODE,
        "dead-code audit pass 2 H-CLI-2 — advertised and unreachable: a flag "
        "with no reader and two control tokens no verb could ask for. The "
        "reachability gate at tests/hermes_cli/"
        "test_harness_flag_and_control_reachability.py is what keeps the "
        "class retired; these rows keep the exact spellings out",
        "--all-persona-profiles",
        "all_persona_profiles",
        scope=("hermes_cli",),
    ),
    # -- S72 stage H-CLI-1b — exceptions that existed to be mapped. -------
    # An AST `Raise` walk over all thirteen production packages AND `tests/`
    # finds ZERO raise sites for any of these four. Each had exactly three
    # references: its own `class` statement, one import in `harness_support`,
    # and one row in `_error_code_for_exception`'s type->code tuple. They were
    # alive solely to be translated by the thing that translates them.
    #
    # Their exit codes did NOT all go with them, and the split matters:
    # `invalid_transition` and `stale_run` had no reader anywhere in either
    # repo and went in H-CLI-1a; `proof_missing` and `wrong_runtime_root` STAY
    # in ERROR_EXIT_CODES because the launcher spells those same words on a
    # DIFFERENT lane (a snapshot-health value, a proof-gate state), and both
    # remain spendable through the ValueError arm. Two lanes, one word — the
    # reason a token was never the unit of meaning here.
    #
    # NOT touched, deliberately: `WorkspaceUnresolved`, added by MC-8/P10 in
    # this same file, which HAS a real raise site (`office_store.py:442`).
    # Conflating it with these four is the exact mistake this row exists to
    # forestall.
    *rows(
        "s72",
        "HEAD",
        Form.ATTR,
        "dead-code audit pass 2 H-CLI-1b — never-raised exception classes "
        "kept alive by the one tuple that mapped them; an exception nothing "
        "throws is not a defensive mapping, it is a false claim about which "
        "states this runtime can reach",
        "InvalidTransition",
        "StaleRun",
        "ProofMissing",
        "RuntimeRootMismatch",
        scope=("agent_runtime.errors",),
    ),
    # -- S72 stage H-CLI-3 — two uninvoked verbs. -------------------------
    # `persona instance resolve-chat-turn` was a second parser binding the SAME
    # handler as `mission-chat turn-resolve`, with the same four required flags
    # and the instance id positional instead of a flag. The launcher has always
    # emitted `turn-resolve`; the alias could only be reached by a human who
    # read the wrong doc line.
    #
    # `workspace actors` listed persona-instance summaries filtered by
    # workspace — the same store, through the same `persona_instance_summary`,
    # that `persona list` already reads. Cut on the principle that decided this
    # whole stage: a verb whose data has ANOTHER DOOR goes; a verb that IS the
    # only door stays. `contracts dump` was proposed alongside these two and
    # was REFUSED on exactly that test — it is the only reader of
    # `contract_manifest()`, which the audit's own ruling wanted kept.
    #
    # The launcher's committed `hermes_cli_contract.json` still carries both
    # paths. Its gate asserts launcher-emitted argv EXISTS in the dump and not
    # the reverse, so it stays green — but the fixture is now stale in the
    # extra direction too, and the refresh is a LAUNCHER commit this repo
    # cannot make.
    *rows(
        "s72",
        "HEAD",
        Form.CODE,
        "dead-code audit pass 2 H-CLI-3 — a duplicate-authority alias for "
        "`mission-chat turn-resolve` and a convenience lister whose data "
        "`persona list` already serves; neither had an invocation in either "
        "repo",
        "resolve-chat-turn",
        "_cmd_workspace_actors",
        scope=("hermes_cli",),
    ),
    # -- S72 stages HA-5 / HB-4-EXCL — three lossy or callerless reads. ----
    # `IncidentStore.list_open_with_closed_count` was a SNAPSHOT-LANE
    # optimisation (open rows plus a count of the closed tail, so a runtime
    # with thousands of closed incident files did not coerce them all per
    # frame). Its caller went with the incident observability S9 removed;
    # `status.build_status` — the function that still reads an incident store —
    # calls `list_all()`. What was left was a perf argument for a cost nobody
    # pays and three asserts that were its only exercise.
    #
    # `PersonaChatClarifyTicketStore.list_tickets` returned `scan_tickets()[0]`
    # and DROPPED the unreadable count. `scan_tickets`' own docstring says why
    # that count has to travel: the adoption metric is a RATIO over the whole
    # store, and "an adoption ratio that moved because the operator asked to
    # see fewer rows would be a lying metric". A thin sibling that silently
    # moves the denominator is the same defect as HB-4's `_list_active_cards`,
    # on a different store.
    #
    # `persona_assignments.safe_assignment_state` had no caller at all.
    *rows(
        "s72",
        "HEAD",
        Form.CLASS_ATTR,
        "dead-code audit pass 2 HA-5 / HB-4 — a snapshot-lane optimisation "
        "whose caller went with S9's incident observability, and a thin read "
        "that dropped the unreadable count its own sibling exists to carry",
        "store.IncidentStore.list_open_with_closed_count",
        "persona_chat_continuity.PersonaChatClarifyTicketStore.list_tickets",
        scope=_AR,
    ),
    *rows(
        "s72",
        "HEAD",
        Form.ATTR,
        "dead-code audit pass 2 HB-4 — callerless assignment-state coercion",
        "safe_assignment_state",
        scope=("agent_runtime.persona_assignments",),
    ),
    # -- S73 = duplicate-implementation retirement, Stage 4 (2026-08-22). --
    # `PersonaInstanceStore.create_free_floating` minted `mode="free_floating"`
    # instance rows for the free-floating queue verb chain S70 removed. What was
    # left was a production method whose ONLY callers were eight test files
    # needing a pair of cheap instance rows to point flow-graph edges and patch
    # batches at — the closed loop this registry's scope tuple excludes `tests`
    # to expose. The mint moved verbatim to
    # `tests/agent_runtime/persona_instance_mint.py::mint_free_floating`, so
    # nothing was lost from the suites and nothing survives in production.
    #
    # CLASS_ATTR, not ATTR: the row's subject is a METHOD on a live class, and
    # this file's own HB-3 note records what an ATTR row does with a dotted
    # spelling (asks `hasattr(module, "Class.attr")`, which is False for every
    # tree that ever existed — a row that passes vacuously).
    #
    # `mode="free_floating"` itself is NOT tombstoned and must not be: it is
    # still read by `persona_assignments._CHAT_MODES`, `operator_channels`,
    # `persona_chat_history` and `persona_instance_identity`, and rows carrying
    # it exist on disk. What is retired is the production MINT, not the mode.
    *rows(
        "s73",
        "HEAD",
        Form.CLASS_ATTR,
        "duplicate-implementation retirement Stage 4 — the free-floating "
        "instance mint lost its last production caller with S70's queue verb "
        "chain and survived only as a test fixture; the mint now lives in "
        "tests/agent_runtime/persona_instance_mint.py and production may not "
        "re-grow a callerless second door onto persona-instance creation",
        "persona_assignments.PersonaInstanceStore.create_free_floating",
        scope=_AR,
    ),
    *rows(
        "s73",
        "HEAD",
        Form.ATTR,
        "duplicate-implementation retirement Stage 4 — the mint's only helper, "
        "orphaned by the same cut; rowed because its resurrection would be the "
        "first half of re-growing the mint",
        "_free_floating_identity",
        scope=("agent_runtime.persona_assignments",),
    ),
    # -- S74 — the read_model.db lane, retired whole ------------------------
    #
    # These two MODULE rows also DISCHARGE the S46 rows above. ``ProjectorResult``
    # / ``LEASE_TTL_SECONDS`` are ATTR rows scoped to ``agent_runtime.projector``
    # and the three ``Projector.*`` methods are CLASS_ATTR rows resolving through
    # the same module; with the module deleted, ``_module_row_covers`` is what
    # keeps them from becoming permanent passes. That is the S66 meta-invariant
    # working as designed — a later wave taking the whole owner is the one
    # legitimate reason a member row stops resolving, and it must be DECLARED.
    *rows(
        "s74",
        "HEAD",
        Form.MODULE,
        "duplicate-implementation retirement Stage 6 — the read_model.db lane "
        "was built, configured on, and served no one: write_snapshot's gated "
        "apply_full_rebuild and Projector.full_rebuild were TWO production "
        "writers of one database with zero production readers, both reachable "
        "only from hand-run CLI verbs, into a resolver that built the full core "
        "before consulting the cache. The serve path persists cores through "
        "core_cache into serve_read_model/ instead, and the launcher's "
        "snapshot.json cold-paint consumer was retired at MC-7/P11. Operator "
        "ruling: RETIRE (outcome 2 of the read-model-db-serve-population "
        "ruling). Production may not re-grow a second cache of the snapshot "
        "core with a second validity authority beside the fingerprint",
        "agent_runtime.read_model",
        "agent_runtime.projector",
    ),
    *rows(
        "s74",
        "HEAD",
        Form.ATTR,
        "duplicate-implementation retirement Stage 6 — the snapshot.json boot "
        "cache writer, orphaned when its only production caller "
        "(read_model.resolve_snapshot_frame) went with the lane and its only "
        "consumer (the launcher's cold-paint reader) had already gone at "
        "MC-7/P11. Its temp-file sweeper went with it: the sweep ran at a "
        "boot-cache write, and nothing stages a .snapshot_*.tmp any more. "
        "paths.snapshot_path() deliberately SURVIVES as the one authority for "
        "where a legacy copy lives",
        "write_snapshot",
        "_sweep_stale_snapshot_tmp_files",
        "_STALE_SNAPSHOT_TMP_AGE_SECONDS",
        scope=("agent_runtime.snapshot",),
    ),
    *rows(
        # ``s75`` and not ``hh12``: every wave in this table is ``sNN`` and the
        # integrity test asserts it. The plan stage this cut belongs to (H-H12)
        # is named in the reason, which is where a row's provenance lives.
        "s75",
        "HEAD",
        Form.ATTR,
        "the desk-litter classifier's id-SPELLING reader, replaced by the "
        "store's own record of what an item was minted as "
        "(OfficeItem.minted_kind). It parsed three launcher minting "
        "conventions that nothing enforces, so a launcher rename would have "
        "silently reclassified every mis-kinded agent as a widowed desk — a "
        "POSITIVE claim resting on a spelling, which this repo's gate rule "
        "forbids. Production may not re-grow a reader that decides an item's "
        "kind from its id",
        "_office_item_id_shape",
        "ITEM_ID_SHAPE_AGENT",
        "ITEM_ID_SHAPE_DESK",
        "ITEM_ID_SHAPE_UNKNOWN",
        scope=("agent_runtime.harness_doctor",),
    ),
    *rows(
        # ``s76`` and not ``ax2``: the sNN spelling is the table's, and the plan
        # stage (§AX AX2 of the realm-sync × actor-lifecycle wave) lives in the
        # reason, where a row's provenance belongs.
        "s76",
        "HEAD",
        Form.CLASS_ATTR,
        "the retire's two ASSIGNMENT guards and the scan that fed them. "
        "assignment_active fenced 'this retire would orphan a live "
        "assignment'; S70 deleted the store's mint side and the 2026-07-30 "
        "chat-only purge deleted the lane that consumed assignments, so no row "
        "it could orphan can be minted and no surviving row is bound to "
        "anything that runs — while the guard made a placement undeletable on "
        "any store still carrying legacy residue. assignments_unknowable was "
        "never a fact about the retire at all: it existed only to keep the "
        "first guard's NEGATIVE honest, so it left with what it was protecting "
        "rather than surviving as a fence over nothing. "
        "PersonaAssignmentStore.scan_all deliberately SURVIVES — the operator's "
        "settle verbs still read it — but no WRITE decides on its count any "
        "more",
        "persona_assignments.PersonaInstanceStore._scan_active_assignments_for_instance",
        scope=_AR,
    ),
    *rows(
        "s76",
        "HEAD",
        Form.ATTR,
        "the retire guard's answer type, whose whole purpose was carrying "
        "'could not look' beside 'looked and found none' for a guard that no "
        "longer exists",
        "ActiveAssignmentScan",
        scope=("agent_runtime.persona_assignments",),
    ),
    *rows(
        "s76",
        "HEAD",
        Form.CODE,
        "the two retire refusal REASONS, retired as wire vocabulary and not "
        "only as code: the launcher decodes data.reason before the numeric "
        "code, so re-minting either spelling would put a refusal back on a lane "
        "whose decode leaves with this landing (launcher 6bf48ba26 kept "
        "MissionAgentRetireReason.assignmentActive / .assignmentsUnknowable "
        "expressly until these guards went). One measurement worth carrying: "
        "the launcher's note that assignments_unknowable 'does not even need a "
        "legacy row' is not quite right — scan_all only counts rows it opened, "
        "so both arms needed residue on disk, one active and one corrupt",
        "assignment_active",
        "assignments_unknowable",
        scope=_AR,
    ),
)


#: `root_node_mode` is the 29th S57 scalar and is deliberately NOT a CODE row.
#: Sixty other hits in the tree are a ContextVar + kwarg of the SAME NAME
#: (skill_utils, prompt_builder, skills_tool) that never read the config field.
#: Banning the name repo-wide would fail on live code. The FIELD absence is
#: pinned in test_s57_unruled_config_debt_removal, where it belongs.
S57_FIELD_ONLY = ("root_node_mode",)


# =========================================================================
# THE SCANNER — one implementation, every CODE row
# =========================================================================


def _code_only(source: str) -> str:
    """Source with comments and docstrings removed, everything else intact.

    ``ast.parse`` drops comments outright; docstrings are stripped explicitly on
    the tree; ``ast.unparse`` re-renders canonical code. STRING LITERALS SURVIVE
    — deliberately, because event kinds and capability ids are invoked by name.

    S48's mechanism, NOT S46's. S46 joined surviving tokens with newlines, which
    turns ``card.title`` into ``card\\n.\\ntitle`` so a dotted assertion can never
    match and passes vacuously. This renders real syntax.
    """

    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def _production_files(packages: tuple[str, ...]) -> list[Path]:
    """Every production ``.py`` in scope.

    ``"."`` means the repo-root modules and is walked NON-recursively on
    purpose: recursing from the root would pull in ``tests/``, ``node_modules``,
    the venvs and the whole docs tree, and a gate whose scope is "everything"
    is a gate that will be narrowed by the first false positive.

    A scope token resolves to a PACKAGE DIRECTORY **or to a single MODULE
    FILE**, and that second arm is the fix for MCF-53's sweep. This walked
    ``HERMES_ROOT / package.replace(".", "/")`` and required a *directory*, so
    every scope naming a module — ``agent_runtime.status``,
    ``agent_runtime.observability``, ``agent_runtime.stream`` and eight more —
    resolved to a path that does not exist, contributed ZERO files, and left
    **32 of 155 CODE rows scanning nothing at all**. Each of those rows has been
    green since it was written for the same reason an empty ``for`` loop is:
    there was nothing to look at. Measured, not argued — see
    ``test_every_code_row_scope_resolves_to_something``.

    Resolution FAILURE is not handled here, because a silent skip is exactly
    what this function did wrong. :func:`_unresolvable_scope_tokens` reports it
    and the gate above decides, under the same S66 covered-or-fail rule the ATTR
    and CLASS_ATTR arms already use.
    """

    files: list[Path] = []
    for package in packages:
        if package == ".":
            files.extend(sorted(HERMES_ROOT.glob("*.py")))
            continue
        relative = package.replace(".", "/")
        root = HERMES_ROOT / relative
        if root.is_dir():
            for path in sorted(root.rglob("*.py")):
                if any(part in _SKIP_DIRS for part in path.parts):
                    continue
                files.append(path)
            continue
        module = HERMES_ROOT / f"{relative}.py"
        if module.is_file():
            files.append(module)
    return files


def _unresolvable_scope_tokens(packages: tuple[str, ...]) -> list[str]:
    """The scope tokens that name neither a package directory nor a module file.

    Separated from :func:`_production_files` on purpose: a scan must not decide
    what an unresolvable scope MEANS. It can mean the module was legitimately
    retired by a later wave (a stronger absence) or that the scope is a typo, and
    only the registry knows which — so the question is answered where the rest of
    the covered-or-fail rule lives.
    """

    missing: list[str] = []
    for package in packages:
        if package == ".":
            continue
        relative = package.replace(".", "/")
        if (HERMES_ROOT / relative).is_dir():
            continue
        if (HERMES_ROOT / f"{relative}.py").is_file():
            continue
        missing.append(package)
    return missing


#: This file, resolved ONCE. It used to be re-resolved inside the per-file loop
#: in :func:`code_offenders` — a filesystem syscall per file per row, 934 files
#: x 156 scans. Measured at 1.04s of pure ``resolve()`` per scan, which was the
#: dominant cost of every row test, for an answer that cannot change.
_THIS_FILE = Path(__file__).resolve()


@functools.lru_cache(maxsize=None)
def _scan_paths(packages: tuple[str, ...]) -> tuple[Path, ...]:
    """The files a scan over ``packages`` visits, with this file excluded.

    The same set :func:`code_offenders` used to recompute inline on every call.
    Both the glob and the self-exclusion are pure functions of ``packages``, and
    ``packages`` is one of a handful of scope tuples shared by 154 rows.
    """

    return tuple(path for path in _production_files(packages) if path.resolve() != _THIS_FILE)


_CODE_CACHE: dict[Path, str] = {}


def _rendered(path: Path) -> str:
    cached = _CODE_CACHE.get(path)
    if cached is not None:
        return cached
    try:
        rendered = _code_only(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, RecursionError):
        # A file the fork's Python cannot parse is reported, never silently
        # skipped — a silent skip is how a scan scope quietly shrinks.
        rendered = f"<<UNPARSEABLE {path}>>"
    _CODE_CACHE[path] = rendered
    return rendered


def code_offenders(row: Tombstone) -> list[str]:
    """Every production file whose comment-stripped code still names the row."""

    offenders: list[str] = []
    for path in _scan_paths(row.scope):
        if row.text in _rendered(path):
            offenders.append(str(path.relative_to(HERMES_ROOT)).replace("\\", "/"))
    return offenders


def _resolve(dotted: str):
    module = importlib.import_module(dotted)
    return module


# --------------------------------------------------------------------------- #
# The render is paid HERE, at import
# --------------------------------------------------------------------------- #
# Rendering the production tree — ``ast.parse`` 27.6s plus ``ast.unparse`` 12.4s
# over 934 files — is a per-SESSION cost that ``_CODE_CACHE`` already shares
# between every scan in this file. What it did NOT have was an owner: the first
# test to reach a full-scope row paid all of it inside its own item, and which
# test that is, is collection order's choice rather than any test's.
#
# `--timeout=30` is a PER-TEST budget, so that test failed for a reason having
# nothing to do with what it asserts — and because ``addopts`` sets
# `--timeout-method=thread`, the timeout KILLS the process rather than failing
# the item, taking every test after this file with it. Measured before this
# change: `pytest tests/agent_runtime/test_tombstone_registry.py -q` exits 1 with
# `+++ Timeout +++` inside `test_the_scanner_is_not_vacuous`, and no summary line
# is ever printed.
#
# Warming here puts the cost in collection, which pytest-timeout does not clock
# (verified directly: a module that sleeps 35s at import passes under
# `--timeout=30`). Nothing is excluded and no marker is involved — the same fix
# and the same reasoning as
# ``tests/agent_runtime/test_s55_registered_events_have_emitters.py``.
#
# NOT marked `integration`: ``addopts`` filters `-m 'not integration'` in every
# pytest invocation, including each per-file subprocess
# ``scripts/run_tests_parallel.py`` spawns, and there is no integration lane —
# ``.github/workflows/tests.yml`` runs ``scripts/run_tests.sh`` and the marker
# appears nowhere in ``.github/workflows/``. The marker would delete this
# registry from every run on every machine, which is worse than a slow gate.
#
# The unparse is NOT dropped, though it is the obvious thing to cut. It is load
# bearing twice over: ``test_no_production_file_failed_to_parse`` treats a file
# the scanner cannot RENDER as a hole rather than a pass, and the docstring on
# ``_code_only`` records that S46's cheaper token-join turned ``card.title`` into
# three tokens so no dotted assertion could ever match — a gate that passed
# vacuously. Making the comparison cheaper means changing what is compared, and
# the cheaper thing was already tried and already failed.
for _warm_path in _scan_paths(PRODUCTION_PACKAGES):
    _rendered(_warm_path)

#: Render-cache occupancy AS OF IMPORT. Snapshotted so the guard below fails
#: deterministically when the warm is deleted, instead of depending on which
#: test happens to run first.
_CODE_CACHE_SIZE_AT_IMPORT = len(_CODE_CACHE)


def test_the_shared_render_is_paid_at_import_not_by_whichever_test_runs_first():
    """REGRESSION GUARD for the timeout, pinning the cause and not a clock.

    A wall-clock assertion would be a flake generator on a box that measured this
    same walk between 40s and 82s depending on load, and it would be asserting
    the symptom. What must be true is that the render ran during collection and
    covered the whole scan set.
    """

    assert _CODE_CACHE_SIZE_AT_IMPORT > 900, (
        "the production tree was NOT rendered at module import (cache size at "
        f"import: {_CODE_CACHE_SIZE_AT_IMPORT}). Whichever test reaches a "
        "full-scope row first now pays ~40s of parse+unparse inside its own "
        "item, against the 30s per-test cap in pyproject.toml — and "
        "--timeout-method=thread kills the process, so nothing after this file "
        "would run at all. Restore the module-scope warm."
    )
    assert _CODE_CACHE_SIZE_AT_IMPORT == len(_scan_paths(PRODUCTION_PACKAGES)), (
        "the import warm covered "
        f"{_CODE_CACHE_SIZE_AT_IMPORT} files but the full scan visits "
        f"{len(_scan_paths(PRODUCTION_PACKAGES))}. The uncovered remainder is "
        "billed to a test."
    )


def test_the_scan_path_cache_is_keyed_so_repeat_scans_are_free():
    """The 154 row tests share a handful of scope tuples; each must glob once.

    A cache key that varied per call — a list instead of a tuple, an unhashable
    row, a key including something mutable — would restore the per-row filesystem
    walk while every assertion in this file stayed green. So the hit is measured
    rather than assumed.
    """

    before = _scan_paths.cache_info()
    for _ in range(20):
        _scan_paths(PRODUCTION_PACKAGES)
    after = _scan_paths.cache_info()

    assert after.misses == before.misses, (
        f"repeat scans re-globbed the tree ({before} -> {after}). The scope "
        "tuple is supposed to be the cache key; something in it is no longer "
        "hashable-stable, and every row test is paying for a filesystem walk."
    )
    assert after.hits == before.hits + 20, f"{before} -> {after}"

    # The check above only proves a REPEATED key hits, which is not the
    # regression worth guarding. The likely one is a caller mixing something
    # per-row into the key — the row's own text, say — so 154 rows produce 154
    # keys and every one of them re-globs the tree.
    #
    # Caught by mutation, and the FIRST attempt at this assertion was wrong in an
    # instructive way: driving every row twice and requiring the second pass to
    # add no misses stays GREEN under exactly that change, because `maxsize=None`
    # keeps all 154 bad entries and the second pass hits every one. Warming a
    # cache and then measuring hits proves the cache is a cache; it says nothing
    # about whether the key is right.
    #
    # What actually has to hold is a bound on the number of ENTRIES: the key is
    # the scope, so the cache can never hold more entries than there are distinct
    # scopes, however many rows are driven through it.
    for row in CODE_ROWS:
        code_offenders(row)

    distinct_scopes = {row.scope for row in TOMBSTONES} | {PRODUCTION_PACKAGES}
    info = _scan_paths.cache_info()
    assert info.currsize <= len(distinct_scopes), (
        f"the scan cache holds {info.currsize} entries after driving "
        f"{len(CODE_ROWS)} rows, but there are only {len(distinct_scopes)} "
        "distinct scopes. The key has picked up something per-row, so every row "
        "re-globs the tree instead of sharing one walk per scope."
    )


def test_the_render_cache_absorbs_every_repeat_read(monkeypatch):
    """The render cache is driven, not inspected.

    A size assertion alone would survive a ``_rendered`` that consulted the cache
    and then re-rendered anyway. This makes re-rendering IMPOSSIBLE to do
    silently: ``_code_only`` is replaced with a landmine and every path in the
    full scan set is read back. ``_rendered`` catches SyntaxError, ValueError and
    RecursionError, so the landmine raises something it does not catch.
    """

    def _landmine(source: str) -> str:
        raise AssertionError(
            "_code_only ran again for a path already in _CODE_CACHE — the render "
            "cache is not absorbing repeat reads, so every scan re-parses the tree"
        )

    monkeypatch.setattr(sys.modules[__name__], "_code_only", _landmine)
    for path in _scan_paths(PRODUCTION_PACKAGES):
        _rendered(path)


# =========================================================================


CODE_ROWS = tuple(row for row in TOMBSTONES if row.form is Form.CODE)
MODULE_ROWS = tuple(row for row in TOMBSTONES if row.form is Form.MODULE)
ATTR_ROWS = tuple(row for row in TOMBSTONES if row.form is Form.ATTR)
CLASS_ATTR_ROWS = tuple(row for row in TOMBSTONES if row.form is Form.CLASS_ATTR)
EVENT_ROWS = tuple(row for row in TOMBSTONES if row.form is Form.EVENT)
PATH_ROWS = tuple(row for row in TOMBSTONES if row.form is Form.PATH)
IMPORT_ROWS = tuple(row for row in TOMBSTONES if row.form is Form.IMPORT)


def _top_level_import_bindings(source: str) -> set[str]:
    tree = ast.parse(source)
    bindings: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        bindings.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    return bindings


# -------------------------------------------------------------------------
# The registry's own integrity
# -------------------------------------------------------------------------


def test_every_row_carries_a_wave_a_commit_and_a_reason():
    for row in TOMBSTONES:
        assert row.text, row
        assert row.wave.startswith("s"), row
        assert row.commit, row.label
        assert row.reason.strip(), row.label
        assert row.scope, row.label


def test_no_row_is_registered_twice_with_the_same_form_and_scope():
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    duplicates: list[str] = []
    for row in TOMBSTONES:
        key = (row.form.value, row.text, row.scope)
        if key in seen:
            duplicates.append(row.label)
        seen.add(key)
    assert duplicates == []


def test_the_scanner_is_comment_and_docstring_immune():
    """The defect this registry exists to retire, proven mechanically.

    Every construct below NAMES a tombstoned symbol and none may survive the
    render: module docstring, function docstring, line comment, trailing
    comment. This is the assertion that would have caught S57's three red rows
    and S46's vacuous dotted gate.
    """

    rendered = _code_only(
        '"""Module doc naming RepoBundleStore and worker_session.closed."""\n'
        "def f(card):\n"
        '    """Doc naming apply_pending and ProjectorResult."""\n'
        "    # comment naming clean_launcher_visual_processes\n"
        "    return card.title  # trailing comment naming LEASE_TTL_SECONDS\n"
    )
    for ghost in (
        "RepoBundleStore",
        "worker_session.closed",
        "apply_pending",
        "ProjectorResult",
        "clean_launcher_visual_processes",
        "LEASE_TTL_SECONDS",
        "Module doc",
        "trailing comment",
    ):
        assert ghost not in rendered, ghost
    # …and the real code, including the DOTTED form S46's helper destroyed.
    assert "card.title" in rendered


def test_the_scanner_keeps_string_literals():
    """Event kinds and capability ids are invoked BY NAME — the S44/S55 lesson.

    A scanner that dropped literals would report every de-registered event type
    as absent while the emitter sat there spelling it out.
    """

    rendered = _code_only('def f():\n    return Event(type="repo_bundle.created")\n')
    assert "repo_bundle.created" in rendered


def test_the_import_binding_scanner_is_ast_based():
    source = '"""build_snapshot in a docstring"""\n# build_snapshot in a comment\nfrom x import live\n'
    assert _top_level_import_bindings(source) == {"live"}


def test_the_scanner_is_not_vacuous():
    """A LIVE neighbour must still be found, in every scoped package.

    Without this every CODE assertion below could pass because the walk found
    no files at all.
    """

    files = _production_files(PRODUCTION_PACKAGES)
    assert len(files) > 300, len(files)
    live = Tombstone(
        "validate_event_payload",
        Form.CODE,
        "canary",
        "n/a",
        "LIVE — EventLog.append calls the event-only registry validator on every append",
    )
    assert code_offenders(live), "the scanner found no live reference"
    # The S57 survivor that proves the gate's SHAPE: read only through the
    # getattr STRING form, which a prefix trim or an eyeball pass takes.
    survivor = Tombstone(
        "lock_acquire_timeout_seconds",
        Form.CODE,
        "canary",
        "n/a",
        "LIVE via getattr(load_root_runtime_config(), ...) at locks.py:133",
    )
    assert code_offenders(survivor)

    # MODULE-scoped rows, pinned separately because they were the hole. 32 of
    # the 155 CODE rows scope to a module rather than a package, and until the
    # sweep that fixed :func:`_production_files` every one of them scanned an
    # EMPTY file set: the resolver required a directory, so `agent_runtime.status`
    # named a path that does not exist. The two assertions above could not see
    # that, because both canaries are package-scoped.
    module_scope = ("agent_runtime.status",)
    assert len(_scan_paths(module_scope)) == 1, _scan_paths(module_scope)
    module_live = Tombstone(
        "build_status",
        Form.CODE,
        "canary",
        "n/a",
        "LIVE — agent_runtime.status.build_status is the module's entry point",
        module_scope,
    )
    assert code_offenders(module_live), "a module-scoped scan found no live reference"


def test_no_production_file_failed_to_parse():
    """A file the scanner cannot render is a HOLE, not a pass."""

    unparseable = [
        str(path.relative_to(HERMES_ROOT))
        for path in _production_files(PRODUCTION_PACKAGES)
        if _rendered(path).startswith("<<UNPARSEABLE")
    ]
    assert unparseable == []


# -------------------------------------------------------------------------
# THE META-INVARIANT — an unresolvable scope must be COVERED, never SKIPPED
# -------------------------------------------------------------------------
#
# A row whose scope no longer resolves cannot fail. Left alone, that is a
# permanent pass wearing a row's clothes — precisely the "silent decay" this
# registry's header promises does not exist here. Both skips below were added
# as conveniences and each turned a mis-scoped row into a green forever:
#
#   ATTR:        ``if find_spec(dotted) is None: continue``
#   CLASS_ATTR:  ``if owner is None: return``
#
# The legitimate case they were reaching for is real — a LATER wave often
# retires the whole owner module, which is a STRICTLY STRONGER absence than the
# member row. S66 keeps that case working, but makes it PROVE itself: the
# stronger absence must be a row in this same table. An unresolvable scope with
# no covering row is now a FAILURE naming what to add, so a typo'd scope, a
# renamed module or an undeclared deletion surfaces instead of decaying.


def _module_row_covers(dotted: str) -> bool:
    """True when ``dotted`` — or any ancestor package — carries a MODULE row.

    Banning a package bans everything under it, so an ancestor row is the
    stronger absence and legitimately supersedes a member row beneath it.
    """

    banned = {row.text for row in MODULE_ROWS}
    parts = dotted.split(".")
    return any(".".join(parts[: index + 1]) in banned for index in range(len(parts)))


def _attr_row_covers(dotted: str, name: str) -> bool:
    """True when some ATTR row bans ``name`` on module ``dotted``."""

    return any(
        row.text == name and dotted in row.scope for row in ATTR_ROWS
    )


def _spec_missing(dotted: str) -> bool:
    """``find_spec`` semantics, but a missing PARENT package counts as missing
    rather than raising ``ModuleNotFoundError`` out of the gate."""

    try:
        return importlib.util.find_spec(dotted) is None
    except (ImportError, ValueError):
        return True


# -------------------------------------------------------------------------
# THE GATE
# -------------------------------------------------------------------------


@pytest.mark.parametrize("row", MODULE_ROWS, ids=lambda row: row.label)
def test_tombstoned_module_is_not_importable(row: Tombstone):
    assert importlib.util.find_spec(row.text) is None, f"{row.label}: {row.reason}"


@pytest.mark.parametrize("row", ATTR_ROWS, ids=lambda row: row.label)
def test_tombstoned_attribute_is_gone(row: Tombstone):
    for dotted in row.scope:
        if _spec_missing(dotted):
            # A later wave may retire the whole owner module — a STRONGER
            # absence than this member row, and the one case where not
            # resolving the scope is correct. It must be DECLARED, not assumed.
            assert _module_row_covers(dotted), (
                f"{row.label}: scope module {dotted!r} does not exist and no "
                f"MODULE row covers it, so this row can never fail again. "
                f"Either add the MODULE tombstone for {dotted!r} (if the module "
                f"was deleted) or retarget the row's scope (if it was renamed). "
                f"Original reason: {row.reason}"
            )
            continue
        module = _resolve(dotted)
        assert not hasattr(module, row.text), f"{dotted}.{row.text}: {row.reason}"


@pytest.mark.parametrize("row", IMPORT_ROWS, ids=lambda row: row.label)
def test_tombstoned_top_level_import_binding_is_gone(row: Tombstone):
    for relative in row.scope:
        source = (HERMES_ROOT / relative).read_text(encoding="utf-8")
        assert row.text not in _top_level_import_bindings(source), (
            f"{relative} imports {row.text}: {row.reason}"
        )


@pytest.mark.parametrize("row", CLASS_ATTR_ROWS, ids=lambda row: row.label)
def test_tombstoned_class_attribute_is_gone(row: Tombstone):
    module_name, class_name, attr = row.text.rsplit(".", 2)
    dotted = f"{row.scope[0]}.{module_name}"
    if _spec_missing(dotted):
        assert _module_row_covers(dotted), (
            f"{row.label}: owner module {dotted!r} does not exist and no MODULE "
            f"row covers it, so this row can never fail again. Add the MODULE "
            f"tombstone, or retarget the row's scope. "
            f"Original reason: {row.reason}"
        )
        return
    module = _resolve(dotted)
    owner = getattr(module, class_name, None)
    if owner is None:
        # The owning CLASS went while its module survived. That is a stronger
        # absence than this member row — but only if it is DECLARED, which for
        # a surviving module means an ATTR row banning the class name itself.
        # (A MODULE row also covers it, for the module-deleted-later case.)
        assert _attr_row_covers(dotted, class_name) or _module_row_covers(dotted), (
            f"{row.label}: class {dotted}.{class_name} no longer exists, so this "
            f"row can never fail again, and nothing in the registry bans the "
            f"class itself. Add an ATTR row for {class_name!r} scoped to "
            f"{dotted!r} (superseding this one), or retarget this row. "
            f"Original reason: {row.reason}"
        )
        return
    assert not hasattr(owner, attr), f"{row.text}: {row.reason}"


@pytest.mark.parametrize("row", EVENT_ROWS, ids=lambda row: row.label)
def test_tombstoned_event_type_is_deregistered(row: Tombstone):
    from agent_runtime.decision_contract_registry import event_catalog
    from agent_runtime.events import Event, EventLog
    from hermes_time import now

    assert row.text not in ALLOWED_EVENT_TYPES, f"{row.label}: {row.reason}"
    assert row.text not in event_catalog(), f"{row.label}: {row.reason}"
    with pytest.raises(ValueError, match="unknown event type"):
        EventLog().append(
            Event(
                ts=now(),
                type=row.text,
                task_id=None,
                run_id=None,
                persona_id=None,
                payload={},
            )
        )


@pytest.mark.parametrize("row", PATH_ROWS, ids=lambda row: row.label)
def test_tombstoned_path_does_not_exist(row: Tombstone):
    assert not (HERMES_ROOT / row.text).exists(), f"{row.label}: {row.reason}"


@pytest.mark.parametrize("row", CODE_ROWS, ids=lambda row: row.label)
def test_every_code_row_scope_resolves_to_something(row: Tombstone):
    """The CODE arm of the S66 meta-invariant: an unresolvable scope must be
    COVERED, never SKIPPED.

    S66 closed this for ATTR and CLASS_ATTR — a scope module that ``find_spec``
    cannot resolve fails unless a MODULE row declares the stronger absence. The
    CODE arm kept the identical silent skip one layer down, in
    :func:`_production_files`: it required the scope to be a *directory*, so
    every module-scoped row resolved to a path that does not exist and scanned
    an empty file set. **32 of 155 CODE rows were inert**, including every row
    protecting ``agent_runtime.status``, ``agent_runtime.observability``,
    ``agent_runtime.stream``, ``agent_runtime.persona_runtime`` and
    ``agent_runtime.persona_assignments``. They asserted ``[] == []`` over
    nothing at all, and they would have kept doing it through a deletion
    campaign that treats a green row as a verified absence.

    The scan itself deliberately does not decide what an unresolvable scope
    MEANS — the same reasoning the ATTR arm records. A retired owner module is a
    legitimate and STRICTLY STRONGER absence; a typo'd or renamed scope is a row
    that can never fail again. Only the registry can tell those apart, so the
    question is answered here, against the MODULE rows, and the failure names
    what to add rather than decaying quietly."""

    for dotted in _unresolvable_scope_tokens(row.scope):
        assert _module_row_covers(dotted), (
            f"{row.label}: scope {dotted!r} names neither a package directory "
            f"nor a module file, so this row scans nothing and can never fail. "
            f"Either add the MODULE tombstone for {dotted!r} (if it was deleted) "
            f"or retarget the row's scope (if it was renamed). "
            f"Original reason: {row.reason}"
        )
    # And the resolvable scopes must actually yield files. A scope that resolves
    # to an EMPTY package directory is the same permanent pass by another route.
    assert _scan_paths(row.scope), (
        f"{row.label}: scope {row.scope} resolves but contains no production "
        f"file, so this row scans nothing and can never fail"
    )


@pytest.mark.parametrize("row", CODE_ROWS, ids=lambda row: row.label)
def test_tombstoned_name_is_absent_from_production_code(row: Tombstone):
    offenders = code_offenders(row)
    assert offenders == [], f"{row.label} reappeared in {offenders}: {row.reason}"


_ROUND4_COVERAGE_BASE = "4a21f0779"
_PRODUCTION_PACKAGES = ("agent_runtime", "hermes_cli")


def _parsed(source: str) -> ast.Module | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def _production_imports(tree: ast.AST) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    direct: dict[str, tuple[str, str]] = {}
    modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(
            _PRODUCTION_PACKAGES
        ):
            for alias in node.names:
                if alias.name != "*":
                    direct[alias.asname or alias.name] = (node.module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(_PRODUCTION_PACKAGES):
                    modules[alias.asname or alias.name.rsplit(".", 1)[-1]] = alias.name
    return direct, modules


def _production_references(
    tree: ast.AST,
    direct: dict[str, tuple[str, str]],
    modules: dict[str, str],
) -> set[tuple[str, str]]:
    references: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in direct:
                references.add(direct[node.id])
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
        ):
            references.add((modules[node.value.id], node.attr))
    return references


def _live_production_symbol(subject: tuple[str, str]) -> bool:
    module_name, symbol = subject
    source_path = HERMES_ROOT / f"{module_name.replace('.', '/')}.py"
    if not source_path.is_file():
        return False
    tree = _parsed(source_path.read_text(encoding="utf-8", errors="replace"))
    return bool(
        tree
        and any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == symbol
            for node in tree.body
        )
    )


@functools.lru_cache(maxsize=1)
def _covered_production_subjects() -> frozenset[tuple[str, str]]:
    """Every ``(module, symbol)`` any test in the tree references.

    A SECOND full-tree walk, independent of the production render above and
    larger: 2880 files under ``tests/``, 43.9s to parse. It was inline in the
    test below, so that one item carried all of it — the same per-test-budget /
    per-session-cost mismatch, one screen further down the same file, and the
    one that surfaced the moment the first was fixed.
    """

    covered: set[tuple[str, str]] = set()
    for path in (HERMES_ROOT / "tests").rglob("test_*.py"):
        tree = _parsed(path.read_text(encoding="utf-8", errors="replace"))
        if tree is None:
            continue
        direct, modules = _production_imports(tree)
        covered.update(_production_references(tree, direct, modules))
    return frozenset(covered)


# Warmed at import for the same reason as the production render above: the walk
# belongs to the session, not to whichever item reaches it first.
_covered_production_subjects()

#: Snapshotted at import so the guard fails deterministically if the warm goes.
_COVERAGE_CACHE_SIZE_AT_IMPORT = _covered_production_subjects.cache_info().currsize


def test_the_test_tree_walk_is_also_paid_at_import():
    """The second walk's guard, and it exists because the first fix revealed it.

    Fixing ``test_the_scanner_is_not_vacuous`` moved the process death forward to
    here rather than removing it — this file had TWO independent full-tree walks
    billed to two different test items. A guard on only the first would have
    reported success on a file that still could not finish.
    """

    assert _COVERAGE_CACHE_SIZE_AT_IMPORT == 1, (
        "the tests/ tree walk was NOT warmed at module import (cache size at "
        f"import: {_COVERAGE_CACHE_SIZE_AT_IMPORT}). "
        "test_round4_deleted_tests_left_no_live_production_subject_uncovered "
        "then pays ~44s of parsing inside its own item, against the 30s cap — "
        "and --timeout-method=thread kills the process. Restore the warm."
    )
    assert _covered_production_subjects.cache_info().misses == 1, (
        "the tests/ tree was walked more than once: "
        f"{_covered_production_subjects.cache_info()}"
    )
    assert len(_covered_production_subjects()) > 100, (
        "the coverage walk resolved almost nothing; the gate below would pass "
        "vacuously"
    )


@functools.lru_cache(maxsize=1)
def _round4_uncovered_subjects() -> tuple[str, ...]:
    """Live production symbols whose last direct test reference a deletion took.

    Hoisted and warmed for the same reason as the two walks above, and this one
    is the least obviously expensive of the three, which is exactly why it is
    worth stating. Its cost is not a walk but a `git show` SUBPROCESS per test
    file changed since ``_ROUND4_COVERAGE_BASE``, plus a parse of each revision.
    Measured at 21.4s after the tests/ walk was hoisted off it.

    21s under a 30s cap reads like headroom and is not. The
    ``test_no_other_module_states_the_contract_version`` case in this same sweep
    measured 8s standalone and still crossed 30s inside a long-lived
    single-process run — a ~4x dilation once several thousand tests have already
    executed in the interpreter. And unlike a fixed tree walk this cost GROWS: the
    base commit is pinned while HEAD advances, so the changed-file list gets
    longer every week. Leaving it at 21s would just be scheduling the same
    process kill for a later date.
    """

    covered = _covered_production_subjects()

    changed_tests = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            f"{_ROUND4_COVERAGE_BASE}..HEAD",
            "--",
            "tests",
        ],
        cwd=HERMES_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    uncovered: list[str] = []
    for relative in changed_tests:
        old = subprocess.run(
            ["git", "show", f"{_ROUND4_COVERAGE_BASE}:{relative}"],
            cwd=HERMES_ROOT,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
        )
        old_tree = _parsed(old.stdout) if old.returncode == 0 else None
        if old_tree is None:
            continue
        current_path = HERMES_ROOT / relative
        current_tree = (
            _parsed(current_path.read_text(encoding="utf-8", errors="replace"))
            if current_path.is_file()
            else None
        )
        current_test_names = {
            node.name
            for node in (current_tree.body if current_tree else ())
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        }
        module_direct, module_imports = _production_imports(old_tree)
        for old_test in old_tree.body:
            if not isinstance(old_test, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not old_test.name.startswith("test_") or old_test.name in current_test_names:
                continue
            local_direct, local_imports = _production_imports(old_test)
            subjects = _production_references(
                old_test,
                module_direct | local_direct,
                module_imports | local_imports,
            )
            for subject in subjects:
                if _live_production_symbol(subject) and subject not in covered:
                    uncovered.append(
                        f"{relative}::{old_test.name} deleted the last direct test "
                        f"reference to {subject[0]}.{subject[1]}"
                    )
    return tuple(uncovered)


# Warmed at import, like the two walks above.
_round4_uncovered_subjects()

#: Snapshotted at import so the guard fails deterministically if the warm goes.
_ROUND4_CACHE_SIZE_AT_IMPORT = _round4_uncovered_subjects.cache_info().currsize


def test_round4_deleted_tests_left_no_live_production_subject_uncovered():
    """A removed input vector may not silently erase a live symbol's coverage."""

    uncovered = _round4_uncovered_subjects()
    assert uncovered == (), "\n".join(uncovered)


def test_the_round4_git_walk_is_also_paid_at_import():
    """The third warm's guard.

    Three independent session-scoped computations in one file, each of which was
    billed to a single test item. This one was never over the cap on the day it
    was written — it is here because its cost grows with every commit to
    ``tests/`` while its base stays pinned.
    """

    assert _ROUND4_CACHE_SIZE_AT_IMPORT == 1, (
        "the round-4 coverage diff was NOT warmed at module import (cache size "
        f"at import: {_ROUND4_CACHE_SIZE_AT_IMPORT}). It runs a `git show` per "
        "changed test file and measured 21.4s inside its own item against the "
        "30s cap — a margin that shrinks with every commit. Restore the warm."
    )
    assert _round4_uncovered_subjects.cache_info().misses == 1, (
        "the round-4 git walk ran more than once: "
        f"{_round4_uncovered_subjects.cache_info()}"
    )


#: S40 gated its two names in MARKDOWN too, on CODE FORMS ONLY — a doc may name
#: a retired renderer (the removal log has to be writable) but may not carry a
#: pasteable call or import of it. Carried here verbatim so deleting the s40
#: file loses nothing; the AST scanner cannot help with prose, so this half
#: stays a form gate, which is honest for exactly the same reason s40's was.
DOC_CODE_FORMS = (
    "def render_objective",
    "render_objective(",
    "import objective_templates",
    "from .objective_templates",
    "from agent_runtime.objective_templates",
    "objective_templates.render",
)


def test_no_doc_carries_a_pasteable_call_to_a_retired_renderer():
    offenders: list[str] = []
    docs = [
        path
        for path in sorted((HERMES_ROOT / "docs").rglob("*.md"))
        if not any(part in _SKIP_DIRS for part in path.parts)
    ]
    assert len(docs) > 10, "the doc scan found nothing — the gate would be vacuous"
    for path in docs:
        text = path.read_text(encoding="utf-8", errors="replace")
        for form in DOC_CODE_FORMS:
            if form in text:
                offenders.append(f"{path.relative_to(HERMES_ROOT)}: {form}")
    assert offenders == []
