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
re-elects identity: explicit ``personainst_*`` targeting stays allowed
cross-workspace, and identity lookups (who steers whom) continue to resolve
against the full, unfiltered roster.

On top of workspace scoping it also owns two canonical-row rules for the
addressable view:

- "Global canonicals are not on the level" (:func:`exclude_global_canonicals`):
  a persona's auto-derived CANONICAL operator row (runtime plumbing, no scene
  claim) is NEVER advertised into a REAL workspace scope. Instance = in-level
  placement (operator ruling 2026-07-18); the old "reachability fallback" that
  kept unplaced canonicals addressable everywhere made an agent's Runtime
  Situation HUD advertise personas that were not on its level (live 2026-07-24:
  a level with two placements advertised five agents, and the agent's fan-out
  message to the unplaced ones was refused). A canonical row that DOES carry a
  workspace pointer follows that pointer like any other row; with no active
  workspace at all (scope ``None``) the exclusion is off — the degrade path
  never hides the whole roster.
- "Placements shadow canonical" (:func:`shadow_canonical_by_placement`): when
  an in-scope placement of a persona exists, that persona's canonical row is
  NOT advertised to — nor bare-persona-resolved for — an AGENT, so a bare
  persona id lands on the deliberate placement instead of the plumbing row.
  Under a real scope the exclusion above already removed global canonicals;
  the shadow still de-dupes pointer-bearing canonicals and the ``None``-scope
  degrade path.

NOTE the deliberate residual asymmetry: bare-persona SEND resolution still
falls back to the canonical channel when a persona has no in-scope placement
(``_mission_chat_bare_persona_target`` / ``agent_chat_tool``) — retiring that
fallback is gated on the global-row adoption migration. This module's change
narrows what is ADVERTISED/LISTED; it does not add refusals.

Both rules are properties of the ADDRESSABLE roster only; identity/steering
lists (``identity_roster``) always read the full, unshadowed set. Each is a
SEPARATE pure helper rather than folded into :func:`scope_roster`, whose
pinned contract is a purely additive filter that never drops rows.

Pure and stdlib-only — no I/O, no harness imports — so the scoping table is
unit-testable in isolation (matches :mod:`agent_runtime.target_policy`). The
canonical discriminator is taken as a passed predicate so this module never
imports the harness row model; callers hand in
``agent_runtime.persona_assignments.is_canonical_persona_channel``.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from .serde import optional_text


def effective_workspace_id(
    instance: Any, *, active_workspace_id: str | None
) -> str | None:
    """The workspace a lane effectively operates in.

    An instance's own ``workspace_id`` pointer wins; a runtime-global instance
    (no pointer) falls back to the ``active_workspace_id`` it is being viewed
    from. Returns ``None`` only when neither is set (no scene at all)."""

    return optional_text(getattr(instance, "workspace_id", None)) or optional_text(active_workspace_id)


def instance_in_scope(
    candidate_workspace_id: str | None, scope_workspace_id: str | None
) -> bool:
    """True when a candidate placement is addressable from ``scope_workspace_id``.

    A runtime-global candidate (``None`` pointer) is visible in every workspace;
    a scene-bound candidate is visible only in its own workspace. When the scope
    itself is ``None`` (no active workspace), everything is in scope so the
    resolver never hides the whole roster."""

    scope = optional_text(scope_workspace_id)
    if scope is None:
        return True
    candidate = optional_text(candidate_workspace_id)
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


def exact_scoped_instance_ids(
    instances: Iterable[Any], *, workspace_id: str | None
) -> list[str]:
    """Ids of live-store rows that explicitly belong to ``workspace_id``.

    This is intentionally stricter than :func:`scope_roster`: runtime-global
    canonical rows and fallback visibility do not represent an agent placed in
    a workspace. Callers pass the active ``PersonaInstanceStore.list_all``
    rows, whose archive-never-delete retirement contract already excludes
    retired instances from the live directory.
    """

    wanted = optional_text(workspace_id)
    if wanted is None:
        return []
    return sorted(
        {
            instance_id
            for instance in (instances or ())
            if optional_text(getattr(instance, "workspace_id", None)) == wanted
            and (instance_id := optional_text(getattr(instance, "id", None))) is not None
        }
    )


