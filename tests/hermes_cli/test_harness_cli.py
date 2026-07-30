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


def test_harness_init_exposes_atomic_bundled_persona_opt_in():
    args = parser().parse_args(["harness", "init", "--with-bundled-personas", "--json"])
    assert args.harness_command == "init"
    assert args.with_bundled_personas is True


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
    assert args.stale_incident_days == 7
    assert args.stale_incident_hours is None


def test_harness_parser_accepts_doctor_stale_incident_overrides():
    days = parser().parse_args(["harness", "doctor", "--stale-incident-days", "3", "--json"])
    hours = parser().parse_args(["harness", "doctor", "--stale-incident-hours", "12", "--json"])

    assert days.stale_incident_days == 3
    assert days.stale_incident_hours is None
    assert hours.stale_incident_hours == 12


def test_harness_doctor_fix_requires_confirmation(capsys):
    args = parser().parse_args(["harness", "doctor", "--fix", "--json"])

    assert args.func(args) == 8
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["error"] == "confirmation_required"


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
    # _add_stage42_global_args(mutation=True) registers --dry-run; the verb must READ it.
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
_STAGE42_SHARED_CONSUMERS: dict[str, _FunctionId] = {
    "output": ("hermes_cli.harness", "_print_stage42"),
    "json": ("hermes_cli.harness", "_print_stage42"),
    "quiet": ("hermes_cli.harness", "_print_stage42"),
    "fields": ("hermes_cli.harness", "_print_stage42"),
    "yes": ("hermes_cli.harness", "_require_yes"),
    "dry_run": ("hermes_cli.harness", "_require_yes"),
}
_STAGE42_DESTINATION_OWNERS: dict[str, _FunctionId] = {
    **_STAGE42_SHARED_CONSUMERS,
    "sort": ("hermes_cli.harness", "_cmd_goal_list"),
    "limit": ("hermes_cli.harness", "_cmd_goal_list"),
    "cursor": ("hermes_cli.harness", "_cmd_goal_list"),
    "since": ("hermes_cli.harness", "_cmd_goal_history"),
}


def _stage42_handlers(*names: str) -> set[_FunctionId]:
    return {("hermes_cli.harness", name) for name in names}


# Independent semantic applicability contract. This is deliberately data, not
# a projection of the AST reader graph: changing a handler read cannot add or
# remove an obligation. The envelope category is the bounded set of commands
# whose result is emitted through the Stage 42 envelope; confirmation is the
# bounded set of destructive commands sharing _require_yes. Domain controls
# name the one parser path whose promise they currently implement.
_STAGE42_ENVELOPE_HANDLERS = _stage42_handlers(
    "_cmd_agent_list", "_cmd_agent_set_profile", "_cmd_board_card_add",
    "_cmd_board_card_archive", "_cmd_board_card_edit", "_cmd_board_card_move",
    "_cmd_board_card_restore", "_cmd_board_create",
    "_cmd_board_list", "_cmd_board_resolve_conflict", "_cmd_board_show",
    "_cmd_board_update", "_cmd_goal_archive", "_cmd_goal_cancel",
    "_cmd_goal_history", "_cmd_goal_list",
    "_cmd_goal_show", "_cmd_goal_unblock", "_cmd_lane_list", "_cmd_lane_show",
    "_cmd_mission_chat_clarify_tickets", "_cmd_office_actor_remove",
    "_cmd_office_actor_restore", "_cmd_office_actor_upsert",
    "_cmd_office_resolve_conflict", "_cmd_office_set_folders", "_cmd_office_show",
    "_cmd_realm_adopt", "_cmd_realm_agents_set", "_cmd_realm_agents_show",
    "_cmd_realm_bind_server", "_cmd_realm_create", "_cmd_realm_default_scope",
    "_cmd_realm_list",
    "_cmd_realm_show", "_cmd_realm_skills_set", "_cmd_realm_skills_show",
    "_cmd_realm_sync_held", "_cmd_realm_sync_publish", "_cmd_realm_sync_pull",
    "_cmd_realm_sync_resolve", "_cmd_realm_sync_status", "_cmd_realm_use",
    "_cmd_roots_list", "_cmd_roots_migrate", "_cmd_roots_set", "_cmd_roots_unset",
    "_cmd_skills_inbox", "_cmd_skills_promote", "_cmd_skills_publishable",
    "_cmd_worker_list", "_cmd_worker_show", "_cmd_workspace_actors",
    "_cmd_workspace_add_agent", "_cmd_workspace_archive", "_cmd_workspace_create",
    "_cmd_workspace_delete", "_cmd_workspace_list", "_cmd_workspace_remove_agent",
    "_cmd_workspace_rename", "_cmd_workspace_show", "_cmd_workspace_use",
)
_STAGE42_CONFIRMATION_HANDLERS = _stage42_handlers(
    "_cmd_goal_archive", "_cmd_goal_cancel", "_cmd_realm_sync_publish",
    "_cmd_realm_sync_resolve", "_cmd_roots_migrate", "_cmd_workspace_archive",
    "_cmd_workspace_delete", "_cmd_workspace_remove_agent",
)
_STAGE42_HANDLER_SCOPES: dict[str, set[_FunctionId]] = {
    **{dest: set(_STAGE42_ENVELOPE_HANDLERS) for dest in ("output", "json", "quiet", "fields")},
    **{dest: set(_STAGE42_CONFIRMATION_HANDLERS) for dest in ("yes", "dry_run")},
    "sort": _stage42_handlers("_cmd_goal_list"),
    "limit": _stage42_handlers("_cmd_goal_list"),
    "cursor": _stage42_handlers("_cmd_goal_list"),
    "since": _stage42_handlers("_cmd_goal_history"),
    "idempotency_key": _stage42_handlers(
        "_cmd_board_card_add", "_cmd_board_card_edit", "_cmd_board_card_move"
    ),
}

