import json
from datetime import timedelta, timezone

import pytest

from hermes_time import now

import agent_runtime.realm_membership as realm_membership_module
from agent_runtime.models import Realm
from agent_runtime.realm_membership import (
    CREDENTIAL_ENV_VAR,
    BackendRealmMembershipProvider,
    RealmSyncCredential,
    adopt_realms,
    load_realm_sync_credential,
    notify_realm_published,
    select_membership_provider,
)
from agent_runtime.realm_sync import RealmMembershipProvider, RealmSyncError
from agent_runtime.store import RealmStore, WorkspaceStore


def _credential_payload(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "realm_id": "realm_eternia",
        "api_base": "https://api.test.invalid/api",
        "api_token": "etk_test_api_token_000000",
        "git_url": "https://git.test.invalid/realm-sync/eternia.git",
        "git_authorization": "Bearer forgejo_sekret_value_1234567890",
        "expires_at": "2999-01-01T00:00:00Z",
    }
    payload.update(overrides)
    return payload


def _credential(**overrides) -> RealmSyncCredential:
    return RealmSyncCredential.parse(_credential_payload(**overrides))


def _realm(realm_id: str = "realm_eternia", server_id: str | None = "srv_9") -> Realm:
    ts = now()
    return Realm(id=realm_id, slug="eternia", name="Eternia", created_at=ts, updated_at=ts, server_id=server_id)


# --- credential parse / load ------------------------------------------------


def test_credential_parse_roundtrip():
    credential = _credential()
    assert credential.realm_id == "realm_eternia"
    assert credential.api_base == "https://api.test.invalid/api"
    assert credential.expires_at.tzinfo is not None
    assert credential.is_expired() is False
    assert credential.git_extra_config() == ["http.extraHeader=Authorization: Bearer forgejo_sekret_value_1234567890"]
    assert credential.bearer_headers()["Authorization"] == "Bearer etk_test_api_token_000000"


def test_credential_missing_field_fails_closed():
    for field_name in ("realm_id", "api_base", "api_token", "git_url", "git_authorization", "expires_at"):
        payload = _credential_payload()
        payload.pop(field_name)
        with pytest.raises(RealmSyncError) as excinfo:
            RealmSyncCredential.parse(payload)
        assert excinfo.value.code == "sync_auth_failed"


def test_credential_bad_schema_version_fails_closed():
    with pytest.raises(RealmSyncError) as excinfo:
        RealmSyncCredential.parse(_credential_payload(schema_version=2))
    assert excinfo.value.code == "sync_auth_failed"


def test_credential_expired_fails_closed_and_leaks_nothing():
    with pytest.raises(RealmSyncError) as excinfo:
        RealmSyncCredential.parse(_credential_payload(expires_at="2020-01-01T00:00:00Z"))
    assert excinfo.value.code == "sync_auth_failed"
    leaked = json.dumps(excinfo.value.safe_details) + str(excinfo.value)
    assert "etk_test_api_token" not in leaked
    assert "forgejo_sekret_value" not in leaked


def test_credential_invalid_expiry_fails_closed():
    with pytest.raises(RealmSyncError) as excinfo:
        RealmSyncCredential.parse(_credential_payload(expires_at="not-a-timestamp"))
    assert excinfo.value.code == "sync_auth_failed"


def test_credential_load_missing_file_fails_closed(tmp_path):
    with pytest.raises(RealmSyncError) as excinfo:
        RealmSyncCredential.load(tmp_path / "missing.json")
    assert excinfo.value.code == "sync_auth_failed"


def test_credential_load_invalid_json_fails_closed(tmp_path):
    path = tmp_path / "cred.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RealmSyncError) as excinfo:
        RealmSyncCredential.load(path)
    assert excinfo.value.code == "sync_auth_failed"


def test_load_credential_resolution_order(tmp_path, monkeypatch):
    monkeypatch.delenv(CREDENTIAL_ENV_VAR, raising=False)
    assert load_realm_sync_credential(None) is None

    env_path = tmp_path / "env-cred.json"
    env_path.write_text(json.dumps(_credential_payload(realm_id="realm_from_env")), encoding="utf-8")
    monkeypatch.setenv(CREDENTIAL_ENV_VAR, str(env_path))
    assert load_realm_sync_credential(None).realm_id == "realm_from_env"

    flag_path = tmp_path / "flag-cred.json"
    flag_path.write_text(json.dumps(_credential_payload(realm_id="realm_from_flag")), encoding="utf-8")
    assert load_realm_sync_credential(str(flag_path)).realm_id == "realm_from_flag"


