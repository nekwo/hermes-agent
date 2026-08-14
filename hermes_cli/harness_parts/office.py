# Mission Office CLI tier: `hermes harness office …`.
#
# This module is exec'd into hermes_cli/harness.py's globals (see
# _load_command_parts) and shares the Stage-42 envelope/printer/error helpers
# with every other tier — imported from hermes_cli.harness_support below, not
# inherited. All writes go through the OfficeStore chokepoint — the same one
# the launcher capability lane uses.
#
# Design contract: EterniaLauncher
# docs/mission_control/OFFICE_LAYOUT_REALM_SYNC_PLAN_2026-07-17.md. Actor keys
# are minted store-side (canonical_persona_instance_id at the boundary); the
# CLI passes the identity triple through verbatim.

# Explicit import header. Still exec'd into harness.py's globals by
# _load_command_parts — that mechanism is unchanged — but no longer dependent
# on it: these names used to arrive implicitly from whatever harness.py
# imported, so a wrong one surfaced as a NameError only when an operator ran
# the one verb that touched it. Re-importing a name harness.py also imports
# rebinds it to the identical object; both halves are checked by
# tests/hermes_cli/test_harness_parts_namespace.py.
#
# Snapshot row builders (``office_summary_row`` /
# ``_office_actor_summary_row``) are imported FUNCTION-LOCALLY rather than
# here on purpose: a module-level import in an exec'd part binds the name into
# harness.py's shared globals for every other tier, which is the shadowing
# surface the namespace guard exists to police. Same convention
# ``_office_store`` already follows.

from __future__ import annotations

from agent_runtime.store import WorkspaceStore
from hermes_cli.harness_support import (
    _load_request_json,
    _object_envelope,
    _print_stage42,
    emit_harness_error,
)


def _office_store():
    from agent_runtime.office_store import OfficeStore

    return OfficeStore()


def _office_workspace_for(args) -> str | None:
    return getattr(args, "workspace", None) or WorkspaceStore().active_id()


def _office_actor_row(actor, *, full: bool = False, summary: dict | None = None) -> dict:
    """One office actor row — a RE-KEY of the snapshot's own
    ``_office_actor_summary_row`` (S48, ledger item 4).

    ``_office_item_row`` went with the consolidation: it re-declared, key for
    key, the item block the snapshot builder already projects. Two copies of a
    scene-item schema is exactly how the workspace/realm rows drifted.

    CLI-only fields, all three plain model scalars the wire never carried:
    ``workspace_id``, ``state``, ``created_at``.

    ``summary`` lets ``_office_surface_row`` pass the row the surface builder
    ALREADY projected for this actor (it owns the per-surface cap).
    """

    if summary is None:
        from agent_runtime.snapshot import _office_actor_summary_row

        summary = _office_actor_summary_row(actor, unpublished=None)
    row = {
        "id": summary["actor_key"],
        "workspace_id": actor.workspace_id,
        "persona_id": summary["persona_id"],
        "persona_instance_id": summary["persona_instance_id"],
        "items": len(summary["items"]),
        "state": actor.state,
        "revision": summary["revision"],
        "updated_at": actor.updated_at,
    }
    if full:
        row["backing_profile"] = summary["backing_profile"]
        row["item_defs"] = summary["items"]
        row["updated_by"] = summary["updated_by"]
        row["created_at"] = actor.created_at
    return row


def _office_surface_row(store, workspace_id: str, *, full: bool = False, surface=None) -> dict:
    """`office show|folders|actor …` surface row — a RE-KEY of the snapshot's
    own ``office_summary_row`` (S48, ledger item 4).

    ``--full`` used to print EVERY actor; the wire had been bounding the actor
    list at ``MAX_OFFICE_ACTORS_PROJECTED`` all along. The bound is now the
    builder's and it is ACCOUNTED — ``actors_truncated`` rides the full row
    whenever actors were cut.

    ``surface`` lets a dry-run render the WOULD-BE surface (folders/revision the
    write would produce) instead of re-reading the untouched on-disk copy. Actor
    counts/conflicts stay disk-sourced — a folder edit never touches actors.

    Function-local builder import: see the module header on part namespaces.
    """

    from agent_runtime.snapshot import office_summary_row

    if surface is None:
        surface = store.get_surface(workspace_id)
    actors = store.list_actors(workspace_id)
    conflict_actor_keys = store.conflict_actor_keys(workspace_id)
    summary = office_summary_row(surface, actors, conflict_actor_keys=conflict_actor_keys)
    row = {
        "workspace_id": summary["workspace_id"],
        "folders": summary["folders"],
        "actors": summary["actor_count"],
        "conflicts": len(summary["conflict_actor_keys"]),
        "revision": summary["revision"],
        "updated_at": surface.updated_at,
    }
    if full:
        by_key = {actor.actor_key: actor for actor in actors}
        row["actor_defs"] = [
            _office_actor_row(by_key[projected["actor_key"]], full=True, summary=projected)
            for projected in summary["actors"]
        ]
        row["actors_truncated"] = summary["actors_truncated"]
        row["archived_actor_keys"] = summary["archived_actor_keys"]
        row["conflict_actor_keys"] = summary["conflict_actor_keys"]
    return row


