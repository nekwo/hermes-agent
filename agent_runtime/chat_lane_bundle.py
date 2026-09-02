"""One chat-lane visibility resolve per turn, reused across turns by IDENTITY.

Why this module exists
----------------------
A mission-chat turn asks "what may this chat do?" at least four times, and every
asker walked the whole resolution independently:

* ``build_mission_chat_turn_context`` → ``capability_block`` (→
  ``chat_lane_capability_drops``) and ``admission_line``;
* the same builder's ``mission_chat_runtime_signature`` → ``tool_contract`` (→
  ``_enabled_toolsets_for_chat`` + ``_blocked_tool_names_for_chat``) and
  ``permission_state`` (→ ``resolve_tool_visibility``);
* ``GPTPersonaRuntime.mission_chat_reply`` → ``_enabled_toolsets_for_chat`` and
  ``_blocked_tool_names_for_chat`` again, for the request it assembles.

Each of those walks ``permission_options_for_chat`` →
``effective_toolsets``/``all_registered_toolsets`` → the registry's ``check_fn``
sweep. On the live ``unbounded`` default the sweep is the expensive one:
``all_registered_toolsets()`` asks ``_toolset_has_exposable_tools`` about every
registered toolset, and each answer that misses the 30 s probe cache costs a
subprocess spawn or a socket dial. Measured on a live turn (2026-08-23T14:34:57Z,
six minutes after a serve boot): ``registry_probe_rounds=27`` inside a 1,313 ms
``request_received → context_built`` span — the same sweep, paid three times
over, on a turn where nothing about the chat had changed since the previous one.

The 15/30 s TTL memos underneath (``tool_visibility._PROFILE_READINESS_TTL_SECONDS``,
``tools.registry._CHECK_FN_TTL_SECONDS``) are tuned for one snapshot BUILD, not
for operator cadence: two consecutive operator messages are minutes apart, so
every turn re-pays the full sweep. Raising those constants would trade the storm
for a staleness window on every consumer in the process. This module removes the
re-composition instead.

What is cached, and what is deliberately NOT
--------------------------------------------
**The composition, never the probes.** The ``check_fn`` cache, its
transient-failure grace window and its re-probe backoff (``tools/registry.py``)
are untouched: a probe that runs still runs exactly as it did, and a genuinely
down backend still strips its tools from the schema the agent is handed, because
``registry.get_definitions`` re-checks every ``check_fn`` at agent-construction
time on its own TTL. What this module stops repeating is the *assembly* of one
chat lane's answer out of those probes.

The key is the bundle's own inputs — not a clock:

* **persona revision** — a content hash of the persona record. Covers its
  declared toolsets, its skills, its ``required_mcp_servers``, its profile, its
  id (which is what the per-persona restore knob keys on).
* **session id + permission fingerprint** — mode, source, expiry, remaining
  turns and the mode's extra blocks, read FRESH on every lookup from
  ``permission_options_for_chat`` (a small JSON read, no probes). This is what
  makes a mid-chat permission change safe: a grant that
  ``ChatToolPermissionStore.consume_turn`` decremented after the previous turn
  produces a different fingerprint on this one, so the bundle is rebuilt rather
  than served. It is also what keeps an ``unbounded`` bundle — which resolves
  ``all_registered_toolsets()`` — from ever being served to a bounded turn.
* **root + active ``config.yaml`` revision** — ``(mtime_ns, size)`` of both
  files. The root config owns the permission default, the MCP admission kill
  switch and ``chat_lane_restore_toolsets``; the active one is what
  ``load_agent_runtime_config()`` reads. Cheaper than re-hashing a parsed config
  and keyed on exactly what the config loader's own mtime cache keys on.
* **runtime root + entry-point lane** — both are inputs to the resolved
  permission state's ``requirement_failures``.
* **registry epoch** (``tools.registry.registry_epoch``) — every registration
  change (including every MCP dynamic refresh) and every explicit
  ``invalidate_check_fn_cache``.

**The instance revision is deliberately absent**, and that is not an oversight:
no component here reads the persona INSTANCE. An instance edit (``set-model``,
a placement move, the per-turn ``skill_manifest_hash`` writeback) changes
``mission_chat_turn_context.mission_chat_runtime_signature``, which folds the instance
directly and is computed per turn from this bundle's OUTPUT — so a resident
actor still stops being reusable exactly when it should. Folding the instance in
here would only cost hit rate on every turn, because the manifest hash is
rewritten each turn.

The staleness surface this moves, stated plainly
------------------------------------------------
Before: any change to tool availability surfaced within 30 s, everywhere.
After: within the turn path, it surfaces when the epoch moves or when one of the
keyed inputs changes. The residue — a change nobody announced — is:

* a backend that goes down without anything calling ``invalidate_check_fn_cache``
  keeps its TOOLSET listed on the chat lane's accounting until the epoch moves.
  Its TOOLS still vanish from the model's schema at construction (the definitions
  pass re-probes), so the agent cannot call what is not there; what goes stale is
  the name in the enabled-toolset list and in the operator's permission preview.
* an on-disk edit to a persona PROFILE's MCP declaration that changes neither
  ``config.yaml`` nor the registry. Registering such a server bumps the epoch, so
  this is bounded to the window before registration.

Everything else — permission changes, persona edits, config edits, tool
registration, MCP refresh, ``hermes tools enable`` — is in the key or bumps the
epoch. :func:`invalidate_chat_lane_bundles` is the explicit escape hatch.

Scope
-----
The MISSION-CHAT TURN PATH only: ``mission_chat_turn_context``'s resolver
defaults and ``persona_runtime.GPTPersonaRuntime.mission_chat_reply``. The
authorities themselves (``_enabled_toolsets_for_chat``,
``chat_lane_capability_drops``, ``permission_state_for_chat``, …) are called by
this module and are otherwise untouched, so the operator preview lane
(``apply_chat_lane_tool_scope``), the snapshot builder and ``persona_prewarm``
keep resolving live. That boundary is deliberate: those callers are routinely
driven with a monkeypatched resolver rather than a changed config, which a
config-keyed memo cannot see.
"""

