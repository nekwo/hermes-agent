"""CLI contract for a wall-budget-ended mission-chat turn.

The 2026-07-26 incident cost an operator a manual ``turn-resolve --action
abandon`` on BOTH ends of a relay plus a full re-brief, because a wall-budget
death was reported as ``chat_turn_outcome_unknown`` — the "I cannot prove what
the provider did" state. A budget death is the opposite: the harness knows
exactly why it stopped. These guards pin the resulting contract so a later edit
cannot quietly reintroduce the resolve instruction or drop the typed fields.

**Executable, not source-shaped (2026-07-31).** Six guards here used to
``ast.parse`` the command part and assert on dict-literal STRINGS, because
``persona_commands.py`` is an ``exec``'d command part rather than an importable
module. That was a workaround for a missing seam, and it carried the usual cost
of one: it pinned the SPELLING of a literal, not the BEHAVIOUR, so it passed
just as happily against a lane that never reached the payload at all. Now that
the vocabulary is owned by ``agent_runtime.mission_chat_outcome`` and the
decision is the pure ``classify_turn_failure``, every row below DRIVES the real
handler and reads the emitted envelope plus the persisted turn record.

What could NOT move is ``test_the_enforced_window_comes_from_the_turn_context_budget``
at the bottom: the runner's clamp is computed in THAT body, so "the runner
enforces the same object the agent was told about" is a property of the source
and stays an AST assertion.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime.mission_chat_outcome import ChatErrorKind, ExecutionState

# Imperative resolve language — the copy that sent the 2026-07-26 operator to
# `turn-resolve --action abandon`. A budget-ended turn must never carry any of
# it. Mentioning the verb only to NEGATE it ("no turn-resolve is required") is
# the point of the fix, so the verb itself is checked separately below.
_RESOLVE_INSTRUCTIONS = (
    "resolve the exact",
    "resolve this turn",
    "action=abandon",
    "before resending",
)
_RESOLVE_VERBS = ("turn-resolve", "turn_resolve")
_NEGATIONS = (
    "no turn-resolve",
    "no turn_resolve",
    "needs no",
    "requires no",
    "without turn-resolve",
)

_SESSION_ID = "persona_chat_personainst_dev"


@pytest.fixture
def isolate_agent_runtime_root(tmp_path, monkeypatch):
    """Local twin of the ``tests/agent_runtime`` conftest fixture.

    These rows drive real store writes (persona rows, the turn journal), so they
    need a throwaway runtime root the same way the agent_runtime suite does.
    """

    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    yield root


# --------------------------------------------------------------------------- #
# Driving the real lane                                                        #
# --------------------------------------------------------------------------- #
def _args(client_message_id: str):
    return SimpleNamespace(
        persona_id="dev",
        persona_instance_id="personainst_dev",
        session_id=_SESSION_ID,
        message="please answer",
        surface_prompt="",
        intent_hint="chat",
        requested_by="test",
        client_message_id=client_message_id,
        stream=False,
        max_seconds=5.0,
        json=True,
    )


class _TranscriptDB:
    """Minimal SessionDB stand-in: enough for the chat lane's reads and writes.

    Deliberately NOT canonical persistence (no
    ``__hermes_canonical_session_persistence__``, not defined in ``hermes_state``),
    so the ``unknown_chat_session`` / ``foreign_chat_session`` guards correctly
    decline to refuse against it — see
    ``agent_runtime.chat_session_scope.is_canonical_session_persistence``. It
    also carries no catch-all ``__getattr__`` for exactly that reason: one would
    answer that marker truthily and silently turn every row here into an
    admission refusal.
    """

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list] = {}
        self.titles: dict[str, str | None] = {}

    def create_session(self, session_id, source, **kwargs):
        self.sessions.setdefault(session_id, {"source": source, **kwargs})
        self.messages.setdefault(session_id, [])
        return session_id

    def get_session(self, session_id):
        session = self.sessions.get(session_id)
        if session is None:
            return None
        return {
            "id": session_id,
            "source": session.get("source"),
            "system_prompt": session.get("system_prompt"),
            "model": session.get("model"),
            "model_config": session.get("model_config"),
            "title": self.titles.get(session_id),
            "preview": None,
            "message_count": len(self.messages.get(session_id, [])),
            "started_at": None,
            "last_active": None,
            "archived": 0,
        }

    def append_message(self, session_id, role, content=None, **kwargs):
        self.messages.setdefault(session_id, []).append(
            {"role": role, "content": content, **kwargs}
        )
        return len(self.messages[session_id])

    def get_messages(self, session_id, include_inactive=False):
        return list(self.messages.get(session_id, []))

    def get_session_title(self, session_id):
        return self.titles.get(session_id)

    def set_session_title(self, session_id, title):
        self.titles[session_id] = title

    def update_session_meta(self, session_id, model_config_json, model=None):
        session = self.sessions.setdefault(session_id, {})
        session["model_config"] = model_config_json
        if model is not None:
            session["model"] = model


def _seed(monkeypatch, provider):
    """Wire the handler onto a hermetic store plus the given provider."""

    from agent_runtime.config import AgentRuntimeConfig
    from agent_runtime.personas import AgentPersona
    from agent_runtime.store import AgentStore
    from hermes_cli import harness

    AgentStore().save(
        AgentPersona(
            id="dev",
            display_name="dev worker",
            role="dev",
            model="gpt-test",
            provider="openai-codex",
            api_mode="codex_responses",
            toolsets=["file", "search", "terminal"],
            system_prompt_path="agent_runtime/prompts/dev.md",
            hermes_profile="profile-dev",
        )
    )
    # S56: this used to flip `enterprise_worker_sessions` on so the persona
    # roster would project. The roster is unconditional now and the block is
    # gone, so a bare config is the same fixture.
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: AgentRuntimeConfig())
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: _TranscriptDB())
    monkeypatch.setattr(harness, "GPTPersonaRuntime", provider)
    return harness


def _wall_budget_provider(wall_budget):
    from agent_runtime.profile_runner import RunBudgetExceeded

    class _Provider:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, *args, **kwargs):
            raise RunBudgetExceeded(
                "wall budget exhausted after 240s", wall_budget=wall_budget
            )

    return _Provider


_WALL_BUDGET = {
    "trigger": "wall_budget_hard_wall",
    "total_seconds": 240,
    "remaining_at_checkpoint_seconds": 0,
}


@pytest.fixture
def budget_envelope(monkeypatch, capsys, isolate_agent_runtime_root):
    """Drive one real wall-budget death and hand back its emitted envelope."""

    harness = _seed(monkeypatch, _wall_budget_provider(_WALL_BUDGET))
    code = harness._cmd_mission_chat_message(_args("budget_turn"))
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def spent_envelope(monkeypatch, capsys, isolate_agent_runtime_root):
    """...then RESEND that same id, which finds the settled record."""

    harness = _seed(monkeypatch, _wall_budget_provider(_WALL_BUDGET))
    assert harness._cmd_mission_chat_message(_args("budget_turn")) == 2
    capsys.readouterr()
    code = harness._cmd_mission_chat_message(_args("budget_turn"))
    return code, json.loads(capsys.readouterr().out)


# --------------------------------------------------------------------------- #
# The contract, as executable rows                                             #
# --------------------------------------------------------------------------- #
def test_a_wall_budget_death_emits_the_typed_budget_payload(budget_envelope):
    code, payload = budget_envelope
    assert code == 2
    assert payload["ok"] is False
    assert payload["execution_state"] == ExecutionState.BUDGET_EXHAUSTED
    assert payload["error_kind"] == ChatErrorKind.CHAT_TURN_BUDGET_EXHAUSTED


def test_a_resend_of_a_spent_id_reports_the_same_typed_pair(spent_envelope):
    """The settled-record refusal and the live death agree on the vocabulary."""

    code, payload = spent_envelope
    assert code == 2
    assert payload["execution_state"] == ExecutionState.BUDGET_EXHAUSTED
    assert payload["error_kind"] == ChatErrorKind.CHAT_TURN_BUDGET_EXHAUSTED
    assert payload["journal_state"] == "budget_exhausted"


@pytest.mark.parametrize("envelope", ["budget_envelope", "spent_envelope"])
def test_budget_payloads_declare_that_no_resolution_is_required(envelope, request):
    _, payload = request.getfixturevalue(envelope)
    assert payload["turn_resolution_required"] is False
    assert payload["budget_exhausted"] is True
    assert "new client_message_id" in payload["next_expected"]


@pytest.mark.parametrize("envelope", ["budget_envelope", "spent_envelope"])
def test_budget_payloads_never_tell_the_operator_to_turn_resolve(envelope, request):
    _, payload = request.getfixturevalue(envelope)
    texts = [value for value in payload.values() if isinstance(value, str)]
    assert texts
    for text in texts:
        lowered = text.lower()
        for phrase in _RESOLVE_INSTRUCTIONS:
            assert phrase not in lowered, (
                "a budget-ended turn is settled: it must never route the "
                f"operator to a resolution verb (found {phrase!r} in {text!r})"
            )
        if any(verb in lowered for verb in _RESOLVE_VERBS):
            assert any(negation in lowered for negation in _NEGATIONS), (
                "a budget-ended payload may name turn-resolve only to say it "
                f"is NOT needed (found an unnegated mention in {text!r})"
            )


def test_the_budget_settle_writes_the_typed_terminal_state_not_outcome_unknown(
    budget_envelope,
):
    """The journal RECORD — not the source text — is the proof.

    ``budget_exhausted`` is terminal and needs no operator resolution, so the
    turn store must hold exactly that. ``outcome_unknown`` here IS the incident.
    """

    from agent_runtime.mission_chat_turns import mission_chat_turn_record

    record = mission_chat_turn_record(
        session_id=_SESSION_ID, client_message_id="budget_turn"
    )
    assert record is not None
    assert record["state"] == "budget_exhausted"
    assert record["provider_submitted"] is True
    assert record["budget_exhausted"] is True


def test_a_non_wall_budget_trip_stays_genuinely_ambiguous(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """The narrowing must not swallow the state it narrowed AWAY from.

    An api-call / token / read-search budget trip carries no ``wall_budget``
    projection, so the harness genuinely cannot prove what the provider did —
    that turn still settles at ``outcome_unknown`` and still routes the operator
    to ``turn-resolve``.
    """

    from agent_runtime.mission_chat_turns import mission_chat_turn_record

    harness = _seed(monkeypatch, _wall_budget_provider(None))
    code = harness._cmd_mission_chat_message(_args("api_budget_turn"))
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["execution_state"] == ExecutionState.BLOCKED
    assert payload["error_kind"] == ChatErrorKind.CHAT_TURN_OUTCOME_UNKNOWN
    assert "resolve the exact" in payload["next_expected"]
    record = mission_chat_turn_record(
        session_id=_SESSION_ID, client_message_id="api_budget_turn"
    )
    assert record["state"] == "outcome_unknown"


def test_a_pre_boundary_failure_is_retryable_on_the_same_id(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """Nothing crossed the provider boundary, so nothing needs resolving.

    The boundary is the ``executing`` journal transition, not the provider call
    — everything from that transition on counts as submitted. Failing the
    pre-turn repair sweep puts the failure unambiguously on the near side.
    """

    harness = _seed(monkeypatch, _wall_budget_provider(_WALL_BUDGET))
    monkeypatch.setattr(
        harness,
        "mark_stale_inflight_turns_interrupted",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("sweep exploded")),
    )
    code = harness._cmd_mission_chat_message(_args("pre_boundary_turn"))
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["execution_state"] == ExecutionState.FAILED
    assert payload["error_kind"] == ChatErrorKind.CHAT_TURN_NOT_SUBMITTED
    assert "retry this client_message_id" in payload["next_expected"]


# --------------------------------------------------------------------------- #
# Budget WIRING — a property of this source, not of any envelope               #
# --------------------------------------------------------------------------- #
# Budget VISIBILITY (``turn_budget`` on the HUD dict for the operator's CONTEXT
# peek, and the budget line on the always-emitted volatile tail for the agent)
# moved into ``agent_runtime.mission_chat_turn_context`` and is asserted on the
# composed OUTPUT in tests/agent_runtime/test_mission_chat_turn_context.py —
# ``test_wall_budget_and_capability_ride_the_tail_and_the_hud_but_never_the_body``
# and ``test_the_tail_is_emitted_on_every_delivery``.
#
# What CANNOT move is the wiring below: the enforcer's window is computed in
# this body, so "the runner enforces the same object the agent was told about"
# is a property of THIS source.
# Split on 2026-07-31: the plan phase kept the name ``_cmd_mission_chat_message``
# and every durable write — including arming the runner's wall clamp — moved into
# ``_mission_chat_commit_turn``, the sole writer under the chat-root lease. Both
# halves are searched so this guard follows the code if the boundary moves again,
# rather than silently finding nothing and passing.
_TURN_BODY_FUNCTIONS = ("_mission_chat_commit_turn", "_cmd_mission_chat_message")


def _mission_chat_message_func() -> ast.FunctionDef:
    import hermes_cli.harness as harness

    path = Path(harness.__file__).with_name("harness_parts") / "persona_commands.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for name in _TURN_BODY_FUNCTIONS:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
    raise AssertionError(
        "the mission-chat turn body "
        f"({' / '.join(_TURN_BODY_FUNCTIONS)}) is not in persona_commands"
    )


def test_the_enforced_window_comes_from_the_turn_context_budget():
    """One authority, across the extraction boundary.

    The agent's budget line is rendered from ``turn_context.wall_budget``; the
    runner's clamp must be armed from that SAME object, never from a second
    resolve in this body. The pre-fix code recomputed the relay clamp inline and
    the two numbers drifted.
    """

    func = _mission_chat_message_func()

    # No second resolve in the CLI body...
    assert not [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and (
            node.func.id
            if isinstance(node.func, ast.Name)
            else getattr(node.func, "attr", None)
        )
        == "resolve_turn_wall_budget"
    ]

    # ...and `wall_budget` is bound to the built context, once.
    bindings = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "wall_budget"
            for target in node.targets
        )
    ]
    assert len(bindings) == 1
    value = bindings[0].value
    assert isinstance(value, ast.Attribute)
    assert isinstance(value.value, ast.Name) and value.value.id == "turn_context"
    assert value.attr == "wall_budget"

    # The relative window handed to the runner is derived from that object.
    relay_wall = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "relay_wall_seconds"
            for target in node.targets
        )
    ]
    assert relay_wall, "the runner's wall window is no longer computed here"
    assert "wall_budget" in ast.dump(relay_wall[0])
