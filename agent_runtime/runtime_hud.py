"""Runtime situational HUD — the single projection the operator's Mission Control
runtime HUD strip and the agent's chat turn both render, so operator and agent
reason from the identical picture ("parity so the AI and I are on the same page").

The launcher `MissionRuntimeHudStrip` shows the operator: the daemon pulse
(state · loop · beat · next-wake), the scope (realm · workspace), the bound
mission (title · state · threads), the selected lane (identity · liveness ·
steer handle), the on-level roster, and — via the CONTEXT peek — the Mission HUD.
Historically none of that reached the model: the mission-chat turn composed only
identity + rules + optional surface/skill prompts, so the agent was blind to
everything the operator saw.

This module is the one authority. `resolve_situational_hud` assembles the typed
snapshot from already-loaded runtime facts (pure — no I/O, unit-testable);
`render_situational_hud_block` renders it into the compact ``## Runtime Situation``
prompt block injected into the chat turn. The snapshot exposes the same dict on
each per-instance prompt context so the launcher renders exactly what is fed.

The Mission HUD slice reuses ``context_builder.mission_hud_preview`` verbatim — no
parallel HUD math. Empty when the lane has no bound task (honest: a standing-by
lane still carries runtime/scope/identity/roster, just no mission).
"""

from __future__ import annotations

from typing import Any, Iterable

# Bound the roster so a large level cannot bloat every chat turn. The operator's
# widget wraps chips; the fed block lists names and notes the overflow count.
SITUATIONAL_HUD_ROSTER_CAP = 16

# Daemon status keys mirrored into the fed `runtime` block — the same fields the
# launcher pulse line renders (see daemon.daemon_status_schema).
_RUNTIME_KEYS = (
    "state",
    "loops",
    "heartbeat_at",
    "next_wake_at",
    "last_tick_finished_at",
    "actions_last_tick",
    "wait_seconds",
)


