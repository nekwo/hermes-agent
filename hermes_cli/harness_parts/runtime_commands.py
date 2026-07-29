# Loaded by hermes_cli.harness via _load_command_parts(); executed in harness.py globals.
# Runtime/task/lane/worker/stream command bodies live here to keep harness.py focused on CLI wiring.

def _cmd_worktree_reap(args) -> int:
    from agent_runtime.delivery_directive import reap_orphan_worktrees

    data = reap_orphan_worktrees(
        min_age_seconds=int(getattr(args, "min_age_seconds", 3600) or 3600),
        dry_run=bool(getattr(args, "dry_run", False)),
        include_legacy_temp=bool(getattr(args, "include_legacy_temp", False)),
    )
    if getattr(args, "json", False):
        print(emit_json(data))
    else:
        dry_run = bool(data.get("dry_run"))
        action = "preview: would reap" if dry_run else "reaped"
        print(f"{action} {len(data.get('reaped') or [])} worktree(s); kept {len(data.get('kept') or [])}")
        for item in data.get("reaped") or []:
            captured = f" (diff -> {item['captured_patch']})" if item.get("captured_patch") else ""
            prefix = "would reap: " if dry_run else ""
            print(f"  - {prefix}{item['worktree']}{captured}")
    return 0


def _cmd_persona_instance_reconcile(args) -> int:
    from agent_runtime.persona_instance_identity import reconcile_persona_instances

    data = reconcile_persona_instances(apply=not bool(getattr(args, "dry_run", False)))
    if getattr(args, "json", False):
        print(emit_json(data))
    else:
        mode = "applied" if data["applied"] else "dry-run"
        print(
            f"persona-instance reconcile ({mode}): merged={data['merged_count']} "
            f"renamed={data['renamed_count']} skipped={data['skipped_count']} "
            f"pruned={data.get('pruned_count', 0)} held={data.get('held_count', 0)} "
            f"steering_repaired={data.get('steering_repaired_count', 0)} "
            f"chat_bindings_cleared={data.get('session_binding_repaired_count', 0)} "
            f"aliases={data['alias_count']}"
        )
        for item in data["actions"]:
            print(f"  - {item['action']}: {item['from_id']} -> {item['to_id']}")
        for item in data.get("pruned") or []:
            print(f"  - pruned ({item['reason']}): {item['persona_instance_id']}")
        for item in data.get("held") or []:
            print(f"  - held ({item['reason']}): {item['persona_instance_id']}")
        for item in data.get("steering_repairs") or []:
            print(
                f"  - steering repaired: {item['persona_instance_id']} "
                f"removed {item['missing_parent_ids']}"
            )
        for item in data.get("session_binding_repairs") or []:
            print(
                f"  - chat binding cleared: {item['persona_instance_id']} "
                f"-> missing session {item['session_id']} ({', '.join(item.get('cleared_fields') or [])})"
            )
        for item in data.get("session_binding_held") or []:
            print(
                f"  - chat binding held ({item['reason']}): {item['persona_instance_id']} "
                f"-> {item['session_id']}"
            )
        if data.get("session_binding_skipped"):
            print(f"  - chat binding repair skipped: {data['session_binding_skipped']}")
    return 0


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
    from agent_runtime.config import load_root_runtime_config

    cfg = load_root_runtime_config()
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
    from agent_runtime.config import load_root_runtime_config

    cfg = load_root_runtime_config()
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
    result = TickEngine(
        config=cfg,
        persona_runtime=GPTPersonaRuntime(default_provider=cfg.default_provider, default_model=cfg.default_model),
    ).tick_once(task_id=args.task_id)
    print(emit_json(result) if args.json else f"tick {result.tick_id}: {len(result.actions_taken)} actions")
    return 0


