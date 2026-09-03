from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import StrEnum

from .models import AgentPersona

_LOGGER = logging.getLogger(__name__)


class AgentRole(StrEnum):
    """RETIRED RESIDUE, deliberately kept. Read this before adding a member.

    One member, and that member (``pm``) is mothballed — see
    ``persona_lifecycle.MOTHBALLED_ROLE_TOKENS``, the set that actually
    enforces it. So this is not a live taxonomy of roles: roles are DATA in
    this runtime, S61/S64 made profile/persona declarations the sole capability
    authority, and ``coerce_agent_role`` returns a plain ``str`` for every role
    a persona really carries today. It survives because ``docs/agent-runtime-
    harness/archive/2026-08-22-pre-consolidation/02-execution-engine.md`` still names it and because the legacy
    ``pm`` spelling has to keep resolving on persisted rows — not because a
    role belongs here.
    """

    PM = "pm"


class AutonomyLevel(StrEnum):
    PROPOSE_ONLY = "propose_only"
    APPLY_WITH_REVIEW = "apply_with_review"
    AUTONOMOUS = "autonomous"


# Retired roles/personas are single-sourced in ``persona_lifecycle`` as
# ``MOTHBALLED_ROLE_TOKENS`` / ``MOTHBALLED_PERSONA_IDS``, which
# ``is_runtime_persona`` actually reads. A ``MOTHBALLED_ROLES`` frozenset lived
# here too, saying it existed so nothing would hand-roll ``role == "pm"`` — with
# zero readers, while the module IMPORTED the two live sets and used neither.
# A third spelling of one fact does not prevent a fourth; the reader does.


# Synthetic operator-channel personas built from a raw Hermes profile carry the
# "profile" role sentinel (see ``hermes_cli.harness._persona_by_id``). They are not
# a typed mission slot, so it is not a real ``AgentRole`` — passing it straight into
# ``AgentRole(...)`` raises ``'profile' is not a valid AgentRole`` and kills the whole
# operator chat turn. Unknown roles remain data and ``validate_toolsets`` applies
# no role-specific ceiling.
PROFILE_ROLE_SENTINEL = "profile"
def coerce_agent_role(role: AgentRole | str | None) -> AgentRole | str:
    """Resolve a persona role token to an ``AgentRole``.

    Known legacy values retain their enum spelling for compatibility. Unknown
    values remain data: they are never dropped or coerced into a hardcoded role.
    """

    if isinstance(role, AgentRole):
        return role
    text = str(role or "").strip()
    try:
        return AgentRole(text)
    except ValueError:
        return text

# Fork registry hygiene (T6c, 2026-07-18). Upstream toolsets the fork's effective
# registry must never resolve on ANY agent-runtime lane: the whole ``kanban``
# toolset (12 tools — superseded by the fork board/mission system) and the
# ``feishu_doc`` + ``feishu_drive`` toolsets (5 tools — an irrelevant Feishu/Lark
# integration). The upstream tool files stay untouched (fork-sync cleanliness);
# this fork-owned constant is the deregistration mechanism. It is enforced in TWO
# places so no lane escapes:
#   1. folded into ``PERSONA_BLOCKED_TOOLS`` below → the persona chat/run lanes and
#      the ``tool_visibility`` permission-preview surface, and
#   2. unioned at the ``profile_runner`` agent-construction chokepoint → every
#      lane, including the worker / root-node lanes that pass
#      ``blocked_tool_names=[]`` (node_tools.py / root_node_engine.py).
# ``delegate_task`` and ``memory`` are deliberately NOT here — operator ruling:
# keep them registered (both are parallel-authority surfaces; a future lane that
# enables either owns reconciling delegation-vs-harness-dispatch / upstream-memory-
# vs-profile-memory).
REGISTRY_HYGIENE_BLOCKED_TOOLS = frozenset(
    {
        # kanban toolset (12)
        "kanban_show",
        "kanban_list",
        "kanban_create",
        "kanban_complete",
        "kanban_block",
        "kanban_link",
        "kanban_comment",
        "kanban_unblock",
        "kanban_heartbeat",
        # +3 from the 2026-07-31 upstream sync: the kanban card-attachment
        # verbs. Blocked for the same reason as the rest of the toolset —
        # upstream kanban itself is KEPT (it is not the fork board), it just
        # must not resolve on an agent-runtime lane.
        "kanban_attach",
        "kanban_attach_url",
        "kanban_attachments",
        # feishu_doc toolset (1)
        "feishu_doc_read",
        # feishu_drive toolset (4)
        "feishu_drive_list_comments",
        "feishu_drive_list_comment_replies",
        "feishu_drive_reply_comment",
        "feishu_drive_add_comment",
    }
)


