# Stage 6 — Hermes CLI + Profile Integration

## Goal

Expose the harness through Hermes commands and profile/persona config while keeping the runtime modular and testable.

Stage 6 is the first user-facing control surface. It must remain thin: CLI parses arguments, calls `agent_runtime` library functions, prints JSON/human summaries, and exits.

## Deep audit findings from current repo

### CLI parser architecture

- `hermes_cli/main.py` is the top-level CLI entrypoint and already wires many subcommands.
- Kanban has a dedicated CLI module (`hermes_cli/kanban.py`) and parser/dispatcher pattern worth copying at the module-boundary level.
- `cli.py` is very large and mostly interactive UI/runtime. Do not add harness logic there.
- `hermes_cli/commands.py` is for slash/gateway command registry, not necessarily top-level argparse groups.

### Config/profile architecture

- `hermes_constants.py` provides profile-aware `get_hermes_home()` and shared-root `get_default_hermes_root()` behavior.
- Stage 1 `agent_runtime/paths.py` already uses `get_default_hermes_root()` unless `HERMES_AGENT_RUNTIME_ROOT` overrides it.
- `config.yaml` already has nested config sections and profile-specific overrides.
- Personas should inherit model/provider/api_mode defaults from profile config but task state must stay in the shared root.

### Testing patterns

- Hermes has CLI tests and parser tests; Stage 6 should add tests without invoking live model providers.
- Existing `scripts/run_tests.sh` expects a venv in this checkout, but direct `python -m pytest` has been used for current work when no venv exists.

## Package additions

```text
hermes_cli/
  harness.py             # build_parser(), harness_command(), subcommand handlers

agent_runtime/
  config.py              # load runtime config from profile config + defaults
  cli_format.py          # JSON/human formatting helpers, no argparse
```

Keep `hermes_cli/main.py` changes to a tiny import + parser registration + dispatcher hook.

## CLI command surface

Initial command group:

```bash
hermes harness init
hermes harness task create --title "..." --description "..." [--json]
hermes harness task list [--state open|all|done|blocked] [--json]
hermes harness task show <task_id> [--json]
hermes harness tick [--once] [--json]
hermes harness status [--json]
hermes harness agents [--json]
hermes harness proof list <task_id> [--json]
hermes harness incident list [--open] [--json]
```

Future aliases can be added later:

```bash
hermes mission ...
```

Do not rename before the core is proven.

## Parser design

`hermes_cli/harness.py` should expose:

```python
def build_parser(parent_subparsers) -> None: ...
def harness_command(args) -> int: ...
```

Subcommands dispatch to small handlers:

```python
def _task_create(args) -> int: ...
def _task_list(args) -> int: ...
def _task_show(args) -> int: ...
def _tick(args) -> int: ...
def _status(args) -> int: ...
```

Rules:

- All handlers return process exit code.
- `--json` prints stable machine-readable output.
- Human output is concise and Telegram/terminal friendly.
- No live model call unless command is `tick` or `daemon`.
- `task create` only writes a `Task` in `CREATED` state.

## Config shape

Add optional config section:

```yaml
agent_runtime:
  store_root: null            # null => <default-hermes-root>/agent-runtime
  default_provider: openai-codex
  default_model: gpt-5.5
  default_api_mode: codex_responses
  heartbeat_ttl_seconds: 900
  max_actions_per_tick: 1
  personas:
    pm:
      provider: openai-codex
      model: gpt-5.5
      api_mode: codex_responses
      toolsets: [file, session_search, todo]
    dev:
      provider: openai-codex
      model: gpt-5.5
      api_mode: codex_responses
      toolsets: [file, search, terminal, session_search, code_execution]
    qa:
      provider: openai-codex
      model: gpt-5.5
      api_mode: codex_responses
      toolsets: [file, search, terminal, browser, vision, session_search]
```

Stage 9 extends this shape with optional profile-bound persona execution:

