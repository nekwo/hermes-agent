"""THE tombstone registry — one data-driven table for every "this name must not
come back" row the s40-s57 removal campaign produced.

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

=============================================================================
WHAT IS DELIBERATELY *NOT* A ROW HERE
=============================================================================

Absence assertions whose subject is a runtime SHAPE rather than a name:

* **parameter absence** — "passing ``tasks=`` must raise ``TypeError``". That is
  a signature fact, checked by calling; a name scan would fire on any unrelated
  local called ``tasks``.
* **wire-key absence** — "``build_status()`` must not emit ``repo_locks``". That
  is a fact about a produced dict, and the only honest way to check it is to
  build the frame.
* **exact key-set / count pins** — ``migration.counts``, ``RunStore``'s public
  surface, ``OPERATOR_SUMMARY_EVENT_TYPES``.

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
import importlib
import importlib.util
import textwrap
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pytest

from agent_runtime.events import ALLOWED_EVENT_TYPES

HERMES_ROOT = Path(__file__).resolve().parents[2]

#: Production packages every row is enforced over unless it narrows the scope.
#:
#: Chosen as the widest tuple any migrated row used, so consolidation could only
#: WIDEN protection. ``tests`` is excluded on purpose (S55's rule: a symbol kept
#: alive only by the test written to exercise it is a closed loop, not coverage)
#: and so is this file, which names every banned symbol by definition.
PRODUCTION_PACKAGES = (
    "agent_runtime",
    "hermes_cli",
    "tools",
    "agent",
    "acp_adapter",
    "gateway",
    "scripts",
)

_SKIP_DIRS = {"__pycache__", ".venv", "venvs", "node_modules", ".git"}


class Form(Enum):
    MODULE = "module"
    ATTR = "attr"
    CLASS_ATTR = "class_attr"
    EVENT = "event"
    CODE = "code"
    PATH = "path"


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
        "role_checklists.py went 420 -> 113 lines. ONLY "
        "validate_checklist_payload_structure survives, because "
        "decision_contract_registry.validate_payload_keys calls it on every "
        "typed decision (live via `hermes harness contracts verify-examples`)",
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
    files: list[Path] = []
    for package in packages:
        root = HERMES_ROOT / package.replace(".", "/")
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            files.append(path)
    return files


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
    for path in _production_files(row.scope):
        if path.resolve() == Path(__file__).resolve():
            continue
        if row.text in _rendered(path):
            offenders.append(str(path.relative_to(HERMES_ROOT)).replace("\\", "/"))
    return offenders


def _resolve(dotted: str):
    module = importlib.import_module(dotted)
    return module


# =========================================================================


CODE_ROWS = tuple(row for row in TOMBSTONES if row.form is Form.CODE)
MODULE_ROWS = tuple(row for row in TOMBSTONES if row.form is Form.MODULE)
ATTR_ROWS = tuple(row for row in TOMBSTONES if row.form is Form.ATTR)
CLASS_ATTR_ROWS = tuple(row for row in TOMBSTONES if row.form is Form.CLASS_ATTR)
EVENT_ROWS = tuple(row for row in TOMBSTONES if row.form is Form.EVENT)
PATH_ROWS = tuple(row for row in TOMBSTONES if row.form is Form.PATH)


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


def test_the_scanner_is_not_vacuous():
    """A LIVE neighbour must still be found, in every scoped package.

    Without this every CODE assertion below could pass because the walk found
    no files at all.
    """

    files = _production_files(PRODUCTION_PACKAGES)
    assert len(files) > 300, len(files)
    live = Tombstone(
        "validate_checklist_payload_structure",
        Form.CODE,
        "canary",
        "n/a",
        "LIVE — decision_contract_registry.validate_payload_keys calls it on "
        "every typed decision; role_checklists.py survives for exactly this",
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


def test_no_production_file_failed_to_parse():
    """A file the scanner cannot render is a HOLE, not a pass."""

    unparseable = [
        str(path.relative_to(HERMES_ROOT))
        for path in _production_files(PRODUCTION_PACKAGES)
        if _rendered(path).startswith("<<UNPARSEABLE")
    ]
    assert unparseable == []


# -------------------------------------------------------------------------
# THE GATE
# -------------------------------------------------------------------------


@pytest.mark.parametrize("row", MODULE_ROWS, ids=lambda row: row.label)
def test_tombstoned_module_is_not_importable(row: Tombstone):
    assert importlib.util.find_spec(row.text) is None, f"{row.label}: {row.reason}"


@pytest.mark.parametrize("row", ATTR_ROWS, ids=lambda row: row.label)
def test_tombstoned_attribute_is_gone(row: Tombstone):
    for dotted in row.scope:
        module = _resolve(dotted)
        assert not hasattr(module, row.text), f"{dotted}.{row.text}: {row.reason}"


@pytest.mark.parametrize("row", CLASS_ATTR_ROWS, ids=lambda row: row.label)
def test_tombstoned_class_attribute_is_gone(row: Tombstone):
    module_name, class_name, attr = row.text.rsplit(".", 2)
    module = _resolve(f"{row.scope[0]}.{module_name}")
    owner = getattr(module, class_name)
    assert not hasattr(owner, attr), f"{row.text}: {row.reason}"


@pytest.mark.parametrize("row", EVENT_ROWS, ids=lambda row: row.label)
def test_tombstoned_event_type_is_deregistered(row: Tombstone):
    from agent_runtime.decision_contract_registry import event_catalog

    assert row.text not in ALLOWED_EVENT_TYPES, f"{row.label}: {row.reason}"
    assert row.text not in event_catalog(), f"{row.label}: {row.reason}"


@pytest.mark.parametrize("row", PATH_ROWS, ids=lambda row: row.label)
def test_tombstoned_path_does_not_exist(row: Tombstone):
    assert not (HERMES_ROOT / row.text).exists(), f"{row.label}: {row.reason}"


@pytest.mark.parametrize("row", CODE_ROWS, ids=lambda row: row.label)
def test_tombstoned_name_is_absent_from_production_code(row: Tombstone):
    offenders = code_offenders(row)
    assert offenders == [], f"{row.label} reappeared in {offenders}: {row.reason}"