PERSONA_BLOCKED_TOOLS = frozenset(
    {
        "delegate_task",
        "clarify",
        "memory",
        "send_message",
        "cronjob",
    }
) | REGISTRY_HYGIENE_BLOCKED_TOOLS

def role_from_persona(persona: AgentPersona) -> AgentRole | str:
    return coerce_agent_role(persona.role)


def validate_toolsets(configured: list[str]) -> list[str]:
    """Normalize a declared toolset list: strip, drop empties, dedupe in order.

    S66 removed the ``role`` parameter. It had been unused since S61/S64 made
    profile/persona declarations the sole capability authority and deleted the
    per-role allow/deny tables (``ALLOWED_TOOLSETS_BY_ROLE`` /
    ``PER_ROLE_TOOL_DENIES``, both s11 tombstones). Accepting a role here read
    as a ceiling this function has not applied for two waves — see
    ``chat_lane_toolsets``, whose safety argument was mis-stated in exactly that
    way. There is NO role ceiling; this is a normalizer.
    """

    return list(dict.fromkeys(str(toolset).strip() for toolset in configured if str(toolset).strip()))


def blocked_tool_names() -> frozenset[str]:
    """The runtime-wide chat blocklist.

    S66 removed the ``persona`` parameter: the body has returned the module
    constant for every persona since the per-role deny tables went, so the
    argument made a constant look like a per-persona lookup. Callers that want
    the constant directly may read ``PERSONA_BLOCKED_TOOLS``; this accessor
    stays because it is the name the visibility/runtime lanes already call.
    """

    return PERSONA_BLOCKED_TOOLS


# ── the harness lane's ONE capability declaration (S0a A1, 2026-09-03) ───────
#
# Read ``docs/agent-runtime-harness/planned/s0a-atlas-cleanup.md`` §0.2 before
# changing any of this. The short version: nothing on the harness lane used to
# read a profile's ``toolsets:`` key at all. The shipped permission posture is
# ``unbounded`` and that branch resolved ``all_registered_toolsets()`` — every
# toolset registered in the process — so Neko, both devs and QA had
# byte-identical 79-tool surfaces, 17 of them withheld as registry hygiene every
# single turn, while three copies of a per-persona ``toolsets`` list (profile
# config, store row, realm-sync body) were consulted by nobody.
#
# Now the profile's top-level ``toolsets:`` IS the declaration, and this module
# is the one reader. The persona-level ``AgentPersona.toolsets`` field is legacy
# display (R-S0a-3): it is reported as ``persona_list`` in the projections and
# admits nothing.

#: What the harness lane resolves for a profile that declares nothing of its own.
HARNESS_LANE_DEFAULT_TOOLSETS: tuple[str, ...] = ("harness_core",)

#: ``ToolsetDeclaration.source`` values.
TOOLSET_SOURCE_PROFILE_CONFIG = "profile_config"
TOOLSET_SOURCE_LANE_DEFAULT = "lane_default"
TOOLSET_SOURCE_PROFILE_UNRESOLVED = "profile_unresolved"

#: The upstream CLI default (``hermes_cli.config_defaults.DEFAULT_CONFIG``).
#: Read as the default it IS rather than as an operator's choice — see
#: R-S0a-2. Kept as a literal fallback for the (import-error) case where the
#: CLI package cannot be reached from this module.
_UPSTREAM_DEFAULT_TOOLSETS: tuple[str, ...] = ("hermes-cli",)


@dataclass(frozen=True)
class ToolsetDeclaration:
    """What ONE persona's bound profile declares for the harness lane.

    ``toolsets`` is the expanded, validated member list the lane admits by;
    ``declared`` is what the config literally said (or the lane default);
    ``source`` says which of the two it was and why.
    """

    toolsets: tuple[str, ...]
    declared: tuple[str, ...]
    source: str
    profile: str | None = None
    config_path: str | None = None
    #: The legacy per-persona list, verbatim, for VISIBILITY only (A2). Never an
    #: admission input — a divergence from ``declared`` is reported, not obeyed.
    persona_list: tuple[str, ...] = ()

    def row(self) -> dict[str, object]:
        return {
            "toolsets": list(self.toolsets),
            "declared": list(self.declared),
            "source": self.source,
            "profile": self.profile,
            "config_path": self.config_path,
            "persona_list": list(self.persona_list),
        }


