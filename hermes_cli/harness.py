from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_time import now
from hermes_constants import get_hermes_home
from hermes_cli.profiles import list_profiles

from agent_runtime.cli_format import emit_json, human_task_line, task_summary
from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config
from agent_runtime.continuity import return_summary_to_parent_session
from agent_runtime.operator_control import operator_takeover_worker
from agent_runtime.coordinator_permissions import (
    CoordinatorPermissionScope,
    authorize_coordinator_action,
    scope_for_persona,
)
from agent_runtime.decision_contract_examples import verify_harness_skill_examples
from agent_runtime.decision_contract_registry import canonical_role_value, contract_manifest, hud_shape_index_for_stage, verify_registry
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.default_plan import ensure_default_mission_plan
from agent_runtime.daemon import MissionDaemon, daemon_status_schema, read_daemon_status, start_daemon, stop_daemon
from agent_runtime.errors import (
    AgentRuntimeError,
    AlreadyExists,
    EventPayloadTooLarge,
    InvalidTransition,
    NotFound,
    ProofMissing,
    RuntimeRootMismatch,
    StaleRun,
    StoreCorrupt,
)
from agent_runtime.events import EventLog
from agent_runtime.goal_hygiene import activate_foreground_runtime, prepare_new_goal_runtime
from agent_runtime.harness_doctor import (
    DEFAULT_STALE_INCIDENT_DAYS,
    DEFAULT_STALE_INCIDENT_HOURS,
    DEFAULT_STALE_RUN_HOURS,
    DEFAULT_STALE_TASK_DAYS,
    DEFAULT_STALE_WORKER_HOURS,
    DEFAULT_WORKTREE_MIN_AGE_SECONDS,
    run_harness_doctor,
)
from agent_runtime.goal_runner import GoalRunOptions, MissionRuntimeController
from agent_runtime.launcher_process_hygiene import launcher_visual_cleanup_needed
from agent_runtime.models import AgentPersona, Event, Task, apply_instance_model_overrides
from agent_runtime import paths
from agent_runtime.persona_assignments import (
    ChatBusyError,
    PersonaAssignmentSpec,
    StaleModelOverrideWrite,
    PersonaAssignmentStore,
    PersonaInstanceStore,
    persona_assignment_store_enabled,
    persona_assignment_summary,
    persona_chat_session_id_for,
    persona_instance_runtime_enabled,
    persona_instance_summary,
    persona_instance_id_for,
    safe_assignment_token,
    safe_assignment_text,
    safe_optional_token,
)
from agent_runtime.persona_diagnostics import PersonaDiagnosticController, PersonaDiagnosticOptions
from agent_runtime.profile_context import active_profile_name
from agent_runtime.realm_sync import (
    RealmSyncError,
    publish_realm_sync,
    pull_realm_sync,
    realm_sync_status,
    sync_artifacts_for_workspace_agent,
)
from agent_runtime.resolution import resolution_table, resolve_runtime
from agent_runtime.burn_in import STAGE47_CASES, STAGE47_SUITE, burn_in_status, create_burn_in, run_burn_in_case, summarize_burn_in, swarm_certification_allows_production
from agent_runtime.migrations import effective_config_summary, migration_status
from agent_runtime.mission_chat_turns import MissionChatTurnPersistOutcome, mark_stale_running_turns_interrupted, persist_mission_chat_turn
from agent_runtime.mission_chat_steer import start_active_mission_chat_turn, submit_mission_chat_steer
from agent_runtime.observability import build_observability
from agent_runtime.persona_runtime import GPTPersonaRuntime
from agent_runtime.personas import profile_chat_toolsets, seed_personas
from agent_runtime.prompt_observability import mission_chat_prompt_observability, persist_prompt_observability_context
from agent_runtime.queued_skills import consume_skills_for_next_turn, queue_skill_for_next_turn
from agent_runtime.provider_health import provider_health_for_personas
from agent_runtime.skill_install import install_harness_skills, install_harness_skills_for_personas
from agent_runtime.snapshot import build_snapshot, write_snapshot
from agent_runtime.smoke import run_smoke
from agent_runtime.scope_control import find_discovery_task
from agent_runtime.planning import apply_planning_decision
from agent_runtime.states import TaskState, RunState, WorkerSessionState
from agent_runtime.status import build_status
from agent_runtime.steering import execute_steer_action
from agent_runtime.store import ACTIVE_RUN_STATES, AgentStore, IncidentStore, ProofStore, RunStore, TaskStore
from agent_runtime.store import RealmStore, WorkspaceStore
from agent_runtime.ticker import TickEngine
from agent_runtime.tool_visibility import ToolVisibilityOptions, resolve_tool_visibility
from agent_runtime.tool_permissions import ChatToolPermissionStore, permission_state_for_chat
from agent_runtime.tool_turn_history import persist_tool_turn_actual
from agent_runtime.worker_sessions import WorkerSessionStore, worker_session_summary

PERSONA_CHAT_SESSION_SOURCE = "agent_runtime_persona_chat"


STAGE42_SCHEMA_VERSION = 1
ERROR_EXIT_CODES = {
    "not_found": 3,
    "realm_not_found": 3,
    "workspace_not_found": 3,
    "goal_not_found": 3,
    "run_not_found": 3,
    "lane_not_found": 3,
    "worker_not_found": 3,
    "persona_not_found": 3,
    "blueprint_not_found": 3,
    "invalid_request": 2,
    "invalid_payload": 2,
    "blueprint_invalid": 2,
    "repo_scope_invalid": 2,
    "invalid_binding": 2,
    "unbound_required_slot": 2,
    "invalid_isolation": 2,
    "duplicate_conflict": 4,
    "already_exists": 4,
    "agent_busy": 4,
    "agent_already_assigned": 4,
    "lane_budget_exceeded": 4,
    "repo_locked": 4,
    "spawn_scope_exhausted": 4,
    "kill_scope_denied": 4,
    "sync_conflict": 4,
    "sync_behind": 4,
    "sync_secret_excluded": 4,
    # Permission / auth (5)
    "permission_denied": 5,
    "membership_denied": 5,
    "role_insufficient": 5,
    "provider_auth_missing": 5,
    "provider_auth_expired": 5,
    "sync_auth_failed": 5,
    # State / precondition (6)
    "goal_blocked": 6,
    "goal_terminal": 6,
    "invalid_transition": 6,
    "stale_run": 6,
    "planning_locked": 6,
    "proof_missing": 6,
    "proof_gate_failed": 6,
    "needs_operator_confirm": 6,
    # Skills / readiness (6)
    "skill_hash_mismatch": 6,
    "missing_skill": 6,
    "skill_install_failed": 6,
    "profile_not_ready": 6,
    "confirmation_required": 8,
    # Runtime / infra (7)
    "runtime_unavailable": 7,
    "daemon_offline": 7,
    "wrong_runtime_root": 7,
    "profile_mismatch": 7,
    "snapshot_stale": 7,
    "contract_version_mismatch": 7,
    "context_bundle_too_large": 7,
    "budget_exhausted": 7,
    "stagec_visual_failed": 7,
    "sync_remote_unreachable": 7,
    "install_clone_failed": 7,
    "install_venv_failed": 7,
    "install_postinstall_failed": 7,
    "install_dependency_missing": 7,
    # Data integrity (1)
    "store_corrupt": 1,
    "event_payload_too_large": 1,
    "internal_error": 1,
    "timeout": 124,
}


def _add_stage42_global_args(parser, *, mutation: bool = False) -> None:
    def add(*flags, **kwargs):
        if any(flag in parser._option_string_actions for flag in flags):  # noqa: SLF001 - argparse has no public query
            return
        parser.add_argument(*flags, **kwargs)

    add("-o", "--output", choices=["json", "table", "yaml", "wide"], default=None)
    add("--json", action="store_true", help="Alias for -o json")
    add("-q", "--quiet", action="store_true")
    add("--no-color", action="store_true")
    add("--fields", default=None)
    add("--sort", default=None)
    add("--filter", action="append", default=[])
    add("--limit", type=int, default=None)
    add("--cursor", default=None)
    add("--watch", "-w", action="store_true")
    add("--since", default=None)
    if mutation:
        add("--dry-run", action="store_true")
        add("--yes", "-y", action="store_true")
        add("--idempotency-key", default=None)


def _add_coordinator_permission_args(parser) -> None:
    parser.add_argument("--coordinator-id", default="neko_supervisor", help="Coordinator persona id when --requested-by is coordinator or coordinator:<id>")
    parser.add_argument("--coordinator-max-spawns", type=int, default=None, help="In-scope create/spawn grant for this coordinator action")
    parser.add_argument("--coordinator-spawns-used", type=int, default=0, help="Create/spawn actions already used in this coordinator scope")
    parser.add_argument("--coordinator-may-kill-own", action="store_true", default=None, help="Allow killing instances spawned by this coordinator")
    parser.add_argument("--coordinator-no-kill-own", action="store_true", default=None, help="Require confirmation even for own-spawned instances")
    parser.add_argument("--coordinator-may-kill-others", action="store_true", default=None, help="Allow killing non-operator instances spawned by another coordinator")


