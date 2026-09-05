from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hermes_time import now
from hermes_constants import get_hermes_home
from hermes_cli.profiles import list_profiles
from hermes_cli.flag_binding import list_flag_or_empty

from agent_runtime.cli_format import emit_json, emit_json_line
from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config, mission_chat_clarify_token_binding, persona_skill_sources, resolve_mission_chat_max_seconds
from agent_runtime.continuity import return_summary_to_parent_session
from agent_runtime.dispatch_session_policy import (
    derive_dispatch_title,
    resolve_dispatch_session_decision,
    session_established_payload,
)
from agent_runtime.coordinator_permissions import (
    CoordinatorPermissionScope,
    review_coordinator_budget,
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
    doctor_detail_sources,
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
    skill_tombstone_rows,
    sync_artifacts_for_workspace_agent,
)
from agent_runtime.resolution import resolution_table, resolve_runtime
from agent_runtime.scope_activation import (
    activate_realm,
    activate_workspace,
    realm_row as _scope_realm_row,
    reconcile_active_workspace_to_realm as _scope_reconcile_active_workspace_to_realm,
    workspace_row as _scope_workspace_row,
)
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

# Pure stdlib, and deliberately at module scope while `agent.charsheet.draft`
# stays a lazy import inside the character verbs: `draft` pulls the pixel
# pipeline (and Pillow behind it) and every other harness verb would pay for it,
# whereas `agent.charsheet.errors` costs one import of `agent.charsheet.spec`.
from agent.charsheet.errors import CharsheetRefusal, DraftBusy

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

    ``controls`` is the opt-in half: a flag listed here is registered only on
    the verbs that ask for it. Only five tokens are ever asked for —
    ``dry_run`` (29 call sites), ``yes`` (7), ``sort`` (7),
    ``idempotency_key`` (3), ``limit`` (2), counted by walking the 58
    ``_add_stage42_global_args(...)`` calls in this file's AST. Branches for
    ``cursor`` and ``since`` also lived here and no call site had ever named
    either, so ``--cursor`` / ``--since`` could not be registered on any verb
    in the surface's history; they were removed 2026-08-19. (The neighbouring
    ``read --since-offset`` is a different, live flag.) A control token with no
    caller is the same defect as an unread flag, one level up: it advertises
    that a verb COULD opt in, and none can.

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

    # Remote-gateway Stage 0b. Beside `roots` deliberately: both answer
    # questions about THIS MACHINE'S runtime root rather than about anything in
    # the store, and neither reads a workspace or a realm.
    #
    # Two subverbs, where the plan wrote `gateway id --set-name`. A rename is a
    # WRITE, and on this tree a write is its own subverb with its own control
    # set (`roots set`, `workspace rename`) — `_add_stage42_global_args` cannot
    # give one parser both the reader's full flag set and the writer's
    # `--dry-run`, so a single verb would have had to advertise one of them
    # falsely. The deviation is recorded in
    # `docs/agent-runtime-harness/planned/remote-gateway.md` § Stage 0b.
    gateway = subs.add_parser(
        "gateway",
        help="This runtime root's remote-gateway install identity — the name and id a phone's install picker shows",
    )
    gateway_subs = gateway.add_subparsers(dest="gateway_command", required=True)
    gateway_id = gateway_subs.add_parser(
        "id",
        help="Show this root's install identity (read-only — never mints; a root that has never served has none)",
    )
    _add_stage42_global_args(gateway_id)
    gateway_id.set_defaults(func=_cmd_gateway_id)
    gateway_rename = gateway_subs.add_parser(
        "rename",
        help="Set the operator-facing display name for this root's install (the install_id never changes)",
    )
    gateway_rename.add_argument("name", help="What a human should call this install, e.g. workstation")
    _add_stage42_global_args(gateway_rename, controls=frozenset({"dry_run"}))
    gateway_rename.set_defaults(func=_cmd_gateway_rename)

    # Stage 1's three. `pair` is a WRITE (it mints a credential channel) and
    # still takes the reader's flag set rather than `dry_run`, which is the one
    # deviation from `rename`'s reasoning and has its own: a dry run of `pair`
    # would have to print a code it did not mint, and a preview that shows an
    # operator eight characters nothing will accept is worse than no preview.
    # `devices revoke` DOES take `dry_run`, because there the preview is a real
    # row an operator can recognise before they cut a device off.
    gateway_pair = gateway_subs.add_parser(
        "pair",
        help="Mint a short-TTL pairing code plus the QR payload a device scans to reach this install",
    )
    gateway_pair.add_argument("--name", help="What to call the device that redeems this code, e.g. \"the phone\"")
    gateway_pair.add_argument(
        "--tier",
        choices=["console", "read"],
        default="console",
        help="What the paired device may do. console: run console verbs (create/retire agents). read: view only.",
    )
    _add_stage42_global_args(gateway_pair)
    gateway_pair.set_defaults(func=_cmd_gateway_pair)
    # S2. `introduce` sits under `gateway` rather than under `peers`, and that
    # placement is the honest one: it mints BOTH halves — a peer code and a
    # device code — so filing it under `peers` would name half of what it does.
    # It takes the reader's flag set for `pair`'s reason (a dry run would have
    # to print codes it did not mint).
    gateway_introduce = gateway_subs.add_parser(
        "introduce",
        help="Mint one envelope a launcher posts as a backend pair-grant: a peer code, a device code, and where to dial this install",
    )
    gateway_introduce.add_argument(
        "--for-install",
        dest="for_install",
        help="The hermes install id this introduction is FOR — the peer half is minted scoped to it and no other install can spend it",
    )
    gateway_introduce.add_argument(
        "--for-device",
        dest="for_device",
        help="The ACCOUNT device id this introduction is for — copied onto the device row as a join key (a label, not a check)",
    )
    gateway_introduce.add_argument(
        "--correlation",
        help="The backend grant id, stamped through both mints and every event so all three parties name one errand",
    )
    gateway_introduce.add_argument(
        "--note",
        help="What this introduction is for, e.g. \"the laptop\" — shown while the codes are pending",
    )
    _add_stage42_global_args(gateway_introduce)
    gateway_introduce.set_defaults(func=_cmd_gateway_introduce)
    gateway_devices = gateway_subs.add_parser(
        "devices",
        help="Devices paired with this install's gateway — list them, or revoke one",
    )
    gateway_devices_subs = gateway_devices.add_subparsers(
        dest="gateway_devices_command", required=True
    )
    gateway_devices_list = gateway_devices_subs.add_parser(
        "list",
        help="Every paired device, oldest first, revoked ones included (never the credential — there is no field for it)",
    )
    # No `sort` control, and that is the honest registration rather than the
    # generous one: `list_devices` returns one deterministic order (created_at,
    # then device_id) and nothing here re-sorts, so advertising the flag would
    # be a WRONG ANSWER believed — an operator who passes `--sort name` gets the
    # unsorted answer and no signal the flag was ignored. Exactly what
    # `_add_stage42_global_args`' own docstring is built around, and what
    # `test_every_stage42_global_flag_is_honored` caught here.
    _add_stage42_global_args(gateway_devices_list)
    gateway_devices_list.set_defaults(func=_cmd_gateway_devices_list)
    gateway_devices_revoke = gateway_devices_subs.add_parser(
        "revoke",
        help="Refuse a paired device from its next handshake on (the row is kept, so an audit can tell it from never-paired)",
    )
    gateway_devices_revoke.add_argument(
        "device_id", help="The device id from `harness gateway devices list`"
    )
    _add_stage42_global_args(gateway_devices_revoke, controls=frozenset({"dry_run"}))
    gateway_devices_revoke.set_defaults(func=_cmd_gateway_devices_revoke)

    # Stage 6's four. A `peers` subtree beside `devices` rather than more verbs
    # under it, because the two answer different questions about different
    # stores: `devices` is "which phones may reach this install", `peers` is
    # "which INSTALLS has an operator approved an edge with". Folding them would
    # make a list that mixes a phone and a workstation and needs a `kind` column
    # to be readable — which is a discriminator standing in for the two verbs
    # this tree already has room for.
    #
    # `pair` and `join` take the reader's flag set for `gateway pair`'s reason
    # (a dry run would have to print a code it did not mint, or perform half a
    # handshake); `revoke` takes `dry_run`, because there the preview is a real
    # row an operator can recognise before they cut an install off.
    gateway_peers = gateway_subs.add_parser(
        "peers",
        help="Installs paired with this one (operator-approved on BOTH sides) — pair, join, list, revoke",
    )
    gateway_peers_subs = gateway_peers.add_subparsers(
        dest="gateway_peers_command", required=True
    )
    gateway_peers_pair = gateway_peers_subs.add_parser(
        "pair",
        help="Mint a short-TTL PEER code plus the payload another install's operator runs `peers join` with",
    )
    gateway_peers_pair.add_argument(
        "--note", help="What this edge is for, e.g. \"laptop\" — shown while the code is pending"
    )
    _add_stage42_global_args(gateway_peers_pair)
    gateway_peers_pair.set_defaults(func=_cmd_gateway_peers_pair)
    gateway_peers_join = gateway_peers_subs.add_parser(
        "join",
        help="Redeem a peer code from ANOTHER install: dials it, and records the edge in both stores",
    )
    gateway_peers_join.add_argument(
        "payload",
        help="The join_payload string from `harness gateway peers pair` over there, or the bare 8-character code with --host/--port",
    )
    gateway_peers_join.add_argument("--host", help="Override the address in the payload (a second interface, a NAT, a machine that moved)")
    gateway_peers_join.add_argument("--port", type=int, help="Override the port in the payload")
    gateway_peers_join.add_argument("--fingerprint", help="Override the certificate fingerprint to pin; omitting it pins NOTHING, which is weaker")
    gateway_peers_join.add_argument(
        "--expect-fingerprint",
        dest="expect_fingerprint",
        help="The certificate fingerprint the ACCOUNT attests for that install; a payload that disagrees is refused before anything is dialled",
    )
    gateway_peers_join.add_argument(
        "--correlation",
        help="The backend grant id this join fulfils; echoed on the receipt so all three parties name one errand",
    )
    gateway_peers_join.add_argument("--timeout", type=float, default=20.0, help="Seconds to wait for the other install's handshake")
    _add_stage42_global_args(gateway_peers_join)
    gateway_peers_join.set_defaults(func=_cmd_gateway_peers_join)
    gateway_peers_list = gateway_peers_subs.add_parser(
        "list",
        help="Every paired install, oldest first, revoked ones included (never the credential — there is no field for it)",
    )
    # No `sort` control, for `devices list`'s reason: `list_peers` returns one
    # deterministic order (approved_at, then install id) and nothing here
    # re-sorts, so advertising the flag would be a wrong answer believed.
    _add_stage42_global_args(gateway_peers_list)
    gateway_peers_list.set_defaults(func=_cmd_gateway_peers_list)
    gateway_peers_revoke = gateway_peers_subs.add_parser(
        "revoke",
        help="Refuse a paired install from its next handshake on — ONE-SIDED: the other install keeps its own row",
    )
    gateway_peers_revoke.add_argument(
        "peer_install_id", help="The install id from `harness gateway peers list`"
    )
    gateway_peers_revoke.add_argument(
        "--no-announce",
        dest="no_announce",
        action="store_true",
        help="Skip telling the other install it was revoked (offline, or when it must not be contacted); it learns at its next call",
    )
    _add_stage42_global_args(gateway_peers_revoke, controls=frozenset({"dry_run"}))
    gateway_peers_revoke.set_defaults(func=_cmd_gateway_peers_revoke)

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
    realm_sync_revert = realm_sync_subs.add_parser(
        "revert",
        help="Revert drifted local store rows to the last-pulled upstream (local-only: no git, no network, no credential — and never mints a realm-visible tombstone)",
    )
    realm_sync_revert.add_argument("realm_id")
    realm_sync_revert.add_argument(
        "--item",
        dest="items",
        action="append",
        default=None,
        help="FAMILY:CONTAINER:KEY from `realm sync status` store_drift.items (e.g. office_actor:ws_x:dev_agent_1234); repeatable",
    )
    realm_sync_revert.add_argument(
        "--all",
        dest="revert_all",
        action="store_true",
        help="Revert every drifted item in this realm",
    )
    _add_stage42_global_args(
        realm_sync_revert, controls=frozenset({"dry_run", "yes"})
    )
    realm_sync_revert.set_defaults(func=_cmd_realm_sync_revert)

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

    skills_delete_cmd = skills_subs.add_parser(
        "delete",
        help="Delete a shared skill realm-wide: archive the local canonical package and record a tombstone so a member's surviving copy cannot republish it",
    )
    skills_delete_cmd.add_argument("skill", help="Canonical skill slug (bare name or <category>/<name>)")
    skills_delete_cmd.add_argument(
        "--realm",
        dest="realms",
        action="append",
        default=None,
        help="Narrow the delete to this realm id (repeatable). Default (R-E): every non-archived realm that currently publishes the slug — one canonical root serves all realms, so leaving one un-tombstoned resurrects the copy on that realm's next pull",
    )
    _add_stage42_global_args(skills_delete_cmd, controls=frozenset({"dry_run"}))
    skills_delete_cmd.set_defaults(func=_cmd_skills_delete)

    skills_restore_cmd = skills_subs.add_parser(
        "restore",
        help="Lift ONE realm's skill tombstone (un-tombstone only — re-admitting the BYTES is `skills promote`, and the receipt names the archived copy)",
    )
    skills_restore_cmd.add_argument("skill", help="Canonical skill slug named by the ledger entry to lift")
    skills_restore_cmd.add_argument("--realm", dest="realm", required=True, help="Realm id whose ledger entry is lifted (a tombstone is per-realm truth; there is no all-realms restore)")
    _add_stage42_global_args(skills_restore_cmd)
    skills_restore_cmd.set_defaults(func=_cmd_skills_restore)

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
    board_list.add_argument("--workspace", "--workspace-id", default=None)
    _add_stage42_global_args(board_list, controls=frozenset({"sort"}))
    board_list.set_defaults(func=_cmd_board_list)
    board_show = board_subs.add_parser("show", help="Show one board")
    board_show.add_argument("board_id")
    board_show.add_argument("--full", action="store_true", help="Include card bodies")
    _add_stage42_global_args(board_show)
    board_show.set_defaults(func=_cmd_board_show)
    board_create = board_subs.add_parser("create", help="Create a board")
    board_create.add_argument("--workspace", "--workspace-id", required=True)
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
    card_add.add_argument("--workspace", "--workspace-id", default=None)
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
    office_show.add_argument("--workspace", "--workspace-id", default=None)
    office_show.add_argument("--full", action="store_true", help="Include actor item bodies")
    _add_stage42_global_args(office_show)
    office_show.set_defaults(func=_cmd_office_show)
    office_actor_upsert = office_subs.add_parser("actor-upsert", help="Create or update one actor placement (keys are minted store-side)")
    office_actor_upsert.add_argument("--workspace", "--workspace-id", default=None)
    office_actor_upsert.add_argument("--actor-json", dest="actor_json", required=True, help="Actor object (path or inline JSON): {persona_id, persona_instance_id?, backing_profile?, items:[...]}")
    # Optional, never required: class-keyed placements are a legal shape (see
    # OfficeStore.archive_actors_for_instance). The re-key migration's fence is
    # the CONDITIONAL refusal in _cmd_office_actor_upsert, not a mandatory flag.
    office_actor_upsert.add_argument("--persona-instance-id", dest="persona_instance_id", default=None, help="Bind the placement to this persona instance (overrides --actor-json's persona_instance_id); the store still mints the key")
    office_actor_upsert.add_argument("--allow-class-key", dest="allow_class_key", action="store_true", help="Escape hatch: force a class-keyed write that would otherwise be refused for re-creating an archived or duplicated placement")
    # Orthogonal to --allow-class-key and deliberately not implied by it: that
    # flag consents to the KEY SHAPE, this one to raising a deleted key. The
    # sanctioned un-archive verb remains `harness office actor-restore`.
    office_actor_upsert.add_argument("--resurrect", dest="resurrect", action="store_true", help="Escape hatch: re-add an actor key that was deleted, clearing its tombstone (default: such a write is refused)")
    office_actor_upsert.add_argument("--expect-revision", dest="expect_revision", type=int, default=None)
    office_actor_upsert.add_argument("--updated-by", dest="updated_by", default=None)
    _add_stage42_global_args(
        office_actor_upsert, controls=frozenset({"dry_run"})
    )
    office_actor_upsert.set_defaults(func=_cmd_office_actor_upsert)
    office_actor_remove = office_subs.add_parser("actor-remove", help="Archive an actor placement (archive-never-delete); tombstones and propagates realm-wide unless --local-only")
    office_actor_remove.add_argument("--workspace", "--workspace-id", default=None)
    office_actor_remove.add_argument("--actor", required=True, help="Actor key")
    office_actor_remove.add_argument("--reason", default=None)
    # The AUTHORED-vs-DIAGNOSTIC split (operator ruling, 2026-08-30). Without
    # the flag this verb carries an operator's intent to delete and writes the
    # tombstone a realm pull replicates — which is what makes a delete stick
    # against a peer that still holds the row. With it, the actor is archived
    # HERE and nothing is asserted about the realm, which is the only honest
    # posture for a repair the operator did not ask for by name.
    office_actor_remove.add_argument("--local-only", dest="local_only", action="store_true", help="Diagnostic repair: archive on THIS install only — no tombstone, nothing propagates, and a realm pull may legitimately bring the actor back. Use for doctor/dispatch/census repairs of local projection; omit when the operator means to delete the placement everywhere")
    office_actor_remove.add_argument("--expect-revision", dest="expect_revision", type=int, default=None)
    _add_stage42_global_args(
        office_actor_remove, controls=frozenset({"dry_run"})
    )
    office_actor_remove.set_defaults(func=_cmd_office_actor_remove)
    office_actor_restore = office_subs.add_parser("actor-restore", help="Restore an archived actor placement")
    office_actor_restore.add_argument("--workspace", "--workspace-id", default=None)
    office_actor_restore.add_argument("--actor", required=True, help="Actor key")
    _add_stage42_global_args(
        office_actor_restore, controls=frozenset({"dry_run"})
    )
    office_actor_restore.set_defaults(func=_cmd_office_actor_restore)
    office_set_folders = office_subs.add_parser("set-folders", help="Replace the surface's shared folder taxonomy")
    office_set_folders.add_argument("--workspace", "--workspace-id", default=None)
    office_set_folders.add_argument("--folders", required=True, help="Comma-separated folder names (structural defaults always kept)")
    office_set_folders.add_argument("--expect-revision", dest="expect_revision", type=int, default=None)
    _add_stage42_global_args(
        office_set_folders, controls=frozenset({"dry_run"})
    )
    office_set_folders.set_defaults(func=_cmd_office_set_folders)
    office_resolve = office_subs.add_parser("resolve-conflict", help="Resolve a realm-sync conflict on an actor placement")
    office_resolve.add_argument("--workspace", "--workspace-id", default=None)
    office_resolve.add_argument("--actor", required=True, help="Actor key")
    office_resolve.add_argument("--take", required=True, choices=["local", "remote"])
    office_resolve.add_argument("--allow-class-key", dest="allow_class_key", action="store_true", help="Escape hatch: adopt a remote actor on a persona CLASS key the re-key migration archived (re-creates the class-keyed placement beside its instance-keyed sibling)")
    _add_stage42_global_args(
        office_resolve, controls=frozenset({"dry_run"})
    )
    office_resolve.set_defaults(func=_cmd_office_resolve_conflict)
    office_archive_surface = office_subs.add_parser(
        "archive-surface",
        help="Archive an ORPHANED office surface (a surface whose workspace no longer resolves); clears its orphaned_office parity warning",
    )
    office_archive_surface.add_argument("--workspace", "--workspace-id", default=None)
    _add_stage42_global_args(
        office_archive_surface, controls=frozenset({"dry_run"})
    )
    office_archive_surface.set_defaults(func=_cmd_office_archive_surface)

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
    persona_set_skills = persona_subs.add_parser("set-skills", help="Persist a persona's default skill set (profile-default lane; future instances inherit it)")
    persona_set_skills.add_argument("persona_id", help="Persona id or profile:<name>")
    # `--skill` keeps the tree's ONE spelling — `action="append", default=None`
    # — so an omitted flag arrives as ABSENT and not as `[]`. What absent MEANS
    # is what differs by tier: on `persona instance update-profile` and `agent
    # create` absent means "inherit the persona's skills", and `[]` means
    # "override with none". The template tier is the ROOT of that cascade and
    # has no one to inherit from, so absent cannot be a write at all — it is a
    # typed `nothing_to_write` refusal (see `_cmd_persona_set_skills`). Keeping
    # `default=None` is what lets the handler tell the two apart; `default=[]`
    # would hand a transport-mangled argv a silent clear-every-skill.
    persona_set_skills.add_argument("--skill", dest="skills", action="append", default=None, help="Skill id for the persona default set (repeatable); the flags given REPLACE the stored set")
    persona_set_skills.add_argument("--clear-skills", action="store_true", help="Write an empty default set: every future inheriting placement starts with no skills")
    persona_set_skills.add_argument("--issued-at", default=None, help="ISO-8601 issue timestamp; stale writes are superseded instead of applied")
    persona_set_skills.add_argument("--requested-by", default="operator")
    persona_set_skills.add_argument("--json", action="store_true")
    persona_set_skills.set_defaults(func=_cmd_persona_set_skills)
    persona_instance = persona_subs.add_parser("instance", help="Create, open, steer, retire, and maintain persona instances (chat is the only messaging lane)")
    persona_instance_subs = persona_instance.add_subparsers(dest="persona_instance_command")
    persona_instance_create = persona_instance_subs.add_parser("create", help="Create an Agent Profile (operator chat channel) or an additional placement-backed instance (--add-instance); requires --display-name")
    persona_instance_create.add_argument("--persona", dest="persona_id", required=True)
    persona_instance_create.add_argument("--title", required=True, help="Fallback display name when --display-name is empty (launcher wire-compat)")
    persona_instance_create.add_argument("--requested-by", default="cli")
    _add_coordinator_permission_args(persona_instance_create)
    persona_instance_create.add_argument("--client-message-id", default=None)
    persona_instance_create.add_argument("--display-name", default=None)
    persona_instance_create.add_argument("--session-id", default=None)
    persona_instance_create.add_argument("--kill-active", action="store_true", help="Cancel the current run/worker before replacing the active chat")
    persona_instance_create.add_argument("--add-instance", action="store_true", help="Create an additional placement-backed instance instead of targeting the primary placement")
    persona_instance_create.add_argument("--placement-id", default=None, help="Scene itemId for an additional placement-backed instance; must end in the deliberate-placement shape <persona-token>_agent_<hex8>")
    persona_instance_create.add_argument("--workspace-id", "--workspace", dest="workspace_id", default=None, help="Mission Control workspace the placement belongs to (scope-provenance pointer; only meaningful with --add-instance)")
    persona_instance_create.add_argument("--realm-id", dest="realm_id", default=None, help="Mission Control realm the placement belongs to (scope-provenance pointer; only meaningful with --add-instance)")
    # S70 removed `--auto-run` / `--stream` / `--max-actions` / `--max-seconds`,
    # and S-DUP5 finished the set with `--message`: all five belonged to the
    # retired free-floating assignment queue (argparse now rejects them cleanly
    # instead of silently ignoring them). `--title` is NOT one of them — it is
    # the live display-name fallback above.
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
    persona_instance_open.add_argument("--placement-id", default=None, help="Scene itemId for an additional placement-backed instance; must end in the deliberate-placement shape <persona-token>_agent_<hex8>")
    persona_instance_open.add_argument("--workspace-id", "--workspace", dest="workspace_id", default=None, help="Mission Control workspace the placement belongs to (scope-provenance pointer; only meaningful with --add-instance)")
    persona_instance_open.add_argument("--realm-id", dest="realm_id", default=None, help="Mission Control realm the placement belongs to (scope-provenance pointer; only meaningful with --add-instance)")
    persona_instance_open.add_argument("--display-name", default=None, help="Authoritative name for a deliberately placed additional instance; ignored unless --add-instance")
    persona_instance_open.add_argument("--requested-by", default="cli")
    _add_coordinator_permission_args(persona_instance_open)
    persona_instance_open.add_argument("--json", action="store_true")
    persona_instance_open.set_defaults(func=_cmd_persona_instance_open_chat)
    # `persona instance resolve-chat-turn` lived here until 2026-08-19: a
    # second parser binding the SAME handler as `mission-chat turn-resolve`,
    # with the same four required flags and the instance id positional instead
    # of a flag. Two spellings for one operation, zero invocations in either
    # repo, and the launcher has always emitted `turn-resolve` — so the alias
    # could only ever be reached by a human who read the wrong doc line.
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
    # D4. `delete` is an argparse ALIAS, not a second parser: one parser object,
    # so the two spellings cannot drift in flags, help, or handler — which is
    # the failure mode a copied `add_parser` would have had, and the operator
    # ruling ("why retire it should just be delete") is about the WORD, not
    # about a second behaviour.
    #
    # `retire` stays the canonical name here and everywhere machine-readable —
    # the RPC method, the capability id `persona.instance.retire`, the event
    # types, the internal symbols. Renaming those would break every launcher
    # build in the field to change a noun; the operator reads "delete" on the
    # surface, and the surface is where the ruling applies.
    persona_instance_retire = persona_instance_subs.add_parser("retire", aliases=["delete"], help="Delete (end-of-life) a placement-backed persona instance: archive its row (chat history preserved)")
    persona_instance_retire.add_argument("persona_instance_id")
    persona_instance_retire.add_argument("--reason", default="placement removed")
    persona_instance_retire.add_argument("--requested-by", default="cli")
    # S8b-b: the same flag `agent retire` took, on the OTHER door onto the same
    # `perform_agent_retire`. S8b withheld it here on the stated grounds that
    # "no gesture behind it is the truth for that door" — which was measured to
    # be false: the launcher's `persona.instance.retire` capability IS this
    # door, fired from `MissionOfficeLayoutController.retireAgent`'s
    # `Unavailable` arm, and `retireAgent` takes `correlationId` as a REQUIRED
    # parameter. So the arm the launcher falls back to when the RPC lane cannot
    # carry the call was the ONE arm that dropped the token — on the lane where
    # joining the two halves of a gesture matters most, because a degraded
    # transport is exactly when an operator greps the event log.
    persona_instance_retire.add_argument("--correlation-id", dest="correlation_id", default=None)
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
    persona_instance_update.add_argument("--clear-skills", action="store_true", help="Pin this agent to NO skills — an explicit empty set, not the template's")
    persona_instance_update.add_argument("--inherit-skills", dest="inherit_skills", action="store_true", help="Drop this agent's own skill set so it follows its persona template again, live")
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
    mission_chat_message.add_argument("--workspace-id", "--workspace", default=None)
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
    # The agent-to-agent delivery QUEUE, as opposed to the chat turns it forges
    # into. Repair verbs only: the drain owns the normal path, and this group
    # exists for the rows it gave up on.
    mission_chat_dispatch = mission_chat_subs.add_parser(
        "dispatch", help="Operate the agent-to-agent dispatch delivery queue"
    )
    mission_chat_dispatch_subs = mission_chat_dispatch.add_subparsers(
        dest="mission_chat_dispatch_command"
    )
    mission_chat_dispatch_redeliver = mission_chat_dispatch_subs.add_parser(
        "redeliver",
        help=(
            "Re-arm a DROPPED dispatch reply for another delivery pass (dropped -> "
            "pending, attempts 0, previous drop reason cleared)"
        ),
    )
    mission_chat_dispatch_redeliver.add_argument(
        "dispatch_id", help="The dispatch handle, e.g. dispatch-2540634d5cf3"
    )
    mission_chat_dispatch_redeliver.add_argument("--json", action="store_true")
    mission_chat_dispatch_redeliver.set_defaults(
        func=_cmd_mission_chat_dispatch_redeliver
    )

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
        "--dry-run",
        action="store_true",
        help=(
            "Report actions without mutating the store. To ONLY ask which chat "
            "bindings are stale, prefer `persona-instance chat-bindings`, which "
            "has no write mode at all"
        ),
    )
    persona_instance_reconcile.add_argument("--json", action="store_true")
    persona_instance_reconcile.set_defaults(func=_cmd_persona_instance_reconcile)

    # H4 (plan realm-pull-live-projection). `reconcile` DEFAULTS TO APPLY and
    # runs five phases; asking it "which instance is the amber chip" means
    # remembering --dry-run on a verb that archives rows, prunes graphs and
    # appends events when you forget. This verb has NO write mode to forget: it
    # is the phase-4 probe alone, read-only by construction.
    persona_instance_chat_bindings = persona_instance_subs.add_parser(
        "chat-bindings",
        help=(
            "READ-ONLY: name every persona instance whose bound chat session "
            "SessionDB no longer holds — the producer of the snapshot's "
            "`session_not_in_db` parity drop. Never writes; `persona-instance "
            "reconcile` is the repair"
        ),
    )
    persona_instance_chat_bindings.add_argument("--json", action="store_true")
    persona_instance_chat_bindings.set_defaults(func=_cmd_persona_instance_chat_bindings)

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
    agent_create.add_argument("--workspace", "--workspace-id", dest="workspace_id", required=True, help="Mission Control workspace the placement lands in; must already exist")
    # OPTIONAL since plan S2. Omitted, the service resolves the slot through
    # `agent_runtime.office_layout_policy` — the same lattice the launcher
    # predicts with — so a door with no canvas (this one, a cron, a remote
    # connector over `call`) has an answer instead of a required guess.
    agent_create.add_argument("--pos", dest="pos", nargs=2, metavar=("X", "Y"), default=None, help="Canvas position for the placement; omitted, the layout policy picks the first free slot in the folder")
    # BYTE-PARALLEL with the RPC's `skills` param (UC-H3's rule, plan D5).
    # `default=None` is load-bearing and is the same spelling `persona instance
    # update-profile` uses: an OMITTED flag must reach the service as an ABSENT
    # key, because absent means "inherit the persona's skills" while `[]` means
    # "override with none" — two different agents. `append` is what makes
    # `--skill a --skill b` one request rather than a last-one-wins.
    agent_create.add_argument("--skill", dest="skills", action="append", default=None, help="Assign a skill to the new instance (repeatable); a canonical harness skill is installed and hash-verified first")
    agent_create.add_argument("--display-name", default=None, help="Authoritative name; omitted falls back to the persona's configured display name")
    agent_create.add_argument("--placement-id", default=None, help="Scene itemId to predict the actor key from; must end in <persona-token>_agent_<hex8>, and omitted mints one server-side")
    agent_create.add_argument("--realm-id", dest="realm_id", default=None)
    agent_create.add_argument("--folder", default=None, help="Office folder for the placement (default: Agents)")
    # A re-run is a NEW gesture unless the caller says otherwise — the same rule
    # the launcher applies by stamping micros into every key. A script that
    # wants resume-on-retry passes its own stable key.
    agent_create.add_argument("--idempotency-key", dest="idempotency_key", default=None, help="Stable retry key; omitted mints a fresh cli-<uuid4> so a re-run is a new gesture")
    agent_create.add_argument("--correlation-id", dest="correlation_id", default=None)
    agent_create.add_argument("--json", action="store_true")
    agent_create.set_defaults(func=_cmd_agent_create)

    # S5: the INVERSE of the create above, and the door that never existed. The
    # store method has always archived BOTH halves (roster row + every office
    # actor bound to the instance); what was missing was a verb over it and an
    # ack that NAMES what it archived. `persona instance retire` stays and calls
    # the very same service function, so the two doors cannot drift.
    agent_retire = agent_subs.add_parser(
        "retire",
        help="Retire a placed agent: archive its roster row AND every office actor bound to it in ONE call",
    )
    agent_retire.add_argument("persona_instance_id", help="Persona-instance id of the placement to retire")
    agent_retire.add_argument("--reason", default="placement removed")
    agent_retire.add_argument("--requested-by", dest="requested_by", default="cli")
    # S8b: the flag `agent create` has carried since D-V2, on the verb that
    # undoes it. A script that placed an agent under one gesture token can now
    # delete it under that token, and ONE grep over the event log joins both
    # halves — which is the whole point of the token and was true of every
    # level-mutating verb except this one.
    agent_retire.add_argument("--correlation-id", dest="correlation_id", default=None)
    agent_retire.add_argument("--json", action="store_true")
    agent_retire.set_defaults(func=_cmd_agent_retire)

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
    skills.add_argument("--json", action="store_true")
    skills.set_defaults(func=_cmd_install_harness_skills)

    # STAGE 6 (2026-08-22): the help text read "Write redaction-safe
    # snapshot.json" until the boot-cache writer it named (``snapshot.write_snapshot``)
    # was deleted with the read-model lane. This verb BUILDS a frame and prints
    # it; it writes no store state. `--json` is the only flag it has ever had —
    # the cache preference was resolved from config, never from argv, so there
    # was no cache-lane flag to retire with the lane.
    snap = subs.add_parser("snapshot", help="Build and print a redaction-safe runtime snapshot frame")
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
    # STAGE 6 (duplicate-implementation retirement, 2026-08-22): the two
    # `read_model.db` verbs stood here — `rebuild-read-model` (Projector.full_rebuild)
    # and `read` (ReadModel.read_projection). Both were the ONLY production
    # entries into `agent_runtime/read_model.py`, a lane that populated a
    # database nothing on the serve path ever read: `hermes serve` builds cores
    # through `build_snapshot()` and persists them under `<store_root>/serve_read_model/`
    # via `core_cache.write_back()`, which is a different store with a different
    # validity model. Operator ruling: RETIRE. Absence is pinned by
    # `tests/agent_runtime/test_s46_incremental_projection_lane_removal.py` and
    # by the `agent_runtime.read_model` / `.projector` MODULE tombstones.

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
    pets_sprite.add_argument("--no-sheet", dest="no_sheet", action="store_true", help="Metadata only: drop `spritesheetBase64` and carry `sheet`, the absolute path, in its place. `spritesheetRevision` and every geometry/taxonomy key are unchanged. Mirrors `characters sprite --no-sheet`. The default is byte-identical to what it always was")
    pets_sprite.add_argument("--json", action="store_true")
    pets_sprite.set_defaults(func=_cmd_pets_sprite)
    pets_thumb = pets_subs.add_parser("thumb", help="Return a Petdex pet thumbnail")
    pets_thumb.add_argument("slug")
    pets_thumb.add_argument("--url", default="", help="Optional Petdex spritesheet URL for non-installed gallery pets")
    pets_thumb.add_argument("--json", action="store_true")
    pets_thumb.set_defaults(func=_cmd_pets_thumb)

    # Character sheets — the QA bridge for `agent.charsheet`. Deliberately a
    # sibling of `pets`, not an extension of it: character sheets carry their row
    # taxonomy as data and must never enter a pet read path (which infers the
    # taxonomy from sheet height and would misread a 16-row sheet as a 9-row pet).
    # Every verb here is a thin veneer over one CharacterDraft method; the stage
    # machine, the pixels and the revision store all live in agent/charsheet/.
    characters = subs.add_parser("characters", help="Mission Control character-sheet bridge (8-way sheets + QA)")
    characters_subs = characters.add_subparsers(dest="characters_command", required=True)

    characters_start = characters_subs.add_parser("start", help="Create a character draft at stage 'turnaround' (offline; generates nothing)")
    characters_start.add_argument("--concept", required=True, help="What to draw, e.g. 'a tall knight in green enamel armour'")
    characters_start.add_argument("--slug", default="", help="Install slug; defaults to a slugified display name")
    characters_start.add_argument("--display-name", dest="display_name", default="", help="Human name; defaults to the concept")
    characters_start.add_argument("--style", default="auto", help="Art-style hint passed to the prompts")
    characters_start.add_argument("--states", default="", help="Animation states as 'idle:6,walk:8[,cheer:5:fixed]'; default = the CHAR8 states")
    characters_start.add_argument("--directions", default="8", help="Direction scheme: 8 (five authored, three mirrored) or 4")
    characters_start.add_argument("--base-image", dest="base_image", default="", help="Identity-anchor image; copied into the draft")
    characters_start.add_argument("--authored-by", dest="authored_by", default="", help="Persona driving this authoring run, recorded as provenance; nothing scopes where the draft lives — the character library is install-wide at <hermes_root>/shared/characters, one directory for every persona and profile — but it is what lets a later reader check a resume is opening under the profile that authored it")
    characters_start.add_argument("--json", action="store_true")
    characters_start.set_defaults(func=_cmd_characters_start)
    characters_list = characters_subs.add_parser("list", help="List character drafts and installed characters")
    characters_list.add_argument("--json", action="store_true")
    characters_list.set_defaults(func=_cmd_characters_list)
    characters_backfill_home = characters_subs.add_parser("backfill-home", help="Record `hermes_home` on library drafts that carry no home, and on no others. The field is provenance of the authoring RUN, not an address — the library is install-wide, so this stamps the home THIS run resolved onto a draft that arrived without one (restored from quarantine, hand-copied in); `migrate-home` stamps the legacy source home instead, which is the case that had a better answer available. Explicit and receipted on purpose: a draft that already states a home keeps it, and the write leaves `updated` and every other key exactly as it found them, because the drafts this reaches are dormant exhibits whose timeline is evidence. Idempotent — a second run stamps nothing")
    characters_backfill_home.add_argument("--json", action="store_true")
    characters_backfill_home.set_defaults(func=_cmd_characters_backfill_home)
    characters_migrate_home = characters_subs.add_parser("migrate-home", help="Move THIS home's legacy `<HERMES_HOME>/characters` store into the install-wide library at `<hermes_root>/shared/characters`. Run once per profile that has one. Drafts keep their directory leaf names and installed characters keep their slugs, so a stored draft id still resolves; a draft carrying no `hermes_home` is stamped with the SOURCE home BEFORE it moves, because afterwards the directory no longer witnesses where it lived. A destination that already holds the leaf or slug is a per-entry REFUSAL, never a merge and never an overwrite, and nothing is deleted — the emptied source tree is left standing as its own tombstone. Idempotent: a second run moves nothing")
    characters_migrate_home.add_argument("--json", action="store_true")
    characters_migrate_home.set_defaults(func=_cmd_characters_migrate_home)
    characters_status = characters_subs.add_parser("status", help="Full draft state: stage, spec, per-item QA history")
    characters_status.add_argument("--draft", required=True, help="Draft id from `harness characters list`")
    characters_status.add_argument("--json", action="store_true")
    characters_status.set_defaults(func=_cmd_characters_status)
    characters_thumb = characters_subs.add_parser("thumb", help="Write a card-size QA crop of ONE FRAME of one row attempt (chroma keyed out, NEAREST upscale on a flat dark backdrop) and return its path")
    characters_thumb.add_argument("--draft", required=True)
    # One crop verb, two QA item kinds — the same two budget booleans for both.
    # A direction reference used to have no crop verb at all, which left the
    # launcher's card drawing a tile through the whole turnaround stage for want
    # of an ANSWER, never for want of a safe picture.
    characters_thumb_item = characters_thumb.add_mutually_exclusive_group(required=True)
    characters_thumb_item.add_argument("--row", help="An authored row key, e.g. walk-n")
    characters_thumb_item.add_argument("--direction", default="", help="An authored direction, e.g. e — crops that direction's turnaround REFERENCE instead of a row strip. Mirrored directions are never drawn and are refused. A reference holds one pose, so --frame does not apply to it")
    characters_thumb.add_argument("--attempt", type=int, default=-1, help="Which attempt to crop, 0-based as in `status --json` history; -1 = latest")
    # No defaults spelled here: the numbers live in `draft.DEFAULT_THUMB_SCALE` /
    # `draft.DEFAULT_THUMB_FRAME` and are resolved in the handler, which is also
    # where charsheet is imported — build_parser runs for EVERY harness call and
    # must not pull in Pillow.
    characters_thumb.add_argument("--frame", type=int, default=None, help="Which frame cell of the strip to crop, 0-based (default 0); the crop is the half that removes pixels, so there is always one")
    characters_thumb.add_argument("--scale", type=int, default=None, help="NEAREST upscale factor (default 2); refused, never clamped. At or below the default the OUTPUT must fit the console's fixed decode ceiling; above it the crop is a fullscreen-viewer artifact, bounded by the write ceiling and reported as withinConsoleBudget=false. The payload carries a SECOND bound, withinOwnSheet — is the crop no larger than the sheet THIS draft composes — which refuses nothing and is reported at every scale. Draw a crop inline only when BOTH are true; otherwise open it in the viewer")
    characters_thumb.add_argument("--square", action="store_true", help="Pad the finished crop onto a square flat-dark backdrop (side = the longer edge, cell centred) so the console's 1:1 centre-cover hero card shows the WHOLE frame instead of a torso zoom. The filename gains -sq and the payload says square: true. Both budget booleans are weighed on the padded output. Use it for a hero card; take the bare crop for a compare pair, whose panes align on today's shapes")
    characters_thumb.add_argument("--json", action="store_true")
    characters_thumb.set_defaults(func=_cmd_characters_thumb)
    characters_base = characters_subs.add_parser("base", help="Set or replace the draft's base identity image")
    characters_base.add_argument("--draft", required=True)
    characters_base.add_argument("--image", required=True, help="Path to the identity-anchor image; copied into the draft")
    characters_base.add_argument("--json", action="store_true")
    characters_base.set_defaults(func=_cmd_characters_base)
    characters_turnaround = characters_subs.add_parser("turnaround", help="Generate the authored direction references (stage 'turnaround')")
    characters_turnaround.add_argument("--draft", required=True)
    characters_turnaround.add_argument("--json", action="store_true")
    characters_turnaround.set_defaults(func=_cmd_characters_turnaround)
    characters_reroll_direction = characters_subs.add_parser("reroll-direction", help="Re-generate ONE direction reference, with an optional operator note")
    characters_reroll_direction.add_argument("--draft", required=True)
    characters_reroll_direction.add_argument("--direction", required=True, help="An authored direction, e.g. ne (mirrored directions are never generated)")
    characters_reroll_direction.add_argument("--note", default="", help="Operator note appended to the prompt and stored with the attempt")
    characters_reroll_direction.add_argument("--json", action="store_true")
    characters_reroll_direction.set_defaults(func=_cmd_characters_reroll_direction)
    characters_approve_direction = characters_subs.add_parser("approve-direction", help="Approve direction references; advances to stage 'rows' once all are approved")
    characters_approve_direction.add_argument("--draft", required=True)
    characters_approve_direction_which = characters_approve_direction.add_mutually_exclusive_group(required=True)
    characters_approve_direction_which.add_argument("--direction", default="", help="Approve this one direction")
    characters_approve_direction_which.add_argument("--all", dest="approve_all", action="store_true", help="Approve the latest attempt of every authored direction")
    characters_approve_direction.add_argument("--attempt", type=int, default=-1, help="Which attempt to approve; -1 = latest (single-direction only)")
    characters_approve_direction.add_argument("--json", action="store_true")
    characters_approve_direction.set_defaults(func=_cmd_characters_approve_direction)
    characters_rows = characters_subs.add_parser("rows", help="Generate the animation row strips (stage 'rows')")
    characters_rows.add_argument("--draft", required=True)
    characters_rows.add_argument("--only", default="", help="Restrict the run to these row keys, e.g. 'walk-e,walk-ne'")
    characters_rows.add_argument("--json", action="store_true")
    characters_rows.set_defaults(func=_cmd_characters_rows)
    characters_reroll_row = characters_subs.add_parser("reroll-row", help="Re-generate ONE row strip, with an optional operator note")
    characters_reroll_row.add_argument("--draft", required=True)
    characters_reroll_row.add_argument("--row", required=True, help="An authored row key, e.g. walk-e")
    characters_reroll_row.add_argument("--note", default="", help="Operator note appended to the prompt and stored with the attempt")
    characters_reroll_row.add_argument("--json", action="store_true")
    characters_reroll_row.set_defaults(func=_cmd_characters_reroll_row)
    characters_compose = characters_subs.add_parser("compose", help="Compose, validate and install the sheet (stage 'rows' → 'composed')")
    characters_compose.add_argument("--draft", required=True)
    characters_compose.add_argument("--accept-handedness", default="", help="Mirrored-art REFUSALS you have looked at and are overriding, spelled '<row>:<basis>' — take the spelling from the refusal. Per row, never blanket. TWO shapes refuse and both can be accepted: a row BOTH passes agree about ('idle-e:rotation+states'), and a row carried by a whole mirrored STATE, where every judged row of that state reads as a mirror ('jumping-e:states') — that one is accepted row by row like any other. A single-basis finding about a single row is a warning and there is nothing to accept. The basis is named on purpose: a bare row key waived a second, independent body of evidence at once. Naming a row that was not flagged is itself refused, and the honoured list rides on the installed manifest, 'characters list' and the sprite payload as {row, gain, basis}")
    characters_compose.add_argument("--json", action="store_true")
    characters_compose.set_defaults(func=_cmd_characters_compose)
    characters_auto = characters_subs.add_parser("auto", help="Drive the whole pipeline in ONE process — turnaround, approve every direction, generate the missing rows, compose and install — printing a receipt line as each stage lands. For an operator's EXPLICIT 'drive it all the way' ask and nothing else: it auto-approves the turnaround, which is the last moment a reference can change. It never overrides a handedness refusal (there is no --accept-handedness here) and it writes the same per-attempt history the interactive verbs write, so `reopen` repair and every QA crop work exactly as they do after a hand-driven run. It resumes rather than restarts: a stage whose work already exists is skipped, with the reason on the summary line, so running it after `reopen` regenerates the missing rows instead of discarding the approved ones. Output is newline-delimited — with --json every line is ONE compact object, and the LAST line is always the summary")
    characters_auto.add_argument("--draft", required=True)
    characters_auto.add_argument("--through", default="compose", choices=list(_CHARACTERS_AUTO_STEPS), help="Last step to run (default: compose, the whole pipeline). Steps before it that the draft already carries are skipped and reported")
    characters_auto.add_argument("--json", action="store_true")
    characters_auto.set_defaults(func=_cmd_characters_auto)
    characters_reopen = characters_subs.add_parser("reopen", help="Reopen a composed draft for fixes (stage 'composed' → 'rows'); the installed sheet stays until the next compose")
    characters_reopen.add_argument("--draft", required=True)
    characters_reopen.add_argument("--json", action="store_true")
    characters_reopen.set_defaults(func=_cmd_characters_reopen)
    characters_add_state = characters_subs.add_parser("add-state", help="Add ONE animation state to a draft at stage 'rows' (reopen a composed draft first); the new rows start un-generated and no approved row is touched")
    characters_add_state.add_argument("--draft", required=True)
    characters_add_state.add_argument("--state", required=True, help="One state in the --states grammar: 'jumping:6' or 'cheer:4:fixed'. Frames 2..8 — a one-frame row is refused HERE rather than several generations later at 'rows'")
    characters_add_state.add_argument("--json", action="store_true")
    characters_add_state.set_defaults(func=_cmd_characters_add_state)
    characters_sprite = characters_subs.add_parser("sprite", help="Return an installed character spritesheet payload")
    characters_sprite.add_argument("slug")
    characters_sprite.add_argument("--no-sheet", dest="no_sheet", action="store_true", help="Metadata only: drop `spritesheetBase64` (468.8 KiB of it on the live 3-state character, and the sheet bytes are not read at all) and carry `sheet`, the absolute path, in its place. `spritesheetRevision` and every geometry/taxonomy key are unchanged, so a consumer that wants framesByRow/states/rows and reads the file itself pays kilobytes instead of half a megabyte. The default is byte-identical to what it always was")
    characters_sprite.add_argument("--json", action="store_true")
    characters_sprite.set_defaults(func=_cmd_characters_sprite)
    characters_payload_contract = characters_subs.add_parser("payload-contract", help="Publish the KEY SET every `characters` READ payload can carry — the cross-repo contract the launcher commits and diffs, so the two sides disagree in a file instead of at runtime. Derived by RUNNING the verbs against a throwaway library in a temp directory (the real library is never touched), never from a hand-written list, so a key a producer grows or drops is in the dump the day it moves. Key PATHS only and never a value, so the dump is byte-stable. Conditional keys are marked with the modes that carry them — `spritesheetBase64` and `sheet` are one slot spelled two ways, which a flat key list cannot express")
    characters_payload_contract.add_argument("--json", action="store_true")
    characters_payload_contract.set_defaults(func=_cmd_characters_payload_contract)


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


