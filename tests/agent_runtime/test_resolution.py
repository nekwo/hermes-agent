from __future__ import annotations

import pytest

from agent_runtime.resolution import (
    RuntimeRootMismatch,
    assert_pinned,
    resolution_table,
    resolve_runtime,
    suspect_default_root,
)
from agent_runtime.snapshot import build_snapshot
from agent_runtime.parse_cache import clear_parse_cache
from agent_runtime.config import AgentRuntimeConfig


def test_resolve_runtime_env_layer_wins(tmp_path):
    root = tmp_path / "runtime-env"
    home = tmp_path / "home"

    resolution = resolve_runtime({"HERMES_AGENT_RUNTIME_ROOT": str(root), "HERMES_HOME": str(home)})

    assert resolution.store_root == root
    assert resolution.layer == "env"
    assert resolution.trace == (
        f"env HERMES_AGENT_RUNTIME_ROOT won: {root}",
        f"config {home / 'config.yaml'} skipped: env won",
        "default skipped: env won",
    )


def test_resolve_runtime_config_layer_wins(tmp_path):
    home = tmp_path / "home"
    root = tmp_path / "runtime-config"
    home.mkdir()
    (home / "config.yaml").write_text(f"agent_runtime:\n  store_root: '{root}'\n", encoding="utf-8")
    clear_parse_cache()

    resolution = resolve_runtime({"HERMES_HOME": str(home)})

    assert resolution.store_root == root
    assert resolution.layer == "config"
    assert resolution.trace[0] == "env HERMES_AGENT_RUNTIME_ROOT skipped: unset"
    assert resolution.trace[1] == f"config agent_runtime.store_root won: {root}"
    assert resolution.trace[2] == "default skipped: config won"


def test_resolve_runtime_default_layer_wins(tmp_path):
    home = tmp_path / "home"

    resolution = resolve_runtime({"HERMES_HOME": str(home)})

    assert resolution.store_root == home / "agent-runtime"
    assert resolution.layer == "default"
    assert resolution.trace == (
        "env HERMES_AGENT_RUNTIME_ROOT skipped: unset",
        f"config agent_runtime.store_root skipped: unset ({home / 'config.yaml'})",
        f"default won: {home / 'agent-runtime'}",
    )


def test_assert_pinned_raises_typed_mismatch(tmp_path):
    resolution = resolve_runtime({"HERMES_AGENT_RUNTIME_ROOT": str(tmp_path / "actual")})

    with pytest.raises(RuntimeRootMismatch):
        assert_pinned(resolution, pinned_root=str(tmp_path / "expected"))


def test_suspect_default_root_and_parity_warning(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_AGENT_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    clear_parse_cache()

    resolution = resolve_runtime()

    assert resolution.layer == "default"
    assert suspect_default_root(resolution) is True
    snapshot = build_snapshot()
    assert "suspect_default_root" in {warning["code"] for warning in snapshot["parity"]["warnings"]}
    assert snapshot["parity"]["resolution"]["layer"] == "default"


def test_default_root_with_store_marker_is_not_suspect(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_AGENT_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    clear_parse_cache()
    # A chat-only store has persona/chat state, never tasks/ — any one marker
    # directory is proof the default-resolved root is the real store.
    (tmp_path / "home" / "agent-runtime" / "persona_instances").mkdir(parents=True)

    resolution = resolve_runtime()

    assert resolution.layer == "default"
    assert suspect_default_root(resolution) is False
    snapshot = build_snapshot()
    assert "suspect_default_root" not in {warning["code"] for warning in snapshot["parity"]["warnings"]}


def test_resolution_table_marks_winner_without_mission_columns(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir(parents=True)

    rows = resolution_table({"HERMES_AGENT_RUNTIME_ROOT": str(root), "HERMES_HOME": str(tmp_path / "home")})

    env_row = next(row for row in rows if row["layer"] == "env")
    assert env_row["winner"] is True
    assert env_row["exists"] is True
    # The tasks/ directory probe left with the mission lane (doc 16); the row
    # carries only layer/value/exists/winner now.
    assert "tasks" not in env_row
    assert set(env_row) == {"layer", "value", "exists", "winner"}
    assert [row["layer"] for row in rows] == ["env", "config", "default"]
