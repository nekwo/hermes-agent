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
import pathlib
import textwrap

import pytest

from agent_runtime import snapshot

from tests.agent_runtime import _tree_index


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
#: ``workers`` left at S56 for the SAME reason, one stage later: S47 kept it as
#: "the one surviving seed" because ``derive_from_workers(agents, workers)``
#: still consumed it, but the list was always empty, so the projection could only
#: ever emit a constant. S56 deleted ``agent_runtime/worker_sessions.py`` whole
#: and renamed the projection to ``ensure_for_personas(personas)``, which takes
#: no worker argument — the seed went with it. Its removal is asserted in
#: ``test_the_lookalike_live_locals_survive`` rather than merely dropped here.
#: S64 retired the last member, ``run_rows``: it was hard-coded empty and only
#: fed the now-removed test-only run-summary operator projection.
KEPT_LIVE_LOCALS = ()


#: The five names S27 hand-verified as this module's external surface. A FLOOR
#: under the derived root set below, never the root set itself — see
#: ``_external_surface_of_snapshot``.
#:
#: H2 (MCF-27) moved one entry rather than removing it. ``status.py`` used to
#: import ``_default_persona_session_db`` and bind the handle it returns without
#: ever closing it; it now imports ``persona_session_db_scope``, which owns the
#: acquisition AND its release for both call sites. So the acquisition helper is
#: still live — the scope calls it — but it is no longer part of this module's
#: EXTERNAL surface, and leaving it in this floor would have asserted a consumer
#: that no longer exists. The floor tracks the surface; it does not preserve it.
#: STAGE 6 (2026-08-22) removed ``write_snapshot`` from this floor, and it is the
#: clean case of the rule stated just above — "the floor tracks the surface; it
#: does not preserve it". The name is not merely no longer external: it is no
#: longer DEFINED. Its one production caller was inside the retired read-model
#: lane, the ``snapshot.json`` boot cache it wrote lost its launcher reader at
#: MC-7 / P11, and the function was deleted with the lane. FOUR names now.
VERIFIED_EXTERNAL_SURFACE = (
    "build_snapshot",
    "_parity_envelope",
    "persona_session_db_scope",
    "persona_instance_detail_for_id",
)

#: Production packages a consumer of ``snapshot.py`` can live in, plus the
#: repo-root modules. ``tests/`` is excluded ON PURPOSE, and that exclusion is
#: this file's own thesis: a test pin is not a caller. It is why the two helpers
#: at the top of this file were removable at all.
_PRODUCTION_PACKAGES = (
    "agent_runtime",
    "hermes_cli",
    "gateway",
    "agent",
    "tools",
    "cron",
    "providers",
    "scripts",
    "apps",
)

_SKIPPED_DIRECTORIES = frozenset(
    {"tests", "node_modules", ".venv", "venv", "site-packages", "__pycache__", "build", "dist"}
)


def _function(name: str) -> ast.FunctionDef:
    tree = ast.parse(inspect.getsource(snapshot))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined in agent_runtime.snapshot")


#: A local ``agent_runtime.snapshot`` demonstrably binds, used as the
#: anti-vacuity witness for :func:`_bound_names`. A binding walk that resolves
#: nothing passes every absence assertion forever — the exact failure this file
#: was re-aimed twice to retire.
_ANTI_VACUITY_LIVE_LOCAL = "agents"


