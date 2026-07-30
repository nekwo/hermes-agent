"""The permanent ``TaskStore`` stub — DO NOT DELETE (mission-lane removal, ruling R-3).

Why this file exists
--------------------
The goal/task mission lane is being removed (``docs/agent-runtime-harness/
16-mission-lane-removal.md``). Every fork-owned caller of ``TaskStore`` goes with
it — but **one upstream-owned caller does not**::

    tools/board_tool.py::_resolve_board_target
        from agent_runtime.store import board_models, BoardStore, TaskStore, WorkspaceStore

That import is *unguarded* and shares a statement with ``BoardStore`` /
``WorkspaceStore`` / ``board_models``, which are all KEEP. ``tools/board_tool.py``
is upstream-owned: the fork must never edit it, so the import can never be
narrowed at the call site. If the name ``agent_runtime.store.TaskStore`` ever
stops resolving, the whole Mission Board agent-tool path dies at import time —
not just the goal-workspace branch.

So the name has to keep resolving forever, even though there are no tasks left.
This class is that name. **It is not dead code, it is not a migration
scaffold, and it is not waiting to be cleaned up in six months.** Deleting it
breaks ``board_card_add`` / ``board_cards`` for every agent.

The contract
------------
* ``TaskStoreStub()`` constructs, accepting (and ignoring) the ``event_log``
  keyword the real store took, so existing construction sites keep working.
* ``.get(task_id)`` **always raises** :class:`agent_runtime.errors.NotFound`.
  This is the whole behavioural contract. The sole upstream call site is already
  inside ``try: ... except Exception: pass`` and falls through to the active
  workspace, which is the correct post-removal answer: there is no bound goal,
  so resolve the board from the active workspace.
* **Nothing else is provided on purpose.** No ``list_all``, ``create``,
  ``update``, or ``list_for_workspace``. A new caller that wants task data should
  fail loudly with ``AttributeError`` rather than silently receive an empty list
  and conclude the mission lane still works.
"""

from __future__ import annotations

from typing import Any, NoReturn

from .errors import NotFound


class TaskStoreStub:
    """Permanent no-op stand-in for the deleted ``TaskStore`` (see module docstring)."""

    def __init__(self, event_log: Any | None = None) -> None:
        # Accepted and discarded: the real store took an EventLog, and keeping the
        # signature means no caller has to change shape to keep constructing it.
        del event_log

    def get(self, task_id: str) -> NoReturn:
        """Always raise ``NotFound`` — there are no tasks, by design, forever."""

        raise NotFound(str(task_id))
