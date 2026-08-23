"""One chat-lane visibility resolve per turn, reused by IDENTITY — never by clock.

The defect these pin (live, 2026-08-23T14:34:57Z): a mission-chat turn asked
"what may this chat do?" four times and each asker walked
``permission_options_for_chat`` → ``all_registered_toolsets`` → the registry
``check_fn`` sweep on its own. The turn record read
``registry_probe_rounds=27`` inside a 1,313 ms context build, six minutes after a
serve boot, on a chat where nothing had changed since the previous message —
because the caches underneath expire on 15/30 s TTLs tuned for one snapshot
build, not for operator cadence.

``agent_runtime.chat_lane_bundle`` removes the RE-COMPOSITION. What is asserted
here is therefore in two halves, and the second matters more than the first:

* the bundle is built once and reused (the win), and
* it is rebuilt the instant any input it depends on moves (the safety), for each
  input in turn — the permission record, a consumed grant, the config files, the
  registry epoch — plus the rule that keeps a degraded answer from being pinned.
"""

from __future__ import annotations

import pytest

from agent_runtime import chat_lane_bundle as CLB
from agent_runtime.models import AgentPersona
from agent_runtime.permission_modes import (
    PERMISSION_MODE_BOUNDED,
    PERMISSION_MODE_PROFILE_DEFAULT,
    PERMISSION_MODE_UNBOUNDED,
)
from agent_runtime.tool_permissions import ChatToolPermissionStore


@pytest.fixture(autouse=True)
def _registry_populated():
    """Register the builtin tools BEFORE any bundle is built.

    ``model_tools`` is imported lazily (BW-H3) and its module scope is what
    REGISTERS every builtin tool into the singleton — which correctly moves
    ``registry_epoch``. In a live serve that happens at boot, long before any
    turn. In a test process the first bundle build would trigger it and thereby
    invalidate its own key, so these tests would be asserting a cold-import
    artifact instead of the steady state they are about.
    """

    from agent_runtime.tool_visibility import _ensure_tool_registry_populated

    _ensure_tool_registry_populated()
    yield


def _warm_the_lane(persona: AgentPersona) -> None:
    """Absorb the one-time cold work the FIRST chat-lane touch of a process does.

    That first touch legitimately moves the key: the lazy ``model_tools`` import
    registers every builtin tool (which is a registration, so the epoch moves),
    and the profile-readiness read materializes parts of a hermetic home that
    did not exist yet. A live serve pays all of that at boot, minutes before any
    operator turn. Warming on a THROWAWAY chat root keeps the assertions below
    about the steady state an operator's turns actually run in, without hiding a
    key that is unstable in general — an unstable key would still move across
    the measured pair.
    """

    CLB.chat_lane_bundle(persona, session_id="chat-warmup")


def _persona(persona_id: str = "dev") -> AgentPersona:
    return AgentPersona(
        id=persona_id,
        display_name="Launcher Dev",
        role="dev",
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=["file", "search", "terminal", "skills"],
        system_prompt_path="personas/dev/system.md",
    )


# ── (1) the win: one resolve, reused ────────────────────────────────────────


def test_the_same_identity_is_resolved_once_and_served_the_same_object():
    """Two turns of an unchanged chat pay ONE resolve between them.

    Object identity, not equality: the whole point is that the second turn does
    not re-walk the resolution, and an equal-but-rebuilt bundle would satisfy an
    equality assertion while paying the exact cost this exists to remove.
    """

    from agent_runtime.tool_permissions import permission_options_for_chat

    persona = _persona()
    _warm_the_lane(persona)
    before = CLB.bundle_builds_this_thread()
    first = CLB.chat_lane_bundle(persona, session_id="chat-reuse")
    second = CLB.chat_lane_bundle(persona, session_id="chat-reuse")
    if second is not first:
        # A digest is unreadable evidence. Name the component that moved.
        material = CLB.chat_lane_bundle_key_material(
            persona,
            permission_options_for_chat(persona, session_id="chat-reuse"),
            session_id="chat-reuse",
        )
        pytest.fail(
            "the bundle key moved between two identical lookups; "
            f"key material now = {material!r}"
        )
    assert CLB.bundle_builds_this_thread() - before == 1


