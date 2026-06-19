import os

from hermes_constants import get_hermes_home

from agent_runtime.profile_context import PersonaProfileBinding, persona_profile_context


def test_persona_profile_context_enters_and_restores_env(tmp_path, monkeypatch):
    profile = tmp_path / "profiles" / "qa"
    profile.mkdir(parents=True)
    runtime_root = tmp_path / "runtime"
    head_home = tmp_path / "profiles" / "alice"
    head_home.mkdir(parents=True)
    monkeypatch.delenv("HERMES_AUTH_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(head_home))
    original_home = os.environ.get("HERMES_HOME")
    original_runtime = os.environ.get("HERMES_AGENT_RUNTIME_ROOT")
    original_auth_home = os.environ.get("HERMES_AUTH_HOME")

    binding = PersonaProfileBinding(
        persona_id="qa",
        hermes_profile="qa",
        profile_home=profile,
    )

    with persona_profile_context(binding, runtime_root=runtime_root):
        assert get_hermes_home() == profile
        assert os.environ["HERMES_HOME"] == str(profile)
        assert os.environ["HERMES_AGENT_RUNTIME_ROOT"] == str(runtime_root)
        assert os.environ["HERMES_AUTH_HOME"] == str(head_home)

    assert os.environ.get("HERMES_HOME") == original_home
    assert os.environ.get("HERMES_AGENT_RUNTIME_ROOT") == original_runtime
    assert os.environ.get("HERMES_AUTH_HOME") == original_auth_home
