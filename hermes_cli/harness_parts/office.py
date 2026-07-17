# Mission Office CLI tier: `hermes harness office …`.
#
# This module is exec'd into hermes_cli/harness.py's globals (see
# _load_command_parts), so it reuses the shared Stage-42 envelope/printer/error
# helpers (_print_stage42, _list_envelope, _object_envelope, emit_harness_error,
# _load_request_json) defined there. All writes go through the OfficeStore
# chokepoint — the same one the launcher capability lane uses.
#
# Design contract: EterniaLauncher
# docs/mission_control/OFFICE_LAYOUT_REALM_SYNC_PLAN_2026-07-17.md. Actor keys
# are minted store-side (canonical_persona_instance_id at the boundary); the
# CLI passes the identity triple through verbatim.


def _office_store():
    from agent_runtime.office_store import OfficeStore

    return OfficeStore()


def _office_workspace_for(args) -> str | None:
    return getattr(args, "workspace", None) or WorkspaceStore().active_id()


def _office_item_row(item) -> dict:
    return {
        "item_id": item.item_id,
        "persona_id": item.persona_id,
        "kind": item.kind,
        "position": list(item.position),
        "folder": item.folder,
        "display_name": item.display_name,
        "pet_slug": item.pet_slug,
        "scale": item.scale,
    }


def _office_actor_row(actor, *, full: bool = False) -> dict:
    row = {
        "id": actor.actor_key,
        "workspace_id": actor.workspace_id,
        "persona_id": actor.persona_id,
        "persona_instance_id": actor.persona_instance_id,
        "items": len(actor.items),
        "state": actor.state,
        "revision": actor.revision,
        "updated_at": actor.updated_at,
    }
    if full:
        row["backing_profile"] = actor.backing_profile
        row["item_defs"] = [_office_item_row(item) for item in actor.items]
        row["updated_by"] = actor.updated_by
        row["created_at"] = actor.created_at
    return row


def _office_surface_row(store, workspace_id: str, *, full: bool = False) -> dict:
    surface = store.get_surface(workspace_id)
    actors = store.list_actors(workspace_id)
    row = {
        "workspace_id": surface.workspace_id,
        "folders": list(surface.folders),
        "actors": len(actors),
        "conflicts": len(store.conflict_actor_keys(workspace_id)),
        "revision": surface.revision,
        "updated_at": surface.updated_at,
    }
    if full:
        row["actor_defs"] = [_office_actor_row(actor, full=True) for actor in actors]
        row["archived_actor_keys"] = list(surface.archived_actor_keys)
        row["conflict_actor_keys"] = store.conflict_actor_keys(workspace_id)
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
    actor = store.upsert_actor(
        workspace,
        payload,
        updated_by=getattr(args, "updated_by", None) or "operator",
        expect_revision=getattr(args, "expect_revision", None),
    )
    _print_stage42(_object_envelope("office_actor", _office_actor_row(actor, full=True)), args=args, default_output="json")
    return 0


def _cmd_office_actor_remove(args) -> int:
    store = _office_store()
    workspace = _office_workspace_for(args)
    if not workspace:
        return emit_harness_error(ValueError("no workspace selected; pass --workspace"), args=args, code="invalid_request")
    actor = store.remove_actor(
        workspace,
        args.actor,
        reason=getattr(args, "reason", None) or "operator",
        expect_revision=getattr(args, "expect_revision", None),
    )
    _print_stage42(_object_envelope("office_actor", _office_actor_row(actor, full=True)), args=args, default_output="json")
    return 0


def _cmd_office_actor_restore(args) -> int:
    store = _office_store()
    workspace = _office_workspace_for(args)
    if not workspace:
        return emit_harness_error(ValueError("no workspace selected; pass --workspace"), args=args, code="invalid_request")
    actor = store.restore_actor(workspace, args.actor)
    _print_stage42(_object_envelope("office_actor", _office_actor_row(actor, full=True)), args=args, default_output="json")
    return 0


def _cmd_office_set_folders(args) -> int:
    store = _office_store()
    workspace = _office_workspace_for(args)
    if not workspace:
        return emit_harness_error(ValueError("no workspace selected; pass --workspace"), args=args, code="invalid_request")
    folders = [part.strip() for part in (getattr(args, "folders", None) or "").split(",") if part.strip()]
    surface = store.update_surface(
        workspace,
        folders=folders,
        expect_revision=getattr(args, "expect_revision", None),
    )
    _print_stage42(_object_envelope("office", _office_surface_row(store, surface.workspace_id)), args=args, default_output="json")
    return 0


def _cmd_office_resolve_conflict(args) -> int:
    store = _office_store()
    workspace = _office_workspace_for(args)
    if not workspace:
        return emit_harness_error(ValueError("no workspace selected; pass --workspace"), args=args, code="invalid_request")
    actor = store.resolve_conflict(workspace, args.actor, take=args.take)
    row = (
        _office_actor_row(actor, full=True)
        if actor is not None
        else {"id": args.actor, "workspace_id": workspace, "state": "archived", "take": args.take}
    )
    _print_stage42(_object_envelope("office_actor", row), args=args, default_output="json")
    return 0
