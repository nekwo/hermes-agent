import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_time import now
from hermes_cli.harness import build_parser
from agent_runtime import paths
from agent_runtime.models import AgentRun, Incident
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.states import RunState, TaskState
from agent_runtime.store import (
    IncidentStore,
    RealmStore,
    RunStore,
    TaskStore,
    WorkspaceStore,
)


def parser():
    p=argparse.ArgumentParser(); subs=p.add_subparsers(dest="command"); build_parser(subs); return p


def test_harness_parser_no_longer_exposes_task_create():
    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "task", "create", "--title", "T", "--description", "D", "--json"])


def test_harness_init_rejects_the_removed_persona_seeding_flag():
    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "init", "--with-" + "bundled-personas", "--json"])


def test_harness_init_materializes_an_idempotent_default_scope(capsys):
    args = parser().parse_args(["harness", "init", "--json"])

    assert args.func(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["default_realm_id"] == "realm_default"
    assert first["default_workspace_id"] == "ws_default"

    assert args.func(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second == first
    assert len(RealmStore().list_all()) == 1
    assert len(WorkspaceStore().list_all()) == 1


def test_harness_default_scope_dry_run_exposes_read_only_inventory(capsys):
    legacy = RealmStore().create(name="default")
    workspace = WorkspaceStore().create(name="default", realm_id=legacy.id)
    legacy.default_workspace_id = workspace.id
    legacy.workspace_ids = [workspace.id]
    RealmStore().save(legacy)
    args = parser().parse_args(
        ["harness", "realm", "default-scope", "--dry-run", "--json"]
    )

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["kind"] == "default_scope_migration_preview"
    assert data["status"] == "legacy_adoption_ready"
    assert data["mutated"] is False
    assert data["proposed_default_scope"]["realm_id"] == legacy.id
    assert RealmStore().get(legacy.id).default_workspace_id == workspace.id


def test_harness_default_scope_applies_explicit_legacy_winner(capsys):
    legacy = RealmStore().create(name="default")
    workspace = WorkspaceStore().create(name="default", realm_id=legacy.id)
    RealmStore().set_active(legacy.id)
    WorkspaceStore().set_active(workspace.id)
    canonical = RealmStore().create(
        name="Default",
        realm_id="realm_default",
        default_workspace_id="ws_default",
    )
    canonical_workspace = WorkspaceStore().create(
        name="Default",
        workspace_id="ws_default",
        realm_id=canonical.id,
    )
    canonical.workspace_ids = [canonical_workspace.id]
    RealmStore().save(canonical)
    args = parser().parse_args(
        [
            "harness",
            "realm",
            "default-scope",
            "--winner-realm",
            legacy.id,
            "--winner-workspace",
            workspace.id,
            "--yes",
            "--json",
        ]
    )

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "applied"
    assert data["winner_realm_id"] == legacy.id
    assert data["winner_workspace_id"] == workspace.id
    assert RealmStore().get("realm_default").archived is True
    assert WorkspaceStore().get("ws_default").archived is True


def test_harness_init_reports_typed_reconciliation_error_without_merging(capsys):
    args = parser().parse_args(["harness", "init", "--json"])
    assert args.func(args) == 0
    capsys.readouterr()
    legacy = RealmStore().create(name="default")
    legacy_workspace = WorkspaceStore().create(
        name="default",
        realm_id=legacy.id,
    )
    before_realm_ids = [item.id for item in RealmStore().list_all()]
    before_workspace_ids = [item.id for item in WorkspaceStore().list_all()]

    assert args.func(args) == 6
    data = json.loads(capsys.readouterr().out)
    assert data["error"]["code"] == "default_scope_reconciliation_required"
    assert data["error"]["safe_details"]["candidate_realm_ids"] == sorted(
        ["realm_default", legacy.id]
    )
    assert [item.id for item in RealmStore().list_all()] == before_realm_ids
    assert [item.id for item in WorkspaceStore().list_all()] == before_workspace_ids
    assert WorkspaceStore().get(legacy_workspace.id).realm_id == legacy.id


def test_harness_parser_exposes_idempotent_exact_instance_chat_mint():
    args = parser().parse_args(
        [
            "harness",
            "persona",
            "instance",
            "open-chat",
            "--persona",
            "dev",
            "--persona-instance-id",
            "personainst_dev",
            "--new-session",
            "--idempotency-key",
            "new-chat-dev-1",
            "--json",
        ]
    )

    assert args.persona_command == "instance"
    assert args.persona_instance_command == "open-chat"
    assert args.persona_instance_id == "personainst_dev"
    assert args.new_session is True
    assert args.idempotency_key == "new-chat-dev-1"


def test_harness_mission_chat_steer_no_active_turn_returns_structured_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    args = parser().parse_args(
        [
            "harness",
            "mission-chat",
            "steer",
            "--session-id",
            "session_missing",
            "--message",
            "for neko",
            "--client-message-id",
            "client_1",
            "--json",
        ]
    )

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["capability_id"] == "mission.chat.steer"
    assert data["execution_state"] == "rejected"
    assert data["error_kind"] == "no_active_turn"
    assert data["session_id"] == "session_missing"
    assert data["client_message_id"] == "client_1"


def test_harness_task_create_is_removed_without_store_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "task", "create", "--title", "T", "--description", "D", "--json"])
    assert vars(TaskStore()) == {}