from __future__ import annotations

import copy
import logging
import os
import threading
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from .cli_format import emit_json

logger = logging.getLogger(__name__)

#: Bumped by hand when the COMPOSITION changes shape (a field added, a component
#: re-pointed at a different authority) rather than any one input. It is how a
#: deployment that reuses a warm process across a code change cannot serve a
#: bundle built by the previous contract.
BUNDLE_CONTRACT_REVISION = "chat-lane-bundle-v1"

#: One entry per live (persona, chat root). Bounded by REPLACEMENT rather than
#: by eviction: a root whose key changed overwrites its own entry, so the dict
#: grows with the number of chats an operator has open in one serve, not with
#: the number of turns they take. The cap is the belt for a process that somehow
#: sees thousands of roots; hitting it clears rather than evicts, because a
#: partial clear needs an ordering this cache has no reason to maintain.
_MEMO_MAX_ROOTS = 256

_memo: dict[tuple[str, str], tuple[str, "ChatLaneBundle"]] = {}
_memo_lock = threading.Lock()

#: THREAD-LOCAL, and cumulative for the life of the thread — the same shape and
#: the same reasoning as ``tools.registry.probe_rounds_this_thread``. ``harness
#: serve`` runs concurrent turns in one process on pooled threads, so a global
#: counter would attribute another turn's build to whichever turn sampled around
#: it, and a counter this module RESET would let two overlapping observers
#: destroy each other's measurement. Callers take a difference across their own
#: window (see ``persona_commands._visibility_bundle_builds``).
_build_state = threading.local()


def bundle_builds_this_thread() -> int:
    """Bundles this THREAD has BUILT (as opposed to reused) since process start.

    The receipt for "one visibility resolve per turn": a warm steady-state turn
    of an unchanged chat should move this by ``0``, and a turn that moves it by
    more than ``1`` is re-resolving something the bundle was supposed to hold.
    """

    return int(getattr(_build_state, "builds", 0))


def _note_bundle_build() -> None:
    _build_state.builds = int(getattr(_build_state, "builds", 0)) + 1


@dataclass(frozen=True, slots=True)
class ChatLaneBundle:
    """One chat lane's resolved visibility, as of one identity.

    Stored canonically (tuples, plain dicts) and handed out through the
    accessors below, which COPY every mutable payload. Sharing the stored dict
    would make any consumer that decorates its copy — the situational HUD folds
    the capability account into itself, the observability row records it — a
    silent writer into everyone else's cache entry.
    """

    key: str
    permission_mode: str
    permission_source: str
    admission: Any
    enabled_toolsets: tuple[str, ...]
    blocked_tool_names: tuple[str, ...]
    operating_skills: tuple[str, ...]
    admission_line: str
    _capability: dict[str, Any]
    _tool_contract: dict[str, Any]
    _permission_state: dict[str, Any]
    #: False when a best-effort component faulted. Such a bundle is served to
    #: THIS caller (degraded exactly as the uncached path degrades) and then
    #: thrown away — pinning a degraded account until the next epoch bump is how
    #: one transient fault becomes a permanent wrong answer.
    complete: bool = True
    #: WHICH best-effort components faulted, as ``<component>:<ExceptionClass>``
    #: — the class, never the message, the same disclosure rule this runtime's
    #: other receipts follow.
    #:
    #: ``complete`` alone says only THAT something degraded, and its most visible
    #: consequence is invisible: an incomplete bundle is not memoized, so the
    #: next lookup rebuilds and a caller measuring reuse sees "the bundle was not
    #: reused" with nothing to say why. That is not hypothetical — it is the
    #: shape of the one intermittent red this cache has produced, where the only
    #: diagnosis available named the bundle KEY (which had not moved). The
    #: degraded components were in a debug log line that nobody was capturing.
    degraded: tuple[str, ...] = ()

    def capability(self) -> dict[str, Any]:
        return copy.deepcopy(self._capability)

    def tool_contract(self) -> dict[str, Any]:
        return copy.deepcopy(self._tool_contract)

    def permission_state(self) -> dict[str, Any]:
        return copy.deepcopy(self._permission_state)


