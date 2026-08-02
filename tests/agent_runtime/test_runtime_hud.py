from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime.runtime_hud import (
    SITUATIONAL_HUD_ROSTER_CAP,
    render_situational_hud_block,
    resolve_situational_hud,
)


def _instance(**overrides):
    base = dict(
        id="personainst_neko",
        persona_id="neko_supervisor",
        role="supervisor",
        display_name="Neko Mission Lead",
        goal_id=None,
        current_task_id=None,
        state="idle",
        mode="configured",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_resolve_situational_hud_standing_by_lane_has_runtime_scope_lane_no_mission():
    instance = _instance()
    roster = [instance, _instance(id="personainst_qa", display_name="QA Agent")]
    hud = resolve_situational_hud(
        instance,
        realm="default",
        workspace="default",
        roster=roster,
    )
    assert hud["preview"] is True
    assert hud["scope"] == {"realm": "default", "workspace": "default"}
    assert hud["lane"]["persona_instance_id"] == "personainst_neko"
    assert hud["lane"]["display_name"] == "Neko Mission Lead"
    # Standing by: no bound task → no mission block and no mission_hud sub-part.
    assert "mission" not in hud
    assert "mission_hud" not in hud
    # Roster carries every lane; the self entry is flagged.
    ids = [entry["persona_instance_id"] for entry in hud["roster"]]
    assert ids == ["personainst_neko", "personainst_qa"]
    assert hud["roster"][0]["is_self"] is True


def test_resolve_situational_hud_bound_lane_carries_goal_and_thread_count():
    instance = _instance(goal_id="goal_1", current_task_id="task_stage")
    peer = _instance(id="personainst_dev", display_name="Dev", goal_id="goal_1")
    other = _instance(id="personainst_x", display_name="X", goal_id="goal_other")
    hud = resolve_situational_hud(
        instance,
        daemon={"state": "running", "loops": 3},
        realm="default",
        workspace="default",
        roster=[instance, peer, other],
    )
    assert hud["mission"]["goal_id"] == "goal_1"
    assert "title" not in hud["mission"]
    assert "state" not in hud["mission"]
    # Two lanes share goal_1 (self + peer); the goal_other lane is excluded.
    assert hud["mission"]["thread_count"] == 2


def test_resolve_situational_hud_no_longer_carries_a_stage_qa_gate_slice():
    """Retargeted in S19 (was ``..._reuses_mission_hud_preview``).

    The slice reused retired task-store projections. The current HUD reads only
    the instance's persisted goal id and addressable roster, so a current-task
    pointer alone cannot manufacture mission context.
    """

    hud = resolve_situational_hud(_instance(current_task_id="task_stage"))
    assert "mission_hud" not in hud
    assert "mission" not in hud


def test_resolve_situational_hud_missing_daemon_and_scope_degrade_cleanly():
    hud = resolve_situational_hud(_instance(), daemon=None, realm=None, workspace=None, roster=[])
    assert "runtime" not in hud
    assert "scope" not in hud
    assert hud["lane"]["display_name"] == "Neko Mission Lead"


def test_resolve_situational_hud_none_instance_is_empty():
    assert resolve_situational_hud(None) == {}


def test_roster_is_capped():
    instance = _instance()
    big_roster = [_instance(id=f"personainst_{n}", display_name=f"A{n}") for n in range(SITUATIONAL_HUD_ROSTER_CAP + 5)]
    hud = resolve_situational_hud(instance, roster=big_roster)
    assert len(hud["roster"]) == SITUATIONAL_HUD_ROSTER_CAP


def test_render_situational_hud_block_is_readonly_and_mirrors_widget_lines():
    instance = _instance(goal_id="goal_1", current_task_id="task_stage", role="supervisor")
    hud = resolve_situational_hud(
        instance,
        realm="default",
        workspace="default",
        roster=[instance],
    )
    block = render_situational_hud_block(hud)
    assert block.startswith("## Runtime Situation")
    assert "read-only" in block
    assert "- Scope: realm default · workspace default" in block
    assert "goal_1" in block
    assert "@personainst_neko" in block
    assert "- On level (1):" in block


def test_roster_line_carries_addressable_handles():
    # The handle IS the address the agent-chat/steer verbs accept: every roster
    # teammate renders as "Name (@personainst_...)" so a name is never visible
    # but unaddressable. (Steering lines already did this; the roster lagged.)
    me = _instance()
    teammate = _instance(id="personainst_qa", display_name="QA Agent")
    hud = resolve_situational_hud(me, roster=[me, teammate])
    block = render_situational_hud_block(hud)
    assert "QA Agent (@personainst_qa)" in block


def test_render_situational_hud_block_says_no_mission_when_unbound():
    hud = resolve_situational_hud(
        _instance(),
        daemon={"state": "offline", "loops": 0},
        realm="default",
        workspace="default",
        roster=[_instance()],
    )
    block = render_situational_hud_block(hud)
    assert "- Mission: no mission bound to this lane" in block


def test_render_situational_hud_block_empty_for_empty_hud():
    assert render_situational_hud_block({}) == ""


def test_resolve_steering_fan_in_resolves_parents_in_edge_order():
    lead = _instance(id="personainst_neko", display_name="Neko Mission Lead")
    reviewer = _instance(id="personainst_rev", display_name="Reviewer")
    child = _instance(
        id="personainst_dev",
        display_name="Dev",
        steered_by=["personainst_neko", "personainst_rev"],
    )
    hud = resolve_situational_hud(child, roster=[lead, reviewer, child])
    steering = hud["steering"]
    assert [entry["persona_instance_id"] for entry in steering["steered_by"]] == [
        "personainst_neko",
        "personainst_rev",
    ]
    assert steering["steered_by"][0]["display_name"] == "Neko Mission Lead"
    assert steering["steers"] == []


def test_resolve_steering_falls_back_to_spawned_by_for_unmigrated_records():
    lead = _instance(id="personainst_neko", display_name="Neko Mission Lead")
    child = _instance(
        id="personainst_dev", display_name="Dev", steered_by=[], spawned_by="personainst_neko"
    )
    hud = resolve_situational_hud(child, roster=[lead, child])
    assert [entry["persona_instance_id"] for entry in hud["steering"]["steered_by"]] == [
        "personainst_neko"
    ]


def test_resolve_steering_ref_may_name_persona_id_and_unresolved_ref_keeps_raw():
    lead = _instance(id="personainst_neko", persona_id="neko_supervisor")
    child = _instance(
        id="personainst_dev",
        display_name="Dev",
        # One ref by persona id (resolvable), one naming a departed lane.
        steered_by=["neko_supervisor", "personainst_gone"],
    )
    hud = resolve_situational_hud(child, roster=[lead, child])
    steered_by = hud["steering"]["steered_by"]
    assert steered_by[0]["persona_instance_id"] == "personainst_neko"
    # A departed steerer is a fact, not "no steerer": the raw ref survives.
    assert steered_by[1] == {"ref": "personainst_gone"}


def test_resolve_steering_derives_steers_by_roster_inversion_once_per_child():
    lead = _instance(id="personainst_neko", persona_id="neko_supervisor", display_name="Neko Mission Lead")
    # Child names the lead twice (instance id + persona id): must appear once.
    child = _instance(
        id="personainst_dev",
        display_name="Dev",
        steered_by=["personainst_neko", "neko_supervisor"],
    )
    bystander = _instance(id="personainst_x", display_name="X")
    hud = resolve_situational_hud(lead, roster=[lead, child, bystander])
    steers = hud["steering"]["steers"]
    assert steers == [{"persona_instance_id": "personainst_dev", "display_name": "Dev"}]


def test_resolve_steering_standalone_is_explicit_empty_not_absent():
    hud = resolve_situational_hud(_instance(), roster=[_instance()])
    assert hud["steering"] == {"steered_by": [], "steers": []}


def test_render_steering_lines_for_links_and_standalone():
    lead = _instance(id="personainst_neko", display_name="Neko Mission Lead")
    child = _instance(id="personainst_dev", display_name="Dev", steered_by=["personainst_neko"])
    linked = render_situational_hud_block(
        resolve_situational_hud(child, roster=[lead, child])
    )
    assert "- Steered by: Neko Mission Lead (@personainst_neko)" in linked

    lead_block = render_situational_hud_block(
        resolve_situational_hud(lead, roster=[lead, child])
    )
    assert "- Steers: Dev (@personainst_dev)" in lead_block

    standalone = render_situational_hud_block(
        resolve_situational_hud(_instance(), roster=[_instance()])
    )
    assert "- Steering: standalone — no steerer, steers nobody" in standalone


def _chat_persona():
    from agent_runtime.models import AgentPersona

    return AgentPersona(
        id="neko_supervisor",
        display_name="Neko Mission Lead",
        role="profile",
        model="gpt-5.5",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file"],
        system_prompt_path="",
        hermes_profile=None,
    )


def test_mission_chat_surface_message_never_carries_runtime_situation_block():
    # T5: the volatile Runtime Situation HUD is NO LONGER injected into the
    # system prompt (the codex ``instructions``). It rides the operator's user
    # turn instead so the cross-turn prompt-cache prefix stays byte-stable. The
    # surface message therefore never contains the HUD, and the builder no
    # longer even accepts a situational_hud argument.
    import inspect

    from agent_runtime.persona_runtime import _mission_chat_surface_message

    message = _mission_chat_surface_message(_chat_persona(), "")
    assert "## Runtime Situation" not in message
    # The situational_hud_content parameter is gone from the system-prompt builder.
    assert "situational_hud_content" not in inspect.signature(
        _mission_chat_surface_message
    ).parameters


def test_mission_chat_system_prompt_is_byte_stable_across_hud_and_roster_state():
    # THE T5 byte-stability proof: two simulated turns whose HUD/roster/scope
    # differ (e.g. `QA Agent` ↔ `QA Agent (2)`, a new mission, a changed realm)
    # must produce a BYTE-IDENTICAL system prompt, so the codex
    # prompt_cache_key = sha256(instructions + tools) does not rotate turn to
    # turn. The HUD content that used to poison the prefix is exercised through
    # the real renderer to make the guard honest.
    from agent_runtime.persona_runtime import (
        _mission_chat_surface_message,
        _mission_chat_user_message,
    )

    persona = _chat_persona()

    hud_turn_1 = render_situational_hud_block(
        {
            "preview": True,
            "scope": {"realm": "default", "workspace": "alpha"},
            "roster": [{"display_name": "QA Agent", "persona_instance_id": "personainst_qa"}],
        }
    )
    hud_turn_2 = render_situational_hud_block(
        {
            "preview": True,
            "scope": {"realm": "staging", "workspace": "beta"},
            "mission": {"goal_id": "goal_x", "title": "Ship it", "state": "running"},
            "roster": [
                {"display_name": "QA Agent (2)", "persona_instance_id": "personainst_qa2"},
                {"display_name": "Dev", "persona_instance_id": "personainst_dev"},
            ],
        }
    )
    assert hud_turn_1 != hud_turn_2  # the two turns really do differ

    system_1 = _mission_chat_surface_message(persona, "")
    system_2 = _mission_chat_surface_message(persona, "")
    assert system_1 == system_2  # byte-identical instructions across turns

    # And the diverging state lives entirely in the (per-turn, uncached) user
    # turn, never the system prompt.
    assert hud_turn_1 not in system_1
    assert hud_turn_2 not in system_2
    user_1 = _mission_chat_user_message("hi", hud_turn_1)
    user_2 = _mission_chat_user_message("hi", hud_turn_2)
    assert user_1 != user_2
    assert hud_turn_1 in user_1
    assert hud_turn_2 in user_2


def test_mission_chat_user_message_rides_hud_after_the_operator_message():
    # Placement is load-bearing: the HUD trails the operator's message (which
    # already carries the rolling chat history), so it sits AFTER chat history,
    # adjacent to the current operator message — the append-only ordering the
    # caching design requires.
    from agent_runtime.persona_runtime import _mission_chat_user_message

    baked = (
        "Prior persona chat context (oldest to newest):\n"
        "Operator: earlier\nAgent: ok\n\nCurrent operator message:\nwhat now?"
    )
    hud = render_situational_hud_block(
        {"preview": True, "scope": {"realm": "default", "workspace": "alpha"}}
    )
    user = _mission_chat_user_message(baked, hud)
    assert baked in user
    assert hud in user
    # HUD strictly after the whole operator/history block.
    assert user.index(hud) > user.index("Current operator message:")
    assert user.index(hud) > user.index("what now?")


def test_mission_chat_user_message_is_bare_message_without_hud():
    # No HUD resolved (best-effort {}/'' from situational_hud_for_instance) -> the
    # operator turn is exactly the message, no dangling separators.
    from agent_runtime.persona_runtime import _mission_chat_user_message

    assert _mission_chat_user_message("just this", "") == "just this"
    assert _mission_chat_user_message("just this", None) == "just this"
    assert _mission_chat_user_message("just this", "   ") == "just this"


def test_runtime_context_revision_is_canonical_and_changes_with_snapshot():
    from agent_runtime.runtime_hud import situational_hud_revision

    left = {"scope": {"workspace": "alpha", "realm": "default"}, "preview": True}
    reordered = {"preview": True, "scope": {"realm": "default", "workspace": "alpha"}}
    changed = {"preview": True, "scope": {"realm": "default", "workspace": "beta"}}

    assert situational_hud_revision(left) == situational_hud_revision(reordered)
    assert situational_hud_revision(left) != situational_hud_revision(changed)
    assert situational_hud_revision({}) == "hud_unavailable"


def test_runtime_context_delivery_sends_snapshot_then_unchanged_and_recovers():
    from agent_runtime.runtime_hud import (
        render_runtime_context_envelope,
        runtime_context_delivery,
    )

    revision = "hud_0123456789abcdef"
    assert runtime_context_delivery([], revision) == "snapshot"
    snapshot = render_runtime_context_envelope(
        context_id="ctx_first",
        revision=revision,
        delivery="snapshot",
        situational_hud_content="## Runtime Situation\n- Scope: alpha",
    )
    history = [{"role": "user", "content": f"hello\n\n{snapshot}"}]
    assert runtime_context_delivery(history, revision) == "unchanged"
    assert runtime_context_delivery(history, "hud_fedcba9876543210") == "snapshot"
    # If compression no longer retains the matching full snapshot, re-anchor.
    assert runtime_context_delivery([{"role": "assistant", "content": "summary"}], revision) == "snapshot"


def test_runtime_context_envelope_is_compact_and_strips_only_at_final_boundary():
    from agent_runtime.runtime_hud import (
        extract_runtime_context_envelope,
        render_runtime_context_envelope,
    )

    envelope = render_runtime_context_envelope(
        context_id="ctx_second",
        revision="hud_0123456789abcdef",
        delivery="unchanged",
        situational_hud_content=None,
    )
    clean, metadata = extract_runtime_context_envelope(f"what now?\n\n{envelope}")
    assert clean == "what now?"
    assert metadata == {
        "context_id": "ctx_second",
        "revision": "hud_0123456789abcdef",
        "delivery": "unchanged",
    }
    assert "## Runtime Situation" not in envelope

    authored = f"please discuss {envelope}\nthen continue"
    assert extract_runtime_context_envelope(authored) == (authored, None)


def test_skill_preload_envelope_round_trips_and_strips_only_at_final_boundary():
    from agent_runtime.runtime_hud import (
        extract_skill_preload_envelope,
        render_skill_preload_envelope,
        skill_preload_revision,
    )

    body = '[IMPORTANT: Runtime policy requires the "harness-runtime-model" skill]\n# Harness Runtime Model\nbody'
    envelope = render_skill_preload_envelope(
        skill_names=["harness-runtime-model"],
        skill_preload_content=body,
    )
    clean, metadata = extract_skill_preload_envelope(f"message launcher dev say hi\n\n{envelope}")
    assert clean == "message launcher dev say hi"
    assert metadata == {
        "skills": ["harness-runtime-model"],
        "revision": skill_preload_revision(body),
        "delivery": "snapshot",
    }

    # Nothing to preload → empty string, so composition joins stay unchanged.
    assert render_skill_preload_envelope(skill_names=["x"], skill_preload_content="  ") == ""

    # Operator-authored text that mentions the tag mid-message stays content.
    authored = f"please discuss {envelope}\nthen continue"
    assert extract_skill_preload_envelope(authored) == (authored, None)

    # Names failing the strict attribute charset are dropped from the attribute
    # without breaking the envelope.
    quoted = render_skill_preload_envelope(
        skill_names=['bad"name', "deep-audit"],
        skill_preload_content="skill body",
    )
    _, meta = extract_skill_preload_envelope(f"hi\n\n{quoted}")
    assert meta["skills"] == ["deep-audit"]

    # Rows persisted by the envelope's first revision carry no
    # revision/delivery attributes — they must keep stripping.
    legacy = '<skill_preload skills="deep-audit">\nskill body\n</skill_preload>'
    legacy_clean, legacy_meta = extract_skill_preload_envelope(f"hi\n\n{legacy}")
    assert legacy_clean == "hi"
    assert legacy_meta == {"skills": ["deep-audit"]}


def test_skill_preload_envelope_extracts_between_message_and_runtime_context():
    # Composition order is message · skill_preload · runtime_context. The
    # projection strips the HUD envelope first, then the skill envelope is
    # end-anchored on the remainder — the operator sees only their message.
    from agent_runtime.runtime_hud import (
        extract_runtime_context_envelope,
        extract_skill_preload_envelope,
        render_runtime_context_envelope,
        render_skill_preload_envelope,
    )

    skill_envelope = render_skill_preload_envelope(
        skill_names=["harness-runtime-model"],
        skill_preload_content="# Harness Runtime Model\nfull skill body",
    )
    hud_envelope = render_runtime_context_envelope(
        context_id="ctx_compose",
        revision="hud_0123456789abcdef",
        delivery="snapshot",
        situational_hud_content="## Runtime Situation\n- Scope: realm default",
    )
    composed = "\n\n".join(["message launcher dev say hi", skill_envelope, hud_envelope])

    remainder, runtime_context = extract_runtime_context_envelope(composed)
    assert runtime_context is not None
    clean, skill_preload = extract_skill_preload_envelope(remainder)
    assert clean == "message launcher dev say hi"
    assert skill_preload["skills"] == ["harness-runtime-model"]


def test_skill_preload_delivery_sends_snapshot_then_unchanged_and_recovers():
    # Mirror of the runtime-context delivery contract: the full body rides only
    # when no matching snapshot survives in the effective native lineage.
    from agent_runtime.runtime_hud import (
        render_runtime_context_envelope,
        render_skill_preload_envelope,
        skill_preload_delivery,
        skill_preload_revision,
    )

    body = "# Harness Runtime Model\nfull skill body"
    revision = skill_preload_revision(body)
    assert skill_preload_delivery([], revision) == "snapshot"

    snapshot = render_skill_preload_envelope(
        skill_names=["harness-runtime-model"],
        skill_preload_content=body,
        revision=revision,
        delivery="snapshot",
    )
    hud = render_runtime_context_envelope(
        context_id="ctx_dedup",
        revision="hud_0123456789abcdef",
        delivery="snapshot",
        situational_hud_content="## Runtime Situation\n- Scope: realm default",
    )
    # History rows carry the skill envelope BEFORE the trailing HUD envelope.
    history = [{"role": "user", "content": "\n\n".join(["hello", snapshot, hud])}]
    assert skill_preload_delivery(history, revision) == "unchanged"
    # Changed preload content (skill edited / different set) → re-snapshot.
    assert skill_preload_delivery(history, skill_preload_revision("other")) == "snapshot"
    # Compression dropped the snapshot row → re-anchor with a full snapshot.
    assert skill_preload_delivery([{"role": "assistant", "content": "summary"}], revision) == "snapshot"
    # Legacy rows (no revision attribute) cannot vouch for content → snapshot.
    legacy_row = {"role": "user", "content": 'hi\n\n<skill_preload skills="a">\nbody\n</skill_preload>'}
    assert skill_preload_delivery([legacy_row], revision) == "snapshot"
    # An "unchanged" stub row is not a snapshot anchor either.
    stub = render_skill_preload_envelope(
        skill_names=["harness-runtime-model"],
        skill_preload_content=body,
        revision=revision,
        delivery="unchanged",
    )
    assert skill_preload_delivery([{"role": "user", "content": f"hi\n\n{stub}"}], revision) == "snapshot"


def test_skill_preload_unchanged_stub_is_compact_and_projection_safe():
    from agent_runtime.runtime_hud import (
        extract_skill_preload_envelope,
        render_skill_preload_envelope,
        skill_preload_revision,
    )

    body = "# Harness Runtime Model\n" + ("skill body line\n" * 200)
    revision = skill_preload_revision(body)
    stub = render_skill_preload_envelope(
        skill_names=["harness-runtime-model"],
        skill_preload_content=body,
        revision=revision,
        delivery="unchanged",
    )
    # The stub re-asserts activation without carrying the body.
    assert len(stub) < 400
    assert "skill body line" not in stub
    assert revision in stub
    clean, metadata = extract_skill_preload_envelope(f"next question\n\n{stub}")
    assert clean == "next question"
    assert metadata == {
        "skills": ["harness-runtime-model"],
        "revision": revision,
        "delivery": "unchanged",
    }


# --------------------------------------------------------------------------- #
# Workspace-scoped addressable roster (D1)                                     #
#                                                                              #
# The addressable "On level" roster and mission thread count are the SCOPED    #
# list (the sender's workspace + runtime-global rows); identity resolution     #
# (steering names) reads the FULL, unscoped list. These compose the two pure   #
# pieces the way the chat wrapper / snapshot do: scope_roster for advertising, #
# the full roster for identity_roster.                                         #
# --------------------------------------------------------------------------- #


def test_situational_hud_roster_scoped_to_target_workspace():
    from agent_runtime import workspace_scope

    target = _instance(id="personainst_dev", persona_id="dev", display_name="Dev", workspace_id="ws_a")
    same_ws = _instance(id="personainst_qa", persona_id="qa", display_name="QA", workspace_id="ws_a")
    other_ws = _instance(id="personainst_qa_b", persona_id="qa", display_name="QA (2)", workspace_id="ws_b")
    global_row = _instance(id="personainst_neko", display_name="Neko Mission Lead", workspace_id=None)
    full = [target, same_ws, other_ws, global_row]

    scope_ws = workspace_scope.effective_workspace_id(target, active_workspace_id="ws_a")
    scoped = workspace_scope.scope_roster(full, scope_workspace_id=scope_ws)
    hud = resolve_situational_hud(target, roster=scoped, identity_roster=full)

    ids = [entry["persona_instance_id"] for entry in hud["roster"]]
    # ws_a rows + the runtime-global row; the ws_b placement is NOT advertised.
    assert ids == ["personainst_dev", "personainst_qa", "personainst_neko"]
    assert "personainst_qa_b" not in ids


def test_situational_hud_global_target_scopes_to_active_workspace():
    from agent_runtime import workspace_scope

    # A runtime-global operator row (no pointer) viewed from active workspace
    # ws_a: its effective scope is ws_a, so it sees ws_a rows + global rows only.
    target = _instance(id="personainst_neko", display_name="Neko Mission Lead", workspace_id=None)
    ws_a_row = _instance(id="personainst_dev", persona_id="dev", display_name="Dev", workspace_id="ws_a")
    ws_b_row = _instance(id="personainst_qa", persona_id="qa", display_name="QA", workspace_id="ws_b")
    global_row = _instance(id="personainst_ops", display_name="Ops", workspace_id=None)
    full = [target, ws_a_row, ws_b_row, global_row]

    scope_ws = workspace_scope.effective_workspace_id(target, active_workspace_id="ws_a")
    assert scope_ws == "ws_a"
    scoped = workspace_scope.scope_roster(full, scope_workspace_id=scope_ws)
    hud = resolve_situational_hud(target, roster=scoped, identity_roster=full)

    ids = [entry["persona_instance_id"] for entry in hud["roster"]]
    assert ids == ["personainst_neko", "personainst_dev", "personainst_ops"]
    assert "personainst_qa" not in ids


def test_situational_hud_steering_name_resolves_when_steerer_out_of_scope():
    from agent_runtime import workspace_scope

    # The steerer (Neko) is in ws_a; the child is in ws_b. From the child's
    # scope Neko is NOT addressable, but its steering NAME must still resolve
    # because identity reads the full, unscoped roster.
    steerer = _instance(id="personainst_neko", display_name="Neko Mission Lead", workspace_id="ws_a")
    child = _instance(
        id="personainst_dev",
        persona_id="dev",
        display_name="Dev",
        workspace_id="ws_b",
        steered_by=["personainst_neko"],
    )
    full = [steerer, child]

    scope_ws = workspace_scope.effective_workspace_id(child, active_workspace_id="ws_a")
    assert scope_ws == "ws_b"
    scoped = workspace_scope.scope_roster(full, scope_workspace_id=scope_ws)
    hud = resolve_situational_hud(child, roster=scoped, identity_roster=full)

    # Addressable roster is scoped — the out-of-workspace steerer is not on-level.
    assert [entry["persona_instance_id"] for entry in hud["roster"]] == ["personainst_dev"]
    # ...but the steerer's identity still resolves to a real name.
    steered_by = hud["steering"]["steered_by"]
    assert steered_by[0]["persona_instance_id"] == "personainst_neko"
    assert steered_by[0]["display_name"] == "Neko Mission Lead"


def test_situational_hud_thread_count_uses_scoped_roster():
    from agent_runtime import workspace_scope

    # A goal with three threads, one of which is a duplicate placement in another
    # workspace. The thread count reflects only the sender-workspace threads, so
    # a two-agent order is not inflated by the out-of-scope placement.
    target = _instance(
        id="personainst_dev", persona_id="dev", display_name="Dev",
        goal_id="goal_1", current_task_id="task_1", workspace_id="ws_a",
    )
    peer_same = _instance(id="personainst_qa", persona_id="qa", display_name="QA", goal_id="goal_1", workspace_id="ws_a")
    peer_other_ws = _instance(id="personainst_qa_b", persona_id="qa", display_name="QA (2)", goal_id="goal_1", workspace_id="ws_b")
    full = [target, peer_same, peer_other_ws]

    scope_ws = workspace_scope.effective_workspace_id(target, active_workspace_id="ws_a")
    scoped = workspace_scope.scope_roster(full, scope_workspace_id=scope_ws)
    hud = resolve_situational_hud(
        target,
        roster=scoped,
        identity_roster=full,
    )
    # Two ws_a threads (self + peer); the ws_b duplicate placement is excluded.
    assert hud["mission"]["thread_count"] == 2


def test_situational_hud_on_level_shadows_canonical_when_placement_in_scope():
    # "Placements shadow canonical" at the HUD roster: when an in-scope PLACEMENT
    # of a persona exists, its plumbing canonical row is dropped from "On level"
    # (and from the mission thread count), so the agent sees the deliberate
    # placement, not the runtime-global canonical. Composes the real
    # addressable_roster the HUD wrapper uses (scope + shadow), keyed on the
    # sanctioned canonical discriminator.
    from agent_runtime import workspace_scope
    from agent_runtime.persona_assignments import is_canonical_persona_channel

    target = _instance(
        id="personainst_neko", persona_id="neko_supervisor", display_name="Neko Mission Lead",
        goal_id="goal_1", current_task_id="task_1", workspace_id="ws_a",
    )
    # dev's runtime-global canonical row + an in-scope placement, both on goal_1.
    dev_canonical = _instance(
        id="personainst_dev", persona_id="dev", display_name="Dev",
        goal_id="goal_1", workspace_id=None,
    )
    dev_placement = _instance(
        id="personainst_dev_agent_2", persona_id="dev", display_name="Dev (2)",
        goal_id="goal_1", workspace_id="ws_a",
    )
    full = [target, dev_canonical, dev_placement]

    scope_ws = workspace_scope.effective_workspace_id(target, active_workspace_id="ws_a")
    addressable = workspace_scope.addressable_roster(
        full, scope_workspace_id=scope_ws, is_canonical=is_canonical_persona_channel
    )
    hud = resolve_situational_hud(
        target,
        roster=addressable,
        identity_roster=full,
    )

    ids = [entry["persona_instance_id"] for entry in hud["roster"]]
    assert "personainst_dev" not in ids  # canonical dev shadowed by its placement
    assert ids == ["personainst_neko", "personainst_dev_agent_2"]
    # thread_count follows the SAME addressable roster: neko + dev placement = 2,
    # NOT 3 — the shadowed canonical dev is not counted.
    assert hud["mission"]["thread_count"] == 2


def test_situational_hud_identity_roster_defaults_to_roster():
    # Back-compat: callers that pass a single roster (no identity_roster) keep
    # identical steering behaviour — identity resolves from that one list.
    lead = _instance(id="personainst_neko", display_name="Neko Mission Lead")
    child = _instance(id="personainst_dev", display_name="Dev", steered_by=["personainst_neko"])
    hud = resolve_situational_hud(child, roster=[lead, child])
    assert hud["steering"]["steered_by"][0]["display_name"] == "Neko Mission Lead"
