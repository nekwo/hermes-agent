"""S39 residue coverage for retiring fresh-row ``mission_hud`` writes."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from agent_runtime.prompt_observability import (
    _backfill_derived_fields,
    mission_chat_prompt_observability,
)


def test_fresh_prompt_rows_have_no_mission_hud_writer_or_key() -> None:
    assert "mission_hud" not in inspect.signature(
        mission_chat_prompt_observability
    ).parameters

    row = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="dev", hermes_profile="dev", display_name="Dev", role="dev"
        )
    )
    assert "mission_hud" not in row


def test_backfill_preserves_history_without_manufacturing_fresh_mission_hud() -> None:
    historical = {"mission_hud": {"phase": "historical"}}
    _backfill_derived_fields(
        historical,
        {"mission_hud": {"phase": "fresh-preview"}},
    )
    assert historical["mission_hud"] == {"phase": "historical"}

    fresh: dict[str, object] = {}
    _backfill_derived_fields(
        fresh,
        {"mission_hud": {"phase": "fresh-preview"}},
    )
    assert "mission_hud" not in fresh
