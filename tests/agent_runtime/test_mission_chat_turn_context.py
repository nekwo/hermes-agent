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
    assert [row["name"] for row in (item.row() for item in context.volatile_tail.shortfalls)] == [
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


def _live_records():
    """The REAL persona / instance dataclasses, not this file's fixtures.

    The churn these tests are about lives in fields the fixtures do not
    declare — ``state``, ``updated_at``, ``last_heartbeat_at``,
    ``skill_manifest_hash`` — so a fixture-shaped record could not reproduce it.
    """

    from agent_runtime.models import AgentPersona, PersonaInstance
    from agent_runtime.states import WorkerSessionState

    persona = AgentPersona(
        id="dev",
        display_name="Launcher Dev",
        role="dev",
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=["file", "search"],
        system_prompt_path="personas/dev/system.md",
        hermes_profile="dev",
    )
    instance = PersonaInstance(
        id="personainst_dev",
        persona_id="dev",
        role="dev",
        display_name="Launcher Dev",
        profile_id="dev",
        runtime_root="X:/test/root",
        state=WorkerSessionState.IDLE,
    )
    return persona, instance


def test_the_reuse_key_survives_a_turn_of_row_liveness():
    """The 2026-08-23T14:45:14Z defect, pinned.

    With ``persona_chat.hot_sessions`` finally on, the SECOND message of one
    neko chat — 45 seconds after the first, no persona / config / permission
    change between them — recorded ``resident_rebuild_runtime_signature_changed``
    and ``resident_actor_reused=0``. ``_runtime_signature`` hashed the whole
    persona-instance ROW, and a chat turn WRITES that row: ``state`` flips across
    the turn, ``updated_at`` / ``last_heartbeat_at`` are stamped on every save,
    and the mission-chat handler writes ``skill_manifest_hash`` back at the end
    of each turn. So the reuse key could never match twice and hot sessions
    bought exactly nothing.

    Everything mutated below is what one ordinary turn does to the row.
    """

    from datetime import datetime, timedelta, timezone

    from agent_runtime.states import WorkerSessionState

    persona, instance = _live_records()
    instance.state = WorkerSessionState.RUNNING
    instance.updated_at = datetime(2026, 8, 23, 14, 44, tzinfo=timezone.utc)
    instance.last_heartbeat_at = instance.updated_at
    instance.skill_manifest_hash = "manifest-turn-1"
    instance.active_run_id = "run_turn_1"
    instance.token_budget_used = 17_633
    before = _build(persona=persona, instance=instance).runtime_signature

    # ...one turn later.
    instance.state = WorkerSessionState.IDLE
    instance.updated_at = instance.updated_at + timedelta(seconds=45)
    instance.last_heartbeat_at = instance.updated_at
    instance.skill_manifest_hash = "manifest-turn-2"
    instance.active_run_id = "run_turn_2"
    instance.token_budget_used = 21_004
    persona.readiness = {"readiness": "ready", "checked_at": "2026-08-23T14:45:14Z"}
    after = _build(persona=persona, instance=instance).runtime_signature

    assert after == before, (
        "row liveness moved the resident-actor reuse key; hot sessions cannot "
        "reuse an actor across two turns of one chat"
    )


def test_the_reuse_key_still_moves_for_a_real_instance_edit():
    """The other half, and the reason the fix is an ALLOWLIST: an instance-level
    ``set-model`` writes model / provider / api_mode / reasoning_effort and their
    ``issued_at`` together, and every one of them changes what a constructed
    actor IS. A denylist that only excluded the fields we happened to notice
    would keep passing this test while quietly re-admitting the next
    per-turn-stamped field anyone adds."""

    persona, instance = _live_records()
    baseline = _build(persona=persona, instance=instance).runtime_signature

    for field, value in (
        ("model", "gpt-5.6-luna"),
        ("provider", "openai-codex"),
        ("api_mode", "chat_completions"),
        ("reasoning_effort", "high"),
        ("skill_overrides", ["harness-dev-delivery"]),
        ("display_name", "Launcher Dev (2)"),
        ("profile_id", "qa"),
    ):
        _persona_copy, edited = _live_records()
        setattr(edited, field, value)
        assert (
            _build(persona=persona, instance=edited).runtime_signature != baseline
        ), f"an instance {field} change no longer rotates the reuse key"

    for field, value in (
        ("toolsets", ["search"]),
        ("skills", ["harness-qa-verdict"]),
        ("hermes_profile", "qa"),
        ("required_mcp_servers", ["launcher_qa"]),
        ("model", "gpt-5.6-luna"),
        ("include_profile_memory", True),
    ):
        edited_persona, _instance_copy = _live_records()
        setattr(edited_persona, field, value)
        assert (
            _build(persona=edited_persona, instance=instance).runtime_signature
            != baseline
        ), f"a persona {field} change no longer rotates the reuse key"


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


def _called_names(source: str) -> set[str]:
    """Names this source actually CALLS — not names it merely mentions.

    A comment or a docstring naming an authority is not a call to it, and the
    substring check this replaced could not tell the two apart. That mattered
    the moment the defaults grew a doctrine comment naming the authority they
    reach THROUGH the bundle: the guard would have passed on the prose.
    """

    import ast

    calls: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
        if name:
            calls.add(name)
    return calls


def test_the_default_resolvers_bind_the_canonical_authorities():
    """No parallel implementations: the defaults are the SAME functions the turn
    itself would have called inline.

    Five of them now reach their authority THROUGH
    ``agent_runtime.chat_lane_bundle``, which resolves the whole chat-lane
    visibility once per turn instead of letting each caller walk
    ``permission_options_for_chat`` → ``all_registered_toolsets`` → the registry
    probe sweep on its own. That is an indirection, not a second
    implementation — so the guard follows it and asserts the authority is
    called at the far end of it.
    """

    import inspect

    from agent_runtime import chat_lane_bundle, mission_chat_turn_context, runtime_hud
    from agent_runtime.prompt_observability import load_workspace_agents_context
    from agent_runtime.queued_skills import consume_skills_for_next_turn

    #: field -> (authority called, whether it is reached through the bundle)
    sources = {
        "consume_queued_skills": (consume_skills_for_next_turn.__name__, False),
        "load_workspace_agents": (load_workspace_agents_context.__name__, False),
        "situational_hud": (runtime_hud.situational_hud_for_instance.__name__, False),
        "admitted_operating_skills": ("mission_chat_operating_skills", True),
        "capability_block": ("capability_block_for_persona", True),
        "admission_line": ("mission_chat_admission_line", True),
        "permission_state": ("permission_state_for_chat", True),
        "tool_contract": ("_enabled_toolsets_for_chat", True),
    }
    bundle_calls = _called_names(inspect.getsource(chat_lane_bundle))
    for field, (authority, via_bundle) in sources.items():
        bound = getattr(DEFAULT_RESOLVERS, field)
        # Assert the BINDING first, which the old source-only check never did: a
        # wrapper or replacement left on the field is caught here, instead of
        # ``getsource`` quietly following it and reporting on the wrapper's body.
        assert bound is getattr(mission_chat_turn_context, f"_default_{field}"), (
            f"{field} is no longer bound to the module's own default"
        )
        called = _called_names(_resolver_default_source(bound.__name__))
        if via_bundle:
            assert "chat_lane_bundle" in called, (
                f"{field} no longer resolves through the one per-turn bundle"
            )
            assert authority in bundle_calls, f"the bundle no longer calls {authority}"
        else:
            assert authority in called, f"{field} no longer calls {authority}"


# -- (7) which component moved: the 2026-08-23T19:03Z diagnosis --------------
#
# The receipt this section answers to. Serve running with hot sessions on and
# the Stage 2 boot prewarm live; the prewarm warmed this exact chat root at
# 19:03:01Z (``outcome=warmed elapsed_ms=469``); THREE consecutive turns of that
# one root at 19:03:10 / 19:03:23 / 19:03:40 each recorded
# ``resident_rebuild_runtime_signature_changed`` and ``resident_actor_reused=0``.
# Construction was cheap on those turns (10-15 ms, warm TTLs) so nothing looked
# broken -- but reuse NEVER happened, which is exactly the cost the prewarm and
# hot sessions exist to remove, unpaid.
#
# The composite key could only say "something moved". These tests drive the REAL
# builder twice across a real turn's worth of change and assert on the
# per-COMPONENT digests, which is the grain the diagnosis actually needs.


def _digests(**overrides):
    return _build(**overrides).runtime_signature_digests


def _moved(before, after):
    missing = object()
    return {
        name
        for name in set(before) | set(after)
        if before.get(name, missing) != after.get(name, missing)
    }


def test_the_signature_is_folded_from_the_components_it_publishes():
    """One composition, two folds. If the digests were taken from a SECOND
    composition they could name a component the key was never built from -- and
    a receipt that names the wrong input is worse than none."""

    from agent_runtime.mission_chat_turn_context import (
        mission_chat_runtime_signature_components,
        mission_chat_runtime_signature_digests,
        mission_chat_runtime_signature_from_components,
    )

    context = _build()
    components = mission_chat_runtime_signature_components(
        persona=_persona(),
        instance=_instance(),
        config=_Config(),
        session_id="chat-root-1",
        session_model_config={},
        model_selection={"effective_provider": "anthropic", "effective_model": "opus"},
        workspace_agents_receipt=None,
        surface_prompt="",
        resolvers=_resolvers(),
    )
    assert context.runtime_signature == mission_chat_runtime_signature_from_components(
        components
    )
    assert context.runtime_signature_digests == mission_chat_runtime_signature_digests(
        components
    )
    # EVERY component the key was folded from is named. A digest map that
    # silently omits one leaves a whole input able to move the composite while
    # the diff reports nothing moved — which is the receipt gap this stage
    # exists to close, reintroduced one component at a time.
    assert set(context.runtime_signature_digests) == set(components)


def test_two_consecutive_turn_builds_of_one_chat_move_no_component(tmp_path):
    """THE diagnosis, run through the real builder: same chat, one turn between.

    Everything an ordinary turn does happens between the two builds -- a native
    history row is appended, the instance is restamped and has its
    ``skill_manifest_hash`` written back, a goal is steered onto it, and the
    operator's workspace ``AGENTS.md`` is re-read from disk through the REAL
    loader (the suspect nobody had checked: does its receipt embed an mtime that
    moves?). The caller's ``surface_prompt`` and ``model_selection`` are the
    values the live launcher sent on both turns -- the 19:03 rows recorded an
    EMPTY surface prompt and byte-identical model selection.

    Not one of those may move a signature component. A single name in this diff
    is a chat that can never reuse its actor.
    """

    from datetime import datetime, timedelta, timezone

    from agent_runtime.prompt_observability import load_workspace_agents_context
    from agent_runtime.states import WorkerSessionState

    agents_file = tmp_path / "AGENTS.md"
    agents_file.write_text("# Workspace\nBuild the launcher.\n", encoding="utf-8")

    persona, instance = _live_records()
    instance.state = WorkerSessionState.RUNNING
    instance.updated_at = datetime(2026, 8, 23, 19, 3, 10, tzinfo=timezone.utc)
    instance.last_heartbeat_at = instance.updated_at
    instance.skill_manifest_hash = "manifest-turn-1"

    resolvers = _resolvers(load_workspace_agents=load_workspace_agents_context)
    common = dict(
        persona=persona,
        config=_Config(),
        session_id="chat-root-1",
        session_model_config={
            "source": "agent_runtime_persona_chat",
            "persona_instance_id": "personainst_dev",
            "model": "opus",
        },
        model_selection={"effective_provider": "anthropic", "effective_model": "opus"},
        agents_file=str(agents_file),
        surface_prompt="",
        resolvers=resolvers,
    )
    first = _build(instance=instance, native_history=[], **common)
    # The workspace receipt is really in the key -- otherwise this test would
    # pass by never having loaded the file it claims to guard.
    assert first.workspace_agents_receipt
    before = first.runtime_signature_digests

    # ...one turn happens.
    instance.state = WorkerSessionState.IDLE
    instance.updated_at = instance.updated_at + timedelta(seconds=13)
    instance.last_heartbeat_at = instance.updated_at
    instance.skill_manifest_hash = "manifest-turn-2"
    instance.current_chat_goal = "Land the resident-actor fix"
    instance.token_budget_used = 21004
    history = [
        {"role": "user", "content": "status?"},
        {"role": "assistant", "content": "landing it."},
    ]

    after = _digests(instance=instance, native_history=history, **common)
    assert _moved(before, after) == set(), (
        "a turn of this chat moved the resident-actor reuse key; hot sessions "
        "and the boot prewarm buy nothing for it"
    )


def test_a_registry_shaped_permission_answer_does_not_rotate_the_key():
    """The convicted component, pinned.

    ``permission_state_for_chat`` is the OPERATOR's spelled-out answer, and under
    the shipped default permission mode (``unbounded``) its ``blocked_tools``
    list is resolved over EVERY tool registered in the process. In a warm
    multi-persona ``harness serve`` that set moves whenever anything registers or
    deregisters -- another persona's MCP admission, a profile bootstrap's plugin
    pass. None of it changes what THIS chat's actor is: the agent factory is
    called with ``enabled_toolsets`` / ``blocked_tool_names``, which the key
    already carries verbatim as ``tool_contract``.
    """

    state = {
        "mode": "unbounded",
        "source": "runtime_default",
        "expired": False,
        "workdir": None,
        "repo_scope": None,
        "can_mutate_files": True,
        "can_run_terminal": True,
        "expires_at": None,
        "turns_remaining": None,
        "blocked_tools": [{"name": "kanban_task", "reason": "registry_hygiene"}],
    }
    before = _digests(
        resolvers=_resolvers(permission_state=lambda _p, **_kw: dict(state))
    )

    churned = {
        **state,
        "blocked_tools": [
            {"name": "kanban_task", "reason": "registry_hygiene"},
            {"name": "feishu_send", "reason": "registry_hygiene"},
        ],
        "turns_remaining": 4,
        "expires_at": "2026-08-23T20:00:00Z",
    }
    after = _digests(
        resolvers=_resolvers(permission_state=lambda _p, **_kw: dict(churned))
    )
    assert _moved(before, after) == set()


def test_a_real_permission_decision_still_rotates_the_key():
    """The other half. Mode, its provenance, and whether the grant behind it has
    lapsed all decide what is CONSTRUCTED (admission mode, terminal-envelope
    scope, toolset resolution), so each must still rebuild."""

    base = {"mode": "unbounded", "source": "operator", "expired": False}
    before = _digests(
        resolvers=_resolvers(permission_state=lambda _p, **_kw: dict(base))
    )
    for name, value in (
        ("mode", "read_only"),
        ("source", "runtime_default"),
        ("expired", True),
    ):
        edited = {**base, name: value}
        after = _digests(
            resolvers=_resolvers(permission_state=lambda _p, **_kw: dict(edited))
        )
        assert _moved(before, after) == {"permissions"}, (
            f"a permission {name} change no longer rotates the reuse key"
        )


def test_the_tool_contract_is_named_and_still_rotates_the_key():
    """``tool_contract`` may NOT be dropped, however volatile it is.

    ``_construct_agent`` builds the actor from these two lists and
    ``_prepare_resident_persona_chat_agent`` does not re-apply them on reuse --
    it refreshes callbacks, the cache scope and the iteration cap and nothing
    else. An actor whose tool surface moved is stale, so this rebuild is correct
    behaviour; the receipt's job is to say so by NAME.
    """

    before = _digests()
    after = _digests(
        resolvers=_resolvers(
            tool_contract=lambda _p, **_kw: {
                "enabled_toolsets": ["search", "mcp-launcher_qa"]
            }
        )
    )
    assert _moved(before, after) == {"tool_contract"}


def test_each_signature_input_is_named_by_exactly_one_component():
    """A diff is only useful if the name points at ONE input. Every suspect the
    19:03 diagnosis had to consider gets its own component."""

    baseline = _digests()
    cases = {
        "surface_prompt_sha256": dict(surface_prompt="operator surface"),
        "root": dict(session_id="chat-root-2"),
        "root_model_config_revision": dict(session_model_config={"model": "sonnet"}),
        "model": dict(
            model_selection={
                "effective_provider": "anthropic",
                "effective_model": "sonnet",
            }
        ),
        "provider": dict(
            model_selection={
                "effective_provider": "openai-codex",
                "effective_model": "opus",
            }
        ),
        "relevant_config_revision": dict(config=_Config(default_model="sonnet")),
        "runtime_root": dict(resolvers=_resolvers(store_root=lambda: "X:/other/root")),
        "instance_revision": dict(instance=_instance(display_name="Renamed")),
        # NOT ``hermes_profile``: that one input is deliberately named twice —
        # by ``persona_revision`` and by the standalone ``profile`` — because
        # the profile binding is read directly as well as through the record.
        # Two names for one input is fine for a receipt; two inputs sharing one
        # name is not, which is what this table actually guards.
        "persona_revision": dict(persona=_persona(skills=("harness-qa-verdict",))),
    }
    for component, override in cases.items():
        assert _moved(baseline, _digests(**override)) == {component}, (
            f"{component} is no longer the one component that names this input"
        )


def test_a_steered_chat_goal_is_hud_content_not_actor_identity():
    """``current_chat_goal`` left ``INSTANCE_IDENTITY_FIELDS`` for the reason
    ``goal_id`` was never in it: its readers are the chat-list title and the
    situational HUD, and the HUD reaches the model as per-turn ENVELOPE content
    a resident actor is handed fresh every turn. A steer changes what the next
    turn SAYS, not what its actor IS."""

    from agent_runtime.mission_chat_turn_context import INSTANCE_IDENTITY_FIELDS

    assert "current_chat_goal" not in INSTANCE_IDENTITY_FIELDS
    persona, instance = _live_records()
    before = _digests(persona=persona, instance=instance)
    instance.current_chat_goal = "Ship the chat-turn prep cost stage"
    assert _moved(before, _digests(persona=persona, instance=instance)) == set()


def test_the_digests_carry_no_component_VALUES():
    """The map is one-way by construction. The components include
    prompt-adjacent material (the surface-prompt hash, the resolved tool
    contract); the diagnostic is which one moved, never what it moved to."""

    context = _build(surface_prompt="a secret operator surface prompt")
    digests = context.runtime_signature_digests
    blob = "".join(digests.values())
    assert "secret" not in blob and "search" not in blob
    assert all(
        len(value) == 64 and set(value) <= set("0123456789abcdef")
        for value in digests.values()
    )
