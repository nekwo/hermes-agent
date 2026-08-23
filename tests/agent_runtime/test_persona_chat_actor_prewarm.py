"""Stage 2: the resident chat actor is built BEFORE the operator's first message.

What has to be true for this to be worth anything, in order of how badly each
one bites when it is false:

1. **The prewarmed actor is REUSED by the next real turn.** ``acquire`` reuses
   only on a byte-equal signature and revision; a mismatch CLOSES the entry and
   rebuilds, so a prewarm whose key the turn does not reproduce is pure cost —
   worse than none. The live 2026-08-23T14:45:14Z record
   (``resident_rebuild_runtime_signature_changed``) is what that failure looks
   like in the field.
2. **The signature is the TURN's, computed by the turn's own function.** Not an
   equal one — the same one. The parity gate below is the whole reason
   ``mission_chat_runtime_signature`` is public.
3. **The prewarm runs no turn.** Construction and registration, then stop: no
   conversation, no provider call, no ``agent_ready``.
4. **It yields to real work.** ``_WORKDIR_LOCK`` serializes every run in the
   process; a prewarm holding it while an operator message arrives would ADD the
   construction to that turn instead of removing it.
5. **It is inert without a registry.** A CLI one-shot must not pay it, must not
   start a thread, and must not warm anything it cannot store.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass

import pytest

from agent_runtime import persona_chat_actor_prewarm as prewarm_module
from agent_runtime.persona_chat_actor_prewarm import (
    OUTCOME_ALREADY_RESIDENT,
    OUTCOME_REGISTRY_OFF,
    OUTCOME_SKIPPED_NO_CHAT_ROOT,
    OUTCOME_SKIPPED_TURN_ACTIVE,
    OUTCOME_WARMED,
    _boot_candidates,
    prewarm_chat_actor,
    prewarm_chat_actors_on_boot,
    request_chat_actor_prewarm,
)
from agent_runtime.persona_chat_continuity import PersonaChatRuntimeRegistry
from agent_runtime.profile_runner import (
    AgentRunRequest,
    ProfileAgentRunner,
    ProfileRunnerError,
    agent_runs_in_flight,
)


# ── fakes ────────────────────────────────────────────────────────────────────


class _Agent:
    """The same construction-counting fake shape ``test_send_path_runner_reuse``
    uses, plus a record of whether a conversation was ever run on it."""

    constructed = 0

    def __init__(self, **kwargs):
        type(self).constructed += 1
        self.kwargs = kwargs
        self.conversations = 0
        self.session_id = kwargs.get("session_id") or "session_fake"
        self.provider = kwargs.get("provider")
        self.model = kwargs.get("model")
        self.base_url = "https://example.invalid/v1"
        self.messages = []
        self._persist_user_message_idx = None
        self.status_callback = kwargs.get("status_callback")
        self.max_iterations = kwargs.get("max_iterations")
        self.cache_scope_id = kwargs.get("cache_scope_id")
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tool_start_callback = kwargs.get("tool_start_callback")
        self.tool_complete_callback = kwargs.get("tool_complete_callback")
        self.clarify_callback = kwargs.get("clarify_callback")

    def run_conversation(self, user_message, system_message=None, task_id=None, **kw):
        self.conversations += 1
        return {
            "final_response": "ok",
            "session_id": self.session_id,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "messages": [{"role": "assistant", "content": "ok"}],
            "api_calls": 1,
            "total_tokens": 3,
        }


@pytest.fixture(autouse=True)
def _reset_constructed():
    _Agent.constructed = 0
    yield
    _Agent.constructed = 0


@pytest.fixture(autouse=True)
def _drain_pending():
    """The module's queue is process-global; keep tests from inheriting it."""

    with prewarm_module._lock:
        prewarm_module._pending.clear()
    yield
    with prewarm_module._lock:
        prewarm_module._pending.clear()


@pytest.fixture
def stub_runtime(monkeypatch):
    def _resolve(requested, target_model):
        return {
            "provider": requested,
            "model": target_model,
            "api_mode": "codex_responses",
        }

    monkeypatch.setattr(
        "agent_runtime.profile_runner.resolve_runtime_provider", _resolve
    )


