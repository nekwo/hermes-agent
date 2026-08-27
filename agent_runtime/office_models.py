"""Pure helpers for the Mission Office domain: the deterministic default
surface factory, the semantic content hash used by realm-sync change
detection, and the small vocabulary constants. No I/O — kept import-light so
both the store and the realm-sync pull applier can depend on it.

Mirrors ``board_models`` 1:1 (the plan's lean directive: office machinery is
the board family's shape, never a new invention).
"""

from __future__ import annotations

import hashlib
import json

from .models import OfficeActor, OfficeItem, OfficeSurface
from .serde import to_jsonable

ITEM_KINDS = ("agent", "desk")

# Operator-authorable character scale — mirrors the launcher's
# kMissionOfficeAgentScale* constants; the store re-clamps defensively so a
# corrupt or hostile payload can never persist an invisible/screen-filling
# avatar.
SCALE_DEFAULT = 1.0
SCALE_MIN = 0.6
SCALE_MAX = 1.8

# The launcher's structural default folders. The deterministic default surface
# carries exactly these so two machines lazily creating the same surface
# converge (timestamp-excluded content hash) instead of conflicting.
DEFAULT_FOLDERS = ("Agents", "Desks")

# Fields excluded from the semantic content hash: revision + timestamps +
# updated_by. Timestamp-only differences are never sync conflicts, and the
# excluded revision lets a converged actor settle without a spurious diff.
_HASH_EXCLUDE = frozenset({"revision", "created_at", "updated_at", "updated_by"})


#: Why an office surface's workspace does not resolve. The discrimination lives
#: in a FIELD; the parity warning's ``code`` stays ``orphaned_office`` for every
#: one of them, because the code is what a census greps and what the launcher's
#: ``warningCodes`` reads, and splitting one condition into three tokens would
#: silently zero every existing count of it.
#:
#: ``UNKNOWN`` is not optional and not a fallback for laziness — see
#: :func:`classify_orphaned_office_workspace` for the two shapes that reach it.
ORPHANED_OFFICE_WORKSPACE_DELETED = "workspace_deleted"
ORPHANED_OFFICE_WORKSPACE_NEVER_RECORDED = "workspace_never_recorded"
ORPHANED_OFFICE_REASON_UNKNOWN = "unknown"


def classify_orphaned_office_workspace(
    workspace_id: str,
    *,
    deleted_ledgers,
    ledger_cap: int,
) -> str:
    """WHICH KIND of orphan an office surface is, decided from the realm ledgers.

    The warning this feeds was worded "which no longer resolves" for every
    orphan — a sentence that PRESUMES the workspace once did. The live field case
    that exposed it had never had a workspace record at all: 135 events for the
    id, one ``office.surface.created`` and 67 actor upserts, and zero
    ``workspace.created`` / ``deleted`` / ``archived``. An operator told "no
    longer resolves" goes looking for the deletion that removed it, and there was
    none.

    ``deleted_ledgers`` is every realm's ``deleted_workspace_ids`` — the bounded
    resurrection-guard ledger ``store.WorkspaceStore.delete`` appends to, capped
    at ``ledger_cap`` (``store.DELETED_WORKSPACE_LEDGER_CAP``) with the OLDEST
    entries falling off first. Passed as data rather than read here so this stays
    a pure function this module can own, and so the caller derives it from the
    realm list the projection has ALREADY read — never a second enumeration free
    to disagree with the one that decided ``orphaned`` in the first place.

    THE RULE, STATED BECAUSE A NEGATIVE HERE IS WEAKER THAN A POSITIVE:

    * a hit in any ledger is a TOMBSTONE — proof, and the answer is
      ``workspace_deleted``;
    * a MISS is not proof of the opposite, so it only becomes
      ``workspace_never_recorded`` when nothing about the ledgers makes the
      negative unprovable. Two things can:

      - **the cap.** A ledger AT ``ledger_cap`` has been evicting its oldest
        entries, so the id may have been recorded and fallen off. Answer
        ``unknown``: an id that dropped off a full ledger is not "never
        recorded", and must not claim to be.
      - **no ledgers at all.** With no realms on the store there is no ledger
        that could have recorded anything, so a miss carries no information
        whatever. That is ``unknown`` too — the C16 rule applied here: an arm
        that cannot compute its answer says so in its own words rather than
        borrowing the confident sentence next to it.

    NAMED RESIDUAL — the one shape a ledger answer cannot see, stated rather than
    left for the next reader to discover. ``WorkspaceStore.delete`` appends the
    tombstone under ``if realm is not None``, so a workspace deleted while bound
    to NO realm — or to a realm row that had already gone — is deleted with no
    ledger entry written anywhere, and would read here as
    ``workspace_never_recorded``. That window is narrow rather than theoretical
    for exactly one reason: the same cascade ``rmtree``s the office subtree, so a
    deleted workspace normally leaves no surface to warn about at all. An orphan
    that survives a delete needs that removal to have failed, which makes it the
    rarer half of an already-rare case. Widening this beyond the ledgers means
    reading the event log per orphan inside the build, which is a cost no warning
    in the parity section currently pays.
    """

    wsid = str(workspace_id or "").strip()
    ledgers = [list(ledger or []) for ledger in (deleted_ledgers or [])]
    if wsid:
        for ledger in ledgers:
            if any(str(entry or "").strip() == wsid for entry in ledger):
                return ORPHANED_OFFICE_WORKSPACE_DELETED
    if not ledgers:
        return ORPHANED_OFFICE_REASON_UNKNOWN
    if any(len(ledger) >= int(ledger_cap) for ledger in ledgers):
        return ORPHANED_OFFICE_REASON_UNKNOWN
    return ORPHANED_OFFICE_WORKSPACE_NEVER_RECORDED