def build_parser(parent_subparsers) -> None:
    parser = parent_subparsers.add_parser("harness", help="Experimental Agent Runtime Harness")
    _add_stage42_global_args(parser)
    subs = parser.add_subparsers(dest="harness_command")
    parser.set_defaults(func=harness_command)

    init = subs.add_parser("init", help="Initialize harness store and default personas")
    init.add_argument("--json", action="store_true")
    init.set_defaults(func=_cmd_init)

    goal = subs.add_parser("goal", help="Create and run Harness goals in-process")
    goal_subs = goal.add_subparsers(dest="goal_command")
    goal_list = goal_subs.add_parser("list", help="List Harness goals")
    goal_list.add_argument("--workspace", default=None)
    goal_list.add_argument("--state", choices=["open", "done", "blocked", "all"], default="open")
    _add_stage42_global_args(goal_list)
    goal_list.set_defaults(func=_cmd_goal_list)
    goal_show = goal_subs.add_parser("show", help="Show one Harness goal")
    goal_show.add_argument("goal_id")
    goal_show.add_argument("--full", action="store_true")
    _add_stage42_global_args(goal_show)
    goal_show.set_defaults(func=_cmd_goal_show)
    goal_history = goal_subs.add_parser("history", help="Show redaction-safe goal event history")
    goal_history.add_argument("goal_id")
    goal_history.add_argument("--limit", type=int, default=50)
    _add_stage42_global_args(goal_history)
    goal_history.set_defaults(func=_cmd_goal_history)
    goal_create = goal_subs.add_parser("create", help="Create a Harness goal")
    goal_create.add_argument("--title")
    goal_create.add_argument("--description")
    goal_create.add_argument("--requested-by", default="cli")
    goal_create.add_argument("--request-json")
    goal_create.add_argument("--workspace", default=None)
    goal_create.add_argument("--start-daemon", dest="start_daemon", action="store_true", default=None)
    goal_create.add_argument("--no-start-daemon", dest="start_daemon", action="store_false")
    _add_stage42_global_args(goal_create, mutation=True)
    goal_create.set_defaults(func=_cmd_goal_create)
    goal_run = goal_subs.add_parser("run", help="Create a goal and run bounded ticks until a meaningful boundary")
    goal_run.add_argument("--title", required=True)
    goal_run.add_argument("--description", required=True)
    goal_run.add_argument("--requested-by", default="cli")
    goal_run.add_argument("--max-actions", type=int, default=16)
    goal_run.add_argument("--max-seconds", type=float, default=None)
    goal_run.add_argument("--archive-on-done", action="store_true")
    goal_run.add_argument("--requires-visual-proof", action="store_true")
    goal_run.add_argument("--affected-repo", action="append", default=[])
    goal_run.add_argument("--acceptance", action="append", default=[])
    goal_run.add_argument("--non-goal", action="append", default=[])
    goal_run.add_argument("--blueprint", default="neko_two_dev_default", help="Blueprint id for graph-routed goal creation")
    goal_run.add_argument("--bind", action="append", default=[], help="Bind a blueprint slot, e.g. builder=persona:dev")
    goal_run.add_argument("--workspace", default=None)
    goal_run.add_argument("--runtime-root", default=None, help="Pin the expected resolved Harness runtime root")
    _add_stage42_global_args(goal_run, mutation=True)
    goal_run.set_defaults(func=_cmd_goal_run)
    goal_unblock = goal_subs.add_parser("unblock", help="Operator-unblock a Harness goal")
    goal_unblock.add_argument("goal_id")
    goal_unblock.add_argument("--reason", required=True)
    goal_unblock.add_argument("--state", choices=["created", "pm_ready_for_dev", "dev_implementing", "qa_testing"], default="created")
    goal_unblock.add_argument("--rescope", action="store_true")
    _add_stage42_global_args(goal_unblock, mutation=True)
    goal_unblock.set_defaults(func=_cmd_goal_unblock)
    goal_cancel = goal_subs.add_parser("cancel", help="Cancel a Harness goal")
    goal_cancel.add_argument("goal_id")
    goal_cancel.add_argument("--reason", required=True)
    _add_stage42_global_args(goal_cancel, mutation=True)
    goal_cancel.set_defaults(func=_cmd_goal_cancel)
    goal_archive = goal_subs.add_parser("archive", help="Archive a terminal Harness goal")
    goal_archive.add_argument("goal_id")
    _add_stage42_global_args(goal_archive, mutation=True)
    goal_archive.set_defaults(func=_cmd_goal_archive)
    goal_archive_ready = goal_subs.add_parser("archive-ready", help="Archive terminal ready/done Harness goals while preserving evidence")
    _add_stage42_global_args(goal_archive_ready, mutation=True)
    goal_archive_ready.set_defaults(func=_cmd_task_archive_ready)

    blueprint = subs.add_parser("blueprint", help="Validate and run Agent Runtime blueprints headlessly")
    blueprint_subs = blueprint.add_subparsers(dest="blueprint_command", required=True)
    blueprint_list = blueprint_subs.add_parser("list", help="List bundled Agent Runtime blueprints")
    blueprint_list.add_argument("--json", action="store_true")
    blueprint_list.set_defaults(func=_cmd_blueprint_list)
    blueprint_validate = blueprint_subs.add_parser("validate", help="Validate one Agent Runtime blueprint by id or path")
    blueprint_validate.add_argument("blueprint")
    blueprint_validate.add_argument("--json", action="store_true")
    blueprint_validate.set_defaults(func=_cmd_blueprint_validate)
    blueprint_run = blueprint_subs.add_parser("run", help="Instantiate a blueprint; --dry-run validates without executing agents")
    blueprint_run.add_argument("blueprint")
    blueprint_run.add_argument("--goal", required=True)
    blueprint_run.add_argument("--bind", action="append", default=[], help="Bind a slot, e.g. builder=persona:dev or builder=profile:gpt-launcher")
    blueprint_run.add_argument("--dry-run", action="store_true")
    blueprint_run.add_argument("--requested-by", default="cli")
    blueprint_run.add_argument("--json", action="store_true")
    blueprint_run.set_defaults(func=_cmd_blueprint_run)
    blueprint_save = blueprint_subs.add_parser("save", help="Create or update a blueprint from a JSON/YAML spec file (validated before write)")
    blueprint_save.add_argument("--spec-file", required=True, help="Path to a JSON or YAML blueprint spec")
    blueprint_save.add_argument("--json", action="store_true")
    blueprint_save.set_defaults(func=_cmd_blueprint_save)
    blueprint_matrix = blueprint_subs.add_parser("matrix-run", help="Instantiate one blueprint across varied slot bindings")
    blueprint_matrix.add_argument("blueprint")
    blueprint_matrix.add_argument("--goal", required=True)
    blueprint_matrix.add_argument("--bind", action="append", default=[], help="Base slot binding, e.g. verifier=persona:qa")
    blueprint_matrix.add_argument("--vary", action="append", default=[], help="Vary a slot across comma-separated bindings, e.g. builder=persona:dev,profile:gpt-launcher")
    blueprint_matrix.add_argument("--dry-run", action="store_true")
    blueprint_matrix.add_argument("--requested-by", default="cli")
    blueprint_matrix.add_argument("--json", action="store_true")
    blueprint_matrix.set_defaults(func=_cmd_blueprint_matrix_run)

    task = subs.add_parser("task", help="Manage harness tasks")
    task_subs = task.add_subparsers(dest="task_command")
    create = task_subs.add_parser("create", help="Create a harness task")
    create.add_argument("--title")
    create.add_argument("--description")
    create.add_argument("--request-json", help="Path to a Stage 38 canonical goal-create request JSON file")
    create.add_argument("--requested-by", default="cli")
    create.add_argument(
        "--affected-repo",
        action="append",
        default=[],
        help="Pin the goal's repo scope (EterniaLauncher, EterniaBackend, hermes-agent, or an alias like launcher/backend). Repeatable.",
    )
    create.add_argument("--start-daemon", dest="start_daemon", action="store_true", default=None, help="Start the Mission Daemon after creating the task")
    create.add_argument("--no-start-daemon", dest="start_daemon", action="store_false", help="Create the task without starting the Mission Daemon")
    create.add_argument("--json", action="store_true")
    create.set_defaults(func=_cmd_task_create)
    listp = task_subs.add_parser("list", help="List harness tasks")
    listp.add_argument("--state", choices=["open", "all", "done", "blocked"], default="open")
    listp.add_argument("--json", action="store_true")
    listp.set_defaults(func=_cmd_task_list)
    show = task_subs.add_parser("show", help="Show harness task")
    show.add_argument("task_id")
    show.add_argument("--events", type=int, default=0, help="Include the newest N task events")
    show.add_argument("--since", default=None, help="Include task events since an ISO-8601 timestamp")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=_cmd_task_show)
    history = task_subs.add_parser("history", help="Show redaction-safe task event history")
    history.add_argument("task_id")
    history.add_argument("--limit", type=int, default=50)
    history.add_argument("--since", default=None, help="Include task events since an ISO-8601 timestamp")
    history.add_argument("--json", action="store_true")
    history.set_defaults(func=_cmd_task_history)
    task_cancel = task_subs.add_parser("cancel", help="Cancel a harness task")
    task_cancel.add_argument("task_id")
    task_cancel.add_argument("--reason", required=True)
    task_cancel.add_argument("--json", action="store_true")
    task_cancel.set_defaults(func=_cmd_task_cancel)
    task_unblock = task_subs.add_parser("unblock", help="Operator-unblock or rescope a non-terminal harness task")
    task_unblock.add_argument("task_id")
    task_unblock.add_argument("--reason", required=True)
    task_unblock.add_argument("--state", choices=["created", "pm_ready_for_dev", "dev_implementing", "qa_testing"], default="created")
    task_unblock.add_argument("--rescope", action="store_true", help="Clear mission plan/stages so Neko can scope the task again")
    task_unblock.add_argument("--foreground", action="store_true", help="Reactivate this task as the foreground runtime lane")
    task_unblock.add_argument("--json", action="store_true")
    task_unblock.set_defaults(func=_cmd_task_unblock)
    task_steer = task_subs.add_parser("steer", help="Execute a live topology steer action")
    task_steer.add_argument("task_id")
    task_steer.add_argument("--action-id", default=None, help="Snapshot steer action id, e.g. steer:slot_lead:slot_builder:route")
    task_steer.add_argument("--verb", choices=["route", "spawn", "re-scope", "resolve", "verdict-back"], default=None)
    task_steer.add_argument("--source-node", dest="source_node_id", default=None)
    task_steer.add_argument("--target-node", dest="target_node_id", default=None)
    task_steer.add_argument("--reason", default="operator steer")
    task_steer.add_argument("--requested-by", default="operator")
    task_steer.add_argument("--json", action="store_true")
    task_steer.set_defaults(func=_cmd_task_steer)
    task_archive_ready = task_subs.add_parser("archive-ready", help="Archive terminal ready/done harness tasks while preserving evidence")
    task_archive_ready.add_argument("--json", action="store_true")
    task_archive_ready.set_defaults(func=_cmd_task_archive_ready)
    task_archive = task_subs.add_parser("archive", help="Archive one terminal harness task while preserving evidence")
    task_archive.add_argument("task_id")
    task_archive.add_argument("--json", action="store_true")
    task_archive.set_defaults(func=_cmd_task_archive)

    workspace = subs.add_parser("workspace", help="Manage Harness workspaces")
    workspace_subs = workspace.add_subparsers(dest="workspace_command", required=True)
    workspace_list = workspace_subs.add_parser("list", help="List workspaces")
    _add_stage42_global_args(workspace_list)
    workspace_list.set_defaults(func=_cmd_workspace_list)
    workspace_show = workspace_subs.add_parser("show", help="Show one workspace")
    workspace_show.add_argument("workspace_id")
    _add_stage42_global_args(workspace_show)
    workspace_show.set_defaults(func=_cmd_workspace_show)
    workspace_create = workspace_subs.add_parser("create", help="Create a workspace")
    workspace_create.add_argument("--name", required=True)
    workspace_create.add_argument("--realm", default=None)
    workspace_create.add_argument("--agent", action="append", default=[])
    workspace_create.add_argument("--blueprint", default=None)
    workspace_create.add_argument("--isolation", choices=["soft", "hard"], default="soft")
    workspace_create.add_argument("--max-lanes", type=int, default=None)
    _add_stage42_global_args(workspace_create, mutation=True)
    workspace_create.set_defaults(func=_cmd_workspace_create)
    workspace_use = workspace_subs.add_parser("use", help="Set active workspace")
    workspace_use.add_argument("workspace_id")
    workspace_use.add_argument(
        "--issued-at",
        dest="issued_at",
        default=None,
        help="ISO-8601 UTC instant the operator issued this switch; a pointer already owned by a strictly newer intent rejects this one as superseded (transport replay guard)",
    )
    _add_stage42_global_args(workspace_use, mutation=True)
    workspace_use.set_defaults(func=_cmd_workspace_use)
    workspace_actors = workspace_subs.add_parser("actors", help="List typed actors in a workspace")
    workspace_actors.add_argument("workspace_id")
    workspace_actors.add_argument("--kind", default=None)
    _add_stage42_global_args(workspace_actors)
    workspace_actors.set_defaults(func=_cmd_workspace_actors)
    workspace_add_agent = workspace_subs.add_parser("add-agent", help="Add a persona to a workspace roster")
    workspace_add_agent.add_argument("workspace_id")
    workspace_add_agent.add_argument("persona_id")
    _add_stage42_global_args(workspace_add_agent, mutation=True)
    workspace_add_agent.set_defaults(func=_cmd_workspace_add_agent)
    workspace_remove_agent = workspace_subs.add_parser("remove-agent", help="Remove a persona from a workspace roster")
    workspace_remove_agent.add_argument("workspace_id")
    workspace_remove_agent.add_argument("persona_id")
    _add_stage42_global_args(workspace_remove_agent, mutation=True)
    workspace_remove_agent.set_defaults(func=_cmd_workspace_remove_agent)
    workspace_rename = workspace_subs.add_parser("rename", help="Rename a workspace")
    workspace_rename.add_argument("workspace_id")
    workspace_rename.add_argument("name")
    _add_stage42_global_args(workspace_rename, mutation=True)
    workspace_rename.set_defaults(func=_cmd_workspace_rename)
    workspace_archive = workspace_subs.add_parser("archive", help="Archive a workspace")
    workspace_archive.add_argument("workspace_id")
    _add_stage42_global_args(workspace_archive, mutation=True)
    workspace_archive.set_defaults(func=_cmd_workspace_archive)

    realm = subs.add_parser("realm", help="Manage Harness realms")
    realm_subs = realm.add_subparsers(dest="realm_command", required=True)
    realm_list = realm_subs.add_parser("list", help="List realms")
    _add_stage42_global_args(realm_list)
    realm_list.set_defaults(func=_cmd_realm_list)
    realm_show = realm_subs.add_parser("show", help="Show one realm")
    realm_show.add_argument("realm_id")
    _add_stage42_global_args(realm_show)
    realm_show.set_defaults(func=_cmd_realm_show)
    realm_create = realm_subs.add_parser("create", help="Create a realm")
    realm_create.add_argument("--name", required=True)
    realm_create.add_argument("--server", default=None)
    _add_stage42_global_args(realm_create, mutation=True)
    realm_create.set_defaults(func=_cmd_realm_create)
    realm_adopt = realm_subs.add_parser("adopt", help="Adopt server-granted realms from the Eternia backend")
    realm_adopt.add_argument("--server", default=None, help="Only adopt realms bound to this Eternia server id")
    realm_adopt.add_argument("--credential-file", default=None, help="Launcher-brokered realm sync credential JSON (fallback: HERMES_REALM_SYNC_CREDENTIAL)")
    _add_stage42_global_args(realm_adopt, mutation=True)
    realm_adopt.set_defaults(func=_cmd_realm_adopt)
    realm_bind = realm_subs.add_parser("bind-server", help="Bind a realm to an Eternia server id")
    realm_bind.add_argument("realm_id")
    realm_bind.add_argument("server_id")
    _add_stage42_global_args(realm_bind, mutation=True)
    realm_bind.set_defaults(func=_cmd_realm_bind_server)
    realm_use = realm_subs.add_parser("use", help="Set active realm")
    realm_use.add_argument("realm_id")
    realm_use.add_argument(
        "--issued-at",
        dest="issued_at",
        default=None,
        help="ISO-8601 UTC instant the operator issued this switch; a pointer already owned by a strictly newer intent rejects this one as superseded (transport replay guard)",
    )
    _add_stage42_global_args(realm_use, mutation=True)
    realm_use.set_defaults(func=_cmd_realm_use)
    realm_sync = realm_subs.add_parser("sync", help="Git-backed selective sync for non-source-controlled realm artifacts")
    realm_sync_subs = realm_sync.add_subparsers(dest="realm_sync_command", required=True)
    realm_sync_status_cmd = realm_sync_subs.add_parser("status", help="Show realm sync state")
    realm_sync_status_cmd.add_argument("realm_id")
    realm_sync_status_cmd.add_argument("--credential-file", default=None, help="Launcher-brokered realm sync credential JSON (fallback: HERMES_REALM_SYNC_CREDENTIAL)")
    _add_stage42_global_args(realm_sync_status_cmd)
    realm_sync_status_cmd.set_defaults(func=_cmd_realm_sync_status)
    realm_sync_pull = realm_sync_subs.add_parser("pull", help="Pull and materialize realm sync artifacts")
    realm_sync_pull.add_argument("realm_id")
    realm_sync_pull.add_argument("--credential-file", default=None, help="Launcher-brokered realm sync credential JSON (fallback: HERMES_REALM_SYNC_CREDENTIAL)")
    _add_stage42_global_args(realm_sync_pull, mutation=True)
    realm_sync_pull.set_defaults(func=_cmd_realm_sync_pull)
    realm_sync_publish = realm_sync_subs.add_parser("publish", help="Publish allowlisted realm sync artifacts")
    realm_sync_publish.add_argument("realm_id")
    realm_sync_publish.add_argument("--credential-file", default=None, help="Launcher-brokered realm sync credential JSON (fallback: HERMES_REALM_SYNC_CREDENTIAL)")
    _add_stage42_global_args(realm_sync_publish, mutation=True)
    realm_sync_publish.set_defaults(func=_cmd_realm_sync_publish)

    playground = subs.add_parser("playground", help="Replay captured contract-failure scenarios against current contracts")
    playground_subs = playground.add_subparsers(dest="playground_command", required=True)
    playground_list = playground_subs.add_parser("list", help="List captured replay scenarios")
    playground_list.add_argument("--json", action="store_true")
    playground_list.set_defaults(func=_cmd_playground_list)
    playground_show = playground_subs.add_parser("show", help="Show one replay scenario")
    playground_show.add_argument("scenario_id")
    playground_show.add_argument("--json", action="store_true")
    playground_show.set_defaults(func=_cmd_playground_show)
    playground_replay = playground_subs.add_parser("replay", help="Replay one scenario (or all) against current contracts; mutates nothing")
    playground_replay.add_argument("scenario_id", nargs="?", default=None, help="Scenario id; omit to replay all")
    playground_replay.add_argument("--json", action="store_true")
    playground_replay.set_defaults(func=_cmd_playground_replay)

    swarm = subs.add_parser("swarm", help="Manage production swarm gate and runtime state")
    swarm_subs = swarm.add_subparsers(dest="swarm_command", required=True)
    swarm_status = swarm_subs.add_parser("status", help="Show swarm certification and enablement status")
    swarm_status.add_argument("--json", action="store_true")
    swarm_status.set_defaults(func=_cmd_swarm_status)
    swarm_enable = swarm_subs.add_parser("enable", help="Enable production swarm mode after certification")
    swarm_enable.add_argument("--lanes", type=int, default=2)
    swarm_enable.add_argument("--allow-uncertified-dev-swarm", action="store_true")
    swarm_enable.add_argument("--json", action="store_true")
    swarm_enable.set_defaults(func=_cmd_swarm_enable)
    swarm_disable = swarm_subs.add_parser("disable", help="Disable new production swarm activation")
    swarm_disable.add_argument("--json", action="store_true")
    swarm_disable.set_defaults(func=_cmd_swarm_disable)

    lane = subs.add_parser("lane", help="Inspect and operate persisted swarm lanes")
    lane_subs = lane.add_subparsers(dest="lane_command", required=True)
    lane_list = lane_subs.add_parser("list", help="List lanes")
    lane_list.add_argument("--json", action="store_true")
    _add_stage42_global_args(lane_list)
    lane_list.set_defaults(func=_cmd_lane_list)
    lane_show = lane_subs.add_parser("show", help="Show one lane")
    lane_show.add_argument("lane_id")
    lane_show.add_argument("--json", action="store_true")
    _add_stage42_global_args(lane_show)
    lane_show.set_defaults(func=_cmd_lane_show)
    for command_name in ("pause", "park", "resume", "drain"):
        command = lane_subs.add_parser(command_name, help=f"{command_name.title()} a lane")
        command.add_argument("lane_id")
        command.add_argument("--reason", default=f"operator {command_name}")
        command.add_argument("--json", action="store_true")
        command.set_defaults(func=_cmd_lane_control)

    tick = subs.add_parser("tick", help="Diagnostic only: run one harness tick")
    tick.add_argument("--task", dest="task_id", default=None, help="Run one tick for a specific task id")
    tick.add_argument("--json", action="store_true")
    tick.set_defaults(func=_cmd_tick)

    settle = subs.add_parser("run-until-settled", help="Diagnostic only: run bounded mission ticks until done, blocked, waiting, or incident")
    settle.add_argument("--task", dest="task_id", default=None, help="Settle a specific task id")
    settle.add_argument("--max-actions", type=int, default=10)
    settle.add_argument("--max-seconds", type=float, default=None)
    settle.add_argument("--json", action="store_true")
    settle.set_defaults(func=_cmd_run_until_settled)

    burn = subs.add_parser("burn-in", help="Run Stage 47 certification burn-in cases")
    burn_subs = burn.add_subparsers(dest="burn_in_command")
    burn_create = burn_subs.add_parser("create", help="Create a burn-in ledger")
    burn_create.add_argument("--suite", default=STAGE47_SUITE)
    burn_create.add_argument("--case-id", choices=sorted(STAGE47_CASES), default=None)
    burn_create.add_argument("--rerun-of", default=None)
    burn_create.add_argument("--json", action="store_true")
    burn_create.set_defaults(func=_cmd_burn_in_create)
    burn_run = burn_subs.add_parser("run", help="Run a burn-in case")
    burn_run.add_argument("case_id", choices=sorted(STAGE47_CASES))
    burn_run.add_argument("--burn-id", default=None)
    burn_run.add_argument("--max-actions", type=int, default=12)
    burn_run.add_argument("--json", action="store_true")
    burn_run.set_defaults(func=_cmd_burn_in_run)
    burn_status = burn_subs.add_parser("status", help="Show burn-in ledger status")
    burn_status.add_argument("burn_id")
    burn_status.add_argument("--json", action="store_true")
    burn_status.set_defaults(func=_cmd_burn_in_status)
    burn_summary = burn_subs.add_parser("summarize", help="Summarize burn-in certification evidence")
    burn_summary.add_argument("burn_id")
    burn_summary.add_argument("--json", action="store_true")
    burn_summary.set_defaults(func=_cmd_burn_in_summarize)

    run = subs.add_parser("run", help="Manage harness runs")
    run_subs = run.add_subparsers(dest="run_command")
    run_show = run_subs.add_parser("show", help="Show one harness run with task-scoped proof/event context")
    run_show.add_argument("run_id")
    run_show.add_argument("--events", type=int, default=25)
    run_show.add_argument("--json", action="store_true")
    run_show.set_defaults(func=_cmd_run_show)
    run_cancel = run_subs.add_parser("cancel", help="Cancel a harness run")
    run_cancel.add_argument("run_id")
    run_cancel.add_argument("--reason", required=True)
    run_cancel.add_argument("--requested-by", default="cli")
    _add_coordinator_permission_args(run_cancel)
    run_cancel.add_argument("--json", action="store_true")
    run_cancel.set_defaults(func=_cmd_run_cancel)
    run_approve = run_subs.add_parser("approve", help="Approve a waiting run to continue the same session")
    run_approve.add_argument("run_id")
    run_approve.add_argument("--json", action="store_true")
    run_approve.set_defaults(func=_cmd_run_approve)

    worker = subs.add_parser("worker", help="Inspect and steer durable Harness worker sessions")
    worker_subs = worker.add_subparsers(dest="worker_command")
    worker_list = worker_subs.add_parser("list", help="List worker sessions")
    worker_list.add_argument("--task", dest="task_id", default=None)
    worker_list.add_argument("--persona", dest="persona_id", default=None)
    worker_list.add_argument("--active", action="store_true")
    worker_list.add_argument("--json", action="store_true")
    _add_stage42_global_args(worker_list)
    worker_list.set_defaults(func=_cmd_worker_list)
    worker_show = worker_subs.add_parser("show", help="Show one worker session")
    worker_show.add_argument("worker_session_id")
    worker_show.add_argument("--json", action="store_true")
    _add_stage42_global_args(worker_show)
    worker_show.set_defaults(func=_cmd_worker_show)
    for command_name in ("pause", "resume", "interrupt", "nudge", "possess", "release"):
        command = worker_subs.add_parser(command_name, help=f"{command_name.title()} a worker session")
        command.add_argument("worker_session_id")
        command.add_argument("--reason", default="")
        command.add_argument("--note", default="")
        command.add_argument("--actor", default="cli")
        command.add_argument("--lease-seconds", type=int, default=900)
        command.add_argument("--json", action="store_true")
        command.set_defaults(func=_cmd_worker_control)
    worker_takeover = worker_subs.add_parser("takeover", help="Audited human takeover: freeze peers, possess worker, and optionally cancel active run with approval")
    worker_takeover.add_argument("worker_session_id")
    worker_takeover.add_argument("--reason", default="operator takeover")
    worker_takeover.add_argument("--actor", default="operator")
    worker_takeover.add_argument("--lease-seconds", type=int, default=900)
    worker_takeover.add_argument("--cancel-active-run", action="store_true")
    worker_takeover.add_argument("--approve-destructive", action="store_true")
    worker_takeover.add_argument("--json", action="store_true")
    worker_takeover.set_defaults(func=_cmd_worker_control)

    persona = subs.add_parser("persona", help="Run bounded live-token diagnostics for one persona")
    persona_subs = persona.add_subparsers(dest="persona_command")
    persona_list = persona_subs.add_parser("list", help="List durable persona instances")
    persona_list.add_argument("--json", action="store_true")
    persona_list.set_defaults(func=_cmd_persona_list)
    persona_show = persona_subs.add_parser("show", help="Show one durable persona instance")
    persona_show.add_argument("persona_id_or_instance_id")
    persona_show.add_argument("--json", action="store_true")
    persona_show.set_defaults(func=_cmd_persona_show)
    persona_tool_diff = persona_subs.add_parser("tool-diff", help="Show resolved model tools and blocked tools for one persona")
    persona_tool_diff.add_argument("persona_id", help="Persona id or alias: neko, dev, launcher-dev, backend-dev, qa")
    persona_tool_diff.add_argument("--session-id", default=None)
    persona_tool_diff.add_argument("--task", dest="task_id", default=None)
    persona_tool_diff.add_argument("--goal", dest="goal_id", default=None)
    persona_tool_diff.add_argument("--permission-mode", default="profile_default")
    persona_tool_diff.add_argument("--repo-scope", default=None)
    persona_tool_diff.add_argument("--workdir", default=None)
    persona_tool_diff.add_argument("--json", action="store_true")
    persona_tool_diff.set_defaults(func=_cmd_persona_tool_diff)
    persona_permission = persona_subs.add_parser("permission", help="Preview or set chat-scoped persona tool permissions")
    persona_permission_subs = persona_permission.add_subparsers(dest="persona_permission_command")
    persona_permission_set = persona_permission_subs.add_parser("set", help="Set chat-scoped permission mode")
    persona_permission_set.add_argument("persona_id", help="Persona id or alias: neko, dev, launcher-dev, backend-dev, qa")
    persona_permission_set.add_argument("--session-id", required=True)
    persona_permission_set.add_argument("--mode", choices=["profile_default", "read_only", "unbounded"], required=True)
    persona_permission_set.add_argument("--reason", required=True)
    persona_permission_set.add_argument("--turns", type=int, default=None)
    persona_permission_set.add_argument("--ttl-seconds", type=int, default=None)
    persona_permission_set.add_argument("--expires-at", default=None)
    persona_permission_set.add_argument("--json", action="store_true")
    persona_permission_set.set_defaults(func=_cmd_persona_permission_set)
    persona_assignments = persona_subs.add_parser("assignments", help="List persona assignments")
    persona_assignments.add_argument("--persona", dest="persona_id", default=None)
    persona_assignments.add_argument("--goal", dest="goal_id", default=None)
    persona_assignments.add_argument("--task", dest="task_id", default=None, help="Deprecated alias for --goal")
    persona_assignments.add_argument("--json", action="store_true")
    persona_assignments.set_defaults(func=_cmd_persona_assignments)
    persona_message = persona_subs.add_parser("message", help="Queue a bounded operator message assignment for one persona")
    persona_message.add_argument("persona_id", help="Persona id or alias: neko, dev, launcher-dev, backend-dev, qa")
    persona_message.add_argument("--task", dest="task_id", required=True)
    persona_message.add_argument("--message", required=True)
    persona_message.add_argument("--title", default="Operator message")
    persona_message.add_argument("--requested-by", default="cli")
    persona_message.add_argument("--json", action="store_true")
    persona_message.set_defaults(func=_cmd_persona_message)
    persona_diagnose = persona_subs.add_parser("diagnose", help="Create a diagnostic task and run exactly one bounded persona turn")
    persona_diagnose.add_argument("persona_id", help="Persona id or alias: neko, dev, launcher-dev, backend-dev, qa")
    persona_diagnose.add_argument("--title", required=True)
    persona_diagnose.add_argument("--message", required=True)
    persona_diagnose.add_argument("--requested-by", default="cli")
    persona_diagnose.add_argument("--operation-kind", default="diagnostic")
    persona_diagnose.add_argument("--operation-mode", default="standalone_task")
    persona_diagnose.add_argument("--max-actions", type=int, default=1)
    persona_diagnose.add_argument("--max-seconds", type=float, default=240.0)
    persona_diagnose.add_argument("--affected-repo", action="append", default=[])
    persona_diagnose.add_argument("--acceptance", action="append", default=[])
    persona_diagnose.add_argument("--non-goal", action="append", default=[])
    persona_diagnose.add_argument(
        "--keep-task",
        action="store_true",
        help="Preserve the standalone diagnostic task in the live runtime instead of auto-archiving it. "
        "By default a diagnostic auto-archives on completion so throwaway probes do not accumulate and gate the scheduler.",
    )
    persona_diagnose.add_argument("--json", action="store_true")
    persona_diagnose.set_defaults(func=_cmd_persona_diagnose)

    persona_chat = persona_subs.add_parser("chat", help="Manage durable persona chat sessions")
    persona_chat_subs = persona_chat.add_subparsers(dest="persona_chat_command")
    persona_chat_delete = persona_chat_subs.add_parser("delete", help="Delete a persona chat session and clear active persona bindings")
    persona_chat_delete.add_argument("--session-id", required=True)
    persona_chat_delete.add_argument("--persona", dest="persona_id", default=None)
    persona_chat_delete.add_argument("--persona-instance-id", default=None)
    persona_chat_delete.add_argument("--requested-by", default="cli")
    persona_chat_delete.add_argument("--json", action="store_true")
    persona_chat_delete.set_defaults(func=_cmd_persona_chat_delete)

    persona_set_model = persona_subs.add_parser("set-model", help="Persist a persona's default provider/model (profile-default lane; future instances inherit it)")
    persona_set_model.add_argument("persona_id", help="Persona id, alias, or profile:<name>")
    persona_set_model.add_argument("--provider", default=None, help="Provider lane (canonical name or alias; api_mode is derived from it)")
    persona_set_model.add_argument("--model", default=None, help="Model id for the provider lane")
    persona_set_model.add_argument("--use-default", action="store_true", help="Clear the persona's model/provider/api_mode so the runtime default cascade applies")
    persona_set_model.add_argument("--issued-at", default=None, help="ISO-8601 issue timestamp; stale writes are superseded instead of applied")
    persona_set_model.add_argument("--requested-by", default="operator")
    persona_set_model.add_argument("--json", action="store_true")
    persona_set_model.set_defaults(func=_cmd_persona_set_model)
    persona_instance = persona_subs.add_parser("instance", help="Create, message, run, close, and archive free-floating persona instances")
    persona_instance_subs = persona_instance.add_subparsers(dest="persona_instance_command")
    persona_instance_create = persona_instance_subs.add_parser("create", help="Create an Agent Profile or queue a free-floating persona assignment")
    persona_instance_create.add_argument("--persona", dest="persona_id", required=True)
    persona_instance_create.add_argument("--title", required=True)
    persona_instance_create.add_argument("--message", required=True)
    persona_instance_create.add_argument("--requested-by", default="cli")
    _add_coordinator_permission_args(persona_instance_create)
    persona_instance_create.add_argument("--client-message-id", default=None)
    persona_instance_create.add_argument("--display-name", default=None)
    persona_instance_create.add_argument("--session-id", default=None)
    persona_instance_create.add_argument("--kill-active", action="store_true", help="Cancel the current run/worker before replacing the active chat")
    persona_instance_create.add_argument("--add-instance", action="store_true", help="Create an additional placement-backed instance instead of targeting the primary placement")
    persona_instance_create.add_argument("--placement-id", default=None, help="Scene itemId for an additional placement-backed instance")
    persona_instance_create.add_argument("--auto-run", action="store_true", help="Immediately run one bounded chat turn after queuing the message")
    persona_instance_create.add_argument("--stream", action="store_true", help="Emit operator-chat deltas and the final payload as NDJSON")
    persona_instance_create.add_argument("--max-actions", type=int, default=1)
    persona_instance_create.add_argument("--max-seconds", type=float, default=240.0)
    persona_instance_create.add_argument("--json", action="store_true")
    persona_instance_create.set_defaults(func=_cmd_persona_instance_create)
    persona_instance_open = persona_instance_subs.add_parser("open-chat", help="Bind a persona instance to a durable chat session without ticking")
    persona_instance_open.add_argument("--persona", dest="persona_id", required=True)
    persona_instance_open.add_argument("--session-id", default=None)
    persona_instance_open.add_argument("--kill-active", action="store_true", help="Cancel the current run/worker before replacing the active chat")
    persona_instance_open.add_argument("--add-instance", action="store_true", help="Open the chat on an additional placement-backed instance")
    persona_instance_open.add_argument("--placement-id", default=None, help="Scene itemId for an additional placement-backed instance")
    persona_instance_open.add_argument("--requested-by", default="cli")
    _add_coordinator_permission_args(persona_instance_open)
    persona_instance_open.add_argument("--json", action="store_true")
    persona_instance_open.set_defaults(func=_cmd_persona_instance_open_chat)
    persona_instance_message = persona_instance_subs.add_parser("message", help="Queue a message to a free-floating persona instance without ticking")
    persona_instance_message.add_argument("persona_instance_id")
    persona_instance_message.add_argument("--message", required=True)
    persona_instance_message.add_argument("--title", default="Free-floating operator message")
    persona_instance_message.add_argument("--requested-by", default="cli")
    persona_instance_message.add_argument("--client-message-id", default=None)
    persona_instance_message.add_argument("--session-id", default=None)
    persona_instance_message.add_argument("--auto-run", action="store_true", help="Immediately run one bounded chat turn after queuing the message")
    persona_instance_message.add_argument("--stream", action="store_true", help="Emit operator-chat deltas and the final payload as NDJSON")
    persona_instance_message.add_argument("--max-actions", type=int, default=1)
    persona_instance_message.add_argument("--max-seconds", type=float, default=240.0)
    persona_instance_message.add_argument("--json", action="store_true")
    persona_instance_message.set_defaults(func=_cmd_persona_instance_message)
    persona_instance_run_once = persona_instance_subs.add_parser("run-once", help="Run one bounded sandbox turn for a free-floating persona instance")
    persona_instance_run_once.add_argument("persona_instance_id")
    persona_instance_run_once.add_argument("--title", default="Free-floating persona run")
    persona_instance_run_once.add_argument("--message", default=None)
    persona_instance_run_once.add_argument("--requested-by", default="cli")
    persona_instance_run_once.add_argument("--max-actions", type=int, default=1)
    persona_instance_run_once.add_argument("--max-seconds", type=float, default=240.0)
    persona_instance_run_once.add_argument("--json", action="store_true")
    persona_instance_run_once.set_defaults(func=_cmd_persona_instance_run_once)
    persona_instance_close = persona_instance_subs.add_parser("close", help="Close active free-floating assignments for one persona instance")
    persona_instance_close.add_argument("persona_instance_id")
    persona_instance_close.add_argument("--reason", required=True)
    persona_instance_close.add_argument("--requested-by", default="cli")
    _add_coordinator_permission_args(persona_instance_close)
    persona_instance_close.add_argument("--json", action="store_true")
    persona_instance_close.set_defaults(func=_cmd_persona_instance_close)
    persona_instance_archive = persona_instance_subs.add_parser("archive", help="Archive active free-floating assignments for one persona instance")
    persona_instance_archive.add_argument("persona_instance_id")
    persona_instance_archive.add_argument("--reason", default="archived free-floating persona assignment")
    persona_instance_archive.add_argument("--requested-by", default="cli")
    persona_instance_archive.add_argument("--json", action="store_true")
    persona_instance_archive.set_defaults(func=_cmd_persona_instance_archive)
    persona_instance_sweep = persona_instance_subs.add_parser("sweep-orphans", help="Reap stale task-bound persona instances with no live worker/run")
    persona_instance_sweep.add_argument("--reason", default="operator persona instance janitor")
    persona_instance_sweep.add_argument("--json", action="store_true")
    persona_instance_sweep.set_defaults(func=_cmd_persona_instance_sweep_orphans)
    persona_instance_steer = persona_instance_subs.add_parser("steer", help="Re-route a persona instance's living-graph wiring (Stage 77 steering edge)")
    persona_instance_steer.add_argument("persona_instance_id")
    persona_instance_steer.add_argument("--parent", dest="parent_instance_id", default=None, help="Owner/coordinator instance id that steers this sub-agent")
    persona_instance_steer.add_argument("--goal", dest="goal_id", default=None, help="Goal/task id this sub-agent inherits from its parent container")
    persona_instance_steer.add_argument("--detach", action="store_true", help="Detach from any parent and goal (becomes a standalone owner)")
    persona_instance_steer.add_argument("--requested-by", default="operator")
    _add_coordinator_permission_args(persona_instance_steer)
    persona_instance_steer.add_argument("--json", action="store_true")
    persona_instance_steer.set_defaults(func=_cmd_persona_instance_steer)
    persona_instance_return = persona_instance_subs.add_parser("return-summary", help="Post a bounded child summary back into a parent chat session")
    persona_instance_return.add_argument("persona_instance_id")
    persona_instance_return.add_argument("--parent-session-id", required=True)
    persona_instance_return.add_argument("--summary", required=True)
    persona_instance_return.add_argument("--proof-id", dest="proof_ids", action="append", default=[])
    persona_instance_return.add_argument("--artifact-ref", dest="artifact_refs", action="append", default=[])
    persona_instance_return.add_argument("--task", dest="task_id", default=None)
    persona_instance_return.add_argument("--stage", dest="stage_id", default=None)
    persona_instance_return.add_argument("--json", action="store_true")
    persona_instance_return.set_defaults(func=_cmd_persona_instance_return_summary)
    persona_instance_update = persona_instance_subs.add_parser("update-profile", help="Update runtime persona-instance profile overrides without editing the backing Hermes profile")
    persona_instance_update.add_argument("persona_instance_id")
    persona_instance_update.add_argument("--display-name", default=None)
    persona_instance_update.add_argument("--current-chat-goal", default=None)
    persona_instance_update.add_argument("--goal", dest="goal_id", default=None)
    persona_instance_update.add_argument("--skill", dest="skills", action="append", default=None)
    persona_instance_update.add_argument("--clear-skills", action="store_true")
    persona_instance_update.add_argument("--requested-by", default="operator")
    _add_coordinator_permission_args(persona_instance_update)
    persona_instance_update.add_argument("--json", action="store_true")
    persona_instance_update.set_defaults(func=_cmd_persona_instance_update_profile)
    persona_instance_set_model = persona_instance_subs.add_parser("set-model", help="Persist an instance-level provider/model override (this agent only; duplicates keep theirs)")
    persona_instance_set_model.add_argument("persona_instance_id")
    persona_instance_set_model.add_argument("--provider", default=None, help="Provider lane (canonical name or alias; api_mode is derived from it)")
    persona_instance_set_model.add_argument("--model", default=None, help="Model id for the provider lane")
    persona_instance_set_model.add_argument("--use-profile-default", action="store_true", help="Clear the instance override so the backing profile default applies live")
    persona_instance_set_model.add_argument("--issued-at", default=None, help="ISO-8601 issue timestamp; stale writes are superseded instead of applied")
    persona_instance_set_model.add_argument("--requested-by", default="operator")
    _add_coordinator_permission_args(persona_instance_set_model)
    persona_instance_set_model.add_argument("--json", action="store_true")
    persona_instance_set_model.set_defaults(func=_cmd_persona_instance_set_model)

    mission_chat = subs.add_parser("mission-chat", help="Canonical Mission Control chat path")
    mission_chat_subs = mission_chat.add_subparsers(dest="mission_chat_command")
    mission_chat_message = mission_chat_subs.add_parser("message", help="Send one Mission Control chat turn through the normal Hermes profile context")
    mission_chat_message.add_argument("--persona", dest="persona_id", required=True)
    mission_chat_message.add_argument("--persona-instance-id", default=None)
    mission_chat_message.add_argument("--session-id", default=None)
    mission_chat_message.add_argument("--task", dest="task_id", default=None)
    mission_chat_message.add_argument("--goal", dest="goal_id", default=None)
    mission_chat_message.add_argument("--title", default="Operator message")
    mission_chat_message.add_argument("--message", required=True)
    mission_chat_message.add_argument("--provider", default=None, help="Provider override for this persona chat session only")
    mission_chat_message.add_argument("--model", default=None, help="Model override for this persona chat session only")
    mission_chat_message.add_argument("--use-agent-default", action="store_true", help="Clear the chat-scoped provider/model override before sending")
    mission_chat_message.add_argument("--surface-prompt", default="")
    mission_chat_message.add_argument("--intent-hint", default="chat")
    mission_chat_message.add_argument("--requested-by", default="cli")
    mission_chat_message.add_argument("--client-message-id", default=None)
    mission_chat_message.add_argument("--stream", action="store_true", help="Emit operator-chat deltas and the final payload as NDJSON")
    mission_chat_message.add_argument("--max-seconds", type=float, default=240.0)
    mission_chat_message.add_argument("--relay-chain", default=None, help="Comma-separated canonical persona ids already on the agent-relay chain (envelope provenance for chained agent_chat_send hops)")
    mission_chat_message.add_argument("--relay-deadline-epoch", type=float, default=None, help="Absolute unix-epoch deadline shared by every hop on the relay chain")
    mission_chat_message.add_argument("--json", action="store_true")
    mission_chat_message.set_defaults(func=_cmd_mission_chat_message)
    mission_chat_queue_skill = mission_chat_subs.add_parser("queue-skill", help="Load a skill on the next Mission Control chat turn")
    mission_chat_queue_skill.add_argument("--persona", dest="persona_id", required=True)
    mission_chat_queue_skill.add_argument("--persona-instance-id", default=None)
    mission_chat_queue_skill.add_argument("--session-id", required=True)
    mission_chat_queue_skill.add_argument("--skill", required=True)
    mission_chat_queue_skill.add_argument("--json", action="store_true")
    mission_chat_queue_skill.set_defaults(func=_cmd_mission_chat_queue_skill)
    mission_chat_steer = mission_chat_subs.add_parser("steer", help="Steer an active streamed Mission Control chat turn")
    mission_chat_steer.add_argument("--session-id", required=True)
    mission_chat_steer.add_argument("--message", required=True)
    mission_chat_steer.add_argument("--client-message-id", required=True)
    mission_chat_steer.add_argument("--persona", dest="persona_id", default=None)
    mission_chat_steer.add_argument("--persona-instance-id", default=None)
    mission_chat_steer.add_argument("--json", action="store_true")
    mission_chat_steer.set_defaults(func=_cmd_mission_chat_steer)

    status = subs.add_parser("status", help="Show harness status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_cmd_status)

    doctor = subs.add_parser("doctor", help="Show Harness runtime diagnostics and stale-state report")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--fix", action="store_true", help="Repair stale Harness runtime rows and reap orphan worktrees")
    doctor.add_argument("--dry-run", action="store_true", help="Preview --fix repairs without mutating runtime state")
    doctor.add_argument("--yes", "-y", action="store_true", help="Confirm --fix repairs")
    doctor.add_argument("--stale-run-hours", type=int, default=DEFAULT_STALE_RUN_HOURS)
    doctor.add_argument("--stale-worker-hours", type=int, default=DEFAULT_STALE_WORKER_HOURS)
    doctor.add_argument("--stale-task-days", type=int, default=DEFAULT_STALE_TASK_DAYS)
    doctor.add_argument("--stale-incident-days", type=int, default=DEFAULT_STALE_INCIDENT_DAYS)
    doctor.add_argument("--stale-incident-hours", type=int, default=None, help="Compatibility override for sub-day incident sweeps")
    doctor.add_argument("--worktree-min-age-seconds", type=int, default=DEFAULT_WORKTREE_MIN_AGE_SECONDS)
    doctor.add_argument("--compact-events", action="store_true", help="Compact archived task rows out of events.jsonl; use with --fix --dry-run to preview")
    doctor.set_defaults(func=_cmd_doctor)

    health = subs.add_parser("health", help="Check Harness runtime/provider dependencies before live ticks")
    health.add_argument("--json", action="store_true")
    health.set_defaults(func=_cmd_health)

    verify = subs.add_parser("verify", help="Run Mission Control proof-packet verification")
    verify.add_argument("--json", action="store_true")
    verify.add_argument("--mode", choices=["live-tony", "ci", "temp-root"], default="ci")
    verify.add_argument("--skip-tests", action="store_true")
    verify.set_defaults(func=_cmd_verify)

    config = subs.add_parser("config", help="Inspect Harness runtime config")
    config_subs = config.add_subparsers(dest="config_command")
    config_show = config_subs.add_parser("show", help="Show effective Harness runtime config")
    config_show.add_argument("--json", action="store_true")
    config_show.set_defaults(func=_cmd_config)

    migrate = subs.add_parser("migrate", help="Inspect Harness runtime migrations")
    migrate.add_argument("--check", action="store_true", help="Report pending migrations without modifying data")
    migrate.add_argument("--json", action="store_true")
    migrate.set_defaults(func=_cmd_migrate)

    observe = subs.add_parser("observe", help="Show redaction-safe Mission Control observability")
    observe.add_argument("--json", action="store_true")
    observe.set_defaults(func=_cmd_observe)

    contracts = subs.add_parser("contracts", help="Inspect canonical AgentDecision and Mission Control event contracts")
    contracts_subs = contracts.add_subparsers(dest="contracts_command")
    contracts_dump = contracts_subs.add_parser("dump", help="Dump redaction-safe contract registry")
    contracts_dump.add_argument("--role", default=None)
    contracts_dump.add_argument("--decision", default=None)
    contracts_dump.add_argument("--json", action="store_true")
    contracts_dump.set_defaults(func=_cmd_contracts_dump)
    contracts_verify = contracts_subs.add_parser("verify-examples", help="Verify the contract registry is internally consistent")
    contracts_verify.add_argument("--json", action="store_true")
    contracts_verify.set_defaults(func=_cmd_contracts_verify_examples)

    daemon = subs.add_parser("daemon", help="Run or inspect the Mission Daemon")
    daemon.add_argument("--foreground", action="store_true", help="Run the daemon loop in the foreground")
    daemon.add_argument("--interval", type=float, default=None)
    daemon.add_argument("--idle-interval", type=float, default=None)
    daemon.add_argument("--task", default=None, help="Drive one foreground task id")
    daemon.add_argument("--max-loops", type=int, default=None, help=argparse.SUPPRESS)
    daemon.add_argument("--json", action="store_true")
    daemon_subs = daemon.add_subparsers(dest="daemon_command")
    daemon_start = daemon_subs.add_parser("start", help="Start the Mission Daemon in the background")
    daemon_start.add_argument("--interval", type=float, default=None)
    daemon_start.add_argument("--idle-interval", type=float, default=None)
    daemon_start.add_argument("--task", default=None, help="Drive one foreground task id")
    daemon_start.add_argument("--json", action="store_true")
    daemon_status = daemon_subs.add_parser("status", help="Show Mission Daemon status")
    daemon_status.add_argument("--json", action="store_true")
    daemon_stop = daemon_subs.add_parser("stop", help="Stop the Mission Daemon")
    daemon_stop.add_argument("--json", action="store_true")
    daemon_foreground = daemon_subs.add_parser("foreground", help="Run the Mission Daemon loop in the foreground")
    daemon_foreground.add_argument("--interval", type=float, default=None)
    daemon_foreground.add_argument("--idle-interval", type=float, default=None)
    daemon_foreground.add_argument("--task", default=None, help="Drive one foreground task id")
    daemon_foreground.add_argument("--max-loops", type=int, default=None, help=argparse.SUPPRESS)
    daemon_foreground.add_argument("--json", action="store_true")
    daemon_run_once = daemon_subs.add_parser("run-once", help="Run one bounded Mission Daemon loop and exit")
    daemon_run_once.add_argument("--task", default=None, help="Drive one foreground task id")
    daemon_run_once.add_argument("--json", action="store_true")
    daemon.set_defaults(func=_cmd_daemon)
    daemon_start.set_defaults(func=_cmd_daemon)
    daemon_status.set_defaults(func=_cmd_daemon)
    daemon_stop.set_defaults(func=_cmd_daemon)
    daemon_foreground.set_defaults(func=_cmd_daemon)
    daemon_run_once.set_defaults(func=_cmd_daemon)

    worktree = subs.add_parser("worktree", help="Manage harness-managed git worktrees")
    worktree_subs = worktree.add_subparsers(dest="worktree_command", required=True)
    worktree_reap = worktree_subs.add_parser(
        "reap",
        help="Capture-then-reap orphan worktrees not owned by any open task run",
    )
    worktree_reap.add_argument("--min-age-seconds", type=int, default=3600)
    worktree_reap.add_argument("--json", action="store_true")
    worktree_reap.set_defaults(func=_cmd_worktree_reap)

    agent = subs.add_parser("agent", help="List harness agent definitions")
    agent_subs = agent.add_subparsers(dest="agent_command", required=True)
    agent_list = agent_subs.add_parser("list", help="List persisted/configured agent definitions")
    agent_list.add_argument("--all-profiles", action="store_true")
    _add_stage42_global_args(agent_list)
    agent_list.set_defaults(func=_cmd_agent_list)

    agents = subs.add_parser("agents", help="Deprecated alias for `agent list`")
    agents.add_argument("--all-profiles", action="store_true")
    agents.add_argument("--json", action="store_true")
    agents.set_defaults(func=_cmd_agent_list)

    skills = subs.add_parser("install-harness-skills", help="Install versioned Harness skills into configured persona profiles")
    skills.add_argument("--active-profile-only", action="store_true", help="Install all Harness skills only into the active Hermes profile")
    skills.add_argument("--all-persona-profiles", action="store_true", help="Compatibility flag; persona profiles are now the default")
    skills.add_argument("--json", action="store_true")
    skills.set_defaults(func=_cmd_install_harness_skills)

    smoke = subs.add_parser("smoke", help="Run a safe Mission Control smoke goal")
    smoke.add_argument("--json", action="store_true")
    smoke.add_argument("--temp-root", action="store_true", default=False)
    smoke.add_argument("--no-model", action="store_true", default=False)
    smoke.set_defaults(func=_cmd_smoke)

    proof = subs.add_parser("proof", help="Manage proof records")
    proof_subs = proof.add_subparsers(dest="proof_command")
    proof_list = proof_subs.add_parser("list")
    proof_list.add_argument("task_id")
    proof_list.add_argument("--json", action="store_true")
    proof_list.set_defaults(func=_cmd_proof_list)

    issue = subs.add_parser("issue", help="Manage issue discoveries")
    issue_subs = issue.add_subparsers(dest="issue_command")
    issue_list = issue_subs.add_parser("list")
    issue_list.add_argument("--task-id", required=True)
    issue_list.add_argument("--json", action="store_true")
    issue_list.set_defaults(func=_cmd_issue_list)
    issue_show = issue_subs.add_parser("show")
    issue_show.add_argument("discovery_id")
    issue_show.add_argument("--json", action="store_true")
    issue_show.set_defaults(func=_cmd_issue_show)
    issue_triage = issue_subs.add_parser("triage")
    issue_triage.add_argument("discovery_id")
    issue_triage.add_argument("--decision", required=True, choices=["blocks_current", "same_scope", "fork_child", "defer", "escalate"])
    issue_triage.add_argument("--child-title", default="")
    issue_triage.add_argument("--child-description", default="")
    issue_triage.add_argument("--acceptance", action="append", default=[])
    issue_triage.add_argument("--rationale", default="Manual CLI triage")
    issue_triage.add_argument("--priority", default="medium")
    issue_triage.add_argument("--json", action="store_true")
    issue_triage.set_defaults(func=_cmd_issue_triage)

    inc = subs.add_parser("incident", help="Manage incidents")
    inc_subs = inc.add_subparsers(dest="incident_command")
    inc_list = inc_subs.add_parser("list")
    inc_list.add_argument("--open", action="store_true", help="(default) show only open incidents")
    inc_list.add_argument("--all", action="store_true", help="include closed incidents")
    inc_list.add_argument("--json", action="store_true")
    inc_list.set_defaults(func=_cmd_incident_list)
    inc_close = inc_subs.add_parser("close")
    inc_close.add_argument("incident_id")
    inc_close.add_argument("--reason", required=True)
    inc_close.add_argument("--json", action="store_true")
    inc_close.set_defaults(func=_cmd_incident_close)

    snap = subs.add_parser("snapshot", help="Write redaction-safe snapshot.json")
    snap.add_argument("--json", action="store_true")
    snap.set_defaults(func=_cmd_snapshot)
    stream = subs.add_parser("stream", help="Emit Mission Control hydrate/delta frames as NDJSON")
    stream.add_argument("--poll-interval", type=float, default=0.25)
    stream.add_argument("--heartbeat-interval", type=float, default=5.0)
    stream.add_argument("--max-frames", type=int, default=None, help=argparse.SUPPRESS)
    stream.set_defaults(func=_cmd_stream)
    serve = subs.add_parser("serve", help="Persistent NDJSON stdio bridge: dispatch harness argv requests in one warm process (Mission Control serve lane)")
    serve.add_argument("--ndjson", action="store_true", help="NDJSON frame transport over stdio (the only v1 transport)")
    serve.add_argument("--pool-size", type=int, default=4, help=argparse.SUPPRESS)
    serve.set_defaults(func=_cmd_serve)
    rebuild_read_model = subs.add_parser("rebuild-read-model", help="Rebuild read_model.db from the current event-sourced store")
    rebuild_read_model.add_argument("--json", action="store_true")
    rebuild_read_model.set_defaults(func=_cmd_rebuild_read_model)
    read_projection = subs.add_parser("read", help="Read one projection from read_model.db")
    read_projection.add_argument("--projection", required=True)
    read_projection.add_argument("--since-offset", type=int, default=None)
    read_projection.add_argument("--json", action="store_true")
    read_projection.set_defaults(func=_cmd_read_projection)

    pets = subs.add_parser("pets", help="Mission Control Petdex bridge")
    pets_subs = pets.add_subparsers(dest="pets_command", required=True)
    pets_gallery = pets_subs.add_parser("gallery", help="List Petdex pets for Launcher")
    pets_gallery.add_argument("--local-only", action="store_true", help="Only include installed pets; skip the remote manifest")
    pets_gallery.add_argument("--limit", type=int, default=0, help="Maximum remote rows; 0 = all")
    pets_gallery.add_argument("--query", default="", help="Filter by slug/display name substring")
    pets_gallery.add_argument("--json", action="store_true")
    pets_gallery.set_defaults(func=_cmd_pets_gallery)
    pets_install = pets_subs.add_parser("install", help="Install a Petdex pet by slug")
    pets_install.add_argument("slug")
    pets_install.add_argument("--force", action="store_true")
    pets_install.add_argument("--json", action="store_true")
    pets_install.set_defaults(func=_cmd_pets_install)
    pets_sprite = pets_subs.add_parser("sprite", help="Return an installed pet spritesheet payload")
    pets_sprite.add_argument("slug")
    pets_sprite.add_argument("--json", action="store_true")
    pets_sprite.set_defaults(func=_cmd_pets_sprite)
    pets_thumb = pets_subs.add_parser("thumb", help="Return a Petdex pet thumbnail")
    pets_thumb.add_argument("slug")
    pets_thumb.add_argument("--url", default="", help="Optional Petdex spritesheet URL for non-installed gallery pets")
    pets_thumb.add_argument("--json", action="store_true")
    pets_thumb.set_defaults(func=_cmd_pets_thumb)


def harness_command(args) -> int:
    print("Use `hermes harness --help`.")
    return 0


def emit_harness_error(exc: BaseException, *, args=None, code: str | None = None, message: str | None = None) -> int:
    error_code = code or _error_code_for_exception(exc)
    safe_details = {"error_class": type(exc).__name__}
    if isinstance(exc, RealmSyncError):
        safe_details.update(exc.safe_details)
    envelope = _error_envelope(
        error_code,
        message or _safe_error_message(exc),
        retryable=getattr(exc, "retryable", False) or error_code in {"runtime_unavailable", "daemon_offline", "timeout"},
        safe_details=safe_details,
    )
    _print_stage42(envelope, args=args, default_output="json")
    return ERROR_EXIT_CODES.get(error_code, 1)


def _error_code_for_exception(exc: BaseException) -> str:
    if isinstance(exc, NotFound):
        return "not_found"
    if isinstance(exc, AlreadyExists):
        return "already_exists"
    if isinstance(exc, ChatBusyError):
        return "agent_busy"
    if isinstance(exc, RealmSyncError):
        return exc.code
    # A persisted-entity file that does not exist on disk is a lookup miss,
    # not an internal error — map it to the not-found taxonomy (exit 3).
    if isinstance(exc, FileNotFoundError):
        return "not_found"
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_payload"
    # Typed AgentRuntimeError subclasses map to their precondition/integrity codes.
    for exc_type, code in (
        (InvalidTransition, "invalid_transition"),
        (StaleRun, "stale_run"),
        (ProofMissing, "proof_missing"),
        (StoreCorrupt, "store_corrupt"),
        (EventPayloadTooLarge, "event_payload_too_large"),
        (RuntimeRootMismatch, "wrong_runtime_root"),
    ):
        if isinstance(exc, exc_type):
            return code
    if isinstance(exc, ValueError):
        text = str(exc)
        if text in ERROR_EXIT_CODES:
            return text
        return "invalid_request"
    if isinstance(exc, AgentRuntimeError):
        return "internal_error"
    return "internal_error"


def _error_envelope(code: str, message: str, *, retryable: bool = False, safe_details: dict | None = None, hint: str | None = None, correlation_id: str | None = None) -> dict:
    return {
        "schema_version": STAGE42_SCHEMA_VERSION,
        "kind": "error",
        "error": {
            "code": code,
            "message": message,
            "hint": hint or _error_hint(code),
            "retryable": bool(retryable),
            "error_id": f"err_{uuid.uuid4().hex[:8]}",
            "correlation_id": correlation_id,
            "safe_details": safe_details or {},
        },
    }


def _error_hint(code: str) -> str:
    return {
        "confirmation_required": "Re-run with --yes after confirming the destructive operation.",
        "not_found": "Check the id with the matching list command.",
        "goal_not_found": "Run `hermes harness goal list --json` and retry with a listed id.",
        "workspace_not_found": "Run `hermes harness workspace list --json` and retry with a listed id.",
        "realm_not_found": "Run `hermes harness realm list --json` and retry with a listed id.",
        "sync_conflict": "Resolve conflicts in the realm sync git repo, then retry.",
        "sync_behind": "Run `hermes harness realm sync pull <realm> --json` before publishing.",
        "sync_secret_excluded": "Remove secrets/state from the realm sync allowlist source before retrying.",
        "sync_remote_unreachable": "Check network/git remote availability and retry.",
        "sync_auth_failed": "Provide a fresh launcher-brokered credential via --credential-file or HERMES_REALM_SYNC_CREDENTIAL.",
    }.get(code, "Inspect safe_details and retry after correcting the request.")


_ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|[\\/])[^\s\"']*[\\/][^\s\"']*")


