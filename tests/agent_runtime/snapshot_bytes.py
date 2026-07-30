"""Snapshot byte accounting for the size-budget ratchet tests.

Formerly ``agent_runtime/snapshot_audit.py``, a read-only diagnostic with a
``python -m`` entry point and zero production importers. S13 retired the module;
the *measurement* it provided is still the instrument three surviving tests use to
prove a slimming win, so the pure functions those tests call move here verbatim.

Bytes are measured exactly the way the snapshot reports ``snapshot_bytes``
(:func:`agent_runtime.snapshot._snapshot_payload_size`): normalize with
:func:`agent_runtime.serde.to_jsonable`, then compact-encode with
``json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`` and
count UTF-8 bytes — so a measurement here stays comparable to the parity envelope.

Pure: no I/O, no globals, never mutates the snapshot.
"""

from __future__ import annotations

import json
from typing import Any

from agent_runtime.serde import to_jsonable

# Generous ceilings inherited from the retired module. Tests ratchet these DOWN
# through the ``budgets`` parameter; a budget for a section absent from the
# snapshot is skipped, because a missing section cannot regress.
DEFAULT_SIZE_BUDGETS: dict[str, int] = {
    "total": 12 * 1024 * 1024,
    "prompt_observability": 5 * 1024 * 1024,
    "operator_channels": 2 * 1024 * 1024,
    "persona_chat_history": 2 * 1024 * 1024,
    "persona_chat_trace": 2 * 1024 * 1024,
}


def _json_bytes(jsonable: Any) -> int:
    """UTF-8 byte length of the compact JSON encoding of an already-jsonable value."""
    return len(
        json.dumps(jsonable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def compact_bytes(value: Any) -> int:
    """Compact-JSON UTF-8 byte cost of ``value`` (normalized via ``to_jsonable``)."""
    return _json_bytes(to_jsonable(value))


def audit_snapshot(snap: dict) -> dict:
    """Per-section byte accounting for a snapshot dict.

    Returns ``{"total_bytes": int, "sections": [{"key", "bytes"}, ...]}`` sorted by
    bytes descending.
    """
    if not isinstance(snap, dict):
        raise TypeError(f"audit_snapshot expects a snapshot dict, got {type(snap).__name__}")

    jsonable = to_jsonable(snap)
    sections = [{"key": str(key), "bytes": _json_bytes(val)} for key, val in jsonable.items()]
    sections.sort(key=lambda item: item["bytes"], reverse=True)
    return {"total_bytes": _json_bytes(jsonable), "sections": sections}


def snapshot_size_budget(snap: dict, budgets: dict[str, int] | None = None) -> list[str]:
    """Return budget-violation strings (empty = within budget)."""

    effective = {**DEFAULT_SIZE_BUDGETS, **(budgets or {})}
    audit = audit_snapshot(snap)
    section_bytes = {row["key"]: row["bytes"] for row in audit["sections"]}

    violations: list[str] = []
    for name, limit in effective.items():
        if name == "total":
            measured = audit["total_bytes"]
        elif name in section_bytes:
            measured = section_bytes[name]
        else:
            continue
        if measured > limit:
            violations.append(
                f"{name}: {measured:,} B exceeds budget {limit:,} B "
                f"(over by {measured - limit:,} B)"
            )
    return violations
