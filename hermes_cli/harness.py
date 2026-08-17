from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hermes_time import now
from hermes_constants import get_hermes_home
from hermes_cli.profiles import list_profiles

from agent_runtime.cli_format import emit_json
from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config, mission_chat_clarify_token_binding, resolve_mission_chat_max_seconds
from agent_runtime.continuity import return_summary_to_parent_session
from agent_runtime.dispatch_session_policy import (
    derive_dispatch_title,
    resolve_dispatch_session_decision,
    session_established_payload,
)
from agent_runtime.coordinator_permissions import (
    CoordinatorPermissionScope,
    authorize_coordinator_action,
    scope_for_persona,
)
from agent_runtime.default_scope import (
    ensure_default_scope,
    preview_default_scope_migration,
    reconcile_default_scope_to_legacy,
)
from agent_runtime.errors import (
    DefaultScopeReconciliationRequired,
    NotFound,
    WorkspaceDeleteBlocked,
)
from agent_runtime.events import EventLog
from agent_runtime.harness_doctor import (
    DEFAULT_WORKTREE_MIN_AGE_SECONDS,
    run_harness_doctor,
)
from agent_runtime.models import AgentPersona, Event, apply_instance_model_overrides
from agent_runtime import paths
from agent_runtime.persona_assignments import (
    CHAT_BINDING_CLEARED_REASON_DELETED,
    PERSONA_INSTANCE_ID_PREFIX,
    PersonaInstanceRetireError,
    RetiredPersonaInstanceError,
    StaleModelOverrideWrite,
    PersonaAssignmentStore,
    PersonaInstanceStore,
    canonical_chat_instance_id,
    canonical_persona_instance_id,
    chat_session_owner_instance_id,
    migrate_retired_persona_assignment_task_ids,
    persona_assignment_summary,
    persona_chat_session_id_for,
    persona_instance_summary,
    persona_instance_id_for,
    resolve_default_chat_session_id_for_instance,
    safe_assignment_token,
    safe_assignment_text,
    safe_optional_token,
)
from agent_runtime.persona_chat_mints import (
    PersonaChatMintError,
    reserve_persona_chat_mint,
)
from agent_runtime.profile_context import active_profile_name
from agent_runtime.root_observability import attach_root_observability
from agent_runtime.realm_sync import (
    RealmSyncError,
    publish_realm_sync,
    pull_realm_sync,
    realm_agent_selection_state,
    realm_sync_status,
    sync_artifacts_for_workspace_agent,
)
from agent_runtime.resolution import resolution_table, resolve_runtime
from agent_runtime.migrations import effective_config_summary, migration_status
from agent_runtime.mission_chat_turns import (
    MissionChatTurnPersistOutcome,
    REPLY_RECOVERABLE_TURN_STATES,
    RESEND_BLOCKING_TURN_STATES,
    SETTLING_TURN_STATES,
    TURN_STATE_ABANDONED,
    TURN_STATE_BUDGET_EXHAUSTED,
    TURN_STATE_COMPLETED,
    TURN_STATE_EXECUTING,
    TURN_STATE_FAILED,
    TURN_STATE_NATIVE_COMMITTED,
    TURN_STATE_OUTCOME_UNKNOWN,
    TURN_STATE_PENDING,
    TURN_STATE_PROJECTED,
    TURN_STATE_RUNNING,
    abandon_mission_chat_turn,
    mark_stale_inflight_turns_interrupted,
    mission_chat_turn_record,
    mission_chat_turn_records,
    persist_mission_chat_turn,
    transition_mission_chat_turn,
)
from agent_runtime.persona_chat_continuity import (
    CLARIFY_TICKET_TTL_SECONDS,
    PersonaChatBusyError,
    PersonaChatClarifyTicketStore,
    PersonaChatMintReceiptStore,
    PERSONA_CHAT_SESSION_SOURCE,
    native_history_revision,
    persona_chat_root_lease,
    persona_chat_runtime_registry,
    safe_native_history,
)
from agent_runtime.chat_session_scope import is_canonical_session_persistence
from agent_runtime.mcp_admission import LANE_MISSION_CHAT, resolve_mcp_admission
from agent_runtime.mcp_lane import HARNESS_LANE
from agent_runtime.mission_chat_steer import start_active_mission_chat_turn, submit_mission_chat_steer
from agent_runtime.mission_chat_workdir import mission_chat_workdir_for_persona
from agent_runtime.observability import build_observability
from agent_runtime.persona_runtime import GPTPersonaRuntime, chat_lane_capability_drops
from agent_runtime.personas import profile_chat_toolsets
from agent_runtime.prompt_observability import attach_prompt_observability_turn_results, mission_chat_prompt_observability, persist_prompt_observability_context, slim_chat_final_observability, turn_usage_from_result
from agent_runtime.provider_health import provider_health_for_personas
from agent_runtime.skill_install import install_harness_skills, install_harness_skills_for_personas
from agent_runtime.states import WorkerSessionState
from agent_runtime.status import build_status
from agent_runtime.store import AgentStore
from agent_runtime.store import RealmStore, WorkspaceStore
from agent_runtime.tool_visibility import ToolVisibilityOptions, resolve_tool_visibility
from agent_runtime.tool_permissions import ChatToolPermissionStore, permission_state_for_chat
from agent_runtime.tool_turn_history import persist_tool_turn_actual

from hermes_cli.harness_support import (
    ERROR_EXIT_CODES,
    PERSONA_CHAT_SESSION_SOURCE,
    STAGE42_SCHEMA_VERSION,
    _error_envelope,
    _list_envelope,
    _object_envelope,
    _print_stage42,
    _require_yes,
    _sort_rows,
    emit_harness_error,
    harness_repo_root,
)

# --- `hermes harness usage` (account-usage lanes) contract ---------------------
# Typed per-provider account-limit snapshot the Launcher's Mission Control
# console consumes instead of scraping the human /usage text. v1 envelope is
# emitted next to `providers`; the same failure-isolation discipline applies —
# nothing in `_cmd_usage` may raise (worst case: envelope with empty lanes).
USAGE_SCHEMA = "hermes.account_usage/v1"
DEFAULT_USAGE_TIMEOUT = 20.0
# Candidate lanes, in stable emission order. A lane is only emitted when the
# operator is detected as signed-in / holding credentials for that provider.
_USAGE_LANE_PROVIDERS: tuple[str, ...] = (
    "openai-codex",
    "anthropic",
    "openrouter",
    "nous",
)


def _add_stage42_global_args(
    parser,
    *,
    controls: frozenset[str] = frozenset(),
    omit: frozenset[str] = frozenset(),
) -> None:
    """The flags EVERY stage42 verb accepts.

    A flag registered here is a PROMISE made on every one of ~60 verbs at
    once, which is exactly why an unconsumed one is worse here than anywhere
    else: it is advertised in `--help` across the whole surface, accepted
    without complaint, and does nothing. An operator who reaches for it gets
    the unfiltered answer and no signal that the flag was ignored — the
    failure mode is a WRONG ANSWER believed, not an error seen.

    ``--filter`` and ``--watch`` were both that (removed 2026-07-28). Neither
    had a reader anywhere in the stack, and ``--filter`` was not merely
    unimplemented but undefined: no grammar, no documented contract, no
    consumer. Wiring it would have meant inventing one — and standing a
    generic untyped key=value filter beside the typed, domain-aware filters
    the list verbs already carry (``goal list --state/--workspace``,
    ``run list --state``, ``checkpoint --classes``), i.e. a SECOND filtering
    authority answering the same question, with the two free to disagree.
    There is also no honest shared place to apply it: the one point every
    list verb passes through, ``_print_stage42``, runs AFTER ``--limit`` has
    already truncated, so filtering there would filter the PAGE and call it
    the set. Removing the advertisement is the complete fix; the typed
    per-verb filters remain the real surface, and an unknown flag now fails
    loudly instead of being silently swallowed.

    ``--no-color`` is deliberately kept with no reader: nothing on this lane
    emits ANSI, so the flag's contract is already satisfied by construction.
    That is a no-op that tells the truth, not one that lies.

    `tests/hermes_cli/test_harness_cli.py::test_every_stage42_global_flag_is_honored`
    pins this — a new flag here must be read somewhere on the lane, or be
    declared satisfied-by-construction like ``--no-color``.

    ``omit`` names flags a verb genuinely does not implement, so they are never
    advertised on it. This is the SAME ruling that removed ``--filter`` and
    ``--watch`` from the whole surface, applied per-verb instead of globally:
    an accepted-but-ignored flag is a wrong answer believed. It defaults empty,
    so every existing call site is unchanged; a call site that passes it owes a
    comment saying what the verb cannot do. Use it sparingly — the point of this
    helper is a uniform surface, and a verb that omits half the contract should
    prompt the question of whether it belongs on this lane at all.
    """

    def add(*flags, **kwargs):
        if any(flag in omit for flag in flags):
            return
        if any(flag in parser._option_string_actions for flag in flags):  # noqa: SLF001 - argparse has no public query
            return
        parser.add_argument(*flags, **kwargs)

    add("-o", "--output", choices=["json", "table", "yaml", "wide"], default=None)
    add("--json", action="store_true", help="Alias for -o json")
    add("-q", "--quiet", action="store_true")
    add("--no-color", action="store_true")
    add("--fields", default=None)
    if "sort" in controls:
        add("--sort", default=None)
    if "limit" in controls:
        add("--limit", type=int, default=None)
    if "cursor" in controls:
        add("--cursor", default=None)
    if "since" in controls:
        add("--since", default=None)
    if "dry_run" in controls:
        add("--dry-run", action="store_true")
    if "yes" in controls:
        add("--yes", "-y", action="store_true")
    if "idempotency_key" in controls:
        add("--idempotency-key", default=None)


