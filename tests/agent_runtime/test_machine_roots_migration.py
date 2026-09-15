"""Contract tests for the absolute-path -> token-form config migration.

The migration is a text rewrite of files that carry hand-written comments, so
the safety property is not "the code looks right" but "the rewritten file
re-expands to the same document". Every planned file carries its own
verification result and an unverified plan is never written.

THE HOST THESE TESTS SPEAK FOR
------------------------------
The fixture config below is the live WINDOWS profile: a ``.exe`` command, a
``.ps1`` helper, and the same root written in both separator styles. That is
not an arbitrary choice — the migration derives its roots from paths that must
exist on the machine running it (``suggest_roots_from_configs`` stats them),
so a Windows config is migrated on the Windows box, and verification is
correctly HOST-RELATIVE: ``expand_config_paths`` resolves ``${exe_suffix}`` to
``.exe`` on Windows and to the empty string everywhere else.

So ``_windows_host`` pins ``current_platform_key`` for the module. Without it
these tests were green on a developer's Windows box and red on every Linux
runner — 6 of them in run 33969282189 — for a difference that says nothing
about the migration. The pin is stated, not hidden:
:func:`test_expansion_is_host_relative_and_that_is_why_the_pin_exists` turns it
off and asserts the POSIX answer.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from agent_runtime import machine_roots
from agent_runtime.machine_roots import MachineRoots, machine_roots_cache_clear
from agent_runtime.machine_roots_migration import (
    _absolute_paths_in,
    apply_config_migration,
    plan_config_migration,
    snake_case_root_name,
    suggest_roots_from_configs,
    tokenize_text,
    unmapped_absolute_paths,
    verify_roundtrip,
)


@pytest.fixture(autouse=True)
def _clear_roots_cache():
    machine_roots_cache_clear()
    yield
    machine_roots_cache_clear()


@pytest.fixture(autouse=True)
def _windows_host(monkeypatch):
    """Migrate the Windows fixture config as the Windows box would. See the
    module docstring: ``${exe_suffix}`` is host-relative by design."""

    monkeypatch.setattr(
        machine_roots, "current_platform_key", lambda: machine_roots.PLATFORM_WINDOWS
    )


def _launcher_repo(tmp_path: Path) -> Path:
    # The real root is "X:\\Unreal Engine\\...": a SPACE in the path is the
    # normal case, not an edge case, and a discovery pattern that stops at
    # whitespace finds nothing to migrate.
    repo = tmp_path / "Unreal Engine" / "EterniaLauncher"
    (repo / "tool" / "stagec_qa_mcp_server" / "build").mkdir(parents=True)
    (repo / "docs" / "stages" / "qa-reboot" / "scripts").mkdir(parents=True)
    (repo / ".git").mkdir()
    return repo


def _config_text(repo: Path) -> str:
    win = str(repo).replace("/", "\\")
    posix = str(repo).replace("\\", "/")
    return textwrap.dedent(
        f"""\
        # Hermes profile config
        mcp_servers:
          launcher_qa:
            command: {win}\\tool\\stagec_qa_mcp_server\\build\\stagec_qa_mcp_server.exe
            args: []
            env:
              STAGEC_QA_REPO_ROOT: {win}
              STAGEC_QA_TRANSPORT: direct_control
              STAGEC_SCREENSHOT_HELPER: {win}\\docs\\stages\\qa-reboot\\scripts\\Capture-StageCWindowScreenshot.ps1
            timeout: 260
          portable_thing:
            command: node
            args:
              - server.js
        agent_runtime:
          personas:
            dev:
              repo_scope: {posix}
              repo_scope_label: EterniaLauncher
        """
    )


# ── Tokenization ────────────────────────────────────────────────────────────


def test_snake_case_root_name_handles_camel_and_kebab():
    assert snake_case_root_name("EterniaLauncher") == "eternia_launcher"
    assert snake_case_root_name("eternia-backend") == "eternia_backend"
    assert snake_case_root_name("hermes-agent") == "hermes_agent"


def test_tokenize_replaces_both_separator_styles_and_the_exe_suffix(tmp_path):
    repo = _launcher_repo(tmp_path)
    roots = MachineRoots(roots={"eternia_launcher": str(repo)})
    after, count = tokenize_text(_config_text(repo), roots)

    assert "${roots.eternia_launcher}" in after
    assert "stagec_qa_mcp_server${exe_suffix}" in after
    # Both the backslash mcp_servers refs and the forward-slash repo_scope ref.
    assert str(repo).replace("/", "\\") not in after
    assert str(repo).replace("\\", "/") not in after
    assert count >= 5


def test_tokenize_leaves_unrelated_absolute_paths_alone(tmp_path):
    repo = _launcher_repo(tmp_path)
    roots = MachineRoots(roots={"eternia_launcher": str(repo)})
    text = "command: C:\\Windows\\System32\\where.exe\n"
    after, count = tokenize_text(text, roots)
    assert after == text
    assert count == 0


def test_suggest_roots_walks_up_to_the_nearest_git_checkout(tmp_path):
    repo = _launcher_repo(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(_config_text(repo), encoding="utf-8")
    assert suggest_roots_from_configs([config]) == {"eternia_launcher": str(repo)}


def test_unmapped_absolute_paths_reports_residue_honestly(tmp_path):
    repo = _launcher_repo(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        _config_text(repo) + "  other:\n    command: C:\\Tools\\thing.exe\n", encoding="utf-8"
    )
    residue = unmapped_absolute_paths([config], MachineRoots(roots={"eternia_launcher": str(repo)}))
    assert any("Tools" in item for item in residue)
    assert not any(str(repo) in item for item in residue)


def test_a_posix_root_is_bound_from_its_leading_slash(tmp_path):
    """The second machine is a Mac, so its bound roots are POSIX-rooted.

    Nothing here depends on the host: the root is a literal, and neither
    ``unmapped_absolute_paths`` nor ``tokenize_text`` stats it. What made the
    sibling above green on Windows and red on Linux is that ``tmp_path`` there
    is drive-rooted, so its pattern kept an anchor (``X:``) that every POSIX
    root loses — see ``_root_pattern``. Both readers of that pattern are
    asserted, because they were wrong in different directions: the residue
    report called a bound root unmapped, and the rewrite left the slash behind
    and wrote a token that re-expands to ``//Users/...``.
    """

    root = "/Users/tony/My Projects/EterniaLauncher"
    roots = MachineRoots(roots={"eternia_launcher": root})
    config = tmp_path / "config.yaml"
    config.write_text(
        "agent_runtime:\n"
        "  personas:\n"
        "    dev:\n"
        f"      repo_scope: {root}\n"
        "  other:\n"
        "    command: /opt/tools/thing\n",
        encoding="utf-8",
    )

    assert unmapped_absolute_paths([config], roots) == ["/opt/tools/thing"]

    after, count = tokenize_text(f"repo_scope: {root}\n", roots)
    assert after == "repo_scope: ${roots.eternia_launcher}\n"
    assert count == 1


@pytest.mark.parametrize(
    "text, expected",
    [
        # The drive-letter half already tolerates spaces — that is what
        # `_ABS_PATH_RE`'s own comment says it exists to do.
        (
            "  root: X:\\Unreal Engine\\EterniaLauncher\n",
            ["X:\\Unreal Engine\\EterniaLauncher"],
        ),
        # The POSIX half has to say the same thing. A macOS checkout under
        # "My Projects" is the ordinary case, not an edge case, and a pattern
        # that stops at the first space discovers "/Users/tony/My" — a path
        # that does not exist, so the migration silently finds no root.
        (
            "  root: /Users/tony/My Projects/EterniaLauncher\n",
            ["/Users/tony/My Projects/EterniaLauncher"],
        ),
        # And the same shape as a Linux CI runner's tmp tree, which is where
        # this asymmetry was found (run 33969282189, slice 6).
        (
            "  root: /tmp/pytest-1/Unreal Engine/EterniaLauncher\n",
            ["/tmp/pytest-1/Unreal Engine/EterniaLauncher"],
        ),
    ],
)
def test_absolute_paths_with_spaces_are_found_whole_in_both_shapes(text, expected):
    assert _absolute_paths_in(text) == expected


def test_slashes_in_prose_are_not_absolute_paths():
    """The guard the space tolerance must not cost.

    Two slashes in a sentence are the shape a naive widening turns into a
    two-segment path. The rule that stops it is that a segment may contain a
    space but may never OPEN with one — the case below has three slashes, so a
    class of ``[A-Za-z0-9_.\\- ]+`` alone would match ``/ split the diff /``.
    """

    assert _absolute_paths_in("# raise the budget / split the diff / stop\n") == []
    assert _absolute_paths_in("# see docs/tooling and/or the queue\n") == []
    assert _absolute_paths_in("# a / b / c\n") == []


def test_url_schemes_are_not_mistaken_for_drive_letters(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "base_url: https://chatgpt.com/backend-api/codex\nimage: docker://nikolaik/python\n",
        encoding="utf-8",
    )
    assert unmapped_absolute_paths([config], MachineRoots(roots={})) == []
    assert suggest_roots_from_configs([config]) == {}


# ── Verification ────────────────────────────────────────────────────────────


def test_expansion_is_host_relative_and_that_is_why_the_pin_exists(tmp_path, monkeypatch):
    """The module's ``_windows_host`` pin, stated as a fact rather than hidden.

    ``${exe_suffix}`` means "the executable suffix on the machine that will run
    this config". Verifying a Windows rewrite therefore only round-trips on a
    Windows host, and that is correct behaviour, not a bug: off Windows the
    token expands to the empty string and verification honestly reports the
    ``.exe`` as changed. The pin exists so the other tests in this file assert
    the migration, not the runner's OS.
    """

    repo = _launcher_repo(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(_config_text(repo), encoding="utf-8")
    roots = MachineRoots(roots={"eternia_launcher": str(repo)})

    # With the pin (the module default): verified.
    assert plan_config_migration([config], roots).files[0].verification == ()

    # Without it, on a POSIX host: the .exe is reported, and nothing is written.
    monkeypatch.setattr(
        machine_roots, "current_platform_key", lambda: machine_roots.PLATFORM_LINUX
    )
    posix_plan = plan_config_migration([config], roots)
    problems = posix_plan.files[0].verification
    assert problems and ".exe" in problems[0]
    assert posix_plan.safe is False


def test_plan_verifies_the_rewrite_reexpands_to_the_original_document(tmp_path):
    repo = _launcher_repo(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(_config_text(repo), encoding="utf-8")
    roots = MachineRoots(roots={"eternia_launcher": str(repo)})

    plan = plan_config_migration([config], roots)
    migration = plan.files[0]
    assert migration.changed is True
    assert migration.verification == ()
    assert plan.safe is True
    assert migration.diff().startswith("---")


def test_verification_catches_a_rewrite_that_changed_meaning(tmp_path):
    repo = _launcher_repo(tmp_path)
    roots = MachineRoots(roots={"eternia_launcher": str(repo)})
    before = _config_text(repo)
    after, _count = tokenize_text(before, roots)
    sabotaged = after.replace("timeout: 260", "timeout: 999")
    problems = verify_roundtrip(before, sabotaged, roots)
    assert problems and "timeout" in problems[0]


def test_windows_only_entries_get_a_platform_gate_and_portable_ones_do_not(tmp_path):
    repo = _launcher_repo(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(_config_text(repo), encoding="utf-8")
    roots = MachineRoots(roots={"eternia_launcher": str(repo)})

    migration = plan_config_migration([config], roots).files[0]
    assert migration.platform_gates == ("launcher_qa",)
    assert migration.verification == ()
    document = yaml.safe_load(migration.after)
    assert document["mcp_servers"]["launcher_qa"]["platforms"] == ["windows"]
    assert "platforms" not in document["mcp_servers"]["portable_thing"]


def test_platform_gates_can_be_declined(tmp_path):
    repo = _launcher_repo(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(_config_text(repo), encoding="utf-8")
    roots = MachineRoots(roots={"eternia_launcher": str(repo)})

    migration = plan_config_migration([config], roots, add_platform_gates=False).files[0]
    assert migration.platform_gates == ()
    assert "platforms" not in yaml.safe_load(migration.after)["mcp_servers"]["launcher_qa"]


# ── Dry run ─────────────────────────────────────────────────────────────────


def test_dry_run_writes_neither_the_configs_nor_the_registry(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    machine_roots_cache_clear()

    repo = _launcher_repo(tmp_path)
    config = tmp_path / "config.yaml"
    original = _config_text(repo)
    config.write_text(original, encoding="utf-8")
    roots = MachineRoots(roots={"eternia_launcher": str(repo)})

    plan = plan_config_migration([config], roots)
    outcome = apply_config_migration(plan, dry_run=True)

    assert outcome["dry_run"] is True
    assert outcome["written"] == []
    assert config.read_text(encoding="utf-8") == original
    assert outcome["registry"]["written"] is False
    assert not Path(outcome["registry"]["path"]).exists()


def test_apply_writes_only_when_dry_run_is_false(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    machine_roots_cache_clear()

    repo = _launcher_repo(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(_config_text(repo), encoding="utf-8")
    roots = MachineRoots(roots={"eternia_launcher": str(repo)})

    plan = plan_config_migration([config], roots)
    outcome = apply_config_migration(plan, dry_run=False)

    assert outcome["written"] == [str(config)]
    assert "${roots.eternia_launcher}" in config.read_text(encoding="utf-8")
    assert Path(outcome["registry"]["path"]).exists()


def test_unsafe_plan_is_never_written(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    machine_roots_cache_clear()

    repo = _launcher_repo(tmp_path)
    config = tmp_path / "config.yaml"
    original = _config_text(repo)
    config.write_text(original, encoding="utf-8")
    roots = MachineRoots(roots={"eternia_launcher": str(repo)})

    plan = plan_config_migration([config], roots)
    broken = plan.files[0]
    sabotaged = type(broken)(
        path=broken.path,
        before=broken.before,
        after=broken.after,
        replacements=broken.replacements,
        platform_gates=broken.platform_gates,
        verification=("mcp_servers.launcher_qa.timeout: 260 -> 999",),
    )
    unsafe_plan = type(plan)(roots=plan.roots, files=(sabotaged,), registry=plan.registry)

    outcome = apply_config_migration(unsafe_plan, dry_run=False)
    assert outcome["error"] == "migration_verification_failed"
    assert outcome["written"] == []
    assert config.read_text(encoding="utf-8") == original
