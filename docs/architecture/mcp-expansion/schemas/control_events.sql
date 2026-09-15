-- control_events: audit log for non-task control-plane mutations and reads.
-- Lives in the shared kanban DB (~/.hermes/kanban.db for the default board, or
-- ~/.hermes/kanban/boards/<slug>/kanban.db). Sibling to task_events.
--
-- Cited by:
--   docs/architecture/mcp-expansion/08-second-pass-audit-and-expansion.md §R1
--   docs/architecture/mcp-expansion/11-stage-2.5-hermes-control-mutate.md
--   docs/architecture/mcp-expansion/09-stage-4.5-arcadia-pm-mcp.md
--   docs/architecture/mcp-expansion/10-stage-6-eternia-backend-mcp.md

CREATE TABLE IF NOT EXISTS control_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,

    -- 'control_mcp_read' | 'control_mcp_mutation'
    -- 'pm_route'         | 'pm_classify' | 'pm_card_sprawl_override'
    -- 'brain_mcp_call'   (Stage 3 uses .brain-mutation-log.jsonl primarily;
    --                     this row is the cross-vault audit trail)
    -- 'agentops_spawn'   | 'agentops_reap' | 'agentops_kill'
    -- 'release_classify' | 'release_create_closure'
    -- 'backend_mcp_call'
    kind            TEXT NOT NULL,

    -- Fully-qualified MCP tool name, e.g. 'hermes_kanban_block'.
    tool            TEXT NOT NULL,

    -- Profile of the *caller* (the orchestrator session), not the worker
    -- that may eventually be spawned as a result.
    caller_profile  TEXT NOT NULL,

    -- Session id of the caller. Short id, not the full UUID, to keep rows
    -- legible in tail listings.
    caller_session  TEXT,

    -- JSON-encoded args, AFTER redaction. Never store raw secrets here.
    args_json       TEXT NOT NULL,

    -- One of the canonical error classes, or 'ok'.
    -- See docs/architecture/mcp-expansion/07-cross-cutting.md §2.
    result_class    TEXT NOT NULL,

    -- True if dry_run=true. Dry-run rows ARE recorded — they document
    -- what *would* have happened.
    dry_run         INTEGER NOT NULL CHECK (dry_run IN (0, 1)),

    -- Optional kanban card id when the call related to one. Foreign key
    -- to tasks(id) is NOT enforced — control events may outlive the task.
    task_id         TEXT,

    -- Unix epoch seconds.
    created_at      INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_control_events_created
    ON control_events(created_at);

CREATE INDEX IF NOT EXISTS idx_control_events_caller
    ON control_events(caller_profile, created_at);

CREATE INDEX IF NOT EXISTS idx_control_events_tool
    ON control_events(tool, created_at);

CREATE INDEX IF NOT EXISTS idx_control_events_task
    ON control_events(task_id) WHERE task_id IS NOT NULL;

-- Retention: the profile's curator daemon prunes rows older than
-- curator.archive_after_days (default 90). The prune query lives in
-- the kanban_db maintenance path; do not add a TTL trigger here —
-- triggers cost a write-amplification penalty on every insert.
