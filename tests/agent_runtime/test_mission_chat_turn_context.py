"""The mission-chat per-turn context, asserted on its OUTPUT rather than its shape.

``_cmd_mission_chat_message`` lives in ``hermes_cli/harness_parts/persona_commands.py``,
a command part ``exec``-loaded into ``harness.py``'s globals rather than
imported. Everything assembled inside it could therefore only ever be guarded by
AST source-shape assertions — "this function calls ``render_capability_block``
and puts the result in a list literal named ``volatile_lines``". Those guards pin
the SHAPE of the code and say nothing about the BYTES the agent receives: a
refactor that kept the shape and broke the output passed, and a refactor that
changed the shape and kept the output failed.

``agent_runtime.mission_chat_turn_context`` extracts the whole assembly into an
importable builder, so these tests replace those guards with the assertions that
actually matter — on the composed context. The CLI body keeps only composition,
and the few AST guards that survive next door pin properties that genuinely live
in ITS source (the typed budget-exhausted payload literals, the
record-at-injection kwarg).
"""

from __future__ import annotations

import types
from dataclasses import dataclass

import pytest

from agent_runtime.mission_chat_turn_context import (
    DEFAULT_RESOLVERS,
    TAIL_BUDGET_BYTES,
    TAIL_CAPABILITY,
    TAIL_MCP_ADMISSION,
    TAIL_TURN_BUDGET,
    MissionChatTurnResolvers,
    build_mission_chat_turn_context,
)
from agent_runtime.runtime_hud import (
    CAPABILITY_HUD_KEY,
    RUNTIME_CONTEXT_DELIVERY_SNAPSHOT,
    extract_runtime_context_envelope,
    render_capability_block,
    render_situational_hud_block,
    volatile_hud_keys,
)
from agent_runtime.turn_budget import render_turn_budget_line
from agent_runtime.volatile_tail import STATUS_EMITTED, STATUS_EMPTY, STATUS_TRUNCATED


# ── fixtures ────────────────────────────────────────────────────────────────


# Dataclasses, not SimpleNamespaces: the runtime signature folds ``asdict``
# revisions of the persona / instance / config, exactly as production does.


@dataclass
class _Persona:
    id: str = "dev"
    display_name: str = "Launcher Dev"
    role: str = "dev"
    skills: tuple[str, ...] = ("harness-dev-delivery",)
    hermes_profile: str = "dev"
    api_mode: str | None = None


@dataclass
class _Instance:
    id: str = "personainst_dev"
    persona_id: str = "dev"
    role: str = "dev"
    display_name: str = "Launcher Dev"
    goal_id: str | None = None
    current_task_id: str | None = None
    state: str = "idle"
    mode: str = "configured"


@dataclass
class _Config:
    default_provider: str = "anthropic"
    default_model: str = "opus"


def _persona(**overrides):
    return _Persona(**overrides)


def _instance(**overrides):
    return _Instance(**overrides)


_CAPABILITY = {
    "toolsets_dropped": ["file", "terminal"],
    "restorable_via": ["agent_runtime.chat_lane.restore_toolsets.dev"],
    "envelope": {
        "lane": "mission_chat",
        "role": "dev",
        "config_key": "agent_runtime.terminal_envelope.grants.dev.mission_chat",
        "granted": ["git_push"],
        "refused_grantable": ["destructive_git"],
        "refused_hard_floor": ["credential_read"],
    },
}

_HUD = {
    "preview": True,
    "scope": {"realm": "default", "workspace": "alpha"},
    "lane": {"display_name": "Launcher Dev", "persona_instance_id": "personainst_dev"},
    "steering": {"steered_by": [], "steers": []},
}


