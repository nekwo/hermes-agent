import os

import pytest

from hermes_constants import get_hermes_home

import agent_runtime.paths as paths_module
import agent_runtime.config as config_module
from agent_runtime.profile_context import (
    PROFILE_CONTEXT_NO_PROFILE_BINDING,
    PROFILE_CONTEXT_RUNTIME_ROOT_UNRESOLVED,
    RUNTIME_ROOT_ENV,
    RUNTIME_ROOT_SOURCE_ARGUMENT,
    RUNTIME_ROOT_SOURCE_RESOLVER,
    RUNTIME_ROOT_SOURCE_UNRESOLVED,
    PersonaProfileBinding,
    persona_bound_profile_name,
    persona_profile_context,
)


def test_persona_bound_profile_name_prefers_persisted_worker_binding(monkeypatch):
    class Persona:
        hermes_profile = "launcher-qa"

    monkeypatch.setattr(config_module, "get_persisted_persona", lambda persona_id: Persona())
    monkeypatch.setenv("HERMES_PROFILE", "default")

    assert persona_bound_profile_name("qa") == "launcher-qa"


def _profile_less() -> PersonaProfileBinding:
    """A persona that binds no Hermes profile — the majority case."""

    return PersonaProfileBinding(
        persona_id="dev",
        hermes_profile=None,
        profile_home=None,
        summary="inherits active Harness profile",
    )


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


# ── audit Q1: the runtime root is exported for EVERY persona ─────────────────
#
# ``profile_home is None`` used to take an early ``yield`` that skipped every
# environment export, including the runtime root — which needs no profile at
# all. That single line was the fail-OPEN half of the live 2026-07-26 split:
# with HERMES_AGENT_RUNTIME_ROOT unset the legacy terminal envelope is inert,
# and ``git push origin main`` ran ungated and unrecorded on the same lane where
# a profile-bound persona was hard-blocked.


def test_a_profile_less_persona_still_gets_the_runtime_root(tmp_path, monkeypatch):
    monkeypatch.delenv(RUNTIME_ROOT_ENV, raising=False)
    monkeypatch.setattr(paths_module, "store_root", lambda: tmp_path / "resolved")

    with persona_profile_context(_profile_less()):
        assert os.environ[RUNTIME_ROOT_ENV] == str(tmp_path / "resolved")

    assert RUNTIME_ROOT_ENV not in os.environ


def test_a_profile_less_persona_restores_a_prior_value(tmp_path, monkeypatch):
    monkeypatch.setenv(RUNTIME_ROOT_ENV, str(tmp_path / "outer"))
    monkeypatch.setattr(paths_module, "store_root", lambda: tmp_path / "resolved")

    with persona_profile_context(_profile_less()):
        assert os.environ[RUNTIME_ROOT_ENV] == str(tmp_path / "resolved")

    assert os.environ[RUNTIME_ROOT_ENV] == str(tmp_path / "outer")


def test_a_profile_less_persona_leaves_the_profile_home_alone(tmp_path, monkeypatch):
    """Only the runtime root is exported — redirection still needs a binding."""

    head_home = tmp_path / "profiles" / "alice"
    head_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(head_home))
    monkeypatch.setattr(paths_module, "store_root", lambda: tmp_path / "resolved")

    with persona_profile_context(_profile_less()):
        assert os.environ["HERMES_HOME"] == str(head_home)
        assert get_hermes_home() == head_home


@pytest.mark.parametrize("preexisting", [None, "outer"])
def test_the_exported_root_is_identical_whether_or_not_the_env_was_already_set(
    preexisting, tmp_path, monkeypatch
):
    """The whole audit in one assertion, for this reader.

    Ancestry decided this value before: a serve process that had run a seeding
    handler exported one root, a fresh one exported nothing. Now the resolver
    answers and the prior value is irrelevant to what the RUN sees.
    """

    if preexisting is None:
        monkeypatch.delenv(RUNTIME_ROOT_ENV, raising=False)
    else:
        monkeypatch.setenv(RUNTIME_ROOT_ENV, str(tmp_path / preexisting))
    monkeypatch.setattr(paths_module, "store_root", lambda: tmp_path / "resolved")

    with persona_profile_context(_profile_less()):
        assert os.environ[RUNTIME_ROOT_ENV] == str(tmp_path / "resolved")
