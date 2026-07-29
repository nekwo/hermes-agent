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


def test_blueprint_list_endpoint_is_removed(client):
    response = client.get("/api/blueprints")

    assert response.status_code == 404


def test_blueprint_run_endpoint_is_removed(client):
    response = client.post(
        "/api/blueprints/one_agent_smoke/run",
        json={"goal": "web smoke", "bindings": {"builder": "persona:dev"}, "dry_run": True},
    )

    # The dashboard's GET catch-all still recognizes the path, but there is no
    # longer a POST handler capable of instantiating a runtime blueprint.
    assert response.status_code == 405


def test_profile_promote_endpoint_persists_persona(client, monkeypatch):
    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: name == "fresh")

    response = client.post("/api/profiles/fresh/promote", json={"slot_role": "builder"})

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["persona_id"] == "fresh"

    from agent_runtime.store import AgentStore

    persona = AgentStore().get("fresh")
    assert persona.hermes_profile == "fresh"
    assert persona.role == "dev"
