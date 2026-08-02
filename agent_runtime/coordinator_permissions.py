from __future__ import annotations

from dataclasses import dataclass, replace

from .models import AgentPersona, PersonaInstance
from .runtime_config import CoordinatorPermissionConfig

STEER_ACTIONS = frozenset(
    {
        "mission.chat.message",
        "re_route",
    }
)
CREATE_ACTIONS = frozenset({"persona.instance.create", "persona.instance.open_chat"})
KILL_ACTIONS = frozenset({"persona.instance.close", "persona.instance.retire"})
OPERATOR_ACTORS = frozenset({"operator", "tony", "cli"})


@dataclass(slots=True)
class CoordinatorPermissionScope:
    max_spawns: int = 0
    spawns_used: int = 0
    may_kill_own: bool = True
    may_kill_others: bool = False


@dataclass(slots=True)
class CoordinatorAuthorization:
    ok: bool
    needs_operator_confirm: bool
    reason: str | None = None
    scope: CoordinatorPermissionScope | None = None


def scope_for_persona(
    persona: AgentPersona | None,
    *,
    config: CoordinatorPermissionConfig | None = None,
    spawns_used: int = 0,
) -> CoordinatorPermissionScope:
    grant = config or CoordinatorPermissionConfig()
    autonomy = str(getattr(persona, "autonomy", "review") or "review").strip().lower()
    if autonomy in {"review", "propose_only"}:
        return CoordinatorPermissionScope(
            max_spawns=0,
            spawns_used=max(0, int(spawns_used)),
            may_kill_own=False,
            may_kill_others=False,
        )
    return CoordinatorPermissionScope(
        max_spawns=max(1, int(grant.max_spawns)),
        spawns_used=max(0, int(spawns_used)),
        may_kill_own=bool(grant.may_kill_own),
        may_kill_others=bool(grant.may_kill_others),
    )


def authorize_coordinator_action(
    action: str,
    scope: CoordinatorPermissionScope | None,
    target_instance: PersonaInstance | None = None,
    *,
    actor: str = "operator",
    coordinator_id: str | None = None,
) -> CoordinatorAuthorization:
    normalized_action = str(action or "").strip()
    actor_id = str(actor or "operator").strip() or "operator"
    if actor_id in OPERATOR_ACTORS:
        return CoordinatorAuthorization(ok=True, needs_operator_confirm=False, reason="operator_bypass", scope=scope)
    if normalized_action in STEER_ACTIONS:
        return CoordinatorAuthorization(ok=True, needs_operator_confirm=False, reason="steer_ungated", scope=scope)
    if scope is None:
        return CoordinatorAuthorization(ok=False, needs_operator_confirm=True, reason="missing_permission_scope", scope=scope)
    if normalized_action in CREATE_ACTIONS:
        if scope.spawns_used < scope.max_spawns:
            return CoordinatorAuthorization(
                ok=True,
                needs_operator_confirm=False,
                reason="spawn_in_scope",
                scope=replace(scope, spawns_used=scope.spawns_used + 1),
            )
        return CoordinatorAuthorization(ok=False, needs_operator_confirm=True, reason="spawn_scope_exhausted", scope=scope)
    if normalized_action in KILL_ACTIONS:
        if target_instance is None:
            return CoordinatorAuthorization(ok=False, needs_operator_confirm=True, reason="missing_target_instance", scope=scope)
        owner = str(target_instance.spawned_by or "").strip()
        if not owner or owner in OPERATOR_ACTORS:
            return CoordinatorAuthorization(ok=False, needs_operator_confirm=True, reason="operator_placed_target", scope=scope)
        if coordinator_id and owner == coordinator_id:
            if scope.may_kill_own:
                return CoordinatorAuthorization(ok=True, needs_operator_confirm=False, reason="kill_own_in_scope", scope=scope)
            return CoordinatorAuthorization(ok=False, needs_operator_confirm=True, reason="kill_own_not_granted", scope=scope)
        if scope.may_kill_others:
            return CoordinatorAuthorization(ok=True, needs_operator_confirm=False, reason="kill_other_in_scope", scope=scope)
        return CoordinatorAuthorization(ok=False, needs_operator_confirm=True, reason="kill_other_not_granted", scope=scope)
    return CoordinatorAuthorization(ok=True, needs_operator_confirm=False, reason="non_restructure_action", scope=scope)
