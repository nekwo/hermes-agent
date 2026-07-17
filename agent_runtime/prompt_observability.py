from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from hermes_cli.profiles import get_profile_dir
from utils import atomic_json_write

from . import paths
from .persona_assignments import safe_assignment_text, safe_assignment_token
from .serde import to_jsonable


SAFE_PREVIEW_LIMIT = 1200
DEFAULT_CHAT_HISTORY_LIMIT = 8
MAX_WORKSPACE_AGENTS_BYTES = 128 * 1024


def _mission_chat_memory_loaded(persona: Any) -> bool:
    """Whether the mission-chat lane loads this persona's bound-profile memory.

    Mirrors ``GPTPersonaRuntime.mission_chat_reply`` (skip_memory is gated on
    ``include_profile_memory``); kept here so the observability report reflects
    the real prompt flag instead of a hardcoded assumption."""
    return bool(getattr(persona, "include_profile_memory", False))


@dataclass(frozen=True, slots=True)
class WorkspaceAgentsContext:
    """One explicitly selected workspace ``AGENTS.md`` and its safe receipt."""

    content: str | None
    receipt: dict[str, Any]


def load_workspace_agents_context(value: str | None) -> WorkspaceAgentsContext | None:
    """Load a Launcher-selected ``AGENTS.md`` without changing process CWD.

    Invalid, missing, unreadable, and oversized files produce an honest receipt
    and no injected content. Mission chat remains available in every case.
    """

    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    requested = Path(raw_value).expanduser()
    receipt: dict[str, Any] = {
        "path": str(requested),
        "name": requested.name or "AGENTS.md",
        "kind": "workspace_context",
        "source": "workspace",
        "included": False,
        "status": "invalid_path",
    }
    if not requested.is_absolute():
        return WorkspaceAgentsContext(content=None, receipt=receipt)
    if requested.name.lower() != "agents.md":
        receipt["status"] = "invalid_name"
        return WorkspaceAgentsContext(content=None, receipt=receipt)
    try:
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError):
        receipt["status"] = "missing"
        return WorkspaceAgentsContext(content=None, receipt=receipt)
    receipt["path"] = str(resolved)
    if not resolved.is_file():
        receipt["status"] = "not_a_file"
        return WorkspaceAgentsContext(content=None, receipt=receipt)
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        receipt.update(status="unreadable", error=type(exc).__name__)
        return WorkspaceAgentsContext(content=None, receipt=receipt)
    receipt["bytes"] = size
    if size > MAX_WORKSPACE_AGENTS_BYTES:
        receipt.update(status="too_large", max_bytes=MAX_WORKSPACE_AGENTS_BYTES)
        return WorkspaceAgentsContext(content=None, receipt=receipt)
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        receipt.update(status="unreadable", error=type(exc).__name__)
        return WorkspaceAgentsContext(content=None, receipt=receipt)
    content = raw.decode("utf-8", errors="replace")
    receipt.update(
        included=True,
        status="loaded",
        sha256=hashlib.sha256(raw).hexdigest().upper(),
        bytes=len(raw),
        preview=_safe_preview(content),
    )
    return WorkspaceAgentsContext(content=content, receipt=receipt)