def _cmd_office_show(args) -> int:
    store = _office_store()
    workspace = _office_workspace_for(args)
    if not workspace:
        return emit_harness_error(ValueError("no workspace selected; pass --workspace"), args=args, code="invalid_request")
    if not store.surface_exists(workspace):
        # An unauthored office is an honest empty, not an error.
        _print_stage42(
            _object_envelope(
                "office",
                {"workspace_id": workspace, "folders": [], "actors": 0, "conflicts": 0, "revision": 0, "updated_at": None},
            ),
            args=args,
        )
        return 0
    _print_stage42(_object_envelope("office", _office_surface_row(store, workspace, full=bool(getattr(args, "full", False)))), args=args)
    return 0


def _cmd_office_actor_upsert(args) -> int:
    """`harness office actor-upsert` — the operator's raw placement writer.

    Also the LAUNCHER's save path: the Flutter bridge shells out to this exact
    verb with ``--actor-json`` (mission_control_bridge.dart, `actor-upsert`),
    so whatever this verb permits is what the live canvas can persist.

    Class-key policy — why ``--persona-instance-id`` is OPTIONAL, not required:

    Class-keyed placements are a supported shape, not a defect. The store says
    so itself (``archive_actors_for_instance``: "Persona-id-keyed placements
    survive instance churn by design"), and the launcher codec emits a payload
    with no binding whenever a canvas group has none. A hard
    ``required=True`` would outlaw a legal placement and break the launcher's
    save for every unbound group — a migration fence is not worth that.

    Warn-and-proceed is the other wrong answer: this verb's default output is
    JSON consumed by the bridge, and the harm it would warn about is silent
    data corruption discovered days later. A warning nobody reads is the same
    as no guard.

    So: the flag is a convenience (fill/override the payload's binding without
    hand-editing JSON), and the GUARD is conditional. A class-keyed write is
    refused only when it would actually undo the re-key migration — archived
    class key, or duplicate item ids against an instance-keyed sibling. Every
    other class-keyed write proceeds untouched, and ``--allow-class-key`` is
    the documented escape hatch for the operator who means it (it forces the
    write and rides an ``office_actor_class_key_forced`` warning, so the
    override is on the record rather than invisible).
    """

    from agent_runtime.office_class_key_guard import (
        CLASS_KEY_REFUSAL_CODE,
        ClassKeyedPlacementRefused,
        class_key_collision,
        refusal_message,
    )

    store = _office_store()
    workspace = _office_workspace_for(args)
    if not workspace:
        return emit_harness_error(ValueError("no workspace selected; pass --workspace"), args=args, code="invalid_request")
    try:
        payload = _load_request_json(args.actor_json)
    except Exception as exc:  # noqa: BLE001
        return emit_harness_error(exc, args=args, code="invalid_payload")
    if not isinstance(payload, dict):
        return emit_harness_error(ValueError("--actor-json must be an actor object"), args=args, code="invalid_request")
    instance_id = str(getattr(args, "persona_instance_id", None) or "").strip()
    if instance_id:
        # The flag wins over the JSON: an operator who typed it is being more
        # specific than the file they piped in. The store still mints the key.
        payload = {**payload, "persona_instance_id": instance_id}

    warnings: list[dict] = []
    collision = class_key_collision(store, workspace, payload)
    if collision is not None:
        message = refusal_message(collision)
        if not bool(getattr(args, "allow_class_key", False)):
            return emit_harness_error(
                ClassKeyedPlacementRefused(message, safe_details=collision),
                args=args,
                code=CLASS_KEY_REFUSAL_CODE,
                message=message,
            )
        warnings.append(
            {
                "code": "office_actor_class_key_forced",
                "actor_key": collision["class_actor_key"],
                "message": message,
                "reasons": collision["reasons"],
                "conflicting_actor_keys": collision["conflicting_actor_keys"],
            }
        )

    dry_run = bool(getattr(args, "dry_run", False))
    actor = store.upsert_actor(
        workspace,
        payload,
        updated_by=getattr(args, "updated_by", None) or "operator",
        expect_revision=getattr(args, "expect_revision", None),
        dry_run=dry_run,
    )
    envelope = _object_envelope("office_actor", _office_actor_row(actor, full=True), warnings=warnings or None)
    if dry_run:
        envelope["dry_run"] = True
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def _cmd_office_actor_remove(args) -> int:
    store = _office_store()
    workspace = _office_workspace_for(args)
    if not workspace:
        return emit_harness_error(ValueError("no workspace selected; pass --workspace"), args=args, code="invalid_request")
    dry_run = bool(getattr(args, "dry_run", False))
    actor = store.remove_actor(
        workspace,
        args.actor,
        reason=getattr(args, "reason", None) or "operator",
        expect_revision=getattr(args, "expect_revision", None),
        dry_run=dry_run,
    )
    envelope = _object_envelope("office_actor", _office_actor_row(actor, full=True))
    if dry_run:
        envelope["dry_run"] = True
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def _cmd_office_actor_restore(args) -> int:
    store = _office_store()
    workspace = _office_workspace_for(args)
    if not workspace:
        return emit_harness_error(ValueError("no workspace selected; pass --workspace"), args=args, code="invalid_request")
    dry_run = bool(getattr(args, "dry_run", False))
    actor = store.restore_actor(workspace, args.actor, dry_run=dry_run)
    envelope = _object_envelope("office_actor", _office_actor_row(actor, full=True))
    if dry_run:
        envelope["dry_run"] = True
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def _cmd_office_set_folders(args) -> int:
    store = _office_store()
    workspace = _office_workspace_for(args)
    if not workspace:
        return emit_harness_error(ValueError("no workspace selected; pass --workspace"), args=args, code="invalid_request")
    folders = [part.strip() for part in (getattr(args, "folders", None) or "").split(",") if part.strip()]
    dry_run = bool(getattr(args, "dry_run", False))
    surface = store.update_surface(
        workspace,
        folders=folders,
        expect_revision=getattr(args, "expect_revision", None),
        dry_run=dry_run,
    )
    row = _office_surface_row(store, surface.workspace_id, surface=surface if dry_run else None)
    envelope = _object_envelope("office", row)
    if dry_run:
        envelope["dry_run"] = True
    _print_stage42(envelope, args=args, default_output="json")
    return 0


