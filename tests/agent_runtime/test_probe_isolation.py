"""Probe-isolation guard: a run that demands an isolated root must never resolve the
live/default store. Retires the Stage-C leak that persisted ``codex_*_probe`` persona
instances into the live store (see ``docs/agent-runtime-harness/orphan-prune.md``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime import paths
from agent_runtime.errors import ProbeIsolationViolation
from agent_runtime.resolution import (
    PROBE_ISOLATION_ENV,
    assert_probe_isolation,
    probe_isolation_required,
    resolve_runtime,
    runtime_resolution_scope,
)


def _env(**over) -> dict[str, str]:
    env: dict[str, str] = {}
    env.update(over)
    return env


def test_marker_unset_is_noop(tmp_path):
    # No marker → guard is a pure no-op regardless of where the root resolves.
    env = _env(HERMES_AGENT_RUNTIME_ROOT=str(tmp_path / "agent-runtime"))
    assert probe_isolation_required(env) is False
    assert_probe_isolation(resolve_runtime(env), env=env)  # does not raise


def test_marker_falsey_is_noop(tmp_path):
    env = _env(
        HERMES_AGENT_RUNTIME_ROOT=str(tmp_path / "agent-runtime"),
        HERMES_REQUIRE_ISOLATED_ROOT="0",
    )
    assert probe_isolation_required(env) is False
    assert_probe_isolation(resolve_runtime(env), env=env)


def test_marker_with_live_shaped_root_raises(tmp_path):
    # Marker set but the env root is not a probe root → the run would touch live → raise.
    env = _env(
        HERMES_AGENT_RUNTIME_ROOT=str(tmp_path / "agent-runtime"),
        HERMES_REQUIRE_ISOLATED_ROOT="1",
    )
    assert probe_isolation_required(env) is True
    with pytest.raises(ProbeIsolationViolation):
        assert_probe_isolation(resolve_runtime(env), env=env)


def test_marker_with_default_layer_raises(tmp_path, monkeypatch):
    # Marker set, no env root pin at all → falls through to the default/live layer → raise.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    env = _env(
        HERMES_REQUIRE_ISOLATED_ROOT="1",
        LOCALAPPDATA=str(tmp_path / "AppData"),
    )
    resolution = resolve_runtime(env)
    assert resolution.layer == "default"
    with pytest.raises(ProbeIsolationViolation):
        assert_probe_isolation(resolution, env=env)


def test_marker_with_probe_root_proceeds(tmp_path):
    probe_root = tmp_path / "agent-runtime-probe-abc123"
    env = _env(
        HERMES_AGENT_RUNTIME_ROOT=str(probe_root),
        HERMES_REQUIRE_ISOLATED_ROOT="1",
    )
    resolution = resolve_runtime(env)
    assert resolution.layer == "env"
    assert_probe_isolation(resolution, env=env)  # does not raise


def test_store_root_enforces_isolation(tmp_path, monkeypatch):
    # Integration: the guard is wired at the single store_root() chokepoint, so a probe
    # that sets the marker but resolves a live-shaped root cannot perform any store I/O.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "agent-runtime"))
    monkeypatch.setenv(PROBE_ISOLATION_ENV, "1")
    with pytest.raises(ProbeIsolationViolation):
        paths.store_root()

    # Repoint at a probe root and it proceeds.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "agent-runtime-probe-xyz"))
    assert isinstance(paths.store_root(), Path)


def test_probe_isolation_validates_the_scoped_resolution(tmp_path, monkeypatch):
    live = resolve_runtime(
        {"HERMES_AGENT_RUNTIME_ROOT": str(tmp_path / "agent-runtime")}
    )
    monkeypatch.setenv(PROBE_ISOLATION_ENV, "1")
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "agent-runtime-probe-safe"))

    with runtime_resolution_scope(live):
        with pytest.raises(ProbeIsolationViolation):
            paths.store_root()
