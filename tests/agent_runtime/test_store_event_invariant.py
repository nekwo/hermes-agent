"""Stage 12 B2 — CI-enforced store/event coupling invariant.

The stream/read-model pipeline is watermark-gated on the EventLog offset:
a store mutation that appends no event is INVISIBLE to every consumer
(launcher snapshot, serve read model) until an unrelated event moves the
offset. That class recurred three times (realm adopt → realm sync →
realm/workspace use) while emission was left to caller convention.

This test makes the convention structural: every function in
``agent_runtime/store.py`` that writes store state must either couple an
event append in its own body or be explicitly classified below with a
written justification. A new write path fails CI until consciously
classified. See docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/12-read-path-freshness-hardening.md.
"""

from __future__ import annotations

import ast
from pathlib import Path

import agent_runtime.store as store_module

# Write primitives whose presence marks a function as a store writer.
_WRITE_CALLS = {"_write_model", "atomic_json_write"}
# Calls that count as coupling an event to the mutation.
_EVENT_CALLS = {"append", "_append_store_event"}

# Functions allowed to write WITHOUT appending an event, each with the reason
# the exemption is sound. Adding a name here is a reviewed decision, not a
# default — the justification must explain why consumers still converge.
EXEMPT: dict[str, str] = {
    # The JSON-write primitive itself; every store writer funnels through it.
    "<module>._write_model": "write primitive — coupling is enforced on its callers",
    # Bare field refresh on a run row. S17 removed the evented RunStore writers
    # that had no production callers left (open_run/heartbeat/
    # approve_continuation), and S33 retired the caller-free
    # persona_runtime._attach_repo_baseline path proven dead in a54e802cd.
}


def _qualified_writers() -> dict[str, bool]:
    """Map '<Class>.<func>' → 'couples an event append' for every writer."""

    source = Path(store_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    writers: dict[str, bool] = {}

    def call_name(node: ast.Call) -> str | None:
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                writes = False
                appends = False
                for sub in ast.walk(child):
                    if isinstance(sub, ast.Call):
                        name = call_name(sub)
                        if name in _WRITE_CALLS:
                            writes = True
                        if name in _EVENT_CALLS:
                            appends = True
                if writes:
                    writers[f"{owner}.{child.name}"] = appends
                visit(child, owner)

    visit(tree, "<module>")
    return writers


def test_every_store_writer_couples_an_event_or_is_classified():
    writers = _qualified_writers()
    assert writers, "AST scan found no store writers — the scan itself broke"

    unclassified = sorted(
        name for name, appends in writers.items() if not appends and name not in EXEMPT
    )
    assert not unclassified, (
        "Store writer(s) with NO event append and no reviewed exemption: "
        f"{unclassified}. An event-less store mutation is invisible to the "
        "watermark-gated stream/read-model pipeline (Stage 12). Either emit "
        "via _append_store_event inside the mutator, or add a justified "
        "EXEMPT entry in this test."
    )


def test_exemptions_do_not_rot():
    """Every exemption must still name a real writer that still lacks an
    append — a stale entry means the exemption list is drifting from the code."""

    writers = _qualified_writers()
    for name in EXEMPT:
        if name == "<module>._write_model":
            continue  # the primitive is matched by its own body, not scanned writes
        assert name in writers, f"EXEMPT entry {name!r} no longer writes store state — remove it"
        assert writers[name] is False, (
            f"EXEMPT entry {name!r} now appends an event — remove the stale exemption"
        )
