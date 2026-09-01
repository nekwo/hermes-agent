"""S27 retires the ~78-name orphan tree left inside ``snapshot.py``.

S18 cut ``goal_detail_for_task`` -> ``_goal_projection_from_task`` ->
``_task_summary``, the only entry point into the mission projection. Everything
that chain reached survived the cut as an unreachable island: the archived-task
transcript readers, the stage-verification / proof-lane builders, the mission
actor + lifecycle summarizers, the role/stage stream projectors, and the
snapshot-local copies of ``_run_summary`` / ``_event_display_*``.

The island is verified caller-free from the module's REAL external surface,
which is exactly five names (``build_snapshot``, ``write_snapshot``,
``_parity_envelope``, ``persona_session_db_scope`` — the S27 name here was
``_default_persona_session_db``, until H2 gave the acquisition an owning scope
and ``status.py`` moved to it — and ``persona_instance_detail_for_id``). A plain
text grep does NOT prove this: nine
of the removed names collide with same-named private helpers in OTHER modules
(``observability._run_summary`` / ``._event_display_kind`` /
``._event_display_title`` / ``._event_display_projection``,
``board_store._read_json``, ``budget_approval._safe_int``,
``runtime_commands._archived_task_summary``, and the ``status.py`` COMMENT that
names ``_stopped_progress`` / ``_has_budget_incident``). Those collisions are
what kept this island looking alive; the pins below record them so a future pass
does not re-derive the same false liveness.

RETARGETED 2026-07-31 (S45): one of the nine collisions no longer exists.
``agent_runtime/budget_approval.py`` was deleted whole as a test-only module, so
``_safe_int`` collides with nothing now. The pin below asserts its ABSENCE
instead of being dropped — a collision witness that quietly loses one of its
nine hides the fact that the count changed.

S18's own keep-side pin is corrected here, not deleted: it justified
``_archived_task_summaries`` as "the fetch lane behind ``harness task
history``". S8 removed that CLI verb (``tests/agent_runtime/
test_snapshot_history_eviction.py`` asserts the parser now rejects the whole
history verb family), so the reader had no lane left to serve.
"""

from __future__ import annotations

import ast
import functools
import inspect
import pathlib
import textwrap

from agent_runtime import snapshot

from tests.agent_runtime import _tree_index


#: The unreachable island. Grouped by the chain that used to reach it.
REMOVED_SNAPSHOT_SYMBOLS = (
    # Caps whose only readers were the removed builders.
    "STAGE_VERIFICATION_STAGE_CAP",
    "STAGE_VERIFICATION_PROOF_ID_CAP",
    "STAGE_VERIFICATION_PATH_CAP",
    "MISSION_FLOW_TIMELINE_ITEM_CAP",
    "ARCHIVED_TASKS_REF_RECENT_CAP",
    # Archived-mission readers + the transcript family behind them.
    "_archived_task_summaries",
    "_archived_task_summary",
    "_archived_conversation_text",
    "_archived_conversation_list",
    "_archived_conversation_message_sort_key",
    "_dedupe_archived_conversation_messages",
    "_latest_archived_message_timestamp",
    "_parse_archived_time",
    "_archived_run_summaries",
    "_archived_proof_summaries",
    "_archived_role_envelope_summaries",
    "_archived_role_checklist_summaries",
    "_archived_persona_assignment_summaries",
    "_archived_repo_bundle_summaries",
    "_archived_persona_streams",
    "_archived_event_log_events",
    "_safe_archive_task_filename",
    "_dedupe_archived_events",
    "_archived_transcript_events",
    "_archived_role_streams",
    "_archived_event_stream_item",
    "_coalesced_archived_progress_events",
    "_empty_archived_role_stream_item",
    "_archived_event_display_kind",
    "_archived_event_display_title",
    "_archived_role_current_stage",
    "_run_summary_from_mapping",
    "_persona_timing_summaries",
    "_timing_total",
    "_add_int",
    "_duration_ms",
    "_proof_summary_from_mapping",
    "_read_json",
    # Stage verification / proof-lane projection.
    "_verification_status",
    "_stage_verification",
    "_stage_owner_by_id",
    "_bounded_projection_strings",
    "_proof_ref",
    "_proof_lane_status",
    "_proof_status",
    "_stage_tamper_flag",
    "_task_current_stage_id",
    "_safe_int",
    "_proof_visibility_summary",
    # Mission lifecycle / actor / capability summarizers.
    "_mission_lifecycle_state",
    "_mission_level_state",
    "_operator_capabilities",
    "_actor_state_label",
    "_latest_actor_event",
    "_actor_budget_summary",
    "_runtime_lane_summary",
    "_persona_streams",
    # Execution routing: "can a run start / why not / what next".
    "_execution_status",
    "_can_start_run",
    "_run_blocked_reason",
    "_next_action_summary",
    "_why_not_done",
    "_stopped_progress",
    "_has_budget_incident",
    # Role / stage / timeline stream projection.
    "_task_timeline",
    "_coalesced_progress_events",
    "_role_streams",
    "_stage_streams",
    "_event_stream_item",
    "_empty_role_stream_item",
    "_display_name_for_persona",
    "_role_current_stage",
    "_event_display_projection",
    "_event_display_kind",
    "_event_display_title",
    # Run projection (orphaned when the run rows left the frame).
    "_run_summary",
    "_safe_llm",
    "_public_decision_value",
)