def _redact_paths(text: str) -> str:
    """Replace absolute filesystem paths with their basename.

    The error contract forbids absolute paths in messages (they leak the
    runtime root). A bare `realm_nope.json` is enough for the operator.
    """

    def _basename(match: "re.Match[str]") -> str:
        token = match.group(0)
        return re.split(r"[\\/]", token)[-1] or token

    return _ABS_PATH_RE.sub(_basename, text)


def _safe_error_message(exc: BaseException) -> str:
    text = " ".join(str(exc or type(exc).__name__).split())
    text = _redact_paths(text)
    return text[:300] or type(exc).__name__


def _load_request_json(raw: str) -> dict:
    """Resolve a ``--request-json`` value to a parsed object.

    Accepts either a path to a JSON file or an inline JSON document. Inline
    JSON (or a malformed file) that fails to parse raises ``json.JSONDecodeError``
    which the CLI maps to ``invalid_payload`` (exit 2) — never the file-not-found
    ``internal_error`` the bare ``Path(...).read_text()`` produced.
    """
    candidate = (raw or "").strip()
    looks_inline = candidate[:1] in {"{", "["}
    if not looks_inline:
        try:
            path = Path(candidate)
            if path.is_file():
                candidate = path.read_text(encoding="utf-8")
        except OSError:
            # Not a usable path — fall through and parse the literal as JSON.
            pass
    return json.loads(candidate)


