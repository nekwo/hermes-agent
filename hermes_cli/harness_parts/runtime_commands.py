# Loaded by hermes_cli.harness via _load_command_parts(); executed in harness.py globals.
# Runtime/task/lane/worker/stream command bodies live here to keep harness.py focused on CLI wiring.

# Explicit import header. Still exec'd into harness.py's globals by
# _load_command_parts — that mechanism is unchanged — but no longer dependent
# on it: these names used to arrive implicitly from whatever harness.py
# imported, so a wrong one surfaced as a NameError only when an operator ran
# the one verb that touched it. Re-importing a name harness.py also imports
# rebinds it to the identical object; both halves are checked by
# tests/hermes_cli/test_harness_parts_namespace.py.

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from agent_runtime import paths
from agent_runtime.cli_format import emit_json
from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config
from agent_runtime.decision_contract_registry import (
    contract_manifest,
)
from agent_runtime.events import EventLog
from agent_runtime.migrations import effective_config_summary, migration_status
from agent_runtime.observability import build_observability
from agent_runtime.profile_context import active_profile_name
from agent_runtime.provider_health import provider_health_for_personas
from agent_runtime.status import build_status
from hermes_cli.harness_support import harness_repo_root


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
            f"graphs_pruned={data.get('graphs_pruned_count', 0)} "
            f"graphs_held={data.get('graphs_held_count', 0)} "
            f"graph_steering_settled={data.get('graph_departed_steering_count', 0)} "
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
        # The graph-prune phase (6c5040ed2) archives owner-less runtime graphs
        # and emits `flow_graph.pruned`. It reported only through `--json` until
        # S28; a phase that moves files and appends events must be legible to the
        # operator reading the human render too.
        for item in data.get("graphs_pruned") or []:
            print(
                f"  - graph pruned ({item['reason']}): {item['graph_id']} "
                f"(drew {item.get('drawn_agent_count', 0)} agent(s))"
            )
        for item in data.get("graphs_held") or []:
            print(f"  - graph held ({item['reason']}): {item['graph_id']}")
        for item in data.get("graph_departed_steering") or []:
            if not item.get("changed"):
                continue
            print(
                f"  - graph steering settled: {item['persona_instance_id']} "
                f"-> removed {item['owner']} ({item['graph_id']})"
            )
    return 0


# S54 removed ``_archived_task_summary``: an archived-task formatter left over
# from the retired task surface.

# S42: ``_event_value`` (an Event field reader), ``_task_events`` (the
# ``EventLog().for_task`` page), ``_clear_task_recovery_markers`` (it mutated
# ``Task.harness_self_heal``, a record deleted in S8 — its own annotation named a
# type this scope cannot resolve), and ``_safe_operator_text`` were removed here:
# each had exactly one hit in the whole harness scope, its own ``def``.

# S13: ``_cancel_task_active_runs`` / ``_close_task_active_workers`` (mission-lane
# task teardown) and the ``_swarm_state_path`` / ``_read_swarm_state`` /
# ``_write_swarm_state`` trio (swarm_state.json, a lane-scheduler artifact) were
# removed here: after the mission-lane removal no harness verb, part, or test
# referenced any of them. swarm_state.json now has no reader and no writer.


def _cmd_status(args) -> int:
    # S28: the line used to open with `open_tasks=` / `running_runs=`. Both were
    # constants by construction on the producer side (see agent_runtime/status.py),
    # so the human render stopped reporting them rather than keep printing a
    # literal an operator would read as a measurement. The verb is unchanged.
    from agent_runtime.root_observability import attach_root_observability

    # ``chat_scope`` here is not chat data — it is this machine's ORIENTATION.
    # ``status`` is the verb an operator (and an agent) runs to ask "which
    # runtime am I talking to", and until the head home was a durable runtime
    # declaration (``config_declared``, 2026-08-13) the honest answer needed
    # the Launcher's spawn environment to be visible. Now the winning rung
    # says it: ``source: "config_declared"`` means the runtime declared its own
    # head, ``ambient_home`` means nobody did and the answer is a guess.
    data = attach_root_observability(build_status(), chat_scope=True)
    _attach_runtime_service_blocks(
        data, prune_stale=bool(getattr(args, "prune_stale", False))
    )
    if args.json:
        print(emit_json(data))
    else:
        build = data.get("runtime_build") or {}
        commit = build.get("commit_short") or "unknown"
        dirty = "+dirty" if build.get("dirty") else ""
        print(
            f"open_incidents={data['open_incidents']} dirty={data['dirty_summary']} "
            f"runtime_health={data['runtime_health']['ok']} "
            f"build={commit}{dirty} serves={_serves_render(data)}"
        )
    return 0


