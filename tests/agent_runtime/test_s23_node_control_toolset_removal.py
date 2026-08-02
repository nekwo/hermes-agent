"""The ``node_control`` toolset entry leaves the upstream-owned ``toolsets.py``.

The block was fork-added: it existed only to expose ``run_node`` / ``steer_node``
from the deleted ``tools/node_control_tool.py``, which dispatched into the
equally deleted ``agent_runtime/node_tools.py``. ``toolsets.py`` itself IS
upstream-owned (it has upstream/main history), so this edit is a documented
boundary crossing made under explicit operator authorization -- see the
upstream boundary ledger.

The surrounding upstream entries are pinned so the surgical block removal cannot
quietly take a neighbour with it.
"""

from __future__ import annotations

from toolsets import TOOLSETS






def test_the_upstream_neighbours_of_the_removed_block_survive():
    # The entries immediately above and below the removed block.
    assert TOOLSETS["session_search"]["tools"] == ["session_search"]
    assert TOOLSETS["project"]["tools"] == [
        "project_list",
        "project_create",
        "project_switch",
    ]
