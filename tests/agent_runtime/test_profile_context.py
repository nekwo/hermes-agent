import os

import pytest

from hermes_constants import get_hermes_home

import agent_runtime.paths as paths_module
from agent_runtime.profile_context import (
    PROFILE_CONTEXT_NO_PROFILE_BINDING,
    PROFILE_CONTEXT_RUNTIME_ROOT_UNRESOLVED,
    RUNTIME_ROOT_ENV,
    RUNTIME_ROOT_SOURCE_ARGUMENT,
    RUNTIME_ROOT_SOURCE_RESOLVER,
    RUNTIME_ROOT_SOURCE_UNRESOLVED,
    PersonaProfileBinding,
    persona_profile_context,
    persona_profile_scope,
)


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


# ── the CONTEXT-LOCAL mode: same binding, no process-global writes ───────────
#
# ``persona_profile_scope`` exists because the snapshot readiness walk enters a
# profile binding per persona every 2-4 s, on the builder thread, in the same
# ``harness serve`` process that hosts chat turns — and takes no
# ``_WORKDIR_LOCK``. With the env mirror on, every ambient ``get_hermes_home()``
# reader on every other thread resolved the WALKED profile for the width of the
# walk. These pin the two halves of the mode's claim: it really binds, and it
# really writes nothing.


def _bound(profile_home) -> PersonaProfileBinding:
    return PersonaProfileBinding(
        persona_id="qa", hermes_profile="qa", profile_home=profile_home
    )


def test_the_context_local_scope_binds_the_home_without_touching_the_process(
    tmp_path, monkeypatch
):
    """The whole point in one assertion: bound here, unchanged everywhere else.

    *Killing mutation:* re-enable the ``os.environ`` block for
    ``export_env is False``. *Probed fields:* the four variables, mid-scope.
    """

    profile = tmp_path / "profiles" / "qa"
    (profile / "home").mkdir(parents=True)
    head_home = tmp_path / "profiles" / "base"
    head_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(head_home))
    monkeypatch.delenv("HERMES_AUTH_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv(RUNTIME_ROOT_ENV, raising=False)
    monkeypatch.setattr(paths_module, "store_root", lambda: tmp_path / "resolved")

    with persona_profile_scope(_bound(profile)):
        # Bound, context-locally.
        assert get_hermes_home() == profile
        # ...and the process is exactly where it was. ``HOME`` is included
        # because the env mode WOULD have redirected it here (``profile/home``
        # exists) — that write is this mode's one named residue, and this is
        # where "it does not happen" is stated.
        assert os.environ["HERMES_HOME"] == str(head_home)
        assert "HERMES_AUTH_HOME" not in os.environ
        assert "HOME" not in os.environ
        assert RUNTIME_ROOT_ENV not in os.environ

    assert get_hermes_home() == head_home


def test_the_context_local_scope_resolves_the_SAME_shared_auth_store(
    tmp_path, monkeypatch
):
    """The reader that decided whether this mode was shippable at all.

    ``hermes_cli.auth._global_auth_file_path`` is reached by the readiness
    walk's provider probe on EVERY walk (``_provider_issue`` →
    ``load_pool`` / ``probe_runtime_provider`` → ``read_credential_pool`` →
    ``_load_global_auth_store``), and it used to read ``HERMES_AUTH_HOME`` raw.
    A context-local binding writes no such variable, so without the ContextVar
    channel this probe would silently fall through to the global ROOT store and
    judge a persona's credentials against the wrong ``auth.json`` — both files
    exist on the live install.

    The two modes must therefore agree byte-for-byte.

    *Killing mutation:* restore ``os.environ.get("HERMES_AUTH_HOME")`` in
    ``_global_auth_file_path``. *Probed field:* the path each mode resolves.
    """

    from hermes_cli.auth import _global_auth_file_path

    root = tmp_path / "hermes"
    profile = root / "profiles" / "qa"
    head_home = root / "profiles" / "base"
    for path in (profile, head_home):
        path.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(head_home))
    monkeypatch.delenv("HERMES_AUTH_HOME", raising=False)
    monkeypatch.setattr(paths_module, "store_root", lambda: tmp_path / "resolved")

    with persona_profile_context(_bound(profile)):
        env_mode = _global_auth_file_path()
    with persona_profile_scope(_bound(profile)):
        scope_mode = _global_auth_file_path()

    assert env_mode == head_home / "auth.json"
    assert scope_mode == env_mode, (
        "the context-local binding resolved a different shared auth store than "
        "the env-exporting one; the readiness provider probe would judge this "
        "persona against another profile's credentials"
    )


