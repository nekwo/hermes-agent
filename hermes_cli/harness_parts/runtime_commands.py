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
    tasks = []
    runs = []
    incidents = []
    workers = []
    cfg = load_agent_runtime_config()
    execution_mode = "manual"
    data = build_observability(
        tasks=tasks,
        runs=runs,
        incidents=incidents,
        proofs=[],
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