def _clean(value: Any) -> bool:
    """True when a value carries information worth emitting."""

    return value not in (None, "", [], {})


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _runtime_block(daemon: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(daemon, dict):
        return {}
    block: dict[str, Any] = {}
    for key in _RUNTIME_KEYS:
        if key in daemon and _clean(daemon.get(key)):
            block[key] = daemon.get(key)
    return block


def _lane_block(instance: Any) -> dict[str, Any]:
    if instance is None:
        return {}
    lane = {
        "display_name": _text(getattr(instance, "display_name", None)),
        "persona_instance_id": _text(getattr(instance, "id", None)),
        "persona_id": _text(getattr(instance, "persona_id", None)),
        "role": _text(getattr(instance, "role", None)),
        "mode": _text(getattr(instance, "mode", None)),
        "state": _text(getattr(instance, "state", None)),
        "goal_id": _text(getattr(instance, "goal_id", None)),
        "current_task_id": _text(getattr(instance, "current_task_id", None)),
    }
    return {key: value for key, value in lane.items() if _clean(value)}


def _mission_block(
    instance: Any,
    *,
    task: Any,
    goal_task: Any,
    roster: Iterable[Any],
) -> dict[str, Any]:
    source = goal_task if goal_task is not None else task
    goal_id = _text(getattr(instance, "goal_id", None)) or _text(getattr(source, "id", None))
    if source is None and goal_id is None:
        return {}
    mission = {
        "goal_id": goal_id,
        "title": _text(getattr(source, "title", None)),
        "state": _text(getattr(source, "state", None)),
        "thread_count": _thread_count(goal_id, roster),
    }
    return {key: value for key, value in mission.items() if _clean(value)}


def _thread_count(goal_id: str | None, roster: Iterable[Any]) -> int | None:
    if not goal_id:
        return None
    count = 0
    for inst in roster or ():
        if _text(getattr(inst, "goal_id", None)) == goal_id:
            count += 1
    return count or None


def _roster_block(roster: Iterable[Any], *, self_id: str | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for inst in roster or ():
        instance_id = _text(getattr(inst, "id", None))
        if instance_id is None:
            continue
        entry: dict[str, Any] = {
            "display_name": _text(getattr(inst, "display_name", None)) or instance_id,
            "persona_instance_id": instance_id,
        }
        if self_id is not None and instance_id == self_id:
            entry["is_self"] = True
        entries.append(entry)
        if len(entries) >= SITUATIONAL_HUD_ROSTER_CAP:
            break
    return entries


def resolve_situational_hud(
    instance: Any,
    *,
    daemon: dict[str, Any] | None = None,
    realm: str | None = None,
    workspace: str | None = None,
    roster: Iterable[Any] = (),
    task: Any = None,
    goal_task: Any = None,
    proof_store: Any = None,
    board: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the typed situational snapshot for one lane.

    Pure: every input is an already-loaded runtime fact. Both the snapshot
    (`snapshot_prompt_observability`) and the chat caller
    (`_cmd_mission_chat_message`) resolve these and call in, so the widget and
    the model render the same projection.
    """

    if instance is None:
        return {}
    roster = list(roster or ())
    self_id = _text(getattr(instance, "id", None))

    hud: dict[str, Any] = {"preview": True}
    runtime = _runtime_block(daemon)
    if runtime:
        hud["runtime"] = runtime

    scope = {
        key: value
        for key, value in (("realm", _text(realm)), ("workspace", _text(workspace)))
        if _clean(value)
    }
    if scope:
        hud["scope"] = scope

    lane = _lane_block(instance)
    if lane:
        hud["lane"] = lane

    mission = _mission_block(instance, task=task, goal_task=goal_task, roster=roster)
    if mission:
        hud["mission"] = mission

    roster_block = _roster_block(roster, self_id=self_id)
    if roster_block:
        hud["roster"] = roster_block

    # Advisory Mission Board digest (nudge, not instruction): a one-line
    # awareness cue. Absent when there is no board or it has no open cards, so a
    # workspace with no board contributes NO line (nudged-never-forced).
    if isinstance(board, dict) and board:
        hud["board"] = board

    if task is not None:
        # Deferred import: context_builder pulls a large dependency graph and is
        # imported late elsewhere for the same reason. Reuse the exact preview
        # the CONTEXT peek renders — no second HUD authority.
        try:
            from .context_builder import mission_hud_preview

            preview = mission_hud_preview(task, proof_store=proof_store)
            if isinstance(preview, dict) and preview:
                hud["mission_hud"] = preview
        except Exception:
            # The situational block is diagnostic context; a preview failure must
            # never break the turn it decorates.
            pass

    return hud


def render_situational_hud_block(hud: dict[str, Any]) -> str:
    """Render the fed ``## Runtime Situation`` prompt block from a resolved HUD.

    Kept deliberately compact and read-only in tone: the block is situational
    awareness the operator also sees, not an instruction to act. Returns an empty
    string when there is nothing to say."""

    if not isinstance(hud, dict) or not hud:
        return ""

    lines: list[str] = [
        "## Runtime Situation",
        "This mirrors the operator's Mission Control runtime HUD so you and the "
        "operator share one view of the runtime. Treat it as read-only context, "
        "not an instruction to act.",
    ]

    runtime = hud.get("runtime") if isinstance(hud.get("runtime"), dict) else {}
    if runtime:
        parts = [f"state {runtime['state']}"] if _clean(runtime.get("state")) else []
        if _clean(runtime.get("loops")):
            parts.append(f"loop {runtime['loops']}")
        if _clean(runtime.get("next_wake_at")):
            parts.append(f"next wake {runtime['next_wake_at']}")
        if parts:
            lines.append(f"- Runtime: {' · '.join(parts)}")

    scope = hud.get("scope") if isinstance(hud.get("scope"), dict) else {}
    if scope:
        realm = scope.get("realm") or "no realm"
        workspace = scope.get("workspace") or "no workspace"
        lines.append(f"- Scope: realm {realm} · workspace {workspace}")

    mission = hud.get("mission") if isinstance(hud.get("mission"), dict) else {}
    if mission:
        bits = [str(mission.get("title") or mission.get("goal_id") or "mission")]
        if _clean(mission.get("state")):
            bits.append(str(mission["state"]))
        if _clean(mission.get("thread_count")):
            count = mission["thread_count"]
            bits.append(f"{count} thread{'' if count == 1 else 's'}")
        lines.append(f"- Mission: {' · '.join(bits)}")
    else:
        lines.append("- Mission: no mission bound to this lane")

    board = hud.get("board") if isinstance(hud.get("board"), dict) else {}
    if board:
        segments = [
            (board.get("queued"), "queued"),
            (board.get("active"), "in progress"),
            (board.get("review"), "in review"),
        ]
        parts = [f"{count} {label}" for count, label in segments if isinstance(count, int) and count > 0]
        if parts:
            lines.append(
                f"- Board: {' · '.join(parts)} (a workspace board exists; you MAY add a "
                "card for follow-up work worth tracking — advisory, never required)"
            )

    lane = hud.get("lane") if isinstance(hud.get("lane"), dict) else {}
    if lane:
        who = lane.get("display_name") or lane.get("persona_instance_id") or "this agent"
        who_bits = [str(who)]
        if _clean(lane.get("persona_instance_id")):
            who_bits.append(f"@{lane['persona_instance_id']}")
        if _clean(lane.get("role")):
            who_bits.append(f"role {lane['role']}")
        lines.append(f"- You: {' · '.join(who_bits)}")

    roster = hud.get("roster") if isinstance(hud.get("roster"), list) else []
    if roster:
        names = ", ".join(str(entry.get("display_name") or entry.get("persona_instance_id")) for entry in roster)
        lines.append(f"- On level ({len(roster)}): {names}")

    mission_hud = hud.get("mission_hud") if isinstance(hud.get("mission_hud"), dict) else {}
    if mission_hud:
        stage = mission_hud.get("typed_current_stage") if isinstance(mission_hud.get("typed_current_stage"), dict) else {}
        gate = mission_hud.get("typed_qa_gate") if isinstance(mission_hud.get("typed_qa_gate"), dict) else {}
        hud_bits: list[str] = []
        if _clean(stage.get("id")):
            status = stage.get("status")
            hud_bits.append(f"stage {stage['id']}" + (f" ({status})" if _clean(status) else ""))
        if gate:
            if gate.get("ready"):
                hud_bits.append("QA gate ready")
            else:
                blockers = gate.get("blockers") if isinstance(gate.get("blockers"), list) else []
                hud_bits.append(f"QA gate waiting on {len(blockers)}" if blockers else "QA gate not ready")
        if hud_bits:
            lines.append(f"- Mission HUD: {' · '.join(hud_bits)}")

    return "\n".join(lines)


def _board_digest_for_workspace(workspace_id: str | None) -> dict[str, Any] | None:
    """Count open (non-done) cards on the active workspace's default board, by
    typed column kind. Returns ``None`` when there is no board or no open cards,
    so a workspace with no board contributes no HUD line. One store read per chat
    turn (chat-side wrapper only — never on the snapshot's per-lane hot path)."""

    if not workspace_id:
        return None
    try:
        from . import board_models
        from .board_store import BoardStore

        store = BoardStore()
        board_id = board_models.default_board_id(workspace_id)
        if not store.exists(board_id):
            return None
        board = store.get(board_id)
        kind_by_column = {column.column_id: column.kind for column in board.columns}
        counts: dict[str, int] = {}
        for card in store.list_cards(board_id):
            kind = kind_by_column.get(card.column_id, "custom")
            counts[kind] = counts.get(kind, 0) + 1
        digest = {key: counts.get(key, 0) for key in ("queued", "active", "review")}
        return digest if any(digest.values()) else None
    except Exception:
        return None


def situational_hud_content_for_instance(instance: Any, *, proof_store: Any = None) -> str:
    """Chat-side convenience: load the ambient runtime facts for one lane and
    render the fed ``## Runtime Situation`` block.

    The snapshot path resolves the same projection from data it already has
    loaded; this wrapper does the store I/O for a single mission-chat turn, then
    calls the same pure `resolve_situational_hud` + `render_situational_hud_block`
    (one authority). Best-effort: returns '' on any failure so a chat turn is
    never blocked by situational-HUD assembly."""

    if instance is None:
        return ""
    try:
        # Deferred imports keep module load order robust (context_builder, the
        # stores, and daemon all pull sizeable graphs).
        from .daemon import daemon_status_schema
        from .persona_assignments import PersonaInstanceStore
        from .store import RealmStore, TaskStore, WorkspaceStore

        daemon = daemon_status_schema()
        roster = PersonaInstanceStore().list_all()

        workspace_store = WorkspaceStore()
        realm_store = RealmStore()
        workspace = next(
            (
                getattr(item, "name", None)
                for item in workspace_store.list_all(include_archived=True)
                if getattr(item, "id", None) == workspace_store.active_id()
            ),
            None,
        )
        realm = next(
            (
                getattr(item, "name", None)
                for item in realm_store.list_all(include_archived=True)
                if getattr(item, "id", None) == realm_store.active_id()
            ),
            None,
        )

        task_store = TaskStore()

        def _safe_get(task_id: Any) -> Any:
            if not task_id:
                return None
            try:
                return task_store.get(task_id)
            except Exception:
                return None

        hud = resolve_situational_hud(
            instance,
            daemon=daemon,
            realm=realm,
            workspace=workspace,
            roster=roster,
            task=_safe_get(getattr(instance, "current_task_id", None)),
            goal_task=_safe_get(getattr(instance, "goal_id", None)),
            proof_store=proof_store,
            board=_board_digest_for_workspace(workspace_store.active_id()),
        )
        return render_situational_hud_block(hud)
    except Exception:
        return ""