def _list_envelope(item_kind: str, items: list[dict], *, cursor: str | None = None, truncated: bool = False) -> dict:
    return {
        "schema_version": STAGE42_SCHEMA_VERSION,
        "kind": "list",
        "item_kind": item_kind,
        "count": len(items),
        "items": items,
        "cursor": cursor,
        "truncated": bool(truncated),
    }


def _object_envelope(kind: str, item: dict, *, warnings: list[dict] | None = None) -> dict:
    data = {"schema_version": STAGE42_SCHEMA_VERSION, "kind": kind, **item}
    if warnings:
        data["warnings"] = warnings
    return data


def _print_stage42(data: dict, *, args, default_output: str | None = None) -> None:
    output = "json" if getattr(args, "json", False) else (getattr(args, "output", None) or default_output or ("table" if sys.stdout.isatty() else "json"))
    data = _apply_fields(data, getattr(args, "fields", None))
    if getattr(args, "quiet", False):
        print(_quiet_output(data))
        return
    if output == "json":
        print(emit_json(data))
    elif output == "yaml":
        import yaml

        print(yaml.safe_dump(json.loads(emit_json(data)), sort_keys=False, allow_unicode=True))
    else:
        print(_table_output(data, wide=output == "wide"))


def _apply_fields(data: dict, fields_text: str | None) -> dict:
    if not fields_text:
        return data
    fields = [field.strip() for field in fields_text.split(",") if field.strip()]
    if data.get("kind") == "list":
        kept = []
        for item in data.get("items") or []:
            kept.append({key: item.get(key) for key in fields if key in item})
        return {**data, "items": kept}
    return {key: data.get(key) for key in ["schema_version", "kind", *fields] if key in data}