# ── identity ─────────────────────────────────────────────────────────────────


def _revision(value: Any) -> str:
    import hashlib

    plain = asdict(value) if is_dataclass(value) and not isinstance(value, type) else value
    return hashlib.sha256(emit_json(plain).encode("utf-8")).hexdigest()


def _path_revision(path: Any) -> str:
    """``(mtime_ns, size)`` of a config file, or a typed sentinel.

    A missing file is a real, stable state ("no root config") and must key as
    itself; an unreadable one is NOT — it is keyed as a fresh identity so a
    permissions fault cannot pin whatever the last readable state resolved to.
    """

    try:
        stat = os.stat(str(path))
    except FileNotFoundError:
        return "absent"
    except OSError:
        return f"unreadable:{os.urandom(8).hex()}"
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _config_revisions() -> tuple[str, str]:
    from hermes_constants import get_config_path

    from .config import harness_root_config_path

    try:
        root = _path_revision(harness_root_config_path())
    except Exception:  # pragma: no cover - defensive; an unresolvable home
        root = f"unresolved:{os.urandom(8).hex()}"
    try:
        active = _path_revision(get_config_path())
    except Exception:  # pragma: no cover - defensive; an unresolvable home
        active = f"unresolved:{os.urandom(8).hex()}"
    return root, active


def chat_lane_bundle_key_material(
    persona: Any, permission: Any, *, session_id: str | None
) -> dict[str, Any]:
    """The key's COMPONENTS, before they are hashed into one digest.

    Public because a digest is unreadable evidence: a test (or an operator
    diagnosing a cache that will not hold) needs to see WHICH input moved, not
    that some input did. :func:`chat_lane_bundle_key` is exactly
    ``sha256(this)``.
    """

    from tools.registry import registry_epoch

    from . import paths
    from .mcp_lane import current_entry_point_lane

    root_config, active_config = _config_revisions()
    try:
        runtime_root = str(paths.store_root())
    except Exception:  # pragma: no cover - defensive
        runtime_root = ""
    try:
        lane = str(current_entry_point_lane() or "")
    except Exception:  # pragma: no cover - current_entry_point_lane never raises
        lane = ""
    return {
        "contract": BUNDLE_CONTRACT_REVISION,
        "persona_revision": _revision(persona),
        "session": str(session_id or ""),
        "permission": {
            "mode": str(getattr(permission, "permission_mode", "") or ""),
            "source": str(getattr(permission, "permission_source", "") or ""),
            "expired": bool(getattr(permission, "permission_expired", False)),
            "expires_at": getattr(permission, "expires_at", None),
            "turns_remaining": getattr(permission, "turns_remaining", None),
            "extra_blocked": sorted(
                str(name) for name in (getattr(permission, "blocked_tool_names", None) or [])
            ),
        },
        "root_config_revision": root_config,
        "active_config_revision": active_config,
        "runtime_root": runtime_root,
        "entry_point_lane": lane,
        "registry_epoch": int(registry_epoch()),
    }


def chat_lane_bundle_key(persona: Any, permission: Any, *, session_id: str | None) -> str:
    """The bundle's identity. Every input the composition actually reads.

    Read the module docstring for what is in here and — as importantly — what
    is deliberately not.
    """

    return _revision(
        chat_lane_bundle_key_material(persona, permission, session_id=session_id)
    )


# ── the bundle ───────────────────────────────────────────────────────────────