def test_the_whole_turn_path_walks_the_toolset_chokepoint_exactly_once(monkeypatch):
    """The receipt the plan asks for, driven through the real consumers.

    Every one of these is a call the turn makes today: the skill preload's
    operating manuals, the runtime signature's tool contract and permission
    state, the volatile tail's capability account and admission line, and the
    request assembly in ``mission_chat_reply``. Before the bundle they walked
    ``_enabled_toolsets_for_chat`` (and with it the registry sweep) between three
    and four times per turn.
    """

    from agent_runtime import persona_runtime
    from agent_runtime.mission_chat_turn_context import DEFAULT_RESOLVERS

    persona = _persona()
    _warm_the_lane(persona)

    calls: list[int] = []
    real = persona_runtime._enabled_toolsets_for_chat

    def _counted(persona, **kwargs):
        calls.append(1)
        return real(persona, **kwargs)

    monkeypatch.setattr(persona_runtime, "_enabled_toolsets_for_chat", _counted)
    builds_before = CLB.bundle_builds_this_thread()
    DEFAULT_RESOLVERS.admitted_operating_skills(persona, session_id="chat-once")
    DEFAULT_RESOLVERS.tool_contract(persona, session_id="chat-once")
    DEFAULT_RESOLVERS.permission_state(persona, session_id="chat-once")
    DEFAULT_RESOLVERS.capability_block(persona, session_id="chat-once")
    DEFAULT_RESOLVERS.admission_line(persona, session_id="chat-once")
    CLB.chat_lane_bundle(persona, session_id="chat-once")
    assert calls == [1], (
        "the chat-lane toolset chokepoint ran more than once for one turn"
    )
    assert CLB.bundle_builds_this_thread() - builds_before == 1


def test_a_consumer_that_decorates_its_copy_cannot_write_into_the_cache():
    """The accessors COPY. The capability account is folded into the situational
    HUD and recorded verbatim on the observability row; if consumers shared the
    stored dict, the first decorator would become a silent writer into every
    later turn's answer."""

    persona = _persona()
    first = CLB.chat_lane_bundle(persona, session_id="chat-copy")
    block = first.capability()
    block["injected"] = True
    envelope = block.get("envelope")
    if isinstance(envelope, dict):
        envelope["granted"] = ["forged"]
    contract = first.tool_contract()
    contract["enabled_toolsets"].append("forged_toolset")

    again = CLB.chat_lane_bundle(persona, session_id="chat-copy")
    assert again is first
    assert "injected" not in again.capability()
    assert "forged_toolset" not in again.tool_contract()["enabled_toolsets"]


# ── (2) the safety: every keyed input rebuilds ──────────────────────────────


def test_an_unbounded_bundle_is_never_served_to_a_bounded_turn(bounded_chat_session):
    """``unbounded`` resolves ``all_registered_toolsets()``. Serving that to a
    bounded turn would hand a restricted chat the whole registry — the single
    worst thing this cache could do, so the mode is in the key."""

    persona = _persona()
    wide = CLB.chat_lane_bundle(persona, session_id="chat-perm")
    assert wide.permission_mode == PERMISSION_MODE_UNBOUNDED

    bounded_chat_session(persona.id, "chat-perm")
    narrow = CLB.chat_lane_bundle(persona, session_id="chat-perm")

    assert narrow is not wide
    assert narrow.key != wide.key
    # ``bounded`` is the STORED spelling of the restriction;
    # ``effective_permission_mode`` collapses it onto the enforced
    # ``profile_default`` tier (see ``agent_runtime.permission_modes``).
    assert narrow.permission_mode == PERMISSION_MODE_PROFILE_DEFAULT
    # The chat-lane cost policy applies only on the bounded tier, so this is the
    # observable proof the narrow answer is not the wide one re-served.
    assert "terminal" not in narrow.enabled_toolsets
    assert "terminal" in wide.enabled_toolsets


def test_consuming_a_turns_bounded_grant_rebuilds_the_bundle():
    """``ChatToolPermissionStore.consume_turn`` runs AFTER the run, so the
    decrement lands between turns. The next turn must see the new fingerprint —
    a granted-then-consumed permission served from cache would keep a lapsed
    posture alive for the life of the process."""

    persona = _persona()
    store = ChatToolPermissionStore()
    store.set(
        persona_id=persona.id,
        session_id="chat-consume",
        mode=PERMISSION_MODE_BOUNDED,
        reason="test pins a turns-bounded restriction",
        turns_remaining=2,
    )
    first = CLB.chat_lane_bundle(persona, session_id="chat-consume")
    store.consume_turn(persona_id=persona.id, session_id="chat-consume")
    second = CLB.chat_lane_bundle(persona, session_id="chat-consume")

    assert second is not first
    assert second.key != first.key


def test_a_root_config_edit_rebuilds_the_bundle():
    """The root ``config.yaml`` owns the permission default, the MCP admission
    kill switch and ``chat_lane_restore_toolsets`` — an operator edit must not
    wait for a restart."""

    from agent_runtime.config import harness_root_config_path

    persona = _persona()
    path = harness_root_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("agent_runtime: {}\n", encoding="utf-8")
    first = CLB.chat_lane_bundle(persona, session_id="chat-config")

    path.write_text(
        "agent_runtime:\n"
        "  personas:\n"
        "    dev:\n"
        "      chat_lane_restore_toolsets: [terminal]\n",
        encoding="utf-8",
    )
    second = CLB.chat_lane_bundle(persona, session_id="chat-config")

    assert second is not first
    assert second.key != first.key