# ── remote-gateway install identity (Stage 0b) ───────────────────────────────
#
# The operator's door onto `agent_runtime/gateway_identity.py`, which is the
# service half Stage 0a shipped and tested. These two handlers hold no rule of
# their own: WHAT a name may be, WHERE the record lives, and WHEN a record is
# minted are all decided there, because the greeting frame
# (`harness_parts/serve.py`, the `install` block) reads the same module and two
# answers to "what is this install called" is the whole failure this lane exists
# to prevent.
#
# **No authorization gate, and that is a decision rather than an omission.**
# The A4 mirror (`persona_commands._console_denial`) exists so the CLI and
# `serve_rpc.handle_request` cannot answer differently about ONE service
# function — `perform_agent_create` / `perform_agent_retire` each have two
# doors. Stage 0b adds no RPC method, so there is one door and nothing to
# disagree with; a `CLI_CONSOLE` check here would gate a door against a
# predicate that allows every caller that exists, with no wire twin to stay
# honest against. The record is also not a level and not a secret. The day a
# paired DEVICE may rename an install, the door it comes through is a
# `gateway.*` method with a tier declaration (gateway plan Stage 1 / A5), and
# the gate goes there — where the caller is something the transport proved
# rather than the machine owner's own shell.


def _gateway_install_row(identity) -> dict:
    """One `gateway id` / `gateway rename` row.

    ``state`` is carried even on the success path, for the reason the frame
    block carries it: ``loaded`` and ``minted`` are different facts about the
    same root, and "minted" is how an operator learns this call is what created
    the identity rather than reading one that was already there.
    """

    row = {
        "install_id": identity.install_id,
        "display_name": identity.display_name,
        "state": identity.state,
        "created_at": identity.created_at,
        "path": identity.path,
    }
    # S2 (R-S2-1 / R-S2-2). Four facts a caller currently has to open a socket
    # to learn, answered by a cold CLI process:
    #
    # * ``capabilities`` — what this BUILD can do, so S3's request loop can
    #   feature-detect over the loopback argv lane without a listener anywhere.
    #   The same tuple the ``gateway`` block stamps on every greeting, read from
    #   the one module both sides import.
    # * ``endpoints`` / ``endpoints_source`` — where another machine should dial
    #   this one, and how confident that answer is. The SAME list a join payload
    #   advertises (``_candidate_endpoints``), so what this prints and what a
    #   hello offers cannot drift.
    # * ``listener`` — the live block minus nothing secret: an outcome, a host,
    #   a port and the fingerprint a client pins. The private key is not in it
    #   and there is no field for one.
    #
    # Best-effort as a whole: this verb is read-only by contract and Stage 4's
    # install picker runs it against roots it does not own, so a store it cannot
    # read degrades to the identity row rather than to an error.
    # * ``dial_host`` — the ONE address every payload writer hands out (R-D1),
    #   which is ``endpoints[0]`` and ``null`` when there is none. Printed as its
    #   own key rather than left for a reader to slice off the list, because the
    #   launcher renders it as a sentence and as a row label (R-D4: `Windows PC
    #   (192.168.1.203)`, `Listening on 192.168.1.203:8765 · all interfaces`) and
    #   two surfaces computing "the first one" independently is how a sheet ends
    #   up naming an address no payload contains. It is deliberately NOT the
    #   ``listener`` block: that one still reports the BIND, because "what is
    #   this listener on" and "what should another machine dial" are different
    #   questions and a wildcard is the honest answer to the first.
    try:
        from agent_runtime import paths
        from agent_runtime.gateway_capabilities import GATEWAY_CAPABILITIES
        from hermes_cli.harness_parts.gateway_commands import (
            _candidate_endpoints,
            _dial_host,
            _endpoint,
        )

        root = paths.store_root()
        endpoint = _endpoint(root)
        row["endpoints"] = _candidate_endpoints(root)
        row["endpoints_source"] = endpoint["source"]
        row["listener"] = {
            "host": endpoint.get("host"),
            "port": endpoint.get("port"),
            "source": endpoint["source"],
        }
        # The list this row already holds, not a second enumeration of it: since
        # D1b that walk reads the routing table, which is a process spawn.
        dial = _dial_host(row["endpoints"])
        row["dial_host"] = {"host": dial[0], "port": dial[1]} if dial else None
        row["capabilities"] = list(GATEWAY_CAPABILITIES)
    except Exception:
        row.setdefault("endpoints", [])
        row.setdefault("endpoints_source", "unknown")
        row.setdefault("listener", {"host": None, "port": None, "source": "unknown"})
        row.setdefault("dial_host", None)
        from agent_runtime.gateway_capabilities import GATEWAY_CAPABILITIES

        row.setdefault("capabilities", list(GATEWAY_CAPABILITIES))
    return row


