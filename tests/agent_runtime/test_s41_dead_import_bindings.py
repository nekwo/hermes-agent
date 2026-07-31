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
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest


#: module path -> binding names that must no longer be bound in it.
REMOVED_BINDINGS = {
    "hermes_cli/harness.py": {
        "human_task_line",
        "task_summary",
        "operator_takeover_worker",
        "DecisionType",
        "LegacyOrchestratorRemoved",
        "launcher_visual_cleanup_needed",
        "default_chat_session_id_for_instance",
        "OPERATOR_RESOLVABLE_TURN_STATES",
        "find_discovery_task",
        "TaskState",
        "RunState",
        "worker_session_summary",
    },
    "agent_runtime/persona_runtime.py": {
        "Protocol",
        "AgentDecision",
        "parse_structured_decision",
        "validate_decision_for_role",
        "validate_planning_decision",
        "TERMINAL_ENVELOPE_LANE_MISSION_WORKER",
        "RunBudgetExceeded",
    },
    "agent_runtime/checkpoint.py": {"os"},
    "agent_runtime/observability.py": {"timedelta"},
    "agent_runtime/parity.py": {"paths"},
    "agent_runtime/terminal_envelope.py": {"field", "Sequence"},
    "agent_runtime/worker_sessions.py": {"Path"},
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

    Two exceptions, and they are deliberate rather than a hole in the gate:
    ``cli_format.human_task_line`` / ``task_summary`` were dead in their own
    right (task-record formatters with no reader anywhere once this binding
    went), so the very next cluster removed the definitions too. They are
    asserted ABSENT here so this file states which side of that line each name
    landed on instead of silently dropping them.
    """

    from agent_runtime import cli_format, scope_control, states, terminal_envelope
    from agent_runtime import worker_sessions
    from agent_runtime.decision_schema import DecisionType
    from agent_runtime.launcher_process_hygiene import launcher_visual_cleanup_needed
    from agent_runtime.operator_control import operator_takeover_worker
    from agent_runtime.persona_assignments import default_chat_session_id_for_instance
    from agent_runtime.profile_runner import RunBudgetExceeded

    assert callable(cli_format.emit_json)
    assert not hasattr(cli_format, "human_task_line")
    assert not hasattr(cli_format, "task_summary")
    assert callable(worker_sessions.worker_session_summary)
    assert callable(operator_takeover_worker)
    assert callable(launcher_visual_cleanup_needed)
    assert callable(default_chat_session_id_for_instance)
    assert issubclass(RunBudgetExceeded, Exception)
    assert terminal_envelope.LANE_MISSION_WORKER
    assert states.TaskState and states.RunState
    assert DecisionType
    assert callable(scope_control.validate_discovery_payload)


def test_every_touched_module_still_imports():
    for relative in sorted(REMOVED_BINDINGS):
        if relative.endswith("harness_parts/persona_commands.py"):
            continue  # exec'd into harness globals, not importable on its own
        importlib.import_module(relative[:-3].replace("/", "."))