def _resolvers(**overrides) -> MissionChatTurnResolvers:
    """Every impure seam faked, so the builder runs with no runtime root.

    That this is POSSIBLE is the point of the extraction: the same assembly
    inside the exec'd CLI body needed a live store, a skill catalog and a
    profile home before a single assertion could be made about it.
    """

    base = dict(
        consume_queued_skills=lambda **_kw: ["queued-skill"],
        required_preload_skills=lambda skills: ["harness-dev-delivery"],
        build_preloaded_skills_prompt=lambda names, **_kw: (
            "SKILL BODY for " + ",".join(names),
            list(names),
            [],
        ),
        load_workspace_agents=lambda _agents_file: None,
        capability_block=lambda _persona, **_kw: dict(_CAPABILITY),
        situational_hud=lambda _instance, **_kw: {
            **_HUD,
            "turn_budget": _kw["turn_budget"],
            CAPABILITY_HUD_KEY: _kw["capability"],
        },
        admitted_operating_skills=lambda _persona, **_kw: [],
        admission_line=lambda _persona, **_kw: "- MCP: launcher_qa (not admitted).",
        tool_contract=lambda _persona, **_kw: {"enabled_toolsets": ["search"]},
        permission_state=lambda _persona, **_kw: {"mode": "profile_default"},
        store_root=lambda: "X:/test/root",
    )
    base.update(overrides)
    return MissionChatTurnResolvers(**base)


def _build(**overrides):
    kwargs = dict(
        persona=_persona(),
        instance=_instance(),
        config=_Config(),
        session_id="chat-root-1",
        native_history=[],
        model_selection={"effective_provider": "anthropic", "effective_model": "opus"},
        session_model_config={},
        max_seconds=240.0,
        relay_deadline_epoch=None,
        relay_chain=(),
        min_relay_seconds=45.0,
        agents_file=None,
        surface_prompt="",
        resolvers=_resolvers(),
    )
    kwargs.update(overrides)
    return build_mission_chat_turn_context(**kwargs)


# ── (1) the volatile tail: roster, order, budgets ───────────────────────────


def test_the_tail_carries_the_three_registered_facts_in_roster_order():
    """The roster IS the contract. Each of these must be true for THIS turn, so
    each rides the always-emitted tail rather than the hashed body."""

    context = _build()
    assert [entry.name for entry in context.volatile_tail.entries] == [
        TAIL_TURN_BUDGET,
        TAIL_CAPABILITY,
        TAIL_MCP_ADMISSION,
    ]
    assert context.volatile_tail.complete
    assert all(entry.status == STATUS_EMITTED for entry in context.volatile_tail.entries)


def test_the_composed_tail_is_byte_identical_to_the_hand_joined_lines():
    """GOLDEN: this is a structure refactor, not a content change.

    The pre-refactor CLI body built ``"\\n".join(line for line in [budget,
    capability, admission] if line)``. Composing the same renderers through the
    budgeted roster must produce exactly those bytes for a representative
    persona — otherwise the extraction quietly changed what the model reads.
    """

    context = _build()
    legacy = "\n".join(
        line
        for line in (
            render_turn_budget_line(context.wall_budget),
            render_capability_block(context.capability),
            "- MCP: launcher_qa (not admitted).",
        )
        if line
    )
    assert context.volatile_tail.content == legacy


def test_each_contributor_owns_its_own_budget_so_none_can_crowd_out_another():
    """Per-contributor rather than one global cap, on purpose: a widened
    capability policy must not be able to silence the countdown."""

    assert set(TAIL_BUDGET_BYTES) == {TAIL_TURN_BUDGET, TAIL_CAPABILITY, TAIL_MCP_ADMISSION}
    assert all(value > 0 for value in TAIL_BUDGET_BYTES.values())

    context = _build(
        resolvers=_resolvers(
            admission_line=lambda _persona, **_kw: "- MCP: " + ("server, " * 2000)
        )
    )
    statuses = {entry.name: entry.status for entry in context.volatile_tail.entries}
    assert statuses[TAIL_MCP_ADMISSION] == STATUS_TRUNCATED
    # The over-budget contributor did not cost the others a byte.
    assert statuses[TAIL_TURN_BUDGET] == STATUS_EMITTED
    assert statuses[TAIL_CAPABILITY] == STATUS_EMITTED
    assert render_turn_budget_line(context.wall_budget) in context.volatile_tail.content
    # ...and the shortfall is visible in band AND as a typed row.
    assert "TRUNCATED" in context.volatile_tail.content
    assert [row["name"] for row in context.volatile_tail.shortfall_rows()] == [
        TAIL_MCP_ADMISSION
    ]