def _bound_names(node) -> set[str]:
    """Every plain name BOUND anywhere under ``node``, in any binding form.

    ``ast.Assign`` alone answers "was this name given a value by ``name = ...``",
    which is narrower than the question these gates ask ("is this seed back?").
    A reintroduced seed spelled ``for runs in ...``, ``runs: list = []``,
    ``runs += ...``, ``(runs := [])`` or ``with ... as runs`` would be invisible
    to it."""

    bound: set[str] = set()

    def _record(target) -> None:
        if isinstance(target, ast.Name):
            bound.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                _record(element)
        elif isinstance(target, ast.Starred):
            _record(target.value)

    for inner in ast.walk(node):
        if isinstance(inner, ast.Assign):
            for target in inner.targets:
                _record(target)
        elif isinstance(inner, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            _record(inner.target)
        elif isinstance(inner, (ast.For, ast.AsyncFor, ast.comprehension)):
            _record(inner.target)
        elif isinstance(inner, ast.withitem):
            if inner.optional_vars is not None:
                _record(inner.optional_vars)
    return bound


def _repo_root() -> pathlib.Path:
    return pathlib.Path(snapshot.__file__).resolve().parents[1]


def _production_sources(root: pathlib.Path):
    for package in _PRODUCTION_PACKAGES:
        directory = root / package
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            if _SKIPPED_DIRECTORIES & set(path.relative_to(root).parts):
                continue
            yield path
    yield from sorted(root.glob("*.py"))


def _imported_module(node: ast.ImportFrom, path: pathlib.Path, root: pathlib.Path) -> str:
    """The absolute dotted module an ``ImportFrom`` reads, relative forms resolved.

    ``from .snapshot import X`` inside ``agent_runtime/`` names the same module
    as ``from agent_runtime.snapshot import X``; a scan that only understood the
    absolute form would miss half the package's own consumers."""

    if node.level == 0:
        return node.module or ""
    package = list(path.relative_to(root).with_suffix("").parts[:-1])
    if node.level > 1:
        package = package[: len(package) - (node.level - 1)]
    return ".".join(package + ([node.module] if node.module else []))


def _external_surface_of_snapshot() -> dict[str, set[str]]:
    """Every production name reached INTO ``agent_runtime.snapshot``, with sites.

    DERIVED, not restated. This file and S27 both walked reachability from a
    hardcoded five-name ``roots`` set, which answers "reachable from the surface
    someone wrote down in 2026-07" rather than "reachable from the surface".
    ``hermes_cli/harness_parts/serve.py`` then added a function-local ``from
    agent_runtime.snapshot import snapshot_build_context_scope``, and both gates
    started reporting that live, called context manager as an unreachable orphan
    — a false accusation pointed at production code, standing for days. A cut
    driven by the verdict would have deleted something with a caller.

    Collected forms:

    * ``from agent_runtime.snapshot import X`` / ``from .snapshot import X`` at
      ANY depth, function bodies included — precisely the form that was missed.
    * ``snapshot.X`` attribute loads in a file that binds this module to that
      name.

    Bounded, deliberate over-approximation: a file that binds the module and
    also shadows the name with a local would contribute that local's attributes.
    Over-approximating roots can only HIDE an orphan, never invent one, so the
    derivation is paired with anti-vacuity assertions — non-empty, contains
    ``build_snapshot``, covers the verified five, and the walk itself must
    demonstrably name a planted orphan."""

    root = _repo_root()
    this_module = pathlib.Path(snapshot.__file__).resolve()
    surface: dict[str, set[str]] = {}
    for path in _production_sources(root):
        if path.resolve() == this_module:
            continue
        try:
            tree = _tree_index.parsed(str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        where = path.relative_to(root).as_posix()
        module_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported = _imported_module(node, path, root)
                if imported == "agent_runtime.snapshot":
                    for alias in node.names:
                        if alias.name != "*":
                            surface.setdefault(alias.name, set()).add(f"{where}:{node.lineno}")
                elif imported == "agent_runtime":
                    module_aliases.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name == "snapshot"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "agent_runtime.snapshot" and alias.asname:
                        module_aliases.add(alias.asname)
        if not module_aliases:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Name)
                and node.value.id in module_aliases
            ):
                surface.setdefault(node.attr, set()).add(f"{where}:{node.lineno}")
    return surface


def _unreachable_module_level_names(source: str, roots) -> list[str]:
    """Module-level names in ``source`` reachable from neither ``roots`` nor
    module-level executable code. Pure over text so it can be exercised against
    a synthetic module — a gate whose walk silently resolves nothing passes
    forever, which is the failure mode this wave exists to retire."""

    tree = ast.parse(source)
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
    stack = list((set(roots) | module_level) & set(defs))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(referenced(defs[current]) - seen)
    return sorted(set(defs) - seen)



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