def _upstream_default_toolsets() -> frozenset[str]:
    try:
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        values = DEFAULT_CONFIG.get("toolsets") or []
        names = frozenset(str(name).strip() for name in values if str(name or "").strip())
        return names or frozenset(_UPSTREAM_DEFAULT_TOOLSETS)
    except Exception:  # pragma: no cover - defensive; the literal is the same value
        return frozenset(_UPSTREAM_DEFAULT_TOOLSETS)


def declared_lane_toolsets(persona: AgentPersona) -> ToolsetDeclaration:
    """The harness lane's capability declaration for ``persona``.

    Resolution (R-S0a-2), from the persona's BOUND profile — not the ambient
    ``HERMES_HOME`` — so the answer does not change with the operator's active
    profile:

    * no bound/resolvable profile home ⇒ the lane default, ``profile_unresolved``
    * ``toolsets:`` absent, not a list, empty, or exactly the upstream default
      ``["hermes-cli"]`` ⇒ the lane default, ``lane_default``
    * anything else ⇒ that list verbatim, ``profile_config``

    Treating the bare ``["hermes-cli"]`` as undeclared is reading the upstream
    default as the default it is (``hermes_cli.config_defaults`` writes it for an
    unset key), not overriding an operator: any list an operator actually wrote —
    ``[harness_core, spotify]``, or a stale explicit ``[hermes-cli, …]`` — is
    honored verbatim and shows up as ``profile_config`` in ``tool-diff``.

    Never raises and never widens: a YAML fault resolves to the narrow lane
    default, the same asymmetry ``default_permission_mode`` applies to an
    unparseable permission mode.

    Cheap and registry-free: path arithmetic plus the mtime-cached YAML parse
    ``profile_readiness`` already performs, then a static ``TOOLSETS`` expansion.
    It must NEVER import ``model_tools`` — the toolset-NAME half of an agent
    create is import-free because of that (S0a A6a), and
    ``tests/agent_runtime/test_toolset_declaration.py`` asserts it in a
    subprocess.
    """

    from toolsets import expand_toolset_names

    persona_list = tuple(
        str(name).strip()
        for name in (getattr(persona, "toolsets", None) or ())
        if str(name or "").strip()
    )

    def _resolved(
        declared: tuple[str, ...],
        source: str,
        *,
        profile: str | None,
        config_path: str | None,
    ) -> ToolsetDeclaration:
        return ToolsetDeclaration(
            toolsets=tuple(validate_toolsets(expand_toolset_names(declared))),
            declared=declared,
            source=source,
            profile=profile,
            config_path=config_path,
            persona_list=persona_list,
        )

    profile_name = str(getattr(persona, "hermes_profile", "") or "").strip() or None
    try:
        from .parse_cache import cached_yaml_file
        from .profile_context import resolve_persona_profile

        binding = resolve_persona_profile(persona)
        profile_name = binding.hermes_profile or profile_name
        if binding.profile_home is None:
            return _resolved(
                HARNESS_LANE_DEFAULT_TOOLSETS,
                TOOLSET_SOURCE_PROFILE_UNRESOLVED,
                profile=profile_name,
                config_path=None,
            )
        config_path = binding.profile_home / "config.yaml"
        raw = cached_yaml_file(config_path, default={}) or {}
        value = raw.get("toolsets") if isinstance(raw, dict) else None
    except Exception:  # pragma: no cover - defensive; a declaration read must never break a turn
        _LOGGER.debug("declared_lane_toolsets: read failed for %r", getattr(persona, "id", None), exc_info=True)
        return _resolved(
            HARNESS_LANE_DEFAULT_TOOLSETS,
            TOOLSET_SOURCE_LANE_DEFAULT,
            profile=profile_name,
            config_path=None,
        )

    path_text = str(config_path)
    if not isinstance(value, list):
        return _resolved(
            HARNESS_LANE_DEFAULT_TOOLSETS,
            TOOLSET_SOURCE_LANE_DEFAULT,
            profile=profile_name,
            config_path=path_text,
        )
    names = tuple(str(name).strip() for name in value if str(name or "").strip())
    if not names or set(names) == set(_upstream_default_toolsets()):
        return _resolved(
            HARNESS_LANE_DEFAULT_TOOLSETS,
            TOOLSET_SOURCE_LANE_DEFAULT,
            profile=profile_name,
            config_path=path_text,
        )
    return _resolved(
        names,
        TOOLSET_SOURCE_PROFILE_CONFIG,
        profile=profile_name,
        config_path=path_text,
    )


