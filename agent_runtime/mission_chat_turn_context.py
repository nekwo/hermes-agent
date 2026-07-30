"""Everything one mission-chat turn must KNOW, assembled in one testable place.

Why this module exists
----------------------
A mission-chat turn carries far more than the operator's sentence. Before the
model is called the harness must resolve, in order: this turn's wall budget, the
skills to preload (and their delivery envelope), the workspace ``AGENTS.md``, the
resident-actor runtime signature, this lane's capability account, the situational
HUD and its snapshot/unchanged delivery, and the volatile tail the agent reads
every single turn.

All of that lived inside ``_cmd_mission_chat_message`` in
``hermes_cli/harness_parts/persona_commands.py`` — a command part that is
``exec``-loaded into ``harness.py``'s globals (``harness._load_command_parts``)
rather than imported. That has a specific, expensive consequence: the assembly
was not reachable by a unit test. Everything guarding it had to be an AST
source-shape assertion ("this function calls ``render_capability_block`` and
puts the result in a list named ``volatile_lines``"), which pins the SHAPE of the
code and says nothing about the BYTES the agent receives. A refactor that kept
the shape and broke the output would pass; a refactor that changed the shape and
kept the output would fail. Both are the wrong answer.

This module is the extraction. :func:`build_mission_chat_turn_context` performs
the whole per-turn assembly and returns one frozen
:class:`MissionChatTurnContext`; the CLI body keeps only composition — gather
inputs, call the builder, feed the result to the runtime, send. The assembly is
now unit-testable end to end, and the AST guards that duplicated it are replaced
by tests that assert the composed OUTPUT.

Pinned semantics (do not "tidy" these)
--------------------------------------
* **The MCP admission line is a SEPARATE voice from the capability block.** Its
  denials are resolved at a different lifecycle point (execution-time
  degradations reach the agent through ``agent.steer``, after this envelope is
  sealed) and it is gated on the admission kill switch. Folding it into the
  capability account would give one fact two voices, which is how an agent
  learns to discount both.
* **Wall-budget visibility rides the volatile tail** (``8e7a37d6d``) — never the
  hashed HUD body, which a cached ``unchanged`` delivery would serve stale.
* **Capability drops and envelope grants/refusals ride the volatile tail too**
  (``ddc5af110``), and simultaneously the HUD dict, so the operator's CONTEXT
  peek shows the SAME account the agent was told.
* **An admitted MCP surface arrives WITH its operating manual.** A turn that
  is handed tools but not the document describing what to do when they refuse
  will improvise — the 2026-07-29 ``helper_low_information_capture`` burn. The
  manuals (``mcp_admission.MCP_OPERATING_SKILLS``) join the SAME required-preload
  set as ``load_policy: required_preload``, because everything downstream asks
  one question and a second list would be a second answer to it.
* **One resolve, one object.** The wall budget the agent is told about is the
  same object the runner's clamp enforces; the capability account recorded for
  the operator is the same object rendered for the agent. Resolving either twice
  is how the two views drift.

Impurity is confined to :class:`MissionChatTurnResolvers`
---------------------------------------------------------
The assembly genuinely has side effects and store reads (consuming the queued
skill list, reading the skill catalog, loading a file, reading persona/permission
state). Rather than scatter them, every one is a named field on a resolvers value
object whose default binds the canonical authority. Production passes nothing and
gets the canonical wiring; a unit test passes fakes and drives the whole builder
without a runtime root. There is no second authority — the defaults ARE the
authorities the turn itself uses.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Callable, Iterable, Sequence

from .cli_format import emit_json
from .runtime_hud import (
    render_capability_block,
    render_runtime_context_envelope,
    render_situational_hud_block,
    render_skill_preload_envelope,
    runtime_context_delivery,
    situational_hud_revision,
    skill_preload_delivery,
    skill_preload_revision,
)
from .turn_budget import TurnWallBudget, render_turn_budget_line, resolve_turn_wall_budget
from .volatile_tail import VolatileTail, VolatileTailBuilder

logger = logging.getLogger(__name__)

#: The prompt-contract revision folded into the runtime signature. A resident
#: actor is reusable only while every prompt/provider/tool input is identical,
#: and this constant is how a deliberate change to the CONTRACT (rather than to
#: any one input) invalidates every resident actor at once.
PROMPT_CONTRACT_REVISION = "mc-chat-continuity-v1"

#: Surface name the required-skill preload policy keys on.
PRELOAD_SURFACE = "mission_chat"


# ── volatile-tail roster ─────────────────────────────────────────────────────
#
# The tail is the ONE channel emitted on every delivery, so what may ride it —
# and how much room each fact gets — is a contract, not an accident of whatever
# each renderer happened to produce. Budgets are per contributor so a long
# capability account cannot squeeze out the countdown and a chatty admission
# line cannot squeeze out the capability account.
#
# Each budget is set several times the realistic maximum of its renderer (all
# three are hard-capped upstream: the capability block caps each name list at
# ``SITUATIONAL_HUD_CAPABILITY_CAP``; the admission line emits one entry per
# declared server; the budget line is a fixed two sentences). So on any standard
# turn the composed tail is byte-identical to the hand-joined list this replaced.
# The budgets exist for the day a policy widens — and when that day comes the
# shortfall is stated in band and recorded, never silently swallowed.

TAIL_TURN_BUDGET = "turn_budget"
TAIL_CAPABILITY = "capability"
TAIL_MCP_ADMISSION = "mcp_admission"

TAIL_BUDGET_BYTES: dict[str, int] = {
    TAIL_TURN_BUDGET: 1024,
    TAIL_CAPABILITY: 4096,
    TAIL_MCP_ADMISSION: 2048,
}


# ── resolvers (the only impure surface) ──────────────────────────────────────


def _default_consume_queued_skills(*, persona_id: str, session_id: str) -> list[str]:
    from .queued_skills import consume_skills_for_next_turn

    return list(consume_skills_for_next_turn(persona_id=persona_id, session_id=session_id))


def _default_required_preload_skills(skills: Sequence[Any]) -> list[str]:
    from agent.skill_utils import required_preload_skill_ids

    return list(
        required_preload_skill_ids(
            list(skills or []), surface=PRELOAD_SURFACE, root_node_mode=False
        )
    )


def _default_build_preloaded_skills_prompt(
    names: Sequence[str], *, task_id: str, required_skill_names: set[str] | None
) -> tuple[str, list[str], list[str]]:
    from agent.skill_commands import build_preloaded_skills_prompt

    kwargs = {"required_skill_names": required_skill_names} if required_skill_names else {}
    prompt, loaded, missing = build_preloaded_skills_prompt(
        list(names), task_id=task_id, **kwargs
    )
    return str(prompt or ""), list(loaded or []), list(missing or [])


def _default_admitted_operating_skills(persona: Any, *, session_id: str | None) -> list[str]:
    from .persona_runtime import mission_chat_operating_skills

    return list(mission_chat_operating_skills(persona, session_id=session_id))


def _default_load_workspace_agents(agents_file: Any) -> Any:
    from .prompt_observability import load_workspace_agents_context

    return load_workspace_agents_context(agents_file)


def _default_capability_block(persona: Any, *, session_id: str | None) -> dict[str, Any]:
    from .runtime_hud import capability_block_for_persona

    return capability_block_for_persona(persona, session_id=session_id)


def _default_situational_hud(
    instance: Any, *, turn_budget: dict[str, Any], capability: dict[str, Any]
) -> dict[str, Any]:
    from .runtime_hud import situational_hud_for_instance

    return situational_hud_for_instance(
        instance, turn_budget=turn_budget, capability=capability
    )


def _default_admission_line(persona: Any, *, session_id: str | None) -> str:
    from .persona_runtime import mission_chat_admission_line

    return mission_chat_admission_line(persona, session_id=session_id)


def _default_tool_contract(persona: Any, *, session_id: str | None) -> dict[str, Any]:
    from .persona_runtime import chat_runtime_tool_contract

    return chat_runtime_tool_contract(persona, session_id=session_id)


def _default_permission_state(persona: Any, *, session_id: str | None) -> dict[str, Any]:
    from .tool_permissions import permission_state_for_chat

    return permission_state_for_chat(persona, session_id=session_id)


def _default_store_root() -> str:
    from . import paths

    return str(paths.store_root())


@dataclass(frozen=True, slots=True)
class MissionChatTurnResolvers:
    """The impure seams the assembly needs, each bound to its ONE authority.

    Every default is the same function the turn itself would have called
    inline — this object relocates the calls, it does not reinterpret them.
    Tests override fields to drive :func:`build_mission_chat_turn_context`
    without a runtime root.
    """

    consume_queued_skills: Callable[..., list[str]] = _default_consume_queued_skills
    required_preload_skills: Callable[[Sequence[Any]], list[str]] = (
        _default_required_preload_skills
    )
    admitted_operating_skills: Callable[..., list[str]] = (
        _default_admitted_operating_skills
    )
    build_preloaded_skills_prompt: Callable[..., tuple[str, list[str], list[str]]] = (
        _default_build_preloaded_skills_prompt
    )
    load_workspace_agents: Callable[[Any], Any] = _default_load_workspace_agents
    capability_block: Callable[..., dict[str, Any]] = _default_capability_block
    situational_hud: Callable[..., dict[str, Any]] = _default_situational_hud
    admission_line: Callable[..., str] = _default_admission_line
    tool_contract: Callable[..., dict[str, Any]] = _default_tool_contract
    permission_state: Callable[..., dict[str, Any]] = _default_permission_state
    store_root: Callable[[], str] = _default_store_root


DEFAULT_RESOLVERS = MissionChatTurnResolvers()


# ── the assembled context ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MissionChatSkillPreload:
    """The turn's skill preload: what was asked for, what loaded, what is missing.

    ``prompt`` is already wrapped in its structural ``<skill_preload>`` envelope,
    because that envelope is what makes the persisted native row
    projection-safe — wrapping at the ONE producer means no downstream caller can
    forget. ``delivery`` is ``snapshot`` only when no matching snapshot survives
    in the effective native lineage (cold resume / post-compression re-anchor);
    otherwise a compact ``unchanged`` stub re-asserts the active skills.
    """

    prompt: str
    queued: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    loaded: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    revision: str = ""
    delivery: str = ""


@dataclass(frozen=True, slots=True)
class MissionChatTurnContext:
    """Everything the turn needs to know, resolved exactly once.

    The runtime-context envelope is deliberately NOT a field: it needs the
    ``context_id`` minted by the observability row, which is built FROM this
    object. :meth:`runtime_context_envelope` closes that loop without inviting a
    second HUD/tail resolution.
    """

    wall_budget: TurnWallBudget
    capability: dict[str, Any]
    situational_hud: dict[str, Any]
    situational_hud_revision: str
    situational_hud_delivery: str
    skills: MissionChatSkillPreload
    workspace_agents: Any
    workspace_agents_receipt: dict[str, Any] | None
    runtime_signature: str
    volatile_tail: VolatileTail

    # — convenience projections the CLI body used to hold as locals —

    @property
    def skill_preload_prompt(self) -> str:
        return self.skills.prompt

    @property
    def workspace_agents_content(self) -> str | None:
        return getattr(self.workspace_agents, "content", None)

    @property
    def workspace_agents_path(self) -> str | None:
        """The loaded ``AGENTS.md``'s own path — the workspace POINTER (G6).

        Only a file that actually LOADED points at a real workspace root: an
        invalid / missing / oversized selection must not ground the turn
        somewhere it never read.
        """

        receipt = getattr(self.workspace_agents, "receipt", None)
        if not isinstance(receipt, dict) or not receipt.get("included"):
            return None
        return str(receipt.get("path") or "") or None

    def situational_hud_body(self) -> str:
        """The hashed HUD body for this turn (stable fields only)."""

        return render_situational_hud_block(self.situational_hud)

    def runtime_context_envelope(self, *, context_id: str) -> str:
        """The per-turn envelope: hashed body + always-emitted volatile tail."""

        return render_runtime_context_envelope(
            context_id=str(context_id),
            revision=self.situational_hud_revision,
            delivery=self.situational_hud_delivery,
            situational_hud_content=self.situational_hud_body(),
            volatile_content=self.volatile_tail.content,
        )


# ── the builder ──────────────────────────────────────────────────────────────


def build_mission_chat_turn_context(
    *,
    persona: Any,
    instance: Any,
    config: Any,
    session_id: str,
    native_history: Iterable[dict[str, Any]] | None,
    model_selection: dict[str, Any],
    session_model_config: dict[str, Any] | None = None,
    max_seconds: Any,
    relay_deadline_epoch: float | None,
    relay_chain: Iterable[str] = (),
    min_relay_seconds: float,
    agents_file: Any = None,
    surface_prompt: str = "",
    resolvers: MissionChatTurnResolvers = DEFAULT_RESOLVERS,
) -> MissionChatTurnContext:
    """Resolve one mission-chat turn's whole context. Order is load-bearing.

    The wall budget is resolved BEFORE the HUD because the HUD carries its
    projection; the capability account is resolved before the HUD for the same
    reason; the skill preload is resolved before the envelope because the
    envelope's delivery is chosen against the CONTENT's revision. Each fact is
    resolved once and only once — a second resolve is how the operator's
    recorded view and the agent's rendered view start describing different
    turns.
    """

    # Materialised once: TWO delivery decisions scan the lineage (the skill
    # preload's and the HUD's), and a one-shot iterable would leave the second
    # scan looking at an empty history — which reads as "no matching snapshot
    # survives" and silently re-snapshots every turn.
    history = list(native_history or ())

    skills = _resolve_skill_preload(
        persona=persona,
        session_id=session_id,
        native_history=history,
        resolvers=resolvers,
    )

    workspace_agents = resolvers.load_workspace_agents(agents_file)
    workspace_agents_receipt = None
    if workspace_agents is not None:
        receipt = getattr(workspace_agents, "receipt", None)
        workspace_agents_receipt = dict(receipt) if isinstance(receipt, dict) else {}
        # The preview is operator-facing content, not a runtime input: folding
        # it into the signature would make the signature an observability
        # channel for prompt text.
        workspace_agents_receipt.pop("preview", None)

    runtime_signature = _runtime_signature(
        persona=persona,
        instance=instance,
        config=config,
        session_id=session_id,
        session_model_config=session_model_config or {},
        model_selection=model_selection,
        workspace_agents_receipt=workspace_agents_receipt,
        surface_prompt=surface_prompt,
        resolvers=resolvers,
    )

    # Wall budget for this turn, resolved ONCE (single authority: the same object
    # arms the runner's checkpoint clamp and renders the agent's budget line). A
    # relayed hop inherits the chain deadline, so the TARGET's HUD shows the
    # SHARED remaining budget — that is how a supervisor learns what window a
    # dispatch actually has instead of briefing 50 minutes of work into a
    # 9-minute hop (live incident 2026-07-26).
    wall_budget = resolve_turn_wall_budget(
        max_seconds=max_seconds,
        relay_deadline_epoch=relay_deadline_epoch,
        relay_chain=relay_chain,
        min_relay_seconds=min_relay_seconds,
    )

    # This lane's capability account, resolved ONCE (G5): the typed chat-lane
    # drops plus the terminal envelope's grant/refusal posture for this role. It
    # rides the HUD dict for operator parity (the CONTEXT peek shows exactly what
    # the agent was told) AND the volatile tail for the agent — never the hashed
    # body.
    capability = resolvers.capability_block(persona, session_id=session_id) or {}

    situational_hud = (
        resolvers.situational_hud(
            instance, turn_budget=wall_budget.hud_block(), capability=capability
        )
        or {}
    )
    revision = situational_hud_revision(situational_hud)
    delivery = runtime_context_delivery(history, revision)

    volatile_tail = _compose_volatile_tail(
        persona=persona,
        session_id=session_id,
        wall_budget=wall_budget,
        capability=capability,
        resolvers=resolvers,
    )

    return MissionChatTurnContext(
        wall_budget=wall_budget,
        capability=capability,
        situational_hud=situational_hud,
        situational_hud_revision=revision,
        situational_hud_delivery=delivery,
        skills=skills,
        workspace_agents=workspace_agents,
        workspace_agents_receipt=workspace_agents_receipt,
        runtime_signature=runtime_signature,
        volatile_tail=volatile_tail,
    )


def _compose_volatile_tail(
    *,
    persona: Any,
    session_id: str,
    wall_budget: TurnWallBudget,
    capability: dict[str, Any],
    resolvers: MissionChatTurnResolvers,
) -> VolatileTail:
    """The registered roster of facts that must be true THIS turn.

    Every contributor here describes something a cached ``unchanged`` delivery
    (or an ``unavailable`` one, which drops the HUD body entirely) must not be
    allowed to serve stale: how much wall clock is left, what this lane's policy
    took away and what its envelope will refuse, and which declared MCP servers
    this turn did not get. Each renderer returns ``""`` when it has nothing to
    report, so a lane with no drops and no denials pays no line.

    The MCP line stays a SEPARATE contributor from the capability account on
    purpose — see this module's docstring.
    """

    builder = VolatileTailBuilder()
    builder.add(
        TAIL_TURN_BUDGET,
        render_turn_budget_line(wall_budget),
        budget_bytes=TAIL_BUDGET_BYTES[TAIL_TURN_BUDGET],
    )
    builder.add(
        TAIL_CAPABILITY,
        render_capability_block(capability),
        budget_bytes=TAIL_BUDGET_BYTES[TAIL_CAPABILITY],
    )
    builder.add(
        TAIL_MCP_ADMISSION,
        _safe_admission_line(persona, session_id=session_id, resolvers=resolvers),
        budget_bytes=TAIL_BUDGET_BYTES[TAIL_MCP_ADMISSION],
    )
    return builder.build()


def _safe_admission_line(
    persona: Any, *, session_id: str, resolvers: MissionChatTurnResolvers
) -> str:
    try:
        return str(resolvers.admission_line(persona, session_id=session_id) or "")
    except Exception:  # pragma: no cover - a context line must never fail a turn
        logger.debug("MCP admission line unavailable for this turn", exc_info=True)
        return ""


def _safe_admitted_operating_skills(
    persona: Any, *, session_id: str, resolvers: MissionChatTurnResolvers
) -> list[str]:
    """Same never-fails contract as the admission LINE, for the same reason.

    Both read the same admission policy, and neither is worth a failed turn: a
    turn that loses its manual is degraded, a turn that raises is lost.
    """

    try:
        return list(
            resolvers.admitted_operating_skills(persona, session_id=session_id) or []
        )
    except Exception:  # pragma: no cover - a context input must never fail a turn
        logger.debug("admitted MCP operating skills unavailable for this turn", exc_info=True)
        return []


def _resolve_skill_preload(
    *,
    persona: Any,
    session_id: str,
    native_history: Iterable[dict[str, Any]] | None,
    resolvers: MissionChatTurnResolvers,
) -> MissionChatSkillPreload:
    """Consume the queued skills, load the preload, wrap it in its envelope.

    Consuming the queue is a real mutation and happens exactly once per turn, in
    this one place, ahead of every other resolution — a second consume would
    silently swallow an operator's queued skill.

    ``required`` has TWO producers and one meaning: "runtime policy says the
    model must be holding this skill on this turn".

    * The persona's own grants whose frontmatter declares
      ``load_policy: required_preload`` for this surface — the standing answer.
    * The operating manual(s) for whatever MCP surface THIS run was actually
      admitted (``mcp_admission.MCP_OPERATING_SKILLS``) — the per-turn answer.
      Handing an agent a tool surface while withholding the document that says
      what to do when that surface refuses is what turned one live
      ``helper_low_information_capture`` into a burned QA turn (2026-07-29): the
      remedy was written down, granted to the persona, and never in context.

    They merge into one list rather than two because everything downstream —
    the ``required_skill_names`` marking that renders the stronger "runtime
    policy requires this skill" activation note, the observability row's
    ``required_preload_skills``, the missing-skill accounting — asks the same
    question, and a second list would be a second answer to it. Standing policy
    comes first so a turn's admitted manual can never displace it; ``dict``
    de-duplication keeps a skill that is both from being loaded twice.
    """

    queued = list(
        resolvers.consume_queued_skills(
            persona_id=str(getattr(persona, "id", "") or ""), session_id=session_id
        )
        or []
    )
    required = list(
        dict.fromkeys(
            [
                *resolvers.required_preload_skills(getattr(persona, "skills", []) or []),
                *_safe_admitted_operating_skills(
                    persona, session_id=session_id, resolvers=resolvers
                ),
            ]
        )
    )
    to_preload = list(dict.fromkeys([*required, *queued]))

    prompt = ""
    loaded: list[str] = []
    missing: list[str] = []
    if to_preload:
        try:
            prompt, loaded, missing = resolvers.build_preloaded_skills_prompt(
                to_preload,
                task_id=session_id,
                required_skill_names=set(required) if required else None,
            )
        except Exception:
            # A preload fault degrades the turn's skills; it never fails the
            # turn. The missing list is what the observability row reports.
            logger.debug("skill preload failed for this turn", exc_info=True)
            prompt, loaded, missing = "", [], list(to_preload)

    revision = skill_preload_revision(prompt)
    delivery = skill_preload_delivery(native_history, revision)
    return MissionChatSkillPreload(
        prompt=render_skill_preload_envelope(
            skill_names=loaded,
            skill_preload_content=prompt,
            revision=revision,
            delivery=delivery,
        ),
        queued=tuple(queued),
        required=tuple(required),
        loaded=tuple(loaded),
        missing=tuple(missing),
        revision=revision,
        delivery=delivery,
    )


def _revision_hash(value: Any) -> str:
    return hashlib.sha256(emit_json(value).encode("utf-8")).hexdigest()


def _as_plain(value: Any) -> Any:
    return asdict(value) if is_dataclass(value) and not isinstance(value, type) else value


def _runtime_signature(
    *,
    persona: Any,
    instance: Any,
    config: Any,
    session_id: str,
    session_model_config: dict[str, Any],
    model_selection: dict[str, Any],
    workspace_agents_receipt: dict[str, Any] | None,
    surface_prompt: str,
    resolvers: MissionChatTurnResolvers,
) -> str:
    """Reuse key for a resident actor: identical inputs ⇒ reusable actor.

    Config objects are HASHED before inclusion rather than embedded, so the
    signature can never become an observability channel for prompt/config text.
    """

    return _revision_hash(
        {
            "persona_revision": _revision_hash(_as_plain(persona)),
            "instance_revision": _revision_hash(_as_plain(instance)),
            "root": session_id,
            "root_model_config_revision": _revision_hash(session_model_config),
            "provider": model_selection.get("effective_provider"),
            "model": model_selection.get("effective_model"),
            "api_mode": model_selection.get("effective_api_mode")
            or getattr(persona, "api_mode", None),
            "reasoning_effort": model_selection.get("effective_reasoning_effort"),
            "profile": getattr(persona, "hermes_profile", None),
            "tool_contract": resolvers.tool_contract(persona, session_id=session_id),
            "permissions": resolvers.permission_state(persona, session_id=session_id),
            "relevant_config_revision": _revision_hash(_as_plain(config)),
            "workspace_agents": workspace_agents_receipt,
            "surface_prompt_sha256": hashlib.sha256(
                str(surface_prompt or "").encode("utf-8")
            ).hexdigest(),
            "runtime_root": resolvers.store_root(),
            "prompt_contract_revision": PROMPT_CONTRACT_REVISION,
        }
    )


__all__ = [
    "DEFAULT_RESOLVERS",
    "PRELOAD_SURFACE",
    "PROMPT_CONTRACT_REVISION",
    "TAIL_BUDGET_BYTES",
    "TAIL_CAPABILITY",
    "TAIL_MCP_ADMISSION",
    "TAIL_TURN_BUDGET",
    "MissionChatSkillPreload",
    "MissionChatTurnContext",
    "MissionChatTurnResolvers",
    "build_mission_chat_turn_context",
]
