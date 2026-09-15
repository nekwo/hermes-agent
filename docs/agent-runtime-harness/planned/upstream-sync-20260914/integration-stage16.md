# Checkpoint 16: native Windows CI is now executable

Pushed candidate: `cabc5509e1a35266328c7e7b31c58405bcdd36b1`. Hermes main is still the original `34ad8ba33f2508ab10bb24a26f0377ddb62660cb`; no managed runtime maintenance has occurred. Launcher delivery remains `241391dd66a55d4375fee81609c9195d8240c1b7` on local/origin main.

## CI repairs and evidence

- `6ca9819437812b2db4b1ecd2737e350071a15821`: Windows CI requested an upstream-only 32-core runner and remained queued. Forks now use `windows-latest`; upstream retains its runner. YAML parsed, both OS lanes retained, and the Windows job actually started.
- `ac4b6d879a7ef6ffc967e9d3850985e30283bc35`: the real dead-relaunch refusal polls for 30 seconds, but the inherited test timeout was also 30 seconds. Its single-test budget is now 90 seconds; production timing and refusal assertions are unchanged. The subsequent Windows job passed this witness and reached later tests.
- `1f00dd7734`: two upstream credential suites needed the fork's explicit real-file test marker plus an autouse temporary `Path.home()`. This preserves the file-access guard while exercising real synthetic credential persistence. Canonical local run of both suites and `tests/test_claude_code_credentials_file_gate.py`: **32 passed, zero failed** (`auth-real-file-scope-final.log`). The first local attempt had a missing `Path` import, fixed before this proof.
- `cabc5509e1`: the interpreter fixture now patches `resolve_managed_python`, which owns this selection. Two fake-Popen spawn tests moved to `tests/gateway/test_windows_gateway_spawn.py`: the CLI directory's unconditional session-long spawn fence intentionally cannot be bypassed by its pause-token marker. The fence was not weakened. Canonical focused Windows run: **3 passed, zero failed** (`windows-spawn-owner-final.log`).
- The real PowerShell pipe-drain test has a 300-second subprocess diagnostic budget but inherited a 30-second pytest limit. Its test timeout is now 330 seconds. Canonical local native run: **6 passed, zero failed**, 61.2 seconds (`windows-pipe-drain-final.log`). Probe children and files are owned by its temporary fixture.

Current CI is run `35010813557` on the pushed candidate. The prior partial Python run only reported the two now-repaired credential suites before cancellation. This is not evidence for the uncompleted remainder. Full CI, native Windows completion, and managed-runtime delivery verification remain required. Recovery logs are retained in the audit worktree's ignored `qa-artifacts` directory.
