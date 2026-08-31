"""Workspace template copy — the create-from-template content lane.

``harness workspace create --from-workspace <id>`` copies a source
workspace's authored STRUCTURE into a freshly created workspace:

- ``office``   — Mission Office folder taxonomy + active actor placements
- ``board``    — the default kanban board's active cards
- ``agents``   — the workspace agent roster (consumed at create time)
- ``settings`` — isolation / max lanes / default blueprint (create time)

Everything flows through the OfficeStore / BoardStore write chokepoints, so
every copied artifact emits its contract event and converges under realm
sync exactly like an operator write (Stage 12 — no silent store writes).
Goals, runs, chat history, and archives are deliberately NEVER copied: a
template is structure, not history. Copying across realms is legal because
office/board content is keyed only by workspace id.

Per-artifact failures degrade to typed warnings instead of aborting the
create: the workspace already exists at that point, and a half-copied
template with an honest warning beats a rolled-back create with a dead
workspace id in the event log.

Office actor copies carry the source's ``persona_instance_id`` through, so a
bound source placement copies as a bound placement. A source placement with NO
binding produces a CLASS-KEYED write, which is refused per-actor
(``office_actor_class_key_refused``) when the destination already holds the
same persona under an instance key or has that class key archived. The refusal is
the STORE's (``OfficeStore._guard_class_keyed_write``, EG-6.6) and this module
only translates it into a copy warning — see ``office_class_key_guard``. Copying
class-keyed actors into a workspace with no
office of its own is the normal path and stays untouched.
"""

from __future__ import annotations

from typing import Any

COPY_SCOPES = ("office", "board", "agents", "settings")
# Scopes materialized AFTER the workspace exists (content stores). The
# ``agents`` / ``settings`` scopes are consumed by the create verb itself.
CONTENT_COPY_SCOPES = ("office", "board")


