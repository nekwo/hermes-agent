"""Memory-scope gating for persona runs (audit Stage 2B).

Decided policy: per-persona recall by default; the shared mission/operator scope
only engages when the instance is on an in-progress goal/task. Free-floating
operator chat has no active goal, so it correctly resolves to per-persona recall
(memory enabled via the free_floating risk flag), never a cross-persona mission read.
"""

from types import SimpleNamespace

from agent_runtime.persona_runtime import _persona_run_uses_memory


def _ctx(risk_flags):
    return SimpleNamespace(task=SimpleNamespace(risk_flags=risk_flags))


def test_free_floating_chat_enables_per_persona_memory():
    persona = SimpleNamespace(include_profile_memory=False)
    assert _persona_run_uses_memory(persona, _ctx(["persona_operation_kind:free_floating"])) is True


def test_non_free_floating_without_profile_memory_stays_off():
    persona = SimpleNamespace(include_profile_memory=False)
    assert _persona_run_uses_memory(persona, _ctx([])) is False
    assert _persona_run_uses_memory(persona, _ctx(["some_other_flag"])) is False


def test_profile_memory_opt_in_always_on():
    persona = SimpleNamespace(include_profile_memory=True)
    assert _persona_run_uses_memory(persona, _ctx([])) is True


def test_missing_risk_flags_is_safe():
    persona = SimpleNamespace(include_profile_memory=False)
    assert _persona_run_uses_memory(persona, _ctx(None)) is False
