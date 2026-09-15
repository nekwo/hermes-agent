# Integration checkpoint 6

Incomplete merge recovery checkpoint; main remains unchanged. Cloud handoff was cancelled; work continues locally.

## Reconciliation

Combined downstream and upstream test isolation fixtures, retaining live-service and home guards. Moved the desktop process-table fixture to its extracted owner. Preserved additive regression suites and adapted context-file coexistence and Nous validation guards to the current owners. Reconciled atlas whole-image destriping, neighboring subject protection and salvage diagnostics. Regenerated uv.lock from the upstream lock with the retained coverage and pytest-timeout dependencies.

## Candidate checks

- `scripts/run_tests.sh tests/agent_runtime/test_local_llama_config.py tests/agent_runtime/test_local_llama_provider.py -j 2`: exit 0, 19 passed, 12.7 seconds.
- Remaining six `tests/agent_runtime/test_local_llama_{catalog,manager,process,router,gateway,rpc}.py` via the wrapper: exit 0, 29 passed, 23.3 seconds; raw log `qa-artifacts/candidate-local-llama-stage6.log`.
- Eleven-file reconciliation sweep: exit 1, final runner report 393 passed, 92 failed, 7 skipped, 163.2 seconds. Includes retry after fixture migration; this is not a clean pass. Raw log `qa-artifacts/candidate-reconciled-tests-stage6.log`.
- Narrow tool-search diagnosis: exit 1, missing snowballstemmer in old shared test environment.
- `uv lock --python C:/Users/beast/.venvs/hermes-test/Scripts/python.exe`: exit 0. `uv sync --frozen --extra dev --python ...`: exit 0; created a candidate-only .venv with 92 packages. No shared or live environment synchronized. Four-file rerun against pinned environment in progress at checkpoint.

Outstanding: remaining conflicts, cache-routing test migration, skill collision/path assertions, shell-hook POSIX execution gaps, toolset membership delta, runner retry classification, broader regression and isolated real model probe. No candidate delivery or installer implementation claimed.

## Recovery manifest

```json
{
  "paths": 231,
  "bytes": 1074868,
  "patch_sha256": "47ef47576889b0e7dd30b11e2b7a5442d45a148cf491f68b3af7cc62552eade2",
  "remaining": [
    ".github/workflows/tests.yml",
    "apps/desktop/e2e/sidebar-states.spec.ts",
    "apps/desktop/src/app/chat/sidebar/session-row-state.test.ts",
    "docs/design/profile-builder.md",
    "docs/plans/2026-06-09-003-fix-telegram-stream-overflow-continuations-plan.md",
    "tests/agent/conftest.py",
    "tests/agent/test_api_call_ttfb.py",
    "tests/agent/test_request_assembled_marker.py",
    "tests/cli/test_surrogate_sanitization.py",
    "tests/gateway/test_feishu.py",
    "tests/gateway/test_update_command.py",
    "tests/hermes_cli/test_auth_noninteractive.py",
    "tests/hermes_cli/test_commands.py",
    "tests/hermes_cli/test_dashboard_unified_launch.py",
    "tests/hermes_cli/test_doctor.py",
    "tests/hermes_cli/test_harness_providers.py",
    "tests/hermes_cli/test_kanban_db.py",
    "tests/hermes_cli/test_managed_uv.py",
    "tests/test_hermes_state.py",
    "tests/tools/test_execute_code_approval_cluster.py",
    "tests/tools/test_file_operations.py",
    "tests/tools/test_file_tools.py",
    "tests/tools/test_local_env_blocklist.py",
    "website/docs/developer-guide/gateway-session-lifecycle.md",
    "website/docs/reference/mcp-config-reference.md"
  ]
}
```
