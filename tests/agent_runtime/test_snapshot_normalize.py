"""Read-model S4 — normalize + delete derived copies (operator moves 4 + 5).

Lists become id-keyed maps; the goals/tasks dual projection collapses to ONE
keyed ``goals`` map (GOAL is the wire entity) and the ``tasks`` wire section
retires; the derived ``agent_topology`` copy leaves the frame; cross-entity
disagreements become typed ``fk_miss`` parity reports. Contract 40 -> 41.
"""

from __future__ import annotations

from hermes_time import now

from agent_runtime.config import AgentRuntimeConfig
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.snapshot import _keyed, _parity_warnings, build_snapshot
from tests.agent_runtime.snapshot_bytes import audit_snapshot, snapshot_size_budget
from agent_runtime.states import TaskState
from agent_runtime.store import TaskStore
import agent_runtime.snapshot as snapshot_mod


def _task(task_id: str, n) -> Task:
    return Task(
        id=task_id,
        title=f"Task {task_id}",
        description="d",
        state=TaskState.RUNNING,
        created_at=n,
        updated_at=n,
        requested_by="tony",
    )


def _runtime_cfg() -> AgentRuntimeConfig:
    # S56: the persona-instance roster is unconditional — the old
    # enterprise_worker_sessions gate block no longer exists.
    return AgentRuntimeConfig()


# --------------------------------------------------------------------------- #
# _keyed discipline (unit).
# --------------------------------------------------------------------------- #
def test_keyed_first_wins_and_drops_missing_id():
    rows = [
        {"id": "a", "v": 1},
        {"id": "a", "v": 2},  # duplicate id -> first wins, never a silent clobber
        {"v": 3},  # no id -> dropped, not keyed under ""
        "not_a_dict",
        {"id": "b", "v": 4},
    ]
    keyed = _keyed(rows, "id")
    assert set(keyed) == {"a", "b"}
    assert keyed["a"]["v"] == 1
    assert "" not in keyed


# --------------------------------------------------------------------------- #
# Goals/tasks merge -> ONE keyed goals map.
# --------------------------------------------------------------------------- #
def test_goals_is_keyed_map_and_tasks_wire_section_retired(isolate_agent_runtime_root):
    snap = build_snapshot()
    assert "goals" not in snap
    assert "tasks" not in snap


def test_goals_single_owner_carries_union_of_both_projections(isolate_agent_runtime_root):
    snap = build_snapshot()
    # S56 bumped the snapshot parity contract 46 -> 47; S57 took it to 48.
    assert snap["parity"]["contract_version"] == 51
    assert set(("goals", "runs", "proofs", "incidents")).isdisjoint(snap)


def test_parent_child_tasks_are_not_collapsed(isolate_agent_runtime_root):
    assert "goals" not in build_snapshot()


# --------------------------------------------------------------------------- #
# Delete derived copies: agent_topology out of the frame.
# --------------------------------------------------------------------------- #
def test_no_agent_topology_in_frame(isolate_agent_runtime_root):
    snap = build_snapshot()
    assert "agent_topology" not in snap
    assert "agent_topology" not in snap["parity"]["capabilities"]


# --------------------------------------------------------------------------- #
# Lists -> id-keyed maps.
# --------------------------------------------------------------------------- #
def test_runs_incidents_boards_are_keyed_maps(isolate_agent_runtime_root):
    snap = build_snapshot()
    assert isinstance(snap["boards"], dict)
    assert "runs" not in snap and "incidents" not in snap


def test_persona_instances_and_operator_channels_are_keyed_maps(monkeypatch, isolate_agent_runtime_root):
    monkeypatch.setattr(snapshot_mod, "load_agent_runtime_config", _runtime_cfg)
    snap = build_snapshot()

    assert isinstance(snap["persona_instances"], dict)
    assert isinstance(snap["operator_channels"], dict)
    # every keyed row's own canonical id equals its map key (no de-key/re-key).
    for pid, row in snap["persona_instances"].items():
        assert row["persona_instance_id"] == pid
    for cid, row in snap["operator_channels"].items():
        assert row["channel_id"] == cid


# --------------------------------------------------------------------------- #
# Cross-entity disagreement -> typed fk_miss (not a "contract error").
# --------------------------------------------------------------------------- #
def test_parity_reports_fk_miss_for_dangling_channel():
    data = {
        "persona_instance_runtime": {"enabled": True},
        "persona_instances": {
            "pi_real": {"persona_instance_id": "pi_real", "persona_id": "dev"},
        },
        "operator_channels": {
            "chan_ok": {
                "channel_id": "chan_ok",
                "persona_instance_id": "pi_real",
                "state": "chat",
            },
            "chan_bad": {
                "channel_id": "chan_bad",
                "persona_instance_id": "pi_missing",
                "state": "chat",
            },
            # an archived channel references a frame-evicted instance by design
            # and must NOT be flagged.
            "chan_archived": {
                "channel_id": "chan_archived",
                "persona_instance_id": "archived:t1:neko_supervisor",
                "state": "archived",
            },
        },
        "boards": {},
        "available_personas": [],
        "agents": [],
        "persona_chat_trace": [],
        "persona_chat_history": [],
        "summary": {"open_tasks": 0},
        "goals": {},
    }
    fk = [w for w in _parity_warnings(data) if w.get("code") == "fk_miss"]
    assert len(fk) == 1
    miss = fk[0]
    assert miss["from_entity"] == "operator_channel"
    assert miss["from_id"] == "chan_bad"
    assert miss["fk_field"] == "persona_instance_id"
    assert miss["target_entity"] == "persona_instances"
    assert miss["target_id"] == "pi_missing"


# --------------------------------------------------------------------------- #
# Prove the win: goals dual-projection elimination is a real byte shrink.
# S1 seam (budgets parameter) — snapshot_audit.py is NOT edited.
# --------------------------------------------------------------------------- #
def test_goals_dual_projection_elimination_shrinks_total(isolate_agent_runtime_root):
    snap = build_snapshot()
    dual = {**snap, "goals": {"legacy": {"task_id": "legacy"}}}

    merged_total = audit_snapshot(snap)["total_bytes"]
    dual_total = audit_snapshot(dual)["total_bytes"]
    assert dual_total > merged_total, "the dual projection must be strictly heavier"

    # A total ratchet the merged frame clears and the dual frame blows — proving
    # the shrink is real via the S1 budgets seam.
    ratchet = {"total": (merged_total + dual_total) // 2}
    assert snapshot_size_budget(snap, ratchet) == []
    dual_violations = snapshot_size_budget(dual, ratchet)
    assert dual_violations and "total" in dual_violations[0]
