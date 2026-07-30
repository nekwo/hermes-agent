"""Mission-chat repo grounding — the workdir ladder and its typed degrade (G6).

``mission_chat_reply`` built its ``AgentRunRequest`` with no ``workdir``, so a
turn ran in whatever cwd the serve process happened to hold and every relative
path the agent used resolved against *that* (mission-chat-lane-gap-audit.md G6).
The fix reuses the existing seam — ``AgentRunRequest.workdir``, which
``profile_runner`` already honors with chdir + ``TERMINAL_CWD`` — and adds no
parallel one.

Two invariants these tests exist to hold:

1. **Grounding is a ladder, and each rung is provable.** config workdir →
   workspace pointer → persona repo_scope → the process cwd.
2. **A broken configuration degrades, loudly.** A configured workdir that does
   not exist can never fail a turn, and can never be silent either.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agent_runtime.config import load_agent_runtime_config, mission_chat_workdir
from agent_runtime.mission_chat_workdir import (
    MISSION_CHAT_WORKDIR_UNRESOLVED,
    WORKDIR_REASON_MISSING,
    WORKDIR_REASON_NOT_ABSOLUTE,
    WORKDIR_REASON_NOT_A_DIRECTORY,
    WORKDIR_SOURCE_PERSONA_CONFIG,
    WORKDIR_SOURCE_PERSONA_REPO_SCOPE,
    WORKDIR_SOURCE_PROCESS_CWD,
    WORKDIR_SOURCE_WORKSPACE_AGENTS,
    mission_chat_workdir_for_persona,
    persona_workdir_config_key,
    resolve_mission_chat_workdir,
)
from tests.agent_runtime.persona_samples import sample_personas
from agent_runtime.profile_runner import AgentRunResult
from agent_runtime.mcp_lane import HARNESS_LANE
from agent_runtime.tool_visibility import ToolVisibilityOptions, resolve_tool_visibility


def _persona(persona_id: str):
    return {persona.id: persona for persona in sample_personas()}[persona_id]


def _same(path: str | None, expected: Path) -> bool:
    return path is not None and Path(path) == expected.resolve()


# ── the ladder ──────────────────────────────────────────────────────────────


def test_configured_workdir_wins(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "workspace"
    other.mkdir()

    resolved = resolve_mission_chat_workdir(
        persona_id="dev",
        configured=str(repo),
        workspace_agents_path=str(other / "AGENTS.md"),
        repo_scope=str(other),
    )

    assert _same(resolved.path, repo)
    assert resolved.source == WORKDIR_SOURCE_PERSONA_CONFIG
    assert resolved.issues == ()
    assert resolved.grounded is True


def test_workspace_pointer_grounds_the_turn_when_nothing_is_configured(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    resolved = resolve_mission_chat_workdir(
        persona_id="dev", workspace_agents_path=str(workspace / "AGENTS.md")
    )

    assert _same(resolved.path, workspace)
    assert resolved.source == WORKDIR_SOURCE_WORKSPACE_AGENTS


def test_repo_scope_is_the_last_rung(tmp_path):
    repo = tmp_path / "product"
    repo.mkdir()

    resolved = resolve_mission_chat_workdir(persona_id="dev", repo_scope=str(repo))

    assert _same(resolved.path, repo)
    assert resolved.source == WORKDIR_SOURCE_PERSONA_REPO_SCOPE


def test_nothing_configured_keeps_the_process_cwd():
    # The pre-G6 behavior, preserved exactly: no workdir is passed, so
    # profile_runner never chdirs and never exports TERMINAL_CWD.
    resolved = resolve_mission_chat_workdir(persona_id="dev")

    assert resolved.path is None
    assert resolved.grounded is False
    assert resolved.source == WORKDIR_SOURCE_PROCESS_CWD
    assert resolved.rows() == []


# ── the typed degrade ───────────────────────────────────────────────────────


def test_missing_configured_workdir_falls_to_safe_cwd_with_a_typed_row(tmp_path):
    resolved = resolve_mission_chat_workdir(
        persona_id="dev", configured=str(tmp_path / "not-here")
    )

    assert resolved.path is None
    assert resolved.source == WORKDIR_SOURCE_PROCESS_CWD
    row = resolved.rows(entry_point_lane=HARNESS_LANE)[0]
    assert row["code"] == MISSION_CHAT_WORKDIR_UNRESOLVED
    assert row["reason"] == WORKDIR_REASON_MISSING
    assert row["workdir"].endswith("not-here")
    assert row["entry_point_lane"] == HARNESS_LANE
    assert row["configured_via"] == "agent_runtime.personas.dev.workdir"
    assert row["configured_via"] in row["fix_hint"]


def test_a_file_is_not_a_workdir(tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text("not a directory", encoding="utf-8")

    resolved = resolve_mission_chat_workdir(persona_id="dev", configured=str(target))

    assert resolved.path is None
    assert resolved.rows()[0]["reason"] == WORKDIR_REASON_NOT_A_DIRECTORY


def test_a_relative_workdir_is_refused_not_guessed():
    # Resolving a relative path against the serve process cwd would ground the
    # turn somewhere nobody chose — the exact bug G6 is about.
    resolved = resolve_mission_chat_workdir(persona_id="dev", configured="./repo")

    assert resolved.path is None
    assert resolved.rows()[0]["reason"] == WORKDIR_REASON_NOT_ABSOLUTE


def test_an_unexpanded_machine_root_token_is_refused(tmp_path):
    # config.py leaves an unbindable ${roots.x} token LITERAL rather than
    # fabricating a path; it must never be mistaken for a real directory.
    resolved = resolve_mission_chat_workdir(
        persona_id="dev", configured="${roots.launcher}/EterniaLauncher"
    )

    assert resolved.path is None
    assert resolved.rows()[0]["code"] == MISSION_CHAT_WORKDIR_UNRESOLVED


def test_a_broken_config_still_degrades_to_the_next_rung(tmp_path):
    repo = tmp_path / "product"
    repo.mkdir()

    resolved = resolve_mission_chat_workdir(
        persona_id="dev", configured=str(tmp_path / "gone"), repo_scope=str(repo)
    )

    assert _same(resolved.path, repo)
    assert resolved.source == WORKDIR_SOURCE_PERSONA_REPO_SCOPE
    # The row still fires, and says what it actually fell back to.
    assert resolved.issues[0].fell_back_to == WORKDIR_SOURCE_PERSONA_REPO_SCOPE
    assert "persona repo scope" in resolved.rows()[0]["summary"]


def test_an_unresolvable_repo_scope_is_not_re_typed_here(tmp_path):
    # machine_roots / profile_readiness already own that taxonomy; minting a
    # second row for one fact is how authorities multiply.
    resolved = resolve_mission_chat_workdir(
        persona_id="dev", repo_scope=str(tmp_path / "gone")
    )

    assert resolved.path is None
    assert resolved.rows() == []


def test_receipt_is_stable_and_machine_readable(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    receipt = resolve_mission_chat_workdir(
        persona_id="dev", configured=str(repo)
    ).receipt()

    assert receipt["grounded"] is True
    assert receipt["source"] == WORKDIR_SOURCE_PERSONA_CONFIG
    assert receipt["issues"] == []


# ── the config key ──────────────────────────────────────────────────────────


def _write_config(text: str):
    from hermes_constants import get_config_path

    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_config_reader_reads_the_per_persona_key(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = _write_config(
        f"""
        agent_runtime:
          personas:
            dev:
              workdir: {repo.as_posix()}
        """
    )
    cfg = load_agent_runtime_config(config_path=path)

    assert Path(mission_chat_workdir("dev", cfg)) == Path(repo.as_posix())


def test_config_reader_honors_the_alice_neko_alias(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = _write_config(
        f"""
        agent_runtime:
          personas:
            alice_supervisor:
              workdir: {repo.as_posix()}
        """
    )
    cfg = load_agent_runtime_config(config_path=path)

    assert mission_chat_workdir("neko_supervisor", cfg) is not None


def test_config_reader_absent_is_none():
    path = _write_config("agent_runtime:\n  personas: {}\n")
    cfg = load_agent_runtime_config(config_path=path)

    assert mission_chat_workdir("dev", cfg) is None
    assert mission_chat_workdir("", cfg) is None


def test_config_key_naming_is_single_authority():
    assert persona_workdir_config_key("dev") == "agent_runtime.personas.dev.workdir"


def test_persona_lookup_survives_a_config_fault(monkeypatch):
    import agent_runtime.config as config_module

    def _boom(*_args, **_kwargs):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(config_module, "mission_chat_workdir", _boom)
    # A config fault must never fail a turn — it degrades to "nothing configured".
    resolved = mission_chat_workdir_for_persona(_persona("qa"))

    assert resolved.path is None
    assert resolved.rows() == []


# ── resolver + live-turn wiring ─────────────────────────────────────────────


def test_visibility_carries_the_workdir_row(tmp_path):
    visibility = resolve_tool_visibility(
        _persona("qa"),
        ToolVisibilityOptions(
            entry_point_lane=HARNESS_LANE,
            mission_chat_workdir=resolve_mission_chat_workdir(
                persona_id="qa", configured=str(tmp_path / "gone")
            ),
        ),
    )

    assert [
        row["code"] for row in visibility["requirement_failures"]
    ] == [MISSION_CHAT_WORKDIR_UNRESOLVED]


def test_a_healthy_workdir_adds_no_row(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    visibility = resolve_tool_visibility(
        _persona("qa"),
        ToolVisibilityOptions(
            entry_point_lane=HARNESS_LANE,
            mission_chat_workdir=resolve_mission_chat_workdir(
                persona_id="qa", configured=str(repo)
            ),
        ),
    )

    assert visibility["requirement_failures"] == []


class _CapturingRunner:
    def __init__(self):
        self.request = None

    def run(self, request):
        self.request = request
        return AgentRunResult(
            final_response="ok",
            session_id="session_mission_chat",
            provider="openai-codex",
            model="gpt-5.5",
            base_url=None,
            messages=[],
        )


def _runtime(runner):
    from agent_runtime.persona_runtime import GPTPersonaRuntime

    return GPTPersonaRuntime(
        default_provider="openai-codex", default_model="gpt-5.5", agent_runner=runner
    )


def test_mission_chat_turn_is_grounded_in_the_configured_workdir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        "agent_runtime.persona_runtime.mission_chat_workdir_for_persona",
        lambda persona, **_kwargs: resolve_mission_chat_workdir(
            persona_id=persona.id, configured=str(repo)
        ),
    )
    runner = _CapturingRunner()

    _runtime(runner).mission_chat_reply(
        _persona("neko_supervisor"),
        "read the repo",
        permission_session_id="session_mission_chat",
    )

    assert _same(str(runner.request.workdir), repo)


def test_mission_chat_turn_without_grounding_passes_no_workdir(tmp_path, monkeypatch):
    # The unchanged default: no workdir on the request means profile_runner
    # never chdirs, exactly as before G6.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    runner = _CapturingRunner()

    _runtime(runner).mission_chat_reply(
        _persona("neko_supervisor"),
        "hello",
        permission_session_id="session_mission_chat",
    )

    assert runner.request.workdir is None


def test_a_missing_configured_workdir_never_fails_the_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(
        "agent_runtime.persona_runtime.mission_chat_workdir_for_persona",
        lambda persona, **_kwargs: resolve_mission_chat_workdir(
            persona_id=persona.id, configured=str(tmp_path / "gone")
        ),
    )
    runner = _CapturingRunner()

    result = _runtime(runner).mission_chat_reply(
        _persona("neko_supervisor"),
        "hello",
        permission_session_id="session_mission_chat",
    )

    assert result.final_response == "ok"
    assert runner.request.workdir is None
    receipt = result.raw["mission_chat_workdir"]
    assert receipt["grounded"] is False
    assert receipt["issues"][0]["code"] == MISSION_CHAT_WORKDIR_UNRESOLVED


def test_the_workspace_pointer_reaches_the_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("# rules", encoding="utf-8")
    runner = _CapturingRunner()

    _runtime(runner).mission_chat_reply(
        _persona("neko_supervisor"),
        "hello",
        permission_session_id="session_mission_chat",
        workspace_agents_path=str(workspace / "AGENTS.md"),
    )

    assert _same(str(runner.request.workdir), workspace)