# --- provider selection (the 4-case matrix) ----------------------------------


def test_provider_selection_serverless_without_credential_uses_stub():
    provider = select_membership_provider(_realm(server_id=None), None)
    assert type(provider) is RealmMembershipProvider
    assert provider.authorize(_realm(server_id=None), "publish").allowed is True


def test_provider_selection_serverless_with_credential_still_uses_stub():
    provider = select_membership_provider(_realm(server_id=None), _credential())
    assert type(provider) is RealmMembershipProvider


def test_provider_selection_server_bound_with_credential_uses_backend():
    provider = select_membership_provider(_realm(), _credential())
    assert isinstance(provider, BackendRealmMembershipProvider)


def test_provider_selection_server_bound_without_credential_fails_closed():
    with pytest.raises(RealmSyncError) as excinfo:
        select_membership_provider(_realm(), None)
    assert excinfo.value.code == "sync_auth_failed"


# --- backend decision mapping -------------------------------------------------


def test_backend_provider_allows_on_allowed_response(monkeypatch):
    def _allow(method, url, **kwargs):
        assert method == "GET"
        assert "/realms/realm_eternia/sync/permission?action=pull" in url
        assert kwargs["headers"]["Authorization"] == "Bearer etk_test_api_token_000000"
        return 200, {"allowed": True}

    monkeypatch.setattr(realm_membership_module, "_request_json", _allow)
    decision = BackendRealmMembershipProvider(_credential()).authorize(_realm(), "pull")
    assert decision.allowed is True


def test_backend_provider_maps_deny_codes(monkeypatch):
    monkeypatch.setattr(
        realm_membership_module,
        "_request_json",
        lambda method, url, **kwargs: (200, {"allowed": False, "code": "role_insufficient", "message": "publisher role required"}),
    )
    decision = BackendRealmMembershipProvider(_credential()).authorize(_realm(), "publish")
    assert decision.allowed is False
    assert decision.code == "role_insufficient"


def test_backend_provider_maps_http_403(monkeypatch):
    monkeypatch.setattr(
        realm_membership_module,
        "_request_json",
        lambda method, url, **kwargs: (403, {"allowed": False, "code": "membership_denied", "message": "not a member"}),
    )
    decision = BackendRealmMembershipProvider(_credential()).authorize(_realm(), "pull")
    assert decision.allowed is False
    assert decision.code == "membership_denied"


def test_backend_provider_fails_closed_on_network_error(monkeypatch):
    def _boom(method, url, **kwargs):
        raise RealmSyncError("sync_remote_unreachable", "connection refused", retryable=True)

    monkeypatch.setattr(realm_membership_module, "_request_json", _boom)
    decision = BackendRealmMembershipProvider(_credential()).authorize(_realm(), "pull")
    assert decision.allowed is False
    assert decision.code == "sync_remote_unreachable"


def test_backend_provider_fails_closed_on_malformed_response(monkeypatch):
    monkeypatch.setattr(realm_membership_module, "_request_json", lambda method, url, **kwargs: (200, None))
    decision = BackendRealmMembershipProvider(_credential()).authorize(_realm(), "status")
    assert decision.allowed is False
    assert decision.code == "sync_remote_unreachable"


