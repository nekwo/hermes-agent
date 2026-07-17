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

from .models import OfficeActor, OfficeSurface
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
