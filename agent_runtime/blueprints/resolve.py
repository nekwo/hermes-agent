"""Resolve blueprint slot bindings to concrete Harness personas.

A binding string is either ``persona:<id>`` (bind an existing persona directly) or
``profile:<name>`` (bind a raw Hermes profile). A profile is only a *template* — it
carries no orchestration contract — so it cannot fill a slot on its own. This module
turns a ``profile:`` binding into a real persona: it reuses the persona that already
wraps the profile, or, when none exists, **promotes** the profile into a persisted
persona cloned from the slot role's default template (so model/provider/toolsets/
system prompt are valid) with ``hermes_profile`` pointed at the profile.

The persisted runtime binding is therefore always ``slot_id -> persona_id``.
"""

from __future__ import annotations

from dataclasses import replace

from agent_runtime.models import AgentPersona


# slot role -> the default_personas() template id to clone when promoting
_ROLE_TEMPLATE = {
    "builder": "dev",
    "dev": "dev",
    "backend_dev": "backend_dev",
    "verifier": "qa",
    "qa": "qa",
    "lead": "neko_supervisor",
    "neko": "neko_supervisor",
    "pm": "neko_supervisor",
    "specialist": "dev",
    "harness": "dev",
    "human": "dev",
}


class BindingResolver:
    """Resolves binding strings to persona ids, promoting profiles when needed."""

    def __init__(self, *, agent_store=None, configured=None, profile_exists=None, allow_promote: bool = True):
        from agent_runtime.store import AgentStore

        self.agent_store = agent_store if agent_store is not None else AgentStore()
        self._configured = list(configured) if configured is not None else None
        self._profile_exists = profile_exists
        self.allow_promote = allow_promote

    def _profile_on_disk(self, name: str) -> bool:
        check = self._profile_exists
        if check is None:
            from hermes_cli.profiles import profile_exists

            check = profile_exists
        try:
            return bool(check(name))
        except Exception:
            return False

    def _personas(self) -> dict[str, AgentPersona]:
        personas: dict[str, AgentPersona] = {}
        try:
            for persona in self.agent_store.list_all():
                personas[persona.id] = persona
        except Exception:
            pass
        if self._configured is None:
            from agent_runtime.config import configured_personas

            self._configured = list(configured_personas())
        for persona in self._configured:
            personas.setdefault(persona.id, persona)
        return personas

    def resolve(self, source: str, *, slot_role: str) -> str:
        text = str(source or "").strip()
        if text.startswith("persona:"):
            persona_id = text.split(":", 1)[1].strip()
            if persona_id not in self._personas():
                raise ValueError(f"persona binding {persona_id!r} does not exist")
            return persona_id
        if text.startswith("profile:"):
            name = text.split(":", 1)[1].strip()
            if not self._profile_on_disk(name):
                raise ValueError(f"profile binding {name!r} does not exist on disk")
            personas = self._personas()
            for persona in personas.values():
                if str(getattr(persona, "hermes_profile", "") or "") == name:
                    return persona.id
            if not self.allow_promote:
                raise ValueError(
                    f"profile {name!r} has no persona; run without --dry-run to promote it"
                )
            return self._promote(name, slot_role, personas)
        raise ValueError("binding must start with persona: or profile:")

    def _promote(self, profile_name: str, slot_role: str, personas: dict[str, AgentPersona]) -> str:
        persona = promote_profile_to_persona(
            profile_name,
            slot_role=slot_role,
            personas=personas,
            agent_store=self.agent_store,
        )
        personas[persona.id] = persona
        return persona.id


def promote_profile_to_persona(
    profile_name: str,
    *,
    slot_role: str,
    personas: dict[str, AgentPersona] | None = None,
    agent_store=None,
) -> AgentPersona:
    from agent_runtime.store import AgentStore

    store = agent_store if agent_store is not None else AgentStore()
    known = dict(personas or {})
    if not known:
        try:
            for persona in store.list_all():
                known[persona.id] = persona
        except Exception:
            pass
        from agent_runtime.config import configured_personas

        for persona in configured_personas():
            known.setdefault(persona.id, persona)
    template_id = _ROLE_TEMPLATE.get(slot_role, "dev")
    template = known.get(template_id) or known.get("dev") or next(iter(known.values()), None)
    if template is None:
        raise ValueError(
            f"cannot promote profile {profile_name!r}: no template persona for role {slot_role!r}"
        )
    new_id = profile_name if profile_name not in known else f"{profile_name}_{slot_role}"
    persona = replace(
        template,
        id=new_id,
        display_name=f"{profile_name} ({slot_role})",
        hermes_profile=profile_name,
        skills=list(template.skills),
        toolsets=list(template.toolsets),
        required_mcp_servers=list(template.required_mcp_servers),
        readiness={},
    )
    return store.save(persona)
