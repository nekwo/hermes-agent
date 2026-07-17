import json
from pathlib import Path

from agent_runtime.mission_goal import create_mission_goal_from_request
from agent_runtime.snapshot import build_snapshot
from agent_runtime.store import TaskStore


FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "mission_control" / "stage38"


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_stage38_fixture_corpus_is_complete_and_safe():
    expected = {
        "goal_create_request.valid.json",
        "goal_create_response.created.json",
        "goal_create_response.already_created.json",
        "goal_create_error.blueprint_not_found.json",
        "goal_create_error.duplicate_conflict.json",
        "mission_snapshot.neko_to_dev_running.json",
        "mission_snapshot.qa_required_missing_proof.json",
        "mission_snapshot.ready_for_tony.json",
        "mission_snapshot.waiting_for_operator.json",
        "mission_snapshot.stale_wrong_root.json",
        "mission_snapshot.unsafe_fields_suppressed.json",
        "mission_snapshot.unknown_enum_degraded.json",
        "mission_snapshot.archived_final_record.json",
    }
    assert {path.name for path in FIXTURE_DIR.glob("*.json")} == expected

    for path in FIXTURE_DIR.glob("mission_snapshot.*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 1
        for goal in payload.get("goals", []):
            gate = goal.get("proof_gate_state", {})
            for evidence in gate.get("captured_evidence", []):
                assert evidence["uri"].startswith("artifact://")
        text = path.read_text(encoding="utf-8")
        assert "C:\\" not in text
        assert "X:\\" not in text


def test_stage38_goal_create_request_fixture_creates_blueprint_goal(
    isolate_agent_runtime_root,
):
    request = _load("goal_create_request.valid.json")

    response = create_mission_goal_from_request(request)

    assert response["schema_version"] == 1
    assert response["state"] == "created"
    assert response["blueprint_id"] == "visual_ui_qa"
    stored = TaskStore().get(response["task_id"])
    assert stored.mission_plan.blueprint_id == "visual_ui_qa"

    duplicate = create_mission_goal_from_request(request)
    assert duplicate["state"] == "already_created"
    assert duplicate["task_id"] == response["task_id"]


def test_stage38_runtime_snapshot_matches_fixture_contract_shape(isolate_agent_runtime_root):
    """The hand-authored snapshot fixtures must mirror real build_snapshot() output.

    Without this, the cross-repo fixtures can drift away from what the runtime
    actually emits (the gap this guards against: fixtures that parse but were
    never produced by the contract layer).
    """

    response = create_mission_goal_from_request(_load("goal_create_request.valid.json"))
    snapshot = build_snapshot()
    row = next(item for item in list(snapshot["goals"].values()) if item["task_id"] == response["task_id"])

    fixture = _load("mission_snapshot.neko_to_dev_running.json")
    fixture_goal = fixture["goals"][0]

    # The nested read-model objects the bridge passes straight through must have
    # the same key set the runtime produces (additive-only within the contract).
    # S8: these mission-level objects stay in the goal HEAD (office/roster/HUD
    # render them every frame); ``operator_capabilities`` moved to the on-demand
    # detail body (verified against the head-fetched detail below).
    for model_key in ("mission_level_state", "mission_flow_timeline", "proof_gate_state"):
        runtime_keys = set(row[model_key].keys())
        fixture_keys = set(fixture_goal[model_key].keys())
        assert fixture_keys <= runtime_keys, (model_key, fixture_keys - runtime_keys)
    # S8: operator_capabilities is served by `harness goal detail`, not the head.
    from agent_runtime.snapshot import goal_detail_for_task

    assert "operator_capabilities" not in row
    detail = goal_detail_for_task(row["task_id"])
    assert set(fixture_goal["operator_capabilities"].keys()) <= set(detail["operator_capabilities"].keys())

    runtime_actor_keys = set(row["mission_level_state"]["actors"][0].keys())
    fixture_actor_keys = set(fixture_goal["mission_level_state"]["actors"][0].keys())
    assert fixture_actor_keys <= runtime_actor_keys, fixture_actor_keys - runtime_actor_keys

    # Envelope: real parity carries every Stage 38 field the Launcher consumes.
    parity = snapshot["parity"]
    assert parity["contract_version"] == 44
    assert {"runtime_root", "profile", "capabilities", "freshness"} <= set(parity.keys())
    assert row["proof_gate_state"]["gate_state"] in {
        "not_required", "incomplete", "running", "blocked", "failed", "passed", "waived",
    }
