# Stage 45 Mission Daemon Live Hardening Report

Date: 2026-06-05
Author: Codex independent audit

## Scope

This report records the live Harness daemon hardening pass after the Mission Control / Agent Runtime Harness Stage 44 plan. The goal was to run real disposable missions through Neko -> Dev -> Neko QA coordination -> QA -> Harness close, observe freezes or inefficient loops, implement root-cause fixes, preserve evidence, and leave the runtime clean.

## Problems Found

1. Dev repo grounding could be wrong for Hermes tasks.
   - Evidence: disposable task `task_86dc3690` was scoped to `hermes-agent`, but the Dev persona loaded `EterniaLauncher` context because the persona `repo_scope` overrode task `affected_repos`.
   - Impact: Harness CLI proof commands failed from the wrong repo; the mission blocked instead of completing.

2. Blocked tasks with no open incident could freeze.
   - Evidence: after the failed Dev run, `task_86dc3690` reached `blocked` with no open incident and no useful next action until manual intervention.
   - Impact: Tony would have to babysit blocked/no-incident missions.

3. QA could not reliably review command proof from prompt context.
   - Evidence: QA blocked on `task_86dc3690` because proof metadata had command and exit code, but not readable compact output; artifact paths such as `proofs/.../artifacts/*.log` were not resolved by the live agent.
   - Impact: QA repeated file-read attempts and blocked even when Harness had preserved proof artifacts.

4. Proof excerpts could expose local absolute paths.
   - Evidence: status-command proof output included runtime fields such as `store_root`, `runtime_root`, `hermes_home`, and `interpreter`.
   - Impact: proof context was too chatty and not consistently redaction-safe for operator/UI surfaces.

5. No-edit exact-command smoke goals were inefficient.
   - Evidence: before prompt hardening, Dev pre-ran terminal commands before returning `request_test_run`, increasing Dev tokens to about 125k in `task_a86846df`.
   - Impact: deterministic no-edit proof missions took longer than necessary and spent more model/tool budget.

## Fix Strategy Implemented

1. Repo scope compatibility guard.
   - Dev persona `repo_scope` is now used only when it resolves to the same repo as the task affected repo.
   - Otherwise the runtime starts Dev in the task-selected repo.

2. Bounded Neko recovery for blocked/no-incident tasks.
   - Added `neko_block_recovery_attempted` recovery flag.
   - State machine routes blocked/no-incident tasks to one Neko recovery pass before settling to a real blocked intervention.
   - Neko context-request/block paths mark the recovery flag so they cannot loop forever.

3. Self-contained command proof records.
   - Command proof metadata now includes `artifact_exists`, `artifact_bytes`, `artifact_relative_path`, `stdout_excerpt`, and `stderr_excerpt`.
   - Context rendering exposes only allowed safe fields so QA can decide from the proof packet without reading raw artifact files.
   - Raw proof logs remain preserved under runtime `proofs/` and archives.

4. Proof output redaction.
   - Secret redaction now also masks Windows and Unix absolute paths in command output.
   - Excerpts are bounded with head/tail truncation and omission markers.

5. Dev no-edit fast path.
   - Dev prompt now tells agents to return `request_test_run` immediately for no-edit stages with exact command proof requirements instead of terminal-preflighting those commands.

## Live Evidence

Initial failed mission:
- `task_86dc3690` exposed wrong repo grounding, QA proof readability gaps, repeated Dev/QA loops, and a final open incident.
- Evidence was preserved by archive batch `20260604T211136755620Z_archive_ready`.

First fixed smoke:
- `task_89eebc84` reached `done`.
- Daemon result: `settle_e2ecc4e0`, `settle_stop_reason=no_eligible_action`, `actions_last_tick=5`, `settle_ticks=6`.
- QA proof: `proof_qa_879302c0`, verdict `approved`.
- Evidence preserved by archive batch `20260604T211510687228Z_archive_ready`.

Final redaction smoke:
- `task_a86846df` reached `done`.
- Proof excerpts redacted local paths as `<path:agent-runtime>`, `<path:alice>`, and `<path:python.exe>`.
- QA proof: `proof_qa_998ae682`, verdict `approved`.
- Evidence preserved by archive batch `20260604T211928556659Z_archive_ready`.

Post-commit no-edit fast-path smoke:
- `task_00ea7a3e` reached `done`.
- Dev used no terminal preflight: tool pattern was repo context plus `skill_view`, then `request_test_run`.
- Dev run `run_422643278a54`: `api_calls=2`, `tool_turns=1`, `total_tokens=42757`.
- QA proof: `proof_qa_ce4077d5`, verdict `approved`.
- Evidence preserved by archive batch `20260604T212246507068Z_archive_ready`.

Final runtime status:
- daemon: `offline`
- open tasks: `0`
- open incidents: `0`
- active runs: `0`
- observability health: `healthy`

## Verification Commands

Harness:
- `python -m pytest -o addopts='' -q tests/agent_runtime/test_personas.py tests/agent_runtime/test_persona_prompts.py tests/agent_runtime/test_qa_intelligence_hardening.py tests/agent_runtime/test_proof_runner.py tests/agent_runtime/test_context_builder.py tests/agent_runtime/test_persona_runtime_fake.py tests/agent_runtime/test_state_machine.py tests/agent_runtime/test_ticker.py tests/agent_runtime/test_status.py tests/agent_runtime/test_snapshot.py`
  - Result: `122 passed`
- `python -m pytest -o addopts='' -q tests/agent_runtime/test_store.py tests/agent_runtime/test_events.py tests/agent_runtime/test_daemon.py tests/agent_runtime/test_migrations.py tests/agent_runtime/test_context_requests.py tests/agent_runtime/test_state_machine.py tests/agent_runtime/test_ticker.py tests/agent_runtime/test_status.py tests/agent_runtime/test_snapshot.py tests/hermes_cli/test_harness_cli.py`
  - Result: `129 passed`

Launcher:
- `flutter test test/features/mission_control`
  - Result: `52 passed`

## Commits

- `ef9437354 fix(harness): harden mission daemon proof recovery`

## Remaining Risk

No correctness blocker remains from this pass. The remaining optimization opportunity is to make exact-command no-edit stages deterministic before Dev model dispatch, but the prompt-level fast path is live-verified and significantly reduced token/tool usage without changing the architecture.
