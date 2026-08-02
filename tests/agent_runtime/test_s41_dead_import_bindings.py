"""S41 removes import BINDINGS that bind a name nothing in scope ever reads.

A dead import is not free. It keeps a retired symbol reachable by name, so the
next reachability pass counts it as "used" and the module it points at looks
live; it also re-imports a module the lane no longer depends on, which is how a
deleted subsystem keeps a foothold in the import graph long after its last
caller left.

Scope rules used to verify every entry here:

* ``hermes_cli/harness.py`` plus every ``hermes_cli/harness_parts/*.py`` is ONE
  scope — the parts are ``exec``'d into harness globals, so a part CAN read a
  name harness.py imported. Each removal below was verified by a whole-scope
  word search, not a per-file one.
* Every ``agent_runtime`` module here carries ``from __future__ import
  annotations``, so a name appearing ONLY in an annotation is a string at
  runtime. None of these do: each was a single line — the import itself.
* Nothing outside re-imports these names from these modules
  (``from agent_runtime.<module> import <name>`` and ``<module>.<name>``: zero).

Source symbols are untouched. ``LANE_MISSION_WORKER`` still lives in
``terminal_envelope``; ``TaskState`` / ``RunState`` still live in ``states`` and
are still imported directly by their real users.

No event contract moves: ``event_catalog()`` stays at 88.

=============================================================================
MIGRATED to ``tests/agent_runtime/test_tombstone_registry.py`` (2026-08-01)
=============================================================================

The pure ABSENCE half of this file is now registry rows, so it is asserted once
instead of once per wave. What moved, and in which form:

* ``hermes_cli/harness.py`` — eight of the twelve bindings (``human_task_line``,
  ``task_summary``, ``operator_takeover_worker``, ``LegacyOrchestratorRemoved``,
  ``launcher_visual_cleanup_needed``, ``OPERATOR_RESOLVABLE_TURN_STATES``,
  ``find_discovery_task``, ``worker_session_summary``) are ``Form.ATTR`` rows
  scoped to ``hermes_cli.harness``. The namespace IS the gate there — the parts
  are ``exec``'d into those globals — so ``not hasattr`` is strictly stronger
  than the AST binding scan and nothing was weakened by the move.
* ``agent_runtime/persona_runtime.py`` — all seven bindings, same form, scoped
  to that module. The whole entry went; nothing is left here to scan.
* ``agent_runtime/cli_format`` — ``human_task_line`` / ``task_summary`` are
  ``Form.ATTR`` rows. The definitions-absent half of
  ``test_every_source_symbol_the_bindings_pointed_at_is_untouched`` therefore
  overlaps the registry deliberately: that test is a KEEP pin with two inverted
  arms, and inverted pins are never dissolved into a row (see below).

WHY THE AST SCAN SURVIVES, TRIMMED. The registry's ``ATTR`` form asks "does the
module expose this name". That is the wrong question for the six rows left in
``REMOVED_BINDINGS``:

* ``os``, ``timedelta``, ``paths``, ``field``, ``Sequence`` are stdlib/sibling
  names. ``hasattr(agent_runtime.checkpoint, "os")`` is a fact about whether a
  module happens to re-export a stdlib module, not about whether THIS file still
  carries a dead ``import os`` — and a sibling module that legitimately imports
  ``os`` would make a repo-wide row unfixably red. Only a per-file AST walk over
  ``ast.Import`` / ``ast.ImportFrom`` can state the binding fact.
* ``hermes_cli/harness_parts/persona_commands.py::_relay_time`` is in a part
  that is ``exec``'d, never imported, so it has no module object to ``hasattr``
  against.
* ``DecisionType``, ``TaskState`` and
  ``RunState`` are LIVE symbols elsewhere (``decision_schema``,
  ``states``) that harness.py merely stopped binding.
  A registry row on their names would be a lie about the tree; the fact is
  file-local, so it stays file-local.

The retained-half pins, the harness-namespace pin, the definitions-untouched
pin (with its S49/S50/S56 inversions) and the still-imports pin are UNCHANGED —
none of them is a pure absence row.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest


#: module path -> binding names that must no longer be bound in it.
#:
#: TRIMMED 2026-08-01 to exactly the rows the tombstone registry CANNOT express
#: (see the migration note in the module docstring). Every name still here is an
#: import-binding fact about ONE file: either a stdlib/sibling name whose
#: ``hasattr`` form would be meaningless or unfixably red, a name in an exec'd
#: harness part that has no module object at all, or a symbol that is LIVE
#: elsewhere and merely stopped being bound here. The eight harness.py bindings
#: and all seven persona_runtime.py bindings whose absence IS expressible as
#: ``not hasattr(module, name)`` are now ``Form.ATTR`` rows in
#: ``test_tombstone_registry.py`` and were removed from this table, not dropped.
REMOVED_BINDINGS = {
    # Live symbols elsewhere (decision_schema / persona_assignments / states)
    # that harness.py merely stopped importing. Their names cannot be banned.
    "hermes_cli/harness.py": {
        "DecisionType",
        "TaskState",
        "RunState",
    },
    "agent_runtime/checkpoint.py": {"os"},
    "agent_runtime/observability.py": {"timedelta"},
    "agent_runtime/parity.py": {"paths"},
    "agent_runtime/terminal_envelope.py": {"field", "Sequence"},
    # RETIRED at S56 (2026-08-01): the row was
    # ``"agent_runtime/worker_sessions.py": {"Path"}`` — a dead ``Path`` binding
    # in the worker-session store. S56 deleted that module WHOLE, so the row's
    # subject no longer exists and a per-file parametrize case for it can only
    # error on a missing path. The reason is preserved here rather than the row
    # silently vanishing; the module's absence is asserted in
    # ``test_every_source_symbol_the_bindings_pointed_at_is_untouched`` below and
    # owned by tests/agent_runtime/test_s56_worker_session_lane_removal.py.
    "hermes_cli/harness_parts/persona_commands.py": {"_relay_time"},
}

#: The retained half of an import line that only lost some of its names, and the
#: source symbols the aliases pointed at. A cut that took these too would be a
#: behavior change, not a binding removal.
RETAINED_BINDINGS = {
    "hermes_cli/harness.py": {"emit_json", "AgentDecision", "WorkerSessionState"},
    "agent_runtime/persona_runtime.py": {"Callable", "TYPE_CHECKING"},
    "agent_runtime/observability.py": {"datetime"},
    "agent_runtime/parity.py": {"event_rotation"},
    "agent_runtime/terminal_envelope.py": {"dataclass", "Any", "Iterator", "Mapping"},
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _bound_names(path: Path) -> set[str]:
    """Every name any ``import`` in this file binds, at any nesting depth."""

    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


@pytest.mark.parametrize("relative", sorted(REMOVED_BINDINGS))
def test_no_dead_binding_survives(relative: str):
    bound = _bound_names(_repo_root() / relative)
    assert not (bound & REMOVED_BINDINGS[relative])


@pytest.mark.parametrize("relative", sorted(RETAINED_BINDINGS))
def test_the_retained_half_of_each_shared_import_line_stays(relative: str):
    bound = _bound_names(_repo_root() / relative)
    assert RETAINED_BINDINGS[relative] <= bound


def test_the_harness_namespace_no_longer_exposes_the_removed_names():
    """The parts are exec'd into these globals, so absence is the real gate."""

    harness = importlib.import_module("hermes_cli.harness")
    exposed = {
        name
        for name in REMOVED_BINDINGS["hermes_cli/harness.py"]
        if hasattr(harness, name)
    }
    assert exposed == set()