def test_harness_parser_exposes_task_archive_ready_json():
    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "task", "archive-ready", "--json"])


def test_harness_parser_rejects_goal_run():
    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "goal", "run", "--title", "T", "--description", "D", "--json"])


def test_harness_no_longer_exports_mission_runtime_controller():
    from hermes_cli import harness

    assert not hasattr(harness, "MissionRuntimeController")


def test_harness_rejects_goal_run_archive_on_done():
    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "goal", "run", "--title", "T", "--description", "D", "--archive-on-done", "--json"])


def test_harness_task_archive_ready_preserves_evidence_and_removes_open_listing(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "task", "archive-ready", "--json"])


def test_harness_task_archive_refuses_active_task_id(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "task", "archive", "task_active", "--json"])


def test_harness_task_cancel_cancels_active_runs_for_archive_cleanup(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "task", "cancel", "task_cancel", "--json"])


def test_harness_task_show_can_include_task_scoped_events(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "task", "show", "task_events", "--json"])


def test_harness_task_history_returns_event_envelope(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "task", "history", "task_history", "--json"])


def test_harness_run_show_returns_run_and_events_without_retired_proofs(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "run", "show", "run_removed", "--json"])


def test_harness_parser_exposes_doctor_fix_flags():
    args = parser().parse_args(["harness", "doctor", "--fix", "--dry-run", "--json"])

    assert args.harness_command == "doctor"
    assert args.fix is True
    assert args.dry_run is True
    assert args.worktree_min_age_seconds == 3600


@pytest.mark.parametrize(
    "flag",
    [
        "--stale-run-hours",
        "--stale-worker-hours",
        "--stale-task-days",
        "--stale-incident-days",
        "--stale-incident-hours",
    ],
)
def test_harness_doctor_rejects_the_removed_stale_threshold_flags(flag):
    """These fed task/run/worker/incident sweeps that died with the mission lane.

    They were accepted and silently ignored; the parser must now refuse them
    rather than let an operator believe a threshold took effect.
    """

    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "doctor", flag, "3", "--json"])


def test_harness_doctor_rejects_the_removed_compact_events_flag():
    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "doctor", "--compact-events", "--json"])


def test_harness_doctor_fix_requires_confirmation(capsys):
    args = parser().parse_args(["harness", "doctor", "--fix", "--json"])

    assert args.func(args) == 8
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["error"] == "confirmation_required"


def test_harness_init_human_branch_states_when_no_personas_are_provisioned(monkeypatch, capsys):
    """Personas are data now, so an empty roster is a real state.

    The old branch printed a dangling ``Initialized harness personas: `` with
    nothing after the colon.
    """

    import hermes_cli.harness as harness_mod

    monkeypatch.setattr(harness_mod, "ensure_persisted_personas", lambda cfg: [])
    monkeypatch.setattr(
        harness_mod,
        "ensure_default_scope",
        lambda agent_ids: SimpleNamespace(
            realm=SimpleNamespace(id="realm_default", name="Default"),
            workspace=SimpleNamespace(id="ws_default", name="Default"),
        ),
    )
    args = parser().parse_args(["harness", "init"])

    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "Initialized harness personas: \n" not in out
    assert "no personas provisioned" in out
    assert "Default scope: Default / Default" in out