def test_the_named_dead_locals_are_not_bound_anywhere_in_the_snapshot_module():
    """Name-level pin on top of the defect-class gate, so a re-introduction is
    reported as the specific mission-lane seed it is.

    RE-AIMED 2026-08-19 (MCF-47), and it is the SAME defect the sibling
    ``test_the_lookalike_live_locals_survive`` was re-aimed for at MCF-27 —
    missed here because this gate names a function rather than a source
    segment. It walked ``_function("_build_snapshot_uncoalesced")`` for
    ``ast.Assign`` targets. That function became a two-statement WRAPPER when
    the SessionDB acquisition moved to the build's outermost frame: one ``with
    runtime_resolution_scope(), persona_session_db_scope() as session_db:`` and
    one ``return _build_snapshot_in_runtime_scope(...)``. It contains **zero**
    assignments, so ``bound`` was the empty set and the intersection was empty
    for a reason that had nothing to do with the seeds. **The gate could not
    have failed** — reintroducing all ten seeds in the body would have left it
    green.

    Re-aiming it at ``_build_snapshot_in_runtime_scope`` would reproduce the
    defect at the next refactor that moves a section one frame over, exactly as
    the sibling's docstring argues. So the scope is the MODULE: the guarantee is
    "no surviving code in ``agent_runtime.snapshot`` binds one of these ten
    mission-lane seed names", which is where the guarantee actually lives and is
    strictly stronger than any single-function walk.

    Binding is read in every form a seed could come back in — plain and
    annotated assignment, augmented assignment, walrus, ``for`` and
    comprehension targets, ``with ... as`` — because ``ast.Assign`` alone would
    let ``for runs in ...`` back in silently. And the walk is pinned
    ANTI-VACUOUSLY against a live local: if it resolves nothing, it says so
    instead of passing."""

    module = ast.parse(inspect.getsource(snapshot))
    bound = _bound_names(module)
    assert _ANTI_VACUITY_LIVE_LOCAL in bound, (
        f"the binding walk cannot see {_ANTI_VACUITY_LIVE_LOCAL!r}, a local "
        "agent_runtime.snapshot demonstrably binds — it is resolving nothing "
        f"and this gate would pass vacuously. Resolved {len(bound)} names."
    )
    assert sorted(bound & set(REMOVED_DEAD_LOCALS)) == []


def test_the_lookalike_live_locals_survive(isolate_agent_runtime_root):
    """Negative gate: the locals that LOOK like the removed seeds and were once
    load-bearing inputs to projections. ``tasks`` left at S47, ``workers`` at
    S56, and the hard-empty ``run_rows`` at S64. Each departure is INVERTED
    below rather than silently dropped.

    RE-AIMED 2026-08-09, same defect class as the four gates in ``a20973d03``.
    Every assertion here read ``ast.get_source_segment`` of ONE function,
    ``_build_snapshot_uncoalesced``, and looked for a substring in it. The
    positive half — ``"ensure_for_personas(agents)" in source`` — went red when a
    refactor moved the roster projection one function over into
    ``_build_snapshot_in_runtime_scope``. The roster was untouched; the gate was
    measuring which function a line of text sits in.

    Re-aiming it by inspecting the OTHER function name would reproduce the
    defect one refactor later, so the whole test is now module-scoped and
    structural: the guarantee is "the snapshot module still derives its roster
    from the persona list, and from nothing else", not "this call is on this
    line of this function". The negative halves move to the module too, which is
    strictly stronger — a seed reintroduced in a sibling function was previously
    invisible to all of them."""

    module = ast.parse(inspect.getsource(snapshot))
    bound_anywhere = {
        target.id
        for statement in ast.walk(module)
        if isinstance(statement, ast.Assign)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }
    for name in KEPT_LIVE_LOCALS:
        assert name in bound_anywhere, name

    # The three departed seeds, by binding rather than by spelling.
    for departed in ("tasks", "workers", "run_rows"):
        assert departed not in bound_anywhere, (
            f"the {departed!r} seed is bound again somewhere in agent_runtime.snapshot"
        )

    calls = [node for node in ast.walk(module) if isinstance(node, ast.Call)]
    keywords_passed = {keyword.arg for call in calls for keyword in call.keywords}
    for forwarded in ("tasks", "run_summaries"):  # S47 / S64
        assert forwarded not in keywords_passed, (
            f"a call in agent_runtime.snapshot forwards {forwarded}= again"
        )

    # INVERTED at S56: this asserted ``derive_from_workers(agents, workers)`` was
    # the live consumer that justified keeping the ``workers`` seed. Both the
    # seed and the worker argument are gone.
    called_names = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in calls
        if isinstance(node.func, (ast.Attribute, ast.Name))
    }
    assert "derive_from_workers" not in called_names

    # The surviving projection, pinned by its GUARANTEE: it still runs, and it
    # takes the persona list alone. A second argument would be the worker seed
    # coming back under any name, in any function.
    roster_calls = [
        node
        for node in calls
        if isinstance(node.func, ast.Attribute) and node.func.attr == "ensure_for_personas"
    ]
    assert roster_calls, (
        "the persona-instance roster projection has left agent_runtime.snapshot "
        "entirely — S56 named ensure_for_personas as where the roster comes from"
    )
    for call in roster_calls:
        assert len(call.args) == 1 and not call.keywords, (
            f"snapshot.py:{call.lineno} passes ensure_for_personas more than the "
            "persona list; the retired worker argument is back"
        )
        assert isinstance(call.args[0], ast.Name), (
            f"snapshot.py:{call.lineno} no longer feeds the roster a plain persona list"
        )

    # Behavioural half: the projection is not merely present in the source, it
    # produces the roster section every Mission Control surface keys on.
    assert "persona_instances" in snapshot.build_snapshot()


