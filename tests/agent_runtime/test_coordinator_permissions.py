from agent_runtime.coordinator_permissions import (
    CoordinatorPermissionScope,
    authorize_coordinator_action,
    scope_for_persona,
)
from agent_runtime.models import AgentPersona, PersonaInstance
from agent_runtime.states import WorkerSessionState


def _instance(spawned_by: str | None) -> PersonaInstance:
    return PersonaInstance(
        id="personainst_child",
        persona_id="dev",
        role="dev",
        display_name="Dev",
        profile_id=None,
        runtime_root="runtime",
        state=WorkerSessionState.IDLE,
        spawned_by=spawned_by,
    )


def _persona(autonomy: str = "autonomous") -> AgentPersona:
    return AgentPersona(
        id="neko_supervisor",
        display_name="Neko",
        role="alice_supervisor",
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=[],
        system_prompt_path="personas/neko_supervisor/system.md",
        autonomy=autonomy,
    )


def test_steer_verbs_run_without_permission_scope():
    auth = authorize_coordinator_action("persona.instance.message", None, actor="neko_supervisor", coordinator_id="neko_supervisor")

    assert auth.ok is True
    assert auth.needs_operator_confirm is False
    assert auth.reason == "steer_ungated"


def test_create_beyond_max_spawns_needs_operator_confirm():
    scope = CoordinatorPermissionScope(max_spawns=1, spawns_used=1, may_kill_own=True)

    auth = authorize_coordinator_action("persona.instance.create", scope, actor="neko_supervisor", coordinator_id="neko_supervisor")

    assert auth.ok is False
    assert auth.needs_operator_confirm is True
    assert auth.reason == "spawn_scope_exhausted"
    assert auth.scope == scope


def test_create_in_scope_increments_spawns_used():
    scope = CoordinatorPermissionScope(max_spawns=1, spawns_used=0)

    auth = authorize_coordinator_action("persona.instance.create", scope, actor="neko_supervisor", coordinator_id="neko_supervisor")

    assert auth.ok is True
    assert auth.scope is not None
    assert auth.scope.spawns_used == 1
    assert scope.spawns_used == 0


def test_kill_own_spawned_child_is_allowed_with_scope():
    scope = CoordinatorPermissionScope(max_spawns=0, may_kill_own=True)

    auth = authorize_coordinator_action(
        "persona.instance.close",
        scope,
        _instance("neko_supervisor"),
        actor="neko_supervisor",
        coordinator_id="neko_supervisor",
    )

    assert auth.ok is True
    assert auth.reason == "kill_own_in_scope"


def test_kill_operator_placed_instance_always_needs_operator_confirm():
    scope = CoordinatorPermissionScope(max_spawns=0, may_kill_own=True, may_kill_others=True)

    auth = authorize_coordinator_action(
        "persona.instance.close",
        scope,
        _instance("operator"),
        actor="neko_supervisor",
        coordinator_id="neko_supervisor",
    )

    assert auth.ok is False
    assert auth.needs_operator_confirm is True
    assert auth.reason == "operator_placed_target"


def test_review_autonomy_defaults_to_confirm_each_create_and_kill():
    scope = scope_for_persona(_persona("review"))

    assert scope.max_spawns == 0
    assert scope.may_kill_own is False
    assert scope.may_kill_others is False