def _request(*, prewarm_only, registry, signature="sig-1", revision="rev-1", **overrides):
    fields = dict(
        prewarm_only=prewarm_only,
        profile=None,
        provider="openai-codex",
        model="gpt-5.6-luna",
        api_mode="codex_responses",
        session_id="session_1",
        root_chat_session_id="chat_root_1",
        persona_chat_runtime_registry=registry,
        persona_chat_runtime_signature=signature,
        persona_chat_native_revision=revision,
        user_message="operator asks",
        task_id="turn_1",
    )
    fields.update(overrides)
    return AgentRunRequest(**fields)


# ── (1) the point: the next real turn reuses what the prewarm built ─────────


def test_the_first_real_turn_after_a_prewarm_constructs_nothing(stub_runtime):
    """§2.3's 3.0-3.6 s moves off the turn's critical path, or nothing does.

    *Killing mutation:* have ``prewarm`` skip ``acquire`` (construct and
    discard). *Probed field:* ``constructed``, plus the runner's own
    ``resident_actor_reused`` receipt — the one the turn record's
    ``agent_init_cold`` is derived from.
    """

    registry = PersonaChatRuntimeRegistry()
    runner = ProfileAgentRunner(agent_factory=_Agent)

    timing = runner.prewarm(_request(prewarm_only=True, registry=registry))
    assert _Agent.constructed == 1
    assert timing["resident_actor_reused"] == 0

    result = runner.run(_request(prewarm_only=False, registry=registry))

    assert _Agent.constructed == 1, "the operator's first message built nothing"
    assert result.profile_timing["resident_actor_reused"] == 1
    assert "agent_construct_ms" not in result.profile_timing
    assert not any(key.startswith("resident_rebuild_") for key in result.profile_timing)


def test_a_prewarm_under_a_signature_the_turn_does_not_reproduce_is_discarded(
    stub_runtime,
):
    """The failure mode this stage is one mistake away from, pinned as a fact.

    Stated as a test rather than a comment because it is the reason the
    signature is computed by the turn's own function: a prewarm keyed on
    ANYTHING the turn re-derives differently costs a construction and then a
    rebuild.
    """

    registry = PersonaChatRuntimeRegistry()
    runner = ProfileAgentRunner(agent_factory=_Agent)

    runner.prewarm(_request(prewarm_only=True, registry=registry, signature="sig-a"))
    result = runner.run(_request(prewarm_only=False, registry=registry, signature="sig-b"))

    assert _Agent.constructed == 2
    assert result.profile_timing["resident_rebuild_runtime_signature_changed"] == 1


def test_a_prewarm_of_an_already_resident_root_builds_nothing(stub_runtime):
    """A real turn that got there first is SUCCESS, not a miss — ``acquire``
    hands back the entry and the prewarm must not double-build."""

    registry = PersonaChatRuntimeRegistry()
    runner = ProfileAgentRunner(agent_factory=_Agent)

    runner.run(_request(prewarm_only=False, registry=registry))
    assert _Agent.constructed == 1

    timing = runner.prewarm(_request(prewarm_only=True, registry=registry))

    assert _Agent.constructed == 1
    assert timing["resident_actor_reused"] == 1


# ── (2) parity: the same function, not an equal one ─────────────────────────


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
    state: str = "idle"
    mode: str = "configured"
    updated_at: str = "2026-08-23T10:00:00Z"


@dataclass
class _Config:
    default_provider: str = "anthropic"
    default_model: str = "opus"


def _parity_resolvers():
    from agent_runtime.mission_chat_turn_context import MissionChatTurnResolvers

    return MissionChatTurnResolvers(
        consume_queued_skills=lambda **_kw: [],
        required_preload_skills=lambda skills: [],
        build_preloaded_skills_prompt=lambda names, **_kw: ("", list(names), []),
        load_workspace_agents=lambda _agents_file: None,
        capability_block=lambda _persona, **_kw: {},
        situational_hud=lambda _instance, **_kw: {},
        admitted_operating_skills=lambda _persona, **_kw: [],
        admission_line=lambda _persona, **_kw: "",
        tool_contract=lambda _persona, **_kw: {"enabled_toolsets": ["search"]},
        permission_state=lambda _persona, **_kw: {"mode": "unbounded"},
        store_root=lambda: "X:/test/root",
    )