def _cmd_run_until_settled(args) -> int:
    cfg = load_agent_runtime_config()
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
    cfg = load_agent_runtime_config()
    execution_mode = "manual"
    data = build_observability(
        tasks=tasks,
        runs=runs,
        incidents=incidents,
        proofs=proofs,
        daemon_status=None,
        events=EventLog().tail(20),
        execution_mode=execution_mode,
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


def _cmd_agents(args) -> int:
    personas = ensure_persisted_personas(load_agent_runtime_config())
    print(emit_json(personas) if args.json else "\n".join(f"{p.id} ({p.role})" for p in personas))
    return 0


def _cmd_smoke(args) -> int:
    data = run_smoke(no_model=args.no_model)
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


def _incident_cursor_ts(incident):
    """The timestamp an incident is ordered/paged by: when it closed (history)
    or when it opened (still live)."""

    return getattr(incident, "closed_at", None) or getattr(incident, "opened_at", None)


def _incident_history_row(incident) -> dict:
    cursor = _incident_cursor_ts(incident)
    return {
        "incident_id": incident.id,
        "task_id": incident.task_id,
        "run_id": incident.run_id,
        "kind": incident.kind,
        "summary": incident.summary,
        "is_open": incident.closed_at is None,
        "opened_at": incident.opened_at,
        "closed_at": incident.closed_at,
        "cursor": cursor,
    }


def _cmd_incident_list(args) -> int:
    """List incidents, or page the closed/ancient HISTORY tail S2 evicts from the
    frame. ``--state {open,closed,all}`` selects the lane; ``--before <iso>`` +
    ``--limit`` page newest-first over the incident store (the store IS the
    history — no new storage). Back-compat: ``--all`` == ``--state all``,
    default == open-only."""

    store = IncidentStore()
    incidents = store.list_all()
    state = getattr(args, "state", None)
    if not state:
        state = "all" if getattr(args, "all", False) else "open"
    if state == "open":
        incidents = [i for i in incidents if i.closed_at is None]
    elif state == "closed":
        incidents = [i for i in incidents if i.closed_at is not None]
    # Newest-first by cursor (closed_at for closed, opened_at for open) so
    # `--before` walks backwards through history one page at a time.
    incidents = sorted(
        incidents,
        key=lambda i: (_incident_cursor_ts(i) is not None, _incident_cursor_ts(i)),
        reverse=True,
    )
    before_text = getattr(args, "before", None)
    if before_text:
        try:
            before = datetime.fromisoformat(str(before_text).replace("Z", "+00:00"))
        except ValueError:
            data = {"ok": False, "error": "invalid_before", "message": "--before must be an ISO-8601 timestamp"}
            print(emit_json(data) if getattr(args, "json", False) else data["message"])
            return 1
        incidents = [i for i in incidents if (_incident_cursor_ts(i) is not None and _incident_cursor_ts(i) < before)]
    truncated = False
    limit = getattr(args, "limit", None)
    if limit is not None:
        limit = max(1, min(500, int(limit)))
        if len(incidents) > limit:
            incidents = incidents[:limit]
            truncated = True
    rows = [_incident_history_row(i) for i in incidents]
    if getattr(args, "json", False):
        next_before = rows[-1]["cursor"] if (truncated and rows) else None
        data = {
            "ok": True,
            "state": state,
            "count": len(rows),
            "truncated": truncated,
            "next_before": next_before,
            "incidents": rows,
        }
        print(emit_json(data))
    else:
        print("\n".join(f"{r['incident_id']} {r['kind']} {'open' if r['is_open'] else 'closed'} {r['summary']}" for r in rows))
    return 0


def _cmd_persona_chat_history(args) -> int:
    """Paged on-demand read of one persona chat session's message tail — the
    fetch that replaces the tail S2 evicts from the frame. The frame carries the
    recency pointer (session id + anchors); this returns the messages."""

    from agent_runtime.persona_chat_history import persona_chat_session_messages

    limit = max(1, min(40, int(getattr(args, "limit", 40) or 40)))
    data = persona_chat_session_messages(
        session_id=args.session_id,
        limit=limit,
        before=getattr(args, "before", None),
    )
    if getattr(args, "json", False):
        print(emit_json(data))
    else:
        lines = []
        for message in data["messages"]:
            text = str(message.get("text") or "").splitlines()
            head = text[0][:120] if text else ""
            lines.append(f"{message.get('timestamp') or '-'} {message.get('role')}: {head}")
        print("\n".join(lines) if lines else f"no messages for {args.session_id}")
    return 0 if data.get("ok") is not False else 2


def _cmd_incident_close(args) -> int:
    incident = IncidentStore().close(args.incident_id, reason=args.reason)
    data = {"incident_id": incident.id, "closed": incident.closed_at is not None, "reason": args.reason}
    print(emit_json(data) if args.json else f"closed {incident.id}: {args.reason}")
    return 0


def _cmd_snapshot(args) -> int:
    from agent_runtime.config import load_root_runtime_config

    cfg = load_root_runtime_config()
    snap = write_snapshot(build_snapshot())
    read_model_cfg = getattr(cfg, "read_model", None)
    if bool(getattr(read_model_cfg, "enabled", False)) and bool(getattr(read_model_cfg, "serve_snapshot_from_db", True)):
        from agent_runtime.read_model import ReadModel

        snap = ReadModel().render_snapshot()
    print(emit_json(snap) if args.json else "snapshot written")
    return 0


def _cmd_stream(args) -> int:
    from agent_runtime.stream import stream_frames
    from agent_runtime.serde import to_jsonable

    try:
        for frame in stream_frames(
            poll_interval_seconds=float(getattr(args, "poll_interval", 0.25) or 0.25),
            heartbeat_interval_seconds=float(getattr(args, "heartbeat_interval", 5.0) or 5.0),
            # 0 disables the settle window; None/absent (old Namespace shapes)
            # keeps the coalescing default.
            delta_debounce_seconds=max(
                0.0, float(getattr(args, "delta_debounce_ms", 200) or 0) / 1000.0
            ),
            max_frames=getattr(args, "max_frames", None),
            # S6: a reconnecting client that lost its fold base asks for a fresh
            # full-core baseline before it folds any patch (else it would fold
            # onto stale/absent state). Off by default → normal patch/delta lane.
            resync=bool(getattr(args, "resync", False)),
        ):
            sys.stdout.write(json.dumps(to_jsonable(frame), ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        return 0
    return 0


def _cmd_rebuild_read_model(args) -> int:
    from agent_runtime.projector import Projector
    from agent_runtime.read_model import ReadModel
    from agent_runtime.config import load_root_runtime_config

    read_model = ReadModel()
    Projector(read_model, config=load_root_runtime_config()).full_rebuild()
    watermark = read_model.projection_watermark("snapshot")
    payload = {"ok": True, "watermark": watermark, "db_path": str(read_model.db_path)}
    print(emit_json(payload) if args.json else f"read model rebuilt: {read_model.db_path}")
    return 0


def _cmd_read_projection(args) -> int:
    from agent_runtime.read_model import ReadModel

    payload = ReadModel().read_projection(args.projection, since_offset=args.since_offset)
    print(emit_json(payload) if args.json else emit_json(payload))
    return 0