#: ``error:<reason>`` → the harness error taxonomy. Split on the operator's next
#: MOVE, which is what the exit families mean:
#:
#: * ``absent`` — nothing to show (3). Not an infrastructure fault: a root that
#:   has never run a serve genuinely has no identity, and `gateway id` is
#:   read-only by contract, so it reports that instead of minting one behind an
#:   operator who only asked.
#: * ``malformed_record`` / ``record_without_id`` — the file exists and will not
#:   decode (1). Deliberately NOT a re-mint, per the asymmetry
#:   `gateway_identity._decode` documents: a paired device may still name the id
#:   in those bytes, and overwriting them to make a verb look tidy destroys the
#:   only copy of the join key.
#: * everything else is an I/O condition on the root — retryable in exactly the
#:   sense family 7 already means (an AV hold releases, an operator fixes a
#:   permission, the identical call then succeeds).
_GATEWAY_IDENTITY_ERROR_CODES = {
    "absent": "not_found",
    "malformed_record": "store_corrupt",
    "record_without_id": "store_corrupt",
    "empty_display_name": "invalid_payload",
}


def _gateway_identity_error(identity, *, args) -> int:
    reason = identity.state.split(":", 1)[1] if ":" in identity.state else identity.state
    code = _GATEWAY_IDENTITY_ERROR_CODES.get(reason, "runtime_unavailable")
    if reason == "absent":
        message = (
            f"{identity.path} does not exist: this runtime root has no gateway "
            "install identity yet. One is minted by the first `harness serve` "
            "against this root, or by `harness gateway rename <name>`."
        )
    else:
        # The typed reason travels verbatim. It is the same vocabulary the
        # `install` block puts on `ready`/`hello_ok`/`version`, so an operator
        # comparing a frame against a verb reads one word, not two spellings.
        message = f"{identity.path}: install identity is {identity.state}"
    return emit_harness_error(
        RuntimeError(identity.state), args=args, code=code, message=message
    )