def test_a_lane_with_nothing_to_report_pays_no_line():
    """Honest silence. An unbounded turn drops nothing and an ungoverned lane
    refuses nothing, so neither should cost the model a bullet."""

    context = _build(
        resolvers=_resolvers(
            capability_block=lambda _persona, **_kw: {},
            admission_line=lambda _persona, **_kw: "",
        )
    )
    assert context.volatile_tail.content == render_turn_budget_line(context.wall_budget)
    statuses = {entry.name: entry.status for entry in context.volatile_tail.entries}
    assert statuses[TAIL_CAPABILITY] == STATUS_EMPTY
    assert statuses[TAIL_MCP_ADMISSION] == STATUS_EMPTY


# ── (2) pinned semantics ────────────────────────────────────────────────────


def test_the_mcp_admission_line_stays_a_separate_voice_from_the_capability_block():
    """Deliberate non-merge, pinned so nobody "tidies" it.

    MCP denials resolve at a different lifecycle point (execution-time
    degradations reach the agent through ``agent.steer``, after this envelope is
    sealed) and are gated on the admission kill switch. Folding them into the
    capability account would give one fact two voices, which is how an agent
    learns to discount both. Two contributors, two budgets, two failure modes.
    """

    context = _build()
    names = [entry.name for entry in context.volatile_tail.entries]
    assert TAIL_CAPABILITY in names and TAIL_MCP_ADMISSION in names

    # A capability fault must not blank the MCP line, and vice versa.
    only_mcp = _build(resolvers=_resolvers(capability_block=lambda _p, **_k: {}))
    assert "- MCP: launcher_qa (not admitted)." in only_mcp.volatile_tail.content
    only_cap = _build(resolvers=_resolvers(admission_line=lambda _p, **_k: ""))
    assert "Dropped on this lane" in only_cap.volatile_tail.content


def test_an_admission_line_fault_never_fails_the_turn():
    """A context line decorates a turn; it must never be able to end one."""

    def _boom(_persona, **_kw):
        raise RuntimeError("admission resolution exploded")

    context = _build(resolvers=_resolvers(admission_line=_boom))
    assert context.volatile_tail.entries[-1].status == STATUS_EMPTY
    assert render_turn_budget_line(context.wall_budget) in context.volatile_tail.content


def test_wall_budget_and_capability_ride_the_tail_and_the_hud_but_never_the_body():
    """The delivery contract, end to end.

    Both facts land on the HUD dict (so the operator's CONTEXT peek shows the
    SAME account the agent was told) AND on the tail (so the agent sees them on
    every delivery) — and never in the hashed body, which a cached ``unchanged``
    stub would serve stale and an ``unavailable`` delivery would drop entirely.
    """

    context = _build()

    assert "turn_budget" in context.situational_hud
    assert CAPABILITY_HUD_KEY in context.situational_hud
    assert volatile_hud_keys() == {"turn_budget", CAPABILITY_HUD_KEY}

    body = context.situational_hud_body()
    assert "Wall budget" not in body
    assert "Dropped on this lane" not in body
    assert "Terminal envelope" not in body

    assert "Wall budget" in context.volatile_tail.content
    assert "Dropped on this lane" in context.volatile_tail.content


