"""Runtime situational HUD — the single projection the operator's Mission Control
runtime HUD strip and the agent's chat turn both render, so operator and agent
reason from the identical picture ("parity so the AI and I are on the same page").

The launcher `MissionRuntimeHudStrip` shows the operator: the daemon pulse
(state · loop · beat · next-wake), the scope (realm · workspace), the bound
mission (title · state · threads), the selected lane (identity · liveness ·
steer handle), and the on-level roster.
Historically none of that reached the model: the mission-chat turn composed only
identity + rules + optional surface/skill prompts, so the agent was blind to
everything the operator saw.

This module is the one authority. `resolve_situational_hud` assembles the typed
snapshot from already-loaded runtime facts (pure — no I/O, unit-testable);
`render_situational_hud_block` renders it into the compact ``## Runtime Situation``
prompt block injected into the chat turn. The snapshot exposes the same dict on
each per-instance prompt context so the launcher renders exactly what is fed.

The stage/QA-gate ``mission_hud`` slice is GONE (S19). Mission context is now
limited to the persisted lane ``goal_id`` and the count of addressable lanes
sharing it; retired task-store title/state projections are not reconstructed.

Two delivery lanes, one authority
---------------------------------
The HUD has a HASHED BODY and a VOLATILE TAIL, and which one a fact rides is a
contract, not a style choice:

* **Body** — `render_situational_hud_block`. Stable facts (scope, mission, lane,
  steering, roster). Hashed into `situational_hud_revision`, so an unchanged
  picture is delivered as a compact ``unchanged`` stub instead of a re-snapshot.
* **Tail** — `render_runtime_context_envelope(volatile_content=…)`, emitted on
  EVERY delivery (snapshot, unchanged, and unavailable alike). Facts that must
  be true THIS turn: the wall budget (`turn_budget.render_turn_budget_line`),
  the MCP admission denials (`mcp_admission.render_mcp_admission_line`), and
  this lane's capability account (`render_capability_block` — what the chat-lane
  cost policy dropped and what the terminal envelope will refuse). Which lane a
  fact rides is declared ONCE, as ``volatile`` on its :class:`HudField` row in
  ``HUD_FIELDS``; the revision hash and the body renderer both derive from that
  one declaration (:func:`stable_hud_fields`), so a cached body cannot show a
  stale countdown or a stale capability claim.

The tail itself is composed by ``agent_runtime.volatile_tail``: contributors
register by name with a byte budget, and an over-budget contribution is
truncated or dropped WITH an in-band note plus a typed accounting row — never
silently.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .chat_lane_toolsets import DROP_KIND_TOOL, DROP_KIND_TOOLSET
from .models import looks_like_persona_instance_id

# Bound the roster so a large level cannot bloat every chat turn. The operator's
# widget wraps chips; the fed block lists names and notes the overflow count.
SITUATIONAL_HUD_ROSTER_CAP = 16

# Bound each capability list for the same reason the roster is bounded: the
# capability block rides EVERY turn, so a policy that later widens the excluded
# toolset set — or a command-class taxonomy that grows past seven — must not
# silently turn two lines into a wall. Overflow is counted, never dropped
# without saying so.
SITUATIONAL_HUD_CAPABILITY_CAP = 8

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


# HUD key for this lane's capability account — what the chat-lane cost policy
# removed from this turn, and what the terminal safety envelope will refuse.
CAPABILITY_HUD_KEY = "capability"


@dataclass(frozen=True, slots=True)
class HudField:
    """One HUD key, and the ONE declaration of which delivery lane it rides.

    ``volatile`` is stated here and nowhere else. Both consumers of the
    body/tail split derive from it — :func:`situational_hud_revision` excludes
    volatile fields from the hash, and :func:`render_situational_hud_block`
    renders from :func:`stable_hud_fields` — so a volatile fact CANNOT reach the
    hashed body even if a later edit tries to render it there. The predecessor
    convention (a ``_VOLATILE_HUD_KEYS`` frozenset, plus a hand-written promise
    in the body renderer's docstring that it would never touch those keys) put
    the declaration in one place and the enforcement in none.
    """

    key: str
    volatile: bool
    summary: str = ""


#: The declared HUD field roster. Adding a key to the HUD means adding a row
#: here; ``tests/agent_runtime/test_runtime_hud_field_contract.py`` fails a HUD
#: that emits an undeclared key, so the roster cannot silently fall behind.
#:
#: The two volatile rows are volatile for two INDEPENDENT reasons that end at
#: the same contract:
#:
#: * ``turn_budget`` changes on every single turn by construction (a wall-clock
#:   countdown), so hashing it would force a full re-snapshot of the whole stable
#:   HUD block every turn and defeat the snapshot/unchanged delivery contract.
#: * ``capability`` is mostly stable but must be true on EVERY turn regardless of
#:   delivery. A cached ``unchanged`` stub — and, worse, an ``unavailable``
#:   delivery, which drops the body entirely — would leave an agent believing it
#:   still has a capability this turn dropped, or leave a refusal unexplained.
#:   Same reasoning, and the same lane, as the MCP admission line
#:   (``mcp_admission.render_mcp_admission_line``): a capability claim that can go
#:   stale in a cache is worse than no claim at all.
HUD_FIELDS: tuple[HudField, ...] = (
    HudField("preview", volatile=False, summary="marks the dict as the fed HUD projection"),
    HudField("scope", volatile=False, summary="realm · workspace"),
    HudField("lane", volatile=False, summary="this agent's identity/role/mode"),
    HudField("mission", volatile=False, summary="bound goal, state, thread count"),
    HudField("roster", volatile=False, summary="addressable on-level agents"),
    HudField("steering", volatile=False, summary="who steers this lane, and whom it steers"),
    HudField("board", volatile=False, summary="advisory Mission Board digest"),
    HudField("turn_budget", volatile=True, summary="wall-clock window left on THIS turn"),
    HudField(
        CAPABILITY_HUD_KEY,
        volatile=True,
        summary="capability drops + terminal-envelope grants/refusals for THIS turn",
    ),
)

_HUD_FIELD_BY_KEY: dict[str, HudField] = {field.key: field for field in HUD_FIELDS}


def hud_field(key: str) -> HudField | None:
    """The declaration for one HUD key, or ``None`` when it is undeclared."""

    return _HUD_FIELD_BY_KEY.get(str(key))


def is_volatile_hud_key(key: str) -> bool:
    """Whether a key rides the always-emitted tail instead of the hashed body.

    An UNDECLARED key is treated as stable. That direction is the safe one: an
    undeclared key stays in the hash, so the worst case is an extra re-snapshot.
    Defaulting the other way would silently drop a new fact out of the revision
    and let a cached body go stale — the exact failure the split exists to
    prevent.
    """

    field = _HUD_FIELD_BY_KEY.get(str(key))
    return bool(field and field.volatile)


def volatile_hud_keys() -> frozenset[str]:
    """The declared volatile key set (derived, never a second hand-kept list)."""

    return frozenset(field.key for field in HUD_FIELDS if field.volatile)


def stable_hud_fields(hud: dict[str, Any] | None) -> dict[str, Any]:
    """``hud`` with every declared-volatile field removed.

    THE single derivation point of the body/tail split. Both the revision hash
    and the body renderer read this, so "volatile" is decided once, in
    :data:`HUD_FIELDS`, and enforced structurally in both places.
    """

    if not isinstance(hud, dict):
        return {}
    return {key: value for key, value in hud.items() if not is_volatile_hud_key(key)}


def situational_hud_revision(hud: dict[str, Any] | None) -> str:
    """Return a stable revision for the exact runtime snapshot fed this turn.

    Fields declared ``volatile`` in :data:`HUD_FIELDS` are excluded: the revision
    describes the STABLE picture, so a per-turn countdown never invalidates it.
    """

    if not isinstance(hud, dict) or not hud:
        return "hud_unavailable"
    stable = stable_hud_fields(hud)
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
    roster: Iterable[Any],
) -> dict[str, Any]:
    goal_id = _text(getattr(instance, "goal_id", None))
    if goal_id is None:
        return {}
    mission = {
        "goal_id": goal_id,
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
    board: dict[str, Any] | None = None,
    turn_budget: dict[str, Any] | None = None,
    capability: dict[str, Any] | None = None,
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

    mission = _mission_block(instance, roster=roster)
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

    # This lane's capability account (``resolve_capability_block``). Volatile by
    # contract for the reasons recorded on its ``HUD_FIELDS`` row, so — exactly
    # like ``turn_budget`` — it is excluded from the revision hash and fed to the
    # agent through the envelope's always-emitted tail rather than the cached
    # body. It lives on the dict so the operator's CONTEXT peek and the
    # observability row see the SAME account the agent was told.
    if isinstance(capability, dict) and capability:
        hud[CAPABILITY_HUD_KEY] = capability

    return hud


def render_situational_hud_block(hud: dict[str, Any]) -> str:
    """Render the fed ``## Runtime Situation`` prompt block from a resolved HUD.

    Kept deliberately compact and read-only in tone: the block is situational
    awareness the operator also sees, not an instruction to act. Returns an empty
    string when there is nothing to say.

    This is the HASHED body, and it renders from :func:`stable_hud_fields` — the
    same derivation :func:`situational_hud_revision` hashes. A field declared
    ``volatile`` in :data:`HUD_FIELDS` is therefore not merely "not rendered
    here by convention": it is not present in the dict this function reads, so
    it CANNOT be rendered here. Volatile facts ride the envelope's
    always-emitted tail instead; putting one behind the revision hash would
    re-snapshot the whole HUD every turn (``turn_budget``) or let a cached body
    show a stale claim (``capability``)."""

    if not isinstance(hud, dict) or not hud:
        return ""
    hud = stable_hud_fields(hud)
    if not hud:
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

    return "\n".join(lines)


def _capped(names: Iterable[Any]) -> tuple[list[str], int]:
    """Order-preserving dedupe, capped; returns the kept names and the overflow."""

    kept: list[str] = []
    seen: set[str] = set()
    for item in names or ():
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        kept.append(text)
    if len(kept) <= SITUATIONAL_HUD_CAPABILITY_CAP:
        return kept, 0
    return kept[:SITUATIONAL_HUD_CAPABILITY_CAP], len(kept) - SITUATIONAL_HUD_CAPABILITY_CAP


def _names(names: Iterable[Any]) -> str:
    kept, overflow = _capped(names)
    text = ", ".join(kept)
    return f"{text} (+{overflow} more)" if overflow else text


def resolve_capability_block(
    *,
    drops: Iterable[Any] = (),
    envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble this lane's capability account: what was DROPPED, what is REFUSED.

    Pure, like :func:`resolve_situational_hud`: both inputs are already-resolved
    facts produced by their own authorities, and this module renders them — it
    resolves no policy of its own.

    * ``drops`` — :class:`agent_runtime.chat_lane_toolsets.ChatLaneDrop` values
      from ``persona_runtime.chat_lane_capability_drops`` (G5). Each carries the
      exact root-config key that un-excludes it, which is the whole point of the
      row: the reader can act without reading source.
    * ``envelope`` — the side-effect-free
      ``terminal_envelope.explain_terminal_envelope`` view. Its ``refused`` set
      is split into the operator-grantable classes and the hard floor, because
      telling an agent to ask for a grant that cannot exist would be a new lie
      (the same reasoning that made ``envelope_command_not_grantable`` a
      distinct refusal code).

    Returns ``{}`` when there is genuinely nothing to account for — an
    ``unbounded`` turn drops nothing, and an ungoverned lane refuses nothing, so
    neither pays a line. Honest silence, not a noise block.
    """

    block: dict[str, Any] = {}

    toolsets: list[str] = []
    tools: list[str] = []
    restorable: list[str] = []
    for drop in drops or ():
        subject = str(getattr(drop, "subject", "") or "").strip()
        if not subject:
            continue
        kind = getattr(drop, "kind", None)
        if kind == DROP_KIND_TOOLSET:
            toolsets.append(subject)
        elif kind == DROP_KIND_TOOL:
            tools.append(subject)
        else:
            continue
        key = str(getattr(drop, "restorable_via", "") or "").strip()
        if key and key not in restorable:
            restorable.append(key)
    if toolsets:
        block["toolsets_dropped"] = toolsets
    if tools:
        block["tools_dropped"] = tools
    if restorable:
        # One persona ⇒ one key in practice; the list shape keeps the block
        # honest if a future dropper ever restores through a different setting.
        block["restorable_via"] = restorable

    if isinstance(envelope, dict) and envelope.get("governed"):
        grantable = {
            str(name) for name in (envelope.get("grantable_command_classes") or ())
        }
        refused = [str(name) for name in (envelope.get("refused") or ())]
        granted = [str(name) for name in (envelope.get("granted") or ())]
        issues = [row for row in (envelope.get("grant_issues") or ()) if isinstance(row, dict)]
        refused_grantable = [name for name in refused if name in grantable]
        refused_hard_floor = [name for name in refused if name not in grantable]
        if granted or refused or issues:
            envelope_block: dict[str, Any] = {
                "lane": str(envelope.get("lane") or "").strip(),
                "role": str(envelope.get("role") or "").strip(),
                "config_key": str(envelope.get("config_key") or "").strip(),
            }
            if granted:
                envelope_block["granted"] = granted
            if refused_grantable:
                envelope_block["refused_grantable"] = refused_grantable
            if refused_hard_floor:
                envelope_block["refused_hard_floor"] = refused_hard_floor
            if issues:
                envelope_block["grant_issues"] = issues
            block["envelope"] = envelope_block

    return block


def render_capability_block(capability: dict[str, Any] | None) -> str:
    """Render the agent-visible capability lines for the volatile envelope tail.

    Two bullets at most, in the same list grammar as
    ``turn_budget.render_turn_budget_line`` and
    ``mcp_admission.render_mcp_admission_line``, because they ride the same tail.

    This is the whole point of the slice: the drops and the envelope refusals
    were already computed and already typed, and the agent still could not SEE
    them — so "I have no terminal" read to the model as an unexplained absence
    and it improvised (the failure class the MCP row was written to retire,
    recurring; see the lane gap audit §6 / G5). Each line therefore states the
    fact, names the ONE authority that could change it, and closes the
    improvisation door explicitly.

    Returns ``""`` for an empty account, so a lane with nothing to report pays
    nothing.
    """

    if not isinstance(capability, dict) or not capability:
        return ""

    lines: list[str] = []

    toolsets = capability.get("toolsets_dropped") or []
    tools = capability.get("tools_dropped") or []
    if toolsets or tools:
        parts: list[str] = []
        if toolsets:
            parts.append(f"toolset{'' if len(toolsets) == 1 else 's'} {_names(toolsets)}")
        if tools:
            parts.append(f"tool{'' if len(tools) == 1 else 's'} {_names(tools)}")
        keys = capability.get("restorable_via") or []
        restore = (
            f" Only an OPERATOR can restore one, with `{_names(keys)}` in the ROOT "
            "config.yaml."
            if keys
            else ""
        )
        lines.append(
            f"- Dropped on this lane: {' · '.join(parts)}. By design — a per-turn "
            "schema-cost cut applied AFTER role and permission resolution, so it is "
            "NOT a permission problem and no permission mode you can reach restores "
            f"it.{restore} Report the absence plainly; do not hunt for a mode and do "
            "not improvise a workaround."
        )

    envelope = capability.get("envelope") if isinstance(capability.get("envelope"), dict) else {}
    if envelope:
        who = ", ".join(
            part
            for part in (
                f"role {envelope['role']}" if envelope.get("role") else "",
                f"lane {envelope['lane']}" if envelope.get("lane") else "",
            )
            if part
        )
        bits: list[str] = []
        granted = envelope.get("granted") or []
        bits.append(f"granted {_names(granted)}" if granted else "no class granted")
        refused_grantable = envelope.get("refused_grantable") or []
        if refused_grantable:
            key = envelope.get("config_key") or ""
            via = f" — operator-grantable via `{key}`" if key else " — operator-grantable"
            bits.append(f"refused {_names(refused_grantable)}{via}")
        refused_hard_floor = envelope.get("refused_hard_floor") or []
        if refused_hard_floor:
            bits.append(
                f"hard floor no config lifts: {_names(refused_hard_floor)}"
            )
        issues = envelope.get("grant_issues") or []
        if issues:
            bits.append(
                f"{len(issues)} grant-config issue"
                f"{'' if len(issues) == 1 else 's'} — the stanza grants less than it reads"
            )
        lines.append(
            f"- Terminal envelope ({who}): " + "; ".join(bits) + ". A refusal is "
            "final for this turn — relay it to the operator, never retry, reword or "
            "split the command."
        )

    return "\n".join(lines)


def capability_block_for_persona(
    persona: Any,
    *,
    session_id: str | None = None,
    permission_mode: str | None = None,
    lane: str | None = None,
) -> dict[str, Any]:
    """Chat-side convenience: resolve both capability accounts for one persona.

    The wrapper twin of :func:`situational_hud_for_instance` — it does the
    lookups a single mission-chat turn needs, then calls the same pure
    :func:`resolve_capability_block` (one authority). Both halves resolve the
    SAME functions the turn itself resolves (``chat_lane_capability_drops`` is
    the accounting twin of ``_enabled_toolsets_for_chat``;
    ``explain_terminal_envelope`` reads the same grants
    ``envelope_decision`` will), so what the agent is told and what the runtime
    then does cannot disagree.

    The two halves degrade INDEPENDENTLY: a fault resolving the drops must not
    blank the envelope posture, and vice versa. Best-effort overall — the
    capability account decorates a turn, it never blocks one.
    """

    if persona is None:
        return {}

    drops: tuple[Any, ...] = ()
    try:
        # Deferred: ``persona_runtime`` pulls the runtime graph (and imports this
        # module's siblings), so a module-level import here would be circular.
        from .persona_runtime import chat_lane_capability_drops

        drops = chat_lane_capability_drops(
            persona, session_id=session_id, permission_mode=permission_mode
        )
    except Exception:
        drops = ()

    envelope: dict[str, Any] | None = None
    try:
        from .personas import role_from_persona
        from .terminal_envelope import LANE_MISSION_CHAT, explain_terminal_envelope

        try:
            role = str(role_from_persona(persona))
        except Exception:
            role = str(getattr(persona, "role", "") or "")
        envelope = explain_terminal_envelope(
            role=role, lane=str(lane or "").strip() or LANE_MISSION_CHAT
        )
    except Exception:
        envelope = None

    return resolve_capability_block(drops=drops, envelope=envelope)


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
    turn_budget: dict[str, Any] | None = None,
    capability: dict[str, Any] | None = None,
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
        from .store import RealmStore, WorkspaceStore

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

        return resolve_situational_hud(
            instance,
            daemon=None,
            realm=realm,
            workspace=workspace,
            roster=scoped_roster,
            identity_roster=identity_roster,
            board=_board_digest_for_workspace(scope_workspace_id),
            turn_budget=turn_budget,
            capability=capability,
        )
    except Exception:
        return {}
