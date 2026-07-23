"""Workspace-scoping authority for the addressable dispatch roster.

Single source of truth for one question: which persona instances are
ADVERTISED to, and BARE-PERSONA-RESOLVED for, a lane operating inside a given
Mission Control workspace. A persona may be placed into more than one workspace
scene; the Runtime Situation HUD and the mission-chat target resolver must only
offer / count the placements that belong to the SENDER's workspace, or a
two-agent order fans out across duplicate placements in other workspaces (live
2026-07-23: a Neko order for two agents landed on sibling placements in
unrelated workspaces).

Semantics (the whole contract lives here):

- An instance's ``workspace_id`` pointer of ``None`` means RUNTIME-GLOBAL: the
  canonical operator rows and any pre-pointer record carry no scene claim and
  are therefore visible/addressable in EVERY workspace.
- A non-None pointer is a "belongs to THIS workspace" claim: in scope only for
  the matching workspace.
- A scope of ``None`` (no active workspace at all) means everything is in scope
  — the resolver degrades to today's unscoped behaviour rather than hiding the
  whole roster.

This module scopes ADVERTISING and BARE-PERSONA resolution ONLY. It never
deletes, dedupes, or re-elects identity: explicit ``personainst_*`` targeting
stays allowed cross-workspace, and identity lookups (who steers whom) continue
to resolve against the full, unfiltered roster.

Pure and stdlib-only — no I/O, no harness imports — so the scoping table is
unit-testable in isolation (matches :mod:`agent_runtime.target_policy`).
"""

from __future__ import annotations

from typing import Any, Iterable


def _norm(value: Any) -> str | None:
    """Normalize a workspace id to a non-empty string, or ``None``.

    Whitespace-only and empty ids collapse to ``None`` (runtime-global), so a
    blank pointer is never mistaken for a distinct scene claim."""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def effective_workspace_id(
    instance: Any, *, active_workspace_id: str | None
) -> str | None:
    """The workspace a lane effectively operates in.

    An instance's own ``workspace_id`` pointer wins; a runtime-global instance
    (no pointer) falls back to the ``active_workspace_id`` it is being viewed
    from. Returns ``None`` only when neither is set (no scene at all)."""

    return _norm(getattr(instance, "workspace_id", None)) or _norm(active_workspace_id)


def instance_in_scope(
    candidate_workspace_id: str | None, scope_workspace_id: str | None
) -> bool:
    """True when a candidate placement is addressable from ``scope_workspace_id``.

    A runtime-global candidate (``None`` pointer) is visible in every workspace;
    a scene-bound candidate is visible only in its own workspace. When the scope
    itself is ``None`` (no active workspace), everything is in scope so the
    resolver never hides the whole roster."""

    scope = _norm(scope_workspace_id)
    if scope is None:
        return True
    candidate = _norm(candidate_workspace_id)
    return candidate is None or candidate == scope


def scope_roster(instances: Iterable[Any], *, scope_workspace_id: str | None) -> list:
    """Return the input roster filtered to the workspace scope, order preserved.

    Keeps every runtime-global row plus the rows bound to ``scope_workspace_id``;
    input order is preserved so the "On level" roster block and candidate
    enumeration stay stable. Never mutates or dedupes — a purely additive
    filter over addressing, not identity."""

    return [
        instance
        for instance in (instances or ())
        if instance_in_scope(getattr(instance, "workspace_id", None), scope_workspace_id)
    ]