@pytest.mark.parametrize(
    "delivery", ["snapshot", "unchanged", "unavailable"]
)
def test_the_tail_is_emitted_on_every_delivery(delivery):
    """Including ``unavailable``, which drops the HUD body entirely — a
    capability fact that vanished with it would leave the agent believing it
    still has what this turn took away."""

    context = _build()
    object.__setattr__(context, "situational_hud_delivery", delivery)
    if delivery == "unavailable":
        object.__setattr__(context, "situational_hud_revision", "hud_unavailable")

    envelope = context.runtime_context_envelope(context_id="ctx_test")
    assert f'delivery="{delivery}"' in envelope
    assert "Wall budget" in envelope
    assert "Dropped on this lane" in envelope


def test_the_envelope_is_well_formed_and_strippable_by_the_projection():
    """The transcript projection strips this envelope from the operator's
    displayed text; a malformed one leaks the whole HUD into the chat log."""

    context = _build()
    envelope = context.runtime_context_envelope(context_id="ctx_abc123")
    remainder, metadata = extract_runtime_context_envelope(f"hello\n\n{envelope}")
    assert remainder == "hello"
    assert metadata == {
        "context_id": "ctx_abc123",
        "revision": context.situational_hud_revision,
        "delivery": RUNTIME_CONTEXT_DELIVERY_SNAPSHOT,
    }


def test_the_body_the_envelope_carries_is_the_rendered_stable_hud():
    context = _build()
    assert context.situational_hud_body() == render_situational_hud_block(
        context.situational_hud
    )


# ── (3) the rest of the assembly ────────────────────────────────────────────


def test_the_skill_preload_is_consumed_once_and_delivered_inside_its_envelope():
    """Consuming the queue is a real mutation: a second consume would silently
    swallow an operator's queued skill. And the preload must travel wrapped, so
    the persisted native row is projection-safe by construction."""

    consumed: list[str] = []

    def _consume(*, persona_id, session_id):
        consumed.append(f"{persona_id}:{session_id}")
        return ["queued-skill"]

    context = _build(resolvers=_resolvers(consume_queued_skills=_consume))

    assert consumed == ["dev:chat-root-1"]
    assert context.skills.queued == ("queued-skill",)
    assert context.skills.required == ("harness-dev-delivery",)
    # Required first, then queued — the order the preload builder is given.
    assert context.skills.loaded == ("harness-dev-delivery", "queued-skill")
    assert context.skill_preload_prompt.startswith("<skill_preload skills=")
    assert context.skill_preload_prompt.endswith("</skill_preload>")
    assert context.skills.delivery == "snapshot"
    assert context.skills.revision.startswith("skills_")


def test_an_admitted_mcp_surface_brings_its_operating_manual_into_the_preload():
    """Live failure, 2026-07-29: a QA turn drove the admitted ``launcher_qa``
    surface, hit ``helper_low_information_capture``, and burned the turn — with
    the skill documenting that exact remedy granted to the persona and never in
    context. An admitted surface's manual is required-preload FOR THAT TURN."""

    context = _build(
        persona=_persona(
            id="qa", role="qa", skills=("harness-qa-verdict", "launcher-stagec-mcp-screenshot")
        ),
        resolvers=_resolvers(
            required_preload_skills=lambda _skills: ["harness-runtime-model"],
            admitted_operating_skills=lambda _persona, **_kw: [
                "launcher-stagec-mcp-screenshot"
            ],
        ),
    )

    # Standing policy first, the turn's admitted manual after it, both marked
    # required so the loader renders the stronger runtime-policy activation note.
    assert context.skills.required == (
        "harness-runtime-model",
        "launcher-stagec-mcp-screenshot",
    )
    assert "launcher-stagec-mcp-screenshot" in context.skills.loaded
    assert "launcher-stagec-mcp-screenshot" in context.skill_preload_prompt