def test_the_prewarms_signature_helper_is_the_builders_own(monkeypatch):
    """The parity gate. ``mission_chat_runtime_signature`` must produce, for the
    turn's inputs, exactly the string ``build_mission_chat_turn_context`` puts on
    the context — because the prewarm calls the former and the turn ships the
    latter, and ``acquire`` compares them for equality.

    *Killing mutation:* give the builder a private signature path again (or
    change one folded input in only one of the two). *Probed field:* string
    equality of the two digests.
    """

    from agent_runtime.mission_chat_turn_context import (
        build_mission_chat_turn_context,
        mission_chat_runtime_signature,
    )

    resolvers = _parity_resolvers()
    shared = dict(
        persona=_Persona(),
        instance=_Instance(),
        config=_Config(),
        session_id="chat-root-1",
        session_model_config={},
        model_selection={"effective_provider": "anthropic", "effective_model": "opus"},
        surface_prompt="",
    )

    context = build_mission_chat_turn_context(
        native_history=[],
        max_seconds=240.0,
        relay_deadline_epoch=None,
        relay_chain=(),
        min_relay_seconds=45.0,
        agents_file=None,
        resolvers=resolvers,
        **shared,
    )
    direct = mission_chat_runtime_signature(
        workspace_agents_receipt=None, resolvers=resolvers, **shared
    )

    assert direct == context.runtime_signature


def _live_chat_root(persona_id: str = "dev") -> tuple[str, object, object]:
    """A real persona instance bound to a real, durable chat root.

    Through the production seams — ``PersonaInstanceStore.open_chat`` and
    ``ensure_persona_chat_session`` — because the whole subject here is whether
    the prewarm reads the SAME state the send path reads.
    """

    from agent_runtime.persona_assignments import PersonaInstanceStore
    from agent_runtime.persona_chat_durability import (
        default_persona_session_db,
        ensure_persona_chat_session,
    )

    instance = PersonaInstanceStore().open_chat(
        persona_id=persona_id, session_id=f"persona_chat_personainst_{persona_id}_warm"
    )
    session_db = default_persona_session_db()
    ensure_persona_chat_session(
        session_db=session_db,
        session_id=instance.default_chat_session_id,
        persona_id=persona_id,
        title="warm",
        required=True,
    )
    return instance.default_chat_session_id, instance, session_db


def test_the_assembled_request_carries_the_signature_the_turn_would_ship(
    persisted_persona_samples, bundled_persona_profiles
):
    """END TO END, against real stores: the digest the prewarm hands ``acquire``
    is byte-identical to the one ``build_mission_chat_turn_context`` puts on the
    turn context for that same chat.

    This is the gate the whole stage rests on. Every other test here can pass
    with a prewarm that warms an actor the first turn throws away; only this one
    catches that, and it catches it through the real persona resolution, the real
    instance model-override fold, the real chat-scoped model cascade, the real
    config load and the real chat-lane bundle.

    *Killing mutation:* change any single input the prewarm reproduces — resolve
    the persona without ``apply_instance_model_overrides``, pass a fabricated
    ``workspace_agents_receipt``, key on the native tip instead of the root.
    *Probed field:* string equality of the two digests.
    """

    from agent_runtime.config import load_agent_runtime_config
    from agent_runtime.mission_chat_turn_context import build_mission_chat_turn_context
    from agent_runtime.models import apply_instance_model_overrides
    from hermes_cli.harness_parts.persona_commands import (
        _chat_effective_model_payload,
        _chat_model_override_from_config,
        _persona_by_id,
        _persona_chat_native_history,
        _persona_chat_native_tip,
        _session_model_config,
    )

    root, instance, session_db = _live_chat_root()

    request, _runner = prewarm_module._prepare(root, None)

    cfg = load_agent_runtime_config()
    persona = apply_instance_model_overrides(_persona_by_id(cfg, "dev"), instance)
    session_model_config = _session_model_config(session_db, root)
    context = build_mission_chat_turn_context(
        persona=persona,
        instance=instance,
        config=cfg,
        session_id=root,
        native_history=_persona_chat_native_history(
            session_db, _persona_chat_native_tip(session_db, root)
        ),
        model_selection=_chat_effective_model_payload(
            persona=persona,
            config=cfg,
            override=_chat_model_override_from_config(session_model_config),
            instance=instance,
        ),
        session_model_config=session_model_config,
        max_seconds=240.0,
        relay_deadline_epoch=None,
        relay_chain=(),
        min_relay_seconds=45.0,
        agents_file=None,
        surface_prompt="",
    )

    assert request.persona_chat_runtime_signature == context.runtime_signature