def chat_lane_bundle(persona: Any, *, session_id: str | None) -> ChatLaneBundle:
    """This chat lane's visibility, resolved once and reused while it holds.

    Never raises anything the uncached path would not have raised: the HARD
    components (permission mode, MCP admission, enabled toolsets, blocked tool
    names, tool contract, permission state) propagate exactly as they do today,
    because each of them decides what the turn SHIPS and a fabricated fallback
    there would be a capability answer nobody resolved. The best-effort
    components (capability account, admission line, operating manuals) degrade
    to the same values their own call sites degrade to, and mark the bundle
    incomplete so it is not stored.
    """

    from .tool_permissions import permission_options_for_chat

    permission = permission_options_for_chat(persona, session_id=session_id)
    key = chat_lane_bundle_key(persona, permission, session_id=session_id)
    root = (str(getattr(persona, "id", "") or ""), str(session_id or ""))

    with _memo_lock:
        cached = _memo.get(root)
    if cached is not None and cached[0] == key:
        return cached[1]

    bundle = _build_bundle(persona, session_id=session_id, permission=permission, key=key)
    if bundle.complete:
        with _memo_lock:
            if len(_memo) >= _MEMO_MAX_ROOTS and root not in _memo:
                _memo.clear()
            _memo[root] = (key, bundle)
    return bundle


def _build_bundle(
    persona: Any, *, session_id: str | None, permission: Any, key: str
) -> ChatLaneBundle:
    from .mcp_admission import LANE_MISSION_CHAT, resolve_mcp_admission
    from .persona_runtime import (
        _blocked_tool_names_for_chat,
        _enabled_toolsets_for_chat,
        mission_chat_admission_line,
        mission_chat_operating_skills,
    )
    from .runtime_hud import capability_block_for_persona
    from .tool_permissions import permission_state_for_chat

    _note_bundle_build()
    mode = str(getattr(permission, "permission_mode", "") or "")

    # ONE admission resolve for the whole turn. It was already resolved once per
    # turn in ``mission_chat_reply`` and threaded into the toolset scope so "the
    # tools the turn asks for" and "the servers the runner registers" could not
    # disagree; resolving it here keeps that property and extends it to the
    # context builder's line and manuals.
    admission = resolve_mcp_admission(
        persona, lane=LANE_MISSION_CHAT, permission_mode=mode
    )
    enabled = tuple(
        _enabled_toolsets_for_chat(persona, session_id=session_id, admission=admission)
    )
    blocked = tuple(_blocked_tool_names_for_chat(persona, session_id=session_id))
    # The tool contract IS these two lists — composed here rather than by a
    # second call to ``chat_runtime_tool_contract`` so the actor's reuse key and
    # the request the actor is built from are literally the same answer, not two
    # equal ones.
    tool_contract = {
        "enabled_toolsets": list(enabled),
        "blocked_tool_names": list(blocked),
    }
    permission_state = permission_state_for_chat(persona, session_id=session_id)

    # Every degradation is RECORDED, not only counted into ``complete``. See
    # ``ChatLaneBundle.degraded``: an incomplete bundle is never memoized, so the
    # component that faulted here is the whole explanation for a rebuild that a
    # reuse measurement two layers up can otherwise only report as "the object
    # changed".
    degraded: list[str] = []
    try:
        capability = capability_block_for_persona(persona, session_id=session_id) or {}
    except Exception as exc:
        logger.debug("chat-lane capability account unavailable for this turn", exc_info=True)
        capability = {}
        degraded.append(f"capability_account:{type(exc).__name__}")
    try:
        admission_line = str(
            mission_chat_admission_line(persona, session_id=session_id) or ""
        )
    except Exception as exc:
        logger.debug("MCP admission line unavailable for this turn", exc_info=True)
        admission_line = ""
        degraded.append(f"admission_line:{type(exc).__name__}")
    try:
        operating_skills = tuple(
            mission_chat_operating_skills(persona, session_id=session_id) or ()
        )
    except Exception as exc:
        logger.debug("admitted MCP operating skills unavailable for this turn", exc_info=True)
        operating_skills = ()
        degraded.append(f"operating_skills:{type(exc).__name__}")

    return ChatLaneBundle(
        key=key,
        permission_mode=mode,
        permission_source=str(getattr(permission, "permission_source", "") or ""),
        admission=admission,
        enabled_toolsets=enabled,
        blocked_tool_names=blocked,
        operating_skills=operating_skills,
        admission_line=admission_line,
        _capability=capability,
        _tool_contract=tool_contract,
        _permission_state=permission_state if isinstance(permission_state, dict) else {},
        complete=not degraded,
        degraded=tuple(degraded),
    )


def invalidate_chat_lane_bundles() -> None:
    """Drop every memoized bundle. The explicit escape hatch.

    Real API, not a test hook: anything that changes chat-lane visibility in a
    way neither the key nor ``tools.registry.registry_epoch`` can see calls
    this. Cheap — the next lookup rebuilds one bundle per live chat root.
    """

    with _memo_lock:
        _memo.clear()


__all__ = [
    "BUNDLE_CONTRACT_REVISION",
    "ChatLaneBundle",
    "bundle_builds_this_thread",
    "chat_lane_bundle",
    "chat_lane_bundle_key",
    "chat_lane_bundle_key_material",
    "invalidate_chat_lane_bundles",
]