def test_a_moved_registry_epoch_rebuilds_the_bundle(monkeypatch):
    """The epoch is what replaces the 30 s TTL as the availability signal."""

    import tools.registry as registry_module

    persona = _persona()
    _warm_the_lane(persona)
    epoch = {"value": 5}
    monkeypatch.setattr(registry_module, "registry_epoch", lambda: epoch["value"])
    first = CLB.chat_lane_bundle(persona, session_id="chat-epoch")
    assert CLB.chat_lane_bundle(persona, session_id="chat-epoch") is first

    epoch["value"] = 6
    assert CLB.chat_lane_bundle(persona, session_id="chat-epoch") is not first


def test_an_availability_invalidation_announces_itself_through_the_epoch():
    """``invalidate_check_fn_cache`` is what ``hermes tools enable`` and the
    credential paths call. Before this stage it only dropped the probe cache;
    a memo built out of those probes had no way to learn about it."""

    from tools.registry import (
        ToolRegistry,
        invalidate_check_fn_cache,
        registry_epoch,
    )

    before = registry_epoch()
    invalidate_check_fn_cache()
    assert registry_epoch() != before

    # ...and the REGISTRATION half moves independently of it. Asserted on a
    # throwaway registry so the process-wide singleton every other test shares
    # is not polluted by this one.
    scratch = ToolRegistry()
    generation = scratch.generation
    scratch.register_toolset_alias("bundle-test-alias", "bundle-test-toolset")
    assert scratch.generation != generation


def test_the_explicit_invalidation_hatch_drops_every_bundle():
    """Real API, not a test hook: the escape valve for a change neither the key
    nor the registry epoch can see."""

    persona = _persona()
    first = CLB.chat_lane_bundle(persona, session_id="chat-hatch")
    CLB.invalidate_chat_lane_bundles()
    assert CLB.chat_lane_bundle(persona, session_id="chat-hatch") is not first


def test_a_degraded_bundle_is_served_to_this_turn_and_then_thrown_away(monkeypatch):
    """A best-effort component that faults must degrade THIS turn exactly as the
    uncached path degrades it — and must not be pinned. Caching a fault is how
    one transient failure becomes a permanent wrong answer that no input change
    can clear."""

    from agent_runtime import runtime_hud

    faults: list[int] = []

    def _boom(*args, **kwargs):
        faults.append(1)
        raise RuntimeError("capability account unavailable")

    monkeypatch.setattr(runtime_hud, "capability_block_for_persona", _boom)
    persona = _persona()
    first = CLB.chat_lane_bundle(persona, session_id="chat-fault")
    assert first.complete is False
    assert first.capability() == {}

    second = CLB.chat_lane_bundle(persona, session_id="chat-fault")
    assert second is not first, "a degraded bundle was pinned in the cache"
    assert len(faults) == 2


def test_a_hard_component_fault_still_fails_the_turn(monkeypatch):
    """The components that decide what the turn SHIPS propagate exactly as they
    did before the bundle existed. Swallowing one here would hand the runner a
    capability answer nobody resolved."""

    from agent_runtime import tool_permissions

    def _boom(*args, **kwargs):
        raise RuntimeError("permission state unavailable")

    monkeypatch.setattr(tool_permissions, "permission_state_for_chat", _boom)
    with pytest.raises(RuntimeError):
        CLB.chat_lane_bundle(_persona(), session_id="chat-hard-fault")


# ── (3) keying scope, stated ────────────────────────────────────────────────


def test_two_chat_roots_of_one_persona_do_not_share_a_bundle():
    """Session id is in the key AND in the memo slot: the permission record is
    per (persona, chat), so one root's answer is never another root's."""

    persona = _persona()
    first = CLB.chat_lane_bundle(persona, session_id="chat-root-a")
    second = CLB.chat_lane_bundle(persona, session_id="chat-root-b")
    assert second is not first
    assert second.key != first.key


def test_a_persona_edit_rebuilds_the_bundle():
    """The persona revision covers its declared toolsets, skills, profile and
    MCP declaration — every persona field the resolution reads."""

    persona = _persona()
    first = CLB.chat_lane_bundle(persona, session_id="chat-persona")
    edited = _persona()
    edited.toolsets = ["search"]
    second = CLB.chat_lane_bundle(edited, session_id="chat-persona")
    assert second is not first
    assert second.key != first.key
