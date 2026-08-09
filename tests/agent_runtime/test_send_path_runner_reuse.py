"""What the runner stops re-paying per send, and what it now records honestly.

Three seams, all measured on 2026-08-09 and all on the critical path of every
mission-chat turn:

* **T4** — ``final_model_input`` captured the COMPOSED user message while the
  persona-chat boundary rewrote the live actor's row in place before the first
  provider call. A record that cannot tell you what was sent is how the 20k
  amputation stayed invisible; these pin that it now reads the agent's own row
  and says which copy it got.
* **T3** — an agent was constructed (~1.5 s) and thrown away on every warm send.
* **T6** — the runtime was re-resolved (~0.3 s) for an unchanged profile.
"""

from __future__ import annotations

import time

import pytest

from agent_runtime.persona_chat_continuity import PersonaChatRuntimeRegistry
from agent_runtime.profile_runner import (
    RUNTIME_RESOLVE_CACHE_TTL_SECONDS,
    AgentRunRequest,
    ProfileAgentRunner,
    _runtime_resolve_cache_key,
    reset_runtime_resolve_cache,
)


class _BoundaryAgent:
    """A fake that rewrites its own user row the way the real boundary does.

    ``run_agent._flush_messages_to_session_db`` runs BEFORE the first API call
    and does ``msg.clear(); msg.update(native)`` on the dict that lives in
    ``agent.messages`` — so the live actor's row, not ``request.user_message``,
    is what the provider sees. This reproduces exactly that: same dict, mutated
    in place, after the composed text was staged.
    """

    #: What the boundary leaves behind. Deliberately a DIFFERENT length from any
    #: composed message a test feeds in, so "the record captured the wire" and
    #: "the record captured the composition" cannot be the same assertion.
    wire_text = "operator asks\n\n<skill_preload skills=\"qa\">bounded</skill_preload>"

    constructed = 0

    def __init__(self, **kwargs):
        type(self).constructed += 1
        self.kwargs = kwargs
        self.session_id = kwargs.get("session_id") or "session_fake"
        self.provider = kwargs.get("provider")
        self.model = kwargs.get("model")
        self.base_url = "https://example.invalid/v1"
        self.tools = []
        self.messages = []
        self._persist_user_message_idx = None
        self.status_callback = kwargs.get("status_callback")
        self.max_iterations = kwargs.get("max_iterations")
        self.cache_scope_id = kwargs.get("cache_scope_id")
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tool_start_callback = kwargs.get("tool_start_callback")
        self.tool_complete_callback = kwargs.get("tool_complete_callback")
        self.clarify_callback = kwargs.get("clarify_callback")

    def run_conversation(self, user_message, system_message=None, task_id=None, **kwargs):
        row = {"role": "user", "content": user_message}
        self.messages = [row]
        self._persist_user_message_idx = 0
        # The boundary, in place, before the "provider call".
        row["content"] = self.wire_text
        # Fire the turn's tool-progress lane from INSIDE the run, which is the
        # only window it is attached: `_finish_resident_persona_chat_agent`
        # detaches every callback afterwards, so a resident's handles read None
        # from the outside whether or not they were ever refreshed.
        if self.tool_start_callback is not None:
            self.tool_start_callback("tool.started", "terminal")
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


class _NoRowAgent(_BoundaryAgent):
    """An agent that never stages a current-turn row (a non-chat lane)."""

    def run_conversation(self, user_message, system_message=None, task_id=None, **kwargs):
        result = super().run_conversation(
            user_message, system_message=system_message, task_id=task_id
        )
        self.messages = []
        self._persist_user_message_idx = None
        return result


@pytest.fixture(autouse=True)
def _reset_constructed():
    _BoundaryAgent.constructed = 0
    yield
    _BoundaryAgent.constructed = 0


@pytest.fixture
def stub_runtime(monkeypatch):
    calls: list[tuple[str, str]] = []

    def _resolve(requested, target_model):
        calls.append((requested, target_model))
        return {
            "provider": requested,
            "model": target_model,
            "api_mode": "codex_responses",
        }

    monkeypatch.setattr(
        "agent_runtime.profile_runner.resolve_runtime_provider", _resolve
    )
    return calls


