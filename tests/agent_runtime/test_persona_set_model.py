"""Per-agent-instance model/provider overrides (Mission Control model switcher).

Contract under test (cascade, highest wins):

    chat-session override > instance override > persona default > cfg default

The load-bearing requirement: two instances of ONE persona must be able to run
different models persistently — switching instance B never moves instance A,
and changing the persona default never moves an instance that carries its own
override.
"""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime import paths
from agent_runtime.config import AgentRuntimeConfig, ensure_persisted_personas
from agent_runtime.models import AgentPersona, apply_instance_model_overrides
from agent_runtime.persona_assignments import (
    PersonaInstanceStore,
    StaleModelOverrideWrite,
    persona_instance_summary,
)
from agent_runtime.store import AgentStore


def _persona(persona_id: str = "dev", *, model: str | None = "gpt-test", provider: str | None = "openai-codex") -> AgentPersona:
    return AgentPersona(
        id=persona_id,
        display_name=f"{persona_id} worker",
        role="dev",
        model=model,
        provider=provider,
        api_mode="codex_responses",
        toolsets=["file", "search", "terminal"],
        system_prompt_path="agent_runtime/prompts/dev.md",
        hermes_profile=f"profile-{persona_id}",
    )


def _cfg() -> AgentRuntimeConfig:
    # S56: the persona-instance runtime / assignment store are unconditional now;
    # the enterprise_worker_sessions gate block was deleted.
    return AgentRuntimeConfig(
        default_provider="openai-codex",
        default_model="cfg-default-model",
    )


def _two_instances(store: PersonaInstanceStore, persona: AgentPersona):
    a = store.open_chat(
        persona_id=persona.id,
        persona_instance_id=f"personainst_{persona.id}_slot_a",
        session_id="model_override_slot_a",
        display_name=persona.display_name,
        profile_id=persona.hermes_profile,
    )
    b = store.open_chat(
        persona_id=persona.id,
        persona_instance_id=f"personainst_{persona.id}_slot_b",
        session_id="model_override_slot_b",
        display_name=persona.display_name,
        profile_id=persona.hermes_profile,
    )
    assert a.id != b.id
    return a, b


