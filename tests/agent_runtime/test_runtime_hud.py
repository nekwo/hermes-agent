from types import SimpleNamespace

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
        daemon={"state": "starting", "loops": 0, "next_wake_at": "2026-07-13T15:12:00Z"},
        realm="default",
        workspace="default",
        roster=roster,
    )
    assert hud["preview"] is True
    assert hud["runtime"]["state"] == "starting"
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


def test_resolve_situational_hud_bound_lane_carries_mission_and_thread_count():
    instance = _instance(goal_id="goal_1", current_task_id="task_stage")
    peer = _instance(id="personainst_dev", display_name="Dev", goal_id="goal_1")
    other = _instance(id="personainst_x", display_name="X", goal_id="goal_other")
    task = SimpleNamespace(id="task_stage", title="Stage task", state="in_progress")
    goal_task = SimpleNamespace(id="goal_1", title="Neko default graph live token burn", state="queued")
    hud = resolve_situational_hud(
        instance,
        daemon={"state": "running", "loops": 3},
        realm="default",
        workspace="default",
        roster=[instance, peer, other],
        task=task,
        goal_task=goal_task,
        proof_store=None,
    )
    assert hud["mission"]["title"] == "Neko default graph live token burn"
    assert hud["mission"]["state"] == "queued"
    # Two lanes share goal_1 (self + peer); the goal_other lane is excluded.
    assert hud["mission"]["thread_count"] == 2


def test_resolve_situational_hud_reuses_mission_hud_preview(monkeypatch):
    from agent_runtime import context_builder

    sentinel = {"preview": True, "typed_current_stage": {"id": "s1", "status": "in_progress"}}
    monkeypatch.setattr(context_builder, "mission_hud_preview", lambda task, *, proof_store=None: sentinel)
    hud = resolve_situational_hud(
        _instance(current_task_id="task_stage"),
        task=SimpleNamespace(id="task_stage", title="t", state="in_progress"),
    )
    assert hud["mission_hud"] == sentinel


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
    task = SimpleNamespace(id="task_stage", title="Stage", state="in_progress")
    goal_task = SimpleNamespace(id="goal_1", title="Live token burn", state="queued")
    hud = resolve_situational_hud(
        instance,
        daemon={"state": "starting", "loops": 0},
        realm="default",
        workspace="default",
        roster=[instance],
        task=task,
        goal_task=goal_task,
    )
    block = render_situational_hud_block(hud)
    assert block.startswith("## Runtime Situation")
    assert "read-only" in block
    assert "- Runtime: state starting" in block
    assert "- Scope: realm default · workspace default" in block
    assert "Live token burn" in block
    assert "@personainst_neko" in block
    assert "- On level (1):" in block


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


def test_mission_chat_surface_message_injects_runtime_situation_block():
    from agent_runtime.persona_runtime import _mission_chat_surface_message

    block = (
        "## Runtime Situation\nThis mirrors the operator's Mission Control "
        "runtime HUD.\n- Runtime: state starting · loop 0"
    )
    message = _mission_chat_surface_message(
        _chat_persona(), "", situational_hud_content=block
    )
    assert "## Runtime Situation" in message
    # The block sits after the "you ARE Neko" identity hat so the agent has its
    # situational context, and the anti-fabrication rules still lead.
    assert message.index("## Runtime Situation") > message.index("Neko Mission Lead")


def test_mission_chat_surface_message_omits_block_when_empty():
    from agent_runtime.persona_runtime import _mission_chat_surface_message

    message = _mission_chat_surface_message(
        _chat_persona(), "", situational_hud_content=""
    )
    assert "## Runtime Situation" not in message