def _request(**overrides) -> AgentRunRequest:
    fields = dict(
        profile=None,
        provider="openai-codex",
        model="gpt-5.6-luna",
        api_mode="codex_responses",
        session_id="session_1",
        user_message="operator asks\n\n<skill_preload skills=\"qa\">"
        + ("BIG " * 4000)
        + "</skill_preload>",
        system_message=None,
        task_id="turn_1",
    )
    fields.update(overrides)
    return AgentRunRequest(**fields)


# ---------------------------------------------------------------------------
# T4 — the record captures the wire, and names which copy it captured
# ---------------------------------------------------------------------------


def test_the_recorded_user_message_is_the_wire_copy_not_the_composition(stub_runtime):
    runner = ProfileAgentRunner(agent_factory=_BoundaryAgent)
    request = _request()

    result = runner.run(request)

    observability = result.raw["model_input_observability"]
    user_row = next(row for row in observability["messages"] if row["role"] == "user")
    assert user_row["content"] == _BoundaryAgent.wire_text
    assert user_row["content"] != request.user_message


def test_the_wire_receipt_reports_the_bound_that_happened(stub_runtime):
    runner = ProfileAgentRunner(agent_factory=_BoundaryAgent)
    request = _request()

    result = runner.run(request)

    receipt = result.raw["model_input_observability"]["user_message_wire"]
    assert receipt["source"] == "agent_wire"
    assert receipt["composed_chars"] == len(request.user_message)
    assert receipt["wire_chars"] == len(_BoundaryAgent.wire_text)
    assert receipt["bounded"] is True
    assert "unavailable_reason" not in receipt


def test_an_unreadable_wire_row_degrades_to_the_composition_and_says_so(stub_runtime):
    runner = ProfileAgentRunner(agent_factory=_NoRowAgent)
    request = _request()

    result = runner.run(request)

    observability = result.raw["model_input_observability"]
    receipt = observability["user_message_wire"]
    assert receipt["source"] == "request_composed"
    assert receipt["unavailable_reason"] == "no_turn_index"
    assert receipt["bounded"] is False
    user_row = next(row for row in observability["messages"] if row["role"] == "user")
    assert user_row["content"] == request.user_message


def test_a_turn_whose_wire_matched_its_composition_reports_no_bound(stub_runtime):
    class _Faithful(_BoundaryAgent):
        def run_conversation(self, user_message, system_message=None, task_id=None, **kw):
            result = super().run_conversation(
                user_message, system_message=system_message, task_id=task_id
            )
            self.messages[0]["content"] = user_message
            return result

    result = ProfileAgentRunner(agent_factory=_Faithful).run(_request())

    receipt = result.raw["model_input_observability"]["user_message_wire"]
    assert receipt["source"] == "agent_wire"
    assert receipt["bounded"] is False
    assert receipt["composed_chars"] == receipt["wire_chars"]


# ---------------------------------------------------------------------------
# T4 — the alarm rows the frame carries
# ---------------------------------------------------------------------------


def _budget(final_model_input, *, metered):
    from agent_runtime.prompt_observability import _context_budget

    return _context_budget(
        {"effective_model": "gpt-5.1", "effective_provider": "openai"},
        final_model_input,
        {"first_call_prompt_tokens": metered, "api_calls": 1},
    )


def _input(*, user_chars, composed_chars=None):
    payload = {
        "messages": [{"role": "user", "content": "u" * user_chars, "bytes": user_chars}],
        "message_count": 1,
    }
    if composed_chars is not None:
        payload["user_message_wire"] = {
            "schema_version": 1,
            "source": "agent_wire",
            "composed_chars": composed_chars,
            "wire_chars": user_chars,
            "bounded": composed_chars != user_chars,
        }
    return payload