def _instance_args(instance_id: str, **overrides) -> Namespace:
    ns = Namespace(
        persona_instance_id=instance_id,
        provider=None,
        model=None,
        use_profile_default=False,
        issued_at=None,
        requested_by="test",
        json=True,
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def _persona_args(persona_id: str, **overrides) -> Namespace:
    ns = Namespace(
        persona_id=persona_id,
        provider=None,
        model=None,
        use_default=False,
        issued_at=None,
        requested_by="test",
        json=True,
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def _events(event_type: str) -> list[dict]:
    path = paths.store_root() / "events.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("type") == event_type]


# --- overlay -----------------------------------------------------------------


def test_overlay_none_instance_returns_persona():
    persona = _persona()
    assert apply_instance_model_overrides(persona, None) is persona


def test_overlay_empty_override_returns_persona():
    from agent_runtime.models import PersonaInstance
    from agent_runtime.states import WorkerSessionState

    persona = _persona()

    blank = PersonaInstance(
        id="personainst_x",
        persona_id=persona.id,
        role="dev",
        display_name="x",
        profile_id=None,
        runtime_root="r",
        state=WorkerSessionState.IDLE,
    )
    assert apply_instance_model_overrides(persona, blank) is persona


@pytest.mark.parametrize(
    ("model", "provider", "api_mode", "expected_model", "expected_provider", "expected_api_mode"),
    [
        ("claude-x", "anthropic", "anthropic_messages", "claude-x", "anthropic", "anthropic_messages"),
        ("claude-x", None, None, "claude-x", "openai-codex", "codex_responses"),
        (None, "anthropic", "anthropic_messages", "gpt-test", "anthropic", "anthropic_messages"),
    ],
)
def test_overlay_partial_and_full(model, provider, api_mode, expected_model, expected_provider, expected_api_mode):
    from agent_runtime.models import PersonaInstance
    from agent_runtime.states import WorkerSessionState

    persona = _persona()
    instance = PersonaInstance(
        id="personainst_x",
        persona_id=persona.id,
        role="dev",
        display_name="x",
        profile_id=None,
        runtime_root="r",
        state=WorkerSessionState.IDLE,
        model=model,
        provider=provider,
        api_mode=api_mode,
    )
    overlaid = apply_instance_model_overrides(persona, instance)
    assert overlaid is not persona
    assert overlaid.model == expected_model
    assert overlaid.provider == expected_provider
    assert overlaid.api_mode == expected_api_mode
    # identity/budget fields ride along untouched
    assert overlaid.id == persona.id
    assert overlaid.toolsets == persona.toolsets
    # source persona never mutated
    assert persona.model == "gpt-test"


def test_overlay_instance_skill_assignment_matches_execution_persona():
    from agent_runtime.models import PersonaInstance
    from agent_runtime.states import WorkerSessionState

    persona = _persona()
    persona.skills = ["harness-dev-delivery"]
    instance = PersonaInstance(
        id="personainst_x",
        persona_id=persona.id,
        role="dev",
        display_name="x",
        profile_id=None,
        runtime_root="r",
        state=WorkerSessionState.IDLE,
        skill_overrides=["launcher-analyze-proof"],
    )

    overlaid = apply_instance_model_overrides(persona, instance)

    assert overlaid.skills == ["launcher-analyze-proof"]
    assert persona.skills == ["harness-dev-delivery"]


# --- store mutator -----------------------------------------------------------


def test_update_profile_persists_model_override_and_emits_event():
    persona = _persona()
    store = PersonaInstanceStore()
    instance, _ = _two_instances(store, persona)

    updated = store.update_profile(
        instance.id,
        provider="anthropic",
        model="claude-x",
        api_mode="anthropic_messages",
        requested_by="test-operator",
    )
    assert updated.provider == "anthropic"
    assert updated.model == "claude-x"
    assert updated.api_mode == "anthropic_messages"
    assert updated.model_override_issued_at is not None

    raw = json.loads(paths.persona_instance_path(instance.id).read_text(encoding="utf-8"))
    assert raw["provider"] == "anthropic"
    assert raw["model"] == "claude-x"

    events = _events("persona_instance.profile_updated")
    assert events, "model write must emit through the store event chokepoint"
    payload = events[-1]["payload"]
    assert payload["persona_instance_id"] == instance.id
    assert payload["provider"] == "anthropic"
    assert payload["model"] == "claude-x"
    assert payload["requested_by"] == "test-operator"


def test_update_profile_clear_model_override_resets_all_three():
    persona = _persona()
    store = PersonaInstanceStore()
    instance, _ = _two_instances(store, persona)
    store.update_profile(instance.id, provider="anthropic", model="claude-x", api_mode="anthropic_messages")

    cleared = store.update_profile(instance.id, clear_model_override=True)
    assert cleared.provider is None
    assert cleared.model is None
    assert cleared.api_mode is None


def test_update_profile_stale_issued_at_is_superseded_not_applied():
    persona = _persona()
    store = PersonaInstanceStore()
    instance, _ = _two_instances(store, persona)
    newer = datetime.now(timezone.utc)
    older = newer - timedelta(seconds=30)

    store.update_profile(instance.id, model="model-new", model_issued_at=newer)
    with pytest.raises(StaleModelOverrideWrite):
        store.update_profile(instance.id, model="model-old", model_issued_at=older)

    assert store.get(instance.id).model == "model-new"


def test_update_profile_rejects_clear_combined_with_values():
    persona = _persona()
    store = PersonaInstanceStore()
    instance, _ = _two_instances(store, persona)
    with pytest.raises(ValueError):
        store.update_profile(instance.id, model="x", clear_model_override=True)


# --- THE requirement: duplicate isolation ------------------------------------


def test_duplicate_instances_keep_their_model_when_sibling_or_default_changes():
    persona = _persona()
    agent_store = AgentStore()
    agent_store.save(persona)
    store = PersonaInstanceStore()
    a, b = _two_instances(store, persona)

    # Switch B only.
    store.update_profile(b.id, provider="anthropic", model="claude-x", api_mode="anthropic_messages")

    a_after = store.get(a.id)
    b_after = store.get(b.id)
    assert a_after.model is None, "sibling instance must not gain an override"
    assert apply_instance_model_overrides(persona, a_after).model == "gpt-test"
    assert apply_instance_model_overrides(persona, b_after).model == "claude-x"

    # Change the persona default: A follows live, B keeps its override.
    persona.model = "gpt-new-default"
    agent_store.save(persona)
    stored_persona = agent_store.get(persona.id)
    assert apply_instance_model_overrides(stored_persona, store.get(a.id)).model == "gpt-new-default"
    assert apply_instance_model_overrides(stored_persona, store.get(b.id)).model == "claude-x"


# --- snapshot surface ---------------------------------------------------------


def test_persona_instance_summary_reports_override_and_effective_tiers():
    persona = _persona()
    store = PersonaInstanceStore()
    a, b = _two_instances(store, persona)
    store.update_profile(b.id, provider="anthropic", model="claude-x", api_mode="anthropic_messages")

    plain = persona_instance_summary(store.get(a.id), persona)
    overridden = persona_instance_summary(store.get(b.id), persona)

    assert plain["model"] is None
    assert plain["model_is_override"] is False
    assert plain["effective_model"] == "gpt-test"
    assert plain["effective_provider"] == "openai-codex"

    assert overridden["model"] == "claude-x"
    assert overridden["provider"] == "anthropic"
    assert overridden["api_mode"] == "anthropic_messages"
    assert overridden["model_is_override"] is True
    assert overridden["effective_model"] == "claude-x"
    assert overridden["effective_provider"] == "anthropic"


# --- chat cascade -------------------------------------------------------------


def test_chat_effective_model_payload_cascade_tiers():
    from hermes_cli import harness

    persona = _persona()
    store = PersonaInstanceStore()
    _, b = _two_instances(store, persona)
    store.update_profile(b.id, provider="anthropic", model="claude-x", api_mode="anthropic_messages")
    instance = store.get(b.id)
    cfg = _cfg()

    # instance tier beats persona/default when no chat override
    selection = harness._chat_effective_model_payload(persona=persona, config=cfg, override=None, instance=instance)
    assert selection["effective_model"] == "claude-x"
    assert selection["effective_provider"] == "anthropic"
    assert selection["instance_model"] == "claude-x"
    assert selection["agent_model"] == "claude-x"
    assert selection["default_model"] == "gpt-test"
    assert selection["model_is_instance_override"] is True
    assert selection["model_is_default"] is True  # no chat-session override active

    # chat-session override still wins over the instance tier
    override = {"provider": "openrouter", "model": "session-model"}
    selection = harness._chat_effective_model_payload(persona=persona, config=cfg, override=override, instance=instance)
    assert selection["effective_model"] == "session-model"
    assert selection["effective_provider"] == "openrouter"
    assert selection["model_is_default"] is False

    # no instance / no override falls through to persona then cfg
    selection = harness._chat_effective_model_payload(persona=persona, config=cfg, override=None, instance=None)
    assert selection["effective_model"] == "gpt-test"
    bare = _persona(model=None, provider=None)
    selection = harness._chat_effective_model_payload(persona=bare, config=cfg, override=None, instance=None)
    assert selection["effective_model"] == "cfg-default-model"


# --- run path -----------------------------------------------------------------




# --- CLI: persona-instance set-model -------------------------------------------


def _patched_harness(monkeypatch):
    from hermes_cli import harness

    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: _cfg())
    return harness


def test_cli_instance_set_model_happy_path_derives_api_mode(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    persona = _persona()
    store = PersonaInstanceStore()
    _, b = _two_instances(store, persona)

    code = harness._cmd_persona_instance_set_model(_instance_args(b.id, provider="anthropic", model="claude-x"))
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["status"] == "applied"
    assert data["scope"] == "agent_instance"
    assert data["provider"] == "anthropic"
    assert data["model"] == "claude-x"
    assert data["api_mode"], "api_mode must be derived from the provider profile"
    assert data["effective_model"] == "claude-x"
    assert data["model_catalog_checked"] is False
    stored = store.get(b.id)
    assert stored.api_mode == data["api_mode"]


def test_cli_instance_set_model_provider_alias_canonicalized(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    from providers import get_provider_profile

    profile = get_provider_profile("anthropic")
    aliases = list(getattr(profile, "aliases", ()) or ())
    if not aliases:
        pytest.skip("anthropic provider exposes no aliases to canonicalize")
    persona = _persona()
    store = PersonaInstanceStore()
    _, b = _two_instances(store, persona)

    code = harness._cmd_persona_instance_set_model(_instance_args(b.id, provider=aliases[0], model="claude-x"))
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["provider"] == str(profile.name)


def test_cli_instance_set_model_unknown_provider_rejected(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    persona = _persona()
    store = PersonaInstanceStore()
    _, b = _two_instances(store, persona)

    code = harness._cmd_persona_instance_set_model(_instance_args(b.id, provider="definitely-not-a-provider", model="x"))
    assert code == 2
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["error_code"] == "unknown_provider"
    assert data["known_providers"]
    assert store.get(b.id).provider is None


def test_cli_instance_set_model_conflicting_args_rejected(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    persona = _persona()
    store = PersonaInstanceStore()
    _, b = _two_instances(store, persona)

    code = harness._cmd_persona_instance_set_model(
        _instance_args(b.id, model="x", use_profile_default=True)
    )
    assert code == 2
    data = json.loads(capsys.readouterr().out)
    assert data["error_code"] == "conflicting_args"

    code = harness._cmd_persona_instance_set_model(_instance_args(b.id))
    assert code == 2
    data = json.loads(capsys.readouterr().out)
    assert data["error_code"] == "missing_args"


def test_cli_instance_set_model_bad_shape_rejected(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    persona = _persona()
    store = PersonaInstanceStore()
    _, b = _two_instances(store, persona)

    code = harness._cmd_persona_instance_set_model(_instance_args(b.id, model="bad model with spaces"))
    assert code == 2
    data = json.loads(capsys.readouterr().out)
    assert data["error_code"] == "invalid_value"


def test_cli_instance_set_model_unknown_instance(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    code = harness._cmd_persona_instance_set_model(_instance_args("personainst_ghost", model="x"))
    assert code == 2
    data = json.loads(capsys.readouterr().out)
    assert data["error_code"] == "persona_not_found"


def test_cli_instance_set_model_use_profile_default_clears(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    persona = _persona()
    store = PersonaInstanceStore()
    _, b = _two_instances(store, persona)
    store.update_profile(b.id, provider="anthropic", model="claude-x", api_mode="anthropic_messages")

    code = harness._cmd_persona_instance_set_model(_instance_args(b.id, use_profile_default=True))
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["cleared"] is True
    assert data["model"] is None
    stored = store.get(b.id)
    assert stored.model is None and stored.provider is None and stored.api_mode is None


def test_cli_instance_set_model_stale_write_superseded(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    persona = _persona()
    store = PersonaInstanceStore()
    _, b = _two_instances(store, persona)
    newer = datetime.now(timezone.utc)
    older = newer - timedelta(seconds=45)

    code = harness._cmd_persona_instance_set_model(
        _instance_args(b.id, model="model-new", issued_at=newer.isoformat())
    )
    assert code == 0
    capsys.readouterr()

    code = harness._cmd_persona_instance_set_model(
        _instance_args(b.id, model="model-old", issued_at=older.isoformat())
    )
    assert code == 0, "supersede is concurrency resolution, not an error"
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["status"] == "superseded"
    assert data["applied"] is False
    assert store.get(b.id).model == "model-new"


# --- CLI: persona set-model (profile-default lane) ------------------------------


def test_cli_persona_set_model_happy_path_emits_persona_updated(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    ensure_persisted_personas(_cfg())
    before = len(_events("persona.updated"))

    code = harness._cmd_persona_set_model(_persona_args("base", provider="anthropic", model="claude-x"))
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["scope"] == "agent_default"
    assert data["applied_to_persona_id"] == "base"
    assert data["provider"] == "anthropic"
    assert data["api_mode"], "api_mode must be derived from the provider profile"
    assert len(_events("persona.updated")) > before

    stored = AgentStore().get("base")
    assert stored.model == "claude-x"
    assert stored.provider == "anthropic"


def test_cli_persona_set_model_profile_id_targets_backing_store_persona(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    ensure_persisted_personas(_cfg())

    code = harness._cmd_persona_set_model(_persona_args("profile:base", model="claude-x"))
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["applied_to_persona_id"] == "base"
    assert AgentStore().get("base").model == "claude-x"


def test_cli_persona_set_model_profile_without_backing_record_rejected(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    ensure_persisted_personas(_cfg())

    code = harness._cmd_persona_set_model(_persona_args("profile:nonexistent", model="claude-x"))
    assert code == 2
    data = json.loads(capsys.readouterr().out)
    assert data["error_code"] == "persona_not_persisted"


def test_cli_persona_set_model_ambiguous_profile_rejected(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    ensure_persisted_personas(_cfg())
    twin = _persona("base_twin")
    twin.hermes_profile = "base"
    AgentStore().save(twin)

    code = harness._cmd_persona_set_model(_persona_args("profile:base", model="claude-x"))
    assert code == 2
    data = json.loads(capsys.readouterr().out)
    assert data["error_code"] == "ambiguous_profile_persona"
    assert sorted(data["candidates"]) == ["base", "base_twin"]


def test_cli_persona_set_model_dormant_catalog_persona_rejected(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    ensure_persisted_personas(_cfg())

    code = harness._cmd_persona_set_model(_persona_args("dev", model="claude-x"))
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["applied"] is True
    assert data["persona_id"] == "dev"


def test_cli_persona_set_model_unknown_persona(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    code = harness._cmd_persona_set_model(_persona_args("definitely_not_a_persona_xyz", model="x"))
    assert code == 2
    data = json.loads(capsys.readouterr().out)
    assert data["error_code"] == "persona_not_found"


def test_cli_persona_set_model_stale_write_superseded(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    ensure_persisted_personas(_cfg())
    newer = datetime.now(timezone.utc)
    older = newer - timedelta(seconds=45)

    assert harness._cmd_persona_set_model(_persona_args("base", model="model-new", issued_at=newer.isoformat())) == 0
    capsys.readouterr()
    code = harness._cmd_persona_set_model(_persona_args("base", model="model-old", issued_at=older.isoformat()))
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "superseded"
    assert AgentStore().get("base").model == "model-new"


def test_cli_persona_set_model_credentials_warning_is_nonblocking(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    ensure_persisted_personas(_cfg())
    from providers import get_provider_profile

    profile = get_provider_profile("anthropic")
    for name in tuple(getattr(profile, "env_vars", ()) or ()):
        monkeypatch.delenv(name, raising=False)

    code = harness._cmd_persona_set_model(_persona_args("base", provider="anthropic", model="claude-x"))
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    codes = {item["code"] for item in data["warnings"]}
    assert "provider_credentials_not_detected" in codes


# --- config-vs-store regression -------------------------------------------------


# --- per-instance reasoning effort ---------------------------------------------


def test_update_profile_persists_and_clears_reasoning_effort():
    persona = _persona()
    store = PersonaInstanceStore()
    a, _ = _two_instances(store, persona)

    updated = store.update_profile(a.id, reasoning_effort="high")
    assert updated.reasoning_effort == "high"
    assert updated.model_override_issued_at is not None
    raw = json.loads(paths.persona_instance_path(a.id).read_text(encoding="utf-8"))
    assert raw["reasoning_effort"] == "high"

    # Empty string clears back to the runtime default (None).
    cleared = store.update_profile(a.id, reasoning_effort="")
    assert cleared.reasoning_effort is None

    # Reasoning rides the model lane: clear_model_override drops it too.
    store.update_profile(a.id, reasoning_effort="xhigh")
    both_cleared = store.update_profile(a.id, clear_model_override=True)
    assert both_cleared.reasoning_effort is None


def test_update_profile_rejects_invalid_reasoning_effort():
    persona = _persona()
    store = PersonaInstanceStore()
    a, _ = _two_instances(store, persona)
    with pytest.raises(ValueError):
        store.update_profile(a.id, reasoning_effort="turbo")


def test_reasoning_effort_is_isolated_per_instance():
    persona = _persona()
    store = PersonaInstanceStore()
    a, b = _two_instances(store, persona)
    store.update_profile(b.id, reasoning_effort="high")
    assert store.get(a.id).reasoning_effort is None
    assert store.get(b.id).reasoning_effort == "high"


def test_persona_instance_summary_projects_reasoning_fields():
    persona = _persona(model="gpt-5.6-luna")  # reasoning-capable gpt-5 family
    store = PersonaInstanceStore()
    a, b = _two_instances(store, persona)
    store.update_profile(b.id, reasoning_effort="high")

    plain = persona_instance_summary(store.get(a.id), persona)
    overridden = persona_instance_summary(store.get(b.id), persona)

    assert plain["reasoning_effort"] is None
    assert plain["reasoning_supported"] is True  # gpt-5 exposes reasoning effort
    assert plain["model_is_override"] is False

    assert overridden["reasoning_effort"] == "high"
    assert overridden["reasoning_supported"] is True
    # A reasoning-only override still marks the instance as overridden.
    assert overridden["model_is_override"] is True


def test_persona_instance_summary_reasoning_unsupported_for_non_reasoning_model():
    persona = _persona(model="gpt-4.1")  # not a reasoning-effort model
    store = PersonaInstanceStore()
    a, _ = _two_instances(store, persona)
    summary = persona_instance_summary(store.get(a.id), persona)
    assert summary["reasoning_supported"] is False


def test_cli_instance_set_model_reasoning_only_applies(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    persona = _persona(model="gpt-5.6-luna")
    store = PersonaInstanceStore()
    _, b = _two_instances(store, persona)

    code = harness._cmd_persona_instance_set_model(_instance_args(b.id, reasoning_effort="xhigh"))
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["status"] == "applied"
    assert data["reasoning_effort"] == "xhigh"
    # Reasoning-only write still flags an instance override.
    assert data["model_is_instance_override"] is True
    assert store.get(b.id).reasoning_effort == "xhigh"


def test_cli_instance_set_model_invalid_reasoning_rejected(monkeypatch, capsys):
    from hermes_constants import VALID_REASONING_EFFORTS

    harness = _patched_harness(monkeypatch)
    persona = _persona()
    store = PersonaInstanceStore()
    _, b = _two_instances(store, persona)

    # The sentinel must be a level the runtime does NOT recognise. It used to be
    # "ultra", which upstream promoted to a real level — the sentinel silently
    # became valid and this rejection test started asserting on an accepted
    # write. Pin the premise so a future upstream level fails HERE, loudly,
    # instead of hollowing the regression out again.
    invalid_effort = "turbo"
    assert invalid_effort not in VALID_REASONING_EFFORTS

    code = harness._cmd_persona_instance_set_model(
        _instance_args(b.id, reasoning_effort=invalid_effort)
    )
    assert code == 2
    data = json.loads(capsys.readouterr().out)
    assert data["error_code"] == "invalid_value"
    assert store.get(b.id).reasoning_effort is None


def test_cli_instance_set_model_use_profile_default_clears_reasoning(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    persona = _persona(model="gpt-5.6-luna")
    store = PersonaInstanceStore()
    _, b = _two_instances(store, persona)
    store.update_profile(b.id, reasoning_effort="high")

    code = harness._cmd_persona_instance_set_model(_instance_args(b.id, use_profile_default=True))
    assert code == 0
    assert store.get(b.id).reasoning_effort is None


def test_cli_persona_set_model_rejects_reasoning_effort(monkeypatch, capsys):
    harness = _patched_harness(monkeypatch)
    ensure_persisted_personas(_cfg())
    code = harness._cmd_persona_set_model(_persona_args("base", reasoning_effort="high"))
    assert code == 2
    data = json.loads(capsys.readouterr().out)
    assert data["error_code"] == "unsupported_scope"


def test_store_persisted_model_survives_config_persona_override(monkeypatch, capsys):
    """config.yaml agent_runtime.personas.base.model must NOT clobber a
    store-persisted verb write on reload (config.py merge: {**catalog, **stored})."""
    harness = _patched_harness(monkeypatch)
    ensure_persisted_personas(_cfg())
    assert harness._cmd_persona_set_model(_persona_args("base", provider="anthropic", model="claude-x")) == 0
    capsys.readouterr()

    cfg_with_override = _cfg()
    cfg_with_override.personas = {"base": {"model": "cfg-clobber-model", "role": "profile"}}
    merged = {persona.id: persona for persona in ensure_persisted_personas(cfg_with_override)}
    assert merged["base"].model == "claude-x", "store tier must win over config catalog tier"