def test_harness_doctor_human_branch_renders_the_surviving_findings(tmp_path, monkeypatch, capsys):
    """The default (non-JSON) doctor path must render only keys the report emits.

    Regression: the human branch read seven mission-era finding keys while the
    report ships two, so every plain ``harness doctor`` died with KeyError.
    """

    import hermes_cli.harness as harness_mod

    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "agent-runtime"))
    # This is a renderer test. Keep it hermetic instead of scanning every real
    # harness worktree (and running git diff in each) merely to obtain the two
    # finding keys whose formatting is under test.
    monkeypatch.setattr(
        harness_mod,
        "run_harness_doctor",
        lambda **_kwargs: {
            "summary": {
                "finding_counts": {
                    "orphan_worktrees": 0,
                    "snapshot_null_id_rows": 0,
                }
            },
            "findings": {
                "event_log": {
                    "size_bytes": 0,
                    "line_count": 0,
                    "archived_event_slices": 0,
                    "index_health": "ok",
                }
            },
        },
    )
    args = parser().parse_args(["harness", "doctor"])

    assert args.func(args) == 0
    out = capsys.readouterr().out
    findings = next(line for line in out.splitlines() if line.startswith("findings: "))
    rendered = {pair.split("=", 1)[0] for pair in findings[len("findings: ") :].split()}
    assert rendered == {"orphan_worktrees", "snapshot_null_id_rows"}
    for retired in ("stale_runs=", "workers=", "incidents=", "event_compactable_rows=", "tasks="):
        assert retired not in out


def test_harness_parser_exposes_config_migrate_and_verify():
    p = parser()
    assert p.parse_args(["harness", "config", "show", "--json"]).config_command == "show"
    assert p.parse_args(["harness", "migrate", "--check", "--json"]).harness_command == "migrate"
    verify = p.parse_args(["harness", "verify", "--mode", "temp-root", "--skip-tests", "--json"])
    assert verify.harness_command == "verify"
    assert verify.mode == "temp-root"
    assert verify.skip_tests is True
    with pytest.raises(SystemExit):
        p.parse_args(["harness", "burn-in", "run", "noop-orchestration", "--json"])


def test_harness_config_show_and_migrate_check_are_redaction_safe(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    args = parser().parse_args(["harness", "config", "show", "--json"])
    assert args.func(args) == 0
    config_data = json.loads(capsys.readouterr().out)
    assert config_data["validation"]["ok"] is True
    assert config_data["schema_version"] == 1
    assert "store_root" in config_data

    args = parser().parse_args(["harness", "migrate", "--check", "--json"])
    assert args.func(args) == 0
    migration_data = json.loads(capsys.readouterr().out)
    assert migration_data["pending"] is False
    assert migration_data["check_only"] is True


def test_harness_verify_skip_tests_emits_proof_packet(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    args = parser().parse_args(["harness", "verify", "--mode", "temp-root", "--skip-tests", "--json"])

    assert args.func(args) == 0
    packet = json.loads(capsys.readouterr().out)
    assert packet["schema_version"] == 1
    assert packet["mode"] == "temp-root"
    assert packet["runtime_root"] == str(tmp_path / "runtime")
    assert {item["label"] for item in packet["commands"]} >= {"harness status", "harness snapshot", "harness config show"}
    assert packet["tests"] == []
    assert packet["runtime_config"]["validation"]["ok"] is True


def test_harness_burn_in_commands_are_removed():
    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "burn-in", "summarize", "missing_burn", "--json"])


def test_harness_parser_exposes_observe():
    args = parser().parse_args(["harness", "observe", "--json"])
    assert args.command == "harness" and args.harness_command == "observe"


def test_harness_smoke_command_is_removed():
    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "smoke", "--json", "--no-model"])


