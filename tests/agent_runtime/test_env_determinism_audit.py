"""Wave-3 env-determinism audit — the class-2 fixes, both env paths pinned.

The bug class (proven live 2026-07-26, fixed for the terminal envelope by
``terminal_envelope.py``): a reader keys behavior on the AMBIENT PRESENCE of
``HERMES_AGENT_RUNTIME_ROOT`` rather than on validated content resolved through
a declared ladder. Profile-less personas never export it
(``profile_context``'s ``profile_home is None`` early-yield) and a warm
``hermes harness serve`` inherits whatever env history it booted with, so
identical requests behaved differently depending on process ancestry.

Every test here asserts the SAME typed outcome with the variable present and
absent. A test that only covers one branch cannot see this bug class — that is
how it survived.

Full reader-by-reader classification:
``docs/agent-runtime-harness/env-determinism-audit.md``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_runtime import smoke as smoke_module
from agent_runtime import stagec_mcp_visual_provider as stagec
from agent_runtime.terminal_envelope import (
    AUDIT_ROOT_SOURCE_ENV,
    AUDIT_ROOT_SOURCE_RESOLVER,
    AUDIT_ROOT_SOURCE_SCOPE,
    ENVELOPE_DECISION_LOG,
    GIT_PUSH,
    LANE_MISSION_CHAT,
    OUTCOME_REFUSE,
    TerminalEnvelopeDecision,
    TerminalEnvelopeScope,
    audit_root_source,
    record_envelope_decision,
)

RUNTIME_ROOT_ENV = "HERMES_AGENT_RUNTIME_ROOT"


@pytest.fixture
def configured_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Env UNSET, root declared in the root config — the resolver's rung 2.

    Reproduces the live shape the audit is about: a profile-less persona on a
    serve process whose ancestry never exported the variable.
    """

    root = tmp_path / "configured-runtime"
    root.mkdir(parents=True, exist_ok=True)
    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "agent_runtime:\n  store_root: " + json.dumps(str(root)) + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(RUNTIME_ROOT_ENV, raising=False)
    return root


def _refusal() -> TerminalEnvelopeDecision:
    return TerminalEnvelopeDecision(
        outcome=OUTCOME_REFUSE,
        lane=LANE_MISSION_CHAT,
        role="dev",
        command_class=GIT_PUSH,
        persona_id="dev",
        session_id="s1",
    )


def _decision_rows(root: Path) -> list[dict]:
    path = root / ENVELOPE_DECISION_LOG
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── the envelope receipt root ────────────────────────────────────────────────
#
# K's wave-2 fix made the DECISION deterministic. Whether the decision left a
# receipt was still keyed on ambient env presence one layer down: with no
# scope-carried root and no exported variable, the receipt was dropped in
# silence — the same coin flip, applied to the proof instead of the verdict.


def test_receipt_is_written_when_the_env_var_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "env-runtime"
    monkeypatch.setenv(RUNTIME_ROOT_ENV, str(root))

    record_envelope_decision(_refusal(), "git push origin main", scope=None)

    rows = _decision_rows(root)
    assert len(rows) == 1
    assert rows[0]["failure_class"] == _refusal().failure_class
    assert rows[0]["audit_root_source"] == AUDIT_ROOT_SOURCE_ENV


def test_receipt_is_written_when_the_env_var_is_absent(configured_root: Path) -> None:
    """The branch that used to drop the receipt in silence."""

    assert RUNTIME_ROOT_ENV not in os.environ

    record_envelope_decision(_refusal(), "git push origin main", scope=None)

    rows = _decision_rows(configured_root)
    assert len(rows) == 1, (
        "A governed refusal produced NO receipt with the env var unset. The "
        "decision is deterministic; its provenance must be too."
    )
    assert rows[0]["audit_root_source"] == AUDIT_ROOT_SOURCE_RESOLVER


def test_both_env_paths_record_the_same_typed_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_root: Path
) -> None:
    """Present vs absent differ only in WHERE, never in WHAT."""

    record_envelope_decision(_refusal(), "git push origin main", scope=None)
    absent_row = _decision_rows(configured_root)[0]

    env_root = tmp_path / "env-runtime"
    monkeypatch.setenv(RUNTIME_ROOT_ENV, str(env_root))
    record_envelope_decision(_refusal(), "git push origin main", scope=None)
    present_row = _decision_rows(env_root)[0]

    volatile = {"ts", "audit_root_source"}
    assert {k: v for k, v in absent_row.items() if k not in volatile} == {
        k: v for k, v in present_row.items() if k not in volatile
    }


