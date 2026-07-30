"""S23 removes the node_control tool pair, dead since the root-node engine went.

``tools/node_control_tool.py`` registered ``run_node`` / ``steer_node`` with a
handler and a check_fn that both did
``__import__("agent_runtime.node_tools", ...)``. ``agent_runtime/node_tools.py``
was deleted with the dispatch loop (doc 16, S5), so every call and every
availability probe on this pair could only raise ``ModuleNotFoundError``. The
tool file is fork-authored (no upstream history); the registration block it
required in the upstream-owned ``toolsets.py`` is removed under explicit
operator authorization.
"""

from __future__ import annotations

import importlib.util


def test_the_node_control_tool_module_is_gone():
    assert importlib.util.find_spec("tools.node_control_tool") is None


def test_node_tools_stays_gone_so_the_pair_could_never_dispatch():
    # The reason the pair is dead, pinned so a future reader does not have to
    # re-derive it: the module both lambdas imported does not exist.
    assert importlib.util.find_spec("agent_runtime.node_tools") is None


def test_neither_tool_name_is_registered():
    from tools.registry import discover_builtin_tools, registry

    # Run the real discovery pass so this asserts against what the registry
    # actually resolves, not against whatever a prior import happened to load.
    discovered = discover_builtin_tools()
    assert "tools.node_control_tool" not in discovered

    names = set(registry.get_all_tool_names())
    assert "run_node" not in names
    assert "steer_node" not in names