def _add_coordinator_permission_args(parser) -> None:
    parser.add_argument("--coordinator-id", default=None, help="Coordinator persona id when --requested-by is coordinator; coordinator:<id> carries it inline")
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

    init = subs.add_parser("init", help="Initialize the harness store")
    init.add_argument("--json", action="store_true")
    init.set_defaults(func=_cmd_init)

    roots = subs.add_parser(
        "roots",
        help="Machine-local logical roots that make ${roots.<name>} config paths portable",
    )
    roots_subs = roots.add_subparsers(dest="roots_command", required=True)
    roots_list = roots_subs.add_parser("list", help="Show this machine's logical-root bindings (read-only)")
    _add_stage42_global_args(roots_list)
    roots_list.set_defaults(func=_cmd_roots_list)
    roots_set = roots_subs.add_parser("set", help="Bind a logical root to an absolute local path")
    roots_set.add_argument("name", help="Logical root name, e.g. eternia_launcher")
    roots_set.add_argument("path", help="Absolute path to the checkout on THIS machine")
    roots_set.add_argument("--allow-missing", action="store_true", help="Bind even when the path does not exist yet")
    _add_stage42_global_args(roots_set, controls=frozenset({"dry_run"}))
    roots_set.set_defaults(func=_cmd_roots_set)
    roots_unset = roots_subs.add_parser("unset", help="Remove a logical-root binding")
    roots_unset.add_argument("name")
    _add_stage42_global_args(roots_unset, controls=frozenset({"dry_run"}))
    roots_unset.set_defaults(func=_cmd_roots_unset)
    roots_migrate = roots_subs.add_parser(
        "migrate",
        help="Rewrite machine-local absolute paths in profile configs into ${roots.<name>} token form",
    )
    roots_migrate.add_argument("configs", nargs="*", help="config.yaml paths to migrate (default: every Hermes profile config)")
    roots_migrate.add_argument("--root", action="append", default=[], metavar="NAME=PATH", help="Explicit root binding; repeatable. Omit to auto-derive from .git ancestors.")
    roots_migrate.add_argument("--no-platform-gates", action="store_true", help="Do not add platforms:[windows] to PowerShell/.ps1-only MCP entries")
    _add_stage42_global_args(
        roots_migrate, controls=frozenset({"dry_run", "yes"})
    )
    roots_migrate.set_defaults(func=_cmd_roots_migrate)

    workspace = subs.add_parser("workspace", help="Manage Harness workspaces")
    workspace_subs = workspace.add_subparsers(dest="workspace_command", required=True)
    workspace_list = workspace_subs.add_parser("list", help="List workspaces")
    _add_stage42_global_args(workspace_list, controls=frozenset({"sort"}))
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
    # ``None`` (not "soft") so a template's isolation can win when the operator
    # did not choose one explicitly; WorkspaceStore.create defaults None→soft.
    workspace_create.add_argument("--isolation", choices=["soft", "hard"], default=None)
    workspace_create.add_argument("--max-lanes", type=int, default=None)
    workspace_create.add_argument(
        "--from-workspace",
        dest="from_workspace",
        default=None,
        help="Use this workspace (any realm) as the template: copy the scopes below into the new workspace",
    )
    workspace_create.add_argument(
        "--copy",
        action="append",
        choices=["office", "board", "agents", "settings"],
        default=None,
        help="Template scope to copy (repeatable). Default with --from-workspace: every scope. Requires --from-workspace.",
    )
    _add_stage42_global_args(workspace_create, controls=frozenset({"dry_run"}))
    workspace_create.set_defaults(func=_cmd_workspace_create)
    workspace_use = workspace_subs.add_parser("use", help="Set active workspace")
    workspace_use.add_argument("workspace_id")
    workspace_use.add_argument(
        "--issued-at",
        dest="issued_at",
        default=None,
        help="ISO-8601 UTC instant the operator issued this switch; a pointer already owned by a strictly newer intent rejects this one as superseded (transport replay guard)",
    )
    _add_stage42_global_args(workspace_use)
    workspace_use.set_defaults(func=_cmd_workspace_use)
    workspace_actors = workspace_subs.add_parser("actors", help="List typed actors in a workspace")
    workspace_actors.add_argument("workspace_id")
    workspace_actors.add_argument("--kind", default=None)
    _add_stage42_global_args(workspace_actors)
    workspace_actors.set_defaults(func=_cmd_workspace_actors)
    workspace_add_agent = workspace_subs.add_parser("add-agent", help="Add a persona to a workspace roster")
    workspace_add_agent.add_argument("workspace_id")
    workspace_add_agent.add_argument("persona_id")
    _add_stage42_global_args(
        workspace_add_agent, controls=frozenset({"dry_run"})
    )
    workspace_add_agent.set_defaults(func=_cmd_workspace_add_agent)
    workspace_remove_agent = workspace_subs.add_parser("remove-agent", help="Remove a persona from a workspace roster")
    workspace_remove_agent.add_argument("workspace_id")
    workspace_remove_agent.add_argument("persona_id")
    _add_stage42_global_args(
        workspace_remove_agent, controls=frozenset({"dry_run", "yes"})
    )
    workspace_remove_agent.set_defaults(func=_cmd_workspace_remove_agent)
    workspace_rename = workspace_subs.add_parser("rename", help="Rename a workspace")
    workspace_rename.add_argument("workspace_id")
    workspace_rename.add_argument("name")
    _add_stage42_global_args(workspace_rename, controls=frozenset({"dry_run"}))
    workspace_rename.set_defaults(func=_cmd_workspace_rename)
    workspace_archive = workspace_subs.add_parser("archive", help="Archive a workspace")
    workspace_archive.add_argument("workspace_id")
    _add_stage42_global_args(
        workspace_archive, controls=frozenset({"dry_run", "yes"})
    )
    workspace_archive.set_defaults(func=_cmd_workspace_archive)
    workspace_delete = workspace_subs.add_parser(
        "delete", help="Permanently delete a workspace and its office/board content (archive is the reversible path)"
    )
    workspace_delete.add_argument("workspace_id")
    _add_stage42_global_args(
        workspace_delete, controls=frozenset({"dry_run", "yes"})
    )
    workspace_delete.set_defaults(func=_cmd_workspace_delete)

    realm = subs.add_parser("realm", help="Manage Harness realms")
    realm_subs = realm.add_subparsers(dest="realm_command", required=True)
    realm_list = realm_subs.add_parser("list", help="List realms")
    _add_stage42_global_args(realm_list, controls=frozenset({"sort"}))
    realm_list.set_defaults(func=_cmd_realm_list)
    realm_show = realm_subs.add_parser("show", help="Show one realm")
    realm_show.add_argument("realm_id")
    _add_stage42_global_args(realm_show)
    realm_show.set_defaults(func=_cmd_realm_show)
    realm_create = realm_subs.add_parser("create", help="Create a realm")
    realm_create.add_argument("--name", required=True)
    realm_create.add_argument("--server", default=None)
    _add_stage42_global_args(realm_create, controls=frozenset({"dry_run"}))
    realm_create.set_defaults(func=_cmd_realm_create)
    realm_adopt = realm_subs.add_parser("adopt", help="Adopt server-granted realms from the Eternia backend")
    realm_adopt.add_argument("--server", default=None, help="Only adopt realms bound to this Eternia server id")
    realm_adopt.add_argument("--credential-file", default=None, help="Launcher-brokered realm sync credential JSON (fallback: HERMES_REALM_SYNC_CREDENTIAL)")
    _add_stage42_global_args(
        realm_adopt, controls=frozenset({"dry_run", "sort"})
    )
    realm_adopt.set_defaults(func=_cmd_realm_adopt)
    realm_bind = realm_subs.add_parser("bind-server", help="Bind a realm to an Eternia server id")
    realm_bind.add_argument("realm_id")
    realm_bind.add_argument("server_id")
    _add_stage42_global_args(realm_bind, controls=frozenset({"dry_run"}))
    realm_bind.set_defaults(func=_cmd_realm_bind_server)
    realm_use = realm_subs.add_parser("use", help="Set active realm")
    realm_use.add_argument("realm_id")
    realm_use.add_argument(
        "--issued-at",
        dest="issued_at",
        default=None,
        help="ISO-8601 UTC instant the operator issued this switch; a pointer already owned by a strictly newer intent rejects this one as superseded (transport replay guard)",
    )
    _add_stage42_global_args(realm_use)
    realm_use.set_defaults(func=_cmd_realm_use)
    realm_default_scope = realm_subs.add_parser(
        "default-scope",
        help="Preview default-scope adoption/reconciliation without mutating persisted state",
    )
    realm_default_scope.add_argument(
        "--dry-run",
        action="store_true",
        help="Inventory only; never mutates persisted state",
    )
    realm_default_scope.add_argument("--winner-realm", default=None)
    realm_default_scope.add_argument("--winner-workspace", default=None)
    realm_default_scope.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Apply the explicitly selected recoverable reconciliation",
    )
    _add_stage42_global_args(realm_default_scope)
    realm_default_scope.set_defaults(func=_cmd_realm_default_scope)
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
    _add_stage42_global_args(realm_sync_pull, controls=frozenset({"dry_run"}))
    realm_sync_pull.set_defaults(func=_cmd_realm_sync_pull)
    realm_sync_publish = realm_sync_subs.add_parser("publish", help="Publish allowlisted realm sync artifacts")
    realm_sync_publish.add_argument("realm_id")
    realm_sync_publish.add_argument("--credential-file", default=None, help="Launcher-brokered realm sync credential JSON (fallback: HERMES_REALM_SYNC_CREDENTIAL)")
    _add_stage42_global_args(
        realm_sync_publish, controls=frozenset({"dry_run", "yes"})
    )
    realm_sync_publish.set_defaults(func=_cmd_realm_sync_publish)
    realm_sync_held = realm_sync_subs.add_parser(
        "held",
        help="List profile files (MEMORY.md / core context / persona prompts) a pull HELD because the member's copy diverged from the realm's",
    )
    realm_sync_held.add_argument("realm_id")
    _add_stage42_global_args(realm_sync_held)
    realm_sync_held.set_defaults(func=_cmd_realm_sync_held)
    realm_sync_resolve = realm_sync_subs.add_parser(
        "resolve",
        help="Resolve one held profile file: --take local keeps the member's content, --take remote adopts the realm's",
    )
    realm_sync_resolve.add_argument("realm_id")
    realm_sync_resolve.add_argument("--key", required=True, help="Entity key from `realm sync held` (e.g. alice:memories/MEMORY.md)")
    realm_sync_resolve.add_argument("--take", required=True, choices=["local", "remote"])
    _add_stage42_global_args(
        realm_sync_resolve, controls=frozenset({"dry_run", "yes"})
    )
    realm_sync_resolve.set_defaults(func=_cmd_realm_sync_resolve)

    realm_skills = realm_subs.add_parser("skills", help="Per-realm selection of which shared skills publish to a realm")
    realm_skills_subs = realm_skills.add_subparsers(dest="realm_skills_command", required=True)
    realm_skills_show = realm_skills_subs.add_parser("show", help="Show a realm's shared-skill publish selection (read-only, local store)")
    realm_skills_show.add_argument("realm_id")
    _add_stage42_global_args(realm_skills_show)
    realm_skills_show.set_defaults(func=_cmd_realm_skills_show)
    realm_skills_set = realm_skills_subs.add_parser(
        "set",
        help="Set a realm's shared-skill publish selection (local, reversible store edit — no --yes gate, like `realm use`)",
    )
    realm_skills_set.add_argument("realm_id")
    realm_skills_set.add_argument("--all", dest="publish_all", action="store_true", help="Publish all shared skills (mode=all; the stored selection list is preserved)")
    realm_skills_set.add_argument("--skills", dest="skills", default=None, help="Comma-separated skill slugs to publish (mode=selected)")
    realm_skills_set.add_argument("--none", dest="publish_none", action="store_true", help="Publish no skills (mode=selected, empty selection)")
    _add_stage42_global_args(realm_skills_set, controls=frozenset({"dry_run"}))
    realm_skills_set.set_defaults(func=_cmd_realm_skills_set)

    realm_agents = realm_subs.add_parser(
        "agents", help="Per-realm selection of which persona definitions publish to a realm"
    )
    realm_agents_subs = realm_agents.add_subparsers(
        dest="realm_agents_command", required=True
    )
    realm_agents_show = realm_agents_subs.add_parser(
        "show", help="Show a realm's persona-definition publish selection"
    )
    realm_agents_show.add_argument("realm_id")
    _add_stage42_global_args(realm_agents_show)
    realm_agents_show.set_defaults(func=_cmd_realm_agents_show)
    realm_agents_set = realm_agents_subs.add_parser(
        "set",
        help="Set a realm's persona-definition selection (required workspace/Office references remain pinned)",
    )
    realm_agents_set.add_argument("realm_id")
    realm_agents_set.add_argument(
        "--workspace",
        dest="publish_workspace",
        action="store_true",
        help="Publish only definitions required by workspace rosters and Office placements",
    )
    realm_agents_set.add_argument(
        "--agents",
        dest="agents",
        default=None,
        help="Comma-separated persona ids to publish in addition to required references",
    )
    realm_agents_set.add_argument(
        "--none",
        dest="publish_none",
        action="store_true",
        help="Clear the explicit selection; required references remain pinned",
    )
    _add_stage42_global_args(realm_agents_set, controls=frozenset({"dry_run"}))
    realm_agents_set.set_defaults(func=_cmd_realm_agents_set)

    flow = subs.add_parser("flow", help="Operator flow-graph documents: ingest the Launcher's authored agent map whole and set the referenced instances' steering relations")
    flow_subs = flow.add_subparsers(dest="flow_command", required=True)
    flow_set = flow_subs.add_parser("set", help="Store one flow-graph JSON doc and reconcile steering for the EXISTING instances it references (never creates instances)")
    flow_set.add_argument("--graph", default=None, help="The flow-graph JSON document, inline")
    flow_set.add_argument("--graph-file", default=None, help="Path to a file holding the flow-graph JSON document")
    flow_set.add_argument("--requested-by", default="operator")
    flow_set.add_argument("--json", action="store_true")
    flow_set.set_defaults(func=_cmd_flow_set)
    flow_show = flow_subs.add_parser("show", help="Show the runtime's stored copy of one flow-graph doc")
    flow_show.add_argument("graph_id")
    flow_show.add_argument("--json", action="store_true")
    flow_show.set_defaults(func=_cmd_flow_show)
    flow_list = flow_subs.add_parser("list", help="List stored flow-graph doc ids")
    flow_list.add_argument("--json", action="store_true")
    flow_list.set_defaults(func=_cmd_flow_list)

    checkpoint = subs.add_parser(
        "checkpoint",
        help="Per-actor read-model checkpoint: bundle the on-disk entity-class stores into a keyed transport envelope (the store IS the checkpoint)",
    )
    checkpoint_subs = checkpoint.add_subparsers(dest="checkpoint_command", required=True)
    checkpoint_fetch = checkpoint_subs.add_parser(
        "fetch",
        help="Bundle the per-actor store files into a keyed checkpoint envelope (entity class -> actor id -> row read verbatim); read-only, writes nothing",
    )
    checkpoint_fetch.add_argument(
        "--classes",
        default=None,
        help="Comma-separated entity-class filter (default: all discovered classes); absent/unknown names are accounted in requested_absent",
    )
    checkpoint_fetch.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Optional per-class row cap; truncation is accounted ({truncated,total,returned}), never silent",
    )
    checkpoint_fetch.add_argument("--json", action="store_true")
    checkpoint_fetch.set_defaults(func=_cmd_checkpoint_fetch)
    checkpoint_classes = checkpoint_subs.add_parser(
        "classes",
        help="List discovered entity classes with per-class actor counts and byte sizes (stat only; contents not read)",
    )
    checkpoint_classes.add_argument("--json", action="store_true")
    checkpoint_classes.set_defaults(func=_cmd_checkpoint_classes)

    skills = subs.add_parser("skills", help="Inspect the shared skills substrate the Launcher's Skills console consumes")
    skills_subs = skills.add_subparsers(dest="skills_command", required=True)
    skills_inventory_cmd = skills_subs.add_parser(
        "inventory",
        help="Typed snapshot of the shared skill catalog, per-persona grants, and per-realm publish/drift state",
    )
    skills_inventory_cmd.add_argument("--json", action="store_true", help="Emit the skills_inventory/v1 contract as JSON")
    skills_inventory_cmd.set_defaults(func=_cmd_skills_inventory)
    skills_catalog_cmd = skills_subs.add_parser(
        "catalog",
        help="S8: resolve ONE content-addressed skills catalog by its hash (the frame ships only *_ref hashes; bodies are fetched once and cached forever)",
    )
    skills_catalog_cmd.add_argument("--hash", dest="content_hash", required=True, help="The content hash carried by a chat_contexts row's available_skills_ref / accessible_skills_ref")
    skills_catalog_cmd.add_argument("--json", action="store_true")
    skills_catalog_cmd.set_defaults(func=_cmd_skills_catalog)

    skills_publishable_cmd = skills_subs.add_parser(
        "publishable",
        help="List every resolvable skill with whether it can reach a realm, and if not, the typed reason (read-only)",
    )
    skills_publishable_cmd.add_argument(
        "--source-kind",
        dest="source_kind",
        default=None,
        choices=["profile_local", "shared_core", "external"],
        help="Restrict to one resolver tier (default: all three)",
    )
    skills_publishable_cmd.add_argument(
        "--unpublishable-only",
        dest="unpublishable_only",
        action="store_true",
        help="Show only packages that cannot reach a realm as they stand",
    )
    _add_stage42_global_args(skills_publishable_cmd)
    skills_publishable_cmd.set_defaults(func=_cmd_skills_publishable)

    skills_inbox_cmd = skills_subs.add_parser(
        "inbox",
        help="List quarantined per-realm inbox skill packages and how each would reconcile (read-only)",
    )
    skills_inbox_cmd.add_argument("--realm", default=None, help="Restrict to one realm id (default: all realms)")
    _add_stage42_global_args(skills_inbox_cmd)
    skills_inbox_cmd.set_defaults(func=_cmd_skills_inbox)

    skills_promote_cmd = skills_subs.add_parser(
        "promote",
        help="Promote a held / authored / profile-local skill package into the canonical shared root (hash-guarded, never-delete)",
    )
    skills_promote_cmd.add_argument("skill", help="Canonical skill slug (bare name or <category>/<name>)")
    skills_promote_cmd.add_argument("--from-realm", dest="from_realm", default=None, help="Promote from this realm's inbox mirror")
    skills_promote_cmd.add_argument("--from-profile", dest="from_profile", default=None, help="Promote from a profile's skills/ (implies --move-source)")
    skills_promote_cmd.add_argument("--from-path", dest="from_path", default=None, help="Promote from an explicit package directory")
    skills_promote_cmd.add_argument("--adopt-divergent", dest="adopt_divergent", action="store_true", help="Adopt over a divergent canonical (archives the previous copy)")
    skills_promote_cmd.add_argument("--move-source", dest="move_source", action="store_true", help="Archive the source package after a successful promotion (retire the duplicate)")
    _add_stage42_global_args(
        skills_promote_cmd, controls=frozenset({"dry_run"})
    )
    skills_promote_cmd.set_defaults(func=_cmd_skills_promote)

    prompt_context = subs.add_parser(
        "prompt-context",
        help="S8: on-demand prompt-observability contexts (the frame ships only LIVE persona instances' current-session rows; historical rows are fetched here)",
    )
    prompt_context_subs = prompt_context.add_subparsers(dest="prompt_context_command", required=True)
    prompt_context_show = prompt_context_subs.add_parser(
        "show",
        help="Show one persisted prompt-observability context by id (read-only; the persisted files stay on disk after frame eviction)",
    )
    prompt_context_show.add_argument("--context-id", dest="context_id", required=True)
    prompt_context_show.add_argument("--json", action="store_true")
    prompt_context_show.set_defaults(func=_cmd_prompt_context_show)

    board = subs.add_parser("board", help="Manage Mission Board planning boards + cards (planning only — cards never drive runtime execution)")
    board_subs = board.add_subparsers(dest="board_command", required=True)
    board_list = board_subs.add_parser("list", help="List boards")
    board_list.add_argument("--workspace", default=None)
    _add_stage42_global_args(board_list, controls=frozenset({"sort"}))
    board_list.set_defaults(func=_cmd_board_list)
    board_show = board_subs.add_parser("show", help="Show one board")
    board_show.add_argument("board_id")
    board_show.add_argument("--full", action="store_true", help="Include card bodies")
    _add_stage42_global_args(board_show)
    board_show.set_defaults(func=_cmd_board_show)
    board_create = board_subs.add_parser("create", help="Create a board")
    board_create.add_argument("--workspace", required=True)
    board_create.add_argument("--title", default=None)
    _add_stage42_global_args(board_create, controls=frozenset({"dry_run"}))
    board_create.set_defaults(func=_cmd_board_create)
    board_update = board_subs.add_parser("update", help="Update a board title/columns")
    board_update.add_argument("board_id")
    board_update.add_argument("--title", default=None)
    board_update.add_argument("--columns-json", dest="columns_json", default=None, help="JSON array of {column_id,title,kind,wip_limit}")
    board_update.add_argument("--expect-revision", dest="expect_revision", type=int, default=None)
    _add_stage42_global_args(board_update)
    board_update.set_defaults(func=_cmd_board_update)

    board_card = board_subs.add_parser("card", help="Manage board cards")
    board_card_subs = board_card.add_subparsers(dest="board_card_command", required=True)
    card_add = board_card_subs.add_parser("add", help="Add a card")
    card_add.add_argument("--board", default=None, help="Board id (default: active workspace's default board)")
    card_add.add_argument("--workspace", default=None)
    card_add.add_argument("--title", required=True)
    card_add.add_argument("--description", default="")
    card_add.add_argument("--column", default=None, help="Column id or kind (default: first queued column)")
    card_add.add_argument("--priority", default=None, choices=["p0", "p1", "p2", "p3"])
    card_add.add_argument("--labels", default=None, help="Comma-separated labels")
    card_add.add_argument("--assignee", default=None)
    card_add.add_argument("--created-by", dest="created_by", default=None, help="operator (default) or a persona id")
    _add_stage42_global_args(
        card_add, controls=frozenset({"dry_run", "idempotency_key"})
    )
    card_add.set_defaults(func=_cmd_board_card_add)
    card_edit = board_card_subs.add_parser("edit", help="Edit a card")
    card_edit.add_argument("card_id")
    card_edit.add_argument("--title", default=None)
    card_edit.add_argument("--description", default=None)
    card_edit.add_argument("--priority", default=None, choices=["p0", "p1", "p2", "p3"])
    card_edit.add_argument("--labels", default=None, help="Comma-separated labels (replaces)")
    card_edit.add_argument("--assignee", default=None)
    card_edit.add_argument("--clear-assignee", dest="clear_assignee", action="store_true")
    card_edit.add_argument("--expect-revision", dest="expect_revision", type=int, default=None)
    _add_stage42_global_args(
        card_edit, controls=frozenset({"idempotency_key"})
    )
    card_edit.set_defaults(func=_cmd_board_card_edit)
    card_move = board_card_subs.add_parser("move", help="Move a card to a column / position")
    card_move.add_argument("card_id")
    card_move.add_argument("--column", required=True, help="Target column id or kind")
    card_move.add_argument("--before", default=None, help="Place before this card id")
    card_move.add_argument("--after", default=None, help="Place after this card id")
    card_move.add_argument("--expect-revision", dest="expect_revision", type=int, default=None)
    _add_stage42_global_args(
        card_move, controls=frozenset({"idempotency_key"})
    )
    card_move.set_defaults(func=_cmd_board_card_move)
    card_archive = board_card_subs.add_parser("archive", help="Archive a card (archive-never-delete)")
    card_archive.add_argument("card_id")
    _add_stage42_global_args(card_archive)
    card_archive.set_defaults(func=_cmd_board_card_archive)
    card_restore = board_card_subs.add_parser("restore", help="Restore an archived card")
    card_restore.add_argument("card_id")
    _add_stage42_global_args(card_restore)
    card_restore.set_defaults(func=_cmd_board_card_restore)

    board_resolve = board_subs.add_parser("resolve-conflict", help="Resolve a realm-sync conflict on a card")
    board_resolve.add_argument("card_id")
    board_resolve.add_argument("--take", required=True, choices=["local", "remote"])
    _add_stage42_global_args(board_resolve)
    board_resolve.set_defaults(func=_cmd_board_resolve_conflict)

    office = subs.add_parser("office", help="Manage the Mission Office layout (one file per actor placement; realm-synced like boards)")
    office_subs = office.add_subparsers(dest="office_command", required=True)
    office_show = office_subs.add_parser("show", help="Show a workspace's office surface + actors")
    office_show.add_argument("--workspace", default=None)
    office_show.add_argument("--full", action="store_true", help="Include actor item bodies")
    _add_stage42_global_args(office_show)
    office_show.set_defaults(func=_cmd_office_show)
    office_actor_upsert = office_subs.add_parser("actor-upsert", help="Create or update one actor placement (keys are minted store-side)")
    office_actor_upsert.add_argument("--workspace", default=None)
    office_actor_upsert.add_argument("--actor-json", dest="actor_json", required=True, help="Actor object (path or inline JSON): {persona_id, persona_instance_id?, backing_profile?, items:[...]}")
    # Optional, never required: class-keyed placements are a legal shape (see
    # OfficeStore.archive_actors_for_instance). The re-key migration's fence is
    # the CONDITIONAL refusal in _cmd_office_actor_upsert, not a mandatory flag.
    office_actor_upsert.add_argument("--persona-instance-id", dest="persona_instance_id", default=None, help="Bind the placement to this persona instance (overrides --actor-json's persona_instance_id); the store still mints the key")
    office_actor_upsert.add_argument("--allow-class-key", dest="allow_class_key", action="store_true", help="Escape hatch: force a class-keyed write that would otherwise be refused for re-creating an archived or duplicated placement")
    office_actor_upsert.add_argument("--expect-revision", dest="expect_revision", type=int, default=None)
    office_actor_upsert.add_argument("--updated-by", dest="updated_by", default=None)
    _add_stage42_global_args(
        office_actor_upsert, controls=frozenset({"dry_run"})
    )
    office_actor_upsert.set_defaults(func=_cmd_office_actor_upsert)
    office_actor_remove = office_subs.add_parser("actor-remove", help="Archive an actor placement (archive-never-delete)")
    office_actor_remove.add_argument("--workspace", default=None)
    office_actor_remove.add_argument("--actor", required=True, help="Actor key")
    office_actor_remove.add_argument("--reason", default=None)
    office_actor_remove.add_argument("--expect-revision", dest="expect_revision", type=int, default=None)
    _add_stage42_global_args(
        office_actor_remove, controls=frozenset({"dry_run"})
    )
    office_actor_remove.set_defaults(func=_cmd_office_actor_remove)
    office_actor_restore = office_subs.add_parser("actor-restore", help="Restore an archived actor placement")
    office_actor_restore.add_argument("--workspace", default=None)
    office_actor_restore.add_argument("--actor", required=True, help="Actor key")
    _add_stage42_global_args(
        office_actor_restore, controls=frozenset({"dry_run"})
    )
    office_actor_restore.set_defaults(func=_cmd_office_actor_restore)
    office_set_folders = office_subs.add_parser("set-folders", help="Replace the surface's shared folder taxonomy")
    office_set_folders.add_argument("--workspace", default=None)
    office_set_folders.add_argument("--folders", required=True, help="Comma-separated folder names (structural defaults always kept)")
    office_set_folders.add_argument("--expect-revision", dest="expect_revision", type=int, default=None)
    _add_stage42_global_args(
        office_set_folders, controls=frozenset({"dry_run"})
    )
    office_set_folders.set_defaults(func=_cmd_office_set_folders)
    office_resolve = office_subs.add_parser("resolve-conflict", help="Resolve a realm-sync conflict on an actor placement")
    office_resolve.add_argument("--workspace", default=None)
    office_resolve.add_argument("--actor", required=True, help="Actor key")
    office_resolve.add_argument("--take", required=True, choices=["local", "remote"])
    office_resolve.add_argument("--allow-class-key", dest="allow_class_key", action="store_true", help="Escape hatch: adopt a remote actor on a persona CLASS key the re-key migration archived (re-creates the class-keyed placement beside its instance-keyed sibling)")
    _add_stage42_global_args(
        office_resolve, controls=frozenset({"dry_run"})
    )
    office_resolve.set_defaults(func=_cmd_office_resolve_conflict)

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
    persona_tool_diff.add_argument("persona_id", help="Persona id")
    persona_tool_diff.add_argument("--session-id", default=None)
    # Unset ⇒ preview the persona under the RUNTIME DEFAULT
    # (``agent_runtime.tool_permissions.default_mode``, shipped ``unbounded``),
    # which is what a real turn gets. Pass a mode to preview a hypothetical one:
    # ``--permission-mode profile_default`` still renders the bounded shape.
    persona_tool_diff.add_argument(
        "--permission-mode",
        default=None,
        help=(
            "Preview under this permission mode (profile_default | bounded | "
            "read_only | unbounded). Default: the runtime default from the ROOT config."
        ),
    )
    persona_tool_diff.add_argument("--repo-scope", default=None)
    persona_tool_diff.add_argument("--workdir", default=None)
    persona_tool_diff.add_argument(
        "--explain-mcp",
        action="store_true",
        help=(
            "Explain MCP admission for this persona (requested / admitted / denied, "
            "with typed reasons). Inspection only — resolves policy without "
            "connecting to or registering any MCP server."
        ),
    )
    persona_tool_diff.add_argument(
        "--explain-envelope",
        action="store_true",
        help=(
            "Explain the terminal safety envelope for this persona's mission-chat "
            "lane: whether the lane binds an envelope scope, which command classes "
            "are operator-grantable vs a hard floor no config lifts, which grants "
            "are active from the ROOT config, and any typed grant-config issues. "
            "Inspection only — resolves policy without running a command."
        ),
    )
    persona_tool_diff.add_argument("--json", action="store_true")
    persona_tool_diff.set_defaults(func=_cmd_persona_tool_diff)
    persona_permission = persona_subs.add_parser(
        "permission",
        help=(
            "Restrict (or clear a restriction on) one chat session's tool permissions. "
            "The runtime default is the standing posture — this is the temporary "
            "narrowing lane, not an escalation ritual."
        ),
    )
    persona_permission_subs = persona_permission.add_subparsers(dest="persona_permission_command")
    persona_permission_set = persona_permission_subs.add_parser(
        "set",
        help=(
            "Set this session's permission mode. 'bounded' / 'read_only' RESTRICT it "
            "below the runtime default; 'profile_default' CLEARS the restriction "
            "(the session falls back to the runtime default); 'unbounded' is normally "
            "redundant with the default and only needed when the default is narrower."
        ),
    )
    persona_permission_set.add_argument("persona_id", help="Persona id")
    persona_permission_set.add_argument("--session-id", required=True)
    persona_permission_set.add_argument(
        "--mode",
        choices=["profile_default", "bounded", "read_only", "unbounded"],
        required=True,
        help=(
            "bounded = the historical bounded tier (persona-safety blocks + chat-lane "
            "cost cuts + envelope grants table only); read_only = bounded plus the "
            "mutating-tool block and the reviewer-shaped MCP subset; profile_default = "
            "no opinion, defer to the runtime default; unbounded = full access."
        ),
    )
    persona_permission_set.add_argument("--reason", required=True)
    persona_permission_set.add_argument(
        "--turns",
        type=int,
        default=None,
        help="Expire the restriction after this many turns (any restricting mode decrements).",
    )
    persona_permission_set.add_argument("--ttl-seconds", type=int, default=None)
    persona_permission_set.add_argument("--expires-at", default=None)
    persona_permission_set.add_argument("--json", action="store_true")
    persona_permission_set.set_defaults(func=_cmd_persona_permission_set)
    persona_assignments = persona_subs.add_parser("assignments", help="List persona assignments")
    persona_assignments.add_argument("--persona", dest="persona_id", default=None)
    persona_assignments.add_argument("--json", action="store_true")
    persona_assignments.set_defaults(func=_cmd_persona_assignments)
    persona_assignment_task_id_migration = persona_subs.add_parser(
        "migrate-assignment-task-ids",
        help="Archive pre-retirement persona assignments whose task_id is non-null",
    )
    persona_assignment_task_id_migration.add_argument("--dry-run", action="store_true")
    persona_assignment_task_id_migration.add_argument("--json", action="store_true")
    persona_assignment_task_id_migration.set_defaults(
        func=_cmd_persona_assignment_task_id_migration
    )
    persona_chat = persona_subs.add_parser("chat", help="Manage durable persona chat sessions")
    persona_chat_subs = persona_chat.add_subparsers(dest="persona_chat_command")
    persona_chat_delete = persona_chat_subs.add_parser("delete", help="Delete a persona chat session and clear active persona bindings")
    persona_chat_delete.add_argument("--session-id", required=True)
    persona_chat_delete.add_argument("--persona", dest="persona_id", default=None)
    persona_chat_delete.add_argument("--persona-instance-id", default=None)
    persona_chat_delete.add_argument("--requested-by", default="cli")
    persona_chat_delete.add_argument("--json", action="store_true")
    persona_chat_delete.set_defaults(func=_cmd_persona_chat_delete)
    persona_chat_history = persona_chat_subs.add_parser("history", help="Page a persona chat session's complete redaction-safe transcript")
    persona_chat_history.add_argument("--session-id", dest="session_id", required=True)
    persona_chat_history.add_argument("--limit", type=int, default=40, help="Page size (clamped 1..40)")
    persona_chat_history.add_argument("--before", default=None, help="Opaque cursor returned by the previous page")
    persona_chat_history.add_argument("--json", action="store_true")
    persona_chat_history.set_defaults(func=_cmd_persona_chat_history)

    persona_set_model = persona_subs.add_parser("set-model", help="Persist a persona's default provider/model (profile-default lane; future instances inherit it)")
    persona_set_model.add_argument("persona_id", help="Persona id or profile:<name>")
    persona_set_model.add_argument("--provider", default=None, help="Provider lane (canonical name or alias; api_mode is derived from it)")
    persona_set_model.add_argument("--model", default=None, help="Model id for the provider lane")
    persona_set_model.add_argument("--use-default", action="store_true", help="Clear the persona's model/provider/api_mode so the runtime default cascade applies")
    persona_set_model.add_argument("--issued-at", default=None, help="ISO-8601 issue timestamp; stale writes are superseded instead of applied")
    persona_set_model.add_argument("--requested-by", default="operator")
    persona_set_model.add_argument("--json", action="store_true")
    persona_set_model.set_defaults(func=_cmd_persona_set_model)
    persona_instance = persona_subs.add_parser("instance", help="Create, open, steer, retire, and maintain persona instances (chat is the only messaging lane)")
    persona_instance_subs = persona_instance.add_subparsers(dest="persona_instance_command")
    persona_instance_create = persona_instance_subs.add_parser("create", help="Create an Agent Profile (operator chat channel) or an additional placement-backed instance (--add-instance); requires --display-name")
    persona_instance_create.add_argument("--persona", dest="persona_id", required=True)
    persona_instance_create.add_argument("--title", required=True, help="Fallback display name when --display-name is empty (launcher wire-compat)")
    # S70: `--message` is accepted for launcher wire-compat only and is NOT
    # acted on — the free-floating assignment queue it used to feed is retired
    # (messaging is `harness mission-chat message`). Removing the flag needs a
    # lockstep launcher change: mission_control_bridge.dart emits it on every
    # persona.instance.create / persona.profile.instantiate call.
    persona_instance_create.add_argument("--message", required=True, help="DEPRECATED: accepted for launcher wire-compat and ignored; send messages with `harness mission-chat message`")
    persona_instance_create.add_argument("--requested-by", default="cli")
    _add_coordinator_permission_args(persona_instance_create)
    persona_instance_create.add_argument("--client-message-id", default=None)
    persona_instance_create.add_argument("--display-name", default=None)
    persona_instance_create.add_argument("--session-id", default=None)
    persona_instance_create.add_argument("--kill-active", action="store_true", help="Cancel the current run/worker before replacing the active chat")
    persona_instance_create.add_argument("--add-instance", action="store_true", help="Create an additional placement-backed instance instead of targeting the primary placement")
    persona_instance_create.add_argument("--placement-id", default=None, help="Scene itemId for an additional placement-backed instance")
    persona_instance_create.add_argument("--workspace-id", dest="workspace_id", default=None, help="Mission Control workspace the placement belongs to (scope-provenance pointer; only meaningful with --add-instance)")
    persona_instance_create.add_argument("--realm-id", dest="realm_id", default=None, help="Mission Control realm the placement belongs to (scope-provenance pointer; only meaningful with --add-instance)")
    # S70 removed `--auto-run` / `--stream` / `--max-actions` / `--max-seconds`:
    # they belonged to the retired free-floating assignment queue (argparse now
    # rejects them cleanly instead of silently ignoring them).
    persona_instance_create.add_argument("--json", action="store_true")
    persona_instance_create.set_defaults(func=_cmd_persona_instance_create)
    persona_instance_open = persona_instance_subs.add_parser("open-chat", help="Bind a persona instance to a durable chat session without ticking")
    persona_instance_open.add_argument("--persona", dest="persona_id", required=True)
    persona_instance_open.add_argument("--persona-instance-id", default=None, help="Exact existing persona instance to bind when minting a new chat")
    persona_instance_open.add_argument("--session-id", default=None)
    persona_instance_open.add_argument("--new-session", action="store_true", help="Mint and select a fresh server-owned chat session for the target instance")
    persona_instance_open.add_argument("--idempotency-key", default=None, help="Stable retry key required with --new-session")
    persona_instance_open.add_argument("--kill-active", action="store_true", help="Cancel the current run/worker before replacing the active chat")
    persona_instance_open.add_argument("--add-instance", action="store_true", help="Open the chat on an additional placement-backed instance")
    persona_instance_open.add_argument("--placement-id", default=None, help="Scene itemId for an additional placement-backed instance")
    persona_instance_open.add_argument("--workspace-id", dest="workspace_id", default=None, help="Mission Control workspace the placement belongs to (scope-provenance pointer; only meaningful with --add-instance)")
    persona_instance_open.add_argument("--realm-id", dest="realm_id", default=None, help="Mission Control realm the placement belongs to (scope-provenance pointer; only meaningful with --add-instance)")
    persona_instance_open.add_argument("--display-name", default=None, help="Authoritative name for a deliberately placed additional instance; ignored unless --add-instance")
    persona_instance_open.add_argument("--requested-by", default="cli")
    _add_coordinator_permission_args(persona_instance_open)
    persona_instance_open.add_argument("--json", action="store_true")
    persona_instance_open.set_defaults(func=_cmd_persona_instance_open_chat)
    persona_instance_resolve_turn = persona_instance_subs.add_parser(
        "resolve-chat-turn", help="Strictly resolve one ambiguous persona-chat turn"
    )
    persona_instance_resolve_turn.add_argument("persona_instance_id")
    persona_instance_resolve_turn.add_argument("--session-id", required=True)
    persona_instance_resolve_turn.add_argument("--client-message-id", required=True)
    persona_instance_resolve_turn.add_argument("--turn-id", required=True)
    persona_instance_resolve_turn.add_argument("--action", choices=["abandon"], required=True)
    persona_instance_resolve_turn.add_argument("--json", action="store_true")
    persona_instance_resolve_turn.set_defaults(func=_cmd_mission_chat_turn_resolve)
    # S70 removed `persona instance message`. It queued a "free-floating
    # persona assignment" — a lane whose only durable consumer was the tick
    # loop removed by the 2026-07-30 chat-only purge (a queued row dead-ended
    # forever; the advertised `run-once` follow-up verb never existed), and its
    # `--auto-run` variant was a second, parallel turn authority. Messaging an
    # instance is `harness mission-chat message`.
    persona_instance_close = persona_instance_subs.add_parser("close", help="Cancel residual free-floating assignment rows for one persona instance (maintenance; the lane that minted them is retired)")
    persona_instance_close.add_argument("persona_instance_id")
    persona_instance_close.add_argument("--reason", required=True)
    persona_instance_close.add_argument("--requested-by", default="cli")
    _add_coordinator_permission_args(persona_instance_close)
    persona_instance_close.add_argument("--json", action="store_true")
    persona_instance_close.set_defaults(func=_cmd_persona_instance_close)
    persona_instance_archive = persona_instance_subs.add_parser("archive", help="Complete residual free-floating assignment rows for one persona instance (maintenance; the lane that minted them is retired)")
    persona_instance_archive.add_argument("persona_instance_id")
    persona_instance_archive.add_argument("--reason", default="archived residual free-floating assignment row")
    persona_instance_archive.add_argument("--requested-by", default="cli")
    persona_instance_archive.add_argument("--json", action="store_true")
    persona_instance_archive.set_defaults(func=_cmd_persona_instance_archive)
    persona_instance_retire = persona_instance_subs.add_parser("retire", help="Retire (end-of-life) a placement-backed persona instance: archive its row (chat history preserved)")
    persona_instance_retire.add_argument("persona_instance_id")
    persona_instance_retire.add_argument("--reason", default="placement removed")
    persona_instance_retire.add_argument("--requested-by", default="cli")
    _add_coordinator_permission_args(persona_instance_retire)
    persona_instance_retire.add_argument("--json", action="store_true")
    persona_instance_retire.set_defaults(func=_cmd_persona_instance_retire)
    # No `sweep-orphans`: S65 retired the owning-task release inference the
    # janitor decided on (and de-registered the `persona_instance.reaped` event
    # it emitted), leaving only this registration and a handler that raised
    # AttributeError. Retired at S66 with them. `persona instance retire` is the
    # live end-of-life verb.
    persona_instance_steer = persona_instance_subs.add_parser("steer", help="Re-route a persona instance's living-graph wiring (Stage 77 steering edge; supports multi-parent fan-in)")
    persona_instance_steer.add_argument("persona_instance_id")
    persona_instance_steer.add_argument("--parent", dest="parent_instance_id", default=None, help="Back-compat: REPLACE the steering set with this single parent (== --set-parents <p>)")
    persona_instance_steer.add_argument("--add-parent", dest="add_parent", default=None, help="Additively ADD one parent to the steering set (fan-in; idempotent)")
    persona_instance_steer.add_argument("--remove-parent", dest="remove_parent", default=None, help="Remove ONE parent from the steering set (detach-one; last one detaches)")
    persona_instance_steer.add_argument("--set-parents", dest="set_parents", nargs="+", default=None, metavar="PARENT", help="Declaratively REPLACE the whole steering set with these parents (fan-in)")
    persona_instance_steer.add_argument("--goal", dest="goal_id", default=None, help="Correlation id this sub-agent inherits from its parent; rides the snapshot as persona_instance.goal_id, which the Launcher groups its agent rooms by")
    persona_instance_steer.add_argument("--detach", action="store_true", help="Detach from ALL parents and clear the inherited correlation id (becomes a standalone owner)")
    persona_instance_steer.add_argument("--requested-by", default="operator")
    _add_coordinator_permission_args(persona_instance_steer)
    persona_instance_steer.add_argument("--json", action="store_true")
    persona_instance_steer.set_defaults(func=_cmd_persona_instance_steer)
    persona_instance_repair = persona_instance_subs.add_parser(
        "repair-steering",
        help="Strip non-instance principals (e.g. the operator) out of a persona instance's steering fields; --dry-run previews without writing or emitting",
    )
    persona_instance_repair.add_argument("persona_instance_id", nargs="?", default=None, help="Target one row; omit and pass --all to scan every row")
    persona_instance_repair.add_argument("--all", action="store_true", help="Scan and repair every persona-instance row")
    _add_stage42_global_args(
        persona_instance_repair,
        controls=frozenset({"dry_run"}),
        omit=frozenset({"--output", "--quiet", "--fields"}),
    )
    persona_instance_repair.set_defaults(func=_cmd_persona_instance_repair_steering)
    persona_instance_return = persona_instance_subs.add_parser("return-summary", help="Post a bounded child summary back into a parent chat session")
    persona_instance_return.add_argument("persona_instance_id")
    persona_instance_return.add_argument("--parent-session-id", required=True)
    persona_instance_return.add_argument("--summary", required=True)
    persona_instance_return.add_argument("--proof-id", dest="proof_ids", action="append", default=[])
    persona_instance_return.add_argument("--artifact-ref", dest="artifact_refs", action="append", default=[])
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
    persona_instance_set_model.add_argument("--reasoning-effort", dest="reasoning_effort", default=None, help="Per-instance reasoning effort for reasoning-capable models (none, minimal, low, medium, high, xhigh); empty string clears it")
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
    # store_true (absent → False) is deliberate on THIS lane: False is now an
    # explicit "continue the target's current default thread", so the operator
    # console and any bare CLI send keep threading exactly as before (that
    # pointer follows the most recently established thread — it is not a
    # separate durable pair thread), while a caller that omits
    # the flag entirely (the agent_chat_send dispatch lane, which forwards
    # None) falls through to agent_runtime.mission_chat.dispatch_session_policy.
    mission_chat_message.add_argument("--new-session", dest="new_session", action="store_true", help="Force a fresh canonical chat session for the target instead of continuing the default thread (agent_chat_send new_session lane); ignored when --session-id is given")
    # The echo half of the clarify binding. A reply that carries the token the
    # question shipped down lands in the question's OWN thread — outranking both
    # the policy default and a stale --session-id (reported, never silent, in the
    # turn's clarify_binding block). Unknown/pruned tokens degrade to normal
    # precedence rather than refusing.
    mission_chat_message.add_argument("--clarify-token", dest="clarify_token", default=None, help="Answer a clarify question by its token (clarify_request.clarify_token from the asking turn); binds this reply to the thread the question was asked in")
    # No --task/--goal: the goal/task mission lane is retired (contract 45) and
    # chat is the only lane. They were not inert residue -- the handler consumed
    # them by writing instance.current_task_id/goal_id and flipping instance.mode
    # to the RETIRED "task_bound", so an armed row re-armed retired runtime state
    # on the operator's next ordinary message. The Launcher stopped emitting them
    # first (launcher 87957547); this is the lockstep half. `persona instance
    # steer --goal` is untouched -- goal_id rides the contract-45 wire and the
    # Launcher groups its agent rooms by it.
    # Default None = "no title opinion": consumed as the fresh thread's title
    # when this send mints one, otherwise the durable "<persona> chat" title
    # stands. A literal default would name every freshly minted thread after it.
    mission_chat_message.add_argument("--title", default=None, help="Title for a chat session this send MINTS (a fresh --new-session thread, or a dispatch under the new_per_dispatch policy); ignored when continuing an existing thread")
    mission_chat_message.add_argument("--message", required=True)
    mission_chat_message.add_argument("--provider", default=None, help="Provider override for this persona chat session only")
    mission_chat_message.add_argument("--model", default=None, help="Model override for this persona chat session only")
    mission_chat_message.add_argument("--use-agent-default", action="store_true", help="Clear the chat-scoped provider/model override before sending")
    mission_chat_message.add_argument("--surface-prompt", default="")
    mission_chat_message.add_argument("--agents-file", default=None, help="Absolute path to one operator-selected workspace AGENTS.md to inject for this turn")
    mission_chat_message.add_argument("--workspace-id", default=None)
    mission_chat_message.add_argument("--workspace-name", default=None)
    mission_chat_message.add_argument("--intent-hint", default="chat")
    mission_chat_message.add_argument("--requested-by", default="cli")
    mission_chat_message.add_argument("--client-message-id", default=None)
    mission_chat_message.add_argument("--idempotency-key", default=None)
    mission_chat_message.add_argument("--stream", action="store_true", help="Emit operator-chat deltas and the final payload as NDJSON")
    # Default is resolved at RUN time (agent_runtime.mission_chat.default_max_seconds
    # in the ROOT config.yaml, itself defaulting to 240s) rather than pinned in the
    # parser: an argparse default cannot be told apart from an explicit flag, and
    # "explicit --max-seconds always wins over the configured default" has to be
    # decidable. `None` here IS "the caller expressed no opinion".
    mission_chat_message.add_argument("--max-seconds", type=float, default=None, help="Wall budget for this turn (default: agent_runtime.mission_chat.default_max_seconds, or 240s when unset). The agent is told how much remains, and the last max(60s, 15%%) is reserved for a final checkpoint reply; the turn then settles as budget_exhausted (terminal, no turn-resolve)")
    mission_chat_message.add_argument("--compression-threshold-tokens", type=int, default=None, help="One-turn native-compression proof seam; overrides the compressor token threshold without changing profile config")
    mission_chat_message.add_argument("--compression-protect-first-n", type=int, default=None, help="One-turn native-compression proof seam; override protected head messages")
    mission_chat_message.add_argument("--compression-protect-last-n", type=int, default=None, help="One-turn native-compression proof seam; override protected tail messages")
    mission_chat_message.add_argument("--relay-chain", default=None, help="Comma-separated canonical persona ids already on the agent-relay chain (envelope provenance for chained agent_chat_send hops)")
    mission_chat_message.add_argument("--relay-deadline-epoch", type=float, default=None, help="Absolute unix-epoch deadline shared by every hop on the relay chain")
    # Sender provenance, not guard logic: the sender's chat-root session id
    # scopes bare-persona target resolution to the SENDER's workspace. The
    # in-process relay always carried it on the args object; a DETACHED dispatch
    # runs its target turn in a CHILD PROCESS, so without an argv spelling the
    # child would silently resolve bare personas against the wrong scope.
    mission_chat_message.add_argument("--requested-by-session", dest="requested_by_session", default=None, help="Chat-root session id of the sender (envelope provenance; scopes bare-persona target resolution to the sender's workspace)")
    # The tri-state ``new_session`` has no argparse spelling: absent is False
    # ("continue the target's current default thread"), present is True, and
    # there is no way to say UNSET ("no opinion — let
    # agent_runtime.mission_chat.dispatch_session_policy decide"), which is
    # exactly what the dispatch lane forwards in-process. Changing
    # ``--new-session``'s default to None would express it, but would also
    # silently start minting a fresh thread for every bare CLI send that omits
    # the flag. This states the unset case explicitly instead, so the child
    # process reproduces the in-process lane's threading exactly.
    mission_chat_message.add_argument("--defer-thread-policy", dest="defer_thread_policy", action="store_true", help="State NO opinion about the thread: let agent_runtime.mission_chat.dispatch_session_policy decide (the tri-state 'unset' the in-process dispatch lane forwards). Overrides --new-session")
    mission_chat_message.add_argument("--json", action="store_true")
    mission_chat_message.set_defaults(func=_cmd_mission_chat_message)
    mission_chat_queue_skill = mission_chat_subs.add_parser("queue-skill", help="Load a skill on the next Mission Control chat turn")
    mission_chat_queue_skill.add_argument("--persona", dest="persona_id", required=True)
    mission_chat_queue_skill.add_argument("--persona-instance-id", default=None)
    mission_chat_queue_skill.add_argument("--session-id", required=True)
    mission_chat_queue_skill.add_argument("--skill", action="append", default=[])
    mission_chat_queue_skill.add_argument("--skills", nargs="+", default=[])
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
    mission_chat_resolve = mission_chat_subs.add_parser(
        "turn-resolve", help="Resolve one outcome_unknown chat turn"
    )
    mission_chat_resolve.add_argument("--session-id", required=True)
    mission_chat_resolve.add_argument("--client-message-id", required=True)
    mission_chat_resolve.add_argument("--turn-id", required=True)
    mission_chat_resolve.add_argument("--persona-instance-id", default=None)
    mission_chat_resolve.add_argument("--action", choices=["abandon"], required=True)
    mission_chat_resolve.add_argument("--json", action="store_true")
    mission_chat_resolve.set_defaults(func=_cmd_mission_chat_turn_resolve)
    # Read-only adoption readout for the clarify-token binding. Registered with
    # the NON-mutating stage42 args on purpose: it never mints, settles, or
    # sweeps, so it has no --dry-run to honor and nothing to confirm. Whether
    # agents are echoing the token is answered from state the binding already
    # records, with no new event kinds (telemetry is not the EventLog here).
    mission_chat_clarify_tickets = mission_chat_subs.add_parser(
        "clarify-tickets",
        help="Clarify-token adoption readout: live tickets with state/age/session binding, and the bound_via histogram",
    )
    _add_stage42_global_args(
        mission_chat_clarify_tickets, controls=frozenset({"limit", "sort"})
    )
    mission_chat_clarify_tickets.add_argument("--session-id", default=None, help="Only list tickets bound to this chat root (counts still cover the whole store)")
    mission_chat_clarify_tickets.add_argument("--state", default=None, choices=["open", "answered", "rebound"], help="Only list tickets in this lifecycle state (counts still cover the whole store)")
    mission_chat_clarify_tickets.set_defaults(func=_cmd_mission_chat_clarify_tickets)

    status = subs.add_parser("status", help="Show harness status")
    status.add_argument("--json", action="store_true")
    status.add_argument(
        "--prune-stale",
        action="store_true",
        help=(
            "Delete serve_instances entries whose PID is PROVABLY dead, and report "
            "exactly which (recycled-PID and unclassifiable entries are always kept)"
        ),
    )
    status.set_defaults(func=_cmd_status)

    providers = subs.add_parser(
        "providers",
        help="List credential pools with typed auth health (machine-readable via --json)",
    )
    providers.add_argument("--json", action="store_true")
    providers.set_defaults(func=_cmd_providers)

    usage = subs.add_parser(
        "usage",
        help="Per-provider account usage/limit windows (machine-readable via --json)",
    )
    usage.add_argument("--json", action="store_true")
    usage.add_argument(
        "--provider",
        default=None,
        help="Restrict to a single provider lane (still emits the full envelope)",
    )
    usage.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_USAGE_TIMEOUT,
        help="Overall wall-clock bound (seconds) for the concurrent lane fetches",
    )
    usage.set_defaults(func=_cmd_usage)

    doctor = subs.add_parser("doctor", help="Show Harness runtime diagnostics: orphan worktrees, snapshot ids, event-log health, model authority, persona/profile binding")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--fix", action="store_true", help="Capture-then-reap the orphan worktrees the scan reports")
    doctor.add_argument("--dry-run", action="store_true", help="Preview --fix repairs without mutating runtime state")
    doctor.add_argument("--yes", "-y", action="store_true", help="Confirm --fix repairs")
    doctor.add_argument("--worktree-min-age-seconds", type=int, default=DEFAULT_WORKTREE_MIN_AGE_SECONDS)
    # The six stale-threshold / --compact-events knobs were removed: they fed
    # task, run, worker and incident sweeps that died with the mission lane, so
    # the CLI accepted them and silently ignored them.
    doctor.set_defaults(func=_cmd_doctor)

    health = subs.add_parser("health", help="Check Harness runtime/provider dependencies are reachable and configured")
    health.add_argument("--json", action="store_true")
    health.set_defaults(func=_cmd_health)

    verify = subs.add_parser("verify", help="Run runtime smoke verification: read-only harness CLI commands plus the focused store/snapshot/status test modules")
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

    contracts = subs.add_parser("contracts", help="Inspect canonical Mission Control event contracts")
    contracts_subs = contracts.add_subparsers(dest="contracts_command")
    contracts_dump = contracts_subs.add_parser("dump", help="Dump redaction-safe contract registry")
    contracts_dump.add_argument("--json", action="store_true")
    contracts_dump.set_defaults(func=_cmd_contracts_dump)

    worktree = subs.add_parser("worktree", help="Manage harness-managed git worktrees")
    worktree_subs = worktree.add_subparsers(dest="worktree_command", required=True)
    worktree_reap = worktree_subs.add_parser(
        "reap",
        help="Capture-then-reap orphan harness worktrees with no live owner",
    )
    worktree_reap.add_argument("--min-age-seconds", type=int, default=3600)
    worktree_reap.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview typed reap/keep decisions without removing worktrees, capturing patches, or emitting events",
    )
    worktree_reap.add_argument(
        "--include-legacy-temp",
        action="store_true",
        help="Also inventory the canonical legacy system-temp hermes-agent-wt base",
    )
    worktree_reap.add_argument("--json", action="store_true")
    worktree_reap.set_defaults(func=_cmd_worktree_reap)

    persona_instance = subs.add_parser(
        "persona-instance", help="Manage durable persona-instance store rows"
    )
    persona_instance_subs = persona_instance.add_subparsers(
        dest="persona_instance_command", required=True
    )
    persona_instance_reconcile = persona_instance_subs.add_parser(
        "reconcile",
        help=(
            "Archive-and-fold legacy-id persona-instance rows onto their canonical "
            "channel (duplicate agent cards repair); records identity_map aliases; "
            "prunes orphan rows; repairs missing steering parents and chat-session "
            "bindings whose session SessionDB no longer has"
        ),
    )
    persona_instance_reconcile.add_argument(
        "--dry-run", action="store_true", help="Report actions without mutating the store"
    )
    persona_instance_reconcile.add_argument("--json", action="store_true")
    persona_instance_reconcile.set_defaults(func=_cmd_persona_instance_reconcile)

    persona_instance_detail = persona_instance_subs.add_parser(
        "detail",
        help=(
            "Serve the tool-detail payloads (tool_resolution / turn_tool_context / "
            "permission_state / blocked_tools) evicted from the steady-state frame "
            "behind the visibility_ref pointer"
        ),
    )
    persona_instance_detail.add_argument(
        "instance_id", help="Persona-instance id (or persona id) whose tool detail to fetch"
    )
    persona_instance_detail.add_argument("--json", action="store_true")
    persona_instance_detail.set_defaults(func=_cmd_persona_instance_detail)

    agent = subs.add_parser("agent", help="Inspect and rebind harness agent definitions")
    agent_subs = agent.add_subparsers(dest="agent_command", required=True)
    agent_list = agent_subs.add_parser("list", help="List persisted/configured agent definitions")
    agent_list.add_argument("--all-profiles", action="store_true")
    _add_stage42_global_args(agent_list, controls=frozenset({"sort"}))
    agent_list.set_defaults(func=_cmd_agent_list)

    # UC-H3: the ONE unified create door for scripts, cron and operators. It
    # calls `agent_create.perform_agent_create` — the exact function
    # `runtime.agent.create` answers with — so a roster row, a chat root and an
    # office placement land together or not at all. `persona instance create`
    # deliberately stays the roster-only / serve-absent recovery door: it mints
    # no placement, which is its feature, not its bug.
    agent_create = agent_subs.add_parser(
        "create",
        help="Place an agent: roster row, chat root and office placement in ONE atomic call",
    )
    agent_create.add_argument("--persona", dest="persona_id", required=True, help="Roster persona id (or profile:<token>); an unknown id is refused before any write")
    agent_create.add_argument("--workspace", dest="workspace_id", required=True, help="Mission Control workspace the placement lands in; must already exist")
    agent_create.add_argument("--pos", dest="pos", nargs=2, metavar=("X", "Y"), required=True, help="Canvas position for the placement")
    agent_create.add_argument("--display-name", default=None, help="Authoritative name; omitted falls back to the persona's configured display name")
    agent_create.add_argument("--placement-id", default=None, help="Scene itemId to predict the actor key from; omitted mints one server-side")
    agent_create.add_argument("--realm-id", dest="realm_id", default=None)
    agent_create.add_argument("--folder", default=None, help="Office folder for the placement (default: Agents)")
    # A re-run is a NEW gesture unless the caller says otherwise — the same rule
    # the launcher applies by stamping micros into every key. A script that
    # wants resume-on-retry passes its own stable key.
    agent_create.add_argument("--idempotency-key", dest="idempotency_key", default=None, help="Stable retry key; omitted mints a fresh cli-<uuid4> so a re-run is a new gesture")
    agent_create.add_argument("--correlation-id", dest="correlation_id", default=None)
    agent_create.add_argument("--json", action="store_true")
    agent_create.set_defaults(func=_cmd_agent_create)

    agent_set_profile = agent_subs.add_parser(
        "set-profile",
        help="Rebind an agent to a different Hermes profile (the ONE door; cascades every instance projection)",
    )
    agent_set_profile.add_argument("persona_id", help="Store-persisted agent id")
    agent_set_profile.add_argument("--profile", required=True, help="Target Hermes profile name; must exist and resolve ready")
    agent_set_profile.add_argument("--requested-by", default="operator")
    _add_stage42_global_args(agent_set_profile, controls=frozenset({"dry_run"}))
    agent_set_profile.set_defaults(func=_cmd_agent_set_profile)

    skills = subs.add_parser("install-harness-skills", help="Install versioned Harness skills into configured persona profiles")
    skills.add_argument("--active-profile-only", action="store_true", help="Install all Harness skills only into the active Hermes profile")
    skills.add_argument("--all-persona-profiles", action="store_true", help="Compatibility flag; persona profiles are now the default")
    skills.add_argument("--json", action="store_true")
    skills.set_defaults(func=_cmd_install_harness_skills)

    snap = subs.add_parser("snapshot", help="Write redaction-safe snapshot.json")
    snap.add_argument("--json", action="store_true")
    snap.set_defaults(func=_cmd_snapshot)
    stream = subs.add_parser("stream", help="Emit Mission Control hydrate/delta frames as NDJSON")
    stream.add_argument("--poll-interval", type=float, default=0.25)
    stream.add_argument("--heartbeat-interval", type=float, default=5.0)
    stream.add_argument(
        "--delta-debounce-ms",
        type=int,
        default=200,
        help="Settle window for coalescing an event burst into one delta frame (0 disables)",
    )
    stream.add_argument("--max-frames", type=int, default=None, help=argparse.SUPPRESS)
    stream.add_argument(
        "--resync",
        action="store_true",
        help="Force the first post-hydrate batch to a full core (S6: a reconnecting fold client re-baselining before it folds patches)",
    )
    stream.add_argument(
        "--fold-entities",
        default=None,
        metavar="persona_instance,incident",
        help=(
            "Comma-separated entity classes THIS client can fold in place. A batch naming any "
            "other entity is demoted to a full core rather than shipping a patch the client "
            "would have to re-hydrate from. Omit the flag for the historical set "
            "(persona_instance,incident) — exactly today's wire; pass an empty value to declare "
            "that you fold nothing."
        ),
    )
    stream.set_defaults(func=_cmd_stream)
    serve = subs.add_parser("serve", help="Persistent NDJSON bridge: dispatch harness argv requests in one warm process (Mission Control serve lane), on stdio and on the per-root localhost socket")
    serve.add_argument("--ndjson", action="store_true", help="NDJSON frame transport over stdio (the only v1 transport)")
    serve.add_argument("--pool-size", type=int, default=4, help=argparse.SUPPRESS)
    serve.add_argument(
        "--no-socket",
        action="store_true",
        help="Run stdio-only: do not race for the per-root socket ownership lock and do not listen (the ready frame reports socket.outcome=disabled)",
    )
    serve.set_defaults(func=_cmd_serve)
    # Sub-verbs under `serve`. The subparser is NOT required, so a bare
    # `harness serve --ndjson` keeps parsing exactly as it always has and still
    # dispatches to `_cmd_serve`.
    serve_subs = serve.add_subparsers(dest="serve_command")
    serve_connect = serve_subs.add_parser(
        "connect",
        help="Connect to this root's live serve socket, perform the hello handshake, and print the reply as JSON",
    )
    serve_connect.add_argument("--probe", action="store_true", help="Also ask the service for its version block (build, boot_id, connections)")
    serve_connect.add_argument("--drain", action="store_true", help="Ask the service to drain and read to its terminal frame (the durable-service restart verb, from the outside)")
    serve_connect.add_argument("--deadline-seconds", type=float, default=None, help="Drain deadline handed to the service (the service floors it at its own minimum, 30s by default, and caps it at 3600s)")
    serve_connect.add_argument("--client", default=None, help="Client name recorded on the connection and in the service's logs (default: harness-serve-connect)")
    serve_connect.add_argument("--timeout", type=float, default=10.0, help="Socket connect/read timeout in seconds")
    serve_connect.set_defaults(func=_cmd_serve_connect)
    rebuild_read_model = subs.add_parser("rebuild-read-model", help="Rebuild read_model.db from the current event-sourced store")
    rebuild_read_model.add_argument("--json", action="store_true")
    rebuild_read_model.set_defaults(func=_cmd_rebuild_read_model)
    read_projection = subs.add_parser("read", help="Read one projection from read_model.db")
    read_projection.add_argument("--projection", required=True)
    read_projection.add_argument("--since-offset", type=int, default=None)
    read_projection.add_argument("--json", action="store_true")
    read_projection.set_defaults(func=_cmd_read_projection)

    # `harness work` — the operator's view of background work in flight
    # (terminal processes, subagent delegations, in-flight chat turns, MCP
    # servers, cron jobs). `list`/`peek` are strictly read-only; `cancel` is a
    # stage42 mutation verb and carries the confirm + replay guards.
    work = subs.add_parser("work", help="Background work running right now (list / peek / cancel)")
    work_subs = work.add_subparsers(dest="work_command", required=True)
    work_list = work_subs.add_parser("list", help="Every piece of running background work, with per-source health")
    work_list.add_argument("--kind", default=None, help="Only rows of this kind (terminal, delegation, chat_turn, mcp_server, cron_job, dispatch)")
    # No `--cursor`/`--since`: this projection is a point-in-time census of what
    # is running NOW, built fresh on every call. There is no page to resume from
    # and no history to filter by, so advertising either would accept a flag and
    # silently return the whole unfiltered set.
    _add_stage42_global_args(
        work_list, controls=frozenset({"limit", "sort"})
    )
    work_list.set_defaults(func=_cmd_work_list)
    work_peek = work_subs.add_parser("peek", help="Bounded read-only look at one item's recent output/progress")
    work_peek.add_argument("work_id", help="Work id from `harness work list`, e.g. terminal:sess-1")
    # Peek answers about ONE row, so nothing to sort, page or bound.
    _add_stage42_global_args(work_peek)
    work_peek.set_defaults(func=_cmd_work_peek)
    work_cancel = work_subs.add_parser("cancel", help="Interrupt one piece of running work through its owning subsystem")
    work_cancel.add_argument("work_id", help="Work id from `harness work list`")
    work_cancel.add_argument("--reason", default="operator_cancel", help="Recorded interrupt reason")
    work_cancel.add_argument("--issued-at", dest="issued_at", default=None, help="ISO-8601 issue timestamp; a cancel issued before the work started is superseded instead of applied")
    # Same single-row reasoning, plus `--idempotency-key`: replay protection on
    # this verb is `--issued-at` (a cancel aimed at a previous incarnation is
    # superseded), and a second, unread key would imply a guarantee nothing here
    # provides.
    _add_stage42_global_args(
        work_cancel, controls=frozenset({"dry_run", "yes"})
    )
    work_cancel.set_defaults(func=_cmd_work_cancel)

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