def test_a_scope_carried_root_still_wins_over_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new rung is a FALLBACK; it must not outrank the run's own scope."""

    env_root = tmp_path / "env-runtime"
    scope_root = tmp_path / "scope-runtime"
    monkeypatch.setenv(RUNTIME_ROOT_ENV, str(env_root))
    scope = TerminalEnvelopeScope(
        lane=LANE_MISSION_CHAT, role="dev", runtime_root=str(scope_root)
    )

    assert audit_root_source(scope) == AUDIT_ROOT_SOURCE_SCOPE
    record_envelope_decision(_refusal(), "git push origin main", scope=scope)

    assert len(_decision_rows(scope_root)) == 1
    assert _decision_rows(env_root) == []


def test_recording_never_raises_when_nothing_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auditability must not gate the answer, even when the resolver refuses."""

    monkeypatch.delenv(RUNTIME_ROOT_ENV, raising=False)
    monkeypatch.setattr(
        "agent_runtime.paths.store_root",
        lambda: (_ for _ in ()).throw(RuntimeError("probe isolation")),
    )

    record_envelope_decision(_refusal(), "git push origin main", scope=None)


# ── the smoke runtime root ───────────────────────────────────────────────────
#
# ``--no-temp-root`` used to mean ``os.environ.get(RUNTIME_ROOT_ENV,
# ".hermes-agent-runtime")``: presence-keyed AND, on the fallback branch,
# relative to the process cwd — which mission-chat workdir grounding now
# mutates per turn.


def test_smoke_root_honors_the_env_var_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "env-runtime"
    monkeypatch.setenv(RUNTIME_ROOT_ENV, str(root))

    assert smoke_module._configured_smoke_root() == root


def test_smoke_root_resolves_the_configured_root_when_the_env_var_is_absent(
    configured_root: Path,
) -> None:
    assert smoke_module._configured_smoke_root() == configured_root


def test_smoke_root_is_absolute_and_cwd_independent(
    tmp_path: Path, configured_root: Path
) -> None:
    """The regression J's chdir made live: the old fallback moved with the cwd.

    ``profile_runner._agent_workdir`` chdirs the serve process per grounded
    turn, so a cwd-relative default answered differently depending on which
    persona spoke last in that process.
    """

    here = tmp_path / "cwd-a"
    there = tmp_path / "cwd-b"
    here.mkdir()
    there.mkdir()
    previous = Path.cwd()
    try:
        os.chdir(here)
        first = smoke_module._configured_smoke_root()
        os.chdir(there)
        second = smoke_module._configured_smoke_root()
    finally:
        os.chdir(previous)

    assert first == second == configured_root
    assert first.is_absolute()


# ── the Stage C marionette preflight ─────────────────────────────────────────
#
# Two readers of the same three env keys disagreed: the ENABLE predicate gated
# on bare presence, the RESOLVER required a real directory. A stale value
# inherited from process ancestry switched on a preflight that then rebuilt a
# different repo than the variable named — or none at all. ``flutter build`` is
# not a cheap surprise.

INERT_CONFIG = stagec.StageCMcpServerConfig(name="other", command="node", args=["x.js"])


@pytest.fixture(autouse=True)
def _clear_launcher_repo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in stagec.LAUNCHER_REPO_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("env_key", stagec.LAUNCHER_REPO_ENV_KEYS)
def test_a_real_directory_in_the_env_enables_the_preflight(
    env_key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "launcher"
    repo.mkdir()
    monkeypatch.setenv(env_key, str(repo))

    assert stagec._env_launcher_repo() == repo
    assert stagec._marionette_preflight_enabled_for_config({}, INERT_CONFIG) is True


@pytest.mark.parametrize("env_key", stagec.LAUNCHER_REPO_ENV_KEYS)
def test_a_stale_env_value_no_longer_enables_the_preflight(
    env_key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Presence alone is not a launcher repo."""

    monkeypatch.setenv(env_key, str(tmp_path / "gone"))

    assert stagec._env_launcher_repo() is None
    assert stagec._marionette_preflight_enabled_for_config({}, INERT_CONFIG) is False


def test_the_enable_predicate_and_the_resolver_read_the_env_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One question, one answer — the property whose absence was the bug.

    ``_launcher_repo_from_metadata`` has a further repo-context fallback, so
    the assertion is one-directional on purpose: whenever the ENV rung enables
    the preflight, the resolver must return exactly the repo the env named.
    """

    real = tmp_path / "launcher"
    real.mkdir()
    for value, expected in ((str(real), real), (str(tmp_path / "gone"), None)):
        monkeypatch.setenv("HERMES_LAUNCHER_REPO", value)
        assert stagec._env_launcher_repo() == expected
        if expected is not None:
            assert stagec._launcher_repo_from_metadata({}) == expected
            assert stagec._marionette_preflight_enabled_for_config({}, INERT_CONFIG)


def test_explicit_metadata_still_outranks_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Narrowing the env rung must not weaken an explicit operator pin."""

    monkeypatch.setenv("HERMES_LAUNCHER_REPO", str(tmp_path / "gone"))

    assert (
        stagec._marionette_preflight_enabled_for_config(
            {"launcher_repo": str(tmp_path / "named")}, INERT_CONFIG
        )
        is True
    )