def effective_toolsets(persona: AgentPersona) -> list[str]:
    """The toolsets this persona's harness lane admits by.

    ONE authority since S0a A1: the profile's declaration
    (:func:`declared_lane_toolsets`), expanded to member toolset names. Every
    existing caller — the chat chokepoint's bounded branch, the visibility
    preview, the snapshot agents drawer, the worker/dev task lanes — follows
    from here, which is what retires ``AgentPersona.toolsets`` as an admission
    input in one place rather than five.
    """

    return list(declared_lane_toolsets(persona).toolsets)


#: Why a profile-backed chat persona inherited nothing. Typed so the fail-CLOSED
#: outcome is ACCOUNTED rather than silent: an operator whose chat has no tools
#: can be told which of these happened instead of reading an empty list.
PROFILE_CHAT_TOOLSET_NO_MATCH = "no_persona_declares_this_profile"
PROFILE_CHAT_TOOLSET_AMBIGUOUS = "profile_shared_by_multiple_personas"
PROFILE_CHAT_TOOLSET_MATCHED_EXACT = "exact_persona_id"
PROFILE_CHAT_TOOLSET_MATCHED_UNIQUE_PROFILE = "unique_profile_owner"


def profile_persona_resolution(
    profile_id: str,
    personas: list[AgentPersona] | tuple[AgentPersona, ...] | None = None,
) -> tuple[AgentPersona | None, str, tuple[str, ...]]:
    """Resolve the one persona allowed to supply profile-backed defaults.

    Exact persona ids outrank profile ownership. A unique profile owner may
    supply defaults; an unowned or multiply-owned profile inherits nothing.
    This is the single precedence authority for toolsets and the CLI's model,
    provider, API mode, autonomy, core-context and readiness defaults.
    """

    profile = str(profile_id or "").strip()
    declared = list(personas or [])
    exact = next(
        (
            persona
            for persona in declared
            if str(getattr(persona, "id", "") or "").strip()
            in {profile, f"profile:{profile}"}
        ),
        None,
    )
    profile_matches = [
        persona
        for persona in declared
        if str(getattr(persona, "hermes_profile", "") or "").strip() == profile
    ]
    candidates = tuple(
        str(getattr(persona, "id", "") or "").strip() for persona in profile_matches
    )
    if exact is not None:
        return exact, PROFILE_CHAT_TOOLSET_MATCHED_EXACT, candidates
    if len(profile_matches) == 1:
        return profile_matches[0], PROFILE_CHAT_TOOLSET_MATCHED_UNIQUE_PROFILE, candidates
    if len(profile_matches) > 1:
        return None, PROFILE_CHAT_TOOLSET_AMBIGUOUS, candidates
    return None, PROFILE_CHAT_TOOLSET_NO_MATCH, candidates


def profile_chat_toolset_resolution(
    profile_id: str,
    personas: list[AgentPersona] | tuple[AgentPersona, ...] | None = None,
) -> tuple[list[str], str, tuple[str, ...]]:
    """``(toolsets, reason, candidate_persona_ids)`` for a profile-backed chat.

    Universal chat capabilities are added later by the chat runtime. Ambiguous
    ownership remains fail-closed (S64's ruling).

    S66 split the reason out. The fail-closed arms are UNCHANGED and still
    fail closed; what changed is that they are no longer silent. An ambiguous
    shared profile used to return ``[]`` indistinguishable from "this profile
    has no persona at all", so an operator staring at a toolless chat had no
    way to tell a misconfiguration from a deliberate denial.
    """

    matching, reason, candidates = profile_persona_resolution(profile_id, personas)
    toolsets = list(getattr(matching, "toolsets", []) or []) if matching is not None else []
    return [toolset for toolset in toolsets if toolset], reason, candidates


def profile_chat_toolsets(profile_id: str, personas: list[AgentPersona] | tuple[AgentPersona, ...] | None = None) -> list[str]:
    """The toolsets half of :func:`profile_chat_toolset_resolution`.

    Emits an operator-visible warning on the ambiguous arm: inheriting nothing
    because two personas share the profile is a CONFIGURATION defect, not a
    normal state, and it must not read as an ordinary empty list.
    """

    toolsets, reason, candidates = profile_chat_toolset_resolution(profile_id, personas)
    if reason == PROFILE_CHAT_TOOLSET_AMBIGUOUS:
        _LOGGER.warning(
            "profile_chat_toolsets: profile %r is claimed by %d personas (%s); "
            "inheriting NO toolsets (fail-closed). Give the chat persona an "
            "exact persona id, or leave exactly one persona bound to this "
            "profile.",
            profile_id,
            len(candidates),
            ", ".join(candidates),
        )
    return toolsets


