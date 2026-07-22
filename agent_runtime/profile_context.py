from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any, Iterator

from hermes_constants import (
    get_hermes_home,
    record_hermes_head_home_if_unset,
    reset_hermes_head_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from hermes_cli.profiles import get_profile_dir, normalize_profile_name, profile_exists


@dataclass(slots=True)
class PersonaProfileBinding:
    persona_id: str
    hermes_profile: str | None
    profile_home: Path | None
    readiness: str = "ready"
    summary: str = "ready"
    metadata: dict[str, Any] = field(default_factory=dict)


def active_profile_name() -> str:
    """Return the current Hermes profile name without assuming Alice is head.

    Mission Control can be driven from any Hermes profile. Prefer the explicit
    profile environment set by the CLI/gateway, then derive the name from the
    active HERMES_HOME path, then fall back to the legacy active_profile marker
    or default profile.
    """
    raw = os.environ.get("HERMES_PROFILE", "").strip()
    if raw:
        return normalize_profile_name(raw)
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    if home.parent.name == "profiles" and home.name:
        return normalize_profile_name(home.name)
    active_profile = Path.home() / ".hermes" / "active_profile"
    try:
        active = active_profile.read_text(encoding="utf-8").strip()
    except OSError:
        active = ""
    return normalize_profile_name(active or "default")


def resolve_persona_profile(persona) -> PersonaProfileBinding:
    if not persona.hermes_profile:
        return PersonaProfileBinding(
            persona_id=persona.id,
            hermes_profile=None,
            profile_home=None,
            readiness="ready",
            summary="inherits active Harness profile",
        )
    name = normalize_profile_name(persona.hermes_profile)
    if not profile_exists(name):
        return PersonaProfileBinding(
            persona_id=persona.id,
            hermes_profile=name,
            profile_home=None,
            readiness="missing_profile",
            summary=f"Hermes profile '{name}' does not exist",
        )
    home = get_profile_dir(name)
    return PersonaProfileBinding(
        persona_id=persona.id,
        hermes_profile=name,
        profile_home=home,
        readiness="ready",
        summary="profile exists",
    )


@contextmanager
def persona_profile_context(binding: PersonaProfileBinding, *, runtime_root: Path | None = None) -> Iterator[None]:
    if binding.profile_home is None:
        yield
        return
    previous_env = {
        "HERMES_HOME": os.environ.get("HERMES_HOME"),
        "HOME": os.environ.get("HOME"),
        "HERMES_AGENT_RUNTIME_ROOT": os.environ.get("HERMES_AGENT_RUNTIME_ROOT"),
        "HERMES_AUTH_HOME": os.environ.get("HERMES_AUTH_HOME"),
    }
    # Record the operator/head home BEFORE this override diverts
    # ``get_hermes_home()``. Set-once (nested relay hops keep the outermost home)
    # so a relay-target chat turn running under a persona profile-home override
    # can still persist its operator-visible transcript (persona-chat SessionDB)
    # to the home the Mission Control projection reads (2026-07-18 relay
    # SessionDB-persistence fix).
    head_home_token = record_hermes_head_home_if_unset(get_hermes_home())
    token = set_hermes_home_override(binding.profile_home)
    try:
        head_auth_home = previous_env.get("HERMES_AUTH_HOME") or previous_env.get("HERMES_HOME")
        if head_auth_home:
            os.environ["HERMES_AUTH_HOME"] = head_auth_home
        os.environ["HERMES_HOME"] = str(binding.profile_home)
        profile_home = binding.profile_home / "home"
        if profile_home.exists():
            os.environ["HOME"] = str(profile_home)
        if runtime_root is not None:
            os.environ["HERMES_AGENT_RUNTIME_ROOT"] = str(runtime_root)
        yield
    finally:
        reset_hermes_home_override(token)
        reset_hermes_head_home(head_home_token)
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
