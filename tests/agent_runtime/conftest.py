import pytest

from agent_runtime import repo_context as _repo_context
from agent_runtime.config import harness_root_config_path
from hermes_constants import get_hermes_head_home


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
def bounded_chat_session():
    """Pin one chat session to the pre-2026-08-09 BOUNDED tier.

    The runtime default is ``unbounded`` (operator ruling 2026-08-09 — see
    ``docs/agent-runtime-harness/UNBOUNDED_DEFAULT_PLAN_2026-08-09.md``), so a
    test whose SUBJECT is the bounded tier — the chat-lane cost policy, the
    persona-safety block set, the envelope grants table — has to say so instead
    of leaning on a default that no longer means what it used to. Leaning on it
    would not just fail; it would silently re-pin the old posture the moment
    someone "fixed" the assertion.

    This writes the real store record through the real store, so the test still
    resolves through the ONE chokepoint (``permission_options_for_chat``) rather
    than monkeypatching a mode into place.
    """

    from agent_runtime.permission_modes import PERMISSION_MODE_BOUNDED
    from agent_runtime.tool_permissions import ChatToolPermissionStore

    def _pin(persona_id: str, session_id: str = "bounded-session") -> str:
        ChatToolPermissionStore().set(
            persona_id=str(persona_id),
            session_id=session_id,
            mode=PERMISSION_MODE_BOUNDED,
            reason="test pins the historical bounded tier",
        )
        return session_id

    return _pin


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
def reset_profile_runner_runtime_resolve_cache():
    """Keep the T6 runtime-resolve memo from crossing test boundaries.

    The memo keys on (profile home, provider, model, config stamps) — none of
    which distinguish one test's monkeypatched ``resolve_runtime_provider`` from
    the next test's. Without this, a suite that patches two different resolvers
    for the same provider/model silently reads the first one's answer in the
    second test. Process-global state gets a process-global reset.
    """

    from agent_runtime.profile_runner import reset_runtime_resolve_cache

    reset_runtime_resolve_cache()
    yield
    reset_runtime_resolve_cache()


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


def assert_under(path, base, *, what: str) -> None:
    """Fail unless ``path`` resolves INSIDE ``base``.

    Shared by the guard fixture below and ``test_root_config_hermetic.py`` so
    the guard and the test that proves the guard cannot drift apart.
    """

    resolved = path.resolve()
    root = base.resolve()
    assert root == resolved or root in resolved.parents, (
        f"{what} resolved to {resolved}, which is OUTSIDE the per-test tmp dir "
        f"{root}. Root-config resolution has escaped the sandbox: this test is "
        "reading the OPERATOR's live runtime root, so what it exercises depends "
        "on a file nobody in this repo controls."
    )


@pytest.fixture(autouse=True)
def assert_root_config_resolution_is_hermetic(tmp_path, _hermetic_environment):
    """Prove — before every agent-runtime test body — that ROOT-scope config
    resolution lands inside the per-test tmp dir.

    WHY AN ASSERTION AND NOT A THIRD PIN. The ``HERMES_HOME`` pin already
    exists and already works: ``tests/conftest.py::_hermetic_environment``
    (autouse, repo-wide) redirects it to ``tmp_path/hermes_test``, and
    ``config.harness_root_config_path()`` reads ``HERMES_HOME`` through
    ``hermes_constants.get_default_hermes_root()``, so the ROOT config a test
    sees is already synthetic. Re-pinning ``HERMES_HOME`` HERE would create a
    SECOND authority for the same variable pointing at a DIFFERENT directory
    than the one the root fixture pre-populates (``sessions/``, ``cron/``,
    ``memories/``, ``skills/``), and the two would drift the first time either
    moved. What was actually missing is not a pin — it is any statement,
    anywhere, that the pin is load-bearing for this package.

    That matters here specifically. ``ROOT_ONLY_CONFIG_KEYS``
    (``agent_runtime/config.py:361``) names four readers —
    ``state_patches.delta_patches_enabled``, ``mcp_admission.admission_config``,
    ``config.chat_lane_restore_toolsets``, ``config.mission_chat_workdir`` —
    that resolve ONLY through the root config. If the root pin ever regresses,
    those four silently start answering out of the operator's live
    ``config.yaml``, every one of them gates real behavior, and NOTHING in the
    suite goes red: the tests keep passing, against a different runtime. That
    is the failure mode this fixture converts into a loud one.

    ``HERMES_HEAD_HOME`` is asserted alongside it because it is the same hole
    one variable over, and unlike ``HERMES_HOME`` it was genuinely unpinned
    until this change (see ``tests/conftest.py::_HERMES_BEHAVIORAL_VARS``). It
    is not merely read: ``get_hermes_head_home()`` selects the SessionDB the
    Mission Control transcript store WRITES to, and the Launcher's serve does
    export it, so a suite run from a Launcher-shaped shell wrote test rows into
    the operator's live ``profiles/base/state.db``.

    Declares ``_hermetic_environment`` explicitly rather than trusting autouse
    ordering: this fixture asserts a property that fixture establishes, so the
    dependency is real and stating it removes any question of which runs first.

    SETUP ONLY, deliberately. Tests that must read the live tree opt out of the
    sandbox INSIDE their own body — ``test_launcher_qa_template_drift.py``
    restores the pre-sandbox ``HERMES_HOME`` on purpose to check the real
    profile tree — and a teardown assertion would punish exactly that legitimate
    pattern for doing the honest thing.
    """

    assert_under(
        harness_root_config_path(),
        tmp_path,
        what="config.harness_root_config_path()",
    )
    assert_under(
        get_hermes_head_home(),
        tmp_path,
        what="hermes_constants.get_hermes_head_home()",
    )
    yield


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
