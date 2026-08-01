from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
from typing import Any, Iterator

from hermes_constants import (
    get_hermes_head_home,
    get_hermes_home,
    record_hermes_head_home_if_unset,
    reset_hermes_head_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from hermes_cli.profiles import get_profile_dir, normalize_profile_name, profile_exists

logger = logging.getLogger(__name__)

RUNTIME_ROOT_ENV = "HERMES_AGENT_RUNTIME_ROOT"

#: Where the exported runtime root came from, recorded on the typed row so an
#: operator reading it never has to guess.
RUNTIME_ROOT_SOURCE_ARGUMENT = "argument"
RUNTIME_ROOT_SOURCE_RESOLVER = "resolver"
RUNTIME_ROOT_SOURCE_UNRESOLVED = "unresolved"

#: A persona bound no Hermes profile, so this context exported the runtime root
#: but performed NO profile-home redirection. Typed so the degradation is
#: accounted for rather than inferred from an absent variable (audit Q1).
PROFILE_CONTEXT_NO_PROFILE_BINDING = "persona_profile_context_no_profile_binding"
#: The canonical resolver could not answer (probe isolation refused, or the
#: module is unimportable), so no runtime root was exported at all. The ONE
#: remaining path to an unset variable, and it is now named.
PROFILE_CONTEXT_RUNTIME_ROOT_UNRESOLVED = "persona_profile_context_runtime_root_unresolved"

# S54 took ``MCP_SERVER_PERSONA_OWNERS`` with ``mcp_owner_profile_name``, its
# only reader.

@dataclass(frozen=True, slots=True)
class ProfileContextRow:
    """A typed account of what this run's environment context actually did.

    Emitted for the cases that used to be silent. ``row()`` is the same
    ``{code, subject, entry_point_lane, summary, fix_hint}`` shape
    ``mcp_lane`` / ``mcp_admission`` / ``terminal_envelope`` already emit, so
    operator surfaces need no new case.
    """

    code: str
    persona_id: str
    summary: str
    fix_hint: str = ""
    runtime_root: str = ""
    runtime_root_source: str = RUNTIME_ROOT_SOURCE_UNRESOLVED

    def row(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "subject": self.persona_id,
            "entry_point_lane": "",
            "summary": self.summary,
            "fix_hint": self.fix_hint,
            "runtime_root": self.runtime_root,
            "runtime_root_source": self.runtime_root_source,
        }


_PROFILE_CONTEXT_ROWS: ContextVar[tuple[ProfileContextRow, ...]] = ContextVar(
    "hermes_profile_context_rows", default=()
)


# S54 removed ``current_profile_context_rows`` and ``mcp_owner_profile_name``.
# The typed rows are still BUILT and exported on the resolution path; these two
# were read-back accessors with no production consumer.

def _resolved_runtime_root() -> Path | None:
    """The configured runtime root, via the ONE canonical ladder. Never raises.

    ``paths.store_root()`` is ``resolution.resolve_runtime`` (env →
    ``agent_runtime.store_root`` in the root config → platform default) plus
    ``assert_probe_isolation``. It answers for EVERY persona, profile-bound or
    not — which is exactly why the runtime-root export does not need a profile
    binding and never should have been gated on one.

    ``None`` means the resolver genuinely refused (probe isolation, or an
    unimportable module). That is a typed outcome
    (:data:`PROFILE_CONTEXT_RUNTIME_ROOT_UNRESOLVED`), not a silent skip.
    """

    try:
        from . import paths

        root = paths.store_root()
    except Exception:
        logger.debug("Runtime root unresolvable for persona profile context", exc_info=True)
        return None
    text = str(root or "").strip()
    return Path(text) if text else None


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


def persona_bound_profile_name(persona_id: str | None) -> str:
    """Return the persisted persona binding used by its Harness worker.

    Visual proof requests run on the owning worker's MCP lane, so their
    ``hermes_profile`` pin must follow that persona binding rather than the
    operator process' active profile.  Falling back keeps generic/unbound
    profiles compatible while making a configured QA worker deterministic.
    """

    resolved_id = str(persona_id or "").strip()
    if resolved_id:
        try:
            from .config import get_persisted_persona

            persona = get_persisted_persona(resolved_id)
            bound = str(getattr(persona, "hermes_profile", None) or "").strip()
            if bound:
                return normalize_profile_name(bound)
        except (OSError, StopIteration, ValueError):
            logger.debug("Persona profile binding unavailable for %s", resolved_id, exc_info=True)
    return active_profile_name()


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
    """Bind this run's environment. Audit Q1: the runtime root is UNCONDITIONAL.

    This block used to conflate two responsibilities and gate both on one field.
    Profile-home redirection (``HERMES_HOME`` / ``HOME`` / ``HERMES_AUTH_HOME``)
    genuinely requires a bound Hermes profile. Exporting the runtime root does
    NOT — ``paths.store_root()`` answers for every persona — yet the
    ``profile_home is None`` early-``yield`` skipped it too.

    That was path 1 of the ambient-environment split: a persona with no
    ``hermes_profile`` ran with ``HERMES_AGENT_RUNTIME_ROOT`` unset, which made
    the legacy terminal envelope INERT and let ``git push origin main`` execute
    ungated and unrecorded, while the same lane with a profile-bound persona
    hard-blocked it. Same lane, same role, opposite outcomes, decided by a
    config field most personas never set.

    Now: the runtime root is exported for every persona (save/restored exactly
    as before), the profile-home redirection stays gated on the binding, and
    both degradations — "no profile bound" and "resolver could not answer" —
    emit a typed :class:`ProfileContextRow` instead of being inferable only from
    an absent variable.

    This must land WITH the Q2 scope binding, never before it: on its own it
    arms the legacy env-keyed envelope on every lane that previously ran with
    the variable unset. With Q2, those lanes bind a scope and are decided by
    policy, so the export changes no verdict — it only makes receipts land.
    """

    resolved_root = runtime_root if runtime_root is not None else _resolved_runtime_root()
    root_source = (
        RUNTIME_ROOT_SOURCE_ARGUMENT
        if runtime_root is not None
        else (
            RUNTIME_ROOT_SOURCE_RESOLVER
            if resolved_root is not None
            else RUNTIME_ROOT_SOURCE_UNRESOLVED
        )
    )
    rows: list[ProfileContextRow] = []
    if resolved_root is None:
        rows.append(
            ProfileContextRow(
                code=PROFILE_CONTEXT_RUNTIME_ROOT_UNRESOLVED,
                persona_id=binding.persona_id,
                summary=(
                    f"No runtime root could be resolved for persona '{binding.persona_id}', so "
                    f"{RUNTIME_ROOT_ENV} is not exported for this run."
                ),
                fix_hint=(
                    "Set agent_runtime.store_root in the root config.yaml, or export "
                    f"{RUNTIME_ROOT_ENV}. Under HERMES_REQUIRE_ISOLATED_ROOT this is expected: "
                    "the resolver refuses rather than let a probe write into the live store."
                ),
                runtime_root_source=RUNTIME_ROOT_SOURCE_UNRESOLVED,
            )
        )

    if binding.profile_home is None:
        rows.append(
            ProfileContextRow(
                code=PROFILE_CONTEXT_NO_PROFILE_BINDING,
                persona_id=binding.persona_id,
                summary=(
                    f"Persona '{binding.persona_id}' binds no Hermes profile, so this run "
                    "inherits the active profile home; only the runtime root is exported."
                ),
                fix_hint=(
                    "Set the persona's hermes_profile if it needs its own HERMES_HOME / HOME / "
                    "HERMES_AUTH_HOME. Running without one is supported and is not an error."
                ),
                runtime_root=str(resolved_root or ""),
                runtime_root_source=root_source,
            )
        )
        previous_root = os.environ.get(RUNTIME_ROOT_ENV)
        rows_token = _PROFILE_CONTEXT_ROWS.set(tuple(rows))
        try:
            if resolved_root is not None:
                os.environ[RUNTIME_ROOT_ENV] = str(resolved_root)
            yield
        finally:
            _PROFILE_CONTEXT_ROWS.reset(rows_token)
            if previous_root is None:
                os.environ.pop(RUNTIME_ROOT_ENV, None)
            else:
                os.environ[RUNTIME_ROOT_ENV] = previous_root
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
    head_home_token = record_hermes_head_home_if_unset(get_hermes_head_home())
    token = set_hermes_home_override(binding.profile_home)
    rows_token = _PROFILE_CONTEXT_ROWS.set(tuple(rows))
    try:
        head_auth_home = previous_env.get("HERMES_AUTH_HOME") or previous_env.get("HERMES_HOME")
        if head_auth_home:
            os.environ["HERMES_AUTH_HOME"] = head_auth_home
        os.environ["HERMES_HOME"] = str(binding.profile_home)
        profile_home = binding.profile_home / "home"
        if profile_home.exists():
            os.environ["HOME"] = str(profile_home)
        # Resolved BEFORE the HERMES_HOME override above so the exported root is
        # the head/operator resolution, not one re-derived through the profile
        # home this context just diverted to.
        if resolved_root is not None:
            os.environ[RUNTIME_ROOT_ENV] = str(resolved_root)
        yield
    finally:
        _PROFILE_CONTEXT_ROWS.reset(rows_token)
        reset_hermes_home_override(token)
        reset_hermes_head_home(head_home_token)
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
