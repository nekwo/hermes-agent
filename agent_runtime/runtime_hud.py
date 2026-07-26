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

import hashlib
import json
import re
from typing import Any, Iterable

from .models import looks_like_persona_instance_id

# Bound the roster so a large level cannot bloat every chat turn. The operator's
# widget wraps chips; the fed block lists names and notes the overflow count.
SITUATIONAL_HUD_ROSTER_CAP = 16

RUNTIME_CONTEXT_DELIVERY_SNAPSHOT = "snapshot"
RUNTIME_CONTEXT_DELIVERY_UNCHANGED = "unchanged"
RUNTIME_CONTEXT_DELIVERY_UNAVAILABLE = "unavailable"
_RUNTIME_CONTEXT_ENVELOPE_RE = re.compile(
    r'(?:\n\n)?<runtime_context context_id="(?P<context_id>ctx_[a-zA-Z0-9_-]+)" '
    r'revision="(?P<revision>hud_[a-f0-9]+|hud_unavailable)" '
    r'delivery="(?P<delivery>snapshot|unchanged|unavailable)">\n'
    r'(?P<body>.*?)\n</runtime_context>\s*\Z',
    re.DOTALL,
)
# Operator-turn skill-preload envelope. The required/queued skill preload rides
# the operator's user turn for the same prompt-cache reason the HUD does (see
# ``_mission_chat_user_message``), so — like the HUD — it needs a structural
# envelope the transcript projection can strip. Without one, the projection
# renders the whole skill body as operator-authored text (live 2026-07-23:
# "message launcher dev say hi" displayed with the full harness-runtime-model
# skill appended). Same grammar rules as the runtime-context envelope: strict
# attribute charset, well-formed-only, end-anchored extraction.
#
# Delivery mirrors the runtime-context contract: the full body is a
# ``snapshot`` sent only when no matching snapshot survives in the effective
# native lineage; otherwise a compact ``unchanged`` stub re-asserts the active
# skills. Required-preload skills ride EVERY chat turn, so without this a
# skill-bearing persona pays the full body per turn. ``revision``/``delivery``
# are optional in the extraction grammar so rows persisted by the envelope's
# first revision (no attributes) keep stripping.
SKILL_PRELOAD_DELIVERY_SNAPSHOT = "snapshot"
SKILL_PRELOAD_DELIVERY_UNCHANGED = "unchanged"
_SKILL_PRELOAD_ENVELOPE_RE = re.compile(
    r'(?:\n\n)?<skill_preload skills="(?P<skills>[a-zA-Z0-9_.:+-]*(?:,[a-zA-Z0-9_.:+-]+)*)"'
    r'(?: revision="(?P<revision>skills_[a-f0-9]+)" delivery="(?P<delivery>snapshot|unchanged)")?>\n'
    r'(?P<body>.*?)\n</skill_preload>\s*\Z',
    re.DOTALL,
)
_SKILL_PRELOAD_NAME_RE = re.compile(r"^[a-zA-Z0-9_.:+-]+$")


# HUD keys deliberately EXCLUDED from the revision hash. These change on every
# single turn by construction (a wall-clock countdown), so hashing them would
# force a full re-snapshot of the whole stable HUD block every turn and defeat
# the snapshot/unchanged delivery contract entirely. They ride the envelope's
# always-emitted volatile tail instead (see ``render_runtime_context_envelope``).
_VOLATILE_HUD_KEYS = frozenset({"turn_budget"})


