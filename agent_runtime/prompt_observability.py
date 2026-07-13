from __future__ import annotations

import hashlib
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
    trace_events: Iterable[dict[str, Any]] | None = None,
    prompt_mode: str = "normal_hermes_profile_chat",
    workspace_id: str | None = None,
    workspace_name: str | None = None,
    workspace_agents: WorkspaceAgentsContext | None = None,
    mission_hud: dict[str, Any] | None = None,
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
        "accessible_skills": accessible_skills,
        "available_skills": available_skills,
        "skills_catalog": available_skills,
        "skills": accessible_skills,
        "chat_history_context": history,
        "retrieval_context": [],
        "final_model_input": _safe_final_model_input(final_model_input),
        "model_selection": _safe_model_selection(model_selection),
        "context_budget": _context_budget(model_selection, final_model_input),
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


def snapshot_prompt_observability(
    *,
    personas: Iterable[Any],
    persona_instances: Iterable[Any],
    session_db: Any | None = None,
    tasks: Iterable[Any] | None = None,
    proof_store: Any | None = None,
) -> dict[str, Any]:
    # Deferred import: context_builder pulls a large dependency graph, and this
    # module is imported very early. A function-local import keeps module load
    # order robust while still giving the preview a single-authority HUD builder.
    from .context_builder import mission_hud_preview

    tasks_by_id = {
        safe_assignment_token(getattr(task, "id", None)): task
        for task in (tasks or [])
        if safe_assignment_token(getattr(task, "id", None))
    }

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

    contexts: list[dict[str, Any]] = []
    by_persona = {
        safe_assignment_token(getattr(persona, "id", None)): persona
        for persona in personas
        if safe_assignment_token(getattr(persona, "id", None))
    }
    for instance in persona_instances:
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
    return {
        "schema_version": 1,
        "default_flow": {
            "id": "neko_two_dev_default",
            "lead": "neko_supervisor",
            "dev_specialists": ["backend_dev", "dev"],
            "qa_default": False,
        },
        "surface_prompt_default": "",
        "chat_contexts": _merge_latest_contexts(contexts, session_db=session_db),
    }


def persist_prompt_observability_context(context: dict[str, Any]) -> None:
    context_id = safe_assignment_token(context.get("context_id"))
    if not context_id:
        return
    root = paths.prompt_observability_dir()
    root.mkdir(parents=True, exist_ok=True)
    atomic_json_write(root / f"{context_id}.json", to_jsonable(context), indent=2, sort_keys=True)


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


def _merge_latest_contexts(
    contexts: list[dict[str, Any]],
    *,
    session_db: Any | None = None,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def key_for(item: dict[str, Any]) -> tuple[str, str, str]:
        return (
            safe_assignment_token(item.get("persona_instance_id")) or "",
            safe_assignment_text(item.get("session_id"), limit=200) or "",
            safe_assignment_token(item.get("persona_id")) or "",
        )

    built_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in contexts:
        built_by_key.setdefault(key_for(item), item)

    for item in load_latest_prompt_observability_contexts():
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
    """Add `skills` / `context_budget` to persisted contexts that predate them.

    Persisted observability rows are written at chat time, so rows captured
    before these fields existed lack them. Backfill skills from the freshly
    built context (correct per-persona set) or the profile snapshot, and
    recompute the budget from the row's own model selection + final input.
    """
    if built:
        # Persisted rows are written at chat time and never carry the typed
        # mission-HUD preview (chat turns don't compute one). Prefer the freshly
        # built preview so the persisted row exposes the same upcoming-turn HUD.
        built_hud = built.get("mission_hud")
        if isinstance(built_hud, dict) and built_hud and not item.get("mission_hud"):
            item["mission_hud"] = built_hud
        for key in (
            "used_skills",
            "accessible_skills",
            "available_skills",
            "skills_catalog",
            "skills",
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
    if not item.get("accessible_skills") and item.get("skills"):
        item["accessible_skills"] = item.get("skills")
    if item.get("used_skills") is None:
        item["used_skills"] = used_skills_context(
            final_model_input=item.get("final_model_input")
        )
    if not item.get("accessible_skills") or _profile_prompt_skills_need_snapshot(item):
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
            item["skills"] = item["accessible_skills"]
            item["available_skills"] = available_skills_context(
                accessible_skills=item["accessible_skills"]
            )
            item["skills_catalog"] = item["available_skills"]
    elif item.get("skills") is None:
        item["skills"] = item["accessible_skills"]
    if item.get("available_skills") is None:
        item["available_skills"] = available_skills_context(
            accessible_skills=item.get("accessible_skills") or item.get("skills") or []
        )
        item["skills_catalog"] = item["available_skills"]
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


def _estimate_used_tokens(final_model_input: dict[str, Any] | None) -> int | None:
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
    if total_bytes <= 0:
        return None
    return max(1, total_bytes // 4)


def _context_budget(
    model_selection: dict[str, Any] | None,
    final_model_input: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Per-model context budget for the Sent-to-model bar: window + compaction line.

    Returns None (UI omits the bar) when the model/window can't be resolved.
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
    return {
        "model": safe_assignment_text(str(model), limit=120),
        "provider": safe_assignment_token(provider) if provider else None,
        "window_tokens": int(window),
        "compaction_ratio": round(float(ratio), 4),
        "compaction_tokens": int(window * ratio),
        "used_tokens": _estimate_used_tokens(final_model_input),
        "used_estimated": True,
    }


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
