# Stage 7 — Upstream MCP stdio environment overrides

> **STATUS: SHIPPED.** This plan has landed on `main` — do not re-implement it.
> The delivered surface is `_normalize_mcp_env_server_name` / `_get_process_mcp_env_overrides`
> and `_build_safe_env(..., server_name=, runtime_env=)` in [`tools/mcp_tool.py`](../../../tools/mcp_tool.py),
> threaded through `MCPServer._run_stdio()`; the `hermes mcp test --env KEY=VALUE`
> flag in [`hermes_cli/subcommands/mcp.py`](../../../hermes_cli/subcommands/mcp.py);
> coverage in [`tests/tools/test_mcp_env_overrides.py`](../../../tests/tools/test_mcp_env_overrides.py);
> user docs in [`website/docs/reference/mcp-config-reference.md`](../../../website/docs/reference/mcp-config-reference.md).
> Everything below the "Current behavior" heading is retained as the design
> record of how it was built, and describes the pre-implementation state.
>
> **For Claude:** Implement this from a fresh branch based on `upstream/main`, not from Tony's local migration branch. Keep the PR generic to Hermes Agent; do not include Eternia, Arcadia, Launcher, Stage C, TonyBrain, or private profile artifacts in the upstream commit.
>
> Parent docs: [`README.md`](README.md), [`../mcp-expansion-roadmap.md`](../mcp-expansion-roadmap.md).
> Upstream contribution guide: <https://hermes-agent.nousresearch.com/docs/developer-guide/contributing>

## Goal

Add a safe, upstream-worthy way to run Hermes MCP discovery/tool calls with **per-run stdio environment overrides** without permanently editing `~/.hermes/config.yaml`.

This closes a real tooling-parity gap: some stdio MCP servers need short-lived runtime values produced immediately before discovery, such as temp file paths, nonces, local ports, or one-shot credentials. Today Hermes only passes the safe baseline env plus the durable `mcp_servers.<name>.env` block, so operators must either mutate profile config or build ad-hoc wrappers.

## Scope classification

- **Type:** upstream-targeted Hermes Agent feature.
- **Target branch:** fresh branch from `upstream/main`.
- **Expected PR shape:** one focused feature commit or small commit stack with tests and docs.
- **Do not include:** private Stage C artifacts, Launcher paths, generated screenshots, local `.hermes` config, auth files, secrets, or Tony-specific brain notes.

## Current behavior

Relevant files in Hermes Agent:

- `tools/mcp_tool.py`
  - `_build_safe_env(user_env)` filters `os.environ` down to safe baseline keys and `XDG_*`, then merges explicit durable config env.
  - `MCPServer._run_stdio()` reads `config.get("env")`, builds the filtered env, resolves the command, and passes the result to `StdioServerParameters(env=...)`.
- `hermes_cli/mcp_config.py`
  - `hermes mcp test <server>` calls `_probe_single_server(name, config)` using server config loaded from `config.yaml`.
  - `hermes mcp add ... --env KEY=VALUE` already supports durable env entries.
- Existing MCP tests live under `tests/tools/test_mcp_*.py` and `tests/hermes_cli/test_mcp_*.py`.
- User docs live under `website/docs/user-guide/features/mcp.md`, `website/docs/guides/use-mcp-with-hermes.md`, and `website/docs/reference/mcp-config-reference.md`.

## Problem to solve

Hermes' env filtering is correct for security, but there is no first-class non-mutating override layer for one run.

Required semantics:

1. Durable `mcp_servers.<name>.env` continues to work exactly as today.
2. Hermes should allow an operator or automation to pass extra stdio env values for a single discovery/test/session.
3. These values must be merged after the safe baseline and durable server env.
4. The final env must not be written back to `config.yaml` unless the operator explicitly uses existing durable config commands.
5. Secret-like values must never be printed in full in CLI output or error messages.

## Proposed design

Implement two generic mechanisms. If time is tight, mechanism A is required and mechanism B is optional but recommended.

### Mechanism A — CLI one-shot env overlays for `hermes mcp test`

Add repeatable flags to the MCP test path:

```bash
hermes mcp test <server-name> --env KEY=VALUE --env OTHER=value
```

Behavior:

- Parse with the existing `_parse_env_assignments()` helper in `hermes_cli/mcp_config.py`.
- Validate variable names with the existing `_ENV_VAR_NAME_RE`.
- Merge into the copied server config for this test only.
- Do not persist the overlay.
- CLI output may say `Applied 2 one-shot env overrides`, but must not print values.
- If a key already exists in durable `env`, the one-shot value wins for this run only.