def _quiet_output(data: dict) -> str:
    if data.get("kind") == "list":
        return "\n".join(str(item.get("id") or item.get("task_id") or "") for item in data.get("items") or [] if item)
    return str(data.get("id") or data.get("task_id") or "")


def _table_output(data: dict, *, wide: bool = False) -> str:
    if data.get("kind") == "error":
        err = data.get("error") or {}
        return f"{err.get('code')}: {err.get('message')}"
    if data.get("kind") == "list":
        items = list(data.get("items") or [])
        if not items:
            return f"no {data.get('item_kind', 'items')}"
        keys = list(items[0].keys()) if wide else [key for key in ("id", "title", "name", "state", "workspace_id", "realm_id", "updated_at") if key in items[0]]
        return "\n".join("  ".join(str(item.get(key, "")) for key in keys) for item in items)
    keys = [key for key in ("id", "title", "name", "state", "workspace_id", "realm_id", "updated_at") if key in data]
    return "  ".join(str(data.get(key, "")) for key in keys) if keys else emit_json(data)


def _require_yes(args, code: str = "confirmation_required") -> bool:
    if getattr(args, "yes", False) or getattr(args, "dry_run", False):
        return True
    _print_stage42(
        _error_envelope(code, "This destructive operation requires --yes.", retryable=False),
        args=args,
        default_output="json",
    )
    return False


def _goal_row(task: Task, *, full: bool = False) -> dict:
    from agent_runtime.mission_plan import mission_plan_summary

    workspace_id = getattr(task, "workspace_id", None)
    realm_id = None
    if workspace_id:
        try:
            realm_id = WorkspaceStore().get(workspace_id).realm_id
        except Exception:
            realm_id = None
    plan = getattr(task, "mission_plan", None)
    stage_id = getattr(plan, "current_stage_id", None) if plan is not None else getattr(task, "current_stage_id", None)
    row = {
        "id": getattr(task, "goal_id", None) or task.id,
        "task_id": task.id,
        "title": task.title,
        "state": str(task.state),
        "workspace_id": workspace_id,
        "realm_id": realm_id,
        "stage": stage_id,
        "updated_at": task.updated_at,
    }
    if full:
        runs = RunStore().list_for_task(task.id)
        proofs = ProofStore().list_for_task(task.id)
        incidents = [item for item in IncidentStore().list_all() if item.task_id == task.id and item.closed_at is None]
        row.update(
            {
                "graph": mission_plan_summary(task),
                "run_ids": [run.id for run in runs],
                "proof_ids": [proof.id for proof in proofs],
                "open_incident_ids": [incident.id for incident in incidents],
            }
        )
    return row


def _archived_goal_row(task_id: str, result) -> dict | None:
    archived = _archived_task_summary(task_id)
    if not archived:
        return None
    task_data = archived.get("task") if isinstance(archived.get("task"), dict) else {}
    archived_task = archived.get("archived_task") if isinstance(archived.get("archived_task"), dict) else {}
    goal_id = task_data.get("goal_id") or task_id
    row = {
        "id": goal_id,
        "task_id": task_id,
        "title": task_data.get("title") or getattr(result, "title", ""),
        "state": task_data.get("state") or getattr(result, "final_task_state", ""),
        "workspace_id": task_data.get("workspace_id"),
        "realm_id": None,
        "stage": (task_data.get("mission_plan") or {}).get("current_stage_id") or task_data.get("current_stage_id"),
        "updated_at": task_data.get("updated_at"),
        "archived": True,
        "archive_batch": archived.get("archive_batch"),
        "archive_dir": archived.get("archive_dir"),
        "manifest_path": archived.get("manifest_path"),
        "archived_run_ids": list(archived_task.get("run_ids") or []),
        "archived_proof_ids": list(archived_task.get("proof_ids") or []),
    }
    return row