def test_a_turn_with_nothing_admitted_preloads_exactly_what_it_did_before():
    """The flag-off / not-admitted / not-granted turn — every turn today — must
    be byte-identical to the pre-change preload."""

    context = _build(resolvers=_resolvers(admitted_operating_skills=lambda _p, **_kw: []))

    assert context.skills.required == ("harness-dev-delivery",)
    assert context.skills.loaded == ("harness-dev-delivery", "queued-skill")


def test_a_manual_that_is_also_standing_policy_is_loaded_once():
    context = _build(
        resolvers=_resolvers(
            required_preload_skills=lambda _skills: ["launcher-stagec-mcp-screenshot"],
            admitted_operating_skills=lambda _p, **_kw: ["launcher-stagec-mcp-screenshot"],
        )
    )

    assert context.skills.required == ("launcher-stagec-mcp-screenshot",)
    assert context.skills.loaded == ("launcher-stagec-mcp-screenshot", "queued-skill")


def test_an_operating_skill_resolution_fault_degrades_the_turn_but_never_fails_it():
    def _boom(_persona, **_kw):
        raise RuntimeError("the admission policy is wedged")

    context = _build(resolvers=_resolvers(admitted_operating_skills=_boom))

    assert context.skills.required == ("harness-dev-delivery",)


def test_a_skill_preload_fault_degrades_the_turn_but_never_fails_it():
    def _boom(_names, **_kw):
        raise RuntimeError("skill catalog unavailable")

    context = _build(resolvers=_resolvers(build_preloaded_skills_prompt=_boom))
    assert context.skill_preload_prompt == ""
    assert context.skills.loaded == ()
    assert set(context.skills.missing) == {"harness-dev-delivery", "queued-skill"}


def test_the_workspace_pointer_is_only_set_when_the_file_actually_loaded():
    """G6: only a file that LOADED points at a real workspace root. An invalid,
    missing or oversized selection must not ground the turn somewhere it never
    read — and its preview must never enter the runtime signature."""

    loaded = types.SimpleNamespace(
        content="# AGENTS",
        receipt={"included": True, "path": "X:/repo/AGENTS.md", "preview": "# AGENTS"},
    )
    context = _build(resolvers=_resolvers(load_workspace_agents=lambda _f: loaded))
    assert context.workspace_agents_content == "# AGENTS"
    assert context.workspace_agents_path == "X:/repo/AGENTS.md"
    assert "preview" not in context.workspace_agents_receipt

    rejected = types.SimpleNamespace(
        content=None, receipt={"included": False, "path": "X:/repo/AGENTS.md"}
    )
    context = _build(resolvers=_resolvers(load_workspace_agents=lambda _f: rejected))
    assert context.workspace_agents_content is None
    assert context.workspace_agents_path is None

    context = _build()
    assert context.workspace_agents is None
    assert context.workspace_agents_receipt is None
    assert context.workspace_agents_path is None


def test_the_runtime_signature_changes_with_every_reuse_relevant_input():
    """A resident actor is reusable only while every prompt/provider/tool input
    is identical, so each of these must move the signature."""

    baseline = _build().runtime_signature
    assert len(baseline) == 64

    assert _build().runtime_signature == baseline  # deterministic

    assert _build(session_id="other-root").runtime_signature != baseline
    assert (
        _build(
            model_selection={"effective_provider": "anthropic", "effective_model": "sonnet"}
        ).runtime_signature
        != baseline
    )
    assert _build(surface_prompt="extra").runtime_signature != baseline
    assert (
        _build(
            resolvers=_resolvers(tool_contract=lambda _p, **_k: {"enabled_toolsets": []})
        ).runtime_signature
        != baseline
    )
    assert (
        _build(
            resolvers=_resolvers(permission_state=lambda _p, **_k: {"mode": "unbounded"})
        ).runtime_signature
        != baseline
    )
    assert (
        _build(resolvers=_resolvers(store_root=lambda: "X:/other")).runtime_signature
        != baseline
    )