### Mechanism B — process-level namespaced overlays for all MCP discovery paths

Add process env support so wrappers and automation can pass one-shot values without CLI flags:

```bash
HERMES_MCP_ENV_<SERVER>_<KEY>=value hermes mcp test <server-name>
```

Name normalization:

- `<SERVER>` is the server name uppercased with non-alphanumeric characters converted to `_`.
  - `launcher-qa` -> `LAUNCHER_QA`
  - `stagec-launcher-qa` -> `STAGEC_LAUNCHER_QA`
- `<KEY>` must still match `_ENV_VAR_NAME_RE` after parsing.
- Example input: `HERMES_MCP_ENV_LAUNCHER_QA_RUNTIME_FILE=/tmp/runtime.json`
- Resulting child env key: `RUNTIME_FILE=/tmp/runtime.json`

Merge order for stdio MCP subprocess env:

1. Safe baseline from `os.environ` (`PATH`, `HOME`, etc. and `XDG_*`).
2. Durable server config `env` from `config.yaml`.
3. Process-level namespaced overlay (`HERMES_MCP_ENV_<SERVER>_<KEY>`).
4. Explicit CLI `--env KEY=VALUE` overlay, when present.

This preserves security because Hermes still does not blindly inherit arbitrary parent env vars; only explicitly namespaced MCP overrides cross the filter.

## Implementation tasks

### Task 1: Add env overlay helpers

**Objective:** Create small, testable helpers for server-name normalization and overlay extraction.

**Files:**

- Modify: `tools/mcp_tool.py`
- Test: `tests/tools/test_mcp_tool.py` or a new focused file `tests/tools/test_mcp_env_overrides.py`

**Implementation notes:**

Add helpers near `_build_safe_env()`:

```python
def _normalize_mcp_env_server_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", str(name or "")).upper()


def _get_process_mcp_env_overrides(server_name: str) -> dict[str, str]:
    normalized = _normalize_mcp_env_server_name(server_name)
    prefix = f"HERMES_MCP_ENV_{normalized}_"
    overrides = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        child_key = key[len(prefix):]
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", child_key):
            logger.warning(
                "Ignoring invalid MCP env override name for server %r: %r",
                server_name,
                child_key,
            )
            continue
        overrides[child_key] = value
    return overrides
```

Do not log values.

### Task 2: Extend safe env construction

**Objective:** Merge process-level overlays after durable config env.

**Files:**

- Modify: `tools/mcp_tool.py`
- Test: `tests/tools/test_mcp_env_overrides.py`

**Implementation notes:**

Prefer changing `_build_safe_env` to accept `server_name` and optional `runtime_env`:

```python
def _build_safe_env(
    user_env: Optional[dict],
    *,
    server_name: Optional[str] = None,
    runtime_env: Optional[dict] = None,
) -> dict:
    env = {}
    for key, value in os.environ.items():
        if key in _SAFE_ENV_KEYS or key.startswith("XDG_"):
            env[key] = value
    if user_env:
        env.update(user_env)
    if server_name:
        env.update(_get_process_mcp_env_overrides(server_name))
    if runtime_env:
        env.update(runtime_env)
    return env
```

Then update `_run_stdio()`:

```python
user_env = config.get("env")
runtime_env = config.get("runtime_env")
safe_env = _build_safe_env(user_env, server_name=self.name, runtime_env=runtime_env)
```

`runtime_env` is intentionally in-memory only; do not document it as durable config.

### Task 3: Add CLI `--env` to `hermes mcp test`

**Objective:** Allow `hermes mcp test <server> --env KEY=VALUE` without persisting the env.

**Files:**

- Modify: `hermes_cli/mcp_config.py`
- Test: `tests/hermes_cli/test_mcp_config.py` or a new focused file `tests/hermes_cli/test_mcp_test_env_overrides.py`

**Implementation notes:**

Find the MCP subparser for `test`. Add:

```python
test_parser.add_argument(
    "--env",
    action="append",
    default=[],
    metavar="KEY=VALUE",
    help="One-shot environment variable for this MCP test run; not saved to config.yaml.",
)
```

In the test handler, parse it with `_parse_env_assignments(args.env)`, copy the target server config, attach `runtime_env`, and call `_probe_single_server()` with the copied config.

Pseudo-flow:

```python
runtime_env = _parse_env_assignments(getattr(args, "env", None))
server_config = dict(servers[name])
if runtime_env:
    server_config["runtime_env"] = runtime_env
    _info(f"Applied {len(runtime_env)} one-shot env override(s) for this test run")
_probe_single_server(name, server_config, connect_timeout=...)
```