def test_the_assembled_request_keys_acquire_on_the_root_the_tip_and_the_revision(
    persisted_persona_samples, bundled_persona_profiles
):
    """``acquire`` compares three things, not one: a stale ``active_session_id``
    or ``revision`` is a REBUILD, so all three must come from the send path's own
    helpers."""

    from hermes_cli.harness_parts.persona_commands import (
        _persona_chat_native_revision,
        _persona_chat_native_tip,
    )

    root, _instance, session_db = _live_chat_root()

    request, _runner = prewarm_module._prepare(root, None)

    assert request.prewarm_only is True
    assert request.root_chat_session_id == root
    assert request.session_id == _persona_chat_native_tip(session_db, root)
    assert request.persona_chat_native_revision == _persona_chat_native_revision(
        session_db, root
    )
    # The lane's identity, not a turn's: no callbacks, no user message.
    assert request.progress_callback is None
    assert request.stream_callback is None
    assert request.agent_ready_callback is None
    assert request.user_message == ""


def test_the_assembly_folds_the_instance_model_override_into_the_persona(
    persisted_persona_samples, bundled_persona_profiles
):
    """The send path folds the instance tier BEFORE it resolves anything else.
    Folding it anywhere but there keys the actor on a persona nobody runs —
    which is one ``set-model`` away from a permanent rebuild.

    Probed on ``api_mode`` and ``skills``: those reach the request and the
    signature ONLY through the folded persona. ``provider``/``model`` would be a
    weak probe, because the model cascade reads the instance a second time and
    would produce the right answer with the fold removed.
    """

    from agent_runtime.persona_assignments import PersonaInstanceStore

    root, instance, _session_db = _live_chat_root()
    instance.api_mode = "chat_completions"
    instance.provider = "openai-codex"
    instance.model = "gpt-5.6-luna"
    instance.skill_overrides = ["harness-continuity"]
    PersonaInstanceStore().update(instance)

    request, _runner = prewarm_module._prepare(root, None)

    assert request.api_mode == "chat_completions"
    assert (request.provider, request.model) == ("openai-codex", "gpt-5.6-luna")


def test_the_signature_tracks_an_instance_model_override(
    persisted_persona_samples, bundled_persona_profiles
):
    """A real ``set-model`` must rotate the reuse key — that is the property the
    identity allowlist was rebuilt to preserve — and the prewarm must rotate it
    the SAME way, or an override leaves the operator on a prewarmed actor built
    for the previous model."""

    from agent_runtime.persona_assignments import PersonaInstanceStore

    root, instance, _session_db = _live_chat_root()
    before, _ = prewarm_module._prepare(root, None)

    instance.api_mode = "chat_completions"
    instance.model = "gpt-5.6-luna"
    instance.provider = "openai-codex"
    PersonaInstanceStore().update(instance)
    after, _ = prewarm_module._prepare(root, None)

    assert (
        before.persona_chat_runtime_signature != after.persona_chat_runtime_signature
    )


# ── (3) a prewarm runs no turn ──────────────────────────────────────────────


