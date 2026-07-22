"""Pure unit tests for the S1 snapshot-size audit.

Synthetic snapshots with sections of known sizes, a planted duplicate payload, and
per-row averages — plus one cheap smoke over a real ``build_snapshot`` output. The
pure tests are the requirement; the smoke only proves the audit survives a real
(empty-store) snapshot shape.
"""

from __future__ import annotations

import copy
import json

import pytest

from agent_runtime.snapshot_audit import (
    DEFAULT_SIZE_BUDGETS,
    audit_snapshot,
    compact_bytes,
    snapshot_size_budget,
)


def _section(audit: dict, key: str) -> dict:
    for row in audit["sections"]:
        if row["key"] == key:
            return row
    raise AssertionError(f"section {key!r} not found in audit")


# --------------------------------------------------------------------------- #
# Byte accounting.
# --------------------------------------------------------------------------- #
def test_total_bytes_matches_compact_encoding():
    snap = {"a": [1, 2, 3], "b": {"x": "y"}}
    audit = audit_snapshot(snap)
    assert audit["total_bytes"] == compact_bytes(snap)
    # And that equals a plain compact json encoding for an already-jsonable snap.
    expected = len(json.dumps(snap, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    assert audit["total_bytes"] == expected


def test_section_bytes_and_pct_sum_reasonably():
    payload_a = ["alpha"] * 100
    payload_b = {"k": "v"}
    snap = {"a": payload_a, "b": payload_b}
    audit = audit_snapshot(snap)

    sec_a = _section(audit, "a")
    sec_b = _section(audit, "b")
    assert sec_a["bytes"] == compact_bytes(payload_a)
    assert sec_b["bytes"] == compact_bytes(payload_b)
    # 'a' dominates, so it sorts first and carries most of the percentage.
    assert audit["sections"][0]["key"] == "a"
    assert sec_a["pct"] > sec_b["pct"]
    # pct is bytes/total, rounded to 2dp.
    assert sec_a["pct"] == round(sec_a["bytes"] / audit["total_bytes"] * 100, 2)


def test_per_row_avg_for_list_section():
    row = {"id": 1, "blob": "x" * 40}
    rows = [dict(row, id=i) for i in range(8)]
    snap = {"items": rows}
    audit = audit_snapshot(snap)

    sec = _section(audit, "items")
    assert sec["kind"] == "list"
    assert sec["count"] == 8
    assert sec["per_row_avg"] == sec["bytes"] // 8


def test_scalar_section_has_no_count_or_avg():
    snap = {"schema_version": 2, "generated_at": "2026-07-16T00:00:00Z"}
    audit = audit_snapshot(snap)
    sec = _section(audit, "schema_version")
    assert sec["kind"] == "scalar"
    assert sec["count"] is None
    assert sec["per_row_avg"] is None
    assert sec["drilldown"] is None
    assert sec["largest_row"] is None


# --------------------------------------------------------------------------- #
# Dict drill-down + list largest-row breakdown.
# --------------------------------------------------------------------------- #
def test_dict_section_drilldown_one_level():
    snap = {
        "prompt_observability": {
            "chat_contexts": [{"i": i, "pad": "y" * 30} for i in range(5)],
            "surface_prompt_default": "hi",
        }
    }
    audit = audit_snapshot(snap)
    sec = _section(audit, "prompt_observability")
    assert sec["kind"] == "dict"
    assert sec["drilldown"] is not None
    top = sec["drilldown"][0]
    assert top["key"] == "chat_contexts"
    assert top["kind"] == "list"
    assert top["count"] == 5
    assert top["per_row_avg"] == top["bytes"] // 5
    # pct_of_section is measured against the SECTION bytes, not the total.
    assert top["pct_of_section"] == round(top["bytes"] / sec["bytes"] * 100, 2)


def test_list_section_largest_row_field_breakdown():
    rows = [
        {"id": 0, "small": "a"},
        {"id": 1, "big": "z" * 500, "small": "b"},  # index 1 is largest
        {"id": 2, "small": "c"},
    ]
    snap = {"tasks": rows}
    audit = audit_snapshot(snap, top_fields=2)
    sec = _section(audit, "tasks")
    lr = sec["largest_row"]
    assert lr is not None
    assert lr["index"] == 1
    # top_fields=2 caps the field list; the 'big' field dominates the row.
    assert len(lr["fields"]) == 2
    assert lr["fields"][0]["field"] == "big"
    assert lr["fields"][0]["bytes"] == compact_bytes("z" * 500)
    assert lr["fields"][0]["pct_of_row"] == round(lr["fields"][0]["bytes"] / lr["bytes"] * 100, 2)


# --------------------------------------------------------------------------- #
# Duplicate detection.
# --------------------------------------------------------------------------- #
def _big_catalog() -> dict:
    # A FLAT dict payload comfortably over the 4 KiB duplicate threshold — flat
    # (scalar values only) so the catalog is the single duplicate level, with no
    # nested >4 KiB list/dict field registering as its own finding.
    catalog = {f"skill_{i}": "s" * 50 for i in range(120)}
    assert compact_bytes(catalog) > 4096
    return catalog


def test_planted_duplicate_detected_with_waste_accounting():
    catalog = _big_catalog()
    # Same catalog embedded in 5 otherwise-distinct rows (distinct so only the
    # catalog — not the rows — registers as a duplicate).
    rows = [{"id": i, "catalog": copy.deepcopy(catalog)} for i in range(5)]
    snap = {"chat_contexts": rows}
    audit = audit_snapshot(snap)

    dups = audit["duplicates"]
    assert len(dups) == 1
    dup = dups[0]
    assert dup["field"] == "catalog"
    assert dup["occurrences"] == 5
    assert dup["bytes"] == compact_bytes(catalog)
    assert dup["total_wasted"] == (5 - 1) * dup["bytes"]
    assert audit["duplicate_wasted_total"] == dup["total_wasted"]
    # md5 hex digest.
    assert len(dup["digest"]) == 32


def test_duplicate_below_threshold_is_ignored():
    small = {"skills": ["a", "b", "c"]}  # well under 4 KiB
    assert compact_bytes(small) < 4096
    rows = [{"id": i, "catalog": copy.deepcopy(small)} for i in range(6)]
    snap = {"chat_contexts": rows}
    audit = audit_snapshot(snap)
    assert audit["duplicates"] == []
    assert audit["duplicate_wasted_total"] == 0


def test_nested_duplicate_payload_is_detected():
    # The real skills-catalog class: a >4 KiB list nested inside a per-row dict,
    # repeated across rows. Both the wrapper dict AND the inner list are duplicated,
    # and the audit reports each level it can de-duplicate.
    inner = {"skills": ["skill-" + "s" * 50 for _ in range(120)]}
    assert compact_bytes(inner["skills"]) > 4096
    rows = [{"id": i, "catalog": copy.deepcopy(inner)} for i in range(4)]
    audit = audit_snapshot({"chat_contexts": rows})
    fields = {dup["field"] for dup in audit["duplicates"]}
    assert {"catalog", "skills"} <= fields
    for dup in audit["duplicates"]:
        assert dup["occurrences"] == 4
        assert dup["total_wasted"] == 3 * dup["bytes"]


def test_duplicate_across_different_field_names_shares_one_digest():
    catalog = _big_catalog()
    snap = {
        "rows": [
            {"id": 0, "catalog": copy.deepcopy(catalog)},
            {"id": 1, "catalog": copy.deepcopy(catalog)},
            {"id": 2, "skills_catalog": copy.deepcopy(catalog)},
        ]
    }
    audit = audit_snapshot(snap)
    assert len(audit["duplicates"]) == 1
    dup = audit["duplicates"][0]
    assert dup["occurrences"] == 3
    assert dup["field"] == "catalog"  # most common name
    assert dup["field_names"] == {"catalog": 2, "skills_catalog": 1}


def test_custom_dup_min_bytes_threshold():
    payload = {f"k{i}": "r" * 20 for i in range(30)}  # ~700 B, flat
    byte_cost = compact_bytes(payload)
    assert byte_cost < 4096
    snap = {"a": copy.deepcopy(payload), "b": copy.deepcopy(payload)}
    # Default threshold ignores it; a low threshold catches it.
    assert audit_snapshot(snap)["duplicates"] == []
    caught = audit_snapshot(snap, dup_min_bytes=100)["duplicates"]
    assert len(caught) == 1
    assert caught[0]["occurrences"] == 2
    assert caught[0]["field_names"] == {"a": 1, "b": 1}


# --------------------------------------------------------------------------- #
# Read-only discipline.
# --------------------------------------------------------------------------- #
def test_audit_does_not_mutate_snapshot():
    snap = {
        "chat_contexts": [{"id": i, "catalog": _big_catalog()} for i in range(3)],
        "schema_version": 2,
    }
    before = copy.deepcopy(snap)
    audit_snapshot(snap)
    assert snap == before


def test_audit_snapshot_rejects_non_dict():
    with pytest.raises(TypeError):
        audit_snapshot([1, 2, 3])  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Size-budget regression seam.
# --------------------------------------------------------------------------- #
def test_size_budget_within_defaults_is_clean():
    snap = {"tasks": [{"id": i} for i in range(3)], "schema_version": 2}
    assert snapshot_size_budget(snap) == []


def test_size_budget_flags_section_over_budget():
    snap = {"tasks": [{"id": i, "pad": "p" * 100} for i in range(20)]}
    tasks_bytes = _section(audit_snapshot(snap), "tasks")["bytes"]
    violations = snapshot_size_budget(snap, {"tasks": 10})
    assert len(violations) == 1
    assert violations[0].startswith("tasks:")
    assert f"{tasks_bytes:,} B" in violations[0]
    assert "exceeds budget 10 B" in violations[0]


def test_size_budget_flags_total_over_budget():
    snap = {"blob": ["x" * 100 for _ in range(50)]}
    total = audit_snapshot(snap)["total_bytes"]
    violations = snapshot_size_budget(snap, {"total": 5})
    assert any(v.startswith("total:") and f"{total:,} B" in v for v in violations)


def test_size_budget_skips_absent_sections():
    # 'incidents' is a default-budgeted section but absent here -> no violation,
    # no crash.
    snap = {"schema_version": 2}
    assert "incidents" in DEFAULT_SIZE_BUDGETS
    assert snapshot_size_budget(snap) == []


# --------------------------------------------------------------------------- #
# Smoke over a real (empty-store) build_snapshot output.
# --------------------------------------------------------------------------- #
def test_audit_handles_real_build_snapshot(isolate_agent_runtime_root):
    from agent_runtime.snapshot import build_snapshot

    snap = build_snapshot()  # empty isolated root -> cheap
    audit = audit_snapshot(snap)

    assert audit["total_bytes"] > 0
    keys = {row["key"] for row in audit["sections"]}
    assert "schema_version" in keys
    assert "prompt_observability" in keys
    # Every section is a real slice of the whole.
    for row in audit["sections"]:
        assert 0 < row["bytes"] <= audit["total_bytes"]
    # Budget seam runs against the real shape and stays clean on an empty store.
    assert snapshot_size_budget(snap) == []
