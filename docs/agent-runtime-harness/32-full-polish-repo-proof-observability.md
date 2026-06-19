# Stage 32 — Full Polish: Repo/Proof Observability

## Goal

Polish the Stage 31 live-goal path after the first successful repo-grounded smoke. The fresh smoke passed, but close inspection found two AAA-quality observability gaps:

1. Command-proof artifact redaction over-redacted safe Python keyword text such as `sort_keys=True` because the proof redactor treated any word containing `key` as a secret assignment.
2. Absolute affected repo paths that point at a subdirectory are currently used as-is, while Dev prompts say “repo root.” This can misload project context and reintroduce low-yield discovery for nested workdirs.

This stage keeps architecture stable: no new scheduler, log service, DB, broker, or analyzer. It only polishes existing proof redaction and repo context resolution.

## Fixed decisions

- Preserve redaction safety. Real secret-looking assignments such as `SECRET_KEY=value`, `api_key=value`, `private_key=value`, `TOKEN=value`, and bearer tokens must still redact.
- Do not redact safe language/API identifiers merely because they contain the substring `key` (`sort_keys=True`, `monkey=True`, `keyboard=True`).
- Normalize absolute affected repo paths upward to the nearest git root when possible; if no `.git` ancestor exists, keep the explicit directory behavior.
- Keep the existing narrow aliases: `agent-runtime-harness` and `hermes-agent`.
- Mission Control/proof outputs still expose safe aliases/basenames only, never absolute private paths.

## Implementation stages

### 32.1 Proof redaction precision

Affected files:

- `agent_runtime/proof_runner.py`
- `tests/agent_runtime/test_proof_runner.py`

Actions:

- Tighten secret-assignment detection to match sensitive variable names by token/underscore boundaries rather than substring matching.
- Keep bearer-token redaction unchanged.
- Add tests proving `sort_keys=True` remains readable and `SECRET_KEY`/`api_key`/`private_key` still redact.

Proof:

- `test_command_proof_redaction_preserves_safe_key_substrings`
- existing proof redaction tests remain green.

### 32.2 Git-root normalization for nested repo paths

Affected files:

- `agent_runtime/repo_context.py`
- `tests/agent_runtime/test_persona_runtime_fake.py` or dedicated repo-context tests

Actions:

- When an absolute affected repo path exists, walk upward until a `.git` file/directory is found and use that root.
- Keep non-git directory behavior unchanged.
- Ensure context-file discovery happens at the normalized root.

Proof:

- Test creates `repo/.git`, `repo/AGENTS.md`, and `repo/pkg/subdir`; affected repo points at `repo/pkg/subdir`; Dev observes cwd at `repo`, prompt/logs show safe repo label, and no absolute path leaks.

## Acceptance criteria

- Proof artifacts preserve safe code options containing `key` while still redacting real secret assignments.
- Dev affected repo subdirectories normalize to the git root for cwd and project context loading.
- Existing Stage 31 behavior and command-proof alias behavior remain unchanged.
- Targeted Harness tests pass.
- `compileall` and `git diff --check` pass.
- Independent deep audit review passes.
- Fresh live Harness smoke still reaches `done` with no incidents.

## Implementation result

Completed in this stage:

- Tightened command-proof redaction so safe identifiers like `sort_keys=True`, `monkey=True`, and `keyboard=True` remain readable while secret-looking assignments still redact.
- Added support for quoted secret assignment values (`TOKEN=...`, `password='...'`) in proof artifact redaction.
- Normalized absolute affected repo subdirectories to the nearest `.git` root for Dev repo context and command-proof workdir labels.
- Preserved non-git explicit-directory behavior.
- Extended context request roots to include the Harness runtime store root so QA can request safe proof artifacts by their persisted relative path.
- Reused git-root normalization in context request affected-repo roots so QA/Dev file-read requests against nested affected repos can still resolve root project files.
- Sanitized unresolved affected-repo exception details to safe labels instead of raw absolute paths.

Verification:

- RED tests first failed for `sort_keys=True` over-redaction and nested repo workdir normalization.
- `PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m pytest -q -o addopts='' tests/agent_runtime/test_context_requests.py tests/agent_runtime/test_proof_runner.py tests/agent_runtime/test_repo_context.py` — PASS (`16 passed`).
- `PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m pytest -q -o addopts='' tests/agent_runtime tests/agent/test_prompt_builder.py` — PASS (`406 passed, 1 skipped, 1 warning`).
- `PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m compileall -q agent_runtime agent tests/agent_runtime tests/agent/test_prompt_builder.py` — PASS.
- `git diff --check` — PASS.
- `PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m hermes_cli.main harness smoke --json --temp-root --no-model` — PASS (`ok: true`, final_state `done`).
- Independent deep audit follow-up — PASS after fixing quoted-secret redaction blocker.

Live proof:

- Fresh canonical Harness smoke task `task_ab6f224d` reached `done` with `settle_7c224739`, 6 ticks, `stop_reason=task_terminal`, and `open_incidents=0`; this smoke exposed the runtime-root context request gap, which was fixed in the same stage.
- Final canonical Harness smoke task `task_2872be8d` reached `done` with `settle_588d3b4a`, 5 ticks, `stop_reason=task_terminal`, `open_incidents=0`, and `context_requests=[]`.
- Final Dev was repo-grounded in `hermes-agent`, attached command proof `test_task_2872be8d_stage_32_final_polish_proof_run_d65bfd01b9dc_0_e1e73627` (`exit_code=0`, `duration_ms=2947`, `shell=bash`).
- Final QA approved with proof `proof_qa_22584d99`.

Remaining non-blocking follow-up:

- None for this Stage 32 scope.