def _machine_root_config_paths(explicit: list[str] | None) -> list[Path]:
    """Config files the migration targets: explicit args, else every profile."""

    if explicit:
        return [Path(item) for item in explicit]
    from hermes_constants import get_default_hermes_root

    root = get_default_hermes_root()
    paths: list[Path] = []
    default_config = root / "config.yaml"
    if default_config.is_file():
        paths.append(default_config)
    profiles_dir = root / "profiles"
    if profiles_dir.is_dir():
        for child in sorted(profiles_dir.iterdir()):
            candidate = child / "config.yaml"
            if candidate.is_file():
                paths.append(candidate)
    return paths


def _cmd_roots_list(args) -> int:
    from agent_runtime.machine_roots import load_machine_roots, machine_roots_registry_paths

    roots = load_machine_roots(refresh=True)
    payload = roots.row()
    payload["registry_paths"] = [str(path) for path in machine_roots_registry_paths()]
    _print_stage42(_object_envelope("machine_roots", payload), args=args, default_output="json")
    return 0


def _cmd_roots_set(args) -> int:
    from agent_runtime.machine_roots import load_machine_roots, write_machine_roots

    path = Path(str(args.path)).expanduser()
    if not path.is_absolute():
        return emit_harness_error(
            ValueError(f"'{args.path}' is not absolute"),
            args=args,
            code="invalid_payload",
            message="A machine root must be an ABSOLUTE local path — relative bindings are exactly the portability bug this replaces.",
        )
    if not path.exists() and not getattr(args, "allow_missing", False):
        return emit_harness_error(
            FileNotFoundError(str(path)),
            args=args,
            code="not_found",
            message=f"{path} does not exist on this machine. Re-run with --allow-missing to bind it anyway.",
        )
    roots = dict(load_machine_roots(refresh=True).roots)
    roots[str(args.name)] = str(path)
    result = write_machine_roots(roots, dry_run=bool(getattr(args, "dry_run", False)))
    _print_stage42(_object_envelope("machine_roots", result), args=args, default_output="json")
    return 0