# Local --json registrations predate Stage 42 and are the identical store_true
# alias, not a second semantic flag. Non-json local replacements must be named
# exactly here; adding a same-spelled option no longer silently erases evidence.
_STAGE42_DELIBERATE_LOCAL_OVERRIDES: dict[tuple[_FunctionId, str], str] = {
    (("hermes_cli.harness", "_cmd_goal_history"), "limit"): "typed history page size",
}


def _stage42_parser_ownership(
    source: str,
    common_dests: set[str],
    mutation_dests: set[str],
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
        mutation = any(kw.arg == "mutation" and isinstance(kw.value, ast.Constant) and kw.value.value is True for kw in node.keywords)
        parser_name = node.args[0].id
        candidates = mutation_dests if mutation else common_dests
        registrations[parser_name] = set(candidates)

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


def _stage42_shared_scope_drift(
    registrations: list[tuple[_FunctionId, set[str], set[str]]],
    reads: dict[_FunctionId, set[str]],
    calls: dict[_FunctionId, set[_FunctionId]],
    *,
    envelope_scope: set[_FunctionId] = _STAGE42_ENVELOPE_HANDLERS,
    confirmation_scope: set[_FunctionId] = _STAGE42_CONFIRMATION_HANDLERS,
) -> dict[str, tuple[set[_FunctionId], set[_FunctionId]]]:
    """Compare maintained shared scopes with reality; never create scope."""

    envelope_owner = _STAGE42_DESTINATION_OWNERS["output"]
    confirmation_owner = _STAGE42_DESTINATION_OWNERS["yes"]
    observed_envelope = {
        handler
        for handler, dests, _ in registrations
        if "output" in dests and envelope_owner in _stage42_reachable(handler, reads, calls)
    }
    observed_confirmation = {
        handler
        for handler, dests, _ in registrations
        if "yes" in dests and confirmation_owner in _stage42_reachable(handler, reads, calls)
    }
    return {
        "envelope": (observed_envelope - envelope_scope, envelope_scope - observed_envelope),
        "confirmation": (
            observed_confirmation - confirmation_scope,
            confirmation_scope - observed_confirmation,
        ),
    }


def _stage42_applicable_registrations(
    registrations: list[tuple[_FunctionId, set[str], set[str]]],
    all_dests: set[str],
    *,
    owners: dict[str, _FunctionId] = _STAGE42_DESTINATION_OWNERS,
    handler_scope: dict[str, set[_FunctionId]] | None = None,
    allowed_local_overrides: dict[tuple[_FunctionId, str], str] = _STAGE42_DELIBERATE_LOCAL_OVERRIDES,
) -> list[tuple[_FunctionId, set[str], set[str]]]:
    """Project the maintained ownership contract; never infer it from reads."""

    applicable: list[tuple[_FunctionId, set[str], set[str]]] = []
    registered_by_dest = {
        dest: {handler for handler, dests, _ in registrations if dest in dests}
        for dest in all_dests
    }
    for dest in all_dests:
        if dest in _STAGE42_SATISFIED_BY_CONSTRUCTION:
            continue
        scoped_handlers = (handler_scope or {}).get(dest)
        if scoped_handlers is not None:
            for handler in scoped_handlers:
                identity = (
                    handler
                    if handler in registered_by_dest[dest]
                    else ("stage42", f"<unregistered:{handler}>")
                )
                applicable.append((identity, {dest}, set()))
        elif dest in owners:
            applicable.append((owners[dest], {dest}, set()))
        else:
            applicable.append((('stage42', '<missing-owner>'), {dest}, set()))

    for handler, registered_dests, collisions in registrations:
        for dest in registered_dests & collisions:
            if (handler, dest) not in allowed_local_overrides:
                applicable.append((handler, {dest}, {dest}))
    return applicable


def test_every_stage42_global_flag_is_honored():
    """Removed mission families cannot advertise or silently swallow flags."""

    for family in ("goal", "task", "run", "worker", "incident", "lane", "swarm"):
        with pytest.raises(SystemExit) as excinfo:
            parser().parse_args(["harness", family, "list", "--json"])
        assert excinfo.value.code == 2


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
    _add_stage42_global_args(one)
    one.set_defaults(func=handler_one)
    two = subs.add_parser('two')
    _add_stage42_global_args(two)
    two.set_defaults(func=handler_two)
""",
        {"limit"},
        {"limit"},
    )
    applicable = _stage42_applicable_registrations(
        registrations,
        {"limit"},
        owners={},
        handler_scope={"limit": {handler_one, handler_two}},
    )
    assert _stage42_unhonored_registrations(
        applicable,
        reads={handler_one: {"limit"}, handler_two: set()},
        calls={handler_one: set(), handler_two: set()},
        required_consumers={"limit": handler_one},
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
        {"limit"},
    )
    applicable = _stage42_applicable_registrations(
        registrations,
        {"limit"},
        owners={"limit": handler},
        allowed_local_overrides={},
    )
    assert _stage42_unhonored_registrations(
        applicable,
        reads={handler: {"limit"}},
        calls={handler: set()},
        required_consumers={"limit": handler},
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


def test_stage42_honored_gate_does_not_derive_ownership_from_an_unrelated_leaf_read():
    module = "hermes_cli.harness"
    handler = (module, "handler")
    designated = (module, "designated_cursor_owner")
    registrations = _stage42_parser_ownership(
        """
def build_parser():
    command = subs.add_parser('command')
    _add_stage42_global_args(command)
    command.set_defaults(func=handler)
""",
        {"cursor"},
        {"cursor"},
    )
    applicable = _stage42_applicable_registrations(
        registrations,
        {"cursor"},
        owners={"cursor": designated},
    )
    assert _stage42_unhonored_registrations(
        applicable,
        reads={handler: {"cursor"}, designated: set()},
        calls={handler: set(), designated: set()},
        required_consumers={"cursor": designated},
    ) == [(designated, "cursor")]


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
        {"output"},
    )
    applicable = _stage42_applicable_registrations(
        registrations,
        {"output", "no_color"},
        owners={},
        handler_scope={"output": {handler_one, handler_two}},
    )
    assert _stage42_unhonored_registrations(
        applicable,
        reads=reads,
        calls=calls,
        required_consumers={"output": ("shared", "consume")},
    ) == []


def test_stage42_envelope_scope_completeness_rejects_an_omitted_new_handler():
    handler = ("hermes_cli.harness", "new_envelope_handler")
    registrations = _stage42_parser_ownership(
        """
def build_parser():
    command = subs.add_parser('new')
    _add_stage42_global_args(command)
    command.set_defaults(func=new_envelope_handler)
""",
        {"output"},
        {"output", "yes"},
    )
    reads, calls = _stage42_function_facts(
        [
            (
                "hermes_cli.harness",
                """
def new_envelope_handler(args):
    return _print_stage42({}, args=args)

def _print_stage42(data, *, args):
    return args.output
""",
            )
        ]
    )
    drift = _stage42_shared_scope_drift(
        registrations,
        reads,
        calls,
        envelope_scope=set(),
        confirmation_scope=set(),
    )
    assert drift["envelope"] == ({handler}, set())


def test_stage42_confirmation_scope_completeness_rejects_an_omitted_new_handler():
    handler = ("hermes_cli.harness", "new_confirmation_handler")
    registrations = _stage42_parser_ownership(
        """
def build_parser():
    command = subs.add_parser('new')
    _add_stage42_global_args(command, mutation=True)
    command.set_defaults(func=new_confirmation_handler)
""",
        {"output"},
        {"output", "yes", "dry_run"},
    )
    reads, calls = _stage42_function_facts(
        [
            (
                "hermes_cli.harness",
                """
def new_confirmation_handler(args):
    return _require_yes(args)

def _require_yes(args):
    return args.yes or args.dry_run
""",
            )
        ]
    )
    drift = _stage42_shared_scope_drift(
        registrations,
        reads,
        calls,
        envelope_scope=set(),
        confirmation_scope=set(),
    )
    assert drift["confirmation"] == ({handler}, set())


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