def test_the_prewarm_runs_no_conversation_and_no_agent_ready(stub_runtime):
    """Construction and registration, then stop. A prewarm that ran the turn
    would send the operator a reply to a message they never wrote."""

    registry = PersonaChatRuntimeRegistry()
    ready_calls: list[object] = []
    runner = ProfileAgentRunner(agent_factory=_Agent)

    runner.prewarm(
        _request(
            prewarm_only=True,
            registry=registry,
            agent_ready_callback=lambda agent: ready_calls.append(agent),
        )
    )

    entry, reused, _ = registry.acquire(
        root_session_id="chat_root_1",
        active_session_id="session_1",
        signature="sig-1",
        revision="rev-1",
        factory=lambda: pytest.fail("the prewarm left no resident entry"),
    )
    assert reused
    assert entry.agent.conversations == 0
    assert ready_calls == []


def test_prewarm_refuses_a_request_that_is_not_marked_prewarm_only(stub_runtime):
    """One entry point, one flag. A plain turn request reaching ``prewarm``
    would run ``_execute_agent_run`` to completion — a whole provider turn with
    nobody listening."""

    runner = ProfileAgentRunner(agent_factory=_Agent)

    with pytest.raises(ProfileRunnerError):
        runner.prewarm(_request(prewarm_only=False, registry=PersonaChatRuntimeRegistry()))

    assert _Agent.constructed == 0


def test_a_prewarmed_actor_is_handed_over_with_no_turn_local_handles(stub_runtime):
    """A warm actor has ONE state, whether it was warmed by a completed turn or
    by a prewarm — otherwise the first real turn inherits prewarm-shaped
    callbacks nobody rebinds."""

    registry = PersonaChatRuntimeRegistry()
    runner = ProfileAgentRunner(agent_factory=_Agent)

    runner.prewarm(_request(prewarm_only=True, registry=registry))

    entry, _, _ = registry.acquire(
        root_session_id="chat_root_1",
        active_session_id="session_1",
        signature="sig-1",
        revision="rev-1",
        factory=lambda: pytest.fail("no resident entry"),
    )
    assert entry.agent.status_callback is None
    assert entry.agent.tool_start_callback is None
    assert entry.agent.clarify_callback is None


# ── (4) yielding to real work ───────────────────────────────────────────────


def test_the_run_gauge_is_zero_outside_a_run_and_nonzero_inside(stub_runtime):
    """The gauge the yield rule reads. Nothing else in the harness can answer
    "is a turn in flight" before that turn reaches the workdir lock."""

    observed: list[int] = []

    class _Peeking(_Agent):
        def run_conversation(self, *args, **kwargs):
            observed.append(agent_runs_in_flight())
            return super().run_conversation(*args, **kwargs)

    assert agent_runs_in_flight() == 0
    ProfileAgentRunner(agent_factory=_Peeking).run(
        _request(prewarm_only=False, registry=None, root_chat_session_id=None)
    )

    assert observed == [1]
    assert agent_runs_in_flight() == 0, "the gauge must not leak past the run"


def test_a_prewarm_stands_down_while_a_real_turn_is_in_flight(stub_runtime, monkeypatch):
    """The contention rule, driven by a genuinely concurrent run rather than a
    patched gauge: the prewarm is called from INSIDE a live turn's conversation.

    *Killing mutation:* drop the ``agent_runs_in_flight`` guard. *Probed field:*
    the returned outcome token — with the guard gone the call proceeds into the
    scope stack and returns a warm/skip token from further down, and (on the
    real lane) blocks on ``_WORKDIR_LOCK`` behind the very turn it was meant to
    speed up.
    """

    registry = PersonaChatRuntimeRegistry()
    monkeypatch.setattr(
        prewarm_module,
        "_prepare",
        lambda root, instance: pytest.fail(
            "the prewarm assembled a request while a turn was running"
        ),
    )
    monkeypatch.setattr(
        "agent_runtime.persona_chat_continuity.persona_chat_runtime_registry",
        lambda: registry,
    )
    outcomes: list[str] = []

    class _Reentrant(_Agent):
        def run_conversation(self, *args, **kwargs):
            outcomes.append(prewarm_chat_actor("chat_root_other"))
            return super().run_conversation(*args, **kwargs)

    ProfileAgentRunner(agent_factory=_Reentrant).run(
        _request(prewarm_only=False, registry=None, root_chat_session_id=None)
    )

    assert outcomes == [OUTCOME_SKIPPED_TURN_ACTIVE]