Do not print `KEY=VALUE`. Printing the keys is acceptable but not necessary.

### Task 4: Add behavior-focused tests

**Objective:** Lock the security and merge behavior.

**Files:**

- Create or modify: `tests/tools/test_mcp_env_overrides.py`
- Create or modify: `tests/hermes_cli/test_mcp_test_env_overrides.py`

Required test cases:

1. `_normalize_mcp_env_server_name("stagec-launcher-qa") == "STAGEC_LAUNCHER_QA"`.
2. `_get_process_mcp_env_overrides("foo-bar")` maps `HERMES_MCP_ENV_FOO_BAR_RUNTIME_FILE=...` to `{"RUNTIME_FILE": "..."}`.
3. Invalid child env names are ignored and do not leak values to logs.
4. `_build_safe_env()` merge order is safe baseline < durable env < process overlay < runtime env.
5. `hermes mcp test <server> --env KEY=VALUE` passes `runtime_env` to `_probe_single_server()` but does not call `save_config()`.
6. CLI output does not include the raw secret value when `--env TOKEN=super-secret-value` is used.

### Task 5: Update docs

**Objective:** Make the feature discoverable and explain the security model.

**Files:**

- Modify: `website/docs/reference/mcp-config-reference.md`
- Modify: `website/docs/guides/use-mcp-with-hermes.md` or `website/docs/user-guide/features/mcp.md`

Add docs for:

- Durable server env in `config.yaml` remains the normal long-lived setup.
- `hermes mcp test <name> --env KEY=VALUE` is for one-shot testing.
- `HERMES_MCP_ENV_<SERVER>_<KEY>` is for automation/wrappers that need one-shot values across discovery paths.
- Hermes intentionally filters arbitrary parent env; the namespaced prefix is the opt-in security boundary.
- Do not put secrets in shell history; prefer temp files or existing secret managers for sensitive values.

### Task 6: Run validation

**Objective:** Prove the branch is ready for upstream review.

Run at minimum:

```bash
pytest tests/tools/test_mcp_env_overrides.py -v
pytest tests/hermes_cli/test_mcp_test_env_overrides.py -v
pytest tests/tools/test_mcp_tool.py -q
pytest tests/hermes_cli/test_mcp_config.py -q
git diff --check
```

If the repo has the standard test wrapper available and time allows, also run:

```bash
scripts/run_tests.sh
```

### Task 7: Commit and handoff

**Objective:** Leave a targeted upstream-ready commit.

Commands:

```bash
git fetch upstream
git switch -c feat/mcp-stdio-env-overrides upstream/main
# apply implementation + tests + docs
git status --short
git add tools/mcp_tool.py hermes_cli/mcp_config.py tests website/docs
git commit -m "feat(mcp): support one-shot stdio env overrides"
```

Handoff must include:

- Branch name.
- Commit hash.
- Files changed.
- Tests run with exit codes.
- Explicit statement: no private Launcher/Eternia/Arcadia artifacts included.
- PR body draft following the upstream contribution guide.

## Acceptance criteria

This stage is complete when:

- `hermes mcp test <server> --env KEY=VALUE` works for stdio MCP servers and does not persist `KEY` or `VALUE` to config.
- Namespaced process env overlays work for stdio MCP subprocess env without passing arbitrary parent env vars.
- Merge precedence is covered by tests.
- Secret-like values are redacted from errors and not printed by normal CLI status output.
- Existing MCP behavior is backward-compatible for configs that only use durable `env`.
- Docs explain durable env vs one-shot env vs namespaced automation env.
- The implementation branch is based on `upstream/main` and contains no unrelated local migration commits.

## Out of scope

- Shipping an Eternia Launcher MCP server upstream.
- Adding product-specific Stage C scripts to Hermes Agent.
- Persisting one-shot env overlays in `config.yaml`.
- A generic launcher/process supervisor framework.
- Changing HTTP/SSE MCP header behavior.
- Changing OAuth handling.

## Risk checklist

- **Secret leakage:** do not log overlay values; keep existing `_sanitize_error()` path intact.
- **Unsafe env inheritance:** do not pass the full parent env to MCP subprocesses.
- **Config mutation:** one-shot `--env` must not call `save_config()`.
- **Windows shell quirks:** document that automation env values may be easier through a script file than inline PowerShell/Bash mixing.
- **Backward compatibility:** existing `mcp_servers.<name>.env` configs must behave exactly as before.
