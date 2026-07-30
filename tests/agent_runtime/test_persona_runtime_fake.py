import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from hermes_time import now

from agent_runtime.context_builder import build_context
from agent_runtime.decision_schema import DecisionPayloadInvalid, DecisionType
from agent_runtime.events import EventLog
from agent_runtime.models import AgentRun, Event
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.persona_runtime import GPTPersonaRuntime
from agent_runtime.persona_runtime import _apply_llm_metadata
from agent_runtime.profile_runner import AgentRunResult
from agent_runtime.personas import (
    REGISTRY_HYGIENE_BLOCKED_TOOLS,
    all_registered_toolsets,
    effective_toolsets,
)
from tests.agent_runtime.persona_samples import sample_personas
from agent_runtime.tool_permissions import ChatToolPermissionStore
from agent_runtime.states import RunState, TaskState


@pytest.fixture(autouse=True)
def _bundled_profiles_exist(bundled_persona_profiles):
    """Every test here drives the REAL run path with a BUNDLED persona.

    ``9ad9c8017`` made a bound Hermes profile a hard precondition of
    ``_invoke_agent``; without it the runtime refuses before any decision is
    produced and every assertion below reads
    ``Hermes profile 'launcher-dev' does not exist`` instead of what it is
    about."""

    return bundled_persona_profiles


class FakeAIAgent:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        FakeAIAgent.instances.append(self)

    def run_conversation(self, *, user_message, system_message, task_id):
        self.calls.append({"user_message": user_message, "system_message": system_message, "task_id": task_id})
        return {
            "final_response": json.dumps(
                {
                    "type": "propose_stage_plan",
                    "summary": "Plan the safe implementation stages.",
                    "rationale": "The task needs staged execution before patching.",
                    "payload": {
                        "stages": [
                            {
                                "title": "Audit",
                                "objective": "Inspect repo",
                                "acceptance_criteria": ["Repo surfaces are identified."],
                                "affected_paths": ["agent_runtime/"],
                                "test_plan": ["Run targeted harness tests."],
                            }
                        ]
                    },
                    "requires_approval": False,
                    "schema_version": 1,
                }
            ),
            "session_id": "session_from_fake",
            "api_calls": 1,
            "model": "gpt-5.5",
            "provider": "openai-codex",
            "base_url": "https://chatgpt.com/backend-api/codex/?token=SECRET",
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "turn_exit_reason": "text_response(finish_reason=stop)",
        }


def make_task_and_run():
    ts = now()
    task = Task(
        id="task_abc",
        title="Build harness",
        description="Make agent runtime reliable",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
    )
    run = AgentRun(
        id="run_abc",
        persona_id="dev",
        task_id=task.id,
        stage_id=None,
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
        iteration_budget=12,
        session_id="session_existing",
    )
    return task, run


def test_apply_llm_metadata_tolerates_non_dict_raw_result():
    _task, run = make_task_and_run()
    result = AgentRunResult(
        final_response='{"type":"report_qa_verdict","summary":"ok","rationale":"ok"}',
        session_id="session_from_fake",
        provider="openai-codex",
        model="gpt-5.5",
        base_url="https://chatgpt.com/backend-api/codex",
        messages=[],
        api_calls=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        latency_ms=123,
        raw="text_response(finish_reason=stop)",  # type: ignore[arg-type]
    )

    _apply_llm_metadata(run, result, timing={})

    assert run.llm["provider"] == "openai-codex"
    assert "finish_reason" not in run.llm
    assert run.llm["total_tokens"] == 15




def _unbounded_chat_toolsets():
    """What ``unbounded`` actually resolves on the chat lane."""

    return all_registered_toolsets()


def test_chat_permission_unbounded_reaches_actual_agent_request(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    FakeAIAgent.instances.clear()
    session_id = "session_unbounded_actual"
    qa = next(persona for persona in sample_personas() if persona.id == "qa")
    ChatToolPermissionStore().set(
        persona_id=qa.id,
        session_id=session_id,
        mode="unbounded",
        reason="operator enabled full tools for this chat",
    )
    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=FakeAIAgent)

    runtime.chat_reply(qa, "can you write now?", session_id=session_id)

    fake = FakeAIAgent.instances[0]
    # `unbounded` resolves the whole live registry. The retired mission creation
    # toolset is absent from that registry rather than filtered per turn.
    assert fake.kwargs["enabled_toolsets"] == _unbounded_chat_toolsets()
    # T6c registry hygiene rides every construction, unbounded included:
    # kanban/feishu are registry junk, not a permission tier, so the escape
    # hatch does not resurrect them.
    assert set(fake.kwargs["blocked_tool_names"]) == set(REGISTRY_HYGIENE_BLOCKED_TOOLS)


def test_chat_permission_unbounded_one_turn_expires_after_success(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    FakeAIAgent.instances.clear()
    session_id = "session_unbounded_once"
    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")
    store = ChatToolPermissionStore()
    store.set(
        persona_id=neko.id,
        session_id=session_id,
        mode="unbounded",
        reason="operator enabled one turn",
        turns_remaining=1,
    )
    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=FakeAIAgent)

    runtime.chat_reply(neko, "run the command", session_id=session_id)

    fake = FakeAIAgent.instances[0]
    assert fake.kwargs["enabled_toolsets"] == _unbounded_chat_toolsets()
    # Registry hygiene applies even on the unbounded turn (see the QA test above).
    assert set(fake.kwargs["blocked_tool_names"]) == set(REGISTRY_HYGIENE_BLOCKED_TOOLS)
    record = store.get(persona_id=neko.id, session_id=session_id)
    assert record is not None
    assert record.mode == "profile_default"
    assert record.turns_remaining == 0