# ── audit Q4: the preflight runtime_root check asserts something real ────────
#
# ``ok = bool(str(store_root()).strip())`` could essentially never fail: the
# resolver's last rung is an unconditional platform default, so the check
# reported ``runtime_root=present`` for a root that does not exist and told the
# operator to configure a variable it never read. It now fails on exactly one
# condition — resolved via the DEFAULT layer and not a store — and reports the
# winning layer, the path, existence and store-shape either way.


@pytest.fixture
def default_layer_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No env root, no configured root ⇒ the resolver's DEFAULT rung wins."""

    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv(RUNTIME_ROOT_ENV, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home / "agent-runtime"


def _runtime_root_check():
    from agent_runtime.preflight import _runtime_root_check

    return _runtime_root_check()


def test_preflight_passes_when_the_env_layer_names_an_existing_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "env-runtime"
    (root / "tasks").mkdir(parents=True)
    monkeypatch.setenv(RUNTIME_ROOT_ENV, str(root))

    check = _runtime_root_check()

    assert check.ok is True
    assert "layer=env" in check.token
    assert str(root) in check.token
    assert "exists=true" in check.token
    assert "store=true" in check.token


def test_preflight_passes_when_the_config_layer_names_an_existing_root(
    configured_root: Path,
) -> None:
    (configured_root / "tasks").mkdir(parents=True, exist_ok=True)

    check = _runtime_root_check()

    assert check.ok is True
    assert "layer=config" in check.token
    assert str(configured_root) in check.token


def test_preflight_now_fails_on_an_uninitialized_default_root(
    default_layer_home: Path,
) -> None:
    """THE new failure. It used to report ``runtime_root=present`` here."""

    check = _runtime_root_check()

    assert check.ok is False
    assert "layer=default" in check.token
    assert "exists=false" in check.token
    assert "store=false" in check.token
    # The fix hint must name what the check actually read.
    assert "agent_runtime.store_root" in check.actionable_fix
    assert str(default_layer_home) in check.detail


def test_preflight_still_passes_on_a_populated_default_root(
    default_layer_home: Path,
) -> None:
    """A machine that never configured a root but HAS a store is fine."""

    (default_layer_home / "tasks").mkdir(parents=True)

    check = _runtime_root_check()

    assert check.ok is True
    assert "layer=default" in check.token
    assert "store=true" in check.token


def test_preflight_reports_an_explicit_root_that_does_not_exist_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit operator statement is honored; a first run creates it.

    The check reports the truth (``exists=false``) without failing — the failure
    is reserved for "nobody said anything AND nothing is there".
    """

    root = tmp_path / "declared-but-absent"
    monkeypatch.setenv(RUNTIME_ROOT_ENV, str(root))

    check = _runtime_root_check()

    assert check.ok is True
    assert "exists=false" in check.token


# ── audit Q5: a smoke run is synthetic, always ───────────────────────────────


def test_smoke_never_writes_into_the_configured_store(
    configured_root: Path,
) -> None:
    """Ruling (a). ``--temp-root=False`` no longer means "pollute the live store"."""

    result = smoke_module.run_smoke(temp_root=False, no_model=True)

    assert result["ok"] is True
    assert result["runtime_root_kind"] == "temp"
    assert Path(result["runtime_root"]) != configured_root
    assert not (configured_root / "tasks").exists()


def test_smoke_reports_the_configured_root_it_left_alone(
    configured_root: Path,
) -> None:
    assert (
        smoke_module.run_smoke(temp_root=True, no_model=True)["configured_runtime_root"]
        == str(configured_root)
    )


def test_smoke_says_out_loud_that_it_ignored_the_flag(configured_root: Path) -> None:
    """Ignoring a flag SILENTLY would be its own small lie."""

    result = smoke_module.run_smoke(temp_root=False, no_model=True)
    rows = result["deprecations"]

    assert [row["code"] for row in rows] == [smoke_module.SMOKE_RUNTIME_ROOT_ALWAYS_TEMP]
    assert rows[0]["subject"] == "--temp-root"
    assert rows[0]["fix_hint"]
    assert "deprecations" not in smoke_module.run_smoke(temp_root=True, no_model=True)


@pytest.mark.parametrize("env_present", [True, False])
def test_smoke_outcome_is_identical_with_and_without_the_env_var(
    env_present: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if env_present:
        monkeypatch.setenv(RUNTIME_ROOT_ENV, str(tmp_path / "env-runtime"))
    else:
        monkeypatch.delenv(RUNTIME_ROOT_ENV, raising=False)

    result = smoke_module.run_smoke(temp_root=False, no_model=True)

    assert result["ok"] is True
    assert result["runtime_root_kind"] == "temp"
    assert result["final_state"] == "done"


def test_smoke_restores_the_ambient_variable_it_borrowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUNTIME_ROOT_ENV, str(tmp_path / "outer"))

    smoke_module.run_smoke(temp_root=True, no_model=True)

    assert os.environ[RUNTIME_ROOT_ENV] == str(tmp_path / "outer")


# ── audit §3: the import-time HERMES_HOME freeze in tools/ ───────────────────
#
# Same ancestry dependence in a different shape. ``tools/skills_sync.py``,
# ``tools/skills_tool.py`` and ``tools/skill_manager_tool.py`` capture
# ``HERMES_HOME`` at IMPORT time into module-level constants, so in a long-lived
# ``hermes harness serve`` the frozen value is whichever home the FIRST import
# saw and every later profile switch is invisible to it.
#
# Two of the three already carry the mitigation (a call-time ``_skills_dir()``
# that honors an explicitly patched constant and otherwise re-resolves from the
# live home). ``skills_sync`` does not, and uses the frozen constants directly
# throughout. ``tools/`` is outside this fork's edit boundary, so the fix ships
# as a documented operator-owed diff — and these tests are what keep that doc
# from rotting into a lie: they pin the shape the diff targets and reproduce the
# staleness live, so a reader can see the bug rather than take the doc's word.


def _tools_source(name: str) -> str:
    import importlib

    module = importlib.import_module(f"tools.{name}")
    return Path(module.__file__).read_text(encoding="utf-8")


def test_skills_sync_still_freezes_hermes_home_at_import_time() -> None:
    """The shape the operator-owed diff targets (audit §3)."""

    source = _tools_source("skills_sync")
    assert "HERMES_HOME = get_hermes_home()" in source
    assert 'SKILLS_DIR = HERMES_HOME / "skills"' in source
    assert 'MANIFEST_FILE = SKILLS_DIR / ".bundled_manifest"' in source
    # No call-time accessor yet — which is exactly why it is the one that bites.
    assert "def _skills_dir(" not in source


def test_the_frozen_constant_does_not_follow_a_profile_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduce the staleness rather than assert it from the source alone."""

    import tools.skills_sync as skills_sync

    frozen = skills_sync.SKILLS_DIR
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "another-profile"))

    assert skills_sync.SKILLS_DIR == frozen
    assert skills_sync.MANIFEST_FILE == frozen / ".bundled_manifest"
    # The live home HAS moved; only this module cannot see it.
    from hermes_constants import get_hermes_home

    assert get_hermes_home() == tmp_path / "another-profile"


