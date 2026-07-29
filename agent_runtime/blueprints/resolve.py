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

from agent_runtime.models import AgentPersona
from agent_runtime.personas import promote_profile_to_persona

# ``promote_profile_to_persona`` (and its ``_ROLE_TEMPLATE``) now live in
# ``agent_runtime.personas`` — it is persona lifecycle, not stage routing, and it
# has a live caller outside this package (mission-lane removal, S1).
#
# This re-export is NOT cosmetic. Upstream-owned ``hermes_cli/web_server.py:12671``
# (``POST /api/profiles/{name}/promote``) does
# ``from agent_runtime.blueprints.resolve import promote_profile_to_persona``, and
# the fork may not edit upstream files, so this import path must keep resolving for
# as long as that endpoint exists. See the S1 report: whoever deletes
# ``agent_runtime/blueprints/`` in S7 must leave a shim behind or get the operator to
# accept an upstream edit.
__all__ = ["BindingResolver", "promote_profile_to_persona"]


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
            from agent_runtime.config import ensure_persisted_personas

            self._configured = list(ensure_persisted_personas())
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