# ── (5) inert without a registry ────────────────────────────────────────────


def test_every_hook_is_a_no_op_when_the_registry_is_off(monkeypatch):
    """``initialize_persona_chat_runtime_registry(enabled=False)`` leaves the
    registry ``None``, which is the state of every CLI one-shot. No thread, no
    queue entry, no work."""

    from agent_runtime.persona_chat_continuity import (
        initialize_persona_chat_runtime_registry,
    )

    initialize_persona_chat_runtime_registry(enabled=False)
    before = prewarm_module._worker

    assert request_chat_actor_prewarm("chat_root_1") == OUTCOME_REGISTRY_OFF
    assert prewarm_chat_actor("chat_root_1") == OUTCOME_REGISTRY_OFF
    assert prewarm_chat_actors_on_boot() == {
        "candidates": 0,
        "queued": 0,
        "skipped": 0,
    }
    assert prewarm_module._worker is before, "no worker thread was started"
    assert not prewarm_module._pending


def test_a_queued_root_is_not_queued_twice(monkeypatch):
    """The chat-open hook fires on every open gesture; the queue must not grow
    with the gestures."""

    registry = PersonaChatRuntimeRegistry()
    monkeypatch.setattr(
        "agent_runtime.persona_chat_continuity.persona_chat_runtime_registry",
        lambda: registry,
    )
    monkeypatch.setattr(prewarm_module, "_ensure_worker", lambda: None)

    assert request_chat_actor_prewarm("chat_root_1") == "started"
    assert request_chat_actor_prewarm("chat_root_1") == "already_running"
    assert request_chat_actor_prewarm("") == OUTCOME_SKIPPED_NO_CHAT_ROOT


# ── (6) the boot pass: cap, order, kill switch ──────────────────────────────


def _instances(*rows):
    return [_Instance(id=f"i{n}", updated_at=stamp) for n, stamp in enumerate(rows)]


def test_the_boot_pass_takes_the_most_recently_active_chats_up_to_the_cap(monkeypatch):
    """Warming more chats than the registry holds would evict the earliest warms
    before anyone used them, so the cap IS ``max_hot_sessions``; the order is the
    one eviction would preserve."""

    class _Store:
        def list_all(self):
            rows = []
            for stamp, root in (
                ("2026-08-23T09:00:00Z", "root_old"),
                ("2026-08-23T12:00:00Z", "root_new"),
                ("2026-08-23T11:00:00Z", "root_mid"),
            ):
                instance = _Instance(id=f"i_{root}", updated_at=stamp)
                instance.default_chat_session_id = root
                rows.append(instance)
            return rows

    monkeypatch.setattr(
        "agent_runtime.persona_assignments.PersonaInstanceStore", _Store
    )

    assert _boot_candidates(limit=2) == ["root_new", "root_mid"]
    assert _boot_candidates(limit=9) == ["root_new", "root_mid", "root_old"]


def test_an_instance_with_no_bound_chat_root_is_not_a_candidate(monkeypatch):
    """A placement with no ``default_chat_session_id`` has no root to key a
    resident actor on, and this module never mints one."""

    class _Store:
        def list_all(self):
            bound = _Instance(id="i_bound", updated_at="2026-08-23T09:00:00Z")
            bound.default_chat_session_id = "root_bound"
            unbound = _Instance(id="i_unbound", updated_at="2026-08-23T12:00:00Z")
            unbound.default_chat_session_id = None
            return [unbound, bound]

    monkeypatch.setattr(
        "agent_runtime.persona_assignments.PersonaInstanceStore", _Store
    )

    assert _boot_candidates(limit=8) == ["root_bound"]