# Same full-production-tree parse as test_s27's walk (25-30 s cold, 2026-09-03
# measurement) — sharing the cache (conftest's `_SHARED_TREE_WALK_MODULES`)
# makes this fast when test_s27 already warmed it, but this test can also run
# alone or run first, so it needs its own honest margin.
@pytest.mark.timeout(60)
def test_the_reachability_roots_are_back_to_the_real_external_surface():
    """S27's gate seeded two extra roots to protect the test-pinned helpers.
    With them removed, the module must be fully reachable from its REAL external
    surface — and this test's whole point is what "real" means.

    RE-AIMED 2026-08-09. It meant "the five names S27 wrote down", restated here
    as a literal set. That is the same category of error the file was written to
    correct, one level up: S27 rooted two helpers on a TEST pin; this gate rooted
    the module on a SNAPSHOT of the surface. Both freeze an answer instead of
    asking the question. When ``hermes_cli/harness_parts/serve.py`` grew a
    function-local ``from agent_runtime.snapshot import
    snapshot_build_context_scope``, the frozen set could not see it and the gate
    accused a live, called context manager of being an orphan.

    The surface is now derived from the production tree, so a genuinely-external
    consumer added tomorrow roots itself, and the two helpers this file removed
    must NOT reappear in it — asserted directly below, which is the property the
    test is named for and which the old literal set could only imply."""

    surface = _external_surface_of_snapshot()
    assert surface, (
        "the external-surface derivation resolved NO consumer of "
        "agent_runtime.snapshot — the roots are empty and this gate would pass "
        "vacuously"
    )
    assert "build_snapshot" in surface, (
        "the derivation cannot see build_snapshot, the module's most-imported "
        f"name; it is not resolving imports. Derived: {sorted(surface)}"
    )
    missing_floor = [name for name in VERIFIED_EXTERNAL_SURFACE if name not in surface]
    assert missing_floor == [], (
        f"{missing_floor} were hand-verified external at S27 and the derivation "
        "no longer finds a production consumer for them"
    )

    # The property this file owns: neither helper it removed has a production
    # consumer, so neither can re-enter the root set as anything but a test pin.
    for test_only in REMOVED_SNAPSHOT_SYMBOLS:
        assert test_only not in surface, (
            f"{test_only} has a production consumer again at "
            f"{sorted(surface[test_only])} — S29 removed it as caller-free"
        )

    unreachable = _unreachable_module_level_names(inspect.getsource(snapshot), surface)
    assert unreachable == [], (
        "unreachable from every production consumer of agent_runtime.snapshot: "
        f"{unreachable}"
    )


def test_the_reachability_walk_names_a_planted_orphan():
    """ANTI-VACUITY, run against a synthetic module. The walk above asserts an
    EMPTY result — the shape that keeps passing quietly after the machinery
    under it stops working. Handed a module with a known root, a helper the root
    reaches, module-level executable code that reaches a second helper, and one
    genuine island, it must name the island and nothing else."""

    synthetic = textwrap.dedent(
        '''
        CAP = 5

        def build_snapshot():
            return _reached_from_the_root(CAP)

        def _reached_from_the_root(limit):
            return limit

        def _reached_from_module_level():
            return 1

        _reached_from_module_level()

        def _orphan_head():
            return _orphan_tail()

        def _orphan_tail():
            return _orphan_head()
        '''
    )

    assert _unreachable_module_level_names(synthetic, {"build_snapshot"}) == [
        "_orphan_head",
        "_orphan_tail",
    ]
    assert _unreachable_module_level_names(synthetic, {"build_snapshot", "_orphan_head"}) == []


def test_the_live_frame_is_unchanged(isolate_agent_runtime_root):
    """Negative gate: this is residue removal, not a contract move."""

    frame = snapshot.build_snapshot()
    assert frame["parity"]["contract_version"] == snapshot.SNAPSHOT_CONTRACT_VERSION
    for section in ("boards", "offices", "workspaces", "realms", "agents"):
        assert section in frame, f"{section} is a KEEP frame and must survive"
    for section in ("goals", "archived_tasks", "proofs", "incidents", "runs", "stage_verification"):
        assert section not in frame, f"{section} must not be a top-level frame section"