@pytest.mark.parametrize("name", ["skills_tool", "skill_manager_tool"])
def test_the_other_two_modules_already_resolve_at_call_time(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mitigation exists here; the doc must keep saying so accurately.

    If one of these LOSES its accessor, this fails — and the operator-owed diff
    in the audit doc grows a second file that nobody would otherwise notice.
    """

    import importlib

    module = importlib.import_module(f"tools.{name}")
    source = _tools_source(name)
    assert "def _skills_dir(" in source
    assert "_SKILLS_DIR_AT_IMPORT" in source

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "another-profile"))
    assert module._skills_dir() == tmp_path / "another-profile" / "skills"


def test_the_fork_side_never_reads_the_frozen_constants() -> None:
    """``agent_runtime`` is immune by construction — keep it that way.

    ``skill_publishability`` imports only ``_dir_hash`` / ``_read_skill_name``
    (pure helpers) and derives every skills root itself, which is why no
    fork-owned call-time accessor is warranted: there is no fork-side reader for
    it to fix. A new import of the frozen names would change that silently.
    """

    import ast

    repo_root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for path in sorted((repo_root / "agent_runtime").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - unreadable source
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith("tools.skill"):
                continue
            for alias in node.names:
                if alias.name in {"HERMES_HOME", "SKILLS_DIR", "MANIFEST_FILE"}:
                    offenders.append(
                        f"{path.relative_to(repo_root).as_posix()}:{node.lineno} "
                        f"imports {alias.name}"
                    )

    assert offenders == [], (
        "agent_runtime imported an import-time-frozen HERMES_HOME constant from "
        "tools/. Resolve the home at call time instead. See "
        "docs/agent-runtime-harness/env-determinism-audit.md §3. Offenders: "
        + ", ".join(offenders)
    )
