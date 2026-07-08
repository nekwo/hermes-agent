# Loaded by hermes_cli.harness via _load_command_parts(); executed in harness.py globals.
# Keep command bodies here so parser registration stays separate from persona/chat behavior.

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
    try:
        normalized_persona = _resolve_mission_chat_persona_id(
            args.persona_id, getattr(args, "persona_instance_id", None)
        )
    except ValueError as exc:
        data = {
            "ok": False,
            "capability_id": "mission.chat.message",
            "execution_state": "rejected",
            "error_kind": "unsupported_persona",
            "error": safe_assignment_text(str(exc), limit=240),
            "persona_id": safe_assignment_token(args.persona_id),
            "next_expected": "pass a seeded persona id (e.g. neko_supervisor, dev), profile:<name>, or a known personainst_* instance id",
        }
        if getattr(args, "stream", False):
            _emit_chat_final(data)
        else:
            print(emit_json(data) if args.json else data["error"])
        return 2
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
    ) or f"agent-chat-send-{uuid.uuid4().hex[:12]}"
    replay = _persona_chat_existing_turn(
        session_db=session_db,
        session_id=session_id,
        client_message_id=client_message_id,
    )
    if replay.get("assistant"):
        reply_text = _redact_persona_chat_text(
            replay["assistant"].get("content"), limit=PERSONA_CHAT_REPLY_LIMIT
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
            turn_id=safe_assignment_token(client_message_id),
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

    reply_text = _redact_persona_chat_text(getattr(chat_result, "final_response", "") or "", limit=PERSONA_CHAT_REPLY_LIMIT)
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


def _cmd_persona_instance_sweep_orphans(args) -> int:
    result = PersonaInstanceStore().sweep_orphaned_task_bound_instances(
        reason=str(getattr(args, "reason", "") or "operator persona instance janitor"),
    )
    data = {"ok": True, "persona_instance_cleanup": result}
    if args.json:
        print(emit_json(data))
    else:
        print(
            "persona instances: "
            f"task_bound {result['before_task_bound_count']} -> {result['after_task_bound_count']}; "
            f"reaped {result['reaped_count']}; "
            f"active preserved {result['skipped_active_count']}"
        )
        remaining = result.get("remaining_task_bound_persona_instance_ids") or []
        if remaining:
            print("remaining task-bound instances: " + ", ".join(remaining[:20]))
    return 0


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
    safe_message = _redact_persona_chat_text(message, limit=PERSONA_CHAT_OPERATOR_MESSAGE_LIMIT)
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
    safe = _redact_persona_chat_text(text, limit=PERSONA_CHAT_REPLY_LIMIT)
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
    safe_message = _redact_persona_chat_text(message, limit=PERSONA_CHAT_OPERATOR_MESSAGE_LIMIT)
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
            turn_id=safe_assignment_token(client_message_id) or safe_assignment_token(assignment_id),
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

    reply_text = _redact_persona_chat_text(getattr(chat_result, "final_response", "") or "", limit=PERSONA_CHAT_REPLY_LIMIT)
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
    if safe_assignment_token(raw).startswith("personainst_"):
        # Mission Control payloads, legacy SessionDB rows, and agent tool calls
        # routinely leak persona INSTANCE ids into persona-id slots. Resolve
        # them here — the one persona-id boundary — instead of rejecting, so
        # every chat entry point accepts either form.
        return _persona_id_from_instance_id(raw)
    return _normalize_cli_persona_id(raw)
