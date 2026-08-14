"""ROOT-scope config resolution must never reach the operator's live runtime.

``agent_runtime`` has a whole class of policy — ``config.ROOT_ONLY_CONFIG_KEYS``
(``agent_runtime/config.py:361``) — that is read ONLY through
``config.harness_root_config_path()``, i.e. through ``HERMES_HOME`` as resolved
by ``hermes_constants.get_default_hermes_root()``. Four live readers hang off
it: ``state_patches.delta_patches_enabled``, ``mcp_admission.admission_config``,
``config.chat_lane_restore_toolsets``, ``config.mission_chat_workdir``.

If that resolution ever escapes the sandbox, every one of those readers starts
answering out of a ``config.yaml`` this repo does not control — and the suite
STAYS GREEN while doing it, because a passing assertion says nothing about
which file it was measured against. The operator would then be able to change
which code paths the suite exercises by editing their own config, and a fresh
machine (no such file, shipped defaults) would exercise different ones again.

The guard is the autouse ``assert_root_config_resolution_is_hermetic`` fixture
in ``conftest.py``; this file is what fails by NAME when the guard is removed,
weakened, or quietly stops covering a variable.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from agent_runtime.config import harness_root_config_path, load_root_runtime_config
from agent_runtime.state_patches import delta_patches_enabled
from hermes_constants import get_hermes_head_home, get_hermes_home

from tests.agent_runtime.conftest import assert_under

#: The two homes whose escape would silently re-target root-scope reads. Kept
#: as data so a third one cannot be added to the guard without a test naming it.
GUARDED_RESOLVERS = (
    ("config.harness_root_config_path", harness_root_config_path),
    ("hermes_constants.get_hermes_head_home", get_hermes_head_home),
    ("hermes_constants.get_hermes_home", get_hermes_home),
)


@pytest.mark.parametrize("label,resolve", GUARDED_RESOLVERS, ids=[n for n, _ in GUARDED_RESOLVERS])
def test_root_scope_resolution_lands_inside_the_per_test_tmp_dir(label, resolve, tmp_path):
    """The headline invariant, stated positively and per resolver."""

    assert_under(resolve(), tmp_path, what=label)


def test_root_config_file_does_not_exist_unless_a_test_wrote_it():
    """Absence, not just location — the sandbox root starts with NO root config.

    Location alone is a weaker claim than it looks: a guard could pass while
    pointing at a synthetic root that some earlier test had already seeded, and
    then ``load_root_runtime_config()`` would answer from leaked state rather
    than from the shipped defaults a fresh install gets. Assert the fresh-install
    condition directly.
    """

    path = harness_root_config_path()
    assert not path.exists(), (
        f"a ROOT config already exists at {path} before this test wrote one — "
        "either the sandbox is pointing somewhere real, or a previous test "
        "leaked a file into it"
    )


def test_root_gated_reader_answers_from_the_shipped_default_not_a_live_file():
    """``delta_patches_enabled()`` is the reader that actually got burned.

    2026-08-13: the producer lane hung on ``agent_runtime.read_model.
    delta_patches`` living in ONE untracked file on ONE machine. Pin the
    property that makes the suite's answer reproducible on any machine: with no
    root config present, the reader returns the SHIPPED default rather than
    whatever an operator's file happens to say.
    """

    from agent_runtime.runtime_config import SHIPPED_DELTA_PATCHES

    assert not harness_root_config_path().exists()
    assert delta_patches_enabled() is SHIPPED_DELTA_PATCHES


def test_live_operator_root_is_not_what_the_suite_reads():
    """Negative form: name the escape route and prove it is shut.

    On a developer machine ``HERMES_HOME`` is typically set in the operator's own
    shell (here: ``X:\\Eternia\\.hermes``) and is inherited by any bare
    ``pytest``. The positive assertions above would still pass if the sandbox
    happened to land on some *other* real root, so also assert that whatever the
    suite resolved is not the ambient value the process was launched with.

    SKIPS where no ambient ``HERMES_HOME`` existed — CI, mostly. That is the
    honest outcome: with nothing to escape to, there is nothing to prove here,
    and the skip message says so rather than reporting coverage it did not run.
    """

    # tests/conftest.py records the pre-sandbox value at import time. Its
    # sys.modules key depends on rootdir/importmode, so scan for the ATTRIBUTE
    # rather than guessing the key — the same reason
    # test_launcher_qa_template_drift.py does.
    ambient = ""
    for name, module in list(sys.modules.items()):
        if "conftest" not in name:
            continue
        recorded = getattr(module, "_PRE_SANDBOX_HERMES_HOME", None)
        if recorded:
            ambient = str(recorded)
            break

    if not ambient:
        pytest.skip(
            "no HERMES_HOME was set when pytest launched, so there is no live "
            "root for the sandbox to have failed to escape from — the positive "
            "assertions above are the whole coverage in this environment"
        )

    resolved = harness_root_config_path().resolve()
    live_root = Path(ambient).resolve()
    assert live_root != resolved and live_root not in resolved.parents, (
        f"root-config resolution landed at {resolved}, inside the LIVE runtime "
        f"root {live_root} that pytest was launched with"
    )


def test_the_guard_fixture_is_autouse_and_covers_both_homes():
    """Red-proof anchor: the guard must still be WIRED, not merely defined.

    Deleting ``autouse=True`` from the conftest fixture is a one-token change
    that disables every assertion above for every OTHER test in the package
    while leaving this file green — the tests here would still resolve inside
    ``tmp_path`` because the root conftest's pin is what does the work. So
    assert the wiring itself.
    """

    from tests.agent_runtime import conftest as ar_conftest

    fixture = ar_conftest.assert_root_config_resolution_is_hermetic
    # pytest 9 replaced the ``_pytestfixturefunction`` attribute with a
    # FixtureFunctionDefinition object; ``getfixturemarker`` is the supported
    # accessor across both shapes. Reading the attribute directly silently
    # returns None here and would turn this whole assertion into a false red.
    from _pytest.fixtures import getfixturemarker

    marker = getfixturemarker(fixture)
    assert marker is not None, "the guard is no longer a pytest fixture"
    assert marker.autouse, (
        "assert_root_config_resolution_is_hermetic lost autouse=True — it now "
        "guards only the tests that name it, which is none of them"
    )

    # WHAT the guard calls assert_under WITH, read off the AST rather than
    # grepped out of the source.
    #
    # This started as a substring check and mutation testing killed it. Repoint
    # the second leg's SUBJECT at ``harness_root_config_path()`` while leaving
    # its ``what=`` label reading "get_hermes_head_home()": every token a grep
    # looks for is still present, the head home is no longer checked by
    # anything, and with HERMES_HEAD_HOME poisoned the suite stayed GREEN. A
    # source-text assertion cannot tell an argument from a docstring, a comment,
    # or a label — so assert on the call the parser sees.
    guard = next(
        node
        for node in ast.walk(ast.parse(Path(ar_conftest.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef)
        and node.name == "assert_root_config_resolution_is_hermetic"
    )

    params = [arg.arg for arg in guard.args.args]
    assert "_hermetic_environment" in params, (
        "the guard stopped requesting _hermetic_environment — it now asserts a "
        "property it no longer forces to be established first, and its ordering "
        "against the fixture that pins HERMES_HOME becomes implicit"
    )

    checks = [
        node
        for node in ast.walk(guard)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "assert_under"
    ]
    subjects = [ast.unparse(call.args[0]) for call in checks]
    assert subjects == ["harness_root_config_path()", "get_hermes_head_home()"], (
        "the guard no longer checks exactly these two resolvers, in this order; "
        f"it now checks {subjects}. Any resolver dropped from this list can "
        "escape the sandbox without anything going red."
    )

    # And the BASE each is compared against. Widening it (``tmp_path.parent``)
    # is the weakening that leaves every other test in this file green: the live
    # root is on another drive entirely, so the headline assertions still pass
    # while cross-test leakage into a sibling tmp dir stops being caught.
    bases = [ast.unparse(call.args[1]) for call in checks]
    assert bases == ["tmp_path", "tmp_path"], (
        f"the guard compares against {bases} rather than the per-test tmp dir — "
        "a widened base still catches the live root but stops catching leakage "
        "between tests"
    )


def test_assert_under_actually_rejects_an_outside_path(tmp_path):
    """The helper the whole file leans on must not be vacuously true.

    An ``assert_under`` that passed for everything would make every assertion
    above green forever. Exercise both directions.
    """

    assert_under(tmp_path / "a" / "b", tmp_path, what="inside")
    assert_under(tmp_path, tmp_path, what="the base itself")
    with pytest.raises(AssertionError):
        assert_under(tmp_path.parent, tmp_path, what="outside")


def test_root_loader_is_reachable_and_reads_the_sandbox(tmp_path):
    """End to end: write a root config into the sandbox and see it come back.

    Location assertions prove where the reader LOOKS. This proves the reader
    also READS there — a resolver that returned a hermetic path but loaded from
    somewhere else would satisfy every other test in this file.
    """

    path = harness_root_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "agent_runtime:\n  read_model:\n    delta_patches: false\n", encoding="utf-8"
    )

    cfg = load_root_runtime_config()
    assert cfg.read_model.delta_patches is False
    assert delta_patches_enabled() is False, (
        "the sandbox root config was written and ignored — the reader resolves "
        "one path and loads another"
    )


def test_root_reader_is_immune_to_the_active_profile_redirect(tmp_path, monkeypatch):
    """The sandbox must not FLATTEN the root-vs-profile distinction away.

    Found by mutation, not by inspection. Replacing
    ``load_root_runtime_config``'s body with the profile-aware
    ``load_agent_runtime_config()`` — i.e. reintroducing the exact 2026-07-23
    defect that ``harness_root_config_path`` was created to kill — left every
    other test in this file GREEN. The reason is structural: the sandbox pins
    ``HERMES_HOME`` at a plain directory, so root and profile resolve to the
    SAME ``config.yaml`` and no assertion about the contents can tell which
    resolver produced it. A hermetic suite that also erases the distinction it
    is meant to protect is not an improvement.

    So put ``HERMES_HOME`` in the shape the CLI bootstrap actually leaves it in
    — ``<root>/profiles/<name>``, which ``get_default_hermes_root()`` walks back
    to ``<root>`` — give the two files OPPOSITE values, and require the ROOT one
    to win. Still fully inside ``tmp_path``: hermetic and discriminating are not
    in tension here.
    """

    root = tmp_path / "runtime-root"
    profile = root / "profiles" / "alice"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))

    assert harness_root_config_path() == root / "config.yaml", (
        "the profile-shaped HERMES_HOME did not walk back to its root — the "
        "rest of this test would compare a file against itself"
    )

    (root / "config.yaml").write_text(
        "agent_runtime:\n  read_model:\n    delta_patches: false\n", encoding="utf-8"
    )
    (profile / "config.yaml").write_text(
        "agent_runtime:\n  read_model:\n    delta_patches: true\n", encoding="utf-8"
    )

    assert load_root_runtime_config().read_model.delta_patches is False, (
        "load_root_runtime_config() answered from profiles/alice/config.yaml — "
        "the sticky-active profile is shadowing harness-global policy again"
    )
    assert delta_patches_enabled() is False, (
        "the ROOT_ONLY reader answered from the profile copy, which "
        "config.ROOT_ONLY_CONFIG_KEYS documents as inert"
    )