def _cmd_gateway_id(args) -> int:
    """`harness gateway id` — which install is this, read WITHOUT minting one.

    The read-only half on purpose: a probe that mints leaves a side effect on a
    root it was only asked about, and Stage 4's install picker will run this
    against roots it does not own.
    """

    from agent_runtime import paths
    from agent_runtime.gateway_identity import read_install_identity

    identity = read_install_identity(paths.store_root())
    if not identity.ok:
        return _gateway_identity_error(identity, args=args)
    # The identity is PER STORE ROOT, so "which root answered" is not decoration
    # here — it is the other half of the answer. A `gateway id` run against the
    # wrong root returns a perfectly well-formed identity for a runtime the
    # operator did not mean (the 2026-08-12 incident's shape, with an id in it).
    envelope = attach_root_observability(
        _object_envelope("gateway_install", _gateway_install_row(identity))
    )
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def _cmd_gateway_rename(args) -> int:
    """`harness gateway rename <name>` — name this install; keep its id.

    Mints when the root has no record yet (``set_display_name``'s contract), so
    an operator can name an install before anything has ever booted against it.
    ``--dry-run`` therefore does NOT refuse an absent record — a real run would
    have succeeded there — but it does refuse an undecodable one, because a real
    run would refuse that too.
    """

    from agent_runtime import paths
    from agent_runtime.gateway_identity import (
        clean_display_name,
        read_install_identity,
        set_display_name,
    )

    dry_run = bool(getattr(args, "dry_run", False))
    # Asked here rather than only inside the service because `--dry-run` must
    # answer the same way as a real run, and a preview cannot ask a writer it is
    # not allowed to call. Same authority function either way — not a second
    # copy of the rule.
    cleaned = clean_display_name(args.name)
    if not cleaned:
        return emit_harness_error(
            ValueError(str(args.name)),
            args=args,
            code="invalid_payload",
            message=(
                "A display name must contain at least one printable character. "
                "It is chrome for a picker row, not an identifier — clearing it "
                "would leave the install with nothing to show, so the rename is "
                "refused rather than applied as blank."
            ),
        )

    root = paths.store_root()
    if dry_run:
        identity = read_install_identity(root)
        if not identity.ok and not identity.state.endswith(":absent"):
            return _gateway_identity_error(identity, args=args)
        row = _gateway_install_row(identity)
        # The name the WRITE would land — normalised by the module's own rule,
        # never the raw argument.
        row["display_name"] = cleaned
    else:
        identity = set_display_name(root, args.name)
        if not identity.ok:
            return _gateway_identity_error(identity, args=args)
        row = _gateway_install_row(identity)
        # S2c: tell every paired install what we are called now. A display name
        # is CACHE at the far end (`gateway_peers`' split) — the install itself
        # is the authority for its own name — so a rename that nobody announced
        # left every peer showing the old one until its next hello, which for an
        # install that is rarely dialled could be days. Best-effort and
        # off-thread; a rename does not wait on a LAN.
        try:
            from agent_runtime.gateway_announce import announce_in_background

            announce_in_background(
                root, {"display_name": identity.display_name}
            )
        except Exception:  # noqa: BLE001 — courtesy channel, never the write
            pass

    envelope = attach_root_observability(_object_envelope("gateway_install", row))
    if dry_run:
        envelope["dry_run"] = True
    _print_stage42(envelope, args=args, default_output="json")
    return 0


# Stage 1's three live in `harness_parts/gateway_commands.py` rather than here.
# Not for length: the credential wiring is the part that must not be got wrong,
# and it reads better next to a module docstring that can carry R3's ruling and
# the no-authorization-gate argument than inline in a 5000-line parser file.
# Imported lazily, the way `serve` already is, so building the parser does not
# pull in the certificate and device-store modules on every `harness --help`.


def _cmd_gateway_pair(args) -> int:
    from hermes_cli.harness_parts.gateway_commands import cmd_gateway_pair

    return cmd_gateway_pair(args)


def _cmd_gateway_introduce(args) -> int:
    from hermes_cli.harness_parts.gateway_commands import cmd_gateway_introduce

    return cmd_gateway_introduce(args)


def _cmd_gateway_devices_list(args) -> int:
    from hermes_cli.harness_parts.gateway_commands import cmd_gateway_devices_list

    return cmd_gateway_devices_list(args)


def _cmd_gateway_devices_revoke(args) -> int:
    from hermes_cli.harness_parts.gateway_commands import cmd_gateway_devices_revoke

    return cmd_gateway_devices_revoke(args)


def _cmd_gateway_peers_pair(args) -> int:
    from hermes_cli.harness_parts.gateway_commands import cmd_gateway_peers_pair

    return cmd_gateway_peers_pair(args)


def _cmd_gateway_peers_join(args) -> int:
    from hermes_cli.harness_parts.gateway_commands import cmd_gateway_peers_join

    return cmd_gateway_peers_join(args)


def _cmd_gateway_peers_list(args) -> int:
    from hermes_cli.harness_parts.gateway_commands import cmd_gateway_peers_list

    return cmd_gateway_peers_list(args)


def _cmd_gateway_peers_revoke(args) -> int:
    from hermes_cli.harness_parts.gateway_commands import cmd_gateway_peers_revoke

    return cmd_gateway_peers_revoke(args)


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
    config_paths = _machine_root_config_paths(list_flag_or_empty(args, "configs"))
    if not config_paths:
        return emit_harness_error(
            NotFound("config.yaml"), args=args, message="No profile config.yaml files found to migrate."
        )

    explicit: dict[str, str] = {}
    for item in list_flag_or_empty(args, "root"):
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


# ── skills delete / restore (canon: docs/agent-runtime-harness/01-system-architecture.md §Skills) ──


def _canonical_packages_covered(slug: str) -> list[tuple[str, Path]]:
    """Every canonical package a tombstone on ``slug`` would cover.

    ONE match rule (``store.skill_tombstone_matches``), asked in the direction
    this verb has to ask it: holding a CANDIDATE entry slug, before any ledger
    exists to hand ``skill_tombstoned``. Plural by construction — a bare-name
    entry covers a categorized ``<cat>/<child>`` package of the same child name,
    so ``foo`` can name both a top-level ``foo`` and a ``bar/foo``.
    """

    from agent_runtime.skill_promotion import iter_skill_packages
    from agent_runtime.store import skill_tombstone_matches
    from hermes_constants import get_shared_skills_dir

    return [
        (pkg_slug, pkg_dir)
        for pkg_slug, pkg_dir in iter_skill_packages(get_shared_skills_dir())
        if skill_tombstone_matches(slug, pkg_slug)
    ]


def _realm_publishes_skill(realm, slug: str, covered_slugs: list[str]) -> bool:
    """Does this realm CURRENTLY publish ``slug``? (R-E's default target set.)

    Answered the way ``_skill_artifacts`` answers it, because that is what
    "currently publishes" means on this machine:

    - mode ``all`` publishes whatever the canonical root holds, so the realm
      publishes the slug exactly when a local package is covered by it;
    - mode ``selected`` publishes what the selection NAMES, which is a standing
      statement about the name and holds even with no local copy.

    A realm whose ledger already carries the slug is a target too: a repeat
    delete refreshes ``deleted_at`` rather than quietly skipping a realm the
    operator would then believe was left alone.

    With no local package the selection entry is only a NAME, and a name cannot
    say whether ``foo`` and ``cat/foo`` are the same package — so that arm
    accepts the match in EITHER direction rather than silently missing a realm.
    Where packages exist, the packages decide.
    """

    from agent_runtime.store import skill_tombstone_matches, skill_tombstoned

    if skill_tombstoned(realm, slug) is not None:
        return True
    if realm.skill_publish_mode == "selected":
        selection = realm.skill_selection or []
        if covered_slugs:
            # The selection rule and the tombstone rule are ONE rule by design
            # (see store.skill_tombstone_matches): a slug must not be able to be
            # simultaneously "selected" and "not the thing that was deleted".
            return any(
                skill_tombstone_matches(entry, pkg_slug)
                for entry in selection
                for pkg_slug in covered_slugs
            )
        return any(
            skill_tombstone_matches(slug, entry) or skill_tombstone_matches(entry, slug)
            for entry in selection
        )
    return bool(covered_slugs)


def _prune_inbox_packages(realm_id: str, slug: str) -> list[str]:
    """Drop this realm's inbox-mirror copies that a tombstone on ``slug`` covers.

    UNLINKED, not archived — deliberately, and it is not a breach of the skills
    lane's never-delete invariant. That invariant protects authored content; the
    inbox is a byte-faithful CACHE of what the realm publishes, rebuilt from the
    subtree on every pull, and ``_mirror_realm_skill_inbox`` already unlinks a
    package the realm stopped publishing. The canonical copy — the authored one
    — is what the delete archives.

    Without this step the operator would delete a skill and still see it in
    ``skills inbox`` as a promotable package until the next pull.
    """

    import shutil

    from agent_runtime.skill_promotion import iter_skill_packages, realm_inbox_dir
    from agent_runtime.store import skill_tombstone_matches

    inbox = realm_inbox_dir(realm_id)
    pruned: list[str] = []
    for pkg_slug, pkg_dir in list(iter_skill_packages(inbox)):
        if not skill_tombstone_matches(slug, pkg_slug):
            continue
        shutil.rmtree(pkg_dir, ignore_errors=True)
        parent = pkg_dir.parent
        # A category dir left empty by the prune is removed too, so the mirror
        # does not accumulate hollow shells the next `iter_skill_packages` walk
        # has to step over.
        if parent != inbox and parent.is_dir() and not any(parent.iterdir()):
            try:
                parent.rmdir()
            except OSError:
                pass
        pruned.append(pkg_slug)
    return sorted(pruned)


_ARCHIVE_STAMP_SUFFIX_RE = re.compile(r"-\d+$")


def _archive_content_hint(slug: str) -> dict:
    """Where the BYTES of a deleted skill are, and the command that re-admits them.

    ``skills restore`` lifts a ledger entry and nothing else (R-C) — content is
    the promotion lane's job. This hint is what keeps that split from being a
    dead end: it names the newest ``.archive/<UTC ts>/<flat>`` copy (archive
    stamp dirs sort chronologically because they are UTC timestamps) and the
    exact promote command for it, paths rendered relative to the hermes root the
    way the plan writes them.

    ``candidates`` is honest about repeats: deleting and re-promoting the same
    slug leaves several archived copies, and the newest is a choice, not a fact.
    """

    from hermes_constants import get_default_hermes_root, get_shared_skills_dir

    shared = get_shared_skills_dir()
    flat = slug.replace("/", "__")
    archive_root = shared / ".archive"
    matches: list[Path] = []
    if archive_root.is_dir():
        for stamp in sorted(p for p in archive_root.iterdir() if p.is_dir()):
            for candidate in sorted(p for p in stamp.iterdir() if p.is_dir()):
                name = candidate.name
                # ``_archive_dir``'s collision breaker appends ``-<n>``; a real
                # skill literally named ``<flat>-1`` would also match here, which
                # is why ``candidates`` is reported rather than one path claimed
                # as the only answer.
                if name != flat and _ARCHIVE_STAMP_SUFFIX_RE.sub("", name) != flat:
                    continue
                if (candidate / "SKILL.md").is_file():
                    matches.append(candidate)
    newest = matches[-1] if matches else None
    rel = None
    if newest is not None:
        # Rendered relative to the hermes ROOT (the plan's own spelling,
        # ``shared/skills/.archive/<ts>/<slug>``) and never absolute — the error
        # contract forbids leaking the runtime root. A HERMES_SHARED_SKILLS
        # override moves the shared dir out from under the root, and then the
        # shared-root-relative path is the only honest answer available.
        try:
            rel = newest.relative_to(get_default_hermes_root()).as_posix()
        except ValueError:
            rel = newest.relative_to(shared).as_posix()
    return {
        "archived": newest is not None,
        "path": rel,
        "candidates": len(matches),
        "promote_command": (
            f"hermes harness skills promote {slug} --from-path {rel}" if rel else None
        ),
    }


def _cmd_skills_delete(args) -> int:
    """Delete a shared skill realm-wide: archive the local canonical package and
    record the tombstone git absence cannot carry (plan §4).

    The bytes really are deleted everywhere — publish is a full-subtree replace,
    so the propagating push removes the file and every member's pull removes it
    from their clone. What git cannot reach is the ADOPTED copy in each member's
    canonical skills root, which is not a git file and which rides that member's
    next publish straight back into the realm. The ledger is that instruction.
    """

    from agent.skill_utils import skill_package_content_hash
    from agent_runtime.errors import SkillTombstoneRefused
    from agent_runtime.skill_promotion import _archive_package, validate_skill_slug
    from agent_runtime.store import active_skill_tombstones, skill_tombstoned
    from hermes_constants import CANONICAL_SHARED_SKILL_IDS

    slug = str(getattr(args, "skill", "") or "").strip()
    dry_run = bool(getattr(args, "dry_run", False))
    requested: list[str] = []
    for value in list_flag_or_empty(args, "realms"):
        token = str(value or "").strip()
        if token and token not in requested:
            requested.append(token)

    # Refuse BEFORE anything is written, reading the SAME two authorities the
    # store chokepoint reads (``validate_skill_slug`` and the constant) rather
    # than re-spelling either. This is not the fence — ``tombstone_skill``
    # refuses on its own — it is the fence answering before N realms have been
    # written and before the archive step has moved bytes.
    reason = validate_skill_slug(slug)
    if reason is not None:
        _print_stage42(
            _error_envelope(
                "skill_slug_invalid",
                f"{slug!r} is not a valid skill slug: {reason}",
                safe_details={"skill": slug},
            ),
            args=args,
            default_output="json",
        )
        return ERROR_EXIT_CODES["skill_slug_invalid"]
    if slug in CANONICAL_SHARED_SKILL_IDS:
        _print_stage42(
            _error_envelope(
                "skill_installer_owned",
                (
                    f"{slug!r} is a hermes-installed harness skill: every realm pull "
                    "reinstalls it from repo source, so a realm tombstone can never "
                    "hold. Delete it from hermes_constants.CANONICAL_SHARED_SKILL_IDS "
                    "and docs/agent-runtime-harness/harness-skills/ instead."
                ),
                safe_details={"skill": slug},
            ),
            args=args,
            default_output="json",
        )
        return ERROR_EXIT_CODES["skill_installer_owned"]

    store = RealmStore()
    covered = _canonical_packages_covered(slug)
    covered_slugs = [pkg_slug for pkg_slug, _pkg_dir in covered]

    if requested:
        # An explicitly named realm is honored even when it does not currently
        # publish the slug (and even when archived): the operator named it, and a
        # tombstone records INTENT.
        realms = []
        for realm_id in requested:
            try:
                realms.append(store.get(realm_id))
            except NotFound as exc:
                return emit_harness_error(exc, args=args, code="not_found")
    else:
        realms = [
            realm
            for realm in store.list_all()
            if _realm_publishes_skill(realm, slug, covered_slugs)
        ]

    hashes: dict[str, str | None] = {}
    for pkg_slug, pkg_dir in covered:
        try:
            hashes[pkg_slug] = skill_package_content_hash(pkg_dir, pkg_dir / "SKILL.md")
        except Exception:  # noqa: BLE001 — evidence, never a reason to refuse
            hashes[pkg_slug] = None
    deleted_hash = hashes.get(covered_slugs[0]) if covered_slugs else None

    warnings: list[dict] = []
    realm_rows: list[dict] = []
    for realm in realms:
        before = set(realm.skill_selection or [])
        # ``refreshed`` asks for the EXACT entry, because that is the entry
        # ``tombstone_skill`` dedupes against (a bare-name entry that merely
        # COVERS this slug is a different record and is not replaced). It asks
        # the ACTIVE ledger: since RD-11 a restored entry stays on the register,
        # and re-deleting a slug someone lifted is a fresh delete, not a refresh
        # of a block that was standing.
        refreshed = any(entry.slug == slug for entry in active_skill_tombstones(realm))
        try:
            updated = store.tombstone_skill(
                realm.id, slug, deleted_hash=deleted_hash, dry_run=dry_run
            )
        except SkillTombstoneRefused as exc:
            _print_stage42(
                _error_envelope(
                    exc.code,
                    str(exc),
                    safe_details={**exc.safe_details, "realm_id": realm.id},
                ),
                args=args,
                default_output="json",
            )
            return ERROR_EXIT_CODES.get(exc.code, 1)
        realm_rows.append(
            {
                "realm_id": realm.id,
                "tombstoned": True,
                # A LIST, not a bool: the plan's row shape left the type open and
                # a bool cannot say WHICH selection entry a bare-name tombstone
                # took (R-F prunes through the same match rule).
                "selection_pruned": sorted(before - set(updated.skill_selection or [])),
                "refreshed": refreshed,
                "inbox_pruned": [],
            }
        )

    archived_rows: list[dict] = []
    for pkg_slug, pkg_dir in covered:
        if dry_run:
            archived_rows.append(
                {"slug": pkg_slug, "archived_to": None, "deleted_hash": hashes.get(pkg_slug)}
            )
            continue
        try:
            dest = _archive_package(pkg_dir, pkg_slug)
        except Exception as exc:  # noqa: BLE001 — accounted, never silent
            warnings.append(
                {"code": "skill_archive_failed", "skill": pkg_slug, "message": str(exc)}
            )
            continue
        archived_rows.append(
            {
                "slug": pkg_slug,
                "archived_to": _rel_to_shared_skills(dest),
                "deleted_hash": hashes.get(pkg_slug),
            }
        )

    if not dry_run:
        for row, realm in zip(realm_rows, realms):
            row["inbox_pruned"] = _prune_inbox_packages(realm.id, slug)

    if not covered and not realm_rows:
        warnings.append(
            {
                "code": "skill_unknown",
                "skill": slug,
                "message": (
                    "No canonical package here and no non-archived realm currently "
                    "publishing it — nothing was written. A tombstone records INTENT "
                    "and is valid without a local copy: name the realm explicitly "
                    "with --realm to record one anyway."
                ),
            }
        )
    elif not covered:
        warnings.append(
            {
                "code": "skill_no_local_package",
                "skill": slug,
                "message": (
                    "Tombstone recorded, nothing archived: no canonical package for "
                    "this slug exists on this machine. The intent still travels and "
                    "still blocks members who do hold one."
                ),
            }
        )

    if len(realm_rows) == 1:
        next_step = (
            f"hermes harness realm sync publish {realm_rows[0]['realm_id']} to propagate"
        )
    else:
        # Zero or many targets: the placeholder is the honest rendering — the
        # publish has to be run once per realm the receipt lists.
        next_step = "hermes harness realm sync publish <realm> to propagate"

    payload = {
        "skill": slug,
        "realms": realm_rows,
        # ``archived`` is the truth and the two scalars below are the §4
        # single-package convenience: the match rule is one-to-many by
        # construction, so a lone ``archived_to`` cannot describe a bare-name
        # delete that covered a top-level AND a categorized package.
        "archived": archived_rows,
        "archived_to": archived_rows[0]["archived_to"] if archived_rows else None,
        "deleted_hash": deleted_hash,
        "next": next_step,
    }
    if dry_run:
        payload["dry_run"] = True
    # This lane is the root-observability gate's own defect class, exactly: a
    # delete resolved against the WRONG shared root finds no package, resolves
    # no publishing realm, and reports a well-formed ``skill_unknown`` — the
    # operator reads "already gone" from a verb that never looked in the right
    # place. The envelope has to say which root answered.
    envelope = attach_root_observability(
        _object_envelope("skill_delete", payload, warnings=warnings or None)
    )
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def _cmd_skills_restore(args) -> int:
    """Lift ONE realm's skill tombstone. Un-tombstone only (R-C).

    The store returns the realm, not a ``restored`` flag, so the answer is taken
    BEFORE the write: an ACTIVE entry with exactly this slug is what
    ``restore_skill`` lifts, and an idempotent no-op — an absent entry, or one
    already lifted — must report ``restored: false`` rather than claim a change
    it did not make.
    """

    from agent_runtime.store import active_skill_tombstones, skill_tombstoned

    slug = str(getattr(args, "skill", "") or "").strip()
    realm_id = str(getattr(args, "realm", "") or "").strip()
    if not slug or not realm_id:
        return emit_harness_error(
            ValueError("a skill slug and --realm <id> are both required"),
            args=args,
            code="invalid_request",
        )

    store = RealmStore()
    try:
        realm = store.get(realm_id)
    except NotFound as exc:
        return emit_harness_error(exc, args=args, code="not_found")

    had_entry = any(entry.slug == slug for entry in active_skill_tombstones(realm))
    updated = store.restore_skill(realm_id, slug)

    warnings: list[dict] = []
    blocking = skill_tombstoned(updated, slug)
    if blocking is not None:
        # A categorized package can be blocked by a BARE-name entry; lifting
        # ``cat/child`` leaves ``child`` standing, and a receipt that said
        # "restored" without saying so would be a lie of omission.
        warnings.append(
            {
                "code": "skill_still_tombstoned",
                "skill": slug,
                "blocking_slug": blocking.slug,
                "message": (
                    f"Ledger entry {blocking.slug!r} still covers this slug — restore "
                    "that entry to un-block it."
                ),
            }
        )
    if not had_entry:
        warnings.append(
            {
                "code": "skill_not_tombstoned",
                "skill": slug,
                "message": "No ledger entry with this exact slug in that realm; nothing was written.",
            }
        )

    payload = {
        "skill": slug,
        "realm_id": updated.id,
        "restored": had_entry,
        "tombstones": skill_tombstone_rows(updated),
        # R-C's other half: the entry is lifted, the BYTES are the promotion
        # lane's job, and this names the copy the delete archived so the
        # two-step is a command to run rather than a data-recovery hunt.
        "content_hint": _archive_content_hint(slug),
    }
    # Same reason as the delete: ``content_hint.archived: false`` is a
    # well-formed empty answer, and a wrong shared root produces it just as
    # readily as a genuinely-unarchived slug does.
    envelope = attach_root_observability(
        _object_envelope("skill_restore", payload, warnings=warnings or None)
    )
    _print_stage42(envelope, args=args, default_output="json")
    return 0


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


