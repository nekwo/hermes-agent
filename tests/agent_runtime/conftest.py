import pytest

from agent_runtime import repo_context as _repo_context


_PRODUCTION_WORKTREE_BASE_DIR = _repo_context._worktree_base_dir
_PRODUCTION_LEGACY_WORKTREE_BASE_DIR = (
    _repo_context.legacy_harness_worktree_base_dir
)


@pytest.fixture
def persisted_persona_samples():
    """Install explicit persona DATA for tests that exercise deployed chat surfaces."""
    from agent_runtime.store import AgentStore
    from tests.agent_runtime.persona_samples import sample_personas

    store = AgentStore()
    for persona in sample_personas():
        store.save(persona)
    return store.list_all()


@pytest.fixture
def bundled_persona_profiles():
    """Provision the explicit profile homes used by legacy runtime test data."""

    from hermes_cli.profiles import get_profile_dir

    homes = []
    for profile in ("gpt-launcher", "backend-dev", "qa"):
        home = get_profile_dir(profile)
        home.mkdir(parents=True, exist_ok=True)
        homes.append(home)
    return homes


@pytest.fixture(autouse=True)
def isolate_agent_runtime_root(tmp_path, monkeypatch):
    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    # Structural janitor boundary: no agent-runtime test may discover the
    # machine-global fallback or a live profile's worktrees by accident. Tests
    # that exercise explicit bases may still layer their narrower pins on top.
    monkeypatch.setattr(
        _repo_context, "_worktree_base_dir", lambda: tmp_path / "worktrees"
    )
    monkeypatch.setattr(
        _repo_context,
        "legacy_harness_worktree_base_dir",
        lambda: tmp_path / "legacy-worktrees",
    )
    yield root


@pytest.fixture
def production_worktree_base_functions():
    """Explicit opt-in for tests that characterize production base selection."""

    return _PRODUCTION_WORKTREE_BASE_DIR, _PRODUCTION_LEGACY_WORKTREE_BASE_DIR


# S7-B RULING-0 COMPAT STRIP (2026-07-16): the ``history_in_frame_config`` and
# ``inline_prompt_payloads_config`` kill-switch fixtures were removed with the
# flags they flipped. The evicted/hoisted read-model shape is the only shape;
# tests that used to assert the legacy full-in-frame / inline content now
# re-target the on-disk archive artifacts + the paged history / on-demand fetch
# paths (see test_snapshot_history_eviction.py, test_snapshot.py,
# test_persona_assignments.py, test_stage52_role_envelopes.py).


def release_to_implementation(task, *, owner_slots=("dev", "backend_dev")):
    """Retarget an old dev-mechanics fixture after removal of the stage graph."""

    task.current_stage_id = "implement"
    return task