def situational_hud_revision(hud: dict[str, Any] | None) -> str:
    """Return a stable revision for the exact runtime snapshot fed this turn.

    Volatile keys (:data:`_VOLATILE_HUD_KEYS`) are excluded: the revision
    describes the STABLE picture, so a per-turn countdown never invalidates it.
    """

    if not isinstance(hud, dict) or not hud:
        return "hud_unavailable"
    stable = {key: value for key, value in hud.items() if key not in _VOLATILE_HUD_KEYS}
    if not stable:
        return "hud_unavailable"
    canonical = json.dumps(
        stable,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8", errors="replace")
    return "hud_" + hashlib.sha256(canonical).hexdigest()[:16]


def extract_runtime_context_envelope(
    content: Any,
) -> tuple[str, dict[str, str] | None]:
    """Strip only our final, well-formed runtime envelope from a user row.

    Anchoring at the end is intentional: operator-authored text which happens to
    mention the tag remains ordinary transcript content.
    """

    text = content if isinstance(content, str) else str(content or "")
    match = _RUNTIME_CONTEXT_ENVELOPE_RE.search(text)
    if match is None:
        return text, None
    return text[: match.start()].rstrip(), {
        "context_id": match.group("context_id"),
        "revision": match.group("revision"),
        "delivery": match.group("delivery"),
    }


def runtime_context_delivery(
    native_history: Iterable[dict[str, Any]] | None,
    revision: str,
) -> str:
    """Choose snapshot vs delta without relying on resident-process memory.

    A full snapshot is resent when no matching snapshot remains in the effective
    native lineage. That makes cold resume and post-compression recovery safe.
    """

    if revision == "hud_unavailable":
        return RUNTIME_CONTEXT_DELIVERY_UNAVAILABLE
    for row in reversed(list(native_history or ())):
        if not isinstance(row, dict) or str(row.get("role") or "").lower() != "user":
            continue
        _, metadata = extract_runtime_context_envelope(row.get("content"))
        if (
            metadata is not None
            and metadata.get("revision") == revision
            and metadata.get("delivery") == RUNTIME_CONTEXT_DELIVERY_SNAPSHOT
        ):
            return RUNTIME_CONTEXT_DELIVERY_UNCHANGED
    return RUNTIME_CONTEXT_DELIVERY_SNAPSHOT


def render_runtime_context_envelope(
    *,
    context_id: str,
    revision: str,
    delivery: str,
    situational_hud_content: str | None,
    volatile_content: str | None = None,
) -> str:
    """Render the compact per-turn envelope appended to the operator message.

    ``volatile_content`` (today: the remaining wall-budget line) is emitted on
    EVERY delivery — snapshot, unchanged, and unavailable alike. That is the
    whole point of separating it from the hashed body: a cached "unchanged"
    stub would otherwise show the agent a stale countdown, which is worse than
    showing none, and folding it into the body would re-snapshot the entire HUD
    every turn.
    """

    if delivery == RUNTIME_CONTEXT_DELIVERY_SNAPSHOT:
        body = (situational_hud_content or "").strip()
        if not body:
            delivery = RUNTIME_CONTEXT_DELIVERY_UNAVAILABLE
            revision = "hud_unavailable"
    elif delivery == RUNTIME_CONTEXT_DELIVERY_UNCHANGED:
        body = (
            "Runtime Situation unchanged from the most recent full snapshot "
            f"for revision {revision}."
        )
    else:
        delivery = RUNTIME_CONTEXT_DELIVERY_UNAVAILABLE
        revision = "hud_unavailable"
        body = "Runtime Situation unavailable for this turn."
    volatile = (volatile_content or "").strip()
    if volatile:
        body = f"{body}\n{volatile}" if body else volatile
    return (
        f'<runtime_context context_id="{context_id}" revision="{revision}" '
        f'delivery="{delivery}">\n{body}\n</runtime_context>'
    )


def skill_preload_revision(skill_preload_content: str | None) -> str:
    """Return a stable revision for the exact preload content built this turn.

    Hashing the CONTENT (not the name list) means a skill edited on disk — or a
    changed queued/required set — re-snapshots, exactly like a changed runtime
    HUD does.
    """

    body = (skill_preload_content or "").strip()
    if not body:
        return "skills_unavailable"
    return "skills_" + hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()[:16]


def skill_preload_delivery(
    native_history: Iterable[dict[str, Any]] | None,
    revision: str,
) -> str:
    """Choose snapshot vs unchanged without relying on resident-process memory.

    Mirror of :func:`runtime_context_delivery`: the full preload body is resent
    when no matching snapshot remains in the effective native lineage, so cold
    resume and post-compression recovery stay safe. History rows carry the skill
    envelope BEFORE the trailing runtime-context envelope, so the scan strips
    the HUD envelope first.
    """

    if revision == "skills_unavailable":
        return SKILL_PRELOAD_DELIVERY_SNAPSHOT
    for row in reversed(list(native_history or ())):
        if not isinstance(row, dict) or str(row.get("role") or "").lower() != "user":
            continue
        remainder, _ = extract_runtime_context_envelope(row.get("content"))
        _, metadata = extract_skill_preload_envelope(remainder)
        if (
            metadata is not None
            and metadata.get("revision") == revision
            and metadata.get("delivery") == SKILL_PRELOAD_DELIVERY_SNAPSHOT
        ):
            return SKILL_PRELOAD_DELIVERY_UNCHANGED
    return SKILL_PRELOAD_DELIVERY_SNAPSHOT


def render_skill_preload_envelope(
    *,
    skill_names: Iterable[str] | None,
    skill_preload_content: str | None,
    revision: str | None = None,
    delivery: str = SKILL_PRELOAD_DELIVERY_SNAPSHOT,
) -> str:
    """Wrap the per-turn skill preload in its structural envelope.

    Returns ``""`` when there is nothing to preload, so callers can keep the
    "join non-empty parts" composition unchanged. Names that fail the strict
    attribute charset are dropped from the attribute (the body still carries
    the full preload text) — the attribute exists for projection metadata and
    must never break extraction.

    ``delivery == "unchanged"`` swaps the full body for a compact stub that
    re-asserts the active skills against the earlier snapshot ``revision`` —
    the snapshot row itself stays in the native lineage the model reads.
    """

    body = (skill_preload_content or "").strip()
    if not body:
        return ""
    names = ",".join(
        name
        for name in (str(item or "").strip() for item in (skill_names or ()))
        if name and _SKILL_PRELOAD_NAME_RE.fullmatch(name)
    )
    resolved_revision = revision or skill_preload_revision(body)
    if delivery == SKILL_PRELOAD_DELIVERY_UNCHANGED:
        body = (
            "Skill instructions unchanged from the full snapshot for revision "
            f"{resolved_revision} earlier in this conversation. The listed "
            "skills remain active for this session."
        )
    else:
        delivery = SKILL_PRELOAD_DELIVERY_SNAPSHOT
    return (
        f'<skill_preload skills="{names}" revision="{resolved_revision}" '
        f'delivery="{delivery}">\n{body}\n</skill_preload>'
    )


def extract_skill_preload_envelope(
    content: Any,
) -> tuple[str, dict[str, Any] | None]:
    """Strip only our final, well-formed skill-preload envelope from a user row.

    Mirrors :func:`extract_runtime_context_envelope`: end-anchored so operator
    text that merely mentions the tag stays ordinary transcript content. Run it
    AFTER the runtime-context extraction — composition order is
    ``message · skill_preload · runtime_context``, so the skill envelope is
    end-anchored only once the HUD envelope has been stripped.
    """

    text = content if isinstance(content, str) else str(content or "")
    match = _SKILL_PRELOAD_ENVELOPE_RE.search(text)
    if match is None:
        return text, None
    skills = [name for name in match.group("skills").split(",") if name]
    metadata: dict[str, Any] = {"skills": skills}
    if match.group("revision") is not None:
        metadata["revision"] = match.group("revision")
        metadata["delivery"] = match.group("delivery")
    return text[: match.start()].rstrip(), metadata


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
    ``missionAgentInstanceParentIds`` so both ends agree who steers whom.

    Raw refs — a ref may name an instance id, a persona id, or a role, resolved
    downstream in :func:`_steering_block`. Principals that resolve to nobody AND
    are not instance-shaped (the operator) are dropped there, not here, so the
    intentional persona/role resolution is preserved."""

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
        # Drop a phantom steerer: a ref that resolves to nobody AND is not even
        # instance-shaped is a non-agent principal (the operator, leaked into a
        # steering field by a legacy mint), never a real steer parent — so the
        # HUD must not narrate "steered by operator". A resolved persona/role ref,
        # or an instance-shaped-but-departed ref ("off level"), is a genuine fact
        # and is kept.
        if parent is None and not looks_like_persona_instance_id(ref):
            continue
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
    identity_roster: Iterable[Any] | None = None,
    task: Any = None,
    goal_task: Any = None,
    proof_store: Any = None,
    board: dict[str, Any] | None = None,
    turn_budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the typed situational snapshot for one lane.

    Pure: every input is an already-loaded runtime fact. Both the snapshot
    (`snapshot_prompt_observability`) and the chat caller
    (`_cmd_mission_chat_message`) resolve these and call in, so the widget and
    the model render the same projection.

    ``roster`` is the ADDRESSABLE set — the workspace-scoped list that feeds the
    "On level" advertising block and the mission thread count. ``identity_roster``
    is the FULL, unscoped list used only for identity resolution (who steers
    whom): a steerer in another workspace must still resolve to a name even
    though it is not addressable from here. It defaults to ``roster`` so existing
    callers that pass a single list keep identical behaviour.
    """

    if instance is None:
        return {}
    roster = list(roster or ())
    identity_roster = roster if identity_roster is None else list(identity_roster)
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
    # its absence is reserved for HUDs that predate steering entirely. Identity
    # resolution reads the FULL roster — a steerer/steered lane in another
    # workspace is a genuine graph fact even when it is not addressable from
    # here, so scoping must never blank out a steering name.
    hud["steering"] = _steering_block(instance, identity_roster, self_id=self_id)

    # Advisory Mission Board digest (nudge, not instruction): a one-line
    # awareness cue. Absent when there is no board or it has no open cards, so a
    # workspace with no board contributes NO line (nudged-never-forced).
    if isinstance(board, dict) and board:
        hud["board"] = board

    # Wall budget for THIS turn (``turn_budget.TurnWallBudget.hud_block``).
    # Volatile by construction, so it is excluded from the revision hash and
    # fed to the agent through the envelope's always-emitted tail rather than
    # the cached body — it lives on the dict purely so the operator's CONTEXT
    # peek and the observability row see the same number the agent was told.
    if isinstance(turn_budget, dict) and turn_budget:
        hud["turn_budget"] = turn_budget

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


def situational_hud_for_instance(
    instance: Any,
    *,
    proof_store: Any = None,
    turn_budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
        from . import workspace_scope
        from .persona_assignments import PersonaInstanceStore, is_canonical_persona_channel
        from .store import RealmStore, TaskStore, WorkspaceStore

        # The FULL roster stays available for identity (steering) resolution; the
        # ADDRESSABLE roster fed to advertising/thread-count is scoped to this
        # lane's own workspace, has runtime-global canonical plumbing rows
        # excluded (instance = in-level placement — the "On level" block lists
        # ONLY what is actually placed on this level), and has each persona's
        # surviving canonical row shadowed behind any in-scope placement. So a
        # placement in another workspace is never offered here, and an unplaced
        # persona no longer advertises its canonical row onto every level.
        identity_roster = PersonaInstanceStore().list_all()

        workspace_store = WorkspaceStore()
        realm_store = RealmStore()
        scope_workspace_id = workspace_scope.effective_workspace_id(
            instance, active_workspace_id=workspace_store.active_id()
        )
        scoped_roster = workspace_scope.addressable_roster(
            identity_roster,
            scope_workspace_id=scope_workspace_id,
            is_canonical=is_canonical_persona_channel,
        )
        # The scope line names the lane's OWN workspace when it carries a pointer
        # (fallback: the active workspace), so it matches the scoped roster.
        workspace = next(
            (
                getattr(item, "name", None)
                for item in workspace_store.list_all(include_archived=True)
                if getattr(item, "id", None) == scope_workspace_id
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
            roster=scoped_roster,
            identity_roster=identity_roster,
            task=_safe_get(getattr(instance, "current_task_id", None)),
            goal_task=_safe_get(getattr(instance, "goal_id", None)),
            proof_store=proof_store,
            board=_board_digest_for_workspace(scope_workspace_id),
            turn_budget=turn_budget,
        )
    except Exception:
        return {}


def situational_hud_content_for_instance(instance: Any, *, proof_store: Any = None) -> str:
    """Rendered-block form of `situational_hud_for_instance` (same authority);
    kept for callers that only need the fed text."""

    return render_situational_hud_block(
        situational_hud_for_instance(instance, proof_store=proof_store)
    )