#: MOVED to `agent_runtime.scope_activation` by plan WS4, and imported back
#: under its old private name so every call site below is unchanged. The reason
#: is the second door: `runtime.workspace.use` answers with THIS row, and a row
#: the method lane could only reach by re-deriving would be the S48 twin all
#: over again. It is a re-key of `agent_runtime.snapshot._workspace_summary`, so
#: agent_runtime is where it always belonged.
_workspace_row = _scope_workspace_row


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
        row = {"id": f"ws_dry_{uuid.uuid4().hex[:6]}", "name": args.name, "realm_id": args.realm, "agents": len(list_flag_or_empty(args, "agent")), "goals": 0, "isolation": args.isolation or "soft", "updated_at": now()}
        if template is not None:
            row["template_workspace_id"] = template.id
            row["copy_scopes"] = list(scopes)
        _print_stage42(_object_envelope("workspace", row), args=args, default_output="json")
        return 0
    if args.realm:
        RealmStore().get(args.realm)
    # Template settings/roster feed the create itself; explicit flags always
    # win over the template so the operator can override any copied field.
    agent_ids = list_flag_or_empty(args, "agent")
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
    # The DECISION is `agent_runtime.scope_activation`'s, not this handler's —
    # `runtime.workspace.use` reaches the same function, so the argv verb and
    # the method lane cannot drift about what `applied` / `superseded` /
    # `duplicate` mean (plan WS4). All that is left here is the envelope.
    row = activate_workspace(args.workspace_id, issued_at=getattr(args, "issued_at", None))
    _print_stage42(_object_envelope("workspace", row), args=args, default_output="json")
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


#: MOVED to `agent_runtime.scope_activation` with `_workspace_row`, for the same
#: reason and in the same landing — see the note there.
_realm_row = _scope_realm_row


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
    # Same shared decision as `_cmd_workspace_use`, reconcile included — see
    # `agent_runtime.scope_activation.activate_realm` for why the workspace
    # reconcile lives inside the shared function rather than at each door.
    row = activate_realm(args.realm_id, issued_at=getattr(args, "issued_at", None))
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


#: MOVED to `agent_runtime.scope_activation` (plan WS4): `activate_realm` calls
#: it, so the reconcile happens on BOTH doors by construction rather than
#: because each door remembered. `_cmd_workspace_delete` still calls it
#: directly — a delete of the active workspace reconciles for the same reason a
#: realm switch does, and that is not an activation.
_reconcile_active_workspace_to_realm = _scope_reconcile_active_workspace_to_realm


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
    except NotFound as exc:
        # An unknown realm id is an ARGUMENT error, not a crash. Without this the
        # store's NotFound escaped the handler uncaught, and the operator got a
        # traceback whose message is the ABSOLUTE PATH of the realm JSON — the
        # one thing the error contract forbids on an operator-visible surface.
        # The sibling verbs that read a realm by id already catch it exactly
        # here (``_cmd_realm_skill_restore``); this one did not, and the response
        # fixture for the case is what made that visible.
        return emit_harness_error(exc, args=args, code="not_found")
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


def _cmd_realm_sync_revert(args) -> int:
    """`realm sync revert` — the SECOND exit from unpublished local changes.

    Gated on ``--yes`` like publish/resolve: it is destructive of LOCAL state
    (archive-never-delete, so recoverable, but the operator still has to mean
    it). Local-only — it takes no ``--credential-file``, because it never
    reaches the remote; the upstream it reverts to is the subtree the last pull
    already put on disk.
    """

    from agent_runtime.realm_revert import revert_realm_sync

    if not _require_yes(args):
        return 8
    dry_run = bool(getattr(args, "dry_run", False))
    try:
        data = revert_realm_sync(
            args.realm_id,
            item_specs=list_flag_or_empty(args, "items"),
            revert_all=bool(getattr(args, "revert_all", False)),
            dry_run=dry_run,
        )
    except RealmSyncError as exc:
        return emit_harness_error(exc, args=args)
    envelope = attach_root_observability(_object_envelope("realm_sync_revert", data))
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def _realm_skill_selection_envelope(realm) -> dict:
    """The realm_skill_selection/v1 envelope (design §5): current mode +
    selection, the shared-catalog slugs on THIS machine, and the honest
    ``missing`` accounting (selection − catalog).

    ``tombstones`` is ADDITIVE (realm skill-delete §4) and is the same row shape
    the sync status envelope and the sidecar carry — one ledger, one rendering.
    It belongs beside the selection because the two answer the one question an
    operator actually asks here: a slug absent from ``selection`` is merely
    unpublished HERE, while a slug in ``tombstones`` is deleted EVERYWHERE."""
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
        "tombstones": skill_tombstone_rows(realm),
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
                skill_sources = persona_skill_sources(cfg)
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
                rows.append(
                    _agent_definition_row(
                        persona,
                        source_profile=profile.name,
                        bindings=bindings,
                        roster=personas,
                        skill_sources=skill_sources,
                    )
                )
    else:
        cfg = load_agent_runtime_config()
        try:
            bindings = binding_index(cfg)
        except Exception:
            bindings = {}
        personas = ensure_persisted_personas(cfg)
        skill_sources = persona_skill_sources(cfg)
        for persona in personas:
            rows.append(
                _agent_definition_row(
                    persona,
                    source_profile=active_profile_name(),
                    bindings=bindings,
                    roster=personas,
                    skill_sources=skill_sources,
                )
            )
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