def _serves_render(data: dict) -> str:
    """``<live>/<entries>`` when the registry was READ; else why it was not.

    S28's rule, applied to the field that arrived after it: no constant dressed
    as a count. When ``_attach_runtime_service_blocks`` cannot read the serve
    registry it sets ``serve_instances`` to ``[]`` plus a typed
    ``serve_instances_error`` — the ``--json`` consumer sees both, but the
    human line rendered ``serves=0/0``, which is not a degraded measurement, it
    is a measurement that never happened claiming the most reassuring possible
    value ("no serves, all accounted for"). An operator debugging a serve that
    will not appear would read the registry failure as an empty registry.
    """

    error = data.get("serve_instances_error")
    if error:
        kind = "unknown"
        if isinstance(error, dict):
            kind = str(error.get("error_kind") or "unknown")
        return f"unavailable({kind})"
    instances = data.get("serve_instances") or []
    live = sum(1 for row in instances if row.get("classification") == "live")
    return f"{live}/{len(instances)}"


def _attach_runtime_service_blocks(data: dict, *, prune_stale: bool) -> None:
    """Stamp ``runtime_build`` + ``serve_instances`` onto a status envelope.

    The two questions a durable runtime-root service makes newly askable, and
    which nothing on this machine could answer before: *what code is answering
    me*, and *how many serves are running against this root*.

    ``runtime_build`` is the build of the PROCESS THAT ANSWERED — which is the
    point, not a caveat. Run as a plain CLI it reports the install; answered
    through ``harness serve`` it reports the SERVICE, because the handler runs
    inside the serve process. ``answered_by`` says which, so an operator
    comparing the two readings sees a service pinned to older code. That
    provenance comes from ``current_serve_request_id()`` — the one honest
    "did this arrive via serve" fact (two proxies for it have already been
    retired for impersonating it; see its docstring).

    ``serve_instances`` is reported, never silently repaired: stale entries
    stay visible until ``--prune-stale`` deletes the provably-dead ones and
    says exactly which. A plain ``harness status --json`` is cacheable inside
    serve for up to 20s, so its instance list can be that stale (the exit
    frame's ``served_from_cache`` discloses it); the ``--prune-stale`` argv
    does not match the cacheable shape at all, so a prune is always freshly
    computed and never itself cached.

    Additive and fail-open: each block degrades to a typed error marker rather
    than raising, because a diagnostic that can fail the verb it instruments is
    worse than no diagnostic. Function-local imports — this file is exec'd into
    harness.py's globals.
    """

    try:
        from agent_runtime.build_stamp import build_stamp
        from hermes_cli.harness_parts.serve import current_serve_request_id

        data["runtime_build"] = {
            **build_stamp().payload(),
            "answered_by": "serve" if current_serve_request_id() else "cli",
        }
    except Exception as exc:
        data["runtime_build"] = {"error_kind": type(exc).__name__}

    try:
        from agent_runtime.serve_registry import (
            list_serve_instances,
            prune_stale_serve_instances,
        )

        root = paths.store_root()
        if prune_stale:
            data["serve_instances_pruned"] = prune_stale_serve_instances(root)
        data["serve_instances"] = list_serve_instances(root)
    except Exception as exc:
        data["serve_instances"] = []
        data["serve_instances_error"] = {"error_kind": type(exc).__name__}


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
    # Not ``Path(__file__)``: this file is exec'd into harness.py's globals, so
    # ``__file__`` reads as *harness.py's* path and ``parents[1]`` lands on the
    # repo root only by accident of harness.py sitting one level down. Anchor
    # to the support module instead, so the answer is the same either way.
    repo_root = harness_repo_root()
    packet = {
        "schema_version": 1,
        "verification_id": f"harness_runtime_verify_{started.strftime('%Y%m%dT%H%M%SZ')}",
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
    # S28: the `tasks=[]` / `proofs=[]` / `daemon_status=None` keywords were the
    # literals holding three dead parameters open on `build_observability`; they
    # and everything they alone fed are gone. The unread `load_agent_runtime_config`
    # call went with them — it was assigned and never used.
    runs = []
    incidents = []
    execution_mode = "manual"
    data = build_observability(
        runs=runs,
        incidents=incidents,
        events=EventLog().tail(20),
        execution_mode=execution_mode,
    )
    print(emit_json(data) if args.json else f"observability={data['health']['status']} interventions={len(data['interventions'])}")
    return 0


def _cmd_contracts_dump(args) -> int:
    manifest = contract_manifest()
    if args.json:
        print(emit_json(manifest))
    else:
        print(f"contracts schema={manifest['schema_version']} hash={manifest['contract_hash'][:16]}")
    return 0


def _run_verify_command(label: str, command: list[str], *, cwd: Path) -> dict:
    started = time.monotonic()
    try:
        # encoding= pinned: text=True alone decodes with the locale codepage
        # (cp1252 on Windows), and non-cp1252 bytes in a sub-command's output
        # crash subprocess's reader thread without changing the exit code.
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
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
            completed = subprocess.run(
                ["git", *args],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except Exception:
            return None
        return completed.stdout.strip() if completed.returncode == 0 else None

    status = run(["status", "--short"])
    return {"path": str(root), "git_head": run(["rev-parse", "HEAD"]), "dirty": bool(status)}


# S13: ``_cmd_agents`` was defined but wired to no parser; ``harness agent list``
# (_cmd_agent_list) is the live, richer implementation of the same listing.


# S42: ``_safe_issue_summary`` (an issue-discovery row projection) and the
# ``_incident_history_row`` / ``_incident_cursor_ts`` pair (incident paging)
# were removed here. Their readers were the mission-record verbs S8/S9 deleted;
# the cursor helper's only caller was the row helper, so the pair went as one.


def _cmd_persona_chat_history(args) -> int:
    """Paged on-demand read of one persona chat session's message tail — the
    fetch that replaces the tail S2 evicts from the frame. The frame carries the
    recency pointer (session id + anchors); this returns the messages."""

    from agent_runtime.persona_chat_history import persona_chat_session_messages
    from agent_runtime.root_observability import attach_root_observability

    limit = max(1, min(40, int(getattr(args, "limit", 40) or 40)))
    # ``chat_scope`` is the incident tell: a ``count: 0`` envelope now says
    # WHICH state.db it read and how that head was named. ``source:
    # "ambient_home"`` beside an empty result means "wrong root", not "no
    # messages" (2026-08-12 ambient chat-history incident).
    data = attach_root_observability(
        persona_chat_session_messages(
            session_id=args.session_id,
            limit=limit,
            before=getattr(args, "before", None),
        ),
        chat_scope=True,
    )
    if getattr(args, "json", False):
        print(emit_json(data))
    elif data.get("ok") is False:
        # A read that FAILED must never render as "no messages" — that is the
        # exact sentence that makes an operator conclude the transcript is gone.
        detail = str(data.get("error") or "").strip()
        print(
            f"could not read {args.session_id}: {data.get('error_kind') or 'error'}"
            + (f" — {detail}" if detail else "")
        )
    else:
        lines = []
        for message in data.get("messages") or []:
            text = str(message.get("text") or "").splitlines()
            head = text[0][:120] if text else ""
            lines.append(f"{message.get('timestamp') or '-'} {message.get('role')}: {head}")
        print("\n".join(lines) if lines else f"no messages for {args.session_id}")
    return 0 if data.get("ok") is not False else 2


def _cmd_snapshot(args) -> int:
    # This module is exec'd into harness.py's globals — every new name is a
    # function-local import.
    from agent_runtime.config import load_root_runtime_config
    from agent_runtime.read_model import resolve_snapshot_frame

    read_model_cfg = getattr(load_root_runtime_config(), "read_model", None)
    prefer_cache = bool(getattr(read_model_cfg, "enabled", False)) and bool(
        getattr(read_model_cfg, "serve_snapshot_from_db", True)
    )
    # The serve source (built / cache / cache_miss_rebuilt) rides the frame's
    # parity envelope. The old branch replaced the freshly built frame with
    # ``render_snapshot()`` unconditionally, so an unpopulated read model made
    # ``harness snapshot --json`` print ``{}`` — an empty runtime, silently.
    resolved = resolve_snapshot_frame(prefer_cache=prefer_cache)
    print(
        emit_json(resolved.frame)
        if args.json
        else f"snapshot written (frame_source={resolved.source})"
    )
    return 0


def _cmd_stream(args) -> int:
    from agent_runtime.patch_coverage import parse_fold_entities_option
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
            # The client's fold-capability declaration. ``getattr`` defaults to
            # None — an old Namespace shape without the flag says NOTHING, which
            # resolves to the historical {persona_instance, incident} and is
            # exactly today's wire. An empty STRING is a different statement
            # ("I fold nothing") and `parse_fold_entities_option` keeps the two
            # apart all the way down.
            fold_entities=parse_fold_entities_option(getattr(args, "fold_entities", None)),
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


# --- `harness work` — running background work -------------------------------
#
# Every import below is function-local ON PURPOSE. These bodies are exec'd into
# hermes_cli/harness.py's globals rather than imported as a module, so a
# module-level import here binds into a namespace shared with five other part
# files; the failure mode is a NameError that fires only when an operator runs
# this one verb, mid-turn (the `persona_commands.py:1435` precedent, pinned by
# tests/hermes_cli/test_harness_parts_namespace.py).


def _work_target_details(row: dict) -> dict:
    """The identifying facts a confirmation prompt must show before a kill.

    Deliberately the human-recognisable ones — what it is, which agent owns it,
    which OS process it maps to — because ``terminal:sess-8f2c`` alone tells an
    operator nothing about whether this is the build they want stopped or the
    dev server they do not.
    """

    owner = row.get("owner") or {}
    return {
        "work_id": row.get("work_id"),
        "kind": row.get("kind"),
        "label": row.get("label"),
        "command": row.get("command") or None,
        "pid": row.get("pid"),
        "pid_verified": row.get("pid_verified"),
        "status": row.get("status"),
        "elapsed_seconds": row.get("elapsed_seconds"),
        "owner_persona_instance_id": owner.get("persona_instance_id"),
        "owner_session_id": owner.get("session_id"),
    }


def _cmd_work_list(args) -> int:
    from agent_runtime.parity import ProjectionAccountant
    from agent_runtime.running_work import RUNNING_WORK_KINDS, build_running_work

    kind = str(getattr(args, "kind", None) or "").strip()
    if kind and kind not in RUNNING_WORK_KINDS:
        _print_stage42(
            _error_envelope(
                "invalid_request",
                f"unknown work kind {kind!r}",
                safe_details={"supported": list(RUNNING_WORK_KINDS)},
            ),
            args=args,
            default_output="json",
        )
        return ERROR_EXIT_CODES["invalid_request"]

    # The snapshot lane accounts its drops through the parity envelope; without
    # an accountant here the CLI lane silently shed the same rows (exited PIDs,
    # recycled PIDs, capped lanes) with nothing to say so. Same projection, same
    # accounting.
    accountant = ProjectionAccountant("running_work")
    projection = build_running_work(accountant)
    rows = [row for row in projection["rows"] if not kind or row.get("kind") == kind]
    rows = _sort_rows(rows, getattr(args, "sort", None))

    limit = getattr(args, "limit", None)
    truncated = False
    if isinstance(limit, int) and limit > 0 and len(rows) > limit:
        rows = rows[:limit]
        truncated = True

    envelope = _list_envelope("running_work", rows, truncated=truncated)
    # Per-source health rides the SAME envelope as the rows, never a separate
    # call: a consumer that reads the rows without the health block would read
    # an unreadable lane as "nothing running", which is the one answer this
    # projection exists to make impossible.
    envelope["sources"] = projection["sources"]
    envelope["counts"] = projection["counts"]
    # Machine-local context, carried in its OWN block rather than folded into a
    # lane's health prose. It is what tells an operator staring at an empty list
    # WHICH home the projection read — the difference between "nothing is
    # running" and "nothing is running over there". Not contract: a consumer
    # renders it, never branches on it.
    #
    # Attached only when the projection published it, and never defaulted. This
    # block is a DIAGNOSTIC, so it must not be able to take the verb down (a
    # required-key read here would turn a missing diagnostic into a dead
    # `work list`), and it must not be fabricated either — a producer that
    # stopped publishing it should be visible by its absence, not papered over
    # with an invented home.
    ambient = projection.get("ambient")
    if isinstance(ambient, dict):
        envelope["ambient"] = ambient
    envelope["completeness"] = accountant.summary()
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def _cmd_work_peek(args) -> int:
    from agent_runtime.running_work import peek_work

    payload = peek_work(str(getattr(args, "work_id", "") or ""))
    if not payload.get("found"):
        _print_stage42(
            _error_envelope(
                "invalid_request" if payload.get("error") == "malformed_work_id" else "not_found",
                payload.get("error") or "no running work with that id",
                safe_details={"work_id": payload.get("work_id")},
            ),
            args=args,
            default_output="json",
        )
        return ERROR_EXIT_CODES[
            "invalid_request" if payload.get("error") == "malformed_work_id" else "not_found"
        ]
    _print_stage42(
        _object_envelope("work_peek", payload), args=args, default_output="json"
    )
    return 0


def _cmd_work_cancel(args) -> int:
    from datetime import datetime, timezone

    from agent_runtime.running_work import cancel_work, find_work_row

    work_id = str(getattr(args, "work_id", "") or "")
    row = find_work_row(work_id)
    if row is None:
        _print_stage42(
            _error_envelope(
                "not_found",
                "no running work with that id",
                safe_details={"work_id": work_id},
            ),
            args=args,
            default_output="json",
        )
        return ERROR_EXIT_CODES["not_found"]

    # Replay guard. A cancel issued BEFORE this work started cannot have been
    # aimed at it: work ids are stable per spawn, so a retried/queued command
    # arriving after the original target died and a new one took its place would
    # otherwise kill the wrong thing. Superseding is the same ruling the active
    # realm/workspace writes make with --issued-at.
    issued_at = str(getattr(args, "issued_at", None) or "").strip()
    started_at = str(row.get("started_at") or "")
    if issued_at and started_at:
        def _epoch(text: str):
            """Epoch seconds for an ISO stamp; naive input is read as UTC.

            Every `started_at` on this wire carries an offset — the projection
            anchors the process registry's naive LOCAL stamps at its boundary
            precisely so this comparison cannot be made against two different
            frames of reference. Before that, a naive local stamp read as UTC
            landed hours in the FUTURE in any UTC-plus timezone, so a
            legitimate cancel issued seconds ago compared as "earlier than the
            work started" and was refused `stale_revision`. The naive branch
            stays as a defensive fallback for a caller-supplied `--issued-at`
            written without an offset, where UTC is the documented reading.
            """

            raw = text[:-1] + "+00:00" if text.endswith("Z") else text
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()

        issued_epoch, started_epoch = _epoch(issued_at), _epoch(started_at)
        if issued_epoch is not None and started_epoch is not None and issued_epoch < started_epoch:
            _print_stage42(
                _error_envelope(
                    "stale_revision",
                    "cancel was issued before this work started; superseded",
                    safe_details={
                        "work_id": work_id,
                        "issued_at": issued_at,
                        "started_at": started_at,
                    },
                ),
                args=args,
                default_output="json",
            )
            return ERROR_EXIT_CODES["stale_revision"]

    target = _work_target_details(row)
    if not _require_yes(
        args,
        message=(
            f"Cancelling {target['kind']} work {target['label']!r} "
            f"(pid={target['pid']}) requires --yes."
        ),
        safe_details=target,
    ):
        return ERROR_EXIT_CODES["confirmation_required"]

    if getattr(args, "dry_run", False):
        _print_stage42(
            _object_envelope(
                "work_cancel",
                {"dry_run": True, "would_cancel": target, "cancelled": False},
            ),
            args=args,
            default_output="json",
        )
        return 0

    result = cancel_work(work_id, reason=str(getattr(args, "reason", "") or "operator_cancel"))
    if result.get("status") != "cancelled":
        code = str(result.get("code") or "internal_error")
        _print_stage42(
            _error_envelope(
                code,
                str(result.get("detail") or f"cancel refused: {code}"),
                safe_details={**target, "detail": result.get("detail")},
            ),
            args=args,
            default_output="json",
        )
        return ERROR_EXIT_CODES.get(code, 1)

    _print_stage42(
        # ``status``/``code``/``kind`` are stripped from the spread: the first
        # two are already expressed by the exit code, and ``kind`` would
        # overwrite the envelope's own kind discriminator.
        _object_envelope(
            "work_cancel",
            {
                "cancelled": True,
                "target": target,
                **{
                    key: value
                    for key, value in result.items()
                    if key not in {"status", "code", "kind"}
                },
            },
        ),
        args=args,
        default_output="json",
    )
    return 0
