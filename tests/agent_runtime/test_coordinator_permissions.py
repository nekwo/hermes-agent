from agent_runtime.coordinator_permissions import (
    CoordinatorPermissionScope,
    review_coordinator_budget,
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
    auth = review_coordinator_budget("mission.chat.message", None, actor="neko_supervisor", coordinator_id="neko_supervisor")

    assert auth.ok is True
    assert auth.needs_operator_confirm is False
    assert auth.reason == "steer_ungated"


def test_create_beyond_max_spawns_needs_operator_confirm():
    scope = CoordinatorPermissionScope(max_spawns=1, spawns_used=1, may_kill_own=True)

    auth = review_coordinator_budget("persona.instance.create", scope, actor="neko_supervisor", coordinator_id="neko_supervisor")

    assert auth.ok is False
    assert auth.needs_operator_confirm is True
    assert auth.reason == "spawn_scope_exhausted"
    assert auth.scope == scope


def test_create_in_scope_increments_spawns_used():
    scope = CoordinatorPermissionScope(max_spawns=1, spawns_used=0)

    auth = review_coordinator_budget("persona.instance.create", scope, actor="neko_supervisor", coordinator_id="neko_supervisor")

    assert auth.ok is True
    assert auth.scope is not None
    assert auth.scope.spawns_used == 1
    assert scope.spawns_used == 0


def test_kill_own_spawned_child_is_allowed_with_scope():
    scope = CoordinatorPermissionScope(max_spawns=0, may_kill_own=True)

    auth = review_coordinator_budget(
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

    auth = review_coordinator_budget(
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


# ── the renamed vocabulary (chokepoint plan A4-i) ────────────────────────────


def test_this_module_only_ever_ESCALATES_and_never_denies():
    """A4-i, as an assertion rather than as a docstring.

    ``authorize_coordinator_action`` claimed a job this code cannot do. The proof
    that it never did it is structural: there is no branch here that answers
    ``ok=False`` without also answering ``needs_operator_confirm=True``. A
    refusal that asks a human is a speed bump; a denial is what
    ``call_authorization`` issues, against a caller the transport proved.

    Driven over every branch this module has, so a future arm that denied
    outright would red here and have to say so out loud.
    """

    denying_scope = CoordinatorPermissionScope(
        max_spawns=0, may_kill_own=False, may_kill_others=False
    )
    cases = [
        review_coordinator_budget(
            "persona.instance.create", denying_scope, actor="neko", coordinator_id="neko"
        ),
        review_coordinator_budget(
            "persona.instance.close", None, actor="neko", coordinator_id="neko"
        ),
        review_coordinator_budget(
            "persona.instance.retire", denying_scope, None, actor="neko", coordinator_id="neko"
        ),
        review_coordinator_budget(
            "persona.instance.retire",
            denying_scope,
            _instance("operator"),
            actor="neko",
            coordinator_id="neko",
        ),
        review_coordinator_budget(
            "persona.instance.retire",
            denying_scope,
            _instance("neko"),
            actor="neko",
            coordinator_id="neko",
        ),
        review_coordinator_budget(
            "persona.instance.retire",
            denying_scope,
            _instance("someone_else"),
            actor="neko",
            coordinator_id="neko",
        ),
    ]

    assert [c.ok for c in cases] == [False] * len(cases)
    assert all(c.needs_operator_confirm for c in cases)


def test_the_old_authorization_name_is_gone_so_nothing_can_call_it_by_mistake():
    """A rename that left an alias behind would leave the false claim reachable,
    and a grep for "who authorizes an agent retire" would still land here."""

    from agent_runtime import coordinator_permissions

    assert not hasattr(coordinator_permissions, "authorize_coordinator_action")
    assert not hasattr(coordinator_permissions, "CoordinatorAuthorization")


def test_the_budget_review_reads_its_own_inputs_and_the_gate_reads_none_of_them():
    """The two systems, side by side, on the one fact that separates them.

    This module's answer moves when the CALLER changes what it typed — that is
    what makes it a self-declaration. ``authorize_call``'s does not: it takes a
    caller the transport minted and there is no argument by which a request can
    widen itself.
    """

    from agent_runtime.call_authorization import (
        CLI_CONSOLE,
        TIER_CONSOLE,
        authorize_call,
    )

    narrow = CoordinatorPermissionScope(max_spawns=0)
    wide = CoordinatorPermissionScope(max_spawns=5)
    # Same actor, same action, different self-declared grant, different answer.
    assert (
        review_coordinator_budget(
            "persona.instance.create", narrow, actor="neko", coordinator_id="neko"
        ).ok
        is False
    )
    assert (
        review_coordinator_budget(
            "persona.instance.create", wide, actor="neko", coordinator_id="neko"
        ).ok
        is True
    )
    # The gate has no equivalent knob: its only input is the minted caller.
    assert authorize_call(TIER_CONSOLE, CLI_CONSOLE).ok is True
