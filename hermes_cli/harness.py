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
from agent_runtime.daemon import MissionDaemon, read_daemon_status, start_daemon, stop_daemon
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
from agent_runtime.goal_runner import GoalRunOptions, MissionRuntimeController
from agent_runtime.launcher_process_hygiene import launcher_visual_cleanup_needed
from agent_runtime.models import AgentPersona, Event, Task
from agent_runtime import paths
from agent_runtime.persona_assignments import (
    ChatBusyError,
    PersonaAssignmentSpec,
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
from agent_runtime.mission_chat_turns import persist_mission_chat_turn
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

    tick = subs.add_parser("tick", help="Run one harness tick")
    tick.add_argument("--task", dest="task_id", default=None, help="Run one tick for a specific task id")
    tick.add_argument("--json", action="store_true")
    tick.set_defaults(func=_cmd_tick)

    settle = subs.add_parser("run-until-settled", help="Run bounded mission ticks until done, blocked, waiting, or incident")
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

    doctor = subs.add_parser("doctor", help="Show Harness runtime resolution diagnostics")
    doctor.add_argument("--json", action="store_true")
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
        data = create_mission_goal_from_request(request)
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
    _print_stage42(_object_envelope("workspace", _workspace_row(item), warnings=[]), args=args, default_output="json")
    return 0


def _cmd_workspace_use(args) -> int:
    WorkspaceStore().set_active(args.workspace_id)
    item = WorkspaceStore().get(args.workspace_id)
    _print_stage42(_object_envelope("workspace", _workspace_row(item)), args=args, default_output="json")
    return 0


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
    item = WorkspaceStore().get(args.workspace_id) if getattr(args, "dry_run", False) else WorkspaceStore().rename(args.workspace_id, args.name)
    row = _workspace_row(item)
    if getattr(args, "dry_run", False):
        row["name"] = args.name
    _print_stage42(_object_envelope("workspace", row), args=args, default_output="json")
    return 0


def _cmd_workspace_archive(args) -> int:
    if not _require_yes(args):
        return 8
    item = WorkspaceStore().get(args.workspace_id) if getattr(args, "dry_run", False) else WorkspaceStore().archive(args.workspace_id)
    _print_stage42(_object_envelope("workspace", _workspace_row(item, full=True)), args=args, default_output="json")
    return 0


def _realm_row(realm, *, full: bool = False) -> dict:
    workspaces = [item for item in WorkspaceStore().list_all(include_archived=True) if item.realm_id == realm.id]
    workspace_ids = list(dict.fromkeys([*(realm.workspace_ids or []), *[item.id for item in workspaces]]))
    row = {
        "id": realm.id,
        "name": realm.name,
        "server_id": realm.server_id,
        "workspaces": len(workspace_ids),
        "sync": "in_sync",
        "updated_at": realm.updated_at,
    }
    if full:
        row.update({"kind": "realm", "slug": realm.slug, "workspace_ids": workspace_ids, "sync_manifest_ref": realm.sync_manifest_ref, "archived": bool(realm.archived), "created_at": realm.created_at})
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
    item = RealmStore().get(args.realm_id) if getattr(args, "dry_run", False) else RealmStore().bind_server(args.realm_id, args.server_id)
    row = _realm_row(item)
    if getattr(args, "dry_run", False):
        row["server_id"] = args.server_id
    _print_stage42(_object_envelope("realm", row), args=args, default_output="json")
    return 0


def _cmd_realm_use(args) -> int:
    RealmStore().set_active(args.realm_id)
    item = RealmStore().get(args.realm_id)
    _print_stage42(_object_envelope("realm", _realm_row(item)), args=args, default_output="json")
    return 0


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
    data = {
        "runtime_resolution": {
            "store_root": str(resolution.store_root),
            "layer": resolution.layer,
            "hermes_home": resolution.hermes_home,
            "config_path": resolution.config_path,
            "trace": list(resolution.trace),
            "layers": resolution_table(),
        }
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


def _cmd_persona_list(args) -> int:
    cfg = load_agent_runtime_config()
    store = PersonaInstanceStore()
    workers = WorkerSessionStore().list_all()
    personas = ensure_persisted_personas(cfg)
    personas_by_id = {str(getattr(persona, "id", "") or ""): persona for persona in personas}
    enabled = persona_instance_runtime_enabled(cfg)
    instances = store.derive_from_workers(personas, workers) if enabled else []
    data = {
        "feature_enabled": enabled,
        "assignment_store_enabled": persona_assignment_store_enabled(cfg),
        "persona_instances": [
            persona_instance_summary(instance, personas_by_id.get(str(getattr(instance, "persona_id", "") or "")))
            for instance in instances
        ],
    }
    if args.json:
        print(emit_json(data))
    else:
        if not enabled:
            print("Persona instance runtime is disabled.")
        for instance in data["persona_instances"]:
            print(f"{instance['persona_instance_id']}: {instance['display_name']} state={instance['state']} assignment={instance['current_assignment_id'] or '-'}")
    return 0


def _cmd_persona_show(args) -> int:
    cfg = load_agent_runtime_config()
    if not persona_instance_runtime_enabled(cfg):
        data = {"ok": False, "feature_enabled": False, "error": "persona instance runtime is disabled"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    store = PersonaInstanceStore()
    personas = ensure_persisted_personas(cfg)
    personas_by_id = {str(getattr(persona, "id", "") or ""): persona for persona in personas}
    store.derive_from_workers(personas, WorkerSessionStore().list_all())
    value = str(args.persona_id_or_instance_id or "").strip()
    instance_id = value if value.startswith("personainst_") else persona_instance_id_for(_normalize_cli_persona_id(value))
    try:
        instance = store.get(instance_id)
    except Exception:
        data = {"ok": False, "feature_enabled": True, "error": f"persona instance not found: {value}"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    assignments = PersonaAssignmentStore().list_for_persona(instance.persona_id) if persona_assignment_store_enabled(cfg) else []
    data = {
        "ok": True,
        "feature_enabled": True,
        "persona_instance": persona_instance_summary(instance, personas_by_id.get(str(getattr(instance, "persona_id", "") or ""))),
        "assignments": [persona_assignment_summary(item) for item in assignments[-25:]],
    }
    if args.json:
        print(emit_json(data))
    else:
        summary = data["persona_instance"]
        print(f"{summary['persona_instance_id']}: {summary['display_name']} state={summary['state']}")
    return 0


def _cmd_persona_tool_diff(args) -> int:
    cfg = load_agent_runtime_config()
    persona = _persona_by_id(cfg, str(args.persona_id or ""))
    if persona is None:
        data = {"ok": False, "error": f"persona not found: {args.persona_id}"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    visibility = resolve_tool_visibility(
        persona,
        ToolVisibilityOptions(
            permission_mode=str(args.permission_mode or "profile_default"),
            permission_source="cli_preview",
            repo_scope=args.repo_scope,
            workdir=args.workdir,
            session_id=args.session_id,
            task_id=args.task_id,
            goal_id=args.goal_id,
        ),
    )
    data = {"ok": True, "tool_visibility": visibility}
    if args.json:
        print(emit_json(data))
    else:
        print(f"{visibility['persona_id']}: {visibility['final_tool_count']} tools")
        if visibility["blocked_tools"]:
            print("blocked:")
            for item in visibility["blocked_tools"]:
                print(f"  {item['name']} ({item['reason']})")
    return 0


def _cmd_persona_permission_set(args) -> int:
    cfg = load_agent_runtime_config()
    persona = _persona_by_id(cfg, str(args.persona_id or ""))
    if persona is None:
        data = {"ok": False, "error": f"persona not found: {args.persona_id}"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    expires_at = str(args.expires_at or "").strip() or None
    ttl_seconds = getattr(args, "ttl_seconds", None)
    if expires_at is None and ttl_seconds is not None and ttl_seconds > 0:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")
    record = ChatToolPermissionStore().set(
        persona_id=persona.id,
        session_id=str(args.session_id or ""),
        mode=str(args.mode or "profile_default"),
        reason=str(args.reason or ""),
        source="operator",
        expires_at=expires_at,
        turns_remaining=getattr(args, "turns", None),
    )
    data = {
        "ok": True,
        "permission": {
            "persona_id": record.persona_id,
            "session_id": record.session_id,
            "mode": record.mode,
            "reason": record.reason,
            "source": record.source,
            "updated_at": record.updated_at,
            "expires_at": record.expires_at or None,
            "turns_remaining": record.turns_remaining,
        },
        "permission_state": permission_state_for_chat(persona, session_id=record.session_id),
    }
    if args.json:
        print(emit_json(data))
    else:
        print(f"{record.persona_id}: {record.session_id} mode={record.mode}")
    return 0


def _cmd_persona_assignments(args) -> int:
    cfg = load_agent_runtime_config()
    if not persona_assignment_store_enabled(cfg):
        data = {"ok": False, "feature_enabled": persona_instance_runtime_enabled(cfg), "assignment_store_enabled": False, "error": "persona assignment store is disabled"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    store = PersonaAssignmentStore()
    goal_id = getattr(args, "goal_id", None) or getattr(args, "task_id", None)
    if goal_id:
        assignments = store.list_for_goal(goal_id)
    elif args.persona_id:
        assignments = store.list_for_persona(_normalize_cli_persona_id(args.persona_id))
    else:
        assignments = store.list_all()
    data = {
        "ok": True,
        "feature_enabled": persona_instance_runtime_enabled(cfg),
        "assignment_store_enabled": True,
        "assignments": [persona_assignment_summary(item) for item in assignments],
    }
    if args.json:
        print(emit_json(data))
    else:
        for item in data["assignments"]:
            print(f"{item['assignment_id']}: {item['persona_id']} {item['kind']} state={item['state']} task={item['task_id'] or '-'}")
    return 0


def _cmd_persona_message(args) -> int:
    cfg = load_agent_runtime_config()
    if not persona_assignment_store_enabled(cfg):
        data = {"ok": False, "feature_enabled": persona_instance_runtime_enabled(cfg), "assignment_store_enabled": False, "error": "persona assignment store is disabled"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    persona_id = _normalize_cli_persona_or_template_id(args.persona_id)
    try:
        task = TaskStore().get(args.task_id)
    except Exception:
        data = {"ok": False, "error": f"task not found: {args.task_id}"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    PersonaInstanceStore().derive_from_workers(ensure_persisted_personas(cfg), WorkerSessionStore().list_all())
    assignment = PersonaAssignmentStore().create_or_resume(
        PersonaAssignmentSpec(
            persona_id=persona_id,
            kind="operator_message",
            title=args.title,
            message=args.message,
            created_by=args.requested_by,
            task_id=task.id,
            stage_id=task.current_stage_id,
        )
    )
    data = {
        "ok": True,
        "assignment_id": assignment.id,
        "persona_instance_id": assignment.persona_instance_id,
        "persona_id": assignment.persona_id,
        "task_id": assignment.task_id,
        "state": assignment.state,
        "kind": assignment.kind,
    }
    print(emit_json(data) if args.json else f"queued {assignment.id} for {assignment.persona_id}")
    return 0


def _cmd_persona_instance_create(args) -> int:
    display_name = safe_assignment_text(getattr(args, "display_name", None), limit=120)
    kill_active = bool(getattr(args, "kill_active", False))
    add_instance = bool(getattr(args, "add_instance", False))
    placement_id = safe_assignment_token(getattr(args, "placement_id", None))
    cfg = load_agent_runtime_config()
    persona_id = _normalize_cli_persona_or_template_id(args.persona_id)
    persona = _persona_by_id(cfg, persona_id)
    coordinator_id = _coordinator_actor_id(args)
    coordinator_scope = None
    if coordinator_id and (display_name or add_instance):
        coordinator_scope = _coordinator_scope_from_args(args, cfg, persona)
        auth = authorize_coordinator_action(
            "persona.instance.create",
            coordinator_scope,
            actor=coordinator_id,
            coordinator_id=coordinator_id,
        )
        if not auth.ok:
            data = _coordinator_confirm_payload("persona.instance.create", coordinator_id, auth)
            print(emit_json(data) if args.json else data["status"])
            return 2
        coordinator_scope = auth.scope
    if display_name:
        if not persona_assignment_store_enabled(cfg):
            data = {"ok": False, "feature_enabled": persona_instance_runtime_enabled(cfg), "assignment_store_enabled": False, "error": "persona assignment store is disabled"}
            print(emit_json(data) if args.json else data["error"])
            return 2
        try:
            if add_instance:
                if not placement_id:
                    data = {"ok": False, "error": "placement_id is required when add_instance is true"}
                    print(emit_json(data) if args.json else data["error"])
                    return 2
                instance = PersonaInstanceStore().add_instance(
                    persona_id=persona_id,
                    placement_id=placement_id,
                    display_name=display_name or safe_assignment_text(args.title, limit=120) or persona_id,
                    session_id=getattr(args, "session_id", None),
                )
            else:
                instance = PersonaInstanceStore().create_operator_chat(
                    persona_id=persona_id,
                    display_name=display_name or safe_assignment_text(args.title, limit=120) or persona_id,
                    session_id=getattr(args, "session_id", None),
                    kill_active=kill_active,
                )
            if add_instance or coordinator_id:
                instance = _maybe_stamp_spawned_by(instance, coordinator_id=coordinator_id)
        except ChatBusyError as exc:
            data = _chat_busy_payload(exc)
            print(emit_json(data) if args.json else data["error"])
            return 2
        _ensure_persona_chat_session(
            session_db=_default_persona_session_db(),
            session_id=instance.session_id,
            persona_id=instance.persona_id,
            title=f"{instance.display_name} chat",
        )
        data = {
            "ok": True,
            "agent_profile_id": instance.id,
            "persona_instance_id": instance.id,
            "source_persona_id": instance.persona_id,
            "persona_id": instance.persona_id,
            "source_profile_id": instance.profile_id,
            "agent_profile_display_name": instance.display_name,
            "display_name": instance.display_name,
            "lifecycle_mode": instance.mode,
            "mode": instance.mode,
            "chat_session_id": instance.session_id,
            "session_id": instance.session_id,
            "chat_busy": False,
            "killed_previous": bool(kill_active),
            "add_instance": add_instance,
            "placement_id": placement_id or None,
            "coordinator_permission_scope": asdict(coordinator_scope) if coordinator_scope is not None else None,
            "next_expected": "agent profile created; refresh Harness snapshot for the profile, chat, and scene placement state",
        }
        print(emit_json(data) if args.json else f"created {instance.id} on chat {instance.session_id}")
        return 0
    return _queue_free_floating_assignment(
        persona_id=args.persona_id,
        title=args.title,
        message=args.message,
        requested_by=args.requested_by,
        json_output=args.json,
        auto_run=getattr(args, "auto_run", False),
        max_actions=getattr(args, "max_actions", 1),
        max_seconds=getattr(args, "max_seconds", 240.0),
        client_message_id=getattr(args, "client_message_id", None),
        session_id=getattr(args, "session_id", None),
        stream=getattr(args, "stream", False),
        kill_active=kill_active,
        add_instance=add_instance,
        placement_id=placement_id,
        spawned_by=coordinator_id if coordinator_id else ("operator" if add_instance else None),
        coordinator_permission_scope=coordinator_scope,
    )


def _cmd_persona_instance_open_chat(args) -> int:
    cfg = load_agent_runtime_config()
    if not persona_assignment_store_enabled(cfg):
        data = {"ok": False, "feature_enabled": persona_instance_runtime_enabled(cfg), "assignment_store_enabled": False, "error": "persona assignment store is disabled"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    persona_id = _normalize_cli_persona_or_template_id(args.persona_id)
    persona = _persona_by_id(cfg, persona_id)
    coordinator_id = _coordinator_actor_id(args)
    coordinator_scope = None
    if coordinator_id and bool(getattr(args, "add_instance", False)):
        coordinator_scope = _coordinator_scope_from_args(args, cfg, persona)
        auth = authorize_coordinator_action(
            "persona.instance.open_chat",
            coordinator_scope,
            actor=coordinator_id,
            coordinator_id=coordinator_id,
        )
        if not auth.ok:
            data = _coordinator_confirm_payload("persona.instance.open_chat", coordinator_id, auth)
            print(emit_json(data) if args.json else data["status"])
            return 2
        coordinator_scope = auth.scope
    elif coordinator_id and bool(getattr(args, "kill_active", False)):
        coordinator_scope = _coordinator_scope_from_args(args, cfg, persona)
        try:
            target = PersonaInstanceStore().get(persona_instance_id_for(persona_id))
        except Exception:
            target = None
        auth = authorize_coordinator_action(
            "persona.instance.close",
            coordinator_scope,
            target,
            actor=coordinator_id,
            coordinator_id=coordinator_id,
        )
        if not auth.ok:
            data = _coordinator_confirm_payload("persona.instance.close", coordinator_id, auth)
            print(emit_json(data) if args.json else data["status"])
            return 2
    try:
        if bool(getattr(args, "add_instance", False)):
            placement_id = safe_assignment_token(getattr(args, "placement_id", None))
            if not placement_id:
                data = {"ok": False, "error": "placement_id is required when add_instance is true"}
                print(emit_json(data) if args.json else data["error"])
                return 2
            instance = PersonaInstanceStore().add_instance(
                persona_id=persona_id,
                placement_id=placement_id,
                session_id=args.session_id,
            )
            instance = _maybe_stamp_spawned_by(instance, coordinator_id=coordinator_id)
        else:
            if not safe_assignment_text(getattr(args, "session_id", None), limit=200):
                data = {"ok": False, "error": "session_id is required unless add_instance is true"}
                print(emit_json(data) if args.json else data["error"])
                return 2
            instance = PersonaInstanceStore().open_chat(
                persona_id=persona_id,
                session_id=args.session_id,
                kill_active=bool(getattr(args, "kill_active", False)),
            )
    except ChatBusyError as exc:
        data = _chat_busy_payload(exc)
        print(emit_json(data) if args.json else data["error"])
        return 2
    data = {
        "ok": True,
        "persona_instance_id": instance.id,
        "persona_id": instance.persona_id,
        "mode": instance.mode,
        "session_id": instance.session_id,
        "chat_busy": False,
        "killed_previous": bool(getattr(args, "kill_active", False)),
        "add_instance": bool(getattr(args, "add_instance", False)),
        "placement_id": safe_assignment_token(getattr(args, "placement_id", None)) or None,
        "coordinator_permission_scope": asdict(coordinator_scope) if coordinator_scope is not None else None,
        "next_expected": "resume or send on this chat session to boot the persona instance history",
    }
    print(emit_json(data) if args.json else f"opened {instance.id} on chat {instance.session_id}")
    return 0


def _cmd_persona_chat_delete(args) -> int:
    cfg = load_agent_runtime_config()
    if not persona_assignment_store_enabled(cfg):
        data = {"ok": False, "feature_enabled": persona_instance_runtime_enabled(cfg), "assignment_store_enabled": False, "error": "persona assignment store is disabled"}
        print(emit_json(data) if args.json else data["error"])
        return 2

    session_id = safe_assignment_text(getattr(args, "session_id", None), limit=200)
    if not session_id:
        data = {"ok": False, "error": "session_id is required"}
        print(emit_json(data) if args.json else data["error"])
        return 2

    requested_persona = None
    raw_persona = safe_assignment_text(getattr(args, "persona_id", None), limit=160)
    if raw_persona:
        try:
            requested_persona = _normalize_cli_persona_or_template_id(raw_persona)
        except Exception:
            requested_persona = safe_assignment_token(raw_persona)
    requested_instance = safe_assignment_token(getattr(args, "persona_instance_id", None))

    deleted_session = False
    session_db = _default_persona_session_db()
    if session_db is not None:
        try:
            deleted_session = bool(session_db.delete_session(session_id, sessions_dir=get_hermes_home() / "sessions"))
        except TypeError:
            deleted_session = bool(session_db.delete_session(session_id))
        except Exception as exc:
            data = {
                "ok": False,
                "session_id": session_id,
                "error": f"failed to delete persona chat session: {exc}",
            }
            print(emit_json(data) if args.json else data["error"])
            return 2

    instance_store = PersonaInstanceStore()
    assignment_store = PersonaAssignmentStore()
    cleared_bindings: list[str] = []
    closed_assignment_ids: list[str] = []
    for instance in instance_store.list_all():
        if safe_assignment_text(getattr(instance, "session_id", None), limit=200) != session_id:
            continue
        if requested_instance and instance.id != requested_instance:
            continue
        if requested_persona and instance.persona_id != requested_persona:
            continue

        assignment_id = safe_assignment_token(getattr(instance, "current_assignment_id", None))
        if assignment_id:
            try:
                assignment = assignment_store.get(assignment_id)
                if (
                    assignment.task_id is None
                    and assignment.evidence_kind == "free_floating"
                    and assignment.state not in {"completed", "blocked", "cancelled"}
                ):
                    closed = assignment_store.complete(
                        assignment.id,
                        state="cancelled",
                        error=f"deleted persona chat session {session_id}",
                    )
                    closed_assignment_ids.append(closed.id)
            except Exception:
                pass

        instance.session_id = None
        instance.current_assignment_id = None
        instance.active_worker_session_id = None
        instance.active_run_id = None
        if instance.mode in {"chat", "free_floating"}:
            instance.mode = "configured"
        instance_store.update(instance)
        cleared_bindings.append(instance.id)

    if not deleted_session and not cleared_bindings:
        data = {
            "ok": False,
            "status": "not_found",
            "session_id": session_id,
            "deleted_session": False,
            "cleared_bindings": [],
            "error": f"persona chat session not found: {session_id}",
            "next_expected": "refresh Harness snapshot; if the row is still visible, inspect SessionDB source and persona_instance.session_id",
        }
        print(emit_json(data) if args.json else data["error"])
        return 2

    try:
        EventLog().append(
            Event(
                id=f"evt_{uuid.uuid4().hex[:12]}",
                type="persona_chat.deleted",
                persona_id=requested_persona or "persona",
                task_id=None,
                run_id=None,
                ts=now(),
                payload={
                    "session_id": session_id,
                    "deleted_session": deleted_session,
                    "cleared_bindings": cleared_bindings,
                    "closed_assignment_ids": closed_assignment_ids,
                    "requested_by": safe_assignment_text(getattr(args, "requested_by", None), limit=120) or "cli",
                },
            )
        )
    except Exception:
        pass

    data = {
        "ok": True,
        "session_id": session_id,
        "deleted_session": deleted_session,
        "cleared_bindings": cleared_bindings,
        "cleared_binding_count": len(cleared_bindings),
        "closed_assignment_ids": closed_assignment_ids,
        "next_expected": "refresh Harness snapshot; deleted persona chat should be absent and active bindings should be cleared",
    }
    print(emit_json(data) if args.json else f"deleted persona chat {session_id}")
    return 0


def _chat_busy_payload(exc: ChatBusyError) -> dict[str, object]:
    return {
        "ok": False,
        "status": "chat_busy",
        "chat_busy": True,
        "error": "chat_busy",
        "persona_instance_id": exc.instance.id,
        "persona_id": exc.instance.persona_id,
        "active_run_id": exc.active_run_id,
        "active_worker_session_id": exc.active_worker_session_id,
        "next_expected": "choose add_instance to keep the current chat, or retry with kill_active to cancel the current run/worker and replace it",
    }


def _coordinator_actor_id(args) -> str | None:
    raw = str(getattr(args, "requested_by", "") or "").strip()
    if raw.lower().startswith("coordinator:"):
        return safe_assignment_token(raw.split(":", 1)[1])
    if raw.lower() == "coordinator":
        return safe_assignment_token(getattr(args, "coordinator_id", "neko_supervisor"))
    return None


def _coordinator_scope_from_args(args, cfg, persona: AgentPersona | None) -> CoordinatorPermissionScope:
    scope = scope_for_persona(
        persona,
        config=getattr(cfg, "coordinator_permissions", None),
        spawns_used=int(getattr(args, "coordinator_spawns_used", 0) or 0),
    )
    max_spawns = getattr(args, "coordinator_max_spawns", None)
    if max_spawns is not None:
        scope.max_spawns = max(0, int(max_spawns))
    may_kill_own = getattr(args, "coordinator_may_kill_own", None)
    no_kill_own = getattr(args, "coordinator_no_kill_own", None)
    if may_kill_own is not None:
        scope.may_kill_own = bool(may_kill_own)
    if no_kill_own is not None:
        scope.may_kill_own = not bool(no_kill_own)
    may_kill_others = getattr(args, "coordinator_may_kill_others", None)
    if may_kill_others is not None:
        scope.may_kill_others = bool(may_kill_others)
    return scope


def _coordinator_confirm_payload(action: str, coordinator_id: str, auth) -> dict[str, object]:
    return {
        "ok": False,
        "status": "needs_operator_confirm",
        "needs_operator_confirm": True,
        "action": action,
        "coordinator_id": coordinator_id,
        "reason": auth.reason,
        "permission_scope": asdict(auth.scope) if auth.scope is not None else None,
        "next_expected": "operator confirmation or a wider coordinator permission scope is required before this warning/destructive action can run",
    }


def _maybe_stamp_spawned_by(instance, *, coordinator_id: str | None, operator_source: str = "operator"):
    source = safe_assignment_token(coordinator_id) if coordinator_id else operator_source
    if not source:
        return instance
    instance.spawned_by = source
    return PersonaInstanceStore().update(instance)


def _cmd_persona_instance_message(args) -> int:
    return _queue_free_floating_assignment(
        persona_id=_persona_id_from_instance_id(args.persona_instance_id),
        title=args.title,
        message=args.message,
        requested_by=args.requested_by,
        json_output=args.json,
        persona_instance_id=args.persona_instance_id,
        auto_run=getattr(args, "auto_run", False),
        max_actions=getattr(args, "max_actions", 1),
        max_seconds=getattr(args, "max_seconds", 240.0),
        client_message_id=getattr(args, "client_message_id", None),
        session_id=getattr(args, "session_id", None),
        stream=getattr(args, "stream", False),
    )


def _cmd_mission_chat_steer(args) -> int:
    session_id = safe_assignment_text(getattr(args, "session_id", None), limit=200)
    client_message_id = safe_assignment_text(getattr(args, "client_message_id", None), limit=200)
    message = safe_assignment_text(getattr(args, "message", None), limit=12000)
    if not session_id or not client_message_id or not message:
        data = {
            "ok": False,
            "capability_id": "mission.chat.steer",
            "execution_state": "rejected",
            "session_id": session_id,
            "client_message_id": client_message_id,
            "error_kind": "invalid_request",
            "error": "session_id, client_message_id, and non-empty message are required",
        }
        print(emit_json(data) if args.json else data["error"])
        return 2
    try:
        data = submit_mission_chat_steer(
            runtime_root=paths.store_root(),
            session_id=session_id,
            message=message,
            client_message_id=client_message_id,
            persona_id=safe_assignment_token(getattr(args, "persona_id", None)) or None,
            persona_instance_id=safe_assignment_token(getattr(args, "persona_instance_id", None)) or None,
        )
    except ValueError as exc:
        data = {
            "ok": False,
            "capability_id": "mission.chat.steer",
            "execution_state": "rejected",
            "session_id": session_id,
            "client_message_id": client_message_id,
            "error_kind": "invalid_request",
            "error": safe_assignment_text(str(exc), limit=240),
        }
        print(emit_json(data) if args.json else data["error"])
        return 2
    print(emit_json(data) if args.json else (data.get("error") or data.get("execution_state") or "accepted"))
    return 0


def _cmd_mission_chat_message(args) -> int:
    cfg = load_agent_runtime_config()
    normalized_persona = _normalize_cli_persona_or_template_id(args.persona_id)
    persona = _persona_by_id(cfg, normalized_persona)
    if persona is None:
        data = {"ok": False, "error": f"unknown persona {safe_assignment_token(args.persona_id)}"}
        if getattr(args, "stream", False):
            _emit_chat_final(data)
        else:
            print(emit_json(data) if args.json else data["error"])
        return 2

    session_db = _default_persona_session_db()
    instance_store = PersonaInstanceStore()
    instance_store.derive_from_workers(ensure_persisted_personas(cfg), WorkerSessionStore().list_all())
    persona_instance_id = safe_assignment_token(getattr(args, "persona_instance_id", None))
    session_id = safe_assignment_text(getattr(args, "session_id", None), limit=200)
    if not session_id:
        session_id = _persona_chat_session_id(persona_instance_id or persona_instance_id_for(normalized_persona))
    display_name = safe_assignment_text(getattr(persona, "display_name", None), limit=120) or _display_name_for_profile(normalized_persona)
    try:
        instance = instance_store.open_chat(
            persona_id=normalized_persona,
            persona_instance_id=persona_instance_id or None,
            session_id=session_id,
            display_name=display_name,
            profile_id=safe_assignment_token(getattr(persona, "hermes_profile", None)),
            kill_active=False,
        )
    except ChatBusyError as exc:
        data = _chat_busy_payload(exc)
        if getattr(args, "stream", False):
            _emit_chat_final(data)
        else:
            print(emit_json(data) if args.json else data["error"])
        return 2
    except ValueError as exc:
        data = {"ok": False, "error": safe_assignment_text(str(exc), limit=240)}
        if getattr(args, "stream", False):
            _emit_chat_final(data)
        else:
            print(emit_json(data) if args.json else data["error"])
        return 2

    task_id = safe_assignment_token(getattr(args, "task_id", None))
    goal_id = safe_assignment_token(getattr(args, "goal_id", None))
    if task_id or goal_id:
        instance.current_task_id = task_id or instance.current_task_id
        instance.goal_id = goal_id or task_id or instance.goal_id
        instance.mode = "task_bound"
        instance = instance_store.update(instance)

    _ensure_persona_chat_session(
        session_db=session_db,
        session_id=session_id,
        persona_id=normalized_persona,
        title=f"{instance.display_name} chat",
    )
    try:
        requested_override = _requested_chat_model_override(args)
        chat_override = _resolve_chat_model_override(
            session_db=session_db,
            session_id=session_id,
            requested_override=requested_override,
        )
        model_selection = _chat_effective_model_payload(
            persona=persona,
            config=cfg,
            override=chat_override,
        )
    except ValueError as exc:
        data = {
            "ok": False,
            "error_kind": "invalid_chat_model_override",
            "error": safe_assignment_text(str(exc), limit=320),
            "persona_instance_id": instance.id,
            "persona_id": normalized_persona,
            "session_id": session_id,
            "chat_session_id": session_id,
            "next_expected": "choose a valid provider/model id or clear the chat-scoped override; Hermes profile defaults were not changed",
        }
        if getattr(args, "stream", False):
            _emit_chat_final(data)
        else:
            print(emit_json(data) if args.json else data["error"])
        return 2
    except Exception as exc:
        data = {
            "ok": False,
            "error_kind": "chat_model_override_persist_failed",
            "error": safe_assignment_text(str(exc), limit=320) or type(exc).__name__,
            "persona_instance_id": instance.id,
            "persona_id": normalized_persona,
            "session_id": session_id,
            "chat_session_id": session_id,
            "next_expected": "inspect Harness session metadata storage; chat-scoped model override was not applied and Hermes profile defaults were not changed",
        }
        if getattr(args, "stream", False):
            _emit_chat_final(data)
        else:
            print(emit_json(data) if args.json else data["error"])
        return 2
    message = safe_assignment_text(getattr(args, "message", None), limit=12000)
    if not message:
        data = {"ok": False, "error": "message is required"}
        if getattr(args, "stream", False):
            _emit_chat_final(data)
        else:
            print(emit_json(data) if args.json else data["error"])
        return 2

    client_message_id = safe_assignment_text(
        getattr(args, "client_message_id", None), limit=200
    )
    replay = _persona_chat_existing_turn(
        session_db=session_db,
        session_id=session_id,
        client_message_id=client_message_id,
    )
    if replay.get("assistant"):
        reply_text = _redact_persona_chat_text(
            replay["assistant"].get("content"), limit=8000
        )
        data = {
            "ok": True,
            "capability_id": "mission.chat.message",
            "agent_profile_id": instance.id,
            "persona_instance_id": instance.id,
            "persona_id": normalized_persona,
            "session_id": session_id,
            "chat_session_id": session_id,
            "task_id": task_id,
            "goal_id": goal_id,
            "client_message_id": client_message_id,
            "execution_state": "completed",
            "kind": "mission_chat_message",
            "intent_hint": safe_assignment_token(getattr(args, "intent_hint", None))
            or "chat",
            "surface_prompt": safe_assignment_text(
                getattr(args, "surface_prompt", ""), limit=4000
            )
            or "",
            "limiting_wrapper_active": False,
            "reply": reply_text,
            "turn_id": safe_assignment_token(client_message_id),
            "run_ids": [],
            "model_selection": model_selection,
            "idempotent_replay": True,
            "next_expected": "duplicate client message id replayed from the canonical Mission Control chat transcript",
        }
        if getattr(args, "stream", False):
            _emit_chat_final(data)
        else:
            print(
                emit_json(data)
                if args.json
                else f"mission chat reply for {normalized_persona}"
            )
        return 0

    _append_persona_operator_turn(
        session_db=session_db,
        session_id=session_id,
        message=message,
        client_message_id=client_message_id,
        skip_if_present=bool(replay.get("operator")),
    )
    chat_message = _persona_chat_message_with_history(
        session_db=session_db,
        session_id=session_id,
        message=message,
    )
    queued_skills = consume_skills_for_next_turn(
        persona_id=normalized_persona,
        session_id=session_id,
    )
    preloaded_skill_prompt = ""
    preloaded_skills_loaded: list[str] = []
    preloaded_skills_missing: list[str] = []
    if queued_skills:
        try:
            from agent.skill_commands import build_preloaded_skills_prompt

            preloaded_skill_prompt, preloaded_skills_loaded, preloaded_skills_missing = (
                build_preloaded_skills_prompt(
                    queued_skills,
                    task_id=session_id,
                )
            )
        except Exception:
            preloaded_skill_prompt = ""
            preloaded_skills_loaded = []
            preloaded_skills_missing = list(queued_skills)
    prompt_context = mission_chat_prompt_observability(
        persona=persona,
        persona_instance_id=instance.id,
        session_id=session_id,
        task_id=task_id,
        goal_id=goal_id,
        turn_id=safe_assignment_token(client_message_id),
        surface_prompt=getattr(args, "surface_prompt", "") or "",
        limiting_wrapper_active=False,
        session_db=session_db,
        current_message=message,
        model_selection=model_selection,
    )
    stream_emitter = (
        _ChatProtocolV2Emitter(
            turn_id=safe_assignment_token(client_message_id),
            client_message_id=client_message_id,
            on_update=lambda emitter: persist_mission_chat_turn(
                session_id=session_id,
                client_message_id=client_message_id,
                turn_id=emitter.turn_id,
                elements=emitter.elements,
            ),
        )
        if getattr(args, "stream", False)
        else None
    )

    def _stream_delta(delta: str | None) -> None:
        _emit_chat_delta(delta)
        if stream_emitter is not None:
            stream_emitter.delta(delta)

    trace_payloads: list[dict[str, object]] = []

    def _stream_progress(payload: dict[str, object] | None) -> None:
        if payload:
            trace_payloads.append(payload)
        if stream_emitter is not None:
            stream_emitter.progress(payload)

    def _agent_ready_for_steer(agent):
        if not getattr(args, "stream", False):
            return None
        handle = start_active_mission_chat_turn(
            runtime_root=paths.store_root(),
            session_id=session_id,
            agent=agent,
            persona_id=normalized_persona,
            persona_instance_id=instance.id,
            client_message_id=client_message_id,
        )
        return handle.close

    try:
        chat_result = GPTPersonaRuntime(
            default_provider=cfg.default_provider,
            default_model=cfg.default_model,
            session_db=session_db,
            persist_agent_session=False,
        ).mission_chat_reply(
            persona,
            chat_message,
            session_id=None,
            permission_session_id=session_id,
            provider_override=model_selection.get("effective_provider"),
            model_override=model_selection.get("effective_model"),
            surface_prompt=getattr(args, "surface_prompt", "") or "",
            max_wall_seconds=getattr(args, "max_seconds", 240.0),
            stream_callback=_stream_delta if getattr(args, "stream", False) else None,
            pre_trace_callback=lambda payload: _append_persona_pre_trace_ack(
                session_db=session_db,
                session_id=session_id,
                trace_payload=payload,
            ),
            trace_callback=_stream_progress,
            agent_ready_callback=_agent_ready_for_steer,
            preloaded_skill_prompt=preloaded_skill_prompt,
        )
        final_model_input = (getattr(chat_result, "raw", {}) or {}).get("model_input_observability")
        prompt_context = mission_chat_prompt_observability(
            persona=persona,
            persona_instance_id=instance.id,
            session_id=session_id,
            task_id=task_id,
            goal_id=goal_id,
            turn_id=safe_assignment_token(client_message_id),
            surface_prompt=getattr(args, "surface_prompt", "") or "",
            limiting_wrapper_active=False,
            session_db=session_db,
            current_message=message,
            final_model_input=final_model_input,
            model_selection=model_selection,
            trace_events=trace_payloads,
        )
        if preloaded_skills_loaded:
            prompt_context["used_skills"] = prompt_context.get("used_skills") or []
            existing = {
                safe_assignment_token(item.get("name"))
                for item in prompt_context["used_skills"]
                if isinstance(item, dict)
            }
            for skill in preloaded_skills_loaded:
                token = safe_assignment_token(skill)
                if token and token not in existing:
                    prompt_context["used_skills"].append(
                        {
                            "name": token,
                            "kind": "skill",
                            "status": "used",
                            "hash_tracked": False,
                            "source": "queued_next_turn_skill",
                        }
                    )
        if preloaded_skills_missing:
            prompt_context["queued_skill_load_errors"] = [
                {
                    "name": safe_assignment_token(skill) or str(skill),
                    "status": "missing",
                    "source": "queued_next_turn_skill",
                }
                for skill in preloaded_skills_missing
            ]
        persist_tool_turn_actual(
            persona_id=normalized_persona,
            session_id=session_id,
            task_id=task_id,
            goal_id=goal_id,
            turn_id=safe_assignment_token(client_message_id),
            model_input=prompt_context.get("final_model_input"),
        )
        try:
            persist_prompt_observability_context(prompt_context)
        except Exception as persist_exc:
            prompt_context = {
                **prompt_context,
                "observability_persist_error": safe_assignment_text(type(persist_exc).__name__, limit=80),
            }
    except Exception as exc:
        if stream_emitter is not None:
            stream_emitter.finish(state="failed")
        data = {
            "ok": False,
            "persona_instance_id": instance.id,
            "persona_id": normalized_persona,
            "session_id": session_id,
            "blocker": safe_assignment_text(str(exc), limit=240),
            "prompt_context_id": prompt_context["context_id"],
            "prompt_observability": prompt_context,
            "model_selection": model_selection,
            "next_expected": "fix the runtime blocker and retry the mission chat turn",
        }
        if getattr(args, "stream", False):
            _emit_chat_final(data)
        else:
            print(emit_json(data) if args.json else data["blocker"])
        return 2

    reply_text = _redact_persona_chat_text(getattr(chat_result, "final_response", "") or "", limit=8000)
    _append_persona_assistant_text(
        session_db=session_db,
        session_id=session_id,
        text=reply_text,
        client_message_id=client_message_id,
    )
    if stream_emitter is not None:
        persist_mission_chat_turn(
            session_id=session_id,
            client_message_id=client_message_id,
            turn_id=stream_emitter.turn_id,
            elements=stream_emitter.elements,
        )
    _update_persona_chat_token_counts(
        session_db=session_db,
        session_id=session_id,
        result=chat_result,
    )
    _maybe_auto_title_persona_chat(
        session_db=session_db,
        session_id=session_id,
        user_message=message,
        assistant_response=reply_text,
    )
    try:
        instance.active_run_id = None
        instance.current_assignment_id = None
        instance.state = WorkerSessionState.IDLE
        instance.session_id = session_id
        instance_store.update(instance)
    except Exception:
        pass

    data = {
        "ok": True,
        "protocol_version": 2 if stream_emitter is not None else None,
        "capability_id": "mission.chat.message",
        "agent_profile_id": instance.id,
        "persona_instance_id": instance.id,
        "persona_id": normalized_persona,
        "session_id": session_id,
        "chat_session_id": session_id,
        "task_id": task_id,
        "goal_id": goal_id,
        "client_message_id": client_message_id,
        "execution_state": "completed",
        "kind": "mission_chat_message",
        "intent_hint": safe_assignment_token(getattr(args, "intent_hint", None)) or "chat",
        "surface_prompt": safe_assignment_text(getattr(args, "surface_prompt", ""), limit=4000) or "",
        "limiting_wrapper_active": False,
        "reply": reply_text,
        "turn_id": safe_assignment_token(client_message_id),
        "run_ids": [],
        "input_tokens": getattr(chat_result, "input_tokens", None),
        "output_tokens": getattr(chat_result, "output_tokens", None),
        "total_tokens": getattr(chat_result, "total_tokens", None),
        "prompt_context_id": prompt_context["context_id"],
        "prompt_observability": prompt_context,
        "queued_skills_loaded": preloaded_skills_loaded,
        "queued_skills_missing": preloaded_skills_missing,
        "model_selection": model_selection,
        "next_expected": "agent replied through the canonical Mission Control chat path; refresh Harness snapshot for transcript and Initial Chat Context",
    }
    if getattr(args, "stream", False):
        if stream_emitter is not None:
            data["turn_elements"] = stream_emitter.elements
            stream_emitter.finish(
                state="completed",
                input_tokens=data.get("input_tokens"),
                output_tokens=data.get("output_tokens"),
                total_tokens=data.get("total_tokens"),
            )
        _emit_chat_final(data)
    else:
        print(emit_json(data) if args.json else f"mission chat reply for {normalized_persona}")
    return 0


def _cmd_mission_chat_queue_skill(args) -> int:
    persona_id = safe_assignment_token(getattr(args, "persona_id", None))
    session_id = safe_assignment_token(getattr(args, "session_id", None))
    skill = safe_assignment_token(getattr(args, "skill", None))
    if not persona_id or not session_id or not skill:
        data = {
            "ok": False,
            "error": "persona, session-id, and skill are required",
        }
        print(emit_json(data) if args.json else data["error"])
        return 2
    try:
        from tools.skills_tool import _find_all_skills

        candidates = _find_all_skills()
    except Exception as exc:
        data = {
            "ok": False,
            "error": "skill catalog is not available",
            "error_kind": type(exc).__name__,
        }
        print(emit_json(data) if args.json else data["error"])
        return 2
    available = {
        str(item.get(key) or "").strip()
        for item in candidates
        if isinstance(item, dict)
        for key in ("name", "identifier")
    }
    if skill not in available:
        data = {
            "ok": False,
            "error": f"skill is not loadable: {skill}",
        }
        print(emit_json(data) if args.json else data["error"])
        return 2
    queued = queue_skill_for_next_turn(
        persona_id=persona_id,
        session_id=session_id,
        persona_instance_id=getattr(args, "persona_instance_id", None),
        skill=skill,
    )
    data = {
        "ok": True,
        "capability_id": "mission.chat.queue_skill_for_next_turn",
        "persona_id": persona_id,
        "persona_instance_id": safe_assignment_token(getattr(args, "persona_instance_id", None)),
        "session_id": session_id,
        "skill": skill,
        "queued_skills": queued.get("skills", []),
        "next_expected": "send the next Mission Control chat message; queued skills will be preloaded for that turn only",
    }
    print(emit_json(data) if args.json else f"queued {skill} for next turn")
    return 0


def _cmd_persona_instance_close(args) -> int:
    cfg = load_agent_runtime_config()
    coordinator_id = _coordinator_actor_id(args)
    if coordinator_id:
        try:
            target = PersonaInstanceStore().get(args.persona_instance_id)
            persona = _persona_by_id(cfg, target.persona_id)
        except Exception:
            target = None
            persona = None
        scope = _coordinator_scope_from_args(args, cfg, persona)
        auth = authorize_coordinator_action(
            "persona.instance.close",
            scope,
            target,
            actor=coordinator_id,
            coordinator_id=coordinator_id,
        )
        if not auth.ok:
            data = _coordinator_confirm_payload("persona.instance.close", coordinator_id, auth)
            print(emit_json(data) if args.json else data["status"])
            return 2
    return _close_free_floating_assignments(args.persona_instance_id, reason=args.reason, json_output=args.json, terminal_state="cancelled")


def _cmd_persona_instance_archive(args) -> int:
    return _close_free_floating_assignments(args.persona_instance_id, reason=args.reason, json_output=args.json, terminal_state="completed")


def _cmd_persona_instance_steer(args) -> int:
    cfg = load_agent_runtime_config()
    if not persona_assignment_store_enabled(cfg):
        data = {"ok": False, "feature_enabled": persona_instance_runtime_enabled(cfg), "assignment_store_enabled": False, "error": "persona assignment store is disabled"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    persona_instance_id = safe_assignment_token(args.persona_instance_id)
    if not persona_instance_id:
        data = {"ok": False, "error": "persona_instance_id is required"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    detach = bool(getattr(args, "detach", False))
    parent_instance_id = None if detach else safe_optional_token(getattr(args, "parent_instance_id", None))
    goal_id = None if detach else safe_optional_token(getattr(args, "goal_id", None))
    if not detach and not parent_instance_id:
        data = {"ok": False, "error": "--parent is required unless --detach is set"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    if not detach and parent_instance_id == persona_instance_id:
        data = {"ok": False, "error": "a persona instance cannot steer itself"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    store = PersonaInstanceStore()
    try:
        target = store.get(persona_instance_id)
    except Exception:
        data = {"ok": False, "error": f"persona instance not found: {persona_instance_id}"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    # 76D.3: re-routing a steering edge is a STEER verb (ungated); operator
    # actors bypass entirely. Coordinators still pass through the authorizer so
    # the contract stays uniform with create/kill paths.
    coordinator_id = _coordinator_actor_id(args)
    if coordinator_id:
        persona = _persona_by_id(cfg, target.persona_id)
        scope = _coordinator_scope_from_args(args, cfg, persona)
        auth = authorize_coordinator_action("re_route", scope, target, actor=coordinator_id, coordinator_id=coordinator_id)
        if not auth.ok:
            data = _coordinator_confirm_payload("re_route", coordinator_id, auth)
            print(emit_json(data) if args.json else data["status"])
            return 2
    try:
        updated = store.steer(persona_instance_id, parent_instance_id=parent_instance_id, goal_id=goal_id, detach=detach)
    except ValueError as exc:
        data = {"ok": False, "error": str(exc)}
        print(emit_json(data) if args.json else data["error"])
        return 2
    try:
        persona = _persona_by_id(cfg, updated.persona_id)
    except Exception:
        persona = None
    data = {"ok": True, "detached": detach, "instance": persona_instance_summary(updated, persona)}
    print(emit_json(data) if args.json else f"steered {persona_instance_id}: parent={updated.spawned_by} goal={updated.goal_id}")
    return 0


def _cmd_persona_instance_return_summary(args) -> int:
    try:
        data = return_summary_to_parent_session(
            args.persona_instance_id,
            parent_session_id=args.parent_session_id,
            summary=args.summary,
            proof_ids=list(getattr(args, "proof_ids", []) or []),
            artifact_refs=list(getattr(args, "artifact_refs", []) or []),
            task_id=getattr(args, "task_id", None),
            stage_id=getattr(args, "stage_id", None),
        )
    except Exception as exc:
        data = {"ok": False, "capability_id": "persona.instance.return_summary", "error": safe_assignment_text(str(exc), limit=240)}
        print(emit_json(data) if args.json else data["error"])
        return 2
    print(emit_json(data) if args.json else f"returned {data['persona_instance_id']} -> {data['parent_session_id']}")
    return 0


def _cmd_persona_instance_update_profile(args) -> int:
    cfg = load_agent_runtime_config()
    if not persona_assignment_store_enabled(cfg):
        data = {"ok": False, "feature_enabled": persona_instance_runtime_enabled(cfg), "assignment_store_enabled": False, "error": "persona assignment store is disabled"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    persona_instance_id = safe_assignment_token(args.persona_instance_id)
    if not persona_instance_id:
        data = {"ok": False, "error": "persona_instance_id is required"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    store = PersonaInstanceStore()
    try:
        target = store.get(persona_instance_id)
    except Exception:
        data = {"ok": False, "error": f"persona instance not found: {persona_instance_id}"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    coordinator_id = _coordinator_actor_id(args)
    if coordinator_id:
        persona = _persona_by_id(cfg, target.persona_id)
        scope = _coordinator_scope_from_args(args, cfg, persona)
        auth = authorize_coordinator_action("persona.instance.update_profile", scope, target, actor=coordinator_id, coordinator_id=coordinator_id)
        if not auth.ok:
            data = _coordinator_confirm_payload("persona.instance.update_profile", coordinator_id, auth)
            print(emit_json(data) if args.json else data["status"])
            return 2
    try:
        updated = store.update_profile(
            persona_instance_id,
            display_name=getattr(args, "display_name", None),
            current_chat_goal=getattr(args, "current_chat_goal", None),
            goal_id=getattr(args, "goal_id", None),
            skills=list(getattr(args, "skills", None) or []),
            clear_skills=bool(getattr(args, "clear_skills", False)),
        )
    except ValueError as exc:
        data = {"ok": False, "error": str(exc)}
        print(emit_json(data) if args.json else data["error"])
        return 2
    try:
        persona = _persona_by_id(cfg, updated.persona_id)
    except Exception:
        persona = None
    data = {
        "ok": True,
        "persona_instance_id": updated.id,
        "persona_id": updated.persona_id,
        "backing_profile": updated.profile_id,
        "updated_instance": persona_instance_summary(updated, persona),
        "next_expected": "refresh Harness snapshot; runtime instance overrides should be visible without modifying the backing Hermes profile",
    }
    print(emit_json(data) if args.json else f"updated runtime profile {updated.id}")
    return 0


def _cmd_persona_instance_run_once(args) -> int:
    cfg = load_agent_runtime_config()
    if not persona_assignment_store_enabled(cfg):
        data = {"ok": False, "feature_enabled": persona_instance_runtime_enabled(cfg), "assignment_store_enabled": False, "error": "persona assignment store is disabled"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    persona_instance_id = safe_assignment_token(args.persona_instance_id)
    persona_id = _persona_id_from_instance_id(persona_instance_id)
    active = [
        item
        for item in PersonaAssignmentStore().find_active(persona_id=persona_id, kind="free_floating_message")
        if item.persona_instance_id == persona_instance_id and item.task_id is None
    ]
    seed = active[-1] if active else None
    message = args.message or (seed.message if seed else "Run one bounded free-floating persona sandbox turn.")
    title = args.title or (seed.title if seed else "Free-floating persona run")
    os.environ.setdefault("HERMES_AGENT_RUNTIME_ROOT", str(paths.store_root()))
    try:
        result = PersonaDiagnosticController(
            config=cfg,
            engine_factory=lambda **kwargs: TickEngine(
                **kwargs,
                persona_runtime=GPTPersonaRuntime(default_provider=cfg.default_provider, default_model=cfg.default_model),
            ),
        ).diagnose(
            PersonaDiagnosticOptions(
                persona_id=persona_id,
                title=title,
                message=message,
                requested_by=args.requested_by,
                operation_kind="free_floating",
                operation_mode="sandbox_task",
                max_actions=args.max_actions,
                max_seconds=args.max_seconds,
                non_goals=["Not production proof"],
            )
        )
    except ValueError as exc:
        data = {"ok": False, "error": str(exc)}
        print(emit_json(data) if args.json else str(exc))
        return 2
    if seed is not None:
        store = PersonaAssignmentStore()
        for run_id in result.run_ids:
            store.attach_run(seed.id, run_id)
        store.complete(seed.id, state="completed")
    data = {
        **asdict(result),
        "ok": result.ok,
        "persona_instance_id": persona_instance_id,
        "production_proof_eligible": False,
        "evidence_kind": "free_floating",
        "archive_scope": "assignment",
    }
    print(emit_json(data) if args.json else f"free-floating persona run {result.task_id}: stop={result.stop_reason}")
    return result.exit_code


def _queue_free_floating_assignment(
    *,
    persona_id: str,
    title: str,
    message: str,
    requested_by: str,
    json_output: bool,
    persona_instance_id: str | None = None,
    auto_run: bool = False,
    max_actions: int = 1,
    max_seconds: float = 240.0,
    client_message_id: str | None = None,
    session_id: str | None = None,
    stream: bool = False,
    kill_active: bool = False,
    add_instance: bool = False,
    placement_id: str | None = None,
    spawned_by: str | None = None,
    coordinator_permission_scope: CoordinatorPermissionScope | None = None,
) -> int:
    cfg = load_agent_runtime_config()
    if not persona_assignment_store_enabled(cfg):
        data = {"ok": False, "feature_enabled": persona_instance_runtime_enabled(cfg), "assignment_store_enabled": False, "error": "persona assignment store is disabled"}
        if stream:
            _emit_chat_final(data)
        else:
            print(emit_json(data) if json_output else data["error"])
        return 2
    normalized_persona = _normalize_cli_persona_or_template_id(persona_id)
    instance_store = PersonaInstanceStore()
    instance_store.derive_from_workers(ensure_persisted_personas(cfg), WorkerSessionStore().list_all())
    if persona_instance_id is None:
        if add_instance:
            if not placement_id:
                data = {"ok": False, "error": "placement_id is required when add_instance is true"}
                if stream:
                    _emit_chat_final(data)
                else:
                    print(emit_json(data) if json_output else data["error"])
                return 2
            instance = instance_store.add_instance(
                persona_id=normalized_persona,
                placement_id=placement_id,
                display_name=safe_assignment_text(title, limit=120) or None,
            )
            instance = _maybe_stamp_spawned_by(instance, coordinator_id=spawned_by, operator_source="operator")
            persona_instance_id = instance.id
        else:
            persona_instance_id = instance_store.create_free_floating(normalized_persona).id
    assignment_store = PersonaAssignmentStore()
    assignment = assignment_store.create_or_resume(
        PersonaAssignmentSpec(
            persona_id=normalized_persona,
            persona_instance_id=persona_instance_id,
            kind="free_floating_message",
            title=title,
            message=message,
            created_by=requested_by,
            task_id=None,
            evidence_kind="free_floating",
            production_proof_eligible=False,
            archive_scope="assignment",
            client_message_id=client_message_id,
        )
    )
    session_db = _default_persona_session_db()
    try:
        session_id = _bind_free_floating_chat_session(
            instance_store=instance_store,
            session_db=session_db,
            persona_id=normalized_persona,
            persona_instance_id=assignment.persona_instance_id,
            assignment_id=assignment.id,
            session_id=session_id,
            kill_active=kill_active,
        )
    except ChatBusyError as exc:
        data = _chat_busy_payload(exc)
        if stream:
            _emit_chat_final(data)
        else:
            print(emit_json(data) if json_output else data["error"])
        return 2
    data = {
        "ok": True,
        "agent_profile_id": assignment.persona_instance_id,
        "assignment_id": assignment.id,
        "persona_instance_id": assignment.persona_instance_id,
        "persona_id": normalized_persona,
        "task_id": assignment.task_id,
        "state": assignment.state,
        "kind": assignment.kind,
        "evidence_kind": assignment.evidence_kind,
        "production_proof_eligible": assignment.production_proof_eligible,
        "archive_scope": assignment.archive_scope,
        "client_message_id": assignment.client_message_id,
        "execution_state": "queued",
        "lifecycle_mode": "free_floating",
        "auto_run": bool(auto_run),
        "chat_session_id": session_id,
        "session_id": session_id,
        "chat_busy": False,
        "killed_previous": bool(kill_active),
        "add_instance": bool(add_instance),
        "placement_id": placement_id or None,
        "coordinator_permission_scope": asdict(coordinator_permission_scope) if coordinator_permission_scope is not None else None,
        "turn_id": None,
        "run_ids": [],
        "next_expected": "agent turn queued; run harness persona instance run-once if auto_run is false",
    }
    exit_code = 0
    if auto_run:
        run_exit, run_payload = _run_free_floating_assignment_once(
            cfg=cfg,
            assignment_id=assignment.id,
            persona_instance_id=assignment.persona_instance_id,
            persona_id=normalized_persona,
            title=title,
            message=message,
            requested_by=requested_by,
            max_actions=max_actions,
            max_seconds=max_seconds,
            client_message_id=assignment.client_message_id,
            stream=stream,
        )
        data.update(run_payload)
        try:
            updated_assignment = assignment_store.get(assignment.id)
            data["state"] = updated_assignment.state
            data["run_ids"] = list(updated_assignment.run_ids or data.get("run_ids") or [])
        except Exception:
            pass
        exit_code = run_exit
    if stream:
        _emit_chat_final(data)
    else:
        print(emit_json(data) if json_output else f"queued free-floating {assignment.id} for {assignment.persona_id}")
    return exit_code


def _emit_chat_delta(delta: str | None) -> None:
    if not delta:
        return
    sys.stdout.write(json.dumps({"type": "chat.delta", "text": str(delta)}, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _emit_chat_final(payload: dict[str, object]) -> None:
    data = dict(payload)
    data["type"] = "chat.final"
    sys.stdout.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _emit_chat_frame(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class _ChatProtocolV2Emitter:
    """Additive Mission Control chat stream protocol.

    Legacy ``chat.delta``/``chat.final`` frames are still emitted by callers.
    These v2 frames give the Launcher stable ids and per-turn sequence order.
    """

    def __init__(self, *, turn_id: str | None, client_message_id: str | None, on_update=None):
        safe_turn = safe_assignment_token(turn_id) or f"turn_{uuid.uuid4().hex[:12]}"
        self.turn_id = safe_turn
        self.client_message_id = safe_assignment_text(client_message_id, limit=200) or None
        self._on_update = on_update
        self._seq = 0
        self._started_at = time.monotonic()
        self._current_segment: dict[str, object] | None = None
        self._segment_count = 0
        self._tool_count = 0
        self._active_tools: dict[str, list[dict[str, object]]] = {}
        self.elements: list[dict[str, object]] = []
        _emit_chat_frame(
            {
                "type": "turn.start",
                "protocol_version": 2,
                "turn_id": self.turn_id,
                "client_message_id": self.client_message_id,
            }
        )

    def delta(self, delta: str | None) -> None:
        if not delta:
            return
        segment = self._ensure_segment()
        text = str(delta)
        segment["text"] = str(segment.get("text") or "") + text
        _emit_chat_frame(
            {
                "type": "segment.delta",
                "protocol_version": 2,
                "turn_id": self.turn_id,
                "seq": segment["seq"],
                "id": segment["id"],
                "text": text,
            }
        )
        self._notify_update()

    def progress(self, payload: dict[str, object] | None) -> None:
        if not isinstance(payload, dict):
            return
        event_type = str(payload.get("type") or "run.progress")
        if event_type not in {"run.tool.started", "run.tool.finished"}:
            return
        self.end_segment(state="settled")
        if event_type == "run.tool.started":
            self._tool_started(payload)
        else:
            self._tool_finished(payload)
        self._notify_update()

    def end_segment(self, *, state: str = "settled") -> None:
        segment = self._current_segment
        if segment is None:
            return
        segment["state"] = state
        segment["duration_ms"] = _elapsed_ms(segment.get("started_at"))
        _emit_chat_frame(
            {
                "type": "segment.end",
                "protocol_version": 2,
                "turn_id": self.turn_id,
                "seq": segment["seq"],
                "id": segment["id"],
                "state": state,
                "duration_ms": segment["duration_ms"],
            }
        )
        self._current_segment = None
        self._notify_update()

    def finish(
        self,
        *,
        state: str,
        input_tokens: object = None,
        output_tokens: object = None,
        total_tokens: object = None,
    ) -> None:
        self.end_segment(state="settled" if state == "completed" else state)
        _emit_chat_frame(
            {
                "type": "turn.end",
                "protocol_version": 2,
                "turn_id": self.turn_id,
                "state": state,
                "duration_ms": _elapsed_ms(self._started_at),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            }
        )
        self._notify_update()

    def _ensure_segment(self) -> dict[str, object]:
        if self._current_segment is not None:
            return self._current_segment
        self._seq += 1
        self._segment_count += 1
        segment = {
            "turn_id": self.turn_id,
            "seq": self._seq,
            "id": f"{self.turn_id}_seg_{self._segment_count}",
            "kind": "segment",
            "seg_type": "answer" if self._tool_count else "plan",
            "state": "streaming",
            "text": "",
            "started_at": time.monotonic(),
        }
        segment["ttft_ms"] = _elapsed_ms(self._started_at)
        self._current_segment = segment
        self.elements.append(segment)
        _emit_chat_frame(
            {
                "type": "segment.start",
                "protocol_version": 2,
                "turn_id": self.turn_id,
                "seq": segment["seq"],
                "id": segment["id"],
                "seg_type": segment["seg_type"],
                "ttft_ms": segment["ttft_ms"],
            }
        )
        self._notify_update()
        return segment

    def _tool_started(self, payload: dict[str, object]) -> None:
        self._seq += 1
        self._tool_count += 1
        name = _tool_name_from_progress(payload)
        command = _safe_stream_text(payload.get("command_full")) or _safe_stream_text(
            payload.get("command_label")
        )
        tool = {
            "turn_id": self.turn_id,
            "seq": self._seq,
            "id": f"{self.turn_id}_tool_{self._tool_count}",
            "kind": "tool",
            "name": name,
            "state": "started",
            "args": _safe_stream_text(payload.get("summary")),
            "command": command,
            "status": _safe_stream_text(payload.get("status")),
            "summary": _safe_stream_text(payload.get("summary")),
        }
        self.elements.append(tool)
        self._active_tools.setdefault(name, []).append(tool)
        _emit_chat_frame(
            {
                "type": "tool.started",
                "protocol_version": 2,
                "turn_id": self.turn_id,
                "seq": tool["seq"],
                "id": tool["id"],
                "name": name,
                "args": _safe_stream_text(payload.get("summary")),
                "command": command,
            }
        )

    def _tool_finished(self, payload: dict[str, object]) -> None:
        name = _tool_name_from_progress(payload)
        stack = self._active_tools.get(name) or []
        tool = stack.pop() if stack else None
        if tool is None:
            self._seq += 1
            self._tool_count += 1
            tool = {
                "turn_id": self.turn_id,
                "seq": self._seq,
                "id": f"{self.turn_id}_tool_{self._tool_count}",
                "kind": "tool",
                "name": name,
            }
            self.elements.append(tool)
        tool["state"] = "finished"
        tool["status"] = _safe_stream_text(payload.get("status")) or "ok"
        tool["duration_ms"] = payload.get("duration_ms")
        files = payload.get("changed_files") or payload.get("files_touched") or []
        if isinstance(files, list):
            tool["files"] = [safe_assignment_text(item, limit=240) for item in files if safe_assignment_text(item, limit=240)]
        # Carry-through started command if the finished payload omits it.
        command = (
            _safe_stream_text(payload.get("command_full"))
            or _safe_stream_text(payload.get("command_label"))
            or tool.get("command")
        )
        if command:
            tool["command"] = command
        detail = _safe_stream_text(payload.get("detail"))
        if detail:
            tool["detail"] = detail
        output = _safe_stream_text(payload.get("output"), limit=8000)
        if output:
            tool["output"] = output
        exit_code = _safe_exit_code_value(payload.get("exit_code"))
        if exit_code is not None:
            tool["exit_code"] = exit_code
        _emit_chat_frame(
            {
                "type": "tool.finished",
                "protocol_version": 2,
                "turn_id": self.turn_id,
                "seq": tool["seq"],
                "id": tool["id"],
                "name": name,
                "status": tool["status"],
                "duration_ms": tool.get("duration_ms"),
                "files": tool.get("files") or [],
                "command": tool.get("command"),
                "detail": tool.get("detail"),
                "output": tool.get("output"),
                "exit_code": tool.get("exit_code"),
            }
        )

    def _notify_update(self) -> None:
        if self._on_update is None:
            return
        try:
            self._on_update(self)
        except Exception:
            pass


def _safe_stream_text(value: object, *, limit: int = 800) -> str | None:
    return safe_assignment_text(value, limit=limit) or None


def _safe_exit_code_value(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _elapsed_ms(started_at: object) -> int | None:
    try:
        started = float(started_at)
    except Exception:
        return None
    return max(0, int((time.monotonic() - started) * 1000))


def _tool_name_from_progress(payload: dict[str, object]) -> str:
    return safe_assignment_token(payload.get("tool_name") or payload.get("tool")) or "tool"


def _default_persona_session_db():
    try:
        from hermes_state import SessionDB

        return SessionDB()
    except Exception:
        return None


_CHAT_MODEL_OVERRIDE_CONFIG_KEY = "mission_control_chat_model_override"
_CHAT_PROVIDER_MODEL_RE = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,200}$")


def _safe_chat_model_override_value(value, *, field: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not _CHAT_PROVIDER_MODEL_RE.fullmatch(text):
        raise ValueError(
            f"{field} contains unsupported characters; only letters, numbers, '.', '_', '-', '+', '/', ':', and '@' are allowed"
        )
    return text


def _requested_chat_model_override(args) -> dict[str, object] | None:
    use_default = bool(getattr(args, "use_agent_default", False))
    provider = _safe_chat_model_override_value(getattr(args, "provider", None), field="provider")
    model = _safe_chat_model_override_value(getattr(args, "model", None), field="model")
    if use_default and (provider or model):
        raise ValueError("use_agent_default cannot be combined with provider or model")
    if use_default:
        return {
            "schema_version": 1,
            "clear": True,
            "source": "operator",
            "scope": "mission_control_chat_session",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    if not provider and not model:
        return None
    return {
        "schema_version": 1,
        "provider": provider,
        "model": model,
        "source": "operator",
        "scope": "mission_control_chat_session",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _session_model_config(session_db, session_id: str | None) -> dict[str, object]:
    if session_db is None or not session_id:
        return {}
    try:
        raw = session_db.get_session(session_id)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    model_config = raw.get("model_config")
    if isinstance(model_config, dict):
        return dict(model_config)
    if isinstance(model_config, str) and model_config.strip():
        try:
            decoded = json.loads(model_config)
        except Exception:
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def _persist_chat_model_override(
    *,
    session_db,
    session_id: str | None,
    override: dict[str, object] | None,
) -> dict[str, object]:
    current = _session_model_config(session_db, session_id)
    if override is not None:
        if override.get("clear") is True:
            current.pop(_CHAT_MODEL_OVERRIDE_CONFIG_KEY, None)
        else:
            current[_CHAT_MODEL_OVERRIDE_CONFIG_KEY] = override
    if session_db is None or not session_id:
        return current
    try:
        session_db.update_session_meta(
            session_id,
            json.dumps(current, sort_keys=True, separators=(",", ":")),
            model=(override or {}).get("model") if override else None,
        )
    except AttributeError:
        if hasattr(session_db, "sessions"):
            session = session_db.sessions.setdefault(session_id, {})
            session["model_config"] = json.dumps(current, sort_keys=True, separators=(",", ":"))
            if override and override.get("model"):
                session["model"] = override.get("model")
    return current


def _chat_model_override_from_config(model_config: dict[str, object]) -> dict[str, object] | None:
    raw = model_config.get(_CHAT_MODEL_OVERRIDE_CONFIG_KEY)
    if not isinstance(raw, dict):
        return None
    provider = _safe_chat_model_override_value(raw.get("provider"), field="provider")
    model = _safe_chat_model_override_value(raw.get("model"), field="model")
    if not provider and not model:
        return None
    return {
        "schema_version": 1,
        "provider": provider,
        "model": model,
        "source": safe_assignment_text(raw.get("source"), limit=80) or "session",
        "scope": "mission_control_chat_session",
        "updated_at": safe_assignment_text(raw.get("updated_at"), limit=80) or None,
    }


def _resolve_chat_model_override(
    *,
    session_db,
    session_id: str | None,
    requested_override: dict[str, object] | None,
) -> dict[str, object] | None:
    model_config = _persist_chat_model_override(
        session_db=session_db,
        session_id=session_id,
        override=requested_override,
    )
    return _chat_model_override_from_config(model_config)


def _chat_effective_model_payload(
    *,
    persona,
    config,
    override: dict[str, object] | None,
) -> dict[str, object]:
    default_provider = getattr(persona, "provider", None) or getattr(config, "default_provider", None)
    default_model = getattr(persona, "model", None) or getattr(config, "default_model", None)
    provider = (override or {}).get("provider") or default_provider
    model = (override or {}).get("model") or default_model
    return {
        "default_provider": default_provider,
        "default_model": default_model,
        "chat_provider": (override or {}).get("provider"),
        "chat_model": (override or {}).get("model"),
        "effective_provider": provider,
        "effective_model": model,
        "model_is_default": not bool(override and ((override.get("provider") or "") or (override.get("model") or ""))),
        "scope": "mission_control_chat_session",
    }


def _ensure_persona_chat_session(
    *,
    session_db,
    session_id: str | None,
    persona_id: str | None,
    title: str | None = None,
) -> None:
    if session_db is None or not session_id:
        return
    try:
        normalized_persona = _normalize_cli_persona_or_template_id(persona_id or "persona")
    except Exception:
        normalized_persona = safe_assignment_token(persona_id) or "persona"
    try:
        session_db.create_session(
            session_id=session_id,
            source=PERSONA_CHAT_SESSION_SOURCE,
            model=None,
            system_prompt=f"Mission Control persona chat for {normalized_persona}",
        )
    except Exception:
        pass

    safe_title = safe_assignment_text(title, limit=120)
    if not safe_title:
        return
    try:
        existing_title = session_db.get_session_title(session_id)
    except Exception:
        existing_title = None
    if existing_title:
        return
    try:
        session_db.set_session_title(session_id, safe_title)
    except Exception:
        pass


def _persona_chat_session_id(persona_instance_id: str) -> str:
    return persona_chat_session_id_for(persona_instance_id)


def _bind_free_floating_chat_session(
    *,
    instance_store: PersonaInstanceStore,
    session_db,
    persona_id: str,
    persona_instance_id: str,
    assignment_id: str | None = None,
    session_id: str | None = None,
    kill_active: bool = False,
) -> str:
    requested_persona = _normalize_cli_persona_or_template_id(persona_id)
    normalized_persona = requested_persona
    normalized_instance = safe_assignment_token(persona_instance_id) or persona_instance_id_for(requested_persona)
    requested_session_id = safe_assignment_text(session_id, limit=200)
    session_id = requested_session_id or ""
    previous_mode = None
    try:
        instance = instance_store.get(normalized_instance)
        normalized_persona = instance.persona_id
        previous_mode = safe_assignment_token(getattr(instance, "mode", None))
        existing_session_id = safe_assignment_text(getattr(instance, "session_id", None), limit=200)
        existing_assignment_id = safe_assignment_token(getattr(instance, "current_assignment_id", None))
        if not session_id and existing_session_id and (not existing_assignment_id or existing_assignment_id == safe_assignment_token(assignment_id)):
            session_id = existing_session_id
    except Exception:
        instance = None
    if not session_id:
        session_id = _persona_chat_session_id(normalized_instance)
    instance = instance_store.open_chat(
        persona_id=normalized_persona,
        persona_instance_id=normalized_instance,
        session_id=session_id,
        kill_active=kill_active,
    )
    instance.mode = "chat" if previous_mode == "chat" else "free_floating"
    instance.current_task_id = None
    instance.active_worker_session_id = None
    instance.active_run_id = None
    instance.current_assignment_id = assignment_id
    instance_store.update(instance)
    if session_db is not None:
        _ensure_persona_chat_session(
            session_db=session_db,
            session_id=session_id,
            persona_id=normalized_persona,
        )
    return session_id


# Redaction-on-write boundary (audit doc Stage 2B). Persona chat turns are now
# persisted to the shared SessionDB and recall is enabled for them, so any
# secret must be stripped *before* it is written — otherwise it becomes
# cross-session reachable. The read projection sanitizes too, but the write
# boundary is the authoritative one.
_PERSONA_CHAT_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|bearer)\s*[:=]\s*\S+"
)


def _redact_persona_chat_text(value, *, limit: int) -> str:
    safe = _safe_persona_chat_body_text(value, limit=limit)
    if not safe:
        return ""
    return _PERSONA_CHAT_SECRET_RE.sub(r"\1: [redacted]", safe)


def _safe_persona_chat_body_text(value, *, limit: int) -> str:
    text = str(value or "").replace("\x00", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    normalized = "\n".join(lines).strip()
    normalized = re.sub(r"\n{4,}", "\n\n\n", normalized)
    return normalized[:limit].rstrip()


def _persona_chat_existing_turn(
    *,
    session_db,
    session_id: str | None,
    client_message_id: str | None,
) -> dict[str, object]:
    if session_db is None or not session_id or not client_message_id:
        return {}
    try:
        messages = session_db.get_messages(session_id)
    except Exception:
        return {}

    result: dict[str, object] = {}
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        message_id = safe_assignment_text(
            item.get("platform_message_id"), limit=200
        )
        if message_id != client_message_id:
            continue
        role = str(item.get("role") or "").strip().lower()
        if role == "user" and "operator" not in result:
            result["operator"] = item
        elif role == "assistant":
            result["assistant"] = item
    return result


def _append_persona_operator_turn(
    *,
    session_db,
    session_id: str,
    message: str,
    client_message_id: str | None = None,
    skip_if_present: bool = False,
) -> None:
    if session_db is None or not session_id:
        return
    if skip_if_present:
        return
    safe_message = _redact_persona_chat_text(message, limit=12000)
    if not safe_message:
        return
    try:
        session_db.append_message(
            session_id=session_id,
            role="user",
            content=safe_message,
            platform_message_id=safe_assignment_text(client_message_id, limit=200)
            or None,
        )
    except Exception:
        return


def _append_persona_assistant_text(
    *,
    session_db,
    session_id: str,
    text: str,
    client_message_id: str | None = None,
) -> None:
    if session_db is None or not session_id:
        return
    safe = _redact_persona_chat_text(text, limit=8000)
    if not safe:
        return
    safe_client_message_id = safe_assignment_text(client_message_id, limit=200)
    if _persona_chat_existing_turn(
        session_db=session_db,
        session_id=session_id,
        client_message_id=safe_client_message_id,
    ).get("assistant"):
        return
    try:
        session_db.append_message(
            session_id=session_id,
            role="assistant",
            content=safe,
            platform_message_id=safe_client_message_id or None,
        )
    except Exception:
        return


def _append_persona_pre_trace_ack(
    *,
    session_db,
    session_id: str,
    trace_payload: dict,
) -> None:
    text = _persona_pre_trace_ack_text(trace_payload)
    _append_persona_assistant_text(
        session_db=session_db,
        session_id=session_id,
        text=text,
        client_message_id=None,
    )


def _persona_pre_trace_ack_text(trace_payload: dict) -> str:
    tool_name = safe_assignment_token(
        trace_payload.get("tool_name") or trace_payload.get("tool")
    )
    command_label = safe_assignment_text(
        trace_payload.get("command_label"), limit=160
    )
    if tool_name in {"skill_view", "skills_list", "skill_search"}:
        return "I'll load the relevant guidance first, then report back with the useful part."
    if tool_name in {"terminal", "shell_command", "execute_code"}:
        if command_label:
            return f"I'll run `{command_label}` now, then report back with the result."
        return "I'll run the check now, then report back with the result."
    if tool_name in {"read_file", "search_files", "find_files", "session_search"}:
        return "I'll inspect the relevant context now, then report back with what I find."
    if tool_name in {"mission_goal_create", "mission_goal"}:
        return "I'll create the real Mission Control goal now, then report back with the task details."
    return "I'll check that now and report back with what I find."


def _update_persona_chat_token_counts(*, session_db, session_id: str, result) -> None:
    if session_db is None or not session_id or result is None:
        return
    input_tokens = _positive_int_or_zero(getattr(result, "input_tokens", None))
    output_tokens = _positive_int_or_zero(getattr(result, "output_tokens", None))
    api_calls = _positive_int_or_zero(getattr(result, "api_calls", None))
    if input_tokens == 0 and output_tokens == 0 and api_calls == 0:
        return
    try:
        session_db.update_token_counts(
            session_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            api_call_count=api_calls,
            model=getattr(result, "model", None),
        )
    except Exception:
        return


def _positive_int_or_zero(value) -> int:
    try:
        parsed = int(value)
    except Exception:
        return 0
    return max(parsed, 0)


def _persona_by_id(cfg, persona_id: str):
    raw = str(persona_id or "").strip()
    # ensure_persisted_personas returns the seeded base profile plus the dormant
    # resolvable catalog, so typed pipeline ids and profile model-inheritance both resolve.
    personas = list(ensure_persisted_personas(cfg))
    if raw.lower().startswith("profile:"):
        profile_id = safe_assignment_token(raw.split(":", 1)[1])
        if not profile_id:
            return None
        matching_profile_persona = next(
            (
                persona
                for persona in personas
                if str(getattr(persona, "hermes_profile", "") or "") == profile_id
            ),
            None,
        )
        default_model = getattr(matching_profile_persona, "model", None) if matching_profile_persona is not None else None
        default_provider = getattr(matching_profile_persona, "provider", None) if matching_profile_persona is not None else None
        default_api_mode = getattr(matching_profile_persona, "api_mode", None) if matching_profile_persona is not None else None
        default_autonomy = getattr(matching_profile_persona, "autonomy", None) if matching_profile_persona is not None else None
        default_include_core = (
            bool(getattr(matching_profile_persona, "include_core_context_files", False))
            if matching_profile_persona is not None
            else False
        )
        default_readiness = (
            dict(getattr(matching_profile_persona, "readiness", {}) or {})
            if matching_profile_persona is not None
            else {}
        )
        return AgentPersona(
            id=f"profile:{profile_id}",
            display_name=f"{_display_name_for_profile(profile_id)} Agent",
            role="profile",
            model=default_model or getattr(cfg, "default_model", None),
            provider=default_provider or getattr(cfg, "default_provider", None),
            api_mode=default_api_mode or getattr(cfg, "default_api_mode", None),
            toolsets=profile_chat_toolsets(profile_id, personas),
            system_prompt_path="",
            autonomy=str(default_autonomy or "review"),
            hermes_profile=profile_id,
            skills=[],
            include_profile_memory=True,
            include_core_context_files=default_include_core,
            readiness=default_readiness,
        )
    normalized = _normalize_cli_persona_id(raw)
    for persona in personas:
        if getattr(persona, "id", None) == normalized:
            return persona
    return None


def _display_name_for_profile(profile_id: str) -> str:
    return " ".join(part.capitalize() for part in profile_id.replace("_", "-").split("-") if part) or "Profile"


def _persona_chat_message_with_history(*, session_db, session_id: str, message: str) -> str:
    safe_message = _redact_persona_chat_text(message, limit=12000)
    if session_db is None or not session_id:
        return safe_message
    try:
        history = session_db.get_messages(session_id)
    except Exception:
        return safe_message
    prior = []
    for item in (history or [])[-8:]:
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = _redact_persona_chat_text(item.get("content"), limit=500)
        if not content or content == safe_message:
            continue
        label = "Operator" if role == "user" else "Agent"
        prior.append(f"{label}: {content}")
    if not prior:
        return safe_message
    return (
        "Prior persona chat context (oldest to newest):\n"
        + "\n".join(prior)
        + "\n\nCurrent operator message:\n"
        + safe_message
    )


def _maybe_auto_title_persona_chat(*, session_db, session_id: str, user_message: str, assistant_response: str) -> None:
    if session_db is None or not session_id or not assistant_response:
        return
    try:
        from agent.title_generator import auto_title_session

        auto_title_session(
            session_db,
            session_id,
            user_message,
            assistant_response,
        )
    except Exception:
        return


def _run_free_floating_assignment_once(
    *,
    cfg,
    assignment_id: str,
    persona_instance_id: str,
    persona_id: str,
    title: str,
    message: str,
    requested_by: str,
    max_actions: int,
    max_seconds: float,
    client_message_id: str | None = None,
    stream: bool = False,
) -> tuple[int, dict[str, object]]:
    """Run one bounded sandbox turn for an already-queued persona chat message."""

    os.environ.setdefault("HERMES_AGENT_RUNTIME_ROOT", str(paths.store_root()))
    session_db = _default_persona_session_db()
    session_id = _bind_free_floating_chat_session(
        instance_store=PersonaInstanceStore(),
        session_db=session_db,
        persona_id=persona_id,
        persona_instance_id=persona_instance_id,
        assignment_id=assignment_id,
    )
    _append_persona_operator_turn(
        session_db=session_db,
        session_id=session_id,
        message=message,
        client_message_id=client_message_id,
    )
    persona = _persona_by_id(cfg, persona_id)
    if persona is None:
        PersonaAssignmentStore().complete(assignment_id, state="blocked", error="unknown persona")
        return 2, {
            "ok": False,
            "execution_state": "blocked",
            "session_id": session_id,
            "blocker": f"unknown persona {safe_assignment_token(persona_id)}",
            "next_expected": "configure the persona before chatting",
        }

    # Chat-first: run a plain conversational turn (no decision contract, no task
    # scoping). Continuity comes from the prepended session history; the agent
    # returns free text which we persist as the assistant turn.
    chat_message = _persona_chat_message_with_history(
        session_db=session_db,
        session_id=session_id,
        message=message,
    )
    stream_emitter = (
        _ChatProtocolV2Emitter(
            turn_id=safe_assignment_token(client_message_id) or safe_assignment_token(assignment_id),
            client_message_id=client_message_id,
            on_update=lambda emitter: persist_mission_chat_turn(
                session_id=session_id,
                client_message_id=client_message_id,
                turn_id=emitter.turn_id,
                elements=emitter.elements,
            ),
        )
        if stream
        else None
    )

    def _stream_delta(delta: str | None) -> None:
        _emit_chat_delta(delta)
        if stream_emitter is not None:
            stream_emitter.delta(delta)

    def _stream_progress(payload: dict[str, object] | None) -> None:
        if stream_emitter is not None:
            stream_emitter.progress(payload)

    try:
        # Keep the model run out of SessionDB. The canonical operator transcript
        # is written below; persisting the internal run as a second hidden
        # session creates orphaned final answers when copy-back is interrupted.
        chat_result = GPTPersonaRuntime(
            default_provider=cfg.default_provider,
            default_model=cfg.default_model,
            session_db=session_db,
            persist_agent_session=False,
        ).chat_reply(
            persona,
            chat_message,
            session_id=None,
            max_wall_seconds=max_seconds,
            stream_callback=_stream_delta if stream else None,
            trace_callback=_stream_progress if stream_emitter is not None else None,
        )
    except Exception as exc:
        if stream_emitter is not None:
            stream_emitter.finish(state="failed")
        PersonaAssignmentStore().complete(assignment_id, state="blocked", error=safe_assignment_text(str(exc), limit=240))
        return 2, {
            "ok": False,
            "execution_state": "blocked",
            "session_id": session_id,
            "blocker": safe_assignment_text(str(exc), limit=240),
            "next_expected": "fix the runtime blocker and retry the persona chat turn",
        }

    reply_text = _redact_persona_chat_text(getattr(chat_result, "final_response", "") or "", limit=8000)
    _append_persona_assistant_text(
        session_db=session_db,
        session_id=session_id,
        text=reply_text,
        client_message_id=client_message_id,
    )
    if stream_emitter is not None:
        persist_mission_chat_turn(
            session_id=session_id,
            client_message_id=client_message_id,
            turn_id=stream_emitter.turn_id,
            elements=stream_emitter.elements,
        )
    _update_persona_chat_token_counts(
        session_db=session_db,
        session_id=session_id,
        result=chat_result,
    )

    PersonaAssignmentStore().complete(assignment_id, state="completed")
    try:
        instance_store = PersonaInstanceStore()
        instance = instance_store.get(persona_instance_id)
        instance.active_run_id = None
        instance.current_assignment_id = None
        instance.state = WorkerSessionState.IDLE
        if instance.mode != "chat":
            instance.mode = "free_floating"
        instance.session_id = session_id
        instance_store.update(instance)
    except Exception:
        pass

    _maybe_auto_title_persona_chat(
        session_db=session_db,
        session_id=session_id,
        user_message=message,
        assistant_response=reply_text,
    )
    data = {
        "ok": True,
        "execution_state": "completed",
        "session_id": session_id,
        "reply": reply_text,
        "turn_id": stream_emitter.turn_id if stream_emitter is not None else None,
        "client_message_id": client_message_id,
        "run_ids": [],
        "task_id": None,
        "input_tokens": getattr(chat_result, "input_tokens", None),
        "output_tokens": getattr(chat_result, "output_tokens", None),
        "total_tokens": getattr(chat_result, "total_tokens", None),
        "next_expected": "agent replied conversationally; refresh Harness snapshot for the chat transcript",
    }
    if stream_emitter is not None:
        data["protocol_version"] = 2
        data["turn_elements"] = stream_emitter.elements
        stream_emitter.finish(
            state="completed",
            input_tokens=data.get("input_tokens"),
            output_tokens=data.get("output_tokens"),
            total_tokens=data.get("total_tokens"),
        )
    return 0, data


def _close_free_floating_assignments(persona_instance_id: str, *, reason: str, json_output: bool, terminal_state: str) -> int:
    cfg = load_agent_runtime_config()
    if not persona_assignment_store_enabled(cfg):
        data = {"ok": False, "feature_enabled": persona_instance_runtime_enabled(cfg), "assignment_store_enabled": False, "error": "persona assignment store is disabled"}
        print(emit_json(data) if json_output else data["error"])
        return 2
    normalized_instance = safe_assignment_token(persona_instance_id)
    store = PersonaAssignmentStore()
    matches = [
        item
        for item in store.list_all()
        if item.persona_instance_id == normalized_instance
        and item.evidence_kind == "free_floating"
        and item.task_id is None
        and item.state not in {"completed", "blocked", "cancelled"}
    ]
    if not matches:
        data = {"ok": False, "error": f"no active free-floating assignments for {persona_instance_id}"}
        print(emit_json(data) if json_output else data["error"])
        return 2
    closed = [store.complete(item.id, state=terminal_state, error=reason) for item in matches]
    try:
        instance_store = PersonaInstanceStore()
        instance = instance_store.get(normalized_instance)
        if instance.current_assignment_id in {item.id for item in closed}:
            instance.current_assignment_id = None
            instance.mode = "configured"
            instance_store.update(instance)
    except Exception:
        pass
    data = {
        "ok": True,
        "persona_instance_id": normalized_instance,
        "closed_assignment_ids": [item.id for item in closed],
        "state": terminal_state,
        "production_proof_eligible": False,
    }
    print(emit_json(data) if json_output else f"closed {len(closed)} free-floating assignments for {normalized_instance}")
    return 0


def _persona_id_from_instance_id(persona_instance_id: str) -> str:
    token = safe_assignment_token(persona_instance_id)
    try:
        return PersonaInstanceStore().get(token).persona_id
    except Exception:
        pass
    if token.startswith("personainst_"):
        raw = token.removeprefix("personainst_")
        if raw.startswith("profile_"):
            profile = safe_assignment_token(raw.removeprefix("profile_"))
            if profile:
                return f"profile:{profile}"
        return _normalize_cli_persona_id(raw)
    try:
        return _normalize_cli_persona_id(token)
    except ValueError as exc:
        raise ValueError(f"unsupported persona instance {persona_instance_id!r}") from exc


def _cmd_persona_diagnose(args) -> int:
    cfg = load_agent_runtime_config()
    os.environ.setdefault("HERMES_AGENT_RUNTIME_ROOT", str(paths.store_root()))
    try:
        result = PersonaDiagnosticController(
            config=cfg,
            engine_factory=lambda **kwargs: TickEngine(
                **kwargs,
                persona_runtime=GPTPersonaRuntime(default_provider=cfg.default_provider, default_model=cfg.default_model),
            ),
        ).diagnose(
            PersonaDiagnosticOptions(
                persona_id=args.persona_id,
                title=args.title,
                message=args.message,
                requested_by=args.requested_by,
                operation_kind=args.operation_kind,
                operation_mode=args.operation_mode,
                max_actions=args.max_actions,
                max_seconds=args.max_seconds,
                affected_repos=list(args.affected_repo or []),
                acceptance_criteria=list(args.acceptance or []),
                non_goals=list(args.non_goal or []),
                preserve_open_task=bool(getattr(args, "keep_task", False)),
            )
        )
    except ValueError as exc:
        data = {"ok": False, "error": str(exc)}
        print(emit_json(data) if args.json else str(exc))
        return 2
    if args.json:
        print(emit_json(result))
    else:
        print(
            f"persona diagnostic {result.task_id}: persona={result.persona_id} "
            f"stop={result.stop_reason} decision={result.latest_decision_type or 'none'} "
            f"tokens={result.latest_total_tokens if result.latest_total_tokens is not None else 'unknown'}"
        )
    return result.exit_code


def _normalize_cli_persona_id(persona_id: str) -> str:
    value = safe_assignment_token(persona_id)
    aliases = {
        "neko": "neko_supervisor",
        "launcher_dev": "dev",
        "launcher-dev": "dev",
        "backend-dev": "backend_dev",
        "backend": "backend_dev",
    }
    value = aliases.get(value, value)
    # Accept any seeded persona id (base-profile foundation seeds ``base``) plus the
    # legacy typed-pipeline ids (dormant, kept for back-compat). Other on-disk profiles
    # are reached through the ``profile:<name>`` branch, not this normalizer.
    allowed = {"neko_supervisor", "dev", "backend_dev", "qa", "pm"} | {p.id for p in seed_personas()}
    if value not in allowed:
        raise ValueError(f"unsupported persona {persona_id!r}")
    return value


def _normalize_cli_persona_or_template_id(persona_id: str) -> str:
    raw = str(persona_id or "").strip()
    if raw.lower().startswith("profile:"):
        profile = safe_assignment_token(raw.split(":", 1)[1])
        if not profile:
            raise ValueError(f"unsupported persona {persona_id!r}")
        return f"profile:{profile}"
    return _normalize_cli_persona_id(raw)


def _cmd_task_create(args) -> int:
    from agent_runtime.mission_goal import create_mission_goal, create_mission_goal_from_request

    if getattr(args, "request_json", None):
        try:
            request = _load_request_json(args.request_json)
        except Exception as exc:
            data = {
                "schema_version": 1,
                "error": {
                    "code": "invalid_payload",
                    "message": _redact_paths("Could not parse goal-create request JSON."),
                    "retryable": False,
                    "safe_details": {"error_class": type(exc).__name__},
                },
            }
        else:
            data = create_mission_goal_from_request(request)
    else:
        if not args.title or not args.description:
            data = {
                "schema_version": 1,
                "error": {
                    "code": "invalid_request",
                    "message": "--title and --description are required unless --request-json is provided.",
                    "retryable": False,
                    "safe_details": {},
                },
            }
        else:
            data = create_mission_goal(
                title=args.title,
                description=args.description,
                requested_by=args.requested_by,
                start_daemon_mode=getattr(args, "start_daemon", None),
            )
    if args.json:
        print(emit_json(data))
    else:
        if data.get("error"):
            print((data["error"] or {}).get("message") or "goal create failed")
            return 1
        daemon_summary = (data.get("daemon_start") or {}).get("summary", "daemon start not attempted")
        dirty_summary = (((data.get("new_goal_hygiene") or {}).get("dirty_state_after_cleanup") or {}).get("summary"))
        print(f"{data.get('task_id')} [{data.get('state')}] {data.get('title')} dirty={dirty_summary} daemon={daemon_summary}")
    return 1 if data.get("error") else 0


def _cmd_task_list(args) -> int:
    store=TaskStore()
    if args.state == "all": tasks=store.list_all()
    elif args.state == "done": tasks=store.list_by_state(TaskState.DONE)
    elif args.state == "blocked": tasks=store.list_by_state(TaskState.BLOCKED)
    else: tasks=store.list_open()
    print(emit_json([task_summary(t) for t in tasks]) if args.json else "\n".join(human_task_line(t) for t in tasks))
    return 0


def _cmd_task_show(args) -> int:
    try:
        task = TaskStore().get(args.task_id)
    except NotFound:
        archived = _archived_task_summary(args.task_id)
        if archived:
            event_limit = max(0, int(getattr(args, "events", 0) or 0))
            since_text = getattr(args, "since", None)
            data = {"archived": True, **archived}
            if args.json and (event_limit or since_text):
                data["events"] = _task_events(args.task_id, limit=event_limit, since_text=since_text)
            print(emit_json(data) if args.json else f"archived {args.task_id}: {archived['archive_batch']}")
            return 0
        data = {"ok": False, "error": "task_not_found", "task_id": args.task_id, "message": f"Task not found: {args.task_id}"}
        print(emit_json(data) if args.json else data["message"])
        return 1
    event_limit = max(0, int(getattr(args, "events", 0) or 0))
    since_text = getattr(args, "since", None)
    if args.json and (event_limit or since_text):
        data = {"task": task, "events": _task_events(task.id, limit=event_limit, since_text=since_text)}
        print(emit_json(data))
    else:
        print(emit_json(task) if args.json else human_task_line(task))
    return 0


def _cmd_task_history(args) -> int:
    try:
        task = TaskStore().get(args.task_id)
        archived = False
        task_state = task.state.value
    except NotFound:
        archive = _archived_task_summary(args.task_id)
        if not archive:
            data = {"ok": False, "error": "task_not_found", "task_id": args.task_id, "message": f"Task not found: {args.task_id}"}
            print(emit_json(data) if args.json else data["message"])
            return 1
        archived = True
        task_data = archive.get("task") if isinstance(archive, dict) else None
        task_state = task_data.get("state") if isinstance(task_data, dict) else None

    limit = max(1, min(500, int(getattr(args, "limit", 50) or 50)))
    events = _task_events(args.task_id, limit=limit, since_text=getattr(args, "since", None))
    data = {
        "ok": bool(events.get("ok", True)),
        "task_id": args.task_id,
        "task_state": task_state,
        "archived": archived,
        "event_count": events.get("count", 0),
        "limit": limit,
        "events": events.get("items", []),
    }
    if not data["ok"]:
        data["error"] = events.get("error")
        data["message"] = events.get("message")
    if args.json:
        print(emit_json(data))
    else:
        lines = [f"{_event_value(item, 'ts')} {_event_value(item, 'type')} run={_event_value(item, 'run_id') or '-'} persona={_event_value(item, 'persona_id') or '-'}" for item in data["events"]]
        print("\n".join(lines))
    return 0 if data["ok"] else 1


def _event_value(event, key: str):
    if isinstance(event, dict):
        return event.get(key)
    return getattr(event, key, None)


def _archived_task_summary(task_id: str) -> dict | None:
    root = paths.deleted_archive_dir()
    if not root.exists():
        return None
    for manifest_path in sorted(root.glob("*/manifest.json"), reverse=True):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in manifest.get("archived_tasks") or []:
            if not isinstance(item, dict) or item.get("task_id") != task_id:
                continue
            task_path = manifest_path.parent / str(item.get("task_path") or "")
            task_data = None
            if task_path.exists():
                try:
                    task_data = json.loads(task_path.read_text(encoding="utf-8"))
                except Exception:
                    task_data = None
            return {
                "ok": True,
                "task_id": task_id,
                "archive_batch": manifest_path.parent.name,
                "archive_dir": str(manifest_path.parent),
                "manifest_path": str(manifest_path),
                "archived_task": item,
                "task": task_data,
            }
    return None


def _task_events(task_id: str, *, limit: int, since_text: str | None) -> dict:
    since = None
    if since_text:
        try:
            since = datetime.fromisoformat(since_text.replace("Z", "+00:00"))
        except ValueError:
            return {"ok": False, "error": "invalid_since", "message": "--since must be an ISO-8601 timestamp", "items": []}
    items = EventLog().for_task(task_id, limit=limit or 50, since=since)
    return {
        "ok": True,
        "task_id": task_id,
        "limit": limit or 50,
        "since": since.isoformat() if since else None,
        "count": len(items),
        "items": items,
    }


def _cmd_task_cancel(args) -> int:
    task = TaskStore().cancel(args.task_id, reason=args.reason, actor="cli")
    cancelled_run_ids = _cancel_task_active_runs(task.id, reason=args.reason)
    closed_worker_ids = _close_task_active_workers(task.id, reason=args.reason)
    data = {"task_id": task.id, "state": task.state.value, "reason_recorded": True, "cancelled_run_ids": cancelled_run_ids, "closed_worker_session_ids": closed_worker_ids}
    print(emit_json(data) if args.json else f"cancelled {task.id}")
    return 0


def _cmd_task_unblock(args) -> int:
    store = TaskStore()
    incident_store = IncidentStore()
    try:
        task = store.get(args.task_id)
    except NotFound:
        archived = _archived_task_summary(args.task_id)
        if archived:
            data = {"ok": False, "error": "task_archived", "task_id": args.task_id, "archive_batch": archived["archive_batch"], "message": "Archived tasks cannot be unblocked; create a new task or inspect the archive evidence."}
        else:
            data = {"ok": False, "error": "task_not_found", "task_id": args.task_id}
        print(emit_json(data) if args.json else data.get("message", data["error"]))
        return 1
    if task.state in {TaskState.DONE, TaskState.CANCELLED}:
        data = {"ok": False, "error": "task_terminal", "task_id": task.id, "state": task.state.value}
        print(emit_json(data) if args.json else f"{task.id} is terminal: {task.state.value}")
        return 1
    previous_state = task.state.value
    open_incident_ids = {
        incident.id
        for incident in incident_store.list_open()
        if getattr(incident, "task_id", None) == task.id
    }
    open_incident_ids.update(task.open_incident_ids or [])
    closed_incident_ids: list[str] = []
    for incident_id in sorted(open_incident_ids):
        try:
            incident_store.close(incident_id, reason=f"operator unblock: {_safe_operator_text(args.reason)}")
            closed_incident_ids.append(incident_id)
        except Exception:
            pass
    task = store.get(task.id)
    task.state = TaskState(args.state)
    task.open_incident_ids = []
    task.risk_flags = [flag for flag in list(task.risk_flags or []) if flag != "neko_block_recovery_attempted"]
    cleared_recovery_keys = _clear_task_recovery_markers(task)
    if args.rescope:
        task.current_stage_id = None
        task.stages = []
        task.affected_repos = []
        task.assigned_persona_ids = {}
        ensure_default_mission_plan(task)
    task.updated_at = now()
    store.update(task, actor="cli", reason=f"operator unblock: {_safe_operator_text(args.reason)}")
    foreground = activate_foreground_runtime(task.id, started_by="cli") if args.foreground else None
    data = {
        "ok": True,
        "task_id": task.id,
        "from": previous_state,
        "to": task.state.value,
        "rescope": bool(args.rescope),
        "foreground_runtime": foreground,
        "closed_incident_ids": closed_incident_ids,
        "cleared_recovery_keys": cleared_recovery_keys,
    }
    print(emit_json(data) if args.json else f"unblocked {task.id}: {previous_state} -> {task.state.value}")
    return 0


def _cmd_task_steer(args) -> int:
    data = execute_steer_action(
        args.task_id,
        action_id=getattr(args, "action_id", None),
        verb=getattr(args, "verb", None),
        source_node_id=getattr(args, "source_node_id", None),
        target_node_id=getattr(args, "target_node_id", None),
        requested_by=getattr(args, "requested_by", "operator"),
        reason=getattr(args, "reason", "operator steer"),
    )
    print(emit_json(data) if args.json else (f"steered {data.get('task_id')}: {data.get('result')}" if data.get("ok") else data.get("error", "steer failed")))
    return 0 if data.get("ok") else ERROR_EXIT_CODES.get(str(data.get("error_kind") or "invalid_request"), 2)


def _clear_task_recovery_markers(task: Task) -> list[str]:
    cleared: list[str] = []
    data = task.harness_self_heal if isinstance(task.harness_self_heal, dict) else {}
    stages = data.get("stages")
    if not isinstance(stages, dict):
        return cleared
    for stage_id, stage_data in list(stages.items()):
        if not isinstance(stage_data, dict):
            continue
        for key in (
            "last_block_recovery_signal",
            "last_closed_incident_id",
            "incident_close_counter",
            "block_recovery_attempted",
            "last_budget_recovery_signal",
        ):
            if key in stage_data:
                stage_data.pop(key, None)
                cleared.append(f"stages.{stage_id}.{key}")
        if not stage_data:
            stages.pop(stage_id, None)
    if not stages:
        data.pop("stages", None)
    task.harness_self_heal = data
    return cleared


def _safe_operator_text(value: str) -> str:
    return " ".join(str(value or "").split())[:160] or "operator requested"


def _cancel_task_active_runs(task_id: str, *, reason: str) -> list[str]:
    runs = RunStore()
    cancelled = []
    for run in runs.list_for_task(task_id):
        if run.state not in ACTIVE_RUN_STATES:
            continue
        cancelled.append(runs.cancel(run.id, reason=reason).id)
    return cancelled


def _close_task_active_workers(task_id: str, *, reason: str) -> list[str]:
    store = WorkerSessionStore()
    closed = []
    for worker in store.find_active(task_id=task_id):
        closed.append(store.close(worker.id, reason=reason).id)
    return closed


def _cmd_task_archive_ready(args) -> int:
    data = TaskStore().archive_ready(actor="cli", reason="operator archive-ready command")
    if args.json:
        print(emit_json(data))
    else:
        batch = data["archive_batch"] or "no archive batch"
        print(f"archived {data['archived_count']} task(s), skipped {data['skipped_count']} task(s): {batch}")
    return 0


def _cmd_task_archive(args) -> int:
    data = TaskStore().archive(args.task_id, actor="cli", reason="operator archive task command")
    if args.json:
        print(emit_json(data))
    else:
        batch = data["archive_batch"] or "no archive batch"
        print(f"archived {data['archived_count']} task(s), skipped {data['skipped_count']} task(s): {batch}")
    return 0 if data.get("archived_count") else 1


def _cmd_playground_list(args) -> int:
    from agent_runtime.replay_scenarios import list_scenarios

    records = list_scenarios()
    if args.json:
        print(emit_json(records))
    else:
        for record in records:
            print(f"{record.get('scenario_id')} [{record.get('status')}] origin={record.get('failure_origin', 'unknown')} {record.get('decision_type')} task={record.get('task_id')} {record.get('error_message', '')[:80]}")
        if not records:
            print("no replay scenarios captured")
    return 0


def _cmd_playground_show(args) -> int:
    from agent_runtime.replay_scenarios import get_scenario

    record = get_scenario(args.scenario_id)
    if record is None:
        data = {"ok": False, "error": "scenario_not_found", "scenario_id": args.scenario_id}
        print(emit_json(data) if args.json else f"scenario not found: {args.scenario_id}")
        return 1
    print(emit_json(record) if args.json else emit_json(record))
    return 0


def _cmd_playground_replay(args) -> int:
    from agent_runtime.replay_scenarios import replay_all, replay_scenario

    if args.scenario_id:
        result = replay_scenario(args.scenario_id)
        print(emit_json(result) if args.json else f"{result.get('scenario_id')}: {result.get('verdict', result.get('error'))}")
        return 0 if result.get("ok") else 1
    summary = replay_all()
    if args.json:
        print(emit_json(summary))
    else:
        print(f"total={summary['total']} passing={len(summary['passes_current_contract'])} still_failing={len(summary['still_failing'])} not_replayable={len(summary['not_replayable'])}")
        for sid in summary["still_failing"]:
            print(f"  still failing: {sid}")
    return 0


def _swarm_state_path() -> Path:
    return paths.store_root() / "swarm_state.json"


def _read_swarm_state() -> dict:
    path = _swarm_state_path()
    if not path.exists():
        return {"enabled": False, "max_active_lanes": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"enabled": False, "max_active_lanes": 0, "error": "invalid_swarm_state"}
    return data if isinstance(data, dict) else {"enabled": False, "max_active_lanes": 0, "error": "invalid_swarm_state"}


def _write_swarm_state(data: dict) -> None:
    from utils import atomic_json_write
    from agent_runtime.serde import to_jsonable

    path = _swarm_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, to_jsonable(data), indent=2, sort_keys=True)


def _cmd_swarm_status(args) -> int:
    cfg = load_agent_runtime_config()
    swarm_cfg = getattr(cfg, "swarm", None)
    allowed, certification = swarm_certification_allows_production(
        requires_certification=bool(getattr(swarm_cfg, "requires_certification", True)),
        allow_uncertified_dev_swarm=bool(getattr(swarm_cfg, "allow_uncertified_dev_swarm", False)),
    )
    state = _read_swarm_state()
    data = {"enabled": bool(state.get("enabled")), "certification_allows_production": allowed, "certification": certification, "state": state}
    print(emit_json(data) if args.json else f"swarm enabled={data['enabled']} certification={certification.get('state')}")
    return 0


def _cmd_swarm_enable(args) -> int:
    cfg = load_agent_runtime_config()
    swarm_cfg = getattr(cfg, "swarm", None)
    allowed, certification = swarm_certification_allows_production(
        requires_certification=bool(getattr(swarm_cfg, "requires_certification", True)),
        allow_uncertified_dev_swarm=bool(getattr(args, "allow_uncertified_dev_swarm", False)),
    )
    lanes = max(1, int(getattr(args, "lanes", 2) or 2))
    if not allowed:
        data = {"ok": False, "enabled": False, "reason": "certification_required", "certification": certification}
        print(emit_json(data) if args.json else "swarm enable refused: certification_required")
        return 2
    data = {
        "ok": True,
        "enabled": True,
        "max_active_lanes": lanes,
        "updated_at": now(),
        "unsafe_dev_override": bool(getattr(args, "allow_uncertified_dev_swarm", False)),
        "certification": certification,
    }
    _write_swarm_state(data)
    print(emit_json(data) if args.json else f"swarm enabled lanes={lanes}")
    return 0


def _cmd_swarm_disable(args) -> int:
    state = _read_swarm_state()
    data = {**state, "ok": True, "enabled": False, "updated_at": now()}
    _write_swarm_state(data)
    print(emit_json(data) if args.json else "swarm disabled")
    return 0


def _cmd_lane_list(args) -> int:
    from agent_runtime.runtime_instances import GoalRuntimeInstanceStore, runtime_instance_summary

    lanes = [runtime_instance_summary(item) for item in GoalRuntimeInstanceStore().list_all()]
    _print_stage42(_list_envelope("lane", _sort_rows(lanes, getattr(args, "sort", None))), args=args, default_output="json")
    return 0


def _cmd_lane_show(args) -> int:
    from agent_runtime.runtime_instances import GoalRuntimeInstanceStore, runtime_instance_summary

    try:
        lane = runtime_instance_summary(GoalRuntimeInstanceStore().get(args.lane_id))
    except (NotFound, FileNotFoundError):
        return emit_harness_error(
            NotFound(f"lane not found: {args.lane_id}"),
            args=args,
            code="lane_not_found",
        )
    _print_stage42(_object_envelope("lane", lane), args=args, default_output="json")
    return 0


def _cmd_lane_control(args) -> int:
    from agent_runtime.runtime_instances import GoalRuntimeInstanceStore, runtime_instance_summary

    store = GoalRuntimeInstanceStore()
    command = str(getattr(args, "lane_command", ""))
    try:
        if command in {"pause", "park"}:
            lane = store.park_lane(args.lane_id, reason=args.reason, state="parked_by_operator")
        elif command == "resume":
            lane = store.resume_lane(args.lane_id, reason=args.reason)
        elif command == "drain":
            lane = store.transition(args.lane_id, "done", reason=args.reason, active_run_ids=[])
        else:
            raise ValueError("unknown lane command")
    except Exception as exc:
        data = {"ok": False, "error": type(exc).__name__, "message": str(exc), "lane_id": args.lane_id}
        print(emit_json(data) if args.json else f"lane {command} failed: {data['message']}")
        return 1
    data = {"ok": True, "lane": runtime_instance_summary(lane)}
    print(emit_json(data) if args.json else f"{lane.id} {command} -> {lane.state}")
    return 0


def _cmd_tick(args) -> int:
    cfg = load_agent_runtime_config()
    os.environ.setdefault("HERMES_AGENT_RUNTIME_ROOT", str(paths.store_root()))
    result = TickEngine(
        config=cfg,
        persona_runtime=GPTPersonaRuntime(default_provider=cfg.default_provider, default_model=cfg.default_model),
    ).tick_once(task_id=args.task_id)
    print(emit_json(result) if args.json else f"tick {result.tick_id}: {len(result.actions_taken)} actions")
    return 0


def _cmd_run_until_settled(args) -> int:
    cfg = load_agent_runtime_config()
    os.environ.setdefault("HERMES_AGENT_RUNTIME_ROOT", str(paths.store_root()))
    result = TickEngine(
        config=cfg,
        persona_runtime=GPTPersonaRuntime(default_provider=cfg.default_provider, default_model=cfg.default_model),
    ).run_until_settled(task_id=args.task_id, max_actions=args.max_actions, max_seconds=args.max_seconds)
    if args.json:
        print(emit_json(result))
    else:
        print(f"settle {result.settle_id}: {len(result.actions_taken)} actions stop={result.stop_reason}")
    return 0


def _cmd_burn_in_create(args) -> int:
    manifest = create_burn_in(suite=args.suite, case_id=getattr(args, "case_id", None), rerun_of=getattr(args, "rerun_of", None))
    if args.json:
        print(emit_json(manifest))
    else:
        print(f"burn-in {manifest['burn_id']}: created")
    return 0


def _cmd_burn_in_run(args) -> int:
    cfg = load_agent_runtime_config()
    os.environ.setdefault("HERMES_AGENT_RUNTIME_ROOT", str(paths.store_root()))
    manifest = run_burn_in_case(
        args.case_id,
        burn_id=getattr(args, "burn_id", None),
        max_actions=getattr(args, "max_actions", 12),
        engine=TickEngine(
            config=cfg,
            persona_runtime=GPTPersonaRuntime(default_provider=cfg.default_provider, default_model=cfg.default_model),
        ),
    )
    if args.json:
        print(emit_json(manifest))
    else:
        print(f"burn-in {manifest['burn_id']}: {manifest['status']}")
    return 0 if manifest.get("status") == "passed" else 2


def _cmd_burn_in_status(args) -> int:
    try:
        data = burn_in_status(args.burn_id)
    except (FileNotFoundError, ValueError) as exc:
        data = {"burn_id": args.burn_id, "ok": False, "error": type(exc).__name__, "message": "burn-in ledger was not found or is invalid"}
        if args.json:
            print(emit_json(data))
        else:
            print(f"burn-in {args.burn_id}: not found")
        return 2
    if args.json:
        print(emit_json(data))
    else:
        print(f"burn-in {args.burn_id}: {data['manifest'].get('status')}")
    return 0


def _cmd_burn_in_summarize(args) -> int:
    try:
        data = summarize_burn_in(args.burn_id)
    except (FileNotFoundError, ValueError) as exc:
        data = {"burn_id": args.burn_id, "ok": False, "error": type(exc).__name__, "message": "burn-in ledger was not found or is invalid"}
        if args.json:
            print(emit_json(data))
        else:
            print(f"burn-in {args.burn_id}: not found")
        return 2
    if args.json:
        print(emit_json(data))
    else:
        print(f"burn-in {args.burn_id}: ok={data['ok']} status={data['status']}")
    return 0 if data.get("ok") else 2


def _cmd_run_cancel(args) -> int:
    worker_store = WorkerSessionStore()
    run_store = RunStore()
    run = run_store.get(args.run_id)
    coordinator_id = _coordinator_actor_id(args)
    if coordinator_id:
        target = None
        for worker in worker_store.find_active(task_id=run.task_id, persona_id=run.persona_id):
            if worker.active_run_id == run.id:
                try:
                    target = PersonaInstanceStore().get(persona_instance_id_for(worker.persona_id))
                except Exception:
                    target = None
                break
        cfg = load_agent_runtime_config()
        persona = _persona_by_id(cfg, run.persona_id)
        scope = _coordinator_scope_from_args(args, cfg, persona)
        auth = authorize_coordinator_action(
            "run.cancel",
            scope,
            target,
            actor=coordinator_id,
            coordinator_id=coordinator_id,
        )
        if not auth.ok:
            data = _coordinator_confirm_payload("run.cancel", coordinator_id, auth)
            print(emit_json(data) if args.json else data["status"])
            return 2
    run = run_store.cancel(args.run_id, reason=args.reason)
    updated_workers = []
    for worker in worker_store.find_active(task_id=run.task_id, persona_id=run.persona_id):
        if worker.active_run_id == run.id:
            updated = worker_store.update_after_run(worker.id, run, close_reason="run_cancelled", count_decision=False)
            updated_workers.append(updated.id)
    data = {"run_id": run.id, "state": run.state.value, "reason_recorded": True, "updated_worker_session_ids": updated_workers}
    print(emit_json(data) if args.json else f"cancelled {run.id}")
    return 0


def _cmd_run_show(args) -> int:
    run_store = RunStore()
    proof_store = ProofStore()
    try:
        run = run_store.get(args.run_id)
    except (NotFound, FileNotFoundError):
        return emit_harness_error(
            NotFound(f"run not found: {args.run_id}"),
            args=args,
            code="run_not_found",
        )
    proof_records = [
        proof
        for proof in proof_store.list_for_task(run.task_id)
        if isinstance(proof.metadata, dict) and proof.metadata.get("run_id") == run.id
    ]
    events = _task_events(run.task_id, limit=max(1, min(250, int(getattr(args, "events", 25) or 25))), since_text=None)
    scoped_events = [
        item
        for item in events.get("items", [])
        if _event_value(item, "run_id") == run.id or _event_value(item, "persona_id") == run.persona_id
    ]
    data = {
        "ok": True,
        "run": run,
        "proofs": proof_records,
        "events": {
            "ok": events.get("ok", True),
            "count": len(scoped_events),
            "items": scoped_events,
        },
    }
    if args.json:
        print(emit_json(data))
    else:
        print(f"{run.id} {run.persona_id} {run.state.value} task={run.task_id} proofs={len(proof_records)} events={len(scoped_events)}")
    return 0


def _cmd_run_approve(args) -> int:
    run_store = RunStore()
    incident_store = IncidentStore()
    run = run_store.approve_continuation(args.run_id)
    closed_incidents = []
    for incident in incident_store.list_open():
        if incident.run_id == run.id and incident.kind == "run_budget_exceeded":
            incident_store.close(incident.id, reason="operator approved same-session continuation")
            closed_incidents.append(incident.id)
    data = {
        "run_id": run.id,
        "state": run.state.value,
        "approved_for_continuation": True,
        "session_id": run.session_id,
        "closed_incidents": closed_incidents,
        "next_expected": "run harness tick to continue same session",
    }
    print(emit_json(data) if args.json else f"approved {run.id} for same-session continuation")
    return 0


def _cmd_worker_list(args) -> int:
    store = WorkerSessionStore()
    if getattr(args, "active", False):
        workers = store.find_active(task_id=getattr(args, "task_id", None), persona_id=getattr(args, "persona_id", None))
    else:
        workers = store.list_all()
        if getattr(args, "task_id", None):
            workers = [worker for worker in workers if worker.task_id == args.task_id]
        if getattr(args, "persona_id", None):
            workers = [worker for worker in workers if worker.persona_id == args.persona_id]
    data = [worker_session_summary(worker) for worker in workers]
    _print_stage42(_list_envelope("worker", _sort_rows(data, getattr(args, "sort", None))), args=args, default_output="json")
    return 0


def _cmd_worker_show(args) -> int:
    try:
        worker = WorkerSessionStore().get(args.worker_session_id)
    except (NotFound, FileNotFoundError):
        return emit_harness_error(
            NotFound(f"worker not found: {args.worker_session_id}"),
            args=args,
            code="worker_not_found",
        )
    data = worker_session_summary(worker)
    _print_stage42(_object_envelope("worker", data), args=args, default_output="json")
    return 0


def _cmd_worker_control(args) -> int:
    store = WorkerSessionStore()
    command = getattr(args, "worker_command", "")
    reason = getattr(args, "reason", "") or getattr(args, "note", "") or f"operator {command}"
    if command == "takeover":
        data = operator_takeover_worker(
            args.worker_session_id,
            actor=args.actor,
            reason=reason,
            lease_seconds=args.lease_seconds,
            cancel_active_run=bool(getattr(args, "cancel_active_run", False)),
            approve_destructive=bool(getattr(args, "approve_destructive", False)),
        )
        print(emit_json(data) if args.json else f"{data['worker_session_id']} takeover -> {data['state']}")
        return 0
    if command == "pause":
        worker = store.pause(args.worker_session_id, actor=args.actor, reason=reason)
    elif command == "resume":
        worker = store.resume(args.worker_session_id, actor=args.actor, reason=reason)
    elif command == "interrupt":
        worker = store.interrupt(args.worker_session_id, actor=args.actor, reason=reason)
    elif command == "nudge":
        worker = store.nudge(args.worker_session_id, actor=args.actor, note=reason)
    elif command == "possess":
        worker = store.possess(args.worker_session_id, actor=args.actor, lease_seconds=args.lease_seconds)
    elif command == "release":
        worker = store.release(args.worker_session_id, actor=args.actor, handback=reason)
    else:
        print("Use `hermes harness worker --help`.")
        return 2
    data = worker_session_summary(worker)
    print(emit_json(data) if args.json else f"{data['worker_session_id']} {command} -> {data['state']}")
    return 0


def _cmd_status(args) -> int:
    data=build_status()
    print(emit_json(data) if args.json else f"open_tasks={data['open_tasks']} running_runs={data['running_runs']} open_incidents={data['open_incidents']} dirty={data['dirty_summary']} runtime_health={data['runtime_health']['ok']}")
    return 0


def _cmd_health(args) -> int:
    personas = ensure_persisted_personas(load_agent_runtime_config())
    data = provider_health_for_personas(personas)
    if args.json:
        print(emit_json(data))
    else:
        issue_count = len(data.get("issues") or [])
        print(f"runtime_health={data['ok']} interpreter={data['interpreter']} issues={issue_count}")
    return 0


def _cmd_config(args) -> int:
    data = effective_config_summary(load_agent_runtime_config())
    print(emit_json(data) if args.json else f"config valid={data['validation']['ok']} schema={data['schema_version']}")
    return 0 if data["validation"]["ok"] else 2


def _cmd_migrate(args) -> int:
    data = migration_status()
    data["check_only"] = bool(getattr(args, "check", False))
    print(emit_json(data) if args.json else f"migrations pending={data['pending']} schema={data['current_schema_version']}")
    return 0 if not data.get("pending") else 2


def _cmd_verify(args) -> int:
    cfg = load_agent_runtime_config()
    started = datetime.now(timezone.utc)
    repo_root = Path(__file__).resolve().parents[1]
    packet = {
        "schema_version": 1,
        "proof_packet_id": f"mission_control_verify_{started.strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at_utc": started.isoformat().replace("+00:00", "Z"),
        "mode": args.mode,
        "runtime_root": str(paths.store_root()),
        "hermes_profile": active_profile_name(),
        "hermes_home": os.environ.get("HERMES_HOME"),
        "harness_repo": _git_summary(repo_root),
        "runtime_config": effective_config_summary(cfg),
        "migration": migration_status(),
        "commands": [],
        "tests": [],
        "final_status": {},
    }
    commands = [
        ("harness status", [sys.executable, "-m", "hermes_cli.main", "harness", "status", "--json"]),
        ("harness snapshot", [sys.executable, "-m", "hermes_cli.main", "harness", "snapshot", "--json"]),
        ("harness task archive help", [sys.executable, "-m", "hermes_cli.main", "harness", "task", "archive", "--help"]),
        ("harness task archive-ready help", [sys.executable, "-m", "hermes_cli.main", "harness", "task", "archive-ready", "--help"]),
        ("harness config show", [sys.executable, "-m", "hermes_cli.main", "harness", "config", "show", "--json"]),
        ("harness migrate check", [sys.executable, "-m", "hermes_cli.main", "harness", "migrate", "--check", "--json"]),
    ]
    ok = True
    for label, command in commands:
        result = _run_verify_command(label, command, cwd=repo_root)
        packet["commands"].append(result)
        ok = ok and result["exit_code"] == 0
    if not args.skip_tests:
        test_result = _run_verify_command(
            "harness focused tests",
            [
                sys.executable,
                "-m",
                "pytest",
                "-o",
                "addopts=",
                "-q",
                "tests/agent_runtime/test_proof_runner.py",
                "tests/agent_runtime/test_daemon.py",
                "tests/agent_runtime/test_store.py",
                "tests/agent_runtime/test_snapshot.py",
                "tests/agent_runtime/test_status.py",
            ],
            cwd=repo_root,
        )
        packet["tests"].append(test_result)
        ok = ok and test_result["exit_code"] == 0
    try:
        packet["final_status"] = build_status()
    except Exception as exc:
        ok = False
        packet["final_status"] = {"error": type(exc).__name__}
    if args.mode == "live-tony" and str(paths.store_root()).replace("/", "\\").lower() != r"x:\eternia\.hermes\agent-runtime":
        ok = False
        packet.setdefault("issues", []).append({"kind": "runtime_root_mismatch", "expected": r"X:\Eternia\.hermes\agent-runtime"})
    print(emit_json(packet) if args.json else f"verify ok={ok} commands={len(packet['commands'])} tests={len(packet['tests'])}")
    return 0 if ok else 2


def _cmd_observe(args) -> int:
    tasks = TaskStore().list_all()
    runs = RunStore().list_all()
    incidents = IncidentStore().list_all()
    worker_store = WorkerSessionStore()
    workers = worker_store.list_all()
    proofs = []
    proof_store = ProofStore()
    for task in tasks:
        proofs.extend(proof_store.list_for_task(task.id))
    data = build_observability(
        tasks=tasks,
        runs=runs,
        incidents=incidents,
        proofs=proofs,
        daemon_status=read_daemon_status(),
        events=EventLog().tail(20),
        execution_mode=load_agent_runtime_config().execution_mode,
        worker_sessions=workers,
    )
    print(emit_json(data) if args.json else f"observability={data['health']['status']} interventions={len(data['interventions'])}")
    return 0


def _cmd_contracts_dump(args) -> int:
    manifest = contract_manifest()
    role = str(getattr(args, "role", "") or "").strip()
    decision = str(getattr(args, "decision", "") or "").strip()
    data = manifest
    if role:
        canonical_role = canonical_role_value(role)
        data = {
            "schema_version": manifest["schema_version"],
            "contract_hash": manifest["contract_hash"],
            "role": canonical_role,
            "requested_role": role,
            "allowed_decisions": manifest["roles"].get(canonical_role, []),
            "decision_menu_shape_ids": manifest["role_shape_ids"].get(canonical_role, []),
            "context_expansion_shape_ids": manifest["context_expansion_shape_ids"].get(canonical_role, []),
            "hud_shapes": hud_shape_index_for_stage(canonical_role),
        }
    if decision:
        data = {
            "schema_version": manifest["schema_version"],
            "contract_hash": manifest["contract_hash"],
            "decision": decision,
            "contract": manifest["decisions"].get(decision),
        }
    if args.json:
        print(emit_json(data))
    else:
        print(f"contracts schema={manifest['schema_version']} hash={manifest['contract_hash'][:16]}")
    return 0 if (not decision or data.get("contract")) else 2


def _cmd_contracts_verify_examples(args) -> int:
    data = verify_registry()
    skill_examples = verify_harness_skill_examples()
    data["skill_examples"] = skill_examples
    data["ok"] = bool(data.get("ok")) and bool(skill_examples.get("ok"))
    print(emit_json(data) if args.json else f"contracts ok={data['ok']} hash={data['contract_hash'][:16]}")
    return 0 if data.get("ok") else 2


def _run_verify_command(label: str, command: list[str], *, cwd: Path) -> dict:
    started = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=120)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = str(exc.stdout or "")
        stderr = str(exc.stderr or "") + "\n[verify command timed out]"
        exit_code = 124
    return {
        "label": label,
        "command": " ".join(command),
        "cwd": str(cwd),
        "exit_code": exit_code,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "stdout_summary": _safe_output_summary(stdout),
        "stderr_summary": _safe_output_summary(stderr),
    }


def _safe_output_summary(text: str) -> str:
    text = " ".join(str(text or "").split())
    lowered = text.lower()
    if any(marker in lowered for marker in ("secret", "token", "password", "credential", "authorization")):
        return "<redacted>"
    return text[:500]


def _git_summary(root: Path) -> dict:
    def run(args: list[str]) -> str | None:
        try:
            completed = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=10)
        except Exception:
            return None
        return completed.stdout.strip() if completed.returncode == 0 else None

    status = run(["status", "--short"])
    return {"path": str(root), "git_head": run(["rev-parse", "HEAD"]), "dirty": bool(status)}


def _cmd_daemon(args) -> int:
    cfg = load_agent_runtime_config()
    command = getattr(args, "daemon_command", None)
    if command == "start":
        data = start_daemon(task_id=getattr(args, "task", None), interval_seconds=args.interval, idle_interval_seconds=args.idle_interval)
        print(emit_json(data) if args.json else f"daemon={data.get('state', 'unknown')} pid={data.get('pid', '')}")
        return 0
    if command == "stop":
        data = stop_daemon()
        print(emit_json(data) if args.json else f"daemon={data.get('state', 'unknown')}")
        return 0
    if command == "status" or (not command and not args.foreground):
        data = read_daemon_status()
        print(emit_json(data) if args.json else f"daemon={data.get('state', 'unknown')}")
        return 0
    os.environ.setdefault("HERMES_AGENT_RUNTIME_ROOT", str(paths.store_root()))

    def engine_factory():
        return TickEngine(
            config=cfg,
            persona_runtime=GPTPersonaRuntime(default_provider=cfg.default_provider, default_model=cfg.default_model),
        )

    daemon = MissionDaemon(
        engine_factory=engine_factory,
        target_task_id=getattr(args, "task", None),
        interval_seconds=args.interval if args.interval is not None else cfg.daemon_interval_seconds,
        idle_interval_seconds=args.idle_interval if args.idle_interval is not None else cfg.daemon_idle_interval_seconds,
        heartbeat_seconds=cfg.daemon_heartbeat_seconds,
    )
    max_loops = 1 if command == "run-once" else getattr(args, "max_loops", None)
    result = daemon.run_foreground(max_loops=max_loops)
    print(emit_json(result) if args.json else f"daemon stopped after {result['loops']} loops")
    return 0


def _cmd_agents(args) -> int:
    personas = ensure_persisted_personas(load_agent_runtime_config())
    print(emit_json(personas) if args.json else "\n".join(f"{p.id} ({p.role})" for p in personas))
    return 0


def _cmd_smoke(args) -> int:
    data = run_smoke(temp_root=args.temp_root, no_model=args.no_model)
    if args.json:
        print(emit_json(data))
    else:
        task = data.get("task_id", "-")
        state = data.get("final_state", data.get("failure_class", "unknown"))
        print(f"smoke={data['ok']} task={task} state={state}")
    return 0


def _cmd_proof_list(args) -> int:
    proofs=ProofStore().list_for_task(args.task_id)
    print(emit_json(proofs) if args.json else "\n".join(f"{p.id} {p.type} {p.title}" for p in proofs))
    return 0


def _safe_issue_summary(item: dict) -> dict:
    return {
        "discovery_id": item.get("id"),
        "parent_task_id": item.get("parent_task_id"),
        "title": item.get("title"),
        "severity": item.get("severity"),
        "relationship_hint": item.get("relationship_hint"),
        "triage_status": item.get("triage_status"),
        "triage_decision": item.get("triage_decision"),
        "child_task_id": item.get("child_task_id"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _cmd_issue_list(args) -> int:
    task = TaskStore().get(args.task_id)
    items = [_safe_issue_summary(item) for item in getattr(task, "issue_discoveries", []) or []]
    if args.json:
        print(emit_json(items))
    else:
        print("\n".join(f"{item['discovery_id']} [{item['triage_status']}] {item['severity']} {item['title']}" for item in items))
    return 0


def _cmd_issue_show(args) -> int:
    _task, item = find_discovery_task(TaskStore(), args.discovery_id)
    data = _safe_issue_summary(item)
    data["summary"] = item.get("summary")
    data["evidence_count"] = len(item.get("evidence", []) or [])
    data["affected_path_count"] = len(item.get("affected_paths", []) or [])
    if args.json:
        print(emit_json(data))
    else:
        print(f"{data['discovery_id']} [{data['triage_status']}] {data['title']}\nsummary: {data['summary']}")
    return 0


def _cmd_issue_triage(args) -> int:
    task_store = TaskStore(); incident_store = IncidentStore()
    task, _item = find_discovery_task(task_store, args.discovery_id)
    payload = {
        "discovery_id": args.discovery_id,
        "decision": args.decision,
        "rationale": args.rationale,
        "priority": args.priority,
    }
    if args.decision == "fork_child":
        payload.update({
            "child_title": args.child_title,
            "child_description": args.child_description,
            "child_acceptance_criteria": list(args.acceptance or []),
        })
    decision = AgentDecision(type=DecisionType.TRIAGE_ISSUE_DISCOVERY, summary=f"CLI triage {args.decision}", rationale=args.rationale, payload=payload)
    apply_planning_decision(task, decision, actor="cli", task_store=task_store, incident_store=incident_store)
    task_store.update(task, actor="cli", reason=f"issue triaged {args.decision}")
    item = next(item for item in getattr(task, "issue_discoveries", []) or [] if item.get("id") == args.discovery_id)
    data = _safe_issue_summary(item)
    print(emit_json(data) if args.json else f"triaged {data['discovery_id']} as {data['triage_status']} child_task_id={data.get('child_task_id')}")
    return 0


def _cmd_incident_list(args) -> int:
    store=IncidentStore(); incidents=store.list_all() if getattr(args, "all", False) else store.list_open()
    print(emit_json(incidents) if args.json else "\n".join(f"{i.id} {i.kind} {i.summary}" for i in incidents))
    return 0


def _cmd_incident_close(args) -> int:
    incident = IncidentStore().close(args.incident_id, reason=args.reason)
    data = {"incident_id": incident.id, "closed": incident.closed_at is not None, "reason": args.reason}
    print(emit_json(data) if args.json else f"closed {incident.id}: {args.reason}")
    return 0


def _cmd_snapshot(args) -> int:
    cfg = load_agent_runtime_config()
    snap = write_snapshot(build_snapshot())
    read_model_cfg = getattr(cfg, "read_model", None)
    if bool(getattr(read_model_cfg, "enabled", False)) and bool(getattr(read_model_cfg, "serve_snapshot_from_db", True)):
        from agent_runtime.read_model import ReadModel

        snap = ReadModel().render_snapshot()
    print(emit_json(snap) if args.json else "snapshot written")
    return 0