def test_chat_reply_can_disable_internal_session_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    FakeAIAgent.instances.clear()
    session_db = object()
    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")
    runtime = GPTPersonaRuntime(
        default_provider="openai-codex",
        default_model="gpt-5.5",
        agent_factory=FakeAIAgent,
        session_db=session_db,
        persist_agent_session=False,
    )

    runtime.chat_reply(neko, "hi", session_id=None)

    fake = FakeAIAgent.instances[0]
    assert fake.kwargs["session_db"] is None


def test_chat_reply_routes_tool_calls_into_session_keyed_trace(tmp_path, monkeypatch):
    # End-to-end through the REAL ProfileAgentRunner: a chat turn that invokes a
    # tool must land redaction-safe run.tool.* events keyed on the chat session,
    # so the snapshot trace projection can surface them in the operator channel.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    FakeAIAgent.instances.clear()
    session_id = "session_chat_tool_trace"
    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")

    class ToolCallingAgent(FakeAIAgent):
        def run_conversation(self, *, user_message, system_message, task_id):
            # Drive the runner's tool callbacks exactly as the real provider does.
            self.kwargs["tool_start_callback"]("tool.started", "terminal", "echo PARITY_OK_2026")
            self.kwargs["tool_complete_callback"](
                "tool.completed", "terminal", "echo PARITY_OK_2026", {"exit_code": 0}
            )
            return super().run_conversation(
                user_message=user_message, system_message=system_message, task_id=task_id
            )

    runtime = GPTPersonaRuntime(
        default_provider="openai-codex", default_model="gpt-5.5", agent_factory=ToolCallingAgent
    )

    pre_trace = []
    runtime.chat_reply(
        neko,
        "run echo PARITY_OK_2026 and paste the output",
        session_id=session_id,
        pre_trace_callback=pre_trace.append,
    )

    events = EventLog().for_session(session_id)
    assert len(pre_trace) == 1
    assert pre_trace[0]["type"] == "run.tool.started"
    assert pre_trace[0]["tool_name"] == "terminal"
    assert [event.type for event in events] == ["run.tool.started", "run.tool.finished"]
    assert all(event.session_id == session_id for event in events)
    assert all(event.task_id is None for event in events)
    assert all(event.persona_id == "neko_supervisor" for event in events)
    assert events[0].payload.get("tool_name") == "terminal"
    assert events[1].payload.get("status") == "passed"


def test_reasoning_summary_does_not_fire_pre_trace_ack(tmp_path, monkeypatch):
    # gpt-5.5 (codex) emits a reasoning-summary run.progress event on EVERY
    # turn, including tool-less "Hi" turns. It belongs in the Trace lane but
    # must NOT latch before_first_trace: doing so persisted a canned
    # "I'll check that now…" acknowledgment row on every conversational turn
    # (a phantom transcript row with no client_message_id that reordered the
    # Agent Console at snapshot reconcile). The ack hook fires on the first
    # run.tool.started only.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    from agent_runtime.progress import ChatProgressSink

    fired = []
    sink = ChatProgressSink(
        session_id="session_reasoning_no_ack",
        persona_id="neko_supervisor",
        before_first_trace=fired.append,
    )

    sink.emit(
        "run.progress",
        {
            "type": "run.progress",
            "phase": "thinking_process",
            "step": "reasoning_summary",
            "status": "running",
            "reasoning_summary": "Hi Master — I'm here.",
        },
    )
    assert fired == []  # reasoning trace recorded, ack hook untouched

    sink.emit(
        "run.tool.started",
        {"type": "run.tool.started", "tool_name": "terminal", "status": "running"},
    )
    assert len(fired) == 1
    assert fired[0]["type"] == "run.tool.started"

    # Latched: a second tool start must not re-fire the ack.
    sink.emit(
        "run.tool.started",
        {"type": "run.tool.started", "tool_name": "terminal", "status": "running"},
    )
    assert len(fired) == 1


def test_persona_chat_prompt_allows_real_tools_and_forbids_fabrication():
    from agent_runtime.persona_runtime import _persona_chat_system_prompt

    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")
    prompt = _persona_chat_system_prompt(neko)

    # Full tool access: actually use tools, permission-gated, no escalation nudge.
    assert "actually use your tools" in prompt
    assert "permission grant is the only gate" in prompt
    assert "hand it off as real work" not in prompt
    assert "task pipeline" not in prompt
    # Hard anti-fabrication invariant.
    assert "Never fabricate" in prompt
    assert "inventing output" in prompt
    # Embodiment context: office + HUD state the operator can see.
    assert "Mission Control office" in prompt
    assert "steer handle" in prompt
    # Operator channel is the ask-when-ambiguous surface (vs act_dont_ask on the
    # autonomous goal path): clarify underspecified orders via the clarify tool
    # instead of guessing.
    assert "use the `clarify` tool to ask before acting" in prompt


def test_clarify_enabled_and_unblocked_on_chat_lane_but_blocked_on_runs():
    from agent_runtime.persona_runtime import (
        _blocked_tool_names_for_chat,
        _enabled_toolsets_for_chat,
    )
    from agent_runtime.personas import blocked_tool_names

    personas = {p.id: p for p in sample_personas()}
    for pid in ("neko_supervisor", "dev", "qa"):
        persona = personas[pid]
        # Chat lane: clarify toolset is offered and the tool is not blocked, so
        # the non-blocking clarify bridge can record a question.
        assert "clarify" in _enabled_toolsets_for_chat(persona, session_id=None)
        assert "clarify" not in _blocked_tool_names_for_chat(persona, session_id=None)
        # Autonomous run lane still blocks clarify — no interactive answer there.
        assert "clarify" in blocked_tool_names(persona)


