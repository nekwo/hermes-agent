from __future__ import annotations

from pathlib import Path

from .resolution import assert_probe_isolation, resolve_runtime


def store_root() -> Path:
    resolution = resolve_runtime()
    # Probe-isolation gate: a no-op unless HERMES_REQUIRE_ISOLATED_ROOT is set, in which
    # case a run that would resolve the live/default root fails fast before any store I/O.
    assert_probe_isolation(resolution)
    return resolution.store_root


def runs_dir() -> Path:
    return store_root() / "runs"


def persona_instances_dir() -> Path:
    return store_root() / "persona_instances"


def persona_instances_archive_dir() -> Path:
    return store_root() / "persona_instances_archive"


def persona_instance_aliases_path() -> Path:
    """Durable legacy-id -> canonical-id registry written by the reconciler.

    Emitted as ``identity_map`` on the snapshot/stream so consumers can
    collapse rows persisted under retired id schemes without heuristics.
    """
    return store_root() / "persona_instance_aliases.json"


def persona_assignments_dir() -> Path:
    return store_root() / "persona_assignments"


def persona_assignments_archive_dir() -> Path:
    return store_root() / "persona_assignments_archive"


def persona_chat_mint_receipts_dir() -> Path:
    """Durable idempotency receipts for server-minted persona chat roots."""
    return store_root() / "persona_chat_mint_receipts"


def persona_chat_mint_receipt_path(key_digest: str) -> Path:
    return persona_chat_mint_receipts_dir() / f"{safe_path_token(key_digest)}.json"


def agent_create_reservations_dir() -> Path:
    """Recorded-progress receipts for ``runtime.agent.create``.

    A sibling of ``persona_chat_mint_receipts`` rather than a directory inside
    it: the two ledgers answer different questions (one write vs. a two-store
    sequence) and share no reader, and nesting them would make a sweep of one
    silently sweep the other.
    """
    return store_root() / "agent_create_reservations"


def agent_create_reservation_path(key_digest: str) -> Path:
    return agent_create_reservations_dir() / f"{safe_path_token(key_digest)}.json"


def chat_turn_reservations_dir() -> Path:
    """ACCEPT receipts for ``runtime.chat.message`` / ``runtime.chat.steer``.

    A sibling of ``agent_create_reservations`` for its reason and NOT for the
    obvious one: these receipts do not record a turn's progress. The mission
    chat turn journal already owns that, keyed on the same ``client_message_id``
    these receipts key on (see ``chat_turn_reservations`` module docstring), and
    a second progress ledger over one fact is the duplicate-authority shape this
    lane keeps retiring. What lives here is strictly the SPAWN decision — "this
    runtime already handed this id to a worker" — which the journal provably
    cannot answer because the journal's first write happens inside the chat-root
    lease, after the worker is already running.
    """
    return store_root() / "chat_turn_reservations"


def chat_turn_reservation_path(key_digest: str) -> Path:
    return chat_turn_reservations_dir() / f"{safe_path_token(key_digest)}.json"


def runtime_instances_dir() -> Path:
    return store_root() / "runtime_instances"


def workspaces_dir() -> Path:
    return store_root() / "workspaces"


def realms_dir() -> Path:
    return store_root() / "realms"


def boards_root() -> Path:
    return store_root() / "boards"


def board_dir(board_id: str) -> Path:
    return boards_root() / safe_path_token(board_id)


def board_def_path(board_id: str) -> Path:
    return board_dir(board_id) / "board.json"


def board_cards_dir(board_id: str) -> Path:
    return board_dir(board_id) / "cards"


def board_card_path(board_id: str, card_id: str) -> Path:
    return board_cards_dir(board_id) / f"{safe_path_token(card_id)}.json"


def board_archive_dir(board_id: str) -> Path:
    # archive-never-delete; NOT published to realms
    return board_dir(board_id) / "archive"


def board_archived_card_path(board_id: str, card_id: str) -> Path:
    return board_archive_dir(board_id) / f"{safe_path_token(card_id)}.json"


def board_conflicts_dir(board_id: str) -> Path:
    # per-card sync conflict sidecars; NOT published to realms
    return board_dir(board_id) / "conflicts"


def board_conflict_path(board_id: str, card_id: str) -> Path:
    return board_conflicts_dir(board_id) / f"{safe_path_token(card_id)}.json"


def board_idempotency_dir(board_id: str) -> Path:
    return board_dir(board_id) / "idempotency"


def board_idempotency_path(board_id: str, key: str) -> Path:
    return board_idempotency_dir(board_id) / f"{safe_path_token(key)}.json"