def _cmd_office_resolve_conflict(args) -> int:
    """`harness office resolve-conflict` — adopt one side of a realm-sync conflict.

    ``--take remote`` is fenced against the class→instance re-key migration in
    the STORE (``OfficeStore._guard_class_keyed_adoption``), because that branch
    writes peer data past ``upsert_actor``. Caught and re-emitted here so the
    refusal lands as ``duplicate_conflict`` (exit 4) like the other writers':
    ``emit_harness_error`` reads ``AgentRuntimeError`` subclasses it does not
    name as ``internal_error``, and an operator refusal is not an internal error.
    """

    from agent_runtime.office_class_key_guard import CLASS_KEY_REFUSAL_CODE, ClassKeyedPlacementRefused

    store = _office_store()
    workspace = _office_workspace_for(args)
    if not workspace:
        return emit_harness_error(ValueError("no workspace selected; pass --workspace"), args=args, code="invalid_request")
    dry_run = bool(getattr(args, "dry_run", False))
    try:
        actor = store.resolve_conflict(
            workspace,
            args.actor,
            take=args.take,
            allow_class_key=bool(getattr(args, "allow_class_key", False)),
            dry_run=dry_run,
        )
    except ClassKeyedPlacementRefused as exc:
        return emit_harness_error(exc, args=args, code=CLASS_KEY_REFUSAL_CODE, message=str(exc))
    row = (
        _office_actor_row(actor, full=True)
        if actor is not None
        else {"id": args.actor, "workspace_id": workspace, "state": "archived", "take": args.take}
    )
    envelope = _object_envelope("office_actor", row)
    if dry_run:
        envelope["dry_run"] = True
    _print_stage42(envelope, args=args, default_output="json")
    return 0