def _cmd_roots_unset(args) -> int:
    from agent_runtime.machine_roots import load_machine_roots, write_machine_roots

    roots = dict(load_machine_roots(refresh=True).roots)
    if str(args.name) not in roots:
        return emit_harness_error(
            NotFound(str(args.name)), args=args, message=f"Machine root '{args.name}' is not bound."
        )
    roots.pop(str(args.name))
    result = write_machine_roots(roots, dry_run=bool(getattr(args, "dry_run", False)))
    _print_stage42(_object_envelope("machine_roots", result), args=args, default_output="json")
    return 0


def _cmd_roots_migrate(args) -> int:
    from agent_runtime.machine_roots import MachineRoots, load_machine_roots
    from agent_runtime.machine_roots_migration import (
        apply_config_migration,
        plan_config_migration,
        suggest_roots_from_configs,
        unmapped_absolute_paths,
    )

    if not _require_yes(args):
        return 8
    config_paths = _machine_root_config_paths(list(getattr(args, "configs", []) or []))
    if not config_paths:
        return emit_harness_error(
            NotFound("config.yaml"), args=args, message="No profile config.yaml files found to migrate."
        )

    explicit: dict[str, str] = {}
    for item in getattr(args, "root", []) or []:
        if "=" not in str(item):
            return emit_harness_error(
                ValueError(str(item)), args=args, code="invalid_payload", message=f"--root expects NAME=PATH, got '{item}'"
            )
        name, _sep, value = str(item).partition("=")
        explicit[name.strip()] = str(Path(value.strip()).expanduser())

    bindings = dict(load_machine_roots(refresh=True).roots)
    bindings.update(suggest_roots_from_configs(config_paths))
    bindings.update(explicit)
    roots = MachineRoots(roots=bindings)

    plan = plan_config_migration(
        config_paths,
        roots,
        add_platform_gates=not bool(getattr(args, "no_platform_gates", False)),
    )
    outcome = apply_config_migration(plan, dry_run=bool(getattr(args, "dry_run", False)))
    payload = plan.row()
    payload["applied"] = outcome
    payload["unmapped_absolute_paths"] = unmapped_absolute_paths(config_paths, roots)
    _print_stage42(_object_envelope("machine_roots_migration", payload), args=args, default_output="json")
    return 0 if plan.safe else 1