#: The realm-sync tree's top-level directory name, under the store root.
#:
#: Promoted out of the four baseline helpers' bodies (and
#: ``realm_sync._sync_repo_path``'s) for the reason
#: :data:`DELETED_ARCHIVE_DIRNAME` and :data:`OFFICE_ARCHIVE_DIRNAME` were, and
#: under the same rule: the read-model cache's fingerprint now keys a NESTED
#: exclusion on this name (``core_cache._EXCLUDED_NESTED_STORE_NAMES`` — skip an
#: entry literally called ``.git`` anywhere below it), so a second module's
#: correctness depends on agreeing with this one, and a name in that position is
#: IMPORTED there rather than re-typed.
#:
#: **TWO DIFFERENT KEYS LIVE UNDER IT, and confusing them inverts the argument.**
#: ``realm_sync._sync_repo_path`` keys a synced WORKTREE by the realm's SERVER
#: token; the four baseline helpers below key their sidecars by the REALM ID.
#: Both land as children of this one directory. That is why the cache's nested
#: exclusion keys on the literal child name ``.git`` and never on a realm or
#: server token: a token-keyed skip would be wrong in both directions, and
#: ``board_baseline.json`` / ``office_baseline.json`` ARE projection inputs
#: (``snapshot.py`` reads both) that must stay inside the fingerprint.
REALM_SYNC_DIRNAME = "realm_sync"


def realm_sync_root() -> Path:
    """The store root's realm-sync tree — worktrees and baseline sidecars alike."""

    return store_root() / REALM_SYNC_DIRNAME


def board_baseline_path(realm_id: str) -> Path:
    # realm-sync baseline sidecar; NEVER synced, NEVER published
    return realm_sync_root() / safe_path_token(realm_id) / "board_baseline.json"


def office_root() -> Path:
    return store_root() / "office"


def office_dir(workspace_id: str) -> Path:
    return office_root() / safe_path_token(workspace_id)


def office_surface_path(workspace_id: str) -> Path:
    return office_dir(workspace_id) / "office.json"


def office_actors_dir(workspace_id: str) -> Path:
    return office_dir(workspace_id) / "actors"


def office_actor_path(workspace_id: str, actor_key: str) -> Path:
    from .office_models import actor_file_token  # filename authority (plan §4.3)

    return office_actors_dir(workspace_id) / f"{actor_file_token(actor_key)}.json"


def office_archive_dir(workspace_id: str) -> Path:
    # archive-never-delete; NOT published to realms
    return office_dir(workspace_id) / "archive"


def office_archived_actor_path(workspace_id: str, actor_key: str) -> Path:
    from .office_models import actor_file_token

    return office_archive_dir(workspace_id) / f"{actor_file_token(actor_key)}.json"


def office_conflicts_dir(workspace_id: str) -> Path:
    # per-actor sync conflict sidecars; NOT published to realms
    return office_dir(workspace_id) / "conflicts"


def office_conflict_path(workspace_id: str, actor_key: str) -> Path:
    from .office_models import actor_file_token

    return office_conflicts_dir(workspace_id) / f"{actor_file_token(actor_key)}.json"


#: The ORPHANED-SURFACE graveyard's top-level directory name.
#:
#: Promoted out of :func:`office_surface_archive_root`'s body for exactly the
#: reason :data:`DELETED_ARCHIVE_DIRNAME` was, and under the same rule: the
#: read-model cache's fingerprint denylist
#: (``core_cache._EXCLUDED_STORE_ENTRIES``) excludes this tree from the store-root
#: walk, so a second module's correctness now depends on agreeing with this one,
#: and a name in that position is IMPORTED there rather than re-typed.
#:
#: **Do not confuse it with :func:`office_archive_dir`**, whose name is one
#: underscore away and whose tree is a different thing entirely: that one is
#: ``office/<ws>/archive/``, per-workspace, holding archived ACTOR placements,
#: and it is READ by ``OfficeStore`` (``read_actor_dir`` on the actor-listing
#: seam, ``office_archived_actor_path`` on the archived-actor lookups). It is a
#: projection INPUT and stays inside the fingerprint. This constant names only the
#: store-root sibling below.
OFFICE_ARCHIVE_DIRNAME = "office_archive"


def office_surface_archive_root() -> Path:
    """Archive-never-delete for a whole ORPHANED office surface.

    A SIBLING of :func:`office_root`, not a child, and that is load-bearing:
    ``OfficeStore.list_workspaces()`` enumerates ``office_root()``'s children by
    the presence of ``office.json``, so an archive nested under it would still be
    projected and still raise its ``orphaned_office`` parity warning — i.e.
    archiving would clear nothing. NOT published to realms.
    """

    return store_root() / OFFICE_ARCHIVE_DIRNAME