def test_mission_chat_surface_message_always_carries_operative_rules():
    from agent_runtime.persona_runtime import (
        _mission_chat_identity_prompt,
        _mission_chat_operative_rules,
        _mission_chat_surface_message,
    )

    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")

    # Blank operator surface still injects the identity block THEN the operative
    # rules (so the "you are <persona>" hat and the anti-fabrication invariant
    # both hold on the default operator channel).
    identity = _mission_chat_identity_prompt(neko)
    rules = _mission_chat_operative_rules()
    assert _mission_chat_surface_message(neko, "") == identity + "\n\n" + rules
    assert _mission_chat_surface_message(neko, None) == identity + "\n\n" + rules
    # Identity comes first; rules follow.
    assert _mission_chat_surface_message(neko, "").startswith(identity)
    assert "Never fabricate" in _mission_chat_surface_message(neko, "")
    # Ambiguous-order clarify norm rides the always-injected operative rules,
    # and names the clarify tool + the answer-threads-back contract.
    assert "use the `clarify` tool to ask before acting" in rules
    assert "answer arrives as their next message" in rules
    # Relay lane: answer a briefed agent's clarify instead of dropping/guessing.
    assert "answer it by sending the choice back" in rules

    # An operator-supplied surface prompt is layered after the rules, not instead.
    composed = _mission_chat_surface_message(neko, "Focus on the auth refresh path.")
    assert composed.startswith(identity)
    assert "Never fabricate" in composed
    assert composed.endswith("Focus on the auth refresh path.")

    workspace_composed = _mission_chat_surface_message(
        neko,
        "",
        workspace_agents_content="# Selected workspace\nUse its conventions.",
    )
    assert "operator-selected AGENTS.md" in workspace_composed
    assert workspace_composed.endswith("Use its conventions.")


def test_mission_chat_ack_is_the_first_hard_rule():
    # §7 acknowledge-before-acting promotion: the ack requirement is the FIRST
    # bullet of the operative rules and phrased as a hard requirement, so a
    # model that skims the rules still meets it before its first tool call.
    from agent_runtime.persona_runtime import _mission_chat_operative_rules

    rules = _mission_chat_operative_rules()
    bullets = [line for line in rules.splitlines() if line.startswith("- ")]
    assert bullets, "operative rules must be a bulleted list"
    first = bullets[0]
    assert "HARD RULE" in first
    assert "before your first tool call" in first
    assert "never open a turn with a silent tool call" in first


def test_mission_chat_operative_rules_teach_the_chat_session_verbs():
    # The chat-session verb set is taught in the busy operative-rules prompt (not
    # only the tool schemas): teammates are addressable by @personainst_* handles,
    # and the three thread lanes of agent_chat_send (omit / session_id /
    # new_session) plus the read verbs are spelled out. Rule #1 must STILL be the
    # ack rule — the new bullet may not displace it.
    from agent_runtime.persona_runtime import _mission_chat_operative_rules

    rules = _mission_chat_operative_rules()
    bullets = [line for line in rules.splitlines() if line.startswith("- ")]
    assert "HARD RULE" in bullets[0], "the acknowledge-before-acting rule must remain first"

    verbs_bullet = next(
        (line for line in bullets if "agent_chat_threads" in line and "agent_chat_open" in line),
        None,
    )
    assert verbs_bullet is not None, "operative rules must teach the chat-session verb set"
    assert "@personainst_" in verbs_bullet
    assert "new_session" in verbs_bullet
    assert "session_id" in verbs_bullet


def test_mission_chat_operative_rules_route_named_agents_without_creating_goals():
    from agent_runtime.persona_runtime import _mission_chat_operative_rules

    # The RULE, not the paragraph. c2320b73e ("keep normal persona chat
    # task-free") rewrote this bullet to match the code change that made
    # `mission_goal` a per-turn caller opt-in rather than a role capability;
    # pinning the old prose byte-for-byte pinned the wording and said nothing
    # about the instruction actually surviving. These are the two halves that
    # must: route named agents with `agent_chat_send`, and never let ordinary
    # chat work imply goal creation.
    rules = _mission_chat_operative_rules()
    routing = next(
        (
            line
            for line in rules.splitlines()
            if line.startswith("- ") and "agent_chat_send" in line and "named agents" in line
        ),
        None,
    )
    assert routing is not None, "operative rules must route named agents to agent_chat_send"
    assert "send, brief, or coordinate named agents" in routing
    assert "never imply goal creation" in routing
    assert "chat-only for every role" in routing