def test_a_bounded_turn_raises_a_typed_wire_drift_row_on_the_frame():
    budget = _budget(_input(user_chars=4_000, composed_chars=56_000), metered=1_000)

    drift = budget["wire_drift"]
    assert drift["kind"] == "user_message_bounded_before_wire"
    assert (drift["composed_chars"], drift["wire_chars"]) == (56_000, 4_000)
    assert drift["dropped_chars"] == 52_000


def test_a_healthy_turn_stays_quiet():
    budget = _budget(_input(user_chars=4_000, composed_chars=4_000), metered=1_000)

    assert "wire_drift" not in budget
    assert "estimate_drift_alarm" not in budget


def test_a_record_that_accounts_for_far_less_than_the_meter_alarms():
    budget = _budget(_input(user_chars=4_000), metered=40_000)

    alarm = budget["estimate_drift_alarm"]
    assert alarm["direction"] == "metered_exceeds_record"
    assert alarm["ratio"] == budget["estimate_drift_ratio"] >= alarm["high"]


def test_a_record_that_accounts_for_far_more_than_the_meter_alarms():
    budget = _budget(_input(user_chars=400_000), metered=1_000)

    assert budget["estimate_drift_alarm"]["direction"] == "record_exceeds_metered"


# ---------------------------------------------------------------------------
# T3 — a reused resident actor constructs nothing
# ---------------------------------------------------------------------------


def _chat_request(*, signature="sig-1", revision="rev-1", registry, **overrides):
    return _request(
        root_chat_session_id="chat_root_1",
        persona_chat_runtime_registry=registry,
        persona_chat_runtime_signature=signature,
        persona_chat_native_revision=revision,
        **overrides,
    )


def test_the_second_send_on_a_warm_root_constructs_no_agent(stub_runtime):
    registry = PersonaChatRuntimeRegistry()
    runner = ProfileAgentRunner(agent_factory=_BoundaryAgent)

    first = runner.run(_chat_request(registry=registry))
    assert _BoundaryAgent.constructed == 1
    assert first.profile_timing["resident_actor_reused"] == 0
    assert isinstance(first.profile_timing["agent_construct_ms"], int)

    second = runner.run(_chat_request(registry=registry))

    assert _BoundaryAgent.constructed == 1, "the warm send built nothing"
    assert second.profile_timing["resident_actor_reused"] == 1
    assert "agent_construct_ms" not in second.profile_timing


def test_a_changed_signature_still_constructs(stub_runtime):
    registry = PersonaChatRuntimeRegistry()
    runner = ProfileAgentRunner(agent_factory=_BoundaryAgent)

    runner.run(_chat_request(registry=registry, signature="sig-1"))
    result = runner.run(_chat_request(registry=registry, signature="sig-2"))

    assert _BoundaryAgent.constructed == 2
    assert result.profile_timing["resident_rebuild_runtime_signature_changed"] == 1
    assert isinstance(result.profile_timing["agent_construct_ms"], int)


def test_a_changed_native_revision_still_constructs(stub_runtime):
    registry = PersonaChatRuntimeRegistry()
    runner = ProfileAgentRunner(agent_factory=_BoundaryAgent)

    runner.run(_chat_request(registry=registry, revision="rev-1"))
    result = runner.run(_chat_request(registry=registry, revision="rev-2"))

    assert _BoundaryAgent.constructed == 2
    assert result.profile_timing["resident_rebuild_disk_revision_changed"] == 1


def test_the_reused_actor_reports_progress_to_THIS_turn_s_listener(stub_runtime):
    """Laziness must not cost the resident its per-turn state refresh.

    Asserted as an OUTCOME — the second turn's listener actually receives the
    second turn's tool event — rather than as wiring. A resident that kept the
    FIRST turn's callback would still have a non-``None`` handle, so pinning
    "a callback is attached" would pass against exactly the bug.
    """

    registry = PersonaChatRuntimeRegistry()
    runner = ProfileAgentRunner(agent_factory=_BoundaryAgent)

    first_events: list[dict] = []
    second_events: list[dict] = []

    runner.run(_chat_request(registry=registry, progress_callback=first_events.append))
    delivered_to_first = len(first_events)

    result = runner.run(
        _chat_request(registry=registry, progress_callback=second_events.append)
    )

    assert result.profile_timing["resident_actor_reused"] == 1
    assert any(row.get("tool_name") == "terminal" for row in second_events)
    assert len(first_events) == delivered_to_first, "turn 1's listener stays closed"