def office_archived_surface_dir(workspace_id: str) -> Path:
    return office_surface_archive_root() / safe_path_token(workspace_id)


def office_baseline_path(realm_id: str) -> Path:
    # realm-sync baseline sidecar; NEVER synced, NEVER published
    return realm_sync_root() / safe_path_token(realm_id) / "office_baseline.json"


def persona_config_baseline_path(realm_id: str) -> Path:
    # realm-sync baseline sidecar; NEVER synced, NEVER published
    return realm_sync_root() / safe_path_token(realm_id) / "persona_config_baseline.json"


def persona_instance_baseline_path(realm_id: str) -> Path:
    # realm-sync baseline sidecar for the replicated persona-INSTANCE family;
    # NEVER synced, NEVER published. Keyed off the REMOTE hash at mint time so a
    # fresh replica reads ZERO drift immediately — without that, the very next
    # `realm sync status` reports the replica as an unpublished local addition
    # and the revert lane offers to archive correct state.
    return realm_sync_root() / safe_path_token(realm_id) / "persona_instance_baseline.json"


def profile_artifact_baseline_path(realm_id: str) -> Path:
    # realm-sync baseline sidecar for the per-profile FILE family (MEMORY.md,
    # core-context files, persona prompts); NEVER synced, NEVER published.
    return realm_sync_root() / safe_path_token(realm_id) / "profile_artifact_baseline.json"


def agents_dir() -> Path:
    return store_root() / "agents"


def incidents_dir() -> Path:
    return store_root() / "incidents"


# S54 removed ``stagec_artifacts_dir``. S43 KEPT it (its ``_task_dir`` leaf went
# then); by S54 the directory helper itself had no production reader. See the
# inverted S43/S23 pins.

def events_path() -> Path:
    """The pristine/base live event-log file.

    Semantics (C6a event-log rotation): before any rotation this IS the live
    file the log appends to; after the first rotation it becomes the sealed
    base-offset (``[0, …)``) slice while appends move to a fresh live file under
    :func:`events_archive_dir`. The current live file and the ordered slice set
    are resolved through ``agent_runtime.event_rotation`` (manifest-backed), so
    prefer those helpers over reading this path directly when you need the whole
    log. This path stays the manifest's canonical base-0 slice name.
    """
    return store_root() / "events.jsonl"


def events_manifest_path() -> Path:
    """Slice manifest for the rotated event log (C6a).

    Absent until the first rotation — its absence is the "pristine, single live
    file" state, in which ``events_path()`` is the whole log at logical offset 0.
    """
    return store_root() / "events_manifest.json"


def events_archive_dir() -> Path:
    """Rotated event-log slices (C6a): archive-never-delete, offset-load-bearing.

    Distinct from :func:`deleted_archive_dir` (per-task compaction batches).
    """
    return store_root() / "events_archive"


def lock_dir() -> Path:
    return store_root() / "locks"


def snapshot_path() -> Path:
    return store_root() / "snapshot.json"


#: The per-task compaction graveyard's top-level directory name.
#:
#: Promoted out of :func:`deleted_archive_dir`'s body because another module has
#: to NAME it: the read-model cache's fingerprint denylist
#: (``core_cache._EXCLUDED_STORE_ENTRIES``) excludes this tree from the store-root
#: walk, and that set's governing rule is that a name its owner spells as a
#: constant is IMPORTED there, never re-typed. That rule is not style: the set
#: shipped with a hand-typed ``"drain_state.json"`` against a writer that said
#: ``dispatch_delivery_drain.json``, so the exclusion named a file that had never
#: existed while the real one sat inside the key.
#:
#: It was the FIRST of this module's inline names to be promoted and was for one
#: stage the only one; :data:`OFFICE_ARCHIVE_DIRNAME` followed it out for the same
#: reason and by this same rule, so the criterion below is what to read, not the
#: count.
#:
#: The siblings here (``locks``, ``snapshot.json``, …) stay inline deliberately:
#: promoting a constant is worth it when a second module's correctness depends on
#: agreeing with this one, and is otherwise noise. Anything that gains a
#: cross-module reader should follow this one out.
DELETED_ARCHIVE_DIRNAME = "deleted_archive"


def deleted_archive_dir() -> Path:
    return store_root() / DELETED_ARCHIVE_DIRNAME