@pytest.mark.parametrize("ambient", ["profile_home", "custom_root", "unset"])
def test_the_profiles_root_survives_dropping_the_HERMES_HOME_write(
    ambient, tmp_path, monkeypatch
):
    """The COLLAPSE the context-local mode rests on, as a property not a claim.

    ``get_default_hermes_root()`` reads ``os.environ["HERMES_HOME"]`` RAW, and it
    is reached from inside a binding by every skill-root resolution that matters
    (`get_shared_skills_dir`, `hermes_cli.profiles.get_profile_dir`,
    `skill_install.harness_skill_destination`, `skills_inventory.build_shared_catalog`).
    So the whole no-env-writes argument turns on ONE question: does re-deriving
    the root from the profile home the env mode WOULD have written give the same
    answer as reading the ambient env the context-local mode leaves alone?

    It does, structurally, because a binding's ``profile_home`` always comes from
    ``get_profile_dir`` — i.e. ``get_default_hermes_root()/profiles/<name>`` — and
    that function is a fixed point over exactly those paths: a path under the
    native home maps back to the native home, and a custom ``<root>/profiles/x``
    maps back to ``<root>``. ``get_shared_skills_dir``'s own docstring states the
    property for its own case ("alice, neko, base, … all compute the same
    ``<root>/shared/skills`` from their own ``HERMES_HOME``").

    Parametrized over the three ambient layouts a serve can be launched in,
    including **unset** — the one an audit flagged as the possible divergence.
    It is not one: with ``HERMES_HOME`` unset the profiles root IS the platform
    default, so the written profile home lands under it and maps straight back.

    *Killing mutation:* drop the ``env_path.parent.name == "profiles"`` mapping
    in ``get_default_hermes_root``. *Probed field:* the re-derived root.
    """

    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import get_default_hermes_root

    with monkeypatch.context() as scoped:
        if ambient == "unset":
            scoped.delenv("HERMES_HOME", raising=False)
        elif ambient == "custom_root":
            scoped.setenv("HERMES_HOME", str(tmp_path / "opt" / "data"))
        else:
            scoped.setenv(
                "HERMES_HOME", str(tmp_path / "opt" / "data" / "profiles" / "base")
            )

        # What the context-local mode leaves the raw readers looking at.
        ambient_root = get_default_hermes_root()
        # The binding a persona resolves, computed from that same ambient env.
        profile_home = get_profile_dir("qa")
        assert profile_home.parent.name == "profiles"

        # ...and what the env-WRITING mode would have made those readers see.
        scoped.setenv("HERMES_HOME", str(profile_home))
        assert get_default_hermes_root() == ambient_root, (
            "the profiles root a binding re-derives is not the one it came from; "
            "dropping the HERMES_HOME write would move every shared-skills and "
            "profile-dir resolution inside the binding"
        )


def test_the_context_local_scope_is_invisible_to_other_threads(tmp_path, monkeypatch):
    """ContextVars are per-task, which is the whole reason this needs no lock."""

    import threading

    profile = tmp_path / "profiles" / "qa"
    profile.mkdir(parents=True)
    head_home = tmp_path / "profiles" / "base"
    head_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(head_home))
    monkeypatch.setattr(paths_module, "store_root", lambda: tmp_path / "resolved")

    seen: list = []
    with persona_profile_scope(_bound(profile)):
        other = threading.Thread(target=lambda: seen.append(get_hermes_home()))
        other.start()
        other.join(10)

    assert seen == [head_home]
