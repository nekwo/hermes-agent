# Stage 20 — Command Proof Workdir Alias Resolution

Status: completed 2026-05-30

## Goal

Fix the live Harness smoke blocker where Dev `request_test_run` command proof fails when PM emits a logical affected repo alias such as `agent-runtime-harness` instead of an absolute filesystem path.

Observed live evidence:

- Task: `task_1340d808` — `Smoke test Mission Control live goal path`.
- PM succeeded and set `affected_repos=["agent-runtime-harness"]`.
- Dev failed while collecting command proof:
  - `request_test_run could not resolve a valid affected repo workdir; affected_repos=['agent-runtime-harness']`
  - incident: `inc_5a064cb4`, kind `harness_action_failure`.

## Product stance

Mission Control should tolerate common Harness-native aliases for its own repo. PM should still prefer exact paths for product repos, but logical aliases for the Harness itself must not break safe command proof.

## Fixed architecture decisions

1. Keep command proof rooted in a real directory.
2. Preserve fail-closed behavior for unknown aliases or missing product paths.
3. Only accept filesystem workdirs from `affected_repos` when the entry is an existing absolute directory. Relative path-like values such as `.`, `./`, or `agent_runtime/..` must fail closed instead of implicitly using the Harness checkout.
4. Add a narrow built-in alias resolver for the Harness repo only:
   - `agent-runtime-harness`
   - `hermes-agent`
5. Do not read secrets or mutate product repos.
6. Do not invent a broad registry yet; if more product aliases are needed, stage a configurable alias map later.
7. Ambiguous names such as `mission-control` must continue to fail closed until a verified repo mapping exists.

## Codebase audit

- `agent_runtime/ticker.py::_command_workdir_for_task` selects the proof workdir.
- Existing behavior:
  - explicit `command_workdir` wins;
  - directory-valued `task.affected_repos` wins;
  - any non-empty unresolved `affected_repos` raises `ValueError`;
  - no affected repos falls back to `Path.cwd()`.
- Existing tests in `tests/agent_runtime/test_ticker.py` cover:
  - explicit workdir command proof;
  - directory-valued affected repo command proof;
  - missing affected repo opens a harness action failure incident.

## Stage plan

### Stage 1 — Regression test

Add a failing test proving `affected_repos=["agent-runtime-harness"]` resolves to the current Hermes checkout and command proof succeeds without an explicit workdir.

Acceptance:

- Test fails before implementation with the existing `affected repo workdir` error.
- Test verifies proof artifact workdir points at the Hermes checkout, not the runtime store or an arbitrary cwd.

### Stage 2 — Minimal resolver

Implement a small helper in `agent_runtime/ticker.py`:

- normalize aliases with lowercase and `[-_ ]` collapse;
- map known Harness aliases to the repo root derived from `ticker.py` location;
- only return the alias path if it is an existing directory;
- preserve the existing missing-repo incident behavior for unknown aliases.

Acceptance:

- New regression test passes.
- Existing missing affected repo test still passes.

### Stage 3 — Verification and live smoke recovery

Run focused tests and hygiene:

```bash
bash scripts/run_tests.sh tests/agent_runtime/test_ticker.py -q
venv/Scripts/python.exe -m compileall agent_runtime tests/agent_runtime
venv/Scripts/hermes.exe harness status --json
```

Then close the stale live smoke incident with the fix reason and rerun the goal ticks or create a fresh smoke goal if state recovery is unsafe.

## Verification results

- RED: `venv/Scripts/python.exe -m pytest -o addopts='' tests/agent_runtime/test_ticker.py::test_tick_collects_command_proof_in_harness_repo_alias_when_no_explicit_workdir -q` failed with the original `affected repo workdir` incident path.
- GREEN: same regression test passed after alias resolver implementation.
- Review fix: restricted alias normalization to spaces/hyphens/underscores only, removed broad aliases, require real filesystem workdirs to be absolute paths, and added fail-closed tests for `mission-control`, path-like, dotted, punctuation-heavy, and relative malformed aliases.
- Targeted: `venv/Scripts/python.exe -m pytest -o addopts='' tests/agent_runtime/test_ticker.py -q` passed `19 passed`.
- Hygiene: `venv/Scripts/python.exe -m compileall agent_runtime tests/agent_runtime && git diff --check` passed.
- Live smoke recovery:
  - closed stale incident `inc_5a064cb4` with fix reason;
  - reran live task `task_1340d808` through Dev proof collection, Dev QA handoff, QA verdict, and deterministic close;
  - final state `done`, stage `stage_1=passed`, proof count `3`, open incidents `0`.
- Post-smoke Harness status: active runs `0`, blocked tasks `0`, health `healthy`, interventions `[]`.

## AAA gaps to reassess after implementation

- Configurable product repo alias map may be needed later for Launcher/Backend aliases.
- PM prompt should prefer absolute paths for `affected_repos` when task scope names a repo.
- Incident detail could include suggested known aliases to make operator recovery faster.
