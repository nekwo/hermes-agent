"""Cross-surface contract: the harness runtime default == the top-level
``model.default`` the user sets, unless an explicit ``agent_runtime.*`` override
is present. Guards against the "dropdown says luna, agents run gpt-5.5" split.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime.config import load_agent_runtime_config, persona_records_from_config
from agent_runtime.snapshot import build_snapshot


def _point_config_at(monkeypatch, path):
    # load_agent_runtime_config / describe_runtime_default_authority both read
    # get_config_path() from the config module namespace; snapshot.build_snapshot
    # loads the config with no path, so this one patch drives every surface.
    monkeypatch.setattr("agent_runtime.config.get_config_path", lambda: path)


def test_snapshot_runtime_default_follows_top_level_model(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text(
        "model:\n"
        "  default: gpt-5.6-luna\n"
        "  provider: openai-codex\n"
        "agent_runtime:\n"
        "  personas:\n"
        "    neko_supervisor:\n"
        "      role: custom-lead\n",
        encoding="utf-8",
    )
    _point_config_at(monkeypatch, p)

    snapshot = build_snapshot()

    assert snapshot["runtime_default"] == {
        "model": "gpt-5.6-luna",
        "provider": "openai-codex",
        "api_mode": "codex_responses",
        "model_source": "model.default",
        "provider_source": "model.provider",
    }
    # The read-only config-show surface agrees.
    assert snapshot["runtime_config"]["default_model"] == "gpt-5.6-luna"
    assert snapshot["runtime_config"]["default_model_source"] == "model.default"


def test_snapshot_runtime_default_reports_explicit_override(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text(
        "model:\n"
        "  default: gpt-5.6-luna\n"
        "agent_runtime:\n"
        "  default_model: gpt-5.5\n",
        encoding="utf-8",
    )
    _point_config_at(monkeypatch, p)

    snapshot = build_snapshot()

    assert snapshot["runtime_default"]["model"] == "gpt-5.5"
    assert snapshot["runtime_default"]["model_source"] == "agent_runtime.default_model"
    # And the validation surface warns that the override shadows the user default.
    warnings = snapshot["runtime_config"]["validation"].get("warnings") or []
    assert any(w["field"] == "agent_runtime.default_model" for w in warnings)


def test_unpinned_catalog_persona_resolves_to_top_level_default(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text(
        "model:\n"
        "  default: gpt-5.6-luna\n"
        "  provider: openai-codex\n"
        "agent_runtime:\n"
        "  personas:\n"
        "    neko_supervisor:\n"
        "      role: custom-lead\n",
        encoding="utf-8",
    )
    _point_config_at(monkeypatch, p)

    cfg = load_agent_runtime_config()
    neko = next(persona for persona in persona_records_from_config(cfg) if persona.id == "neko_supervisor")

    # The persona-turn cascade bottoms out at cfg.default_model — with no pin it
    # follows the user's top-level default, which is the whole point of the fix.
    assert neko.model == "gpt-5.6-luna"