def test_the_boot_pass_walks_no_roster_without_a_registry(monkeypatch):
    """``hot_sessions_enabled`` is the ONLY switch, so it has to gate the pass
    before any work — not merely refuse to store the result.

    Pinned as its own fact because this lane deliberately has no
    ``prewarm_on_boot`` key (see ``PersonaChatConfig``): if the registry check
    stops gating the pass, nothing else will.
    """

    from agent_runtime.persona_chat_continuity import (
        initialize_persona_chat_runtime_registry,
    )

    initialize_persona_chat_runtime_registry(enabled=False)
    monkeypatch.setattr(
        prewarm_module,
        "_boot_candidates",
        lambda **_kw: pytest.fail("the roster was walked with hot sessions off"),
    )

    assert prewarm_chat_actors_on_boot()["queued"] == 0


def test_the_persona_chat_config_carries_no_prewarm_knob():
    """A decision, pinned so it is re-taken rather than drifted into: every
    field of this dataclass is projected onto the read-model wire
    (``core.runtime_config.persona_chat.*``), so a new key is a cross-stack
    golden landing — regenerate the stream fixtures, mirror the bytes into the
    Launcher, update both manifests. Adding one here without that ceremony reds
    ``test_stream_contract_fixture``; adding one WITH it is a contract change
    that deserves its own landing.
    """

    from agent_runtime.runtime_config import PersonaChatConfig

    assert set(PersonaChatConfig.__dataclass_fields__) == {
        "hot_sessions_enabled",
        "max_hot_sessions",
        "idle_ttl_seconds",
    }


def test_the_boot_pass_honours_max_hot_sessions_as_the_cap(monkeypatch):
    """The cap the pass asks for is the registry's own, read from the same
    config stanza that sized the registry."""

    from agent_runtime.runtime_config import PersonaChatConfig, RuntimeConfig

    registry = PersonaChatRuntimeRegistry()
    seen: list[int] = []
    monkeypatch.setattr(
        "agent_runtime.persona_chat_continuity.persona_chat_runtime_registry",
        lambda: registry,
    )
    monkeypatch.setattr(
        "agent_runtime.config.load_root_runtime_config",
        lambda: RuntimeConfig(
            persona_chat=PersonaChatConfig(
                hot_sessions_enabled=True, max_hot_sessions=3
            )
        ),
    )

    def _candidates(*, limit):
        seen.append(limit)
        return []

    monkeypatch.setattr(prewarm_module, "_boot_candidates", _candidates)

    prewarm_chat_actors_on_boot()

    assert seen == [3]


# ── (7) the receipts ────────────────────────────────────────────────────────


def test_the_worker_logs_one_outcome_line_per_item(monkeypatch, caplog):
    """The per-entry receipt. A pass line with only totals cannot answer "which
    chat took four seconds", and the whole claim of this module is a race
    against the operator's first message."""

    monkeypatch.setattr(
        prewarm_module, "prewarm_chat_actor", lambda root: OUTCOME_WARMED
    )
    monkeypatch.setattr(prewarm_module, "_ensure_worker", lambda: None)
    registry = PersonaChatRuntimeRegistry()
    monkeypatch.setattr(
        "agent_runtime.persona_chat_continuity.persona_chat_runtime_registry",
        lambda: registry,
    )

    request_chat_actor_prewarm("chat_root_1")
    with caplog.at_level("INFO", logger=prewarm_module.__name__):
        worker = threading.Thread(target=prewarm_module._drain, daemon=True)
        worker.start()
        prewarm_module._queue.join()

    line = next(
        record.getMessage()
        for record in caplog.records
        if "persona_chat_actor_prewarm root=" in record.getMessage()
    )
    assert "root=chat_root_1" in line
    assert f"outcome={OUTCOME_WARMED}" in line
    assert "elapsed_ms=" in line


def test_the_receipt_formats_name_only_ids_timings_and_outcomes():
    """Format-pinned, and deliberately narrow: a receipt that grew a display
    name or a resolved toolset would put chat-lane content into the log."""

    assert prewarm_module.CHAT_ACTOR_PREWARM_DONE_RECEIPT == (
        "persona_chat_actor_prewarm root=%s outcome=%s elapsed_ms=%d"
    )
    assert prewarm_module.CHAT_ACTOR_PREWARM_PASS_RECEIPT == (
        "persona_chat_actor_prewarm pass candidates=%d queued=%d skipped=%d elapsed_ms=%d"
    )


# ── (8) the chat-open trigger, and the seam it deliberately avoids ──────────