def test_backend_provider_denies_expired_credential_without_http(monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("expired credential must be denied before any HTTP call")

    monkeypatch.setattr(realm_membership_module, "_request_json", _forbidden)
    fresh = _credential()
    expired = RealmSyncCredential(
        schema_version=fresh.schema_version,
        realm_id=fresh.realm_id,
        api_base=fresh.api_base,
        api_token=fresh.api_token,
        git_url=fresh.git_url,
        git_authorization=fresh.git_authorization,
        expires_at=now().astimezone(timezone.utc) - timedelta(hours=1),
    )
    decision = BackendRealmMembershipProvider(expired).authorize(_realm(), "pull")
    assert decision.allowed is False
    assert decision.code == "sync_auth_failed"


def test_backend_provider_denies_realm_mismatch_without_http(monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("realm mismatch must be denied before any HTTP call")

    monkeypatch.setattr(realm_membership_module, "_request_json", _forbidden)
    decision = BackendRealmMembershipProvider(_credential()).authorize(_realm(realm_id="realm_other"), "pull")
    assert decision.allowed is False
    assert decision.code == "sync_auth_failed"


def test_backend_provider_rejects_unknown_action():
    decision = BackendRealmMembershipProvider(_credential()).authorize(_realm(), "erase")
    assert decision.allowed is False
    assert decision.code == "invalid_request"


def test_request_json_sends_hermes_user_agent(monkeypatch):
    """The stdlib default UA (Python-urllib/3.x) is bot-blocked by the
    Cloudflare edge in front of the production API — every realm-sync
    request must carry an explicit Hermes-Agent UA."""
    captured = {}

    class _FakeResponse:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def _fake_urlopen(request, timeout):
        captured["user_agent"] = request.get_header("User-agent")
        return _FakeResponse()

    monkeypatch.setattr(
        realm_membership_module.urllib.request, "urlopen", _fake_urlopen
    )
    status, _payload = realm_membership_module._request_json(
        "GET", "https://api.test.invalid/api/realms"
    )
    assert status == 200
    assert captured["user_agent"].startswith("Hermes-Agent/")
    assert "Python-urllib" not in captured["user_agent"]


def test_decision_from_response_honors_ninja_detail_code():
    """Django Ninja HttpError bodies carry the deny code as
    {"detail": …} — the real backend reason must survive instead of
    collapsing to the generic membership_denied string."""
    decision = realm_membership_module._decision_from_response(
        403, {"detail": "role_insufficient"}, action="publish"
    )
    assert decision.allowed is False
    assert decision.code == "role_insufficient"


def test_decision_from_response_uses_unknown_detail_as_message():
    decision = realm_membership_module._decision_from_response(
        403, {"detail": "banned from this server"}, action="pull"
    )
    assert decision.allowed is False
    assert decision.code == "membership_denied"
    assert decision.message == "banned from this server"


# --- realm adopt ---------------------------------------------------------------


def _adopt_items():
    return [
        {
            "id": "realm_eternia",
            "server_id": "srv_9",
            "slug": "eternia",
            "name": "Eternia Community",
            "can_pull": True,
            "can_publish": True,
            "git_url": "https://git.test.invalid/realm-sync/eternia.git",
        },
        {
            "id": "realm_other",
            "server_id": "srv_other",
            "slug": "other",
            "name": "Other Server Realm",
            "can_pull": True,
            "can_publish": False,
            "git_url": "https://git.test.invalid/realm-sync/other.git",
        },
    ]


def test_adopt_upserts_granted_realms(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setattr(
        realm_membership_module,
        "_request_json",
        lambda method, url, **kwargs: (200, {"count": 2, "items": _adopt_items()}),
    )
    adopted = adopt_realms(_credential())
    assert sorted(item.id for item in adopted) == ["realm_eternia", "realm_other"]

    stored = RealmStore().get("realm_eternia")
    assert stored.server_id == "srv_9"
    assert stored.slug == "eternia"
    assert stored.name == "Eternia Community"
    assert stored.sync_manifest_ref == "https://git.test.invalid/realm-sync/eternia.git"

    # The store mutation must advance the EventLog watermark — the
    # stream/read-model pipeline is invisible to event-less writes.
    from agent_runtime.events import EventLog

    adopted_events = [e for _, e in EventLog().iter_from_offset(0) if e.type == "realm.adopted"]
    assert sorted(e.payload["realm_id"] for e in adopted_events) == ["realm_eternia", "realm_other"]
    assert adopted_events[0].payload["server_id"] == "srv_9"


def test_adopt_materializes_fresh_realm_owned_default_workspace(
    isolate_agent_runtime_root, monkeypatch
):
    items = _adopt_items()
    items[0].update({
        "default_workspace_id": "ws_realm_fresh",
        "default_workspace_version": 3,
        "default_workspace_name": "Custom Office",
    })
    monkeypatch.setattr(
        realm_membership_module,
        "_request_json",
        lambda method, url, **kwargs: (200, {"count": 2, "items": items}),
    )

    adopt_realms(_credential())

    realm = RealmStore().get("realm_eternia")
    workspace = WorkspaceStore().get("ws_realm_fresh")
    assert realm.default_workspace_id == "ws_realm_fresh"
    assert realm.default_workspace_version == 3
    assert realm.default_workspace_name == "Custom Office"
    assert realm.workspace_ids == ["ws_realm_fresh"]
    assert workspace.name == "Custom Office"
    assert workspace.realm_id == realm.id
    assert workspace.agent_ids == []


def test_adopt_default_workspace_collision_fails_closed_without_moving_workspace(
    isolate_agent_runtime_root, monkeypatch
):
    other = RealmStore().create(name="Other")
    WorkspaceStore().create(
        name="Occupied", realm_id=other.id, workspace_id="ws_realm_collision"
    )
    items = _adopt_items()
    items[0].update({
        "default_workspace_id": "ws_realm_collision",
        "default_workspace_name": "Custom Office",
    })
    monkeypatch.setattr(
        realm_membership_module,
        "_request_json",
        lambda method, url, **kwargs: (200, {"count": 2, "items": items}),
    )

    with pytest.raises(RealmSyncError) as excinfo:
        adopt_realms(_credential())

    assert excinfo.value.code == "sync_conflict"
    assert WorkspaceStore().get("ws_realm_collision").realm_id == other.id
    with pytest.raises(Exception):
        RealmStore().get("realm_eternia")


def test_adopt_is_idempotent(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setattr(
        realm_membership_module,
        "_request_json",
        lambda method, url, **kwargs: (200, {"count": 2, "items": _adopt_items()}),
    )
    adopt_realms(_credential())
    import agent_runtime.paths as runtime_paths

    realm_file = runtime_paths.realm_path("realm_eternia")
    before = realm_file.read_text(encoding="utf-8")
    adopt_realms(_credential())
    assert realm_file.read_text(encoding="utf-8") == before


def test_readopt_preserves_workspace_ids(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setattr(
        realm_membership_module,
        "_request_json",
        lambda method, url, **kwargs: (200, {"count": 2, "items": _adopt_items()}),
    )
    adopt_realms(_credential())
    store = RealmStore()
    realm = store.get("realm_eternia")
    realm.workspace_ids = ["ws_launcher"]
    store.save(realm)

    renamed = _adopt_items()
    renamed[0]["name"] = "Eternia Community (Renamed)"
    monkeypatch.setattr(
        realm_membership_module,
        "_request_json",
        lambda method, url, **kwargs: (200, {"count": 2, "items": renamed}),
    )
    adopt_realms(_credential())

    stored = store.get("realm_eternia")
    assert stored.name == "Eternia Community (Renamed)"
    assert stored.workspace_ids == ["ws_launcher"]


def test_adopt_filters_by_server(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setattr(
        realm_membership_module,
        "_request_json",
        lambda method, url, **kwargs: (200, {"count": 2, "items": _adopt_items()}),
    )
    adopted = adopt_realms(_credential(), server_id="srv_9")
    assert [item.id for item in adopted] == ["realm_eternia"]
    with pytest.raises(Exception):
        RealmStore().get("realm_other")


def test_adopt_dry_run_persists_nothing(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setattr(
        realm_membership_module,
        "_request_json",
        lambda method, url, **kwargs: (200, {"count": 2, "items": _adopt_items()}),
    )
    adopted = adopt_realms(_credential(), dry_run=True)
    assert sorted(item.id for item in adopted) == ["realm_eternia", "realm_other"]
    assert RealmStore().list_all() == []

    from agent_runtime.events import EventLog

    assert [e for _, e in EventLog().iter_from_offset(0) if e.type == "realm.adopted"] == []


def test_adopt_denied_maps_membership_denied(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setattr(
        realm_membership_module,
        "_request_json",
        lambda method, url, **kwargs: (403, {"allowed": False, "code": "membership_denied", "message": "no"}),
    )
    with pytest.raises(RealmSyncError) as excinfo:
        adopt_realms(_credential())
    assert excinfo.value.code == "membership_denied"


def test_adopt_fails_closed_on_malformed_response(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setattr(realm_membership_module, "_request_json", lambda method, url, **kwargs: (200, {"items": "nope"}))
    with pytest.raises(RealmSyncError) as excinfo:
        adopt_realms(_credential())
    assert excinfo.value.code == "sync_remote_unreachable"


# --- publish notify -------------------------------------------------------------


def test_notify_realm_published_posts_counts(monkeypatch):
    captured: dict = {}

    def _capture(method, url, **kwargs):
        captured.update({"method": method, "url": url, "body": kwargs.get("body")})
        return 204, None

    monkeypatch.setattr(realm_membership_module, "_request_json", _capture)
    notify_realm_published(_credential(), "realm_eternia", commit="a" * 40, artifact_counts={"skill": 3, "soul": 1})

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/realms/realm_eternia/sync/published")
    assert captured["body"] == {"commit": "a" * 40, "artifact_counts": {"skill": 3, "soul": 1}}


def test_notify_realm_published_raises_on_rejection(monkeypatch):
    monkeypatch.setattr(realm_membership_module, "_request_json", lambda method, url, **kwargs: (500, None))
    with pytest.raises(RealmSyncError) as excinfo:
        notify_realm_published(_credential(), "realm_eternia", commit="a" * 40, artifact_counts={"skill": 1})
    assert excinfo.value.code == "sync_remote_unreachable"