def test_the_signature_never_embeds_prompt_or_config_text():
    """Config objects are HASHED before inclusion so the signature cannot become
    an observability channel for prompt/config text."""

    persona = _persona(display_name="SECRET-NAME")
    signature = _build(persona=persona, surface_prompt="SECRET-PROMPT").runtime_signature
    assert "SECRET" not in signature
    assert set(signature) <= set("0123456789abcdef")


def test_a_relay_hop_inherits_the_shared_chain_deadline():
    """The TARGET's tail must show the SHARED remaining budget — that is how a
    supervisor learns what window a dispatch actually has instead of briefing 50
    minutes of work into a 9-minute hop (live incident 2026-07-26)."""

    import time

    deadline = time.time() + 120.0
    context = _build(
        max_seconds=3600.0,
        relay_deadline_epoch=deadline,
        relay_chain=("neko", "dev"),
    )
    assert context.wall_budget.shared is True
    assert context.wall_budget.deadline_epoch == deadline
    assert context.wall_budget.total_seconds <= 3600.0
    assert "shared with every agent on this relay chain" in context.volatile_tail.content
    assert context.situational_hud["turn_budget"]["shared"] is True


def _resolver_default_source(name: str) -> str:
    """The default resolver ``name`` as written ON DISK, looked up by NAME.

    Deliberately NOT ``inspect.getsource(DEFAULT_RESOLVERS.<field>)``. That call
    fuses two readings taken at DIFFERENT times: ``co_firstlineno``, frozen when
    the module was imported at collection, and the file's text, read from disk at
    assertion time. Anything that rewrites ``mission_chat_turn_context.py``
    in between — another agent's edit landing during a ten-minute suite run, an
    editor save, a checkout — shifts the line numbers, and ``getsource`` then
    returns a DIFFERENT function's body with no error at all. That is why this
    assertion could pass in a two-second isolated run and fail in the full suite:
    the failure tracked the length of the run, not the binding under test.

    Parsing the file once and locating the definition by name reads a single
    consistent snapshot, so it cannot desynchronize. Same remedy, for the same
    class of full-suite-only fault, as ``test_runtime_root_request_ordering.py``'s
    ``_function_def`` and ``test_terminal_envelope_grants.py``'s
    ``_upstream_receipt_writer_source``.
    """

    import ast
    from pathlib import Path

    from agent_runtime import mission_chat_turn_context

    text = Path(mission_chat_turn_context.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(text, node) or ""
    raise AssertionError(f"{name} not found in {mission_chat_turn_context.__file__}")


def test_the_default_resolvers_bind_the_canonical_authorities():
    """No parallel implementations: the defaults are the SAME functions the turn
    itself would have called inline."""

    from agent_runtime import mission_chat_turn_context, runtime_hud, tool_permissions
    from agent_runtime.prompt_observability import load_workspace_agents_context
    from agent_runtime.queued_skills import consume_skills_for_next_turn

    # Each default is a thin adapter, so assert the authority it reaches for.
    sources = {
        "consume_queued_skills": consume_skills_for_next_turn.__name__,
        "admitted_operating_skills": "mission_chat_operating_skills",
        "load_workspace_agents": load_workspace_agents_context.__name__,
        "capability_block": runtime_hud.capability_block_for_persona.__name__,
        "situational_hud": runtime_hud.situational_hud_for_instance.__name__,
        "permission_state": tool_permissions.permission_state_for_chat.__name__,
    }
    for field, authority in sources.items():
        bound = getattr(DEFAULT_RESOLVERS, field)
        # Assert the BINDING first, which the old source-only check never did: a
        # wrapper or replacement left on the field is caught here, instead of
        # ``getsource`` quietly following it and reporting on the wrapper's body.
        assert bound is getattr(mission_chat_turn_context, f"_default_{field}"), (
            f"{field} is no longer bound to the module's own default"
        )
        body = _resolver_default_source(bound.__name__)
        assert authority in body, f"{field} no longer reaches {authority}"