def _persona_key(instance: Any) -> Any:
    """Default persona grouping key: the row's ``persona_id`` (or ``None``)."""

    return getattr(instance, "persona_id", None)


def shadow_canonical_by_placement(
    instances: Iterable[Any],
    *,
    is_canonical: Callable[[Any], bool],
    persona_key: Callable[[Any], Any] = _persona_key,
) -> list:
    """Drop each persona's CANONICAL row when a PLACEMENT of it is in the list.

    "Placements shadow canonical": a persona's auto-derived canonical operator
    row (runtime plumbing) must not be advertised to — nor bare-persona-resolved
    for — an AGENT when the same persona also has a deliberate, non-canonical
    (placement-backed) row present here. A persona with only its canonical row,
    or with placements but no canonical, is returned unchanged (reachability
    fallback). ``is_canonical`` is the sanctioned discriminator
    (``persona_assignments.is_canonical_persona_channel``), passed in so this
    module stays stdlib-pure; ``persona_key`` groups rows by persona.

    Input order is preserved and inputs are never mutated — a purely subtractive
    filter over the ADDRESSABLE set, never over identity: callers keep the full
    roster for steering/identity resolution and pass only the addressable copy
    through here. Because the drop is decided from the rows PRESENT in this list,
    compose it AFTER :func:`scope_roster` so an out-of-scope placement (already
    filtered away) cannot shadow a canonical row that is still reachable.
    """

    rows = list(instances or ())
    # Personas with at least one non-canonical (placement-backed) row present.
    # Only these shadow their canonical row. A ``None`` key never shadows.
    placed: set = set()
    for instance in rows:
        if not is_canonical(instance):
            key = persona_key(instance)
            if key is not None:
                placed.add(key)
    return [
        instance
        for instance in rows
        if not (is_canonical(instance) and persona_key(instance) in placed)
    ]


def exclude_global_canonicals(
    instances: Iterable[Any],
    *,
    scope_workspace_id: str | None,
    is_canonical: Callable[[Any], bool],
) -> list:
    """Drop pointerless CANONICAL rows when the scope names a real workspace.

    Instance = in-level placement (operator ruling 2026-07-18): a runtime-global
    canonical row (no ``workspace_id`` claim) is plumbing, not a level presence,
    so it must not be advertised into a workspace's "On level" roster nor listed
    to an agent as an addressable peer. A canonical row that carries a workspace
    pointer made a scene claim and is treated by that pointer (it already passed
    :func:`scope_roster` to get here). Scope ``None`` (no active workspace)
    keeps the degrade contract: nothing is dropped.

    Purely subtractive over the ADDRESSABLE view, order preserved, inputs never
    mutated — identity/steering resolution keeps the full roster.
    """

    if optional_text(scope_workspace_id) is None:
        return list(instances or ())
    return [
        instance
        for instance in (instances or ())
        if not (
            is_canonical(instance)
            and optional_text(getattr(instance, "workspace_id", None)) is None
        )
    ]


def addressable_roster(
    instances: Iterable[Any],
    *,
    scope_workspace_id: str | None,
    is_canonical: Callable[[Any], bool],
    persona_key: Callable[[Any], Any] = _persona_key,
) -> list:
    """The roster a lane may ADVERTISE / bare-persona-resolve, order preserved.

    The single composition every addressable surface uses so the rule lives in
    exactly one place: (1) :func:`scope_roster` narrows to the sender's
    workspace, then (2) :func:`exclude_global_canonicals` drops the pointerless
    canonical plumbing rows under a real scope (instance = in-level placement),
    then (3) :func:`shadow_canonical_by_placement` shadows each persona's
    surviving canonical row behind an in-scope placement. Never mutates inputs.
    The FULL roster (for identity/steering) must be kept separately by the
    caller — this returns only the addressable view.
    """

    scoped = scope_roster(instances, scope_workspace_id=scope_workspace_id)
    leveled = exclude_global_canonicals(
        scoped, scope_workspace_id=scope_workspace_id, is_canonical=is_canonical
    )
    return shadow_canonical_by_placement(
        leveled, is_canonical=is_canonical, persona_key=persona_key
    )
