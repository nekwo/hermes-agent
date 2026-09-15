# Integration checkpoint 4 — 2026-09-15

IN PROGRESS. Cumulative recovery patch relative to pinned upstream; not a runnable candidate. Preserve the pending merge worktree. No combined behavioral tests have run; prior 394 tests and real llama probe remain ORIGINAL FORK evidence only.

## Resolutions

- Prompt policy preserves runtime skill eligibility, required skills, deterministic context-file composition, and downstream tool/Windows guidance while using current upstream prompt assembly.
- Runtime dependency installation barriers remain enforced at shared install entry points. Upstream file-sync temp cleanup is equivalent; credential archive paths keep POSIX separators. No install or service action was executed.
- Windows gateway wrapper pinning, shared Tirith policy, MCP root interpolation and one-shot stdio environment validation, CLI command aliases, and plugin discovery timing survive upstream owner moves.
- Gateway background completions remain visibility-only by default, with explicit opt-in for agent turns. Queue status, bounded completion/heartbeat messages, reply targets, PM watcher, claim TTL, and durable startup completions are retained at new upstream owners. Upstream reconnection supervision and event ownership remain.
- Kanban crash evidence/redaction moves to hermes_cli/kanban_crash_evidence.py and joins upstream reclaim event/run payloads. TTL flows through the new dispatch/daemon parser and settings owners.
- Profile deletion retains the typed fail-closed process-inspection refusal and explicit override, using upstream process matchers, deletion tombstones, resource closure and cleanup. Read-only template enumeration and persona orphan marking remain.
- Model-tool schema receipts, blocked tool names and always-available detail lookup are reconciled with upstream profile-scoped synchronized caching. Availability checks and registry conflicts still require resolution.
- TTS config remains private/read-only; Windows same-file transcode protection retained. Vision uses upstream native-image/aux-video gates and shared probe mode, with read-only config and brief schema description. Memory plugin loading keeps symmetric parent-package publication/rollback.

## Remaining work

Resolve remaining product and test conflicts; migrate original tests to actual new owners without weakening behavioral assertions; review renamed tool aliases and claims; run focused scripts/run_tests.sh regressions, isolated llama/model/tool roundtrip, and finalize installer contract handoff. Do not implement the installer. Main is unchanged.

The operator briefly requested a cloud handoff then cancelled it. The attempted temporary-index snapshot failed on ignored upstream tracked paths before creating a commit or branch; it did not alter the real merge index or files. Continue locally. No cloud handoff was pushed.

Manifest: 183 paths. Patch SHA-256: `59f8ad5e8712f997190a3acdce870d9195459e97d1a7c9459ea09167977038b1`. Remaining unmerged paths: 60.

- `.github/workflows/tests.yml`
- `agent/pet/generate/atlas.py`
- `apps/desktop/e2e/sidebar-states.spec.ts`
- `apps/desktop/src/app/chat/sidebar/session-row-state.test.ts`
- `docs/design/profile-builder.md`
- `docs/plans/2026-06-09-003-fix-telegram-stream-overflow-continuations-plan.md`
- `hermes_cli/gateway.py`
- `pyproject.toml`
- `scripts/check_subprocess_stdin.py`
- `scripts/run_tests_parallel.py`
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
- `tools/approval.py`
- `tools/browser_tool.py`
- `tools/environments/local.py`
- `tools/file_tools.py`
- `tools/mcp_tool.py`
- `tools/process_registry.py`
- `tools/registry.py`
- `tools/skills_tool.py`
- `tools/terminal_tool.py`
- `tools/tool_search.py`
- `toolsets.py`
- `uv.lock`
- `website/docs/developer-guide/gateway-session-lifecycle.md`
- `website/docs/reference/mcp-config-reference.md`