def test_mission_chat_operative_rules_preserve_media_lines_verbatim():
    # A MEDIA:<path> line standing alone is a DECLARATION the operator console
    # renders as a titled image card. The two failure modes are NOT the same:
    # backticking or fencing that line un-declares it and nothing paints at all,
    # while retyping the path loose in a sentence still previews the image and
    # merely demotes it (untitled, raw path left in the prose). The rules must
    # teach the verbatim relay AND a WHY that survives contact with the console —
    # a lead who watches a backticked path render fine must not catch the rule
    # overstating its own mechanism, because that discredits it exactly where it
    # is right. It must also read as the explicit carve-out to the clean-prose
    # rule that precedes it (not as a competing instruction).
    from agent_runtime.persona_runtime import _mission_chat_operative_rules

    rules = _mission_chat_operative_rules()
    bullets = [line for line in rules.splitlines() if line.startswith("- ")]

    media_bullet = next((line for line in bullets if "MEDIA:" in line), None)
    assert media_bullet is not None, "operative rules must teach the MEDIA-verbatim relay"
    assert "VERBATIM" in media_bullet
    assert "never wrap it in backticks or a code fence" in media_bullet
    assert "bare absolute screenshot path" in media_bullet

    # The WHY ships with the rule — a rule without its reason gets rationalized away.
    assert "WHY:" in media_bullet
    # ...and it names both outcomes honestly rather than collapsing them into
    # "any rewrap loses the image", which is checkably false for a bare path.
    assert "un-declares it" in media_bullet
    assert "NOTHING renders" in media_bullet
    assert "still previews" in media_bullet

    # The rule must not demonstrate the form it forbids. Models imitate exemplar
    # surface forms, so the bullet that bans backticking a MEDIA line carries no
    # backticks of its own — the placeholder is plain prose.
    assert "`" not in media_bullet, "the MEDIA rule must not model the formatting it forbids"

    # It is the carve-out to the clean-prose bullet, so it must follow it.
    prose_index = next(
        i for i, line in enumerate(bullets) if "Keep replies as clean teammate prose" in line
    )
    assert bullets.index(media_bullet) == prose_index + 1


def test_persona_soul_overlay_layers_between_identity_and_rules(tmp_path, monkeypatch):
    # A profile-backed persona reads its canonical SOUL.md by default (an
    # explicit `soul_overlay_path` remains an override) from
    # soul from ITS OWN profile home (single source — realm sync already models
    # soul_overlay as profile-home-relative) and layers it into BOTH chat lanes
    # — after the identity hat, before the operative rules on the mission-chat
    # surface. Personas without one keep the exact legacy composition.
    from dataclasses import replace

    import agent_runtime.persona_runtime as pr

    profile_home = tmp_path / "profiles" / "neko"
    profile_home.mkdir(parents=True)
    (profile_home / "SOUL.md").write_text(
        "You are Neko, the Mission Lead — test soul.\n"
        "- Before your first tool call in any turn, tell Tony what you're about to do.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pr, "_persona_profile_home", lambda name: profile_home if name == "neko" else None
    )

    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")
    souled = replace(neko, soul_overlay_path=None, hermes_profile="neko")

    composed = pr._mission_chat_surface_message(souled, "")
    identity = pr._mission_chat_identity_prompt(souled)
    rules = pr._mission_chat_operative_rules()
    soul_marker = "You are Neko, the Mission Lead — test soul."
    ack_habit = "Before your first tool call in any turn"
    assert soul_marker in composed
    assert ack_habit in composed
    assert composed.index(identity[:60]) < composed.index(soul_marker) < composed.index(
        "Mission Control operator-chat rules"
    )
    # The rules still ride after the soul — soul shapes voice, never displaces
    # the surface invariants.
    assert rules in composed

    # Persona-chat lane carries the same soul.
    chat_prompt = pr._persona_chat_system_prompt(souled)
    assert soul_marker in chat_prompt
    assert chat_prompt.index("operator-channel agent") < chat_prompt.index(soul_marker)

    # A bogus explicit path degrades to no soul, never an error or an implicit
    # fallback that hides the bad override.
    bogus = replace(neko, soul_overlay_path="does_not_exist.md", hermes_profile="neko")
    assert (
        pr._mission_chat_surface_message(bogus, "")
        == pr._mission_chat_identity_prompt(bogus) + "\n\n" + rules
    )


def test_profile_backed_soul_never_falls_through_to_operator_home(tmp_path, monkeypatch):
    # Identity-leak guard: when a profile-backed persona's soul misses, a bare
    # `SOUL.md` path must NOT resolve to the OPERATOR profile's SOUL.md via the
    # operator-home fallback (that would put Alice's soul on Neko).
    from dataclasses import replace

    import agent_runtime.persona_runtime as pr

    operator_home = tmp_path / "profiles" / "alice"
    operator_home.mkdir(parents=True)
    (operator_home / "SOUL.md").write_text("OPERATOR SOUL — must not leak", encoding="utf-8")
    monkeypatch.setattr(pr, "get_hermes_home", lambda: operator_home)
    monkeypatch.setattr(pr, "_persona_profile_home", lambda name: None)

    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")
    souled = replace(neko, soul_overlay_path="SOUL.md", hermes_profile="neko")
    composed = pr._mission_chat_surface_message(souled, "")
    assert "OPERATOR SOUL" not in composed

    # A persona WITHOUT a bound profile keeps the legacy operator-home lane.
    legacy = replace(neko, soul_overlay_path="SOUL.md", hermes_profile=None)
    assert "OPERATOR SOUL" in pr._mission_chat_surface_message(legacy, "")


def test_mission_chat_identity_prompt_names_persona_and_forbids_self_relay():
    # Root-cause guard for the "Neko messages itself" incident: the isolated
    # chat lane does not load the profile SOUL, so this block is the ONLY place
    # the model learns which persona it is. It must name the persona, name the
    # persona id (so a self-directed agent_chat_send is recognizable), and
    # forbid relaying to itself.
    from agent_runtime.persona_runtime import _mission_chat_identity_prompt

    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")
    identity = _mission_chat_identity_prompt(neko)

    assert "You are Neko Mission Lead" in identity
    assert "`neko_supervisor`" in identity
    assert "agent_chat_send" in identity
    assert "that persona is you" in identity
    assert "already the persona speaking in this channel" in identity
    # No leaked Alice-identity / third-person "deploy Neko" framing.
    assert "Alice" not in identity
    assert "catgirl" not in identity.lower()