def test_a_run_with_no_chat_registry_still_constructs(stub_runtime):
    ProfileAgentRunner(agent_factory=_BoundaryAgent).run(_request())

    assert _BoundaryAgent.constructed == 1


# ---------------------------------------------------------------------------
# T6 — the runtime resolves once for an unchanged profile
# ---------------------------------------------------------------------------


def test_the_second_send_reuses_the_resolved_runtime(stub_runtime):
    runner = ProfileAgentRunner(agent_factory=_BoundaryAgent)

    first = runner.run(_request())
    second = runner.run(_request())

    assert stub_runtime == [("openai-codex", "gpt-5.6-luna")]
    assert first.profile_timing["runtime_resolve_cached"] == 0
    assert second.profile_timing["runtime_resolve_cached"] == 1
    assert second.provider == "openai-codex"


def test_a_model_switch_re_resolves(stub_runtime):
    runner = ProfileAgentRunner(agent_factory=_BoundaryAgent)

    runner.run(_request())
    result = runner.run(_request(model="gpt-5.6-nova"))

    assert len(stub_runtime) == 2
    assert result.profile_timing["runtime_resolve_cached"] == 0
    assert result.model == "gpt-5.6-nova"


def test_a_provider_switch_re_resolves(stub_runtime):
    runner = ProfileAgentRunner(agent_factory=_BoundaryAgent)

    runner.run(_request())
    runner.run(_request(provider="anthropic"))

    assert len(stub_runtime) == 2


def test_editing_config_yaml_invalidates_the_memo(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = tmp_path / "config.yaml"
    config.write_text("model:\n  default: a\n", encoding="utf-8")
    request = _request()

    before = _runtime_resolve_cache_key(request)
    time.sleep(0.01)
    config.write_text("model:\n  default: bb\n", encoding="utf-8")

    assert _runtime_resolve_cache_key(request) != before


def test_the_memo_is_scoped_to_the_profile_home(tmp_path, monkeypatch):
    request = _request()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "alice"))
    alice = _runtime_resolve_cache_key(request)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "bob"))

    assert _runtime_resolve_cache_key(request) != alice


def test_the_memo_ttl_stays_under_the_token_refresh_skew():
    """The safety bound, pinned structurally — behaviour cannot demonstrate it.

    A memo handed out T seconds after resolution carries a token with at least
    ``skew - T`` seconds of validity. Raise the TTL past the skew and the runner
    starts handing turns expired OAuth credentials; no unit test would notice,
    because the credential is opaque here. So pin the rule.
    """

    from hermes_cli.auth import ACCESS_TOKEN_REFRESH_SKEW_SECONDS

    assert RUNTIME_RESOLVE_CACHE_TTL_SECONDS > 0
    assert RUNTIME_RESOLVE_CACHE_TTL_SECONDS < ACCESS_TOKEN_REFRESH_SKEW_SECONDS / 2


def test_an_expired_memo_re_resolves(stub_runtime, monkeypatch):
    runner = ProfileAgentRunner(agent_factory=_BoundaryAgent)
    runner.run(_request())
    assert len(stub_runtime) == 1

    clock = time.monotonic() + RUNTIME_RESOLVE_CACHE_TTL_SECONDS + 1
    monkeypatch.setattr(
        "agent_runtime.profile_runner.time.monotonic", lambda: clock
    )

    result = runner.run(_request())

    assert len(stub_runtime) == 2
    assert result.profile_timing["runtime_resolve_cached"] == 0


def test_resetting_the_memo_forces_a_re_resolve(stub_runtime):
    runner = ProfileAgentRunner(agent_factory=_BoundaryAgent)
    runner.run(_request())
    reset_runtime_resolve_cache()

    runner.run(_request())

    assert len(stub_runtime) == 2