def all_registered_toolsets() -> list[str]:
    """Every toolset NAME the registry holds. No availability verdict.

    Through ``get_registered_toolset_names`` and not ``get_available_toolsets``,
    which answers the SAME key set — both are ``{entry.toolset for entry in <one
    snapshot>}`` — plus an ``available`` boolean per toolset that this caller
    discards. That boolean is the whole cost: it runs every toolset's
    ``check_fn``, which probes binaries, reads env and builds external clients.
    Measured warm on this checkout, first call: **3.96 s**, and this function is
    on ``perform_agent_create``'s path — the permission preview
    ``persona_instance_summary`` projects onto the wire row it emits — so an
    agent create paid four seconds of machine-capability probing to learn a list
    of strings the registry already had.

    ``from model_tools import`` and not ``from tools.registry import`` on
    purpose: importing ``model_tools`` is what POPULATES that registry, and a
    reader that reached the singleton directly would read an EMPTY one and
    answer "there are no toolsets" — a silent wrong answer, exactly the trap
    ``tool_visibility._ensure_tool_registry_populated`` documents.
    """

    from model_tools import get_registered_toolset_names

    return [str(name) for name in get_registered_toolset_names()]


# ── profile → persona promotion ───────────────────────────────────────────────
# Re-homed here from ``agent_runtime/blueprints/resolve.py`` (mission-lane removal,
# S1). It only lived under ``blueprints/`` by accident of filing: promoting a raw
# Hermes profile into a persisted persona is a persona-lifecycle operation, not
# stage routing, and its live callers are the blueprint slot resolver *and* the
# upstream ``POST /api/profiles/{name}/promote`` endpoint, which has nothing to do
# with stage graphs. It has to outlive the blueprint package.
#
def promote_profile_to_persona(
    profile_name: str,
    *,
    slot_role: str,
    personas: dict[str, AgentPersona] | None = None,
    agent_store=None,
) -> AgentPersona:
    """Mint and persist a persona that wraps the raw Hermes profile ``profile_name``.

    A profile is only a *template* — it carries no persona record — so promotion
    persists that record and points ``hermes_profile`` at the profile. When a
    matching persisted persona exists its settings are cloned; otherwise the
    supplied role remains data and runtime defaults provide the chat settings.

    Stores are imported lazily: ``agent_runtime.config`` imports this module, so a
    top-level import would close a cycle.
    """

    from agent_runtime.store import AgentStore

    store = agent_store if agent_store is not None else AgentStore()
    known = dict(personas or {})
    explicit_single_template = (
        next(iter(personas.values())) if personas is not None and len(personas) == 1 else None
    )
    cfg = None
    if not known:
        try:
            for persona in store.list_all():
                known[persona.id] = persona
        except Exception:
            pass
        from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config

        cfg = load_agent_runtime_config()
        for persona in ensure_persisted_personas(cfg):
            known.setdefault(persona.id, persona)
    template = next(
        (
            persona
            for persona in known.values()
            if str(getattr(persona, "role", "") or "").strip() == str(slot_role or "").strip()
        ),
        None,
    ) or known.get(str(slot_role or "").strip()) or explicit_single_template
    new_id = profile_name if profile_name not in known else f"{profile_name}_{slot_role}"
    if template is not None:
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
    else:
        if cfg is None:
            from agent_runtime.config import load_agent_runtime_config

            cfg = load_agent_runtime_config()
        role = str(slot_role or "").strip() or PROFILE_ROLE_SENTINEL
        persona = AgentPersona(
            id=new_id,
            display_name=f"{profile_name} ({role})",
            role=role,
            model=cfg.default_model,
            provider=cfg.default_provider,
            api_mode=cfg.default_api_mode,
            # S66 BUGFIX: this called ``profile_chat_toolsets(profile_name)``
            # with no persona list, so ``declared`` was always ``[]`` and the
            # promoted persona was ALWAYS minted with zero toolsets — reachable
            # live through ``POST /api/profiles/{name}/promote``. The declared
            # set is right here: ``known`` is the merged persona map this
            # function already built two branches up to look for a template.
            toolsets=profile_chat_toolsets(profile_name, list(known.values())),
            system_prompt_path="",
            autonomy=AutonomyLevel.PROPOSE_ONLY.value,
            hermes_profile=profile_name,
            include_profile_memory=True,
        )
    return store.save(persona)
