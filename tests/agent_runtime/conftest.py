import pytest


@pytest.fixture(autouse=True)
def isolate_agent_runtime_root(tmp_path, monkeypatch):
    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    yield root


# S7-B RULING-0 COMPAT STRIP (2026-07-16): the ``history_in_frame_config`` and
# ``inline_prompt_payloads_config`` kill-switch fixtures were removed with the
# flags they flipped. The evicted/hoisted read-model shape is the only shape;
# tests that used to assert the legacy full-in-frame / inline content now
# re-target the on-disk archive artifacts + the paged history / on-demand fetch
# paths (see test_snapshot_history_eviction.py, test_snapshot.py,
# test_persona_assignments.py, test_stage52_role_envelopes.py).
