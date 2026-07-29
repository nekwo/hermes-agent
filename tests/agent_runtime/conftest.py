import pytest


@pytest.fixture
def bundled_persona_profiles():
    """Provision the Hermes profile homes the bundled personas BIND.

    Since ``9ad9c8017`` (*fix(harness): provision bundled agent personas*,
    2026-07-19) ``default_personas()`` binds a real profile to every advertised
    agent — ``dev`` → ``launcher-dev``, ``backend_dev`` → ``backend-dev``,
    ``qa`` → ``qa`` — and ``GPTPersonaRuntime._invoke_agent`` refuses to run a
    persona whose bound profile does not exist ("every advertised agent must
    have a real Hermes profile behind it"). The autouse hermetic fixture points
    ``HERMES_HOME`` at a per-test tempdir, where none of them do.

    Production provisions them with ``harness init --with-bundled-personas``;
    a test that drives the REAL run path is asking for the same precondition.
    Opt in explicitly — tests that assert the *missing*-profile refusal must not
    have it provisioned underneath them.
    """

    from agent_runtime.personas import BUNDLED_PERSONA_PROFILES
    from hermes_cli.profiles import get_profile_dir

    homes = []
    for profile in dict.fromkeys(BUNDLED_PERSONA_PROFILES.values()):
        home = get_profile_dir(profile)
        home.mkdir(parents=True, exist_ok=True)
        homes.append(home)
    return homes


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


def release_to_implementation(task, *, owner_slots=("dev", "backend_dev")):
    """Retarget an old dev-mechanics fixture after removal of the stage graph."""

    task.current_stage_id = "implement"
    return task
