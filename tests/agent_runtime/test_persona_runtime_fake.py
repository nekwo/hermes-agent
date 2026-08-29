import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from hermes_time import now

from agent_runtime.events import EventLog
from agent_runtime.models import AgentRun, Event
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.persona_runtime import GPTPersonaRuntime
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

    runtime.mission_chat_reply(qa, "can you write now?", permission_session_id=session_id)

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

    runtime.mission_chat_reply(neko, "run the command", permission_session_id=session_id)

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

    runtime.mission_chat_reply(neko, "hi", session_id=None)

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
    runtime.mission_chat_reply(
        neko,
        "run echo PARITY_OK_2026 and paste the output",
        permission_session_id=session_id,
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
        assert "clarify" in blocked_tool_names()


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


def test_mission_chat_operative_rules_treat_a_clear_order_as_the_go_ahead():
    # 2026-08-03 live regression: told to message the agents it steers, Neko
    # ended its turn on a bare "Waiting for your go-ahead before I send the
    # messages." — no tool calls, no plan restated, and NO harness approval gate
    # involved (verified in logs; the pause was entirely model-side). Two halves
    # must survive here: a clear non-destructive order is itself the go-ahead,
    # and any pause that IS earned has to restate a concrete plan — so a bare
    # hold is never the shape of a compliant turn. The ack rule stays first.
    from agent_runtime.persona_runtime import _mission_chat_operative_rules

    rules = _mission_chat_operative_rules()
    bullets = [line for line in rules.splitlines() if line.startswith("- ")]
    assert "HARD RULE" in bullets[0], "the acknowledge-before-acting rule must remain first"

    go_ahead = next((line for line in bullets if "IS the go-ahead" in line), None)
    assert go_ahead is not None, "operative rules must name a clear order as the go-ahead"
    assert "SAME turn" in go_ahead
    assert "Never end a turn asking permission" in go_ahead
    # The pause is carved out to the two cases that earn it — not to side effects.
    assert "destructive or irreversible" in go_ahead
    assert "`clarify`" in go_ahead

    restate = next((line for line in bullets if "waiting for your go-ahead" in line), None)
    assert restate is not None, "a pause must be required to restate its concrete plan"
    assert "state " in restate and "concretely" in restate
    assert "never acceptable" in restate

    # The clarify bullet must not read as a blanket "on this channel, ask" license.
    clarify = next(
        line for line in bullets if "use the `clarify` tool to ask before acting" in line
    )
    assert "here, ask." not in clarify
    assert "never for permission to carry out a clear order" in clarify


def test_mission_chat_operative_rules_own_the_confirm_back_scope():
    # The confirm-back the operator actually wants, stated HERE because this is
    # the single authority for it. Two halves, and both have drawn blood:
    #   * the pause has a third earned case beyond destructive/ambiguous — a
    #     technical or multi-step task where the agent filled in a detail the
    #     operator never stated. Its purpose is to prove COMPREHENSION, so it
    #     restates the plan; it is not a permission request.
    #   * a complete, unambiguous instruction is NOT that case even when it has
    #     side effects. Relaying a dictated one-liner behind a "go ahead?" is
    #     the friction that started this (2026-08-10 operator report).
    from agent_runtime.persona_runtime import _mission_chat_operative_rules

    rules = _mission_chat_operative_rules()
    bullets = [line for line in rules.splitlines() if line.startswith("- ")]
    assert "HARD RULE" in bullets[0], "the acknowledge-before-acting rule must remain first"

    go_ahead = next(line for line in bullets if "IS the go-ahead" in line)

    # The pause and its three earned cases.
    assert "exactly three cases" in go_ahead
    assert "destructive or irreversible" in go_ahead
    assert "technical or multi-step task" in go_ahead
    assert "substantive detail the operator did not state" in go_ahead
    # ...and what that third pause is FOR. Without this the rule reads as a
    # permission gate again, which is the behavior being retired.
    assert "prove you UNDERSTOOD the task" in go_ahead
    assert "never a permission request" in go_ahead

    # The act-directly carve-out, with the two shapes the operator named.
    assert "complete and unambiguous on its face" in go_ahead
    assert "relaying a message the operator dictated" in go_ahead
    assert "a new target they named" in go_ahead
    assert "never needs confirmation either" in go_ahead

    # A briefed agent is not on the operator channel; the brief authorizes it.
    assert "that brief is your authorization" in go_ahead


def test_operator_channel_permission_policy_has_exactly_one_layer():
    # SINGLE-AUTHORITY pin. The operator-channel system message is composed from
    # runtime-owned parts; only the operative rules may define confirm /
    # permission / go-ahead behavior. A future edit that teaches the identity
    # hat (or any sibling layer) its own permission rule reintroduces the
    # 2026-08-10 two-rules-one-prompt defect, so it fails here.
    from agent_runtime.persona_runtime import (
        _mission_chat_identity_prompt,
        _mission_chat_operative_rules,
    )

    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")
    vocabulary = ("go-ahead", "go ahead", "permission", "confirmation", "approval")

    rules = _mission_chat_operative_rules()
    assert [word for word in vocabulary if word in rules], (
        "the operative rules must be the layer that DOES define this policy — "
        "if this fails the pin below is vacuous"
    )

    identity = _mission_chat_identity_prompt(neko)
    leaked = [word for word in vocabulary if word in identity.lower()]
    assert not leaked, (
        "the runtime identity layer must not define operator-channel permission "
        f"policy; that belongs to _mission_chat_operative_rules() alone: {leaked}"
    )


def test_workspace_agents_may_not_redefine_operator_channel_policy():
    # The 2026-08-10 defect, pinned at the seam it actually happened at.
    #
    # The workspace file is ARBITRARY — whatever directory the operator aimed
    # the Mission Control picker at — so this cannot be an allowlist naming one
    # repo's AGENTS.md. The only thing knowable on this side is the boundary,
    # and the preamble states it once, ahead of the body: repo instructions
    # describe the repo and never govern this channel's confirmation behavior.
    from agent_runtime.persona_runtime import (
        MISSION_CHAT_WORKSPACE_AGENTS_PREAMBLE,
        _mission_chat_operative_rules,
        _mission_chat_surface_message,
    )

    neko = next(persona for persona in sample_personas() if persona.id == "neko_supervisor")

    # A workspace file shaped exactly like the one that actually shipped.
    hostile = (
        "# SomeRepo\n\n"
        "Before dispatching work to other agents, send one short message and "
        "WAIT for Tony's go-ahead — his reply is the approval."
    )
    composed = _mission_chat_surface_message(neko, "", workspace_agents_content=hostile)

    # Non-vacuity: the workspace body really is injected. If a future change
    # dropped it, every assertion below would pass for the wrong reason.
    assert "WAIT for Tony's go-ahead" in composed

    scope_sentence = (
        "wherever they touch confirmation, permission, or go-ahead behavior, the "
        "Mission Control operator-chat rules above are authoritative and win"
    )
    assert scope_sentence in composed
    assert scope_sentence in MISSION_CHAT_WORKSPACE_AGENTS_PREAMBLE

    # Ordering is the whole mechanism: the channel's own rules, then the scope
    # statement, then the arbitrary body. A scope statement that landed after
    # the body it scopes would be decoration.
    rules_at = composed.index(_mission_chat_operative_rules())
    scope_at = composed.index(scope_sentence)
    body_at = composed.index("WAIT for Tony's go-ahead")
    assert rules_at < scope_at < body_at

    # And the boundary is stated even when the workspace file is innocuous —
    # it is a property of the layer, not a reaction to its contents.
    benign = _mission_chat_surface_message(
        neko, "", workspace_agents_content="# SomeRepo\n\nRun the tests with `just test`."
    )
    assert scope_sentence in benign


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


def test_mission_chat_operative_rules_delegate_charsheet_authoring():
    # Owner ruling R-1 = option 1b, DELEGATION (not 1a, "give the supervisor the
    # skill"): docs/agent-runtime-harness/planned/charsheet-turn-efficiency-2026-08-29.md.
    #
    # Measured 2026-08-28 on the fire-imp one-shot: the supervisor drove the
    # whole 8-way pipeline itself, on the expensive model, WITHOUT the authoring
    # skill in context — 27 API calls, 36 tool elements (72% of them
    # rediscovery / environment archaeology / redundant state reads), 1.556M
    # cumulative prompt tokens, 19.7 min — and then shipped a reply carrying
    # ZERO `MEDIA:` and ZERO `CHARSHEET-QA:` lines, costing a whole remediation
    # turn to show the operator the character that was already installed. It had
    # already dispatched the authoring specialist for standby QA in that same
    # turn and then kept the pipeline anyway; the fix is posture, not preloads.
    #
    # Pin the RULE, not the paragraph. Four halves have to survive any rewrite:
    # recognize the ask, dispatch it BY CAPABILITY (never a memorized instance
    # id — the live specimen instance is disposable), relay the receipt lines
    # verbatim, and do not drive the `characters` verbs. Plus the self-exemption:
    # without it this same channel-wide rule would tell the authoring agent to
    # delegate its own job.
    from agent_runtime.persona_runtime import _mission_chat_operative_rules

    rules = _mission_chat_operative_rules()
    bullets = [line for line in rules.splitlines() if line.startswith("- ")]
    assert "HARD RULE" in bullets[0], "the acknowledge-before-acting rule must remain first"

    delegation = next(
        (line for line in bullets if "charsheet authoring skill" in line),
        None,
    )
    assert delegation is not None, "operative rules must make charsheet authoring a delegation"

    # 1. Recognize the ask by the operator's own verbs, not one keyword.
    for verb in ("make", "fix", "resume", "add a state", "install"):
        assert verb in delegation, f"the authoring ask must be recognizable by {verb!r}"
    assert "sprite sheet" in delegation

    # 2. Dispatch by capability, through the machinery that already exists. A
    #    persona id or an @personainst_ id baked into the prompt would rot the
    #    moment the operator places a different authoring agent.
    assert "agent_chat_send" in delegation
    assert "carries the charsheet authoring skill" in delegation
    assert "chara_a2" not in rules, "the rules must not hardcode the live specimen instance"
    assert "personainst_chara" not in rules

    # 3. The receipts ARE the deliverable, and they pass through untouched.
    assert "MEDIA:" in delegation
    assert "CHARSHEET-QA:" in delegation
    assert "verbatim" in delegation

    # 4. Don't drive the pipeline yourself...
    assert "hermes harness characters" in delegation
    # ...unless you are the one holding the skill.
    assert "you are that specialist" in delegation

    # The verbatim-relay half must land BEFORE the image-line carve-out it
    # leans on, or the cross-reference points backwards.
    media_carveout = next(line for line in bullets if "One carve-out to that" in line)
    assert rules.index(delegation) < rules.index(media_carveout)


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

    # Select the carve-out by its own opening, not by "MEDIA:" alone: the
    # charsheet delegation bullet above also names MEDIA:/CHARSHEET-QA: when it
    # tells the relay what to pass through, so a bare substring match now picks
    # the wrong bullet.
    media_bullet = next((line for line in bullets if "One carve-out to that" in line), None)
    assert media_bullet is not None, "operative rules must teach the MEDIA-verbatim relay"
    assert "MEDIA:" in media_bullet
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


def test_mission_chat_behavior_is_role_agnostic_and_profile_owned():
    """Changing only the legacy role label cannot change prompt bytes.

    Identity still names the selected instance, while behavioral voice comes
    only from the profile-owned SOUL/config layers.
    """

    from dataclasses import replace

    from agent_runtime.persona_runtime import _mission_chat_surface_message

    base = next(persona for persona in sample_personas() if persona.id == "qa")
    custom = replace(base, role="operator_authored_custom_role")
    dev_labeled = replace(base, role="dev")

    custom_prompt = _mission_chat_surface_message(custom, "")
    assert custom_prompt == _mission_chat_surface_message(dev_labeled, "")
    assert "quality gate" not in custom_prompt.lower()
    assert "senior engineer" not in custom_prompt.lower()
    assert "chief-of-staff" not in custom_prompt.lower()


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
    # Under the 2026-08-09 runtime default (`unbounded`) the turn resolves the
    # FULL registry — the T6a cost policy that used to drop the `file` dev
    # toolkit applies only to a restricted session now.
    # (``search`` is service-gated and simply not registered without a web API
    # key, which is a capability fact rather than a permission one.)
    assert set(["session_search", "todo", "skills", "file", "terminal"]).issubset(
        set(request.enabled_toolsets)
    )
    # The mode reaches the EXECUTION plane too: the envelope scope this turn
    # binds carries it, which is what makes the gated command classes grantable
    # by mode (with a receipt) instead of needing a per-role config stanza.
    assert request.terminal_envelope_scope.permission_mode == "unbounded"
    assert request.blocked_tool_names == []

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


def test_mission_chat_reply_without_session_records_no_trace(tmp_path, monkeypatch):
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

    runtime.mission_chat_reply(neko, "hi", session_id=None)

    assert EventLog().tail(10) == []


# S29: two tests lived here -- test_dev_grounds_in_task_affected_repo_without_
# stage_graph and test_dev_grounding_honors_compatible_persona_scope_without_
# blueprint_placeholder. Both exercised persona_runtime._repo_context_for_persona,
# the WORKER lane's repo grounding, by constructing an AgentContext by hand after
# S27 removed the builder that used to produce one. With no producer and no
# caller the helper was removed, so both tests exercised removed behavior; the
# repo-resolution behavior they covered is asserted at its own boundary in
# tests/agent_runtime/test_repo_context.py. See
# tests/agent_runtime/test_s29_persona_runtime_context_lane_removal.py.


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
