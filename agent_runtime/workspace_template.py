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
    for actor in store.list_actors(source_workspace_id):
        payload = {
            "persona_id": actor.persona_id,
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
