"""The harness lane's ONE capability declaration (S0a A1, 2026-09-03).

Plan: ``docs/agent-runtime-harness/planned/s0a-atlas-cleanup.md`` §2 A1.

What is at stake, so a future reader does not "tidy" one of these away:

* Before this stage NOTHING on the harness lane read a profile's ``toolsets:``
  key. The shipped posture is ``unbounded`` and that branch resolved
  ``all_registered_toolsets()``, so every persona had the same 79-tool surface
  and the three per-persona ``toolsets`` lists (profile config, store row,
  realm-sync body) were consulted by nobody. ``declared_lane_toolsets`` is the
  reader that makes the declaration mean something.
* The bare upstream default ``["hermes-cli"]`` is what
  ``hermes_cli/config_defaults.py`` writes for an UNSET key, so reading it as
  "undeclared" is reading a default as the default it is (R-S0a-2). Any other
  list — including a stale explicit one — is honored verbatim, which is what
  keeps an operator's choice an operator's choice.
* Resolution failures fall to the NARROW known set, never to the wide registry.
* The read must not import ``model_tools``: the toolset-NAME half of an agent
  create is import-free because of that (A6a), and the subprocess case below is
  what says so.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from agent_runtime.models import AgentPersona
from agent_runtime.parse_cache import clear_parse_cache
from agent_runtime.personas import (
    HARNESS_LANE_DEFAULT_TOOLSETS,
    TOOLSET_SOURCE_LANE_DEFAULT,
    TOOLSET_SOURCE_PROFILE_CONFIG,
    TOOLSET_SOURCE_PROFILE_UNRESOLVED,
    declared_lane_toolsets,
    effective_toolsets,
)


HARNESS_CORE_MEMBERS = [
    "agent_chat", "board", "clarify", "delegation", "terminal", "file",
    "web", "browser", "browser-cdp", "skills", "memory", "todo",
    "session_search", "vision", "code_execution",
]


def _persona(profile: str | None = "gpt-launcher", *, toolsets=None) -> AgentPersona:
    return AgentPersona(
        id="dev",
        display_name="Launcher Dev Agent",
        role="dev",
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=list(toolsets if toolsets is not None else ["file", "terminal"]),
        system_prompt_path="",
        hermes_profile=profile,
    )


@pytest.fixture
def profile_config(bundled_persona_profiles):
    """Write a profile's ``config.yaml`` and hand back the persona bound to it.

    The parse is mtime-cached, so a same-second rewrite has to drop the cache —
    without this the second case in a test reads the first case's file.
    """

    from hermes_cli.profiles import get_profile_dir

    def _write(body: str | None, *, profile: str = "gpt-launcher") -> AgentPersona:
        home = get_profile_dir(profile)
        home.mkdir(parents=True, exist_ok=True)
        path = home / "config.yaml"
        if body is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(textwrap.dedent(body), encoding="utf-8")
        clear_parse_cache()
        return _persona(profile)

    return _write


# ── the rule (R-S0a-2) ───────────────────────────────────────────────────────


def test_an_absent_toolsets_key_resolves_the_lane_default(profile_config):
    persona = profile_config("agent:\n  model: gpt-5.5\n")

    declaration = declared_lane_toolsets(persona)

    assert declaration.source == TOOLSET_SOURCE_LANE_DEFAULT
    assert declaration.declared == HARNESS_LANE_DEFAULT_TOOLSETS
    assert list(declaration.toolsets) == HARNESS_CORE_MEMBERS
    assert declaration.config_path is not None and declaration.config_path.endswith("config.yaml")


def test_a_missing_config_file_resolves_the_lane_default(profile_config):
    persona = profile_config(None)

    assert declared_lane_toolsets(persona).source == TOOLSET_SOURCE_LANE_DEFAULT


def test_the_bare_upstream_default_is_read_as_undeclared(profile_config):
    """``[hermes-cli]`` is what an unset key WRITES — the whole point of R-S0a-2.

    Every profile on the operator's box carries exactly this line and none of
    them chose it; treating it as a declaration would keep the harness lane on
    the upstream CLI bundle (and its 12 hygiene-blocked kanban verbs) forever.
    """

    persona = profile_config("toolsets:\n  - hermes-cli\n")

    declaration = declared_lane_toolsets(persona)

    assert declaration.source == TOOLSET_SOURCE_LANE_DEFAULT
    assert declaration.declared == HARNESS_LANE_DEFAULT_TOOLSETS


def test_an_empty_list_resolves_the_lane_default(profile_config):
    persona = profile_config("toolsets: []\n")

    assert declared_lane_toolsets(persona).source == TOOLSET_SOURCE_LANE_DEFAULT


def test_a_non_list_value_resolves_the_lane_default(profile_config):
    persona = profile_config("toolsets: harness_core\n")

    assert declared_lane_toolsets(persona).source == TOOLSET_SOURCE_LANE_DEFAULT


def test_an_explicit_harness_core_is_the_same_answer_with_its_own_provenance(profile_config):
    """The operator's optional legibility step (A2 item 5) must not CHANGE the
    resolved set — only how it is reported. If these two ever diverge, writing
    the recommended line becomes a capability edit."""

    default_persona = profile_config("agent:\n  model: gpt-5.5\n")
    default_toolsets = list(declared_lane_toolsets(default_persona).toolsets)

    persona = profile_config("toolsets:\n  - harness_core\n")
    declaration = declared_lane_toolsets(persona)

    assert declaration.source == TOOLSET_SOURCE_PROFILE_CONFIG
    assert declaration.declared == ("harness_core",)
    assert list(declaration.toolsets) == default_toolsets


def test_an_opt_in_integration_rides_beside_the_bundle(profile_config):
    persona = profile_config("toolsets:\n  - harness_core\n  - spotify\n")

    declaration = declared_lane_toolsets(persona)

    assert declaration.source == TOOLSET_SOURCE_PROFILE_CONFIG
    assert list(declaration.toolsets) == HARNESS_CORE_MEMBERS + ["spotify"]


def test_a_stale_explicit_list_is_honored_verbatim(profile_config):
    """A list an operator actually wrote is not second-guessed — it is REPORTED.

    ``hermes-cli`` here resolves ``kanban``, which the A3 ratchet then reds on
    ``withheld``: the cue to write the explicit set, delivered as a diff rather
    than as a silent rewrite of the operator's file."""

    persona = profile_config("toolsets:\n  - hermes-cli\n  - spotify\n")

    declaration = declared_lane_toolsets(persona)

    assert declaration.source == TOOLSET_SOURCE_PROFILE_CONFIG
    assert list(declaration.toolsets) == ["hermes-cli", "spotify"]