def _result_goal_row(result) -> dict:
    return {
        "id": getattr(result, "task_id", "") or "",
        "task_id": getattr(result, "task_id", "") or "",
        "title": getattr(result, "title", ""),
        "state": getattr(result, "final_task_state", ""),
        "workspace_id": None,
        "realm_id": None,
        "stage": None,
        "updated_at": None,
        "archived": False,
        "source": "goal_run_result",
    }


def _resolve_goal(value: str) -> Task:
    try:
        return TaskStore().get_goal(value)
    except NotFound as exc:
        raise NotFound(f"goal not found: {value}") from exc


def _sort_rows(rows: list[dict], sort_key: str | None) -> list[dict]:
    key = str(sort_key or "").strip()
    if not key:
        return rows
    reverse = key.startswith("-")
    if reverse:
        key = key[1:]
    return sorted(rows, key=lambda item: str(item.get(key, "")), reverse=reverse)


def _goal_contention_warnings(task: Task) -> list[dict]:
    try:
        assignment_store = PersonaAssignmentStore()
        warnings: list[dict] = []
        for assignment in assignment_store.list_for_task(task.id):
            warnings.extend(assignment_store.contention_warnings(persona_id=assignment.persona_id, goal_id=getattr(task, "goal_id", None) or task.id))
        return warnings
    except Exception:
        return []


def _cmd_goal_list(args) -> int:
    store = TaskStore()
    if args.state == "all":
        tasks = store.list_all()
    elif args.state == "done":
        tasks = store.list_by_state(TaskState.DONE)
    elif args.state == "blocked":
        tasks = store.list_by_state(TaskState.BLOCKED)
    else:
        tasks = store.list_open()
    if args.workspace:
        tasks = [task for task in tasks if getattr(task, "workspace_id", None) == args.workspace]
    rows = _sort_rows([_goal_row(task) for task in tasks], getattr(args, "sort", None))
    limit = getattr(args, "limit", None)
    truncated = False
    if limit is not None and limit >= 0 and len(rows) > limit:
        rows = rows[:limit]
        truncated = True
    _print_stage42(_list_envelope("goal", rows, cursor=getattr(args, "cursor", None), truncated=truncated), args=args)
    return 0


def _cmd_goal_show(args) -> int:
    task = _resolve_goal(args.goal_id)
    _print_stage42(_object_envelope("goal", _goal_row(task, full=True)), args=args)
    return 0


def _cmd_goal_history(args) -> int:
    task = _resolve_goal(args.goal_id)
    events = _task_events(task.id, limit=max(1, int(getattr(args, "limit", 50) or 50)), since_text=getattr(args, "since", None))
    if not events.get("ok"):
        return emit_harness_error(ValueError(events.get("message") or events.get("error")), args=args, code="invalid_request")
    rows = [
        {
            "id": f"event_{index}",
            "goal_id": getattr(task, "goal_id", None) or task.id,
            "task_id": task.id,
            "type": _event_value(event, "type"),
            "run_id": _event_value(event, "run_id"),
            "persona_id": _event_value(event, "persona_id"),
            "ts": _event_value(event, "ts"),
        }
        for index, event in enumerate(events.get("items", []), start=1)
    ]
    _print_stage42(_list_envelope("goal_event", rows), args=args)
    return 0


