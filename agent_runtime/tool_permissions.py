"""Chat-scoped tool permissions: the ONE place the runtime's default is decided.

Since the 2026-08-09 operator ruling (``docs/agent-runtime-harness/
archive/2026-08-22-pre-consolidation/UNBOUNDED_DEFAULT_PLAN_2026-08-09.md``) this module answers TWO questions that
used to be one:

* **What is the standing posture?** ``default_permission_mode()`` — the
  ``agent_runtime.tool_permissions.default_mode`` ROOT-config knob, shipping as
  ``unbounded``. Every consumer (chat toolsets, blocked names, MCP admission
  mode, terminal-envelope scope, HUD, previews) reaches it through
  :func:`permission_options_for_chat`, which is why flipping the default needed
  no persona or profile migration at all.
* **Did the operator narrow THIS session?** the :class:`ChatToolPermissionStore`
  record. The store survives the ruling INVERTED: it used to be the escalation
  lane ("grant this one chat unbounded for N turns") and is now the RESTRICTION
  lane ("hold this one chat at ``read_only``/``bounded`` for N turns"). A stored
  ``profile_default`` is NOT a restriction — it is the no-opinion sentinel the
  expiry writeback has been writing for months, and reading it as a pin would
  freeze every session that ever held a lapsed grant into the old bounded tier.
  See :mod:`agent_runtime.permission_modes`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from utils import atomic_json_write

from . import paths
from .models import AgentPersona
from .permission_modes import (
    FALLBACK_DEFAULT_PERMISSION_MODE,
    PERMISSION_MODE_BOUNDED,
    PERMISSION_MODE_PROFILE_DEFAULT,
    PERMISSION_MODE_READ_ONLY,
    PERMISSION_MODE_UNBOUNDED,
    RESTRICTION_PERMISSION_MODES,
    SHIPPED_DEFAULT_PERMISSION_MODE,
    SUPPORTED_PERMISSION_MODES,
    effective_permission_mode,
    normalize_permission_mode,
    permission_mode_is_restriction,
    permission_mode_is_unbounded,
)
from .tool_visibility import ToolVisibilityOptions, permission_state_for_persona

logger = logging.getLogger(__name__)

#: The mutating-tool set. ONE definition: ``read_only`` blocks exactly the tools
#: that cross the mutation boundary, so ``tool_visibility``'s mutation labelling
#: reads this constant (via its ``_mutating_tools()`` accessor) instead of
#: maintaining a second copy of the same 7 names — they drifted apart in two
#: files until 2026-08-09.
READ_ONLY_BLOCKS = frozenset({"apply_patch", "edit_file", "file.edit", "file.write", "patch", "terminal", "write_file"})

#: ``permission_source`` for an answer that came from the runtime default rather
#: than from any stored record. Replaces the old ``persona_role_policy``
#: spelling, which named an authority S61/S64 retired.
PERMISSION_SOURCE_RUNTIME_DEFAULT = "runtime_default"


@dataclass(slots=True)
class ChatToolPermission:
    persona_id: str
    session_id: str
    mode: str = PERMISSION_MODE_PROFILE_DEFAULT
    reason: str = ""
    source: str = "operator"
    updated_at: str = ""
    expires_at: str = ""
    turns_remaining: int | None = None
    expired: bool = False


class ChatToolPermissionStore:
    def __init__(self, path=None):
        self.path = path or paths.store_root() / "tool_permissions.json"

    def get(self, *, persona_id: str, session_id: str | None) -> ChatToolPermission | None:
        if not session_id:
            return None
        raw = self._read()
        item = raw.get(_key(persona_id, session_id))
        if not isinstance(item, dict):
            return None
        mode = normalize_permission_mode(item.get("mode")) or PERMISSION_MODE_PROFILE_DEFAULT
        if mode not in SUPPORTED_PERMISSION_MODES:
            # A stored token the runtime cannot read is corruption, not consent.
            # It clamps to the explicit BOUNDED restriction rather than to the
            # no-opinion sentinel: falling through to the (wide) runtime default
            # would let a damaged record silently widen a session.
            logger.warning(
                "Unrecognized stored permission mode %r for %s::%s; restricting to '%s'",
                item.get("mode"),
                persona_id,
                session_id,
                PERMISSION_MODE_BOUNDED,
            )
            mode = PERMISSION_MODE_BOUNDED
        record = ChatToolPermission(
            persona_id=str(item.get("persona_id") or persona_id),
            session_id=str(item.get("session_id") or session_id),
            mode=mode,
            reason=str(item.get("reason") or ""),
            source=str(item.get("source") or "operator"),
            updated_at=str(item.get("updated_at") or ""),
            expires_at=str(item.get("expires_at") or ""),
            turns_remaining=_optional_int(item.get("turns_remaining")),
        )
        if _permission_expired(record):
            return ChatToolPermission(
                persona_id=record.persona_id,
                session_id=record.session_id,
                mode=PERMISSION_MODE_PROFILE_DEFAULT,
                reason=record.reason,
                source=f"{record.source}:expired",
                updated_at=record.updated_at,
                expires_at=record.expires_at,
                turns_remaining=record.turns_remaining,
                expired=True,
            )
        return record

    def set(
        self,
        *,
        persona_id: str,
        session_id: str,
        mode: str,
        reason: str,
        source: str = "operator",
        expires_at: str | None = None,
        turns_remaining: int | None = None,
    ) -> ChatToolPermission:
        # Same fail-safe as ``get``: an unwritable mode clamps to the explicit
        # restriction, never to the no-opinion sentinel (which now means "give
        # this session the runtime default").
        requested = normalize_permission_mode(mode)
        resolved_mode = requested if requested in SUPPORTED_PERMISSION_MODES else PERMISSION_MODE_BOUNDED
        record = ChatToolPermission(
            persona_id=persona_id,
            session_id=session_id,
            mode=resolved_mode,
            reason=reason,
            source=source,
            updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            expires_at=str(expires_at or ""),
            turns_remaining=turns_remaining if turns_remaining is None else max(0, int(turns_remaining)),
        )
        raw = self._read()
        payload = {
            "persona_id": record.persona_id,
            "session_id": record.session_id,
            "mode": record.mode,
            "reason": record.reason,
            "source": record.source,
            "updated_at": record.updated_at,
        }
        if record.expires_at:
            payload["expires_at"] = record.expires_at
        if record.turns_remaining is not None:
            payload["turns_remaining"] = record.turns_remaining
        raw[_key(persona_id, session_id)] = payload
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(self.path, raw, indent=2, sort_keys=True)
        return record

    def consume_turn(self, *, persona_id: str, session_id: str | None) -> ChatToolPermission | None:
        if not session_id:
            return None
        raw = self._read()
        key = _key(persona_id, session_id)
        item = raw.get(key)
        if not isinstance(item, dict):
            return None
        record = self.get(persona_id=persona_id, session_id=session_id)
        if record is None:
            return None
        # Decrement for ANY mode that expresses an opinion, not just
        # ``unbounded``. The old condition was written when the store existed
        # only to ESCALATE, and it meant a turns-bounded ``read_only`` grant
        # never decremented and therefore never expired — a latent bug that
        # becomes the main path now that restriction is the store's purpose
        # (plan §3.5). ``profile_default`` is the no-opinion sentinel: it has
        # nothing to count down.
        if record.mode == PERMISSION_MODE_PROFILE_DEFAULT or record.turns_remaining is None:
            return record
        next_turns = max(0, record.turns_remaining - 1)
        if next_turns == 0:
            # Expiry writes the no-opinion sentinel, which now resolves to the
            # configured runtime default rather than pinning the session to the
            # bounded tier. The SOURCE distinguishes which direction lapsed so
            # an operator reading the store can still tell them apart.
            item["mode"] = PERMISSION_MODE_PROFILE_DEFAULT
            item["source"] = (
                "operator:restriction_expired"
                if permission_mode_is_restriction(record.mode)
                else "operator:elevation_expired"
            )
            item["turns_remaining"] = 0
        else:
            item["turns_remaining"] = next_turns
        item["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        raw[key] = item
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(self.path, raw, indent=2, sort_keys=True)
        return self.get(persona_id=persona_id, session_id=session_id)

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}


def default_permission_mode(cfg: Any | None = None) -> str:
    """The runtime-wide default chat permission mode. THE single config reader.

    Harness-wide operator policy, so it loads through
    ``config.load_root_runtime_config`` for the same reason the terminal-envelope
    grants and MCP admission do: a sticky-active profile's own ``config.yaml``
    must not be able to widen (or narrow) the posture of the whole runtime.

    Never raises. A config that will not load is a FAULT, and a fault resolves to
    the bounded tier — the same asymmetry the parser applies to an unknown value
    (``config._tool_permission_config``): the shipped default is the wide mode,
    but nothing the runtime failed to read may hand out more capability than the
    operator wrote.
    """

    try:
        resolved = cfg
        if resolved is None:
            from .config import load_root_runtime_config

            resolved = load_root_runtime_config()
        block = getattr(resolved, "tool_permissions", None)
        mode = normalize_permission_mode(getattr(block, "default_mode", "") if block else "")
    except Exception:  # pragma: no cover - defensive; a config fault must narrow
        logger.debug("tool-permission default config load failed; using bounded", exc_info=True)
        return FALLBACK_DEFAULT_PERMISSION_MODE
    if mode not in SUPPORTED_PERMISSION_MODES:
        # The parser already clamps; this is the belt for a hand-built config
        # object (tests, callers passing ``cfg=``) that never went through it.
        return FALLBACK_DEFAULT_PERMISSION_MODE
    return effective_permission_mode(mode)


def default_permission_mode_issues(cfg: Any | None = None) -> tuple[dict[str, str], ...]:
    """Typed config faults behind :func:`default_permission_mode`, if any."""

    try:
        resolved = cfg
        if resolved is None:
            from .config import load_root_runtime_config

            resolved = load_root_runtime_config()
        issues = getattr(getattr(resolved, "tool_permissions", None), "issues", ()) or ()
    except Exception:  # pragma: no cover - defensive
        return ()
    return tuple(issue for issue in issues if isinstance(issue, dict))


def _record_expresses_opinion(record: ChatToolPermission | None) -> bool:
    """Whether a stored record actually restricts (or elevates) this session.

    ``profile_default`` is the NO-OPINION sentinel — both as the dataclass
    default and as what ``consume_turn`` writes back when a grant lapses. The
    live ``tool_permissions.json`` is full of those writebacks; reading them as
    "operator pinned this session bounded" would have frozen every session that
    ever held a temporary grant out of the new runtime default, permanently.
    An explicit restriction is spelled ``bounded`` or ``read_only``.
    """

    if record is None:
        return False
    return normalize_permission_mode(record.mode) != PERMISSION_MODE_PROFILE_DEFAULT


def permission_options_for_chat(
    persona: AgentPersona,
    *,
    session_id: str | None,
    task_id: str | None = None,
    goal_id: str | None = None,
    runtime_root: str | None = None,
    store: ChatToolPermissionStore | None = None,
) -> ToolVisibilityOptions:
    """The ONE resolution of "what may this chat turn do".

    Record wins when it expresses an opinion; otherwise the configured runtime
    default answers with ``permission_source="runtime_default"``. Everything
    downstream — chat toolsets, blocked tool names, the MCP admission mode, the
    terminal-envelope scope, the HUD, ``persona tool-diff`` previews — reads this
    one answer.
    """

    record = (store or ChatToolPermissionStore()).get(persona_id=persona.id, session_id=session_id)
    if _record_expresses_opinion(record):
        mode = effective_permission_mode(record.mode)
        source = record.source
        expired = bool(record.expired)
        expires_at = record.expires_at or None
        turns_remaining = record.turns_remaining
    else:
        # No record, or a no-opinion / lapsed one: the runtime default answers,
        # and nothing session-scoped applies (a lapsed restriction's expiry
        # provenance stays on the store record, where the operator reads it).
        mode = default_permission_mode()
        source = PERMISSION_SOURCE_RUNTIME_DEFAULT
        expired = False
        expires_at = None
        turns_remaining = None
    return ToolVisibilityOptions(
        permission_mode=mode,
        permission_source=source,
        permission_expired=expired,
        session_id=session_id,
        task_id=task_id,
        goal_id=goal_id,
        runtime_root=runtime_root,
        blocked_tool_names=extra_blocked_tools_for_permission_mode(mode),
        expires_at=expires_at,
        turns_remaining=turns_remaining,
    )


def permission_state_for_chat(persona: AgentPersona, *, session_id: str | None) -> dict[str, Any]:
    return permission_state_for_persona(persona, permission_options_for_chat(persona, session_id=session_id))


def extra_blocked_tools_for_permission_mode(mode: str) -> list[str]:
    if effective_permission_mode(mode) == PERMISSION_MODE_READ_ONLY:
        return sorted(READ_ONLY_BLOCKS)
    return []


def _key(persona_id: str, session_id: str) -> str:
    return f"{persona_id.strip()}::{session_id.strip()}"


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _permission_expired(record: ChatToolPermission) -> bool:
    # The no-opinion sentinel has nothing to expire.
    if normalize_permission_mode(record.mode) == PERMISSION_MODE_PROFILE_DEFAULT:
        return False
    if record.turns_remaining is not None and record.turns_remaining <= 0:
        return True
    if not record.expires_at:
        return False
    raw = record.expires_at.strip()
    try:
        expires_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


#: Re-exported from :mod:`agent_runtime.permission_modes` so this module's public
#: API is unchanged for the CLI, the runtime lanes and the tests that already
#: import the mode vocabulary from here. The SPELLING lives in that leaf because
#: ``config`` / ``runtime_config`` / ``terminal_envelope`` also need it and none
#: of them may import this module (it pulls the whole registry stack in through
#: ``tool_visibility``).
__all__ = [
    "FALLBACK_DEFAULT_PERMISSION_MODE",
    "PERMISSION_MODE_BOUNDED",
    "PERMISSION_MODE_PROFILE_DEFAULT",
    "PERMISSION_MODE_READ_ONLY",
    "PERMISSION_MODE_UNBOUNDED",
    "PERMISSION_SOURCE_RUNTIME_DEFAULT",
    "READ_ONLY_BLOCKS",
    "RESTRICTION_PERMISSION_MODES",
    "SHIPPED_DEFAULT_PERMISSION_MODE",
    "SUPPORTED_PERMISSION_MODES",
    "ChatToolPermission",
    "ChatToolPermissionStore",
    "default_permission_mode",
    "default_permission_mode_issues",
    "effective_permission_mode",
    "extra_blocked_tools_for_permission_mode",
    "normalize_permission_mode",
    "permission_mode_is_restriction",
    "permission_mode_is_unbounded",
    "permission_options_for_chat",
    "permission_state_for_chat",
]
