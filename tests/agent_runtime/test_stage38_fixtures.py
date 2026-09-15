import json
import importlib.util
from pathlib import Path

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


def test_stage38_goal_create_request_fixture_is_historical_after_lane_removal():
    request = _load("goal_create_request.valid.json")
    assert request["blueprint"]["requested_blueprint_id"] == "visual_ui_qa"
    assert importlib.util.find_spec("agent_runtime.mission_goal") is None


def test_stage38_historical_snapshot_fixture_remains_safe_to_parse():
    fixture = _load("mission_snapshot.neko_to_dev_running.json")
    fixture_goal = fixture["goals"][0]
    assert fixture_goal["proof_gate_state"]["gate_state"] in {
        "not_required", "incomplete", "running", "blocked", "failed", "passed", "waived",
    }
