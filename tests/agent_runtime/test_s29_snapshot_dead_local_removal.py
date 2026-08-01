"""S29 sweeps the two leftovers the S27 snapshot-island cut left standing.

S27 (``064d46a27``) removed the 78-name orphan tree but explicitly KEPT
``_open_incidents_frame`` and ``snapshot_section_bytes``, because both were
*unenumerated* by its reachability gate: it seeded them as extra roots on the
grounds that S18 and ``test_snapshot_history_eviction`` pinned them. That was a
TEST pin, not a caller. Neither helper has ever had a production call site since
S9 dropped the ``incidents`` frame section and S2 stopped weighing sections
in-process:

* ``_open_incidents_frame`` split incidents into in-frame/evicted halves. S9
  removed ``incidents`` from ``ROW_TABLES`` and the frame; the builder no longer
  assembles an incident list at all, so nothing has a list to split.
* ``snapshot_section_bytes`` weighed one top-level section for the S2 byte
  goldens. Its docstring already says it is "deliberately independent of the
  parallel snapshot-audit module" — i.e. it exists for a test, and the only
  section it is still asked about is one the frame no longer carries.

The same commit's cut also exposed a PRE-EXISTING defect class in
``_build_snapshot_uncoalesced``: ten locals assigned and never read. They are
the seed values the removed mission projections used to consume
(``runs`` / ``incidents`` / ``proofs`` / ``self_tests`` / ``role_envelopes`` /
``role_checklists`` / ``repo_bundles`` / ``runtime_instances``), plus
``execution_mode`` (the retired Mission Daemon's mode string) and
``live_channel_task_ids`` (a set derived for the archived-operator-channel merge
S18 removed). An assigned-never-read local is invisible to ``flake8``-less CI
and to the intra-module reachability gate alike, because the reachability gate
walks module-level names, not function bodies — so this is gated on its own
defect class below, not on a name list.

KEEP-side, verified live and pinned here: ``tasks`` / ``workers`` / ``run_rows``
look identical to the removed seeds and are NOT dead — each is still passed into
a live projection (``_workspace_summary`` / ``snapshot_prompt_observability`` /
``operator_channel_summary`` / ``derive_from_workers``). Deleting them by
resemblance would break the build.
"""

from __future__ import annotations

import ast
import inspect

from agent_runtime import snapshot


#: The two helpers S27 kept as roots on a test-only pin.
REMOVED_SNAPSHOT_SYMBOLS = (
    "_open_incidents_frame",
    "snapshot_section_bytes",
)

#: The ten assigned-never-read locals in ``_build_snapshot_uncoalesced``.
REMOVED_DEAD_LOCALS = (
    "runs",
    "execution_mode",
    "incidents",
    "role_envelopes",
    "role_checklists",
    "repo_bundles",
    "runtime_instances",
    "proofs",
    "self_tests",
    "live_channel_task_ids",
)

#: Same-shaped locals in the SAME function that are live inputs to a projection.
#: ``tasks`` stood here until S47: it WAS passed to three projections, which is
#: why S29 kept it, but an always-empty list can only make a projection emit a
#: constant. The operator ruling cut the seed with the ``workspaces[].goals``
#: field it fed (ledger item 5) — see
#: ``tests/agent_runtime/test_s47_wire_constant_field_removal.py``.
KEPT_LIVE_LOCALS = ("workers", "run_rows")


def _function(name: str) -> ast.FunctionDef:
    tree = ast.parse(inspect.getsource(snapshot))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined in agent_runtime.snapshot")


def test_the_test_pinned_history_helpers_are_gone():
    assert [name for name in REMOVED_SNAPSHOT_SYMBOLS if hasattr(snapshot, name)] == []


def test_no_function_assigns_a_local_it_never_reads():
    """The defect class this sweep retires: a local bound to a seed value that
    no surviving statement reads.

    Scoped to plain single-target ``name = ...`` bindings so tuple unpacking and
    augmented/annotated assignment (whose targets carry other meaning) are not
    misread as dead."""

    tree = ast.parse(inspect.getsource(snapshot))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        loaded = {
            inner.id
            for inner in ast.walk(node)
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load)
        }
        for statement in ast.walk(node):
            if not isinstance(statement, ast.Assign):
                continue
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                continue
            name = statement.targets[0].id
            if name not in loaded:
                offenders.append(f"{node.name}:{statement.lineno}:{name}")
    assert offenders == []


def test_the_named_dead_locals_are_not_bound_anywhere_in_the_builder():
    """Name-level pin on top of the defect-class gate, so a re-introduction is
    reported as the specific mission-lane seed it is."""

    builder = _function("_build_snapshot_uncoalesced")
    bound = {
        target.id
        for statement in ast.walk(builder)
        if isinstance(statement, ast.Assign)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }
    assert sorted(bound & set(REMOVED_DEAD_LOCALS)) == []


def test_the_lookalike_live_locals_survive():
    """Negative gate: the locals in the same function that LOOK like the removed
    seeds and are load-bearing inputs to live projections. Two of the original
    three remain; ``tasks`` left at S47 (see KEPT_LIVE_LOCALS)."""

    builder = _function("_build_snapshot_uncoalesced")
    source = ast.get_source_segment(inspect.getsource(snapshot), builder) or ""
    bound = {
        target.id
        for statement in ast.walk(builder)
        if isinstance(statement, ast.Assign)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }
    for name in KEPT_LIVE_LOCALS:
        assert name in bound, name
    assert "tasks=tasks" not in source  # S47
    assert "run_summaries=run_rows" in source
    assert "derive_from_workers(agents, workers)" in source


def test_the_reachability_roots_are_back_to_the_real_external_surface():
    """S27's gate seeded two extra roots to protect the test-pinned helpers.
    With them removed, the module must be fully reachable from its five real
    external names alone."""

    roots = {
        "build_snapshot",
        "write_snapshot",
        "_parity_envelope",
        "_default_persona_session_db",
        "persona_instance_detail_for_id",
    }
    tree = ast.parse(inspect.getsource(snapshot))
    defs: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defs[target.id] = node
    module_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load):
                module_level.add(inner.id)

    def referenced(node) -> set[str]:
        return {
            inner.id
            for inner in ast.walk(node)
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load) and inner.id in defs
        }

    seen: set[str] = set()
    stack = list((roots | module_level) & set(defs))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(referenced(defs[current]) - seen)

    assert sorted(set(defs) - seen) == []


def test_the_live_frame_is_unchanged(isolate_agent_runtime_root):
    """Negative gate: this is residue removal, not a contract move."""

    frame = snapshot.build_snapshot()
    assert frame["parity"]["contract_version"] == 46
    for section in ("boards", "offices", "workspaces", "realms", "agents"):
        assert section in frame, f"{section} is a KEEP frame and must survive"
    for section in ("goals", "archived_tasks", "proofs", "incidents", "runs", "stage_verification"):
        assert section not in frame, f"{section} must not be a top-level frame section"
