# Planned — retire the task-bound vocabulary

**Owner domain:** system architecture ([01-system-architecture.md](../01-system-architecture.md))
**Status:** not done. The task lane was removed 2026-07-30; three of its names
still write and read on the live chat lane.
**Raised / verified:** 2026-08-22 against HEAD.

## What is true today

There are no tasks. `agent_runtime.store.TaskStore` is
`TaskStoreStub` (`agent_runtime/store.py:149`), whose `.get()` is `-> NoReturn`
and raises `NotFound` unconditionally (`agent_runtime/task_store_stub.py:54`).

Three task-shaped names nevertheless remain live:

1. **`PersonaInstance.mode == "task_bound"` is still WRITTEN.** Passing
   `--goal <id>` to `harness persona instance steer` reaches
   `_apply_steer_edges`, which on a non-empty `goal_id` sets
   `updates["mode"] = "task_bound"` and `updates["current_task_id"] = goal_id`
   (`agent_runtime/persona_assignments.py:1086-1096`). The CLI's own help text
   says `goal_id` is a *correlation id* the Launcher groups agent rooms by
   (`hermes_cli/harness.py:1002`) — so a grouping label silently flips an
   instance into a mode named after a lane that no longer exists.
2. **That mode is then READ as if it meant something.** The chat-history
   projection skips `task_bound` rows in three places
   (`agent_runtime/persona_chat_history.py:308`, `:455`, `:774`), the session
   repair skips them (`persona_assignments.py:932`), and the identity
   reconciler branches on them (`persona_instance_identity.py:342`). An
   operator who used `--goal` for grouping loses chat-history projection for
   that instance.
3. **`TaskStoreStub` no longer has the caller its docstring names.** The stub
   exists, per ruling R-3, because `tools/board_tool.py::_resolve_board_target`
   imported `TaskStore` unguarded in a shared import statement
   (`task_store_stub.py:6-22`). That is no longer so: `_resolve_board_target`
   today imports only `board_models`, `BoardStore` and `WorkspaceStore`
   (`tools/board_tool.py:82-84`) and states in a comment that the bound-goal
   rung is gone (`:92-96`, "no longer participates in resolution at all"). A
   repo-wide grep finds **no** constructor or `.get`
   call outside the stub's own module and the re-export — the name is kept
   alive by one `from … import … as TaskStore` line and nothing else.

## Why it was not just deleted

R-3 made the stub permanent, and permanence was the right call while the
upstream import stood. The import is what changed, not the ruling — so
reversing it needs an operator decision, not a cleanup commit.

## Gate to open this

1. Operator ruling on R-3, now that its stated cause is gone. Either the stub
   stays as insurance against a future upstream re-import (then its docstring
   must be corrected to say so — it currently asserts a false present fact), or
   it and the `store.py:149` re-export go.
2. A decision on `--goal`: keep it as a pure correlation id and stop stamping
   `mode`/`current_task_id`, or rename the flag to what it is. Either way the
   five readers above need retargeting in the same change, because dropping
   the write while leaving the reads makes persisted `task_bound` rows
   permanently invisible.
3. A migration pass over persisted instances already stamped `task_bound`.