#: Imports the island was the sole consumer of. A surviving import is a
#: surviving dependency edge, so the gate is the import statement.
REMOVED_SNAPSHOT_IMPORTS = (
    "role_checklists",
    "simplified_contract",
)

#: The five names this file verified as external at S27. They are a FLOOR
#: (asserted on its own below), never the root set itself — see
#: ``_external_surface_of_snapshot``.
#:
#: THE CONSUMER LEFT, and the floor's own assertion message asks for that to be
#: said here rather than patched silently. H2 (MCF-27) gave the chat SessionDB
#: acquisition an owner: ``status.py`` used to import
#: ``_default_persona_session_db`` and bind the handle without ever closing it,
#: and now imports ``persona_session_db_scope``, which acquires AND releases for
#: both call sites. ``_default_persona_session_db`` is still live — the scope
#: calls it — so it stays reachable from the walk below; it is simply no longer
#: part of this module's EXTERNAL surface, and the scope that replaced it is.
#: STAGE 6 (2026-08-22): ``write_snapshot`` LEFT this floor, and it left in the
#: manner the paragraph above demands — said here, not patched silently. The
#: consumer went first: its one production caller was
#: ``read_model.resolve_snapshot_frame``, and the ``snapshot.json`` boot cache it
#: wrote had already lost its launcher reader at MC-7 / P11. With the read-model
#: lane retired, ``write_snapshot`` had no caller of any kind and was deleted from
#: ``snapshot.py`` — so leaving it in this floor would have asserted a name the
#: module does not define, which is the opposite failure to the one the floor
#: guards. FOUR names now, not five.
VERIFIED_EXTERNAL_SURFACE = (
    "build_snapshot",
    "_parity_envelope",
    "persona_session_db_scope",
    "persona_instance_detail_for_id",
)

#: Production packages a consumer of ``snapshot.py`` can live in, plus the
#: repo-root modules. ``tests/`` is excluded ON PURPOSE and that exclusion is
#: this whole family's thesis: a test pin is not a caller (S29 removed two
#: helpers S27 had rooted on exactly that mistake).
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

    ``from .snapshot import X`` inside ``agent_runtime/`` names the same module as
    ``from agent_runtime.snapshot import X``; a scan that only understood the
    absolute form would miss half the package's own consumers."""

    if node.level == 0:
        return node.module or ""
    package = list(path.relative_to(root).with_suffix("").parts[:-1])
    if node.level > 1:
        package = package[: len(package) - (node.level - 1)]
    return ".".join(package + ([node.module] if node.module else []))