def _cmd_persona_instance_detail(args) -> int:
    """Serve the persona-instance tool detail evicted from the frame (residue-slim
    R2), rebuilt read-only from the stores so the launcher's visibility dialog
    fetches identical bytes on open. A miss is an honest ``not_found``, never a
    fabricated empty payload."""

    from agent_runtime.snapshot import persona_instance_detail_for_id

    entity_id = str(getattr(args, "instance_id", "") or "")
    detail = persona_instance_detail_for_id(entity_id)
    if detail is None:
        return emit_harness_error(
            ValueError(f"persona-instance '{entity_id}' did not resolve"),
            args=args,
            code="not_found",
        )
    print(emit_json(detail))
    return 0


def _cmd_skills_catalog(args) -> int:
    """S8: resolve one content-addressed skills catalog by hash (frame-evicted)."""

    from agent_runtime.prompt_observability import skills_catalog_by_hash

    content_hash = str(getattr(args, "content_hash", "") or "").strip()
    catalog = skills_catalog_by_hash(content_hash)
    payload = {
        "hash": content_hash,
        "found": catalog is not None,
        "skills": catalog or [],
    }
    if getattr(args, "json", False):
        print(emit_json(payload))
        return 0
    if catalog is None:
        print(f"skills catalog {content_hash}: (not resolvable from persisted contexts)")
        return 0
    print(f"skills catalog {content_hash}: {len(catalog)} skill(s)")
    for skill in catalog:
        if isinstance(skill, dict):
            print(f"  {skill.get('name')} — {skill.get('status', 'accessible')}")
    return 0


def _rel_to_shared_skills(path) -> str | None:
    """Render a shared-skills path relative to the shared root (never leak the
    absolute runtime root). Falls back to the basename for outside paths."""

    if path is None:
        return None
    from hermes_constants import get_shared_skills_dir

    path = Path(path)
    try:
        return path.relative_to(get_shared_skills_dir()).as_posix()
    except ValueError:
        return path.name


def _cmd_skills_publishable(args) -> int:
    """Read-only: every resolvable skill package, whether it can reach a realm,
    and — when it cannot be promoted into the shared root — the typed reason.

    Names ALL offenders in one listing with a per-row typed code; a surface that
    reported only the shared root is exactly how "resolvable but structurally
    unable to travel" stayed invisible."""

    from agent_runtime.skill_publishability import build_publishability_rows

    source_kind = str(getattr(args, "source_kind", "") or "").strip() or None
    unpublishable_only = bool(getattr(args, "unpublishable_only", False))

    rows = build_publishability_rows()
    if source_kind is not None:
        rows = [row for row in rows if row["source_kind"] == source_kind]
    if unpublishable_only:
        rows = [row for row in rows if not row["publishable"]]
    _print_stage42(_list_envelope("skill_publishability", rows), args=args, default_output="json")
    return 0


def _cmd_skills_inbox(args) -> int:
    """C4: read-only listing of quarantined per-realm inbox skill packages and
    how each would reconcile against the canonical shared root."""

    from agent_runtime.skill_promotion import list_inbox_packages

    realm_token = str(getattr(args, "realm", "") or "").strip() or None
    rows = list_inbox_packages(realm_token)
    items = [
        {
            "skill": row["skill"],
            "realm": row["realm"],
            "action": row["action"],
            "source_hash": row["source_hash"],
            "canonical_hash": row["canonical_hash"],
            "promotion_block_reason": row["promotion_block_reason"],
            "promotion_block_detail": row["promotion_block_detail"],
        }
        for row in rows
    ]
    _print_stage42(_list_envelope("inbox_package", items), args=args, default_output="json")
    return 0


def _resolve_promotion_source(args, skill: str):
    """Resolve exactly one promotion source.

    Returns ``(source_dir, source_meta, move_source)`` on success, or an
    ``(error_code, message, safe_details)`` triple wrapped in a
    :class:`_PromotionSourceError` on failure.
    """

    from agent_runtime.skill_promotion import list_inbox_packages, realm_inbox_dir

    from_realm = str(getattr(args, "from_realm", "") or "").strip() or None
    from_profile = str(getattr(args, "from_profile", "") or "").strip() or None
    from_path = str(getattr(args, "from_path", "") or "").strip() or None
    provided = [flag for flag, value in (("--from-realm", from_realm), ("--from-profile", from_profile), ("--from-path", from_path)) if value]
    slug_parts = skill.split("/")

    if len(provided) > 1:
        raise _PromotionSourceError("invalid_request", "Provide exactly one of --from-realm / --from-profile / --from-path.", {"skill": skill, "provided": provided})

    if not provided:
        # Implied source: exactly one realm inbox must hold the skill.
        matches = [row for row in list_inbox_packages() if row["skill"] == skill]
        if not matches:
            raise _PromotionSourceError("not_found", "Skill not held in any realm inbox — specify --from-realm / --from-profile / --from-path.", {"skill": skill})
        if len(matches) > 1:
            raise _PromotionSourceError("invalid_request", "Skill present in multiple realm inboxes — specify --from-realm <id>.", {"skill": skill, "candidates": sorted(row["realm"] for row in matches)})
        row = matches[0]
        return Path(row["source_dir"]), {"kind": "realm", "realm_id": row["realm"]}, False

    if from_realm:
        source_dir = realm_inbox_dir(from_realm).joinpath(*slug_parts)
        if not (source_dir / "SKILL.md").is_file():
            raise _PromotionSourceError("not_found", "Skill not present in that realm's inbox.", {"skill": skill, "realm": from_realm})
        # An inbox is a byte-faithful realm mirror — promotion never moves it.
        return source_dir, {"kind": "realm", "realm_id": from_realm}, False

    if from_profile:
        from hermes_cli.profiles import get_profile_dir, normalize_profile_name

        skills_root = get_profile_dir(normalize_profile_name(from_profile)) / "skills"
        candidate = skills_root.joinpath(*slug_parts)
        if (candidate / "SKILL.md").is_file():
            source_dir = candidate
        elif "/" not in skill:
            nested = [
                child / skill
                for child in sorted(skills_root.iterdir(), key=lambda p: p.name)
                if child.is_dir() and not child.name.startswith(".") and (child / skill / "SKILL.md").is_file()
            ] if skills_root.is_dir() else []
            if not nested:
                raise _PromotionSourceError("not_found", "Skill not found in that profile's skills directory.", {"skill": skill, "profile": from_profile})
            if len(nested) > 1:
                raise _PromotionSourceError("invalid_request", "Skill found under multiple categories in that profile — use <category>/<name>.", {"skill": skill, "profile": from_profile, "candidates": sorted(p.parent.name for p in nested)})
            source_dir = nested[0]
        else:
            raise _PromotionSourceError("not_found", "Skill not found in that profile's skills directory.", {"skill": skill, "profile": from_profile})
        # Promoting from a profile retires the duplicate (the collision guard).
        return source_dir, {"kind": "profile", "profile": from_profile}, True

    # from_path
    source_dir = Path(from_path).expanduser()
    if not (source_dir / "SKILL.md").is_file():
        raise _PromotionSourceError("not_found", "No SKILL.md at that path.", {"skill": skill})
    return source_dir, {"kind": "path", "path": str(source_dir)}, bool(getattr(args, "move_source", False))


class _PromotionSourceError(Exception):
    def __init__(self, code: str, message: str, safe_details: dict):
        super().__init__(message)
        self.code = code
        self.message = message
        self.safe_details = safe_details