def test_mission_chat_reply_injects_operative_rules_into_system_message(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")
    captured = {}

    class CapturingRunner:
        def run(self, request):
            captured["request"] = request
            return AgentRunResult(
                final_response="ok",
                session_id="session_mission_chat",
                provider="openai-codex",
                model="gpt-5.5",
                base_url=None,
                messages=[],
            )

    runtime = GPTPersonaRuntime(
        default_provider="openai-codex", default_model="gpt-5.5", agent_runner=CapturingRunner()
    )

    def agent_ready(_agent):
        return None

    runtime.mission_chat_reply(
        neko,
        "run echo PARITY_OK_2026",
        permission_session_id="session_mission_chat",
        agent_ready_callback=agent_ready,
        workspace_agents_content="# Workspace rules\nUse the selected conventions.",
    )

    system_message = captured["request"].system_message
    # Identity block leads the system message (the "you ARE Neko" hat) so the
    # isolated chat lane never externalizes the persona and relays to itself.
    assert system_message.startswith("You are Neko Mission Lead")
    assert "that persona is you" in system_message
    assert "Never fabricate" in system_message
    assert "actually use your tools" in system_message
    assert "# Workspace rules" in system_message
    # And the chat-trace callback is wired on the canonical operator path too.
    assert captured["request"].progress_callback is not None
    assert captured["request"].agent_ready_callback is agent_ready


def test_mission_chat_reply_rides_hud_on_user_turn_not_system_prompt(tmp_path, monkeypatch):
    # T5 wiring guard: the caller passes the resolved situational HUD block; it
    # must land on the operator's USER turn (after the message), never in the
    # system prompt (the codex instructions), so the byte-stable cross-turn
    # cache prefix survives. Pins the mission_chat_reply -> _mission_chat_user_message
    # / _mission_chat_surface_message wiring against a silent regression.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")
    captured = {}

    class CapturingRunner:
        def run(self, request):
            captured["request"] = request
            return AgentRunResult(
                final_response="ok",
                session_id="session_mission_chat",
                provider="openai-codex",
                model="gpt-5.5",
                base_url=None,
                messages=[],
            )

    runtime = GPTPersonaRuntime(
        default_provider="openai-codex", default_model="gpt-5.5", agent_runner=CapturingRunner()
    )

    hud_block = (
        "## Runtime Situation\nThis mirrors the operator's Mission Control "
        "runtime HUD.\n- Scope: realm default · workspace alpha\n"
        "- On level (1): QA Agent (@personainst_qa)"
    )
    runtime.mission_chat_reply(
        neko,
        "what's the state?",
        permission_session_id="session_mission_chat",
        situational_hud_content=hud_block,
    )

    request = captured["request"]
    # HUD is on the user turn, after the operator's message.
    assert hud_block in request.user_message
    assert request.user_message.index(hud_block) > request.user_message.index(
        "what's the state?"
    )
    # HUD is NOT in the system prompt — the instructions stay byte-stable.
    assert "## Runtime Situation" not in request.system_message
    assert hud_block not in request.system_message


def test_mission_chat_user_message_orders_skill_then_hud_after_message():
    # T9a placement unit: the operator message leads, then the queued-skill
    # preload, then the live Runtime HUD (HUD stays last). Backward-compatible
    # shapes (no skill / no HUD / HUD-only) match the T5 behaviour exactly.
    from agent_runtime.persona_runtime import _mission_chat_user_message

    body = "operator message with baked history"
    skill = "Loaded skill: deep-audit\nFollow this procedure."
    hud = "## Runtime Situation\n- Scope: realm default"

    composed = _mission_chat_user_message(body, hud, preloaded_skill_prompt=skill)
    assert composed.index(body) < composed.index(skill) < composed.index(hud)

    assert _mission_chat_user_message(body) == body
    assert _mission_chat_user_message(body, hud) == f"{body}\n\n{hud}"
    assert (
        _mission_chat_user_message(body, preloaded_skill_prompt=skill)
        == f"{body}\n\n{skill}"
    )


def test_mission_chat_reply_skill_preload_rides_user_turn_byte_stable(tmp_path, monkeypatch):
    # T9a byte-stability: the queued-skill preload — T5's flagged secondary
    # cache-invalidation vector — now rides the operator USER turn with the HUD,
    # never the codex ``instructions``. Two turns of one conversation that differ
    # ONLY in preloaded-skill state must produce BYTE-IDENTICAL instructions so
    # the cross-turn prompt cache prefix survives a mid-conversation skill load;
    # the skill content lands on the user turn instead.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")

    captured: list = []

    class CapturingRunner:
        def run(self, request):
            captured.append(request)
            return AgentRunResult(
                final_response="ok",
                session_id="session_mission_chat",
                provider="openai-codex",
                model="gpt-5.5",
                base_url=None,
                messages=[],
            )

    runtime = GPTPersonaRuntime(
        default_provider="openai-codex", default_model="gpt-5.5", agent_runner=CapturingRunner()
    )

    skill_block = (
        "Loaded skill: deep-audit\nFollow this procedure when auditing the harness."
    )
    # Turn 1: operator loads a skill this turn.
    runtime.mission_chat_reply(
        neko,
        "run the audit",
        permission_session_id="session_mission_chat",
        preloaded_skill_prompt=skill_block,
    )
    # Turn 2: same conversation, no skill loaded.
    runtime.mission_chat_reply(
        neko,
        "and the next step?",
        permission_session_id="session_mission_chat",
        preloaded_skill_prompt=None,
    )

    with_skill, without_skill = captured
    # Instructions (system message) are byte-identical regardless of skill state.
    assert with_skill.system_message == without_skill.system_message
    # The skill preload is NOT in the byte-stable system prompt...
    assert "deep-audit" not in with_skill.system_message
    assert "Loaded skill" not in with_skill.system_message
    # ...it rides the operator USER turn instead, trailing the operator message.
    assert skill_block in with_skill.user_message
    assert with_skill.user_message.index(skill_block) > with_skill.user_message.index(
        "run the audit"
    )
    # No skill this turn -> nothing leaks onto the user turn.
    assert "deep-audit" not in without_skill.user_message


def test_mission_chat_reply_honors_include_profile_memory(tmp_path, monkeypatch):
    # A persona bound to a profile for CAPABILITIES must not also inherit that
    # profile's MEMORY.md/USER.md worldview unless it opts in. skip_memory now
    # tracks include_profile_memory instead of being hardcoded False (which had
    # loaded Alice's "goal->Neko->Dev" memory into every Neko turn).
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    from agent_runtime.models import AgentPersona

    def _persona(include_memory: bool) -> AgentPersona:
        return AgentPersona(
            id="neko_supervisor",
            display_name="Neko Mission Lead",
            role="alice_supervisor",
            model="gpt-5.5",
            provider="openai-codex",
            api_mode="codex_responses",
            toolsets=["file", "search", "skills"],
            system_prompt_path="",
            hermes_profile=None,
            include_profile_memory=include_memory,
        )

    captured = {}

    class CapturingRunner:
        def run(self, request):
            captured["request"] = request
            return AgentRunResult(
                final_response="ok",
                session_id="s",
                provider="openai-codex",
                model="gpt-5.5",
                base_url=None,
                messages=[],
            )

    runtime = GPTPersonaRuntime(
        default_provider="openai-codex", default_model="gpt-5.5", agent_runner=CapturingRunner()
    )

    runtime.mission_chat_reply(_persona(False), "hi", permission_session_id="s")
    assert captured["request"].skip_memory is True

    runtime.mission_chat_reply(_persona(True), "hi", permission_session_id="s")
    assert captured["request"].skip_memory is False


def test_profile_role_sentinel_resolves_to_supervisor_capabilities():
    # A synthetic operator-channel persona built from a raw Hermes profile carries
    # the "profile" role sentinel. It must resolve (not raise 'profile' is not a
    # valid AgentRole) and intersect down to its own configured toolsets.
    from agent_runtime.personas import (
        AgentRole,
        coerce_agent_role,
        effective_toolsets,
        role_from_persona,
    )
    from agent_runtime.models import AgentPersona

    assert coerce_agent_role("profile") == "profile"

    profile = AgentPersona(
        id="profile:alice",
        display_name="Alice Agent",
        role="profile",
        model="gpt-5.5",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file", "search", "session_search", "todo", "skills"],
        system_prompt_path="",
        hermes_profile="alice",
    )

    assert role_from_persona(profile) == "profile"
    # The profile's own toolsets survive the supervisor-ceiling intersection.
    assert effective_toolsets(profile) == ["file", "search", "session_search", "todo", "skills"]


def test_mission_chat_reply_runs_for_profile_persona(tmp_path, monkeypatch):
    # Regression: the operator chat path (mission_chat_reply -> toolset/blocked
    # resolution -> role_from_persona) used to raise "'profile' is not a valid
    # AgentRole" for every profile persona, killing the whole turn before the model.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    from agent_runtime.models import AgentPersona

    profile = AgentPersona(
        id="profile:alice",
        display_name="Alice Agent",
        role="profile",
        model="gpt-5.5",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file", "search", "session_search", "todo", "skills"],
        system_prompt_path="",
        # None -> binding "inherits active profile" so the test does not depend on a
        # profile on disk; the role="profile" sentinel still drives toolset resolution.
        hermes_profile=None,
    )
    captured = {"requests": []}

    class CapturingRunner:
        def run(self, request):
            captured["request"] = request
            captured["requests"].append(request)
            return AgentRunResult(
                final_response="hi from alice",
                session_id="session_profile_chat",
                provider="openai-codex",
                model="gpt-5.5",
                base_url=None,
                messages=[],
            )

    runtime = GPTPersonaRuntime(
        default_provider="openai-codex", default_model="gpt-5.5", agent_runner=CapturingRunner()
    )

    result = runtime.mission_chat_reply(
        profile, "say hi", permission_session_id="session_profile_chat"
    )

    assert result.final_response == "hi from alice"
    request = captured["request"]
    # Toolsets resolve through the supervisor ceiling, but ordinary profile chat
    # is globally chat-only and cannot create a durable mission by default.
    assert "mission_goal" not in request.enabled_toolsets
    # The chat lane keeps the supervision toolsets; the T6a cost policy drops the
    # `file` dev toolkit (patch/read/write/search_files) from the conversational
    # lane, so it is no longer present even though the persona configured it.
    assert set(["search", "session_search", "todo", "skills"]).issubset(
        set(request.enabled_toolsets)
    )
    assert "file" not in request.enabled_toolsets

    assert len(captured["requests"]) == 1


def test_mission_chat_reply_has_no_api_call_cap_and_keeps_iteration_failsafe(
    tmp_path, monkeypatch
):
    # Chat lane matches base Hermes: a turn is bounded by the tool-calling loop
    # (max_iterations=90) + wall clock, NOT a hard api-call count. The old
    # max_api_calls=8 also throttled the operator's own multi-step chat requests
    # (operator chat and agent_chat_send relays share this path), so it was
    # lifted. Guard against a silent re-introduction of the cap.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    from agent_runtime.models import AgentPersona

    profile = AgentPersona(
        id="profile:alice",
        display_name="Alice Agent",
        role="profile",
        model="gpt-5.5",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file", "search"],
        system_prompt_path="",
        hermes_profile=None,
    )
    captured = {}

    class CapturingRunner:
        def run(self, request):
            captured["request"] = request
            return AgentRunResult(
                final_response="ok",
                session_id="s",
                provider="openai-codex",
                model="gpt-5.5",
                base_url=None,
                messages=[],
            )

    runtime = GPTPersonaRuntime(
        default_provider="openai-codex",
        default_model="gpt-5.5",
        agent_runner=CapturingRunner(),
    )
    runtime.mission_chat_reply(
        profile, "read three files and summarize", permission_session_id="s"
    )

    request = captured["request"]
    assert request.max_api_calls is None
    assert request.max_iterations == 90


def test_mission_chat_reply_honors_core_context_file_opt_in(tmp_path, monkeypatch):
    # Regression: the operator chat path used to hardcode skip_context_files=False,
    # forcing the process-cwd repo project docs (e.g. the 72KB hermes-agent
    # AGENTS.md, truncated to ~65K chars) into EVERY conversational turn — ~20K
    # tokens of fixed overhead regardless of persona. Operator chat must honor the
    # persona's include_core_context_files opt-in exactly like the mission-run
    # (skip_context_files) and free-chat paths. Isolated personas (the default)
    # stay lean; only an explicit opt-in loads repo docs.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    from agent_runtime.models import AgentPersona

    def _capture_skip(include_core: bool) -> bool:
        persona = AgentPersona(
            id="profile:alice",
            display_name="Alice Agent",
            role="profile",
            model="gpt-5.5",
            provider="openai-codex",
            api_mode="codex_responses",
            toolsets=["file", "search", "skills"],
            system_prompt_path="",
            hermes_profile=None,
            include_core_context_files=include_core,
        )
        captured = {}

        class CapturingRunner:
            def run(self, request):
                captured["request"] = request
                return AgentRunResult(
                    final_response="ok",
                    session_id="s",
                    provider="openai-codex",
                    model="gpt-5.5",
                    base_url=None,
                    messages=[],
                )

        runtime = GPTPersonaRuntime(
            default_provider="openai-codex", default_model="gpt-5.5", agent_runner=CapturingRunner()
        )
        runtime.mission_chat_reply(persona, "hi", permission_session_id="s")
        return captured["request"].skip_context_files

    # Default isolated persona -> context files skipped (lean turn).
    assert _capture_skip(False) is True
    # Explicit opt-in -> context files loaded (parity with the mission-run path).
    assert _capture_skip(True) is False


def test_chat_reply_without_session_records_no_trace(tmp_path, monkeypatch):
    # A sandbox chat turn with no durable session must not synthesize trace.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    FakeAIAgent.instances.clear()
    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")

    class ToolCallingAgent(FakeAIAgent):
        def run_conversation(self, *, user_message, system_message, task_id):
            # The runner still wraps a no-op budget callback; driving it must not
            # synthesize any trace because there is no session to key on.
            self.kwargs["tool_start_callback"]("tool.started", "terminal", "echo hi")
            return super().run_conversation(
                user_message=user_message, system_message=system_message, task_id=task_id
            )

    runtime = GPTPersonaRuntime(
        default_provider="openai-codex", default_model="gpt-5.5", agent_factory=ToolCallingAgent
    )

    runtime.chat_reply(neko, "hi", session_id=None)

    assert EventLog().tail(10) == []


def test_llm_timing_records_repeated_profile_attempts_without_losing_totals():
    _, run = make_task_and_run()
    first = AgentRunResult(
        final_response="{}",
        session_id="session_1",
        provider="openai-codex",
        model="gpt-5.5",
        base_url="https://chatgpt.com/backend-api/codex",
        messages=[],
        latency_ms=100,
        profile_timing={"conversation_call_ms": 90, "provider_stream_event_count": 4},
    )
    second = AgentRunResult(
        final_response="{}",
        session_id="session_1",
        provider="openai-codex",
        model="gpt-5.5",
        base_url="https://chatgpt.com/backend-api/codex",
        messages=[],
        latency_ms=60,
        profile_timing={"conversation_call_ms": 50, "provider_stream_event_count": 7},
    )

    _apply_llm_metadata(run, first, timing={"provider_call_ms": 110})
    _apply_llm_metadata(run, second, timing={"provider_call_ms": 70})

    timing = run.llm["timing"]
    assert timing["provider_call_ms"] == 70
    assert timing["provider_call_count"] == 2
    assert timing["provider_call_total_ms"] == 180
    assert timing["provider_call_max_ms"] == 110
    assert timing["profile_conversation_call_ms"] == 50
    assert timing["profile_conversation_call_count"] == 2
    assert timing["profile_conversation_call_total_ms"] == 140
    assert timing["profile_conversation_call_max_ms"] == 90
    assert timing["profile_provider_stream_event_count"] == 11








def test_dev_grounds_in_task_affected_repo_without_stage_graph():
    from agent_runtime import persona_runtime as pr

    task, run = make_task_and_run()
    task.affected_repos = ["hermes-agent"]
    dev = next(persona for persona in sample_personas() if persona.id == "dev")
    dev.repo_scope = None
    ctx = build_context(task, run)

    repo_ctx = pr._repo_context_for_persona(dev, ctx)

    harness_root = Path(pr.__file__).resolve().parents[1]
    assert repo_ctx is not None
    assert repo_ctx.workdir == harness_root
















def test_dev_grounding_honors_compatible_persona_scope_without_blueprint_placeholder():
    from agent_runtime.models import AgentPersona
    from agent_runtime import persona_runtime as pr

    task, run = make_task_and_run()
    task.affected_repos = ["hermes-agent"]
    harness_root = Path(pr.__file__).resolve().parents[1]
    persona = AgentPersona(
        id="dev_scoped",
        display_name="Scoped Dev",
        role="dev",
        model="stub",
        provider="stub",
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
        repo_scope=str(harness_root),
    )
    ctx = build_context(task, run)

    repo_ctx = pr._repo_context_for_persona(persona, ctx)
    assert repo_ctx is not None
    assert repo_ctx.workdir == harness_root


def test_mission_chat_reply_sets_cache_scope_id_but_keeps_session_none(tmp_path, monkeypatch):
    # T10c: the persona-chat lane must feed the STABLE chat session id as the
    # header-only cache_scope_id (so the codex cache-scope headers stay stable
    # across turns) WHILE keeping session_id=None (so the runtime never re-loads
    # the transcript it already baked into the message). The two ids must not be
    # conflated — cache_scope_id is a routing value, session_id is the
    # transcript/session-load key.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")
    captured = {}

    class CapturingRunner:
        def run(self, request):
            captured["request"] = request
            return AgentRunResult(
                final_response="ok",
                session_id="chat-neko-stable-1",
                provider="openai-codex",
                model="gpt-5.5",
                base_url=None,
                messages=[],
            )

    runtime = GPTPersonaRuntime(
        default_provider="openai-codex", default_model="gpt-5.5", agent_runner=CapturingRunner()
    )

    runtime.mission_chat_reply(
        neko,
        "status?",
        session_id=None,
        permission_session_id="chat-neko-stable-1",
    )

    request = captured["request"]
    # The header-only cache scope is the stable chat session identity…
    assert request.cache_scope_id == "chat-neko-stable-1"
    # …and the transcript/session-load key is left None (no re-bake).
    assert request.session_id is None


def test_chat_reply_threads_cache_scope_id_to_run_request(tmp_path, monkeypatch):
    # T10c follow-up: the FREE-FLOATING lane (operator console chats +
    # agent_chat relays) calls chat_reply with session_id=None while holding a
    # bound stable chat session id. chat_reply must thread that id through as
    # the header-only cache_scope_id — this lane was cache-cold on every turn
    # without it (live-observed: fmi.session_id='' and cache_read=0 post-T10c).
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")
    captured = {}

    class CapturingRunner:
        def run(self, request):
            captured["request"] = request
            return AgentRunResult(
                final_response="ok",
                session_id="persona_chat_personainst_neko_free_1",
                provider="openai-codex",
                model="gpt-5.5",
                base_url=None,
                messages=[],
            )

    runtime = GPTPersonaRuntime(
        default_provider="openai-codex", default_model="gpt-5.5", agent_runner=CapturingRunner()
    )

    runtime.chat_reply(
        neko,
        "hi",
        session_id=None,
        cache_scope_id="persona_chat_personainst_neko_free_1",
    )

    request = captured["request"]
    assert request.cache_scope_id == "persona_chat_personainst_neko_free_1"
    assert request.session_id is None


def test_chat_reply_cache_scope_defaults_to_none(tmp_path, monkeypatch):
    # Callers that pass a REAL session_id (transcript-loading chat lanes) get
    # header routing via session_id at the transport seam; chat_reply must not
    # invent a scope for them — absent stays absent.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")
    captured = {}

    class CapturingRunner:
        def run(self, request):
            captured["request"] = request
            return AgentRunResult(
                final_response="ok",
                session_id="s2",
                provider="openai-codex",
                model="gpt-5.5",
                base_url=None,
                messages=[],
            )

    runtime = GPTPersonaRuntime(
        default_provider="openai-codex", default_model="gpt-5.5", agent_runner=CapturingRunner()
    )

    runtime.chat_reply(neko, "hi", session_id="real-session-7")

    request = captured["request"]
    assert request.cache_scope_id is None
    assert request.session_id == "real-session-7"


def test_mission_chat_reply_cache_scope_falls_back_to_session_when_no_perm(tmp_path, monkeypatch):
    # perm_session_id = permission_session_id or session_id. When only session_id
    # is supplied (no separate permission id), the cache scope still resolves to
    # that same stable chat id — never left empty on the chat lane.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")
    captured = {}

    class CapturingRunner:
        def run(self, request):
            captured["request"] = request
            return AgentRunResult(
                final_response="ok",
                session_id="s",
                provider="openai-codex",
                model="gpt-5.5",
                base_url=None,
                messages=[],
            )

    runtime = GPTPersonaRuntime(
        default_provider="openai-codex", default_model="gpt-5.5", agent_runner=CapturingRunner()
    )

    runtime.mission_chat_reply(neko, "status?", session_id="chat-only-2")

    assert captured["request"].cache_scope_id == "chat-only-2"