```yaml
agent_runtime:
  personas:
    pm:
      hermes_profile: pm
      skills: [agent-runtime-harness, writing-plans]
    dev:
      hermes_profile: gpt-launcher
      skills: [agent-runtime-harness, flutter-ui-development, systematic-debugging, test-driven-development, requesting-code-review]
    qa:
      hermes_profile: launcher-qa
      skills: [agent-runtime-harness, flutter-ui-development, launcher-stagec-mcp-screenshot, systematic-debugging, requesting-code-review]
    alice_supervisor:
      hermes_profile: alice
      skills: [agent-runtime-harness]
```

These profile bindings must reuse Hermes profile OAuth/MCP/skills/config while preserving the shared Harness store root and role-based blocked-tool policy. See [Stage 9 — Profile-Bound Personas, Souls, and Skills](09-profile-bound-personas-souls-and-skills.md).

Precedence:

1. Explicit CLI flags.
2. Profile `config.yaml` `agent_runtime` section.
3. `agent_runtime.personas.default_personas()` and runtime defaults.
4. Environment override `HERMES_AGENT_RUNTIME_ROOT` for store root only.

## Shared-root rule

Profiles can configure persona defaults, but all profiles should see one task truth:

```text
<get_default_hermes_root()>/agent-runtime/
```

This mirrors the useful part of Kanban's profile-shared coordination model. It prevents PM/dev/QA profiles from each seeing a different mission board.

If `agent_runtime.store_root` is configured, it must be clearly documented as a global coordination override, not per-persona scratch space.

## Command behavior details

### `harness init`

- Creates store directories.
- Seeds default persona JSON files only if absent.
- Writes no secrets.
- Prints store root and persona IDs.

### `harness task create`

- Requires title and description.
- Creates `Task(state=CREATED, requested_by=<profile/user>)`.
- Emits `task.created`.
- Does not run PM automatically unless `--tick` is explicitly added later.

### `harness tick`

- Loads config/personas.
- Calls `TickEngine.tick_once()`.
- In tests, use fake runtime injection; live runtime only in integration/manual tests.

### `harness status`

- Calls `agent_runtime.status.build_status()`.
- Includes missing proof and open incidents once Stage 4/5 exist.

## Implementation tasks

1. Add failing parser test proving `hermes harness` exists.
2. Add failing CLI handler test for `task create --json` returning task ID.
3. Add failing test for `harness init` seeding default personas idempotently.
4. Add failing config test for profile config merging with defaults.
5. Add failing test that profile changes model/provider but store root remains shared.
6. Implement `agent_runtime/config.py`.
7. Implement `agent_runtime/cli_format.py`.
8. Implement `hermes_cli/harness.py` with thin handlers.
9. Wire `hermes_cli/main.py` minimally.
10. Run targeted CLI and `tests/agent_runtime/` tests.

## Tests

Required test files:

```text
tests/agent_runtime/test_config.py
tests/agent_runtime/test_cli_format.py
tests/hermes_cli/test_harness_cli.py
```

Test matrix:

- Parser exposes `harness` group and expected subcommands.
- `task create --json` writes a task and prints JSON with `task_id`.
- `task list --json` returns created task.
- `task show missing --json` exits nonzero with safe error.
- `init` is idempotent.
- Default personas load when no config exists.
- Configured persona toolsets are filtered by role policy.
- `--profile`/profile config affects model/provider, not shared task store root.
- `tick --json` can run with fake runtime and no network.

## Acceptance criteria

- User can create/list/show tasks from CLI.
- One fake tick can be triggered from CLI.
- Persona config is profile-aware and role-filtered.
- Store root remains shared by default.
- No gateway/TUI/Launcher/Unreal work is required.
- Existing Kanban commands continue to work unchanged.

## Risks / interventions

- **`hermes_cli/main.py` bloat:** keep additions tiny; delegate to `hermes_cli/harness.py`.
- **Profile/store confusion:** document and test config precedence.
- **Accidental live model calls in tests:** fake runtime injection is mandatory.
- **Command name churn:** ship as experimental `harness`; add `mission` alias later only after Tony validates UX.
- **Output instability:** `--json` schema must be stable because Launcher/automation may consume it later.
