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
