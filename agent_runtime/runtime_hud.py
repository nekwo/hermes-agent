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

def _clean(value: Any) -> bool:
    """True when a value carries information worth emitting."""

    return value not in (None, "", [], {})


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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


def _parent_refs(instance: Any) -> list[str]:
    """The steering-parent ref set of an instance (fan-in aware): the
    authoritative ``steered_by`` list, falling back to ``[spawned_by]`` for
    un-migrated records. Mirrors the launcher's
    ``missionAgentInstanceParentIds`` so both ends agree who steers whom."""

    refs = [ref for ref in (_text(item) for item in (getattr(instance, "steered_by", None) or ())) if ref]
    if refs:
        return refs
    spawned_by = _text(getattr(instance, "spawned_by", None))
    return [spawned_by] if spawned_by else []


def _resolve_parent_ref(ref: str, roster: Iterable[Any]) -> Any | None:
    """Resolve a parent ref to a roster instance. A ref may name an instance id,
    a persona id, or a role — in that precedence order (mirrors the launcher's
    ``missionAgentOwnerForSpawnedBy``)."""

    by_persona = None
    by_role = None
    for inst in roster or ():
        if _text(getattr(inst, "id", None)) == ref:
            return inst
        if by_persona is None and _text(getattr(inst, "persona_id", None)) == ref:
            by_persona = inst
        if by_role is None and _text(getattr(inst, "role", None)) == ref:
            by_role = inst
    return by_persona or by_role


def _steering_block(instance: Any, roster: Iterable[Any], *, self_id: str | None) -> dict[str, Any]:
    """Who steers this lane, and whom it steers — harness truth, fan-in aware.

    Hermes states steering only child→parent (``steered_by``/``spawned_by``), so
    the downstream set is derived by inverting the roster through the same ref
    resolution, keeping both directions consistent. Both keys are ALWAYS present:
    explicit empty lists mean genuinely standalone (the common case), which a
    consumer must be able to tell apart from "this hermes predates steering"."""

    roster = list(roster or ())
    steered_by: list[dict[str, Any]] = []
    for ref in _parent_refs(instance):
        parent = _resolve_parent_ref(ref, roster)
        entry: dict[str, Any] = {"ref": ref}
        if parent is not None:
            entry["persona_instance_id"] = _text(getattr(parent, "id", None))
            entry["display_name"] = _text(getattr(parent, "display_name", None)) or ref
        steered_by.append(entry)

    steers: list[dict[str, Any]] = []
    for inst in roster:
        instance_id = _text(getattr(inst, "id", None))
        if instance_id is None or instance_id == self_id:
            continue
        for ref in _parent_refs(inst):
            parent = _resolve_parent_ref(ref, roster)
            if parent is not None and _text(getattr(parent, "id", None)) == self_id:
                steers.append(
                    {
                        "persona_instance_id": instance_id,
                        "display_name": _text(getattr(inst, "display_name", None)) or instance_id,
                    }
                )
                break

    return {"steered_by": steered_by, "steers": steers}


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

    # Steering is always emitted (unlike the other blocks, which drop when
    # empty): an explicit empty block is the honest "standalone" answer, and
    # its absence is reserved for HUDs that predate steering entirely.
    hud["steering"] = _steering_block(instance, roster, self_id=self_id)

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

    # Shared "Name (@personainst_...)" formatter: the handle IS the address the
    # chat/steer verbs accept, so every line naming a teammate must carry it —
    # a name without its handle is visible but not actionable.
    def _handle(entry: dict[str, Any]) -> str:
        name = entry.get("display_name")
        ref = entry.get("persona_instance_id") or entry.get("ref")
        if _clean(name) and _clean(ref) and name != ref:
            return f"{name} (@{ref})"
        return f"@{ref}" if _clean(ref) else str(name or "unknown")

    steering = hud.get("steering") if isinstance(hud.get("steering"), dict) else None
    if steering is not None:
        steered_by = steering.get("steered_by") if isinstance(steering.get("steered_by"), list) else []
        steers = steering.get("steers") if isinstance(steering.get("steers"), list) else []
        if steered_by:
            lines.append(
                "- Steered by: " + ", ".join(_handle(e) for e in steered_by if isinstance(e, dict))
            )
        if steers:
            lines.append(
                "- Steers: " + ", ".join(_handle(e) for e in steers if isinstance(e, dict))
            )
        if not steered_by and not steers:
            lines.append("- Steering: standalone — no steerer, steers nobody")

    roster = hud.get("roster") if isinstance(hud.get("roster"), list) else []
    if roster:
        names = ", ".join(_handle(entry) for entry in roster if isinstance(entry, dict))
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


def situational_hud_for_instance(instance: Any, *, proof_store: Any = None) -> dict[str, Any]:
    """Chat-side convenience: load the ambient runtime facts for one lane and
    resolve the situational HUD dict.

    The snapshot path resolves the same projection from data it already has
    loaded; this wrapper does the store I/O for a single mission-chat turn, then
    calls the same pure `resolve_situational_hud` (one authority). The chat
    caller renders THIS dict into the fed block AND records it verbatim on the
    turn's observability row, so the Mission Control CONTEXT peek shows exactly
    the object that was injected — parity by construction, not by later
    re-derivation. Best-effort: returns {} on any failure so a chat turn is
    never blocked by situational-HUD assembly."""

    if instance is None:
        return {}
    try:
        # Deferred imports keep module load order robust (context_builder, the
        # stores, and daemon all pull sizeable graphs).
        from .persona_assignments import PersonaInstanceStore
        from .store import RealmStore, TaskStore, WorkspaceStore

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

        return resolve_situational_hud(
            instance,
            daemon=None,
            realm=realm,
            workspace=workspace,
            roster=roster,
            task=_safe_get(getattr(instance, "current_task_id", None)),
            goal_task=_safe_get(getattr(instance, "goal_id", None)),
            proof_store=proof_store,
            board=_board_digest_for_workspace(workspace_store.active_id()),
        )
    except Exception:
        return {}


def situational_hud_content_for_instance(instance: Any, *, proof_store: Any = None) -> str:
    """Rendered-block form of `situational_hud_for_instance` (same authority);
    kept for callers that only need the fed text."""

    return render_situational_hud_block(
        situational_hud_for_instance(instance, proof_store=proof_store)
    )