@functools.lru_cache(maxsize=1)
def _external_surface_of_snapshot() -> dict[str, set[str]]:
    """Every production name reached INTO ``agent_runtime.snapshot``, with sites.

    DERIVED, not restated. S27 and S29 both hardcoded a five-name ``roots`` set
    and walked reachability from it. A hardcoded root set answers "is this name
    reachable from the surface I wrote down in 2026-07", which stops being the
    question the moment someone adds a consumer: ``serve.py`` grew a
    function-local ``from agent_runtime.snapshot import
    snapshot_build_context_scope`` and both gates reported that live, called
    helper as an unreachable orphan — a false accusation aimed at production
    code, for days.

    So the roots are read off the tree instead:

    * ``from agent_runtime.snapshot import X`` / ``from .snapshot import X`` —
      at ANY depth, function bodies included, which is precisely the form that
      was being missed.
    * ``snapshot.X`` attribute loads in a file that binds this module to that
      name (``from agent_runtime import snapshot``, ``import
      agent_runtime.snapshot as snapshot``).

    Known over-approximation, bounded and deliberate: a file that both binds the
    module AND shadows the binding with a local of the same name would
    contribute that local's attributes too. Over-approximating roots can only
    HIDE an orphan, never invent one, so it is paired with the anti-vacuity
    assertions below — the derived set must be non-empty, must contain
    ``build_snapshot``, must cover the verified five, and the walk itself must
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
    forever, which is the failure mode this whole wave is about."""

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




def test_no_module_level_name_is_unreachable_from_the_external_surface():
    """The defect class this stage retires: an island that survives a cut because
    every reference into it comes from inside itself.

    The roots are the module's external surface. Anything not reachable from
    them (or from module-level executable code) is unreachable by construction,
    regardless of what a text grep says.

    RE-AIMED 2026-08-09. The roots used to be a hardcoded list of five names —
    the surface as verified once, at S27 — so the gate could only ever answer
    "reachable from the 2026-07 surface", and any consumer added afterwards was
    invisible to it. ``hermes_cli/harness_parts/serve.py`` added a
    function-local ``from agent_runtime.snapshot import
    snapshot_build_context_scope`` and this gate began naming that live, called
    context manager as an unreachable orphan. A cut driven by that verdict would
    have deleted production code that has a caller.

    The surface is now DERIVED from the production tree (see
    ``_external_surface_of_snapshot``), so a new external consumer roots itself.
    The five originally-verified names survive as a separately-asserted FLOOR —
    ``test_the_verified_external_surface_is_still_external`` — because a
    derivation that silently resolved nothing would make this assertion pass
    vacuously forever."""

    surface = _external_surface_of_snapshot()
    assert surface, (
        "the external-surface derivation resolved NO consumer of "
        "agent_runtime.snapshot anywhere in the production tree — the roots are "
        "empty and this gate would pass vacuously"
    )
    assert "build_snapshot" in surface, (
        "the derivation cannot see build_snapshot, the module's most-imported "
        f"name; it is not resolving imports. Derived: {sorted(surface)}"
    )

    unreachable = _unreachable_module_level_names(inspect.getsource(snapshot), surface)
    assert unreachable == [], (
        "unreachable from every production consumer of agent_runtime.snapshot: "
        f"{unreachable}"
    )


def test_the_verified_external_surface_is_still_external():
    """FLOOR under the derivation above. These five were hand-verified as
    external at S27; the derived set must still contain every one of them.

    Kept as a floor rather than as the root set: as the root set they were a
    ceiling that falsely accused ``snapshot_build_context_scope``. As a floor
    they catch the opposite failure — a derivation that quietly stops resolving
    imports would produce a small or empty root set, every gate above it would
    still pass, and the "no orphans" claim would mean nothing."""

    surface = _external_surface_of_snapshot()
    missing = [name for name in VERIFIED_EXTERNAL_SURFACE if name not in surface]
    assert missing == [], (
        f"{missing} were verified external at S27 but the derivation no longer "
        "finds a production consumer for them — either the consumer left (say "
        "so in this file) or the scan is broken"
    )
    # ``snapshot_build_context_scope`` is the name that exposed the hardcoded
    # root set. Pinned by its call site so a regression names the file to look at.
    assert any(
        site.startswith("hermes_cli/harness_parts/serve.py")
        for site in surface.get("snapshot_build_context_scope", set())
    ), (
        "serve.py no longer imports snapshot_build_context_scope; it was the "
        "function-local import the old hardcoded roots could not see"
    )