def _cmd_skills_promote(args) -> int:
    """C4: hash-guarded promotion of a held / authored / profile-local package
    into the canonical shared root. Honors --dry-run (stage42 gate); never
    deletes (displaced content is archived)."""

    from agent_runtime.skill_promotion import classify_promotion, execute_promotion

    skill = str(getattr(args, "skill", "") or "").strip()
    dry_run = bool(getattr(args, "dry_run", False))
    adopt_divergent = bool(getattr(args, "adopt_divergent", False))

    try:
        source_dir, source_meta, move_source = _resolve_promotion_source(args, skill)
    except _PromotionSourceError as exc:
        _print_stage42(_error_envelope(exc.code, exc.message, safe_details=exc.safe_details), args=args, default_output="json")
        return ERROR_EXIT_CODES.get(exc.code, 1)

    plan = classify_promotion(skill, source_dir)

    # Installer-ownership policy, consulted BEFORE the divergence branch below.
    # The door enforces this too (``execute_promotion`` refuses without writing),
    # but reporting it here keeps the operator from being told "re-run with
    # --adopt-divergent" for a promotion that adopting could never make legal.
    # Same seam, one authority — the CLI adds only the exit code and hint.
    if plan.action in ("promote_new", "hold_divergent"):
        from agent_runtime.skill_publishability import promotion_refusal

        refusal = promotion_refusal(skill, source_dir)
        if refusal is not None:
            _print_stage42(
                _error_envelope(
                    "invalid_request",
                    refusal.message,
                    safe_details={
                        "skill": skill,
                        "reason_code": refusal.code,
                        "manifest_name": refusal.manifest_name,
                        "profile": refusal.profile,
                        "source_hash": plan.source_hash,
                        "canonical_hash": plan.canonical_hash,
                    },
                    hint=(
                        "The hermes installer owns this skill package. Promote a "
                        "package of your own under a distinct slug instead — see "
                        "`hermes harness skills publishable --json`."
                    ),
                ),
                args=args,
                default_output="json",
            )
            return ERROR_EXIT_CODES.get("invalid_request", 1)

    # A real (non-dry-run) divergent promotion without --adopt-divergent is a
    # hold: surface BOTH hashes in a typed payload and exit non-zero.
    if not dry_run and plan.action == "hold_divergent" and not adopt_divergent:
        _print_stage42(
            _error_envelope(
                "sync_conflict",
                "Canonical differs from source — re-run with --adopt-divergent to adopt (archives the previous copy).",
                safe_details={"skill": skill, "source_hash": plan.source_hash, "canonical_hash": plan.canonical_hash},
                hint="Re-run with --adopt-divergent to adopt the source over the divergent canonical (the previous copy is archived, never deleted).",
            ),
            args=args,
            default_output="json",
        )
        return ERROR_EXIT_CODES.get("sync_conflict", 1)

    result = execute_promotion(
        plan,
        source=source_meta,
        adopt_divergent=adopt_divergent,
        dry_run=dry_run,
        move_source=move_source,
    )

    envelope = _object_envelope(
        "skill_promotion",
        {
            "skill": skill,
            "source": source_meta,
            "classification": plan.action,
            "action": result.action,
            "source_hash": plan.source_hash,
            "canonical_hash": plan.canonical_hash,
            "archived_previous_to": _rel_to_shared_skills(result.archived_previous_to),
            "provenance_path": _rel_to_shared_skills(result.provenance_path),
            "reason": result.reason,
            # Machine-readable companion to ``reason`` when the guarded door
            # refuses on installer-ownership policy, so a UI branches on a code
            # instead of pattern-matching prose (None otherwise).
            "reason_code": result.reason_code,
            "dry_run": dry_run,
        },
    )
    _print_stage42(envelope, args=args, default_output="json")
    return 2 if result.action == "refused" else 0


def _cmd_prompt_context_show(args) -> int:
    """S8: show one persisted prompt-observability context by id (frame-evicted
    historical rows stay on disk and are fetched here; C2 retention MOVES older
    rows to the archive dir, which this verb resolves too — archive-never-delete
    means the fetch lane keeps working). Honest miss on absence."""

    from agent_runtime.prompt_observability import load_persisted_context_row

    token = str(getattr(args, "context_id", "") or "").strip()
    if not token:
        return emit_harness_error(ValueError("--context-id is required"), args=args, code="invalid_request")
    data = load_persisted_context_row(token)
    if data is None:
        return emit_harness_error(
            ValueError(f"prompt context '{token}' not found on disk"),
            args=args,
            code="not_found",
        )
    if getattr(args, "json", False):
        print(emit_json(data))
        return 0
    print(f"prompt context {token}: persona={data.get('persona_id')} session={data.get('session_id')}")
    return 0


def _workspace_row(workspace, *, full: bool = False) -> dict:
    """`workspace list|show|…` row — a RE-KEY of the snapshot's own workspace
    builder, never a second projection (S48, ledger item 4).

    The hand-rolled twin this replaces is what shipped the ``tasks`` NameError
    (`a21ab1a2a`): a field the snapshot row had already dropped survived here
    because nothing tied the two together. Every value below now comes from
    ``_workspace_summary``; the CLI owns only the key SUBSET (skinny vs
    ``--full``).

    Deliberate deviations, each with a reason:

    * ``created_at`` is CLI-only — the wire never carried it, and an operator
      reading ``workspace show`` does. It is a plain timestamp off the model,
      not a re-derivation of anything the builder computes.
    * timestamps stay as ``datetime`` rather than the builder's value, because
      the Stage-42 printer (``emit_json`` -> ``to_jsonable``) is the
      serialization authority for this lane; pre-serializing here would change
      the ``--output table`` rendering for no reason. ``_workspace_summary``
      passes ``updated_at`` through unconverted anyway, so this is a no-op for
      workspaces and kept only for symmetry with the board/office rows.

    The builder import is FUNCTION-LOCAL on purpose (all six rows do this): a
    module-level ``from … import _workspace_summary`` binds whichever
    definition existed at CLI import time, which is itself a second reference
    to the authority. Resolving through the module on every call means there is
    exactly one live definition and it is the snapshot module's.
    """

    from agent_runtime.snapshot import _workspace_summary

    summary = _workspace_summary(workspace, persona_instances=PersonaInstanceStore().list_all())
    row = {
        key: summary[key]
        for key in (
            "id",
            "name",
            "realm_id",
            "agents",
            "agent_ids",
            "live_scoped_agent_count",
            "live_scoped_agent_ids",
            "roster_agent_count",
            "roster_agent_ids",
            "isolation",
        )
    }
    row["updated_at"] = workspace.updated_at
    if full:
        row.update({key: summary[key] for key in ("kind", "slug", "default_blueprint_id", "max_concurrent_lanes", "archived")})
        row["created_at"] = workspace.created_at
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
    from agent_runtime.workspace_template import (
        CONTENT_COPY_SCOPES,
        copy_workspace_content,
        normalize_copy_scopes,
    )

    template = None
    scopes: tuple[str, ...] = ()
    if getattr(args, "from_workspace", None):
        try:
            template = WorkspaceStore().get(args.from_workspace)
        except NotFound as exc:
            return emit_harness_error(exc, args=args, code="template_workspace_not_found")
        scopes = normalize_copy_scopes(getattr(args, "copy", None))
    elif getattr(args, "copy", None):
        return emit_harness_error(
            ValueError("--copy requires --from-workspace"), args=args, code="invalid_request"
        )
    if getattr(args, "dry_run", False):
        row = {"id": f"ws_dry_{uuid.uuid4().hex[:6]}", "name": args.name, "realm_id": args.realm, "agents": len(args.agent or []), "goals": 0, "isolation": args.isolation or "soft", "updated_at": now()}
        if template is not None:
            row["template_workspace_id"] = template.id
            row["copy_scopes"] = list(scopes)
        _print_stage42(_object_envelope("workspace", row), args=args, default_output="json")
        return 0
    if args.realm:
        RealmStore().get(args.realm)
    # Template settings/roster feed the create itself; explicit flags always
    # win over the template so the operator can override any copied field.
    agent_ids = list(args.agent or [])
    blueprint = args.blueprint
    isolation = args.isolation
    max_lanes = args.max_lanes
    if template is not None:
        if "agents" in scopes and not agent_ids:
            agent_ids = list(template.agent_ids or [])
        if "settings" in scopes:
            if blueprint is None:
                blueprint = template.default_blueprint_id
            if isolation is None:
                isolation = template.isolation
            if max_lanes is None:
                max_lanes = template.max_concurrent_lanes
    item = WorkspaceStore().create(
        name=args.name,
        agent_ids=agent_ids,
        default_blueprint_id=blueprint,
        isolation=isolation or "soft",
        max_concurrent_lanes=max_lanes,
        realm_id=args.realm,
    )
    if args.realm:
        realm = RealmStore().get(args.realm)
        if item.id not in realm.workspace_ids:
            realm.workspace_ids.append(item.id)
            RealmStore().save(realm)
    # Office/board content copies AFTER the workspace exists, through the
    # store chokepoints, so every copied artifact rides its contract event.
    warnings: list[dict] = []
    copied = None
    if template is not None:
        content_scopes = tuple(scope for scope in scopes if scope in CONTENT_COPY_SCOPES)
        if content_scopes:
            outcome = copy_workspace_content(template.id, item.id, scopes=content_scopes)
            copied = outcome["copied"]
            warnings.extend(outcome["warnings"])
    # A workspace created inside the ACTIVE realm becomes active
    # immediately — the operator expects to land in the workspace they
    # just created, not to run a second `workspace use` by hand.
    # (workspace.created / workspace.activated are emitted by the store
    # chokepoint — Stage 12.)
    if item.realm_id and item.realm_id == RealmStore().active_id():
        WorkspaceStore().set_active(item.id)
    row = _workspace_row(item)
    if template is not None:
        row["template_workspace_id"] = template.id
        row["copy_scopes"] = list(scopes)
        if copied is not None:
            row["copied"] = copied
    _print_stage42(_object_envelope("workspace", row, warnings=warnings), args=args, default_output="json")
    return 0


def _cmd_workspace_delete(args) -> int:
    if not _require_yes(args):
        return 8
    store = WorkspaceStore()
    try:
        item = store.get(args.workspace_id)
    except NotFound as exc:
        return emit_harness_error(exc, args=args)
    if getattr(args, "dry_run", False):
        row = _workspace_row(item, full=True)
        row["deleted"] = False
        row["dry_run"] = True
        _print_stage42(_object_envelope("workspace", row), args=args, default_output="json")
        return 0
    was_active = store.active_id() == item.id
    realm_id = item.realm_id
    try:
        row = store.delete(args.workspace_id)
    except WorkspaceDeleteBlocked as exc:
        return emit_harness_error(exc, args=args, code=exc.code)
    # Deleting the ACTIVE workspace falls back to the realm's default (same
    # reconcile rule as a realm switch) instead of leaving the operator on a
    # cleared pointer.
    if was_active and realm_id:
        try:
            _reconcile_active_workspace_to_realm(RealmStore().get(realm_id))
        except Exception:  # noqa: BLE001 — pointer reconcile is best-effort; the delete already landed
            pass
    _print_stage42(_object_envelope("workspace", row), args=args, default_output="json")
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


def _cmd_workspace_actors(args) -> int:
    workspace = WorkspaceStore().get(args.workspace_id)
    actors = [
        persona_instance_summary(instance)
        for instance in PersonaInstanceStore().list_all()
        if getattr(instance, "workspace_id", None) == workspace.id
    ]
    if getattr(args, "kind", None):
        actors = [actor for actor in actors if actor.get("kind") == args.kind]
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
        roster_agent_ids = list(dict.fromkeys([*item.agent_ids, args.persona_id]))
        row["roster_agent_ids"] = roster_agent_ids
        row["roster_agent_count"] = len(roster_agent_ids)
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
    """`realm list|show|…` row — a RE-KEY of the snapshot's own realm builder
    (S48, ledger item 4).

    The hand-rolled twin this replaces is where ``"sync": "in_sync"`` was
    hardcoded (`a21ab1a2a`) — the exact fake ``_realm_summary`` forbids. The
    honest sidecar read now happens in ONE place, so the CLI cannot drift from
    the wire again. CLI-only additions (never on the wire, both plain model
    scalars): ``sync_manifest_ref`` and ``created_at``.
    """

    from agent_runtime.snapshot import _realm_summary

    summary = _realm_summary(realm, workspaces=WorkspaceStore().list_all(include_archived=True))
    row = {
        key: summary[key]
        for key in ("id", "name", "server_id", "default_workspace_id", "default_workspace_version", "workspaces", "sync")
    }
    row["updated_at"] = realm.updated_at
    if full:
        row.update({key: summary[key] for key in ("kind", "slug", "workspace_ids", "default_workspace_name", "archived")})
        row["sync_manifest_ref"] = realm.sync_manifest_ref
        row["created_at"] = realm.created_at
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
        row = {"id": f"realm_dry_{uuid.uuid4().hex[:6]}", "name": args.name, "server_id": args.server, "workspaces": 0, "sync": None, "updated_at": now()}
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


def _cmd_realm_default_scope(args) -> int:
    if getattr(args, "dry_run", False):
        _print_stage42(
            preview_default_scope_migration(),
            args=args,
            default_output="json",
        )
        return 0
    if not getattr(args, "yes", False):
        return emit_harness_error(
            ValueError("default-scope reconciliation requires --dry-run or --yes"),
            args=args,
            code="confirmation_required",
        )
    winner_realm_id = str(getattr(args, "winner_realm", "") or "").strip()
    winner_workspace_id = str(
        getattr(args, "winner_workspace", "") or ""
    ).strip()
    if not winner_realm_id or not winner_workspace_id:
        return emit_harness_error(
            ValueError("--winner-realm and --winner-workspace are required with --yes"),
            args=args,
            code="invalid_request",
        )
    _print_stage42(
        reconcile_default_scope_to_legacy(
            winner_realm_id=winner_realm_id,
            winner_workspace_id=winner_workspace_id,
        ),
        args=args,
        default_output="json",
    )
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


def _realm_sync_subtree(realm_id: str):
    """The checked-out realm subtree the profile-file lane reconciles against.

    Read-only: never clones, never fetches, never mutates the repo — the resolve
    verb operates on what the last pull already put on disk.
    """
    from agent_runtime.realm_sync import _realm_subtree, _sync_repo_path

    realm = RealmStore().get(realm_id)
    return _realm_subtree(_sync_repo_path(realm), realm.id)


def _cmd_realm_sync_held(args) -> int:
    from agent_runtime.profile_artifact_sync import apply_profile_artifact_pull

    summary = apply_profile_artifact_pull(args.realm_id, _realm_sync_subtree(args.realm_id), dry_run=True)
    rows = [
        {"id": key, "kind": "profile_artifact_hold", "realm_id": args.realm_id, "take_hint": "--take local|remote"}
        for key in sorted(set(summary.held))
    ]
    _print_stage42(_list_envelope("profile_artifact_hold", rows), args=args, default_output="json")
    return 0


def _cmd_realm_sync_resolve(args) -> int:
    from agent_runtime.profile_artifact_sync import (
        ProfileArtifactResolveError,
        resolve_profile_artifact,
    )

    if not _require_yes(args):
        return 8
    dry_run = bool(getattr(args, "dry_run", False))
    try:
        row = resolve_profile_artifact(
            args.realm_id,
            _realm_sync_subtree(args.realm_id),
            args.key,
            take=args.take,
            dry_run=dry_run,
        )
    except ProfileArtifactResolveError as exc:
        return emit_harness_error(exc, args=args, code=exc.code)
    envelope = _object_envelope("profile_artifact_hold", {"id": row["key"], **row})
    if dry_run:
        envelope["dry_run"] = True
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def _realm_skill_selection_envelope(realm) -> dict:
    """The realm_skill_selection/v1 envelope (design §5): current mode +
    selection, the shared-catalog slugs on THIS machine, and the honest
    ``missing`` accounting (selection − catalog)."""
    from agent_runtime.skills_inventory import build_shared_catalog

    _root, _exists, catalog = build_shared_catalog()
    catalog_slugs = sorted({entry["slug"] for entry in catalog})
    selection = sorted(realm.skill_selection or [])
    missing = sorted(set(selection) - set(catalog_slugs))
    return {
        "schema_version": 1,
        "id": realm.id,
        "kind": "realm_skill_selection",
        "mode": realm.skill_publish_mode,
        "selection": selection,
        "catalog": catalog_slugs,
        "missing": missing,
    }


def _cmd_realm_skills_show(args) -> int:
    realm = RealmStore().get(args.realm_id)
    _print_stage42(_realm_skill_selection_envelope(realm), args=args, default_output="json")
    return 0


def _cmd_realm_skills_set(args) -> int:
    chosen = [
        name
        for name, present in (
            ("--all", bool(getattr(args, "publish_all", False))),
            ("--skills", getattr(args, "skills", None) is not None),
            ("--none", bool(getattr(args, "publish_none", False))),
        )
        if present
    ]
    if len(chosen) != 1:
        return emit_harness_error(
            ValueError("exactly one of --all, --skills, or --none is required"),
            args=args,
            code="invalid_request",
        )
    if getattr(args, "publish_all", False):
        mode, selection = "all", []
    elif getattr(args, "publish_none", False):
        mode, selection = "selected", []
    else:
        mode = "selected"
        selection = [slug.strip() for slug in str(args.skills).split(",") if slug.strip()]
    dry_run = bool(getattr(args, "dry_run", False))
    realm = RealmStore().set_skill_selection(
        args.realm_id, mode=mode, selection=selection, dry_run=dry_run
    )
    envelope = _realm_skill_selection_envelope(realm)
    if dry_run:
        envelope["dry_run"] = True
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def _realm_agent_selection_envelope(realm_id: str) -> dict:
    state = realm_agent_selection_state(realm_id)
    return {
        "schema_version": 1,
        "id": realm_id,
        "kind": "realm_agent_selection",
        **state,
    }


def _cmd_realm_agents_show(args) -> int:
    # RealmStore lookup occurs inside the state resolver, so a missing id keeps
    # the same typed command error behavior as every other Realm read verb.
    _print_stage42(
        _realm_agent_selection_envelope(args.realm_id),
        args=args,
        default_output="json",
    )
    return 0


