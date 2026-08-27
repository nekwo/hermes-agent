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

# Explicit import header — its rationale lives ONCE, in
# ``hermes_cli/harness_support.py``'s module docstring, and the pair of
# guarantees it rests on are checked by
# tests/hermes_cli/test_harness_parts_namespace.py.
#
# Snapshot row builders (``office_summary_row`` /
# ``_office_actor_summary_row``) are imported FUNCTION-LOCALLY rather than
# here on purpose: a module-level import in an exec'd part binds the name into
# harness.py's shared globals for every other tier, which is the shadowing
# surface the namespace guard exists to police. Same convention
# ``_office_store`` already follows.

from __future__ import annotations

from agent_runtime.root_observability import attach_root_observability
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
    # ``scan_actors``, not ``list_actors``: the row this feeds accounts BOTH ways
    # the actor list can be short — the cut we chose and the files the platform
    # would not open (RD-H4 / EG-1.5). The CLI tier is the other reader of the
    # snapshot's row builder, and a reader that passed a shortened list would
    # re-open the hole at this seam.
    scan = store.scan_actors(workspace_id)
    actors = scan.actors
    conflict_actor_keys = store.conflict_actor_keys(workspace_id)
    summary = office_summary_row(
        surface,
        actors,
        actors_unreadable=scan.unreadable,
        conflict_actor_keys=conflict_actor_keys,
    )
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
        row["actors_unreadable"] = summary["actors_unreadable"]
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

    The fence itself is the STORE's (``OfficeStore._guard_class_keyed_write``,
    EG-6.6). This verb holds no copy of it: it CALLS the write, translates the
    typed refusal into the stage-42 exit taxonomy, and — on ``--allow-class-key``
    — replays the same write with the store's own override parameter, minting the
    warning out of the refusal the store already produced. Refuse-then-consent is
    deliberate: it means the recorded override names the exact collision the
    store found, and it means the flag cannot suppress a refusal nobody saw.
    """

    from agent_runtime.errors import ActorsUnreadable
    from agent_runtime.office_class_key_guard import (
        CLASS_KEY_REFUSAL_CODE,
        ClassKeyedPlacementRefused,
    )
    from agent_runtime.office_store import (
        DUPLICATE_DESK_REFUSAL_CODE,
        DuplicateDeskRefused,
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
    dry_run = bool(getattr(args, "dry_run", False))

    def _write(*, allow_class_key: bool):
        return store.upsert_actor(
            workspace,
            payload,
            updated_by=getattr(args, "updated_by", None) or "operator",
            expect_revision=getattr(args, "expect_revision", None),
            allow_class_key=allow_class_key,
            dry_run=dry_run,
        )

    try:
        actor = _write(allow_class_key=False)
    except ActorsUnreadable as exc:
        # The fence could not READ the actor directory it must consult, so it
        # refused instead of answering "no conflict" from half of it. Its own
        # code rather than ``duplicate_conflict``: nothing collided — the store
        # declined to guess — and the cure is repairing a file, not re-shaping a
        # payload.
        #
        # This arm is no longer what saves the condition from ``internal_error``:
        # the taxonomy carries it now (``_error_code_for_exception`` maps every
        # ``ArchiveUnreadable`` to ``exc.code``, exit family 7), which is what
        # covers the office write verbs that have no arm of their own.
        #
        # Kept anyway, and NOT because it changes the answer — it does not. It is
        # the verb-local statement at the seam that raises: the ONE write that
        # runs the class-key fence says out loud, where the fence is called, that
        # an unprovable answer is a hold and not a collision. Passing
        # ``message=str(exc)`` hands over the fence's own sentence (both cures,
        # under the 300-char safe-message bound) instead of a reconstruction, and
        # passing ``code=exc.code`` rather than letting the mapping infer it means
        # a divergence between the two would have to be written deliberately.
        return emit_harness_error(exc, args=args, code=exc.code, message=str(exc))
    except DuplicateDeskRefused as exc:
        # The desk fence (D6), translated into the stage-42 taxonomy and NOT
        # given a consent flag. ``--allow-class-key`` exists because an operator
        # can legitimately want the pre-migration shape back; there is no
        # legitimate second desk, so there is no flag and this arm is terminal.
        #
        # ``message=str(exc)`` hands over the store's own sentence — which names
        # the holding actor, the holding item and `harness office actor-remove`
        # — rather than a reconstruction: ``emit_harness_error`` merges
        # ``safe_details`` only for three exception types it lists, and this is
        # not one of them, so the message is the only place the holder can ride.
        # ``code=`` explicitly rather than through ``_error_code_for_exception``
        # so a divergence between the two would have to be written on purpose.
        return emit_harness_error(
            exc, args=args, code=DUPLICATE_DESK_REFUSAL_CODE, message=str(exc)
        )
    except ClassKeyedPlacementRefused as exc:
        collision = exc.safe_details
        if not bool(getattr(args, "allow_class_key", False)):
            return emit_harness_error(
                exc,
                args=args,
                code=CLASS_KEY_REFUSAL_CODE,
                message=str(exc),
            )
        # Consent, on the record and derived from the refusal itself: the warning
        # names the collision the STORE found rather than one this verb went and
        # computed, so the two can never describe different faults.
        warnings.append(
            {
                "code": "office_actor_class_key_forced",
                "actor_key": collision["class_actor_key"],
                "message": str(exc),
                "reasons": collision["reasons"],
                "conflicting_actor_keys": collision["conflicting_actor_keys"],
            }
        )
        actor = _write(allow_class_key=True)
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


def _cmd_office_archive_surface(args) -> int:
    """`harness office archive-surface` — the operator's exit from an orphan.

    An office surface whose workspace record has gone raises an
    ``orphaned_office`` parity warning (``snapshot._office_parity_warnings``) on
    every frame, and until this verb existed the only way to clear it was
    deleting directories by hand inside the live runtime root. Board cards have
    had an "archive to repair" route since inception; the office side had none,
    which is how one leaked test workspace kept a HUD chip lit indefinitely.

    Archive-never-delete: the surface directory is MOVED under
    ``store/office_archive/``, so a mistake is recoverable.

    The store REFUSES a surface whose workspace still resolves — pointed at a
    live workspace this verb would move every placement on it out of the
    projection. That refusal is an ``invalid_request`` (exit 2), not an internal
    error: the operator named the wrong workspace.
    """

    from agent_runtime.errors import NotFound

    store = _office_store()
    workspace = _office_workspace_for(args)
    if not workspace:
        return emit_harness_error(ValueError("no workspace selected; pass --workspace"), args=args, code="invalid_request")
    dry_run = bool(getattr(args, "dry_run", False))
    try:
        row = store.archive_orphaned_surface(workspace, dry_run=dry_run)
    except NotFound as exc:
        return emit_harness_error(exc, args=args, code="not_found")
    except ValueError as exc:
        return emit_harness_error(exc, args=args, code="invalid_request", message=str(exc))
    envelope = attach_root_observability(_object_envelope("office_archived", row))
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