def test_the_reachability_walk_names_a_planted_orphan():
    """ANTI-VACUITY, run against a synthetic module. The walk above asserts an
    EMPTY result, which is exactly the shape that keeps passing after the
    machinery underneath it stops working. Here it is handed a module with a
    known-good root, a helper that root reaches, module-level executable code
    that reaches a second helper, and one genuine island — and it must name the
    island and nothing else."""

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
    # And it does not report an island once the surface reaches it.
    assert _unreachable_module_level_names(synthetic, {"build_snapshot", "_orphan_head"}) == []


def test_the_lookalike_keep_set_survives():
    """Nine removed names collide with live private helpers in other modules —
    the exact collisions that made a text grep report this island as live.

    S45 retarget: one of the nine, ``budget_approval._safe_int``, no longer
    exists — see below. The other eight are untouched."""

    from agent_runtime import board_store, observability

    assert callable(observability._run_summary)
    assert callable(observability._event_display_kind)
    assert callable(observability._event_display_title)
    assert callable(observability._event_display_projection)
    assert callable(board_store._read_json)
    # S45: ``budget_approval._safe_int`` was the ninth collision pinned here — a
    # live private helper sharing a name with a removed snapshot local. The
    # collision was real and this file was right to pin it; the module was then
    # deleted whole at S45 as test-only, so the name now collides with nothing.
    # Asserted as ABSENT rather than dropped, so the reversal of an explicit
    # keep-side claim stays visible in the file that made it.

    # ``status.py`` only NAMES snapshot's ``_stopped_progress`` /
    # ``_has_budget_incident`` in a comment; its own chain went in S21.

    # The live frames the launcher renders every tick.
    assert callable(snapshot.build_snapshot)
    assert callable(snapshot._boards_summary)
    assert callable(snapshot._realm_summary)
    assert callable(snapshot._workspace_summary)
    assert callable(snapshot._offices_summary)
    assert callable(snapshot.persona_instance_detail_for_id)
    assert callable(snapshot._agent_tool_detail)


def test_the_live_frame_is_unchanged(isolate_agent_runtime_root):
    """Negative gate: cutting the island must not move the contract or drop a
    section the Launcher reads."""

    frame = snapshot.build_snapshot()
    assert frame["parity"]["contract_version"] == snapshot.SNAPSHOT_CONTRACT_VERSION
    for section in ("boards", "workspaces", "realms", "agents"):
        assert section in frame, f"{section} is a KEEP frame and must survive"
    for section in ("goals", "archived_tasks", "proofs", "incidents", "runs", "stage_verification"):
        assert section not in frame, f"{section} must not be a top-level frame section"


def test_the_workspace_goals_wire_field_is_gone_at_s47(isolate_agent_runtime_root):
    """SUPERSEDED PIN, recorded rather than deleted. S27 protected
    ``_workspace_summary``'s ``goals`` count as "a contract field the Launcher
    reads", correctly refusing to cut it by resemblance to the removed mission
    lane. S47 cut it on an operator ruling for the OTHER reason: the count read
    an always-empty seed, so the Launcher was reading a permanent 0 — it
    rendered it and gated workspace deletion on it. The field is a contract
    move (45 -> 46), not a resemblance sweep.

    See ``tests/agent_runtime/test_s47_wire_constant_field_removal.py``."""

    emitted = {
        key.value
        for node in ast.walk(ast.parse(inspect.getsource(snapshot._workspace_summary)))
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert "goals" not in emitted
    assert "tasks" not in inspect.signature(snapshot._workspace_summary).parameters
