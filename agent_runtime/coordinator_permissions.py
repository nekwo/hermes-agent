"""A coordinator persona's SELF-DECLARED budget — advisory, never authorization.

Renamed and re-documented by Stage A4 of
``docs/agent-runtime-harness/planned/authorization-chokepoint.md``, because the
old name (``authorize_coordinator_action``) claimed a job this code cannot do
and was never built to do. What it is:

**A coordinator persona declares its own restructure budget so the runtime can
refuse it and ask the OPERATOR to confirm.** Read the refusal payload and the
shape is unmistakable — ``_coordinator_confirm_payload`` in
``persona_commands.py`` answers ``needs_operator_confirm``, never ``denied``, and
``next_expected`` is a sentence asking a human. It is a speed bump between an
autonomous agent and a destructive action, and it is well shaped for that.

**Why it is not authorization, stated so no future stage designs around a
fiction.** The request carries BOTH the identity being checked and the grant it
is checked against:

* the identity is ``--requested-by`` (``_coordinator_actor_id``), which is argv;
* the scope is seeded from the persona's ``autonomy`` and then OVERRIDDEN by four
  argv flags (``--coordinator-max-spawns``, ``--coordinator-may-kill-own``,
  ``--coordinator-no-kill-own``, ``--coordinator-may-kill-others``);
* and :data:`OPERATOR_ACTORS` short-circuits everything on a NAME.

Every input a caller could lie about is one the caller supplies. That is a
self-declaration protocol, and no remote device can be held to one.

**Where authorization actually runs.** ``agent_runtime.call_authorization``, at
the front door: a tier declared per method, evaluated against what the transport
PROVED (``serve_rpc.handle_request``), mirrored by a ``local_console`` identity
at CLI entry. The two are complementary and must not be confused — this file
answers "should a human be asked first?", that one answers "may this caller do
this at all?".

This file's MODEL is deliberately unchanged by the rename. Whether a persona
should be able to spawn or kill at all, and under what budget, is a separate
question from where the check runs (plan §4).
"""

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
#: Names that mean "a human placed this" — used twice, and for two things.
#:
#: As an ACTOR it short-circuits the review: a human is not asked to confirm
#: their own action. As a target's ``spawned_by`` it does the opposite — an
#: operator-placed instance is protected from a coordinator's kill.
#:
#: It is a NAME match, and a name is not a credential. That is tolerable here
#: and only here: this file decides whether to ask a human, and the worst a
#: forged name buys is a destructive action that runs without a confirmation
#: prompt at the machine owner's own shell, where the owner could have typed the
#: verb directly. It would NOT be tolerable in an authorization decision, which
#: is why authorization does not read it — see the module docstring.
OPERATOR_ACTORS = frozenset({"operator", "tony", "cli"})


@dataclass(slots=True)
class CoordinatorPermissionScope:
    max_spawns: int = 0
    spawns_used: int = 0
    may_kill_own: bool = True
    may_kill_others: bool = False


@dataclass(slots=True)
class CoordinatorBudgetReview:
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


def review_coordinator_budget(
    action: str,
    scope: CoordinatorPermissionScope | None,
    target_instance: PersonaInstance | None = None,
    *,
    actor: str = "operator",
    coordinator_id: str | None = None,
) -> CoordinatorBudgetReview:
    """Does this coordinator's declared budget cover *action*, or ask a human?

    ``ok=False`` always arrives with ``needs_operator_confirm=True``: there is no
    branch in this function that DENIES, only branches that escalate. That is the
    single sentence that tells the two systems apart — a denial is
    ``call_authorization``'s to issue, and it comes with a caller the transport
    proved rather than an actor name off argv.

    Pure over its arguments by construction: no config read, no store read, no
    connection. Every input is supplied by the caller, which is why the answer is
    advice and not a decision (see the module docstring).
    """

    normalized_action = str(action or "").strip()
    actor_id = str(actor or "operator").strip() or "operator"
    if actor_id in OPERATOR_ACTORS:
        return CoordinatorBudgetReview(ok=True, needs_operator_confirm=False, reason="operator_bypass", scope=scope)
    if normalized_action in STEER_ACTIONS:
        return CoordinatorBudgetReview(ok=True, needs_operator_confirm=False, reason="steer_ungated", scope=scope)
    if scope is None:
        return CoordinatorBudgetReview(ok=False, needs_operator_confirm=True, reason="missing_permission_scope", scope=scope)
    if normalized_action in CREATE_ACTIONS:
        if scope.spawns_used < scope.max_spawns:
            return CoordinatorBudgetReview(
                ok=True,
                needs_operator_confirm=False,
                reason="spawn_in_scope",
                scope=replace(scope, spawns_used=scope.spawns_used + 1),
            )
        return CoordinatorBudgetReview(ok=False, needs_operator_confirm=True, reason="spawn_scope_exhausted", scope=scope)
    if normalized_action in KILL_ACTIONS:
        if target_instance is None:
            return CoordinatorBudgetReview(ok=False, needs_operator_confirm=True, reason="missing_target_instance", scope=scope)
        owner = str(target_instance.spawned_by or "").strip()
        if not owner or owner in OPERATOR_ACTORS:
            return CoordinatorBudgetReview(ok=False, needs_operator_confirm=True, reason="operator_placed_target", scope=scope)
        if coordinator_id and owner == coordinator_id:
            if scope.may_kill_own:
                return CoordinatorBudgetReview(ok=True, needs_operator_confirm=False, reason="kill_own_in_scope", scope=scope)
            return CoordinatorBudgetReview(ok=False, needs_operator_confirm=True, reason="kill_own_not_granted", scope=scope)
        if scope.may_kill_others:
            return CoordinatorBudgetReview(ok=True, needs_operator_confirm=False, reason="kill_other_in_scope", scope=scope)
        return CoordinatorBudgetReview(ok=False, needs_operator_confirm=True, reason="kill_other_not_granted", scope=scope)
    return CoordinatorBudgetReview(ok=True, needs_operator_confirm=False, reason="non_restructure_action", scope=scope)