def _prewarm_call_sites() -> set[str]:
    """Which functions in the persona command part queue a chat-actor prewarm.

    A CALL-SITE CENSUS rather than a source-shape assertion about one function:
    the property under test is genuinely "which seams fire this and which do
    not", and both halves of it are regressions with no other witness — a
    missing hook is a silently slow first turn, and a hook on the per-turn seam
    is a background construction racing every live turn for ``_WORKDIR_LOCK``.
    """

    import ast
    import pathlib

    import hermes_cli.harness_parts.persona_commands as commands

    tree = ast.parse(pathlib.Path(commands.__file__).read_text(encoding="utf-8"))
    sites: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "_prewarm_chat_actor_for_open"
            ):
                sites.add(node.name)
    return sites


def test_both_open_chat_arms_queue_a_prewarm_and_nothing_else_does():
    """The two operator gestures — open an existing chat, mint a new one — and
    only those. The new-chat arm matters most: a freshly minted root has no turn
    that is not its first."""

    assert _prewarm_call_sites() == {
        "_cmd_persona_instance_open_chat",
        "_cmd_persona_instance_open_new_chat",
    }


def test_the_send_path_seam_never_queues_a_prewarm():
    """``PersonaInstanceStore.open_chat`` is re-entered by EVERY mission-chat
    turn (``_cmd_mission_chat_message`` calls it before the lease). A prewarm
    hooked there would construct an agent in the background against every live
    turn — the exact contention the yield rule exists to avoid, arriving from
    the one place the yield rule cannot help, because the turn that would be
    displaced is the one that queued it."""

    import pathlib

    import agent_runtime.persona_assignments as assignments

    source = pathlib.Path(assignments.__file__).read_text(encoding="utf-8")
    assert "actor_prewarm" not in source
    assert "_cmd_mission_chat_message" not in _prewarm_call_sites()


def test_the_open_chat_helper_can_never_fail_an_open(monkeypatch):
    """Best effort by contract: an operator's chat must open even when the warm
    cannot be queued at all."""

    from hermes_cli.harness_parts.persona_commands import _prewarm_chat_actor_for_open

    seen: list[str] = []
    monkeypatch.setattr(
        prewarm_module, "request_chat_actor_prewarm", lambda root: seen.append(root)
    )
    _prewarm_chat_actor_for_open("chat_root_1")
    assert seen == ["chat_root_1"]

    def _boom(_root):
        raise RuntimeError("the queue is on fire")

    monkeypatch.setattr(prewarm_module, "request_chat_actor_prewarm", _boom)
    _prewarm_chat_actor_for_open("chat_root_1")  # must not raise


# ── (9) the serve boot ordering ─────────────────────────────────────────────


def test_the_actor_prewarm_runs_last_on_the_one_prewarm_thread():
    """Third, behind the read-model build the launcher's canvas waits on and
    behind the provider warmup whose SDK import every construction would
    otherwise pay itself.

    *Killing mutation:* reorder the step tuple. *Probed field:* the recorder's
    order, which the loop cannot reorder back.
    """

    from hermes_cli.harness_parts.serve import serve_loop

    order: list[str] = []
    done = threading.Event()

    def _actor():
        order.append("actor")
        done.set()

    serve_loop(
        iter(['{"id":"1","op":"shutdown"}']),
        io.StringIO(),
        dispatch=lambda argv: 0,
        snapshot_prewarm=lambda: order.append("snapshot"),
        provider_prewarm=lambda: order.append("provider"),
        actor_prewarm=_actor,
    )

    assert done.wait(10)
    assert order == ["snapshot", "provider", "actor"]


def test_the_loop_starts_the_thread_for_an_actor_prewarm_alone():
    """The step is injected on the same contract as the other two, so a caller
    that wants ONLY it must still get a thread."""

    from hermes_cli.harness_parts.serve import serve_loop

    fired = threading.Event()

    serve_loop(
        iter(['{"id":"1","op":"shutdown"}']),
        io.StringIO(),
        dispatch=lambda argv: 0,
        actor_prewarm=fired.set,
    )

    assert fired.wait(10)
