import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def test_blueprint_list_endpoint_returns_bundled_blueprints(client):
    response = client.get("/api/blueprints")

    assert response.status_code == 200
    data = response.json()
    ids = {item["id"] for item in data["blueprints"]}
    assert {"one_agent_smoke", "two_agent_build_verify", "neko_dev_qa_basic"}.issubset(ids)
    assert "runs" in data


def test_blueprint_run_endpoint_dry_run_returns_next_action(client):
    response = client.post(
        "/api/blueprints/one_agent_smoke/run",
        json={"goal": "web smoke", "bindings": {"builder": "persona:dev"}, "dry_run": True},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["created"] is False
    assert data["next_action"]["type"] == "run_slot"
    assert data["next_action"]["slot_id"] == "builder"


def test_blueprint_run_endpoint_persists_task(client):
    response = client.post(
        "/api/blueprints/one_agent_smoke/run",
        json={"goal": "web persisted smoke", "bindings": {"builder": "persona:dev"}},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["created"] is True

    from agent_runtime.store import TaskStore

    task = TaskStore().get(data["task_id"])
    assert task.mission_plan.blueprint_id == "one_agent_smoke"
    assert task.mission_plan.current_stage_id == "build"
