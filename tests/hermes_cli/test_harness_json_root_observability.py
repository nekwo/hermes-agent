"""Root-observability gate: every ``--json`` harness verb states WHICH root
answered, or carries a written reason why not.

The defect class (2026-08-12 ambient chat-history incident): a wrong runtime
root returns a well-formed EMPTY answer — ``ok: true, count: 0`` —
indistinguishable from genuinely-empty data, so any diagnostic that resolves
its own root always confirms health. The only durable fix is structural: the
envelope itself must carry its frame of reference
(``agent_runtime/root_observability.py``), and a NEW ``--json`` verb that
ships without it must fail CI until consciously classified below.

Pattern per ``tests/agent_runtime/test_store_event_invariant.py``: AST scan +
justified ledger + anti-rot checks on the ledger itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

import hermes_cli.harness as harness_module

_HARNESS_PY = Path(harness_module.__file__)
_PARTS_DIR = _HARNESS_PY.parent / "harness_parts"

# Calls whose presence marks a handler as a JSON-envelope emitter.
_EMIT_CALLS = {"emit_json", "_print_stage42"}
# The one attach chokepoint (agent_runtime/root_observability.py).
_ATTACH_CALL = "attach_root_observability"

# Chat lanes must also stamp ``chat_scope`` — ``source: "ambient_home"``
# beside an empty result is the tell the incident lacked entirely.
#
# ``_cmd_status`` is here for the other half of the same property: it is the
# ORIENTATION verb, and since 2026-08-13 the head home is a durable runtime
# declaration (``config_declared``) rather than a string only the Launcher's
# spawn environment carried. Status must therefore be able to say which rung
# named the head it would read from — otherwise the runtime declares its own
# identity and the verb an operator uses to check it still cannot show it.
#
# ``_cmd_persona_instance_chat_bindings`` judges chat bindings AGAINST a
# SessionDB, so a wrong head corrupts both of its answers: ``stale_count: 0``
# reads as a clean store, and a named stale row is a false accusation against a
# live instance (the 2026-07-25 incident cleared 10 healthy bindings exactly
# that way). The verb refuses to answer unless the head was named — the stamp
# is how the envelope SHOWS which one.
CHAT_SCOPE_REQUIRED = {
    "_cmd_persona_chat_history",
    "_cmd_persona_instance_chat_bindings",
    "_cmd_status",
}

_BACKLOG_REASON = (
    "predates the resolution block (2026-08-12 root-observability wave); "
    "route the envelope through attach_root_observability when this verb is "
    "next touched. Do NOT add new verbs to this list with this reason."
)

# Handlers allowed to emit JSON WITHOUT the standard resolution block, each
# with the reason the exemption is sound. Adding a name here is a reviewed
# decision, not a default.
LEDGER: dict[str, str] = {
    name: _BACKLOG_REASON
    for name in (
        # harness.py
        "_cmd_roots_list", "_cmd_roots_set", "_cmd_roots_unset", "_cmd_roots_migrate",
        "_cmd_persona_instance_detail",
        "_cmd_skills_catalog", "_cmd_skills_publishable", "_cmd_skills_inbox",
        "_cmd_skills_promote", "_cmd_skills_inventory",
        "_cmd_prompt_context_show",
        "_cmd_workspace_list", "_cmd_workspace_show", "_cmd_workspace_create",
        "_cmd_workspace_delete", "_cmd_workspace_use",
        "_cmd_workspace_add_agent", "_cmd_workspace_remove_agent",
        "_cmd_workspace_rename", "_cmd_workspace_archive",
        "_cmd_realm_list", "_cmd_realm_show", "_cmd_realm_create",
        "_cmd_realm_bind_server", "_cmd_realm_use", "_cmd_realm_default_scope",
        "_cmd_realm_adopt", "_cmd_realm_sync_status", "_cmd_realm_sync_pull",
        "_cmd_realm_sync_publish", "_cmd_realm_sync_held", "_cmd_realm_sync_resolve",
        "_cmd_realm_skills_show", "_cmd_realm_skills_set",
        "_cmd_realm_agents_show", "_cmd_realm_agents_set",
        "_cmd_agent_set_profile",
        "_cmd_pets_gallery", "_cmd_pets_install", "_cmd_pets_sprite", "_cmd_pets_thumb",
        "_cmd_init", "_cmd_install_harness_skills", "_cmd_providers",
        # harness_parts/board.py
        "_cmd_board_list", "_cmd_board_show", "_cmd_board_create", "_cmd_board_update",
        "_cmd_board_card_add", "_cmd_board_card_edit", "_cmd_board_card_move",
        "_cmd_board_card_archive", "_cmd_board_card_restore",
        "_cmd_board_resolve_conflict",
        # harness_parts/checkpoint_commands.py
        "_cmd_checkpoint_fetch", "_cmd_checkpoint_classes",
        # harness_parts/flow_commands.py
        "_cmd_flow_set", "_cmd_flow_show", "_cmd_flow_list",
        # harness_parts/office.py
        "_cmd_office_show", "_cmd_office_actor_upsert", "_cmd_office_actor_remove",
        "_cmd_office_actor_restore", "_cmd_office_set_folders",
        "_cmd_office_resolve_conflict",
        # harness_parts/persona_commands.py
        "_cmd_persona_list", "_cmd_persona_show", "_cmd_persona_tool_diff",
        "_cmd_persona_permission_set", "_cmd_persona_assignments",
        "_cmd_persona_assignment_task_id_migration",
        "_cmd_persona_instance_create", "_cmd_persona_instance_open_chat",
        "_cmd_persona_instance_open_new_chat", "_cmd_persona_chat_delete",
        "_cmd_mission_chat_steer", "_cmd_mission_chat_queue_skill",
        "_cmd_mission_chat_clarify_tickets", "_cmd_mission_chat_turn_resolve",
        "_cmd_persona_instance_close", "_cmd_persona_instance_retire",
        "_cmd_persona_instance_repair_steering", "_cmd_persona_instance_steer",
        "_cmd_persona_instance_return_summary", "_cmd_persona_instance_update_profile",
        "_cmd_persona_instance_set_model", "_cmd_persona_set_model",
        # harness_parts/runtime_commands.py
        "_cmd_worktree_reap", "_cmd_persona_instance_reconcile",
        "_cmd_health", "_cmd_config", "_cmd_migrate", "_cmd_observe",
        # ``_cmd_rebuild_read_model`` / ``_cmd_read_projection`` stood here until
        # Stage 6 (2026-08-22) retired the read_model.db lane with both verbs.
        "_cmd_contracts_dump",
        "_cmd_work_list", "_cmd_work_peek", "_cmd_work_cancel",
    )
}
LEDGER.update(
    {
        # The snapshot frame carries the builder's OWN block —
        # ``parity.resolution`` (agent_runtime/snapshot.py) — for both the CLI
        # print and the serve/read-model cache lanes. A second top-level copy
        # of the same answer on the same wire would be the S48 duplication
        # class, so the verb is exempt rather than double-stamped. Pinned by
        # test_snapshot_frame_already_carries_parity_resolution below.
        "_cmd_snapshot": "frame carries parity.resolution from the builder",
        # ``verify`` packets have stated ``runtime_root`` / ``hermes_home`` /
        # ``hermes_profile`` at top level since inception — historically the
        # ONE verb that said which root it checked.
        "_cmd_verify": "packet already states runtime_root at top level",
    }
)


def _scan_files() -> list[Path]:
    return [_HARNESS_PY, *sorted(_PARTS_DIR.glob("*.py"))]


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _handlers() -> dict[str, dict]:
    """Map handler name → {emits, attaches, chat_scope} for every ``_cmd_*``.

    A handler that hands its payload to a local ``_emit_*`` helper emits what
    that helper emits. That indirection is not incidental: the R-C5 lowering
    (``13c1d67178``) gave the open-chat and mission-chat verbs a
    ``_emit_<verb>_payload`` seam so an in-process serve caller can take the row
    without ``redirect_stdout`` rebinding the whole process's stdout, and the
    usage verb has had ``_emit_usage_json`` since it was written. A scan that
    only saw DIRECT ``emit_json`` calls read that refactor as "this verb stopped
    emitting JSON" and asked for the ledger entry to be deleted — which would
    have retired the exemption on a verb that emits as much as it ever did.

    The follow is one named seam, not a general call-graph walk, and the
    difference is measured: following ``_emit_*`` adds three handlers, while
    following EVERY local callee adds thirty — twenty of which are neither
    classified nor attaching. Those twenty are a real hole in this gate and a
    workstream of their own; they are not silently absorbed here.
    """

    direct: dict[str, dict] = {}
    calls: dict[str, set[str]] = {}
    for path in _scan_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not (node.name.startswith("_cmd_") or node.name.startswith("_emit_")):
                continue
            emits = attaches = chat_scope = False
            called: set[str] = set()
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                name = _call_name(sub)
                if name is None:
                    continue
                if name.startswith("_emit_"):
                    called.add(name)
                if name in _EMIT_CALLS:
                    emits = True
                if name == _ATTACH_CALL:
                    attaches = True
                    for keyword in sub.keywords:
                        if (
                            keyword.arg == "chat_scope"
                            and isinstance(keyword.value, ast.Constant)
                            and keyword.value.value is True
                        ):
                            chat_scope = True
            assert node.name not in direct or direct[node.name]["emits"] == emits, (
                f"duplicate handler name {node.name!r} with diverging shape — "
                "the name-keyed ledger below can no longer address it"
            )
            direct[node.name] = {
                "emits": emits,
                "attaches": attaches,
                "chat_scope": chat_scope,
            }
            calls[node.name] = called

    # Fixpoint rather than one hop: an ``_emit_*`` seam is free to delegate to
    # another one, and a cycle must not hang the scan.
    changed = True
    while changed:
        changed = False
        for name, called in calls.items():
            for target in called:
                if target == name or target not in direct:
                    continue
                for key in ("emits", "attaches", "chat_scope"):
                    if direct[target][key] and not direct[name][key]:
                        direct[name][key] = True
                        changed = True

    return {
        name: info for name, info in direct.items() if name.startswith("_cmd_")
    }


def test_every_json_verb_states_its_root_or_is_classified():
    handlers = _handlers()
    assert handlers, "AST scan found no _cmd_ handlers — the scan itself broke"

    unclassified = sorted(
        name
        for name, info in handlers.items()
        if info["emits"] and not info["attaches"] and name not in LEDGER
    )
    assert not unclassified, (
        "JSON-emitting harness verb(s) that state no runtime root and carry no "
        f"reviewed ledger reason: {unclassified}. A wrong root returns a "
        "well-formed EMPTY answer (ok: true, count: 0), so an envelope that "
        "does not say which root answered cannot be trusted when it is empty. "
        "Route the payload through "
        "agent_runtime.root_observability.attach_root_observability, or add a "
        "justified LEDGER entry in this test."
    )


def test_chat_lanes_stamp_chat_scope():
    handlers = _handlers()
    for name in sorted(CHAT_SCOPE_REQUIRED):
        info = handlers.get(name)
        assert info is not None, f"chat-lane handler {name!r} no longer exists"
        assert info["chat_scope"], (
            f"{name!r} must call {_ATTACH_CALL}(..., chat_scope=True): an empty "
            "chat read without chat_scope.source is the exact envelope the "
            "2026-08-12 incident could not distinguish from a lost transcript"
        )


def test_the_scan_sees_the_known_adopters():
    """Self-check against detection vacuity: if the attach predicate stops
    matching (rename, import shape change), this fails before the main gate
    silently passes every future verb."""

    handlers = _handlers()
    for name in ("_cmd_status", "_cmd_agent_list", "_cmd_doctor", "_cmd_persona_chat_history"):
        assert handlers.get(name, {}).get("attaches"), (
            f"{name!r} should be detected as attaching the resolution block — "
            "either it regressed or the scan's attach predicate broke"
        )


def test_ledger_does_not_rot():
    """Every ledger entry must still name a real, JSON-emitting handler that
    still lacks the attach call — otherwise the entry is stale and must go."""

    handlers = _handlers()
    for name in LEDGER:
        info = handlers.get(name)
        assert info is not None, f"LEDGER entry {name!r} names no handler — remove it"
        assert info["emits"], f"LEDGER entry {name!r} no longer emits JSON — remove it"
        assert not info["attaches"], (
            f"LEDGER entry {name!r} now attaches the resolution block — remove "
            "the stale exemption"
        )


def test_snapshot_frame_already_carries_parity_resolution():
    """The ``_cmd_snapshot`` ledger reason is a claim about the producer; pin
    it at the producer so the exemption cannot outlive the block."""

    snapshot_source = (
        Path(harness_module.__file__).parent.parent
        / "agent_runtime"
        / "snapshot.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(snapshot_source)
    stamped = any(
        isinstance(node, ast.Dict)
        and any(
            isinstance(key, ast.Constant) and key.value == "resolution"
            for key in node.keys
        )
        for node in ast.walk(tree)
    )
    assert stamped, (
        "agent_runtime/snapshot.py no longer stamps a 'resolution' key — the "
        "_cmd_snapshot LEDGER exemption is stale; attach the block in the verb"
    )
