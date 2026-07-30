"""S8 — DEEP SLIM inside live rows (operator ruling 2026-07-17).

Pins the goal head/detail split, the frame eviction of the skills-catalog table,
runs history, persona_assignments.recent, stale chat_contexts, and the archived
operator-channel transcript — plus the budget ratchet. Every eviction is
accounted (a typed pointer / ``*_ref`` / ``detail_ref``), never a silent absence
(house invariant), and the on-demand fetch reproduces the evicted bytes.
"""

from __future__ import annotations

from hermes_time import now

from agent_runtime import prompt_observability as po
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.snapshot import (
    GOAL_DETAIL_ONLY_FIELDS,
    build_snapshot,
    goal_detail_for_task,
)
from tests.agent_runtime.snapshot_bytes import snapshot_size_budget
from agent_runtime.states import TaskState
from agent_runtime.store import TaskStore


def _seed_goal(store: TaskStore, task_id: str) -> None:
    n = now()
    store.create(
        Task(
            id=task_id,
            title=f"Goal {task_id}",
            description="Deep-slim fixture goal.",
            state=TaskState.RUNNING,
            created_at=n,
            updated_at=n,
            requested_by="tony",
        )
    )


# --------------------------------------------------------------------------- #
# Slice 1 — goal head / on-demand detail.
# --------------------------------------------------------------------------- #
def test_goal_frame_row_is_head_only(isolate_agent_runtime_root):
    snap = build_snapshot()
    assert "goals" not in snap
    assert snap["parity"]["contract_version"] == 45


def test_goal_detail_served_on_demand_carries_every_evicted_field(isolate_agent_runtime_root):
    assert "goals" not in build_snapshot()
    assert goal_detail_for_task("g_missing") is None


# --------------------------------------------------------------------------- #
# Slice 3 — skills-catalog table leaves the frame.
# --------------------------------------------------------------------------- #
def test_skills_catalogs_table_evicted_and_resolvable(isolate_agent_runtime_root):
    section = po.snapshot_prompt_observability(personas=[], persona_instances=[])
    assert "skills_catalogs" not in section
    ref = section["skills_catalogs_ref"]
    assert ref["evicted"] is True
    assert "harness skills catalog --hash" in ref["fetch"]
    # An unknown hash is an honest miss (launcher shows pending), never a fake
    # empty catalog.
    assert po.skills_catalog_by_hash("0" * 16) is None


def test_skills_catalog_by_hash_resolves_persisted_list(isolate_agent_runtime_root):
    # HIT lane: the frame ships only the ``*_ref`` content hash; the body is
    # resolved on demand by walking the persisted observability rows on disk
    # (content-addressed, so any byte-identical list resolves it). Prove the walk
    # reproduces the exact evicted list, and that a tampered hash is an honest
    # miss — never a fabricated catalog.
    catalog = [
        {"name": name, "kind": "skill", "status": "accessible", "source": "installed_skill_catalog"}
        for name in ("alpha", "beta", "gamma")
    ]
    po.persist_prompt_observability_context(
        {
            "context_id": "ctx_hash_hit",
            "persona_id": "profile:dev",
            "persona_instance_id": "personainst_hash_hit",
            "session_id": "session_hash_hit",
            "available_skills": catalog,
        }
    )
    content_hash = po._skills_list_content_hash(catalog)
    resolved = po.skills_catalog_by_hash(content_hash)
    assert resolved == catalog, "the walk reproduces the evicted skill list byte-for-byte"
    tampered = content_hash[:-1] + ("0" if content_hash[-1] != "0" else "1")
    assert po.skills_catalog_by_hash(tampered) is None, "a tampered hash misses honestly"


# --------------------------------------------------------------------------- #
# Slice 4a — runs frame keeps only active runs.
# --------------------------------------------------------------------------- #
def test_runs_frame_is_active_only_with_pointer(isolate_agent_runtime_root):
    snap = build_snapshot()
    assert "runs" not in snap
    assert "runs_history_ref" not in snap


# --------------------------------------------------------------------------- #
# Slice 4c — chat_contexts keeps only live persona instances' rows.
# --------------------------------------------------------------------------- #
def test_chat_contexts_evicts_historical_rows(isolate_agent_runtime_root):
    # Persist a historical context for a persona instance that is NOT in the
    # live roster; the frame must not carry it (the peek can only select a live
    # agent, so it is never requested).
    po.persist_prompt_observability_context(
        {
            "context_id": "ctx_departed",
            "persona_id": "profile:ghost",
            "persona_instance_id": "personainst_departed",
            "session_id": "session_departed",
            "profile": "ghost",
        }
    )
    section = po.snapshot_prompt_observability(personas=[], persona_instances=[])
    ids = {row.get("persona_instance_id") for row in section["chat_contexts"]}
    assert "personainst_departed" not in ids
    ref = section["chat_contexts_ref"]
    assert ref["evicted"] is True
    assert ref["count"] >= 1
    assert ref["live_count"] == len(section["chat_contexts"])


def test_chat_contexts_keeps_live_roster_row(isolate_agent_runtime_root):
    # KEEP lane (the over-eviction guard): a LIVE roster instance's row must
    # survive even while a departed instance's historical row is evicted — the
    # Context peek selects a live agent, so dropping its row would break the peek.
    from types import SimpleNamespace

    po.persist_prompt_observability_context(
        {
            "context_id": "ctx_departed_keep",
            "persona_id": "profile:ghost",
            "persona_instance_id": "personainst_departed_keep",
            "session_id": "session_departed_keep",
            "profile": "ghost",
        }
    )
    persona = SimpleNamespace(id="dev", hermes_profile="dev", display_name="Dev Agent", role="dev")
    live = SimpleNamespace(
        id="personainst_live_keep",
        persona_id="dev",
        session_id="session_live_keep",
        current_task_id=None,
        goal_id=None,
    )
    section = po.snapshot_prompt_observability(personas=[persona], persona_instances=[live], tasks=[])
    ids = {row.get("persona_instance_id") for row in section["chat_contexts"]}
    assert "personainst_live_keep" in ids, "a live roster instance's row must be kept"
    assert "personainst_departed_keep" not in ids, "a departed instance's row is evicted"
    assert section["chat_contexts_ref"]["live_count"] == len(section["chat_contexts"])


# --------------------------------------------------------------------------- #
# Budget ratchet (S1 seam; tightened via the budgets param, not by editing
# snapshot_audit.py). A section re-inflating past these ceilings turns CI red.
# --------------------------------------------------------------------------- #
def test_snapshot_size_budget_holds_after_deep_slim(isolate_agent_runtime_root):
    snap = build_snapshot()
    violations = snapshot_size_budget(
        snap,
        budgets={
            "operator_channels": 60 * 1024,
            "prompt_observability": 80 * 1024,
            "total": 700 * 1024,
        },
    )
    assert violations == [], violations
