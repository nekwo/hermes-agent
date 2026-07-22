"""Snapshot-size audit: per-section byte accounting + duplicate-payload detection.

Stage S1 of the Mission Control read-model workstream. A repeatable, **read-only**
diagnostic that measures the live (or any) Mission Control snapshot's per-section
byte cost, per-row averages, largest-row field breakdown, and cross-tree duplicate
payloads — so every later stage (S2-S7) can prove its byte win and CI can catch a
size regression.

Bytes are measured exactly the way the snapshot itself reports ``snapshot_bytes``
(:func:`agent_runtime.snapshot._snapshot_payload_size`): normalize with
:func:`agent_runtime.serde.to_jsonable`, then compact-encode with
``json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`` and
count UTF-8 bytes. That makes an audit total comparable to the parity envelope's own
``snapshot_bytes`` figure.

Discipline (hard requirements for this module):
  * NEVER writes to any store, NEVER mutates the snapshot, NEVER requires the serve
    loop. ``--live`` builds the snapshot in-process from the ambient runtime root via
    :func:`agent_runtime.snapshot.build_snapshot` (a read-only projection).
  * Pure functions (:func:`audit_snapshot`, :func:`snapshot_size_budget`,
    :func:`compact_bytes`) take a plain snapshot ``dict`` and return plain data — no
    I/O, no globals.

Baseline (2026-07-16 live measurement, for reference — the live store drifts):
  TOTAL ~8,951,618 B. Biggest sections:
    * ``prompt_observability`` 32.8% (``chat_contexts`` n=38; the skills *catalog*
      payload ~19 KB is byte-identical across rows ×11/×8 — one global stored ~76×,
      exactly the class :func:`audit_snapshot` duplicate detection surfaces).
    * ``archived_tasks`` 14.2% (25 dead tasks).
    * ``tasks`` 559 KB n=7; ``incidents`` 417 KB n=1914; ``goals`` 412 KB n=7 (the
      same entities as ``tasks``, projected a second time).

This module lives on fork-owned surface (``agent_runtime/**``). It is a
``python -m agent_runtime.snapshot_audit`` entry point, NOT a CLI subcommand —
``hermes_cli/harness.py`` is owned elsewhere and must not be touched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from typing import Any

from .serde import to_jsonable

# Minimum per-value size for a repeated list/dict payload to count as a duplicate.
# 4 KiB matches the S1 brief: below this the md5/accounting overhead outweighs any
# realistic byte win from de-duplication.
DUP_MIN_BYTES = 4096

# Default field count for the largest-row breakdown of a list section.
DEFAULT_TOP_FIELDS = 12

# Generous starting budgets (bytes). All comfortably above the 2026-07-16 live
# measurement so nothing trips today; S2+ ratchet these DOWN as sections shrink and
# a CI gate can start consuming :func:`snapshot_size_budget`.
DEFAULT_SIZE_BUDGETS: dict[str, int] = {
    "total": 12 * 1024 * 1024,          # ~12.0 MiB  (live ~8.5-9 MiB)
    "prompt_observability": 5 * 1024 * 1024,
    "archived_tasks": 3 * 1024 * 1024,
    "tasks": 2 * 1024 * 1024,
    "goals": 2 * 1024 * 1024,
    "incidents": 2 * 1024 * 1024,
    "operator_channels": 2 * 1024 * 1024,
    "persona_chat_history": 2 * 1024 * 1024,
    "persona_chat_trace": 2 * 1024 * 1024,
}


# --------------------------------------------------------------------------- #
# Byte measurement (identical to snapshot._snapshot_payload_size).
# --------------------------------------------------------------------------- #
def _json_str(jsonable: Any) -> str:
    """Compact, deterministic JSON string for an already-jsonable value."""
    return json.dumps(jsonable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_bytes(jsonable: Any) -> int:
    """UTF-8 byte length of the compact JSON encoding of an already-jsonable value."""
    return len(_json_str(jsonable).encode("utf-8"))


def compact_bytes(value: Any) -> int:
    """Compact-JSON UTF-8 byte cost of ``value`` (normalized via ``to_jsonable``).

    This is the public, snapshot-agnostic entry — it normalizes first, so it accepts a
    raw snapshot fragment carrying datetimes / dataclasses / enums. Internally the
    audit converts the whole snapshot once and uses :func:`_json_bytes` on the
    already-jsonable sub-values to avoid re-converting.
    """
    return _json_bytes(to_jsonable(value))


def _kind(value: Any) -> str:
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return "scalar"


def _count(value: Any) -> int | None:
    if isinstance(value, (list, dict)):
        return len(value)
    return None


def _per_row_avg(byte_cost: int, count: int | None) -> int | None:
    if not count:
        return None
    return byte_cost // count


def _pct(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return round(part / whole * 100.0, 2)


# --------------------------------------------------------------------------- #
# Duplicate-payload detection.
# --------------------------------------------------------------------------- #
def _collect_duplicates(jsonable: Any, min_bytes: int) -> list[dict[str, Any]]:
    """Find list/dict field values >``min_bytes`` whose md5 digest repeats anywhere.

    Walks the (already-jsonable) tree. A *field* is a dict key; its value is a
    candidate when it is a list/dict larger than ``min_bytes``. Byte-identical
    payloads (same canonical compact JSON) share an md5 digest even under different
    field names or in different rows — that is how one global skills catalog stored
    into every chat-context row surfaces as a single high-``total_wasted`` finding.
    """
    # digest -> {"names": Counter, "bytes": int, "occurrences": int}
    acc: dict[str, dict[str, Any]] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if isinstance(val, (dict, list)):
                    byte_cost = _json_bytes(val)
                    if byte_cost > min_bytes:
                        digest = hashlib.md5(_json_str(val).encode("utf-8")).hexdigest()
                        entry = acc.get(digest)
                        if entry is None:
                            entry = {"names": Counter(), "bytes": byte_cost, "occurrences": 0}
                            acc[digest] = entry
                        entry["names"][str(key)] += 1
                        entry["occurrences"] += 1
                    visit(val)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    visit(item)

    visit(jsonable)

    duplicates: list[dict[str, Any]] = []
    for digest, entry in acc.items():
        occurrences = entry["occurrences"]
        if occurrences <= 1:
            continue
        byte_cost = entry["bytes"]
        names: Counter = entry["names"]
        duplicates.append(
            {
                "field": names.most_common(1)[0][0],
                "field_names": dict(names),
                "digest": digest,
                "occurrences": occurrences,
                "bytes": byte_cost,
                "total_wasted": (occurrences - 1) * byte_cost,
            }
        )
    duplicates.sort(key=lambda row: row["total_wasted"], reverse=True)
    return duplicates


# --------------------------------------------------------------------------- #
# Per-section accounting.
# --------------------------------------------------------------------------- #
def _dict_drilldown(section: dict[str, Any], section_bytes: int) -> list[dict[str, Any]]:
    """One-level breakdown of a dict section (e.g. prompt_observability.chat_contexts)."""
    rows: list[dict[str, Any]] = []
    for key, val in section.items():
        byte_cost = _json_bytes(val)
        count = _count(val)
        rows.append(
            {
                "key": str(key),
                "bytes": byte_cost,
                "pct_of_section": _pct(byte_cost, section_bytes),
                "kind": _kind(val),
                "count": count,
                "per_row_avg": _per_row_avg(byte_cost, count),
            }
        )
    rows.sort(key=lambda row: row["bytes"], reverse=True)
    return rows


def _largest_row_breakdown(section: list[Any], top_fields: int) -> dict[str, Any] | None:
    """Top-``top_fields`` field breakdown of the single largest row in a list section."""
    if not section:
        return None
    largest_index = -1
    largest_bytes = -1
    largest_val: Any = None
    for index, item in enumerate(section):
        byte_cost = _json_bytes(item)
        if byte_cost > largest_bytes:
            largest_bytes = byte_cost
            largest_index = index
            largest_val = item
    fields: list[dict[str, Any]] = []
    if isinstance(largest_val, dict):
        field_rows = []
        for key, val in largest_val.items():
            byte_cost = _json_bytes(val)
            field_rows.append(
                {
                    "field": str(key),
                    "bytes": byte_cost,
                    "pct_of_row": _pct(byte_cost, largest_bytes),
                }
            )
        field_rows.sort(key=lambda row: row["bytes"], reverse=True)
        fields = field_rows[:top_fields]
    return {"index": largest_index, "bytes": largest_bytes, "fields": fields}


def audit_snapshot(
    snap: dict,
    *,
    top_fields: int = DEFAULT_TOP_FIELDS,
    dup_min_bytes: int = DUP_MIN_BYTES,
) -> dict:
    """Measure a snapshot's per-section byte cost + duplicate payloads.

    Pure and read-only: the snapshot is normalized once via ``to_jsonable`` (which
    builds fresh containers), so ``snap`` is never mutated.

    Returns a plain, JSON-serializable dict:
      * ``total_bytes`` — compact-JSON byte total (matches parity ``snapshot_bytes``).
      * ``sections`` — one row per top-level key, sorted by bytes desc, each with
        ``{key, bytes, pct, kind, count, per_row_avg}`` plus, for dict sections, a
        one-level ``drilldown``, and for list sections a ``largest_row`` field
        breakdown (top ``top_fields`` fields of the biggest row).
      * ``duplicates`` — list/dict field values >``dup_min_bytes`` whose md5 digest
        repeats: ``{field, field_names, digest, occurrences, bytes, total_wasted}``,
        sorted by ``total_wasted`` desc.
      * ``duplicate_wasted_total`` — sum of ``total_wasted`` across duplicates.
      * ``params`` — the thresholds used, for reproducibility.
    """
    if not isinstance(snap, dict):
        raise TypeError(f"audit_snapshot expects a snapshot dict, got {type(snap).__name__}")

    jsonable = to_jsonable(snap)
    total_bytes = _json_bytes(jsonable)

    sections: list[dict[str, Any]] = []
    for key, val in jsonable.items():
        byte_cost = _json_bytes(val)
        count = _count(val)
        row: dict[str, Any] = {
            "key": str(key),
            "bytes": byte_cost,
            "pct": _pct(byte_cost, total_bytes),
            "kind": _kind(val),
            "count": count,
            "per_row_avg": _per_row_avg(byte_cost, count),
            "drilldown": None,
            "largest_row": None,
        }
        if isinstance(val, dict):
            row["drilldown"] = _dict_drilldown(val, byte_cost)
        elif isinstance(val, list):
            row["largest_row"] = _largest_row_breakdown(val, top_fields)
        sections.append(row)
    sections.sort(key=lambda item: item["bytes"], reverse=True)

    duplicates = _collect_duplicates(jsonable, dup_min_bytes)

    return {
        "total_bytes": total_bytes,
        "sections": sections,
        "duplicates": duplicates,
        "duplicate_wasted_total": sum(dup["total_wasted"] for dup in duplicates),
        "params": {"dup_min_bytes": dup_min_bytes, "top_fields": top_fields},
    }


# --------------------------------------------------------------------------- #
# Size-budget regression seam (NOT wired into CI yet — S2+ ratchets budgets down).
# --------------------------------------------------------------------------- #
def snapshot_size_budget(snap: dict, budgets: dict[str, int] | None = None) -> list[str]:
    """Return a list of budget-violation strings (empty = within budget).

    ``budgets`` maps a budget key to a max byte count and overrides
    :data:`DEFAULT_SIZE_BUDGETS`. Keys: ``"total"`` (checked against
    ``audit["total_bytes"]``) and any top-level section name (checked against that
    section's measured bytes). A budget for a section absent from the snapshot is
    silently skipped — a missing section cannot regress.

    Deliberately NOT wired into any CI gate at S1. It is the seam S2+ will tighten as
    sections shrink; today the defaults are generous and this returns ``[]`` on the
    live snapshot.
    """
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


# --------------------------------------------------------------------------- #
# Snapshot loading (file / live).
# --------------------------------------------------------------------------- #
def load_snapshot(path: str) -> dict:
    """Load a snapshot dict from a JSON file (read-only)."""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object at the top level, got {type(data).__name__}")
    return data


def build_live_snapshot() -> dict:
    """Build the live snapshot in-process from the ambient runtime root.

    Read-only projection: :func:`agent_runtime.snapshot.build_snapshot` lists stores
    and returns a fresh dict; it never mutates the runtime. Imported lazily so the
    pure audit functions carry no snapshot-builder dependency.
    """
    from .snapshot import build_snapshot

    return build_snapshot()


# --------------------------------------------------------------------------- #
# Human table rendering.
# --------------------------------------------------------------------------- #
def _human_bytes(byte_cost: int) -> str:
    mib = byte_cost / (1024 * 1024)
    if mib >= 1.0:
        return f"{mib:.2f} MiB"
    kib = byte_cost / 1024
    return f"{kib:.1f} KiB"


def _fmt_count(count: int | None) -> str:
    return f"n={count}" if count is not None else "—"


def _fmt_avg(avg: int | None) -> str:
    return f"{avg:,}" if avg is not None else "—"


def format_audit_table(
    audit: dict,
    *,
    source: str | None = None,
    max_sections: int = 20,
    max_duplicates: int = 20,
) -> str:
    """Render an :func:`audit_snapshot` result as a human-readable table."""
    total = audit["total_bytes"]
    lines: list[str] = []
    header = f"Snapshot audit — total {total:,} B ({_human_bytes(total)})"
    if source:
        header += f"  [source: {source}]"
    lines.append(header)
    lines.append("=" * max(len(header), 78))
    lines.append(f"{'SECTION':<30}{'BYTES':>14}{'PCT':>8}{'COUNT':>10}{'PER-ROW':>12}")
    lines.append("-" * 78)

    sections = audit["sections"]
    for row in sections[:max_sections]:
        lines.append(
            f"{row['key']:<30}{row['bytes']:>14,}{row['pct']:>7.2f}%"
            f"{_fmt_count(row['count']):>10}{_fmt_avg(row['per_row_avg']):>12}"
        )
        # Dict drill-down (top 3 children) — e.g. prompt_observability.chat_contexts.
        if row.get("drilldown"):
            for child in row["drilldown"][:3]:
                label = f"  ├ {child['key']}"
                lines.append(
                    f"{label:<30}{child['bytes']:>14,}{'':>8}"
                    f"{_fmt_count(child['count']):>10}{_fmt_avg(child['per_row_avg']):>12}"
                )
        # List largest-row field breakdown (top 3 fields of the biggest row).
        elif row.get("largest_row") and row["largest_row"].get("fields"):
            lr = row["largest_row"]
            for field in lr["fields"][:3]:
                label = f"  ├ [{lr['index']}].{field['field']}"
                lines.append(
                    f"{label:<30}{field['bytes']:>14,}{field['pct_of_row']:>7.1f}%{'':>10}{'':>12}"
                )
    if len(sections) > max_sections:
        lines.append(f"... {len(sections) - max_sections} smaller sections omitted")

    lines.append("")
    duplicates = audit["duplicates"]
    if not duplicates:
        lines.append(f"DUPLICATE PAYLOADS (> {audit['params']['dup_min_bytes']:,} B, appearing >1×): none")
    else:
        wasted = audit["duplicate_wasted_total"]
        lines.append(
            f"DUPLICATE PAYLOADS (> {audit['params']['dup_min_bytes']:,} B, appearing >1×) "
            f"— {len(duplicates)} finding(s), {wasted:,} B wasted ({_human_bytes(wasted)})"
        )
        lines.append(f"{'FIELD':<24}{'DIGEST':>12}{'OCC':>6}{'BYTES':>12}{'WASTED':>14}")
        for dup in duplicates[:max_duplicates]:
            lines.append(
                f"{dup['field']:<24}{dup['digest'][:10]:>12}{dup['occurrences']:>6}"
                f"{dup['bytes']:>12,}{dup['total_wasted']:>14,}"
            )
        if len(duplicates) > max_duplicates:
            lines.append(f"... {len(duplicates) - max_duplicates} smaller duplicates omitted")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# python -m agent_runtime.snapshot_audit entry point.
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent_runtime.snapshot_audit",
        description="Read-only snapshot-size audit: per-section byte accounting + duplicate detection.",
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("path", nargs="?", help="path to a snapshot .json file to audit")
    src.add_argument(
        "--live",
        action="store_true",
        help="build the snapshot in-process from the ambient runtime root (read-only)",
    )
    parser.add_argument("--json", action="store_true", help="emit the full audit dict as JSON")
    parser.add_argument(
        "--top", type=int, default=DEFAULT_TOP_FIELDS, help="fields in the largest-row breakdown (default %(default)s)"
    )
    parser.add_argument(
        "--dup-min-bytes",
        type=int,
        default=DUP_MIN_BYTES,
        help="minimum payload size to count as a duplicate (default %(default)s)",
    )
    parser.add_argument(
        "--budget",
        action="store_true",
        help="also print size-budget violations (default budgets are generous)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.live:
        snap = build_live_snapshot()
        source = "live"
    else:
        snap = load_snapshot(args.path)
        source = args.path

    audit = audit_snapshot(snap, top_fields=args.top, dup_min_bytes=args.dup_min_bytes)

    if args.json:
        print(json.dumps(audit, ensure_ascii=False, indent=2))
    else:
        print(format_audit_table(audit, source=source))

    if args.budget:
        violations = snapshot_size_budget(snap)
        print("")
        if violations:
            print(f"SIZE-BUDGET VIOLATIONS ({len(violations)}):")
            for violation in violations:
                print(f"  ✗ {violation}")
        else:
            print("SIZE-BUDGET: within all budgets ✓")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
