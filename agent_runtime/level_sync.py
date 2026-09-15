"""A workspace's LEVEL as a realm-syncable family — the fifth one.

**The ruling this implements** (owner, 2026-09-10, launcher R15): a Mission
Control workspace can have its own level, and *"realm sync sounds good, deploy"*
is the transport for setting a REALM's default workspace's level from the
launcher's map picker. The launcher's own note
(``EterniaLauncher/docs/spatial/planned/one-engine-one-catalogue-levels-per-workspace.md``,
"R15's office half LANDED … and its realm half did NOT") measured why the
launcher could not do this alone and named this change: the realm store's file
kinds are hermes's, an unknown path is DROPPED on pull
(``realm_sync._destination_for_sync_path`` → ``None``), and
``publish_realm_sync`` ``rmtree``s the realm subtree and rebuilds it from the
artifact list — so a file the launcher planted is deleted at the next publish.

**hermes does not read this document, and that is the contract.** The bytes are
the launcher's ``SceneSerializer`` output VERBATIM. This module validates
exactly two facts — it decodes as UTF-8 JSON, and the object carries a
``version`` — and stores what it was handed byte for byte. It does not
reformat, re-indent, re-key or renumber anything, because the day the backend's
level routes land (contract §6.2) the transport is supposed to be swappable
without a format change, and a hermes that had normalised the bytes would have
made itself a second author of a document it does not own.

**What "one document" buys.** Unlike the office (per-actor 3-way merge), a level
is ONE document, so it merges at whole-document granularity exactly like
``flow_graph_sync``: adopt when the member has nothing or an untouched copy,
converge when identical, KEEP LOCAL when only this machine edited, HOLD when
both diverged. Same shared classifier (:func:`sync_merge.classify_three_way_pull`),
same never-synced baseline sidecar, same loud conflict — no clock, no
last-writer-wins.

**Identity is the JSON VALUE, not the formatting.** The content hash parses and
re-serialises canonically before hashing, so an indentation or key-order
difference between two publishers converges instead of conflicting, while the
STORED bytes stay the ones their author wrote. That is the one place this module
looks at the document's shape, and it still needs no knowledge of the schema.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths

#: Published subtree prefix for this family. Unknown to every older hermes:
#: ``_destination_for_sync_path`` answers ``None`` for it through the final
#: fallthrough, so an older member SKIPS the artifact rather than writing it
#: somewhere wrong. Degrading to "no level replication" is the whole
#: version-skew story, and it is the door the canvas projection walked through.
LEVEL_PUBLISHED_PREFIX = "store/levels/"

#: The launcher's own store bound (``LocalSceneStore.maxPayloadBytes``),
#: mirrored rather than exceeded. A document larger than this could not be
#: written by the store on the receiving end, so accepting it here would publish
#: something every member is guaranteed to refuse — and it would do it into a
#: git repo, where the cost is permanent. Refused loudly at both doors instead.
MAX_LEVEL_DOCUMENT_BYTES = 1024 * 1024

#: The bytes are not UTF-8, or not JSON at all.
REFUSAL_UNREADABLE_DOCUMENT = "unreadable_document"
#: Valid JSON, but not an object — so it cannot carry a ``version`` and is not a
#: level document whatever else it may be.
REFUSAL_NOT_AN_OBJECT = "not_an_object"
#: No numeric ``version``. The one schema fact hermes reads, and it reads it
#: because every store on the other end gates on it: a document with no version
#: is one the launcher's own loader answers ``SceneLoadMalformed`` for.
REFUSAL_MISSING_VERSION = "missing_version"
#: Over :data:`MAX_LEVEL_DOCUMENT_BYTES`.
REFUSAL_TOO_LARGE = "document_too_large"
#: The LOCAL stored level exists and will not read. Refused rather than reported
#: absent: absent drives the ADOPT arm, and adopting over a file this machine
#: could not read would overwrite an environment nobody has seen.
REFUSAL_UNREADABLE_LOCAL = "unreadable_local_level"
#: A published level file that will not read on arrival.
REFUSAL_UNREADABLE_REMOTE = "unreadable_remote_level"

_REFUSAL_MESSAGES = {
    REFUSAL_UNREADABLE_DOCUMENT: "level document is not readable UTF-8 JSON",
    REFUSAL_NOT_AN_OBJECT: "level document is not a JSON object",
    REFUSAL_MISSING_VERSION: "level document carries no numeric version",
    REFUSAL_TOO_LARGE: "level document is larger than the store accepts",
    REFUSAL_UNREADABLE_LOCAL: "stored level is not a readable document",
    REFUSAL_UNREADABLE_REMOTE: "published level is not a readable document",
}


def _refusal(key: str, code: str, *, message: str | None = None) -> dict[str, str]:
    return {"key": key, "code": code, "message": message or _REFUSAL_MESSAGES.get(code, code)}


class LevelDocumentError(ValueError):
    """A level document this runtime refuses to store or publish.

    Carries the typed ``code`` rather than only a sentence, because every caller
    — the CLI's exit taxonomy, the publish scan's refusal list, the pull's
    per-entity isolation — spends the code and none of them should be re-deriving
    it from a message.
    """

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or _REFUSAL_MESSAGES.get(code, code))
        self.code = code


# --- the two facts hermes reads -------------------------------------------


def validate_level_document(raw: bytes) -> dict[str, Any]:
    """Refuse anything that is not a size-bounded JSON object with a ``version``.

    Returns the parsed object for the CALLER's accounting (the CLI prints the
    version it accepted). **It is never what gets stored** — every writer in this
    module writes ``raw``, because the whole contract is that the launcher's
    bytes survive the round trip unchanged.
    """

    if len(raw) > MAX_LEVEL_DOCUMENT_BYTES:
        raise LevelDocumentError(REFUSAL_TOO_LARGE)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LevelDocumentError(REFUSAL_UNREADABLE_DOCUMENT) from exc
    if not isinstance(parsed, dict):
        raise LevelDocumentError(REFUSAL_NOT_AN_OBJECT)
    if not isinstance(parsed.get("version"), (int, float)) or isinstance(
        parsed.get("version"), bool
    ):
        raise LevelDocumentError(REFUSAL_MISSING_VERSION)
    return parsed


def level_document_hash(raw: bytes) -> str:
    """Semantic content hash of one level document.

    Parses and re-serialises canonically (sorted keys, no whitespace) so the
    merge keys on the JSON VALUE. Two publishers whose serializers differ only
    in indentation converge; a real edit diverges. The stored bytes are
    untouched by this — nothing here is ever written back.

    Assumes :func:`validate_level_document` has already accepted ``raw``; a
    caller that has not validated gets the same ``LevelDocumentError``.
    """

    parsed = validate_level_document(raw)
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def level_baseline_key(workspace_token: str) -> str:
    """This family's baseline key, namespaced like every sibling family's."""

    return f"level:{workspace_token}"


def published_relative_path(workspace_id: str) -> str:
    """Where one workspace's level is published inside a realm subtree.

    ONE spelling, and both the publish scan and the pull walk read it from here:
    the office family's ``store/office/<token>/…`` is assembled at three sites
    and they have to agree by inspection, which is the drift this avoids.
    """

    return f"{LEVEL_PUBLISHED_PREFIX}{paths.safe_path_token(workspace_id)}.json"


def workspace_token_for_published_path(rel: str) -> str | None:
    """The workspace token a published path names, or ``None`` if it is not one.

    The exact inverse of :func:`published_relative_path` over the tokens that
    function can produce — a nested path under the prefix is NOT one of them and
    answers ``None`` rather than a guess.
    """

    text = str(rel).replace("\\", "/")
    if not text.startswith(LEVEL_PUBLISHED_PREFIX) or not text.endswith(".json"):
        return None
    tail = text[len(LEVEL_PUBLISHED_PREFIX) : -len(".json")]
    if not tail or "/" in tail:
        return None
    return tail


# --- the store door ---------------------------------------------------------
#
# Below the line on purpose: everything above is pure, so the validation, the
# hash and the path grammar stay unit-testable without a store. Same two-halves
# shape as ``flow_graph_sync`` and ``persona_instance_sync``, for the same
# reason — one module per synced family beats a pure module and a sidecar module
# that can drift apart.


class LevelStore:
    """Read and write one workspace's level document.

    THE door. Both the CLI verbs and the realm-sync pull applier write through
    it, so "hermes accepted this level" means one thing on this machine. A raw
    file write beside it would be a second author with a different idea of what
    a level document has to satisfy — which is the shape ``apply_persona_
    instance_pull`` names as its reason for existing too.
    """

    def read(self, workspace_id: str) -> bytes | None:
        """The stored bytes, or ``None`` when this workspace has no level.

        Absence is not an error: most workspaces never had a level applied, and
        an unauthored environment is the normal case.
        """

        path = paths.level_path(workspace_id)
        try:
            return path.read_bytes() if path.is_file() else None
        except OSError:
            return None

    def write(self, workspace_id: str, raw: bytes) -> dict[str, Any]:
        """Store ``raw`` VERBATIM after validating it, and report what happened.

        ``newline=""`` on the write is load-bearing rather than tidy: the default
        translates ``\\n`` to the host's line ending, which on Windows would make
        hermes rewrite every line of a document it promised not to touch — and
        the publish lane's own EOL canonicalisation would then report a change on
        a no-op set.
        """

        from utils import atomic_write_text

        validate_level_document(raw)
        path = paths.level_path(workspace_id)
        before = self.read(workspace_id)
        if before == raw:
            return {"path": path, "changed": False}
        atomic_write_text(path, raw.decode("utf-8"), newline="")
        return {"path": path, "changed": True}

    def list_workspace_tokens(self) -> list[str]:
        """Every workspace token this store holds a level for, sorted."""

        root = paths.levels_root()
        if not root.is_dir():
            return []
        return sorted(path.stem for path in root.glob("*.json") if path.is_file())


# --- baseline sidecar (never synced, never published) ------------------------


def read_level_baseline(realm_id: str) -> dict[str, str]:
    path = paths.level_baseline_path(realm_id)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return {str(k): str(v) for k, v in entries.items()} if isinstance(entries, dict) else {}


def write_level_baseline(realm_id: str, entries: dict[str, str]) -> None:
    from utils import atomic_json_write

    atomic_json_write(
        paths.level_baseline_path(realm_id),
        {"schema_version": 1, "entries": entries},
        indent=2,
        sort_keys=True,
    )


def update_level_baseline_after_publish(realm_id: str, hashes: dict[str, str]) -> None:
    """Record the published levels' hashes as the new baseline.

    Without it a member who publishes and then pulls reads the level they just
    shipped as locally-edited-and-remotely-changed — and for this family, as for
    the canvas, the answer to a two-sided divergence is a HOLD with a conflict
    sidecar. The publisher would be handed a held environment over content
    nobody disagreed about.
    """

    baseline = read_level_baseline(realm_id)
    for token, body_hash in hashes.items():
        baseline[level_baseline_key(token)] = body_hash
    write_level_baseline(realm_id, baseline)


# --- pull: adopt, keep or HOLD — whole document, never merged ----------------


def read_remote_levels(subtree) -> tuple[dict[str, bytes], list[dict[str, str]]]:
    """Level bytes carried by a pulled realm subtree, keyed by workspace token.

    ``(levels, refused)``. A file under the prefix that will not read is REFUSED
    by name rather than omitted, because an omission is indistinguishable from
    "the realm stopped publishing this level" — which drives the
    ``upstream_absent`` arm, and answering a read failure with a removal-shaped
    decision is the mistake this family cannot afford to make about somebody's
    authored environment.
    """

    root = Path(subtree).joinpath(*LEVEL_PUBLISHED_PREFIX.strip("/").split("/"))
    levels: dict[str, bytes] = {}
    refused: list[dict[str, str]] = []
    if not root.is_dir():
        return levels, refused
    for path in sorted(root.glob("*.json")):
        if not path.is_file():
            continue
        token = path.stem
        try:
            raw = path.read_bytes()
            validate_level_document(raw)
        except (OSError, LevelDocumentError) as exc:
            code = getattr(exc, "code", REFUSAL_UNREADABLE_REMOTE)
            refused.append(_refusal(token, REFUSAL_UNREADABLE_REMOTE, message=_REFUSAL_MESSAGES.get(code, code)))
            continue
        levels[token] = raw
    return levels, refused


@dataclass(slots=True)
class LevelPullSummary:
    """Typed accounting for the level pull — the contract seam the launcher
    renders, carried on a pull ack as ``result["level_sync"]``.

    - ``adopted`` — the realm's level was written whole, through the store door
      so it was validated on the way in.
    - ``converged`` — local already equals remote; nothing written.
    - ``kept_local`` — this machine changed its level and the realm did not.
    - ``held`` — BOTH sides changed it. The local level is untouched and the
      remote bytes are parked in a conflict sidecar. One document has no natural
      three-way resolution, so this is a loud hold, never a merge.
    - ``upstream_absent`` — the realm no longer carries a level this baseline
      says it published. **Never a delete.** A level is authored work and this
      family has no archive; the baseline is KEPT so a repaired publish still
      converges.
    - ``refused`` — a published level that will not read, or a local one that
      will not. Per-entity isolation: nothing is written, the refusal is named,
      the pull continues.

    ``source`` is ``None`` when the subtree carries no level directory at all —
    an older publisher, or a realm whose workspaces never had one applied.
    Absence is never a removal, and the launcher's version-skew rule keys on the
    distinction, which is why the key is emitted unconditionally.
    """

    adopted: list[str] = field(default_factory=list)
    converged: list[str] = field(default_factory=list)
    kept_local: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    upstream_absent: list[str] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    source: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.adopted)

    def as_dict(self) -> dict[str, Any]:
        return {
            "adopted": sorted(set(self.adopted)),
            "converged": sorted(set(self.converged)),
            "kept_local": sorted(set(self.kept_local)),
            "held": sorted(set(self.held)),
            "upstream_absent": sorted(set(self.upstream_absent)),
            "refused": list(self.refused),
            "source": self.source,
        }


def apply_level_pull(realm_id: str, subtree) -> LevelPullSummary:
    """Adopt, keep or HOLD each pulled level — whole document, never merged.

    Runs inside ``pull_realm_sync`` AFTER the generic overwrite loop, which is
    what materialises ``store/workspaces/*``: a level is addressed BY a
    workspace, so landing one before its workspace record exists would leave an
    environment for a workspace nothing on this machine can name. It does NOT
    gate on the workspace existing, though — the generic loop already ran, and
    refusing a level because a workspace record failed to arrive would discard
    the operator's environment over somebody else's failure.

    **Order against the office is deliberate and is a no-op by design.** R7 says
    a level is the ENVIRONMENT and the office's actors are the CONTENTS placed on
    it, stored separately and never merged; neither applier reads the other's
    files, so this runs beside the office rather than inside its ordering
    argument.
    """

    from .sync_merge import PullAction, classify_three_way_pull

    summary = LevelPullSummary()
    root = Path(subtree).joinpath(*LEVEL_PUBLISHED_PREFIX.strip("/").split("/"))
    if not root.is_dir():
        # Not published. Never a removal — no baselined level is touched, so an
        # older peer in the rotation cannot strand this machine's environments.
        return summary
    summary.source = "subtree"
    remote, refused = read_remote_levels(subtree)
    summary.refused.extend(refused)

    store = LevelStore()
    baseline = read_level_baseline(realm_id)
    prefix = level_baseline_key("")
    baselined = {key[len(prefix) :] for key in baseline if key.startswith(prefix)}
    for token in sorted(set(remote) | baselined):
        remote_raw = remote.get(token)
        # A token that refused above is neither adopted nor treated as absent:
        # it is already named on ``refused`` and must not fall into the
        # upstream_absent arm, which is delete-SHAPED reporting about a file that
        # is right there and merely would not open.
        if remote_raw is None and any(row["key"] == token for row in refused):
            continue
        try:
            local_raw = store.read(token)
            local_hash = level_document_hash(local_raw) if local_raw is not None else None
        except LevelDocumentError:
            summary.refused.append(_refusal(token, REFUSAL_UNREADABLE_LOCAL))
            continue
        if remote_raw is None:
            summary.upstream_absent.append(token)
            continue
        remote_hash = level_document_hash(remote_raw)
        decision = classify_three_way_pull(
            local_hash, remote_hash, baseline.get(level_baseline_key(token))
        )
        if decision.action is PullAction.NOOP:
            summary.converged.append(token)
            continue
        if decision.action is PullAction.KEEP_LOCAL:
            summary.kept_local.append(token)
            continue
        if decision.action is PullAction.CONFLICT:
            summary.held.append(token)
            _write_conflict_sidecar(realm_id, token, remote_raw, local_hash, remote_hash)
            continue
        if decision.action is PullAction.ARCHIVE_LOCAL:
            # Unreachable with ``remote_raw`` in hand — the classifier only
            # answers ARCHIVE_LOCAL for an absent remote, which the arm above
            # already took. Named rather than folded into the adopt arm so a
            # future classifier change cannot silently start overwriting a level
            # on a removal decision.
            summary.upstream_absent.append(token)
            continue
        store.write(token, remote_raw)
        baseline[level_baseline_key(token)] = remote_hash
        summary.adopted.append(token)
    if summary.adopted:
        write_level_baseline(realm_id, baseline)
    return summary


def _write_conflict_sidecar(
    realm_id: str,
    workspace_token: str,
    remote_raw: bytes,
    local_hash: str | None,
    remote_hash: str | None,
) -> None:
    """Park the environment a HOLD refused to adopt.

    Best-effort: a sidecar this machine cannot write is not a reason to clobber
    the level the hold exists to protect. The remote bytes are carried as TEXT
    rather than re-parsed JSON so the parked copy is the one the publisher wrote
    — a resolve that took it would otherwise adopt hermes's re-serialisation of
    somebody else's document.
    """

    from utils import atomic_json_write

    try:
        atomic_json_write(
            paths.level_conflict_path(realm_id, workspace_token),
            {
                "schema_version": 1,
                "realm_id": realm_id,
                "workspace_token": workspace_token,
                "local_hash": local_hash,
                "remote_hash": remote_hash,
                "remote_document": remote_raw.decode("utf-8", errors="replace"),
            },
            indent=2,
            sort_keys=True,
        )
    except Exception:  # noqa: BLE001 — the HOLD stands with or without its receipt
        pass