def prompt_observability_dir() -> Path:
    return store_root() / "prompt_observability"


def prompt_observability_catalogs_dir() -> Path:
    """C1 content-addressed skills-catalog store: one ``<hash>.json`` per
    distinct skill list, written iff absent at persist time (a content hash is
    immutable). Persisted ctx rows carry ``*_ref`` hashes into this store."""
    return store_root() / "prompt_observability_catalogs"


def prompt_observability_archive_dir() -> Path:
    """C2 retention target: rows evicted from the live observability dir MOVE
    here (archive-never-delete); ``harness prompt-context show`` still resolves
    them."""
    return store_root() / "prompt_observability_archive"


def prompt_observability_index_path() -> Path:
    """C2 latest-pointer index: (instance, session) -> newest context ids.
    A CACHE, never authority — the persist chokepoint is its only writer; a
    missing/corrupt index falls back to the directory scan."""
    return store_root() / "prompt_observability_index.json"


def patch_diffs_dir() -> Path:
    """The unified diff a patch tool call produced, one ``.diff`` file per call.

    Written at the agent_runtime trace boundary (never by the tools layer, which
    is upstream-shared and runs in contexts with no store). The TRACE carries
    only this file's PATH — the diff body never rides a frame, so the operator's
    viewer reads it locally at view time and nothing is shipped anywhere."""
    return store_root() / "patch_diffs"


def patch_diffs_archive_dir() -> Path:
    """Retention target for :func:`patch_diffs_dir`: diffs evicted from the live
    dir MOVE here, same archive-never-delete discipline as
    :func:`prompt_observability_archive_dir` and with the same unbounded archive
    side. A trace row whose diff was archived renders its viewer affordance
    disabled (the path no longer resolves) — honest by construction."""
    return store_root() / "patch_diffs_archive"


# S44 removed the eight role_envelope* / role_checklist* path helpers that lived
# here. They addressed the two store directories archived aside as writer-less on
# 2026-07-30; their last readers were the deleted stores and the two checkpoint
# EntityClass rows retired in the same commit. Same rule as S23's writer-less
# path sweep.


def persona_instance_path(persona_instance_id: str) -> Path:
    return persona_instances_dir() / f"{safe_path_token(persona_instance_id)}.json"


def persona_assignment_path(assignment_id: str) -> Path:
    return persona_assignments_dir() / f"{safe_path_token(assignment_id)}.json"


# S57 removed ``repo_bundle_path`` with ``RepoBundleStore.get``. Round 2 then
# removed the caller-less historical promotion-record reader and the two
# directory helpers that survived solely to address its retired tree.


def runtime_instance_path(instance_id: str) -> Path:
    return runtime_instances_dir() / f"{safe_path_token(instance_id)}.json"


def workspace_path(workspace_id: str) -> Path:
    return workspaces_dir() / f"{safe_path_token(workspace_id)}.json"


def realm_path(realm_id: str) -> Path:
    return realms_dir() / f"{safe_path_token(realm_id)}.json"


def active_workspace_path() -> Path:
    return store_root() / "active_workspace.json"


def active_realm_path() -> Path:
    return store_root() / "active_realm.json"


def run_path(run_id: str) -> Path:
    return runs_dir() / f"{run_id}.json"


def agent_path(persona_id: str) -> Path:
    return agents_dir() / f"{persona_id}.json"


def incident_path(incident_id: str) -> Path:
    return incidents_dir() / f"{incident_id}.json"


def safe_path_token(value: str | None) -> str:
    """Sanitize an id/token into ONE filesystem-safe path segment.

    Public because it is the single authority: realm_sync and
    skill_promotion each carried a byte-identical private copy, and a
    directory written by one and read by the other has to agree on the
    spelling. Idempotent for already-safe tokens.
    """

    text = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in str(value or "").strip())
    return text.strip("._")[:120] or "item"


def unlink_quietly(path: Path) -> None:
    """Best-effort delete. A file that is already gone is the goal state.

    ONE authority: chat_live_log and mission_chat_steer each carried a
    byte-identical private copy.
    """

    try:
        path.unlink()
    except OSError:
        pass


def safe_mtime(path: Path) -> float:
    """Modification time, or ``0.0`` for anything that cannot be stat'd.

    ONE authority: ``repo_context`` carried this as ``_safe_mtime`` and gateway
    Stage 8's media derivation needed the same "newest first, and a path that
    vanished mid-sort must not raise" ordering key. ``0.0`` sorts oldest, which
    is the honest answer for a file that is not there: whatever it was, it is
    not the newest.
    """

    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