def default_surface(workspace_id: str, *, created_at, updated_by: str = "operator") -> OfficeSurface:
    """Deterministic default surface for a workspace.

    Everything that feeds the semantic content hash (workspace, folders, empty
    ledger) is fixed; only the excluded timestamp/updated_by fields vary per
    machine, so ``office_content_hash`` is identical everywhere.
    """

    return OfficeSurface(
        workspace_id=workspace_id,
        folders=list(DEFAULT_FOLDERS),
        archived_actor_keys=[],
        revision=1,
        created_at=created_at,
        updated_at=created_at,
        updated_by=updated_by,
    )


def normalize_item_kind(value: str | None) -> str:
    text = str(value or "").strip().lower()
    return text if text in ITEM_KINDS else "agent"


def normalize_scale(value) -> float:
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return SCALE_DEFAULT
    if scale != scale or scale in (float("inf"), float("-inf")):  # NaN / inf
        return SCALE_DEFAULT
    return min(max(scale, SCALE_MIN), SCALE_MAX)


def office_content_hash(entity: OfficeSurface | OfficeActor) -> str:
    """Semantic content hash H(entity): a stable hash over every field EXCEPT
    revision + timestamps + updated_by. Drives realm-sync change detection so
    that timestamp-only diffs are never conflicts and the deterministic default
    surface converges."""

    payload = to_jsonable(entity)
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key not in _HASH_EXCLUDE}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def actor_file_token(actor_key: str) -> str:
    """Deterministic, collision-proof filename token for an actor key.

    Capped at 64 chars (Windows realm-clone depth budget — plan §4.1/§4.3);
    when truncation applies, a stable ``-h<sha1[:8]>`` of the FULL key is
    appended so two long keys sharing a 64-char prefix cannot collide. Minted
    hermes-side only — the launcher never computes sync filenames.
    """

    token = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in str(actor_key or "").strip())
    token = token.strip("._") or "actor"
    if len(token) <= 64:
        return token
    digest = hashlib.sha1(str(actor_key).encode("utf-8")).hexdigest()[:8]
    return f"{token[:64]}-h{digest}"


def office_item_wire_row(actor: OfficeActor, item: OfficeItem) -> dict:
    """ONE scene item in the shape every reader on the wire already decodes.

    THE shape ``runtime.office.get`` renders (``serve_rpc._office_projection``
    flattens its items through this function), and therefore the shape the
    create's ack carries inside ``result.actor`` (plan D11). Lives here, in the
    pure module, because ``agent_create`` cannot import ``serve_rpc`` — that
    import is inverted by design, and the comment above ``ERR_INVALID_PARAMS``
    in ``agent_create`` says so. A second copy of these ten keys is the
    silent-disagreement shape this repo has already paid for once, in the
    ``get``-vs-``subscribe`` split that ``_office_projection`` was extracted to
    end; one function is the answer, not one function plus a test that they
    match.

    ``persona_instance_id`` and ``revision`` are the ACTOR's, repeated onto each
    of its items because the wire shape is flat: an actor file is the binding
    unit, and ``revision`` is the token ``runtime.office.upsert``'s
    ``expect_revision`` is checked against — never the SURFACE's, which does not
    move when an actor moves.
    """

    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "persona_id": item.persona_id,
        "persona_instance_id": actor.persona_instance_id,
        "revision": actor.revision,
        "folder": item.folder,
        "position": [float(item.position[0]), float(item.position[1])],
        "scale": float(item.scale),
        "display_name": item.display_name,
        "pet_slug": item.pet_slug,
    }


def office_actor_wire_row(actor: OfficeActor) -> dict:
    """The actor as ``runtime.agent.create``'s ack carries it (plan D11).

    Actor-level identity plus its items in :func:`office_item_wire_row`'s shape,
    so a client that already decodes ``runtime.office.get`` needs no second
    decoder to adopt the row the server just wrote — which is the point of
    returning it at all: the launcher stops trusting its own predicted key,
    position and revision and adopts the server's.

    Deliberately NOT carried, matching ``_office_projection``'s own exclusions:
    ``updated_by`` / ``created_at`` / ``updated_at`` / ``backing_profile`` (none
    of it renderable, the first three provenance for a different surface) and
    ``state``, which a create can only ever answer ``"active"``.
    """

    return {
        "actor_key": actor.actor_key,
        "workspace_id": actor.workspace_id,
        "persona_id": actor.persona_id,
        "persona_instance_id": actor.persona_instance_id,
        "revision": actor.revision,
        "items": [office_item_wire_row(actor, item) for item in actor.items],
    }
