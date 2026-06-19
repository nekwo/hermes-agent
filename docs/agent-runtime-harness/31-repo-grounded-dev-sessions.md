# Stage 31 — Repo-Grounded Dev Sessions

## Goal

Live Dev runs must start as repo-grounded engineering sessions instead of broad user-home sessions. When a task names an affected repo, Harness resolves that repo to a concrete local workdir, starts Dev with that workdir as cwd, allows normal project instruction loading (`AGENTS.md`, `CLAUDE.md`, `.cursorrules`), and tells Dev to use scoped brain/session search only when task context is insufficient.

This stage fixes the Stage 30 smoke finding where Dev performed real audit work but burned wall-clock budget in broad/low-yield discovery from `C:\Users\beast` and timed out before a decision.

## Fixed decisions

- Reuse the existing affected-repo alias policy: only absolute existing directories or narrow aliases (`agent-runtime-harness`, `hermes-agent`) resolve automatically.
- Dev/QA/PM independence remains unchanged. This stage changes execution grounding and prompt context, not state-machine authority.
- Project instruction files are loaded by the normal Hermes `AIAgent` context-file mechanism from the resolved cwd; Harness does not dump arbitrary repo files into prompts.
- Brain search means existing `session_search`/recall tooling and explicit prompt permission, not automatic Obsidian vault ingestion.
- Mission Control logs may show repo aliases and loaded context filenames, but never raw absolute private paths.

## Deep audit evidence

- `agent_runtime/ticker.py::_command_workdir_for_task` already resolves affected repos for deterministic command proof, but live persona execution does not use it.
- `agent_runtime/persona_runtime.py::_invoke_agent` calls `AgentRunRequest(... skip_context_files=True, user_message=render_context(ctx))`, so project files are intentionally skipped today.
- `agent_runtime/profile_runner.py` constructs `AIAgent` without setting cwd or `TERMINAL_CWD`; the agent therefore inherits the Hermes gateway/current process cwd.
- `agent/agent_init.py` documents `skip_context_files=False` as the path for `AGENTS.md`, `CLAUDE.md`/project context files from cwd.
- Dev default toolsets already include `session_search`, so scoped prior-context lookup can be used without new infrastructure.

## Implementation stages

### 31.1 Shared repo workdir resolver

Affected files:
- `agent_runtime/repo_context.py` or equivalent shared module
- `agent_runtime/ticker.py`
- tests in `tests/agent_runtime/`

Actions:
- Move/reuse affected repo resolution behind a shared helper.
- Preserve current fail-closed alias/path behavior.
- Expose a safe repo label for logs/prompts without absolute private paths.

Proof:
- Existing command-proof alias tests still pass.
- New tests cover dev context workdir resolution without broad fallback.

### 31.2 ProfileAgentRunner cwd support

Affected files:
- `agent_runtime/profile_runner.py`
- `tests/agent_runtime/test_profile_runner.py`

Actions:
- Add `workdir: Path | None` to `AgentRunRequest`.
- During `run_conversation`, temporarily set process cwd and `TERMINAL_CWD` to the resolved workdir, restoring both afterwards.
- Keep this inside the existing synchronous persona/profile context and tick lock; do not create background worker threads.
- Validate `workdir` exists and is a directory.

Proof:
- RED/GREEN test proves the fake agent observes cwd and `TERMINAL_CWD` equal to repo root during the run and the caller environment is restored afterwards.

### 31.3 Dev prompt/project context grounding

Affected files:
- `agent_runtime/persona_runtime.py`
- `agent_runtime/context_builder.py`
- tests in `tests/agent_runtime/test_persona_runtime_*.py`

Actions:
- For Dev runs with a resolved repo workdir, pass that workdir into `AgentRunRequest` and set `skip_context_files=False` so Hermes loads `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, etc. from cwd.
- Add redaction-safe context to the tick prompt: repo label, that cwd is the repo root, and loaded context filenames if present.
- Add Dev-only instruction: stay scoped to repo by default; use `session_search`/brain search only for prior decisions/conventions when task context is insufficient; do not dump secrets or vault pages.
- Emit a redaction-safe progress event such as `phase=inspect`, `step=repo_context_loaded`, `status=ready`.

Proof:
- Tests prove Dev request has `workdir`, `skip_context_files=False`, and context message mentions repo-grounded execution without raw absolute private paths.
- Tests prove missing context files are non-fatal and log `context_loaded: none`.

### 31.4 Tool truth regression

Affected files:
- `agent_runtime/profile_runner.py`
- `tests/agent_runtime/test_profile_runner.py`

Actions:
- Ensure raw `run.tool.finished` lifecycle events classify non-zero/timeout results the same way progress events do.
- Preserve redaction: no raw command args, paths, or result blobs in summaries.

Proof:
- Test covers `exit_code=124` and asserts `status=failed`, `exit_code=124` for lifecycle events.

## Acceptance criteria

- Dev agent starts with cwd at resolved affected repo for `agent-runtime-harness` / `hermes-agent` aliases and absolute repo paths.
- Project context-file injection is enabled only when a valid workdir is resolved.
- Prompt/log context is redaction-safe and basename/alias based; no absolute private path leaks into Mission Control.
- Dev can use session/brain search when needed, but automatic vault dumping is not introduced.
- Existing deterministic command proof workdir behavior remains unchanged.
- Targeted Harness tests pass, compileall passes, diff hygiene passes, independent review passes, and the change is committed locally.

## Implementation result

Completed in this stage:

- Added shared repo execution context resolution in `agent_runtime/repo_context.py`.
- Wired Dev persona runs to resolved workdirs with `AgentRunRequest.workdir`.
- Guarded `ProfileAgentRunner` cwd / `TERMINAL_CWD` / persona-profile environment mutation with a process-local reentrant lock and always restores caller state.
- Enabled project context loading for valid repo-grounded Dev runs.
- Changed `agent/prompt_builder.py` to load all supported project context sources in deterministic order: `.hermes.md`/`HERMES.md`, `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, and `.cursor/rules/*.mdc`.
- Made Dev fail closed before agent construction when `affected_repos` is missing or invalid.
- Redacted affected repo paths in rendered tick context, task snapshots, command-proof workdir errors, and progress payloads.
- Preserved command-proof alias behavior for both `agent-runtime-harness` and `hermes-agent`.

Verification:

- `PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m pytest -q -o addopts='' tests/agent_runtime tests/agent/test_prompt_builder.py` — PASS (`399 passed, 1 skipped, 1 warning`).
- `PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m compileall -q agent_runtime agent tests/agent_runtime tests/agent/test_prompt_builder.py` — PASS.
- `git diff --check` — PASS.
- `PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m hermes_cli.main harness smoke --json --temp-root --no-model` — PASS (`ok: true`, final_state `done`).
- Independent final review — PASS, no ship-blocking issues.

Remaining non-blocking follow-up:

- If future missions pass affected repo subdirectories instead of repo roots, consider normalizing absolute workdirs to their git root or softening prompt wording from “repo root” to “resolved repo workdir.”
