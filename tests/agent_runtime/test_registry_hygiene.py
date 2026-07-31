"""Fork registry hygiene (T6c, Context Cost Workstream 2026-07-18).

The fork's effective registry must never resolve the upstream ``kanban`` toolset
(12 tools) or the ``feishu_doc`` / ``feishu_drive`` toolsets (5 tools) on ANY
agent-runtime lane. The upstream tool files stay untouched; the fork-owned
``REGISTRY_HYGIENE_BLOCKED_TOOLS`` constant plus the ``profile_runner``
agent-construction chokepoint are the deregistration mechanism.
"""

from __future__ import annotations

from agent_runtime.personas import PERSONA_BLOCKED_TOOLS, REGISTRY_HYGIENE_BLOCKED_TOOLS
from agent_runtime.profile_runner import (
    AgentRunRequest,
    ProfileAgentRunner,
    _blocked_tool_names_with_registry_hygiene,
)
from toolsets import resolve_toolset


def _static_toolset_tools(name: str) -> set[str]:
    # Static (non-registry) view so the invariant is a stable contract, not a
    # live-registry snapshot.
    return set(resolve_toolset(name, include_registry=False))


def test_hygiene_set_covers_exactly_the_kanban_and_feishu_toolsets():
    # Drift detector: if upstream adds/removes a tool in any of these toolsets,
    # this fails so the fork updates the deregistration constant deliberately.
    expected = (
        _static_toolset_tools("kanban")
        | _static_toolset_tools("feishu_doc")
        | _static_toolset_tools("feishu_drive")
    )
    assert REGISTRY_HYGIENE_BLOCKED_TOOLS == frozenset(expected)
    # 9 → 12 at the 2026-07-31 upstream sync (kanban card attachments:
    # kanban_attach / kanban_attach_url / kanban_attachments). The detector did
    # its job; the constant was extended deliberately rather than the count
    # loosened.
    assert len(_static_toolset_tools("kanban")) == 12
    assert len(_static_toolset_tools("feishu_doc") | _static_toolset_tools("feishu_drive")) == 5


def test_hygiene_set_keeps_delegate_task_and_memory_registered():
    # Operator ruling: delegate_task and memory stay registered (useful) — they
    # are parallel-authority surfaces, not deregistered.
    assert "delegate_task" not in REGISTRY_HYGIENE_BLOCKED_TOOLS
    assert "memory" not in REGISTRY_HYGIENE_BLOCKED_TOOLS


def test_persona_blocked_tools_is_a_superset_of_the_hygiene_set():
    # Persona chat/run lanes + the tool_visibility permission preview carry the
    # hygiene block through PERSONA_BLOCKED_TOOLS; the pre-existing persona blocks
    # are preserved unchanged.
    assert REGISTRY_HYGIENE_BLOCKED_TOOLS.issubset(PERSONA_BLOCKED_TOOLS)
    assert {"delegate_task", "clarify", "memory", "send_message", "cronjob"}.issubset(
        PERSONA_BLOCKED_TOOLS
    )


def test_helper_unions_hygiene_and_preserves_requested_blocks():
    # The worker / root-node lanes pass blocked_tool_names=[] — the helper still
    # blocks the whole hygiene set.
    from_empty = set(_blocked_tool_names_with_registry_hygiene([]))
    assert REGISTRY_HYGIENE_BLOCKED_TOOLS.issubset(from_empty)
    # A caller's own blocks are preserved (order-first, no duplicates).
    result = _blocked_tool_names_with_registry_hygiene(["custom_block", "kanban_show"])
    assert result[0] == "custom_block"
    assert result.count("kanban_show") == 1
    assert REGISTRY_HYGIENE_BLOCKED_TOOLS.issubset(set(result))


class _CapturingAgent:
    """Minimal fake agent that records the kwargs it was constructed with."""

    last_kwargs: dict | None = None

    def __init__(self, **kwargs):
        _CapturingAgent.last_kwargs = kwargs
        self.session_id = kwargs.get("session_id") or "session_hygiene"
        self.provider = kwargs.get("provider")
        self.model = kwargs.get("model")
        self.base_url = None
        self.tools = []

    def run_conversation(self, user_message, system_message=None, task_id=None):
        return {
            "final_response": "ok",
            "session_id": self.session_id,
            "messages": [],
            "api_calls": 1,
            "total_tokens": 1,
        }


def test_profile_runner_blocks_kanban_feishu_on_the_worker_lane_shape():
    # A request with blocked_tool_names=[] is the worker / root-node lane shape
    # (node_tools.py / root_node_engine.py). The chokepoint must still block the
    # hygiene set so those lanes cannot resolve kanban / feishu either.
    ProfileAgentRunner(agent_factory=_CapturingAgent).run(
        AgentRunRequest(profile=None, blocked_tool_names=[], user_message="hi")
    )
    blocked = set(_CapturingAgent.last_kwargs["blocked_tool_names"])
    assert REGISTRY_HYGIENE_BLOCKED_TOOLS.issubset(blocked)
