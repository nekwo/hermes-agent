"""Test-only mint for ``mode="free_floating"`` persona instances.

WHY THIS LIVES IN ``tests/``. ``PersonaInstanceStore.create_free_floating`` was
production-callerless — the free-floating queue verb chain that used to call it
was retired with the mission lane, and the only remaining callers were the
flow-graph / checkpoint / state-patch suites that need a pair of cheap instance
rows to point edges at. A production method whose sole callers are tests is a
method nothing proves; the mint moved here and the store method was deleted
(``docs/agent-runtime-harness/planned/duplicate-implementation-retirement.md``
Stage 4; the tombstone row lives in ``test_tombstone_registry.py``).

BEHAVIOUR IS UNCHANGED from the deleted method, deliberately: same id
derivation (``persona_instance_id_for``), same identity resolution for the
``"profile:<x>"`` spelling, same idempotent adopt-or-create, same
``persona_instance.created`` event with ``{"mode": "free_floating"}``, same
"return the row read back off disk" contract.

IT REACHES STORE INTERNALS ON PURPOSE. ``PersonaInstance`` rows in
``mode="free_floating"`` have no public creation path — ``ensure_for_persona``
mints ``mode=None`` rows and ``add_instance``/``create_operator_chat`` mint
placements and operator chats. Reproducing the mode through a public API would
change what the suites are pinning, so the helper keeps the private
``_write``/``_event`` access the method had, in test code where it is honest
about being a fixture rather than a runtime capability.
"""

from __future__ import annotations

from hermes_time import now

from agent_runtime import paths
from agent_runtime.models import AgentPersona, PersonaInstance
from agent_runtime.persona_assignments import (
    PersonaInstanceStore,
    _display_name_for_template,
    persona_instance_id_for,
    safe_assignment_token,
)
from agent_runtime.states import WorkerSessionState


def free_floating_identity(
    persona_or_template: AgentPersona | str,
) -> tuple[str, str, str, str | None]:
    """(persona_id, role, display_name, profile_id) for a persona or template.

    Verbatim from the deleted ``persona_assignments._free_floating_identity``,
    which had no other caller and left with the method.
    """

    if isinstance(persona_or_template, AgentPersona):
        return (
            persona_or_template.id,
            str(persona_or_template.role),
            persona_or_template.display_name,
            persona_or_template.hermes_profile,
        )
    raw = str(persona_or_template or "").strip()
    if raw.lower().startswith("profile:"):
        profile = safe_assignment_token(raw.split(":", 1)[1])
        persona_id = f"profile:{profile}" if profile else "profile:persona"
        return (persona_id, "profile", _display_name_for_template(profile), profile or None)
    persona_id = safe_assignment_token(raw) or "persona"
    return (persona_id, persona_id, persona_id, None)


def mint_free_floating(
    persona_or_template: AgentPersona | str,
    *,
    store: PersonaInstanceStore | None = None,
) -> PersonaInstance:
    """Create (or adopt) the canonical free-floating instance for a persona.

    Idempotent: a second call for the same persona reuses the canonical
    ``persona_instance_id_for(persona_id)`` row and only rewrites it when one of
    the identity fields actually drifted — the property
    ``test_persona_assignments.py`` pins.
    """

    store = store or PersonaInstanceStore()
    persona_id, role, display_name, profile_id = free_floating_identity(persona_or_template)
    instance_id = persona_instance_id_for(persona_id)
    try:
        instance = store.get(instance_id)
    except Exception:
        instance = PersonaInstance(
            id=instance_id,
            persona_id=persona_id,
            role=role,
            display_name=display_name,
            profile_id=profile_id,
            runtime_root=str(paths.store_root()),
            state=WorkerSessionState.IDLE,
            mode="free_floating",
            updated_at=now(),
        )
        store._write(instance)
        store._event("persona_instance.created", instance, {"mode": "free_floating"})
        return store.get(instance_id)
    changed = False
    for attr, value in {
        "persona_id": persona_id,
        "role": role,
        "display_name": display_name,
        "profile_id": profile_id,
        "runtime_root": str(paths.store_root()),
        "mode": "free_floating",
    }.items():
        if getattr(instance, attr) != value:
            setattr(instance, attr, value)
            changed = True
    if changed:
        instance.updated_at = now()
        store._write(instance)
    return store.get(instance_id)