def test_harness_incident_close_closes_incident_with_reason(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        parser().parse_args(["harness", "incident", "close", "inc_1", "--json"])


def test_harness_parser_has_no_import_kanban():
    p=parser()
    try:
        p.parse_args(["harness", "import-kanban", "x"])
    except SystemExit as e:
        assert e.code != 0
    else:
        raise AssertionError("import-kanban unexpectedly parsed")


def test_harness_cli_init_status_observe_snapshot_e2e(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    init_args = parser().parse_args(["harness", "init", "--json"])
    assert init_args.func(init_args) == 0

    for argv in [
        ["harness", "status", "--json"],
        ["harness", "observe", "--json"],
        ["harness", "snapshot", "--json"],
    ]:
        args = parser().parse_args(argv)
        assert args.func(args) == 0

    assert capsys.readouterr().out
    assert (tmp_path / "runtime" / "snapshot.json").exists()


def _seed_rebind_fixture(tmp_path, monkeypatch):
    """A store-persisted agent bound to `alpha`, with two real profile homes."""

    from agent_runtime.models import AgentPersona
    from agent_runtime.persona_assignments import PersonaInstanceStore
    from agent_runtime.store import AgentStore

    home = tmp_path / "hermes-home"
    for name in ("alpha", "beta"):
        (home / "profiles" / name).mkdir(parents=True, exist_ok=True)
        (home / "profiles" / name / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    persona = AgentStore().save(
        AgentPersona(
            id="widget",
            display_name="Widget Agent",
            role="dev",
            model=None,
            provider=None,
            api_mode="codex_responses",
            toolsets=["file"],
            system_prompt_path="",
            hermes_profile="alpha",
        )
    )
    PersonaInstanceStore().ensure_for_persona(persona)
    return persona


def test_harness_parser_exposes_agent_set_profile_with_dry_run():
    args = parser().parse_args(
        ["harness", "agent", "set-profile", "widget", "--profile", "beta", "--dry-run", "--json"]
    )

    assert args.harness_command == "agent"
    assert args.agent_command == "set-profile"
    assert args.persona_id == "widget"
    assert args.profile == "beta"
    # The parser opts into --dry-run explicitly; the verb must READ it.
    assert args.dry_run is True


def test_agent_set_profile_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    from agent_runtime.store import AgentStore

    _seed_rebind_fixture(tmp_path, monkeypatch)

    args = parser().parse_args(
        ["harness", "agent", "set-profile", "widget", "--profile", "beta", "--dry-run", "--json"]
    )
    assert args.func(args) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["dry_run"] is True
    assert data["from_profile"] == "alpha"
    assert data["to_profile"] == "beta"
    assert AgentStore().get("widget").hermes_profile == "alpha"


def test_agent_set_profile_applies_and_cascades(tmp_path, monkeypatch, capsys):
    from agent_runtime.persona_assignments import PersonaInstanceStore
    from agent_runtime.store import AgentStore

    _seed_rebind_fixture(tmp_path, monkeypatch)

    args = parser().parse_args(["harness", "agent", "set-profile", "widget", "--profile", "beta", "--json"])
    assert args.func(args) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["dry_run"] is False
    assert data["changed"] is True
    assert data["binding_files"]["hermes_profile"] == "beta"
    assert data["realm_artifact_delta"]["measured"] is True
    assert AgentStore().get("widget").hermes_profile == "beta"
    assert PersonaInstanceStore().get("personainst_widget").profile_id == "beta"


def test_agent_set_profile_reports_typed_refusal(tmp_path, monkeypatch, capsys):
    _seed_rebind_fixture(tmp_path, monkeypatch)

    args = parser().parse_args(
        ["harness", "agent", "set-profile", "widget", "--profile", "definitely-missing", "--json"]
    )
    assert args.func(args) == 2

    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["error_code"] == "profile_missing"


# ── the stage42 global flags are promises, and every one must be kept ────────
#
# A flag registered by `_add_stage42_global_args` is advertised on ~60 verbs at
# once. An unconsumed one is therefore the worst kind of no-op: it appears in
# `--help` everywhere, argparse accepts it without a murmur, and the operator
# gets the UNFILTERED / UNWATCHED answer with no signal that the flag was
# dropped on the floor. That is a wrong answer believed, not an error seen.
# `--filter` and `--watch` were both exactly that until 2026-07-28.


#: Flags whose contract is met WITHOUT any code reading them, and the reason.
#: An entry here is a claim that has to stay true — not an escape hatch for the
#: next unimplemented flag.
_STAGE42_SATISFIED_BY_CONSTRUCTION = {
    "no_color": "nothing on this lane emits ANSI, so 'no color' is already what you get",
}


def _stage42_lane_sources():
    """Every source file that could legitimately honor a stage42 global flag.

    Scoped to the HARNESS lane on purpose. Scanning all of `hermes_cli/` would
    let an unrelated command's own identically-named flag stand in as the
    reader — `hermes_cli/journey.py` reads a `no_color` it registers itself,
    which would have passed this gate while the harness lane ignored its own."""

    root = Path(__file__).resolve().parents[2]
    yield root / "hermes_cli" / "harness.py"
    parts = root / "hermes_cli" / "harness_parts"
    for filename in (
        "persona_commands.py",
        "runtime_commands.py",
        "board.py",
        "office.py",
        "flow_commands.py",
        "checkpoint_commands.py",
    ):
        yield parts / filename


def _stage42_source_module(path: Path) -> str:
    # These files are compiled into hermes_cli.harness globals in this exact
    # order by _load_command_parts; they are not independent Python modules.
    if path.parent.name == "harness_parts" or path.name == "harness.py":
        return "hermes_cli.harness"
    return ".".join(path.with_suffix("").parts[-2:])


def _argument_dest(call: ast.Call) -> str | None:
    explicit = next((kw.value for kw in call.keywords if kw.arg == "dest"), None)
    if isinstance(explicit, ast.Constant) and isinstance(explicit.value, str):
        return explicit.value
    flags = [arg.value for arg in call.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
    option = next((flag for flag in reversed(flags) if flag.startswith("--")), None)
    return option[2:].replace("-", "_") if option else None


_FunctionId = tuple[str, str]
_STAGE42_PRESENTATION_DESTS = {"output", "json", "quiet", "fields", "no_color"}

_STAGE42_DELIBERATE_LOCAL_OVERRIDES: dict[tuple[_FunctionId, str], str] = {}


def _literal_string_set(node: ast.AST | None) -> set[str]:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "frozenset" and node.args:
            return _literal_string_set(node.args[0])
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return {
            item.value
            for item in node.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
    return set()


def _stage42_parser_ownership(
    source: str,
    common_dests: set[str],
):
    """Return each Stage 42 handler's flags and per-verb destination collisions."""

    tree = ast.parse(source)
    build = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "build_parser")
    registrations: dict[str, set[str]] = {}
    local_dests: dict[str, set[str]] = {}
    equivalent_local_overrides: dict[str, set[str]] = {}
    handlers: dict[str, str] = {}
    for node in ast.walk(build):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Name):
            continue
        parser_name = node.func.value.id
        if node.func.attr == "add_argument":
            dest = _argument_dest(node)
            if dest:
                local_dests.setdefault(parser_name, set()).add(dest)
                options = {
                    arg.value
                    for arg in node.args
                    if isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and arg.value.startswith("-")
                }
                action = next((kw.value for kw in node.keywords if kw.arg == "action"), None)
                if (
                    dest == "json"
                    and options == {"--json"}
                    and isinstance(action, ast.Constant)
                    and action.value == "store_true"
                ):
                    equivalent_local_overrides.setdefault(parser_name, set()).add(dest)
        elif node.func.attr == "set_defaults":
            func = next((kw.value for kw in node.keywords if kw.arg == "func"), None)
            if isinstance(func, ast.Name):
                handlers[parser_name] = func.id
    for node in ast.walk(build):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "_add_stage42_global_args":
            continue
        if not node.args or not isinstance(node.args[0], ast.Name):
            continue
        parser_name = node.args[0].id
        controls = _literal_string_set(
            next((kw.value for kw in node.keywords if kw.arg == "controls"), None)
        )
        omitted = {
            flag.lstrip("-").replace("-", "_")
            for flag in _literal_string_set(
                next((kw.value for kw in node.keywords if kw.arg == "omit"), None)
            )
        }
        registrations[parser_name] = (set(common_dests) | controls) - omitted

    return [
        (
            ("hermes_cli.harness", handlers[parser_name]),
            set(owned_dests),
            (owned_dests & local_dests.get(parser_name, set()))
            - equivalent_local_overrides.get(parser_name, set()),
        )
        for parser_name, owned_dests in registrations.items()
        if parser_name in handlers
    ]


class _Stage42FunctionVisitor(ast.NodeVisitor):
    """Collect one function body without crediting nested definitions."""

    def __init__(self, module: str, qualname: str) -> None:
        self.module = module
        self.qualname = qualname
        self.reads: set[str] = set()
        self.calls: set[_FunctionId] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name) and node.value.id == "args":
            self.reads.add(node.attr)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "args"
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            self.reads.add(node.args[1].value)
        if isinstance(node.func, ast.Name):
            self.calls.add((self.module, node.func.id))
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            owner = node.func.value.id
            if owner == "self" and "." in self.qualname:
                self.calls.add((self.module, f"{self.qualname.rsplit('.', 1)[0]}.{node.func.attr}"))
            else:
                self.calls.add((owner, node.func.attr))
        self.generic_visit(node)


def _stage42_function_facts(sources: list[tuple[str, str]]):
    reads: dict[_FunctionId, set[str]] = {}
    calls: dict[_FunctionId, set[_FunctionId]] = {}

    def collect(module: str, node: ast.FunctionDef | ast.AsyncFunctionDef, prefix: str = "") -> None:
        qualname = f"{prefix}.{node.name}" if prefix else node.name
        visitor = _Stage42FunctionVisitor(module, qualname)
        for statement in node.body:
            visitor.visit(statement)
        identity = (module, qualname)
        reads[identity] = visitor.reads
        calls[identity] = visitor.calls
        for statement in node.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                collect(module, statement, qualname)
            elif isinstance(statement, ast.ClassDef):
                for member in statement.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        collect(module, member, f"{qualname}.{statement.name}")

    for module, source in sources:
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                collect(module, node)
            elif isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        collect(module, member, node.name)
    return reads, calls


def _stage42_unhonored_registrations(
    registrations: list[tuple[_FunctionId, set[str], set[str]]],
    *,
    reads: dict[_FunctionId, set[str]],
    calls: dict[_FunctionId, set[_FunctionId]],
    required_consumers: dict[str, _FunctionId] | None = None,
) -> list[tuple[_FunctionId, str]]:
    unhonored: list[tuple[_FunctionId, str]] = []
    for handler, dests, collisions in registrations:
        reachable = {handler}
        pending = [handler]
        while pending:
            for callee in calls.get(pending.pop(), set()):
                if callee in reads and callee not in reachable:
                    reachable.add(callee)
                    pending.append(callee)
        for dest in dests:
            if dest in _STAGE42_SATISFIED_BY_CONSTRUCTION:
                continue
            if dest in collisions:
                unhonored.append((handler, dest))
                continue
            consumer = (required_consumers or {}).get(dest)
            if consumer is not None:
                honored = consumer in reachable and dest in reads.get(consumer, set())
            else:
                honored = any(dest in reads.get(function, set()) for function in reachable)
            if not honored:
                unhonored.append((handler, dest))
    return sorted(unhonored)


def _stage42_reachable(
    handler: _FunctionId,
    reads: dict[_FunctionId, set[str]],
    calls: dict[_FunctionId, set[_FunctionId]],
) -> set[_FunctionId]:
    reachable = {handler}
    pending = [handler]
    while pending:
        for callee in calls.get(pending.pop(), set()):
            if callee in reads and callee not in reachable:
                reachable.add(callee)
                pending.append(callee)
    return reachable


def test_every_stage42_global_flag_is_honored():
    """Every flag advertised by a real verb reaches a real reader."""

    paths = list(_stage42_lane_sources())
    harness_source = paths[0].read_text(encoding="utf-8")
    sources = [
        (_stage42_source_module(path), path.read_text(encoding="utf-8"))
        for path in paths
    ]
    support = paths[0].with_name("harness_support.py")
    sources.append(("hermes_cli.harness", support.read_text(encoding="utf-8")))
    reads, calls = _stage42_function_facts(sources)
    registrations = _stage42_parser_ownership(
        harness_source,
        _STAGE42_PRESENTATION_DESTS,
    )
    # The root parser registration supplies options before the chosen verb;
    # its `harness_command` default is replaced by every real subparser. The
    # per-verb registrations below are the semantic ownership boundary.
    registrations = [
        registration
        for registration in registrations
        if registration[0][1] != "harness_command"
    ]

    assert _stage42_unhonored_registrations(
        registrations,
        reads=reads,
        calls=calls,
    ) == []


@pytest.mark.parametrize(
    "argv",
    [
        ["harness", "workspace", "show", "ws_1", "--sort", "name"],
        ["harness", "board", "update", "board_1", "--yes"],
        ["harness", "work", "list", "--cursor", "opaque"],
        ["harness", "work", "peek", "terminal:one", "--limit", "1"],
    ],
)
def test_stage42_controls_are_refused_when_the_handler_cannot_honor_them(argv):
    with pytest.raises(SystemExit) as excinfo:
        parser().parse_args(argv)
    assert excinfo.value.code == 2


def test_stage42_controls_remain_available_on_their_real_owners():
    assert parser().parse_args(
        ["harness", "workspace", "list", "--sort", "name"]
    ).sort == "name"
    assert parser().parse_args(
        ["harness", "work", "list", "--limit", "3"]
    ).limit == 3
    assert parser().parse_args(
        ["harness", "work", "cancel", "terminal:one", "--yes"]
    ).yes is True


def test_stage42_honored_gate_rejects_a_per_verb_destination_collision():
    module = "fixture.cli"
    reads = {
        (module, "global_handler"): {"output"},
        (module, "local_handler"): {"cursor"},
        (module, "shared_output"): {"quiet"},
    }
    calls = {
        (module, "global_handler"): {(module, "shared_output")},
        (module, "local_handler"): set(),
        (module, "shared_output"): set(),
    }

    old_unhonored = sorted(
        dest
        for dest in {"output", "quiet", "cursor", "no_color"}
        if dest not in _STAGE42_SATISFIED_BY_CONSTRUCTION
        and not any(dest in function_reads for function_reads in reads.values())
    )
    assert old_unhonored == [], "the fixture must reproduce the old whole-lane false pass"

    assert _stage42_unhonored_registrations(
        [
            ((module, "global_handler"), {"output", "quiet", "no_color"}, set()),
            ((module, "local_handler"), {"cursor"}, {"cursor"}),
        ],
        reads=reads,
        calls=calls,
    ) == [((module, "local_handler"), "cursor")]


def test_stage42_honored_gate_requires_each_registered_handler_to_consume():
    module = "hermes_cli.harness"
    handler_one = (module, "handler_one")
    handler_two = (module, "handler_two")
    registrations = _stage42_parser_ownership(
        """
def build_parser():
    one = subs.add_parser('one')
    _add_stage42_global_args(one, controls=frozenset({'limit'}))
    one.set_defaults(func=handler_one)
    two = subs.add_parser('two')
    _add_stage42_global_args(two, controls=frozenset({'limit'}))
    two.set_defaults(func=handler_two)
""",
        set(),
    )
    assert _stage42_unhonored_registrations(
        registrations,
        reads={handler_one: {"limit"}, handler_two: set()},
        calls={handler_one: set(), handler_two: set()},
    ) == [(handler_two, "limit")]


def test_stage42_honored_gate_rejects_a_same_option_local_reregistration():
    module = "hermes_cli.harness"
    handler = (module, "handler")
    registrations = _stage42_parser_ownership(
        """
def build_parser():
    command = subs.add_parser('command')
    command.add_argument('--limit', type=int, default=7)
    _add_stage42_global_args(command)
    command.set_defaults(func=handler)
""",
        {"limit"},
    )
    assert _stage42_unhonored_registrations(
        registrations,
        reads={handler: {"limit"}},
        calls={handler: set()},
    ) == [(handler, "limit")]


def test_stage42_honored_gate_does_not_merge_duplicate_helpers_across_modules():
    handler = ("fixture.cli", "handler")
    local_helper = ("fixture.cli", "consume")
    unrelated_helper = ("fixture.other", "consume")
    assert _stage42_unhonored_registrations(
        [(handler, {"since"}, set())],
        reads={handler: set(), local_helper: set(), unrelated_helper: {"since"}},
        calls={handler: {local_helper}, local_helper: set(), unrelated_helper: set()},
    ) == [(handler, "since")]


def test_stage42_honored_gate_ignores_uncalled_nested_readers():
    reads, calls = _stage42_function_facts(
        [
            (
                "fixture.cli",
                """
def handler(args):
    def hidden():
        return args.cursor
    return 0
""",
            )
        ]
    )
    handler = ("fixture.cli", "handler")
    assert reads[handler] == set()
    assert _stage42_unhonored_registrations(
        [(handler, {"cursor"}, set())],
        reads=reads,
        calls=calls,
    ) == [(handler, "cursor")]


def test_stage42_honored_gate_accepts_a_supported_qualified_shared_consumer():
    reads, calls = _stage42_function_facts(
        [
            (
                "hermes_cli.harness",
                """
def handler_one(args):
    return shared.consume(args)

def handler_two(args):
    return shared.consume(args)
""",
            ),
            ("shared", "def consume(args):\n    return args.output\n"),
        ]
    )
    handler_one = ("hermes_cli.harness", "handler_one")
    handler_two = ("hermes_cli.harness", "handler_two")
    registrations = _stage42_parser_ownership(
        """
def build_parser():
    one = subs.add_parser('one')
    _add_stage42_global_args(one)
    one.set_defaults(func=handler_one)
    two = subs.add_parser('two')
    _add_stage42_global_args(two)
    two.set_defaults(func=handler_two)
""",
        {"output"},
    )
    assert _stage42_unhonored_registrations(
        registrations,
        reads=reads,
        calls=calls,
        required_consumers={"output": ("shared", "consume")},
    ) == []


def test_a_stage42_flag_nothing_implements_is_refused_not_swallowed():
    """`--filter` was accepted and ignored; now it is refused.

    The behavior contract that replaces it, and the reason removal was the
    complete fix rather than a retreat: an operator who reaches for filtering
    on a list verb must find out. Silently returning the unfiltered set is the
    one outcome that cannot be told apart from the flag working."""

    for flag in ("--filter=state=running", "--watch"):
        with pytest.raises(SystemExit) as excinfo:
            parser().parse_args(["harness", "goal", "list", flag])
        assert excinfo.value.code == 2, f"{flag} was swallowed instead of refused"

    # The mission family itself is gone, including its former typed filter.
    with pytest.raises(SystemExit) as excinfo:
        parser().parse_args(["harness", "goal", "list", "--state", "done"])
    assert excinfo.value.code == 2


def test_run_verify_command_survives_non_cp1252_bytes_in_child_output(tmp_path):
    """Sub-command output is decoded as UTF-8 with replacement, never the
    locale codepage: byte 0x90 is undefined in cp1252, and without a pinned
    encoding it crashed subprocess's reader thread on Windows, silently
    dropping the captured output from the verification payload."""
    # runtime_commands.py is exec'd into hermes_cli.harness globals by
    # _load_command_parts(); it is not importable as a standalone module.
    from hermes_cli.harness import _run_verify_command

    child = (
        "import sys;"
        "sys.stdout.buffer.write('utf8:\\u2713'.encode('utf-8') + b' raw:\\x90');"
        "sys.stderr.buffer.write(b'err:\\x90')"
    )
    result = _run_verify_command("unicode", [sys.executable, "-c", child], cwd=tmp_path)

    assert result["exit_code"] == 0
    assert "utf8:✓" in result["stdout_summary"]
    assert "raw:�" in result["stdout_summary"]
    assert "err:�" in result["stderr_summary"]
