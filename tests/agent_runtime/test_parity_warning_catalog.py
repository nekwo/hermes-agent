"""B5 — every parity warning code this module can SPELL, it can also PRODUCE.

Two warnings (``open_tasks_without_task_rows``, ``open_incident_budget_exceeded``)
sat in ``_parity_warnings`` for two contract versions after the frame stopped
emitting the ``summary`` fields they read. A dict answers ``.get()`` for any key,
so both branches read ``None``, evaluated false, and simply never fired — the
Launcher shipped an "incident budget" alert for a warning that could not exist,
and the ``open_incident_warning_threshold`` knob tuned nothing.

Nothing catches that class by inspection: an unreachable branch analyses clean,
covers no lines anyone misses, and reads exactly like a working check. So the
gate is EXECUTABLE — for every code literal the module writes, a row here builds
the input that makes it appear. A code that becomes unproducible fails this test
at the line that spells it.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from agent_runtime import snapshot as snapshot_mod
from agent_runtime.snapshot import SnapshotSummary, _parity_warnings


def _runtime_on(**sections) -> dict:
    """A frame with the persona-instance runtime enabled (the gate every
    instance/channel warning sits behind)."""

    data: dict = {
        "persona_instance_runtime": {"enabled": True},
        "persona_instances": {},
        "operator_channels": {},
        "summary": SnapshotSummary(persona_instances=0).as_dict(),
    }
    data.update(sections)
    return data


def _instance(instance_id: str, **extra) -> dict:
    row = {"persona_instance_id": instance_id, "persona_id": "alice", "status": "active"}
    row.update(extra)
    return row


_NOW = datetime.now(timezone.utc)


PRODUCIBLE = {
    "orphaned_board": {"boards": {"b1": {"board_id": "b1", "workspace_id": "gone", "orphaned": True}}},
    "board_card_conflict": {"boards": {"b1": {"board_id": "b1", "conflict_card_ids": ["c1"]}}},
    "orphaned_office": {"offices": {"w1": {"workspace_id": "w1", "orphaned": True}}},
    "office_actor_conflict": {"offices": {"w1": {"workspace_id": "w1", "conflict_actor_keys": ["a1"]}}},
    "persona_instance_runtime_disabled": {"persona_instance_runtime": {"enabled": False}},
    # Two ids that canonicalize to one row (the ``persona_`` actor-token drift
    # scheme) is what ``duplicate_persona_instance_groups`` reports on.
    "duplicate_persona_instance": _runtime_on(
        persona_instances={
            "a": _instance("personainst_alice"),
            "b": _instance("persona_personainst_alice"),
        }
    ),
    "trace_persona_not_in_instances": _runtime_on(
        persona_chat_trace=[{"persona_id": "ghost", "persona_instance_id": "personainst_ghost"}]
    ),
    "persona_chat_history.non_iso_timestamp": _runtime_on(
        persona_chat_history=[{"session_id": "s1", "persona_id": "alice", "created_at": "not-a-timestamp"}]
    ),
    "persona_chat_history.live_mission_shadow": _runtime_on(
        generated_at=_NOW.isoformat(),
        persona_chat_history=[
            {
                "session_id": "chat",
                "persona_id": "alice",
                "updated_at": (_NOW - timedelta(hours=1)).isoformat(),
            },
            {
                "session_id": "mission",
                "persona_id": "alice",
                "kind": "mission",
                "updated_at": _NOW.isoformat(),
            },
        ],
    ),
    "operator_channels_missing": {
        "persona_instance_runtime": {"enabled": True},
        "persona_instances": {},
        "operator_channels": None,
    },
    "operator_channels_invalid": {
        "persona_instance_runtime": {"enabled": True},
        "persona_instances": {},
        "operator_channels": [],
    },
    # ``spawned_by`` is only read as a steering edge when it is structurally a
    # persona-instance id (``personainst_*``) — anything else is filtered, not
    # reported.
    "fk_miss": _runtime_on(
        persona_instances={
            "a": _instance("personainst_alice", spawned_by="personainst_gone"),
        }
    ),
    "operator_channel.": _runtime_on(
        operator_channels={
            "ch1": {
                "channel_id": "ch1",
                "state": "archived",
                "warnings": [{"code": "transcript_truncated", "detail": "tail evicted"}],
            }
        }
    ),
}


def _codes(data: dict) -> set[str]:
    return {str(warning.get("code") or "") for warning in _parity_warnings(data)}


@pytest.mark.parametrize("code", sorted(PRODUCIBLE))
def test_every_declared_warning_code_is_producible(code, monkeypatch):
    monkeypatch.setattr(snapshot_mod, "suspect_default_root", lambda *_a, **_k: False)
    produced = _codes(PRODUCIBLE[code])
    if code.endswith("."):
        assert any(item.startswith(code) for item in produced), (code, produced)
    else:
        assert code in produced, (code, produced)


def test_orphan_persona_instance_codes_are_producible(monkeypatch):
    """The two orphan classes come from a classifier, so they are produced by
    stubbing the classification rather than by hand-shaping identity inputs."""

    monkeypatch.setattr(
        snapshot_mod,
        "classify_orphan_persona_instances",
        lambda *_a, **_k: {
            "prunable": [{"persona_instance_id": "pi_x_0001", "reason": "profile_missing"}],
            "held": [{"persona_instance_id": "pi_y_0002", "reason": "protected"}],
        },
    )
    produced = _codes(_runtime_on(persona_instances={"a": _instance("personainst_x")}))
    assert "orphaned_persona_instance" in produced
    assert "held_orphan_persona_instance" in produced


COVERED = set(PRODUCIBLE) | {"orphaned_persona_instance", "held_orphan_persona_instance"}


def _declared_codes() -> set[str]:
    """Every literal this module assigns to a ``"code"`` key.

    f-string codes contribute their literal prefix (``operator_channel.``), which
    is how the table above covers the dynamic family.
    """

    declared: set[str] = set()
    for func in (
        snapshot_mod._parity_warnings,
        snapshot_mod._board_parity_warnings,
        snapshot_mod._office_parity_warnings,
    ):
        tree = ast.parse(inspect.getsource(func).lstrip())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant) and key.value == "code"):
                    continue
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    declared.add(value.value)
                elif isinstance(value, ast.JoinedStr):
                    head = value.values[0]
                    if isinstance(head, ast.Constant) and isinstance(head.value, str):
                        declared.add(head.value)
    return declared


def test_no_warning_code_is_declared_without_being_producible():
    declared = _declared_codes()
    assert declared, "AST walk found no warning codes — the extractor drifted"
    unproducible = declared - COVERED
    assert not unproducible, (
        "these parity warning codes are spelled but no input in this table "
        f"produces them (the open_tasks/open_incidents class): {sorted(unproducible)}"
    )


def test_retired_summary_keyed_warnings_stay_retired():
    source = inspect.getsource(snapshot_mod._parity_warnings)
    for retired in ("open_tasks_without_task_rows", "open_incident_budget_exceeded"):
        assert f'"code": "{retired}"' not in source
    assert not hasattr(SnapshotSummary, "open_tasks")
    assert not hasattr(SnapshotSummary, "open_incidents")
    with pytest.raises(TypeError):
        SnapshotSummary(persona_instances=0, open_incidents=5)  # type: ignore[call-arg]
