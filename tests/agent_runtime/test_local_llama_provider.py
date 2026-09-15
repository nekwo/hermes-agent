from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_runtime.local_llama import PROVIDER_ID
from agent_runtime.local_llama.provider import project_config, construction_kwargs, actor_signature, provider_profile
from hermes_cli.config_read_scope import readonly_config_scope, project_readonly_config


def runtime():
    return {"provider": "custom", "model": "hermes-local-example", "base_url": "http://127.0.0.1:8181/v1",
            "api_key": "never-in-config", "local_parameters": {"effective_context_size": 8192,
              "generation": {"max_output_tokens": 1024, "temperature": .7, "top_p": .9, "top_k": 40}}}


def test_local_read_projection_keeps_disk_view_and_other_threads_unchanged():
    original = {"model": {"provider": "cloud"}, "auxiliary": {"compression": {"provider": "cloud", "api_key": "cloud-key"}}}
    before = deepcopy(original)
    with readonly_config_scope(lambda cfg: project_config(cfg, runtime())):
        projected = project_readonly_config(original)
        assert projected["model"]["context_length"] == 8192
        assert projected["auxiliary"]["compression"]["provider"] == "main"
        assert "cloud-key" not in str(projected)
        assert "never-in-config" not in str(projected)
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(project_readonly_config, original).result() == before
    assert project_readonly_config(original) is original
    assert original == before


def test_factory_parameters_and_actor_reuse_signature_track_runtime():
    resolved = runtime()
    kwargs = construction_kwargs(resolved)
    assert kwargs["max_tokens"] == 1024
    assert kwargs["fallback_model"] == []
    assert kwargs["request_overrides"]["extra_body"]["top_k"] == 40
    old = actor_signature(resolved, "session")
    resolved["local_parameters"]["generation"]["max_output_tokens"] = 2048
    assert actor_signature(resolved, "session") != old
    assert construction_kwargs({"provider": "cloud"}) == {}
    assert actor_signature({}, "session") == "session"


def test_local_provider_has_no_api_key_requirement():
    profile = provider_profile()
    assert profile.name == PROVIDER_ID
    assert not profile.env_vars
    assert profile.api_mode == "chat_completions"


def test_real_runner_resolution_bypasses_cloud_and_caches(monkeypatch):
    from agent_runtime import profile_runner
    from agent_runtime.local_llama import provider
    def cloud(**kwargs):
        pytest.fail("managed local selection reached cloud resolver")
    monkeypatch.setattr(profile_runner, "resolve_runtime_provider", cloud)
    calls = []
    def local(model, *, root=None):
        calls.append(model)
        return runtime()
    monkeypatch.setattr(provider, "resolve", local)
    request = profile_runner.AgentRunRequest(profile=None, provider=PROVIDER_ID, model="saved-id")
    assert profile_runner._resolve_request_runtime(request)["provider"] == "custom"
    profile_runner._resolve_request_runtime(request)
    assert calls == ["saved-id", "saved-id"]
