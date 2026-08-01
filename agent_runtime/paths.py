from __future__ import annotations

from pathlib import Path

from .resolution import assert_probe_isolation, resolve_runtime


def store_root() -> Path:
    resolution = resolve_runtime()
    # Probe-isolation gate: a no-op unless HERMES_REQUIRE_ISOLATED_ROOT is set, in which
    # case a run that would resolve the live/default root fails fast before any store I/O.
    assert_probe_isolation(resolution)
    return resolution.store_root


def goals_dir() -> Path:
    return store_root() / "goals"


def runs_dir() -> Path:
    return store_root() / "runs"


def worker_sessions_dir() -> Path:
    return store_root() / "worker_sessions"


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
    return persona_chat_mint_receipts_dir() / f"{_safe_path_token(key_digest)}.json"


def repo_bundles_dir() -> Path:
    return store_root() / "repo_bundles"


def runtime_instances_dir() -> Path:
    return store_root() / "runtime_instances"


def workspaces_dir() -> Path:
    return store_root() / "workspaces"


def realms_dir() -> Path:
    return store_root() / "realms"


def boards_root() -> Path:
    return store_root() / "boards"


def board_dir(board_id: str) -> Path:
    return boards_root() / _safe_path_token(board_id)


def board_def_path(board_id: str) -> Path:
    return board_dir(board_id) / "board.json"


def board_cards_dir(board_id: str) -> Path:
    return board_dir(board_id) / "cards"


def board_card_path(board_id: str, card_id: str) -> Path:
    return board_cards_dir(board_id) / f"{_safe_path_token(card_id)}.json"


def board_archive_dir(board_id: str) -> Path:
    # archive-never-delete; NOT published to realms
    return board_dir(board_id) / "archive"


def board_archived_card_path(board_id: str, card_id: str) -> Path:
    return board_archive_dir(board_id) / f"{_safe_path_token(card_id)}.json"


def board_conflicts_dir(board_id: str) -> Path:
    # per-card sync conflict sidecars; NOT published to realms
    return board_dir(board_id) / "conflicts"


def board_conflict_path(board_id: str, card_id: str) -> Path:
    return board_conflicts_dir(board_id) / f"{_safe_path_token(card_id)}.json"


def board_idempotency_dir(board_id: str) -> Path:
    return board_dir(board_id) / "idempotency"


def board_idempotency_path(board_id: str, key: str) -> Path:
    return board_idempotency_dir(board_id) / f"{_safe_path_token(key)}.json"


def board_baseline_path(realm_id: str) -> Path:
    # realm-sync baseline sidecar; NEVER synced, NEVER published
    return store_root() / "realm_sync" / _safe_path_token(realm_id) / "board_baseline.json"


def office_root() -> Path:
    return store_root() / "office"


def office_dir(workspace_id: str) -> Path:
    return office_root() / _safe_path_token(workspace_id)


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


def office_baseline_path(realm_id: str) -> Path:
    # realm-sync baseline sidecar; NEVER synced, NEVER published
    return store_root() / "realm_sync" / _safe_path_token(realm_id) / "office_baseline.json"


def persona_config_baseline_path(realm_id: str) -> Path:
    # realm-sync baseline sidecar; NEVER synced, NEVER published
    return store_root() / "realm_sync" / _safe_path_token(realm_id) / "persona_config_baseline.json"


def profile_artifact_baseline_path(realm_id: str) -> Path:
    # realm-sync baseline sidecar for the per-profile FILE family (MEMORY.md,
    # core-context files, persona prompts); NEVER synced, NEVER published.
    return store_root() / "realm_sync" / _safe_path_token(realm_id) / "profile_artifact_baseline.json"


def repo_bundles_task_dir(task_id: str) -> Path:
    return repo_bundles_dir() / _safe_path_token(task_id)


def agents_dir() -> Path:
    return store_root() / "agents"


def incidents_dir() -> Path:
    return store_root() / "incidents"


def stagec_artifacts_dir() -> Path:
    """Stage C's own artifact drop — deliberately NOT under ``proofs/``.

    Stage C visual proof is KEEP; the ``proofs/`` store is REMOVE (mission-lane
    removal, S6/S9) and is already quarantined out of the live runtime root. Stage C
    side artifacts (marionette rebuild stdout/stderr logs) therefore land here, in a
    directory whose lifetime matches Stage C's rather than the retired proof store's.
    """

    return store_root() / "stagec_artifacts"


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


def deleted_archive_dir() -> Path:
    return store_root() / "deleted_archive"


def context_dir() -> Path:
    return store_root() / "context"


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


def proof_sandbox_root() -> Path:
    return store_root() / "proof_sandbox"


def self_tests_dir() -> Path:
    return store_root() / "self_tests"


def self_test_task_dir(task_id: str) -> Path:
    return self_tests_dir() / _safe_path_token(task_id)


def self_test_artifacts_dir(task_id: str) -> Path:
    return self_test_task_dir(task_id) / "artifacts"


def self_test_record_path(task_id: str, evidence_id: str) -> Path:
    return self_test_task_dir(task_id) / f"{_safe_path_token(evidence_id)}.json"




# S44 removed the eight role_envelope* / role_checklist* path helpers that lived
# here. They addressed the two store directories archived aside as writer-less on
# 2026-07-30; their last readers were the deleted stores and the two checkpoint
# EntityClass rows retired in the same commit. Same rule as S23's writer-less
# path sweep.


def packet_artifacts_dir() -> Path:
    return store_root() / "packet_artifacts"


def worker_context_dir(task_id: str, persona_id: str) -> Path:
    return context_dir() / _safe_path_token(task_id) / _safe_path_token(persona_id)


def task_path(task_id: str) -> Path:
    return goal_path(task_id)


def goal_path(goal_id: str) -> Path:
    return goals_dir() / f"{_safe_path_token(goal_id)}.json"


def worker_session_path(worker_session_id: str) -> Path:
    return worker_sessions_dir() / f"{worker_session_id}.json"


def persona_instance_path(persona_instance_id: str) -> Path:
    return persona_instances_dir() / f"{_safe_path_token(persona_instance_id)}.json"


def persona_assignment_path(assignment_id: str) -> Path:
    return persona_assignments_dir() / f"{_safe_path_token(assignment_id)}.json"


def repo_bundle_path(task_id: str, repo_bundle_id: str) -> Path:
    return repo_bundles_task_dir(task_id) / f"{_safe_path_token(repo_bundle_id)}.json"


def runtime_instance_path(instance_id: str) -> Path:
    return runtime_instances_dir() / f"{_safe_path_token(instance_id)}.json"


def workspace_path(workspace_id: str) -> Path:
    return workspaces_dir() / f"{_safe_path_token(workspace_id)}.json"


def realm_path(realm_id: str) -> Path:
    return realms_dir() / f"{_safe_path_token(realm_id)}.json"


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


def _safe_path_token(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in str(value or "").strip())
    return text.strip("._")[:120] or "item"