def _agent_definition_row(
    persona: AgentPersona,
    *,
    source_profile: str | None,
    bindings: dict | None = None,
    roster: Sequence[AgentPersona] | None = None,
    skill_sources: dict | None = None,
) -> dict:
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

    ``persona_spellings`` and ``skills`` (2026-09-02) make this verb answer the
    question the refusal on ``agent create --persona`` sends an operator here to
    ask. It already enumerated the placeable definitions — that is exactly what
    ``ensure_persisted_personas`` returns — but it named only ONE of the two
    spellings ``--persona`` takes, and the ``profile`` column beside it is the
    persona's binding rather than an accepted argument, which is a column an
    operator can read as the second spelling and be wrong. The list comes from
    :func:`agent_create.accepted_persona_spellings`, the same function the
    refusal's choice list spends, so the verb and the error cannot disagree.
    ``roster`` is this enumeration's OWN batch, because the ``profile:`` spelling
    is offered only for a uniquely-owned profile and ownership is a property of
    the set the row was read from — under ``--all-profiles`` that is the
    enumerated profile's config merge, not the active root's.
    """

    from agent_runtime.agent_create import accepted_persona_spellings

    binding = (bindings or {}).get(persona.id)
    row = {
        "id": persona.id,
        "name": persona.display_name,
        "role": str(persona.role),
        "profile": persona.hermes_profile,
        "persona_spellings": accepted_persona_spellings(persona, list(roster or [persona])),
        "skills": list(getattr(persona, "skills", []) or []),
        # S0a A6c: WHICH tier answered ``skills``, and what the config declared
        # that the store row does not carry. ``ensure_persisted_personas``
        # resolves that disagreement store-wins and said nothing, so a config
        # ``skills:`` addition that never reached a placement was invisible here.
        # Accounting only — this verb writes nothing; ``persona set-skills`` is
        # the store-writing door and keeps its supersede clock.
        **((skill_sources or {}).get(persona.id) or {
            "skills_source": "catalog",
            "catalog_only_skills": [],
        }),
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
    data = {
        "ok": True,
        "pet": _pet_sprite_payload_for_launcher(
            pet, include_sheet=not bool(getattr(args, "no_sheet", False))
        ),
    }
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
        # The producer the launcher's revision-keyed sprite-cache eviction has
        # been waiting for. That eviction (``PetdexSpriteCache
        # .noteSpritesheetRevision``) was built and gated on 2026-08-21 with no
        # writer on this side: hermes stamped ``spritesheetRevision`` only on the
        # ``pets sprite`` payload, which the launcher reads AFTER it has already
        # decided whether its resident decode is stale. A cache key nothing
        # produces is a cache key that never fires, so the only live
        # invalidation writers were the explicit clear paths.
        #
        # Same ``_pet_sheet_revision`` the sprite payload uses, deliberately: two
        # producers of one key would drift, and the launcher compares the value
        # from THIS row against the one the sprite payload stamped into its
        # resident decode. Different arithmetic here would evict every sheet on
        # every gallery read.
        #
        # REMOTE manifest rows above stay unstamped, and that is the honest
        # answer rather than an omission: their sheet is the one behind
        # ``spritesheetUrl``, which this process cannot stat. A revision
        # fabricated from something else would be a key that means nothing, and
        # the launcher — which correctly treats an unstamped sheet as "no
        # evidence of staleness" rather than as stale — would start evicting on
        # it. A sheet that cannot be stat'd at all lands here as the empty
        # string, which that same reader already handles as unstamped.
        "spritesheetRevision": _pet_sheet_revision(pet.spritesheet),
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


def _pet_sprite_payload_for_launcher(pet, *, include_sheet: bool = True) -> dict:
    """The launcher payload for an installed pet.

    ``include_sheet=False`` is the METADATA-ONLY shape (``pets sprite
    --no-sheet``, row 33), mirroring ``characters sprite --no-sheet``: it
    drops ``spritesheetBase64`` and puts ``sheet`` — the absolute path — in
    the same slot, so a consumer that wants ``framesByRow``/``stateRows`` and
    reads the file itself is not also handed the whole sheet re-encoded as
    base64. Unlike the character-sheet mode, the geometry keys below
    (``framesByRow``, ``framesByState``, ``stateRows``) still open the pet's
    sheet — a pet carries no per-row frame count in its manifest, so the
    padding-trimmed counts can only come from the image itself; only the
    WHOLE-SHEET base64 encode is skipped. The default is byte-identical to
    what it always was.
    """

    import base64

    from agent.pet import constants, render

    suffix = pet.spritesheet.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/webp"
    sheet_slot = (
        {"spritesheetBase64": base64.standard_b64encode(pet.spritesheet.read_bytes()).decode("ascii")}
        if include_sheet
        else {"sheet": str(pet.spritesheet)}
    )
    return {
        "slug": pet.slug,
        "displayName": pet.display_name,
        "description": pet.description,
        "mime": mime,
        **sheet_slot,
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


# ───────────────────────── character sheets (charsheet) ─────────────────────────
#
# The launcher's transport for pets is a one-shot `--json` subprocess, so the
# character QA panel's backend is the same thing: these verbs, and nothing else.
# Each handler loads a draft and calls exactly one CharacterDraft method — the
# stage machine refuses out-of-order calls, so there is no ordering logic here to
# get out of sync with the backend's.


def _characters_error(args, exc: BaseException, **extra) -> int:
    """The pets error shape, verbatim: flat `{"ok": false, "error": …}`, exit 2.

    Not `emit_harness_error`: that emits the Stage-42 envelope (`error` as a
    nested object with a code taxonomy), and a launcher panel that already parses
    the pets shape should not have to learn a second one for its sibling verbs.
    """
    data = {"ok": False, "error": str(exc)}
    data.update(extra)
    print(emit_json(data) if getattr(args, "json", False) else data["error"])
    return 2


def _characters_next(verb: str, *flags: str, alternatives=()) -> dict:
    """The machine next-step hint: `{verb, cmd}`, and sometimes a second way on.

    Measured cause (turn-efficiency plan, Stage 4b): one authoring turn spent
    seven `--help` elements and a `characters --help` working out which verb came
    next and how to spell it. Every one of those is a full API round-trip that
    re-sends the whole turn context — 60-120k prompt tokens each, late in a heavy
    turn. A payload that names its own successor answers the question before it
    is asked, and it answers it for a caller with no skill in context at all.

    A COMMAND, not a verb name: a bare name still costs a `--help` to turn into a
    command line, which is the round-trip being removed. It is spelled with the
    `hermes` entrypoint the skill teaches rather than `python -m hermes_cli.main`
    — both resolve, but the long one is noise in every trace row and the operator
    trace truncates commands at 500 chars.

    `alternatives` carries the second legal move when there genuinely are two
    (a failed batch: re-roll the row that died, or resume the ones that never
    landed). One shape, so a consumer reads `next["cmd"]` and, if it wants the
    fork, `next["alternatives"]` — never a different key per verb.

    ADDITIVE and OPTIONAL. The payloads are ruled supersets, so a new key breaks
    no consumer; a verb with no next step omits `next` rather than carrying an
    empty one, because a hint naming a verb the draft would refuse costs the
    caller exactly the round-trip this exists to save.
    """
    hint = {
        "verb": verb,
        "cmd": " ".join(["hermes", "harness", "characters", verb, *flags, "--json"]),
    }
    if alternatives:
        hint["alternatives"] = list(alternatives)
    return hint


def _characters_emit(args, data: dict, human: str) -> int:
    print(emit_json(data) if getattr(args, "json", False) else human)
    return 0


def _attempt_label(index: int, total: int | None = None) -> str:
    """How an attempt is SHOWN to a person: 1-based, and counted the same way.

    **A QA surface relabels, never renumbers.** Attempt indices are 0-based
    everywhere a machine reads them — the `status --json` history, `--attempt`,
    the revision store's one resolver — and the payloads emitted beside these
    lines keep that. But the store's own files are `attempt-1.png`,
    `attempt-2.png`, and a human line that said "attempt 0 of 3" put two
    different bases in one sentence and left an operator correlating a crop to
    a store file off by one. Every human line that shows an attempt renders it
    here, so the two numbering systems meet in exactly one place.
    """
    shown = f"attempt {index + 1}"
    return f"{shown} of {total}" if total is not None else shown


# What the backend raises for a refusal the operator can act on: a wrong stage, an
# unauthored direction, an unknown row key (ValueError); a missing draft or an
# uninstalled slug (FileNotFoundError); an --attempt out of range (IndexError);
# and the two TYPED refusals the charsheet package raises for conditions that are
# not about the request at all (`CharsheetRefusal` — a draft another generation
# already holds, a provider call past its deadline). Anything else is a bug and
# keeps its traceback.
#
# The typed pair deliberately does NOT ride `ValueError`. This tuple is the
# taxonomy the character lane actually reads — `emit_harness_error` /
# `_error_code_for_exception` are never called by any `characters` verb — so
# these two are caught HERE, and their `code` travels on the flat payload rather
# than being flattened into `invalid_request` by a mapping nothing on this lane
# consults.
_CHARACTERS_EXPECTED = (ValueError, FileNotFoundError, IndexError, CharsheetRefusal)


def _characters_refusal_extra(draft, exc: BaseException) -> dict:
    """What a TYPED charsheet refusal adds to the flat error payload.

    Two keys, both additive on a ruled superset (`_characters_next`):

    * ``code`` — the stable token (`draft_busy`, `provider_timeout`). A consumer
      that had to branch on the message text was branching on prose; the
      launcher's `CharaAuthoringOutcome.refused` already ignores keys it does
      not know, so this costs it nothing and gains it a test it can write.
    * ``busy`` — the HOLDER, for `draft_busy` only: verb, pid, host, start time,
      age, and the lock path. The last one is not decoration. There is no pid
      liveness on Windows (`os.kill(pid, 0)` kills the process — this repo's
      mutation gate refuses the probe for that reason), so a lock a crashed
      generation left behind is cleared by hand until the age ceiling laps it,
      and a refusal that withheld the path would leave the operator guessing.

    The ``next`` hint is `status` and nothing else. There genuinely is no second
    legal move — hermes cannot cancel a running generation
    (`serve.py`'s `cancel_denied`), so "wait, and look at what has landed" is
    the whole of it — and `_characters_next` is explicit that `alternatives`
    carries a second move only when there are two.
    """

    code = getattr(exc, "code", "")
    if not code:
        return {}
    extra: dict = {"code": code}
    if isinstance(exc, DraftBusy):
        details = getattr(exc, "safe_details", None)
        if details:
            extra["busy"] = dict(details)
        extra["next"] = _characters_next("status", "--draft", draft.id)
    return extra


def _characters_verb(args, call, on_error=None) -> int:
    """Load `--draft`, run one backend call, emit the house payload.

    *call* takes the draft and returns `(payload_dict, human_line)`. The draft id
    and the stage AFTER the call ride on every response: a verb can advance the
    stage, and the caller's next legal verb depends on knowing that it did.

    *on_error*, when given, takes `(draft, exception)` and returns extra keys for
    the REFUSAL payload — the one place a verb knows something about its own
    failure that the flat error shape cannot carry. It runs only for refusals a
    caller can act on (`_CHARACTERS_EXPECTED`); a bug still keeps its traceback.
    """
    from agent.charsheet.draft import CharacterDraft

    draft_id = str(getattr(args, "draft", "") or "").strip()
    try:
        draft = CharacterDraft.load(draft_id)
    except _CHARACTERS_EXPECTED as exc:
        return _characters_error(args, exc, draft=draft_id)
    try:
        result, human = call(draft)
    except _CHARACTERS_EXPECTED as exc:
        extra = dict((on_error(draft, exc) or {}) if on_error is not None else {})
        # LAST, so a typed refusal's own hint wins. `rows`'s `on_error` builds a
        # resume naming the rows that never landed — exactly right for a batch
        # that died mid-flight, and exactly wrong for a batch that was never
        # admitted because another writer holds the draft.
        extra.update(_characters_refusal_extra(draft, exc))
        return _characters_error(args, exc, draft=draft.id, stage=draft.stage, **extra)
    data = {"ok": True, "draft": draft.id, "stage": draft.stage}
    data.update(result)
    return _characters_emit(args, data, human)


def _characters_draft_summary(draft) -> dict:
    """A list row: identity and shape, without walking the revision store.

    ``baseImage`` answers with the SAME spelling of absence ``status --json``
    uses — a ``str`` or JSON ``null``, never ``""`` — through the one helper
    (``draft.path_or_none``). ``list`` and ``status`` name the same field, and a
    consumer that has to remember which of the two flattens absence is a
    consumer that will get it wrong.

    ``shadows`` is what makes a duplicate ``id`` readable rather than a defect.
    A backup directory is a copy of a draft directory, so it answers the
    ORIGINAL's id and two rows carried one id with nothing to tell them apart.
    The copy stays a row — it is on disk — and names the id it copies, so a
    consumer drops every row carrying ``shadows`` and keeps the un-shadowed one.
    ``str`` or JSON ``null``, the same spelling of absence as its neighbours.
    """
    from agent.charsheet.draft import path_or_none

    spec = draft.spec
    return {
        "id": draft.id,
        "slug": draft.slug,
        "displayName": draft.display_name,
        "concept": draft.concept,
        "style": draft.style,
        "shadows": draft.shadows,
        "authoredBy": draft.authored_by,
        # Beside `authoredBy` in all three payloads that carry provenance —
        # this row, `status --json`, and the `start --json` summary (which is
        # this helper) — so a consumer never has to remember which of the three
        # answers the question. `str` or JSON `null`, never `""`.
        "hermesHome": draft.hermes_home,
        "stage": draft.stage,
        "rows": len(spec.rows()),
        "authoredRows": len(spec.authored_rows()),
        "directions": len(spec.scheme.order),
        "baseImage": path_or_none(draft.base_image),
        "directory": str(draft.directory),
    }


def _characters_installed_rows() -> list[dict]:
    """Installed characters: one row per directory carrying a manifest.

    ``handednessAccepted`` rides on every row because the alternative is that a
    character carrying a mirrored row its operator overrode looks IDENTICAL here
    to one that passed clean — which is the shape this whole lane exists to
    retire. It is a list of ``{row, gain, basis}``, empty for nearly every
    character.

    ``palette`` is the compose-time colour table (``#RRGGBBAA``, most-used
    first) and is CONDITIONAL, unlike its neighbours: a character composed
    before the table existed carries no key at all rather than an empty list.
    "Nobody recorded a palette" and "this sheet has no colours" are different
    facts, and the launcher's swatch strip owes an old character a blank strip
    and a colourless one a defect report. See
    ``agent/charsheet/draft.py::read_palette``.
    """
    from agent.charsheet.draft import (
        MANIFEST_FILENAME,
        SHEET_FILENAME,
        _handedness_accepted,
        characters_dir,
        read_palette,
    )

    root = characters_dir()
    rows: list[dict] = []
    for child in sorted(root.iterdir()) if root.is_dir() else []:
        manifest_path = child / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}
        sheet = child / SHEET_FILENAME
        palette = read_palette(child)
        rows.append(
            {
                "slug": str(manifest.get("slug", "") or child.name),
                "displayName": str(manifest.get("displayName", "") or child.name),
                "draftId": str(manifest.get("draftId", "")),
                "created": str(manifest.get("created", "")),
                "directory": str(child),
                "sheet": str(sheet) if sheet.is_file() else "",
                "installed": sheet.is_file(),
                **({"palette": palette} if palette is not None else {}),
                "handednessAccepted": _handedness_accepted(manifest),
            }
        )
    return rows


def _cmd_characters_start(args) -> int:
    from agent.charsheet import spec as charsheet_spec
    from agent.charsheet.draft import CharacterDraft

    states_text = str(getattr(args, "states", "") or "").strip()
    base_text = str(getattr(args, "base_image", "") or "").strip()
    try:
        # An empty --states means "the CHAR8 states" — taken from CHAR8 itself
        # rather than re-spelled as a default string, so the default has one home.
        states = charsheet_spec.parse_states(states_text) if states_text else charsheet_spec.CHAR8.states
        scheme = charsheet_spec.parse_directions(str(getattr(args, "directions", "8") or "8"))
        sheet_spec = charsheet_spec.SheetSpec(states=states, scheme=scheme)
        draft = CharacterDraft.create(
            concept=str(getattr(args, "concept", "") or ""),
            slug=str(getattr(args, "slug", "") or ""),
            display_name=str(getattr(args, "display_name", "") or ""),
            style=str(getattr(args, "style", "auto") or "auto"),
            spec=sheet_spec,
            # Passed to create() rather than set after it, so a draft is never
            # observable without the anchor the operator asked it to have.
            base_image=Path(base_text).expanduser() if base_text else None,
            authored_by=str(getattr(args, "authored_by", "") or ""),
        )
    except _CHARACTERS_EXPECTED as exc:
        return _characters_error(args, exc)
    data = {"ok": True, "draft": draft.id, "stage": draft.stage, "summary": _characters_draft_summary(draft)}
    # The pipeline's first hint, and it reads the draft rather than the plan: a
    # draft started WITHOUT `--base-image` is the CS-5 repair shape, and
    # `turnaround` refuses without the anchor. Naming it there would hand the
    # caller the one round-trip this key exists to remove, so the anchorless
    # draft is pointed at the verb that repairs it. `<image>` is the single thing
    # the runtime cannot know.
    data["next"] = (
        _characters_next("turnaround", "--draft", draft.id)
        if draft.base_image is not None
        else _characters_next("base", "--draft", draft.id, "--image", "<image>")
    )
    return _characters_emit(
        args,
        data,
        f"Draft {draft.id} ({draft.slug}) created at stage '{draft.stage}': "
        f"{len(draft.spec.rows())} rows, {len(draft.spec.scheme.order)} directions",
    )


def _cmd_characters_list(args) -> int:
    from agent.charsheet.draft import CharacterDraft

    try:
        drafts = [_characters_draft_summary(draft) for draft in CharacterDraft.list_drafts()]
        installed = _characters_installed_rows()
    except _CHARACTERS_EXPECTED as exc:
        return _characters_error(args, exc)
    data = {"ok": True, "drafts": drafts, "characters": installed}
    lines = [f"{len(drafts)} draft(s), {len(installed)} installed character(s)"]
    lines += [f"  draft {row['id']}  {row['slug']}  stage={row['stage']}" for row in drafts]
    lines += [
        f"  installed {row['slug']}  {row['displayName']}"
        + (
            "  handedness accepted: "
            + ", ".join(
                f"{entry['row']} {entry['gain'] * 100:.0f}% ({entry['basis']})"
                for entry in row["handednessAccepted"]
            )
            if row["handednessAccepted"]
            else ""
        )
        for row in installed
    ]
    return _characters_emit(args, data, "\n".join(lines))


def _cmd_characters_backfill_home(args) -> int:
    """Stamp `hermes_home` on the library drafts that lack it, with THIS run's home.

    **Why a verb, and not a hook.** Doing it on LOAD turns every read into a
    disk writer — `characters list` is the launcher's cached polling read, and a
    read-that-writes races any concurrent mutation of the same draft for the
    rest of time. Doing it on the next MUTATION never reaches the dormant drafts
    that are the entire backfill population, and hides a provenance write inside
    every unrelated receipt. An explicit verb is bounded to the moment an
    operator chose, and its receipt is the evidence that it was.

    **The receipt names directories beside ids.** Two drafts can carry the same
    `id` — a copied draft keeps the id inside its `draft.json` — so an id-only
    receipt cannot say which of the two directories was written, which is the
    one thing an operator reading it afterwards needs to know.
    """
    from agent.charsheet.draft import CharacterDraft
    from hermes_constants import get_hermes_home

    home = str(get_hermes_home())
    try:
        stamped: list[dict] = []
        skipped: list[dict] = []
        # `list_drafts` already skips the unreadable ones, with a warning — a
        # draft this cannot parse is not a draft this may rewrite.
        for draft in CharacterDraft.list_drafts():
            row = {"id": draft.id, "directory": str(draft.directory)}
            if draft.record_home():
                stamped.append(row)
            else:
                skipped.append({**row, "hermesHome": draft.hermes_home})
    except _CHARACTERS_EXPECTED as exc:
        return _characters_error(args, exc)
    data = {"ok": True, "home": home, "stamped": stamped, "skipped": skipped}
    lines = [f"{len(stamped)} draft(s) stamped with {home}; {len(skipped)} already recorded"]
    lines += [f"  stamped {row['id']}  {row['directory']}" for row in stamped]
    # The DIRECTORY rides on this arm too. The skipped rows are exactly the
    # population an id cannot disambiguate — a copied draft keeps the id inside
    # its `draft.json` AND keeps the home it was made in, so the two rows an
    # id-collision produces here differ in nothing BUT their directory.
    lines += [
        f"  skipped {row['id']}  {row['directory']}  already {row['hermesHome']}"
        for row in skipped
    ]
    return _characters_emit(args, data, "\n".join(lines))


def _cmd_characters_migrate_home(args) -> int:
    """Move this home's legacy character store into the install-wide library.

    **The source is spelled literally, and that is not laziness.** It is
    `get_hermes_home() / "characters"` written out here rather than resolved
    through `characters_dir()`, because after the head-home that function answers
    the DESTINATION — a source resolved through it would ask the verb to move
    the library onto itself. This is the one site in hermes that is allowed to
    name the legacy location, and it names it because the location is legacy:
    nothing writes there any more, and this verb exists to empty it once.

    **Explicit and per-home, not a sweep.** One invocation migrates ONE home, the
    `backfill-home` operational pattern: the receipt stays attributable to the
    home the operator named, and the verb never has to enumerate profiles —
    which is the per-home resolution the launcher refused to do and hermes has
    no better claim to guess at. The operator runs it once per profile that has
    a store.
    """
    from agent.charsheet.draft import migrate_characters_home
    from hermes_constants import get_hermes_home, get_shared_characters_dir

    home = get_hermes_home()
    try:
        receipt = migrate_characters_home(
            home / "characters", get_shared_characters_dir(), source_home=str(home)
        )
    except _CHARACTERS_EXPECTED as exc:
        return _characters_error(args, exc)
    moved, stamped, skipped = receipt["moved"], receipt["stamped"], receipt["skipped"]
    lines = [
        f"{len(moved)} entr(ies) moved from {receipt['from']} to {receipt['to']}; "
        f"{len(stamped)} stamped, {len(skipped)} skipped"
    ]
    # Every line names the DIRECTORY beside the id or slug, for the reason the
    # backfill's receipt does: an id-collision pair lists twice under one id, and
    # the directory is the only thing that says which entry a row is about.
    lines += [
        f"  moved {row['kind']} {row.get('id') or row.get('slug')}  {row['from']} -> {row['to']}"
        for row in moved
    ]
    lines += [f"  stamped {row['id']}  {row['directory']}" for row in stamped]
    # The skipped arm was the exception the comment above did not know it had:
    # a refusal is the row an operator has to ACT on — the entry is still
    # sitting somewhere and they have to go look at it — so it is the last one
    # that may print an id the collision pair makes ambiguous.
    lines += [
        f"  skipped {row['kind']} {row.get('id') or row.get('slug')}  "
        f"{row['directory']}  {row['reason']}"
        for row in skipped
    ]
    return _characters_emit(args, receipt, "\n".join(lines))


def _cmd_characters_status(args) -> int:
    def call(draft):
        status = draft.status_payload()
        return {"status": status}, (
            f"Draft {draft.id} ({draft.slug}) at stage '{draft.stage}'; pending "
            f"turnaround={status['pending']['turnaround']} rows={status['pending']['rows']}"
        )

    return _characters_verb(args, call)


def _cmd_characters_thumb(args) -> int:
    # A path, never bytes: the crop is written into the draft and the payload
    # names it (plan A-4). `pets thumb` answers with a data URI because a Petdex
    # gallery row may come from a remote sheet; a draft's attempts are always on
    # this disk, and the launcher reads them there.
    from agent.charsheet.draft import DEFAULT_THUMB_FRAME, DEFAULT_THUMB_SCALE

    row_key = str(getattr(args, "row", "") or "").strip()
    direction = str(getattr(args, "direction", "") or "").strip()
    attempt = int(getattr(args, "attempt", -1))
    requested_frame = getattr(args, "frame", None)
    frame = DEFAULT_THUMB_FRAME if requested_frame is None else int(requested_frame)
    requested_scale = getattr(args, "scale", None)
    scale = DEFAULT_THUMB_SCALE if requested_scale is None else int(requested_scale)
    square = bool(getattr(args, "square", False))

    def call(draft):
        if direction:
            # A reference holds ONE pose. Ignoring `--frame` here would answer a
            # caller who asked for cell 3 with cell 0 and call it a crop.
            if requested_frame is not None:
                raise ValueError(
                    "--frame addresses a cell of a row STRIP; a direction "
                    f"reference is one pose, so `--direction {direction}` and "
                    "--frame cannot be asked for together"
                )
            result = draft.direction_thumb(
                direction, attempt=attempt, scale=scale, square=square
            )
        else:
            result = draft.row_thumb(
                row_key, attempt=attempt, frame=frame, scale=scale, square=square
            )
        # An agent reads the human line as often as the payload, and the one
        # thing it must not do with a deep zoom is declare it with `MEDIA:`. So
        # the line says which artifact this is, not just how big it came out.
        #
        # TWO bounds, and the line names WHICH one a crop missed, because the
        # remedy differs: over the console ceiling means the decode itself is
        # unsafe, while heavier-than-your-own-sheet means the crop is safe and
        # simply bought nothing. Inline only when both hold — the same rule
        # `row_thumb`'s docstring states for the launcher card.
        if not result["withinConsoleBudget"]:
            weight = " — over the console's decode ceiling; open it in the viewer, never as a card"
        elif not result["withinOwnSheet"]:
            weight = " — heavier than this draft's own sheet, so cropping bought nothing; open it in the viewer"
        else:
            weight = ""
        # And WHICH SHAPE, because the two are used for different things: a
        # padded square is the hero-card crop, a bare cell is what a compare
        # pair's panes align on.
        shape = ", padded square" if result["square"] else ""
        # WHICH item, in the item's own vocabulary: a row crop names its frame
        # because an operator judges a row frame by frame, and a reference has
        # no frame to name. Saying "frame 1 of 1" for a reference would invite
        # the reader to go looking for frame 2.
        subject = (
            f"direction {result['direction']} reference "
            if direction
            else f"row {result['row']} "
        )
        frames = "" if direction else f"frame {result['frame'] + 1} of {result['frames']}, "
        return result, (
            f"Draft {draft.id}: {subject}"
            f"{_attempt_label(result['attempt'], result['attempts'])}, "
            f"{frames}"
            f"cropped at {result['width']}x{result['height']} "
            f"({result['scale']}x{shape}) → {result['path']}{weight}"
        )

    return _characters_verb(args, call)


def _cmd_characters_base(args) -> int:
    # Repairs a draft started without --base-image (CS-5 finding: without this
    # verb such a draft could never advance) and covers the base-pick flow.
    def call(draft):
        target = draft.set_base_image(str(getattr(args, "image", "") or "").strip())
        return {"baseImage": str(target)}, f"Draft {draft.id}: base image set to {target}"

    return _characters_verb(args, call)


# ── the four pipeline steps, as callables both doors share ──────────────────
#
# `turnaround → approve-direction --all → rows → compose` is driven from two
# places now: one verb per call (the interactive lane) and `characters auto`
# (the one-shot lane, Stage 5). The plan's promise for `auto` is that each stage
# receipt is "the same payload the verb prints today" — a promise a copy of the
# body could not keep for longer than the next edit to one of them. So the body
# lives once, here, and both doors call it.


def _characters_step_turnaround(draft):
    result = draft.run_turnaround()
    directions = ", ".join(sorted(result.get("turnaround", {})))
    return result, f"Draft {draft.id}: proposed turnaround references for {directions} (awaiting approval)"


def _characters_face_offset_label(offset) -> str:
    """One approved reference's measured facing, spelled for a person.

    The sign is the reading — positive is a head to the RIGHT of frame — so it
    is always printed, and the word beside it is there because a bare `+10.9` in
    a receipt is a number an operator has to go and look up. Approving a
    turnaround said nothing about the direction until this line existed.
    """
    if offset is None:
        return "facing unmeasured (no image to read)"
    if offset > 0:
        return f"face offset +{offset:.1f} px (head right of body centre)"
    if offset < 0:
        return f"face offset {offset:.1f} px (head LEFT of body centre)"
    return "face offset 0.0 px (head over body centre)"


def _characters_face_offsets(offsets: dict) -> str:
    """The same measurement for a whole turnaround, in one clause.

    Compact on purpose: `--all` approves five references at once and the line
    has to stay one line, so it is the signed numbers in the order they were
    approved. The disagreement this exists to surface is legible as a sign
    that does not match its neighbours.
    """
    if not offsets:
        return "no facings measured"
    return "face offsets " + ", ".join(
        f"{direction} {'unmeasured' if value is None else format(value, '+.1f')}"
        for direction, value in offsets.items()
    )


def _characters_step_approve_all(draft):
    result = draft.approve_all_directions()
    human = (
        f"Draft {draft.id}: approved {len(result['approved'])} direction(s) "
        f"({_characters_face_offsets(result['faceOffsets'])}); "
        f"stage is now '{draft.stage}'"
    )
    # Only when the approval ADVANCED the stage. `rows` refuses at stage
    # `turnaround`, and the payload already says `advanced: false` — a hint
    # disagreeing with the key beside it is a payload arguing with itself.
    if result.get("advanced"):
        result["next"] = _characters_next("rows", "--draft", draft.id)
    return result, human


def _characters_step_rows(draft, only: list[str] | None):
    result = draft.run_rows(only=only)
    return result, f"Draft {draft.id}: generated {len(result.get('rows', {}))} row strip(s)"


def _characters_step_compose(draft, accept: list[str]):
    from agent.charsheet import pipeline

    result = draft.compose(accept_handedness=accept)
    validation = result["validation"]
    # The handedness accounting rides on the SUCCESS line too. A clean
    # `composed → 1536x3120` told an operator nothing about the six of
    # fifteen rows the check could not answer for, and a clean pass has never
    # been a certificate.
    #
    # The WARNINGS ride on it as well, and that is not cosmetic. A
    # single-basis handedness finding no longer blocks, and an accepted one
    # never did: both are warnings, `validation["warnings"]` needs `--json`,
    # and a successful `--accept-handedness` therefore used to print a row
    # count and nothing else — no gain, no basis, no reason. A warning
    # nobody prints is the shape of the failure this whole lane exists to
    # retire.
    lines = [
        f"Draft {draft.id} composed → {result['slug']} "
        f"({validation['width']}x{validation['height']}) at {result['sheet']}; "
        + pipeline.handedness_summary(validation["handedness"])
    ]
    # A warning is a BLOCK now, not a sentence, so its continuation lines
    # are pushed under the same indent rather than falling back to column
    # zero and reading as separate output.
    lines += [
        "  warning: " + text.replace("\n", "\n  ")
        for text in validation["warnings"]
    ]
    return result, "\n".join(lines)


def _characters_rows_next(draft, only: list[str] | None) -> dict | None:
    """A batch that died mid-flight is the expensive moment to be lost in.

    `run_rows` generates and approves row by row, so a refusal leaves some
    rows landed and the rest not — and the two moves from here are re-roll
    the row that died (a `--note` is what changes the prompt) or resume the
    batch with the ones that never landed.

    The rows come off the draft's OWN pending list rather than off the error
    text, intersected with what this call ASKED for: a caller who ran
    `--only walk-e` must not be handed a resume naming eleven rows they
    never wanted. Order is the spec's, so the first pending requested row is
    the one the loop stopped on.

    Returns ``None`` when there is no honest hint to give.
    """
    # ONLY a refusal that happened while generating. `rows` also refuses for
    # being out of order, and at stage `turnaround` every row is "pending"
    # while `reroll-row` is just as illegal as `rows` was — a hint there
    # sends the caller at a second refusal. Caught by the existing
    # out-of-order test, which asserts the flat shape exactly.
    if draft.stage != "rows":
        return None
    # A hint may never cost the caller the refusal it rides on: if reading
    # the draft's state fails here, the flat error shape still travels.
    try:
        pending = draft.status_payload()["pending"]["rows"]
    except Exception:  # noqa: BLE001 - a missing hint beats a lost refusal
        return None
    if only is not None:
        wanted = set(only)
        pending = [key for key in pending if key in wanted]
    if not pending:
        return None
    return _characters_next(
        "reroll-row", "--draft", draft.id, "--row", pending[0],
        alternatives=[
            _characters_next("rows", "--draft", draft.id, "--only", ",".join(pending))
        ],
    )


def _cmd_characters_turnaround(args) -> int:
    return _characters_verb(args, _characters_step_turnaround)


def _cmd_characters_reroll_direction(args) -> int:
    direction = str(getattr(args, "direction", "") or "").strip()
    note = str(getattr(args, "note", "") or "")

    def call(draft):
        result = draft.reroll_direction(direction, note=note)
        return result, (
            f"Draft {draft.id}: direction {result['direction']} re-rolled "
            f"({_attempt_label(result['attempt'], result['attempts'])}, unapproved)"
        )

    return _characters_verb(args, call)


def _cmd_characters_approve_direction(args) -> int:
    approve_all = bool(getattr(args, "approve_all", False))
    direction = str(getattr(args, "direction", "") or "").strip()
    attempt = int(getattr(args, "attempt", -1))

    def call(draft):
        if approve_all:
            return _characters_step_approve_all(draft)
        result = draft.approve_direction(direction, attempt=attempt)
        human = (
            f"Draft {draft.id}: approved {result['direction']} "
            f"{_attempt_label(result['approved'])}, "
            f"{_characters_face_offset_label(result['faceOffset'])}; "
            f"stage is now '{draft.stage}'"
        )
        # Only when the approval ADVANCED the stage — same rule the `--all` arm
        # states at its own site.
        if result.get("advanced"):
            result["next"] = _characters_next("rows", "--draft", draft.id)
        return result, human

    return _characters_verb(args, call)


def _cmd_characters_rows(args) -> int:
    only_text = str(getattr(args, "only", "") or "").strip()
    only = [part.strip() for part in only_text.split(",") if part.strip()] if only_text else None

    def call(draft):
        return _characters_step_rows(draft, only)

    def on_error(draft, exc):
        hint = _characters_rows_next(draft, only)
        return {"next": hint} if hint is not None else None

    return _characters_verb(args, call, on_error=on_error)


def _cmd_characters_reroll_row(args) -> int:
    row_key = str(getattr(args, "row", "") or "").strip()
    note = str(getattr(args, "note", "") or "")

    def call(draft):
        result = draft.reroll_row(row_key, note=note)
        return result, (
            f"Draft {draft.id}: row {result['row']} re-rolled "
            f"({_attempt_label(result['attempt'], result['attempts'])}, approved)"
        )

    return _characters_verb(args, call)


def _cmd_characters_compose(args) -> int:
    accept_text = str(getattr(args, "accept_handedness", "") or "").strip()
    accept = [part.strip() for part in accept_text.split(",") if part.strip()]

    def call(draft):
        return _characters_step_compose(draft, accept)

    return _characters_verb(args, call)


# ───────────────────────────── the autopilot ─────────────────────────────
#
# Stage 5 of the turn-efficiency plan, ruled in 2026-08-31 (R-3 → YES, wave
# ruling RD-10). The measured problem: a one-message "make me a character and
# drive it all the way" ask cost 27 API calls and 1.556M cumulative prompt
# tokens, and twelve of those calls were the agent asking a blocked pipeline
# "done yet?". This verb is ONE process that runs the four pipeline steps and
# prints a receipt as each lands, so the same ask costs a fire and a delivery.
#
# Three conditions the ruling adopted from the stage text, each load-bearing:
#
#   * it is for an operator's EXPLICIT "drive it all the way" ask only — it
#     auto-approves the turnaround, which is the last moment a reference can
#     change;
#   * it STOPS on a handedness refusal and never overrides one (no
#     `--accept-handedness` reaches this door, and the compose refusal it prints
#     carries no `next` hint, because the only hint available would be the
#     override itself);
#   * it writes the SAME per-attempt history the interactive verbs write — it
#     calls their bodies, so `reopen` repair, `status --json` history and every
#     QA crop work identically afterwards.

_CHARACTERS_AUTO_STEPS: tuple[str, ...] = (
    "turnaround",
    "approve-direction",
    "rows",
    "compose",
)


def _characters_auto_write(args, data: dict, human: str) -> None:
    """One receipt line, written and FLUSHED the moment its stage lands.

    Flushed on purpose. The turn that fires this verb backgrounds it and reads
    its log while it runs — a 10-20 minute `rows` batch is the whole reason the
    verb exists — and Python block-buffers stdout when it is a pipe. Without the
    flush every receipt arrives at once, at exit, and the operator watches a
    silent process for twenty minutes: exactly the "frozen" reading the plan
    measured on the live fire-imp run.

    `--json` frames with `emit_json_line`, never `emit_json`: the indenting
    encoder every other verb uses would break the newline framing this stream
    IS. Human mode prints the verb's own line, which for `compose` is a block.
    """
    sys.stdout.write((emit_json_line(data) if getattr(args, "json", False) else human) + "\n")
    sys.stdout.flush()


def _characters_auto_plan(draft, status: dict, through: str) -> tuple[list[str], list[dict]]:
    """Which steps this run will attempt, and the reason it skips each of the rest.

    Read the draft's STATE, never a fixed script. Driven as a flat script the
    autopilot is destructive twice over: `run_turnaround` re-rolls every
    direction reference AND clears the approvals, and `run_rows` with no
    `--only` regenerates every authored row — including the ones an operator
    kept. On the `reopen`-repair path that discards the QA work the operator
    just did and spends ten to twenty minutes of generation doing it. So the
    turnaround step runs only when some direction has NO attempt at all (the
    strip is generated whole, so there is no partial resume), and the rows step
    runs only over the rows with no approved strip — the same two lists the 4b
    resume hint reads, `missing.turnaround` and `pending.rows`.

    Every skip is REPORTED. An autopilot that quietly does three of four steps
    and exits 0 is indistinguishable from one that did all four, which is the
    one thing a caller who ended its turn cannot afford to be wrong about.
    """
    limit = _CHARACTERS_AUTO_STEPS.index(through)
    stage = draft.stage
    plan: list[str] = []
    skipped: list[dict] = []
    for index, step in enumerate(_CHARACTERS_AUTO_STEPS):
        if index > limit:
            reason = f"past --through {through}"
        elif step in ("turnaround", "approve-direction") and stage != "turnaround":
            reason = f"draft is at stage {stage!r}"
        elif step in ("rows", "compose") and stage not in ("turnaround", "rows"):
            reason = f"draft is at stage {stage!r}"
        elif step == "turnaround" and not status["missing"]["turnaround"]:
            reason = "every authored direction already has a reference"
        elif step == "rows" and not status["pending"]["rows"]:
            reason = "every authored row already has an approved strip"
        else:
            plan.append(step)
            continue
        skipped.append({"step": step, "reason": reason})
    return plan, skipped


def _characters_auto_next(step: str, draft) -> dict | None:
    """The `next` hint for a step that refused — or nothing, honestly.

    `compose` is deliberately absent. Its refusing shape is the handedness
    gate, and the only command that answers it is
    `compose --accept-handedness <row>:<basis>` — the override R-3 forbids this
    verb from nudging anyone toward. The refusal text already names every
    flagged row and its basis; an autopilot adds nothing to that but pressure.
    """
    if step == "turnaround" and draft.base_image is None:
        # Same arm `start` takes for an anchorless draft: `turnaround` refuses
        # without the anchor, and `<image>` is the one thing the runtime cannot
        # know.
        return _characters_next("base", "--draft", draft.id, "--image", "<image>")
    if step == "rows":
        return _characters_rows_next(draft, None)
    return None


def _cmd_characters_auto(args) -> int:
    from agent.charsheet.draft import CharacterDraft

    draft_id = str(getattr(args, "draft", "") or "").strip()
    through = str(getattr(args, "through", "compose") or "compose")

    def refuse(message: str, *, step: str, draft=None, hint: dict | None = None) -> int:
        """A refusal is a LINE, and the summary still follows it.

        `_characters_error` is not used anywhere in this verb: it emits the
        indented `emit_json` block, and one multi-line block in the middle of a
        newline-framed stream is unparseable by the consumer the framing exists
        for. Every line this verb writes — receipts, refusals, summary — has
        one shape, and the LAST line is always the summary.
        """
        line = {"ok": False, "error": message, "draft": draft_id, "step": step}
        if draft is not None:
            line["stage"] = draft.stage
        if hint is not None:
            line["next"] = hint
        _characters_auto_write(args, line, message)
        return 2

    try:
        draft = CharacterDraft.load(draft_id)
    except _CHARACTERS_EXPECTED as exc:
        refuse(str(exc), step="load")
        _characters_auto_write(
            args,
            {
                "ok": False,
                "draft": draft_id,
                "step": "auto",
                "through": through,
                "ran": [],
                "skipped": [],
                "stopped_at": "load",
                "error": str(exc),
            },
            f"Autopilot ran nothing: {exc}",
        )
        return 2

    # ONE acquisition around the WHOLE plan, not one per step. The lock is
    # re-entrant for this thread, so the step bodies below take it again and
    # get a no-op; a per-step lock would leave a gap between `turnaround` and
    # `rows` that a second generation could walk into, and this verb spends
    # ten to twenty minutes standing in those gaps.
    holding = contextlib.ExitStack()
    try:
        holding.enter_context(draft.generation_lock("auto"))
    except CharsheetRefusal as exc:
        refuse(str(exc), step="lock", draft=draft, hint=_characters_next("status", "--draft", draft.id))
        _characters_auto_write(
            args,
            {
                "ok": False,
                "draft": draft.id,
                "stage": draft.stage,
                "step": "auto",
                "through": through,
                "ran": [],
                "skipped": [],
                "stopped_at": "lock",
                "code": getattr(exc, "code", ""),
                "error": str(exc),
            },
            f"Autopilot ran nothing: {exc}",
        )
        return 2

    with holding:
        return _characters_auto_steps(args, draft, through, refuse)


def _characters_auto_steps(args, draft, through: str, refuse) -> int:
    """The plan, the loop and the summary — everything under the draft's lock.

    Split out of :func:`_cmd_characters_auto` only so the acquisition can refuse
    before any of it runs; the body is the verb it always was.
    """

    plan, skipped = _characters_auto_plan(draft, draft.status_payload(), through)
    ran: list[str] = []
    stopped_at: str | None = None
    error: str | None = None

    if not plan:
        # Nothing to drive is a REFUSAL, not a quiet success. The caller of this
        # verb ended its turn expecting a character; "ok, did nothing" twenty
        # minutes later is the reply it cannot act on.
        message = (
            f"nothing for the autopilot to run: draft {draft.id} is at stage "
            f"{draft.stage!r} and --through is {through!r}"
        )
        hint = (
            _characters_next("reopen", "--draft", draft.id)
            if draft.stage == "composed"
            else None
        )
        refuse(message, step="plan", draft=draft, hint=hint)
        stopped_at, error = "plan", message

    for step in plan:
        try:
            if step == "turnaround":
                result, human = _characters_step_turnaround(draft)
            elif step == "approve-direction":
                result, human = _characters_step_approve_all(draft)
            elif step == "rows":
                # The pending list is re-read HERE rather than reused from the
                # plan: the steps before this one can fail a row, and the batch
                # must ask for what is missing now.
                result, human = _characters_step_rows(
                    draft, draft.status_payload()["pending"]["rows"]
                )
            else:
                result, human = _characters_step_compose(draft, [])
        except _CHARACTERS_EXPECTED as exc:
            refuse(str(exc), step=step, draft=draft, hint=_characters_auto_next(step, draft))
            stopped_at, error = step, str(exc)
            break
        line = {"ok": True, "draft": draft.id, "stage": draft.stage, "step": step}
        line.update(result)
        _characters_auto_write(args, line, human)
        ran.append(step)

    summary = {
        "ok": error is None,
        "draft": draft.id,
        "stage": draft.stage,
        "step": "auto",
        "through": through,
        "ran": ran,
        "skipped": skipped,
    }
    if error is not None:
        summary["error"] = error
        summary["stopped_at"] = stopped_at
    _characters_auto_write(
        args,
        summary,
        f"Draft {draft.id}: autopilot ran {len(ran)} step(s)"
        + (f" ({', '.join(ran)})" if ran else "")
        + f"; stage is now '{draft.stage}'"
        + (f" — stopped at {stopped_at}" if error is not None else ""),
    )
    return 0 if error is None else 2


def _cmd_characters_reopen(args) -> int:
    def call(draft):
        result = draft.reopen()
        return result, (
            f"Draft {draft.id} reopened at stage {result['stage']} "
            "(installed sheet unchanged until the next compose)"
        )

    return _characters_verb(args, call)


def _cmd_characters_add_state(args) -> int:
    state_text = str(getattr(args, "state", "") or "").strip()

    def call(draft):
        result = draft.add_state(state_text)
        state = result["state"]
        # The `--only` list is spelled out for the operator because `--only` has
        # NO glob: `run_rows` matches keys exactly and raises on any key it does
        # not author, so `jumping-*` is one unknown row key, not a wildcard. The
        # verb that knows the new keys is the verb that should hand them over.
        return result, (
            f"Draft {draft.id}: state {state['name']} added "
            f"({state['frames']} frames, "
            f"{'directional' if state['directional'] else 'fixed'}); "
            f"{len(result['rows'])} new row(s) to generate — "
            f"`characters rows --draft {draft.id} --only {','.join(result['rows'])}`"
        )

    return _characters_verb(args, call)


def _cmd_characters_sprite(args) -> int:
    from agent.charsheet import draft as charsheet_draft

    slug = str(getattr(args, "slug", "") or "").strip()
    try:
        payload = charsheet_draft.sprite_payload(
            slug, include_sheet=not bool(getattr(args, "no_sheet", False))
        )
    except _CHARACTERS_EXPECTED as exc:
        return _characters_error(args, exc, slug=slug)
    data = {"ok": True, "character": payload}
    accepted = payload.get("handednessAccepted") or []
    return _characters_emit(
        args,
        data,
        f"{payload['slug']} ({payload['displayName']}): {len(payload['framesByRow'])} rows, "
        f"{payload['frameW']}x{payload['frameH']} cells, revision {payload['spritesheetRevision']}"
        # The human line says where the bytes ARE exactly when it is not
        # carrying them. Reading the key off the payload rather than off
        # `args.no_sheet` keeps the two from drifting: the mode is the payload's
        # fact, and the flag is only how this handler asked for it.
        + (f", sheet {payload['sheet']}" if "sheet" in payload else "")
        + (
            "; handedness accepted: "
            + ", ".join(
                f"{entry['row']} {entry['gain'] * 100:.0f}% ({entry['basis']})"
                for entry in accepted
            )
            if accepted
            else ""
        ),
    )


def _cmd_characters_payload_contract(args) -> int:
    """Publish the key set of every `characters` READ payload.

    The artifact the launcher's fixture is vendored from. Everything about how
    the key set is measured — probing rather than declaring, two vocabularies to
    tell data keys from schema keys, modes for the conditional slot — lives in
    `hermes_cli/charsheet_payload_contract.py`, which is also where a new
    payload kind is added.

    It emits through `_characters_emit` like every other `characters` verb, so
    `--json` prints the document and a bare call prints a human line. There is
    no draft and no slug: the probes are built and thrown away inside the call.
    """
    from hermes_cli.charsheet_payload_contract import build_payload_contract

    document = build_payload_contract()
    kinds = document["payloads"]
    return _characters_emit(
        args,
        document,
        "; ".join(
            f"{name}: {len(kind['keys'])} keys"
            + (
                f" ({sum(1 for k in kind['keys'].values() if k['conditional'])} conditional)"
                if any(k["conditional"] for k in kind["keys"].values())
                else ""
            )
            for name, kind in sorted(kinds.items())
        ),
    )


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


def _record_visibility_block(payload: dict, name: str, builder) -> None:
    """Build one failure-isolated ``provider_visibility`` block, and NAME the
    failure when the builder raises.

    The isolation itself is load-bearing (a broken status probe must never break
    the credential payload the launcher's model switcher depends on), but until
    EG-6.1 the isolator was a bare ``except Exception: pass`` and an absent block
    meant TWO things: "this hermes is too old to emit it" or "this hermes tried
    and the builder threw". The `catalog` block made that worst: it exists
    precisely to separate "never configured" from "configured and dead", so a
    silent drop fell the client back into the indistinguishable rendering the
    block was added to end.

    So a raise records ``block_errors[name] = <ExceptionClass>`` — the class NAME
    only, never ``str(exc)``, the same disclosure rule
    [_usage_failure_reason] and [_credential_token_preview] follow: a status
    probe's message can carry a resolved key, a URL or a header.

    ``block_errors`` is written ONLY when something actually threw. A healthy
    build carries no such key at all, so the three wire states stay distinct:
    block present (built), block absent with no ``block_errors`` (old hermes /
    never emitted), block absent and named in ``block_errors`` (this hermes
    tried and failed).
    """
    try:
        value = builder()
    except Exception as exc:  # noqa: BLE001 — class name only, block isolated
        payload.setdefault("block_errors", {})[name] = type(exc).__name__
        return
    payload[name] = value


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
    # the scrape", exactly like a v1 hermes — and since EG-6.1 a block that
    # dropped because its builder RAISED is named in `block_errors` by exception
    # class, so absence no longer means two things. See
    # [_record_visibility_block].
    _record_visibility_block(payload, "environment", _provider_visibility_environment)
    _record_visibility_block(payload, "api_keys", _provider_visibility_api_keys)
    _record_visibility_block(payload, "auth_logins", _provider_visibility_auth_logins)
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
    _record_visibility_block(payload, "catalog", _provider_visibility_catalog)
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
    pool holds any openai-codex entry.

    Two independent sources OR'd together, so a raise from the FIRST is not yet
    an answer — the pool may still say yes, and that yes is the truth. But a
    raise that ends with no affirmative source is NOT "not signed in": it is
    "we could not tell", and per EG-6.1 that must reach the caller as a class,
    not as a ``False`` indistinguishable from an empty pool. So the primary
    error is held and re-raised only if nothing affirms; a raise from the pool
    read itself propagates directly (one lane carries one named class).
    """
    primary_error: Optional[BaseException] = None
    try:
        from hermes_cli.auth import get_codex_auth_status

        if bool((get_codex_auth_status() or {}).get("logged_in")):
            return True
    except Exception as exc:  # noqa: BLE001 — held, re-raised only if unanswered
        primary_error = exc
    from agent.credential_pool import load_pool

    if load_pool("openai-codex").entries():
        return True
    if primary_error is not None:
        raise primary_error
    return False


def _openrouter_usage_login_detected() -> bool:
    """OpenRouter lane detected when the pool holds an entry OR the runtime
    resolver finds a usable key.

    Same held-primary-error discipline as [_codex_usage_login_detected]: a
    failure that leaves the question unanswered is raised, never flattened into
    the ``False`` that would silently delete the lane.
    """
    primary_error: Optional[BaseException] = None
    try:
        from agent.credential_pool import load_pool

        if load_pool("openrouter").entries():
            return True
    except Exception as exc:  # noqa: BLE001 — held, re-raised only if unanswered
        primary_error = exc
    from hermes_cli.runtime_provider import resolve_runtime_provider

    runtime = resolve_runtime_provider(requested="openrouter")
    if str(runtime.get("api_key", "") or "").strip():
        return True
    if primary_error is not None:
        raise primary_error
    return False


def _usage_lane_detected(provider_id: str) -> bool:
    """True iff the operator is signed-in / holds credentials for ``provider_id``.

    Detection has THREE outcomes, not two, and EG-6.1 stopped collapsing two of
    them together:

    * ``True``  — signed in; the lane is fetched and emitted;
    * ``False`` — credentials genuinely absent; the lane is OMITTED (this is now
      the exclusive meaning of an omitted lane);
    * RAISE     — the detector could not tell. This function no longer swallows.
      [_detect_usage_candidates] catches it per provider and the lane IS emitted,
      unavailable, naming the exception class.

    The old blanket ``except Exception: return False`` was strictly worse than
    the S1 defect it neighbours: S1 left a row saying "no usage data", whereas a
    swallowed detector fault made the row VANISH from the Limits panel, leaving
    nothing to carry a reason at all.
    """
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
    return False


class UnknownUsageLaneError(LookupError):
    """``_fetch_usage_lane`` was handed a provider id it has no fetcher for.

    The dispatch below covers ``_USAGE_LANE_PROVIDERS`` exactly, and that tuple
    is the ONLY producer of ids (``_detect_usage_candidates`` filters it and
    never adds). So this is unreachable today — and it is raised rather than
    handled precisely so it STAYS that way: a fifth provider added to the tuple
    without its fetcher becomes a loud, named per-lane failure instead of
    silently falling back into the upstream blanket swallow (see the docstring
    below for why that fallback was the loaded gun EG-0.3 removed).

    Carries ``provider_id`` as an attribute so ``_usage_failure_reason`` can
    name the id without ever touching ``str(exc)``.
    """

    def __init__(self, provider_id: str) -> None:
        self.provider_id = str(provider_id)
        super().__init__(f"no account-usage fetcher for lane {self.provider_id!r}")


def _fetch_usage_lane(provider_id: str):
    """Fetch the account-usage snapshot for one provider (may return None or
    raise; callers isolate failures). Nous flows through the portal-account +
    credits-snapshot path; the rest dispatch DIRECTLY to their per-provider
    fetcher. An id outside ``_USAGE_LANE_PROVIDERS`` raises
    ``UnknownUsageLaneError``.

    The direct dispatch is the point. ``agent.account_usage.fetch_account_usage``
    wraps all three shared fetchers in a blanket ``except Exception: return
    None`` (upstream-owned, `:884-902` — this module must not modify it), which
    erases the failure CLASS before ``_fetch_usage_lanes``' honest per-lane
    handler can report it. The None then serializes as the unfalsifiable
    ``no usage data``: a swallowed 401 rendered exactly like a provider that
    genuinely has nothing to say. Routing around the wrapper (the fork-boundary
    rule: route around upstream, don't patch it) lets the exception reach the
    handler that was built to name it.

    EG-0.3 REAPED THE FALL-THROUGH. This function used to end with
    ``return fetch_account_usage(provider_id)`` — dead code (EG-0.2 §3.2 proved
    the chain closed: the tuple is filter-only, and an unknown ``--provider``
    yields an empty candidate list that returns before dispatch) that was ALSO
    the one surviving route back into the swallow the paragraph above routed
    around. A typed raise is the replacement: the removal contract is a CODE row
    on ``fetch_account_usage`` scoped to ``hermes_cli`` in
    ``tests/agent_runtime/test_tombstone_registry.py``, so re-adding the call is
    loud by enumeration rather than by review.
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
    raise UnknownUsageLaneError(provider_id)


def _usage_failure_reason(exc: BaseException, *, phase: str = "fetch") -> str:
    """The operator-facing reason for a raised usage lane, in one grammar:
    ``usage <phase> failed (<fact>)``.

    ``phase`` names WHICH half of the lane raised — ``fetch`` (the default, the
    only phase before EG-6.1) or ``detection``. It is a caller-supplied literal,
    never derived from the exception, so it cannot leak anything; and it is
    deliberately the SAME function rather than a sibling, because the
    class-name-only rule below is the whole point and a second reason-builder
    would be a second place to forget it.

    CLASS NAME ONLY for everything except an HTTP status error, where the bare
    numeric status is added — a status code leaks nothing (no token, no URL, no
    exception message), and it is the single fact that separates "the provider
    rejected our credential" from "the network broke". 401/403 additionally
    earn the re-auth hint, matching the reauth-vs-connectivity discipline: only
    a confirmed auth rejection may suggest signing in again.

    ``UnknownUsageLaneError`` earns the second such exemption, on the same test
    the HTTP status passes: the fact added is the provider ID, which is the
    dispatch key itself — already carried verbatim in this lane's ``provider``
    field — so it leaks nothing that is not already in the envelope, and it is
    the single fact that turns "a lane failed" into "lane X has no fetcher".
    Read off ``exc.provider_id``, never ``str(exc)``.
    """
    name = type(exc).__name__
    if isinstance(exc, UnknownUsageLaneError):
        return f"usage {phase} failed ({name}: {exc.provider_id})"
    status = None
    response = getattr(exc, "response", None)
    if response is not None:
        raw = getattr(response, "status_code", None)
        if isinstance(raw, int):
            status = raw
    if status is None or name != "HTTPStatusError":
        return f"usage {phase} failed ({name})"
    suffix = " — re-auth may be required" if status in (401, 403) else ""
    return f"usage {phase} failed (HTTP {status}{suffix})"


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


def _usage_lane_scope(only_provider: Optional[str]) -> tuple[str, ...]:
    """The lanes this invocation may speak about, in stable emission order —
    ``_USAGE_LANE_PROVIDERS`` narrowed by ``--provider``. FILTER ONLY: it never
    adds an id, which is the property [UnknownUsageLaneError] leans on."""
    providers = _USAGE_LANE_PROVIDERS
    if only_provider:
        norm = str(only_provider).strip().lower()
        providers = tuple(p for p in providers if p == norm)
    return providers


def _detect_usage_candidates(
    only_provider: Optional[str],
) -> tuple[list[str], dict[str, str]]:
    """``(detected, detect_failures)`` — the two-value split EG-6.1 introduced.

    ``detected`` are the lanes to fetch. ``detect_failures`` maps provider id →
    the operator-facing reason for a detector that RAISED, so
    [build_account_usage] can emit that lane unavailable-and-named instead of
    letting it vanish. Isolation is per provider: one broken detector never
    suppresses another lane, which is the same guarantee
    [_fetch_usage_lanes] already gives the fetch half.
    """
    detected: list[str] = []
    failures: dict[str, str] = {}
    for provider in _usage_lane_scope(only_provider):
        try:
            hit = _usage_lane_detected(provider)
        except Exception as exc:  # noqa: BLE001 — class/status only, per lane
            failures[provider] = _usage_failure_reason(exc, phase="detection")
            continue
        if hit:
            detected.append(provider)
    return detected, failures


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


#: The degrade stages that can have EATEN LANES — the discriminator
#: [_usage_lanes_suppressed] reads, and the ONE place that judgement is written.
#:
#: The full stage vocabulary is these four plus ``active_provider``, which is
#: deliberately absent here: a failed active-provider resolution suppresses no
#: lanes at all, so an envelope degraded only there may still carry an honest
#: empty lane list. Every stage name is a fixed literal chosen at the raising
#: seam, never derived from an exception, so it leaks nothing.
_USAGE_LANE_SUPPRESSING_STAGES: tuple[str, ...] = (
    "detect",
    "fetch",
    "build",
    "serialize",
)


def _stamp_usage_degraded(payload: dict, stage: str, exc: BaseException) -> dict:
    """Record ``degraded[stage] = <ExceptionClass>`` on a usage envelope.

    The same ``{unit: ExceptionClass}`` shape as ``block_errors`` on the
    provider-visibility payload (see [_record_visibility_block]) — one idiom for
    "this builder tried and failed", on both halves of the provider surface.

    Why it exists: ``lanes: []`` and ``active_provider: null`` were each carrying
    two meanings. Empty lanes meant "no signed-in providers" OR "a seam
    collapsed"; a null active provider meant "none selected" OR "the resolver
    threw". The renderer's positive claim ("no signed-in providers detected") was
    the visible lie — see [_render_account_usage_human], which now consults this
    field before making it.

    First cause wins (``setdefault``): a detector collapse routinely makes the
    seams after it collapse too, and the FIRST class is the one that explains the
    envelope. Class name only, never ``str(exc)``.
    """
    payload.setdefault("degraded", {}).setdefault(stage, type(exc).__name__)
    return payload


def _usage_lanes_suppressed(payload: dict) -> Optional[str]:
    """The exception class of a degrade that SUPPRESSED lanes, or None.

    Not every degrade suppresses: a failed ``active_provider`` resolution leaves
    lane collection untouched, so an envelope degraded only there may still
    carry a complete, honest, EMPTY lane list — and the "no signed-in providers"
    claim is legitimate. Only the stages in [_USAGE_LANE_SUPPRESSING_STAGES] can
    have eaten lanes.
    """
    degraded = payload.get("degraded")
    if not isinstance(degraded, dict):
        return None
    for stage in _USAGE_LANE_SUPPRESSING_STAGES:
        found = degraded.get(stage)
        if found:
            return str(found)
    return None


def build_account_usage(
    *,
    only_provider: Optional[str] = None,
    timeout: float = DEFAULT_USAGE_TIMEOUT,
) -> dict:
    """Build the ``hermes.account_usage/v1`` envelope: one lane per detected
    provider login (codex / anthropic / openrouter / nous), fetched concurrently
    under an overall wall-clock bound. Fail-open at every seam — worst case the
    envelope carries empty lanes AND a ``degraded`` map naming which seam gave
    way (EG-6.1: fail-open, but never fail-silent)."""
    payload: dict = {
        "schema": USAGE_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "active_provider": None,
        "lanes": [],
    }
    try:
        payload["active_provider"] = _resolve_active_provider_id()
    except Exception as exc:  # noqa: BLE001 — named, not swallowed
        _stamp_usage_degraded(payload, "active_provider", exc)
    active_provider = payload["active_provider"]
    try:
        candidates, detect_failures = _detect_usage_candidates(only_provider)
    except Exception as exc:  # noqa: BLE001 — named, not swallowed
        return _stamp_usage_degraded(payload, "detect", exc)
    if not candidates and not detect_failures:
        return payload
    fetched: list[dict] = []
    if candidates:
        try:
            fetched = _fetch_usage_lanes(
                candidates, active_provider=active_provider, timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001 — named, not swallowed
            fetched = []
            _stamp_usage_degraded(payload, "fetch", exc)
    # Merge the fetched lanes with the detector-failed ones, back into the stable
    # `_USAGE_LANE_PROVIDERS` emission order: a lane whose DETECTOR raised holds
    # its natural slot in the Limits panel rather than being appended after the
    # healthy ones (or, as before EG-6.1, disappearing).
    lanes_by_provider = {lane["provider"]: lane for lane in fetched}
    for provider, reason in detect_failures.items():
        lanes_by_provider[provider] = _unavailable_usage_lane(
            provider, reason, active=provider == active_provider
        )
    payload["lanes"] = [
        lanes_by_provider[p]
        for p in _usage_lane_scope(only_provider)
        if p in lanes_by_provider
    ]
    return payload


def _render_account_usage_human(payload: dict) -> None:
    """Render the envelope as human lines, reusing the shared
    ``render_account_usage_lines`` per available lane."""
    from agent.account_usage import (
        AccountUsageSnapshot,
        AccountUsageWindow,
        render_account_usage_lines,
    )

    raw_degraded = payload.get("degraded")
    degraded = raw_degraded if isinstance(raw_degraded, dict) else {}
    active_label = payload.get("active_provider") or "(none)"
    if degraded.get("active_provider"):
        # "(none)" would be a claim about the operator's configuration; the
        # resolver never got far enough to make it.
        active_label = f"(unknown — resolution failed ({degraded['active_provider']}))"
    print(f"Active provider: {active_label}")
    lanes = payload.get("lanes") or []
    if not lanes:
        # THE claim this stage exists to fence. "no signed-in providers detected"
        # is a positive assertion about the operator's auth state, and it may be
        # printed ONLY when detection actually ran and found none. When a seam
        # that collects lanes gave way, the truth is that we do not know — so
        # state the degrade instead, naming the class.
        suppressed = _usage_lanes_suppressed(payload)
        if suppressed:
            print(f"No account-usage lanes: usage lanes unavailable ({suppressed}).")
            return
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


def _empty_usage_envelope(degraded: Optional[tuple[str, BaseException]] = None) -> dict:
    """Minimal, always-serializable ``hermes.account_usage/v1`` envelope with no
    lanes — the guaranteed fallback whenever a richer build or serialization step
    fails.

    ``degraded`` is the ``(stage, exception)`` that forced the fallback. It is
    optional only because a caller may genuinely have no cause to name; every
    caller that DOES have one passes it, because this envelope's empty ``lanes``
    is otherwise the same two-meaninged absence EG-6.1 removed everywhere else —
    and here it is emitted at the exact moment something is known to be broken.
    """
    payload: dict = {
        "schema": USAGE_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "active_provider": None,
        "lanes": [],
    }
    if degraded is not None:
        stage, exc = degraded
        _stamp_usage_degraded(payload, stage, exc)
    return payload


def _emit_usage_json(payload: dict) -> None:
    """Print the envelope as JSON, guaranteeing the ``--json`` branch NEVER
    raises. The verb's contract is total failure isolation, but ``emit_json`` (or
    the stdout write itself) can still fail; if it does, fall back to a minimal
    always-valid empty envelope serialized with the stdlib ``json.dumps`` so the
    fallback does not depend on the possibly-broken ``emit_json``. If even that
    write fails there is nothing more we can do, so it is swallowed and the verb
    still exits 0.

    The fallback envelope carries ``degraded: {"serialize": <Class>}``: it is
    reporting zero lanes at the exact moment it knows the real payload could not
    be written, and a client cannot be asked to tell that apart from a genuinely
    idle account."""
    try:
        print(emit_json(payload))
        return
    except Exception as exc:  # noqa: BLE001 — named on the fallback envelope
        serialize_error: BaseException = exc
    try:
        print(json.dumps(_empty_usage_envelope(("serialize", serialize_error))))
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
    except Exception as exc:  # noqa: BLE001 — named on the fallback envelope
        payload = _empty_usage_envelope(("build", exc))
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
        # Where each section keeps its own error text — DERIVED from the
        # doctor's own section table, not re-typed here. This roster was the
        # fourth copy of one set and the only one no test pinned, so a section
        # added to the report but forgotten here was counted and verdicted while
        # rendering no operator line at all.
        detail_sources = doctor_detail_sources(hygiene)
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
        census = hygiene.get("findings", {}).get("placement_census") or {}
        if census.get("observed"):
            # The census's own line, because its lists are the payload an
            # operator acts on and the ``findings:`` counts above only say how
            # many. Orphans are named individually — that id IS the remediation
            # argument — while unplaced rows are counted, since a healthy
            # runtime can legitimately carry several.
            print(
                f"placement census: placed={census.get('placed')} "
                f"unplaced_rows={len(census.get('unplaced_rows') or [])} "
                f"orphan_actors={len(census.get('orphan_actors') or [])} "
                f"desk_litter={len(census.get('desk_litter') or [])} "
                f"duplicate_placements={len(census.get('duplicate_placements') or [])}"
            )
            for orphan in census.get("orphan_actors") or []:
                print(
                    f"  orphan actor: {orphan.get('workspace_id')}/"
                    f"{orphan.get('actor_key')} -> "
                    f"{orphan.get('persona_instance_id')} (no live roster row)"
                )
            # Named individually for the same reason orphans are, and with the
            # REASON on the line: the four buckets have two different cures, and
            # a bare count would send an operator to reap a desk that is really
            # a mis-kinded agent. Uncapped, like the orphan block above — the
            # doctor's contract forbids a silent truncation, and a store with
            # enough litter to make this long is a store that needs to see it.
            for litter in census.get("desk_litter") or []:
                print(
                    f"  desk litter: {litter.get('workspace_id')}/"
                    f"{litter.get('actor_key')} item={litter.get('item_id')} "
                    f"persona={litter.get('persona_id')} "
                    f"({litter.get('reason')})"
                )
            # Every HOLDER on the line, for the same reason the orphan block
            # names its actor: the repair is to remove or re-place one of them,
            # and a row that named only the item id would leave the operator to
            # go find out which two rows are claiming it.
            for duplicate in census.get("duplicate_placements") or []:
                holders = ", ".join(
                    str(holder.get("actor_key"))
                    for holder in duplicate.get("holders") or []
                )
                print(
                    f"  duplicate placement: {duplicate.get('workspace_id')}/"
                    f"{duplicate.get('item_id')} held by {holders} "
                    f"({duplicate.get('reason')})"
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