def normalize_copy_scopes(raw: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Deduped, order-preserving scope list; empty/None means ALL scopes."""
    if not raw:
        return COPY_SCOPES
    scopes = tuple(dict.fromkeys(scope for scope in raw if scope in COPY_SCOPES))
    return scopes or COPY_SCOPES


def copy_workspace_content(
    source_workspace_id: str,
    dest_workspace_id: str,
    *,
    scopes: tuple[str, ...],
    updated_by: str = "operator",
) -> dict[str, Any]:
    """Copy office/board content between workspaces via the store chokepoints.

    Returns ``{"copied": {...counts...}, "warnings": [...]}``; callers attach
    both to the create envelope so the operator sees exactly what a template
    produced (and what it could not).
    """
    copied = {"office_actors": 0, "office_folders": 0, "board_cards": 0}
    warnings: list[dict[str, Any]] = []
    if "office" in scopes:
        _copy_office(source_workspace_id, dest_workspace_id, updated_by=updated_by, copied=copied, warnings=warnings)
    if "board" in scopes:
        _copy_default_board(source_workspace_id, dest_workspace_id, updated_by=updated_by, copied=copied, warnings=warnings)
    return {"copied": copied, "warnings": warnings}


def _copy_office(
    source_workspace_id: str,
    dest_workspace_id: str,
    *,
    updated_by: str,
    copied: dict[str, int],
    warnings: list[dict[str, Any]],
) -> None:
    from .office_class_key_guard import ClassKeyedPlacementRefused
    from .office_store import OfficeStore

    store = OfficeStore()
    if not store.surface_exists(source_workspace_id):
        return
    try:
        surface = store.get_surface(source_workspace_id)
    except Exception as exc:  # noqa: BLE001 — a broken source surface degrades to a warning
        warnings.append({"code": "office_surface_copy_failed", "message": str(exc)})
        return
    store.ensure_surface(dest_workspace_id, created_by=updated_by)
    if surface.folders:
        try:
            store.update_surface(dest_workspace_id, folders=list(surface.folders), updated_by=updated_by)
            copied["office_folders"] = len(surface.folders)
        except Exception as exc:  # noqa: BLE001
            warnings.append({"code": "office_folders_copy_failed", "message": str(exc)})
    # Instance-BOUND actors copy first so the class-key guard below is
    # order-independent: a source that itself carries a bound and an unbound
    # placement of one persona (a pre-existing double placement) then lands as
    # the bound copy plus a named refusal, not as a faithfully duplicated mess.
    # ``scan_actors`` sorts by actor_key, which would otherwise put the bare
    # class key first purely because "dev" < "personainst_dev_…".
    #
    # ``.actors``, shortfall dropped, and the drop is ACCOUNTED one level up:
    # this function already degrades every office fault to a ``warnings`` row
    # rather than failing the clone, and an actor whose source file will not
    # decode simply does not copy. Naming it here so the choice is visible
    # (AX5); widening ``warnings`` with a ``office_actors_unreadable`` row is
    # the honest next step and is filed, not smuggled in.
    scan = store.scan_actors(source_workspace_id)
    source_actors = sorted(scan.actors, key=lambda a: not a.persona_instance_id)
    for actor in source_actors:
        payload = {
            "persona_id": actor.persona_id,
            # Already threaded: a bound source actor copies as a bound payload,
            # so the destination store mints the same instance key. Only a
            # source actor with NO binding produces a class-keyed write.
            "persona_instance_id": actor.persona_instance_id,
            "backing_profile": actor.backing_profile,
            "items": [
                {
                    "item_id": item.item_id,
                    "persona_id": item.persona_id,
                    "kind": item.kind,
                    "position": list(item.position),
                    "folder": item.folder,
                    "display_name": item.display_name,
                    "pet_slug": item.pet_slug,
                    "scale": item.scale,
                }
                for item in actor.items
            ],
        }
        try:
            store.upsert_actor(dest_workspace_id, payload, updated_by=updated_by)
            copied["office_actors"] += 1
        except ClassKeyedPlacementRefused as exc:
            # REFUSED by the store's fence, and this lane never asks it to stand
            # down: a template apply holds no operator intent about THIS
            # destination, so it cannot be the caller that decides an archived
            # class key should come back. ``allow_class_key`` is never passed here
            # (unlike the CLI) for the same reason — nobody is in the loop at copy
            # time to take responsibility for the double placement.
            #
            # A named WARNING and not a raise: one hostile actor must not abort a
            # whole template. The remaining actors still copy, and the create
            # envelope carries what the template could not do.
            warnings.append(
                {
                    "code": "office_actor_class_key_refused",
                    "actor_key": actor.actor_key,
                    "message": str(exc),
                    "reasons": exc.safe_details["reasons"],
                    "conflicting_actor_keys": exc.safe_details["conflicting_actor_keys"],
                }
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                {
                    "code": "office_actor_copy_failed",
                    "actor_key": actor.actor_key,
                    "message": str(exc),
                }
            )


def _copy_default_board(
    source_workspace_id: str,
    dest_workspace_id: str,
    *,
    updated_by: str,
    copied: dict[str, int],
    warnings: list[dict[str, Any]],
) -> None:
    from . import board_models
    from .board_store import BoardStore

    store = BoardStore()
    source_board_id = board_models.default_board_id(source_workspace_id)
    try:
        source_cards = store.list_cards(source_board_id)
    except Exception:  # noqa: BLE001 — no source board = nothing to copy
        return
    # Materialize the destination default board even for an empty source, so
    # a "board" copy always yields the same deterministic board shape.
    store.ensure_default_board(dest_workspace_id, created_by=updated_by)
    dest_board_id = board_models.default_board_id(dest_workspace_id)
    for card in source_cards:
        try:
            store.add_card(
                board_id=dest_board_id,
                title=card.title,
                description=card.description,
                # Default boards share FIXED column ids, so the source column
                # id resolves 1:1 on the destination default board.
                column=card.column_id,
                priority=card.priority,
                labels=list(card.labels),
                assignee=card.assignee,
                checklist=[dict(entry) for entry in card.checklist],
                created_by=card.created_by,
            )
            copied["board_cards"] += 1
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                {
                    "code": "board_card_copy_failed",
                    "card_id": card.card_id,
                    "message": str(exc),
                }
            )
