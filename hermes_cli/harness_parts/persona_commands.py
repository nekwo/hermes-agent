# Loaded by hermes_cli.harness via _load_command_parts(); executed in harness.py globals.
# Keep command bodies here so parser registration stays separate from persona/chat behavior.

# Explicit import header — its rationale lives ONCE, in
# ``hermes_cli/harness_support.py``'s module docstring, which also names the
# two gates that hold it: ruff's F821 for the header being complete, and
# tests/hermes_cli/test_harness_parts_namespace.py for the load-order namespace.

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from agent_runtime import paths
from agent_runtime.call_authorization import CLI_CONSOLE
from agent_runtime.chat_session_scope import is_canonical_session_persistence
from agent_runtime.chat_turn_presence import ChatTurnPresence
from agent_runtime.cli_format import emit_json
from agent_runtime.config import (
    ensure_persisted_personas,
    load_agent_runtime_config,
    mission_chat_clarify_token_binding,
    resolve_mission_chat_max_seconds,
)
from agent_runtime.continuity import return_summary_to_parent_session
from agent_runtime.coordinator_permissions import (
    CoordinatorPermissionScope,
    review_coordinator_budget,
    scope_for_persona,
)
from agent_runtime.dispatch_session_policy import (
    derive_dispatch_title,
    resolve_dispatch_session_decision,
    session_established_payload,
)
from agent_runtime.events import EventLog
from agent_runtime.mcp_admission import LANE_MISSION_CHAT, resolve_mcp_admission
from agent_runtime.mcp_lane import HARNESS_LANE
from agent_runtime.mission_chat_steer import (
    start_active_mission_chat_turn,
    submit_mission_chat_steer,
)
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
from agent_runtime.mission_chat_workdir import mission_chat_workdir_for_persona
from agent_runtime.models import AgentPersona, Event, apply_instance_model_overrides
from agent_runtime.persona_assignments import (
    CHAT_BINDING_CLEARED_REASON_DELETED,
    PERSONA_INSTANCE_ID_PREFIX,
    # The instance tier's skill-id discipline (token safety, dedupe, cap 40).
    # IMPORTED rather than re-spelled: two spellings of one cap is exactly how
    # `agent_create.MAX_SKILLS`'s comment says drift starts, and the template
    # tier must normalize the same way the instance tier does or the two
    # surfaces disagree about what an operator just typed.
    _safe_skill_overrides,
    PersonaAssignmentStore,
    PersonaInstanceRetireError,
    PersonaInstanceStore,
    RetiredPersonaInstanceError,
    StaleModelOverrideWrite,
    canonical_chat_instance_id,
    canonical_persona_instance_id,
    chat_session_owner_instance_id,
    migrate_retired_persona_assignment_task_ids,
    normalize_persona_id as _normalize_cli_persona_id,
    normalize_persona_or_template_id as _normalize_cli_persona_or_template_id,
    persona_assignment_summary,
    persona_chat_session_id_for,
    persona_id_from_instance_id as _persona_id_from_instance_id,
    persona_instance_id_for,
    persona_instance_summary,
    personas_equal,
    resolve_default_chat_session_id_for_instance,
    safe_assignment_text,
    safe_assignment_token,
    safe_optional_token,
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
from agent_runtime.persona_chat_durability import (
    PersonaChatPersistenceError,
    default_persona_session_db as _default_persona_session_db,
    ensure_persona_chat_session as _ensure_persona_chat_session,
    persona_chat_persistence_failed as _persona_chat_persistence_failed,
)
from agent_runtime.persona_chat_mints import PersonaChatMintError, reserve_persona_chat_mint
from agent_runtime.persona_runtime import GPTPersonaRuntime, chat_lane_capability_drops
from agent_runtime.personas import profile_chat_toolsets, profile_persona_resolution
from agent_runtime.prompt_observability import (
    attach_prompt_observability_turn_results,
    mission_chat_prompt_observability,
    persist_prompt_observability_context,
    slim_chat_final_observability,
    turn_usage_from_result,
)
from agent_runtime.root_observability import attach_root_observability
from agent_runtime.states import WorkerSessionState
from agent_runtime.store import AgentStore
from agent_runtime.tool_permissions import (
    ChatToolPermissionStore,
    default_permission_mode,
    permission_state_for_chat,
)
from agent_runtime.tool_turn_history import persist_tool_turn_actual
from agent_runtime.tool_visibility import ToolVisibilityOptions, resolve_tool_visibility
from hermes_cli.flag_binding import list_flag_or_absent, list_flag_or_empty
from hermes_cli.harness_support import (
    PERSONA_CHAT_SESSION_SOURCE,
    _list_envelope,
    _print_stage42,
    _sort_rows,
)
from hermes_constants import get_hermes_home
from hermes_time import now


def _persona_chat_fault_injection(boundary: str) -> None:
    """Named live-proof seam; inert unless the exact boundary is requested."""

    requested = str(os.environ.get("HERMES_PERSONA_CHAT_FAULT_INJECTION", "")).strip()
    if requested == boundary:
        raise RuntimeError(f"injected persona chat fault at {boundary}")

def _cmd_persona_list(args) -> int:
    cfg = load_agent_runtime_config()
    store = PersonaInstanceStore()
    personas = ensure_persisted_personas(cfg)
    personas_by_id = {str(getattr(persona, "id", "") or ""): persona for persona in personas}
    # S56 made the roster unconditional; the two `enterprise_worker_sessions`
    # gates went with the block, and S56 kept `feature_enabled` /
    # `assignment_store_enabled` on the reply "so operator tooling that reads
    # them keeps parsing". A 2026-08-31 trace found no such reader: not in this
    # repo (the only other `feature_enabled` is skills-sync's, a different key
    # on a different reply) and not in the launcher, whose `lib/` never names
    # either and whose CLI contract dump covers argv only. Both were therefore
    # constants that no one read and that could only ever say `true` — the same
    # always-true shape AX2 took off the snapshot wire in `s76` — so they are
    # gone from this reply too. The keys a caller actually branches on (`ok`,
    # the rows) are untouched.
    instances = store.ensure_for_personas(personas)
    data = {
        "persona_instances": [
            persona_instance_summary(instance, personas_by_id.get(str(getattr(instance, "persona_id", "") or "")))
            for instance in instances
        ],
    }
    if args.json:
        print(emit_json(data))
    else:
        for instance in data["persona_instances"]:
            print(f"{instance['persona_instance_id']}: {instance['display_name']} state={instance['state']} assignment={instance['current_assignment_id'] or '-'}")
    return 0


def _cmd_persona_show(args) -> int:
    cfg = load_agent_runtime_config()
    store = PersonaInstanceStore()
    personas = ensure_persisted_personas(cfg)
    personas_by_id = {str(getattr(persona, "id", "") or ""): persona for persona in personas}
    store.ensure_for_personas(personas)
    value = str(args.persona_id_or_instance_id or "").strip()
    instance_id = value if value.startswith("personainst_") else persona_instance_id_for(_normalize_cli_persona_id(value))
    try:
        instance = store.get(instance_id)
    except Exception:
        data = {"ok": False, "error": f"persona instance not found: {value}"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    assignments = PersonaAssignmentStore().list_for_persona(instance.persona_id)
    data = {
        "ok": True,
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
    # No flag ⇒ the RUNTIME DEFAULT, so the preview describes what a real turn
    # gets. Hardcoding ``profile_default`` here would have made every preview
    # report the bounded shape while every turn ran under the configured default.
    permission_mode = str(args.permission_mode or "").strip() or default_permission_mode()
    visibility = resolve_tool_visibility(
        persona,
        ToolVisibilityOptions(
            permission_mode=permission_mode,
            permission_source="cli_preview",
            repo_scope=args.repo_scope,
            workdir=args.workdir,
            session_id=args.session_id,
            # S27: no ``task_id``/``goal_id``. Their ``--task``/``--goal`` flags
            # were the only writers and nothing emitted them (the Launcher's
            # permission-preview argv never carried either), so this preview
            # could only ever correlate against a mission record deleted in S8.
            # The fields themselves stay on ToolVisibilityOptions -- the CHAT
            # lane fills them from the live persona-instance row.
            # This command IS the harness lane; say so rather than letting the
            # lane be inferred from argv.
            entry_point_lane=HARNESS_LANE,
            # G5: account for what the CHAT lane takes AWAY from this persona,
            # under the mode the operator is asking about (``--permission-mode
            # unbounded`` genuinely bypasses the cost policy and so honestly
            # reports no drops). Accounting only — the resolved tool list is
            # unchanged; these rows explain absences it would otherwise show as
            # nothing at all.
            chat_lane_capability_drops=chat_lane_capability_drops(
                persona,
                session_id=args.session_id,
                permission_mode=permission_mode,
            ),
            mission_chat_workdir=mission_chat_workdir_for_persona(persona),
        ),
    )
    data = {"ok": True, "tool_visibility": visibility}
    # Inspection only: resolve_mcp_admission is pure policy — it never connects
    # to or registers an MCP server — so an operator can read exactly what a
    # persona WOULD be admitted before the kill switch is ever flipped.
    if getattr(args, "explain_mcp", False):
        data["mcp_admission"] = resolve_mcp_admission(
            persona,
            lane=LANE_MISSION_CHAT,
            permission_mode=permission_mode,
        ).explain()
    # Same shape, same guarantee, the other gate: what the TERMINAL safety
    # envelope will do to this persona's mission-chat lane. Rendered entirely
    # from the canonical envelope authorities (``explain_terminal_envelope`` +
    # ``hard_floor_command_classes``) — no parallel derivation of the taxonomy,
    # the stage floor, or the grant table lives here.
    if getattr(args, "explain_envelope", False):
        from agent_runtime.terminal_envelope_explain import (
            explain_persona_terminal_envelope,
        )

        data["terminal_envelope"] = explain_persona_terminal_envelope(
            persona, session_id=args.session_id, permission_mode=permission_mode
        )
    if args.json:
        print(emit_json(data))
    else:
        print(f"{visibility['persona_id']}: {visibility['final_tool_count']} tools")
        # S0a A2: say WHERE the capability came from. Before this, an operator
        # reading a preview had no way to tell a profile declaration from the
        # lane default — or to see that the persona-level ``toolsets`` list in
        # the config/store was being ignored.
        declaration = visibility.get("toolset_declaration") or {}
        if declaration:
            declared = ", ".join(declaration.get("declared") or []) or "-"
            where = declaration.get("config_path") or "no profile config"
            print(f"toolsets: {declared} ({declaration.get('source')}, {where})")
            persona_list = declaration.get("persona_list") or []
            if persona_list:
                print(
                    "persona-level toolsets list ignored (legacy; delete it from "
                    f"agent_runtime.personas.{visibility['persona_id']}.toolsets): "
                    + ", ".join(persona_list)
                )
        envelope = data.get("terminal_envelope")
        if envelope is not None:
            from agent_runtime.terminal_envelope_explain import (
                render_terminal_envelope_explanation,
            )

            for line in render_terminal_envelope_explanation(envelope):
                print(line)
        admission = data.get("mcp_admission")
        if admission is not None:
            print(
                f"mcp admission ({admission['lane']}, role={admission['role']}, "
                f"mode={admission['permission_mode']}): "
                f"{'enabled' if admission['enabled'] else 'DISABLED'}"
            )
            print(f"  requested: {', '.join(admission['requested']) or '-'}")
            print(f"  admitted:  {', '.join(admission['admitted']) or '-'}")
            # What would actually REGISTER. An empty include is the launcher's
            # full-capability glob ("everything this server advertises"), not an
            # empty admission — say which, or the operator has to infer it.
            for server, include in sorted((admission.get("tool_include") or {}).items()):
                shape = (
                    f"{len(include)} tool(s): {', '.join(include)}"
                    if include
                    else "every tool the server advertises (no include filter)"
                )
                print(f"  include {server}: {shape}")
            for row in admission["denied"]:
                print(f"  denied {row['server']} ({row['code']}): {row['summary']}")
        if visibility["blocked_tools"]:
            print("blocked:")
            for item in visibility["blocked_tools"]:
                print(f"  {item['name']} ({item['reason']})")
        # A declared-but-unregistered capability is a real gap in what this
        # persona can do here. Printing it beside the tool count is what stops
        # the count from being read as the whole story.
        for failure in visibility.get("requirement_failures") or []:
            print(f"requirement failure: {failure.get('code')}")
            print(f"  {failure.get('summary')}")
            if failure.get("fix_hint"):
                print(f"  fix: {failure['fix_hint']}")
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
    store = PersonaAssignmentStore()
    if args.persona_id:
        assignments = store.list_for_persona(_normalize_cli_persona_id(args.persona_id))
    else:
        assignments = store.list_all()
    data = {
        "ok": True,
        "assignments": [persona_assignment_summary(item) for item in assignments],
    }
    if args.json:
        print(emit_json(data))
    else:
        for item in data["assignments"]:
            print(f"{item['assignment_id']}: {item['persona_id']} {item['kind']} state={item['state']} task={item['task_id'] or '-'}")
    return 0


def _cmd_persona_assignment_task_id_migration(args) -> int:
    data = migrate_retired_persona_assignment_task_ids(
        dry_run=bool(getattr(args, "dry_run", False))
    )
    if args.json:
        print(emit_json(data))
    else:
        mode = "preview" if data["dry_run"] else "apply"
        print(
            f"assignment task-id migration {mode}: scanned={data['scanned']} "
            f"eligible={data['eligible']} archived={data['archived']} "
            f"held={len(data['held'])}"
        )
    return 0 if data["ok"] else 2


#: UC-H3. The JSON-RPC error-code family, mapped to the harness exit-code
#: taxonomy (2 = bad request, 3 = missing, 4 = conflict, 1 = internal). Keyed on
#: the CODE rather than the reason on purpose: the code family is what the
#: service guarantees, while reasons are added freely and an unmapped one must
#: not silently become exit 0. Note `persona_not_found` therefore exits 2, not
#: the 3 that `ERROR_EXIT_CODES` gives that spelling — it arrives under
#: ERR_INVALID_PARAMS because it is refused by the request normaliser, and the
#: two lanes agreeing about WHY beats either agreeing with a different table.
_AGENT_CREATE_EXIT_CODES = {-32602: 2, 4001: 3, 4090: 4, -32000: 1}


def _cli_create_persona(persona_id: str):
    """The CLI's richer persona resolution, with the RPC lane's typed fault.

    RD-H6 item 2. ``_persona_by_id`` reads the roster through
    ``ensure_persisted_personas`` — the exact call ``agent_create.persona_roster``
    wraps into :class:`PersonaRosterUnavailable`, but UNWRAPPED here, and the
    config load above it is unwrapped too. So a config this process could not
    read left ``harness agent create`` printing a traceback where
    ``runtime.agent.create`` answered a typed ``persona_roster_unavailable``
    refusal naming the runtime as the subject. The fault was identical; only the
    rendering differed, and the argv one blamed nothing and named no cure.

    ``except Exception`` deliberately mirrors :func:`persona_roster`'s own net,
    for the same reason it has one: the roster read reaches YAML parsing, the
    filesystem and the persona store, and enumerating that fault surface here
    would leave the un-enumerated remainder tracebacking — which is the defect.
    It is narrow in SCOPE instead: exactly the roster read, nothing after it.
    A bad id is NOT caught here — ``_persona_by_id`` answers ``None`` for that,
    and the service's ``persona_not_found`` refusal is the one that must answer
    it (collapsing the two would send an operator hunting a typo that does not
    exist, which is the distinction :class:`PersonaRosterUnavailable` exists for).
    """

    from agent_runtime.agent_create import PersonaRosterUnavailable

    try:
        return _persona_by_id(load_agent_runtime_config(), persona_id)
    except Exception as exc:  # noqa: BLE001 — re-raised as the typed fault
        raise PersonaRosterUnavailable(str(exc)) from exc


def _cmd_agent_create(args) -> int:
    """`harness agent create` — one call places an agent.

    The unified door the operator asked for: it calls
    ``agent_create.perform_agent_create``, which is the SAME function
    ``runtime.agent.create`` answers with, so every RESULT field a script reads
    here is the field it would read off the wire — ``position`` and ``actor``
    (plan D2/D11) included, which is what lets a script place an agent WITHOUT
    ``--pos`` and still learn exactly where it went and what row was written. That is the point — a lane
    switch must not be a behaviour change. Two keys are envelope-only and have
    no wire counterpart: ``ok`` (the exit status) and ``resolution`` (the
    root-observability block every ``--json`` harness verb stamps).

    It works with no ``harness serve`` running: every lock in the path (the
    reservation lock, the persona-instance lock, the office lock) is a
    cross-process FILE lock, and the argv fallback lanes already write beside a
    live serve today.

    `--skill` (repeatable) assigns skills to the NEW instance, and is the argv
    twin of the RPC's `skills` param — the service installs and hash-verifies
    every canonical id before it assigns, and refuses rather than handing an
    agent a stale copy. Omitted, the instance inherits its persona's skills.
    A skills refusal keeps the placement: the printed `rolled_back: false` is
    the literal truth there, and re-running with the SAME `--idempotency-key`
    resumes the skills phase alone instead of minting a second agent.

    Why this and not `persona instance create --add-instance`: that verb never
    writes a placement (R#37's shape — there is no office write anywhere in the
    handler), so it leaves a roster row with no desk. It stays as the
    roster-only door; this one is the placement door.
    """

    from agent_runtime.agent_create import (
        PersonaRosterUnavailable,
        perform_agent_create,
        roster_unavailable_outcome,
    )

    try:
        persona_id = _normalize_cli_persona_or_template_id(args.persona_id)
    except ValueError as exc:
        data = {"ok": False, "reason": "persona_id_required", "error": str(exc)}
        print(emit_json(data) if args.json else data["error"])
        return 2

    raw_position = getattr(args, "pos", None)
    position = list(raw_position) if raw_position is not None else None
    if position is not None and len(position) == 2:
        # argparse hands these over as strings; the service refuses anything
        # non-finite, so a bad value stays ONE refusal rather than an
        # argparse traceback here and a typed error there.
        try:
            position = [float(position[0]), float(position[1])]
        except (TypeError, ValueError):
            pass

    params = {
        "persona_id": persona_id,
        "workspace_id": getattr(args, "workspace_id", None),
        "idempotency_key": (
            safe_assignment_text(getattr(args, "idempotency_key", None), limit=240)
            or f"cli-{uuid.uuid4().hex}"
        ),
    }
    # OMITTED, not an explicit ``None`` (plan S2/D2). ``--pos`` is optional now,
    # and the service reads an ABSENT position as "the operator did not aim, let
    # the layout policy choose". Sending ``position: []`` — which this lane did
    # when the flag was required-but-empty — is a malformed aim and refuses
    # ``position_invalid``, so the omission has to reach the service as an
    # omission.
    if position is not None:
        params["position"] = position
    # Same omission rule, one flag over (plan D5). `--skill` absent must reach
    # the service as an ABSENT key: absent leaves `skill_overrides` at `None`
    # (inherit the persona's, live) while `[]` writes an explicit "no skills"
    # override. Sending `skills: []` for an operator who never typed the flag
    # is the exact `None -> []` collapse this slice fixes one handler below.
    requested_skills = list_flag_or_absent(args, "skills")
    if requested_skills is not None:
        params["skills"] = requested_skills
    for key, value in (
        ("display_name", getattr(args, "display_name", None)),
        ("placement_id", getattr(args, "placement_id", None)),
        ("realm_id", getattr(args, "realm_id", None)),
        ("folder", getattr(args, "folder", None)),
        ("correlation_id", getattr(args, "correlation_id", None)),
    ):
        # Omitted stays OMITTED rather than becoming an explicit ``None``: the
        # service distinguishes "no placement_id, mint one" from "a placement
        # id that will not tokenise, refuse".
        if value is not None:
            params[key] = value

    # RD-H6 item 2. The CLI resolves its own richer persona object BEFORE the
    # service runs, so the service's typed roster refusal cannot cover this read
    # — and unwrapped it tracebacked where `runtime.agent.create` answered
    # `persona_roster_unavailable`. Same fault, same reason, both lanes; the
    # refusal falls through to the ONE rendering arm below, so it also inherits
    # the same exit code and the same root-observability envelope.
    # The CLI half of the front-door gate (chokepoint plan A4-ii), the mirror of
    # what `serve_rpc.handle_request` runs for `runtime.agent.create`. Asked
    # BEFORE the roster read for the same reason the coordinator review is asked
    # before it one handler over: a caller who may not place an agent should be
    # told that, not handed a probe of which persona ids exist. Today it always
    # allows — see `_console_denial`.
    denial = _console_denial("runtime.agent.create")
    if denial is not None:
        from agent_runtime.agent_create import AgentCreateOutcome, AgentCreateRefusal

        outcome = AgentCreateOutcome(refusal=AgentCreateRefusal(**denial))
    else:
        try:
            persona = _cli_create_persona(persona_id)
        except PersonaRosterUnavailable as exc:
            outcome = roster_unavailable_outcome(exc)
        else:
            # ``CLI_CONSOLE`` travels INTO the service as well (Stage A6).
            # The door's own gate above is this lane's front door — the mirror
            # of ``handle_request``'s — and the backstop is the second,
            # independent evaluation at the verb itself. Passing the identity
            # rather than letting the service default to it is what makes the
            # door's authority explicit at the call: the default is for callers
            # that have no identity to give.
            outcome = perform_agent_create(
                params, persona=persona, caller=CLI_CONSOLE
            )

    if outcome.refusal is not None:
        refusal = outcome.refusal
        # Root-observability: a create that answered out of the WRONG runtime
        # root refuses just as plausibly as one that answered out of the right
        # one — `persona_not_found` against an empty roster is exactly the
        # well-formed-wrong-answer class the resolution block exists for.
        data = attach_root_observability({
            "ok": False,
            "error": refusal.message,
            **refusal.data,
            "next_expected": (
                "fix the named field and re-run; a refused create wrote nothing "
                "unless it says rolled_back: false"
            ),
        })
        print(emit_json(data) if args.json else data["error"])
        return _AGENT_CREATE_EXIT_CODES.get(refusal.code, 1)

    data = attach_root_observability({"ok": True, **outcome.result})
    print(
        emit_json(data)
        if args.json
        else (
            f"placed {data['persona_instance_id']} as {data['actor_key']} "
            f"on chat {data['default_chat_session_id']}"
        )
    )
    return 0


#: Same family map ``agent create`` uses, over the codes ``agent_retire``
#: answers in. Not re-derived at the call site: an exit code guessed beside a
#: print statement is the second taxonomy this file already retired once.
_AGENT_RETIRE_EXIT_CODES = {-32602: 2, 4001: 3, 4090: 4}


def _console_denial(action: str) -> dict | None:
    """The CLI's half of the front-door gate. ``None`` when the call may run.

    The MIRROR of ``serve_rpc.handle_request``'s check (chokepoint plan, Ruling
    A option (b)), evaluated by the same predicate against the same tier
    vocabulary, so the two doors onto ``perform_agent_create`` /
    ``perform_agent_retire`` cannot answer differently.

    The identity is a CONSTANT — ``CLI_CONSOLE`` — and takes nothing off the
    invocation. That is the whole point: an argv-derived identity is what
    ``coordinator_permissions`` already is, and rebuilding it here would put a
    self-declaration at the one door the machine owner types into. The operator
    at their own shell IS the console; there is nothing to prove and nothing to
    read.

    So today this returns ``None`` unconditionally, and that is honest rather
    than vestigial: it is the grandfather clause §2 of the plan names, spelled as
    a call so it is greppable, so both retire doors provably share it, and so the
    day a non-console CLI identity exists (a sudo-less service account, a
    delegated shell) the refusal is a predicate edit and not a new concept.

    The refusal it WOULD render is shaped as the two service functions' own
    refusal kwargs, so both CLI envelopes print it through the arm they already
    have for a refused create/retire.
    """

    from agent_runtime.call_authorization import (
        CLI_CONSOLE,
        TIER_CONSOLE,
        authorize_call,
    )
    from agent_runtime.serve_rpc import ERR_HANDLER_FAILED

    decision = authorize_call(TIER_CONSOLE, CLI_CONSOLE)
    if decision.ok:
        return None
    return {
        "code": ERR_HANDLER_FAILED,
        "message": f"{action} requires the {decision.tier} tier",
        "data": {
            **decision.refusal_data(),
            "next_expected": (
                "run this verb from an operator console on the install that owns "
                "this runtime root"
            ),
        },
    }


def _agent_retire_outcome(args):
    """The ONE retire the CLI performs, whichever verb the operator typed.

    ``harness agent retire <id>`` and ``harness persona instance retire <id>``
    are two doors onto ``agent_retire.perform_agent_retire`` — the same function
    ``runtime.agent.retire`` answers with — so a lane switch is not a behaviour
    change and the ack is IDENTICAL down to the key order. The two handlers
    below differ only in their envelope, which is the operator surface each has
    always had, and in the coordinator gate, which is `persona instance`'s.

    ``correlation_id`` is read through ``getattr`` with the same ``None``
    default as its siblings, so an operator who does not type the flag on either
    door reaches the store with no token and gets the ack they always got.

    **The authorization identity is the same on both doors, because it is minted
    here** (chokepoint plan A4-ii/iii). Canon 06 recorded the asymmetry as "one
    retire consults the coordinator gate and the other does not, on the same
    service function" — and the survey found it was worse than an asymmetry: the
    consulted gate never ran either, because it only recognises
    ``--requested-by coordinator`` and the two spellings anyone actually sends
    are ``cli`` (the CLI's own default) and ``launcher``.

    The asymmetry disappears not by giving `agent retire` the coordinator gate —
    that gate answers a different question, see
    ``agent_runtime.coordinator_permissions`` — but because BOTH doors now carry
    ``CLI_CONSOLE``, evaluated by the same predicate the RPC front door uses. It
    is minted in this function rather than in the two handlers precisely so the
    two cannot drift apart a second time: there is one retire, so there is one
    identity.

    Today it always allows — the operator at the machine's own shell IS the
    console — and that is the grandfather clause, made greppable instead of
    implicit in an absent check.

    BOTH doors publish ``--correlation-id``. S8b gave it to `agent retire` alone,
    on the reasoning that only it is the scripted inverse of `agent create
    --correlation-id` and that "no gesture behind it" was the truth for `persona
    instance retire`. That was wrong about its own largest caller: the launcher's
    `persona.instance.retire` argv capability IS this door, fired from
    ``MissionOfficeLayoutController.retireAgent``'s ``Unavailable`` arm, and that
    method takes ``correlationId`` as a REQUIRED parameter. The token therefore
    existed on every launcher retire and was dropped by precisely the arm that
    runs when the RPC lane is degraded — so the create half and the retire half
    of one gesture landed in two correlation spaces on the lane where a single
    grep over the event log is the only join an operator has (S8b-b).
    """

    from agent_runtime.agent_retire import AgentRetireOutcome, AgentRetireRefusal
    from agent_runtime.agent_retire import perform_agent_retire

    denial = _console_denial("runtime.agent.retire")
    if denial is not None:
        return AgentRetireOutcome(refusal=AgentRetireRefusal(**denial))

    return perform_agent_retire(
        {
            "persona_instance_id": getattr(args, "persona_instance_id", None),
            "reason": getattr(args, "reason", None),
            "requested_by": getattr(args, "requested_by", None),
            "correlation_id": getattr(args, "correlation_id", None),
        },
        # Stage A6's backstop gets this door's identity too — the SAME constant
        # the gate above evaluated, minted in this one function so the two
        # retire doors cannot drift apart a second time.
        caller=CLI_CONSOLE,
    )


def _cmd_agent_retire(args) -> int:
    """`harness agent retire` — one call takes an agent off the level.

    The inverse of `harness agent create`, and its exact twin in shape: it calls
    ``agent_retire.perform_agent_retire``, which is the SAME function
    ``runtime.agent.retire`` answers with, so every RESULT field a script reads
    here is the field it would read off the wire — ``archived_actor_keys`` and
    ``office_archive_failures`` included, which is what lets a script learn
    whether the desk actually left the canvas instead of assuming it did.

    Works with no ``harness serve`` running: every lock in the path is a
    cross-process FILE lock.

    A second retire of the same id is NOT an error — it answers the same ack
    with ``already_retired: true``, so a script that lost its first ack (or a
    cron that runs twice) is idempotent by construction.
    """

    outcome = _agent_retire_outcome(args)

    if outcome.refusal is not None:
        refusal = outcome.refusal
        # Root-observability for the same reason the create carries it: a
        # ``not_found`` answered out of the WRONG runtime root refuses just as
        # plausibly as one answered out of the right one.
        data = attach_root_observability({
            "ok": False,
            "error": refusal.message,
            **refusal.data,
            "next_expected": (
                "a refused retire archived nothing; fix the named condition "
                "(or retire the placement it names) and re-run"
            ),
        })
        print(emit_json(data) if args.json else data["error"])
        return _AGENT_RETIRE_EXIT_CODES.get(refusal.code, 1)

    result = outcome.result
    data = attach_root_observability({"ok": True, **result})
    if args.json:
        print(emit_json(data))
    else:
        keys = ", ".join(result["archived_actor_keys"]) or "no actors"
        failures = result["office_archive_failures"]
        suffix = f" ({len(failures)} office archive failure(s))" if failures else ""
        replay = " (already retired)" if result.get("already_retired") else ""
        print(
            f"retired {result['persona_instance_id']} -> {result['archive_path']}; "
            f"archived {keys}{suffix}{replay}"
        )
    return 0


def _placement_discriminability_refusal(placement_id: str) -> dict | None:
    """R1's fence for the two ``--add-instance`` doors, or ``None`` to proceed.

    These verbs do not pass through ``agent_create``, so the fence there covers
    neither of them; the SHAPE and the SENTENCE still come from the one
    authority in ``agent_runtime.models`` rather than being re-spelled per door.

    Returns a payload rather than raising because both callers already answer
    their own placement refusals this way (``placement_id is required when
    add_instance is true``), and neither catches ``ValueError`` around the
    store call — a raise here would surface as a traceback, not a refusal.
    """

    from agent_runtime.models import (
        PLACEMENT_ID_NOT_DISCRIMINABLE_REASON,
        looks_like_deliberate_placement,
        placement_id_not_discriminable_message,
    )

    if looks_like_deliberate_placement(placement_id):
        return None
    return {
        "ok": False,
        "reason": PLACEMENT_ID_NOT_DISCRIMINABLE_REASON,
        "error": placement_id_not_discriminable_message(placement_id),
        "placement_id": placement_id,
        "next_expected": (
            "re-run without --placement-id to have a discriminable one minted, "
            "or send the <persona-token>_agent_<hex8> shape"
        ),
    }


def _cmd_persona_instance_create(args) -> int:
    # Function-local: this file is exec'd into harness.py's globals, so a
    # module-level import here would need a matching harness.py import or it
    # is a NameError on a LIVE turn. The turn-outcome vocabulary is owned by
    # agent_runtime.mission_chat_outcome; nothing re-spells its values.
    from agent_runtime.agent_create import require_known_persona
    from agent_runtime.mission_chat_outcome import ChatErrorKind
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
        auth = review_coordinator_budget(
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
    # UC-H4. Until now this handler minted a roster row and a chat root for any
    # id that TOKENISED — `--persona qa_agent` against a roster of
    # base/backend_dev/dev/neko_supervisor/qa produced durable artifacts bound
    # to nothing, and `_persona_by_id` returning None went unchecked three
    # lines above. Fail-open becomes fail-closed for that class only; the
    # `profile:` carve-out (D-U1) and every roster-sourced caller are
    # untouched, and the refusal is the SAME spelling the unified lane uses.
    #
    # Asked AFTER the coordinator gate on purpose: an unauthorised actor should
    # be told it is unauthorised, not handed a roster probe. Both refusals are
    # still before any store write.
    refusal = require_known_persona(persona_id, persona)
    if refusal is not None:
        print(emit_json(refusal) if args.json else refusal["error"])
        return 2
    if display_name:
        try:
            if add_instance:
                if not placement_id:
                    data = {"ok": False, "error": "placement_id is required when add_instance is true"}
                    print(emit_json(data) if args.json else data["error"])
                    return 2
                refusal = _placement_discriminability_refusal(placement_id)
                if refusal is not None:
                    print(emit_json(refusal) if args.json else refusal["error"])
                    return 2
                instance = PersonaInstanceStore().add_instance(
                    persona_id=persona_id,
                    placement_id=placement_id,
                    display_name=display_name or safe_assignment_text(args.title, limit=120) or persona_id,
                    session_id=getattr(args, "session_id", None),
                    workspace_id=safe_assignment_token(getattr(args, "workspace_id", None)) or None,
                    realm_id=safe_assignment_token(getattr(args, "realm_id", None)) or None,
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
        except RetiredPersonaInstanceError as exc:
            data = _retired_persona_instance_payload(exc)
            print(emit_json(data) if args.json else data["error"])
            return 2
        except PersonaChatPersistenceError as exc:
            # The mint itself now refuses rather than binding a root it could not
            # persist, so this frame arrives from INSIDE the store. Same shape as
            # the post-bind one below; there is simply no instance yet to name.
            data = {
                "ok": False,
                "error_kind": ChatErrorKind.CHAT_SESSION_PERSIST_FAILED,
                "persistence_operation": exc.operation,
                "error": str(exc),
                "persona_id": persona_id,
                "next_expected": "restore canonical persona chat transcript storage and retry",
            }
            print(emit_json(data) if args.json else data["error"])
            return 2
        try:
            _ensure_persona_chat_session(
                session_db=_default_persona_session_db(),
                session_id=instance.default_chat_session_id,
                persona_id=instance.persona_id,
                title=f"{instance.display_name} chat",
                required=True,
            )
        except PersonaChatPersistenceError as exc:
            data = {
                "ok": False,
                "error_kind": ChatErrorKind.CHAT_SESSION_PERSIST_FAILED,
                "persistence_operation": exc.operation,
                "error": str(exc),
                "persona_id": instance.persona_id,
                "persona_instance_id": instance.id,
                "session_id": instance.default_chat_session_id,
                "next_expected": "restore canonical persona chat transcript storage and retry",
            }
            print(emit_json(data) if args.json else data["error"])
            return 2
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
            "default_chat_session_id": instance.default_chat_session_id,
            "chat_session_id": instance.default_chat_session_id,
            "session_id": instance.default_chat_session_id,
            "chat_busy": False,
            "killed_previous": bool(kill_active),
            "add_instance": add_instance,
            "placement_id": placement_id or None,
            "coordinator_permission_scope": asdict(coordinator_scope) if coordinator_scope is not None else None,
            "next_expected": "agent profile created; refresh Harness snapshot for the profile, chat, and scene placement state",
        }
        print(emit_json(data) if args.json else f"created {instance.id} on chat {instance.default_chat_session_id}")
        return 0
    # S70: the display-name-less branch used to queue a "free-floating persona
    # assignment" (and optionally auto-run one bounded turn beside the canonical
    # chat lane). The queue's only durable consumer was the tick loop the
    # 2026-07-30 chat-only purge removed — a queued row dead-ended forever, the
    # advertised `persona instance run-once` follow-up verb never existed, and
    # the auto-run turn was a second, parallel turn authority beside
    # `mission-chat message`. The lane is retired; refuse loudly instead of
    # silently minting work nothing will ever pick up.
    data = {
        "ok": False,
        "error": (
            "persona instance create requires --display-name (an Agent Profile "
            "or placement mint); the free-floating assignment lane is retired"
        ),
        "persona_id": persona_id,
        "next_expected": (
            "pass --display-name to create the agent profile/placement, then "
            "send messages with `harness mission-chat message`"
        ),
    }
    print(emit_json(data) if args.json else data["error"])
    return 2


def _cmd_persona_instance_open_chat(args) -> int:
    # Function-local: this file is exec'd into harness.py's globals, so a
    # module-level import here would need a matching harness.py import or it
    # is a NameError on a LIVE turn. The turn-outcome vocabulary is owned by
    # agent_runtime.mission_chat_outcome; nothing re-spells its values.
    from agent_runtime.mission_chat_outcome import ChatErrorKind
    cfg = load_agent_runtime_config()
    persona_id = _normalize_cli_persona_or_template_id(args.persona_id)
    persona = _persona_by_id(cfg, persona_id)
    coordinator_id = _coordinator_actor_id(args)
    coordinator_scope = None
    if coordinator_id and bool(getattr(args, "add_instance", False)):
        coordinator_scope = _coordinator_scope_from_args(args, cfg, persona)
        auth = review_coordinator_budget(
            "persona.instance.open_chat",
            coordinator_scope,
            actor=coordinator_id,
            coordinator_id=coordinator_id,
        )
        if not auth.ok:
            data = _coordinator_confirm_payload("persona.instance.open_chat", coordinator_id, auth)
            _emit_persona_open_chat_payload(args, data, plain=data["status"])
            return 2
        coordinator_scope = auth.scope
    elif coordinator_id and bool(getattr(args, "kill_active", False)):
        coordinator_scope = _coordinator_scope_from_args(args, cfg, persona)
        try:
            target = PersonaInstanceStore().get(persona_instance_id_for(persona_id))
        except Exception:
            target = None
        auth = review_coordinator_budget(
            "persona.instance.close",
            coordinator_scope,
            target,
            actor=coordinator_id,
            coordinator_id=coordinator_id,
        )
        if not auth.ok:
            data = _coordinator_confirm_payload("persona.instance.close", coordinator_id, auth)
            _emit_persona_open_chat_payload(args, data, plain=data["status"])
            return 2
    if bool(getattr(args, "new_session", False)):
        return _cmd_persona_instance_open_new_chat(
            args,
            persona_id=persona_id,
            coordinator_scope=coordinator_scope,
        )
    previous_instance = None
    try:
        if bool(getattr(args, "add_instance", False)):
            placement_id = safe_assignment_token(getattr(args, "placement_id", None))
            if not placement_id:
                data = {"ok": False, "error": "placement_id is required when add_instance is true"}
                _emit_persona_open_chat_payload(args, data)
                return 2
            # UC-H4, scoped to --add-instance ONLY. The other branches of this
            # verb REBIND an instance that already exists (and the recorded
            # 2026-07-25 recovery replayed ten of them out of the event log);
            # they mint nothing, so a roster check there would refuse a repair
            # for a persona whose config row went away — the exact workflow the
            # refusal must not break. Minting is the thing being fenced.
            from agent_runtime.agent_create import require_known_persona

            refusal = require_known_persona(persona_id, persona)
            if refusal is not None:
                _emit_persona_open_chat_payload(args, refusal)
                return 2
            # AFTER the roster check, matching the order `persona instance
            # create` already had: "that agent does not exist" is the more
            # fundamental answer than "that id is the wrong shape", and an
            # operator who typed both mistakes should hear the one that is
            # about the agent. Both still refuse before any store write.
            placement_refusal = _placement_discriminability_refusal(placement_id)
            if placement_refusal is not None:
                _emit_persona_open_chat_payload(args, placement_refusal)
                return 2
            try:
                # Local import: this file is exec'd into harness.py globals, and
                # this name is NOT among them — as a free name it raised NameError
                # on every --add-instance and the except below swallowed it,
                # silently disabling the named-placement preservation described in
                # the comment that follows (found by the 2026-07-31 audit).
                from agent_runtime.persona_assignments import (
                    persona_instance_id_for_placement,
                )

                previous_instance = PersonaInstanceStore().get(
                    persona_instance_id_for_placement(placement_id)
                )
            except Exception:
                previous_instance = None
            # A deliberately-placed additional instance ("QA Agent (2)") carries
            # its distinct name so the operator's placement cue survives into the
            # store, the launcher conversational fold (keyed on
            # persona+display_name), and the HUD roster. When the client omits it
            # the honest fallback is the persona's OWN configured display name
            # ("QA Agent"), never the title-cased persona id ("Qa") the store
            # template fallback would otherwise mint.
            #
            # That rule is now ONE copy, in ``agent_runtime.agent_create``, because
            # ``runtime.agent.create`` mints the same placements over the method
            # lane and calling the store directly would have dropped it silently.
            # This lane still passes the persona object it already resolved
            # through its own richer ``_persona_by_id``, so its behaviour is
            # unchanged; the shared function only supplies the fallback ladder.
            from agent_runtime.agent_create import (
                honest_default_display_name as _honest_default_display_name,
            )

            explicit_display_name = safe_assignment_text(getattr(args, "display_name", None), limit=120)
            honest_default_display_name = _honest_default_display_name(
                persona_id, persona
            )
            instance = PersonaInstanceStore().add_instance(
                persona_id=persona_id,
                placement_id=placement_id,
                session_id=args.session_id,
                display_name=explicit_display_name,
                default_display_name=honest_default_display_name,
                workspace_id=safe_assignment_token(getattr(args, "workspace_id", None)) or None,
                realm_id=safe_assignment_token(getattr(args, "realm_id", None)) or None,
            )
            instance = _maybe_stamp_spawned_by(instance, coordinator_id=coordinator_id)
        else:
            if not safe_assignment_text(getattr(args, "session_id", None), limit=200):
                data = {"ok": False, "error": "session_id is required unless add_instance is true"}
                _emit_persona_open_chat_payload(args, data)
                return 2
            # RETIREMENT, asked before the session-existence cutoff below.
            # Retiring a placement archives the row but deliberately leaves its
            # chat on disk, so an operator (or a stale launcher frame) re-opening
            # that thread is asking about something that HAS a typed end-of-life
            # answer — the tombstone and "history preserved". It used to get
            # `unknown_chat_session` instead, which names the wrong fact and
            # offers the wrong next step ("open a server-minted chat root").
            #
            # The owner comes from the session id itself (`persona_chat_<
            # instance>_<hex>` encodes it), so this answers even when the row is
            # archived and no SessionDB entry survives — precisely the case that
            # read as "unknown". Read-only, through the ONE bind seam, so a live
            # row always wins and a legitimately re-created placement is never
            # refused by its own history. A bare persona resolves to the
            # canonical channel, which cannot be retired.
            PersonaInstanceStore().assert_bindable(
                persona_id=persona_id,
                # No session_id: the sibling-steal refusal is a ValueError, and
                # the `foreign_chat_session` guard below already owns that answer
                # with the envelope the operator needs.
                persona_instance_id=(
                    safe_assignment_token(getattr(args, "persona_instance_id", None))
                    or chat_session_owner_instance_id(args.session_id)
                    or None
                ),
            )
            session_db = _default_persona_session_db()
            if (
                is_canonical_session_persistence(session_db)
                and session_db.get_session(args.session_id) is None
            ):
                data = {
                    "ok": False,
                    "error_kind": ChatErrorKind.UNKNOWN_CHAT_SESSION,
                    "error": f"unknown explicit persona chat root: {args.session_id}",
                }
                _emit_persona_open_chat_payload(args, data)
                return 2
            target_instance_id = safe_assignment_token(
                getattr(args, "persona_instance_id", None)
            ) or None
            if is_canonical_session_persistence(session_db):
                session_owner = _persona_chat_session_owner(session_db, args.session_id)
                try:
                    owner_instance = (
                        PersonaInstanceStore().get(session_owner)
                        if session_owner
                        else None
                    )
                except Exception:
                    owner_instance = None
                # Same two-normalizer defect as the mission-chat fence, one verb
                # over: ``safe_assignment_token(owner.persona_id)`` (token form)
                # against ``persona_id``, which is
                # ``_normalize_cli_persona_or_template_id`` output (colon form).
                # ``personas_equal`` folds both sides through one authority; the
                # pin leg is bounded exactly as it is there (ownership proof, not
                # persona proof).
                owner_persona = getattr(owner_instance, "persona_id", None)
                pin_proves_ownership = bool(target_instance_id) and (
                    target_instance_id == session_owner
                )
                persona_ok = personas_equal(owner_persona, persona_id) or (
                    pin_proves_ownership and not safe_assignment_token(owner_persona)
                )
                if (
                    owner_instance is None
                    or not persona_ok
                    or (target_instance_id and target_instance_id != session_owner)
                ):
                    data = {
                        "ok": False,
                        "error_kind": ChatErrorKind.FOREIGN_CHAT_SESSION,
                        "error": f"explicit chat root is not owned by the target instance: {args.session_id}",
                        "persona_id": persona_id,
                        "session_id": args.session_id,
                        "next_expected": "use the server-minted root returned for this exact persona instance",
                    }
                    _emit_persona_open_chat_payload(args, data)
                    return 2
                target_instance_id = session_owner
            try:
                previous_instance = PersonaInstanceStore().get(
                    target_instance_id or persona_instance_id_for(persona_id)
                )
            except Exception:
                previous_instance = None
            try:
                instance = PersonaInstanceStore().open_chat(
                    persona_id=persona_id,
                    persona_instance_id=target_instance_id,
                    session_id=args.session_id,
                    kill_active=bool(getattr(args, "kill_active", False)),
                )
            except ValueError as exc:
                data = {
                    "ok": False,
                    "error_kind": ChatErrorKind.FOREIGN_CHAT_SESSION,
                    "error": safe_assignment_text(str(exc), limit=320),
                    "persona_id": persona_id,
                    "session_id": args.session_id,
                    "next_expected": "open the instance that owns this chat session, or start a fresh thread",
                }
                _emit_persona_open_chat_payload(args, data)
                return 2
    except RetiredPersonaInstanceError as exc:
        data = _retired_persona_instance_payload(exc)
        _emit_persona_open_chat_payload(args, data)
        return 2
    except PersonaChatPersistenceError as exc:
        # ``add_instance``'s mint refuses rather than binding an unpersistable
        # root, so the persist failure can now arrive BEFORE the bind. Same
        # typed frame the post-bind arm below emits.
        data = {
            "ok": False,
            "error_kind": ChatErrorKind.CHAT_SESSION_PERSIST_FAILED,
            "persistence_operation": exc.operation,
            "error": str(exc),
            "persona_id": persona_id,
            "next_expected": "restore canonical persona chat transcript storage and retry",
        }
        _emit_persona_open_chat_payload(args, data)
        return 2
    try:
        _ensure_persona_chat_session(
            session_db=_default_persona_session_db(),
            session_id=instance.default_chat_session_id,
            persona_id=instance.persona_id,
            title=f"{instance.display_name} chat",
            required=True,
        )
    except PersonaChatPersistenceError as exc:
        data = {
            "ok": False,
            "error_kind": ChatErrorKind.CHAT_SESSION_PERSIST_FAILED,
            "persistence_operation": exc.operation,
            "error": str(exc),
            "persona_id": instance.persona_id,
            "persona_instance_id": instance.id,
            "session_id": instance.default_chat_session_id,
            "next_expected": "restore canonical persona chat transcript storage and retry",
        }
        _emit_persona_open_chat_payload(args, data)
        return 2
    # Placed AFTER the transcript row is durable (the ensure above) so the warm
    # can read the root's native tip and revision — the two values that decide
    # whether the first turn REUSES the actor or rebuilds it. See the helper.
    _prewarm_chat_actor_for_open(instance.default_chat_session_id)
    previous_session_id = (
        safe_assignment_text(
            getattr(previous_instance, "default_chat_session_id", None)
            or getattr(previous_instance, "session_id", None),
            limit=200,
        )
        if previous_instance is not None
        else None
    )
    instance_updated_at = _persona_instance_updated_at(instance)
    previous_updated_at = _persona_instance_updated_at(previous_instance)
    binding_changed = (
        previous_instance is None
        or previous_session_id != instance.default_chat_session_id
        or getattr(previous_instance, "mode", None) != instance.mode
        or previous_updated_at != instance_updated_at
    )
    data = {
        "ok": True,
        "persona_instance_id": instance.id,
        "persona_id": instance.persona_id,
        "mode": instance.mode,
        "default_chat_session_id": instance.default_chat_session_id,
        "session_id": instance.default_chat_session_id,
        "previous_session_id": previous_session_id,
        "binding_receipt": {
            "schema_version": 1,
            "persona_instance_id": instance.id,
            "session_id": instance.default_chat_session_id,
            "previous_session_id": previous_session_id,
            "changed": binding_changed,
            "instance_updated_at": instance_updated_at,
        },
        "chat_busy": False,
        "killed_previous": bool(getattr(args, "kill_active", False)),
        "add_instance": bool(getattr(args, "add_instance", False)),
        "placement_id": safe_assignment_token(getattr(args, "placement_id", None)) or None,
        "coordinator_permission_scope": asdict(coordinator_scope) if coordinator_scope is not None else None,
        "next_expected": "resume or send on this chat session to boot the persona instance history",
    }
    _emit_persona_open_chat_payload(
        args,
        data,
        plain=f"opened {instance.id} on chat {instance.default_chat_session_id}",
    )
    return 0


def _persona_instance_updated_at(instance) -> str | None:
    if instance is None:
        return None
    value = getattr(instance, "updated_at", None)
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return safe_assignment_text(value, limit=80) or None


def _cmd_persona_instance_open_new_chat(args, *, persona_id: str, coordinator_scope) -> int:
    """Mint one exact-instance chat root with durable retry semantics."""
    # Function-local: this file is exec'd into harness.py's globals, so a
    # module-level import here would need a matching harness.py import or it
    # is a NameError on a LIVE turn. The turn-outcome vocabulary is owned by
    # agent_runtime.mission_chat_outcome; nothing re-spells its values.
    from agent_runtime.mission_chat_outcome import ChatErrorKind
    if bool(getattr(args, "add_instance", False)):
        return _emit_persona_open_chat_error(
            args,
            error_kind=ChatErrorKind.INVALID_REQUEST,
            error="new_session and add_instance are mutually exclusive",
            persona_id=persona_id,
        )
    if safe_assignment_text(getattr(args, "session_id", None), limit=200):
        return _emit_persona_open_chat_error(
            args,
            error_kind=ChatErrorKind.INVALID_REQUEST,
            error="session_id must be omitted when new_session is true",
            persona_id=persona_id,
        )

    requested_instance_id = safe_assignment_token(
        getattr(args, "persona_instance_id", None)
    )
    target_instance_id = (
        canonical_persona_instance_id(requested_instance_id, persona_id=persona_id)
        if requested_instance_id
        else persona_instance_id_for(persona_id)
    )
    store = PersonaInstanceStore()
    try:
        current = store.get(target_instance_id)
    except Exception:
        return _emit_persona_open_chat_error(
            args,
            error_kind=ChatErrorKind.PERSONA_INSTANCE_NOT_FOUND,
            error=f"persona instance not found: {target_instance_id}",
            persona_id=persona_id,
            persona_instance_id=target_instance_id,
            next_expected="refresh the Harness roster and retry against a live persona instance",
        )
    # Fourth site of the shape: the STORED persona compared raw against
    # ``_normalize_cli_persona_or_template_id`` output. One authority, both sides
    # — a spelling difference is not an instance mismatch.
    if not personas_equal(current.persona_id, persona_id):
        return _emit_persona_open_chat_error(
            args,
            error_kind=ChatErrorKind.PERSONA_INSTANCE_MISMATCH,
            error=(
                f"persona instance {target_instance_id!r} belongs to "
                f"{current.persona_id!r}, not {persona_id!r}"
            ),
            persona_id=persona_id,
            persona_instance_id=target_instance_id,
        )

    try:
        with reserve_persona_chat_mint(
            idempotency_key=getattr(args, "idempotency_key", None),
            persona_id=persona_id,
            persona_instance_id=target_instance_id,
            session_id=persona_chat_session_id_for(target_instance_id),
        ) as mint:
            receipt = mint.receipt
            if receipt.bound:
                # A retry after a confirmed response loss must be observational:
                # return the original root without moving the instance pointer
                # back over a newer chat selected since this mint completed.
                instance = store.get(target_instance_id)
            else:
                # Make the transcript root durable before publishing it as the
                # instance's selected chat. If SessionDB is temporarily
                # unavailable the reserved receipt survives and retry reuses the
                # same root instead of creating a duplicate conversation.
                try:
                    _ensure_persona_chat_session(
                        session_db=_default_persona_session_db(),
                        session_id=receipt.session_id,
                        persona_id=persona_id,
                        title=f"{current.display_name} chat",
                        required=True,
                    )
                except PersonaChatPersistenceError as exc:
                    data = {
                        "ok": False,
                        "error_kind": ChatErrorKind.CHAT_SESSION_PERSIST_FAILED,
                        "persistence_operation": exc.operation,
                        "error": str(exc),
                        "persona_id": persona_id,
                        "persona_instance_id": target_instance_id,
                        "session_id": receipt.session_id,
                        "mission_chat_root_id": receipt.session_id,
                        "idempotent_replay": receipt.idempotent_replay,
                        "mint_receipt_state": receipt.state,
                        "next_expected": "restore canonical persona chat transcript storage and retry with the same idempotency key",
                    }
                    _emit_persona_open_chat_payload(args, data)
                    return 2
                try:
                    instance = store.open_chat(
                        persona_id=persona_id,
                        persona_instance_id=target_instance_id,
                        session_id=receipt.session_id,
                        kill_active=bool(getattr(args, "kill_active", False)),
                    )
                except RetiredPersonaInstanceError as exc:
                    data = _retired_persona_instance_payload(exc)
                    data.update(
                        {
                            "session_id": receipt.session_id,
                            "mission_chat_root_id": receipt.session_id,
                            "idempotent_replay": receipt.idempotent_replay,
                            "mint_receipt_state": receipt.state,
                        }
                    )
                    _emit_persona_open_chat_payload(args, data)
                    return 2
                receipt = mint.mark_bound()
    except PersonaChatMintError as exc:
        return _emit_persona_open_chat_error(
            args,
            error_kind=exc.code,
            error=str(exc),
            persona_id=persona_id,
            persona_instance_id=target_instance_id,
        )

    # The highest-value prewarm in the harness: a freshly minted root has no
    # turn that is not its first, so without this EVERY new chat pays the cold
    # construction on the operator's opening message. The mint is bound and the
    # transcript row is durable by this line.
    _prewarm_chat_actor_for_open(receipt.session_id)
    selected = instance.session_id == receipt.session_id
    data = {
        "ok": True,
        "persona_instance_id": instance.id,
        "persona_id": instance.persona_id,
        "mode": instance.mode,
        "session_id": receipt.session_id,
        "mission_chat_root_id": receipt.session_id,
        "chat_busy": False,
        "killed_previous": bool(getattr(args, "kill_active", False)),
        "add_instance": False,
        "new_session": True,
        "selected": selected,
        "superseded": not selected,
        "idempotent_replay": receipt.idempotent_replay,
        "mint_receipt_state": receipt.state,
        "coordinator_permission_scope": asdict(coordinator_scope)
        if coordinator_scope is not None
        else None,
        "next_expected": "resume or send on this server-minted chat root",
    }
    _emit_persona_open_chat_payload(
        args,
        data,
        plain=f"opened {instance.id} on new chat {receipt.session_id}",
    )
    return 0


def _prewarm_chat_actor_for_open(session_id) -> None:
    """Queue this chat root's resident actor for background construction.

    Stage 2 of ``planned/chat-turn-prep-cost``: the FIRST turn of a chat root
    pays ~3 s of agent construction on the operator's critical path, and a
    freshly minted chat has no turn that is not its first. Building the actor
    when the chat is OPENED moves that cost off the turn.

    Called from the two open-chat arms and NOWHERE else — in particular not from
    ``PersonaInstanceStore.open_chat``, which the send path re-enters on every
    turn: hooking the store method would fire a background construction against
    every live turn, which is the contention the prewarm's yield rule exists to
    avoid. These two arms are the operator's gesture; the launcher fires
    ``persona.instance.open_chat`` when a chat is opened or created and never
    when a message is sent.

    Inert without a resident registry — every CLI one-shot, and any serve with
    ``persona_chat.hot_sessions_enabled`` off — and best effort by contract: an
    open must never fail because a warm could not be queued.
    """

    try:
        from agent_runtime.persona_chat_actor_prewarm import request_chat_actor_prewarm

        request_chat_actor_prewarm(session_id)
    except Exception:
        pass


def _emit_persona_open_chat_payload(args, data: dict, *, plain: str | None = None) -> None:
    """Hand ONE open-chat payload to whoever owns this call's transport.

    The exact seam ``_emit_mission_chat_payload`` is for the send lane, one verb
    over, and it exists for the same reason and against the same alternative.
    ``runtime.persona.instance.open_chat`` (plan C1h, ruling R-C5) is an
    IN-PROCESS second door onto this handler, running on a serve's reader loop —
    so the only other way for it to read the row would be
    ``contextlib.redirect_stdout``, which rebinds ``sys.stdout``
    PROCESS-GLOBALLY and would briefly steal the serve's own frame protocol from
    every other thread on it. That argument is written out in full at
    :func:`_emit_mission_chat_payload`; nothing about it is weaker here.

    ``args.payload_sink`` is the seam, and it is absent on every argparse
    Namespace, so the CLI and the serve's argv bridge are untouched: with no
    sink this prints byte-for-byte what each call site printed before.

    ``plain`` is the non-JSON console line; ``None`` keeps the historical
    ``data["error"]``. Deliberately no ``stream`` arm — opening a chat is not a
    turn and has never had one.
    """

    sink = getattr(args, "payload_sink", None)
    if callable(sink):
        sink(data)
        return
    print(emit_json(data) if args.json else (data["error"] if plain is None else plain))


def _emit_persona_open_chat_error(
    args,
    *,
    error_kind: str,
    error: str,
    persona_id: str,
    persona_instance_id: str | None = None,
    next_expected: str = "correct the open-chat request and retry",
) -> int:
    data = {
        "ok": False,
        "error_kind": error_kind,
        "error": safe_assignment_text(error, limit=400),
        "persona_id": persona_id,
        "persona_instance_id": persona_instance_id,
        "next_expected": next_expected,
    }
    _emit_persona_open_chat_payload(args, data)
    return 2


def _cmd_persona_chat_delete(args) -> int:
    # Function-local: this file is exec'd into harness.py's globals, so a
    # module-level import here would need a matching harness.py import or it
    # is a NameError on a LIVE turn. The turn-outcome vocabulary is owned by
    # agent_runtime.mission_chat_outcome; nothing re-spells its values.
    from agent_runtime.mission_chat_outcome import ChatErrorKind
    cfg = load_agent_runtime_config()
    session_id = safe_assignment_text(getattr(args, "session_id", None), limit=200)
    if not session_id:
        data = {"ok": False, "error": "session_id is required"}
        _emit_persona_open_chat_payload(args, data)
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
    try:
        session_db = _default_persona_session_db()
    except PersonaChatPersistenceError as exc:
        data = {
            "ok": False,
            "session_id": session_id,
            "error_kind": ChatErrorKind.CHAT_SESSION_DB_UNAVAILABLE,
            "persistence_operation": exc.operation,
            "error": str(exc),
        }
        print(emit_json(data) if args.json else data["error"])
        return 2
    owner_instance_id = _persona_chat_session_owner(session_db, session_id)
    if not owner_instance_id:
        owner_instance_id = _persona_chat_bound_owner(session_id)
    try:
        owner_instance = (
            PersonaInstanceStore().get(owner_instance_id)
            if owner_instance_id
            else None
        )
    except Exception:
        owner_instance = None
    session_exists = False
    try:
        session_exists = session_db.get_session(session_id) is not None
    except Exception:
        session_exists = False
    if owner_instance is None and not session_exists:
        data = {
            "ok": False,
            "status": "not_found",
            "session_id": session_id,
            "deleted_session": False,
            "cleared_bindings": [],
            "error": f"persona chat session not found: {session_id}",
        }
        print(emit_json(data) if args.json else data["error"])
        return 2
    # Third site of the same shape: the STORED ``owner_instance.persona_id``
    # compared raw against ``requested_persona``, which is
    # ``_normalize_cli_persona_or_template_id`` output — or, on that call's
    # except branch, ``safe_assignment_token`` output. Two normalizers reachable
    # from one argument, in one predicate. Folded through ``personas_equal`` so a
    # spelling can no longer refuse the delete of a root the caller owns.
    owner_persona = getattr(owner_instance, "persona_id", None)
    pin_proves_ownership = bool(requested_instance) and (
        owner_instance is not None and owner_instance.id == requested_instance
    )
    # Pin bounded exactly as in the mission-chat fence: proven ownership speaks
    # only where the persona leg is silent (an owner row with no readable
    # persona). A pin must not license deleting a root the caller has just
    # named a DIFFERENT persona for — that is caller confusion, and refusing it
    # costs nothing.
    persona_ok = (not requested_persona) or personas_equal(
        owner_persona, requested_persona
    ) or (pin_proves_ownership and not safe_assignment_token(owner_persona))
    if (
        owner_instance is None
        or (requested_instance and owner_instance.id != requested_instance)
        or not persona_ok
    ):
        data = {
            "ok": False,
            "capability_id": "persona.chat.delete",
            "error_kind": ChatErrorKind.FOREIGN_CHAT_SESSION,
            "error": "chat root is not owned by the requested persona instance",
            "session_id": session_id,
            "persona_instance_id": requested_instance or None,
        }
        print(emit_json(data) if args.json else data["error"])
        return 2
    if not bool(getattr(args, "_persona_chat_delete_lease_acquired", False)):
        try:
            with persona_chat_root_lease(
                session_id,
                owner_id=safe_assignment_token(
                    getattr(args, "requested_by", None)
                ),
                observer_kind="delete",
            ):
                args._persona_chat_delete_lease_acquired = True
                try:
                    return _cmd_persona_chat_delete(args)
                finally:
                    args._persona_chat_delete_lease_acquired = False
        except PersonaChatBusyError as exc:
            data = {
                "ok": False,
                "capability_id": "persona.chat.delete",
                "error_kind": ChatErrorKind.CHAT_BUSY,
                "session_id": session_id,
                "lease_owner": exc.owner,
                "error": str(exc),
            }
            print(emit_json(data) if args.json else data["error"])
            return 2
    try:
        lineage_delete = getattr(session_db, "delete_compression_lineage", None)
        if callable(lineage_delete):
            deleted_session = bool(
                lineage_delete(session_id, sessions_dir=get_hermes_home() / "sessions")
            )
        else:
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
    registry = persona_chat_runtime_registry()
    if registry is not None:
        registry.evict(session_id)
    try:
        from tools.terminal_tool import cleanup_vm

        cleanup_vm(session_id, force_remove=True)
    except Exception:
        pass
    closed_assignment_ids: list[str] = []
    # Unbind EVERY instance still pointing at the session that was just deleted —
    # on either pointer, and regardless of which identity was named on the
    # request. Ownership was already enforced above (a foreign request is refused
    # with ``foreign_chat_session``), so anything still holding this session id is
    # by definition a dangling pointer: id-scheme drift, a sibling steal, or the
    # legacy ``session_id`` mirror. Leaving one behind is exactly how a permanent
    # ``session_not_in_db`` parity drop is minted — the projection can only hide
    # the row, it can never repair the binding.
    for instance in instance_store.list_all():
        bound_here = session_id in {
            safe_assignment_text(getattr(instance, "default_chat_session_id", None), limit=200),
            safe_assignment_text(getattr(instance, "session_id", None), limit=200),
        }
        if not bound_here:
            continue

        assignment_id = safe_assignment_token(getattr(instance, "current_assignment_id", None))
        if assignment_id:
            try:
                assignment = assignment_store.get(assignment_id)
                if (
                    assignment.evidence_kind == "free_floating"
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

        # One write path for every unbind (delete verb + reconcile sweep): it
        # nulls only the pointers that name THIS session, demotes the mode, and
        # emits ``persona_instance.chat_binding_cleared``.
        record = instance_store.clear_chat_session_binding(
            instance,
            session_id=session_id,
            reason=CHAT_BINDING_CLEARED_REASON_DELETED,
        )
        if record is not None:
            cleared_bindings.append(instance.id)

    if not deleted_session and not cleared_bindings:
        data = {
            "ok": False,
            "status": "not_found",
            "session_id": session_id,
            "deleted_session": False,
            "cleared_bindings": [],
            "error": f"persona chat session not found: {session_id}",
            "next_expected": "refresh Harness snapshot; if the row is still visible, inspect SessionDB source and persona_instance.default_chat_session_id",
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


def _retired_persona_instance_payload(
    exc: RetiredPersonaInstanceError,
) -> dict[str, object]:
    return _retired_persona_instance_refusal(
        persona_instance_id=exc.persona_instance_id,
        archive_path=exc.archive_path,
        error_kind=exc.code,
    )


def _retired_persona_instance_refusal(
    *,
    persona_instance_id: str,
    archive_path: object,
    # The ONE ``error_kind`` in this file still spelled as a literal, and it is
    # structural rather than an oversight: a default argument is evaluated when
    # the ``def`` executes, and this file is EXEC'd into harness.py's globals —
    # so ``ChatErrorKind`` cannot be bound yet without a module-level import
    # here, which is exactly the namespace collision the exec'd-part discipline
    # forbids. The value is pinned to ``ChatErrorKind.RETIRED_PERSONA_INSTANCE``
    # by tests/agent_runtime/test_mission_chat_outcome.py, so it cannot drift.
    error_kind: str = "retired_persona_instance",
) -> dict[str, object]:
    """ONE retired-target refusal body, whether or not an exception carried it.

    A pre-flight that refuses BEFORE the write lane has no exception to render,
    but the caller must not be able to tell the difference: same ``error_kind``,
    same fields, same ``next_expected``. Two spellings of this payload would be
    two contracts."""
    # Function-local: this file is exec'd into harness.py's globals, so a
    # module-level import here would need a matching harness.py import or it
    # is a NameError on a LIVE turn. The turn-outcome vocabulary is owned by
    # agent_runtime.mission_chat_outcome; nothing re-spells its values.
    from agent_runtime.mission_chat_outcome import ExecutionState

    return {
        "ok": False,
        "execution_state": ExecutionState.REFUSED,
        "error_kind": error_kind,
        "error": (
            f"persona instance {persona_instance_id} was retired with its "
            "placement and cannot be reopened as a live agent"
        ),
        "persona_instance_id": persona_instance_id,
        "archive_path": str(archive_path),
        "history_preserved": True,
        "next_expected": (
            "view the preserved chat history read-only, or create a fresh "
            "placement with a new instance id"
        ),
    }


def _coordinator_actor_id(args) -> str | None:
    raw = str(getattr(args, "requested_by", "") or "").strip()
    if raw.lower().startswith("coordinator:"):
        return safe_assignment_token(raw.split(":", 1)[1])
    if raw.lower() == "coordinator":
        return safe_assignment_token(getattr(args, "coordinator_id", None))
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


# S70 removed `_cmd_persona_instance_message` and its `persona instance
# message` subparser. The verb queued a "free-floating persona assignment" —
# a row whose only durable consumer was the tick loop the 2026-07-30 chat-only
# purge removed — and its `--auto-run` variant ran a second, parallel chat-turn
# authority beside `mission-chat message`. Messaging an instance is
# `harness mission-chat message`.


def _cmd_mission_chat_steer(args) -> int:
    # Function-local: this file is exec'd into harness.py's globals, so a
    # module-level import here would need a matching harness.py import or it
    # is a NameError on a LIVE turn. The turn-outcome vocabulary is owned by
    # agent_runtime.mission_chat_outcome; nothing re-spells its values.
    from agent_runtime.mission_chat_outcome import (
        ChatErrorKind,
        ExecutionState,
    )
    session_id = safe_assignment_text(getattr(args, "session_id", None), limit=200)
    client_message_id = safe_assignment_text(getattr(args, "client_message_id", None), limit=200)
    message = safe_assignment_text(getattr(args, "message", None), limit=12000)
    if not session_id or not client_message_id or not message:
        data = {
            "ok": False,
            "capability_id": "mission.chat.steer",
            "execution_state": ExecutionState.REJECTED,
            "session_id": session_id,
            "client_message_id": client_message_id,
            "error_kind": ChatErrorKind.INVALID_REQUEST,
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
            "execution_state": ExecutionState.REJECTED,
            "session_id": session_id,
            "client_message_id": client_message_id,
            "error_kind": ChatErrorKind.INVALID_REQUEST,
            "error": safe_assignment_text(str(exc), limit=240),
        }
        print(emit_json(data) if args.json else data["error"])
        return 2
    print(emit_json(data) if args.json else (data.get("error") or data.get("execution_state") or "accepted"))
    return 0


def _publish_persona_chat_projection_event(
    *,
    session_id: str,
    client_message_id: str,
    turn_id: str,
    persona_id: str,
    persona_instance_id: str,
    active_session_id: str | None,
    native_revision: str | None,
) -> bool:
    """Notify stream consumers after the durable chat projection commits.

    The journal bit makes normal retries exactly-once. Event append deliberately
    precedes the bit: a crash between the two can duplicate a harmless rebuild
    notification, while the opposite order could permanently hide a committed
    reply from a running Launcher stream.
    """

    record = mission_chat_turn_record(
        session_id=session_id,
        client_message_id=client_message_id,
    ) or {}
    if record.get("state") != TURN_STATE_PROJECTED or record.get(
        "projection_event_emitted"
    ):
        return False
    try:
        EventLog().append(
            Event(
                type="persona_chat.projected",
                # Chat is the only lane (contract 45): a chat turn has no task
                # binding to report, so this is a constant rather than an
                # argv-sourced value that could only ever be None.
                task_id=None,
                run_id=None,
                persona_id=persona_id or None,
                ts=now(),
                payload={
                    "persona_instance_id": persona_instance_id,
                    "root_chat_session_id": session_id,
                    "active_session_id": active_session_id or session_id,
                    "client_message_id": client_message_id,
                    "turn_id": turn_id,
                    "native_revision": native_revision,
                    "change_kind": "projection_committed",
                },
                session_id=session_id,
                turn_id=turn_id,
            )
        )
    except Exception:
        # The reply is already durable. Leave the marker false so an
        # idempotent replay repairs the missed notification.
        return False
    outcome = transition_mission_chat_turn(
        session_id=session_id,
        client_message_id=client_message_id,
        turn_id=turn_id,
        state=TURN_STATE_PROJECTED,
        metadata={"projection_event_emitted": True},
        elements=record.get("elements") or [],
    )
    return outcome is MissionChatTurnPersistOutcome.PERSISTED


def _publish_persona_chat_metadata_event(
    *,
    session_id: str,
    persona_id: str,
    persona_instance_id: str,
) -> bool:
    """Notify stream consumers that SessionDB-only chat metadata changed."""

    try:
        EventLog().append(
            Event(
                type="persona_chat.metadata_updated",
                # See the projection event above: chat turns carry no task
                # binding under contract 45.
                task_id=None,
                run_id=None,
                persona_id=persona_id or None,
                ts=now(),
                payload={
                    "persona_instance_id": persona_instance_id,
                    "root_chat_session_id": session_id,
                    "change_kind": "auto_title_updated",
                },
                session_id=session_id,
            )
        )
        return True
    except Exception:
        return False


def _publish_persona_chat_send_refused_event(
    *,
    session_id: str,
    client_message_id: str | None,
    persona_id: str | None,
    persona_instance_id: str | None,
    error_kind: str,
    lease_owner: dict | None = None,
) -> bool:
    """Record that an operator send was REFUSED before any turn write happened.

    The refusal lanes are the one turn outcome with no durable trace by
    construction: every durable write a mission-chat turn performs lives inside
    ``_mission_chat_commit_turn``, under the chat-root lease, so a send refused
    on the way to that lease writes no operator row, no turn record, and no
    event. The 2026-08-09 investigation went looking for a message the operator
    had definitely sent and found it in exactly zero persistence surfaces —
    history, live log, and turn journal all showed only the turn that was
    holding the lease. A lost operator message was, by design, undiagnosable.

    This is the counter-record. It is deliberately a FACT ABOUT a send, not a
    copy of one:

    * ``client_message_id`` is the idempotency key the operator's client already
      owns, so a refused send can be correlated with the retry that eventually
      landed (or the absence of one) without the text ever leaving the client.
    * the message TEXT is not here and must never be added. Operator text has
      exactly two sanitising chokepoints — the chat persistence path and the
      live-log mirror — and both sit inside the lease this branch never took.
      Writing raw text here would put unsanitised operator prose into
      ``events.jsonl``, which is read by the snapshot/read-model pipeline and
      shipped to every consumer of it. ``error_kind`` plus the identity keys is
      what makes a loss diagnosable; the prose is what makes it a leak.
    * ``lease_owner`` is reduced to pid/kind/acquired_at. The sidecar is
      operator-written JSON read off disk best-effort; only the three scalar
      fields are copied so an oversized or attacker-shaped sidecar cannot ride
      into the payload (events are byte-capped, and a rejected append here would
      lose the very record this exists to keep).

    Returns True when the row landed. Never raises: a refusal that additionally
    failed to record is still a refusal, and the caller's typed envelope must
    reach the client unchanged.

    Registered as ``persona_chat.send_refused``. The first caller was the
    ``chat_busy`` branch — the refusal the incident produced and the only one
    that is genuinely transient. ``_cmd_mission_chat_message``'s three other
    pre-lease guards (``unknown_chat_session``, ``foreign_chat_session``,
    ``retired_persona_instance`` — the explicit-session ownership fences and
    the retired-target pre-flight/mint-race arms) are terminal and
    operator-visible by other means, but a caller cannot tell a terminal
    refusal from a transient one without first correlating it, so they are
    routed through here too: every pre-lease refusal costs one call site and
    leaves the same durable trace.
    """

    owner = lease_owner if isinstance(lease_owner, dict) else {}
    payload = {
        "root_chat_session_id": session_id,
        "client_message_id": safe_assignment_token(client_message_id) or None,
        "error_kind": str(error_kind),
        "persona_instance_id": safe_assignment_token(persona_instance_id) or None,
        "lease_owner_pid": owner.get("pid") if isinstance(owner.get("pid"), int) else None,
        "lease_owner_kind": safe_assignment_token(owner.get("observer_kind")) or None,
        "lease_acquired_at": (
            owner.get("acquired_at") if isinstance(owner.get("acquired_at"), (int, float)) else None
        ),
    }
    try:
        EventLog().append(
            Event(
                type="persona_chat.send_refused",
                # See the projection event above: chat turns carry no task
                # binding under contract 45.
                task_id=None,
                run_id=None,
                persona_id=safe_assignment_token(persona_id) or None,
                ts=now(),
                payload=payload,
                session_id=session_id,
            )
        )
        return True
    except Exception:
        return False


#: The journal states that mean "the root's CURRENT turn is this very
#: ``client_message_id``". Narrower than ``INFLIGHT_TURN_STATES`` on purpose:
#: ``outcome_unknown`` is in-flight-ish but already has its own honest refusal
#: (``chat_turn_outcome_unknown``, which routes to ``turn-resolve``), and
#: ``running`` is the legacy pre-journal spelling no live transition produces.
_DUPLICATE_IN_FLIGHT_TURN_STATES = frozenset(
    {TURN_STATE_PENDING, TURN_STATE_EXECUTING}
)


def _mission_chat_busy_outcome(
    *,
    args,
    session_db,
    session_id: str,
    client_message_id: str,
    normalized_persona: str,
    persona_instance_id,
    session_established,
    exc,
) -> int:
    """Answer a send that lost the chat-root lease. Emits, returns the exit code.

    **Reads only.** Every branch here runs OUTSIDE the lease it just failed to
    acquire, against a root another turn is actively writing, so it may not
    transition the journal, may not publish the projection event (that helper
    writes a ``projection_event_emitted`` marker through
    ``transition_mission_chat_turn``), and may not touch SessionDB. The turn
    journal is per-session JSON on disk and readable without the lease; that
    read is the whole mechanism.

    Why this exists (2026-08-24 incident). ``_cmd_mission_chat_message`` splits
    plan → commit, and ALL of the lane's dedupe/idempotent-replay logic lives
    inside ``_mission_chat_commit_turn`` — i.e. AFTER the lease. So a duplicate
    of the turn that is CURRENTLY RUNNING died ``chat_busy`` before any dedupe
    could see it. ``chat_busy`` means "someone else holds the root", which a
    caller is entitled to read as "your message never landed": the Launcher's
    streaming-inactivity fallback re-presented its own still-running
    ``client_message_id``, got ``chat_busy``, and painted a delivered turn as a
    rejection while the agent's reply committed 20 seconds later.

    The distinction this restores is: *whose* turn is the busy root running?

    * this message's, still going  → ``chat_turn_duplicate_in_flight`` (BLOCKED,
      non-terminal: do not resend a new id, do not resolve, re-present THIS id)
    * this message's, already answered → the idempotent replay, served read-only
    * this message's, unprovable   → the existing ``chat_turn_outcome_unknown``
    * somebody else's              → ``chat_busy``, exactly as before

    A torn or missing journal read simply falls through to ``chat_busy`` — the
    degraded answer, never a wrong one.
    """

    # Function-local: this file is exec'd into harness.py's globals, so the
    # turn-outcome vocabulary is imported where it is used (same convention as
    # every other handler in this file).
    from agent_runtime.mission_chat_outcome import ChatErrorKind, ExecutionState

    journal = (
        mission_chat_turn_record(
            session_id=session_id, client_message_id=client_message_id
        )
        or {}
    )
    journal_state = safe_assignment_token(journal.get("state"))
    turn_id = journal.get("turn_id") or client_message_id

    if journal_state in _DUPLICATE_IN_FLIGHT_TURN_STATES:
        data = {
            "ok": False,
            "capability_id": "mission.chat.message",
            "execution_state": ExecutionState.BLOCKED,
            "error_kind": ChatErrorKind.CHAT_TURN_DUPLICATE_IN_FLIGHT,
            "duplicate_in_flight": True,
            # Explicitly NOT busy. A consumer that switches on this flag must
            # not see a duplicate-in-flight as a lost message.
            "chat_busy": False,
            "turn_resolution_required": False,
            "journal_state": journal_state,
            "root_chat_session_id": session_id,
            "session_id": session_id,
            "client_message_id": client_message_id,
            "turn_id": turn_id,
            "lease_owner": exc.owner,
            "error": (
                "this client_message_id is the turn currently running on this root"
            ),
            "next_expected": (
                "do not resend a new id and do not resolve; re-present this same "
                "client_message_id after the turn settles to replay its committed reply"
            ),
        }
        _publish_persona_chat_send_refused_event(
            session_id=session_id,
            client_message_id=client_message_id,
            persona_id=normalized_persona,
            persona_instance_id=persona_instance_id,
            error_kind=ChatErrorKind.CHAT_TURN_DUPLICATE_IN_FLIGHT,
            lease_owner=exc.owner,
        )
        _mission_chat_emit(args, data)
        return 2

    if journal_state in RESEND_BLOCKING_TURN_STATES:
        # Only ``outcome_unknown`` can reach here (``executing`` was taken
        # above). The record already IS the state the leased path would move it
        # to, so the same refusal is served without the transition.
        data = {
            "ok": False,
            "capability_id": "mission.chat.message",
            "execution_state": ExecutionState.BLOCKED,
            "error_kind": ChatErrorKind.CHAT_TURN_OUTCOME_UNKNOWN,
            "journal_state": journal_state,
            "root_chat_session_id": session_id,
            "session_id": session_id,
            "client_message_id": client_message_id,
            "turn_id": turn_id,
            "error": (
                "the prior provider outcome cannot be proven; resolve this turn "
                "before resending"
            ),
            "next_expected": (
                "resolve the exact outcome_unknown turn with action=abandon, then "
                "send a new client_message_id"
            ),
        }
        _mission_chat_emit(args, data)
        return 2

    if journal_state in (TURN_STATE_PROJECTED, TURN_STATE_NATIVE_COMMITTED):
        stored_reply = journal.get("stored_reply")
        if stored_reply is None:
            replay = _persona_chat_existing_turn(
                session_db=session_db,
                session_id=session_id,
                client_message_id=client_message_id,
            )
            assistant = replay.get("assistant")
            if isinstance(assistant, dict):
                stored_reply = assistant.get("content")
        if stored_reply is not None:
            reply_text = _redact_persona_chat_text(
                stored_reply, limit=PERSONA_CHAT_REPLY_LIMIT
            )
            data = {
                "ok": True,
                "capability_id": "mission.chat.message",
                "persona_id": normalized_persona,
                "persona_instance_id": persona_instance_id,
                "root_chat_session_id": session_id,
                "active_session_id": journal.get("active_session_id") or session_id,
                "session_id": session_id,
                "chat_session_id": session_id,
                # A replay reports the same thread lineage the original turn
                # did — see the leased replay branch in the commit phase.
                "session_established": session_established,
                "client_message_id": client_message_id,
                "turn_id": turn_id,
                "execution_state": ExecutionState.COMPLETED,
                "reply": reply_text,
                "idempotent_replay": True,
                "journal_state": journal_state,
                # ``native_committed`` still owes the settling→projected walk.
                # It is deliberately NOT done here (it is a journal WRITE); the
                # next lease-holding presentation of this id finishes it.
                "next_expected": (
                    "duplicate client message id replayed from the turn journal "
                    "while another turn holds this chat root"
                ),
            }
            _stamp_turn_visibility(data, reply_text)
            _stamp_reply_media(data, reply_text, args)
            _mission_chat_emit(
                args, data, f"mission chat reply for {normalized_persona}"
            )
            return 0

    # No record, an unreadable record, or a settled state with no reply to
    # serve: the root is busy with something that is not provably this message.
    data = {
        "ok": False,
        "capability_id": "mission.chat.message",
        "execution_state": ExecutionState.REJECTED,
        "error_kind": ChatErrorKind.CHAT_BUSY,
        "chat_busy": True,
        "root_chat_session_id": session_id,
        "session_id": session_id,
        "lease_owner": exc.owner,
        "client_message_id": client_message_id,
        "error": str(exc),
    }
    # Durable FIRST, then the wire. A refused send is the one turn outcome
    # that writes nothing by construction — every durable write lives inside
    # the lease this branch never acquired — so before 2026-08-09 an
    # operator message lost to a busy root left no trace anywhere: not in
    # the transcript, not in the turn journal, not in the EventLog. The
    # refusal envelope on stdout was the only evidence, and it died with the
    # banner that rendered it.
    _publish_persona_chat_send_refused_event(
        session_id=session_id,
        client_message_id=client_message_id,
        persona_id=normalized_persona,
        persona_instance_id=persona_instance_id,
        error_kind=ChatErrorKind.CHAT_BUSY,
        lease_owner=exc.owner,
    )
    _mission_chat_emit(args, data)
    return 2


def _mission_chat_emit(args, data, plain=None, *, stream=None) -> None:
    """THE one place a mission-chat turn payload leaves this handler.

    Every terminal payload — refusal, replay, blocker, reply — goes out through
    here, so an in-process caller can take the payload DICT instead of scraping
    it back out of stdout.

    Why that matters, and why it is a seam rather than a convention: the
    agent-to-agent relay (``tools/agent_chat_tool.agent_chat_send``) invoked
    this handler in-process under ``contextlib.redirect_stdout`` and then parsed
    the captured text back into JSON. ``redirect_stdout`` rebinds
    ``sys.stdout`` PROCESS-GLOBALLY, so two concurrent relays — which is exactly
    what a detached ``wait: false`` dispatch introduces — would interleave into
    one buffer and, worse, briefly steal the serve process's own stdout
    protocol from every other thread. Handing the caller the dict removes the
    capture entirely.

    ``args.payload_sink`` (absent on every argparse Namespace, so the CLI and
    the serve bridge are untouched) is the seam. When it is present the payload
    is handed over and NOTHING is printed: the caller owns the transport, and a
    nested reply must never land in the OUTER turn's stdout capture. When it is
    absent this prints byte-for-byte what each call site printed before.

    ``plain`` is the non-JSON console line; ``None`` keeps the historical
    ``data["error"]``. ``stream`` overrides the ``args.stream`` read for the one
    site that had already resolved it into a local.
    """

    sink = getattr(args, "payload_sink", None)
    if callable(sink):
        sink(data)
        return
    streaming = getattr(args, "stream", False) if stream is None else stream
    if streaming:
        _emit_chat_final(data)
    else:
        print(emit_json(data) if args.json else (data["error"] if plain is None else plain))


def _bind_mission_chat_delivery_capability() -> bool:
    """Answer ``async_delivery_supported()`` HONESTLY for this lane. Returns it.

    ``terminal(notify_on_complete / watch_patterns)`` and
    ``delegate_task(background=true)`` consult that flag before promising to
    deliver a result after the turn ends, and they were being told ``True`` on
    the mission-chat lane for the worst possible reason: nothing had ever bound
    the contextvar, so the default answered for it. Nothing in the serve process
    drained the completion queue at all, which made every one of those promises
    unkeepable — the tool registered a watcher, the turn ended, and the result
    went nowhere.

    Now the answer is bound to ONE fact: ``delivery_drain_is_live()``.

    * **a turn in a process with a LIVE DRAIN ⇒ True.** The drain is the
      consumer the promise names — it walks the durable dispatch store and
      forges completions back into the sender's thread — and it runs in exactly
      one place, the serve loop. So its liveness already distinguishes a
      serve-hosted turn from everything else; no second serve detector needed.
    * **cold one-shot CLI turn ⇒ False.** No drain was ever started there. The
      process exits when the turn does: ``terminal``'s notifications live only
      in this process's in-memory queue and die with it, so that promise is
      simply false. ``delegate_task``'s completions ARE durable and a later
      serve boot could deliver them — but "your subagent result may reappear in
      some future session" is a worse outcome than the inline/synchronous
      fallback ``False`` selects, which returns the result inside the turn that
      asked for it. Refusing the promise is the honest and the more useful
      answer.

    This used to ALSO require ``persona_chat_runtime_registry() is not None``,
    believing the registry "tells serve from CLI". It does not: the registry
    exists only when ``agent_runtime.persona_chat.hot_sessions_enabled`` is set,
    and that flag is an unrelated resident-agent CACHE policy which defaults to
    False and is off in production. The conjunct therefore answered False on
    every default-config serve — with the drain alive and perfectly able to
    deliver — and ``agent_chat_send(wait=false)`` refused on the exact lane it
    was built for, from the day it shipped (2026-08-09 live incident; see
    ``test_a_serve_with_hot_sessions_disabled_still_delivers``). A capability
    must gate on the consumer's own liveness, never on a proxy owned by a
    different feature.
    """

    from agent_runtime.dispatch_delivery import delivery_drain_is_live
    from gateway.session_context import (
        declare_async_delivery_channel,
        declare_stateless_channel,
    )

    can_deliver = delivery_drain_is_live()
    if can_deliver:
        declare_async_delivery_channel()
    else:
        declare_stateless_channel()
    return can_deliver


def _mission_chat_lease_provenance() -> tuple[str | None, str]:
    """``(owner_id, observer_kind)`` for the message-turn root lease.

    ONE fact answers both fields: the serve frame-protocol request id, read from
    the context serve's ``_run`` bound it in. Non-None means this turn arrived
    as a serve request — so the request id itself becomes the lease
    ``owner_id`` (correlating the lease owner file, and the ``lease_owner``
    block a ``chat_busy`` refusal surfaces, with the exact serve frame that
    holds the root) and the observer is labelled ``serve``. None means a
    one-shot CLI turn: no request to correlate (the lease falls back to its
    ``pid-<n>`` owner), labelled ``cli``.

    History, because this line has now lied twice (2026-08-09 investigation):
    ``observer_kind`` was derived from ``persona_chat_runtime_registry() is not
    None`` — the hot-sessions CACHE flag, default off, so every live serve turn
    was labelled ``cli`` in exactly the forensics a ``chat_busy`` incident
    reaches for — and ``owner_id`` read ``args.serve_request_id``, an attribute
    nothing in the tree has ever set, so every owner file carried the
    ``pid-<n>`` fallback instead of the request id the name promised. Both
    fields now read the one authority: :func:`current_serve_request_id`.
    """

    # serve is a real module (not an exec'd part) — see harness._cmd_serve —
    # so this import is safe from part-module code, and lazy to keep the CLI
    # path from paying serve's import weight before it needs it.
    from hermes_cli.harness_parts.serve import current_serve_request_id

    serve_request_id = current_serve_request_id()
    return serve_request_id, ("serve" if serve_request_id is not None else "cli")


def _normalize_deferred_thread_policy(args) -> None:
    """Restore the tri-state ``new_session`` argparse cannot express.

    ``--new-session`` is ``store_true``: present is True, absent is False
    ("continue the target's current default thread"). There is no spelling for
    UNSET — "no opinion, let ``agent_runtime.mission_chat.dispatch_session_policy``
    decide" — which is exactly what the in-process dispatch lane forwards, and
    what a DETACHED dispatch has to reproduce across an argv boundary now that
    its turn runs in a child process. Without it every dispatch would silently
    stop opening its own task thread and pile back into one sticky per-pair
    thread.

    Changing ``--new-session``'s own default to ``None`` would express it too,
    and would also start minting a fresh thread for every bare CLI send that
    omits the flag. So the unset case gets its own explicit flag, normalized
    HERE — once, at the boundary where args are first consumed — leaving the
    policy resolver downstream to see exactly the three states it was written
    for, with no second spelling anywhere behind it.
    """

    if getattr(args, "defer_thread_policy", False):
        args.new_session = None


def _stamp_turn_visibility(data: dict, reply_text, *, chat_result=None) -> dict:
    """Stamp the typed "did this turn produce visible content" block, in place.

    Every payload on this lane that carries a `reply` carries this beside it —
    the live turn, both idempotent-replay branches, and the projection-failure
    branch — so a consumer never has to know which internal path produced its
    payload in order to know whether anyone saw an answer. `ok` alone cannot
    tell it: this handler returns `ok: True` with an empty `reply` when the
    model produces no content, and on 2026-08-11 it did exactly that on a
    background-completion delivery, which every surface downstream then
    reported as a clean delivery.

    Total by construction (`classify_turn_visibility` never raises) because one
    of the four call sites is inside an exception handler, where a raise would
    replace a real failure with this one and corrupt the one-JSON-object stdout
    contract on the way.

    Function-local import for the reason given in `_cmd_mission_chat_message`:
    this file is exec'd into harness.py's globals.
    """

    from agent_runtime.turn_visibility import (
        TURN_VISIBILITY_KEY,
        classify_turn_visibility,
    )

    data[TURN_VISIBILITY_KEY] = classify_turn_visibility(
        reply_text=reply_text,
        # Absent on the replay branches, which answer from a stored reply and
        # have no turn result in hand. That is honestly less evidence, not
        # missing evidence: the stored text is exactly what the operator saw.
        messages=getattr(chat_result, "messages", None),
        raw=getattr(chat_result, "raw", None),
    ).as_dict()
    return data


#: The payload key carrying the ``reference → handle`` map minted for a
#: peer-executed turn's reply. Named here, read by
#: ``tools/agent_chat_dispatch._run_remote_dispatch``, and by nothing else.
REPLY_MEDIA_KEY = "media"


def _stamp_reply_media(data: dict, reply_text, args) -> dict:
    """Mint content handles for a PEER-EXECUTED reply's ``MEDIA:`` lines.

    Stage P4 / ruling R-P3. This is install **B**, answering a turn install A
    dispatched to it. The reply is about to travel home carrying
    ``MEDIA:<absolute path>`` lines that name files on THIS disk, and A can
    never mint handles for them: a handle is a digest of BYTES and A has none.
    So the mint happens here, at reply time, and the map rides the completion —
    which is the only channel between the two installs that does not require a
    second verb, because the payload this function stamps IS what
    ``peer.agent_chat.execute``'s frame lane carries back.

    **Gated on the peer origin, and the gate is the one fact that is already
    true.** ``--requested-by peer:<install id>`` is set by
    ``chat_turn.normalize_peer_chat_execute`` from a connection whose HMAC
    verified, and it is the ONLY spelling that reaches this handler for a
    cross-install turn. A local turn mints nothing, which is not an
    optimisation but the honest answer: on this machine the ``MEDIA:`` path IS
    the pointer, every local surface opens it directly, and hashing every image
    of every local turn would spend real I/O to produce a field with no reader.

    Absent, never empty. A reply that declared no image carries no key at all,
    so a local payload is byte-identical to what it has always been and a
    consumer never has to tell "no pictures" from "an older runtime".

    Total by construction, like :func:`_stamp_turn_visibility` beside it and for
    the same reason: one call site is inside an exception handler, and a raise
    here would replace a real failure with this one and corrupt the
    one-JSON-object stdout contract on the way out.
    """

    try:
        requested_by = str(getattr(args, "requested_by", "") or "")
        from agent_runtime.chat_turn import PEER_REQUESTED_BY_PREFIX

        if not requested_by.startswith(PEER_REQUESTED_BY_PREFIX):
            return data
        from agent_runtime.media_handles import mint_reply_media

        minted = mint_reply_media(reply_text)
        if minted:
            data[REPLY_MEDIA_KEY] = minted
    except Exception:  # noqa: BLE001 - a picture is never worth losing a turn
        import logging

        logging.getLogger(__name__).debug(
            "reply media mint failed; the reply travels without handles",
            exc_info=True,
        )
    return data


def _registry_probe_rounds():
    """This thread's cumulative tool-registry probe rounds, or ``None``.

    ``None`` is the honest answer when the registry cannot be consulted at all
    (an import shape this file cannot assume — it is exec'd into ``harness.py``
    globals, so every import here is function-local by contract). The caller
    turns an unknown END or an unknown BASELINE into an ABSENT
    ``registry_probe_rounds`` rather than a zero, because "the registry probed
    nothing" is a finding and "I could not ask" is not.
    """

    try:
        from tools.registry import probe_rounds_this_thread

        return int(probe_rounds_this_thread())
    except Exception:
        return None


def _visibility_bundle_builds():
    """This thread's cumulative chat-lane bundle BUILDS, or ``None``.

    Same contract, and the same reason for it, as
    :func:`_registry_probe_rounds`: cumulative, thread-local, never reset here,
    and ``None`` when the module cannot be consulted at all so the caller leaves
    ``visibility_bundle_builds`` ABSENT rather than reporting a zero it never
    measured.
    """

    try:
        from agent_runtime.chat_lane_bundle import bundle_builds_this_thread

        return int(bundle_builds_this_thread())
    except Exception:
        return None


def _snapshot_builds_overlapped(marks, *, until_ms):
    """Stage 4: snapshot builds whose span intersects ``anchor → until_ms``.

    ``until_ms`` is a mark off the same monotonic anchor (``stream_done``), so
    the window is reconstructed on the build ledger's own clock — never from
    wall stamps, and never across processes.

    Returns ``None`` — leaving the key ABSENT — for a window that was never
    marked (the turn did not reach ``stream_done``) or for a process that has
    never led a build and therefore cannot see the lane. A ``0`` from here is a
    real measurement: builds happened in this process and none of them touched
    this turn. That distinction is the entire point of the counter, because the
    warm sample turn stayed fast WITH a concurrent build and the remedy
    decision turns on whether that generalizes.
    """

    if until_ms is None:
        return None
    try:
        from agent_runtime import snapshot_build_ledger

        anchor = marks.anchor_monotonic
        return snapshot_build_ledger.overlapping_builds(
            start=anchor, end=anchor + (float(until_ms) / 1000.0)
        )
    except Exception:
        return None


def _cmd_mission_chat_message(args) -> int:
    # Function-local: this file is exec'd into harness.py's globals, so a
    # module-level import here would need a matching harness.py import or it
    # is a NameError on a LIVE turn. The turn-outcome vocabulary is owned by
    # agent_runtime.mission_chat_outcome; nothing re-spells its values.
    from agent_runtime.mission_chat_outcome import (
        ChatErrorKind,
        ExecutionState,
        MissionChatDeferredFinalization,
        MissionChatTurnPlan,
    )
    from agent_runtime.mission_chat_phases import TurnPhaseMarks

    # ── the turn's monotonic anchor ────────────────────────────────────────
    # FIRST statement of the handler, ahead of the capability bind, the config
    # load and every resolution below, because everything below is admission
    # cost the operator is waiting through. The emitter's own ``ttft_ms`` clock
    # does not start until ~1,100 lines from here (after replay checks, the
    # native-history load, the turn-context build and the observability row),
    # so on a cold turn it cannot see the profile bootstrap at all — that gap
    # is what this anchor closes. ``ttft_ms`` is unchanged and still means what
    # it always meant; these phases are a superset of it.
    #
    # One ``time.monotonic()`` read. See ``agent_runtime.mission_chat_phases``
    # for the honesty contract every mark below obeys.
    turn_phases = TurnPhaseMarks()
    # Baseline for Stage 4's ``registry_probe_rounds``. The registry's counter
    # is cumulative and thread-local (serve reuses pooled threads across turns),
    # so the turn's number is a DELTA and the near end of it has to be sampled
    # here, on the turn's own thread, before any of the turn's work runs.
    turn_phases.set_baseline("registry_probe_rounds", _registry_probe_rounds())
    # Same reasoning, one layer up: the chat-lane visibility bundle's build
    # counter is thread-cumulative too, and its turn number is the delta across
    # the same window. Sampled here so the baseline predates the turn-context
    # build, which is where the first bundle lookup happens.
    turn_phases.set_baseline("visibility_bundle_builds", _visibility_bundle_builds())
    # Per-request capability binding, at the very top so every path below —
    # including the refusals — runs with the truthful answer bound.
    _bind_mission_chat_delivery_capability()
    _normalize_deferred_thread_policy(args)
    cfg = load_agent_runtime_config()
    try:
        normalized_persona = _resolve_mission_chat_persona_id(
            args.persona_id, getattr(args, "persona_instance_id", None)
        )
    except ValueError as exc:
        data = {
            "ok": False,
            "capability_id": "mission.chat.message",
            "execution_state": ExecutionState.REJECTED,
            "error_kind": ChatErrorKind.UNSUPPORTED_PERSONA,
            "error": safe_assignment_text(str(exc), limit=240),
            "persona_id": safe_assignment_token(args.persona_id),
            "next_expected": "pass a configured persona id, profile:<name>, or a known personainst_* instance id",
        }
        _mission_chat_emit(args, data)
        return 2

    # Relay-chain guard at the canonical persona chokepoint. The chain is
    # explicit envelope provenance (relay_chain / relay_deadline_epoch), so
    # every transport into this handler gets the same depth/cycle/budget
    # answer; agent_chat_send carries the envelope but does not re-decide.
    from agent_runtime import relay_policy

    relay_chain_in = relay_policy.normalize_chain(getattr(args, "relay_chain", None))
    relay_deadline = relay_policy.parse_deadline_epoch(getattr(args, "relay_deadline_epoch", None))
    relay_decision = relay_policy.evaluate_relay(
        chain=relay_chain_in,
        target_persona_id=normalized_persona,
        deadline_epoch=relay_deadline,
    )
    if not relay_decision.allowed:
        data = {
            "ok": False,
            "capability_id": "mission.chat.message",
            "execution_state": ExecutionState.REJECTED,
            "error_kind": relay_decision.error_kind,
            "error": safe_assignment_text(relay_decision.reason, limit=400),
            "persona_id": normalized_persona,
            "relay_chain": list(relay_decision.chain),
            "next_expected": "answer your caller directly; do not relay onward on this chain",
        }
        _mission_chat_emit(args, data)
        return 2
    # Chain for THIS turn: envelope chain + the persona now speaking. Seeded
    # into the ContextVars around the model turn so the turn's own
    # agent_chat_send calls (tool workers run under copy_context) inherit it.
    turn_relay_chain = relay_decision.chain
    persona = _persona_by_id(cfg, normalized_persona)
    if persona is None:
        data = {
            "ok": False,
            "capability_id": "mission.chat.message",
            "execution_state": ExecutionState.REJECTED,
            "error_kind": ChatErrorKind.UNSUPPORTED_PERSONA,
            "error": f"unknown persona {safe_assignment_token(args.persona_id)}",
            "persona_id": safe_assignment_token(args.persona_id),
            "next_expected": "persist the persona, use profile:<name>, or address a known personainst_* instance",
        }
        _mission_chat_emit(args, data)
        return 2

    # chat-turn-prep Stage 4: MEASURE the SessionDB open before deciding to pool
    # it. H3 (§3) confirmed in CODE that every turn constructs a fresh
    # ``SessionDB`` whose writer-path constructor runs ``_init_schema`` (DDL +
    # FTS probe + column reconcile) plus the WAL checks, and confirmed just as
    # plainly that its millisecond share of the ``request_received →
    # context_built`` span was never measured. The pooling remedy is gated on
    # this number, not on the code shape — the plan's own §6 rule about remedies
    # measured against numbers nobody can re-take applies to its own stages.
    #
    # ``time.monotonic`` for the same reason ``mission_chat_phases`` uses it: an
    # NTP step must not be able to poison a duration. The value is folded into
    # the durable record's ``profile_timing`` block below; a turn that fails
    # here never reaches that fold, so the key stays ABSENT rather than
    # reporting the cost of an open that did not succeed.
    _session_db_open_started = time.monotonic()
    _session_db_open_ms: int | None = None
    try:
        session_db = _default_persona_session_db()
    except PersonaChatPersistenceError as exc:
        data = {
            "ok": False,
            "capability_id": "mission.chat.message",
            "execution_state": ExecutionState.FAILED,
            "error_kind": ChatErrorKind.CHAT_SESSION_DB_UNAVAILABLE,
            "persistence_operation": exc.operation,
            "error": str(exc),
            "persona_id": normalized_persona,
            "next_expected": "restore canonical persona chat transcript storage and retry the message",
        }
        _mission_chat_emit(args, data)
        return 2
    _session_db_open_ms = max(
        0, int((time.monotonic() - _session_db_open_started) * 1000)
    )
    instance_store = PersonaInstanceStore()
    instance_store.ensure_for_personas(ensure_persisted_personas(cfg))
    # Canonicalize a caller-supplied instance id at THIS boundary (the same
    # chokepoint open_chat uses), so an instance-shaped target can never mint a
    # variant row.
    #
    # An instance-shaped `--persona` (`personainst_qa_agent_f24601ba`) IS a
    # caller pin, and it arrives in the persona slot constantly (Mission Control
    # payloads, agent @handle targeting, legacy SessionDB rows).
    # `_resolve_mission_chat_persona_id` above canonicalizes it DOWN to the
    # persona id so every persona-keyed lookup works — and the instance half
    # used to be dropped right here, leaving the caller's explicit pin to be
    # re-decided by the bare-persona placement resolver below. Recover the pin
    # at this same chokepoint (no second resolver: `canonical_persona_instance_id`
    # remains the one derivation authority) so an explicit @handle is
    # authoritative BEFORE "placements shadow canonical" runs — which is what
    # that ruling already documents: it never fires when the caller already
    # disambiguated with a `personainst_*` target.
    requested_instance_id = getattr(args, "persona_instance_id", None)
    if not safe_assignment_token(requested_instance_id):
        raw_persona_target = safe_assignment_token(getattr(args, "persona_id", None))
        if raw_persona_target.startswith(PERSONA_INSTANCE_ID_PREFIX):
            requested_instance_id = raw_persona_target
    persona_instance_id = canonical_persona_instance_id(
        requested_instance_id, persona_id=normalized_persona
    )
    session_id = safe_assignment_text(getattr(args, "session_id", None), limit=200)
    # What the CALLER named, before anything on this turn overwrites it. Kept so
    # the settlement can tell "they answered in the right thread because they
    # named it" from "they inherited it" — the adoption signal this whole
    # binding is measured by.
    #
    # It used to be stashed on `args._stated_session_id`, because the lease
    # re-entry re-ran this line AFTER both the clarify bind and the
    # omitted-session mint had written a session back onto `args.session_id` —
    # so the second pass read every sticky continuation as a thread the caller
    # had named. The plan/commit split runs this line exactly once, so a plain
    # local is now the honest carrier.
    stated_session_id = session_id
    # CLARIFY CONTINUITY. Resolved HERE — after the caller's session id is read,
    # BEFORE the target decision below — and both consequences are free:
    #
    #   1. a ticket-supplied session flows through the EXISTING
    #      `unknown_chat_session` / `foreign_chat_session` guards below, so a
    #      token can never smuggle in a session the caller could not have named
    #      legitimately (a token for another instance's thread is refused as
    #      `foreign_chat_session`, reached without a line of new guard code);
    #   2. a bound turn never reaches the omitted-session branch, so it never
    #      mints, never repoints the instance's default-thread pointer, and
    #      never runs the pre-mint gate.
    #
    # Resolved ONCE. It used to be stashed on `args._clarify_binding` because
    # the handler re-entered itself to take the chat-root lease, and by then
    # `args.session_id` had been rewritten — a second pass would rediscover "the
    # caller named a session", report the turn as a plain `explicit_session_id`
    # continuation, and settle the ticket TWICE. The plan/commit split retires
    # the re-entry, so the local is the whole story.
    clarify_binding = _resolve_mission_chat_clarify_binding(
        args, session_id=session_id
    )
    clarify_session_id = (
        safe_assignment_text((clarify_binding or {}).get("bound_session_id"), limit=240)
        if (clarify_binding or {}).get("bound_via") == "clarify_token"
        else ""
    )
    if clarify_session_id:
        session_id = clarify_session_id
        args.session_id = clarify_session_id
    # How this turn's thread was established — typed, and carried in the reply
    # envelope so a dispatching agent can tell "this is a fresh task thread,
    # here is the one it superseded" from "this continued what we had". Decided
    # here for the explicit-session lane; re-decided by policy below when the
    # caller named no session.
    #
    # It used to be carried on `args._dispatch_session_established` because this
    # handler RE-ENTERED itself once to take the chat-root lease, and by then
    # the resolved session had been written back onto `args.session_id` — so a
    # second pass would rediscover "the caller named a session" and report every
    # freshly minted dispatch thread as a plain continuation. One pass now.
    session_established = None
    if session_id:
        # No `policy=`: an explicit session id outranks deployment policy, and
        # the resolver short-circuits before loading it — this lane runs on
        # every mission-chat turn and `mission_chat_dispatch_session_policy()`
        # parses the root config.yaml UNCACHED, to fill a field the
        # `explicit_session_id` reason never reads.
        session_established = session_established_payload(
            resolve_dispatch_session_decision(
                clarify_session_id=clarify_session_id or None, session_id=session_id
            ),
            fresh=False,
            predecessor_session_id=None,
        )
    # Sender identity for workspace-scoped target resolution: the chat-root
    # session of the agent that requested this send (agent-to-agent relay
    # threads it through the envelope; a bare operator CLI send omits it).
    requested_by_session = safe_assignment_text(
        getattr(args, "requested_by_session", None), limit=200
    )
    client_message_id = safe_assignment_text(
        getattr(args, "client_message_id", None), limit=200
    ) or f"agent-chat-send-{uuid.uuid4().hex[:12]}"
    # Written back so the serve lane and the turn store agree on the id a
    # generated fallback produced. (It also used to have to survive the lease
    # recursion; that recursion is gone.)
    args.client_message_id = client_message_id

    # Ambiguous-target guard at the canonical persona chokepoint (sibling of the
    # relay-chain guard above; same envelope, evaluated for every transport so an
    # instance-id target cannot dodge it). A BARE persona id names a persona, not
    # an instance; when the persona runs more than one live instance and the
    # caller pinned none, the omitted-session default below silently threads onto
    # the canonical primary and DROPS the message for every sibling (live
    # 2026-07-19: bare `qa` with two live `qa` instances landed only in
    # `personainst_qa`). Refuse with the candidate @handles so the caller can
    # retry against an exact instance. Never fires when the caller already
    # disambiguated (an explicit `persona_instance_id`, a `personainst_*` target,
    # or ANY caller-chosen session id — the operator console always carries an
    # instance-bearing session id, so its chats to every sibling keep working),
    # for a `profile:<name>` target, or for a single-instance persona.
    target_decision = _mission_chat_target_decision(
        instance_store=instance_store,
        normalized_persona=normalized_persona,
        raw_persona_id=getattr(args, "persona_id", None),
        persona_instance_id=persona_instance_id,
        session_id=session_id,
        relay_chain=turn_relay_chain,
        requested_by_session=requested_by_session,
    )
    if not target_decision.allowed:
        data = {
            "ok": False,
            "capability_id": "mission.chat.message",
            "execution_state": ExecutionState.REJECTED,
            "error_kind": target_decision.error_kind,
            "error": safe_assignment_text(target_decision.reason, limit=400),
            "persona_id": normalized_persona,
            "relay_chain": list(target_decision.chain),
            "candidates": [candidate.as_dict() for candidate in target_decision.candidates],
            "next_expected": (
                "re-send to a specific instance by the @personainst_ handle listed in candidates"
            ),
        }
        _mission_chat_emit(args, data)
        return 2

    if (
        session_id
        and is_canonical_session_persistence(session_db)
        and session_db.get_session(session_id) is None
    ):
        data = {
            "ok": False,
            "capability_id": "mission.chat.message",
            "execution_state": ExecutionState.REJECTED,
            "error_kind": ChatErrorKind.UNKNOWN_CHAT_SESSION,
            "error": f"unknown explicit persona chat root: {session_id}",
            "session_id": session_id,
            "next_expected": "open a server-minted chat root before sending",
        }
        # Pre-lease refusal: durably recorded the same way `chat_busy` is (see
        # `_publish_persona_chat_send_refused_event`'s docstring) — the send
        # never reaches the lease, so this is the only trace it leaves.
        _publish_persona_chat_send_refused_event(
            session_id=session_id,
            client_message_id=client_message_id,
            persona_id=normalized_persona,
            persona_instance_id=persona_instance_id,
            error_kind=ChatErrorKind.UNKNOWN_CHAT_SESSION,
        )
        _mission_chat_emit(args, data)
        return 2
    if session_id and is_canonical_session_persistence(session_db):
        owner = _persona_chat_session_owner(session_db, session_id)
        owner_instance = None
        try:
            owner_instance = instance_store.get(owner) if owner else None
        except Exception:
            owner_instance = None
        # ONE spelling authority on BOTH sides (``personas_equal``). This fence
        # used to compare ``safe_assignment_token(owner.persona_id)`` against
        # ``normalize_persona_or_template_id(caller_persona)`` — token form vs
        # colon form, two normalizers, one persona — so ``profile:alice`` could
        # never equal ``profile_alice`` and EVERY agent-to-agent reply delivery
        # for a profile persona was refused as foreign
        # (2026-08-24, dispatch-2540634d5cf3).
        owner_persona = getattr(owner_instance, "persona_id", None)
        # The PIN leg is an identity check on the very field ownership is
        # defined by: the chat root's owner IS an instance id. When the caller
        # supplies one it is authoritative about OWNERSHIP — but not about
        # WHOSE BRAIN RUNS. ``normalized_persona`` (not the owner's persona) is
        # what selected the persona, model and profile for this turn, so a pin
        # that proved ownership while the caller named a genuinely DIFFERENT
        # persona would run that persona's turn inside this instance's thread.
        # The pin therefore overrides the persona leg only where the persona
        # leg has nothing to say: an owner row whose persona is missing or
        # unreadable. See the F1 note in the guard tests.
        pin_proves_ownership = bool(persona_instance_id) and owner == persona_instance_id
        owner_persona_known = bool(safe_assignment_token(owner_persona))
        persona_ok = personas_equal(owner_persona, normalized_persona) or (
            pin_proves_ownership and not owner_persona_known
        )
        if (
            not owner
            or owner_instance is None
            or not persona_ok
            or (persona_instance_id and owner != persona_instance_id)
        ):
            data = {
                "ok": False,
                "capability_id": "mission.chat.message",
                "execution_state": ExecutionState.REJECTED,
                "error_kind": ChatErrorKind.FOREIGN_CHAT_SESSION,
                "error": f"explicit chat root is not owned by the target instance: {session_id}",
                "session_id": session_id,
                "persona_instance_id": persona_instance_id or None,
                "next_expected": "use the server-minted root returned for this exact persona instance",
            }
            # Pre-lease refusal — same durable record as `chat_busy`. See
            # `_publish_persona_chat_send_refused_event`'s docstring.
            _publish_persona_chat_send_refused_event(
                session_id=session_id,
                client_message_id=client_message_id,
                persona_id=normalized_persona,
                persona_instance_id=persona_instance_id,
                error_kind=ChatErrorKind.FOREIGN_CHAT_SESSION,
            )
            _mission_chat_emit(args, data)
            return 2
        persona_instance_id = owner
    if not session_id:
        # Omitted session (agent_chat_send dispatch lane, and any first-turn
        # open): the thread target is decided by policy below, not by this
        # branch. Under the default `new_per_dispatch` a dispatch MINTS its own
        # task-scoped thread; under `sticky` (or an explicit new_session=False)
        # it continues the target's current default thread. Either way the
        # session comes from the canonical resolve-or-mint chokepoint — never a
        # new random session per send, which orphaned the relay lane in
        # 2026-07-18 (unpointed session, invisible to the snapshot projection).
        # `open_chat` below repoints the instance's default pointer onto
        # whatever thread this turn established.
        #
        # BARE persona (no pin, no session): route through the "placements shadow
        # canonical" ruling — a single in-scope placement is the default target,
        # so the send lands on the deliberate placement instead of the plumbing
        # canonical row. The guard above already refused two-or-more in-scope
        # placements, so this resolves to at most one; with none it returns None
        # and the canonical channel is the reachability fallback.
        #
        # ONE instance identity for the turn: whatever resolves the root/mint
        # here is the instance `open_chat` BINDS below, so it is assigned back
        # onto `persona_instance_id` rather than kept as a second local. Keeping
        # them apart meant the bind received the RAW pin (`None` for a bare or
        # instance-shaped send), fell back to the canonical channel, and the
        # sibling-steal guard correctly refused the root this very turn had just
        # minted for the placement ("chat session
        # 'persona_chat_personainst_qa_agent_f24601ba_...' belongs to instance
        # 'personainst_qa_agent_f24601ba'; it cannot be bound onto
        # 'personainst_qa'") — refusing every placement-routed send, new-session
        # and continue alike. The explicit-session branch above already adopts
        # its resolved owner the same way; this is the omitted-session mirror.
        persona_instance_id = (
            persona_instance_id
            or _mission_chat_bare_persona_target(
                instance_store,
                normalized_persona=normalized_persona,
                requested_by_session=requested_by_session,
            )
            or canonical_chat_instance_id(normalized_persona, None)
        )
        # Fresh-vs-continue is decided by ONE authority
        # (agent_runtime.dispatch_session_policy), not by an inline
        # `not args.new_session` boolean: the flag is tri-state now (True /
        # False / unset) and "unset" must be answerable by deployment policy.
        # Default policy is new_per_dispatch — a dispatched task gets its own
        # task-scoped thread instead of accumulating in one mega-thread per
        # pair (which re-fed the whole transcript every turn). The CLI/serve
        # lane's argparse `--new-session` is store_true, so an operator console
        # send arrives as an explicit False and keeps continuing the target's
        # current default thread, exactly as before this policy existed.
        dispatch_decision = resolve_dispatch_session_decision(
            session_id=None,
            new_session=getattr(args, "new_session", None),
        )
        # Resolved UNCONDITIONALLY, even when we are about to mint: the thread
        # this dispatch supersedes is the lineage a later reader follows back
        # (recorded as `_dispatched_from`, reported as `predecessor_session_id`).
        # Read-only — `resolve_…` never mints.
        existing_root = resolve_default_chat_session_id_for_instance(
            instance_store,
            persona_id=normalized_persona,
            persona_instance_id=persona_instance_id,
        )
        if existing_root and not dispatch_decision.mint:
            session_id = existing_root
            session_established = session_established_payload(
                dispatch_decision, fresh=False, predecessor_session_id=None
            )
        else:
            # PRE-MINT GATE. The mint below is this turn's first DURABLE side
            # effect: a titled session row, and — once `open_chat` binds it —
            # a REPOINT of the instance's default-thread pointer. Under the
            # new_per_dispatch default, running it before the caller's own
            # arguments have been checked meant every refused or retried
            # dispatch littered an empty task thread AND stole the pointer that
            # `new_session:false` follows. So the refusals decidable from
            # `args` alone are evaluated FIRST. They need no session id, so
            # ordering them here costs nothing; their original, session-bearing
            # sites below stay put as defense in depth for the explicit-session
            # lane (which never mints).
            #
            # A RETIRED target is the same defect one layer out: `open_chat`
            # refuses it by raising, but the mint reaches `open_chat` only after
            # creating and titling the row, so the refusal landed one durable
            # thread too late — and outside the typed handler below, so it also
            # escaped as a traceback. It needs the store rather than `args`, so
            # it is its own read-only pre-flight, evaluated through the same gate.
            premint_refusal = _mission_chat_caller_refusal(
                args,
                persona_id=normalized_persona,
                persona_instance_id=persona_instance_id,
            ) or _mission_chat_retired_target_refusal(
                instance_store,
                persona_id=normalized_persona,
                persona_instance_id=persona_instance_id,
            )
            if premint_refusal is not None:
                # Only the retired-target arm of this gate is one of the three
                # pre-lease guard kinds this event exists for; the sibling
                # `_mission_chat_caller_refusal` arms (missing message,
                # invalid model override) are argument validation, not chat-
                # root ownership, and are not routed here.
                if premint_refusal.get("error_kind") == ChatErrorKind.RETIRED_PERSONA_INSTANCE:
                    _publish_persona_chat_send_refused_event(
                        session_id=session_id,
                        client_message_id=client_message_id,
                        persona_id=normalized_persona,
                        persona_instance_id=persona_instance_id,
                        error_kind=ChatErrorKind.RETIRED_PERSONA_INSTANCE,
                    )
                _mission_chat_emit(args, premint_refusal)
                return 2
            # A fresh thread is only navigable if it is NAMED: nine identical
            # "QA Agent chat" rows are worse than the mega-thread they replaced.
            # An explicit --title/`title` wins; otherwise a deliberate fresh
            # dispatch is titled after the task it carries. A first-ever STICKY
            # mint (the operator console's first message) keeps the durable
            # per-persona title, so that lane is unchanged.
            persona_thread_title = (
                f"{safe_assignment_text(getattr(persona, 'display_name', None), limit=120) or normalized_persona} chat"
            )
            requested_title = safe_assignment_text(getattr(args, "title", None), limit=120)
            mint_title = persona_thread_title
            if dispatch_decision.mint:
                mint_title = (
                    requested_title
                    or derive_dispatch_title(getattr(args, "message", None))
                    or persona_thread_title
                )
            try:
                receipt = PersonaChatMintReceiptStore().mint(
                    instance_store=instance_store,
                    session_db=session_db,
                    persona_id=normalized_persona,
                    persona_instance_id=persona_instance_id,
                    idempotency_key=(
                        safe_assignment_text(getattr(args, "idempotency_key", None), limit=240)
                        or f"send:{client_message_id}"
                    ),
                    title=mint_title,
                    dispatched_from={
                        "predecessor_chat_session_id": existing_root,
                        "requested_by_session": requested_by_session,
                    },
                )
            except RetiredPersonaInstanceError as exc:
                # What this handler guarantees is the TYPE. An unhandled raise
                # here was the untyped traceback the operator saw instead of a
                # refusal.
                #
                # What reaches it: a target already retired when the mint began
                # (a caller that skipped the pre-flight above by another road,
                # or a `retire` that landed in the gap between the pre-flight
                # and the mint), AND a `retire` that lands inside the mint lane
                # itself. Both now arrive with nothing left behind: the mint
                # asserts bindability before its first durable write and BINDS
                # before its first session-visible one, so a refusal from either
                # point precedes the titled row that used to survive it.
                data = _retired_persona_instance_payload(exc)
                # Pre-lease refusal, same as the pre-flight arm above — durably
                # recorded via `_publish_persona_chat_send_refused_event`. No
                # `session_id` yet: the mint that would have established one
                # never completed.
                _publish_persona_chat_send_refused_event(
                    session_id=session_id,
                    client_message_id=client_message_id,
                    persona_id=normalized_persona,
                    persona_instance_id=persona_instance_id,
                    error_kind=ChatErrorKind.RETIRED_PERSONA_INSTANCE,
                )
                _mission_chat_emit(args, data)
                return 2
            except PersonaChatPersistenceError as exc:
                # The mint's OTHER typed refusal, and the newer one: the bind
                # landed and the transcript row did not, so the mint retracted
                # the bind and reports the failure rather than returning a root
                # that dereferences nowhere. Same frame the commit phase below
                # emits for the sticky lane — one ``chat_session_persist_failed``
                # vocabulary for "the transcript store would not take it",
                # whichever end of the lane hit it. Without this arm the typed
                # error is merely a better-named traceback.
                #
                # No ``session_id``: the whole point is that this turn never
                # established one. The receipt stays RESERVED, so a retry with
                # the same idempotency key resolves the same root and completes.
                data = {
                    "ok": False,
                    "capability_id": "mission.chat.message",
                    "execution_state": ExecutionState.FAILED,
                    "error_kind": ChatErrorKind.CHAT_SESSION_PERSIST_FAILED,
                    "persistence_operation": exc.operation,
                    "error": str(exc),
                    "persona_id": normalized_persona,
                    "persona_instance_id": persona_instance_id,
                    "next_expected": (
                        "restore canonical persona chat transcript storage and retry the message"
                    ),
                }
                _mission_chat_emit(args, data)
                return 2
            session_id = str(receipt["root_chat_session_id"])
            session_established = session_established_payload(
                dispatch_decision,
                fresh=True,
                # The mint reports the lineage it actually RECORDED, so the
                # envelope and `_dispatched_from` cannot disagree. It matters on
                # a RETRY of the same client_message_id: that resolves the same
                # idempotency-keyed receipt, by which time `existing_root` has
                # become this very thread — reporting it here would claim "A
                # superseded A", and reporting nothing would lose the real
                # predecessor the first pass established.
                predecessor_session_id=(receipt.get("dispatched_from") or {}).get(
                    "predecessor_chat_session_id"
                ),
            )
        args.session_id = session_id
    # ── plan → commit ──────────────────────────────────────────────────────
    # Everything above RESOLVED this turn; everything below WRITES it, once,
    # under the chat-root lease.
    #
    # This boundary used to be a self-call: the body re-entered
    # ``_cmd_mission_chat_message(args)`` from inside ``with lease:`` after
    # setting an ``args._persona_chat_root_lease_acquired`` flag. Every
    # resolution above therefore ran TWICE per turn, three durable writes with
    # it (``open_chat``, the session ensure, the model-override persist), and
    # the turn's phase state had to be smuggled across the re-entry on
    # ``args._*`` attributes so the second pass would not re-decide it. The
    # split retires the recursion, the double writes, and the smuggling
    # together.
    display_name = (
        safe_assignment_text(getattr(persona, "display_name", None), limit=120)
        or _display_name_for_profile(normalized_persona)
    )
    plan = MissionChatTurnPlan(
        args=args,
        cfg=cfg,
        session_db=session_db,
        instance_store=instance_store,
        persona=persona,
        normalized_persona=normalized_persona,
        persona_instance_id=persona_instance_id,
        display_name=display_name,
        session_id=session_id,
        client_message_id=client_message_id,
        session_established=session_established,
        clarify_binding=clarify_binding,
        stated_session_id=stated_session_id,
        requested_by_session=requested_by_session,
        turn_relay_chain=turn_relay_chain,
        relay_chain_in=relay_chain_in,
        relay_deadline=relay_deadline,
        phases=turn_phases,
        session_db_open_ms=_session_db_open_ms,
    )
    # The lease covers the WRITES and nothing else. Post-emit decoration — the
    # auxiliary-LLM auto-title and the metadata event that reports it — is
    # packaged into ``deferred`` under the lease and run below, after the
    # ``with`` has exited. See MissionChatDeferredFinalization for the 2026-08-09
    # incident that made the distinction load-bearing: a 46-second title tail
    # inside the lease refused the operator's next message ``chat_busy`` long
    # after the reply it answered was on screen.
    deferred = MissionChatDeferredFinalization()
    # C1h-bis: the turn's own two stream publishes. START is issued inside the
    # commit, immediately after the write-ahead record that puts this turn in
    # the ``running_work`` projection; END rides the ``finally`` below, which is
    # the one place EVERY exit of the commit passes through — fourteen terminal
    # journal transitions, a bare ``return`` on each refusal, and an exception
    # that propagates all land there. Unpaired by construction: ``publish_ended``
    # is a no-op unless START actually appended, so the busy/refused paths (which
    # never reach a write-ahead) announce nothing. See
    # ``agent_runtime.chat_turn_presence``.
    presence = ChatTurnPresence()
    try:
        # Provenance decided in ONE place (owner id + observer kind from the
        # same serve-request fact) — see _mission_chat_lease_provenance for the
        # two diagnostic lies this line used to tell.
        lease_owner_id, lease_observer_kind = _mission_chat_lease_provenance()
        with persona_chat_root_lease(
            session_id,
            owner_id=safe_assignment_token(lease_owner_id),
            observer_kind=lease_observer_kind,
        ):
            exit_code = _mission_chat_commit_turn(plan, deferred, presence)
    except PersonaChatBusyError as exc:
        # "The root is busy" is not one answer, it is four — and which one it is
        # depends on whether the turn holding the lease IS this message. The
        # journal is readable without the lease, so that question is answerable
        # here; see ``_mission_chat_busy_outcome`` for the incident that made
        # collapsing all four into ``chat_busy`` a delivered-turn-painted-as-
        # rejected bug.
        return _mission_chat_busy_outcome(
            args=args,
            session_db=session_db,
            session_id=session_id,
            client_message_id=client_message_id,
            normalized_persona=normalized_persona,
            persona_instance_id=persona_instance_id,
            session_established=session_established,
            exc=exc,
        )
    finally:
        # The turn has left the in-flight set (or never entered it). Publishing
        # here rather than at each terminal transition is deliberate: the END
        # frame must be built from a projection that no longer carries the row,
        # and only this point is past every write the commit performs. Fail-safe
        # and idempotent — see ``ChatTurnPresence.publish_ended``.
        presence.publish_ended()
    # ── lease RELEASED ─────────────────────────────────────────────────────
    # Everything the turn owed the root is committed and reported. The root is
    # free from here, so a slow or failing deferred step delays nobody's next
    # send. ``run_once`` never raises: this is past the point where the exit
    # code is decided, and a decoration failure may not change it.
    deferred.run_once()
    return exit_code


def _mission_chat_commit_turn(plan, deferred, presence) -> int:
    """The SOLE writer for a mission-chat turn. Runs under the chat-root lease.

    Every durable write from ``open_chat`` onward lives here, so the lease that
    serialises a chat root actually covers them — before the plan/commit split
    the first three ran outside it, twice, on the way to acquiring it.

    The unpack below is deliberate and load-bearing: it rebinds each planned
    value to the name the turn body has always used, so the body itself is
    unchanged by the extraction. A renaming pass through 1,000 lines of the
    most-live code in the harness is a separate risk from moving the lease, and
    they do not have to be taken together.

    ``deferred`` is the one value that flows back OUT: a
    ``MissionChatDeferredFinalization`` the caller runs after the ``with`` block
    exits. Nothing that writes the root's turn or transcript state may go in it;
    it exists for post-emit decoration whose only cost is time — today, the
    auxiliary-LLM auto-title and the metadata event that reports a title change.

    ``presence`` is a ``ChatTurnPresence`` the caller owns (C1h-bis). This
    function publishes the turn's START on it, one line after the write-ahead
    journal record that puts the turn in the ``running_work`` projection; the
    caller publishes the END from its ``finally``, because this function has
    fourteen terminal transitions and no single exit.
    """

    # Function-local: this file is exec'd into harness.py's globals (see the
    # note in _cmd_mission_chat_message). ``relay_policy`` is here rather than
    # inherited, because the plan phase's own local import does NOT reach across
    # the split — a free name here would be a NameError on a LIVE turn and
    # nothing but a live turn would find it.
    from agent_runtime import relay_policy
    from agent_runtime.mission_chat_outcome import (
        ChatErrorKind,
        ExecutionState,
        FinalizationWarning,
        FinalizationWarningKind,
        classify_turn_failure,
    )
    from agent_runtime.mission_chat_phases import (
        TURN_PHASES_KEY as MISSION_CHAT_TURN_PHASES_KEY,
        TURN_TIMING_KEY as MISSION_CHAT_TURN_TIMING_KEY,
        mark_from_trace_payload as _mark_turn_phase_from_trace_payload,
        turn_timing_block,
    )
    from agent_runtime.mission_chat_turns import (
        TURN_PROFILE_TIMING_KEY as MISSION_CHAT_TURN_PROFILE_TIMING_KEY,
    )

    args = plan.args
    cfg = plan.cfg
    session_db = plan.session_db
    instance_store = plan.instance_store
    persona = plan.persona
    normalized_persona = plan.normalized_persona
    persona_instance_id = plan.persona_instance_id
    display_name = plan.display_name
    session_id = plan.session_id
    client_message_id = plan.client_message_id
    session_established = plan.session_established
    clarify_binding = plan.clarify_binding
    stated_session_id = plan.stated_session_id
    requested_by_session = plan.requested_by_session
    turn_relay_chain = plan.turn_relay_chain
    relay_chain_in = plan.relay_chain_in
    relay_deadline = plan.relay_deadline
    # The turn's monotonic timeline, anchored at handler entry (see the plan's
    # ``phases`` field). Marked below at the boundaries the operator's TTFT is
    # actually made of; serialized onto the record by the persists that already
    # happen. Nothing in this function reads a mark back to decide anything.
    turn_phases = plan.phases
    # ── finalization accounting ────────────────────────────────────────────
    # Bookkeeping that fails AFTER the reply is durable does not fail the turn —
    # and used to leave no trace at all. Two classes of silence lived here:
    #
    #   1. the instance-state commit (return the agent to idle, repoint its
    #      default chat thread) sat inside a bare ``except Exception: pass``, so
    #      a cockpit showing an agent stuck ``busy`` after a completed turn had
    #      no record anywhere of why;
    #   2. eight ``transition_mission_chat_turn`` calls DISCARDED their
    #      ``MissionChatTurnPersistOutcome``, so a skipped or rejected journal
    #      write (lock timeout, stale transition, invalid state) was
    #      indistinguishable from a clean one.
    #
    # Both now record a typed reason and ride the envelope as
    # ``finalization_warnings``. The key is ABSENT on a clean turn, so the wire
    # is unchanged for every healthy send — its presence is the whole signal.
    finalization_warnings: list[FinalizationWarning] = []

    def _warn(kind, detail: object, *, step: str | None = None) -> None:
        finalization_warnings.append(
            FinalizationWarning(
                kind=kind,
                detail=safe_assignment_text(str(detail), limit=200) or "unknown",
                step=step,
            )
        )

    def _route_turn_write(outcome, *, step: str):
        """Account for a turn-journal transition. No write is lost silently."""

        if outcome is not MissionChatTurnPersistOutcome.PERSISTED:
            _warn(
                FinalizationWarningKind.TURN_RECORD_NOT_PERSISTED,
                getattr(outcome, "value", None) or "no_outcome",
                step=step,
            )
        return outcome

    def _stamp_finalization(data: dict) -> dict:
        if finalization_warnings:
            data["finalization_warnings"] = [
                warning.as_dict() for warning in finalization_warnings
            ]
        return data

    try:
        instance = instance_store.open_chat(
            persona_id=normalized_persona,
            persona_instance_id=persona_instance_id or None,
            session_id=session_id,
            # persona DEFAULT name, NOT authoritative: names a first-ever chat
            # holder but must never rename an existing instance — else the send
            # path clobbers a deliberate placement name ("QA Agent (2)"), folding
            # a sibling onto the primary's console channel. Explicit rename lives
            # in persona.instance.update_profile.
            default_display_name=display_name,
            profile_id=safe_assignment_token(getattr(persona, "hermes_profile", None)),
            kill_active=False,
        )
    except RetiredPersonaInstanceError as exc:
        # A placement retired between this turn's thread being established and
        # this bind. Defense in depth rather than the litter site it used to be:
        # the mint now binds before its first session-visible write, so a
        # retirement that beats the mint leaves nothing behind, and one that
        # lands after it archives a row that legitimately owned the thread
        # (`retire` preserves chat history by contract). This still refuses, and
        # still refuses with the same typed error.
        data = _retired_persona_instance_payload(exc)
        _mission_chat_emit(args, data)
        return 2
    except ValueError as exc:
        data = {"ok": False, "error": safe_assignment_text(str(exc), limit=240)}
        _mission_chat_emit(args, data)
        return 2

    # The retired mission lane's re-entry point used to live here: --task/--goal
    # wrote instance.current_task_id/goal_id and flipped instance.mode to the
    # RETIRED "task_bound", then persisted the row. A chat send must never arm
    # retired runtime state. Both flags are gone from the parser (contract 45);
    # arming a row is `persona instance steer --goal`, which is untouched.

    try:
        _ensure_persona_chat_session(
            session_db=session_db,
            session_id=session_id,
            persona_id=normalized_persona,
            title=f"{instance.display_name} chat",
            required=True,
        )
    except PersonaChatPersistenceError as exc:
        data = {
            "ok": False,
            "capability_id": "mission.chat.message",
            "execution_state": ExecutionState.FAILED,
            "error_kind": ChatErrorKind.CHAT_SESSION_PERSIST_FAILED,
            "persistence_operation": exc.operation,
            "error": str(exc),
            "persona_id": normalized_persona,
            "session_id": session_id,
            "persona_instance_id": instance.id,
            "next_expected": "restore canonical persona chat transcript storage and retry the message",
        }
        _mission_chat_emit(args, data)
        return 2
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
            instance=instance,
        )
    except ValueError as exc:
        data = _invalid_chat_model_override_payload(
            exc,
            persona_id=normalized_persona,
            persona_instance_id=instance.id,
            session_id=session_id,
        )
        _mission_chat_emit(args, data)
        return 2
    except Exception as exc:
        data = {
            "ok": False,
            "error_kind": ChatErrorKind.CHAT_MODEL_OVERRIDE_PERSIST_FAILED,
            "error": safe_assignment_text(str(exc), limit=320) or type(exc).__name__,
            "persona_instance_id": instance.id,
            "persona_id": normalized_persona,
            "session_id": session_id,
            "chat_session_id": session_id,
            "next_expected": "inspect Harness session metadata storage; chat-scoped model override was not applied and Hermes profile defaults were not changed",
        }
        _mission_chat_emit(args, data)
        return 2
    # Resolve the effective instance once. Prompt receipts and execution must
    # observe the same model and skill assignment authority.
    persona = apply_instance_model_overrides(persona, instance)
    message = safe_assignment_text(getattr(args, "message", None), limit=12000)
    if not message:
        data = _missing_chat_message_payload()
        _mission_chat_emit(args, data)
        return 2

    replay = _persona_chat_existing_turn(
        session_db=session_db,
        session_id=session_id,
        client_message_id=client_message_id,
    )
    journal = mission_chat_turn_record(
        session_id=session_id, client_message_id=client_message_id
    ) or {}
    journal_state = safe_assignment_token(journal.get("state"))
    # A durable reply already in SessionDB outranks whatever the journal thinks
    # happened. The recoverable set is the turn store's (a view of its own
    # transition table), never a literal spelled here — the wall-budget state
    # was invisible to exactly this kind of inline set until 2026-07-26.
    if journal_state in REPLY_RECOVERABLE_TURN_STATES and replay.get("assistant"):
        recovered_reply = _redact_persona_chat_text(
            replay["assistant"].get("content"), limit=PERSONA_CHAT_REPLY_LIMIT
        )
        _settled = transition_mission_chat_turn(
            session_id=session_id,
            client_message_id=client_message_id,
            turn_id=journal.get("turn_id") or client_message_id,
            state=TURN_STATE_NATIVE_COMMITTED,
            metadata={
                "root_chat_session_id": session_id,
                "active_session_id": _persona_chat_native_tip(
                    session_db, session_id
                ),
                "native_revision": _persona_chat_native_revision(
                    session_db, session_id
                ),
                "native_committed": True,
                "stored_reply": recovered_reply,
            },
            elements=journal.get("elements") or [],
        )
        _route_turn_write(_settled, step="reply_recovery_native_commit")
        journal = mission_chat_turn_record(
            session_id=session_id, client_message_id=client_message_id
        ) or {}
        journal_state = safe_assignment_token(journal.get("state"))
    # Settling: the reply is durable, the projection is not. Finish the walk.
    if journal_state in SETTLING_TURN_STATES:
        stored_reply = journal.get("stored_reply")
        if stored_reply is None and replay.get("assistant"):
            stored_reply = _redact_persona_chat_text(
                replay["assistant"].get("content"),
                limit=PERSONA_CHAT_REPLY_LIMIT,
            )
        _settled = transition_mission_chat_turn(
            session_id=session_id,
            client_message_id=client_message_id,
            turn_id=journal.get("turn_id") or client_message_id,
            state=TURN_STATE_PROJECTED,
            metadata={
                "stored_reply": stored_reply,
                "projection_committed": True,
            },
            elements=journal.get("elements") or [],
        )
        _route_turn_write(_settled, step="settling_projection_commit")
        journal = mission_chat_turn_record(
            session_id=session_id, client_message_id=client_message_id
        ) or {}
        journal_state = TURN_STATE_PROJECTED
    if journal_state == TURN_STATE_BUDGET_EXHAUSTED:
        # Settled, NOT ambiguous: this turn ended on its wall clock and the
        # harness knows it. Never route the operator to turn-resolve (that verb
        # exists only for genuinely unknown provider outcomes) and never block
        # the lane — just say plainly that this id is spent.
        data = {
            "ok": False,
            "capability_id": "mission.chat.message",
            "execution_state": ExecutionState.BUDGET_EXHAUSTED,
            "error_kind": ChatErrorKind.CHAT_TURN_BUDGET_EXHAUSTED,
            "budget_exhausted": True,
            "turn_resolution_required": False,
            "journal_state": TURN_STATE_BUDGET_EXHAUSTED,
            "root_chat_session_id": session_id,
            "session_id": session_id,
            "client_message_id": client_message_id,
            "turn_id": journal.get("turn_id") or client_message_id,
            "budget_summary": journal.get("budget_summary"),
            "error": "this turn already ended on its wall-clock budget; it is settled and needs no resolution",
            "next_expected": "send a new client_message_id to continue; no turn-resolve is required",
        }
        _stamp_finalization(data)
        _mission_chat_emit(args, data)
        return 2
    # No proven reply and the journal says a provider call may still be
    # outstanding: refuse the resend and route to the resolve verb.
    if journal_state in RESEND_BLOCKING_TURN_STATES:
        if journal_state == TURN_STATE_EXECUTING:
            _settled = transition_mission_chat_turn(
                session_id=session_id,
                client_message_id=client_message_id,
                turn_id=journal.get("turn_id") or client_message_id,
                state=TURN_STATE_OUTCOME_UNKNOWN,
                metadata={"provider_submitted": True},
            )
            _route_turn_write(_settled, step="resend_settle_outcome_unknown")
        data = {
            "ok": False,
            "capability_id": "mission.chat.message",
            "execution_state": ExecutionState.BLOCKED,
            "error_kind": ChatErrorKind.CHAT_TURN_OUTCOME_UNKNOWN,
            "root_chat_session_id": session_id,
            "session_id": session_id,
            "client_message_id": client_message_id,
            "turn_id": journal.get("turn_id") or client_message_id,
            "error": "the prior provider outcome cannot be proven; resolve this turn before resending",
            "next_expected": "resolve the exact outcome_unknown turn with action=abandon, then send a new client_message_id",
        }
        _stamp_finalization(data)
        _mission_chat_emit(args, data)
        return 2
    if journal_state == TURN_STATE_PROJECTED and journal.get("stored_reply") is not None:
        _publish_persona_chat_projection_event(
            session_id=session_id,
            client_message_id=client_message_id,
            turn_id=journal.get("turn_id") or client_message_id,
            persona_id=normalized_persona,
            persona_instance_id=instance.id,
            active_session_id=journal.get("active_session_id")
            or _persona_chat_native_tip(session_db, session_id),
            native_revision=journal.get("native_revision")
            or _persona_chat_native_revision(session_db, session_id),
        )
        reply_text = _redact_persona_chat_text(
            journal.get("stored_reply"), limit=PERSONA_CHAT_REPLY_LIMIT
        )
        data = {
            "ok": True,
            "capability_id": "mission.chat.message",
            "persona_instance_id": instance.id,
            "persona_id": normalized_persona,
            "root_chat_session_id": session_id,
            "active_session_id": journal.get("active_session_id") or _persona_chat_native_tip(session_db, session_id),
            "session_id": session_id,
            "chat_session_id": session_id,
            # Same lineage the live envelope reports. A replay is the SAME turn
            # answered again (the mint receipt is idempotency-keyed, so a
            # retried dispatch lands back in the thread it established), so it
            # must report the same {fresh, reason, predecessor_session_id} —
            # a caller that reads `session_established` to decide where its
            # follow-up goes cannot have that answer disappear on a retry.
            "session_established": session_established,
            "client_message_id": client_message_id,
            "turn_id": journal.get("turn_id") or client_message_id,
            "execution_state": ExecutionState.COMPLETED,
            "reply": reply_text,
            "idempotent_replay": True,
            "journal_state": TURN_STATE_PROJECTED,
        }
        _stamp_turn_visibility(data, reply_text)
        _stamp_reply_media(data, reply_text, args)
        _stamp_finalization(data)
        _mission_chat_emit(args, data, f"mission chat reply for {normalized_persona}")
        return 0
    if replay.get("assistant"):
        reply_text = _redact_persona_chat_text(
            replay["assistant"].get("content"), limit=PERSONA_CHAT_REPLY_LIMIT
        )
        _settled = transition_mission_chat_turn(
            session_id=session_id,
            client_message_id=client_message_id,
            turn_id=client_message_id,
            state=TURN_STATE_PENDING,
            metadata={"root_chat_session_id": session_id, "pending_user_message": message},
        )
        _route_turn_write(_settled, step="replay_walk_pending")
        _settled = transition_mission_chat_turn(
            session_id=session_id,
            client_message_id=client_message_id,
            turn_id=client_message_id,
            state=TURN_STATE_EXECUTING,
            metadata={"provider_submitted": True},
        )
        _route_turn_write(_settled, step="replay_walk_executing")
        _settled = transition_mission_chat_turn(
            session_id=session_id,
            client_message_id=client_message_id,
            turn_id=client_message_id,
            state=TURN_STATE_NATIVE_COMMITTED,
            metadata={"native_committed": True, "stored_reply": reply_text},
        )
        _route_turn_write(_settled, step="replay_walk_native_committed")
        _settled = transition_mission_chat_turn(
            session_id=session_id,
            client_message_id=client_message_id,
            turn_id=client_message_id,
            state=TURN_STATE_PROJECTED,
            metadata={"projection_committed": True, "stored_reply": reply_text},
        )
        _route_turn_write(_settled, step="replay_walk_projected")
        _publish_persona_chat_projection_event(
            session_id=session_id,
            client_message_id=client_message_id,
            turn_id=client_message_id,
            persona_id=normalized_persona,
            persona_instance_id=instance.id,
            active_session_id=_persona_chat_native_tip(session_db, session_id),
            native_revision=_persona_chat_native_revision(session_db, session_id),
        )
        data = {
            "ok": True,
            "capability_id": "mission.chat.message",
            "agent_profile_id": instance.id,
            "persona_instance_id": instance.id,
            "persona_id": normalized_persona,
            "session_id": session_id,
            "chat_session_id": session_id,
            # See the projected-replay envelope above: a replay reports the same
            # thread lineage the original turn did.
            "session_established": session_established,
            # S30: no task binding key. It was kept only because the Launcher
            # parsed it, and that parse fed a field which was never read -- a
            # dead key held alive by a dead reader. The Launcher dropped the
            # reader first (23bd05c6); the goal key went the same way in S26.
            "client_message_id": client_message_id,
            "execution_state": ExecutionState.COMPLETED,
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
        _stamp_turn_visibility(data, reply_text)
        _stamp_reply_media(data, reply_text, args)
        _stamp_finalization(data)
        _mission_chat_emit(args, data, f"mission chat reply for {normalized_persona}")
        return 0

    # SessionDB's native structured lineage is the sole continuation authority.
    # The Mission Control projection is never folded into this input.
    active_session_id = _persona_chat_native_tip(session_db, session_id)
    native_history = _persona_chat_native_history(session_db, active_session_id)
    abandoned_ids = {
        str(record.get("client_message_id") or "")
        for record in mission_chat_turn_records(session_id=session_id)
        if record.get("state") == TURN_STATE_ABANDONED
    }
    native_history = safe_native_history(
        [
            item
            for item in (native_history or [])
            if not any(
                str(item.get("platform_message_id") or "") == abandoned_id
                or str(item.get("platform_message_id") or "").startswith(
                    f"{abandoned_id}:"
                )
                for abandoned_id in abandoned_ids
            )
        ]
    )
    native_revision_before = _persona_chat_native_revision(session_db, session_id)
    runtime_registry = persona_chat_runtime_registry()
    chat_message = message
    # Relay sender attribution: resolve who sent this incoming message ONCE
    # (agent_chat_send relays carry requested_by="agent:<caller session>") and
    # hand the typed marker to the runtime, which stamps it on the native user
    # row it persists for this turn. Without it the target's transcript renders
    # a relayed message as the OPERATOR. Operator/CLI sends resolve to None →
    # no marker → byte-identical persistence.
    #
    # This lane stopped appending the incoming row itself when native session
    # continuity landed (c60413e17); the marker therefore rides
    # `mission_chat_reply(relay_sender_marker=)` down to the single seam that
    # types the row the RUNTIME writes (profile_runner.
    # stage_persona_chat_user_row_marker) rather than a second write here.
    relay_sender_marker = _resolve_relay_sender_marker(
        getattr(args, "requested_by", None),
        instance_store=instance_store,
        relay_chain_in=relay_chain_in,
    )
    from agent_runtime import turn_budget as _turn_budget
    from agent_runtime.mission_chat_turn_context import build_mission_chat_turn_context
    # Function-local on purpose: this file is exec'd into harness.py's globals,
    # so a module-level name here would need a matching harness.py import or it
    # is a NameError on a LIVE turn (nothing a test run would notice). A local
    # import binds in this function's scope and needs no harness cooperation.
    from agent_runtime.run_budget import turn_run_budget_metadata

    # The WHOLE per-turn context — wall budget, capability account, situational
    # HUD + delivery, skill preload envelope, workspace AGENTS.md, runtime
    # signature, volatile tail — is assembled by one unit-testable builder
    # (`agent_runtime.mission_chat_turn_context`). This command part is exec'd
    # into harness.py's globals rather than imported, so anything assembled HERE
    # can only ever be guarded by AST source-shape assertions; assembled there it
    # is guarded by tests that assert the composed bytes. What remains here is
    # composition: gather the turn's inputs, call the builder, send.
    #
    # G10: an explicit --max-seconds ALWAYS wins; only its absence (None) falls
    # through to the operator's configured lane default
    # (agent_runtime.mission_chat.default_max_seconds, itself 240s when unset),
    # so the deployment sets the work-shaped window once instead of every caller
    # remembering a flag.
    turn_context = build_mission_chat_turn_context(
        persona=persona,
        instance=instance,
        config=cfg,
        session_id=session_id,
        native_history=native_history,
        model_selection=model_selection,
        session_model_config=_session_model_config(session_db, session_id),
        max_seconds=resolve_mission_chat_max_seconds(getattr(args, "max_seconds", None)),
        relay_deadline_epoch=relay_deadline,
        relay_chain=turn_relay_chain,
        min_relay_seconds=relay_policy.MIN_RELAY_BUDGET_SECONDS,
        agents_file=getattr(args, "agents_file", None),
        surface_prompt=getattr(args, "surface_prompt", "") or "",
    )
    turn_phases.mark("context_built")
    # The same object the runner's checkpoint clamp is armed from below, so the
    # number the agent was told and the number the runtime enforces cannot drift.
    wall_budget = turn_context.wall_budget
    workspace_id = safe_assignment_token(getattr(args, "workspace_id", None))
    workspace_name = safe_assignment_text(
        getattr(args, "workspace_name", None), limit=120
    )
    # Record-at-injection: the observability row carries the very HUD dict that
    # was rendered into the fed block, so the operator's CONTEXT peek shows
    # exactly what the agent was told — never a later re-derivation.
    prompt_context = mission_chat_prompt_observability(
        persona=persona,
        persona_instance_id=instance.id,
        session_id=session_id,
        # task_id/goal_id intentionally not passed: both are defaulted kwargs and
        # the retired mission lane was their only source on this path.
        turn_id=safe_assignment_token(client_message_id),
        surface_prompt=getattr(args, "surface_prompt", "") or "",
        limiting_wrapper_active=False,
        session_db=session_db,
        current_message=message,
        model_selection=model_selection,
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        workspace_agents=turn_context.workspace_agents,
        situational_hud=turn_context.situational_hud,
        situational_hud_revision=turn_context.situational_hud_revision,
        situational_hud_delivery=turn_context.situational_hud_delivery,
        queued_skills=list(turn_context.skills.queued),
        required_preload_skills=list(turn_context.skills.required),
        preloaded_skills_loaded=list(turn_context.skills.loaded),
        preloaded_skills_missing=list(turn_context.skills.missing),
        instance_skill_overrides=(
            list(instance.skill_overrides)
            if instance.skill_overrides is not None
            else None
        ),
    )
    turn_phases.mark("observability_built")
    # The envelope is rendered last because it needs the observability row's
    # context_id; body and volatile tail both come from the one built context.
    situational_hud_content = turn_context.runtime_context_envelope(
        context_id=str(prompt_context["context_id"])
    )
    instance.skill_manifest_hash = safe_assignment_token(
        prompt_context.get("skill_manifest_hash")
    )
    instance = instance_store.update(instance)
    stream = bool(getattr(args, "stream", False))
    stream_emitter = _ChatProtocolV2Emitter(
        turn_id=safe_assignment_token(client_message_id),
        client_message_id=client_message_id,
        emit_frames=stream,
        on_update=lambda emitter: persist_mission_chat_turn(
            session_id=session_id,
            client_message_id=client_message_id,
            turn_id=emitter.turn_id,
            elements=emitter.elements,
        ),
        # The emitter owns exactly ONE phase mark: the first token off the
        # provider. It is taken there because `delta()` is the first site in
        # this process that has seen a provider byte, and taking it there costs
        # no provider-client surgery. Guarded by a boolean read so the cost is
        # per TURN, not per token.
        turn_phases=turn_phases,
    )
    turn_phases.mark("emitter_created")

    # C8: the legacy `chat.delta` lane is RETIRED (ruling 0 — one wire shape per
    # token). Deltas ride the v2 `segment.delta` frame only; the emitter runs
    # every frame inside the captured request context, so worker-thread deltas
    # keep their serve request id.
    _stream_delta = stream_emitter.delta

    trace_payloads: list[dict[str, object]] = []

    def _stream_progress(payload: dict[str, object] | None) -> None:
        if payload:
            trace_payloads.append(payload)
            # The conversation loop's dispatch-start marker becomes the
            # `request_assembled` phase mark here — the loop cannot hold the
            # turn's TurnPhaseMarks itself (it lives a layer below the
            # harness), so the mark rides the trace payload it already emits.
            # Same-process, synchronous callback chain: the receipt instant IS
            # the emission instant to within the callback's own cost.
            _mark_turn_phase_from_trace_payload(turn_phases, payload)
        stream_emitter.progress(payload)

    def _agent_ready_for_steer(agent):
        # ── the two marks that bracket profile bootstrap ───────────────────
        # The runner calls this the instant the agent object exists and
        # IMMEDIATELY starts the conversation when it returns, so `agent_ready`
        # is the end of profile bootstrap (tool-registry probe rounds, plugin
        # discovery, auxiliary-client probes — the ~5.4 s that gap G1 said
        # nothing on the record could see) and `provider_request_started` is
        # the handoff to the model turn.
        #
        # Stated exactly, because the name is generous: prompt assembly inside
        # ``run_conversation`` happens AFTER this mark, so it lands inside the
        # span this mark opens. `request_assembled` (marked from the loop's
        # dispatch-start trace payload in `_stream_progress`) closes that
        # honestly WITHOUT provider-client surgery: request_started →
        # request_assembled is hermes assembly, request_assembled →
        # provider_first_byte is client init + network + provider. Both marks
        # here are ABSENT when the runner never reached agent construction — a
        # cold-init failure reports no agent_ready, which is the truth about
        # it.
        turn_phases.mark("agent_ready")
        turn_phases.count_delta("registry_probe_rounds", _registry_probe_rounds())
        turn_phases.count_delta("visibility_bundle_builds", _visibility_bundle_builds())
        try:
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
        finally:
            turn_phases.mark("provider_request_started")

    provider_submitted = False
    try:
        mark_stale_inflight_turns_interrupted(
            session_id=session_id,
            active_client_message_id=client_message_id,
        )
        # Marked BEFORE the write it names, on purpose: the write-ahead record
        # is the first durable trace of this turn, and it has to be able to
        # describe its own admission. Every phase mark that names a PERSIST is
        # taken at the moment the persist is issued, so the block a record
        # carries is always complete as of that record.
        turn_phases.mark("write_ahead")
        write_ahead_outcome = transition_mission_chat_turn(
            session_id=session_id,
            client_message_id=client_message_id,
            turn_id=stream_emitter.turn_id,
            state=TURN_STATE_PENDING,
            elements=stream_emitter.elements,
            metadata={
                "root_chat_session_id": session_id,
                "active_session_id": active_session_id,
                "persona_instance_id": instance.id,
                "pending_user_message": message,
                "provider_submitted": False,
                # Rides the persist that already happens — no new write. On a
                # turn that dies before the provider this is the ONLY phase
                # block that ever lands, and its provider_* keys are absent.
                MISSION_CHAT_TURN_PHASES_KEY: turn_phases.snapshot(),
            },
        )
        # C1h-bis: the turn's START, published the moment its row is real and
        # not one line earlier. The hub builds a FRESH projection when the event
        # log moves, so an event appended before this record exists produces a
        # frame with no row on it — indistinguishable, to a second console, from
        # never publishing at all. Gated on the record actually persisting for
        # the same reason: a skipped or rejected journal write leaves nothing for
        # the projection to carry, and announcing it would be a claim about a row
        # that is not there.
        if write_ahead_outcome is MissionChatTurnPersistOutcome.PERSISTED:
            presence.publish_started(
                session_id=session_id,
                client_message_id=client_message_id,
                turn_id=stream_emitter.turn_id,
                persona_id=normalized_persona,
                persona_instance_id=instance.id,
                active_session_id=active_session_id,
            )
        # Live-log mirror, at the write-ahead point ON PURPOSE: this lane does
        # not append the operator row itself (native continuity: the runtime
        # persists it with the turn), and a head agent checking on a teammate
        # MID-TASK needs to see the order it was given before the turn ends —
        # not only once the reply lands. Redacted through the same write
        # boundary the persisted row crosses; deduped on
        # (role, client_message_id) so a resend of this turn cannot double it.
        # The relay marker rides along so a teammate's relayed message is
        # attributed to the SENDER in the grep file instead of reading as the
        # operator — the same attribution the conversation projection carries.
        _mirror_persona_chat_message(
            session_db=session_db,
            session_id=session_id,
            role="user",
            text=_redact_persona_chat_text(
                message, limit=PERSONA_CHAT_OPERATOR_MESSAGE_LIMIT
            ),
            client_message_id=client_message_id,
            turn_id=stream_emitter.turn_id,
            relay_marker=relay_sender_marker,
        )
        # Chained relays share one deadline: this hop's wall budget is capped
        # by the time left on the chain, and the deadline is seeded (root
        # turns mint it) so deeper hops inherit the same clock. Both facts come
        # from the ONE `wall_budget` object resolved above — the same object the
        # agent's HUD line was rendered from, so the number the model was told
        # and the number the runner enforces can never drift apart.
        # Relative window handed to the runner: time from NOW to the same
        # absolute deadline the agent's HUD line quoted, so prompt assembly
        # cannot let the enforced wall outlive the shared chain deadline.
        relay_wall_seconds = max(
            relay_policy.MIN_RELAY_BUDGET_SECONDS,
            wall_budget.remaining_seconds(),
        )
        _relay_chain_token = relay_policy.RELAY_CHAIN.set(turn_relay_chain)
        _relay_deadline_token = relay_policy.RELAY_DEADLINE.set(
            wall_budget.deadline_epoch
        )
        # situational_hud / situational_hud_content were resolved once above
        # (record-at-injection): the write-ahead row, the fed block here, and
        # the post-turn row all carry the same object.
        try:
            request_fingerprint = hashlib.sha256(
                emit_json(
                    {
                        "root": session_id,
                        "client": client_message_id,
                        "turn": stream_emitter.turn_id,
                        "model": model_selection.get("effective_model"),
                        "message": message,
                    }
                ).encode("utf-8")
            ).hexdigest()
            executing_outcome = transition_mission_chat_turn(
                session_id=session_id,
                client_message_id=client_message_id,
                turn_id=stream_emitter.turn_id,
                state=TURN_STATE_EXECUTING,
                metadata={
                    "provider_submitted": True,
                    "provider_request_fingerprint": request_fingerprint,
                },
                elements=stream_emitter.elements,
            )
            if executing_outcome is not MissionChatTurnPersistOutcome.PERSISTED:
                raise RuntimeError(
                    f"provider boundary journal transition failed: {executing_outcome.value}"
                )
            provider_submitted = True
            if runtime_registry is not None:
                runtime_registry.transition(session_id, "busy")
            _persona_chat_fault_injection("after_provider_boundary")
            chat_result = GPTPersonaRuntime(
                default_provider=cfg.default_provider,
                default_model=cfg.default_model,
                session_db=session_db,
                persist_agent_session=True,
            ).mission_chat_reply(
                # Instance model-override tier folded in (api_mode included);
                # the chat-session override still wins via the explicit
                # provider_override/model_override args below.
                persona,
                chat_message,
                session_id=active_session_id,
                permission_session_id=session_id,
                conversation_history=native_history,
                reuse_current_user_message=(
                    journal_state == TURN_STATE_PENDING and bool(replay.get("operator"))
                ),
                relay_sender_marker=relay_sender_marker,
                root_chat_session_id=session_id,
                client_message_id=client_message_id,
                runtime_registry=runtime_registry,
                runtime_signature=turn_context.runtime_signature,
                # Receipt-only: lets a refused reuse name the component that
                # moved (`resident_rebuild_component_*` on the turn record)
                # rather than only reporting that the composite key changed.
                runtime_signature_components=turn_context.runtime_signature_digests,
                native_revision=native_revision_before,
                compression_threshold_tokens_override=getattr(
                    args, "compression_threshold_tokens", None
                ),
                compression_protect_first_n_override=getattr(
                    args, "compression_protect_first_n", None
                ),
                compression_protect_last_n_override=getattr(
                    args, "compression_protect_last_n", None
                ),
                provider_override=model_selection.get("effective_provider"),
                model_override=model_selection.get("effective_model"),
                # Per-instance reasoning-effort override for this turn (None =
                # inherit the runtime default). Applied to the model call by the
                # transport; unsupported/absent values fall back to the default.
                reasoning_effort=getattr(instance, "reasoning_effort", None),
                surface_prompt=getattr(args, "surface_prompt", "") or "",
                max_wall_seconds=relay_wall_seconds,
                stream_callback=_stream_delta if getattr(args, "stream", False) else None,
                # C8: pre-trace acks are presentation-only. The emitter turns the
                # payload into a v2 `turn.ack` stream frame — never a SessionDB
                # row, never a turn-store element; replay never shows it.
                pre_trace_callback=stream_emitter.ack,
                trace_callback=_stream_progress,
                agent_ready_callback=_agent_ready_for_steer,
                preloaded_skill_prompt=turn_context.skill_preload_prompt,
                workspace_agents_content=turn_context.workspace_agents_content,
                # The workspace POINTER (G6): the loaded AGENTS.md's own path, from
                # the receipt the loader already produced. Only a file that actually
                # LOADED points at a real workspace root — an invalid/missing/too
                # large selection must not ground the turn somewhere it never read.
                workspace_agents_path=turn_context.workspace_agents_path,
                situational_hud_content=situational_hud_content,
                turn_id=safe_assignment_token(client_message_id),
            )
        finally:
            relay_policy.RELAY_CHAIN.reset(_relay_chain_token)
            relay_policy.RELAY_DEADLINE.reset(_relay_deadline_token)
        # The model turn is over — every token that was going to arrive has.
        # Only reached when the run RETURNED; a turn that raised (wall budget,
        # provider failure) leaves `stream_done` absent, and with it the Stage 4
        # overlap count whose window it defines.
        turn_phases.mark("stream_done")
        # A COPY, not the runner's dict: the handler folds its own Stage-4
        # measurement in below, and the live result frame further down reads
        # ``chat_result.profile_timing`` directly. Copying keeps the frame the
        # runner's own accounting, byte-for-byte as it was, while the DURABLE
        # RECORD carries the turn's — which is a superset, and which is where
        # Stage 4's receipt was asked for.
        _profile_timing = dict(getattr(chat_result, "profile_timing", None) or {})
        # chat-turn-prep Stage 4: the handler's SessionDB open, folded into the
        # same block the runner's phases ride. ``safe_turn_profile_timing``
        # admits any ``*_ms`` int, so this needs no schema change — but it is
        # bounded there rather than trusted from here. It arrives on the turn
        # PLAN, which is the declared boundary between the phase that paid this
        # cost and this one. ABSENT when the plan carried no measurement (an
        # unavailable store returns before the plan is built), never a zero.
        _session_db_open_ms = getattr(plan, "session_db_open_ms", None)
        if isinstance(_session_db_open_ms, int) and not isinstance(
            _session_db_open_ms, bool
        ):
            _profile_timing["session_db_open_ms"] = _session_db_open_ms
        # Cold/warm, from the runner's own receipt rather than a guess here.
        # `resident_actor_reused` is written by the resident-actor registry on
        # every acquire; a turn whose runner reported none (no registry on this
        # path) leaves `agent_init_cold` ABSENT rather than claiming either.
        turn_phases.flag(
            "agent_init_cold",
            (not bool(_profile_timing.get("resident_actor_reused")))
            if "resident_actor_reused" in _profile_timing
            else None,
        )
        turn_phases.count(
            "builds_overlapped",
            _snapshot_builds_overlapped(
                turn_phases, until_ms=turn_phases.get("stream_done")
            ),
        )
        final_model_input = (getattr(chat_result, "raw", {}) or {}).get("model_input_observability")
        # C1 build-once: the row was built ONCE before the turn (record-at-
        # injection: history, skills, context files, the very situational_hud
        # dict rendered into the fed block). Attach the turn's results onto that
        # object instead of a full rebuild — the pre-C1 second build re-read
        # SessionDB history and re-scanned the skill catalog per turn. The
        # metered turn_usage is recorded at the injection site next to the
        # context it describes (never key-matched back on later).
        prompt_context = attach_prompt_observability_turn_results(
            prompt_context,
            final_model_input=final_model_input,
            model_selection=model_selection,
            turn_usage=turn_usage_from_result(chat_result),
            trace_events=trace_payloads,
        )
        if turn_context.skills.missing:
            prompt_context["queued_skill_load_errors"] = [
                {
                    "name": safe_assignment_token(skill) or str(skill),
                    "status": "missing",
                    "source": "queued_next_turn_skill",
                }
                for skill in turn_context.skills.missing
            ]
        persist_tool_turn_actual(
            persona_id=normalized_persona,
            session_id=session_id,
            # task_id/goal_id intentionally not passed: defaulted kwargs whose
            # only source on this path was the retired mission lane.
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
        stream_emitter.finish(state="failed")
        # A wall-budget death is NOT an ambiguous provider outcome: the harness
        # knows exactly why the turn stopped. Settle it as the typed terminal
        # `budget_exhausted` (no operator turn-resolve, never a frozen console
        # row) and hand back an honest synthesized account of what did run —
        # the live 2026-07-26 failure mode this replaces froze both ends of a
        # relay at `outcome_unknown` and cost a full re-brief.
        # ONE decision, made by the owned vocabulary rather than a nested
        # conditional spelled inline three times (state, kind, exit code).
        turn_outcome = classify_turn_failure(exc, provider_submitted=provider_submitted)
        wall_budget_exceeded = (
            turn_outcome.execution_state is ExecutionState.BUDGET_EXHAUSTED
        )
        failed_outcome = None
        if wall_budget_exceeded:
            budget_block = dict(getattr(exc, "wall_budget", None) or {})
            checkpoint_summary = _turn_budget.synthesize_checkpoint_summary(
                None, tool_names=_chat_turn_tool_names(stream_emitter.elements)
            )
            failed_outcome = transition_mission_chat_turn(
                session_id=session_id,
                client_message_id=client_message_id,
                turn_id=stream_emitter.turn_id,
                elements=stream_emitter.elements,
                state=TURN_STATE_BUDGET_EXHAUSTED,
                metadata={
                    "provider_submitted": True,
                    "budget_exhausted": True,
                    "budget_trigger": safe_assignment_token(
                        budget_block.get("trigger")
                    )
                    or "wall_budget_hard_wall",
                    "budget_summary": safe_assignment_text(str(exc), limit=400),
                    "stored_reply": checkpoint_summary,
                    # However far the turn got. A wall-budget death usually
                    # HAS provider_first_byte (the agent replied, then ran out
                    # of clock) and never has stream_done — the two together
                    # are what say "the tokens were flowing when the wall
                    # closed", which no prose line has ever said.
                    MISSION_CHAT_TURN_PHASES_KEY: turn_phases.snapshot(),
                    # The WHOLE accounting block, verbatim. A raised run has no
                    # result to carry it, so it rides the exception — and this
                    # is the only place it can become durable, because a pure
                    # chat turn writes no run record.
                    **turn_run_budget_metadata(error=exc),
                },
            )
        elif provider_submitted:
            failed_outcome = transition_mission_chat_turn(
                session_id=session_id,
                client_message_id=client_message_id,
                turn_id=stream_emitter.turn_id,
                elements=stream_emitter.elements,
                state=TURN_STATE_OUTCOME_UNKNOWN,
                # A non-wall budget trip (read/search loop, api calls, tokens)
                # settles here, and it is bounded just as knowably. Yields {}
                # for any other exception, so absence stays absence.
                metadata={
                    "provider_submitted": True,
                    MISSION_CHAT_TURN_PHASES_KEY: turn_phases.snapshot(),
                    **turn_run_budget_metadata(error=exc),
                },
            )
        if runtime_registry is not None:
            runtime_registry.transition(session_id, "failed")
        data = {
            "ok": False,
            "capability_id": "mission.chat.message",
            "execution_state": turn_outcome.execution_state,
            "error_kind": turn_outcome.error_kind,
            "persona_instance_id": instance.id,
            "persona_id": normalized_persona,
            "session_id": session_id,
            "root_chat_session_id": session_id,
            "client_message_id": client_message_id,
            "turn_id": stream_emitter.turn_id,
            "blocker": safe_assignment_text(str(exc), limit=240),
            "prompt_context_id": prompt_context["context_id"],
            # C3: failure frames carry the SAME slim block, never the full row.
            "prompt_observability": slim_chat_final_observability(prompt_context),
            "model_selection": model_selection,
            "next_expected": (
                "send a new client_message_id with a smaller scope or a larger --max-seconds; this turn is settled and needs NO turn-resolve"
                if wall_budget_exceeded
                else (
                    "resolve the exact outcome_unknown turn with action=abandon, then send a new client_message_id"
                    if provider_submitted
                    else "retry this client_message_id; Hermes did not cross the provider boundary"
                )
            ),
        }
        _stamp_finalization(data)
        if wall_budget_exceeded:
            data.update(
                {
                    "budget_exhausted": True,
                    "turn_resolution_required": False,
                    "journal_state": TURN_STATE_BUDGET_EXHAUSTED,
                    "wall_budget": dict(getattr(exc, "wall_budget", None) or {}),
                    "checkpoint_summary": _turn_budget.synthesize_checkpoint_summary(
                        None, tool_names=_chat_turn_tool_names(stream_emitter.elements)
                    ),
                }
            )
        if failed_outcome is not None and failed_outcome is not MissionChatTurnPersistOutcome.PERSISTED:
            data["turn_persist_outcome"] = failed_outcome.value
        _mission_chat_emit(args, data, data["blocker"])
        return turn_outcome.exit_code

    reply_text = _redact_persona_chat_text(getattr(chat_result, "final_response", "") or "", limit=PERSONA_CHAT_REPLY_LIMIT)
    active_session_id = _persona_chat_native_tip(session_db, session_id)
    native_revision = _persona_chat_native_revision(session_db, session_id)
    # Graceful checkpoint: the wall budget ended this turn, but it ended it at a
    # boundary and the agent still produced a real, durable reply. That reply
    # MUST project like any other (the whole point of the checkpoint), so the
    # journal keeps its normal native_committed -> projected walk; the
    # budget provenance rides the record metadata and the terminal frame so
    # "why is this reply a checkpoint?" is answerable from the record.
    budget_checkpoint = (getattr(chat_result, "raw", None) or {}).get(
        "wall_budget_checkpoint"
    )
    budget_checkpoint = budget_checkpoint if isinstance(budget_checkpoint, dict) else None
    budget_engaged = bool(budget_checkpoint and budget_checkpoint.get("engaged"))
    budget_metadata: dict[str, object] = {
        # UNCONDITIONAL, unlike the checkpoint provenance below: the accounting
        # block is the answer to "what bounded this turn?" and an UNTRIPPED turn
        # answers it too ("nothing did, and here is the headroom"). Gating it on
        # `budget_engaged` would keep exactly the pre-2026-07-27 blindness — a
        # turn that stopped at its bound and one that finished with room to
        # spare would again be indistinguishable from the record. Yields {} when
        # the run declared no budget at all, so absence still means absence.
        **turn_run_budget_metadata(result=chat_result),
        **(
            {
                "budget_exhausted": True,
                "budget_trigger": safe_assignment_token(budget_checkpoint.get("trigger"))
                or "wall_budget_checkpoint",
                "budget_summary": safe_assignment_text(
                    f"wall budget checkpoint: "
                    f"{budget_checkpoint.get('remaining_at_checkpoint_seconds')}s left of "
                    f"{budget_checkpoint.get('total_seconds')}s when new tool work stopped",
                    limit=400,
                ),
            }
            if budget_engaged
            else {}
        ),
    }
    if runtime_registry is not None:
        runtime_registry.finish(
            session_id,
            active_session_id=active_session_id,
            revision=native_revision,
        )
    turn_phases.mark("native_committed")
    _settled = transition_mission_chat_turn(
        session_id=session_id,
        client_message_id=client_message_id,
        turn_id=stream_emitter.turn_id,
        state=TURN_STATE_NATIVE_COMMITTED,
        elements=stream_emitter.elements,
        metadata={
            MISSION_CHAT_TURN_PHASES_KEY: turn_phases.snapshot(),
            "root_chat_session_id": session_id,
            "active_session_id": active_session_id,
            "continuity_runtime": (
                runtime_registry.observation(session_id, owning_process=True)
                if runtime_registry is not None
                else {
                    "runtime_state": "unknown",
                    "runtime_observer_id": "external_cli",
                }
            ),
            "native_revision": native_revision,
            "native_committed": True,
            "stored_reply": reply_text,
            **budget_metadata,
            # The runner's per-run timing breakdown, riding the SAME persist as
            # the run-budget block above and bounded by the same store-side
            # sanitizer (`mission_chat_turns.safe_turn_profile_timing`: `*_ms`
            # ints, `resident_actor_reused`, `resident_rebuild_*`, nothing
            # else). The phase block spans profile bootstrap in ONE number
            # (`write_ahead → agent_ready`); this says which part of it was
            # runtime resolution, MCP admission, or agent construction, so a
            # prep-cost remedy has a before/after receipt on the record instead
            # of a log-grep. Absent when the runner reported nothing — an empty
            # dict would claim an accounting nobody took.
            **(
                {MISSION_CHAT_TURN_PROFILE_TIMING_KEY: dict(_profile_timing)}
                if _profile_timing
                else {}
            ),
        },
    )
    _route_turn_write(_settled, step="native_commit")
    # The reply is durable in SessionDB (the runtime persisted it natively) and
    # in the turn journal — so mirror it now, at the one point on this lane that
    # KNOWS a real recorded reply exists. Deduped on (role, client_message_id),
    # which is what makes the recovery/replay walks above safe to re-enter.
    _mirror_persona_chat_message(
        session_db=session_db,
        session_id=session_id,
        role="assistant",
        text=reply_text,
        client_message_id=client_message_id,
        turn_id=stream_emitter.turn_id,
    )
    # The native reply is durable. Projection/bookkeeping failures deliberately
    # leave `native_committed` intact so an idempotent retry can repair the
    # projection without ever invoking the provider again.
    terminal_outcome: MissionChatTurnPersistOutcome | None = None
    try:
        _persona_chat_fault_injection("after_native_commit")
        # Token accounting: NONE here, on purpose. This lane runs the agent
        # natively bound to the chat session (mission_chat_reply receives
        # session_id=active_session_id with persist_agent_session=True), so
        # conversation_loop/codex_runtime already record every API call's usage
        # onto the bound session row. Adding the turn totals again via
        # _update_persona_chat_token_counts double-counted every counter
        # (input/output/cache/reasoning/api_call_count) at exactly 2x — the
        # per-call runtime writes are the single usage authority on this lane.
        # The scratch-session assignment lane (session_id=None) keeps its
        # explicit post-turn write.
        try:
            instance.active_run_id = None
            instance.current_assignment_id = None
            instance.state = WorkerSessionState.IDLE
            instance.default_chat_session_id = session_id
            instance_store.update(instance)
        except Exception as instance_commit_exc:
            # NOT silent any more. This write is what returns the agent to idle
            # and repoints its default thread; swallowing its failure is why a
            # cockpit could render an agent ``busy`` forever after a completed
            # turn with nothing anywhere saying so. It still must not fail the
            # turn — the reply is durable — so it becomes a typed warning.
            _warn(
                FinalizationWarningKind.INSTANCE_STATE_COMMIT_FAILED,
                type(instance_commit_exc).__name__,
                step="return_instance_to_idle",
            )

        # Clarify accounting, in this order and only now that the reply is
        # durable: SETTLE the question this turn answered before MINTING a
        # ticket for any question it asks. Reversed, the tokenless settlement
        # would find the ticket this very turn just created and mark a brand-new
        # question answered by the turn that asked it.
        clarify_binding = _settle_mission_chat_clarify_binding(
            clarify_binding,
            session_id=session_id,
            client_message_id=client_message_id,
            explicit_session_id=stated_session_id,
        )
        clarify_request = _mission_chat_clarify_request_payload(
            chat_result,
            session_id=session_id,
            persona_id=normalized_persona,
            persona_instance_id=instance.id,
            client_message_id=client_message_id,
            turn_id=stream_emitter.turn_id,
            requested_by_session=requested_by_session,
        )

        # RO-7, built HERE and not inside the payload: the block is a COPY of
        # what the ledger record carries, so it is read off the same two
        # instruments the terminal persist below writes — `turn_phases` for the
        # marks and counters, the handler's `_profile_timing` superset for the
        # runner's durations. Built at commit, from the record's own numbers,
        # so the frame and the file can never disagree about a turn.
        turn_timing = turn_timing_block(
            phases=turn_phases.snapshot(), profile_timing=_profile_timing
        )
        data = {
            "ok": True,
            "protocol_version": 2 if stream else None,
            "capability_id": "mission.chat.message",
            "agent_profile_id": instance.id,
            "persona_instance_id": instance.id,
            "persona_id": normalized_persona,
            "session_id": session_id,
            "chat_session_id": session_id,
            "root_chat_session_id": session_id,
            # How this turn's thread was established: {fresh, reason,
            # predecessor_session_id}. A dispatching agent reads it to know
            # whether it just opened a task-scoped thread (and which thread that
            # supersedes) or continued an existing one — the same lineage the
            # session meta records as `_dispatched_from`.
            "session_established": session_established,
            # Clarify binding — a TOP-LEVEL SIBLING of session_established, not a
            # field inside it: that block's shape is pinned by contract, and
            # nesting here would break it. Present only when this turn presented
            # a clarify token or settled an open ticket; absent means neither
            # happened, which is the whole normal path. `bound_via` is the
            # adoption signal (`clarify_token` = the runtime bound it,
            # `session_id` = the caller named the right thread themselves,
            # `none` = they landed there by inheritance).
            **({"clarify_binding": clarify_binding} if clarify_binding else {}),
            "active_session_id": active_session_id,
            # S30: no task binding key, retired with the replay envelope's
            # copy above.
            "relay_chain": list(turn_relay_chain),
            "client_message_id": client_message_id,
            # A checkpointed turn is a SUCCESS with a truncated scope, not a
            # failure: a real reply was produced and committed. `ok` stays true
            # so relay callers do not treat it as an error; the typed
            # `execution_state` + `budget_exhausted` flag carry the truncation,
            # and the operator is never told to run turn-resolve.
            "execution_state": (
                ExecutionState.BUDGET_EXHAUSTED
                if budget_engaged
                else ExecutionState.COMPLETED
            ),
            **(
                {
                    "budget_exhausted": True,
                    "turn_resolution_required": False,
                    "wall_budget": budget_checkpoint,
                }
                if budget_engaged
                else {}
            ),
            "kind": "mission_chat_message",
            "intent_hint": safe_assignment_token(getattr(args, "intent_hint", None)) or "chat",
            "surface_prompt": safe_assignment_text(getattr(args, "surface_prompt", ""), limit=4000) or "",
            "limiting_wrapper_active": False,
            "reply": reply_text,
            # Structured clarify-back (non-blocking clarify tool on this lane):
            # when present, the agent is asking a question whose answer is the
            # operator's / caller's next message in this same session. The HUD
            # renders `choices` as pickable rows; agent_chat_send forwards it up
            # the relay so a briefed child can surface context only it has.
            # `clarify_token` rides inside it: echo that token back on the reply
            # and the runtime — not the model's memory for opaque ids — puts the
            # answer in this thread.
            "clarify_request": clarify_request,
            "turn_id": safe_assignment_token(client_message_id),
            "run_ids": [],
            "input_tokens": getattr(chat_result, "input_tokens", None),
            "output_tokens": getattr(chat_result, "output_tokens", None),
            "total_tokens": getattr(chat_result, "total_tokens", None),
            # Turn latency accounting (see harness-serve brain note, 2026-07-08):
            # latency_ms is the whole runner.run wall; profile_timing carries the
            # per-phase breakdown (agent construct, provider dispatch, stream).
            # Without these, diagnosing a slow chat turn needs an in-process probe.
            "latency_ms": getattr(chat_result, "latency_ms", None),
            "profile_timing": dict(getattr(chat_result, "profile_timing", None) or {}) or None,
            # RO-7: the same numbers, joined and named for a person. The two
            # instruments above are the runner's own namespace and the record's
            # phase marks; this is the seven-key projection of them, and it is
            # what the launcher's `[MissionChatTiming]` line appends — so "where
            # did this turn's time go" is one grep instead of a script that
            # joins a diag line to a ledger file on the turn id. ABSENT when the
            # turn knew nothing, and each key absent when its own phase never
            # happened — never a zero. Additive: no key here moves and no
            # contract integer moves with it.
            **({MISSION_CHAT_TURN_TIMING_KEY: turn_timing} if turn_timing else {}),
            "resident_actor_reused": bool(
                (getattr(chat_result, "profile_timing", None) or {}).get(
                    "resident_actor_reused"
                )
            ),
            "rehydrated": not bool(
                (getattr(chat_result, "profile_timing", None) or {}).get(
                    "resident_actor_reused"
                )
            ),
            "prompt_context_id": prompt_context["context_id"],
            # C3 (2026-07-17): the terminal frame carries the turn's facts ONCE,
            # small — the slim typed subset (ruling §7.3), not the full ~26 KB
            # record-at-injection row. The launcher reads exactly these fields
            # off the live frame; the complete row stays on disk (persisted +
            # archived). Same slim shape on stream and non-stream (one dict).
            "prompt_observability": slim_chat_final_observability(prompt_context),
            "queued_skills_loaded": list(turn_context.skills.loaded),
            "queued_skills_missing": list(turn_context.skills.missing),
            "model_selection": model_selection,
            "next_expected": (
                "wall budget ran out: this is the agent's final checkpoint reply, "
                "already committed. Send a new client_message_id to continue "
                "(no turn-resolve required); raise --max-seconds or narrow the ask"
                if budget_engaged
                else "agent replied through the canonical Mission Control chat path; refresh Harness snapshot for transcript and Initial Chat Context"
            ),
        }
        _stamp_turn_visibility(data, reply_text, chat_result=chat_result)
        _stamp_reply_media(data, reply_text, args)
        _stamp_finalization(data)
        stream_emitter.finish(
            state="completed",
            input_tokens=data.get("input_tokens"),
            output_tokens=data.get("output_tokens"),
            total_tokens=data.get("total_tokens"),
        )
        turn_phases.mark("projected")
        terminal_outcome = transition_mission_chat_turn(
            session_id=session_id,
            client_message_id=client_message_id,
            turn_id=stream_emitter.turn_id,
            elements=stream_emitter.elements,
            state=TURN_STATE_PROJECTED,
            metadata={
                # The complete block, on the terminal record. Every earlier
                # persist carried a prefix of it; this is the one a latency
                # audit reads.
                MISSION_CHAT_TURN_PHASES_KEY: turn_phases.snapshot(),
                "projection_committed": True,
                "stored_reply": reply_text,
                "active_session_id": active_session_id,
                "native_revision": native_revision,
            },
        )
        if terminal_outcome is not MissionChatTurnPersistOutcome.PERSISTED:
            data["turn_persist_outcome"] = terminal_outcome.value
        else:
            _publish_persona_chat_projection_event(
                session_id=session_id,
                client_message_id=client_message_id,
                turn_id=stream_emitter.turn_id,
                persona_id=normalized_persona,
                persona_instance_id=instance.id,
                active_session_id=active_session_id,
                native_revision=native_revision,
            )
        if write_ahead_outcome is not MissionChatTurnPersistOutcome.PERSISTED:
            data["turn_write_ahead_outcome"] = write_ahead_outcome.value
        # C3: `turn_elements` DROPPED from the terminal frame — the launcher
        # never decoded them (turn structure arrives via the incremental v2
        # frames), and the turn store is the element/replay authority for
        # reconnect. Emitting them here was a pure duplicate carriage.
        _mission_chat_emit(
            args,
            data,
            f"mission chat reply for {normalized_persona}",
            stream=stream,
        )
        # Auto-title is a SessionDB-only side effect that NOTHING in the emitted
        # frame depends on, and it costs a synchronous auxiliary-LLM RTT on a
        # session's first turn — an RTT that walks the whole provider-resolution
        # chain and can lazily pip-install a provider on the way (2026-08-09:
        # 46 seconds of it, ending in a 401 on a revoked OAuth token).
        #
        # It was already placed AFTER the terminal frame so that RTT stayed off
        # the last-delta -> chat.final critical path. That was necessary and not
        # sufficient: it still ran inside the chat-root lease, so for those 46
        # seconds the root REFUSED the operator's next send with ``chat_busy``
        # while his answer sat on screen. Post-emit is not the same boundary as
        # post-lease, and the operator experiences the second one.
        #
        # So it is packaged, not run. The caller invokes this thunk once the
        # ``with persona_chat_root_lease(...)`` block has exited; nothing inside
        # it touches turn or transcript state, so the root does not need to be
        # serialised for any of it. The internal try/except stays: the helper
        # already swallows, and ``run_once`` swallows too, but a raise from here
        # while the thunk is being BUILT (not run) would still reach the
        # crash-tail guard below and corrupt the one-JSON-object stdout
        # contract.
        def _deferred_auto_title() -> None:
            title_before = session_db.get_session_title(session_id)
            _maybe_auto_title_persona_chat(
                session_db=session_db,
                session_id=session_id,
                user_message=message,
                assistant_response=reply_text,
            )
            if session_db.get_session_title(session_id) != title_before:
                _publish_persona_chat_metadata_event(
                    session_id=session_id,
                    persona_id=normalized_persona,
                    persona_instance_id=instance.id,
                )

        try:
            deferred.defer(_deferred_auto_title)
        except Exception:
            pass
        return 0
    except Exception as exc:
        if terminal_outcome is not None:
            # The record is already settled; stdout may be mid-write, so a
            # second JSON object would corrupt the contract. Crash honestly.
            raise
        stream_emitter.finish(state="failed")
        data = {
            "ok": False,
            "capability_id": "mission.chat.message",
            "execution_state": ExecutionState.FAILED,
            "error_kind": ChatErrorKind.CHAT_PROJECTION_INCOMPLETE,
            "persistence_operation": (
                exc.operation if isinstance(exc, PersonaChatPersistenceError) else None
            ),
            "persona_instance_id": instance.id,
            "persona_id": normalized_persona,
            "session_id": session_id,
            "root_chat_session_id": session_id,
            "active_session_id": active_session_id,
            "client_message_id": client_message_id,
            "turn_id": stream_emitter.turn_id,
            "reply": reply_text,
            "blocker": safe_assignment_text(str(exc), limit=240),
            "prompt_context_id": prompt_context["context_id"],
            # C3: same slim block on this failure lane too, so the peek's live
            # fallback (situational HUD + turn usage) still resolves when the
            # agent replied but the record settle failed.
            "prompt_observability": slim_chat_final_observability(prompt_context),
            "model_selection": model_selection,
            "next_expected": "retry this client_message_id to repair projection from the native committed reply",
        }
        # The guarded block starts AFTER the run returns and after `reply_text`
        # is derived from it, so both names are bound on every path that
        # reaches this handler: a projection failure gets the same evidence the
        # success payload would have had. What failed here is persistence, not
        # the turn — the reply may well be real and visible, and saying so is
        # what lets a repair retry be told apart from a silent turn.
        _stamp_turn_visibility(data, reply_text, chat_result=chat_result)
        _stamp_reply_media(data, reply_text, args)
        _stamp_finalization(data)
        _mission_chat_emit(args, data, data["blocker"])
        return 2


def _cmd_mission_chat_queue_skill(args) -> int:
    persona_id = safe_assignment_token(getattr(args, "persona_id", None))
    session_id = safe_assignment_token(getattr(args, "session_id", None))
    # Both spellings collapse deliberately: this verb refuses below unless
    # at least one skill survives, so "flag absent" and "flag given empty"
    # reach the same refusal and no store can tell them apart.
    raw_skills = [
        *list_flag_or_empty(args, "skill"),
        *list_flag_or_empty(args, "skills"),
    ]
    skills = list(
        dict.fromkeys(
            token
            for item in raw_skills
            if (token := safe_assignment_token(item))
        )
    )
    if not persona_id or not session_id or not skills:
        data = {
            "ok": False,
            "error": "persona, session-id, and at least one skill are required",
        }
        print(emit_json(data) if args.json else data["error"])
        return 2
    from agent.skill_utils import resolve_skill, skill_runtime_compatibility

    resolutions = {skill: resolve_skill(skill) for skill in skills}
    rejected = {
        skill: result.status
        for skill, result in resolutions.items()
        if result.status != "resolved"
    }
    for skill, result in resolutions.items():
        compatibility = skill_runtime_compatibility(
            result.candidate,
            surface="mission_chat",
            root_node_mode=False,
        )
        if not compatibility["compatible"]:
            rejected[skill] = compatibility["reason"]
    if rejected:
        data = {
            "ok": False,
            "error": "one or more skills are not loadable",
            "rejected_skills": rejected,
        }
        print(emit_json(data) if args.json else data["error"])
        return 2
    from agent_runtime.queued_skills import queue_skills_for_next_turn

    queued = queue_skills_for_next_turn(
        persona_id=persona_id,
        session_id=session_id,
        persona_instance_id=getattr(args, "persona_instance_id", None),
        skills=skills,
    )
    data = {
        "ok": True,
        "capability_id": "mission.chat.queue_skill_for_next_turn",
        "persona_id": persona_id,
        "persona_instance_id": safe_assignment_token(getattr(args, "persona_instance_id", None)),
        "session_id": session_id,
        "skills": skills,
        "queued_skills": queued.get("skills", []),
        "next_expected": "send the next Mission Control chat message; queued skills will be preloaded for that turn only",
    }
    print(emit_json(data) if args.json else f"queued {', '.join(skills)} for next turn")
    return 0


#: The three ways a clarify question's answer can have reached its thread, plus
#: the bucket for a question that has not been answered at all. ORDERED, because
#: the histogram is read as a ladder: `clarify_token` is the runtime owning the
#: binding, `session_id` is a caller that complied with the prompt, `none` is the
#: bug still happening, and `unsettled` is a question nobody answered yet.
CLARIFY_BOUND_VIA_BUCKETS = ("clarify_token", "session_id", "none", "unsettled")


def _clarify_ticket_row(record: dict, *, now: float, ttl_seconds: float) -> dict:
    created_at = float(record.get("created_at") or 0.0)
    answered_at = record.get("answered_at")
    return {
        # The token is a LOOKUP KEY validated against a stored record, never a
        # capability secret (see PersonaChatClarifyTicketStore), so printing it
        # is what makes this readout actionable: an operator can hand a stuck
        # agent the token its answer should have carried.
        "id": safe_assignment_text(record.get("clarify_token"), limit=240) or None,
        "state": str(record.get("state") or "") or None,
        "bound_via": record.get("bound_via") or None,
        "age_seconds": round(max(now - created_at, 0.0), 3) if created_at else None,
        # Orthogonal to state, and deliberately so: TTL governs GC only, so an
        # expired ticket is one the sweep MAY prune — it still binds until the
        # file is actually gone. Reporting it as a state would claim a cliff the
        # store does not have.
        "expired": bool(created_at and (now - created_at) > ttl_seconds),
        "chat_session_id": safe_assignment_text(record.get("chat_session_id"), limit=240)
        or None,
        "persona_id": record.get("persona_id") or None,
        "persona_instance_id": record.get("persona_instance_id") or None,
        "asked_by_client_message_id": record.get("asked_by_client_message_id") or None,
        "answered_by_client_message_id": record.get("answered_by_client_message_id") or None,
        "requested_by_session": record.get("requested_by_session") or None,
        "answered_age_seconds": (
            round(max(now - float(answered_at), 0.0), 3) if answered_at else None
        ),
    }


#: What the redeliver verb tells the operator to do about each refusal. Beside
#: the outcomes rather than inside the handler so a new outcome cannot ship
#: without an answer to "so what do I do".
_DISPATCH_REDELIVER_NEXT_EXPECTED = {
    "not_found": "check the dispatch id; only rows still inside the store's retention window exist",
    "already_delivered": "nothing to do — the sender already received this reply",
    "not_dropped": "the row is still queued; the drain will deliver it on its next pass",
}


def _cmd_mission_chat_dispatch_redeliver(args) -> int:
    """Re-arm one DROPPED dispatch reply for another delivery pass.

    The operator's way back from a terminal give-up. A dropped row still holds a
    real answer that its sender was never told — the 2026-08-24
    ``foreign_chat_session`` outage produced a queue full of exactly these — and
    once the reason the delivery was refused is fixed and a serve restart has
    picked the fix up, the row deserves another pass rather than a hand-written
    re-send that loses the dispatch's provenance.

    Refusals are TYPED, not silent: re-arming a delivered row would deliver a
    second copy, and re-arming a pending one would tell an operator something
    happened when nothing did.

    Never prints message bodies. The ask and the reply live on the row, the row
    is a queue record and not a transcript, and an operator repairing a delivery
    queue does not need to read either — the thread this re-arms into is where
    the text belongs.
    """

    # Function-local, per the exec'd-part discipline: this file is exec'd into
    # harness.py's globals, so a module-level import here would need a matching
    # one there or it is a NameError on a LIVE turn. Neither vocabulary is
    # re-spelled — the admission kind is the enum member, and the queue's own
    # refusals are read off ``dispatch_store``, which owns them.
    from agent_runtime import dispatch_store
    from agent_runtime.mission_chat_outcome import ChatErrorKind

    dispatch_id = safe_assignment_text(getattr(args, "dispatch_id", None), limit=200)
    if not dispatch_id:
        data = attach_root_observability(
            {
                "ok": False,
                "capability_id": "mission.chat.dispatch.redeliver",
                "error_kind": ChatErrorKind.INVALID_REQUEST,
                "error": "dispatch_id is required",
            }
        )
        print(emit_json(data) if args.json else data["error"])
        return 2

    try:
        outcome, row = dispatch_store.rearm_delivery(dispatch_id)
    except Exception as exc:
        data = attach_root_observability(
            {
                "ok": False,
                "capability_id": "mission.chat.dispatch.redeliver",
                "error_kind": dispatch_store.ERROR_KIND_DISPATCH_STORE_UNAVAILABLE,
                "error": safe_assignment_text(str(exc), limit=320),
                "dispatch_id": dispatch_id,
            }
        )
        print(emit_json(data) if args.json else data["error"])
        return 2

    record = row if isinstance(row, dict) else {}
    # The queue-record fields ONLY. `ask` / `reply` are deliberately absent.
    state = {
        "dispatch_id": dispatch_id,
        "state": record.get("state") or "",
        "delivery_state": record.get("delivery_state") or "",
        "delivery_attempts": int(record.get("delivery_attempts") or 0),
        "delivery_error": record.get("delivery_error") or None,
        "target_persona": record.get("target_persona") or "",
        "target_instance_id": record.get("target_instance_id") or "",
        "sender_session_id": record.get("sender_session_id") or "",
    }
    if outcome != dispatch_store.REARM_REARMED:
        data = attach_root_observability(
            {
                "ok": False,
                "capability_id": "mission.chat.dispatch.redeliver",
                "error_kind": dispatch_store.REARM_ERROR_KINDS.get(
                    outcome, dispatch_store.ERROR_KIND_DISPATCH_STORE_UNAVAILABLE
                ),
                "error": f"dispatch delivery cannot be re-armed: {outcome}",
                "outcome": outcome,
                **state,
                "next_expected": _DISPATCH_REDELIVER_NEXT_EXPECTED.get(
                    outcome, "inspect the dispatch row before re-arming it"
                ),
            }
        )
        print(emit_json(data) if args.json else data["error"])
        return 2

    data = attach_root_observability(
        {
            "ok": True,
            "capability_id": "mission.chat.dispatch.redeliver",
            "outcome": outcome,
            **state,
            "next_expected": (
                "the serve delivery drain picks it up on its next pass; a serve "
                "process without the fix that dropped it will drop it again"
            ),
        }
    )
    print(
        emit_json(data)
        if args.json
        else (
            f"re-armed {dispatch_id}: delivery_state="
            f"{state['delivery_state']} attempts={state['delivery_attempts']}"
        )
    )
    return 0


def _cmd_mission_chat_clarify_tickets(args) -> int:
    """Read-only adoption readout for the clarify-token binding.

    The design's rollout step: watch echo adoption climb, WITHOUT new event
    kinds (telemetry is not the EventLog here). Everything below is read from
    state the binding already records — the per-turn ``clarify_binding.bound_via``
    is mirrored onto the ticket at settle, so the ticket files alone answer it.

    ``bound_via: "none"`` is the number that matters. It counts questions whose
    answer landed in a thread the caller neither named nor bound — the original
    defect, still happening. ``clarify_token`` climbing against it is the whole
    point of the feature; ``unsettled`` is a question nobody has answered yet and
    is not evidence either way.

    Strictly read-only: no mint, no settle, and NO SWEEP. A readout that pruned
    would silently change the very population it is reporting on, and an operator
    checking adoption twice would get two different denominators."""

    store = PersonaChatClarifyTicketStore()
    now = time.time()
    ttl_seconds = float(CLARIFY_TICKET_TTL_SECONDS)
    try:
        records, unreadable = store.scan_tickets()
    except OSError:
        records, unreadable = [], 0
    wanted_session = safe_assignment_text(getattr(args, "session_id", None), limit=240)
    wanted_state = safe_assignment_token(getattr(args, "state", None))
    rows = [_clarify_ticket_row(record, now=now, ttl_seconds=ttl_seconds) for record in records]

    # Counts are computed over the WHOLE store, before --session-id/--state/
    # --limit narrow the listing. A filtered view is a lens on the population,
    # not a redefinition of it: an adoption ratio that moved because the operator
    # asked to see fewer rows would be a lying metric.
    states: dict[str, int] = {}
    bound_via: dict[str, int] = {bucket: 0 for bucket in CLARIFY_BOUND_VIA_BUCKETS}
    expired = 0
    for row in rows:
        states[str(row["state"] or "unknown")] = states.get(str(row["state"] or "unknown"), 0) + 1
        bucket = str(row["bound_via"] or "unsettled")
        bound_via[bucket] = bound_via.get(bucket, 0) + 1
        if row["expired"]:
            expired += 1

    listed = rows
    if wanted_session:
        listed = [row for row in listed if row["chat_session_id"] == wanted_session]
    if wanted_state:
        listed = [row for row in listed if row["state"] == wanted_state]
    listed = _sort_rows(listed, getattr(args, "sort", None))
    limit = getattr(args, "limit", None)
    truncated = False
    if limit is not None and limit >= 0 and len(listed) > limit:
        listed = listed[:limit]
        truncated = True

    data = _list_envelope("clarify_ticket", listed, cursor=None, truncated=truncated)
    data.update(
        {
            "ok": True,
            "capability_id": "mission.chat.clarify_tickets",
            # The gate's CURRENT setting, which is not the same question as
            # whether the tickets below were minted under it: turning the gate
            # off stops minting but leaves the store readable, and those tickets
            # are exactly what an operator wants to see after a rollback.
            "binding_enabled": bool(mission_chat_clarify_token_binding()),
            "ttl_seconds": ttl_seconds,
            "total": len(rows),
            # Additive, and it belongs beside ``total`` because it is the same
            # question: how many tickets is this readout actually about. A file
            # that would not decode is missing from every count above, including
            # the denominator of the adoption ratio the block below tells the
            # operator to watch. Stating it is the difference between a metric
            # that is narrower than the store and one that lies about it.
            "unreadable": unreadable,
            "states": states,
            "bound_via": bound_via,
            "expired": expired,
            "next_expected": (
                "watch bound_via.none — every one of those is a clarify answer that "
                "opened a fresh thread instead of returning to the question"
            ),
        }
    )
    _print_stage42(data, args=args, default_output="json")
    return 0


def _cmd_mission_chat_turn_resolve(args) -> int:
    # Function-local: this file is exec'd into harness.py's globals, so a
    # module-level import here would need a matching harness.py import or it
    # is a NameError on a LIVE turn. The turn-outcome vocabulary is owned by
    # agent_runtime.mission_chat_outcome; nothing re-spells its values.
    from agent_runtime.mission_chat_outcome import ChatErrorKind
    from agent_runtime.mission_chat_turns import OPERATOR_RESOLVABLE_TURN_STATES
    session_id = safe_assignment_text(getattr(args, "session_id", None), limit=240)
    client_message_id = safe_assignment_text(
        getattr(args, "client_message_id", None), limit=240
    )
    turn_id = safe_assignment_token(getattr(args, "turn_id", None))
    action = safe_assignment_token(getattr(args, "action", None))
    persona_instance_id = safe_assignment_token(
        getattr(args, "persona_instance_id", None)
    )
    if action != "abandon" or not persona_instance_id:
        data = {
            "ok": False,
            "capability_id": "mission.chat.turn.resolve",
            "error_kind": ChatErrorKind.INVALID_REQUEST,
            "error": "action=abandon and persona_instance_id are required",
        }
        print(emit_json(data) if args.json else data["error"])
        return 2
    try:
        session_db = _default_persona_session_db()
        owner_instance_id = _persona_chat_session_owner(session_db, session_id)
        owner_instance = (
            PersonaInstanceStore().get(owner_instance_id)
            if owner_instance_id
            else None
        )
    except Exception:
        owner_instance = None
    if owner_instance is None or owner_instance.id != persona_instance_id:
        data = {
            "ok": False,
            "capability_id": "mission.chat.turn.resolve",
            "error_kind": ChatErrorKind.FOREIGN_CHAT_SESSION,
            "error": "chat root is not owned by the requested persona instance",
            "session_id": session_id,
            "persona_instance_id": persona_instance_id,
        }
        print(emit_json(data) if args.json else data["error"])
        return 2
    if not bool(getattr(args, "_persona_chat_resolve_lease_acquired", False)):
        try:
            with persona_chat_root_lease(
                session_id,
                owner_id=persona_instance_id,
                observer_kind="turn_resolve",
            ):
                args._persona_chat_resolve_lease_acquired = True
                try:
                    return _cmd_mission_chat_turn_resolve(args)
                finally:
                    args._persona_chat_resolve_lease_acquired = False
        except PersonaChatBusyError as exc:
            data = {
                "ok": False,
                "capability_id": "mission.chat.turn.resolve",
                "error_kind": ChatErrorKind.CHAT_BUSY,
                "session_id": session_id,
                "lease_owner": exc.owner,
                "error": str(exc),
            }
            print(emit_json(data) if args.json else data["error"])
            return 2
    record = mission_chat_turn_record(
        session_id=session_id, client_message_id=client_message_id
    )
    if (
        not record
        # The resolvable set is the turn store's own table, never a literal
        # spelled here — see mission_chat_turns' vocabulary guard.
        or record.get("state") not in OPERATOR_RESOLVABLE_TURN_STATES
        or safe_assignment_token(record.get("turn_id")) != turn_id
    ):
        data = {
            "ok": False,
            "capability_id": "mission.chat.turn.resolve",
            "error_kind": ChatErrorKind.CHAT_TURN_RESOLUTION_MISMATCH,
            "error": "resolution requires the exact matching outcome_unknown root/client/turn",
            "session_id": session_id,
            "client_message_id": client_message_id,
            "turn_id": turn_id,
        }
        print(emit_json(data) if args.json else data["error"])
        return 2
    outcome = abandon_mission_chat_turn(
        session_id=session_id,
        client_message_id=client_message_id,
        turn_id=turn_id,
        resolution_actor=persona_instance_id,
        resolution_reason=safe_assignment_text(
            getattr(args, "reason", None), limit=320
        )
        or "operator requested abandon and resend",
    )
    data = {
        "ok": outcome is MissionChatTurnPersistOutcome.PERSISTED,
        "capability_id": "mission.chat.turn.resolve",
        "resolution": "abandon",
        "journal_state": TURN_STATE_ABANDONED,
        "root_chat_session_id": session_id,
        "session_id": session_id,
        "client_message_id": client_message_id,
        "turn_id": turn_id,
        "next_expected": "send again with a new client_message_id",
    }
    print(emit_json(data) if args.json else "abandoned ambiguous chat turn")
    return 0 if data["ok"] else 2


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
        auth = review_coordinator_budget(
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


def _cmd_persona_instance_retire(args) -> int:
    """Instance end-of-life: archive a placement-backed persona-instance ROW.

    Unlike ``close``/``archive`` (which act on free-floating ASSIGNMENTS), this
    verb ends the deliberate instance itself — the operator ruling that deleting
    a placement is the instance's end-of-life. Refusals surface the typed
    ``PersonaInstanceRetireError.code`` so the launcher/operator can distinguish
    canonical-channel / active-binding / active-assignment / not-found."""
    cfg = load_agent_runtime_config()
    store = PersonaInstanceStore()
    coordinator_id = _coordinator_actor_id(args)
    if coordinator_id:
        try:
            target = store.get(args.persona_instance_id)
            persona = _persona_by_id(cfg, target.persona_id)
        except Exception:
            target = None
            persona = None
        scope = _coordinator_scope_from_args(args, cfg, persona)
        auth = review_coordinator_budget(
            "persona.instance.retire",
            scope,
            target,
            actor=coordinator_id,
            coordinator_id=coordinator_id,
        )
        if not auth.ok:
            data = _coordinator_confirm_payload("persona.instance.retire", coordinator_id, auth)
            print(emit_json(data) if args.json else data["status"])
            return 2
    # DELEGATES to the shared service (plan S5/D7) rather than calling the store
    # itself: the ack this verb prints and the one `harness agent retire` and
    # `runtime.agent.retire` print are now the same object built by the same
    # function, so ``archived_actor_keys`` / ``office_archive_failures`` /
    # ``already_retired`` arrive here too and a scripted operator does not have
    # to know which door they typed. The ENVELOPE below is unchanged — this
    # verb's `persona_instance_retired` key and its `code`-spelled refusal are
    # its operator surface, and the refusal is rendered from the service's typed
    # data rather than from a second `except` over the same store guard.
    outcome = _agent_retire_outcome(args)
    if outcome.refusal is not None:
        refusal = outcome.refusal
        reason = refusal.data.get("reason")
        data = {
            "ok": False,
            "error": reason,
            "code": reason,
            "message": refusal.message,
            **{k: v for k, v in refusal.data.items() if k != "reason"},
        }
        print(emit_json(data) if args.json else f"{reason}: {refusal.message}")
        return 2
    result = outcome.result
    data = {"ok": True, "persona_instance_retired": result}
    if args.json:
        print(emit_json(data))
    else:
        print(
            f"retired {result['persona_instance_id']} "
            f"({result['display_name']}) -> {result['archive_path']}"
        )
    return 0


# S66 removed ``_cmd_persona_instance_sweep_orphans`` and its ``sweep-orphans``
# subparser. The janitor it invoked reaped instances whose OWNING TASK had gone
# terminal, and S65 (`f9aa0faab`) retired that entire basis in ONE commit: the
# store method, the owner-release inference behind it, and the goal/task path
# helpers it read (`paths.task_path` / `goal_path` / `goals_dir`). Only the
# caller survived, so the verb raised `AttributeError` on every invocation. It
# is not restorable without a contract move either — the reap emitted
# `persona_instance.reaped`, which the same wave DE-REGISTERED, so
# `EventLog.append` would now refuse it. Retiring a placement is
# `persona instance retire`, which is live and untouched.


def _cmd_persona_instance_repair_steering(args) -> int:
    """Strip non-instance principals (e.g. the operator) out of a persona
    instance's steering fields. A steering parent is a persona-instance id; a
    principal that leaked into ``steered_by`` / ``spawned_by`` via a legacy mint
    renders as a phantom "steered by <principal>" edge. Honors --dry-run
    (validate + preview, write nothing, emit nothing)."""
    cfg = load_agent_runtime_config()
    target = safe_optional_token(getattr(args, "persona_instance_id", None))
    scan_all = bool(getattr(args, "all", False))
    if not target and not scan_all:
        data = {"ok": False, "error": "pass a persona_instance_id or --all"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    if target and scan_all:
        data = {"ok": False, "error": "pass either a persona_instance_id or --all, not both"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    dry_run = bool(getattr(args, "dry_run", False))
    store = PersonaInstanceStore()
    try:
        result = store.repair_non_instance_steering(target or None, apply=not dry_run)
    except Exception as exc:
        data = {"ok": False, "error": safe_assignment_text(str(exc), limit=240) or "repair failed"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    data = {"ok": True, "dry_run": dry_run, "persona_instance_steering_repair": result}
    if args.json:
        print(emit_json(data))
    else:
        verb = "would repair" if dry_run else "repaired"
        print(f"{verb} {result['repaired_count']} row(s) with non-instance steering entries")
        for rec in result["repaired"]:
            print(
                f"  {rec['persona_instance_id']}: "
                f"steered_by {rec['steered_by_before']} -> {rec['steered_by_after']}; "
                f"spawned_by {rec['spawned_by_before']!r} -> {rec['spawned_by_after']!r}; "
                f"removed {rec['removed_steered_by']}"
            )
    return 0


def _cmd_persona_instance_steer(args) -> int:
    cfg = load_agent_runtime_config()
    persona_instance_id = safe_assignment_token(args.persona_instance_id)
    if not persona_instance_id:
        data = {"ok": False, "error": "persona_instance_id is required"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    # Exactly one steering operation. --parent stays a back-compat alias for
    # "replace the set with this single parent"; the multi-parent verbs are
    # additive (--add-parent / --remove-parent) or declarative (--set-parents).
    detach = bool(getattr(args, "detach", False))
    parent_instance_id = safe_optional_token(getattr(args, "parent_instance_id", None))
    add_parent = safe_optional_token(getattr(args, "add_parent", None))
    remove_parent = safe_optional_token(getattr(args, "remove_parent", None))
    set_parents_raw = getattr(args, "set_parents", None)
    goal_id = None if detach else safe_optional_token(getattr(args, "goal_id", None))
    selected = [
        name
        for name, present in (
            ("detach", detach),
            ("parent", bool(parent_instance_id)),
            ("set_parents", set_parents_raw is not None),
            ("add_parent", bool(add_parent)),
            ("remove_parent", bool(remove_parent)),
        )
        if present
    ]
    if not selected:
        data = {"ok": False, "error": "one of --parent / --add-parent / --remove-parent / --set-parents / --detach is required"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    if len(selected) > 1:
        data = {"ok": False, "error": f"steer operations are mutually exclusive: got {', '.join(selected)}"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    op = selected[0]
    store = PersonaInstanceStore()
    try:
        target = store.get(persona_instance_id)
    except Exception:
        data = {"ok": False, "error": f"persona instance not found: {persona_instance_id}"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    before = list(target.steered_by)
    # 76D.3: re-routing a steering edge is a STEER verb (ungated); operator
    # actors bypass entirely. Coordinators still pass through the authorizer so
    # the contract stays uniform with create/kill paths.
    coordinator_id = _coordinator_actor_id(args)
    if coordinator_id:
        persona = _persona_by_id(cfg, target.persona_id)
        scope = _coordinator_scope_from_args(args, cfg, persona)
        auth = review_coordinator_budget("re_route", scope, target, actor=coordinator_id, coordinator_id=coordinator_id)
        if not auth.ok:
            data = _coordinator_confirm_payload("re_route", coordinator_id, auth)
            print(emit_json(data) if args.json else data["status"])
            return 2
    try:
        if op == "detach":
            updated = store.detach_parents(persona_instance_id)
        elif op == "add_parent":
            updated = store.add_parent(persona_instance_id, add_parent, goal_id=goal_id)
        elif op == "remove_parent":
            updated = store.remove_parent(persona_instance_id, remove_parent)
        elif op == "set_parents":
            updated = store.set_parents(persona_instance_id, list(set_parents_raw or []), goal_id=goal_id)
        else:  # "parent" — back-compat replace-with-one
            updated = store.set_parents(persona_instance_id, [parent_instance_id], goal_id=goal_id)
    except ValueError as exc:
        data = {"ok": False, "error": str(exc)}
        print(emit_json(data) if args.json else data["error"])
        return 2
    try:
        persona = _persona_by_id(cfg, updated.persona_id)
    except Exception:
        persona = None
    after = list(updated.steered_by)
    added = [pid for pid in after if pid not in before]
    removed = [pid for pid in before if pid not in after]
    data = {
        "ok": True,
        "detached": not after,
        "steered_by": after,
        "added": added,
        "removed": removed,
        "instance": persona_instance_summary(updated, persona),
    }
    parents_label = ",".join(after) if after else "(none)"
    print(emit_json(data) if args.json else f"steered {persona_instance_id}: parents={parents_label} goal={updated.goal_id}")
    return 0


def _cmd_persona_instance_return_summary(args) -> int:
    try:
        data = return_summary_to_parent_session(
            args.persona_instance_id,
            parent_session_id=args.parent_session_id,
            summary=args.summary,
            proof_ids=list_flag_or_empty(args, "proof_ids"),
            artifact_refs=list_flag_or_empty(args, "artifact_refs"),
        )
    except Exception as exc:
        data = {"ok": False, "capability_id": "persona.instance.return_summary", "error": safe_assignment_text(str(exc), limit=240)}
        print(emit_json(data) if args.json else data["error"])
        return 2
    print(emit_json(data) if args.json else f"returned {data['persona_instance_id']} -> {data['parent_session_id']}")
    return 0


def _cmd_persona_instance_update_profile(args) -> int:
    cfg = load_agent_runtime_config()
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
        auth = review_coordinator_budget("persona.instance.update_profile", scope, target, actor=coordinator_id, coordinator_id=coordinator_id)
        if not auth.ok:
            data = _coordinator_confirm_payload("persona.instance.update_profile", coordinator_id, auth)
            print(emit_json(data) if args.json else data["status"])
            return 2
    requested_skills = list_flag_or_absent(args, "skills")
    try:
        updated = store.update_profile(
            persona_instance_id,
            display_name=getattr(args, "display_name", None),
            current_chat_goal=getattr(args, "current_chat_goal", None),
            goal_id=getattr(args, "goal_id", None),
            # `None` when the flag was not given, and NEVER `[]` — which is
            # the whole content of `list_flag_or_absent`. Three handlers in
            # this file had each re-derived that rule in its own paragraph
            # before the reader existed; the name now carries it.
            #
            # THE BUG THIS REPLACES. `list(... or [])` handed the store an empty
            # LIST for every call that omitted `--skill`, and the store's own
            # contract is `if skills is not None or clear_skills:` — correct, and
            # correctly read as "the caller sent a list, write it". So
            # `persona instance update-profile <id> --display-name X` CLEARED
            # every skill override on that instance, silently, and the operator
            # who renamed an agent lost the skills it was assigned. The store was
            # never wrong; the collapse happened here, in the layer that is
            # supposed to translate "absent" into "absent".
            #
            # The launcher already defends against it from the outside
            # (`agent_chat/skills_context_controller.dart` refuses to write an
            # unproven baseline), which is a client working around a server bug —
            # not a fix, and not something a cron script or a remote `call` gets.
            skills=requested_skills,
            clear_skills=bool(getattr(args, "clear_skills", False)),
            # The third value of the skills tri-state, and the only one that
            # had no door before 2026-09-03: `--clear-skills` writes `[]`
            # ("explicitly none"), never `null` ("follow the template again"),
            # so one Save at "this agent" scope pinned the agent off its
            # persona forever. `getattr` with a default, like its siblings,
            # because `harness call` builds an args namespace by hand.
            inherit_skills=bool(getattr(args, "inherit_skills", False)),
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


class _SetModelRequestError(ValueError):
    """Typed validation failure for the set-model verbs (machine-readable error_code)."""

    def __init__(self, error_code: str, message: str, *, extra: dict | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.extra = dict(extra or {})


def _set_model_error_payload(exc: _SetModelRequestError, **identity) -> dict:
    return {
        "ok": False,
        "error_code": exc.error_code,
        "error": safe_assignment_text(str(exc), limit=320),
        **exc.extra,
        **identity,
        "next_expected": "fix the arguments and retry; no model settings were changed",
    }


def _parse_issued_at_arg(raw_value) -> datetime | None:
    """``--issued-at`` → aware/naive datetime, or ``None`` when not supplied.

    One spelling for every supersede-clock verb (``persona set-model``,
    ``persona instance set-model``, ``persona set-skills``): the launcher stamps
    ``Z``-suffixed timestamps that ``datetime.fromisoformat`` rejects on the
    Python versions this repo still supports, and a second copy of that
    workaround is a second place for it to go stale.
    """

    raw_issued = str(raw_value or "").strip()
    if not raw_issued:
        return None
    text = raw_issued[:-1] + "+00:00" if raw_issued.endswith("Z") else raw_issued
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise _SetModelRequestError("invalid_value", f"--issued-at is not an ISO-8601 timestamp: {raw_issued}") from exc


def _validated_set_model_request(args) -> dict:
    """Shared validation for `persona set-model` / `persona-instance set-model`.

    Provider must resolve in the plugin registry (canonical name is persisted;
    api_mode is derived from the provider profile so a lane switch can never
    strand a stale api_mode). Model ids are shape-checked only — the harness
    has no catalog authority, so catalog membership is deliberately NOT faked
    (`model_catalog_checked: false` in the envelope).
    """
    use_default = bool(getattr(args, "use_default", False) or getattr(args, "use_profile_default", False))
    try:
        provider_raw = _safe_chat_model_override_value(getattr(args, "provider", None), field="provider")
        model = _safe_chat_model_override_value(getattr(args, "model", None), field="model")
    except ValueError as exc:
        raise _SetModelRequestError("invalid_value", str(exc)) from exc
    # Reasoning-effort override rides the same set-model lane (per-instance).
    # ``None`` = not provided; ``""`` = clear back to the runtime default; a
    # level ("none"/minimal/low/medium/high/xhigh) sets it. Shape-checked here
    # so a bad value is a typed rejection, not a downstream ValueError.
    reasoning_raw = getattr(args, "reasoning_effort", None)
    reasoning_effort: str | None = None
    if reasoning_raw is not None:
        from hermes_constants import VALID_REASONING_EFFORTS

        reasoning_effort = str(reasoning_raw).strip().lower()
        if reasoning_effort and reasoning_effort != "none" and reasoning_effort not in VALID_REASONING_EFFORTS:
            raise _SetModelRequestError(
                "invalid_value",
                f"invalid reasoning effort: {reasoning_raw!r} (expected one of none, "
                f"{', '.join(VALID_REASONING_EFFORTS)})",
            )
    reasoning_provided = reasoning_effort is not None
    if use_default and (provider_raw or model or reasoning_provided):
        raise _SetModelRequestError("conflicting_args", "--use-default cannot be combined with --provider, --model, or --reasoning-effort")
    if not use_default and not provider_raw and not model and not reasoning_provided:
        raise _SetModelRequestError("missing_args", "pass --provider and/or --model, --reasoning-effort, or the use-default flag to clear the override")
    provider = None
    api_mode = None
    warnings: list[dict] = []
    if provider_raw:
        from providers import get_provider_profile, list_providers

        profile = get_provider_profile(provider_raw)
        if profile is None:
            known = sorted({str(item.name) for item in list_providers()})
            raise _SetModelRequestError(
                "unknown_provider",
                f"unknown provider: {provider_raw}",
                extra={"known_providers": known},
            )
        provider = str(profile.name)
        api_mode = str(getattr(profile, "api_mode", "") or "") or None
        import os as _os

        env_vars = tuple(getattr(profile, "env_vars", ()) or ())
        if env_vars and not any(_os.environ.get(name) for name in env_vars):
            warnings.append(
                {
                    "code": "provider_credentials_not_detected",
                    "message": f"no API-key env var ({', '.join(env_vars)}) detected on this host; OAuth/auth-store credentials may still apply at runtime",
                }
            )
    issued_at = _parse_issued_at_arg(getattr(args, "issued_at", None))
    return {
        "use_default": use_default,
        "provider": provider,
        "model": model,
        "api_mode": api_mode,
        "reasoning_effort": reasoning_effort,
        "issued_at": issued_at,
        "warnings": warnings,
    }


def _cmd_persona_instance_set_model(args) -> int:
    cfg = load_agent_runtime_config()
    persona_instance_id = safe_assignment_token(args.persona_instance_id)
    if not persona_instance_id:
        data = {"ok": False, "error_code": "persona_not_found", "error": "persona_instance_id is required"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    store = PersonaInstanceStore()
    try:
        target = store.get(persona_instance_id)
    except Exception:
        data = {"ok": False, "error_code": "persona_not_found", "error": f"persona instance not found: {persona_instance_id}"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    coordinator_id = _coordinator_actor_id(args)
    if coordinator_id:
        persona = _persona_by_id(cfg, target.persona_id)
        scope = _coordinator_scope_from_args(args, cfg, persona)
        auth = review_coordinator_budget("persona.instance.set_model", scope, target, actor=coordinator_id, coordinator_id=coordinator_id)
        if not auth.ok:
            data = _coordinator_confirm_payload("persona.instance.set_model", coordinator_id, auth)
            print(emit_json(data) if args.json else data["status"])
            return 2
    try:
        request = _validated_set_model_request(args)
    except _SetModelRequestError as exc:
        data = _set_model_error_payload(exc, persona_instance_id=persona_instance_id, scope="agent_instance")
        print(emit_json(data) if args.json else data["error"])
        return 2
    status = "applied"
    try:
        updated = store.update_profile(
            persona_instance_id,
            provider=request["provider"],
            model=request["model"],
            api_mode=request["api_mode"],
            reasoning_effort=request["reasoning_effort"],
            clear_model_override=request["use_default"],
            model_issued_at=request["issued_at"],
            requested_by=getattr(args, "requested_by", None) or "operator",
        )
    except StaleModelOverrideWrite as exc:
        updated = exc.instance
        status = "superseded"
    except ValueError as exc:
        data = {"ok": False, "error_code": "invalid_value", "error": safe_assignment_text(str(exc), limit=320), "persona_instance_id": persona_instance_id}
        print(emit_json(data) if args.json else data["error"])
        return 2
    try:
        persona = _persona_by_id(cfg, updated.persona_id)
    except Exception:
        persona = None
    default_provider = getattr(persona, "provider", None) or getattr(cfg, "default_provider", None)
    default_model = getattr(persona, "model", None) or getattr(cfg, "default_model", None)
    data = {
        "ok": True,
        "status": status,
        "applied": status == "applied",
        "scope": "agent_instance",
        "cleared": bool(request["use_default"]) and status == "applied",
        "persona_instance_id": updated.id,
        "persona_id": updated.persona_id,
        "backing_profile": updated.profile_id,
        "provider": updated.provider,
        "model": updated.model,
        "api_mode": updated.api_mode,
        "reasoning_effort": updated.reasoning_effort,
        "default_provider": default_provider,
        "default_model": default_model,
        "effective_provider": updated.provider or default_provider,
        "effective_model": updated.model or default_model,
        "model_is_instance_override": bool(updated.provider or updated.model or updated.reasoning_effort),
        "model_catalog_checked": False,
        "persistence": "persona_instance_store",
        "warnings": request["warnings"],
        "next_expected": (
            "a newer model write already applied for this agent; refresh the Harness snapshot for current truth"
            if status == "superseded"
            else "refresh Harness snapshot; this agent's future chat turns and mission runs use the instance override unless a chat-session override is active"
        ),
    }
    print(emit_json(data) if args.json else f"{status}: {updated.id} model={data['effective_model']} provider={data['effective_provider']}")
    return 0


def _template_write_store_target(store, persona_id: str, *, what: str):
    """Resolve the STORE row a template-tier (profile-default) write lands on.

    Shared by ``persona set-model`` and ``persona set-skills`` because both
    write the same tier and must answer the same three questions identically:

    * ``profile:<name>`` resolves to the SINGLE store row bound to that profile
      (several rows ⇒ ``ambiguous_profile_persona``, none ⇒ the refusal below);
    * a config-only catalog id is REFUSED ``persona_not_persisted`` rather than
      promoted into a store row. Minting a row to persist one field would
      freeze EVERY other field of that persona at its write-time value, because
      ``config.ensure_persisted_personas`` merges ``{**catalog, **stored}`` and
      a store row wins wholesale. The refusal names what would have happened;
      silent promotion could not.

    Returns ``(target, None)`` or ``(None, refusal_payload)`` — never both, and
    never raises for a resolution outcome.
    """

    if persona_id.startswith("profile:"):
        profile_name = persona_id.split(":", 1)[1]
        candidates = [item for item in store.list_all() if str(getattr(item, "hermes_profile", "") or "") == profile_name]
        if not candidates:
            return None, {
                "ok": False,
                "error_code": "persona_not_persisted",
                "error": f"{what} can only be set on store-persisted agents; {persona_id} has no backing agent record",
                "persona_id": persona_id,
            }
        if len(candidates) > 1:
            return None, {
                "ok": False,
                "error_code": "ambiguous_profile_persona",
                "error": f"{persona_id} is backed by multiple store personas; target one explicitly",
                "persona_id": persona_id,
                "candidates": sorted(str(item.id) for item in candidates),
            }
        return candidates[0], None
    try:
        return store.get(persona_id), None
    except Exception:
        return None, {
            "ok": False,
            "error_code": "persona_not_persisted",
            "error": f"{what} can only be set on store-persisted agents; {persona_id} is not in the agent store",
            "persona_id": persona_id,
        }


def _cmd_persona_set_model(args) -> int:
    cfg = load_agent_runtime_config()
    raw_id = str(getattr(args, "persona_id", "") or "").strip()
    try:
        persona = _persona_by_id(cfg, raw_id)
    except ValueError:
        persona = None
    if persona is None:
        data = {"ok": False, "error_code": "persona_not_found", "error": f"unknown persona: {safe_assignment_token(raw_id)}"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    try:
        request = _validated_set_model_request(args)
    except _SetModelRequestError as exc:
        data = _set_model_error_payload(exc, persona_id=str(getattr(persona, "id", "") or raw_id), scope="agent_default")
        print(emit_json(data) if args.json else data["error"])
        return 2
    if request["reasoning_effort"] is not None:
        # Reasoning effort is a per-agent-instance override for now (AgentPersona
        # carries no reasoning field). Reject at the profile-default scope rather
        # than silently dropping it.
        data = _set_model_error_payload(
            _SetModelRequestError(
                "unsupported_scope",
                "reasoning effort can only be set per agent instance (persona.instance.set_model), not on the profile default",
            ),
            persona_id=str(getattr(persona, "id", "") or raw_id),
            scope="agent_default",
        )
        print(emit_json(data) if args.json else data["error"])
        return 2
    store = AgentStore()
    persona_id = str(getattr(persona, "id", "") or "")
    target, refusal = _template_write_store_target(store, persona_id, what="agent defaults")
    if target is None:
        print(emit_json(refusal) if args.json else refusal["error"])
        return 2
    status = "applied"
    issued_at = request["issued_at"]
    if issued_at is not None and getattr(target, "model_override_issued_at", None) is not None:
        issued = issued_at if issued_at.tzinfo is not None else issued_at.replace(tzinfo=timezone.utc)
        applied_at = target.model_override_issued_at
        applied_at = applied_at if applied_at.tzinfo is not None else applied_at.replace(tzinfo=timezone.utc)
        if issued <= applied_at:
            status = "superseded"
    changed = False
    if status == "applied":
        if request["use_default"]:
            if target.model is not None or target.provider is not None or target.api_mode is not None:
                target.model = None
                target.provider = None
                target.api_mode = None
                changed = True
        else:
            for field_name in ("provider", "model", "api_mode"):
                value = request[field_name]
                if value is not None and getattr(target, field_name) != value:
                    setattr(target, field_name, value)
                    changed = True
        if changed:
            target.model_override_issued_at = issued_at or datetime.now(timezone.utc)
            store.save(target)
    default_provider = getattr(cfg, "default_provider", None)
    default_model = getattr(cfg, "default_model", None)
    data = {
        "ok": True,
        "status": status,
        "applied": status == "applied",
        "changed": changed,
        "scope": "agent_default",
        "cleared": bool(request["use_default"]) and status == "applied",
        "persona_id": persona_id,
        "applied_to_persona_id": str(target.id),
        "provider": target.provider,
        "model": target.model,
        "api_mode": target.api_mode,
        "default_provider": default_provider,
        "default_model": default_model,
        "effective_provider": target.provider or default_provider,
        "effective_model": target.model or default_model,
        "model_catalog_checked": False,
        "persistence": "agent_store",
        "warnings": request["warnings"],
        "next_expected": (
            "a newer model write already applied for this persona; refresh the Harness snapshot for current truth"
            if status == "superseded"
            else "refresh Harness snapshot; instances without their own override inherit this default live"
        ),
    }
    print(emit_json(data) if args.json else f"{status}: {target.id} model={data['effective_model']} provider={data['effective_provider']}")
    return 0


def _set_skills_error_payload(error_code: str, message: str, **identity) -> dict:
    return {
        "ok": False,
        "error_code": error_code,
        "error": safe_assignment_text(message, limit=320),
        "scope": "persona_template",
        **identity,
        "next_expected": "fix the arguments and retry; no persona skills were changed",
    }


def _validated_set_skills_request(args) -> dict:
    """Turn the argv into the set the template will hold — or refuse.

    **Absent is NEVER a write here.** ``--skill`` is ``default=None`` so the
    handler can tell "the operator listed skills" from "the operator listed
    none", and at THIS tier the second one has no meaning: the template is the
    root of the cascade, so there is nothing for an omitted flag to inherit
    from. Writing ``[]`` for it would turn any transport that dropped the
    repeated flag — a stale launcher build, a mangled argv, a capability whose
    ``allowedArgs`` lost a row — into a silent clear-every-skill. That exact
    collapse already shipped once at the instance tier (``list(args.skills or
    [])``; see THE BUG THIS REPLACES in ``_cmd_persona_instance_update_profile``)
    and cost every skill of every renamed agent. So absent is a typed
    ``nothing_to_write`` refusal, and emptying the set has its own flag.
    """

    raw_skills = list_flag_or_absent(args, "skills")
    clear = bool(getattr(args, "clear_skills", False))
    if raw_skills is not None and clear:
        raise _SetModelRequestError(
            "conflicting_args",
            "--clear-skills cannot be combined with --skill; pass one or the other",
        )
    if raw_skills is None and not clear:
        raise _SetModelRequestError(
            "nothing_to_write",
            "pass --skill (repeatable) to replace the persona's default skill set, or --clear-skills to empty it",
        )
    skills = [] if clear else _safe_skill_overrides([str(item) for item in raw_skills])
    if not clear and not skills:
        # Every id the operator supplied was dropped by token safety (or they
        # were all blank). Writing the survivors here would be an empty set —
        # i.e. the clear the previous branch just refused to infer — so it gets
        # the same answer rather than a different route to the same damage.
        raise _SetModelRequestError(
            "invalid_value",
            "every --skill value was rejected by token safety; pass --clear-skills to deliberately empty the set",
        )
    return {
        "skills": skills,
        "clear": clear,
        "issued_at": _parse_issued_at_arg(getattr(args, "issued_at", None)),
    }


def _cmd_persona_set_skills(args) -> int:
    """Persist a persona TEMPLATE's default skill set (profile-default tier).

    The skills half of ``persona set-model``, and deliberately its twin: same
    store-row write target (``_template_write_store_target``), same
    ``persona_not_persisted`` refusal for a config-only catalog id, same
    ``profile:<name>`` resolution, same supersede clock — on its OWN field
    (``skills_override_issued_at``), so a skills write and a model write can
    never supersede each other.

    Why the tier needs a verb at all: ``persona instance update-profile
    --skill`` writes ``skill_overrides`` on ONE agent, and a placement made
    later inherits ``persona.skills`` — not that agent's overrides. Before this
    verb no operator door wrote ``persona.skills``, so "set the skills, then
    place a new agent from that persona" could not work by construction.

    Inheritance is LIVE, not a copy: ``models.apply_instance_model_overrides``
    falls back to ``list(persona.skills)`` at EVERY resolution for an instance
    whose ``skill_overrides`` is ``None``. So this write also moves existing
    non-overridden instances, and the ack says that out loud instead of
    pretending only the future is affected — the first idle agent would
    disprove the pretence anyway.
    """

    cfg = load_agent_runtime_config()
    raw_id = str(getattr(args, "persona_id", "") or "").strip()
    try:
        persona = _persona_by_id(cfg, raw_id)
    except ValueError:
        persona = None
    if persona is None:
        # Root-observability on the REFUSAL, for the reason the create and retire
        # verbs carry it: a `persona_not_found` answered out of the WRONG runtime
        # root refuses exactly as plausibly as one out of the right one — and this
        # verb writes the template every later placement inherits.
        data = attach_root_observability({"ok": False, "error_code": "persona_not_found", "error": f"unknown persona: {safe_assignment_token(raw_id)}"})
        print(emit_json(data) if args.json else data["error"])
        return 2
    persona_id = str(getattr(persona, "id", "") or raw_id)
    try:
        request = _validated_set_skills_request(args)
    except _SetModelRequestError as exc:
        data = attach_root_observability(_set_skills_error_payload(exc.error_code, str(exc), persona_id=persona_id))
        print(emit_json(data) if args.json else data["error"])
        return 2
    store = AgentStore()
    target, refusal = _template_write_store_target(store, persona_id, what="persona default skills")
    if target is None:
        refusal = attach_root_observability(refusal)
        print(emit_json(refusal) if args.json else refusal["error"])
        return 2
    status = "applied"
    issued_at = request["issued_at"]
    if issued_at is not None and getattr(target, "skills_override_issued_at", None) is not None:
        issued = issued_at if issued_at.tzinfo is not None else issued_at.replace(tzinfo=timezone.utc)
        applied_at = target.skills_override_issued_at
        applied_at = applied_at if applied_at.tzinfo is not None else applied_at.replace(tzinfo=timezone.utc)
        if issued <= applied_at:
            status = "superseded"
    changed = False
    if status == "applied":
        requested = list(request["skills"])
        if list(target.skills or []) != requested:
            target.skills = requested
            changed = True
        if changed:
            target.skills_override_issued_at = issued_at or datetime.now(timezone.utc)
            store.save(target)
    stored_skills = list(target.skills or [])
    data = attach_root_observability({
        "ok": True,
        "status": status,
        "applied": status == "applied",
        "changed": changed,
        "scope": "persona_template",
        "cleared": bool(request["clear"]) and status == "applied",
        "persona_id": persona_id,
        "applied_to_persona_id": str(target.id),
        "skills": stored_skills,
        "unresolved": _unresolvable_skill_ids(stored_skills),
        "persistence": "agent_store",
        "next_expected": (
            "a newer skills write already applied for this persona; refresh the Harness snapshot for current truth"
            if status == "superseded"
            else "refresh Harness snapshot; instances whose skill_overrides is null follow this set live on their next resolution, and instances carrying their own overrides keep them"
        ),
    })
    print(emit_json(data) if args.json else f"{status}: {target.id} skills={','.join(stored_skills) or '(none)'}")
    return 0


def _unresolvable_skill_ids(skills: list[str]) -> list[str]:
    """Which of these ids no skills root on THIS machine can resolve.

    A WARNING, never a refusal (plan R3). The instance tier does not refuse
    them either; placement-time strictness already lives in the create verb's
    skills phase, and the readiness projection carries the standing truth for a
    template that names a skill this host lacks. Hard-gating here would make a
    realm-synced persona uneditable on any machine missing one of its skills.

    A resolver FAULT answers "nothing unresolved" rather than failing the verb:
    the store write has already landed by the time this runs, so a resolver
    problem must not turn a successful write into a non-zero exit.
    """

    if not skills:
        return []
    try:
        from agent.skill_utils import resolve_skills

        resolutions = resolve_skills(list(skills))
    except Exception:  # noqa: BLE001 - advisory warning list, never a gate
        return []
    return [
        name
        for name in skills
        if str(getattr(resolutions.get(name), "status", "missing")) != "resolved"
    ]


def _emit_chat_final(payload: dict[str, object]) -> None:
    data = dict(payload)
    data["type"] = "chat.final"
    sys.stdout.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _emit_chat_frame(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


# Streamed delta chunks arrive far faster than the turn store can absorb full
# locked rewrites (O(file size) per persist). Delta-driven on_update flushes
# are therefore debounced to at most one per interval; segment boundaries,
# tool events, and the handler-owned write-ahead/terminal persists stay
# immediate and unthrottled.
_CHAT_TURN_INCREMENTAL_FLUSH_INTERVAL_SECONDS = 0.25


class _ChatProtocolV2Emitter:
    """Mission Control chat stream protocol v2 — the ONLY delta wire shape.

    C8 retired the legacy ``chat.delta`` lane (ruling 0: one shape per token);
    callers still emit the terminal ``chat.final`` envelope themselves. These
    v2 frames carry the one ordering authority: ``turn_id`` (the turn anchor,
    minted from ``client_message_id``) plus a per-turn element ``seq``.
    """

    def __init__(
        self,
        *,
        turn_id: str | None,
        client_message_id: str | None,
        on_update=None,
        emit_frames: bool = True,
        clock=time.monotonic,
        turn_phases=None,
    ):
        import contextvars as _contextvars
        import threading as _threading

        safe_turn = safe_assignment_token(turn_id) or f"turn_{uuid.uuid4().hex[:12]}"
        self.turn_id = safe_turn
        self.client_message_id = safe_assignment_text(client_message_id, limit=200) or None
        self._on_update = on_update
        self._emit_frames = bool(emit_frames)
        # The turn's phase timeline, or None for callers that do not keep one
        # (tests, and any future emitter user outside the chat handler). The
        # emitter takes exactly ONE mark from it — `provider_first_byte` — and
        # never reads a mark back.
        self._turn_phases = turn_phases
        # Guard for that mark, and the reason the cost of this whole plan is
        # per-turn rather than per-token: `delta()` runs once per delta, and
        # this boolean is all it costs after the first one. `TurnPhaseMarks`
        # enforces first-mark-wins on its own side too — belt and braces,
        # because a guard at the call site is not a property anything can
        # assert, and the mark rides a worker thread.
        self._provider_first_byte_marked = False
        # Streaming deltas fire from the provider's worker thread. Under
        # `harness serve` the stdout proxy tags each line with a request-id
        # ContextVar that new threads do NOT inherit, so frames written from
        # the worker thread went out with id=null and the Launcher's
        # per-request frame router dropped them — the console showed nothing
        # until turn end. Capture the request context here (handler thread,
        # request id set) and emit every frame inside it; the lock keeps the
        # single Context from being entered concurrently (RuntimeError).
        self._turn_context = _contextvars.copy_context()
        self._emit_lock = _threading.Lock()
        # Debounce bookkeeping only — element timing fields keep using
        # time.monotonic directly so an injected test clock cannot skew them.
        self._clock = clock
        self._last_on_update_flush: float | None = None
        self._finishing = False
        self._finished = False
        self._seq = 0
        self._started_at = time.monotonic()
        self._current_segment: dict[str, object] | None = None
        self._segment_count = 0
        self._tool_count = 0
        self._active_tools: dict[str, list[dict[str, object]]] = {}
        self.elements: list[dict[str, object]] = []
        self._emit_chat_frame(
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
        if not self._provider_first_byte_marked:
            # FIRST provider byte this process has seen. Marked here rather
            # than inside the provider client because this is the earliest site
            # the turn owns, and the plan explicitly declines provider-client
            # surgery for it. An empty delta is not a byte — the guard above
            # already returned.
            self._provider_first_byte_marked = True
            if self._turn_phases is not None:
                self._turn_phases.mark("provider_first_byte")
        segment = self._ensure_segment()
        text = str(delta)
        segment["text"] = str(segment.get("text") or "") + text
        self._emit_chat_frame(
            {
                "type": "segment.delta",
                "protocol_version": 2,
                "turn_id": self.turn_id,
                "seq": segment["seq"],
                "id": segment["id"],
                "text": text,
            }
        )
        self._notify_update(immediate=False)

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
        self._emit_chat_frame(
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
        # Idempotent: a crash-path caller may reach finish() after the success
        # path already finished — a second turn.end frame would corrupt the
        # stream protocol. on_update is suppressed for the whole finish window:
        # the caller's terminal persist is the single settling write, and an
        # extra incremental "running" persist here would race it.
        if self._finished:
            return
        self._finished = True
        self._finishing = True
        try:
            self.end_segment(state="settled" if state == "completed" else state)
            self._emit_chat_frame(
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
        finally:
            self._finishing = False

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
        self._emit_chat_frame(
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
        # No on_update here: the only caller is delta(), which notifies right
        # after appending its text — flushing before the append would burn the
        # debounce window on an empty segment.
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
        # Generic input record (already scrubbed/bounded at the progress sink).
        # Block-preserving: key-per-line structure is the rendering contract.
        tool_input = _safe_stream_block(payload.get("tool_input"), limit=1200)
        if tool_input:
            tool["tool_input"] = tool_input
        self.elements.append(tool)
        self._active_tools.setdefault(name, []).append(tool)
        self._emit_chat_frame(
            {
                "type": "tool.started",
                "protocol_version": 2,
                "turn_id": self.turn_id,
                "seq": tool["seq"],
                "id": tool["id"],
                "name": name,
                "args": _safe_stream_text(payload.get("summary")),
                "command": command,
                "tool_input": tool.get("tool_input"),
            }
        )

    def _match_started_tool(self, name: str, payload: dict[str, object]) -> dict[str, object] | None:
        """Which STARTED element this finished event belongs to.

        Measured defect (turn-efficiency plan bucket f, Stage 6): two tools of
        the same name started concurrently and this matched by NAME alone, then
        `pop()`ed — LIFO. So finish-A landed on the element started SECOND and
        finish-B on the element started first, and because a finished payload's
        `tool_input` overrides the started one, the elements came out crossed:
        `[0]`'s summary naming one skill while its `tool_input` named the other,
        and `[1]` the reverse. Two `skill_view` calls, two `read_file` calls and
        two more `read_file` calls all landed that way in one live turn record.

        The plan said "key by tool-call id". **There is no tool-call id here**:
        the runner's progress contract is `(event, tool_name, invocation,
        result)` (`agent_runtime/profile_runner.py`
        `_progress_payload_from_callback`) and carries no identifier at all. The
        identity that DOES reach both events is the invocation itself — rendered
        to the `tool_input` block for most tools, and to `command_full` /
        `command_label` for the terminal class, whose `tool_input` is
        deliberately suppressed against the event cap. So the match is on those,
        in that order.

        The fallback when neither side carries an identity is FIFO, not the old
        LIFO: concurrent starts of one tool finish in start order often enough
        that arrival order is the better guess, and it is the guess that was
        measurably wrong before.
        """
        pending = self._active_tools.get(name) or []
        if not pending:
            return None
        finished_command = _safe_stream_text(payload.get("command_full")) or _safe_stream_text(
            payload.get("command_label")
        )
        identities = (
            ("tool_input", _safe_stream_block(payload.get("tool_input"), limit=1200)),
            ("command", finished_command),
        )
        for field, value in identities:
            if not value:
                continue
            for index, candidate in enumerate(pending):
                if candidate.get(field) == value:
                    return pending.pop(index)
        return pending.pop(0)

    def _tool_finished(self, payload: dict[str, object]) -> None:
        name = _tool_name_from_progress(payload)
        tool = self._match_started_tool(name, payload)
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
        # T7: the todo tool's structured checklist rides the finished event so the
        # operator console can render it (the store is otherwise in-memory only).
        # Producer-bounded already (profile_runner `_todo_state_payload`); the
        # turn-store re-bounds it in `_safe_elements`.
        # T9d: carry an explicit EMPTY list too (`isinstance(...)`, not
        # `and todo_state`) — a cleared checklist emits `todo_state: []`, which the
        # launcher resolver distinguishes from absence to clear the panel. A
        # non-todo tool never carries the key (the producer returns None for it).
        todo_state = payload.get("todo_state")
        if isinstance(todo_state, list):
            tool["todo_state"] = todo_state
        # Generic input/result record (scrubbed/bounded at the progress sink;
        # block-preserving). Input carries through from the started element when
        # the finished payload omits it, mirroring the command carry-through.
        tool_input = _safe_stream_block(payload.get("tool_input"), limit=1200) or tool.get("tool_input")
        if tool_input:
            tool["tool_input"] = tool_input
        # Patch observability: the local diff artifact's path (same
        # `_safe_stream_text` grade `command` rides — paths survive it) plus the
        # +/− counts and the grammar. Scrubbed and bounded at the progress sink;
        # this is the live-turn carrier of the same four fields the snapshot
        # lane carries, so a streaming tile and a reloaded one agree.
        patch_artifact = _safe_stream_text(payload.get("patch_artifact"), limit=500)
        if patch_artifact:
            tool["patch_artifact"] = patch_artifact
        for count_key in ("patch_adds", "patch_dels"):
            count = payload.get(count_key)
            if isinstance(count, int) and not isinstance(count, bool):
                tool[count_key] = count
        patch_mode = _safe_stream_text(payload.get("patch_mode"), limit=20)
        if patch_mode:
            tool["patch_mode"] = patch_mode
        self._emit_chat_frame(
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
                "tool_input": tool.get("tool_input"),
                "tool_result": tool.get("tool_result"),
                **({"todo_state": tool["todo_state"]} if "todo_state" in tool else {}),
                # Absent-when-absent, like todo_state: a non-patch tool never
                # carries these keys at all, so the launcher's "no affordance"
                # branch is reached by absence rather than by a null sentinel.
                **{
                    key: tool[key]
                    for key in ("patch_artifact", "patch_adds", "patch_dels", "patch_mode")
                    if key in tool
                },
            }
        )

    def _notify_update(self, *, immediate: bool = True) -> None:
        # Debounced (immediate=False) callers are the streamed-delta path:
        # they flush at most once per interval. Segment boundaries and tool
        # events flush immediately — they are rare and carry the trace
        # visibility operators debug from. A delta suppressed here is never
        # lost: `elements` accumulates in place, so the next flush of any
        # flavor (interval, boundary, tool, terminal) carries the full text.
        if self._on_update is None or self._finishing:
            return
        now = self._clock()
        if not immediate:
            last = self._last_on_update_flush
            if last is not None and (now - last) < _CHAT_TURN_INCREMENTAL_FLUSH_INTERVAL_SECONDS:
                return
        self._last_on_update_flush = now
        try:
            self._on_update(self)
        except Exception:
            pass

    def ack(self, trace_payload: dict | None) -> None:
        """Presentation-only pre-trace acknowledgment frame (C8).

        The moment the model commits to a tool path, the console gets a typed
        ``turn.ack`` v2 frame carrying the canned "about to work" copy. It is
        NEVER durable: not an element, not a turn-store flush, not a SessionDB
        row — live content supersedes it and replay never shows it. When
        frames are suppressed (non-stream turns) this is a no-op.
        """
        if not isinstance(trace_payload, dict):
            return
        from agent_runtime.transcript_order import pre_trace_ack_text

        self._emit_chat_frame(
            {
                "type": "turn.ack",
                "protocol_version": 2,
                "turn_id": self.turn_id,
                "text": pre_trace_ack_text(trace_payload),
            }
        )

    def _emit_chat_frame(self, payload: dict[str, object]) -> None:
        if not self._emit_frames:
            return
        with self._emit_lock:
            self._turn_context.run(_emit_chat_frame, payload)


def _safe_stream_text(value: object, *, limit: int = 800) -> str | None:
    return safe_assignment_text(value, limit=limit) or None


def _safe_stream_block(value: object, *, limit: int) -> str | None:
    """Newline-PRESERVING stream text for the tool input/result record.

    ``safe_assignment_text`` whitespace-collapses, which would fold the
    key-per-line block (the rendering contract for the console's Input/Result
    dropdowns) into one unreadable line. The value was already secret-scrubbed
    and bounded at the progress sink; this only re-bounds and strips NULs."""

    text = str(value or "").replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None
    if len(text) > limit:
        text = f"{text[:limit]}\n…(rest truncated)…"
    return text


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


# ``PersonaChatPersistenceError``, ``_persona_chat_persistence_failed``,
# ``_default_persona_session_db`` and ``_ensure_persona_chat_session`` used to be
# defined HERE. They moved to ``agent_runtime.persona_chat_durability`` (imported
# at the top of this part under the same private names) because chat-root
# durability was a CLI-lane-only concern for as long as it lived in this file:
# every call site was an argv handler, so ``agent_runtime``'s one-call create
# lane — the one the launcher's drag-drop reaches over RPC — structurally could
# not reach it and minted phantom roots. See that module's docstring.


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


def _missing_chat_message_payload() -> dict[str, object]:
    """Refusal for a send that carries no message text.

    One spelling for two call sites: the pre-mint gate and the post-lease check
    that has always owned it."""

    return {"ok": False, "error": "message is required"}


def _invalid_chat_model_override_payload(
    exc: Exception,
    *,
    persona_id: str,
    persona_instance_id: str | None,
    session_id: str | None,
) -> dict[str, object]:
    """Refusal for a provider/model override the caller stated wrongly.

    Built here rather than inline because the same refusal is now reachable
    from TWO points of one turn — the pre-mint gate (no session exists yet, so
    the session fields are honestly null) and the post-``open_chat`` resolve —
    and one envelope with two spellings is how ``error_kind`` drifts."""
    # Function-local: this file is exec'd into harness.py's globals, so a
    # module-level import here would need a matching harness.py import or it
    # is a NameError on a LIVE turn. The turn-outcome vocabulary is owned by
    # agent_runtime.mission_chat_outcome; nothing re-spells its values.
    from agent_runtime.mission_chat_outcome import ChatErrorKind

    return {
        "ok": False,
        "error_kind": ChatErrorKind.INVALID_CHAT_MODEL_OVERRIDE,
        "error": safe_assignment_text(str(exc), limit=320),
        "persona_instance_id": persona_instance_id,
        "persona_id": persona_id,
        "session_id": session_id,
        "chat_session_id": session_id,
        "next_expected": "choose a valid provider/model id or clear the chat-scoped override; Hermes profile defaults were not changed",
    }


def _mission_chat_caller_refusal(
    args,
    *,
    persona_id: str,
    persona_instance_id: str | None,
) -> dict[str, object] | None:
    """The refusals decidable from the caller's arguments alone, or ``None``.

    Exists so they can run BEFORE a dispatch mints its task-scoped thread: a
    mint is durable (a titled row in Mission Control) and a repoint (the
    instance's default-thread pointer), so a send that was always going to be
    refused must not leave either behind. Both checks are pure functions of
    ``args``, so evaluating them here AND at their original sites is free and
    keeps those sites intact for the explicit-session lane."""

    if not safe_assignment_text(getattr(args, "message", None), limit=12000):
        return _missing_chat_message_payload()
    try:
        _requested_chat_model_override(args)
    except ValueError as exc:
        return _invalid_chat_model_override_payload(
            exc,
            persona_id=persona_id,
            persona_instance_id=persona_instance_id,
            session_id=None,
        )
    return None


def _clarify_ticket_store_populated() -> bool:
    """Cheap probe: has this runtime ever minted a clarify ticket?

    The tokenless settlement below (a turn landing in a session that has an open
    ticket) has to run on turns that present NO token, which is nearly all of
    them. Gating it on ``mission_chat_clarify_token_binding()`` would put an
    UNCACHED root-``config.yaml`` parse on every mission-chat turn — the exact
    cost ``resolve_dispatch_session_decision``'s lazy-config note exists to
    avoid. One ``exists()`` answers it instead: with the gate off nothing ever
    mints, so the directory never appears and no ticket work (and no config
    read) happens at all."""

    try:
        return (paths.store_root() / "persona_chat_clarify_tickets").exists()
    except OSError:  # pragma: no cover - defensive; a store probe must not fail a turn
        return False


def _resolve_mission_chat_clarify_binding(
    args, *, session_id: str | None
) -> dict[str, object] | None:
    """Resolve an echoed ``clarify_token`` into the thread it was asked in.

    Returns the turn's ``clarify_binding`` report block, or ``None`` when the
    caller presented no token (the overwhelmingly common case, and the one that
    must cost nothing — no store read, no config parse).

    **The token beats a conflicting ``session_id``, loudly.** Refusing would
    defeat the purpose: this binding exists precisely because agents are
    unreliable about session arguments, so a reply that echoes the token AND
    attaches a stale session id must still land correctly. Silence would be the
    other half of the same failure, so the override is reported in
    ``overrode_session_id``.

    **An unknown or pruned token degrades, it does not refuse.** Tickets are
    swept on a TTL; turning a GC'd ticket into a hard failure would punish a
    parent that did exactly the right thing. The turn falls through to normal
    precedence and says so (``state: "unknown_token"``)."""

    token = safe_assignment_text(getattr(args, "clarify_token", None), limit=240)
    if not token:
        return None
    if not mission_chat_clarify_token_binding():
        return None
    ticket = PersonaChatClarifyTicketStore().resolve(token)
    bound_session_id = safe_assignment_text(
        (ticket or {}).get("chat_session_id"), limit=240
    )
    if not bound_session_id:
        return {
            "token": token,
            "state": "unknown_token",
            "bound_via": "none",
            "bound_session_id": None,
            "overrode_session_id": None,
        }
    stated = safe_assignment_text(session_id, limit=200)
    return {
        "token": token,
        "state": "bound",
        "bound_via": "clarify_token",
        "bound_session_id": bound_session_id,
        "overrode_session_id": stated if stated and stated != bound_session_id else None,
    }


def _settle_mission_chat_clarify_binding(
    binding: dict[str, object] | None,
    *,
    session_id: str | None,
    client_message_id: str | None,
    explicit_session_id: str | None,
) -> dict[str, object] | None:
    """Close out this turn's clarify accounting and return the report block.

    Two settlements, one chokepoint. A turn that BOUND through a token settles
    that ticket (``bound``, or ``rebound`` when a different message answers the
    same question again). A turn that presented NO token but landed in a session
    that HAS an open ticket settles it anyway — ``bound_via: "session_id"`` when
    the caller named the thread, ``"none"`` when they merely inherited it. That
    tokenless half is not bookkeeping pedantry: without it every
    prompt-compliant parent leaves a permanently-open ticket and the adoption
    metric lies in the pessimistic direction, which is the direction that would
    argue for building more machinery than this needs.

    Called only after the turn's reply is durable — a refused turn answered
    nothing and must not mark a question answered."""

    store = PersonaChatClarifyTicketStore()
    if binding is not None:
        if binding.get("bound_via") != "clarify_token":
            return binding
        record = store.settle(
            binding.get("token"),
            client_message_id=client_message_id,
            bound_via="clarify_token",
        )
        if record is not None and record.get("state") == "rebound":
            binding = {**binding, "state": "rebound"}
        return binding
    if not _clarify_ticket_store_populated():
        return None
    ticket = store.open_ticket_for_session(session_id)
    if ticket is None:
        return None
    bound_via = "session_id" if safe_assignment_text(explicit_session_id, limit=200) else "none"
    store.settle(
        ticket.get("clarify_token"),
        client_message_id=client_message_id,
        bound_via=bound_via,
    )
    return {
        "token": safe_assignment_text(ticket.get("clarify_token"), limit=240) or None,
        "state": "answered",
        "bound_via": bound_via,
        "bound_session_id": safe_assignment_text(ticket.get("chat_session_id"), limit=240)
        or None,
        "overrode_session_id": None,
    }


def _mission_chat_clarify_request_payload(
    chat_result,
    *,
    session_id: str | None,
    persona_id: str,
    persona_instance_id: str | None,
    client_message_id: str | None,
    turn_id: str | None,
    requested_by_session: str | None,
) -> dict[str, object] | None:
    """The turn's ``clarify_request``, with a freshly minted binding token.

    The token is minted HERE — where the question is materialized into the turn
    payload — and never inside :class:`MissionChatClarifyCapture`, which is
    deliberately a pure dataclass with no store and no session id. No clarify,
    no ticket: the normal path pays nothing.

    A mint failure is not a turn failure. The question still ships (without a
    token), the answering parent falls through to today's precedence, and the
    only thing lost is the structural binding — which is exactly the state the
    lane was in before this existed."""

    raw = (getattr(chat_result, "raw", None) or {}).get("clarify_request")
    if not isinstance(raw, dict) or not raw:
        # Passed through verbatim, exactly as before this seam existed: no
        # question asked, nothing to bind.
        return raw
    if not mission_chat_clarify_token_binding():
        return dict(raw)
    token = PersonaChatClarifyTicketStore().mint(
        chat_session_id=session_id,
        persona_instance_id=persona_instance_id,
        persona_id=persona_id,
        asked_by_client_message_id=client_message_id,
        asked_turn_id=turn_id,
        requested_by_session=requested_by_session,
    )
    payload = dict(raw)
    if token:
        payload["clarify_token"] = token
    return payload


def _mission_chat_retired_target_refusal(
    instance_store: PersonaInstanceStore,
    *,
    persona_id: str,
    persona_instance_id: str | None,
) -> dict[str, object] | None:
    """Refusal when the dispatch target's placement was RETIRED, or ``None``.

    The sibling of :func:`_mission_chat_caller_refusal` for the one refusal that
    is not a function of ``args``: it needs the store. Same reason for being
    here rather than only at its original site — ``open_chat`` surfaces this
    refusal by raising, but the mint below binds through ``open_chat`` only
    AFTER creating and titling the session row, so a dispatch to a target that
    can never be served used to leave a permanent empty thread behind (and
    escape as an untyped traceback, because this call site's typed handler
    wraps the LATER bind, not the mint).

    Read-only: the store predicate never writes, and a store that cannot answer
    returns ``None`` rather than fabricating a refusal — the mint lane and
    ``open_chat`` both still refuse a retired target, so failing open here costs
    the litter, never the guarantee."""

    try:
        archive_path = instance_store.retired_instance_archive_path(
            persona_instance_id, persona_id=persona_id
        )
    except Exception:
        return None
    if archive_path is None:
        return None
    return _retired_persona_instance_refusal(
        persona_instance_id=canonical_chat_instance_id(persona_id, persona_instance_id),
        archive_path=archive_path,
    )


def _session_row(session_db, session_id: str | None) -> dict[str, object]:
    if session_db is None or not session_id:
        return {}
    try:
        raw = session_db.get_session(session_id)
    except Exception:
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _session_model_config(session_db, session_id: str | None) -> dict[str, object]:
    raw = _session_row(session_db, session_id)
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


def _persona_chat_session_owner(session_db, session_id: str | None) -> str | None:
    """Resolve the immutable owner of a canonical persona-chat transcript.

    SessionDB's typed ``source`` column is the authority that makes a row a
    persona chat.  Older rows predate the duplicated ownership fields in
    ``model_config``; their server-minted id still carries the exact instance
    owner.  Requiring the duplicate field made those rows visible through the
    history projection but impossible to open or message.

    New rows persist both forms.  If both are present they must agree, so a
    corrupt metadata write cannot reassign another instance's transcript.
    """

    raw = _session_row(session_db, session_id)
    if safe_assignment_token(raw.get("source")) != PERSONA_CHAT_SESSION_SOURCE:
        return None
    config = _session_model_config(session_db, session_id)
    config_source = safe_assignment_token(config.get("source"))
    if config_source and config_source != PERSONA_CHAT_SESSION_SOURCE:
        return None

    metadata_owner = safe_assignment_token(config.get("persona_instance_id")) or None
    structural_owner = chat_session_owner_instance_id(session_id)
    if metadata_owner and structural_owner and metadata_owner != structural_owner:
        return None
    owner = metadata_owner or structural_owner
    if not owner:
        return None

    # Retired singleton/operator ids are recorded as durable aliases during
    # persona-instance reconciliation.  Resolve those aliases here so history,
    # open and send all name the same current instance authority.
    try:
        from agent_runtime.persona_instance_identity import load_persona_instance_aliases

        aliases = load_persona_instance_aliases()
    except Exception:
        aliases = {}
    seen: set[str] = set()
    while owner in aliases and owner not in seen:
        seen.add(owner)
        owner = aliases[owner]
    return canonical_persona_instance_id(owner) or owner


def _persona_chat_bound_owner(session_id: str | None) -> str | None:
    """Resolve a uniquely bound root when its canonical transcript is gone.

    This is deliberately narrower than trusting a caller-supplied instance id:
    the persisted instance binding itself must name the exact root, and an
    ambiguous binding is treated as unowned.  It preserves safe cleanup of a
    stale binding without permitting a foreign-root delete.
    """

    exact_session = safe_assignment_text(session_id, limit=240)
    if not exact_session:
        return None
    matches = []
    try:
        instances = PersonaInstanceStore().list_all()
    except Exception:
        return None
    for instance in instances:
        bound = safe_assignment_text(
            getattr(instance, "default_chat_session_id", None), limit=240
        ) or safe_assignment_text(getattr(instance, "session_id", None), limit=240)
        if bound == exact_session:
            matches.append(safe_assignment_token(getattr(instance, "id", None)))
    unique = {item for item in matches if item}
    return next(iter(unique)) if len(unique) == 1 else None


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
    instance=None,
) -> dict[str, object]:
    """Effective-model cascade for one chat turn.

    Tiers (highest wins): chat-session override > instance override >
    persona default > agent_runtime config default. ``default_*`` stays the
    persona/config tier so the instance tier is reported honestly instead of
    being folded in silently; ``agent_*`` is what future turns/runs of THIS
    agent use when no chat override is active.
    """
    default_provider = getattr(persona, "provider", None) or getattr(config, "default_provider", None)
    default_model = getattr(persona, "model", None) or getattr(config, "default_model", None)
    instance_provider = getattr(instance, "provider", None) if instance is not None else None
    instance_model = getattr(instance, "model", None) if instance is not None else None
    agent_provider = instance_provider or default_provider
    agent_model = instance_model or default_model
    provider = (override or {}).get("provider") or agent_provider
    model = (override or {}).get("model") or agent_model
    return {
        "default_provider": default_provider,
        "default_model": default_model,
        "instance_provider": instance_provider,
        "instance_model": instance_model,
        "agent_provider": agent_provider,
        "agent_model": agent_model,
        "chat_provider": (override or {}).get("provider"),
        "chat_model": (override or {}).get("model"),
        "effective_provider": provider,
        "effective_model": model,
        "model_is_default": not bool(override and ((override.get("provider") or "") or (override.get("model") or ""))),
        "model_is_instance_override": bool(instance_provider or instance_model),
        "scope": "mission_control_chat_session",
    }


def _persona_chat_native_tip(session_db, root_session_id: str) -> str:
    resolver = getattr(session_db, "resolve_resume_session_id", None)
    return resolver(root_session_id) if callable(resolver) else root_session_id


def _persona_chat_native_history(session_db, active_session_id: str) -> list[dict]:
    loader = getattr(session_db, "get_messages_as_conversation", None)
    if callable(loader):
        return list(loader(active_session_id, include_ancestors=True) or [])
    legacy = getattr(session_db, "get_messages", None)
    return list(legacy(active_session_id) or []) if callable(legacy) else []


def _persona_chat_native_revision(session_db, root_session_id: str) -> str:
    try:
        return native_history_revision(session_db, root_session_id)
    except Exception:
        history = _persona_chat_native_history(
            session_db, _persona_chat_native_tip(session_db, root_session_id)
        )
        return f"{root_session_id}:{hashlib.sha256(emit_json(history).encode('utf-8')).hexdigest()[:16]}"


_PERSONA_CHAT_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|bearer)\s*[:=]\s*\S+"
)


# One pair of caps for the whole mission-chat lane. The reply cap matches the
# operator-channel projection read cap (operator_channels._safe_conversation_text
# limit=20000) so a persisted reply is never shorter than what the projection
# is willing to display.
PERSONA_CHAT_OPERATOR_MESSAGE_LIMIT = 12000
PERSONA_CHAT_REPLY_LIMIT = 20000


def _redact_persona_chat_text(value, *, limit: int) -> str:
    safe = _safe_persona_chat_body_text(value, limit=limit)
    if not safe:
        return ""
    return _PERSONA_CHAT_SECRET_RE.sub(r"\1: [redacted]", safe)


def _safe_persona_chat_body_text(value, *, limit: int) -> str:
    text = str(value or "").replace("\x00", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Preserve intra-line whitespace: chat bodies carry code blocks and aligned
    # output, and collapsing runs of spaces destroys them irreversibly at
    # persistence time. Only trim line endings and cap blank runs.
    lines = [line.rstrip() for line in text.split("\n")]
    normalized = "\n".join(lines).strip()
    normalized = re.sub(r"\n{4,}", "\n\n\n", normalized)
    if len(normalized) > limit:
        # Truncation must be visible, never silent.
        normalized = normalized[:limit].rstrip() + " … [truncated]"
    return normalized


def _chat_turn_tool_names(elements) -> list[str]:
    """Tool names this turn actually ran, in emitter order.

    Feeds the synthesized budget-exhausted summary: when not even the final
    checkpoint call fits, the operator still gets an honest account of what DID
    execute instead of a blank blocked turn.
    """

    names: list[str] = []
    for element in elements or ():
        if not isinstance(element, dict) or element.get("kind") != "tool":
            continue
        name = safe_assignment_token(element.get("name"))
        if name:
            names.append(name)
    return names


def _persona_chat_existing_turn(
    *,
    session_db,
    session_id: str | None,
    client_message_id: str | None,
) -> dict[str, object]:
    if session_db is None or not session_id or not client_message_id:
        return {}
    try:
        active_session_id = _persona_chat_native_tip(session_db, session_id)
        messages = _persona_chat_native_history(session_db, active_session_id)
    except Exception:
        return {}

    result: dict[str, object] = {}
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        message_id = safe_assignment_text(
            item.get("platform_message_id") or item.get("message_id"), limit=200
        )
        if message_id != client_message_id and not message_id.startswith(
            f"{client_message_id}:"
        ):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role == "user" and "operator" not in result:
            result["operator"] = item
        elif role == "assistant":
            result["assistant"] = item
    return result


def _resolve_relay_sender_marker(
    requested_by,
    *,
    instance_store,
    relay_chain_in,
) -> str | None:
    """Resolve an incoming message's ORIGIN into a finish_reason marker.

    This is the one site that decides what the persisted user row is typed
    with, so both non-operator origins resolve here rather than in two places
    that could disagree about the same column:

    * ``requested_by = "harness-delivery:<dispatch>:<flag>"`` — a dispatch
      delivery the harness forged into the sender's own thread. Checked FIRST
      because it is an exact-prefix machine provenance, and because a delivery
      is not a relay: no agent sent it and there is no sending persona to name.
    * ``requested_by = "agent:<caller session id>"`` — an ``agent_chat_send``
      relay (set by tools/agent_chat_tool.py from the CALLER's session).
      Resolution is tiered most-to-least specific; each tier is a deliberate
      source of truth and the bare ``relay_from::`` marker is the honest
      unknown, never a guessed operator.

    Operator / CLI / coordinator sends match neither, and must persist
    byte-identically to today (this returns ``None`` for them → no marker).

    The name is kept for the AST gate that pins ``_cmd_mission_chat_message``
    to this single call site.
    """
    from agent_runtime import dispatch_delivery as _dispatch_delivery
    from agent_runtime import relay_policy as _relay_policy

    delivery = _dispatch_delivery.parse_delivery_requested_by(requested_by)
    if delivery is not None:
        return _relay_policy.build_harness_delivery_marker(
            delivery.dispatch_id, delivery.notify_operator, delivery.state
        )
    if not isinstance(requested_by, str) or not requested_by.startswith("agent:"):
        return None
    token = requested_by[len("agent:"):].strip()
    if not token:
        return None

    from agent_runtime import relay_policy

    persona_id: str | None = None
    instance_id: str | None = None
    instances = instance_store.list_all()
    by_id = {instance.id: instance for instance in instances}

    # Tier 1 — the caller session is a minted chat session; its exact-mint owner
    # is the sending instance (sibling-safe: personainst_<p>_agent_2 survives).
    # Resolve the persona from the store row, never by string-parsing the
    # instance id (placement suffixes make that fragile).
    owner = chat_session_owner_instance_id(token)
    if owner and owner in by_id:
        instance_id = owner
        persona_id = safe_assignment_token(by_id[owner].persona_id) or None
    elif owner and owner.startswith(PERSONA_INSTANCE_ID_PREFIX):
        # A real instance handle whose row is absent from this snapshot (e.g.
        # reaped): keep the instance identity, leave the persona honestly
        # unknown rather than guessing.
        instance_id = owner

    # Tier 2 — task-lane callers: the caller session is the instance's bound
    # session, not a chat-shaped id. S56 removed the ``active_worker_session_id``
    # candidate with the worker store that was its only writer.
    if instance_id is None and persona_id is None:
        for instance in instances:
            candidates = {
                safe_assignment_text(instance.default_chat_session_id, limit=200),
            }
            if token in candidates:
                instance_id = instance.id
                persona_id = safe_assignment_token(instance.persona_id) or None
                break

    # Tier 3 — persona-level fallback: the immediate caller is the LAST entry of
    # the pre-target-append relay chain (canonical persona id; no instance).
    if instance_id is None and persona_id is None and relay_chain_in:
        persona_id = relay_chain_in[-1] or None

    # Tier 4 — nothing resolved → the honest bare marker relay_from::.
    return relay_policy.build_relay_sender_marker(persona_id, instance_id)


def _append_persona_operator_turn(
    *,
    session_db,
    session_id: str,
    message: str,
    client_message_id: str | None = None,
    skip_if_present: bool = False,
    relay_marker: str | None = None,
    required: bool = False,
) -> bool:
    if session_db is None or not session_id:
        return _persona_chat_persistence_failed(
            "operator_append", None, required=required
        )
    if skip_if_present:
        return True
    safe_message = _redact_persona_chat_text(message, limit=PERSONA_CHAT_OPERATOR_MESSAGE_LIMIT)
    if not safe_message:
        return True
    return _persist_persona_chat_row(
        session_db=session_db,
        session_id=session_id,
        role="user",
        text=safe_message,
        client_message_id=safe_assignment_text(client_message_id, limit=200) or None,
        # Relayed incoming rows carry the sending agent's identity here (the
        # pre_trace_ack typed-marker-in-finish_reason precedent); operator/CLI
        # sends pass relay_marker=None → finish_reason stays None, unchanged.
        relay_marker=relay_marker,
        step="operator_append",
        required=required,
    )


def _append_persona_assistant_text(
    *,
    session_db,
    session_id: str,
    text: str,
    client_message_id: str | None = None,
    required: bool = False,
) -> bool:
    # C8: the ONLY assistant rows this writes are real recorded replies. The
    # pre-trace ack lane that used to inject a canned assistant row here (with
    # a finish_reason marker the projections then had to suppress) is retired —
    # acks are a presentation-only `turn.ack` v2 stream frame now (see
    # _ChatProtocolV2Emitter.ack). Pre-C8 ack rows persisted before this change
    # stay in SessionDB (archive-never-delete) and keep their typed kind on the
    # read side.
    if session_db is None or not session_id:
        return _persona_chat_persistence_failed(
            "assistant_append", None, required=required
        )
    safe = _redact_persona_chat_text(text, limit=PERSONA_CHAT_REPLY_LIMIT)
    if not safe:
        return True
    safe_client_message_id = safe_assignment_text(client_message_id, limit=200)
    if _persona_chat_existing_turn(
        session_db=session_db,
        session_id=session_id,
        client_message_id=safe_client_message_id,
    ).get("assistant"):
        return True
    return _persist_persona_chat_row(
        session_db=session_db,
        session_id=session_id,
        role="assistant",
        text=safe,
        client_message_id=safe_client_message_id or None,
        step="assistant_append",
        required=required,
    )


def _persist_persona_chat_row(
    *,
    session_db,
    session_id: str,
    role: str,
    text: str,
    client_message_id: str | None,
    step: str,
    required: bool,
    relay_marker: str | None = None,
) -> bool:
    """THE explicit-append seam for persona-chat rows.

    Both ``_append_persona_operator_turn`` and ``_append_persona_assistant_text``
    funnel their ``session_db.append_message`` through here, and the write is
    wrapped in ``mirrored_persona_chat_append`` so the SessionDB row and its
    live-log line can never drift apart — one write, one mirror, no hook
    copy-pasted per call site.

    The mirror is bound by a CONTEXT MANAGER rather than by a trailing call on
    purpose. The first cut hooked append sites by convention and immediately
    missed one (``agent_runtime.continuity.return_summary_to_parent_session``,
    the child return-summary lane), so its rows never reached the live log. A
    seam a new append site has to *wrap* is the version of that rule a reviewer
    can actually check.

    The mission-chat lane does not come through here: since native session
    continuity landed it no longer appends the operator/assistant rows itself
    (the runtime persists them with the turn — see
    ``profile_runner.stage_persona_chat_user_row_marker``), so it mirrors from
    its own two known points via ``_mirror_persona_chat_message``.

    KEPT DELIBERATELY — do not re-propose this as dead code
    -------------------------------------------------------
    The 2026-08-18 dead-code audit (NEW-1) proposed reaping this function and
    the two ``_append_*`` writers above it as a "test-only-alive island": zero
    production callers, ~184 lines, ~250 test lines with them. The census is
    RIGHT and the conclusion was REFUSED on 2026-08-19. Reasons, so the next
    sweep does not spend the same hours:

    * **Zero callers is a phase, not a verdict, for a chokepoint.** This is not
      an orphan that lost its lane — it is the door the lane goes through, and
      the lane is currently entering by another route (native continuity). The
      explicit-append route is still reachable and still used: the relay lane
      drives ``_append_persona_operator_turn(relay_marker=)``, whose wire
      behaviour broke silently for eleven days once already.
    * **Deleting it deletes five enforcement points, not one function.**
      Redaction with the per-role limit, the assistant-row idempotency check,
      the mirror binding, the typed ``PersonaChatPersistenceError`` reporting,
      and the ``required=`` raise-or-degrade split. The next explicit append
      would hand-roll all five — which is exactly the failure this seam's
      SHAPE already records: the first cut hooked append sites by convention
      and immediately missed one.
    * **A seam whose only current exercise is a test is under-used, not dead.**
      The honest fix is to give it an executable contract rather than to remove
      it, which is what ``tests/hermes_cli/test_persona_chat_append_seam.py``
      now does.

    The real open question is an OPERATOR one, recorded rather than answered:
    is the explicit-append lane coming back (relay / CLI sends), or should the
    persona-chat write path be declared native-only? If the latter, this goes —
    but deliberately, with its five guarantees re-homed, not as a line-count
    reap.
    """

    from agent_runtime.chat_live_log import mirrored_persona_chat_append

    try:
        with mirrored_persona_chat_append(
            session_db=session_db,
            session_id=session_id,
            role=role,
            text=text,
            client_message_id=client_message_id,
            relay_marker=relay_marker,
        ):
            session_db.append_message(
                session_id=session_id,
                role=role,
                content=text,
                finish_reason=relay_marker,
                platform_message_id=client_message_id,
            )
    except Exception as exc:
        return _persona_chat_persistence_failed(step, exc, required=required)
    return True


def _mirror_persona_chat_message(
    *,
    session_db=None,
    session_id: str,
    role: str,
    text: str,
    client_message_id: str | None = None,
    turn_id: str | None = None,
    relay_marker: str | None = None,
) -> None:
    """Mirror ONE persisted persona-chat message into the live chat log.

    The mission-chat lane's hook (the explicit-append lanes use the
    ``mirrored_persona_chat_append`` seam instead). The text handed in has
    ALREADY crossed the ``_redact_persona_chat_text`` write boundary, so the
    mirror is redaction-safe by construction; the mirror re-runs the shared
    secret rule anyway, because "the caller already did it" is exactly how a
    redaction boundary rots.

    Best effort by contract: the mirror is a regenerable convenience artifact
    (``agent_runtime/chat_live_log.py``), and a mirror failure must never take
    down a chat turn whose transcript is already durable. Failures are counted
    inside the mirror module rather than swallowed anonymously.

    Function-local import: this file is exec'd into ``harness.py``'s globals, so
    a module-level import here would NameError on a live turn.
    """

    try:
        from agent_runtime.chat_live_log import record_chat_message

        record_chat_message(
            session_id=session_id,
            role=role,
            text=text,
            turn_id=turn_id,
            client_message_id=client_message_id,
            relay_marker=relay_marker,
            session_db=session_db,
        )
    except Exception:
        return None
    return None


def _update_persona_chat_token_counts(*, session_db, session_id: str, result) -> None:
    """Record this turn's canonical token usage onto the bound chat session —
    SCRATCH-SESSION LANES ONLY.

    Valid only where the runtime ran with an ephemeral scratch session
    (``mission_chat_reply(session_id=None)``, e.g. the assignment relay lane):
    there ``conversation_loop``'s per-call token writes land on the throwaway
    scratch row, so this explicit post-turn write is the sole writer of the
    bound session the Launcher reads.

    It must NEVER run on the native-continuity chat lane, where the runtime is
    bound to the real chat session (``session_id=active_session_id`` with
    ``persist_agent_session=True``): the per-call runtime writes already land on
    the bound row, and stacking this turn-total write on top double-counts every
    counter at exactly 2x (the 2026-07 "in 20,208 for a bare hi" Runtime-card
    bug). One lane, one usage writer.

    It forwards the COMPLETE canonical usage (cache reads/writes and reasoning,
    not just input/output). ``input_tokens`` is already the uncached, full-price
    remainder; the cache buckets are what let the Launcher tell a warm cache from
    a cold one being re-billed at full rate. Dropping them here was the reason the
    bound session always reported zero cache — keep this write canonical so no
    lossy subset can silently diverge again.
    """
    if session_db is None or not session_id or result is None:
        return
    input_tokens = _positive_int_or_zero(getattr(result, "input_tokens", None))
    output_tokens = _positive_int_or_zero(getattr(result, "output_tokens", None))
    cache_read_tokens = _positive_int_or_zero(getattr(result, "cache_read_tokens", None))
    cache_write_tokens = _positive_int_or_zero(getattr(result, "cache_write_tokens", None))
    reasoning_tokens = _positive_int_or_zero(getattr(result, "reasoning_tokens", None))
    api_calls = _positive_int_or_zero(getattr(result, "api_calls", None))
    if (
        input_tokens == 0
        and output_tokens == 0
        and cache_read_tokens == 0
        and cache_write_tokens == 0
        and reasoning_tokens == 0
        and api_calls == 0
    ):
        return
    try:
        session_db.update_token_counts(
            session_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
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
    normalized = _normalize_cli_persona_id(raw)
    exact = next(
        (persona for persona in personas if getattr(persona, "id", None) == raw),
        None,
    ) or next(
        (persona for persona in personas if getattr(persona, "id", None) == normalized),
        None,
    )
    if exact is not None:
        return exact
    if raw.lower().startswith("profile:"):
        profile_id = safe_assignment_token(raw.split(":", 1)[1])
        if not profile_id:
            return None
        matching_profile_persona, _, _ = profile_persona_resolution(profile_id, personas)
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
    return None


def _display_name_for_profile(profile_id: str) -> str:
    return " ".join(part.capitalize() for part in profile_id.replace("_", "-").split("-") if part) or "Profile"


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




def _close_free_floating_assignments(persona_instance_id: str, *, reason: str, json_output: bool, terminal_state: str) -> int:
    # Function-local: this file is exec'd into harness.py's globals (see the
    # note in _cmd_mission_chat_message).
    from agent_runtime.mission_chat_outcome import (
        FinalizationWarning,
        FinalizationWarningKind,
    )

    cfg = load_agent_runtime_config()
    normalized_instance = safe_assignment_token(persona_instance_id)
    store = PersonaAssignmentStore()
    matches = [
        item
        for item in store.list_all()
        if item.persona_instance_id == normalized_instance
        and item.evidence_kind == "free_floating"
        and item.state not in {"completed", "blocked", "cancelled"}
    ]
    if not matches:
        data = {"ok": False, "error": f"no active free-floating assignments for {persona_instance_id}"}
        print(emit_json(data) if json_output else data["error"])
        return 2
    closed = [store.complete(item.id, state=terminal_state, error=reason) for item in matches]
    finalization_warnings: list[FinalizationWarning] = []
    try:
        instance_store = PersonaInstanceStore()
        instance = instance_store.get(normalized_instance)
        if instance.current_assignment_id in {item.id for item in closed}:
            instance.current_assignment_id = None
            instance.mode = "configured"
            instance_store.update(instance)
    except Exception as instance_commit_exc:
        # Third copy of the same swallow. The assignments ARE closed by this
        # point, so a failure here leaves the instance pointing at work that no
        # longer exists — reported, not hidden.
        finalization_warnings.append(
            FinalizationWarning(
                kind=FinalizationWarningKind.INSTANCE_STATE_COMMIT_FAILED,
                detail=type(instance_commit_exc).__name__,
                step="clear_instance_assignment",
            )
        )
    data = {
        "ok": True,
        "persona_instance_id": normalized_instance,
        "closed_assignment_ids": [item.id for item in closed],
        "state": terminal_state,
        "production_proof_eligible": False,
        **(
            {
                "finalization_warnings": [
                    warning.as_dict() for warning in finalization_warnings
                ]
            }
            if finalization_warnings
            else {}
        ),
    }
    print(emit_json(data) if json_output else f"closed {len(closed)} free-floating assignments for {normalized_instance}")
    return 0


def _mission_chat_target_decision(
    *,
    instance_store,
    normalized_persona: str,
    raw_persona_id,
    persona_instance_id,
    session_id,
    relay_chain,
    requested_by_session=None,
):
    """Decide the ``ambiguous_target`` refusal for a mission-chat send.

    Reads the persona's live instances from the store (the roster's
    on-the-level set — retired instances are archived out of ``list_all``),
    NARROWS them to the sender's workspace, and computes whether the caller
    already pinned a specific instance, then defers the actual decision to the
    pure ``target_policy.evaluate_target`` authority (unit-testable in
    isolation, no store).

    ``caller_pinned`` is True whenever the send is NOT on the silent-fallback
    path — an explicit ``persona_instance_id``, a ``personainst_*`` target, or
    ANY caller-chosen ``session_id`` (the operator console always carries an
    instance-bearing session id, so its sends never trip this). Only the
    omitted-session + no-instance path can be ambiguous.

    ``requested_by_session`` is the SENDER's chat-root session id (threaded from
    the relay envelope). A BARE persona id is resolved only among the placements
    in the sender's own workspace, so a persona placed into several workspace
    scenes does not fan a two-agent order out onto duplicate placements in
    unrelated workspaces. Runtime-global PLACEMENT rows (no workspace pointer)
    stay in scope everywhere, but runtime-global CANONICAL rows are excluded
    from the candidate list under a real scope (instance = in-level placement)
    — an unplaced persona then has zero candidates, which ``evaluate_target``
    allows through to today's canonical-channel fallback (retiring that
    fallback is gated on the global-row adoption migration). An operator CLI
    invocation with no sender session falls back to the active workspace.

    "Placements shadow canonical": when an in-scope PLACEMENT of the persona
    exists, its auto-derived CANONICAL row is dropped from the candidate list —
    so a bare persona id with one in-scope placement resolves to that single
    placement (``evaluate_target`` auto-routes on one candidate, retiring the
    ambiguity prompt) instead of the plumbing canonical row, while TWO in-scope
    placements stay genuinely ambiguous. The scope + shadow is the one shared
    ``workspace_scope.addressable_roster`` authority; the count-based policy is
    unchanged.
    """
    from agent_runtime import target_policy, workspace_scope
    from agent_runtime.persona_assignments import (
        is_canonical_persona_channel,
        sender_scope_workspace_id,
    )

    is_profile = normalized_persona.startswith("profile:")
    raw_token = safe_assignment_token(raw_persona_id)
    caller_pinned = bool(
        persona_instance_id
        or raw_token.startswith(PERSONA_INSTANCE_ID_PREFIX)
        or safe_assignment_text(session_id, limit=200)
    )
    # Derive the sender's workspace scope (session → owner instance → its
    # workspace pointer; bare operator CLI send falls back to the active
    # workspace), then scope + shadow the persona's rows through the one shared
    # addressable-roster authority.
    scope_workspace_id = sender_scope_workspace_id(
        requested_by_session, instance_store=instance_store
    )
    addressable = workspace_scope.addressable_roster(
        (
            instance
            for instance in instance_store.list_all()
            if getattr(instance, "persona_id", None) == normalized_persona
        ),
        scope_workspace_id=scope_workspace_id,
        is_canonical=is_canonical_persona_channel,
    )
    candidates = sorted(
        (
            target_policy.TargetCandidate(
                instance_id=instance.id,
                display_name=safe_assignment_text(getattr(instance, "display_name", None), limit=120)
                or instance.id,
            )
            for instance in addressable
        ),
        key=lambda candidate: candidate.instance_id,
    )
    return target_policy.evaluate_target(
        persona_id=normalized_persona,
        candidates=candidates,
        caller_pinned_instance=caller_pinned,
        is_profile_target=is_profile,
        relay_chain=relay_chain,
    )


def _mission_chat_bare_persona_target(
    instance_store,
    *,
    normalized_persona: str,
    requested_by_session=None,
):
    """Resolve a BARE persona id to its single in-scope PLACEMENT id, or ``None``.

    The routing counterpart to the ambiguous-target guard: both read the one
    ``workspace_scope.addressable_roster`` authority with the same sender scope,
    so they never disagree. When exactly one placement of the persona is in the
    sender's scope, a bare persona send threads onto THAT placement (the
    "placements shadow canonical" ruling — the plumbing canonical row is not the
    default target while a deliberate placement is on the level). Returns
    ``None`` when there is no in-scope placement (the caller falls back to the
    canonical channel, reachability fallback) or when the guard would already
    have refused two-or-more in-scope placements.
    """
    from agent_runtime import workspace_scope
    from agent_runtime.persona_assignments import (
        is_canonical_persona_channel,
        sender_scope_workspace_id,
    )

    scope_workspace_id = sender_scope_workspace_id(
        requested_by_session, instance_store=instance_store
    )
    addressable = workspace_scope.addressable_roster(
        (
            instance
            for instance in instance_store.list_all()
            if getattr(instance, "persona_id", None) == normalized_persona
        ),
        scope_workspace_id=scope_workspace_id,
        is_canonical=is_canonical_persona_channel,
    )
    placements = [
        instance for instance in addressable if not is_canonical_persona_channel(instance)
    ]
    if len(placements) == 1:
        return placements[0].id
    return None


def _resolve_mission_chat_persona_id(persona_id, persona_instance_id) -> str:
    """Resolve the chat target persona from whichever identity the caller sent.

    Prefer the persona id; when it is mangled (a stale instance-shaped id from a
    legacy SessionDB row, a display token, etc.) but the caller also supplied a
    resolvable persona_instance_id, the instance wins instead of failing the
    whole send.
    """
    try:
        return _normalize_cli_persona_or_template_id(persona_id)
    except ValueError:
        instance_token = safe_assignment_token(persona_instance_id)
        if instance_token:
            return _persona_id_from_instance_id(instance_token)
        raise


# ``_normalize_cli_persona_id``, ``_persona_id_from_instance_id`` and
# ``_normalize_cli_persona_or_template_id`` were defined here. They are pure
# functions of ``agent_runtime.persona_assignments`` primitives and moved into
# that module (aliased back to these names in this part's import header) so the
# chat-root durability step, which normalizes the persona id it stamps on the
# session row, could stop being reachable only from a CLI part.
