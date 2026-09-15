# Integration checkpoint 5 — 2026-09-15

IN PROGRESS. Cumulative recovery patch relative to pinned upstream. Do not run this unfinished candidate as a service or abort/reset its active merge. Main remains unchanged.

## Reconciled behavior

- Registry keeps upstream profile-scoped cache, secret-scope failures and plugin override protections. Added fork probe counters, static tool-name scanning and invalidation epoch; short grace re-probe caching is clamped to the last real success.
- Approval guards and browser daemon ownership checks use downstream path identity at upstream's new owners. Shared Tirith overrides now read config without creating runtime homes. File/device/config protections retain POSIX matching on Windows.
- MCP transient environment/root resolution lives in agent_runtime/mcp_environment.py; current config/transport owners retain scoped secrets, delegation env restrictions and command resolution.
- Process completion requests and wait policy live in agent_runtime/process_notifications.py. Upstream finished-result persistence, ownership/handoff fields and handle closure remain; explicit notify requests survive inline consumption and use redacted output. Shared checkpoint home and Windows EOF behavior are retained. Upstream already aliases legacy process/todo/cronjob names during tool execution; downstream literal-name consumers still need tests.
- Tool search retains upstream batched queries, connector search and validation. Fork eager-only promotions, top-three bounded parameter schemas, and full documentation for session-granted tools remain. Legacy single query/name callers retain their response form. Details lookup is present even without deferral.
- Terminal policy moves to agent_runtime/terminal_policy.py. The wrapper gates all upstream execution exits, attaches grant provenance and forwards the new _host_local argument. Persona execution scope and interactive-menu prevention remain. New upstream execution planning/backend isolation is preserved outside that explicit scope.
- Skills use canonical shared resolution and runtime compatibility, while upstream project precedence/quarantine still applies. Compact skill search moved downstream; skill_search remains in core/skills bundles and harness_core is retained. Metadata includes content hash/provenance and portable paths.
- Gateway process matching combines canonical upstream flag parsing with bounded home matching. Managed interpreter refusal and boot home receipt remain. No gateway was started or stopped.
- Test runner retains eight-worker default, coverage wrapper, cross-platform drive lists, isolated timeout retry and upstream host markers. Project config keeps new upstream dependencies/lints plus fork timeout, coverage and F821 requirements. uv.lock reconciliation is pending.
- Stdin guard uses upstream AST kwargs detection (same intended guard, stronger than the fork's nearby-line scan) plus portable path comparisons.

## Verification

AST parsing on Python files in the manifest. TOML parsed successfully. Focused undefined-name check initially found five missing names; repaired all five, then `python -m ruff check --isolated --select F821` over 19 touched implementation files exited 0 (All checks passed). No candidate regression tests have run. The original 394-test and real llama baseline remains baseline only.

Remaining: product atlas conflict, workflow/docs/test conflicts, lockfile, focused wrapper regressions and fixtures migrated to new owners, real isolated llama probe, installer contract reassessment, final history-preserving merge and safe ff-only delivery. Preserve every intended behavior; do not weaken tests to accept drops.

Manifest paths: 211. Patch SHA-256: `f77e4864fb00ec726291e70124b193d14d215c3d557c1836e1b09feb43cf0396`. Unmerged paths: 45.

- `.github/workflows/tests.yml`
- `agent/pet/generate/atlas.py`
- `apps/desktop/e2e/sidebar-states.spec.ts`
- `apps/desktop/src/app/chat/sidebar/session-row-state.test.ts`
- `docs/design/profile-builder.md`
- `docs/plans/2026-06-09-003-fix-telegram-stream-overflow-continuations-plan.md`
- `tests/agent/conftest.py`
- `tests/agent/test_api_call_ttfb.py`
- `tests/agent/test_prompt_builder.py`
- `tests/agent/test_request_assembled_marker.py`
- `tests/agent/test_shell_hooks.py`
- `tests/agent/test_system_prompt.py`
- `tests/agent/test_turn_finalizer_final_response_persistence.py`
- `tests/agent/transports/test_codex_transport.py`
- `tests/cli/test_surrogate_sanitization.py`
- `tests/conftest.py`
- `tests/gateway/test_background_process_notifications.py`
- `tests/gateway/test_feishu.py`
- `tests/gateway/test_update_command.py`
- `tests/hermes_cli/conftest.py`
- `tests/hermes_cli/test_auth_noninteractive.py`
- `tests/hermes_cli/test_commands.py`
- `tests/hermes_cli/test_dashboard_unified_launch.py`
- `tests/hermes_cli/test_doctor.py`
- `tests/hermes_cli/test_harness_providers.py`
- `tests/hermes_cli/test_kanban_db.py`
- `tests/hermes_cli/test_managed_uv.py`
- `tests/hermes_cli/test_nous_inference_url_validation.py`
- `tests/scripts/test_run_tests_parallel.py`
- `tests/test_hermes_constants.py`
- `tests/test_hermes_state.py`
- `tests/tools/test_approval.py`
- `tests/tools/test_browser_content_none_guard.py`
- `tests/tools/test_execute_code_approval_cluster.py`
- `tests/tools/test_file_operations.py`
- `tests/tools/test_file_tools.py`
- `tests/tools/test_local_env_blocklist.py`
- `tests/tools/test_skills_tool.py`
- `tests/tools/test_terminal_tool.py`
- `tests/tools/test_terminal_tool_requirements.py`
- `tests/tools/test_tool_search.py`
- `tests/tools/test_toolsets.py`
- `uv.lock`
- `website/docs/developer-guide/gateway-session-lifecycle.md`
- `website/docs/reference/mcp-config-reference.md`