def test_malformed_yaml_resolves_narrow_rather_than_wide(profile_config):
    """A config fault must never hand out MORE capability — the asymmetry
    ``default_permission_mode`` applies to an unparseable permission mode."""

    persona = profile_config("toolsets: [harness_core\n  broken: : :\n")

    declaration = declared_lane_toolsets(persona)

    assert declaration.source == TOOLSET_SOURCE_LANE_DEFAULT
    assert list(declaration.toolsets) == HARNESS_CORE_MEMBERS


def test_an_unresolvable_profile_is_typed_rather_than_silent():
    persona = _persona("no-such-profile-anywhere")

    declaration = declared_lane_toolsets(persona)

    assert declaration.source == TOOLSET_SOURCE_PROFILE_UNRESOLVED
    assert list(declaration.toolsets) == HARNESS_CORE_MEMBERS
    assert declaration.config_path is None


def test_a_persona_with_no_bound_profile_resolves_the_lane_default():
    persona = _persona(None)

    assert declared_lane_toolsets(persona).source == TOOLSET_SOURCE_PROFILE_UNRESOLVED


# ── the persona field is inert (R-S0a-3) ─────────────────────────────────────


def test_effective_toolsets_ignores_the_persona_level_list(profile_config):
    """The one authority, asserted at the seam every caller goes through.

    A persona whose field says ``["kanban"]`` — the exact class of stale row the
    operator's store carries — still resolves the profile's declaration, and the
    stale list is reported beside it rather than obeyed.
    """

    persona = profile_config("agent:\n  model: gpt-5.5\n")
    persona.toolsets = ["kanban", "messaging"]

    declaration = declared_lane_toolsets(persona)

    assert effective_toolsets(persona) == HARNESS_CORE_MEMBERS
    assert "kanban" not in effective_toolsets(persona)
    assert declaration.persona_list == ("kanban", "messaging")


def test_the_wire_row_carries_declaration_and_the_legacy_list(profile_config):
    persona = profile_config("toolsets:\n  - harness_core\n")
    persona.toolsets = ["kanban"]

    row = declared_lane_toolsets(persona).row()

    assert row["declared"] == ["harness_core"]
    assert row["source"] == TOOLSET_SOURCE_PROFILE_CONFIG
    assert row["persona_list"] == ["kanban"]
    assert row["toolsets"] == HARNESS_CORE_MEMBERS
    assert row["profile"] == "gpt-launcher"


# ── A6a: names without the registrars ────────────────────────────────────────


def test_the_declaration_read_never_imports_model_tools(tmp_path):
    """The A6a gate, in a subprocess so the assertion is about a COLD process.

    ``import model_tools`` runs ``discover_builtin_tools()`` over every module
    under ``tools/`` (1.6-2.6 s cold on the 2026-09-03 box). The create path's
    toolset NAMES no longer need it; its tool NAMES still do. Same shape as
    ``tests/agent_runtime/test_tool_visibility_import_deferral.py``.
    """

    home = tmp_path / "hermes-home"
    (home / "profiles" / "gpt-launcher").mkdir(parents=True)
    code = (
        "import sys\n"
        "from agent_runtime.models import AgentPersona\n"
        "from agent_runtime.personas import declared_lane_toolsets\n"
        "persona = AgentPersona(id='dev', display_name='dev', role='dev', model=None,\n"
        "    provider=None, api_mode='codex_responses', toolsets=['file'],\n"
        "    system_prompt_path='', hermes_profile='gpt-launcher')\n"
        "declaration = declared_lane_toolsets(persona)\n"
        "assert len(declaration.toolsets) == 15, declaration\n"
        "print('model_tools' in sys.modules)\n"
    )
    env = {
        **{k: v for k, v in __import__("os").environ.items()},
        "HERMES_HOME": str(home),
    }
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )

    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", out.stdout
