"""The permission-mode vocabulary, as a dependency-free leaf.

Why this module exists
----------------------
The mode names are read by three layers that cannot import each other:

* :mod:`agent_runtime.tool_permissions` — the resolution chokepoint (it imports
  ``tool_visibility``, which pulls the whole registry/toolset stack),
* :mod:`agent_runtime.runtime_config` / :mod:`agent_runtime.config` — the config
  dataclass + parser for ``agent_runtime.tool_permissions.default_mode``, which
  must stay cheap and import-light, and
* :mod:`agent_runtime.terminal_envelope` — the per-command decision, imported by
  config/policy readers that must not drag the registry in.

A string literal repeated in three files is exactly the drift this codebase has
paid for elsewhere, and the obvious fix (import the constants from
``tool_permissions``) closes an import cycle: ``tool_permissions`` already
imports ``tool_visibility`` at module scope, so nothing on that chain may import
``tool_permissions`` back at module scope. This leaf owns the spelling; every
layer imports it from here. ``tool_permissions`` re-exports the names so its
public API is unchanged.

The MODES themselves
--------------------
``profile_default`` is the historical bounded tier AND, as a stored record, the
sentinel meaning **"no opinion — defer to the configured runtime default"**.
That dual role is load-bearing: the live ``tool_permissions.json`` contains
expiry writebacks that wrote ``profile_default`` to mean "the grant lapsed", and
under an unbounded default those stale rows must not silently pin those sessions
to the bounded tier forever (see ``archive/2026-08-22-pre-consolidation/UNBOUNDED_DEFAULT_PLAN_2026-08-09.md`` §3.1).

``bounded`` is therefore the EXPLICIT spelling an operator uses to restrict a
session to the old bounded tier. It resolves exactly as ``profile_default`` used
to — :func:`effective_permission_mode` maps it onto ``profile_default`` so every
downstream consumer, wire field and launcher surface keeps the vocabulary it
already reads — but as a STORED record it is unambiguous: an operator
restriction, never a lapsed grant.
"""

from __future__ import annotations

#: The historical bounded tier. As a STORED record it means "no opinion".
PERMISSION_MODE_PROFILE_DEFAULT = "profile_default"
#: Explicit operator restriction to the bounded tier (§3.5 option (a)).
PERMISSION_MODE_BOUNDED = "bounded"
#: Bounded tier plus the mutating-tool block set (``READ_ONLY_BLOCKS``) and the
#: reviewer-shaped MCP narrowing.
PERMISSION_MODE_READ_ONLY = "read_only"
#: Full tool access. The shipped runtime default per the 2026-08-09 ruling.
PERMISSION_MODE_UNBOUNDED = "unbounded"

SUPPORTED_PERMISSION_MODES = frozenset(
    {
        PERMISSION_MODE_PROFILE_DEFAULT,
        PERMISSION_MODE_BOUNDED,
        PERMISSION_MODE_READ_ONLY,
        PERMISSION_MODE_UNBOUNDED,
    }
)

#: Modes that express an operator RESTRICTION when stored on a session record.
#: ``profile_default`` is deliberately absent — it is the no-opinion sentinel.
RESTRICTION_PERMISSION_MODES = frozenset(
    {PERMISSION_MODE_BOUNDED, PERMISSION_MODE_READ_ONLY}
)

#: What ``agent_runtime.tool_permissions.default_mode`` ships as. Operator
#: ruling 2026-08-09: full tool access is the standing posture.
SHIPPED_DEFAULT_PERMISSION_MODE = PERMISSION_MODE_UNBOUNDED

#: Where a config FAULT lands. Deliberately not the shipped default: a config
#: the runtime could not parse must never resolve to MORE capability than the
#: operator wrote, so the fallback is the bounded tier even though the shipped
#: default is unbounded.
FALLBACK_DEFAULT_PERMISSION_MODE = PERMISSION_MODE_PROFILE_DEFAULT


def normalize_permission_mode(mode: str | None) -> str:
    """Trim/lowercase a mode token. Unknown values are returned as-is (empty
    when blank) — callers decide whether an unknown token is a fault or a
    fall-through, so nothing is silently coerced here."""

    return str(mode or "").strip().lower()


def effective_permission_mode(mode: str | None) -> str:
    """The mode the RUNTIME enforces for a stored/configured token.

    Collapses the ``bounded`` alias onto ``profile_default`` (they enforce
    identically) so the wire vocabulary consumers already parse — the launcher
    HUD strip, ``permission_state``, the MCP admission argument — never grows a
    fourth spelling it would have to learn.
    """

    text = normalize_permission_mode(mode)
    if text == PERMISSION_MODE_BOUNDED:
        return PERMISSION_MODE_PROFILE_DEFAULT
    return text


def permission_mode_is_unbounded(mode: str | None) -> bool:
    return normalize_permission_mode(mode) == PERMISSION_MODE_UNBOUNDED


def permission_mode_is_restriction(mode: str | None) -> bool:
    """Whether a STORED mode expresses an operator restriction.

    ``unbounded`` is not a restriction; ``profile_default`` is not one either
    (it is the no-opinion sentinel). Used by the session store to decide whether
    a turns-bounded record decrements and how its expiry is labelled.
    """

    return normalize_permission_mode(mode) in RESTRICTION_PERMISSION_MODES


__all__ = [
    "FALLBACK_DEFAULT_PERMISSION_MODE",
    "PERMISSION_MODE_BOUNDED",
    "PERMISSION_MODE_PROFILE_DEFAULT",
    "PERMISSION_MODE_READ_ONLY",
    "PERMISSION_MODE_UNBOUNDED",
    "RESTRICTION_PERMISSION_MODES",
    "SHIPPED_DEFAULT_PERMISSION_MODE",
    "SUPPORTED_PERMISSION_MODES",
    "effective_permission_mode",
    "normalize_permission_mode",
    "permission_mode_is_restriction",
    "permission_mode_is_unbounded",
]