def _cmd_realm_agents_set(args) -> int:
    chosen = [
        name
        for name, present in (
            ("--workspace", bool(getattr(args, "publish_workspace", False))),
            ("--agents", getattr(args, "agents", None) is not None),
            ("--none", bool(getattr(args, "publish_none", False))),
        )
        if present
    ]
    if len(chosen) != 1:
        return emit_harness_error(
            ValueError("exactly one of --workspace, --agents, or --none is required"),
            args=args,
            code="invalid_request",
        )
    if getattr(args, "publish_workspace", False):
        mode, selection = "workspace", []
    elif getattr(args, "publish_none", False):
        mode, selection = "selected", []
    else:
        mode = "selected"
        selection = [
            persona_id.strip()
            for persona_id in str(args.agents).split(",")
            if persona_id.strip()
        ]
    dry_run = bool(getattr(args, "dry_run", False))
    RealmStore().set_agent_selection(
        args.realm_id,
        mode=mode,
        selection=selection,
        dry_run=dry_run,
    )
    envelope = _realm_agent_selection_envelope(args.realm_id)
    if dry_run:
        # The state resolver reads disk, so reflect the validated would-be
        # selection in the preview without mutating RealmStore.
        preview = RealmStore().set_agent_selection(
            args.realm_id,
            mode=mode,
            selection=selection,
            dry_run=True,
        )
        envelope["mode"] = preview.agent_publish_mode
        envelope["selection"] = sorted(preview.agent_selection or [])
        effective = set(envelope["required"])
        if preview.agent_publish_mode == "selected":
            effective.update(envelope["selection"])
        catalog = set(envelope["catalog"])
        envelope["published"] = sorted(effective & catalog)
        envelope["missing"] = sorted(effective - catalog)
        envelope["dry_run"] = True
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def _cmd_agent_list(args) -> int:
    from agent_runtime.persona_profile_binding import binding_index

    rows: list[dict] = []
    if getattr(args, "all_profiles", False):
        for profile in list_profiles():
            try:
                cfg = load_agent_runtime_config(Path(profile.path) / "config.yaml")
                personas = ensure_persisted_personas(cfg)
                # Same asymmetry `ensure_persisted_personas(cfg)` already has:
                # the config side comes from the ENUMERATED profile's
                # config.yaml, the store side from the ACTIVE runtime root
                # (there is one agent store per runtime root, not per profile).
                # The binding columns therefore describe exactly the merge the
                # row above came from.
                bindings = binding_index(cfg)
            except Exception:
                continue
            for persona in personas:
                rows.append(_agent_definition_row(persona, source_profile=profile.name, bindings=bindings))
    else:
        cfg = load_agent_runtime_config()
        try:
            bindings = binding_index(cfg)
        except Exception:
            bindings = {}
        for persona in ensure_persisted_personas(cfg):
            rows.append(_agent_definition_row(persona, source_profile=active_profile_name(), bindings=bindings))
    deduped: dict[tuple[str, str | None], dict] = {}
    for row in rows:
        # Dedup on the ENUMERATED Hermes profile the definition was read from,
        # not on `profile` — `profile` is now the agent's own binding, and two
        # agents in different profile homes can legitimately share one binding.
        deduped[(row["id"], row.get("source_profile"))] = row
    # Roster rows are read per enumerated profile, but WHICH agent store they
    # merged against is a property of the resolved runtime root — the envelope
    # says so (`source_profile` alone cannot; see the 2026-08-12 incident).
    envelope = attach_root_observability(
        _list_envelope("agent", _sort_rows(list(deduped.values()), getattr(args, "sort", None)))
    )
    _print_stage42(envelope, args=args)
    return 0


def _agent_definition_row(persona: AgentPersona, *, source_profile: str | None, bindings: dict | None = None) -> dict:
    """One `agent list` row.

    ``profile`` is the agent's OWN ``hermes_profile`` binding — the thing the
    column name promises. It used to be filled with ``active_profile_name()``,
    so every row printed the operator's current profile (live evidence
    2026-07-25: all five agents printed ``alice`` both before AND after a real
    rebind — a first-class surface that structurally could not answer the
    question it appeared to answer). The enumeration source keeps its own,
    honestly-named ``source_profile`` column, and the config-vs-store
    disagreement that ``ensure_persisted_personas`` silently resolves
    store-wins is surfaced rather than hidden.
    """

    binding = (bindings or {}).get(persona.id)
    row = {
        "id": persona.id,
        "name": persona.display_name,
        "role": str(persona.role),
        "profile": persona.hermes_profile,
        "source_profile": source_profile,
        "state": "available",
        "updated_at": None,
    }
    if binding is not None:
        row["config_profile"] = binding.config_profile
        row["store_profile"] = binding.store_profile
        row["binding_source"] = binding.source
        row["binding_diverged"] = binding.diverged
    return row


def _cmd_agent_set_profile(args) -> int:
    """`harness agent set-profile` — the ONE persona⇄profile rebind door.

    The parser opts this verb into the ``dry_run`` control explicitly; it is
    READ here and threaded into the store chokepoint, which validates
    fully, writes nothing and emits nothing on a preview. A mutation verb that
    ignores the flag silently mutates on a preview — this repo has shipped that
    bug twice (the 2026-07-17 office verb family).
    """

    from agent_runtime.persona_profile_binding import PersonaProfileRebindError, rebind_persona_profile

    dry_run = bool(getattr(args, "dry_run", False))
    try:
        result = rebind_persona_profile(
            str(getattr(args, "persona_id", "") or ""),
            profile=str(getattr(args, "profile", "") or ""),
            dry_run=dry_run,
            actor=str(getattr(args, "requested_by", None) or "operator"),
        )
    except PersonaProfileRebindError as exc:
        data = {
            "ok": False,
            "error_code": exc.code,
            "error": str(exc),
            **exc.details,
            "dry_run": dry_run,
            "next_expected": "fix the arguments and retry; no agent binding was changed",
        }
        _print_stage42(_object_envelope("agent_profile_rebind", data), args=args, default_output="json")
        return 2
    _print_stage42(_object_envelope("agent_profile_rebind", result), args=args, default_output="json")
    # A partial apply moved the persona authority but stranded projection rows.
    # Placement rows have no self-heal, so exiting 0 would tell a script the
    # binding is fully consistent when it is not.
    return 0 if result.get("ok", True) else 2


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


def _cmd_init(args) -> int:
    cfg = load_agent_runtime_config()
    personas = ensure_persisted_personas(cfg)
    persona_ids = [p.id for p in personas]
    try:
        scope = ensure_default_scope(agent_ids=persona_ids)
    except DefaultScopeReconciliationRequired as exc:
        return emit_harness_error(exc, args=args, code=exc.code)
    data = {
        "personas": persona_ids,
        "default_realm_id": scope.realm.id,
        "default_workspace_id": scope.workspace.id,
    }
    if args.json:
        print(emit_json(data))
    else:
        # Personas are data now: a fresh root provisions none, so the old
        # unconditional line rendered a dangling "personas: " with nothing after it.
        print(
            f"Initialized harness personas: {', '.join(data['personas'])}"
            if data["personas"]
            else "Initialized harness: no personas provisioned (personas are data — add them with `harness persona`)"
        )
        print(f"Default scope: {scope.realm.name} / {scope.workspace.name}")
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


def _credential_health(entry) -> dict:
    """Typed health for one pooled credential, derived from the SAME upstream
    classification the human `hermes auth list` uses — reused, never
    re-implemented, so there is exactly one authority on what "401 vs 429 vs
    dead" means. A healthy credential carries no annotation; an exhausted one
    is split into auth_failed / rate_limited / exhausted with the retry window;
    a dead credential (which the human list renders with NO marker — a latent
    "looks healthy" bug) is surfaced explicitly.
    """
    from agent.credential_pool import STATUS_DEAD, STATUS_EXHAUSTED, _exhausted_until
    from hermes_cli.auth_commands import (
        _classify_exhausted_status,
        _format_exhausted_status,
    )

    last_status = getattr(entry, "last_status", None)
    if last_status not in {STATUS_EXHAUSTED, STATUS_DEAD}:
        return {"state": "healthy"}

    message = _format_exhausted_status(entry).strip()
    if last_status == STATUS_DEAD:
        return {
            "state": "dead",
            "code": getattr(entry, "last_error_code", None),
            "reason": getattr(entry, "last_error_reason", None),
            "retry_at": None,
            "message": message or "credential dead (re-auth required)",
        }

    label, retryable = _classify_exhausted_status(entry)
    state = {"auth failed": "auth_failed", "rate-limited": "rate_limited"}.get(
        label, "exhausted"
    )
    return {
        "state": state,
        "code": getattr(entry, "last_error_code", None),
        "reason": getattr(entry, "last_error_reason", None),
        "retry_at": _exhausted_until(entry) if retryable else None,
        "message": message,
    }


#: How many trailing characters of a credential a preview may show. Matches the
#: dashboard's existing rule (`web_server.py::_truncate_token`), deliberately
#: SHORTER than its 6 because this payload is consumed by a GUI that only needs
#: to tell two keys apart, not to identify one out of context.
_TOKEN_PREVIEW_CHARS = 4


def _credential_token_preview(entry) -> Optional[str]:
    """``…abcd`` — the last few characters of a pooled credential, or None.

    This is the ONLY function in the provider-visibility payload that reads a
    credential value, and it is written so that no input can make it emit more
    than [_TOKEN_PREVIEW_CHARS] characters:

    * a value shorter than 2× the preview length yields None rather than a
      short secret rendered nearly whole — a 6-character key must not become
      its own preview;
    * a non-string (the Entra-ID bearer *callable*, for instance) yields None
      and is NEVER invoked;
    * every failure path yields None.

    A preview is not a fallback for an absent value: absence is None, and the
    client renders "no preview" rather than an empty-looking secret.
    """
    try:
        raw = getattr(entry, "access_token", None)
        if not isinstance(raw, str):
            return None
        value = raw.strip()
        if len(value) < _TOKEN_PREVIEW_CHARS * 2:
            return None
        return f"…{value[-_TOKEN_PREVIEW_CHARS:]}"
    except Exception:
        return None


def _provider_visibility_catalog() -> list[dict]:
    """The `catalog` block: every CONNECTABLE provider, credential or not.

    Failure-isolated by its caller like every other v2 block. See
    `hermes_cli.provider_catalog.provider_login_catalog` for why this exists —
    in one line: without it a client cannot distinguish "never configured" from
    "configured and dead", because both render as an absence of usable models.
    """
    from hermes_cli.provider_catalog import provider_login_catalog

    return provider_login_catalog()


def build_provider_visibility() -> dict:
    """Typed, machine-readable snapshot of every credential pool — the contract
    the Launcher's provider/model surfaces consume instead of scraping the
    human `hermes auth list` table. Mirrors that command's provider iteration
    (skips empty pools, marks the selected credential) but emits structure, not
    prose, so a present-but-failing credential is never indistinguishable from
    an absent one.
    """
    from agent.credential_pool import list_custom_pool_providers, load_pool
    from hermes_cli.auth import PROVIDER_REGISTRY
    from hermes_cli.auth_commands import _display_source

    provider_ids = sorted(
        {*PROVIDER_REGISTRY.keys(), "openrouter", *list_custom_pool_providers()}
    )
    providers_out = []
    for provider in provider_ids:
        pool = load_pool(provider)
        entries = pool.entries()
        if not entries:
            continue
        current = pool.peek()
        credentials = []
        for idx, entry in enumerate(entries, start=1):
            credentials.append(
                {
                    "index": idx,
                    "label": entry.label,
                    "auth_type": entry.auth_type,
                    "source": _display_source(entry.source),
                    "selected": current is not None and entry.id == current.id,
                    "health": _credential_health(entry),
                    # Last-4 only, matching the dashboard's existing preview
                    # rule. Enough to tell two keys apart in a UI; never enough
                    # to use. See _credential_token_preview for why this is the
                    # only shape allowed anywhere near this payload.
                    "token_preview": _credential_token_preview(entry),
                }
            )
        providers_out.append({"id": provider, "credentials": credentials})
    payload: dict = {
        "schema": "hermes.provider_visibility/v2",
        "providers": providers_out,
    }
    # v2 additions (transport plan W4): the fields the launcher used to
    # scrape out of the human `hermes status` ◆-box — model/provider, API-key
    # presence (STATUS_API_KEYS is the box's own registry, hoisted so this
    # cannot drift from it), and OAuth login state. Each block is
    # failure-isolated: a broken import or status probe drops the block, it
    # NEVER breaks the credential payload above (which the launcher's model
    # switcher depends on). Consumers treat an absent block as "fall back to
    # the scrape", exactly like a v1 hermes.
    try:
        payload["environment"] = _provider_visibility_environment()
    except Exception:
        pass
    try:
        payload["api_keys"] = _provider_visibility_api_keys()
    except Exception:
        pass
    try:
        payload["auth_logins"] = _provider_visibility_auth_logins()
    except Exception:
        pass
    # The `catalog` block (plan PL-1). Additive and failure-isolated exactly
    # like the three blocks above, and — deliberately — WITHOUT a schema bump.
    #
    # Decided out loud at PL-1: the schema string stays
    # `hermes.provider_visibility/v2`. Every consumer feature-detects blocks
    # rather than reading the version (the Launcher decides "v2" by the
    # presence of `environment`, and never reads `schema` at all), so a bump
    # buys no consumer behaviour; meanwhile the string IS pinned by tests, one
    # of which was left stale and red by the v1→v2 bump. A version string
    # nothing branches on is a change-detector, so the additive-key path — the
    # one the plan names as preferred when a consumer pins the string — is
    # taken. `catalog` present ⇒ this hermes can name connectable providers.
    try:
        payload["catalog"] = _provider_visibility_catalog()
    except Exception:
        pass
    return payload


def _provider_visibility_environment() -> dict:
    from hermes_cli.status import _configured_model_label, _effective_provider_label

    try:
        from hermes_cli.config import load_config

        config = load_config()
    except Exception:
        config = {}
    return {
        "model": _configured_model_label(config),
        "provider": _effective_provider_label(),
    }


def _provider_visibility_api_keys() -> list[dict]:
    from hermes_cli.auth import get_anthropic_key
    from hermes_cli.status import STATUS_API_KEYS, resolve_status_env

    out: list[dict] = []
    for name, env_ref in STATUS_API_KEYS.items():
        if name == "Anthropic":
            # Same single source of truth the status box uses (also resolves
            # OAuth tokens).
            configured = bool(get_anthropic_key())
        else:
            configured = bool(resolve_status_env(env_ref))
        out.append({"name": name, "configured": configured})
    return out


def _provider_visibility_auth_logins() -> list[dict]:
    from hermes_cli.auth import (
        get_codex_auth_status,
        get_minimax_oauth_auth_status,
        get_nous_auth_status,
        get_qwen_auth_status,
    )

    logins: list[dict] = []
    for name, probe in (
        ("Nous Portal", get_nous_auth_status),
        ("OpenAI Codex", get_codex_auth_status),
        ("Qwen", get_qwen_auth_status),
        ("MiniMax", get_minimax_oauth_auth_status),
    ):
        try:
            status = probe() or {}
        except Exception:
            status = {}
        entry: dict = {"name": name, "logged_in": bool(status.get("logged_in"))}
        refreshed = status.get("last_refresh")
        if refreshed:
            entry["refreshed_at"] = str(refreshed)
        logins.append(entry)
    return logins


def _cmd_providers(args) -> int:
    payload = build_provider_visibility()
    if getattr(args, "json", False):
        print(emit_json(payload))
        return 0
    if not payload["providers"]:
        print("No credential pools configured.")
        return 0
    for provider in payload["providers"]:
        print(f"{provider['id']} ({len(provider['credentials'])} credentials):")
        for credential in provider["credentials"]:
            health = credential["health"]
            tag = "" if health["state"] == "healthy" else f"  [{health['state']}]"
            marker = " ←" if credential["selected"] else ""
            print(
                f"  #{credential['index']}  {credential['label']:<20} "
                f"{credential['auth_type']:<7} {credential['source']}{tag}{marker}"
            )
    return 0


# --- `hermes harness usage` implementation ------------------------------------
#
# Structure mirrors build_provider_visibility: every seam is failure-isolated so
# a broken probe on one provider can NEVER sink the envelope or another lane.
# The reusable fetch/render primitives live upstream in agent/account_usage.py
# (which this module must not modify) — snapshot → dict serialization lives HERE.


def _usage_provider_label(provider_id: str) -> str:
    """Human display name for a provider id, degrading to the id itself."""
    try:
        from hermes_cli.models import provider_label

        return provider_label(provider_id)
    except Exception:
        return provider_id


def _usage_iso(dt) -> Optional[str]:
    """Serialize a datetime as ISO-8601 UTC, or None. Fail-open → None."""
    if dt is None:
        return None
    try:
        aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def _resolve_active_provider_id() -> Optional[str]:
    """Normalized effective provider id, mirroring the CLI runtime resolution
    seam (`hermes_cli/status.py::_effective_provider_label`) but emitting the id
    rather than a label. Fail-open → None; ``auto`` (unresolved) also maps to
    None so callers get a concrete id or nothing.
    """
    try:
        from hermes_cli.auth import AuthError, resolve_provider
        from hermes_cli.runtime_provider import resolve_requested_provider

        requested = resolve_requested_provider()
        try:
            effective = resolve_provider(requested)
        except AuthError:
            effective = requested or None
        normalized = str(effective or "").strip().lower() or None
        if normalized in {"", "auto"}:
            return None
        return normalized
    except Exception:
        return None


def _codex_usage_login_detected() -> bool:
    """Codex lane detected when the OAuth status is logged-in OR the credential
    pool holds any openai-codex entry."""
    try:
        from hermes_cli.auth import get_codex_auth_status

        if bool((get_codex_auth_status() or {}).get("logged_in")):
            return True
    except Exception:
        pass
    try:
        from agent.credential_pool import load_pool

        return bool(load_pool("openai-codex").entries())
    except Exception:
        return False


def _openrouter_usage_login_detected() -> bool:
    """OpenRouter lane detected when the pool holds an entry OR the runtime
    resolver finds a usable key."""
    try:
        from agent.credential_pool import load_pool

        if load_pool("openrouter").entries():
            return True
    except Exception:
        pass
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested="openrouter")
        return bool(str(runtime.get("api_key", "") or "").strip())
    except Exception:
        return False


def _usage_lane_detected(provider_id: str) -> bool:
    """True iff the operator is signed-in / holds credentials for ``provider_id``.
    Only detected lanes are ever emitted (undetected providers are omitted).
    Fail-open per provider → False."""
    try:
        if provider_id == "openai-codex":
            return _codex_usage_login_detected()
        if provider_id == "anthropic":
            from agent.anthropic_adapter import resolve_anthropic_token

            return bool((resolve_anthropic_token() or "").strip())
        if provider_id == "openrouter":
            return _openrouter_usage_login_detected()
        if provider_id == "nous":
            from hermes_cli.auth import get_provider_auth_state

            tok = (get_provider_auth_state("nous") or {}).get("access_token")
            return bool(isinstance(tok, str) and tok.strip())
    except Exception:
        return False
    return False