def test_every_source_symbol_the_bindings_pointed_at_is_untouched():
    """Negative gate: THIS stage removed bindings, never definitions.

    Four exceptions, and they are deliberate rather than a hole in the gate:

    ``cli_format.human_task_line`` / ``task_summary`` were dead in their own
    right (task-record formatters with no reader anywhere once this binding
    went), so the very next cluster removed the definitions too. They are
    asserted ABSENT here so this file states which side of that line each name
    landed on instead of silently dropping them.

    INVERTED at S49/S50 (2026-08-01), same rule, the S47 precedent: this pin
    asserted ``operator_takeover_worker`` and ``launcher_visual_cleanup_needed``
    were still callable *definitions* whose bindings S41 had merely unbound.
    That was true at S41 and is now false by ruling — removing this binding is
    exactly what left each module importer-free, so S49/S50 deleted
    ``agent_runtime/operator_control.py`` and
    ``agent_runtime/launcher_process_hygiene.py`` whole. The assertions are
    INVERTED (module absent), never weakened or deleted: a definitions-untouched
    gate that silently loses its subject is how a pin rots into decoration.
    The removals themselves are owned by
    tests/agent_runtime/test_s49_operator_control_removal.py and
    tests/agent_runtime/test_s50_launcher_process_hygiene_removal.py.

    INVERTED again at S56 (2026-08-01) for ``worker_sessions``, by the same rule
    and for the same reason: this pin asserted ``worker_session_summary`` was
    still a callable definition whose ``Path`` binding S41 had merely unbound.
    S56 deleted ``agent_runtime/worker_sessions.py`` whole, so the module is
    asserted ABSENT rather than the pin being dropped. ``states``' companion
    ``WorkerSessionState`` deliberately SURVIVES the cut (``PersonaInstance``
    still types its ``state`` on it) and is pinned below so the two are not
    confused for one another.
    """

    from importlib.util import find_spec

    from agent_runtime import cli_format, scope_control, states, terminal_envelope
    from agent_runtime.decision_schema import DecisionType
    from agent_runtime.profile_runner import RunBudgetExceeded

    assert callable(cli_format.emit_json)
    assert not hasattr(cli_format, "human_task_line")
    assert not hasattr(cli_format, "task_summary")
    assert find_spec("agent_runtime.worker_sessions") is None
    assert states.WorkerSessionState  # the survivor, not the deleted store
    assert find_spec("agent_runtime.operator_control") is None
    assert find_spec("agent_runtime.launcher_process_hygiene") is None
    assert issubclass(RunBudgetExceeded, Exception)
    assert terminal_envelope.LANE_MISSION_CHAT
    assert states.TaskState and states.RunState
    assert DecisionType
    assert callable(scope_control.validate_discovery_payload)


def test_every_touched_module_still_imports():
    for relative in sorted(REMOVED_BINDINGS):
        if relative.endswith("harness_parts/persona_commands.py"):
            continue  # exec'd into harness globals, not importable on its own
        importlib.import_module(relative[:-3].replace("/", "."))