def mission_chat_prompt_observability(
    *,
    persona: Any,
    persona_instance_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    goal_id: str | None = None,
    turn_id: str | None = None,
    surface_prompt: str | None = "",
    limiting_wrapper_active: bool = False,
    session_db: Any | None = None,
    current_message: str | None = None,
    final_model_input: dict[str, Any] | None = None,
    model_selection: dict[str, Any] | None = None,
    turn_usage: dict[str, Any] | None = None,
    trace_events: Iterable[dict[str, Any]] | None = None,
    prompt_mode: str = "normal_hermes_profile_chat",
    workspace_id: str | None = None,
    workspace_name: str | None = None,
    workspace_agents: WorkspaceAgentsContext | None = None,
    mission_hud: dict[str, Any] | None = None,
    situational_hud: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build redaction-safe prompt/context observability for Mission Control.

    This intentionally reports prompt layers and file provenance, not raw secret
    config values. Context files get hashes and short previews only when their
    filenames are known prompt/memory files.
    """

    persona_id = _safe_persona_id(getattr(persona, "id", None))
    profile = safe_assignment_token(getattr(persona, "hermes_profile", None)) or persona_id
    context_id = "ctx_" + hashlib.sha256(
        "|".join(
            str(item or "")
            for item in (
                persona_id,
                persona_instance_id,
                session_id,
                task_id,
                goal_id,
                turn_id,
                current_message,
                workspace_id,
                workspace_name,
                (
                    workspace_agents.receipt.get("sha256")
                    if workspace_agents is not None
                    else None
                ),
            )
        ).encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    surface = safe_assignment_text(surface_prompt, limit=4000) or ""
    history = _chat_history_context(session_db=session_db, session_id=session_id)
    chat = _chat_metadata(session_db=session_db, session_id=session_id, task_id=task_id)
    accessible_skills = _accessible_skills_context(persona, profile)
    available_skills = available_skills_context(accessible_skills=accessible_skills)
    used_skills = used_skills_context(
        final_model_input=final_model_input,
        trace_events=trace_events,
    )
    return {
        "context_id": context_id,
        "prompt_mode": prompt_mode,
        "persona_id": persona_id,
        "persona_instance_id": safe_assignment_token(persona_instance_id),
        "profile": profile,
        "display_name": safe_assignment_text(getattr(persona, "display_name", None), limit=120) or persona_id,
        "role": safe_assignment_token(getattr(persona, "role", None)) or "agent",
        "session_id": safe_assignment_text(session_id, limit=200),
        "chat_id": chat.get("id"),
        "chat_title": chat.get("title"),
        "chat_name": chat.get("name"),
        "chat": chat,
        "task_id": safe_assignment_token(task_id),
        "goal_id": safe_assignment_token(goal_id),
        # The run-independent slice of the ``## Mission HUD`` the harness injects
        # each turn (typed plan / stage / QA gate). Empty for personas with no
        # bound task; Mission Control's runtime-HUD peek renders it verbatim.
        "mission_hud": mission_hud if isinstance(mission_hud, dict) else {},
        # The full runtime situational HUD (runtime · scope · mission · lane ·
        # roster · mission_hud) — the single projection the operator's runtime
        # HUD strip and the agent's mission-chat turn both render, so operator
        # and agent share one view. Empty until threaded (snapshot path); the
        # chat lane feeds the same projection into the model. See runtime_hud.py.
        "situational_hud": situational_hud if isinstance(situational_hud, dict) else {},
        "workspace_id": safe_assignment_token(workspace_id),
        "workspace_name": safe_assignment_text(workspace_name, limit=120),
        "turn_id": safe_assignment_token(turn_id),
        "surface_prompt": surface,
        "surface_prompt_is_blank": surface == "",
        "limiting_wrapper_active": bool(limiting_wrapper_active),
        "prompt_layers": [
            {
                "name": "Persona identity",
                "kind": "persona_identity",
                "status": "loaded",
                "summary": (
                    "First-person 'you are "
                    + (safe_assignment_text(getattr(persona, "display_name", None), limit=120) or persona_id)
                    + "' identity block; the isolated chat lane does not load the profile SOUL, so this "
                    "names the persona and forbids self-relay."
                ),
            },
            {
                "name": "Hermes core prompt",
                "kind": "system_core",
                "status": "loaded_by_profile_runner",
                "summary": "Normal Hermes profile chat system stack.",
            },
            {
                "name": "Mission Control surface prompt",
                "kind": "surface",
                "status": "blank" if surface == "" else "configured",
                "summary": "Blank by default; no limiting wrapper is applied.",
                "preview": surface[:SAFE_PREVIEW_LIMIT],
            },
            {
                "name": "Profile memory",
                "kind": "profile_context",
                "status": "loaded" if _mission_chat_memory_loaded(persona) else "skipped",
                "summary": (
                    "Profile MEMORY.md / USER.md loaded (persona opts in via include_profile_memory)."
                    if _mission_chat_memory_loaded(persona)
                    else "Profile memory skipped; this persona does not opt into its bound profile's memory."
                ),
            },
            *(
                [
                    {
                        "name": "Workspace AGENTS.md",
                        "kind": "workspace_context",
                        "status": workspace_agents.receipt.get("status", "unknown"),
                        "summary": (
                            "Injected from the operator-selected workspace directory."
                            if workspace_agents.content is not None
                            else "Workspace instructions were not injected; see the file receipt."
                        ),
                    }
                ]
                if workspace_agents is not None
                else []
            ),
            {
                "name": "Chat history context",
                "kind": "conversation",
                "status": "loaded" if history else "empty",
                "summary": f"{len(history)} prior redaction-safe chat message(s) supplied before this turn.",
            },
        ],
        "context_files": [
            *_profile_context_files(profile),
            *([workspace_agents.receipt] if workspace_agents is not None else []),
        ],
        "used_skills": used_skills,
        # C1 RECORD-ONCE (2026-07-17): the built row carries ONE copy of each
        # fact — the pre-C1 alias keys (``skills_catalog`` ≡ available_skills,
        # ``skills`` ≡ accessible_skills) are DELETED, no compat emission
        # (ruling 0). Readers were audited and retargeted to the canonical two;
        # legacy persisted rows that still carry the aliases are normalized at
        # the read/persist boundaries.
        "accessible_skills": accessible_skills,
        "available_skills": available_skills,
        "chat_history_context": history,
        "retrieval_context": [],
        "final_model_input": _safe_final_model_input(final_model_input),
        "model_selection": _safe_model_selection(model_selection),
        # What this ONE operator message actually burned, metered per API call
        # and summed over the turn's tool loop. Distinct from context_budget,
        # which is the size of the assembled context for a single call — the
        # two were conflated, which is how a 6K inspector sat next to a 13K bill.
        "turn_usage": _safe_turn_usage(turn_usage),
        "context_budget": _context_budget(model_selection, final_model_input, turn_usage),
        "prompt_flags": {
            "skip_context_files": not bool(getattr(persona, "include_core_context_files", False)),
            "skip_memory": not _mission_chat_memory_loaded(persona),
            # The isolated chat lane runs with skip_context_files=True and never
            # sets load_soul_identity, so the profile SOUL is NOT the identity —
            # the first-person persona-identity layer is. Report that honestly.
            "load_soul_identity": False,
            "surface_prompt_blank": surface == "",
            "limiting_wrapper_active": bool(limiting_wrapper_active),
            "workspace_agents_injected": bool(
                workspace_agents is not None and workspace_agents.content is not None
            ),
        },
        "redaction": {
            "status": "safe",
            "notes": [
                "Prompt observability shows file provenance, hashes, and short redaction-safe previews.",
                "Secrets and raw provider credentials are not included.",
            ],
        },
    }


def attach_prompt_observability_turn_results(
    context: dict[str, Any],
    *,
    final_model_input: dict[str, Any] | None = None,
    model_selection: dict[str, Any] | None = None,
    turn_usage: dict[str, Any] | None = None,
    trace_events: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """C1 build-once seam: PATCH the pre-turn row with the turn's results.

    The mission-chat turn used to build the observability row TWICE per turn —
    a pre-turn build (record-at-injection: history, skills, context files,
    HUD) and a post-turn FULL rebuild that re-read SessionDB history and
    re-scanned the skill catalog just to attach what the turn produced. This
    attaches exactly the turn-result fields (``final_model_input``,
    ``turn_usage``, trace-derived ``used_skills``, ``model_selection``, and the
    recomputed ``context_budget``) onto the pre-turn object instead. The
    record-at-injection fields are deliberately NOT touched: the peek shows
    exactly what was fed, never a post-hoc re-derivation.

    Lives here (not in the CLI part) because ``persona_commands.py`` is exec'd
    into harness globals — nothing defined there is importable or unit-testable.
    Mutates ``context`` in place and returns it.
    """

    context["final_model_input"] = _safe_final_model_input(final_model_input)
    if model_selection is not None:
        context["model_selection"] = _safe_model_selection(model_selection)
    context["turn_usage"] = _safe_turn_usage(turn_usage)
    context["used_skills"] = used_skills_context(
        final_model_input=final_model_input,
        trace_events=trace_events,
    )
    context["context_budget"] = _context_budget(
        model_selection if model_selection is not None else context.get("model_selection"),
        final_model_input,
        turn_usage,
    )
    return context


#: C3 (2026-07-17): the slim typed subset of the per-turn observability row that
#: the terminal ``chat.final`` frame — and the mission-chat failure frames that
#: carry observability — embed on the wire. The FULL row used to ride every
#: terminal frame (~26 KB post-C1, still mostly ``final_model_input`` + prompt
#: layers + context files + chat history), yet the launcher decodes only these
#: fields off the LIVE frame: the Context peek's primary source is the snapshot
#: frame's ``chat_contexts``, and this block is its zero-fetch live fallback
#: (situational HUD + turn usage), while the Skills HUD prefers the frame
#: context and degrades honestly to ``instance.skills`` when the fallback lacks
#: skill lists. Ruling §7.3 (settled by the operator): keep EXACTLY these keys.
#: ``chat_id`` / ``chat_title`` are included because the launcher
#: ``MissionPromptChatContext`` parser consumes them (chat identity in the
#: fallback window). Deliberately NOT shipped: the skill LISTS
#: (``accessible_skills`` / ``available_skills`` + their refs),
#: ``final_model_input``, ``prompt_layers``, ``context_files``,
#: ``chat_history_context`` — the complete record-at-injection truth stays on
#: disk in the persisted ctx row (archive-never-delete) and the turn store keeps
#: the element/replay authority.
CHAT_FINAL_OBSERVABILITY_FIELDS: tuple[str, ...] = (
    "context_id",
    "chat_id",
    "chat_title",
    "turn_usage",
    "model_selection",
    "context_budget",
    "situational_hud",
    "used_skills",
)


def slim_chat_final_observability(
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project a built observability row down to the ``chat.final`` wire subset.

    Pure and side-effect-free (``persona_commands.py`` is exec'd into harness
    globals, so this lives here where it is importable/unit-testable and is
    called at the emit site). ONE shape (ruling 0): always returns the same key
    set, and the collection-typed fields keep their empty shape (``{}`` / ``[]``)
    even if the row lacked them, so the launcher never decodes a ``null`` where
    it expects a map or list. Never mutates ``context`` and never re-derives
    anything — the record-at-injection fields it reads were resolved once,
    upstream, on the row this projects from.
    """

    if not isinstance(context, dict):
        return {}
    slim: dict[str, Any] = {key: context.get(key) for key in CHAT_FINAL_OBSERVABILITY_FIELDS}
    if slim.get("situational_hud") is None:
        slim["situational_hud"] = {}
    if slim.get("used_skills") is None:
        slim["used_skills"] = []
    if slim.get("model_selection") is None:
        slim["model_selection"] = {}
    return slim


def _safe_model_selection(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "default_provider",
        "default_model",
        "chat_provider",
        "chat_model",
        "effective_provider",
        "effective_model",
        "model_is_default",
        "scope",
    }
    result: dict[str, Any] = {}
    for key in allowed:
        item = value.get(key)
        if isinstance(item, bool):
            result[key] = item
        elif isinstance(item, str) and item.strip():
            result[key] = safe_assignment_text(item, limit=220)
        elif item is None and key in {"chat_provider", "chat_model"}:
            result[key] = None
    return result


def _safe_persona_id(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.lower().startswith("profile:"):
        profile = safe_assignment_token(raw.split(":", 1)[1])
        return f"profile:{profile}" if profile else "profile:unknown"
    return safe_assignment_token(raw) or "unknown"


# --------------------------------------------------------------------------- #
# S3 read-model — hoist duplicated globals + evict on-demand debug payloads.
#
# The skills catalog is a GLOBAL (one installed catalog per config; one
# accessible set per persona), yet the pre-S3 frame stored ``available_skills``
# / ``skills_catalog`` (installed catalog, ~19KB) AND ``accessible_skills`` /
# ``skills`` (per-persona set, ~8KB) INLINE on EVERY ``chat_contexts`` row —
# byte-identical across rows (S1 audit: 1.91 MiB wasted of a 6.05 MiB frame).
# S3 stores each distinct skill list ONCE, content-addressed, under
# ``prompt_observability.skills_catalogs`` and replaces the four inline fields
# with two hash refs (``available_skills_ref`` / ``accessible_skills_ref``); the
# two byte-identical alias pairs collapse to their canonical ref.
#
# ``final_model_input`` (~30KB/row) is a per-turn DEBUG artifact read only when
# an operator opens the Context peek — it has no steady-state reader yet rode
# every frame. S3 evicts it to a typed stub carrying the recorded byte count +
# message count + fetch verb; the full payload stays on disk in the persisted
# observability row (archive-never-delete) and is fetched on demand.
#
# S7-B RULING-0 COMPAT STRIP (2026-07-16): the ``read_model.inline_prompt_payloads``
# kill-switch and its inline legacy branch were removed — the hoisted/evicted
# shape is the ONLY shape. Rollback = ``git revert``, not a flag flip.
# --------------------------------------------------------------------------- #

#: Length of the content-hash refs (sha256 prefix). Short enough to be cheap on
#: every row, wide enough that a collision across a frame's skill lists is
#: astronomically unlikely.
SKILLS_REF_HASH_LEN = 16

#: The inline per-row skill-list fields hoisted out of each ``chat_contexts``
#: row in the default (hoisted) shape. ``skills_catalog`` aliases
#: ``available_skills`` and ``skills`` aliases ``accessible_skills`` — all four
#: leave the row; the two canonical lists are recoverable by resolving the two
#: refs against ``skills_catalogs``.
HOISTED_SKILL_LIST_FIELDS = (
    "available_skills",
    "skills_catalog",
    "accessible_skills",
    "skills",
)


def _skills_list_content_hash(rows: Any) -> str:
    """Stable content hash of a skill list (compact, sorted, non-ASCII-safe).

    Byte-identical lists hash to the same ref, so the global installed catalog
    (identical on every row) and any two personas sharing an accessible set map
    to one stored blob."""

    payload = json.dumps(
        to_jsonable(rows),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:SKILLS_REF_HASH_LEN]


def _hoist_skills_catalogs(
    chat_contexts: list[dict[str, Any]], catalogs: dict[str, Any]
) -> None:
    """Replace each row's inline skill lists with content-hash refs into
    ``catalogs`` (stored once). Mutates ``chat_contexts`` and ``catalogs`` in
    place. A row missing a list simply carries no ref for it — never a fake
    empty catalog (an absent ref resolves to nothing, honestly)."""

    for row in chat_contexts:
        if not isinstance(row, dict):
            continue
        available = row.get("available_skills")
        if isinstance(available, list):
            ref = _skills_list_content_hash(available)
            catalogs.setdefault(ref, available)
            row["available_skills_ref"] = ref
        accessible = row.get("accessible_skills")
        if isinstance(accessible, list):
            ref = _skills_list_content_hash(accessible)
            catalogs.setdefault(ref, accessible)
            row["accessible_skills_ref"] = ref
        for field in HOISTED_SKILL_LIST_FIELDS:
            row.pop(field, None)


def _final_model_input_stub(final_model_input: dict[str, Any], context_id: Any) -> dict[str, Any]:
    """The evicted ``final_model_input`` frame value: a typed accounting stub.

    Carries the recorded byte size (so the operator knows the payload exists and
    how large it is), the message count (so the peek can still say "N messages"),
    and the addressable fetch verb. Never a silent absence, never a fake-empty
    payload."""

    payload = json.dumps(
        to_jsonable(final_model_input),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    message_count = final_model_input.get("message_count")
    if not isinstance(message_count, int):
        messages = final_model_input.get("messages")
        message_count = len(messages) if isinstance(messages, list) else 0
    token = safe_assignment_token(context_id)
    return {
        "evicted": True,
        "bytes": len(payload.encode("utf-8")),
        "message_count": message_count,
        "context_id": token or None,
        "fetch": "harness prompt-context final-model-input --context-id <id> --json",
    }


def _evict_final_model_input(chat_contexts: list[dict[str, Any]]) -> None:
    """Replace each row's heavy ``final_model_input`` with the accounting stub.

    Mutates ``chat_contexts`` in place. The persisted row on disk is untouched —
    only the FRAME copy is stubbed."""

    for row in chat_contexts:
        if not isinstance(row, dict):
            continue
        final_model_input = row.get("final_model_input")
        if isinstance(final_model_input, dict) and not final_model_input.get("evicted"):
            row["final_model_input"] = _final_model_input_stub(
                final_model_input, row.get("context_id")
            )


def load_final_model_input_for_context(context_id: str) -> dict[str, Any] | None:
    """On-demand read of a persisted context's ``final_model_input``.

    The S3 frame ships an eviction stub; the FULL payload stays on disk in the
    persisted observability row (``persist_prompt_observability_context``). This
    is the read the Context peek's on-demand fetch resolves — the launcher fetch
    lane and a future ``harness prompt-context final-model-input`` CLI verb both
    call through here (verb handoff filed; harness_cli is not this stage's to
    edit). Returns the redaction-safe payload, or None when the row/field is
    absent."""

    data = load_persisted_context_row(context_id)
    if not isinstance(data, dict):
        return None
    final_model_input = data.get("final_model_input")
    if isinstance(final_model_input, dict) and not final_model_input.get("evicted"):
        return final_model_input
    return None


def snapshot_prompt_observability(
    *,
    personas: Iterable[Any],
    persona_instances: Iterable[Any],
    session_db: Any | None = None,
    tasks: Iterable[Any] | None = None,
    proof_store: Any | None = None,
    daemon: dict[str, Any] | None = None,
    realm: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    # Deferred import: context_builder pulls a large dependency graph, and this
    # module is imported very early. A function-local import keeps module load
    # order robust while still giving the preview a single-authority HUD builder.
    from .context_builder import mission_hud_preview
    from .runtime_hud import resolve_situational_hud

    tasks_by_id = {
        safe_assignment_token(getattr(task, "id", None)): task
        for task in (tasks or [])
        if safe_assignment_token(getattr(task, "id", None))
    }
    # Materialize once: the roster is reused for every lane's situational HUD
    # (thread count + on-level list) and the input may be a one-shot iterable.
    roster = list(persona_instances)

    def _preview_for(task_id: str | None) -> dict[str, Any]:
        task = tasks_by_id.get(safe_assignment_token(task_id) or "")
        if task is None:
            return {}
        try:
            return mission_hud_preview(task, proof_store=proof_store)
        except Exception:
            # The peek is diagnostic; never let a HUD preview failure break the
            # snapshot that carries everything else Mission Control renders.
            return {}

    def _situational_for(instance: Any, task_id: str | None) -> dict[str, Any]:
        try:
            goal_id = getattr(instance, "goal_id", None)
            return resolve_situational_hud(
                instance,
                daemon=daemon,
                realm=realm,
                workspace=workspace,
                roster=roster,
                task=tasks_by_id.get(safe_assignment_token(task_id) or ""),
                goal_task=tasks_by_id.get(safe_assignment_token(goal_id) or ""),
                proof_store=proof_store,
            )
        except Exception:
            # Same guarantee as the preview: a situational-HUD failure degrades
            # to {} rather than breaking the snapshot.
            return {}

    contexts: list[dict[str, Any]] = []
    by_persona = {
        safe_assignment_token(getattr(persona, "id", None)): persona
        for persona in personas
        if safe_assignment_token(getattr(persona, "id", None))
    }
    for instance in roster:
        persona_id = safe_assignment_token(getattr(instance, "persona_id", None))
        persona = by_persona.get(persona_id) or _profile_persona_from_instance(instance)
        if persona is None:
            continue
        task_id = getattr(instance, "current_task_id", None)
        contexts.append(
            mission_chat_prompt_observability(
                persona=persona,
                persona_instance_id=_persona_instance_id(instance),
                session_id=getattr(instance, "session_id", None),
                task_id=task_id,
                goal_id=getattr(instance, "goal_id", None),
                surface_prompt="",
                limiting_wrapper_active=False,
                session_db=session_db,
                mission_hud=_preview_for(task_id),
                situational_hud=_situational_for(instance, task_id),
            )
        )
    if not contexts:
        for persona in personas:
            persona_id = safe_assignment_token(getattr(persona, "id", None))
            if persona_id == "neko_supervisor":
                contexts.append(
                    mission_chat_prompt_observability(
                        persona=persona,
                        surface_prompt="",
                        limiting_wrapper_active=False,
                        session_db=session_db,
                    )
                )
                break
    # S8: the frame keeps only LIVE persona instances' current-session context
    # rows; historical/stale rows (departed instances, closed sessions) leave the
    # frame (operator ruling 2026-07-17: "old residue and runs need to be
    # purged"). The persisted files stay on disk (archive-never-delete); the
    # Context peek only selects a live roster agent, so a dropped row is never
    # requested. ``built_keys`` are the freshly-built roster contexts = the live
    # rows; live instance/session ids catch a live agent whose row came only from
    # disk.
    built_keys = {_context_row_key(item) for item in contexts}
    live_instance_ids = {
        token
        for token in (safe_assignment_token(_persona_instance_id(inst)) for inst in roster)
        if token
    }
    live_session_ids = {
        session
        for session in (
            safe_assignment_text(getattr(inst, "session_id", None), limit=200) for inst in roster
        )
        if session
    }
    # C2: roster-keyed read — the latest-pointer index resolves the live lanes
    # and the build reads exactly those rows (typed glob fallback on index
    # miss/corruption; the read never writes).
    disk_rows, ctx_read = load_live_prompt_observability_contexts(
        built_keys=built_keys,
        live_instance_ids=live_instance_ids,
        live_session_ids=live_session_ids,
    )
    chat_contexts = _merge_latest_contexts(contexts, session_db=session_db, disk_rows=disk_rows)
    chat_contexts, chat_contexts_evicted = _filter_live_chat_contexts(
        chat_contexts,
        built_keys=built_keys,
        live_instance_ids=live_instance_ids,
        live_session_ids=live_session_ids,
    )
    # Index-mode reads never load the stale lanes at all — fold them into the
    # same eviction count the post-merge filter feeds (one accounting, both
    # read modes).
    chat_contexts_evicted += int(ctx_read.get("stale_lanes") or 0)
    # S3: hoist the duplicated skills catalogs to one content-addressed table and
    # evict the heavy per-turn debug payload. This is the only shape (S7-B
    # RULING-0: no inline legacy fallback).
    skills_catalogs: dict[str, Any] = {}
    _hoist_skills_catalogs(chat_contexts, skills_catalogs)
    _evict_final_model_input(chat_contexts)
    # C1: ref-shaped persisted rows reach the frame already carrying their
    # ``*_ref`` hashes (no inline lists for the hoist to fold) — the frame's
    # catalog accounting must include those refs too, or the pointer stub would
    # under-report the resolvable hashes.
    frame_catalog_hashes = set(skills_catalogs)
    for row in chat_contexts:
        if not isinstance(row, dict):
            continue
        for ref_key in ("available_skills_ref", "accessible_skills_ref"):
            token = safe_assignment_token(row.get(ref_key))
            if token:
                frame_catalog_hashes.add(token)
    # S8: the ``skills_catalogs`` table LEAVES the frame entirely (operator ruling
    # 2026-07-17: "skills catalog should just be pointers to the skills"). Rows
    # keep their ``*_ref`` content hashes; the catalog bodies are served on demand
    # by ``harness skills catalog --hash <h> --json`` and cached FOREVER launcher
    # side (a content hash is immutable). The frame carries only a typed pointer
    # stub (count + fetch verb) — never a silent absence.
    return {
        "schema_version": 1,
        "default_flow": {
            "id": "neko_two_dev_default",
            "lead": "neko_supervisor",
            "dev_specialists": ["backend_dev", "dev"],
            "qa_default": False,
        },
        "surface_prompt_default": "",
        "chat_contexts": chat_contexts,
        # S8: honest accounting for the historical/stale context rows evicted
        # from the frame — their persisted files remain on disk and are fetched
        # on demand (never a silent absence). C2 adds the retention accounting
        # (``archived_count``: rows MOVED to the archive dir, still fetchable)
        # and the typed read receipt (index hit vs glob fallback — a degraded
        # read is visible, never silent).
        "chat_contexts_ref": {
            "evicted": True,
            "count": chat_contexts_evicted,
            "live_count": len(chat_contexts),
            "archived_count": int(ctx_read.get("archived_count") or 0),
            "fetch": "harness prompt-context show --context-id <id> --json",
            "read": {
                "source": ctx_read.get("source"),
                "index_status": ctx_read.get("index_status"),
                "files_read": int(ctx_read.get("files_read") or 0),
                "index_misses": int(ctx_read.get("index_misses") or 0),
            },
        },
        # One fact, one owner, one COPY: the deduplicated skill lists are no
        # longer shipped in-frame. Rows carry ``available_skills_ref`` /
        # ``accessible_skills_ref``; the bodies are content-addressed and fetched
        # once by hash. This pointer accounts the eviction (hoist-folded catalogs
        # plus the refs already carried by C1 ref-shaped persisted rows).
        "skills_catalogs_ref": {
            "evicted": True,
            "count": len(frame_catalog_hashes),
            "hashes": sorted(frame_catalog_hashes),
            "fetch": "harness skills catalog --hash <hash> --json",
        },
    }


def skills_catalog_by_hash(content_hash: str) -> list[dict[str, Any]] | None:
    """On-demand resolve of one content-addressed skills catalog by its hash.

    S8 evicted the ``skills_catalogs`` table from the frame; rows keep only
    ``*_ref`` hashes. C1 (2026-07-17) made the resolve O(1): the catalog bodies
    are stored ONCE, content-addressed, in the persist-time catalog store
    (``prompt_observability_catalogs/<hash>.json``) and read back directly. The
    pre-C1 walk over the newest persisted rows remains as the LEGACY-ROW
    fallback — rows persisted before the store existed still carry inline lists
    (archive-never-delete means they exist) and still resolve. Read-only: the
    store is written only by the persist chokepoint. Returns ``None`` on an
    honest miss (the launcher renders a pending state and retries next frame),
    never a fake empty catalog."""

    token = str(content_hash or "").strip()
    if not token:
        return None
    stored = load_skills_catalog_from_store(token)
    if stored is not None:
        return stored
    for row in load_latest_prompt_observability_contexts():
        if not isinstance(row, dict):
            continue
        for field in HOISTED_SKILL_LIST_FIELDS:
            value = row.get(field)
            if isinstance(value, list) and _skills_list_content_hash(value) == token:
                return value
    return None


def load_skills_catalog_from_store(content_hash: str) -> list[dict[str, Any]] | None:
    """O(1) read of one catalog from the content-addressed store.

    Integrity-checked: the loaded list must hash back to its own address — a
    corrupt or tampered store file is a typed miss (never fake content), and
    the caller's legacy fallback walk still gets its chance."""

    token = safe_assignment_token(content_hash)
    if not token:
        return None
    path = paths.prompt_observability_catalogs_dir() / f"{token}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    if _skills_list_content_hash(data) != token:
        return None
    return data


def _store_skills_catalog(ref: str, rows: list) -> None:
    """Write one content-addressed catalog iff absent (a content hash is
    immutable, so an existing file is already the right bytes). Compact."""

    path = paths.prompt_observability_catalogs_dir() / f"{ref}.json"
    if path.exists():
        return
    atomic_json_write(
        path,
        to_jsonable(rows),
        indent=None,
        sort_keys=True,
        separators=(",", ":"),
    )


# --------------------------------------------------------------------------- #
# C1/C2 record-once store (2026-07-17, console-chat plan stages C1+C2).
#
# C1 — the persisted per-turn RECORD carries one copy of each fact: the two
# byte-identical alias key-pairs (57.8% of every pre-C1 file) are gone from the
# built row, and the two canonical skill lists leave the row as content-hash
# refs into a persist-time catalog store. ``final_model_input`` STAYS in the
# row, compact (operator ruling 2026-07-17 §7.2). Writes are compact JSON.
#
# C2 — the live dir is bounded and reads are roster-keyed: the persist
# chokepoint (ONE owner) maintains a latest-pointer index mapping
# (persona_instance_id, session_id) -> newest context ids and enforces per-lane
# retention (newest K live; older rows MOVE to the archive dir —
# archive-never-delete, accounted via ``archived_count``). The frame build
# resolves the live roster's lanes through the index and reads exactly those
# rows; an absent/corrupt index or a dangling pointer falls back to the legacy
# glob path with typed accounting. The READ path never writes — the heal
# happens at the next persist (emit-path projections are READ-ONLY).
# --------------------------------------------------------------------------- #

#: C2 retention: newest K rows per (persona_instance_id, session_id) lane stay
#: live; older rows move to ``prompt_observability_archive/``.
PROMPT_OBSERVABILITY_RETAIN_PER_LANE = 2

#: C1 persisted-row shape: (canonical inline field, legacy alias field,
#: persisted ref field). The alias is normalized into the canonical value when
#: a legacy-shaped input carries only the alias — data is never dropped.
_PERSIST_REF_FIELDS = (
    ("available_skills", "skills_catalog", "available_skills_ref"),
    ("accessible_skills", "skills", "accessible_skills_ref"),
)


def persist_prompt_observability_context(context: dict[str, Any]) -> None:
    """THE persist chokepoint (one owner): ref-transform, compact write,
    latest-pointer index, and retention happen here and nowhere else.

    The caller's dict is NEVER mutated — the live ``chat.final`` wire echo
    still carries the built row with its inline canonical lists (slimming that
    echo is stage C3's lane, not this one)."""

    context_id = safe_assignment_token(context.get("context_id"))
    if not context_id:
        return
    # Deep JSON copy (to_jsonable rebuilds every dict/list) — mutations below
    # cannot touch the caller's object.
    row = to_jsonable(context)
    for canonical_field, alias_field, ref_field in _PERSIST_REF_FIELDS:
        value = row.pop(canonical_field, None)
        alias = row.pop(alias_field, None)
        if not isinstance(value, list):
            value = alias if isinstance(alias, list) else None
        if value is None:
            # No list, no ref — an absent catalog is honest absence, never a
            # fake empty one. A re-persisted ref-shaped row keeps its refs.
            continue
        ref = _skills_list_content_hash(value)
        _store_skills_catalog(ref, value)
        row[ref_field] = ref
    root = paths.prompt_observability_dir()
    root.mkdir(parents=True, exist_ok=True)
    atomic_json_write(
        root / f"{context_id}.json",
        row,
        indent=None,
        sort_keys=True,
        separators=(",", ":"),
    )
    _index_and_retain_after_persist(row, context_id=context_id)


def _lane_key_for_row(row: dict[str, Any]) -> tuple[str, str]:
    """The (instance, session) retention/index lane a persisted row belongs to."""

    return (
        safe_assignment_token(row.get("persona_instance_id")) or "",
        safe_assignment_text(row.get("session_id"), limit=200) or "",
    )


def _load_prompt_observability_index() -> dict[str, Any] | None:
    """The latest-pointer index, or ``None`` when absent/corrupt.

    ``None`` is the typed fallback signal: readers glob instead, and the next
    persist rebuilds the index (its one owner). Never raises."""

    path = paths.prompt_observability_index_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return None
    return data


def _archived_context_count() -> int:
    archive = paths.prompt_observability_archive_dir()
    if not archive.exists():
        return 0
    return sum(1 for _ in archive.glob("*.json"))


def _rebuild_prompt_observability_index() -> dict[str, Any]:
    """Full index rebuild from the live dir (the heal path; persist-time only).

    Parses every live row once — the one-time O(dir) cost that makes every
    subsequent frame read roster-sized — orders each lane newest-first by file
    mtime, and recounts the archive. Unreadable/mis-named files are counted
    (typed, never silent) and left alone: nothing here deletes."""

    root = paths.prompt_observability_dir()
    unreadable = 0
    files: list[tuple[float, str, dict[str, Any]]] = []
    if root.exists():
        for path in root.glob("*.json"):
            try:
                mtime = path.stat().st_mtime
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                unreadable += 1
                continue
            context_id = safe_assignment_token(data.get("context_id")) if isinstance(data, dict) else None
            if not context_id or path.name != f"{context_id}.json":
                unreadable += 1
                continue
            files.append((mtime, context_id, data))
    lanes: dict[tuple[str, str], dict[str, Any]] = {}
    for _mtime, context_id, data in sorted(files, key=lambda item: item[0], reverse=True):
        key = _lane_key_for_row(data)
        entry = lanes.setdefault(
            key,
            {
                "instance_id": key[0],
                "session_id": key[1],
                "persona_id": safe_assignment_token(data.get("persona_id")) or "",
                "context_ids": [],
            },
        )
        entry["context_ids"].append(context_id)
    return {
        "schema_version": 1,
        "archived_count": _archived_context_count(),
        "unreadable_count": unreadable,
        "entries": list(lanes.values()),
    }


def _index_and_retain_after_persist(row: dict[str, Any], *, context_id: str) -> None:
    """Update the latest-pointer index for this persist and enforce retention.

    One owner: only this persist-time hook moves rows out of the live dir or
    writes the index. An absent/corrupt index is healed here by a full rebuild
    (which also folds in any pre-index legacy rows, so the first persist after
    landing performs the one-time bounded-store sweep)."""

    index = _load_prompt_observability_index()
    if index is None:
        index = _rebuild_prompt_observability_index()
    entries = [entry for entry in index.get("entries", []) if isinstance(entry, dict)]
    key = _lane_key_for_row(row)
    entry = next(
        (
            candidate
            for candidate in entries
            if (
                str(candidate.get("instance_id") or ""),
                str(candidate.get("session_id") or ""),
            )
            == key
        ),
        None,
    )
    if entry is None:
        entry = {
            "instance_id": key[0],
            "session_id": key[1],
            "persona_id": "",
            "context_ids": [],
        }
        entries.append(entry)
    known = [str(item) for item in entry.get("context_ids", []) if str(item or "").strip()]
    entry["context_ids"] = [context_id] + [item for item in known if item != context_id]
    entry["persona_id"] = (
        safe_assignment_token(row.get("persona_id")) or str(entry.get("persona_id") or "")
    )
    archived = int(index.get("archived_count") or 0)
    root = paths.prompt_observability_dir()
    archive_dir = paths.prompt_observability_archive_dir()
    for candidate in entries:
        ids = [str(item) for item in candidate.get("context_ids", []) if str(item or "").strip()]
        keep = ids[:PROMPT_OBSERVABILITY_RETAIN_PER_LANE]
        for stale_id in ids[PROMPT_OBSERVABILITY_RETAIN_PER_LANE:]:
            source = root / f"{stale_id}.json"
            try:
                if source.exists():
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    os.replace(source, archive_dir / f"{stale_id}.json")
                    archived += 1
            except OSError:
                # The move failed — keep the row indexed AND live rather than
                # losing track of it. Retention retries on the next persist.
                keep.append(stale_id)
        # Dangling-pointer heal: a kept id whose live file vanished outside the
        # chokepoint (sabotage/manual deletion) is dropped here — the READ path
        # reported the typed miss and fell back; THIS is where the index heals
        # (its one owner). An id that was legitimately archived is not "kept".
        candidate["context_ids"] = [
            item for item in keep if (root / f"{item}.json").exists()
        ]
    # A lane with no live rows left has nothing to point at — prune the entry
    # (its archived rows remain fetchable by id; the index only maps LIVE rows).
    index["entries"] = [entry for entry in entries if entry.get("context_ids")]
    index["archived_count"] = archived
    atomic_json_write(
        paths.prompt_observability_index_path(),
        index,
        indent=None,
        sort_keys=True,
        separators=(",", ":"),
    )


def load_live_prompt_observability_contexts(
    *,
    built_keys: set[tuple[str, str, str]],
    live_instance_ids: set[str],
    live_session_ids: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Roster-keyed frame read (C2): read EXACTLY the live lanes' newest rows.

    Resolves the live roster's (instance, session) lanes through the
    latest-pointer index and reads one file per live lane, instead of the
    legacy glob+stat+parse of the newest 50 files (4.51 MB measured) on every
    full core. The index is a cache, never authority: an absent/corrupt index
    or a pointer at a missing/corrupt file degrades to the legacy glob path
    with typed accounting in the returned receipt — and this READ path never
    writes (the heal happens at the next persist, the index's one owner).

    Returns ``(rows, receipt)`` — rows newest-first (the legacy ordering
    contract), receipt = {source, index_status, files_read, index_misses,
    stale_lanes, archived_count}."""

    receipt: dict[str, Any] = {
        "source": "index",
        "index_status": "hit",
        "files_read": 0,
        "index_misses": 0,
        "stale_lanes": 0,
        "archived_count": 0,
    }
    index = _load_prompt_observability_index()
    if index is None:
        receipt["source"] = "glob_fallback"
        receipt["index_status"] = "absent_or_corrupt"
        rows = load_latest_prompt_observability_contexts()
        receipt["files_read"] = len(rows)
        receipt["archived_count"] = _archived_context_count()
        return rows, receipt
    receipt["archived_count"] = int(index.get("archived_count") or 0)
    root = paths.prompt_observability_dir()
    targets: list[Path] = []
    for entry in index.get("entries", []):
        if not isinstance(entry, dict):
            continue
        instance_id = safe_assignment_token(entry.get("instance_id")) or ""
        session_id = safe_assignment_text(entry.get("session_id"), limit=200) or ""
        persona_id = safe_assignment_token(entry.get("persona_id")) or ""
        is_live = (
            (instance_id, session_id, persona_id) in built_keys
            or (instance_id and instance_id in live_instance_ids)
            or (session_id and session_id in live_session_ids)
        )
        if not is_live:
            # Counted, never silently skipped: these lanes' rows stay on disk
            # and feed the frame's ``chat_contexts_ref`` eviction accounting.
            receipt["stale_lanes"] += 1
            continue
        newest = next(
            (
                token
                for token in (
                    safe_assignment_token(item) for item in entry.get("context_ids", [])
                )
                if token
            ),
            None,
        )
        if newest:
            targets.append(root / f"{newest}.json")
    loaded: list[tuple[float, str, dict[str, Any]]] = []
    for path in targets:
        try:
            mtime = path.stat().st_mtime
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            receipt["index_misses"] += 1
            continue
        receipt["files_read"] += 1
        if isinstance(data, dict) and safe_assignment_token(data.get("context_id")):
            loaded.append((mtime, path.name, data))
    if receipt["index_misses"]:
        # A pointer aimed at a deleted/corrupt file: typed miss, and the frame
        # still gets CORRECT output via the legacy glob. Heal at next persist.
        receipt["source"] = "glob_fallback"
        receipt["index_status"] = "miss"
        receipt["stale_lanes"] = 0
        rows = load_latest_prompt_observability_contexts()
        receipt["files_read"] += len(rows)
        return rows, receipt
    # Newest-first with the file name as a stable tiebreak — the same recency
    # order the legacy glob path produced, so the frame section stays
    # deterministic across both read modes.
    loaded.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in loaded], receipt


def load_persisted_context_row(context_id: str) -> dict[str, Any] | None:
    """One persisted observability row by id — live dir first, then the C2
    archive. Archive-never-delete means retention MOVES rows; the fetch lane
    (``harness prompt-context show``) must keep resolving them. Honest ``None``
    on absence/corruption, never a fabricated row."""

    token = safe_assignment_token(context_id)
    if not token:
        return None
    for root in (paths.prompt_observability_dir(), paths.prompt_observability_archive_dir()):
        path = root / f"{token}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def load_latest_prompt_observability_contexts() -> list[dict[str, Any]]:
    root = paths.prompt_observability_dir()
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            import json

            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, dict) and safe_assignment_token(data.get("context_id")):
            rows.append(data)
        if len(rows) >= 50:
            break
    return rows


def _context_row_key(item: dict[str, Any]) -> tuple[str, str, str]:
    """The (instance, session, persona) identity a chat-context row folds on."""

    return (
        safe_assignment_token(item.get("persona_instance_id")) or "",
        safe_assignment_text(item.get("session_id"), limit=200) or "",
        safe_assignment_token(item.get("persona_id")) or "",
    )


def _filter_live_chat_contexts(
    chat_contexts: list[dict[str, Any]],
    *,
    built_keys: set[tuple[str, str, str]],
    live_instance_ids: set[str],
    live_session_ids: set[str],
) -> tuple[list[dict[str, Any]], int]:
    """Keep only chat-context rows tied to a LIVE persona instance's current
    session (S8); return ``(kept, evicted_count)``.

    A row is live when it was freshly BUILT this frame for a roster instance
    (``built_keys``) OR its persona_instance_id / session_id resolves to a live
    instance. Purely-historical/stale rows (a departed instance, a closed
    session) are evicted from the frame — their persisted files stay on disk and
    the Context peek, which only ever selects a LIVE roster agent, never requests
    them (so the eviction is honest, never a fake-empty)."""

    kept: list[dict[str, Any]] = []
    evicted = 0
    for row in chat_contexts:
        if not isinstance(row, dict):
            kept.append(row)
            continue
        key = _context_row_key(row)
        inst_id = safe_assignment_token(row.get("persona_instance_id"))
        sess_id = safe_assignment_text(row.get("session_id"), limit=200)
        is_live = (
            key in built_keys
            or (inst_id and inst_id in live_instance_ids)
            or (sess_id and sess_id in live_session_ids)
        )
        if is_live:
            kept.append(row)
        else:
            evicted += 1
    return kept, evicted


def _merge_latest_contexts(
    contexts: list[dict[str, Any]],
    *,
    session_db: Any | None = None,
    disk_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Merge persisted rows (newest-first) over the freshly built contexts.

    ``disk_rows`` lets the C2 roster-keyed loader supply exactly the live
    lanes' rows; when omitted (legacy callers/tests) the newest-50 glob load
    is used."""

    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    key_for = _context_row_key

    built_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in contexts:
        built_by_key.setdefault(key_for(item), item)

    if disk_rows is None:
        disk_rows = load_latest_prompt_observability_contexts()
    for item in disk_rows:
        key = key_for(item)
        if key in seen:
            continue
        _backfill_derived_fields(item, built_by_key.get(key), session_db=session_db)
        merged.append(item)
        seen.add(key)
    for item in contexts:
        key = key_for(item)
        if key in seen:
            continue
        merged.append(item)
        seen.add(key)
    return merged


def _backfill_derived_fields(
    item: dict[str, Any],
    built: dict[str, Any] | None,
    *,
    session_db: Any | None = None,
) -> None:
    """Repair persisted contexts that predate the current row shape.

    Persisted observability rows are written at chat time, so rows captured
    before newer fields existed lack them. Backfill skills from the freshly
    built context (correct per-persona set) or the profile snapshot, and
    recompute the budget from the row's own model selection + final input.

    C1 shape rules: the alias keys (``skills`` ≡ accessible, ``skills_catalog``
    ≡ available) are READ from legacy rows for normalization but never written
    back — one copy of each fact. A row carrying ``accessible_skills_ref`` /
    ``available_skills_ref`` was persisted by the C1 chokepoint and is correct
    by construction: its skills are present BY REF, so the legacy re-inflation
    paths must not fabricate inline lists over them.
    """
    if built:
        # Persisted rows are written at chat time and never carry the typed
        # mission-HUD preview (chat turns don't compute one). Prefer the freshly
        # built preview so the persisted row exposes the same upcoming-turn HUD.
        built_hud = built.get("mission_hud")
        if isinstance(built_hud, dict) and built_hud and not item.get("mission_hud"):
            item["mission_hud"] = built_hud
        # Same rationale for the runtime situational HUD: persisted chat rows
        # never compute one, so prefer the freshly built projection.
        built_situational = built.get("situational_hud")
        if isinstance(built_situational, dict) and built_situational and not item.get("situational_hud"):
            item["situational_hud"] = built_situational
        for key in (
            "used_skills",
            "accessible_skills",
            "available_skills",
            "chat_id",
            "chat_title",
            "chat_name",
            "chat",
        ):
            value = built.get(key)
            if value is not None and value != []:
                item[key] = value
    if not item.get("chat_id") or not item.get("chat_title"):
        chat = _chat_metadata(
            session_db=session_db,
            session_id=safe_assignment_text(item.get("session_id"), limit=200),
            task_id=safe_assignment_text(item.get("task_id"), limit=160),
        )
        if chat:
            item["chat_id"] = chat.get("id")
            item["chat_title"] = chat.get("title")
            item["chat_name"] = chat.get("name")
            item["chat"] = chat
    # Legacy alias normalization (READ then retire): rows persisted before C1
    # may carry only the alias keys. Fold them into the canonical fields and
    # drop them — the frame carries one copy of each fact.
    if not item.get("accessible_skills") and item.get("skills"):
        item["accessible_skills"] = item.get("skills")
    if not item.get("available_skills") and item.get("skills_catalog"):
        item["available_skills"] = item.get("skills_catalog")
    item.pop("skills", None)
    item.pop("skills_catalog", None)
    if item.get("used_skills") is None:
        item["used_skills"] = used_skills_context(
            final_model_input=item.get("final_model_input")
        )
    # C1 ref-shaped rows carry their skills by content-hash ref — present, not
    # missing. Re-inflating them from the profile snapshot / installed catalog
    # would overwrite the recorded truth with a re-derivation.
    has_accessible_ref = bool(safe_assignment_token(item.get("accessible_skills_ref")))
    has_available_ref = bool(safe_assignment_token(item.get("available_skills_ref")))
    if not has_accessible_ref and (
        not item.get("accessible_skills") or _profile_prompt_skills_need_snapshot(item)
    ):
        profile = safe_assignment_token(item.get("profile"))
        names = _profile_snapshot_skill_names(profile) if profile else []
        if names:
            item["accessible_skills"] = [
                {
                    "name": safe_assignment_token(name) or name,
                    "kind": "skill",
                    "status": "loaded",
                    "hash_tracked": False,
                    "source": "profile_skills_snapshot",
                }
                for name in names[:80]
            ]
            item["available_skills"] = available_skills_context(
                accessible_skills=item["accessible_skills"]
            )
    if item.get("available_skills") is None and not has_available_ref:
        item["available_skills"] = available_skills_context(
            accessible_skills=item.get("accessible_skills") or []
        )
    if _context_budget_needs_refresh(item):
        budget = _context_budget(item.get("model_selection"), item.get("final_model_input"))
        if budget is not None:
            item["context_budget"] = budget


def _persona_instance_id(instance: Any) -> str | None:
    return safe_assignment_token(
        getattr(instance, "id", None) or getattr(instance, "persona_instance_id", None)
    )


def _profile_persona_from_instance(instance: Any) -> Any | None:
    raw_persona_id = str(getattr(instance, "persona_id", "") or "").strip()
    profile = safe_assignment_token(getattr(instance, "profile_id", None))
    lowered = raw_persona_id.lower()
    if lowered.startswith("profile:"):
        profile = safe_assignment_token(raw_persona_id.split(":", 1)[1])
    elif lowered.startswith("profile_"):
        profile = safe_assignment_token(raw_persona_id[len("profile_") :])
    if not profile:
        return None
    display_name = (
        safe_assignment_text(getattr(instance, "display_name", None), limit=120)
        or f"{profile.replace('_', ' ').title()} Agent"
    )
    return SimpleNamespace(
        id=f"profile:{profile}",
        display_name=display_name,
        role="profile",
        hermes_profile=profile,
        skills=[],
        toolsets=["file", "search", "session_search", "todo", "skills"],
    )


def _profile_prompt_skills_need_snapshot(item: dict[str, Any]) -> bool:
    persona_id = str(item.get("persona_id") or "").strip().lower()
    if not (persona_id.startswith("profile:") or persona_id.startswith("profile_")):
        return False
    skills = item.get("accessible_skills") or item.get("skills")
    if not isinstance(skills, list) or not skills:
        return True
    sources = {
        safe_assignment_token(entry.get("source"))
        for entry in skills
        if isinstance(entry, dict)
    }
    return "profile_skills_snapshot" not in sources


def _context_budget_needs_refresh(item: dict[str, Any]) -> bool:
    budget = item.get("context_budget")
    if not isinstance(budget, dict):
        return True
    if budget.get("used_tokens") is None and _estimate_used_tokens(item.get("final_model_input")) is not None:
        return True
    return False


_DEFAULT_COMPACTION_RATIO = 0.50
_CODEX_GPT55_WINDOW_CAP = 272_000


def _static_context_window(model: str, provider: str | None) -> int | None:
    """Resolve a model's context window from the static fallback map.

    Network-free (this runs in the per-turn observability path) — mirrors the
    longest-key-first substring fallback in ``agent.model_metadata`` and applies
    the known ChatGPT-Codex OAuth cap (gpt-5.5 → 272K instead of the 1.05M raw).
    """
    name = (model or "").lower()
    if not name:
        return None
    try:
        from agent.model_metadata import DEFAULT_CONTEXT_LENGTHS
    except Exception:
        return None
    window: int | None = None
    for key in sorted(DEFAULT_CONTEXT_LENGTHS, key=len, reverse=True):
        if key.lower() in name:
            try:
                window = int(DEFAULT_CONTEXT_LENGTHS[key])
            except (TypeError, ValueError):
                window = None
            break
    if not window:
        return None
    prov = (provider or "").lower()
    if window > _CODEX_GPT55_WINDOW_CAP and "codex" in prov and "gpt-5.5" in name:
        window = _CODEX_GPT55_WINDOW_CAP
    return window


def _compaction_ratio(model: str, provider: str | None) -> float:
    """The fraction of the window at which Hermes compacts (0.5 default)."""
    try:
        from agent.auxiliary_client import _compression_threshold_for_model

        override = _compression_threshold_for_model(
            model, provider, allow_codex_gpt55_autoraise=True
        )
        if isinstance(override, (int, float)) and 0 < float(override) <= 1:
            return float(override)
    except Exception:
        pass
    return _DEFAULT_COMPACTION_RATIO


# How ``context_budget.used_tokens`` was derived. The provider's meter is the
# only authority; the estimates exist for the window BEFORE a call has returned
# (or when a turn failed before any call) and must be labeled as such — an
# unlabeled estimate reads as truth and silently under-reports (it cannot see
# tool schemas, which are ~12K tokens on a typical mission-chat turn).
BUDGET_BASIS_METERED_FIRST_CALL = "metered_first_call"
BUDGET_BASIS_ESTIMATE_WITH_TOOLS = "estimate_messages_plus_tools"
BUDGET_BASIS_ESTIMATE_MESSAGES_ONLY = "estimate_messages_only"


def _tool_schema_json_bytes(final_model_input: dict[str, Any] | None) -> int | None:
    if not isinstance(final_model_input, dict):
        return None
    schema = final_model_input.get("tool_schema")
    if not isinstance(schema, dict):
        return None
    raw = schema.get("json_bytes")
    return raw if isinstance(raw, int) and raw > 0 else None


def _estimate_used_tokens(final_model_input: dict[str, Any] | None) -> int | None:
    """Heuristic bytes//4 estimate of the assembled prompt.

    Counts the recorded messages PLUS the tool-schema wire size when it is
    known: the schemas ship on every API call, so an estimate without them is
    not "roughly right", it is missing the second-largest block.
    """
    if not isinstance(final_model_input, dict):
        return None
    messages = final_model_input.get("messages")
    if not isinstance(messages, list):
        return None
    total_bytes = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        raw = message.get("bytes")
        if isinstance(raw, int) and raw > 0:
            total_bytes += raw
        else:
            total_bytes += len(str(message.get("content") or "").encode("utf-8"))
    total_bytes += _tool_schema_json_bytes(final_model_input) or 0
    if total_bytes <= 0:
        return None
    return max(1, total_bytes // 4)


def _metered_assembled_tokens(turn_usage: dict[str, Any] | None) -> int | None:
    """The assembled-context size as the provider metered it, or None.

    Row 1 of the usage ledger is the only honest answer: that call carried the
    system prompt + user message + tool schemas and nothing else. Later calls
    also carry tool results (loop growth), which is turn burn, not context size.
    A single-call turn's total is by definition its first call.
    """
    if not isinstance(turn_usage, dict):
        return None
    first = turn_usage.get("first_call_prompt_tokens")
    if isinstance(first, int) and first > 0:
        return first
    api_calls = turn_usage.get("api_calls")
    prompt_tokens = turn_usage.get("prompt_tokens")
    if api_calls == 1 and isinstance(prompt_tokens, int) and prompt_tokens > 0:
        return prompt_tokens
    return None


def _context_budget(
    model_selection: dict[str, Any] | None,
    final_model_input: dict[str, Any] | None,
    turn_usage: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Per-model context budget for the Sent-to-model bar: window + compaction line.

    ``used_tokens`` prefers the provider-metered first-call prompt (the exact
    assembled context, tool schemas included) and falls back to a labeled
    estimate only when no call has completed. ``used_basis`` says which, so the
    UI never renders a guess as a measurement. Returns None (UI omits the bar)
    when the model/window can't be resolved.
    """
    sel = model_selection if isinstance(model_selection, dict) else {}
    model = sel.get("effective_model") or sel.get("chat_model") or sel.get("default_model")
    provider = sel.get("effective_provider") or sel.get("chat_provider") or sel.get("default_provider")
    if not model:
        return None
    window = _static_context_window(str(model), provider)
    if not window:
        return None
    ratio = _compaction_ratio(str(model), provider)
    estimate = _estimate_used_tokens(final_model_input)
    metered = _metered_assembled_tokens(turn_usage)
    if metered is not None:
        used = metered
        basis = BUDGET_BASIS_METERED_FIRST_CALL
    else:
        used = estimate
        basis = (
            BUDGET_BASIS_ESTIMATE_WITH_TOOLS
            if _tool_schema_json_bytes(final_model_input) is not None
            else BUDGET_BASIS_ESTIMATE_MESSAGES_ONLY
        )
    budget = {
        "model": safe_assignment_text(str(model), limit=120),
        "provider": safe_assignment_token(provider) if provider else None,
        "window_tokens": int(window),
        "compaction_ratio": round(float(ratio), 4),
        "compaction_tokens": int(window * ratio),
        "used_tokens": used,
        "used_basis": basis,
        # Back-compat for launcher builds that predate `used_basis`; they render
        # a tilde off this bool. Derived, never independently decided.
        "used_estimated": basis != BUDGET_BASIS_METERED_FIRST_CALL,
        "estimate_tokens": estimate,
    }
    # Drift is the tripwire the old design lacked: when the estimate and the
    # meter both exist and disagree badly, the estimator has lost an input class
    # (as it did with tool schemas) and says so instead of failing silently.
    if metered is not None and estimate is not None and estimate > 0:
        budget["estimate_drift_ratio"] = round(metered / estimate, 2)
    return budget


def _profile_snapshot_skill_names(profile: str) -> list[str]:
    """Skill names compiled into the profile's prompt (the skills snapshot).

    Used when a persona declares no per-agent skill subset (e.g. a bare profile
    identity) — the profile still compiles its skills snapshot into the prompt,
    so reporting zero would contradict the loaded `.skills_prompt_snapshot.json`.
    """
    try:
        profile_dir = get_profile_dir(profile)
    except Exception:
        return []
    if profile_dir is None:
        return []
    path = profile_dir / ".skills_prompt_snapshot.json"
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    entries = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    names: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            name = str(entry.get("skill_name") or entry.get("frontmatter_name") or "").strip()
            if name:
                names.append(name)
    return names


def _accessible_skills_context(persona: Any, profile: str) -> list[dict[str, Any]]:
    """Redaction-safe list of skills accessible to this persona/profile.

    Reports skill *identity* and install/hash status (names only — never SKILL.md
    bodies). Hash-tracked harness skills surface drift; the rest are reported as
    accessible so Mission Control can distinguish the catalog from actual
    turn-level skill use.
    Falls back to the profile skills snapshot when the persona declares none.
    """
    declared = [str(item).strip() for item in (getattr(persona, "skills", None) or []) if str(item).strip()]
    source_label = "persona_definition"
    if not declared:
        declared = _profile_snapshot_skill_names(profile)
        source_label = "profile_skills_snapshot"
    if not declared:
        return []
    tracked: set[str] = set()
    mismatched: set[str] = set()
    missing: set[str] = set()
    # Resolve the persona's OWN profile home so skill hash/missing checks run against
    # the profile the persona actually runs on — mirroring profile_readiness (the
    # authoritative surface). Without hermes_home these checks fall back to the active
    # HERMES_HOME, so an isolated persona (e.g. base, whose home differs from the active
    # profile) shows a false hash_mismatch in the HUD while `harness status` reports
    # clean. Keep the None fallback (legacy behavior) when the home can't be resolved.
    profile_home = None
    try:
        from .profile_context import resolve_persona_profile

        binding = resolve_persona_profile(persona)
        profile_home = getattr(binding, "profile_home", None)
    except Exception:
        profile_home = None
    try:
        from .skill_install import HARNESS_SKILLS, harness_skill_hash_mismatches

        tracked = {name for name in declared if name in HARNESS_SKILLS}
        mismatched = set(harness_skill_hash_mismatches(sorted(tracked), hermes_home=profile_home))
    except Exception:
        pass
    try:
        from .profile_readiness import _missing_skill_names

        skill_root = (profile_home / "skills") if profile_home is not None else None
        missing = set(_missing_skill_names(sorted(tracked), skill_root=skill_root))
    except Exception:
        pass
    skills: list[dict[str, Any]] = []
    for name in declared[:80]:
        token = safe_assignment_token(name) or name
        if name in missing:
            status = "missing"
        elif name in mismatched:
            status = "hash_mismatch"
        else:
            status = "accessible"
        skills.append(
            {
                "name": token,
                "kind": "skill",
                "status": status,
                "hash_tracked": name in tracked,
                "source": source_label,
            }
        )
    return skills


# The installed-skill catalog walk parses every SKILL.md frontmatter (~1k
# YAML loads across one snapshot core, measured 2026-07-09), and the core
# asks once per persona chat session (15+ times per build). A short TTL memo
# collapses that to one walk per build. Observability rows only — never
# authority — so a skill installed/removed mid-window simply appears on the
# first core built after the TTL lapses.
_SKILL_CATALOG_TTL_SECONDS = 15.0
_skill_catalog_memo: dict[str, Any] = {"at": 0.0, "rows": None, "walker": None}


def _resolve_skill_walker():
    try:
        from tools.skills_tool import _find_all_skills

        return _find_all_skills
    except Exception:
        return None


def _installed_skill_catalog() -> list:
    """Memo keyed on BOTH the TTL and the walker's identity: a monkeypatched
    or hot-reloaded `skills_tool._find_all_skills` invalidates the memo
    immediately instead of being masked for a TTL window."""
    import time

    walker = _resolve_skill_walker()
    now = time.monotonic()
    if (
        _skill_catalog_memo["rows"] is not None
        and _skill_catalog_memo["walker"] is walker
        and now - _skill_catalog_memo["at"] < _SKILL_CATALOG_TTL_SECONDS
    ):
        return _skill_catalog_memo["rows"]
    rows: list = []
    if walker is not None:
        try:
            installed = walker()
            if isinstance(installed, list):
                rows = installed
        except Exception:
            rows = []
    _skill_catalog_memo["rows"] = rows
    _skill_catalog_memo["at"] = now
    _skill_catalog_memo["walker"] = walker
    return rows


def available_skills_context(
    *,
    accessible_skills: list[dict[str, Any]] | None = None,
    limit: int = 160,
) -> list[dict[str, Any]]:
    """Redaction-safe installed skill catalog for Mission Control.

    This deliberately exposes names and frontmatter descriptions only. It never
    returns SKILL.md bodies or referenced files.
    """

    accessible_by_name = {
        safe_assignment_token(item.get("name")): item
        for item in accessible_skills or []
        if isinstance(item, dict) and safe_assignment_token(item.get("name"))
    }
    rows: list[dict[str, Any]] = []
    installed = _installed_skill_catalog()
    if isinstance(installed, list):
        for skill in installed:
            if not isinstance(skill, dict):
                continue
            name = safe_assignment_token(skill.get("name"))
            if not name:
                continue
            accessible = accessible_by_name.get(name)
            status = "accessible" if accessible else "available"
            if accessible and isinstance(accessible.get("status"), str):
                status = safe_assignment_token(accessible.get("status")) or status
            rows.append(
                {
                    "name": name,
                    "kind": "skill",
                    "status": status,
                    "hash_tracked": bool(accessible.get("hash_tracked")) if accessible else False,
                    "source": "installed_skill_catalog",
                    "category": safe_assignment_token(skill.get("category")) or "skills",
                    "description": safe_assignment_text(skill.get("description"), limit=220) or "",
                    "loadable": True,
                }
            )
    if not rows:
        for name, item in accessible_by_name.items():
            rows.append(
                {
                    "name": name,
                    "kind": "skill",
                    "status": safe_assignment_token(item.get("status")) or "accessible",
                    "hash_tracked": bool(item.get("hash_tracked")),
                    "source": safe_assignment_token(item.get("source")) or "accessible_skills",
                    "category": "skills",
                    "description": "",
                    "loadable": True,
                }
            )
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: str(item.get("name", "")).lower()):
        name = safe_assignment_token(row.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        deduped.append(row)
        if len(deduped) >= limit:
            break
    return deduped


def used_skills_context(
    *,
    final_model_input: dict[str, Any] | None = None,
    trace_events: Iterable[dict[str, Any]] | None = None,
    queued_skills: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Redaction-safe list of skills actually loaded/read during this turn."""
    names: list[str] = []
    for entry in _list_used_skill_entries(final_model_input):
        _append_used_skill_name(names, _extract_skill_name(entry))
    for entry in trace_events or ():
        if not isinstance(entry, dict):
            continue
        tool_name = safe_assignment_token(entry.get("tool_name") or entry.get("tool")).lower()
        if tool_name != "skill_view":
            continue
        if not _skill_trace_event_counts_as_used(entry):
            continue
        _append_used_skill_name(names, _extract_skill_name(entry))
    rows = [
        {
            "name": name,
            "kind": "skill",
            "status": "used",
            "hash_tracked": False,
            "source": "skill_view_trace",
        }
        for name in names
    ]
    for skill in queued_skills or ():
        token = safe_assignment_token(skill)
        if not token or token in names:
            continue
        rows.append(
            {
                "name": token,
                "kind": "skill",
                "status": "used",
                "hash_tracked": False,
                "source": "queued_next_turn_skill",
            }
        )
    return rows


def _list_used_skill_entries(final_model_input: dict[str, Any] | None) -> list[Any]:
    if not isinstance(final_model_input, dict):
        return []
    entries = final_model_input.get("used_skills")
    if isinstance(entries, list):
        return entries
    trace = final_model_input.get("skill_trace")
    if isinstance(trace, list):
        return trace
    return []


def _skill_trace_event_counts_as_used(entry: dict[str, Any]) -> bool:
    status = safe_assignment_token(entry.get("status")).lower()
    if status in {"failed", "error", "errored", "blocked"}:
        return False
    step = safe_assignment_token(entry.get("step")).lower()
    if step and step not in {"tool_finished", "completed", "finished"}:
        return False
    return True


def _append_used_skill_name(names: list[str], value: str | None) -> None:
    token = safe_assignment_token(value)
    if token and token not in names:
        names.append(token)


def _extract_skill_name(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if not isinstance(entry, dict):
        return None
    for key in ("skill_name", "skill", "identifier", "name"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for key in ("input", "invocation", "tool_input", "result", "metadata"):
        nested = entry.get(key)
        if isinstance(nested, dict):
            name = _extract_skill_name(nested)
            if name:
                return name
    command = entry.get("command_label")
    if isinstance(command, str):
        lowered = command.strip()
        for prefix in ("skill_view ", "skills view "):
            if lowered.lower().startswith(prefix):
                return lowered[len(prefix) :].strip().split()[0]
    return None


def _chat_metadata(
    *,
    session_db: Any | None,
    session_id: str | None,
    task_id: str | None = None,
) -> dict[str, Any]:
    safe_id = safe_assignment_text(session_id, limit=200)
    if not safe_id:
        return {}
    title = None
    source = None
    if session_db is not None:
        try:
            title = safe_assignment_text(session_db.get_session_title(safe_id), limit=160)
        except Exception:
            title = None
        try:
            raw = session_db.get_session(safe_id)
        except Exception:
            raw = None
        if isinstance(raw, dict):
            if not title:
                title = safe_assignment_text(raw.get("title"), limit=160)
            source = safe_assignment_token(raw.get("source"))
    if not title and safe_assignment_text(task_id, limit=160):
        title = "Mission run"
        source = source or "task_bound"
    data: dict[str, Any] = {
        "id": safe_id,
        "title": title,
        "name": title,
    }
    if source:
        data["source"] = source
    return data


def _profile_context_files(profile: str) -> list[dict[str, Any]]:
    try:
        profile_dir = get_profile_dir(profile)
    except Exception:
        profile_dir = None
    files: list[dict[str, Any]] = []
    if profile_dir is None:
        return files
    candidates = [
        profile_dir / "SOUL.md",
        profile_dir / "memories" / "MEMORY.md",
        profile_dir / "memories" / "USER.md",
        profile_dir / ".skills_prompt_snapshot.json",
        profile_dir / "config.yaml",
    ]
    for path in candidates:
        files.append(_context_file_summary(path, included=path.exists()))
    return files


def _context_file_summary(path: Path, *, included: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "path": str(path),
        "name": path.name,
        "kind": _file_kind(path),
        "included": bool(included),
        "status": "loaded" if included else "missing_or_not_configured",
    }
    if not included:
        return data
    try:
        raw = path.read_bytes()
    except OSError as exc:
        data["status"] = "unreadable"
        data["error"] = type(exc).__name__
        return data
    data["sha256"] = hashlib.sha256(raw).hexdigest().upper()
    data["bytes"] = len(raw)
    if path.name in {"SOUL.md", "MEMORY.md", "USER.md", "AGENTS.md"}:
        text = raw.decode("utf-8", errors="replace")
        data["preview"] = _safe_preview(text)
    elif path.name == ".skills_prompt_snapshot.json":
        data["preview"] = "Skills prompt snapshot present; body withheld from observability preview."
    elif path.name == "config.yaml":
        data["preview"] = "Profile config present; raw values withheld from observability preview."
    return data


def _file_kind(path: Path) -> str:
    name = path.name
    if name == "SOUL.md":
        return "soul"
    if name == "MEMORY.md":
        return "memory"
    if name == "USER.md":
        return "user_memory"
    if name == "AGENTS.md":
        return "project_context"
    if name == ".skills_prompt_snapshot.json":
        return "skills"
    if name == "config.yaml":
        return "profile_config"
    return "context_file"


def _safe_final_model_input(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    messages = value.get("messages") if isinstance(value.get("messages"), list) else []
    safe_messages = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = safe_assignment_text(message.get("content"), limit=65000) or ""
        safe_messages.append(
            {
                "role": safe_assignment_token(message.get("role")) or "message",
                "source": safe_assignment_token(message.get("source")) or "model_input",
                "content": content,
                "truncated": bool(message.get("truncated")),
                "bytes": _safe_int(message.get("bytes")),
                "sha256": safe_assignment_token(message.get("sha256")),
            }
        )
    return {
        "schema_version": _safe_int(value.get("schema_version")) or 1,
        "kind": safe_assignment_token(value.get("kind")) or "redaction_safe_final_model_input",
        "platform": safe_assignment_token(value.get("platform")),
        "profile": safe_assignment_token(value.get("profile")),
        "session_id": safe_assignment_text(value.get("session_id"), limit=200),
        "task_id": safe_assignment_token(value.get("task_id")),
        "skip_context_files": bool(value.get("skip_context_files")),
        "skip_memory": bool(value.get("skip_memory")),
        "system_message_supplied": bool(value.get("system_message_supplied")),
        "message_count": _safe_int(value.get("message_count")) or len(safe_messages),
        "messages": safe_messages,
        # Tool schemas ship in full on every API call and are the largest fixed
        # slice of the prompt after the system prompt. This whitelist previously
        # dropped them, which is why the context budget could not see (or
        # estimate) them at all.
        "tool_schema": _safe_tool_schema(value.get("tool_schema")),
    }


_TURN_USAGE_FIELDS = (
    "api_calls",
    "prompt_tokens",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "first_call_prompt_tokens",
)


def _safe_turn_usage(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """Key-whitelisted, int-coerced turn usage. All ints — nothing to redact."""
    if not isinstance(value, dict):
        return None
    safe = {field: _safe_int(value.get(field)) for field in _TURN_USAGE_FIELDS}
    if all(number is None for number in safe.values()):
        return None
    return safe


def turn_usage_from_result(result: Any) -> dict[str, int] | None:
    """Shape an ``AgentRunResult`` into the envelope's ``turn_usage`` block.

    Mission Control builds a fresh runtime per turn, so the result's totals ARE
    this turn's totals, and ``usage_ledger[0]`` is the turn's FIRST API call —
    the one whose prompt is exactly the assembled context (system + user + tool
    schemas) with no tool-result loop growth yet. That single number is what the
    context budget must show; the sums are what the message cost.

    Lives here, beside the envelope contract it feeds, rather than in the CLI
    command part — `hermes_cli/harness_parts/persona_commands.py` is exec'd into
    harness globals, so nothing defined there is importable or unit-testable.
    """
    if result is None:
        return None
    ledger = getattr(result, "usage_ledger", None)
    first_call_prompt: int | None = None
    if isinstance(ledger, list) and ledger and isinstance(ledger[0], dict):
        candidate = ledger[0].get("prompt_tokens")
        if isinstance(candidate, int) and candidate > 0:
            first_call_prompt = candidate

    def _count(name: str) -> int:
        value = getattr(result, name, None)
        return value if isinstance(value, int) and value > 0 else 0

    input_tokens = _count("input_tokens")
    cache_read_tokens = _count("cache_read_tokens")
    cache_write_tokens = _count("cache_write_tokens")
    usage = {
        "api_calls": _count("api_calls"),
        # prompt = input + cache_read + cache_write (CanonicalUsage's own
        # definition — input_tokens is already the uncached remainder).
        "prompt_tokens": input_tokens + cache_read_tokens + cache_write_tokens,
        "input_tokens": input_tokens,
        "output_tokens": _count("output_tokens"),
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "reasoning_tokens": _count("reasoning_tokens"),
        "first_call_prompt_tokens": first_call_prompt,
    }
    if not any(value for value in usage.values()):
        return None
    return usage


_SAFE_TOOL_NAME_LIMIT = 120


def _safe_tool_schema(value: Any) -> dict[str, Any] | None:
    """Redaction-safe tool-schema summary: names + count + wire size.

    Never carries the schema bodies (they can embed paths/enums); the byte size
    is what the context budget needs.
    """
    if not isinstance(value, dict):
        return None
    names: list[str] = []
    raw_names = value.get("final_model_tools")
    if isinstance(raw_names, list):
        for entry in raw_names[:_SAFE_TOOL_NAME_LIMIT]:
            token = safe_assignment_token(entry)
            if token:
                names.append(token)
    return {
        "schema_version": _safe_int(value.get("schema_version")) or 1,
        "kind": safe_assignment_token(value.get("kind")) or "actual_model_tools",
        "final_model_tools": names,
        "tool_count": _safe_int(value.get("tool_count")),
        "json_bytes": _safe_int(value.get("json_bytes")),
    }


def _safe_int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _chat_history_context(*, session_db: Any | None, session_id: str | None) -> list[dict[str, Any]]:
    if session_db is None or not session_id:
        return []
    try:
        messages = session_db.get_messages(session_id)
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for item in (messages or [])[-DEFAULT_CHAT_HISTORY_LIMIT:]:
        if not isinstance(item, dict):
            continue
        role = safe_assignment_token(item.get("role")) or "message"
        content = safe_assignment_text(item.get("content"), limit=SAFE_PREVIEW_LIMIT)
        if not content:
            continue
        rows.append(
            {
                "role": "operator" if role == "user" else role,
                "text": _safe_preview(content),
                "timestamp": safe_assignment_text(item.get("created_at") or item.get("timestamp"), limit=80),
                "source": "persona_chat_history",
            }
        )
    return rows


def _safe_preview(text: str) -> str:
    safe = safe_assignment_text(text, limit=SAFE_PREVIEW_LIMIT) or ""
    safe = safe.replace("\r\n", "\n").replace("\r", "\n")
    return safe[:SAFE_PREVIEW_LIMIT]
