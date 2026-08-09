"""Pins for the venv-mutation barrier (2026-08-09 incident).

The rule these hold: a live turn must never mutate the venv it is running in.
Every test proves the refusal happens WITHOUT running pip — the fixture always
declares a genuinely-absent distribution, so a barrier that stopped working
would reach the real installer rather than quietly pass.
"""

from __future__ import annotations

import threading

import pytest

from tools import lazy_deps


ABSENT_FEATURE = "test.absent_distribution"
ABSENT_SPEC = "hermes-test-absent-distribution==9.9.9"


@pytest.fixture
def absent_feature(monkeypatch):
    monkeypatch.setitem(lazy_deps.LAZY_DEPS, ABSENT_FEATURE, (ABSENT_SPEC,))
    return ABSENT_FEATURE


@pytest.fixture(autouse=True)
def _no_real_pip(monkeypatch):
    """Any install that escapes the barrier fails the test loudly."""

    def _explode(*args, **kwargs):
        raise AssertionError("a venv install escaped the barrier and reached pip")

    monkeypatch.setattr(lazy_deps, "_venv_pip_install", _explode)
    return _explode


def test_barrier_is_disarmed_by_default():
    assert lazy_deps.venv_install_denial() is None
    assert lazy_deps.venv_mutation_denial() is None


def test_ensure_refuses_inside_a_barrier(absent_feature):
    with lazy_deps.deny_venv_installs("an agent turn (profile='base')"):
        with pytest.raises(lazy_deps.RuntimeInstallDenied) as caught:
            lazy_deps.ensure(absent_feature, prompt=False)

    message = str(caught.value)
    assert "an agent turn (profile='base')" in message
    # The operator must be handed the command, not just the refusal.
    assert ABSENT_SPEC in message


def test_refusal_is_a_feature_unavailable_so_existing_call_sites_degrade(absent_feature):
    # ~40 backends already handle FeatureUnavailable by reporting themselves
    # unavailable. If the refusal stopped being one, they would all raise.
    with lazy_deps.deny_venv_installs("an agent turn"):
        with pytest.raises(lazy_deps.FeatureUnavailable):
            lazy_deps.ensure(absent_feature, prompt=False)


def test_ensure_is_a_no_op_for_an_already_satisfied_feature(monkeypatch):
    # Every turn calls ensure() for features that ARE installed; the barrier
    # must not turn those into failures or no turn could run.
    monkeypatch.setitem(lazy_deps.LAZY_DEPS, ABSENT_FEATURE, ("pytest",))
    with lazy_deps.deny_venv_installs("an agent turn"):
        lazy_deps.ensure(ABSENT_FEATURE, prompt=False)


def test_install_specs_reports_blocked_instead_of_installing():
    with lazy_deps.deny_venv_installs("an agent turn"):
        outcome = lazy_deps.install_specs([ABSENT_SPEC])

    assert outcome.ok is False
    assert outcome.blocked is True
    assert "must never mutate" in outcome.reason


def test_ensure_does_not_prompt_inside_a_barrier(absent_feature, monkeypatch):
    # A refusal that arrived AFTER the confirmation prompt would block a
    # headless turn on stdin forever.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr(
        "builtins.input",
        lambda *_: pytest.fail("the barrier prompted for confirmation inside a turn"),
    )
    with lazy_deps.deny_venv_installs("an agent turn"):
        with pytest.raises(lazy_deps.RuntimeInstallDenied):
            lazy_deps.ensure(absent_feature, prompt=True)


def test_durable_target_installs_stay_allowed(absent_feature, monkeypatch):
    # A --target install is append-only on sys.path and cannot shadow the core
    # venv, so the immutable Docker deployment keeps working under a barrier.
    monkeypatch.setenv("HERMES_LAZY_INSTALL_TARGET", "/opt/data/lazy-packages")
    with lazy_deps.deny_venv_installs("an agent turn"):
        assert lazy_deps.venv_install_denial() is not None
        assert lazy_deps.venv_mutation_denial() is None


def test_barrier_holds_on_worker_threads(absent_feature):
    # An agent runs its tools on worker threads — the exact place a mid-turn
    # install fires. A ContextVar-based barrier would not follow them.
    seen: list[object] = []

    def _probe():
        try:
            lazy_deps.ensure(absent_feature, prompt=False)
            seen.append(None)
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
            seen.append(exc)

    with lazy_deps.deny_venv_installs("an agent turn"):
        worker = threading.Thread(target=_probe)
        worker.start()
        worker.join(timeout=30)

    assert len(seen) == 1
    assert isinstance(seen[0], lazy_deps.RuntimeInstallDenied)


def test_nested_barriers_restore_the_outer_scope():
    with lazy_deps.deny_venv_installs("outer turn"):
        with lazy_deps.deny_venv_installs("inner subagent turn"):
            assert lazy_deps.venv_install_denial() == "inner subagent turn"
        assert lazy_deps.venv_install_denial() == "outer turn"
    assert lazy_deps.venv_install_denial() is None


def test_barrier_is_released_when_the_scope_raises():
    with pytest.raises(RuntimeError):
        with lazy_deps.deny_venv_installs("a turn that failed"):
            raise RuntimeError("turn blew up")

    assert lazy_deps.venv_install_denial() is None


def test_venv_scoped_pip_install_is_refused_at_the_last_line_of_defence(monkeypatch):
    # _venv_pip_install is the ladder every path converges on; a future caller
    # that skips ensure()/install_specs() must still be refused.
    monkeypatch.undo()  # drop the _no_real_pip stub for this one probe
    monkeypatch.setattr(
        lazy_deps.subprocess,
        "run",
        lambda *a, **k: pytest.fail("pip ran under an armed barrier"),
    )
    with lazy_deps.deny_venv_installs("an agent turn"):
        result = lazy_deps._venv_pip_install((ABSENT_SPEC,))

    assert result.success is False
    assert "must never mutate" in result.stderr


def test_tools_config_pip_install_refuses_venv_scoped_installs(monkeypatch):
    from hermes_cli import tools_config

    monkeypatch.setattr(
        tools_config.subprocess,
        "run",
        lambda *a, **k: pytest.fail("pip ran under an armed barrier"),
    )
    with lazy_deps.deny_venv_installs("an agent turn"):
        result = tools_config._pip_install(["some-package"])

    assert result.returncode == 1
    assert "must never mutate" in result.stderr


def test_tools_config_target_installs_stay_allowed(monkeypatch):
    # The LSP server installer writes to a hermes-owned --target dir, which
    # cannot corrupt the running venv. Blocking it would break language-server
    # autostart for no safety gain.
    from hermes_cli import tools_config

    calls: list[list[str]] = []

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        return _Ok()

    monkeypatch.setattr(tools_config.shutil, "which", lambda name: None)
    monkeypatch.setattr(tools_config.subprocess, "run", _fake_run)
    with lazy_deps.deny_venv_installs("an agent turn"):
        result = tools_config._pip_install(["--target", "/tmp/lsp", "pyright"])

    assert result.returncode == 0
    assert any("--target" in call for call in calls)
