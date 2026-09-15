# Upstream tool dividend — what NousResearch/hermes-agent has that this fork does not

**Status: MEASURED 2026-09-03, PLANNED — no code touched.** Evidence note for the
Mission Control queue row of the same name (Launcher Brain,
`20 — Active Initiatives/mission-control-queue.md`). Measured against the
`upstream` remote at `562ee8ab76` from fork main `99c8fa5725`; the fork point is
`126ff7071b` (2026-07-31). Operator, 2026-09-03: *"add the missing upstream ones
as planned todo in the brain."*

## How it was measured (reproduce before acting)

```bash
git fetch upstream
git merge-base HEAD upstream/main                       # 126ff7071b
git rev-list --count HEAD..upstream/main                # 7927
comm -13 <(grep -rho -E '^\s*"name": *"[a-z_0-9]+"' tools/*.py | sed -E 's/.*"name": *"([a-z_0-9]+)".*/\1/' | sort -u) \
         <(git grep -h -E '^\s*"name": *"[a-z_0-9]+"' upstream/main -- 'tools/*.py' | sed -E 's/.*"name": *"([a-z_0-9]+)".*/\1/' | sort -u)
```

The name diff is over the tool SCHEMAS (`"name":` keys in `tools/*.py`), not over
files, because a file rename is not a capability change and a schema is. Counts:
fork 87 names, upstream 93, upstream-only 26, fork-only 20.

## The 26 upstream-only names, grouped

| group | names | what it is |
|---|---|---|
| renames | `cronjob_manage`, `process_manage`, `todo_list` | our `cronjob`, `process`, `todo` under new names; same function. A merge must map the old names in every profile, skill, and test that spells them. |
| preview family | `open_preview` (kept), `read_preview`, `annotate_preview`, `close_preview`, `drive_preview`, `desktop_preview`, `apply_layout`, `read_window_below` | in-app preview pane verbs for upstream's desktop GUI sessions; `desktop_ui` toolset (GUI sessions only). |
| onboarding + diagnostics | `gui_tour`, `show_tip`, `setup_mcp`, `cli_doctor`, `health_report_path`, `platform_supported`, `binary_version`, `session_active` | first-run and self-diagnosis verbs. `setup_mcp` is the one worth reading against our profile-declared MCP admission (S64/S66) before adopting. |
| macOS permission probes | `tcc_accessibility`, `tcc_screen_recording`, `ax_capability` | TCC consent checks; relevant to the Mac install once S3-FW (host firewall) lands, same class of "ask the OS, confirm by re-probe". |
| family additions | `browser_exec`, `kanban_request_review`, `kanban_request_changes` | one browser verb; two kanban review verbs (kanban is withheld here by registry hygiene — see the instant-pairing plan's R-IP13). |
| new toolsets | `bot_room`, `desktop_ui` | `bot_room` = "verified text-only Group Chat turn capabilities" (empty tool list, a capability marker); `desktop_ui` = the preview/pane family above. |

Upstream-only tool MODULES that carry no schema but matter for a merge:
`code_kernel.py` / `code_kernel_remote.py` (execute_code's kernel split),
`subagent_worktree.py` (delegate_task in a git worktree), `terminal_scope.py` /
`terminal_hints.py` / `shell_heredoc.py` (terminal hardening),
`spill_safety.py`, `self_repo_guard.py`, `plugin_guard.py`,
`mcp_death_supervisor.py` / `mcp_schema_cache.py` (our `mcp_stdio_watchdog.py`
is the fork's answer to the first), `skill_ledger.py` / `skill_linter.py` /
`skillevaluator_scan.py`, `web_result_cache.py`, `delegation_output_schema.py`.

## The 20 fork-only names (what a merge must not lose)

`agent_chat_send`, `agent_chat_dispatches`, `agent_chat_threads`,
`agent_chat_open`, `agent_chat_log_path` (the cross-agent lane, `tools/agent_chat_tool.py`);
`board_cards`, `board_card_add` (`tools/board_tool.py`); the six `bfl_flux3_*`
video tools (`tools/flux3_video_tool.py`); `project_create` / `project_list` /
`project_switch`; `skill_search`; and the three pre-rename verbs `cronjob`,
`process`, `todo`. Fork-only modules with no schema: `agent_chat_dispatch.py`,
`path_identity.py`, `process_notify_store.py`, `tool_full_descriptions.py`,
`mcp_stdio_watchdog.py`.

## Recommendation

- **Nothing an agent needs to write or run code is missing.** The coding core
  (`read_file`, `search_files`, `write_file`, `patch`, `terminal`,
  `execute_code`) is identical by name on both sides.
- **Collect it in a merge session, not tool by tool.** 7,927 commits since the
  fork point; the SessionDB dividend note already records that a merge is
  nearly free in the runtime-data domain. Cherry-picking 26 schemas out of that
  would re-solve the merge's conflicts one file at a time.
- **Sequence it after the atlas cleanup (instant-pairing plan S0a).** Adopting
  the three renames and `setup_mcp` onto a profile that still admits the
  `hermes-cli` alias would widen a surface the operator has asked to narrow
  first. After S0a the `harness_core` toolset names what we take.
- **Pre-merge checklist (the row's acceptance):** rename map applied in
  profiles, skills, and tests; the fork-only 20 present and admitted after
  merge; `harness persona tool-diff <persona> --json` per persona shows
  `withheld_tools == 0` and `requirement_failures == 0` with the S0a counts
  moved only by names the merge intentionally added; `bot_room` and
  `desktop_ui` NOT admitted on the harness lane (GUI-session toolsets).