def _fetch_usage_lane(provider_id: str):
    """Fetch the account-usage snapshot for one provider (may return None or
    raise; callers isolate failures). Nous flows through the portal-account +
    credits-snapshot path; the rest dispatch DIRECTLY to their per-provider
    fetcher.

    The direct dispatch is the point. ``agent.account_usage.fetch_account_usage``
    wraps all three shared fetchers in a blanket ``except Exception: return
    None`` (upstream-owned, `:884-902` — this module must not modify it), which
    erases the failure CLASS before ``_fetch_usage_lanes``' honest per-lane
    handler can report it. The None then serializes as the unfalsifiable
    ``no usage data``: a swallowed 401 rendered exactly like a provider that
    genuinely has nothing to say. Routing around the wrapper (the fork-boundary
    rule: route around upstream, don't patch it) lets the exception reach the
    handler that was built to name it.
    """
    if provider_id == "nous":
        from agent.account_usage import build_nous_credits_snapshot
        from hermes_cli.nous_account import get_nous_portal_account_info

        account = get_nous_portal_account_info(force_fresh=True)
        return build_nous_credits_snapshot(account)
    if provider_id == "openai-codex":
        from agent.account_usage import _fetch_codex_account_usage

        return _fetch_codex_account_usage()
    if provider_id == "anthropic":
        from agent.account_usage import _fetch_anthropic_account_usage

        return _fetch_anthropic_account_usage()
    if provider_id == "openrouter":
        from agent.account_usage import _fetch_openrouter_account_usage

        return _fetch_openrouter_account_usage(None, None)
    from agent.account_usage import fetch_account_usage

    return fetch_account_usage(provider_id)


def _usage_failure_reason(exc: BaseException) -> str:
    """The operator-facing reason for a raised usage fetch.

    CLASS NAME ONLY for everything except an HTTP status error, where the bare
    numeric status is added — a status code leaks nothing (no token, no URL, no
    exception message), and it is the single fact that separates "the provider
    rejected our credential" from "the network broke". 401/403 additionally
    earn the re-auth hint, matching the reauth-vs-connectivity discipline: only
    a confirmed auth rejection may suggest signing in again.
    """
    name = type(exc).__name__
    status = None
    response = getattr(exc, "response", None)
    if response is not None:
        raw = getattr(response, "status_code", None)
        if isinstance(raw, int):
            status = raw
    if status is None or name != "HTTPStatusError":
        return f"usage fetch failed ({name})"
    suffix = " — re-auth may be required" if status in (401, 403) else ""
    return f"usage fetch failed (HTTP {status}{suffix})"


def _serialize_usage_window(window) -> Optional[dict]:
    """Serialize one usage window into a lane-window dict, or return None to DROP
    the window.

    A non-finite ``used_percent`` (NaN / inf leaking out of an upstream fetcher)
    would serialize through ``emit_json`` — which is ``json.dumps`` with the
    default ``allow_nan=True`` — as the bare tokens ``NaN`` / ``Infinity``. That
    is invalid JSON, and the Launcher's strict parser drops the ENTIRE envelope
    into its failure state. So a non-finite percent drops just this window (never
    nulled, never clamped) rather than corrupting the whole payload. A genuinely
    unknown percent (``None``) is still a valid window and is kept.
    """
    used = window.used_percent
    if used is not None:
        try:
            used = float(used)
        except (TypeError, ValueError):
            # Non-numeric percent (shouldn't happen for a typed snapshot): treat
            # as unknown rather than crash the whole envelope.
            used = None
        else:
            if not math.isfinite(used):
                return None
    return {
        "label": window.label,
        # Raw float, not clamped — the console decides how to present overage.
        "used_percent": used,
        "reset_at": _usage_iso(window.reset_at),
        "detail": window.detail,
    }


def _unavailable_usage_lane(provider_id: str, reason: str, *, active: bool) -> dict:
    return {
        "provider": provider_id,
        "display_name": _usage_provider_label(provider_id),
        "active": active,
        "available": False,
        "plan": None,
        "source": None,
        "fetched_at": None,
        "windows": [],
        "details": [],
        "unavailable_reason": reason,
    }


def _serialize_usage_lane(provider_id: str, snapshot, *, active: bool) -> dict:
    """Serialize an AccountUsageSnapshot (or None) into a lane dict.

    A None snapshot on a detected login lane is emitted as ``no usage data``,
    which since the direct-dispatch change in [_fetch_usage_lane] means exactly
    ONE thing: the fetcher DECLINED — it returned without raising, e.g. no token
    resolved, or the provider exposes no usage surface. It no longer means
    "anything broke": a fetcher that raises now reaches
    [_fetch_usage_lanes]' handler and is reported by class or HTTP status.
    """
    if snapshot is None:
        return _unavailable_usage_lane(provider_id, "no usage data", active=active)
    return {
        "provider": provider_id,
        "display_name": _usage_provider_label(provider_id),
        "active": active,
        "available": bool(snapshot.available),
        "plan": snapshot.plan,
        "source": snapshot.source,
        "fetched_at": _usage_iso(snapshot.fetched_at),
        # A window whose percent is non-finite serializes to None and is dropped
        # here — an empty windows list on an otherwise-available lane is honest.
        "windows": [
            w
            for w in (_serialize_usage_window(win) for win in snapshot.windows)
            if w is not None
        ],
        "details": [str(d) for d in snapshot.details],
        "unavailable_reason": snapshot.unavailable_reason,
    }


def _detect_usage_candidates(only_provider: Optional[str]) -> list[str]:
    providers = _USAGE_LANE_PROVIDERS
    if only_provider:
        norm = str(only_provider).strip().lower()
        providers = tuple(p for p in providers if p == norm)
    return [p for p in providers if _usage_lane_detected(p)]


def _fetch_usage_lanes(
    candidates: list[str],
    *,
    active_provider: Optional[str],
    timeout: float,
) -> list[dict]:
    """Fetch every candidate lane CONCURRENTLY, bounded by an overall wall-clock
    deadline. Per-lane timeout/failure degrades to an unavailable lane carrying a
    CLASS-NAME-ONLY reason (never an exception message, which could leak a token
    or URL). Never blocks process exit on a hung fetch."""
    import concurrent.futures

    lanes_by_provider: dict[str, dict] = {}
    deadline = time.monotonic() + max(0.0, float(timeout))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(candidates)))
    try:
        future_map = {pool.submit(_fetch_usage_lane, p): p for p in candidates}
        for future, provider_id in future_map.items():
            active = provider_id == active_provider
            # Deadline-derived remaining bound: forwarding a shared deadline as
            # each future's result timeout keeps the OVERALL wall clock ≈timeout
            # even though the futures run concurrently.
            remaining = max(0.0, deadline - time.monotonic())
            try:
                snapshot = future.result(timeout=remaining)
            except concurrent.futures.TimeoutError:
                lanes_by_provider[provider_id] = _unavailable_usage_lane(
                    provider_id, "usage fetch failed (TimeoutError)", active=active
                )
            except Exception as exc:  # noqa: BLE001 — class/status only, fail-open
                lanes_by_provider[provider_id] = _unavailable_usage_lane(
                    provider_id,
                    _usage_failure_reason(exc),
                    active=active,
                )
            else:
                lanes_by_provider[provider_id] = _serialize_usage_lane(
                    provider_id, snapshot, active=active
                )
    finally:
        # Don't let the context-manager shutdown(wait=True) block on a hung
        # provider fetch — each underlying fetch already carries its own HTTP
        # timeout, and cancel_futures drops anything not yet started.
        pool.shutdown(wait=False, cancel_futures=True)
    return [lanes_by_provider[p] for p in candidates if p in lanes_by_provider]


def build_account_usage(
    *,
    only_provider: Optional[str] = None,
    timeout: float = DEFAULT_USAGE_TIMEOUT,
) -> dict:
    """Build the ``hermes.account_usage/v1`` envelope: one lane per detected
    provider login (codex / anthropic / openrouter / nous), fetched concurrently
    under an overall wall-clock bound. Fail-open at every seam — worst case the
    envelope carries empty lanes."""
    try:
        active_provider = _resolve_active_provider_id()
    except Exception:
        active_provider = None
    payload: dict = {
        "schema": USAGE_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "active_provider": active_provider,
        "lanes": [],
    }
    try:
        candidates = _detect_usage_candidates(only_provider)
    except Exception:
        return payload
    if not candidates:
        return payload
    try:
        payload["lanes"] = _fetch_usage_lanes(
            candidates, active_provider=active_provider, timeout=timeout
        )
    except Exception:
        payload["lanes"] = []
    return payload


def _render_account_usage_human(payload: dict) -> None:
    """Render the envelope as human lines, reusing the shared
    ``render_account_usage_lines`` per available lane."""
    from agent.account_usage import (
        AccountUsageSnapshot,
        AccountUsageWindow,
        render_account_usage_lines,
    )

    print(f"Active provider: {payload.get('active_provider') or '(none)'}")
    lanes = payload.get("lanes") or []
    if not lanes:
        print("No account-usage lanes (no signed-in providers detected).")
        return
    for lane in lanes:
        marker = " *" if lane.get("active") else ""
        print("")
        print(f"{lane.get('display_name') or lane.get('provider')}{marker}")
        if not lane.get("available"):
            print(f"  Unavailable: {lane.get('unavailable_reason') or 'no usage data'}")
            continue
        windows = tuple(
            AccountUsageWindow(
                label=w.get("label"),
                used_percent=w.get("used_percent"),
                reset_at=_parse_usage_iso(w.get("reset_at")),
                detail=w.get("detail"),
            )
            for w in lane.get("windows") or []
        )
        snapshot = AccountUsageSnapshot(
            provider=lane.get("provider") or "",
            source=lane.get("source") or "",
            fetched_at=datetime.now(timezone.utc),
            plan=lane.get("plan"),
            windows=windows,
            details=tuple(lane.get("details") or []),
        )
        for line in render_account_usage_lines(snapshot):
            print(f"  {line}")


def _parse_usage_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _empty_usage_envelope() -> dict:
    """Minimal, always-serializable ``hermes.account_usage/v1`` envelope with no
    lanes — the guaranteed fallback whenever a richer build or serialization step
    fails."""
    return {
        "schema": USAGE_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "active_provider": None,
        "lanes": [],
    }


def _emit_usage_json(payload: dict) -> None:
    """Print the envelope as JSON, guaranteeing the ``--json`` branch NEVER
    raises. The verb's contract is total failure isolation, but ``emit_json`` (or
    the stdout write itself) can still fail; if it does, fall back to a minimal
    always-valid empty envelope serialized with the stdlib ``json.dumps`` so the
    fallback does not depend on the possibly-broken ``emit_json``. If even that
    write fails there is nothing more we can do, so it is swallowed and the verb
    still exits 0."""
    try:
        print(emit_json(payload))
        return
    except Exception:
        pass
    try:
        print(json.dumps(_empty_usage_envelope()))
    except Exception:
        pass


def _cmd_usage(args) -> int:
    """`hermes harness usage` — typed per-provider account-usage envelope. Total
    failure isolation: nothing here may raise; worst case is a valid envelope
    with empty lanes."""
    only_provider = getattr(args, "provider", None)
    timeout = float(getattr(args, "timeout", DEFAULT_USAGE_TIMEOUT) or DEFAULT_USAGE_TIMEOUT)
    try:
        payload = build_account_usage(only_provider=only_provider, timeout=timeout)
    except Exception:
        payload = _empty_usage_envelope()
    if getattr(args, "json", False):
        _emit_usage_json(payload)
        return 0
    try:
        _render_account_usage_human(payload)
    except Exception:
        # Human rendering must not crash the verb either.
        _emit_usage_json(payload)
    return 0


def _cmd_skills_inventory(args) -> int:
    from agent_runtime.skills_inventory import build_skills_inventory

    payload = build_skills_inventory()
    if getattr(args, "json", False):
        print(emit_json(payload))
        return 0

    root = payload["shared_root"] or "(none)"
    print(f"Shared skills root: {root}")
    if not payload["skills"]:
        print("  (no shared skills)")
    for skill in payload["skills"]:
        count = skill["file_count"]
        files = f"{count} file" + ("" if count == 1 else "s")
        shadow = f"  shadowed by {', '.join(skill['shadowed_by'])}" if skill["shadowed_by"] else ""
        print(f"  {skill['slug']:<28} {files}{shadow}")
        if skill["description"]:
            print(f"      {skill['description']}")
    print("Personas:")
    for persona in payload["personas"]:
        print(f"  {persona['id']:<16} {len(persona['skills'])} skills")
    print("Realms:")
    for realm in payload["realms"]:
        bound = "server" if realm["server_bound"] else "local"
        state = realm["sync_state"] or "not checked"
        drift = f"  drift: {', '.join(realm['skills_drift'])}" if realm["skills_drift"] else ""
        print(f"  {realm['realm_id']:<20} [{bound}] {state}{drift}")
    return 0


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
        worktree_min_age_seconds=int(
            getattr(args, "worktree_min_age_seconds", DEFAULT_WORKTREE_MIN_AGE_SECONDS)
            or DEFAULT_WORKTREE_MIN_AGE_SECONDS
        ),
    )
    data = {
        # Command status ("the doctor ran to completion"), NOT the runtime's
        # health — the refusal branch above spends the same key on a rejected
        # invocation. ``healthy`` is the verdict a triage reader wants, mirrored
        # up from ``hygiene.ok`` so `--json | jq .healthy` cannot read the
        # command's exit as an all-clear over an unexamined body.
        "ok": True,
        "healthy": bool(hygiene.get("ok", False)),
        # ``runtime_resolution`` predates the cross-verb ``resolution`` block
        # and stays for its richer layers table; the standard block is stamped
        # below from the SAME resolution object (attach_root_observability).
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
    attach_root_observability(data, resolution=resolution)
    if args.json:
        print(emit_json(data))
    else:
        print("Harness runtime resolution")
        print(f"resolved: {resolution.store_root} ({resolution.layer})")
        for row in data["runtime_resolution"]["layers"]:
            marker = "*" if row["winner"] else " "
            print(
                f"{marker} {row['layer']:<7} value={row['value'] or '<unset>'} "
                f"exists={row['exists']}"
            )
        summary = hygiene.get("summary") or {}
        counts = summary.get("finding_counts") or {}
        event_log = hygiene.get("findings", {}).get("event_log") or {}
        print("Harness doctor")
        # THE verdict line, first. Text mode printed two counts and an event-log
        # size — three of the five examined sections never reached the operator
        # at all, so a diverged binding or an unreadable model-authority config
        # was invisible on the default path. Every section reports here now, and
        # ``unknown`` is stated as unknown rather than folded into a clean run.
        verdict = (
            "ok"
            if hygiene.get("ok")
            else "NEEDS FIX"
            if summary.get("needs_fix")
            else "UNKNOWN"
        )
        print(f"verdict: {verdict}")
        # Render exactly the findings the report emits. The mission-era counts
        # (runs/workers/open tasks/incidents/compactable rows) went away with
        # the lane; reading them here is what made the default path crash.
        # ``None`` means the class was not observed — say so, never print 0.
        print(
            "findings: "
            + " ".join(
                f"{name}={'unknown' if count is None else count}"
                for name, count in counts.items()
            )
        )
        # Where each section keeps its own error text. ``snapshot_null_id_rows``
        # is a bare list of rows, so its build outcome lives one key over.
        detail_sources = {
            "orphan_worktrees": hygiene.get("findings", {}).get("orphan_worktrees"),
            "snapshot_null_id_rows": hygiene.get("findings", {}).get("snapshot_build"),
            "event_log": hygiene.get("findings", {}).get("event_log"),
            "model_authority": hygiene.get("model_authority"),
            "persona_binding": hygiene.get("persona_binding"),
        }
        for name, health in sorted((summary.get("section_health") or {}).items()):
            if health in (None, "ok"):
                continue
            source = detail_sources.get(name)
            detail = source.get("error") if isinstance(source, dict) else None
            print(f"  {name}: {health}" + (f" ({detail})" if detail else ""))
        if event_log.get("health") == "unknown":
            print(f"event log: unknown ({event_log.get('error') or 'unreadable'})")
        else:
            print(
                "event log: "
                f"size={event_log.get('size_bytes')} bytes lines={event_log.get('line_count')} "
                f"archive_slices={event_log.get('archived_event_slices')} "
                f"index={event_log.get('index_health')}"
            )
        binding = hygiene.get("persona_binding") or {}
        if binding.get("diverged_count"):
            print(
                f"persona binding: {binding['diverged_count']} diverged — "
                f"{binding.get('remediation', '')}"
            )
        for notice in (hygiene.get("model_authority") or {}).get("notices") or []:
            print(f"model authority: {notice}")
        if getattr(args, "fix", False):
            mode = "dry run" if getattr(args, "dry_run", False) else "applied"
            print(f"repairs: {mode}")
    return 0


def _cmd_serve(args) -> int:
    # serve is a real module (not an exec'd part): it is the process's main
    # loop, owns sys.stdout/sys.stderr swaps, and is imported by tests.
    from hermes_cli.harness_parts.serve import _cmd_serve as _run_serve

    return _run_serve(args)


def _cmd_serve_connect(args) -> int:
    from hermes_cli.harness_parts.serve import _cmd_serve_connect as _run_connect

    return _run_connect(args)


def _load_command_parts() -> None:
    parts_dir = Path(__file__).with_name("harness_parts")
    for filename in ("persona_commands.py", "runtime_commands.py", "board.py", "office.py", "flow_commands.py", "checkpoint_commands.py"):
        path = parts_dir / filename
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), globals())


_load_command_parts()