def _cmd_goal_create(args) -> int:
    from agent_runtime.mission_goal import create_mission_goal, create_mission_goal_from_request

    if getattr(args, "request_json", None):
        request = _load_request_json(args.request_json)
        data = create_mission_goal_from_request(
            request,
            start_daemon_mode=getattr(args, "start_daemon", None),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
    else:
        if not args.title or not args.description:
            return emit_harness_error(ValueError("--title and --description are required unless --request-json is provided"), args=args, code="invalid_request")
        if getattr(args, "dry_run", False):
            _print_stage42(
                _object_envelope("goal", {"id": f"goal_dry_{uuid.uuid4().hex[:6]}", "title": args.title, "state": "dry_run", "workspace_id": getattr(args, "workspace", None), "updated_at": now()}),
                args=args,
                default_output="json",
            )
            return 0
        data = create_mission_goal(
            title=args.title,
            description=args.description,
            requested_by=args.requested_by,
            start_daemon_mode=getattr(args, "start_daemon", None),
            idempotency_key=getattr(args, "idempotency_key", None),
        )
    if data.get("error"):
        err = data["error"]
        _print_stage42(_error_envelope(err.get("code") or "invalid_request", err.get("message") or "goal create failed", retryable=bool(err.get("retryable")), safe_details=err.get("safe_details") or {}), args=args, default_output="json")
        return ERROR_EXIT_CODES.get(err.get("code"), 1)
    task = TaskStore().get(data.get("task_id"))
    if getattr(args, "workspace", None):
        WorkspaceStore().get(args.workspace)
        task.workspace_id = args.workspace
        task.updated_at = now()
        TaskStore().update(task, actor="cli", reason="assigned workspace")
        task = TaskStore().get(task.id)
    _print_stage42(_object_envelope("goal", _goal_row(task), warnings=_goal_contention_warnings(task)), args=args, default_output="json")
    return 0


def _cmd_goal_unblock(args) -> int:
    task = _resolve_goal(args.goal_id)
    if task.state in {TaskState.DONE, TaskState.CANCELLED}:
        _print_stage42(
            _error_envelope("goal_terminal", f"{task.id} is terminal: {task.state.value}", retryable=False, correlation_id=getattr(task, "goal_id", None) or task.id),
            args=args,
            default_output="json",
        )
        return 6
    previous_state = task.state.value
    incident_store = IncidentStore()
    closed_incident_ids: list[str] = []
    open_incident_ids = {
        incident.id
        for incident in incident_store.list_open()
        if getattr(incident, "task_id", None) == task.id
    }
    open_incident_ids.update(task.open_incident_ids or [])
    for incident_id in sorted(open_incident_ids):
        try:
            incident_store.close(incident_id, reason=f"operator unblock: {_safe_operator_text(args.reason)}")
            closed_incident_ids.append(incident_id)
        except Exception:
            pass
    task = TaskStore().get(task.id)
    task.state = TaskState(args.state)
    task.open_incident_ids = []
    if args.rescope:
        task.current_stage_id = None
        if task.mission_plan is not None:
            task.mission_plan.current_stage_id = None
        task.stages = []
        task.affected_repos = []
        task.assigned_persona_ids = {}
        ensure_default_mission_plan(task)
    task.updated_at = now()
    TaskStore().update(task, actor="cli", reason=f"operator unblock: {_safe_operator_text(args.reason)}")
    task = TaskStore().get(task.id)
    row = _goal_row(task)
    row.update({"from": previous_state, "to": task.state.value, "closed_incident_ids": closed_incident_ids, "rescope": bool(args.rescope)})
    _print_stage42(_object_envelope("goal", row), args=args, default_output="json")
    return 0


def _cmd_goal_cancel(args) -> int:
    if not _require_yes(args):
        return 8
    task = _resolve_goal(args.goal_id)
    task = TaskStore().cancel(task.id, reason=args.reason, actor="cli")
    _print_stage42(_object_envelope("goal", _goal_row(task)), args=args)
    return 0


def _cmd_goal_archive(args) -> int:
    if not _require_yes(args):
        return 8
    task = _resolve_goal(args.goal_id)
    data = TaskStore().archive(task.id, actor="cli", reason="operator archive goal command")
    row = {"id": getattr(task, "goal_id", None) or task.id, "task_id": task.id, "state": "archived" if data.get("archived_count") else "skipped", "updated_at": now()}
    _print_stage42(_object_envelope("goal", row, warnings=data.get("skipped_tasks") or []), args=args)
    return 0 if data.get("archived_count") else 6


def _workspace_row(workspace, *, full: bool = False) -> dict:
    tasks = TaskStore().list_for_workspace(workspace.id)
    row = {
        "id": workspace.id,
        "name": workspace.name,
        "realm_id": workspace.realm_id,
        "agents": len(workspace.agent_ids or []),
        "goals": len(tasks),
        "isolation": workspace.isolation,
        "updated_at": workspace.updated_at,
    }
    if full:
        row.update(
            {
                "kind": "workspace",
                "slug": workspace.slug,
                "agent_ids": list(workspace.agent_ids or []),
                "default_blueprint_id": workspace.default_blueprint_id,
                "max_concurrent_lanes": workspace.max_concurrent_lanes,
                "goal_ids": [getattr(task, "goal_id", None) or task.id for task in tasks],
                "archived": bool(workspace.archived),
                "created_at": workspace.created_at,
            }
        )
    return row


def _cmd_workspace_list(args) -> int:
    rows = [_workspace_row(item) for item in WorkspaceStore().list_all()]
    _print_stage42(_list_envelope("workspace", _sort_rows(rows, getattr(args, "sort", None))), args=args)
    return 0


def _cmd_workspace_show(args) -> int:
    item = WorkspaceStore().get(args.workspace_id)
    _print_stage42(_object_envelope("workspace", _workspace_row(item, full=True)), args=args)
    return 0


def _cmd_workspace_create(args) -> int:
    if getattr(args, "dry_run", False):
        row = {"id": f"ws_dry_{uuid.uuid4().hex[:6]}", "name": args.name, "realm_id": args.realm, "agents": len(args.agent or []), "goals": 0, "isolation": args.isolation, "updated_at": now()}
        _print_stage42(_object_envelope("workspace", row), args=args, default_output="json")
        return 0
    if args.realm:
        RealmStore().get(args.realm)
    item = WorkspaceStore().create(
        name=args.name,
        agent_ids=list(args.agent or []),
        default_blueprint_id=args.blueprint,
        isolation=args.isolation,
        max_concurrent_lanes=args.max_lanes,
        realm_id=args.realm,
    )
    if args.realm:
        realm = RealmStore().get(args.realm)
        if item.id not in realm.workspace_ids:
            realm.workspace_ids.append(item.id)
            RealmStore().save(realm)
    # A workspace created inside the ACTIVE realm becomes active
    # immediately — the operator expects to land in the workspace they
    # just created, not to run a second `workspace use` by hand.
    # (workspace.created / workspace.activated are emitted by the store
    # chokepoint — Stage 12.)
    if item.realm_id and item.realm_id == RealmStore().active_id():
        WorkspaceStore().set_active(item.id)
    _print_stage42(_object_envelope("workspace", _workspace_row(item), warnings=[]), args=args, default_output="json")
    return 0


def _cmd_workspace_use(args) -> int:
    store = WorkspaceStore()
    outcome = store.set_active(args.workspace_id, issued_at=getattr(args, "issued_at", None))
    if not outcome.get("applied", True):
        _print_stage42(
            _object_envelope("workspace", _activation_outcome_row(store, _workspace_row, outcome, "workspace_id")),
            args=args,
            default_output="json",
        )
        return 0
    item = store.get(args.workspace_id)
    row = _workspace_row(item)
    row["applied"] = True
    _print_stage42(_object_envelope("workspace", row), args=args, default_output="json")
    return 0


def _activation_outcome_row(store, row_builder, outcome: dict, key: str) -> dict:
    """Envelope row for a set_active call the store declined. The row shows
    the pointer's CURRENT owner (what stays active); `applied`/`reason` tell
    the client why its request did not take. `superseded` = a strictly newer
    intent owns the pointer (client should drop its optimistic state);
    `duplicate` = this exact intent already applied (client treats as
    success). Exit code stays 0 — both are valid protocol outcomes, not
    errors."""
    current_id = outcome.get(key)
    try:
        row = row_builder(store.get(current_id)) if current_id else {"id": None, "name": None}
    except Exception:
        row = {"id": current_id, "name": None}
    row["applied"] = False
    row["reason"] = outcome.get("reason")
    row["superseded"] = outcome.get("reason") == "superseded"
    row[f"requested_{key}"] = outcome.get(f"requested_{key}")
    return row


def _append_scope_event(event_type: str, **payload) -> None:
    """Advance the EventLog watermark after a verb-layer mutation with no
    evented store chokepoint. Stage 12 moved scope emission into the stores
    (agent_runtime/store.py); this helper remains for catalog writes like
    `blueprint save`. Payload values of None are dropped. Best effort: a
    broken event log must not fail the verb."""
    try:
        body = {key: value for key, value in payload.items() if value is not None}
        EventLog().append(Event(now(), event_type, None, None, None, body))
    except Exception:
        pass


def _cmd_workspace_actors(args) -> int:
    workspace = WorkspaceStore().get(args.workspace_id)
    snapshot = build_snapshot()
    wanted_goal_ids = {
        getattr(task, "goal_id", None) or task.id
        for task in TaskStore().list_for_workspace(workspace.id)
    }
    actors: list[dict] = []
    for goal in snapshot.get("goals") or []:
        if goal.get("id") not in wanted_goal_ids:
            continue
        mission = goal.get("mission_level_state") if isinstance(goal, dict) else None
        for actor in (mission or {}).get("actors", []):
            if getattr(args, "kind", None) and actor.get("kind") != args.kind:
                continue
            actors.append(actor)
    _print_stage42(_list_envelope("actor", actors), args=args)
    return 0


def _known_persona_ids() -> set[str]:
    """Persona definition ids across every `.hermes` profile (Add-agent source)."""
    known: set[str] = set()
    for profile in list_profiles():
        try:
            personas = ensure_persisted_personas(load_agent_runtime_config(Path(profile.path) / "config.yaml"))
        except Exception:
            continue
        known.update(persona.id for persona in personas)
    try:
        known.update(persona.id for persona in ensure_persisted_personas(load_agent_runtime_config()))
    except Exception:
        pass
    return known


def _validate_roster_persona(persona_id: str) -> None:
    """Reject an unknown persona before it pollutes a workspace roster.

    Adding an agent is meant to sync that agent's skills/soul/memory/context —
    a non-existent persona has none, so this must fail rather than silently
    persist garbage (`persona_not_found`, exit 3).
    """
    if persona_id not in _known_persona_ids():
        raise NotFound(f"persona not found: {persona_id}")


def _cmd_workspace_add_agent(args) -> int:
    try:
        _validate_roster_persona(args.persona_id)
    except NotFound as exc:
        return emit_harness_error(exc, args=args, code="persona_not_found")
    if getattr(args, "dry_run", False):
        item = WorkspaceStore().get(args.workspace_id)
        row = _workspace_row(item)
        row["agents"] = len(set([*item.agent_ids, args.persona_id]))
        warnings = _workspace_agent_sync_warnings(item.id, args.persona_id)
        _print_stage42(_object_envelope("workspace", row, warnings=warnings), args=args, default_output="json")
        return 0
    item = WorkspaceStore().add_agent(args.workspace_id, args.persona_id)
    warnings = _workspace_agent_sync_warnings(item.id, args.persona_id)
    _print_stage42(_object_envelope("workspace", _workspace_row(item), warnings=warnings), args=args, default_output="json")
    return 0


def _workspace_agent_sync_warnings(workspace_id: str, persona_id: str) -> list[dict]:
    artifacts = sync_artifacts_for_workspace_agent(workspace_id, persona_id)
    if not artifacts:
        return []
    return [
        {
            "code": "realm_sync_set_updated",
            "message": "Workspace agent artifacts are now included in the realm sync allowlist.",
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
        }
    ]


def _cmd_workspace_remove_agent(args) -> int:
    if not _require_yes(args):
        return 8
    if getattr(args, "dry_run", False):
        item = WorkspaceStore().get(args.workspace_id)
    else:
        item = WorkspaceStore().remove_agent(args.workspace_id, args.persona_id)
    _print_stage42(_object_envelope("workspace", _workspace_row(item)), args=args, default_output="json")
    return 0


def _cmd_workspace_rename(args) -> int:
    if getattr(args, "dry_run", False):
        item = WorkspaceStore().get(args.workspace_id)
    else:
        item = WorkspaceStore().rename(args.workspace_id, args.name)
    row = _workspace_row(item)
    if getattr(args, "dry_run", False):
        row["name"] = args.name
    _print_stage42(_object_envelope("workspace", row), args=args, default_output="json")
    return 0


def _cmd_workspace_archive(args) -> int:
    if not _require_yes(args):
        return 8
    if getattr(args, "dry_run", False):
        item = WorkspaceStore().get(args.workspace_id)
    else:
        item = WorkspaceStore().archive(args.workspace_id)
    _print_stage42(_object_envelope("workspace", _workspace_row(item, full=True)), args=args, default_output="json")
    return 0


def _realm_row(realm, *, full: bool = False) -> dict:
    workspaces = [item for item in WorkspaceStore().list_all(include_archived=True) if item.realm_id == realm.id]
    workspace_ids = list(dict.fromkeys([*(realm.workspace_ids or []), *[item.id for item in workspaces]]))
    row = {
        "id": realm.id,
        "name": realm.name,
        "server_id": realm.server_id,
        "default_workspace_id": getattr(realm, "default_workspace_id", None),
        "default_workspace_version": getattr(realm, "default_workspace_version", 0),
        "workspaces": len(workspace_ids),
        "sync": "in_sync",
        "updated_at": realm.updated_at,
    }
    if full:
        row.update({"kind": "realm", "slug": realm.slug, "workspace_ids": workspace_ids, "default_workspace_name": getattr(realm, "default_workspace_name", "Default"), "default_workspace_version": getattr(realm, "default_workspace_version", 0), "sync_manifest_ref": realm.sync_manifest_ref, "archived": bool(realm.archived), "created_at": realm.created_at})
    return row


def _cmd_realm_list(args) -> int:
    rows = [_realm_row(item) for item in RealmStore().list_all()]
    _print_stage42(_list_envelope("realm", _sort_rows(rows, getattr(args, "sort", None))), args=args)
    return 0


def _cmd_realm_show(args) -> int:
    item = RealmStore().get(args.realm_id)
    _print_stage42(_object_envelope("realm", _realm_row(item, full=True)), args=args)
    return 0


def _cmd_realm_create(args) -> int:
    if getattr(args, "dry_run", False):
        row = {"id": f"realm_dry_{uuid.uuid4().hex[:6]}", "name": args.name, "server_id": args.server, "workspaces": 0, "sync": "in_sync", "updated_at": now()}
        _print_stage42(_object_envelope("realm", row), args=args, default_output="json")
        return 0
    item = RealmStore().create(name=args.name, server_id=args.server)
    _print_stage42(_object_envelope("realm", _realm_row(item)), args=args, default_output="json")
    return 0


def _cmd_realm_bind_server(args) -> int:
    if getattr(args, "dry_run", False):
        item = RealmStore().get(args.realm_id)
    else:
        item = RealmStore().bind_server(args.realm_id, args.server_id)
    row = _realm_row(item)
    if getattr(args, "dry_run", False):
        row["server_id"] = args.server_id
    _print_stage42(_object_envelope("realm", row), args=args, default_output="json")
    return 0


def _cmd_realm_use(args) -> int:
    store = RealmStore()
    issued_at = getattr(args, "issued_at", None)
    outcome = store.set_active(args.realm_id, issued_at=issued_at)
    if not outcome.get("applied", True):
        _print_stage42(
            _object_envelope("realm", _activation_outcome_row(store, _realm_row, outcome, "realm_id")),
            args=args,
            default_output="json",
        )
        return 0
    item = store.get(args.realm_id)
    _reconcile_active_workspace_to_realm(item, issued_at=issued_at)
    row = _realm_row(item)
    row["applied"] = True
    _print_stage42(_object_envelope("realm", row), args=args, default_output="json")
    return 0


def _reconcile_active_workspace_to_realm(realm, *, issued_at: str | None = None) -> None:
    """Switching realms must not leave the active workspace pointing into
    another realm. Keep it when it already belongs; otherwise fall to the
    realm's declared default, then its configured order, then listing order,
    choosing only unarchived workspaces; clear it when the realm has none."""
    store = WorkspaceStore()
    active_id = store.active_id()
    if active_id:
        try:
            active = store.get(active_id)
        except Exception:
            active = None
        if active is not None and getattr(active, "realm_id", None) == realm.id:
            return
    candidates = [
        workspace
        for workspace in store.list_all()
        if getattr(workspace, "realm_id", None) == realm.id and not workspace.archived
    ]
    configured_order = {wid: index for index, wid in enumerate(getattr(realm, "workspace_ids", None) or [])}
    default_workspace_id = getattr(realm, "default_workspace_id", None)
    candidates.sort(
        key=lambda workspace: (
            0 if workspace.id == default_workspace_id else 1,
            configured_order.get(workspace.id, len(configured_order)),
            workspace.id,
        )
    )
    next_workspace = candidates[0] if candidates else None
    # set_active emits workspace.activated (or {"cleared": true}) at the
    # store chokepoint — Stage 12. The realm intent's basis rides along so a
    # late-delivered realm switch cannot clobber a newer explicit workspace
    # selection through its reconcile.
    store.set_active(next_workspace.id if next_workspace else None, issued_at=issued_at)


def _realm_sync_credential(args):
    """Parse the launcher-brokered credential from --credential-file or the
    HERMES_REALM_SYNC_CREDENTIAL env fallback; None when neither is set."""
    from agent_runtime.realm_membership import load_realm_sync_credential

    return load_realm_sync_credential(getattr(args, "credential_file", None))


def _cmd_realm_adopt(args) -> int:
    from agent_runtime.realm_membership import adopt_realms

    try:
        credential = _realm_sync_credential(args)
        if credential is None:
            raise RealmSyncError(
                "sync_auth_failed",
                "realm adopt requires a launcher-brokered credential; pass --credential-file or set HERMES_REALM_SYNC_CREDENTIAL.",
            )
        adopted = adopt_realms(credential, server_id=getattr(args, "server", None), dry_run=bool(getattr(args, "dry_run", False)))
    except RealmSyncError as exc:
        return emit_harness_error(exc, args=args)
    rows = [_realm_row(item) for item in adopted]
    _print_stage42(_list_envelope("realm", _sort_rows(rows, getattr(args, "sort", None))), args=args, default_output="json")
    return 0


def _cmd_realm_sync_status(args) -> int:
    try:
        data = realm_sync_status(args.realm_id, credential=_realm_sync_credential(args))
    except RealmSyncError as exc:
        return emit_harness_error(exc, args=args)
    _print_stage42(data, args=args, default_output="json")
    return 0


def _cmd_realm_sync_pull(args) -> int:
    try:
        data = pull_realm_sync(args.realm_id, dry_run=bool(getattr(args, "dry_run", False)), credential=_realm_sync_credential(args))
    except RealmSyncError as exc:
        return emit_harness_error(exc, args=args)
    _print_stage42(data, args=args, default_output="json")
    return 0


def _cmd_realm_sync_publish(args) -> int:
    if not _require_yes(args):
        return 8
    try:
        data = publish_realm_sync(args.realm_id, dry_run=bool(getattr(args, "dry_run", False)), credential=_realm_sync_credential(args))
    except RealmSyncError as exc:
        return emit_harness_error(exc, args=args)
    _print_stage42(data, args=args, default_output="json")
    return 0


def _cmd_agent_list(args) -> int:
    rows: list[dict] = []
    if getattr(args, "all_profiles", False):
        for profile in list_profiles():
            try:
                cfg = load_agent_runtime_config(Path(profile.path) / "config.yaml")
                personas = ensure_persisted_personas(cfg)
            except Exception:
                continue
            for persona in personas:
                rows.append(_agent_definition_row(persona, profile_name=profile.name))
    else:
        for persona in ensure_persisted_personas(load_agent_runtime_config()):
            rows.append(_agent_definition_row(persona, profile_name=active_profile_name()))
    deduped: dict[tuple[str, str | None], dict] = {}
    for row in rows:
        deduped[(row["id"], row.get("profile"))] = row
    _print_stage42(_list_envelope("agent", _sort_rows(list(deduped.values()), getattr(args, "sort", None))), args=args)
    return 0


def _agent_definition_row(persona: AgentPersona, *, profile_name: str | None) -> dict:
    return {
        "id": persona.id,
        "name": persona.display_name,
        "role": str(persona.role),
        "profile": profile_name or persona.hermes_profile,
        "state": "available",
        "updated_at": None,
    }


def _cmd_pets_gallery(args) -> int:
    from agent.pet import store

    query = str(getattr(args, "query", "") or "").strip().lower()
    limit = max(0, int(getattr(args, "limit", 0) or 0))
    local_only = bool(getattr(args, "local_only", False))
    installed = {pet.slug: pet for pet in store.installed_pets()}
    rows: list[dict] = []
    seen: set[str] = set()
    manifest_error = ""

    if not local_only:
        try:
            from agent.pet.manifest import fetch_manifest

            entries = fetch_manifest()
            if query:
                entries = [
                    entry
                    for entry in entries
                    if query in entry.slug.lower() or query in entry.display_name.lower()
                ]
            if limit:
                entries = entries[:limit]
            for entry in entries:
                seen.add(entry.slug)
                rows.append(
                    {
                        "slug": entry.slug,
                        "displayName": entry.display_name,
                        "kind": entry.kind,
                        "submittedBy": entry.submitted_by,
                        "installed": entry.slug in installed,
                        "spritesheetUrl": entry.spritesheet_url,
                        "petJsonUrl": entry.pet_json_url,
                        "zipUrl": entry.zip_url,
                        "curated": "/curated/" in entry.spritesheet_url,
                        "generated": entry.slug in installed and installed[entry.slug].generated,
                    }
                )
        except Exception as exc:  # noqa: BLE001 - Launcher can still use local pets
            manifest_error = f"manifest unavailable: {exc}"

    for slug, pet in installed.items():
        if slug in seen:
            continue
        if query and query not in slug.lower() and query not in pet.display_name.lower():
            continue
        rows.append(_installed_pet_gallery_row(pet))

    data = {"ok": True, "localOnly": local_only, "pets": rows}
    if manifest_error:
        data["manifestError"] = manifest_error
    print(emit_json(data) if getattr(args, "json", False) else json.dumps(data, indent=2))
    return 0


def _cmd_pets_install(args) -> int:
    from agent.pet import store
    from agent.pet.manifest import ManifestError

    slug = str(getattr(args, "slug", "") or "").strip()
    try:
        pet = store.install_pet(slug, force=bool(getattr(args, "force", False)))
    except (store.PetStoreError, ManifestError) as exc:
        data = {"ok": False, "slug": slug, "error": str(exc)}
        print(emit_json(data) if getattr(args, "json", False) else data["error"])
        return 2
    data = {"ok": True, "pet": _installed_pet_gallery_row(pet)}
    print(emit_json(data) if getattr(args, "json", False) else json.dumps(data, indent=2))
    return 0


def _cmd_pets_sprite(args) -> int:
    from agent.pet import store

    slug = str(getattr(args, "slug", "") or "").strip()
    pet = store.load_pet(slug)
    if pet is None or not pet.exists:
        data = {"ok": False, "slug": slug, "error": f"pet '{slug}' is not installed"}
        print(emit_json(data) if getattr(args, "json", False) else data["error"])
        return 2
    data = {"ok": True, "pet": _pet_sprite_payload_for_launcher(pet)}
    print(emit_json(data) if getattr(args, "json", False) else json.dumps(data, indent=2))
    return 0


def _cmd_pets_thumb(args) -> int:
    import base64

    from agent.pet import store

    slug = str(getattr(args, "slug", "") or "").strip()
    data = store.thumbnail_png(slug, source_url=str(getattr(args, "url", "") or ""))
    payload = (
        {"ok": True, "slug": slug, "dataUri": "data:image/png;base64," + base64.standard_b64encode(data).decode("ascii")}
        if data
        else {"ok": False, "slug": slug}
    )
    print(emit_json(payload) if getattr(args, "json", False) else json.dumps(payload, indent=2))
    return 0


def _installed_pet_gallery_row(pet) -> dict:
    return {
        "slug": pet.slug,
        "displayName": pet.display_name,
        "description": pet.description,
        "kind": "local",
        "submittedBy": "",
        "installed": True,
        "spritesheetUrl": "",
        "petJsonUrl": "",
        "zipUrl": "",
        "curated": False,
        "generated": pet.generated,
    }


def _pet_sheet_revision(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return ""
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _pet_row_frame_counts(spritesheet: Path) -> dict[str, int]:
    try:
        from PIL import Image

        from agent.pet import constants, render

        with Image.open(spritesheet) as opened:
            image = opened.convert("RGBA")
        cols = max(1, image.width // constants.FRAME_W)
        row_count = max(1, image.height // constants.FRAME_H)
        rows = constants.state_rows_for_grid(row_count)
        out: dict[str, int] = {}
        for row_idx, name in enumerate(rows[:row_count]):
            top = row_idx * constants.FRAME_H
            count = 0
            for col in range(cols):
                left = col * constants.FRAME_W
                frame = image.crop((left, top, left + constants.FRAME_W, top + constants.FRAME_H))
                if render._frame_is_blank(frame):
                    break
                count += 1
            out[name] = count
        return out
    except Exception:  # noqa: BLE001 - cosmetic payload; renderer can fall back
        return {}


def _pet_state_rows(spritesheet: Path) -> list[str]:
    try:
        from PIL import Image

        from agent.pet import constants

        with Image.open(spritesheet) as opened:
            row_count = max(1, opened.height // constants.FRAME_H)
        return list(constants.state_rows_for_grid(row_count))
    except Exception:  # noqa: BLE001
        from agent.pet import constants

        return list(constants.STATE_ROWS)


def _pet_sprite_payload_for_launcher(pet) -> dict:
    import base64

    from agent.pet import constants, render

    raw = pet.spritesheet.read_bytes()
    suffix = pet.spritesheet.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/webp"
    return {
        "slug": pet.slug,
        "displayName": pet.display_name,
        "description": pet.description,
        "mime": mime,
        "spritesheetBase64": base64.standard_b64encode(raw).decode("ascii"),
        "spritesheetRevision": _pet_sheet_revision(pet.spritesheet),
        "frameW": constants.FRAME_W,
        "frameH": constants.FRAME_H,
        "framesPerState": constants.FRAMES_PER_STATE,
        "framesByState": render.state_frame_counts(pet.spritesheet),
        "framesByRow": _pet_row_frame_counts(pet.spritesheet),
        "loopMs": constants.LOOP_MS,
        "scale": constants.DEFAULT_SCALE,
        "stateRows": _pet_state_rows(pet.spritesheet),
    }


def _cmd_blueprint_list(args) -> int:
    from agent_runtime.blueprints.store import BlueprintStore, blueprint_summary

    items = [blueprint_summary(bp) for bp in BlueprintStore().list()]
    data = {"ok": True, "blueprints": items}
    if args.json:
        print(emit_json(data))
    else:
        for bp in items:
            print(f"{bp['id']} v{bp['version']}: {bp['title']}")
    return 0


def _cmd_blueprint_validate(args) -> int:
    from agent_runtime.blueprints.schema import validate_blueprint
    from agent_runtime.blueprints.store import BlueprintStore, blueprint_summary

    try:
        bp = BlueprintStore().get(args.blueprint)
        errors = validate_blueprint(bp)
    except Exception as exc:
        data = {"ok": False, "error": str(exc)}
        print(emit_json(data) if args.json else data["error"])
        return 2
    data = {"ok": not errors, "errors": errors, "blueprint": blueprint_summary(bp)}
    if args.json:
        print(emit_json(data))
    else:
        print(f"blueprint {bp.id}: {'valid' if not errors else 'invalid'}")
        for error in errors:
            print(f"- {error}")
    return 0 if not errors else 2


def _cmd_blueprint_save(args) -> int:
    import json as _json

    from agent_runtime.blueprints.schema import blueprint_from_dict
    from agent_runtime.blueprints.store import blueprint_summary, save_blueprint

    try:
        spec_path = Path(args.spec_file)
        text = spec_path.read_text(encoding="utf-8")
        if spec_path.suffix.lower() in {".yaml", ".yml"}:
            import yaml

            raw = yaml.safe_load(text) or {}
        else:
            raw = _json.loads(text)
        bp = blueprint_from_dict(raw)  # validates; raises ValueError on an invalid graph
        path = save_blueprint(bp)
    except Exception as exc:
        data = {"ok": False, "error": str(exc)}
        print(emit_json(data) if args.json else data["error"])
        return 2
    # The blueprint catalog is client-visible snapshot state (snapshot
    # `blueprints[]`); a save without an event is invisible to the
    # watermark-gated stream/read-model pipeline (Stage 12).
    _append_scope_event("blueprint.saved", blueprint_id=bp.id, version=bp.version, title=bp.title)
    data = {"ok": True, "blueprint_id": bp.id, "version": bp.version, "path": str(path), "blueprint": blueprint_summary(bp)}
    print(emit_json(data) if args.json else f"saved blueprint {bp.id} -> {path}")
    return 0


def _cmd_blueprint_run(args) -> int:
    from agent_runtime.blueprints.instantiate import instantiate_blueprint
    from agent_runtime.blueprints.store import BlueprintStore
    from agent_runtime.mission_plan import mission_plan_summary
    from agent_runtime.state_machine import MissionStateMachine

    from agent_runtime.blueprints.resolve import BindingResolver

    try:
        bp = BlueprintStore().get(args.blueprint)
        bindings = _parse_blueprint_bindings(args.bind or [])
        # Dry-run resolves find-only (no persona promotion / no writes); a real run
        # may promote a bare profile into a persisted persona.
        resolver = BindingResolver(allow_promote=not args.dry_run)
        plan = instantiate_blueprint(bp, goal=args.goal, bindings=bindings, resolver=resolver)
    except Exception as exc:
        data = {"ok": False, "error": str(exc)}
        print(emit_json(data) if args.json else data["error"])
        return 2
    task = Task(
        id=f"task_blueprint_{uuid.uuid4().hex[:12]}",
        title=bp.title,
        description=args.goal,
        state=TaskState.CREATED,
        created_at=now(),
        updated_at=now(),
        requested_by=args.requested_by,
        mission_plan=plan,
        current_stage_id=plan.current_stage_id,
    )
    action = MissionStateMachine().next_action(task)
    data = {
        "ok": True,
        "dry_run": bool(args.dry_run),
        "blueprint_id": bp.id,
        "blueprint_version": bp.version,
        "task_id": task.id,
        "mission_plan": mission_plan_summary(task),
        "next_action": {
            "type": action.type.value,
            "task_id": action.task_id,
            "slot_id": action.slot_id,
            "reason": action.reason,
        },
    }
    if not args.dry_run:
        TaskStore().create(task)
        data["created"] = True
    if args.json:
        print(emit_json(data))
    else:
        prefix = "dry-run" if args.dry_run else "created"
        print(f"{prefix} blueprint {bp.id} task={task.id} next={action.type.value} slot={action.slot_id or '-'}")
    return 0


def _cmd_blueprint_matrix_run(args) -> int:
    from agent_runtime.blueprints.instantiate import instantiate_blueprint
    from agent_runtime.blueprints.resolve import BindingResolver
    from agent_runtime.blueprints.store import BlueprintStore
    from agent_runtime.mission_plan import mission_plan_summary
    from agent_runtime.state_machine import MissionStateMachine

    try:
        bp = BlueprintStore().get(args.blueprint)
        base_bindings = _parse_blueprint_bindings(args.bind or [])
        variations = _parse_blueprint_variations(args.vary or [])
        cases = _matrix_binding_cases(base_bindings, variations)
    except Exception as exc:
        data = {"ok": False, "error": str(exc)}
        print(emit_json(data) if args.json else data["error"])
        return 2
    resolver = BindingResolver(allow_promote=not args.dry_run)
    results = []
    ok = True
    for index, bindings in enumerate(cases, start=1):
        task_id = f"task_blueprint_matrix_{uuid.uuid4().hex[:12]}"
        try:
            plan = instantiate_blueprint(bp, goal=args.goal, bindings=bindings, resolver=resolver)
            task = Task(
                id=task_id,
                title=f"{bp.title} Matrix {index}",
                description=args.goal,
                state=TaskState.CREATED,
                created_at=now(),
                updated_at=now(),
                requested_by=args.requested_by,
                mission_plan=plan,
                current_stage_id=plan.current_stage_id,
            )
            action = MissionStateMachine().next_action(task)
            if not args.dry_run:
                TaskStore().create(task)
            results.append(
                {
                    "case": index,
                    "ok": True,
                    "created": not bool(args.dry_run),
                    "task_id": task.id,
                    "bindings": dict(bindings),
                    "resolved_bindings": dict(plan.bindings),
                    "metrics": {
                        "stage_count": len(plan.stages),
                        "edge_count": len(plan.edges),
                        "attempts": dict(plan.stage_attempts),
                    },
                    "next_action": {
                        "type": action.type.value,
                        "task_id": action.task_id,
                        "slot_id": action.slot_id,
                        "reason": action.reason,
                    },
                    "mission_plan": mission_plan_summary(task),
                }
            )
        except Exception as exc:
            ok = False
            results.append({"case": index, "ok": False, "created": False, "task_id": task_id, "bindings": dict(bindings), "error": str(exc)})
    data = {
        "ok": ok,
        "dry_run": bool(args.dry_run),
        "blueprint_id": bp.id,
        "blueprint_version": bp.version,
        "case_count": len(results),
        "vary": variations,
        "results": results,
    }
    if args.json:
        print(emit_json(data))
    else:
        print(f"{'dry-run ' if args.dry_run else ''}matrix blueprint {bp.id}: {len(results)} case(s)")
        for item in results:
            status = "ok" if item.get("ok") else "failed"
            slot = ((item.get("next_action") or {}).get("slot_id") if item.get("ok") else "-") or "-"
            print(f"- case {item['case']}: {status} task={item['task_id']} next_slot={slot}")
    return 0 if ok else 2


def _parse_blueprint_bindings(items: list[str]) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for item in items:
        if "=" not in str(item):
            raise ValueError("--bind must be slot=persona:<id> or slot=profile:<name>")
        slot, value = str(item).split("=", 1)
        slot = slot.strip()
        value = value.strip()
        if not slot or not value:
            raise ValueError("--bind must include a non-empty slot and value")
        bindings[slot] = value
    return bindings


def _parse_blueprint_variations(items: list[str]) -> dict[str, list[str]]:
    variations: dict[str, list[str]] = {}
    for item in items:
        if "=" not in str(item):
            raise ValueError("--vary must be slot=value1,value2")
        slot, values = str(item).split("=", 1)
        slot = slot.strip()
        choices = [value.strip() for value in values.split(",") if value.strip()]
        if not slot or not choices:
            raise ValueError("--vary must include a non-empty slot and at least one binding")
        variations[slot] = choices
    if not variations:
        raise ValueError("matrix-run requires at least one --vary slot=value1,value2")
    return variations


def _matrix_binding_cases(base: dict[str, str], variations: dict[str, list[str]]) -> list[dict[str, str]]:
    from itertools import product

    slots = list(variations)
    cases: list[dict[str, str]] = []
    for choices in product(*(variations[slot] for slot in slots)):
        bindings = dict(base)
        bindings.update({slot: value for slot, value in zip(slots, choices)})
        cases.append(bindings)
    return cases


def _cmd_init(args) -> int:
    personas = ensure_persisted_personas(load_agent_runtime_config())
    data = {"personas": [p.id for p in personas]}
    print(emit_json(data) if args.json else f"Initialized harness personas: {', '.join(data['personas'])}")
    return 0


def _cmd_install_harness_skills(args) -> int:
    if getattr(args, "active_profile_only", False):
        results = install_harness_skills()
    else:
        results = install_harness_skills_for_personas(ensure_persisted_personas(load_agent_runtime_config()))
    data = {"installed": [asdict(result) for result in results], "ok": all(result.ok for result in results)}
    if args.json:
        print(emit_json(data))
    else:
        for result in results:
            state = "updated" if result.changed else "ok"
            print(f"{result.skill}: {state}")
    return 0 if data["ok"] else 1


def _cmd_doctor(args) -> int:
    resolution = resolve_runtime()
    if getattr(args, "fix", False) and not getattr(args, "dry_run", False) and not getattr(args, "yes", False):
        data = {
            "ok": False,
            "error": "confirmation_required",
            "summary": "harness doctor --fix requires --yes, or use --dry-run to preview repairs",
        }
        print(emit_json(data) if args.json else data["summary"])
        return ERROR_EXIT_CODES["confirmation_required"]
    hygiene = run_harness_doctor(
        fix=bool(getattr(args, "fix", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        stale_run_hours=int(getattr(args, "stale_run_hours", DEFAULT_STALE_RUN_HOURS) or DEFAULT_STALE_RUN_HOURS),
        stale_worker_hours=int(getattr(args, "stale_worker_hours", DEFAULT_STALE_WORKER_HOURS) or DEFAULT_STALE_WORKER_HOURS),
        stale_task_days=int(getattr(args, "stale_task_days", DEFAULT_STALE_TASK_DAYS) or DEFAULT_STALE_TASK_DAYS),
        stale_incident_hours=int(getattr(args, "stale_incident_hours", None) or DEFAULT_STALE_INCIDENT_HOURS),
        stale_incident_days=(
            None
            if getattr(args, "stale_incident_hours", None) is not None
            else int(getattr(args, "stale_incident_days", DEFAULT_STALE_INCIDENT_DAYS) or DEFAULT_STALE_INCIDENT_DAYS)
        ),
        worktree_min_age_seconds=int(
            getattr(args, "worktree_min_age_seconds", DEFAULT_WORKTREE_MIN_AGE_SECONDS)
            or DEFAULT_WORKTREE_MIN_AGE_SECONDS
        ),
        compact_events=bool(getattr(args, "compact_events", False)),
    )
    data = {
        "ok": True,
        "runtime_resolution": {
            "store_root": str(resolution.store_root),
            "layer": resolution.layer,
            "hermes_home": resolution.hermes_home,
            "config_path": resolution.config_path,
            "trace": list(resolution.trace),
            "layers": resolution_table(),
        },
        "hygiene": hygiene,
    }
    if args.json:
        print(emit_json(data))
    else:
        print("Harness runtime resolution")
        print(f"resolved: {resolution.store_root} ({resolution.layer})")
        for row in data["runtime_resolution"]["layers"]:
            marker = "*" if row["winner"] else " "
            print(
                f"{marker} {row['layer']:<7} value={row['value'] or '<unset>'} "
                f"exists={row['exists']} tasks={row['tasks']}"
            )
        counts = hygiene["summary"]["finding_counts"]
        event_log = hygiene["findings"]["event_log"]
        print("Harness doctor")
        print(
            "findings: "
            f"runs={counts['stale_runs']} workers={counts['stale_workers']} "
            f"tasks={counts['stale_open_tasks']} incidents={counts['stale_incidents']} "
            f"worktrees={counts['orphan_worktrees']} "
            f"snapshot_null_ids={counts['snapshot_null_id_rows']} "
            f"event_compactable_rows={counts['event_log_compactable_rows']}"
        )
        print(
            "event log: "
            f"size={event_log['size_bytes']} bytes lines={event_log['line_count']} "
            f"archive_slices={event_log['archived_event_slices']} "
            f"index={event_log['index_health']}"
        )
        if getattr(args, "fix", False):
            mode = "dry run" if getattr(args, "dry_run", False) else "applied"
            print(f"repairs: {mode}")
    return 0


def _cmd_goal_run(args) -> int:
    cfg = load_agent_runtime_config()
    os.environ.setdefault("HERMES_AGENT_RUNTIME_ROOT", str(paths.store_root()))
    try:
        bindings = _parse_blueprint_bindings(list(args.bind or []))
    except Exception as exc:
        data = {"ok": False, "error": str(exc)}
        print(emit_json(data) if args.json else data["error"])
        return 2
    result = MissionRuntimeController(
        config=cfg,
        engine_factory=lambda **kwargs: TickEngine(
            **kwargs,
            persona_runtime=GPTPersonaRuntime(default_provider=cfg.default_provider, default_model=cfg.default_model),
        ),
    ).run_goal(
        GoalRunOptions(
            title=args.title,
            description=args.description,
            requested_by=args.requested_by,
            max_actions=args.max_actions,
            max_seconds=args.max_seconds,
            archive_on_done=args.archive_on_done,
            requires_visual_proof=args.requires_visual_proof,
            affected_repos=list(args.affected_repo or []),
            acceptance_criteria=list(args.acceptance or []),
            non_goals=list(args.non_goal or []),
            blueprint_id=args.blueprint,
            bindings=bindings,
            workspace_id=getattr(args, "workspace", None),
            runtime_root=getattr(args, "runtime_root", None),
        )
    )
    if getattr(args, "json", False) or getattr(args, "output", None):
        task = None
        try:
            task = TaskStore().get(result.task_id)
            row = _goal_row(task)
            warnings = _goal_contention_warnings(task)
        except NotFound:
            row = _archived_goal_row(result.task_id, result)
            if row is None:
                row = _result_goal_row(result)
            warnings = []
        row.update(
            {
                "stop_reason": result.stop_reason,
                "actions_taken": result.actions_taken,
                "run_ids": list(result.run_ids),
                "proof_ids": list(result.proof_ids),
                "open_incident_ids": list(result.open_incident_ids),
            }
        )
        if result.archive_result is not None:
            row["archive_result"] = result.archive_result
            row["archived"] = bool(row.get("archived") or result.archive_result.get("archived_count"))
        _print_stage42(_object_envelope("goal", row, warnings=warnings), args=args, default_output="json")
    else:
        print(f"goal {result.task_id}: stop={result.stop_reason} state={result.final_task_state} actions={result.actions_taken}")
    return result.exit_code



def _cmd_serve(args) -> int:
    # serve is a real module (not an exec'd part): it is the process's main
    # loop, owns sys.stdout/sys.stderr swaps, and is imported by tests.
    from hermes_cli.harness_parts.serve import _cmd_serve as _run_serve

    return _run_serve(args)


def _load_command_parts() -> None:
    parts_dir = Path(__file__).with_name("harness_parts")
    for filename in ("persona_commands.py", "runtime_commands.py"):
        path = parts_dir / filename
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), globals())


_load_command_parts()
